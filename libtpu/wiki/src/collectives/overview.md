# On-Pod Collectives — Section Map

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`).
> **Status:** Reimplementation-grade map · **Evidence grade:** Confirmed (byte-anchored), substrate split / end-to-end flow / op-family dispatch all cross-checked against the IDA decompile · **Part XIII — On-Pod Collectives & Barriers** / Collective algorithms · [back to index](../index.md)

## Abstract

This page is the **map of the TPU collective stack** as reconstructed from the stripped (in fact full-symbol) `libtpu.so`. A collective in an XLA/HLO module — `all-reduce`, `all-gather`, `reduce-scatter`, `all-to-all`, `collective-permute` and their async/ragged variants — is lowered to ICI (inter-chip-interconnect) ring traffic over the physical torus. The compiler does this on **two distinct execution substrates**: the dense **TensorCore** path that drives ICI DMA directly, and the **SparseCore-offload** path that hands embedding-class collectives to the SparseCore as a separate operating point. This page documents (1) the substrate split and what gates SC-offload, (2) the end-to-end flow from HLO collective op to emitted ICI ring schedule, and (3) the collective op family with its per-kind cost/emitter dispatch. Each algorithm, the routing/twist/barrier subsystems, and the SC-offload config builder are **sibling pages** — this page links them; it does not duplicate their byte-level derivations.

Contract of the collective stack as observed in the binary:

- Every collective is reduced over the **physical torus** (3 dimensions X/Y/Z), not over the logical replica list directly: the partitioner and the cost model both query topology (`ReplicaGroupsOnNDPlane`, `EstimatePhysicalLinksUsed`) to derive how many torus dimensions the collective's replica-groups span.
- **Strategy selection is flag-and-shape driven, not cost-compared.** `BaseStrategyND::SelectNDStrategy` @`0x137c78e0` picks the emitter family (sub-plane / ND-ring / N-Way / twisted-torus / strided) from `TpuCompEnv` flags + torus extents; the cost model (`CostModel::GetCollectiveCycles` @`0x130abfc0`) only produces the scheduler's per-resource cycle deposit. The one true cost-vs-cost comparison is the SPMD partitioner's `GetCommunicationTimeInMilliSec`, used to choose **sharding**, not the emitter algorithm.
- **ICI bandwidth is modeled per-direction (bidirectional ring):** the shared term is `eff_Bps = IciGigabytesPerSecond() · 0.5 · 1e9`; there is **no additive latency term** in any collective branch — bundle collective cost is pure bandwidth, in TensorCore cycles.
- **SC-offload is gated by a Target capability bit + a platform-type bool**, with a per-generation hardware basis (`TpuVersion == 5`); when the gate holds, the embedding collective is emitted as a `CollectiveIciStrategyConfig` proto of per-color UNIDIR rings rather than as HLO ReplicaGroup device lists.

## At a glance

| Aspect | TensorCore (dense) substrate | SparseCore-offload substrate |
|--------|------------------------------|------------------------------|
| Emitter selector | `BaseStrategyND::SelectNDStrategy` @`0x137c78e0` | `ConstructConfigForCollectiveUniDirNDGroups<*>` @`0x133c82c0` / `0x133c2dc0` / `0x133cd800` |
| Output of selection | heap `StrategyND*` family → HLO ReplicaGroup device lists | `CollectiveIciStrategyConfig` proto (per-color UNIDIR rings) |
| Cost estimator | `CostModel::GetCollectiveCycles` @`0x130abfc0` (TC cycles) | SC ring cost via `GetCollectiveOffloadConfig` @`0x133e1740` probe |
| Async tracker / scheduler resources | jellyfish `TpuAsyncTracker` `{13..46}` | plain `SparseCoreAsyncTracker` (base `{0..12}`) → resource-aware `{13..17}` |
| Gate | always (dense XLA path) | `Megachip ∧ CoresPerChip(SC)>0 ∧ (Target[+0x628]&4 ∨ Target[+0x540]) ∧ ModuleContainsLEM… ∧ FLAGS_xla_sc_enable_latency_hiding_scheduler` |
| Op-type key | HLO opcode + `SparseCoreConfig.offload` enum | `SparseCoreConfig.offload` (field 2, `xla::jellyfish::Offload`) |

---

## 1. The two execution substrates

The binary realizes on-pod collectives through two parallel lowerings that share the same physical torus but differ in who drives the ICI DMA and how the schedule is built.

### 1.1 TensorCore-driven ICI collectives (dense)

The dense path is the default. The XLA collective op survives into the jellyfish backend, where `BaseStrategyND::SelectNDStrategy` selects an emitter strategy and the cost model charges ICI cycles into a `ResourceVector`. The strategy object (a `StrategyND` subclass) produces the per-color ring decomposition — sequences of HLO `ReplicaGroup` device lists — that the collective emitters turn into ICI DMA descriptors. The TensorCore issues the ring transfers; the cost the scheduler sees is the per-torus-dimension ICI ring cost.

Confirmed in the decompile: `SelectNDStrategy` constructs `StrategySubgroupND`, `StrategyND` (the umbrella 1D/ND-ring class, also used for the N-Way and strided variants), and `TwistedTorusND`, gated by `IsGroupNDPlane`, `UseSpecialStrategyNDNWay`, `UseStridedStrategyND`, and a single-ND-plane test via `ReplicaGroupsOnNDPlane(…, plane=2, …)`. The terminal classes are detailed in **[SelectNDStrategy](strategy-nd-picker.md)**.

### 1.2 SparseCore-offloaded collectives

Embedding-class collectives (the gradient all-reduce / all-gather / reduce-scatter that arise from sparse embedding lookups) can be **offloaded** to the SparseCore. Instead of emitting HLO ReplicaGroup lists, the SC path builds a `CollectiveIciStrategyConfig` proto — a per-color set of UNIDIR rings (`ICI_RING_TYPE_UNIDIR_CW` / `_CCW`) over the same X/Y/Z torus extents — embedded inside an `AllGatherOffloadConfig` / `AllReduceOffloadConfig` / `ReduceScatterOffloadConfig` backend-config message (sizeof `0x48`, byte-identical layout). The cost model, when it sees an offloaded collective, probes `GetCollectiveOffloadConfig` @`0x133e1740` and charges the SC ring operating point rather than the dense TC one.

This substrate runs its own latency-hiding scheduler with **two trackers in sequence**: the plain `SparseCoreAsyncTracker` (vtable @`0x2190da10`) — which has **no target-defined resources** and only throttles the base XLA collective resources `{0..12}`, classifying by opcode + the custom-call target name `"AllToAllDynamic"` — followed by the resource-aware `SparseCoreResourceAwareAsyncTracker` (vtable @`0x2190e1b0`) carrying the `{13..17}` = `{SCS, SCT, ICI, LocalReduction, 2DAllToAll}` resource caps `{1, 20, 5, 1, 1}`.

The SC config builder is the SparseCore analog of the TC `StrategyND::BuildStrategy`; it is fully documented in **[SC-Offload Config Builder](sc-offload-config-builder.md)**, its phase-split flag in **[HierarchicalKind](hierarchical-kind.md)**, and its core selection in **[SC Core-Selection (Offload)](sc-core-selection-offload.md)**.

### 1.3 Shared substrate: the physical-torus mesh decomposition

Both substrates reduce over the **same physical torus** and share the topology-derivation machinery — this is the glue that keeps the dense and offload cost/dimension models consistent. A collective's replica-groups are mapped onto the torus and reduced to a per-dimension mesh descriptor:

- **`ReplicaGroupsOnNDPlane`** @`0x1c890960` (memoized on an `NDPlaneCacheKey → vector<MeshNDInfo>`) decomposes the replica-groups onto the torus via `TensorCoreLocationForLogicalDeviceId` → `TpuCoreLocation::Chip()` (physical chip coordinates) and reports how many torus mesh dimensions the groups span. Both the SPMD partitioner (the link-count divisor, §3) and the dense picker (the single-ND-plane test, §4) call it with `plane = 2`.
- **`EstimatePhysicalLinksUsed`** @`0x1c8939c0` walks the same chip coordinates to count the physical ICI links a collective uses — the divisor for the all-to-all / ragged / cross-module-all-reduce cost branches.
- The torus extents X/Y/Z are read at the same chip-config offsets (`[chip_cfg+0x58]` / `+0x5c` / `+0x60`) by the dense picker, the cost model, and the SC `GetDimensionRings` @`0x133df520` — so the dense `StrategyND` ring dims and the SC `IciStrategyRingDim` ring dims index the identical hardware geometry.

The per-dimension ICI resource map is shared too: `GetResourceFromIciResource` @`0x1c894c00` maps `IciResource ∈ [1..6]` to `ResourceVector` slots `{0xd,0xe | 0xf,0x10 | 0x11,0x12}` = 3 torus dimensions (Y, X, Z) × 2 ring directions (±). The degraded-axis remap demotes a failed axis's two slots out of the primary ring (see [Degraded-Axis Ingest](degraded-axis.md)).

---

## 2. End-to-end flow

The lowering of one collective op, from HLO to emitted ICI traffic, proceeds through the stages below. Stages 2–4 are the dense TC path; the SC-offload path forks at stage 2 (gate) and replaces stages 3–5 with the config builder of §1.2.

```text
HLO collective op  (all-reduce / all-gather / reduce-scatter / all-to-all / collective-permute)
        │
   [1]  classify opcode → IsNonFusionCollective; read SparseCoreConfig.offload (field 2)
        │
   [2]  SUBSTRATE GATE
        │   SC-offload? (Megachip ∧ CoresPerChip(SC)>0 ∧ (Target[+0x628]&4 ∨ Target[+0x540])
        │                ∧ ModuleContainsLEMSparseCoreInstruction ∧ FLAGS_xla_sc_enable_lhs)
        ├── yes ─────────────► SC-OFFLOAD: ConstructConfigForCollectiveUniDirNDGroups<*>
        │                       → CollectiveIciStrategyConfig (per-color UNIDIR rings)
        │                       → SparseCore latency-hiding schedule
        │
        └── no  (dense TensorCore path)
                 │
            [3]  STRATEGY SELECTION   BaseStrategyND::SelectNDStrategy @0x137c78e0
                 │   entry fold: is_cross_module &= hlo->IsCrossReplicaAllReduce()
                 │   sub-plane? ND-plane? N-Way? twisted-torus? strided? → subgroup default
                 │   (degraded-axis remap folds in here via ComputeColorDimensions)
                 │
            [4]  ND-STRATEGY / TOPOLOGY
                 │   ReplicaGroupsOnNDPlane(plane=2) → mesh-dim count over physical torus
                 │   ComputeColorDimensions → [6][3] per-color ring-dimension table
                 │
            [5]  ROUTE-TABLE GENERATION  (per-color RingLocation neighbor schedule)
                 │
            [6]  BARRIER / SYNC          (replica/TensorCore barrier, SFLAG binding)
                 │
            [7]  ICI DMA EMISSION        (per-torus-dimension ring DMA descriptors)
