# Route-Table Generation

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset `0x84a0000`).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — `DmaDestinationRoutingTableEntryMapper::Map` (`@0x1fc584e0`), its two reachability workers, the public entry `RoutingTableEntryForICILimitedRouting` (`@0x1fc58040`), and `GetPhysicalToLogicalMapping3D` (`@0x1c88a280`) were each cross-checked against the IDA decompile of the function; the per-axis `kCaseHopsSignToOffsets` binary-search table and the `chip_coordinates` axis-field bindings are marked **[LOW]** below · **Part XII — Interconnect & Routing** / Routing · [back to index](../index.md)

## Abstract

This page documents the **per-destination route-table entry mapper** and the **physical↔logical core mapping** the route table is keyed on. It owns three byte-exact mechanisms:

1. the **per-`(src,dst)` route-table entry** — `DmaDestinationRoutingTableEntryMapper::Map` (`@0x1fc584e0`), which takes a `(source_chip_id, destination_chip_id, ToroidalTopologyInterface&, RoutingScheme)` and returns a single `StatusOr<int>` **routing-table index**: the value the on-chip routing engine reads to forward a DMA toward `dst`. It dispatches on the `RoutingScheme` enum to one of three reachability workers (all-to-all direct, n-hop neighbor folding, two-axes);
2. the **physical→logical 3-D map** — `xla::jellyfish::GetPhysicalToLogicalMapping3D` (`@0x1c88a280`), a `vector<vector<vector<pair<long,long>>>>` sized `[Y][X][Z]`, each leaf `{core0_logical, core1_logical}`, filled by enumerating every `(replica, partition)` of a `DeviceAssignment`, resolving its flat logical-device id through `TpuTopology::LogicalDeviceForId` to a `TpuCoreLocation`, and depositing the logical id at the chip's `(cY,cX,cZ)` coordinate. This is the device-placement table the twisted-torus replica-group builders index;
3. the **route-table data-structure layout** — the `RoutingScheme` enum, the `slice_builder::Topology` adapter that wraps a `TpuTopology` into the `ToroidalTopologyInterface` the mapper consumes, and the contract that the mapper emits **one index per `(src,dst)` pair** rather than a multi-hop path.

The **unicast emission layer** above this mapper — the per-source fiber fan-out that strings these entries into the full `superpod::routing::RoutingTable` arrays (`CreateUnicastRoutingTables` → `CreateSrcDestUnicastRoutingTable` → `PopulateRoutingTable`/`GetNextHopAction`) — is **not** owned here; it lives on **[Unicast Route Emission](unicast-route-emission.md)**. The deterministic single-path generator is on **[Static-Path Generation](get-static-path.md)**; the resilient path generator on **[Randomized Toroidal Wild-First](randomized-toroidal-wildfirst.md)**; the precomputed-path cache on **[Toroidal Route Cache](toroidal-route-cache.md)**; and the per-step DMA schedule literal on **[Create-Routing-Schedule](create-routing-schedule.md)**. This page picks up at the entry mapper that turns one `(src,dst)` into one index, and at the placement table that table is implicitly keyed against.

Contract of the entry mapper as observed in the binary:

- `Map` returns a **single `int` routing-table index** (a chip id), not a hop list: the success path writes `*((int*)result + 2) = index` and `*result = 1` (ok-Status). The on-chip routing engine consumes this index to forward toward `dst` (see `tpu::RoutingTableEntryForICILimitedRouting` and `net_util::MapSrcDstCoreToRoutingTableIndex`).
- The mapper **validates `src` and `dst`** against `topology->TotalSize()` (the chip count, interface vtable slot `+0x78`) before any scheme runs; out-of-range chip ids return `FAILED_PRECONDITION` (`MakeErrorImpl<9>`).
- The scheme is the `RoutingScheme` enum (`a5`): **`0` = all-to-all (direct)**, **`1` = n-hop (one/two/four/eight neighbors)**, **`2` = two-axes**. Schemes `0` and `2` carry topology-size preconditions (all-to-all ≤ 16 chips; two-axes 2-D only and ≤ 64 chips, wrap dims of length 16).
- `GetPhysicalToLogicalMapping3D` is a **pure copy of placement**: each `(replica, partition)` is resolved to its physical chip coordinate and the logical id is stored at `mapping[cY][cX][cZ].{first|second}`; leaves are pre-initialised to `{-1,-1}` (unmapped). `per_partition` selects whether the stored id is the raw partition index or the flattened `partition·replicas + replica`.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| Entry mapper | `DmaDestinationRoutingTableEntryMapper::Map` `@0x1fc584e0` → `StatusOr<int>` |
| Mapper args | `(int src, int dst, ToroidalTopologyInterface const&, RoutingScheme)` |
| Scheme enum | `0` = all-to-all · `1` = n-hop · `2` = two-axes |
| N-hop worker | `MapOneTwoFourEightHopNeighborsReachable` `@0x1fc588a0` |
| Two-axes worker | `MapTwoAxesReachable` `@0x1fc58fa0` |
| Reachability check | `CheckReachable` `@0x1fc594c0` |
| Public entry (NHop) | `tpu::RoutingTableEntryForICILimitedRouting` `@0x1fc58040` (calls `Map(...,1)`) |
| Net-util caller (NHop) | `net_util::MapSrcDstCoreToRoutingTableIndex` `@0x1c6aea80` (calls `Map(...,1)`) |
| Topology adapter | `slice_builder::Topology::Topology` (built `@0x1fc58040` from `TpuTopology`) |
| Interface vtable slots | `+0x48` dim-sizes-cleanup · `+0x50` dim count · `+0x58` per-dim size · `+0x68` is-wrap · `+0x78` TotalSize · `+0x88` GetCoordinate(int) → `StatusOr<Coordinates>` |
| Result write (ok) | `*((int*)result + 2) = index` · `*result = 1` |
| Phys→logical map | `xla::jellyfish::GetPhysicalToLogicalMapping3D` `@0x1c88a280` |
| Map shape | `vector<vector<vector<pair<long,long>>>>` `[Y=cfg+0x5c][X=cfg+0x58][Z=cfg+0x60]` |
| Map leaf | `pair{core0_logical, core1_logical}`, init `{-1,-1}` |
| Map consumers | `StrategyND` / `TwistedTorusND` `GetPhase0/1ReplicaGroups` (`@0x137c9e80` / `@0x137cb6a0` / `@0x137cc620` / `@0x137ce240` / `@0x137d3560` / `@0x137d3de0`) |
| Source TUs | `dma_destination_routing_table_entry_mapper.cc` · `group_utils.cc` · `n_hop_route.cc` |

---

## 1. The route-table entry — `DmaDestinationRoutingTableEntryMapper::Map`

### 1.1 Role in the pipeline

The mapper is the **bottom of the route-table-generation stack**: given a source chip and a destination chip, it returns the single `int` that the routing engine programs into a route-table cell so a DMA launched at `src` reaches `dst`. For *limited ICI routing* (the n-hop scheme, the only scheme the public entry points request) that index is the **next chip a packet should be sent toward** — the destination chip id folded onto the 1/2/4/8-hop neighbor structure of the torus. The unicast emission layer (**[Unicast Route Emission](unicast-route-emission.md)**) calls the analogous per-`(src,dst)` build for the *full slice* route table; this mapper is the per-cell primitive used by the runtime's `n_hop_route.cc` path and by `net_util` when building the routing-table-index mapping table.

`Map` is a method on `accel_ssw::deepsea::slice_builder::viperlite_pod::DmaDestinationRoutingTableEntryMapper`. Its first argument (`a1`) is the **return slot** for a `StatusOr<int>` (the absl C++ ABI returns the aggregate through a hidden pointer); the result int lives at `result+0x8` (`*((int*)result + 2)`), the Status discriminant at `result+0x0`.

### 1.2 Signature and validation

```cpp
// @0x1fc584e0  (dma_destination_routing_table_entry_mapper.cc)
StatusOr<int> DmaDestinationRoutingTableEntryMapper::Map(
    int src,                              // a2
    int dst,                              // a3
    const ToroidalTopologyInterface& topo,// a4 (abstract base; vtable-dispatched)
    RoutingScheme scheme);                // a5  {0,1,2}
```

The first two operations are bound checks against the **chip count** (`topo.TotalSize()`, interface vtable slot `+0x78`):

```text
n = topo.TotalSize()                        // (*(int(*)(...))(*(qword*)topo + 120))(topo)
if (src < 0 || src >= n)  return INVALID_ARGUMENT("Invalid source chip ID")
if (dst < 0 || dst >= n)  return INVALID_ARGUMENT("Invalid destination chip ID")
```

