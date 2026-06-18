# Tile / Tensor Abstract Data Model

> *All symbols, docstrings and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The model lives in one Cython module: `neuronxcc/nki/compiler/backends/neuron/tensor.cpython-310-x86_64-linux-gnu.so` (1.70 MB, ELF x86-64, **not stripped**, `with debug_info`, Cython 3.0.10, `BuildID[sha1]=a891a5a1e9df2405124efc49e052d640ec6ea906`). The cp311/cp312 wheels carry the same classes under their own BuildIDs. For `.text`/`.rodata` the virtual address equals the file offset. Treat every address as version-pinned.*

## Abstract

A NKI kernel never touches a hardware buffer directly. Everything it manipulates is a Python object from one four-class hierarchy: **`tensor`** (the abstract root — a multidimensional homogeneous array), **`tile`** (a `tensor` subclass whose *partition dimension is the highest dimension*), **`tile_index`** (a `tile` subclass that carries affine index values), and **`mask`** (a `tensor` subclass that is a boolean predicate gating a tile op). This page recovers that hierarchy from `tensor.so`, the contract each class promises, and the four mechanisms that make the abstraction work: the **`as_tile()` chokepoint** that materializes the implicit tensor→tile conversion (and thereby asserts the partition-dim contract), the **`__getitem__` value-vs-mask split**, the **operator-overload dispatch** that routes every `+`/`<`/`@` through `as_tile()._binop(np.<ufunc>, …)`, and the **store-only-lvalue rule** — `tile[...] = value` is a *hard error*; writes go through an explicit store op, and the only legal lvalue path is `tile_assignment(a, b) → a.update_lvalue(b)`.

The structural surprise, recovered and CONFIRMED below, is that **this module is almost entirely abstract**. All four classes are *pure-Python* classes (no `cdef` extension types, so no recoverable `__slots__`/field offsets), built with `tensor_type` as their metaclass. Nearly every leaf method `raise`s `NotImplementedError` with a `"... not implemented for base <class>"` message; the concrete tile is `NeuronSBTensor` (an SBUF tensor) defined in the sibling `tensors.so` ([6.2.2](memref-view-model.md)). `tensor.py` is the **interface contract** for the whole NKI value model — the set of operator overloads, the conversion rule, and the lvalue ban — with the real arithmetic, indexing, and predicate math deferred to siblings.

For reimplementation, the contract is:

- The `tensor > tile > tile_index` / `tensor > mask` hierarchy and its metaclass, with `tile_index` being the index-carrying tile and `mask` deriving from `tensor` (**not** `tile`).
- The partition-dim-is-highest-dim rule and its single enforcement chokepoint, `as_tile()`, called at the top of every arithmetic/compare/matmul/index path.
- `__getitem__`'s two branches (mask/predicate → masked view, else → `_index_tensor`) and `__setitem__`'s unconditional raise.
- The operator dispatch table (which numpy ufunc each dunder binds), the `tile` vs `tile_index` add-split, and the `_build_inplace_op` closure factory.
- `tile_assignment → update_lvalue` as the SSA-style reified-store lvalue path.

| | |
|---|---|
| **Module** | `nki/compiler/backends/neuron/tensor.cpython-310-…so` (Cython 3.0.10) |
| **Metaclass** | `tensor_type` (from `.metaclasses`) — drives all 4 classes |
| **Classes** | `tensor` (root) ▸ `tile(tensor)` ▸ `tile_index(tile)` ; `mask(tensor)` |
| **Concrete tile** | `NeuronSBTensor` (imported from `tensors.so`, [6.2.2](memref-view-model.md)) — SBUF |
| **Conversion chokepoint** | `tensor.as_tile()` (abstract here: `"as_tile not implemented for base tensor"`) |
| **Lvalue ban** | `tensor.__setitem__` always raises `"<cls> cannot be directly assigned to a tensor, use store operation instead."` |
| **Lvalue path** | `tile_assignment(a,b)` @ `0x36390` → `a.update_lvalue(b)` |
| **Partition aligner** | `match_par_dim(x, y)` @ `0x308c0` |
| **In-place factory** | `_build_inplace_op` @ `0x283c0` → inner `_inplace_op` @ `0x43290` |
| **Index dispatcher** | `tensor.__getitem__` @ `0x31b00` |

