# Tree-Barrier Vsync

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the two-phase `BarrierCoresTree` sweep, the `Vsync`/`VsyncAdd`/`VsyncAddRemote` builder→creator→MLIR-op chain, and the `InfoTable` build/read arithmetic are byte-exact (cross-checked against the demangled symbol signatures); the SMEM `Replica/PartitionIdLocationWordOffset` literal values and the 2D `DeviceAssignment` flatten ordering are LOW (memfile / partially decoded, see §6) · **Part XIII — On-Pod Collectives & Barriers** / SFLAG & barriers · [back to index](../index.md)

## Abstract

The `net_util` **tree-barrier** is the TensorCore cross-core rendezvous that does **not** route through a single coloring-assigned per-key SFLAG. It is the actuation tier underneath two distinct topologies the jellyfish lowering emits:

- **`BarrierCoresTree`** @`0x1c6a75c0` — the *cross-core* tree barrier (all-cores / replicated / partitioned). It walks a routing-table-indexed peer set in **two phases** (an up-sweep fan-in and a down-sweep fan-out), signalling each peer with a `VsyncAddRemote`, and it rendezvouses on the reserved **GLOBAL** TC SFLAG (`base+count+4`, `GetGlobalBarrierSyncFlagNumber`). Used by the AllGather / RingSum / RotatedPincer fusions and the runtime all-cores barriers.
- The **within-replica-group star** (`BarrierWithinReplicaGroup*`) — a *flat* fan-in-to-master-then-fan-out-from-master barrier over one replica group, lowered by the AllToAll emitter. It is **not** a binomial tree; it is documented on [Replica (type-2) Barrier](replica-barrier.md). This page covers only its *Vsync actuation* (the same `VsyncAddRemote`/`VwaitGeSV`/`VsyncAdd` primitives) and the `InfoTable` that supplies its peer set, because the tree-barrier and the star share the same primitive layer.

Both topologies are built from one three-instruction primitive family — `VsyncAddRemote` (signal a *peer's* sflag over ICI), `VsyncAdd` (add to the *local* sflag, used to reset), `VwaitGeSV` (block until a sflag reaches a threshold) — and both enumerate their peer cores through the **replica-group `InfoTable`**: a compile-time `int32` array indexed by the flattened global `device_id`, storing each device's ordinal within its replica group, read back at emit-time from SMEM via the current core's `(replica_id, partition_id)`.

This page owns three things: **(1)** the tree sweep (the `BarrierCoresTree` two-phase up/down walk), **(2)** the Vsync primitive family (builder → `LloInstruction` creator → MLIR `llo` op), and **(3)** the `InfoTable` layout + the on-wire indexing the walks consume. The barrier *kind selection* (`BarrierConfig` `GLOBAL`/`REPLICA`/`CUSTOM`) is upstream — see [Barriers Overview](overview.md), [Infer Barrier Config](infer-barrier-config.md); the global SFLAG slot is on [Global-Barrier SFLAG Window](global-barrier-window.md); the SFLAG memref binding is on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md).

For reimplementation, the contract is:

- **Two actuation paths, one primitive family.** The cross-core `BarrierCoresTree` (routing-table tree, GLOBAL sflag) and the per-replica-group star (`InfoTable` peer set, per-key/REPLICA sflag) are separate emitters but emit the identical `VSyncAddRemoteOp` / `VSyncAddOp` / `VWaitGeOp` ops. The tree barrier is **not** layered on the star.
- **The Vsync wire format.** `VsyncAddRemote(sflag, peer_CoreLocationBase, value, bool)` folds the peer location into an ICI remote-sflag address (`EncodeRemoteSyncFlagAddress`, per-codename) and emits `VSyncAddRemoteOp`. `VsyncAdd` emits `VSyncAddOp` against the *local* sflag; `VwaitGeSV` emits `VWaitGeOp`. These are the **TensorCore** sequencer Vsync ops, disjoint from the SparseCore `sc_tpu.sync_add` family.
- **The `InfoTable` is indexed by `device_id`, stores the within-group ordinal.** Build: `CreateStaticReplicaInfoTable` → R1 `int32[replica_count × (use_partition ? partition_count : 1)]`, `table[device_id] = within_group_position`. Read: `GetReplicaGroupCoreInfo` reads this core's `(replica_id, partition_id)` from SMEM, `LoadInfoTable` converts the linear index → `(word, lane)` and `Sld`-loads the entry; the recovered ordinals → a peer `CoreLocationBase` list whose element `[0]` is the group **master**.
- **The tree shape is a loop-nesting choice.** `GetTreeBarrierInfoTable` picks `ALL_CORES` / `REPLICATED` / `PARTITIONED` by swapping which `DeviceAssignment` dimension is the outer (fixed-per-group) axis; the three tables are lazily registered once per program.

