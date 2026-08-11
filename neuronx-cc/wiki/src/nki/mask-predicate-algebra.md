# Mask / Predicate Algebra

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, cp310 wheel. The front-end algebra lives in `neuronxcc/nki/compiler/backends/neuron/predicates.cpython-310-x86_64-linux-gnu.so` (Cython 3.0.10, ELF with `debug_info`, **not stripped** — every class and method name below is a real symbol). cp311/cp312 are byte-twins. Treat addresses as version-pinned.*

## Abstract

When a NKI kernel writes `mask = (i < N) & (j >= 0)`, no Python `bool` is ever produced. The `<`, `&`, `~`, `|` operators build an *object graph* of mask/predicate nodes, and that graph lowers to a tuple-of-`penguin.ir.AffinePredicate` — the hardware-facing "IndexPredicate", whose own docstring (recovered verbatim from `.rodata`) defines it as a **"logical AND of expressions in form `Index >= 0` or `Index == 0`"**. This page is the algebra and its lowering: the two parallel class hierarchies (tile-shaped `TileMask` vs scalar `ScalarPredicate`), the boolean operators and their absorbing/identity constants, the disjoint-intersection (DNF) engine that lets an `|` become a hardware AND-of-`Index≥0`, the two-level enumeration that emits the `AffinePredicate` tuples, the EQ→GE normalization, and how `affine_select` / `mask=` consume the result.

The module docstring states the whole purpose in one line (`.rodata`): *"Define the subclasses that implement nki mask."*

| | |
|---|---|
| **Module** | `predicates.cpython-310-x86_64-linux-gnu.so` (`neuronxcc/nki/compiler/backends/neuron/predicates.py`) |
| **Abstract base** | `mask` — error `"mask is an abstract class"` |
| **Tile hierarchy** | `TileMask` → `EQTileMask` · `TileMaskIntersection` · `AlwaysTrueMask` · `AlwaysFalseMask` |
| **Scalar hierarchy** | `ScalarPredicate` → `EQScalarPredicate` · `ScalarPredicateUnion` · `ScalarPredicateIntersection` · `AlwaysTruePredicate` · `AlwaysFalsePredicate` |
| **Boolean ops** | `&` → Intersection · <code>&#124;</code> → Union (scalar only) · `~` → `__invert__` (De Morgan on combinators) |
| **Lowering target** | `neuronxcc.starfish.penguin.ir.AffinePredicate` (FQN string in `.rodata`) |
| **Factories** | `pred_ge / pred_gt / pred_le / pred_lt / pred_eq` (defined in `indexing.so`, imported here) |
| **Canonical form** | AND of `Index ≥ 0` / `Index == 0` — *"Should normalize eq predicate to ge predicates"* |

> **The one claim to internalise.** A NKI comparison is **never** a Python truth value. `i < N` evaluates to an `EQTileMask` *object*; `(i<N) & (j>=0)` to a `TileMaskIntersection` *object*. The algebra is a deferred symbolic structure that gets *enumerated* into affine relations at lowering time, not a tree of `True`/`False`. The `==`/`!=` operators are deliberately rejected (`err_mask_not_equal_not_supported` in `.rodata`) precisely because masks are not value-comparable — only `& | ~` are defined on them.

---

## 1. Why there are two hierarchies

The module ships **eleven** Python classes in two structurally-identical families. Both names and bodies are recovered: every class/method below is a `__pyx_mdef_…`/`__pyx_pw_…` symbol, the 174 per-symbol disassembly sidecars under `disasm/…_10predicates_*.asm` carry the bodies, and the docstrings/error strings are in `.rodata`. (The Cython name-mangling convention `__pyx_…_predicates_<N><Name>_<k><method>` encodes `<N>` = byte-length of the class name, so the class set is unambiguous: `8TileMask`, `10EQTileMask`, `20TileMaskIntersection`, `14AlwaysTrueMask`, `15AlwaysFalseMask`, `15ScalarPredicate`, `17EQScalarPredicate`, `20ScalarPredicateUnion`, `27ScalarPredicateIntersection`, `19AlwaysTruePredicate`, `20AlwaysFalsePredicate`.)

