# Degraded-Axis Ingest — `TpuDegradedAxesProto` → `Target` Degraded Bytes → the Resilient ND Ring

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ. `.text` VMA equals file offset; all addresses are VMA.*

## Abstract

When an ICI link on a TPU slice fails at bring-up, the slice is not abandoned. The runtime records *which torus axis* the faulty link belongs to, carries that fact through compilation as three booleans, and the collective-ring picker folds the degraded axis out of the primary all-reduce ring. This page documents the **producer** end of that mechanism: how a faulty-link *orientation* is reduced to a per-axis degraded byte, the proto and POD it travels in, the `Target` struct ingest, and the per-color `RingLocation` ring-construction fold that consumes it. The *picker* side — `GetDegradedAxis`, `InitColorDimensionsDegraded`, the resilient gate, and the `IciResource` slot map — is on [SelectNDStrategy](strategy-nd-picker.md); this page links it rather than duplicating it.

Three functions form the ingest spine. `tpu::OrientationsToTpuDegradedAxes` (`0x1fc57d00`) maps a vector of faulty-link `Orientation` enum values to three degraded bytes (orientation `1`→X, `2`→Y, `3`→Z). Those bytes flow through `TpuDegradedAxesProto{x,y,z}` (a 3-bool proto nested as `degraded_axes` in `TpuConfiguredPropertiesProto`) and a parallel 8-byte in-process POD `tpu::TpuConfiguredProperties`. `xla::jellyfish::target::CreateFromTopology` (`0x1d48e520`) is the **writer**: it copies the four flat config bytes into `Target[+0x3f8..+0x3fb]` and the routing-strategy `int32` into `Target[+0x3fc]`. From there the degraded byte triple `Target[+0x3f8..+0x3fa]` is exactly what `Target::Is{X,Y,Z}Degraded` reads, and what `StrategyND::BuildStrategy` (`0x137c4660`) reflects in the per-color `RingLocation` coordinate build that the AllReduce emitter walks.

A reader who knows MPI fault-tolerant collectives owns the frame: this is a one-axis "route-around-the-dead-link" record threaded from link discovery through the compiler into the physical ring geometry. The reimplementation contract is:

- **The proto + POD layout.** `TpuDegradedAxesProto` (3 bool fields, wire tags `0x08`/`0x10`/`0x18`, in-mem bytes `+0x18`/`+0x19`/`+0x1a`, has-bits `[+0x10]` bits 1/2/4), its parent `TpuConfiguredPropertiesProto`, and the flat 8-byte `tpu::TpuConfiguredProperties` POD the compile path passes by const reference.
- **The orientation→byte map.** `OrientationsToTpuDegradedAxes`: dense enum `0..6`, only values `1/2/3` set the X/Y/Z degraded byte; `0=UNKNOWN` and `4/5/6` set nothing.
- **The `Target` write.** `CreateFromTopology` byte-for-byte: `cfg[+0..+3] → Target[+0x3f8..+0x3fb]`, `cfg[+4]` (or `topo[+0xa4]` default) `→ Target[+0x3fc]`.
- **The ring-construction fold.** `BuildStrategy`'s `[obj+0xa8]==1` ND-ring gate, the per-color `color_dims` `memmove` fan-out, and the per-color `RingLocation` coordinate build that turns the (possibly degraded-remapped) `[6][3]` color-dimension table into the physical ring the emitter traverses.

