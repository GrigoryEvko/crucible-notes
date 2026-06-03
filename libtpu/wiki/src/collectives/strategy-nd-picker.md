# SelectNDStrategy — the ND Collective-Algorithm Picker

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ. `.text` VMA equals file offset; all addresses are VMA.*

## Abstract

`xla::jellyfish::BaseStrategyND::SelectNDStrategy` (`0x137c78e0`, 2016 bytes) is the single function that decides *which collective-ring algorithm* an ICI all-reduce / reduce-scatter / all-gather is compiled into. Given the slice topology (a 1-D / 2-D / 3-D ICI torus), the `HloInstruction`, and a fistful of `TpuCompilationEnvironment` flag bytes, it walks a fixed decision tree and constructs exactly one of five terminal strategy objects: a **sub-plane subgroup** strategy, a generic **ND-ring** `StrategyND` (which itself resolves to a 1-D or N-D unidirectional ring at build time), an **N-way model-parallel** ring, a **twisted-torus** ring, or a **strided** ND ring. There is no cost-comparison search here — the picker is a sequence of predicate gates; the *cost* of the chosen ring is computed separately in the link-count model (see [SPMD Link-Count Cost](spmd-link-count-cost.md)), and the *neighbour schedule* the ring traverses is built downstream in `StrategyND::BuildStrategy`.

Layered on top of the picker is the **degraded-axis fault-tolerant remap**: when one torus axis has a partially-failed ICI link, the resilient path folds that axis out of the primary ring. `GetDegradedAxis` (`0x1c894c20`) reduces three per-axis "this dimension is degraded" flags to a single degraded-axis index (or `-1` = "cannot isolate, give up"); `InitColorDimensionsDegraded` (`0x137c6580`) then rewrites the `[6][3]` per-color ring-dimension table so the dead axis is demoted to the innermost ring dimension and the two healthy axes carry the rings. `GetResourceFromIciResource` (`0x1c894c00`) maps the resulting ring dimensions onto the ICI `ResourceVector` slots the cost model deposits cycles into.

A reader who knows MPI ring/recursive-doubling collectives owns the frame: this is the TPU's per-axis algorithm selector over a rectangular torus, plus a one-axis fault-tolerance escape hatch. The reimplementation contract is:

- **The decision tree.** The two top-level paths split on the `enable_sub_plane` argument, and within each the ordered guard predicates (TpuCompEnv flag bytes, opcode test, single-ND-plane test, topology-shape probes, cross-module fold) that select among the five terminal classes.
- **The strategy enum.** The five terminal classes, their `operator new` object sizes, their constructors, and the `StrategyND` ctor's distinguishing parameters (the `[obj+0xa8]` 1-D-vs-ND-ring selector consumed by `BuildStrategy`).
- **The degraded-axis fold.** The per-axis degraded predicate (`IsAxisDegraded && extent≥2 && usable`), the `≥2 ⇒ -1` reduction, and the `[6][3]` color-dimension remap that pushes the dead axis to the inner ring dimension.

| | |
|---|---|
| **Picker entry** | `xla::jellyfish::BaseStrategyND::SelectNDStrategy` @ `0x137c78e0` (0x7e0 = 2016 B) |
| **Single-ND-plane test** | `xla::jellyfish::ReplicaGroupsOnNDPlane(…, plane=2, false)` @ `0x1c890960` |
| **2-D-plane gate** | `BaseStrategyND::IsGroupNDPlane` @ `0x137c6700` (`NumNetworkDimensions==3`, NDtopo dims not all 1, env`[0x1015]==1`) |
| **N-way gate** | `BaseStrategyND::UseSpecialStrategyNDNWay` @ `0x137c6be0` (single-slice, comp-count ∈ {2,4}) |
| **Strided gate** | `BaseStrategyND::UseStridedStrategyND` @ `0x137c72e0` (single-slice, `NumNetworkDimensions==3`, `LogicalDevicesPerChip==1`) |
| **Terminal classes** | `StrategySubgroupND` (0x638), `StrategyND` (0x5f0), `TwistedTorusND` (0x610) |
| **`StrategyND` ctor** | `0x137c2f40` → `ComputeColorDimensions` `0x137c3ba0` → `BuildStrategy` `0x137c4660` |
| **Degraded-axis reducer** | `xla::jellyfish::GetDegradedAxis` @ `0x1c894c20` (0x180 = 384 B) |
| **Color-dim remap** | `BaseStrategyND::InitColorDimensionsDegraded` @ `0x137c6580` |
| **Resilient gate** | `UseResilientAlgorithmBase` @ `0x1c894da0` (env`[0x1116]==1`, `NumNetworkDimensions==3`, `GetDegradedAxis≠-1`) |
| **ICI slot map** | `GetResourceFromIciResource` @ `0x1c894c00` |
| **Confidence** | HIGH (decompile-verified bodies for all six core functions) unless a row/callout says otherwise |

