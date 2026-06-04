# 2-Phase Replica-Group Construction

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); all addresses are VMA. Every symbol cited is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

A twisted-torus all-reduce is **two collectives back to back**: a reduce-scatter along the doubled `2K` ICI ring, then an all-gather over the `K×R` plane orthogonal to that ring. XLA does not see ICI links — it sees HLO `ReplicaGroup` device lists. The two functions on this page, `TwistedTorusND::GetPhase0ReplicaGroups` (`0x137d3560`) and `TwistedTorusND::GetPhase1ReplicaGroups` (`0x137d3de0`), are the constructors of those lists: each walks the twist coordinate fold and emits a `std::vector<ReplicaGroup>` whose `i`-th group is exactly the set of logical devices that participate in collective `i` of that phase. They are the **emission side** that complements [`BuildStrategy`](buildstrategy.md): `BuildStrategy` writes the per-color *ring neighbour* tables the LLO all-reduce emitter consumes, while these two write the *device-id* group lists the XLA collective scheduler keys on. Both views describe the same twisted geometry.

The whole construction reduces to three numbers from `UpdateMinMaxDims` — `K` (short axis), `2K` (long axis), `R = (num-2K-axes ≥ 2) ? 2K : K` — and one coordinate fold, [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md), which maps each `(i, j_or_m, k)` loop triple to a physical chip and then to that chip's `{core0, core1}` logical-device pair through the `[Y][X][Z]` table built by `GetPhysicalToLogicalMapping3D`. Phase0 sweeps `j` over the `2K` ring (group index `k·R + i`, `K·R` groups of `2K` members); Phase1 sweeps the `(i, k)` plane (group index `m` or `{2m, 2m+1}`, `2K·LogicalDevicesPerChip` groups of `R·K` members). The asymmetry between the phases — Phase0 always co-groups both cores of a chip, Phase1 may split them across an even/odd group pair — is the **4K-vs-2K group sizing** this page documents.

This page owns the two-phase build, the group sizing, and the single-phase shard gate `GetPerColorShardIdTable` (`0x137d2d80`). It does **not** re-derive the per-`(i,j,k)` coordinate fold (that is [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md)), the `K`/`2K` shape classification (that is [Shape Folds](shape-folds.md)), or the byte-exact megacore gate (that is [Megacore Even/Odd Split](megacore-even-odd.md)). It links them and uses their results.

For reimplementation, the contract is:

- **The three derived scalars.** `K = min_dim`, `2K = max_dim`, `R = num-2K-axes ≥ 2 ? 2K : K`, all read after `UpdateMinMaxDims`. `R` is the *plane* dimension shared by both phases; `2K` is the *ring* dimension.
- **The Phase0 build.** `K·R` groups; group index `k·R + i`; for each group the `2K` chips the twisted ring places at steps `j = 0..2K-1`; both megacore cores of a chip join the same group.
- **The Phase1 build.** `2K·LogicalDevicesPerChip(0)` groups; group index `m` (single) or `{2m, 2m+1}` (even/odd split); for each group the `R·K` chips of long-axis slice `m`; the split routes core0→even and core1→odd.
- **The shard gate.** `GetPerColorShardIdTable` fatal-errors any shard count `≥ 2`: the twisted collective is single-phase-sharding only, so the two-phase RS→AG above is the *whole* algorithm.