| | |
|---|---|
| **Cross-core tree** | `net_util::BarrierCoresTree` @`0x1c6a75c0` (two-phase, GLOBAL sflag, routing-table peer set) |
| **Replica-group star** | `BarrierWithinReplicaGroupStartImpl` @`0x1c698080` / `…Done` @`0x1c6984e0` / Join @`0x1c6ad400` (see [Replica Barrier](replica-barrier.md)) |
| **Signal primitive** | `LloRegionBuilder::VsyncAddRemote(LloValue*, CoreLocationBase const&, LloValue*, bool)` @`0x1d522f40` → `VSyncAddRemoteOp` |
| **Local-add primitive** | `LloRegionBuilder::VsyncAdd` @`0x1d523200` → `VSyncAddOp` (`CreateVectorSyncFlagAdd` @`0x1d4dc1c0`) |
| **Wait primitive** | `LloRegionBuilder::VwaitGeSV` @`0x1d522f80` (int form @`0x1d54c4a0`) → `VWaitGeOp` |
| **Remote-addr fold** | `EncodeRemoteSyncFlagAddress` @`0x1d54da40` (per-codename JfDf/Viperfish/Pufferfish/Ghostlite) |
| **InfoTable build** | `CreateStaticReplicaInfoTable` @`0x1c69b780` → `InfoTable{tag=1, ptr, byte_len, byte_size}` |
| **InfoTable read** | `GetReplicaGroupCoreInfo` @`0x1c698740` → `LoadInfoTable` @`0x1c69ff20` → peer `CoreLocationBase` list |
| **Tree-shape select** | `GetTreeBarrierInfoTable` @`0x1c6a8780` (ALL_CORES / REPLICATED / PARTITIONED) |
| **Global sflag source** | `Target::GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` (`base+count+4`, [Global-Barrier Window](global-barrier-window.md)) |

---

## 1. The Vsync primitive family

Every cross-core SFLAG write the TensorCore tree/star barriers emit goes through one of three `LloRegionBuilder` methods. Each is a thin builder that allocates an `LloInstruction` via a `Create…` factory and appends it to the current `LloRegion`. The factories carry the MLIR `llo` dialect op as their type; the op names are confirmed in the `Dialect::addOperations` registration list (`VSyncAddRemoteOp`, `VSyncAddOp`, `VSyncReadOp`, `VWaitGeOp`, `VWaitEqOp`).

| Builder method (VMA) | `LloInstruction` creator (VMA) | MLIR `llo` op | Role |
|---|---|---|---|
| `VsyncAddRemote(sf, loc, v, b)` @`0x1d522f40` | `CreateVectorSyncFlagAddRemote(LloValue*, LloValue*, LloRegion*)` @`0x1d4dc340` | `VSyncAddRemoteOp` | add `v` to a **peer's** sflag (the fan-in / fan-out signal) |
| `VsyncAdd(sf, v)` @`0x1d523200` | `CreateVectorSyncFlagAdd(LloValue*, LloValue*, optional<bool>, LloRegion*)` @`0x1d4dc1c0` | `VSyncAddOp` | add `v` to the **local** sflag (used as the post-wait reset) |
| `VwaitGeSV(sf, thr, b)` @`0x1d522f80` (int form @`0x1d54c4a0`) | — (`VWaitGe` lowering) | `VWaitGeOp` | block until the **local** sflag `>= thr` |

The signal builder's body is exactly the three-call chain (confirmed in the decompile of `0x1d522f40`):

