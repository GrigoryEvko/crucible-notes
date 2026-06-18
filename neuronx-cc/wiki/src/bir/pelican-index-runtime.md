# Pelican Index, Collective-Rank, and Runtime Expressions

> *All symbols, addresses, struct offsets, and vtable VAs on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The pelican `Expr` algebra is statically linked into **two** libraries: `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`) owns the four (de)serializers (`toJson`/`fromJson` v1/v2) and the simple-leaf factories; `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`) owns the `PelicanContext` factories and the per-subclass virtual bodies (`isLegalDelinearizedAddress`, geometry). The 29 pelican typeinfos are byte-identical in both — one canonical algebra duplicated per `.so`. `VA == file offset` for `.text`/`.rodata` in both; `.data.rel.ro` slot pointers are `R_X86_64_64`/`R_X86_64_RELATIVE` relocs (read via `readelf -rW`; `objdump -s` shows the unrelocated zero slots). cp311/cp312 share the ABI but drift the VAs.*

## Abstract

The pelican `Expr` algebra (29 polymorphic classes; the base header, affine, and binary
subclasses are documented in [Pelican Expr Core](pelican-expr-core.md) and the RTTI
roster in [Pelican Hierarchy](pelican-hierarchy.md)) splits into three leaf families that
this page completes. The **AffineIdx family** (kinds 6..13) encodes how a loop-nested,
possibly-tiled access into SBUF or PSUM becomes an index tree — an induction variable,
a 1-D access-pattern index, a tiled index, a symbolic index, a shard identifier. The
**collective-rank family** (`CCGetRank`=2, `ShardId`=13, `CCDiv`=27, `CCMod`=28) does the
per-replica rank arithmetic a sharded collective needs: "which shard am I, and what is my
offset within it." The **runtime family** (`IndirectArg`=3, `IntRuntimeValueBase`=7) carries
values the compiler cannot know statically — a gather argument id, and a register-backed
runtime integer.

The last of these, **`IntRuntimeValueBase` with its `regref` at `+0x40`**, is the central
member: it is the pelican-`Expr` side of the dynamic-shape spine. When a loop bound, a
gather offset, or a tensor extent is only known at execution time, the compiler does not
fold it — it parks a `Register*` inside an `Expr` node and treats the whole node as an
opaque loop-invariant scalar. At codegen that register is the operand a scalar-engine ALU
chain reads; the [Symbolic-AP → Register-ALU](../penguin/symbolic-ap-register-alu.md)
materialization, the [Dynamic For-Loop](../penguin/dynamic-for-loop.md), and
[Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md) are the back-end half of
the same mechanism. The bir-level leaf that grafts onto this pelican node is
`bir::BirIntRuntimeValue` (see [BIR Structural Hierarchy](pelican-hierarchy.md)); it reads
the same `+0x40` regref.

The second decisive mechanism is the index→address lowering test:
**`isLegalDelinearizedAddress`**, the virtual at **vtable slot 12 (`+0x60`)** that every
`Expr` operand answers. It decides whether an affine index tree can become a real
delinearized 2-D (partition, free) hardware address, or whether the access must fall back
to the indirect/register path. The answer is recursive — a sum is legal iff every term is,
a divide is legal iff its divisor evenly partitions the index range (a gcd test), an opaque
function is *never* legal, a comparison is *not an address at all*. This page reconstructs
both spines as annotated pseudocode over the real symbols.

For reimplementation, the contract is:

- The AffineIdx geometry contract (`{lo, ub, step}` at `+0x20/+0x28/+0x30`) and the five interned index leaves built through `PelicanContext` `FoldingSet` uniquing.
- The four collective-rank exprs, their kind tags (`2/13/27/28`), and how `CCDiv`/`CCMod` over a `ShardId` + `replica_groups_id` encode rank-to-shard math.
- The `IntRuntimeValueBase` `+0x40` regref: how `fromJsonv2` resolves a register by name and how `toJsonv2`/`BirIntRuntimeValue::getNameStr` read it back — the dynamic-shape spine.
- The `isLegalDelinearizedAddress` vtable-slot-12 (`+0x60`) dispatch and its per-subclass legality rules, the test that gates affine vs. indirect addressing.

