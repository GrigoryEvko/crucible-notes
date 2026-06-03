# TensorCore Barrier Assignment and `InitializeOnScs`

> *Every address, field offset, opcode value, and enum value on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`; 781,691,048 B, not stripped — full C++ symbols). `.text` VMA equals file offset at `0xe63c000`; `.rodata` / `.lrodata` are identity-mapped. All addresses are VMA. Other wheel versions differ.*

## Abstract

This page owns two byte-anchored pieces that bracket the TensorCore (TC) on-chip barrier path. The first is the **TC barrier-assignment pass** — `TensorCoreBarrierAssignment::Run` @`0x109c7420` and its per-key kind selector `DetermineBarrierConfigForKey` @`0x109c6fa0` — the TensorCore counterpart of the SparseCore coloring/assignment. It walks every TC collective, builds a `TensorCoreBarrierKey` per op, feeds the two greedy coloring passes' conflict set in, and assigns each distinct key a `BarrierConfig {type, id}` that is written back into the collective's HLO `BackendConfig`. The second is `ExplicitUniDirRingStrategy::InitializeOnScs` @`0x1337aa60` and its **strategy+0x98 lookup-callback** (`ExplicitRingRecord` @`0x133a9a40` for the plain ring, `ExplicitAllToAllRingRecord` @`0x133a94a0` for the all-to-all twin): the SparseCore-side runtime init that turns the static explicit ring-config table into a programmed `next_chip` / `ordinal` / `reorder` triple bound onto the strategy object.

These are two distinct subsystems joined by the chip SFLAG model. The TC assignment pass *decides* a barrier kind per key (global vs per-key) and the `InitializeOnScs` callback *binds* the per-ring transfer geometry the SC sequencer's barrier `sync_add` targets — both ultimately program the same chip SFLAG block. The generic greedy coloring engine is owned by [Barrier Coloring](barrier-coloring.md) (this page consumes its conflict set, it does not re-derive it); the `BarrierConfig.id` → concrete SFLAG-number formulas are owned by [Barrier → SFLAG Binding](barrier-to-sflag-binding.md) (this page produces the `{type, id}`, it does not lower it). The `BarrierType` enum, the `InferBarrierConfig` normaliser, and the reserved-block geometry are on the [overview](overview.md).

For reimplementation, the contract is:

- **TC dedups by graph color, SC by static-key hash.** `Run` runs the two `BarrierColoring` passes ([Barrier Coloring](barrier-coloring.md)), merges their conflict sets, then per distinct `TensorCoreBarrierKey` calls `DetermineBarrierConfigForKey(key, config, has_conflict)`. A non-conflicting key may share a `REPLICA(2)` or take a `GLOBAL(1)` barrier; a conflicting key (in the merged conflict set) is forced to a fresh `CUSTOM(3)` id.
- **`DetermineBarrierConfigForKey` writes only `{1,2,3}`.** Type at message `+0x20`, id at `+0x18`, presence hasbits `+0x10 |= 3`. `GLOBAL(1)` → `id = -1`; `REPLICA(2)` / `CUSTOM(3)` → `id = replica_group_count` (key`+0x10`), with `2` chosen when a `std::map<key, BarrierConfig>` dedup finds the key already present. `MEGACORE(4)` is **never** written here.
- **`IsGlobalBarrierBeneficial` is a narrow heuristic.** Global is "beneficial" only for a channelled `all-to-all(0xc)` whose tested topology axis is unit-sized and which spans a single replica group — the degenerate case where a custom per-key barrier would synchronise a trivial group.
- **`InitializeOnScs` is the static-config → hardware final hop.** It folds the runtime core index by `CoresPerChip / LogicalDevicesPerChip`, then calls `[strategy+0x98]` with the captured table descriptor (`strategy+0x88`) and the runtime `global_core_id` / `ordinal`; the callback indexes the const-literals table and returns `(next_chip, ordinal, reorder)`, written to `strategy+0x58` (`ordinal_`) / `+0x60` (`next_chip_`) / `+0x78` (`reordering_map_`). `next_chip_` is what the per-ring SyncAdd targets.

| | |
|---|---|
| **TC assignment pass** | `xla::jellyfish::TensorCoreBarrierAssignment::Run` @`0x109c7420` |
| **Per-key kind selector** | `DetermineBarrierConfigForKey` @`0x109c6fa0` (writes `{1,2,3}`; never `4`) |
| **Global heuristic** | `IsGlobalBarrierBeneficial` @`0x109c6ee0` |
| **Collective filter** | `ForEachCollective` @`0x109c7060` (opcode bitmask `0x1400001340` + `0x56`/`0x5d`/`0x31`) |
| **Key** | `TensorCoreBarrierKey` ctor @`0x109d6200`, `operator<` @`0x109d6620` |
| **Coloring engine (sibling)** | `BarrierColoring<…>::Run` @`0x109cf600` / `0x109d1a60` → [Barrier Coloring](barrier-coloring.md) |
| **Config carrier** | `BackendConfig.BarrierConfig {type @+0x20, id @+0x18, hasbits @+0x10}` |
| **Explicit-ring runtime init** | `ExplicitUniDirRingStrategy::InitializeOnScs` @`0x1337aa60` |
| **strategy+0x98 callback (plain)** | `ExplicitRingRecord` lambda @`0x133a9a40` (`$_2`, kind selector `0x4`) |
| **strategy+0xb0 callback (a2a)** | `ExplicitAllToAllRingRecord` lambda @`0x133a94a0` (`$_1`, kind selector `0x2`) |
| **Writeback** | `strategy+0x58 = ordinal_`, `+0x60 = next_chip_`, `+0x78 = reordering_map_` |
| **Confidence** | CONFIRMED (both bodies decompiled and re-checked) unless a row says otherwise |

---

## 1. The TC barrier-assignment pass

### 1.1 Where `Run` sits — the SC/TC fork

`BarrierAssignment::RunImpl` @`0x109c8c00` is the shared entry. It receives a `flat_hash_set<string_view>` of XLA thread-pool names and forks on which threads are present:

- `kTensorCoreThread` @`0x217f78d8` present → build a `TensorCoreBarrierAssignment` on the stack from the base config fields and call `Run` @`0x109c8d6e`.
- `kSparseCoreThread` @`0x217f5ca8` present → build a `SparseCoreBarrierAssignment` and call its `Run` @`0x109c8e8e`.

The two result hasbits OR together, so a module compiled for **both** a TensorCore thread and a SparseCore thread runs **both** passes. The on-chip barrier model is the union of the colored-map TC barriers and the static-keyed SC barriers; both program chip SFLAGs ([overview §1](overview.md)), differing only in how a barrier identity is chosen.

```text
BarrierAssignment::RunImpl @0x109c8c00
  ├─ if kTensorCoreThread present → TensorCoreBarrierAssignment::Run @0x109c7420   (THIS PAGE §1.2)
  │     coloring ×2 → conflict set → per-key DetermineBarrierConfigForKey → BackendConfig writeback
  └─ if kSparseCoreThread present → SparseCoreBarrierAssignment::Run @0x109c8e8e   (static ring-key dedup)
  result.changed = TC.changed | SC.changed
