# InferBarrierConfig

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`InferBarrierConfig` @`0x1376c240` is the **pincer-fusion barrier canonicaliser**: a small decision tree that runs at fusion-emit time, re-reads the `BarrierConfig` a collective already carries in its HLO `BackendConfig`, and *downgrades* a per-key `CUSTOM` barrier to the cheaper `GLOBAL` or `REPLICA` when it can prove the collective's communicating set is genuinely multi-participant. It is the **second** of the two `BarrierConfig` touchpoints — distinct from the coloring producer `TensorCoreBarrierAssignment::DetermineBarrierConfigForKey` @`0x109c6fa0`, which writes the original config at HLO-pass time. The two never share state; they communicate only through the proto field. This page derives the tree byte-exact and documents the **per-generation SFLAG memory map** the tree resolves against — the per-codename base/count blocks `Target::Init` / `SparseCoreTarget::Init` carve from the chip config.

The tree is short and has exactly three structural arms. The first is a guard: a **singleton-group predicate** `a4[1] <= 1 && a4[2] <= 1` over the Strategy's two-axis participant counts. A singleton set takes no rewrite (validate only). The second arm fires on a multi-participant set whose `barrier_type == CUSTOM(3)`: it consults `hlo->channel_id()` and rewrites to `GLOBAL(1)` with the sentinel `id = -1` if the collective is channelled, or `REPLICA(2)` with `id = Target[+0x8c4] − 1` (the last usable TC barrier-id slot) if it is not. The third arm is the legality gate: `BARRIER_INVALID(0)` is always rejected with a RetCheck. An already-set `GLOBAL`/`REPLICA` is kept untouched; `MEGACORE(4)` is **never** written.

The `REPLICA` id, `Target[+0x8c4] − 1`, ties the tree to the **SFLAG block geometry**: `Target+0x8c4` is the TC barrier-id count, one of a pair of `{base, count}` integers that `Target::Init` reads from the chip-config `compiler_reserved` repeated-int32 range and that every reserved-slot SFLAG formula is parametric in. This page documents that per-gen map — the base/count carve and the gen-independent block structure — so a reimplementer can resolve `Target[+0x8c4] − 1` and the five reserved top slots without re-tracing the chip config. The `BarrierType` enum, the coloring engine, the SFLAG-number binding, and the literal per-codename integers are sibling pages (links below); this page owns the **decision tree and the SFLAG base/count map**.

For reimplementation, the contract is:

- **The three-arm decision tree** — the singleton-group predicate `a4[1] <= 1 && a4[2] <= 1`, the `CUSTOM(3)` → channel-id-gated `GLOBAL`/`REPLICA` rewrite, and the `INVALID(0)` RetCheck — with the exact local layout (`type @-0x30`, `id @-0x38`, `hasbits @-0x40`) so the rewrite writes the right bytes.
- **The two rewrite targets:** `GLOBAL(1)` carries `id = -1`; `REPLICA(2)` carries `id = Target[+0x8c4] − 1`, the top usable TC barrier-id slot.
- **The per-gen SFLAG memory map** the tree resolves `Target[+0x8c4]` against: the `{base @+0x8c0, count @+0x8c4}` TC block (`count = |CR_TC| − 5`), the `{base @+0x1d0, count @+0x1d4}` SC block (`count = |CR_SC|`, no `−5`), and the five reserved TC top slots.

| | |
|---|---|
| **Function** | `xla::jellyfish::(anonymous namespace)::InferBarrierConfig` @`0x1376c240` |
| **Signature** | `Status InferBarrierConfig(Target const&, HloInstruction const*, Strategy*) → StatusOr<BarrierConfig>` |
| **TU** | `platforms/xla/service/jellyfish/lowering/rotated_pincer_fusion_emitter.cc` |
| **Callers** | 8, all RotatedPincer / RotatedPincerShort / AsyncPincer fusion emitters |
| **Predicate** | `*((int64*)a4 + 1) <= 1 && *((int64*)a4 + 2) <= 1` (Strategy `+0x8` / `+0x10`) |
| **CUSTOM arm** | `type==3` → channelled? `GLOBAL(1), id=-1` : `REPLICA(2), id=Target[+0x8c4]−1` |
| **Never writes** | `MEGACORE(4)` — no `movl $4` anywhere in the body |
| **TC block** | `Target+0x8c0` base / `Target+0x8c4` count (`= \|CR_TC\| − 5`) |
| **SC block** | `SparseCoreTarget+0x1d0` base / `+0x1d4` count (`= \|CR_SC\|`, no `−5`) |