| | |
|---|---|
| **Expr base ctor** | `sub_5FE310` @ libBIR `0x5fe310` — writes `refcount=1@+0x08`, `kind@+0x10`, `ctx@+0x18`, vptr@`+0x00` |
| **AffineIdx-family range** | kinds **6..13** (the `(kind−6) <= 7` test in `DivLikeExpr::isLegalDelinearizedAddress`) |
| **Collective-rank kinds** | `CCGetRank=2`, `ShardId=13`, `CCDiv=27`, `CCMod=28` |
| **Runtime kinds** | `IndirectArg=3`, `IntRuntimeValueBase=7` |
| **The dynamic-shape field** | `IntRuntimeValueBase.regref` @ **`+0x40`** (a `bir::Register*`) |
| **Legality virtual** | `isLegalDelinearizedAddress` @ **vtable slot 12 = `+0x60`** (CONFIRMED across 6 subclasses) |
| **Serializers** | `toJsonv2` `0x3bab90` / `fromJsonv2` `0x3bc8c0` / `toJsonv1` `0x3b9f30` / `fromJsonv1` `0x3bbe90` / `isV1` `0x3b4110` (all libBIR) |
| **Uniquing arena** | `pelican::PelicanContext` (vtable libBIR `0x90c238`): `BumpPtrAllocator@+0x08`, `FoldingSet<FoldingIdx>@+0x68` |

---

## 1. The AffineIdx family — index expressions (kinds 6..13)

### Purpose

An access into SBUF or PSUM is, at the pelican level, an **index tree**: a symbolic
formula `idx = lo + step·iv` over loop induction variables, possibly tiled, possibly
shard-parametric, possibly symbolic. Every index leaf derives the abstract **`AffineIdx`**
and therefore carries the same geometry triple. All but one (`AffineIV`) are
**FoldingSet-interned** through `PelicanContext`, so two structurally-equal index exprs are
pointer-identical — which is what lets the eval-substitution map (`DenseMap<AffineIdx*, long>`,
witnessed in every `Instruction::evalFieldsInto` signature, e.g. libBIR `0x252cf0`) key on a
raw `AffineIdx*` and have it mean a unique index.

### The geometry contract

`AffineIdx` (abstract; no own kind) defines the contract every index leaf inherits:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| (Expr header) | `+0x00` | `0x20` | vptr / refcount / kind / ctx (see [Expr Core](pelican-expr-core.md)) | CERTAIN |
| `lo` / infimum | `+0x20` | int64 | start of the iteration range; `infimum()` returns this | CERTAIN |
| `ub` / sup-seed | `+0x28` | int64 | upper-bound seed fed to `calculateTripcount` | CERTAIN |
| `step` | `+0x30` | int64 | stride per IV increment | CERTAIN |

Semantics: `idx = lo + step·iv` for `iv ∈ [0, tripcount)`, where
`tripcount = calculateTripcount(ub, lo, step)` (`getTripcount` @ libwalrus `0xb45660`).
`supremum() = lo + step·(tripcount−1)`; the fast geometry thunks `getSupremumFast` /
`getInfimumFast` are at libwalrus `0x18d5350` / `0x18d52a0`. `getMaxLoopdepth`
(`0xb45450`, vtable slot ~336) and `getMaxTripcount` are the virtual geometry queries.

> **NOTE —** the bare `AffineIdx` base is a usable address: `AffineIdx::isLegalDelinearizedAddress`
> (libwalrus `0xb45440`) is a one-instruction `return 1`. A plain induction index is always a
> legal delinearized address; only the compound forms (sum / mult / div) gate.

### The five index leaves

