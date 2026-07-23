# `pelican::Expr` Core: Affine / Binary Nodes & Member Byte-Offsets

> *All addresses and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`, sha256 `2387d0d4…dc5a4cfa`, 22151 functions). The cp311/cp312 builds carry the identical ABI but their virtual addresses drift; member offsets, object sizes, and kind tags are version-stable.*

## Abstract

`pelican::Expr` is the polymorphic root of Penguin's quasi-affine expression algebra — every tensor index, address term, stride, division, and predicate atom is an `Expr` subtree. The **shape** of that algebra (`c + Σ cᵢ·idxᵢ` with floordiv/mod locals, plus the collective-cyclic family) is documented from the Penguin side in [Penguin AffineExpr Algebra](../penguin/affine-expr-algebra.md), and the LLVM-RTTI'd class tree — 29 typeinfo records, 39 vtable slots — is the remit of [the pelican::Expr hierarchy page](./pelican-hierarchy.md). **This** page is the third leg: the byte-level **struct layout** of the affine/binary core nodes — the universal object header that every node shares, the `BinaryExpr` two-field payload, the `AffineExpr`/`SumExpr` compound bodies, and the integer **kind tags** that discriminate them.

The central reverse-engineering finding is that the whole hierarchy is built on **two shared constructors and one fixed header**. `pelican::Expr` is a `RefCountedObject`; its base constructor `sub_5FE310` lays down a four-field header — `vptr@0x00`, `refcount@0x08`, `kind@0x10`, `ctx@0x18` — identical for *every* node. Binary nodes (`MultExpr`, `FloorDivExpr`, `ModuloExpr`, `CCDivExpr`, `CCModExpr`) chain through a second base constructor `sub_60F4B0` that appends `operand@0x20` and `intval@0x28`. Compound nodes (`AffineExpr`, `SumExpr`) skip the binary base and grow a variable-length operand body at `+0x20` — but the two use *different* containers: `AffineExpr` holds a `boost::container::vector<pair<RefPtr<AffineIdx>, long>>` (heap begin/end/cap triple, no inline buffer), while `SumExpr` holds an `llvm::SmallVector<RefPtr<Expr>>` with 8 inline slots (see the per-node sections below). No node moves a base field; subclasses only append.

> **GOTCHA — the two compound nodes use different containers.** Only `SumExpr` (kind 18) is backed by an `llvm::SmallVector`; `AffineExpr` (kind 17) is a `boost::container::vector`, so do not model its terms as a `SmallVector<pair<RefPtr<AffineIdx>, long>, 16>`. The tell is in the factories: `sub_62BCF0` @ `0x62bcf0` zeroes a begin/end/cap triple (`[e+0x20]=0; movups [e+0x28],xmm0`) with no self-pointer to an inline buffer and no capacity literal — the boost empty-init — whereas `SumExpr`'s `sub_62C8D0` writes the SmallVector `begin = e+0x30; cap = 8` pattern. The populate-from-terms factory `sub_635860` @ `0x635860` settles it with verbatim boost assert strings naming both the element type `std::pair<pelican::RefPtr<pelican::AffineIdx>, long int>` and the source path `boost/container/vector.hpp`, and it frees the backing store unconditionally, with no `begin != inline-buffer` guard.

A reimplementer who reproduces the header, the two payload shapes, and the kind-tag literals can rebuild any core node.

Every offset below is anchored to a real `mov [reg+N]` store in a constructor (write side) and, where possible, cross-checked against an accessor or dispatch read (`cmp dword [reg+10h], …`). There is no `pelican::*` struct in IDA's type database — the binary carries only ELF/libc types — so this table *is* the definition.

### Reimplementation contract

To rebuild a core node a reimplementer must reproduce:

- The **universal 4-field header** `{vptr@0, refcount@8, kind@0x10 (int32), ctx@0x18}` written by the `Expr` base ctor, with `refcount` initialised to `1` and the vptr set to `Expr-vtable + 0x10` (later overwritten by the derived vtable).
- The **`BinaryExpr` payload** `{operand@0x20, intval@0x28}` written by the binary base ctor; the second field doubles as a `RefPtr<Expr>` operand *or* a raw `long` (denominator / modulus), per node.
- The **kind tags** `17` (AffineExpr) · `18` (SumExpr) · `23` (MultExpr) · `25` (FloorDivExpr) · `26` (ModuloExpr) · `27` (CCDivExpr) — plus `28` (CCModExpr), `2` (CCGetRankExpr), `3` (IndirectArgExpr) — read as a 4-byte field at `+0x10` by every dispatcher.
- The **object sizes**: `0x30` for the plain binary nodes, `0x38` for the CC binary nodes (one extra word), `0x40` for `AffineExpr`, `0x70` for `SumExpr`.
- The **`denom > 0` invariant** the div-like factories enforce before publishing the node.

| | |
|---|---|
| **Defining binary** | `libBIR.so` (cp310), 22151 functions |
| **Expr base ctor** | `sub_5FE310` @ `0x5fe310` (30 bytes) — sets header |
| **BinaryExpr base ctor** | `sub_60F4B0` @ `0x60f4b0` (82 bytes) — sets operand/intval |
| **Expr vtable** | `_ZTVN7pelican4ExprE` @ `0x90a920`; vptr = symbol `+0x10` = `0x90a930` |
| **BinaryExpr vtable** | `_ZTVN7pelican10BinaryExprE` @ `0x90be00`; vptr = `0x90be10` |
| **Kind tags (core)** | 17 AffineExpr · 18 SumExpr · 23 MultExpr · 25 FloorDivExpr · 26 ModuloExpr · 27 CCDivExpr |
| **Source path (asserts)** | `neuronxcc/pelican/include/pelican/IR/AffineExpr.h` |

> **NOTE —** the base-class chain (which node derives from `BinaryExpr` vs `CompoundExpr` vs `Expr`) is owned by [the hierarchy page](./pelican-hierarchy.md). Here it is *inferred from construction evidence only*: a node that chains through `sub_60F4B0` is a `BinaryExpr`; a node built directly on `sub_5FE310` with a vector body is a `CompoundExpr`. That inference agrees with the typeinfo tree but is restated, not re-derived.

The page proceeds: the universal header ([§1](#1-the-universal-header)), the `BinaryExpr` payload and the binary nodes ([§2](#2-binaryexpr--the-binary-nodes)), the compound nodes `AffineExpr`/`SumExpr` ([§3](#3-compound-nodes-affineexpr--sumexpr)), the consolidated member-offset map ([§4](#4-the-consolidated-member-offset-map)), the kind-tag census ([§5](#5-the-kind-tag-census)), and the adversarial self-verification ([§6](#6-adversarial-self-verification)).

---

## 1. The universal header

### The base constructor

Every `pelican::Expr` node, regardless of subclass, begins life in `sub_5FE310` — the `pelican::Expr` (≡ `RefCountedObject`) base constructor. It is 30 bytes and stores exactly four fields:

```asm
; sub_5FE310 @ 0x5fe310  —  pelican::Expr::Expr(this, int kind, PelicanContext* ctx)
0x5fe310: lea  rax, _ZTVN7pelican4ExprE     ; Expr vtable @ 0x90a920
0x5fe317: mov  qword [rdi+8], 1             ; refcount@0x08 = 1
0x5fe31f: mov  [rdi+10h], esi               ; kind@0x10 = <int kind>   (32-bit!)
0x5fe322: add  rax, 10h                     ; vptr = vtable + 0x10  (past RTTI header)
0x5fe326: mov  [rdi+18h], rdx               ; ctx@0x18 = PelicanContext*
0x5fe32a: mov  [rdi], rax                   ; vptr@0x00
0x5fe32d: retn
```

Reconstructed as pseudocode:

```c
// sub_5FE310 @ 0x5fe310 — pelican::Expr::Expr
void Expr_ctor(Expr *this, int kind, PelicanContext *ctx) {
    this->refcount = 1;                 // [this+0x08]
    this->kind     = kind;              // [this+0x10]  (int32, NOT a full qword)
    this->ctx      = ctx;               // [this+0x18]
    this->vptr     = &Expr_vtable + 2;  // [this+0x00] = 0x90a930
}
```

### Header field map

| Offset | Width | Field | Evidence |
|---|---|---|---|
| `0x00` | 8 | `vptr` | `mov [rdi], rax` where `rax = _ZTVN7pelican4ExprE + 0x10` |
| `0x08` | 8 | `refcount` | `mov qword [rdi+8], 1` — initialised to 1 |
| `0x10` | 4 | `kind` (`ExprKind` int) | `mov [rdi+10h], esi` — 32-bit store from `esi` |
| `0x14` | 4 | *(padding / unused)* | no store; `0x18` is 8-aligned |
| `0x18` | 8 | `ctx` (`PelicanContext*`) | `mov [rdi+18h], rdx` |

> **GOTCHA —** `kind` is a **32-bit** field (`mov [rdi+10h], esi`, and every reader uses `cmp dword [reg+10h], …`). The high 4 bytes at `+0x14` are never written by the base ctor and act as padding before the 8-aligned `ctx`. Treat the discriminator as `int32`, not `int64`.

> **QUIRK —** the vptr stored by the base ctor is the **`Expr` base vtable** (`0x90a930`), not the concrete node's. Every concrete factory *overwrites* `[obj+0]` with the derived vtable immediately after the base ctor returns (e.g. `MultExpr` writes `off_90B4E8`). A node briefly carries the base vtable mid-construction — relevant only if a constructor throws (the unwind path resets the vptr to the base before `operator delete`).

The `refcount@0x08` confirms `pelican::Expr : pelican::RefCountedObject`: the typeinfo `_ZTIN7pelican16RefCountedObjectE` exists at `0x90a8f0`, the increment/decrement helpers are `sub_638F80` (retain) and `sub_638FB0` (release, returns "was-last"), and every factory's tail calls `sub_638F80(obj + 8)` — i.e. it bumps the refcount field **at `+0x08`**, the second proof of the offset.

---

## 2. `BinaryExpr` & the binary nodes

### The binary base constructor

`MultExpr`, `FloorDivExpr`, `ModuloExpr`, `CCDivExpr`, and `CCModExpr` are `BinaryExpr` nodes. They chain through `sub_60F4B0`, which calls the `Expr` base ctor and then appends two fields:

```asm
; sub_60F4B0 @ 0x60f4b0  —  pelican::BinaryExpr::BinaryExpr(this, int kind, ctx, RefPtr lhs, long rhs)
0x60f4bd: call sub_5FE310                    ; header: vptr/refcount/kind/ctx
0x60f4c2: lea  rax, _ZTVN7pelican10BinaryExprE   ; BinaryExpr vtable @ 0x90be00
0x60f4c9: mov  [rbx+20h], rbp                ; operand@0x20 = lhs (4th arg, rcx)
0x60f4cd: add  rax, 10h
0x60f4d1: mov  [rbx], rax                    ; vptr := BinaryExpr vtable + 0x10
        ; … if (lhs != 0) retain(lhs+8) …