The enum + producer→normaliser→lowering flow is on [Barriers — Section Map](overview.md); the coloring engine on [Barrier Coloring](barrier-coloring.md); the SFLAG-number lowering on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the `compiler_reserved` proto source on [Special-Purpose Sync Flags](special-purpose-sync-flags.md); the literal per-gen integers on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md). This page does not duplicate them.

---

## 1. The Decision Tree

### Purpose

A pincer fusion is emitted from one HLO collective whose barrier was already colored at HLO-pass time. At emit time the fusion's *actual* communicating-set shape is known — the participant counts on its two axes — which the coloring pass did not have. `InferBarrierConfig` exploits that: if the set is genuinely multi-participant, a per-key `CUSTOM` barrier is wasteful (it burns a fresh SFLAG id for a barrier that could share the device-global or replica-group slot), so the tree collapses it to the cheaper shared kind. If the set is degenerate (singleton on both axes), the explicit `CUSTOM` coloring is left alone. The function is a *normaliser*, not a producer: it only ever downgrades `CUSTOM`, and it never touches an already-set `GLOBAL`/`REPLICA`.

### Entry Point

```text
RotatedPincerFusionEmitter / RotatedPincerShortFusionEmitter / AsyncPincerFusionEmitter
  ├─ CreateEmitterForMultipleInputsOrOutputs    @0x1376b96c
  ├─ EmitSingleInputAllReduceScatterFusion      @0x1376dac9
  ├─ EmitSingleInputAllGatherFusion             @0x13773230
  ├─ EmitColorwiseFusedAllReduces (Rotated)     @0x13774e15
  ├─ EmitColorwiseFusedAllReduces (Short)       @0x1377554e
  ├─ EmitSingleInputAllReduceScatterFusion (A)  @0x137760a0
  ├─ EmitAllReduceScatterFusion (Async)         @0x137775d3
  └─ EmitColorwiseFusedAllReduces (Async)       @0x13778359
        └─ InferBarrierConfig(target, hlo, strat)   @0x1376c240   ── this page
```

All eight callers are direct `E8 rel32` calls resolved by a full `.text` xref scan; there is no virtual dispatch. `InferBarrierConfig` lives in the file-local anonymous namespace of `rotated_pincer_fusion_emitter.cc`, so it has no external linkage — it is reachable only from the eight pincer emitters in that TU.

### Algorithm

The body is short. Below is the annotated tree; the IDA locals (`v35` = type, `v34` = id, `v33` = hasbits, `a4` = Strategy, `a2` = Target, `a3` = HLO) are named, with the decompiler line numbers and frame offsets cited for cross-check.

