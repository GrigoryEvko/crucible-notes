# SC-Side Twist

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); all addresses are VMA. Every symbol below carries a full C++ name in the binary and was cross-checked against the IDA decompile.*

## Abstract

This page documents the **SparseCore side of the twisted torus** — the three pieces a reimplementer needs that the TensorCore twist pages deliberately do not own:

1. **`sparse_core::collective::TwistedTorusTopologyInfo`** — the embedding-shard analog of the TensorCore `TwistedTorusND`. It accepts the same `K`/`2K` geometry and uses the same `R = (num-2K-axes ≥ 2) ? 2K : K` knob and the same `+K`-mod-`2K` seam, but expresses the twist as **two composable `TwistedView` objects** whose per-axis coordinate transforms are `std::function<long(long,long)>` closures, applied by a shared `ForEachPhase` fold engine — not as two `GetPhaseNReplicaGroups` loop nests.
2. **`TwistedTorusND::GetPhase0Cores` / `GetPhase1Cores`** — the per-phase **core-count** virtuals (the count-only twins of the TensorCore `GetPhaseNReplicaGroups` device lists). `GetPhase0Cores = 2K · LogicalDevicesPerChip`; `GetPhase1Cores = R`. The collective-fusion validators read these to check that an offloaded fusion's shard geometry matches the strategy.
3. **`EstimatePhysicalLinksUsed`** — the physical-ICI-link estimator. It walks each replica group's member chip coordinates, detects which torus axes the group spans, and returns the **sorted set of distinct directional `IciResource`s** (`1..6` = 3 dims × 2 directions) the collective traverses. Its `result.size()` is the "links" divisor the all-to-all / cross-module-all-reduce cost model divides bandwidth by.

The TensorCore-side `TwistedTorusND` *class family* (ctor, `BuildStrategy`, the seam builders, the megacore split) lives on [BuildStrategy](buildstrategy.md) and the device-list phases on [2-Phase Replica-Group Construction](replica-group-2phase.md); the `+K`-mod-`2K` coordinate fold itself is on [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md). The SparseCore offload **config emission** that *consumes* `TwistedTorusTopologyInfo` — the `ConstructConfigForCollectiveUniDirNDGroups<*>` builder and the `*OffloadConfig` proto family — is on [SC-Offload Config Builder](../collectives/sc-offload-config-builder.md). This page owns the SC topology struct, the phased-view enumeration, and the physical-link estimator.

Contract of the three pieces as observed in the binary:

- **The shape gate.** `TryCreateTwistedTorusTopologyInfo` accepts a twisted topology only when `max_dim_size % min_dim_size == 0` **and** `min_dims.size()` is in a `2:1` ratio with `max_dims.size()`. The ratio direction is the shape discriminant: `2·|min| == |max|` → `k_2k_2k`, `|min| == 2·|max|` → `k_k_2k`. **Exactly two shapes** — no literal `nK`.
- **The two views.** Each shape builds **two `TwistedView` objects** (the reduce-scatter view and the all-gather view) from a pool of **7 coordinate-fold closures**, each capturing `K`.
- **The fold.** `TwistedView::ForEachPhase` is a fixed **3-axis** fold (`CHECK size == 3`): for each axis it reads a permutation index and invokes the matching `std::function` closure on a `(coord_a, coord_b)` pair, producing physical `TpuDimensions`. The seam primitive is `(⌊j/K⌋·K + i) mod 2K`; the short-axis primitive is `i mod K`.
- **The link count.** `EstimatePhysicalLinksUsed` is a topology **set walk**, not a scalar span product: a group that spans more axes / both directions touches more distinct `IciResource`s, and `links = |set|`.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| SC topology class | `xla::tpu::sparse_core::collective::TwistedTorusTopologyInfo` (`operator new(0x100)`) |
| SC factory | `TryCreateTwistedTorusTopologyInfo @0x133e1980` → `StatusOr<unique_ptr<…>>` |
| SC view builder | `ConstructTwistedViews @0x133e1ea0` (two `unique_ptr<TwistedView>` push_backs) |
| SC fold engine | `TwistedView::ForEachPhase @0x133e17c0` (3-axis; `CHECK size == 3`) |
| SC fold closures | 7 × `std::function<long(long,long)>`, lambdas `0x133e4ae0..0x133e4dc0` |
| Source TU (SC) | `platforms/xla/sparse_core/offload_collective_config_builder.cc` |
| Core counts | `TwistedTorusND::GetPhase0Cores @0x137d6de0`, `GetPhase1Cores @0x137d6ec0` |
| Phase0 count | `[this+0x5f0]` (`2K`) · `LogicalDevicesPerChip(0)` |
| Phase1 count | `[this + (num2K<2)·8 + 0x5f0]` = `(num2K ≥ 2 ? 2K : K)` = `R` |
| Source TU (cores) | `all_reduce_strategies.h` (VLOG lines 792 / 800) |
| Link estimator | `xla::jellyfish::EstimatePhysicalLinksUsed @0x1c8939c0` → `vector<IciResource>` |
| Source TU (links) | `group_utils.cc` (cross-slice RetCheck line 1652) |
| Link encoding | 6 `IciResource`s `1..6` = `{X,Y,Z} × {dir0,dir1}`; `GetResourceFromIciResource @0x1c894c00` (`e-1+0xd` → slots `0xd..0x12`) |
| Cost consumer | `ComputeAllToAllCyclesHelper @0x130d02a0` (`IciResource` → torus-dim extent) |
| Confidence | HIGH (decompile-verified `TryCreate` RetCheck strings, `ForEachPhase` `CHECK==3` + `*0x10` dispatch, `GetPhaseNCores` field arithmetic, the 6 `IciResource` inserts + sorted-set return) unless a row/callout says otherwise |

