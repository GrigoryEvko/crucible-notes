# AllGather ND-Ring

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ. `.text` VMA equals file offset; all addresses are VMA.*

## Abstract

The ND-ring AllGather is how the jellyfish backend turns an HLO `all-gather` (opcode 6) into ICI ring traffic over a 1-D / 2-D / 3-D physical torus. Unlike the all-reduce family, which decomposes into reduce-scatter + all-gather phases over a *count* of active axes, the AllGather emitter is explicitly **axis-decomposed**: it materializes one per-axis `RingLocation` table per mesh axis and, at emit time, walks the rings axis-by-axis, advancing one mesh ring per DMA phase. The reader stage that does this is a clean two-stage composition: `GetShardIndex` computes each axis's per-step ring coordinate with a single modular step `(base ± step) mod ring_len` over the precomputed `RingLocation` state, then `GetOffset` linearizes the full per-axis coordinate vector into a flat gather-buffer slot via a minor-to-major mixed-radix dot product. This is the TPU analog of an MPI ring all-gather generalized to a rectangular torus, with the ring rotation expressed as modular arithmetic over device ordinals rather than an explicit neighbor walk.

The choice of *how many* axes to ring over is not a single immediate. It is a conjunction: a `TpuCompilationEnvironment` enable byte (`+0xbe` for 2-D, `+0xc0` for 3-D) **and** whether the collective's device list geometrically projects onto a 2-axis or 3-axis plane, tested by `ReplicaGroupsOnNDPlane(plane_dim=2/3)`. `ReplicaGroupsOnNDPlane` is the builder of the per-axis `MeshNDInfo` vector; the projection succeeds iff the device list fits a `plane_dim`-axis plane. `Init2DAllGather` / `Init3DAllGather` then re-verify the projection by counting the active-axis bits in `MeshNDInfo+0x38` (popcount 2 or 3) before installing the ring-order and per-dim size vectors that the readers consume.

This page owns the **ND-ring AllGather shard math** (`GetShardIndex` / `GetOffset` / `ComputeAdjustedIndexAtRuntime`), the **2-D / 3-D mesh-dimensionality selector** (`UseAllGather2D` / `UseAllGather3D`), the **per-axis ring install** (`Init{1,2,3}DAllGather` → `InitDim`), and the **`MeshNDInfo` ring geometry** the readers index. The *generation* of the per-axis device→neighbor tables (`CreateStaticNDRingReplicaInfoTable`) lives on [Constant Mapper](constant-mapper.md); the *strategy choice* that routes an all-gather here at all is on [SelectNDStrategy](strategy-nd-picker.md); the *cost* of the ring is on [SPMD Link-Count Cost](spmd-link-count-cost.md). This page links those and does not re-derive them.

For reimplementation, the contract is:

- **The shard math.** `GetShardIndex` reads `RingLocation[dim]` and `ring_len = sizes[dim]`, lifts every axis's ring base into a coordinate vector, computes this axis's stepped coordinate as `(base ± step) mod ring_len` (bidir vs forward chosen by a bool), pins non-walked axes to ordinal 0, and calls `GetOffset` to relinearize.
- **The linearizer.** `GetOffset` is a minor-to-major mixed-radix dot product `offset = (Σ_k coords[mtm[k]] · Π_{j<k} bounds[mtm[j]]) mod Π bounds`, guarded by a three-span equal-length `RET_CHECK`.
- **The selector.** 1-D / 2-D / 3-D = `(env enable byte) ∧ (ReplicaGroupsOnNDPlane projects onto a 2/3-axis plane)`, with `MeshNDInfo+0x38` popcount = `#active mesh axes` re-verified at install.