```c
function InferBarrierConfig(target /*a2*/, hlo /*a3*/, strat /*a4*/):   // 0x1376c240
    cfg = hlo->backend_config<BackendConfig>()                  // 0xf58e6c0; StatusOr @-0x490
    if (!cfg.ok())                                              // line 37
        return RetCheck(line 93, rotated_pincer_fusion_emitter.cc)   // AddSourceLocationImpl, line 41

    BarrierConfig bc;                                           // local @-0x50 (ctor line 71)
    if (cfg.has_barrier_config())                               // hasbit 0x10 in v27 @-0x268, line 72
        bc.CopyFrom( cfg.barrier_config() ?: BarrierConfig_globals_ )  // default @0x223a9450, lines 74-77
    // bc fields now: type = v35 @-0x30, id = v34 @-0x38, hasbits = v33 @-0x40

    // ARM 1 — singleton-group predicate (the GUARD)
    if (strat[1] <= 1 && strat[2] <= 1):                        // *((int64*)a4+1)<=1 && *((int64*)a4+2)<=1, line 79
        type = bc.type                                          // v14 = v35, validate-only (line 81)
        // fall through to legality gate (ARM 3)
    else:                                                       // MULTI-participant on either axis
        type = bc.type                                          // v14 = v35, line 85
        // ARM 2 — CUSTOM downgrade
        if (bc.type == CUSTOM /*3*/):                           // if (v35 == 3), line 86
            hlo->channel_id()                                   // 0x1e59ff80; has_value → v15 (dl), line 88
            if (v15 == 1):                                      // channelled collective, line 89
                bc.type = GLOBAL  /*1*/;  bc.id = -1            // v35=1, v16=-1, lines 91-92
            else:                                               // non-channelled
                bc.type = REPLICA /*2*/                         // v35=2, line 97
                bc.id   = target[0x8c4] - 1                     // v16 = *((int*)a2 + 561) - 1, line 99
            bc.hasbits |= 3                                     // v33 = v17 | 3, line 102
            goto WRITE                                          // LABEL_24, line 103
        // else (already GLOBAL/REPLICA, type != 3) → keep; fall to ARM 3

    // ARM 3 — legality gate (reached by singleton OR multi-but-not-CUSTOM)
    if (type != 0):  goto WRITE                                 // if (v14) goto LABEL_24, line 146
    return RetCheck(line 115,                                   // BARRIER_INVALID rejected, line 148
                    "barrier.barrier_type() != BarrierType::BARRIER_INVALID")

WRITE:                                                          // LABEL_24, line 103
    out.barrier_config = bc                                     // BarrierConfig ctor+CopyFrom into this+8
    *(int64*)this = 1                                           // StatusOr "ok" tag, line 109
    return OK
```

> **NOTE —** the source line numbers in the RetCheck calls are `93` and `115` decimal (`0x5d` and `0x73` in the raw immediates), both referring to the same two `rotated_pincer_fusion_emitter.cc` lines. The `INVALID` RetCheck string `"barrier.barrier_type() != BarrierType::BARRIER_INVALID"` is the only `.rodata` anchor that pins `BARRIER_INVALID = 0` by name.

### The three arms, restated

The tree has exactly three structural outcomes for a reimplementer to reproduce:

| Arm | Entry condition | Action |
|---|---|---|
| **1 — Singleton guard** | `strat[1] <= 1 && strat[2] <= 1` (both axes ≤ 1) | No rewrite. Drop to ARM 3 (validate only). |
| **2 — CUSTOM downgrade** | multi-participant **and** `type == CUSTOM(3)` | channelled → `GLOBAL(1), id=-1`; else `REPLICA(2), id=count−1`; `hasbits \|= 3`; write. |
| **3 — Legality gate** | reached by singleton, or by multi-but-already-`GLOBAL`/`REPLICA` | `type != 0` → keep & write; `type == 0` (`INVALID`) → RetCheck. |

> **QUIRK —** the `CUSTOM(3)` test sits **only inside the multi-participant `else` branch**. A `CUSTOM` barrier on a *singleton* communicating set is never downgraded — it falls into ARM 3, where `type == 3` is non-zero, so it is kept verbatim. The only way a `CUSTOM` becomes `GLOBAL`/`REPLICA` is a strictly multi-participant set. A reimplementation that hoists the `type == 3` check above the predicate will wrongly collapse degenerate single-core collectives onto the shared barrier.

> **GOTCHA —** the rewrite is a one-way downgrade gated on the enum *value*, not a general remap. `type == 3` is the only value that triggers a write of a new type; `type == 1`/`type == 2` fall straight through ARM 3 unchanged. There is **no** `movl $4` and no path that produces `MEGACORE(4)` — confirmed by reading the full body. A switch driven off all five `BarrierType` values has a dead `case 4` here.

---

## 2. The Predicate — Strategy's Two-Axis Participant Counts

### What it reads

The guard is two independent `int64` compares against the constant `1`:

```c
if ( *((__int64 *)a4 + 1) <= 1 && *((__int64 *)a4 + 2) <= 1 )   // line 79
```

`a4` is the `Strategy*`. The two fields are `Strategy+0x8` (`a4[1]`) and `Strategy+0x10` (`a4[2]`). Both are 64-bit signed loads compared with `cmpq $1` — `a4[1] > 1` and `a4[2] > 1` are the multi-participant conditions, ORed (the decompiler renders the De Morgan dual as the `&&` of the ≤-1 tests). A set that is `1` (or `0`) on **both** axes is "singleton"; `> 1` on **either** axis is "multi-participant".

### What the axes mean

