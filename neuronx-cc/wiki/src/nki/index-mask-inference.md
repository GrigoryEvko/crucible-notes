# Affine-Range and Index/Mask Inference

> *All addresses on this page apply to the cp310 build of `neuronxcc/nki/compiler/backends/neuron/indexing.cpython-310-x86_64-linux-gnu.so` in neuronx_cc 2.24.5133.0+58f8de22. The cp311/cp312 wheels carry the same module compiled with the same Cython 3.0.10 front-end; addresses differ. Backend AffineExpr primitives are in `libwalrus.so`.*

## Abstract

NKI lets a kernel author write tile accesses in ordinary Python subscript syntax — `a[i, :]`, `a[ds(off, 64), :]`, `a[i] + 1`, `a[i] < n` — and the compiler must turn that into a concrete *access pattern*: a partition axis, a list of free axes, an affine address expression per axis, and (for predicated instructions) a mask. The `indexing` module is the abstract-interpretation layer that performs that translation. It is a pure-Python module compiled with Cython 3.0.10 (the interned string `_cython_3_0_10` is present); `TileIndex` and `NDTile` are plain `@dataclass`-flavored Python classes, so there is no `__pyx_obj_*` extension struct for them — instance state lives in interned-name attributes (`e_2`, `tile_2`, `par_indices`, …) accessed through `tp_setattro`/`tp_getattro`. Every function-to-address mapping below is read from the `__pyx_pw_`/`__pyx_pf_` mangled symbols in the module's `_names.json`; every source line number is the argument to a `_Pyx_AddTraceback` call in the decompiled body, which Cython sets to the `.py` source line.

The data model has two levels. A **`TileIndex`** is `(e: AffineExpr, tile: Optional[NDTile])` — one affine index expression over loop variables, plus a back-pointer to the tile it indexes. All index arithmetic (`+ - * // %`, negation, comparisons) folds into `e` while keeping `tile` fixed; the index can never become non-affine because multiply/floordiv/mod by a non-integer is rejected. An **`NDTile`** is `(par_indices, free_indices : list[TileIndex])` — the Neuron two-dimensional tile: one partition axis group and a list of free axes, each an affine index. This is the same partition×free geometry the SBUF/PSUM hardware imposes (see [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md)); the index layer keeps the two axis classes separate at every step, broadcasting them independently.

The inference itself is a five-stage pipeline. Range constructors (`arange[:]`, `ds(start,size)`, Python slices) are normalized into `(start, length, step)` triples by `normalize_slice` → `normalize_range_args` → `normalize_expr_range_args`, the last of which decides static-vs-dynamic length. `partial_tile_index` turns a shape into a full per-axis `TileIndex` tuple. `combine_tiles` / `cached_combine_index_tiles` compose nested per-axis indices by broadcasting and substituting them onto a common iteration space. `canonicalize_expr` → `AffineExpr.substitute` is the affine-substitution leaf that re-bases one index's loop axes. Finally `inst_mask` substitutes each predicate's indices onto an instruction's destination tile and intersects them into a mask consumed by the [mask predicate algebra](mask-predicate-algebra.md).

For reimplementation, the contract is:

- The two-level `(TileIndex, NDTile)` data model and the affine-only invariant on index arithmetic.
- The range→index inference: how `normalize_expr_range_args` distinguishes a compile-time-constant length from a runtime (`scalar[int]`) length and what it does in each case.
- The composition algorithm: `combine_tiles` broadcasts par and free shapes *separately*; `cached_combine_index_tiles` folds an index-expression tuple under an `lru_cache`; `generate_subst_map` + `canonicalize_expr` re-base each input.
- The index→mask bridge: `inst_mask` and the comparison operators that promote an index relation to a tile mask.

| | |
|---|---|
| **Module** | `neuronxcc/nki/compiler/backends/neuron/indexing.py` (Cython 3.0.10) |
| **`TileIndex.__init__`** | `__pyx_pw_..._9TileIndex_1__init__` @ `0x4d0d0` (py:270) |
| **`NDTile.__init__`** | `__pyx_pw_..._6NDTile_1__init__` @ `0x51020` (py:54) |
| **`combine_tiles`** | `__pyx_pw_..._7combine_tiles` @ `0x49de0` (py:229) |
| **`cached_combine_index_tiles`** | `__pyx_pw_..._13cached_combine_index_tiles` @ `0x52d40` (py:423) |
| **`canonicalize_expr`** | `__pyx_pw_..._15canonicalize_expr` @ `0x469e0` (py:433) |
| **`normalize_expr_range_args`** | `__pyx_pw_..._19normalize_expr_range_args` @ `0x30b00` (py:462) |
| **`inst_mask`** | `__pyx_pw_..._25inst_mask` @ `0x3d0a0` (py:542) |
| **Backend substitution** | `bir::QuasiAffineExpr::substituteLoopAxes` @ `0x5f93d0` (`libwalrus.so`) |
| **Data model** | `TileIndex = (e: AffineExpr, tile: Optional[NDTile])`; `NDTile = (par_indices, free_indices)` |

