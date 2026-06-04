# RandomizedToroidalWildFirstPaths

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). VA == file offset throughout `.text`. Other versions will differ.*

## Abstract

`RandomizedToroidalWildFirstPaths` is the per-`(src,dst)` route generator that produces deadlock-free, fault-avoiding paths on a faulty 3D ICI torus. It is the live fallback of the resilient-routing layer: when bring-up cannot find a pre-baked `ToroidalRouteCache` matching the slice's discovered fault pattern, the deduplicator runs this generator for every chip pair and builds the routing table on the fly. The 85 pre-baked caches are the offline-computed *output* of this same algorithm, so the page documents both the cache-hit producer and the cache-miss fast path. The producing source unit is `platforms/accel_ssw/deepsea/slice_builder/tools/lib/toroidal_wild_first_paths.cc` and its `randomized_*` subclass file (both string-anchored in the binary).

The algorithm is **dimension-order routing (DOR) with a "wild-first" perturbation**. For each `(src,dst)` it first tries the minimal DOR path — strict total-order axis traversal, the textbook torus deadlock-free base. If that minimal path crosses a faulty link, it enters the wild loop: take one deliberately *non-minimal* hop ("wild first") in some axis, then resume strict DOR for the remainder. The wild hop nudges the path off the faulty lattice cell; the DOR remainder keeps the channel-dependency graph acyclic. A reimplementer familiar with Glass–Ni turn-model routing or dateline virtual-channel torus DOR will recognize the shape: one bounded non-DOR turn, validated against a half-turn symmetry constraint, is the entire fault-avoidance freedom.

