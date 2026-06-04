# SC-Offload Config Builder

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the templated builder signature, body pipeline, per-color emission lambda, and the three `*OffloadConfig` struct families were cross-checked against the IDA decompile; two residual sub-layouts marked **[LOW]** below · **Part XIII — On-Pod Collectives & Barriers** / SparseCore-offload collectives · [back to index](../index.md)

## Abstract

When the SparseCore-offload substrate gate holds (see **[On-Pod Collectives — Section Map](overview.md)** §5), an embedding-class collective is **not** lowered to HLO `ReplicaGroup` device lists. Instead a templated builder constructs a `CollectiveIciStrategyConfig` proto — a per-color set of unidirectional (UNIDIR) rings over the physical X/Y/Z torus — and embeds it inside one of three byte-identical backend-config messages: `AllGatherOffloadConfig`, `AllReduceOffloadConfig`, or `ReduceScatterOffloadConfig`. This page documents the builder algorithm and the OffloadConfig struct family that carries its output.

The builder is `xla::tpu::sparse_core::collective::(anonymous)::ConstructConfigForCollectiveUniDirNDGroups<OffloadConfig, HloXxx>`, instantiated three times (one per op family). It is the **SparseCore analog of the dense TensorCore `StrategyND::BuildStrategy`**: same chip torus extents, same K/2K twist geometry, but emitting a proto of per-color rings instead of HLO replica-group lists. The flat-vs-hierarchical phase split is governed by the `HierarchicalKind` argument, documented on its own page; the SparseCore-core selection and the offload gate are documented elsewhere. This page owns three things:

1. the **`ConstructConfigForCollectiveUniDirNDGroups<*>` builder algorithm** (body pipeline + the three public ND wrappers that fix `HierarchicalKind`),
2. the **three `*OffloadConfig` struct layouts** (`sizeof 0x48`, byte-identical) and the `CollectiveIciStrategyConfig` proto nest they carry,
3. the **per-color ring construction** — how `GetDimensionRings` per-axis attributes feed the per-color `PerColorIciStrategyConfig` / `IciStrategyRingConfig` emission.

Contract of the builder as observed in the binary:

- The builder is a single templated function `@0x133c82c0` (AllGather) / `@0x133c2dc0` (AllReduce) / `@0x133cd800` (ReduceScatter), source `offload_collective_config.cc`. All three share the exact mangled signature; only the proto type and Hlo instruction type differ.
- The output is a `CollectiveConfigInfo<OffloadConfig>` — the populated `*OffloadConfig` proto plus its strategy-dimension metadata — returned by `sret` as an `absl::StatusOr<...>`.
- The per-axis ring set is produced by `GetDimensionRings`, which reads the **same chip torus extents** (`[chip_cfg+0x58]`=X, `+0x5c`=Y, `+0x60`=Z) as the dense picker and the cost model — so the SparseCore `IciStrategyRingDim` ring dims index the identical hardware geometry as the dense `StrategyND` ring dims.
- **AllGather and ReduceScatter are pinned flat** (their wrappers hardwire `HierarchicalKind == 0x100`); **only AllReduce can take the hierarchical multi-phase path**, and only when the compile flag is engaged+true and the caller's optional override does not force flat.

## At a glance

| Aspect | Value (byte-anchored) |
|--------|----------------------|
| Templated builder | `ConstructConfigForCollectiveUniDirNDGroups<OffloadConfig, HloXxx>` |
| Instantiations | AllGather `@0x133c82c0`, AllReduce `@0x133c2dc0`, ReduceScatter `@0x133cd800` |
| Source TU | `offload_collective_config.cc` (string `@0x133c82c0` body, line `1616`) |
| Return | `absl::StatusOr<CollectiveConfigInfo<OffloadConfig>>` (`sret`) |
| Public ND wrappers | `…AllGatherUniDirND @0x133c76c0`, `…AllReduceUniDirND @0x133c2c80`, `…ReduceScatterUniDirND @0x133ccbe0` |
| Per-axis ring source | `GetDimensionRings @0x133df520` (X/Y/Z extent `+0x58/+0x5c/+0x60` + LDPC megacore) |
| Twist bridge | `TryCreateTwistedTorusTopologyInfo @0x133e1980` (gated by `cmp $3` mesh-dim count) |
| Per-color appender | lambda `@0x133e0a80` (AG) / `0x133ddae0` (AR) / `0x133e0c00` (RS) |
| OffloadConfig structs | `AllGather`/`AllReduce`/`ReduceScatter` `OffloadConfig` — byte-identical, `sizeof 0x48` |
| Carried proto nest | `CollectiveIciStrategyConfig` → `PerColorIciStrategyConfig[]` → `IciStrategyRingConfig[]` |