0x60f4df: mov  [rbx+28h], r12               ; intval@0x28 = rhs (5th arg, r8)
```

```c
// sub_60F4B0 @ 0x60f4b0 — pelican::BinaryExpr::BinaryExpr
void BinaryExpr_ctor(BinaryExpr *this, int kind, PelicanContext *ctx,
                     RefPtr<Expr> lhs, long rhs) {
    Expr_ctor(this, kind, ctx);          // header @ 0x00..0x18
    this->operand = lhs;                 // [this+0x20]
    this->vptr    = &BinaryExpr_vtable + 2;
    if (lhs) retain(&lhs->refcount);     // sub_638F80(lhs + 8)
    this->intval  = rhs;                 // [this+0x28]
}
```

> **NOTE —** the field at `+0x28` is named `intval` here but is **polymorphic in role**: for `MultExpr` it is the integer *coefficient*; for `FloorDivExpr`/`ModuloExpr` it is the *denominator/modulus* (a raw `long`); for `CCDivExpr`/`CCModExpr` it is the group-size denominator. The first operand `+0x20` is always a `RefPtr<Expr>` (retained). This single `{RefPtr, long}` pair is the entire `BinaryExpr` payload — there is no second `RefPtr` slot; binary nodes are *expr ⊕ constant*, not *expr ⊕ expr*.

### The five binary factories

Each concrete binary node has a factory that `operator new`s the object, calls `sub_60F4B0` with its kind tag, then publishes the derived vtable. Kind literals and `operator new` sizes are read straight off each factory (`mov edi, <size>` before `__Znwm`, `mov esi, <kind>` before the base-ctor call):

| Factory | `new` size | kind tag | Final vtable | Node |
|---|---|---|---|---|
| `sub_62BAA0` @ `0x62baa0` | `0x30` | `0x17` = **23** | `off_90B4E8` (`MultExpr` vt `0x90b4d8`) | `MultExpr` |
| `sub_62B7A0` @ `0x62b7a0` | `0x30` | `0x19` = **25** | `off_90B8D0` (`FloorDivExpr` vt `0x90b8c0`) | `FloorDivExpr` |
| `sub_62B830` @ `0x62b830` | `0x30` | `0x1A` = **26** | `off_90BA20` (`ModuloExpr` vt `0x90ba10`) | `ModuloExpr` |
| `sub_62B8C0` @ `0x62b8c0` | `0x38` | `0x1B` = **27** | `off_90BB70` (`CCDivExpr` vt `0x90bb60`) | `CCDivExpr` |
| `sub_62B960` @ `0x62b960` | `0x38` | `0x1C` = **28** | `off_90BCC0` (`CCModExpr` vt `0x90bcb0`) | `CCModExpr` |

```c
// sub_62BAA0 @ 0x62baa0 — pelican::MultExpr factory  (kind 23)
MultExpr *make_MultExpr(PelicanContext *ctx, RefPtr<Expr> sub, long coef) {
    MultExpr *e = operator new(0x30);
    BinaryExpr_ctor(e, 23, ctx, sub, coef);   // sub_60F4B0
    e->vptr = &MultExpr_vtable + 2;           // off_90B4E8
    if (e) retain(&e->refcount);              // sub_638F80(e + 8)
    return e;
}
```

> **GOTCHA —** the div-like factories (`FloorDivExpr`, `ModuloExpr`, `CCDivExpr`, `CCModExpr`) transiently store `unk_90B638` — the **`DivLikeExpr`** vtable (`0x90b628 + 0x10`) — *before* overwriting it with the concrete vtable. `DivLikeExpr` is the abstract intermediate base for the div family; its vtable appears in the byte stream but no object is ever published with it. All four div factories share the assert string `AffineExpr.h:711: denom > 0` and throw `"Unsupported denom for DivLikeExpr!"` if the denominator is `≤ 0` — the construction-time invariant on `intval@0x28`.

> **NOTE — `CCModExpr` factory anchor.** The `sub_62B960` row is byte-pinned in `libBIR.so` (cp310): `62b974: bf 38 00 00 00 mov $0x38,%edi` (size `0x38`) → `62b982: call _Znwm@plt` (`operator new`) → `62b990: be 1c 00 00 00 mov $0x1c,%esi` (kind `0x1C` = 28) → `62b9b8: lea 0x2e02f1(%rip),%rax # 90bcb0` then `62b9cb: mov %rax,0x0(%rbp)` (publishes vtable `0x90bcb0`) → `62b9bf: mov %r13,0x30(%rbp)` (`replica_groups_id@+0x30`). This is a **distinct** factory from `sub_62B830` (`ModuloExpr`, `0x30` bytes / kind `0x1A`=26), so kind-28 is not shared with `ModuloExpr`. The same node has a symboled analogue in the `libwalrus` frame — `createCCModExpr` (the libwalrus VA for the same `0x38`/kind-28/`0x90bcb0`-class constructor) — so the two frames cross-confirm the kind-tag.

