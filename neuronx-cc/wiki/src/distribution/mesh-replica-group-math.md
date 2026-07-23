# Mesh → Replica-Group Topology Math

> *All addresses on this page are virtual addresses (VMA) for `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), binary `neuronxcc/starfish/bin/hlo-opt` unless tagged otherwise; resolve via `objdump --start-address=<VA>` or the VMA-keyed `disasm/` sidecars. VA ≠ raw file offset: for `hlo-opt`, `.text` file_off = VA − 0x201000 and `.rodata` file_off = VA − 0x200000 (section headers). Cross-binary symbols in `hlo2penguin` / `libwalrus.so` are tagged inline. Other builds differ; treat every address as version-pinned. Provenance D-AB07.*

## Abstract

Once Shardy propagation ([13.2](sharding-propagation.md)) and the factor algebra ([13.3](sharding-algebra.md)) have decided *which mesh axes shard which tensor dimensions*, and the bridge ([13.4](shardy-hlosharding-bridge.md)) has lowered that decision into an `HloSharding` with a concrete `TileAssignment`, the SPMD partitioner must turn the abstract **device mesh** (named axes + sizes) into the flat **replica-group lists** that each emitted collective consumes. This page documents that last arithmetic step: the axis-major flattening of the mesh, the compact strided encoding XLA uses to store a replica-group list, the lazy expansion of that encoding into explicit `[group][rank]` device lists, and the `channel_id` × `use_global_device_ids` decode that fixes whether the device ids in a group name *replica* ids, *partition* ids, or *flattened global* ids.

The headline finding is a provenance one: **the replica-group representation and all the index arithmetic are stock upstream XLA** (`xla::CollectiveDeviceList`, `xla::IotaReplicaGroupList`, `xla::IotaTileAssignment`, `xla::GetCollectiveOpGroupMode`), linked verbatim into `hlo-opt`. Neuron invents *no* replica-group encoding. The only Neuron-authored code in this strand is (a) the `--lnc` mesh cardinality that seeds `num_partitions` upstream, (b) `neuron::GetTpReplicaGroup` / `neuron::HasMatchingReplicaGroups`, which *discover* the tensor-parallel group from the already-partitioned graph rather than recompute it, and (c) the NEFF-side 3-D `replica_groups[topology][group][rank]` envelope read by `getCCRankWorldSize`. The XLA 2-D `(num_replica_groups, num_devices_per_group)` pair reconciles exactly with Neuron's `(middle, inner)` dims at `topology[0]`; the outer "topology" axis is an MPMD superset that single-program SPMD never populates (and which `getCCRankWorldSize` rejects).

For reimplementation, the contract is:

- The **`CollectiveDeviceList` dual form** — an embedded compact `IotaReplicaGroupList` (`has_iota_ == 1`) *or* an explicit `vector<ReplicaGroup>`, with the lazy `ExpandIota()` that materialises the explicit list on first demand.
- The **iota math** — `IotaTileAssignment::value_at`: flatten a tile coordinate row-major over `dims`, delinearise into `reshape_dims` walking `transpose_perm` in reverse (one `idiv` per axis), re-linearise to a device id. This is how a *strided* group (e.g. `{0,2,4,6}`) is stored in a few words.
- The **group construction** — a replica group is the set of device ids that share *all* tile-coordinates **except** the dims the collective reduces/gathers over; `ExpandIota` chunks the flattened device grid into `num_replica_groups` runs of `num_devices_per_group`.
- The **group-mode decode** — the `GetCollectiveOpGroupMode` truth table over `(has_channel_id, use_global_device_ids)`, which determines the *meaning* of the ids in each group.

| | |
|---|---|
| **Topology container** | `xla::CollectiveDeviceList` — 0x38 B; `has_iota_`@+0x20, `replica_groups_` shared_ptr@+0x28 |
| **Compact form** | `xla::IotaReplicaGroupList` — 0x20 B; `num_replica_groups_`@+0x10, `num_devices_per_group_`@+0x18 |
| **Iota descriptor** | `xla::IotaTileAssignment` — `{ndims@+0, reshape_ndims@+4, buf@+8}` |
| **Iota → device id** | `IotaTileAssignment::value_at(Span<long>)` @ `0x97a2d80` |
| **Lazy expand** | `CollectiveDeviceList::replica_groups()` @ `0x96240f0` → `ExpandIota` @ `0x9623c00` |
| **Group-mode table** | `xla::GetCollectiveOpGroupMode(bool, optional<bool>)` @ `0x9163ae0` |
| **TP-group discovery** | `neuron::GetTpReplicaGroup` @ `0x1f80360` / `0x1f808f0` **[NEURON]** |
| **NEFF 3-D reader** | `NeffInfo.getCCRankWorldSize` (Cython `NeffInfo.so`) **[NEURON]** |

> **NOTE — provenance.** Everything in [§1](#1-the-replica-group-representation)–[§4](#4-channel_id--use_global_device_ids) is **stock OpenXLA** (`xla/hlo/ir/collective_device_list.{h,cc}`, `xla/service/collective_ops_utils.cc`), statically linked. The Neuron-authored code is confined to [§6](#6-reconciliation--xla-2-d-vs-neuron-3-d-rank-model) and is tagged inline. Where this page touches `channel_id`/`use_global_device_ids` it must not contradict [4.4 Collective Stream-ID & Channel-ID Family](../hlo-opt/collective-stream-channel-id.md) or [13.6 SPMD Collective Emission](spmd-collective-emission.md); any discrepancy is called out with byte evidence. The 3-D rank model is detailed in [13.8 (3-D rank model)](three-d-rank-model.md).

---

## 1. The Replica-Group Representation

> **Origin: stock XLA.**

Every cross-device collective — `all-reduce`, `all-gather`, `all-to-all`, `reduce-scatter`, `collective-broadcast`, `ragged-all-to-all` — carries its device topology as one `xla::CollectiveDeviceList`. The container has two interchangeable internal forms, and this binary links both: a compact strided **iota** form and an explicit `vector<ReplicaGroup>`.

### Layout

`CollectiveDeviceList` is 0x38 bytes. The compact form is embedded in-place at offset 0; a `has_iota_` byte selects which form is authoritative:

```c
// xla::CollectiveDeviceList — 0x38 B
// ctor(Span<ReplicaGroup>) @0x9667ef0 ; replica_groups() @0x96240f0
struct CollectiveDeviceList {
    IotaReplicaGroupList iota_replica_group_list_;  // +0x00 : compact form, overlays +0x00..+0x1F (§2)
    bool                 has_iota_;                 // +0x20 : 1 => iota form authoritative, 0 => explicit
    shared_ptr<const vector<ReplicaGroup>> replica_groups_;  // +0x28 : ptr, lazily filled
    // +0x30 : shared_ptr control block
};
```

The explicit `Span<ReplicaGroup>` ctor `@0x9667ef0` writes `[this+0x20] = 0` (`mov BYTE PTR [rdi+0x20], 0x0` `@0x9667f13` — *not* iota) and `[this+0x28] = 0`, then `operator new(0x28)` allocates a `vector<ReplicaGroup>` and copies the span in. Two further ctors set `has_iota_ = 0` the same way:

- `CollectiveDeviceList(Span<vector<long>>)` `@0x1f7aa90` — the group form taking `vector<vector<long>>`, used by the explicit-group callbacks of the SPMD default creator ([13.6](spmd-collective-emission.md)).
- the `RepeatedPtrField<ReplicaGroup>` iterator ctor `@0x9623620`.

### Lazy iota → explicit expansion

The iota form is the **canonical** storage; the explicit `vector<ReplicaGroup>` is computed on first demand and cached behind the shared_ptr. The accessor:

```c
// xla::CollectiveDeviceList::replica_groups() const  @0x96240f0
const vector<ReplicaGroup>& replica_groups() const {
    void *rax = *(void**)(this + 0x28);          // cached explicit vector ptr
    if (rax != nullptr) return *rax;             // hot path — already materialised

    if (*(bool*)(this + 0x20) == 0)              // explicit form but null
        goto cold_build_empty;                   // (@0x962407a) — yield empty list

    // has_iota_ : expand the compact descriptor into a real vector<ReplicaGroup>
    vector<ReplicaGroup> tmp = ExpandIota(*(IotaReplicaGroupList*)this);  // @0x9623c00
    *(void**)(this + 0x28) = tmp.data;           // cache the result
    *(void**)(this + 0x30) = tmp.control_block;
    return *(vector<ReplicaGroup>*)(this + 0x28);
}
```

Every downstream consumer that wants concrete device ids — `HloInstruction::replica_groups()` `@0x965e7e0`, `GetReplicaGroups`, the Neuron combiners, `ConvertCollectivesToCustomCall` — funnels through this accessor, so an iota-encoded group is *transparently* seen as an explicit `[group][rank]` list. Printing (`Print` `@0x9624300` / `ToString` `@0x9624290`) emits the literal `iota_replica_group_list` with `{num_replica_groups, num_devices_per_group}` for the compact form, or `{{d,d,…},{…}}` for the explicit form (rodata strings `iota_replica_group_list`, `num_replica_groups`, `num_devices_per_group` all present).

---

## 2. The Compact (Strided) Form — IotaReplicaGroupList + IotaTileAssignment

> **Origin: stock XLA.**

This is the heart of the page: how a four-field descriptor `(num_groups, group_size, reshape, transpose)` expands to a full device→group map without materialising it until needed.

### IotaReplicaGroupList layout

```c
// xla::IotaReplicaGroupList — 0x20 B; embedded at CollectiveDeviceList+0
// num_replica_groups()    @0x9622500 : return *(long*)(this+0x10)
// num_devices_per_group() @0x9622510 : return *(long*)(this+0x18)
struct IotaReplicaGroupList {
    IotaTileAssignment iota_tile_assignment_;  // +0x00 : {ndims,reshape_ndims,bufptr} (§2.2)
    int64 num_replica_groups_;                 // +0x10 : number of groups   (outer)
    int64 num_devices_per_group_;              // +0x18 : ranks per group     (inner)
};
```

Conceptually this is a 2-D grid `[num_replica_groups][num_devices_per_group]`. The embedded `IotaTileAssignment` carries the *reshape/transpose* that lets a strided (non-contiguous) assignment be stored compactly. `FromProto` `@0x9622550` reads `IotaReplicaGroupListProto {iota_reshape_dims[], iota_transpose_perm[], num_replica_groups, num_devices_per_group}`.

### IotaTileAssignment layout

```c
// xla::IotaTileAssignment — ctor(dims,reshape,perm) @0x97a1ac0
struct IotaTileAssignment {
    int32 ndims;          // +0x00 : dims.size()
    int32 reshape_ndims;  // +0x04 : reshape_dims.size() == transpose_perm.size()
    char *buf;            // +0x08 : one packed allocation, layout below
};
// buf =  [ dims      : int64 × ndims        ]   offset 0
//        [ reshape   : int64 × reshape_ndims ]   offset ndims*8
//        [ transpose : int32 × reshape_ndims ]   offset (ndims+reshape_ndims)*8
```

The ctor `@0x97a1ac0` allocates `new char[ndims*8 + reshape_ndims*(8+4)]` (`@0x97a1af6`) and fills the three runs with three `memcpy`s (`@0x97a1b17`, `@0x97a1b32`, tail-jmp `@0x97a1b55`). Two factory entries:

- `Create(Span<long> dims)` `@0x97a1b60` — the trivial row-major iota: `reshape_dims = dims`, `transpose_perm = identity`.
- `Create(Span<long> dims, Span<long> reshape, Span<int> perm)` `@0x97a2870` — the general form the bridge ([13.4](shardy-hlosharding-bridge.md)) uses to encode mesh-axis nesting.

### The iota → device-id math

`value_at(Span<long> index)` `@0x97a2d80` is the function that maps a multi-dim tile coordinate to a device id. It is three loops — linearise, delinearise-with-transpose, re-linearise:

```c
// xla::IotaTileAssignment::value_at(Span<long> index) const  @0x97a2d80  (full body)
// index[] : one coordinate per tile/group dim.  Returns the device id at that tile.
int64 value_at(const int64 *index) {
    const int64 *dims    = (int64*)buf;                       // dims base
    const int64 *reshape = (int64*)(buf + ndims*8);           // r12 in disasm
    const int32 *perm    = (int32*)(buf + (ndims+reshape_ndims)*8);

    // STEP 1 — linearise over dims_, row-major   (imul+add loop @0x97a2dc0)
    int64 lin = index[0];
    for (int a = 1; a < ndims; ++a)
        lin = lin * dims[a] + index[a];          // flat position in the un-reshaped grid

    // STEP 2 — delinearise into reshape_dims_, honouring transpose_perm in REVERSE
    int64 coord[reshape_ndims];                  // reverse loop @0x97a2e18
    int64 rem = lin;
    for (int k = reshape_ndims - 1; k >= 0; --k) {
        int   ax = perm[k];                      // movsxd from perm_base (int32)
        int64 sz = reshape[ax];                  // movsxd from reshape_base (int64)
        coord[ax] = rem % sz;                    // single idiv: rdx = rem%sz, rax = rem/sz
        rem       = rem / sz;
    }

    // STEP 3 — re-linearise the reshaped coords to a contiguous device id   (@0x97a2e68)
    int64 dev = coord[0];
    for (int k = 1; k < reshape_ndims; ++k)
        dev = dev * reshape[k] + coord[k];
    return dev;
}
```

This is the canonical XLA "reshape + transpose" iota encoding. The device id for a tile coordinate is obtained by flattening over the tile dims, reshaping the flat id into the mesh-axis grid, applying the axis permutation, then re-flattening — which is exactly what lets a strided group assignment (ranks `{0,2,4,6}` in group 0) be stored as a tiny descriptor instead of an explicit list.

### Materialising the full list

Two helpers turn the descriptor into concrete ids:

- `flattened_replica_groups()` `@0x9622af0` — returns one flat `vector<int64>` of all groups concatenated, length `num_replica_groups * num_devices_per_group`, in `[group-major, rank-minor]` order. It calls `IotaTileAssignment::ToArray()` `@0x97a38b0` (materialise the whole grid) and `memcpy`s the flat int64 run (`@0x9622b9b`). Group `k` is the slice `[k*group_size, (k+1)*group_size)`.
- `ExpandIota` `@0x9623c00` — builds the explicit `vector<ReplicaGroup>` that `replica_groups()` caches:

```c
// xla::(anon)::ExpandIota(const IotaReplicaGroupList&)  @0x9623c00  (prologue)
vector<ReplicaGroup> ExpandIota(const IotaReplicaGroupList &g) {
    vector<ReplicaGroup> result;
    result.reserve(g.num_replica_groups_);                    // @0x9623ca1
    vector<int64> arr = g.iota_tile_assignment_.ToArray();    // @0x9623cb0 ; ndev = prod(dims)
    int64 group_size = g.num_devices_per_group_;              // @0x9623cbc

    for (int64 gi = 0; gi < g.num_replica_groups_; ++gi) {
        ReplicaGroup rg;                                      // ctor @0x984f4e0
        rg.mutable_replica_ids()->Reserve(group_size);
        memmove_into(rg.replica_ids,                          // memmove @0x9a1e8c0
                     &arr[gi*group_size], group_size * 8);
        result.push_back(rg);
    }
    return result;
}
```

So the device→group map is: **device `d` belongs to group `(position-of-d-in-arr) / num_devices_per_group`.** Each group is `num_devices_per_group` consecutive entries of the `ToArray()`'d grid.

---

## 3. World-Size and Group Derivation

> **Origin: stock XLA consuming Neuron-supplied numbers.**

### Where num_partitions / num_replicas come from

The `SpmdPartitioner` ctor `@0x2a93280` is constructed with `num_partitions = module->config().num_partitions()` and `num_replicas = module->config().replica_count()` (= `[config+0x170]`), stored at `SpmdPartitioner+0x08` (`num_partitions_`) and `+0x10` (`num_replicas_`). Their product is the global device count the replica groups index into.

**Neuron decides the numbers, stock XLA consumes them unchanged.** Neuron does *not* override these at the driver ctor; the LNC mesh cardinality (`--lnc`, default 2 on Trn2) is what populates `config.num_partitions()` upstream ([13.1](spmd-partitioner-driver.md)). These two longs are also captured into the eight default-creator callbacks ([13.6](spmd-collective-emission.md)).

### Who actually builds the per-collective groups

This page is **not** where device groups are computed from scratch. The chain is:

1. **Sharding propagation + factor algebra** ([13.2](sharding-propagation.md)/[13.3](sharding-algebra.md)) decide each tensor's `HloSharding` tile assignment — which mesh axes shard which dims.
2. **The bridge** ([13.4](shardy-hlosharding-bridge.md)) flattens that into a `TileAssignment` whose device order *is* the mesh-axis nesting, encoded either as an `IotaTileAssignment` (iota order) or an explicit `Array<long>` (device ids present).
3. **The SPMD per-op handlers / reshards** ([13.6](spmd-collective-emission.md)) form each collective's device groups by grouping the tile assignment along the **sharding dims being collapsed**:
   - `AllReduceAlongShardingDims` — the groups are the tile-assignment rows along the reduced dims, i.e. one group per fixed value of the *non-reduced* dims. An affine row pattern is stored as `IotaReplicaGroupList`; otherwise as `vector<vector<long>>`.
   - `AllGatherShards` — groups along the gathered dims.

So the mesh→replica-group **rule** is one sentence:

> **A replica group is the set of device ids that share all tile-coordinates *except* the dims the collective sums/gathers over.** The iota encoding of [§2](#2-the-compact-strided-form--iotareplicagrouplist--iotatileassignment) is just the compact storage of that strided set.

### across-replicas vs across-partitions

These are *not* two group shapes from two code paths — they are the **same** replica-group list interpreted under a different `CollectiveOpGroupMode` ([§4](#4-channel_id--use_global_device_ids)):

- **cross-PARTITION** all-reduce: `channel_id` set, group ids are partition ids (the SPMD case the default creator emits). `num_devices_per_group` = partitions in the reduction group.
- **cross-REPLICA** all-reduce: no `channel_id`, group ids are replica ids; produced by the front-end for data-parallel replicas, not by the SPMD partitioner.

The op's world size = `num_replica_groups * num_devices_per_group`; per-group rank count = `num_devices_per_group`.

---

## 4. channel_id / use_global_device_ids

> **Origin: stock XLA.**

### channel_id seeding

`channel_id` is seeded by `hlo_query::NextChannelId(module)` `@0x8ab1ac0` (= `1 + max existing channel_id`) and threaded as `int64_t* next_channel_id` through the partitioner's visitor state. Each emitted collective takes the current value as its `optional<long> channel_id`; the caller post-increments. A **set** `channel_id` ⇒ cross-partition communicator semantics. (Consistent with [4.4](../hlo-opt/collective-stream-channel-id.md) and [13.6](spmd-collective-emission.md).)

### The group-mode truth table

`xla::GetCollectiveOpGroupMode(bool has_channel_id, optional<bool> use_global_device_ids)` `@0x9163ae0` returns `StatusOr<CollectiveOpGroupMode>`. This determines what the device ids in a group *mean*. The decode is disasm-exact (sret @`rdi`=`r12`; `sil`=`has_channel_id`; `dl`=use_global value; `al`←`dh`=optional-engaged flag):

| `has_channel_id` | use_global engaged | use_global | result | branch |
|---|---|---|---|---|
| false | false (nullopt) | — | `0` kCrossReplica | `@0x9163b0a` |
| false | true | false | `0` kCrossReplica | `@0x9163b72` |
| false | true | **true** | **InvalidArgument** (*"Invalid combination of has_channel_id and use_global_device_ids"*) | `@0x9163b74` |
| true | false (nullopt) | — | `1` kCrossPartition | `@0x9163b44` |
| true | true | false | `2` kCrossReplicaAndPartition | `@0x9163b60` |
| true | true | true | `3` kFlattenedID | `@0x9163c10` |

```c
// xla::GetCollectiveOpGroupMode  @0x9163ae0   (disasm-exact branch decode @0x9163afe)
// regs: sil=has_channel_id, dl=use_global value, al(<-dh)=optional-engaged
StatusOr<CollectiveOpGroupMode> mode(bool has_channel_id, optional<bool> g) {
    if (!has_channel_id) {                          // @0xb06
        if (!g.engaged)        return kCrossReplica;           // @0xb0a  = 0
        if (g.value == false)  return kCrossReplica;           // @0xb70 -> b0a
        return InvalidArgument(                                // @0xb74
            "Invalid combination of has_channel_id and use_global_device_ids");  // rodata @0x36e498
    } else {                                        // @0xb40
        if (!g.engaged)        return kCrossPartition;             // @0xb44 = 1
        if (g.value == false)  return kCrossReplicaAndPartition;   // @0xb60 = 2
        return kFlattenedID;                                       // @0xc10 = 3
    }
}
```

*Anchors: the `(has_channel_id=false, use_global_device_ids=true)` reject literal is `"Invalid combination of has_channel_id and use_global_device_ids"` at hlo-opt `.rodata` `0x36e498` (file offset `0x16e498` under the `.rodata = fileoff + 0x200000` frame), reached from the `@0x9163b74` branch of `GetCollectiveOpGroupMode @0x9163ae0`; cross-checked against the [3-D rank model](three-d-rank-model.md) page.*

The four enum names are present in rodata and mapped by `CollectiveOpGroupModeToString` `@0x9163c50`: `kCrossReplica(0)`, `kCrossPartition(1)`, `kCrossReplicaAndPartition(2)`, `kFlattenedID(3)`. Note the counter-intuitive cell: **no channel + `use_global=false` yields `kCrossReplica`, not `kCrossPartition`** — the bool is ignored without a channel id.

> **QUIRK —** the SPMD partitioner's default creator emits `channel_id` set **and** `use_global_device_ids = true`, so its collectives are `kFlattenedID` ([13.6](spmd-collective-emission.md)). In that mode every `replica_group` entry is a **global device id** = `replica_id * num_partitions + partition_id`. This is required when one group must name devices from different replicas *and* partitions by absolute id.

### What each mode means for the group ids

- **kCrossReplica** — group entries are replica ids; one comm per replica group, all partitions of a replica share data. No channel id.
- **kCrossPartition** — channel id present, `use_global_device_ids` *absent* (nullopt); group entries are partition ids (partition-local). Bare-SPMD cross-partition.
- **kCrossReplicaAndPartition** — channel id present, `use_global_device_ids=false`; spans both replicas and partitions, ids are `(replica,partition)` flattened replica-major.
- **kFlattenedID** — channel id present, `use_global_device_ids=true`; entries are global ids `replica_id*num_partitions + partition_id`.

### Expansion to participants

`GetCollectiveOpGroupMode(HloInstruction*)` `@0x91649b0` reads `channel_id().has_value()` and `use_global_device_ids()` and calls the 2-arg form. `GetParticipatingIDs(mode, device_id, optional<total_replica_count>, Span<ReplicaGroup>)` `@0x9164f90` then returns a device's peers: if `replica_groups` is **empty** (branch `@0x9164fca`) all `total_replica_count` ids participate (implicit single group); else it looks up the group containing `device_id` under `mode`. `GetParticipatingDevicesGroups(...)` `@0x9167cf0` produces the full `vector<vector<GlobalDeviceId>>` the runtime uses; `…ForSourceTargetPairs` `@0x1f7b0e0` does the analogue for collective-permute. All stock XLA.

---

## 5. Worked Example — 2-Axis Mesh → AllReduce Replica Groups

> **Origin: derived from the §2/§3 math rather than read from a single body.**

Take a mesh with two named axes, `data` (size 2) and `model` (size 4), giving 8 devices `0..7`. Axis-major flattening (row-major over `[data, model]`) lays the mesh out as:

```text
              model →
        m0   m1   m2   m3