---

## 1. `TwistedTorusTopologyInfo` — the SparseCore twist

The SparseCore embedding collective does not own a `TwistedTorusND`. It owns a sibling type, `xla::tpu::sparse_core::collective::TwistedTorusTopologyInfo`, that describes **the same physical twist** but is wired into the SparseCore offload-config builder rather than the dense HLO `ReplicaGroup` scheduler. It is the third member of the "twisted torus" name collision catalogued on [Twisted Torus — Section Map](overview.md) §2, and it is the one [SC-Offload Config Builder](../collectives/sc-offload-config-builder.md) §6 reaches through its `cmp $3` K/2K mesh-dim gate.

### 1.1 `TryCreateTwistedTorusTopologyInfo` — the validation + shape gate

```cpp
// xla::tpu::sparse_core::collective::TwistedTorusTopologyInfo::
absl::StatusOr<std::unique_ptr<TwistedTorusTopologyInfo>>
TryCreateTwistedTorusTopologyInfo(
    const std::vector<IciDim>& min_dims,   // K-side axes
    const std::vector<IciDim>& max_dims,   // 2K-side axes
    long                       min_dim_size, // = K
    long                       max_dim_size); // = 2K
```

The factory body (`@0x133e1980`) applies two gates before it constructs anything:

```text
TryCreateTwistedTorusTopologyInfo(min_dims, max_dims, K, 2K):
   // gate 1 — divisibility (RetCheck line 96):
   if (max_dim_size % min_dim_size != 0)               // = 2K % K
       return RetCheckFail("max_dim_size % min_dim_size == 0")
           << "Found unexpected twisted torus topology where
               max_dim_size is not divisible by min_dim_size."

   // gate 2 — the 2:1 size ratio (RetCheck line 102):
   if (2·min_dims.size() != max_dims.size()
       && min_dims.size() != 2·max_dims.size())
       return RetCheckFail("min_dims.size() == 2 * max_dims.size()")
           << "Twisted torus is only for k_2k_2k and k_k_2k topology."

   obj = operator new(0x100); TwistedTorusTopologyInfo::ctor(obj)   // §1.2
   return obj
```

The decompile splits the divisibility test into a 32-bit fast path (`(2K | K) >> 32 == 0` → 32-bit `idiv`) and a 64-bit `idiv` fall-back; both compute `2K % K`. The size gate reads `min_dims.size()` and `max_dims.size()` from `[vector+8]` (the `end` pointer minus `begin` is folded into the element count) and accepts **either** ratio direction.

> **[CONFIRMED]** Both RetCheck strings are byte-exact in the decompile: `"max_dim_size % min_dim_size == 0"` at `offload_collective_config_builder.cc:96`, `"min_dims.size() == 2 * max_dims.size()"` at line 102, with the two user-facing error messages (`"Found unexpected … not divisible …"`, `"Twisted torus is only for k_2k_2k and k_k_2k topology."`) following each via `StatusBuilder::operator<<`. The `operator new(0x100u)` allocation is present.

The **shape discriminant** is the ratio direction itself: `2·|min_dims| == |max_dims|` is the `k_2k_2k` shape (two doubled axes, one short axis), `|min_dims| == 2·|max_dims|` is the `k_k_2k` shape (one doubled axis, two short axes). This is the single difference from the routing-side `TwistedTorusTopology`, which additionally handles a literal `k_2k_nK` (`n > 2`); the SparseCore factory rejects anything outside the two `2:1` shapes (the same restriction the TensorCore `TwistedTorusND` enforces via its `max_dim == 2·min_dim` CHECK — see [overview](overview.md) §3).

