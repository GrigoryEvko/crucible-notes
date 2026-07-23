# SPMD Programming Model: `program_id` / `num_programs` / `grid`

A NKI kernel is launched not once but as an **ND grid of independent SPMD
(Single-Program-Multiple-Data) programs**. `kernel[g0, g1, …](args)` says "run this
same kernel body once per point in the grid `g0 × g1 × …`"; each instance is one
*program*, and the only thing that distinguishes one program from the next is its
**position** in the grid. The entire SPMD surface — the four functions a kernel uses to
ask "where am I and how big is the grid", plus the dim notations that *build* the grid and
decide how its axes map onto physical NeuronCores — is what this page reconstructs.

The surface splits across **two source modules** and lowers into the Penguin IR's `Axis`
model (P5.3, [`penguin/axis-loop-model.md`](../penguin/axis-loop-model.md)):

* `neuronxcc/nki/language/programming_model.py` (`programming_model.so`) — the **query**
  side: `program_id`, `num_programs`, `program_ndim`. Reads the trace context.
* `neuronxcc/nki/language/iterators.py` (`iterators.so`) — `sync_program`, the **SPMD
  barrier**. Note the split: three queries live in `programming_model`, the barrier lives
  with the iterators.
* `neuronxcc/nki/compiler/backends/neuron/dimensions.py` (`dimensions.so`) — the **grid
  spec**: `Dim` / `ParDim` / `SPMDDim` / `CompositeSPMDDim`, the `AxisType` binding, and
  `grid_has_multi_physical_cores` (the LNC-mode predicate).
* `neuronxcc/nki/compiler/backends/neuron/scalars.py` (`scalars.so`) — the **symbolic
  scalars** a program's position resolves to: `DynamicScalar` → `ProgramId` / `LoopVar`.

Confidence tags follow the house convention: **CERTAIN** = verbatim interned string or
decompiled control flow; **HIGH** = multi-source symbol evidence; **MEDIUM** =
structure-derived. All `.py` line numbers are the original line numbers recovered from the
Cython `_Pyx_AddTraceback` strings embedded in each `.so`.

---

## 1. The shape of the model: grid → ProgramId

```
kernel[ <dim0>, <dim1>, … ](args)        # launch: an ND grid of SPMD programs
   │
   │  each dimᵢ ∈ {int, Dim, ParDim, SPMDDim, CompositeSPMDDim}   (dimensions.py)
   ▼
 for each (sub-)axis:
   penguin.ir.Axis  with  AxisType ∈ {SPMD, Shard, Thread, …}    (axis role)
   ProgramId        bound to that axis, carrying its tripcount    (scalars.py)
   ▼
 sema.program_ids = [ProgramId, ProgramId, …]   # one per grid axis, in order
   ▲
   │  read by:
 nl.program_id(axis)   → program_ids[axis]       # "which index am I"
 nl.num_programs(axes) → ∏ program_ids[a].num_programs   # "how big is the grid"
 nl.program_ndim()     → len(program_ids)         # "how many dims"
```

The kernel body is **traced once** (P6.1, the abstract-interpretation pass in
`TraceContext`); the grid does *not* unroll. `nl.program_id(0)` does not return a Python
`int` — it returns a **`ProgramId`**, a symbolic `uint64` scalar bound to grid axis 0. The
actual per-program value is filled in by the runtime, one launch per grid point. Everything
the kernel computes from `program_id` (e.g. `nl.program_id(0) * TILE + nl.arange(TILE)`)
is therefore *symbolic affine arithmetic over the grid index*, baked into the IR and
re-evaluated per program. This is the whole point of SPMD: one IR, N programs.

> **NOTE —** `ProgramId` is a subclass of `DynamicScalar` (§5), so it participates directly
> in tile-index arithmetic and can be promoted to a `TileIndex`. It is the symbolic glue
> between "where am I in the grid" and "which slice of the tensor do I touch".

---

## 2. The query functions (`programming_model.py`)

This module exports **exactly three** Cython functions — `program_id`, `num_programs`,
`program_ndim` — only these three `pyx_pw_…` wrappers exist. All three reach the
active trace context through `nki_ctx()` (P6.1.2) and read `ctx.sema.program_ids`, the
per-axis `ProgramId` list the trace builder populated when the grid was established.