> **GOTCHA —** `Sum`/`Mult`/`Modulo`/`FloorDiv` are often sketched as direct children of `AffineExpr`. In the binary, `MultExpr`/`FloorDivExpr`/`ModuloExpr`/`CCDivExpr`/`CCModExpr` chain through the **`BinaryExpr`** base ctor (`sub_60F4B0`), not the `AffineExpr` body: the compound nodes are the affine-combination `AffineExpr` (kind 17) and the operand-list `SumExpr` (kind 18), while the multiplicative and division nodes are binary. `MultExpr`'s typeinfo sits at `0x90b448`, distinct from `AffineExpr` at `0x90aa80` — see [the hierarchy page](./pelican-hierarchy.md).

### The CC binary nodes' extra word

`CCDivExpr` (27) and `CCModExpr` (28) are `0x38`-byte objects — one word longer than the plain binary nodes — because they carry a `replica_groups_id` at `+0x30`: both factories write `v10[6] = a5` (`[obj+0x30]`) after the base ctor.

| Offset | Field (CC binary) | Evidence |
|---|---|---|
| `0x00`–`0x18` | universal header | `sub_5FE310` |
| `0x20` | `operand` (`RefPtr<Expr>`, the rank/index sub-expr) | `sub_60F4B0` |
| `0x28` | `intval` (group size / denominator, `> 0`) | `sub_60F4B0` |
| `0x30` | `replica_groups_id` (`long`) | `mov [obj+0x30], a5` in factory |