```c
// LloRegionBuilder::VsyncAddRemote(LloValue* sflag, CoreLocationBase const& peer_loc,
//                                  LloValue* value, bool is_remote)        @0x1d522f40
LloValue *enc = LloRegionBuilder::EncodeRemoteSyncFlagAddress(this, sflag, peer_loc, is_remote);  // 0x1d54da40
LloInstruction *op = LloInstruction::CreateVectorSyncFlagAddRemote(enc, value, *this, region);    // 0x1d4dc340
return LloRegion::AppendInstruction(region, op, 0, region_ctx);                                    // 0x1d50f9a0
```

`EncodeRemoteSyncFlagAddress` @`0x1d54da40` folds the peer `CoreLocationBase` and the local sflag number into an ICI-routable remote-sflag address. It dispatches per chip codename — `EncodeRemoteSyncFlagAddressJfDf`, `…Viperfish`, `…Pufferfish`, `…Ghostlite` are all present in `functions.json` — so the *exact* address bit-layout is generation-specific. The high-level contract ("`VsyncAddRemote` targets a peer chosen by a `CoreLocationBase`, against the local sflag number") is gen-independent and byte-confirmed; the per-codename encoding is documented separately ([Remote SFLAG Encoders](remote-sflag-encoders.md)).

> **NOTE —** these are the **TensorCore-sequencer** Vsync ops. The SparseCore embedding barriers use a disjoint primitive family (`sc_tpu.sync_add` / `sync_wait`) against a disjoint reserved SFLAG sub-block (`SparseCoreTarget+0x1d0`). The two never share a primitive or an sflag; see [Barriers Overview §1](overview.md) and [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md).

---

## 2. `BarrierCoresTree` — the cross-core two-phase tree sweep

`net_util::BarrierCoresTree` @`0x1c6a75c0` is the all-cores / replicated / partitioned **tree** barrier. Its signature (demangled from `functions.json`) is:

```text
net_util::BarrierCoresTree(
    LloRegionBuilder,
    std::function<absl::StatusOr<LloValue*>(LloRegionBuilder, TreeBarrierType)>,  // per-type InfoTable producer
    TreeBarrierType,
    ProgramSharedRegistry const*,
    LloValue* /*barrier_sflag, may be null*/,
    bool)
```

### 2.1 The sflag: GLOBAL slot, not a per-key id

When the caller passes a null `barrier_sflag`, `BarrierCoresTree` materialises the reserved **GLOBAL** TC barrier slot itself (confirmed in the decompile):

```c
int global = Target::GetGlobalBarrierSyncFlagNumber(target);          // 0x1d60f420 → base+count+4
LloValue *sf = LloRegionBuilder::SflagImmPtr(b, global, "global barrier sync flag", 24);
```

This is the reserved top-5 slot `base+count+4` (the same slot the GLOBAL `BarrierConfig` lowers to — see [Global-Barrier SFLAG Window](global-barrier-window.md)). The tree barrier therefore rendezvouses on a *single, program-wide* sflag, not on a coloring-assigned per-key id. A `TreeBarrierType::kAll` legality CHECK (`.rodata` string `"barrier_type == TreeBarrierType::kAll"`) gates the all-cores path.

### 2.2 The two-phase sweep (up-sweep fan-in, down-sweep fan-out)

The tree walk is emitted as **two phases**, each annotated in the `.rodata` strings `"tree-barrier-phase-1"` and `"tree-barrier-phase-2"`. Each phase computes a per-core routing-table index and signals its tree-selected peers in a `SimpleLoop` over a `0x10`-byte stride (the `CoreLocationBase`-sized element of the routing entry):

```c
// PHASE 1 (up-sweep): signal children/parent toward the tree root
GetLimitedIciRoutingTableIndex(idx, …, "tree-barrier-phase-1", 20, …);   // 0x1c6a5e80
region1 = LloRegionBuilder::SimpleLoop(b, n1, /*start*/1, /*stride*/16, /*step*/1);   // 0x1d57d4a0
  VsyncAddRemote(b, value, peer, sflag, 0, …);                            // 0x1d522f40 → VSyncAddRemoteOp

// PHASE 2 (down-sweep): release back down the tree
GetLimitedIciRoutingTableIndex(idx, …, "tree-barrier-phase-2", 20, …);   // 0x1c6a5e80
region2 = LloRegionBuilder::SimpleLoop(b, n2, 1, 16, 1);                  // 0x1d57d4a0
  VsyncAddRemote(b, value, peer, sflag, 0, …);                            // VSyncAddRemoteOp
```