```
mask                                  ("mask is an abstract class")
├── TileMask              ── tile-shaped boolean mask over an NDTile (per-element)
│     ├── EQTileMask              leaf:  tile_index >= k  /  == k
│     ├── TileMaskIntersection    &-combinator (logical AND of N tile masks)
│     ├── AlwaysTrueMask          const ⊤   (identity of &, absorbing for |)
│     └── AlwaysFalseMask         const ⊥   (absorbing for &, identity of |)
└── ScalarPredicate       ── scalar boolean predicate (one affine condition, not tile-shaped)
      ├── EQScalarPredicate          leaf:  scalar_index >= k  /  == k
      ├── ScalarPredicateUnion       |-combinator + DNF engine  (the richest class)
      ├── ScalarPredicateIntersection &-combinator
      ├── AlwaysTruePredicate        const ⊤
      └── AlwaysFalsePredicate       const ⊥
```

**Why split tile vs scalar?** A *tile* mask is a predicate evaluated per element of an `NDTile` — it carries tile geometry (shape, the underlying `NDTile`, broadcast/project/align adapters). A *scalar* predicate is a single affine condition on a scalar/affine index with no tile attached. They share an identical *contract* — `{__and__, __or__, __invert__, enumerate_disjoint_intersections, enumerate_intersection_predicates, predicates, is_always_false}` — which is why the lowering machinery is uniform. The reason the user sees both is that the index-side entry points (`TileIndex.__ge__`, …, in `indexing.so`) produce **tile** masks (`EQTileMask`), while internal scalar relations and the DNF engine work on **scalar** predicates; the two meet at `_promote_to_mask` (§5).

> **GOTCHA — `ScalarPredicate`'s base is not byte-confirmable.** Whether `ScalarPredicate` derives from `mask` directly or from a sibling abstract "predicate" base is interned at import and not readable from a fixed C struct (these are ordinary Cython-compiled Python classes, not `cdef class` extension types — there are *no* `__pyx_type_<ClassName>` structs for the eleven classes; the only `__pyx_type_` structs are closure/genexpr scopes). The symmetric method set strongly implies a shared abstract contract, but that is an inference from shape, not a read. Instance attribute names (`m_index`, `nb_index`, `_intersections`, `tmp_predicate_list`) are `.rodata` identifiers whose precise per-class binding is likewise reconstructed rather than read off a slot — treat both as **INFERRED**.

The base `mask` and the abstract index-predicate raise on unimplemented members; these error strings sit in `.rodata`:

```
"enumerate_disjoint_intersections not implemented for base mask"
"enumerate_intersection_predicates not implemented for base mask"
"predicates not implemented for base index predicate"
"shape not implemented for base index predicate"
"tile not implemented for base index predicate"
```

---

## 2. The boolean algebra (`& | ~`)

The operator → class mapping, as fixed by the method symbol set and the dispatch bodies:

| operator | result | builder |
|---|---|---|
| `A & B` | Intersection | `TileMaskIntersection` / `ScalarPredicateIntersection` |
| <code>A &#124; B</code> | Union | `ScalarPredicateUnion` **only** |
| `~A` | per-class `__invert__` | De Morgan on combinators |

### 2.1 `&` constructs a fresh combinator

`TileMask.__and__` does **not** simplify into a tree — it allocates a 2-tuple of operands and constructs a new `TileMaskIntersection`. The dispatch runs a sequence of `PyObject_IsInstance` checks against the operand (catching `AlwaysTrue*`/`AlwaysFalse*`/peer `TileMask`), applies the short-circuit identity if one matches, and otherwise does `PyTuple_New` + `PyObject_Init` on a fresh combinator with `PyObject_GC_Track`. The `&` operator carries a `genexpr` closure (`__pyx_scope_struct____and__`) — the comprehension that flattens/collects operand predicates into the combinator's list.

```c
// TileMask.__and__(self, other)  — IsInstance ×3 → PyTuple_New → PyObject_Init
PyObject* TileMask___and__(self, other) {
    if (isinstance(other, AlwaysTrueMask))  return self;   //  x & ⊤  = x
    if (isinstance(other, AlwaysFalseMask)) return other;  //  x & ⊥  = ⊥
    // collect operand predicate lists (genexpr) and build the AND combinator
    return TileMaskIntersection( (self, other) );          //  fresh node
}
```

`ScalarPredicate` additionally defines `__rand__` (`_5`) and `__ror__` (`_7`) so that a non-predicate left operand (`pyconst & pred`) routes through the reflected operator instead of raising.

### 2.2 `|` is scalar-only — the most important asymmetry

