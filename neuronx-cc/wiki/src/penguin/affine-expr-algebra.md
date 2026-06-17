# Penguin AffineExpr Algebra

> *All symbols, offsets, and addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel). The Penguin IR modules are Cython `.so` files shipped **with** `debug_info` (unstripped); `pelican.cpython-310…so` is the C++ backbone and is stripped of `debug_info` but retains its Itanium-mangled symbol table and embedded assert source-lines. Other versions will differ.*

## Abstract

Penguin — the Python middle-end of neuronx-cc (`neuronxcc.starfish.penguin.ir`) — addresses every tensor and predicates every instruction with a **quasi-affine expression algebra**. This page is the *Penguin-side* view of that algebra: the node kinds an index or address is built from (`SumExpr` / `MultExpr` / `ModuloExpr` / `FloorDivExpr` / `CompoundExpr` / `ICmpExpr`), the collective-cyclic `CC{Div,Mod,GetRank}` quasi-affine family used to address tensors sharded across a collective replica set, and the access-pattern descriptors (`AffineLoad` / `AffineStore` / `AffineAtomicRMW`, and the post-tiling `TileAccessBase`) that bind an instruction operand to a tensor sub-region.

The central reverse-engineering finding (D-U08 §5, re-confirmed here) is that **the algebra is not implemented in Python**. `ir/AffineExpr.cpython-310…so` is a thin Cython wrapper whose module docstring is verbatim `"AffineExpr - Affine and Quasi-Affine expressions."`; the real class hierarchy is the LLVM-RTTI'd `pelican::Expr` tree in `pelican.cpython-310…so`, hash-consed in an `llvm::FoldingSet`. A reader familiar with MLIR's `AffineExpr` / `AffineMap` will recognize the shape — `c + Σ cᵢ·idxᵢ` with floordiv/mod locals — but Penguin adds two things MLIR's affine dialect does not have: a **collective-cyclic** sub-family that encodes `floor(rank/group_size)` / `rank mod group_size` as first-class nodes carrying a `replica_groups_id`, and an explicit **access descriptor layer** that pairs the address-expression list with a partition/free-axis annotation for the 128-lane SBUF/PE geometry.

The page proceeds: the `pelican::Expr` class hierarchy and what each node represents (`§ Node Hierarchy`); how an index forms from a loop axis (`§ Building an Index`); the `CC{Div,Mod,GetRank}` family (`§ Collective-Cyclic`); the affine access descriptors and `TileAccessBase` (`§ Access Descriptors`); the predicate atom `ICmpExpr` (`§ Predicate Atom`); then the algorithm-level flatten and an adversarial self-verification. The **byte-level `pelican::Expr` wire-form / `toJsonv2` encoding** is owned by Part 7 (7.16–7.20) and is cross-referenced rather than duplicated here; this page is the algebra and the descriptors as Penguin sees them.

For reimplementation, the contract is:

- The `pelican::Expr` node taxonomy and the kind tag each node carries (`SumKind`, `MultKind`, `ModuloKind`, `FloorDivKind`, `CCDivKind`, `CCModKind`, `CCGetRankKind`, `ICmpKind`), plus the `FoldingSet` uniquing invariant.
- How a Penguin loop `AffineAxis` produces affine index terms via its operator overloads, and how those fold to a flat `c + Σ cᵢ·idxᵢ`.
- The `CC{Div,Mod,GetRank}` semantics (shard-index / intra-shard offset / global-rank) and the `denom > 0` / `iteration_id,channel_id ≥ 0` invariants.
- The access-descriptor model: `Access ⊃ AffineAccess ⊃ {AffineLoad, AffineStore, AffineAtomicRMW}`, the per-dim `addrs` list of quasi-affine expressions, the P/F partition annotation, and `TileAccessBase`'s `(partition_ap, [free_ap…])` split that the BIR codegen consumes.

