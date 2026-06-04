# Replica-Group Barrier Emission

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`BarrierWithinReplicaGroupStartImpl` @`0x1c698080` is the TensorCore (TC) actuator that emits a per-replica-group rendezvous: a single core in each replica group signals each of its group peers' barrier sync-flag, then a deferred join waits for all of them. It is the leaf of the `REPLICA(2)` lowering and — with a wider peer set — the leaf of the `GLOBAL(1)` lowering as well. Both arrive through the same `net_util::BarrierCoresTree` @`0x1c6a75c0` driver; they differ only in which `TreeBarrierType` (and therefore which peer set) the tree barrier selects. This page owns that leaf: the `GetReplicaGroupCoreInfo` peer lookup, the `Pneg`/`Predicated` master-core gating, the `ScheckGe`/`ScheckLt`/`ScheckNe` legality checks, and the `VsyncAddRemote` per-peer actuation.

The reader should already know two things from sibling pages. The classifier that decides a collective gets `REPLICA(2)` (single, non-partition replica group, no schedule conflict, not globally beneficial) lives on [Infer Barrier Config](infer-barrier-config.md). The `BarrierConfig → SFLAG` number — `base + id` for `REPLICA(2)`/`CUSTOM(3)`, `base + count + 4` for `GLOBAL(1)` — lives on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md), and the GLOBAL-window reservation plus the shared `net_util::GetBarrierSyncFlag` mapper live on [Global-Barrier SFLAG Window](global-barrier-window.md). This page does **not** re-derive the number; it takes the `LloValue* sflag` as an input and documents what the actuator does with it.

The signal/wait *protocol* — the tree fan-out structure, `GetTreeNodeRecord`, `SimpleLoop`, the `VWaitGe` wait side — is on [Tree-Barrier / vSync](tree-barrier-vsync.md). This page owns the **replica-group emission body**: how `BarrierCoresTree` consults `GetGlobalBarrierSyncFlagNumber` for the GLOBAL slot and routes a within-group `TreeBarrierType` into `BarrierWithinReplicaGroupStartImpl`, and how that impl resolves peers, gates participation to the master core, validates each peer, and emits `VsyncAddRemote(sflag, peer, +1)`.

For reimplementation, the contract is:

- **`BarrierCoresTree` is the one driver for both GLOBAL and REPLICA.** It binds the GLOBAL sflag (via `GetGlobalBarrierSyncFlagNumber`) for the ALL-cores arm, fetches the per-type `InfoTable` from the `ProgramSharedRegistry`, and threads the within-group fan-out into `BarrierWithinReplicaGroupStartImpl`. The `TreeBarrierType` arm (`kAll`/`REPLICATED`/`PARTITIONED`) selects the peer set; the SFLAG slot is supplied by the caller's `GetBarrierSyncFlag` (§1).
- **`BarrierWithinReplicaGroupStartImpl` signals; the join waits.** The Start impl resolves this core's group peers (`GetReplicaGroupCoreInfo`), predicates the body to the *master* core (`Pneg`/`Predicated`), validates each peer (`ScheckGe`/`ScheckLt`/`ScheckNe`), and emits `VsyncAddRemote(sflag, peer, +1)` per peer. It then materialises a deferred `BarrierWithinReplicaGroupJoin::$_1` heap closure (the wait side) rather than emitting the wait inline (§2).
- **The master-only gate is `Pneg(is_master) → Predicated`.** Only the group's master core (peer ordinal 0, resolved by `GetReplicaGroupCoreInfo`) runs the fan-out body; non-master cores skip the signal loop. A degenerate group (`group_size <= 1`) takes a trivial single-member fast path (`$_0`) with no remote signal at all (§2.3).
- **`VsyncAddRemote` is the actuation primitive.** A 55-byte `LloRegionBuilder` method that takes `(LloValue* sflag, CoreLocationBase const& peer, LloValue* delta, bool)` and emits one remote sync-flag add of `+1` to the peer's barrier slot. The peer `CoreLocationBase` and the `+1` delta are the whole wire content (§3).