data d0  0    1    2    3
     d1  4    5    6    7
device_id = data_idx * |model| + model_idx       // axis-major flatten, |model| = 4
```

Now emit an **AllReduce that reduces over the `model` axis** (a tensor-parallel reduction). Per the rule of [§3](#3-world-size-and-group-derivation): *a group is the set of devices that agree on all coordinates except the reduced axis* — here, devices that share a `data` index. So:

```text
group 0  (data=d0) : {0, 1, 2, 3}      // all model ranks for data row 0
group 1  (data=d1) : {4, 5, 6, 7}      // all model ranks for data row 1
num_replica_groups    = 2   (= |data|)
num_devices_per_group = 4   (= |model|)
```

This is **contiguous**, so the iota descriptor is trivial — `Create([2,4])` (`reshape_dims=[2,4]`, identity perm). `ExpandIota` chunks `arr=[0,1,2,3,4,5,6,7]` into runs of 4 → exactly the two groups above.

Now reduce over the **`data` axis** instead (`|data|=2`). Groups are devices sharing a `model` index — a **strided** set:

```text
group 0  (model=m0) : {0, 4}           // stride 4
group 1  (model=m1) : {1, 5}
group 2  (model=m2) : {2, 6}
group 3  (model=m3) : {3, 7}
num_replica_groups    = 4   (= |model|)
num_devices_per_group = 2   (= |data|)
```

Storing `{0,4},{1,5},{2,6},{3,7}` explicitly costs 8 ints; the iota form stores it as a transpose. Build `IotaTileAssignment` with `dims = [4, 2]` (the group grid `[num_groups][group_size]`), `reshape_dims = [2, 4]` (the physical mesh `[data, model]`), `transpose_perm = [1, 0]`. Hand-run `value_at` for group 1, rank 0 → tile coord `index = [1, 0]`:

```text
STEP 1  linearise over dims=[4,2] row-major:
        lin = index[0]*dims[1] + index[1] = 1*2 + 0 = 2
