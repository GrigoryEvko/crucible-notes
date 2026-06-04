# Twisted Torus — Section Map

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); all addresses are VMA. Every symbol below is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

This page is the **map of the twisted-torus subsystem** — the geometry libtpu uses when one ICI torus axis is exactly twice the length of the short axis, and a plain rectangular ring on that doubled axis would deadlock. A *twisted torus* folds the doubled (`2K`) axis back on itself with a **dateline twist**: instead of a flat ring of `2K` chips on one physical axis, the collective walks the short `K`-axis, jumps `+K` along the long axis, and walks the `K`-axis a second time — so the `2K` ring is split into two `K`-segments that live on the two halves of the doubled dimension. The twist (the `+K`-mod-`2K` seam) is what breaks the cyclic dependency that a folded torus would otherwise carry, the same trick a dateline buys on a 1-D bidirectional ring.

The subsystem has **two structurally distinct halves that a reimplementer must keep apart**, because they live in different namespaces and answer different questions:

1. The **collective-strategy half** — `xla::jellyfish::TwistedTorusND` — builds the per-color reduce-scatter / all-gather *ring schedule* and the HLO `ReplicaGroup` device lists for a twisted slice. This is the consumer of the [collective-algorithm picker](../collectives/strategy-nd-picker.md): when the picker's C-ii branch detects a twisted shape it constructs a `TwistedTorusND`. This half owns `BuildStrategy`, the `K`/`2K` shape classifier, the four seam builders, the two-phase replica-group construction, and the megacore even/odd split.
2. The **routing half** — `accel_ssw::deepsea::slice_builder::TwistedTorusTopology` — answers "given (src, dst) on a twisted slice, which link hops" for the static route table. It owns `InitRouteSolution`, `GetStaticPath`, `GetDistance`, and the `GetTiebreak` rule that the `k*k*2k`/`k*2k*2k` route caches key on. It is documented as a sibling of the [routing section](../routing/overview.md); this page locates it but does not duplicate its derivation.

This page documents (a) **what a twisted torus is** and the dateline twist that justifies it; (b) the **`TwistedTorusND` class family** and where each member sits; (c) the **`BuildStrategy` phase order** — how the per-color ring is built then the seam phases overwrite the `K`/`2K`-axis neighbours; and (d) the **shape-fold catalog** (`K_K_2K` / `K_2K_2K` / `K_2K_NK`) and the **megacore even/odd split**. Each algorithm has its own twist page; this page links them and does not re-derive their byte-level math.

For reimplementation, the contract of the subsystem is:

- **The shape gate.** A slice is twisted only when `max_dim == 2·min_dim` and every axis is either the short `K` or the long `2K` (`UpdateMinMaxDims` enforces both as fatal `CHECK`s). The `(num-2K-axes, num-K-axes)` counts classify the slice into one of three `TwistedTorusShape` enum values.
- **The class split.** `TwistedTorusND` (jellyfish, collective emission) vs `TwistedTorusTopology` (slice_builder, route table) are separate types in separate namespaces; both describe the same physical twist but produce different artifacts.
- **The seam.** The `+K`-mod-`2K` fold (`UpdateNeighborsKTo2K` and friends) is applied to the doubled axis (one axis for `K_K_2K`, both axes diagonally for `K_2K_2K`) per color, per ring-dimension phase.
- **The two-phase collective.** A twisted all-reduce is Phase0 reduce-scatter along the `2K` ring, then Phase1 all-gather over the orthogonal `K×R` plane — single-shard only.

