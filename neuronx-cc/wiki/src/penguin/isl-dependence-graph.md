# Penguin ISL Dependence-Graph Construction

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, cp310 wheel, module `neuronxcc/starfish/penguin/targets/transforms/experimental/TongaIslDependenceAnalysis.cpython-310-x86_64-linux-gnu.so` (Cython, unstripped, docstrings intact; md5 `a286f6ee6d109d7a23060c0ba4995100`). cp311/cp312 ship the same class at different VAs.*

## Abstract

`NeuronIslDependenceAnalysis` is Penguin's bridge from backend instruction IR into the [ISL](https://libisl.sourceforge.io/) (Integer Set Library) polyhedral model. Its class docstring states the job exactly: *"NeuronISLDependenceAnalysis - Dependence analysis interface using ISL for TongaISAInst"* (`.rodata`, prefixed with a `Copyright (c) 2021, Amazon.com` banner). It translates a Penguin loop-nest — `NeuronInst` statements wrapped in `ScopeRegion`/`Axis` loop scopes, each touching tensors through `NeuronAP` access patterns — into three isl objects: a per-statement **iteration domain** (`isl.UnionSet`), a per-access **access relation** (`isl.UnionMap`), and a hand-built **RAW/WAR/WAW dependence** relation that a candidate schedule is checked against.

The single most important fact about this module is what it is *not*. The integer-set algebra, the schedule/AST machinery, and the dependence *primitives* are stock **islpy ~= 2023.1**, a pip dependency — they are not Neuron-authored. Every isl call you will see (`apply_range`, `lex_le_union_map`, `lex_lt_at_multi_pw_aff`, `from_set`, `deltas`, `is_empty`) is an upstream islpy method invoked inline. What Neuron wrote — and what this page reverses — is the **glue**: how Penguin IR drives those primitives. A reimplementer who has islpy needs only this glue. The page is organized as that glue: §domain (loop-nest → `UnionSet`), §access (`NeuronAP` → `UnionMap`, including `build_aff`), §dependence (read/write unions → RAW/WAR/WAW), and §schedule-tree feed. This page opens the Part-5 ISL chapter (5.16–5.22); the schedule-tree *legality policy* is owned by [Schedule-Tree Legality](isl-schedule-legality.md) (5.17), which consumes the dependence relation built here.

> **QUIRK —** the dependence test is **hand-rolled May-dependence**, not isl exact dataflow. There is **no** `compute_flow` / `union_access_info` symbol anywhere in either `.so`. Instead the glue forms access-pair products (`apply_range(reverse())`) and intersects them with the schedule's happens-before relation (`lex_le_union_map`). A reimplementer who reaches for `isl_union_access_info_compute_flow` will produce a *different* (exact-dataflow) result and a *more permissive* legality gate than what ships. See [§Dependence Relation](#dependence-relation-readwrite-unions--rawwarwaw).

For reimplementation, the contract is:

- **The data model:** one `NeuronInst` ↔ one isl statement tuple `sN` (`N = inst.id`); its iteration domain is `{ sN[ivs] : bounds }` over the enclosing loop induction variables; an allocation-remapped tensor is named `"%s_alloc"`.
- **`build_aff`:** the `AffineExpr → isl.Aff/PwAff` recursive translator and its `AffExprKind` dispatch (Sum/Mult/FloorDiv/Modulo/CCDiv/CCMod) — the one piece of genuine arithmetic glue.
- **The dependence construction:** read/write `UnionMap`s, the `apply_range(reverse) ∩ lex_le` access-pair products, and the `lex_lt_at_multi_pw_aff` lex-positive emptiness test that classifies a schedule legal.
- **The MRO split:** which methods are this class's overrides vs the generic `IntegerSetAnalysis` base (a separate `.so`) vs stock islpy.

