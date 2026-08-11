# ISL Schedule-Tree Construction & Legality Gate

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, the cp310 wheel — module `neuronxcc/starfish/penguin/targets/transforms/experimental/TongaIslDependenceAnalysis.cpython-310-x86_64-linux-gnu.so` (Cython, unstripped, docstrings intact; md5 `a286f6ee6d109d7a23060c0ba4995100`). cp311/cp312 ship the same code under different mangled prefixes and VAs. Every offset is a `.text` VA in the cp310 object.*

## Abstract

`NeuronIslDependenceAnalysis` (the `TongaIslDependenceAnalysis` Cython extension) wraps stock **islpy ~2023.1** to answer one question about a candidate instruction ordering: *is it dependence-legal?* This page documents the half of that class that touches the ISL **schedule tree** — the builders `get_schedule_tree` / `add_band` / `add_sequence_filter`, and the legality kernel `check_valid_schedule` / `internal_check_valid_schedule` that returns an `Optional[DependenceViolation]`. The dependence/domain/access *construction* it consumes (`get_dependency_map`, the access relations, `get_child_domain_union_set`'s body) is the sibling subject documented in [ISL Dependence Graph](isl-dependence-graph.md).

The single most important finding here is a **negative** one, and it settles the long-standing open question about this component: **there is no scheduler.** Penguin contains neither a custom Pluto-style affine cost-model search nor a call to stock ISL's own `isl.Schedule.compute_schedule`. It only *builds* a schedule tree that mechanically mirrors the existing loop nest, and *validates* a client-supplied candidate ordering against a hand-rolled May-dependence relation. The textbook polyhedral pipeline — `ScheduleConstraints` with validity/proximity/coincidence, an ILP solve, Feautrier or Pluto affine-form discovery — is **entirely absent** from this `.so` and from every other `.so` in the penguin tree (verified by raw symbol enumeration, [§ The No-Scheduler Verdict](#the-no-scheduler-verdict)). The "scheduling decision" lives in the **client**, `SoftwarePipelineCodeGen._reorder_instructions`, as a heuristic permutation driven by per-instruction `execution_order` stage annotations; this class is purely its legality oracle.

If you have read LLVM's polyhedral or modulo-scheduling code, the mental model to discard is "the compiler searches for a schedule." The correct model is: a known reordering is proposed, ISL re-expresses every dependence edge in *schedule-time × schedule-time* space, and the gate asserts that **every** edge runs strictly forward in time (lexicographic positivity). A non-empty set of backward edges is a `DependenceViolation`; the reorder is rejected.

For reimplementation, the contract is:

- The schedule-tree shape: a top **DOMAIN** node over the whole-kernel iteration domain, then **SEQUENCE → per-statement FILTER → BAND** built by a recursive IR walk (`get_schedule_tree_helper`), with band coordinates being the *existing* induction variables, never searched coefficients.
- The legality test: `dependences.apply_domain(schedule).apply_range(schedule)` followed by a per-edge `lex_lt_at_multi_pw_aff` strict-before emptiness check.
- The May-dependence pairing in `check_valid_schedule`: `srcs.apply_range(dsts.reverse()).intersect(order)` per `{RAW, WAW, WAR}` class, where `order = sched.lex_le_union_map(sched)`.
- The architectural boundary: this class **validates**, the client **decides**.

| | |
|---|---|
| **Module** | `experimental/TongaIslDependenceAnalysis.cpython-310-…so` (Cython, 1.85 MB) |
| **Class** | `NeuronIslDependenceAnalysis` (qualnames intact in `.rodata`) |
| **Entry — build** | `get_schedule_tree` @ `0x1f600` → `isl.Schedule` |
| **Entry — gate** | `check_valid_schedule` @ `0x273f0` → `Optional[DependenceViolation]` |
| **Legality kernel** | `internal_check_valid_schedule` @ `0x3f080` |
| **ISL** | stock islpy ~2023.1 (pip dependency; internals NOT reversed) |
| **Sole client** | `SoftwarePipelineCodeGen._reorder_instructions` (separate `.so`) |
| **Scheduler present?** | **No** — zero `compute_schedule`/`ScheduleConstraints`/`pluto`/`coincidence`/`cost` symbols anywhere in penguin |

> **NOTE —** the eight VAs cited below (`0x1f600`, `0x24bc0`, `0x22b20`, `0x273f0`, `0x3f080`, `0x3bbd0`, `0x33a70`, `0x18ce0`, plus the `0x1d7a0` singleton closure) are the **`__pyx_pw_…` public method wrappers**, the canonical entry symbols in `function_addresses.json`. IDA decompiles each with the Cython method body inlined, so these are the addresses to disassemble; the separately-numbered `__pyx_pf_…` body slots are not exported as distinct named functions in this object.

---

## The No-Scheduler Verdict

### Purpose

This is the headline result and the reason the page exists. Before any mechanism, establish *what is not here*, because the absence is what classifies the whole component.

### Evidence — presence

Every schedule-related token the class *does* carry is a schedule-**tree** manipulation or a Python method name, recovered verbatim from the `.rodata` string pool:

```text
isl.Schedule          isl.ScheduleNode        (the tree object + node object)
get_schedule          get_schedule_tree       get_schedule_tree_helper
add_band              add_sequence_filter
insert_partial_schedule  insert_sequence  insert_mark
from_domain  var_on_domain  map_from_domain_and_range  identity_on_domain_space
lex_le_union_map  lex_lt_at_multi_pw_aff  apply_domain  apply_range
check_valid_schedule  internal_check_valid_schedule  double_schedule_relation
DependenceViolation   DependenceType   DebugLevel
```

`isl.Schedule` here is a *container*: the glue builds it by hand with `from_domain`/`insert_*` and reads it back with `.get_map()`. There is no objective function and no constraint object.

### Evidence — absence

Raw symbol enumeration over this `.so`'s string table returns **zero** hits for every entrypoint a real scheduler would need (each grepped individually against `*_strings.json`):

| Symbol probed | Role in a real polyhedral scheduler | Hits in this `.so` | Hits across penguin |
|---|---|---|---|
| `compute_schedule` | ISL Pluto/Feautrier scheduler entry | 0 | 0 |
| `ScheduleConstraints` / `schedule_constraints` | constraint object the scheduler solves | 0 | 0 |
| `set_validity` / `set_proximity` / `set_coincidence` | Pluto constraint setters | 0 | 0 |
| `set_max_coefficient` / `serialize_sccs` | Pluto tuning knobs | 0 | 0 |
| `pluto` / `coincidence` / `carry_dependences` / `cost_model` | cost-model vocabulary | 0 | 0 |
| `compute_flow` / `union_access_info` / `UnionAccessInfo` | stock ISL exact dataflow | 0 | 0 |

The "across penguin" column was verified by listing matches over `*TongaIsl*/*_strings.json` and `*Simplifier*/*_strings.json` — the ISL extensions where such a call would have to live — and over the `SoftwarePipelineCodeGen` client; all returned empty.

> **GOTCHA —** the only `cost` hit anywhere in this subsystem is a *docstring* in the base `IntegerSetAnalysis.so` ("For ISL APIs whose cost scales super-linearly (e.g. `is_subset`…)") — a performance caveat, not a scheduler cost model. A keyword sweep for "cost" that does not read context will mis-flag it.

### Classification

`NeuronIslDependenceAnalysis` is a **schedule validator / legality oracle**. It answers "is this candidate ordering dependence-legal?" and nothing else. There is no ILP, no affine-coefficient search, no Feautrier/Pluto. The reorder *decision* belongs to the client (see [The Client](#the-client--who-supplies-the-schedule)).

> **GOTCHA — `double_schedule_relation` and `scheduled_*_deps` are local-variable names, not calls.** Cython folds source-level local names into the global string pool, so these appear in the string-init table (`__Pyx_CreateStringTabAndInitStrings`) yet never as a `getattr` in any body. They name intermediate values in the legality logic ([§ Legality Kernel](#legality-kernel--internal_check_valid_schedule)). `double_schedule_relation` in particular is an *inline expression* — islpy has no primitive of that name — so a string sweep that reads them as scheduler entrypoints will mis-classify the whole component.

---

## Schedule-Tree Builder — `get_schedule_tree`

### Purpose

The entrypoint that turns a penguin loop-nest IR rooted at `stmt` into a stock `isl.Schedule` whose tree shape exactly mirrors the nest. This is the schedule that `check_valid_schedule` later validates (when the client does not supply its own candidate).

### The data model (verbatim docstring)

The class docstring, recovered intact from `.rodata`, is the canonical specification of the output tree. For the nest `for i in 0..10 { S0; for j in 0..20 { S1 } S2 }` it produces:

```text
Entrypoint to generate an ISL Schedule Tree from backend penguin IR.
  schedule: "{ s0[i] -> [(i)]; s1[i, j] -> [(i)]; s2[i] -> [(i)] }"
    - filter: "{ s0[i] : 0 <= i <= 10 }"
    - filter: "{ s1[i, j] : 0 <= i < 10 and 0 <= j < 20 }"
        schedule: "[{ s1[i, j] -> [(j)] }]"
    - filter: "{ s2[i] : 0 <= i <= 10 }"
```

Read this as: an outer **BAND** schedules every statement by its `i` coordinate; a **SEQUENCE** of three **FILTER** children (one per statement) gives the order at that level; the `S1` filter carries an inner **BAND** over its `j` coordinate.

### Algorithm

```c
// get_schedule_tree @ 0x1f600  (entrypoint)
isl.Schedule get_schedule_tree(self, stmt):
    dom  = self.get_child_domain_union_set(stmt);     // UnionSet of every stmt domain
    root = isl.ScheduleNode.from_domain(dom).child(0); // DOMAIN node, descend to its child
    node = self.get_schedule_tree_helper(stmt, root);  // fill SEQUENCE/FILTER/BAND recursively
    return node.get_schedule();                        // finished isl.Schedule
```

The ordered `from_domain → child → get_schedule_tree_helper → get_schedule` call chain appears in that order in the decompiled body of `0x1f600`. The top node is **always** a DOMAIN over the whole-kernel iteration domain; everything below is the helper's work.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `get_schedule_tree` | `0x1f600` | entrypoint; builds `isl.Schedule` | CERTAIN (docstring + body) |
| `get_schedule_tree_helper` | `0x3bbd0` | recursive tree assembler | HIGH |
| `get_child_domain_union_set` | `0x33a70` | subtree-domain `UnionSet` (shared with the dependence half) | CERTAIN |

---

## Recursive Assembler — `get_schedule_tree_helper`

### Purpose

Walks the penguin IR and emits schedule nodes, mirroring the domain-building traversal but producing BANDs and SEQUENCE/FILTERs instead of sets. Dispatches three ways on the IR class of `stmt`.

### Algorithm

```c
// get_schedule_tree_helper @ 0x3bbd0
ScheduleNode get_schedule_tree_helper(self, stmt, root):
    if isinstance(stmt, NeuronInst):              // a single instruction = a leaf statement
        node = self.add_band(root, domain, var);  // schedule THIS stmt by an outer IV coordinate
        if stmt.has_ap_indices:                    // instruction carries AP index dims
            with self.in_predicate_domain(stmt):   // push the loop level (ctx mgr, __enter__/__exit__)
                node = node.insert_mark(<mark>);    // tag the inner band region
                for k in self.enumerate_ap_indices(stmt):
                    node = self.add_band(node, ..., var=k);  // inner 1-D band per AP index dim
                                                              //  ⇒ the "[{ s1[i,j] -> [(j)] }]" sub-band
    elif isinstance(stmt, (Axis, ScopeRegion)):   // a loop level / region = a SEQUENCE of children
        node = self.add_sequence_filter(root, stmt.instructions);  // descends/recurses per child
    else:
        assert False;  // "Unhandled IR class in NeuronISLDependenceAnalysis"  (literal in .rodata)
    return node;
```

The 3-way `isinstance` dispatch, the `has_ap_indices` / `in_predicate_domain` / `insert_mark` / `enumerate_ap_indices` vocabulary, and the `"Unhandled IR class in NeuronISLDependenceAnalysis"` assertion string are all present.

A `NeuronInst` gets a 1-D **outer** band (its position in the enclosing loops). If it additionally has AP indices it gets a **mark-tagged inner** band per index dim — the deeper schedule coordinates (`[(j)]` in the docstring). `Axis`/`ScopeRegion` build the SEQUENCE of per-child FILTERs and recurse.

---

## Band Builder — `add_band`

### Purpose

Inserts exactly **one** 1-D schedule band that schedules each statement instance by its `var`-th set-dimension coordinate. Bands are stacked by repeated calls (one per loop level / AP index level). This is where a real scheduler would *choose* affine coefficients — here the coordinate is mechanically the existing induction variable `var`.

### Algorithm

```c
// add_band @ 0x24bc0  (exact isl call shapes [INFERRED] from islpy 2023.1 semantics)
ScheduleNode add_band(self, root, domain, var):
    bsl  = root.domain().get_basic_set_list();
    mupa = None;
    for k in range(bsl.n_basic_set()):             // one entry per statement tuple in this domain
        bset = bsl.get_at(k);
        sp   = bset.get_space();
        ma   = isl.MultiAff.zero(<band map space from sp via map_from_domain_and_range>);
        aff  = isl.Aff.var_on_domain(isl.LocalSpace.from_space(sp),
                                     dim_type.set, var);   // band coordinate = set-dim `var`
        ma   = ma.set_aff(0, aff);                          // 1-D band
        m    = isl.MultiUnionPwAff.from_multi_aff(ma);
        mupa = m if mupa is None else mupa.union_add(m);    // fold across all statement tuples
    return root.insert_partial_schedule(mupa);              // "{ sN[ivs] -> [(ivs[var])] }"
```

The decompiled body of `0x24bc0` carries the call sequence `n_basic_set → get_at → map_from_domain_and_range → MultiAff → set_aff → var_on_domain → MultiUnionPwAff → from_multi_aff → union_add → insert_partial_schedule`, in that order — an exact match.

> **QUIRK —** `union_add` folds the per-statement (per-basic-set) affine forms into one `MultiUnionPwAff` so a single band covers **every** statement present in `root.domain()` at that depth. That is why the outer band in the docstring is `{ s0[i]->[(i)]; s1[i,j]->[(i)]; s2[i]->[(i)] }` — three tuples, one band, one shared coordinate name. No coefficient is searched; `var` indexes the existing IV.

---

## Sequence Builder — `add_sequence_filter`

### Purpose

Inserts a **SEQUENCE** node whose children are per-statement **FILTER**s, then recursively populates each filtered subtree. This is the SEQUENCE → FILTER level of the tree: each child statement of an `Axis`/`ScopeRegion` becomes a sibling filter holding its own iteration domain.

### Algorithm

```c
// add_sequence_filter @ 0x22b20
ScheduleNode add_sequence_filter(self, root, stmts):
    usl = isl.UnionSetList.alloc(self.ctx, len(stmts));
    for s in stmts:
        usl = usl.add(self.get_child_domain_union_set(s));  // one FILTER set per child stmt
    node = root.insert_sequence(usl);                        // SEQUENCE of FILTER children
    for i, s in enumerate(stmts):
        node = node.child(i);                                // enter i-th FILTER subtree
        node = self.get_schedule_tree_helper(s, node);       // recurse to fill its bands
        node = node.parent();                                // back up to the SEQUENCE
    return node;
```

The body of `0x22b20` shows `UnionSetList`, `get_child_domain_union_set`, `insert_sequence`, and the recursive `get_schedule_tree_helper` call — the distinctive markers of this routine.

> **NOTE —** the *order* of children in `stmts` **is** the schedule order at this loop level, and that order is whatever the IR (or the client's reorder) hands in. The builder does not reorder anything; it transcribes the order it is given. This is the precise hinge of the no-scheduler claim: reordering is a property of the input, not an output of this code.

---

## Legality Gate — `check_valid_schedule`

### Purpose

The public legality oracle. Given a candidate `new_schedule` (or, if `None`, the schedule of `root`'s own nest as a self-consistency check), test it against the dependence graph and return the first `DependenceViolation` found, or `None` if legal.

### Signature

```text
check_valid_schedule(self, root, insts, new_schedule, dependence_types={RAW, WAR, WAW})
    -> Optional[DependenceViolation]
```

- `root` — penguin IR root, used to build the default tree if `new_schedule` is `None`.
- `new_schedule` — the **candidate** `isl.Schedule` to validate (client-supplied; e.g. a reordering).
- `insts` — the instruction list scoping the dependence graph.
- `dependence_types` — `Set[DependenceType]`, default all three.

### Algorithm

```c
// check_valid_schedule @ 0x273f0  (call sequence as decompiled)
Optional<DependenceViolation> check_valid_schedule(self, root, insts, new_schedule,
                                                   dependence_types):
    reads, writes = self.get_dependency_map(insts);                 // May-access maps
    sched = (new_schedule or self.get_schedule_tree(root)).get_map(); // UnionMap sN[ivs] -> [time]
    sched = self.try_simplify(sched);                                // coalesce/gist (IslSimplifier)
    order = sched.lex_le_union_map(sched);                           // happens-before (<=) in time
    for dt in dependence_types:                                      // identical body x3
        if   dt == RAW: srcs, dsts = writes, reads;                  // write -> later read   (flow)
        elif dt == WAW: srcs, dsts = writes, writes;                 // write -> later write  (output)
        elif dt == WAR: srcs, dsts = reads,  writes;                 // read  -> later write  (anti)
        pairs = self.try_simplify(srcs.apply_range(dsts.reverse())); // same-allocation inst pairs
        pairs = self.try_simplify(pairs.intersect(order));           // schedule-ordered pairs
        v = self.internal_check_valid_schedule(pairs, sched, debug_info);
        if v is not None:
            return DependenceViolation(v.src_id, v.dst_id, tensor, dt, msg);
            // msg one of: "RAW violation on tensor: " / "WAR violation on tensor: " /
            //             "WAW violation on tensor: "  (literals in .rodata)
    return None;  // no class produced a violation -> schedule is LEGAL
```

The decompiled body of `0x273f0` runs the prologue `get_dependency_map → get_schedule_tree → get_map → try_simplify → try_simplify → lex_le_union_map`, then **three** identical `DependenceType → try_simplify → apply_range → reverse → try_simplify → intersect → internal_check_valid_schedule` blocks — one per RAW/WAW/WAR class.

> **GOTCHA —** the pairing is **May-dependence**, not exact dataflow. `pairs` are *all* access-pairs touching the same allocation that are schedule-ordered (`apply_range(reverse)` then `intersect(order)`), built without `compute_flow`/`union_access_info` (both absent). A reimplementer who reaches for ISL exact dataflow will produce a *different, sparser* relation and may admit reorderings this gate rejects. The conservatism is deliberate.

> **NOTE —** the two-stage `le`-then-`lt` layering matters. `order = lex_le_union_map(self)` (≤, same-or-before) **prunes** the candidate pair set to schedule-consistent edges; the strict `lex_lt` inside `internal_check_valid_schedule` is the **actual** legality assertion. The `≤` does not by itself prove legality.

---

## Legality Kernel — `internal_check_valid_schedule`

### Purpose

The per-dependence legality kernel. Re-expresses each dependence edge in schedule-time × schedule-time space (the inline `double_schedule_relation`), then asserts every edge is strictly forward in time. Returns the offending edge as a `DependenceViolation`, or `None`.

### Signature

```text
internal_check_valid_schedule(self, dependences, schedule, debug_info)
```

`dependences` is a `UnionMap` of `{ src_inst[ivs] -> dst_inst[ivs'] }` pairs touching the same allocation (from `check_valid_schedule`); `schedule` is the candidate's `UnionMap` `sN[ivs] -> [time]`; `debug_info` gates a diagnostic distance dump.

### The double-schedule relation

```c
// inline at the top of internal_check_valid_schedule (NOT a method/primitive)
double_schedule_relation = dependences.apply_domain(schedule).apply_range(schedule);
    // dependences          : { src_inst[ivs] -> dst_inst[ivs'] }   (iteration x iteration)
    // apply_domain(sched)  : rewrite SOURCE side  src_inst[ivs]  -> src_time[t]
    // apply_range(sched)   : rewrite DEST   side  dst_inst[ivs'] -> dst_time[t']
    // result               : { src_time[t] -> dst_time[t'] }       (TIME x TIME)
```

"Double" = the schedule applied on **both** ends. The decompiled body shows `apply_domain` immediately followed by `apply_range` at the function head. Legality now reduces to "is source-time strictly-before dest-time on every edge?"

### Algorithm

```c
// internal_check_valid_schedule @ 0x3f080  (isl argument shapes [INFERRED])
Optional<DependenceViolation> internal_check_valid_schedule(self, dependences, schedule, debug_info):
    dsr = dependences.apply_domain(schedule).apply_range(schedule);   // time x time
    for m in dsr.get_map_list():                  // per basic dependence (n_map of them)
        sp     = m.get_space();
        // strict lexicographic happens-before in the TIME tuple:
        before = isl.Map.universe(sp).map_from_set(...)               // build before-relation
                    .intersect( <identity(time) lex< identity(time)> );
        id_mpa = isl.MultiPwAff.identity_on_domain_space(sp.domain());
        before = id_mpa.lex_lt_at_multi_pw_aff(<range identity>);     // { t -> t' : t <lex t' }
        bad    = m.intersect_domain(before.domain())
                  .intersect_range(before.range());                  // edges NOT strictly-before
        if not bad.is_empty():
            src = m.get_tuple_name(dim_type.in);                     // "in"  tuple
            dst = m.get_tuple_name(dim_type.out);                    // "out" tuple
            return DependenceViolation(src, dst, ...);               // ILLEGAL: edge runs backward
        if debug_info != DebugLevel.NONE:
            _ = m.deltas();                                          // distance vector, report only
    return None;  // every edge forward in time -> LEGAL
```

The decompiled body of `0x3f080` carries, in order: `apply_domain`, `apply_range`, `get_map_list`, then per-edge `universe`, `deltas`, `universe`, `map_from_set`, `lex_lt_at_multi_pw_aff`, `identity_on_domain_space`, `intersect_domain`, `intersect_range`, `is_empty`, `DependenceViolation` — and the whole block appears **twice** (the `debug_info`-gated path and the main path), the second carrying the `get_tuple_name`×2 recovery of `in`/`out` tuple names. Both perform the identical lex-lt emptiness test.

### The legality criterion

A schedule is **legal iff** for every dependence edge the source instance is scheduled strictly before the destination — i.e. the set of edges that are *not* strictly-ordered-forward is **empty**. This is the textbook "every dependence is satisfied (lex-positive in time)" test, performed directly with `lex_lt_at_multi_pw_aff` on the double-schedule relation, with no ISL scheduler involved.

> **NOTE —** `deltas()` (the dependence distance vector) is consumed **only** for the debug distance report; it does not influence the verdict. A reimplementer can omit it entirely without changing legality results.

### The `singleton` closure

```c
// internal_check_valid_schedule.<locals>.singleton @ 0x1d7a0  (closure body)
isl.Set singleton(us):                         // us : UnionSet expected to hold ONE space
    s = isl.Set.from_union_set(us);
    if <us not a single space>:  raise "ExpectedSpace";   // literal in .rodata
    return s;
```

A nested helper that coerces a single-space `isl.UnionSet` to a plain `isl.Set` when extracting a violation's offending instances. The `"ExpectedSpace"` string and the `from_union_set` call are present; `0x1e6d0` is its `def`-wrapper slot.

### Function Map

| Function | VA | Role | Confidence |
|---|---|---|---|
| `check_valid_schedule` | `0x273f0` | legality gate; iterates RAW/WAW/WAR | CERTAIN |
| `internal_check_valid_schedule` | `0x3f080` | per-dep lex-lt kernel + double-schedule | HIGH |
| `…singleton` (closure) | `0x1d7a0` | `UnionSet`→`Set` coercion | HIGH |
| `…singleton` (def wrapper) | `0x1e6d0` | closure factory slot | HIGH |
| `DependenceViolation.__init__` | `0x18ce0` | violation record | CERTAIN |

---

## The Violation Record — `DependenceViolation`

`DependenceViolation.__init__(self, src_id, dst_id, tensor, type, msg)` @ `0x18ce0` is a plain record carrying the failing edge's source/dest statement ids, the offending tensor/allocation, the `DependenceType`, and a formatted message. It is the `Optional` return of both `check_valid_schedule` and `internal_check_valid_schedule`; a `None` return means *legal*.

Two enums gate behavior (both `enum.Enum`, recovered from `*_enums.json` and the string pool):

| Enum | Members | Use |
|---|---|---|
| `DependenceType` | `RAW` (flow), `WAR` (anti), `WAW` (output) | default validated set = all three |
| `DebugLevel` | `NONE` (+ levels) | gates the `deltas()` distance dump in the kernel |

---

## The Client — who supplies the schedule

The **sole** caller of `check_valid_schedule` is `SoftwarePipelineCodeGen.SoftwarePipelineCodeGen._reorder_instructions` — that `.so` carries both the `"check_valid_schedule"` and `"NeuronIslDependenceAnalysis"` strings alongside the `_reorder_instructions` qualname. Its vocabulary fixes the "who schedules" answer (all string literals from its pool):

```text
"Applying reordering based on execution_orders: "
"Reorder instructions in the loop body based on execution_order."
"axis: The loop axis containing the instructions to reorder"
"execution_order is either all None or forms consecutive order from 0 to n"
"Mix of None and non-None execution_orders is invalid: "
"_create_software_pipeline_schedule"   "band_get_partial_schedule"
"disable_execution_order_check"        "found first violation in "
```

The software-pipelining pass assigns each instruction an integer `execution_order` (its pipeline stage / position within the axis). `_reorder_instructions` builds the corresponding candidate schedule — a *permutation of the children at that axis*, exactly the SEQUENCE/FILTER ordering of `add_sequence_filter` — then calls `NeuronIslDependenceAnalysis.check_valid_schedule` to verify the reordering violates no RAW/WAR/WAW dependence. A returned `DependenceViolation` ("found first violation in") rejects the reorder; `disable_execution_order_check` bypasses the gate entirely.

> **QUIRK —** `compute_schedule` and `ScheduleConstraints` are absent from the **client** too. The scheduling decision is a heuristic instruction reorder computed from stage annotations, not a polyhedral optimize. This class is its legality oracle and nothing more — the architectural confirmation of [The No-Scheduler Verdict](#the-no-scheduler-verdict).

---

## Worked Example

```text
Penguin IR:  for i in 0..10 { S0 };   for i in 0..10 { for j in 0..20 { S1 } };   for i in 0..10 { S2 }

BUILD (get_schedule_tree -> helper):
  DOMAIN = { s0[i]:0<=i<=10 } u { s1[i,j]:0<=i<10 ^ 0<=j<20 } u { s2[i]:0<=i<=10 }
  isl.ScheduleNode.from_domain(DOMAIN).child(0)
  helper(region) -> add_sequence_filter(root, [s0, s1-nest, s2]):
      insert_sequence( UnionSetList[ {s0..}, {s1..}, {s2..} ] )        # 3 sibling FILTERs
      child(0): helper(s0)      -> add_band(var=i)  -> "{ s0[i]->[(i)] }"
      child(1): helper(s1-nest) -> add_band(var=i) (outer);
                s1.has_ap_indices => insert_mark + add_band(var=j) -> "[{ s1[i,j]->[(j)] }]"
      child(2): helper(s2)      -> add_band(var=i)  -> "{ s2[i]->[(i)] }"
  .get_schedule()  =>  exactly the docstring tree.

VALIDATE a candidate reorder (check_valid_schedule):
  reads, writes = get_dependency_map(insts)
  sched  = candidate.get_map()                          # e.g. swap S1/S2 order at the top axis
  order  = sched.lex_le_union_map(sched)
  RAW: pairs = writes.apply_range(reads.reverse()).intersect(order)
       internal_check_valid_schedule(pairs, sched):
         dsr = pairs.apply_domain(sched).apply_range(sched)   # time x time edges
         per edge: identity lex_lt identity?  -> bad = NOT-forward edges; is_empty?
         non-empty => DependenceViolation("RAW violation on tensor: A", s_src, s_dst)
  (WAW, WAR analogous.)  All empty => return None => reorder accepted.
```

---

## ISL Surface Used (the glue → islpy call inventory)

Every ISL call this glue makes is stock schedule-tree construction or set algebra. islpy ~2023.1 internals are **not** reversed; only the *which calls in what order* is.

```text
SCHEDULE-TREE  from_domain · child · parent · domain · insert_sequence ·
               insert_partial_schedule · insert_mark · get_schedule · Schedule.get_map ·
               UnionSetList.alloc/add · UnionSet.from_set/union · Set.from_union_set
BAND           MultiAff.zero/set_aff · Aff.var_on_domain · LocalSpace.from_space ·
               MultiUnionPwAff.from_multi_aff/union_add · map_from_domain_and_range · Space(ctx-alloc)
LEGALITY       UnionMap.apply_domain/apply_range/reverse/intersect/lex_le_union_map/
               get_map_list/n_map · Map.intersect_domain/intersect_range/is_empty/get_space/
               get_tuple_name/deltas/universe/map_from_set ·
               MultiPwAff.identity_on_domain_space/lex_lt_at_multi_pw_aff · Set.universe
```

None is a scheduler-search call. `Schedule.compute_schedule`, `ScheduleConstraints.{set_validity,set_proximity,set_coincidence,compute_schedule}`, `UnionAccessInfo`, and `UnionFlow.compute_flow` are all **absent** from the binary.

---

## Evidence summary

Where the page's principal claims come from:

- **No scheduler exists.** Each of `compute_schedule`, `ScheduleConstraints`, `set_proximity`, `set_coincidence`, `compute_flow` was grepped individually against this `.so`'s string table (0 hits each) and against the `*TongaIsl*` / `*Simplifier*` / `SoftwarePipelineCodeGen` string tables (no file matched).
- **Legality = double-schedule + lex-lt emptiness.** The body of `0x3f080` runs `apply_domain → apply_range → get_map_list → … lex_lt_at_multi_pw_aff → identity_on_domain_space → intersect_domain → intersect_range → is_empty → DependenceViolation`, twice.
- **Three identical RAW/WAW/WAR blocks over a `lex_le` order.** The body of `0x273f0` runs `…lex_le_union_map`, then 3× `DependenceType → apply_range → reverse → intersect → internal_check_valid_schedule`.
- **`add_band`'s coordinate is the existing IV.** The body of `0x24bc0` runs `var_on_domain` + `set_aff(0,·)` — a single 1-D affine over the chosen set-dim — `union_add`-folded into `insert_partial_schedule`. No coefficient enumeration, no cost call.
- **The client decides, this class validates.** `SoftwarePipelineCodeGen` carries `_reorder_instructions`, `execution_order`, `"Applying reordering based on execution_orders:"`, `disable_execution_order_check`, and references both `check_valid_schedule` and `NeuronIslDependenceAnalysis`; it carries no scheduler symbols of its own.

## Limits of this reading

- The exact islpy argument tuples inside `add_band` and `internal_check_valid_schedule` are **[INFERRED]** from islpy 2023.1 semantics: the `getattr` name is present in each body, but the argument tuple was not individually traced.
- The cited VAs are `__pyx_pw_` public wrappers with the method body inlined, not separately-named `__pyx_pf_` body slots (see the opening NOTE); a symbol table from a different Cython build may number them differently.
- islpy itself is treated as a black box. Only the sequence of calls into it is recovered, not what ISL does with them.

---

## Related Components

| Name | Relationship |
|---|---|
| `SoftwarePipelineCodeGen._reorder_instructions` | **sole client**; supplies the candidate schedule from `execution_order` stage annotations |
| `NeuronIslDependenceAnalysis` (dependence half) | builds the `get_dependency_map` / access / domain inputs this gate consumes |
| `IslSimplifier` (`TongaIslSimplifier.so`) | backs `try_simplify` (coalesce/gist) on the schedule and pair maps |
| stock islpy ~2023.1 | the entire ISL tree + set algebra; not reversed |

## Cross-References

- [Software Pipelining](software-pipelining.md) — the SOLE client; computes the `execution_order` reorder this page validates
- [ISL Dependence Graph](isl-dependence-graph.md) — the dependence/domain/access construction (`get_dependency_map`, `get_child_domain_union_set` body) feeding `check_valid_schedule`
- [Loop-Transform Clients](loop-transform-clients.md) — the loop transforms that do **not** use ISL at all, bounding this component's reach
