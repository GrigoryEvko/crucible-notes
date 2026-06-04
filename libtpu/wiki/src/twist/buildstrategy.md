# TwistedTorusND::BuildStrategy

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`TwistedTorusND::BuildStrategy` (`0x137d0c00`) is the driver that turns a classified twisted slice into the per-color ring neighbour/ordinal tables the all-reduce emitter rides. It is the twisted-torus override of `StrategyND::BuildStrategy`: it builds the *same* rectangular ND ring that any torus strategy builds, then **overwrites the doubled-axis edges** with the twist fold so the two length-`K` rings of the short axis join end-to-end into one length-`2K` ring while each long axis carries an ordinary doubled ring. The reference frame is a per-axis ring schedule the way LLVM's reduce-scatter lowering would emit one — except that one logical ring is re-threaded across two physical axes by the dateline seam.

The function is a strict **phase pipeline** running on top of a setup prologue. The prologue (`UpdateMinMaxDims` → `InitColorDimensions`) runs in the **`TwistedTorusND` constructor** (`0x137d0040`, call sites `0x137d00df` / `0x137d0115`) — *not* in `BuildStrategy`'s own body — and reduces the three torus extents to the `(K, 2K)` scalars and the two axis-count fields, then fills the `color_dims[6][3]` permutation table. `BuildStrategy` then **reads** those already-populated fields (`[obj+0x5f0]`/`[obj+0x5f8]`/`[obj+0x600]`/`[obj+0x608]`/`color_dims`) rather than recomputing them. **Stage 1** builds the unwrapped base ND ring for every color and active dimension (`ComputeOrdinal`, `Torus2DevicePhase0Neighbor` / `MeshStrideNPhasekNeighbor`, deposited via `UpdateNeighborLocation`). Then **Stage 2** runs a per-color, per-ring-dimension loop whose inner index `p ∈ {0,1,2}` is the **ring-dimension column** of `color_dims[c]`; the physical axis stored in that column decides whether the phase seams `K→2K` or `2K→K`, dispatching to the four seam builders.

The single insight a reimplementer must carry from this page: **the phase index `p` is literally the third argument to every seam builder, and it is also the column index into `color_dims[c]`**. The fold direction is not chosen by `p` — it is chosen by *which physical axis* `color_dims[c][p]` names (the `K`-axis or one of the `2K`-axes). `p` only selects which of the three RingLocation neighbour/ordinal slots the result lands in. This page owns the BuildStrategy driver, the phase→column mapping, `InitColorDimensions`, and the four seam builders' roles; it does **not** re-derive the twist predicate ([Twist Predicate & Orientation](twist-predicate-orientation.md)), the per-shape coordinate fold ([Shape Folds](shape-folds.md), [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md)), or the downstream 2-phase replica-group build ([2-Phase Replica-Group Construction](replica-group-2phase.md)).

For reimplementation, the contract is:

- **The prologue.** `UpdateMinMaxDims` sets `min_dim_size_`/`max_dim_size_` and the `num_min_dims_`/`num_max_dims_` counts; `InitColorDimensions` fills `color_dims[6][3]` as a cyclic permutation mod `NumNetworkDimensions` (or the degraded remap).
- **The two-stage build.** A base rectangular ND ring (reused from `StrategyND`), then a per-color seam overwrite of the doubled-axis edges.
- **The phase→column→fold mapping.** Phase `p` is `color_dims[c]` column `p`; the column's axis class (`K` vs `2K`) selects the fold direction; the four seam builders apply it.
- **The two shape branches.** `num_max_dims_ == 1` (K_K_2K: one 2K axis) vs `num_max_dims_ != 1` + `num_min_dims_ == 1` (K_2K_2K: two 2K axes) take structurally distinct axis-identification paths.