---

## 1. The templated builder entry + ABI

The builder is one templated function with three instantiations. The Itanium-mangled signature decodes (from the decompiled symbol) to:

```cpp
// xla::tpu::sparse_core::collective::(anonymous namespace)::
template <class OffloadConfig, class HloXxx>
absl::StatusOr<CollectiveConfigInfo<OffloadConfig>>
ConstructConfigForCollectiveUniDirNDGroups(
    const jellyfish::Target&     target,        // [+0]  chip-config carrier
    const DeviceAssignment&      device_assign, //       replica → device map
    const HloXxx*                hlo,           //       AllGather/AllReduce/ReduceScatter inst
    bool                         arg0,          //       leading bool (see §1.1)
    bool                         arg1,          //
    jellyfish::HierarchicalKind  hier,          //       flat-vs-hierarchical discriminant
    std::optional<bool>          opt0,
    std::optional<bool>          opt1,
    std::optional<int>           tensor_split); //       tensor_split_factor override
```

The three instantiations differ only in `OffloadConfig`/`HloXxx`:

| Instantiation | VA | `OffloadConfig` | `HloXxx` |
|---------------|----|-----------------|----------|
| AllGather | `0x133c82c0` | `AllGatherOffloadConfig` | `HloAllGatherInstruction` |
| AllReduce | `0x133c2dc0` | `AllReduceOffloadConfig` | `HloAllReduceInstruction` |
| ReduceScatter | `0x133cd800` | `ReduceScatterOffloadConfig` | `HloReduceScatterInstruction` |

> The mangled symbol read from the AllGather instantiation is `…StatusOrINS2_20CollectiveConfigInfoIT_EEEERKNS5_6TargetERKNS_16DeviceAssignmentEPKT0_bbNS3_16HierarchicalKindENSt3__u8optionalIbEESQ_NSP_IiEE` — i.e. `(Target const&, DeviceAssignment const&, T0 const*, bool, bool, HierarchicalKind, optional<bool>, optional<bool>, optional<int>) → StatusOr<CollectiveConfigInfo<T>>`. The `bbNS3_16HierarchicalKind…` fragment confirms two `bool`s then `HierarchicalKind` then the three `optional`s. All three instantiations carry the identical fragment with only the message/Hlo type swapped.

### 1.1 The three public ND wrappers — who sets `HierarchicalKind`

Option generation does not call the templated builder directly; three public wrappers do, each fixing `HierarchicalKind` for its op family:

| Wrapper | VA | `HierarchicalKind` passed | Phase behaviour |
|---------|----|--------------------------|-----------------|
| `ConstructConfigForAllGatherUniDirND` | `0x133c76c0` | hardwired `0x100` | always FLAT (single-phase) |
| `ConstructConfigForReduceScatterUniDirND` | `0x133ccbe0` | hardwired `0x100` | always FLAT (single-phase) |
| `ConstructConfigForAllReduceUniDirND` | `0x133c2c80` | caller `optional<bool>` | FLAT *or* HIERARCHICAL |

The AllGather and ReduceScatter wrappers pass the engaged-but-false discriminant `0x100`, which the builder reads as "explicitly flat". The AllReduce wrapper is the only one that can reach the hierarchical multi-phase path. Its leading `bool` argument is computed as

```text
arg0  =  (!ShouldEnableSparseCoreHierarchicalAllReduce(target_comp_env))   @0x133c2c80
       && ((~caller_optional_hier) & 0x101 != 0)
```

