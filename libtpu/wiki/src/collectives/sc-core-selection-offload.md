# SC Core-Selection and the Offload Gate

> *Every address, offset, field number, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). `.text` VMA == file offset (`0xE63C000`); `.rodata` VMA == file offset (`0x84A0000`); `.data.rel.ro` VMA − `0x200000` == file offset. Other versions differ.*

## Abstract

This page is the **collective-stack view of SparseCore (SC) core selection** and the **offload gate** that turns the SC-offload substrate on. It answers two questions a reimplementer of the collective stack must answer before any SC ring config can be built: *(1) is this collective routed to the SC substrate at all,* and *(2) once routed, which physical SC cores does it run on, in what order?* Both questions are decided inside one back-end pass, `xla::jellyfish::SparseCoreQueueAssignment`, and both gate on the **same backend-config enum**: `SparseCoreConfig.offload` (field 2, type `xla::jellyfish::Offload`), read through `backend_config_util::GetSparseCoreConfig` (`0x1C868D20`).

The core-selection policy is a two-function producer. `GetAllowedCores` (`0x10FDA3C0`) computes a *candidate-core mask* — the megachip per-axis chip IDs grouped by the collective's scheduling resource-type IDs `{0, 23..28}`, minus the cores whose per-resource reservation budget is exhausted. `SelectCores` (`0x10FDC4E0`) turns that mask into an *ordered* `vector<long>` by running a **five-phase greedy priority filter** over the cost-sorted candidates, where each phase scans the running `vector<Info>` of already-assigned async collectives. The phase order — same ND plane → data dependency → assignment-group hint → not-on-a-different-plane → fallback — is the placement policy; there is no numeric scorer. This page documents that policy at the level the collective stack consumes it, and the gate that decides whether the policy runs at all. The **byte-level body** of both functions (the `Info` struct, the SIMD core search, the `HloReachabilityMap` bit probe, the Swiss-table mask build) is owned by **[SC Core Selection](../sparsecore/sc-core-selection.md)** — this page links it, it does not re-derive it.

The page is three units: the **offload gate** (`RunHloScheduler`'s capability/platform predicate and the `SparseCoreConfig.offload` op-type classifier that selects the resource arm), **`GetAllowedCores`** (the candidate mask and the reservation budget, from the collective's perspective), and **`SelectCores`** (the five-phase policy and the cost tie-break). The numeric `__sort` into `physical_core_indices` and the per-color→physical-core index mapping are downstream and live on **[Physical-Core Placement](physical-core-placement.md)**.

For reimplementation, the contract is:

- **One enum gates both routing and resource selection.** `SparseCoreConfig.offload` (field 2) decides whether a collective is SC-offloaded (`OFFLOAD_COLLECTIVE = 4` reaches the offload collective config builder) and, in `GetSparseCoreResources`, which scheduling resource-type ID `{0, 23..28}` the collective occupies (which in turn keys the candidate mask).
- **The hardware gate is a capability has-bit OR a platform bool.** `(Target[+0x628] & 4) ∨ (Target[+0x540] ≠ 0)` — the SC-offload-capability has-bit (real hardware) or the `iss`/simulator platform bool — both written in `Target::Init`, with a per-generation basis of `TpuVersion == 5`.
- **Core selection is a candidate mask then an ordered filter, not a scorer.** `GetAllowedCores` → `btree_set<long>` mask; `SelectCores` → ordered `vector<long>` (returned **unsorted** — the caller numerically sorts). A core's rank is `(phase 1..5)` major, `(ascending per-core cost)` minor.
- **The reservation budget is the reserved-core exclusion.** A per-resource thread-local `long` (`__tls_get_addr(&qword_22048D78)`), decremented per assignment and gated `>= 2`, removes cores whose reservation for that resource is exhausted — the embedding-device reserved-core effect.