| | |
|---|---|
| **Orientation→byte map** | `tpu::OrientationsToTpuDegradedAxes` @ `0x1fc57d00` (0x2e0 B) |
| **Faulty-link source** | `accel_ssw::deepsea::slice_builder::SliceBuilderHelper::StatSession` @ `0x1fbb5b80` |
| **Wire proto** | `tpu::TpuDegradedAxesProto` (3 bool) nested in `TpuConfiguredPropertiesProto.degraded_axes` (field 1) |
| **Serializer (layout proof)** | `TpuDegradedAxesProto::_InternalSerialize` @ `0x20adb880` |
| **Flat POD** | `tpu::TpuConfiguredProperties` (8-byte: x,y,z,nhop, routing_strategy) |
| **Default POD** | `tpu::GetDefaultConfiguredProperties` @ `0x20acee40` (degraded bytes = 0) |
| **Writer** | `xla::jellyfish::target::CreateFromTopology(…,TpuConfiguredProperties&,…)` @ `0x1d48e520` |
| **Degraded bytes** | `Target[+0x3f8]` (X), `[+0x3f9]` (Y), `[+0x3fa]` (Z); routing `[+0x3fc]` |
| **Accessors** | `Target::IsXDegraded` @ `0x1d615940` / `IsYDegraded` @ `0x1d615960` / `IsZDegraded` @ `0x1d615980` |
| **Ring builder** | `xla::jellyfish::StrategyND::BuildStrategy` @ `0x137c4660` (0xca0 B) |
| **Consumer (picker)** | `GetDegradedAxis` @ `0x1c894c20`, `InitColorDimensionsDegraded` @ `0x137c6580` — see [SelectNDStrategy](strategy-nd-picker.md) |
| **Confidence** | HIGH (decompile-verified bodies for all ingest functions) unless a row/callout says otherwise |

---

## Where This Sits

The degraded-axis path joins ICI bring-up to the collective picker. Upstream, link discovery and failure handling live in the ICI fabric — see [ICI Failure Modes & Recovery](../ici/failure-recovery.md); a failed link is what produces the faulty-link orientation this page ingests. Downstream, the byte triple this page writes into the `Target` is consumed by:

- The **resilient ND-ring picker** — `GetDegradedAxis` / `InitColorDimensionsDegraded` / `UseResilientAlgorithmBase` on [SelectNDStrategy](strategy-nd-picker.md). That page owns the *consumption*: reducing three degraded bytes to one axis index (or `-1`), and the `[6][3]` color-dimension remap.
- The **cost model** — the degraded axis's two `IciResource` slots leave the primary ring, dropping the `num_dims` divisor `3 → 2`; see [SPMD Link-Count Cost](spmd-link-count-cost.md).
- The **twisted-torus resilient variant** — `UseResilientAlgorithmTwistedTorus` reuses the same degraded gate; the twisted ring geometry is on [Twisted-Torus Overview](../twist/overview.md) and [TwistedTorusND::BuildStrategy](../twist/buildstrategy.md).

This page owns the **proto ingest, the degraded representation, and the `RingLocation` construction under degradation** — the producer side that `SelectNDStrategy` consumes.

```text
ICI link fails at bring-up          (ICI Failure Modes & Recovery)
        │  faulty-link Orientation enum (1/2/3 = +X/+Y/+Z axis)
        ▼
SliceBuilderHelper::StatSession  →  OrientationsToTpuDegradedAxes  (enum → x/y/z bytes)
        │
        ▼
TpuDegradedAxesProto{x,y,z}  ⊂  TpuConfiguredPropertiesProto.degraded_axes
        │   ≡  tpu::TpuConfiguredProperties POD (+0 x, +1 y, +2 z, +3 nhop, +4 routing)
        ▼
target::CreateFromTopology   →   Target[+0x3f8..+0x3fc]
        │
        ▼
GetDegradedAxis / InitColorDimensionsDegraded  →  [6][3] color_dims  (SelectNDStrategy)
        │
        ▼
StrategyND::BuildStrategy   →  per-color RingLocation  →  AllReduce emitter walk
```

---

## The Proto and POD Layout

The degraded record exists in two parallel forms: a serializable proto (used to carry the config across the runtime/compiler boundary and into the compilation-cache key) and a flat 8-byte POD passed by const reference through the in-process compile path.

### `TpuDegradedAxesProto` — three booleans

Defined in `…/tpu/runtime/topology/tpu_routing_strategy.proto` (proto3/editions). The byte-exact in-memory layout is read directly from `TpuDegradedAxesProto::_InternalSerialize` (`0x20adb880`):