> **[LOW]** The decompiled `TryCreate` calls the ctor with only the object pointer; the shape byte and the `(2K, R, K)` triple are populated **inside** the ctor (`@0x133e1b40`), not visible at the `TryCreate` call site. The ctor field layout (§1.2) is reconstructed from the working-note disassembly of the ctor body, not re-walked instruction-by-instruction here. The ratio-direction → shape mapping and the `R` knob are HIGH; the exact `[this+…]` offsets of the triple are MEDIUM.

### 1.2 The object layout and the `(2K, R, K)` triple

The ctor (`@0x133e1b40`) builds a `0x100`-byte object whose live region is:

| Offset | Field | Meaning |
|--------|-------|---------|
| `+0x00` | `vector<IciDim> min_dims` | the `K`-side axes (memcpy of arg) |
| `+0x18` | `vector<IciDim> max_dims` | the `2K`-side axes (memcpy of arg) |
| `+0x30` | `long min_dim_size` | `K` |
| `+0x38` | `long max_dim_size` | `2K` |
| `+0x40` | ptr → `{2K, R, K}` | the parameter triple, `R = (shape ? 2K : K)` |
| `+0x58` | `vector<bool>` (3) | "is-doubled-axis" mask |
| `+0x70` | `char shape` | `0 = k_k_2k`, `1 = k_2k_2k` |
| `+0x78..0xe0` | 7 × `std::function<long(long,long)>` | the fold closures (each captures `K`) |

The `R` knob here is **identical** to the TensorCore one: `R = (num-2K-axes ≥ 2) ? 2K : K`. On the TensorCore side it is `[obj+0x5f0]=2K`/`[obj+0x5f8]=K` selected by `[obj+0x600]=num2K` (see §2 and [overview](overview.md) §3); on the SparseCore side it is the `shape` byte selecting `2K` vs `K` into the triple's middle slot. Same geometry, re-expressed.

### 1.3 `ConstructTwistedViews` — the two phase views

`ConstructTwistedViews @0x133e1ea0` dispatches on the `shape` byte and, for **either** shape, builds **two** `TwistedView` objects, push-backed into a `vector<unique_ptr<TwistedView>>` — the reduce-scatter view and the all-gather view (the SparseCore analog of the TensorCore Phase0/Phase1 split). Each `TwistedView` carries a `vector<vector<shared_ptr<function<long(long,long)>>>>` drawn from the 7 ctor closures, plus the `is-doubled-axis` mask tagging which of the 3 axes is the doubled (`2K`) axis for that view.

```text
ConstructTwistedViews(this):
   if (shape != 0)  build k_2k_2k arm   // 2 doubled axes
   else             build k_k_2k arm    // 1 doubled axis
   // each arm:
   push_back( TwistedView{ reduce-scatter coordinate folds } )  // view 0
   push_back( TwistedView{ all-gather    coordinate folds } )   // view 1
```

> **[CONFIRMED]** The decompile shows two `vector<unique_ptr<TwistedView>>::push_back` sites (`@0x133e1ea0` body, lines ~1300 and ~1542). The two views carry `std::function<TpuDimensions(TpuDimensions)>` closure stores: the first view installs `ConstructTwistedViews()::$_0` (`@0x133e6160`) and `ConstructTwistedViews()::$_1` (`@0x133e6180`); the second installs the forward/inverse pair `TwistedView::SetToTwistedCoordinates::$_0` (`@0x133e4140`) and `TwistedView::SetToOriginalCoordinates::$_0` (`@0x133e4580`). The two-view structure and the shape dispatch are byte-confirmed. The presence of a `SetToOriginalCoordinates` partner to `SetToTwistedCoordinates` matches `$_4` being the *inverse* of the `$_1` seam (§1.5).

> **[LOW]** **Which** of the 7 fold closures populates **which** of the two views' three axis-groups, per shape, was traced to the construction order amid the `shared_ptr` refcounting — not reduced to a per-`(shape, view, group)` composition table. The two-view `{2,3,3}`-group structure and the 7 primitives' closed forms (§1.5) are byte-exact, but the analog of the TensorCore "Phase0 uses fold $_1/$_3/$_2; Phase1 uses $_5/$_4/$_6" assignment table is open. Decoding it would let the SparseCore physical coordinate of any `(logical group, member)` be reconstructed in closed form, the SparseCore analog of the [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) coordinate table.