---

## 1. The four-class hierarchy

### Purpose

The hierarchy encodes one idea: a *value* in a NKI kernel is a `tensor`, but only a `tile` — a tensor whose partition (leading) axis is already in the privileged "highest dimension" slot — can be fed to the Trainium engines. The class tree makes that distinction a *type*, and the abstract base makes the conversion rule (§2), the indexing semantics (§3), and the operator surface (§5) into a single contract every concrete buffer class must implement.

### Construction

All four classes are ordinary Python classes built at module-exec time with the Cython `_Pyx_Py3MetaclassPrepare` + `_Pyx_Py3ClassCreate` pair — i.e. the equivalent of `class X(Base, metaclass=tensor_type): …`. There are **no** `cdef` extension-type structs for `tensor`/`tile`/`tile_index`/`mask` (CONFIRMED: the only `__pyx_type_*` struct in the binary is the `_build_inplace_op` closure scope). Attribute storage is therefore dict-based; **no field offsets or `__slots__` are recoverable**, by design — they live on the concrete `NeuronSBTensor`.

```text
class tensor(metaclass=tensor_type):   # bases = ()       — abstract root
class tile(tensor):                    # base  = tensor   — partition-dim-highest subtensor
class tile_index(tile):                # base  = tile     — carries affine index values
class mask(tensor):                    # base  = tensor   — boolean predicate (NOT a tile!)
```

> **NOTE — `mask` is a `tensor`, not a `tile`.** Recovered class qualnames confirm `mask` derives from `tensor`: the `mask` ClassCreate builds its bases tuple from the `tensor` global, not `tile`. This matters because a `mask` is a *predicate over* a tile region, not a value you do arithmetic on — its operator surface is the boolean algebra `& | ~` (§6), not the elementwise ufunc set.

### Docstrings (CONFIRMED — verbatim from `.rodata`)

The three class docstrings are the most precise statement of the data model and are present byte-for-byte in the binary:

- `tensor`: *"A tensor object represents a multidimensional, homogeneous array of fixed-size items"*
- `tile`: *"A tile object represents a subtensor whose partition dimension is the highest dimension"*
- `tile_index` (module note): *"Indices, like the values produced by (affine expression of) arange, are also a tile"*
- `mask`: *"mask is an abstract class"*

The `tile_index` note is the key to §5.4: an index expression (an `arange`-derived affine value) is *itself a tile*, which is why arithmetic on it composes into a dynamic access pattern rather than a numeric add.

### Source line map (CONFIRMED — recovered from per-method `_Pyx_TraceSetupAndCall("tensor.py", <lineno>)`)

The recovered line-number table gives the authoritative class layout. `[abstract]` = body is a single `raise NotImplementedError("… not implemented for base <cls>")`; the matching message string is present in `.rodata` for every one of them.

