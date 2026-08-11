# AffineExpr / AffinePredicate ⇄ ISL ⇄ pelican::Expr Bridge

> **Scope.** How a Penguin quasi-affine expression reaches isl and comes back. This is the *plumbing* between three layers that every other Part-5 polyhedral page assumes: the Python `AffineExpr` / `AffinePredicate` faces, the C++ `pelican::Expr` linear form they lower to, and the isl `Aff` / `Set` the dependence analysis actually computes on. The single most important structural fact — and the one most likely to be assumed wrong — is that **there is no `AffineExpr → isl` function**. The bridge is a *two-module, three-stage* pipeline, and the isl leg lives in a different binary entirely from the modules whose names contain "Affine". By the end of this page a reader can reproduce the round trip in both directions.

Related reading: [Penguin AffineExpr Algebra](affine-expr-algebra.md) (5.4 — the `pelican::Expr` node taxonomy this page lowers to), [Penguin ISL Dependence-Graph Construction](isl-dependence-graph.md) (5.16 — `build_aff` and the isl-side domain/access machinery), and the `pelican::Expr` C++ hierarchy in Part 7.

---

## The three stages and the two modules

The forward direction is three stages crossing a single common currency — the **flat pelican linear form** `c + Σ coeffᵢ·idxᵢ`:

```
  Penguin AffineExpr  ──(A: linearize)──►  FLAT pelican::Expr  ──(B: build_aff)──►  isl.Aff / isl.Set
  isl result          ──────────────────(C: enumerate)─────────────────────────►  Python AffinePredicate
```

| Stage | What | Where it lives | isl? |
|---|---|---|---|
| **A** | `linearize_affineexpr` / `linearize_affineindices`, `cc_div` / `cc_mod`, `pred_{eq,ge,gt,le,lt}`, `is_legal_predicate`, `wrap_expr` | `ir/AffineExpr.so`, `ir/AffinePredicate.so` (Cython, unstripped) | **NONE** |
| **B** | `build_aff` / `affine_exp` — flat pelican kind → `isl.Aff`/`PwAff` verbs | C++ `islwrapper::IntegerSetAnalysis` in `libBIR.so` (driven from the Python `IntegerSetAnalysis` base) | **all isl here** |
| **C** | `enumerate_affine_predicates` — `isl.Constraint` → pelican expr → `AffinePredicate` | same `islwrapper::IntegerSetAnalysis` base | reads isl |

**Stage A touches no isl at all.** Neither `AffineExpr.so` nor `AffinePredicate.so` imports `isl` or `islpy`, and neither contains a single `isl` string. Their import set is `neuronxcc.pelican.ir` (the C++ pelican factory home), `neuronxcc.starfish.penguin.common`, sibling `…penguin.ir.Af*` modules, plus `numpy` / `numbers` / `collections` / `functools` / `math`. The two modules whose *names* contain "Affine" never see an integer set — they only build and normalise `pelican::Expr` nodes. All isl arithmetic is in `libBIR.so`.

**Where the isl leg actually lives.** `build_aff`, `predicated_domain`, `enumerate_affine_predicates`, `quasi_affine_expr` and `convex_hull` are methods of the **C++ class `islwrapper::IntegerSetAnalysis` compiled into `libBIR.so`**, and that is where the domain/aff/predicate algebra runs. The Python `IntegerSetAnalysis` (Cython, `0x4c790`) is a thin policy/glue layer above it, as is the `TongaIslDependenceAnalysis` subclass; neither carries the arithmetic. Wherever this page says "`build_aff` lives in `IntegerSetAnalysis`", read it as that C++ base — the same split described in [5.16](isl-dependence-graph.md).

**Why the split is structural, not incidental.** Penguin's `AffineExpr` is a *tree* — nested `SumExpr` / `MultExpr` / `ModuloExpr` / `FloorDivExpr` over `AffineIdx` leaves, hash-consed in an `llvm::FoldingSet` (see [5.4](affine-expr-algebra.md)). isl wants a *flat* `{ c + Σ coeffᵢ·varᵢ }` with explicit floor-div locals. Doing the tree→flat fold in **pelican** (Stage A) rather than inside the isl glue means the *same* flattened expression is reused by all three of pelican's consumers: isl dependence analysis, BIR emission (`bir::QuasiAffineExpr`), and JSON serialisation (`toJsonv2`). isl is only one of three consumers, so the flatten *cannot* live inside the isl glue. The flat pelican form is the hub; isl is a spoke.

