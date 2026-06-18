# pelican::ModuloExpr — the Euclidean ring that rotates multi-buffer addresses

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical for the bodies cited). The `pelican::ModuloExpr` family lives in `neuronxcc/starfish/lib/libwalrus.so` (it is also duplicated in `libBIR.so` / `BIRSimulator`, see [7.16](pelican-hierarchy.md)). For `.text`/`.rodata` the virtual address equals the file offset; `.data` carries a fixed `+0x400000` delta and is called out where it matters. Treat every address as version-pinned.*

## Abstract

A software-pipelined loop in the Neuron backend reuses a small ring of physical buffers — `N` PSUM banks, `N` SBUF tiles, an `N`-slot DRAM window — and cycles successive loop iterations through them so that iteration *i* writes the buffer that iteration *i−2* just released. The symbolic object that names *which* buffer a given iteration uses is one expression node: **`pelican::ModuloExpr`**, the `(numer mod denom)` arm of the [pelican::Expr](pelican-hierarchy.md) algebra (kind **26** = `0x1a`, a `DivLikeExpr` subclass: `numer@+0x20`, `denom@+0x28`>0, node size `0x30`). This page recovers the *math* — the byte-exact evaluator, the static value range, and the algebraic simplification lemmas — and corrects the producer question: it proves from the binary's xref table that **`ModuloExpr` is not constructed by the AddressRotation pass at all**; its sole non-pelican producer is `neuronxcc::backend::Unroll::LowerDynamicExprs`.

Three facts carry the page, each read firsthand off `libwalrus.so`:

1. **`ModuloExpr::eval` is Euclidean modulo, not C `%`.** The body computes the truncated remainder with one `idiv`, then applies a sign correction so the result is *always* in `[0, denom)` even when the numerator is negative. This is the mathematician's floored modulo, and it is what makes a ring index well-defined.
2. **The static value range is exactly `[0, denom−1]`.** `getInfimumFast → 0` and `getSupremumFast → denom−1` are two-instruction constant functions; together they are the binary's own proof that a rotated address is confined to a window of *exactly* `denom` distinct slots — the ring of `denom` physical buffers. **`denom` = ring size = pipeline stage count `N`.**
3. **The sole backend producer is `Unroll::LowerDynamicExprs`.** Of the seven functions in the binary that call `createModuloExpr`, six are pelican-internal (simplify / clone / `Expr::modulo` / `moduloIdentity`); exactly one is outside pelican. The whole `AddressRotation` pass calls `createModuloExpr` / `Expr::modulo` / `Expr::floordiv` **zero** times — it rebinds *physical* addresses only.

For reimplementation the contract is: the Euclidean evaluator and its sign-correction branch; the `[0, denom)` range pair and what it guarantees; the smart constructor `Expr::modulo` that normalizes the numerator to an `AffineExpr`; the four simplify lemmas that keep stacked rotations canonical; and the producer/consumer ownership split — `Unroll` writes the symbolic mod index (preserving `denom`), `AddressRotation` chooses the physical slot the ring maps onto. The two are complementary, and prior recovery notes conflated them.

| | |
|---|---|
| **Evaluator** | `ModuloExpr::eval(DenseMap<AffineIdx*,long> const&)` @ `0x18d7dd0` — Euclidean mod ⭐ |
| **Evaluator (env,long)** | `ModuloExpr::eval(DenseMap, long)` @ `0x18d7e50` — numer vtable slot `+0xd8` |
| **Range floor** | `ModuloExpr::getInfimumFast` @ `0x18e0a60` → `0` |
| **Range ceil** | `ModuloExpr::getSupremumFast` @ `0x18e0a70` → `denom−1` ⭐ |
| **Smart constructor** | `Expr::modulo(long)` @ `0x18c8bb0` — normalizes numer → `AffineExpr` |
| **Factory** | `PelicanContext::createModuloExpr(Expr*, long)` @ `0x18f5d00` — `new(0x30)`, kind `0x1a`, `denom>0` assert |
| **Nested-mod collapse** | `ModuloExpr::simplifyModSubclass(long)` @ `0x18d81a0` |
| **Range-bound floordiv→0** | `ModuloExpr::simplifyFloordivSubclass(long)` @ `0x18d8040` |
| **Distributive mod** | `AffineExpr::simplifyModSubclass(long)` @ `0x18cf6e0` |
| **n-ary push-mod-through-sum** | `SumExpr::moduloIdentity` @ `0x18dbe80` |
| **Sole backend producer** | `Unroll::LowerDynamicExprs` @ `0xb38a50`; the `createModuloExpr` call @ `0xb39140` ⭐ |
| **Ring-id emitter** | `Unroll::createIndices(int N, …)` @ `0xb34d10` — emits `[0..N−1]` |
| **Leaf evaluator** | `AffineIdx::eval(DenseMap)` @ `0x18d5b90` — env lookup, bounds-checked |
| **vptr base** | `ZTV(pelican::ModuloExpr)` @ `0x3dc5728` (+0x10 = first vfn); `ZTV(DivLikeExpr)` @ `0x3dc5f90` |