```protobuf
message TpuDegradedAxesProto {     // descriptor ".tpu.TpuDegradedAxesProto"
  bool x = 1;   // wire tag 0x08; in-mem byte [this+0x18]; has-bit [this+0x10] & 0x1
  bool y = 2;   // wire tag 0x10; in-mem byte [this+0x19]; has-bit [this+0x10] & 0x2
  bool z = 3;   // wire tag 0x18; in-mem byte [this+0x1a]; has-bit [this+0x10] & 0x4
}
```

The serializer body proves each field independently — `has-bit & {1,2,4}` AND the value byte `== 1`, then it emits `{tag, value}`:

```c
// TpuDegradedAxesProto::_InternalSerialize @ 0x20adb880  (has-bits in *((_DWORD*)this+4) = [this+0x10])
int v4 = *((_DWORD *)this + 4);
if ((v4 & 1) && *((_BYTE *)this + 24) == 1) { *a2 = 8;  a2[1] = 1; a2 += 2; }   // x: tag 0x08, byte 0x18
if ((v4 & 2) && *((_BYTE *)this + 25) == 1) { *a2 = 16; a2[1] = 1; a2 += 2; }   // y: tag 0x10, byte 0x19
if ((v4 & 4) && *((_BYTE *)this + 26) == 1) { *a2 = 24; a2[1] = 1; a2 += 2; }   // z: tag 0x18, byte 0x1a
```

`TpuDegradedAxesProto` is nested as **field 1 `degraded_axes`** of `TpuConfiguredPropertiesProto`, alongside field 2 `is_nhop_source_relative` (bool) and field 3 `routing_strategy` (`TpuRoutingStrategyProto` enum). The three degraded bools map one-for-one, in X/Y/Z order, onto `Target[+0x3f8]`/`[+0x3f9]`/`[+0x3fa]` — the bytes `Target::Is{X,Y,Z}Degraded` read (see [SelectNDStrategy](strategy-nd-picker.md)).

### `tpu::TpuConfiguredProperties` — the flat 8-byte POD

The in-process form is an 8-byte POD distinct from the wire proto. It is passed by `const&` through the compile path (`TpuJitCompileTF`, `BuildXLADeviceAssignment`, the `TpuMeshCommonState` ctor, `TpuTopologyDescription`, the compilation cache) and formatted as `"degraded_axes("` in the cache raw-key (`GenerateCacheRawKey`, `.rodata 0xa17d7d6`, immediately after the `:` separator at `0xa17d7d5`):

| Offset | Field | Source |
|---|---|---|
| `+0x0` | `byte x_degraded` | `TpuDegradedAxesProto.x` |
| `+0x1` | `byte y_degraded` | `.y` |
| `+0x2` | `byte z_degraded` | `.z` |
| `+0x3` | `byte is_nhop_source_relative` | `TpuConfiguredPropertiesProto` field 2 |
| `+0x4` | `int32 routing_strategy` | field 3; default `= topo[+0xa4]` |

The no-degraded default is built by `tpu::GetDefaultConfiguredProperties` (`0x20acee40`), which returns the POD packed into a single 64-bit value with the degraded bytes zeroed:

```c
// GetDefaultConfiguredProperties @ 0x20acee40   (this = TpuTopology; +41*4 = topo[+0xa4])
return ((uint64_t)topo[+0xa4] << 32) | ((uint8_t)(topo[+0xa4] == 2) << 24);
//        ^ routing_strategy in high dword            ^ is_nhop_source_relative = (routing==2)
//        x = y = z = 0  (no degraded link in the default path)
```

> **NOTE —** the default packing places `routing_strategy` in the high dword and `is_nhop_source_relative` at bit 24 (POD byte `+0x3`), with the three degraded bytes at `+0x0..+0x2` left zero. A degraded slice instead arrives via `OrientationsToTpuDegradedAxes` (below), not `GetDefaultConfiguredProperties`.

---

## The Orientation → Degraded-Byte Map

### `OrientationsToTpuDegradedAxes` — `0x1fc57d00`

This function reduces a vector of faulty-link `Orientation` enum values to the three degraded bytes. Its argument is an `absl::StatusOr<std::vector<Orientation>>`; the `Orientation` is the dense enum `accel_ssw::deepsea::proto::Orientation` (`0..6`, `0 = UNKNOWN_ORIENTATION`). Only values `1`, `2`, `3` set a degraded byte:

```c
// tpu::OrientationsToTpuDegradedAxes @ 0x1fc57d00   (8-at-a-time unrolled int32 walk)
//   x = v8, y = v10, z = v9   (decompiler temporaries)
char x = 0, y = 0, z = 0;
for (int orient : vec) {           // vec = StatusOr.value() if ok; else propagate status
    if (orient == 1) x = 1;        // Orientation +X  ⇒ X axis degraded
    if (orient == 2) y = 1;        // Orientation +Y  ⇒ Y axis degraded
    if (orient == 3) z = 1;        // Orientation +Z  ⇒ Z axis degraded
}
result[0x8] = x;  result[0x9] = y;  result[0xa] = z;  result[0] = ok(=1);
```

The decompiled `LABEL_75` epilogue writes `result[8] = v8 (x)`, `result[9] = v10 (y)`, `result[10] = v9 (z)`, `result[0] = 1`, confirming the enum-to-byte map: orientation `1`→X, `2`→Y, `3`→Z. If the input `StatusOr` is not OK (`*(a2) != 1`), the function propagates the status without touching the bytes.

> **GOTCHA —** the `switch` in the decompile dispatches `case 1 → x`, `case 3 → z`, `case 2 → y`. The X/Y/Z byte slots in the *result* struct are `+8`/`+9`/`+0xa`, in X/Y/Z order, which is the same order as the proto bytes `+0x18`/`+0x19`/`+0x1a` and the `Target` bytes `+0x3f8`/`+0x3f9`/`+0x3fa`. Do not transpose Y and Z when re-deriving — the decompiler's `v9`/`v10` temporary numbering swaps them, but the *byte stores* are X@+8, Y@+9, Z@+0xa.

The dense enum runs `0..6`. The runtime caller-side note `"Only one faulty orientation is allowed but found two:"` (`.rodata`) mirrors the single-axis constraint enforced downstream by `GetDegradedAxis` (`≥2 degraded ⇒ -1`, on [SelectNDStrategy](strategy-nd-picker.md)).

> **NOTE — confidence LOW (orientations 4/5/6).** `OrientationsToTpuDegradedAxes` tests only `== 1`, `== 2`, `== 3`; enum values `4/5/6` produce no degraded byte. Whether `4/5/6` are the negative-axis orientations (`-X/-Y/-Z`, which would ideally fold to the same axis byte) or a distinct meaning was not confirmed from this function alone — it simply ignores them.

### `SliceBuilderHelper::StatSession` — the faulty-link source (`0x1fbb5b80`)

The sole caller of `OrientationsToTpuDegradedAxes` (verified by a call-site scan of `.text`) is the slice builder's session-stat path. It assembles a `std::vector<Orientation>` from the faulty links observed on the slice, calls `OrientationsToTpuDegradedAxes` (`0x1fbb5e4c`), then packs the three result bytes plus a fourth byte into a single dword for the slice-information summary:

```text
ecx = (x & 1) | ((y & 1) << 8) | ((z & 1) << 16) | (w << 24);   // w from a local
[SliceInformation + 0x8] = ecx;   [SliceInformation + 0x0] = ok(=1);
```

The dword bit-layout (`x@0`, `y@8`, `z@16`, `w@24`) is exactly the 4-byte prefix of `tpu::TpuConfiguredProperties` consumed by the writer below.

---

## The `Target` Write — `CreateFromTopology`

### `xla::jellyfish::target::CreateFromTopology(…,TpuConfiguredProperties&,…)` — `0x1d48e520`

This is the function that lands the degraded config in the `Target` struct. After constructing the per-`TpuVersion` raw `Target` via a resolved factory pointer (from `target::GetForVersion`, `0x1d49f500`), it copies the four flat config bytes from the `TpuConfiguredProperties&` (`a5`) into the `Target` and resolves the routing strategy:

```c
// CreateFromTopology @ 0x1d48e520   (v14 = constructed Target; a5 = TpuConfiguredProperties&; a2 = TpuTopology*)
*(_BYTE  *)(v14 + 1016) = *(_BYTE *)a5;          // cfg[+0] → Target+0x3f8   IsXDegraded
*(_BYTE  *)(v14 + 1017) = *((_BYTE *)a5 + 1);    // cfg[+1] → Target+0x3f9   IsYDegraded
*(_BYTE  *)(v14 + 1018) = *((_BYTE *)a5 + 2);    // cfg[+2] → Target+0x3fa   IsZDegraded
*(_BYTE  *)(v14 + 1019) = *((_BYTE *)a5 + 3);    // cfg[+3] → Target+0x3fb   is_nhop_source_relative
int v15 = *((_DWORD *)a5 + 1);                   // cfg[+4]  routing_strategy
if (!v15) v15 = *((_DWORD *)a2 + 41);            //   if zero, default = topo[+0xa4]
*(_DWORD *)(v14 + 1020) = v15;                   //          → Target+0x3fc
*(_QWORD *)(v14 + 2344) = a6;                    // MultiSliceTopologyAndLocation* → Target+0x928
*(_BYTE  *)(v14 + 2352) = a7;                    // bool → Target+0x930
```

The offsets are byte-exact: `1016 = 0x3f8`, `1017 = 0x3f9`, `1018 = 0x3fa`, `1019 = 0x3fb`, `1020 = 0x3fc`. This is the inverse of the `Target::IsXDegraded` accessor (`return *((uint8_t*)this + 1016)`, `0x1d615940`), closing the round-trip from proto byte to `Target` byte to accessor.

| Target offset | Field | Source byte |
|---|---|---|
| `+0x3f8` | `IsXDegraded` | `cfg[+0]` (proto `degraded_axes.x`) |
| `+0x3f9` | `IsYDegraded` | `cfg[+1]` (`.y`) |
| `+0x3fa` | `IsZDegraded` | `cfg[+2]` (`.z`) |
| `+0x3fb` | `is_nhop_source_relative` | `cfg[+3]` |
| `+0x3fc` | `routing_strategy` (`int32`) | `cfg[+4]`, or `topo[+0xa4]` if zero |
| `+0x928` | `MultiSliceTopologyAndLocation*` | argument `a6` |
| `+0x930` | bool | argument `a7` |

Two thin default-config wrappers feed this writer. Both first call `GetDefaultConfiguredProperties` (so the degraded bytes are zero) and then tail-call this writer: the `(TpuTopology const*, long)` overload (`0x1d48e460`, called by `CreateTargetFromDeepseaPlatform`, `0x10a4efe0`) and the `(TpuTopology const*, long, long, MultiSliceTopologyAndLocation const*)` overload (`0x1d48e4c0`, called by `tensorflow::GetTargetDescription`, `0xe9745e0`). The degraded bytes only become non-zero when a caller threads a `TpuConfiguredProperties` populated by `OrientationsToTpuDegradedAxes` directly into this writer overload.

> **NOTE —** the `routing_strategy` zero-means-default rule (`if (!cfg[+4]) cfg[+4] = topo[+0xa4]`) is the only branch in the byte ingest — every degraded byte is an unconditional copy. A reimplementer must apply the topo default only to the routing dword, not to the degraded bytes.

---

## The `RingLocation` Construction Under Degradation

`StrategyND::BuildStrategy` (`0x137c4660`, 0xca0 B; source `all_reduce_strategies.cc`) is where the (possibly degraded-remapped) `[6][3]` color-dimension table becomes the physical per-color `RingLocation` set the AllReduce emitter walks. The degraded fold does not change *this* function's structure — `BuildStrategy` reads whatever `color_dims` `ComputeColorDimensions` produced, and on a degraded slice that table came from `InitColorDimensionsDegraded` (the dead axis demoted to column `[2]`; see [SelectNDStrategy](strategy-nd-picker.md)). The effect of degradation is therefore a *re-ordered* `color_dims` feeding an unchanged ring builder.

### The `[obj+0xa8]` ND-ring gate + color_dims fan-out