| | |
|---|---|
| **Collective-strategy class** | `xla::jellyfish::TwistedTorusND` (ctor `0x137d0040`, vtable `0x219242a0`) |
| **Routing-side class** | `accel_ssw::deepsea::slice_builder::TwistedTorusTopology` (`InitRouteSolution` `0x20b3f7c0`) |
| **SparseCore variant** | `xla::tpu::sparse_core::collective::TwistedTorusTopologyInfo` (`0x133e1980`) |
| **Picker entry (constructs it)** | `BaseStrategyND::SelectNDStrategy` C-ii branch — [picker page](../collectives/strategy-nd-picker.md) |
| **Twist predicate** | `max_dim == 2·min_dim` (`UpdateMinMaxDims` `0x137d0260`, `CHECK_EQ` `0x137d037a`) |
| **Shape enum** | `TwistedTorusShape {UNSPECIFIED, K_K_2K, K_2K_2K, K_2K_NK}` |
| **BuildStrategy** | `TwistedTorusND::BuildStrategy` `0x137d0c00` (base ND ring + 3 seam phases) |
| **2-phase collective** | `GetPhase0ReplicaGroups` `0x137d3560` (RS along 2K) · `GetPhase1ReplicaGroups` `0x137d3de0` (AG over plane) |
| **Coordinate fold** | `GetReplicaPair3DOnTwistedTorus` `0x1c893400` (the `+K`-mod-`2K` seam) |
| **Confidence** | HIGH (decompile-verified class family, `UpdateMinMaxDims` CHECK strings, `BuildStrategy` phase dispatch, `num_max_dims == 2` CHECK, shape string) unless a row/callout says otherwise |

---

## 1. What a twisted torus is

A 3-D ICI slice is a rectangular grid of chips with wrap-around links on each axis (a torus). Collectives ride that torus as per-axis rings: a reduce-scatter sends each chip's partial to its ring neighbour, accumulating around the loop. On a rectangular torus the ring on each axis is just the `N` chips of that axis in order, closed by the wrap link.

The problem the twist solves arises when one axis has extent `2K` while the others have extent `K` (`K = min`, `2K = max`). The natural `2K`-chip ring on the long axis is a *folded* loop: it traverses the long axis forward and the wrap link folds it back, and on certain slice shapes the forward and return halves of that loop contend for the same physical links in a way that forms a cyclic buffer-dependency — a routing deadlock. This is the torus analogue of the classic deadlock on a unidirectional ring with no dateline.

The **twisted torus** breaks the cycle by re-threading the `2K` ring so its two halves live on *different physical link sets*. Concretely (the `K_K_2K` case, `GetReplicaPair3DOnTwistedTorus` num2K==1 fold):

```text
ring member index j = 0 .. 2K-1   (the 2K reduce-scatter ring)
   coord(K-axis)   =  j mod K              ── walk the short K-axis
   coord(2K-axis)  = (j >= K ? K : 0) + …  ── second pass offset by +K on the long axis
```

The ring walks the short `K`-axis once (`j = 0..K-1`), then **jumps `+K` along the long axis** and walks the `K`-axis a second time (`j = K..2K-1`). The two `K`-segments occupy the two halves of the doubled dimension, so the seam (the `+K` jump, taken mod `2K`) is the dateline: the cyclic dependency is cut at exactly one point per ring. For the `K_2K_2K` case the seam is applied to **both** doubled axes simultaneously — a diagonal `(+K, +K)` offset — so each of the two `2K` axes carries one `K`-segment and the doubled-axis ICI bandwidth is balanced across both. The seam math is on [Twist Predicate & Orientation](twist-predicate-orientation.md) and the per-color application on [BuildStrategy](buildstrategy.md).

> **NOTE —** the twist is a property of the *logical ring layout*, not of the hardware: the same physical 3-D torus is used. What the twist changes is which physical chip each ring step lands on (`GetReplicaPair3DOnTwistedTorus`) and which ICI link each neighbour edge uses (the seam builders in `BuildStrategy`). On the routing side, the same twist re-biases the `GetDistance` / `GetTiebreak` minimum-hop path so the route table avoids the would-be-deadlocked link cycle. See [Get Distances](../routing/get-distances.md) for how the twist adjusts the torus-reduced distance vector.

---

## 2. The `TwistedTorus` class family

The name "twisted torus" attaches to **two** unrelated C++ types plus a SparseCore variant. Conflating them is the single easiest mistake; they share the geometry concept and nothing else.

