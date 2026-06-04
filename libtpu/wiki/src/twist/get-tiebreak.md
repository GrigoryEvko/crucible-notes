# GetTiebreak

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); `.rodata` VMA equals file offset (base `0x84a0000`). All addresses are VMA. Every symbol, offset, and `.rodata` string below is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

`accel_ssw::deepsea::slice_builder::TwistedTorusTopology::GetTiebreak` (`0x20b41320`) is the **routing-side disambiguator**: given a vertex and a set of *equal-minimum-distance* candidate routes, it picks the single canonical route. The distance layer ([Get Distances](../routing/get-distances.md)) has already pruned to the shortest-hop set; when that set has more than one member, two min-distance paths *tie*, and `GetTiebreak` applies a deterministic, per-twist-shape rule to break the tie so the route table is reproducible across machines. It is the analogue, in *physical chip-coordinate* space, of the dateline choice the jellyfish [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md) makes in device-id space — but it is a *separate* mechanism on a separate class, and it consumes a candidate vector rather than computing a fold.

The tiebreak rule is governed by a single field, the **twist-shape discriminant** `this[+0xe8]` (set by `Init`, `0x20b3e5e0`): `1 = k*k*2k`, `2 = k*2k*2k`, `3 = k*2k*nk`. The **tiebreaking length is `K`** — the minimum dimension size, computed by a `std::min_element` over the topology's dim-sizes vector at function entry (`this[+0x10..+0x18]`). Every comparison the rule makes is against `±K`, and the literal `nK` of the `k*2k*nk` shape never reaches a `GetTiebreak` arm: `Init`'s `DropDimension` collapses it to a 3-D twist before tiebreak time, so the only literal-`n` distance math lives upstream in `GetDistance`. This is the "literal-nK ordering" the page title names — the ordering rule is *literally the `±nK` (= `±K`) coordinate test on the seam dimension*, and `K` is the only length it ever compares to.

`GetTiebreak` dispatches first on `this[+0xe8]`, then (for `k*2k*2k`) on the **candidate count**, which encodes the vertex's geometric class: 6 candidates ⇒ `k*k*2k` (any vertex); 4 ⇒ corner, 3 ⇒ mid, 2 ⇒ edge for `k*2k*2k`. The page documents the comparator, the three sub-rules (symmetric-route membership, parity-rotated corner, the `±K` edge test and the `max|coord| < K` mid select), the five error strings that pin each path, and how the rule disambiguates the `K_2K_2K` group assignment by selecting one canonical seam route per `(src, dst)`.

For reimplementation, the contract is:

- **The tiebreaking length is `K = std::min(dim_sizes)`.** Computed once at entry by a SIMD-unrolled `min_element` over the dim-sizes vector `this[+0x10..+0x18]`; it is the length every `±K` comparison and the `k*k*2k` `±tb` route are built from. There is no per-vertex distance recomputation inside `GetTiebreak` — it operates purely on the candidate vector and `K`.
- **The shape discriminant `this[+0xe8]` ∈ {1,2,3} selects the rule.** `1` and `2` are handled; `3` (`k*2k*nk`) is impossible here (reduced to a 3-D twist by `Init`), so it falls to the Invalid-vertex error like any non-twist topology.
- **The candidate count is the vertex class.** For `k*2k*2k` the count of equal-min routes (4/3/2 = corner/mid/edge) chooses the sub-rule; for `k*k*2k` it must be exactly 6 or the call errors.
- **Five distinct error strings, byte-exact, pin every path** to a source line; a reimplementation must reproduce both the success route and the exact failure taxonomy.