### 2.1 `program_id(axis)` — `0xe140`, source ~L64

```c
// pyx_pw_…_program_id_0xe140 — symbol/subscript evidence:
//   refs: nki_ctx ×2, sema ×2 (GetAttr), program_ids ×1, normalize_dim ×2,
//         ProgramId ×2, err_program_id_axis_out_of_bounds, mp_subscript ×4 / PyObject_GetItem
PyObject* program_id(PyObject* axis) {
    ctx  = nki_ctx();                  // P6.1.2 — raises err_nki_api_outside_of_nki_kernel
                                       //          if not currently tracing a kernel
    sema = ctx->sema;                  // the IR semantic builder
    program_ids = sema->program_ids;   // list[ProgramId], one entry per grid axis
    i = normalize_dim(axis);           // int|Axis → non-negative axis index
    if (!(0 <= i && i < len(program_ids)))
        err_program_id_axis_out_of_bounds(...);   // raise (D-A13 error catalog)
    return program_ids[i];             // mp_subscript → ProgramId (symbolic uint64)
}
```

Docstring, interned verbatim:
> *"Index of the current SPMD program along the given axis in the launch grid."*

The `mp_subscript` (×4 in the decompile) is `program_ids[i]` — a plain list index. The
caller-facing `axis` is normalized first (`normalize_dim`) so a kernel can pass either a
positive index, a negative (Python-style) index, or a Penguin `Axis` handle.

### 2.2 `num_programs(axes=None)` — `0xf510`, source ~L37

```c
// accepts Union[int, Axis, List, ProgramId] ("axes is invalid type: " on miss),
//   n_elts accumulator, tripcount, ProgramId.num_programs, a num_programs.<locals>.genexpr
PyObject* num_programs(PyObject* axes /* = None */) {
    ctx = nki_ctx(); sema = ctx->sema;
    if (axes is None)
        axes = <all grid axes>;                 // total program count over the full grid
    assert_type(axes, (int, Axis, list, ProgramId));   // else "axes is invalid type: …"
    n_elts = 1;
    for (a in normalized(axes))                 // the .<locals>.genexpr
        n_elts *= program_id_for(a).num_programs;   // per-axis tripcount
    return n_elts;                              // product of per-axis program counts
}
```

Docstring:
> *"Number of SPMD programs along the given axes in the launch grid. If `axes` is not
> provided, returns the total number of programs."*

`num_programs()` with no argument is the grid's **total** program count; with `axes` it is
the **product of the tripcounts** of just those axes. The per-axis count comes from
`ProgramId.num_programs` (§5.2) — the tripcount the axis was created with.

### 2.3 `program_ndim()` — `0xb230`, source ~L18

```c
PyObject* program_ndim(void) {        // references nki_ctx + the grid-length helper
    ctx = nki_ctx();
    return len(<launch grid axes>);   // == len(program_ids) == grid.ndim
}
```

> *"Number of dimensions in the SPMD launch grid, i.e. the number of axes."*

> **GOTCHA —** all three functions call `nki_ctx()` with `allow_none=False`, so calling
> `nl.program_id(0)` **outside a traced kernel** raises `err_nki_api_outside_of_nki_kernel`
> rather than returning a default. There is no "I'm program 0 of a 1-program grid" fallback
> at module scope — the grid only exists inside a trace.

---

## 3. The dim notations: how the grid is built (`dimensions.py`)

> *"Define the classes for dimension notations"* (module docstring).

A launch grid passed to `kernel[…]` is a tuple of **dim specifiers**, each one of five
kinds. The kind decides the Penguin `AxisType` the axis lands on, which in turn decides how
the program instances along that axis are physically distributed.

