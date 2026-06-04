# Global-Barrier SFLAG Window and the REPLICA Path

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the `GetGlobalBarrierSyncFlagNumber` formula, the `GetBarrierSyncFlag` dispatch, the `MaybeInsertGlobalBarrier` insertion gate, and the REPLICA tree path (`BarrierWithinReplicaGroupStartImpl` → `VsyncAddRemote`) were re-derived from the IDA decompile; the literal per-gen reserved integers are an embedded-memfile dependency (LOW, see §5) · **Part XIII — On-Pod Collectives & Barriers** / SFLAG & barriers · [back to index](../index.md)

## Abstract

The phrase "global barrier" names **three different things** in this binary, on **two different sequencers**, each drawing from a **disjoint reserved SFLAG region**. This page owns the one that actually carries the name `GetGlobalBarrierSyncFlagNumber` — the **TensorCore (TC) reserved GLOBAL slot** at `base + count + 4` — and two structures that surround it: the **insertion gate** `CustomKernelEmitter::MaybeInsertGlobalBarrier` @`0x1321ac20` (which decides *whether* a SparseCore func gets a global barrier, on a different SFLAG window entirely), and the **REPLICA(2) barrier path** that the TC GLOBAL machinery shares — the within-replica-group tree barrier reached through the same `net_util::GetBarrierSyncFlag` mapper and `BarrierCoresTree` actuator.

The decisive structural fact, and the reason this page exists, is that the SparseCore Mosaic emitter's `MaybeInsertGlobalBarrier` **never calls** `GetGlobalBarrierSyncFlagNumber`. The two were historically conflated. A full `.text` E8/E9 `rel32` xref scan finds 20 direct callers of `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`, and **not one** is in the SparseCore custom-kernel emitter. The global SFLAG *number* belongs to the TC engine only; the SC func-level global barrier inserted by `MaybeInsertGlobalBarrier` runs on its own per-core Mosaic window.

The SFLAG number formulas live on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the `{base, count}` integers per generation on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md); the `BarrierConfig`-type classification on [Infer Barrier Config](infer-barrier-config.md). This page owns the **global-barrier SFLAG window reservation, the `MaybeInsertGlobalBarrier` insertion gate, and the REPLICA path the global barrier shares**.

For reimplementation, the contract is:

- **The global SFLAG slot is one number, reserved at the top of the TC block.** `GetGlobalBarrierSyncFlagNumber` returns `base + count + 4` — the highest of the five named top slots of the TC reserved range (§2). It is computed, never stored in `BarrierConfig`; the GLOBAL config carries `id = -1`.
- **`MaybeInsertGlobalBarrier` is a gate, not a number source.** It runs on the SparseCore Mosaic side, reads `CustomCallConfig` flags + `BarrierConfig.type`, and decides whether to insert a func-level tree barrier on the SC per-core window — it computes no `GetGlobalBarrierSyncFlagNumber` (§3).
- **The consumer set of the global slot is the TC engine.** `net_util::GetBarrierSyncFlag` (the `BarrierConfig → SFLAG` mapper), the `net_util` tree-barrier family (`BarrierCoresTree`, `BarrierAllCores*`), and the memory-reservation passes — all TensorCore LLO (§1).
- **REPLICA(2) is a TensorCore-only barrier type.** It maps to `base + id` (not the global slot) and lowers through `BarrierCoresTree(REPLICATED/PARTITIONED)` → `BarrierWithinReplicaGroupStartImpl` → `VsyncAddRemote` to the current core's replica-group peers; membership is an `InfoTable` literal. SparseCore **RetChecks** type 2 (§4).

