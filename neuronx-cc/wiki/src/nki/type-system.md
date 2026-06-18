# NkiTypeSystem and Traced-Tile Operator Overloading

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, cp310 wheel. The tracer lives in three Cython extension modules under `neuronxcc/nki/compiler/backends/neuron/`: `NkiTypeSystem.cpython-310-x86_64-linux-gnu.so` (the n-ary tracer), `NkiTypeSystemCmpOp.…so` (comparison synthesis), and `NkiTypeSystemLogicalOp.…so` (logical synthesis), all Cython 3.0.10 with `debug_info` — class and method names below are real `__pyx_pw_…` / `__pyx_n_s_…` symbols. cp311/cp312 are byte-twins. Addresses are pinned to the cp310 artifact's `.so`; the offsets quoted from the D-W09 report belong to a particular Cython output and the cp310 wheel's addresses differ (e.g. the base module's generator trampoline is at `0xbe30`, not `0x10b70`) — treat offsets as **module-relative landmarks**, the symbols and strings as the hard anchors.*

## Abstract

When a NKI kernel writes `lo <= x < hi`, or `i < N`, or `(a < b) & (c >= 0)` over **traced** operands, Python never computes a `bool`. The comparison and logical operators on traced tiles, scalars, and tile-indices are *synthesized at class-construction time* by three decorator modules and dispatched at trace time by **operand type category** into one of two outcomes: an **affine predicate / mask object** (the deferred symbolic structure documented in [§6.2.4 Mask / Predicate Algebra](./mask-predicate-algebra.md)), or — when a real `tensor` is involved — an elementwise tensor op whose result dtype is `np.int8`. This is the entire reason a NKI `mask=` argument or `affine_select` predicate can be built from ordinary Python comparison syntax: the syntax is the same, but the operators are overloaded to produce structure, not truth.

The machinery has three layers. **`NkiTypeSystem`** (the "TypeTraceContext" tracer) holds the only hand-written method bodies: it lowers Python *chained* comparisons (`a < b < c`) and `in` / `not in` into n-ary folds over the per-operator methods, and it provides `logical_and_nary` / `logical_or_nary` reductions. **`NkiTypeSystemCmpOp`** synthesizes `__eq__/__ne__/__lt__/__le__/__gt__/__ge__` onto traced-value classes via two class decorators, choosing a result type from a `TypeCategory` lattice. **`NkiTypeSystemLogicalOp`** synthesizes `__and__/__or__/__invert__` (and the `logical_and/logical_or/logical_not` the n-ary folds consume) over an *extended* `TypeCategory` lattice with a **promotion map** that coerces heterogeneous logical operands (mask, predicate, scalar, bool) to a common logical type before combining.

The single docstring fragment recovered from the base module states the purpose: *"This file define the TypeTraceContext class, it defines a tracer that implement NKI's core type system"* (`.rodata`, CONFIRMED). This page reconstructs the synthesis factories, the two `TypeCategory` lattices, the promotion lattice, and the exact dispatch each operator performs.

### What a reimplementer must reproduce

- The **synthesis decorators** — three class decorators that, applied at class-body evaluation, inject the six rich-comparison dunders and the three logical dunders (plus their `logical_*` aliases) onto every traced-value class.
- The **two `TypeCategory` lattices** — a 4-member one in `CmpOp` (`OTHER/SCALAR/TILE_INDEX/TENSOR`) and a 6-member one in `LogicalOp` (adds `MASK/PREDICATE`), each with an `isinstance`-chain classifier `get_type_category`.
- The **comparison dispatch** — given `(cat_a, cat_b)`, pick `EQTileMask`/`EQScalarPredicate` (for `==`/`!=`), `TileMaskIntersection`/`ScalarPredicateIntersection` (for `<,<=,>,>=`), or a `tensor._binop(…, dtype=np.int8)` elementwise op (when a `tensor` is present).
- The **logical dispatch** — `highest_category`, `promote_to_logical_type` via `PROMOTION_MAP`, then `operator.and_/or_/invert` over the promoted predicate/mask objects, with bool/number operands promoted to `AlwaysTrue/AlwaysFalsePredicate` and tensor operands routed to an elementwise `np.int8` op (or rejected, for `~`).
- The **chained-comparison lowering** — `compare_nary` rewriting `a OP1 b OP2 c` into `(a OP1 b) & (b OP2 c) & …` AND-folded by `logical_and_nary`.

| | |
|---|---|
| **Tracer module** | `NkiTypeSystem.…so` — class `NkiTypeSystem` (the "TypeTraceContext") |
| **Comparison module** | `NkiTypeSystemCmpOp.…so` — `synthesize_equality_comparisons`, `synthesize_ordered_comparisons` |
| **Logical module** | `NkiTypeSystemLogicalOp.…so` — `synthesize_logical_operations` |
| **CmpOp lattice** | `TypeCategory(IntEnum)` = `{OTHER, SCALAR, TILE_INDEX, TENSOR}` |
| **LogicalOp lattice** | `TypeCategory(IntEnum)` = `{OTHER, SCALAR, TILE_INDEX, MASK, PREDICATE, TENSOR}` |
| **Comparison result types** | `EQTileMask` · `EQScalarPredicate` · `TileMaskIntersection` · `ScalarPredicateIntersection` (from `…neuron.predicates`) |
| **Tensor-compare result** | `tensor._binop(numpy_op, other, dtype=np.int8)` → `return_tensor_or_extracted_scalar(…)` |
| **Promotion map** | `PROMOTION_MAP` : `TypeCategory → {promote_to_mask, promote_to_predicate, …}` (indexed by `highest_category`) |
| **Bool/number promotion** | `promote_other_to_predicate` → `AlwaysTruePredicate()` / `AlwaysFalsePredicate()` |
| **Affine predicate ctors** | `pred_lt / pred_le / pred_gt / pred_ge` (from `starfish.penguin.ir.AffinePredicate`) |
| **Forbidden** | inverting / bool-testing a `tensor` → `err_ambiguous_tensor_truth_value` |

> **The one claim to internalise.** A comparison or logical operation on a traced NKI value is **never** a Python `bool`. `i < N` evaluates to an `EQTileMask` / `TileMaskIntersection` *object*; `p & q` to a combined predicate/mask *object*; a tensor compare to an `int8`-dtype tensor op. The "type system" is precisely the dispatch that guarantees this: every operator is replaced, at class-construction, with a handler that classifies its operands and constructs structure. This page is the construction side; [§6.2.4](./mask-predicate-algebra.md) is the algebra and lowering of the objects it produces.

---

## 1. Three modules, one decorated class set

The traced-value classes — `scalar`, `tensor`, `tile_index`, and the predicate/mask classes — are built by the metaclass machinery ([§6.3.2](./type-metaclasses.md)) and then **decorated** with three class decorators that rewrite their operator slots. The decorators are applied at module-exec time of whatever module defines the traced-value class; the base `NkiTypeSystem` class itself is decorated by all three (its module-exec looks up the three `synthesize_*` names and applies them as class decorators), which is why `NkiTypeSystem` also gains `logical_and`/`logical_or` that its own `logical_and_nary`/`logical_or_nary` consume.

```
                       class body of a traced-value class
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                         ▼
 @synthesize_logical_operations   @synthesize_equality_   @synthesize_ordered_
   (NkiTypeSystemLogicalOp)         comparisons              comparisons
                                   (NkiTypeSystemCmpOp)     (NkiTypeSystemCmpOp)
            │                        │                         │
   injects __and__ __or__          injects __eq__            injects __lt__ __le__
   __invert__ + logical_and        __ne__                    __gt__ __ge__
   logical_or logical_not
```

Each injected dunder is a thin `make_binary_method` / `make_unary_method` wrapper around a *handler closure* produced by one of three factories (`make_equality_operation`, `make_comparison_operation`, `make_logical_operation` / `make_logical_not`). The handler is where the type-category dispatch lives. All factory and wrapper symbols are CONFIRMED in the modules' `_strings.json`:

```text
NkiTypeSystemCmpOp:      make_equality_operation   make_comparison_operation
                         make_binary_method        make_commutative_binary_method
                         synthesize_equality_comparisons  synthesize_ordered_comparisons
NkiTypeSystemLogicalOp:  make_logical_operation     make_logical_not
                         make_binary_method         make_unary_method
                         synthesize_logical_operations
```

> **NOTE — the same factory shape repeats across all three layers.** Every operator is `setattr(cls, dunder, wrapper(handler))` where `handler = factory(op, op_name, numpy_op[, pred_op])`. The `op` is the stdlib `operator.<x>` (used as the actual reducing operation on already-promoted operands), `op_name` is the dunder stem used for error messages and the tensor `_binop` selector, `numpy_op` is the `np.<x>` ufunc passed to the tensor path, and (for ordered comparisons) `pred_op` is the affine-predicate constructor `pred_lt/le/gt/ge`. A reimplementer can build all nine operators from one parameterized factory family.

---

## 2. The `TypeCategory` lattices and `get_type_category`

The dispatch key is an `IntEnum` *category* assigned to each operand by an `isinstance` chain. There are **two** lattices — `CmpOp` has four members, `LogicalOp` has six. Both are CONFIRMED: every member name appears as a `__pyx_n_s_<MEMBER>` string in the respective module, and each module ships a `TypeCategory.__str__` symbol (pretty enum printing).

### 2.1 CmpOp lattice (comparisons)

```c
// NkiTypeSystemCmpOp — TypeCategory(IntEnum), members CONFIRMED via _strings.json
//   {OTHER, SCALAR, TILE_INDEX, TENSOR}      // exactly four — no MASK/PREDICATE here
enum TypeCategory { OTHER, SCALAR, TILE_INDEX, TENSOR };   // integer values INFERRED

// get_type_category  (__pyx_pw_…_18NkiTypeSystemCmpOp_1get_type_category, CONFIRMED body)
TypeCategory get_type_category(PyObject *x) {
    if (isinstance(x, scalar))      return SCALAR;       // neuron `scalar`
    if (isinstance(x, tensor))      return TENSOR;       // neuron `tensor`
    if (isinstance(x, tile_index))  return TILE_INDEX;   // `tile_index`
    return OTHER;                                        // numbers / np scalars / anything else
}
```

### 2.2 LogicalOp lattice (logical combine)

The logical module extends the lattice with `MASK` and `PREDICATE` so that already-built mask/predicate objects classify distinctly from the index/scalar/tensor inputs. The `isinstance` chain order is recovered directly from the decompiled body of `get_type_category` (`__pyx_pw_…_22NkiTypeSystemLogicalOp_1get_type_category`, file `…_1get_type_categor_0x15cb0_…c`), which probes the module globals in exactly this sequence: `scalar → tensor → tile_index → nki_mask → predicate`, falling through to `OTHER`:

```c
// NkiTypeSystemLogicalOp — TypeCategory(IntEnum), all six members CONFIRMED
//   `__pyx_k_MASK` and `__pyx_n_s_PREDICATE` are real constants in this module's pool
enum TypeCategory { OTHER, SCALAR, TILE_INDEX, MASK, PREDICATE, TENSOR };  // ints INFERRED

// get_type_category  (CONFIRMED — decompiled isinstance chain, in this order)
TypeCategory get_type_category(PyObject *x) {
    if (isinstance(x, scalar))      return SCALAR;
    if (isinstance(x, tensor))      return TENSOR;
    if (isinstance(x, tile_index))  return TILE_INDEX;
    if (isinstance(x, nki_mask))    return MASK;        // a built tile-mask object
    if (isinstance(x, predicate))   return PREDICATE;   // a built scalar-predicate object
    return OTHER;                                        // bool / number / etc.
}
```

> **GOTCHA — the `isinstance` probe order is not the precedence order.** `get_type_category` returns the *first* matching class, so its chain order is just classification, not dominance. The dominance order — which category "wins" when two operands differ — is the `IntEnum` *integer* ordering consumed by `highest_category(a,b) = max(get_type_category(a), get_type_category(b))`. With members declared `OTHER, SCALAR, TILE_INDEX, MASK, PREDICATE, TENSOR`, the auto-assigned `IntEnum` values (0..5) put **`TENSOR` highest**, so any operation touching a real tensor takes the tensor path. The *exact integer values* are **INFERRED** from declaration order — `IntEnum` member values are interned at import and not readable from a fixed struct — but the relative ordering (`TENSOR` dominant, `OTHER` least) is corroborated by the dispatch: the tensor branch is checked first in every handler, and `promote_to_logical_type` explicitly refuses `TENSOR`. Tagged **STRONG** for the ordering, **INFERRED** for the literal ints.

There is also a `get_target_type_category` symbol (CONFIRMED, `…_3get_target_type_c_0xfb50_…`) and a `PROMOTION_TARGET_MAP` (CONFIRMED name): these map an `(catA, catB)` pair to the *target* logical category (MASK vs PREDICATE) that the promotion should produce. The precise contents of `PROMOTION_TARGET_MAP` are **INFERRED** (the map is built at import; only its name and the two target categories are byte-confirmable).

`TYPE_COMBINATIONS = itertools.product(TypeCategory, repeat=2)` (CONFIRMED names `product`, `repeat`, `TYPE_COMBINATIONS` in both modules) — the full Cartesian set of category pairs, used to drive the synthesis loop and/or validate dispatch coverage.

---

## 3. Comparison synthesis (`NkiTypeSystemCmpOp`)

Two factories build the comparison handlers; two class decorators install them.

### 3.1 `make_equality_operation` — `==` and `!=`

```c
// make_equality_operation(op, op_name, numpy_op) -> handler(self, a, b)
//   op       = operator.eq / operator.ne
//   op_name  = 'eq' / 'ne'                 (also selects tensor _binop op)
//   numpy_op = np.equal / np.not_equal
// CONFIRMED: closure refs SCALAR/TENSOR/TILE_INDEX, EQTileMask, EQScalarPredicate,
//            _binop, return_tensor_or_extracted_scalar, "Unexpected type "
PyObject *handler(self, a, b) {
    cat_a = get_type_category(a);
    cat_b = get_type_category(b);

    if (cat_a == TENSOR || cat_b == TENSOR) {            // a real tensor is present
        // elementwise tensor compare -> int8 boolean mask tensor
        result = <tensor>._binop(numpy_op_or_op_name, other, /*dtype=*/np.int8);
        return return_tensor_or_extracted_scalar(result);   // scalar-collapse if 0-D
    }
    else if (cat_a == TILE_INDEX || cat_b == TILE_INDEX)
        return EQTileMask(a, b, op);                     // tile-shaped equality mask
    else if (/* SCALAR involved */)
        return EQScalarPredicate(a, b, op);             // scalar equality predicate
    else
        raise TypeError("Unexpected type " ...);        // CONFIRMED .rodata
}
```

### 3.2 `make_comparison_operation` — `<`, `<=`, `>`, `>=`

Identical category dispatch, but the predicate/mask results are the **Intersection** variants, the tile/scalar path fetches `combine_tile_with` as a *method* on the operand, and the affine relation is encoded by `pred_op`:

```c
// make_comparison_operation(op, op_name, numpy_op, pred_op) -> handler(self, a, b)
//   pred_op = pred_lt / pred_le / pred_gt / pred_ge   (affine predicate constructor)
// CONFIRMED: TileMaskIntersection, ScalarPredicateIntersection, combine_tile_with,
//            _binop, return_tensor_or_extracted_scalar, pred_lt/le/gt/ge
PyObject *handler(self, a, b) {
    cat_a = get_type_category(a);
    cat_b = get_type_category(b);

    if (cat_a == TENSOR || cat_b == TENSOR) {
        result = <tensor>._binop(numpy_op_or_op_name, other, /*dtype=*/np.int8);
        return return_tensor_or_extracted_scalar(result);
    }
    else if (cat_a == TILE_INDEX || cat_b == TILE_INDEX)
        return TileMaskIntersection(a.combine_tile_with(b), pred_op);
    else if (/* SCALAR involved */)
        return ScalarPredicateIntersection(a.combine_tile_with(b), pred_op);
    else
        raise TypeError("Unexpected type " ...);
}
```

> **QUIRK — ordered comparisons build an *Intersection*, not a bare leaf.** `i < N` does not return a single `EQTileMask`; it returns a `TileMaskIntersection` wrapping `combine_tile_with` of the two operands and the relation `pred_lt`. This is what makes a chained compare `lo <= x < hi` fold cleanly — every link is already an intersection-shaped mask, and `compare_nary` just `&`-folds them (§5). Equality (`==`/`!=`) takes the simpler `EQTileMask(a,b,op)` leaf path. The split is CONFIRMED by the disjoint result-class strings in the two factories.

### 3.3 Wrappers and decorators

`make_binary_method(op)` wraps a handler into a real `method(self, b)` that re-runs the dispatch on the bound `self`. `make_commutative_binary_method(op)` additionally pulls in `operator.itemgetter` (CONFIRMED — `itemgetter` string present) to order the operand pair by category, so `a OP b` and `b OP a` route identically. The two decorators install them:

```c
// synthesize_equality_comparisons(cls)   (CONFIRMED class decorator)
for ((name, op, numpy_op) in [('eq', operator.eq, np.equal),
                              ('ne', operator.ne, np.not_equal)]) {
    handler = make_equality_operation(op, name, numpy_op);
    setattr(cls, "__" + name + "__", make_binary_method(handler));   // __eq__, __ne__
}

// synthesize_ordered_comparisons(cls)    (CONFIRMED class decorator)
for ((name, op, numpy_op, pred_op) in
        [('lt', operator.lt, np.less,          pred_lt),
         ('le', operator.le, np.less_equal,    pred_le),
         ('gt', operator.gt, np.greater,       pred_gt),
         ('ge', operator.ge, np.greater_equal, pred_ge)]) {
    handler = make_comparison_operation(op, name, numpy_op, pred_op);
    setattr(cls, "__" + name + "__", make_binary_method(handler));   // __lt__ __le__ __gt__ __ge__
}
```

The op-name lists are CONFIRMED: `equal/not_equal/less/less_equal/greater/greater_equal` (the `np` ufunc `.lower()` names) and `eq/ne/lt/le/gt/ge` (dunder stems) all appear in `_strings.json`, alongside `lower` (the `.lower()` call) and the `pred_*` constructors.

---

## 4. Logical synthesis and the promotion lattice (`NkiTypeSystemLogicalOp`)

The logical module is where heterogeneous operands are *unified*. `p & q`, `p | q`, `~p` first compute `highest_category(a,b)`, then **promote both operands to a common logical type** via `PROMOTION_MAP`, then apply the stdlib `operator` over the now-homogeneous predicate/mask objects.

### 4.1 Promotion helpers

```c
// promote_other_to_predicate(a)   (CONFIRMED body)
//   bool / number  ->  a constant predicate
PyObject *promote_other_to_predicate(PyObject *a) {
    if (!PyObject_IsTrue(a)) return AlwaysFalsePredicate();   // falsey -> ⊥
    return AlwaysTruePredicate();                              // truthy -> ⊤
}

// promote_to_logical_type(value, highest_category)   (CONFIRMED body)
PyObject *promote_to_logical_type(PyObject *value, TypeCategory highest_category) {
    if (get_type_category(value) == TENSOR)
        raise(...);                                  // tensors are not promotable here
    promoter = PROMOTION_MAP[highest_category];      // CONFIRMED: PyObject_GetItem(PROMOTION_MAP, …)
    return promoter(value);                          // promote_to_mask / promote_to_predicate
    // failure path emits "Cannot promote from <…> to <…>"   (CONFIRMED .rodata "Cannot promote from ")
}
```

The `PROMOTION_MAP` entries are the module functions `promote_to_mask` and `promote_to_predicate` (both CONFIRMED as names in the pool) plus `promote_other_to_predicate` for the `OTHER` slot. **The exact key→value pairing of `PROMOTION_MAP` is INFERRED** — the dict is constructed at import and only the participating function names and the `GetItem` lookup are byte-confirmable. The structurally-implied map:

| `highest_category` (key) | promoter (value) — **INFERRED pairing** |
|---|---|
| `OTHER` | `promote_other_to_predicate` (bool/number → const predicate) |
| `SCALAR` / `PREDICATE` | `promote_to_predicate` |
| `TILE_INDEX` / `MASK` | `promote_to_mask` |
| `TENSOR` | *(no entry — `promote_to_logical_type` raises before lookup)* |

### 4.2 `make_logical_operation` — `&` and `|`

```c
// make_logical_operation(op, op_name, numpy_op) -> handler(self, a, b)
//   op = operator.and_ / operator.or_ ;  numpy_op = np.logical_and / np.logical_or
// CONFIRMED: highest_category, TENSOR branch, promote_to_logical_type, a_promoted/b_promoted
PyObject *handler(self, a, b) {
    cat = highest_category(a, b);
    if (cat == TENSOR)                               // tensor & tensor -> elementwise int8 mask
        return <tensor>._binop(numpy_op_or_op_name, other, /*dtype=*/np.int8);
    a_promoted = promote_to_logical_type(a, cat);    // both coerced to common logical type
    b_promoted = promote_to_logical_type(b, cat);
    return op(a_promoted, b_promoted);               // predicate/mask  &/|  predicate/mask
}
```

`a_promoted` / `b_promoted` are CONFIRMED local-variable names in the decompiled handler.

### 4.3 `make_logical_not` — `~`

```c
// make_logical_not(op_name) -> handler(self, a)   (unary invert)
PyObject *handler(self, a) {
    cat = highest_category(a, a);
    if (cat == TENSOR)
        raise err_ambiguous_tensor_truth_value(...); // CONFIRMED: inverting a tensor is ambiguous
    a_promoted = promote_to_logical_type(a, cat);
    return operator.invert(a_promoted);              // De Morgan-aware invert on the promoted object
}
```

> **GOTCHA — a tensor has no logical truth value in tracing.** `&` / `|` over two real tensors lower to an elementwise `np.int8` op (a tensor *result*, not a predicate), but `~tensor` and any bool-test of a tensor raise `err_ambiguous_tensor_truth_value` (CONFIRMED string + sema error factory). This mirrors NumPy's "truth value of an array is ambiguous" and is the tracer's way of forcing the user to write an explicit comparison (which *does* produce a predicate) before combining. The asymmetry — binary logical over tensors is allowed but unary invert is not — is intentional: `a & b` has a defined elementwise meaning, `~a` over an int8 tensor would silently mean bitwise-not, which is not a mask negation.

### 4.4 The decorator

```c
// synthesize_logical_operations(cls)   (CONFIRMED class decorator)
for ((name, op, numpy_op) in [('and', operator.and_, np.logical_and),
                              ('or',  operator.or_,  np.logical_or)]) {
    handler = make_logical_operation(op, name, numpy_op);
    method  = make_binary_method(handler);
    setattr(cls, "__" + name + "__", method);     // __and__, __or__
    setattr(cls, "logical_" + name, method);      // cls.logical_and, cls.logical_or   <-- consumed by §5
}
not_handler = make_logical_not('not');
setattr(cls, "__invert__",  make_unary_method(not_handler));   // ~
setattr(cls, "logical_not", make_unary_method(not_handler));
```

This is what gives `NkiTypeSystem` its `logical_and` / `logical_or` (the n-ary folds in §5 call `self.logical_and`), and gives every traced predicate/mask value its `&` / `|` / `~`. `nki_method` decoration (sema integration) is applied at factory time — `nki_method` is CONFIRMED imported in both `CmpOp` and `LogicalOp`.

---

## 5. The tracer: chained comparisons and n-ary folds (`NkiTypeSystem`)

The base `NkiTypeSystem` class (the "TypeTraceContext") is the only module with hand-written method bodies. It holds a single attribute (`self.ctx`, the trace context) and provides the lowerings for Python constructs that the synthesized binary operators cannot express alone: **chained comparisons** and **membership**.

```c
class NkiTypeSystem {                       // decorated by all three synthesize_* (§1)
    void __init__(self, ctx) { self.ctx = ctx; }   // only attribute stored

    // ---- membership:  x in coll  /  x not in coll ----
    PyObject *in_(self, a, b) {
        return operator.contains(a, b);     // CONFIRMED: GetBuiltin 'operator', getattr 'contains'
    }                                        //   -> yields a predicate, not a bool
    PyObject *not_in(self, a, b) {
        return !self.in_(a, b);             // STRONG: body refs in_, PyObject_Not
    }

    // ---- chained comparison:  a OP1 b OP2 c  ->  (a OP1 b) & (b OP2 c) & … ----
    PyObject *compare_nary(self, ops, operands) {
        assert(len(ops) == len(operands) - 1);            // CONFIRMED: two PyObject_Size, v==v+1
        return self.logical_and_nary(                     // AND-fold the per-link results
            (op(a, b)
             for (op, (a, b)) in zip(ops, zip(operands[:-1], operands[1:]))));
        //   operands[:-1] / operands[1:] = pairwise; the genexpr applies each
        //   comparison op to consecutive operand pairs (CONFIRMED: slices, zip, genexpr)
    }

    // ---- n-ary logical reductions (consume the synthesized logical_and/_or) ----
    PyObject *logical_and_nary(self, operands) {
        return functools.reduce(self.logical_and, operands);   // CONFIRMED reduce + self.logical_and
    }
    PyObject *logical_or_nary(self, operands) {
        return functools.reduce(self.logical_or, operands);    // CONFIRMED reduce + self.logical_or
    }
}
```

So `lo <= x < hi`, which Python would normally evaluate as `(lo <= x) and (x < hi)` (short-circuiting to a bool), is instead routed through `compare_nary([le, lt], [lo, x, hi])`, producing `(lo <= x) & (x < hi)` — two `TileMaskIntersection`/`ScalarPredicateIntersection` objects AND-folded by `self.logical_and` into a single combined mask/predicate. The `genexpr` trampoline is CONFIRMED present in the base module (`Pyx_Generator_Next` at `0xbe30`), and `compare_nary` calling `self.logical_and_nary` is the recovered tail of its body.

> **NOTE — why the tracer, not the operators, owns chained compares.** Python evaluates `a < b < c` with built-in short-circuit `and`, which would force a `bool` out of `a < b` before the second comparison runs — defeating the entire predicate machinery. NKI's front-end therefore *rewrites* chained comparisons at trace time (the nisa validators in the W-strand call `compare_nary` when they encounter a chained relation on traced operands) so that each link is evaluated independently to a predicate/mask object and then `&`-folded. A reimplementer must intercept chained comparisons at the AST/trace level — they cannot be recovered from the `__lt__`/`__le__` dunders alone.

---

## 6. End-to-end dispatch summary

Putting the three layers together, here is what each surface-syntax construct resolves to over traced operands:

| Surface syntax | Injected by | Handler | Result (non-tensor) | Result (tensor present) |
|---|---|---|---|---|
| `a == b`, `a != b` | `synthesize_equality_comparisons` | `make_equality_operation` | `EQTileMask` / `EQScalarPredicate` | `tensor._binop(…, int8)` |
| `a < b`, `a <= b`, `a > b`, `a >= b` | `synthesize_ordered_comparisons` | `make_comparison_operation` | `TileMaskIntersection` / `ScalarPredicateIntersection` (via `pred_lt/le/gt/ge`) | `tensor._binop(…, int8)` |
| `a & b`, `a | b` | `synthesize_logical_operations` | `make_logical_operation` | `op(promote(a), promote(b))` → combined predicate/mask | `tensor._binop(logical_*, int8)` |
| `~a` | `synthesize_logical_operations` | `make_logical_not` | `operator.invert(promote(a))` | **raises** `err_ambiguous_tensor_truth_value` |
| `a < b < c …` | `NkiTypeSystem.compare_nary` | n-ary fold | `(a<b) & (b<c) & …` AND-folded | per-link, then folded |
| `x in coll`, `x not in coll` | `NkiTypeSystem.in_` / `not_in` | `operator.contains` | predicate | predicate |
| bool / number operand to `&`/`|`/`~` | promotion | `promote_other_to_predicate` | `AlwaysTruePredicate` / `AlwaysFalsePredicate` | n/a |

The predicate/mask objects produced here flow into the algebra of [§6.2.4 Mask / Predicate Algebra](./mask-predicate-algebra.md), whose `EQTileMask` / `TileMaskIntersection` / `ScalarPredicateIntersection` / `AlwaysTrue*` / `AlwaysFalse*` classes are exactly the result types named above — the two pages describe the *construction* side (this page) and the *combination + lowering to `AffinePredicate`* side (mask-predicate-algebra) of one object graph. The dtype of the tensor-compare result (`np.int8`) connects to the dtype model in [§6.3.3 dtype](./dtype-facade.md); the metaclasses that build `scalar`/`tensor`/`tile_index` (and thus make `get_type_category`'s `isinstance` chain meaningful) are [§6.3.2 metaclasses](./type-metaclasses.md).

---

## 7. Confidence ledger

| Claim | Tag | Evidence |
|---|---|---|
| Comparisons/logicals never yield `bool` | **CONFIRMED** | handlers return `EQTileMask`/`EQScalarPredicate`/`*Intersection`/`tensor._binop`; no `Py_True`/`Py_False` return path; corroborated by §6.2.4 |
| CmpOp `TypeCategory` = `{OTHER,SCALAR,TILE_INDEX,TENSOR}` | **CONFIRMED** | only these four `__pyx_n_s_<MEMBER>` strings in `NkiTypeSystemCmpOp`; no `MASK`/`PREDICATE` |
| LogicalOp `TypeCategory` adds `MASK`,`PREDICATE` | **CONFIRMED** | `__pyx_k_MASK` + `__pyx_n_s_PREDICATE`; `get_type_category` `isinstance` chain probes `nki_mask`→MASK, `predicate`→PREDICATE |
| `get_type_category` `isinstance` order (both modules) | **CONFIRMED** | decompiled bodies (`…_1get_type_categor_0x15cb0`, CmpOp `…_1get_type_category`) |
| `make_equality_operation`/`make_comparison_operation` result-type dispatch | **CONFIRMED** | factory symbols + `EQTileMask`/`EQScalarPredicate`/`TileMaskIntersection`/`ScalarPredicateIntersection`/`_binop`/`combine_tile_with`/`return_tensor_or_extracted_scalar` strings |
| `promote_to_logical_type` refuses TENSOR, indexes `PROMOTION_MAP` | **CONFIRMED** | decompiled body: TENSOR `getattr`+RichCompare, `PyObject_GetItem_Slow(PROMOTION_MAP, …)`, `"Cannot promote from "` |
| `promote_other_to_predicate` → AlwaysTrue/False | **CONFIRMED** | decompiled `PyObject_IsTrue` → `AlwaysFalsePredicate`/`AlwaysTruePredicate` |
| `~tensor` raises ambiguous-truth error | **CONFIRMED** | `err_ambiguous_tensor_truth_value` string in `make_logical_not` |
| `compare_nary` AND-folds pairwise via `logical_and_nary` | **CONFIRMED** (assert+slices+zip+genexpr) / **STRONG** (full body) | base-module symbols + `Pyx_Generator_Next` trampoline |
| `TENSOR` highest in dominance ordering | **STRONG** | declaration order + tensor-branch-first dispatch + TENSOR-refused promotion |
| Exact IntEnum integer values | **INFERRED** | interned at import; only declaration order recoverable |
| `PROMOTION_MAP` / `PROMOTION_TARGET_MAP` key→value pairings | **INFERRED** | dict built at import; only names + participating functions byte-confirmable |

> **CORRECTION — module-relative offsets, not absolute cp310 addresses.** The backing D-W09 report quotes per-method offsets (e.g. `compare_nary @ 0xbf80`, `compare_nary.<locals>.genexpr @ 0x10b70`) that belong to a *specific* Cython output; the cp310 wheel's `NkiTypeSystem.…so` places the generator trampoline at `0xbe30` and does not expose a dedicated `_strings.json` (the base module has only disasm/decompiled sidecars). Cite the **symbols and strings** as anchors, not the absolute offsets. The CmpOp/LogicalOp offsets (`0x15cb0`, `0x16a10`, `0x13750`, `0xfb50`, etc.) *do* match the cp310 decompiled filenames and are reliable for those two modules.