```text
xla::jellyfish::TwistedTorusND                       ── COLLECTIVE STRATEGY (this section)
  derives from StrategyND; constructed by SelectNDStrategy's C-ii twisted branch
  ctor                       0x137d0040   (vtable 0x219242a0)
  UpdateMinMaxDims           0x137d0260   ── K/2K classifier + the twist CHECKs
  InitColorDimensions        0x137d0800   ── color_dims[6][3] cyclic fill (or degraded remap)
  BuildStrategy              0x137d0c00   ── base ND ring + per-color seam phases
  seam builders:  UpdateNeighborsKTo2K  0x137d24c0 · UpdateNeighbors2KToK 0x137d29c0
                  UpdateOrdinal2K       0x137d2c60 · UpdateOrdinal2KToK   0x137d28c0
  2-phase groups: GetPhase0ReplicaGroups 0x137d3560 · GetPhase1ReplicaGroups 0x137d3de0
                  GetPhase0Cores         0x137d6de0 · GetPhase1Cores        0x137d6ec0
  GetPerColorShardIdTable    0x137d2d80   ── 1-phase-only gate
  ToStringName               0x137d6da0   ── literally emits "TwistedTorusND"

accel_ssw::deepsea::slice_builder::TwistedTorusTopology   ── ROUTE TABLE (routing section)
  InitRouteSolution          0x20b3f7c0
  GetStaticPath              0x20b407c0 · GetDistance 0x20b408e0 · GetDistances 0x20b420e0
  GetTiebreak                0x20b41320 · GetDistanceFromOrigin 0x20b42980
  UnboundedWalk / Walk / Format / ToProto / Clone …

xla::tpu::sparse_core::collective::TwistedTorusTopologyInfo   ── SPARSECORE collective
  TryCreateTwistedTorusTopologyInfo  0x133e1980 · ConstructTwistedViews 0x133e1ea0
```

| Class | Namespace | Produces | Where documented |
|---|---|---|---|
| `TwistedTorusND` | `xla::jellyfish` | per-color ring schedule + HLO `ReplicaGroup` lists | this section |
| `TwistedTorusTopology` | `accel_ssw::deepsea::slice_builder` | static per-link route table for a twisted slice | [routing section](../routing/overview.md) |
| `TwistedTorusTopologyInfo` | `xla::tpu::sparse_core::collective` | the SparseCore twisted collective views | [SC-Side Twist](sc-side-twist.md) |

> **GOTCHA —** the literal `K_2K_NK` (n > 2) geometry is handled only by the **routing-side** `TwistedTorusTopology` (its `GetTiebreak` / `GetDistance` walk an n-fold-doubled axis). The jellyfish collective `TwistedTorusND` never sees `n > 2`: `UpdateMinMaxDims`'s `max_dim == 2·min_dim` CHECK and `GetReplicaPair3DOnTwistedTorus`'s `num_max_dims == 2` CHECK both fatal-error on any axis that is not exactly `K` or `2K`. So a `K_2K_NK` slice folds through the same `num2K ∈ {1,2}` machinery as `K_K_2K`/`K_2K_2K` in the collective half, and the literal `nK` only matters to the route generator. See [Shape Folds](shape-folds.md).

---

## 3. The shape gate and the shape-fold catalog

Before any ring is built, `TwistedTorusND::UpdateMinMaxDims` (`0x137d0260`) reduces the three torus extents to two scalars and two counts, with two fatal `CHECK`s that *define* what "twisted" means:

```text
min_dim_ (K)        = the short axis size        ([obj+0x5f8])
max_dim_ (2K)       = the long axis size         ([obj+0x5f0])
CHECK:  max_dim_ == 2 * min_dim_                  ("Max. dim size should be 2 times the min. in a twisted torus")
num_max_dims_       = count of axes equal to 2K   ([obj+0x600])   ── decompiled vpcmpeqq
num_min_dims_       = count of axes equal to K    ([obj+0x608])   ── decompiled vpcmpeqq
CHECK:  num_min_dims_ + num_max_dims_ == num_dims_ (==3)   ("Dimension sizes should either be maximum or minimum")
```

Both CHECK strings are decompile-verified verbatim (`MakeCheckOpString` at `0x137d0260` body). The `(num-2K-axes, num-K-axes)` pair then classifies the slice into one of the three `TwistedTorusShape` enum values. The enum and the slice-shape error string are pinned to the proto descriptor and the slice_builder `.rodata` string `"TPU twisted torus only supports k*k*2k and k*2k*2k and k*2k*nk slice shapes."` (confirmed in `CreateTpuSliceTopology` @ `0x1ff939c0`).

