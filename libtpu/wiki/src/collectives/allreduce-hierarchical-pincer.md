# Hierarchical AllReduce / Pincer

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

The SparseCore-offload AllReduce config builder has a fork that the AllGather and ReduceScatter builders do not reach: when `HierarchicalKind` is *engaged and true*, the builder decomposes the collective into a **multi-phase ring list** instead of a single flat ring. This page owns three coupled things a reimplementer cannot recover from the op surface:

- **The `0x101` hierarchical decomposition.** `ConstructConfigForCollectiveUniDirNDGroups<AllReduceOffloadConfig, …>` (`0x133c2dc0`) reloads the 16-bit `HierarchicalKind`, computes `is_hierarchical = (kind & 0x101) != 0x100` (decompile line 806: `v637 = (v63 & 0x101) != 256`), and on the *true* arm emits an **intra-chip D2D phase ring + one inter-chip ring per torus axis** into a `PerColorIciStrategyConfig.phase_rings` list — versus the flat arm's single explicit-neighbour ring. AllGather (`0x133c82c0`) and ReduceScatter (`0x133cd800`) hard-wire the flag to `0x100`, so **only AllReduce takes this path**.
- **The `IciStrategyRingConfig` leaf layout.** The per-ring proto leaf — its 13 scalar fields pinned to byte offsets `0x18..0x53` with hasbits `0x1..0x1000` and proto field numbers — including the fields this builder *writes* (`partner_transfers_outside_the_ring` @0x3e, `core_count_adjustment` @0x40) and the three it leaves for a later runtime pass (`barrier_id` @0x30, `id_info_offset` @0x50, `group_info_table_offset` @0x4c).
- **The AllReduce pincer fusion.** The bidirectional-ring family (`RotatedPincer*` / `AsyncPincer*`) whose **reduce-scatter arm and all-gather arm are separable** — the structural opposite of the self-completing [binomial butterfly](binomial-recursive-doubling.md) — and which keeps a 2-D `[dim][color]` sflag table from the **reserved** AllReduce sflag slots (`GetAllReduceSyncFlagNumber(1)/(2)`), not the binomial table.

The single-axis binomial / recursive-doubling emitter loop and the ring offset *consumers* are documented elsewhere ([Binomial / Recursive-Doubling](binomial-recursive-doubling.md), [ICI AllReduce primitive](../ici/all-reduce-primitive.md)); this page concentrates on the *hierarchical decomposition + the ring-config field map + the pincer fusion arms*.

| | |
|---|---|
| **AllReduce builder (templated body)** | `ConstructConfigForCollectiveUniDirNDGroups<AllReduceOffloadConfig, HloAllReduceInstruction>` — `0x133c2dc0` (3826 decompiled lines) |
| **AllReduce ND wrapper** | `ConstructConfigForAllReduceUniDirND` — `0x133c2c80` |
| **Per-color phase_rings appender** | `$_` lambda(`long color`) — `0x133ddae0` (returns the appended ring; caller fills fields) |
| **Hierarchical enable gate** | `ShouldEnableSparseCoreHierarchicalAllReduce` — `0x1d6b6d80` |
| **Per-axis ring-dim / extent producer** | `GetDimensionRings` — `0x133df520` (jump table `0xae2eaac`) |
| **Ring-config leaf proto** | `IciStrategyRingConfig` — `_InternalSerialize` `0x1d6ec320`, `ByteSizeLong` `0x1d6ec700`, `MergeImpl` `0x1d6ec120`, `Clear` `0x1d6ec2c0` |
| **Pincer fusion arms** | `EmitAllReduceScatterFusion`, `EmitAllGatherFusion` (`0x1374e580`); `TpuAllReduceScatterFusion` pass |
| **Pincer sflag init** | `AsyncPincerInstance::InitSflags` `0x13782fc0`; `RotatedPincerEmitterBase::InitSyncFlags` `0x137a56a0` |
| **Reserved AllReduce sflags** | `Target::GetAllReduceSyncFlagNumber` — `0x1d60f440` (slots `+2`/`+3`) |
| **Source TU** | `platforms/xla/service/.../offload_collective_config_builder.cc` (builder + `GetDimensionRings`) |