```text
class tensor (root, lines ~39..446):
   39 shape [abstract]   56 ndim [abstract]   63 dtype [abstract]   70 buffer [abstract]
   46 assert_shape [CONCRETE]          77 itemsize [CONCRETE, = dtype.itemsize]
   84 __getitem__ [CONCRETE]          101 _index_tensor [abstract]   104 __setitem__ [CONCRETE→raises]
  114 __add__ … 244 __xor__/__rxor__  248 __lt__ 252 __le__ 256 __gt__ 260 __ge__   [CONCRETE]
  264 __matmul__ [CONCRETE]           268 __lshift__ 272 __rshift__ 276 __mod__     [CONCRETE]
  292 __iadd__ 297 __imul__ 302 __isub__ 307 __itruediv__ 312 __invert__  [→ _build_inplace_op / _unary_op]
  316 reshape [abstract]  325 expand_dims [abstract]  334 broadcast_to [CONCRETE]
  352 astype [CONCRETE]   365 view [CONCRETE]  377 _view_impl [abstract]
  380 _compute_new_shape_for_view [CONCRETE — dtype-resize byte math]
  410 base [abstract]     420 as_tile [abstract]   423 as_tensor [abstract]
  426 tensor_ir_class [abstract]  431 _matmul [abstract]  434 _binop [abstract]
  440 _iop_tile_impl [abstract]  443 _iop_number_impl [abstract]  446 _create_instance [abstract]
module fns: 464 _build_inplace_op [CONCRETE]   465 _inplace_op [CONCRETE inner]
            604 tile_assignment [CONCRETE]      609 match_par_dim [CONCRETE]

class tile(tensor), lines ~486..502:
  486 mask_tensor [abstract]   489 _astype_impl / _broadcast_to_impl [abstract]
  495 _build_dynamic_index [abstract]  498 _add_tile_or_number [CONCRETE]  502 _radd_tile_or_number [CONCRETE]
  update_lvalue [abstract: "update_lvalue not implemented for base tile"]

class mask(tensor), lines ~514..601:
  514 dtype [abstract]  518 is_scalar [abstract]  522 tensor_ir_class [abstract]
  526 _index_tensor [abstract]  529 __setitem__ [abstract]  532 combine_tile_with [abstract]
  535 _promote_to_mask [abstract]   578 enumerate_disjoint_intersections [abstract]
  581 enumerate_intersection_predicates [abstract]  584 is_always_false [abstract]
  588 __and__ [abstract]  591 __rand__ [CONCRETE→self.__and__]  594 __or__ [abstract]
  597 __ror__ [CONCRETE→self.__or__]  600 __invert__ [abstract]

class tile_index(tile), overrides ~535/560..575:
  535 _promote_to_mask [CONCRETE → raises "not supported for tile_index"]
  532 combine_tile_with [abstract]   560 dtype  564 is_scalar  568 tensor_ir_class
  572 _index_tensor  575 __setitem__  [all abstract]
```

---

## 2. The partition-dim contract and the `as_tile()` chokepoint

### The implicit-conversion rule

The strongest statement of the data model lives in `tensor.broadcast_to`'s docstring (CONFIRMED verbatim):

> *"Broadcast tensor to a new shape based on numpy broadcast rules. The tensor object must be a tile or can be implicitly converted to a tile. **A tensor can be implicitly converted to a tile iff the partition dimension is the highest dimension.**"*

This is the privilege rule of NKI's SBUF layout: the partition dimension (dim 0, mapped to the 128 SBUF partitions — [3.x SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md)) must be the leading axis. A bare `tensor` whose partition axis is *not* leading is not a legal tile and cannot enter an engine op; the conversion is illegal and the concrete `as_tile()` rejects it.

### `as_tile()` is the single enforcement point

```c
// tensor.as_tile() — abstract in base (line 420).
//   base body: raise NotImplementedError("as_tile not implemented for base tensor")
//   concrete  : NeuronSBTensor.as_tile() asserts partition-dim==highest, else errors.
// EVERY arithmetic / compare / matmul / index-mask path opens by calling operand.as_tile():
//   __add__@124, __lt__, __matmul__@264, _inplace_op@466, __getitem__@94, …
PyObject *as_tile(self);   // n_s_as_tile getattr at the top of each op body
```

Because *every* value-producing path begins by calling `operand.as_tile()`, this one method is the choke point that materializes the implicit tensor→tile conversion **and** asserts the partition-dim contract. There is no second path into an engine op that bypasses it — a reimplementer who wants the contract enforced once need only enforce it here.

> **GOTCHA — the rule is asserted by the *sibling*, not by `tensor.so`.** The base `as_tile` is a stub; the actual partition-dim check lives in the concrete `NeuronSBTensor.as_tile` in `tensors.so` ([6.2.2](memref-view-model.md)). `tensor.so` only *declares* that every op routes through `as_tile`; do not look for the geometry assertion in this module — it is the contract, not the implementation.

The reverse view, `tensor.as_tensor()` (line 423, abstract: `"as_tensor not implemented for base tensor"`), demotes a tile back to a plain tensor.

---

## 3. Indexing — `__getitem__` / `__setitem__` / the store-only-lvalue rule

### 3.1 `__getitem__` — value vs mask split (line 84, CONCRETE, STRONG)

`tensor.__getitem__` @ `0x31b00` branches on whether the index is a boolean predicate:

```c
// tensor.__getitem__(self, indices)                          # line 84
PyObject *__getitem__(self, indices) {
    // from .predicates import predicate                       (ImportFrom)
    if (isinstance(indices, (mask, predicate))) {              // ~88 two PyObject_IsInstance
        PyObject *t = self->as_tile();                         // 94  n_s_as_tile
        if (!isinstance(t, mask))                              // 95
            indices = indices->_promote_to_mask(t);            // 96  n_s_promote_to_mask — ON THE INDEX
        return t->mask_tensor(indices);                        // 97  n_s_mask_tensor
    }
    return self->_index_tensor(indices);                       // 99  n_s_index_tensor
}
```

Two semantics:

- **Predicate / mask indexing** (`tile[mask]` or `tile[predicate]`): the receiver is converted to a tile, a raw `predicate` is first *lifted to a mask* via `predicate._promote_to_mask(t)`, and a masked view is produced by `tile.mask_tensor(mask)`. **The `_promote_to_mask` call is on the index object, not the receiver** (STRONG — the getattr order in the decompiled body; the line-96 path is taken only when the index is not already a `mask`).
- **Value indexing** (affine / `arange` / int slices): everything else dispatches to `self._index_tensor(indices)` (abstract here; concrete in `NeuronSBTensor` and `indexing.so` [6.2.3](index-mask-inference.md)), producing a sub-tile / strided view. Because "indices are also a tile," an `arange`-derived index expression is itself a `tile_index`.

### 3.2 `__setitem__` is a hard error — the store-only-lvalue rule (line 104, CONCRETE, CONFIRMED)

Direct assignment into a tensor is **banned unconditionally**. The body ignores its arguments and raises:

```c
// tensor.__setitem__(self, indices, value)                   # line 104
void __setitem__(self, indices, value) {
    const char *clsname = type(self)->__name__;               // n_s_class / n_s_name
    raise err_cannot_assign_to_index(
        f"{clsname} cannot be directly assigned to a tensor, use store operation instead.");
}
```

The message fragment is present verbatim in `.rodata`:

> *" cannot be directly assigned to a tensor, use store operation instead."*

The docstring (*"Set the value(s) at the given indices…"*) is still attached, but the body never honors it. **A NKI tile write must go through an explicit store op** (`nl.store` / SBUF store), never `tile[...] = value`. This is the single most important lvalue rule in the model: tiles are SSA-like values, and `__setitem__` is deliberately a trap that redirects the user to the store API.

> **QUIRK — the type name is formatted into the message at raise time.** `type(self).__name__` is interpolated, so the error reads e.g. `"NeuronSBTensor cannot be directly assigned…"` — the message names the *concrete* class even though the raising code is the abstract `tensor.__setitem__`. `mask.__setitem__` (line 529) and `tile_index.__setitem__` (line 575) are *separate abstract overrides*, so subclasses can give their own assignment errors.

### 3.3 The real lvalue path — `tile_assignment` / `update_lvalue` (line 604, CONCRETE, CONFIRMED)

The legal way to model `a[...] = b` at the IR level is the module function `tile_assignment` @ `0x36390`:

```c
// tile_assignment(a, b)                                      # line 604
PyObject *tile_assignment(a, b) {
    return a->update_lvalue(b);                               // n_s_update_lvalue
}
```

`update_lvalue` is abstract in base `tile` (`"update_lvalue not implemented for base tile"`); the concrete impl reifies the assignment as an SSA-style lvalue-update *node* in the trace, not a Python `__setitem__`. This is how the trace machinery models a store: assignment becomes an IR operation (cross-ref `KernelBuilder` / `BirCodeGenLoop`), keeping the value model functional while still expressing mutation.

> **CORRECTION (report §3.4 cross-ref):** the backing report cites the lowering target as "D-P22". On this wiki the trace/codegen lowering is documented under [6.2.6 nki/bir-codegen-loop](bircodegenloop.md) and the `KernelBuilder` page; the IR-level reification claim itself is CONFIRMED by the one-line `update_lvalue` delegation and the abstract base stub.

---

## 4. `match_par_dim` — partition-dim operand alignment (line 609, CONCRETE)

