# Pelican Expression Wire Serialization

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), `libBIR.so` (md5 `12bb979f…`). VA == file-offset for `.text`/`.rodata`. Other versions will differ.*

## Abstract

A `pelican::Expr` is the quasi-affine address/index node that Penguin builds for every tensor operand and instruction predicate — `c + Σ cᵢ·idxᵢ` with floordiv/mod/collective-cyclic locals, hash-consed in an `llvm::FoldingSet` (the algebra itself is owned by [Penguin AffineExpr Algebra](../penguin/affine-expr-algebra.md)). This page documents how that in-memory node tree becomes JSON bytes inside a BIR document, and — equally — which nodes have **no** byte form at all. The serializers are four free functions in `libBIR.so`: `toJsonv1`/`toJsonv2` (write) and `fromJsonv1`/`fromJsonv2` (read), selected by the BIR module version (1 = legacy, 2 = modern). A reader familiar with MLIR's `AffineExpr` round-tripping will recognize the shape — a recursive per-kind object writer — but two facts diverge sharply from the obvious design and are the headline findings here.

First, **the wire is a tree, not a DAG**. The `pelican::Expr` graph in memory is a hash-consed DAG: a sub-expression reached through two parents is one node, pointer-identical. The writer does not preserve that sharing. There is no node-id table, no `$ref` key, no visited-set in the recursion; a shared sub-tree is fully re-serialized once per visit. A DAG in memory becomes a (potentially exponentially larger) tree on the wire, and is only re-deduplicated on the *read* side by the `PelicanContext`'s hash-consing.

Second, **three index-leaf kinds have no wire form**: `APIndex` (kind 9), `TiledAPIndex` (kind 10), and `SymbolicIdx` (kind 12). They are first-class members of the live access-pattern algebra, but they are not cases in either writer — an instance reaching the serializer hits `default:` and a fatal error. They are lowered/expanded to descriptor strides *before* JSON emission, so they never reach it. The only index leaves that survive to the wire are `AffineIV` (kind 6, by axis-name) and `ShardId` (kind 13, by upper bound). This absence is not an oversight to be filled in by transcription — the absence *is* the finding.

For reimplementation, the contract is:

- The exact set of **11 written kinds** in the v2 (modern) format and their per-kind field encoding, plus the **7-kind** v1 (legacy) subset.
- The **tree-expansion** discipline of the recursive writer and the proof that no node-sharing survives serialization.
- The **no-wire-form** rule for `APIndex`/`TiledAPIndex`/`SymbolicIdx`, and the read-side asymmetry (`CCModKind` is read-accepted but never written).
- The v1 ↔ v2 structural delta: flat op-tagged affine vs. faithful recursive tree.

| | |
|---|---|
| **Write dispatch** | `bir::QuasiAffineExpr::toJson(json&, uint version)` @ `0x3bbda0` — `version==1`→`toJsonv1`, `==2`→`toJsonv2`, else fatal |
| **Read dispatch** | `bir::QuasiAffineExpr::createFromJson(Instruction*, json&)` @ `0x3bd8d0` — `Module::getVersion()`→`fromJsonv1`/`fromJsonv2` |
| **`isV1`** | `0x3b4110` (151 B) — v1-representability predicate |
| **`toJsonv1`** | `0x3b9f30` — legacy flat-affine writer (7 kinds) |
| **`toJsonv2`** | `0x3bab90` — modern recursive-tree writer (11 kinds) |
| **`fromJsonv1`** | `0x3bbe90` (2596 B) — legacy reader |
| **`fromJsonv2`** | `0x3bc8c0` (4109 B) — modern reader |
| **Kind field** | `*(Expr + 0x10)` — the LLVM-RTTI discriminant |
| **Fatal emitter** | `sub_679F20` (`report_fatal_error`) |

---

## The Four Serializers

### Purpose

The pelican wire layer is four free functions, not vtable slots — a deliberate choice, since `toJson`/`fromJson` for an LLVM-RTTI'd hierarchy could have been virtual but instead live as central-`switch`-on-kind free functions. They are owned by `libBIR.so`; the same object code is statically linked into `libwalrus.so` and `libBIRSimulator.so`. A `pelican::Expr` never appears bare on the wire — it is always wrapped by a `bir::QuasiAffineExpr` (`{RefPtr<Expr> @ +0x00, flat_set<LoopAxis*> @ +0x08}`), and the QAE is the unit BIR serializes; its `expr` field is what reaches the four functions.

### Entry Point