### 1.4 `TwistedView::ForEachPhase` — the 3-axis fold engine

```cpp
// xla::tpu::sparse_core::collective::TwistedView::
TpuDimensions ForEachPhase(
    TpuDimensions                                              dims,
    std::vector<std::shared_ptr<std::function<long(long,long)>>> transform,
    int                                                       phase);
```

`ForEachPhase @0x133e17c0` is the SparseCore equivalent of the TensorCore coordinate fold — it maps a logical `TpuDimensions` to a physical `TpuDimensions` by applying three per-axis fold closures:

```text
ForEachPhase(dims, transform, phase):
   out = TpuDimensions{}                       // new(0xC) = 3 × int32
   CHECK dim_per_phase_.size() == transform.size()   // fatal, line 77
   CHECK dim_per_phase_.size() == 3                   // fatal, line 78
   for axis i in [0, 3):
       a    = dim_per_phase_[i]                 // permutation index, < 3
       b    = dim_per_phase_[phase]             // the phase column index, < 3
       out[a] = (*transform[i])(dims[a], dims[b])   // call *(fn+0x10): std::function invoker
   return out
```

The closure is invoked through `*(transform[i] + 0x10)` — the `std::function` call-operator slot — with the two source coordinates `dims[a]` and `dims[b]`. The fixed `== 3` rank is the SparseCore counterpart of the TensorCore `num_max_dims == 2` / 3-D twist invariant ([get-replica-pair-3d.md](get-replica-pair-3d.md) §2). `TwistedView::SetToTwistedCoordinates::$_0` (`@0x133e4140`) clones the fold list and drives `ForEachPhase` once per phase; `SetToOriginalCoordinates::$_0` (`@0x133e4580`) is its inverse partner.

> **[CONFIRMED]** The decompile shows `new(0xCu)` (12-byte `TpuDimensions`) for both the input clone and the output, the two fatal `CHECK`s (`"dim_per_phase_.size() == transform.size()"` line 77, `"dim_per_phase_.size() == 3"` line 78, both `LogMessageFatal` in `offload_collective_config_builder.cc`), the 3-iteration loop with the `< 3` bound checks on each index, and the `*(transform[i] + 16)(obj, dims[a], dims[b])` indirect call. The raw working note's hex line markers `0x4d`/`0x4e` are decimal `77`/`78` — consistent.

### 1.5 The 7 fold primitives

Each closure is `operator()(long i, long j)` capturing `K`, where `i` is the **first** argument and `j` the **second** (`dims[a]` and `dims[b]` respectively in `ForEachPhase`). The closed forms below follow that convention, byte-verified against each `$_N` body:

| Closure | Address | Closed form | Role |
|---------|---------|-------------|------|
| `$_1` | `0x133e4ae0` | `(⌊j/K⌋·K + i) mod 2K` | the `+K`-mod-`2K` twist seam (forward) |
| `$_2` | `0x133e4b80` | `i mod K` | short `K`-axis coordinate (uses `i` only) |
| `$_3` | `0x133e4bc0` | `⌊j/K⌋·K + i` | long-axis base `+ i` |
| `$_4` | `0x133e4c40` | `(i − ⌊j/K⌋·K) mod 2K` | inverse seam (the `−⌊j/K⌋·K` counterpart of `$_1`) |
| `$_5` | `0x133e4d00` | `⌊j/K⌋·K + i` | `= $_3` |
| `$_6` | `0x133e4d80` | `i mod K` | `= $_2` (uses `i` only) |
| `$_7` | `0x133e4dc0` | `i` | identity (returns the first arg) |

The seam primitive `$_1` is the exact SparseCore restatement of the TensorCore `+K`-mod-`2K` fold ([overview](overview.md) §1, [get-replica-pair-3d.md](get-replica-pair-3d.md) §3): walk the short `K`-axis, jump `+K` along the long axis, walk the `K`-axis a second time — the dateline that cuts the cyclic dependency on the doubled ring. `$_4` is its inverse: same `⌊j/K⌋·K` step subtracted rather than added, with a `(… mod 2K + 2K) mod 2K` non-negative-remainder fixup. The `⌊j/K⌋` floor division is the standard sign-correct truncated-quotient idiom (`idiv K` then a `(rem != 0 && signs differ) ? -1` carry correction).

---

## 2. The per-phase core counts — `GetPhase0Cores` / `GetPhase1Cores`