| | |
|---|---|
| **Class** | `NeuronIslDependenceAnalysis(IntegerSetAnalysis)` |
| **Module** | `...transforms.experimental.TongaIslDependenceAnalysis` (this `.so`) |
| **Base class** | `IntegerSetAnalysis` + `IntegerSetWrapper` (module `neuronxcc.starfish.penguin.IntegerSetAnalysis`, separate `.so`) |
| **ISL provider** | stock **islpy ~= 2023.1** (pip dep) — all integer-set/schedule/dep primitives |
| **Dependence entry** | `get_dependency_map` @ `0x1b8f0` → `(reads, writes)` `Tuple[isl.UnionMap, isl.UnionMap]` |
| **Legality gate** | `check_valid_schedule` @ `0x273f0` → `Optional[DependenceViolation]` (policy: 5.17) |
| **IR consumed** | `NeuronInst`, `NeuronAP`, `ScopeRegion`, `Axis`, `AffineExpr` (= `CExpr`) |
| **Dataflow mode** | hand-rolled **May-dependence** — no `compute_flow`/`union_access_info` |

---

## Architecture — Two-Module Inheritance

`NeuronIslDependenceAnalysis` is not self-contained. It is the `TongaISAInst` specialization of a generic polyhedral analysis that lives in a **separate** `.so`, and the raw isl-object factory is a third class.

```text
neuronxcc.starfish.penguin.IntegerSetAnalysis      (IntegerSetAnalysis.so — base, NOT this module)
  class IntegerSetWrapper        ── RAII lifetime mgr around isl objects; ALL raw isl
                                    Set/Map/Aff construction (create_domain, create_access_map_impl,
                                    build_aff, add_loop_bounds, create_relation_space, try_simplify)
       docstring: "IntegerSetAnalysis - Manage the lifetime of the isl objects created from ..."
  class IntegerSetAnalysis        ── generic polyhedral analysis: domain / predicated_domain /
                                    tensor_space / create_access / create_domain_space /
                                    is_injective / cover_whole_tensor_impl / _make_access_cache_key
                                    over IR: AffineExpr, AffineIdx, AffineLoad/Store, AffinePredicate

...experimental.TongaIslDependenceAnalysis         (THIS .so — subclass)
  class NeuronIslDependenceAnalysis(IntegerSetAnalysis)
    OVERRIDES (TongaISAInst specialization):
       access / access_impl / build_new_access_impl / in_domain /
       in_predicate_domain / build_aff / affine_exp
    ADDS (the dependence + schedule-tree machinery — this page's surface):
       get_child_domain_union_set, get_dependency_map, get_alloc_remapping,
       get_schedule_tree(+_helper), add_band, add_sequence_filter,
       check_valid_schedule, internal_check_valid_schedule, DependenceViolation,
       enums DependenceType / DebugLevel
```

The practical consequence of the MRO: when this module calls `self.create_access` / `self.create_domain_space` / `self.domain`, the lookup resolves to the **base** `IntegerSetAnalysis` unless overridden here. Three name-classes interleave at every call site:

| Method | Resolves to | Confidence |
|---|---|---|
| `create_domain_space`, `create_access`, `domain`, `predicated_domain`, `tensor_space` | base `IntegerSetAnalysis` | STRONG (base symbol set) |
| `access`, `access_impl`, `build_new_access_impl`, `in_domain`, `in_predicate_domain`, `build_aff`, `affine_exp` | overridden HERE (Pyx symbols present in this `.so`) | CONFIRMED |
| `apply_range`, `apply_domain`, `reverse`, `from_set`, `lex_le_union_map`, `lex_lt_at_multi_pw_aff` | stock **islpy** UnionMap/Set/MultiPwAff methods, inline | CONFIRMED (string table) |

Other imports observed in `pymod_exec`: `neuronxcc.starfish.penguin.common`, `.IslSimplifier` (`NeuronIslSimplifier`), `.targets.tonga.TongaInst`, and the sibling `.targets.transforms.TongaIslSimplifier` (used by `try_simplify` to coalesce/gist results).

> **NOTE —** the names `double_schedule_relation`, `scheduled_raw_deps`, `scheduled_war_deps`, `scheduled_waw_deps`, `raw_dep`, `war_dep`, `waw_dep` appear ONLY in the Cython string-init table (`__Pyx_CreateStringTabAndInitStrings` @ `0x5dfc`), never as a `getattr` in any traced body. Cython folds source-level *local variable names* into the global string table. They are the local names of the schedule-relation and per-type dependence sub-maps inside `check_valid_schedule` / `internal_check_valid_schedule` — they index the §Dependence logic, they are not isl primitives (`isl.*.double_schedule_relation` does not exist in islpy).

---