---

## Where This Sits

`SelectNDStrategy` is one stage of the on-pod collective lowering. Upstream, the SPMD partitioner ([Collectives Overview](overview.md)) has already decided *that* a collective is needed and over which replica groups; `SelectNDStrategy` decides *how* that collective is realized on the physical ICI torus. Downstream:

- The **cost** of the ring it picks is priced by the per-kind cycle model and the ICI link-count divisor — see [SPMD Link-Count Cost](spmd-link-count-cost.md). The `GetResourceFromIciResource` slot map documented below is shared with that model.
- The **recursive-doubling / binomial emitter loop** (the per-rank partner schedule a `StrategyND` ring runs) is on [Binomial / Recursive-Doubling](binomial-recursive-doubling.md).
- The **twisted-torus topology** (the non-rectangular ICI link graph the `TwistedTorusND` branch targets, and its `BuildStrategy` phase order) is on [TwistedTorusND::BuildStrategy](../twist/buildstrategy.md) and the [twist overview](../twist/overview.md).
- The **degraded-axis proto ingest** — how the runtime slice-failure descriptor writes the `Target` degraded bytes this page *consumes* — is on [Degraded-Axis Ingest](degraded-axis.md).
- The SparseCore-offload `HierarchicalKind` strategy (a distinct offload path) is on [HierarchicalKind](hierarchical-kind.md).

This page owns the **`SelectNDStrategy` decision tree, the five-class strategy enum, and the `IciResource → strategy` / degraded-axis fold**.

---

## Signature and Argument Layout

The decompiled prologue (`0x137c78f4`..`0x137c790e`) and the mangled symbol fix the full signature:

```c
xla::jellyfish::StrategyND* BaseStrategyND::SelectNDStrategy(   // 0x137c78e0
    Target&                         target,        // this/BaseStrategyND
    const DeviceAssignment*         dev_assign,
    bool                            is_cross_module,
    ObjectView<TpuCompilationEnvironment> env,      // the flag-byte base
    const ProgramSharedRegistry*    psr,
    bool                            use_global_ids,  // StrategyND ctor param
    LloRegionBuilder*               lrb,
    const HloInstruction*           hlo,
    bool                            enable_sub_plane,  // GATES the top-level split
    optional<vector<net_util::RingLocation>> ring_locs,
    bool                            b11,
    bool                            b12);
```

It returns a heap-allocated `StrategyND*` (every terminal class derives from `StrategyND`, so the return type is the base pointer even for `StrategySubgroupND` / `TwistedTorusND`).

**Entry fold (`0x137c7912`..`0x137c7922`).** Before any branching, the cross-module flag is folded with the opcode test:

```c
is_cross_module &= (hlo != nullptr) ? hlo->IsCrossReplicaAllReduce() : 0;
//  decompiled: v14 = IsCrossReplicaAllReduce(hlo) & is_cross_module
```