| | |
|---|---|
| **The global SFLAG number** | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` = `base + count + 4` (`this[560]`/`this[561]` = `Target+0x8c0`/`+0x8c4`) |
| **Reserved window** | top 5 slots of the TC block `[base, base+count)`; GLOBAL at `+count+4`, MEGACORE at `+count` |
| **Consumer set** | 20 direct callers, **all** TC LLO (`net_util::GetBarrierSyncFlag`, `BarrierCoresTree`, `BarrierAllCores*`, memory-reservation passes); **zero** SparseCore-emitter callers |
| **`BarrierConfig → SFLAG` mapper** | `net_util::GetBarrierSyncFlag` @`0x1c69ad00` (GLOBAL→`base+count+4`, else→`base+id`) |
| **Insertion gate (SC Mosaic)** | `CustomKernelEmitter::MaybeInsertGlobalBarrier` @`0x1321ac20` (3 RetCheck gates; does **not** call the global accessor) |
| **REPLICA producer** | `DetermineBarrierConfigForKey` @`0x109c6fa0` (writes `type=2, id=key id` for a single, non-partition replica group) |
| **REPLICA actuator** | `BarrierWithinReplicaGroupStartImpl` @`0x1c698080` → `VsyncAddRemote` @`0x1d522f40` to InfoTable peers |
| **Source TU anchors** | `net_util.cc`, `barrier_assignment.cc`, `custom_kernel_emitter.cc`, `offload_a2a_util.cc` |

---

## 1. The global SFLAG slot: number, window, consumers

The "global barrier sync flag" is a single SFLAG number reserved at the top of the TensorCore reserved block. It is **computed on demand**, never carried in a config field — the GLOBAL `BarrierConfig` stores the sentinel `id = -1`, and the lowering substitutes the computed slot.

### 1.1 `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`

The accessor is two adds. Byte-exact from the decompile:

```c
// xla::jellyfish::Target::GetGlobalBarrierSyncFlagNumber(Target *this)   // 0x1d60f420
__int64 GetGlobalBarrierSyncFlagNumber(Target *this) {
    return (unsigned int)(*((_DWORD *)this + 561)    // Target+0x8c4 = count
                        + *((_DWORD *)this + 560)    // Target+0x8c0 = base
                        + 4);
}
```

`this[560]` is the TC reserved-block **base** (`Target+0x8c0`); `this[561]` is the **count** (`Target+0x8c4`, where `count = |compiler_reserved(TensorCore)| − 5`). The result is `base + count + 4` — the **fifth and highest** of the five named top slots reserved above the per-id window `[base, base+count)`. The slot derivation, the `−5` carve, and the sibling accessors (`GetMegacoreBarrierSyncFlagNumber` = `base+count`, the two `GetAllReduceSyncFlagNumber` slots) are derived on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the per-codename `{base, count}` integers on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md). This page does not re-derive them — it documents the window's *consumers* and the GLOBAL/REPLICA *paths*.

> **NOTE —** the global slot is **not** in the per-id window. REPLICA(2)/CUSTOM(3) ids satisfy `0 <= id < count` and map to `base + id` (§4, §1.2); GLOBAL is `base + count + 4`, four slots above the top usable id. The two never alias.

### 1.2 `net_util::GetBarrierSyncFlag` @`0x1c69ad00` — the `BarrierConfig → SFLAG` mapper

The one named, primary consumer of the global slot is the TC dense-collective `BarrierConfig → SFLAG` mapper. It dispatches on `BarrierConfig.barrier_type` at `[bc+0x20]`. Byte-exact:

```c
// net_util::GetBarrierSyncFlag(BarrierConfig const& bc, LloRegionBuilder b)   // 0x1c69ad00
int GetBarrierSyncFlag(BarrierConfig const& bc, LloRegionBuilder b) {
    int type = *(int*)(&bc + 0x20);
    if (type == 1) {                                   // GLOBAL
        int n = b.target()->GetGlobalBarrierSyncFlagNumber();   // base + count + 4
        return b.SflagImmPtr(n, "global barrier sync flag");
    }
    if (type == 0)                                     // BARRIER_INVALID
        CHECK_FAIL("barrier.barrier_type() != BarrierType::BARRIER_INVALID");  // net_util.cc:2065 (0x811)
    // type 2 (REPLICA) / 3 (CUSTOM) / 4 (MEGACORE):
    long id = *(long*)(&bc + 0x18);
    CHECK(id < *(int*)(b.target() + 0x8c4));           // "barrier.id() < b.target().GetBarrierSyncFlagCount()", :2070 (0x816)
    int n = id + *(int*)(b.target() + 0x8c0);          // base + id
    return b.SflagImmPtr(n, "barrier sync flag number");
}
```

The arithmetic: **GLOBAL → `base + count + 4`** (the reserved global slot); **per-id (2/3/4) → `base + id`** with `0 <= id < count`. `BARRIER_INVALID(0)` is a hard CHECK-fail; the slot value comes back wrapped as an `SflagImmPtr` (the immediate `LloValue` the emitter signals/waits on). This is the TensorCore analog of the SparseCore `GetSyncFlagForBarrierId` (`id + SC_base`) — **same structure, different `Target` fields and different sequencer/op family** ([Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md)).

`GetBarrierSyncFlag` has 18 direct callers, all `xla::jellyfish` dense-collective emitters (`AllGather`/`RingSum`/`Binomial`/`RotatedPincer`/`CollectivePermute`/`AllToAll`); each reads the HLO `BackendConfig.BarrierConfig` chosen by `TensorCoreBarrierAssignment` and emits the rendezvous through the `net_util` tree-barrier helpers.

### 1.3 The consumer set (20 callers; all TensorCore)

A whole-`.text` scan for `E8`/`E9` `rel32` resolving to `0x1d60f420` finds **20** direct call sites; every one resolves (via the sorted symbol table) to an `xla::jellyfish` TensorCore LLO / runtime function. **None** is in the SparseCore Mosaic custom-kernel emitter — confirming that `MaybeInsertGlobalBarrier` (§3), `EmitScsBarrier`, and `EmitAllToAllBarrierStart` never touch `Target+0x8c0`/`+0x8c4`. The callers fall into four groups:

| Group | Functions (representative) | Role |
|---|---|---|
| **A. config mapper** | `net_util::GetBarrierSyncFlag` @`0x1c69ad00` | the `BarrierConfig → SFLAG` mapper (§1.2) — the primary named consumer |
| **B. tree-barrier family** | `BarrierCoresTree` @`0x1c6a75c0`; `BarrierAllCoresStartNoReturn` @`0x1c697b60`; `BarrierAllCoresJoin` @`0x1c697e60`; `BarrierAllCoresWithIdVerification` @`0x12715900`; `BarrierCoresWithIdVerificationInternal` @`0x12715c00`; `MaybeDistributeStackBaseAddresses` @`0x1c69ae00` | the runtime ICI / cross-core tree barriers (the GLOBAL actuator) |
| **C. memory-reservation / analysis** | `DoMemoryAllocation` @`0x10a31ee0` (×4 sites); `LinkAndFinishProgram` @`0x10a25a20`; `AllocateHloOutputsInMemorySpace` @`0x1c439980`; `ComputeAvailableSyncFlags` @`0x1c6e42a0`; `RaceAnalyzerStepper::PreProcessEvent` @`0x10bb2a60`; `Processor::Instrument $_1` @`0x10c94920`; `GetReferencedSyncFlags $_1` @`0x10c94c60`; `TpuCompactionIsaEmitterCodegen::Generate` @`0x1090ece0` | reserve / account for the global slot in the SFLAG pool |
| **D. dense-collective references** | `AllToAllEmitter::EmitOutputAddressExchange` @`0x10f00dc0`; SC `LoweringEmitter::Emit` @`0x131ce4e0` (`+1` as an SFLAG-range upper bound, **not** a barrier consumer) | reference the slot directly |

The sibling `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0` (`= base + count`, `Megacore()`-gated) has only 3 callers — `SynchronizeProgramDescriptorStatesMegacore`, `LloRegionBuilder::BarrierMegacore`, and `RaceAnalyzerStepper::PreProcessEvent` — i.e. the megacore-fold runtime barrier only; no `BarrierConfig` producer ever writes `MEGACORE(4)` (see [Infer Barrier Config](infer-barrier-config.md) and the [overview](overview.md) §2 QUIRK).

> **[CONFIRMED]** `BarrierCoresTree` @`0x1c6a75c0` calls `GetGlobalBarrierSyncFlagNumber` in its body and wraps it as `SflagImmPtr("global barrier sync flag")` — the TC tree barrier's GLOBAL slot. Verified directly in the decompile.

---

## 2. Three "global barriers", three reserved regions, two sequencers

Because the word "global" is overloaded, a reimplementer must keep three distinct sources apart. Only **(c)** is `GetGlobalBarrierSyncFlagNumber`; this page owns (c) and the gate that produces (a).

| # | What | Chosen / inserted by | SFLAG number source | Sequencer / op family | Confidence |
|---|---|---|---|---|---|
| **(a)** | SC Mosaic **func-level** tree barrier | `CustomKernelEmitter::MaybeInsertGlobalBarrier` @`0x1321ac20` (§3) | SC Mosaic per-core window (`mlir::sparse_core::MemorySpaceAttr::get(ctx, 14)` — SC MLIR enum, **not** jellyfish `MemorySpace`; `AllocateAtOffsetOp`); **NOT** `GetGlobalBarrierSyncFlagNumber` | TC sequencer (Mosaic) `tpu.sem_signal`/`tpu.sem_wait` in `scf.for` | CONFIRMED |
| **(b)** | SC **per-collective** global barrier | `EmitScsBarrier(type1)` / `EmitAllToAllBarrierStart(type1)` → `EmitGlobalBarrier` @`0x13352820` | `GetSyncFlagForBarrierId(reserved id)` = `id + SC_base` (SC barrier block) | SC sequencer `sc_tpu.sync_add` tree over SC cores | CONFIRMED |
| **(c)** | **TC LLO reserved GLOBAL slot** | `net_util::GetBarrierSyncFlag(type1)` @`0x1c69ad00`; `BarrierCoresTree` etc. (§1) | `GetGlobalBarrierSyncFlagNumber()` = `base + count + 4` (TC barrier block) | TC sequencer Vsync* / `net_util` tree barrier | CONFIRMED |

The three live in three number spaces: (a) the SparseCore Mosaic per-core window (`mlir::sparse_core::MemorySpace` value 14 — the SC MLIR enum, distinct from the jellyfish `MemorySpace` enum where 14 = `sparse_core_sequencer_smem`); (b) the SC barrier block; (c) the TC barrier block `[base, base+count]` with the GLOBAL slot at `+count+4`. (a) and (b) are SparseCore; (c) is TensorCore. **`GetGlobalBarrierSyncFlagNumber` belongs to the TC engine only** — which is exactly why the SparseCore `MaybeInsertGlobalBarrier` never calls it. An earlier reading that attributed source (c) to the SC tree barrier (a) is corrected here: they are different `Target`/`SparseCoreTarget` fields and different sequencers.

---

## 3. `MaybeInsertGlobalBarrier` @`0x1321ac20` — the insertion gate

`CustomKernelEmitter::MaybeInsertGlobalBarrier` is the SparseCore Mosaic entry point that decides **whether** a custom-call func is given a func-level global barrier and — if so — inserts it on the SC per-core window. It is a **gate**, not an SFLAG-number source: it reads `CustomCallConfig` flags and `BarrierConfig.type`, runs three legality RetChecks, and then walks the func's ops to materialise the barrier on the appropriate core type. It computes no `GetGlobalBarrierSyncFlagNumber`.

### 3.1 Signature and inputs

```c
// xla::tpu::sparse_core::CustomKernelEmitter::MaybeInsertGlobalBarrier(
//     mlir::ModuleOp module, CustomCallConfig const* cfg, BarrierConfig const* bc)   // 0x1321ac20
```

The decompile presents `a1`=module handle, `a2`=walk iterator over the module, `a3`=`CustomCallConfig*`, `a4`=`BarrierConfig*` (may be null). The gate reads three things out of `CustomCallConfig` (`a3`) and one out of `BarrierConfig` (`a4`):

| Decompile read | Meaning (attributed from use + RetCheck strings) | Confidence |
|---|---|---|
| `v4 = cfg[+0x10]` flags dword; bit `0x200` | presence guard for the *has-communication* byte | HIGH |
| `v5 = cfg[+0x90]` (read only if bit `0x200`) | **has-communication** flag (custom call communicates) | HIGH |
| `v4` bit `0x2000` | presence guard for the *skip-device-barrier* byte | HIGH |
| `v6 = cfg[+0x94]` (read only if bit `0x2000`) | **skip_device_barrier** flag | HIGH |
| `v4` bit `0x40` | **custom-barrier-requested** flag (a `BarrierConfig` is attached) | HIGH |
| `v7 = (bc->type [a4+0x20] == 3)` | the attached barrier is `CUSTOM(3)` | CONFIRMED |

### 3.2 The three legality gates (byte-exact)

The body resolves the four predicates and then guards three error paths, each an `absl::Status` `MakeErrorImpl<3>` (`kInvalidArgument`) anchored to `custom_kernel_emitter.cc`:

```c
result = 1;  // OK / kOk by default
// GATE 1 — a barrier was requested for a non-communicating custom call.
if ((flags & 0x40) && !has_communication)
    return Error("Custom barrier requested for non-communicating custom call.");   // :3560