| Shape | `TwistedTorusShape` | `num-2K` / `num-K` | Seam applied to | Phase0 `R` |
|---|---|---|---|---|
| `K, K, 2K` | `TWIST_SHAPE_K_K_2K` | 1 / 2 | the single `2K` axis (one-axis seam) | `K` |
| `K, 2K, 2K` | `TWIST_SHAPE_K_2K_2K` | 2 / 1 | **both** `2K` axes (diagonal `(+K,+K)` seam) | `2K` |
| `K, 2K, nK` | `TWIST_SHAPE_K_2K_NK` | — | collective folds it as the n=2 case; literal `nK` only in routing | — |

`R = (num-2K-axes ≥ 2) ? 2K : K` — i.e. `R = K` for a single doubled axis, `R = 2K` for two. `R` is the outer-loop bound of the Phase0 replica-group construction (§5). The per-shape coordinate-fold tables (the unified `K_2K_2K` per-orientation closed form, the `K_K_2K` single-axis fold, and the `K_2K_NK` reduction) are on [Shape Folds](shape-folds.md) and [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md).

> **NOTE —** the shape classifier reads the three obj dim fields in the order `[obj+0xb8]=Y`, `[obj+0xc0]=X`, `[obj+0xc8]=Z` (the `UpdateMinMaxDims` load order). The loop-variable↔axis map used throughout the collective half is `Y↔j`, `X↔i`, `Z↔k`, confirmed byte-exact from both `GetPhase0ReplicaGroups` and `GetPhase1ReplicaGroups` `GetReplicaPair3D` call-site push order.

---

## 4. The `BuildStrategy` phase order

`TwistedTorusND::BuildStrategy` (`0x137d0c00`) turns the `color_dims[6][3]` table (filled by `InitColorDimensions` `0x137d0800`) into the per-color ring neighbour/ordinal tables the all-reduce emitter consumes. It runs in two stages: a **base rectangular ND ring** build (reused from `StrategyND`), then a **per-color seam overwrite** that re-wires the `K`/`2K`-axis edges with the twist fold. The page that owns the byte-level derivation is [TwistedTorusND::BuildStrategy](buildstrategy.md); the phase order is summarized here.

**Stage 1 — base ND ring (`0x137d0e62..0x137d13ad`).** Same `[obj+0xa8]==1` ND-ring-vs-1D-ring gate as the base `StrategyND::BuildStrategy`. Per color, per active dimension it computes the ring ordinal (`ComputeOrdinal` `0x137c5300`), then the forward/backward neighbours via `Torus2DevicePhase0Neighbor` (`0x137c57a0`) on the no-wrap fast path or `MeshStrideNPhasekNeighbor` (`0x137c5cc0`) with an inline `ModuloRingSize` fold otherwise, depositing into the per-color clockwise / counter-clockwise neighbour buffers. This is the *unwrapped* torus ring; the seam phases then overwrite the doubled-axis entries.

**Stage 2 — the per-color, per-phase seam (`0x137d168a..0x137d1c68`).** The outer loop is color `c` (count `≤6`); the inner loop is the **ring-dimension phase** `p ∈ {0,1,2}`. For each `(c, p)` the loop reads `color_dims[c][p]` (the physical axis assigned to that ring dimension of that color) and dispatches on whether that axis is the `K`-axis or a `2K`-axis:

| Phase `p` | `color_dims[c][p]` is the `K`-axis | `color_dims[c][p]` is a `2K`-axis |
|---|---|---|
| 0 | `UpdateNeighborsKTo2K` + `UpdateOrdinal2KToK` | `UpdateNeighbors2KToK` + `UpdateOrdinal2K` |
| 1 | `UpdateNeighborsKTo2K` + `UpdateOrdinal2KToK` | `UpdateNeighbors2KToK` + `UpdateOrdinal2K` |
| 2 | `UpdateNeighborsKTo2K` + `UpdateOrdinal2KToK` | `UpdateNeighbors2KToK` |