The global rendezvous itself uses the wait + reset primitives in the same body: `VwaitGeSV(sflag, thr)` (`VWaitGeOp`) blocks until the up-sweep arrivals have accumulated, then `VsyncAdd(sflag, …)` (`VSyncAddOp`) resets the local sflag before the down-sweep release. This is the canonical up-sweep/down-sweep barrier: arrivals fan **in** over the routing tree to the rendezvous point, the core waits-ge on the GLOBAL sflag, then releases fan **out** over the same tree.

`GetLimitedIciRoutingTableIndex` @`0x1c6a5e80` reads the per-core `Replica/PartitionIdLocationWordOffset` SMEM words and bit-packs them (the `SandU32`/`SshrlU32`/`Por` chain) into the routing-table index that selects the phase's peer slice — i.e. the *tree shape* the walk follows is data-driven by the routing table, not a static binomial fan-out degree.

### 2.3 The Custom variant

A `BarrierCoresTreeCustom` closure (`.rodata` `"custom-tree-barrier"`) is a second entry that takes the `TreeBarrierType` directly from the module config rather than the arg, gated on the program's megachip/megacore module flags. It emits the same two-phase `VsyncAddRemote` walk; only the `TreeBarrierType` *source* differs.

> **GOTCHA —** `BarrierCoresTree` is **not** the within-replica-group barrier. The AllToAll within-group barrier is a flat star (one master, `peer[0]`) and lives in `BarrierWithinReplicaGroup*` ([Replica Barrier](replica-barrier.md)). `BarrierCoresTree` is the cross-core tree used by AllGather / RingSum / RotatedPincer and the runtime all-cores barriers. Both end at the same `VSyncAddRemoteOp` — but the peer set, the sflag, and the topology differ.

---

## 3. The Vsync star (within-replica-group) — the primitive-level view

The replica-group barrier ([Replica Barrier](replica-barrier.md)) is included here only at the *primitive* level, because it is the simplest exhibit of the Vsync family and because it consumes the same `InfoTable` (§4). It is a two-half flat star over one barrier sflag `N` = group size:

```text
NON-MASTER half  (BarrierWithinReplicaGroupStartImpl @0x1c698080):
  if N <= 1: install no-op Join, return        // singleton group ⇒ no barrier
  peers = GetReplicaGroupCoreInfo(InfoTables…)  // peers[0] = group MASTER
  Predicated(Pneg(participation_pred))          // only non-masters run the signal
  (verify) ScheckGe(0)/ScheckLt(chip_count)/ScheckNe(self) on master chip id
  VsyncAddRemote(barrier_sflag, MASTER=peers[0], +1, false)     // +1 → master's sflag

MASTER half  (BarrierWithinReplicaGroupDone @0x1c6984e0 / Join @0x1c6ad400):
  Predicated(participation_pred)                // only the master runs wait+release
  VwaitGeSV(barrier_sflag, N-1)  [annotation "replica-group-barrier-wait"]   // all non-masters arrived
  VsyncAdd(barrier_sflag, 1-N)                  // reset the sflag back to 0
  for peer in peers[1..]: (ScheckGe/Lt/Ne guards)
      VsyncAddRemote(peer.barrier_sflag, +1, false)            // release each waiting non-master
```

The decompile of both halves matches byte-for-byte: `StartImpl` @`0x1c698080` runs `GetReplicaGroupCoreInfo` → `Pneg`/`Predicated` → `ScheckGe`/`ScheckLt` → `ToGlobalCoreId`/`GlobalCoreId`/`ScheckNe("Non-master core has same location as master core!")` → `VsyncAddRemote`; the Join @`0x1c6ad400` runs `Predicated` → `VwaitGeSV` (annotated `"replica-group-barrier-wait"`) → `VsyncAdd` (reset) → a per-peer loop of `ScheckGe`/`ScheckLt`/`ScheckNe("Barriering with self!")` + `VsyncAddRemote`.

> **NOTE —** the `count >> 1` capture in the `StartImpl` closure is the `SmallVector` inline-capacity split for the peer span, **not** a tree fan-out degree. The within-group barrier has no tree halving — it is `N-1` remote-adds in, one wait-ge, `N-1` remote-adds out. The binomial table (`CreateStaticBinomialReplicaInfoTable` @`0x1375efa0`) is a *different* structure that feeds the AllReduce/AllGather ring/pincer fusions ([Binomial Recursive Doubling](../collectives/binomial-recursive-doubling.md)), not this barrier.