---

## Stage A.1 — `linearize_affineexpr` / `linearize_affineindices`

Both are module-level functions in `AffineExpr.so` (prefix `__pyx_pw_9neuronxcc_8starfish_7penguin_2ir_10AffineExpr_*`), bodies inlined into the `pyx_pw` wrappers (no separate `pyx_pf` symbols).

| Function | Offset | Generator body | Operates on |
|---|---|---|---|
| `linearize_affineexpr(expr)` | `0x17e00` | `__pyx_gb_…_20…generator2 @0x16290` | ONE `AffineExpr` tree |
| `linearize_affineindices(indices)` | `0x171c0` | `__pyx_gb_…_23…generator3 @0x157f0` | a *vector* of index exprs (an access's per-dim addrs) |

Each wrapper allocates two Cython closure scopes — `scope_struct_*_linearize_*` plus a nested `_genexpr` — confirmed by the referenced freelist symbols. The nested genexpr iterates the `SumExpr` term list (interned name `n_terms`, the AG10 `SumExpr.n_terms@+0x28` / `terms@+0x20`), producing per-term `(coeff, idx)` pairs. The Cython function is a *thin orchestrator + result-adopter*; the real arithmetic — folding nested `Mult`/`Sum` into one coefficient-per-`AffineIdx` accumulation plus a scalar `c` — is the C++ pelican `AffineExpr::flattenTerms` / `getLinearExpr` / `accumulateTerm` (see [5.4 §the flatten](affine-expr-algebra.md)).

```c
// linearize_affineexpr(expr)  @AffineExpr.so 0x17e00
//   orchestrator shape is read from the wrapper; the arithmetic is [INFERRED]
//   from the pelican OPS it drives
PyObject *linearize_affineexpr(PyObject *expr) {
    // genexpr over expr's SumExpr terms (n_terms slot); the heavy fold is in C++:
    //   pelican::AffineExpr::flattenTerms / getLinearExpr / accumulateTerm
    acc = {};                                  // map<AffineIdx*, int64 coeff>
    int64 c = 0;
    for ((coeff, idx) in walk_terms(expr))     // tree → (coeff,idx) pairs + const fold
        if (idx == NULL) c += coeff;           //   bare scalar
        else acc[idx] += coeff;                //   coefficient-per-index accumulation
    return SumExpr_from(acc) + c;              // canonical  c + Σ coeffᵢ·idxᵢ  (re-interned in FoldingSet)
}
```

`linearize_affineindices` is the *vector* variant: it flattens each address index of an `Access` independently and returns one flat pelican `AffineExpr` per access dimension. It is the function `Access.linearize_indices` calls to turn a multi-dim access-pattern index list into the flat addr-expr list that `build_aff` consumes one-per-tensor-dimension (the `affs=[build_aff(a) for a in addrs]` loop in [5.16 §access](isl-dependence-graph.md)).

> **NOTE — the BIR layer has its own linearizer.** `libBIR.so` / `libwalrus.so` carry a *separate* C++ `bir::QuasiAffineExpr::linearize_affineexpr(pelican::PelicanContext*, llvm::SmallVector<unsigned long,4>&, std::vector<bir::QuasiAffineExpr>&)` at `0x5e9e70`, called by `backend::Unroll::genPhyAP` during access-pattern materialisation. That is the *BIR-emission* consumer of the same flat form — corroborating that the flatten is shared currency, not isl-specific. The Python `AffineExpr.so` function and this BIR method are distinct symbols feeding different back-ends from the identical canonical form.

---

## Stage A.2 — `cc_div` / `cc_mod` — the collective-cyclic constructors {#collective-cyclic}

`cc_div` `@0x23b30` and `cc_mod` `@0x233d0` are byte-for-byte the same shape — both `0x760` long, identical call topology, and a symmetric `ccdiv`/`ccmod` interned-name pair. They are the Python factory front-doors for the collective-cyclic pelican expressions:

| Python | pelican class | kind | factory |
|---|---|---|---|
| `cc_div(numer, denom, rgid)` | `CCDivExpr` | **27** (`CCDivKind`) | `sub_62B8C0` |
| `cc_mod(numer, denom, rgid)` | `CCModExpr` | **28** (`CCModKind`) | `createCCModExpr` @walrus `0x18f5e30` |

Each `CC*Expr` is a `BinaryExpr` plus one extra field (AG10 layout):

```
numer            @+0x20  RefPtr<Expr>   // the rank / linear index being divided
denom            @+0x28  int64 (>0)     // the group_size / cyclic modulus  (DivLike: denom>0 invariant)
replica_groups_id@+0x30  uint64         // which collective replica-group set
```

**Semantics.** For a collective op with global rank `r` and group size `g`:

```c
CCDivExpr(r, g, rgid)  =  floor(r / g)   // SHARD INDEX        — which group the rank is in
CCModExpr(r, g, rgid)  =  r mod g        // WITHIN-SHARD OFFSET — rank's position inside its group
```

This is how a sharded collective's per-replica address decomposes into *(group-selector, intra-group-offset)* — the cyclic / block-cyclic distribution of a tensor across the replica set. They are *not* generic `ModuloExpr` / `FloorDivExpr`: their `denom` is a **runtime** collective parameter (`group_size`), so they are tagged with `replica_groups_id` to keep distinct collective groups apart on the isl side.

**Body.** One `__Pyx__GetModuleGlobalName` (resolve `CCDivExpr`/`CCModExpr` from `neuronxcc.pelican.ir`), one `PyObject_GetAttr`, one `PyObject_Call` (construct). A `__pyx_ctuple_long` — a C-tuple of `int64`, sitting adjacent to the `cc_div` qualname in the string pool — passes the integer `(denom, replica_groups_id)` operands at the C level.

> **GOTCHA — the "CC-ness" collapses inside isl.** On the isl side (Stage B) a `CCDivKind` maps to a *ceiling* `scale_down_val` and a `CCModKind` to a *ceiling* `mod_val` — the same primitives a plain `FloorDivExpr`/`ModuloExpr` (floor) uses, only with the rounding direction flipped. The `replica_groups_id` has already selected *which* rank affine feeds `numer`, so isl sees an ordinary integer-division local. The collective identity is a Penguin/pelican distinction that has no isl representation. See [5.16 §build_aff GOTCHA](isl-dependence-graph.md) — mapping all four kinds onto floor semantics silently mis-rounds the ceiling pair.

---

## Stage B — `build_aff` — flat pelican → `isl.Aff` / `PwAff`

`build_aff` is the only place a pelican `Expr` touches isl. It lives in the C++ `islwrapper::IntegerSetAnalysis` base and is reversed in full on [5.16 §build_aff](isl-dependence-graph.md); it is reproduced here as the bridge's Stage B because the round trip is incomplete without it.

```c
// build_aff(self, expr, space=None, loopnest=None, params=None)   — dispatch per 5.16
isl_aff *build_aff(self, expr, space, loopnest, params) {
    space = space ?: self->create_domain_space(loopnest, params);
    return build(expr);                        // recursive descent on expr.kind (pelican ExprKind)
}

isl_aff *build(expr) {
    switch (expr->kind) {                       // discriminator read by expr_kind (§Stage A.4)
      case CExpr:        return isl.Aff.val_on_domain(space, Val(expr->c));
      case SumKind /*18*/:                       // the §A.1 flat form, term by term
          acc = val_on_domain(space, Val(0));
          for (t in expr->terms) acc = acc.add( build(t->idx) * t->coeff );  // c + Σ coeffᵢ·idxᵢ
          return acc.add( val_on_domain(space, Val(expr->c)) );
      case MultKind /*23*/:    return build(expr->sub).scale(val_on_domain(space, Val(expr->coeff)));
      case FloorDivKind /*25*/: return build(expr->numer).scale_down_val(expr->denom);  // floor
      case ModuloKind  /*26*/:  return build(expr->numer).mod_val(expr->denom);
      case CCDivKind   /*27*/:  return build(expr->numer).scale_down_val(expr->denom);  // CEILING variant
      case CCModKind   /*28*/:  return build(expr->numer).mod_val(expr->denom);          // CEILING variant
      default: /* AffineIdx leaf */
          pos = position_of(expr, loopnest_ivs, params);   // POSITIONAL, by loopnest-IV / param order
          if (pos.in_set)   return isl.Aff.var_on_domain(LocalSpace(space), dim_type.set,   pos.i);
          if (pos.in_param) return isl.Aff.var_on_domain(LocalSpace(space), dim_type.param, pos.i);
          raise("<expr> doesn't appear in params or loopnest");   // verbatim message
    }
}
```

Two conventions matter for reproduction:

- **Positional variable identity.** An `AffineIdx` resolves to its *position* in `dim_type.set` (the enclosing loopnest induction variables, in loop-nest order) or `dim_type.param` (`SymbolicIdx` / runtime params). The variable identity is positional, keyed by loopnest-IV order — never by a textual name. The names `"sN"`, `"i"`, `"j"` appear only in isl's human-readable string form (`{ s0[i] : 0<=i<=10 }`). One Penguin instruction ↔ one isl tuple `"sN"` where `N = inst.id`.
- **Coefficient extraction.** The per-term `int64 coeff` (`AffineExpr.terms[i].coeff@+8`) becomes the isl `Val` multiplied onto `var_on_domain`; the scalar constant `c` (`AffineExpr.c@+0x38`) becomes a `val_on_domain` added in. So flat `c + Σ coeffᵢ·idxᵢ` maps term-by-term onto isl `c + Σ coeffᵢ·(set|param dim)`. All of `val_on_domain` / `var_on_domain` / `add` / `scale` / `scale_down_val` / `mod_val` are **stock islpy ~2023.1**.

`affine_exp` (generator) is the per-tensor-dimension sibling: it emits one `isl.PwAff` per access dimension over a `LocalSpace` (`isl.LocalSpace.from_space`), assembled via `isl.MultiPwAff.from_pw_aff_list([...])`. It feeds `get_alloc_remapping`'s address affs.

---

## Stage A.3 — the predicate side: five verbs, two native forms

`AffinePredicate.so` (prefix `__pyx_pw_…_15AffinePredicate_*`):

| Function | Offset | Size |
|---|---|---|
| `is_legal_predicate(preds)` | `0x8980` | `0xA40` |
| `pred_lt(lhs, rhs)` | `0x93c0` | `0xB50` |
| `pred_eq(lhs, rhs)` | `0x9f10` | `0xB70` |
| `pred_ge(lhs, rhs)` | `0xaa80` | `0xB70` |
| `pred_le(lhs, rhs)` | `0xb5f0` | `0xB70` |
| `pred_gt(lhs, rhs)` | `0xc160` | `0xB50` |
| `wrap_predicate(c_pred)` | `0xccb0` | — |

All five comparison constructors **normalise to exactly two native forms** before building the predicate: `e >= 0` and `e == 0`. There is exactly one comparison direction internally — `ge` — and it is a **boolean keyword argument**, not an operator name. `n_s_ge` is the only interned comparison-flag name in the module (there is no `n_s_eq` / `n_s_le` / `n_s_lt` / `n_s_gt` / `n_s_ne`), and it is resolved via `_PyDict_GetItem_KnownHash` as a kwarg key. This is the classic Presburger normalisation: every isl constraint is `e >= 0` (or `e == 0`), so the predicate layer pre-bakes that form and the isl glue drops it straight onto a `Set` with no per-comparator logic.

```c
// each pred_XX(lhs, rhs) — op-shape IDENTICAL across all five:
//   1× PyNumber_Subtract  [strict only: 1× PyInt_SubtractObjC($1)]  1× PyObject_Call  2× PyObject_RichCompare
PyObject *pred_XX(lhs, rhs) {
    e = SUB(a, b);                       // operand order per verb (below)
    if (STRICT) e = e - 1;               // __Pyx_PyInt_SubtractObjC.constprop.0, imm $0x1
    return AffinePredicate(e, ge=FLAG);  // ge=True → e>=0 (inequality);  ge=False → e==0 (equality)
}
```

The five verbs differ only in subtraction operand order, the `-1` for the strict pair, and the `ge` bool:

| Verb | Builds | Meaning |
|---|---|---|
| `pred_ge(lhs,rhs)` | `AffinePredicate(lhs - rhs,     ge=True)` | `lhs - rhs >= 0` |
| `pred_le(lhs,rhs)` | `AffinePredicate(rhs - lhs,     ge=True)` | `rhs - lhs >= 0` (operands swapped) |
| `pred_gt(lhs,rhs)` | `AffinePredicate(lhs - rhs - 1, ge=True)` | `lhs - rhs >= 1` (strict, `-1`) |
| `pred_lt(lhs,rhs)` | `AffinePredicate(rhs - lhs - 1, ge=True)` | `rhs - lhs >= 1` (strict, swap + `-1`) |
| `pred_eq(lhs,rhs)` | `AffinePredicate(rhs - lhs,     ge=False)` | `rhs - lhs == 0` (equality) |

**Evidence:**
- Every `pred_*` references the same `ge` kwarg-name slot (`mstate+0xc8`) exactly once (5/5).
- `pred_eq` is the lone outlier on the `True`/`False` boolean-reference count — it passes the *opposite* `ge` value (`ge=False`). The four inequality verbs carry the `ge=True` count; `pred_eq` alone carries `ge=False`. This single discriminator is `>= 0` vs `== 0`.
- `SubtractObjC(-1)` appears **only** in `pred_lt` and `pred_gt` (1 each, 0 elsewhere) — the strict→non-strict `>= 1` rewrite: `a < b ⟺ a-b ≤ -1 ⟺ b-a-1 ≥ 0`.
- The two `PyObject_RichCompare` per function are constant-fold / triviality gates (is `e` a constant of known sign → return a trivially true/false predicate); they gate, they don't change the lowering.

**The pelican `ICmpExpr` it builds** (AG10 kind **20**, `createICmpExpr` @walrus `0x18f60f0`, alloc `0x38`, vtbl `0x90c290`):

```
compare_op @+0x20  int (ICmpExpr::CmpPred)   // ge=True → SGE   ge=False → EQ
lhs        @+0x28  RefPtr<Expr>              //   (the normalised e)
rhs        @+0x30  RefPtr<Expr>              //   (the constant 0)
```

Because the Python layer pre-normalises, the `AffinePredicate` constructor reaches exactly two `ICmpExpr` forms: `ICmp(SGE, e, 0)` (four inequality verbs) and `ICmp(EQ, e, 0)` (`pred_eq`). The bool `ge` selects the `CmpPred`; no other `CmpPred` value is reachable from the Python predicate layer. `ICmpExpr` is never serialised (no `toJson` case) — it is a control/predicate atom consumed by `InstCompareAndBranch::updateAffineExprs`, never an address expr.

> **GOTCHA — the int that `ge` maps to is not pinned.** The mapping `ge=True → SGE`, `ge=False → EQ` is read directly from the wrappers, but the concrete integer literals written to `ICmpExpr.compare_op@+0x20` for `SGE` vs `EQ` live in the `AffinePredicate.__init__` body, a `pelican.so` method rather than these wrapper `.so` files. The exact ordinals are **[INFERRED]**.

---

## Stage A.3′ — `is_legal_predicate` — the validity gate

`is_legal_predicate(preds)` `@0x8980` iterates the predicate list (loop var `p`, slot `mstate+0x148`) and per predicate fetches and tests two attributes (2× `PyObject_GetAttr` + 2× `PyObject_IsTrue`):

```c
bool is_legal_predicate(preds) {
    for (p in preds) {
        e   = p.expr;               // slot mstate+0xb0  — the underlying affine expr
        rt  = p.has_runtime_value;  // slot mstate+0xd0  — is the expr data-dependent?
        if (!is_affine(e) || PyObject_IsTrue(rt))   // illegal if non-affine OR runtime-valued
            return false;           // -> cannot become an exact isl Presburger constraint
    }
    return true;
}
```

A predicate is **legal** ⟺ its `expr` is a genuine compile-time affine expression **and** has no runtime value (no `IntRuntimeValue` / `IndirectArg` / `Opaque` term) — only then can it be an exact isl constraint. Illegal predicates are flagged **`"Invalid Predicate!"`**, raised by `addPredicateExprsToInst` when an illegal predicate reaches instruction attach. Legal-but-overapprox predicates are carried as `is_approx` and dropped by the isl simplifier before `gist`.

**How a legal predicate becomes an isl constraint** (Stage B, in `in_predicate_domain` / `predicated_domain`):

```c
for (p in legal_preds) {
    aff = build_aff(p.expr);                          // §Stage B — flat pelican → isl.Aff
    set = p.ge ? aff.ge_set(zero) : aff.eq_set(zero); // stock islpy
    domain = domain.intersect(set);                   // { sN[ivs] : ... and aff >= 0 }
}
```

Because Stage A guaranteed the `>= 0` / `== 0` form, the isl side needs only `ge_set` / `eq_set` — **no per-comparator branching, no strict-inequality handling** (already rewritten to `>= 1 ⊂ >= 0`).

---

## Stage A.4 — `wrap_expr` / `try_wrap_expr` / `expr_kind` — the C-ptr ⇄ Python adopters

These let the Python layer hold a `pelican::Expr*` as a typed Python object.

- **`expr_kind(expr)` `@0x24290`.** Reads `expr.kind` (the pelican `ExprKind @+0x10`); the body is dominated by the interned name `kind` (42 refs — the dispatch switch). This is the discriminator `wrap_expr` and `build_aff` read.
- **`wrap_expr(c_expr)` `@0x19d90`.** The kind-dispatched adopter: reads the kind and instantiates the matching Python face (`Expr` / `CExpr` / `AffineExpr` / `SumExpr` / `MultExpr` / `ModuloExpr` / `FloorDivExpr` / `CompoundExpr` / `CCExpr` / `CCDivExpr` / `CCModExpr` / `ICmpExpr`). Two `GetModuleGlobalName` resolve the target class per kind, then construct.
- **`try_wrap_expr(x)` `@0x190e0`.** Non-raising variant — returns the wrapped face or `None` if `x` is a plain int / non-expr.
- **`wrap_predicate(c_pred)` `@AffinePredicate.so 0xccb0`.** Predicate-side analogue; it imports `wrap_expr` to wrap the predicate's inner expr.
- **`remove_const_term(expr)` `@0x1f500`.** Splits an `AffineExpr` into `(Σ coeffᵢ·idxᵢ, c)`, dropping the constant `c@+0x38` — a helper for the `e - rhs` rewrites and for canonicalising an address's variable part separately from its offset.

---

## Stage C — the round trip back: `isl.Constraint → AffinePredicate`

The return leg is `enumerate_affine_predicates(constraints, cu, spmd_ids)`, in the C++ `islwrapper::IntegerSetAnalysis` base, invoked by `IslSimplifier` after `gist` / `convex_hull`. It is the **exact inverse of Stage A** in the canonical `>= 0` basis.

```c
// enumerate_affine_predicates(constraints, cu, spmd_ids)  — INFERRED mechanics,
//   grounded by the forward inverse + the AG10 factories
List<AffinePredicate> enumerate_affine_predicates(constraints, cu, spmd_ids) {
    out = [];
    for (c in constraints) {                       // domain.get_basic_sets()[0].get_constraints()
        a0 = c.get_constant_val();                 // a0           (stock islpy)
        terms = [];
        for (i in set_dims)
            if ((ai = c.get_coefficient_val(dim_type.set,   i)))  // ai != 0
                terms.push(MultExpr(cu.resolve_set(i),   ai));     // kind 23, factory sub_62BAA0
        for (j in param_dims)
            if ((pj = c.get_coefficient_val(dim_type.param, j)))
                terms.push(MultExpr(cu.resolve_param(spmd_ids, j), pj));
        e = wrap_expr( SumExpr(terms) + a0 );      // kind 18 (sub_62C8D0) / AffineExpr kind 17 (sub_62BCF0)
        out.push( c.is_equality() ? pred_eq(e, 0)  // isl ==0 → AffinePredicate(e, ge=False)
                                  : pred_ge(e, 0)); // isl >=0 → AffinePredicate(e, ge=True)
    }
    return out;                                    // replaces inst's predicates (resetPredicates + addPredicate)
}
```

Each `isl.Constraint` is an affine `a0 + Σ ai·xi {>= | ==} 0` over loop IVs `xi` (`dim_type.set`) and `spmd_id` params (`dim_type.param`). `cu` resolves each set-dim back to its loop `AffineIdx` / axis and each param-dim back to its SPMD parameter. Step 4 re-materialises `coeff·idx` terms into `MultExpr`/`SumExpr` — the inverse of the `linearize_*` fold — and step 5 wraps and builds the predicate with `pred_ge` / `pred_eq`. `get_coefficient_val` / `get_constant_val` / `is_equality` are stock islpy.

**Why the inverse is clean.** isl *always* returns constraints in `… >= 0` / `… == 0` normal form, which matches the Stage A canonical form exactly — that match is the whole reason Stage A normalises to `ge`. An isl `>= 0` constraint maps to a single `ge` `AffinePredicate` with no sign gymnastics.

```
  AffinePredicate(e, ge)  --build_aff-->  isl.Aff e  --ge_set-->  { e >= 0 }
  { e >= 0 } (post-gist)  --get_constraints-->  Constraint(a, a0)  --enumerate-->  AffinePredicate(e', ge)
```

where `e'` is `e` re-expressed in the (possibly fewer, gist-simplified) constraints. The pelican kinds used on the rebuild come from the AG10 kind table: `SumExpr`=18, `MultExpr`=23, `AffineExpr`=17, `ICmpExpr`=20.

---

## Reimplementation checklist

1. Penguin `AffineExpr` is a hash-consed pelican `Expr` *tree*; flatten it to `c + Σ coeffᵢ·AffineIdxᵢ` with `linearize_affineexpr` (one expr) / `linearize_affineindices` (a per-dim index vector), driving pelican `AffineExpr::flattenTerms` / `getLinearExpr`.
2. For collectives, `cc_div` / `cc_mod` build `CCDivExpr(27)=floor(rank/group_size)`=shard-index and `CCModExpr(28)=rank%group_size`=intra-shard-offset, each carrying `denom>0` + `replica_groups_id`.
3. `build_aff` (C++ `islwrapper::IntegerSetAnalysis`, `libBIR.so`) maps the flat form onto isl: `CExpr→val_on_domain`, `SumKind→fold add`, `MultKind→scale`, `FloorDiv/CCDiv→scale_down_val`, `Modulo/CCMod→mod_val` (CC pair = *ceiling*), `AffineIdx→var_on_domain` at a *positional* `dim_type.set|param` index; one tuple `"sN"` per instruction.
4. Predicates: `pred_{ge,le,gt,lt,eq}` normalise to just two native forms `e >= 0` (`ge=True`) / `e == 0` (`ge=False`) — `e = ±(lhs-rhs)` with `-1` for the strict pair, `ge=False` only for `pred_eq` — build an `AffinePredicate` wrapping `ICmpExpr(20, SGE|EQ, e, 0)`; `is_legal_predicate` gates on affine ∧ ¬`has_runtime_value` (else `"Invalid Predicate!"`). `build_aff(e).ge_set(0)` / `eq_set(0)` intersect them into the iteration domain.
5. After isl `gist` / `convex_hull`, `enumerate_affine_predicates(constraints, cu, spmd_ids)` reads each constraint's coefficient/constant vector and re-materialises `SumExpr(18)`+`MultExpr(23)` → `wrap_expr` → `pred_ge`/`pred_eq` — an exact inverse in the `>= 0` basis. islpy is stock ~2023.1 throughout; only the linearize / build_aff / predicate-normalise / enumerate glue is Neuron-authored.
