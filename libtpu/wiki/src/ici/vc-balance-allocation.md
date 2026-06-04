# VC-Balance Allocation

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships with full C++ symbols (`.text` VMA == file offset); every address below is a VMA. Other versions will differ.*

## Abstract

The ICI fabric is a twisted torus, and a torus deadlocks. If every chip routes dimension-order and a packet may ride any virtual channel on any link, the wrap edges close a cyclic channel-dependency graph: a ring of packets each holding one link's buffer and waiting on the next can stall forever. libtpu breaks that cycle with the classic **dateline / VC-class discipline** — a packet may only step *up* to a higher virtual channel after it crosses a designated wrap boundary (the *dateline*), so the channel-dependency graph is acyclic by construction. This page documents the allocation **rule** that implements that discipline: how each non-terminal hop of a static route is assigned a VC `∈ {0,1,2}`, why the assignment is deadlock-free, and how a per-axis load-balance threshold bleeds a controlled fraction of traffic onto the high VC to even per-VC link utilisation.

The rule is a 3-way priority cascade inside `RoutingTableGenerator::GetNextHopAction` @ `0x1fbda6a0`, keyed on three predicates over the hop and the path: does the route *turn* at the next chip (`Direction::IsSame`), does this hop *cross a dateline* (`CrossesDateline`), and does *VC load-balancing* fire on this hop (`GetVcBalanceUsage`). A turn forces the low VC (1); a dateline-cross or a balance trigger on a straight hop forces the high VC (2); a plain straight hop keeps the default VC (0). The dateline predicate is a per-axis wrap-boundary **side-flip** test; the balance predicate gates on a per-axis hop-count threshold built by `CreateVcBalanceThreshold` @ `0x1fbd8320` to scale roughly as `~0.2 · axis_size`.

This page owns the **deadlock-freedom argument, the `CrossesDateline` side-flip predicate, the `GetVcBalanceUsage` gate, and the `CreateVcBalanceThreshold` construction math** — the slice of the route generator that the [`GetStaticPath`](../routing/get-static-path.md) page deferred here. The route generator that *produces* the path whose shape feeds this rule (the torus-vs-mesh per-axis distance pick and the `(hop_count<<6 | polarity<<3 | orientation)` `DirectionHops` packing) is documented there and is not re-derived below. The final section covers the **multipod inter-pod route emission**, which deliberately *does not* use this cascade — it pins the inter-pod VC at a constant because point-to-point optical links carry no torus cycle to break.

For reimplementation, the contract is:

- **The 3-way VC cascade** — the exact branch order in `GetNextHopAction` (turn ⇒ VC1; straight + dateline ⇒ VC2; straight + balance ⇒ VC2; else VC0) and the result-struct VC field.
- **The dateline predicate** — the per-axis side-flip test in `CrossesDateline` (two flavours: a fixed `axis_size-1` boundary, or an explicit per-axis dateline coordinate), and the path-walking overload that ORs the per-hop crossings.
- **The balance gate and threshold** — the four-way gate chain in `GetVcBalanceUsage` and the `~0.2 · axis_size` per-axis threshold built by `CreateVcBalanceThreshold`, including its three-way axis-"kind" selector.
- **The deadlock-freedom invariant** — why monotone VC-increment-on-wrap makes the channel-dependency graph acyclic, and why the multipod layer can skip it.

| | |
|---|---|
| **VC cascade** | `RoutingTableGenerator::GetNextHopAction` @ `0x1fbda6a0` (VC field at result `+0x10`) |
| **Dateline predicate (Coord,Coord)** | `RoutingTableGenerator::CrossesDateline` @ `0x1fbdb120` |
| **Dateline predicate (Coord,DirectionHops)** | `RoutingTableGenerator::CrossesDateline` @ `0x1fbdd200` |
| **Balance gate** | `RoutingTableGenerator::GetVcBalanceUsage` @ `0x1fbdb4c0` |
| **Threshold construction** | `RoutingTableGenerator::CreateVcBalanceThreshold` @ `0x1fbd8320` |
| **Turn predicate** | `slice_builder::Direction::IsSame` @ `0x20c025e0` |
| **VC count** | 3 (`{0, 1, 2}`); VC0 default, VC1 "low" (turn), VC2 "high" (wrap / balance) |
| **Source (slice_builder)** | `platforms/accel_ssw/deepsea/slice_builder/friends/routing_table_generator.cc` |
| **Source (multipod)** | `platforms/accel_ssw/deepsea/dragonfish/multipod/routing_table_generator.cc` |