| | |
|---|---|
| **Tree driver** | `net_util::BarrierCoresTree` @`0x1c6a75c0` (4186 bytes) — GLOBAL + REPLICA; binds GLOBAL sflag, dispatches `TreeBarrierType` |
| **Replica-group Start impl** | `net_util::(anon)::BarrierWithinReplicaGroupStartImpl` @`0x1c698080` (852 bytes) — the per-group signal fan-out |
| **Join (wait) side** | `net_util::BarrierWithinReplicaGroupDone` @`0x1c6984e0` (581 bytes); deferred `BarrierWithinReplicaGroupJoin::$_1` heap closure |
| **Peer lookup** | `net_util::(anon)::GetReplicaGroupCoreInfo` @`0x1c698740` (4283 bytes) — reads the replica-group `InfoTable`, returns master predicate + peer set |
| **Participation gate** | `LloRegionBuilder::Pneg` @`0x1d5208e0` → `LloRegionBuilder::Predicated` @`0x1d520f00` (master-only) |
| **Per-peer legality** | `ScheckGe`/`ScheckLt`/`ScheckNe` — `0 <= core_index < cores_per_chip`; `ScheckNe` msg "Non-master core has same location as master core!" |
| **Actuation** | `LloRegionBuilder::VsyncAddRemote` @`0x1d522f40` (55 bytes) — `(sflag, peer, delta=SimmS32(1), 0)` |
| **SFLAG number source** | caller's `net_util::GetBarrierSyncFlag` (`base + id`); GLOBAL slot via `GetGlobalBarrierSyncFlagNumber` — see [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) |
| **TreeBarrierType arms** | `kAll(0)` GLOBAL / `REPLICATED(1)` / `PARTITIONED(2)` — same impl, different peer set |
| **Source TU** | `platforms/xla/service/jellyfish/lowering/net_util.cc` |

---

## 1. `BarrierCoresTree` @`0x1c6a75c0` — the one driver for GLOBAL and REPLICA

`net_util::BarrierCoresTree` is the entry the dense-collective emitters reach (via their `BarrierStart` `$_3` closure) for both GLOBAL and within-replica-group barriers. The TC analog of an MPI tree barrier, it does three things before any signal is emitted: bind the GLOBAL sync-flag immediate, validate that a tree-info provider exists, and select the per-`TreeBarrierType` peer set. Its 4186-byte body then threads the within-group fan-out into `BarrierWithinReplicaGroupStartImpl`.

### 1.1 Signature and the GLOBAL-slot bind

```c
// xla::jellyfish::net_util::BarrierCoresTree(
//     LloRegionBuilder b,
//     std::function<StatusOr<LloValue*>(LloRegionBuilder, TreeBarrierType)> sflag_provider,
//     TreeBarrierType barrier_type,
//     ProgramSharedRegistry const* registry,
//     LloValue* sflag,                 // caller-supplied; null → bind the GLOBAL slot
//     bool is_start)                                              // 0x1c6a75c0
```

The decisive line: when the caller passes no explicit `sflag` (the GLOBAL path), the driver computes the global slot itself and wraps it:

```c
// 0x1c6a75c0, ~+0xa5 (decompile lines 188-191)
Target *t = b.target();
int  n     = t->GetGlobalBarrierSyncFlagNumber();         // base + count + 4
LloValue *sflag = b.SflagImmPtr(n, "global barrier sync flag", /*bits=*/24);
```

This is the **only** call to `GetGlobalBarrierSyncFlagNumber` in the replica/global lowering chain — it lives here, not in `BarrierWithinReplicaGroupStartImpl`. For `REPLICA(2)`/`CUSTOM(3)`, the caller has *already* bound `sflag = base + id` via `net_util::GetBarrierSyncFlag` and passes it in non-null, so this branch is skipped (the `if (v6) goto LABEL_8` at decompile line 171/178). The number formulas are owned by [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); this page only notes *where* the GLOBAL bind happens.

> **NOTE —** `SflagImmPtr(n, …, 24)` produces the immediate `LloValue` the actuator signals on. The `"global barrier sync flag"` annotation string is byte-confirmed in the body; for the per-id arms the caller's `SflagImmPtr` annotates `"barrier sync flag number"` ([Global-Barrier SFLAG Window](global-barrier-window.md) §1.2).

### 1.2 The `TreeBarrierType` dispatch and its annotation strings

