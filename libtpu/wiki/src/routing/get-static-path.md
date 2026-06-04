# GetStaticPath & Multipod

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships with full C++ symbols (`.text` VMA == file offset); every address below is a VMA. Other versions will differ.*

## Abstract

`GetStaticPath` is the deterministic single-path generator that the unicast ICI route-table emitter uses when the per-`(src,dst)` route is not already in the `RouteTargetCache`. Given two torus coordinates it produces exactly one Dimension-Order-Routed (DOR) path — one `DirectionHops` entry per axis, packed `(hop_count<<6 | polarity<<3 | orientation)` — by computing the signed per-axis distance and choosing, on each axis, the shorter of the wrap-around (torus) and direct (mesh) distances under a per-`(src,dst)` hop cap. This is the production sibling of the resilient, fault-avoiding [`RandomizedToroidalWildFirstPaths`](randomized-toroidal-wildfirst.md): same packed-`DirectionHops` output, same `CreateRoutePathFromDistance` packer, but no multi-path search and no fault avoidance — a single DOR path per call.

The page also documents the **VC-balance allocation rule as GetStaticPath feeds it**. The static path determines, hop by hop, whether the route *turns* (changes axis/polarity), whether it *crosses a dateline* (a torus wrap edge), and how far it travels along each axis. Those three properties drive the deadlock-free virtual-channel cascade in `GetNextHopAction` — turn forces VC1, a dateline-cross or a load-balance trigger forces VC2, a plain straight hop stays on VC0. The monotone "VC may only increase across a wrap" discipline is the classic dimension-order torus deadlock break; the per-axis balance threshold (`~0.2 · axis_size`) bleeds a fraction of short dateline traffic onto the high VC to even per-VC link load. The dedicated [`VC-Balance Allocation`](../ici/vc-balance-allocation.md) page owns the full deadlock-freedom argument and the threshold-construction math; this page documents the rule as the route generator's downstream consumer of the path shape.

Finally the page covers the **multipod inter-pod route emission** (`multipod::RoutingTableGenerator`, a separate translation unit `dragonfish/multipod/routing_table_generator.cc`). The multipod layer models the whole fabric as `num_pods` copies of a single-pod mesh concatenated along the X axis, builds an egress and a next-hop routing table per chip over the per-pod-local destinations re-expressed in multipod coordinates, routes inter-pod hops by a torus-only-if-short-else-mesh DOR rule, fixes the next-hop VC at 1 (inter-pod links are point-to-point — no torus cycle to break across pods), and adds channel merges on the high-latency (optical) inter-pod links. Its `GeneratesDeadlockFreeTables` returns `false`: the multipod layer defers intra-pod deadlock freedom to the per-pod table built by the VC cascade above.

For reimplementation, the contract is:

- **The static-path traversal** — the `use_limited_ici` mode gate, the torus-vs-mesh per-axis shortest-distance pick under the `max_hop` cap, and the `(hop_count<<6 | polarity<<3 | orientation)` `DirectionHops` packing emitted by `CreateRoutePathFromDistance`.
- **The VC-balance allocation rule** — the 3-way per-hop cascade (turn ⇒ VC1; straight + dateline ⇒ VC2; straight + balance ⇒ VC2; else VC0), the dateline predicate, and the balance gate keyed on a per-axis hop-count threshold.
- **The multipod inter-pod emission** — the N-pods-along-X model, `GetMultipodCoordinate`, the inter-pod `GetRoutingDistance`/`GetNextRoutingDirection` DOR rule, the egress/next-hop entry writers with their fixed VC=1, and the high-latency-link channel merge.