```

### 1.2 `TensorCoreBarrierAssignment::Run` @`0x109c7420`

`Run` is the TC counterpart of the SparseCore coloring/assignment. Its five phases:

1. **Build the policy base.** Construct an `AsyncOpPolicy<TensorCoreBarrierKey>` (vtable @`0x217f7a68`) carrying the megacore byte (`this+0x18`), the core-id `std::function<int(HloInstruction*)>` (`this+0x58`), and two policy bools (`this+0x60`, `this+0x68`). Re-vtable it to `AsyncCollectivePermutePolicy<TensorCoreBarrierKey>` (vtable @`0x217f7ac8`).
2. **First coloring pass.** `BarrierColoring<AsyncCollectivePermutePolicy<…>>::Run` @`0x109cf600` → `StatusOr<pair<map<HloInstruction*,long>, flat_hash_set<HloInstruction*>>>`: a per-op color and a conflict-op set. Merge the colored ops into a `std::map<HloInstruction*,long>` (frame `-0x158`) and the conflict ops into a `FlatHashSet` (frame `-0x140`).
3. **Second coloring pass.** Re-vtable the policy to `AsyncAllToAllWithAsyncBarrierPolicy<TensorCoreBarrierKey>` (vtable @`0x217f7b30`) and run `BarrierColoring<…>::Run` @`0x109d1a60`; merge its coloring + conflict set into the **same** `-0x158` map / `-0x140` set.
4. **Collect collectives.** `ForEachCollective` @`0x109c7060` (§1.4) walks every TC-thread collective, builds each `TensorCoreBarrierKey`, and appends `(key, InlinedVector<HloInstruction*, 2>)` pairs into a vector (entry stride `0x78`). `std::__stable_sort` with the `Run::$_2` comparator @`0x109ccdc0` makes the iteration order deterministic.
5. **Per-key assign loop** (`@0x109c7ed0 … 0x109c8250`, `r12` = entry, stride `0x78`):
   - look up the entry's first instruction in the merged conflict `FlatHashSet` (`raw_hash_set::find` @`0x109cf200`) → `has_conflict`;
   - `config = HloModule->config` (`module+0x20`);
   - `DetermineBarrierConfigForKey(&result, key, config, has_conflict)` @`0x109c6fa0` (§1.3) → a `BarrierConfig`;
   - **ragged-all-to-all special-case** @`0x109c7f89`: if opcode `== ragged-all-to-all(0x56)` AND the resulting kind `!= 3` (not a fresh per-key id) AND the policy bool `this+0x68` is set → divert to the a2a async-barrier special handling (jump `0x109c8371`);
   - for **every** `HloInstruction` in the entry's `InlinedVector` (each colored op sharing this key): open its `BackendConfig` (`BackendConfigWrapper::GetProto` @`0x1e60dc60`), set its `BarrierConfig` sub-message = the `DetermineBarrierConfigForKey` result (`BarrierConfig::CopyFrom` @`0x1d6f1fc0`), set the `BackendConfig` hasbit (`or BYTE [-0x600], 0x10`), and re-serialise via `CloneBackendConfigProto` @`0x1e60dac0` + `BackendConfigWrapper::operator=` @`0x1e60de40`.

> **NOTE — dedup by color, not by key hash.** Ops the coloring engine proves can share a barrier (non-conflicting in the async dependency graph) get the same color and can share a barrier id; ops with the *same* `TensorCoreBarrierKey` but a graph conflict are split apart. This is the structural difference from the SparseCore `FlatHashMap` dedup, which keys purely on the static ring-config and has no notion of schedule overlap. The interference-graph construction and first-fit color search are owned by [Barrier Coloring](barrier-coloring.md); this page only consumes the conflict set as `has_conflict`.

### 1.3 `DetermineBarrierConfigForKey` @`0x109c6fa0` — the kind selector

Effective signature: `BarrierConfig DetermineBarrierConfigForKey(const TensorCoreBarrierKey& key, const HloModuleConfig& config, bool force_global)`. The decompiled body is short and pins every store:

```c
// 0x109c6fa0  (this = &result BarrierConfig; a2 = key; a3 = config; a5 = has_conflict/force_global)
BarrierConfig::BarrierConfig(this, 0);                       // zero-init result
beneficial = IsGlobalBarrierBeneficial(key, config);         // 0x109c6ee0 (§1.5)