`TwistedTorusND::GetPhase0Cores` and `GetPhase1Cores` are vtable virtuals on the **TensorCore** strategy class (`TwistedTorusND` vtable `0x219242a0`, slots `+0x50`/`+0x58`), but they answer a SparseCore-relevant question: **how many cores does one phase's ring/plane touch?** They are the count-only twins of the `GetPhaseNReplicaGroups` device lists ([2-Phase Replica-Group Construction](replica-group-2phase.md)), and the offload-collective fusion validators read them to check that an offloaded fusion's shard geometry matches the strategy.

Both first call `UpdateMinMaxDims` (the `K`/`2K` classifier; [overview](overview.md) §3) to populate `[obj+0x5f0]=2K`, `[obj+0x5f8]=K`, `[obj+0x600]=num2K`, then index those three fields.

### 2.1 `GetPhase0Cores = 2K · LogicalDevicesPerChip`

```text
GetPhase0Cores(target, device_assign):
   UpdateMinMaxDims(target)
   v10    = *((long*)this + 190)            // [this+0x5f0] = 2K
   result = v10 * Target::LogicalDevicesPerChip(target, 0)
   VLOG(1) "GetPhase0Cores: " << result     // all_reduce_strategies.h:792
   return result
```

`LogicalDevicesPerChip(0)` is `1` in megacore mode and the physical core count otherwise (see [SC-Offload Config Builder](../collectives/sc-offload-config-builder.md) §2.1). So the Phase0 (reduce-scatter) core count is `2K` under megacore and `4K` for a non-megacore 2-core chip — the doubled-axis ring length scaled by cores per chip.

### 2.2 `GetPhase1Cores = R = (num2K ≥ 2 ? 2K : K)`

```text
GetPhase1Cores(target, device_assign):
   UpdateMinMaxDims(target)
   result = *((long*)this + (*((long*)this + 192) < 2) + 190)
   //  index 192 = [this+0x600] = num2K
   //  (num2K < 2) → +1 → index 191 = [this+0x5f8] = K
   //  (num2K >= 2) → +0 → index 190 = [this+0x5f0] = 2K
   VLOG(1) "GetPhase1Cores: " << result      // all_reduce_strategies.h:800
   return result
```

The boolean `(num2K < 2)` is added to the base QWORD index `190` (`0x5f0`): a single doubled axis (`num2K == 1`) selects `K`, two doubled axes (`num2K ≥ 2`) selects `2K`. That is exactly `R`, the all-gather segment count and the **same `R`** the SparseCore `TwistedTorusTopologyInfo` triple carries (§1.2) and the TensorCore replica-group Phase1 plane spans.

> **[CONFIRMED]** Both bodies are byte-exact in the decompile. Phase0: `*((long*)this + 190) * LogicalDevicesPerChip(a2, 0)` with VLOG `"GetPhase0Cores: "` at `all_reduce_strategies.h:792`. Phase1: `*((long*)this + (*((long*)this + 192) < 2LL) + 190)` with VLOG `"GetPhase1Cores: "` at line 800. The QWORD indices `190`/`191`/`192` are offsets `0x5f0`/`0x5f8`/`0x600` (`190·8 = 0x5f0`). The base `StrategyND::GetPhase0Cores @0x137d6980` instead routes through `BaseStrategyND::ComputeColorDimensions` (a per-color chip dimension) — confirming the `TwistedTorusND` override is the distinct `2K·LDPC` / `R` formula.

### 2.3 The validators that read them

| Function | Address | Reads | Purpose |
|----------|---------|-------|---------|
| `cross_replica_sharding_util::TryParseColorwiseAllReduceFusion` | `0x137e34c0` | `GetPhase0Cores` (slot `+0x50`) | validate AR fusion shard geometry |
| `cross_replica_sharding_util::TryParseAllGatherFusion` | `0x137e4ec0` | `GetPhase1Cores` (slot `+0x58`) | validate AG fusion replica geometry |

Each parser calls the virtual through the vtable (`call *0x50(%rax)` / `*0x58`) and compares the returned per-phase core count against the HLO fusion's operand shapes — rejecting a fusion whose shard/replica geometry does not match the strategy's per-phase ring/plane.

### 2.4 Core counts vs replica groups — the distinction

These return a **count**, not a device list. The companion [2-Phase Replica-Group Construction](replica-group-2phase.md) builds the full `ReplicaGroup` member device-IDs; `GetPhaseNCores` return only the ring length / segment cardinality.

| Phase | Function | Count | vs replica group member count |
|-------|----------|-------|-------------------------------|
| 0 — reduce-scatter | `GetPhase0Cores @0x137d6de0` | `2K · LDPC` | RS ring length × cores/chip (group has `2K`, ×2 if megacore) |
| 1 — all-gather | `GetPhase1Cores @0x137d6ec0` | `R = (num2K ≥ 2 ? 2K : K)` | AG plane segment count (group has `R·K` members) |