There is **no `TileMaskUnion` class**. The only union class in the entire module is `ScalarPredicateUnion`, plus the `union_repr` helper. This is structural, not an oversight: an OR *cannot* be represented as a single tile-shaped AND-term, and the disjoint-intersection (DNF) machinery that makes OR lowerable lives entirely on the scalar side.

`TileMask.__or__` therefore does **not** instantiate a tile object. Its body performs the `AlwaysTrue*`/`AlwaysFalse*` short-circuits, then **delegates** via type-slot calls + `PyObject_GetAttrStr` + `PyObject_FastCallDict` on the operands rather than building a fixed class. `ScalarPredicate.__or__` (`_15`) does the `IsInstance`/`IsTrue` dispatch and then builds a `ScalarPredicateUnion` through the `create` smart constructor (`_5`, §3.2).

> **QUIRK — tile `|` promotes to scalar.** `(i < N) | (i >= M)` on tile masks does not yield a tile union; it resolves to the *scalar* `ScalarPredicateUnion` representation, where the DNF engine can split the OR into disjoint AND-regions and only those regions become affine predicates. `TileMask.__or__` demonstrably delegates rather than constructing, and no `TileMaskUnion` class exists; that the delegation target is specifically `ScalarPredicateUnion` is **INFERRED** from it being the only union class in the module.

### 2.3 The lattice constants (⊤ / ⊥)

`AlwaysTrue*` and `AlwaysFalse*` are the 1 and 0 of a bounded lattice. Each one overrides `__and__`/`__or__`/`__invert__` to encode the absorbing/identity laws directly, which is what lets `create`/`normalize` collapse trivial terms (an intersection containing ⊥ → ⊥; a union containing ⊤ → ⊤):

```
AlwaysTrueMask   = ⊤ :   ⊤ & x = x        ⊤ | x = ⊤        ~⊤ = ⊥  (→ AlwaysFalseMask)
AlwaysFalseMask  = ⊥ :   ⊥ & x = ⊥        ⊥ | x = x        ~⊥ = ⊤  (→ AlwaysTrueMask)
```

(The same four identities hold for `AlwaysTruePredicate`/`AlwaysFalsePredicate`.) Each constant class carries exactly the `__init__ · __and__ · __or__ · __invert__` method set, and each body is short enough to be a single return or a 0-arg construction of the dual class. Which identity each individual method encodes follows from that shape plus standard lattice semantics rather than from a decoded body.

### 2.4 `~` and De Morgan

Negation is per-class. The leaves (`EQTileMask.__invert__` / `EQScalarPredicate.__invert__`) flip the relation to its complement. The combinators apply De Morgan:

```
~(A & B)  =  ~A | ~B      (Intersection.__invert__ → a Union of inverted operands)
~(A | B)  =  ~A & ~B      (Union.__invert__       → an Intersection of inverted operands)
```

Each combinator owns its own `__invert__`: `TileMaskIntersection` (`_17`), `ScalarPredicateUnion` (`_14`), `ScalarPredicateIntersection` (`_16`). The DNF of a negation has its own dedicated path — `enumerate_invert_disjoint_intersections`, present on `ScalarPredicateUnion` (`_12`) and `ScalarPredicateIntersection` (`_13`) — backed by the helper `logical_not_disjoint_intersections` (a `.rodata` string). This matters because `~` interacts with the disjoint-intersection enumeration (§4): you cannot just invert each affine relation, you must re-DNF the result.

---

## 3. The disjoint-intersection (DNF) engine

The hardware predicate is an **AND of `Index ≥ 0`** relations. A disjunction has no such form. So an OR must be rewritten as a *sum of products* — a set of **mutually-exclusive** AND-terms (a DNF) whose union equals the original predicate. That rewriting is the entire reason `ScalarPredicateUnion` is the richest class in the module.

### 3.1 The two-level enumeration protocol

Two methods, present on every node class, form the contract — the method names appear across the symbol table and `.rodata` carries their exact type annotations:

```
enumerate_intersection_predicates(self) -> Iterable[AffinePredicate]
    # the AND-conjuncts of ONE intersection term (one "product").

enumerate_disjoint_intersections(self)  -> Iterable[<Intersection>]
    # the set of MUTUALLY-EXCLUSIVE AND-terms whose union == self (the DNF).
```

