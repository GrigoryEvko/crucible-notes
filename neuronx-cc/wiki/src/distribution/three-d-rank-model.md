# The 3-D Rank Model and `getCCRankWorldSize` Reconciliation

> *All addresses on this page are virtual addresses (VMA) for `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). `hlo-opt` symbols resolve in `neuronxcc/starfish/bin/hlo-opt` (`.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000); `hlo2penguin`, `libwalrus.so`, and the Cython `NeffInfo.so` symbols are tagged inline with their binary. Other builds differ; treat every address as version-pinned.*

## Abstract

XLA models a collective's participants in two logical axes: **replica** and **partition**. Every cross-device collective carries one flat replica-group list — conceptually a 2-D array `[num_replica_groups][num_devices_per_group]` (the iota flattening that produces it is [13.7](mesh-replica-group-math.md)'s subject). The Neuron NEFF, by contrast, stores collective topology in **three** nested axes: `replica_groups[topology][group][rank]`, read back by the Cython `NeffInfo.getCCRankWorldSize`. This page answers one question a reimplementer must get right: **how does XLA's 2-D per-op group list map onto Neuron's 3-D NEFF envelope, and what is a "rank"?**

The answer is a reconciliation, not a translation layer. XLA's `(num_replica_groups, num_devices_per_group)` *is* Neuron's `(middle, inner)` pair, sitting at the single outer slot `topology[0]`. The outer "topology" axis is a NEFF-assembly-time superset that enumerates *distinct* collective group geometries across the whole module; single-program SPMD — the only flow this compiler supports — emits exactly one consistent geometry, so the outer dim has length 1 and the 3-D list degenerates to the XLA 2-D list. A NEFF with more than one topology is **MPMD**, and `getCCRankWorldSize` explicitly **rejects** it. The innermost coordinate, a *rank*, is a device id on both sides: XLA places the flattened global device id (`replica_id*num_partitions + partition_id`) into each group, and the Neuron simulator resolves a physical core to that same id by `core / numCoresPerLNC`.

The wire between the two models is three functions: stock `xla::GetReplicaGroups` flattens the lazily-expanded `replica_groups()` into a `vector<vector<int64>>`; Neuron-authored `neuron::GetTpReplicaGroup` *discovers* the tensor-parallel group from the already-partitioned graph (it does not recompute it from a mesh); and the NEFF reader `getCCRankWorldSize` consumes the 3-D structure that `writeCCInfo` serialized. This page reproduces all three, gives a concrete XLA-replica-group → `[topology][group][rank]` worked mapping, and pins the MPMD reject to its strings.

For reimplementation, the contract is:

- The **dimensionality map** — which XLA quantity feeds each of Neuron's three axes, and why the outer axis is length 1 in the supported flow.
- The **rank identity** — `XLA replica_group entry == Neuron CC rank id == physical core / numCoresPerLNC`, and the `getRankIdforCore` / `isInReplicaGroup` sim primitives that consume it.
- The **bridge functions** — stock `GetReplicaGroups` (flatten) vs Neuron `GetTpReplicaGroup` (discover) vs the NEFF `getCCRankWorldSize` (read), tagged by provenance.
- The **MPMD rejection** — the `>1`-topology guard, its reject strings, and the BIR-sim single-LNC assertions, so a reimplementer reproduces the *refusal* rather than silently averaging topologies.