| | |
|---|---|
| **Entry point** | `TwistedTorusND::BuildStrategy` `0x137d0c00` (~0x18c0 B, ends at next symbol `0x137d24c0`) |
| **Signature** | `(TwistedTorusND* this, const Target&, LloRegionBuilder*)` |
| **Prologue** | run by the **ctor** `TwistedTorusND::TwistedTorusND` `0x137d0040` (not by `BuildStrategy`): `UpdateMinMaxDims` `0x137d0260` · `InitColorDimensions` `0x137d0800` |
| **ND-ring gate** | `[obj+0xa8] == 1` (ND ring vs 1-D ring; same as base `StrategyND`) |
| **Classifier fields** | `num_max_dims_` `[obj+0x600]` (qword idx 192) · `num_min_dims_` `[obj+0x608]` (idx 193) |
| **Base-ring helpers** | `ComputeOrdinal` `0x137c5300` · `Torus2DevicePhase0Neighbor` `0x137c57a0` · `MeshStrideNPhasekNeighbor` `0x137c5cc0` |
| **Seam builders** | `UpdateNeighborsKTo2K` `0x137d24c0` · `UpdateNeighbors2KToK` `0x137d29c0` · `UpdateOrdinal2K` `0x137d2c60` · `UpdateOrdinal2KToK` `0x137d28c0` |
| **Phase index** | `p ∈ {0,1,2}` — third call argument == `color_dims[c]` column |
| **VLOG anchors** | `all_reduce_strategies.cc:390` ("TorusPhasekNeighbor, stride: ") · `:1916` ("color count: ") |
| **Confidence** | HIGH — phase dispatch, the four seam builders, the two-branch classifier, and the prologue all decompile-verified unless a row/callout says otherwise |

---

## 1. Entry Point and Phase Pipeline

```text
TwistedTorusND::TwistedTorusND  (ctor)   0x137d0040   ── runs the setup prologue BEFORE BuildStrategy
  ├─ UpdateMinMaxDims                    0x137d0260   ── @0x137d00df: K/2K scalars + num-K/num-2K counts  (§3 of overview)
  └─ InitColorDimensions                 0x137d0800   ── @0x137d0115: color_dims[6][3] cyclic fill / degraded remap
       └─ UseResilientAlgorithmTwistedTorus 0x1c894fc0   ── env[0x1116] + GetDegradedAxis != -1 gate
            └─ InitColorDimensionsDegraded  0x137c6580   ── (resilient tail) degraded [6][3] remap

TwistedTorusND::BuildStrategy            0x137d0c00   ── twisted-torus override of StrategyND::BuildStrategy
  │   (reads the prologue fields above; does not call the prologue itself)
  ├─ STAGE 1 — base ND ring  (0x137d0e62..0x137d13ad, per color × dim)
  │    ├─ StrategyND::ComputeOrdinal     0x137c5300   ── coord -> ring ordinal
  │    ├─ Torus2DevicePhase0Neighbor     0x137c57a0   ── +1/-1 neighbour, no-wrap fast path
  │    ├─ MeshStrideNPhasekNeighbor      0x137c5cc0   ── neighbour with inline ModuloRingSize fold
  │    └─ UpdateNeighborLocation         0x137c5fa0   ── deposit into CwCore/CounterCwCore buffers
  └─ STAGE 2 — per-color, per-phase seam  (0x137d168a..0x137d1c68)
       ├─ UpdateNeighborsKTo2K           0x137d24c0   ── K-axis column: K->2K neighbour seam
       ├─ UpdateOrdinal2KToK             0x137d28c0   ── K-axis column: inverse 2K->K ordinal fold
       ├─ UpdateNeighbors2KToK           0x137d29c0   ── 2K-axis column: 2K->K neighbour seam
       └─ UpdateOrdinal2K                0x137d2c60   ── 2K-axis column: 2K ordinal fold
```

The function body (with the prologue already done by the constructor) is one base-ring loop, and then a **two-way branch** on the 2K-axis count (`num_max_dims_`) into two nearly identical Stage-2 loops — one for the single-2K-axis shape (K_K_2K) and one for the double-2K-axis shape (K_2K_2K). Both Stage-2 loops walk colors × phases with the same seam-builder vocabulary; they differ only in the axis-identification CHECKs and in how many phases land in the `2K→K` branch.

> **NOTE —** `BuildStrategy` does **not** decide which physical ICI links carry each hop — it produces the *logical* neighbour ordinals only. The physical link assignment is the routing half's job (`TwistedTorusTopology`, [routing overview](../routing/overview.md)). A reimplementer who stops at this page has a ring schedule that knows its partners but not its wires.

---

## 2. Prologue — UpdateMinMaxDims and InitColorDimensions

### Purpose