```c
// BuildStrategy @ 0x137c4660
if (*((_BYTE *)this + 168) == 1) {        // [obj+0xa8] == 1  ⇒ ND-ring path
    *((_QWORD *)this + 185) = 1;          // [obj+0x5c8] = 1   unit stride for dim0
    --*((_QWORD *)this + 2);              // [obj+0x10]        num-active-dims adjust
    // per-color memmove fan-out: shift each color row's dim array by one slot.
    // rows are at [obj + {0xd0/0xd8, 0xe8/0xf0, 0x100/0x108, 0x118/0x120, 0x130/0x138, 0x148/0x150}]
    int64_t v5 = *((_QWORD *)this + 2);   // dim count; v7 = 8 * v5  (bytes)
    memmove((char*)this + 208, (char*)this + 216, v7);            // row 0  (0xd0 <- 0xd8)
    if (num_colors != 1) memmove((char*)this + 232, (char*)this + 240, v7);   // row 1
    if (num_colors != 2) memmove((char*)this + 256, (char*)this + 264, v7);   // row 2
    if (num_colors != 3) memmove((char*)this + 280, (char*)this + 288, v7);   // row 3
    if (num_colors != 4) memmove((char*)this + 304, (char*)this + 312, v7);   // row 4
    if (num_colors != 5) memmove((char*)this + 328, (char*)this + 336, v7);   // row 5
    if (num_colors != 6) __builtin_trap();   // ud1: >6 colors unreachable
}
// else [obj+0xa8] == 0: 1-D-ring path, no fan-out
```

The `[obj+0xa8]` byte is the `StrategyND` ctor's `p6` parameter (the `UniDirectionNDRingStrategy` vs `UniDirection1DRingStrategy` selector; see the [SelectNDStrategy ctor table](strategy-nd-picker.md)). The fan-out shifts each of up to six color rows' dimension arrays by one slot, gated on `num_colors != 1..6`.

### The wrap-flag load

The per-axis torus-wrap flags come from the chip config inside the `Target`:

```c
// chip_config = Target[+0x3b8]  (decompile: *((_QWORD*)v3 + 119))
int64_t cfg = *((_QWORD *)Target + 119);
v9  = *(uint16_t *)(cfg + 160);    // chip_cfg+0xa0
wrapX = v9 & 1;                    // → v102
wrapY = *(_BYTE *)(cfg + 161) & 1; // chip_cfg+0xa1  → v101
wrapZ = *(_BYTE *)(cfg + 162) & 1; // chip_cfg+0xa2  → v103
```

These three bytes (`chip_cfg+0xa0/+0xa1/+0xa2`) are the X/Y/Z per-axis torus-wrap flags; they select Torus (wrapped ring) vs Mesh (open ring) per dimension in `ComputeNeighbor`. The adjacent byte `chip_cfg+0xa3` is the twist/torus-wrap enable gate used by the twisted-torus path (see [Twisted-Torus Overview](../twist/overview.md)).

### The per-color `RingLocation` coordinate build

`net_util::RingLocation` is three `long`s (24 B). The per-color array begins at `[obj+0x18]`, stride `0x18`. For each color, for each active dim `d`, the coordinate is `extent · stride / divisor`:

```text
RingLocation = { coord[0], coord[1], coord[2] }      // 24 B, stride 0x18 per color
for color in [0 .. num_colors([obj+0x8]) - 1]:
  for d in [0 .. num_dims([obj+0x10]) - 1]:
     dim_idx  = color_dims[color][d]      // long[r13 + 0xb8 + d*8],  bounds < 3
     extent   = long[obj + 0xb8 + dim_idx*8]
     stride   = long[obj + 0x5c8 + d*8]   // d=0:0x5c8, d=1:0x5d0, d=2:0x5d8
     divisor  = long[obj + 0x5b0 + dim_idx*8]
     coord[d] = (extent * stride) / divisor      // 32-bit unsigned div fast path
     store coord[d] → long[r13 + d*8]
```

The decompile confirms the three division stores at the loop body — `*v19 = (unsigned int)v23 / (unsigned int)v24`, `v19[1] = … / …`, `v19[2] = … / …` — writing the three `RingLocation` longs. The `color_dims[6][3]` table consumed here is the one `ComputeColorDimensions` (normal) or `InitColorDimensionsDegraded` (degraded) produced; **under degradation the column `[2]` of every row is the dead axis**, so the coordinates that seed the ring walk for columns `[0]`/`[1]` are the two healthy axes only.