STEP 2  delinearise into reshape=[2,4], perm=[1,0] walked in REVERSE (k=1 then k=0):
        k=1: ax=perm[1]=0, sz=reshape[0]=2 -> coord[0]=2%2=0 ; rem=2/2=1
        k=0: ax=perm[0]=1, sz=reshape[1]=4 -> coord[1]=1%4=1 ; rem=1/4=0
        coord = [data=0, model=1]
STEP 3  re-linearise over reshape=[2,4]:
        dev = coord[0]*reshape[1] + coord[1] = 0*4 + 1 = 1
```

`value_at([1,0]) = 1` ✓ — group 1 (model=m1), first rank, is device 1. Repeat for `index=[1,1]` → `lin=3`; `coord=[1,1]`; `dev = 1*4+1 = 5` ✓. The compact descriptor reproduces `{1,5}` for group 1, matching the explicit list. Both AllReduces are `kFlattenedID` (channel set, global ids), so these device ids are global ids = `data_idx*4 + model_idx` directly.

---

## 6. Reconciliation — XLA 2-D vs Neuron 3-D Rank Model

This is the one place Neuron-authored code enters. The XLA replica-group representation is 2-D-ish; the Neuron NEFF carries a 3-D nesting. They must describe the same physical fabric.

### The two models

```text
XLA (hlo-opt, §1-§4):                         Neuron NEFF (getCCRankWorldSize):
  device id = replica*num_partitions+part        replica_groups[topology][group][rank]
  per-op topology = ONE list of groups:            - Outer : unique CC topologies
    vector<ReplicaGroup>, conceptually              - Middle: groups per CC topology
    [num_replica_groups][num_devices_per_group]     - Inner : rank size per CC
  NO third axis — all groups of one op share
  the same channel id + mode.