if (force_global || beneficial) {                            // → GLOBAL
LABEL_global:
    *(int  *)(this + 0x20) = 1;     // type  = GLOBAL(1)
    *(long *)(this + 0x18) = -1;    // id    = -1 (none)
    *(char *)(this + 0x10) |= 3;    // hasbits: type+id present
    return this;
}

v9 = *(long*)(key + 0x10);          // replica_group_count
if (v9 == *(long*)(key + 0x20) - 1) {           // spans all-but-one group (num_groups - 1)
    if (*(char*)(config + 0x50) != 1) {          // "use global on saturation" flag NOT set
        *(int *)(this + 0x20) = 2;  // type = REPLICA(2)  (shared)
        *(long*)(this + 0x18) = v9; // id   = replica_group_count
        *(char*)(this + 0x10) |= 3;
        return this;
    }
    goto LABEL_global;              // saturation + flag set → GLOBAL(1)
}

// ELSE: provisional fresh per-key
*(int *)(this + 0x20) = 3;          // type = CUSTOM(3)  (fresh)
*(long*)(this + 0x18) = v9;         // id   = replica_group_count (provisional)
*(char*)(this + 0x10) |= 3;
v11 = std::map<key,BarrierConfig>::__try_key_extraction_impl(key, key, config, this);  // 0x109cbd60
if ((dl & 1) == 0)                  // emplace did NOT insert ⇒ key already present (cache HIT)
    BarrierConfig::CopyFrom(this, (BarrierConfig*)(v11 + 0x80));   // adopt the existing config (its type==2)