> **NOTE —** all method bodies below are de-Cythonised reconstructions. The isinstance ladders, attribute reads, called globals (`PyNumber_*`/module-name lookups), `_Pyx_AddTraceback` line numbers, and error strings are read from the decompiled `__pyx_pw_` bodies and the module string table. The exact Python surface syntax (loop form, local names) is [INFERRED] to match those operations. Helpers named as module globals with no local `__pyx_pw_` symbol — `wrap_expr`, `implicit_cast_idx`, `generate_ap_index`, `filter_indices`, `ap_expr`, `n_elts`, `substitute_expr_indices`, `colon_slice`, `_build_dynamic_index`, `EQTileMask`, `TileMaskIntersection`, `CExpr`, `DynamicScalar`, `AffineExpr` — are imported; their bodies live in the penguin IR / predicates / tensor modules and are out of scope here.

---

## TileIndex — the Affine Index Abstraction

### Purpose

A `TileIndex` is the unit of NKI index inference: a single affine expression over loop variables, tagged with the tile it addresses. The class doc string in the binary is `Index - Base class of Affine Expression of indices` — `TileIndex` *is* the index `Index` class, and `__init__` calls `super().__init__()` (py:271), so it sits on top of a generic expression base. Everything an author can write on an index — arithmetic, comparison, broadcast, delinearize — is a method here that pushes the operation into the wrapped `AffineExpr` while holding the `tile` pointer fixed. That discipline is what guarantees the index stays affine and stays attached to its tile.

### Algorithm

```c
// __pyx_pw_..._9TileIndex_1__init__  @0x4d0d0  (indexing.py:270)
function TileIndex.__init__(self, e, tile=None):
    super().__init__()                              // py:271
    self.e    = implicit_cast_idx(wrap_expr(e))     // py:272 — wrap_expr THEN implicit_cast_idx
    self.tile = tile                                // py:273 — interned attr "tile_2"
    // instance state is exactly { e: AffineExpr, tile: Optional[NDTile] }
```

`wrap_expr(e)` lifts a raw Python int / expression into an `AffineExpr`; `implicit_cast_idx` canonicalises it to the index dtype. The only two attributes are `e` (accessor `__pyx_pw_..._9TileIndex_5e` @ `0x27150`, py:277) and `tile` (accessor `..._9TileIndex_7tile` @ `0x26ec0`, py:282) — thin field reads into the interned `e_2` / `tile_2` slots. `TileIndex.shape` (`..._9TileIndex_59shape` @ `0x22560`, py:403) returns `self.tile.shape` when a tile is attached and the empty tuple otherwise.

The arithmetic algebra all routes through one primitive:

```c
// __pyx_pw_..._9TileIndex_11_index_with_same_tile  @0x3b6c0  (indexing.py:290)
function TileIndex._index_with_same_tile(self, e):
    return TileIndex(e=e, tile=self.tile)   // rebuild on the SAME tile, new affine expr

// _add_impl  @0x4f330  (indexing.py:370)
function TileIndex._add_impl(self, other):
    if isinstance(other, Integral):
        return self._index_with_same_tile(self.e + other)     // constant affine shift
    if isinstance(other, scalar):                             // an nki runtime scalar
        if other.dtype != nki_scalar_int_dtype:
            raise <err: index + non-integer scalar>           // err_tile_index_add_non_integer_scalar
        return self._build_dynamic_index(other ...)           // runtime (dynamic-slice) offset
    if isinstance(other, TileIndex):
        return self.combine_tile_with(other)                  // compose two indexed tiles
    raise TypeError("unexpected type %s" % type(other))

// _mul_impl  @0x23320  (indexing.py:387)
function TileIndex._mul_impl(self, other):
    if isinstance(other, Integral):
        return self._index_with_same_tile(self.e * other)     // integer scale only
    raise TypeError("unexpected type %s" % type(other))
```

The three `_add_impl` branches are the three semantic classes of "add to an index": a compile-time `Integral` is a constant affine shift; an `nki scalar[int]` is a *runtime* offset that builds a dynamic index (the `ds()` lowering, §Range Constructors); another `TileIndex` is a cross-tile composition that defers to `combine_tile_with`. `_mul_impl` admits only integer scaling, which is precisely the constraint that keeps `e` affine.

### Function Map