| | |
|---|---|
| **Phase0 (reduce-scatter)** | `TwistedTorusND::GetPhase0ReplicaGroups` `0x137d3560` (`all_reduce_strategies.cc:2302`) |
| **Phase1 (all-gather)** | `TwistedTorusND::GetPhase1ReplicaGroups` `0x137d3de0` (`all_reduce_strategies.cc:2334`) |
| **Shard gate** | `TwistedTorusND::GetPerColorShardIdTable` `0x137d2d80` (1-phase only) |
| **Coordinate fold (called by both)** | `GetReplicaPair3DOnTwistedTorus` `0x1c893400` — [page](get-replica-pair-3d.md) |
| **Physical→logical map (built by both)** | `GetPhysicalToLogicalMapping3D` `0x1c88a280` — `[Y][X][Z] → {core0, core1}` |
| **`K` / `2K` / `R`** | `[obj+0x5f8]` / `[obj+0x5f0]` / `(num-2K-axes ≥ 2 ? 2K : K)` ; num-2K-axes `[obj+0x600]` |
| **Phase0 groups / members** | `K·R` groups, `2K` members each (member index `j`) |
| **Phase1 groups / members** | `2K · LogicalDevicesPerChip(0)` groups, `R·K` members each (member index `(i,k)`) |
| **`ReplicaGroup` element size** | 48 bytes (`operator new(48·groups)`, both phases) |
| **Confidence** | HIGH — Phase0/Phase1 loop nests, group indices, and sizing decompile-verified; megacore split gate deferred to [sibling](megacore-even-odd.md) |

---

## 1. Shared Prologue — The Three Scalars and the Two Maps

Both phase builders run an identical prologue before their loop nest. A reimplementer should factor it out exactly as the binary does.

### Algorithm

```c
function GetPhaseNReplicaGroups(target, dev_assign, dev_assign2, arg, all_cores):
    UpdateMinMaxDims(target)                       // K, 2K, num-2K, num-K  (Shape Folds)
    CHECK(target.num_dims_ == 3)                   // [obj+0x59c]==3, "num_dims_ == kMaxDims"
    InitColorDimensions(target)                    // color_dims[6][3]  (BuildStrategy)
    phys_to_log = GetPhysicalToLogicalMapping3D(target, dev_assign2, all_cores)
                                                   // 0x1c88a280 — [Y][X][Z] -> {core0, core1}
    K  = target[obj+0x5f8]                          // min_dim   (Phase0 v19, Phase1 v92)
    twoK = target[obj+0x5f0]                        // max_dim   (Phase0 v89, Phase1 v96)
    R  = (target[obj+0x600] >= 2) ? twoK : K        // num-2K-axes >= 2 ? 2K : K
                                                   // Phase0 v20/v94, Phase1 v19/v95 (cmovge)
```

`K`, `2K`, and `num-2K-axes` are the `UpdateMinMaxDims` outputs at `[obj+0x5f8]`, `[obj+0x5f0]`, `[obj+0x600]` ([Shape Folds](shape-folds.md)). `R` is the one branch in the prologue: it is `K` for a single doubled axis (`K_K_2K`) and `2K` for two doubled axes (`K_2K_2K`), selected by a `cmovge` on `num-2K-axes ≥ 2`. `R` is the *plane* dimension — the axis count that is **not** the ring — and it appears as a loop bound in both phases.