| | |
|---|---|
| **Offload gate** | `SparseCoreCompiler::RunHloScheduler` (`0x1306F820`) — 5-term predicate |
| **Capability bits** | `Target[+0x628] & 4` (SC-offload has-bit) ∨ `Target[+0x540]` (platform == 2) — set in `Target::Init` (`0x1D60FC20`) |
| **Per-gen basis** | `TpuVersion == 5` (`TpuChipParts[+0] == 5`) |
| **Op-type enum** | `SparseCoreConfig.offload` (field 2, `xla::jellyfish::Offload`); struct `+0x24`, has-bit `+0x10` mask `0x4` |
| **Config reader** | `backend_config_util::GetSparseCoreConfig` (`0x1C868D20`) |
| **Candidate mask** | `GetAllowedCores(HloInstruction*)` (`0x10FDA3C0`) → `btree_set<long,…,256>` |
| **Ordered selection** | `SelectCores(hlo, allowed, devcount, cost, assigned, reach, assign_groups)` (`0x10FDC4E0`) → `StatusOr<vector<long>>` |
| **Resource-type set** | `{0, 23, 24, 25, 26, 27, 28}` (`GetSparseCoreResources`, `0x10FDC0A0`) |
| **Reservation budget** | per-resource thread-local `long` (`__tls_get_addr(&qword_22048D78)`), decremented, gated `>= 2` |
| **Selection phases** | P1 same-plane · P2 data-dep · P3 assign-group · P4 not-different-plane · P5 fallback |
| **Tie-break** | cost-ascending `stable_sort` comparator `$_0` (`vucomisd`, `0x10FE8AE0`) |
| **Confidence** | CONFIRMED (decompile + binary-byte anchored) unless a row or callout says otherwise |

---

## 1. The Offload Gate

### Purpose

Before any SC core is selected, the compile must decide whether the SparseCore-offload latency-hiding scheduler runs at all. That decision is a hardware/platform feature-detect (does this part support SC offload, or is this the simulator?) combined with a module-content check (does the HLO module actually contain an SC instruction?) and a flag. The gate sits in `SparseCoreCompiler::RunHloScheduler` (`0x1306F820`); when it does **not** hold, the collective stays on the dense TensorCore path and none of `GetAllowedCores` / `SelectCores` runs.

This page owns the gate *bits* — what each term means and where it is written. The gate's *role in the substrate split* (which scheduler installs which async tracker) is on **[On-Pod Collectives — Section Map](overview.md#5-the-sc-offload-gate)**; do not duplicate that framing here.

### Algorithm

The full predicate, byte-confirmed in the `RunHloScheduler` decompile (offsets `1576 = 0x628`, `1344 = 0x540`, `148 = 0x94`):

```c
function RunHloScheduler(target, module):                 // 0x1306F820
    chip_cfg = target->chip_config;                        // target[+119*8][+24]
    runSC =  TpuChipConfig::Megachip(chip_cfg)             // line 80 / 193
          && CoresPerChip(kSparseCore) > 0                 // chip_cfg[+0x94] > 0
          && ( (target[+0x628] & 4) != 0                   // SC-offload-capability has-bit
               || target[+0x540] != 0 )                    // platform == 2 (iss/simulator)
    if !runSC: return;                                     // stay on dense TC path
    if !ModuleContainsLEMSparseCoreInstruction(module):    // line 196 (0x13853280)
        return;
    if !ReadOneBool(FLAGS_xla_sc_enable_latency_hiding_scheduler):  // line 243
        return;
    ... run the SparseCore latency-hiding schedulers ...
```

The decompiled condition is verbatim:

```text
*(int *)(v8 + 148) > 0
  && ((*((_BYTE *)this + 1576) & 4) != 0 || *((_BYTE *)this + 1344))     // line 85
```

and is replayed identically at line 193 inside the SC path before the module-content check.

### The Two Target Bits

Both Target fields are written in `jellyfish::Target::Init` (`0x1D60FC20`):

| Field | Meaning | Set where | How |
|---|---|---|---|
| `Target[+0x540]` (byte/bool) | platform type == 2 (the `iss` / simulator platform) | `Target::Init` `@0x1D610B1B` | `sete` from `(TpuTopology[+0] == 2)` |
| `Target[+0x541]` (byte/bool) | platform type == 1 (`grm`) | `Target::Init` `@0x1D610B29` | `sete` from `(TpuTopology[+0] == 1)` |
| `Target[+0x628]` bit-0 (`& 1`) | a config sub-field has-bit (companion) | `Target::Init` `@0x1D611D52` | `\|= 0x1` |
| `Target[+0x628]` bit-2 (`& 4`) | the **SC-offload-capability** has-bit | `Target::Init` `@0x1D612121` | `\|= 0x4`, inside a config-append loop gated by the SC-offload feature-detect |