After binding the sflag, the driver checks `tree_info_provider != nullptr` (RetCheck, `net_util.cc:3855`) and then dispatches on the `TreeBarrierType` (`v13`) to label the barrier and select its `InfoTable`. The arm is observable directly from the annotation strings assigned to the wait region:

| `TreeBarrierType` | Value | Peer set | Wait annotation (byte-confirmed) | InfoTable variant |
|---|---|---|---|---|
| `kAll` | 0 | **all cores** | `"global-barrier-wait"` / `"start-global-barrier-wait"` (if `is_start`) | `kAllCoresTreeBarrierInfoTable` @`0xb433290` |
| `REPLICATED` | 1 | **this core's replica group** | (replica-group wait region) | `kReplicatedCoresTreeBarrierInfoTable` @`0xb4332c0` |
| `PARTITIONED` | 2 | this core's partition group | `"cross-partition-barrier-wait"` (emitted on the **wait/done** arm, `!a6`); asserting it with `is_start` set is the `"!start_barrier"` RetCheck error (`net_util.cc:3908`) | `kPartitionedCoresTreeBarrierInfoTable` @`0xb4332f0` |

The `kAll` arm is the GLOBAL barrier; `REPLICATED`/`PARTITIONED` are the within-replica-group arms a `REPLICA(2)` config selects. The `InfoTable` variant is fetched from the `ProgramSharedRegistry` via `GetOrCreateTreeBarrierInfoTable` @`0x1c6b60e0`, which selects per arm by the registry-name annotation it stamps — `"<all-cores-tree-barrier-info-table>"` (len 35) for `kAll`, `"<replicated-cores-tree-barrier-info-table>"` (len 42) for `REPLICATED`, `"<partitioned-cores-tree-barrier-info-table>"` (len 43) for `PARTITIONED` — and the tables are pre-registered once per program by `RegisterTreeBarrierInfoTables` @`0x1c6a8620`. The tree fan-out then descends through `GetTreeNodeRecord` + `SimpleLoop` (the protocol on [Tree-Barrier / vSync](tree-barrier-vsync.md)) into the within-group leaf.

> **QUIRK —** the cross-group arms gate on **not-start**. For `PARTITIONED(2)`, `if (v13 == 2 && !a6)` emits the `"cross-partition-barrier-wait"` region (the wait/done arm); the `a6` (is-start) branch instead falls into a `"!start_barrier"` RetCheck (`net_util.cc:3908`) — a *start* cross-partition barrier is the structural error, not a non-start one. `REPLICATED(1)` mirrors this: `!a6` emits `"cross-replica-barrier-wait"`, `a6` hits the same `"!start_barrier"` RetCheck (`net_util.cc:3904`). Only the `kAll(0)` GLOBAL arm has both forms — `"global-barrier-wait"` (`!a6`) and `"start-global-barrier-wait"` (`a6`). A reimplementer must thread the start/done flag through the dispatch, not just the `TreeBarrierType`.

### 1.3 How `REPLICA(2)` reaches this driver

A TC dense collective with `BarrierConfig.type == 2` enters through its emitter's `BarrierStart` (e.g. `AllGatherEmitter::BarrierStart` @`0x13809520`). The type dispatch (`r15d == 2`) invokes the `$_3` closure with `TreeBarrierType = (target_predicate ^ 1)` — i.e. `REPLICATED(1)` or `PARTITIONED(2)` depending on the partition predicate. The sflag itself is `net_util::GetBarrierSyncFlag(bc) = base + id` (§1.1, non-null path). `$_3` @`0x1380a0e0` then calls `GetRegistryTreeBarrierInfoProvider` @`0x1c6a7480` followed by `BarrierCoresTree`. The classification that produces that `type=2, id=key_id` config is on [Infer Barrier Config](infer-barrier-config.md).

```text
BarrierConfig{type=2, id} ──GetBarrierSyncFlag──▶ sflag = base + id   (caller binds, passes in)
                          ──$_3(TreeBarrierType = predicate^1)──▶ BarrierCoresTree
                                                                    │  (sflag non-null → skip GLOBAL bind)
   GetOrCreateTreeBarrierInfoTable @0x1c6b60e0:                     ▼
     REPLICATED(1)  → kReplicatedCoresTreeBarrierInfoTable  @0xb4332c0
     PARTITIONED(2) → kPartitionedCoresTreeBarrierInfoTable @0xb4332f0
                                                                    │  (tree fan-out, §Tree-Barrier/vSync)
   BarrierWithinReplicaGroupStartImpl @0x1c698080:                  ▼
     GetReplicaGroupCoreInfo  → this core's master predicate + peers
     Pneg + Predicated        → run body on master core only
     ScheckGe/Lt/Ne           → validate each peer location
     VsyncAddRemote(sflag, peer, +1)  → signal each peer's barrier slot
   + deferred BarrierWithinReplicaGroupJoin::$_1 → the wait side
```