```text
bir::QuasiAffineExpr::toJson(json&, uint version)        0x3bbda0   ── WRITE dispatch
  ├─ version==1 → toJsonv1(RefPtr<Expr>, json&)          0x3b9f30   ── asserts isV1(expr)
  └─ version==2 → toJsonv2(RefPtr<Expr>, json&)          0x3bab90   ── faithful tree
bir::QuasiAffineExpr::createFromJson(Instruction*, json&) 0x3bd8d0  ── READ dispatch
  ├─ getModule()->getVersion() == 1 → fromJsonv1(ctx, instr, json&) 0x3bbe90
  └─                            == 2 → fromJsonv2(ctx, instr, json&) 0x3bc8c0
adl_serializer<QuasiAffineExpr>::to_json                  0x482620   ── HARDWIRES version=2
```

### Algorithm — the read/write asymmetry

The two sides take different arguments, and the asymmetry is structural, not incidental.

```c
// WRITER: context-free. It just walks the live tree.
void toJsonv2(RefPtr<Expr> e, json& out);      // 0x3bab90
// READER: context-bound. Needs the arena to mint/unique nodes AND the
//   instruction to resolve by-NAME leaves (axes, registers).
RefPtr<Expr> fromJsonv2(PelicanContext* ctx,   // 0x3bc8c0  the interning arena
                        bir::Instruction* instr,//          the name resolver
                        json const& j);
```