| | |
|---|---|
| **Shard-index reader** | `xla::jellyfish::(anonymous namespace)::GetShardIndex` @ `0x13811600` |
| **Buffer linearizer** | `xla::jellyfish::(anonymous namespace)::GetOffset` @ `0x138106c0` (`all_gather_emitter.cc:164`) |
| **Short-ring rescale** | `ComputeAdjustedIndexAtRuntime` @ `0x13800d00` |
| **2-D selector** | `AllGatherEmitter::UseAllGather2D` @ `0x13801740` (env `+0xbe`, `plane_dim=2`) |
| **3-D selector** | `AllGatherEmitter::UseAllGather3D` @ `0x13801a40` (env `+0xc0`, `plane_dim=3`) |
| **Plane projector** | `ReplicaGroupsOnNDPlane` @ `0x1c890960` → `optional<vector<MeshNDInfo>>` |
| **Per-axis install** | `Init1DAllGather` @ `0x13807180` / `Init2DAllGather` @ `0x13807720` / `Init3DAllGather` @ `0x13807aa0` |
| **Per-axis ring fill** | `AllGatherEmitter::InitDim` @ `0x13804980` → `RingLocation[]` at `this+0x408` |
| **`MeshNDInfo` size** | `0x40` B (copy ctor `0x127b5100`) — axis-id vec / size vec / ring-order vec / dim bitmask |
| **`RingLocation` stride** | `0x38` B (7 qwords) |
| **Confidence** | HIGH (decompile-verified bodies for `GetShardIndex`, `GetOffset`, `UseAllGather2D`, `Init2DAllGather`) unless a row/callout says otherwise |

---

## Where This Sits

`SelectNDStrategy` ([SelectNDStrategy](strategy-nd-picker.md)) decides *that* an all-gather is realized as an ND ring; this page is the **emit-time read pipeline** that the chosen ring actually runs. The build side — `CreateStaticNDRingReplicaInfoTable` registering one `device_id → ring-neighbor` table per mesh axis as ConstantMapper Types 0/1/2 — is on [Constant Mapper](constant-mapper.md). This page is the *consumer*: the dimensionality selector that decides how many of those tables to install, the `InitDim` walk that materializes each axis's `RingLocation` from its table, and the `GetShardIndex` / `GetOffset` arithmetic that turns a ring position into a flat gather-buffer slot.

The pipeline, end to end:

```text
UseAllGather2D / UseAllGather3D          (dimensionality select: env byte ∧ plane projection)
        │   ReplicaGroupsOnNDPlane(plane_dim=2/3) → optional<vector<MeshNDInfo>>
        ▼
Init{1,2,3}DAllGather                     (popcount-verify MeshNDInfo+0x38, install ring-order + sizes)
        │   InitDim once per mesh axis  → RingLocation into this+0x408[dim]
        ▼
Phase{Zero,One,Two}DmaNDPlaneAllDimensionsStart   (one mesh ring advanced per DMA phase)
        │
        ▼
GetShardIndex (per axis)  ── (base ± step) mod ring_len  over RingLocation[dim]
        │
        ▼
GetOffset                 ── minor-to-major mixed-radix linearization → flat buffer slot
        │   (ComputeAdjustedIndexAtRuntime rescales when this ring axis is shorter than the longest)
        ▼
ICI DMA descriptor (PerBufferDmaEmitter)
```

---

## MeshNDInfo — the ND-ring geometry

The ring geometry is carried in a `MeshNDInfo` (0x40 bytes; copy ctor `0x127b5100`). One `MeshNDInfo` describes one *plane*: its active mesh axes, the ring length along each, the device ordinals along each ring, and a bitmask of which torus axes participate. `ReplicaGroupsOnNDPlane` builds a `vector<MeshNDInfo>` — `plane_dim` entries — and the AllGather installer reads its element `[0]`.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `minor_to_major` | `+0x00` | `vector<MeshDim>` (`int32×N`) | the mesh-axis id list, in minor-to-major (radix) order; `size` at `+0x08` |
| `dim_sizes` | `+0x18` | `vector<long>` | the per-dimension **ring lengths** (one per axis); `size` at `+0x20` |
| `ring_order` | `+0x28` | `vector<MeshDim>` (`int32×N`) | the **ring traversal order** — device ordinals along each ring; `size` at `+0x30` |
| `dim_bitmask` | `+0x38` | `long` | low-3-bits bitmask of **active torus axes**; `popcount(bitmask & 7)` = `#axes` |

The `dim_bitmask` at `+0x38` is the dimensionality oracle. `Is2D()` is `popcount(bitmask & 7) == 2`; `Is3D()` is `popcount(bitmask & 7) == 3`. `CreateStaticNDRingReplicaInfoTable` ([Constant Mapper](constant-mapper.md)) `RET_CHECK`s `mesh_info.Is2D() || mesh_info.Is3D()` (`net_util.cc:2440`), and the installers re-verify it (next section).

