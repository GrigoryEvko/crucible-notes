# GetDistances

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships with full C++ symbols; `.text` VMA == file offset, so every `0x20b…` address below is a VMA. Other versions will differ.*

## Abstract

`GetDistances` is the **candidate-route generator** for the slice-builder twisted torus: given a source and a destination coordinate, it returns *every* minimum-Manhattan-distance route between them, as a `vector<Coordinates>` of signed per-axis displacement vectors. It is the geometric core that the path generators ([`GetStaticPath`](get-static-path.md), [`RandomizedToroidalWildFirstPaths`](randomized-toroidal-wildfirst.md)) consume — they take a distance vector, pick one axis order, and pack it into hops. This page owns the *distance* end: `TwistedTorusTopology::GetDistances` @ `0x20b420e0` (the generator), `TwistedTorusTopology::GetDistanceFromOrigin` @ `0x20b42980` (the per-axis toroidal-with-twist distance), and the seam table that makes the torus *twisted* rather than plain.

The familiar reference frame is the textbook **toroidal distance**: on a ring of size `S`, the distance between two positions is `min(forward, backward)` where one direction wraps around the seam (`±S`). A 3-D torus does this per axis independently; choosing the wrap direction on a subset of axes is a sign choice, so a node has up to `2^(#wrappable-axes)` candidate routes, and the shortest may *tie* across several of them. `GetDistances` enumerates exactly that power set: it builds a **Polarity** vector (POSITIVE = wrap forward, NEGATIVE = wrap backward, NONE = no wrap) for each combination, computes the resulting displacement via `GetDistanceFromOrigin`, takes the `ManhattanNorm`, and keeps every displacement that ties the minimum. The size of the returned set is the *vertex class* — corner / edge / mid — that the twist tiebreaker ([`twist/get-tiebreak`](../twist/get-tiebreak.md)) dispatches on.

The single divergence from a plain torus is the **twist seam**. On a doubled (length-`2K`) axis, crossing the wrap edge does not return you to the same position on the partner axes — it shifts you by `+K`, the `(+K,+K)` diagonal fold that the replica-group construction also uses. `GetDistanceFromOrigin` threads this with a per-wrap **seam carry**: when an axis wraps, it adds a per-axis offset vector (`seam[d]`, from `this+0x108`) into the running accumulator and folds it modulo each axis size. This is the entire nK-shape story — the long `nK` axis is just another dimension with `dimsize = nK` and its own seam entry; there is no special-case arm, which is why the tiebreaker needs none either.

For reimplementation, the contract is:

- **The candidate enumeration** (`GetDistances`): the wrap-mask popcount, the `2^m`-capped-`≥2`-but-set-to-`1`-when-`<2` Polarity loop, the min-Manhattan keep-all-ties rule, and the sorted-insert into a `vector<Coordinates>`.
- **The per-axis toroidal-with-twist distance** (`GetDistanceFromOrigin`): the signed `delta = c − accum[d]`, the Polarity-gated `±dimsize[d]` wrap, the wrap-trigger condition, and the seam carry `accum[j] = (seam[d][j] + accum[j]) mod dimsize[j]`.
- **The seam table** (`this+0x108`): a `vector<vector<int>>` built in `Init`, one inner vector per axis, encoding the `+K` diagonal offset that a doubled-axis wrap induces on the partner axes.

| | |
|---|---|
| **Generator** | `TwistedTorusTopology::GetDistances` @ `0x20b420e0` (vtable `+0xc0`) |
| **Per-axis distance** | `TwistedTorusTopology::GetDistanceFromOrigin` @ `0x20b42980` |
| **Single-distance consumer** | `TwistedTorusTopology::GetDistance` @ `0x20b408e0` (vtable `+0xb8`) |
| **Norm / compare** | `Coordinates::ManhattanNorm` @ `0x20c0ba00` · `operator<` @ `0x20c0ba40` |
| **Seam / sizes / order** | `this+0x108` seam · `this+0x10` dim-sizes · `this+0x40` dim-order · `this+0xf0` wrap-mask |
| **Polarity enum** | `NONE=0`, `POSITIVE=1`, `NEGATIVE=2` (`accel_ssw::deepsea::proto::Polarity`) |
| **Coordinates** | `int32 ndims @+0`, `≤6 int32 coords @+4..+0x18`, `sizeof = 0x1c` |
| **Source** | `platforms/accel_ssw/deepsea/slice_builder/friends/twisted_torus_topology.cc` |