The two fields are the participant counts of the pincer collective's communicating set on its two axes — the phase-0 / phase-1 ring or replica-group sizes the StrategyND emitter builds from the HLO's `replica_groups` / `ShardingConfig` before invoking the emitter. "Multi-participant" (`> 1` on either axis) means a genuine cross-core rendezvous is required, so the per-key `CUSTOM` coloring is collapsed to the cheaper shared barrier; "singleton" means the collective is degenerate / single-core and the explicit colored barrier is kept.

> **GOTCHA —** the offsets `Strategy+0x8` / `Strategy+0x10` are byte-confirmed (`cmpq $1` on each), but the field *names* (which axis is the phase-0 ring length versus the phase-1 replica-group count) are attributed from the StrategyND fusion context, **not** from a struct descriptor. Treat the "ring length vs replica-group count" reading as inferred (MEDIUM). The *behavior* — `> 1` on either axis triggers the downgrade — is CERTAIN. The Strategy writers that set `+0x8`/`+0x10` (e.g. `StrategyND::BuildStrategy` and the `GetPhase{0,1}ReplicaGroups` helpers) were not individually traced for this page.

---

## 3. The Two Rewrite Targets

When ARM 2 fires, the tree writes one of two `{type, id}` pairs into the local `BarrierConfig` (`type @-0x30`, `id @-0x38`, `hasbits @-0x40`), then `hasbits |= 3` to mark both fields present.

### GLOBAL — channelled collective

```c
xla::HloInstruction::channel_id(a3);   // 0x1e59ff80
if (v15 /*has_value, dl*/ == 1) {
    v35 = 1;     // BarrierType::GLOBAL @-0x30
    v16 = -1;    // id = -1 (the sentinel GLOBAL id) @-0x38
}
```

A `channel_id` present on the collective marks it a cross-module / cross-device **channelled** collective, which must rendezvous on the device-wide global barrier. The tree writes `GLOBAL(1)` with the sentinel `id = -1`; the lowering resolves a `GLOBAL` barrier's SFLAG number from the reserved top slot `base+count+4` (`GetGlobalBarrierSyncFlagNumber`, §4), ignoring the `-1` id placeholder. `channel_id` @`0x1e59ff80` returns `optional<int64>` with `value = *(hlo+0xc0)`, `has_value = *(hlo+0xc8)` loaded into `dl` and compared `== 1`.

### REPLICA — non-channelled collective

```c
else {
    v35 = 2;                          // BarrierType::REPLICA @-0x30
    v16 = *((int *)a2 + 561) - 1LL;   // id = Target[+0x8c4] - 1 @-0x38
}
```

No `channel_id` means a within-replica-group collective, which needs only a replica-group tree barrier. The tree writes `REPLICA(2)` and pins the id to `*((int*)a2 + 561) − 1` = `Target+0x8c4 − 1` = `count − 1` — the **last usable TC barrier-id slot** in the reserved block (`561 * 4 = 0x8c4`; see §4 for why `Target+0x8c4` is the count). This is the single point where the decision tree depends on the per-gen SFLAG map: the `REPLICA` id is not a fresh allocation but a fixed reference to the top of the usable id window.

> **QUIRK —** the `REPLICA` id is pinned to `count − 1`, the same slot every pincer fusion's `REPLICA` downgrade lands on. This is deliberate sharing: all replica-group pincer barriers from this normaliser reuse one id (the top of the usable window), distinct from the coloring producer's `REPLICA` ids (which are shared *per key*). A reimplementer must not allocate a fresh id here — the value is a fixed function of the chip-config count.

---

## 4. The Per-Generation SFLAG Memory Map

The decision tree's `REPLICA` id (`Target[+0x8c4] − 1`) and the lowering of every barrier kind resolve against a per-core-type **reserved SFLAG block** carved at target init. This section documents the block geometry the tree depends on. The proto *source* of the block (`compiler_reserved`) is on [Special-Purpose Sync Flags](special-purpose-sync-flags.md); the *numeric binding* of an id to an SFLAG memref is on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the literal per-codename integers are on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md). This page owns the **base/count carve and the reserved-slot map**.

### The carve

`Target::Init` @`0x1d60fc20` copies the `compiler_reserved(TensorCore)` repeated-int32 range from the chip config, CHECKs it is contiguous-ascending, and writes:

```c
*((_DWORD *)target + 560) = arr[0];        // base  → Target+0x8c0   (560*4 = 0x8c0)
*((_DWORD *)target + 561) = size - 5;      // count → Target+0x8c4   (561*4 = 0x8c4)
```

The `count = size − 5` reserves the top 5 of the range for the named cross-core barrier sync flags (below). `SparseCoreTarget::Init` @`0x1d612b20` does the same for `compiler_reserved(SparseCore)` but **without the `−5`**:

```c
*(_DWORD *)(sctgt + 464) = SpecialPurposeSyncFlags[1];      // SC base  → SparseCoreTarget+0x1d0
*(_DWORD *)(sctgt + 468) = SpecialPurposeSyncFlags->size;   // SC count → SparseCoreTarget+0x1d4 (FULL, no −5)
```

The SC block is full and reserves its global-barrier id *within* `[SC_base, SC_base+SC_count)`. TC and SC ranges are disjoint by construction — they are different `SpecialPurposeSyncFlags` proto messages, keyed by distinct `TpuCoreType`, read from `GetSpecialPurposeSyncFlags(core)` @`0x20afcf40` (index `core << 6`, i.e. `+0x2a0 + core*0x40`; the TensorCore entry is mandatory or `Target::Init` dies via `DieBecauseNull`).

| Block | base field | count field | count formula |
|---|---|---|---|
| TensorCore | `Target+0x8c0` (`target[560]`) | `Target+0x8c4` (`target[561]`) | `\|CR_TC\| − 5` |
| SparseCore | `SparseCoreTarget+0x1d0` (`+464`) | `SparseCoreTarget+0x1d4` (`+468`) | `\|CR_SC\|` (no `−5`) |

> **GOTCHA —** `SparseCoreTarget+0x90` is **not** an SFLAG-window base: `*(sctgt+144) = TpuCoreParts::SequencerCount(core, 5)`, a per-core sequencer count, not a barrier id. `SparseCoreTarget+0x1fc` (`*(sctgt+508) = v77 − 4`) is the `GetMemoryReservation → GetUserRegion` length (the Mosaic per-core tree-barrier window, MemorySpace 14), a third disjoint region. Neither is part of the `compiler_reserved` SFLAG block. The SC tree-barrier window is on [Tree-Barrier / vSync](tree-barrier-vsync.md).

### The five reserved TC top slots (the `−5`)

The `count = size − 5` carves the top 5 ids of the TC range into the named cross-core barrier sync flags. All three accessor formulas are byte-exact (`target[560]` = `Target+0x8c0` = base; `target[561]` = `Target+0x8c4` = count):

| Slot | Accessor | Formula |
|---|---|---|
| `base + count + 0` | `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0` | `target[560] + target[561]` = `base + count` (`Megacore()`-gated; CHECK `"topology_->chip_config().Megacore()"`, line 154) |
| `base + count + 1` | (gap; `GetAllReduceSyncFlagNumber(0)` illegal — CHECK `phase > 0`) | `base + count + 1` (permanent gap) |
| `base + count + 2` | `GetAllReduceSyncFlagNumber(1)` @`0x1d60f440` | `target[560] + 1 + target[561] + 1` = `base + count + 2` |
| `base + count + 3` | `GetAllReduceSyncFlagNumber(2)` @`0x1d60f440` | `base + count + 3` |
| `base + count + 4` | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` | `target[561] + target[560] + 4` = `base + count + 4` |

`GetAllReduceSyncFlagNumber(phase)` is `LogMessageFatal`-bounded to `0 < phase < 3` (CHECK lines 143/144), which is why `base+count+1` is a permanent gap — `phase = 0` is illegal, so no caller can name it. The **usable per-id window** is `[base, base+count)`: `REPLICA` and `CUSTOM` ids satisfy `id < count`, sitting strictly *below* the five reserved slots. This is exactly the window the decision tree's `REPLICA` id, `count − 1`, indexes — the top of the usable range, one below the first reserved slot.

```text
TC SFLAG block (Target+0x8c0 = base, Target+0x8c4 = count):

  base                          base+count                      base+count+5
   |  usable per-id window [base, base+count)  |   5 reserved top slots   |
   |  CUSTOM / REPLICA ids (id < count)         |  mega gap ar1 ar2 glob   |
   |                          ^                 | +0   +1  +2  +3  +4      |
   |                          |                 |
   |          REPLICA id = count-1 (normaliser, §3) lands here