| Operator | Symbol | Addr | py | Behavior |
|---|---|---|---|---|
| `__neg__` | `_9TileIndex_29__neg__` | `0x251f0` | 334 | `self * -1` |
| `__add__` | `_9TileIndex_35__add__` | `0x495a0` | 344 | `self._add_impl(other)` |
| `__radd__` | `_9TileIndex_37__radd__` | `0x47ce0` | 347 | `self._add_impl(other)` (commutes) |
| `__sub__` | `_9TileIndex_39__sub__` | `0x2f460` | 350 | `self + (-other)` |
| `__rsub__` | `_9TileIndex_41__rsub__` | `0x2edb0` | 353 | `(-self) + other` |
| `__floordiv__` | `_9TileIndex_43__floordiv__` | `0x24860` | 356 | int-guarded `self.e // other` |
| `__mod__` | `_9TileIndex_45__mod__` | `0x23ed0` | 360 | int-guarded `self.e % other` |
| `__mul__` | `_9TileIndex_47__mul__` | `0x48520` | 364 | `self._mul_impl(other)` |
| `__rmul__` | `_9TileIndex_49__rmul__` | `0x48d60` | 367 | `self._mul_impl(other)` |
| `__iadd__` | `_9TileIndex_31__iadd__` | `0x2e7c0` | 337 | in-place → `_add_impl` |
| `__imul__` | `_9TileIndex_33__imul__` | `0x2e1d0` | 341 | in-place → `_mul_impl` |
| `combine_tile_with` | `_9TileIndex_27combine_tile_with` | `0x25500` | 323 | cross-tile compose (§Composition) |
| `substitute` | `_9TileIndex_13substitute` | `0x4af00` | — | `self.e.substitute(...)` |
| `broadcast_to` | `_9TileIndex_61broadcast_to` | `0x333d0` | — | broadcast index to a target shape |
| `delinearize` | `_9TileIndex_55delinearize` | `0x3add0` | — | `self.e.delinearize(shape)` |
| `as_tile` | `_9TileIndex_57as_tile` | `0x22950` | 400 | materialise index as a value tile |

> **GOTCHA —** `__floordiv__` and `__mod__` raise `ValueError` (`'//' between NKI index and non integer is not supported.` / `'%' between NKI index and non integer is not supported.` — both strings present in the table) when the right operand is not `Integral`. A reimplementation that silently lowers `idx // var` to a runtime division will produce a non-affine index the access-pattern emitter cannot express. The affine-only invariant is enforced at the operator, not downstream.

### Considerations

`as_tile(self, dtype, mask=None)` (py:400) is the bridge out of the abstract layer: it calls `nki_ctx().index_value_inst(self, dtype, mask)`, emitting a one-element `ScalarTile` that materialises the index as a concrete value consumed by codegen. `broadcast_to` is guarded by three strings — `Not a broadcast!`, ` broadcast shape len: `, `, tile's rank: ` — and rejects a broadcast whose source and destination ranks are incompatible (`Can only broadcast when the rank of src and dst shape match!`).

---

## NDTile — the Two-Dimensional Tile

### Purpose

`NDTile` (doc string `NDTile - Represent a n-dimensional tile of an Neuron instruction`) is the partition×free tile. Its constructor takes a `par_shape` and `free_shape` plus an `is_partial` flag and builds, per axis, a `generate_ap_index` access-pattern index; it then prunes the trivial (size-1 / identity) axes with `filter_indices` to a *canonical* index set. The canonical set is what drives address generation — `par_addr` / `free_addr` fold it into one affine expression — so size-1 axes never pollute the access pattern.

### Algorithm

```c
// __pyx_pw_..._6NDTile_1__init__  @0x51020  (indexing.py:54)
function NDTile.__init__(self, par_shape, free_shape, is_partial):
    self.is_partial = is_partial                                          // py:56
    // one AP index per axis, built in REVERSED axis order:
    self.par_indices  = [generate_ap_index(i, d)
                            for i, d in enumerate(reversed(par_shape))]
    self.free_indices = [generate_ap_index(i, d)
                            for i, d in enumerate(reversed(free_shape))]
    self.canonical_par_indices  = filter_indices(self.par_indices)        // drop trivial axes
    self.canonical_free_indices = filter_indices(self.free_indices)
    self._ast_par_indices  = <expr form of par_indices>                   // py:65 genexpr
    self._ast_free_indices = <expr form of free_indices>                  // py:66 genexpr
    self.par_shape  = par_shape
    self.free_shape = free_shape
```

> **QUIRK —** `par_shape` and `free_shape` are walked through `reversed()` before `generate_ap_index`. The access-pattern index numbering therefore runs innermost-axis-first; a reimplementation that builds AP indices in source-shape order will assign the wrong stride to each axis. The two `_ast_*` lists are built by separate generator expressions (`NDTile.__init__.genexpr` at py:65 and py:66, symbols `..._6NDTile_8__init___2generator1` @ `0x35050` and `..._5generator2` @ `0x357f0`).