return this;                        // cache MISS ⇒ keep the fresh type-3 config
```

The numeric layout (`this+0x20` is the DWORD type at byte offset `0x20`; `this+0x18` is the QWORD id; `this+0x10` is the hasbit byte):

| Outcome | type @`+0x20` | id @`+0x18` | when | Confidence |
|---|---|---|---|---|
| `GLOBAL` | `1` | `-1` | `force_global` OR `IsGlobalBarrierBeneficial` OR (saturation AND `config+0x50 == 1`) | CONFIRMED |
| `REPLICA` (shared) | `2` | `replica_group_count` | saturation (`rgc == num_groups − 1`) AND `config+0x50 != 1`; **or** map dedup found the key present | CONFIRMED |
| `CUSTOM` (fresh) | `3` | `replica_group_count` | otherwise, on a `std::map` cache miss | CONFIRMED |

> **CORRECTION — the saturation arm.** An earlier reading (P-3-392 §1d) summarised the saturation case (`replica_group_count == num_groups − 1`) as "also GLOBAL". The decompile shows it is more subtle: saturation produces a shared `REPLICA(2)` (id = the real `replica_group_count`) **unless** the module-config flag `config+0x50 == 1`, in which case it falls to `GLOBAL(1)`. The `2` write (`*(int*)(this+0x20) = 2`) at `0x109c704d` is reached both from the saturation arm and from the `std::map` cache-hit `CopyFrom`. `MEGACORE(4)` is never written — there is no `movl $4` into `+0x20` in this function (full-`.text` xref confirmed; [overview §2](overview.md)).

### 1.4 `ForEachCollective` @`0x109c7060` — which ops are TC collectives

`ForEachCollective` iterates `HloModule::computations(kTensorCoreThread)` @`0x10944b40`, walks each instruction, and invokes the callback for each op whose opcode byte (`instr+0xc`) is in the collective set. The filter (`@0x109c72b5 … 0x109c736d`):

- a bitmask `bt 0x1400001340, opcode` over `opcode <= 0x24` — set bits: `all-gather(0x06)`, `all-gather-start(0x08)`, `all-reduce(0x09)`, `all-to-all(0x0c)`, `collective-permute(0x22)`, `collective-permute-start(0x24)`;
- `cmp 0x56` → `ragged-all-to-all` accepted;
- `cmp 0x5d` → `reduce-scatter` accepted;
- otherwise (`custom-call(0x31)`): `GetCustomCallCollectiveId` @`0x109d6540` reads the `CustomCallConfig` backend-config field (hasbit `0x40`, returns `+0x78`) → accepted iff it carries a TPU collective id.

The callback also threads the conflict graph through async-start/async-done pairs by recursing into operand positions (`instr+0x40` / `instr+0x48`).

| byte | mnemonic | filter mechanism | barrier-key role | Confidence |
|---|---|---|---|---|
| `0x06` | all-gather | bitmask | replica-group keyed | CONFIRMED |
| `0x08` | all-gather-start | bitmask | replica-group keyed (async start) | CONFIRMED |
| `0x09` | all-reduce | bitmask | replica-group keyed | CONFIRMED |
| `0x0c` | all-to-all | bitmask | replica-group keyed; **only** opcode eligible for `IsGlobalBarrierBeneficial` | CONFIRMED |
| `0x22` | collective-permute | bitmask | source-target-pair keyed (`+0x20`) | CONFIRMED |
| `0x24` | collective-permute-start | bitmask | source-target-pair keyed (async start) | CONFIRMED |
| `0x31` | custom-call | `GetCustomCallCollectiveId` | accepted iff it has a TPU collective id | CONFIRMED |
| `0x56` | ragged-all-to-all | eq-check | replica-group keyed; `+0x51`/`+0x58` special-case | CONFIRMED |
| `0x5d` | reduce-scatter | eq-check | replica-group keyed (also the a2a-5d rewrite target) | CONFIRMED |

> **NOTE — opcode→mnemonic map.** The map is recovered from the `HloOpcodeString` length array @`0x421c9c0` (lengths `10/16/10/10/18/24/11/17/14` match `all-gather` / `all-gather-start` / `all-reduce` / `all-to-all` / `collective-permute` / `collective-permute-start` / `custom-call` / `ragged-all-to-all` / `reduce-scatter`) plus `.lrodata` string presence; the `char*` table itself is relocated, so values are resolved by length + presence.

### 1.5 `IsGlobalBarrierBeneficial` @`0x109c6ee0` — the heuristic

Byte-exact body:

```c
// 0x109c6ee0  (key = a1; config = a2)  → bool
if (*(char*)(key + 0x51) == 0)                  // "barrier-heuristic candidate" byte not set
    return false;                               // (VLOG(10) diagnostic path)
if (*(byte*)(key + 0x00) != 0x0c)               // opcode != all-to-all
    return false;