---

## 1. The evaluator is Euclidean modulo — `ModuloExpr::eval` @ `0x18d7dd0`

The node carries two fields ([7.16](pelican-hierarchy.md) / [7.17](pelican-expr-core.md)): `numer` (a `RefPtr<Expr>`) at `+0x20`, and `denom` (an `int64`, invariant `> 0`) at `+0x28`. `eval(env)` takes a `DenseMap<AffineIdx*, long>` — the iteration-variable *environment*, mapping each induction-variable leaf to a concrete `long` — and returns a `{value, validByte}` pair. The body is 125 bytes; the arithmetic core is exact:

```asm
0x18d7dd8: mov  rdi, [rbx+0x20]      ; numer  (RefPtr<Expr>)
0x18d7ddc: mov  rax, [rdi]           ; numer vtable
0x18d7ddf: call [rax+0xd0]           ; numer->eval(env)  → {rax=value, rdx=validByte}
0x18d7dea: cmp  byte [rsp+..], 0     ; if numer not evaluable (valid==0):
0x18d7df4: jz   .invalid             ;     return {?, valid=0}
0x18d7df6: mov  rax, value
0x18d7dfb: mov  rcx, [rbx+0x28]      ; denom  (int64 > 0)
0x18d7e04: cqo                       ; sign-extend value into rdx:rax
0x18d7e06: idiv rcx                  ; rax = value/denom (trunc), rdx = value%denom (C rem)
0x18d7e09: mov  rax, rcx             ; rax = denom
0x18d7e0c: neg  rax                  ; rax = -denom        (SF set, since denom>0)
0x18d7e0f: cmovs rax, rcx            ; rax = denom         (SF ⇒ pick rcx = +denom)
0x18d7e13: add  rax, rdx             ; rax = denom + rem
0x18d7e16: test rdx, rdx            ; if rem >= 0 :
0x18d7e19: cmovns rax, rdx           ;     rax = rem
;                                    ; result = (rem >= 0) ? rem : rem + denom
```

The two `cmov` instructions are the entire story. After `idiv`, `rdx` holds the **C truncated remainder** — which for a negative numerator is negative (e.g. `-1 % 3 == -1`). The `neg/cmovs` pair materializes `+denom` unconditionally (because `denom>0`, `-denom` is negative, so `cmovs` always restores `rcx`), `add` forms `rem + denom`, and the final `cmovns` selects between the two: if `rem >= 0` keep `rem`, otherwise keep `rem + denom`. The result is always in `[0, denom)`.

### Algorithm — `ModuloExpr::eval`

```c
// pelican::ModuloExpr::eval @0x18d7dd0  (env overload; the (env,long) overload
// @0x18d7e50 is byte-identical except the numer dispatch is vtable slot +0xd8).
EvalResult ModuloExpr::eval(this, const DenseMap<AffineIdx*,long>& env) {
    EvalResult v = this->numer->eval(env);    // [+0x20]->vtbl[+0xd0] : recursive child eval
    if (!v.valid) return { 0, /*valid=*/false };

    long denom = this->denom;                  // [+0x28], invariant > 0
    long r     = v.value % denom;               // idiv: C truncated remainder, sign of v.value
    long fixed = (r >= 0) ? r : r + denom;      // neg/cmovs/add + test/cmovns : Euclidean fix
    return { fixed, /*valid=*/true };           // result ∈ [0, denom)
}
```