---

## 3. Compound nodes: `AffineExpr` & `SumExpr`

The two compound core nodes skip the `BinaryExpr` base and build a variable-length operand body directly on the `Expr` header.

### `AffineExpr` (kind 17) — object size `0x40`

`AffineExpr` is the canonical affine combination `c + Σ cᵢ·idxᵢ`. Its body is a `boost::container::vector<pair<RefPtr<AffineIdx>, long>>` (the term list) plus a constant, as laid down by `sub_62BCF0` @ `0x62bcf0` (the empty-with-const factory) and `sub_635860` @ `0x635860` (the populate-from-terms factory):

```c
// sub_62BCF0 @ 0x62bcf0 — pelican::AffineExpr factory (empty body, kind 17)
AffineExpr *make_AffineExpr(PelicanContext *ctx, long c) {
    AffineExpr *e = operator new(0x40);
    Expr_ctor(e, 17, ctx);               // sub_5FE310 — header
    *(qword*)(e + 0x20) = 0;             // terms.begin  = null
    *(qword*)(e + 0x38) = c;             // const        = c
    e->vptr = &AffineExpr_vtable + 2;    // off_90ABF0  (0x90abe0 + 0x10)
    *(oword*)(e + 0x28) = 0;             // terms.end / terms.cap = 0
    if (e) retain(&e->refcount);
    return e;
}
```

