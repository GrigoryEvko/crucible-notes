# Penguin Axis / Loop-Axis Model

> *All symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The loop-axis classes live in `neuronxcc/starfish/penguin/ir/Axis.cpython-310-x86_64-linux-gnu.so` (a Cython module shipped **with** debug-info, **not** stripped — its public method roster is recoverable byte-for-byte from `__pyx_pw_…` wrapper symbols). The umbrella registry is `ir.cpython-310…so`. Other wheels differ.*

## Abstract

Penguin — the Python middle-end that hlo2penguin emits into and that the Tonga tiler rewrites — has **no `scf.for` and no CFG loop**. A loop is a first-class IR object: an `Axis`. A nested loop-nest is a *tree of `Axis` objects* hung off each `Instruction` (the `axes` / `loopnest` / `loopdepth` triple on `Instruction`, P5.1 §2.2), and the affine address of every tensor access is a quasi-affine expression over those axes' induction variables. If you know MLIR's `affine` dialect, the mental model is exact: an `Axis` is an `affine.for` whose body is implicit and whose iteration domain is `range(lb, ub, stride)`; the difference is that Penguin attaches the *parallelism/sharding role* (`Parallel` / `Sequential` / `Shard` / `Thread` / `Block` / `CCRank` / `SPMD`) directly to the axis as an `AxisType`, rather than inferring it from a dependence test.

The class is `Axis`, base of three concrete forms confirmed as nested subclasses in `Axis.so`: **`AffineAxis`** (the canonical static loop — `lb`/`ub`/`stride` are compile-time affine expressions; 63 pyx methods, by far the dominant class), **`DynamicAxis`** (the data-dependent loop — `ub` is a runtime value), and **`SequentialAxis`** (an ordered, non-parallel axis). A fourth subclass, **`Filter`**, brackets a predicated sub-domain. The `AxisType` enum — an integral `Enum` (the module interns both `Enum` and `Integral`) — carries the role; conversion between roles (`as_shard_axis`, `as_thread_axis`, `parallelize`, …) is gated by legality checks whose diagnostics are recoverable verbatim.

This page reconstructs the class hierarchy, the `AxisType` enum and its `is_*`/`as_*` query/conversion surface, the verbatim serialize form (`for (it: range(lb, ub, stride))`), and how a loop-nest plus its parallelism/sharding semantics is represented and consumed by the tiler. Everything here is grounded in `Axis.so`'s pyx symbol table, string pool, and the D-U08 node-model report; tags follow the house convention (CONFIRMED = verbatim symbol/string; STRONG = multi-source; INFERRED = structure-derived).

For reimplementation, the contract is:

- The `Axis` hierarchy and which fields (`iv`, `lb`, `ub`, `stride`, `name`, `tripcount`) live on `AffineAxis`.
- The `AxisType` enum membership and the `is_<type>` / `as_<type>_axis` query/conversion surface, including the legality guards.
- How a nested loop-nest is materialized as a tree of `Axis` objects on each `Instruction`, and how the affine address algebra (`AffineExpr` over each axis `iv`) binds to it.
- How `AffineAxis` (static) and `DynamicAxis` (runtime-bounded) split the bounded-vs-data-dependent loop space, and why `DynamicAxis` is *not* the same use-def chain as `Axis`.

| | |
|---|---|
| **Module** | `neuronxcc/starfish/penguin/ir/Axis.py` → `Axis.cpython-310…so` (debug-info, not stripped) |
| **Base class** | `Axis` (15 pyx methods) |
| **Subclasses** | `AffineAxis` (63), `DynamicAxis` (2), `SequentialAxis` (1), `Filter` (8), `AxisHandle` (6) |
| **Enum** | `AxisType` — integral `Enum`; members `{Default, Parallel, Sequential, Shard, Thread, Block, CCRank, Interleave, SPMD}` |
| **Serialize form** | `for ({it}: range({lb}, {ub}, {stride}))` (verbatim `__pyx_k_` template) |
| **Imports** | `pelican.ir`, `AffineExpr`, `AffinePredicate`, `Instruction`, `Stmt`, `typing` |
| **Loop-nest carrier** | `Instruction.axes` / `loopnest` / `loopdepth` (P5.1 §2.2) |