---

## 4. The replica-group `InfoTable` — layout + indexing

Both the star (§3) and the tree (§2) enumerate their peer cores through `InfoTable`s — compile-time `int32` arrays read at emit-time from SMEM. The build side and the read side are symmetric.

### 4.1 Build — `CreateStaticReplicaInfoTable` @`0x1c69b780`

```c
InfoTable CreateStaticReplicaInfoTable(
    Span<ReplicaGroup> groups, long replica_count, long partition_count,
    bool b4, bool use_partition, DeviceAssignment*):

  table_len = replica_count * (use_partition ? partition_count : 1)   // entries, int32 each
  int32 *table = operator new(16 * table_len); memset(table, 0, 16 * table_len)
  // (over-allocates 16 B/entry — the SmallVector-backed alloc path; only the int32 lanes are used)
  for each ReplicaGroup g (stride 0x30):
      n   = g.replica_ids_size [g+0x1c]
      ids = g.replica_ids (inline [g+0x18] or heap [g+0x20])
      for k in [0, n):
          device_id = ids[k]                    // CHECK 0 <= device_id < table_len
          table[device_id] = k                  // 1D: store the within-group ORDINAL
          // 2D / use_partition: also div/mod device_id by partition_count and
          //   flatten through the DeviceAssignment dims (imul/add chain @0x1c69bb70),
          //   bounds-checked via proto2::internal::LogIndexOutOfBoundsAndAbort @0x21063300
  return InfoTable{ tag=1 [+0], data_ptr [+8], byte_len=table_len*4 [+0x10], byte_size=table_len*4 [+0x18] }
  // NOTE: in the decompile [+0x10] is stored as (16*table_len)>>2 == table_len*4 — a BYTE length,
  //       equal to [+0x18], not the int32 element count (table_len).
```

The decompile confirms the signature, the `memset`, the per-`ReplicaGroup` walk, the `LogIndexOutOfBoundsAndAbort` bounds checks, and the `InfoTable{tag,ptr,count,bytes}` result shape. **The table is indexed by the flattened global `device_id` and stores that device's ordinal within its replica group** (the 1D form). There is one such table per `(replica_count × partition_count)` topology, derived directly from the HLO collective's `replica_groups` attribute (and the `DeviceAssignment` for the 2D form). `CreateReplicaInfoTable` @`0x1c69b660` is the thin wrapper.

### 4.2 Read — `GetReplicaGroupCoreInfo` @`0x1c698740` + `LoadInfoTable` @`0x1c69ff20`

At emit time, per core, `GetReplicaGroupCoreInfo` (cached under `replica_group_core_info_cache_mutex` @`0x22579828`, keyed by hlo/module/the InfoTables/longs/bool) recovers the current core's group peers:

```c
// 1. read this core's identity from SMEM
replica_id   = Sld(SmemWordImmPtr(Target::ReplicaIdLocationWordOffset(),   "replica id location"));    // 0x1d617c80
partition_id = Sld(SmemWordImmPtr(Target::PartitionIdLocationWordOffset(), "partition id location"));  // 0x1d617ca0

// 2. LoadInfoTable: linear element index -> (word, lane), then Sld the int32 entry
//    element width = (InfoTable[+0xb] >> 2) & 0x1F   (= s32)
//    stride        = SmemWordSizeBytes / WordSizeBytes(MemorySpace)
//    (word, lane)  = SdivmodU32(SmulU32(index, stride))
entry = LoadInfoTable(b, index, dtype, InfoTable);            // 0x1c69ff20

// 3a. 1D path  (optional 3rd table absent): one LoadInfoTable; build the peer
//     CoreLocationBase list (0x18-byte stride) via FromGlobalCoreId.            // 0x1d51b340
// 3b. 2D path  (3rd InfoTable present / partition flag): LoadInfoTable twice
//     (replica table + partition table) -> ReplicaAndPartitionId pairs ->
//     GetCoreLocations @0x1c69a580 -> peer CoreLocationBase list.
// peers[0] = the group MASTER.
```