```

`getCCRankWorldSize` (Cython `NeffInfo.so`, **NEURON**) computes `world_size = max(ws_rg, ws_pairs, ws_expl, 1)`, where `ws_rg` is the max inner rank-size across `replica_groups` (validated consistent across topologies — an inconsistency raises *"Inconsistent worker count … MPMD"*), `ws_pairs` is the span of `src_target_pairs` (collective-permute), and `ws_expl` is the `#rank_world_size` literal override.

### The bridge — how XLA's 2-D groups become Neuron data

> **Origin: Neuron-authored.**

1. XLA emits collectives whose `CollectiveDeviceList → replica_groups()` ([§1](#1-the-replica-group-representation)) yields the explicit `vector<ReplicaGroup>`. `ConvertCollectivesToCustomCall` copies `HloInstruction::replica_groups()` **verbatim** into the `AwsNeuron*` custom call — device ids preserved bit-for-bit.
2. `xla::GetReplicaGroups(HloInstruction*)` `@0x1f8ca40` (stock) reads `inst->replica_groups()` (which lazily `ExpandIota`s) and flattens each `ReplicaGroup`'s `replica_ids` (count@+0x10, data@+0x18) into `vector<vector<int64>>` — the 2-D `[group][rank]` form Neuron consumes.
3. `neuron::GetTpReplicaGroup(HloComputation*)` `@0x1f80360` (**NEURON**) walks `MakeInstructionPostOrder`, matches an HLO pattern keyed on the collective opcode byte `[inst+0x14]` (e.g. immediate `0x57=87` = `kReduceScatter`), and on a match reads that collective's `replica_groups()` (`@0x965e7e0`) as the canonical TP replica group. **The TP group is *discovered* from the already-partitioned graph, not recomputed from a mesh.** The `HloModule` overload `@0x1f808f0` takes a `flat_hash_set<string_view>` of names to scan and aggregates. `neuron::HasMatchingReplicaGroups(inst, vector<ReplicaGroup>)` `@0x1f7e060` (**NEURON**) gates combiners by checking an instruction's groups equal a reference set. (Same symbols in `hlo2penguin`: `GetTpReplicaGroup` `@0x205cbf0`, `HasMatchingReplicaGroups` `@0x205a8f0`.)
4. The Neuron combiners use `vector<vector<long>>` groups as part of the combine **key** — `NeuronReduceScatterCombiner`'s key tuple is `<HloOpcode, PrimitiveType, dim, bool, bool, vector<vector<long>> groups>` — so two collectives merge only if their replica groups match exactly. The 2-D group list *is* a collective's topology identity, end to end.

### Dimensionality map

```text
Neuron INNER  (rank size per CC)        <- XLA num_devices_per_group
Neuron MIDDLE (groups per CC topology)  <- XLA num_replica_groups
Neuron OUTER  (unique CC topologies)    <- DISTINCT collectives with different group geometry
```

XLA has no outer "topology" axis at the op level; it appears at NEFF-assembly time as the **set of distinct group shapes** in the module — `writeCCInfo` `@0x1523af0` (libwalrus, **NEURON**) scans all `InstCollectiveCompute` ops and de-dups their group geometries. For single-program SPMD (the supported case) every collective shares one consistent geometry, so the outer dim is effectively length 1 and Neuron's 3-D list degenerates to the XLA 2-D list at `topology[0]`. `getCCRankWorldSize` rejects the `>1`-topology case as MPMD (*"Empty topology found at index {} … possible MPMD neff"*, *"Inconsistent worker count across topologies … MPMD"*); the BIR simulator likewise asserts `replica_groups.size()==1` / *"Multiple LNCs are not supported yet"* for `ReduceScatter`/`SendRecv`.

> **NET —** there is **no genuine 2-D-vs-3-D mismatch** in the supported flow. XLA's `(num_replica_groups, num_devices_per_group)` is exactly Neuron's `(middle, inner)` at the single topology XLA produces; the outer axis is a NEFF-level superset for MPMD/multi-distinct-CC neffs that stock single-program SPMD never emits. The two data shapes are identical where they meet; reading the outer axis as "distinct group shapes" follows from the `writeCCInfo` de-dup plus the MPMD guards rather than from one decisive instruction.

### Rank coordinate map

> **Origin: cross-binary, simulator side.**

```text
XLA: device id in a replica_group = flattened global id (kFlattenedID:
        replica_id*num_partitions + partition_id) OR partition id (kCrossPartition).
Neuron sim:
        rank-of-core  = core / numCoresPerLNC      // NeuronCoresManager::getRankIdforCore @0x272380
        numReplicas   = replica_groups[0].size()   // inner group size, per-op
        participation = isInReplicaGroup(core)     // @0x1aa460 : linear scan of replica_groups[0]
                                                   //             empty => all participate
```

Neuron maps a *physical core* to a rank by integer-dividing out the LNC core count, then looks the rank up in `replica_groups[0]` (the single topology). **The XLA replica-group device ids *are* these ranks** — the ids XLA places in each group are the ranks Neuron scans for:

> `XLA replica_group entry  ==  Neuron CC rank id  ==  (physical core / numCoresPerLNC)`

`GetGlobalRankId` (IT11) emits the precomputed global rank (`InstVisitor+0xDE8`); `GetCurProcessingRankID` (IT66) resolves the TP rank *within* the group from `(iter_id, channel_id, replica_groups[0], arch-discriminant)` via `sub_1BA6A0` `@0x1ba6a0`, indexing the XLA-supplied group list against a per-arch precomputed TP-rank table (`qword_22969A0`: ArchLevel × groupSize → rank permutation). The full 3-D rank model is documented in [13.8](three-d-rank-model.md).

### Collective-permute — the pair-based exception

`collective-permute` carries **no** `replica_groups`; it carries `src/target` pairs. XLA derives device groups from the pairs via `GetParticipatingDevicesGroupsForSourceTargetPairs` `@0x1f7b0e0`; Neuron's `getCCRankWorldSize` derives world size from the `src_target_pairs` span, and the sim asserts *"Permute should not contain replica_groups"* and routes purely by pairs. So collective-permute is the one collective whose topology is pair-based, not group-based, **on both sides consistently**.

---

## 7. Stock-XLA vs Neuron — Explicit Boundary

**STOCK upstream XLA** (`xla::`; nobody at Neuron authored these):

- `CollectiveDeviceList` (iota/explicit dual form, lazy `ExpandIota`) — [§1](#1-the-replica-group-representation), [§2](#2-the-compact-strided-form--iotareplicagrouplist--iotatileassignment)
- `IotaReplicaGroupList` / `IotaTileAssignment` (`value_at` reshape/transpose math) — [§2](#2-the-compact-strided-form--iotareplicagrouplist--iotatileassignment)
- `GetCollectiveOpGroupMode` (the `channel_id` × `use_global_device_ids` table) — [§4](#4-channel_id--use_global_device_ids)
- `GetParticipatingIDs` / `GetParticipatingDevicesGroups[ForSourceTargetPairs]` — [§4](#4-channel_id--use_global_device_ids), [§6](#6-reconciliation--xla-2-d-vs-neuron-3-d-rank-model)
- `GetReplicaGroups(HloInstruction*)` (flatten to `vector<vector<long>>`) — [§6](#6-reconciliation--xla-2-d-vs-neuron-3-d-rank-model)
- `hlo_query::NextChannelId`; the bridge iota encoding — [§3](#3-world-size-and-group-derivation), [§4](#4-channel_id--use_global_device_ids)

**NEURON-authored** (the only Neuron code in this strand):

- `neuron::GetTpReplicaGroup(HloComputation*/HloModule*)` `@0x1f80360`/`@0x1f808f0`
- `neuron::HasMatchingReplicaGroups` `@0x1f7e060`
- the Neuron combiner passes that key on replica groups
- the NEFF-side 3-D `replica_groups` reader `getCCRankWorldSize` (`NeffInfo.so`) + `writeCCInfo` producer `@0x1523af0` (libwalrus) + the BIR sim rank model

**Honest flag:** the replica-group *math* and the 2-D representation are entirely stock XLA. Neuron invents no replica-group encoding. Its contribution is (a) the `--lnc` mesh cardinality that seeds `num_partitions`, (b) discovering/canonicalising the TP group from the partitioned graph (`GetTpReplicaGroup`), (c) keying its combiners on the groups, and (d) the NEFF-level 3-D `[topology][group][rank]` envelope (single-topology in the supported flow; `>1` topology = MPMD = rejected). The "3-D vs 2-D" is reconciled, not a true mismatch.

---

## Symbol / Address Quick-Reference

`hlo-opt` cp310 unless noted. VAs are direct disasm addresses.

| Symbol | VA | Tag |
|---|---|---|
| `CollectiveDeviceList::ctor(Span<ReplicaGroup>)` | `0x9667ef0` | STOCK |
| `CollectiveDeviceList::ctor(Span<vector<long>>)` | `0x1f7aa90` | STOCK |
| `CollectiveDeviceList::ctor(RepeatedPtrIterator)` | `0x9623620` | STOCK |
| `CollectiveDeviceList::replica_groups()` | `0x96240f0` | STOCK |
| `CollectiveDeviceList::Print / ToString` | `0x9624300` / `0x9624290` | STOCK |
| `IotaReplicaGroupList::num_replica_groups()` | `0x9622500` | STOCK |
| `IotaReplicaGroupList::num_devices_per_group()` | `0x9622510` | STOCK |
| `IotaReplicaGroupList::flattened_replica_groups()` | `0x9622af0` | STOCK |
| `ExpandIota(IotaReplicaGroupList const&)` | `0x9623c00` | STOCK |
| `IotaTileAssignment::ctor(dims,reshape,perm)` | `0x97a1ac0` | STOCK |
| `IotaTileAssignment::Create(dims)` | `0x97a1b60` | STOCK |
| `IotaTileAssignment::Create(dims,reshape,perm)` | `0x97a2870` | STOCK |
| `IotaTileAssignment::value_at(index)` | `0x97a2d80` | STOCK |
| `IotaTileAssignment::ToArray()` | `0x97a38b0` | STOCK |
| `GetCollectiveOpGroupMode(bool,optional<bool>)` | `0x9163ae0` | STOCK |
| `GetCollectiveOpGroupMode(HloInstruction*)` | `0x91649b0` | STOCK |
| `CollectiveOpGroupModeToString` | `0x9163c50` | STOCK |
| `GetParticipatingIDs(mode,id,cnt,groups)` | `0x9164f90` | STOCK |
| `GetParticipatingDevicesGroups(HloInstruction*)` | `0x9167cf0` | STOCK |
| `GetParticipatingDevicesGroupsForSourceTargetPairs` | `0x1f7b0e0` | STOCK |
| `hlo_query::NextChannelId` | `0x8ab1ac0` | STOCK |
| `HloInstruction::replica_groups()` | `0x965e7e0` | STOCK |
| `xla::GetReplicaGroups(HloInstruction*)` | `0x1f8ca40` | STOCK |
| `neuron::GetTpReplicaGroup(HloComputation*)` | `0x1f80360` | **NEURON** |
| `neuron::GetTpReplicaGroup(HloModule*, set<sv>)` | `0x1f808f0` | **NEURON** |
| `neuron::HasMatchingReplicaGroups` | `0x1f7e060` | **NEURON** |
| `GetTpReplicaGroup` / `HasMatchingReplicaGroups` (hlo2penguin) | `0x205cbf0` / `0x205a8f0` | **NEURON** |
| `writeCCInfo` (libwalrus) | `0x1523af0` | **NEURON** |
| `NeuronCoresManager::getRankIdforCore` (birsim) | `0x272380` | **NEURON** |
| `isInReplicaGroup` (birsim) | `0x1aa460` | **NEURON** |
| `SpmdPartitioner ctor` (`+0x08` nparts, `+0x10` nrepl) | `0x2a93280` | STOCK |