> **NOTE —** the `MeshNDInfo` triple — axis-id vector, ring-length vector, ring-order vector — maps one-to-one onto the three argument spans `GetShardIndex` / `GetOffset` consume: `minor_to_major` becomes the linearizer's radix order, `dim_sizes` becomes `bounds` (the modular ring lengths), and `ring_order` is the per-axis ordinal lookup baked into each `RingLocation`. A reimplementer should think of `MeshNDInfo` as "the radix decomposition of one ND ring."

> **GOTCHA —** the bit→axis mapping inside `+0x38` (which bit is mesh axis 0/1/2) is the structural reading consistent with the `+0x00` axis-id vector; only the popcount→dimension-count semantics are byte-confirmed (popcnt instruction at the `Is2D`/`Is3D` checks). Drive dimensionality off the popcount, not off a presumed bit position. (MEDIUM on the per-bit assignment, HIGH on the popcount.)

---

## The 2-D / 3-D Selector

### Purpose

`UseAllGather2D` and `UseAllGather3D` answer "ring over 2 axes or 3 axes?" Each is a conjunction of an environment enable byte and a geometric plane-projection test. They are tried in `AllGatherEmitter::GenerateConstants` (`0x13801be0`): 3-D first (`0x13801dfd`), then 2-D (`0x1380203f`), else the 1-D fallback; the explicit-routing path (`ShouldUseExplicitRouting` @ `0x13803aa0`) instead builds a Type-5 route schedule (see [Routing](../routing/overview.md)).

### Algorithm

```c
// AllGatherEmitter::UseAllGather2D — 0x13801740. Returns {projected_groups, has_value} (0x18 B).
function UseAllGather2D(target, hlo, dev_assign, env, bidir):
    // ENABLE GATE
    if env == nullptr:
        if hlo->GetModule() == nullptr: return {_, false}
        env = GetTpuCompEnv(hlo, target)
    if IsPartOfCollectiveComputeFusion(hlo, target): return {_, false}   // 0x1380176f
    if env[0xbe] == 0:                  return {_, false}   // 2-D enable byte (190)
    if dev_assign == nullptr:                                // fall back to static DA
        if module_config[+0x660] == 0:  return {_, false}    // static_device_assignment.has_value()
        dev_assign = HloModuleConfig::static_device_assignment(...)
    if target->vtable[0x18](target):    return {_, false}    // a Target capability veto

    // PROJECTION GATE
    device_list = GetCollectiveDeviceList(hlo, dev_assign)              // 0x13801871
    planes = ReplicaGroupsOnNDPlane(target, dev_assign, device_list,
                                    /*plane_dim=*/2, bidir)             // 0x138018a3 (mov $0x2,%r8d)
    if !planes.ok:                      return {_, false}               // success byte at result+0x18
    X = planes[0].dim_sizes[0]; Y = planes[0].dim_sizes[1]             // result+0x18 reads
    if env[0xbf] == 0 && hlo->opcode_field == 8                         // continuation-fusion arm
       && !ShouldUseContinuationFusionAllGather(...) && X != Y:
        return {_, false}                                              // square-plane requirement
    return {planes, true}                                             // has_value byte at result+0x10
```