---

## The Flat-vs-Hierarchical Dispatch (`0x101`)

The three SparseCore-offload collective builders are the same template, `ConstructConfigForCollectiveUniDirNDGroups<OffloadConfig, HloInst>`, instantiated three times. The ND wrappers differ in exactly one thing: the value of the `HierarchicalKind` argument they push.

| Collective | Builder body | `HierarchicalKind` source | Can reach hierarchical? | Confidence |
|---|---|---|---|---|
| AllReduce | `0x133c2dc0` | wrapper `0x133c2c80` pushes a real `optional<int>` gated by `ShouldEnableSparseCoreHierarchicalAllReduce` (`0x1d6b6d80`) | **yes** | Confirmed |
| AllGather | `0x133c82c0` | wrapper `0x133c76c0` pins `r9d = 0x100` | no (always flat) | Confirmed |
| ReduceScatter | `0x133cd800` | wrapper `0x133ccbe0` pins `r9d = 0x100` | no (always flat) | Confirmed |

`HierarchicalKind` is a 16-bit `AutoOr<bool>`: bit `0x100` is the *engaged* discriminant, bit `0x1` is the boolean value. The dispatch masks both bits and tests against the engaged-but-false encoding:

```c
// ConstructConfigForCollectiveUniDirNDGroups<AllReduceOffloadConfig,...> — 0x133c2dc0
// decompile line 806 (the is_hierarchical byte)
v637 = (v63 & 0x101) != 256;   // 256 == 0x100
//        ^ mask {engaged, value}     ^ 0x100 == engaged + value-false
// is_hierarchical = (kind & 0x101) != 0x100
//   0x100 (engaged, false) -> FLAT     ; v637 = 0
//   0x101 (engaged, true)  -> HIER     ; v637 = 1
```

Later, `if (v637) goto hier; else goto flat;` selects the inter-chip ring shape. The same predicate, byte-for-byte, appears in the AllGather body as a direct `cmp $0x100` against a stack slot — but AG/RS can never satisfy it because their wrapper pins the kind to `0x100`.

> **NOTE —** the bit-`0x100` / bit-`0x1` `AutoOr<bool>` encoding is the same engaged-discriminant pattern used across the offload-config plumbing; see [HierarchicalKind](hierarchical-kind.md) for the `*OffloadConfig` struct layout that carries it.

---

## The Multi-Phase Decomposition

Each `PerColorIciStrategyConfig.phase_rings` is an **ordered list** of `IciStrategyRingConfig` leaves — the proto field name `phase_rings` is itself the confirmation that this is a multi-phase structure, not a single ring. The builder appends rings in phase order via the per-color appender lambda (`0x133ddae0`), which `Add`s a fresh `IciStrategyRingConfig` to the right `PerColorIciStrategyConfig` and returns the pointer; **the caller then fills the ring's fields**.

```text
PerColorIciStrategyConfig (one per color)
  phase_rings:  [ Phase 0: D2D intra-chip ]   <- emitted first, megacore-gated
                [ Phase 1: torus axis 0   ]   \
                [ Phase 2: torus axis 1   ]    >  one ring PER axis (hierarchical)
                [ ...                     ]   /   OR one collapsed ring (flat)
```

### Phase 0 — the D2D intra-chip ring (both arms, megacore-gated)

When the chip carries more than one logical core (the megacore / across-cores byte, `test $0x1,%bl`), the builder prepends one **device-to-device** ring per color. Its fields are identical on both the flat and hierarchical arms — the intra-chip phase is shared; only the *inter-chip* phase differs.