On real hardware the gate is carried by bit-2 (the per-generation capability set inside the predicate-gated config-append loop). The `platform == 2` branch force-takes the SC path for the `iss` simulator regardless of the capability bit. The same feature-detect predicate — `Megachip ∧ CoresPerChip(SC)>0 ∧ ((+0x628&4) ∨ +0x540) ∧ GetContinuationQueues(SC)[0]==2` — is replayed twice inside `Target::Init` itself (`@0x1D611A4C`, `@0x1D611E52`).

> **NOTE —** `TpuTopology[+0]` is the topology's first scalar = the platform-type enum (internal `{0 hardware, 1 grm, 2 iss}`, descriptor-string order). The gate uses `== 2`; the platform-enum→name pairing is descriptor order, not separately switch-confirmed, but the gate role is byte-exact regardless. **(LOW confidence on the enumerator ordering; CONFIRMED on the `== 2` comparison.)**

### Per-Generation Hardware Basis

The SC-offload concurrency knobs default on for exactly one chip generation. `ShouldEnableConcurrentSparseCoreOffloading` (`0x1D6B6F80`) and `EnableSparseCoreOffloadQueuingInLhs` (`0x1D6B81E0`) both compute their hardware default as `(TpuChipParts[+0] == 5)`, i.e. `TpuVersion == 5`, then let an `AutoOr<bool>` proto flag override (`test eax, 0x100; cmove`). `TpuChipParts[+0]` is the 0-based internal chip-generation enum.

| `TpuVersion` (internal) | codename | proto value (= internal + 1) |
|---|---|---|
| 0 | jellyfish | 1 |
| 1 | dragonfish | 2 |
| 2 | pufferfish | 3 |
| 3 | viperfish | 4 |
| 4 | ghostlite | 5 |
| 5 | `6acc60406` (codename obfuscated in this build) | 6 — **SC-offload default on** |

> **QUIRK —** the gate has *two* off-ramps that look like the same thing but are not. `Target[+0x628]&4` is a per-generation **capability** discovered at `Target::Init`; `FLAGS_xla_sc_enable_latency_hiding_scheduler` is a **runtime flag**; and `ShouldEnableConcurrentSparseCoreOffloading` is a **third** `AutoOr<bool>` knob defaulting to `TpuVersion==5`. A reimplementation that conflates "capability bit" with "the enable flag" will mis-gate the simulator (where `+0x540` force-enables) and mis-gate an older part with the flag forced on (where bit-2 is clear and the gate still fails).

---

## 2. `SparseCoreConfig.offload` — the op-type classifier

### Purpose

Once the gate holds, the SparseCore op type is read from one backend-config enum: `SparseCoreConfig` field 2 `offload`, a `TYPE_ENUM` of type `.xla.jellyfish.Offload`. It is **not** a custom-call target name and **not** an MLIR op kind. Both the candidate-mask producer (`GetSparseCoreResources`, this page §3) and the dense-side scheduler's resource producer (`MayAddSparseCoreResource`) read the *same* field. The enum routes each collective into one scheduling resource-type ID, which is the per-collective resource the reservation budget meters and the mask groups chips under.

### Where the enum lives

`GetSparseCoreConfig` (`0x1C868D20`) returns the full `SparseCoreConfig` proto; consumers read the offload field directly out of the returned struct. In `GetSparseCoreResources` the read is byte-confirmed:

```c
if ( *((_BYTE *)hlo + 12) == 17 )            // opcode == 0x11 (custom-call)        line 44
{
    GetSparseCoreConfig(&cfg, hlo);          //                                     line 46
    if ( (cfg[16] & 4) != 0 )                // has-bit [cfg+0x10] mask 0x4 = field 2 line 47
    {
        switch ( offload_enum )              // enum at [cfg+0x24], indexed enum-1   line 49
        { case 1: ... case 7: ... }
    }
}
else
    GetResourceTypeForOp(async_body_opcode); //                                     line 142
```

The has-bit offset (`+0x10` mask `0x4`) and the value offset (`+0x24`) match the `SparseCoreConfig::_InternalSerialize` field map (field 2, has-bit `0x4`, stored at `+0x24`). The full proto field map and the descriptor bytes are on **[SparseCoreConfig (GetSparseCoreConfig)](../sparsecore/getsparsecoreconfig.md)**.

### The `Offload` enum → resource arm