`UseAllGather3D` (`0x13801a40`) is byte-identical in shape with two substitutions: the enable byte is `env[0xc0]` (`0x13801a65`) and the projection is `ReplicaGroupsOnNDPlane(..., plane_dim=3)` (`0x13801b52`, `mov $0x3,%r8d`); its projection-success byte sits at the same frame slot as the 2-D version (`cmpb $0x1,-0x38(%rbp)` @ `0x13801b5d`, i.e. the `optional`'s `has_value` byte), and its continuation-fusion override reads the env byte `env[0xc1]` (`cmpb $0x0,0xc1(%rax)` @ `0x13801b6a`) — the 3-D analog of the 2-D `env[0xbf]` arm.

| Selector | Enable byte | Projection | Success / shape test | Init path |
|---|---|---|---|---|
| `UseAllGather2D` | `TpuCompEnv[+0xbe]` | `ReplicaGroupsOnNDPlane(plane_dim=2)` | `has_value` byte @ `rsp+0x18` (`cmpb $0x1` @ `0x138018ae`); square-plane `X==Y` arm gated by env `+0xbf` | `Init2DAllGather` (2 axes) |
| `UseAllGather3D` | `TpuCompEnv[+0xc0]` | `ReplicaGroupsOnNDPlane(plane_dim=3)` | `has_value` byte @ `rsp+0x18` (`cmpb $0x1` @ `0x13801b5d`); continuation-fusion arm gated by env `+0xc1` | `Init3DAllGather` (3 axes) |

> **QUIRK —** the 2-D arm imposes a **square-plane** requirement: when the continuation-fusion path is *not* taken, it rejects `X != Y` (the two projected ring lengths must match). A reimplementer who allows rectangular 2-D planes unconditionally will over-select the 2-D ring on shapes the binary routes to 1-D. The continuation-fusion override (`ShouldUseContinuationFusionAllGather`) is the documented escape hatch from this; the `(ShapeSize & 3) == 0` alignment probe (`0x13801740` body) also forces the device-list path before projection.

> **GOTCHA —** `ReplicaGroupsOnNDPlane` is not a predicate — it is the **builder** of the `MeshNDInfo` vector, called with the desired `plane_dim`. Its `plane_dim` argument equals the `MeshNDInfo` count it returns, which equals the number of ND-ring tables and the popcount the installer re-verifies. A reimplementation that splits "decide dimensionality" from "build the ring tables" into two passes will diverge from the binary, which fuses them: the projection *is* the decision and the construction in one call. It is memoized on an `NDPlaneCacheKey → optional<vector<MeshNDInfo>>` cache (`0x225799b8`), so re-querying the same device list with the same `plane_dim` is free; the cache key is the device-assignment string ⊕ the serialized topology (`TpuTopologySerdes::Distill` → `TpuTopologyArgs::ToProto` → `SerializeToStringDeterministic`) ⊕ the `plane_dim` int ⊕ the bool, all under `nd_plane_cache_mutex`. The actual coordinate-projection math is **not** inline in `ReplicaGroupsOnNDPlane` — the entry function only builds the key, takes the lock, dispatches, and on a miss invokes a per-dimension lambda `ReplicaGroupsOnNDPlaneImpl::$_0` (`0x1c896400`) **once per mesh axis** (`plane_dim` ∈ {0,1,2}, passed as `0x100000000 | axis`). That lambda is where the projection lives; its body is decompiled on the [Tensor-Split / ND-Plane](tensor-split-ndplane.md) page's *dense `ReplicaGroupsOnNDPlane` projection* section.

---

## Per-Axis Ring Install

### Purpose

Once a `MeshNDInfo` vector is selected, `Init{1,2,3}DAllGather` install the rings: re-verify the dimensionality, drive `InitDim` once per mesh axis to materialize each axis's `RingLocation`, and copy the axis-order vector (`m0.minor_to_major` → `this+0x218`) and the per-dim-size vector (`m0.dim_sizes` → `this+0x1e8`) into the emitter so the readers can index them. The `MeshNDInfo`'s own `ring_order` field (`m0+0x28`) is *not* read by the installer; the emitter's order vector at `this+0x218` is built from `minor_to_major`.

### Algorithm

```c
// AllGatherEmitter::Init2DAllGather — 0x13807720. (Init3D @0x13807aa0 differs only in counts/popcount.)
function Init2DAllGather(meshes /*Span<MeshNDInfo>*/, hlo):
    RET_CHECK(!meshes.empty())                       // "mesh_info should have size larger than 1" (line 2970)
    if InitHierarchicalStates(...) != ok: return err  // line 2972
    this->mesh_axes /*this+0x1d0*/ = {axis0, axis1}    // assign 2 MeshDim ids
    for k in [0, this->mesh_axes.size):               // 0x1380779d, stride 4
        if InitDim(hlo, mesh_axes[k], /*reorder=*/0) != ok: return err   // line 2976
    this->initialized_2d /*this+0x19c*/ = 1            // 0x138077b5

    m0 = meshes[0]
    RET_CHECK(m0.minor_to_major[0] == y || == x)      // "could only be x or y" (line 2980)
    RET_CHECK(m0.minor_to_major.size == 2              // m0+0x08
              && m0.dim_sizes.size == 2                // m0+0x20  (cmpq $0x2,0x20(%r14) @0x138077e3)
              && popcount(m0.dim_bitmask & 7) == 2)    // m0+0x38 — "mesh_info[0].Is2D()" (line 2982)
    this->ring_order /*this+0x218*/ = m0.minor_to_major // from m0+0x00, count 2 (0x13807819)
    this->dim_sizes  /*this+0x1e8*/ = m0.dim_sizes     // from m0+0x18, count 2 (0x13807838)
    return ok
```

`Init3DAllGather` (`0x13807aa0`) is the same with the count-3 substitutions: `this+0x19d=1` (3D-initialized), the install copies count-3 `minor_to_major` (into `this+0x218`) and `dim_sizes` (into `this+0x1e8`), and the popcount check is implemented as `not(bitmask); test $7` (all low-3 bits set ⇔ popcount 3) rather than an explicit `popcnt == 3` (`mov 0x38(%r14),%eax; not %eax; test $0x7,%al` @ `0x13807b6e`). `Init1DAllGather` (`0x13807180`) calls `InitHierarchicalStates`, assigns a single-element axis list, and runs `InitDim` once with `reorder=1`.

> **NOTE —** the redundant popcount re-verification inside `Init2D`/`Init3D` is deliberate, not paranoia: `ReplicaGroupsOnNDPlane`'s result is cached and may be reused across selector calls, so the installer asserts the cached `MeshNDInfo` actually matches the dimensionality it is about to install. The two `RET_CHECK`s are different facts — `minor_to_major[0] ∈ {x, y}` (the minor axis is a real torus axis, line 2980) and `Is2D()` (exactly two active axes, line 2982).

### InitDim — the RingLocation fill

`InitDim` (`0x13804980`) populates one axis. It reads the per-axis constant for this `MeshDim` — `GetConstant(MeshDim==dim)` (the ND-ring neighbor table, Type 0/1/2; `0x13804c58`), with `GetConstant(Type 3)` (static ND-ring; `0x13804c4e`) and `GetConstant(Type 4)` (limited-ICI routing; `0x13804ae6`) as alternates — then calls `net_util::GetRingLocation` (`0x1c6a0c40`), or `GetRingLocationWithReordering` (`0x1c6a19c0`) on the reorder path, and stores the resulting `0x38`-byte `RingLocation` into `this+0x408[dim]`.

```text
this+0x408 / +0x410 / +0x418  : vector<RingLocation> {data, size, cap}   (per-axis ring state)
  RingLocation stride 0x38 (7 qwords); RingLocation[dim].first is this axis's ring base value
```

`this+0x408` is exactly the `rings` span `GetShardIndex` indexes, one element per mesh axis, populated one axis at a time by `InitDim` under the `Init*` driver.

---

## The Shard Math

### GetShardIndex — one ring rotation per axis

`GetShardIndex` (`0x13811600`) computes, for one mesh axis at one ring step, the flat gather-buffer slot the local core should read. The rotation is a single modular step over the precomputed `RingLocation` state — there is no neighbor-walk loop at emit time; the neighbor relation is baked into the per-axis tables and the step is arithmetic.

### Algorithm

```c
// (anon)::GetShardIndex — 0x13811600
// rings = this+0x408 (Span<RingLocation>, stride 0x38 = 7 qwords)
// sizes = MeshNDInfo dim_sizes (the ring lengths)
function GetShardIndex(rb, base /*LloValue*/, dim, bidir,
                       rings, sizes, minor_to_major, coords_span):
    ring_base = rings[dim].first        // a5[7*dim]            — this axis's ring base value
    ring_len  = sizes[dim]              // a7[2*dim]            — the modulus for this axis

    // (1) lift every axis's ring base into a coordinate vector  (grow loop @0x13811670)
    coords = vector<LloValue*>()
    for k in [0, rings.size):
        coords.push_back(rings[k].first)

    // (2) THE RING ROTATION — this axis's per-step coordinate, modulo the ring length
    if bidir:                                              // a4 != 0
        // (ring_base - base + ring_len) mod ring_len   — backward+forward step
        t = rb.SsubS32(ring_base, base)                   // 0x1381175f
        t = rb.SaddS32(t, rb.SimmS32(ring_len))           // 0x1381178e (+ring_len keeps it ≥0)
    else:
        // (ring_base + base) mod ring_len               — forward-only step
        t = rb.SaddS32(ring_base, base)                   // 0x1381178e
    stepped = rb.SmodU32(t, ring_len)                     // 0x1381179c — modulus = sizes[dim]

    // (3) write the stepped coordinate back; pin non-walked axes to ordinal 0
    coords[dim] = stepped                                 // 0x138117b9
    for slot in coords_span:                              // 0x138117d0
        coords[slot] = rb.SimmS32(0)

    // (4) relinearize the full coordinate vector into the flat buffer slot
    offset = GetOffset(minor_to_major, coords, sizes, rb) // 0x13811813
    free(coords); return offset
```

The bidir/forward split is the only branch: a bidirectional ring computes `(ring_base − base + ring_len) mod ring_len` (the `+ring_len` guarantees a non-negative argument to the unsigned mod), a unidirectional ring computes `(ring_base + base) mod ring_len`. Every other axis is pinned to ordinal 0, so a single `GetShardIndex` call advances exactly one ring while holding the rest fixed — which is why the three DMA phases each advance a different axis.

> **QUIRK —** the modulus is always `sizes[dim]` (the ring length of *this* axis), and `SmodU32` is unsigned. The `+ring_len` before the mod in the bidir arm is not a stylistic choice — it is the correctness guard: `ring_base − base` can be negative, and an unsigned mod of a negative-wrapped value is wrong. A reimplementer who folds the bidir arm to `(ring_base − base) mod ring_len` will produce wrong shard indices for every backward step. (HIGH — confirmed in the decompile: `SsubS32` then `SaddS32(SimmS32(ring_len))` then `SmodU32`.)

### GetOffset — minor-to-major mixed-radix linearization

`GetOffset` (`0x138106c0`) collapses the per-axis coordinate vector into one flat slot. It is a standard row-major linearization, but in *minor-to-major* radix order (the `minor_to_major` span chooses the axis order and the running product of bounds).

### Algorithm

```c
// (anon)::GetOffset — 0x138106c0
function GetOffset(minor_to_major, coords, bounds, rb):
    // all three spans must be the same length
    RET_CHECK(minor_to_major.size == coords.size
              && coords.size == bounds.size)      // all_gather_emitter.cc:164
    if minor_to_major.size == 1:
        return coords[0]                          // single-axis fast path (0x138106fb)

    acc = rb.SimmS32(0)
    for k in [0, n):
        term = coords[minor_to_major[k]]
        for j in [0, k):                          // running radix product
            term = rb.SmulU32(term, rb.SimmS32(bounds[minor_to_major[j]]))
        acc = rb.SaddS32(acc, term)               // 0x1381074e
    radix = Π_k bounds[minor_to_major[k]]          // vectorized vpmulld @0x13810816
    return rb.SmodU32(acc, radix)                  // 0x13810898
```

That is `offset = (Σ_k coords[mtm[k]] · Π_{j<k} bounds[mtm[j]]) mod (Π_k bounds[mtm[k]])`. The final `mod (Π bounds)` wraps the linearized index into the gather buffer's slot count. The bounds product is computed with a hand-vectorized `vpmulld` reduction (4-wide, seeded from `dword_84A2B08 == 1`) over the bounds array, with a scalar tail for the remainder — a micro-optimization that does not change the formula.

> **GOTCHA —** the `RET_CHECK` proves the span identities `minor_to_major.size == coords.size == bounds.size`; this is what tells a reverse-engineer the three opaque spans are exactly `{radix order, per-axis coordinates, per-axis ring lengths}`. If the reimplementation passes a `bounds` array indexed by raw axis id rather than by `minor_to_major[j]`, the running product is taken over the wrong radix order and the offset is silently wrong for any non-trivial minor-to-major permutation. Index `bounds` through `minor_to_major`, exactly as the inner loop does (`bounds[minor_to_major[j]]`, decompiled at `0x13810790`).

### ComputeAdjustedIndexAtRuntime — the short-ring rescale

`GetShardIndex`'s slot is computed against this axis's ring length; when one mesh axis is shorter than the longest, the index must be scaled up so the shorter ring's slot maps into the full gather buffer. `ComputeAdjustedIndexAtRuntime` (`0x13800d00`) does this, gated by a predicate mask:

```c
// ComputeAdjustedIndexAtRuntime — 0x13800d00
function ComputeAdjustedIndexAtRuntime(rb, core_idx, idx, dim):
    p0 = rb.SeqS32(core_idx, 0)                       // 0x13800d3e
    p1 = rb.SeqS32(core_idx, 1)                       // 0x13800d62
    is2d = (this->mesh_axes.size /*this+0x1d8*/ == 2) // 0x13800d6c
    mask = p0 | (~is2d & p1)                           // Pimm/Pneg/Pand/Por (0x13800d7c..)
    ratio = 1                                          // default (LODWORD(v10)=1)
    if !GetTpuCompEnv[+0x13f2]:                         // 0x13800db5 — rescale enabled when byte is 0
        max_dim = max_element(this->dim_sizes)        // this+0x1e8 / size this+0x1f0 (0x13800e10..)
        ratio   = max_dim / dim_sizes[dim]            // idiv (0x13800f10)
    scaled  = rb.SdivS32(idx, ratio)                  // 0x13800f30
    return rb.Sselect(mask, idx, scaled)              // 0x13800f41 — args: (mask, idx, scaled)
```

The rescale divides the index by `max_dim / ring_len[dim]` and emits an `Sselect` over the per-core predicate `mask`; the binary passes the arguments as `Sselect(mask, idx, scaled)` (verified at `0x13800f3b`: `%rdx`=idx, `%rcx`=scaled). When the env byte `+0x13f2` is set, the `max_element` scan is skipped and `ratio` stays `1`, so the scaled branch collapses to `idx`. Its callers are the async window-emission path: `AsyncAllGatherEmitter::EmitWindow` (`0x137eec20`, call `0x137eed9a`) and `AsyncAllGatherEmitter::MaybeEmitWindow` (`0x137eefa0`, calls `0x137ef393` / `0x137ef583`) — not the `GetPhazeZeroShardIndexHelper` / `GetShardIndex` chain. The separate hierarchical-split recurrence (`MaybeMapShardIndexForHierarchicalSplit` @ `0x138108e0`, the per-chip core-split fold reached from `GetPhazeZeroShardIndexHelper` @ call `0x137f1818`) was only partially traced (LOW).

### Call sites — one ring per DMA phase

`GetShardIndex` has three callers, all in the AllGather emitter, and each passes the emitter's `this+0x408` `RingLocation` array as `rings`:

| Caller | Address | Role |
|---|---|---|
| `GetPhazeZeroShardIndexHelper` | `0x137f1780` (call `0x137f1803`) | phase-0 shard index + hierarchical-split map |
| `PhaseOneDmaNDPlaneAllDimensionsStart` | `0x137f42c0` (call `0x137f5011`) | phase-1 ring step (single-element MeshDim) |
| `PhaseTwoDmaNDPlaneAllDimensionsStart` | `0x137f9060` (call `0x137f9e6d`) | phase-2 ring step |

The three phases advance the rings in turn; the exact per-axis division of labor (which axis each phase walks, the send/recv SFLAG interplay via `GetRecvFlags` @ `0x137f5cc0`, and the `PipelinedAllGatherSlots` @ `0x137f4100` overlap) is the DMA-loop layer and was not traced here (it belongs to the [Routing](../routing/overview.md) / [ICI fabric](../ici/overview.md) sections).

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `(anon)::GetShardIndex` | `0x13811600` | per-axis ring rotation `(base±step) mod ring_len` | HIGH (decompile-verified) |
| `(anon)::GetOffset` | `0x138106c0` | minor-to-major mixed-radix linearizer | HIGH (decompile-verified) |
| `ComputeAdjustedIndexAtRuntime` | `0x13800d00` | short-ring index rescale | HIGH |
| `GetPhazeZeroShardIndexHelper` | `0x137f1780` | `GetShardIndex` + hierarchical-split wrapper | HIGH |
| `MaybeMapShardIndexForHierarchicalSplit` | `0x138108e0` | per-chip core-split shard remap | LOW (partial trace) |
| `AllGatherEmitter::UseAllGather2D` | `0x13801740` | 2-D selector (env `+0xbe` ∧ `plane_dim=2`) | HIGH (decompile-verified) |
| `AllGatherEmitter::UseAllGather3D` | `0x13801a40` | 3-D selector (env `+0xc0` ∧ `plane_dim=3`) | HIGH |
| `ReplicaGroupsOnNDPlane` | `0x1c890960` | plane projector / `MeshNDInfo` builder (cached) | HIGH (return type, plane_dim); internals LOW |
| `GetCollectiveDeviceList` | `0x13801940` | device-list resolver for the projection | HIGH |
| `AllGatherEmitter::Init1DAllGather` | `0x13807180` | single-axis install | HIGH |
| `AllGatherEmitter::Init2DAllGather` | `0x13807720` | 2-axis install + popcount-2 verify | HIGH (decompile-verified) |
| `AllGatherEmitter::Init3DAllGather` | `0x13807aa0` | 3-axis install + popcount-3 verify | HIGH |
| `AllGatherEmitter::InitDim` | `0x13804980` | per-axis `RingLocation` fill into `this+0x408` | HIGH |
| `net_util::GetRingLocation` | `0x1c6a0c40` | `RingLocation` from the ND-ring InfoTable | HIGH |
| `MeshNDInfo` copy ctor | `0x127b5100` | 0x40-B geometry: axis-id / sizes / ring-order / bitmask | HIGH |

---

## What Was Not Resolved

- **`ReplicaGroupsOnNDPlane` internals.** This page confirms it returns `optional<vector<MeshNDInfo>>`, is cached on `NDPlaneCacheKey`, and that `plane_dim` = `MeshNDInfo` count = ring dimensionality. The coordinate-projection itself lives in the per-axis lambda `ReplicaGroupsOnNDPlaneImpl::$_0` (`0x1c896400`), not inline in the `0x1c890960` entry: each device's `chip_coordinates` (via `TensorCoreLocationForLogicalDeviceId` `0x1c8904e0`) is mixed-radix-linearized against the mesh extents, then dispatched to `ReplicaGroupForm2DRing` (`0x1c88e6e0`) or `ReplicaGroupsOn3DPlane` (`0x1c8901e0`) by `n_dim`, with an `n_dim==1` short-circuit; the selected axis's extent is pre-multiplied by `LogicalDevicesPerChip` to fold the sub-cores into that ring. Decompiled on [Tensor-Split / ND-Plane](tensor-split-ndplane.md). HIGH for the dispatch + the sub-core fold; the exact `MeshNDInfo+0x38` bit→axis assignment remains structural (see next bullet).
- **The per-bit assignment of `MeshNDInfo+0x38`.** popcount→dimension-count is byte-confirmed; which bit selects mesh axis 0/1/2 is the structural reading. MEDIUM.
- **`MaybeMapShardIndexForHierarchicalSplit` (`0x138108e0`).** The post-`GetShardIndex` per-chip core-split remap was only partially read (`CoreIndex` / `LogicalDevicesPerChip` `idiv` at `0x13810932`); the full recurrence and its no-op conditions were not traced. LOW.
- **The Phase{Zero,One,Two} DMA scheduling.** `GetShardIndex` is proven called once per ring per phase; which axis each phase advances and the SFLAG send/recv interplay (`GetRecvFlags`, `PipelinedAllGatherSlots`) were not traced — they are the DMA-emission layer.

---

## Cross-References

- [Collectives Overview](overview.md) — the substrate split and the op-family dispatch that routes `all-gather` here
- [SelectNDStrategy](strategy-nd-picker.md) — the strategy picker that decides an all-gather becomes an ND ring; the `[obj+0xa8]` 1-D-vs-ND gate
- [Constant Mapper](constant-mapper.md) — `CreateStaticNDRingReplicaInfoTable`: the per-axis `device_id → ring-neighbor` tables (Types 0/1/2) that `InitDim` reads
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — the AllGather cost branch (1D ÷2, 2D ÷4) and the ICI `ResourceVector` slots
- [AllToAll Tables](alltoall-tables.md) — the A2A barrier-membership table generation (sibling table family)
- [ReduceScatter](reduce-scatter.md) — the reduce-scatter phase that pairs with all-gather in the all-reduce decomposition
- [Degraded-Axis Ingest](degraded-axis.md) — the fault-tolerant axis remap that demotes a failed axis out of the primary ring
- [Twisted Torus](../twist/overview.md) — the non-rectangular ring geometry the twisted strategy targets
- [Routing](../routing/overview.md) — the route-table / DMA-phase layer that consumes the `GetShardIndex` offsets
- [ICI fabric](../ici/overview.md) — the inter-chip interconnect DMA layer
- [back to index](../index.md)
