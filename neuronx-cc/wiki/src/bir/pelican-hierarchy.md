# pelican::Expr Class Hierarchy

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The pelican library is statically linked into both `neuronxcc/starfish/lib/libBIR.so` (9.5 MB) and `neuronxcc/starfish/lib/libwalrus.so` (65 MB); every RTTI record and vtable below is present, byte-for-byte, in both. For `.text`/`.rodata` the virtual address equals the file offset; `.data.rel.ro` sits at VA `0x8f7cc0` / file offset `0x8f6cc0`, a `+0x1000` delta you must subtract before `xxd`-ing a typeinfo or vtable. Other wheels differ — treat every address as version-pinned.*

## Abstract

`pelican` is BIR's symbolic-expression library: the small algebra of integer-valued expression nodes the backend uses to represent every **address and index** it cannot reduce to a compile-time constant — loop induction variables, tiled access-pattern offsets, floor-divisions and moduli of those, collective rank lookups, and the linear affine combinations built on top of them. It is a **separate namespace from `bir::`**; BIR depends on it the way LLVM's NVPTX backend depends on `llvm::APInt`. Penguin's `AffineExpr` algebra ([5.4](../penguin/affine-expr-algebra.md)) and the ISL bridge ([5.x](../penguin/affine-isl-pelican-bridge.md)) are both built *over* `pelican::Expr`, not beside it.

Every expression node is a reference-counted, polymorphic object rooted at a single abstract base, `pelican::Expr`, which in turn inherits an intrusive-refcount mixin from `nanobind` — the same `nanobind::intrusive_base` that lets these C++ objects be handed to Python without a second ownership scheme. The tree splits two ways immediately under `Expr`: `SimpleExpr` (the "atomic / non-arithmetic" side — index leaves and opaque runtime values) and `CompoundExpr` (the "arithmetic tree" side — affine sums, products, div/mod). This page reconstructs the **complete 29-class RTTI tree** edge by edge from Itanium `__si`/`__vmi` base-pointer relocations, then reproduces the **39-slot `Expr` vtable** — the virtual interface (`clone`, `eval`, `simplify`, `canonicalize`, `getInfimumFast`/`getSupremumFast`, `str`, …) that every leaf overrides.

The whole hierarchy is recoverable despite both libraries being stripped of their static symbol tables (`nm` reports "no symbols"). Three binary facts survive stripping and carry the entire reconstruction: the Itanium typeinfo **name strings** in `.rodata` (e.g. `N7pelican4ExprE`), the **RELA relocations** in `.data.rel.ro` that wire each `_ZTI` typeinfo's base pointer, and the **vtable slot relocations** pointing into `.text`. One vtable slot in `libBIR.so` even retains its mangled symbol — `_ZNK7pelican4Expr13getIndicesSetEv` at slot 14 — which pins the entire slot numbering to ground truth.

For reimplementation, the contract is:

- The 29-node inheritance tree: the two-way `SimpleExpr`/`CompoundExpr` split, the **deep** index chain (`AffineIdx → FoldingIdx → InvariantId → APIndexLike → …`), the `OpaqueFnExpr` runtime side, and the div/mod sub-tree where collective ops *derive* their plain counterparts.
- The `nanobind::intrusive_base → RefCountedObject → Expr` refcount spine and why neither base above `Expr` contributes a vtable slot.
- The 39-slot `Expr` virtual interface — what each slot does, which classes override which slots, and why serialization and visitor-dispatch are deliberately *not* in the vtable.
- The `FoldingIdx` multiple-inheritance mixin (`__vmi`, two bases) that pulls in LLVM's `FoldingSet` interning.