```text
xla::jellyfish::Offload (9 enumerators, EnumDescriptorProto-confirmed):
  0  OFFLOAD_UNSPECIFIED
  1  OFFLOAD_EMBEDDING
  2  OFFLOAD_GATHER
  3  OFFLOAD_SCATTER
  4  OFFLOAD_COLLECTIVE          ← reaches the offload COLLECTIVE config builder
  5  OFFLOAD_DATA_FORMATTING
  6  OFFLOAD_KERNEL
  7  OFFLOAD_SORT
  8  OFFLOAD_COMPUTE
```

The reservation-map producer `GetSparseCoreResources` indexes the switch by `enum − 1`, so its live arms are `{1..7}`; the dense scheduler's `MayAddSparseCoreResource` indexes by `enum − 2`, so its live arms are `{2..7}`. The mapping each yields, byte-confirmed:

| `Offload` value | Enumerator | `GetSparseCoreResources` (idx = enum − 1) | `MayAddSparseCoreResource` (idx = enum − 2) |
|---|---|---|---|
| 0 | `OFFLOAD_UNSPECIFIED` | (no arm) | (no arm; `rt22 ×N-cores` path) |
| 1 | `OFFLOAD_EMBEDDING` | `rt28` (embedding; `unk_AC0A910` = `0x1C`) | (no arm; `rt22 ×N-cores` path) |
| 2 | `OFFLOAD_GATHER` | `rt23` `kSparseCoreGather` | `rt23` |
| 3 | `OFFLOAD_SCATTER` | `rt24` `kSparseCoreScatter` | `rt24` |
| 4 | `OFFLOAD_COLLECTIVE` | collective arm (async-body recurse) | async-body recurse |
| 5 | `OFFLOAD_DATA_FORMATTING` | `rt25` `kSparseCoreDataFormatting` | `rt25` |
| 6 | `OFFLOAD_KERNEL` | `rt26` `kSparseCoreKernel` | `rt26` |
| 7 | `OFFLOAD_SORT` | `rt27` `kSparseCoreSort` | `rt27` |
| 8 | `OFFLOAD_COMPUTE` | (out of enum − 1 range) | (no arm; `rt22 ×N-cores` path) |

`GetSparseCoreResources` returns the set of these resource-type IDs the collective occupies (one per device-assignment entry it walks). For a non-custom-call op it instead unwraps the `async_wrapped_instruction` and uses `AsyncTracker::GetResourceTypeForOp` (`0x13612240`) over the async body. The set it returns — `{0, 23, 24, 25, 26, 27, 28}` — is the per-collective resource key that §3's mask groups chips under.

> **GOTCHA —** the two index bases (`enum − 1` for the reservation map, `enum − 2` for the dense scheduler) are not a transcription error; they are deliberate. The reservation map additionally covers `OFFLOAD_EMBEDDING(1)` (which the dense scheduler routes through the `rt22 ×N-cores` fallback). A reimplementation that uses one base for both producers will silently drop or double-count the embedding resource.

---

## 3. `GetAllowedCores` — the candidate-core mask

### Purpose

`GetAllowedCores` (`0x10FDA3C0`) computes, for one collective, the set of physical SC cores it is *allowed* to run on — the candidate pool `SelectCores` then orders. From the collective stack's perspective this is where two collective-level facts intersect: the **megachip parallelism** (how the collective's data is split across chips) and the **per-collective scheduling resource** (which the reservation budget meters). The byte-level Swiss-table mechanics are on **[SC Core Selection](../sparsecore/sc-core-selection.md)**; this section documents the *policy inputs* and the reservation interaction a collective-stack reimplementer must reproduce.

### The two inputs

```c
function GetAllowedCores(hlo) -> btree_set<long>:          // 0x10FDA3C0
    chips     = GetChipIDsFromParallelismConfig(hlo)       // line 204 (0x10FDBF40)
    // chips = MegaChipParallelism per-axis split (GetMegaChipParallelism 0x1C867B00)
    for each device-assignment entry in hlo:               // [hlo+0x90]/[hlo+0x98], stride 0x68
        resources = GetSparseCoreResources(member_op)      // line 241/725/930 (0x10FDC0A0)
        // resources ⊆ {0,23,24,25,26,27,28}  — the offload-enum arm from §2
        budget    = *__tls_get_addr(&qword_22048D78)       // line 237/747 per-resource long
        if budget >= 2:                                    // line 244/637/811 — reservation gate
            insert chip into resource's chip_set           // insert_hint_unique (line 522)
            budget--                                        // (*addr)-- (line 636/810)
    ... second pass over [hlo+0xc0] re-applies the budget ...
    return assembled btree_set<long> of surviving cores
```