---

## The Class Hierarchy

### Purpose

`Axis` is the loop-dimension node: one `Axis` is one loop level. A two-deep nest `for i: for j:` is two `Axis` objects, and an `Instruction` inside the nest carries both in its `axes` list (outer-to-inner), with `loopdepth` recording its nesting level. There is no separate loop *header* object — the `Axis` *is* the loop, and the body is whatever `Instruction`s name that axis. This mirrors the pelican C++ `AffineIdx` parent-chain (P5.5 §5.2) whose verbatim assertion `loopdepth == (parent ? parent->getLoopdepth() + 1 : 0)` proves the index chain is exactly the loop-nesting chain.

### Hierarchy

```text
Axis                       (ir/Axis.py — base; 15 pyx methods)
 ├─ AffineAxis             — canonical STATIC loop: lb/ub/stride are affine exprs (63 methods)
 ├─ DynamicAxis            — data-dependent loop: ub is a runtime IntRuntimeValue (2 methods)
 ├─ SequentialAxis         — an ordered (non-parallel) axis (1 method)
 ├─ Filter                 — a predicated sub-domain bracket (8 methods)
 └─ AxisHandle             — a (coef · axis) use-counted reference (6 methods)

AffineAxisCollection       (ir.so) — an ordered set of AffineAxes = one loop-nest
AffineIV / AffineIdx       (ir.so) — the induction-variable value / affine-index types
AffineIndicesSet           (ir.so) — a set of AffineIdx
SymbolicIdx                (ir.so) — a compile-time-unknown index
```

> **NOTE —** the subclass count comes straight from the pyx symbol table. `nm Axis.so | rg __pyx_pw_…` resolves to method wrappers tagged with their owning class (`…_4Axis_10AffineAxis_<idx><method>`), so the per-class roster sizes — `AffineAxis` 63, `Axis` 15, `Filter` 8, `AxisHandle` 6, `DynamicAxis` 2, `SequentialAxis` 1 — are CONFIRMED, not inferred. `AffineAxis` dominating the roster (63/95 methods) confirms the static affine loop is the canonical form; `DynamicAxis`/`SequentialAxis` are thin specializations that override only `serialize_block_begin` and the bound semantics.

### `Axis` base — the shared loop contract

The base `Axis` (15 CONFIRMED methods) is the loop-level contract every subclass satisfies:

| Group | Methods (CONFIRMED pyx wrappers) | Role |
|---|---|---|
| Construction / copy | `__init__`, `__recursively_copy_children` | build; deep-copy the child axis sub-tree |
| Trip count / range | `tripcount`, `max_tripcount`, `min_value`, `max_value`, `aligned_tripcount` | the iteration domain bounds |
| Tiling | `tile`, `__tile_impl`, `num_tiles` | split this axis into tile-sized sub-axes (the tiler's cut) |
| Unroll | `unroll` | fully/partially unroll the loop |
| Domain | `generateDomains` | enumerate the iteration domain (for codegen) |
| Range narrowing | `tighten_range_from_predicates`, `has_same_shape` | shrink `[lb,ub)` using guarding `AffinePredicate`s |
| Serialize | `serialize_block_begin` | emit the `for (…)` block header |

Beyond these, the report (D-U08 §4.1) records the role-query and mapping helpers that the string pool confirms present in `Axis.so`: `has_shard_axis`, `has_thread_axis`, `has_thread_or_shard_axis`, `is_spmd_or_shard`, `is_thread_or_cc_axis`, `allow_output_dependencies`, `track_axis`, `map_axis`, `map_iv`, `map_and_filter_axis` (all CONFIRMED as interned identifiers). These let a pass ask of a whole sub-tree "does any axis below me shard/thread?" without walking the role of each individually.

> **QUIRK —** `tile` / `__tile_impl` / `num_tiles` live on the *base* `Axis`, not on a separate tiling pass object. Tiling in Penguin is an axis-level operation: the tiler (D-U02) calls `axis.tile(size)`, which splits one `Axis` into an outer block-axis and an inner tile-axis, mutating the loop-nest tree in place. A reimplementer who models tiling as an external rewrite over a flat loop list will not match the IR's shape — the tile boundary is encoded *in the axis tree itself*.

---

## AffineAxis — the Canonical Static Loop

### Purpose

`AffineAxis` is the workhorse: a loop whose lower bound, upper bound, and stride are compile-time **affine expressions** (P5.5 `pelican::AffineExpr` over outer axes' induction variables). Its induction variable `iv` is the symbol the access-pattern addresses are written against; the affine arithmetic operators on `AffineAxis` build `AffineExpr` terms over that `iv`.

### Fields

```c
class AffineAxis(Axis):       // ir/Axis.py
    iv          // induction variable — an AffineIV / AffineIdx (CONFIRMED getter)
    lb          // lower bound — an affine Expr  (CONFIRMED getter)
    ub          // upper bound — an affine Expr  (CONFIRMED getter)
    stride      // step          (CONFIRMED getter)
    name        // the loop variable name string
    tripcount   // (ub - lb + stride - 1) // stride  — the static iteration count
```

All four of `iv`, `lb`, `ub`, `stride` are CONFIRMED pyx getters on `AffineAxis`; `tripcount`/`max_tripcount` are inherited from `Axis`. The string-pool interns `stride`, `tripcount`, `max_tripcount`, `min_value`, `max_value` directly (verified via `strings`).

### Serialize — a Python `for`-over-`range`

The textual form of an `AffineAxis` is recovered verbatim from the `__pyx_k_` format template:

```text
{space_indent}{_label}{attr}for ({it}: range({lb}, {ub}, {stride}))
```

So a Penguin loop axis *is* a Python `for`-over-`range`, and a loop-nest prints as nested `for (…: range(…))` blocks. `{it}` is the iteration variable (`iv.name`), and `{lb}`, `{ub}`, `{stride}` are the serialized affine bounds. `serialize_block_begin` (overridden per subclass: `Axis`, `AffineAxis`, `DynamicAxis`, `Filter` each have their own) emits this header.

> **NOTE —** the `range(lb, ub, stride)` form is a *half-open* domain `[lb, ub)` with step `stride`, exactly like Python's `range`. `tripcount` is therefore `ceildiv(ub - lb, stride)`. The NKI range semantics page (P6.1.3) documents the user-facing `nl.affine_range` / `nl.sequential_range` that lower to these axes; `affine_range` → `AffineAxis` with `AxisType.Parallel`, `sequential_range` → `AxisType.Sequential`.

### Affine arithmetic over the `iv`

`AffineAxis` overloads the arithmetic operators to build `AffineExpr` terms over its induction variable (CONFIRMED pyx wrappers `__add__`/`__radd__`/`__sub__`/`__rsub__`/`__mul__`/`__rmul__`/`__neg__`/`__floordiv__`, plus `_add_impl`/`_mul_impl`):

| Operator group | Methods | Builds |
|---|---|---|
| Add / sub | `__add__`, `__radd__`, `_add_impl`, `__sub__`, `__rsub__` | `SumExpr` term |
| Mul | `__mul__`, `__rmul__`, `_mul_impl` | `MultExpr` (coef · iv) |
| Mod / floordiv | `__mod__`, `__floordiv__` | `ModuloExpr` / `FloorDivExpr` |
| Negate | `__neg__` | negated term |
| Shift / pad | `shift_axis`, `pad_axis` | re-based / edge-padded axis |
| Simplify | `simplify`, `linearizedExpr` | fold; delinearize a nested loop-nest into one flat affine expr |

`linearizedExpr` carries the docstring "Get the delinearized Affine Expr out of all the [loops]" — i.e. it flattens a whole nest of `AffineAxis` iteration variables into a single linear index, which is exactly what address-linearization needs at the tile boundary (P5.5 §5.5).

---

## The AxisType Enum and the Role Surface

### Purpose

An axis's *role* — what kind of parallelism or sharding it expresses — is an `AxisType`, stored on the axis and queried/mutated through a uniform `is_<type>` / `as_<type>_axis` surface. This is the single most important fact for a reimplementer: **parallelism and sharding semantics are a property of the axis, set explicitly, not derived from a dependence analysis.** The tiler and scheduler read `AxisType` to decide which axis becomes the 128-partition (P) dim, which becomes a free (F) dim, which is sharded across LNC cores, and which iterates over collective-comm ranks.

### Members

`AxisType` is an integral `Enum` (the module interns both `Enum` and `Integral`; references appear as `Optional['AxisType']`). The nine members are CONFIRMED as interned identifiers in `Axis.so`'s string pool, and each has a paired `is_<type>` predicate whose docstring is recovered verbatim (`True if loop type is <type>.`):

| Member | `is_*` predicate | `True if loop type is …` (verbatim) | Meaning |
|---|---|---|---|
| `Default` | `is_default` | `default` | unclassified / not-yet-assigned axis |
| `Parallel` | `is_parallel` | `parallel` | independent iterations — vectorizable / partition-mappable |
| `Sequential` | `is_sequential` | `sequential` | ordered iterations — carries a loop-carried dependence |
| `Shard` | `is_shard` | `shard` | LNC shard dim — split across Logical-Neuron-Core shards |
| `Thread` | `is_thread` | `thread` | LNC thread dim — split across the threads of a core |
| `Block` | — | — | block-tiling outer dim (the per-tile block index) |
| `CCRank` | `is_ccrank` | `ccrank` | collective-comm rank loop (one iter per participating rank) |
| `Interleave` | `is_interleave` | `interleave` | interleaved iteration (e.g. ping-pong double-buffer) |
| `SPMD` | `is_spmd` | `spmd` | SPMD replica dim |

> **GOTCHA —** there is no `is_block` predicate in the pyx roster (the `Block` role is queried differently — through the tiler's block-dim helpers on `NeuronBlockTensor`, P5.3-tensor §3.4, not through `Axis.is_block`). A reimplementer enumerating `is_<type>` methods will find eight, not nine; the ninth role (`Block`) is real but lacks a symmetric predicate. Do not assume the enum membership and the predicate set are 1:1.

### Ordinals — assigned by declaration order, not interned

The enum *member names* are interned, but the *ordinal values* are not stored as literals in the binary — `AxisType` is a Python `Enum`, so each member's integer value is assigned at class-definition time by enumeration order. The canonical declaration order recovered from the report (D-U08 §4.2) is `{Default, Parallel, Sequential, Shard, Thread, Block, CCRank, Interleave, SPMD}`, which gives ordinals `Default=0, Parallel=1, Sequential=2, Shard=3, Thread=4, Block=5, CCRank=6, Interleave=7, SPMD=8`.

> **CORRECTION (AXIS-1) —** the *ordinal assignment* above is INFERRED from declaration order, not read as a literal table from the binary. The member *names* and their pairing with `is_<type>` predicates are CONFIRMED (verbatim strings); the integer values are STRONG-at-best because a `unique`/explicit-value decorator could renumber them. Treat the names as authoritative and the ordinals as the natural enumeration unless a downstream BIR `LoopAxis` table (Part 7) pins a different value.

### Conversion — `as_<type>_axis` with legality guards

Re-tagging an axis is done through `convert_axis_type` / `as_axis_type` and the per-role converters (all CONFIRMED pyx wrappers on `AffineAxis`):

```c
function as_shard_axis(self):           // AffineAxis
    if self.is_sequential or self.is_shard:
        fatal("Cannot parallelize sequential/shard axis!")   // CONFIRMED diag string
    self.type = AxisType.Shard
    return self

function as_interleave_axis(self):       // AffineAxis
    if self.is_sequential:
        fatal("Cannot interleave sequential axis!")          // CONFIRMED diag string
    self.type = AxisType.Interleave
```

The full converter set: `as_axis_type`, `convert_axis_type`, `as_sequential_axis`, `as_shard_axis`, `as_thread_axis`, `as_ccrank_axis`, `as_interleave_axis`, plus `parallelize` / `parallel`. The legality diagnostics recovered verbatim:

- `Cannot parallelize sequential/shard axis!` — a `Sequential` or `Shard` axis cannot be re-marked parallel.
- `Cannot interleave sequential axis!` — a `Sequential` axis cannot be interleaved.
- `expect shard axis on top, but got ` — a shard axis must be the outermost loop in the nest (the LNC shard split happens at the top).
- `Expect AffineAxis, got:` — several converters reject non-affine (e.g. `DynamicAxis`) inputs.

> **QUIRK —** "expect shard axis on top" encodes a structural invariant: the `Shard` axis must be the *outermost* axis of the loop-nest, because LNC sharding partitions the whole iteration space across cores before any inner tiling. A reimplementer who allows a shard axis at an inner level will produce an unschedulable nest — the shard split must dominate every other axis.

### Predicate-driven narrowing

Two role-adjacent methods — `require_predicate` and `used_by_predicate` — connect an axis to the `AffinePredicate` guard model (P5.1 §2.2): an `Instruction` inside a partially-covered tile carries `AffinePredicate`s over the loop axes, and `tighten_range_from_predicates` shrinks an axis's `[lb,ub)` using those guards. This is how Penguin masks the boundary tile of a non-evenly-divisible loop *without* a CFG branch — the predicate is an affine guard on the axis, not an `scf.if`.

---

## DynamicAxis — the Data-Dependent Loop

### Purpose

`DynamicAxis` is the affine loop whose **upper bound is a runtime value** — a `pelican::IntRuntimeValue` (P5.5 §5.2), not a compile-time constant. It models a data-dependent trip count (e.g. a sequence length read from a tensor) while keeping the loop *affine in form* so the address algebra still works.

### Methods and invariants

`DynamicAxis` has only two pyx methods — `__init__` and `serialize_block_begin` — so it is a thin specialization of `AffineAxis`-shaped bounds where `ub` is runtime-valued. Its invariants are recovered verbatim from diagnostics:

- `Dynamic Axis must be in canonical form` — a `DynamicAxis` must be normalized (lb=0, stride=1) before lowering.
- `Dynamic Axis must have a runtime-valued upper bound` — the defining property: `ub` is an `IntRuntimeValue`.
- `Loops have a dynamic upper bound; the use-def chain of \`DynamicAxis\` and \`Axis\` are not the same` — the data-dependent `ub` introduces an SSA def-use edge from the value computing the bound *into* the axis, which a plain static `Axis` does not have.

> **GOTCHA —** the third diagnostic is the trap. A `DynamicAxis`'s runtime `ub` is a real SSA value with its own producers, so the def-use chain you must walk to find the loop's dependences is **not** the same as for a static `Axis` — you have to chase the bound-computing instruction as well as the body. A pass that walks only the body's tensor def-use (P5.3-tensor §3.1) will miss the dependence on the value that sizes the loop.

`DynamicAxis` is the Penguin twin of the BIR `BirDynamicForAxis` / `DynamicForLoopAxis` (Part 7); `BirCodeGenLoop` lowers a `DynamicAxis` into the BIR dynamic-for construct, carrying the runtime bound across the Python→C++ boundary.

> **NOTE —** Penguin has *two* ways to express a data-dependent loop, and they are not interchangeable. `DynamicAxis` is for an **affine-bounded** loop with a runtime trip count (the address algebra still holds). The structured `While` node (P5.1 §7.6 — `guard` + `continue_condition` + `body`, docstring "A while loop, with the following cannonical structure") is for a **truly unstructured** data-dependent loop with no affine domain. Choose `DynamicAxis` when only the bound is dynamic; choose `While` when the iteration itself is condition-driven.

---

## SequentialAxis, Filter, and AxisHandle

### SequentialAxis

`SequentialAxis` (1 pyx method) is an axis pinned to `AxisType.Sequential` — an *ordered* loop that carries a loop-carried dependence and therefore cannot be parallelized or sharded (the `Cannot parallelize sequential/shard axis!` guard rejects exactly this). It is the bound form of `AxisType.Sequential`; most code reaches it through `axis.as_sequential_axis()` rather than constructing it directly.

### Filter

`Filter` (8 pyx methods: `__init__`, `serialize_block_begin`, `predicates`, `filter_bound`, `info_str`, `replaceUseOfWith`, `verifyAxis`, `verify`) is a predicated sub-domain bracket — an axis-level node carrying `predicates` (`AffinePredicate`s) and a `filter_bound` that restricts the iteration domain to the subset satisfying the predicate. It is how a sparse / masked iteration space is represented as an axis rather than as control flow.

### AxisHandle

`AxisHandle` (6 pyx methods: `__init__`, `__call__`, `coef`, `replaceUseOfWith`, `release`, `release_list`) is a **use-counted (coef · axis) reference** — the `(coefficient · induction_variable)` term that appears inside an address expression. `coef` is the integer coefficient; `release` / `release_list` drop the reference (the use-count machinery that keeps an axis alive while any access still references its `iv`). When an access's address is `3*i + 2*j`, the `3*i` and `2*j` are two `AxisHandle`s.

```c
class AxisHandle:                  // ir/Axis.py
    axis            // the referenced Axis
    coef            // integer coefficient  (CONFIRMED getter)
    def __call__(self): ...        // materialize the (coef · iv) term
    def release(self): ...         // drop one use of the axis
    def release_list(self): ...    // drop a list of uses
    def replaceUseOfWith(old, new): ...   // rewrite the referenced axis
```

---

## How a Loop-Nest Is Represented

### The nest is a tree on each Instruction

A nested loop is not a region or a block — it is the `axes` list on each `Instruction` (P5.1 §2.2), ordered outermost-to-innermost, with `loopdepth` = the instruction's nesting level and `loopnest` the canonical nest object. An `AffineAxisCollection` (ir.so) is an ordered set of `AffineAxes` = one whole loop-nest. The parent-chain is mirrored at the index level by `pelican::AffineIdx` (P5.5 §5.2), whose verbatim assertion `loopdepth == (parent ? parent->getLoopdepth() + 1 : 0)` makes the index nesting depth equal the loop nesting depth.

```text
for (i: range(0, M, 1))            // AffineAxis  type=Parallel    loopdepth 0
  for (j: range(0, N, 1))          // AffineAxis  type=Sequential  loopdepth 1
    C[i, j] += A[i, k] * B[k, j]   // Instruction  axes=[i, j]  loopdepth=2
                                   //   access addr = AxisHandle(1·i) , AxisHandle(1·j) , …
```

Each access on the instruction (P5.3-tensor §5.4 `Access`) binds the tensor to an address list of `AffineExpr`s built from `AxisHandle(coef · iv)` terms over the nest's axes. So the loop-nest, the iteration variables, and the addresses are one connected def-use graph: rewriting an axis (`replaceUseOfWith`) rewrites every address that references its `iv`.

### How parallelism / sharding attaches and is consumed

The role flows from front-end intent to back-end placement:

```text
NKI range / hlo2penguin  →  AxisType on each AffineAxis
   nl.affine_range        →  Parallel
   nl.sequential_range    →  Sequential
   shard annotation        →  as_shard_axis()  (must be outermost)
   collective op           →  as_ccrank_axis() / SPMD

Tiler (D-U02) reads AxisType + Operator.used_as_{reduce,contract,lhs_free,rhs_free}_axis
   →  classifies each AffineAxis into P(artition) / F(ree) / B(lock)
   →  axis.tile(size) splits it, mutating the nest tree
   →  LoopSplitting + MacroGeneration emit the per-tile loop-nest

BirCodeGenLoop (P5.15 / Part 7)
   →  lowers each AffineAxis → BIR LoopAxis, DynamicAxis → BirDynamicForAxis
```

The tiler does not *infer* parallelism — it *reads* the `AxisType` that the front-end and the role queries set, then chooses which axis maps to the 128-partition SBUF dim and which is sharded across LNC cores. `enumerate_loopnest` / `enumerate_accesses` (CONFIRMED on `AffineAxis`) walk the nest and the accesses for these passes.

> **NOTE —** the `Shard` / `Thread` roles are LNC (Logical-Neuron-Core) concepts: `Shard` splits the iteration space across the cores of an LNC group, `Thread` across the threads within a core. The base-axis sub-tree queries (`has_shard_axis`, `has_thread_axis`, `is_spmd_or_shard`, `is_thread_or_cc_axis`) let a pass test a whole nest for these roles in one call, which is how `InferShardAxis` / `LowerShardAxis` / `LowerCCOpBlockAxis` (penguin/targets/transforms — present as sibling `.so` modules) decide where to cut.

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against the binary:

1. **"`AffineAxis`/`DynamicAxis`/`SequentialAxis`/`Filter`/`AxisHandle` are subclasses of `Axis`, with method counts 63/2/1/8/6."** Re-checked: `nm Axis.so | rg -o '__pyx_pw_…_4Axis_<len><Class>_<idx>…'` resolves each class's wrapper set; counts confirmed (`AffineAxis` 63, `Axis` 15, `Filter` 8, `AxisHandle` 6, `DynamicAxis` 2, `SequentialAxis` 1). **CONFIRMED.**
2. **"The serialize form is `for ({it}: range({lb}, {ub}, {stride}))`."** Re-checked verbatim via `strings`: `{space_indent}{_label}{attr}for ({it}: range({lb}, {ub}, {stride}))` is present as a `__pyx_k_` template. **CONFIRMED.**
3. **"`AxisType` members are `{Default, Parallel, Sequential, Shard, Thread, Block, CCRank, Interleave, SPMD}`."** Re-checked: all nine names interned in the string pool; eight have paired `is_<type>` predicates with verbatim `True if loop type is <type>.` docstrings; `Block` has no `is_block`. Member names **CONFIRMED**; ordinal values **INFERRED** from declaration order (tagged CORRECTION AXIS-1).
4. **"`DynamicAxis` upper bound is a runtime value, and its use-def chain differs from `Axis`."** Re-checked: diagnostics `Dynamic Axis must have a runtime-valued upper bound` and `…the use-def chain of \`DynamicAxis\` and \`Axis\` are not the same` recovered verbatim. **CONFIRMED.**
5. **"Role conversion is guarded — sequential/shard axes cannot be parallelized; shard must be outermost."** Re-checked: `Cannot parallelize sequential/shard axis!`, `Cannot interleave sequential axis!`, `expect shard axis on top, but got `, `Expect AffineAxis, got:` all recovered verbatim from `Axis.so`. **CONFIRMED.**

Remaining gaps, marked honestly: the exact integer **ordinals** of `AxisType` are declaration-order-inferred, not read as a literal table (AXIS-1); the precise field *types* of `iv` (`AffineIV` vs `AffineIdx`) are STRONG (the getter exists; the returned type is inferred from the ir.so registry, not from a method signature); the `Block`-role query path is INFERRED to route through `NeuronBlockTensor` rather than `Axis`.

---

## Related Components

| Name | Relationship |
|---|---|
| `Instruction` (P5.1 §2.2) | carries the `axes` / `loopnest` / `loopdepth` triple — the loop-nest lives on each instruction |
| `pelican::AffineExpr` / `AffineIdx` (P5.5) | the C++ affine algebra the axis `iv`s feed; index parent-chain mirrors loop nesting |
| `Access` (P5.3-tensor §5.4) | binds a tensor operand to an address list of `AffineExpr`s over the axes |
| `NeuronBlockTensor` (P5.3-tensor §3.4) | the partition/block-tiled tensor whose block dim pairs with a `Block`-role axis |
| `While` (P5.1 §7.6) | the structured alternative to `DynamicAxis` for non-affine data-dependent loops |
| BIR `LoopAxis` / `BirDynamicForAxis` (Part 7) | the downward lowering target of `AffineAxis` / `DynamicAxis` |

## Cross-References

- [Penguin IR Node Model](ir-node-model.md) — P5.1; `Instruction.axes`/`loopnest`/`loopdepth`, the SSA value base, `While`/`Filter` predicate model
- [Penguin Loop Transforms](loop-transform-clients.md) — P5.15; LoopSplitting / MacroGeneration / tiling that cut and emit the axis nest
- [NKI Range Semantics](../nki/range-semantics.md) — P6.1.3; `nl.affine_range` / `nl.sequential_range` → `AxisType.Parallel` / `Sequential`
- [BIR LoopAxis](../bir/loop-axis.md) — Part 7; the `BirCodeGenLoop` lowering target for each Penguin axis