```c
// D2D phase emission — 0x133c2dc0, decompile lines 3263-3265
*(_DWORD *)(v103 + 36) = 2;    // ring_neighbor   = ICI_RING_NEIGHBOR_IMPLICIT (2)  @0x24
*(_DWORD *)(v103 + 56) = 7;    // ring_dim        = ICI_RING_DIM_D2D          (7)  @0x38
*(_BYTE  *)(v103 + 60) = 1;    // across_cores_on_chip = true                       @0x3c
// hasbits |= 0x64  (ring_neighbor | ring_dim | across_cores)
```

### Phase 1..N — the inter-chip ring(s)

The per-axis outer loop walks a deque of ND-plane axes (the `170 * (… >> 3)` / `/ 0xAA` block-iterator over the `0xaa`-byte tuple blocks). Each tuple yields `(ringDim, lo, hi)`; the builder computes the ring length (an `idiv` by device count, multiplied by logical-devices-per-chip when megacore) and then takes the flat or hierarchical block.

**FLAT block** (one collapsed EXPLICIT-neighbour ring, taken when `is_hierarchical == 0`):

```c
// FLAT inter-chip ring — 0x133c2dc0, decompile lines 3548-3559
*(_QWORD *)(v114 + 24) = v119;          // core_count = computed ring length   @0x18
*(_DWORD *)(v114 + 36) = 1;             // ring_neighbor = ICI_RING_NEIGHBOR_EXPLICIT (1) @0x24
// --- flat_map<IciStrategyRingDim, RingConfigAttributes>::operator[](ringDim) (0x133ddc60) ---
*(_QWORD *)(v114 + 40) = *(_QWORD *)v108;       // ring_neighbor_table_offset = RCA[+0]  @0x28
*(_BYTE  *)(v114 + 61) = *(_BYTE *)(v108 + 8);  // has_reordering_map         = RCA[+8]  @0x3d
*(_DWORD *)(v114 + 72) = v107;          // explicit_strategy_ring_dim = ringDim          @0x48
*(_BYTE  *)(v114 + 62) = 0;             // partner_transfers_outside_the_ring = false    @0x3e
// hasbits |= core_count | ring_neighbor | table_offset | has_reordering | explicit_dim | partner
```

**HIER block** (one IMPLICIT-neighbour ring *per axis*, taken when `is_hierarchical == 1`):

```c
// HIERARCHICAL inter-chip ring — 0x133c2dc0, decompile lines 3531-3537 + 3564 + 3621
*(_DWORD *)(v114 + 36) = 2;             // ring_neighbor = ICI_RING_NEIGHBOR_IMPLICIT (2)  @0x24
*(_DWORD *)(v114 + 56) = v118;          // ring_dim = the per-axis ringDim (X/Y/Z torus|mesh) @0x38
if (across_cores_first_axis)            //   only on axis 0 when megacore
    *(_BYTE *)(v114 + 60) = 1;          // across_cores_on_chip = true                    @0x3c
*(_BYTE  *)(v114 + 62) = 0;             // partner_transfers_outside_the_ring = false     @0x3e
if (logical_devices_per_chip >= 2 && hi > 0)        // megacore adjust
    *(_QWORD *)(v143 + 64) = hi;        // core_count_adjustment = hi                     @0x40
// hasbits |= ring_neighbor | ring_dim [| across_cores] [| core_count_adjustment 0x200]
```

The decisive divergence: the flat arm sets `core_count` (the ring length) **directly** and carries a precomputed `ring_neighbor_table_offset` (a flat-map lookup keyed by `IciStrategyRingDim`); the hierarchical arm sets **no** explicit length and **no** neighbour table — it carries the `ring_dim` per axis and lets the consumer derive the length from `ring_dim` plus the megacore `core_count_adjustment`. The hierarchical decomposition is the SparseCore analog of the TensorCore reduce-scatter / all-gather phase split: a phase per *torus dimension* instead of a single collapsed ring.