## Iteration Domain — Loop-Nest → isl.UnionSet

### Purpose

The iteration domain of a statement is the set of loop-index tuples for which the instruction executes: `{ sN[ivs] : bounds }`. The full kernel domain is the **union** of all per-statement Sets, built by a recursive walk of the Penguin loop-nest tree.

### Data model

The `get_schedule_tree` docstring (verbatim from `.rodata`) fixes the entire model with a worked example:

```text
Simplified example
 for i in 0..10
    S0
    for j in 0..20 :
      S1
    S2

The corresponding schedule tree:        (printable with print(root.to_str()))
  schedule: "{ s0[i] -> [(i)]; s1[i, j] -> [(i)]; s2[i] -> [(i)]}
    sequence:
    - filter: "{ s0[i] : 0 <= i <= 10 }"          child:  mark: "s0"
    - filter: "{ s1[i, j] : 0 <= i < 10 and 0 <= j < 20 }"
        child: schedule: "[{ s1[i, j] -> [(j)] }]"  child: mark: "s1"
    - filter: "{ s2[i] : 0 <= i <= 10 }"          child:  mark: "s2"
```

From this:

- One Penguin statement (instruction) `sN` ↔ one isl statement tuple `"sN"`, where `N = inst.id` (format string `"s%s"` in `.rodata`).
- The iteration domain of `sN` is `{ sN[i,j,...] : bounds }`; the index vars are the enclosing loop induction variables (`loopnest_ivs`), and the bounds come from each loop's trip count plus any predicates.
- The schedule maps `sN[ivs] -> [(loop-coords)]`; a deeper loop (`s1` over `i,j`) gets a nested band `"[{ s1[i,j] -> [(j)] }]"`.
- The tree is `DOMAIN(union of all sN sets) → SEQUENCE → per-stmt FILTER → BAND → MARK("sN")`. (The `mark` leaf names each statement — built via `insert_mark`/`get_tuple_id` in §Schedule-Tree.)

### Algorithm

`in_domain` (@ `0x182c0`, generator @ `0x38450`) and `in_predicate_domain` (@ `0x1ad20`, generator @ `0x367c0`) are thin **cached** wrappers around the base-class domain builders. Their generator bodies reference only `domain_cache.pop(...)`, `domain_space_cache.pop(...)`, `AttrRAII`, and (for the predicate variant) `ignore_ap`. The `<lambda>` closures are the cache-miss fillers that call into `IntegerSetAnalysis.domain` / `predicated_domain`.

```c
function get_child_domain_union_set(self, stmt):       // 0x33a70 — recursive IR walk → UnionSet
    cache = self.domain_union_cache                    // memoized per subtree
    if isinstance(stmt, NeuronInst):
        d  = self.in_predicate_domain(stmt)            // isl.Set { sN[ivs] : bounds } ∩ predicates
        d  = d.set_tuple_name("s%s" % stmt.id)         // statement id → tuple name
        us = isl.UnionSet.from_set(d)                  // stock islpy: Set → UnionSet
        return acc.union(us)
    elif isinstance(stmt, (ScopeRegion, Axis)):        // a loop scope
        with stmt:                                     // __enter__/__exit__ push loop level
            for child in stmt.instructions:
                acc = acc.union(get_child_domain_union_set(child))   // recurse
        return acc
    else:
        assert False, "Unhandled IR class in NeuronISLDependenceAnalysis"   // .rodata literal
```

The `ScopeRegion`/`Axis` `__enter__`/`__exit__` manage `loopdepth` so a child `NeuronInst`'s `in_predicate_domain` observes the correct enclosing IVs and bounds. This is the core glue: the nested Penguin loop-nest is flattened into one flat isl `UnionSet` of per-statement Sets. The isl primitives used are stock: `isl.UnionSet.from_set(Set)` and `UnionSet.union(UnionSet)`.