i.e. "use the flat path" — true unless the compile flag is engaged+true **and** the caller's optional override does not force flat.

> In the AllReduce wrapper decompile (`…AllReduceUniDirND…_0x133c2c80.c`) `ShouldEnableSparseCoreHierarchicalAllReduce` appears at lines 18/35 and is combined with a `0x101` mask at line 41 — matching the inversion above. The flat-pin of AG/RS (`0x100`) is the engaged+false discriminant; the full enum decode is on **[HierarchicalKind](hierarchical-kind.md)**.

---

## 2. The builder body pipeline

Traced on the AllGather instantiation `@0x133c82c0` (the AllReduce/ReduceScatter bodies are structurally identical, differing only in proto type and — for AllReduce — the reachable hierarchical branch). Line numbers are into the decompiled C of that function.

```text
ConstructConfigForCollectiveUniDirNDGroups<AllGatherOffloadConfig, HloAllGatherInstruction>:

 [a]  CheckInputOutputNumberOfElementIsBelowLimit(hlo)          @line 529 (0x133dcc00)
        └─ size gate; on failure return an error Status

 [b]  construct empty AllGatherOffloadConfig proto              (ctor @0x1d6ee220; sizeof 0x48)

 [c]  GetPhysicalDeviceGroups(hlo, device_assign, megacore)     @line 552 (0x133b6b80)
        └─ the replica device groups, with a megacore-aware byte read from the Hlo

 [d]  ExtractNDPlaneInfo(target, device_assign, hlo, …)         @line 571 (0x133bb940)
        └─ NDPlaneInfo: per-axis ring-dim / extent descriptor
        └─ RetCheck "IsNDPlaneSpanAcrossEntireDimension"        @line 595
           (the ND-plane must span the entire torus dimension, else bail)

 [e]  read chip torus extents from (target +0x3b8):             @lines 567-569
        v613 = *(int*)(v17 + 88);   // 0x58 = X extent
        v615 = *(int*)(v17 + 92);   // 0x5c = Y extent
        addr = *(int*)(v17 + 96);   // 0x60 = Z extent

 [f]  per plane-dim: push tuple<IciStrategyRingDim, long, long> onto a deque
        └─ ring-dim slot = 2 - (NDPlaneInfo[+0xa0] & 1)   (mesh-vs-torus / megacore parity)
        └─ tuple stride 0x18 : {ringDim @+0, lo @+8, hi @+0x10}

 [g]  HierarchicalKind dispatch  (mask & 0x101, see §3):
        (HierarchicalKind & 0x101) == 0x100  →  FLAT  : one GetDimensionRings per axis
                                                        inserted directly into the flat_map
        else                                 →  HIER  : walk the deque, each queued ring
                                                        dim processed as a phase

 [h]  GetDimensionRings(target, ringDim, devcount, …, megacore_aware)  @line 3104 (0x133df520)
        └─ per-axis ring partitioner → RingConfigAttributes (0x18-byte POD)

 [i]  accumulate into flat_map<IciStrategyRingDim, RingConfigAttributes>  (operator[] @0x133ddc60)

 [j]  TWIST gate: count how many of the 3 mesh dims == K (one operand) or == 2K (other)
        └─ if all 3  (cmp $3)  →  TryCreateTwistedTorusTopologyInfo(min,max,K,2K)  @line 936 (0x133e1980)
                                  VLOG "Skipping twisted torus strategy because its strided."

 [k]  per-color emission: lambda(long) @0x133e0a80
        └─ index/create the color's PerColorIciStrategyConfig and Add() an IciStrategyRingConfig
        └─ CopyRuntimeConfigToProtoLiteral<AllGatherOffloadConfig>  @lines 2641/3366 (0x133dd260)

 [l]  return CollectiveConfigInfo<AllGatherOffloadConfig>(config, strategy_dimension)
```