- **`GetChipIDsFromParallelismConfig` (`0x10FDBF40`)** reads `GetMegaChipParallelism` (`0x1C867B00`) — the same `MegaChipParallelismConfig` per-axis split the caller `AssignQueueIDsToAsyncStart` uses — and emits one `long` "chip ID" per axis value. This is the collective's mega-chip data split; it is the column source for the candidate mask.
- **`GetSparseCoreResources` (`0x10FDC0A0`)** is the §2 producer: the resource-type IDs `{0, 23..28}` the collective occupies. This is the row key.

### The reservation budget — the reserved-core exclusion

The two inputs are accumulated through two Swiss tables (a `flat_hash_map<resource_id, btree_set<chip_id>>` and a `flat_hash_map<chip_id, refcount>`), gated by a **per-resource thread-local reservation budget**. The budget is a `long` reached via `__tls_get_addr(&qword_22048D78)`, decremented once per accepted assignment (`(*addr)--`), and the accept path is gated `if (budget >= 2)`. A chip stops being a candidate for a resource once that resource's budget falls below 2 — this is the embedding-device / reserved-core effect: cores reserved for the embedding device (seeded from `NumEmbeddingDevices` = `sc_dev − num_embedding_devices`) are excluded from the candidate pool.

> **QUIRK —** the reservation budget here is the *live* exclusion that shapes the candidate set, and it is **distinct** from the `SparseCoreQueueAssignment` `[this+0xC0]` `btree_map<long,long>` reservation map — which, in v0.0.40, is built and freed with **no reader** (see [SC Queue Assignment & Reservation](../sparsecore/sc-queue-assignment-reservation.md)). The thread-local budget actually gates `GetAllowedCores`; the `[this+0xC0]` map does not. A reimplementer wiring the `[this+0xC0]` map into core selection would be reproducing a vestigial member, not the live policy.

> **NOTE —** the budget's **initializer** was not traced to its writer. The decrement (`(*addr)--`) and the `>= 2` gate are byte-exact; whether the initial value is `NumEmbeddingDevices`, a fixed cores-per-resource cap of 2, or `LogicalDevicesPerChip(SC)` is **(LOW confidence)** — see WHAT-WE-DO-NOT-HAVE below.

---

## 4. `SelectCores` — the five-phase placement policy

### Purpose

`SelectCores` (`0x10FDC4E0`) takes the `GetAllowedCores` candidate mask and produces an *ordered* `vector<long>` of physical SC cores. The order is the placement policy: a collective is preferentially co-located with the already-assigned collectives it shares structure with (same ND plane, data dependency, assignment group), and the fallback guarantees coverage. The `0x28E0`-byte body — the `Info` struct layout, the SIMD per-core search, the `HloReachabilityMap` bit-matrix probe, the per-phase grow/append — is owned by **[SC Core Selection](../sparsecore/sc-core-selection.md)**; this section documents the *policy* (the five predicates, their order, and the tie-break) at the level the collective stack reads it.

### The policy is a greedy priority filter, not a scorer

There is no closed-form `score(core)` and no core→queue bijection arithmetic anywhere in `SelectCores`. A candidate's final rank is `(phase index 1..5)` major, `(ascending per-core cost)` minor:

```c
function SelectCores(hlo, allowed, devcount, cost, assigned, reach, assign_groups):  // 0x10FDC4E0
    allowed_vec  = materialize(allowed)                      // btree_set -> flat vector<long>
    stable_sort(allowed_vec, $_0)                            // ascending per-core cost (0x10FE8AE0)
    target_plane = TryGetNDPlaneInfoForSparseCoreCollectives(hlo, target_)   // line 425
    selected = []
    for phase in [P1 same-plane, P2 data-dep, P3 assign-group, P4 not-diff-plane, P5 fallback]:
        for core in allowed_vec:                             // ascending-cost order
            if core in selected: continue
            if phase.predicate(core, assigned, reach, assign_groups, target_plane):
                selected.append(core)                        // + per-phase VLOG
    return StatusOr(selected)                                // UNSORTED; caller numerically sorts
```