if (*(char*)(key + 0x50) != 0)                  // channel-id parity (cross-module) != 0
    return false;
if (*(int*)(config + 0x170) == 1 ||             // num_partitions / replica_count along axis == 1
    *(int*)(config + 0x178) == 1)
    return (*(long*)(key + 0x10) == 1);         // replica_group_count == 1 ⇒ single group
return false;
```

A global barrier is "beneficial" exactly for a **single-replica-group, channelled `all-to-all`** on a topology axis whose tested partition/replica count is `1` — the degenerate all-to-all where a per-key barrier would synchronise a trivial group and a single shared global barrier is strictly cheaper.

> **GOTCHA — `config+0x170` / `config+0x178`.** The two `== 1` scalars are byte-confirmed at those offsets and read as "single-device-along-axis" from use, but their proto field names (`num_partitions` vs `replica_count` vs a derived device count of `HloModuleConfig`) are **inferred**, not field-matched against the descriptor. The behaviour (`==1` on either ⇒ test `replica_group_count == 1`) is CERTAIN; the field naming is LOW.

### 1.6 `TensorCoreBarrierKey` ctor @`0x109d6200` / `operator<` @`0x109d6620`

The key is what two collectives must agree on to be *candidates* for sharing a barrier (the coloring then decides whether they actually may). Ctor `TensorCoreBarrierKey(HloInstruction* hlo, function<int(HloInstruction*)> core_id_fn, bool b)`:

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `+0x00` | opcode byte | `hlo+0xc`; `all-to-all(0xc)` may be rewritten to `0x5d` for the "5d" reshaped form (`@0x109d62db`) | CONFIRMED |
| `+0x08…+0x18` | sorted `vector<ReplicaGroup>` | copied from `hlo+0xd0`/`hlo+0xd8` (`__assign_with_size` @`0x10d6f60`), `__introsort`'d @`0x109d7580` (order-independent); `+0x10` = group count | CONFIRMED |
| `+0x20…+0x28` | sorted `vector<pair<long,long>>` | source-target pairs (for collective-permute `0x22`/`0x24`): `__assign_with_size` @`0x109d7220` + `__introsort` @`0x109da4e0` | CONFIRMED |
| `+0x38` | custom-call config scalar | `all-to-all(0xc)` custom-calls: reads `CustomCallConfig+0x78` when hasbit `0x40` set; also sets `+0x50` true | CONFIRMED |
| `+0x40` | core-id (int) | `-1` default, else `core_id_fn(hlo)` (invoked via `[fn+0x10]` when the function target is non-null) | CONFIRMED |
| `+0x50` | channel-id parity | `hlo->channel_id() & 1` (`HloInstruction::channel_id` @`0x1e59ff80`) — the byte `IsGlobalBarrierBeneficial` gates on | CONFIRMED |
| `+0x51` | heuristic-candidate flag | the ctor's `bool b`, AND'd with the `ragged-all-to-all(0x56)` check @`0x109d64e2` | CONFIRMED |
| `+0x58` | secondary discriminant | `-1` sentinel for the ragged-all-to-all global-disable case | CONFIRMED |

`operator<` @`0x109d6620` compares in order: `+0x58`, `+0x38`, `+0x00` (opcode), `+0x50` (channel parity), then the replica-group vector (`+0x10` count, then per-group sorted device ids). Two collectives share a TC barrier **key** iff same opcode, same channel parity, same custom-call scalar, same secondary discriminant, and identical sorted replica-group membership — the dense-collective analog of the SparseCore ring key, keyed on HLO replica groups rather than ICI ring offsets.

---

## 2. `ExplicitUniDirRingStrategy::InitializeOnScs` and the strategy+0x98 callback

This is a different subsystem from §1: the SparseCore-side ring strategy runtime init. Where the TC pass *decides* a barrier kind, `InitializeOnScs` *binds* the per-ring transfer geometry the SC sequencer's barrier `sync_add` will target. It is the final hop of the static `IciStrategyRingConfig` EXPLICIT-table → programmed `next_chip` bridge.

### 2.1 `InitializeOnScs` @`0x1337aa60` — the fold + callback dispatch

System-V args: `this` = strategy, `OpBuilder&`, `LocationGenerator`, then three `Value`s (`scs ordinal`, `scs partition`, a third `Value` triple). The decompiled body confirms every step:

```c
// 0x1337aa60  (a1 = strategy; a2 = OpBuilder; a5 = scs partition Value; a3 = scs ordinal Value)
if (!a5)                                                         // line 351
    return RetCheckFailSlowPath("config != nullptr");