> Every callee in the pipeline was located at the cited line in `…AllGatherIn_dfa0bc1bcac2597e_0x133c82c0.c`: `CheckInputOutputNumberOfElementIsBelowLimit` (529), `GetPhysicalDeviceGroups` (552), `ExtractNDPlaneInfo` (571), `IsNDPlaneSpanAcrossEntireDimension` (595), `TryCreateTwistedTorusTopologyInfo` (936), `GetDimensionRings` (3104), `CopyRuntimeConfigToProtoLiteral` (2641, 3366), and the chip extents at `v17 + 88/92/96` (567-569). The `MakeErrorStream` call at line 3206 cites source `offload_collective_config.cc:1616`, pinning the TU name.

### 2.1 `GetDimensionRings` — the per-axis ring partitioner

`GetDimensionRings @0x133df520` is the per-axis worker that turns one `IciStrategyRingDim` plus the device count into a `RingConfigAttributes` POD. (It is its own out-of-line function — `xla::tpu::sparse_core::collective::(anonymous namespace)::GetDimensionRings(Target const&, IciStrategyRingDim, int, bool, bool)` — `call`ed from the builder body at the `@line 3104` site, not inlined.)

```text
RingConfigAttributes GetDimensionRings(
    const Target&        target,
    IciStrategyRingDim   ring_dim,        // 1..7
    int                  devcount,
    bool                 /*r8*/,
    bool                 megacore_aware): // r9

  1. validate ring_dim ∈ [1..7]                         (jump table)
       RetCheck "dim_x || dim_y || dim_z"               (offload_collective_config.cc:417)
  2. select chip torus extent by ring_dim:
       X → [chip_cfg +0x58]   Y → [+0x5c]   Z → [+0x60]
       (set the mesh-vs-torus flag per switch arm)
  3. LDPC = LogicalDevicesPerChip(target)               (0x1d615b00)
       megacore = megacore_aware && (LDPC >= 2)          (setge)
  4. divide extent by devcount → ring length / segment counts
  5. emit RingConfigAttributes {flag, lo, hi}            (0x18-byte POD)
```

The X/Y/Z chip-extent offsets `+0x58/+0x5c/+0x60` are the **same offsets** the dense picker, the cost model, and the twist geometry read — this is the shared-geometry invariant noted in **[overview](overview.md)** §1.3.

The `RingConfigAttributes` value is a 24-byte (`0x18`) POD copied verbatim into the flat_map and then into the per-color ring. Its three fields `{flag/int, lo, hi}` were traced to the deque-tuple correspondence (`{ringDim, lo, hi}`) but **not** individually named to a ring-length / segment-count / neighbor-stride semantic — see [LOW] in §6.

---

## 3. The HierarchicalKind dispatch (flat vs multi-phase)

The builder dispatches on `HierarchicalKind`, an `xla::jellyfish::AutoOr<bool>`-packed 16-bit value (bit 0 = the contained bool value, bit 8 = the "engaged" bit, mask `0x101` = the discriminant every consumer applies). The full enum decode lives on **[HierarchicalKind](hierarchical-kind.md)**; here is only how the *builder* uses it.

```text
v75 = (HierarchicalKind value) & 0x101;          @line 3032   (the mask)

FLAT  path  when (HierarchicalKind & 0x101) == 0x100  →  one GetDimensionRings call per axis,
                                                          inserted directly into the flat_map
HIER  path  otherwise                                 →  the queued-deque multi-phase walk

guard:  if ((~hier_word & 0x101) == 0) { … }     @lines 3204, 3262
            └─ MakeErrorStream("offload_collective_config.cc", 1616, …)   @line 3206
```

The `(~hier_word & 0x101) == 0` test is true exactly when both the value bit and the engaged bit are set — i.e. the *hierarchical* discriminant `0x101`. That branch reaches the multi-phase deque walk and an associated validation error path. The flat path (`== 0x100`) is what AllGather/ReduceScatter hardwire, so in this build only AllReduce ever evaluates the hierarchical branch.

> The `& 0x101` mask at line 3032 and the `(~v & 0x101) == 0` discriminant at lines 3204/3262 are present byte-exact in the AllGather builder decompile; the `0x100` flat comparison sites (`(_DWORD)v615 == 256`) are at lines 3102 and 3525. The mechanism — flat single-ring per axis vs the queued multi-phase walk — is confirmed; the *contents* of the hierarchical AllReduce emission (how the intra-chip and inter-chip phase rings differ in `IciStrategyRingType`/neighbor) was not fully expanded — see [LOW] §6.

