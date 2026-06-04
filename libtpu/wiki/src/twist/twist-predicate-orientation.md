# Twist Predicate, the `Orientation` Enum, and the n-hop Source-Relative Gate

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset (base `0xe63c000`); all addresses are VMA. Every symbol below is present in the full-symbol binary and cross-checked against the IDA decompile.*

## Abstract

Three small, byte-pinned facts gate the entire twisted-torus subsystem, and this page owns all three. First, the **twist eligibility predicate**: a slice may be lowered as a twisted torus only when its longest axis is *exactly* twice its shortest axis — `max_dim_size_ == 2 * min_dim_size_` — a fatal `CHECK` inside `TwistedTorusND::UpdateMinMaxDims`. Second, the **`Orientation` enum**, the dense `0..6` enum that names a torus axis on a faulty-link record and on every static route hop. Third, the **`is_nhop_source_relative` flag** (`Target[+0x3fb]`), the one-byte gate that selects whether the ICI next-hop route table is computed relative to each source chip or keyed by absolute destination IDs.

The `Orientation` enum is the page's center of gravity. `Direction::OrientationToDimension` and the proto descriptor both show `4/5/6 = A/B/C`: three *additional logical dimensions* (logical dims `3/4/5`) beyond the physical `X/Y/Z` torus axes — **not** signed/negative variants of the physical axes. The negative SerDes direction is carried by a **separate** `polarity` field on the `Direction` message, not by an extra orientation value. This page documents the enum, proves it from the binary, and explains why `OrientationsToTpuDegradedAxes` folds only `1/2/3` (the physical-axis subset consumed by the [degraded-axis ingest path](../collectives/degraded-axis.md)).

A reader who knows torus collectives and dimension-order routing owns the frame: a "twist" needs a doubled axis to fold (hence the `2·min` predicate); an axis-naming enum to describe where each link points (hence `Orientation`); and a routing-table convention bit (hence `is_nhop_source_relative`). The reimplementation contract is:

- **The predicate.** `max_dim_size_ == 2 * min_dim_size_`, enforced as a fatal `CHECK` in `UpdateMinMaxDims`, alongside the `twist() == true` gate, the three `dim_sizes_[i] >= 1` checks, and the `num_min_dims_ + num_max_dims_ == num_dims_` partition check.
- **The enum.** `Orientation {0=UNKNOWN_ORIENTATION, 1=X, 2=Y, 3=Z, 4=A, 5=B, 6=C}`; `OrientationToDimension(o) = (o == 0 ? INVALID_ARGUMENT : o - 1)`; the `Polarity {0=UNKNOWN, 1=POSITIVE, 2=NEGATIVE}` companion field that carries direction.
- **The route gate.** `Target::IsNhopSourceRelative()` reads `Target[+0x3fb]`; the default is `(routing_strategy == ROUTING_NHOP)`; `DragonfishTarget` overrides it with a per-slice byte and asserts the base byte is unset.

| | |
|---|---|
| **Twist predicate** | `TwistedTorusND::UpdateMinMaxDims` @ `0x137d0260`; `CHECK` `"max_dim_size_ == 2 * min_dim_size_"` @ `0x137d037a` (`all_reduce_strategies.cc:1900`) |
| **Predicate fatal string** | `"Max. dim size should be 2 times the min. in a twisted torus"` |
| **K / 2K fields** | `min_dim_size_` = `this[+0x5f8]`; `max_dim_size_` = `this[+0x5f0]`; `num_max_dims_` = `this[+0x600]`; `num_min_dims_` = `this[+0x608]` |
| **`Orientation` enum** | `accel_ssw::deepsea::proto::Orientation {0 UNKNOWN, 1 X, 2 Y, 3 Z, 4 A, 5 B, 6 C}` (`dimension.proto`) |
| **`o → dim` map** | `Direction::OrientationToDimension` @ `0x20c027c0` — `o == 0 ⇒ error; else o - 1` |
| **`Polarity` enum** | `{0 UNKNOWN_POLARITY, 1 POSITIVE, 2 NEGATIVE}` (`dimension.proto`) |
| **n-hop accessor (base)** | `Target::IsNhopSourceRelative` @ `0x1d6159a0` — `return byte[this+0x3fb]` |
| **n-hop accessor (Dragonfish)** | `DragonfishTarget::IsNhopSourceRelative` @ `0x1d48f020` — per-slice byte + `CHECK(!base)` |
| **n-hop default** | `GetDefaultConfiguredProperties` @ `0x20acee40` — `is_nhop = (routing_strategy == 2 == ROUTING_NHOP)` |
| **Routing enum** | `TpuRoutingStrategyProto {0 ROUTING_DEFAULT, 1 ROUTING_MESH, 2 ROUTING_NHOP}` |
| **Limited-ICI route consumer** | `tpu::RoutingTableEntryForICILimitedRouting` @ `0x1fc58040` |
| **Confidence** | HIGH (decompile-verified predicate `CHECK` strings, `OrientationToDimension` body, both n-hop accessors, the default-config packing) unless a row/callout says otherwise |