The decompile of `LoadInfoTable` confirms the element-width expression `(*(byte*)(infotable+11) >> 2) & 0x1F`, the `WordSizeBytes` / `SmemWordSizeBytes` stride, and the `SmulU32`/`SdivmodU32` index→`(word, lane)` conversion. The decompile of `GetReplicaGroupCoreInfo` confirms the `ReplicaIdLocationWordOffset` / `PartitionIdLocationWordOffset` SMEM reads via `SmemWordImmPtr` + `Sld`, the 1D single / 2D double `LoadInfoTable` calls, and `FromGlobalCoreId`.

> ⇒ A `(replica_id, partition_id)` pair indexes the SMEM-resident `InfoTable` to recover the current core's within-group ordinal; that ordinal enumerates the group's peer `CoreLocation`s. `peers[0]` is the master the star (§3) signals/waits on.

### 4.3 The two tables + the optional third

`GetReplicaGroupCoreInfo` and the barrier emitters take `(InfoTable& A, InfoTable& B, optional<InfoTable> C)`:

| Arg | Table | Source |
|---|---|---|
| `A` | replica-axis table | `CreateStaticReplicaInfoTable` (replica fill) |
| `B` | partition-axis table | `CreateStaticReplicaInfoTable` (partition variant) |
| `C` (optional) | 3D / limited-ICI-routing table | `CreateStaticReplicaInfoTableForLimitedIciRouting` @`0x1c69c120` |

The cache hash combines `(InfoTable, InfoTable, long, long, bool, long)` @`0x1c6acb20` (both tables + `replica_count` + `partition_count` + a flag + a long). The tables themselves are carried as HLO constants (`GetConstantTables` returns the `StatusOr<tuple<InfoTable, InfoTable, optional<InfoTable>>>` consumed by the AllToAll emitter — see [All-to-All Tables](../collectives/alltoall-tables.md)).

---

## 5. Tree-table shape — `GetTreeBarrierInfoTable` @`0x1c6a8780`

The cross-core tree barrier's *grouping* (which cores rendezvous together) is a single `DeviceAssignment` loop-nesting choice. `GetTreeBarrierInfoTable(TpuTopology, DeviceAssignment, TreeBarrierType, …)` reads the assignment dims `DA[+0]=replica_count`, `DA[+8]=partition_count`, computes `total = replica_count × partition_count`, and nests two loops whose order is set by the `TreeBarrierType`:

| `TreeBarrierType` | r8d | registry key (`.rodata`) | outer loop (fixed per group) | inner loop | each group = cores that … |
|---|---|---|---|---|---|
| `ALL_CORES` (0) | 0 | `<all-cores-tree-barrier-info-table>` @`0xb433290` | (single flat group over all device ids) | — | every core in the program |
| `REPLICATED` (1) | 1 | `<replicated-cores-tree-barrier-info-table>` @`0xb4332c0` | `partition_count` | `replica_count` | share a partition, differ in replica (sync across replicas of one partition) |
| `PARTITIONED` (2) | 2 | `<partitioned-cores-tree-barrier-info-table>` @`0xb4332f0` | `replica_count` | `partition_count` | share a replica, differ in partition (sync the model-parallel shards of one replica) |

The **only** difference between `REPLICATED` and `PARTITIONED` is which `DeviceAssignment` dimension is the outer (fixed-per-group) axis: `REPLICATED` fixes the partition and groups across replicas; `PARTITIONED` fixes the replica and groups across partitions. Both produce a `Span<pair<long,long>>` of `(replica, partition)` pairs fed to the `$_2` pair→core-id closure @`0x1c6a94c0` → `GetCoreLocations` @`0x1c69a580`. The decompile confirms the three `$_2`/`$_3`/`$_4` closures and the `TreeBarrierType`-keyed `LogMessage` dispatch.

The three tables are keyed and registered once per program:

- `GetOrCreateTreeBarrierInfoTable` @`0x1c6b60e0` maps the type → the registry string key: `r8d==2` → tag `0x2b` `<partitioned-…>`; `==1` → tag `0x2a` `<replicated-…>`; else tag `0x23` `<all-cores-…>` (the tags are the key-string lengths).
- `RegisterTreeBarrierInfoTables` @`0x1c6a8620` lazily registers all three lazy-InfoTable closures (`0x1c6b60a0` / `0x1c6b66c0` / `0x1c6b6700`) into the `ProgramSharedRegistry` via `AddValue` @`0x1c8dad80`.