| | |
|---|---|
| **Python wrapper module** | `ir/AffineExpr.cpython-310-x86_64-linux-gnu.so` (946 KB, Cython, unstripped, BuildID `d56f86cd…`) |
| **C++ backbone** | `pelican.cpython-310-x86_64-linux-gnu.so` (2.64 MB), header `pelican/IR/AffineExpr.h`, `pelican/IR/AffineIndices.h` |
| **Node uniquing** | `llvm::FoldingSet<pelican::FoldingIdx>` (`_ZN4llvm10FoldingSetIN7pelican10FoldingIdxEE…NodeEquals…`) |
| **Access module** | `ir/Access.cpython-310…so`; tile form `ir/TileAccess.cpython-310…so` |
| **Lowers to (BIR)** | `pelican::AffineExpr → BirQuasiAffineExpr`; `Access → sNdM AP-struct` (Part 7) |
| **Quasi-affine factory home** | `pelican::PelicanContext::createCCGetRankExpr(ll)` (`_ZN7pelican14PelicanContext19createCCGetRankExprEll`) |

---

## Node Hierarchy

### Purpose

`pelican::Expr` is the base of every index, address, stride, and predicate expression in Penguin. It is LLVM-RTTI'd — the binary contains explicit template instantiations such as `decltype(auto) llvm::dyn_cast(From*) [with To = pelican::FloorDivExpr; From = pelican::Expr]`, so the codebase navigates the hierarchy with `isa<>`/`dyn_cast<>`/`cast<>` exactly as LLVM IR does. Each concrete node carries an integer `ExprKind` discriminator (read by `expr_kind` in the Python wrapper); the kind names are interned in `pelican.so`'s string pool: `SumKind`, `MultKind`, `ModuloKind`, `FloorDivKind`, `CCDivKind`, `CCModKind`, `ICmpKind` (with `CompoundExpr`, `SimpleExpr`, `BinaryExpr` as further node names).

### Class Hierarchy

```c
// pelican::Expr — CONFIRMED node names from pelican.so string pool +
// the AffineExpr.so Python-face class pool (1:1 wrappers).
Expr                          // base; LLVM-RTTI (isa/dyn_cast/cast)
 ├─ SimpleExpr                // a leaf: a constant (CExpr) or a single index term
 ├─ AffineExpr                // affine combination of AffineIdx terms; FoldingSet-uniqued
 │   ├─ SumExpr      (SumKind)       //  Σ cᵢ·idxᵢ + const           — the canonical flat form
 │   ├─ MultExpr     (MultKind)      //  coef · sub-expr             — scalar scaling
 │   ├─ ModuloExpr   (ModuloKind)    //  expr mod m
 │   ├─ FloorDivExpr (FloorDivKind)  //  expr floordiv d   (denom>0) — DivLike
 │   └─ CompoundExpr                 //  nested compound term (a non-flattened sub-tree)
 ├─ BinaryExpr                // generic binary node (projectMin/projectMax base)
 ├─ ICmpExpr        (ICmpKind)       //  integer comparison — the PREDICATE atom (§ Predicate Atom)
 ├─ CCDivExpr       (CCDivKind)      //  floor(rank / group_size)    — collective shard index
 ├─ CCModExpr       (CCModKind)      //  rank mod group_size         — intra-shard offset
 └─ CCGetRankExpr                    //  the collective global rank  (§ Collective-Cyclic)
    // IndirectArgExpr / OpaqueFnExpr — non-affine / opaque terms (gather, indirect addr)
```

The Python faces in `AffineExpr.so` are 1:1 with the C++ classes (all interned in the `.so` pool, verified by `strings`): `Expr`, `CExpr`, `AffineExpr`, `SumExpr`, `MultExpr`, `ModuloExpr`, `FloorDivExpr`, `CompoundExpr`, `CCExpr`, `CCDivExpr`, `CCModExpr`, `ICmpExpr`, plus the triviality probes `AsTrivialExpr` / `IsTrivialExpr`. `CExpr` is the constant leaf; `CCExpr` is the Python umbrella for the collective-cyclic family.

> **NOTE —** "quasi-affine" (not merely "affine") is the precise term, and the module docstring spells it out: `"AffineExpr - Affine and Quasi-Affine expressions."`. The *quasi*-affine extension is the floordiv/mod/CC nodes — an index that is `floor(i/4)` or `i mod 4` is not affine in the textbook sense but is still exactly representable as an integer-division *local* in a Presburger constraint, which is why the whole tree survives the round-trip to isl (see [Part 5.21](affine-isl-pelican-bridge.md)).

### The FoldingSet Invariant