| | |
|---|---|
| **Entry point** | `TwistedTorusTopology::GetTiebreak` `0x20b41320` (`twisted_torus_topology.cc`) |
| **ABI** | `rdi`=`StatusOr` sret, `rsi`=`this`, `rdx`=`const Coordinates& vertex`, `rcx`=`const vector<Coordinates>& candidates` (mangled `…GetTiebreakERKN8superpod7routing11CoordinatesERKNSt3__u6vectorIS5_…E`) |
| **Returns** | `StatusOr<Coordinates>` = the chosen canonical route, or one of five errors |
| **Tiebreaking length** | `K = std::min_element(this[+0x10..+0x18])` at entry (`v117`) |
| **Shape discriminant** | `this[+0xe8]` ∈ {`1`=k\*k\*2k, `2`=k\*2k\*2k, `3`=k\*2k\*nk} — set by `Init` `0x20b3e5e0` |
| **Candidate count = vertex class** | `k*k*2k`: ==6 · `k*2k*2k`: 4=corner / 3=mid / 2=edge |
| **Coordinates** | `sizeof = 0x1c` (28 B); `[+0]`=ndims, `[+4+4·i]`=int32 coord; `GetCoordinate` `0x20c0b800`, `ManhattanNorm` `0x20c0ba00`, `operator==` `0x20c0bac0` |
| **Confidence** | HIGH (dispatch on `this[+0xe8]`, candidate-count gates, `std::min` K, all five error strings, the `±K`/parity-rotate comparators decompile-verified) unless a row/callout says otherwise |

---

## 1. Where GetTiebreak sits

A static route table for an ICI slice answers, per `(src_chip, dst_chip)` pair, *which sequence of physical links* a transfer takes. The generator first finds the minimum-hop distance ([`GetDistances`](../routing/get-distances.md) → `GetDistanceFromOrigin` `0x20b42980`), which yields a *set* of equal-minimum-distance candidate routes (a `vector<Coordinates>` of per-axis hop signatures). On a plain torus there is usually one shortest path; on a **twisted** torus the `+K`-mod-`2K` seam creates symmetric alternatives, so a vertex routinely has 2, 3, 4, or 6 equal-minimum routes. The route table must be deterministic, so a tie must be broken the same way every time and on every machine — that is `GetTiebreak`'s entire job.

```text
GetStaticPath / route-table generation                       (../routing/get-static-path.md)
  └─ GetDistances 0x20b420e0  → vector<Coordinates> candidates   (equal-minimum routes)
       └─ if |candidates| > 1:
            GetTiebreak 0x20b41320  → ONE canonical Coordinates   (this page)
                 ├─ this[+0xe8]==1 (k*k*2k):  build ±tb route, assert ∈ candidates
                 ├─ this[+0xe8]==2 (k*2k*2k): count-dispatch corner/mid/edge → ±K route
                 └─ else:                     Invalid-vertex error
```

`GetTiebreak` is `const` (it reads `this` fields but does not mutate the topology) and **does not regenerate distances** — it is handed the already-pruned candidate vector and only the scalar `K`. The discriminant `this[+0xe8]` and the dim-sizes vector `this[+0x10..+0x18]` were both populated by `Init` (`0x20b3e5e0`) at topology construction. The chosen route is the one the route-cache codec keys on and the [emission layer](../routing/get-static-path.md) programs into the per-link table.