---

## 3. `EstimatePhysicalLinksUsed` — the physical-ICI-link estimator

```cpp
// xla::jellyfish::
std::vector<IciResource> EstimatePhysicalLinksUsed(
    const Target&                                target,
    const DeviceAssignment&                      device_assign,
    absl::Span<const std::vector<GlobalDeviceId>> replica_groups);
```

`EstimatePhysicalLinksUsed @0x1c8939c0` answers "how many distinct physical ICI links does this collective's set of replica groups traverse?" — the **"links" divisor** the all-to-all / ragged / cross-module-all-reduce cost model divides bandwidth by. Crucially, it is **not** a scalar product of per-axis spans: it returns a `vector<IciResource>` and the link count is `result.size()`.

### 3.1 The algorithm — a torus-set walk over member chip coordinates

```text
EstimatePhysicalLinksUsed(target, device_assign, replica_groups):
   result = {}                                    // FlatHashSet<IciResource> (CRC32 swiss-set)
   multi_slice = (Target::GetMultiSliceTopology(target) != nullptr)

   for each replica group g in replica_groups:    // outer loop, group stride 0x18
       // resolve member[0] → chip coordinates:
       id0  = LogicalDeviceForId(0, g.member[0])  // multi-slice: ToSliceAndLogicalDeviceId
       ref  = chip_coordinates(id0)               // (ref.X, ref.Y, ref.Z)
       sameX = sameY = sameZ = true               // "all members share this axis?"

       for m in g.members[1 .. N-1]:
           id = LogicalDeviceForId(0, m)          // multi-slice gated
           c  = chip_coordinates(id)
           sameY &= (c.Y == ref.Y)                // HIDWORD compare
           sameX &= (c.X == ref.X)                // LODWORD compare
           sameZ &= (c.Z == ref.Z)

       // per-axis directional resource insert (the 6 IciResources 1..6):
       insert( sameY ? <Y dir> : <Y dir'> )       // value in {1, 2}
       insert( sameX ? <X dir> : <X dir'> )       // value in {3, 4}
       insert( sameZ ? <Z dir> : <Z dir'> )       // value in {5, 6}

   v = sort(vector(result))                        // __introsort over IciResource*
   return v                                        // links = v.size()
```

A flag stays `true` only if every member of the group shares that coordinate with `member[0]` — i.e. the group does **not** span that axis. The per-axis flag then selects one of the axis's two directional `IciResource` values to insert. Because `result` is a set, a directional resource touched by multiple groups counts once; a group that spans more axes (or both directions of an axis) contributes more distinct resources.

> **[CONFIRMED]** The decompile shows: `GetMultiSliceTopology` gate; the per-member `LogicalDeviceForId(…, 0, …)` → `TpuCoreLocation::chip_coordinates` resolution; the three "all-same-on-axis" AND reductions (`v34 &= HIDWORD(coords) == ref`, `v185 &= (DWORD)coords == ref`, third axis `&& `); the six `IciResource` constant stores `LODWORD(res) = N` for `N ∈ {1,2,3,4,5,6}`; the `FlatHashSet<IciResource>` inserts via `PrepareInsertLarge` / `GrowSooTableToNextCapacityAndPrepareInsert` with `_mm_crc32_u64` hashing; the multi-slice `ToSliceAndLogicalDeviceId` path with cross-slice RetCheck `"Unsupported cross-slice replica groups"` (`group_utils.cc:1652`); and the finalize — `operator new(4 * count)` (4-byte `IciResource` each), copy the set's live slots (skipping the `≤ -2` empty/deleted sentinels), and `std::__introsort<… IciResource* …>` to sort before the `sret` return. All byte-confirmed.

### 3.2 The `IciResource` encoding

The 6 directional resources are the `{axis × direction}` cross product:

| `IciResource` | Torus dim | Direction | `ResourceVector` slot (`GetResourceFromIciResource`) |
|---------------|-----------|-----------|------------------------------------------------------|
| `1` | X | dir 0 | `0xd` |
| `2` | X | dir 1 | `0xe` |
| `3` | Y | dir 0 | `0xf` |
| `4` | Y | dir 1 | `0x10` |
| `5` | Z | dir 0 | `0x11` |
| `6` | Z | dir 1 | `0x12` |

`GetResourceFromIciResource @0x1c894c00` maps each value `e ∈ [1,6]` to slot `e - 1 + 0xd` (slots `0xd..0x12`). The dim↔resource pairing matches the cost consumer's per-dim read (§3.3).