### Function Map

| Property | Symbol | Addr | py | Returns |
|---|---|---|---|---|
| `canonical_indices_set` | `_6NDTile_7canonical_indices_set` | `0x2c110` | 85 | `canonical_par_indices + canonical_free_indices` |
| `npartitions` | `_6NDTile_16npartitions` | `0x31f90` | 109 | `n_elts(par_shape)` |
| `partition_size` | `_6NDTile_18partition_size` | `0x329b0` | 113 | `n_elts(free_shape)` |
| `par_addr` | `_6NDTile_20par_addr` | `0x324a0` | 117 | `ap_expr(canonical_par_indices)` |
| `free_addr` | `_6NDTile_22free_addr` | `0x32ec0` | 121 | `ap_expr(canonical_free_indices)` |
| `shape_2d` | `_6NDTile_24shape_2d` | `0x2ad70` | 125 | `(npartitions, partition_size)` |
| `is_scalar` | `_6NDTile_26is_scalar` | `0x2aa60` | 129 | `shape_2d == (1, 1)` |
| `shape` | `_6NDTile_28shape` | `0x2a6d0` | 133 | `par_shape + free_shape` |
| `ndim` | `_6NDTile_30ndim` | `0x2a360` | 137 | `len(par_shape) + len(free_shape)` |
| `ap_tile` (classmethod) | `_6NDTile_34ap_tile` | `0x29780` | 144 | build `NDTile` from AP indices |
| `append_free_index` | `_6NDTile_3append_free_index` | `0x2c540` | — | push onto `free_indices` |
| `extend_free_indices` | `_6NDTile_5extend_free_indices` | `0x35f90` | — | extend `free_indices` |
| `inst_dst_tile` | `_6NDTile_38inst_dst_tile` | `0x45690` | — | destination tile of an instruction |
| `__eq__` / `__hash__` | `_6NDTile_11__eq__` / `_9__hash__` | `0x2b400` / `0x2be00` | — | structural identity |

The `par_addr` / `free_addr` pair is where an `NDTile` becomes an address: `ap_expr` (a module helper) folds the canonical index list into a single AP address expression, separately for the partition and free dimensions, matching the hardware's two address generators.

---

## Range Constructors — arange / ds / partial_tile_index

### Purpose

`arange` is the compile-time iteration-axis generator; its only state is `[start, end)`. Subscripting an `arange` materialises it as a tile index. The dynamic-slice user API `ds(start, size)` lives on the `nl` language side, but its *inference* — turning a `(start, size)` into either a static-length axis or a runtime-offset dynamic axis — is the slice-normalisation chain in this module. Both paths converge on `partial_tile_index`, which produces the per-axis `TileIndex` tuple.

### Algorithm

```c
// arange.tripcount  @0x29310  (indexing.py:178)
function arange.tripcount(self):
    return self.end - self.start                 // axis length

// arange.__getitem__  @0x3eb60  (indexing.py:183)
function arange.__getitem__(self, item):
    function _check_index(i):                    // @0x3c7d0  (py:187)
        if isinstance(i, slice):
            assert colon_slice(i), "Only support `:` or `None` as subscript to arange!"
            return self.tripcount                 // a bare ':' contributes a full axis
        elif i is None:
            return 1                              // None inserts a size-1 broadcast axis
        else:
            raise IndexError("Only support `:` or `None` as subscript to arange!")
    if not isinstance(item, tuple):
        item = (item,)                            // "Only support multi-dimensional subscript to arange!"
    // exactly one ':' permitted: "Need exact 1 `:` in the subscript to arange!"
    return partial_tile_index(<shape from start + checked indices>)   // py:204

// partial_tile_index  @0x38810  (indexing.py:31)
function partial_tile_index(shape):
    t = NDTile(...shape...)                       // build the tile from the shape
    indices = []
    for idx in t.par_indices:                     // partition axes first
        indices.append(TileIndex(idx, ...))       // each carries its tripcount
    for idx in t.free_indices:                    // then free axes
        indices.append(TileIndex(idx, ...))
    return TileIndex[tuple(indices)]              // typed index tuple via __class_getitem__
```

> **NOTE —** `arange[None, :]` inserts a size-1 broadcast axis (the `None` branch returns `1`) before the full axis (the `:` branch returns `tripcount`). This is how NKI expresses an explicit broadcast dimension at the index level; downstream `combine_tiles` will broadcast it against a real axis of another operand.

The three contract strings (`Only support multi-dimensional subscript to arange!`, `Only support \`:\` or \`None\` as subscript to arange!`, `Need exact 1 \`:\` in the subscript to arange!`) are all present in the table and enforce that an `arange` subscript is a single colon plus optional `None` broadcast axes — never a partial integer slice.