| Kind | Class | Factory (`PelicanContext::…`) | Interned? | Distinguishing field(s) |
|---|---|---|---|---|
| 6 | `AffineIV` | (built per loop axis, not a `create*`) | **no** | a `BirLoopAxis` back-pointer reached via vtable slot ~312 (axis name) |
| 9 | `APIndex` | `createAPIndex` @ `0x18f68b0` | yes | a `long stride` + an inner `APIndex*` child; FoldingSet profile `{9, …, stride, inner}` |
| 10 | `TiledAPIndex` | `createTiledAPIndex` @ `0x18f6300` | yes | `child@+0x58`, `tile_pos_flags@+0x60`, `tile_size@+0x68`, `is_partition@+0x70` (alloc `0x78` — the largest index node) |
| 12 | `SymbolicIdx` | `createSymbolicIdx` @ `0x18f6b50` | yes | four longs `{lo, ub, step, offset}` packed onto the FoldingIdx payload (alloc `0x58`) |
| 13 | `ShardId` | `createShardId` @ `0x18f66e0` | yes | `ub@+0x28` = the shard count (alloc `0x58`); see [§2](#2-collective-rank-arithmetic) |

> **QUIRK —** `AffineIV` (kind 6) is the *only* non-interned index leaf, because it is
> **identity-uniqued by its loop axis**, not value-uniqued. Two `AffineIV`s over different
> axes with identical `{lo, ub, step}` are still distinct objects. The other four leaves are
> value-interned in the `PelicanContext` `FoldingSet` (arena field `+0x68`), so equality is
> pointer equality. A reimplementation that hash-conses `AffineIV` will silently merge
> distinct induction variables.

`TiledAPIndex` carries a legality assert at construction —
`tripcount(child) % tile_size == 0`, i.e. the tile must divide the iteration space evenly,
else a `PelicanAssertion` is thrown. **Nested tiling collapses**: if `child` is itself a
`TiledAPIndex` (kind 10) with the *same* `is_partition` flag, the two fuse and the tile
sizes multiply (`tile_size *= child.tile_size@+0x68`, loop at the factory head). A tiled
access therefore denotes `(outer iv of count tripcount/tile, inner iv of count tile)`; the
encoder emits the outer as the partition/highest descriptor dim and the inner as a free dim.

### Interning substrate — FoldingIdx

The four interned leaves derive the abstract **`FoldingIdx`** (ctor libwalrus `0x18d6fc0`),
which inserts an `llvm::FoldingSetNode` after the Expr header:

```text
+0x00  Expr header (vptr / refcount / kind / ctx)
+0x20  FoldingSetNode {hash-low | longA}      ← interned payload re-interpreted per leaf
+0x30  {hash-hi | longB}                       ← (longs a5/a6/a7 = lo/ub/step/offset)
+0x40  FoldingSetNode::NextInBucket (init 0)
+0x48  FoldingSetNodeIDRef (the profile/hash)
+0x50  longC
```

Construction goes through `FindNodeOrInsertPos(ctx+0x68, profile, …)`: it returns an
existing structurally-equal node or inserts a fresh bump-allocated one (the arena's
`BumpPtrAllocator` is at `ctx+0x08`). This is the mechanism behind the `AffineIdx*`-keyed
eval map; without it the map key would not be unique.

---

## 2. Collective-rank arithmetic

### Purpose

A sharded collective (FSDP-style) computes a per-replica rank/shard from the loop-nest plus
the replica-group configuration. Four exprs do this math; they couple to the BIR-level
`InstGetCurProcessingRankID` (whose `iteration_id`/`channel_id` fields mirror
`CCGetRankExpr`'s) and to the reduce-op (`AluOpType`) which is orthogonal — the reduce
*operation* and the rank *math* are separate. `CCDiv` of a rank by a group size gives the
shard **index**; `CCMod` gives the within-shard **offset**.

### CCGetRankExpr (kind 2) — current rank in a replica group

```c
// pelican::CCGetRankExpr::create  @ libBIR 0x3bebf0   (alloc 0x30)
function CCGetRankExpr_create(ctx, iteration_id, channel_id):   // a1=ctx, a2/a3 packed
    node = operator_new(0x30)
    Expr_ctor(node, /*kind=*/2, ctx)              // sub_5FE310: refcount=1, kind=2, ctx
    node[+0x20] = iteration_id                    // _mm_insert_epi64 packs both into node[2]
    node[+0x28] = channel_id
    node.vptr = &off_90BFA8                        // _ZTVN7pelican13CCGetRankExprE + 0x10
    if (iteration_id | channel_id) & 0x8000000000000000:   // combined sign check: either < 0
        throw PelicanAssertion("Invalid iteration_id or channel_id for CCGetRankExpr!",
                               "AffineExpr.h:326")
    return node
```

The `(a2 | a3) & sign-bit` test is the compiler's branchless form of
`iteration_id >= 0 && channel_id >= 0`. Wire (v2): `{"kind":"CCGetRankKind",
"iteration_id":N, "channel_id":M}`; v1 reduces to `op="CCGetRank", op_param=iteration_id`.

### CCDivExpr (kind 27) and CCModExpr (kind 28) — rank division / modulo

Both extend the abstract `BinaryExpr` (`numer@+0x20`, `denom@+0x28`) with a third field at
`+0x30`; the only difference is the kind tag and the vtable.

```c
// pelican::PelicanContext::createCCDivExpr  @ libwalrus 0x18f5d90   (alloc 0x38)
// pelican::PelicanContext::createCCModExpr  @ libwalrus 0x18f5e30   (alloc 0x38)
function createCCDivExpr(ctx, numer, a3, denom, replica_groups_id):
    node = tc_new(56)                             // 0x38 bytes, BinaryExpr base
    BinaryExpr_ctor(node, /*kind=*/27, numer, a3, denom)    // CCMod: kind 28, identical else
    if denom <= 0:
        throw PelicanAssertion(...)               // denom > 0 required
    node[+0x30] = replica_groups_id               // node[6] = a5
    node.vptr = &off_3DA9A48                        // CCMod uses off_3DA9B98
    inc_ref(node + 0x08)                            // nanobind::intrusive_counter
    return node
```

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| (BinaryExpr) | `+0x00` | `0x30` | Expr header + `numer@+0x20` + `denom@+0x28` | CERTAIN |
| `numer` | `+0x20` | `RefPtr<Expr>` | dividend (inc_ref'd); typically a `CCGetRankExpr` or `ShardId` | CERTAIN |
| `denom` | `+0x28` | int64 | divisor = group/shard size; `assert denom > 0` | CERTAIN |
| `replica_groups_id` | `+0x30` | uint64 | selects which replica-group table the group size came from | CERTAIN |

The `replica_groups_id` is the rank→shard mapping selector: it names the replica-group table
whose group size was the divisor. `CCDiv(rank, group_size, rg_id)` = the shard the replica
belongs to; `CCMod(rank, group_size, rg_id)` = its lane within that shard.

> **GOTCHA —** `CCModExpr` (kind 28) is **not directly wire-serialized**. The `toJsonv2`
> kind switch (`0x3bab90`) has cases `{2,3,6,7,0xD,0x11,0x12,0x17,0x19,0x1A,0x1B}` only — `0x1B`
> (=27) is `CCDiv`, and there is **no case for 28**. A `CCMod` node only ever reaches JSON as a
> sub-tree of a `CCDiv`/`Sum` that recurses, or is resolved/lowered before BIR-JSON emission.
> A serializer that round-trips a top-level `CCMod` will hit the `default:` arm
> (`report_fatal_error("Unsupported expression kind")`). [absence = HIGH]

### ShardId (kind 13) — the shard identifier

`createShardId(ctx, long ub)` @ libwalrus `0x18f66e0` (alloc `0x58`, FoldingIdx-interned;
FoldingSet profile `{13, tag 0x2000000003, ub}`). A `ShardId` is a symbolic "which shard
am I" index with a known upper bound — `ub@+0x28` is the shard count. It is consumed as an
`AffineIdx` in shard-parametric access patterns (the partition dimension of a sharded
tensor). Its `_ZTI` is at libBIR `0x8ffaa0`, vtable `0x8ffae8`; `getNameStr`/`str`
(`0x18d3e60` / `0x19010f0` walrus) format the shard label. `bir::Function::getOrCreateShardId`
(libBIR `0x17b780`) is the bir-level interner that hands these out.

---

## 3. Runtime expressions — the dynamic-shape spine

### Purpose

Some values are unknowable at compile time: a gather argument id, a runtime loop bound, an
indirect offset. The runtime family parks these as opaque loop-invariant scalars. Both
members report `getLoopdepth → −1` (loop-invariant, never inside a nest).

### IndirectArgExpr (kind 3) — a gather argument id

```c
// pelican::IndirectArgExpr::create  @ libBIR 0x3becf0   (alloc 0x28)
function IndirectArgExpr_create(ctx, arg_id):
    node = operator_new(0x28)
    Expr_ctor(node, /*kind=*/3, ctx)
    node[+0x20] = arg_id                          // node[4] = a2
    node.vptr = &off_90C0F0                         // _ZTVN7pelican15IndirectArgExprE + 0x10
    if arg_id < 0:
        throw PelicanAssertion("Invalid arg_id for IndirectArgExpr!", "AffineExpr.h:356")
    return node
```

`arg_id@+0x20` is the symbolic handle that ties a `SymbolicAccessPattern`'s
`tensor_indirect_arg_id` to the indirect index operand feeding a gather. Wire (v2):
`{"kind":"IndirectArgId","arg_id":N}`.

### IntRuntimeValueBase (kind 7) — a register-backed runtime integer

This is the central node. It is constructed *inline* in the `fromJsonv2` `"regref"` branch
— there is no standalone `create`. The reconstruction below is verbatim from the
serializer body:

```c
// fromJsonv2  @ libBIR 0x3bc8c0, the "regref" arm (lines ~547-555)
function fromJsonv2_regref(ctx, inst, json):
    name = json.at("regref")                        // a 6-char-key std::string
    func = Instruction::getFunction(inst)
    reg  = Function::getRegisterByName(func, name)  // resolve name → bir::Register*
    node = operator_new(0x48)
    Expr_ctor(node, /*kind=*/7, ctx)                // sub_5FE310: refcount=1, kind=7, ctx
    node[+0x40] = reg                               // ★ the regref — the backing Register*
    node[+0x20] = -1 (OWORD)                         // idx vector: no static indices
    node.vptr  = &off_8FFC78                          // _ZTVN7pelican19IntRuntimeValueBaseE + 0x10
    node[+0x30] = si128 (from xmmword_7812D0)         // flags init
    inc_ref(node + 0x08)
    return node
```

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| (Expr header) | `+0x00` | `0x20` | vptr / refcount / kind=7 / ctx | CERTAIN |
| (idx vector) | `+0x20` | `0x10` | OWORD set to −1 in ctor (no static indices; `collectIndices` reads a vec) | HIGH |
| (flags) | `+0x30` | OWORD | `si128` init from `xmmword_7812D0` | MEDIUM |
| **`regref`** | `+0x40` | `bir::Register*` | **the backing register** — resolved by `getRegisterByName` | CERTAIN |

The serialized state is just `{kind, regref}`. The register is read from a device register at
execution; the compiler treats the node as an opaque loop-invariant scalar (a dynamic loop
bound, an indirect offset). Wire: `{"kind":"IntRuntimeValueKind","regref":<register-name>}`.

### The resolution spine, both directions

The `+0x40` regref is the pivot the dynamic-shape system turns on. **Inbound** (`fromJsonv2`),
the wire `"regref"` string is resolved to a `bir::Register*` through the owning `Function`
and stored. **Outbound**, both `toJsonv2` and the bir-level `BirIntRuntimeValue::getNameStr`
read the field back the same way:

```c
// toJsonv2 case 7  @ libBIR 0x3bab90:
reg = expr[+0x40]                                   // v6[8] = *(expr + 0x40)
name = std::string(reg + 296, reg + 296 + len@+304) // Register name {ptr@+296, len@+304}
json["kind"]   = "IntRuntimeValueKind"
json["regref"] = name

// bir::BirIntRuntimeValue::getNameStr  @ libBIR 0x3be4a0  (the bir-leaf grafting onto kind 7):
reg = this[+0x40]                                   // *(this + 64)
return std::string(reg + 296, reg + 296 + len@+304) // same Register-name read
```

`bir::BirIntRuntimeValue::str` (`0x3be580`) dispatches through vtable slot ~312 to
`getNameStr` — the same slot the `AffineIV` axis-name accessor uses, because both are
`AffineIdx`-rooted. The Register's name lives at `Register+296` (`{ptr, len}` std::string at
`+296`/`+304`).

> **QUIRK —** there are **two unrelated `regref` fields** in this subsystem, and conflating
> them is a real trap. The kind-3 `bir::RegisterAccessPattern` (a *value object*, not an Expr)
> stores its registers at object offset **`+0xE8`**. The **`+0x40`** regref documented here
> belongs to the `IntRuntimeValueBase`/`BirIntRuntimeValue` *Expr-node* inside a dynamic
> expression tree — the field `rewireDynamicAPRegisters` re-binds when registers are renamed.
> They are different objects; see the correction note in
> [Symbolic-AP → Register-ALU](../penguin/symbolic-ap-register-alu.md#1).

### OpaqueFnExpr — the escape hatch

`OpaqueFnExpr` (vtable libwalrus `0x3da9e70`; own kind value not isolated — ctor inlined,
MEDIUM) wraps an opaque function over a set of AP indices
(`keepApIndicies`/`dropApIndicies`/`substituteIndices`/`projectMin`/`projectMax` manipulate
that set). Its `isLegalDelinearizedAddress` (`0x18e4910`) is a hard `return 0` — an opaque
function can never be statically delinearized, so its presence forces the runtime/indirect
address path. It is the modelling escape hatch for index math the compiler cannot express
affinely.

---

## 4. Index → delinearized-address legality (vtable slot 12 = `+0x60`)

### Purpose

Before an access-pattern dimension can become a real hardware descriptor, the compiler asks:
**can this affine index tree be lowered to a legal delinearized SBUF/PSUM address?** If yes,
the descriptor encoder emits a static 2-D (partition, free) address; if no, the access falls
to the indirect/register path (`RegisterAccessPattern` / `DynamicAPINFO`). The answer is the
virtual `isLegalDelinearizedAddress`, queried per operand through **vtable slot 12 (`+0x60`)**.

### The slot is `+0x60` — confirmed across six subclasses

Reading the `.data.rel.ro` relocs (`readelf -rW`) for each subclass's vtable and subtracting
the vptr base (`_ZTV + 0x10`) yields slot offset `0x60` (slot index 12) in **every** case:

| Subclass | `_ZTV` (walrus) | slot addr | offset | impl @ | rule |
|---|---|---|---|---|---|
| `AffineExpr` | `0x3da8638` | `0x3da86a8` | `0x60` | `0x18c9ea0` | terms-empty `+0x28==0` OR `!isPartitionDim` OR `c@+0x38==0` |
| `SumExpr` | `0x3da9650` | `0x3da96c0` | `0x60` | `0x18d8aa0` | every term legal (recurses `*(vtbl+0x60)` over `n_terms@+0x28`) |
| `MultExpr` | `0x3da93b0` | `0x3da9420` | `0x60` | `0x18e09f0` | delegates to operand[0] (`var@+0x20`) |
| `DivLikeExpr` | `0x3da9500` | `0x3da9570` | `0x60` | `0x18d9f30` | divisor evenly partitions the index range (gcd test) |
| `OpaqueFnExpr` | `0x3da9e70` | `0x3da9ee0` | `0x60` | `0x18e4910` | **always 0** |
| `ICmpExpr` | `0x3daa440` | `0x3daa4b0` | `0x60` | `0x1904d20` | `llvm_unreachable("Not implemented!")` |

The base `AffineIdx::isLegalDelinearizedAddress` (libwalrus `0xb45440`) is `return 1`,
installed at the same slot 12 of the `AffineIdx` vtable (reloc at `0x3da8808`, vptr base
`0x3da87a8`, offset `0x60`).

### Signature

The mangled name `_ZNK7pelican…26isLegalDelinearizedAddressElbb` decodes the signature as
`(long, bool, bool) const`:

```c
bool Expr::isLegalDelinearizedAddress(long axis_or_idx, bool isPartitionDim, bool requirePred) const
```

`isPartitionDim` distinguishes the highest (partition) descriptor dimension from the free
dims; `requirePred` asks whether a boundary predicate (a partial-tile guard) is tolerable.

### The recursive rule

```c
// pelican::SumExpr::isLegalDelinearizedAddress  @ libwalrus 0x18d8aa0
function Sum_isLegal(this, axis, isPartitionDim, requirePred):
    terms = this[+0x20]                           // SmallVector<Expr*> base
    n     = this[+0x28]                           // term count
    for t in terms[0 .. n):
        // dispatch through slot 12 of each term's own vtable:
        if !(*(t.vptr + 0x60))(t, axis, isPartitionDim, requirePred):
            return false                          // short-circuit on first illegal term
    return true

// pelican::DivLikeExpr::isLegalDelinearizedAddress  @ libwalrus 0x18d9f30
//   (FloorDiv / Modulo / Div / CCDiv share this body)
function DivLike_isLegal(this, axis, isPartitionDim, requirePred):
    numer = dyn_cast<AffineIdx>(this[+0x20])       // numerator
    k = numer.kind
    if (k - 6) <= 7:                               // ★ numerator is an AffineIdx (kinds 6..13)
        if requirePred:
            return !AffineIdx::requirePredicate(numer, axis, 0)
        return true
    if k != 17:                                    // must be AffineExpr(17) otherwise
        return false
    // single-term AffineExpr case: gcd test
    if numer.n_terms@+0x28 != 1: return (numer.n_terms == 0) ? handle_const : false
    coeff = AffineExpr::getCoefficient(numer, the_one_index)
    g = gcd(|denom@this+0x28|, |coeff|)
    return !AffineIdx::requirePredicate(the_index, floor_div(denom, g), …)
```

> **GOTCHA —** the `(kind − 6) <= 7` test in `DivLikeExpr` is the *operational definition*
> of the AffineIdx family range: kinds **6..13** are exactly the indices a divide can have as a
> legal numerator. This is the same unsigned-subtract range check the compiler uses everywhere
> to recognise "this operand is an index leaf." A reimplementation that uses a different
> contiguous range for the index family will misclassify divides.

### The lowering decision, stated whole

An access-pattern dimension (a `QuasiAffineExpr` wrapping an Expr tree) becomes a **legal
delinearized SBUF/PSUM address** iff its tree is a **sum of `coef·AffineIdx` terms plus an
optional constant that is zero on the partition dimension**, and **every divisor in any
embedded FloorDiv/Mod/CCDiv evenly partitions its index range** (the gcd test — no boundary
predicate needed). Opaque functions and comparisons are never legal: they force the
indirect/register address path. The companion AP-level check
`bir::QuasiAffineExpr::check_partial` (libBIR `0x3b6ea0`) walks the dimension's `LoopAxis`
vector and returns false the moment it finds an axis flagged partial/predicated — a partial
axis means the delinearized address needs a predicate guard, dovetailing with `requirePred`.

---

## 5. Serialization — the four versioned writers

The pelican exprs (de)serialize through four free functions on `RefPtr<Expr>` (canonical
bodies in libBIR; `isV1` @ `0x3b4110` selects the writer). **`isV1`**: an Expr is
v1-representable iff `kind==17`(AffineExpr), or `kind∈{2,3}`, or `kind∈{25,26,27}` whose
numerator operand (`+0x20`) is itself a flat `AffineExpr`. v1 encodes only a flat affine form
plus the four simple scalar-parametric wrappers; v2 added the nested trees (`Sum`, `Mult`,
`AffineIV`, `ShardId`, `IntRuntimeValueBase`, arbitrary Div/Mod/CCDiv nesting).

| Kind | Class | v2 keys (`toJsonv2`) | v1 keys (`toJsonv1`) |
|---|---|---|---|
| 2 | `CCGetRankExpr` | `kind="CCGetRankKind"`, `iteration_id@+0x20`, `channel_id@+0x28` | `op="CCGetRank"`, `op_param=iter_id` |
| 3 | `IndirectArgExpr` | `kind="IndirectArgId"`, `arg_id@+0x20` | `op="IndirectArgId"`, `op_param=arg_id` |
| 6 | `AffineIV` | `kind="AffIVKind"`, `axis=<name>` | (v2-only) |
| 7 | `IntRuntimeValueBase` | `kind="IntRuntimeValueKind"`, `regref=<reg name @ (+0x40)+296>` | (v2-only) |
| 13 | `ShardId` | `kind="ShardIDKind"`, `ub@+0x28` | (v2-only) |
| 27 | `CCDivExpr` | `kind="CCDivKind"`, `numer@+0x20`, `denom@+0x28`, `replica_groups_id@+0x30` | `op="Div"` (flat numer only) |
| 28 | `CCModExpr` | **not emitted** (see [§2](#2-collective-rank-arithmetic)) | (n/a) |

All four bodies dispatch on `*(Expr+0x10)` (the kind); the `default:` arm calls
`report_fatal_error("Unsupported expression kind")`. The BIR-JSON file-header version literal
selects which pair runs; see [Wire Versioning](wire-versioning.md).

> **NOTE —** `PelicanAssertion` (ctor libBIR `0x3be750`, vtable `0x8fcb68`, `what()`
> `0x2bd040`) is the `std::exception` thrown by every validation guard on this page
> (`iteration_id>=0`, `arg_id>=0`, `denom>0`, `tile | tripcount`, the gcd/predicate failures).
> It formats `"Pelican exception: <msg> at <file:line>"` from a `StringRef` pair.

---

## Function Map

| Symbol | Library @ VA | Role | Confidence |
|---|---|---|---|
| `sub_5FE310` | libBIR `0x5fe310` | Expr base ctor (refcount/kind/ctx/vptr) | CERTAIN |
| `CCGetRankExpr::create` | libBIR `0x3bebf0` | kind-2 ctor; iter/chan @ `+0x20/+0x28` | CERTAIN |
| `IndirectArgExpr::create` | libBIR `0x3becf0` | kind-3 ctor; `arg_id@+0x20` | CERTAIN |
| `createCCDivExpr` | libwalrus `0x18f5d90` | kind-27 ctor; `+0x20/+0x28/+0x30` | CERTAIN |
| `createCCModExpr` | libwalrus `0x18f5e30` | kind-28 ctor (not wire-emitted) | CERTAIN |
| `createShardId` | libwalrus `0x18f66e0` | kind-13 interned ctor; `ub@+0x28` | CERTAIN |
| `createSymbolicIdx` | libwalrus `0x18f6b50` | kind-12 interned ctor; 4 longs | HIGH |
| `createAPIndex` / `createTiledAPIndex` | libwalrus `0x18f68b0` / `0x18f6300` | kind-9 / kind-10 interned ctors | HIGH |
| `fromJsonv2` (regref arm) | libBIR `0x3bc8c0` | builds kind-7; resolves `regref@+0x40` via `getRegisterByName` | CERTAIN |
| `toJsonv2` | libBIR `0x3bab90` | kind switch `{2,3,6,7,0xD,0x11,0x12,0x17,0x19,0x1A,0x1B}` | CERTAIN |
| `BirIntRuntimeValue::getNameStr` | libBIR `0x3be4a0` | bir-leaf read of `regref@+0x40` → Register name | CERTAIN |
| `isLegalDelinearizedAddress` (6 impls) | libwalrus (see [§4](#4-index--delinearized-address-legality-vtable-slot-12--0x60)) | slot-12 (`+0x60`) legality virtual | CERTAIN |
| `QuasiAffineExpr::check_partial` | libBIR `0x3b6ea0` | AP-level partial-axis predicate check | HIGH |

## Re-Verification Ceiling

Verified firsthand against the cp310 decompiled sidecars and `readelf` relocs:

- **`regref@+0x40`** — CERTAIN. Witnessed in `fromJsonv2` (`*(node+64) = getRegisterByName(...)`), `toJsonv2` case 7 (`*(v168+296)` off `v6[8]`), and `BirIntRuntimeValue::getNameStr` (`*(this+64)`). Three independent reads agree.
- **CC kind tags 27 / 28** — CERTAIN. `BinaryExpr_ctor(node, 27, …)` and `(…, 28, …)` literal in `createCCDivExpr` / `createCCModExpr`; distinct vtables `off_3DA9A48` / `off_3DA9B98`.
- **vtable slot 12 = `+0x60`** — CERTAIN. Computed for six subclasses from `.data.rel.ro` relocs; every one resolves to offset `0x60`, slot index 12. AffineIdx base reloc at `0x3da8808` confirms independently.
- **`IntRuntimeValueBase` role** (opaque loop-invariant register scalar) — CERTAIN on the `+0x40`/kind-7/`getLoopdepth→−1` mechanics; the `+0x20` idx-vector (HIGH) and `+0x30` flags (MEDIUM) are reconstructed by role, not pinned to a named type.
- **`isLegalDelinearizedAddress` rules** — CERTAIN on the six impls read in full; the `DivLikeExpr` gcd branch's `requirePredicate` denominator argument (`floor_div(denom,g)`) is HIGH (the gcd loop is unambiguous; the exact arg threading is reconstructed).

Open items (not pinned): kind values 8/11 within the 6..13 range (host `InvariantId`/`APIndexLike`, MEDIUM — ctors inlined); `OpaqueFnExpr`'s own kind value (its behaviour is CERTAIN); the literal field offset of `AffineIV`'s `BirLoopAxis` back-pointer (reached only via the vtable axis-name accessor). No NEFF/BIR-JSON fixture was available; all offsets are cross-checked via ctor ⇄ factory ⇄ serializer agreement across libBIR and libwalrus.

## Cross-References

- [Pelican Expr Core](pelican-expr-core.md) — the Expr base header, refcount, kind, and the affine/binary subclasses (7.17)
- [Pelican Hierarchy](pelican-hierarchy.md) — the 29-typeinfo RTTI roster, vtable shapes, and the `BirIntRuntimeValue : IntRuntimeValueBase : AffineIdx` graft (7.16)
- [Symbolic-AP → Register-ALU](../penguin/symbolic-ap-register-alu.md) — the back-end half of the dynamic-shape spine; the `+0xE8` vs `+0x40` regref correction
- [Dynamic For-Loop](../penguin/dynamic-for-loop.md) — runtime trip-counts that resolve through the same regref mechanism
- [Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md) — where runtime extents become `IntRuntimeValueBase` nodes
- [BIR Value Model](value-model.md) — the `Argument`/`AccessPattern` operands that hold these `RefPtr<Expr>` index trees
- [Wire Versioning](wire-versioning.md) — the v1/v2 BIR-JSON header that selects the serializer pair