> **NOTE —** two vtable slots are one letter apart and are *not* interchangeable. `vtable+0xc0` is `GetDistances` (plural) — the candidate-set generator on this page. `vtable+0xb8` is `GetDistance` (singular) — the cache-aware *single* distance that [`GetStaticPath`](get-static-path.md) calls. Sibling pages that say "`GetStaticPath` calls `GetDistances` (`vtable+0xb8`)" are naming the singular slot by its plural sibling; the byte-exact distinction is in [§GetDistance](#getdistance--the-cache-aware-single-distance).

---

## GetDistances — the candidate-route generator

### Purpose

`GetDistances(src, dst)` returns a `StatusOr<vector<Coordinates>>`: the set of every signed per-axis displacement vector that achieves the minimum Manhattan distance from `src` to `dst` on the twisted torus. A plain mesh has one shortest path per pair; a torus has one *per axis-wrap choice*, and the twist makes several of those wrap choices tie at the minimum. The set is the input to the tiebreaker and the path packers downstream — the generator answers "how far, and in how many equally-good ways"; the consumers answer "which one, and via which hop order."

### Entry Point

```text
TwistedTorusTopology::GetDistances (0x20b420e0)          ── vtable+0xc0, candidate-set generator
  ├─ Topology::CheckBoundary(src) (0x20bf5800)           ── line 390, both src and dst must be in-bounds
  ├─ Topology::CheckBoundary(dst) (0x20bf5800)           ── line 391
  ├─ Coordinates::Subtract(src, dst) (0x20c0bfc0)        ── line 394, raw mesh delta (boundary-validated)
  ├─ vtable[+0x98](src, raw_delta)                       ── line 395, normalize coord into dim range
  ├─ popcount(wrap_mask[this+0xf0])                      ── #wrappable axes  → combo count
  ├─ for each Polarity combination:
  │     └─ GetDistanceFromOrigin(dst, polarity, wrap)    ── line 444 (0x20b42980)
  │           └─ ManhattanNorm (0x20c0ba00)              ── keep all equal-minimum vectors
  └─ vector<Coordinates>::emplace (operator< 0x20c0ba40) ── sorted insert of each tying vector
```

### Algorithm

```c
function GetDistances(this, src, dst):                   // 0x20b420e0
    if !CheckBoundary(src):  return Error(line 390)       // 0x20b42106
    if !CheckBoundary(dst):  return Error(line 391)       // 0x20b4211b
    raw   = Coordinates::Subtract(src, dst)               // 0x20b42137, line 394 — plain mesh delta
    base  = this->vtable[0x98](src, raw)                  // 0x20b42174, line 395 — fold into dim range
                                                          //   (base is the "from-origin" coord passed below)
    wrap  = this->wrap_applies                            // this+0xf0, vector<bool>: which axes may wrap
    m     = popcount(wrap.bits)                            // 0x20b42210 (bt; adc) — #wrappable axes
    n_combos = (1 << m) < 2 ? 1 : (1 << m)                // 0x20b42404 (shl; cmp $2; cmovl $1)

    best  = 0x7FFFFFFF                                     // 0x20b4242e — min Manhattan norm so far
    routes = []                                           // vector<Coordinates>
    for combo in 0 .. n_combos-1:                          // outer bound is 1<<ndims, see GOTCHA
        // Build the per-axis Polarity span for this combination.
        pol = new Polarity[ndims]                          // 0x20b42470
        for axis a in 0 .. ndims-1:
            pol[a] = _bittest(combo, a) ? NEGATIVE : POSITIVE   // (bit ? 2 : 1) — set for every axis
        // Build the wrap-applies bitset that gates the seam carry, masking out non-wrappable axes.
        wmask = bitset(ndims)                              // 0x20b424d0-ish
        for axis a in 0 .. ndims-1:
            wmask[a] = wrap[a] && pol_combo_selects(a)     // only axes in this combo wrap

        dc = GetDistanceFromOrigin(base, pol, wmask)       // 0x20b42671, line 444
        norm = ManhattanNorm(dc)                           // 0x20b426aa — Sigma |coord|
        if norm < best:                                    // 0x20b426af
            best   = norm
            routes = [dc]                                  // reset to the new minimum
        else if norm == best:
            routes.emplace_sorted(dc)                      // 0x20b4278e, operator< (0x20c0ba40)
        free(pol)
    return routes                                          // |routes| == vertex class size
```

> **GOTCHA —** the combination count has two distinct bounds and they are *not* the same value. The inner accept/reject loop is capped at `n_combos = max(1<<popcount, 1)` (set to `1` when `1<<m < 2`, i.e. when no axis wraps — `0x20b42404`). But the **outer** loop-continuation test reloads `ndims` and compares `combo >= (1 << ndims)` (`0x20b4244c`/`0x20b42460` region), not `1 << m`. A reimplementation that drives both bounds off the popcount will iterate too few combinations on a topology where `wrappable < ndims`; one that drives both off `ndims` builds redundant Polarity spans on non-wrappable axes (harmless — those axes get `pol = POSITIVE/NEGATIVE` but `wmask[a] = 0`, so no wrap fires). The safe reading: build `2^ndims` Polarity spans, but only the axes whose `wrap_applies` bit is set actually wrap; duplicate displacement vectors collapse in the keep-all-ties set because `operator<` orders them identically.

> **QUIRK —** the generator computes a `Coordinates::Subtract` (`0x20b42137`, line 394) — the *plain mesh* delta — before the Polarity loop, then normalizes it through `vtable+0x98` (line 395) into a `base` coordinate that is the actual argument to `GetDistanceFromOrigin`. So the per-axis distance is computed *from the normalized mesh delta*, not from `dst` directly; the Polarity loop then re-introduces the wrap on top of it. This is the opposite of the intuitive "compute fresh per combination" reading and matters for matching the wrap arithmetic exactly — `GetDistanceFromOrigin` sees a coordinate already reduced into `[0, dimsize)` per axis, and its `delta = c − accum` runs against a zero-initialized accumulator.

### The keep-all-ties set

The result is a *set of equal-minimum-distance routes*, not a single distance. `best` starts at `INT32_MAX` (`0x20b4242e`); a strictly smaller norm resets `routes` to a one-element vector, an equal norm sorted-inserts via `Coordinates::operator<` (lexicographic, `0x20c0ba40`). The final `vector<Coordinates>` is therefore sorted and deduplicated by displacement.

| `\|routes\|` | Vertex class | Shape | Consumer |
|---|---|---|---|
| `6` | symmetric mid | `k*k*2k` | [`get-tiebreak`](../twist/get-tiebreak.md) corner/edge/mid dispatch |
| `4` | corner | `k*2k*2k` | (same) |
| `3` | mid | `k*2k*2k` | (same) |
| `2` | edge | `k*2k*2k` | (same) |

> **NOTE —** the `|routes|` → vertex-class mapping (above) is the count the [twist tiebreaker](../twist/get-tiebreak.md) reads to pick a single canonical route; the byte-exact class boundaries belong to that page. This page owns *why the set has that cardinality* (the `2^(#wrap-axes)` tie structure), not the selection. Confidence on the specific counts: HIGH (anchored on the tiebreaker page).

### Function Map

| Function | Address | Role |
|---|---|---|
| `TwistedTorusTopology::GetDistances` | `0x20b420e0` | Candidate-set generator (vtable `+0xc0`) |
| `TwistedTorusTopology::GetDistanceFromOrigin` | `0x20b42980` | Per-axis toroidal-with-twist distance |
| `Coordinates::ManhattanNorm` | `0x20c0ba00` | `Σ\|coord\|` (vpabsd + horizontal add) |
| `Coordinates::operator<` | `0x20c0ba40` | Lexicographic order for the sorted set |
| `Coordinates::Subtract` | `0x20c0bfc0` | Plain mesh delta before normalization |
| `Topology::CheckBoundary` | `0x20bf5800` | In-bounds guard on src and dst |

---

## GetDistanceFromOrigin — the per-axis toroidal-with-twist distance

### Purpose

Given a (normalized) coordinate, a per-axis `Polarity` span, and a wrap-applies bitset, `GetDistanceFromOrigin` produces one signed displacement vector: per axis it is the direct delta, optionally wrapped by `±dimsize` when the chosen polarity crosses that axis's seam, with the **twist seam carry** threading a doubled-axis wrap onto the partner axes. This is the function that distinguishes a *twisted* torus from a plain one; on a plain torus the seam table is empty and the carry is a no-op.

### Entry Point

```text
TwistedTorusTopology::GetDistanceFromOrigin (0x20b42980)
  ├─ Topology::CheckBoundary(coord) (0x20bf5800)         ── line 702
  ├─ alloc accum[ndims], res[ndims]  (memset 0)          ── two int32 arrays
  ├─ for i in dim_order[this+0x40]:
  │     ├─ Coordinates::GetCoordinate(coord, dim)        ── line 713 (0x20c0b800)
  │     ├─ delta = c - accum[dim]                         ── signed
  │     ├─ Polarity[dim] gate → ±dimsize[dim]             ── wrap fwd/back
  │     └─ seam carry: accum[j] = (seam[dim][j]+accum[j]) % dimsize[j]
  └─ Coordinates(res, ndims)                             ── line 713/error line 702
```

### Algorithm

```c
function GetDistanceFromOrigin(this, coord, pol, wmask):  // 0x20b42980
    if !CheckBoundary(coord):  return Error(line 702)     // 0x20b429a9
    ndims = this->ndims                                   // this+0x8
    accum = new int[ndims];  memset(accum, 0)             // 0x20b429dd — running carry per axis
    res   = new int[ndims];  memset(res,   0)             // 0x20b429f1 — output displacement per axis

    for i in 0 .. ndims-1:                                // walk axes in DIMENSION ORDER
        dim = this->dim_order[i]                          // this+0x40 base, this+0x48 count (0x20b42a28)
        c   = coord.GetCoordinate(dim)                     // 0x20b42a4c, line 713
        a   = accum[dim]                                   // current carry for this axis
        delta = c - a                                      // 0x20b42a74 — signed direct distance
        res[dim] = delta

        wrapped = false
        switch pol[dim]:                                   // [pol + dim*4] (0x20b42a81)
          POSITIVE(1): if c < a:                            // 0x20b42a95
                          res[dim] = delta + dimsize[dim]   // wrap forward; this+0x10 (0x20b42a9e)
                          wrapped = true
          NEGATIVE(2): if c > a:                            // 0x20b42ada
                          res[dim] = delta - dimsize[dim]   // wrap backward (0x20b42ae2)
                          wrapped = true
          // NONE(0): res[dim] stays the direct delta
        // A third wrap trigger: an axis flagged wrappable whose endpoints coincide.
        if !wrapped && wmask[dim] && c == a:               // 0x20b42aXX (bittest wmask; cmp c,a)
            wrapped = true                                 // seam carry still fires (degenerate wrap)

        if wrapped and seam[dim] is non-empty:             // seam = this+0x108[dim], stride 0x18
            for j in 0 .. ndims-1:                          // thread the twist onto every axis
                accum[j] = (seam[dim].data[j] + accum[j]) % dimsize[j]   // 0x20b42b3f / 0x20b42b98
        accum[dim] = c                                      // 0x20b42b.. — this axis's carry := c
    return Coordinates(res, ndims)                          // 0x20b42c34
```

> **GOTCHA —** the seam-carry modulus folds by `dimsize[j]` — the *target* axis `j`'s size — **not** by `dimsize[dim]`, the wrapping axis. The decompile is unambiguous: the modulus operand is `dimsize_base[this+0x10] + 4*j` inside the `j` loop (`0x20b42b3f`/`0x20b42b98`), indexed by the inner counter. A reimplementation that folds the whole carry by the wrapping axis's size silently corrupts every partner axis whose size differs from `dim`'s — which on a `k*2k*nk` shape is most of them. The byte-exact reading is `mod dimsize[j]`.

> **QUIRK —** the coordinate is read from the *argument* (`coord` / the normalized `base` `GetDistances` passes), and the accumulator threads the carry *forward* as axes are visited in dimension order — `accum[dim]` is only set to `c` (`0x20b42b..`) *after* the seam carry has already mutated `accum[j]` for the partner axes. So a wrap on an early-order axis shifts the reference point that later-order axes measure their delta against. This ordering coupling is the twist: the axes are not independent, and visiting them out of `dim_order` produces a different (wrong) displacement.

### Why nK needs no special case

For the `k*2k*nk` shape the long `nK` axis is simply another entry in `dim_order` with `dimsize = nK` and its own seam vector `seam[nK_axis]`. The per-axis loop wraps it by `±nK` exactly as it wraps a `2K` axis by `±2K`, and the seam carry threads it onto the partner axes through `seam[nK_axis][j]`. There is no `nK`-only arm in this function, and correspondingly no `[+0xe8]==3` arm in the [tiebreaker](../twist/get-tiebreak.md): the entire `nK` dependence lives here, in `dimsize[nK_axis]` and the seam offset. The only place an `nK` axis is treated specially is up in [`GetDistance`](#getdistance--the-cache-aware-single-distance), which can *drop* the `nK` axis before computing on the reduced 3-D twist.

### Function Map

| Function | Address | Role |
|---|---|---|
| `TwistedTorusTopology::GetDistanceFromOrigin` | `0x20b42980` | Per-axis distance + seam carry |
| `Coordinates::GetCoordinate` | `0x20c0b800` | Read axis `dim` from a coordinate |
| `Coordinates::Coordinates(int*, n)` | `0x20c0b1e0` | Build the result `Coordinates` from `res[]` |

---

## The seam table — `this+0x108`

### Purpose

The seam table is what makes the torus *twisted*. It is a `vector<vector<int>>` at `this+0x108` (count `this+0x110`), one inner vector per axis, each `ndims` long. `seam[d]` is the offset vector that crossing the wrap edge on axis `d` adds to the running accumulator: on a plain torus it is all-zero (wrap returns you to the same partner-axis position), but on a doubled (`2K`) axis it records the `+K` diagonal — the same `(+K,+K)` fold the replica-group construction uses ([`twist/get-replica-pair-3d`](../twist/get-replica-pair-3d.md)).

### Construction (in `Init`)

```c
function Init(this, dim_sizes, twisted):                 // 0x20b3e5e0
    ...
    this->wrap_applies.assign(ndims, ...)                 // this+0xf0 (line 149, 0x20b3e604)
    this->seam.assign(ndims, vector<int>{})               // this+0x108 (lines 167-168, 0x20b3e681)
    // classify axes: an axis of even size >=2 is "doubled" (size == 2K) via K = size/2 (idiv 0x20b3ea37)
    for each doubled axis d:                               // fill loop 0x20b3ea90
        for each partner doubled axis j:                   // 0x20b3eaa4
            this->seam[d][j] = +K                           // the +K diagonal twist (0x20b3eafc)
    // wrap bits for doubled axes are also set here (this+0xf0[d] |= 1)   (0x20b3e870-ish)
```

> **NOTE —** the *structure* of the fill (doubled-axis detection via the `size==2K` test, the partner-axis store into `seam[d][j]`, and the wrap-bit set on `this+0xf0`) is byte-confirmed in `Init`. The exact per-`(d,j)` seam **value matrix** — which axis carries `+K` vs `+2K` vs `0` onto which, for each of the `k*k*2k` / `k*2k*2k` / `k*2k*nk` orientations — was read to its `+K` diagonal arithmetic but not exhaustively tabulated per orientation. The carry consumer (`GetDistanceFromOrigin`, above) is fully decoded; the table that feeds it is HIGH-confidence on shape, LOW on the complete per-orientation value matrix. Treat `seam[d][j]` as "the twist offset axis-`d`-wrap induces on axis `j`", inferred from `Init`'s `+K` fill rather than as a fully tabulated constant matrix.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `ndims` | `+0x8` | `int32` | Axis count; bounds every per-axis loop |
| dim-sizes | `+0x10` / `+0x18` | `vector<int>` base / count | Per-axis size = the wrap modulus `dimsize[a]` |
| dim-order | `+0x40` / `+0x48` | `vector<int>` base / count | Axis visit order in `GetDistanceFromOrigin` |
| wrap-applies | `+0xf0` / `+0xf8` | `vector<bool>` base / bitcount | Which axes may wrap; popcount → combo count |
| seam | `+0x108` / `+0x110` | `vector<vector<int>>` base / count | Per-axis `+K` twist offset list |

---

## GetDistance — the cache-aware single distance

### Purpose

`GetDistance` @ `0x20b408e0` (vtable `+0xb8`) is the *consumer* of this geometry that [`GetStaticPath`](get-static-path.md) and the route emitter call. It returns one distance, not the candidate set: it first consults the embedded precomputed [route cache](toroidal-route-cache.md), and only on a per-pair cache miss falls back to the live metric (the `GetDistances` / `GetDistanceFromOrigin` machinery above). It is documented here only at the boundary — the cache mechanics live on [`toroidal-route-cache`](toroidal-route-cache.md), the path packing on [`get-static-path`](get-static-path.md).

### Algorithm

```c
function GetDistance(this, src, dst):                    // 0x20b408e0, vtable+0xb8
    if this->cache_enabled:                               // BYTE[this+0xe0]==1 (0x20b40900)
        r = GetDistanceFromCache(src, dst)                // 0x20b40fa0 — embedded ToroidalRouteCache
        if r is ok:  return r                             // cache HIT → cached distance, done
        // per-pair cache MISS: fall through to the live metric (0x20b40922)
    if this->alt_cache_ptr && this->nK_valid:             // [this+0x120]!=0 && BYTE[this+0x134] (0x20b40968)
        // k*2k*nk shape: drop the nK axis, compute on the reduced 3-D twist
        src' = DropDimnesion(src, this->nK_dim)           // [this+0x130] (0x20b40988)
        dst' = DropDimnesion(dst, this->nK_dim)
        ...compute on the reduced coordinates...
    return  the single twisted-torus distance (the GetDistances min-norm route)
```

> **NOTE —** the per-pair cache-miss fall-through to the live metric is byte-confirmed structurally: after `GetDistanceFromCache` (`0x20b40fa0`) returns, `GetDistance` branches on the `StatusOr` ok-bit (`0x20b40922`) and proceeds to `LABEL_8` — the live computation — when the lookup is *not* ok. This is distinct from the *whole-cache* miss in `InitRouteSolution`, which hard-errors `"Cannot find route cache: "` with no live fallback (see [`toroidal-route-cache`](toroidal-route-cache.md)). A single absent `(src,dst)` pair degrades to the live metric; an absent baked cache *file* for the slice shape aborts bring-up. Confidence: HIGH (the fall-through branch is byte-confirmed; the inline live-recompute body is traced to the branch but not decoded line-by-line for every cache type).

> **GOTCHA —** the `DropDimnesion` spelling (one transposed letter) is the real symbol in the binary (`accel_ssw::deepsea::slice_builder::(anonymous namespace)::DropDimnesion` @ `0x20b3f4e0`, called at `0x20b40999`). Grep for it verbatim; do not "correct" it.

---

## Relationship to the consumers

| Component | What it takes from here | Page |
|---|---|---|
| `GetStaticPath` | `GetDistance` (vtable `+0xb8`), the single min-norm distance, then packs hops | [get-static-path](get-static-path.md) |
| `RandomizedToroidalWildFirstPaths` | the torus-reduced distance, then a fault-avoiding multi-path search | [randomized-toroidal-wildfirst](randomized-toroidal-wildfirst.md) |
| twist tiebreaker | `\|GetDistances result\|` as the corner/edge/mid vertex class | [twist/get-tiebreak](../twist/get-tiebreak.md) |
| route cache | the distance-vector value stored per `(src,dst)` chip-id pair | [toroidal-route-cache](toroidal-route-cache.md) |

`GetDistances` produces the *set*; the consumers each reduce it to one route by a different policy — deterministic DOR (`GetStaticPath`), fault-avoiding search (`WildFirst`), or a precomputed lookup (the cache). The seam-and-wrap math is shared by all three; only the selection differs.

## Cross-References

- [Routing Overview](overview.md) — the route-generation → cache → emission pipeline this distance metric seeds (stage 1)
- [Get Static Path](get-static-path.md) — the deterministic single-path consumer; calls `GetDistance` (vtable `+0xb8`) and packs `DirectionHops`
- [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md) — the fault-avoiding multi-path consumer of the same torus distance
- [Toroidal Route Cache](toroidal-route-cache.md) — the precomputed cache `GetDistance` reads before falling back to this live metric
- [Create Routing Schedule](create-routing-schedule.md) — the orthogonal net_router per-step DMA schedule (distinct from the auto-route distance here)
- [Twisted Torus](../twist/overview.md) — the twist geometry; the `(+K,+K)` fold the seam table encodes, and the tiebreaker that consumes `|GetDistances|`