| Offset | Width | Field (`AffineExpr`) | Evidence |
|---|---|---|---|
| `0x00`–`0x18` | — | universal header | `sub_5FE310` |
| `0x20` | 8 | `terms.begin` (vector data ptr) | `[e+0x20] = 0` then filled by `sub_62B490` |
| `0x28` | 8 | `terms.end` / size | `oword [e+0x28] = 0`; reader at `+0x28` in `sub_635860` |
| `0x30` | 8 | `terms.capacity` | second half of the `oword` zeroing |
| `0x38` | 8 | `const` (`long`) | `[e+0x38] = c` (`sub_62BCF0`) / `= a4` (`sub_635860`) |

> **NOTE —** the term list is a `boost::container::vector` of `pair<RefPtr<AffineIdx>, long>` (16-byte elements: the index ref + its integer coefficient). `sub_635860`'s asserts name the element type verbatim — `std::pair<pelican::RefPtr<pelican::AffineIdx>, long int>` — and the boost source path `boost/container/vector.hpp`. The factory copies an input term vector, then `erase`s zero-coefficient terms in place before publishing.

### `SumExpr` (kind 18) — object size `0x70`

`SumExpr` is the operand-list compound — a sum of arbitrary sub-`Expr`s — backed by an `llvm::SmallVector<RefPtr<Expr>>` with **8 inline slots**, laid down by `sub_62C8D0` @ `0x62c8d0`:

```c
// sub_62C8D0 @ 0x62c8d0 — pelican::SumExpr factory (kind 18)
SumExpr *make_SumExpr(PelicanContext *ctx, SmallVector<RefPtr<Expr>> *src) {
    SumExpr *e = operator new(0x70);
    Expr_ctor(e, 18, ctx);               // sub_5FE310 — header
    *(qword*)(e + 0x20) = e + 0x30;      // ops.begin = inline buffer
    e->vptr = &SumExpr_vtable + 2;       // off_90B788  (0x90b778 + 0x10)
    *(qword*)(e + 0x28) = 0x800000000;   // size=0 (lo32), capacity=8 (hi32)
    // … copy src elements into the SmallVector, retaining each …
}
```

| Offset | Width | Field (`SumExpr`) | Evidence |
|---|---|---|---|
| `0x00`–`0x18` | — | universal header | `sub_5FE310` |
| `0x20` | 8 | `ops.BeginX` (SmallVector data ptr) | `[e+0x20] = e+0x30` (points at inline buffer) |
| `0x28` | 4 | `ops.Size` | low dword of `0x800000000` → `0` |
| `0x2C` | 4 | `ops.Capacity` | high dword → `8` |
| `0x30`–`0x6F` | 64 | inline element buffer (8 × `RefPtr<Expr>`) | `ops.begin` aimed here; `operator delete(e, 0x70)` in dtor |

> **QUIRK —** `SumExpr`'s SmallVector is **inline-first**: `ops.begin@0x20` initially points *into the object itself* (`e + 0x30`). The destructor (`sub_616FB0`/`sub_6169A0`) only `free()`s the buffer when `ops.begin != e + 0x30` — i.e. when the sum spilled past 8 terms and heap-promoted. The `0x70` object size is exactly header(`0x20`) + SmallVector control(`0x10`) + 8 inline `RefPtr` slots(`0x40`).

---

## 4. The consolidated member-offset map

Every cell is a field written by a constructor at the stated offset; blank = field does not exist in that node. Header rows (`0x00`–`0x18`) are universal — written by `sub_5FE310` for **all** nodes.

| Offset | `Expr` base | `BinaryExpr` (Mult/Floor/Modulo) | CC binary (CCDiv/CCMod) | `AffineExpr` (k17) | `SumExpr` (k18) |
|---|---|---|---|---|---|
| `0x00` | `vptr` | `vptr` | `vptr` | `vptr` | `vptr` |
| `0x08` | `refcount` (=1) | `refcount` | `refcount` | `refcount` | `refcount` |
| `0x10` | `kind` (int32) | `kind` | `kind` | `kind` | `kind` |
| `0x14` | *(pad)* | *(pad)* | *(pad)* | *(pad)* | *(pad)* |
| `0x18` | `ctx` | `ctx` | `ctx` | `ctx` | `ctx` |
| `0x20` | — | `operand` (`RefPtr<Expr>`) | `operand` | `terms.begin` | `ops.begin` |
| `0x28` | — | `intval` (`long`) | `intval` (group size) | `terms.end`/size | `ops.size`(lo32) `cap`(hi32) |
| `0x30` | — | — | `replica_groups_id` | `terms.capacity` | inline buf[0] |
| `0x38` | — | — | — | `const` (`long`) | inline buf[1] |
| `0x40`+ | — | — | — | *(end, `0x40`)* | inline buf[2…7] |
| size | — | `0x30` | `0x38` | `0x40` | `0x70` |