The recovered annotation strings make the nesting explicit (`.rodata`):
`Iterable[AffinePredicate]`, `Iterable[Tuple[AffinePredicate, ...]]`, `Tuple[Tuple[AffinePredicate, ...], ...]`, `Iterable[TileMaskIntersection]`, `Iterable[ScalarPredicateIntersection]`.

Per node type:

- **Leaf `EQ*`** — `enumerate_disjoint_intersections` yields a *single* term (itself); `enumerate_intersection_predicates` yields that term's affine relation(s) via the factories.
- **Intersection** — distributes: the disjoint terms of an AND are the cross-combination of the operands' disjoint terms, intersected; `normalize_intersection` (`_14`/`_10`) canonicalizes the AND set.
- **Union** — the cross product of the operands' disjoint terms.

### 3.2 `ScalarPredicateUnion` — the cross product

`ScalarPredicateUnion.enumerate_disjoint_intersections` (`_10`) is built on **`itertools.product`** over the operands' disjoint terms — `product` is interned in 3 module symbols and `is_disjoint`/`_is_disjoint` in 7. Each product tuple is recombined by an inner closure **`build_intersection`** — recovered with its fully-qualified name in `.rodata`:
`neuronxcc.nki.compiler.backends.neuron.predicates.ScalarPredicateUnion.enumerate_disjoint_intersections.build_intersection` (and the `<locals>` form). The actual cross-product worker is `enumerate_disjoint_intersections_impl` (`_7`); the public method calls it and the disjointness predicate (`is_disjoint`) filters/partitions the products so the emitted AND-terms genuinely don't overlap.

```c
// ScalarPredicateUnion.enumerate_disjoint_intersections(self)  — itertools.product + build_intersection
Iterable<Intersection> enumerate_disjoint_intersections(self) {
    // for a union of operands o0, o1, ... each contributing its own disjoint AND-terms,
    // the disjoint terms of the union are the disjoint cross-products:
    for (combo in itertools_product(o0.enumerate_disjoint_intersections(),
                                    o1.enumerate_disjoint_intersections(), ...)) {
        term = build_intersection(combo);     // inner closure: AND the picked terms
        if (is_disjoint(term, already_emitted))   // keep terms mutually-exclusive
            yield term;
    }
}
```

The other `ScalarPredicateUnion` methods round out the engine:
`create` (`_5`) is the smart constructor (flatten nested unions, short-circuit `AlwaysTrue*`/`AlwaysFalse*`, simplify); `logical_and_union` (`_16`, with a `genexpr` closure) distributes an AND across a union (`(A|B) & C → (A&C)|(B&C)`); `enumerate_invert_disjoint_intersections` (`_12`) produces the DNF of `~self`.

> **NOTE — why DNF and not CNF.** The target form is an AND of relations *per term*; alternative terms are disjoint. That is exactly DNF (sum of products), not CNF. The "sum" is realised by *emitting multiple `Tuple[AffinePredicate,...]`* — one per disjoint region — and the downstream select consumer ORs the regions implicitly by writing under whichever region's AND-mask matches. The disjointness invariant (`is_disjoint`) guarantees no element is written twice.

---

## 4. Lowering: `predicates` → `AffinePredicate` tuples

The `predicates` property is the bridge out of the algebra. It flattens the two-level enumeration into the `Tuple[Tuple[AffinePredicate, ...], ...]` handed to Penguin:

```c
// <node>.predicates  (property)  — flatten(disjoint terms → each term's conjuncts)
Tuple<Tuple<AffinePredicate,...>,...> predicates(self) {
    out = ();
    for (term in self.enumerate_disjoint_intersections())          // the DNF (§3)
        out += ( tuple(term.enumerate_intersection_predicates()), ); // each term → AND of AffinePredicate
    return out;
}
```

### 4.1 The leaf build — `_build_index_predicate`

The actual `AffinePredicate` construction is done by `_build_index_predicate` on the two leaf classes — `EQTileMask._build_index_predicate` (`_3`, `@0x43140`) and `EQScalarPredicate._build_index_predicate` (`_3`). The body binds the index variables to the tile's actual coordinates and calls the affine-predicate factories. The EQTileMask `predicates()` generator (`…_10EQTileMask_6generator…`) references the interned names `pred_eq`, `pred_gt`, `pred_lt`, `eq`, `lhs`, `rhs`, `tile`, `combine_tile_with`, `is_always_false`, and constructs a `TileMaskIntersection`.