> **NOTE —** this is the *routing* half of the twisted torus, not the *collective* half. The jellyfish `TwistedTorusND` ([2-Phase Replica-Group Construction](replica-group-2phase.md)) answers "who does each core reduce with"; `TwistedTorusTopology::GetTiebreak` answers "which physical links carry the bytes between two chips that already tied on distance." Both classes describe the same `+K` seam; conflating them yields a schedule that knows its partners but not its links. See the [section map](overview.md#2-the-twistedtorus-class-family).

---

## 2. The shape discriminant — `this[+0xe8]` (set in `Init`)

`TwistedTorusTopology::Init(absl::Span<const int> dims, bool)` (`0x20b3e5e0`) classifies a 3-D twisted geometry into one of three shapes and stores the result in `this[+0xe8]` (decompiled as `*((_DWORD*)this + 58)`; `58·4 = 232 = 0xe8`). The classifier counts how many axes equal the short size `K` (`numK`) and how many equal the doubled size `2K` (`num2K`):

```c
function TwistedTorusTopology::Init(this, dims, flag):      // 0x20b3e5e0
    ndims = this[+0x8]
    if ndims != 3: ... (no twist arm; GetTiebreak later falls to Invalid-vertex)
    K  = min over dims                            // the short axis size
    numK  = count(dim == K)                        // count loop
    num2K = count(dim == 2*K)                      // count loop
    if      numK  == 2:  this[+0xe8] = 1           // k*k*2k  — line 730
    else if num2K == 2:  this[+0xe8] = 2           // k*2k*2k — line 734
    else if num2K == 1:  this[+0xe8] = 3           // k*2k*nk — line 738
        nK_dim = wmemchr(dims, nK)                 // 0x211ac220 — index of the nK axis
        this[+0x130] = nK_dim ; this[+0x134] = 1   // line 750 ([+0x134]=1: nK recorded
        DropDimension(this, nK_dim)                //   0x20b3f4e0 — reduce to a 3-D twist)
    this[+0xe0] = route-cache-enabled bool
```

The three assignments are decompile-verified at lines 730 / 734 / 738; the `wmemchr` (`0x211ac220`) and the `this[+0x134]=1` store (line 750) confirm the `nK`-axis recording. The slice-shape support is pinned to the `.rodata` string `"TPU twisted torus only supports k*k*2k and k*2k*2k and k*2k*nk slice shapes."` (`0xa020458`).

| `this[+0xe8]` | shape | `numK` / `num2K` | reaches a `GetTiebreak` arm? |
|---|---|---|---|
| `1` | `k*k*2k` | 2 / 1 | yes — the 6-candidate symmetric-route path (§4) |
| `2` | `k*2k*2k` | 1 / 2 | yes — the count-dispatched corner/mid/edge path (§5) |
| `3` | `k*2k*nk` | 1 / 0 (one nK axis) | **no** — `DropDimension` reduced it; falls to Invalid-vertex (§6) |

> **GOTCHA —** the `k*2k*nk` (literal `n > 2`) shape **never reaches a `GetTiebreak` arm**. `Init` records the `nK`-axis index (`this[+0x130]`) and `DropDimension`-reduces the geometry to a 3-D twist *before* any tiebreak runs, so by the time `GetTiebreak` is called the discriminant is effectively `1` or `2` for the reduced shape. The literal-`n` distance handling — the only place the long axis is genuinely `n`-fold — lives in `GetDistance` / `GetDistanceFromOrigin`, **not** here. This is byte-confirmed by the *absence* of any `this[+0xe8]==3` arm in `GetTiebreak`: a discriminant of `3` would fall straight to the Invalid-vertex error. The "literal-nK ordering rule" is therefore the `±K` comparator on the reduced twist, not an `n`-dependent comparison.

---

## 3. The comparator: the tiebreaking length `K`

The first thing `GetTiebreak` does is compute the tiebreaking length. It is **`K`, the minimum dimension size**, obtained by a `std::min_element` over the topology's dim-sizes vector `this[+0x10]` (begin) … `this[+0x18]` (end). The decompile shows the standard libc++ `min_element` shape — a SIMD/unrolled-by-8 scan (the `v8 += 8` loop at `0x20b41380`, lines ~150-246) with a scalar tail — leaving the minimum in `v117` (used everywhere below as `K`):

```c
function GetTiebreak(this, vertex, candidates) -> StatusOr<Coordinates>:   // 0x20b41320
    K = min_element(this[+0x10] .. this[+0x18])      // v117 = the "tiebreaking length"
    shape = this[+0xe8]                               // v119[58]
    if shape == 1:  return TieBreak_kk2k(K, vertex, candidates)    // §4
    if shape == 2:  return TieBreak_k2k2k(K, vertex, candidates)   // §5
    return InvalidVertexError(vertex, this)           // §6
```

`K` is the canonical hop distance the twist routes travel on the short axis. It is the value the error strings print as the *"tiebreaking length"* and the *"expected distance"*: every comparator in §4 and §5 tests a candidate coordinate against `±K`.

> **QUIRK —** `GetTiebreak` recomputes `K` from the dim-sizes vector on every call rather than reading a cached `min_dim_` field — it does *not* reuse the `this[+0x5f8]` style scalar the collective half stores. The two halves are different classes (`slice_builder::TwistedTorusTopology` vs `jellyfish::TwistedTorusND`); the routing class keeps its sizes in the `[+0x10..+0x18]` vector and reduces it inline. A reimplementer must not assume a shared `K` field across the two halves.

| Field | Offset (`this`) | Decompile expr | Meaning |
|---|---|---|---|
| ndims | `+0x8` | `v119[2]` | dimension count (3 for a twist) |
| dim-sizes begin / end | `+0x10` / `+0x18` | `min_element` operands | the vector `std::min` reduces to `K` |
| shape discriminant | `+0xe8` | `v119[58]` | 1/2/3 (§2) |
| route-cache enabled | `+0xe0` | — | bool; tiebreak result is memoised when set |
| nK-axis index / valid | `+0x130` / `+0x134` | — | set for shape 3 by `Init` (§2) |

The `Coordinates` operand layout (cross-checked against `GetCoordinate` `0x20c0b800`, `ManhattanNorm` `0x20c0ba00`, `operator==` `0x20c0bac0`, ctor-from-`Span` `0x20c0b1e0`): `[+0]` = int32 ndims, `[+4 + 4·i]` = up to 6 inline int32 coords, `sizeof = 0x1c` (28 bytes — the candidate-vector stride, `28LL * count` and the `+= 28` walks below).

---

## 4. The `k*k*2k` path — `this[+0xe8] == 1`, 6 candidates

This path is gated on **exactly 6 candidates** (`a4[1] == &byte_6`, i.e. `candidates.size() == 6`, line 252). The rule: construct the *symmetric expected route* — a `Coordinates` of `ndims` ints, each `±tb` — and assert it is one of the 6 equal-minimum candidates, returning it on a match.

```c
function TieBreak_kk2k(K, vertex, candidates):     // inline block @ line 252
    if candidates.size() != 6: goto InvalidVertex   // line 252 gate
    norm = ManhattanNorm(vertex)                     // 0x20c0ba00 (= Σ|coord|), v22, line 256
    ndims = this[+0x8]                                // v24
    // the "tb" fold:  (norm/2) reduced modulo ((K % ndims == 0) + ndims - 1)
    tb_index = (norm/2) % ( (K % ndims == 0) + ndims - 1 )    // v25, line 261
    expected = Coordinates(ndims ints, all 0)        // operator new + memset 0, line 264
    sign = (norm & 1) ? -K : +K                      // v27, lines 267-269 (norm even ⇒ +K)
    expected[tb_index] = sign                         // line 270 — the lone ±K seam coord
    for cand in candidates:                           // stride 28, lines 277-284
        if cand == expected:                          // Coordinates::operator==
            return OK(cand)                            // copy to sret, *a1 = 1 (line 293)
    return Error("…k*k*2k twisted torus vertex %s, expected distance %s "
                 "is not in its minimum route sets.")  // 0xa000dba, line 515 (=0x203)
```

The decompile (lines 256-304) confirms each step: `ManhattanNorm` at line 256; the modular `tb` reduction at line 261 (`v22 / 2 % (((int)v117 % (int)v24 == 0) + v119[2] - 1)`, where `v117 = K`, `v24 = ndims`); the zero-fill `Coordinates` build (lines 262-271); the `(v23 & 1) ? -K : +K` sign (lines 267-269, `v23` = `norm & 0xff`; `v27` defaults to `-K` and is overwritten to `+K` when `norm` is even); and the `operator==` candidate search (stride 28, lines 277-284). On a match the chosen candidate is copied to the sret and `*a1 = 1` (OK status); on no match the `MakeErrorImpl<13>` at line 311 fires with the string `"When applying tiebreaking rule for k*k*2k twisted torus vertex %s, expected distance %s is not in its minimum route sets."` (`0xa000dba`, source line 515).

> **NOTE —** the rule for `k*k*2k` is *membership verification*, not search-and-rank: the canonical route is fully determined by `(norm, K, ndims)` before the candidate list is consulted, and the loop only confirms that this deterministic route is actually one of the six the distance layer produced. If it is not, that is a *consistency failure* between the distance generator and the tiebreak rule — hence the error wording "is not in its minimum route sets." A reimplementer whose `GetDistances` disagrees with this `±tb` construction will trip this exact error.

---

## 5. The `k*2k*2k` path — `this[+0xe8] == 2`, count dispatch

For `k*2k*2k` the rule dispatches on the **candidate count**, which is the vertex's geometric class: 4 = corner, 3 = mid, 2 = edge. The count comparisons are decompile-verified — `candidates.size()` (`a4[1]`) against `&dword_0 + 2` (==2, line 376), `+3` (==3, line 459), `+4` (==4) — with any other count falling to Invalid-vertex.

```c
function TieBreak_k2k2k(K, vertex, candidates):
    n = this[+0x8]                                   // ndims
    switch candidates.size():                           // not separate symbols —
        case 4:  return Corner(K, n, vertex, candidates)   // inline block @ line 592
        case 3:  return Mid(K, n, candidates)              // inline block @ line 459
        case 2:  return Edge(K, vertex, candidates)        // inline block @ line 376
        default: goto InvalidVertex
```

### 5.1 Corner (4 candidates)

The corner rule first finds the **dimension whose travelling distance is short** — a dim `d` for which the max `|coord|` over all candidates is `< K` — then **rotates** to a different dimension `d'` by a parity computed from the vertex, and selects the candidate whose `d'`-coord equals `±K` (sign by the same parity bit).

```c
function Corner(K, n, vertex, candidates):           // inline block @ line 592
    // 1. find winning dim d: max|cand.coord(d)| over candidates < K
    for d in 0 .. n-1:                                // lines 622-659
        max_abs = 0
        for cand in candidates:                       // lines 633-651
            max_abs = max(max_abs, |cand.coord(d)|)
        if max_abs < K: break                          // line 653 — dim d wins (v33 = d)
    else:
        return Error("…corner vertex %s, did not find a dimension whose "
                     "travelling distances are all less than tiebreaking length %d…")
                     // 0xa01debc, source line 610
    // 2. rotate to d' by the per-other-dim parity
    parity = XOR over dims != d of ( (vertex.coord(dim) >> 1) & 1 )   // v76, line 764
    d'     = (d + parity + 1) mod n                    // line 665: (v33 + v76 + 1) % v75
    // 3. select candidate whose coord(d') == ±K (sign by vertex parity bit)
    target = (vertex.parity & 1) ? -K : +K             // lines 670-672 (v102 & 1; +K when parity even)
    for cand in candidates:                            // lines 676-...
        if cand.coord(d') == target: return OK(cand)
    return Error("…corner vertex %s, expected distance %d on dimension %d "
                 "is not found among the candidates.")  // 0xa01dfed, source line 602
```

Decompile anchors: the max-`|coord|` accumulation (lines 643-647), the `max_abs < K` dim-win test (line 653, `v35 < (int)v117`), the parity `v76 ^= ((unsigned)v108 >> 1) & 1` (line 764), the rotation `(v33 + v76 + 1) % v75` (line 665), and the `(v102 & 1) ? -K : +K` target (lines 670-672). The two corner errors are byte-exact: no winning dim → `0xa01debc` (`MakeErrorImpl<13>`, source line 610); no matching candidate after rotation → `0xa01dfed` (source line 602).

> **QUIRK —** the corner rule does **not** pick the short dimension `d` itself — it picks `d' = (d + parity + 1) mod n`, a *different* dimension chosen by a parity over the *other* dims' second bits. This deterministic rotation is what disambiguates which of the two doubled axes carries the `+K` seam for a corner chip touching both `2K` axes: a naive "pick the short dim" reimplementation will route corners onto the wrong axis and pick a different (still-valid-distance) candidate, silently producing a *different* but legal route table that no longer matches the reference caches. The exact physical diagonal this rotation disambiguates was traced to its arithmetic, not tied to the `(+K,+K)` seam orientation — MEDIUM.

### 5.2 Edge (2 candidates) and Mid (3 candidates)

```c
function Edge(K, vertex, candidates):                // count==2 (a4[1] == &dword_0+2)
    cand = candidates[0]                              // v50
    for d in 0 .. ndims-1:                            // lines 382-457
        target = (vertex.parity & 1) ? -K : +K        // v51 = -K default, flipped to +K when (v115 & 1)==0
        if cand.coord(d) == target: return OK(cand)    // lines 406-413
    goto InvalidVertex                                 // line 457 — no match falls to 0xa085c3a (§6)
    // (a GetCoordinate failure inside the loop propagates that status
    //  with AddSourceLocationImpl at source lines 677 / 678, lines 395 / 450)

function Mid(K, candidates):                          // count==3 (a4[1] == &dword_0+3)
    for cand in candidates[0..2]:                      // up to 3 candidates, lines 461-561
        max_abs = max over cand's dims of |coord|      // v46 / v63 / v89
        if max_abs < K: return OK(cand)                // first under K wins (LABEL_163)
    return Error("…edge vertex %s, did not find a route whose traveling "
                 "distances are less than tiebreaking length %d for all "
                 "dimensions among its candidates.")   // 0xa01ddf5, source line 651
```

The **edge** path (count==2) walks `candidates[0]` over its dims and selects when `coord(d) == ±K`, the sign chosen by the vertex parity bit (`v115 & 1`, line 407; `v51 = -(int)v117`, line 381). When no dim matches, the path falls through to the generic **Invalid vertex** error (`0xa085c3a`, `goto LABEL_117` at line 457) — *not* the `0xa01ddf5` string; the `677` / `678` source lines that appear here are `AddSourceLocationImpl` wrappers that only fire if `GetCoordinate` itself errors. The **mid** path (count==3) iterates over up to three candidates and returns the first whose max `|coord|` is below `K` (`v46`/`v63`/`v89 < K`, LABEL_163); only if *none* qualifies does it emit the `0xa01ddf5` "edge vertex" string at source line 651 (a `GetCoordinate` failure inside the scan propagates at source line 643).

| count | vertex class | rule (canonical route) | success | fail string / line |
|---|---|---|---|---|
| 6 | (any, `k*k*2k`) | build symmetric `±tb` route, assert ∈ candidates | copy + `*a1=1` | `0xa000dba` / 515 |
| 4 | corner | dim `d`: `max\|cand\|<K`; rotate `d'=(d+parity+1)%n`; `coord(d')==±K` | OK(cand) | `0xa01debc` 610; `0xa01dfed` 602 |
| 3 | mid | first of ≤3 cands with `max\|cand\|<K` ⇒ select | OK(cand) | `0xa01ddf5` ("edge vertex" text) / 651 |
| 2 | edge | `cand.coord(d)==±K` (vertex-parity sign) | OK(cand) | falls to `0xa085c3a` (Invalid vertex) |
| — | invalid | — | — | `0xa085c3a` / Invalid vertex |

(`tb`/`K` = the `std::min` dim size = "tiebreaking length" = `v117`. `parity` = XOR of `((vertex.coord(otherdim) >> 1) & 1)` over the other dims.)

> **GOTCHA —** the vertex class is **not** a property the chip carries — it is `candidates.size()`, the number of equal-minimum routes the *distance generator* produced for that vertex (6 for `k*k*2k`; 4/3/2 for corner/mid/edge on `k*2k*2k`). A reimplementation that gets `GetDistances` slightly wrong — emitting, say, 3 candidates for a chip the reference treats as a corner — will dispatch to the *wrong* sub-rule and either select a different route or trip the wrong error. The corner/edge/mid classification therefore lives in the distance generator, and `GetTiebreak` is its strict consumer. The per-orientation distance formula that decides the count was located (`GetDistances` `0x20b420e0` via `CheckBoundary` `0x20bf5800` + `GetDistanceFromOrigin` `0x20b42980`) but not field-decoded — see §8.

---

## 6. The Invalid-vertex fallthrough

Any shape `∉ {1, 2}` (a non-twist topology that reached this vtable slot, or a `k*2k*nk` discriminant of `3` that `Init` should have reduced) — or, within `k*2k*2k`, a candidate count `∉ {2, 3, 4}`, or a count==2 (edge) candidate whose dims never hit `±K` — falls to the generic error (`LABEL_117`, the `FormatPack` at line 345):

```c
    // shape not 1 or 2, or unsupported candidate count
    return Error("Invalid vertex %s in topology %s for algorithmic "
                 "tiebreaking rule.")                  // 0xa085c3a, line 345
```

The error stringifies the topology via its virtual `Format` (the `(*(*(_QWORD *)this + 40))(…)` vtable dispatch at `+0x28`, line 339). This is the catch-all; in a correctly classified twist it is unreachable, which is why a discriminant of `3` reaching here is a signal that the `DropDimension` reduction in `Init` (§2) did not run.

---

## 7. How the tiebreak disambiguates `K_2K_2K` group assignment

The `k*2k*2k` tiebreak (§5) is what makes the `K_2K_2K` route table *deterministic and seam-consistent*. The [collective half](replica-group-2phase.md) builds the `K_2K_2K` reduce-scatter / all-gather replica groups by folding device IDs through the diagonal `(+K, +K)` twist ([`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md) with `num_max_dims == 2`); that fold tells each core *who* it reduces with. But the bytes between two grouped chips must traverse physical ICI links, and on a two-doubled-axis twist a `(src, dst)` pair frequently has multiple equal-minimum paths — one along each `2K` axis. If the route table picked arbitrarily, two chips in the same replica group could route through *different* link sets, re-introducing the very link cycle the twist exists to break.

`GetTiebreak`'s `k*2k*2k` rule removes that freedom. The **corner** rule's parity rotation `d' = (d + parity + 1) mod n` deterministically chooses *which* of the two `2K` axes carries the `+K` seam for a corner chip that touches both doubled axes — the same diagonal choice the replica-group fold makes in device-id space, now enforced in chip-coordinate space. The **edge** and **mid** rules pin the `±K` route on the single relevant doubled axis. Because the rule is a pure function of the vertex and `K`, every chip in a `K_2K_2K` group resolves its ties identically, so the per-link route table is a single coherent twisted ring rather than a mix of clockwise and counter-clockwise seams. The chosen canonical route is then memoised (`this[+0xe0]` cache-enabled, via `GetDistanceFromCache` `0x20b40fa0`) and handed to the [emission layer](../routing/get-static-path.md).

> **NOTE —** this is the routing-side mirror of the collective-side `K_2K_2K` group construction. The replica-group builder uses `R = 2K` (two doubled axes) and folds members through the `(+K,+K)` diagonal; `GetTiebreak` independently re-derives the *same* diagonal seam choice for the route table from the candidate count and the parity rotation. The two must agree — a reimplementer who builds the groups with one seam convention and the routes with the other gets a schedule whose partners and links contradict each other. See [2-Phase Replica-Group Construction](replica-group-2phase.md) (the `R = 2K` two-doubled-axis prologue).

---

## 8. What was not resolved

- **The candidate-route generator.** `GetTiebreak` consumes the `vector<Coordinates>` of equal-minimum routes and reads the vertex class *only* from its size (6 / 4 / 3 / 2). The generator that decides that count per orientation — `GetDistances` (`0x20b420e0`) via `CheckBoundary` (`0x20bf5800`) and `GetDistanceFromOrigin` (`0x20b42980`) walking the per-axis wrap mask — was located but its per-orientation `n`-fold distance formula was not field-decoded. This is the residual literal-`nK` distance math. MEDIUM. See [Get Distances](../routing/get-distances.md).
- **The `k*2k*nk` (shape == 3) literal-nK handling.** Confirmed it never reaches a `GetTiebreak` arm (no `this[+0xe8]==3` branch; `Init` `DropDimension`-reduces it). The `n > 2` distance handling in `GetDistance` / `UnboundedWalk` (`0x20b40680`) — the `+K`-mod-`nK` seam on the long axis — was not separately decoded. MEDIUM.
- **The corner parity-rotation geometry.** `d' = (d + parity + 1) mod n` with `parity = XOR((coord>>1)&1)` over the other dims is byte-confirmed, but *why this particular rotation* (which physical diagonal it disambiguates on the two doubled axes, tied to the `(+K,+K)` seam orientation of the replica-group fold) was traced to its arithmetic, not to its geometric intent. MEDIUM.
- **The route-cache path.** `GetDistanceFromCache` (`0x20b40fa0`) + `kRouteCacheSet` (`0x22011f88`) — the tiebreak result is presumably memoised here keyed on the `Coordinates` pair, but the cache key/value format and its interaction with the emission layer were not decoded. LOW. See [route cache codec](../routing/route-cache-codec.md).

---

## 9. Function & String Map

| Symbol / string | Address | Role |
|---|---|---|
| `TwistedTorusTopology::GetTiebreak` | `0x20b41320` | the tiebreak rule; entry, `std::min` K, shape dispatch |
| `TwistedTorusTopology::Init` | `0x20b3e5e0` | sets `this[+0xe8]` shape discriminant (1/2/3) |
| `TwistedTorusTopology::GetDistances` | `0x20b420e0` | candidate-route generator (the vertex-class source) |
| `TwistedTorusTopology::GetDistanceFromOrigin` | `0x20b42980` | per-axis distance walk feeding the candidate count |
| `TwistedTorusTopology::GetDistanceFromCache` | `0x20b40fa0` | route memoisation (`this[+0xe0]` cache bool) |
| `Coordinates::GetCoordinate` | `0x20c0b800` | `[+0]`=ndims, `[+4+4·i]`=coord |
| `Coordinates::ManhattanNorm` | `0x20c0ba00` | `Σ\|coord\|` — the `k*k*2k` `tb` input |
| `Coordinates::operator==` | `0x20c0bac0` | candidate ↔ expected route compare (stride 28) |
| `"…k*k*2k twisted torus vertex %s, expected distance %s is not in its minimum route sets."` | `0xa000dba` | k\*k\*2k not-in-set error (line 515) |
| `"…k*2k*2k twisted torus's edge vertex %s, did not find a route whose traveling distances are less than tiebreaking length %d…"` | `0xa01ddf5` | "edge vertex" error, emitted by the count==3 (mid) path (source line 651) |
| `"…k*2k*2k twisted torus's corner vertex %s, did not find a dimension whose travelling distances are all less than tiebreaking length %d…"` | `0xa01debc` | corner no-dim error (source line 610) |
| `"…k*2k*2k twisted torus's corner vertex %s, expected distance %d on dimension %d is not found among the candidates."` | `0xa01dfed` | corner no-match error (source line 602) |
| `"Invalid vertex %s in topology %s for algorithmic tiebreaking rule."` | `0xa085c3a` | shape∉{1,2} / bad-count fallthrough |
| `"TPU twisted torus only supports k*k*2k and k*2k*2k and k*2k*nk slice shapes."` | `0xa020458` | the shape-support classifier string |

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Section Map](overview.md) — the subsystem map; the `K`/`2K` shape gate and the two-class split (routing vs collective)
- [Shape Folds](shape-folds.md) — the `K_K_2K` / `K_2K_2K` / `K_2K_NK` coordinate-fold catalog this tiebreak mirrors in chip-coordinate space
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — the `K_2K_2K` (`R = 2K`) reduce-scatter / all-gather groups whose seam the tiebreak makes route-consistent
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the device-id-space `(+K,+K)` diagonal fold; the collective-side analogue of this routing-side rule
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the per-color `K↔2K` seam phase order

**Sibling sections**
- [Get Distances](../routing/get-distances.md) — the twist-adjusted distance vector that produces the equal-minimum candidate set this rule disambiguates
- [Get Static Path](../routing/get-static-path.md) — the route-table construction that consumes the chosen canonical route
- [back to index](../index.md)