| Spec | Class / source | Penguin `AxisType` | Physical meaning |
|---|---|---|---|
| `4` (plain int) | — | `SPMD` | ordinary replica axis; 4 independent programs |
| `Dim(4)` | `Dim` | `SPMD` | sized axis, no special mapping (`int(Dim)` → size) |
| `ParDim(128)` | `ParDim(Dim)` | (partition) | **the** on-chip partition / SBUF axis, pinned |
| `nc(2)` | `SPMDDim` | `Shard` | sharded across **2 physical NeuronCores** (LNC) |
| `cc_pipeline(m)` | `SPMDDim` | (CC-pipeline) | fused with producer/consumer + CC pipelined |
| `nc(2)*cc_pipeline(2)*4` | `CompositeSPMDDim` | per-sub-axis | one logical dim, tiled into sub-axes |

> **NOTE — the NKI grid spec only ever names three `AxisType` members.** The enum itself has
> nine — `{Default, Parallel, Sequential, Shard, Thread, Block, CCRank, Interleave, SPMD}` —
> but it is defined in Penguin's `Axis.so` (P5.3,
> [`penguin/axis-loop-model.md`](../penguin/axis-loop-model.md)), not here. `dimensions.so`
> merely `_Pyx_ImportFrom`s `AxisType` and references exactly three members by name:
> **`SPMD`** (line 1083), **`Shard`** (line 1670), **`Thread`** (line 1813). Those are the
> only three an NKI grid spec ever constructs — plain `int`/`Dim` → `SPMD`, `nc()` → `Shard`,
> a thread dim → `Thread`. `Block` / `CCRank` / `Interleave` are real axis types consumed by
> the tiler, but they never appear in the dim-notation surface.

### 3.1 `Dim` and `ParDim` — `0x11230` / `0x106b0`

```c
class Dim:
    value: int
    int(self) -> self.value          // Dim.__int__ @ 0x11230 — a Dim is usable
                                      //   anywhere an int grid extent is expected
class ParDim(Dim):                    // "Mark a dimension explicitly as a partition dimension."
    repr(self) -> "par_dim({value})"  // (kp_u "par_dim", value) @ 0x106b0
```

`ParDim` is how a kernel author explicitly pins *which* grid axis is the hardware partition
dim (the 128-partition P axis of SBUF/PSUM, P4 `arch/sbuf-psum-geometry.md`) rather than
letting the compiler infer it.

### 3.2 `SPMDDim` — the axis-typed dim — `__mul__` @ `0x17020`

This is the central class: a sized dim that **carries an explicit `AxisType`**, the value
that tells the IR what role the axis plays.

```c
class SPMDDim:
    value:     int                       // dimension size
    axis_type: AxisType                  // {SPMD, Shard, Thread} — the NKI-visible subset

    bool has_axis_type(self, axis_ty):   // @ 0xffe0
        return self.axis_type == axis_ty

    void check_mul_operand(self, other): // @ 0x11d20
        // require other be SPMDDim/int, else:
        raise TypeError("unsupported operand type(s) for * : …");   // interned

    SPMDDim* __mul__(self, other):       // @ 0x17020 (refs CompositeSPMDDim ×2)
        self.check_mul_operand(other);
        return CompositeSPMDDim(self, other);    // compose, don't multiply numerically
    __rmul__(self, other): …             // @ 0x15330, symmetric (so `8 * nc(2)` works)
```

The decompile of `__mul__` (`0x17020`) references `CompositeSPMDDim` twice and
`check_mul_operand` once — confirming that `*` on an `SPMDDim` **does not collapse to a
number**; it builds a `CompositeSPMDDim` node that records the *factorization* of the axis
so each sub-factor can keep its own `AxisType`.

### 3.3 `CompositeSPMDDim` — the LNC composition syntax — `tile_axis` @ `0x132f0`

> *"CompositeSPMDDim — Represent the composition of spmd dimensions. CompositeSPMDDim allow
> users to write composition of spmd dimension like `nc(m0) * cc_pipeline(m1) * m2`, which
> mean the dimension size is `m0 * m1 * m2`, the grid axis is tiled into `(m0, m1, m2)`,
> where the tiled `m0` dimension will be sharded across `m0` physical neuron cores; the
> tiled `m1` dimension will be fused with the producer/consumer and apply CC pipelining;
> the tiled `m2` dimension is a normal spmd dimension."* (docstring, verbatim).