---

## Slice / Dynamic-Slice Normalisation

### Purpose

A Python `slice` (or a `range`, or a `ds()`) must be reduced to a `(start, length, step)` triple before it can become a `TileIndex`. The three-layer chain is `normalize_slice` (fills `None` defaults from the dimension length) → `normalize_range_args` (validates `step != 0`) → `normalize_expr_range_args` (the affine-aware coercion that decides static vs dynamic length). The last layer is where `ds()`'s runtime length is inferred.

### Algorithm

```c
// normalize_slice  @0x2cc80  (indexing.py:528)
function normalize_slice(idx, length):
    step  = idx.step  if idx.step  is not None else 1
    start = idx.start if idx.start is not None else 0
    stop  = idx.stop  if idx.stop  is not None else length      // fill with the dim length
    return normalize_range_args(slice(start, stop, step), length=length)

// normalize_range_args  @0x2fb10  (indexing.py:503)
function normalize_range_args(range_or_slice, ...):
    start, stop, step = <unpack range_or_slice>
    if step == 0:
        raise ValueError("step of `...` must be non-zero")      // string present
    return normalize_expr_range_args(start, stop, step, range_or_slice)

// normalize_expr_range_args  @0x30b00  (indexing.py:462)  -- THE ds() inference
function normalize_expr_range_args(start, stop, step, range_or_slice):
    function as_scalar_expr(v, name):                           // @0x1e460  (py:471)
        if isinstance(v, scalar):                               // nki runtime scalar
            return CExpr(v.value)                               // dynamic value expr
        if isinstance(v, Integral):
            return v                                            // compile-time int
        raise ValueError(f"`{name}` must be int or scalar[int]")
    start = as_scalar_expr(start, "start")
    stop  = as_scalar_expr(stop,  "stop")
    diff  = AffineExpr(stop - start)                            // py: PyNumber_Subtract then AffineExpr
    if diff.is_const:                                           // py:~381 — compile-time length?
        length = diff.c                                         // py:~418 — constant length
    else:
        // DYNAMIC length (ds with a runtime scalar[int] bound):
        assert step == 1, "step of `...` must be 1 when `stop - start` is not compile time constant"
        length = diff.supremum                                 // py:~708 — static upper bound
    return (start, length, step)
```