> **[LOW]** **Which** of an axis's two directional resources (even vs odd of each pair) a spanned axis selects — the `+/-` SerDes direction parity — was traced to the same-flag conjunction (the `sameX`/`sameY`/`sameZ` booleans drive the `{1,2}`/`{3,4}`/`{5,6}` choice), not tied to the physical SerDes direction sign. The decompile shows resource values `2`, `4`, and `6` (the "dir 1" of each axis) inserted at **two** sites each (the even/odd selection by member-walk parity), confirming the dispatch but not the geometric direction sign. The interaction with a degraded axis (which can drop a resource from the set — the partial-torus reliability path) was observed via the consumer's degraded-mask AND (§3.3) but not field-decoded.

### 3.3 The cost-model consumer

`ComputeAllToAllCyclesHelper @0x130d02a0` calls `EstimatePhysicalLinksUsed` and walks the returned `vector<IciResource>` (4 bytes each). For each value it maps `{1/3/5 → X/Y/Z}` and reads the corresponding chip torus-dim extent (`[cfg+0x58]` etc.) with a `cmovle` (the **minimum** over the resources), AND'ing the degraded-axis bool `[cfg+0xa0]`. So the all-to-all / cross-module-all-reduce bandwidth divisor is a function of the **minimum spanned-axis extent** and the degraded mask — and the `÷ links` term is concretely "`÷` (distinct directional ICI resources the replica groups touch)", not a closed-form product of spans.

This resolves the cost model's prior open question (the link divisor "traced to the chip-coordinate walk, not reduced to a single equation"): the divisor is `|EstimatePhysicalLinksUsed(...)|`, the cardinality of the sorted directional-resource set.

---

## 4. SparseCore twist vs TensorCore twist — structure parallel

The SparseCore `TwistedTorusTopologyInfo` and the TensorCore `TwistedTorusND` describe the **same physical twist** and share the `R` knob and the seam, but differ in representation and output:

| Aspect | TensorCore (`group_utils.cc` / `all_reduce_strategies.h`) | SparseCore (`offload_collective_config_builder.cc`) |
|--------|----------------------------------------------------------|-----------------------------------------------------|
| Class | `xla::jellyfish::TwistedTorusND` (StrategyND) | `sparse_core::collective::TwistedTorusTopologyInfo` |
| Shapes | `k_k_2k`, `k_2k_2k` (+ routing-only `k_2k_nK`) | `k_k_2k`, `k_2k_2k` **only** (no literal `nK`) |
| `R` knob | `num2K ≥ 2 ? 2K : K` (`[obj+0x600]` select) | `shape ? 2K : K` (triple `[this+0x40]`) — **same** |
| Seam | `+K`-mod-`2K` (`GetReplicaPair3DOnTwistedTorus`) | `+K`-mod-`2K` (fold primitive `$_1`) — **same** |
| Phase representation | 2 × `GetPhaseNReplicaGroups` loop nests | 2 × `TwistedView` (`vector<vector<shared_ptr<fn>>>`) |
| Coordinate fold | `GetReplicaPair3DOnTwistedTorus @0x1c893400` | `ForEachPhase @0x133e17c0` + 7 fold closures |
| Rank invariant | `num_max_dims == 2` CHECK | `CHECK dim_per_phase_.size() == 3` — **same 3-D** |
| Output | HLO `ReplicaGroup` device lists | `CollectiveIciStrategyConfig` per-color rings |
| Consumer | XLA collective scheduler | `ConstructConfigForCollectiveUniDirNDGroups<*>` (offload) |
| Per-phase counts | `GetPhase0/1Cores` (§2) — `2K·LDPC` / `R` | (counts shared via the same `TwistedTorusND` virtuals) |

---

## 5. Function Map