```c
class CompositeSPMDDim:
    // holds the ordered list of sub-SPMDDims (lhs * rhs * …)
    bool has_axis_type(self, axis_type):     // @ 0x16240
        return any(d.has_axis_type(axis_type) for d in <subdims>);   // genexpr

    tile_axis(self, …):                      // @ 0x132f0
        // split the composite into ordered (lhs, rhs, …) sub-axes; each sub-axis gets its
        // own AxisType + tripcount via spmd_axis_type.
        // refs: lhs, rhs, axis, axis_type, tile, tripcount, spmd_axis_type
```

This is the **`nc()*degree` LNC math** the task asks about, stated precisely. Writing

```python
flash_fwd[nc(2) * 8, 8](args)
```

does **not** multiply 2×8 into a flat extent-16 axis. It builds a `CompositeSPMDDim` whose
*size* is `2 * 8 = 16`, but which the compiler `tile_axis`-splits into two ordered
sub-axes: an inner `Shard` sub-axis of size **2** (the 2 physical NeuronCores the kernel is
sharded over) and an outer `SPMD` sub-axis of size **8** (8 ordinary replicas *within* each
core). The grid extent is the **product**; the *role* of each factor is preserved. The
kernel body never changes — `nl.program_id` still sees one logical axis — but the placement
across hardware changes with the factorization. This is how a single NKI kernel scales from
one NeuronCore to a Logical-NeuronCore (LNC) ensemble without a rewrite.

### 3.4 Factory / helper functions

```c
spmd_dim(*args):            // @ 0x17f30
    // "Create a dimension in the SPMD launch grid of a NKI kernel with sub-dimension
    //  tiling." Guard: "spmd_dim expect at least 1 argument!" (interned).
    // Builds an SPMDDim/CompositeSPMDDim from sub-dim sizes — the constructor behind
    // the nc()*…*… sugar.

spmd_axis_type(i):          // @ 0xf2e0 (refs i, SPMDDim, axis_type)
    return <AxisType of the i-th sub-dim>;   // maps a sub-dim position → Penguin AxisType

serialize_dims(dims):       // @ 0xef00
    // serialize the dims tuple into the kernel's grid metadata / IR
```

The module also interns the docstrings *"Create a logical neuron core dimension in launch
grid."* and *"Create a dimension in launch grid for cc pipelining"* — the `nc()` and
`cc_pipeline()` factories.

> **GOTCHA — `nc` and `cc_pipeline` are not functions in `dimensions.py`.** Their docstrings
> are interned in `dimensions.so`, which makes them look module-level, but there is no
> `__pyx_n_s_nc` or `__pyx_n_s_cc_pipeline` in that module-exec. The four dim functions the
> module actually exports are `spmd_dim`, `spmd_axis_type`, `serialize_dims`, and
> `grid_has_multi_physical_cores`. The `nc()` / `cc_pipeline()` user sugar is built on
> `spmd_dim` — which constructs the `SPMDDim`/`CompositeSPMDDim` with the right `AxisType` —
> and is exposed through the `nl.*` language namespace.

Only the `nc → Shard` binding is read directly (via `grid_has_multi_physical_cores`, §3.5);
the per-factory `AxisType` binding for `cc_pipeline` is HIGH rather than pinned.

### 3.5 `grid_has_multi_physical_cores(grid)` — the LNC-mode predicate — source L179

```c
// decompiled genexpr of grid_has_multi_physical_cores
//   refs (inside the genexpr): has_axis_type ×1, AxisType ×2, Shard ×1
bool grid_has_multi_physical_cores(grid):
    return any(dim.has_axis_type(AxisType.Shard) for dim in grid);   // genexpr
```

This is the single most decisive piece of confirmed evidence for the LNC mapping: the
predicate that decides "is this a multi-NeuronCore launch" keys **specifically on
`AxisType.Shard`**. A grid is multi-physical-core iff *some* dim carries the `Shard` axis
type — i.e. some `nc(k>1)`. The trace context stores the result in
`TraceContext.multi_physical_cores` (P6.1, set at `__init__`). So the chain is exact:

```
nc(k)  →  SPMDDim(axis_type=Shard)  →  grid_has_multi_physical_cores → True
       →  TraceContext.multi_physical_cores = True   (kernel is LNC / multi-core)
```

---