### The neighbour schedule

Each color/dim coordinate is then turned into a ring ordinal and a forward/backward neighbour:

- `StrategyND::ComputeOrdinal` (`0x137c5300`) — coordinate → ring-ordinal `LloValue`; called once per (color, active dim). The decompile shows the three per-dim calls (`dim` arg `0`/`1`/`2`) passing the per-dim wrap byte `*(&v101 + …)`.
- `StrategyND::ComputeNeighbor` (`0x137c5600`) — called **twice** per (color, active dim): offset `+1` (clockwise) and `-1` (counter-clockwise). The decompile confirms the six calls (dims `0`/`1`/`2`, each with `1` and `-1`). The dispatch reads `logical_devices = *(_QWORD*)(strat + 8*dim + 1480)` and a per-dim degree byte `*(strat + 24*color + 8*dim + 208)`, then branches on the per-dim `wrap` byte (arg `a6`, the `*(&v101 + dim_idx)` value threaded from `BuildStrategy`) and a `(logical_devices≥2 && dim==0)` "special" predicate to one of four builders. The non-special path `CHECK`s `logical_devices == 1` (`.rodata`, `target_factory`/`all_reduce_strategies.cc:314,322`):

| `wrap` | `special` (`logical_devices≥2` & dim0) | `logical_devices==1` | Builder |
|---:|---|---|---|
| 1 | yes | — | `Torus2DevicePhase0Neighbor` |
| 0 | yes | — | `Mesh2DevicePhase0Neighbor` |
| 1 | no | yes | `TorusStrideNPhasekNeighbor` |
| 0 | no | yes | `MeshStrideNPhasekNeighbor` |

All four deposit through `BaseStrategyND::UpdateNeighborLocation` (`0x137c5fa0`) into the per-color clockwise/counter-clockwise neighbour buffers. The result is, per color and per active torus dim, two ring neighbours (forward/backward) built as Torus (wrapped) or Mesh (open) hops from the `RingLocation` coordinates — the physical collective ring. The populated `StrategyND` becomes a `UniDirectionNDRingStrategy` (ctor `0x137d4700`) or, on the 1-D path, `UniDirection1DRingStrategy` (ctor `0x137d4a20`).

> **NOTE — confidence MEDIUM (degraded interaction with neighbour dispatch).** The degraded fold is fully proven at the `color_dims` level (dead axis → column `[2]`) and the coordinate build consumes that table verbatim. The per-color *emitter walk* — exactly which ring step targets which neighbour under the re-ordered table — is the recursive-doubling partner schedule documented on [Binomial / Recursive-Doubling](binomial-recursive-doubling.md); only the dispatch table above and the call structure are transcribed here.

---

## Ingest Chain Summary