| | |
|---|---|
| **XLA per-op topology** | `vector<ReplicaGroup>` — 2-D `[num_replica_groups][num_devices_per_group]` |
| **Neuron NEFF topology** | `replica_groups[topology][group][rank]` — 3-D |
| **Reconciliation** | XLA `(nrg, ndpg)` = Neuron `(middle, inner)` at `topology[0]`; outer dim = 1 (SPMD) |
| **Flatten (stock)** | `xla::GetReplicaGroups(HloInstruction*)` @ `0x1f8ca40` |
| **Discover TP group (Neuron)** | `neuron::GetTpReplicaGroup` @ `0x1f80360` / `0x1f808f0` **[NEURON]** |
| **NEFF 3-D reader (Neuron)** | `NeffInfo.getCCRankWorldSize` @ `0x1ade0` (`NeffInfo.so`) **[NEURON]** |
| **NEFF 3-D writer (Neuron)** | `writeCCInfo` @ `0x1523af0` (`libwalrus`) **[NEURON]** |
| **Rank = core / N** | `NeuronCoresManager::getRankIdforCore` @ `0x272380` (birsim) **[NEURON]** |
| **MPMD reject** | `>1` topology ⇒ `"… possible MPMD neff file"` (`getCCRankWorldSize`) |

> **NOTE — provenance and scope.** The replica-group *representation* and the mesh→group flattening arithmetic are stock OpenXLA and belong to [13.7 (Mesh → Replica-Group Topology Math)](mesh-replica-group-math.md); this page does **not** re-derive them — it cross-refs them and covers only the 2-D↔3-D *rank reconciliation*. Where this page cites the iota math, `getCCRankWorldSize` algorithm, or LNC core model, it must agree with [13.7](mesh-replica-group-math.md), [12.3 NEFF JSON sidecars / `def.json` CCInfo](../formats/neff-json-sidecars.md), and [1.07 LNC memory model](../arch/lnc-memory-model.md) respectively.

---

## 1. The Two Models, Side by Side

*Provenance: the XLA side is stock upstream; the 3-D envelope is Neuron-authored.*

A reimplementer's first task is to hold both shapes in mind simultaneously. They describe the *same physical fabric* with a different number of brackets.

```text
XLA  (hlo-opt, per collective op)            Neuron NEFF  (def.json, per module)
-------------------------------------        -----------------------------------------
one CollectiveDeviceList per op              replica_groups[topology][group][rank]
  -> replica_groups() : vector<ReplicaGroup>   - OUTER  topology : unique CC topologies
  conceptually 2-D:                             - MIDDLE group    : groups per topology
    [num_replica_groups]                        - INNER  rank     : rank size per CC
    [num_devices_per_group]
  device id = replica*num_partitions+part     rank id = physical core / numCoresPerLNC
  NO third axis: every group of one op        + src_target_pairs (collective-permute)
  shares one channel_id + one group-mode.     + #rank_world_size  (explicit override)
```

### Why XLA has no third axis

At the HLO-op level there is exactly one topology per collective: all of an op's groups share one `channel_id` and one `CollectiveOpGroupMode` ([13.7 §4](mesh-replica-group-math.md#4-channel_id--use_global_device_ids)). The "outer" choice — *which distinct group geometry* — is not an op property; it only emerges when the NEFF packager surveys the whole module and collects the *set* of group shapes that appear. `writeCCInfo` @ `0x1523af0` (libwalrus, **NEURON**) scans every `InstCollectiveCompute` op and de-dups their group geometries; the outer `topology` dim enumerates that de-duped set. For single-program SPMD every collective shares one geometry, so the set has one element and `topology` is length 1.

> **QUIRK —** the "3-D vs 2-D" gap is *not* a representational mismatch that needs a converter. It is a containment: XLA's per-op 2-D list is one element of Neuron's outer-dim set. In the supported flow that set is a singleton, so Neuron's `replica_groups[0]` is bit-for-bit XLA's flattened group list. A reimplementer who builds an explicit 2-D→3-D *transform* has over-engineered; the correct move is to wrap the XLA list as `topology[0]`.

---

## 2. The Dimensionality Map

The exact correspondence, axis by axis:

| Neuron axis | Holds | XLA source | Confidence |
|---|---|---|---|
| **INNER** — rank size per CC | ranks in one comm | `num_devices_per_group` (a group's length) | CERTAIN |
| **MIDDLE** — groups per topology | independent comms in the op | `num_replica_groups` | CERTAIN |
| **OUTER** — unique CC topologies | distinct group geometries across the module | *no op-level source*; the de-duped set built by `writeCCInfo` | HIGH |

`num_replica_groups` and `num_devices_per_group` are read directly from the iota descriptor by `IotaReplicaGroupList::num_replica_groups()` @ `0x9622500` (returns `[this+0x10]`) and `num_devices_per_group()` @ `0x9622510` (returns `[this+0x18]`) — see [13.7 §2](mesh-replica-group-math.md#2-the-compact-strided-form--iotareplicagrouplist--iotatileassignment). After the lazy `ExpandIota` (13.7 §2), the same pair is the outer/inner length of the explicit `vector<ReplicaGroup>`.

```c
// Reconciliation invariant (single-program SPMD).  Pseudocode for the SHAPE identity;
// no single function computes this — it is the structural equality the NEFF enforces.
//   neuron_rg : the def.json replica_groups[topology][group][rank]   (written by writeCCInfo @0x1523af0)
//   xla_rg    : one op's replica_groups()  (vector<ReplicaGroup>, after ExpandIota)
assert(neuron_rg.size() == 1);                       // exactly one topology in SPMD (else MPMD reject, §4)
assert(neuron_rg[0].size()    == num_replica_groups);    // MIDDLE == XLA num_replica_groups
for (group g : neuron_rg[0])
    assert(g.size()           == num_devices_per_group); // INNER  == XLA num_devices_per_group (consistent)
//   element identity:
//   neuron_rg[0][gi][ri]      == xla_rg[gi].replica_ids(ri)   (same device id, bit-for-bit, §3)
```

There is no genuine 2-D-vs-3-D mismatch in the supported flow. XLA's `(num_replica_groups, num_devices_per_group)` is exactly Neuron's `(middle, inner)` at the one topology XLA produces; the outer axis is a NEFF-level superset for MPMD / multi-distinct-CC NEFFs that stock single-program SPMD never emits. The data shapes are identical — the `getCCRankWorldSize` docstring at [12.3](../formats/neff-json-sidecars.md) names the three axes verbatim. The reading of the outer axis as "the set of distinct group shapes" follows from the `writeCCInfo` de-dup plus the MPMD guards rather than from any single decisive instruction.

---

## 3. The Rank Identity — Device ID = Core / numCoresPerLNC

*This section spans two binaries: the ids originate in `hlo-opt` and are consumed as ranks in the BIR simulator.*

The innermost coordinate is a *rank*, and a rank is a device id on both sides. This is the crux of the reconciliation: the integers XLA writes into each group are the *same integers* the Neuron simulator scans for when it asks "is this core in this collective?".

### What XLA writes

In the SPMD case the partitioner's default creator emits `channel_id` set **and** `use_global_device_ids = true`, so collectives are `kFlattenedID` ([13.7 §4](mesh-replica-group-math.md#4-channel_id--use_global_device_ids)). In that mode each `replica_group` entry is a **global device id**:

```text
device_id = replica_id * num_partitions + partition_id          // kFlattenedID
```

For a bare cross-partition collective (`kCrossPartition`, no `use_global_device_ids`) the entries are partition ids instead; either way they are absolute ids in a flat space.

### What Neuron reads

The BIR simulator resolves a *physical core* to a *rank* by integer-dividing out the LNC core count, then locates that rank in the single topology:

```c
// birsim::NeuronCoresManager::getRankIdforCore(core)   @0x272380   [NEURON]
//   *(this+0) == numCoresPerLNC  (== 2 on Trn2/sunda; == 1 elsewhere — see 1.07)
int getRankIdforCore(int core) {
    return core / *(int*)(this + 0);                 // core / numCoresPerLNC
}

// isInReplicaGroup(core)   @0x1aa460   [NEURON] — does this core participate?
bool isInReplicaGroup(int core) {
    auto &g0 = replica_groups[0];                    // THE single topology (topology[0])
    if (g0.flat_u32.empty()) return true;            // empty => implicit single group, all participate
    for (uint32 rank : g0.flat_u32)                  // linear scan of the flat rank list
        if (rank == core) return true;               // (compared against `core`, the device id)
    return false;
}
```

`numReplicas` for an op is `replica_groups[0].size()` — the inner group size — read per-op. The reconciliation is exact under the single-LNC / single-topology regime:

> `XLA replica_group entry  ==  Neuron CC rank id  ==  (physical core / numCoresPerLNC)`

`numCoresPerLNC` is the `--lnc` mesh cardinality (`PassOptions+0x1A4`, default 2 on Trn2, 1 elsewhere) — the same number that seeds `config.num_partitions()` upstream ([1.07](../arch/lnc-memory-model.md) confirms `getRankIdforCore` and the `NeuronCoresManager` logical/physical-core mapping; [13.1](spmd-partitioner-driver.md) confirms the `--lnc` → `num_partitions` seeding). So the two models close the loop: Neuron picks `N`, `N` becomes `num_partitions`, XLA flattens device ids over `num_partitions`, and the simulator divides physical cores by `N` to recover those very ids.

> **GOTCHA —** the rank is `core / numCoresPerLNC`, **not** `core` itself. On a 2-core LNC (`N = 2`) physical cores `{0,1}` both map to rank 0, `{2,3}` to rank 1, and so on. A reimplementer who treats the physical core index as the CC rank will mis-route every collective on Trn2. The division is the whole point of the LNC abstraction: the two physical cores of one LNC are one SPMD *device* with one rank.

### Rank-within-group resolution

Two sim visitors consume the reconciled ranks:

- `GetGlobalRankId` (IT11, `visitInstGetGlobalRankId` @ `0x1ba670`) emits the precomputed global rank straight from `InstVisitor+0xDE8`.
- `GetCurProcessingRankID` (IT66, `visitInstGetCurProcessingRankID` @ `0x1bb1f0`) resolves the TP rank *within* the group from `(iter_id, channel_id, replica_groups[0], arch-discriminant *(int*)(Module+172))` via `sub_1BA6A0` @ `0x1ba6a0`, indexing the XLA-supplied group list against a per-arch precomputed TP-rank permutation table (`qword_22969A0`: `ArchLevel × groupSize → rank permutation`).

Both take `replica_groups[0]` — the single topology — as their group source. The "3-D" is therefore `(topology = 0, group, rank)`; only the last two carry information in the SPMD-supported case, and both come straight from XLA's replica groups.

---

## 4. The MPMD Rejection — Why the Outer Axis Stays Length 1

*Neuron-authored; grounded in the reject strings and the guards around them.*

The 3-D envelope *could* carry more than one topology; the reader refuses to. `getCCRankWorldSize` @ `0x1ade0` (Cython `NeffInfo.so`, **NEURON**) walks the outer dim and rejects any NEFF where the topologies are not a single consistent shape. Its docstring names the axes verbatim (reproduced in [12.3 §5](../formats/neff-json-sidecars.md#5-getarchtype-and-getccrankworldsize--derived-accessors)):

```text
replica_groups: 3D structure defining CC topology
  - Outermost dim: Unique CC topologies.
  - Middle:        Number of groups per CC topology.
  - Inner:         rank size per CC.
```

```c
// __pyx_pw_..._getCCRankWorldSize  @0x1ade0   [NEURON, NeffInfo.so]
// world_size = max(ws_rg, ws_pairs, ws_expl, 1).  See 12.3 for the full reader; here the MPMD guards.
function getCCRankWorldSize(self):
    cc = self.def_json["CCInfo"]
    ws_rg = 0
    for topo_idx, topo in enumerate(cc["replica_groups"]):    // (a) the 3-D replica_groups path
        if topo is empty:
            FATAL("Empty topology found at index {}, possible MPMD neff file ...", topo_idx)   // reject #1
        inner = max(len(group) for group in topo)             // rank size per CC, this topology
        if ws_rg != 0 and inner != ws_rg:
            FATAL("Inconsistent worker count across topologies: {} vs {}. Possible MPMD ...",   // reject #2
                  ws_rg, inner)
        ws_rg = inner
    ws_pairs = derive_from(cc["src_target_pairs"])            // (b) collective-permute span
    ws_expl  = cc.get("#rank_world_size", 0)                  // (c) explicit literal override
    return max(ws_rg, ws_pairs, ws_expl, 1)
```

The two reject paths — **`"Empty topology found at index {}, possible MPMD neff file …"`** (an empty inner topology) and **`"Inconsistent worker count across topologies: {} vs {}. Possible MPMD …"`** (topologies of different rank sizes) — are the mechanism by which the outer axis is constrained to one effective entry. A well-formed single-program SPMD NEFF has one topology of one consistent inner size; anything else is MPMD and is refused (the diagnostic suggests `--stopAfter=compile` as the workaround). The producer side never *emits* `>1` topology in this flow, but the reader enforces the contract defensively.

The BIR simulator carries the same single-topology assumption from the other end: it asserts `replica_groups.size() == 1` and emits **`"Multiple LNCs are not supported yet"`** on the `ReduceScatter` / `SendRecv` paths. Both ends agree: one topology, one LNC, in the supported flow.

> **GOTCHA —** a reimplementer who silently maxes or averages inconsistent topologies instead of rejecting them produces a NEFF that runs with the wrong world size and corrupts every collective. The contract is *refuse*, not *reconcile*. Reproduce the two FATALs verbatim.

---

## 5. The Bridge Functions — Flatten, Discover, Read

*Provenance: `GetReplicaGroups` is stock XLA; `GetTpReplicaGroup` and `HasMatchingReplicaGroups` are Neuron-authored.*

Three functions carry a collective's groups from the XLA graph into the Neuron NEFF. They are not a pipeline of transforms — each consumes the *same* device ids — but they have different provenance, and a reimplementer must keep that straight.

### (i) Stock flatten — `xla::GetReplicaGroups`

```c
// xla::GetReplicaGroups(HloInstruction*)  @0x1f8ca40   [STOCK XLA]
vector<vector<int64>> GetReplicaGroups(HloInstruction *inst) {
    const auto &groups = inst->replica_groups();          // @0x965e7e0 — lazily ExpandIota's (13.7 §1)
    vector<vector<int64>> out;
    out.reserve(groups.size());
    for (const ReplicaGroup &rg : groups) {
        int64 n        = *(int64*)((char*)&rg + 0x10);    // replica_ids count
        const int64 *d =  (int64*)*(void**)((char*)&rg + 0x18);  // replica_ids data (RepeatedField<int64>)
        out.emplace_back(d, d + n);                       // flatten this group
    }
    return out;                                           // the 2-D [group][rank] form Neuron consumes
}
```

This produces the 2-D `vector<vector<int64>>` that Neuron passes consume. The device ids are unchanged — `inst->replica_groups()` already ran `ExpandIota`, so iota-encoded groups arrive as explicit `[group][rank]` lists.

### (ii) Neuron discover — `neuron::GetTpReplicaGroup`

```c
// neuron::GetTpReplicaGroup(HloComputation*)  @0x1f80360   [NEURON]
vector<ReplicaGroup> GetTpReplicaGroup(HloComputation *comp) {
    for (HloInstruction *inst : comp->MakeInstructionPostOrder()) {
        // HLO match:: pattern keyed on the opcode byte [inst+0x14]
        // (pattern immediates: 0x57 == 87 == kReduceScatter, etc.)
        if (matches_collective_pattern(*(uint8*)((char*)inst + 0x14)))
            return inst->replica_groups();                // @0x965e7e0 — the canonical TP group
    }
    return {};
}
```

> **QUIRK —** `GetTpReplicaGroup` **discovers** the tensor-parallel group from the *already-partitioned* graph's collective — it does **not** recompute it from a mesh. By the time this runs, the SPMD partitioner has already emitted the collectives with their groups baked in; the TP group is just *whichever collective matches the pattern*'s `replica_groups()`. A reimplementer who tries to re-derive the TP group from the sharding/mesh here is doing redundant (and divergence-prone) work — the authoritative group is already in the graph.

The `HloModule` overload `@0x1f808f0` takes a `flat_hash_set<string_view>` of computation/attr names to scan and aggregates across them. `neuron::HasMatchingReplicaGroups(inst, vector<ReplicaGroup>)` @ `0x1f7e060` (**NEURON**) gates collective combiners by checking that an instruction's groups equal a reference set — two collectives merge only if their replica groups match exactly, so the 2-D group list *is* a collective's topology identity end to end. (Same symbols appear in `hlo2penguin`: `GetTpReplicaGroup` @ `0x205cbf0`, `HasMatchingReplicaGroups` @ `0x205a8f0`.)

### (iii) Neuron read — `getCCRankWorldSize`

The NEFF reader of §4 consumes the 3-D `replica_groups[topology][group][rank]` that `writeCCInfo` @ `0x1523af0` serialized into `def.json` from the same per-op groups. The flatten of (i) and discover of (ii) operate on the XLA side; the read of (iii) operates on the NEFF side; the device ids are identical across all three.

---

## 6. Worked Mapping — XLA Replica Group → `[topology][group][rank]`

*This worked example is composed from the mappings established in §1–§3; it is not itself read out of the binary.*

Take the strided AllReduce from [13.7 §5](mesh-replica-group-math.md#5-worked-example--2-axis-mesh--allreduce-replica-groups): a 2×4 mesh (`data`=2, `model`=4, 8 devices) reducing over the `data` axis. The iota math there yields four groups of two, strided by 4 (cross-ref 13.7 for the `value_at` derivation; this page does not re-run it):

```text
XLA side (one collective op, after ExpandIota):
   replica_groups() = { {0,4}, {1,5}, {2,6}, {3,7} }
   num_replica_groups    = 4          // = |model|
   num_devices_per_group = 2          // = |data|
   mode = kFlattenedID  =>  ids are global: device_id = data_idx*4 + model_idx
```

Now map it into the Neuron 3-D envelope. There is one collective geometry in the module, so the outer dim is a singleton:

```text
Neuron NEFF side  (replica_groups[topology][group][rank]):

  replica_groups = [                       // OUTER: topology index, length 1 (SPMD)
      [                                    // topology[0]  (the only topology)
          [0, 4],                          // group 0, ranks [r0=0, r1=4]      MIDDLE/INNER
          [1, 5],                          // group 1
          [2, 6],                          // group 2
          [3, 7],                          // group 3
      ]
  ]

  MIDDLE length = 4  == XLA num_replica_groups       ✓
  INNER  length = 2  == XLA num_devices_per_group    ✓  (consistent across all 4 groups)
  element [0][1][0] == 1  == XLA replica_groups()[1].replica_ids(0)  ✓  (bit-for-bit)
```

`getCCRankWorldSize` over this structure: the outer loop sees one non-empty topology; `inner = max(len(g)) = 2` for all four groups (consistent, no MPMD reject); `ws_rg = 2`. No `src_target_pairs`, no `#rank_world_size`. World size `= max(2, 0, 0, 1) = 2`.

Sim-side rank resolution, on a Trn2 LNC with `numCoresPerLNC = 2`, for physical core 9:

```text
rank          = getRankIdforCore(9) = 9 / 2 = 4
isInReplicaGroup(... core==4 ...):
   scans topology[0] flat list [0,4,1,5,2,6,3,7] for 4  ->  found in group 0
=> physical core 9 participates as global rank 4, the second rank of group {0,4}.
```

The device id `4` that XLA placed in group 0 is exactly the rank the simulator recovers from physical core 9 by dividing out the LNC core count. The 2-D XLA list and the 3-D Neuron envelope name the same fabric; the only "extra" Neuron bracket is the singleton outer dim.

---

## 7. Stock-XLA vs Neuron — Explicit Boundary

**STOCK upstream XLA** (`xla::`; no Neuron authorship):

- `xla::GetReplicaGroups(HloInstruction*)` @ `0x1f8ca40` — flatten `replica_groups()` to `vector<vector<int64>>` ([§5(i)](#5-the-bridge-functions--flatten-discover-read))
- `HloInstruction::replica_groups()` @ `0x965e7e0` (lazy `ExpandIota`; the 2-D representation itself is [13.7](mesh-replica-group-math.md))
- the `kFlattenedID` global-id convention `replica*num_partitions + partition` ([§3](#3-the-rank-identity--device-id--core--numcorespernlnc), table in 13.7 §4)

**NEURON-authored** (the only Neuron code on this page):

- `neuron::GetTpReplicaGroup(HloComputation*/HloModule*)` @ `0x1f80360` / `0x1f808f0` — discover the TP group from the partitioned graph ([§5(ii)](#5-the-bridge-functions--flatten-discover-read))
- `neuron::HasMatchingReplicaGroups` @ `0x1f7e060` — gate combiners on group equality
- `NeffInfo.getCCRankWorldSize` @ `0x1ade0` (`NeffInfo.so`) — the 3-D reader + the MPMD rejection ([§4](#4-the-mpmd-rejection--why-the-outer-axis-stays-length-1))
- `writeCCInfo` @ `0x1523af0` (`libwalrus`) — the 3-D `replica_groups` writer + the outer-dim de-dup
- `getRankIdforCore` @ `0x272380` / `isInReplicaGroup` @ `0x1aa460` (birsim) — the `core / numCoresPerLNC` rank map ([§3](#3-the-rank-identity--device-id--core--numcorespernlnc))

**Honest flag:** Neuron invents *no* replica-group encoding and *no* rank arithmetic beyond the LNC division. The 2-D↔3-D "reconciliation" is, in the supported flow, a containment: XLA's per-op list becomes Neuron's `topology[0]`. Neuron's genuine contributions are (a) discovering the TP group from the graph rather than recomputing it, (b) keying combiners on the groups, (c) the NEFF 3-D envelope with its MPMD guard, and (d) the `core / numCoresPerLNC` rank map. `>1` topology = MPMD = rejected.

---

## Related Components

| Name | Relationship |
|---|---|
| [13.7 Mesh → Replica-Group Topology Math](mesh-replica-group-math.md) | Owns the XLA 2-D representation, iota `value_at` flattening, and `GetCollectiveOpGroupMode` table this page reconciles against |
| [13.1 SpmdPartitioner Driver & Options](spmd-partitioner-driver.md) | `--lnc` → `num_partitions` seeding; the `N` that becomes `numCoresPerLNC` |
| [13.6 SPMD Collective Emission](spmd-collective-emission.md) | The default creator that emits `kFlattenedID` collectives whose ids this page maps |
| [12.3 NEFF JSON Sidecars / `def.json` CCInfo](../formats/neff-json-sidecars.md) | Owns the full `getCCRankWorldSize` reader and `def.json` CC schema |
| [1.07 LNC Memory Model](../arch/lnc-memory-model.md) | `NeuronCoresManager` logical/physical-core mapping; `getRankIdforCore`; cores-per-LNC = 2 on Trn2 |

## Cross-References

- [Mesh → Replica-Group Topology Math](mesh-replica-group-math.md) — the 2-D representation + iota flattening this page reconciles into 3-D
- [SpmdPartitioner Driver & Options](spmd-partitioner-driver.md) — where `num_partitions` / the LNC cardinality originate
- [SPMD Collective Emission](spmd-collective-emission.md) — the `kFlattenedID` global-id convention that defines a rank
- [NEFF JSON Sidecars](../formats/neff-json-sidecars.md) — the `def.json` CCInfo schema and the full `getCCRankWorldSize` reader
- [LNC Memory Model](../arch/lnc-memory-model.md) — `rank = physical core / numCoresPerLNC`, the LNC device abstraction