```

### Per-gen structure (parametric)

Let `CR_TC = compiler_reserved(TensorCore)` and `CR_SC = compiler_reserved(SparseCore)` for a given `(codename, deployment-name)` chip config. The block structure is **identical across every generation** — only the integers differ, because the `−5` is a compile-time constant in `Target::Init` (the decompiler renders it `size − 5`; the disassembly is `add $0xfffffffb`), and every reserved-slot formula is parametric in `(base, count)`.

| Gen (codename) | TC block (`+0x8c0`/`+0x8c4`) | SC block (`+0x1d0`/`+0x1d4`) | TC top-5 reserved (within block) |
|---|---|---|---|
| JF (`kJellyfish`, v2) | `base=CR_TC[0]`, `count=\|CR_TC\|−5` | `base=CR_SC[0]`, `count=\|CR_SC\|` | mega `b+c`, gap `b+c+1`, ar1 `b+c+2`, ar2 `b+c+3`, glob `b+c+4` |
| DF (`kDragonfish`, v3) | same | same | same 5-slot map |
| PF (`kPufferfish`, v4) | same | same | same 5-slot map |
| VF (`kViperfish`, v5p) | same | same | same 5-slot map |
| GL (`kGhostlite`, v6e) | same | same | same 5-slot map |
| GF (`k6acc60406`, v7) | same | same | same 5-slot map |

Megacore deployments (`megacore*`, `megachip`) are the ones for which `CoresPerChip(TensorCore) == 2` → `BarrierMegacore` is active and the `base+count` megacore slot is consumed; other deployments leave it reserved-unused (the `Megacore()` gate on `GetMegacoreBarrierSyncFlagNumber` fails). The per-gen table is a *structure*, not a value table — the literal `CR_TC[0]` / `|CR_TC|` / `CR_SC[0]` / `|CR_SC|` integers are an embedded-memfile dependency.

> **GOTCHA —** the literal per-`(codename, deployment-name)` integers are **not statically extractable** from `.rodata`. They live in embedded chip-config memfile binarypb blobs (`tpu_chip_config_memfile_{default,megacore,megachip,…}_embed_internal_create` @`0x20b18fa0..`), resolved at runtime via a `flat_hash_map<tuple<TpuVersion, name, TpuCoreType>, FileToc*>` keyed by `FLAGS_deepsea_chip_config_name` @`0x224714b0`. The block geometry and the `−5` are CONFIRMED; the integers are LOW (memfile dependency). See [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md).

---

## 5. Relationship to the Coloring Producer

`InferBarrierConfig` is one of **two** writers of the `BackendConfig.BarrierConfig` field; a reimplementer must keep them distinct.

| Producer | When it runs | Writes | id source |
|---|---|---|---|
| `DetermineBarrierConfigForKey` @`0x109c6fa0` | HLO barrier-assignment pass (per key) | `GLOBAL(1)` / `CUSTOM(3 fresh)` / `REPLICA(2 shared)` | `-1` (GLOBAL) / fresh / shared key id |
| `InferBarrierConfig` @`0x1376c240` | pincer fusion emit (per fusion, 8 callers) | `CUSTOM → GLOBAL(1, id=-1)` if channelled; `CUSTOM → REPLICA(2, id=count−1)` if not | `-1` (GLOBAL) / `count−1` (REPLICA) |

`DetermineBarrierConfigForKey` is the **authoritative coloring producer** at HLO-pass time: it runs over the per-key conflict/coloring map (fed by [Barrier Coloring](barrier-coloring.md)) and assigns the original `{type, id}`. `InferBarrierConfig` is a per-fusion **normaliser** that only fires later, at emit time, and only downgrades a `CUSTOM` choice — it never upgrades, never rewrites an already-set `GLOBAL`/`REPLICA`, and never writes `MEGACORE(4)`. Both results feed the same lowering ([Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md)) → the same per-gen SFLAG number space (§4): a `GLOBAL` resolves to `base+count+4`, a `REPLICA` to `base+id` (with `id = count−1` from this normaliser, the top usable slot).

> **NOTE —** the division of labor is "color, then specialise." The coloring pass decides barrier *sharing* over the whole module without knowing each fusion's participant set; the normaliser specialises the surviving `CUSTOM` choices once the pincer fusion's actual ring/replica-group shape is materialised. Neither alone is the complete barrier-assignment story — the `BarrierConfig` a kernel finally lowers is the *normaliser's* output when a pincer fusion was involved, the *coloring's* output otherwise.

---

## 6. Verification Notes

> Byte-exact in `libtpu.so` v0.0.40:
> - `InferBarrierConfig` @`0x1376c240` full body: predicate `*((__int64*)a4+1) <= 1 && *((__int64*)a4+2) <= 1` (line 79); `if (v35 == 3)` CUSTOM (line 86); `channel_id(a3)` then `v15 == 1` → `v35=1, v16=-1` GLOBAL (lines 88-92) else `v35=2, v16 = *((int*)a2+561) − 1` REPLICA (lines 97-99); `v33 = v17 | 3` hasbits (line 102); `if (v14)` keep-else-RetCheck line 115 `"barrier.barrier_type() != BarrierType::BARRIER_INVALID"` (line 148); default `BarrierConfig_globals_` @`0x223a9450` (line 76); **no `movl $4`** — exact.
> - `Target::Init` @`0x1d60fc20`: `target[560] = arr[0]` (base → `+0x8c0`), `target[561] = size − 5` (count → `+0x8c4`) — exact (decompile lines 2067-2068).
> - `SparseCoreTarget::Init` @`0x1d612b20`: `(sctgt+464) = SPSF[1]` (SC base → `+0x1d0`), `(sctgt+468) = SPSF->size` (SC count → `+0x1d4`, no `−5`); `(sctgt+144) = SequencerCount(core, 5)` (`+0x90`, not SFLAG) — exact.
> - `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`: `target[561] + target[560] + 4` = `base + count + 4` — exact.
> - `GetAllReduceSyncFlagNumber` @`0x1d60f440`: CHECK `phase > 0` (143) / `phase < 3` (144); `target[560] + phase + target[561] + 1` — exact.
> - `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0`: `Megacore()`-gated; `target[560] + target[561]` = `base + count` — exact.
> - `GetSpecialPurposeSyncFlags` @`0x20afcf40`: `_bittest64(*(chip+864), core)` gate; `core >= 3` → `ud1`; `return chip + 672 + (core << 6)` = `+0x2a0 + core*0x40` — exact.
> - 8 callers, all pincer fusion emitters, by `E8 rel32` xref scan — exact.
>
> **[MEDIUM]** The Strategy `+0x8` / `+0x10` field *names* (which axis is the phase-0 ring length vs phase-1 replica-group count) are attributed from the StrategyND fusion context; the offsets and the `cmpq $1` predicate are CERTAIN, the names are inferred.
>
> **[LOW]** The literal per-generation `compiler_reserved` integers (§4) — the proto field, the carve formula, and the memfile lookup are CONFIRMED, but the integers are runtime-resolved from embedded binarypb blobs and were not statically extracted. The `BarrierType` numeric `2` (`REPLICA`) and `4` (`MEGACORE`) are recovered from `movl`/`cmp` byte patterns; only `INVALID(0)`, `GLOBAL(1)`, `CUSTOM(3)` appear as named `.rodata` strings.

---

## Cross-References

- [Barriers — Section Map](overview.md) — the `BarrierType` enum, the producer→normaliser→lowering flow, and the subsystem index
- [Barrier Coloring](barrier-coloring.md) — the greedy interference-graph engine feeding the coloring producer's `has_conflict`
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — `CustomKernelEmitter` lowering of a `BarrierConfig` id to a chip SFLAG memref
- [Special-Purpose Sync Flags](special-purpose-sync-flags.md) — the `compiler_reserved` repeated-int32 range + four named scalars (proto source of the §4 blocks)
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the literal per-`(codename, deployment)` SFLAG-range integers (memfile-resolved)
- [Tree-Barrier / vSync](tree-barrier-vsync.md) — the SparseCore per-core tree barrier over the Mosaic user-region window (`SparseCoreTarget+0x90`/`+0x1fc`)
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the SFLAG atomic-counter substrate every barrier is built on
- [back to index](../index.md)