// GATE 2 — communication needs a global barrier, but the compiler couldn't allocate a
//          dedicated SFLAG *and* Mosaic isn't allowed to fall back to a device barrier.
//   reached only when (flags & 0x40) is set, has_communication is true,
//   the attached barrier is NOT CUSTOM(3) (v7 == false), and skip_device_barrier (v6) is set:
if ((flags & 0x40)) {
    if (bc->type == 3 /*CUSTOM*/) return result;          // dedicated barrier already allocated → no insert
    if (skip_device_barrier)
        return Error("The compiler failed to allocate a barrier semaphore and Mosaic "
                     "wasn't allowed to perform a global barrier due to skip_device_barrier.");   // :3572
} else if (skip_device_barrier) {
    return result;                                        // no barrier requested + skip → nothing to do
}

// INSERT — walk the module's ops; pick the core-type variant, materialise the barrier.
v12 = 2 - HasAnyCoreType(walk, &kSparseCoreType, 1);      // core-type selector (TC vs SC variant)
ok  = mlir::detail::walk<ForwardIterator>(walk, MaybeInsertGlobalBarrier::$_0(FuncOp), &state, /*WalkOrder=*/1);
// GATE 3 — the walk found no func it could attach a barrier to.
if (!ok) return Error("Requested barrier for unsupported core type");   // :3681
return result;  // OK
```

The semantics for a reimplementer:

- A global barrier is inserted **only** when the custom call communicates (`has_communication`) and either a barrier was explicitly requested (flag `0x40`) without a usable dedicated `CUSTOM(3)` config, or the fall-through insert path is reached. A `CUSTOM(3)` config means the compiler already allocated a per-key SFLAG (via the coloring producer), so `MaybeInsertGlobalBarrier` returns OK without inserting a func-level global one.
- `skip_device_barrier` is the **escape hatch**: if Mosaic is told not to emit a device-wide barrier and no dedicated semaphore was allocated, the gate **fails the compile** (GATE 2) rather than silently dropping the rendezvous — a deadlock-avoidance hard error.
- The actual barrier materialisation is the `walk` callback `MaybeInsertGlobalBarrier::$_0` (a `FuncOp` visitor): it builds the `AllocateAtOffsetOp(MemorySpace::sflag)` + `scf.for(tpu.sem_signal/tpu.sem_wait)` tree on the **SC per-core window** (source (a) in §2), selected by `v12 = 2 − HasAnyCoreType(...)`. This is **not** `GetGlobalBarrierSyncFlagNumber` — the SFLAG offset comes from the Mosaic per-core memory reservation, not the TC `Target+0x8c0` block. The tree protocol itself is on [Tree-Barrier / vSync](tree-barrier-vsync.md).

> **GOTCHA —** the three `CustomCallConfig` field offsets (`+0x10` flags, `+0x90` has-communication, `+0x94` skip_device_barrier) and the bit masks (`0x200`/`0x2000`/`0x40`) are **byte-confirmed** from the decompile reads; the field *names* (`has_communication`, `skip_device_barrier`, `custom_barrier_requested`) are attributed from the RetCheck strings (`"non-communicating custom call"`, `"due to skip_device_barrier"`), not from a proto descriptor. The *behavior* of all three gates is CONFIRMED.

> **NOTE — line numbers.** The three error sites are `custom_kernel_emitter.cc:3560`, `:3572`, `:3681`. The `BarrierConfig.type == 3` read confirms the only barrier kind that short-circuits the global insert is `CUSTOM(3)`; `GLOBAL(1)`/`REPLICA(2)` attached to a communicating custom call still take the insert path. These match the lowering gates summarised in the [overview](overview.md) §3.3.

---

## 4. The REPLICA(2) path the global barrier shares

`REPLICA(2)` is a **TensorCore-only** barrier type. It is *not* the global slot — it maps to `base + id` like `CUSTOM(3)` — but it travels the **same** `net_util::GetBarrierSyncFlag` mapper (§1.2) and the **same** `BarrierCoresTree` actuator as the GLOBAL barrier, differing only in which `TreeBarrierType` (and therefore which peer set) it selects. This is why the global-barrier window and the REPLICA path are documented together: they share the mapper and the tree-barrier engine; they diverge only at the peer set (all cores vs replica-group peers) and the SFLAG slot (`+count+4` vs `base+id`). The classification that *produces* a REPLICA config is on [Infer Barrier Config](infer-barrier-config.md); the full REPLICA lowering on [Replica Barrier](replica-barrier.md). This section documents the *shared* path.

### 4.1 Producer — `DetermineBarrierConfigForKey` @`0x109c6fa0`

`TensorCoreBarrierAssignment::DetermineBarrierConfigForKey(key, config, has_conflict)` builds the `BarrierConfig`. Byte-exact decision tree:

```c
// 0x109c6fa0
BarrierConfig DetermineBarrierConfigForKey(TensorCoreBarrierKey key, HloModuleConfig config, bool has_conflict) {
    BarrierConfig bc{};                                      // ctor(0)
    bool beneficial = IsGlobalBarrierBeneficial(key, config);   // 0x109c6ee0
    if (has_conflict || beneficial) {                        // → GLOBAL(1)
        bc.type = 1;  bc.id = -1;                            // movl $1,+0x20 ; movq -1,+0x18
    } else if (key[+0x10] /*group begin*/ == key[+0x20] - 1 /*group end−1*/) {   // single replica group
        if (config[+0x50] /*partition flag*/ != 1) {         // → REPLICA(2)
            bc.type = 2;  bc.id = key[+0x10];                // shared key id
        } else {                                             // → GLOBAL(1) (partition-scoped singleton)
            bc.type = 1;  bc.id = -1;
        }
    } else {                                                 // → CUSTOM(3)
        bc.type = 3;  bc.id = <fresh via __try_key_extraction_impl @0x109cbd60>;
    }
    bc.hasbits |= 3;                                         // type + id present (orb $3, +0x10)
    return bc;
}
```

`IsGlobalBarrierBeneficial` @`0x109c6ee0` returns true for a **singleton dimension**: gated by `key[+0x51]` (an ICI-routing-known flag — when clear it bails after a `VLOG` "Skipping evaluation of global barrier benefits due to unknown ICI routing limitations.") and `key[+0x50] == 0` (the partition flag, read here off the key), it then checks a config-derived axis pair at `+0x170` (replica_count) / `+0x178` (num_partitions) and returns beneficial only when one of those axes is `1` **and** `key[+0x10] == 1` (a degenerate singleton collective best served by the all-cores global barrier). The producer pass writes only `{1, 2, 3}` — **never `MEGACORE(4)`**.

So `REPLICA(2)` is the **shared-per-replica-group** barrier: a *single* replica group, *not* partition-scoped (`config[+0x50] != 1`), *no* schedule conflict, and *not* globally beneficial. Its `id` is the shared key id (multiple collectives in the group reuse it), satisfying `0 <= id < count` for the `base + id` mapping in §1.2.

### 4.2 Lowering (TensorCore) — `BarrierCoresTree` scoped to the replica group

A TC dense collective with `BarrierConfig.type == 2` lowers via its emitter's `BarrierStart` (e.g. `AllGatherEmitter::BarrierStart` @`0x13809520`). The type dispatch (`r15d == 2`) calls the `$_3` closure with a `net_util::TreeBarrierType` = `(target_predicate ^ 1)` — i.e. `REPLICATED(1)` or `PARTITIONED(2)`. The SFLAG itself comes from `GetBarrierSyncFlag(bc)` → `base + id` (§1.2), and `$_3` → `net_util::GetRegistryTreeBarrierInfoProvider` @`0x1c6a7480` + `net_util::BarrierCoresTree` @`0x1c6a75c0`:

```text
BarrierConfig{type=2, id} ──GetBarrierSyncFlag──▶ sflag = base + id
                          ──$_3(TreeBarrierType)──▶ BarrierCoresTree
                                                     │
   GetOrCreateTreeBarrierInfoTable @0x1c6b60e0:      ▼
     TreeBarrierType 0 ALL_CORES   → variant 0x23  (kAllCoresTreeBarrierInfoTable        @0xb433290)
     TreeBarrierType 1 REPLICATED  → variant 0x2a  (kReplicatedCoresTreeBarrierInfoTable @0xb4332c0)
     TreeBarrierType 2 PARTITIONED → variant 0x2b  (kPartitionedCoresTreeBarrierInfoTable@0xb4332f0)
                                                     │
   BarrierWithinReplicaGroupStartImpl @0x1c698080:   ▼
     GetReplicaGroupCoreInfo  → this core's group peers (from the InfoTable)
     Pneg + Predicated        → gate participation
     for each peer: VsyncAddRemote(sflag, peer)  → signal the peer's barrier sflag
   BarrierWithinReplicaGroupDone @0x1c6984e0 → wait