| Function | Address | Role | Confidence |
|----------|---------|------|------------|
| `TwistedTorusTopologyInfo::TryCreateTwistedTorusTopologyInfo` | `0x133e1980` | SC twist factory + shape/divisibility gate | HIGH (RetCheck strings verified) |
| `TwistedTorusTopologyInfo::TwistedTorusTopologyInfo` (ctor) | `0x133e1b40` | object layout + `(2K,R,K)` triple + 7 closures | MEDIUM (offsets from disasm, not re-walked) |
| `TwistedTorusTopologyInfo::ConstructTwistedViews` | `0x133e1ea0` | two `TwistedView` build (RS + AG) | HIGH (two push_backs); per-group fold assignment LOW |
| `TwistedView::ForEachPhase` | `0x133e17c0` | 3-axis fold; `CHECK==3` + `*0x10` dispatch | HIGH |
| `TwistedView::SetToTwistedCoordinates::$_0` | `0x133e4140` | drives `ForEachPhase` per phase (forward twist) | HIGH (call-func policy) |
| `TwistedView::SetToOriginalCoordinates::$_0` | `0x133e4580` | inverse twist partner of `SetToTwistedCoordinates` | HIGH (call-func policy) |
| `ConstructTwistedViews()::$_0` / `$_1` | `0x133e6160` / `0x133e6180` | first view's two `TpuDimensions(TpuDimensions)` closures | HIGH (policy stores) |
| fold closures `$_1..$_7` | `0x133e4ae0..0x133e4dc0` | the 7 coordinate-fold primitives | HIGH (closed forms verified) |
| `TwistedTorusND::GetPhase0Cores` | `0x137d6de0` | `2K · LogicalDevicesPerChip` | HIGH (field arithmetic verified) |
| `TwistedTorusND::GetPhase1Cores` | `0x137d6ec0` | `R = (num2K ≥ 2 ? 2K : K)` | HIGH (field arithmetic verified) |
| `StrategyND::GetPhase0Cores` (base, contrast) | `0x137d6980` | per-color chip dim via `ComputeColorDimensions` | HIGH |
| `TryParseColorwiseAllReduceFusion` | `0x137e34c0` | AR-fusion validator (reads slot `+0x50`) | HIGH (located) |
| `TryParseAllGatherFusion` | `0x137e4ec0` | AG-fusion validator (reads slot `+0x58`) | HIGH (located) |
| `EstimatePhysicalLinksUsed` | `0x1c8939c0` | sorted-set directional-`IciResource` link estimator | HIGH |
| `EstimatePhysicalLinksUsed::$_0` (multi-slice) | `0x1c894ac0` | global → local logical-id resolver | HIGH (cross-slice RetCheck) |
| `GetResourceFromIciResource` | `0x1c894c00` | `e-1+0xd` (slots `0xd..0x12`) | HIGH |
| `ComputeAllToAllCyclesHelper` | `0x130d02a0` | maps `IciResource` → torus-dim extent (divisor) | HIGH (call site + dim read) |

---

## 6. What Was Not Resolved

- **The per-`(shape, view, group)` fold-closure assignment** in `ConstructTwistedViews`. The two-view `{2,3,3}`-group structure and the 7 primitives' closed forms are byte-exact, but which closure index populates which of the two views' three axis-groups, per shape, was traced to construction order, not reduced to a composition table. **[LOW]** — see §1.3.
- **The `+/-` SerDes direction parity** in `EstimatePhysicalLinksUsed`. The per-axis same-flag → resource dispatch and the 6 values are confirmed, but which directional resource of each pair a spanned axis maps to (the even/odd selection by member-walk parity), and how a degraded axis drops a resource from the set, were not field-decoded. **[LOW]** — see §3.2.
- **The `TwistedTorusTopologyInfo` ctor offsets.** The `(2K,R,K)` triple, the `is-doubled-axis` mask, and the 7-closure storage offsets (`[this+0x40]`/`[+0x58]`/`[+0x78..0xe0]`) were reconstructed from the ctor disassembly, not re-walked instruction-by-instruction. The `R` knob and shape discriminant are HIGH; the exact field offsets are **MEDIUM**.
- **The `IciDim` type layout.** `TryCreate` takes `vector<IciDim>` and the cost path uses `vector<IciResource>`; whether `IciDim` carries a `(dim_index, extent, wrap-mode)` tuple and how it unifies with the dense mesh descriptor was not field-decoded. **[LOW]**

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Section Map](overview.md) — the three-class name collision (`TwistedTorusND` / `TwistedTorusTopology` / `TwistedTorusTopologyInfo`) and the shape gate
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the TensorCore twist class family, ND ring + per-color seam phases
- [2-Phase Replica-Group Construction](replica-group-2phase.md) — the TensorCore `GetPhaseNReplicaGroups` device lists these `*Cores` counts twin
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the TensorCore `+K`-mod-`2K` coordinate fold the SC `$_1` seam mirrors

**Sibling sections**
- [SC-Offload Config Builder](../collectives/sc-offload-config-builder.md) — `ConstructConfigForCollectiveUniDirNDGroups<*>`, the consumer that reaches `TryCreateTwistedTorusTopologyInfo` through its `cmp $3` K/2K gate
- [SC Core-Selection (Offload)](../collectives/sc-core-selection-offload.md) — the SparseCore op-type classification and core selection upstream of the offload config
- [back to index](../index.md)