> **GOTCHA —** the `DeviceAssignment` dim convention `[+0]=replica_count, [+8]=partition_count` has **byte-confirmed offsets** (`movslq DA[+0]` / `DA[+8]`) but the dimension *names* are attributed from the `REPLICATED`/`PARTITIONED` loop-nesting semantics and the standard XLA `DeviceAssignment [replica, computation]` shape — not from a struct field descriptor. The *behaviour* (which axis is outer per type) is certain.

---

## 6. The end-to-end actuation datapath

| Stage | Function (VMA) | Output |
|---|---|---|
| BarrierConfig → sflag | `net_util::GetBarrierSyncFlag` @`0x1c69ad00` | TC barrier sflag = `*(Target+0x8c0)+id` ([Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md)) |
| membership table (build) | `CreateStaticReplicaInfoTable` @`0x1c69b780` | R1 `int32[replica×partition]`, `table[dev]=pos` |
| membership read (per core) | `GetReplicaGroupCoreInfo` @`0x1c698740` | peer `CoreLocationBase` list (`peer[0]`=master) |
| ↳ scalar table load | `LoadInfoTable` @`0x1c69ff20` | `Sld` of the SMEM `int32` entry |
| START (non-master signal) | `BarrierWithinReplicaGroupStartImpl` @`0x1c698080` | `VSyncAddRemoteOp(+1)` → master |
| DONE (master wait+release) | `…Done` @`0x1c6984e0` / Join @`0x1c6ad400` | `VWaitGeOp(N-1)` + `VSyncAddOp(1-N reset)` + `N-1× VSyncAddRemoteOp(+1)` → peers |
| cross-core tree (two-phase) | `BarrierCoresTree` @`0x1c6a75c0` | up-sweep + down-sweep over `GetLimitedIciRoutingTableIndex`, GLOBAL sflag, `VSyncAddRemoteOp` |
| tree-table shape select | `GetTreeBarrierInfoTable` @`0x1c6a8780` | `ALL_CORES` / `REPLICATED` / `PARTITIONED` groups |
| global sflag slot | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` | `base+count+4` ([Global-Barrier Window](global-barrier-window.md)) |

---

## 7. Verification notes

> **[CONFIRMED]** Byte-exact in `libtpu.so` v0.0.40 (cross-checked against the demangled symbol table):
> - `VsyncAddRemote` @`0x1d522f40`: body is exactly `EncodeRemoteSyncFlagAddress` @`0x1d54da40` → `CreateVectorSyncFlagAddRemote` @`0x1d4dc340` → `LloRegion::AppendInstruction` @`0x1d50f9a0` — exact.
> - The Vsync creators: `CreateVectorSyncFlagAddRemote(LloValue*, LloValue*, LloRegion*)` and `CreateVectorSyncFlagAdd(LloValue*, LloValue*, optional<bool>, LloRegion*)` present in `functions.json`; MLIR ops `VSyncAddRemoteOp` / `VSyncAddOp` / `VWaitGeOp` / `VWaitEqOp` / `VSyncReadOp` confirmed in the `addOperations` registration list.
> - `BarrierCoresTree` @`0x1c6a75c0`: the GLOBAL-sflag materialisation (`GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` + `SflagImmPtr "global barrier sync flag"`); the **two-phase** sweep (`.rodata` `"tree-barrier-phase-1"` / `"tree-barrier-phase-2"`, each `GetLimitedIciRoutingTableIndex` @`0x1c6a5e80` + `SimpleLoop` @`0x1d57d4a0` stride `0x10` + `VsyncAddRemote`); the `VwaitGeSV` + `VsyncAdd` rendezvous; the `BarrierCoresTreeCustom` `"custom-tree-barrier"` variant — confirmed.
> - The star (`StartImpl` @`0x1c698080` / Join @`0x1c6ad400`): the `Pneg`/`Predicated` gate, the `ScheckGe`/`ScheckLt`/`ScheckNe` guards (`"Non-master core has same location as master core!"`, `"Barriering with self!"`), `VwaitGeSV` annotated `"replica-group-barrier-wait"`, the `VsyncAdd` reset, the per-peer `VsyncAddRemote` loop — confirmed byte-for-byte.
> - `CreateStaticReplicaInfoTable` @`0x1c69b780`: the `Span<ReplicaGroup>, long, long, bool, bool, DeviceAssignment*` signature, the `memset`, the per-group fill, the `LogIndexOutOfBoundsAndAbort` @`0x21063300` bounds checks, the `InfoTable{tag=1, ptr, byte_len=4·table_len, byte_size=4·table_len}` result (the `[+0x10]` field is a byte length `(16·table_len)>>2`, **not** the element count) — confirmed.
> - `LoadInfoTable` @`0x1c69ff20`: element width `(*(byte*)(table+0xb) >> 2) & 0x1F`, `WordSizeBytes` / `SmemWordSizeBytes`, `SmulU32` + `SdivmodU32` index→`(word,lane)` — exact.
> - `GetReplicaGroupCoreInfo` @`0x1c698740`: `Replica/PartitionIdLocationWordOffset` via `SmemWordImmPtr` + `Sld`, 1D-single / 2D-double `LoadInfoTable`, `FromGlobalCoreId` — confirmed.
> - `GetTreeBarrierInfoTable` @`0x1c6a8780`: the `TpuTopology, DeviceAssignment, TreeBarrierType` signature and the three pair-mapping closures — confirmed; the registry keys / tags in `GetOrCreateTreeBarrierInfoTable` @`0x1c6b60e0` — confirmed.
>
> **[LOW / partially decoded]**
> - The literal `Target::ReplicaIdLocationWordOffset()` @`0x1d617c80` / `PartitionIdLocationWordOffset()` @`0x1d617ca0` *values* — the SMEM word offsets each core reads to learn its own `(replica_id, partition_id)` at runtime — are filled from the chip-config / boot path (an embedded-memfile dependency); only their *use* is confirmed, not the accessor bodies.
> - The 2D `DeviceAssignment`-aware fill in `CreateStaticReplicaInfoTable` (the `imul`/`add` multi-dim flatten @`0x1c69bb70`): the 1D path (`table[dev]=pos`) is byte-exact; the exact dim ordering of the 2D flatten was followed structurally but not fully pinned.
> - The `GetTreeBarrierInfoTable` `$_2` pair→global-core-id closure @`0x1c6a94c0` reaches `GetCoreLocations` @`0x1c69a580` but the precise `(replica, partition)` → core-id flattening (vs the replica-group tables) was sampled, not fully decoded.
> - `Target+0x3b8`→`[+0x70]` (the master-chip-id `ScheckLt` upper bound used by `StartImpl`): the offsets are confirmed; the "chip count" role is attributed from the `"Master core chip id underflow/overflow"` assert strings, with no standalone named accessor.

---

## Cross-References

**Barrier algorithms (this section)**
- [Barriers Overview](overview.md) — the `BarrierType` model and the producer→normaliser→lowering flow that selects which barrier this tier actuates
- [Replica (type-2) Barrier](replica-barrier.md) — the within-replica-group flat-star barrier whose `Vsync` actuation + `InfoTable` peer set are described here
- [Global-Barrier SFLAG Window](global-barrier-window.md) — the `base+count+4` GLOBAL slot `BarrierCoresTree` rendezvouses on
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — `GetBarrierSyncFlag` / `BarrierConfig` → chip SFLAG memref
- [Infer Barrier Config](infer-barrier-config.md) — the `CUSTOM → GLOBAL/REPLICA` normaliser that decides the sflag kind
- [TensorCore Barrier](tensorcore-barrier.md) — the TC-substrate signal/wait barrier and coloring-chosen `CUSTOM` ids
- [Remote SFLAG Encoders](remote-sflag-encoders.md) — the per-codename `EncodeRemoteSyncFlagAddress` ICI address fold

**Sibling subsystems**
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the SFLAG atomic-counter substrate every Vsync op writes
- [All-to-All Tables](../collectives/alltoall-tables.md) — the AllToAll emitter that supplies the barrier's `GetConstantTables` `InfoTable`s
- [Binomial Recursive Doubling](../collectives/binomial-recursive-doubling.md) — the *separate* binomial table (`CreateStaticBinomialReplicaInfoTable`) feeding the reduce/gather fusions, not this barrier
- [Collectives](../collectives/overview.md) — the collective ops that consume these barriers
- [back to index](../index.md)