Every `AffineExpr` node is hash-consed: structurally identical expressions are the *same* object. The uniquing table is an `llvm::FoldingSet` whose node type is `pelican::FoldingIdx` — confirmed by the mangled symbol `_ZN4llvm10FoldingSetIN7pelican10FoldingIdxEE10NodeEqualsEPKNS_14FoldingSetBaseEPNS4_4NodeERKNS_16FoldingSetNodeIDEjRS9_` and by `pelican::Expr::hash_value`. Consequences for a reimplementer:

- Pointer identity *is* value equality for canonicalized expressions — the dependency analyzer and the layout solver compare addresses with impunity.
- `createFromCtx` / the per-kind factories must intern through the `PelicanContext` (the `FoldingSet` owner), not allocate freely; a duplicate `SumExpr` is silently deduplicated.

> **GOTCHA —** because nodes are immutable and uniqued, the "mutating" operations (`add`, `inplaceAdd`, `dropIndexInplace`, `substituteIndices`) all produce a *new* canonical node and re-intern; the `inplace`-named methods mutate a builder/accumulator, not a live `FoldingSet` node. A reimplementation that edits a `SumExpr` in place corrupts every other expression that shares it.

---

## Building an Index

### Purpose

A Penguin loop is a tree of `AffineAxis` nodes (`ir/Axis.py`). Each axis carries an induction variable `iv` (an `AffineIdx`), bounds `lb`/`ub`, and `stride`. The *index into a tensor* is built by doing arithmetic on these axes, and the arithmetic is overloaded to construct `AffineExpr` nodes rather than evaluate integers — the classic "build the symbolic expression by running the loop's index math" pattern.

### Operator Overloads → Expr Construction

`AffineAxis` (and the `AffineIdx` it wraps) overload the Python arithmetic dunders, each mapped to a `pelican::Expr` factory:

| Python op on axis | Builds | Kind |
|---|---|---|
| `a + b`, `a - b` (`__add__`/`__radd__`/`__sub__`/`_add_impl`) | `SumExpr` | `SumKind` |
| `c * a` (`__mul__`/`__rmul__`/`_mul_impl`) | `MultExpr` | `MultKind` |
| `a % m` (`__mod__`) | `ModuloExpr` | `ModuloKind` |
| `a // d` (`__floordiv__`) | `FloorDivExpr` | `FloorDivKind` |
| `-a` (`__neg__`) | `MultExpr(a, -1)` | `MultKind` |

So `2*i + j // 4` over loop axes `i`, `j` constructs `SumExpr([MultExpr(i,2), FloorDivExpr(j,4)])` — a quasi-affine tree, then `simplify` / `linearizedExpr` flattens it.

### The AffineIdx Parent Chain

The index leaves are not free integers — they form a **parent chain mirroring loop-nest depth**, enforced by asserts embedded in the binary (`pelican/IR/AffineIndices.h`):

```c
// AffineIndices.h:152  — a plain loop index sits one level below its parent loop
assert(loopdepth == (parent ? parent->getLoopdepth() + 1 : 0));

// AffineIndices.h:170  — a div/mod SPLIT index DOUBLES depth and tags which half it is
assert(loopdepth == (getParent()->getLoopdepth() * 2 + int64_t(isDivNotMod)));

// AffineIndices.h:171  — the div/mod factor is strictly positive
assert(factor > 0);
```

The :170 invariant is the structural heart of tiling: when an axis `i` is split by tile size `t` into an outer `i//t` and inner `i%t`, the two resulting indices are children of `i` at *double* the depth, distinguished by the boolean `isDivNotMod` (the `+0` / `+1` term). This is how the algebra keeps `i//t` and `i%t` provably consistent — they share a parent and a `factor`, so the polyhedral layer can reconstruct `i = (i//t)*t + (i%t)` exactly.

> **QUIRK —** the depth-doubling means an `AffineIdx`'s `loopdepth` is *not* simply its loop-nest level once any div/mod split has happened; it is a positional code in a binary tree of splits. A reimplementer computing loop depth by counting enclosing `for`s will disagree with the binary the moment a tile split occurs. Read `loopdepth` as the AG-coded path, not the visible nesting.

### Linearizing an Address

