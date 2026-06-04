# Twisted-Torus Shape Folds

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); all addresses are VMA. Every symbol below is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

A twisted torus is admitted only when the ICI slice has exactly two distinct axis extents — a short axis `K` and a long axis `2K = 2·K` — and every axis is one or the other. The `(num-2K-axes, num-K-axes)` pair then sorts the slice into one of **three** `TwistedTorusShape` cases, and those three are the *only* shapes a twisted torus supports: the single error string the slice builder emits when the gate fails reads, verbatim, `"TPU twisted torus only supports k*k*2k and k*2k*2k and k*2k*nk slice shapes."` (`CreateTpuSliceTopology` `0x1ff939c0`, `topology_helper.cc`). This page is the **fold catalog**: for each of `K_K_2K`, `K_2K_2K`, and `K_2K_NK`, how the doubled-axis ring is re-threaded back onto the short `K`-axis, and how the megacore even/odd device split pairs with the resulting plane.

The mechanism every shape shares is the `+K`-mod-`2K` **seam**, the dateline that breaks the folded-ring deadlock (see [Twisted Torus — Overview](overview.md#1-what-a-twisted-torus-is)). A `2K`-chip reduce-scatter ring cannot be a flat loop on one physical doubled axis without the forward and return halves contending for the same ICI links; instead the ring walks the short `K`-axis (`K` chips), jumps `+K` along the doubled axis, and walks the `K`-axis a second time. The two length-`K` segments land on the two halves of the doubled dimension. What differs **per shape** is *how many* doubled axes the seam touches: `K_K_2K` (one `2K` axis) applies a one-axis `+K` seam; `K_2K_2K` (two `2K` axes) applies the seam to *both* axes simultaneously — a diagonal `(+K, +K)` offset. `K_2K_NK` (a long axis of literal extent `n·K`, `n > 2`) is a routing-layer concept only: inside the jellyfish collective it can only enter as the `n = 2` case, so it folds through the same `num2K ∈ {1,2}` machinery.

The fold is computed by `GetReplicaPair3DOnTwistedTorus` (`0x1c893400`), dispatched on `(num2K, arg)`. The arg-`0` (single-phase) folds are what the live v0.0.40 collective uses, and they are byte-decoded here. The megacore split is a separate, *orthogonal* decision applied in Phase1 (`GetPhase1ReplicaGroups` `0x137d3de0`): the group count is `2K · LogicalDevicesPerChip`, and the even/odd `{2m, 2m+1}` split fires when a chip presents **two** logical devices (non-megacore, 2 cores), not when it is in megacore mode.

For reimplementation, the contract of the shape-fold catalog is:

- **The three-shape gate.** `UpdateMinMaxDims` (`0x137d0260`) reduces three extents to `K`, `2K`, and the two axis counts, with two fatal `CHECK`s; the count pair selects the shape. There is no fourth shape.
- **The per-shape seam fan-out.** `num2K` (`1` vs `2`) is *the* parameter that distinguishes `K_K_2K` from `K_2K_2K`: it is the number of doubled axes the `+K`-mod-`2K` seam is added to. `K_2K_NK` reduces to one of these two.
- **The unified fold.** A single closed form covers all three physical orientations of which axis is `K`: `coord(K-axis) = t mod K`, `coord(2K-axis) = (var + seam) mod 2K`, with `seam = (t mod 2K ≥ K) ? K : 0` and `t` the loop variable of the `K`-axis.
- **The megacore plane split.** Phase1 group count `= 2K · LogicalDevicesPerChip`; the `{2m, 2m+1}` even/odd split is the 2-logical-device (non-megacore 2-core) case, not megacore mode.

| | |
|---|---|
| **Shape enum** | `TwistedTorusShape {UNSPECIFIED, K_K_2K, K_2K_2K, K_2K_NK}` |
| **Shape gate** | `TwistedTorusND::UpdateMinMaxDims` `0x137d0260` (two fatal CHECKs) |
| **Gate CHECK 1** | `max_dim_size_ == 2 * min_dim_size_` ("Max. dim size should be 2 times the min. in a twisted torus") |
| **Gate CHECK 2** | `num_min_dims_ + num_max_dims_ == num_dims_` ("Dimension sizes should either be maximum or minimum") |
| **Slice-shape error string** | `"TPU twisted torus only supports k*k*2k and k*2k*2k and k*2k*nk slice shapes."` (`CreateTpuSliceTopology` `0x1ff939c0`) |
| **Distinguishing parameter** | `num2K = num_max_dims_` (`[obj+0x600]`): `1`→`K_K_2K`, `2`→`K_2K_2K` |
| **Coordinate fold** | `GetReplicaPair3DOnTwistedTorus` `0x1c893400` (dispatch on `(num2K, arg)`; `num_max_dims == 2` CHECK gates `num2K ∉ {1,2}`) |
| **Megacore split** | `GetPhase1ReplicaGroups` `0x137d3de0`, group count `2K · LogicalDevicesPerChip(0)` (`0x20ad3020`) |
| **Confidence** | HIGH (decompile-verified shape CHECKs, slice-shape string verbatim, fold dispatch, group-count formula) unless a row/callout says otherwise |

---

## 1. The shape gate — three shapes, no fourth

Before any fold runs, `TwistedTorusND::UpdateMinMaxDims` (`0x137d0260`) reduces the three torus extents to two scalars and two counts. Two fatal `CHECK`s *define* "twisted", and the count pair is the classifier:

```c
function UpdateMinMaxDims(target):              // 0x137d0260
    // reduce the three axis extents to short K and long 2K
    min_dim_size_ = min extent                  // [obj+0x5f8]  (this+191)
    max_dim_size_ = max extent                  // [obj+0x5f0]  (this+190)
    CHECK_EQ(max_dim_size_, 2 * min_dim_size_)  // "Max. dim size should be 2 times the min.
                                                //  in a twisted torus"   (FATAL)
    // count axes equal to each extent (decompiled as vpcmpeqq lane sums)
    num_max_dims_ = count(extent == max_dim_size_)   // [obj+0x600]  (this+192) = num2K
    num_min_dims_ = count(extent == min_dim_size_)   // [obj+0x608]  (this+193) = numK
    CHECK_EQ(num_min_dims_ + num_max_dims_, num_dims_)   // == 3
                                                // "Dimension sizes should either be
                                                //  maximum or minimum"   (FATAL)
```

Both CHECK strings are decompile-verified verbatim (`MakeCheckOpString<long,long>(..., 2 * R9, "max_dim_size_ == 2 * min_dim_size_")` and `"num_min_dims_ + num_max_dims_ == num_dims_"` in `UpdateMinMaxDims`). The `2 * min_dim_size_` operand is the literal multiply the first CHECK compares against. The `(num2K, numK)` pair selects the shape:

| `num2K` / `numK` | Shape | `TwistedTorusShape` | Doubled axes |
|---|---|---|---|
| 1 / 2 | `K, K, 2K` | `K_K_2K` | one `2K` axis |
| 2 / 1 | `K, 2K, 2K` | `K_2K_2K` | two `2K` axes |
| (1 or 2) | `K, 2K, nK` → folds as `n=2` | `K_2K_NK` | one `2K` axis (collective); literal `nK` only in routing |

`num2K + numK == 3` always (CHECK 2), so only the partitions `(1,2)` and `(2,1)` exist for a 3-D slice; `(0,3)` is a plain cube (not twisted) and `(3,0)` is impossible (max ≠ min). That is *why* there are exactly two collective-side folds (`num2K ∈ {1,2}`), and the slice-builder string lists exactly three shapes — the third (`K_2K_NK`) being a routing distinction, not a fourth fold.

> **NOTE —** `UpdateMinMaxDims` reads the three obj dim fields in the order `[obj+0xb8]=Y`, `[obj+0xc0]=X`, `[obj+0xc8]=Z`; the loop-variable↔axis map used by every fold is `Y↔j`, `X↔i`, `Z↔k`, byte-confirmed from the `GetReplicaPair3D` call-site push order in both phases. The classifier does **not** care which physical axis is the `K`-axis — the fold (§3) is orientation-agnostic via the `t = K-axis loop var` selector. See [Twist Predicate & Orientation](twist-predicate-orientation.md).

---

## 2. `K_2K_NK` reduces to the `num2K ∈ {1,2}` cases

The literal `K_2K_NK` shape — a long axis of extent `n·K` with `n > 2` — never reaches a distinct collective fold. Two independent CHECKs forbid it inside the jellyfish path:

1. `UpdateMinMaxDims`'s `CHECK_EQ(max_dim_size_, 2 * min_dim_size_)` fatal-errors on any `max = n·K` with `n ≠ 2`.
2. `GetReplicaPair3DOnTwistedTorus` re-asserts `num_max_dims == 2` on every `num2K ∉ {1,2}` fallthrough — three fatal `MakeCheckOpString<long,long>(a5, 2, "num_max_dims == 2")` sites, one per `arg` arm (decompile-verified at `0x1c89391c` / `0x1c893945` / `0x1c89396e`).

So inside the jellyfish ND collective, a long axis is admitted **only when it equals `2K`** (i.e. `n = 2`). A would-be `K_2K_NK` slice that the collective sees is therefore folded as `K_K_2K` (one doubled axis) or `K_2K_2K` (two), through the identical `num2K ∈ {1,2}` machinery of §3. The literal `nK` geometry — where the long axis is genuinely `n`-fold doubled — matters *only* to the routing-side `TwistedTorusTopology::GetTiebreak` / `GetDistance` (`0x20b41320` / `0x20b408e0`), which compute the minimum-hop path on an `n`-fold-doubled axis. The `k*k*2k` / `k*2k*2k` tiebreak strings and the route caches live there, not in the replica-group fold.

> **GOTCHA —** do not implement a third coordinate fold for `K_2K_NK`. The collective half has exactly two folds (`num2K = 1` and `num2K = 2`); the `nK` distinction is entirely on the routing side. A reimplementer who reads "three shapes" as "three folds" will write a fold that the binary never reaches and that `num_max_dims == 2` would fatal-error on. See [Get Tiebreak](get-tiebreak.md) for the routing-side `nK` handling and [Twisted Torus — Overview §2](overview.md#2-the-twistedtorus-class-family).

---

## 3. The fold geometry — the unified `(num2K, arg)` closed form

`GetReplicaPair3DOnTwistedTorus` (`0x1c893400`) maps a `(i, j_or_m, k)` ring/plane index to a physical `(coord_Y, coord_X, coord_Z)` and then looks up the two megacore-core logical device IDs `{core0, core1}` from the `[Y][X][Z]` physical→logical table. Its signature carries `2K` (`a3`), `K` (`a4`), `num2K` (`a5`), and `arg` (`a6`); the dispatch is a two-level tree:

```text
GetReplicaPair3DOnTwistedTorus(map, dims, 2K=a3, K=a4, num2K=a5, arg=a6, i=a7, j_or_m=a8, k=a9):
   dispatch on arg (a6):   == 1  → 2-phase folds  (CHECK-gated unreachable in v0.0.40)
                           == 0  → single-phase folds  (LIVE)
                           else  → arg-other folds (unreachable)
     each nests on num2K (a5):  == 1 → K_K_2K fold   (one-axis seam)
                                == 2 → K_2K_2K fold   (two-axis diagonal seam)
                                else → CHECK(num_max_dims == 2)  FATAL
```

The live single-phase collective always passes `arg = 0` (the shard bool, which `GetPerColorShardIdTable` `0x137d2d80` gates as 1-phase-only). So the two folds a reimplementer must reproduce are `(arg=0, num2K=1)` and `(arg=0, num2K=2)`. Both reduce to a **single orientation-agnostic closed form**.

### The seam

Let `t` be the loop variable of the short `K`-axis — whichever of `{j, i, k}` indexes the dimension whose extent equals `K` (`Y==K ⇒ t=j`, `X==K ⇒ t=i`, `Z==K ⇒ t=k`). The seam is the dateline:

```text
seam = ((t mod 2K) >= K) ? K : 0
```

In the decompile this is the `if (a7 >= a4)` guard combined with `a7 % a4` (coord on the `K`-axis) and `a7 + a4` (the `+K` offset on a doubled axis), evaluated against `2K = a3` and `K = a4` (verified at the modulo/add sites `0x1c8935f5` / `0x1c89362d` / `0x1c8937fa` and the `% a4` reductions). The `K`-ring threads the short axis twice over the doubled `2K` member span; the second pass (`t ≥ K`) is shifted by `+K`.

### The unified coordinate fold

```c
// the (arg=0) fold, both num2K==1 and num2K==2; t = the K-axis loop var
seam = ((t mod 2K) >= K) ? K : 0
coord(K-axis)        = t mod K                     // % a4
for each 2K-axis x:  coord(x) = (var_x + seam) mod 2K   // (var + a4) % a3 on the seamed pass
return map[coord_Y][coord_X][coord_Z]              // -> {core0, core1}
```

The *only* difference between the two shapes is **how many `2K` axes receive the seam**:

| Shape | `num2K` | Seam applied to | `Phase0 R` |
|---|---|---|---|
| `K_K_2K` | 1 | the single `2K` axis (one-axis `+K` seam) | `K` |
| `K_2K_2K` | 2 | **both** `2K` axes simultaneously (diagonal `(+K, +K)` seam) | `2K` |

`R = (num2K ≥ 2) ? 2K : K` is the Phase0 outer-loop bound, decompile-confirmed as the `if (this+192 >= 2) v19 = v18` cmov in `GetPhase1ReplicaGroups` (`v18 = 2K = obj+190`, `v19 = K = obj+191`). For `K_K_2K` the single doubled axis carries the whole seam; for `K_2K_2K` the two passes are diagonally offset across *both* doubled axes, so each `2K` axis carries one length-`K` segment of the ring.

### Per-orientation fold table (`arg=0`, all three orientations)

The closed form expands to this table; `var_Y = j`, `var_X = i`, `var_Z = k`, and `seam` uses the `K`-axis's own loop var as `t`:

```text
| K-axis is | t | coord(Y)         | coord(X)         | coord(Z)         |
|-----------|---|------------------|------------------|------------------|
| Y (dim0)  | j | j mod K          | (i+seam) mod 2K  | (k+seam) mod 2K  |   K_2K_2K (Y short)
| X (dim1)  | i | (j+seam) mod 2K  | i mod K          | (k+seam) mod 2K  |   K_2K_2K (X short)
| Z (dim2)  | k | (j+seam) mod 2K  | (i+seam) mod 2K  | k mod K          |   K_2K_2K (Z short)
```

For `K_K_2K` (`num2K = 1`) only the single `2K` axis row-cell gets the `+seam` offset; the other axis is the short `K`-axis (`var mod K`) and the third is a plain `K`-extent axis carried without a seam. The table above is the `num2K = 2` (`K_2K_2K`) form where both non-`K` axes are doubled.

### Worked trace — `K_2K_2K`, `K = 2` (so `2K = 4`), `K`-axis = Y

A reimplementer can self-check a fold against this trace. The slice is `(Y, X, Z) = (2, 4, 4)`, the `K`-axis is `Y` (`t = j`), and a single Phase0 reduce-scatter ring fixes `(i, k)` and walks the ring member `j = 0..2K-1 = 0..3`:

```text
fix the plane cell (i, k) = (1, 2); walk j = 0..3:
  j=0:  seam=(0>=2?2:0)=0 ; cY=0%2=0 ; cX=(1+0)%4=1 ; cZ=(2+0)%4=2  -> chip (0,1,2)
  j=1:  seam=(1>=2?2:0)=0 ; cY=1%2=1 ; cX=(1+0)%4=1 ; cZ=(2+0)%4=2  -> chip (1,1,2)
  j=2:  seam=(2>=2?2:0)=2 ; cY=2%2=0 ; cX=(1+2)%4=3 ; cZ=(2+2)%4=0  -> chip (0,3,0)
  j=3:  seam=(3>=2?2:0)=2 ; cY=3%2=1 ; cX=(1+2)%4=3 ; cZ=(2+2)%4=0  -> chip (1,3,0)
```

The ring's first pass (`j = 0,1`) walks the short `Y`-axis at `(X,Z) = (1,2)`; the second pass (`j = 2,3`) jumps `+K = +2` on **both** `X` and `Z` (the diagonal seam) and walks `Y` again at `(X,Z) = (3,0)`. The four members are four distinct chips — a proper `2K = 4`-chip reduce-scatter ring — and the seam splits the ring across the two halves of each doubled axis (`X: 1↔3`, `Z: 2↔0`). For `K_K_2K` the seam would touch only one of `X`/`Z`; the other doubled-form axis would stay short.

> **NOTE —** the fold is **emulation-verified** for the `K_2K_2K` (`num2K=2`, `arg=0`) case: zero mismatches against the instruction-level disassembly over `K ∈ {2,3,4}`, all `(i,j,k)`, all three orientations; every produced coordinate is in-bounds (`coord_Y < Y`, `coord_X < X`, `coord_Z < Z`); and every Phase0 group `(i,k)` contains `2K` distinct chips — a proper reduce-scatter ring. The `K_K_2K` (`num2K=1`) single-axis seam is the contrast case documented on [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md).

> **QUIRK —** the `(arg=1)` and `(arg-other)` branches of the dispatch are *structurally distinct* folds (a different cmov chain at `0x1c893535` / `0x1c89359e`), but they are unreachable in v0.0.40: `GetPerColorShardIdTable` (`0x137d2d80`) fatal-errors any shard count `≥ 2` with `"3D twisted torus weight update sharding algorithm currently supports only 1-phase sharding."`, so `arg` is always `0`. The `arg=1` fold would only fire under a future multi-shard twisted weight-update. Confidence: LOW — its closed form is not reduced. See [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md).

---

## 4. The megacore even/odd split per fold

Each shape's fold produces a `2K`-member reduce-scatter ring (Phase0) and a `K×R` plane to all-gather over (Phase1). The **megacore even/odd split** is applied in Phase1 and pairs with whatever fold produced the plane. It is keyed by **logical-device count**, not by megacore mode directly.

`GetPhase1ReplicaGroups` (`0x137d3de0`) sets the group count to `2K · LogicalDevicesPerChip(0)`:

```c
function GetPhase1ReplicaGroups(target, dev_assign, arg, ...):   // 0x137d3de0
    UpdateMinMaxDims(target)                          // K=obj+191, 2K=obj+190, num2K=obj+192
    GetPhysicalToLogicalMapping3D(...)                // [Y][X][Z] -> {core0, core1}
    R          = (num2K >= 2) ? 2K : K                // cmov: if (obj+192 >= 2) R = 2K
    cores      = Target::CoresPerChip(target, 0)      // 0x1d615b40
    n_groups   = 2K * Target::LogicalDevicesPerChip(target, 0)   // 0x20ad3020; imul
    for m in 0 .. 2K-1:                               // m = the long-axis plane index
        even = 2*m ; odd = 2*m + 1
        for i in 0 .. R-1:
          for k in 0 .. K-1:
            pair = GetReplicaPair3DOnTwistedTorus(map, dims, 2K, K, num2K, arg, i, m, k)
            // pair.first = core0 device, pair.second = core1 device
            if Megacore() && (cores == 1 || secondary_cores <= 1):
                ... (megacore split path, rare)
            elif !Megacore() && cores != 1:           // NON-megacore 2-core
                group[even].add(pair.first)           // core0 -> even group 2m
                group[odd ].add(pair.second)          // core1 -> odd  group 2m+1
            else:                                     // megacore OR single core
                group[m].add(pair.first)              // both cores -> group m
```

`LogicalDevicesPerChip(0)` (`0x20ad3020`) is `Megacore() ? 1 : CoreCount()` — a megacore chip reports **one** logical device, a non-megacore 2-core chip reports **two**. So the group count is:

| Mode | `LogicalDevicesPerChip` | Phase1 groups | Append routing |
|---|---|---|---|
| megacore | 1 | `2K` | group `m` (both cores, one logical device) |
| non-megacore, 1 core | 1 | `2K` | group `m` |
| non-megacore, 2 cores | 2 | `4K` | core0 → group `2m` (even), core1 → group `2m+1` (odd) |

The even/odd split (`group[2m]` / `group[2m+1]`, decompile-confirmed as `v90 = 2*v26` / `v91 = 2*v26 + 1` written into the proto's repeated field) is the **non-megacore 2-logical-device** case. The two cores then all-gather over disjoint plane halves, so each core's AG traffic uses a distinct slice of the plane's ICI links — balancing the doubled-plane bandwidth across the two logical devices. A **megacore** chip collapses its two physical cores to one logical participant and uses group `m` (no split), because the megacore fusion machinery has already split the *workload* across the two cores inside one logical device (see [Megacore Collective Fusion](../collectives/megacore-fusion.md)).

The split is **orthogonal to the shape**: all three shapes produce a Phase1 plane, and the same `2K · LogicalDevicesPerChip` group count applies to each. What the shape changes is the plane's extent — `R = K` for `K_K_2K`, `R = 2K` for `K_2K_2K` — which sets the inner `i`-loop bound, not the group count.

> **NOTE —** the Phase1 even/odd split is the **non-megacore** 2-core case (`LogicalDevicesPerChip = CoreCount = 2 → 4K` groups), *not* a megacore-mode behaviour; megacore collapses to 1 logical device and uses group `m` (`2K` groups). The group count `2K · LogicalDevicesPerChip` is the `imul` at `0x137d3eb1`, and `LogicalDevicesPerChip` resolves through the `Megacore() ? 1 : CoreCount` getter at `0x20ad3020`. Phase0 (reduce-scatter) *always* co-groups both cores of a chip into the same group regardless of mode (both proto appends write the same group pointer). The byte-exact gate is on [Megacore Even/Odd Split](megacore-even-odd.md).

> **NOTE —** the decompiled gate has one extra wrinkle the table abstracts: the megacore branch can itself reach the split path when a *secondary*-core-count field (`[topology+0x7c]`/`+0x124`) is `> 1`. In the common megacore configuration that field is `≤ 1`, so megacore uses group `m`. The reimplementation-grade rule is "split iff the chip presents 2 logical devices"; the byte-level gate-jump senses are deferred to [Megacore Even/Odd Split](megacore-even-odd.md).

---

## 5. Why each shape folds the way it does

The fold geometry is forced by the requirement that the Phase0 reduce-scatter ring be a `2K`-chip cycle whose two halves do not contend for the same ICI links — the same dateline argument as a 1-D bidirectional ring.

**`K_K_2K` (one doubled axis).** The slice has one short axis and one doubled axis (the third axis is also short). The `2K` ring must live on the single doubled axis, but a flat `0..2K-1` walk folds back on itself and deadlocks. The fold instead walks the short `K`-axis (`K` chips), jumps `+K` along the doubled axis, and walks the `K`-axis again — placing the ring's two `K`-segments on the two halves of the one doubled dimension. `R = K`, because the orthogonal plane the all-gather sweeps is `K`-wide.

**`K_2K_2K` (two doubled axes).** Two axes are doubled and one is short. A single doubled axis carrying the entire `2K` ring would leave the second doubled axis's ICI bandwidth idle and overload the first. The fold therefore applies the seam to **both** doubled axes at once — the diagonal `(+K, +K)` offset — so each of the two `2K` axes carries one length-`K` segment of the ring. This balances the doubled-axis ICI bandwidth across both physical axes. `R = 2K`, because the orthogonal plane spans a full doubled axis.

**`K_2K_NK`.** A genuinely `n`-fold-doubled long axis cannot form a balanced `2·(n·K)`-aware ring inside the `num2K ∈ {1,2}` fold, so the collective never sees it as `n > 2`; the route generator (`TwistedTorusTopology`) handles the literal `nK` distance geometry, and the collective only ever folds the `n = 2` reduction.

The megacore split pairs with the plane, not the ring: the reduce-scatter ring is a physical ICI cycle on the doubled axis, where both cores of a chip sit at the same ring position and share the chip's ICI ports — so splitting them buys nothing and Phase0 always co-groups them. The all-gather plane, by contrast, has distinct link sets per logical device, so two logical devices (non-megacore 2-core) split into disjoint even/odd plane halves to use both link sets.

---

## 6. Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TwistedTorusND::UpdateMinMaxDims` | `0x137d0260` | `K`/`2K` reduction + the two shape CHECKs + `(num2K, numK)` counts | HIGH (CHECK strings verbatim) |
| `GetReplicaPair3DOnTwistedTorus` | `0x1c893400` | the per-shape coordinate fold (dispatch on `num2K`, `arg`); `num_max_dims == 2` gate | HIGH (fold emulation-verified for `num2K=2`) |
| `TwistedTorusND::GetPhase1ReplicaGroups` | `0x137d3de0` | Phase1 AG-over-plane groups + megacore even/odd split | HIGH (group-count `imul`, split appends verified) |
| `TwistedTorusND::GetPerColorShardIdTable` | `0x137d2d80` | 1-phase-only gate (forces `arg = 0`) | HIGH |
| `TpuTopology::LogicalDevicesPerChip` | `0x20ad3020` | `Megacore() ? 1 : CoreCount()` — keys the split | HIGH |
| `Target::CoresPerChip` | `0x1d615b40` | `int32[topology+0x7c + coreType*12]` | HIGH |
| `CreateTpuSliceTopology` | `0x1ff939c0` | emits the verbatim three-shape error string | HIGH (string verified) |
| `GetReplicaPair3D arg==1 fold` | `0x1c893535` | distinct `≥2-phase` seam | LOW (located; CHECK-gated unreachable in v0.0.40) |
| `TwistedTorusTopology::GetTiebreak` | `0x20b41320` | routing-side literal `nK` distance/tiebreak | MEDIUM (located; n-dependent math not decoded) |

---

## 7. What Was Not Resolved

- **The `arg=1` (`≥2-phase`) folds.** `GetReplicaPair3DOnTwistedTorus`'s `arg==1` entry (`0x1c893535`) is a structurally distinct seam (a 2nd-shard partition), but it is unreachable in v0.0.40 (the `GetPerColorShardIdTable` 1-phase gate). Its closed form is not reduced. LOW. See [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md).
- **The routing-side literal `nK` math.** `TwistedTorusTopology::GetTiebreak` / `GetDistanceFromOrigin` are the only place the `n > 2` long axis is genuinely distinct; the `n`-dependent distance formula was not decoded. MEDIUM. See [Get Tiebreak](get-tiebreak.md).
- **The SparseCore variant's per-shape split.** `TwistedTorusTopologyInfo` (`0x133e1980`, `sparse_core::collective`) reads `CoresPerChip(coreType=0)` only; whether SC Phase groups split per logical device differently was not traced. MEDIUM. See [SC-Side Twist](sc-side-twist.md).
- **The exact megacore-split gate jumps.** The decompiled gate's secondary-core-count condition (`[topology+0x124]`) is abstracted here to "2 logical devices → split"; the byte-level jump senses are on [Megacore Even/Odd Split](megacore-even-odd.md).

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Overview](overview.md) — the subsystem map and the shape-fold catalog summary this page details
- [Twist Predicate & Orientation](twist-predicate-orientation.md) — the `max == 2·min` predicate and the orientation/`K`-axis selection
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the byte-level `+K`-mod-`2K` fold and the `num_max_dims == 2` gate
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the per-color seam phases that apply the fold to the ring neighbours
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — Phase0 RS along `2K`, Phase1 AG over the plane the megacore split pairs with
- [Megacore Even/Odd Split](megacore-even-odd.md) — the byte-exact `LogicalDevicesPerChip`-keyed Phase1 split gate
- [Get Tiebreak](get-tiebreak.md) — the routing-side literal `nK` (`K_2K_NK`) distance/tiebreak handling
- [SC-Side Twist](sc-side-twist.md) — the SparseCore `TwistedTorusTopologyInfo` variant

**Sibling sections**
- [Megacore Collective Fusion](../collectives/megacore-fusion.md) — the workload-level megacore decomposition that lets a megacore chip act as one logical device
- [back to index](../index.md)