---

## 2. `BarrierWithinReplicaGroupStartImpl` @`0x1c698080` — the per-group signal fan-out

This 852-byte function in the `net_util` anonymous namespace is the leaf that actually signals. It is shared verbatim by GLOBAL (over all cores) and REPLICA (over a replica/partition group); the only difference is the `InfoTable` the caller passed, which determines the peer set `GetReplicaGroupCoreInfo` resolves.

### 2.1 Signature

```c
// xla::jellyfish::net_util::(anon)::BarrierWithinReplicaGroupStartImpl(
//     LloRegionBuilder b,            // a1 — out: builds a deferred Join closure into b
//     LloValue *sflag,               // a2 — the barrier sync-flag immediate (base+id or global slot)
//     InfoTable const& replica_tbl,  // a3 — the replica/partition membership table
//     InfoTable const& core_tbl,     // a4 — the per-core location table
//     std::optional<InfoTable const>,// a8/a9 — optional second-level (partition) table
//     long, long,                    // a5/a6 — replica/partition counts (ints in regs)
//     bool,                          // a10
//     long,                          // a11
//     bool master_check)             // a12 — emit the ScheckNe master-identity guard
//                                                                 // 0x1c698080
```

The matching demangled symbol confirms the shape: `BarrierWithinReplicaGroupStartImpl(LloRegionBuilder, LloValue*, InfoTable const&, InfoTable const&, optional<InfoTable const>, long, long, bool, long, bool)`.

### 2.2 Algorithm

```c
function BarrierWithinReplicaGroupStartImpl(b, sflag, replica_tbl, core_tbl, part_tbl,
                                            rcount, pcount, a10, a11, master_check):   // 0x1c698080
    // --- degenerate group: <= 1 member, nothing to signal (decompile line 45) ---
    if (part_tbl.count /* *((long*)&a9 + 1) */ <= 1):
        b.deferred = BarrierWithinReplicaGroupStartImpl::$_0;   // trivial single-member closure
        return                                                  // no GetReplicaGroupCoreInfo, no signal

    // --- read cores-per-chip bound for the per-peer ScheckLt (decompile line 49) ---
    cores_per_chip = *(int*)(*(Target+952) + 112);              // Target+0x3b8 deref, +0x70

    // --- resolve this core's group peers + master predicate ---
    // GetReplicaGroupCoreInfo reads the replica_tbl/core_tbl/part_tbl InfoTables and
    // returns: peer_set (vector<CoreLocationBase>) + a "is this core the master?" predicate.
    info = GetReplicaGroupCoreInfo(replica_tbl, core_tbl, part_tbl,
                                   rcount, pcount, a10, a11);    // 0x1c698740

    // --- MASTER-ONLY GATE: run the fan-out body only on the group's master core ---
    pred = b.Pneg(info.is_master);                              // 0x1d5208e0 — negate-into-predicate
    region = b.Predicated(pred);                                // 0x1d520f00 — open a predicated region
    b2 = LloRegionBuilder(region);                              // build inside the predicated region

    peer = info.peer_set[0];        // the (single) target this core signals (master → peer)

    // --- PER-PEER LEGALITY (only when master_check / a12 set) ---
    if (master_check):
        b2.ScheckGe(peer.core_index, SimmS32(0));               // 0 <= core_index
        b2.ScheckLt(peer.core_index, SimmS32(cores_per_chip));  // core_index < cores_per_chip
        gid_self = b2.ToGlobalCoreId();                         // 0x1d517240
        gid_peer = b2.GlobalCoreId();                           // 0x1d51b4c0
        b2.ScheckNe(gid_self, gid_peer,
                    "Non-master core has same location as master core!", 49);

    // --- ACTUATE: signal +1 to the peer's barrier sflag ---
    one = b2.SimmS32(1);
    b2.VsyncAddRemote(sflag, peer, one, /*flag=*/0);            // 0x1d522f40

    // --- defer the WAIT side as a heap closure (NOT emitted inline) ---
    join = operator new(0x48);                                 // 72-byte closure object
    join.call  = BarrierWithinReplicaGroupJoin::$_1;           // the wait callback
    join.peer  = peer; join.sflag = sflag; join.count = part_tbl.count; ...
    b.deferred = join;                                          // run by the Done side
```