```

The cost model (`CostModel::GetCollectiveCycles` @`0x130abfc0`) runs orthogonally to stages 3–7: it consumes the same topology dimension count that stage 4 derives, and deposits per-torus-dimension cycle estimates into the scheduler's `ResourceVector`. It does **not** select the emitter algorithm. Routing, twist geometry, and barriers are sibling sections — see **[Routing](../routing/overview.md)**, **[Twisted Torus](../twist/overview.md)**, **[Barriers](../barrier/overview.md)**, and the **[ICI fabric](../ici/overview.md)** for the DMA layer.

### 2.1 Scheduler resource spaces

Stages 6–7 are governed by a latency-hiding scheduler whose async tracker classifies each collective into resource slots and enforces concurrency caps. Three trackers exist, installed on different substrates within the same compile:

| Tracker (vtable) | Substrate | `GetResourcesFromInstructionImpl` | Resource space | Classifier key |
|------------------|-----------|-----------------------------------|----------------|----------------|
| jellyfish `TpuAsyncTracker` | TensorCore LHS | @`0x11001040` (own + 6 `MayAdd*` helpers) | `{13..46}` | opcode + `SparseCoreConfig.offload` + collective_id |
| `SparseCoreAsyncTracker` @`0x2190da10` | plain SC LHS | @`0x136122a0` (**base** opcode→rt only) | base `{0..12}` (no target resources) | opcode `{0xc, 0x10/0x11, 0x31}` + custom-call target `"AllToAllDynamic"` |
| `SparseCoreResourceAwareAsyncTracker` @`0x2190e1b0` | cost-model SC LHS | @`0x134a7580` (own jump table) | `{13..17}` = `{1, 20, 5, 1, 1}` | opcode−3 jump → SCS / SCT / ICI / LocalReduction / 2DAllToAll |

When the SC-offload gate (§5) holds, the plain `SparseCoreAsyncTracker` runs **first** (base-resource throttle + the `FindNearestAllToAlls` all-to-all post-process), then the resource-aware tracker refines with the `{13..17}` caps. The plain tracker is the surprising one: it defines **no** target resources and overrides only `IsSupportedAsyncStart`/`Done` (@`0x134964c0` / `0x13496520`) and `PostProcessScheduleGraph` — its async-schedulable set is opcode all-to-all (`0xc`), async start/done (`0x11`/`0x10`), and custom-call (`0x31`) iff the target name maps to `SparseCoreOperationType == 8` (`"AllToAllDynamic"`).

---

## 3. The collective op family

The HLO opcode integers below were length-verified via the `HloOpcodeString` table and confirmed in the `GetCollectiveCycles` jump table (decompile cases `6/8`, `9/11`, `12`, `34/36`, `86`, `93`). Each data-carrying opcode routes to a per-kind cost branch; the async shells (`-start`/`-done`) and `collective-broadcast` contribute **zero** ICI bundle cost — the cost is charged on the data-carrying opcode.

| Collective (opcode) | Cost branch (`GetCollectiveCycles`) | Dense emitter / strategy | Per-page |
|---------------------|-------------------------------------|--------------------------|----------|
| `all-gather` (6), `all-gather-start` (8) | AllGather branch @`0x130ac06c` (1D ÷2, 2D ÷4) | `StrategyND` ND-ring (`UseAllGather2D`) | [AllGather ND-Ring](allgather-nd-ring.md) |
| `all-reduce` (9), `all-reduce-start` (11) | AllReduce ND-plane branch @`0x130ac14c` (÷`2·num_dims`) | sub-plane / hierarchical / pincer / binomial | [Hierarchical / Pincer](allreduce-hierarchical-pincer.md), [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) |
| `reduce-scatter` (93) | AllReduce-family path (RS phase, ÷`2·num_dims`) | RS phase of the AR decomposition | [ReduceScatter](reduce-scatter.md) |
| `all-to-all` (12) | `ComputeAllToAllCycles` @`0x130ae8e0` (÷`EstimatePhysicalLinksUsed`) | all-link saturating | [AllToAll Tables](alltoall-tables.md) |
| `ragged-all-to-all` (86) | `ComputeRaggedAllToAllCycles` @`0x130aea80` (shares A2A helper) | ragged A2A | [AllToAll Tables](alltoall-tables.md) |
| `collective-permute` (34), `-start` (36) | CollectivePermute branch @`0x130ac40f` (÷1, single-link) | point-to-point | (cost: [SPMD Link-Count Cost](spmd-link-count-cost.md)) |
| `*-done` (7/10/35), `collective-broadcast` (33), `cp-done` | default @`0x130ae546` | — | 0 cycles |

Notes on the cost shape (full per-kind formulas live in [SPMD Link-Count Cost](spmd-link-count-cost.md)):

- The all-reduce ND-plane branch charges `B = 2 · operand_size` (reduce-scatter + all-gather phases) over `num_dims = popcnt(active torus axes)`, depositing into the two ICI slots of each active dimension.
- All-to-all / ragged-all-to-all divide by `EstimatePhysicalLinksUsed` and a `{1D→2.0, 2D→4.0}` per-link table, saturating all six ICI slots `0xd..0x12`.
- Collective-permute is the single point-to-point case (`÷1`, no `×2` bidirectional factor); `AllPairsUseSameIciLink` narrows the deposit to one ICI resource when every `(src,dst)` pair rides the same link.

The cost-vs-cost decision that **does** happen is in the SPMD partitioner: `GetCommunicationMultiplier` @`0x127a16c0` returns `ReplicaGroupsOnNDPlane(plane=2).num_mesh_dims + 1` as the link-count divisor (confirmed in the decompile: `return (unsigned int)v7 + 1` after the `plane=2` query, with the `GetMultiSliceTopology` fork to the inter-slice rate). See [SPMD Link-Count Cost](spmd-link-count-cost.md).

---

## 4. Strategy selection (dense)

`BaseStrategyND::SelectNDStrategy` is the dense-substrate picker. It splits on the `enable_sub_plane` argument, then on topology and `TpuCompEnv` flags, producing one of five terminal strategy classes. The decision is **predicate-and-flag driven**; the table below summarizes the branch order (full derivation, guard predicates, VLOG names, and object sizes in [SelectNDStrategy](strategy-nd-picker.md)).

| Order | Guard (summary) | Strategy built | VLOG name |
|-------|-----------------|----------------|-----------|
| A | `enable_sub_plane ∧ all-reduce ∧ !cross_module ∧ env[0xe1f] ∧ single ND-plane` | `StrategySubgroupND` (0x638) | "Enabling ND sub-plane allreduce" |
| B | `!enable_sub_plane ∧ IsGroupNDPlane ∧ env[0x1015]` | `StrategyND` ND-ring (0x5f0) | "Enabling 2-D algorithm …" |
| C-i | `cross_module ∧ UseSpecialStrategyNDNWay` (single-slice, 2-/4-way) | `StrategyND` N-Way | "Enable Strategy NDNway" |
| C-ii | single-module ∧ twisted-torus shape (`2·a == dim`) | `TwistedTorusND` (0x610) | "AllReduceEmitter: Choosing twisted topology" |
| C-iii | `UseStridedStrategyND` (single-slice, NumNetDims==3, LDPC==1) | `StrategyND` strided | "Enable StridedStrategyND" |
| D | else | `StrategyND` default ND-ring | "Enable StrategySubgroupND." |

Each `StrategyND` then resolves `UniDirection1DRingStrategy` vs `UniDirectionNDRingStrategy` in `BuildStrategy` @`0x137c4660` via the `[obj+0xa8]` gate. The per-color ring-dimension table comes from `ComputeColorDimensions` @`0x137c3ba0`, which is where the **degraded-axis fault-tolerant remap** folds in (a failed torus axis is demoted to the inner ring dimension; the effective dimension count drops 3→2) — see [Degraded-Axis Ingest](degraded-axis.md). The twisted-torus geometry is its own section: [Twisted Torus](../twist/overview.md).

---

## 5. The SC-offload gate

SparseCore offload is enabled only when **all** of the following hold (confirmed byte-exact in `SparseCoreCompiler::RunHloScheduler` @`0x1306f820`; offsets `1576 = 0x628`, `1344 = 0x540` match the decompiled `(*((_BYTE *)v6 + 1576) & 4) != 0 || *((_BYTE *)v6 + 1344)`):

```text
runSC =  TpuChipConfig::Megachip(Target chip-config)                    @0x1306f84c
      ∧  CoresPerChip(kSparseCore) > 0                                  @0x1306f863
      ∧  ( Target[+0x628] & 4   ∨   Target[+0x540] ≠ 0 )                @0x1306f86c / @0x1306f87a
      ∧  ModuleContainsLEMSparseCoreInstruction(module)                @0x1306fbc8
      ∧  FLAGS_xla_sc_enable_latency_hiding_scheduler                  @0x1306fc04