> The two-VA-frame artifact applies: the `nm`-style symbol VAs above (e.g. `0x5fe310`) are from the per-binary frame; internal per-symbol sidecar offsets in IDA's split exports may use a second frame. All addresses on this page are the binary-VA frame, cross-checked against the disasm sidecars whose filenames embed the same VA.

---

## 5. The kind-tag census

The `ExprKind` discriminator at `+0x10` is a dense small-integer enum. The core affine/binary band is `17`–`28`; the collective leaves `CCGetRankExpr`/`IndirectArgExpr` sit low at `2`/`3`. Each kind below is pinned to a `mov esi, <lit>` immediately preceding a base-ctor call in the named factory:

| Kind | Hex | Node | Pinned at | Base ctor |
|---|---|---|---|---|
| 2 | `0x02` | `CCGetRankExpr` | `CCGetRankExpr::create` @ `0x3bebf0` | `sub_5FE310` |
| 3 | `0x03` | `IndirectArgExpr` | `IndirectArgExpr::create` @ `0x3becf0` | `sub_5FE310` |
| **17** | `0x11` | `AffineExpr` | `sub_62BCF0`, `sub_635860` | `sub_5FE310` |
| **18** | `0x12` | `SumExpr` | `sub_62C8D0` | `sub_5FE310` |
| **23** | `0x17` | `MultExpr` | `sub_62BAA0` | `sub_60F4B0` |
| **25** | `0x19` | `FloorDivExpr` | `sub_62B7A0` | `sub_60F4B0` |
| **26** | `0x1A` | `ModuloExpr` | `sub_62B830` | `sub_60F4B0` |
| **27** | `0x1B` | `CCDivExpr` | `sub_62B8C0` | `sub_60F4B0` |
| 28 | `0x1C` | `CCModExpr` | `sub_62B960` | `sub_60F4B0` |

> **NOTE —** the six tags the title calls out — **17, 18, 23, 25, 26, 27** — are exactly `{AffineExpr, SumExpr, MultExpr, FloorDivExpr, ModuloExpr, CCDivExpr}`. Tag `28` (`CCModExpr`) completes the binary band; `24` is not observed among the affine/binary factories (a gap in the captured set — possibly `DivLikeExpr` as an abstract tag, never constructed). The low tags `2`/`3` belong to the collective/indirect leaves built directly on the simple base.

The **read side** independently confirms both the field offset and the tag values. `isV1` (`0x3b4110`) dispatches on the discriminator:

```c
// isV1 @ 0x3b4110 — version-1 affine-form predicate (reads kind@0x10)
v3 = *(int32*)( *(qword*)expr_ref + 0x10 );   // kind@0x10
if (v3 == 17) return true;                     // AffineExpr
if (v3 <= 0x11) return (v3 - 2 <= 1);          // CCGetRank(2)/IndirectArg(3) band
if (v3 - 25 > 2) return false;                 // outside FloorDiv/Modulo/CCDiv(25..27)
return *(int32*)( operand_at_0x20->kind ) == 17; // recurse into operand@0x20
```

This single function pins three claims at once: `kind` is a 4-byte field at `+0x10`; the div-like band is the contiguous range `25..27`; and the binary operand lives at `+0x20` (`v2[4]`). `bir::QuasiAffineExpr::isSimpleAffineExpr` (`0x3b5a20`) reduces to `*(int32*)(expr->kind) == 17`, and `QuasiAffineExprIterator::traverseChildren` (`0x3b07d0`) switches on `+0x10` against `0x11`/`0x12` — all reading the same field.