The body is, structurally, "on the master core: validate the peer, add 1 to its barrier sflag, then register a deferred wait." The wait itself is the `BarrierWithinReplicaGroupJoin::$_1` closure copied into the 72-byte (`0x48`) heap object — the Done side ([§2.4](#24-the-join-wait-side)) invokes it. The `VsyncAddRemote` delta is the constant `SimmS32(1)`: each rendezvous bumps the peer's counter by exactly one.

> **GOTCHA —** the wait is **not** emitted inline. `BarrierWithinReplicaGroupStartImpl` only emits the *signal* (and builds a deferred closure for the wait). A reimplementer who emits signal+wait together will serialise the barrier and deadlock for any group larger than the tree fan-out width — the whole point of splitting Start from Done is to let the collective body run between them. The Done side runs the deferred closure; see [Tree-Barrier / vSync](tree-barrier-vsync.md).

### 2.3 The degenerate single-member fast path (`$_0`)

When the group has `<= 1` member (`part_tbl.count <= 1`, decompile line 45), the function skips `GetReplicaGroupCoreInfo`, the predication, the checks, and `VsyncAddRemote` entirely, and installs `BarrierWithinReplicaGroupStartImpl::$_0` as the deferred closure (decompile lines 156-157). A one-core group has nobody to signal, so the barrier degenerates to a no-op wait. This matches the producer: `IsGlobalBarrierBeneficial` ([Infer Barrier Config](infer-barrier-config.md)) routes singleton-dimension collectives to GLOBAL rather than REPLICA, so a `REPLICA(2)` config should never *itself* hit this path with `count==1` — but the guard is unconditional and protects the GLOBAL all-cores case on a single-core target too.

### 2.4 The Join (wait) side

`net_util::BarrierWithinReplicaGroupDone` @`0x1c6984e0` (581 bytes) carries the identical 10-argument signature as the Start impl and is the entry that consumes the deferred `BarrierWithinReplicaGroupJoin::$_1` closure: it re-resolves the peer set, predicates to the master, and waits (the `VWaitGe`-class op) for each peer's sflag to reach the expected count before releasing. The signal/wait threshold arithmetic and the wait primitive are documented on [Tree-Barrier / vSync](tree-barrier-vsync.md); this page notes only that Start emits the `+1` add and Done consumes it.

---

## 3. `VsyncAddRemote` @`0x1d522f40` — the actuation primitive

`LloRegionBuilder::VsyncAddRemote` is a 55-byte method — the single op `BarrierWithinReplicaGroupStartImpl` emits per peer. Its signature is `(LloValue* sflag, CoreLocationBase const& peer, LloValue* delta, bool)`; the Start impl calls it with `delta = SimmS32(1)` and the trailing flag `0`.

```c
// xla::jellyfish::LloRegionBuilder::VsyncAddRemote(
//     LloValue *sflag,             // which sync-flag (the barrier slot, base+id or global)
//     CoreLocationBase const& peer,// the target core's logical location (0x18-byte POD)
//     LloValue *delta,             // amount to add (always SimmS32(1) for a barrier)
//     bool)                                                       // 0x1d522f40
```

Semantically: it issues a remote sync-flag add of `delta` to `peer`'s `sflag` over the ICI fabric. The remote address is not formed here — the encoder that turns a `(peer CoreLocationBase, local sflag number)` pair into an ICI-routable VMEM address is `EncodeRemoteSyncFlagAddress` (the per-codename `TpuVersion`-dispatched encoder; see [Remote-SFLAG Encoders](remote-sflag-encoders.md)). `VsyncAddRemote` is the TC sequencer's `Vsync*` analog of the SparseCore `sc_tpu.sync_add` tree op; the `+1` it adds is what the Done side's wait counts.

> **NOTE —** the `peer` argument is a `CoreLocationBase` (a logical chip-coord + core-index POD), not a global core id. The `ScheckGe`/`ScheckLt` in the Start impl validate `0 <= peer.core_index < cores_per_chip` *before* the add, and `ScheckNe` (`"Non-master core has same location as master core!"`) confirms the resolved peer is not the master itself — three hard guards against a mis-resolved `InfoTable` entry signalling the wrong core or self-signalling.

---

## 4. Peer resolution and membership — `GetReplicaGroupCoreInfo`

`net_util::(anon)::GetReplicaGroupCoreInfo` @`0x1c698740` is the 4283-byte helper that turns the replica-group `InfoTable`(s) into this core's master predicate and peer set. The membership it reads is a precomputed flat table, **not** a bitmask or a per-ring constant slice: `net_util::CreateStaticReplicaInfoTable` @`0x1c69b780` (2133 bytes) flattens the HLO collective's `replica_groups` attribute into an `xla::InfoTable` backed by an `xla::Literal` R1 `int` array (`LiteralUtil::CreateR1<int>`), keyed by global device-id, storing each device's within-group **ordinal** (`table[device_id] = k`). The master is ordinal 0; `GetReplicaGroupCoreInfo` reads the current core's ordinal to recover its group and whether it is the master.

This **flat star** membership is the structural counterpart of the *binomial* AllReduce schedule: `CreateStaticBinomialReplicaInfoTable` builds an `int32[N × 8]` table indexed by `(rank × 8 + step)` storing each rank's recursive-doubling butterfly *partner* device-id — eight columns per rank instead of one, a precomputed butterfly schedule rather than a single membership ordinal. The two tables are structural inverses at different widths and feed different actuators (this barrier's flat star vs. the binomial emitter's butterfly); see [Binomial Recursive-Doubling](../collectives/binomial-recursive-doubling.md).

> **NOTE — on-wire indexing is LOW confidence.** That the membership is a `replica_count × partition_count`-keyed flat `int` `InfoTable` is confirmed (the `CreateStaticReplicaInfoTable` → `LiteralUtil::CreateR1<int>` chain, the `device_id → ordinal` store). The exact arithmetic inside `GetReplicaGroupCoreInfo` @`0x1c698740` that maps a core ordinal to its peer-set entries within the flattened literal — and the precise meaning of each entry (peer global-core-id vs. group index vs. ordinal) — was not fully disassembled. The 2D (DeviceAssignment-aware) dimension ordering of the fill is the same residual the flat-table fill carries.

---

## 5. Why REPLICA never lowers on SparseCore

`REPLICA(2)` is a **TensorCore-only** barrier type. Both SparseCore custom-kernel barrier entry points RetCheck it: `EmitScsBarrier` @`0x13352500` accepts only `GLOBAL(1)`/`CUSTOM(3)`, and `EmitAllToAllBarrierStart` @`0x133500e0` rejects type 2 with `"Only custom and global barriers are supported for all-to-all collectives on SparseCore"` (`offload_a2a_util.cc:124`). The SparseCore function literally named `EmitReplicaGroupCustomBarrierStart` @`0x13353620` is, despite its name, the lowering for the SC A2A `CUSTOM(3)` barrier (SMEM-buffer membership), **not** for `BarrierType::REPLICA`. The three distinct "global barrier" sources and this SC name trap are detailed on [Global-Barrier SFLAG Window](global-barrier-window.md) §4.4; this page documents only the TC emission.

---

## 6. Verification notes

> **[CONFIRMED]** Byte-exact in `libtpu.so` v0.0.40:
> - `BarrierCoresTree` @`0x1c6a75c0` (4186 bytes): binds the GLOBAL slot only when the caller's `sflag` is null — `GetGlobalBarrierSyncFlagNumber()` → `SflagImmPtr(n, "global barrier sync flag", 24)` (decompile lines 188-191); RetCheck `"tree_info_provider != nullptr"` (`net_util.cc:3855`); `TreeBarrierType` dispatch annotates `"global-barrier-wait"` (`!a6`) / `"start-global-barrier-wait"` (`a6`) for `kAll`, `"cross-partition-barrier-wait"` (`PARTITIONED`, `!a6`) and `"cross-replica-barrier-wait"` (`REPLICATED(1)`, `!a6`); the `a6`/is-start branch of both cross-group arms hits the `"!start_barrier"` RetCheck (`net_util.cc:3908`/`3904`); the `barrier_type == TreeBarrierType::kAll` RetCheck string (`net_util.cc:3843`) and `"b.target().HasLimitedIciRouting()"` (`net_util.cc:3836`) are present — exact.
> - `BarrierWithinReplicaGroupStartImpl` @`0x1c698080` (852 bytes): `group_size <= 1` → install `$_0` and return (lines 45, 156-157); `cores_per_chip = *(int*)(*(Target+0x3b8) + 0x70)` (line 49); `GetReplicaGroupCoreInfo` (lines 66/76/81); `Pneg` → `Predicated` master gate (lines 83-85); `master_check` arm `ScheckGe(.,0)` / `ScheckLt(., cores_per_chip)` / `ToGlobalCoreId` / `GlobalCoreId` / `ScheckNe(.,., "Non-master core has same location as master core!", 49)` (lines 89-122); `VsyncAddRemote(sflag, peer, SimmS32(1), 0)` (lines 124-126); deferred `BarrierWithinReplicaGroupJoin::$_1` into `operator new(0x48)` (lines 131-150) — exact.
> - `BarrierWithinReplicaGroupDone` @`0x1c6984e0` (581 bytes), `GetReplicaGroupCoreInfo` @`0x1c698740` (4283 bytes), `CreateStaticReplicaInfoTable` @`0x1c69b780` (2133 bytes): demangled signatures and sizes confirmed in the functions index.
> - `VsyncAddRemote` @`0x1d522f40` (55 bytes): demangled signature `(LloValue*, CoreLocationBase const&, LloValue*, bool)` confirmed.
>
> **[HIGH]** The argument identities of `BarrierWithinReplicaGroupStartImpl` (`a2 = sflag`, `a3/a4 = InfoTable&`, `a12 = master_check`) are attributed from the demangled signature ordering + the decompile reads, not from a struct descriptor. The `cores_per_chip` reading at `Target+0x3b8`/`+0x70` is byte-confirmed as the `ScheckLt` upper bound; its name is attributed from that use. The master = peer-ordinal-0 identity is attributed from `GetReplicaGroupCoreInfo` returning the predicate that `Pneg`/`Predicated` consumes.
>
> **[LOW]** The exact `InfoTable` on-wire indexing inside `GetReplicaGroupCoreInfo` (core ordinal → peer-set entries within the flattened R1 `int` literal, and the per-entry meaning) was not fully disassembled — proven to be a `replica_count × partition_count`-keyed flat `int` table; the per-entry meaning and the 2D DeviceAssignment dimension ordering are LOW. See [Global-Barrier SFLAG Window](global-barrier-window.md) §5.

---

## Cross-References

**Barrier algorithms (this section)**
- [Barriers and Sync-Flags — Section Map](overview.md) — the subsystem map: `BarrierType` enum, producer → normaliser → lowering flow
- [Global-Barrier SFLAG Window and the REPLICA Path](global-barrier-window.md) — the shared `net_util::GetBarrierSyncFlag` mapper, the GLOBAL-window reservation, the three distinct global-barrier number spaces, and the SC name trap
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — the `base + id` / `base + count + 4` SFLAG-number formulas this page's actuator consumes (computed in `BarrierCoresTree`/`GetBarrierSyncFlag`, never here)
- [Infer Barrier Config](infer-barrier-config.md) — the classification (`DetermineBarrierConfigForKey` / `IsGlobalBarrierBeneficial`) that produces the `REPLICA(2)` config this page lowers
- [Tree-Barrier / vSync](tree-barrier-vsync.md) — the signal-all-then-wait tree protocol, `GetTreeNodeRecord`/`SimpleLoop`, and the Done-side wait this page defers
- [TensorCore Barrier](tensorcore-barrier.md) — the TC signal/wait substrate and coloring-chosen `CUSTOM` ids

**Sibling subsystems**
- [Binomial Recursive-Doubling](../collectives/binomial-recursive-doubling.md) — the AllReduce butterfly schedule table that is the structural inverse of this barrier's flat membership table
- [back to index](../index.md)