---

## 4. The three `*OffloadConfig` struct layouts

The three backend-config messages — `AllGatherOffloadConfig`, `AllReduceOffloadConfig`, `ReduceScatterOffloadConfig` — are **byte-identical** in memory layout (proto-generated from the same field set), `sizeof = 0x48` (72 bytes). Only the vtable / typeinfo pointer differs.

| Offset | Field | Proto field (#, type) |
|--------|-------|-----------------------|
| `+0x00` | vptr | AG vtbl `0x21ce1ce0` / AR `0x21ce1ca0` / RS `0x21ce1c60` (`+0x10`) |
| `+0x08` | `InternalMetadata` (Arena ptr \| tag bits) | — (proto2 internal) |
| `+0x10` | `hasbits` (int32) | bit0=`physical_core_indices`, bit1=`ici_strategy_config`, bit2=`constant_propagation_config`, bit3=`use_single_sparse_core`, bit5=`use_n_dimension_strategy` |
| `+0x18` | `RepeatedField<int32>` flags \| arena tag | `physical_core_indices` heap-bit (low bit) |
| `+0x1c` | int32 `current_size` | `physical_core_indices` element count |
| `+0x20` | `Rep*` (`int32[]` at `Rep+0x8`) | `physical_core_indices` data |
| `+0x28` | int32 serialized-size cache | (`physical_core_indices` cached byte length) |
| `+0x30` | `message*` `ici_strategy_config` | 2, `CollectiveIciStrategyConfig` |
| `+0x38` | `message*` `constant_propagation_config` | 3, `CollectiveOffloadConstantPropagationConfig` |
| `+0x40` | `bool` `use_single_sparse_core` | 1, optional bool |
| `+0x44` | int32 scalar | `tensor_split_factor` / `use_n_dimension_strategy` (low-byte region) |

The repeated `physical_core_indices` is a standard proto2 `RepeatedField<int32>` at `+0x18..+0x20` (heap-bit `+0x18`, `current_size` `+0x1c`, `Rep*` `+0x20` with the `int32[]` at `Rep+0x8`); the writer/reader (`AddCollectivePhysicalCoreIndicesHelper` / `GetPhysicalCoreIndices`) and the `_InternalSerialize` witness all agree on these offsets — see **[Physical-Core Placement](physical-core-placement.md)** §1. The `bool` scalars `use_single_sparse_core` (field 1) and `use_n_dimension_strategy` (field 6) live in the `+0x40` / low-byte region; the builder reads `tensor_split_factor` from its `optional<int>` argument and fills `physical_core_indices`.

The proto field schema (read from the serialized `FileDescriptorProto` in `.rodata`):

```protobuf
// byte-identical for AllGather / AllReduce / ReduceScatter OffloadConfig
message {AllGather|AllReduce|ReduceScatter}OffloadConfig {
  bool                                       use_single_sparse_core      = 1;
  CollectiveIciStrategyConfig                ici_strategy_config         = 2;
  CollectiveOffloadConstantPropagationConfig constant_propagation_config = 3;
  repeated int32                             physical_core_indices       = 4;
  int32                                      tensor_split_factor         = 5;
  bool                                       use_n_dimension_strategy    = 6;
}
```

> The three ctors `@0x1d6ee220` (AG) / `0x1d6ed860` (AR) / `0x1d6eebe0` (RS), with matching `Clear` / `ByteSizeLong` triplets, zero the identical `0x48`-byte range; the three proto symbols (`AllGather`/`AllReduce`/`ReduceScatter` `OffloadConfig`) are present throughout the builder decompile and in the `CopyRuntimeConfigToProtoLiteral<…>` and `CollectiveConfigInfo<…>` instantiations. The vtable/typeinfo bases and the field offsets are byte-anchored.

### 4.1 The outer wrapper and the cost-model probe

The three `*OffloadConfig` messages do not stand alone — each is one arm of a `CollectiveOffloadConfig` oneof (`GetIciStrategyAndConstantConfig @0x133d2c20`) over `{all_reduce | all_gather | reduce_scatter | ragged_all_to_all | all_to_all}_offload_config` (field 1). This is the message the SparseCore-offload **cost** path probes: `GetCollectiveOffloadConfig @0x133e1740` reads exactly the `CollectiveIciStrategyConfig` this builder emitted and charges the SparseCore ring operating point rather than the dense TensorCore one (see [overview](overview.md) §1.2 and the cost page).

---

## 5. The per-color ring construction (the proto nest)

The builder's output rings are accumulated into the `ici_strategy_config` sub-tree of the OffloadConfig. The nest is the SparseCore counterpart to the dense `StrategyND` per-color ring schedule:

```text
{AllGather|AllReduce|ReduceScatter}OffloadConfig        (sizeof 0x48)
  └─ ici_strategy_config : CollectiveIciStrategyConfig   (field 2)
       └─ color_strategies[]  : PerColorIciStrategyConfig          ([+0x18 repeated, +0x20 size])
            └─ phase_rings[]   : IciStrategyRingConfig             ([+0x18 repeated, +0x20 size])
```

```protobuf
message CollectiveIciStrategyConfig {
  repeated PerColorIciStrategyConfig color_strategies = 1;
}
message PerColorIciStrategyConfig {
  repeated IciStrategyRingConfig phase_rings = 1;
}
message IciStrategyRingConfig {            // 13 scalars; hasbits @[+0x10] = 0x1 .. 0x1000
  IciStrategyRingType    ring_type;
  IciStrategyRingNeighbor ring_neighbor;
  int32                  core_count;
  IciStrategyRingDim     ring_dim;
  int32                  ring_neighbor_table_offset;
  int32                  barrier_id;
  bool                   across_cores_on_chip;
  bool                   has_reordering_map;
  IciStrategyRingDim     explicit_strategy_ring_dim;
  int32                  core_count_adjustment;
  bool                   partner_transfers_outside_the_ring;
  int32                  id_info_offset;
  int32                  group_info_table_offset;
}
```

### 5.1 The per-color appender lambda

The per-color emission is a lambda — `@0x133e0a80` for AllGather, `0x133ddae0` for AllReduce, `0x133e0c00` for ReduceScatter. It takes a color index (`long`), indexes-or-creates that color's `PerColorIciStrategyConfig`, and `Add()`s a fresh `IciStrategyRingConfig` whose fields it copies from the accumulated `RingConfigAttributes`:

```text
lambda(long color):
   1. RepeatedPtrFieldBase::Add  →  PerColorIciStrategyConfig[color]    (proto2 repeated)
   2. RepeatedPtrFieldBase::Add  →  IciStrategyRingConfig               (a new phase ring)
   3. fill ring_type / ring_dim / core_count / … from RingConfigAttributes
```

> In the AllGather per-color lambda (`…_adac2b005e1e2b0d_0x133e0a80.c`) the body shows, in order, `RepeatedPtrFieldBase` → `PerColorIciStrategyConfig` (line 38), then `RepeatedPtrFieldBase` → `IciStrategyRingConfig` (line 67), indexed by `color` (line 78) — exactly the index-color / Add-ring sequence above. The 13-scalar `IciStrategyRingConfig` field *set* and the has-bit count are byte-exact; the per-field byte offset of each scalar within the message was not individually pinned — see [LOW] §6.

### 5.2 The ring enums

The three enums the per-color rings reference were read from the proto `NameOfDenseEnum` tables in `.rodata`:

| `IciStrategyRingDim` | val | | `IciStrategyRingType` | | `IciStrategyRingNeighbor` |
|----------------------|-----|-|-----------------------|-|---------------------------|
| `ICI_RING_DIM_INVALID` | 0 | | `ICI_RING_TYPE_INVALID_RING_TYPE` | | `ICI_RING_NEIGHBOR_INVALID` |
| `ICI_RING_DIM_X_TORUS` | 1 | | `ICI_RING_TYPE_BIDIR` | | `ICI_RING_NEIGHBOR_EXPLICIT` |
| `ICI_RING_DIM_X_MESH` | 2 | | `ICI_RING_TYPE_UNIDIR_CW` | | `ICI_RING_NEIGHBOR_IMPLICIT` |
| `ICI_RING_DIM_Y_TORUS` | 3 | | `ICI_RING_TYPE_UNIDIR_CCW` | | |
| `ICI_RING_DIM_Y_MESH` | 4 | | `ICI_RING_TYPE_UNIDIR_ALL_TO_ALL_CW` | | |
| `ICI_RING_DIM_Z_TORUS` | 5 | | `ICI_RING_TYPE_UNIDIR_ALL_TO_ALL_CCW` | | |
| `ICI_RING_DIM_Z_MESH` | 6 | | | | |
| `ICI_RING_DIM_D2D` | 7 | | | | |

Ring dims 1/3/5 are the `*_TORUS` axes, 2/4/6 the `*_MESH` axes; torus-vs-mesh is the wrap mode `GetDimensionRings` sets per axis from the chip extent. `ConstructConfigForCollectiveUniDirNDGroups` emits the UNIDIR ring-type set (`ICI_RING_TYPE_UNIDIR_CW` / `_CCW`), consistent with its name; the `ICI_RING_DIM_D2D` value (7) is the die-to-die ring dimension.

---

## 6. SC-offload builder vs dense `StrategyND` — structure parallel

The builder is deliberately the SparseCore mirror of the dense `StrategyND::BuildStrategy` (**[SelectNDStrategy](strategy-nd-picker.md)**). The two produce the same ring geometry over the same torus, but in different output forms:

| Aspect | Dense `StrategyND` (TensorCore) | SC-offload builder (this page) |
|--------|---------------------------------|--------------------------------|
| Builder | `TwistedTorusND::BuildStrategy` / phase replica-groups | `ConstructConfigForCollectiveUniDirNDGroups<*>` |
| Output | HLO `ReplicaGroup` device lists | `CollectiveIciStrategyConfig` proto (per-color UNIDIR rings) |
| Per-dim ring source | `ComputeColorDimensions` + RingLocation tables | `GetDimensionRings` (X/Y/Z extent + LDPC) → `RingConfigAttributes` |
| Dim enum | implicit X/Y/Z torus | `IciStrategyRingDim` `{X,Y,Z}_{TORUS,MESH}` + `D2D` (8 vals) |
| Twist gate | mesh-dim `2·a == dim` count | count 3 mesh dims `== K`/`== 2K` (`cmp $3`) → `TryCreateTwistedTorusTopologyInfo` |
| Phase split | megacore `LogicalDevicesPerChip` 2m / 2m+1 | `HierarchicalKind` `AutoOr<bool>` bit0/bit8 (flat vs multi) |
| Consumer | XLA collective scheduler | SparseCore emitters + `GetCollectiveOffloadConfig` (cost) |

The twist gate is the bridge to the SparseCore twisted-torus path: when all three mesh dimensions match the K/2K geometry, the builder calls `TryCreateTwistedTorusTopologyInfo @0x133e1980` (`@0x133c9560` in the AllGather body, `@0x133c45db` in AllReduce) — the same twist subsystem documented under **[Twisted Torus](../twist/overview.md)**.

---

## 7. Verification notes

> Cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - **Templated signature** — the mangled symbol of all three instantiations carries `…bbNS3_16HierarchicalKindENSt3__u8optionalIbEESQ_NSP_IiEE` = `(…, bool, bool, HierarchicalKind, optional<bool>, optional<bool>, optional<int>) → StatusOr<CollectiveConfigInfo<T>>` — exact.
> - **Body pipeline** — `CheckInputOutputNumberOfElementIsBelowLimit` (line 529), `GetPhysicalDeviceGroups` (552), `ExtractNDPlaneInfo` + `IsNDPlaneSpanAcrossEntireDimension` RetCheck (571/595), chip extents `v17 + 88/92/96` = `0x58/0x5c/0x60` (567-569), `TryCreateTwistedTorusTopologyInfo` (936), `GetDimensionRings` (3104), `CopyRuntimeConfigToProtoLiteral` (2641/3366) — all present in order.
> - **HierarchicalKind dispatch** — `& 0x101` at line 3032; `(~v & 0x101) == 0` at lines 3204/3262; `0x100` (`== 256`) flat sites at 3102/3525 — exact.
> - **AllReduce wrapper** — `ShouldEnableSparseCoreHierarchicalAllReduce` (lines 18/35) combined with `0x101` (line 41); AG/RS wrappers (`0x133c76c0`/`0x133ccbe0`) hardwire `0x100` — exact.
> - **OffloadConfig structs** — three byte-identical `sizeof 0x48` messages via ctors `0x1d6ee220`/`0x1d6ed860`/`0x1d6eebe0`; the `_InternalSerialize` witness (`@0x1d6ee760`, AG) pins `physical_core_indices` `RepeatedField<int32>` at `+0x18`(heap-bit)/`+0x1c`(size)/`+0x20`(`Rep*`) with serialized-size cache `+0x28`, `ici_strategy_config` msg ptr `+0x30`, `constant_propagation_config` msg ptr `+0x38`, `use_single_sparse_core` bool `+0x40`, and a `<6>` int32 scalar `+0x44`; the carried `CollectiveIciStrategyConfig` → `PerColorIciStrategyConfig` → `IciStrategyRingConfig` nest — confirmed (matches the `AddCollectivePhysicalCoreIndicesHelper` writer and `GetPhysicalCoreIndices` reader on [Physical-Core Placement](physical-core-placement.md) §1).
> - **Per-color appender** — lambda `@0x133e0a80` Adds `PerColorIciStrategyConfig` (line 38) then `IciStrategyRingConfig` (line 67) keyed by `color` (line 78) — exact.
> - **Source TU** — the `MakeErrorStream` call (line 3206) cites `offload_collective_config.cc:1616`, pinning the translation unit (the raw working note's `_builder.cc` suffix is corrected to the binary-confirmed name).
>
> **[LOW]** Two residuals were confirmed by *structure* but not by per-field numeric decode:
> - The exact per-field byte offset of each of the 13 `IciStrategyRingConfig` scalars within `[+0x18..0x50]`: the field *set* and the 13 has-bits (`0x1..0x1000`) are byte-exact, but which byte holds `ring_type` vs `core_count` vs `barrier_id` etc. was not individually pinned.
> - The three `RingConfigAttributes` fields (`{flag/int, lo, hi}`, the flat_map value): proven a `0x18`-byte POD copied verbatim into each ring, but the semantic of the three fields (ring length vs segment count vs neighbor stride) was traced only to the deque-tuple correspondence, not separately named.
> - The hierarchical (non-`0x100`) AllReduce multi-phase branch — an in-body branch of the AllReduce builder `@0x133c2dc0` reached from the `(~v & 0x101) == 0` discriminant at AllReduce decompile line 3142 (its `MakeErrorStream` cites `offload_collective_config.cc:1616` at line 3144): the deque-block iterator and per-phase append are confirmed, but how the intra-chip vs inter-chip phase rings differ (their `IciStrategyRingType` / neighbor) was not expanded — only AllReduce reaches it and the flag is off by default in the wrappers observed.

---

## Cross-References

**SparseCore-offload substrate**
- [On-Pod Collectives — Section Map](overview.md) — the substrate split and the SC-offload gate (§5)
- [HierarchicalKind](hierarchical-kind.md) — the `AutoOr<bool>` flat-vs-hierarchical phase split this builder dispatches on
- [SC Core-Selection (Offload)](sc-core-selection-offload.md) — `SparseCoreConfig.offload` op-type classification and core selection

**Dense counterpart**
- [SelectNDStrategy](strategy-nd-picker.md) — the dense `StrategyND` picker this builder mirrors
- [ReduceScatter](reduce-scatter.md) — the reduce-scatter phase of the dense all-reduce decomposition

**Sibling subsystems**
- [Twisted Torus](../twist/overview.md) — the twist geometry the `cmp $3` K/2K gate bridges into
- [Barriers](../barrier/overview.md) — the `barrier_id` / SFLAG binding the ring config references
- [back to index](../index.md)