| | |
|---|---|
| **GetStaticPath** | `RoutingTableGenerator::GetStaticPath` @ `0x1fbdbd00` |
| **Path packer** | `slice_builder::CreateRoutePathFromDistance` @ `0x20c02040` |
| **VC cascade** | `RoutingTableGenerator::GetNextHopAction` @ `0x1fbda6a0` (VC field @ result `+0x10`) |
| **Balance gate** | `RoutingTableGenerator::GetVcBalanceUsage` @ `0x1fbdb4c0` |
| **Multipod emit** | `multipod::RoutingTableGenerator::Generate` @ `0x1fbf03a0` |
| **Source (slice_builder)** | `platforms/accel_ssw/deepsea/slice_builder/friends/routing_table_generator.cc` |
| **Source (multipod)** | `platforms/accel_ssw/deepsea/dragonfish/multipod/routing_table_generator.cc` |
| **Source (packer)** | `platforms/accel_ssw/deepsea/slice_builder/internal/routing_path.cc` |
| **Packed `DirectionHops` code** | `(hop_count<<6) \| (polarity<<3) \| (orientation & 7)`, ≤ 7 axes |

---

## GetStaticPath — the deterministic single-path generator

### Purpose

`GetStaticPath(src, dst)` returns one `IciRoutePath` (a `DirectionHops` vector + a Manhattan-norm cost) describing the DOR path from `src` to `dst`. The unicast emitter calls it on a `RouteTargetCache` miss (the cached fast path is on [`Route-Table Generation`](route-table-generation.md)). It has two modes selected by one config byte; the interesting one is the inline "limited-ICI" generator that picks the shorter of torus and mesh distance per axis.

### Entry Point

```text
RoutingTableGenerator::GetStaticPath (0x1fbdbd00)         ── deterministic path gen
  ├─ if !use_limited_ici (gen+0x14): vtable[+0xe8] GetStaticPath ── delegate to topology
  │     └─ e.g. TwistedTorusTopology::GetStaticPath (0x20b407c0) ── plain torus DOR
  ├─ topo[gen+0x20]->GetDistances (vtable+0xb8)           ── torus (wrap) distance
  ├─ topo[gen+0x28]->GetDistances (vtable+0xb8)           ── mesh (direct) distance
  └─ slice_builder::CreateRoutePathFromDistance (0x20c02040) ── pack DirectionHops
```

### Algorithm

```c
function GetStaticPath(gen, src, dst):                   // 0x1fbdbd00
    if (gen.use_limited_ici == 0):                       // BYTE[src+0x14], line 0x1fbdbd1a
        // limited-routing off: hand the whole decision to the topology's
        // own generator (plain torus shortest-DOR path).
        return gen.topo->vtable[0xe8](gen, dst)           // delegate, line 0x1fbdbd2x..ed5

    // limited-ICI on: compute a bounded path inline.
    dist_torus = gen.topo->GetDistances(src, dst)         // vtable+0xb8 (gen+0x20)  [routing_table_generator.cc:697]
    dist_mesh  = gen.mesh->GetDistances(src, dst)         // vtable+0xb8 (gen+0x28)  [:698]
    n          = gen.topo->num_dimensions()               // vtable+0x48
    chosen     = new int[n]                               // [:702] on alloc failure
    for i in 0 .. n-1:                                    // [:703] per-axis loop
        t = dist_torus[i]                                 // signed wrap distance
        m = dist_mesh[i]                                  // signed direct distance
        if abs(t) >= abs(m):                              // 0x1fbdbe43: torus not strictly shorter
            chosen[i] = m                                 //   take mesh (also wins ties)
        else if abs(t) <= gen.max_hop:                    // 0x1fbdbe52: torus within hop cap (gen+0x10)
            chosen[i] = t                                 //   take torus (the wrap path)
        else:
            chosen[i] = m                                 //   torus too long → mesh
    dim_order = gen.topo->GetDimensionOrder()             // vtable+0x70, line 0x1fbdbf02
    return CreateRoutePathFromDistance(dst, chosen, dim_order)   // 0x20c02040, line 0x1fbdbf2d
```

