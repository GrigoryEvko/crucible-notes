# IslSimplifier — Predicate / Domain Gist

> *All symbols on this page apply to neuronx_cc `2.24.5133.0+58f8de22` (cp310 wheel). The native `islwrapper::*` code lives in `neuronxcc/starfish/lib/libBIR.so`; the Python driver lives in `neuronxcc/starfish/penguin/transforms/SimplifyPredicates.cpython-310-x86_64-linux-gnu.so`. Other versions/ABIs differ.*

## Abstract

`IslSimplifier` is Penguin's polyhedral guard-and-domain simplifier. Given a predicated loop-nest statement, it pushes the statement's iteration domain and its conjunction of affine guard predicates into the [Integer Set Library (ISL)](https://libisl.sourceforge.io/), runs an ISL **gist** against the loop-nest/parameter context to drop every predicate that the loop bounds already imply, and either re-installs the surviving (smaller) predicate set on the instruction or rewrites the loop bounds outright. It is the pass that turns a runtime mask like `if (0 <= i < 10 && i < 7)` into a tightened loop `for i in 0..7` with no guard at all.

The single most important fact for a reimplementer — and the first correction to the backing analysis — is that **`IslSimplifier` is a native C++ class, `islwrapper::IslSimplifier`, compiled into `libBIR.so`**, not a Cython module. Its five public methods carry fully demangled, type-bearing symbols (`simplify_predicate(bir::Instruction*)`, `gist_convex_hull(bir::Instruction*)`, `predicates_over_loopnest(bir::Instruction*, isl::set)`, `predicates_over_loopnest_convex_hull(bir::Instruction*, isl::set, bool)`, `shrink_domain(bir::InstLoop*, isl::set)`). The actual ISL↔Penguin codecs live one class up the hierarchy, in the base `islwrapper::IntegerSetAnalysis` (also native C++ in `libBIR.so`), whose methods `predicated_domain`, `convex_hull`, `apply_predicates`, `enumerate_affine_predicates`, `exract_cst_bounds`, and `simplify` are likewise confirmed by mangled symbol. The Python pass `SimplifyPredicates` is only the driver that instantiates `IslSimplifier` and calls `simplify_predicate` / `shrink_domain` on predicated instructions.

Conceptually this is the ISL-`gist` pattern familiar from Polly and the isl tutorial: *simplify set `S` given that context set `C` holds*. `gist(S, C)` returns a set equal to `S` inside `C` but with all constraints already entailed by `C` stripped. Here `S` is `domain ∩ predicates`, `C` is the convex hull of the loop-nest domain (or, in the shrink path, a rebuilt loop "box"), and what survives the gist is exactly the non-redundant guards. The page covers both paths: the **predicate path** (`gist_convex_hull` → `predicates_over_loopnest` → `predicates_over_loopnest_convex_hull`) and the **domain-shrink path** (`shrink_domain` / `shrink_domain_convex_hull`).

For reimplementation, the contract is:

- The class split: `IslSimplifier` (orchestration + statistics) over `IntegerSetAnalysis` (the AffinePredicate↔`isl::set` codecs and domain construction). The simplifier never calls ISL directly except through the base-class helpers and a handful of stock `isl::set` verbs.
- The two-pass predicate-simplify control flow in `gist_convex_hull`, including the `is_approx` filter and the `None`/empty short-circuits.
- The exact ISL op-set actually invoked, and *where* each runs: `gist` (twice), `convex_hull` (twice), `coalesce` (once), `compute_divs`/`remove_divs` (over-approx only), `is_empty`, `get_basic_sets`, `get_constraints`, `get_space`, `intersect_range`, and `BasicSet.universe`.
- The shrink-path domain narrowing: rebuild a canonical box, gist+coalesce, intersect predicates, re-extract per-axis `lb/ub/stride/tripcount`, and clone the loop nest.

| | |
|---|---|
| **Native class** | `islwrapper::IslSimplifier` (in `libBIR.so`) |
| **Base class** | `islwrapper::IntegerSetAnalysis` (the codecs / domain builders) |
| **Public entry** | `simplify_predicate(bir::Instruction*)` @ thunk `0x17b250` |
| **Predicate worker** | `predicates_over_loopnest_convex_hull(bir::Instruction*, isl::set, bool)` @ thunk `0x181650` |
| **Domain-shrink worker** | `shrink_domain(bir::InstLoop*, isl::set)` @ thunk `0x17b8c0` |
| **Python driver** | `SimplifyPredicates.cpython-310` (instantiates `IslSimplifier`) |
| **Core ISL op** | `isl::set::gist(context)` — redundant-constraint elimination |
| **Statistics** | `eliminated_predicates`, `eliminated_loops`, `eliminated_iterations`, `strided_axes` |

> **CORRECTION (Y04-01) —** the backing analysis frames the subject as a Cython module `…/penguin/IslSimplifier.cpython-310…so` and reconstructs every method from CPython C-API call sequences. No such `.so` exists in the corpus, and the qualname strings `gist_convex_hull` / `eliminated_predicates` appear in *no* `strings.json`. The binary truth is a native C++ class `islwrapper::IslSimplifier` in `libBIR.so` with all five methods present as **demangled, parameter-typed mangled symbols** (verified with `c++filt`). The Cython call-sequence reconstruction is still a faithful description of the *algorithm* — Cython simply emits C-API glue that drives these native methods — so the control flow below is sound, but the "Cython module" provenance is wrong and the native signatures (with `bir::Instruction*` / `isl::set` / `bir::InstLoop*` operands) are the authoritative ones.

---

## The Class Hierarchy and Data Flow

### Purpose

`IslSimplifier` derives from `IntegerSetAnalysis`. The split is the standard "policy over mechanism" division: the base class owns every conversion between Penguin's IR (loop axes, quasi-affine expressions, `AffinePredicate`s) and ISL objects (`isl::set`, `isl::aff`, `isl::constraint`, `isl::local_space`); the derived class owns the *simplification policy* — which gist to run, when to over-approximate, what to do with the result, and the running statistics. A reimplementer should build `IntegerSetAnalysis` first; `IslSimplifier` is a thin strategy on top.

### Base class `islwrapper::IntegerSetAnalysis` — the codecs

Every method below is a confirmed mangled symbol in `libBIR.so` (addresses are the export-thunk addresses in the cp310 build). The bodies themselves are imported (these per-symbol files are 6-byte PLT jumps), so signatures are CONFIRMED but line-level bodies are not individually traced here.

| Method | Signature (demangled) | Role | Confidence |
|---|---|---|---|
| `predicated_domain` | `(bir::Instruction*, isl::set)` @ `0x17e4d0` | forward: build `domain ∩ predicates` as an `isl::set` | CONFIRMED |
| `combine_predicated_domain` | `(bir::Instruction*, isl::set)` @ `0x17e8b0` | merge a predicated domain into an existing set | CONFIRMED |
| `convex_hull` | `(std::vector<bir::LoopAxis*>&, isl::space)` @ `0x17b5a0` | build the **gist context**: convex hull of the loop-nest domain in a space | CONFIRMED |
| `enumerate_affine_predicates` | `(pelican::PelicanContext*, …)` @ `0x17c400` | back: `isl::constraint` list → `[AffinePredicate]` | CONFIRMED |
| `enumerate_predicates` | `(isl::local_space, std::vector<bir::…>&)` @ `0x17c680` | lower-level constraint→predicate enumeration | CONFIRMED |
| `apply_predicates` | `(pelican::PelicanContext*, vector<bir::Instruction*>&, vector<isl::constraint>&, vector<bir::LoopAxis*>, bool)` @ `0x175180` | intersect a box/set with a predicate set (narrowing) | CONFIRMED |
| `exract_cst_bounds` | `(isl::set, int, int, int, int)` @ `0x17a180` | read tightened constant `lb/ub` per axis | CONFIRMED |
| `extract_cst_floor` / `extract_cst_ceil` / `extract_cst_val` | `(isl::pw_aff)` @ `0x17e1a0` / `0x176ab0` / `0x17bf30` | scalar bound extraction from piecewise-affines | CONFIRMED |
| `add_loop_bounds` | `(isl::set, vector<bir::LoopAxis*>&)` @ `0x17a050`; `(isl::basic_set, vector<Bound*>&)` @ `0x175630` | inject IV bounds into a (basic) set | CONFIRMED |
| `intersect_bound` | `(isl::set, int,int,int,int, vector<bir::LoopAxis*>&)` @ `0x17ee50` | clip a set on one axis | CONFIRMED |
| `build_aff` / `build_linear_expr` / `quasi_affine_expr` | over `bir::QuasiAffineExpr` + `isl::aff` | Penguin expr → `isl::aff` | CONFIRMED |
| `domain` / `domain_space` / `create_domain_space` | `(bir::Instruction*, …)` | build the iteration `isl::set` / its `isl::space` | CONFIRMED |
| `simplify` | `(isl::set)` @ `0x17e0c0` | generic set simplify | CONFIRMED |
| `extract_int` | `(isl::val)` @ `0x17d450` | `isl::val` → C++ int | CONFIRMED |

> **NOTE —** the base-class name is `exract_cst_bounds` — the binary symbol is misspelled (missing the second `t`). The backing analysis writes `extract_cst_bounds`; a reimplementer matching symbols must use the misspelled form. The neighbours `extract_cst_floor`/`extract_cst_ceil`/`extract_cst_val` are spelled correctly, so this is a one-off typo baked into the shipped `libBIR.so`.

> **NOTE —** `pelican::PelicanContext*` threads through `enumerate_affine_predicates` and `apply_predicates`. Pelican is Penguin's compilation-context/codename layer; it resolves IV and SPMD-parameter names when turning an `isl::constraint` back into a named `AffinePredicate`. The Cython-level analysis calls this `cu` (compilation unit) with `cu.spmd_ids`; the native operand is the `PelicanContext`.

### Top-level data flow (predicate simplification)

```text
inst.predicates : [AffinePredicate]   (exact only — is_approx dropped)
      |  IntegerSetAnalysis::predicated_domain(inst, domain)     [forward: AP -> isl::set]
      v
   isl::set  S = domain ∩ predicates
      |  context C = IntegerSetAnalysis::convex_hull(loop_axes, S.get_space())
      v
   S = S.gist(C)                       <<< ISL GIST: drop guards implied by C
      |  [+ S.compute_divs().remove_divs()  if overapproximate]
      v
   bsets = S.get_basic_sets(); if len==1: S = bsets[0]
      |  cons = S.get_constraints()
      v
   [isl::constraint]  --enumerate_affine_predicates(cons, PelicanContext, spmd_ids)-->
      v
   [AffinePredicate]   (or None  if any element infeasible -> empty domain)
      |  inst.resetPredicates(*R) ; inst.addPredicate(*R)
      v
   inst.predicates SMALLER  -> IslSimplifier::eliminated_predicates += dropped
```

---

## simplify_predicate — Public Entry

### Purpose

The thin public driver. It records the predicate count, delegates the entire ISL pipeline to `gist_convex_hull` (which mutates `inst`'s predicate list in place), then bumps the class statistic by the number of guards dropped and returns whether the count shrank — so a caller can iterate to a fixpoint or skip downstream rework.

### Algorithm

```c
bool IslSimplifier::simplify_predicate(bir::Instruction *inst):   // sym @0x17b250
    n = len(inst->predicates());                  // count before
    if (this->gist_convex_hull(inst)):            // delegates the whole ISL pipeline
        // gist_convex_hull mutated inst->predicates() in place
        IslSimplifier::eliminated_predicates += (n - len(inst->predicates()));  // CLASS stat
    return n != len(inst->predicates());          // "did the guard count change?"
```

> **NOTE —** `eliminated_predicates` is a *class-level* (static) accumulator, surfaced by Penguin's `Statistics` subsystem as the human-readable counter "Number of predicate eliminated". It tallies across every instruction in a compile. The companion shrink-path counters (`eliminated_loops`, `eliminated_iterations`, `strided_axes`) are the same shape.

---

## gist_convex_hull — Predicate-Simplify Orchestrator

### Purpose

The orchestrator (not itself an ISL caller). It (a) drops approximate predicates, (b) asks `predicates_over_loopnest` for the simplified predicate set, (c) wipes and re-installs them, then (d) runs a *second* pass against the now-tighter context and adds those. A `None` at either pass is the no-op / empty-domain short-circuit.

### Algorithm

```c
bool IslSimplifier::gist_convex_hull(bir::Instruction *inst):     // sym @0x17c770
    if (!inst->is_predicated()):                  // py line 157: unpredicated -> nothing
        return false;
    // keep EXACT affine predicates only; an is_approx guard is an over/under-
    // approximation, not exact affine, and would poison the gist.
    preds = [p for p in inst->predicates() if !p.is_approx];        // line 161
    domain = this->domain;                                          // line 162  (self.domain)
    R1 = this->predicates_over_loopnest(inst, domain, preds, ...);  // line 164  PASS 1
    if (R1 == None):
        return false;                             // nothing simplified
    inst->resetPredicates(*R1);                   // line 168: clear, re-install simplified set
    R2 = this->predicates_over_loopnest(inst, ...);                // line 169  PASS 2
    if (R2 == None):
        return true;                              // second pass yielded nothing extra
    inst->addPredicate(*R2);                      // line 173: add the delta
    return true;
```

> **QUIRK —** the two-pass structure is "simplify, commit, re-simplify against the now-tighter context, commit the delta." Pass 1's `resetPredicates` makes the instruction's guard set strictly smaller; pass 2 then gists *that* tighter context, which can expose still more redundancy. `R1==None` returns `false` (truly nothing to do); `R2==None` returns `true` (pass 1 already changed something). A reimplementer that runs a single pass will leave easy second-order redundancy on the table.

> **GOTCHA —** the `is_approx` filter is silent and total. Any predicate the front-end flagged as an over- or under-approximation is excluded from the conjunction fed to ISL, so the gist operates only on exact affine constraints and *never rewrites* an approximate guard. If your `AffinePredicate` model lacks an `is_approx` bit, you will feed non-exact constraints into `gist` and produce wrong (unsound) simplifications.

---

## predicates_over_loopnest — Forward-Build Thunk

### Purpose

A two-step thunk: build the `isl::set` for `domain ∩ predicates`, then hand it to the convex-hull worker. The forward conversion lives in the base class (`predicated_domain`); this method just sequences it and short-circuits on an empty build.

### Algorithm

```c
List? IslSimplifier::predicates_over_loopnest(                    // sym @0x17a000
        bir::Instruction *inst, isl::set domain, preds,
        bool overapproximate, bool approximate_predicates):
    pd = this->predicated_domain(domain, preds);   // line 116: base-class forward codec
    if (pd == None):                               //   domain ∩ preds empty/unbuildable
        return None;
    return this->predicates_over_loopnest_convex_hull(            // lines 120-121
               inst, pd, overapproximate, approximate_predicates);
```

The `overapproximate` / `approximate_predicates` flags steer the worker: the first allows a convex over-approximation (the `compute_divs`/`remove_divs` div-elimination, which may weaken a guard but makes it div-free); the second governs whether predicates flagged approximate are retained. `predicated_domain` itself — the AffinePredicate→`isl::set` direction — is `IntegerSetAnalysis::predicated_domain(bir::Instruction*, isl::set)` (`@0x17e4d0`), out of this method's hot path.

---

## predicates_over_loopnest_convex_hull — The ISL Worker

### Purpose

The only method that drives ISL set algebra directly. It builds the gist context (convex hull of the loop-nest domain in the predicate's space), gists the predicated domain against it (the redundant-guard eliminator), optionally over-approximates away existential divs, decomposes to a single convex basic set, extracts the surviving constraints, and converts them back to `AffinePredicate`s.

### Algorithm

```c
List? IslSimplifier::predicates_over_loopnest_convex_hull(       // sym @0x181650
        bir::Instruction *inst, isl::set domain,
        bool overapproximate, bool approximate_predicates):
    ln  = this->loopnest(inst);                       // line 125: loop-nest of this stmt
    sp  = domain.get_space();                         // line 126: gist target space
    ctx = this->convex_hull(sp, ...);                 // line 127: GIST CONTEXT
                                                      //   = convex hull of loop-nest domain
    // --- ISL GIST: drop every constraint of `domain` already implied by `ctx` ---
    domain = this->try([&]{ return domain.gist(ctx); });          // line 129 (lambda4)

    if (overapproximate):                             // line 133
        // OVER-APPROX: materialise existential (floor/mod) divs, then project them
        // out -> a div-free, possibly weaker, affine set.
        domain = this->try([&]{                       // line 134 (lambda5)
            return domain.compute_divs().remove_divs(); });

    bsets = domain.get_basic_sets();                  // line 140: decompose to convex pieces
    if (len(bsets) == 1):                             //   after gist+hull, expect ONE piece
        domain = bsets[0];                            //   take it so get_constraints is 1 conj
    cons = domain.get_constraints();                  // line 141: isl::constraint list

    if (cons):                                        // line 143
        // back-convert: isl::constraint -> Penguin AffinePredicate, named via PelicanContext
        result = enumerate_affine_predicates(         // lines 147-148
                     cons, /*cu=*/this->cu, /*spmd_ids=*/this->cu->spmd_ids, ...);
        result = list(result);
        if (any(p is None for p in result)):          // line 151: a None constraint = infeasible
            return None;                              //   -> empty/unsat domain
        return result;                                //   the simplified AffinePredicate list
    return None;                                      // cons empty: nothing to simplify
```

### The ISL operations, and exactly where each runs

| ISL op | Site | Purpose | Confidence |
|---|---|---|---|
| `convex_hull(space)` | worker @127 | build the gist **context** (hull of loop-nest domain in predicate space) | STRONG |
| `set::gist(context)` | worker @129 | **the core simplifier** — strip guards entailed by the loop/param context | STRONG |
| `compute_divs()` → `remove_divs()` | worker @134 (overapprox only) | materialise then project existential floor/mod divs → div-free over-approx | STRONG |
| `get_basic_sets()` | worker @140 | decompose; expect a single convex piece after gist+hull | STRONG |
| `get_constraints()` | worker @141 | extract the `isl::constraint` list of the convex result | STRONG |
| `get_space()` | worker @126 | the space for the context / universe | STRONG |
| `set::gist(box)` → `coalesce()` | `shrink_domain` @51 | **second, distinct** gist against a rebuilt box, then fuse pieces | STRONG |
| `set::convex_hull()` | `shrink_domain_convex_hull` @24 | convexify a non-convex union before shrink | STRONG |
| `is_empty()` | `shrink_domain` @32 | early-out: drop a fully-empty (gisted) domain | STRONG |
| `BasicSet::universe(space)` | `shrink_domain` @47 | fresh full set to rebuild the loop box | STRONG |
| `intersect_range(valid)` | `predicate_access_range` @97 | clip an access map's range to the valid address window | STRONG |

> **QUIRK —** `gist` appears **twice** in the module (predicate path @129 against the convex hull, shrink path @51 against the rebuilt box), and `coalesce` appears **exactly once** (chained on the shrink-path gist). `detect_equalities` — the canonical ISL normalizer one would expect before reading constraints — is **never called by name**; equality detection is folded into ISL's own `get_constraints` normalization. A reimplementer porting to a fresh `isl` binding does not need an explicit `detect_equalities` call to match behavior.

> **GOTCHA —** every ISL call is wrapped in `this->try(λ)`, an exception-guarded apply. ISL operations (`gist`, `convex_hull`, `compute_divs`) can throw on resource-exhaustion or malformed sets; `try` catches and yields a "skip this simplification" signal rather than aborting the compile. Omitting the guard turns a transient ISL failure into a crash. The `try` result *replaces* `domain`, so a caught failure must leave `domain` in a usable (e.g. unchanged) state.

> **NOTE —** the single-basic-set fast path (`if len(bsets)==1: domain = bsets[0]`) is not just an optimization — `get_constraints` on a multi-piece union does not yield a single conjunction. After `gist` against a convex context the result is *usually* one convex basic set, so the common case is a clean single conjunction. When it is *not* one piece, the code reads constraints from the whole set; the `any(p is None)` check then guards against an infeasible component collapsing the predicate list to `None`.

---

## predicate_access_range — Access-Range-Driven Predicates

### Purpose

The access-range variant of the predicate path. Instead of starting from the instruction's iteration domain, it starts from the iteration points whose *tensor address* lands inside a caller-supplied `valid_range`, derives the guard that exactly bounds the in-range accesses (e.g. masking an out-of-bounds tail), and installs it.

### Algorithm

```c
void IslSimplifier::predicate_access_range(                      // sym @0x11600 (cython) 
        bir::Instruction *inst, isl::set valid_range):
    acc = this->access(inst);                     // line 96: access UnionMap  { sN[ivs] -> tensor[addr] }
    rng = acc.intersect_range(valid_range);       // line 97: clip RANGE to the legal address window
    R = this->predicates_over_loopnest(           // line 99: simplify over the access-restricted
            inst, /*domain=*/rng.domain(), ...);  //          DOMAIN (= access-range pre-image)
    if (R != None):
        inst->resetPredicates(*R);                // ~line 100
        inst->addPredicate(*R);                   // ~line 101
```

`intersect_range` is stock islpy/`isl::map`: it restricts the access relation's *range* to `valid_range`; the *domain* of the restricted map is then the set of iteration points that touch only valid addresses, which becomes the predicate domain. Net effect: the simplifier derives the exact loop-nest guard that keeps accesses inside the tensor's legal index window.

---

## shrink_domain / shrink_domain_convex_hull — Domain Narrowing

### Purpose

The *domain-narrowing* path (vs. predicate-simplify). Where the predicate path leaves a runtime guard, this path rewrites the **loop bounds** so the guard disappears. Native symbol: `shrink_domain(bir::InstLoop*, isl::set)` — note it takes a `bir::InstLoop*`, the loop instruction, not a generic `Instruction*`.

### Algorithm — convex-hull pre-step

```c
Loopnest IslSimplifier::shrink_domain_convex_hull(              // sym @0x12c40 (cython)
        bir::InstLoop *bottom_loop, isl::set domain, bool approximate_predicates):
    domain = this->try([&]{ return domain.convex_hull(); });    // line 24: convexify union
    return this->shrink_domain(bottom_loop, domain,             // line 28
                               /*approximate_predicates=*/approximate_predicates);
```

### Algorithm — the narrowing worker

```c
Loopnest IslSimplifier::shrink_domain(                         // sym @0x17b8c0
        bir::InstLoop *bottom_loop, isl::set domain,
        fixed_axes, bool approximate_predicates):
    axis = bottom_loop->front;                    // top axis of the nest
    if (this->try([&]{ return domain.is_empty(); })):           // line 32: empty -> bail
        return ...;
    lbs, ubs = exract_cst_bounds(domain, ...);    // const lb/ub per axis (base-class codec)

    box = isl::BasicSet::universe(space);         // line 47: fresh full set over the IV space
    box = box.add_loop_bounds(...)                // line 48: inject loop-nest IV bounds
              .add_param_bounds(...);             // line 49: inject spmd/param bounds

    // SECOND gist (distinct from the predicate path) + coalesce into minimal-piece union
    domain = this->try([&]{                        // line 51 (lambda2)
        return domain.gist(box).coalesce(); });

    narrowed = apply_predicates(box, predicates, ...);          // line 65: intersect predicates -> narrow

    new_axes = [];
    for (old, (lo, hi) in zip(axes, zip(lbs, ubs))):            // line 63 (per-axis genexpr)
        a = shallowClone(old);                    // IRCloner.shallowClone
        a.lb = lo; a.ub = hi; a.stride = ...;     // tightened bounds from exract_cst_bounds
        if (a.tripcount < old.tripcount):
            IslSimplifier::eliminated_iterations += (old.tripcount - a.tripcount);
            if (a.is_strided):
                IslSimplifier::strided_axes += 1;
        new_axes.append(a);
    if (loop fully eliminated):
        IslSimplifier::eliminated_loops += 1;
    return new_loopnest(new_axes);                // rebuilt, tighter nest
```

> **QUIRK —** `shrink_domain` rebuilds a *fresh* canonical box (`BasicSet.universe` + `add_loop_bounds` + `add_param_bounds`) and gists the domain against *that*, rather than reusing the predicate-path convex hull. The reason: the predicate path's context is the convex hull of the original domain (good for stripping redundant guards), but the shrink path needs a *box in axis-canonical form* so that `exract_cst_bounds` can read one tight `lb/ub/stride` per axis. Gisting against a hull would leave the bounds entangled across axes; gisting against the box keeps them separable. This is why there are two distinct gists in the module, and why `coalesce` (which only makes sense on the box-gisted, multi-piece-prone result) appears only here.

> **NOTE —** `fixed_axes` pins axes that must not be reshaped (e.g. an axis whose extent is fixed by a downstream consumer); those are cloned with their original bounds. `approximate_predicates` threads down to `apply_predicates` to decide whether approximate guards participate in the narrowing intersection.

This is the mechanism that converts a predicate such as `0 <= i < 10 ∧ i < 7` into an actual tightened loop bound `for i in 0..7` with no residual runtime mask: gist the domain against the box, read the now-`[0,7)` axis bound, and clone the axis with the smaller `ub`. Axes whose tripcount drops feed `eliminated_iterations`; whole loops that vanish bump `eliminated_loops`; axes that become strided bump `strided_axes`.

---

## The isl↔AffinePredicate Conversion

The forward and back conversions both live in the base `IntegerSetAnalysis`, and both are confirmed native symbols.

**Forward (`AffinePredicate` → `isl::set`):** `predicated_domain(bir::Instruction*, isl::set)` `@0x17e4d0` builds `domain ∩ (conjunction of exact predicates)`. The per-expression piece is `build_aff` / `quasi_affine_expr` / `build_linear_expr` turning a `bir::QuasiAffineExpr` into an `isl::aff`, and `add_loop_bounds` injecting the IV ranges.

**Back (`isl::constraint` → `AffinePredicate`):** `enumerate_affine_predicates(pelican::PelicanContext*, …)` `@0x17c400` walks each surviving `isl::constraint` — an affine inequality `a0 + Σ ai·xi ≥ 0` over loop IVs `xi` and SPMD-grid parameters — and emits one Penguin `AffinePredicate`. The `PelicanContext` resolves IV/parameter names; the SPMD-grid parameter dimensions (`spmd_ids`) supply the set's parameter space. A `None` element signals an infeasible constraint set (empty domain), which propagates up as the whole-pass `None` short-circuit.

> **GOTCHA —** the back-conversion is where soundness lives. `gist` can legitimately *enlarge* a non-convex predicate union when over-approximating (the `convex_hull` context and `compute_divs/remove_divs` both can weaken the set). The result is therefore a *sound superset* of the original guard — it never rejects an iteration the original would have admitted — but it may admit iterations the original guarded out, relying on the loop bounds to exclude them. A reimplementer must keep the `overapproximate` flag honest: it is only valid where a weaker (superset) guard is acceptable because the surrounding loop/param context already excludes the difference.

---

## Statistics

Four class-level (static) counters, registered with Penguin's `Statistics` subsystem and surfaced as human-readable lines:

| Counter | Display string | Bumped in | Confidence |
|---|---|---|---|
| `eliminated_predicates` | "Number of predicate eliminated" | `simplify_predicate` (§ public entry) | STRONG |
| `eliminated_loops` | "Number of loops eliminated" | `shrink_domain` (fully removed axis) | STRONG |
| `eliminated_iterations` | "Number of iteration eliminated" | `shrink_domain` (tripcount drop) | STRONG |
| `strided_axes` | "Number of strided axes" | `shrink_domain` (axis becomes strided) | STRONG |

> **CORRECTION (Y04-02) —** the exact display strings ("Number of predicate eliminated", etc.) and the `eliminated_predicates` qualname could **not** be located in any `strings.json` in the corpus (the strings that *do* match, e.g. in `DeadStoreElimination`, belong to unrelated counters). They are reported as STRONG on the strength of the consistent counter-bump pattern in the call sequences, but a reimplementer should treat the precise display wording as INFERRED, not verbatim-confirmed.

---

## Related Components

| Component | Relationship |
|---|---|
| `islwrapper::IntegerSetAnalysis` (libBIR.so) | Base class — owns every `AffinePredicate`↔`isl` codec and domain builder that `IslSimplifier` orchestrates |
| `SimplifyPredicates.cpython-310` | Python pass that instantiates `IslSimplifier` and calls `simplify_predicate` / `shrink_domain` on predicated instructions |
| `TongaIslSimplifier` / `TongaSimplifyPredicate` | Tonga-target-specialized siblings (access→address rewrite) — see § 5.20 |
| `pelican::PelicanContext` | name/parameter resolution context threaded through the back-conversion |

## Cross-References

- [TongaIslSimplifier](tonga-isl-simplifier.md) — the Tonga-specialized sibling that does access→address rewriting on top of the same ISL machinery
- [ISL Dependence Graph](isl-dependence-graph.md) — how Penguin builds the `isl::map` access relations and iteration domains this pass consumes
- [ISL Schedule-Tree Legality](isl-schedule-tree-legality.md) — the validation-only ISL use (the legality sibling of this transform-driving use)
- [Predicate Bridge](predicate-bridge.md) — the `AffinePredicate` IR model and the bridge between Penguin guards and ISL constraints