## 4. The SPMD barrier: `sync_program` (`iterators.py`)

```c
// pyx_pw_…_sync_program @ 0x12d30 (refs inactive_program_axes ×2, nki_ctx ×2)
PyObject* sync_program(PyObject* inactive_program_axes /* = … */) {
    ctx = nki_ctx();
    return ctx->sync_program(inactive_program_axes);   // delegate to the builder
}
```

`sync_program` is the cross-program synchronization primitive — a **barrier across the
active SPMD programs of the launch grid**. Its one parameter, `inactive_program_axes`, names
the grid axes that are *not* participating: the barrier synchronizes the programs of the
*complement* (the active axes). The thin language-level wrapper delegates to
`ctx.sync_program` on the builder; the exact emitted IR op is on the builder side (INFERRED
to be a barrier/sema op — it is reached through `ctx.sync_program`, not directly emitted in
this wrapper). This is the primitive a kernel reaches for when programs sharing a tensor
must agree on a point in time (e.g. one program writes, all read).

> **GOTCHA — `sync_program` is not in `programming_model`.** The four SPMD-grid APIs look
> like one family but ship in two modules: `programming_model.so` exports exactly three
> `pyx_pw_…` wrappers (`program_id`, `num_programs`, `program_ndim`) and no fourth, while the
> `sync_program` wrapper at `0x12d30` lives in `iterators.so`.

---

## 5. The symbolic scalars (`scalars.py`)

> *"Define the implementations of scalar types."* (module docstring).

A program's grid position is not a number during tracing — it is a `ProgramId`, a subclass
of `DynamicScalar`. `DynamicScalar` is the symbolic-integer base that all data-dependent
addressing flows through.

### 5.1 `DynamicScalar` — base — `__init__` @ `0x18a80`

```c
class DynamicScalar:
    value: CExpr               // symbolic expression (wrap_expr-wrapped)
    dtype: np.dtype            // default np.int64
    __init__(self, value, dtype=np.int64):   // value, dtype, implicit_cast_idx,
                                             //            wrap_expr, np.int64
```