The prologue — run by the `TwistedTorusND` constructor (`0x137d0040`) at `0x137d00df`/`0x137d0115`, before `BuildStrategy` is ever entered — reduces the slice to the four numbers the rest of the function keys on and fills the color-dimension permutation table. `BuildStrategy` consumes these fields read-only. `UpdateMinMaxDims` (`0x137d0260`) is documented as the shape gate on [Twist Predicate & Orientation](twist-predicate-orientation.md) and summarized in the [section overview](overview.md#3-the-shape-gate-and-the-shape-fold-catalog); only the fields BuildStrategy consumes are repeated here.

### Fields it leaves for BuildStrategy

| Field | Offset | qword idx | Meaning |
|---|---|---|---|
| `min_dim_size_` (`K`) | `[obj+0x5f8]` | 191 | short axis size |
| `max_dim_size_` (`2K`) | `[obj+0x5f0]` | 190 | long axis size; `CHECK max == 2·min` |
| `num_max_dims_` | `[obj+0x600]` | 192 | count of axes equal to `2K` (the 2K-count) |
| `num_min_dims_` | `[obj+0x608]` | 193 | count of axes equal to `K` (the K-count) |
| `NumNetworkDimensions` | `[obj+0x598]` | 179 | `3` on a healthy slice, `2` when degraded |
| `dim_sizes_[i]` | `[obj+0xb8..0xc8]` | 23/24/25 | per-axis extents (load order `Y,X,Z`) |

The classifier fields are produced by a vectorised `vpcmpeqq` count (decompiled in `UpdateMinMaxDims`) and define the shape: `num_max_dims_ == 1` is K_K_2K (one long axis), `num_max_dims_ == 2` with `num_min_dims_ == 1` is K_2K_2K (two long axes). BuildStrategy keys its top-level Stage-2 branch on exactly these two fields.

### InitColorDimensions — the color_dims[6][3] fill

`InitColorDimensions` (`0x137d0800`) fills the `[6][3]` table that names, for each color row and each ring-dimension column, the **physical torus axis** that column's ring traverses.

```c
function InitColorDimensions(this, target):                 // 0x137d0800
    if UseResilientAlgorithmTwistedTorus(target, …):        // 0x1c894fc0 — env[0x1116] + GetDegradedAxis != -1
        this.NumColors = NumNetworkDimensions - 1            // [obj+0x8] = [obj+0x598] - 1
        return InitColorDimensionsDegraded(target, …)        // 0x137c6580 — tail: demote dead axis to inner column
    this.NumColors = NumNetworkDimensions                    // [obj+0x8] = [obj+0x598]
    VLOG(1) << "color count: " << NumColors                  // all_reduce_strategies.cc:1916
    // non-resilient fill: each color row is a rotation of {0,1,2}
    for color c in 0 .. up_to_6:
        for column d in 0 .. NumNetworkDimensions-1:         // n = [obj+0x598] = 3
            color_dims[c][d] = (c·stride + d) mod n          // [obj+0xd0 + c*0x18 + d*8]
```

The decompile shows the modulo-`n` arithmetic literally as `1 % n`, `2 % n`, … (`idiv`/`div` by `NumNetworkDimensions`), so each color's three columns are a cyclic rotation of `{0,1,2}`. The `.rodata` note `"twisted topology should have 3 pairs of colors"` confirms the 6-row (3×2) color structure. The resilient path tail-calls the base `InitColorDimensionsDegraded` (`0x137c6580`) — the same `[6][3]` remap the [picker](../collectives/strategy-nd-picker.md) and [degraded-axis](../collectives/degraded-axis.md) machinery use, which demotes the dead axis to the inner ring column.

> **QUIRK —** the cyclic permutation is *why* the per-color seam stays balanced. Because each color row is a rotation of `{0,1,2}`, consecutive colors place the `K`-axis (the seam-bearing axis for K_K_2K) in a different column. So the `K→2K` fold rotates across physical axes color by color, spreading the doubled-axis ICI bandwidth instead of overloading one link set. A reimplementer who fills `color_dims` with a constant permutation gets a correct ring but an unbalanced one.

---

## 3. Stage 1 — The Base ND Ring

### Purpose

Before any twist, BuildStrategy builds the ordinary rectangular ND ring — the same one `StrategyND::BuildStrategy` produces — so the seam phase has a complete neighbour table to overwrite. Conceptually this is the "unwrapped" torus: each axis is a flat ring of its `N` chips closed by the wrap link, with no dateline.

### Gate and algorithm

The build is guarded by the same `[obj+0xa8] == 1` ND-ring-vs-1-D-ring gate the base strategy uses. The loop runs per color, per active dimension:

```c
function BuildBaseRing(this):                          // 0x137d0e62..0x137d13ad
    for color c, for active dim d:
        coord = copy RingLocation coords               // [rbp-0x70] <- [rbp-0x40]
        ord   = StrategyND::ComputeOrdinal(this, coord, …)     // 0x137c5300
        if (coord | ring_size) needs no wrap:          // fast path: (or rax,r13) == 0
            fwd = Torus2DevicePhase0Neighbor(coord, …, +1)     // 0x137c57a0
            bwd = Torus2DevicePhase0Neighbor(coord, …, -1)
        else:                                          // wrap path
            VLOG(1) << "TorusPhasekNeighbor, stride: " << stride   // all_reduce_strategies.cc:390
            coord' = ModuloRingSize(coord)             // inline SltS32/SaddS32/Sselect + SgeS32/SsubS32/Sselect
            fwd = MeshStrideNPhasekNeighbor(coord', …, +1, 1)     // 0x137c5cc0
            bwd = MeshStrideNPhasekNeighbor(coord', …, -1, 1)
        UpdateNeighborLocation(this, &CwCore[c][d],        fwd, …)   // 0x137c5fa0, [obj+0x238+c*0x48+d*0x18]
        UpdateNeighborLocation(this, &CounterCwCore[c][d], bwd, …)   //            [obj+0x3e8+…]
```

The inline `ModuloRingSize` fold (`SltS32(coord,0)+SaddS32+Sselect` and `SgeS32+SsubS32+Sselect`) wraps a coordinate into `[0, ring_size)` before the mesh-stride neighbour query; on the no-wrap fast path the `Torus2DevicePhase0Neighbor` `+1`/`-1` neighbours are used directly. Both forward (clockwise) and backward (counter-clockwise) neighbours are deposited into the per-color neighbour buffers at `[obj+0x238 + color*0x48 + dim*0x18]` (CwCore) and `[obj+0x3e8 + …]` (CounterCwCore). The Stage-2 seam then **overwrites** the doubled-axis entries of these buffers.

---

## 4. Stage 2 — The Per-Color, Per-Phase Seam

### Purpose

Stage 2 re-threads the ring so the two length-`K` segments of the short axis join end-to-end into the single length-`2K` reduce-scatter ring, with the seam (the `+K`-mod-`2K` jump) cutting the cyclic dependency. It does this by overwriting the doubled-axis entries the base ring left behind, color by color, phase by phase.

### The phase → column → fold-direction mapping

The Stage-2 loop is `for color c (count ≤6): for phase p in {0,1,2}`. The inner index `p` is **simultaneously** the third argument to every seam builder and the column index into `color_dims[c]`:

```c
function SeamOverwrite(this):                          // 0x137d168a..0x137d1c68
    for color c in 0 .. NumColors-1:                   // [obj+0x8], <= 6
        for phase p in 0,1,2:                          // p == color_dims column AND seam-builder arg
            axis = color_dims[c][p]                     // [r13 + p*8 - 0xe8] = [obj+0xd0 + c*0x18 + p*8]
            if axis == K_axis_index:                    // [rbp-0x38]
                // this phase folds K -> 2K: join the two length-K rings
                UpdateNeighborsKTo2K(this, c, p, …, cw[p], ccw[p], lrb)    // 0x137d24c0
                UpdateOrdinal2KToK  (this, c, p, axis, …, lrb)            // 0x137d28c0 (inverse ordinal)
            else:                                       // axis is a 2K-axis ([rbp-0x48] / [rbp-0x80])
                // this phase folds 2K -> K: ordinary doubled ring
                UpdateNeighbors2KToK(this, c, p, …, cw[p], ccw[p], lrb)    // 0x137d29c0
                UpdateOrdinal2K     (this, c, p, axis, …, lrb)            // 0x137d2c60 (phases 0,1 only)
```

The neighbour-info arguments fed from the stack step by `+0x18` per phase (`cw[0]`/`ccw[0]` at `[rcx+0]`/`[r14+0]`, `cw[1]`/`ccw[1]` at `+0x18`, `cw[2]`/`ccw[2]` at `+0x30`) — i.e. the three RingLocation neighbour slots of the color row, one per phase column.

The dispatch table, with the byte-exact call sites and their `edx` phase immediates:

| Phase `p` | `color_dims[c][p]` axis class | Seam builders (call sites, `edx` immediate = `p`) |
|---|---|---|
| 0 | `K`-axis | `UpdateNeighborsKTo2K` `@0x137d183e` + `UpdateOrdinal2KToK` `@0x137d1871` |
| 0 | `2K`-axis | `UpdateNeighbors2KToK` `@0x137d18b9` + `UpdateOrdinal2K` `@0x137d1a24` |
| 1 | `K`-axis | `UpdateNeighborsKTo2K` `@0x137d19c0` + `UpdateOrdinal2KToK` `@0x137d1a4e` |
| 1 | `2K`-axis | `UpdateNeighbors2KToK` `@0x137d1a9b` + `UpdateOrdinal2K` `@0x137d1ad9` |
| 2 | `K`-axis | `UpdateNeighborsKTo2K` `@0x137d1beb` + `UpdateOrdinal2KToK` `@0x137d1c19` |
| 2 | `2K`-axis | `UpdateNeighbors2KToK` `@0x137d1c63` (no `UpdateOrdinal2K`) |

> **QUIRK —** phase 2 has **no** `UpdateOrdinal2K` call. The `UpdateOrdinal2K` ordinal fold fires only on phases 0 and 1 (`@0x137d1a24` and `@0x137d1ad9`); the phase-2 `2K`-axis branch updates only the neighbour table (`UpdateNeighbors2KToK @0x137d1c63`). A reimplementer who symmetrically calls the ordinal fold on all three phases will corrupt the third ring dimension's ordinal — the third dimension's `2K` ordinal is left as the base-ring value by design. This is decompile-verified: there is no third `UpdateOrdinal2K` call site — the Stage-2 loop tabulated above (the `num_max_dims_ != 1` / K_2K_2K branch, `0x137d168a..0x137d1c70`) emits exactly eleven seam calls (three `KTo2K`, three `Ordinal2KToK`, three `2KToK`, two `Ordinal2K`).

> **GOTCHA —** the fold direction is chosen by the *axis class of the column*, **not** by the phase number. Phase `p` is just a slot index. The same phase `p=0` folds `K→2K` for one color (whose column 0 holds the `K`-axis) and `2K→K` for another color (whose column 0 holds a `2K`-axis), because `InitColorDimensions` rotated the permutation. Driving the fold off `p` instead of off `color_dims[c][p]` is the single most likely reimplementation bug.

### The two shape branches

BuildStrategy splits the Stage-2 loop on the 2K-axis count at `[obj+0x600]` (`num_max_dims_`):

```c
if (num_max_dims_ == 1):                                // K_K_2K — one 2K axis
    find min_dim_index  (the K axis)                     // CHECK "min_dim_index >= 0"
    find the single max_dim_index (the 2K axis)          // CHECK "max_dim_index >= 0",
                                                         //       "dim_sizes_[max_dim_index] == max_dim_size_"
    // each color's columns are a rotation of {K, K, 2K}: one K->2K seam phase
else:                                                    // K_2K_2K — two 2K axes
    CHECK num_min_dims_ == 1                              // "num_min_dims_ == 1"  (exactly one K axis)
    find min_dim_index  (the single K axis)              // CHECK "dim_sizes_[min_dim_index] == min_dim_size_"
    find both 2K-axis indices ([rbp-0x48], [rbp-0x80])   // CHECK "num_min_dims_ == num_dims_ - 1"
    // each color's columns are a rotation of {K, 2K, 2K}: one K->2K seam, two 2K->K seams
```

The CHECK strings `"num_min_dims_ == 1"` (`@0x137d1...`, line-anchored in the decompile body), `"dim_sizes_[min_dim_index] == min_dim_size_"`, `"dim_sizes_[max_dim_index] == max_dim_size_"`, `"min_dim_index >= 0"`, `"max_dim_index >= 0"`, and `"num_min_dims_ == num_dims_ - 1"` are all decompile-verified verbatim and confirm the field names `num_min_dims_`/`num_max_dims_`/`min_dim_index`/`max_dim_index`/`dim_sizes_[]`/`min_dim_size_`/`max_dim_size_`. The K_K_2K branch produces exactly one `K→2K` seam phase per color (the column holding `K`) and two ordinary-ring phases; the K_2K_2K branch produces one `K→2K` seam phase and two `2K→K` seam phases per color.

> **NOTE —** the `K_2K_NK` shape (`n > 2`) never reaches this branch: `UpdateMinMaxDims`'s `max == 2·min` CHECK fatal-errors on any axis that is not exactly `K` or `2K`, so the jellyfish collective folds every twisted slice through the `num_max_dims_ ∈ {1,2}` machinery. The literal `nK` long axis matters only to the routing-side `TwistedTorusTopology`. See [Shape Folds](shape-folds.md).

---

## 5. The Four Seam Builders

Each Stage-2 phase calls one neighbour seam plus (usually) one ordinal seam. The neighbour seam rewrites which physical chip the ring step lands on; the ordinal seam rewrites the ring index that chip occupies. The byte-level math is recapped here; the full coordinate fold is on [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) and [Shape Folds](shape-folds.md).

### UpdateNeighborsKTo2K — `0x137d24c0` (K-axis column, K→2K)

Joins the two length-`K` rings end-to-end into the length-`2K` ring. The seam predicate fires at the last chip of a `K`-segment and jumps `+K` along the long axis:

```c
function UpdateNeighborsKTo2K(this, color, phase, dim, …):   // 0x137d24c0
    seam = SeqS32(coord, K-1)                            // K-1 = [obj+0x5f8]-1; high end of a K-segment
           AND (Pimm(dir == 1) OR SeqS32([obj+0x180], dir-1))
    // per coordinate in the ring: fold the long axis by +K mod 2K, gated by seam
    wrapped = ModuloRingSize(SaddS32(coord_long, K), 2K) // 0x137c61a0 — modulus = [obj+0x5f0] (2K)
    folded  = Sselect(seam, wrapped, base_coord)
    fwd_chip = ToChipId(folded, …)                       // ToChipId 0x1d519cc0
    // forward neighbour (CwCore), then backward (CounterCwCore) via a second seam pass
    [obj + color*0x48 + dim*0x18 + 0x238] = fwd_chip      // overwrite base-ring CwCore entry
    [obj + color*0x48 + dim*0x18 + 0x3e8] = bwd_chip      //   and the CounterCwCore entry
```

### UpdateOrdinal2KToK — `0x137d28c0` (K-axis column, inverse ordinal)

Maps a `2K` ring ordinal back into `[0, K)` — the inverse fold that re-numbers the joined ring's positions:

```c
function UpdateOrdinal2KToK(this, color, phase, dim, …):  // 0x137d28c0
    in_lower = SltS32(coord, K)                          // K = [obj+0x5f8] (min_dim_size_)
    ordinal' = Sselect(in_lower, SmodU32(ordinal, K), SsubS32(ordinal, K/2))
    // else branch subtracts K/2 = [obj+0x5f8]/2, not K; slot [obj + color*0x18 + phase*8 + 0x1a8]
```

### UpdateNeighbors2KToK — `0x137d29c0` (2K-axis column, 2K→K)

The symmetric `2K→K` neighbour seam applied to each long axis — the ordinary doubled-ring neighbour with the fold that keeps the long axis a clean `2K` ring (structurally the mirror of `KTo2K`).

### UpdateOrdinal2K — `0x137d2c60` (2K-axis column, 2K ordinal; phases 0,1 only)

Folds the long-axis ordinal across the seam, scaled by direction:

```c
function UpdateOrdinal2K(this, color, phase, dim, dir, …):  // 0x137d2c60
    at_or_past = SgeS32(coord, K)
    slot       = [obj + color*0x18 + phase*8 + 0x1a8]        // the per-color/per-phase ordinal slot
    ordinal'   = Sselect(at_or_past,
                         SmodU32(SaddS32(ord, dir·K), dir·2K),   // dir·K = imul [obj+0x5f8]; dir·2K = imul [obj+0x5f0]
                         ord)
```

The ordinal slot offset `[obj + color*0x18 + phase*8 + 0x1a8]` makes the `phase` argument the **column selector** into the per-color ordinal row — the same role `p` plays for the neighbour buffers in Stage 1.

### Seam builder map

| Builder | Address | Column class | Role |
|---|---|---|---|
| `UpdateNeighborsKTo2K` | `0x137d24c0` | `K`-axis | `K→2K` neighbour seam (`+K`-mod-`2K` jump) |
| `UpdateOrdinal2KToK` | `0x137d28c0` | `K`-axis | inverse `2K→K` ordinal fold |
| `UpdateNeighbors2KToK` | `0x137d29c0` | `2K`-axis | `2K→K` neighbour seam |
| `UpdateOrdinal2K` | `0x137d2c60` | `2K`-axis | `2K` ordinal fold (phases 0,1 only) |

---

## 6. Function Map

| Function | Address | Role |
|---|---|---|
| `TwistedTorusND::BuildStrategy` | `0x137d0c00` | driver: prologue + base ND ring + per-color seam |
| `TwistedTorusND::UpdateMinMaxDims` | `0x137d0260` | prologue: `K`/`2K` scalars + axis counts |
| `TwistedTorusND::InitColorDimensions` | `0x137d0800` | `color_dims[6][3]` cyclic fill / degraded remap |
| `UseResilientAlgorithmTwistedTorus` | `0x1c894fc0` | `env[0x1116]` + `GetDegradedAxis != -1` resilient gate |
| `BaseStrategyND::InitColorDimensionsDegraded` | `0x137c6580` | degraded `[6][3]` remap (resilient tail) |
| `StrategyND::ComputeOrdinal` | `0x137c5300` | coord → ring ordinal (Stage 1) |
| `BaseStrategyND::Torus2DevicePhase0Neighbor` | `0x137c57a0` | `+1`/`-1` neighbour, no-wrap fast path |
| `BaseStrategyND::MeshStrideNPhasekNeighbor` | `0x137c5cc0` | neighbour with inline `ModuloRingSize` fold |
| `BaseStrategyND::UpdateNeighborLocation` | `0x137c5fa0` | deposit into Cw/CounterCw neighbour buffers |
| `TwistedTorusND::UpdateNeighborsKTo2K` | `0x137d24c0` | `K→2K` neighbour seam |
| `TwistedTorusND::UpdateOrdinal2KToK` | `0x137d28c0` | inverse `2K→K` ordinal fold |
| `TwistedTorusND::UpdateNeighbors2KToK` | `0x137d29c0` | `2K→K` neighbour seam |
| `TwistedTorusND::UpdateOrdinal2K` | `0x137d2c60` | `2K` ordinal fold (phases 0,1) |
| `ModuloRingSize` | `0x137c61a0` | `+K`-mod-`2K` coordinate wrap |
| `ToChipId` | `0x1d519cc0` | folded coordinate → chip ID |

---

## 7. What Was Not Resolved

- **The 2K-second-index resolution for K_2K_2K.** The `cmov` chain (`0x137d15e1..0x137d165c`) that distinguishes the first 2K-axis index (`[rbp-0x48]`) from the second (`[rbp-0x80]`) was traced to its class effect but not reduced to a closed per-shape formula for which physical axis becomes the "primary" 2K seam. LOW. See [Shape Folds](shape-folds.md).
- **`UpdateNeighbors2KToK` byte-level math.** Located and confirmed symmetric to `UpdateNeighborsKTo2K`, but the `2K→K` seam predicate was not transcribed instruction-by-instruction. MEDIUM.
- **The downstream phase split.** `BuildStrategy` builds the per-color ring neighbour/ordinal tables; how the `2K` ring is then partitioned into the Phase0 reduce-scatter and Phase1 all-gather replica groups is owned by [2-Phase Replica-Group Construction](replica-group-2phase.md).

---

## Cross-References

- [Twisted Torus — Section Map](overview.md) — the subsystem map and the BuildStrategy phase summary this page expands
- [Twist Predicate & Orientation](twist-predicate-orientation.md) — `UpdateMinMaxDims`'s `max == 2·min` predicate and the Orientation/Polarity dimension map
- [Shape Folds](shape-folds.md) — the `K_K_2K` / `K_2K_2K` / `K_2K_NK` coordinate-fold catalog the seam builders implement
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the `+K`-mod-`2K` coordinate fold and the `num_max_dims == 2` gate
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — how the per-color ring becomes the Phase0 RS / Phase1 AG replica groups
- [SelectNDStrategy — the ND Collective-Algorithm Picker](../collectives/strategy-nd-picker.md) — the C-ii branch that constructs `TwistedTorusND` and calls `BuildStrategy`
- [Degraded Axis](../collectives/degraded-axis.md) — the `GetDegradedAxis` source feeding `UseResilientAlgorithmTwistedTorus`
- [back to index](../index.md)