The writer needs no context because every datum it emits is read directly off the live node. The reader needs the `Instruction*` because the wire stores some leaves *by name*, not by value: an `AffineIV` writes only its axis name string, and an `IntRuntimeValue` writes only its register name. To rebuild those, `fromJsonv2` calls `bir::Instruction::findAxis(instr, name)` (loop-axis resolution), `bir::Function::getRegisterByName(...)` (register resolution, via `instr.getFunction()`), and `Function::getOrCreateShardId(...)` (shard interning) — all three call targets are confirmed in the `fromJsonv2` body. The arena (`PelicanContext*`) supplies the `BumpPtrAllocator` and `FoldingSet` that re-unique structurally-equal nodes on import (see [§ Tree-Not-DAG](#tree-not-dag--the-headline-quirk)).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `isV1` | `0x3b4110` | v1-representability predicate; `toJsonv1` asserts it | CERTAIN |
| `toJsonv1` | `0x3b9f30` | legacy flat-affine writer, 7 kinds | CERTAIN |
| `toJsonv2` | `0x3bab90` | modern recursive-tree writer, 11 kinds | CERTAIN |
| `fromJsonv1` | `0x3bbe90` | legacy reader (affine-body-then-op) | CERTAIN |
| `fromJsonv2` | `0x3bc8c0` | modern reader (compare-ladder on `"kind"`) | CERTAIN |
| `sub_3B2930` | — | build a C-literal `std::string` json (the kind tag) | HIGH |
| `sub_3B3540` | — | `obj[key] = value` json assign | HIGH |
| `sub_638F80` | `0x638f80` | intrusive `inc_ref` on `Expr + 0x08` | HIGH |
| `sub_3B25E0` | `0x3b25e0` | `RefPtr` release (`dec_ref` + maybe-delete) | HIGH |
| `sub_679F20` | `0x679f20` | `report_fatal_error` | CERTAIN |

---

## `isV1` — the v1-Representability Gate

### Purpose

`isV1(RefPtr<Expr>)` answers one question: can this whole tree be written in the legacy flat format? `toJsonv1` asserts it up front (`isV1(expr) && "Pelican Expr should be in V1 format"`); a tree that fails is structurally inexpressible in v1, which is the entire reason v2 exists. Note that `isV1` does **not** select the format — the BIR module version does (see [§ Version Dispatch](#version-dispatch--the-pelican-version-is-the-bir-version)). It is purely a precondition.

### Algorithm

```c
bool isV1(RefPtr<Expr> e):                       // 0x3b4110, 151 B
    kind = *(e + 0x10)                            // the RTTI discriminant
    if kind == 17:           return true          // AffineExpr — the flat affine form
    if kind <= 17:           return (kind - 2) <= 1   // kind in {2,3}: CCGetRank / IndirectArg
    if (kind - 25) > 2:      return false         // not Div/Mod/CCDiv → not v1
    // kind in {25,26,27}: v1-OK *only if* the numerator operand is itself a flat AffineExpr
    inc_ref(operand)                              // sub_638F80 — touch child via temp RefPtr
    ok = (*(operand + 0x10) == 17)                // operand at e[4] == e+0x20
    if dec_ref(operand): operand.vtable[1](...)   // release; if refcount→0, deleting dtor
    return ok
```

So v1 can express **only**: a flat `AffineExpr` (17); a `CCGetRank` (2) or `IndirectArg` (3) scalar wrapper; or a `FloorDiv`/`Modulo`/`CCDiv` (25/26/27) *whose numerator is directly a flat affine*. Any nested arithmetic — a `Sum`, a `Mult`, a Div-over-Div, an `AffineIV`/`ShardId`/runtime leaf inside a Div — forces v2. The `inc_ref`/`dec_ref` around the operand peek is the same intrusive-refcount discipline used throughout the wire path (`RefPtr` guards both the null pointer and the `DenseMapInfo` empty/tombstone sentinels `0xFF..FD`/`0xFF..FE` before touching `Expr + 0x08`).

---

## `toJsonv2` — the Modern, Faithful Tree Writer

### Purpose

`toJsonv2` is the only format the live compiler emits (every `QuasiAffineExpr::toJson` call site passes `version=2`; see [§ Version Dispatch](#version-dispatch--the-pelican-version-is-the-bir-version)). Every node becomes an object `{ "kind":"<Tag>", …fields… }`. It is a `switch` on `*(Expr + 0x10)` with exactly eleven handled cases and a fatal `default`.

### Algorithm — the dispatch and the eleven kinds

```c
void toJsonv2(RefPtr<Expr> e, json& out):              // 0x3bab90
    switch (*(e + 0x10)) {                             // IDA: "switch 28 cases", ja → default
      case 2:  emit "CCGetRankKind"  + iteration_id=e[+0x20], channel_id=e[+0x28]
      case 3:  emit "IndirectArgId"  + arg_id=e[+0x20]
      case 6:  emit "AffIVKind"      + axis=<vtable slot+312 call: the LoopAxis NAME string>
      case 7:  emit "IntRuntimeValueKind" + regref=<name of bir::Register* at e[+0x40]>
      case 13: emit "ShardIDKind"    + ub=e[+0x28]      // llvm::cast<ShardId> asserts kind==13
      case 17: emit "AffExprKind"    + terms:[{idx:<RECURSE>, coeff:<long>}…], c=e[+0x38]
      case 18: emit "SumKind"        + n_terms:<uint e[+0x28]>, terms:[<RECURSE>…]
      case 23: emit "MultKind"       + var:<RECURSE e[+0x20]>, scale=e[+0x28]
      case 25: emit "FloorDivKind"   + numer:<RECURSE e[+0x20]>, denom=e[+0x28]
      case 26: emit "ModuloKind"     + numer:<RECURSE e[+0x20]>, denom=e[+0x28]
      case 27: emit "CCDivKind"      + numer:<RECURSE e[+0x20]>, denom=e[+0x28],
                                        replica_groups_id=<UNSIGNED e[+0x30]>
      default: report_fatal_error("Unsupported expression kind",
                                  ".../neuronxcc/walrus/ir/lib/IR/QuasiAffineExpr.cpp");
    }
```

> **GOTCHA —** the IDA jump-table analysis of `toJsonv2` @ `0x3bab90` states verbatim: `cmp dword ptr [r14+10h], 1Bh; switch 28 cases` then `ja def_3BABC4; jumptable … default case, cases 0,1,4,5,8-12,14-16,19-22,24`. The default set **explicitly includes 8, 9, 10, 11, 12** — so kinds 9 (`APIndex`), 10 (`TiledAPIndex`), 12 (`SymbolicIdx`) are not silently dropped; they hit `default:` → fatal. The eleven survivors are `{2,3,6,7,13,17,18,23,25,26,27}`. There is no case 28 (`CCMod`) either — see [§ The CCMod Asymmetry](#the-ccmod-asymmetry--read-accepted-never-written).

### Encoding — per-kind v2 field table

The field column lists keys in emit order; the offset is the source struct slot.

| Kind | Tag | Fields (offset → key) | Value type |
|---|---|---|---|
| 2 | `CCGetRankKind` | `+0x20`→`iteration_id`, `+0x28`→`channel_id` | signed long |
| 3 | `IndirectArgId` | `+0x20`→`arg_id` | signed long |
| 6 | `AffIVKind` | vtable+312 → `axis` (LoopAxis name) | string |
| 7 | `IntRuntimeValueKind` | reg name → `regref` (from `*(reg+296)`/`(reg+304)`) | string |
| 13 | `ShardIDKind` | `+0x28`→`ub` | signed long |
| 17 | `AffExprKind` | `terms:[{idx:<expr>, coeff}…]`, `+0x38`→`c` | array / long |
| 18 | `SumKind` | `+0x28`→`n_terms`, `terms:[<expr>…]` | unsigned / array |
| 23 | `MultKind` | `var:<expr>` (`+0x20`), `+0x28`→`scale` | object / long |
| 25 | `FloorDivKind` | `numer:<expr>` (`+0x20`), `+0x28`→`denom` | object / long |
| 26 | `ModuloKind` | `numer:<expr>` (`+0x20`), `+0x28`→`denom` | object / long |
| 27 | `CCDivKind` | `numer:<expr>`, `denom` (`+0x28`), `+0x30`→`replica_groups_id` | object / long / **unsigned** |

`n_terms` and `replica_groups_id` are the two unsigned values (nlohmann type tag 6); everything else (`iteration_id`/`channel_id`/`arg_id`/`ub`/`c`/`coeff`/`scale`/`denom`) is a signed `json<long>`. `FloorDivKind`/`ModuloKind` are byte-identical apart from the tag string — both reach a shared `LABEL_43` that emits `denom`.

### Worked exemplars

```json
// kind 17 — AffineExpr, the linear form  c + Σ coeffᵢ·idxᵢ
{ "kind": "AffExprKind",
  "terms": [ { "idx": <toJsonv2(term.first)>, "coeff": <term.second : long> }, … ],
  "c": <e[+0x38] : long> }

// kind 26 — ModuloExpr, numer mod denom (e.g. PSUM bank-period address rotation)
{ "kind": "ModuloKind",
  "numer": <toJsonv2(e[+0x20])>,    // the dividend sub-expression, serialized INLINE
  "denom": <e[+0x28] : long> }
```

The `terms` container is `boost::container::vector<std::pair<RefPtr<AffineIdx>, long>>` (16-byte stride; the `vec_iterator` assert names `std::pair<pelican::RefPtr<pelican::AffineIdx>, long int>*`). Each term is `{ "idx": <recursive toJsonv2 of the index node>, "coeff": <int64> }`, `push_back`'d into the `"terms"` array; an empty term set yields `"terms":[]`. The term *key* is `RefPtr<AffineIdx>` — narrower than `RefPtr<Expr>` — so every affine term's index is index-family by construction.

> **QUIRK —** `AffIVKind` (kind 6) writes only the axis *name* via a virtual call (`vtable slot+312`), never the loop's numeric `lo`/`ub`/`step`. The geometry lives on the `bir::LoopAxis` that the name resolves to (`Instruction::findAxis`) on the read side. A reimplementer who expects the trip-count on the pelican wire will not find it there — the wire carries a *reference* to the axis, the descriptor carries the geometry. `ShardId` similarly stores only its `ub` (the shard count).

---

## `toJsonv1` — the Legacy Flat-Affine Writer

### Purpose

`toJsonv1` is present but unreached by any live emitter (no call site passes `version=1`); it survives so the read path's v1 branch has a documented symmetric counterpart and so externally-authored legacy BIR can be understood. It writes a **shallow, op-tagged, affine-centric** schema: every node either *is* a flat affine form, or is one scalar parameter (`op_param`) wrapping a flat affine form.

### Algorithm — the op/op_param wrapper

```c
void toJsonv1(RefPtr<Expr> e, json& out):              // 0x3b9f30
    assert(isV1(e) && "Pelican Expr should be in V1 format");
    switch (*(e + 0x10)) {                             // IDA default: cases 0,1,4-12,14-16,18-24
      case 17: emit { terms:[{AxisLabel:<name>, coef:<long>}…], c:e[+0x38] }   // bare affine
      case 25: emit { op:"Div",  op_param:e[+0x28], …affine body of numer… }
      case 26: emit { op:"Mod",  op_param:e[+0x28], …affine body of numer… }
      case 27: emit { op:<10-char tag>, op_param:e[+0x28], …affine body of numer… }   // CCDiv
      case 2:  emit { op:<10-char tag>, op_param:e[+0x28], …affine body of createAffineInt(e[+0x20])… }
      case 3:  emit { op:"IndirectArgId", op_param:e[+0x20] }                   // no affine body
      case 13: emit { kind:"ShardIDKind", ub:e[+0x28] }                        // IDENTICAL to v2
      default: report_fatal_error("Unsupported expression kind", ".../QuasiAffineExpr.cpp");
    }
```

The handled set is `{2,3,13,17,25,26,27}` — exactly the `isV1`-representable kinds, **7** of them (IDA's `toJsonv1` jumptable defaults `0,1,4-12,14-16,18-24`). The central v1 idea: a `Div`/`Mod`/`CCDiv`/`CCGetRank` is written as the *affine body of its (mandatory kind-17) numerator*, decorated with two extra keys `"op"` and `"op_param"`, all in one flat object. There is **no nested-expr recursion** in v1: the only child it ever inlines is a single `AffineExpr`, and it inlines that as `terms`+`c`, not as a sub-object. The serializer re-casts the numerator to `AffineExpr` (asserts `kind==17`) and runs the same `terms`/`c` emit code used for a bare affine.

### Encoding — the v1↔v2 key delta

| Concept | v1 spelling | v2 spelling |
|---|---|---|
| affine term | `{AxisLabel:<name>, coef:<long>}` | `{idx:<nested expr>, coeff:<long>}` |
| op wrapper | `op:"Div"\|"Mod"\|<10-char>`, `op_param:<int>` | `kind:"…Kind"`, `numer:<expr>`, `denom:<long>` |
| index reference | by axis **NAME** (string) | by nested **expr** (recursive) |
| `ShardId` | `{kind:"ShardIDKind", ub}` | `{kind:"ShardIDKind", ub}` (byte-identical) |

> **NOTE —** the one-letter spelling change `coef` (v1) → `coeff` (v2) is real; both literals are present in `.rodata` and confirmed in the respective writer bodies (`"AxisLabel"`/`"coef"`/`"op"`/`"op_param"` appear only in `toJsonv1`; `"idx"`/`"coeff"`/`"numer"`/`"denom"` only in `toJsonv2`). `ShardId` and `IndirectArg` round-trip through either format; `ShardId` in particular is byte-identical between versions.

The structural verdict: v1 references indices **by name** and can express only a flat affine plus four scalar wrappers; v2 **embeds** indices as nested exprs and expresses arbitrary trees. v2 adds the kinds v1 has no encoding for — `SumKind`(18), `MultKind`(23), `AffIVKind`(6), `IntRuntimeValueKind`(7) — plus arbitrary nesting (`numer`/`var`/`terms` as sub-objects) and the `replica_groups_id` slot on `CCDiv`. This is the entire v1↔v2 BIR-JSON delta; see [v1↔v2 Versioning](wire-versioning.md) for why nothing *outside* pelican changes between versions.

---

## Tree-Not-DAG — the Headline Quirk

### The claim

The in-memory `pelican::Expr` graph is a hash-consed DAG (uniqued in the `PelicanContext`'s `FoldingSet`): a sub-expression reached through two parents is *one* node, pointer-identical. The wire form is a **tree**. Shared sub-expressions are duplicated, not ref-shared.

### The proof from the writer

The recursion that does this is visible in `toJsonv2`: every nested operand is rebuilt by an unconditional recursive `toJsonv2()` self-call on the inner node.

```c
// case 17 (AffExprKind) — each term's idx recurses, unconditionally, per term:
for each (idx, coeff) in e.terms:
    json term;
    toJsonv2(idx, term["idx"]);      // ← recursive self-call, no visited-set guard
    term["coeff"] = coeff;
    out["terms"].push_back(term);

// case 18 (SumKind) — each summand recurses (the LABEL_85 loop):
for each child in e.children[0 .. n_terms):
    json sub;  toJsonv2(child, sub);  out["terms"].push_back(sub);

// cases 23/25/26/27 — the single operand recurses inline:
toJsonv2(e[+0x20], out["var" | "numer"]);   // var (Mult) / numer (Div/Mod/CCDiv)
```

Three properties establish tree-expansion, all confirmed against the body:

1. **No visited-set / id-table.** The recursion threads no "seen nodes" structure. Each `toJsonv2` call is unconditional on reaching an operand slot.
2. **No `$ref` / `id` key anywhere in the schema.** The complete v2 key set is `{kind, terms, n_terms, idx, coeff, c, var, scale, numer, denom, replica_groups_id, axis, arg_id, regref, ub, iteration_id, channel_id}` — there is no node-identity key. A DAG cannot be reconstructed from these keys because nothing names a node.
3. **The refcount is for lifetime only.** Every place the writer touches a child it does `if (p && p <= 0xFF..FD) inc_ref(p+8)` before and `dec_ref` (`sub_3B25E0`) after — but this is intrusive memory-lifetime safety during the walk, *not* wire-level sharing. The `inc_ref`/`dec_ref` pair guarantees the child outlives its serialization; it has no effect on whether the child is written once or twice.

> **GOTCHA —** because the writer has no sharing and no recursion-depth cap visible in the body, a memory DAG with N-fold sharing can blow up to an exponentially larger wire document. A reimplementer who reproduces the *algebra* with DAG sharing but the *writer* naively will produce correct-but-bloated JSON for deeply-shared address expressions (e.g. a modulo numerator that itself appears in several terms). The compiler tolerates this because pelican address trees are shallow in practice, but the wire format does not bound it.

### How the read side re-deduplicates

Sharing is recovered on import, inside the context, not on the wire. `fromJsonv2` rebuilds every node through `PelicanContext` factories (`createAffineExpr` / `createModuloExpr` / `createMultExpr` / …) that hash-cons structurally-equal nodes, and the interned leaves go back through the same uniquers — `Function::getOrCreateShardId(ub)` for `ShardId`, `Instruction::findAxis(name)` for `AffineIV`. So a tree-expanded DAG is re-folded into a DAG on load: round-trip memory-DAG → tree-wire → memory-DAG is **value-preserving but not pointer-stable** across the JSON. (That the re-fold preserves value follows from the factories being uniquers; it was not traced end-to-end.)

---

## The No-Wire-Form Finding — `APIndex` / `TiledAPIndex` / `SymbolicIdx`

### The claim

`APIndex` (kind 9), `TiledAPIndex` (kind 10), and `SymbolicIdx` (kind 12) are members of the live access-pattern index algebra (`FoldingSet`-interned index leaves used inside the access-pattern descriptor math) but have **no JSON form**. They are not cases in `toJsonv1` or `toJsonv2`; an instance reaching either serializer hits `default:` → `report_fatal_error`. The serializable index leaves are `AffIVKind`(6) and `ShardIDKind`(13) **only**.

### The proof

The proof is the exhaustive case set, confirmed from both writers' jump tables:

```text
toJsonv2 @0x3bab90  default cases: 0,1,4,5,8-12,14-16,19-22,24   ← includes 9,10,12
toJsonv1 @0x3b9f30  default cases: 0,1,4-12,14-16,18-24           ← includes 9,10,12
```

Both writers send kinds 9/10/12 to `default:`. Independently, there is **no JSON serializer object code for these classes anywhere in `libBIR.so`** — a search of every decompiled function for a `*APIndex*`/`*TiledAPIndex*`/`*SymbolicIdx*` `toJson`/`createFromJson` returns nothing; the only three functions in `libBIR` that name `APIndex` at all (`sub_604CF0`, `sub_604860`, `sub_6043C0`) are access-pattern-algebra functions, none of which reference `json` or `toJson`.

### What happens instead

An `APIndex`-rooted access-pattern dimension is lowered to a plain descriptor **stride/num** before JSON emission — it is written as part of the physical access-pattern descriptor, not as a pelican `Expr`. So in a serialized `AffExprKind`, the only `terms[i].idx` values that can legally appear are an `AffIVKind` (axis-name) or a `ShardIDKind` (shard ub); an access-pattern index leaf that did not lower would crash the writer rather than appear on the wire.

> **QUIRK —** this is the inverse of the obvious assumption. One expects every index node kind to have a wire encoding. In fact the *access-pattern* index family (`APIndex`/`TiledAPIndex`/`SymbolicIdx`) is wire-invisible by design: it is an *internal* algebra that must be fully resolved to descriptor strides before serialization. The pelican wire vocabulary for indices is just two leaves — a loop-induction-variable name and a shard upper-bound. The absence is the contract, not a gap to transcribe.

---

## The Read Side — `fromJsonv2` / `fromJsonv1`

### Purpose

The readers rebuild a live `RefPtr<Expr>` from the JSON via the `PelicanContext` (which mints and uniques nodes) and the `Instruction` (which resolves by-name leaves). Dispatch is **not** a switch — it is a ladder of `std::string::compare()` on `j.at("kind")` (v2) or on the affine body plus `j["op"]` (v1).

### Algorithm — `fromJsonv2`

```c
RefPtr<Expr> fromJsonv2(ctx, instr, j):            // 0x3bc8c0, 4109 B
    s = j.at("kind");                              // asserts json string
    if s == "AffIVKind":  name=j.at("axis"); axis=Instruction::findAxis(instr, name);
                          return dyn_cast<LoopAxis>(axis)         // resolved BY NAME
    if s == "ShardIDKind": return Function::getOrCreateShardId(instr.getFunction(), j.at("ub"))
    if s == "AffExprKind": for t in j.at("terms"):
                              idx = cast<AffineIdx>(fromJsonv2(t.at("idx")));  // kind∈6..13
                              flat_map[idx] = t.at("coeff");
                           return createAffineExpr(ctx, flat_map, j.at("c"))   // boost flat_map
    if s == "SumKind":     for t in j.at("terms"): vec.push(fromJsonv2(t));
                           assert(vec.size() == j.at("n_terms") && "Number of terms don't match");
                           return createNArySum(ctx, vec)
    if s == "MultKind":    return createMultExpr(ctx, fromJsonv2(j.at("var")), j.at("scale"))
    if s == "FloorDivKind":return createFloorDivExpr(ctx, fromJsonv2(j.at("numer")), j.at("denom"))
    if s == "ModuloKind" || s == "CCModKind":                    // ← BOTH hit the Modulo factory
                           return createModuloExpr(ctx, fromJsonv2(j.at("numer")), j.at("denom"))
    if s == "CCDivKind":   return createCCDivExpr(ctx, fromJsonv2(j.at("numer")),
                                                  j.at("denom"), j.at("replica_groups_id"))
    if s == "IndirectArgId":        return IndirectArgExpr::create(ctx, j.at("arg_id"))
    if s == "IntRuntimeValueKind":  reg = Function::getRegisterByName(instr.getFunction(),
                                                                      j.at("regref"));
                                    return <inline kind-7 node, +0x40=reg>
    if s == "CCGetRankKind":        return CCGetRankExpr::create(ctx, j.at("iteration_id"),
                                                                 j.at("channel_id"))
    report_fatal_error("Walrus doesn't support this kind of Expr: " + s, ".../QuasiAffineExpr.cpp");
```

The recursion is symmetric to the writer: every nested operand is rebuilt by a recursive `fromJsonv2` on the inner object, held by `RefPtr`; the factory `inc_ref`s it into the parent, the local temp `dec_ref`s. The complete read-side tag set — confirmed as `lea`-to-rodata references in the `fromJsonv2` body — is `{AffExprKind, AffIVKind, CCDivKind, CCGetRankKind, CCModKind, FloorDivKind, IndirectArgId, IntRuntimeValueKind, ModuloKind, MultKind, ShardIDKind, SumKind}` = **12 tags** (the 11 written kinds plus the read-only `CCModKind`).

### Algorithm — `fromJsonv1`

```c
RefPtr<Expr> fromJsonv1(ctx, instr, j):            // 0x3bbe90, 2596 B
    // (1) parse the flat affine body:
    for t in j.at("terms"):
        axis = Instruction::findAxis(instr, t.at("AxisLabel"));
        flat_map[axis] = t.at("coef");
    base = createAffineExpr(ctx, flat_map, j.at("c"));
    // (2) if an "op" key exists (a std::map lookup, not at()), re-expand the wrapper:
    if "op" in j:
      switch j["op"]:
        "Mod"           → return createModuloExpr(ctx, base, j.at("op_param"))
        "Div"           → return createFloorDivExpr(ctx, base, j.at("op_param"))
        "CCGetRank"     → return createCCDivExpr(ctx, base, j.at("op_param"), 0)  // ← decodes to a CCDIV
        "IndirectArgId" → assert(base.equal(createAffineInt(ctx, 0)));
                          return IndirectArgExpr::create(ctx, j.at("op_param"))
    return base;       // no "op" key → the bare AffineExpr
```

> **GOTCHA — the v1 `"CCGetRank"` tag does not decode to a `CCGetRank` node.** It rebuilds
> a `CCDivExpr`; the `.rodata` assert `"Only CCDiv can be converted to CCGetRank"` is the
> tell. v2 carries a distinct `CCGetRankKind` that does rebuild an actual `CCGetRankExpr`.
> So the `CCGetRank`↔`CCDiv` relationship is version-dependent: v1 collapses the two, v2
> keeps them apart.

### The CCMod Asymmetry — read-accepted, never written

`CCModExpr` (kind 28) is **not** a case in either `toJsonv1` or `toJsonv2` — it is never written — yet its v2 tag `"CCModKind"` is accepted on *read*, routed to the same `createModuloExpr` factory as `"ModuloKind"`. This is a deliberate read-side permissiveness: the reader can ingest a `CCModKind` that some older or external producer emitted, but this compiler's writer never produces one (a live `CCMod` is lowered or only ever appears as a sub-tree of a recursing parent before emission). The absence-from-write is HIGH (exhaustive case set), the read-acceptance is CERTAIN (the tag is in the `fromJsonv2` compare ladder).

---

## Version Dispatch — the Pelican Version *Is* the BIR Version

### The dispatch

The pelican v1/v2 selector is not an independent counter — it is the BIR module version threaded down. Write and read are gated separately but by the same integer.

```text
WRITE:  QuasiAffineExpr::toJson(json&, uint version) @0x3bbda0
          version==1 → assert isV1, toJsonv1   |  version==2 → toJsonv2   |  else FATAL
READ:   QuasiAffineExpr::createFromJson(Instr*, json&) @0x3bd8d0
          Module::getVersion() == 1 → fromJsonv1  | == 2 → fromJsonv2  | else FATAL
```

`bir::Module::getVersion()` has exactly one caller in all of `libBIR` — `createFromJson` @ `0x3bd8ee` — which is why the 1-vs-2 version controls *precisely* the pelican deserializer and nothing else (no per-opcode schema branches on version). The producer always emits v2: every `QuasiAffineExpr::toJson` call site passes `mov $0x2,%edx`; there is no `mov $0x1` before any `toJson` anywhere. v1 is read-only back-compat.

> **GOTCHA — pelican has no version of its own.** The v1 and v2 *formats* are separate
> codepaths, but which one runs is decided by the single shared `bir::Module::getVersion`.
> There is no pelican version field to read, set, or bump independently.

> **GOTCHA —** the one exception: `adl_serializer<QuasiAffineExpr>::to_json` @ `0x482620` **hardwires version=2** (`mov edx,2`). Any QAE serialized through the nlohmann ADL hook — notably `DynamicAPINFO.offset_expr` — is always v2, regardless of the module version. Explicit `QAE::toJson(json&, version)` callers honor the module version; ADL callers force v2.

---

## Where Pelican JSON Sits in the BIR Record

A `pelican::Expr` JSON object never appears bare; the `bir::QuasiAffineExpr` is the unit BIR serializes, and three embedding sites carry one (all confirmed from the owning `toJson` bodies):

| Site | Owner `toJson` | Key | Notes |
|---|---|---|---|
| Symbolic access pattern | `SymbolicAccessPattern::toJson` @ `0x3d7a20` | `"addrs":[<QAE>…]` under `"kind":"symbolic_ap"` | one pelican Expr per AP dimension (`vector<QuasiAffineExpr>`, 32-byte stride) |
| Dynamic DMA offset | `DynamicAPINFO::toJson` @ `0x268c00` | `"offset_expr":<QAE>` via the ADL hook | a single Expr = the runtime byte offset; **always v2** |
| Loop-axis bound / `AffineIV` | (instruction loop `order` vector) | `AffIVKind` axis-name | geometry lives on the resolved `LoopAxis`, not the wire |

Only the `symbolic_*` / `register_*` AP variants carry pelican exprs; a `physical_ap` carries plain integer strides (and is where a lowered `APIndex` dimension's stride ends up). The AP kind discriminator is one of `{physical_ap, symbolic_ap, symbolic_pwap, register_ap, imm_value, symbolic_imm_value, imm_array, register_access}`.

---

## Evidence anchors and limits

| Claim | Evidence | Verdict |
|---|---|---|
| Exactly **11 kinds** written in v2 | `toJsonv2` @ `0x3bab90` jumptable: handled `{2,3,6,7,13,17,18,23,25,26,27}`, default `0,1,4,5,8-12,14-16,19-22,24` | CERTAIN |
| Exactly **7 kinds** written in v1 | `toJsonv1` @ `0x3b9f30` jumptable: handled `{2,3,13,17,25,26,27}`, default `0,1,4-12,14-16,18-24` | CERTAIN |
| Tree-expansion, not DAG | unconditional recursive `toJsonv2()` self-calls (terms idx / Sum loop / var / numer); no `$ref`/`id` in the key set; refcount is lifetime-only | CERTAIN |
| `APIndex`/`TiledAPIndex`/`SymbolicIdx` have no wire form | kinds 9,10,12 in both writers' `default:` sets; no `*APIndex*`-`json` function in `libBIR` | CERTAIN |
| v1↔v2 encoding delta (`AxisLabel`/`coef`/`op`/`op_param` vs `idx`/`coeff`/`kind`/`numer`/`denom`) | literals confined to their respective writer bodies; both present in `.rodata` | CERTAIN |
| `CCModKind` read-accepted, never written | tag in `fromJsonv2` compare ladder (→`createModuloExpr`); absent from both writers' case sets | CERTAIN on read; HIGH on write-absence (exhaustive case set) |
| Read-side re-dedup value-preserving | factories (`createAffineExpr`/`getOrCreateShardId`/`findAxis`) are uniquers | HIGH (not traced end-to-end) |

The kind sets, the tag spellings, the field offsets, the recursion structure, and the no-wire-form default sets are all byte- or jumptable-pinned. Three things stop short of that. The intra-body field *read order* inside `fromJsonv1`/`fromJsonv2` is established at the tag/key/factory level, not by an instruction-by-instruction pass. The value-preservation of the memory-DAG → tree → memory-DAG round-trip rests on the factories being uniquers rather than on an executed round-trip. And the calendar date when v2 superseded v1 is not recoverable from this snapshot at all.

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::QuasiAffineExpr` | the wrapper (`{RefPtr<Expr>, flat_set<LoopAxis*>}`) BIR serializes; its `expr` reaches the four functions |
| `SymbolicAccessPattern` | embeds `[QAE]` under `"addrs"` — per-dimension symbolic strides |
| `DynamicAPINFO` | embeds `offset_expr` (a QAE) via the v2-forcing ADL hook |
| `PelicanContext` | the arena/uniquer the readers rebuild into; re-dedups the tree-expanded wire |

## Cross-References

- [v1↔v2 Versioning — the pelican-Expr Encoding Gate](wire-versioning.md) — the module-version dispatch (`getVersion`'s sole caller); why nothing outside pelican changes between versions
- [Penguin AffineExpr Algebra over pelican::Expr](../penguin/affine-expr-algebra.md) — the live `pelican::Expr` node taxonomy, kind tags, and `FoldingSet` uniquing this page serializes
- [AffineExpr / AffinePredicate ⇄ ISL ⇄ pelican Bridge](../penguin/affine-isl-pelican-bridge.md) — the algebra the wire form encodes and how predicates ride the same gate
- [Two-Pass BIR-JSON Loader](json-loader.md) — where `createFromJson` is invoked during instruction reconstruction
- [BIR-JSON Write Path](json-writer.md) — `Instruction::toJson`, skip-default, the always-v2 emit sites
- [NEFF Container Format](../formats/neff-container.md) — *(planned, Part 12)* where the serialized BIR document is packaged