> **NOTE —** the `num_dims_ == 3` (`kMaxDims`) CHECK fires on entry to both phase builders (`all_reduce_strategies.cc:2304` / `:2336`). The twisted collective half is hard-wired to a 3-D slice; the `num_max_dims == 2` CHECK in the coordinate fold ([`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md)) enforces the matching 2-axis cap. A reimplementer targeting >3 dims must rebuild both, not just the loop bounds.

### `GetPhysicalToLogicalMapping3D` — the device-id source

Every member device id a `ReplicaGroup` receives comes from this map (`0x1c88a280`), not from the loop indices. It is a `vector<vector<vector<pair<long,long>>>>` indexed `[Y][X][Z]` — dimensions `[chip_cfg+0x5c]`, `[chip_cfg+0x58]`, `[chip_cfg+0x60]` respectively — whose every leaf is initialised `{-1,-1}` and then filled by walking the `DeviceAssignment`:

```c
function GetPhysicalToLogicalMapping3D(target, dev_assign, all_cores):  // 0x1c88a280
    map[Y][X][Z] = pair{-1, -1}  for all                 // 0x1c88a5bf / 0x1c88a5e6
    for each logical device in dev_assign:
        flat_id = sum(coord_k * stride_k)                 // imul/add chain 0x1c88a700..
        loc     = TpuTopology::LogicalDeviceForId(0, flat_id)   // 0x20ad4120
        (cY, cX, cZ) = loc.chip_coordinates()             // 0x20ad62e0
        if partition slot 0: map[cY][cX][cZ].first  = logical_id   // 0x1c88a882
        else               : map[cY][cX][cZ].second = logical_id   // 0x1c88a8de
    return map
```

`.first` is the chip's core0 logical id, `.second` its core1. The coordinate fold returns one of these two halves per `(i,j,k)` triple, and the phase builder appends it (and, in the megacore Phase0 case, its sibling half) to the active group. A leaf left at `{-1,-1}` means the chip is absent from the assignment — the loops never index an absent chip because their bounds are the slice extents.

> **GOTCHA —** the map is indexed `[Y][X][Z]`, the same order `UpdateMinMaxDims` reads the obj dim fields (`[obj+0xb8]=Y`, `[obj+0xc0]=X`, `[obj+0xc8]=Z`). The loop-variable↔axis convention is `Y↔j`, `X↔i`, `Z↔k`, confirmed from the `GetReplicaPair3DOnTwistedTorus` call-site argument order in both phases. A reimplementer who builds the map `[X][Y][Z]` will read the wrong chip and the groups will silently scramble.

---

## 2. Phase 0 — Reduce-Scatter Along the `2K` Ring

`GetPhase0ReplicaGroups` (`0x137d3560`) builds the groups for the first collective: the reduce-scatter that walks the doubled `2K` ICI ring. There is one group per `(i, k)` position in the orthogonal plane, and each group's members are the `2K` chips the twisted ring threads through.

### Algorithm

```c
function GetPhase0ReplicaGroups(target, da, da2, arg, all_cores):   // 0x137d3560
    <shared prologue: K, 2K, R, phys_to_log>                        // §1
    n_groups = K * R                                                // v21 = v19 * v20
    groups   = vector<ReplicaGroup>(n_groups)                       // operator new(48 * n_groups)
    cores_per_chip = target.CoresPerChip(0)                         // 0x137d360d (v88)

    for i = 0 .. R-1:                                               // OUTER  (v94 == R)
        for j = 0 .. 2K-1:                                          // MIDDLE (v89 == 2K) -- the ring
            for k = 0 .. K-1:                                       // INNER  (v87 == K)
                pair = GetReplicaPair3DOnTwistedTorus(              // 0x1c893400
                           phys_to_log, &target[obj+0xb8],
                           2K, K, num_2K, arg, i, j, k)
                g = i + R*k                                         // group index k*R + i  (v50)
                groups[g].add_replica_id(pair.first)               // core0 (0x137d3adc)
                if not skip_second_core:                            // megacore predicate below
                    groups[g].add_replica_id(pair.second)          // core1 (0x137d3737)
```

### Group count, index, and members

| Quantity | Value | Evidence |
|---|---|---|
| Group count | `K · R` | `v21 = v19[K] * v20[R]`, alloc `48·v21` @ `0x137d3560` body |
| Group index | `k · R + i` | `v50 = i + R·k` @ `0x137d3a79` |
| Members per group | `2K` (`×2` if both cores appended) | middle loop `j = 0..2K-1`, one or two appends per `(i,k)` |
| Member ordering | the twisted ring step order `j = 0..2K-1` | `j` is the only index swept into a single group |

Group `(i, k)` is the reduce-scatter ring at plane position `(i, k)`: it collects, for `j = 0..2K-1`, the chip the twist places at ring step `j`. Because `j` is the **middle** loop and the group index `k·R + i` does not depend on `j`, all `2K` ring steps for a fixed `(i, k)` land in the same group — that group *is* the ring. The seam (the `+K`-mod-`2K` jump that the coordinate fold applies at `j ≥ K`) means consecutive `j` values are not consecutive physical chips; they are the two `K`-segments stitched at the dateline. See [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md) for the per-`j` chip math.

### The Phase0 megacore co-grouping

Phase0 appends `pair.first` unconditionally and then `pair.second` (the chip's other core) **into the same group**, unless a megacore predicate skips the second append:

```c
// skip_second_core (Phase0), decompile @ 0x137d35... around the LABEL_19 guard
if Megacore(chip_cfg):
    skip = (cores_per_chip == 1) || (chip_cfg[+124] > 1)
else:
    skip = (cores_per_chip == 1)
```

The decisive property for Phase0 is that whenever both cores are appended, **they go to the same group** (both appends write the same `groups[g]` pointer, `0x137d3adc` then `0x137d3737`). A reduce-scatter ring keeps a chip's two cores co-resident on the ring; the cores do not fan out until the all-gather. The exact predicate is the [Megacore Even/Odd Split](megacore-even-odd.md)'s domain; for Phase0 the only group-shape consequence is "one group, both cores".

> **NOTE —** the `arg` parameter (`a5`, the weight-update shard count) is passed straight through to `GetReplicaPair3DOnTwistedTorus` and otherwise ignored by Phase0. It selects an alternate coordinate fold only for shard counts `≥ 1`, which the shard gate (§4) makes unreachable in v0.0.40. In practice Phase0 always runs with `arg == 0`.

---

## 3. Phase 1 — All-Gather Over the `K×R` Plane

`GetPhase1ReplicaGroups` (`0x137d3de0`) builds the groups for the second collective: the all-gather over the plane orthogonal to the `2K` ring. There is one group (or one even/odd pair of groups) per long-axis slice `m`, and each group's members are the `R·K` chips of that slice.

### Algorithm

```c
function GetPhase1ReplicaGroups(target, da, da2, arg, all_cores, b):   // 0x137d3de0
    <shared prologue: K, 2K, R, phys_to_log>                           // §1
    ldpc     = target.LogicalDevicesPerChip(0)                         // 0x1d615b00
    n_groups = 2K * ldpc                                               // v20 = v18[2K] * ldpc
    groups   = vector<ReplicaGroup>(n_groups)                          // operator new(48 * n_groups)
    cores_per_chip = target.CoresPerChip(0)                            // call @ 0x137d3e8a (v93)

    for m = 0 .. 2K-1:                                                 // OUTER  (v96 == 2K)
        g_single = m                                                   // 48*m offset (v94)
        g_even   = 2*m                                                 // (v90)
        g_odd    = 2*m + 1                                             // (v91)
        for i = 0 .. R-1:                                              // MIDDLE (v95 == R)
            for k = 0 .. K-1:                                          // INNER  (v92 == K)
                pair = GetReplicaPair3DOnTwistedTorus(                 // 0x1c893400
                           phys_to_log, &target[obj+0xb8],
                           2K, K, num_2K, arg, i, m, k)
                if split:                                              // even/odd split predicate
                    groups[g_even].add_replica_id(pair.first)         // core0 -> 2m   (0x137d43d5)
                    groups[g_odd ].add_replica_id(pair.second)        // core1 -> 2m+1 (0x137d3ff2)
                else:
                    groups[g_single].add_replica_id(pair.first)       // -> m          (0x137d3fee)
```

### Group count, index, and members

| Quantity | Value | Evidence |
|---|---|---|
| Group count | `2K · LogicalDevicesPerChip(0)` | `v20 = v18[2K] * LogicalDevicesPerChip(a3,0)` @ `0x137d3eb1` |
| Group index (no split) | `m` | `v94 = 48·m`, append @ `0x137d3fee` |
| Group index (split) | `{2m, 2m+1}` | `v90 = 2m`, `v91 = 2m+1`, appends @ `0x137d43d5` / `0x137d3ff2` |
| Members per group | `R · K` | middle/inner loops `i = 0..R-1`, `k = 0..K-1` |
| Member ordering | the `(i, k)` plane scan | `m` is the outer loop, fixed per group |

Group `m` (or pair `{2m, 2m+1}`) is the all-gather over long-axis slice `m`: it collects every `(i, k)` chip in the `K×R` cross-section at ring position `m`. Because `m` is the **outer** loop, all `R·K` plane chips for a fixed `m` land in the slice's group(s) — that plane *is* the all-gather domain. The all-gather reassembles, over the plane orthogonal to the ring, what the Phase0 reduce-scatter dispersed along the ring; together they form one logical all-reduce.

> **QUIRK —** the member loop bound is `R`, not `2K`, even though the *group count* multiplier is `2K`. The plane is `K × R` (`R = K` or `2K` depending on shape); the ring it is orthogonal to is always `2K`. So Phase1 has `2K` slices (one per ring step) of `R·K` chips each, while Phase0 has `K·R` rings of `2K` chips each — the two phases partition the same `K·R·2K` device grid two different ways, and the products match (`(2K)·(R·K) == (K·R)·(2K)` per logical device).

---

## 4. Group Sizing — the 4K-vs-2K Split

The single asymmetry between the phases is **whether a chip's two cores share a group or fan out into a pair of groups**. Phase0 always co-groups both cores (§2). Phase1 may split them, and that split is what doubles the group count from `2K` to `4K`.

The Phase1 group count is `2K · LogicalDevicesPerChip(0)` (decompile-exact, `0x137d3eb1`). The split is keyed on whether each chip contributes one or two logical devices:

| Configuration | `LogicalDevicesPerChip(0)` | Phase1 group count | Per-`m` append |
|---|---|---|---|
| megacore | 1 | `2K` | single group `m` (both cores → one logical device) |
| non-megacore, 1 core/chip | 1 | `2K` | single group `m` |
| non-megacore, 2 cores/chip | 2 | `4K` | **split**: core0 → group `2m` (even), core1 → group `2m+1` (odd) |

The split branch (`LABEL_50` in the decompile, the `{2m, 2m+1}` appends) is reached when a chip presents two distinct logical participants — the non-megacore 2-core case. There the two cores all-gather over **disjoint plane halves** (even/odd group), balancing the plane's ICI links across the two cores. A **megacore** chip collapses its two physical cores to a single logical device (`LogicalDevicesPerChip = 1`) and uses the single group `m`, giving `2K` groups with no even/odd split.

> **CORRECTION (TWIST-1, inherited) —** an earlier reconstruction (and the loop-level reading where `Megacore` appears in the split guard) labelled the Phase1 even/odd split as "megacore: core0→even / core1→odd". Byte-exact, it is the **non-megacore 2-logical-device** case that produces the `4K` split; megacore collapses to one logical device and uses the `2K`-group single path. The group count `2K · LogicalDevicesPerChip(0)` and the `Megacore ? 1 : CoreCount` getter behind `LogicalDevicesPerChip(0)` (`0x1d615b00` → `TpuTopology::LogicalDevicesPerChip`) are decompile-confirmed. The exact split predicate (the `CoresPerChip != 1` test, plus the megacore `chip_cfg[+124]` sub-condition) lives on [Megacore Even/Odd Split](megacore-even-odd.md). Marked HIGH.

> **NOTE —** Phase0 is unaffected by this split: its second-core append (when present) targets the *same* group as the first (`0x137d3adc` and `0x137d3737` write one `groups[g]`). So the reduce-scatter ring is always `2K` chips with `1`-or-`2` cores per chip co-grouped, never split into a `4K` count. Only the all-gather plane fans the cores apart.

---

## 5. Worked Sizing — a `K, K, 2K` Slice

A concrete shape makes the two partitions and the products line up. Take the `K_K_2K` case with `K = 2`, so `2K = 4`, one doubled axis (`num-2K-axes = 1`) ⇒ `R = K = 2`. The grid is `K·R·2K = 2·2·4 = 16` chips; assume non-megacore, 2 cores/chip ⇒ `LogicalDevicesPerChip = 2`, 32 logical devices.

| | Phase0 (RS along `2K`) | Phase1 (AG over plane) |
|---|---|---|
| Group count | `K·R = 4` | `2K·LDPC = 8` (the `4K` split) |
| Members / group | `2K = 4` chips (`×2` cores = 8 ids) | `R·K = 4` chips (1 core each) |
| Group index | `k·R + i` ∈ {0,1,2,3} | `{2m, 2m+1}` ∈ {0..7} |
| Swept index | `j = 0..3` (ring step) | `(i,k)` over `2×2` plane |
| Total replica ids | `4 groups × 4 chips × 2 cores = 32` | `8 groups × 4 ids = 32` |

Both phases cover all 32 logical devices, partitioned orthogonally: Phase0 has 4 rings of 4 chips (8 cores each); Phase1 has 8 plane-halves of 4 cores each. The reduce-scatter disperses a tensor across each 4-chip ring, then the all-gather over each 4-chip plane reassembles it — the standard RS→AG all-reduce, mapped onto the twist.

If the same slice were **megacore**, `LogicalDevicesPerChip` would be 1 ⇒ Phase1 group count `2K·1 = 4` (no even/odd split), each of the 4 groups holding the 4 chips of its slice as 4 single logical devices. Phase0 is identical in both modes (4 rings of 4 chips), differing only in whether each chip contributes one or two appended ids.

> **QUIRK —** the Phase1 group count `8` exceeds the Phase0 count `4` precisely because of the `LDPC = 2` multiplier, not because the plane is larger. The plane (`R·K = 4`) is the *same size* as the ring (`2K = 4`) for this shape; the extra Phase1 groups come from fanning the two cores of each chip into the even/odd pair. A reimplementer sizing the `vector<ReplicaGroup>` from the plane extent alone will under-allocate by a factor of `LDPC`.

---

## 6. The Single-Phase Shard Gate — `GetPerColorShardIdTable`

`GetPerColorShardIdTable` (`0x137d2d80`) is the gate that makes the §2–§4 two-phase construction the *entire* twisted-torus collective: it rejects any attempt to shard the weight update across more than one phase.

### Algorithm

```c
function GetPerColorShardIdTable(target, da, shard_table, shard_count, all_cores):  // 0x137d2d80
    n = shard_table->size                          // **shard_table  (v16 = (*a4)[0])
    if all_cores: n *= shard_table->stride         // a6 -> v16 *= v17
    else if shard_table->stride != 1: return ...   // "not all cores" early-out
    if n != TpuTopology::LogicalDeviceCount(0):     // 0x137d2d80 body @ +... (LABEL guard)
        return error  // "2D all-reduce ... only ... where all available cores participate"
    if shard_count >= 2:                            // a5 >= 2  (the 1-phase gate)
        return Unimplemented(
            "3D twisted torus weight update sharding algorithm "
            "currently supports only 1-phase sharding.")     // .rodata @ 0xa06c3fb (91 bytes)
    UpdateMinMaxDims(target); CHECK(num_dims_ == 3)
    ... build the per-color shard-id table (1-phase) ...
```

The `shard_count` argument (`a5`, the same `long` that both phase builders pass into the coordinate fold as `arg`) is compared `>= 2`; anything `≥ 2` fatal-errors with the `"3D twisted torus weight update sharding algorithm currently supports only 1-phase sharding."` string (`.rodata` `0xa06c3fb`, 91 bytes, decompile-verified verbatim). The companion `all_cores` bool (`a6`) gates the "all available cores participate" precondition; failing it returns the `"2D all-reduce algorithm only implemented for cases where all available cores participate the reduction."` string (`0xa04a6b0`).

The consequence for a reimplementer: the twisted-torus collective is **one shard**, decomposed into Phase0 reduce-scatter (§2) then Phase1 all-gather (§3). There is no multi-shard pipelining of the twist in v0.0.40 — the `arg ≥ 1` branch of [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md) (a structurally distinct fold) is present but unreachable behind this gate. Build the single-phase path; treat the multi-shard fold as dead code unless a later version lifts the `< 2` gate.

> **GOTCHA —** "1-phase sharding" here means *one weight-update shard*, not *one collective*. The collective itself is unambiguously two collectives (RS then AG). The "phase" in `GetPhase0`/`GetPhase1` (the RS/AG split) and the "phase" in this Unimplemented string (the shard count) are different axes; conflating them leads a reimplementer to think Phase1 is the forbidden second shard, which it is not.

---

## 7. Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TwistedTorusND::GetPhase0ReplicaGroups` | `0x137d3560` | RS-along-`2K` group lists; `K·R` groups, index `k·R+i`, member `j` | HIGH (loop nest + index + alloc decompile-verified) |
| `TwistedTorusND::GetPhase1ReplicaGroups` | `0x137d3de0` | AG-over-plane group lists; `2K·LDPC` groups, index `m`/`{2m,2m+1}`, member `(i,k)` | HIGH (loop nest + index + alloc decompile-verified) |
| `TwistedTorusND::GetPerColorShardIdTable` | `0x137d2d80` | 1-phase-only gate (`shard_count ≥ 2` → Unimplemented) | HIGH (string + `>= 2` compare verified) |
| `GetReplicaPair3DOnTwistedTorus` | `0x1c893400` | per-`(i,j,k)` chip fold; called by both phases | HIGH (own page) |
| `GetPhysicalToLogicalMapping3D` | `0x1c88a280` | `[Y][X][Z] → {core0, core1}` device-id source | HIGH (fill loop + init verified) |
| `Target::LogicalDevicesPerChip` | `0x1d615b00` | Phase1 group-count multiplier (`Megacore ? 1 : cores`) | HIGH (delegates to `TpuTopology`) |
| `Target::CoresPerChip` | `0x1d615b40` | the `cores_per_chip` second-core / split predicate input | HIGH (located) |
| `TwistedTorusND::GetPhase0Cores` / `GetPhase1Cores` | `0x137d6de0` / `0x137d6ec0` | parallel per-phase core-ID vectors (cost model) | MEDIUM (located, bodies not transcribed) |

---

## 8. What Was Not Resolved

- **The exact Phase1 split predicate.** This page establishes the *group sizing* (`2K` vs `4K`) and which case splits (non-megacore 2-core). The byte-exact branch — the `Megacore(chip_cfg)` test, `CoresPerChip != 1`, and the megacore `chip_cfg[+124] <= 1` sub-condition that together gate `LABEL_50` — is decoded on [Megacore Even/Odd Split](megacore-even-odd.md). HIGH for the sizing; the predicate's full truth table is deferred there.
- **`GetPhase0Cores` / `GetPhase1Cores`.** The `ReplicaGroup` proto construction (this page) is decoded; the parallel `*Cores` device-ID vectors the cost estimator walks (`EstimatePhysicalLinksUsed` `0x1c8939c0`) were located but not transcribed. MEDIUM.
- **The `arg ≥ 1` multi-shard fold.** Both phase builders forward `arg` to the coordinate fold, which has a distinct `arg == 1` entry block; it is CHECK-unreachable behind §5's `< 2` gate in v0.0.40. Its collective semantic (what a second shard would partition) is unexercised. LOW. See [`GetReplicaPair3DOnTwistedTorus`](get-replica-pair-3d.md).

---

## Cross-References

**Twist algorithms (this section)**
- [Twisted Torus — Section Map](overview.md) — the subsystem map; cites this page's 4K/2K split summary
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the per-`(i,j,k)` `+K`-mod-`2K` coordinate fold both phases call
- [Shape Folds](shape-folds.md) — where `K`, `2K`, `num-2K-axes`, and `R` come from (`UpdateMinMaxDims` + the shape catalog)
- [Megacore Even/Odd Split](megacore-even-odd.md) — the byte-exact Phase1 split predicate behind the `4K` group count
- [TwistedTorusND::BuildStrategy](buildstrategy.md) — the per-color ring-neighbour emission side these device-id lists complement

**Sibling sections**
- [SelectNDStrategy — the ND Collective-Algorithm Picker](../collectives/strategy-nd-picker.md) — the C-ii branch that constructs `TwistedTorusND`
- [On-Pod Collectives — Section Map](../collectives/overview.md) — where the twisted strategy sits in collective lowering
- [back to index](../index.md)