`SelectCores` returns the vector **unsorted**; the numeric `__sort` into `physical_core_indices` happens in the caller `AssignQueueIDsToAsyncStart` (`0x10FDF480`) — that sort and the proto write are on **[Physical-Core Placement](physical-core-placement.md)**.

### The five predicates

Each predicate scans the running `vector<Info>` of already-assigned async collectives (the `Info` struct carries the assigned op, its physical-core list, and its ND-plane descriptor). The VLOG strings below were each read verbatim from the decompile and pin the source line and phase identity:

| Phase | VLOG line | Predicate on candidate core `c` (scanning `assigned`) | VLOG fragment (byte-read) |
|---|---|---|---|
| P1 same-plane | `0x117` (279) | ∃ `Info` holding `c` whose ND plane == target's ND plane | `… because it is running … on the same ND plane.` |
| P2 data-dep | `0x12E` (302) | ∃ `Info` holding `c` whose op is reachable↔`hlo` (`HloReachabilityMap`) | `… because an assigned op … has data dependency with it.` |
| P3 assign-group | `0x149` (329) | `hlo` ∈ some assignment group ∧ a group member's `Info` holds `c` | `… due to hint from pre-determined assignment groups.` |
| P4 not-diff-plane | `0x16A` (362) | NO `Info` holding `c` is on a *different* ND plane → append | `… because it is not running a collective on a different ND plane.` |
| P4 (skip) | `0x160` (352) | `c` IS on a different plane → skip | `Core … is running … on a different ND plane.` |
| P5 fallback | — | always (append every remaining allowed core) | (none) |

P5 guarantees the selected set covers the whole allowed set; P1–P4 only fix the *order*, which determines membership after the caller truncates the list to the device/megachip count. The phases form a strict priority: a core that any earlier phase claims is never re-evaluated by a later one (the `if core in selected: continue` dup-check).

### The tie-break

Within a phase, ties are broken by the single `stable_sort` of `allowed_vec` into ascending per-core cost (comparator `$_0`, a `vucomisd` on a per-core `double` weight array, in `__stable_sort` `0x10FE8AE0`). The `double cost` argument is the *only* place a numeric weight enters `SelectCores`, and it is consumed *once* at setup to order the candidates — never per-phase. Among cores that equally satisfy a phase's predicate, the lower-cost core is appended first.

> **NOTE —** the **producer** of each core's cost weight (the `LatencyEstimator` / queue-occupancy feed that builds the per-core `double` array and the `cost` argument in the caller) was not traced. The comparator's *use* (ascending sort) is byte-confirmed; the arithmetic that computes each core's cost is **(LOW confidence)** — the tie-break order is structurally pinned but not closed-form. See WHAT-WE-DO-NOT-HAVE.

---

## 5. Verification notes

> The offload gate, the offload enum classifier, and the five-phase policy were cross-checked against the IDA decompile of `libtpu.so` v0.0.40:
> - `SparseCoreCompiler::RunHloScheduler` (`0x1306F820`): the predicate `*(int *)(v8 + 148) > 0 && ((*((_BYTE *)this + 1576) & 4) != 0 || *((_BYTE *)this + 1344))` (offsets `0x94`/`0x628`/`0x540`), the `Megachip` call, `ModuleContainsLEMSparseCoreInstruction`, and `FLAGS_xla_sc_enable_latency_hiding_scheduler` — all present (decompile lines 80/85/193/196/243).
> - `GetSparseCoreResources` (`0x10FDC0A0`): opcode `[hlo+12] == 17` (0x11), `GetSparseCoreConfig`, has-bit `(cfg[16] & 4)` (`+0x10` mask `0x4`), the offload-enum `switch` arms `case 1..7`, and the non-custom-call `GetResourceTypeForOp` path — exact (lines 44/46/47/49/142).
> - `GetAllowedCores` (`0x10FDA3C0`): `GetChipIDsFromParallelismConfig` (line 204), `GetSparseCoreResources` (lines 241/725/930), `__tls_get_addr(&qword_22048D78)` budget (lines 237/747), the decrement `(*addr)--` (lines 636/810), the `>= 2` gate (lines 244/637/811), and `insert_hint_unique` (line 522) — exact.
> - `SelectCores` (`0x10FDC4E0`): the `stable_sort` comparator `$_0` (lines 298/416), `TryGetNDPlaneInfoForSparseCoreCollectives` (line 425), and all five phase VLOG fragments (`… on the same ND plane.` / `… has data dependency with it.` / `… due to hint from pre-determined assignment groups.` / `… not running a collective on a different ND plane.` / `… on a different ND plane.`) — exact.
>
> **[LOW]** Three residual unknowns, each marked in place above: (1) the per-core cost-weight **producer** (the tie-break order is structurally pinned, not closed-form); (2) the `GetAllowedCores` reservation-budget **initializer** (the decrement and `>= 2` gate are exact; the initial value is one of `NumEmbeddingDevices` / fixed-2 / `LDPC(SC)`); (3) the platform-type enumerator ordering behind `Target[+0x540]`'s `== 2` (the comparison is exact; the `{hardware,grm,iss}` enumerator order is descriptor-string order, not switch-confirmed).