This page owns the *predicate*, the *enum*, and the *route gate*. The `BuildStrategy` driver that the predicate guards is on [BuildStrategy](buildstrategy.md); the three shape cases the `K`/`2K` counts classify are on [Shape Folds](shape-folds.md); the coordinate fold is on [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md). The `Orientation → degraded-byte` map this page corrects is on [Degraded-Axis Ingest](../collectives/degraded-axis.md). The packed `(hop<<6 | polarity<<3 | orientation)` route hop the enum feeds is on [GetStaticPath](../routing/get-static-path.md).

---

## 1. The Twist Eligibility Predicate

A slice is geometrically eligible for the twisted-torus lowering only when one torus axis is exactly twice the length of the shortest axis, and every axis is either that short length `K` or the long length `2K`. Both conditions are *fatal* `CHECK`s — a slice that reaches `TwistedTorusND::UpdateMinMaxDims` without satisfying them aborts the compile rather than falling back. (The picker's C-ii branch is what only *constructs* a `TwistedTorusND` when the shape looks twisted; `UpdateMinMaxDims` is the second, authoritative line of defence — see [SelectNDStrategy](../collectives/strategy-nd-picker.md).)

### `TwistedTorusND::UpdateMinMaxDims` — `0x137d0260`

The function loads the three torus dimensions into `dim_sizes_[0..2]`, sorts them into `min_dim_size_` (the short axis `K`) and `max_dim_size_` (the long axis `2K`), counts how many axes equal each, and enforces five invariants. The decompile (lightly cleaned, with the SIMD count loops summarised) is:

```c
// TwistedTorusND::UpdateMinMaxDims @ 0x137d0260
// chip_config = Target[+0x3b8]  (decompile: *((_QWORD*)a2 + 119))
int64 cfg = *((_QWORD *)a2 + 119);

// (1) the twist-enable gate — the chip_config twist() byte must be set
CHECK(*(byte*)(cfg + 163) == 1, "target.Topology()->twist() == true");   // chip_cfg+0xa3

// load the three torus dims into dim_sizes_[0..2]  (note the X/Y/Z source order below)
this->dim_sizes_[0] = *(int*)(cfg + 92);   // this[+0xb8]    (chip_cfg+0x5c)
this->dim_sizes_[1] = *(int*)(cfg + 88);   // this[+0xc0]    (chip_cfg+0x58)
this->dim_sizes_[2] = *(int*)(cfg + 96);   // this[+0xc8]    (chip_cfg+0x60)

// (2) every dim must be >= 1
CHECK(dim_sizes_[2] >= 1, "dim_sizes_[2] >= 1");
CHECK(dim_sizes_[1] >= 1, "dim_sizes_[1] >= 1");
CHECK(dim_sizes_[0] >= 1, "dim_sizes_[0] >= 1");

this->num_dims_ = 3;                       // this[+0x598]

// 3-way min/max sort via cmov: max_dim_size_ = max, min_dim_size_ = min
this->max_dim_size_ = max(dim0, dim1, dim2);   // this[+0x5f0]   == 2K
this->min_dim_size_ = min(dim0, dim1, dim2);   // this[+0x5f8]   == K

// (3) THE TWIST PREDICATE
if (this->max_dim_size_ != 2 * this->min_dim_size_) {
    CHECK_FAIL("max_dim_size_ == 2 * min_dim_size_");          // all_reduce_strategies.cc:1900
    LOG(FATAL) << "Max. dim size should be 2 times the min. in a twisted torus";
}

// vpcmpeqq count loops: how many of the 3 dims equal MAX (2K) / MIN (K)
this->num_max_dims_ = count(dims == max_dim_size_);   // this[+0x600]
this->num_min_dims_ = count(dims == min_dim_size_);   // this[+0x608]

// (4) the partition check — every dim is either the max or the min, nothing between
CHECK(num_min_dims_ + num_max_dims_ == num_dims_,
      "num_min_dims_ + num_max_dims_ == num_dims_");           // :1904
      // LOG(FATAL) << "Dimension sizes should either be maximum or minimum";
```

> **NOTE — the predicate is the page's title CHECK.** `max_dim_size_ == 2 * min_dim_size_` is the single condition that makes a torus "twistable". If `max == 3·min` (e.g. a `K, K, 3K` slice), the predicate fails and the compile aborts; there is no `3·K` twist. The companion partition `CHECK` then forbids a third distinct length: a `K, 2K, 1.5K`-style slice would pass the `2·min` check on its two extremes yet fail `num_min + num_max == num_dims`. Together they restrict the geometry to exactly `{K, 2K}` extents, which is what the three `TwistedTorusShape` cases enumerate.

### What the counts classify

`num_max_dims_` (`2K`-count) and `num_min_dims_` (`K`-count) sum to `num_dims_ == 3` and choose the shape:

| `(num_max, num_min)` | extents | `TwistedTorusShape` |
|---|---|---|
| `(1, 2)` | `K, K, 2K` | `TWIST_SHAPE_K_K_2K` |
| `(2, 1)` | `K, 2K, 2K` | `TWIST_SHAPE_K_2K_2K` |
| generalised long axis | `K, 2K, nK` | `TWIST_SHAPE_K_2K_NK` |

`BuildStrategy` reads these two counts to decide which axes carry the `K→2K` seam versus the doubled ring. The byte-level shape-case wiring is on [Shape Folds](shape-folds.md); the per-color seam application is on [BuildStrategy](buildstrategy.md). This page only establishes that the counts exist and are gated by the predicate.

> **NOTE — `dim_sizes_` source order is X/Y/Z-shuffled.** The three loads pull `dim_sizes_[0]` from `chip_cfg+0x5c`, `[1]` from `+0x58`, `[2]` from `+0x60`. In the chip-config layout the `+0x58`/`+0x5c`/`+0x60` words are the X/Y/Z dims, so `dim_sizes_` is stored `{Y, X, Z}`. The min/max sort is order-independent, so the predicate is unaffected; a reimplementer copying field offsets must keep the shuffle to match the per-axis `num_*_dims_` accounting `BuildStrategy` later does.

---

## 2. The `Orientation` Enum

`Orientation` is the dense proto enum that names a torus axis. It appears on a faulty-link record (which axis failed), on a `Direction` message (which way a link points), and on every packed static route hop. It is defined in `dimension.proto` (`accel_ssw::deepsea::proto`).

### 2.1 The enum values

Read byte-exact from the `EnumDescriptorProto` value list in the embedded `FileDescriptorProto` for `dimension.proto` (each `EnumValueDescriptorProto` is `12 05 0a 01 <ascii> 10 <num>`):

| value | name | descriptor bytes | logical dim (`o-1`) |
|---:|---|---|---:|
| `0` | `UNKNOWN_ORIENTATION` | (`10 00` default) | — (error) |
| `1` | `X` | `12 05 0a 01 58 10 01` | `0` |
| `2` | `Y` | `12 05 0a 01 59 10 02` | `1` |
| `3` | `Z` | `12 05 0a 01 5a 10 03` | `2` |
| `4` | `A` | `12 05 0a 01 41 10 04` | `3` |
| `5` | `B` | `12 05 0a 01 42 10 05` | `4` |
| `6` | `C` | `12 05 0a 01 43 10 06` | `5` |

The ASCII bytes are literally `'X'`/`'Y'`/`'Z'`/`'A'`/`'B'`/`'C'` (`0x58`/`0x59`/`0x5a`/`0x41`/`0x42`/`0x43`). The dense-enum range `0..6` is confirmed by the `NameOfDenseEnum<Orientation, 0, 6>` instantiation (`0x22471c50`) and the `Orientation_descriptor` (`0x20c0ad20`).

> **NOTE — `4/5/6` are not negative axes.** The descriptor names them `A/B/C`: three *additional logical dimensions* (logical dims `3/4/5`), not signed variants of the physical `X/Y/Z`. The negative SerDes direction is the `polarity` field (§2.3), a wholly separate concept.

### 2.2 `Direction::OrientationToDimension` — `0x20c027c0`

This is the function that proves the `o → dimension` map. It is trivial and total:

```c
// accel_ssw::deepsea::slice_builder::Direction::OrientationToDimension @ 0x20c027c0
//   -> absl::StatusOr<int>  (result struct in a1; *a2 = the Orientation value)
StatusOr<int> OrientationToDimension(const Orientation& o) {
  if (*o)                         // o != UNKNOWN_ORIENTATION (0)
    return *o - 1;                //   X/Y/Z/A/B/C  ->  dim 0/1/2/3/4/5
  return MakeErrorImpl<INVALID_ARGUMENT>(  // <3> = INVALID_ARGUMENT
      "Unknown orientation", /*len*/19, /*col*/87,
      "platforms/accel_ssw/deepsea/slice_builder/friends/direction.cc");
}
```

The map is therefore exactly `dim = orientation - 1` for all six named values, with `UNKNOWN_ORIENTATION` the only error case. The supported dimension count is six (`A/B/C` reach logical dims `3/4/5`), even though the physical 3-D torus uses only `X/Y/Z` (`1/2/3`).

> **NOTE — why only `1/2/3` ever degrade an axis.** `tpu::OrientationsToTpuDegradedAxes` (`0x1fc57d00`, documented on [Degraded-Axis Ingest](../collectives/degraded-axis.md)) tests only `== 1`, `== 2`, `== 3` and sets the X/Y/Z degraded byte; it ignores `0` and `4/5/6`. That is *correct*, not a bug: the degraded-axis machinery folds a dead link out of the physical 3-D torus ring, and only `X/Y/Z` are physical torus axes. A faulty `A/B/C` link (if it existed on a slice) has no physical torus axis to fold out of, so it produces no degraded byte.

### 2.3 The `Direction` message and `Polarity`

`Orientation` never travels alone for a *link*. A link's full direction is a `Direction` message pairing an orientation with a polarity:

```protobuf
// dimension.proto  (accel_ssw.deepsea.proto)
message Direction {
  Orientation orientation = 1;   // which axis: X/Y/Z/A/B/C
  Polarity    polarity    = 2;   // which way along it: +/-
}
enum Polarity { UNKNOWN_POLARITY = 0; POSITIVE = 1; NEGATIVE = 2; }
```

The **negative** SerDes direction is `polarity = NEGATIVE`, *not* a fourth/fifth/sixth orientation value. This is the structural reason the degraded-axis fold ignores polarity entirely: a degraded link on the `-X` SerDes has `orientation = X (1)`, `polarity = NEGATIVE (2)`, and folds to the *same* `X` degraded byte as a `+X` failure — the axis is what matters for routing around a dead link, not the direction along it. The per-chip ICI link table is keyed on the full `(orientation, polarity)` pair: the runtime holds a `flat_hash_map<Direction, PhysicalIciLink>` (with `DirectionHash` / `DirectionEqual`) per chip, so `+X` and `-X` are distinct map keys but the same axis.

The same `(hop_count, polarity, orientation)` triple is what the static route emitter packs into one byte per axis — `(hop_count << 6) | (polarity << 3) | (orientation & 7)` — so the enum's low 3 bits are sized to fit `0..6` exactly. The route-hop packing is on [GetStaticPath](../routing/get-static-path.md).

---

## 3. The `is_nhop_source_relative` Route Gate

`is_nhop_source_relative` is a single configured-properties byte that selects how the ICI next-hop route table is keyed: **source-relative** (each source chip's table is an offset-from-source map) versus **absolute** (entries keyed by destination chip ID). It rides the same `TpuConfiguredProperties` POD and the same `CreateFromTopology` ingest as the degraded bytes — see the [POD layout on the degraded-axis page](../collectives/degraded-axis.md) (`+0x3` is this byte). This page owns the *accessors* and the *default rule*; the actual route-table generation it steers is on [GetStaticPath](../routing/get-static-path.md) and [Route-Table Generation](../routing/route-table-generation.md).

### 3.1 The base accessor — `Target::IsNhopSourceRelative` @ `0x1d6159a0`

A pure byte load — the `Target[+0x3fb]` byte that `CreateFromTopology` wrote from `TpuConfiguredProperties[+0x3]`:

```c
// xla::jellyfish::Target::IsNhopSourceRelative @ 0x1d6159a0
__int64 Target::IsNhopSourceRelative(Target *this) {
  return *((unsigned __int8 *)this + 1019);   // 1019 == 0x3fb
}
```

`+0x3fb` is the byte immediately after the X/Y/Z degraded triple (`+0x3f8`/`+0x3f9`/`+0x3fa`) and before the routing-strategy int32 (`+0x3fc`). The whole 5-byte block is the flat config the writer lands; this is the inverse of the POD `+0x3` slot.

### 3.2 The Dragonfish override — `DragonfishTarget::IsNhopSourceRelative` @ `0x1d48f020`

The newer-generation `Target` does **not** simply return the base byte. It asserts the base byte is *unset* and then returns a per-slice byte:

```c
// xla::jellyfish::DragonfishTarget::IsNhopSourceRelative @ 0x1d48f020
__int64 DragonfishTarget::IsNhopSourceRelative(DragonfishTarget *this) {
  // the base Target flag MUST be false on Dragonfish — fatal CHECK otherwise
  CHECK(!Target::IsNhopSourceRelative(this),
        "!JellyfishTarget::IsNhopSourceRelative()");   // target_dragonfish.cc:28
  // value comes from the adjacent per-slice/topology struct (Target[+0x940] + 0x49)
  return *(unsigned __int8 *)(*((_QWORD *)this + 296) + 73LL);   // [[this+0x940]+0x49]
}
```

`this+296` qwords `= +0x940` (the slice/topology struct adjacent to the `MultiSliceTopologyAndLocation*` stored at `Target+0x928`); `+73 = +0x49`. So Dragonfish *replaces* the configured-properties byte with a per-slice source-relative byte and enforces that the generic `Target[+0x3fb]` byte was never set on this path.

> **GOTCHA — this is a replace-and-assert, not an OR.** The accessor is a *fatal `CHECK`* that the base byte is `false`, followed by an unconditional return of the per-slice byte (`[[+0x940]+0x49]`, `target_dragonfish.cc:28`). It is not an inclusive-or fallback `base || per_slice`: if the base byte were ever set on a Dragonfish target the process aborts rather than silently OR-ing.

### 3.3 The default — `GetDefaultConfiguredProperties` @ `0x20acee40`

When no per-slice override is supplied, the default config derives the flag from the routing strategy:

```c
// tpu::GetDefaultConfiguredProperties @ 0x20acee40   (this = TpuTopology; this[+0xa4] = routing_strategy)
unsigned __int64 GetDefaultConfiguredProperties(const TpuTopology *topo) {
  uint32_t rs = topo[+0xa4];                       // routing_strategy
  return ((uint64_t)rs << 32)                      // routing_strategy in high dword (POD +0x4)
       | ((uint8_t)(rs == 2) << 24);               // is_nhop_source_relative = (rs == ROUTING_NHOP)  (POD +0x3)
  //   degraded bytes (POD +0x0..+0x2) left zero
}
```

So `is_nhop_source_relative` defaults to **`routing_strategy == 2 == ROUTING_NHOP`**. The routing-strategy enum is read byte-exact from `tpu_routing_strategy.proto`:

| value | name | descriptor bytes |
|---:|---|---|
| `0` | `ROUTING_DEFAULT` | `12 13 0a 0f ROUTING_DEFAULT 10 00` |
| `1` | `ROUTING_MESH` | `12 10 0a 0c ROUTING_MESH 10 01` |
| `2` | `ROUTING_NHOP` | `12 10 0a 0c ROUTING_NHOP 10 02` |

### 3.4 The limited-ICI route consumer

The flag's downstream effect is the n-hop route-table generation. The query path is:

```text
tpu::IsReachableOverLimitedIci  @0x1fc57fe0
   entry = RoutingTableEntryForICILimitedRouting(topo, src, dst)
   return entry.ok && entry.target_port >= 0

tpu::RoutingTableEntryForICILimitedRouting  @0x1fc58040
   bounds-check src/dst against [topo+0x70] chip count
   require [topo+0x60] (routing_strategy) == 1  (ROUTING_MESH)  else InvalidArgument
   read x_wrapping byte[topo+0xa0], y_wrapping byte[topo+0xa1], dims[topo+0x58]
   build slice_builder::Topology(dims_span, wrap_span)            (ctor @0x20bf3320)
   entry = viperlite_pod::DmaDestinationRoutingTableEntryMapper::Map(
              src, dst, ToroidalTopology, RoutingScheme)          (@0x1fc584e0)
              // RoutingScheme == 2 -> MapTwoAxesReachable @0x1fc58fa0
              // RoutingScheme == 0 -> MapOneTwoFourEightHopNeighborsReachable @0x1fc588a0
              // 3-D topology -> error "toplogy must be 2d for limited ICI routing, z:"
```

Limited-ICI routing is a **2-D n-hop scheme** (1/2/4/8-hop neighbour reachability on two axes); the route entries are produced by the slice-builder `RoutingTableGenerator::GetNextHopAction` (`0x1fbda6a0`). `is_nhop_source_relative` selects whether those next-hop entries are computed relative to the source chip's coordinates (an offset-from-source map per source) or keyed by absolute destination chip IDs. The route-emission layer reads the flag — the `CHECK "!JellyfishTarget::IsNhopSourceRelative()"` in the Dragonfish path (§3.2) guards an absolute-route assertion. The full hop-offset tables and the `GetNextHopAction` body are on [GetStaticPath](../routing/get-static-path.md) and [Route-Table Generation](../routing/route-table-generation.md); this page pins only the *flag* and its default.

---

## 4. Function & Enum Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `TwistedTorusND::UpdateMinMaxDims` | `0x137d0260` | the twist predicate + dim sort + K/2K counts | HIGH (decompile-verified `CHECK` strings) |
| `Direction::OrientationToDimension` | `0x20c027c0` | `o == 0 ⇒ error; else dim = o − 1` | HIGH (byte-exact body) |
| `Orientation_descriptor` | `0x20c0ad20` | enum descriptor (`dimension.proto`) | HIGH |
| `NameOfDenseEnum<Orientation,0,6>` | `0x22471c50` | confirms dense `0..6` range | HIGH |
| `tpu::OrientationsToTpuDegradedAxes` | `0x1fc57d00` | folds only `1/2/3` (X/Y/Z) — see [degraded-axis](../collectives/degraded-axis.md) | HIGH |
| `Target::IsNhopSourceRelative` | `0x1d6159a0` | `return byte[this+0x3fb]` | HIGH (byte-exact body) |
| `DragonfishTarget::IsNhopSourceRelative` | `0x1d48f020` | per-slice byte + `CHECK(!base)` | HIGH (byte-exact body) |
| `GetDefaultConfiguredProperties` | `0x20acee40` | `is_nhop = (routing_strategy == 2)` | HIGH (byte-exact body) |
| `tpu::IsReachableOverLimitedIci` | `0x1fc57fe0` | `entry.target_port >= 0` reachability query | HIGH |
| `tpu::RoutingTableEntryForICILimitedRouting` | `0x1fc58040` | 2-D limited-ICI route entry; `ROUTING_MESH` gate | MEDIUM (gate + call chain; full body on routing pages) |

| Enum | Values |
|---|---|
| `Orientation` (`dimension.proto`) | `0=UNKNOWN_ORIENTATION, 1=X, 2=Y, 3=Z, 4=A, 5=B, 6=C` |
| `OrientationToDimension(o)` | `o == 0 ⇒ INVALID_ARGUMENT; else o − 1` (`X/Y/Z = 0/1/2`, `A/B/C = 3/4/5`) |
| `Polarity` (`dimension.proto`) | `0=UNKNOWN_POLARITY, 1=POSITIVE, 2=NEGATIVE` |
| `TpuRoutingStrategyProto` | `0=ROUTING_DEFAULT, 1=ROUTING_MESH, 2=ROUTING_NHOP` |
| `TwistedTorusShape` | `0=UNSPECIFIED, K_K_2K, K_2K_2K, K_2K_NK` |

---

## 5. What Was Not Resolved

- **`Orientation` `A/B/C` producers.** `OrientationToDimension` supports six logical dimensions, but which slice topology ever emits orientation `4/5/6` — i.e. where a `>3`-D logical ICI dimension arises (a multi-pod or optical-switch logical axis beyond the physical 3-D torus) — was not traced. The physical `X/Y/Z` (`1/2/3`) path is fully proven; the `A/B/C` callers were not. LOW.
- **The Dragonfish per-slice byte source.** `DragonfishTarget::IsNhopSourceRelative` returns `byte[[Target+0x940]+0x49]`; which writer populates that per-slice struct byte (vs the `CreateFromTopology` path that fills `Target[+0x3fb]`) was not decoded. MEDIUM.
- **`RoutingScheme` enum value set.** `DmaDestinationRoutingTableEntryMapper::Map` dispatches on a `RoutingScheme` argument (`2 ⇒ MapTwoAxesReachable`, `0 ⇒ MapOneTwoFourEightHopNeighborsReachable`); the full enum (names/numbers) and the 1/2/4/8-hop offset tables are owned by the routing section, not transcribed here. Out of scope.

---

## Cross-References

- [Twisted-Torus Overview](overview.md) — the dateline-twist motivation, the `TwistedTorusND` / `TwistedTorusTopology` class split, and the section map
- [BuildStrategy](buildstrategy.md) — the driver this predicate guards: the base ND ring plus the per-color seam phases that consume the `K`/`2K` counts
- [Shape Folds](shape-folds.md) — the `K_K_2K` / `K_2K_2K` / `K_2K_NK` cases the `(num_max, num_min)` counts classify
- [GetReplicaPair3DOnTwistedTorus](get-replica-pair-3d.md) — the `+K`-mod-`2K` coordinate fold the twist applies
- [Degraded-Axis Ingest](../collectives/degraded-axis.md) — the `Orientation → degraded-byte` map (`OrientationsToTpuDegradedAxes`) this page corrects, and the `TpuConfiguredProperties` POD the `is_nhop` byte (`+0x3`) rides
- [SelectNDStrategy](../collectives/strategy-nd-picker.md) — the picker C-ii branch that constructs a `TwistedTorusND` only when the shape looks twisted
- [GetStaticPath](../routing/get-static-path.md) — the packed `(hop << 6 | polarity << 3 | orientation)` route hop the enum feeds, and the n-hop route table the source-relative flag rebases
- [Route-Table Generation](../routing/route-table-generation.md) — the `GetNextHopAction` route-emission layer that reads `is_nhop_source_relative`