Before an elementwise binary op runs, the two operands must share the same partition (leading) extent. `match_par_dim` @ `0x308c0` is that pre-pass. The control flow is STRONG (the `RichCompare` / `PyNumber_Add` / `broadcast_to` sequence and linenos 609–625 are CONFIRMED; the exact per-element comparison constants are obscured by Cython's `RichCompare` lowering — see fidelity note):

```c
// match_par_dim(x, y)                                        # line 609
(PyObject*, PyObject*) match_par_dim(x, y) {
    xs = list(x->shape);  ys = list(y->shape);                // 610  getattr shape → PySequence_List
    if (xs == ys)                                             // 612  RichCompare ==
        return (x, y);                                        // 613  fast-return on equal shapes
    // else align the partition (leading) dim to the larger of the two:
    if (xs[0] < ys[0]) {                                      // ~616
        new = [max(xs[0], ys[0])] + xs[1:];                   // 618  PyList_New + PyNumber_Add (list concat)
        x = x->broadcast_to(new);                            // 619  n_s_broadcast_to FastCall
    }
    if (ys[0] < xs[0]) {                                      // 621
        new = [max(xs[0], ys[0])] + ys[1:];                   // 622  PyList_New + PyNumber_Add
        y = y->broadcast_to(new);                            // 623  n_s_broadcast_to FastCall
    }
    return (x, y);                                            // 625
}
```

What is certain: it compares `x.shape` vs `y.shape` (fast-returns on equal), then conditionally calls `x.broadcast_to(…)` and/or `y.broadcast_to(…)`, each preceded by a one-element-list-head concatenated to a tail. That one-element head is the partition-dim length being substituted into the shape — i.e. the smaller operand's leading dim is broadcast *up* to the larger, leaving the trailing (free) dims for ordinary numpy broadcast. `match_par_dim` is the operand-alignment rule for the partition dimension and returns the aligned `(x, y)` pair.

---

## 5. Operators — the dispatch table

### 5.1 The universal pattern (STRONG)

Every elementwise / comparison operator on a `tensor` routes through `as_tile()._binop(np.<ufunc>, other, …)`:

```c
PyObject *__OP__(self, other) {
    return self->as_tile()->_binop(np.<ufunc>, other, …);    // _binop abstract @434 → concrete subclass
}
```

`_binop` (line 434), `_unary_op` (~312), and `_matmul` (line 431) are all abstract in base; the concrete subclass lowers them to `nki_api` / `array_functions` op nodes (cross-ref `KernelBuilder.NeuronCodegen`).

### 5.2 The ufunc binding table (CONFIRMED for the loaded constants; STRONG for the rest)

Each dunder binds a specific numpy ufunc name. **All of these names are present in the module's `.rodata` string pool** (verified directly): `add`, `subtract`, `multiply`, `true_divide`, `divide`, `mod`, `bitwise_and`, `bitwise_or`, `bitwise_xor`, `left_shift`, `right_shift`, `less`, `less_equal`, `greater`, `greater_equal`, `int8`, `int32`, `integer`.

| dunder | line | numpy ufunc | grounding |
|---|---|---|---|
| `__add__` / `__radd__` | 114/131 | `np.add` | CONFIRMED (`n_s_add` loaded in `__add__`) |
| `__sub__` / `__rsub__` | 148/161 | `np.subtract` | STRONG (pool const) |
| `__mul__` / `__rmul__` | 171/184 | `np.multiply` | STRONG |
| `__truediv__` / `__floordiv__` | 202/197 | `np.true_divide` / `np.floor_divide` | STRONG (`divide`,`true_divide` both pooled) |
| `__mod__` | 276 | `np.mod` | CONFIRMED (binds `mod`, **not** `fmod`) |
| `__and__`/`__or__`/`__xor__` | 224/232/244 | `bitwise_and`/`_or`/`_xor` | STRONG |
| `__lshift__`/`__rshift__` | 268/272 | `left_shift`/`right_shift` | STRONG |
| `__lt__`/`__le__`/`__gt__`/`__ge__` | 248/252/256/260 | `less`/`less_equal`/`greater`/`greater_equal` | CONFIRMED for `__lt__` (`n_s_less`) |

> **GOTCHA — `mod` ≠ `fmod`.** The string pool contains **both** `mod` and `fmod`. `__mod__` binds `mod` (CONFIRMED via the `n_s_mod` getattr in its body); `fmod` is used elsewhere. Do not assume every pooled ufunc name maps 1:1 to the operator with the closest spelling.

> **NOTE — comparisons return an int8 mask-tile.** `__lt__` fetches `np.less`, then `np.int8`, and **casts the boolean result to `int8`** (CONFIRMED: the body loads `n_s_np→n_s_less` then `n_s_np→n_s_int8`). A tile compare therefore yields an `int8` mask-like tile, not a Python `bool`. `int32` / `integer` are also referenced for index-dtype normalization.

All reflected forms (`__radd__`…`__rxor__`) exist and mirror with operands swapped.

### 5.3 The `tile` vs `tile_index` add-split (CONFIRMED)

`__add__` is *not* a plain elementwise add — it dispatches on whether the operand is a `tile_index`:

```c
// tensor.__add__(self, other)                                # line 114
PyObject *__add__(self, other) {
    other = other->as_tile();                                // 124
    if (!isinstance(other, tile_index))                       // 127  IsInstance vs tile_index global
        return self->_add_tile_or_number(other);             // 129  → self._binop(np.add, other)  @498
    return self->_build_dynamic_index(other);                // tile_index branch → dynamic index expr
}
```

Adding a plain tile/number does an elementwise add (`tile._add_tile_or_number` @498 → `self._binop(np.add, other)`, CONFIRMED). Adding a `tile_index` instead builds a **dynamic index expression** via `_build_dynamic_index` (abstract here; concrete in `indexing.so` [6.2.3](index-mask-inference.md)). This is how `arange`/affine index math composes: `base_index + offset_tile` produces a new dynamic *access pattern*, not a numeric value-add. The `tile_index` subtype is precisely the carrier that flips `+` from "add values" to "compose accesses."

### 5.4 In-place ops — the `_build_inplace_op` factory (lines 464/465, CONCRETE, STRONG)

The augmented assigns (`__iadd__`/`__imul__`/`__isub__`/`__itruediv__`, …) are bound to closures produced by `_build_inplace_op` @ `0x283c0`, whose inner `_inplace_op` is at `0x43290`:

```c
// _build_inplace_op(op, name)                                # line 464
closure _build_inplace_op(op, name) {
    PyObject *_inplace_op(a, b, name=name) {                  // 465  inner CyFunction (a, b, name)
        a = a->as_tile();                                     // 466
        if (isinstance(b, tensor)) {                          // 467  IsInstance vs `tensor` global
            b = b->as_tile();                                 // 468
            return a->_iop_tile_impl(b, op=op, name=name);    // 468  n_s_iop_tile_impl (kwargs op,name)
        }
        return a->_iop_number_impl(b, op=op, name=name);      // 470  n_s_iop_number_impl (kwargs op,name)
    }
    return _inplace_op;
}
```

`op` and `name` are captured per-operator; the kwargs dict is built with two `PyDict_SetItem` calls (`op=…`, `name=…`, CONFIRMED). `_iop_tile_impl` (440) / `_iop_number_impl` (443) are abstract in base (`"_iop_tile_impl not implemented for base tensor"`) — the concrete subclass emits an in-place IR op. The factory context also references `.sema.check_shape_identical` (the in-place op validates the two tiles have identical shape before mutating — STRONG/INFERRED).

### 5.5 `__matmul__` (line 264, CONCRETE, CONFIRMED)

```c
PyObject *__matmul__(self, other) {                          // 264
    return self->as_tile()->_matmul(other->as_tile());       // n_s_as_tile ×2, n_s_matmul
}
```

Both operands are converted via `as_tile()`; `_matmul` (431) is abstract here, with the concrete impl validating via the imported `sema.check_matmul_high_level_shape` before emitting the matmul IR op.

---

## 6. `mask` — the predicate algebra (cross-ref [6.2.4](mask-predicate-algebra.md))

`mask` is an abstract `tensor` subclass (docstring `"mask is an abstract class"`); the concrete masks (`predicate`, `ScalarPredicate`) live in `predicates.so`. A `mask` is a tensor-shaped boolean that gates a tile op, and its operator surface is a *boolean algebra*, not arithmetic:

```text
mask.__and__   [588] → NotImplementedError (589)    intersection (concrete in predicates.so)
mask.__or__    [594] → NotImplementedError (595)    union
mask.__invert__[600] → NotImplementedError (601)    complement
mask.__rand__  [591] → CONCRETE: return self.__and__(other)   (n_s_and — commutative delegation)
mask.__ror__   [597] → CONCRETE: return self.__or__(other)    (n_s_or)
```

The `__rand__`/`__ror__` reflected forms are the only concrete bodies — they delegate to `__and__`/`__or__` on `self`, which is the commutativity of `&`/`|`. The region-enumeration hooks (`enumerate_disjoint_intersections` 578, `enumerate_intersection_predicates` 581, `is_always_false` 584 for dead-region elimination, `is_scalar` 518, `combine_tile_with` 532, `_promote_to_mask` 535) are all abstract contract stubs that the trace/codegen uses to turn `tile[mask]` / `tile[pred]=…` into a set of guarded (predicated) sub-tile operations; the real region math is in `predicates.so` + `sema`.

The end-to-end mask-attach path (CONFIRMED via §3.1):

```text
tile_expr[predicate_or_mask]
  → tensor.__getitem__
  → t = tile_expr.as_tile()
  → if raw predicate: m = predicate._promote_to_mask(t)    # lift predicate → mask
  → t.mask_tensor(m)   # tile.mask_tensor (486, abstract; concrete in NeuronSBTensor)
                       #   → a masked/predicated tile whose subsequent ops carry the predicate
```

> **QUIRK — a `tile_index` cannot be a predicate.** `tile_index._promote_to_mask` is an explicit override (line 535, CONCRETE) that raises `"_promote_to_mask() is not supported for tile_index"` (string CONFIRMED in `.rodata`). An index expression carries affine *values*, not a boolean — it can never act as a mask, and the model rejects the conversion loudly rather than producing a nonsense predicate.

---

## 7. View / dtype family

| method | line | semantics | grounding |
|---|---|---|---|
| `astype(dtype)` | 352 | *"Copy of the tensor… Copy ALWAYS occur."* → `_astype_impl` (abstract @489) if dtype differs, else `self` | CONFIRMED (docstring) |
| `view(dtype)` | 365 | *"reinterpret… NO copy will occur."* → `_view_impl` (377, abstract) | CONFIRMED |
| `_compute_new_shape_for_view` | 380 | dtype-resize byte math (numpy `ndarray.view` rule) | CONFIRMED |
| `reshape(shape)` | 316 | *"new shape without changing data… no copy"* | abstract |
| `expand_dims(axis)` | 325 | adds a size-1 dim at `axis` | abstract |
| `broadcast_to(shape)` | 334 | tuple-equal fast-path → `self`, else `_broadcast_to_impl` (489, abstract) | CONFIRMED |
| `assert_shape(shape)` | 46 | `assert self.shape == shape, "Expected shape …"`; returns `self` (fluent) | CONFIRMED |

`_compute_new_shape_for_view` carries two CONFIRMED error strings — *"Cannot obtain view of tensor with dtype '…"* and *"When changing to a larger dtype, its size must be a divisor of the total size in bytes of the last axis of the tensor"* — encoding the numpy view rule: enlarging a dtype requires the new itemsize divide the old last-axis byte length; shrinking subdivides the last axis. `broadcast_to`'s body normalizes both `self.shape` and `shape` to tuples (`PySequence_Tuple`) before the equality compare, then returns `self` unchanged on a no-op (CONFIRMED, line 346).

The properties `shape`(39)/`ndim`(56)/`dtype`(63)/`buffer`(70) are abstract; `itemsize`(77) is derived from `dtype.itemsize`; `base`(410) parallels `numpy.ndarray.base` (its docstring even cites the numpy doc URL). `buffer` is the memory space (SBUF / PSUM / DRAM) — the concrete `NeuronSBTensor` lives in SBUF.

---

## 8. Scalar interop

There is **no** standalone `return_tensor_or_extracted_scalar` symbol in `tensor.so` (INFERRED: it lives in a sibling — `scalars.so` / `tensors.so`). Scalar-vs-tensor disambiguation here is the `is_scalar` predicate (`mask.is_scalar` @518, `tile_index.is_scalar` @564 — both abstract) plus the imported `DynamicScalar` (the trace-time-unknown scalar value type a tile op returns when it reduces to a single element). A scalar compare yields a `ScalarPredicate` usable as a mask; `is_scalar` tells the caller whether to unwrap a 0-D result to a scalar.

---

## 9. Adversarial self-verification

The five strongest claims, re-challenged against the binary (`BuildID a891a5a1…`):

1. **Partition-dim-is-highest contract.** `strings` on the `.so` returns the `broadcast_to` docstring verbatim, including *"A tensor can be implicitly converted to a tile iff the partition dimension is the highest dimension"* and the `tile` class docstring *"a subtensor whose partition dimension is the highest dimension."* **CONFIRMED.**
2. **Store-only-lvalue rule.** The fragment *" cannot be directly assigned to a tensor, use store operation instead."* is present in `.rodata`; `tensor.__setitem__` qualname exists at line 104; the lvalue path `tile_assignment` @ `0x36390` and the `__pyx_n_s_update_lvalue` constant both exist. **CONFIRMED.**
3. **`as_tile()` is abstract here and is the chokepoint.** The string *"as_tile not implemented for base tensor"* is present; `__pyx_n_s_as_tile` exists; `tensor.as_tile` qualname exists. The per-op routing is STRONG (decompiled getattr order, not a literal call graph), so the *enforcement* is tagged GOTCHA as living in `tensors.so`. **CONFIRMED (abstract) + STRONG (routing).**
4. **`mask` derives from `tensor`, not `tile`.** Recovered qualnames place `mask.*` methods directly; the four class names `tensor`/`tile`/`tile_index`/`mask` each appear exactly once as a defined name, and the ClassCreate bases for `mask` use the `tensor` global. **CONFIRMED.**
5. **`tile_index._promote_to_mask` raises.** The string *"_promote_to_mask() is not supported for tile_index"* and the override qualname `tile_index._promote_to_mask` (`__pyx_pf_…tile_index_12__promote_to_mask`) both exist. **CONFIRMED.**

Items left explicitly tagged below CONFIRMED: the per-element comparison constants inside `match_par_dim`/`broadcast_to` (Cython `RichCompare` lowering hides which `<`/`==` and which shape element — algorithm STRONG, literal predicate reconstructed); the ufunc bindings for ops whose body wasn't individually decompiled (STRONG, grounded on pool presence); `check_shape_identical` use inside `_build_inplace_op` (STRONG/INFERRED); and `return_tensor_or_extracted_scalar`'s sibling location (INFERRED).

### Fidelity notes (where Cython obscures the truth)

- All four classes are pure-Python (no `cdef` extension types) — attribute/slot layout is dict-based; there are no RTTI records or field offsets to recover for them. **CONFIRMED absence.**
- `RichCompare`-heavy bodies (`match_par_dim`, `broadcast_to`) lower the comparison op-code generically; the operands (`x.shape`/`y.shape`, broadcast targets) and branch structure ARE recovered, so the algorithm is STRONG, but the precise per-element predicate is reconstructed from the surrounding `broadcast_to` + list-concat, not read as a literal.
- The numpy-ufunc binding per operator is taken from the `n_s_<ufunc>` constants loaded in each decompiled body (CONFIRMED for `add`/`less`/`int8`/`mod`); the rest are matched by pool-present names (STRONG). The `mod`/`fmod` coexistence is the standing trap.

---

## Cross-references

- **[6.2.2 memref / view model](memref-view-model.md)** — the concrete `NeuronSBTensor` / `MemrefTile` where `as_tile`'s partition-dim assertion and the 0-stride `_broadcast_to_impl` actually live.
- **[6.2.3 indexing inference](index-mask-inference.md)** — `_index_tensor` / `_build_dynamic_index` / `tile_index` affine arithmetic (the `arange` machinery).
- **[6.2.4 mask / predicate](mask-predicate-algebra.md)** — concrete `predicate` / `ScalarPredicate` and the `enumerate_*` region splitting.
- **[6.3.1 type system](type-system.md)** — `nki_dtype` / `nki_int_dtype` and the dtype helpers this module imports.
- **`sema`** — `check_shape` / `check_shape_identical` / `check_store_shape` / `check_matmul_high_level_shape` shape-legality validators imported here.
- **`metaclasses`** — `tensor_type`, the metaclass driving all four classes.