| Stage | Symbol @ VMA | Data form / field |
|---|---|---|
| 1 detect | `SliceBuilderHelper::StatSession` @ `0x1fbb5b80` | `vector<Orientation>` (faulty links) |
| 2 map | `OrientationsToTpuDegradedAxes` @ `0x1fc57d00` | enum `1/2/3` → x/y/z bytes |
| 3 pack | `StatSession` tail | dword `x \| y<<8 \| z<<16 \| w<<24` |
| 4 wire | `TpuDegradedAxesProto{x,y,z}` | tags `0x08`/`0x10`/`0x18`; bytes `+0x18..+0x1a` |
| 5 carry | `tpu::TpuConfiguredProperties` (8-byte POD) | `+0 x`, `+1 y`, `+2 z`, `+3 nhop`, `+4 routing` |
| 6 write | `target::CreateFromTopology` @ `0x1d48e520` | → `Target[+0x3f8..+0x3fc]` |
| 7 use | `GetDegradedAxis` @ `0x1c894c20` ([SelectNDStrategy](strategy-nd-picker.md)) | → resilient 2-axis ND ring |
| 8 build | `StrategyND::BuildStrategy` @ `0x137c4660` | per-color `RingLocation` ring walk |

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `tpu::OrientationsToTpuDegradedAxes` | `0x1fc57d00` | enum `1/2/3` → x/y/z degraded bytes |
| `SliceBuilderHelper::StatSession` | `0x1fbb5b80` | faulty-link `Orientation` source; pack dword |
| `TpuDegradedAxesProto::_InternalSerialize` | `0x20adb880` | proto byte layout proof |
| `tpu::GetDefaultConfiguredProperties` | `0x20acee40` | no-degraded default POD |
| `target::CreateFromTopology` (`…,TpuConfiguredProperties&,…`) | `0x1d48e520` | the writer: cfg → `Target[+0x3f8..+0x3fc]` |
| `target::CreateFromTopology` (`…,MultiSliceTopologyAndLocation*`) | `0x1d48e4c0` | default-config wrapper (called by `GetTargetDescription`) |
| `target::CreateFromTopology` (`TpuTopology*,long`) | `0x1d48e460` | default-config wrapper (called by `CreateTargetFromDeepseaPlatform`) |
| `Target::IsXDegraded` / `IsYDegraded` / `IsZDegraded` | `0x1d615940` / `…960` / `…980` | read `Target[+0x3f8..+0x3fa]` |
| `StrategyND::BuildStrategy` | `0x137c4660` | `[obj+0xa8]` gate, color_dims fan-out, `RingLocation` build |
| `StrategyND::ComputeOrdinal` | `0x137c5300` | coord → ring-ordinal `LloValue` |
| `StrategyND::ComputeNeighbor` | `0x137c5600` | forward/backward neighbour, 4-builder dispatch |
| `BaseStrategyND::UpdateNeighborLocation` | `0x137c5fa0` | deposit neighbour into ring list |
| `UniDirectionNDRingStrategy::ctor` / `UniDirection1DRingStrategy::ctor` | `0x137d4700` / `0x137d4a20` | thin ND / 1-D ring wrappers |

---

## What Was Not Resolved

- **Orientation enum `4/5/6`.** `OrientationsToTpuDegradedAxes` folds only `1/2/3`; whether `4/5/6` are negative-axis orientations (`-X/-Y/-Z`) that should fold to the same axis byte, or a distinct meaning, was not confirmed. LOW.
- **`is_nhop_source_relative` consumer.** The 4th POD byte (`cfg[+3]` → `Target[+0x3fb]`) is written here; its effect in the ICI route-table / ring-direction layer was not traced. Only that `GetDefaultConfiguredProperties` sets it `= (routing_strategy == 2)` is proven. LOW.
- **Per-color emitter walk under degradation.** The `color_dims` re-order (dead axis → inner column) and the coordinate build are HIGH; the exact recursive-doubling partner sequence the emitter walks over the re-ordered table is owned by [Binomial / Recursive-Doubling](binomial-recursive-doubling.md). MEDIUM.
- **Negative-axis attribution upstream.** Which slice-discovery path emits the `vector<Orientation>` and how link polarity maps to a single axis is in the ICI layer — see [ICI Failure Modes & Recovery](../ici/failure-recovery.md). Not decoded here.

---

## Cross-References

- [SelectNDStrategy — the ND Collective-Algorithm Picker](strategy-nd-picker.md) — the *consumer* side: `GetDegradedAxis`, `InitColorDimensionsDegraded`, `UseResilientAlgorithmBase`, the `[6][3]` remap, and the `GetResourceFromIciResource` slot map
- [Collectives Overview](overview.md) — where the degraded path sits in the on-pod collective lowering
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — how the degraded axis's two ICI slots leave the primary ring (`num_dims` divisor `3 → 2`)
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — the per-rank partner schedule the constructed ring runs
- [Twisted-Torus Overview](../twist/overview.md) and [TwistedTorusND::BuildStrategy](../twist/buildstrategy.md) — the resilient twisted variant gated on the same degraded record
- [ICI Failure Modes & Recovery](../ici/failure-recovery.md) — the upstream link-failure detection that produces the faulty-link orientation