```

The two Target fields are written in `jellyfish::Target::Init` @`0x1d60fc20`:

- **`Target[+0x628] & 4`** — the SC-offload-**capability** has-bit (`|= 0x4` @`0x1d612121`), OR'd in inside a config-append loop gated by the same SC-offload feature-detect predicate. This is the real-hardware path.
- **`Target[+0x540]`** — a platform-type bool, set `(TpuTopology[+0] == 2)` @`0x1d610b1b` (the `iss`/simulator platform), which force-takes the SC path for the simulator.

The **per-generation hardware basis** is `TpuVersion == 5` (the newest generation, codename obfuscated as `"6acc60406"` in this build): `ShouldEnableConcurrentSparseCoreOffloading` @`0x1d6b6f80` and `EnableSparseCoreOffloadQueuingInLhs` @`0x1d6b81e0` both default `(TpuChipParts[+0] == 5)`, overridable by an `AutoOr<bool>` proto flag. The internal `TpuVersion` enum is `0 jellyfish, 1 dragonfish, 2 pufferfish, 3 viperfish, 4 ghostlite, 5 "6acc60406"` (proto value = internal + 1).

### 5.1 Op-type classification: `SparseCoreConfig.offload`

Once gated in, the SparseCore op type is read from `SparseCoreConfig` field 2 `offload`, a `TYPE_ENUM` of type `xla::jellyfish::Offload` (struct offset `+0x24`, has-bit `+0x10` mask `0x4`). It is a backend-config enum — **not** a custom-call target name and **not** an MLIR op kind. This enum routes the op into the scheduler's `kSparseCore*` resource arms:

| `Offload` value | Enumerator | Resource arm (scheduler, idx = enum − 2) |
|-----------------|------------|-------------------------------------------|
| 0 | `OFFLOAD_UNSPECIFIED` | (none; `rt22 ×N-cores` path) |
| 1 | `OFFLOAD_EMBEDDING` | (none; `rt22 ×N-cores` path) — reservation map only |
| 2 | `OFFLOAD_GATHER` | `rt23 kSparseCoreGather` |
| 3 | `OFFLOAD_SCATTER` | `rt24 kSparseCoreScatter` |
| 4 | `OFFLOAD_COLLECTIVE` | async-body recurse |
| 5 | `OFFLOAD_DATA_FORMATTING` | `rt25 kSparseCoreDataFormatting` |
| 6 | `OFFLOAD_KERNEL` | `rt26 kSparseCoreKernel` |
| 7 | `OFFLOAD_SORT` | `rt27 kSparseCoreSort` |
| 8 | `OFFLOAD_COMPUTE` | (none; `rt22 ×N-cores` path) |

The `OFFLOAD_COLLECTIVE` case (4) is the one that reaches the offload **collective** config builder of §1.2. Full enum derivation and the reservation-map twin (`GetSparseCoreResources`, idx = enum − 1) are in [SC Core-Selection (Offload)](sc-core-selection-offload.md) and [SC-Offload Config Builder](sc-offload-config-builder.md).

### 5.2 What the SC substrate emits

The offload config builder (`ConstructConfigForCollectiveUniDirNDGroups<*>`) produces a `CollectiveIciStrategyConfig` proto nest rather than HLO ReplicaGroup lists. The shape of that nest is the SC substrate's counterpart to the dense `StrategyND` per-color ring schedule:

```text
{AllGather|AllReduce|ReduceScatter}OffloadConfig   (sizeof 0x48, byte-identical layout)
  └─ ici_strategy_config : CollectiveIciStrategyConfig   (field 2)
       └─ color_strategies[] : PerColorIciStrategyConfig
            └─ phase_rings[]  : IciStrategyRingConfig    (ring_type, ring_dim, core_count, …)