> **NOTE —** the per-axis pick is *shortest-distance*, not a blanket "prefer mesh". The torus (wrap) distance wins only when it is **strictly shorter** than the mesh distance **and** its magnitude is `<= max_hop` (`gen+0x10`, the per-`(src,dst)` bound). On a tie, or when the wrap exceeds the cap, the mesh (non-wrap) distance is taken. Because a wrap hop crosses a dateline, choosing torus on an axis is precisely what arms the VC2 "Crossed a dateline" branch downstream (see [the VC cascade](#the-vc-balance-allocation-rule)); the mesh path never crosses one.

### The mesh distance view

`gen+0x28` is a `slice_builder::Mesh` — the non-wrapping distance view of the same dimensions — constructed once in `InitializeGenerator` @ `0x1fbd78f7` from the primary topology's serialized `proto::Topology` (topology `vtable[+0x38]` Serialize → `Mesh` ctor; `Mesh` vtable installed at `0x1fbd78fe`, stored to `[gen+0x28]` at `0x1fbd7909`). `gen+0x20` is the primary topology (twisted-torus / resilient-toroidal), whose `GetDistances` is twist- and wrap-adjusted. The two share dimensions but differ only in whether wrap edges count — which is the entire point of the per-axis comparison.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `RoutingTableGenerator::GetStaticPath` | `0x1fbdbd00` | Mode gate + torus-vs-mesh per-axis pick | CERTAIN |
| `slice_builder::CreateRoutePathFromDistance` | `0x20c02040` | Pack `DirectionHops`, compute cost | CERTAIN |
| `TwistedTorusTopology::GetStaticPath` | `0x20b407c0` | Delegate target (limited-ICI off) | HIGH |
| `ResilientToroidalTopology::GetStaticPath` | `0x1fbe1ce0` | Delegate target (resilient topology) | HIGH |
| `InitializeGenerator` (Mesh build) | `0x1fbd78f7` | Build the `gen+0x28` non-wrap mesh | HIGH |

---

## CreateRoutePathFromDistance — the DirectionHops packer

### Purpose

Turns a signed per-axis distance vector and a dimension order into a packed `IciRoutePath`. One `DirectionHops` word per axis encodes the hop count, the polarity (sign), and the orientation (axis). This is the deterministic twin of the resilient generator's `AppendHopsToPath` — both emit the same packed code — and the format every downstream per-hop decoder (`CrossesDateline`, `GetVcBalanceUsage`) reads back.

### Algorithm

```c
function CreateRoutePathFromDistance(out, dst, dist, dim_order):  // 0x20c02040
    if dist.num_dims != dst.num_dims:                    // dimensionality guard
        return MakeError("Mismatch source coordinate dimensionality %d and "
                         "routing distance dimensionality %d")    // [routing_path.cc:106]
    if dim_order.size != dst.num_dims:
        return MakeError("Mismatch source coordinate dimensionality %d and "
                         "routing order dimensionality %d")        // [:113]

    path.cost = dst.ManhattanNorm()                       // staged in the path body, copied to path+0x28 by the tail vmovups
    for idx, dim in enumerate(dim_order):                 // walk axes in dimension order
        if idx >= 7: BUG()                                // ≤ 7 axes hard cap (0x20c0212d)
        d        = dist.GetCoordinate(dim)                // signed per-axis distance
        orient   = Direction::DimensionToOrientation(dim) // 0x20c02107
        polarity = (d <= 0) ? 2 : 1                       // setle;inc (0x20c02121/24)
        path.dir_hops[idx] = (d << 6) | ((orient & 7) + 8*polarity)   // pack (0x20c0213a..41)
    return path
```

### The packed DirectionHops code

| Field | Bits | Meaning | Source |
|---|---|---|---|
| `orientation` | `[2:0]` (`code & 7`) | The axis, via `DimensionToOrientation(dim)` | `0x20c02107` |
| `polarity` | `[5:3]` (`(code>>3) & 7`) | `(d <= 0) ? 2 : 1` — direction sign | `0x20c02121` |
| `hop_count` | `[..:6]` (`code >> 6`) | Per-axis distance, used as signed (`sar`) by readers | `0x20c0213a` |

> **GOTCHA —** the packer shifts the *signed* distance into the hop-count field (`d << 6`), while `polarity` separately and redundantly carries the sign. A reimplementation that stores `abs(d) << 6` produces the same bits *for the readers that matter*, because every consumer recovers the hop count with an arithmetic right shift (`code >> 6`, `sar`) and the orientation/polarity from the low bits — `GetVcBalanceUsage` reads `code >> 6` as the signed hop count along the axis (`0x1fbdb55c`), and `CrossesDateline(DirectionHops)` walks `(code >> 6) - 1` hops. Match the reader's `sar` semantics, not a particular sign convention in the packed word. *(The exact polarity-1/2 → POS/NEG mapping against the `proto::Direction` enum was inferred from the identical `(setle;inc)` pattern in the resilient packer, not re-read from the enum: HIGH confidence.)*

---

## The VC-balance allocation rule

> **Scope —** the full deadlock-freedom proof, the `CreateVcBalanceThreshold` `~0.2·axis_size` construction, and the dateline-side-flip predicate live on [`VC-Balance Allocation`](../ici/vc-balance-allocation.md). This section documents the rule from the route generator's side: how the static path's shape (turn / dateline-cross / hop-count) selects the per-hop VC in `GetNextHopAction`.

### Purpose

After the emitter resolves a non-terminal hop's `output_link` (via `LinkMap::GetLink` toward the next hop), it must assign that hop a virtual channel `vc ∈ {0,1,2}`. The choice is a 3-way priority cascade computed in `GetNextHopAction` @ `0x1fbda6a0` from three booleans derived from the path the static generator built. The result struct holds `{next_chip @ +8, output_link @ +0xc, vc @ +0x10}` (written at `0x1fbdaa6a`).

### Algorithm

```c
function VcForHop(gen, src, path, hop_id):               // inside GetNextHopAction 0x1fbda6a0
    // three inputs, all for the same hop:
    turned  = Direction::IsSame(this_hop_dir, next_hop_out_dir)   // 0x1fbda8fd → IsSame @0x20c025e0
              // IsSame compares the proto Direction {orient,polarity}; FALSE ⇒ the path turns
    crossed = CrossesDateline(src, path)                  // 0x1fbda8b5 → 0x1fbdb120 (per-hop)
    balance = GetVcBalanceUsage(src, this_hop_dir_hops)   // 0x1fbda921 → 0x1fbdb4c0

    if not turned:                                        // 0x1fbda9cb
        vc = 1   // VLOG "Turned, forced to low VC!" (a1b33dd, $_2 @ line 251)
    else if crossed:                                      // 0x1fbda950
        vc = 2   // VLOG "Crossed a dateline, forced to high VC!" (a1b341d, $_3 @ line 254)
    else:
        vc = 0
        if balance:                                       // 0x1fbda9ea/0x1fbda9fd
            vc = 2   // VLOG "VC load balancing, forced to high VC!" (a1b33f7, $_4 @ line 257)
    return {next_chip, output_link, vc}
```

> **QUIRK —** the first branch fires on `!IsSame` yet logs *"Turned, forced to low VC"*. `IsSame` is the *same-direction* test between this hop's direction and the next hop's outgoing direction, so `!IsSame` ⇒ the path changes axis or polarity at the next chip ⇒ a **turn**. A reimplementer who reads the branch literally as "if same-direction, VC1" inverts the rule. VC0 is the *unlabelled* straight-continuation default; VC2 is the only "high" VC and is reached two ways — a dateline wrap (deadlock break) or a balance overflow on a straight hop.

### The VC table

| Condition (per non-terminal hop) | VC | VLOG label |
|---|---|---|
| Straight (`IsSame`), no dateline, no balance | 0 | *(unlabelled — default)* |
| Turn (`!IsSame` this-vs-next direction) | 1 | `Turned, forced to low VC!` |
| Straight, crosses dateline (wrap edge) | 2 | `Crossed a dateline, forced to high VC!` |
| Straight, no dateline, balance fires (`hops ≤ threshold`) | 2 | `VC load balancing, forced to high VC!` |
| Terminal at dst (`src == dst`, no hops) | 1 | *(set via `SetUnicastVcControl(.,1)` directly)* |

### The balance gate

`GetVcBalanceUsage` @ `0x1fbdb4c0` returns true only when **all** of the following hold (the byte-exact gate chain):

```c
function GetVcBalanceUsage(gen, src, dir_hops):           // 0x1fbdb4c0
    if not CrossesDateline(src, dir_hops):  return false  // 0x1fbdb4de — only dateline hops balance
    if gen.vc_balance_enabled != 1:         return false  // BYTE[gen+0x9]==1 gate (0x1fbdb4ed)
    if (gen.flag_0x14 | crossed) != 0:      return false  // gating flag, BYTE[gen+0x14] (0x1fbdb4f4)
    orient    = dir_code & 7
    hops      = dir_code >> 6                              // signed, sar (0x1fbdb55c)
    return hops <= gen.vc_balance_threshold[orient - 1]    // setle (0x1fbdb55f/64); vector<int> @gen+0x60
```

The threshold array (`gen+0x60`, count `gen+0x68`) is built by `CreateVcBalanceThreshold` @ `0x1fbd8320` and scales roughly linearly with the axis size (`threshold ≈ round(axis_size · ~0.2 ± const)`), so only hops travelling `≤ ~⅕` of an axis are balance-shifted to VC2. The construction math and the three-way axis-"kind" selector are documented on the [VC-Balance Allocation](../ici/vc-balance-allocation.md) page.

> **NOTE —** the byte at `gen+0x14` is read by both `GetVcBalanceUsage` (this gate) and `GetStaticPath` (its `use_limited_ici` mode bit). Whether the same offset is genuinely aliased — "limited routing disables balance" — or two distinct adjacent fields collapse to one read was not disambiguated. Both code paths read `BYTE[+0x14]`. (LOW confidence on the field's dual role; CERTAIN that both reads target `+0x14`.)

---

## Multipod inter-pod route emission

### Purpose

`multipod::RoutingTableGenerator::Generate` @ `0x1fbf03a0` (a distinct translation unit, `dragonfish/multipod/routing_table_generator.cc`) builds the route table layer *above* the per-pod table. It treats the whole fabric as `num_pods` copies of a single-pod mesh laid out along the X axis (dim 0) and emits one egress table and one next-hop table per chip. Intra-pod deadlock freedom is delegated to the per-pod slice-builder table (the VC cascade above); the inter-pod links are point-to-point so the multipod next-hop VC is fixed.

### Entry Point

```text
multipod::RoutingTableGenerator::Generate (0x1fbf03a0)
  ├─ topo->TotalSize()                  (vtable+0x78)  ── total_chips  → gen+0x18
  ├─ Mesh(topo->GetDimensionSizes())    (vtable+0x50)  ── single-pod mesh
  ├─ SinglePod.TotalSize()                             ── per_pod chip count  → gen+0x90
  ├─ require total_chips % per_pod == 0                ── else "Invalid multipod topology …"
  ├─ require per_pod <= 1024 (kNumRoutingTableEntries)
  ├─ CreateDateline()                   (0x1fbf0617)
  └─ for chip in 0..total_chips-1:
        ├─ CreateEgressRoutingTable     (0x1fbf0709) ──┐ one RoutingTable each
        └─ CreateNextHopRoutingTable    (0x1fbf07a9) ──┘  (egress gen+0xb8/0xc0, nexthop gen+0xd0/0xd8)
     SetChannelMerges                   (0x1fbf2100) ── AddChannelMerge on high-latency links
```

### The model: N single pods concatenated along X

```c
function Generate(gen, topo, linkmap, opts):              // 0x1fbf03a0
    total_chips = topo->TotalSize()                       // vtable+0x78, line 0x1fbf03f2  → gen+0x18
    pod_mesh    = Mesh(topo->GetDimensionSizes())         // vtable+0x50, line 0x1fbf040a  → gen+0xb0
    per_pod     = pod_mesh.TotalSize()                    // line 0x1fbf0512               → gen+0x90
    if total_chips % per_pod != 0:                        // line 0x1fbf051e (idiv)
        return MakeError("Invalid multipod topology %s. Must be multiple %s "
                         "single pod concatenated along X dimension.")  // @0xa053c08
    if per_pod > 1024:                                    // kNumRoutingTableEntries, line 0x1fbf0608
        RetCheckFail("num_entries <= kNumRoutingTableEntries")
    CreateDateline()                                      // 0x1fbf0617
    for chip in 0 .. total_chips-1:
        coord = topo->GetCoordinate(chip)                 // vtable+0x88
        CreateEgressRoutingTable(coord, &egress[chip])    // 0x1fbf11e0
        CreateNextHopRoutingTable(coord, &nexthop[chip])  // 0x1fbf1360
    SetChannelMerges(...)                                 // 0x1fbf2100
```

> **CORRECTION (P-3-355) —** the `1024` hard cap is on `per_pod` — the **per-pod chip count** (the routing-table `num_entries`, `kNumRoutingTableEntries`), checked at `0x1fbf0608` as `num_entries <= 1024`. It is *not* a cap on the pod count. The divisibility requirement (`total_chips % per_pod == 0`, `0x1fbf051e`) is the only constraint tying `num_pods = total_chips / per_pod` to the topology. The error string at `0xa053c08` names the concatenation axis explicitly: *"concatenated along X dimension."*

### GetMultipodCoordinate — chip → pod-aligned multipod coord

`CreateEgressRoutingTable` / `CreateNextHopRoutingTable` iterate `dst = 0 .. per_pod-1` (per-**pod** count, not total), map each local destination to its multipod coordinate, and write one entry per local dst — the inter-pod hop is implied by the multipod coordinate delta.

```c
function GetMultipodCoordinate(gen, pod_index, global):   // 0x1fbf14a0
    pod_coord = SinglePod.GetCoordinate(pod_index)        // line 0x1fbf14cc
    pod_x0    = SinglePod.GetDimensionSize(0)             // line 0x1fbf14fb (per-pod X extent)
    local_x   = global.x % pod_x0                         // intra-pod X offset (idiv 0x1fbf153a)
    result.x  = global.x - local_x + pod_coord.x          // snap to pod boundary + pod identity
                                                          //   (v12 + v9 - v10, line 0x1fbf1572)
    result.y  = pod_coord.y                               // line 0x1fbf1588
    return {result.x, result.y}                           // a 2-tuple — concat axis is X (dim 0)
```

The result strips the chip's intra-pod X offset and replaces it with the pod's mesh-X position: a 2-tuple `{pod-aligned-X, pod-Y}` naming which pod (in mesh coordinates) the chip belongs to.

### Inter-pod DOR routing

```c
function GetRoutingDistance(gen, src, dst):               // 0x1fbf2e60
    dist_torus = gen.topo->GetDistances(src, dst)         // vtable+0xb8 (gen+0x8)
    dist_mesh  = gen.pod->GetDistances(src, dst)          // vtable+0xb8 (gen+0xb0)
    for i in 0 .. num_dims-1:
        t = dist_torus[i];  m = dist_mesh[i]
        if abs(t) < abs(m) and abs(t) < 3:                // 0x1fbf2f94/97 (setb; setb; and)
            chosen[i] = t                                 //   short torus wrap allowed
        else:
            chosen[i] = m                                 //   else the direct (cross-pod) mesh
    return chosen

function GetNextRoutingDirection(gen, src, dst):          // 0x1fbf26e0
    dist = GetRoutingDistance(src, dst)
    for i in 1 .. num_dims:                               // 1-based axis index
        if dist[i-1] != 0:                                // first non-zero axis (0x1fbf2771)
            return Direction{orient = i, polarity = (dist[i-1] <= 0) ? 2 : 1}  // 0x1fbf278a
    // strict lowest-index-axis-first DOR
```

> **NOTE —** the inter-pod distance allows a torus wrap **only** when it is both strictly shorter than the mesh distance **and** `< 3` hops (`0x1fbf2f97`). That `< 3` window is the line between an intra-pod short wrap (kept) and an inter-pod hop (forced onto the direct, non-wrapping mesh distance). Source lines `392`–`399` of `multipod/routing_table_generator.cc`.

### Entry writers, fixed VC, and channel merge

```c
function SetEgressRoutingTableEntry(this, src, dst, idx, table):    // 0x1fbf17c0
    if src == dst:  table.SetUnicastTerminal(idx, false)            // line 0x1fbf17fd / :147
    else:
        dir  = GetNextRoutingDirection(src)                        // line 0x1fbf184e / :39
        link = LinkMap.GetLink(this.chip_id, dir)                  // line 0x1fbf1914 / :76
        table.SetUnicastTarget(idx, link, false)                   // line 0x1fbf1936 / :80
        // NO SetUnicastVcControl on the egress hop

function SetNextHopRoutingTableEntry(this, src, dst, idx, table):  // 0x1fbf1a80
    if src == dst:
        table.SetUnicastTerminal(idx, 1)                           // line 0x1fbf1ac0 / :296
        table.SetUnicastVcControl(idx, /*vc=*/1, true)             // FIXED VC=1, line 0x1fbf1adf
    else:
        next = topo.Walk(coord, GetNextRoutingDirection(src))      // vtable+0xa0, line 0x1fbf1c00
        link = LinkMap.GetLink(...)                                // line 0x1fbf1de2
        table.SetUnicastTarget(idx, link, 1)                       // line :210
        table.SetUnicastVcControl(idx, /*vc=*/1, true)             // FIXED VC=1, line :213 / 0x1fbf1c6e
```

> **QUIRK —** the multipod next-hop table uses a **fixed VC = 1** on every hop, terminal and non-terminal alike — *not* the slice-builder dateline/turn/balance cascade of [the VC rule](#the-vc-balance-allocation-rule). Inter-pod links are point-to-point optical hops with no torus channel cycle to break across pods, so a single VC suffices. The egress table writes **no** `SetUnicastVcControl` at all (egress is the local hop into the next-hop machinery). The VC immediate `1` is byte-confirmed (`mov ecx,1` at the call site); the IDA decompiler folds the immediate into the `SetUnicastVcControl(a5)` call and does not print it.

`SetChannelMerges` @ `0x1fbf2100` applies `AddChannelMerge(MergeBehavior)` (`0x1fbf24c3`) over `LinkMap::GetHighLatencyLinks` (`0x1fbf220f`) — the optical / long inter-pod links — combined with `GetDirection` (`0x1fbf2270`) and `GetLinks` (`0x1fbf238c`). The channel merge therefore targets specifically the high-latency inter-pod fabric (OCS / optical).

`GeneratesDeadlockFreeTables` @ `0x1fbf2e40` is `xor eax,eax; ret` — it returns `false`. The multipod layer does not itself guarantee deadlock freedom: intra-pod freedom comes from the per-pod slice-builder VC cascade, inter-pod freedom from the acyclic point-to-point optical links.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `multipod::Generate` | `0x1fbf03a0` | Pod model, per-chip table build, divisibility/cap checks | CERTAIN |
| `multipod::GetMultipodCoordinate` | `0x1fbf14a0` | Chip → pod-aligned `{X,Y}` multipod coord | CERTAIN |
| `multipod::GetRoutingDistance` | `0x1fbf2e60` | Per-axis torus-if-`<3`-else-mesh distance | CERTAIN |
| `multipod::GetNextRoutingDirection` | `0x1fbf26e0` | First non-zero axis DOR direction | CERTAIN |
| `multipod::CreateEgressRoutingTable` | `0x1fbf11e0` | Loop `dst < per_pod`, fill egress entries | HIGH |
| `multipod::CreateNextHopRoutingTable` | `0x1fbf1360` | Same loop, next-hop entries | HIGH |
| `multipod::SetEgressRoutingTableEntry` | `0x1fbf17c0` | Terminal / target writer (no VC) | CERTAIN |
| `multipod::SetNextHopRoutingTableEntry` | `0x1fbf1a80` | Terminal / target writer + fixed VC=1 | CERTAIN |
| `multipod::SetChannelMerges` | `0x1fbf2100` | `AddChannelMerge` on high-latency links | HIGH |
| `multipod::GeneratesDeadlockFreeTables` | `0x1fbf2e40` | Returns `false` (defers to per-pod) | CERTAIN |
| `multipod::CreateDateline` | `0x1fbf0b20` | Build the multipod dateline (call site only) | LOW |

> **NOTE —** `CreateDateline` @ `0x1fbf0b20` was traced to its call site in `Generate` (`0x1fbf0617`) but its body was not byte-decoded. Given the inter-pod next-hop uses a fixed VC=1, the multipod dateline is presumably per-pod only (intra-pod wrap); the inter-pod mesh hops are acyclic by construction. Marked LOW until the body is decoded.

---

## Relationship to the sibling generators

| Generator | Path search | Fault handling | VC discipline | Page |
|---|---|---|---|---|
| `GetStaticPath` (this page) | Single DOR path | None | Per-hop 3-way cascade | — |
| `RandomizedToroidalWildFirstPaths` | Randomized multi-path | Fault-avoiding (wild-first) | Same cascade, same packer | [link](randomized-toroidal-wildfirst.md) |
| `multipod::Generate` (this page) | Inter-pod DOR | None | Fixed VC=1 | — |

`GetStaticPath` and `RandomizedToroidalWildFirstPaths` emit the identical packed `DirectionHops` format via the same `CreateRoutePathFromDistance` packer; the static generator is the deterministic, no-search slice-builder analogue. The multipod generator reuses the DOR idea (`GetNextRoutingDirection` is its lowest-index-axis-first DOR) but operates on multipod coordinates and a separate, simpler VC rule.

## Cross-References

- [Routing Overview](overview.md) — the route-generation → cache → emission pipeline this generator sits inside
- [GetDistances](get-distances.md) — the twisted-torus distance metric `GetStaticPath` calls (`vtable+0xb8`)
- [VC-Balance Allocation](../ici/vc-balance-allocation.md) — the full deadlock-freedom argument, the dateline-side-flip predicate, and `CreateVcBalanceThreshold` math
- [RandomizedToroidalWildFirstPaths](randomized-toroidal-wildfirst.md) — the resilient, fault-avoiding multi-path sibling that shares the packed-`DirectionHops` output
- [CreateRoutingSchedule Solver](create-routing-schedule.md) — the per-hop schedule that consumes the `{next_chip, output_link, vc}` actions emitted here
- [Route-Table Generation](route-table-generation.md) — the cached `RouteTargetCache` fast path that calls `GetStaticPath` on a miss
- [Topology Discovery](../ici/topology-discovery.md) — where the topology objects (`gen+0x20` torus, `gen+0x28` mesh) and the `LinkMap` come from