> **QUIRK — this is *not* C `%`.** C's `%` (and the bare `idiv` remainder this body starts from) follows the sign of the dividend: `(-1) % 3 == -1`. `ModuloExpr::eval` adds the `(r<0) ? r+denom` correction, so it computes the **floored / Euclidean modulo**: `eval(-1, denom=3) == 2`. The distinction is decisive for a *ring* index — only the Euclidean form maps the whole integer line onto `[0, denom)` with no gap or sign flip at zero, which is exactly what "buffer index" must do. CONFIRMED — the `cqo; idiv; neg; cmovs; add rdx; test rdx; cmovns` sequence read firsthand @ `0x18d7e04`–`0x18d7e19`.

> **NOTE — why the correction is dormant in practice.** In the rotation use-case the `numer` is a loop induction term (`base + step·iv`, non-negative for a forward-counting loop), so `r >= 0` always and the correction branch is a no-op. But the node is *defined* Euclidean, so the index stays correct for negative affine offsets — e.g. the `−stage` index shift the annotated software pipeliner applies ([5.14](../penguin/software-pipelining.md), `band_shift` / per-stage index offset). The compiler does not have to prove the numerator non-negative to trust the range. STRONG — the non-negativity of the rotation numer is inferred from the `AffineExpr(base+step·iv)` shape (§3); the Euclidean *guarantee* is CONFIRMED.