The factories themselves are **defined in `indexing.so`** and imported here — `__pyx_n_s_pred_ge`/`__pyx_n_s_pred_eq` are interned, `__pyx_k_pred_ge` is in `.rodata`, and `pred_ge` is referenced from 6 module symbols and `pred_eq` from 3. They take a `penguin.ir.AffineExpr` (built from the `TileIndex`'s affine index via `substitute_expr_indices` / `generate_subst_map`) and emit a `penguin.ir.AffinePredicate`:

| factory | builds | meaning |
|---|---|---|
| `pred_ge(lhs, rhs)` | `AffinePredicate(lhs - rhs,     ge=True)` | `lhs − rhs ≥ 0` |
| `pred_le(lhs, rhs)` | `AffinePredicate(rhs - lhs,     ge=True)` | `rhs − lhs ≥ 0` (operands swapped) |
| `pred_gt(lhs, rhs)` | `AffinePredicate(lhs - rhs - 1, ge=True)` | `lhs − rhs ≥ 1` (strict, `−1`) |
| `pred_lt(lhs, rhs)` | `AffinePredicate(rhs - lhs - 1, ge=True)` | `rhs − lhs ≥ 1` (strict, swap + `−1`) |
| `pred_eq(lhs, rhs)` | `AffinePredicate(rhs - lhs,     ge=False)` | `rhs − lhs == 0` (equality) |

This is the exact convention pinned in **[5.21 affine-isl-pelican-bridge](../penguin/affine-isl-pelican-bridge.md)**: there is exactly *one* internal comparison direction — `ge` — a boolean keyword on `AffinePredicate.__init__`. `ge=True` → `e ≥ 0` (an `ICmp(SGE, e, 0)`); `ge=False` → `e == 0` (an `ICmp(EQ, e, 0)`). The four inequality verbs all pass `ge=True`; **`pred_eq` is the lone `ge=False`**. The strict verbs (`gt`/`lt`) carry the integer `−1` (Presburger strictening), the swap verbs (`le`/`lt`) negate the difference. Keeping this page consistent with 5.21: *no `CmpPred` other than `SGE` and `EQ` is reachable from the predicate layer.*

### 4.2 EQ → GE normalization

The base index-predicate docstring fixes the canonical form (`.rodata`): *"IndexPredicate - logical AND of expressions in form `Index >= 0` or `Index == 0`"*. But some downstream consumers accept *only* GE relations. So an `== 0` is, where required, rewritten as the conjunction `expr ≥ 0 AND −expr ≥ 0`. The intent is recorded verbatim as a `.rodata` string: **"Should normalize eq predicate to ge predicates"**. `_build_index_predicate` is the method that performs this leaf-level build + normalization.

```c
// EQ → GE:  (expr == 0)  ≡  (expr >= 0) AND (-expr >= 0)     — "Should normalize eq predicate to ge predicates"
normalize_eq_to_ge(expr):
    return ( pred_ge(expr, 0), pred_ge(neg(expr), 0) );   // two SGE relations, pure AND
```

> **GOTCHA —** every `pred_*` factory is **binary**: `pred_ge(lhs, rhs) → AffinePredicate(lhs - rhs, ge=True)`. The index-predicate lowering always passes `rhs = 0`, so call sites read like unary `pred_ge(expr)` constructors — that is the call shape, not the signature.

### 4.3 Tile-geometry adapters

Because two masks over *different* `NDTile`s must be brought onto a common index frame before they can be intersected or lowered, `TileMask` carries four adapters, several with `genexpr` closures:

- `broadcast_tile` (`_21`) — broadcast a mask's tile to a target shape (`broadcast_shapes`).
- `project_tile` (`_23`) — project the mask onto a subset of dims.
- `align_tile` (`_25`) — bring two masks onto a shared index frame (`combine_tile_with`; `target_tile`/`other_tile`/`new_mask` intermediates).
- `as_tile` (`_19`) — materialize the underlying `NDTile`.

`align_tile` is what makes `(i<N) & (j>=0)` over different tile axes intersectable: it produces the `self_subst_map`/`other_subst_map` that re-express both operands' indices over one frame, then `substitute_expr_indices` binds them.

---

## 5. `_promote_to_mask` — the scalar→tile bridge

The masked-tile path (`mask(tensor)`) needs a *tile*-shaped mask to attach to a tile op. `_promote_to_mask` lifts a scalar predicate into a tile mask. It is defined on `ScalarPredicate` (`_19`), `AlwaysTruePredicate` (`_9`), and `AlwaysFalsePredicate` (`_9`).

```
ScalarPredicate._promote_to_mask        →  wrap index/relation as EQTileMask (or a
                                           TileMaskIntersection of them) carrying the op's NDTile.   [INFERRED body; it does construct a mask]
AlwaysTruePredicate._promote_to_mask     →  AlwaysTrueMask()      [0-arg ctor of the dual constant]
AlwaysFalsePredicate._promote_to_mask    →  AlwaysFalseMask()
indexing.TileIndex._promote_to_mask      →  EQTileMask directly from `tile_index <rel> k`  [symbol in indexing.so]
```

So the end-to-end front-end chain for `mask = (i < N) & (j >= 0)`:

```
i < N            →  TileIndex.__lt__  →  EQTileMask                 [indexing.so]
(i<N) & (j>=0)   →  TileMask.__and__  →  TileMaskIntersection       [this module]
attach via mask= →  _promote_to_mask  →  already a TileMask (identity / wrap)
mask.predicates  →  enumerate disjoint intersections → each as Tuple[AffinePredicate,...] via pred_ge/pred_eq (EQ→GE)
                 →  penguin.ir.AffinePredicate
```

> **NOTE — mutual recursion with `indexing.so`.** `TileIndex.__ge__`/`__gt__`/… in `indexing.so` *construct* `EQTileMask` + `TileMaskIntersection` from *this* module, while this module imports `TileIndex`/`NDTile`/`pred_ge`/`generate_subst_map`/… *from* `indexing.so`. The two are mutually recursive at the class level, so the import is lazy / function-local on at least one side. (The factories `pred_ge … pred_lt`, `equals`, `substitute_expr_indices`, `generate_subst_map`, `broadcast_shapes`, `combine_tile_with`, `index_type_name`, `NDTile`, `DynamicScalar` are *defined* in `indexing.so` — it is the authoritative home; `predicates.so` imports them.)

---

## 6. Consumption: `affine_select` and `mask=`

The lowered `AffinePredicate` tuples feed two distinct backend realisations, and which one fires depends on whether the predicate guards a *region* or a *write*. The lowering is in `NeuronCodegen`, which lives in `KernelBuilder.cpython-310-*.so` (§6.2).

### 6.1 `affine_select` — single-predicate masked write

`NeuronCodegen.affine_select` (`_183`, `@0x18cf70`) emits a masked element-level write. It requires **exactly one** affine predicate and asserts otherwise; the guard string in `KernelBuilder.so` is `"'affine_select' only supports single predicate, was provided <N>"`. It builds an `AffSelTensorScalarOp` (interned as `__pyx_k_AffSelTensorScalarOp`) — a TensorScalar variant whose write is masked by the affine predicate: `out = predicate ? on_true(src) : on_false/fill_value`. The compare opcode comes from the predicate's relation (`is_ge → GE`). This is the primitive behind the attention causal mask (`affine_select` with `iota q_pos ≥ k_pos → _FLOAT32_MIN`). See **[6.5.5 affine_select](../nki/neuroncodegen-control.md)** for the op-emit detail.

### 6.2 The predicate's two BIR fates

`opcode_for_predicate` (`NeuronCodegen._185`, `@0x7c7d0`) maps a predicate's comparison kind to a numpy-ufunc compare opcode used at trace time: `if pred.is_ge: return np.greater_equal` (GE) else `np.equal` (EQ), plus `np.less`/`np.greater` for the remaining kinds. These ufunc codes are the trace-time stand-ins for the BIR compare codes. The **same** Python predicate object then has two realisations:

- **Region guard** (`if_scope`/`while_scope` — a control-flow branch) → BIR `CmpBranch` (sequencer branch).
- **Masked write** (`affine_select`/`tensor_copy_predicated` — a datapath select) → BIR `CopyPredicated` (`InstCopyPredicated`, IT 52).

`lower_predicates` (`_191`) dispatches: one predicate → `lower_simple_predicate` (`_187`, a TensorScalar over a `TileIndex`/`index_value_inst` + the `opcode_for_predicate` compare → an `int32` mask tile); multiple → `lower_compound_predicates` (`_189`, an `np.logical_and` AND-fold). This is the *region-guard* lowering — distinct from the `mask.predicates` algebra of §4, which is the *front-end* lowering. See **[6.3.1 type-system predicates](../nki/type-system.md)** for the type-level view.

All of this predicate and select lowering lives in `KernelBuilder.NeuronCodegen.*`, under `nki/compiler/backends/neuron`. The similarly-named `starfish/penguin/targets/codegen/NkiCodegen.so` runs the *opposite* direction: it is a BIR→NKI-Python source-text printer (`write_line`/`quote`/`begin_loop`/`simple_predicates`, entry points `ir_to_nki`/`nki_to_nki`), a debug round-tripper for reading IR back as kernel source.

> **GOTCHA —** `NkiCodegen.so` is not the NKI→Penguin lowering path despite the name. Looking there for the forward lowering finds a printer.

### 6.3 End-to-end

```
nisa.affine_select(pred, ...) / range_select(...)               [front door, 6.5.5]
  pred from TileIndex comparisons:
     i < N           → EQTileMask              (relation LT over tile i)   [indexing.so]
     (i<N)&(j>=0)    → TileMaskIntersection                               [this module §2]
     (i<N)|(i>=M)    → ScalarPredicateUnion → DNF                         [this module §3]
  ── _promote_to_mask : ensure TileMask                                   [§5]
  ── mask.predicates  : enumerate disjoint intersections, each a
        Tuple[AffinePredicate,...] via pred_ge/pred_eq (EQ→GE norm)       [§4]
  ── penguin.ir.AffinePredicate  (per-Inst affine guard)                  [5.21]
  ── Penguin passes: SimplifyPredicates / ResolvePredicates /
        RelaxPredicates / ResolveTongaMacroPredicates                     [other .so]
  ── tonga select-mask : the lane-select bytes                            (wire side)
```

(The downstream Penguin passes are separate, IDA-decompiled modules — `starfish.penguin.transforms.{Resolve,Simplify,Relax,ResolveComplicate}Predicates`, `targets.tonga.ResolveTongaMacroPredicates` — present in the corpus as `ida/…__SimplifyPredicates.cpython-310…` etc. They *consume* the lowered `AffinePredicate`s this front-end algebra produces.)

---

## 7. Evidence summary

The five central claims and what pins them:

1. **"Comparisons never produce bools; the algebra is an object graph."** The operator methods (`__and__`, `__or__`, `__invert__`) are real `__pyx_mdef`/`__pyx_pw` symbols that allocate objects (`PyTuple_New` + `PyObject_Init` traced in `TileMask.__and__`); `err_mask_not_equal_not_supported` exists precisely because masks are not value-comparable. No code path returns a `Py_True`/`Py_False` from a comparison.

2. **"Two parallel hierarchies, tile vs scalar, eleven classes."** All eleven class names appear in the symbol table with the Cython length-prefix mangling (`8TileMask`…`27ScalarPredicateIntersection`); the per-method roster (`fd '_10predicates_' disasm` → 80+ unique class/method symbols) matches §1 exactly.

3. **"OR uses `itertools.product` + `build_intersection` to produce a disjoint DNF."** `product` interned in 3 symbols; `is_disjoint`/`_is_disjoint` in 7; the inner closure's fully-qualified name `…ScalarPredicateUnion.enumerate_disjoint_intersections.build_intersection` is a `.rodata` string; `enumerate_disjoint_intersections_impl` and `enumerate_invert_disjoint_intersections` are real symbols.

4. **"EQ→GE normalization, `ge=True`→`e≥0`, `ge=False`→`e==0`, `pred_eq` the lone `ge=False`."** Pinned by the strings (`"Should normalize eq predicate to ge predicates"`, the IndexPredicate docstring) and by the factory table cross-checked against 5.21's register-traced `pred_*` table, where only `pred_eq` carries `ge=False`. The `−1` strictening arithmetic for `pred_gt`/`pred_lt` is the standard integer-affine rewrite and is pinned in 5.21 rather than here.

5. **"`affine_select` requires exactly one predicate → `AffSelTensorScalarOp`."** `'affine_select' only supports single predicate, was provided` and `AffSelTensorScalarOp` are both `.rodata` strings in `KernelBuilder.so`; `opcode_for_predicate` and the `is_ge → np.greater_equal` / `np.equal` mapping are `NeuronCodegen` symbols.

What is *not* directly byte-readable is flagged INFERRED in place: Cython-merged statement order, instance-attribute slot bindings, and the base class of `ScalarPredicate`.