The instruction is treated as "cross-module / special" only if it is genuinely a cross-replica all-reduce (`IsCrossReplicaAllReduce` @ `0x1e5a0020`). This folded `is_cross_module` (the decompiler's `v14`) is what every downstream branch tests, never the raw argument.

---

## The Decision Tree

Two top-level paths split on the `enable_sub_plane` argument. Each path either constructs a terminal strategy and returns, or falls through to a common tail. The ordered branches, with their guard predicate, naming VLOG string, terminal class, and `operator new` size, are below. The VLOG strings are decoded byte-exact from `.rodata` and re-confirmed in the decompiled body.

### Path A — `enable_sub_plane == true`: the sub-plane subgroup

This is the 2-D-algorithm-on-a-sub-plane-of-the-3-D-torus all-reduce. **All** of the following must hold (`0x137c7936`..`0x137c79c3`):

```text
hlo != nullptr
AND !is_cross_module                          (folded value)
AND env[0xe1f] != 0                           (xla_pf_enable_nd_allreduce)
AND hlo->opcode == kAllReduce  (0xd)
AND cast<HloAllReduceInstructionBase>(hlo)->field[0xe1] == 1   (use-global / constrain-layout)
AND ReplicaGroupsOnNDPlane(target, dev_assign, hlo->device_list(), plane=2, false).ok
AND the returned vector<MeshNDInfo> is a SINGLE group   (begin == end)
```

On success: VLOG `"Enabling ND sub-plane allreduce"` (`.rodata 0x86df4d5`, 31 B) and construct **`StrategySubgroupND`** (`operator new(0x638)` @ `0x137c79d7`; ctor `StrategySubgroupND::StrategySubgroupND` @ `0x137d4c00`) with `(lrb, target, env, psr, dev_assign, hlo, ring_locs)`. If any guard fails, fall through to the common tail.

> **NOTE —** the `ReplicaGroupsOnNDPlane(plane=2)` call here is the same plane-count primitive the cost model uses; `SelectNDStrategy` reads only its `ok` byte and the begin==end single-group test (decompiled at `0x137c79bf`). The internal `MeshNDInfo` field layout — what "plane=2" means beyond "the ND sub-plane" — is not decoded; see [SPMD Link-Count Cost](spmd-link-count-cost.md). The `xla_pf_enable_nd_allreduce` flag carries the documentation string *"Use 2-D algorithm on a sub-plane of the 3-D torus"* (`.rodata 0x850c910`).

### Path B — `enable_sub_plane == false`: the 2-D ND-plane ring

Call `IsGroupNDPlane(target, env, dev_assign, hlo, &NDtopo, is_cross_module)` (`0x137c6700`). It returns true when the slice is a 3-D torus whose collective fits a single 2-D plane:

```text
Target::NumNetworkDimensions() == 3                    (0x137c6725)
AND !multi-slice  (GetMultiSliceTopology / IsMultiSliceDeviceAssignment)
AND hlo->opcode == kAllGather (0x9)  AND  hlo->field[0xe1] use-global    (0x137c67ef..0x137c67f7)
AND the NDtopo dims are not all 1   (decompiled: v28[22]!=1 && v28[23]!=1 && (v28[24]!=1 | …))
AND env[0x1015] == 1                                    (0x137c6921; the 2-D-algorithm enable)
```

The `env[0x1015]` byte read is confirmed in the decompile as `*(a2 + 4117) == 1` (4117 = 0x1015). On `IsGroupNDPlane == true`: VLOG `"Enabling 2-D algorithm …"` (string family `0x85a31f0 / 0xa00b8b8`) and construct **`StrategyND`** (`operator new(0x5f0)`) with the ND-ring parameter set — the decompiled ctor call passes `p7 = 1` (ND-ring) and the NDtopo extent in `p8`. If `IsGroupNDPlane == false`, fall through to the common tail.

> **GOTCHA —** the sub-plane (Path A) gate keys on `kAllReduce (0xd)` while the ND-plane (Path B) gate keys on `kAllGather (0x9)`. They are not interchangeable: a reimplementer must dispatch the opcode test per path. The `IsGroupNDPlane` skip emits `"Skipping because 2D subplane wasn't detected"` (`.rodata 0xa212791`).

### Common tail — N-way / twisted / strided / default

Reached when neither Path A nor Path B fires. Evaluated in order:

**C-i — N-way model parallelism.** If the folded `is_cross_module` is set, call `UseSpecialStrategyNDNWay(target, dev_assign)` (`0x137c6be0`): true when the slice is single-slice (`GetMultiSliceTopology == 0`, `0x137c6bff`) and the device-assignment component count `[grp+0x8] ∈ {2,4}` (`0x137c6c27`). On true: VLOG `"Enable Strategy NDNway"` (`.rodata 0x84bec52`, 22 B) and construct **`StrategyND`** (0x5f0) with the N-way parameter set — the decompiled ctor pushes the `LogicalDeviceCount` (`*(int*)(*dev_assign + 8)`) and the ring-kind code `7`. This is the 2-/4-way model-parallel cross-replica all-reduce (string `0xa00b8b8`).

**C-ii — twisted torus.** Otherwise (single-module path) read the chip-part torus dims from `[Target+0x3b8]` (X = `+0x58`, Y = `+0x5c`, Z = `+0x60`) and run the twisted-torus shape probe (`0x137c7c39`..`0x137c7c7d`): take the min/mid of the three extents and test whether `2·(smaller dim) == (a dim extent)`. On a twisted shape: VLOG `"AllReduceEmitter: Choosing twisted topology"` (`.rodata 0x84b9da0`, 43 B) and construct **`TwistedTorusND`** (`operator new(0x610)` @ `0x137c7c8d`; ctor `0x137d0040`) with `(target, lrb)`. See [TwistedTorusND::BuildStrategy](../twist/buildstrategy.md) for the resulting non-rectangular ring.

**C-iii — strided ND.** If not twisted, probe `UseStridedStrategyND(target, &stride0, &stride1, dev_assign, hlo)` (`0x137c72e0`): true when single-slice (`0x137c731e`), `NumNetworkDimensions == 3` (`0x137c734d`), and `LogicalDevicesPerChip == 1` (`0x137c7361`). On true: VLOG `"Enable StridedStrategyND"` (`.rodata 0x960d0b9`, 24 B) and construct **`StrategyND`** (0x5f0) with the computed `stride0/stride1` passed as the ring-dimension parameters.

**D — default ND ring / subgroup fallback.** If none of the above fires, construct the plain default **`StrategyND`** (0x5f0, ctor `0x137c2f40`) as an ND ring. The default-fallback subgroup arm carries VLOG `"Enable StrategySubgroupND."` (`.rodata 0xa0c5fc7`, 26 B; decompiled at line 89 of the body).

### Summary table

| Order | Guard (all conditions) | VLOG / name | Class built (size, ctor) |
|---|---|---|---|
| A | `enable_sub_plane && hlo && !cross_module && env[0xe1f] && opcode==kAllReduce(0xd) && cast.field[0xe1]==1 && ReplicaGroupsOnNDPlane(plane=2).ok && single-group` | `"Enabling ND sub-plane allreduce"` | `StrategySubgroupND` (0x638, `0x137d4c00`) |
| B | `!enable_sub_plane && IsGroupNDPlane && env[0x1015]==1` | `"Enabling 2-D algorithm …"` | `StrategyND` ND-ring (0x5f0, `0x137c2f40`) |
| C-i | `cross_module && UseSpecialStrategyNDNWay` (single-slice, comp-count ∈ {2,4}) | `"Enable Strategy NDNway"` | `StrategyND` N-way (0x5f0) |
| C-ii | single-module && twisted shape (`2·a == dim`) | `"AllReduceEmitter: Choosing twisted topology"` | `TwistedTorusND` (0x610, `0x137d0040`) |
| C-iii | `UseStridedStrategyND` (single-slice, `NumNetDims==3`, `LDPC==1`) | `"Enable StridedStrategyND"` | `StrategyND` strided (0x5f0) |
| D | else | `"Enable StrategySubgroupND."` | `StrategyND` default ND ring (0x5f0) |

`env` = `ObjectView<TpuCompilationEnvironment>`; the bracketed values are byte offsets into the flag struct. Each `StrategyND` further resolves to a 1-D vs N-D ring inside `BuildStrategy` (next section).

---

## The Strategy Enum — Five Terminal Classes

| Class | `operator new` | Ctor | Role |
|---|---|---|---|
| `StrategySubgroupND` | 0x638 | `0x137d4c00` | sub-plane (2-D-of-3-D-torus) all-reduce; per-subgroup ring then over rings |
| `StrategyND` (ND-ring) | 0x5f0 | `0x137c2f40` | umbrella 1-D / N-D ring; `BuildStrategy` resolves which via `[obj+0xa8]` |
| `StrategyND` (N-way) | 0x5f0 | `0x137c2f40` | 2-/4-way model-parallel ring (ring-kind code 7) |
| `StrategyND` (strided) | 0x5f0 | `0x137c2f40` | strided ND ring (`stride0/stride1`) |
| `TwistedTorusND` | 0x610 | `0x137d0040` | twisted-torus ring over a non-rectangular ICI link graph |

Three of the five terminal "classes" are the *same* C++ type `StrategyND`, distinguished only by the ctor argument vector (ND-ring vs N-way vs strided). The object size (0x5f0) is identical; only the distinguishing parameters differ.

### The `StrategyND` constructor

`StrategyND::StrategyND` (`0x137c2f40`, 12 parameters) records the distinguishing parameters, then derives the color-dimension table and builds the ring:

```c
StrategyND(Target&, LloRegionBuilder*, ProgramSharedRegistry*, ObjectView<TpuCompEnv>,
           bool p6, bool p7, long p8, long p9, ulong p10, long p11, long p12, bool p13)
```

| Param | Stored at | Meaning |
|---|---|---|
| `p6` | `[obj+0xa8]` | the **1-D-vs-ND-ring selector** consumed by `BuildStrategy` (`==1` ⇒ `UniDirectionNDRingStrategy`, else `UniDirection1DRingStrategy`) |
| `p7` | `[obj+0x5e1]` | "use ND-ring vs 1-D-ring" flag; `1` on the ND-plane branch, varies per call site |
| `p8`, `p9` | — | the two ring dims/extents, **or** the strided `stride0/stride1`; many branches push `7` here (ring-kind / all-3-dims-usable code) |
| `p10` | — | a size/count — `LogicalDeviceCount` (`movsxd` from the dev-assign group) in the N-way / ND-plane branches |
| `p13` | `[obj+0x5a8]` | from the picker's `b12` argument |

The ctor sets the strategy-name slot `[obj+0x5b0]` (initialized referencing the `"UniDirection1DRingStrategy"` type-name string @ `0xae5cdb0+0x2e`), then calls `Target::LogicalDevicesPerChip` (`0x1d615b00`), `ComputeColorDimensions` (`0x137c3ba0` — the `[6][3]` color-dim producer), and `StrategyND::BuildStrategy` (`0x137c4660`).

> **NOTE —** `SelectNDStrategy` chooses the *class*; `BuildStrategy` (`0x137c4660`) turns the `[6][3]` color-dim table into the per-color `RingLocation` neighbour schedule, gating `UniDirection1DRingStrategy` vs `UniDirectionNDRingStrategy` on `[obj+0xa8]` (`0x137c4689`). The per-color `RingLocation` construction inside `BuildStrategy` was not fully decoded (LOW); the partner schedule it emits is documented on [Binomial / Recursive-Doubling](binomial-recursive-doubling.md).

---

## The Degraded-Axis Fault-Tolerant Remap

A torus axis can have a partially-failed ICI link at slice bring-up. The *resilient* collective path routes around it by demoting that axis out of the primary ring. Three functions implement this: a per-axis reducer, a remap, and the resilient gate.

### `GetResourceFromIciResource` — the ICI slot map (`0x1c894c00`)

The 7-instruction body maps an `IciResource` enum value onto a packed `{slot, valid}` pair. Decompiled verbatim:

```c
__int64 GetResourceFromIciResource(int ici_resource) {   // 0x1c894c00
    __int64 e = (unsigned int)(ici_resource - 1);
    if (e < 6) return e + 0x10000000DLL;   // {slot = 0xd + e, valid = 1}
    return 0;                              // {0, 0}
}
```

`IciResource e ∈ [1..6]` → packed pair `{slot = 0xc + e, valid = 1}`; otherwise `{0, 0}`. The six slots `{0xd, 0xe | 0xf, 0x10 | 0x11, 0x12}` are the **3 torus dimensions × 2 ring directions** of the cost model's `ResourceVector` (see [SPMD Link-Count Cost](spmd-link-count-cost.md)). The high dword (`valid = 1`, from the constant `0x10000000D`) is the per-resource "this slot is a real ICI link" marker used when accumulating link counts / depositing cycles.

### `Target::Is{X,Y,Z}Degraded` — the per-axis failed-link flags

Three adjacent bytes in the `Target` struct, each a boolean "this dimension has a partial/failed ICI link, route around it":

| Accessor | Address | Source byte |
|---|---|---|
| `Target::IsXDegraded()` | `0x1d615940` | `Target[+0x3f8]` |
| `Target::IsYDegraded()` | `0x1d615960` | `Target[+0x3f9]` |
| `Target::IsZDegraded()` | `0x1d615980` | `Target[+0x3fa]` |

These are populated from the slice topology descriptor / `TpuDegradedAxesProto` — see [Degraded-Axis Ingest](degraded-axis.md) for the proto→Target ingest.

### `GetDegradedAxis` — reduce per-axis flags to one axis or `-1` (`0x1c894c20`)

Inputs: `Target* target`, `bitset<3> usable_mask` (bit0=X, bit1=Y, bit2=Z usable). It reads the topology extents from `[Target+0x3b8]` (`*(this+119)` in the decompile; Xext = `+0x58`, Yext = `+0x5c`, Zext = `+0x60`) and applies, per axis, the predicate:

```text
dX = IsXDegraded() && (Xext >= 2) && usable_mask.bit0
dY = IsYDegraded() && (Yext >= 2) && usable_mask.bit1
dZ = IsZDegraded() && (Zext >= 2) && usable_mask.bit2
num_degraded_axes = dX + dY + dZ        (decompiled accumulator v12)
degraded_axis     = index of the single degraded axis (0=X, 1=Y, 2=Z), else 0
return  (num_degraded_axes >= 2) ? -1 : degraded_axis;
```

The decompiled tail (`0x1c894cd2`) is exactly `if ((unsigned)v12 >= 2) return -1; return v9;`. The function VLOGs `"num_degraded_axes = {n}, degraded_axis = {a}"` (`.rodata 0xa219d1f + 0xa219bb2`; source `group_utils.cc:1728`).

> **NOTE —** the resilient algorithm can route around **at most one** degraded axis. If two or three axes are degraded it returns `-1` and the resilient path is *not* taken. The X-axis arm is implemented branchlessly (`v6 = a2 & 2; v7 = v6 - 1; v8 = v6 >> 1`, then `cmov`-style selection); the per-axis semantic above is what the three arms compute. The exact SSA of the X arm's index write is an impl detail (LOW), the predicate is HIGH.

### `UseResilientAlgorithmBase` — when the degraded path is taken (`0x1c894da0`)

The gate (ALL must hold), decompiled at `0x1c894dd2`..`0x1c894e3b`:

```text
env[0x1116] == 1                          (xla_tpu_use_resilient_collective_emitter; *(a2+4374)==1)
AND a usable-axis-mask test               (also accepts a bool override arg)
AND Target::NumNetworkDimensions() == 3   (3-D torus only)
AND a symmetric-torus dimension comparison (X==Y, and Y==Z or 2·-related)
AND GetDegradedAxis(target, mask) != -1
```

The `env[0x1116]` byte is confirmed as `*(a2 + 4374) == 1` (4374 = 0x1116). When this returns true, `ComputeColorDimensions` takes the degraded color-dim init path below. The twisted variant `UseResilientAlgorithmTwistedTorus` (`0x1c894fc0`) reuses the same `env[0x1116]` gate + `GetDegradedAxis ≠ -1` test for the twisted-torus resilient path, tying it to the C-ii picker branch.

### `InitColorDimensionsDegraded` — the `[6][3]` ring-dim remap (`0x137c6580`)

Signature `(Target&, long num_colors, long(*color_dims)[6][3], const bitset<3>& mask)`. Decompiled steps:

1. `degraded_axis = GetDegradedAxis(target, mask)` (`0x137c659a`).
2. Derive the two healthy axes (decompiled `v5`/`v6`):
   ```c
   healthy_a (v5) = (degraded_axis == 0) ? 1 : 0;     // Y if X bad, else X
   healthy_b (v6) = (healthy_a ^ 3) - degraded_axis;
   ```
   giving, per degraded axis:

   | `degraded_axis` | healthy_a | healthy_b | inner (col [2]) |
   |---|---|---|---|
   | 0 (X bad) | 1 (Y) | 2 (Z) | 0 (X) |
   | 1 (Y bad) | 0 (X) | 2 (Z) | 1 (Y) |
   | 2 (Z bad) | 0 (X) | 1 (Y) | 2 (Z) |

3. Fill `color_dims[c][0..2]` for `c = 0 .. min(num_colors, 6) - 1` (the decompile unrolls all six rows with `a2 != 1..6` guards; the final `ud1` is the `>6` unreachable trap):
   ```text
   even rows c:  [ healthy_a, healthy_b, degraded ]   = [v5, v6, result]
   odd  rows c:  [ healthy_b, healthy_a, degraded ]   = [v6, v5, result]
   ```

Every color row pins the **degraded axis to the inner (last, column [2]) ring dimension** and alternates the two healthy axes across the outer two columns per color, so consecutive colors traverse the healthy axes in opposite order (balancing the two healthy SerDes directions). VLOG `"Using resilient collective, degraded_axis = {a}, color count = {n}"` (`.rodata 0xa219b98 + 0xa218cee`; source `all_reduce_strategies.cc:571`).

### How the remap changes the ICI slot deposits

The `[6][3]` `color_dims` table is the same structure `ComputeColorDimensions` feeds the ring builder *and* that drives the per-torus-dimension ICI `ResourceVector` deposits (slots `0xd..0x12` via `GetResourceFromIciResource`):

- **Normal** (no degraded axis): each color's outer dims are the active torus axes; the cost model deposits cycles into the 2 slots of each active dim and the `num_dims` divisor = `popcnt(active axes)`.
- **Degraded** (one axis bad): the bad axis is in the inner column for every color, so the outer (primary) ring dims that carry the reduce-scatter / all-gather traffic are the 2 **healthy** axes only. The degraded axis's two ICI slots are not used as primary ring dims — the effective per-collective dimension count drops `3 → 2`, which (a) reduces the cost-model `num_dims` divisor toward the 2-axis case and (b) keeps the failed axis's link out of the ring so the collective completes over the surviving torus links.

> **NOTE —** the net effect is to convert a 3-D-torus collective into a 2-healthy-axis ND ring with the dead axis demoted to the innermost dimension, costed via the same per-axis formula but with the degraded axis excluded from the primary dim set. The numeric cost formula and the `num_dims` divisor are detailed on [SPMD Link-Count Cost](spmd-link-count-cost.md).

---

## TpuCompilationEnvironment Flag Bytes

The picker reads four flag bytes from the `ObjectView<TpuCompilationEnvironment>`. The byte offsets are byte-exact reads in the binary; the flag *names* are pinned via adjacent VLOG / documentation strings and helper semantics, not via a numeric proto-field-id decode.

| Offset | Flag (inferred) | Read by | Effect |
|---|---|---|---|
| `0xe1f` | `xla_pf_enable_nd_allreduce` | Path A gate | enable the ND sub-plane all-reduce (doc *"Use 2-D algorithm on a sub-plane of the 3-D torus"*) |
| `0x1015` | the 2-D-algorithm / ND-plane enable | `IsGroupNDPlane`, Path B | enable the 2-D-plane ND ring |
| `0x1116` | `xla_tpu_use_resilient_collective_emitter` | `UseResilientAlgorithmBase` | enable the degraded-axis resilient path |
| `0xf45` | the multi-color / resilient color-count enable | `GetColorCount` (`0x137c6260`) | gate the multi-color decomposition before the resilient path |

> **GOTCHA —** the offset↔name mapping for `0x1015` carries the one residual uncertainty: it is read as `*(env+4117)==1` (HIGH on the offset and its gating role), but whether it is `xla_tpu_use_strided_strategy_nd` (`.rodata 0x86f5a4b`) or a distinct 2-D-algorithm flag was not pinned via the proto field-descriptor table (`TpuCompEnvReflection::GetFieldValue` @ `0x1d7523a0`, `_table_` @ `0x21cfa9e0`). Treat the *name* as MEDIUM; the byte read and its branch effect are HIGH.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `BaseStrategyND::SelectNDStrategy` | `0x137c78e0` | the picker (2016 B) | HIGH (decompile-verified) |
| `IsCrossReplicaAllReduce` | `0x1e5a0020` | entry fold | HIGH |
| `ReplicaGroupsOnNDPlane` | `0x1c890960` | Path A single-ND-plane test (plane=2) | HIGH |
| `BaseStrategyND::IsGroupNDPlane` | `0x137c6700` | Path B 2-D-plane gate | HIGH (env`[0x1015]` read confirmed) |
| `BaseStrategyND::UseSpecialStrategyNDNWay` | `0x137c6be0` | C-i N-way gate | HIGH |
| `BaseStrategyND::UseStridedStrategyND` | `0x137c72e0` | C-iii strided gate | HIGH |
| `StrategyND::StrategyND` | `0x137c2f40` | ND-ring ctor (12 params) | HIGH |
| `StrategyND::BuildStrategy` | `0x137c4660` | `[obj+0xa8]` 1-D-vs-ND ring | HIGH (gate); per-color `RingLocation` LOW |
| `BaseStrategyND::ComputeColorDimensions` | `0x137c3ba0` | `[6][3]` color-dim producer | HIGH |
| `BaseStrategyND::GetColorCount` | `0x137c6260` | env`[0xf45]` color count | HIGH |
| `StrategySubgroupND::StrategySubgroupND` | `0x137d4c00` | sub-plane ctor | HIGH |
| `TwistedTorusND::TwistedTorusND` | `0x137d0040` | twisted-torus ctor | HIGH |
| `GetResourceFromIciResource` | `0x1c894c00` | ICI slot map | HIGH (byte-exact) |
| `GetDegradedAxis` | `0x1c894c20` | per-axis reducer (384 B) | HIGH (byte-exact) |
| `Target::IsXDegraded / IsYDegraded / IsZDegraded` | `0x1d615940 / …960 / …980` | per-axis flag bytes `Target+0x3f8..3fa` | HIGH |
| `UseResilientAlgorithmBase` | `0x1c894da0` | resilient gate | HIGH (env`[0x1116]` confirmed) |
| `UseResilientAlgorithmTwistedTorus` | `0x1c894fc0` | twisted resilient gate | HIGH |
| `BaseStrategyND::InitColorDimensionsDegraded` | `0x137c6580` | `[6][3]` degraded remap | HIGH (byte-exact) |

---

## What Was Not Resolved

- **The `0x1015` flag name.** Byte offset and branch effect are HIGH; the canonical XLA flag string was not pinned via the proto field-descriptor table. MEDIUM.
- **The twisted-torus shape predicate.** The C-ii probe (`0x137c7c39`..`0x137c7c7d`) was reduced to "`2·(smaller dim) == (a dim extent)`"; the full closed-form twist inequality generalizing the `Nx_Ny_Nz_twisted` topology labels was not derived. See [twist overview](../twist/overview.md). MEDIUM.
- **`StrategyND::BuildStrategy` per-color `RingLocation` construction.** The `[obj+0xa8]` 1-D-vs-ND gate is confirmed; the per-dim memmove fan-out producing each color's `RingLocation` set was not transcribed. See [Binomial / Recursive-Doubling](binomial-recursive-doubling.md). LOW.
- **`ReplicaGroupsOnNDPlane(plane=2)` internals.** `SelectNDStrategy` reads only `ok` + the begin==end single-group test; the `MeshNDInfo` field layout and the plane-count semantics are open. LOW.
- **The exact bit-fold in the `GetDegradedAxis` X arm.** The per-axis degraded *semantic* is proven; the literal SSA of the branchless X-axis index write is annotated as impl detail. LOW.

---

## Cross-References

- [Collectives Overview](overview.md) — where the picker sits in the on-pod collective lowering
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — the per-rank partner schedule a `StrategyND` ring runs (the emitter loop, owned there)
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — the cost / `num_dims` divisor model and the `ResourceVector` slot deposits the `IciResource` map feeds
- [Degraded-Axis Ingest](degraded-axis.md) — the `TpuDegradedAxesProto` → `Target[+0x3f8..3fa]` ingest that produces the degraded flags this page consumes
- [TwistedTorusND::BuildStrategy](../twist/buildstrategy.md) and the [twist overview](../twist/overview.md) — the twisted-torus topology the C-ii branch targets
- [HierarchicalKind](hierarchical-kind.md) — the SparseCore-offload collective strategy (a distinct path)
- [ICI Overview](../ici/overview.md) — the physical ICI torus and link model