| Aspect | FLAT (`kind & 0x101 == 0x100`) | HIERARCHICAL (`kind & 0x101 == 0x101`) | Confidence |
|---|---|---|---|
| `phase_rings` per color | `[D2D?] +` **one** explicit ring | `[D2D?] +` **one ring per torus axis** | Confirmed |
| inter-chip neighbour | `ICI_RING_NEIGHBOR_EXPLICIT` (1) | `ICI_RING_NEIGHBOR_IMPLICIT` (2) | Confirmed |
| ring length carried | `core_count` (`0x18`) set directly | implicit; `core_count_adjustment` (`0x40`) megacore delta | Confirmed |
| neighbour table | `ring_neighbor_table_offset` (`0x28`) + `has_reordering_map` (`0x3d`) | none (implicit ordering) | Confirmed |
| ring-dim field | `explicit_strategy_ring_dim` (`0x48`) | `ring_dim` (`0x38`) per axis | Confirmed |
| D2D intra-chip ring | emitted if megacore (Phase 0) | emitted if megacore (Phase 0) | Confirmed |
| reached by | AG/RS (pinned) + AR (flag off) | AllReduce only (flag engaged + true) | Confirmed |

> **NOTE —** the per-axis `ringDim` is `2 - (NDPlaneInfo[+0xa0] & 1)`, i.e. `ICI_RING_DIM_X_TORUS` (1) or `ICI_RING_DIM_X_MESH` (2) per the low parity bit (and the Y/Z analogs via the deque tuple). The flat arm instead routes through `GetDimensionRings` (`0x133df520`), whose 7-entry jump table at `0xae2eaac` maps each `IciStrategyRingDim` to a chip torus extent (`X=0x58`, `Y=0x5c`, `Z=0x60`) and a torus-vs-mesh flag, then divides the extent by device count (megacore-aware) to fill the `RingConfigAttributes`. What physical property the `[+0xa0]` parity bit encodes is not pinned (LOW) — see [Tensor-split ND-plane](tensor-split-ndplane.md).

---

## The `IciStrategyRingConfig` Field Map

The per-ring leaf is a standard proto2 message: vptr at `+0x00`, `InternalMetadata` at `+0x08`, the hasbits `int32` at `+0x10`, then the scalars packed `0x18..0x53`. Every `{offset, width, hasbit, number}` triple **agrees across three independently generated methods** — `_InternalSerialize` (`0x1d6ec320`), `ByteSizeLong` (`0x1d6ec700`), and `MergeImpl` (`0x1d6ec120`) — and the names/numbers come from the serialized `FieldDescriptorProto`.

| # | Field | Proto type | Offset | Width | Hasbit | Wire tag | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | `ring_type` | enum | `0x20` | 4 | `0x0002` | `0x08` | Confirmed |
| 2 | `core_count` | int64 | `0x18` | 8 | `0x0001` | `0x10` | Confirmed |
| 3 | `ring_neighbor` | enum | `0x24` | 4 | `0x0004` | `0x18` | Confirmed |
| 4 | `ring_dim` | enum | `0x38` | 4 | `0x0020` | `0x20` | Confirmed |
| 5 | `ring_neighbor_table_offset` | int64 | `0x28` | 8 | `0x0008` | `0x28` | Confirmed |
| 6 | `barrier_id` | int64 | `0x30` | 8 | `0x0010` | `0x30` | Confirmed |
| 7 | `across_cores_on_chip` | bool | `0x3c` | 1 | `0x0040` | `0x38` | Confirmed |
| 8 | `has_reordering_map` | bool | `0x3d` | 1 | `0x0080` | `0x40` | Confirmed |
| 9 | `explicit_strategy_ring_dim` | enum | `0x48` | 4 | `0x0400` | `0x48` | Confirmed |
| 10 | `core_count_adjustment` | int64 | `0x40` | 8 | `0x0200` | `0x50` | Confirmed |
| 11 | `partner_transfers_outside_the_ring` | bool | `0x3e` | 1 | `0x0100` | `0x58` | Confirmed |
| 12 | `id_info_offset` | int64 | `0x50` | 8 | `0x1000` | `0x60` | Confirmed |
| 13 | `group_info_table_offset` | int32 | `0x4c` | 4 | `0x0800` | `0x68` | Confirmed |