---

## What We Do Not Yet Have

1. **The per-core cost-weight producer.** The `stable_sort` comparator `$_0` orders `allowed_vec` by ascending `cost[core]`, fed by the `double cost` argument and a per-core weight array, but the arithmetic that *computes* each core's cost (the `LatencyEstimator` / queue-occupancy feed in `AssignQueueIDsToAsyncStart` / `AssignQueueIDsForComputation`) was not traced — the tie-break is structurally pinned (ascending cost) but not closed-form.
2. **The reservation-budget initializer.** The thread-local per-resource budget is decremented and gated `>= 2`, but the writer that seeds the TLS `long` and its initial value (`NumEmbeddingDevices` reservation vs a fixed cores-per-resource cap of 2 vs `LogicalDevicesPerChip(SC)`) was not traced to the initialization site.
3. **The resource-type → physical-core registry.** `GetSparseCoreResources` returns the `{0, 23..28}` resource-type IDs and `GetAllowedCores` groups chips under them, but the global table mapping a resource-type ID to the set of physical SC cores it can occupy (and how megacore pairing collapses two cores into one resource slot) was not enumerated.

---

## Related Components

| Component | Relationship |
|---|---|
| `SparseCoreCompiler::RunHloScheduler` (`0x1306F820`) | Owns the offload gate; runs the SC latency-hiding schedulers when it holds |
| `SparseCoreQueueAssignment` | The pass that owns `GetAllowedCores` + `SelectCores` (this page's policy) |
| `AssignQueueIDsToAsyncStart` (`0x10FDF480`) | Per-collective driver: calls `GetAllowedCores` → `SelectCores` → `__sort` → proto write |
| `GetSparseCoreConfig` (`0x1C868D20`) | Reads the `SparseCoreConfig.offload` enum that gates both routing and resource selection |

## Cross-References

- [On-Pod Collectives — Section Map](overview.md) — the substrate split and the gate's role in selecting the SC scheduler (§5); this page is the gate-bits + selection-policy detail
- [SC Core Selection](../sparsecore/sc-core-selection.md) — the **byte-level** `GetAllowedCores` / `SelectCores` body (the `Info` struct, SIMD search, `HloReachabilityMap` probe, Swiss-table mask build) this page summarizes
- [Physical-Core Placement](physical-core-placement.md) — the downstream numeric `__sort` and the per-color→physical-core index mapping into `physical_core_indices`
- [SC-Offload Config Builder](sc-offload-config-builder.md) — `ConstructConfigForCollectiveUniDirNDGroups<*>` and the `*OffloadConfig` proto the gated collective produces
- [HierarchicalKind](hierarchical-kind.md) — the flat-vs-hierarchical phase split inside the offload collective config builder
- [Tensor-Split / ND-Plane](tensor-split-ndplane.md) — the `tensor_split_factor` / ND-plane derivation that consumes the *count* of selected cores
- [SparseCoreConfig (GetSparseCoreConfig)](../sparsecore/getsparsecoreconfig.md) — the full `SparseCoreConfig` proto field map and the `Offload` enum descriptor bytes
- [SC Queue Assignment & Reservation](../sparsecore/sc-queue-assignment-reservation.md) — the `[this+0xC0]` reservation `btree_map` (the *vestigial* twin of the live thread-local budget §3 enforces)
- [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) — the `{0, 23..28}` scheduling resource-type space these resources index into
- [back to index](../index.md)