Arithmetic methods: `__add__ __sub__ __mul__ __neg__ __floordiv__ __mod__
__radd__ __rsub__ __rmul__ __iadd__ __imul__ __hash__`. `_add_impl`/`_mul_impl` build a
symbolic `CExpr` and `promote_type(self.dtype, other)`; scalar±scalar is integer-only
(guards *"'%' between scalar and non integer is not supported."*, *"'//' between scalar and
non integer is not supported."* — both interned).

**Comparisons return predicates, not bools** — `__lt__ __le__ __gt__ __ge__` each call
`_generate_predicate(other, pred_fn)` (`0x16900`) returning a `ScalarPredicateIntersection`.

```c
__lt__(self, other): return self._generate_predicate(other, pred_lt);   //
```

> **GOTCHA —** this is *why* `if nl.program_id(0) < n:` is rejected. `program_id(0) < n`
> evaluates to a **predicate object**, not a Python `bool`, so `TraceContext.eval_if_cnd`
> (P6.1) raises *"dynamic control flow not supported"*. SPMD divergence on a program index
> is not expressed with Python `if`; it must use masking / tensor-predicate ops.

Promotion chain (interned comment `OTHER -> SCALAR -> TILE_INDEX`):
`_promote_to_predicate` → `EQScalarPredicate`; `_promote_to_mask` → `EQTileMask`;
`_promote_to_tile_index` → `TileIndex(NDTile(self.value))` (`0x19850`). So a `ProgramId` can
become a tensor index (to slice), a predicate (to mask), or a tile mask.

### 5.2 `ProgramId(DynamicScalar)` — the grid index — `__init__` @ `0x12650`

```c
class ProgramId(DynamicScalar):              // HIGH: subclass (shared metaclass/bases +
                                             //   shared value/dtype init + promotion surface)
    __init__(self, program_axis, num_programs):
        // program_axis : AffineAxis  (the penguin grid axis)
        // num_programs : the axis tripcount
        // value = symbolic index over [0, num_programs); dtype = np.uint64
    @property program_axis(self): return self._program_axis;   // @ 0xeeb0
    @property num_programs(self): return self._num_programs;   // @ 0xec20
```

`ProgramId` is exactly what `nl.program_id(axis)` (§2.1) returns: a **`uint64`** symbolic
scalar bound to one launch-grid `AffineAxis`, carrying that axis's program count. The
`num_programs(axes)` total (§2.2) is the product of each axis's `ProgramId.num_programs`.
Being a `DynamicScalar`, a `ProgramId` indexes tiles directly:
`nl.program_id(0) * TILE + nl.arange(TILE)` is a legal symbolic affine expression.

### 5.3 `LoopVar(DynamicScalar)` — the loop iteration var — `__init__` @ `0x1d4d0`

```c
class LoopVar(DynamicScalar):                // HIGH — subclass
    __init__(self, loop_scope, …):           // loop_scope, items, np.uint64, dtype
        // dtype = np.uint64
    @property loop_scope(self): return self._loop_scope;   //
```

`LoopVar` is the symbolic iteration variable yielded by `nl.affine_range` /
`nl.sequential_range` — the **non-unrolled** iterators (P6.1.3). For those a *single*
`LoopVar` stands for the whole loop axis (in contrast to `static_range`, which unrolls and
hands out concrete Python `int`s). `ProgramId` and `LoopVar` are siblings: a `ProgramId`
indexes the *grid* (cross-program, set by launch); a `LoopVar` indexes a *loop axis*
(within-program, set by iteration). Both are symbolic `uint64` `DynamicScalar`s, both index
tiles, both are inter-program/loop opaque during tracing.

---

## 6. End-to-end: from `kernel[…]` to `program_ids`

```
1. kernel[<grid dims>](args)
     dimsᵢ ∈ {int, Dim, ParDim, SPMDDim, CompositeSPMDDim}   (dimensions.py §3)
     grid_has_multi_physical_cores(grid) → any dim is AxisType.Shard → LNC mode  (§3.5)

2. TraceContext.new_ctx(opts) installs the singleton global_ctx; trace_kernel walks the
   kernel body once under a KernelScope.                                          (P6.1)

3. Per (sub-)axis the builder makes a penguin.ir.Axis (AxisType from spmd_axis_type) and
   a ProgramId(program_axis, num_programs)  →  sema.program_ids = [ProgramId, …]   (§5.2)

4. Inside the body, every nl.* op calls nki_ctx() → ctx.sema (the builder) and
   get_cur_scope() (the active emission scope).                                  (P6.1.2)

5. nl.program_id(axis)   → program_ids[axis]            (§2.1)
   nl.num_programs(axes)  → ∏ program_ids[a].num_programs (§2.2)
   nl.program_ndim()      → len(program_ids)            (§2.3)
   nl.sync_program(inactive_axes) → barrier over active programs  (§4, iterators.py)

6. The ProgramId flows through DynamicScalar arithmetic → TileIndex (§5.1), giving each
   program its own slice of the tensors — the SPMD payload.
```

---

## 7. Where the model is pinned

Four mechanisms carry the weight of this page, and each is read directly out of a
decompiled body rather than inferred from names:

| Claim | Anchor |
|---|---|
| `program_id` returns `program_ids[axis]`, a `ProgramId` | `program_id` @ `0xe140` — `program_ids` ×1, `mp_subscript` ×4 (the list index), `ProgramId` ×2, `normalize_dim` ×2, `err_program_id_axis_out_of_bounds`, `sema` ×2 |
| `SPMDDim.__mul__` builds a `CompositeSPMDDim`, not a numeric product | `__mul__` @ `0x17020` — `CompositeSPMDDim` ×2, `check_mul_operand` ×1 |
| `grid_has_multi_physical_cores` keys on `AxisType.Shard` | its genexpr — `has_axis_type` ×1, `AxisType` ×2, `Shard` ×1 |
| `sync_program` lives in `iterators.so` | wrapper @ `0x12d30`, `inactive_program_axes` ×2; `programming_model.so` has only three `pyx_pw_…` exports |

The third row is the strongest statement available about the `nc → Shard` binding: LNC mode
holds precisely when some grid dim carries the `Shard` axis type, and the genexpr says so
literally.