| | |
|---|---|
| **Namespace** | `pelican` (BIR's symbolic-expr dependency; *not* `bir::`) |
| **Root / refcount spine** | `nanobind::intrusive_base → pelican::RefCountedObject → pelican::Expr` |
| **Typeinfo records** | **29** pure `_ZTIN7pelican…E` class typeinfos (both libs) |
| **Vtable slots** | **39** virtual slots on `pelican::Expr` (slot0..slot38) |
| **`Expr` TI** | `_ZTIN7pelican4ExprE` @ `0x90a908` (libBIR) |
| **`AffineExpr` vtable** | `0x90abe0` — concrete leaf, overrides all 39 slots; ti-ptr `0x90abe8 → 0x90aa80` |
| **`ModuloExpr` vtable** | `0x90ba10` (matches E17's cited address) |
| **Slot-14 anchor** | `_ZNK7pelican4Expr13getIndicesSetEv` (the one un-stripped vtable symbol) |
| **Smart pointer** | `pelican::RefPtr<T>` (intrusive; inc/dec inlined on refcount@+0x08) |
| **Typeinfo-name table** | `.rodata` cluster `0x79ace0`–`0x79b010` (Expr family) |

---

## 1. The Inheritance Tree

The tree is reconstructed purely from RTTI. Each `_ZTI<class>` typeinfo object in `.data.rel.ro` is one of three Itanium ABI shapes — `__class_type_info` (a root with no base), `__si_class_type_info` (exactly one public base, pointer at TI+0x10), or `__vmi_class_type_info` (multiple/virtual bases, a `base_info[]` array). Reading the base pointer of all 29 records and following the chain yields:

```text
nanobind::intrusive_base                       (external; nanobind intrusive refcount)
  └─ pelican::RefCountedObject                 _ZTI… @0x90a8f0  (si → intrusive_base)
       └─ pelican::Expr   (ROOT of all exprs)  _ZTI… @0x90a908  (si → RefCountedObject)
            ├─ pelican::SimpleExpr  (abstract)  @0x8fd140  (si → Expr)
            │    ├─ pelican::AffineIdx          @0x90ad28  (si → SimpleExpr)
            │    │    ├─ pelican::AffineIV         @0x90ad50   (loop induction-var leaf)
            │    │    ├─ pelican::IntRuntimeValueBase @0x8ffab8 (kind7 runtime scalar)
            │    │    └─ pelican::FoldingIdx     @0x90ad68  *VMI — 2 bases*
            │    │         (AffineIdx @off0  +  llvm::FoldingSetBase::Node @off0x40)
            │    │         └─ pelican::InvariantId   @0x90ada0  (si → FoldingIdx)
            │    │              ├─ pelican::SymbolicIdx @0x90adb8 (kind12 leaf)
            │    │              ├─ pelican::ShardId     @0x8ffaa0 (kind13 leaf)
            │    │              └─ pelican::APIndexLike @0x90add0 (abstract)
            │    │                   ├─ pelican::APIndex      @0x90ade8 (kind9 leaf)
            │    │                   └─ pelican::TiledAPIndex @0x90ae00 (kind10 leaf)
            │    └─ pelican::OpaqueFnExpr        @0x90bf50  (si → SimpleExpr)
            │         ├─ pelican::CCGetRankExpr   @0x90bf68 (kind2 leaf)
            │         └─ pelican::IndirectArgExpr @0x90bf80 (kind3 leaf)
            └─ pelican::CompoundExpr  (abstract)  @0x90aa68  (si → Expr)
                 ├─ pelican::AffineExpr  @0x90aa80 (kind17 leaf — terms vec + const)
                 ├─ pelican::SumExpr     @0x90b418 (kind18 leaf — SmallVector<RefPtr<Expr>,8>)
                 ├─ pelican::ICmpExpr    @0x90c278 (kind20 leaf — compare_op + lhs + rhs)
                 └─ pelican::BinaryExpr  @0x90b430 (abstract — operand + int64)
                      ├─ pelican::MultExpr    @0x90b448 (kind23 leaf)
                      └─ pelican::DivLikeExpr @0x90b460 (abstract — numer/denom, denom>0)
                           ├─ pelican::FloorDivExpr @0x90b478 (kind25)
                           │    └─ pelican::CCDivExpr @0x90b490 (kind27 — +collective)
                           └─ pelican::ModuloExpr   @0x90b4a8 (kind26)
                                └─ pelican::CCModExpr @0x90b4c0 (kind28 — +collective)
```

Two `pelican` typeinfos sit in the namespace but **outside** the `Expr` tree:

- `pelican::PelicanContext` @ `0x90c228` — a `__class_type_info` **root** (no base). The interning arena: a bump-pointer allocator plus the `FoldingSet` that uniques index nodes; every `PelicanContext::create*` factory returns a `RefPtr<Expr>`. It is not an `Expr`, it *owns* them.
- `pelican::PelicanAssertion` @ `0x8fcb28` — `si → St9exception` (`std::exception`). The throw type used by pelican's internal assertions.

That is **26 tree classes + `RefCountedObject` (the intrusive base of `Expr`, in-tree but counted with the spine) + `PelicanContext` + `PelicanAssertion` = 29** (see [§5](#5-the-29-count)).

> **CONFIRMED —** every parent edge above was read mechanically from a RELA base-pointer reloc; not one is inferred. The Python resolver walked all 29 `__si`/`__vmi`/`__class` records and the parent of each matched D-R03's hand-built tree edge-for-edge. The decode is reproduced in [§4](#4-how-the-tree-was-recovered).

### The two-way split, in design terms

The split under `Expr` is the single most important structural fact for a reimplementer, because it tells you where a new node *type* belongs:

- **`SimpleExpr`** is the **leaf / atomic** side: things that are not an arithmetic combination of other expressions. It has no own data fields. Under it live the **index family** (everything that names a coordinate — loop IVs, AP indices, symbolic/shard IDs, runtime scalars) and the **opaque family** (`OpaqueFnExpr` — values produced at runtime by a function call whose delinearized form is "always illegal", i.e. cannot be treated as a legal address; see slot 12 below).
- **`CompoundExpr`** is the **tree / arithmetic** side: a node whose value is computed from child expressions. Also field-less and abstract; its leaves are the affine workhorse (`AffineExpr`), n-ary sum (`SumExpr`), integer compare (`ICmpExpr`), and the binary `operand op int64` family (`BinaryExpr → {MultExpr, DivLikeExpr}`).

> **GOTCHA —** the index family is a **deep chain, not a flat set**. `AffineIdx → FoldingIdx → InvariantId → APIndexLike → {APIndex, TiledAPIndex}` is four inheritance levels, and `FoldingIdx` is where the LLVM-`FoldingSet` interning mixin enters ([§3](#3-the-foldingidx-multiple-inheritance-mixin)). A reimplementer who flattens these into siblings will mis-place the interning seam and the `getNameStr` accessor (`SymbolicIdx`/`ShardId`).

> **QUIRK — collective ops *derive* their plain ops.** `CCDivExpr : public FloorDivExpr` and `CCModExpr : public ModuloExpr` (the binary base pointers at `0x90b490+0x10 → 0x90b478` and `0x90b4c0+0x10 → 0x90b4a8` prove it). The collective div/mod are **specialisations** that add a replica-group field on top of the plain floor-div / modulo — not independent siblings under `DivLikeExpr`. So `simplify()` on a `CCDivExpr` can fall through to `FloorDivExpr`'s arithmetic and only re-attach the collective metadata.

---

## 2. The Refcount Spine — `intrusive_base → RefCountedObject → Expr`

```text
nanobind::intrusive_base                       _ZTIN8nanobind14intrusive_baseE  (external)
  └─ pelican::RefCountedObject  _ZTIN7pelican16RefCountedObjectE @0x90a8f0  (si → intrusive_base)
       └─ pelican::Expr          _ZTIN7pelican4ExprE             @0x90a908  (si → RefCountedObject)
```

`RefCountedObject`'s base pointer (`0x90a8f0+0x10`) resolves to the typeinfo-name string `N8nanobind14intrusive_baseE`, confirming the cross-library `nanobind` dependency directly. `nanobind::intrusive_base` is nanobind's intrusive-refcount mixin: an atomic reference counter plus a Python-object back-pointer slot. In pelican only the counter half is exercised at the C++ level — the Python back-pointer is what lets a `RefPtr<Expr>` be surfaced to nanobind without a second wrapper.

**Neither `intrusive_base` nor `RefCountedObject` has a `_ZTV` vtable of its own** in either library — they contribute **zero** virtual slots. `pelican::Expr` is the first class in the chain to introduce a vtable, so the polymorphic vptr at object offset `+0x00` is *Expr's*, not the refcount base's. The object layout (consistent with the 24-byte `Expr` base recovered in the struct work):

```text
+0x00  vptr             Expr vtable (introduced by Expr, not RefCountedObject)
+0x08  refcount         intrusive counter; initialised to 1 on construction
+0x10  uint32 KIND      ExprKind switch key (the per-leaf discriminator, kinds 2..28)
+0x18  PelicanContext*  owning interning arena / FoldingSet
```

`RefCountedObject`'s sole instance field is the refcount at `+0x08`; `intrusive_base` is folded into that single word (empty-base / single-counter). The smart pointer is **`pelican::RefPtr<T>`** with `intrusive_ptr` semantics — pervasive in the backend: `SumExpr` and `AffineExpr` term vectors are `llvm::SmallVector<RefPtr<Expr>,8>`, the `PelicanContext::create*` factories return `RefPtr<Expr>`, and backend `DenseMap`/`HashSet` containers are keyed by `RefPtr<Expr>` through an `ExprRefHasher`.

> **NOTE —** no out-of-line `addRef`/`release` symbol survives. `RefPtr`'s copy-ctor (atomic inc on `+0x08`) and dtor (atomic dec, free at zero) are **inlined at every use site** — the signature of a header-only intrusive pointer. A reimplementer should expect the refcount arithmetic to appear inline, not as a call. (Confidence: STRONG — inferred from the absence of the symbols plus the inline `lock inc/dec` patterns at `RefPtr` use sites, not from a named body.)

---

## 3. The 39-Slot `Expr` Virtual Interface

`pelican::Expr` declares **39 virtual functions**, slot 0 through slot 38. The count and ordering are recovered from the **`AffineExpr` concrete-leaf vtable at `0x90abe0`**, which overrides essentially every slot and is the authoritative witness: its slots are 39 consecutive `.text` pointers terminated by the next class's typeinfo header (`_ZTVN10__cxxabiv120__si_class_type_infoE` at vtable+`0x10`+39·8). Slot 14 retains its mangled symbol, `_ZNK7pelican4Expr13getIndicesSetEv`, which anchors the absolute slot numbering — the binary's slot 14 *is* `getIndicesSet`.

Slot index = `(fn_va − vtable − 0x10) / 8`. Slot names below come from the `pelican` method symbols retained in `libwalrus.so`; `libBIR.so` holds the **identical slot order** with stripped `sub_5F…/sub_61…` bodies (the `0x5F…/0x60…/0x61…` region the `AffineExpr` slots point into — e.g. slot 5 → `0x5ff850`, slot 26 → `0x6000d0`).

| slot | `pelican::Expr::…` | semantics |
|---|---|---|
| 0 | `~Expr` (D1, complete dtor) | abstract base: empty; leaves fill in |
| 1 | `~Expr` (D0, deleting dtor) | abstract base: empty; leaves fill in |
| 2 | `isConst() const` | is this a constant literal |
| 3 | `getConst() const` | extract the constant value |
| 4 | `isAffine() const` | is this affine-expressible |
| 5 | `clone()` | **deep-copy** this node |
| 6 | `add(Expr*)` | algebraic add (builder) |
| 7 | `simplify()` | **one-step simplify** |
| 8 | `canonicalize()` | reduce to canonical normal form |
| 9 | `hash_value() const` | structural hash (FoldingSet / DenseMap key) |
| 10 | `equal(const Expr*) const` | **structural equality** |
| 11 | `equalInt(long) const` | equals an integer literal |
| 12 | `isLegalDelinearizedAddress(…)` | **is this a legal delinearized address** |
| 13 | `collectIndices(SmallPtrSet<AffineIdx*,16>&)` | gather index leaves into the set |
| 14 | `getIndicesSet() const` | return the index set (the un-stripped anchor) |
| 15 | `hasRuntimeValue() const` | static-vs-runtime discriminator |
| 16 | `hasIndex(AffineIdx*) const` | does the tree reference this IV |
| 17 | `getMaxLoopdepth() const` | deepest loop axis referenced |
| 18 | `getInfimumFast() const` | **range lower bound** (Modulo → 0) |
| 19 | `getSupremumFast() const` | **range upper bound** (Modulo → denom−1) |
| 20 | `str[abi:cxx11]() const` | **toString / pretty-print** (the only in-vtable stringifier) |
| 21 | `projectMax(SmallPtrSet<AffineIdx*,16>&)` | project out indices → maximum |
| 22 | `projectMin(SmallPtrSet<AffineIdx*,16>&)` | project out indices → minimum |
| 23 | `replaceSubExprWith(Expr*, Expr*) const` | substitute a subtree (by-expr) |
| 24 | `replaceSubExprWith(Expr*, long) const` | substitute a subtree (by-int) |
| 25 | `substituteIndices(DenseMap<AffineIdx*,Expr*>&)` | bulk index substitution |
| 26 | `eval(DenseMap<AffineIdx*,long>&) const` | **evaluate** (env → integer) |
| 27 | `eval(DenseMap<AffineIdx*,long>&, long) const` | eval with a partial/default |
| 28 | `dropApIndicies()` | strip AP-index terms |
| 29 | `keepApIndicies()` | keep only AP-index terms |
| 30 | `keepApIndiciesLinearExpr()` | keep AP indices, linear form |
| 31 | `containExpr(const Expr*, long) const` | subtree-containment test |
| 32 | `flattenTerms(SmallVector<RefPtr<Expr>,8>&, bool)` | flatten the n-ary tree |
| 33 | `exactDivSubclass(long)` | exact-division dispatch hook |
| 34 | `replaceSubExprWithExactDivSubclass(Expr*,Expr*,long) const` | div-aware substitute |
| 35 | `simplifyFloordivSubclass(long)` | per-subclass floordiv simplify |
| 36 | `simplifyModSubclass(long)` | per-subclass modulo simplify |
| 37 | `simplifyMultSubclass(long)` | per-subclass mult simplify |
| 38 | `simplifyAddSubclass(long)` | per-subclass add simplify |

### What is *not* in the vtable

- **Serialization** (`toJsonv1`/`toJsonv2`/`fromJsonv1`/`fromJsonv2`) is **not** a vtable slot. These are **free functions** that switch on `KIND@+0x10` ([7.15](wire-versioning.md)). The wire v1↔v2 gate is therefore a *function-level* encoding choice, not a virtual override — the class side documented here is the same for both versions. Slot 20 `str()` is the only stringifier reachable through the vtable.
- **There is no `accept(Visitor&)` slot.** pelican does not use the classic visitor pattern; it dispatches by `KIND` switch plus the template-method `*Subclass` hooks (slots 33–38). A reimplementer should not add a visitor vtable entry expecting it to match.

> **GOTCHA — slot 12 is the "legal address" gate.** `isLegalDelinearizedAddress` lives at slot 12, i.e. byte offset `12·8 = 0x60 = 96` into the function-pointer region — the "vtable+96 virtual" call sites in the delinearization code resolve here. `AffineExpr`, `DivLikeExpr`, and `OpaqueFnExpr` each override it; `OpaqueFnExpr`'s override returns "always illegal", which is the whole reason the opaque/runtime leaves sit on the `SimpleExpr` side.

### The template-method seam (slots 33–38)

Slots 35–38 (`simplify{Floordiv,Mod,Mult,Add}Subclass`) are the customization seam. `Expr::simplify` (slot 7) and `Expr::canonicalize` (slot 8) call into these per-operation hooks, and each concrete leaf specialises the ones relevant to it. This is how, e.g., a `ModuloExpr` participates in a surrounding `simplify` without the base `simplify` knowing about modulo: it calls `simplifyModSubclass`, which `ModuloExpr` overrides. The override pattern (from the `ModuloExpr` vtable at `0x90ba10`) is:

- **own (overridden)**: dtor slots 0/1, `clone` (5), `simplify` (7), `getInfimumFast`/`getSupremumFast` (18/19) — the modulo-specific arithmetic.
- **inherited from `BinaryExpr`**: `hash_value` (9), `equal` (10), `collectIndices` (13), `hasRuntimeValue` (15), `hasIndex`/`getMaxLoopdepth` (16/17), `str` (20), `projectMax`/`projectMin` (21/22) — shared by all `operand op int64` nodes.
- **inherited from `DivLikeExpr`**: `isLegalDelinearizedAddress` (12) — the gcd/partition rule shared by floor-div and modulo.
- **base default from `Expr`**: slots 2,3,4,6,8,11,14.

`AffineExpr` (the affine workhorse) overrides nearly all 39 because it is the widest, most general node; the index and opaque leaves override the fewest, thunking most slots up to the nearest concrete base.

---

## 4. The `FoldingIdx` Multiple-Inheritance Mixin

`FoldingIdx` is the **only** `__vmi_class_type_info` in the tree — the one class with more than one base. Its typeinfo at `0x90ad68` (file offset `0x909d68`) decodes byte-exactly:

```text
0x90ad68  +0x00  vptr        -> _ZTVN10__cxxabiv121__vmi_class_type_infoE   (vmi marker)
          +0x08  name        -> 0x79adb0 = "N7pelican10FoldingIdxE"
          +0x10  flags  u32  = 0x00000000
          +0x14  base_count u32 = 0x00000002         ← TWO bases
          +0x18  base[0].type -> 0x90ad28 = pelican::AffineIdx
          +0x20  base[0].offset_flags = 0x00000002   ← public, offset 0
          +0x28  base[1].type -> 0x90ad40 = llvm::FoldingSetBase::Node
          +0x30  base[1].offset_flags = 0x00004002   ← public, offset (0x4002>>8)=0x40=64
```

So `FoldingIdx : public AffineIdx, public llvm::FoldingSetBase::Node`, with `AffineIdx` as the primary base (offset 0, sharing the `Expr` vptr) and `FoldingSetBase::Node` laid down at object offset **+64**. The `{0x2, 0x4002}` offset-flags pair is byte-identical in `libBIR.so` and `libwalrus.so`.

The second base is what gives the entire index subtree (everything from `FoldingIdx` down — `InvariantId`, `SymbolicIdx`, `ShardId`, `APIndexLike`, `APIndex`, `TiledAPIndex`) its **`FoldingSet` interning**: identical index nodes are de-duplicated/uniqued through LLVM's `FoldingSet`, so pointer-equality on a `RefPtr<AffineIdx>` is value-equality. `PelicanContext` owns that `FoldingSet`. `FoldingIdx` itself retains a named `clone()` body in `libBIR.so` (`pelican::FoldingIdx::clone`), distinct from the base `Expr::clone` at slot 5, because cloning an interned node must re-intern rather than copy.

> **NOTE —** a reimplementer building only the affine/arithmetic side can ignore the `FoldingSetBase::Node` mixin entirely; it only matters for the index family. But if you intern indices, the second base must be laid down at the same +64 offset or the `FoldingSet` node links will corrupt the object.

---

## 5. The "29" Count

The total number of **pure `pelican` class typeinfo objects** — `_ZTIN7pelican<len><Name>E` records, length-prefix validated, in `.data.rel.ro` — is **exactly 29** in `libBIR.so` (and the same 29 in `libwalrus.so`). Broken down:

- **26 in the `Expr` inheritance tree**, of which:
  - **10 abstract bases**: `Expr`, `SimpleExpr`, `CompoundExpr`, `AffineIdx`, `FoldingIdx`, `InvariantId`, `APIndexLike`, `OpaqueFnExpr`, `BinaryExpr`, `DivLikeExpr`.
  - **16 concrete leaves** (one per `ExprKind` enum value): `AffineIV`(6), `IntRuntimeValueBase`(7), `APIndex`(9), `TiledAPIndex`(10), `SymbolicIdx`(12), `ShardId`(13), `CCGetRankExpr`(2), `IndirectArgExpr`(3), `AffineExpr`(17), `SumExpr`(18), `ICmpExpr`(20), `MultExpr`(23), `FloorDivExpr`(25), `ModuloExpr`(26), `CCDivExpr`(27), `CCModExpr`(28).
- **`RefCountedObject`** — the intrusive base of `Expr` (in the chain, but not an `Expr`).
- **`PelicanContext`** — the arena / `FoldingSet` owner (a `__class_type_info` root).
- **`PelicanAssertion`** — the throw type (`: std::exception`).

`26 + 3 = 29`.

> **CORRECTION — `pelican::User` is *not* a 30th RTTI class.** The typeinfo-name string `N7pelican4UserE` is present in `.rodata`, but a full-binary scan of `.data.rel.ro` finds **no typeinfo *object*** for it — it appears only as a parameter type in `bir::BirIntRuntimeValue::{addUser,removeUser,isUsedBy}(pelican::User*)`. `pelican::User` is a non-polymorphic use-tracking handle (a def-use list edge), so the compiler emitted its name string but never an RTTI record. Counting the name string would inflate the total to 30; counting actual typeinfo objects gives the correct 29. (A naive `rg` of class-name strings in `libwalrus.so` also surfaces a `calculateTripcount` artifact that fails the length-prefix check — another reason to count typeinfo *objects*, not name strings.)

> **NOTE — `ExprKind` ≠ class count.** There are 16 concrete leaf classes but the `ExprKind` enum runs through 28; the index-family kinds (6..13) share the `AffineIdx → FoldingIdx → InvariantId` chain, and the v1/v2 JSON wrappers ([7.15](wire-versioning.md)) are not separate classes (same typeinfo, op-tag in the JSON only). Sixteen instantiable classes cover the full kind range.

---

## 6. How the Tree Was Recovered

Both libraries report `nm: no symbols` — the static symbol table is stripped; only the dynamic symbol table survives, and it exports just four `pelican` typeinfos (`SimpleExpr`, `ShardId`, `IntRuntimeValueBase`, `PelicanAssertion`). Everything else is reconstructed from three stripping-resistant sources.

**(a) Typeinfo-name strings** live in `.rodata` as bare Itanium nested names (no `_ZTS` prefix — that prefix exists only in the symbol table). The `Expr` family clusters at `0x79ace0`–`0x79b010`:

```text
0x79ace0  N7pelican16RefCountedObjectE
0x79ad00  N7pelican4ExprE
0x79ad10  N7pelican12CompoundExprE
0x79ad30  N7pelican10AffineExprE
0x79ad50  N7pelican9AffineIdxE  ...  0x79aff0  N7pelican14PelicanContextE
```

Validate each candidate by the length prefix (`N7pelican<len><Name>E` is genuine iff `len == strlen(Name)`); this rejects the `N7pelican13CCGetRankE` / `N7pelican15IndirectArgE` substrings that `rg` matches inside the `::create` method symbols.

**(b) Base-pointer relocations.** For each typeinfo object, the vptr at `+0x00` names the ABI shape, the name at `+0x08`, and the base at `+0x10` (for `__si`). Example — `SimpleExpr` @ `0x8fd140`:

```text
0x8fd140  R_X86_64_64        _ZTVN10__cxxabiv120__si_class_type_infoE + 0x10   (si marker)
0x8fd148  R_X86_64_64        _ZTSN7pelican10SimpleExprE                         (name)
0x8fd150  R_X86_64_RELATIVE  -> 0x90a908  = _ZTIN7pelican4ExprE                 (base = Expr)
```

A script reading all 16 213 RELA entries, keyed each typeinfo by its `+0x00` ABI vptr, resolved the `+0x08` name and the `+0x10` (or `__vmi base_info[]`) base, and emitted the parent of all 29 records. Every edge matched the hand-built tree.

**(c) Vtable slot relocations.** The `AffineExpr` vtable at `0x90abe0` is 39 consecutive `R_X86_64_RELATIVE` slots into `.text` (`0x5fd…/0x60…/0x609…`), terminated at slot 39 by the `_ZTV…__si_class_type_info` header of the adjacent `AffineIdx` vtable. Slot 14 alone retains an `R_X86_64_64` to the named `_ZNK7pelican4Expr13getIndicesSetEv`, fixing the absolute numbering. `ModuloExpr` @ `0x90ba10` and `FloorDivExpr` @ `0x90b8c0` show the same primary-vtable shape, with the shared slot 14 (`getIndicesSet`) landing at the same offset `0x70` past the vptr.

> **CORRECTION —** the `ModuloExpr` and `FloorDivExpr` vtables are **40-slot**, not 39-slot. The 39-slot count is the `Expr` interface, witnessed by the two `CompoundExpr`-direct leaves `AffineExpr` (`0x90abe0`, vptr `0x90abf0`) and `SumExpr` (`0x90b778`, vptr `0x90b788`), both **39** function slots. The five `BinaryExpr`-family nodes — `MultExpr` (vptr `0x90b4e8`), `FloorDivExpr` (`0x90b8d0`), `ModuloExpr` (`0x90ba20`), `CCDivExpr` (`0x90bb70`), `CCModExpr` (`0x90bcc0`) — each carry **40** slots: `BinaryExpr` introduces one extra virtual function appended at slot 39 (past `Expr`'s slot 38). The retained-symbol anchors are unmoved by this — in the `ModuloExpr` table the named `Expr::` slots land at exactly slot 2 (`isConst`), 3 (`getConst`), 4 (`isAffine`), 11 (`equalInt`), 14 (`getIndicesSet`), 31 (`containExpr`), confirming the first 39 slots are the `Expr` interface and only slot 39 is the binary addition. Evidence: `data_tables.json` slot runs at vptr `0x90ba20` (40 entries, slot 39 → `sub_60DA40`) vs `0x90abf0` (39 entries, last slot 38 → `sub_6091E0`).

### Cross-binary identity

`pelican` is a static library compiled once and linked into both `libBIR.so` and `libwalrus.so`. The two are **type-identical** along all three axes: the same 29 typeinfo name strings, the same 29 base edges (including the `FoldingIdx __vmi {0x2, 0x4002}` pair), and the same 39-slot vtable order. The *only* difference is symbolisation — `libwalrus.so` retains the `pelican` method symbols while `libBIR.so`'s bodies are stripped to `sub_`. A reimplementer can read the slot *names* off walrus and the slot *addresses* off BIR; they describe one translation unit.

| Class | `libBIR.so` `_ZTI` | shape | parent (binary-decoded) |
|---|---|---|---|
| `RefCountedObject` | `0x90a8f0` | si | `nanobind::intrusive_base` |
| `Expr` | `0x90a908` | si | `RefCountedObject` |
| `SimpleExpr` | `0x8fd140` | si | `Expr` |
| `CompoundExpr` | `0x90aa68` | si | `Expr` |
| `AffineIdx` | `0x90ad28` | si | `SimpleExpr` |
| `AffineIV` | `0x90ad50` | si | `AffineIdx` |
| `IntRuntimeValueBase` | `0x8ffab8` | si | `AffineIdx` |
| `FoldingIdx` | `0x90ad68` | **vmi** | `AffineIdx` + `llvm::FoldingSetBase::Node` |
| `InvariantId` | `0x90ada0` | si | `FoldingIdx` |
| `SymbolicIdx` | `0x90adb8` | si | `InvariantId` |
| `ShardId` | `0x8ffaa0` | si | `InvariantId` |
| `APIndexLike` | `0x90add0` | si | `InvariantId` |
| `APIndex` | `0x90ade8` | si | `APIndexLike` |
| `TiledAPIndex` | `0x90ae00` | si | `APIndexLike` |
| `OpaqueFnExpr` | `0x90bf50` | si | `SimpleExpr` |
| `CCGetRankExpr` | `0x90bf68` | si | `OpaqueFnExpr` |
| `IndirectArgExpr` | `0x90bf80` | si | `OpaqueFnExpr` |
| `AffineExpr` | `0x90aa80` | si | `CompoundExpr` |
| `SumExpr` | `0x90b418` | si | `CompoundExpr` |
| `ICmpExpr` | `0x90c278` | si | `CompoundExpr` |
| `BinaryExpr` | `0x90b430` | si | `CompoundExpr` |
| `MultExpr` | `0x90b448` | si | `BinaryExpr` |
| `DivLikeExpr` | `0x90b460` | si | `BinaryExpr` |
| `FloorDivExpr` | `0x90b478` | si | `DivLikeExpr` |
| `CCDivExpr` | `0x90b490` | si | `FloorDivExpr` |
| `ModuloExpr` | `0x90b4a8` | si | `DivLikeExpr` |
| `CCModExpr` | `0x90b4c0` | si | `ModuloExpr` |
| `PelicanContext` | `0x90c228` | **base** | *(root — no base)* |
| `PelicanAssertion` | `0x8fcb28` | si | `std::exception` |

---

## 7. Confidence and Limits

| Claim | Confidence | Basis |
|---|---|---|
| 29 pelican class typeinfos | **CONFIRMED** | length-validated name strings + 29 typeinfo objects in `.data.rel.ro`; identical in both libs |
| 39 `Expr` vtable slots | **CONFIRMED** | `AffineExpr` vtable @`0x90abe0` — 39 `.text` slots terminated by the next class's `_ZTV` header |
| `SimpleExpr`/`CompoundExpr` split under `Expr` | **CONFIRMED** | both base pointers (`0x8fd150`, `0x90aa78`) resolve to `Expr` TI `0x90a908` |
| `nanobind::intrusive_base` base | **CONFIRMED** | `RefCountedObject` base ptr resolves to `N8nanobind14intrusive_baseE` |
| Index/CC/div-mod subtree membership | **CONFIRMED** | every parent edge from a RELA base reloc (table in §6) |
| Slot *names* (slots 0–38) | **STRONG** | from `libwalrus` `pelican` method symbols; slot 14 (`getIndicesSet`) confirmed in-binary; other slot↔name pairings rely on walrus symbolisation, not re-derived here |
| `addRef`/`release` are inlined | **INFERRED** | absence of out-of-line symbols + inline refcount arithmetic at `RefPtr` sites |
| Per-class slot override sets | **STRONG** | `ModuloExpr`/`AffineExpr` vtables decoded; not every leaf's vtable was slot-diffed |

**Re-verify ceiling.** The hard facts — the 29-node tree and its edges, the 39-slot count, the `FoldingIdx __vmi` layout, the refcount spine — are byte-exact and re-derivable from this build with `readelf -rW` and the §6 resolver. The **slot semantics** (what slot 18 *computes*) and the **per-leaf override maps** are STRONG, not CONFIRMED: they lean on `libwalrus`'s retained method names and on the cross-references below, and only `AffineExpr`/`ModuloExpr`/`FloorDivExpr` vtables were decoded slot-by-slot. The body of any individual slot (e.g. `ModuloExpr::eval` = Euclidean modulo) is documented in the struct/eval pages, not re-traced here.

---

## Cross-References

- [7.17 — pelican::Expr Member Offsets](pelican-expr-core.md) — the per-class field layouts that sit behind these typeinfos (`Expr` base 24 B, the div/mod numer/denom fields, the AP-index tile fields).
- [7.18 — pelican Index & Collective Leaves](pelican-index-runtime.md) — the `AffineIdx`/`OpaqueFnExpr`/`CC*` leaves in depth, by `ExprKind`.
- [5.4 — Penguin AffineExpr Algebra over pelican::Expr](../penguin/affine-expr-algebra.md) — Penguin's affine layer is built *over* this hierarchy; the bridge ([affine-isl-pelican-bridge](../penguin/affine-isl-pelican-bridge.md)) maps it to ISL.
- [7.15 — v1↔v2 Versioning: the pelican-Expr Encoding Gate](wire-versioning.md) — the wire-version gate is a `KIND`-switching *free function* (`toJsonv1/v2`), not a vtable slot; this page is the class side of that.