The base-class build (INFERRED from the `IntegerSetWrapper` symbol set) is: `create_domain_space(loopnest, params)` → `isl.Space` (set space, tuple `sN`, dims = #IVs, params = symbolic loop-bound params); then `add_loop_bounds` / `add_partial_loop_bounds` add `0 <= iv < trip` constraints per loop; `predicated_domain = domain ∩ enumerate_predicates(...)`. Symbolic bound params are named/bounded by `add_param_bounds` / `set_param_dim_name`.

---

## Access Relation — NeuronAP → isl.UnionMap

### Purpose

An access maps each statement instance to the tensor elements it touches:

```text
{ sN[i0,i1,i2] -> A[o0,o1,o2] : o0 = AffineExpr(i,..), o1 = AffineExpr(i,..), ... }
```

The base `create_access` docstring states this verbatim: *"s[i0, i1, i2] --> A[o0, o1, o2] where o0 = AffineExpr(i0,i1,i2) and o1 = AffineExpr(...) (i.e. array index expression)"*.

### Algorithm

`access` / `access_impl` / `build_new_access_impl` form a three-layer chain: predicate restriction, memoization, and the actual map build.

```c
function access(self, inst, ap, use_inst):             // 0x39f70 — entry
    dom = self.in_predicate_domain(inst or use_inst)   // predicate-restricted iteration domain
    rel = self.access_impl(inst, ap)                   // raw access map (intersected w/ dom inside)
    return rel

function access_impl(self, inst, ap):                  // 0x21180 — pure memoization
    key = (inst, ap)
    return self.relation_cache.setdefault(key,
               self.build_new_access_impl(inst, ap))

function build_new_access_impl(self, inst, ap):        // 0x31ce0 — block/bank-aware builder
    preds  = self.drop_approx_predicates(inst.predicates)   // exact predicates only
    tensor = inst.tensor ; sid = "s%s" % inst.id
    addrs  = self.get_scaled_addrs(ap)                 // list of AffineExpr address dims
    return self.create_access(sid, tensor_tuple_name(tensor), addrs, preds, ...)   // base class
```

The base `IntegerSetAnalysis.create_access` (INFERRED from its isl-API symbol set) assembles the map:

```c
function create_access(self, sid, tname, addrs, preds, ...):   // base IntegerSetAnalysis
    space = create_relation_space(domain_space(sid), tensor_space(tname))  // map space sN -> A
    affs  = [ build_aff(a) for a in addrs ]            // AffineExpr → isl.Aff/PwAff (one per tensor dim)
    mpa   = isl.MultiPwAff.from_pw_aff_list(affs)
    m     = isl.Map.from_multi_pw_aff(mpa)             // sN[ivs] -> A[affs]
    m     = m.intersect_domain(predicated_domain)      // restrict to valid iteration instances
    m     = m.set_tuple_name(in=sid, out=tname)
    is_injective(m)                                    // checked
    // when addrs unknown: cover_whole_tensor_impl → conservative full_tensor_range
    return try_simplify(m)                             // coalesce/gist
```

Result caching is keyed by `_make_access_cache_key(tensor_shape, loopnest, addrs, normalized_predicates)` — its docstring: *"Create access cache key from tensor shape, loopnest, addrs and normalized predicates."* — toggled by `enable/disable_create_access_caching`.

### build_aff — AffineExpr → isl.Aff/PwAff

This is the one piece of genuine arithmetic glue in the module: a recursive descent that turns a Penguin `AffineExpr` (an alias of pelican `CExpr`) into an isl affine expression on a given domain space.

```c
function build_aff(self, expr, space=None, loopnest=None, params=None):   // 0x4c790
    space = space or self.create_domain_space(loopnest, params)
    return build(expr)                                 // nested recursion, body @ 0x44ee0

function build(expr):                                  // 0x44ee0 — dispatch on expr.kind (AffExprKind)
    if is_constant(expr):                              // CExpr leaf constant
        return isl.Aff.val_on_domain(space, Val(c))
    switch expr.kind:                                  // AffExprKind enum member
      case SumKind:                                    // fold terms
        acc = zero ; for (term, coef) in expr.terms:
            acc = acc.add(build(term) * coef)          //  + scalar via val_on_domain
        return acc
      case MultKind:                                   // build(sub) scaled
        return build(sub).scale(val_on_domain(expr.scale))
      case FloorDivKind:                               // numer / denom, floor
        return build(expr.numer).scale_down_val(expr.denom)
      case ModuloKind:                                 // numer mod denom
        return build(expr.numer).mod_val(expr.denom)
      case CCDivKind:                                  // ceiling div   (scale_down_val variant)
      case CCModKind:                                  // ceiling mod   (mod_val variant)
    // variable / AffineIdx leaf:
    idx = find(expr, loopnest_ivs) ?? find(expr, params)
    if idx in loop IVs:   return var_on_domain(LocalSpace(space), dim_type.set,   k) * coef
    elif idx in params:   return var_on_domain(LocalSpace(space), dim_type.param, k) * coef
    else: raise "%s doesn't appear in params or loopnest" % expr   // .rodata literal
```

The dispatch is confirmed by the string table: `kind`, `CExpr`, `AffExprKind`, `SumKind`, `MultKind`, `FloorDivKind`, `ModuloKind`, `CCDivKind`, `CCModKind`, `val_on_domain`, `var_on_domain`, `terms`, `numer`, `denom`, `scale_down_val`, `mod_val`, `dim_type`, and the literal *" doesn't appear in params or loopnest"* are all present as `.value` strings.

`affine_exp` (generator @ `0x4dee0`, genexpr @ `0x208a0`) is a sibling iterator that emits per-tensor-dim `isl.PwAff` terms over a `LocalSpace` (`isl.LocalSpace.from_space`), using `var_on_domain` / `zero_on_domain` / `scale_val` / `mod_val` / `int_from_si` / `Val` / `shape`. It is consumed by `get_alloc_remapping`'s address affs (see §Dependence).

> **GOTCHA —** `CCDivKind`/`CCModKind` are *ceiling* div/mod, distinct from `FloorDivKind`/`ModuloKind` (floor). A reimplementation that maps all four onto isl's floor-semantics `scale_down_val`/`mod_val` will silently mis-round ceiling expressions. The kind distinguishes the rounding direction; the isl primitive does not.

---

## Dependence Relation — read/write unions → RAW/WAR/WAW

### Purpose

The dependence relation is derived in two steps: `get_dependency_map` builds the read- and write-access `UnionMap`s, then `check_valid_schedule` combines them pairwise and orders them by a candidate schedule to classify flow/anti/output dependences and verify the schedule respects them.

### DependenceType / DebugLevel enums

```c
from enum import Enum
class DependenceType(Enum):
    RAW   # flow   (Read-After-Write)    value 0   (INFERRED ordinal — ints 0/1/2 created)
    WAR   # anti   (Write-After-Read)    value 1
    WAW   # output (Write-After-Write)   value 2
class DebugLevel(Enum): NONE [, ...]     # gates debug_info printing
```

`DependenceType` is a Python `enum.Enum` subclass built in `pymod_exec` (the strings `DependenceType`, `DebugLevel`, `RAW`/`WAR`/`WAW` appear in the table; `RAW`/`WAR`/`WAW` are also embedded in the `"RAW violation on tensor: "` / `"WAR …"` / `"WAW …"` diagnostics). Default `dependence_types` arg to `check_valid_schedule` is `{RAW, WAR, WAW}` (all three). The member names are RAW/WAR/WAW — there are no `flow`/`anti`/`output` string members.

> **NOTE —** the `0/1/2` ordinals are INFERRED (an `enum.Enum` body with three bare members yields auto-values, but the exact integers are not directly observable in the string table — only the names are). Treat ordinal identity as low-confidence; the *names and their flow/anti/output meaning* are CONFIRMED via the diagnostics.

### Step 1 — get_dependency_map

```c
function get_dependency_map(self, insts=None) -> (isl.UnionMap, isl.UnionMap):   // 0x1b8f0
    read_union  = empty UnionMap ; write_union = empty UnionMap
    for inst in (insts or self.insts):
        for ap in inst.operands:                       // input access patterns
            if isinstance(ap, NeuronAP):
                r = self.access(inst, ap)              // sN[ivs] -> tensor[affs]
                r = r.apply_range(self.get_alloc_remapping(inst, ap))   // → allocation tensor
                read_union = read_union.union(r.to_union_map())
        dst = inst.dst                                 // output access pattern
        if isinstance(dst, NeuronAP):
            w = self.access(inst, dst)
            w = w.apply_range(self.get_alloc_remapping(inst, dst))
            write_union = write_union.union(w.to_union_map())
    return (read_union, write_union)                   // annotation: "Tuple[isl.UnionMap, isl.UnionMap]"
```

The `apply_range(get_alloc_remapping(...))` step is the tensor→physical-allocation rename: two access patterns that alias the *same* SBUF/PSUM allocation are renamed into the *same* tensor space so the dependence test can see they conflict. Without it, `A` and `B` that overlap in SBUF would look disjoint.

### Step 1b — get_alloc_remapping

```c
function get_alloc_remapping(self, inst, ap):          // 0x2c870
    if ap.isInputOrOutput():
        return identity                                // DRAM I/O tensors: no remap
    addrs = self.access_ap_indices(ap)                 // physical index affs
    affs  = [ self.affine_exp(a, allocated_shape, allocated_block_shape,
                              allocated_bank_shape) for a in addrs ]
    mpa   = isl.MultiPwAff.from_pw_aff_list(isl.PwAffList(...))
    m     = isl.Map.from_multi_pw_aff(mpa)             // { A[idx] -> A_alloc[scaled idx] }
    return m.set_tuple_name(out = "%s_alloc" % tensor)
```

`affine_exp` projects the AP indices through `allocated_shape` / `allocated_block_shape` / `allocated_bank_shape` (the block/bank tiling of the allocation), producing the `"%s_alloc"`-tupled physical-address map. Input/output (DRAM) tensors are passed through unremapped.

### Step 2 — check_valid_schedule (the legality gate; policy in 5.17)

This is the gate that *consumes* the dependence relation. The RAW/WAR/WAW *construction* below is the D-Y01 dependence relation and is documented here; the gate *policy* (when it runs, what it does with a violation) is owned by [Schedule-Tree Legality](isl-schedule-legality.md).

```c
function check_valid_schedule(self, new_schedule, insts,
                              dependence_types={RAW,WAR,WAW}):   // 0x273f0
    reads, writes = self.get_dependency_map(insts)
    sched = (new_schedule or self.get_schedule_tree(...)).get_map()   // UnionMap sN[ivs]->[coords]
    sched = self.try_simplify(sched)
    order = sched.lex_le_union_map(sched)              // schedule happens-before:  src scheduled <= dst

    for dt in dependence_types:
        if   dt == RAW:  srcs, dsts = writes, reads    // flow:   write → later read
        elif dt == WAW:  srcs, dsts = writes, writes   // output: write → later write
        elif dt == WAR:  srcs, dsts = reads,  writes   // anti:   read  → later write
        // pair accesses to the SAME (allocation) tensor, keep schedule-ordered pairs:
        pairs = srcs.apply_range( dsts.reverse() )     // { src_inst -> dst_inst : same tensor }
        pairs = pairs.intersect(order)                 // keep src-before-dst per schedule
        pairs = self.try_simplify(pairs)
        v = self.internal_check_valid_schedule(pairs, dt)
        if v is not None:
            return DependenceViolation(src_id, dst_id, tensor, dt, msg)
    return None
```

The pairing `srcs.apply_range(dsts.reverse())` is the access-pair product: `dsts.reverse()` is `{ tensor[elem] -> dst_inst }`, so `apply_range` composes `{ src_inst -> tensor[elem] } ∘ { tensor[elem] -> dst_inst }` = `{ src_inst -> dst_inst }` for every pair touching the same allocation element. Intersecting with `order` (the `lex_le_union_map` happens-before relation) keeps only pairs the candidate schedule orders src-before-dst.

### Step 3 — internal_check_valid_schedule (lex-positive emptiness test)

```c
function internal_check_valid_schedule(self, dependences, dt):   // 0x3f080
    for dep in dependences:                            // per dependence map
        sd = dep.apply_domain(sched).apply_range(sched)   // re-express dep in schedule(time) space
        for m in sd.get_map_list():                    // per basic relation
            sp  = m.get_space()
            rng = isl.Set.universe(sp.range())
            lt  = isl.MultiPwAff.identity_on_domain_space(...)
                     .lex_lt_at_multi_pw_aff(...)       // strict-before in time
            bad = m.intersect_domain(...).intersect_range(...)   // violating instances
            if not bad.is_empty():
                return DependenceViolation(...)         // carried against schedule → illegal
    deltas = dep.deltas()                              // dependence-distance set (distance/dump)
    // else satisfied
```

The legality criterion is: a dependence is respected iff, after expressing it in schedule (time × time) space via `apply_domain(sched).apply_range(sched)`, every source-to-target pair is *strictly* forward in time. The `lex_lt_at_multi_pw_aff` against `identity_on_domain_space` is the "delta is lex-positive" test; the `bad` set is the violating instances, and a non-empty `bad` is a violation. `deltas()` extracts the dependence-distance set for distance reporting / dumps.

> **QUIRK —** there is **no** call to `isl.union_access_info` / `compute_flow` anywhere in either `.so` (verified by absence across strings, decompiled bodies, and the symbol tables). The glue computes deps *directly* as access-pair products restricted by the schedule order (`lex_le_union_map` / `lex_lt_at_multi_pw_aff`), not via isl exact-dataflow. This is **May-dependence**: every pair of accesses touching the same allocation that the schedule orders is treated as a dependence, with legality = "no such pair is ordered the wrong way." Exact dataflow (which would kill same-write-overwritten reads) is *not* performed — the gate is intentionally conservative.

### DependenceViolation

`DependenceViolation.__init__` @ `0x18ce0` is the record carrying `(src_id, dst_id, tensor, type, msg)` for the failing dependence. It is the `Optional[DependenceViolation]` returned by the `check_*` methods and is rendered with the `"RAW violation on tensor: "` / `"WAR …"` / `"WAW …"` diagnostic strings.

---

## Schedule-Tree Assembly (Feed Only — Legality in 5.17)

`get_schedule_tree` (@ `0x1f600`, the docstring entrypoint) drives `get_schedule_tree_helper` (@ `0x3bbd0`), a recursive builder mirroring `get_child_domain_union_set`'s traversal. It emits an `isl.Schedule(Node)` tree `DOMAIN → SEQUENCE → per-stmt FILTER → BAND → MARK`.

```c
function get_schedule_tree_helper(self, stmt, root):   // 0x3bbd0
    if isinstance(stmt, NeuronInst):
        node = self.add_band(root, stmt)               // if stmt.has_ap_indices: 1-D band over IVs
        // followed by insert_mark(get_tuple_id("s%s")) → the mark: "sN" leaf
    elif isinstance(stmt, (Axis, ScopeRegion)):
        node = self.add_sequence_filter(root, stmt.instructions)
        for child in stmt.instructions:
            get_schedule_tree_helper(child, node)

function add_band(self, root, stmt):                   // 0x24bc0 — insert 1-D schedule band
    dom  = root.domain ; n = dom.get_basic_set_list().n_basic_set
    ma   = isl.MultiAff(zero + set_aff(isl.Aff.var_on_domain(dim_type.set, k)))
    mupa = isl.MultiUnionPwAff.from_multi_aff(ma).union_add(...)
    return root.insert_partial_schedule(mupa)          // the "[{ sN[i,j]->[(j)] }]" band

function add_sequence_filter(self, root, stmts):       // 0x22b20 — SEQUENCE of per-stmt FILTERs
    usl = isl.UnionSetList(ctx)
    for s in stmts: usl = usl.add(get_child_domain_union_set(s))
    return root.insert_sequence(usl)                   // the "- filter: {sN[ivs]:bounds}" siblings
```

This reproduces the docstring tree exactly: `DOMAIN(union sets)` / `SEQUENCE` / three `FILTER`s / a nested `BAND` for `s1`'s inner loop / a `MARK("sN")` leaf per statement. The legality policy that wraps this (running `check_valid_schedule` on the produced tree) is [Schedule-Tree Legality](isl-schedule-legality.md).

---

## Worked Example

Penguin IR: `for i in 0..10: s0; for i in 0..10: for j in 0..20: s1; for i in 0..10: s2`.

```text
DOMAINS (get_child_domain_union_set):
  s0 → { s0[i] : 0 <= i <= 10 }
  s1 → { s1[i, j] : 0 <= i < 10 and 0 <= j < 20 }
  s2 → { s2[i] : 0 <= i <= 10 }
  UnionSet = union of the three  (DOMAIN node)

ACCESS (access → build_new_access_impl → create_access; build_aff per dim):
  s1 writes A:  { s1[i,j] -> A[i, j] }     (o0 = i via SumKind/var_on_domain, o1 = j)
  apply_range(get_alloc_remapping) renames A → A_alloc if A is an SBUF/PSUM allocation

SCHEDULE (get_schedule_tree):
  { s0[i]->[(i)]; s1[i,j]->[(i)]; s2[i]->[(i)] }  + nested band  [{ s1[i,j]->[(j)] }]

DEPENDENCES (get_dependency_map + check_valid_schedule):
  reads, writes : UnionMaps
  order = sched.lex_le_union_map(sched)
  RAW   = writes.apply_range(reads.reverse()).intersect(order)   // write → later read
  internal_check_valid_schedule confirms each RAW/WAR/WAW pair is strictly forward in time
```

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `__init__` | `0x19de0` | construct analysis ctx (caches, params) | CONFIRMED |
| `in_domain` (wrapper / gen) | `0x182c0` / `0x38450` | cached per-stmt iteration domain | CONFIRMED |
| `in_predicate_domain` (wrapper / gen) | `0x1ad20` / `0x367c0` | predicate-restricted domain | CONFIRMED |
| `get_child_domain_union_set` | `0x33a70` | union of subtree domains → `UnionSet` | CONFIRMED |
| `access` | `0x39f70` | AP → access relation entry | CONFIRMED |
| `access_impl` | `0x21180` | memoization layer | CONFIRMED |
| `build_new_access_impl` | `0x31ce0` | block/bank-aware access-map builder | CONFIRMED |
| `build_aff` (+ `.build`) | `0x4c790` (+ `0x44ee0`) | `AffineExpr` → `isl.Aff/PwAff` | CONFIRMED |
| `affine_exp` (gen + genexpr) | `0x4dee0` (+ `0x208a0`) | per-dim `PwAff` term iterator | CONFIRMED |
| `get_dependency_map` | `0x1b8f0` | read/write `UnionMap` builder | CONFIRMED |
| `get_alloc_remapping` | `0x2c870` | tensor → allocation rename map | STRONG |
| `add_band` | `0x24bc0` | insert 1-D schedule band | STRONG |
| `add_sequence_filter` | `0x22b20` | insert SEQUENCE + per-stmt FILTER | STRONG |
| `get_schedule_tree` | `0x1f600` | entrypoint (docstring) | CONFIRMED |
| `get_schedule_tree_helper` | `0x3bbd0` | recursive tree assembler | STRONG |
| `check_valid_schedule` | `0x273f0` | legality gate (policy: 5.17) | CONFIRMED |
| `internal_check_valid_schedule` | `0x3f080` | lex-positive carried-dep test | STRONG |
| `DependenceViolation.__init__` | `0x18ce0` | violation record ctor | CONFIRMED |

Base-class glue (in `IntegerSetAnalysis.so`, not IDA-extracted here — from symbol/docstring evidence):
`IntegerSetAnalysis.{create_access, create_domain_space, domain, predicated_domain, tensor_space, is_injective, cover_whole_tensor_impl, _make_access_cache_key, get_scaled_addrs, enumerate_predicates}` and `IntegerSetWrapper.{create_domain, create_access_map_impl, create_relation_space, build_aff, add_loop_bounds, try_simplify, union_and_simplify, val, ctx}`.

---

## Related Components

| Name | Relationship |
|---|---|
| `IntegerSetAnalysis` / `IntegerSetWrapper` | base class; raw isl-object factory and lifetime mgr |
| islpy ~= 2023.1 | upstream provider of every isl Set/Map/Aff/Schedule primitive |
| `TongaIslSimplifier` / `NeuronIslSimplifier` | sibling coalesce/gist used by `try_simplify` |
| pelican `Expr` / `CExpr` | the `AffineExpr` backing consumed by `build_aff` |

## Cross-References

- [Schedule-Tree Legality](isl-schedule-legality.md) — 5.17, owns the `check_valid_schedule` legality *policy* that consumes this page's dependence relation
- [Affine-Expression Bridge](affine-bridge.md) — 5.21, the `AffineExpr`/`CExpr` substrate that `build_aff` translates
- [Backend Dependence](backend-dependence.md) — 5.22, the downstream consumer of the dependence graph
- [Penguin Front-End Pipeline](../front/pipeline.md) — where Penguin IR (`NeuronInst`/`NeuronAP`/`ScopeRegion`/`Axis`) is produced