```

The GLOBAL barrier takes the **same** `BarrierCoresTree` path with `TreeBarrierType = ALL_CORES(0)` over *all* cores; REPLICA narrows the peer set to the current core's replica group. The actuator op is `VsyncAddRemote` @`0x1d522f40` — signal each peer's SFLAG, then wait — the TC analog of the SparseCore `sc_tpu.sync_add` ([Tree-Barrier / vSync](tree-barrier-vsync.md)).

> **[CONFIRMED]** `BarrierWithinReplicaGroupStartImpl` @`0x1c698080`: the decompile shows `GetReplicaGroupCoreInfo` (resolving group peers), `Pneg` @`0x1d5208e0` + `Predicated` @`0x1d520f00` (participation gate), and `VsyncAddRemote` (per-peer signal). The peer set is the **current core's replica group**, not all cores.

### 4.3 Membership encoding — the replica-group `InfoTable`

The replica-group membership is a precomputed **int membership table**, not a bitmask or a per-ring const-literal slice. `net_util::CreateReplicaInfoTable(absl::Span<xla::ReplicaGroup const>, replica_count, partition_count, …)` @`0x1c69b660` → `CreateStaticReplicaInfoTable` @`0x1c69b780` flattens the HLO collective's `replica_groups` attribute into an `xla::InfoTable` backed by an `xla::Literal` R1 `int` array (`LiteralUtil::CreateR1<int>` @`0x10aa05e0`). `GetReplicaGroupCoreInfo` reads this table at the current core's ordinal to recover its group peer ids. `RegisterTreeBarrierInfoTables` @`0x1c6a8620` pre-registers the ALL/REPLICATED/PARTITIONED tables into the `ProgramSharedRegistry` once per program; `GetRegistryTreeBarrierInfoProvider` fetches them at emit time (cached via `GetReplicaGroupCoreInfoCache`).

### 4.4 Why REPLICA(2) never lowers on SparseCore — and the name trap

Both SparseCore custom-kernel barrier entry points **RetCheck** type 2:

- `EmitScsBarrier` @`0x13352500`: only `==1 GLOBAL` / `==3 CUSTOM`; else RetCheck.
- `EmitAllToAllBarrierStart` @`0x133500e0`: only `==1 GLOBAL` / `==3 CUSTOM`; else `RetCheckFailSlowPath(offload_a2a_util.cc:124)` `"backend_config.barrier_config().barrier_type() == jellyfish::BarrierType::GLOBAL"`, message **"Only custom and global barriers are supported for all-to-all collectives on SparseCore"**.

> **GOTCHA — the `EmitReplicaGroupCustomBarrierStart` name trap.** The SparseCore function literally named `EmitReplicaGroupCustomBarrierStart` @`0x13353620` is the lowering for the SC A2A **`CUSTOM(3)`** barrier (and the explicit-id path), **NOT** for `BarrierType::REPLICA`. It emits a predicated `sc_tpu.sync_add` to each member of a group whose membership lives in an **SMEM buffer** at a passed `BufferOffset` — distinct from the TC `REPLICA(2)` whose membership is the `InfoTable` literal (§4.3). On SparseCore there is **no** `BarrierType-REPLICA(2)` lowering at all; embedding collectives use `GLOBAL(1)` or `CUSTOM(3)` only. `REPLICA(2)` is a TensorCore dense-collective barrier type **exclusively**.

> **[CONFIRMED]** `EmitAllToAllBarrierStart` @`0x133500e0`: the RetCheck string and message are present verbatim in the decompile at the `else` arm of the `barrier_type` dispatch — REPLICA(2) is rejected on SparseCore.

---

## 5. Verification notes

> **[CONFIRMED]** Re-derived from the IDA decompile of `libtpu.so` v0.0.40 for this page:
> - `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`: `this[561] + this[560] + 4` = `base + count + 4` — exact.
> - `net_util::GetBarrierSyncFlag` @`0x1c69ad00`: `type==1` → `target()->GetGlobalBarrierSyncFlagNumber()` → `SflagImmPtr("global barrier sync flag")`; `type==0` → CHECK-fail (`net_util.cc:2065`); else → CHECK `id < target[0x8c4]` (`:2070`) → `id + target[0x8c0]` → `SflagImmPtr("barrier sync flag number")` — exact, offsets `0x8c0`/`0x8c4` (=`2240`/`2244`) confirmed.
> - `MaybeInsertGlobalBarrier` @`0x1321ac20`: flags read from `cfg[+0x10]` (bits `0x200`/`0x2000`/`0x40`), `cfg[+0x90]`/`cfg[+0x94]`, `bc->type [+0x20] == 3`; three error sites `custom_kernel_emitter.cc:3560`/`:3572`/`:3681`; **no call to `GetGlobalBarrierSyncFlagNumber`** anywhere in the body; insert via `mlir::detail::walk(MaybeInsertGlobalBarrier::$_0)` with `v12 = 2 − HasAnyCoreType(...)` — exact.
> - `DetermineBarrierConfigForKey` @`0x109c6fa0`: `if (has_conflict || IsGlobalBarrierBeneficial)` → `type=1, id=-1`; else `if (key[+0x10] == key[+0x20]−1)` then `config[+0x50]!=1` → `type=2, id=key[+0x10]` else `type=1,id=-1`; else → `type=3, id=fresh`; `hasbits |= 3` — exact (partition-flag read disassembles to `cmpb $0x1,0x50(%r14)` with `%r14`=`HloModuleConfig`, the 2nd param — **config**, not the key); **no `movl $4`**.
> - `BarrierCoresTree` @`0x1c6a75c0`: calls `GetGlobalBarrierSyncFlagNumber` → `SflagImmPtr("global barrier sync flag")` — confirmed in body.
> - `BarrierWithinReplicaGroupStartImpl` @`0x1c698080`: `GetReplicaGroupCoreInfo` + `Pneg`/`Predicated` + `VsyncAddRemote` — confirmed.
> - `EmitAllToAllBarrierStart` @`0x133500e0`: REPLICA(2) RetCheck (`offload_a2a_util.cc:124`, "Only custom and global barriers are supported for all-to-all collectives on SparseCore") — confirmed.
>
> **[HIGH]** The `CustomCallConfig` field *names* (`has_communication` @`+0x90`, `skip_device_barrier` @`+0x94`, `custom_barrier_requested` bit `0x40`) and the `TensorCoreBarrierKey` field identities (`+0x10`/`+0x20` group span; `+0x50` partition flag and `+0x51` ICI-routing-known guard as read by `IsGlobalBarrierBeneficial`) — offsets byte-confirmed, names attributed from the RetCheck strings and config reads, not from struct descriptors. NB: the REPLICA-vs-GLOBAL partition test inside `DetermineBarrierConfigForKey` reads a **distinct** `+0x50` byte off the `HloModuleConfig` (`%r14`), not off the key.
>
> **[LOW]** The literal per-`(codename, deployment)` `{base, count}` integers (`Target+0x8c0`/`+0x8c4`) are runtime-resolved from embedded chip-config memfile blobs and were not statically extracted; the window *geometry* (`count = |CR_TC| − 5`, GLOBAL @ `+count+4`) is CONFIRMED. See [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md). The net_util `InfoTable` on-wire indexing (how `GetReplicaGroupCoreInfo` maps a core ordinal → its peer set within the flattened R1 int literal) was not fully disassembled — proven to be a `replica_count × partition_count`-keyed int table; the per-entry meaning is LOW.

---

## Cross-References

**Barrier algorithms (this section)**
- [Barriers and Sync-Flags — Section Map](overview.md) — the subsystem map: `BarrierType` enum, producer→normaliser→lowering flow, per-gen SFLAG memory map
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — the `base+count+4` / `base+id` SFLAG-number formulas and the TC reserved-slot map (the formulas this page consumes)
- [Infer Barrier Config](infer-barrier-config.md) — the classification that produces `GLOBAL`/`REPLICA`/`CUSTOM` configs (the normaliser feeding the window + REPLICA path)
- [Replica Barrier](replica-barrier.md) — the full within-replica-group tree-barrier lowering (`REPLICA(2)`)
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the literal per-`(codename, deployment)` `{base, count}` SFLAG-range integers
- [Tree-Barrier / vSync](tree-barrier-vsync.md) — the signal-all-then-wait tree protocol both GLOBAL and REPLICA actuate
- [TensorCore Barrier](tensorcore-barrier.md) — the TC-substrate signal/wait barrier and coloring-chosen `CUSTOM` ids
- [Barrier Coloring](barrier-coloring.md) — the interference-graph engine feeding `DetermineBarrierConfigForKey`'s `has_conflict`
- [Special-Purpose Sync Flags](special-purpose-sync-flags.md) — the `compiler_reserved` range that sources the reserved block

**Sibling subsystems**
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the SFLAG atomic-counter substrate every barrier is built on
- [Collectives](../collectives/overview.md) — the dense collectives that consume the global / REPLICA barriers
- [back to index](../index.md)