The **phase index `p` is the third argument to every seam-builder call**, decompile-verified: the three call groups pass `0`, `1`, `2` respectively (e.g. `UpdateNeighborsKTo2K(…, 0, …)` → `(…, 1, …)` → `(…, 2, …)`). The `K`-axis column is seamed **K→2K** (its two length-`K` rings join end-to-end into one length-`2K` ring via `UpdateNeighborsKTo2K` + the `2KToK` ordinal); each `2K`-axis column is seamed **2K→K** (`UpdateNeighbors2KToK` + the `2K` ordinal). Because each color's three `color_dims` columns are a cyclic rotation of `{0,1,2}` (filled by `InitColorDimensions`), each color gets exactly one `K→2K` seam phase and the remaining `2K→K` phase(s), and consecutive colors rotate which physical axis carries the seam — balancing the doubled-axis ICI bandwidth across colors.

```text
BuildStrategy(target, lrb):
    UpdateMinMaxDims(target)            // K, 2K, num-2K, num-K  (§3)
    InitColorDimensions(target)         // color_dims[6][3] = cyclic perm mod 3 (or degraded remap)
    identify K-axis index + the one/two 2K-axis indices
    for color c in 0..num_colors-1:     // ≤ 6
        for phase p in 0,1,2:           // p IS the ring-dimension column
            base ND ring neighbours already deposited (Stage 1)
            axis = color_dims[c][p]
            if axis == K-axis:   UpdateNeighborsKTo2K(c, p, …) ; UpdateOrdinal2KToK(c, p, …)
            else  (2K-axis):     UpdateNeighbors2KToK(c, p, …) ; UpdateOrdinal2K(c, p, …)
```

> **NOTE —** `InitColorDimensions` first calls `UseResilientAlgorithmTwistedTorus` (`0x1c894fc0`, the `env[0x1116]` + `GetDegradedAxis ≠ −1` gate shared with the [picker](../collectives/strategy-nd-picker.md)). On a degraded axis it tail-calls the base `InitColorDimensionsDegraded` (`0x137c6580`) — the same `[6][3]` remap that demotes the dead axis to the inner ring column. The non-resilient fill is `color_dims[c][d] = (c·stride + d) mod 3`. The `.rodata` note `"twisted topology should have 3 pairs of colors"` confirms the 6-row (3×2) color structure.

---

## 5. The two-phase collective and the megacore split

A twisted-torus all-reduce is **two phases**: a reduce-scatter along the doubled `2K` ring, then an all-gather over the orthogonal plane. The HLO `ReplicaGroup` device lists for each phase are built by `GetPhase0ReplicaGroups` / `GetPhase1ReplicaGroups`; both first call `UpdateMinMaxDims` (§3) and build the physical→logical device map via `GetPhysicalToLogicalMapping3D` (`0x1c88a280`, a `[Y][X][Z]` table whose leaves are the two megacore-core logical IDs `{core0, core1}`), then walk the twist coordinate fold `GetReplicaPair3DOnTwistedTorus` (`0x1c893400`). Full derivation on [2-Phase Replica-Group Construction](replica-group-2phase.md).

| Phase | Group count | Group index | Members | Axis swept |
|---|---|---|---|---|
| 0 — reduce-scatter | `K·R` | `k·R + i` | `2K` (×2 if megacore), member index `j` | the long (`2K`) ring |
| 1 — all-gather | `2K` (or `4K` megacore-split) | `m` or `{2m, 2m+1}` | `R·K`, member index `(i,k)` | the plane ⟂ to the `2K` ring |

**Phase0** collects, for each `(i,k)`, the `2K` chips the twisted ring places at steps `j = 0..2K-1` — it *is* the reduce-scatter ring along the doubled axis. **Phase1** collects all `R·K` chips of the `m`-th long-axis slice (the `K×R` cross-section) — the all-gather over the plane orthogonal to the ring. The collective is **single-shard only**: `GetPerColorShardIdTable` (`0x137d2d80`) fatal-errors with `"3D twisted torus weight update sharding algorithm currently supports only 1-phase sharding."` (decompile-verified) on any shard count `≥ 2`.

**The megacore even/odd split** (Phase1 only) is keyed by **logical-device count**, not by megacore mode directly. `Phase1` group count is `LogicalDevicesPerChip(0) · 2K`, where `LogicalDevicesPerChip(0)` (`0x20ad3020`) `= Megacore ? 1 : CoreCount`:

| Mode | `LogicalDevicesPerChip` | Phase1 groups | Append routing |
|---|---|---|---|
| megacore | 1 | `2K` | group `m` (both cores, one logical device) |
| non-megacore, 1 core | 1 | `2K` | group `m` |
| non-megacore, 2 cores | 2 | `4K` | core0 → group `2m` (even), core1 → group `2m+1` (odd) |

So the **even/odd split is the non-megacore 2-logical-device case**, where the two cores all-gather over disjoint plane halves to balance the plane's ICI links; a **megacore** chip collapses its two physical cores to one logical participant and uses group `m` (no split). Phase0 (reduce-scatter) *always* co-groups both cores of a chip into the same group regardless of megacore mode (both proto appends write the same group pointer). The group count is `LogicalDevicesPerChip(0)·2K` with `LogicalDevicesPerChip` resolving through the `Megacore ? 1 : CoreCount` getter: `CoreCount = 2 → 4K` groups for the split, `1 → 2K` groups for megacore. The byte-exact gate and rationale are on [Megacore Even/Odd Split](megacore-even-odd.md); the workload-level megacore decomposition is on the collectives section's [Megacore Collective Fusion](../collectives/megacore-fusion.md).

---

## 6. Where the twist meets routing

The collective half (§2–§5) produces the *ring schedule* and *replica groups*; it does **not** decide which physical ICI links carry each hop. That is the routing half's job, and the twist re-enters there:

- `accel_ssw::deepsea::slice_builder::TwistedTorusTopology::GetDistance` (`0x20b408e0`) / `GetDistances` (`0x20b420e0`) compute the twist-adjusted torus-reduced distance vector — the `+K`-mod-`2K` seam biases the minimum-hop count on the doubled axis. See [Get Distances](../routing/get-distances.md).
- `GetTiebreak` (`0x20b41320`) is the rule the `k*k*2k` / `k*2k*2k` route caches key on when two equal-distance paths exist; the precomputed twisted route caches (`…_twisted.binarypb`) are the offline output of the WildFirst generator over twisted shapes. See [routing section](../routing/overview.md) and [Get Tiebreak](get-tiebreak.md).
- `TwistedTorusTopology::InitRouteSolution` (`0x20b3f7c0`) is the twisted-shape sibling of the resilient `InitRouteSolution`, producing the per-link route table for a twisted slice.

> **GOTCHA —** the two halves use different coordinate conventions and different "twist" anchors. The jellyfish `GetReplicaPair3DOnTwistedTorus` works in *replica-group / device-id* space and seams the `2K` ring at the `+K` step; the slice_builder `TwistedTorusTopology` works in *physical chip-coordinate* space and seams the route distance. A reimplementer must build both — the replica groups tell each core *who* it reduces with, the route table tells the DMA fabric *how the bytes get there*. Conflating them yields a schedule that knows its partners but not its links.

---

## 7. Function Map

| Function | Address | Role |
|---|---|---|
| `TwistedTorusND::TwistedTorusND` (ctor) | `0x137d0040` | constructed by `SelectNDStrategy` C-ii |
| `TwistedTorusND::UpdateMinMaxDims` | `0x137d0260` | `K`/`2K` classifier + the two twist CHECKs |
| `TwistedTorusND::InitColorDimensions` | `0x137d0800` | `color_dims[6][3]` cyclic fill / degraded remap |
| `TwistedTorusND::BuildStrategy` | `0x137d0c00` | base ND ring + per-color seam phases |
| `TwistedTorusND::UpdateNeighborsKTo2K` | `0x137d24c0` | `K→2K` neighbour seam |
| `TwistedTorusND::UpdateNeighbors2KToK` | `0x137d29c0` | `2K→K` neighbour seam |
| `TwistedTorusND::UpdateOrdinal2K` | `0x137d2c60` | `2K` ordinal fold |
| `TwistedTorusND::UpdateOrdinal2KToK` | `0x137d28c0` | inverse `2K→K` ordinal fold |
| `TwistedTorusND::GetPhase0ReplicaGroups` | `0x137d3560` | RS-along-`2K` group lists |
| `TwistedTorusND::GetPhase1ReplicaGroups` | `0x137d3de0` | AG-over-plane group lists + megacore split |
| `TwistedTorusND::GetPhase0Cores / GetPhase1Cores` | `0x137d6de0 / 0x137d6ec0` | per-phase core-ID vectors (cost model) |
| `TwistedTorusND::GetPerColorShardIdTable` | `0x137d2d80` | 1-phase-only gate (Unimplemented string) |
| `GetReplicaPair3DOnTwistedTorus` | `0x1c893400` | the `+K`-mod-`2K` coordinate fold; `num_max_dims == 2` CHECK |
| `GetPhysicalToLogicalMapping3D` | `0x1c88a280` | `[Y][X][Z] → {core0, core1}` map |
| `TwistedTorusND::ToStringName` | `0x137d6da0` | emits `"TwistedTorusND"` |
| `TwistedTorusTopology::InitRouteSolution` | `0x20b3f7c0` | routing-side twisted route table |
| `TwistedTorusTopology::GetTiebreak / GetDistance` | `0x20b41320 / 0x20b408e0` | route distance / tiebreak on a twisted slice |
| `TwistedTorusTopologyInfo::TryCreate…` | `0x133e1980` | SparseCore twisted collective |