The serializer reads exactly these word offsets — e.g. `WriteInt64ToArrayWithField<2>(… *((_QWORD*)this + 3) …)` reads byte `0x18` (`core_count`), `<5>` reads `+5`=`0x28` (`ring_neighbor_table_offset`), `<6>` reads `+6`=`0x30` (`barrier_id`), `<10>` reads `+8`=`0x40` (`core_count_adjustment`), `<12>` reads `+10`=`0x50` (`id_info_offset`), and `WriteInt32<13>` reads `+19`=`0x4c` (`group_info_table_offset`); the three bool bytes are read at `60`/`61`/`62` (`0x3c`/`0x3d`/`0x3e`). `Clear` (`0x1d6ec2c0`) zeroes the block with `vmovups %ymm0, 0x18` (`0x18..0x37`) + `movq $0, 0x36`, then `vmovups %xmm0, 0x48` + `vmovups %xmm0, 0x3e`, and resets the hasbits — a tight match for the packed layout.

### The three ring enums (value numbers, byte-exact from the `EnumDescriptorProto`)

| `IciStrategyRingType` | # | | `IciStrategyRingNeighbor` | # | | `IciStrategyRingDim` | # |
|---|---|---|---|---|---|---|---|
| `INVALID_RING_TYPE` | 0 | | `NEIGHBOR_INVALID` | 0 | | `RING_DIM_INVALID` | 0 |
| `BIDIR` | 1 | | `NEIGHBOR_EXPLICIT` | 1 | | `X_TORUS` | 1 |
| `UNIDIR_CW` | 2 | | `NEIGHBOR_IMPLICIT` | 2 | | `X_MESH` | 2 |
| `UNIDIR_CCW` | 3 | | | | | `Y_TORUS` | 3 |
| `UNIDIR_ALL_TO_ALL_CW` | 4 | | | | | `Y_MESH` | 4 |
| `UNIDIR_ALL_TO_ALL_CCW` | 5 | | | | | `Z_TORUS` | 5 |
| | | | | | | `Z_MESH` | 6 |
| | | | | | | `D2D` | 7 |

So the emission constants decode exactly: flat `ring_neighbor = 1 = EXPLICIT`, hierarchical / D2D `ring_neighbor = 2 = IMPLICIT`, D2D `ring_dim = 7`, per-axis `ring_dim ∈ {1 X_TORUS, 2 X_MESH}`.

> **NOTE —** the standard per-axis / D2D emission **does not** write `ring_type` (field 1, `0x20`) — it is left at the appender lambda's stamp / proto default. The explicit `UNIDIR_CW` (2) / `UNIDIR_CCW` (3) constants are written only in a separate tensor-split / steppable emission region (the `*(_DWORD*)(… + 56) = 2*v490 + 1` ring-type stamp and the `movl $0x2/$0x1, 0x20` writes), which serves a different generation option. Whether a downstream pass rewrites `ring_type` per axis — and how CW vs CCW is chosen per phase — is not traced here (LOW).

> **NOTE —** three confirmed fields are **not** written by the builder decoded here: `barrier_id` (`0x30`), `id_info_offset` (`0x50`), and `group_info_table_offset` (`0x4c`). Their offsets and hasbits are byte-confirmed in all three proto methods, but the populator is a separate runtime barrier / id-allocation pass that consumes the proto. Where they are set is unresolved (LOW). Likewise the `RingConfigAttributes` POD: the flat path reads only `+0x0` (`ring_neighbor_table_offset`) and `+0x8` (`has_reordering_map`); the `0x10..0x17` slot is copied by the flat-map machinery but unread by the AllReduce flat emission (LOW — decode via the AG/RS flat `core_count` fill).

---

## The AllReduce Pincer Fusion