> **NOTE —** "Randomized" here is **not** a runtime PRNG with a seed. It is a deterministic multi-path *policy*. `RandomizedToroidalWildFirstPaths` (`PathType=1`) keeps **every** fault-free wild path as an equal-cost candidate set so the all-reduce/runtime layer can load-balance across them; `NonRandomizedToroidalWildFirstPaths` (`PathType=0`) keeps only the **first** valid one. The "seed" the routing layer randomizes over is the deterministic `WildFirstDimensionOrder` (an `introsort` of the dimensions, `0x1fbefc20`) plus the runtime spreading hash applied at install time — there is no stochastic state in the generator. See [§ Randomized vs Non-Randomized](#randomized-vs-non-randomized).

The deterministic single static path (`GetStaticPath`, singular) and the per-color route schedule are owned by sibling pages; this page owns the `*ToroidalWildFirstPaths` algorithm, the wild-first exploration, the toroidal wrap handling, the periodic fault lattice, and how the generated candidate set feeds the route table.

For reimplementation, the contract is:

- **The `Paths(src,dst)` dispatch** — self-route short-circuit (builds a trivial all-ones path inline), then the general-case split on the fault set into `PathsWithoutFaults` (empty) / `PathsWithFaults` (non-empty).
- **The DOR base** — `PathFromDistance` + `AppendHopsToPath`: strict dimension-order traversal of the torus-reduced distance vector.
- **The wild-first loop** — one non-minimal `Walk` hop, `WildHopToPath` remainder (forward+reverse split), `IsFaultFreePath` validation, and the keep-all-vs-keep-first policy.
- **The periodic fault model** — `IsFaultFreeLink`: a single physical fault is a *lattice* of logically-faulty links, tested by a per-dimension modular comparison against the symmetry period.
- **The toroidal wrap / deadlock guarantee** — strict dimension order plus `HalfwayDirections` / `UpdateSymmetryForHalfway` (the dateline/half-turn symmetry phase).
- **The construction gate** — `CreateInternal`: `topology_size[dim] % fault_symmetry == 0` for every dimension, else no resilient route exists.

| | |
|---|---|
| **Entry point (Randomized)** | `RandomizedToroidalWildFirstPaths::Paths` `0x1fbe9fc0` |
| **Fault-aware generator** | `PathsWithFaults` `0x1fbea380` |
| **No-fault short path** | `PathsWithoutFaults` `0x213dc800` |
| **DOR base** | `PathFromDistance` `0x1fbeea20` + `AppendHopsToPath` `0x1fbeb600` |
| **Wild remainder (Randomized)** | `WildHopToPath` `0x1fbeb2c0` |
| **Fault lattice** | `IsFaultFreeLink` `0x1fbede40`, `IsFaultFreePath` `0x1fbee0c0` |
| **Deadlock phase** | `HalfwayDirections` `0x1fbee280`, `UpdateSymmetryForHalfway` `0x1fbee7c0` |
| **Construction gate** | `CreateInternal` `0x1fbeb720`; factories `CreateRandomized` `0x1fbedc20` / `CreateNonRandomized` `0x1fbedc00` |
| **Source units** | `…/slice_builder/tools/lib/toroidal_wild_first_paths.cc` and `randomized_toroidal_wild_first_paths.cc` (string-anchored) |
| **vtables** | base `0x21f57bd8`, Randomized `0x21f57c00`, Non `0x21f57b88` |
| **Return type** | `StatusOr<vector<vector<proto::Direction>>>` — a *set* of candidate paths |

---

## The Class Family and Object Layout

### Purpose

Three classes share one algorithm and one object layout; they differ only in the wild-first selection policy. The abstract base `ToroidalWildFirstPaths` holds the shared machinery (DOR base, fault model, deadlock phase, construction gate). The two concrete subclasses override only `Paths` and the wild-path policy.

### Function Map

| Class | vtable | `PathType` | Overrides | Confidence |
|---|---|---|---|---|
| `ToroidalWildFirstPaths` (abstract) | `0x21f57bd8` | — | `PathFromDistance`, `AppendHopsToPath`, `IsFaultFreeLink`, `IsFaultFreePath`, `HalfwayDirections`, `UpdateSymmetryForHalfway`, `CreateInternal`, ctor | CERTAIN |
| `RandomizedToroidalWildFirstPaths` | `0x21f57c00` | 1 | `Paths` `0x1fbe9fc0`, `PathsWithFaults` `0x1fbea380`, `PathsWithoutFaults` `0x213dc800`, `WildHopToPath` `0x1fbeb2c0` | CERTAIN |
| `NonRandomizedToroidalWildFirstPaths` | `0x21f57b88` | 0 | `Paths` `0x1fbe6ae0`, `WildFirstHops` `0x1fbe81e0`, `WildHopToPath` `0x1fbe95a0`, `AddWildHopPathIfValid` `0x1fbe8f20`, `GenerateFirstValidPathOnRing` `0x1fbe7ac0` | CERTAIN |

### Object Layout

The ctor `ToroidalWildFirstPaths(…)` at `0x1fbedc40` stores its arguments into a single flat object. Layout recovered from the ctor store sequence:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `vptr` | `+0x00` | ptr | vtable | CERTAIN |
| `topology` | `+0x08` | `ToroidalTopologyInterface*` | the discovered topology (distance/walk oracle) | CERTAIN |
| `faults` | `+0x10`/`+0x18`/`+0x20` | `vector<IciLink>` | copied seed-fault set, `0x58 B` each | CERTAIN |
| ctor coords `a`..`d` | `+0x28`,`+0x44`,`+0x60`,`+0x7c` | `Coordinates` | endpoint/box parameters | HIGH |
| `dimension_order` | `+0x98`/`+0xa0`/`+0xa8` | `vector<int>` | the DOR index list (per-instance copy) | CERTAIN |
| `symmetry` | `+0xb0` | `Coordinates` | the fault symmetry period `S` | CERTAIN |
| `path_type` | `+0xcc` | `int` | 1 = Randomized, 0 = Non | CERTAIN |
| `wild_mask` | `+0xd0`/`+0xd8`/`+0xe0` | `vector<bool>` | per-dim "wild-eligible" bitmap (read by `bt`) | HIGH |

The `topology` object is accessed only through its vtable. The slots the algorithm uses (resolved by call pattern):

| Slot | Method | Confidence |
|---|---|---|
| `+0x48` | `num_dimensions()` → 3 for a 3D torus | CERTAIN |
| `+0xa0` | `Walk(coord, direction)` — one-hop walk, returns reached `Coordinates` | HIGH |
| `+0xb8` | `GetDistances(src, dst)` — per-dim signed shortest distance (torus-wrap-reduced, twist included) | HIGH |

> **NOTE —** `Paths` at `0x1fbe9fc0` calls `topology` vtable `+0x48` (decompiled as `(*(...)(*(_QWORD*)v9 + 72LL))(v9)`, i.e. byte offset 72 = `0x48`) to get the dimension count before allocating the distance seed. The offset is byte-confirmed in the decompile.

---

## Paths — the Top-Level Dispatch

### Purpose

`Paths(src,dst)` is the public entry. It normalizes the degenerate self-route, builds the "one step in every dimension" distance seed for the general case, and dispatches to the fault-aware or no-fault generator. It always returns a *vector* of candidate paths, never a single path — this is the structural reason `GetStaticPath` (singular) is invalid in randomized mode.

### Algorithm

```c
function Paths(this, src, dst):              // RandomizedToroidalWildFirstPaths::Paths @ 0x1fbe9fc0
    if Coordinates::operator==(src, dst):    // 0x20c0bac0 — self-route (line 43)
        n    = topology->num_dimensions()    // vtable +0x48 (byte 72, line 56)
        seed = Coordinates(filled with 1, length n)  // all-ones, length n (line 171)
        return [ trivial 1-element path ]    // degenerate result built inline; no generator call
    // general case (src != dst):
    if this.faults == empty:                 // this+0x18 == 0 (a2+3, line 177)
        return PathsWithoutFaults(this, src, dst)   // tail @ 0x213dc800 (line 180)
    return PathsWithFaults(this, src, dst)   // 0x1fbea380 (line 210)
```

`PathsWithoutFaults` (`0x213dc800`) is the no-fault path: it builds the single minimal DOR path, identical in cost to the unmodified routing-table generator. It carries the `.rodata` warning **"RandomizedToroidalWildFirstPaths may have degraded performance on non-faulty topologies"** (string lives in this function) — running the resilient generator on a healthy slice is wasteful because it would consider wild detours that are never needed.

> **GOTCHA —** the all-ones, length-`n` vector built inside `Paths` is **only** the degenerate self-route case (`src == dst`); it is constructed inline and returned as a trivial one-element path — neither generator is called on the self-route branch. The general case (`src != dst`) never builds an all-ones seed: it dispatches straight on the fault set (`PathsWithoutFaults` when empty, `PathsWithFaults` otherwise), and the real distance is computed inside those via `topology->GetDistances`. A reimplementer must not feed the all-ones vector into the general route generator.

---

## PathsWithFaults — DOR Base + Wild-First Perturbation

### Purpose

This is the heart of the algorithm: try the minimal dimension-order path first, and only if it crosses the fault lattice, search the six torus directions for a single wild first hop that opens a clean route. Source unit `randomized_toroidal_wild_first_paths.cc` (string-anchored at source lines 73–119 in the decompile).

### Algorithm

```c
function PathsWithFaults(this, src, dst):    // 0x1fbea380
    dist = topology->GetDistances(src, dst)  // vtable +0xb8, torus-reduced (twist-aware)
    dor  = PathFromDistance(dist)            // 0x1fbeea20 — strict dimension order
    if IsFaultFreePath(src, dor, dst):       // 0x1fbee0c0 — DOR avoids the lattice
        return [dor]                         // common case: minimal path is clean

    paths = []
    for d in 6 torus directions:             // per-dim eligibility via wild_mask bt-test (+0xd0)
        if not wild_eligible[d]: continue
        wild = topology->Walk(src, d)        // vtable +0xa0 — ONE non-minimal hop ("wild first")
        remainder_dist = GetDistances(wild, dst)
        rem  = WildHopToPath(wild, remainder_dist)   // 0x1fbeb2c0 — DOR remainder, fwd+rev split
        full = [direction d] ++ rem
        if IsFaultFreePath(src, full, dst):  // 0x1fbee0c0 — whole {wild + remainder} clean
            half  = HalfwayDirections(dist, full)        // 0x1fbee280 — per-dim +-1 at midpoint
            phase = UpdateSymmetryForHalfway(src, full, dst)  // 0x1fbee7c0 — dateline phase / cache key
            paths.append(full)               // RANDOMIZED: keep ALL fault-free wild paths
            // NON-RANDOMIZED: break here — keep only the FIRST (GenerateFirstValidPathOnRing)
    return paths                             // StatusOr<vector<vector<Direction>>>
```

Decompile cross-check (function `0x1fbea380`): `PathFromDistance` at line 196, `IsFaultFreePath` on the DOR path at line 210, the wild-loop `WildHopToPath` at line 364, the post-wild `IsFaultFreePath` at line 382, `HalfwayDirections` at line 396, and `UpdateSymmetryForHalfway` at line 424 — exactly the order above. The source-line anchors in `AddSourceLocationImpl` (73, 74, 76, 101, 103, 105, 107, 112, 119) confirm the unit.

> **QUIRK —** "wild-first" means the **first** hop is the non-minimal one, and only that hop. The detour is bounded to a single off-minimal step in one axis; the rest is strict DOR. This is what keeps deadlock-freedom: a single bounded non-DOR turn validated against the symmetry phase cannot close a channel-dependency cycle, whereas a multi-hop detour could.

### What the Six Directions Are

A 3D torus exposes six unit directions: `±X`, `±Y`, `±Z`. The wild loop iterates them, gated by the per-dimension `wild_mask` bitmap at `this+0xd0` (read with a `bt` bit-test), bounded by the per-instance dimension index list at `this+0x98` (count at `this+0xa0`). The eligibility mask is what excludes wild hops along axes that cannot help (e.g. a fault confined to one dimension). The order in which the dimensions are considered is the deterministic `WildFirstDimensionOrder` (`introsort` `0x1fbefc20`), so the "first valid" tie-break in the non-randomized variant is reproducible.

---

## PathFromDistance + AppendHopsToPath — the DOR Base

### Purpose

`PathFromDistance` turns a signed distance vector into a strict dimension-order hop sequence; `AppendHopsToPath` emits the `|distance|` repeated directional hops for one axis. Together they are the deadlock-free base path on which the whole algorithm rests.

### Algorithm

```c
function PathFromDistance(this, dist):       // 0x1fbeea20
    path = []
    for dim in this.dimension_order:         // +0x98 list, length +0xa0
        d = dist.GetCoordinate(dim)          // signed per-axis distance
        AppendHopsToPath(dim, d, &path)      // 0x1fbeb600
    return path                              // axes traversed dim_order[0], then [1], then [2]

function AppendHopsToPath(dim, signed_d, *path):   // 0x1fbeb600
    n           = |signed_d|                 // number of hops
    orientation = Direction::DimensionToOrientation(dim)   // 0x20c02700
    sign        = (signed_d > 0) ? POS : NEG // polarity of the hop
    repeat n times:
        path.push(Direction{orientation, sign})    // emplace_back per hop
```

Decompile cross-check: `PathFromDistance` (`0x1fbeea20`) loops `GetCoordinate` (line 51) → `AppendHopsToPath` (line 55). `AppendHopsToPath` (`0x1fbeb600`) guards on `a2 > 0` (line 25), calls `DimensionToOrientation` (line 33), `emplace_back`s the `Direction` (line 45), and carries the source path `toroidal_wild_first_paths.cc` at line 62. Traversing all of one axis before the next is textbook DOR → inherently deadlock-free on a torus once a dateline edge fixes the wrap (see [§ Toroidal Wrap & Deadlock Freedom](#toroidal-wrap--deadlock-freedom)).

### WildHopToPath — the Remainder After the Wild Hop

`WildHopToPath` (Randomized override `0x1fbeb2c0`) builds the post-wild-hop remainder in **two segments**: a forward dimension-order pass, then a reverse dimension-order pass over the residual distance. The split ensures the wild detour and its remainder share no return edge, keeping the dependency graph acyclic. It records the remainder's `ManhattanNorm` as the path cost (`0x20c0ba00`, stored on the `PathCost` element) so the cache/AR layer can rank equal-length candidates.

Decompile cross-check (`0x1fbeb2c0`): `ManhattanNorm(a3)` at line 46, the forward `AppendHopsToPath` at line 71, the reverse-pass `AppendHopsToPath` at line 130 (two distinct `while` loops over `GetCoordinate`).

---

## The Periodic Fault Model — IsFaultFreeLink

### Purpose

A fault is **not** a single dead link. The fault set the algorithm reasons about is a *lattice*: one physical seed fault at coordinate `c` with direction `d` is replicated to every link at `c + k·S` (componentwise, all integer `k`) where `S` is the per-axis fault-symmetry period. `IsFaultFreeLink` enforces this lattice with a modular comparison. This periodicity is what makes the route table reusable across the whole pod: the slice tiles into `S`-periodic cells, every cell sees the same fault pattern, so one route scheme covers all `(src,dst)` pairs in the same residue class.

### Algorithm

```c
function IsFaultFreeLink(this, link, period):        // 0x1fbede40
    // `period` is the single per-instance symmetry Coordinates passed by the
    // caller (object field +0x28), NOT a per-fault field — same modulus for every fault.
    for f in this.faults:                            // this+0x10 array, +0x18 count, 0x58 B each
        matches = true
        for i in 0 .. num_dimensions-1:              // topology vtable +0x48
            a = link.GetCoordinate(i)                // link tail coord            (line 59 -> v12)
            b = f.coord.GetCoordinate(i)             // this fault's coord         (line 64 -> v15)
            m = period.GetCoordinate(i)              // per-dim period S[i]        (line 70 -> v29)
            if (a % m) != (b % m):                   // MODULAR REDUCTION
                matches = false
        matches &= Direction::IsSame(link.dir, f.dir)   // 0x20c025e0
        if matches:
            return NOT-fault-free                    // link coincides with the lattice
    return fault-free

function IsFaultFreePath(this, src, dirs, period):   // 0x1fbee0c0
    pos = src
    for d in dirs:
        if not IsFaultFreeLink(IciLink{pos, d}, period): return false  // line 40; period = this+0x28
        pos = topology->Walk(pos, d)                 // vtable +0xa0 (byte 160)
    return true                                      // every hop avoids the lattice
```

Decompile cross-check (`0x1fbede40`, the lattice test): three `GetCoordinate` reads (lines 59/64/70 — `a`, `b`, `m`), then the modular comparison

```text
LODWORD(v9) = v15 % v29;            // line 75 — fault_coord % m  (v15=fault, v29=m)
v33 &= v12 % v29 == v15 % v29;      // line 76 — AND across dims: (link % m) == (fault % m)
v17 = 4 * (v12 % v29 != v15 % v29); // line 77 — per-dim mismatch advances the loop
...
Direction::IsSame(...)              // line 143 — directions must match too
```

The `%` operator reduces each coordinate modulo the symmetry period in every dimension; a candidate link is faulty iff its reduced coordinate equals a fault's reduced coordinate in **all** dimensions **and** the directions match. This is the periodic fault lattice in code, byte-confirmed.

> **GOTCHA —** the modulus is **not** read per-fault. It is the single per-instance symmetry `Coordinates` passed as the second `Coordinates&` argument (sourced from object field `+0x28` by `PathsWithFaults`/`IsFaultFreePath`); `v29` is loaded once per dimension from that one period vector, and the same `m` is applied to every fault in the loop. A reimplementer stores the symmetry period **once on the route generator**, not on each `IciLink`. (In every shipped cache the period is uniformly `4_4_4`.)

---

## Toroidal Wrap & Deadlock Freedom

### Purpose

Two complementary guarantees make every generated path deadlock-free on the wrap-around torus.

**(A) Strict dimension order — the structural guarantee.** Every path — the minimal `PathFromDistance` and the `WildHopToPath` remainder — traverses axes in a fixed total order. DOR on a torus is deadlock-free provided each wrap (dateline) edge is assigned so the channel-dependency graph stays acyclic. The single wild hop is the only non-DOR edge, bounded to one hop and validated against the symmetry phase, so it cannot create a cycle.

**(B) Halfway / dateline symmetry — the wrap-around guarantee.** `HalfwayDirections` walks the path and, at the **midpoint** of each dimension's traversal, records a per-dimension `±1` symmetry value — which side of the torus wrap that axis traversal used. `UpdateSymmetryForHalfway` then GCD-reduces the axis coordinate to a normalized fractional "phase" (a binary-GCD against 2), producing a canonical per-axis position of the `(src,dst)` pair within the symmetry cell.

### Algorithm

```c
function UpdateSymmetryForHalfway(this, src, path, dst):   // 0x1fbee7c0
    half  = HalfwayDirections(dist, path)        // 0x1fbee280 — per-dim +-1 at the midpoint
    phase = Coordinates()
    for i in dims:
        if half[i] > 0:                          // positive half-turn on axis i
            c        = dst.GetCoordinate(i)
            phase[i] = 2 * (c / gcd(c, 2))        // binary-GCD reduce, doubled
    return phase
```

The `phase` plays a double role:

- **Deadlock constraint** — all paths sharing a phase break the wrap cycle the same way (consistent dateline side).
- **Cache key** — symmetric `(src,dst)` pairs collapse to the same `RouteScheme`, which is the basis of the `4_4_4` fault-symmetry tiling and why only ~85 cache files cover the whole shape/fault cross-product.

The dateline-edge predicate itself is `ResilientToroidalTopology::CheckOnEdge(coord, orientation)` (`0x1fbe36e0`) — it tells the topology which coordinates sit on the wrap boundary. The actual one-hop wrap math (incrementing past the last coordinate of an axis back to 0) lives in the topology's `Walk` (`0x1fbe3640` / vtable `+0xa0`) and `GetDistances` (`0x1fbe36c0` / vtable `+0xb8`), which the generator treats as black-box oracles. Twist adjustments for the `_twisted` shapes are baked into `GetDistances` — see [GetDistances](get-distances.md) and [Twist Overview](../twist/overview.md).

> **NOTE —** the `phase` is computed only for axes whose halfway symmetry is **positive** (decompile `0x1fbea380` line 414 walks `while (*(int*)&v48[v53] >= 0)`). The GCD-reduce + doubling is the canonicalization that makes two physically distinct but symmetry-equivalent pairs hash to one scheme. (HIGH confidence on the GCD-against-2 shape; the exact rounding of the doubled fraction is inferred from the `tzcnt`/`shr` binary-GCD idiom, marked HIGH.)

---

## Randomized vs Non-Randomized

### Purpose

The two subclasses implement the same DOR + wild-first search but differ in *how many* valid wild paths they keep. This single difference is what "randomized" means — a multi-path policy, not stochastic state.

### Policy Comparison

| Aspect | Randomized (`PathType=1`) | Non-Randomized (`PathType=0`) | Confidence |
|---|---|---|---|
| Wild-path policy | keep **all** fault-free wild paths | keep **first** fault-free wild path | CERTAIN |
| Emitted `RouteScheme` | `RandomHop` (set of equal-cost directions) | `StaticPath` (one direction sequence) | HIGH |
| Path enumerator | inline loop in `PathsWithFaults` | `GenerateFirstValidPathOnRing` `0x1fbe7ac0`, `AddWildHopPathIfValid` `0x1fbe8f20` | CERTAIN |
| Valid accessor | `GetStaticPaths` (plural) `0x1fbe1e20` | `GetStaticPath` (singular) `0x1fbe1ce0` | CERTAIN |
| Factory | `CreateRandomized` `0x1fbedc20` | `CreateNonRandomized` `0x1fbedc00` | CERTAIN |

`GetStaticPath` (singular, `0x1fbe1ce0`) hard-errors with the string **"GetStaticPath … is not defined when randomized_paths is enabled, use GetStaticPaths (plural)"** — byte-confirmed: that string lives in `0x1fbe1ce0`. There is no single canonical path under the randomized policy, only a candidate set.

> **QUIRK —** the "randomization" lives in *two* deterministic places, neither of which is a runtime random number: (1) the per-`(src,dst)` candidate **set** stored in the `RandomHop` scheme, and (2) the install-time / all-reduce spreading that picks one candidate per transfer via a deterministic hash. The generator is fully reproducible; the same fault pattern always yields the same candidate set in the same `WildFirstDimensionOrder`. A reimplementer must reproduce the *ordering* (`introsort` of dimensions, `0x1fbefc20`) to match the baked caches bit-for-bit. (The exact runtime spreading hash — whether it reuses `MapSrcDstCoreToRoutingTableIndex` — is structural, not byte-confirmed: LOW.)

---

## The Construction Gate — CreateInternal

### Purpose

`CreateInternal` is the constructor gate that decides whether a resilient route *can* exist for the slice before any path is generated. It validates the fault set against the symmetry model and enforces the tiling condition.

### Algorithm

```c
function CreateInternal(top, faults, symmetry, path_type):   // 0x1fbeb720
    require faults non-empty                  // else MakeErrorImpl<3> "ICI resiliency only supports
                                              //   non-empty faulty link sets with super-pod fault symmetry"
    require top.num_dimensions() == symmetry.dims     // else "Faulty symmetry and topology must have a
                                                      //   matching number of dimensions"
    for f in faults:
        for dim:
            require f.coord[dim] consistent mod symmetry[dim]   // seed faults lie on the lattice
            require fault_dim < num_dimensions  // "Fault dimension outside specified number of dimensions"
    for dim:
        require top.size[dim] % symmetry[dim] == 0   // THE GATE
            else MakeErrorImpl<3> "The topology size must be a multiple of the fault symmetry"
    return WildFirstPaths(top, faults, ..., symmetry, path_type, wild_mask)
```

Decompile cross-check (`0x1fbeb720`):

| Check | Decompile evidence | String |
|---|---|---|
| dimension agreement | line 260 | `"Faulty symmetry and topology must have a matching number of dimensions"` |
| fault dim in range | line 1452 | `"Fault dimension outside specified number of dimensions"` |
| per-fault lattice consistency | lines 983/1031/1072 (`v % m`) | (no string — silent reject path) |
| **the gate** | line 1532 `if (v17 % v250[0])` → line 1534 | `"The topology size must be a multiple of the fault symmetry"` |

> **CORRECTION —** an earlier reconstruction paraphrased the dimension-agreement check as "topology `num_dimensions()` must equal the symmetry vector's dimension count." The binary string is exactly **"Faulty symmetry and topology must have a matching number of dimensions"** (`0x1fbeb720` line 260). Same meaning, but the page uses the verbatim diagnostic.

### When No Resilient Route Exists (Chip Isolation)

The gate fails — and bring-up fails — when (1) the fault set is not on a clean lattice (breaks super-pod symmetry), (2) the slice size is not a multiple of `S` in some dimension, or (3) even with a valid lattice the generator finds no fault-free path for some `(src,dst)`, surfaced as **"No route solution for topology %s."** There is no "isolate one chip and continue"; the slice is removed. The seed fault is tiled to the full lattice by `AddFaultsFromSeed` (`0x20bfab00`) before generation, and `MaximumPathSymmetry` (`0x1fbed440`) computes the period `S` the gate uses.

---

## Related Components

| Name | Relationship |
|---|---|
| `ResilientToroidalTopology::InitRouteSolution` `0x1fbdf8a0` | Orchestrator: tries the cache, else `PopulateUnoptimizedRouteCache` `0x1fbe0a20` → this generator |
| `PopulateUnoptimizedRouteCache` `0x1fbe0a20` | Live fallback: `CreateRandomized`/`CreateNonRandomized` + `Paths(s,d)` for all pairs |
| `RouteCacheDeduplicator::Find` `0x20b59000` | Selects a pre-baked cache; on miss the live generator runs |
| `ToroidalRouteCache::RouteScheme` (`StaticPath`/`RandomHop`) | The wire form of one generated `(src,dst)` route class |
| `WildFirstDimensionOrder` `0x1fbefc20` (introsort) | The deterministic dimension ordering — the "seed" of the wild search |

---

## Cross-References

- [Routing Overview](overview.md) — the route-generation → route-cache → emission pipeline this generator feeds
- [GetStaticPath & Multipod](get-static-path.md) — the deterministic single static path accessor (singular), invalid in randomized mode
- [CreateRoutingSchedule Solver](create-routing-schedule.md) — the per-hop schedule built from the generated path set
- [Route-Table Generation](route-table-generation.md) — physmap and the route-table emission that consumes the schemes
- [GetDistances](get-distances.md) — the nK twisted-torus distance metric (`vtable +0xb8`) the wild-first loop calls
- [Twist Overview](../twist/overview.md) — the twisted-torus wrap handling baked into `GetDistances` for `_twisted` shapes
- [ICI Topology Discovery](../ici/topology-discovery.md) — where the topology and the seed fault set originate