> Both checks call interface slot `+0x78` (`+120`) for `n`; the strings `"Invalid source chip ID"` (len 22, line 34) and `"Invalid destination chip ID"` (len 27, line 37) are `MakeErrorImpl<9>` (absl `kFailedPrecondition` = `9`, i.e. FAILED_PRECONDITION — **not** INVALID_ARGUMENT) at the TU `dma_destination_routing_table_entry_mapper.cc`. (All of the mapper's own topology-precondition errors use `<9>`; only the `not-reachable` and `unsupported-scheme` paths use `MakeErrorImpl<3>` = `kInvalidArgument`.)

### 1.3 Scheme dispatch + preconditions

`Map` records the scheme in `v30` and applies scheme-specific topology preconditions before delegating:

| `scheme` | Name | Precondition (checked in `Map`) | Worker |
|----------|------|---------------------------------|--------|
| `2` | two-axes | dim count == 2 (slot `+0x48` returns 2) **and** `TotalSize() <= 64`; every is-wrap dim must have length 16 (else `"All wrap-around dimensions must be of length 16"`); each axis must be ≤ 8 (`"Two axes routing must use axes of size <= 8"`) | `MapTwoAxesReachable` `@0x1fc58fa0` |
| `1` | n-hop | none beyond src/dst bounds | `MapOneTwoFourEightHopNeighborsReachable` `@0x1fc588a0` |
| `0` | all-to-all | `TotalSize() <= 16` (else `"All to all routing is only supported for slices with <= 16 chips"`) | inline (direct, see §1.4) |
| other | — | — | `INVALID_ARGUMENT("Unsupported routing scheme: %d")` (line 94) |

> The `scheme == 2` branch first checks `(*(...)(*topo + 72))(topo) == 2` — interface slot `+0x48` returns the dim count — with error `"Two axes routing is only supported for 2-D topologies"` (len 53), then `TotalSize() <= 64` with `"Two axes routing is only supported for slices with <= 64 chips"` (len 62). It then iterates the per-dim sizes (slot `+0x50` total, slot `+0x58` per-dim `StatusOr<int>`) checking `size < 9` (`"...axes of size <= 8"`, line 61) and, for is-wrap dims (slot `+0x68` `StatusOr<bool>`), `size >= 16` (`"All wrap-around dimensions must be of length 16"`, line 77). The `scheme == 0 && TotalSize() >= 17` guard precedes the loop. The dispatch at the tail is `if (v30==2) MapTwoAxes else if (v30==1) MapOneTwoFourEight else if (v30) <error> else <direct>`.

### 1.4 The all-to-all direct case (`scheme == 0`)

When the scheme is all-to-all and the chip count is small (≤ 16), every chip is a one-hop neighbor of every other; the routing-table index **is just the destination chip id**:

```text
// scheme == 0, fall-through after the per-dim wrap loop
*((int*)result + 2) = dst;     // routing_table_index = destination chip id
*result = 1;                   // ok Status
```

> The `else` arm of the tail dispatch (`v30 == 0`) writes `*((_DWORD*)v32 + 2) = v31` where `v31 = a3 = dst`, and `*v5 = 1`. No worker is invoked — direct routing means "send straight to the destination chip".

### 1.5 The n-hop worker (`MapOneTwoFourEightHopNeighborsReachable`)

This is the worker the production limited-ICI path uses (every public caller requests `scheme == 1`). It resolves `src` and `dst` to torus `Coordinates` (interface slot `+0x88` `GetCoordinate(int) → StatusOr<Coordinates>`), confirms `dst` is reachable from `src` (`CheckReachable`, §1.6), then computes the **next-hop chip id** by folding the source→destination displacement onto the {1,2,4,8}-hop neighbor lattice.

```text
// @0x1fc588a0
src_coord = topo.GetCoordinate(src)                       // slot +0x88
dst_coord = topo.GetCoordinate(dst)                       // slot +0x88
reach = CheckReachable(src_coord, dst_coord, topo)        // @0x1fc594c0
if (!reach.reachable)
    return INVALID_ARGUMENT("Chip ID %d is not reachable from chip ID %d "
                            "for this topology, %s")        // line 196
// reach carries {reachable: bit32, hops: int}
hops = reach.hops
sx = src_coord.x();  dx = dst_coord.x()
sy = src_coord.y();  axis_len = topo.dimsize(0)            // slot +0x88 with arg 0
GetHopLength(hops)                                         // validates |hops| ∈ {1,2,4,8}
routing_case = (sx == dx) ? ((axis_len <= 4 ? 3 : 1) + (sy&1))   // Y-axis move
                          : ((dx&1) /* X-axis move */ + 1)
sign = (hops <= 0) ? NEGATIVE(2) : POSITIVE(1)
// binary search kCaseHopsSignToOffsets for {routing_case, |hops|, sign}
offsets = kCaseHopsSignToOffsets[{routing_case, hop_len, sign}]  // RET_CHECK if absent
routing_table_index = fold(offsets, sx, sy, axis_len) & 7 | 8    // see [LOW] below
RET_CHECK(routing_table_index != src)                     // line 387
*((int*)result + 2) = routing_table_index
*result = 1
```

> The worker reads `src`/`dst` coordinates via slot `+0x88`, calls `CheckReachable` (`@0x1fc594c0`), and on unreachable emits `MakeErrorImpl<3>` with the format string `"Chip ID %d is not reachable from chip ID %d for this topology, %s"` (len 65, line 196). The reachability result is a `StatusOr<{reachable,hops}>` whose bit `& 0x100000000` is the reachable flag and whose low dword is the hop count. `GetHopLength` is called on the hop count (line 356). A `RET_CHECK(routing_table_index != source_chip_id)` (string `"routing_table_index != source_chip_id"`, line 387) guards against a self-loop, with a VLOG dump (`"routing_table_index is equal to source_chip_id for: ..."`, line 382).

> **[LOW]** The exact arithmetic of `kCaseHopsSignToOffsets` — a static lookup table searched by two interleaved binary searches over a 512-byte region of 16-byte `{routing_case, hop_len, sign, offset}` records (`kCaseHopsSignToOffsets.contains({routing_case, hop_len, hops>0?POSITIVE:NEGATIVE})`, line 363) — and the final `(offset + sy + hops*axis_len) & 7 | 8` fold (`v41 = ((unsigned)offset + v79 + v16*v17 ...) & 7 | 8`) were traced to their operands but not reduced to a closed-form per-axis index. The structure (search the table for the `(case, |hops|, sign)` key, add the stored offset to a strided coordinate, mask to the axis, set bit 3) is byte-confirmed; the precise meaning of each `routing_case` (0/1/3 + parity) is inferred from the `sx==dx` / `axis_len<=4` branch shape.

### 1.6 The reachability check + two-axes worker

`CheckReachable` (`@0x1fc594c0`) takes two `superpod::routing::Coordinates` and the topology and returns a `StatusOr<{bool reachable, int hops}>` — whether `dst` is a valid n-hop neighbor of `src` along a single axis, and the signed hop count. `MapTwoAxesReachable` (`@0x1fc58fa0`) is the `scheme == 2` analog: it resolves both coordinates (slot `+0x88`), and folds a displacement that may move along **both** axes (the small ≤ 64-chip 2-D case), again validating reachability before producing the index. Both workers share the `dma_destination_routing_table_entry_mapper.cc` TU and the same `StatusOr<int>` return contract.

> `MapTwoAxesReachable @0x1fc58fa0` opens with the same `GetCoordinate(src)` (slot `+0x88`) pattern as the n-hop worker and is reached only from the `scheme == 2` arm of `Map`. The two-axes precondition loop (axes ≤ 8, wrap dims == 16) is enforced by `Map` before the call.

---

## 2. The public entry points

The mapper is private to `viperlite_pod`; runtime code reaches it through two wrappers, both of which request **`RoutingScheme = 1` (n-hop)**.

### 2.1 `tpu::RoutingTableEntryForICILimitedRouting`

```cpp
// @0x1fc58040  (n_hop_route.cc — learning/45eac/tpu/runtime/hal/internal/vxc/)
StatusOr<int> RoutingTableEntryForICILimitedRouting(
    const tpu::TpuTopology& topo, int src, int dst);
```

This is the **HAL-level limited-ICI route query**. It validates `src`/`dst` against `topo.chip_count()` (`*((int*)&topo + 28)`), requires a **2-D topology** (`*((int*)&topo + 24) == 1`, i.e. Z dim == 1, else `"toplogy must be 2d for limited ICI routing, z: %d"` [sic — the binary's own spelling]), then builds a `slice_builder::Topology` adapter from the raw `TpuTopology` and calls the mapper:

```text
is_wrap_x = topo[+160] & 1
is_wrap_y = topo[+161] & 1
dim_sizes = topo[+88]                       // (qword*)topo + 11
Topology adapter(&dim_sizes, /*ndims=*/2, {is_wrap_x, is_wrap_y}, /*?*/2, ...)
idx = DmaDestinationRoutingTableEntryMapper::Map(adapter, src, dst, /*scheme=*/1)
return ok(idx) ? idx : -1     // on error, returns index -1
```

> The function checks `a3 >= topo[+0x70/4]` (chip count, field at int-offset 28) with `"Invalid source chip id "` / `"Invalid destination chip id "`, then `topo[+0x60/4] != 1` (int-offset 24, the Z size) with `"toplogy must be 2d for limited ICI routing, z: "`. It constructs `slice_builder::Topology` from `topo[+0xa0]`/`topo[+0xa1]` (wrap bits) and `topo[+0x58]` (dim-size span), then calls `DmaDestinationRoutingTableEntryMapper::Map(&result, src, dst, &adapter, 1)`. On success it writes `*((int*)this + 2) = mapped_index`; on the mapper returning an error Status it writes `-1` and unrefs the error.

### 2.2 `net_util::MapSrcDstCoreToRoutingTableIndex`

`xla::jellyfish::net_util::MapSrcDstCoreToRoutingTableIndex` (`@0x1c6aea80`) is the compile-side analog: it likewise builds the topology adapter and calls `Map(...,1)`, used by `GenerateRoutingTableIndexMappingTable` (`@0x1c6a2b80`) to materialise the **full src×dst routing-table-index table** the runtime indexes per DMA (the `routing_table_index` field of the DMA descriptor; see **[Unicast Route Emission](unicast-route-emission.md)** for how these indices populate `superpod::routing::RoutingTable`).

> `MapSrcDstCoreToRoutingTableIndex @0x1c6aea80` contains the call `DmaDestinationRoutingTableEntryMapper::Map(&result, src, dst, adapter, 1)` (line 60 of its decompile), confirming both public callers fix `scheme == 1`.

---

## 3. The route-table data structure

### 3.1 What the mapper emits

The mapper's output is **one `int32` per `(src,dst)` cell**, not a packed multi-field entry. The success path is uniformly:

```text
*((int*)result + 2) = routing_table_index;   // the int the routing engine reads
*result = 1;                                  // absl::Status == OK (discriminant 1)
```

The full route table the runtime programs is therefore a dense `src × dst` array of these `int32` indices, built by `GenerateRoutingTableIndexMappingTable` calling the mapper for every pair. The richer per-link `RoutingTable` rows (`unicast_target` / `unicast_terminal` / `vc_control`) are produced one level up by the emission layer; this page's mapper supplies the **next-hop index** those rows fold in.

### 3.2 The `ToroidalTopologyInterface` ABI

The mapper never touches a concrete topology — it dispatches through the abstract `accel_ssw::deepsea::slice_builder::ToroidalTopologyInterface` vtable, so any slice/pod topology can drive it. The slots exercised by `Map` and its workers:

| Vtable offset | Method (inferred) | Returns | Used by |
|---------------|-------------------|---------|---------|
| `+0x28` (`+40`) | topology-name / to-string | `string` | n-hop error message (`%s`) |
| `+0x48` (`+72`) | dim count | `int` | two-axes "must be 2-D" check |
| `+0x50` (`+80`) | dim-sizes accessor (cleanup ptr) | — | per-dim precondition loop |
| `+0x58` (`+88`) | per-dim size | `StatusOr<int>` | axes ≤ 8 / wrap == 16 checks |
| `+0x68` (`+104`) | is-wrap(dim) | `StatusOr<bool>` | wrap-dim length check |
| `+0x78` (`+120`) | `TotalSize()` (chip count) | `int` | src/dst bounds, scheme caps |
| `+0x88` (`+136`) | `GetCoordinate(int)` | `StatusOr<Coordinates>` | both workers |

> Every slot above is taken from a `(*(...)(*(qword*)a4 + N))(a4, ...)` indirect call in `Map` / `MapOneTwoFourEightHopNeighborsReachable`. The `slice_builder::Topology` concrete class (constructed in `RoutingTableEntryForICILimitedRouting @0x1fc58040`, vtable `off_220174F0`) is the implementation the HAL path uses.

### 3.3 The `slice_builder::Topology` adapter

`RoutingTableEntryForICILimitedRouting` builds the adapter as `Topology(dim_size_span, ndims=2, wrap_bits[2], 2, ...)`. The adapter holds the two dim sizes, the two wrap bits (X, Y), and exposes them through the interface vtable. It is destroyed at function exit (three `free` calls release the dim-size span, a scratch buffer, and the wrap-bit storage).

---

## 4. The physical→logical 3-D map — `GetPhysicalToLogicalMapping3D`

### 4.1 Why it exists

The route table is keyed on **chip coordinates**, but a collective is expressed over **logical device ids** (replica/partition). `GetPhysicalToLogicalMapping3D` is the inverse-placement table that lets the twisted-torus replica-group builders translate a physical `(Y,X,Z)` torus coordinate back into the `{core0, core1}` logical ids occupying that chip — the bridge between the device-assignment world and the route-table world. It is consumed by `StrategyND`/`TwistedTorusND` `GetPhase0/1ReplicaGroups` (and their `NDNway` variants), which fold a twist coordinate into a replica pair.

### 4.2 Signature and shape

```cpp
// @0x1c88a280  (group_utils.cc — platforms/xla/service/jellyfish/lowering/)
std::vector<std::vector<std::vector<std::pair<long,long>>>>
GetPhysicalToLogicalMapping3D(const Target& tgt,
                              const DeviceAssignment* da,
                              bool per_partition);
```

`chip_cfg = tgt[+0x3b8]` (an `int*`; the page treats it as `v5` with `v5[N]` int fields). The three nesting levels are sized from the chip config:

| Level | Size source | Index coordinate | Leaf |
|-------|-------------|------------------|------|
| outer | `chip_cfg[23]` = `[cfg+0x5c]` = **Y** | `cY` = `chip_coordinates()` field +4 | `vector<vector<pair>>` (24 B) |
| middle | `chip_cfg[22]` = `[cfg+0x58]` = **X** | `cX` = `chip_coordinates()` field +0 | `vector<pair>` (24 B) |
| inner | `chip_cfg[24]` = `[cfg+0x60]` = **Z** | `cZ` = `chip_coordinates()` field +8 | `pair<long,long>` (16 B) |

Each leaf `pair` is initialised to `{-1, -1}`:

```text
*(qword*)(leaf)      = -1;   // .first  = core0_logical (unmapped)
*(qword*)(leaf + 8)  = -1;   // .second = core1_logical (unmapped)
```

> The outer vector is `operator new(24 * Y)` with `Y = v5[23]`; each middle vector is grown to `X = v5[22]` via `vector<…>::__append`; each inner to `Z = v5[24]` `pair`s; the `{-1,-1}` init is the two `*(qword*)(... ) = -1` stores inside the `LABEL_34` fill loop. The element strides match: outer `24 * v12`, middle `lea [r15+r15*2]` (= `·24`), inner `16 * v77` (the pair).

### 4.3 The device-assignment fill loop

```text
replica_count   = da[+0x8] & 0x7fffffff
partition_count = da[+0x0] & 0x7fffffff
for r in [0, replica_count):                 // outer (i)
  for p in [0, partition_count):             // inner (v41)
    // 1. flat logical-device id = Σ coord[i]·stride[i], coord={p, r}, stride=da[+0]
    flat = horner(coord = {p, r}, stride = da[+0x0])     // imul/add chain
    logical_id = da.flat_id_table[flat]      // (int*)(da[+0x10])[flat]
    // 2. resolve to a TpuCoreLocation
    loc = TpuTopology::LogicalDeviceForId(chip_cfg, /*core_type=*/0, logical_id)
    (cY, cX, cZ) = loc.chip_coordinates()    // struct {coord0+0, coord1+4, coord2+8, valid+12}
    cY = field+4;  cX = field+0;  cZ = field+8
    // 3. choose the stored logical id
    stored = per_partition ? p : (p·replica_count + r)
    // 4. choose the megacore slot from the location's "second core" flag
    if (loc.is_second_core /* v75[52] != 0 */)
        mapping[cY][cX][cZ].second = stored      // core1
    else
        mapping[cY][cX][cZ].first  = stored      // core0
```

> The loop bounds are `da[+0x8] & 0x7fffffff` (replica) and `da[+0x0] & 0x7fffffff` (partition). The flat-id is a Horner-style `imul/add` chain over the `{p, r}` coordinate and the `da[+0x0]` stride vector (the 8-way-unrolled multiply chain at `LABEL_57`/the unrolled body). `LogicalDeviceForId(chip_cfg, 0, da.flat_id_table[flat])` is called with `core_type = 0` and the flat-id table at `da[+0x10]` (`*((qword*)v38 + 2)`). `chip_coordinates()` is read three times into `v76[1]`=cY, `v76[0]`=cX, `v77`=cZ. The stored id is `v57 = p + replica_count·r` unless `!per_partition`-arg... — precisely, `v57 = v88 + v85*r`; when `!(byte)v79` (the `per_partition` arg is false) `v57 = (unsigned)r`. The slot is chosen on `*(dword*)&v75[52]` (the `chip_coordinates` struct's `valid`/second-core field): nonzero → `.second` (`+8` of the pair), zero → `.first`. All three levels are bound-checked (`BUG()` on out-of-range).

> **[LOW]** The exact named `TpuChipConfig`/`TpuCoreLocation` fields behind `chip_coordinates()` (`coord0`=cX, `coord1`=cY, `coord2`=cZ, and the `+52` second-core/valid flag) and the `per_partition` true/false semantics (`v57 = p + replicas·r` vs `v57 = r`) are byte-confirmed at the offset level; the binding of each offset to a proto field name is inferred from the VLOG dump labels (`"replica:"`, `"model id:"`, `"row:"`, `"col:"`, `"dim_z:"` at `group_utils.cc:285`).

### 4.4 Diagnostic VLOG

Each fill iteration, when `VLOG(2)` is enabled (`group_utils.cc:285`), emits:

```text
device assignment. replica: <r> model id: <p> row: <cY> col: <cX> dim_z: <cZ>
```

> The `LogMessage` chain at `LABEL_73` emits exactly these labels (`"device assignment. replica: "`, `" model id: "`, `" row: "`, `" col: "`, `" dim_z: "`) with `r`, `p`, `cY`, `cX`, `cZ` operands, gated on `VLogSite::SlowIsEnabled2(..., dword_2236DE58)`.

### 4.5 Consumers

The map is read by the replica-group builders to turn a twist coordinate `(cY,cX,cZ)` into a `{core0_logical, core1_logical}` pair:

| Consumer | Address |
|----------|---------|
| `StrategyND::GetPhase0ReplicaGroups` | `@0x137c9e80` |
| `StrategyND::GetPhase0ReplicaGroupsNDNway` | `@0x137cb6a0` |
| `StrategyND::GetPhase1ReplicaGroups` | `@0x137cc620` |
| `StrategyND::GetPhase1ReplicaGroupsNDNway` | `@0x137ce240` |
| `TwistedTorusND::GetPhase0ReplicaGroups` | `@0x137d3560` |
| `TwistedTorusND::GetPhase1ReplicaGroups` | `@0x137d3de0` |

> All six functions reference `GetPhysicalToLogicalMapping3D` in their decompiled bodies (cross-checked by symbol grep). These are the megacore replica-group emitters that append the two core ids of each physical chip into the HLO `ReplicaGroup` device lists.

---

## 5. The `CollectivePermute` route table (cross-reference)

`CollectivePermute` does **not** route through the per-`(src,dst)` mapper above. It compiles its permutation directly from the HLO into a flat **source→target id table** (ConstantMapper Type `0xb`) plus a per-step DMA schedule literal (Type `5`):

- `CreateCollectivePermutePairs` (`@0x1347aa40`) reads `HloInstruction::source_target_pairs()`, `channel_id()`, and `Target::ReplicaCount()` into a `vector<pair<long,long>>` of `(source_id, target_id)`.
- `CreateCollectivePermuteTransfers` (`@0x13470fe0`) decodes each id into a logical coordinate, maps it through the `LogicalTopologyInfo` coordinate→core-id table (`+0x10`), and emits one 16-byte `net_router::Transfer {src_core@0, src_index@4, dst_core@8, dst_index@0xc}` per `(pair × buffer × read/write)`.
- `CollectivePermuteEmitter::GenerateConstants` (`@0x1346ff60`) registers the flattened `s32[]` table as `AddConstant(0xb, …)` and the route schedule (over the same `Transfer`s) as `AddConstant(5, …)`.

The Type-5 schedule literal and Type-0xb table layout are documented on the collective-lowering side — see **[Create-Routing-Schedule](create-routing-schedule.md)** and the **[Collectives Overview](../collectives/overview.md)**. The division of labour: Type `0xb` answers *who* sends to whom (the placement permutation, analogous to the `GetPhysicalToLogicalMapping3D` placement keying above); Type `5` answers *how/when* (the per-step, per-direction DMA program). The route-table-index mapper of §1 is the unicast / auto-routing path; CollectivePermute uses the explicit-schedule path. Both ultimately program ICI DMAs, but through orthogonal mechanisms.

---

## 6. Function map

| Function | Address | Role |
|----------|---------|------|
| `DmaDestinationRoutingTableEntryMapper::Map` | `0x1fc584e0` | per-`(src,dst)` → `StatusOr<int>` routing-table index; scheme dispatch |
| `…::MapOneTwoFourEightHopNeighborsReachable` | `0x1fc588a0` | n-hop (`scheme==1`) worker — the production limited-ICI path |
| `…::MapTwoAxesReachable` | `0x1fc58fa0` | two-axes (`scheme==2`) worker (≤ 64-chip 2-D) |
| `…::CheckReachable` | `0x1fc594c0` | `(src_coord, dst_coord)` → `{reachable, hops}` along an axis |
| `tpu::RoutingTableEntryForICILimitedRouting` | `0x1fc58040` | HAL entry; builds adapter, calls `Map(...,1)`, returns `-1` on error |
| `net_util::MapSrcDstCoreToRoutingTableIndex` | `0x1c6aea80` | compile-side entry; calls `Map(...,1)` |
| `net_util::GenerateRoutingTableIndexMappingTable` | `0x1c6a2b80` | builds the full src×dst index table |
| `xla::jellyfish::GetPhysicalToLogicalMapping3D` | `0x1c88a280` | `[Y][X][Z]` → `{core0,core1}` placement map |
| `tpu::TpuTopology::LogicalDeviceForId` | (called) | flat logical id → `TpuCoreLocation` (core_type 0) |
| `tpu::TpuCoreLocation::chip_coordinates` | (called) | `TpuCoreLocation` → `(cX@0, cY@4, cZ@8, valid@12)` |

---

## 7. Diagnostic strings

| String (in `dma_destination_routing_table_entry_mapper.cc`) | Line | Status code | Condition |
|-------------------------------------------------------------|------|-------------|-----------|
| `Invalid source chip ID` | 34 | FAILED_PRECONDITION (`<9>`) | `src` out of `[0, TotalSize)` |
| `Invalid destination chip ID` | 37 | FAILED_PRECONDITION (`<9>`) | `dst` out of `[0, TotalSize)` |
| `Two axes routing is only supported for 2-D topologies` | 50 | FAILED_PRECONDITION (`<9>`) | `scheme==2`, dim count != 2 |
| `Two axes routing is only supported for slices with <= 64 chips` | 54 | FAILED_PRECONDITION (`<9>`) | `scheme==2`, `TotalSize > 64` |
| `Two axes routing must use axes of size <= 8` | 63 | FAILED_PRECONDITION (`<9>`) | `scheme==2`, axis size ≥ 9 |
| `All to all routing is only supported for slices with <= 16 chips` | 44 | FAILED_PRECONDITION (`<9>`) | `scheme==0`, `TotalSize >= 17` |
| `All wrap-around dimensions must be of length 16` | 77 | FAILED_PRECONDITION (`<9>`) | wrap dim length < 16 |
| `Chip ID %d is not reachable from chip ID %d for this topology, %s` | 196 | INVALID_ARGUMENT (`<3>`) | `CheckReachable` false |
| `Unsupported routing scheme: %d` | 94 | INVALID_ARGUMENT (`<3>`) | scheme not in `{0,1,2}` |
| `routing_table_index != source_chip_id` | 387 | RET_CHECK | computed index == src (self-loop) |
| `toplogy must be 2d for limited ICI routing, z: %d` (in `n_hop_route.cc`) | 40 | INVALID_ARGUMENT (`InvalidArgumentErrorBuilder`) | Z dim != 1 |

> All strings above were read at their referenced `MakeErrorImpl` / `InvalidArgumentErrorBuilder` / `RetCheckFailSlowPath` call sites in the two TUs. The mapper's own six precondition errors are `MakeErrorImpl<9>` (absl `kFailedPrecondition`); the `not-reachable` (line 196) and `unsupported-scheme` (line 94) paths are `MakeErrorImpl<3>` (`kInvalidArgument`); and the three `n_hop_route.cc` errors (`Invalid source/destination chip id`, `toplogy must be 2d`) are `util::InvalidArgumentErrorBuilder`.

---

## Cross-References

- **[Routing Overview](overview.md)** — the route-generation → cache → emission pipeline this mapper sits at the bottom of.
- **[Unicast Route Emission](unicast-route-emission.md)** — the per-source fiber fan-out that strings these entries into the full `superpod::routing::RoutingTable` arrays.
- **[Static-Path Generation](get-static-path.md)** — the deterministic single-path generator the non-cached emission uses.
- **[Randomized Toroidal Wild-First](randomized-toroidal-wildfirst.md)** — the resilient path generator and the route-cache schema.
- **[Toroidal Route Cache](toroidal-route-cache.md)** — the precomputed per-`(src,dst)` path cache.
- **[Create-Routing-Schedule](create-routing-schedule.md)** — the per-step DMA schedule literal (Type 5) and the CollectivePermute Type-0xb table.
- **[Collectives Overview](../collectives/overview.md)** — how replica groups (built via `GetPhysicalToLogicalMapping3D`) drive the on-pod collectives.