The pincer is a different emitter family from the hierarchical config builder above, and a different one from the [binomial butterfly](binomial-recursive-doubling.md). It is the **bidirectional-ring** AllReduce whose distinguishing feature — for a reimplementer choosing where to fuse surrounding compute — is that its **reduce-scatter arm and all-gather arm are separable**. The binomial butterfly is self-completing (every step both sends and reduces, no separable scatter or gather phase); the pincer instead exposes the two halves as distinct fusable arms, which is what lets surrounding matmul tiles be windowed *into* the collective (windowed-einsum).

### The two fusion arms

`MayFuseAllReduce` (`0x127acfc0`) decides whether an `HloAllReduceInstruction` is fused into the pincer form; the `TpuAllReduceScatterFusion` HLO pass (`GetFusionSpec` `0x127a8d60`, `MaybeGetAllReduceScatterLayout` `0x127a8b60`) handles the reduce-scatter side. At lowering time the arms are emitted by `EmitAllReduceScatterFusion` and `EmitAllGatherFusion` (the `EmitAllGatherFusion` thunk at `0x1374e580` takes a `Span<ShardingConfig>`, an `InfoTable`, and a `std::function` provider) through the `AsyncPincerFusionEmitter` / `RotatedPincerEmitter` hierarchy.

| | Binomial butterfly | Pincer fusion | Confidence |
|---|---|---|---|
| Emitter | `BinomialSinglePhaseRingSumEmitter` (`0x13769be0`) | `RotatedPincerEmitter` / `AsyncPincerFusionEmitter` | Confirmed |
| Reduce arm / broadcast arm | fused into one traversal (not separable) | **separable** — RS arm and AG arm exposed for windowing | Confirmed |
| Fusion arm emitters | — | `EmitAllReduceScatterFusion` / `EmitAllGatherFusion` (`0x1374e580`) | Confirmed |
| Schedule source | binomial replica table (ConstantMapper Type 7) | `net_util::GetRingLocation` (no precomputed table) | Confirmed |
| Sflags | 7 general recv sflags | **reserved** AllReduce slots (`GetAllReduceSyncFlagNumber`) | Confirmed |
| Directions / step | one (butterfly partner) | two (rotated CW + counter-rotated CCW) | Confirmed |
| Fits when | small power-of-2 ring, latency-bound | large ring, bandwidth-bound, windowed-einsum overlap | Inferred |

### The pincer sflag table — the bidirectionality smoking-gun

The pincer runs the bandwidth-optimal ring rotation in **both directions per step** — each direction covering half the ring, `⌈N/2⌉` steps — and so it keeps **separate sync flags** for the rotated and counter-rotated shards. `RotatedPincerEmitterBase::InitSyncFlags` (`0x137a56a0`) draws those flags from the two *reserved* AllReduce slots and walks a nested `[dim][color]` loop:

```c
// RotatedPincerEmitterBase::InitSyncFlags — 0x137a56a0, decompile lines 155/176/196
AllReduceSyncFlagNumber = Target::GetAllReduceSyncFlagNumber(v25, 1);   // direction-1 slot (base+count+2)
v29                     = Target::GetAllReduceSyncFlagNumber(v28, 2);   // direction-2 slot (base+count+3)
while (1) { ... }   // per (dim, color): install one flag pair
```

A full `.text` cross-reference of `GetAllReduceSyncFlagNumber` (`0x1d60f440`) shows its **only** callers are `AsyncPincerInstance::InitSflags` (`0x13782fc0`) and `RotatedPincer*::InitSyncFlags` (`0x137a56a0`) — **none** in the binomial emitter, and none in the hierarchical config builder. This is the three-topology split:

- **Binomial butterfly** — `CreateStaticBinomialReplicaInfoTable` precomputed `int32[N×8]` partner schedule, 7 general recv sflags ([Binomial / Recursive-Doubling](binomial-recursive-doubling.md)).
- **Ring rotation** — `net_util::GetRingLocation{ring_index, position, ring_size}`, no precomputed table.
- **Pincer bidirectional** — `net_util::GetRingLocation` **plus** the reserved sflag slots (`GetAllReduceSyncFlagNumber(1)/(2)`) and a 2-D `[dim][color]` flag table.

> **NOTE —** the picker's choice between binomial / ring / pincer is a correctness-and-performance fork, not a quality knob. Binomial is *only* legal for power-of-2 `N ≤ 128`; the pincer is selected for large bandwidth-bound rings and windowed-einsum overlap. A reimplementer that fuses a producer into a "binomial reduce-scatter arm" has misread the topology — that arm does not exist in the butterfly; it exists only in the pincer fusion. The megacore split that shares each pincer arm across the two TensorCore cores is in [Megacore Fusion](megacore-fusion.md).

---

## Reimplementation Checklist

- **Dispatch:** reload the 16-bit `HierarchicalKind`; `is_hierarchical = (kind & 0x101) != 0x100`. Pin the flag to `0x100` for AllGather and ReduceScatter — only AllReduce may take the hierarchical arm.
- **Phase list:** build `phase_rings` as an ordered list per color. Prepend the D2D ring (`ring_neighbor=IMPLICIT`, `ring_dim=D2D(7)`, `across_cores_on_chip=true`) only when the chip is megacore.
- **Flat inter-chip ring:** one ring — `core_count` = computed length, `ring_neighbor=EXPLICIT(1)`, `ring_neighbor_table_offset` + `has_reordering_map` from `RingConfigAttributes[ringDim]`, `explicit_strategy_ring_dim=ringDim`, `partner_transfers=false`.
- **Hierarchical inter-chip rings:** one ring **per torus axis** — `ring_neighbor=IMPLICIT(2)`, `ring_dim=ringDim` (X/Y/Z torus|mesh), `across_cores_on_chip` only on the first axis when megacore, `core_count_adjustment=hi` when `logical_devices_per_chip ≥ 2`. No `core_count`, no neighbour table.
- **Leaf layout:** lay `IciStrategyRingConfig` out with the 13 fields at the byte offsets in the field-map table; pack the three bools at `0x3c/0x3d/0x3e`, the enums at `0x20/0x24/0x38/0x48/0x4c`, the int64s at `0x18/0x28/0x30/0x40/0x50`; hasbits int32 at `0x10`.
- **Pincer:** for the bidirectional fusion, draw the two direction sflags from `GetAllReduceSyncFlagNumber(1)/(2)` and key a `[dim][color]` flag table; keep the reduce-scatter and all-gather arms separable for windowed-einsum overlap. Do **not** route the pincer through the binomial replica table.

---

## Cross-References

- [Collectives Overview](overview.md) — the family taxonomy and the strategy picker that selects flat vs. hierarchical, binomial vs. ring vs. pincer.
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — the single-axis butterfly emitter loop (the self-completing topology this page contrasts the pincer against).
- [SelectNDStrategy](strategy-nd-picker.md) — the per-axis picker that chooses the ring shape and the degraded-axis handling.
- [HierarchicalKind](hierarchical-kind.md) — the `AllGather/AllReduce/ReduceScatter OffloadConfig` structs that carry the `0x100`/`0x101` `AutoOr<bool>` flag.
- [Megacore Fusion](megacore-fusion.md) — how the reduce-arm / broadcast-arm fuse across the two TensorCore cores.
- [Tensor-split ND-plane](tensor-split-ndplane.md) — the `NDPlaneInfo` parity word that selects per-axis `X_TORUS` vs `X_MESH`.
- [ICI All-Reduce Primitive](../ici/all-reduce-primitive.md) — the shared step-generation primitive (`ReduceShardInPlace`, `EnqueueDmaInGranules`, the sflag handshake) the per-ring consumers run.
- [Twist Overview](../twist/overview.md) — the twisted-torus topology this builder gates into after the ring-config emission.