`AffineAxis.linearizedExpr` (docstring verbatim: *"linearizedExpr - Get the delinearized Affine Expr out of all the [loops]"*) folds the whole enclosing loop-nest into one flat expression. The textual loop serialize is verbatim `{space_indent}{_label}{attr}for ({it}: range({lb}, {ub}, {stride}))` — i.e. a Penguin loop axis literally prints as a Python `for`-over-`range`, and the index algebra is the symbolic arithmetic over those range variables.

---

## Collective-Cyclic — CC{Div, Mod, GetRank}

### Purpose

When a tensor is distributed across a collective replica set (all-gather / all-reduce / reduce-scatter, the LNC sharding model), a replica's address into the global tensor decomposes into *(which shard, offset within shard)*. The `CC` family encodes this decomposition as first-class quasi-affine nodes so the polyhedral layer can keep distinct collective groups apart by `replica_groups_id`. These are *not* generic `FloorDivExpr`/`ModuloExpr`: their denominator (`group_size`) is a runtime collective parameter, and they are tagged with the replica-group set they belong to.

### Semantics

```c
// For a collective with global rank r and group size g, group-set rgid:
CCGetRankExpr(iteration_id, channel_id)   // the global rank r itself
CCDivExpr(r, g, rgid)  =  floor(r / g)    // SHARD INDEX  (which group the rank is in)
CCModExpr(r, g, rgid)  =  r mod g         // WITHIN-SHARD OFFSET (rank's position in its group)
```

`CCGetRankExpr` is the rank term that feeds the other two. Its factory is the confirmed mangled symbol `pelican::PelicanContext::createCCGetRankExpr(long, long)` (`_ZN7pelican14PelicanContext19createCCGetRankExprEll`) — the two `long` arguments are the `iteration_id` and `channel_id`, gated by the embedded assert `AffineExpr.h:326: iteration_id >= 0 && channel_id >= 0` and the diagnostic string `"Invalid iteration_id or channel_id for CCGetRankExpr!"`. The node has the full RTTI complement (`_ZTVN7pelican13CCGetRankExprE` vtable, `clone`, `equal(const Expr*)`, `hash_value`, `str`), so it is a genuine `Expr` subclass and participates in the same `FoldingSet` uniquing as the rest.

### The denom > 0 Invariant

Both `CCDivExpr` and `CCModExpr` are `DivLike` nodes and inherit the strict-positive-denominator invariant, embedded verbatim as `AffineExpr.h:711: denom > 0`. The `arg_id >= 0` companion (`AffineExpr.h:356`) gates the per-argument index used by the opaque/indirect-arg path. A reimplementer must reject a zero or negative `group_size` at construction — the divisor is never validated downstream because the constructor already asserted it.

### Python Constructors

`AffineExpr.so` exposes `cc_div(...)` and `cc_mod(...)` as the Python factory front-doors (both confirmed by `strings`, both ~`0x760` bytes and byte-for-byte the same call topology). Each resolves the `CCDivExpr`/`CCModExpr` class from `neuronxcc.pelican.ir` and constructs it, passing the integer `(denom, replica_groups_id)` operands through a C-level `__pyx_ctuple_long`.