This is the exact static-vs-dynamic decision. When `stop - start` folds to a constant `AffineExpr`, `is_const` is true and the axis takes that constant length (`diff.c`). When a bound is a runtime `scalar[int]`, the difference is a symbolic `AffineExpr`; the axis takes `diff.supremum` (the analysis's upper bound) as its static extent and is restricted to `step == 1`. The `supremum` is what lets the rest of the pipeline allocate a fixed-size tile for a runtime slice — the dynamic offset rides in the index expression, but the axis length is bounded at compile time.

> **GOTCHA —** the `step == 1` restriction applies *only* to the dynamic-length path (`assert` inside the `else` branch). A static slice may have any non-zero step; a runtime-bounded `ds()` may not be strided. A reimplementation that forbids strided slices universally, or that allows strided dynamic slices, both diverge — the gate is exactly `not diff.is_const`. The error string is `\` must be 1 when \`stop - start\` is not compile time constant`. Bad operand types raise via `Unexpected type of stop/start in \`` and `\` must be int or scalar[int]`, both present in the table.

---

## Tile Composition — combine_tiles / cached_combine_index_tiles

### Purpose

This is the core of the module: how nested per-axis indices collapse into one access pattern. There are two entry points with different granularity. `combine_tiles(*tiles)` works at the *shape* level — it broadcasts the partition shapes and the free shapes of several `NDTile`s separately and rebuilds one tile. `cached_combine_index_tiles(exprs, extra_tiles=())` works at the *expression* level — it folds a tuple of index expressions, broadcasting their shapes and building the affine substitution table that re-bases each inner index onto the broadcast output. The second is `lru_cache`d on the immutable `(exprs, extra_tiles)` tuple.

### Algorithm

```c
// combine_tiles  @0x49de0  (indexing.py:229)
function combine_tiles(*tiles):
    tiles = unique_sequence(tiles)                              // py:230 — order-preserving dedup
    par_shape  = _broadcast_shape_with_expand_dims(             // py:234 genexpr8
                     t.par_shape  for t in tiles)               //   broadcast PARTITION shapes
    free_shape = _broadcast_shape_with_expand_dims(             // py:236 genexpr9
                     t.free_shape for t in tiles)               //   broadcast FREE shapes (separately!)
    return NDTile(par_shape, free_shape, ...)                   // fresh combined tile

// cached_combine_index_tiles  @0x52d40  (indexing.py:423)
@lru_cache(maxsize=...)
function cached_combine_index_tiles(exprs, extra_tiles=()):
    tiles = [<index tile of e> for e in exprs]                  // py:424-425 genexpr
    tiles.extend(extra_tiles)                                   // py:427
    shape = broadcast_shapes(*[t.shape for t in tiles])         // py:428
    subst_map = generate_subst_map(...)                         // re-base each axis onto `shape`
    return <combined access pattern using subst_map over shape>

// TileIndex.combine_tile_with  @0x25500  (indexing.py:323) -- per-index entry
function TileIndex.combine_tile_with(self, other):
    if isinstance(other, TileIndex):                            // @ py:323 ladder
        return cached_combine_index_tiles((self, other))        // lru-cached fold
    if isinstance(other, DynamicScalar):
        return TileIndex(self.e + other.value, self.tile)       // fold runtime offset, keep tile
    return <fallback: combine on self.tile / self.e>
```

> **QUIRK —** `combine_tiles` broadcasts the partition shape list and the free shape list *as two independent numpy broadcasts*. The Neuron tile is not a flat n-D array; its partition dimension is a distinct hardware axis (the 128 partitions of SBUF/PSUM). A reimplementation that flattens par+free into one shape and broadcasts once will mis-align operands whose partition counts differ from their free extents. The two `_broadcast_shape_with_expand_dims` calls (genexpr8 @ py:234, genexpr9 @ py:236) are deliberately separate.

The `combine_tile_with` dispatch is confirmed in the decompiled body: the `TileIndex` branch looks up `cached_combine_index_tiles` as a module global and calls it on `(self, other)`; the `DynamicScalar` branch reads `other.value` and `self.tile` and folds the runtime offset directly into a new `TileIndex` on the same tile. The `lru_cache` on `cached_combine_index_tiles` means identical nested-index shapes reuse the fold — a hot path when the same `a[i][j]` pattern recurs across an unrolled loop body.

### Considerations

```c
// _broadcast_shape_with_expand_dims  @0x40ee0  (indexing.py:~213)
function _broadcast_shape_with_expand_dims(shapes):
    max_rank = max(len(s) for s in shapes)                      // py:215 genexpr5
    expanded = [(1,) * (max_rank - len(s)) + s                  // py:218 genexpr6 — left-pad with 1
                    for s in shapes]
    return np.broadcast_shapes(*expanded)                       // numpy broadcast

// unique_sequence  @0x3a1a0  (indexing.py:223, classmethod)
function unique_sequence(cls, seq, exclude_none=...):
    seen = set(); add = seen.add
    return [x for x in seq
              if (not exclude_none or x is not None)
              and not (x in seen or add(x))]                    // order-preserving dedup
```

`_broadcast_shape_with_expand_dims` is the pure shape-tuple broadcaster: left-pad each shape with `1`s to the maximum rank, then delegate to `np.broadcast_shapes` (the `numpy` global and the `broadcast_shapes` attribute are both referenced). `unique_sequence` (used by `combine_tiles` at py:230) dedups while preserving order — the `seen.add` cell is the classic "membership-test-with-side-effect" idiom (`PySet_New` plus a bound `add` method in the decompile).

---

## Canonicalisation and Affine Substitution

### Purpose

`canonicalize_expr` is the per-index leaf of the inference: it turns *any* index — a `TileIndex`'s affine `e`, a `DynamicScalar`'s runtime `value`, or a raw int — into one `AffineExpr` whose loop axes have been re-based per a substitution map. `generate_subst_map` builds that map by pairing source axes with destination axes positionally, dropping degenerate size-1 axes. The actual axis rewrite is `AffineExpr.substitute(subst_map)`, which in the backend is `bir::QuasiAffineExpr::substituteLoopAxes`.

### Algorithm

```c
// canonicalize_expr  @0x469e0  (indexing.py:433)
function canonicalize_expr(i, subst_map):
    function substitute(expr):                       // closure  @0x46180  (py:434)
        if not subst_map:                            // py:435 — empty map = identity
            return expr
        return expr.substitute(subst_map)            // py:436 — AffineExpr.substitute(map)
    if isinstance(i, TileIndex):                     // py:440
        return substitute(i.e)                       // py:441
    if isinstance(i, DynamicScalar):                 // py:443
        return substitute(i.value)                   // py:444 — reads ".value"
    if i is None:
        return i
    return substitute(wrap_expr(i))                  // py:449 — raw int path

// generate_subst_map  @0x280d0  (indexing.py:241)
function generate_subst_map(from_indices, to_indices):
    subst_map = {}
    for f, t in zip(from_indices, to_indices):
        if f.tripcount != 1:                         // skip trivial size-1 axes
            subst_map[f] = t
    return subst_map
```

The decompiled `canonicalize_expr` body confirms the three-way isinstance ladder at py:433, the `TileIndex` global lookup, the `DynamicScalar` global lookup with a `.value` attribute read (py:~274), and the `wrap_expr` fallback (py:~262). The nested `substitute` closure (a separate `__pyx_pf_..._17canonicalize_expr_substitute` symbol @ `0x46180`) is set up at py:434 and short-circuits to identity on an empty map (py:435) before calling `expr.substitute` (py:436).

> **NOTE —** `generate_subst_map` is gated by `tripcount` so size-1 (broadcast) axes are *not* substituted — they have no real iteration variable to re-base. This is the dual of the broadcast-axis insertion in `arange.__getitem__`: a `None` axis is created with extent 1, then skipped here so it never appears in the affine map. The substitution is positional axis-to-axis, which is why `broadcast_shapes` (below) must first unify all inputs onto one tile to define a consistent axis ordering.

### Considerations

```c
// broadcast_shapes  @0x277c0  (indexing.py:245)  -- the top-level orchestrator
function broadcast_shapes(indices):
    tile = combine_tiles(*indices)                   // py:246 — unify all index tiles into one
    results = []
    for idx in indices:
        subst_map = generate_subst_map(idx.indices, tile.indices)   // py:253
        results.append(<idx canonicalised onto tile via subst_map>)
    return tile, results
```

`broadcast_shapes` (single-argument: the error path is `takes exactly one argument`) ties §Composition and §Canonicalisation together. It folds all input indices into one combined `tile` via `combine_tiles`, then for each input produces the `generate_subst_map`-driven substitution that re-bases it onto the common iteration space. Its output — the combined tile plus per-input canonicalised expressions — is the broadcast result a multi-operand instruction consumes.

The backend `AffineExpr.substitute` is `bir::QuasiAffineExpr::substituteLoopAxes(const vector<LoopAxis*>&, const vector<LoopAxis*>&)` @ `0x5f93d0` in `libwalrus.so` — it takes a `from`-axis vector and a `to`-axis vector (exactly the keys and values of `subst_map`) and rewrites the quasi-affine expression's loop-axis references. This is the C++ primitive under the Python `expr.substitute(subst_map)` call; see [AffineExpr Algebra](../penguin/affine-expr-algebra.md).

---

## Index → Mask Bridge

### Purpose

Comparisons on a `TileIndex` (`<`, `<=`, `>`, `>=`, `==`) do not produce booleans — they produce *tile masks*. The module constructs masks from index relations; the mask algebra itself (intersection, union, the `EQTileMask` / `TileMaskIntersection` types) lives in the sibling predicates module ([mask predicate algebra](mask-predicate-algebra.md)). `inst_mask` is the per-instruction terminus: it takes an instruction's predicate indices, substitutes them onto the concrete destination-tile axes, and intersects them into one mask.

### Algorithm

```c
// TileIndex._promote_to_mask  @0x269c0  (indexing.py:308)
function TileIndex._promote_to_mask(self):
    from <predicates> import EQTileMask
    return EQTileMask(self ...)                       // lift an index into an equality mask

// TileIndex._generate_predicate  @0x4e0f0  (indexing.py:313)
function TileIndex._generate_predicate(self, other, pred_fn):
    ti = self.combine_tile_with(other)                // unify the two index tiles
    return TileMaskIntersection(pred_fn(ti, ...))     // build the comparison mask
// __lt__/__le__/__gt__/__ge__ feed pred_lt/pred_le/pred_gt/pred_ge

// inst_mask  @0x3d0a0  (indexing.py:542)
function inst_mask(inst):
    if not inst.is_predicated:                        // py:~498 gate
        return None
    dst_tile = inst.inst_dst_tile                     // py:~567
    preds    = inst.predicates                        // py:~605
    dst_par  = inst.dst_par_indices                   // py:~618
    dst_free = inst.dst_free_indices                  // py:~630
    subst_map = generate_subst_map(                   // py:~1358/1443
                    dst_tile.canonical_par_indices + dst_tile.canonical_free_indices,
                    dst_par + dst_free)
    masks = [substitute_expr_indices(p, subst_map) for p in preds]   // py:557 genexpr
    return TileMaskIntersection(*masks)               // intersect all predicate masks
```

The `inst_mask` decompiled body confirms every step: the `is_predicated` gate (`tp_getattro` of `is_predicated` @ py:498) returning `None` when false; `inst_dst_tile` (@567), `predicates` (@605), `dst_par_indices` (@618), `dst_free_indices` (@630); the `canonical_par_indices` (@675) and `canonical_free_indices` (@856) reads feeding `generate_subst_map` (@1358 and @1443); and the `TileMaskIntersection` `ImportFrom` (@1110) that intersects the substituted predicate masks. This is precisely where abstract predicate indices become the instruction-level access mask.

> **NOTE —** `inst_mask` uses `substitute_expr_indices` (an imported bulk-substitution helper) rather than the per-axis `canonicalize_expr`. The difference is that a predicate is a whole expression with many index references; `substitute_expr_indices` applies one `subst_map` across the entire predicate at once, where `canonicalize_expr` re-bases a single index leaf. Both consume the same `generate_subst_map` output.

### Considerations

The mask *types* (`EQTileMask`, `TileMaskIntersection`) are imported from the sibling predicates backend module — the `ImportFrom` in `inst_mask` and `_promote_to_mask`. This module only *constructs* masks from index relations and *substitutes* their indices onto destination tiles; it does not implement mask conjunction/disjunction. A reimplementer should treat the mask object as an opaque value here and look to [mask predicate algebra](mask-predicate-algebra.md) for its operations.

---

## Pipeline Data-Flow

```text
  range source        arange[:]  /  ds(start, size)  /  Python slice
       |              normalize_slice -> normalize_range_args
       v              -> normalize_expr_range_args   (is_const ? length=diff.c : length=diff.supremum, step==1)
  per-axis index      partial_tile_index(shape) -> TileIndex tuple (par + free)
       |                each TileIndex = (AffineExpr e, NDTile tile)
       |  arithmetic    +,-,*,//,%  fold into .e via _index_with_same_tile (affine-only)
       |                scalar[int] -> _build_dynamic_index (runtime offset)
       v
  tile composition    combine_tiles(*tiles): unique_sequence -> broadcast
       |                par_shape & free_shape SEPARATELY (_broadcast_shape_with_expand_dims) -> NDTile
       |  index compose  TileIndex.combine_tile_with -> cached_combine_index_tiles (lru-cached fold)
       v
  canonicalisation    broadcast_shapes(indices): combine_tiles +
       |                generate_subst_map(src_axes, dst_axes)  (tripcount-gated, drop size-1)
       |                -> canonicalize_expr(i, subst_map) -> AffineExpr.substitute(subst_map)
       |                   (backend: bir::QuasiAffineExpr::substituteLoopAxes @0x5f93d0)
       v
  access pattern /    NDTile.par_addr / free_addr = ap_expr(canonical indices)
  mask                inst_mask(inst): substitute predicate indices onto dst tile
                      -> TileMaskIntersection  (-> predicates module)
```

### Key invariants

- **Index stays affine.** Only integer `+ - * // %` are legal on a `TileIndex`; multiply by a non-int (`_mul_impl`) and floordiv/mod by a non-int (`__floordiv__`/`__mod__`) raise, keeping `e` an `AffineExpr`.
- **Same-tile vs cross-tile.** Same-tile arithmetic rebuilds via `_index_with_same_tile`; cross-tile arithmetic goes through `combine_tile_with` → `cached_combine_index_tiles`.
- **Dynamic slices.** A runtime (`scalar[int]`) bound forces `step == 1` and uses `diff.supremum` as the static axis extent; a compile-time-constant difference uses `diff.c`.
- **Two-level broadcast.** `combine_tiles` broadcasts the partition shapes and free shapes independently — the partition axis is a distinct hardware dimension.
- **Substitution is positional and tripcount-gated.** `generate_subst_map` pairs axes by position and drops size-1 axes, so broadcast (`None`) axes are never substituted.
- **The fold is cached.** `cached_combine_index_tiles` is `lru_cache`d on the immutable `(exprs, extra_tiles)` tuple, so recurring nested-index shapes reuse the result.

---

## Related Components

| Name | Relationship |
|---|---|
| `predicates` module | Owns the mask algebra (`EQTileMask`, `TileMaskIntersection`); this module constructs masks but does not operate on them |
| penguin IR (`AffineExpr` / `LoopAxis`) | Provides `wrap_expr`, `implicit_cast_idx`, and the `substitute`/`substituteLoopAxes` backend |
| tensor / memref layer | Provides `generate_ap_index`, `filter_indices`, `ap_expr`, `n_elts`; consumes the per-dim index list from `translate_indices_to_list` |

## Cross-References

- [Memref View Model](memref-view-model.md) — how a tile's memref and AP view consume the indices this module infers
- [Mask Predicate Algebra](mask-predicate-algebra.md) — the `EQTileMask` / `TileMaskIntersection` algebra that `inst_mask` and the comparison operators feed
- [AffineExpr Algebra](../penguin/affine-expr-algebra.md) — the `AffineExpr.substitute` / `bir::QuasiAffineExpr::substituteLoopAxes` backend that re-bases loop axes
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the partition×free hardware geometry that `NDTile`'s two-level model mirrors