UniDirRingStrategy::InitializeOnScs(a1, a2, a3, a4, ...);        // base init; stores +0x68/+0x70
if (base != OK) return AddSourceLocationImpl(354);

CoreIndex = OffloadFactory::GetCoreIndex(a1+8, a2, a4);          // 0x133e6aa0 (per-replica core index)
v17  = *(a1+8);                                                  // OffloadFactory / Target subobject
v18  = Target::CoresPerChip(v17, /*SC*/2);                       // 0x1d615b40
v19  = Target::LogicalDevicesPerChip(v17, /*SC*/2);             // 0x1d615b00
fold = v18 / v19;                                                // unsigned divide (megacore fold)
v22  = OffloadFactory::IdxConst(a1+8, a2, fold);                 // 0x133e6ba0 (MLIR index constant)
v23  = OffloadFactory::DivU(a1+8, a2, CoreIndex, v22);           // 0x133e6a60 → arith::DivUIOp
ordinal       = v23;
global_core_id = OffloadFactory::ToGlobalCoreId(a1+8, a2, partition, v23);   // 0x133e6880

// THE CALLBACK — call QWORD PTR [strategy + 0x98]
(*(a1 + 0x98))(&record /*StatusOr<ExplicitRingRecord>*/,
               a1 + 0x88 /*captured table descriptor*/,
               a1 + 0x08 /*OffloadFactory subobject*/,
               a2        /*OpBuilder*/,
               &global_core_id,
               &ordinal);