> **NOTE —** the "CC-ness" is a Penguin/pelican distinction that *collapses* once the expression reaches the polyhedral layer: a `CCDivExpr` maps to an isl ceiling `scale_down_val`, a `CCModExpr` to `mod_val` — exactly as an ordinary `FloorDivExpr`/`ModuloExpr` would. The `replica_groups_id` has already selected *which* rank affine feeds the numerator, so isl sees only an ordinary integer-division local. The cross-ref for that mapping is [Part 5.21](affine-isl-pelican-bridge.md#collective-cyclic).

---

## Access Descriptors — AffineLoad / AffineStore / AtomicRMW

### Purpose

An instruction operand is a *(Tensor, Access)* pair. The `Access` (`ir/Access.py`) is the read/write descriptor: it binds the operand to a tensor and carries the per-dimension list of quasi-affine address expressions (`addrs`), the partition/free-axis annotation for the 128-lane geometry, the access mode, and the out-of-bounds policy. This is the Penguin twin of the BIR access-pattern (`sNdM`) struct.

### Class Hierarchy

```c
// ir/Access.py — CONFIRMED class names from Access.so string pool.
Access                        // base descriptor: tensor + addrs[] (per-dim AffineExpr)
 ├─ AffineAccess              // "Describe affine read/write access to tensor" (verbatim docstring)
 │   ├─ AffineLoad            //  a read  (LoadStore.is_load)
 │   ├─ AffineStore           //  a write (LoadStore.is_store)
 │   ├─ AffineLoadStore       //  read-modify-write addressed affinely
 │   └─ AffineAtomicRMW       //  atomic reduce-into (the reduction-accumulate form)
 ├─ GenericAccess             // "Describe read/write access to tensor" — the data-dependent form
 │   └─ GenericLoad/Store/LoadStore/GenericAtomicRMW   // indirect (gather/scatter) variants
 ├─ SeqAccess / FullTensorAccess / NDimSubTensorAccess / FlattenedSubTensorAccess
 └─ OpaqueAccess / OffloadedSlice                      // non-affine / host-offloaded
```

The `LoadStore` base provides the load/store discriminator (`is_load`/`is_store`) and the partition/free-axis annotation methods (`update_free_axes`, `update_partition_axes`, `intersect_partition_free_axes`) that the layout solver writes onto each access — this is where the abstract address gains its P/F geometry.

### The Address List

Every `Access` holds `addrs` (and `full_addrs`): a per-dimension list of quasi-affine `AffineExpr` over the enclosing loop axes. The probe `has_all_affine_addrs` (a genexpr over the list, confirmed symbol `…Access_6Access_10has_all_affine_addrs`) tests whether every dimension is affine — a `GenericAccess` with an `IndirectArgExpr`/`OpaqueFnExpr` term fails it and forces the indirect codegen path. The flatten from a multi-dim `addrs` list to a single linear offset is `linearize_indices` / `linearize_address`, driving the pelican `AffineExpr::flattenTerms` / `getLinearExpr` (`flattenTerms` confirmed in `pelican.so`).

### Atomic-RMW Serialize Format

The verbatim textual form (from `Access.so`, exact binary string — note the leading `{indent}` that D-U08 §5.4 omitted):

```text
{indent}{type} {dst} = atomic_rmw_{op}({dst}, {src}, {{{reduce_axes}}}){partition_axes}{predicate}{dl}, id = {id}
{indent}{type} {dst} = generic_atomic_rmw_{op}({dst}, {src}, {{{reduce_axes}}}){partition_axes}{predicate}{dl}, id = {id}
```

> **CORRECTION (5.4-A) —** D-U08 §5.4 cited the atomic-RMW serialize without the leading `{indent}` token. The binary string in `Access.so` begins `{indent}{type} {dst} = atomic_rmw_{op}(…`. The version on this page is the exact binary form. The `generic_` prefix marks the data-dependent (`GenericAtomicRMW`) variant; note also the embedded `"generic_atomic_rmw eval not supported yet"` — the constant-folder `eval` is unimplemented for the indirect form.

### Modes and OOB Policy

`AccessMode` (load / store / indirect-load / indirect-store) selects the descriptor flavor; `OOBMode` is the out-of-bounds policy for indirect (gather/scatter) access. Both are confirmed enums in `Access.so`. The OOB policy only matters on the `Generic*` path — an `AffineAccess` is bounds-checked statically by the polyhedral domain.

---

## TileAccessBase — the Post-Tiling Descriptor

### Purpose

After the tiler (D-U02) cuts each axis to tile size, the abstract `Access` is lowered to a *tile* access pattern split into a **partition AP** (the 128-partition SBUF/PE dimension) and **free APs** (the in-tile contiguous dimensions). `TileAccessBase` (`ir/TileAccess.py`) is that concrete descriptor — the exact form `BirCodeGenLoop` consumes when it emits the BIR `sNdM` access struct.

### Fields and the Partition/Free Split

```c
// ir/TileAccess.py — CONFIRMED fields from TileAccess.so string pool.
class TileAccessBase:
    partition_ap          // the 128-partition-dim access pattern
    free_ap               // the in-tile contiguous free-dim access pattern(s)
    full_ap   / dst_ap    // assembled / destination forms
    start_partition       // base partition for this tile
    npartitions           // partition count (≤ 128)
    tile_size             // in elements

    linearize_address          // fold (partition_ap, free_ap…) → linear byte offset
    linearize_partition_addr   // partition-dim contribution
    linearize_tile_addr        // in-tile contribution
```

The `(partition_ap, [free_ap…])` split is the literal bridge between a Penguin `Access` and the BIR ISA instruction's source/destination access pattern: `BirCodeGenLoop`'s `add*AP` helpers turn `partition_ap` + each `free_ap` into the `(stride, size)` entries of the `sNdM` struct. `NeuronIndicesAP` is the index-access-pattern carrier alongside it (the per-index-dim form for indirect addressing).

> **NOTE —** the partition dim is special because it maps to physical SBUF/PSUM partitions, not just another loop axis — its stride is the partition stride, not a byte stride. See [Part 2.2 (ADDR4 / SBUF-PSUM geometry)](../arch/sbuf-psum-geometry.md) for why the 128-partition axis is addressed separately and what `start_partition` indexes into.

---

## Predicate Atom — ICmpExpr

### Purpose

Penguin encodes loop-nest guards as per-instruction `AffinePredicate`s (D-U08 §2.2: ~25 of `Instruction`'s 51 methods are the predicate sub-model), not as CFG branches. The atom of a predicate is `ICmpExpr` — an integer comparison node in the same `pelican::Expr` hierarchy.

### Structure

`ICmpExpr` (`ICmpKind`) carries a compare-op and two `Expr` operands (`lhs`, `rhs`). The Python `AffinePredicate` layer normalizes *every* comparison to one of two native forms — `e >= 0` or `e == 0` — so the only compare-ops an `ICmpExpr` ever holds from the Python side are `SGE` and `EQ`. The five public constructors (`pred_ge`/`pred_le`/`pred_gt`/`pred_lt`/`pred_eq`) reduce to these two by sign-flipping the subtraction and adding a `-1` for the strict pair; the lone discriminator is a boolean `ge` kwarg (`True` → inequality, `False` → equality). The validity gate `is_legal_predicate` rejects any predicate whose expression has a runtime value or non-affine term — diagnostic string `"Invalid Predicate!"` (confirmed in `AffineExpr.so`).

> **NOTE —** `ICmpExpr` is a *control* atom, never an address expression — it is not serialized to the tensor wire-form. The full predicate normalization, the `ge`-kwarg mechanics, and the round-trip to/from isl constraints are owned by [Part 5.21](affine-isl-pelican-bridge.md); this section establishes only that the predicate atom is a member of the same `Expr` algebra. Byte-level `ICmpExpr` layout is in [Part 7.16–7.20](../bir/pelican-expr-wireform.md).

---

## Algorithm — Flatten to Canonical Form

### Purpose

The common currency between Penguin, BIR, and the isl polyhedral layer is the **flat** linear form `c + Σ cᵢ·idxᵢ`. The tree built by the operator overloads (`§ Building an Index`) must fold to that form before it can be emitted to BIR or mapped to isl.

### The Flatten

```c
// linearize_affineexpr(expr)  — AffineExpr.so @0x17e00 (thin orchestrator).
// The real arithmetic is pelican AffineExpr::flattenTerms / getLinearExpr /
// accumulateTerm (C++, CONFIRMED symbol flattenTerms in pelican.so).
function linearize_affineexpr(expr):
    // walk the Sum/Mult/Mod/FloorDiv/CC tree; fold nested Mult/Sum into
    // one coefficient per AffineIdx plus a scalar constant c.
    acc = {}                       // idx → int64 coefficient
    c   = 0
    for (coeff, idx) in expr.terms:        // genexpr over SumExpr.terms (n_terms)
        if idx is None: c += coeff         // constant leaf (CExpr)
        else:           acc[idx] += coeff  // accumulate per-index coefficient
    return SumExpr(acc) + c                // canonical  c + Σ cᵢ·idxᵢ  (re-interned)

// linearize_affineindices(indices)  — AffineExpr.so @0x171c0
//   the VECTOR variant: flattens a per-dim index list (an Access's addrs)
//   into one flat AffineExpr per access dimension.
```

`linearize_affineexpr` flattens *one* expression; `linearize_affineindices` flattens the *vector* of per-dim index expressions that an `Access` holds. Both bottom out in the same pelican flatten. Doing the flatten in pelican (not in the isl glue) is deliberate: the flattened form is reused by three consumers — BIR emission (`BirQuasiAffineExpr`), serialization (`toJsonv2`, Part 7), and the isl bridge — so it cannot live inside any one of them.

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against the binary:

1. **"The algebra is C++ `pelican::Expr`, not Python."** — CONFIRMED. `AffineExpr.so`'s module docstring is verbatim `"AffineExpr - Affine and Quasi-Affine expressions."`; the kind enum (`SumKind`/`MultKind`/`ModuloKind`/`FloorDivKind`/`CCDivKind`/`CCModKind`/`ICmpKind`), `flattenTerms`, and `dyn_cast<…FloorDivExpr…From = pelican::Expr>` all live in `pelican.so`. The Python `.so` interns the face-class names but the arithmetic symbols are in pelican. **Holds.**

2. **"`CCGetRankExpr` is a real `Expr` subclass with factory `createCCGetRankExpr(ll)`."** — CONFIRMED by the exact mangled symbol `_ZN7pelican14PelicanContext19createCCGetRankExprEll`, the vtable `_ZTVN7pelican13CCGetRankExprE`, and the assert `AffineExpr.h:326: iteration_id >= 0 && channel_id >= 0` plus the diag `"Invalid iteration_id or channel_id for CCGetRankExpr!"`. The two `long` args = `(iteration_id, channel_id)` is read off the `ll` mangling. **Holds.**

3. **"Nodes are `FoldingSet`-uniqued."** — CONFIRMED by `_ZN4llvm10FoldingSetIN7pelican10FoldingIdxEE10NodeEquals…` and `pelican::Expr::hash_value`. The node type is `pelican::FoldingIdx`. **Holds.** The *immutability consequence* (the GOTCHA) is INFERRED from FoldingSet semantics, tagged as such in prose.

4. **"Atomic-RMW serialize begins with `{indent}`."** — CONFIRMED; the exact `Access.so` string is `{indent}{type} {dst} = atomic_rmw_{op}({dst}, {src}, {{{reduce_axes}}})…`. This overturns the D-U08 §5.4 form (flagged as CORRECTION 5.4-A). **Holds.**

5. **"`AffineIdx` depth doubles on div/mod split, tagged by `isDivNotMod`."** — CONFIRMED verbatim: `AffineIndices.h:170: loopdepth == (getParent()->getLoopdepth() * 2 + int64_t(isDivNotMod))`, with the `:152` parent-chain and `:171 factor > 0` companions. The *tiling interpretation* (`i//t`/`i%t` split) is STRONG (consistent with the :170 form + the tiler's div/mod split) but the literal "tile size `t`" naming is INFERRED — the assert proves the doubling and the `isDivNotMod` tag, not the tile-size source. Tagged in prose.

No fabricated addresses or symbols: every `sub_`/offset cited (`0x17e00`, `0x171c0`) is from D-Y06 §0's wrapper roster; every class name and assert string was re-grepped from the `.so` this session.

---

## Related Components

| Name | Relationship |
|---|---|
| `pelican::AffineExpr` (C++) | The actual algebra; the Python `AffineExpr.so` is a thin wrapper over it |
| `BirQuasiAffineExpr` | What a Penguin `AffineExpr` lowers to in BIR codegen (`BirCodeGenLoop`) |
| `Access` / `TileAccessBase` | The descriptors that *use* the algebra to address tensors |
| `AffinePredicate` | The per-instruction guard wrapping an `ICmpExpr` atom |
| isl `PwAff` / `Set` | The polyhedral form the flat expression maps onto ([Part 5.21](affine-isl-pelican-bridge.md)) |

## Cross-References

- [Affine ↔ isl ↔ pelican Bridge](affine-isl-pelican-bridge.md) — Part 5.21; how the flat form maps to/from isl, the predicate normalization, the `CC` → integer-division collapse, and the round-trip
- [Penguin IR Node Model](ir-node-model.md) — the surrounding SSA node schema (Value/Instruction/Tensor/Axis) this algebra plugs into
- [pelican::Expr Wire-Form](../bir/pelican-expr-wireform.md) — Part 7.16–7.20; the byte-level `Expr` layout, kind tags, factory addresses, and `toJsonv2` encoding
- [SBUF / PSUM Geometry (ADDR4)](../arch/sbuf-psum-geometry.md) — Part 2.2; why the 128-partition axis is addressed separately from free dims, what `start_partition` indexes
