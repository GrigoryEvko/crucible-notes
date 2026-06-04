# Tensor-Split / ND-Plane

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset `0x84a0000`). Other versions differ.*

## Abstract

When an embedding-class collective is offloaded to the SparseCore (the SC-offload substrate of **[On-Pod Collectives](overview.md)** §1.2), the substrate must answer two geometric questions before it can build a ring schedule: **how many SparseCores participate and how the tensor is cut across them**, and **which torus axes the collective's replica-groups actually project onto**. This page owns those two derivations. The first is the *device partition*: `NumScOffloadDevices` (the participating-SC count), the `tensor_split_factor` (an in-collective tensor cut), and the per-axis ring device split inside `GetDimensionRings`. The second is the *ND-plane geometry*: `ExtractNDPlaneInfo`, the `NDPlaneInfo` / `NDPlaneStrideInfo` struct it returns, and the `IsNDPlaneSpanAcrossEntireDimension` projection that validates each axis stride.

The ND-plane is the SparseCore analog of the dense TensorCore `ReplicaGroupsOnNDPlane` decomposition (**[SelectNDStrategy](strategy-nd-picker.md)** / **[Overview §1.3](overview.md#13-shared-substrate-the-physical-torus-mesh-decomposition)**). Where the dense path memoizes a `vector<MeshNDInfo>` and reports a mesh-dimension count through `ReplicaGroupsOnNDPlane(plane=2)`, the SC path computes an `NDPlaneInfo` descriptor whose embedded `NDPlaneStrideInfo` carries a per-axis `optional<int32>` span stride, and reports the dimension count as `popcount` of the three has-bits — `GetCollectiveNDPlaneDimensionCount`. Both count the torus axes the replica-groups span; they differ only in representation.

The two derivations meet downstream. The `NumScOffloadDevices` total is partitioned multiplicatively across the X/Y/Z ring axes by `GetDimensionRings`; the `NDPlaneInfo` dimension count gates the builder's twist / ND-plane-count branches (the `cmp $3` 3-axis twist gate). Scope boundaries: the *physical-core placement* of the chosen logical colors lives on **[Physical-Core Placement](physical-core-placement.md)**, and the *strategy choice* (sub-plane vs ND-ring vs twisted-torus) on **[SelectNDStrategy](strategy-nd-picker.md)**. This page owns the split-factor partition, the `NDPlaneInfo` struct, and the per-axis projection only.

For reimplementation, the contract is:

- The **participating-SC count** `NumScOffloadDevices = (TpuTopology SC-AvailableCoreCount / LogicalDevicesPerChip(SparseCore)) − num_embedding_devices`, the offload complement of the reserved embedding partition.
- The **`tensor_split_factor`** `optional<int>` gate: a factor `>1` requires more than one SparseCore (`!use_single_core`), and the only supported non-trivial factor is `2`, which enables split-tensor mode (color duplication).
- The **per-axis ring device split** `segments = extent / devcount − 1`, with `devcount` carried as a running remainder across axes — a multiplicative ND partition of the offload-device total.
- The **`NDPlaneInfo` / `NDPlaneStrideInfo` byte layout**, and the **`IsNDPlaneSpanAcrossEntireDimension` projection** — the per-axis stride must evenly divide the torus dimension extent for the plane to be valid.

| | |
|---|---|
| **Participating-SC count** | `NumScOffloadDevices` `@0x1d6b8b00` → `long` (flag_utils.cc) |
| **Embedding-reserve sibling** | `NumEmbeddingDevices` `@0x1d6b8a00` → `long` (complement partition) |
| **Tensor-split gate** | `ConstructConfigForCollectiveUniDirNDGroups<*>` `@0x133c82c0` / `0x133c2dc0` / `0x133cd800` (trailing `optional<int>`) |
| **Per-axis ring split** | `GetDimensionRings` `@0x133df520` → `vector<RingConfigAttributes>` (`segments = extent/devcount − 1`) |
| **ND-plane extractor** | `ExtractNDPlaneInfo` `@0x133bb940` → `StatusOr<NDPlaneInfo>` (offload_collective_config.cc) |
| **Per-axis projection** | `ExtractNDPlaneInfo::$_0` `@0x133bf700` (the `IsNDPlaneSpanAcrossEntireDimension` RetCheck triple) |
| **Dimension count** | `GetCollectiveNDPlaneDimensionCount` `@0x133bb6e0` = `popcount(has_x + has_y + has_z)` |
| **`NDPlaneInfo` size** | ~`0x28` (3 × `int32` + `NDPlaneStrideInfo` at `+0xc`) |
| **`NDPlaneStrideInfo` size** | `0x1c` (3 × `optional<int32>` + `bool across_cores_on_chip`) |
| **Torus extents** | `Target[+0x3b8][+0x58]` (X) / `+0x5c` (Y) / `+0x60` (Z) — shared with dense picker + cost model |

---

## 1. The SC offload device partition

The device partition answers "how many SparseCores does the offloaded collective run on, and how is the tensor cut across them" in three independent pieces: the participating-SC count (`NumScOffloadDevices`), the in-collective tensor cut (`tensor_split_factor`), and the per-axis ring device split (`GetDimensionRings`). All three are byte-traced.

### 1.1 `NumScOffloadDevices` — the participating-SC count

`NumScOffloadDevices(ObjectView<TpuCompEnv>, const TpuTopology&) → long` (`@0x1d6b8b00`, source `flag_utils.cc`) computes the count of SparseCore *logical devices* available for collective offload. It is the **complement** of the embedding reservation: the topology's total SparseCores divided by SparseCores-per-chip gives the per-device SC count; subtracting the reserved `num_embedding_devices` leaves the offload-available count.

```c
function NumScOffloadDevices(compEnv, topo):           // sub_1D6B8B00
    sc_total = topo[+0x94]                              // SC AvailableCoreCount (core type 2)
    ldpc_sc  = TpuTopology::LogicalDevicesPerChip(topo, 2)   // sub_20AD3020, SparseCore
    if ldpc_sc > 0:
        sc_dev = sc_total / ldpc_sc                     // signed idiv
    else:
        sc_dev = 0
    auto = AutoOr<long>::FromProtoOrDie(compEnv[+0x898])  // sub_1092F7E0 ("num_embedding_devices")
    n_emb = auto.engaged ? auto.value : 0               // dl bit0 = engaged
    CHECK(n_emb >= 0)                                   // "num_embedding_devices >= 0", line 1803 — FATAL
    CHECK(sc_dev >= n_emb)                               // "num_embedding_devices <= sc_per_device", line 1805
    return sc_dev - n_emb                               // = NumScOffloadDevices
```

The `TpuTopology[+0x94]` field is the SparseCore (core type 2) `AvailableCoreCount`. It is the SC slot of the per-core-type triple `TpuTopology[+0x7c + coretype·12]` (TC at `+0x7c`, an intermediate type at `+0x88`, SC at `+0x94`) that `Target::CoresPerChip` (`@0x1d615b40`) reads; it is confirmed as the SC core count independently by `TpuTopology_MaybeAvailableSparseCoresPerLogicalDevice` (`@0xf6a1ea0`), which for core type 2 calls `NumEmbeddingDevices` (`@0x1d6b8a00`).

> **NOTE —** the FATAL `CHECK` source lines read decimal in the decompile — `1803` and `1805` — which are `0x70b` and `0x70d`. The two CHECKs bracket the result into a valid range: `num_embedding_devices` must be non-negative, and it must not exceed `sc_dev` (so the offload complement cannot go negative). On either violation the process aborts with the diagnostic `"Invalid number of embedding devices specified"` (`.rodata @0x871a7c3`).

The sibling `NumEmbeddingDevices` (`@0x1d6b8a00`) shares the same `sc_dev` computation but returns the *reserved* count: `n_emb = engaged ? value : sc_dev` (defaulting to all of `sc_dev` when the flag is unset), bounded `0 <= n_emb <= sc_dev` (CHECK lines `0x6f9` / `0x6fb`). The two functions partition `sc_dev` exactly: `num_embedding_devices` (reserved) + `NumScOffloadDevices` (offload) = `sc_dev`.

> **GOTCHA —** `NumScOffloadDevices` is a count of SparseCore **logical devices** (`sc_total / ldpc_sc`), not raw cores. A reimplementation that subtracts `num_embedding_devices` from the raw core count `TpuTopology[+0x94]` will over-count on a megacore SparseCore (`ldpc_sc == 2`). The division by `LogicalDevicesPerChip(SparseCore)` must precede the subtraction.

### 1.2 `tensor_split_factor` — the in-collective tensor cut

The templated builder `ConstructConfigForCollectiveUniDirNDGroups<*>` takes a trailing `optional<int>` `tensor_split` as its last stack argument (`[rbp+0x10]`, copied to `[rbp-0x108]` / `[rbp-0xf8]`). Source `offload_collective_config.cc`. The gate is small and entirely error-driven:

```c
function ConstructConfigForCollectiveUniDirNDGroups(..., optional<int> tensor_split):
    split = tensor_split.has_value() ? tensor_split.value : 1   // cmovne, kDefaultTensorSplitFactor = 1
    if split >= 2:                                              // cmp eax, 2 ; jl skip
        RetCheck( !use_single_core.value_or(kDefaultUseSingleCore) )  // line 0x650
            // fail → "A larger than 1 tensor split factor requires more than one
            //         sparse core to split the tensor on."
        RetCheck( split == 2 )                                  // "tensor_split_factor.value_or(...) == 2", line 1558
            // fail → "We currently only support tensor split factor of 2 across two sparse cores."
        set tensor_split_mode = 2                               // [rbp-0x234] = 2 ; flag bit `or [..], 0x20`
        VLOG("Adopting split tensor mode.")                     // line 0x65c
        // mode-2 effect: "Twisted torus: duplicate colors as indicated by tensor split factor."
```

The factor lands in the `OffloadConfig` proto field 5 `tensor_split_factor` (`[variant+0x1c]`, see **[SC-Offload Config Builder](sc-offload-config-builder.md)**) via the GenerationOption feed. The optional is forwarded **unchanged** from the public ND wrappers:

| Wrapper | Address | Forwards `tensor_split`? |
|---|---|---|
| `ConstructConfigForAllReduceUniDirND` | `@0x133c2c80` | Yes — `push [rbp+0x10]` `@0x133c2d01` |
| `ConstructConfigForReduceScatterUniDirND` | `@0x133ccbe0` | Yes — `optional<int>` in signature |
| `ConstructConfigForAllGatherUniDirND` | `@0x133c76c0` | **No** — no `optional<int>` parameter; receives the default empty optional |

> **QUIRK —** AllGather cannot tensor-split in this build. Its public wrapper `ConstructConfigForAllGatherUniDirND` (`@0x133c76c0`) has no `optional<int>` parameter, so its templated builder instantiation always sees the default-1 factor and the `split >= 2` gate never fires. Only AllReduce and ReduceScatter expose the knob. A reimplementation that plumbs `tensor_split` into the AllGather path is wiring a knob the binary leaves dead.

> **GOTCHA —** the only supported non-trivial factor is exactly `2`. The second RetCheck (`"tensor_split_factor.value_or(kDefaultTensorSplitFactor) == 2"`, source line 1558) rejects 3, 4, … with `"We currently only support tensor split factor of 2 across two sparse cores."` The two-core split is the SC analog of the dense TensorCore megacore data-split; mode-2's effect is to **duplicate colors** ("Twisted torus: duplicate colors as indicated by tensor split factor."), splitting the tensor across the two cores rather than halving each ring's volume. The per-color emission keyed by this mode is owned by **[Physical-Core Placement §5](physical-core-placement.md)** (`TensorSplitPerCoreClassifier`).

### 1.3 `GetDimensionRings` — the per-axis ring device split

`GetDimensionRings(const Target&, IciStrategyRingDim, int devcount, bool, bool megacore_aware) → vector<RingConfigAttributes>` (`@0x133df520`) is the partitioner that consumes the running device count and produces a per-axis ring decomposition. The `IciStrategyRingDim` (1..7) selects which torus axis's extent to read; the `devcount` is how many devices remain to be partitioned along this and later axes.

```c
function GetDimensionRings(target, ring_dim, devcount, b, megacore_aware):  // sub_133DF520
    validate ring_dim in 1..7                              // lea -7 ; cmp 0xfffffff9 ; jump table @0xae2eaac
    extent = target[+0x3b8][ X=0x58 | Y=0x5c | Z=0x60 ]    // chip torus dim extent
    set mesh/torus flag [-0x48] per switch arm
    ldpc_tc  = Target::LogicalDevicesPerChip(target, 0)    // sub_1D615B00 (TensorCore)
    megacore = (ldpc_tc >= 2) && megacore_aware            // setge AND'd with arg
    // THE SPLIT — how many ring segments this axis carries:
    segments = extent / devcount - 1                       // idiv ebx=devcount @0x133df670 ; dec eax @0x133df672 ; mov [-0xb8] @0x133df674
    ...                                                    // build RingConfigAttributes from segments + flags
```

The flat-path caller (the AllGather builder, `@0x133c8d3e`..`@0x133c8d5c`) passes the **running remaining device count** in `rcx = [rbp-0x150]`, initialized from the per-axis deque tuple's `hi` field. The quotient `extent / devcount` becomes the next axis's `devcount`, so the offload devices are partitioned **multiplicatively** across the X/Y/Z ring axes — each axis consumes `devcount` devices, and the remainder flows to the next. The cumulative products accumulate in a `std::__tree` (set) rooted at `[rbp-0x228]` — the device-offset / color-index map. `NumScOffloadDevices` is fetched in `rbx` immediately before this tree is initialized (`@0x133c89d3`..`@0x133c89e3`, via `GetTpuCompEnv` `@0x1d73de80`), bounding the total devices the per-axis rings may consume.

> **NOTE —** the `−1` in `segments = extent / devcount − 1` is the off-by-one that converts a count of devices-per-ring into a count of *inter-device hops* (a ring of N devices has N−1 forward steps before wrap). It is byte-confirmed at `@0x133df670`–`@0x133df674` (`idiv %ebx` then `dec %eax` then `mov %eax,-0xb8(%rbp)`; decompile `(int)v12 / v10 - 1`), where `v12` holds the axis extent and `v10` the device count.

---

## 2. `ExtractNDPlaneInfo` — the ND-plane geometry

`ExtractNDPlaneInfo` projects a collective's replica-groups onto the physical torus and reports, per axis, the *span stride* — the spacing between the chip coordinates the group touches along that axis. This is the SparseCore counterpart to the dense `ReplicaGroupsOnNDPlane` decomposition; it produces an `NDPlaneInfo` descriptor instead of a `vector<MeshNDInfo>`.

### 2.1 The algorithm

```c
function ExtractNDPlaneInfo(target, device_assignment, hlo, Span<vector<int>> groups)  // sub_133BB940
        -> StatusOr<NDPlaneInfo>:                          // sret in rdi
    // (a) read chip torus extents (same offsets as the dense picker + cost model)
    X = target[+0x3b8][+0x58]; Y = target[+0x3b8][+0x5c]; Z = target[+0x3b8][+0x60]
    // (b) collect the distinct chip coordinate per axis
    for each member core_id in each replica group:
        loc  = FromGlobalCoreId(target, core_id)           // sub_133B7BC0 → TC core location
        coord = loc / LogicalDevicesPerChip                // idiv → chip-relative coordinate
        binary-search-insert coord into per-axis sorted-unique list   // memmove @0x133bbb30
    // (c) per axis (X, then Y, then Z) run the span lambda
    for axis in {X, Y, Z}:
        stride_slot = $_0(extent_axis, coord_set_axis, axis_name)      // sub_133BF700
    // (d) assemble the sret NDPlaneInfo: 3 ints + NDPlaneStrideInfo at +0xc
    return NDPlaneInfo{ size_x, size_y, size_z, NDPlaneStrideInfo{...} }
```

The axis-name strings (`"X"` `@0x8a106a1`, `"Y"` `@0x8a0f71b`, `"Z"` `@0x886531a`) are passed to the `$_0` lambda purely for the diagnostic messages on a failed span check. The reduction-mod-`LogicalDevicesPerChip` collapses each global core ID to its chip coordinate, so two cores on the same chip (megacore) map to the same coordinate and contribute one entry to the sorted-unique set.

### 2.2 `ExtractNDPlaneInfo::$_0` — the per-axis projection (`IsNDPlaneSpanAcrossEntireDimension`)

The `$_0` lambda (`@0x133bf700`) computes one axis's span stride and validates that the plane spans the *entire* torus dimension. It writes into a `NDPlaneStrideInfo` slot: an `int32` size at `+8` and a `bool has_size` at `+0xc` (lambda-`this`-relative), plus the `StatusOr` ok flag at `+0`.

```c
function ExtractNDPlaneInfo::$_0(this, dim_size, coords, axis_name):   // sub_133BF700
    if coords.size() == 1:                                 // single coordinate ⇒ degenerate
        this[+8] = 0          // has_size = 0  (no span on this axis)
        this[+0xc] = 0
        this[+0]  = 1         // ok
        return
    stride = coords[1] - coords[0]                         // [-0x48]
    RetCheck( stride >= 1 )            // line 922; "Stride must be larger or equal to 1."
    RetCheck( stride < dim_size )      // line 923; "Stride must be less than the dimension size."
    RetCheck( dim_size % stride == 0 ) // line 925; "Stride must divide the dimension size."  ← idiv @0x133bf7b4
    // consistency: every adjacent pair must share the stride
    for k in 2 .. coords.size()-1:
        if coords[k] - coords[k-1] != stride:
            return FailedPrecondition(
                "All devices within a group must have the same stride along the "
                "dimension %s. Expected stride: %d but got %d.", axis_name, stride, observed)  // line 931
    this[+8]  = stride       // size = stride
    this[+0xc] = 1           // has_size = 1
    this[+0]  = 1            // ok
```

The three RetChecks are the **`IsNDPlaneSpanAcrossEntireDimension`** gate. The crucial one is the third: `dim_size % stride == 0`. A plane is a valid ND-plane only if its per-axis stride evenly divides the torus dimension extent — i.e. the strided coordinate set wraps cleanly around the entire torus axis. A stride that does not divide the extent would leave a partial ring, which the SC offload substrate does not emit.

> **GOTCHA —** the lambda does **not** merely read `coords[1] - coords[0]`; it then *verifies* that every adjacent pair in the sorted coordinate set has the identical stride (source line 931, error `"All devices within a group must have the same stride along the dimension %s."`). A group whose members are non-uniformly spaced along an axis — a valid HLO replica-group but not a clean torus plane — is rejected here, not silently approximated by the first pair. The raw single-pair reading is necessary but not sufficient; the full per-pair scan is the real contract.

> **NOTE —** the source lines confirm byte-exact against the raw findings: `stride >= 1` at `922` (`0x39a`), `stride < dim_size` at `923` (`0x39b`), `dim_size % stride == 0` at `925` (`0x39d`). The user-facing string for the second check is `"Stride must be less than the dimension size."` and for the third `"Stride must divide the dimension size."`

### 2.3 `NDPlaneInfo` / `NDPlaneStrideInfo` struct layout

The two structs are pinned by their `ToString` methods (`NDPlaneInfo::ToString` `@0x10fdf2a0`, `NDPlaneStrideInfo::ToString` `@0x10fe62e0`) and independently cross-checked by the field reads in the two consumers, `GetCollectiveNDPlaneDimensionCount` (`@0x133bb6e0`) and `GetMinorToMajorOrder` (`@0x133c1c40`).

| Struct | Offset | Type | Field | Confidence |
|---|---|---|---|---|
| `NDPlaneInfo` | `+0x00` | `int32` | `size_x` (plane span, X axis) | HIGH |
| | `+0x04` | `int32` | `size_y` | HIGH |
| | `+0x08` | `int32` | `size_z` (also the iteration/plane bound `GetMinorToMajorOrder` reads at `+0x8`) | HIGH |
| | `+0x0c` | `NDPlaneStrideInfo` | embedded stride descriptor | CERTAIN |
| `NDPlaneStrideInfo` | `+0x00` | `int32` | `stride_x` (`optional<int32>` value) | CERTAIN |
| (at `NDPlaneInfo+0xc`) | `+0x04` | `bool` | `has_stride_x` | CERTAIN |
| | `+0x08` | `int32` | `stride_y` | CERTAIN |
| | `+0x0c` | `bool` | `has_stride_y` | CERTAIN |
| | `+0x10` | `int32` | `stride_z` | CERTAIN |
| | `+0x14` | `bool` | `has_stride_z` | CERTAIN |
| | `+0x18` | `bool` | `across_cores_on_chip` (megacore / both-cores flag) | CERTAIN |

`sizeof(NDPlaneStrideInfo)` is `0x1c`; `sizeof(NDPlaneInfo)` is `~0x28` (three ints + the embedded `0x1c`-byte stride info, rounded up). Relative to the parent `NDPlaneInfo`, the embedded stride fields sit at: `stride_x`@`+0xc` / `has_x`@`+0x10`, `stride_y`@`+0x14` / `has_y`@`+0x18`, `stride_z`@`+0x1c` / `has_z`@`+0x20`, `across_cores`@`+0x24`.

> **CORRECTION (P-3-379) —** an earlier finding read the `NDPlaneInfo::ToString` labels as `"NDPlaneInfo: "` / `" max: "` / `"Delay: "` and left the three top-level `int32`s semantically un-named (inferred from those labels). The decompile of `NDPlaneInfo::ToString` (`@0x10fdf2a0`) shows the labels are byte-exactly `"size_x: "`, `"size_y: "`, `"size_z: "`, with a trailing `"stride_info: "` for the embedded `NDPlaneStrideInfo`. The three top-level ints are the **plane span sizes** `(size_x, size_y, size_z)`; the embedded `NDPlaneStrideInfo` carries the per-axis **strides** (`NDPlaneStrideInfo::ToString` labels them `"stride_x: "`, `"stride_y: "`, `"stride_z: "`). The `" max: "` / `"Delay: "` reading is disproven.

> **QUIRK —** the size/stride pairing is the counter-intuitive part. The top-level `NDPlaneInfo` ints are the plane *spans* (`size_*`), while the *strides* the `$_0` lambda computes (`coords[1] - coords[0]`) are stored in the embedded `NDPlaneStrideInfo` as `stride_*`. A reimplementer who conflates the two — writing the stride into the top-level int — will mis-feed `GetMinorToMajorOrder`, which reads the embedded `stride_*`/`has_*` (`@0x133c1c40`: `has_x`@`+0x10`, `size_x`@`+0xc` relative to the `NDPlaneInfo`), not the top-level `size_*`.

### 2.4 The ND-plane dimension count

`GetCollectiveNDPlaneDimensionCount` (`@0x133bb6e0`) is the SC analog of the dense `ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims`. It calls `ExtractNDPlaneInfo` and reduces the result to a single integer: the number of torus axes the replica-groups span.

```c
function GetCollectiveNDPlaneDimensionCount(target, device_assignment, collective):  // sub_133BB6E0
    CHECK(collective != nullptr)                           // line 845
    groups = GetPhysicalDeviceGroups(collective, device_assignment)
    info   = ExtractNDPlaneInfo(target, device_assignment, collective, groups)
    if !info.ok: return info.status                        // AddSourceLocation line 852 / 849
    return info.has_stride_x + info.has_stride_y + info.has_stride_z   // sum of the 3 has-bytes
```

The dimension count is literally the sum of the three `has_*` bytes — byte-confirmed at `@0x133bb772`..`@0x133bb786` (decompile line `*((_DWORD *)this + 2) = v26 + v27 + v28;`, where `v26/v27/v28` are the three has-bits read out of the local `NDPlaneInfo` at `+0x18`/`+0x20`/`+0x28`). Because each `has_*` is `0` or `1`, the sum is the `popcount` over the three axes. This count is what the builder's `cmp $3` twist gate (3 dims → `k_2k_2k` / `k_k_2k` twist) and the AllGather/ReduceScatter ND-plane-count gate (1..4 dims) test.

> **NOTE —** the dense and SC paths compute the same quantity by different routes. The dense `GetCommunicationMultiplier` (`@0x127a16c0`, **[Overview §3](overview.md#3-the-collective-op-family)**) returns `ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1` as a link-count divisor; the SC `GetCollectiveNDPlaneDimensionCount` returns the bare `popcount` without the `+1`. The `+1` is a cost-model convention on the dense side, not a difference in the underlying axis count — both count the torus axes the replica-groups span.

### 2.5 The dense `ReplicaGroupsOnNDPlane` projection (`MeshNDInfo` builder)

The dense (TensorCore) counterpart of §2.1's SC `ExtractNDPlaneInfo` is `ReplicaGroupsOnNDPlane` (`@0x1c890960`). It is the builder the [AllGather ND-ring](allgather-nd-ring.md) and ReduceScatter selectors call to both *decide* dimensionality and *build* the per-axis `vector<MeshNDInfo>`. The entry function itself contains **no** coordinate math: it (a) renders the device-assignment to a string and serializes the topology (`TpuTopologySerdes::Distill` → `TpuTopologyArgs::ToProto` → `tsl::SerializeToStringDeterministic`), (b) takes `nd_plane_cache_mutex` and looks the composite key up in the `NDPlaneCacheKey → optional<vector<MeshNDInfo>>` cache (`@0x225799b8`, guarded singleton `GetNDPlaneCache`), and (c) on a miss invokes the per-axis lambda `ReplicaGroupsOnNDPlaneImpl::$_0` (`@0x1c896400`) **once per mesh axis** — `axis ∈ {0,1,2}` passed as `0x100000000 | axis` — then stores the resulting `optional<vector<MeshNDInfo>>` into the cache.

```c
function ReplicaGroupsOnNDPlane(target, device_assignment, device_list, n_dim, b):  // sub_1C890960
    CHECK(device_assignment != nullptr)
    key.dev   = render(device_assignment)                  // "<id>_<d0>,<d1>,…"
    key.topo  = SerializeToStringDeterministic(Distill(target.topology).ToProto())
    key.n_dim = n_dim; key.b = b
    lock(nd_plane_cache_mutex)
    CHECK(n_dim == 1 || n_dim == 2 || n_dim == 3)          // line 1155, group_utils.cc
                                                           // "…only supports dimension n_dim = 1, 2 or 3."
    if !cache.contains(key):
        v = nullopt
        for axis in {0, 1, 2}:                             // built per mesh axis
            v = ReplicaGroupsOnNDPlaneImpl::$_0(&state, groups, 0x100000000 | axis)   // sub_1C896400
            if !v.has_value: break                         // projection failed ⇒ no plane
        cache[key] = v
    result = cache[key]                                    // optional<vector<MeshNDInfo>>
    unlock(nd_plane_cache_mutex)
    return result
```

The lambda `$_0` (`@0x1c896400`) is where the projection actually happens; it is the dense analog of `ExtractNDPlaneInfo::$_0` (§2.2), but it emits a `MeshNDInfo` ring geometry rather than a single span stride.

```c
function ReplicaGroupsOnNDPlaneImpl::$_0(state, replica_groups, axis_flag):  // sub_1C896400
    n_dim = state.n_dim                                    // ***(int***) = 1 | 2 | 3
    if n_dim == 1:
        // trivial 1-D plane: one MeshNDInfo whose single axis lists all group members
        return optional(vector<MeshNDInfo>{ MeshNDInfo_1D(replica_groups) })
    ldpc = LogicalDevicesPerChip(target, /*TensorCore*/0)  // megacore ⇒ 2
    // mesh extents seeded from the chip torus (X,Y,Z at target[+0x58]/[+0x60]/[+0x161,…])
    if axis_flag & 0x100000000:                            // a specific axis was requested
        a = axis_flag & 3                                  // 0|1|2
        mesh_extent[a] *= ldpc                              // fold the per-chip sub-cores into this axis
    // multi-slice fan-out
    num_slices = GetMultiSliceTopology(target) ? GetNumSlices(target) : 1
    alloc PerSliceReplicaData[num_slices]                  // 88-byte stride per slice
    for each group:
        for each device d in group:
            (core_id, slice) = GetMegascalePerSliceCoreIdAndSliceId(target, da, d)   // sub_1C8906E0
            loc   = TensorCoreLocationForLogicalDeviceId(target, da, core_id, nullopt) // sub_1C8904E0
            (cx,cy,cz) = loc.chip_coordinates()
            idx = mixed_radix_linearize((cx,cy,cz), mesh_extent)    // Horner over mesh strides
            per_slice[slice].grid[idx] = group_member_ordinal
    // per (slice, group) emit one MeshNDInfo by dispatching on n_dim
    for each slice, each group:
        if n_dim == 2: m = ReplicaGroupForm2DRing(group, …, mesh_extent)   // sub_1C88E6E0
        if n_dim == 3: m = ReplicaGroupsOn3DPlane(group, …, NDTopologyInfo) // sub_1C8901E0
        if !m.has_value: return nullopt                    // group does not fit an n_dim plane
        out.push_back(m)
    return optional(out)
```

Three facts a reimplementer must preserve, all decompile-verified:

- **The per-axis sub-core fold.** When a specific axis is requested (`axis_flag & 0x100000000`), that axis's mesh extent is pre-multiplied by `LogicalDevicesPerChip` (`mesh_extent[axis] *= ldpc`, the `v253[8*(axis&3)+16] *= v51` store). On a megacore TensorCore (`ldpc == 2`) the chosen ring axis carries *both* on-chip cores; the other two axes stay at chip granularity. A reimplementation that linearizes against the raw chip extents on all three axes will mis-place the megacore second core.
- **`chip_coordinates`, not raw core id.** Each device is projected through `TensorCoreLocationForLogicalDeviceId` → `chip_coordinates`, then mixed-radix-linearized against the (folded) mesh extents — the same Horner-style `coord + extent·(…)` recurrence the SC path reduces mod `LogicalDevicesPerChip`. The projection is a chip-coordinate placement, not a core-id sort.
- **`n_dim` dispatch + short-circuit.** `n_dim == 1` returns a trivial single-axis `MeshNDInfo` without any ring construction; `n_dim == 2` dispatches each group to `ReplicaGroupForm2DRing` (`@0x1c88e6e0`); `n_dim == 3` to `ReplicaGroupsOn3DPlane` (`@0x1c8901e0`). Any group that does not fit an `n_dim`-axis plane makes the helper return `nullopt`, which aborts the whole projection (`v.has_value` break in the entry loop) and is exactly the "device list does not project onto a `k`-axis plane" signal the AllGather/ReduceScatter selectors test.

> **NOTE —** this resolves the `allgather-nd-ring.md` "What Was Not Resolved" entry that placed the projection math at `0x1c891402` *inside* `ReplicaGroupsOnNDPlane`. The entry function (`0x1c890960`) only builds the cache key and dispatches; the actual coordinate-projection body is the per-axis lambda `ReplicaGroupsOnNDPlaneImpl::$_0` at `0x1c896400`. The `n_dim == 1 || 2 || 3` assertion (`"…only supports dimension n_dim = 1, 2 or 3."`, group_utils.cc line 1155) is the entry function's only inline check.

> **GOTCHA —** the multi-slice path is not optional bookkeeping. When `GetMultiSliceTopology` is set, the lambda allocates one `PerSliceReplicaData` (88-byte stride) per `GetNumSlices`, and a device's slot is selected by `GetMegascalePerSliceCoreIdAndSliceId` (`@0x1c8906e0`), which returns *both* a per-slice core id and the slice index. A single-slice reimplementation that ignores the slice index will collide devices from different slices into the same grid cell on a multi-slice (Megascale) topology.

---

## 3. How the two derivations meet

The device partition (§1) and the ND-plane geometry (§2) are consumed together when the offload config builder lays out the per-color ring schedule. The table summarizes the data flow.

| Quantity | Source | Role |
|---|---|---|
| SC AvailableCoreCount | `TpuTopology[+0x94]` (core type 2) | total SparseCores in topology |
| `LogicalDevicesPerChip(SparseCore)` | `TpuTopology::LogicalDevicesPerChip(2)` `@0x20ad3020` | SparseCores per chip (megacore ⇒ 2) |
| `sc_dev = total / ldpc_sc` | `NumScOffloadDevices` / `NumEmbeddingDevices` | SC logical-device count |
| `num_embedding_devices` | `AutoOr<long>` of `compEnv[+0x898]` | reserved-for-embedding partition |
| `NumScOffloadDevices` | `sc_dev − num_embedding_devices` | offload-available SC count (builder `rbx`) |
| `tensor_split_factor` | `optional<int>` builder arg → proto field 5 | in-collective tensor cut (`==2` ⇒ split mode) |
| per-axis ring segments | `GetDimensionRings`: `extent / devcount − 1` | how the ring is cut along each axis |
| ND-plane dimension count | `popcount(has_x + has_y + has_z)` | torus axes the collective spans (twist gate) |
| per-axis span stride | `$_0` lambda: `coords[1] − coords[0]`, `stride \| extent` | `NDPlaneStrideInfo` `stride_x/y/z` |
| minor-to-major axis order | `GetMinorToMajorOrder` `@0x133c1c40` (reads `NDPlaneStrideInfo` + `topo[+0xa3]`) | ring traversal order across spanned axes |

The `NumScOffloadDevices` total bounds the running `devcount` that `GetDimensionRings` partitions across X/Y/Z; the `NDPlaneInfo` dimension count selects the builder's twist / ND-plane branch; the `tensor_split_factor` modifies the per-color emission (color duplication in mode 2). `GetMinorToMajorOrder` then turns the per-axis `NDPlaneStrideInfo` span sizes into the minor-to-major axis ordering the per-color rings iterate, gated by the `topo[+0x3b8][+0xa3]` `across_cores_on_chip` flag (FATAL `"Stride x/y/z should be set for 3D plane."` at lines 2295/2297/2299 if a 3D plane is missing an axis stride).

---

## 4. Verification notes

> **[CONFIRMED]** Cross-checked against the IDA decompile of `libtpu.so` v0.0.40 (build-id `89edbbe8…`):
> - **`NumScOffloadDevices`** (`@0x1d6b8b00`) — `sc_total = topo[+0x94]`; `ldpc_sc = LogicalDevicesPerChip(2)`; `sc_dev = sc_total/ldpc_sc` (idiv, `ldpc_sc>0` guard); `AutoOr<long>::FromProtoOrDie(compEnv[+0x898])` engaged-bit; both FATAL CHECKs (`"num_embedding_devices >= 0"` line 1803, `"num_embedding_devices <= sc_per_device"` line 1805); `return sc_dev − n_emb` — all byte-exact. Sibling `NumEmbeddingDevices` (`@0x1d6b8a00`) confirmed as the complement partition.
> - **`tensor_split_factor`** gate (AllGather builder `@0x133c82c0`) — `value_or(1)`; `cmp 2` `>= 2` gate; RetCheck `"!use_single_core.value_or(kDefaultUseSingleCore)"` → `"A larger than 1 tensor split factor requires more than one sparse core to split the tensor on."`; RetCheck `"tensor_split_factor.value_or(kDefaultTensorSplitFactor) == 2"` (line 1558) → `"We currently only support tensor split factor of 2 across two sparse cores."`; VLOG `"Adopting split tensor mode."`; the mode-2 effect VLOG `"Twisted torus: duplicate colors as indicated by tensor split factor."` — all byte-exact. Forwarding from AR (`push [rbp+0x10] @0x133c2d01`) / RS confirmed; AllGather wrapper `@0x133c76c0` confirmed to have no `optional<int>` parameter.
> - **`GetDimensionRings`** (`@0x133df520`) — X/Y/Z extents `[+0x3b8][0x58/0x5c/0x60]`; `LogicalDevicesPerChip(0)` (TC) megacore detect; the split `extent / devcount − 1` byte-confirmed at `@0x133df670` (`idiv %ebx`) / `@0x133df672` (`dec %eax`) / `@0x133df674` (`mov %eax,-0xb8(%rbp)`; decompile `(int)v12 / v10 − 1`) — exact.
> - **`ExtractNDPlaneInfo::$_0`** (`@0x133bf700`) — `coords.size()==1` fast path (`has_size=0`); `stride = coords[1] − coords[0]`; the three RetChecks at lines 922/923/925 (`"Stride must be larger or equal to 1."` / `"Stride must be less than the dimension size."` / `"Stride must divide the dimension size."`); the per-pair stride-consistency scan (line 931); the `has_size=1, size=stride` success store — all byte-exact.
> - **`NDPlaneInfo` / `NDPlaneStrideInfo` layout** — `NDPlaneInfo::ToString @0x10fdf2a0` labels `"size_x: "`/`"size_y: "`/`"size_z: "`/`"stride_info: "`; `NDPlaneStrideInfo::ToString @0x10fe62e0` labels `"stride_x: "`/`"stride_y: "`/`"stride_z: "`/`"across_cores_on_chip: "` with reads at `+0`/`+4`, `+8`/`+0xc`, `+0x10`/`+0x14`, `+0x18`; the two consumers `GetCollectiveNDPlaneDimensionCount @0x133bb6e0` (has-bits) and `GetMinorToMajorOrder @0x133c1c40` (`has`@`+0x10`/`+0x18`/`+0x20`, `size`@`+0xc`/`+0x14`/`+0x1c`) agree with the ToString-derived layout — byte-exact.
> - **`GetCollectiveNDPlaneDimensionCount`** (`@0x133bb6e0`) — `*((_DWORD *)this + 2) = v26 + v27 + v28` (sum of the three has-bytes) confirmed; CHECK `"collective != nullptr"` line 845; `AddSourceLocation` lines 849/852 — exact.
> - **dense `ReplicaGroupsOnNDPlane`** (`@0x1c890960`) + lambda `ReplicaGroupsOnNDPlaneImpl::$_0` (`@0x1c896400`) — entry function builds the `NDPlaneCacheKey` (device-assignment render + `TpuTopologySerdes::Distill`/`ToProto`/`SerializeToStringDeterministic` + `n_dim` + bool) under `nd_plane_cache_mutex`, asserts `n_dim == 1||2||3` (`"…only supports dimension n_dim = 1, 2 or 3."`, group_utils.cc line 1155), and on a cache miss calls the lambda once per axis with `0x100000000 | axis` (`axis ∈ {0,1,2}`) — byte-confirmed. The lambda's per-axis sub-core fold `mesh_extent[axis&3] *= LogicalDevicesPerChip(0)` (`v253[8*(a5&3)+16] *= v51`), the `n_dim==1` trivial-`MeshNDInfo` short-circuit (`**(int**)a2 == 1` arm), the per-device `TensorCoreLocationForLogicalDeviceId` (`@0x1c8904e0`) → `chip_coordinates` → mixed-radix linearize, the multi-slice fan-out via `GetMultiSliceTopology`/`GetNumSlices`/`GetMegascalePerSliceCoreIdAndSliceId` (`@0x1c8906e0`) with an 88-byte `PerSliceReplicaData` stride, and the `n_dim`-dispatch to `ReplicaGroupForm2DRing` (`@0x1c88e6e0`) / `ReplicaGroupsOn3DPlane` (`@0x1c8901e0`) with `nullopt`-on-no-fit — all decompile-verified.
>
> **[LOW]** Confirmed by structure / label, not by an independent numeric consumer:
> - The exact arithmetic the top-level `NDPlaneInfo` `size_x/y/z` ints carry (plane *extent* vs *device-count along axis*): the labels are byte-read and `size_z` (`+0x8`) is used as the iteration/plane bound by `GetMinorToMajorOrder`, but the producer arithmetic in `ExtractNDPlaneInfo`'s sret tail (`@0x133bc674`..`@0x133bc880`) was not individually decoded — the size semantic is inferred from the `"size_x: "` label and the consumer use.
> - The unification of `NDPlaneStrideInfo` (SC) with the dense `MeshNDInfo` (TC) — both report a per-axis span, but the field-by-field correspondence was not closed; the SC dimension count uses `popcount` of the has-bits, the TC uses `ReplicaGroupsOnNDPlane.num_mesh_dims`.

---

## Cross-References

**Scope boundaries (this page's neighbors)**
- [Physical-Core Placement](physical-core-placement.md) — where the chosen logical colors land on concrete physical SC cores, and the `tensor_split_mode==2` per-core emission keyed by `TensorSplitPerCoreClassifier`
- [SelectNDStrategy](strategy-nd-picker.md) — the dense-substrate ND-strategy *choice* (sub-plane / ND-ring / twisted / strided), the TC `ReplicaGroupsOnNDPlane` analog this page's `NDPlaneInfo` mirrors

**SparseCore-offload substrate**
- [SC-Offload Config Builder](sc-offload-config-builder.md) — the `*OffloadConfig` proto carrying `tensor_split_factor` (field 5), and the `ConstructConfigForCollectiveUniDirNDGroups<*>` builder this page's gate lives inside
- [HierarchicalKind](hierarchical-kind.md) — the `AutoOr<bool>` flat-vs-hierarchical split the offload builder dispatches on
- [SC Core-Selection (Offload)](sc-core-selection-offload.md) — `SparseCoreConfig.offload` op-type classification + core selection

**Substrate map + sibling subsystems**
- [On-Pod Collectives — Section Map](overview.md) — the substrate split, the SC-offload gate, and the shared physical-torus mesh decomposition
- [AllToAll Tables](alltoall-tables.md) — the all-to-all / ragged link tables (`EstimatePhysicalLinksUsed`)
- [back to index](../index.md)