if (record == OK) {                                              // writeback (double-init guarded)
    CHECK(strategy+0x58 == null, "ordinal_ == nullptr");         // line 250
    *(a1 + 0x58) = record.ordinal;                               // ordinal_
    CHECK(strategy+0x60 == null, "next_chip_ == nullptr");       // line 245
    *(a1 + 0x60) = record.next_chip;                             // next_chip_
    CHECK(strategy+0x78 == null, "reordering_map_ == nullptr");  // line 255
    *(a1 + 0x78) = record.reorder;                               // reordering_map_
    return OK;
}
return AddSourceLocationImpl(362);                               // line 362
```

The three writeback fields are named in the `LogMessageFatal` strings the decompile carries: `ordinal_` at `strategy+0x58`, `next_chip_` at `+0x60`, `reordering_map_` at `+0x78` — each guarded against a second init (a re-init dies fatal). `next_chip_` is then consumed by `next_chip()` @`0x1337a200` → `GlobalCoreIdToPhysicalChipId` → `ComputeRemoteCoreIndex` → the `SyncAddOp` the SC custom-barrier emits ([Barrier → SFLAG Binding](barrier-to-sflag-binding.md)).

> **NOTE — line numbers.** The `RetCheck` / `AddSourceLocationImpl` line constants (`351`, `354`, `362`) and the writeback `CHECK` lines (`245`/`250`/`255`) match `platforms/xla/sparse_core/offload_collective_strategies.cc` exactly (P-3-392 §2a anchors `0x15f`=351, `0x162`=354, `0x16a`=362). The `CoresPerChip(2)` / `LogicalDevicesPerChip(2)` fold is the same megacore-fold divisor used by `ToPartnerGlobalCoreId`.

### 2.2 The callback body — `ExplicitRingRecord` @`0x133a9a40` (plain) / `ExplicitAllToAllRingRecord` @`0x133a94a0` (a2a)

The `strategy+0x98` slot is installed by the per-color factory closures inside `CreateRingStrategiesForNdFromExplicitTable` (`emitter_helpers`). The factory → strategy map (the demangled `$_N` lambdas confirm the record types):

| Factory | Builds | Callback slot | Record type | kind selector |
|---|---|---|---|---|
| `$_0` @`0x133a9080` | `D2DUniDirRingStrategy` (sizeof `0x98`) | — | — | — |
| `$_1` @`0x133a91e0` | `ExplicitUniDirAllToAllRingStrategy` (sizeof `0xe8`, vtable @`0x21908ec8`) | `+0xb0` = `0x133a94a0` | `ExplicitAllToAllRingRecord` | `0x2` |
| `$_2` @`0x133a9840` | **plain** `ExplicitUniDirRingStrategy` (sizeof `0xc0`, vtable @`0x21908e30`) | `+0x98` = `0x133a9a40` | `ExplicitRingRecord` | `0x4` |

> **CORRECTION — the factory map.** An earlier reading labelled `$_0` as the implicit/explicit slot; the demangled symbols confirm `$_0` builds `D2DUniDirRingStrategy`, `$_1` builds the all-to-all strategy (returning `ExplicitAllToAllRingRecord`), and `$_2` builds the plain explicit ring (returning `ExplicitRingRecord`). The `$_2` install at `0x133a99d9` writes the `0x133a9a40` lambda into `strategy+0x98` — the exact call target of §2.1.

The plain lookup `ExplicitRingRecord` @`0x133a9a40` body:

- `ctx = [strategy+0x88]` (the table descriptor) → `r14`: `r14[0]` = an inner const-literals accessor object (its `operator()` at `accessor+0x10`); `r14[0x10]` = `id_info_offset` (`movsxd` int32); `r14[0x18]` = the table base ptr; `r14[0x20]`/`r14[0x21]` = a byte selector + a variant tag.
- inner call: push `(kind = 0x4, base, byte-selector, &out, global_core_id, ordinal)`; `call QWORD PTR [accessor+0x10]` @`0x133a9a96` — the accessor that slices the const-literals int vector at `[base + id_info_offset…]` and materialises the per-core neighbor/id/group constant as MLIR `Value`s.
- on success: `next_chip = out[0]`, `ordinal = out[8 or 0x10]` (the `r14+0x21` variant tag selects the field), `reorder = out[0x20]` → written into the `StatusOr<ExplicitRingRecord>` (`record.next_chip` `+0x8`, `record.ordinal` `+0x10`, `record.reorder` `+0x18`, `record.ok` `+0x0`). Free the temporary vector. `RetCheck` line `0xfe5`.

The all-to-all twin `ExplicitAllToAllRingRecord` @`0x133a94a0` is identical in shape but uses **kind selector `0x2`** and threads the result through `OffloadFactory::location` @`0x131e9ca0` + `Target::GranuleBytes` @`0x1d617f80` + `BufferOffset::Create` @`0x133ea5c0` + `BufferOffset::WithOffsetElements` @`0x133eace0` to convert the group-info table slice into a byte `BufferOffset` (the per-core group-info window). `RetCheck` line `0xfb9`.

> **NOTE — the final hop.** The callback is where the static EXPLICIT offsets become a programmed `Value`: `id_info_offset` (`table_desc+0x10`) + the table base (`+0x18`) index the runtime const-literals vector, keyed by the runtime `global_core_id` / `ordinal`, to produce the `next_chip` the per-ring `SyncAdd` targets. The plain ring returns raw `Value`s; the a2a ring returns a `BufferOffset`-wrapped group window. The `0x4` vs `0x2` selector chooses which.

> **INFERRED — the inner accessor body.** The accessor object reached via `call [accessor+0x10]` has three proven captured members (`+0x10` = `id_info_offset`, `+0x18` = table base, `+0x20`/`+0x21` = byte + variant selectors) and a pinned call signature (`global_core_id, ordinal, base, selector, kind 0x4/0x2 → out triple`), but its own body — the closure that performs the spmem load / `DivU`-`Mod` indexing into the const-literals vector — was not separately disassembled. Its CALL signature is CONFIRMED; the const-literals vector entry semantics are the standing producer gap (LOW).

---

## 3. The two subsystems, one chip SFLAG block

The TC assignment pass (§1) and the explicit-ring `InitializeOnScs` (§2) sit on opposite ends of the same on-chip barrier model: the TC pass chooses a `BarrierConfig` for dense TC collectives; `InitializeOnScs` binds the per-ring transfer geometry for SC embedding collectives. Both ultimately program chip SFLAGs ([Barrier → SFLAG Binding](barrier-to-sflag-binding.md)).

| Aspect | TensorCore (§1) | SparseCore explicit ring (§2) | Confidence |
|---|---|---|---|
| Pass / init | `TensorCoreBarrierAssignment::Run` @`0x109c7420` | `ExplicitUniDirRingStrategy::InitializeOnScs` @`0x1337aa60` | CONFIRMED |
| Identity unit | `TensorCoreBarrierKey` (opcode, channel parity, replica-group / src-tgt vectors, cc scalar) | static `IciStrategyRingConfig` EXPLICIT offsets (id_info / group_info) | CONFIRMED |
| Dedup / bind mechanism | greedy graph coloring ([Barrier Coloring](barrier-coloring.md)) → `BarrierConfig {1,2,3}` | `strategy+0x98` const-literals lookup → `(next_chip, ordinal, reorder)` | CONFIRMED |
| Output destination | HLO `BackendConfig.BarrierConfig` submessage | `strategy+0x58/+0x60/+0x78` (`ordinal_` / `next_chip_` / `reordering_map_`) | CONFIRMED |
| Hardware sink | chip SFLAG block via the kernel emitter | per-ring `SyncAdd` targeting `next_chip_` on the chip SFLAG block | CONFIRMED |

The split is purely in **how** a barrier identity is chosen and bound: the TC pass colors the live async-collective conflict graph; the explicit ring indexes a static config table by the runtime core id. Both arms end at the chip SFLAG tier ([overview §1](overview.md)).

---

## 4. Verification notes

> **[CONFIRMED]** Re-derived from the IDA decompile of `libtpu.so` v0.0.40 for this page:
> - `DetermineBarrierConfigForKey` @`0x109c6fa0`: `BarrierConfig::BarrierConfig(this,0)`; `IsGlobalBarrierBeneficial`; `force_global || beneficial` → `type=1`, `id=-1`; saturation (`key+0x10 == key+0x20 − 1`) with `config+0x50 != 1` → `type=2`, `id=replica_group_count`; else `type=3` fresh + `std::map __try_key_extraction_impl` → cache hit `CopyFrom(node+0x80)`. Type at `+0x20`, id at `+0x18`, hasbits `+0x10 |= 3`. **No `movl $4`** — exact.
> - `ExplicitUniDirRingStrategy::InitializeOnScs` @`0x1337aa60`: `if (!config) RetCheck` line 351; base `UniDirRingStrategy::InitializeOnScs` → `AddSourceLocationImpl(354)`; `GetCoreIndex`; `CoresPerChip(2) / LogicalDevicesPerChip(2)` unsigned-divide fold; `IdxConst`; `DivU`; `ToGlobalCoreId`; `call [a1+0x98]` with `(&record, a1+0x88, a1+0x08, OpBuilder, &global_core_id, &ordinal)`; writeback `*(a1+0x58)=ordinal_`, `*(a1+0x60)=next_chip_`, `*(a1+0x78)=reordering_map_`, each `LogMessageFatal`-guarded (lines 250/245/255); `AddSourceLocationImpl(362)` — exact.
> - Symbol confirmation: `TensorCoreBarrierAssignment::{Run, ForEachCollective, IsGlobalBarrierBeneficial, DetermineBarrierConfigForKey}` and `TensorCoreBarrierKey` present with full demangled symbols; `ExplicitUniDirRingStrategy::InitializeOnScs` and the `CreateRingStrategiesForNdFromExplicitTable` `$_1` (→ `ExplicitAllToAllRingRecord`) / `$_2` (→ `ExplicitRingRecord`) lambdas present, confirming the factory→record map and the `(OffloadFactory&, OpBuilder&, Value, Value) → StatusOr<…Record>` callback signature.
>
> **[LOW]**
> - `IsGlobalBarrierBeneficial`'s `config+0x170` / `config+0x178` scalars are offset-confirmed and read as "single-device-along-axis", but the proto field names (`num_partitions` vs `replica_count`) are inferred, not descriptor-matched.
> - The inner const-literals accessor body (`call [accessor+0x10]`, §2.2) — its three captured members and call signature are pinned, but the closure that slices the const-literals vector and materialises the per-core `Value`s was not separately disassembled.
> - The `TensorCoreBarrierKey` sub-member *semantics* (which vector is replica-groups vs src-tgt-pairs) are read from the `__assign_with_size` / `__introsort` sources and the opcode branch; the offsets are CONFIRMED, the role naming is structural.

---

## Cross-References

**Barrier algorithms (this section)**
- [Overview](overview.md) — the SFLAG-based barrier model, the `BarrierType` enum, and the `InferBarrierConfig` normaliser (the second `BarrierConfig` writer this page's producer feeds)
- [Barrier Coloring](barrier-coloring.md) — the greedy interference-graph engine whose merged conflict set is the `has_conflict` input to `DetermineBarrierConfigForKey`
- [Barrier → SFLAG Binding](barrier-to-sflag-binding.md) — how a chosen `BarrierConfig {type, id}` (and the bound `next_chip_`) becomes a concrete chip SFLAG number + signal/wait ops
- [Replica Barrier](replica-barrier.md) — the within-replica-group tree barrier (`REPLICA(2)`), the shared-id outcome this pass can emit

**Sibling subsystems**
- [Megacore Fusion](../collectives/megacore-fusion.md) — the megacore-fold collective fusion whose ring strategies `InitializeOnScs` binds
- [back to index](../index.md)

**Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`).