---

## 6. Evidence anchors and limits

Each layout claim is anchored on both a write site and an independent read site:

1. **Universal header `{vptr@0, refcount@8, kind@0x10, ctx@0x18}`.** Write side: `sub_5FE310` stores `[rdi+8]=1`, `[rdi+10h]=esi`, `[rdi+18h]=rdx`, `[rdi]=Expr_vtable+0x10`. Read side: `isV1`/`isSimpleAffineExpr`/`traverseChildren` all read `[expr+0x10]` as the kind, and every factory's tail `retain(obj+8)` re-derives `refcount@0x08`.

2. **`refcount@0x08` initialised to `1` (≡ `RefCountedObject`).** `mov qword [rdi+8], 1` in the base ctor; `_ZTIN7pelican16RefCountedObjectE` @ `0x90a8f0` exists; retain `sub_638F80` / release `sub_638FB0` both operate on `obj + 8`.

3. **`BinaryExpr` payload `{operand@0x20, intval@0x28}`.** `sub_60F4B0` writes `mov [rbx+20h], rbp` (lhs) and `mov [rbx+28h], r12` (rhs); on the read side `isV1` dereferences `v2[4]` (= `+0x20`) as the operand and recurses on *its* kind.

4. **Kind tags `17/18/23/25/26/27` (+28/2/3).** Each tag appears as the `mov esi, <imm>` immediately before the base-ctor call in the named factory (`0x11,0x12,0x17,0x19,0x1A,0x1B,0x1C`) and again as `cmp dword [reg+10h], <imm>` in the readers.

5. **Object sizes `0x30 / 0x38 / 0x40 / 0x70`.** Each `mov edi, <size>` precedes the `operator new` call in its factory (`0x30` Mult/Floor/Modulo, `0x38` CCDiv/CCMod, `0x40` AffineExpr, `0x70` SumExpr), and `SumExpr`'s dtor `operator delete(e, 0x70)` cross-checks.

**Limits.**

- The **base-class chain** (BinaryExpr ⊃ DivLikeExpr ⊃ {FloorDiv, CCDiv}, etc.) is reconstructed here from which base ctor each factory chains through and from the transient `DivLikeExpr` vtable, not from reading the `__si_class_type_info` base pointers — so it is **[INFERRED]**. The authoritative tree is [the hierarchy page](./pelican-hierarchy.md).
- The **field *semantics*** of `intval@0x28` (coefficient vs denominator vs modulus per node) are **[INFERRED]** from the assert strings (`denom > 0`) and the node names; the binary stores one `long` and the role label is contextual.
- `AffineExpr`'s `terms` triple at `0x20/0x28/0x30` is identified as a `boost::container::vector` from `sub_635860`'s asserts. The begin/end/cap field assignment matches the boost layout and the zeroing pattern, but the precise end-vs-size encoding at `+0x28` was not disassembled field-by-field, so it is **[INFERRED]**.
- Kind **`24`** is **unobserved** — no captured factory emits it. Whether it is a reserved/abstract `DivLikeExpr` tag or a node outside this page's scope is **[SPECULATIVE]**.

---

## Cross-references

- [The `pelican::Expr` Hierarchy](./pelican-hierarchy.md) — the 29-typeinfo / 39-vtable-slot RTTI class tree (the *types*; this page is the *bytes*).
- [Penguin AffineExpr Algebra over `pelican::Expr`](../penguin/affine-expr-algebra.md) — the algebra and access-descriptor layer (the *meaning* of the nodes laid out here).
- [AffineExpr / AffinePredicate ⇄ ISL ⇄ pelican Bridge](../penguin/affine-isl-pelican-bridge.md) — how these nodes round-trip through ISL.
- The `pelican::Expr` JSON wire-form (`toJsonv1`/`toJsonv2`) and the `FoldingSet` uniquing are companion Part-7 pages (index/CC).