---

## 8. What Was Not Resolved

- **`GetPhase0Cores` / `GetPhase1Cores` bodies.** The `ReplicaGroup` proto construction is decoded; the parallel `*Cores` device-ID vectors the cost estimator walks (`EstimatePhysicalLinksUsed` `0x1c8939c0`) were located but not transcribed. MEDIUM. See [2-Phase Replica-Group Construction](replica-group-2phase.md).
- **The routing-side `K_2K_NK` distance math.** `TwistedTorusTopology::GetTiebreak` / `GetDistanceFromOrigin` (`0x20b42980`) are the only place the literal `nK` (n > 2) long axis is genuinely distinct; the n-dependent distance formula was not decoded. MEDIUM. See [routing section](../routing/overview.md).
- **The `≥2-phase` (arg≥1) coordinate fold.** `GetReplicaPair3DOnTwistedTorus`'s arg==1 entry block (`0x1c893535`) is a structurally distinct seam, but it is CHECK-gated as unreachable in v0.0.40 (`GetPerColorShardIdTable` 1-phase gate). LOW. See [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md).
- **The SparseCore twisted views.** `TwistedTorusTopologyInfo::ConstructTwistedViews` (`0x133e1ea0`) — whether SC Phase groups split per logical device differently from the TensorCore path — was located but not traced. MEDIUM. See [SC-Side Twist](sc-side-twist.md).

---

## Cross-References

**Twist algorithms (this section)**
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the base ND ring + per-color `K↔2K` seam phase order, byte-level
- [Twist Predicate & Orientation](twist-predicate-orientation.md) — the `max == 2·min` predicate and the Orientation/Polarity dimension map
- [Shape Folds](shape-folds.md) — the `K_K_2K` / `K_2K_2K` / `K_2K_NK` coordinate-fold catalog
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the `+K`-mod-`2K` coordinate fold and the `num_max_dims == 2` gate
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — Phase0 RS along `2K`, Phase1 AG over the plane
- [Megacore Even/Odd Split](megacore-even-odd.md) — the `LogicalDevicesPerChip`-keyed Phase1 split
- [Get Tiebreak](get-tiebreak.md) — the route tiebreak rule on a twisted slice
- [SC-Side Twist](sc-side-twist.md) — the SparseCore `TwistedTorusTopologyInfo` variant

**Sibling sections**
- [SelectNDStrategy — the ND Collective-Algorithm Picker](../collectives/strategy-nd-picker.md) — the C-ii branch that constructs `TwistedTorusND`
- [On-Pod Collectives](../collectives/overview.md) — where the twisted strategy sits in collective lowering
- [Megacore Collective Fusion](../collectives/megacore-fusion.md) — the workload-level megacore decomposition
- [ICI Routing](../routing/overview.md) — the `TwistedTorusTopology` route-table family and the twist-adjusted `GetDistances`
- [Get Distances](../routing/get-distances.md) — the twist-adjusted torus-reduced distance vector
- [back to index](../index.md)