```

The ring dimensions are drawn from the same X/Y/Z torus extents as the dense path, via the `IciStrategyRingDim` enum (8 values): `ICI_RING_DIM_{X,Y,Z}_{TORUS,MESH}` (1/2, 3/4, 5/6) and `ICI_RING_DIM_D2D` (7), with `0` invalid. UNIDIR rings emit `ICI_RING_TYPE_UNIDIR_CW` / `_CCW`. Whether the builder emits a single flat ring per axis or a multi-phase hierarchical decomposition is the **`HierarchicalKind`** decision (an `AutoOr<bool>` packing of `xla_tpu_enable_sparse_core_hierarchical_all_reduce`; AllGather/ReduceScatter are pinned flat, only AllReduce can be hierarchical in this build) — see [HierarchicalKind](hierarchical-kind.md). The SC twisted-torus path branches off the same K/2K mesh-dimension count gate the dense path uses; see [Twisted Torus](../twist/overview.md).

---

## 6. Verification notes

> **[CONFIRMED]** Substrate split, end-to-end flow, op-family dispatch, and the SC-offload gate were all cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `CostModel::GetCollectiveCycles` @`0x130abfc0`: opcode jump table cases `6/8` (AllGather → `UseAllGather2D`), `9/11/93` (AllReduce-family → `ComputeAllReduceCycles`), `12` (`ComputeAllToAllCycles`), `86` (`ComputeRaggedAllToAllCycles`), `34/36` (CollectivePermute); `TensorCoreFrequencyInMegaHertz`; `GetCollectiveOffloadConfig` SC-offload probe — all present.
> - `GetCommunicationMultiplier` @`0x127a16c0`: `ReplicaGroupsOnNDPlane(…, 2, 0)` then `return (unsigned int)v7 + 1`; `GetMultiSliceTopology` fork; `ConstructSliceTransferGroup(mode=3)` — exact.
> - `BaseStrategyND::SelectNDStrategy` @`0x137c78e0`: `StrategySubgroupND`, `StrategyND` (NDNway/strided), `TwistedTorusND` constructed; `IsGroupNDPlane`, `UseSpecialStrategyNDNWay`, `UseStridedStrategyND` guards; `ReplicaGroupsOnNDPlane(plane=2)`; VLOG strings — exact.
> - `SparseCoreCompiler::RunHloScheduler` @`0x1306f820`: `Megachip ∧ CoresPerChip(SC)>0 ∧ ((Target[+0x628]&4) ∨ Target[+0x540]) ∧ ModuleContainsLEMSparseCoreInstruction ∧ FLAGS_xla_sc_enable_latency_hiding_scheduler` — exact (offsets 0x628/0x540 confirmed).
>
> **[LOW]** Asynchronous-shell zero-cost set: opcodes `7/10/35` and `collective-broadcast` (33) are assigned to the default (0-cycle) branch per the cost-table derivation, but only the data-carrying opcodes were individually re-confirmed in the live jump-table walk here. The behavior (cost charged on the data-carrying opcode) is consistent across the AllReduce/AllGather/AllToAll branches.

---

## Cross-References

**Dense TensorCore collectives**
- [SelectNDStrategy](strategy-nd-picker.md) — the ND-strategy picker (sub-plane / ND-ring / N-Way / twisted / strided / subgroup)
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — latency-bound all-reduce emitters
- [AllReduce Hierarchical / Pincer](allreduce-hierarchical-pincer.md) — bandwidth-bound all-reduce emitters
- [AllGather ND-Ring](allgather-nd-ring.md) — 1D/2D ring all-gather
- [ReduceScatter](reduce-scatter.md) — the reduce-scatter phase of the all-reduce decomposition
- [AllToAll Tables](alltoall-tables.md) — all-to-all / ragged-all-to-all link tables
- [Degraded-Axis Ingest](degraded-axis.md) — fault-tolerant axis remap (3→2 dimension demotion)

**Cost model**
- [SPMD Link-Count Cost](spmd-link-count-cost.md) — `GetCommunicationMultiplier`, per-kind `GetCollectiveCycles` formulas, ICI resource slots

**SparseCore-offload substrate**
- [SC-Offload Config Builder](sc-offload-config-builder.md) — `ConstructConfigForCollectiveUniDirNDGroups<*>` and the `*OffloadConfig` proto
- [HierarchicalKind](hierarchical-kind.md) — the `AutoOr<bool>` flat-vs-hierarchical phase split
- [SC Core-Selection (Offload)](sc-core-selection-offload.md) — `SparseCoreConfig.offload` op-type classification and core selection

**Sibling subsystems**
- [Routing](../routing/overview.md) — route-table generation, toroidal route cache, unicast emission
- [Twisted Torus](../twist/overview.md) — twisted-torus geometry, 2-phase replica-group construction
- [Barriers](../barrier/overview.md) — replica / TensorCore barriers, SFLAG binding, tree-barrier vsync
- [ICI fabric](../ici/overview.md) — the inter-chip interconnect DMA layer
- [back to index](../index.md)