---

## The deadlock-freedom invariant

### Purpose

The ICI topology is a (twisted) torus: every axis wraps. Dimension-order routing (DOR) — route fully along axis 0, then axis 1, and so on — eliminates *turn*-induced cycles within a mesh, but the wrap edges of a torus reintroduce a cycle on each ring. The standard fix, due to Dally and Seitz, partitions each physical link into virtual channels and assigns a **dateline** to every ring: a packet uses the low VC class until it crosses the dateline, then the high VC class for the remainder of that axis. Because a packet's VC class only ever *increases* across a dateline and never decreases, no cyclic wait can form across the VC-expanded channel-dependency graph. libtpu implements exactly this, with one extra wrinkle (a load-balance shift) folded into the high VC.

### The invariant, stated

```text
For every non-terminal hop of every static route:
  VC class is monotone non-decreasing along the path, and strictly increases
  exactly when the hop crosses a dateline (a wrap edge). A turn drops to VC1
  but a turn in DOR only happens between completed axes, so it never re-enters
  a ring already traversed on a higher VC.

  ⇒ The channel-dependency graph over (physical link × VC) is acyclic.
  ⇒ The fabric is deadlock-free under DOR.
```

The three VC classes map onto this as: **VC0** is the straight-continuation default (a DOR run that has not yet wrapped); **VC1** ("low") is forced by a *turn* — the point where the route finishes one axis and begins the next; **VC2** ("high") is the post-dateline class, forced whenever a hop crosses a wrap edge. The load-balance trigger reuses VC2 for a subset of *straight, non-wrapping* hops (see [the balance gate](#the-balance-gate)) purely to even per-VC link load — it does not affect the deadlock argument, because those hops are gated to *also* be dateline hops (the balance gate's first condition is `CrossesDateline`), so they were already legitimately on the high VC class.

> **NOTE —** the multipod layer ([below](#multipod-inter-pod-route-emission)) returns `false` from `GeneratesDeadlockFreeTables` and uses a *fixed* VC=1 on inter-pod hops. That is not a violation of this invariant — the inter-pod optical links are point-to-point with no torus ring, so there is no cycle to break. Intra-pod deadlock freedom comes entirely from the per-pod slice-builder table that this cascade produces.

---

## The 3-way VC cascade

### Purpose

After the unicast emitter resolves a non-terminal hop's `output_link` (toward the next chip), `GetNextHopAction` @ `0x1fbda6a0` must assign that hop a VC `∈ {0,1,2}`. The choice is a strict priority cascade over three predicates, all evaluated for the same hop. The result is written to the action struct's VC field at `+0x10` (alongside `next_chip @ +8` and `output_link @ +0xc`).

### Entry Point

```text
RoutingTableGenerator::GetNextHopAction (0x1fbda6a0)
  ├─ CrossesDateline(src, path)              (0x1fbda8b5 → 0x1fbdb120)  ── crossed?
  ├─ Direction::IsSame(this_dir, next_dir)   (0x1fbda8fd → 0x20c025e0)  ── turned?  (FALSE ⇒ turn)
  ├─ GetVcBalanceUsage(src, dir_hops)        (0x1fbda921 → 0x1fbdb4c0)  ── balance?
  └─ select VC ∈ {0,1,2}, write {next_chip+8, output_link+0xc, vc+0x10}  (0x1fbdaa6a)
```

### Algorithm

```c
function VcForHop(gen, src, path, hop):                   // inside GetNextHopAction @0x1fbda6a0
    crossed = CrossesDateline(src, path)                  // 0x1fbda8b5 → 0x1fbdb120
    turned  = Direction::IsSame(this_dir, next_out_dir)   // 0x1fbda8fd → IsSame @0x20c025e0
              // IsSame compares two proto Direction {orientation,polarity} 8-byte structs;
              // FALSE ⇒ the next hop's outgoing direction differs ⇒ the path TURNS here.
    balance = GetVcBalanceUsage(src, this_dir_hops)       // 0x1fbda921 → 0x1fbdb4c0

    if not turned:                                        // line 0x1fbda9cb (v16 = 1)
        return 1   // "Turned, forced to low VC!"          [routing_table_generator.cc:251]
    else if crossed:                                      // line 0x1fbda950 (v16 = 2)
        return 2   // "Crossed a dateline, forced to high VC!"  [:254]
    else:
        vc = 0                                            // line 0x1fbda9ea (v16 = 0)
        if balance:                                       // line 0x1fbda9fd (v16 = 2)
            vc = 2   // "VC load balancing, forced to high VC!"  [:257]
        return vc
```

> **QUIRK —** the first branch fires on `!IsSame` yet logs *"Turned, forced to low VC"*. `IsSame` is the *same-direction* test between this hop's direction and the next hop's outgoing direction; `!IsSame` therefore means the route changes axis or polarity at the next chip — a **turn**. A reimplementer who reads the branch as "if the directions are the same, force VC1" inverts the rule and produces a fabric that deadlocks under load. VC0 is the *unlabelled* straight-continuation default; VC2 is the only "high" VC and is reached two distinct ways — a dateline wrap (the deadlock break) or a balance overflow on a straight hop.

### The VC table

| Condition (per non-terminal hop) | VC | VLOG label (source line) |
|---|---|---|
| Straight (`IsSame`), no dateline, no balance | 0 | *(unlabelled — default)* |
| Turn (`!IsSame` this-vs-next direction) | 1 | `Turned, forced to low VC!` (`:251`) |
| Straight, crosses dateline (wrap edge) | 2 | `Crossed a dateline, forced to high VC!` (`:254`) |
| Straight, no dateline, balance fires (`hops ≤ threshold`) | 2 | `VC load balancing, forced to high VC!` (`:257`) |
| Terminal at `dst` (`src == dst`, no hop) | 1 | *(set directly via `SetUnicastVcControl(.,1)`)* |

The three VLOG strings are byte-confirmed semantic labels of the three branches and are read verbatim from the decompiled cascade (`GetNextHopAction` `$_2`/`$_3`/`$_4` log sites). The cascade structure (`if !turned … else if crossed … else { vc=0; if balance vc=2 }`) is the exact decompiled control flow.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `RoutingTableGenerator::GetNextHopAction` | `0x1fbda6a0` | Resolve hop + select VC via the cascade | CERTAIN |
| `slice_builder::Direction::IsSame` | `0x20c025e0` | Compare two `proto::Direction` (turn predicate) | CERTAIN |
| `RoutingTableGenerator::CrossesDateline` (Coord,Coord) | `0x1fbdb120` | Per-axis wrap-boundary side-flip | CERTAIN |
| `RoutingTableGenerator::GetVcBalanceUsage` | `0x1fbdb4c0` | Per-axis hop-count balance gate | CERTAIN |

---

## The dateline predicate

### Purpose

`CrossesDateline` answers, for a single hop's outgoing-direction axis, whether the hop steps across that axis's wrap boundary. It is the predicate that forces VC2 in the cascade and the first gate of the balance test. Two overloads exist: a single-hop `(Coord src, Coord dst)` form @ `0x1fbdb120`, and a path-walking `(Coord src, DirectionHops)` form @ `0x1fbdd200` that ORs the per-hop crossings of a multi-hop direction along one axis.

### Algorithm — single hop

```c
function CrossesDateline(gen, src, dst):                  // 0x1fbdb120
    dim       = orient(dst's direction)                   // dim from the hop's Direction
    applies   = bit(dim-1) of [topo+0x78]                 // per-axis "dateline applies" bitmap
                                                          //   (_bittest64 at line 92; QWORD[a2+15])
    if not applies:
        return false                                      // axis has no dateline → never crosses

    dateline  = [topo+0x90][dim-1]                        // per-axis dateline coordinate (QWORD[a2+18])
    axis_size = [topo+0x48][dim-1]                        // per-axis size (QWORD[a2+9])
    a = dst.GetCoordinate(dim)                            // line 100
    c = src.GetCoordinate(dim)                            // line 105

    if dateline != 0:                                     // explicit dateline coordinate
        side_dst = (a < dateline)                         // line 121 (v18 = v14 < v16)
        side_src = (c < dateline)                         // line 122 (v19 = v40 < v16)
    else:                                                 // implicit boundary = top of axis
        side_dst = (a + 1 == axis_size)                   // line 117 (v18 = v14+1 == v17)
        side_src = (c + 1 == axis_size)                   // line 118 (v19 = v40+1 == v17)

    return side_src != side_dst                           // line 124 (the cmp dl,r9b side-flip)
```

The crossing test is a pure **side flip**: the hop crosses iff `src` and `dst` lie on opposite sides of the wrap boundary. When the per-axis dateline coordinate is set (`dateline != 0`), the boundary is that coordinate and the "side" is `coord < dateline`. When it is zero, the boundary degenerates to the seam between the last index and index 0, detected by `coord + 1 == axis_size`. The bitmap at `[topo+0x78]` lets the topology disable the dateline on axes that do not wrap (a mesh axis returns `false` unconditionally).

> **GOTCHA —** the `dateline == 0` branch is *not* "no dateline" — it is the **implicit boundary at the top of the axis**. The "no dateline" case is the bitmap test (`applies == false`), which short-circuits to `false` before either side computation. A reimplementation that treats a zero dateline coordinate as "disabled" loses every wrap detection on the most common axis layout (dateline at the seam), producing a fabric that under-uses VC2 and can deadlock.

### Algorithm — path walk

```c
function CrossesDateline(gen, src, dir_hops):             // 0x1fbdd200
    if dir_hops.code < 128:                               // (code >> 6) < 2 → fewer than 2 hops
        return false                                      // line 30: a single step cannot wrap an axis
    pos = src
    crossed = false
    for k in 0 .. (code >> 6) - 2:                        // walk (code>>6)-1 hops (line 89)
        next = topo.Walk(pos, one_step_dir)               // vtable+0xa0, line 49
        if CrossesDateline(src, pos, next): crossed = true; break   // per-hop single-hop test (line 61)
        pos = next
    return crossed                                        // early-true on first crossing
```

The path-walking overload exists because a packed `DirectionHops` entry can encode several hops along one axis (`hop_count = code >> 6`); the predicate must report a crossing if *any* of those intermediate steps crosses the wrap boundary, hence the early-true OR over `Walk`-stepped coordinates. The `code < 128` guard is `(code >> 6) < 2`: a single hop along an axis cannot cross its own wrap boundary, so it returns `false` without walking.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CrossesDateline` (Coord, Coord) | `0x1fbdb120` | Single-hop side-flip on the hop's axis | CERTAIN |
| `CrossesDateline` (Coord, DirectionHops) | `0x1fbdd200` | OR per-hop crossings over a multi-hop direction | CERTAIN |
| `Coordinates::GetCoordinate` | — | Read per-axis coordinate value | HIGH |
| topology `Walk` | `vtable+0xa0` | Step one chip along a direction (path walk) | HIGH |

---

## The balance gate

### Purpose

VC load balancing shifts a controlled fraction of *short* dateline-crossing traffic onto VC2, evening the per-VC link load that the deadlock discipline alone would skew (the deadlock rule otherwise pins long wrap runs to VC2 and everything else to VC0). `GetVcBalanceUsage` @ `0x1fbdb4c0` returns the boolean that arms the cascade's third branch. It is a four-way gate: only dateline hops balance, balancing must be enabled, a gating config flag must be clear, and the hop must be *short* — its hop count along the axis must not exceed a per-axis threshold.

### Algorithm

```c
function GetVcBalanceUsage(gen, src, dir_hops):           // 0x1fbdb4c0
    if not CrossesDateline(src, dir_hops):  return false  // line 17/18 — only dateline hops balance
    if gen.vc_balance_enabled != 1:         return false  // BYTE[gen+0x9] == 1 gate (line 20)
    if gen.flag_0x14 | crossed_flag:        return false  // !(v15 | BYTE[gen+0x14]) (line 20)
    orient = dir_code & 7                                 // axis index (line 24)
    hops   = (int)dir_code >> 6                            // signed hop count, sar (line 32)
    return hops <= gen.vc_balance_threshold[orient - 1]    // setle (line 32); vector<int> @gen+0x60
```

The gate chain, byte-exact from the decompiled function:

1. **`CrossesDateline(src, dir_hops)`** — the path-walk overload; non-dateline hops never balance (they are already VC0 and shifting them would break nothing but also help nothing). If this is false, return false immediately.
2. **`BYTE[gen+0x9] == 1`** — `vc_balance_enabled`, a build/config bool. If balancing is off, return false.
3. **`!(crossed_flag | BYTE[gen+0x14])`** — a second gating bool combined with the crossing result; balancing is suppressed when either is set.
4. **`hops ≤ threshold[orient-1]`** — the per-axis cap. `orient` is the low 3 bits of the packed direction code; `hops` is the arithmetic-right-shift of the code (the signed hop count along this axis). The threshold array lives at `gen+0x60` (a `vector<int>`, bound `gen+0x68`).

> **NOTE —** the byte at `gen+0x14` is read here *and* by [`GetStaticPath`](../routing/get-static-path.md) as its `use_limited_ici` mode bit. Whether this is a genuinely aliased field ("limited routing disables balance") or two adjacent fields that collapse to the same read was not disambiguated; both code paths read `BYTE[gen+0x14]`. (LOW confidence on the field's dual role; CERTAIN that both reads target `+0x14`.)

### Why the threshold caps *short* hops

The gate fires only when `hops ≤ threshold`, i.e. for *short* dateline hops. Combined with the threshold's `~0.2 · axis_size` scaling ([below](#the-threshold-construction)), this means roughly the shortest fifth of dateline-crossing distances get balance-shifted to VC2, while long wrap runs stay on VC2 anyway (via the dateline branch) — so the net effect is to spread short wrap traffic across VC0/VC2 and keep long wrap traffic on VC2. The deadlock invariant is untouched because every balanced hop is, by gate condition 1, already a dateline hop and thus legitimately on the high VC class.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `GetVcBalanceUsage` | `0x1fbdb4c0` | Four-way balance gate, per-axis threshold compare | CERTAIN |
| `CreateVcBalanceThreshold` | `0x1fbd8320` | Build the `gen+0x60` per-axis threshold vector | CERTAIN |

---

## The threshold construction

### Purpose

`CreateVcBalanceThreshold` @ `0x1fbd8320` populates the per-axis threshold vector at `gen+0x60` that `GetVcBalanceUsage` reads. Each axis gets an integer threshold that scales linearly with axis size; the scaling constant is chosen per-axis from three precomputed candidates selected by an axis-"kind" field, with a direct per-axis computation as the fallback.

### Algorithm

```c
function CreateVcBalanceThreshold(gen):                   // 0x1fbd8320
    n = gen.num_dims                                      // QWORD[gen+0x50] (this+10)
    resize(gen.threshold, n)                              // vector<int> @gen+0x60, append zeros (line 53)
    if gen.vc_balance_enabled != 1 or gen.flag_0x14:      // BYTE[gen+9]==1 && !BYTE[gen+0x14] (line 56)
        return ok                                         // balancing off → leave thresholds zeroed

    dim_sizes = topo.GetDimensionSizes()                  // vtable+0x50 (line 58)
    min_dim   = min(dim_sizes)                            // cmp/cmovl keeps the smaller (line 59..353)

    // three candidate thresholds, all round-to-nearest( min_dim * mul + add ):
    t_kind1 = round( min_dim * 0.175 - 0.15 )             // stored var_58 (asm @ line 162-169)
    t_kind2 = round( min_dim * 0.222 - 0.1  )             // stored var_60 (asm @ line 155-161)
    t_kind3 = round( min_dim * 0.207 - 0.2  )             // stored var_50 (asm @ line 146-154)

    for i in 0 .. n-1:                                    // per-axis loop (line 171)
        // skip axes whose axis-property (vtable+0x30) == 2:
        prop = topo.AxisProperty(i)                       // vtable+0x30 (line 178)
        if prop == 2:
            continue                                      // (loop advances to next dim)

        kind = [topo+0xe8] for this axis                  // axis "kind" ∈ {1,2,3} (switch line 229)
        switch kind:
            case 1: threshold[i] = t_kind1                // var_58 (line 244)
            case 2: threshold[i] = t_kind2                // var_60 (line 239)
            case 3:                                       // line 231
                if [topo+0x134] == 1 and i == [topo+0x130]:
                    goto direct                           // a single skipped axis (line 232)
                threshold[i] = t_kind3                    // var_50 (line 234)
            default:
direct:         threshold[i] = round( dim_sizes[i] * 0.145 - 0.3 )  // per-axis, asm @ line 194-204
```

The reduction at the top finds `min_dim`, the *smallest* per-axis size (`cmp; cmovl` keeps the lesser element — see the `objdump` below); the three candidate thresholds are all `round_to_nearest(min_dim · mul + add)` computed once via the inline SSE block (the `±0.5` magic + `vroundsd …, 0Bh` round-to-nearest). Each axis then selects a candidate by its "kind" field at `[topo+0xe8]`, with two escape hatches: axis-property 2 skips an axis entirely (no balance), and one specific axis (named by the `[topo+0x130]`/`[topo+0x134]` pair) under kind 3 falls through to a *direct* per-axis computation off that axis's own size rather than `min_dim`.

> **CORRECTION —** the top-of-function reduction is a **minimum**, not a maximum. The byte-anchored loop is `mov (%rax),%r10d; cmp %esi,%r10d; cmovl %r10d,%esi` (`0x1fbd83d0`) — `cmovl` keeps `r10d` only when it is *less than* the running `esi`, and the minimum value is reloaded into `r14d` at `0x1fbd84d0` to feed `vcvtsi2sd`. The candidate thresholds therefore scale with the *shortest* axis, not the longest.

The scaling multipliers (`0.175`, `0.222`, `0.207`, and the direct `0.145`) place the threshold at roughly a fifth of the axis length — `threshold ≈ round(axis_size · ~0.2 − const)` — so the balance gate admits only the shortest ~⅕ of dateline-crossing distances. All four `mul`/`add` pairs are byte-anchored to their rodata doubles (`min_dim · {0.175 − 0.15, 0.222 − 0.1, 0.207 − 0.2}` for kinds 1/2/3, and `axis_size · 0.145 − 0.3` for the direct path); the multiplier-to-kind mapping below follows the `var_58`/`var_60`/`var_50` switch arms in the decompiled body.

### The axis-kind selector

| Axis kind (`[topo+0xe8]`) | Threshold source | Scaling | Confidence |
|---|---|---|---|
| 1 | `t_kind1` (`var_58`) | `round(min_dim · 0.175 − 0.15)` | CERTAIN |
| 2 | `t_kind2` (`var_60`) | `round(min_dim · 0.222 − 0.1)` | CERTAIN |
| 3 | `t_kind3` (`var_50`), or direct if `[topo+0x134]==1 && i==[topo+0x130]` | `round(min_dim · 0.207 − 0.2)` | CERTAIN |
| *(default / kind-3 skip)* | direct per-axis | `round(axis_size[i] · 0.145 − 0.3)` | CERTAIN |
| *(axis-property == 2)* | skipped (threshold stays 0) | — | HIGH |

> **CORRECTION (P-3-355) —** an earlier reading placed the `vc_balance_enabled` / `flag_0x14` gate only inside `GetVcBalanceUsage`. `CreateVcBalanceThreshold` carries the **same** gate (`BYTE[gen+9]==1 && !BYTE[gen+0x14]`, line 56): if balancing is disabled, the threshold vector is resized but left zeroed and the function returns early. A zeroed threshold makes `GetVcBalanceUsage`'s `hops ≤ threshold[orient-1]` true only for `hops ≤ 0` (i.e. essentially never for a real hop), so the runtime gate and the build-time construction redundantly disable balancing — the construction does not rely on the runtime gate alone.

> **NOTE —** the binding of the three axis "kinds" to physical axis classes (wrap / twist / dateline) is inferred from the `~0.2 · axis_size` scaling and the single-axis skip under kind 3, not pinned to a named proto field. The kind → axis-class mapping is therefore MEDIUM confidence; the *mechanism* (three candidates selected by `[topo+0xe8] ∈ {1,2,3}`, with the `[topo+0x130/0x134]` skip pair) is byte-confirmed.

---

## Multipod inter-pod route emission

### Purpose

`multipod::RoutingTableGenerator::Generate` @ `0x1fbf03a0` (a separate translation unit, `dragonfish/multipod/routing_table_generator.cc`) builds the routing-table layer *above* the per-pod table. It is included here because its VC handling is the deliberate counterpoint to the cascade above: the inter-pod links carry **no torus ring**, so the multipod next-hop table fixes the VC rather than running the dateline/turn/balance discipline. The model, coordinate mapping, and DOR distance rule of the multipod emitter are documented on [`GetStaticPath`](../routing/get-static-path.md#multipod-inter-pod-route-emission); this section covers only the VC and deadlock-freedom facets that this page owns.

### The fixed VC, and why

```c
function SetNextHopRoutingTableEntry(this, src, dst, idx, table):   // 0x1fbf1a80
    if src == dst:                                                 // terminal entry (immediate dst)
        table.SetUnicastTerminal(idx, 1)                           // line 0x1fbf1ac0
        table.SetUnicastVcControl(idx, /*vc=*/1, true)             // FIXED VC=1, mov ecx,1 @0x1fbf1ada
    else:
        dir  = GetNextRoutingDirection(src, this_chip)             // 0x1fbf1aXX
        next = topo.GetCoordinate(dir)                             // vtable+0x90, line 163 (advance one chip)
        link = LinkMap.GetLink(this.link_map, dir)                 // 0x1fbf1de2
        // diagnostics only — the result of these is logged but does NOT pick the VC:
        crossed = CrossesDateline(src)                             // 0x1fbf28e0; logs "Crossed … high VC!" (:217)
        turned  = !Direction::IsSame(this_dir, next_dir)           // logs "Turned … low VC!" (:221)
        table.SetUnicastTarget(idx, link, 1)                       // line 0x1fbf1c??
        table.SetUnicastVcControl(idx, /*vc=*/1, true)             // FIXED VC=1, mov ecx,1 @0x1fbf1c69

function SetEgressRoutingTableEntry(this, src, dst, idx, table):   // 0x1fbf17c0
    if src == dst:  table.SetUnicastTerminal(idx, false)           // line 0x1fbf17fd
    else:
        dir  = GetNextRoutingDirection(src, dst)                   // line 0x1fbf184e
        link = LinkMap.GetLink(this.chip_id, dir)                  // line 0x1fbf1914
        table.SetUnicastTarget(idx, link, false)                   // line 0x1fbf1936
        // NO SetUnicastVcControl on the egress hop
```

> **QUIRK —** the multipod next-hop table writes a **fixed VC = 1** on every hop, terminal and non-terminal alike. It is *not* the slice-builder dateline/turn/balance cascade, even though the non-terminal path still **evaluates** the dateline and turn predicates (`multipod::CrossesDateline` @ `0x1fbf28e0` and `Direction::IsSame`) and emits the same two VLOG strings — `"Crossed a dateline, forced to high VC!"` (`:217`) and `"Turned, forced to low VC!"` (`:221`). Those predicates only drive *logging*; the VC immediate handed to `SetUnicastVcControl` is a hardcoded `1` regardless of the result. Inter-pod links are point-to-point optical hops with no torus channel cycle to break across pods, so a single VC suffices and the dateline discipline does not need to *select* the VC here. The egress table writes **no** `SetUnicastVcControl` at all (egress is the local hop into the next-hop machinery). The VC immediate `1` is byte-confirmed: `mov ecx, 1` at `0x1fbf1ada` (terminal) and `0x1fbf1c69` (next-hop), each immediately preceding the `SetUnicastVcControl` call; the IDA pseudocode folds the immediate and prints only `SetUnicastVcControl(table)`.

### Deadlock freedom is delegated

```c
function GeneratesDeadlockFreeTables(this):               // 0x1fbf2e40
    return false                                           // xor eax,eax; ret
```

`GeneratesDeadlockFreeTables` is `xor eax,eax; ret` — the multipod layer does **not** itself guarantee deadlock freedom. Intra-pod freedom comes from the per-pod slice-builder VC cascade (this page's [3-way rule](#the-3-way-vc-cascade)); inter-pod freedom comes from the acyclic point-to-point optical links. `SetChannelMerges` @ `0x1fbf2100` applies `AddChannelMerge` (`0x1fbf24c3`) specifically over `LinkMap::GetHighLatencyLinks` (`0x1fbf220f`) — the optical / long inter-pod links — so the inter-pod fabric's channel merging is targeted at exactly those high-latency hops.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `multipod::SetNextHopRoutingTableEntry` | `0x1fbf1a80` | Next-hop writer + fixed VC=1 | CERTAIN |
| `multipod::SetEgressRoutingTableEntry` | `0x1fbf17c0` | Egress writer (no VcControl) | CERTAIN |
| `multipod::SetChannelMerges` | `0x1fbf2100` | `AddChannelMerge` on high-latency links | HIGH |
| `multipod::GeneratesDeadlockFreeTables` | `0x1fbf2e40` | Returns `false` (defers to per-pod) | CERTAIN |
| `multipod::CreateDateline` | `0x1fbf0b20` | Build multipod dateline (call site only) | LOW |

> **NOTE —** the multipod `CreateDateline` @ `0x1fbf0b20` is called from `Generate` (`0x1fbf0617`) but its body was not byte-decoded. Given the inter-pod next-hop uses a fixed VC=1, the multipod dateline is presumably per-pod only (intra-pod wrap); the inter-pod mesh hops are acyclic by construction. Marked LOW until the body is decoded.

---

## Relevant struct offsets

| Offset (`gen`) | Field | Read by | Confidence |
|---|---|---|---|
| `+0x9` | `vc_balance_enabled` (bool) | `GetVcBalanceUsage` (`0x1fbdb4ed`), `CreateVcBalanceThreshold` (line 56) | CERTAIN |
| `+0x14` | gating flag / `use_limited_ici` (bool) | `GetVcBalanceUsage` (`0x1fbdb4f4`), `CreateVcBalanceThreshold`, `GetStaticPath` | HIGH |
| `+0x60` / `+0x68` | `vc_balance_threshold` `vector<int>` base/end | `GetVcBalanceUsage` (`0x1fbdb55c`), `CreateVcBalanceThreshold` (`0x1fbd8746`) | CERTAIN |
| `+0x20` | primary topology (twist / resilient) | distance + `Walk` + axis metadata | CERTAIN |

| Offset (`topo`) | Field | Used by | Confidence |
|---|---|---|---|
| `+0x30` | axis-property (`== 2` ⇒ skip balance) | `CreateVcBalanceThreshold` (line 178) | HIGH |
| `+0x48` | per-axis size vector | `CrossesDateline` (`0x1fbdb246`) | CERTAIN |
| `+0x50` | `GetDimensionSizes` / `num_dimensions` | `CreateVcBalanceThreshold` (line 58) | CERTAIN |
| `+0x78` | dateline-applies bitmap | `CrossesDateline` (`0x1fbdb1be`) | CERTAIN |
| `+0x90` | per-axis dateline coordinate | `CrossesDateline` (`0x1fbdb223`) | CERTAIN |
| `+0xa0` | `Walk(Coord, Direction)` | `CrossesDateline` (path walk) | CERTAIN |
| `+0xe8` | axis "kind" `∈ {1,2,3}` | `CreateVcBalanceThreshold` (`0x1fbd8770`) | HIGH |
| `+0x130` / `+0x134` | single-axis skip pair (kind 3) | `CreateVcBalanceThreshold` (line 232) | MEDIUM |

---

## Related Components

| Component | Relationship |
|---|---|
| `GetStaticPath` | Produces the static path whose turn / dateline-cross / hop-count shape feeds this rule |
| `GetNextHopAction` | Hosts the cascade; resolves `output_link` then calls into this rule |
| Unicast route emission | Installs the `{output_link, vc, terminal}` `RoutingEntry` the cascade fills |
| `RandomizedToroidalWildFirstPaths` | Resilient fault-aware path generator; shares the same VC cascade and dateline predicate |

## Cross-References

- [GetStaticPath & Multipod](../routing/get-static-path.md) — the deterministic path generator (torus-vs-mesh pick + `DirectionHops` packing) and the full multipod model; this page covers the VC rule and deadlock-freedom that page defers here
- [Route-Table Generation](../routing/route-table-generation.md) — the `RouteTargetCache` fast path that calls `GetStaticPath` on a miss, feeding the route into this cascade
- [Unicast Route Emission](../routing/unicast-route-emission.md) — the emitter that installs the `{output_link, vc, terminal}` `RoutingEntry` this rule fills
- [RandomizedToroidalWildFirstPaths](../routing/randomized-toroidal-wildfirst.md) — the resilient, fault-avoiding sibling path generator that reuses this VC cascade
- [GetDistances](../routing/get-distances.md) — the twisted-torus distance metric whose wrap edges define where datelines apply
- [Routing Overview](../routing/overview.md) — the route-generation → cache → emission pipeline this rule sits inside
- [ICI Overview](overview.md) — the ICI fabric section opener
- [Topology Discovery](topology-discovery.md) — where the topology object (`gen+0x20`), its dateline bitmap, and axis metadata come from
- [DMA Descriptor](dma-descriptor.md) — the runtime consumer of the `output_link` / VC that the cascade emits
- [Failure Recovery](failure-recovery.md) — fault handling that re-runs route generation (and thus this rule) on link loss