The recursion terminates at the leaf. `numer->eval` dispatches through vtable slot `+0xd0` (= `Expr::eval(env)`, [7.16](pelican-hierarchy.md) slot 26). The leaf is `AffineIdx::eval` @ `0x18d5b90`: a `DenseMap` probe keyed on the `AffineIdx*` pointer that, on a hit, returns the bound `long` (after asserting `lo <= val < ub` against the leaf's own `[+0x20]=lo` / `[+0x28]=ub` range), and on a miss returns `{0, valid=0}` — which propagates straight back up through every `ModuloExpr`/`AffineExpr` as `valid=0`. The end-to-end chain is:

```
AffineIdx  (iv leaf; env lookup, bounds-checked lo<=v<ub)     @0x18d5b90
   └─→ AffineExpr  (base + Σ coeff·iv ; kind 17)              [per-iteration address]
         └─→ ModuloExpr  (… mod denom ; kind 26)              [the ring reduction]

  phys_index(iter) = (base + step·iter)  mod  denom           (Euclidean, ∈ [0,denom))
```

CONFIRMED — eval body, leaf body, and the kind chain read firsthand; the same node is independently cross-corroborated as "slot-26 override → Euclidean modulo" in the recovered vtable map for `ModuloExpr` ([7.16](pelican-hierarchy.md)).

---

## 2. The range `[0, denom)` is the ring window — `getInfimumFast` / `getSupremumFast`

`pelican::Expr` exposes a static value-range interface (slots 18/19, [7.16](pelican-hierarchy.md)). `ModuloExpr` overrides both with constant-folded bodies:

```asm
; ModuloExpr::getInfimumFast @0x18e0a60
0x18e0a60: xor  eax, eax       ; → 0
0x18e0a62: ret

; ModuloExpr::getSupremumFast @0x18e0a70
0x18e0a70: mov  rax, [rdi+0x28] ; denom
0x18e0a74: sub  rax, 1          ; → denom - 1
0x18e0a78: ret
```

So a `ModuloExpr`'s static value range is exactly **`[0, denom−1]` = `[0, denom)`**, regardless of what its numerator's range is. This is the binary's own formal statement that the rotated index visits *precisely `denom` distinct slots and never escapes them*. The compiler consumes this pair downstream — in delinearization and bound-checking, the floor/ceil interface feeds the `isLegalDelinearizedAddress` virtual ([7.17](pelican-expr-core.md)) — to guarantee the rotated access stays inside its bank/tile window.

> **The ring proof.** A "ring of `N` buffers" is, mathematically, the residues `{0, 1, …, N−1}` under addition mod `N`. `getInfimumFast → 0` and `getSupremumFast → denom−1` pin the *image* of `ModuloExpr` to exactly that residue set with `N = denom`. Combined with the Euclidean evaluator (§1), which is a total surjection `ℤ → [0, denom)`, the node *is* the ring `ℤ/denom·ℤ`. CONFIRMED — both accessor bodies read firsthand; independently corroborated in the recovered hierarchy notes ("getInf=0, getSup=denom−1 give the `[0,denom)` ring-window proof").

---

## 3. The smart constructor normalizes the numerator — `Expr::modulo` @ `0x18c8bb0`

Builders never call the raw factory directly; they go through `Expr::modulo`, which simplifies and normalizes before constructing:

### Algorithm — `Expr::modulo` (the smart constructor)

```c
// pelican::Expr::modulo @0x18c8bb0
RefPtr<Expr> Expr::modulo(this /*numer*/, long denom) {
    assert(denom != 0);
    RefPtr<Expr> folded = Expr::simplifyMod(this, denom);   // @0x61a5d0 virtual dispatch (§4)
    if (folded) return folded;                              // node folded away entirely

    int k = this->kind;                                     // [+0x10]
    if ((unsigned)(k - 6) <= 7) {                           // k ∈ [6,13] = AffineIdx family
        // a BARE index (AffIV=6 .. ShardId=13, see 7.18) — wrap it in an affine first:
        this = createAffineExpr(idx, /*coeff=*/1, /*c=*/0); // @0x629340
    }
    return createModuloExpr(this, denom);                   // @0x18f5d00
}
```

> **INVARIANT — a `ModuloExpr`'s numer is always an `AffineExpr`.** The `(k-6) <= 7` test catches a raw index leaf and wraps it in an `AffineExpr(idx, coeff=1, c=0)` before the modulo node is built, so the numerator is *never* a bare index — it is always the per-iteration affine address `base + step·iv`. This is the concrete realization of "the numer is the per-iteration buffer-base index". CONFIRMED — the `(k-6)<=7` family test and the `createAffineExpr` fall-through are the documented numer-normalization path ([7.18](pelican-index-runtime.md) gives the `AffIV=6 … ShardId=13` family range). `ModuloExpr::clone` @ `0x18d80b0` and `cloneBinaryExpr` @ `0x18d8180` re-run this same path, so the invariant survives cloning.

The factory it lands on enforces the field invariants:

```asm
; PelicanContext::createModuloExpr @0x18f5d00
0x18f5d19: call _tc_new                       ; allocate 0x30 bytes
0x18f5d27: mov  esi, 0x1a                      ; kind = 26 (ModuloExpr)
0x18f5d32: call BinaryExpr::BinaryExpr(kind=0x1a, ctx, numer, denom)
0x18f5d46: test rbx, rbx                       ; denom
0x18f5d49: jle  .throw                         ; assert denom > 0  (else fatal)
0x18f5d6c: call inc_ref                        ; refcount the new node
;          vptr ← ZTV(ModuloExpr)+0x10
```

CONFIRMED — `new(0x30)`, `kind=0x1a`, and the `test rbx,rbx; jle →throw` (the `denom>0` invariant) read firsthand. This re-confirms the [7.16](pelican-hierarchy.md)/[7.17](pelican-expr-core.md) struct facts: kind 26, `numer@+0x20`, `denom@+0x28` (`>0`), size `0x30`, vptr = `ZTV(ModuloExpr)+0x10`.

---

## 4. The simplify algebra keeps stacked rotations canonical

Software pipelining can run its address rotation in two phases (an initial pass and a post-schedule re-run, [Part 8](../walrus/address-rotation.md)). Without canonicalization that would stack `(addr mod N1) mod N2` on one access pattern. Four virtual simplify bodies, all read firsthand, collapse such stacks to a single ring modulus:

**4a. Nested-modulo collapse — `ModuloExpr::simplifyModSubclass(n)` @ `0x18d81a0`.** Folds `(x mod m) mod n`:

```c
// reads m = this.denom [+0x28], outer modulus n = arg
long q_rem = m % n;                              // idiv r8  @0x18d81b1
if (q_rem == 0)        return Expr::modulo(this.numer, n);  //  n | m  ⇒  (x mod m) mod n == x mod n
else if (m >= 0 && n > m) return this;           // inc_ref  ;  n > m  ⇒  (x mod m) mod n == x mod m
else                   return nullptr;           // no simplification
```

CONFIRMED — the `idiv r8; test rdx,rdx; jz` (divisibility) and `cmp r8,rcx` (`n>m`) branches read firsthand; the `Expr::modulo` and `inc_ref` calls are present at `0x18d81df` / `0x18d81fd`. The decisive consequence: two stacked rotations fold to **one** `ModuloExpr`, so the final access pattern carries a single ring modulus.

**4b. Range-bound floordiv → 0 — `ModuloExpr::simplifyFloordivSubclass(d)` @ `0x18d8040`.** Simplifies `(x mod m) floordiv d`:

```c
if (m >= 0 && m < d) return createAffineInt(0);  // cmp rcx,rdx; jl  @0x18d804c → @0x18d8074
else                 return nullptr;
```

Because `x mod m ∈ [0, m) ⊂ [0, d)` when `m < d`, the floor-divide is identically `0`. CONFIRMED — `cmp rcx,rdx; jl → createAffineInt(0)` read firsthand. This is the *algebraic twin* of the §2 range: when a rotated ring index is the low part of a delinearized address, the high part `floordiv`s to zero — the static guarantee that the ring stays inside its window dimension.

**4c. Distributive modulo over an affine form — `AffineExpr::simplifyModSubclass(n)` @ `0x18cf6e0`.** Pushes `mod n` through `(c + Σ coeff_i·idx_i)`: reduces `c mod n` and each `coeff_i mod n`, **drops** terms whose `coeff_i ≡ 0 (mod n)` (they contribute a multiple of the modulus), and rebuilds the reduced affine form. This is `(c + Σ coeff·idx) mod n = (c mod n + Σ (coeff mod n)·idx) mod n`, the canonicalization that lets `base + (iv·stride) mod (N·bufSize)` reduce when the stride and buffer size are commensurate with the ring. CONFIRMED — body present; the `createModuloExpr` call site is among the 7 callers (§5).

**4d. n-ary push-mod-through-sum — `SumExpr::moduloIdentity` @ `0x18dbe80`.** The un-normalized sibling of 4c: distributes a modulo across a `SumExpr`'s operand list, emitting one `ModuloExpr` per operand (four `createModuloExpr` call sites inside it). CONFIRMED — present; four of the §5 caller hits originate here.

---

## 5. Who materializes the node — `Unroll::LowerDynamicExprs` is the sole backend producer

This is the page's correction. The `createModuloExpr` factory has a PLT thunk at `0x5f5a70` (→ `0x18f5d00`); sweeping the binary's xref table for every function that calls it yields **seven** unique callers:

| Caller | Namespace | Role |
|---|---|---|
| `AffineExpr::simplifyModSubclass` | `pelican::` | distributive simplify (§4c) |
| `ModuloExpr::clone` | `pelican::` | node clone |
| `Expr::modulo` | `pelican::` | smart constructor (§3) |
| `SumExpr::moduloIdentity` | `pelican::` | n-ary push-mod (§4d) |
| `SumExpr::simplifyModSubclass` | `pelican::` | simplify |
| `MultExpr::simplifyModSubclass` | `pelican::` | simplify |
| **`neuronxcc::backend::Unroll::LowerDynamicExprs`** | **`neuronxcc::backend::`** | **the only non-pelican caller** |

Six are pelican-internal plumbing; **exactly one** is a backend pass. And the whole `AddressRotation` pass — `psum_rotation` @ `0x938050`, `sb_rotation_batch` @ `0x92a1a0`, `dram_rotation` @ `0x93cae0`, the lot — appears in the caller list of `createModuloExpr` / `Expr::modulo` / `Expr::floordiv` **zero** times.

> **CORRECTION — `AddressRotation` does *not* emit the `ModuloExpr`.** Earlier recovery notes attributed "materialises / commits / inserts the pelican::ModuloExpr" to `address_rotation_psum_post_schedule`. Reading the xref table directly: `AddressRotation` makes **zero** `createModuloExpr` / `Expr::modulo` / `Expr::floordiv` calls. It performs **physical** rebinding only — `MemoryLocation::allocate()` onto a new bank/tile/region. The **symbolic** modulo index is produced by `Unroll::LowerDynamicExprs`. The two are complementary: `Unroll` writes the access pattern's mod expression; `AddressRotation` chooses the physical slot the ring maps onto. "Physical rotation" ≠ "ModuloExpr emission." CONFIRMED — the 7-caller set and the empty `AddressRotation` intersection both read firsthand from the binary's xref table; this falsifies the prior STRONG claim.

`Unroll::LowerDynamicExprs` @ `0xb38a50` is a 5039-byte recursive expression-tree lowerer. Its head reads the kind and dispatches a 25-case jump table; the `ModuloExpr` arm preserves `denom` verbatim:

```asm
; head — kind dispatch  @0xb38a82
0xb38a82: mov  eax, [rax+0x10]    ; expr->kind
0xb38a92: sub  eax, 3             ; switch 25 cases (kinds 3..27)
0xb38a9c: cmp  eax, 0x18
0xb38a9f: ja   .default

; ModuloExpr arm  @0xb39104
0xb39104: cmp  dword [rax+0x10], 0x1a   ; kind == 26 (ModuloExpr)?
0xb39108: mov  rsi, [rax+0x18]          ; ctx
0xb39126: call inc_ref
0xb39132: mov  rcx, [rbx+0x28]          ; denom  — READ AND PRESERVED UNCHANGED
0xb39136: mov  rdx, [rbp-0x150]         ; numer' = recursively-lowered numer
0xb39140: call createModuloExpr(ctx, numer', denom)
```

### Algorithm — the `ModuloExpr` arm of `LowerDynamicExprs`

```c
// neuronxcc::backend::Unroll::LowerDynamicExprs @0xb38a50, kind-26 arm @0xb39104
case ExprKind::Modulo: {                       // [+0x10] == 0x1a
    PelicanContext* ctx = expr->ctx;           // [+0x18]
    RefPtr<Expr> numer2 = LowerDynamicExprs(   // recurse: resolve dynamic/runtime sub-exprs
        expr->numer, inst, /*out lo*/, /*out hi*/);
    long denom = expr->denom;                  // [+0x28] — carried through UNCHANGED
    return ctx->createModuloExpr(numer2, denom);
}
```

> **NOTE — `Unroll` carries the ring size, it does not choose it.** The arm recursively lowers the *numerator* (resolving runtime/dynamic sub-expressions inside `base + step·iv`) while reading `denom` from `[+0x28]` and passing it straight to the factory. `Unroll` never computes or re-derives the modulus. The `denom` (= ring size = stage count `N`) is chosen *upstream* — the `ModuloExpr` enters walrus already present in the BIR access pattern, deserialized by `fromJsonv2` ([7.19](pelican-wire.md), `ModuloKind` → `createModuloExpr`, reading `"numer"` then `"denom"`), where it was emitted by the penguin software-pipeline code-gen ([5.14](../penguin/software-pipelining.md), which sizes the buffer factor from `num_stages`). STRONG — the JSON read path and the `Unroll` denom-preserving lowering are CONFIRMED; the upstream chooser that *picks* `N` lives in penguin (outside `libwalrus`) and is not byte-walked here.

---

## 6. `denom` = stage count `N` = number of physical buffers

Tying the math (§§1–2) to the physical ring. `Unroll` replicates a recurring tensor/register `N` times, and `createIndices(int N, SmallVector<u32,64>&)` @ `0xb34d10` emits exactly the index sequence `[0, 1, …, N−1]` — the `N` copy-ids of the ring:

```asm
; Unroll::createIndices @0xb34d10  — counter loop writing ebp = 0..N-1
0xb34d12: jle  .ret              ; N <= 0 ⇒ nothing
0xb34d3c: mov  [rdx+rax*4], ebp  ; slot[i] = current id
0xb34d48: add  rdx, 1
0xb34d51: add  eax, 1            ; ++id
0xb34d4c: cmp  rdx, rcx          ; until N slots filled
```

`createDuplicateTensor` @ `0xb3b550` / `createDuplicateRegister` @ `0xb37320` / `tensorUnroll` @ `0xb3c520` then replicate the buffer `N`-fold, keying each copy by an index in `[0, N)`. So `N` = unroll/duplication factor = stage count = the `ModuloExpr` `denom` for that tensor's per-iteration index. The relationship, per memory space:

| Space | Ring period (`= denom`) | Physical mechanism |
|---|---|---|
| **PSUM** | `N · bankSize`, where `N` = architectural PSUM bank count (`ArchModel`, [arch geometry](../arch/sbuf-psum-geometry.md)) | `psum_rotation` maps successive instances onto banks `start_bank + k` within a 32-partition band; the bank-window modulus is `(iv·width) mod (N·bankSize)` |
| **SBUF** | per-engine `period ∈ {1, 5, 10, 20, 60}` | `sb_rotation` keys `#SB tiles` in the ring; numer = `base + (iv mod period)·roundup8(slice)` |
| **DRAM** | window size ≤ `options.dram_rotation_size` GiB (CLI-capped, ≤ 8) | `dram_rotation`; the only ring whose period is a direct CLI knob (`"Allreduce rotation distance"` / `allreduce-rotation-dis`) |

> **The relationship.** `denom` (the `ModuloExpr` modulus) == ring size == number of physical buffers in that space's ring == the software-pipeline stage count `N`. The Euclidean reduction (§1) maps the unbounded loop induction term `iv` into the bounded ring index `[0, denom)` (§2); successive iterations therefore cycle deterministically through the `N` buffers: `buffer(iter) = (iv·stride) mod denom`. STRONG — the physical ring sizes per space are cross-corroborated with the AddressRotation pass facts ([Part 8](../walrus/address-rotation.md)); the precise `denom = N·bankSize` (PSUM) / `period` (SBUF) / `window` (DRAM) mapping is the synthesis of the §2 range with those physical periods, not a single byte-walked line.

---

## 7. How this realizes double / multi-buffering

The producer/consumer skew across `N` buffers is expressed entirely by the `ModuloExpr`:

```
consumer reads iteration (i−1):  addr = base + ((i−1)·stride) mod denom
producer writes iteration  i  :  addr = base + ( i   ·stride) mod denom
```

For `N = 2` (double buffering: `denom = 2·bufSize`, `stride = bufSize`) the two map alternately to the two banks/tiles — the load for iteration `i+1` writes the bank that compute for `i−1` just released; compute for `i` reads the bank load `i` filled last cycle. There is no WAR/WAW hazard because `(i mod N) != ((i−1) mod N)` for `N >= 2` — a property that is *only* true because the index is Euclidean and confined to `[0, denom)` (§§1–2).

The realized pipeline, with corrected ownership:

1. **(upstream, penguin)** The software-pipeline heuristic ([5.14](../penguin/software-pipelining.md)) picks stage count `N` and encodes the loop induction index as a `ModuloExpr(numer = base+step·iv, denom = ring(N))` in the BIR access pattern; emitted to `bir.json`, read back by `fromJsonv2` ([7.19](pelican-wire.md)).
2. **`Unroll::run`** → `tensorUnroll` / `createDuplicateTensor` replicate `N` buffers (`createIndices [0..N−1]`); **`Unroll::LowerDynamicExprs`** (§5) lowers each access pattern's `ModuloExpr`, **preserving `denom = N`**, while resolving dynamic numer sub-expressions.
3. The PSUM/SBUF/DRAM coloring allocators color one physical slot per logical buffer.
4. **`AddressRotation`** ([Part 8](../walrus/address-rotation.md)) **physically** rebinds the `N` duplicates onto distinct banks/tiles/regions so the `N` ring positions occupy `N` real addresses — but it does **not** create the `ModuloExpr` (§5).
5. `separate_load_and_compute` cuts load↔compute edges and fences them; the post-schedule list scheduler overlaps `load(i+1)` with `compute(i)`.
6. The post-schedule rotation re-run re-rotates the split-stage buffers and rebuilds dependency edges against the rotated *physical* addresses — the `ModuloExpr` index was already in place from step 2.

The `ModuloExpr` is the symbolic glue: its `denom = N` pins the ring, its `[0, denom)` range (§2) guarantees the rotation stays in-window, and its Euclidean eval (§1) defines the per-iteration buffer index. The physical passes give the `N` ring positions `N` distinct real addresses; together they are the multi-buffered software pipeline. STRONG — the end-to-end ordering is the synthesis of the CONFIRMED per-stage facts (eval, range, producer, AddressRotation's zero emissions) with the pass roster.

---

## 8. Adversarial self-check

The five headline claims, re-tested against the binary, with the strongest falsifier each survived:

| Claim | Verdict | Decisive evidence | Ceiling |
|---|---|---|---|
| Eval is **Euclidean**, not C `%` | CONFIRMED | `cqo; idiv; neg; cmovs; add rdx; test rdx; cmovns` @ `0x18d7e04`–`19` read firsthand — the `cmovns` *is* the `(r<0)?r+denom` correction | Full body read; no ambiguity |
| Range = **`[0, denom)`** ring | CONFIRMED | `getInfimumFast` = `xor eax,eax; ret` → 0; `getSupremumFast` = `[rdi+0x28]-1; ret` → denom−1 | Both bodies are 2–3 instructions; exhaustive |
| `denom` = **stage count `N`** | STRONG | `Unroll` preserves `denom` (`mov rcx,[rbx+0x28]` @ `0xb39132`); `createIndices` emits `[0..N−1]` @ `0xb34d10`; per-space periods cross-ref Part 8 | The `denom = N·bankSize/period/window` *equation* is synthesized, not one line; the chooser is in penguin, not byte-walked |
| Sole producer = **`Unroll::LowerDynamicExprs`** | CONFIRMED | 7 unique `createModuloExpr` callers; only one (`Unroll::LowerDynamicExprs`) is non-pelican; `AddressRotation` ∩ callers = ∅ | xref sweep over `0x5f5a70`/`0x5fe4d0`/`0x62af50`; complete for those three PLT targets |
| Kind tag = **26 (`0x1a`)** | CONFIRMED | `mov esi,0x1a` in `createModuloExpr` @ `0x18f5d27`; `cmp [rax+0x10],0x1a` in the `Unroll` arm @ `0xb39104` | Two independent sites agree |

**Honest re-verify ceiling.** Everything in §§1–5 is CONFIRMED by firsthand reads of `libwalrus.so` (cp310) bodies and the xref table. The two STRONG items are: (a) `denom = N` *as an equation across the three memory spaces* — the per-space periods (`N·bankSize` / `{1,5,10,20,60}` / GiB window) are corroborated against the AddressRotation pass but the `denom`↔period identity is a synthesis, not a single byte-walked store; and (b) the **upstream chooser** that picks `N` lives in penguin's `SoftwarePipelineCodeGen` (outside `libwalrus`) and is reached only through the CONFIRMED JSON read path, not walked at the chooser. cp311/cp312 parity for the eval/range/producer bodies is asserted from the cross-build note (eval `cqo/idiv/neg/cmovs` sequence and `[+0x28]-1` supremum byte-identical) and is STRONG, not re-disassembled here per-build on this page.

---

## Related Components

| Name | Relationship |
|---|---|
| `pelican::DivLikeExpr` | Abstract base (`numer`/`denom`, `denom>0`); `ModuloExpr`/`FloorDivExpr`/`CCDivExpr` derive from it |
| `pelican::AffineExpr` (kind 17) | The `ModuloExpr` numerator after normalization (`base + Σ coeff·iv`); `simplifyModSubclass` is the distributive lemma (§4c) |
| `pelican::AffineIdx` | The iv leaf the eval recursion terminates at; env-keyed, bounds-checked |
| `neuronxcc::backend::Unroll` | The sole backend producer; replicates `N` buffers and lowers the `ModuloExpr` preserving `denom` |
| `neuronxcc::backend::AddressRotation` | The *physical* rebinder; emits **no** `ModuloExpr` (the correction in §5) |

## Cross-References

- [pelican::Expr Class Hierarchy](pelican-hierarchy.md) — 7.16, the 29-typeinfo / 39-slot vtable map; the kind set (`ModuloExpr` = 26) and the slot-26 eval / slot-18-19 range interface this node overrides
- [pelican Expr Core — kinds & member offsets](pelican-expr-core.md) — 7.17, the `ModuloExpr` struct (kind 26, `numer@+0x20`/`denom@+0x28`, size `0x30`) and the `DivLikeExpr`/`isLegalDelinearizedAddress` machinery
- [pelican Index & Runtime Exprs](pelican-index-runtime.md) — 7.18, the `AffIV=6 … ShardId=13` index family that the smart constructor's `(k-6)<=7` test catches and wraps
- [pelican Expr Wire Format](pelican-wire.md) — 7.19, the `fromJsonv2` `ModuloKind` reader (`"numer"` then `"denom"`) that deserializes the node before `Unroll` lowers it
- [Penguin Software Pipelining](../penguin/software-pipelining.md) — 5.14, the annotation-driven (`num_stages`) pipeliner that *chooses* `N` and sizes the buffer factor upstream
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the per-generation PSUM bank count / SBUF partition budget that fixes the physical ring sizes
- [Walrus Address Rotation](../walrus/address-rotation.md) — Part 8, the *physical* bank/tile/region rebinder that gives the `N` ring positions `N` real addresses (and emits no `ModuloExpr`)
