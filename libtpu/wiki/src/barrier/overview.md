# Barriers and Sync-Flags — Section Map

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset).
> **Status:** Reimplementation-grade map · **Evidence grade:** Confirmed (byte-anchored) — the `BarrierType` enum, the `InferBarrierConfig` normaliser tree, and the three TC reserved-slot SFLAG formulas were re-derived from the IDA decompile; the literal per-generation SFLAG ranges are an embedded-memfile dependency (LOW, see §5) · **Part XIII — On-Pod Collectives & Barriers** / SFLAG & barriers · [back to index](../index.md)

## Abstract

A **barrier** on a TPU is not a CPU memory fence and not an OS futex. It is a point in the program where a set of cores (or chips) must rendezvous, implemented entirely on top of the chip's **sync-flag (SFLAG) atomic counter tier** (see [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md)). Each barrier is bound to one reserved SFLAG number; cores signal it (`tpu.sem_signal` on TensorCore, `sc_tpu.sync_add` on SparseCore) and spin-wait on it (`tpu.sem_wait` / `sync_wait`) until every participant has arrived. There is no kernel involvement and no shared lock object — the entire rendezvous is a counter in the SFLAG MemorySpace plus a pair of MLIR ops wrapped in `scf.for` loops over the peer set.

The compiler chooses *which* barrier a collective gets through a small typed model. Every collective carries, in its HLO `BackendConfig`, a `BarrierConfig` submessage — a `{BarrierType type, int id}` pair — that names a barrier kind and a slot within the per-core SFLAG block. Two passes touch this field. The **producer** is `TensorCoreBarrierAssignment::DetermineBarrierConfigForKey` @`0x109c6fa0`, fed by a greedy graph-coloring engine ([Barrier Coloring](barrier-coloring.md)) that decides which collectives may share a barrier id. The **normaliser** is `InferBarrierConfig` @`0x1376c240`, which runs at pincer-fusion-emit time and *downgrades* a per-key `CUSTOM` barrier to the cheaper `GLOBAL` or `REPLICA` when it can prove the collective's communicating set is genuinely multi-participant ([Infer Barrier Config](infer-barrier-config.md)). The chosen `BarrierConfig` is then read back by `CustomKernelEmitter::Emit` @`0x1321ad60` and lowered to an SFLAG memref + signal/wait ops ([Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md)).

This page is the **map** of that subsystem: the SFLAG-barrier model, the `BarrierType` enum, the producer→normaliser→lowering flow, and the per-generation SFLAG memory map. Each algorithm — the coloring engine, the SFLAG binding, the per-core reserved-slot layout, the SparseCore tree barrier — is a **sibling page** under `barrier/`; this page links them and does not duplicate their byte-level derivations. The cross-host (DCN) barrier is a different subsystem entirely: see [Megascale](../megascale/overview.md).

For reimplementation, the contract is:

- **The SFLAG model:** a barrier is a reserved SFLAG number plus a signal-all-then-wait protocol; there is no lock object. The number comes from a per-core reserved block carved by `Target::Init` / `SparseCoreTarget::Init`.
- **The `BarrierType` enum** (`{INVALID, GLOBAL, REPLICA, CUSTOM, MEGACORE}` = `0..4`) and which value each producer is allowed to write — `MEGACORE(4)` is reserved in the enum but **no `BarrierConfig` producer ever writes it**.
- **The lowering flow:** `BarrierConfig` (proto, in `BackendConfig`) → coloring/`DetermineBarrierConfigForKey` → `InferBarrierConfig` normalisation → `BarrierConfig::id` → a chip SFLAG number, per gen, via the reserved-block formulas.
- **The per-gen SFLAG map:** the TC block reserves 5 named top slots (`count = |compiler_reserved| − 5`); the SC block reserves none. The block geometry is gen-independent; only the literal integers vary per `(codename, deployment-name)` chip-config memfile.

| | |
|---|---|
| **Barrier substrate** | chip SFLAG atomic-counter tier ([SFLAG Sync-Flag Tier](../memory/sflag-protocol.md)) |
| **`BarrierType` enum** | `BARRIER_INVALID=0, GLOBAL=1, REPLICA=2, CUSTOM=3, MEGACORE=4` (MEGACORE never produced) |
| **Config carrier** | `BackendConfig.BarrierConfig` `{type @+0x20, id, hasbits @+0x10}` (per-HLO proto submessage) |
| **Producer (coloring)** | `TensorCoreBarrierAssignment::DetermineBarrierConfigForKey` @`0x109c6fa0` |
| **Normaliser (fusion-emit)** | `InferBarrierConfig` @`0x1376c240` (8 pincer-fusion callers) |
| **SFLAG lowering** | `CustomKernelEmitter::Emit` @`0x1321ad60` → `MaybeInsertGlobalBarrier` @`0x1321ac20` / `RunPasses` @`0x13202780` |
| **TC reserved block** | `Target+0x8c0` base / `Target+0x8c4` count (`= |CR_TC| − 5`); 5 named top slots |
| **SC reserved block** | `SparseCoreTarget+0x1d0` base / `+0x1d4` count (`= |CR_SC|`, no `−5`) |
| **SFLAG-range source** | `TpuChipConfigProto.special_purpose_sync_flags` field 13 → `.compiler_reserved` repeated int32 |

---

## 1. What a barrier is on a TPU

The barrier subsystem sits on top of the on-chip SFLAG tier — a small, MemorySpace-tagged array of atomic counters. A barrier reserves one SFLAG number and runs a **signal-all-then-wait** tree protocol over it; the MLIR primitive on both substrates is `AllocateAtOffsetOp` @`0x145a5aa0` with `MemorySpaceAttr(sflag)` @`0x1458ff20`, which materialises a memref at the reserved SFLAG offset. The two substrates differ only in the atomic-op family they wrap it in:

```text
TensorCore barrier                       SparseCore barrier
  AllocateAtOffsetOp(MemorySpace::sflag)   AllocateAtOffsetOp(MemorySpace::sflag)
  scf.for peer in cores:                   (per-ring)
    tpu.sem_signal  @0x14b442e0              sc_tpu.sync_add
  scf.for peer in cores:                     sc_tpu.sync_wait
    tpu.sem_wait    @0x14b45460
```

Two consequences for a reimplementer:

- **The barrier *is* an SFLAG number.** There is no separate barrier object at runtime — the `BarrierConfig.id` is, after lowering, an index into the reserved per-core SFLAG block (§5). Allocating a barrier means reserving an SFLAG slot; freeing one means it returns to the pool. The whole barrier-assignment problem is therefore an SFLAG-number-allocation problem.
- **The rendezvous is purely cooperative.** Every participating core both signals (bumps the counter) and waits (spins until the counter reaches the participant count). A wrong participant count or a duplicated id deadlocks silently — there is no timeout in the emitted ops. The producer's job (§3) is to guarantee no two concurrently-live collectives are assigned the same id.

> **NOTE —** the SFLAG number space is *shared* across all barrier kinds and across both substrates. The TC global barrier (`tpu.sem_*`), the TC per-key barriers, and the SparseCore per-ring barriers (`sc_tpu.sync_*`) all draw from the same reserved chip-SFLAG block (`Target+0x8c0/+0x8c4` for TC, `SparseCoreTarget+0x1d0/+0x1d4` for SC). The only thing that distinguishes them at the hardware level is the atomic-op family and which core block they index.

---

## 2. The `BarrierType` enum and `BarrierConfig`

Every barrier the compiler emits is tagged with one of five `BarrierType` values. The enum is a proto enum carried in the `BarrierConfig` submessage of each collective's `BackendConfig`; the numeric values are the protobuf field values, recovered from the `movl $N,-0x30` / `cmp $N` byte patterns in the producers and normaliser. The named string `"barrier.barrier_type() != BarrierType::BARRIER_INVALID"` (the RetCheck at `InferBarrierConfig` line 115) anchors the `INVALID` enumerator at 0.

| Value | Enumerator | Meaning | Lowered SFLAG | Confidence |
|---|---|---|---|---|
| 0 | `BARRIER_INVALID` | sentinel / unset; always rejected by both touchpoints | — | CERTAIN |
| 1 | `GLOBAL` | all-cores device-wide barrier; `id = -1` sentinel | `base + count + 4` (`GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`) | CERTAIN |
| 2 | `REPLICA` | within-replica-group tree barrier; shares an id | `base + id` (top reserved per-id slot when set by normaliser, `id = count − 1`) | HIGH |
| 3 | `CUSTOM` | per-key dedicated barrier; coloring assigns a fresh `id` | `base + id` (per-key slot, [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md)) | HIGH |
| 4 | `MEGACORE` | two-tensorcore-per-chip barrier; reserved in the enum | `base + count` (`GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0`, `Megacore()`-gated) | HIGH |

`BarrierConfig` itself is a small proto: the type field sits at byte offset `+0x20` of the runtime message, the presence hasbits at `+0x10` (the normaliser ORs in `0x3` to mark type-and-id present). When a collective has no `barrier_config` submessage, both touchpoints fall back to the default-instance `BarrierConfig_globals_` @`0x223a9450`.

> **QUIRK —** `MEGACORE(4)` exists in the enum and has a live SFLAG accessor (`GetMegacoreBarrierSyncFlagNumber`, gated on `TpuChipConfig::Megacore()`), but **no `BarrierConfig` producer in this build ever writes the value 4**. The coloring producer writes only `{1,2,3}`; the normaliser writes only `{1,2}` (and never away from an already-set GLOBAL/REPLICA). A reimplementation that drives a switch off all five values will have a dead `case 4` for the config-producing side — the megacore slot is consumed by the hardware barrier emission directly, not by a `BarrierConfig`. Confirmed by a full E8-xref scan: no `movl $4` into the type field at any producer.

---

## 3. The lowering flow

One collective's barrier travels through three stages: a **producer** picks a `BarrierType` and `id` and writes it into the HLO `BackendConfig`; a **normaliser** may rewrite it at fusion-emit time; an **emitter** reads it back and lowers it to an SFLAG memref + signal/wait. The two writers are distinct, run at different pipeline stages, and never share state — they communicate only through the `BackendConfig` proto field.

```text
HLO collectives  (all-reduce / all-gather / reduce-scatter / collective-permute / all-to-all)
        │
  [P]  PRODUCER — TensorCoreBarrierAssignment::Run  (HLO barrier-assignment pass)
        │   ├─ BarrierColoring::Run @0x109cf600 / 0x109d1a60   (greedy graph coloring; §3.1)
        │   │     two passes (collective-permute policy + async-barrier policy) →
        │   │     interference graph over live async ranges → first-fit color → conflict set
        │   └─ DetermineBarrierConfigForKey @0x109c6fa0(key, config, has_conflict)
        │         writes BarrierConfig {GLOBAL(1) | CUSTOM(3 fresh) | REPLICA(2 shared)} → BackendConfig
        │
  [N]  NORMALISER — InferBarrierConfig @0x1376c240   (per pincer fusion, 8 callers; §3.2)
        │   reads BackendConfig.BarrierConfig; if multi-participant & CUSTOM(3):
        │     channel_id present?  → GLOBAL(1), id=-1
        │     else                 → REPLICA(2), id = count−1
        │   singleton set → keep; INVALID(0) → RetCheck
        │
  [L]  LOWERING — CustomKernelEmitter::Emit @0x1321ad60   (kernel emit; §3.3)
        │   reads BackendConfig.BarrierConfig back out
        ├─ type 1 (global) → MaybeInsertGlobalBarrier @0x1321ac20
        │                     → AllocateAtOffsetOp(sflag) + scf.for(tpu.sem_signal/sem_wait) tree barrier
        └─ type 2/3 (per-key) → RunPasses @0x13202780
                                → GetSyncFlagForBarrierId → AllocateAtOffsetOp(sflag)
        │
        ▼
   chip SFLAG number (§5)  →  hardware rendezvous
```

### 3.1 Producer — the coloring engine

`DetermineBarrierConfigForKey` does not decide barrier sharing alone; it is fed a `has_conflict` boolean by a greedy graph-coloring engine, `BarrierColoring<Policy>::Run`. The engine groups all TensorCore collectives by `TensorCoreBarrierKey`, builds an **interference graph** in which two same-key async collectives get an edge iff their async-start..async-done live ranges overlap in the call-graph-ordered schedule, then first-fit colors each per-key graph. A collective that takes color 0 may share a barrier (`REPLICA`/`GLOBAL`); a collective forced to a non-zero color lands in the conflict set and gets a fresh `CUSTOM` id. The full algorithm — the two policies (collective-permute-start/done `0x24`/`0x23` vs the async-barrier custom-call), the conflict predicate, and the first-fit search — is in [Barrier Coloring](barrier-coloring.md).

This is the structural difference from SparseCore, which dedups barriers on a static ring-config hash with no notion of schedule overlap. TensorCore dedups on the *live* interference graph, so even two collectives with identical keys are split onto distinct barriers if they are concurrently in flight.

### 3.2 Normaliser — `InferBarrierConfig`

`InferBarrierConfig` @`0x1376c240` is the second, distinct touchpoint. It runs inside the eight RotatedPincer / RotatedPincerShort / AsyncPincer fusion emitters, at emit time, when the fusion's actual communicating-set shape is known. It re-reads the HLO `BackendConfig.BarrierConfig` and *downgrades* a `CUSTOM` barrier to a cheaper kind. The decision tree, verified byte-exact against the decompile (the `cmp`/`movl` sites are annotated):

```c
function InferBarrierConfig(target, hlo, strat):              // 0x1376c240
    cfg = hlo->backend_config<BackendConfig>()                // 0xf58e6c0
    if (!cfg.ok()) return RetCheck(line 0x5d)                 // rotated_pincer_fusion_emitter.cc

    bc = cfg.has_barrier_config()                             // hasbit 0x10 @ msg+0x10
       ? cfg.barrier_config()
       : BarrierConfig_globals_                               // default @0x223a9450

    // PREDICATE: is the communicating set multi-participant on either axis?
    multi = (strat->f8 /*+0x8*/ > 1) || (strat->f10 /*+0x10*/ > 1)   // strat[1]>1 || strat[2]>1

    if (multi):
        if (bc.type == CUSTOM /*3*/):                         // cmp $3
            if (hlo->channel_id().has_value()):               // channel_id @0x1e59ff80, dl==1
                bc.type = GLOBAL /*1*/;  bc.id = -1           // channelled collective → device-global
            else:
                bc.type = REPLICA /*2*/                       // within-replica collective
                bc.id   = target.Target[0x8c4] - 1           // = count − 1, the top usable TC id
                bc.set_has_type_and_id()                      // hasbit |= 3
        else if (bc.type == INVALID /*0*/):
            return RetCheck(line 0x73, "...!= BarrierType::BARRIER_INVALID")
        // else (already GLOBAL/REPLICA) → keep
    else:                                                     // SINGLETON set on both axes
        if (bc.type == INVALID /*0*/): return RetCheck(line 0x73)
        // else keep CUSTOM as-is

    out.barrier_config = bc;  return OK                       // movq $1,(rbx)
```

The semantics: a `CUSTOM` barrier on a genuinely multi-participant collective is collapsed to the cheaper shared barrier — `GLOBAL` (with the sentinel `id = -1`) if the collective carries a `channel_id` (a cross-module / cross-device channelled collective requiring an all-cores barrier), or `REPLICA` (a within-replica-group tree barrier, pinned to the last usable TC id, `count − 1`) if it does not. A `CUSTOM` barrier on a *singleton* set (the collective is degenerate / single-core) is left untouched. `INVALID` is always rejected. The byte-exact body — including the `*((int *)a2 + 561) - 1` (`Target+0x8c4 − 1`) REPLICA id and the absence of any `movl $4` — is in [Infer Barrier Config](infer-barrier-config.md).

> **GOTCHA —** the predicate inputs `Strategy+0x8` / `Strategy+0x10` are the two-axis participant counts of the pincer collective's communicating set (phase-0 / phase-1 ring or replica-group sizes). The offsets are byte-confirmed (`cmpq $1` on each), but the field *names* are attributed from the StrategyND fusion context, not from a struct descriptor — treat the "ring length vs replica-group count" reading as inferred. The *behavior* (`>1 on either axis ⇒ downgrade CUSTOM`) is certain.

### 3.3 Lowering — `CustomKernelEmitter`

At kernel-emit time, `CustomKernelEmitter::Emit` @`0x1321ad60` reads the `BarrierConfig` back out of the `BackendConfig` and hands it to two consecutive lowerers. Type 1 (global) flows into `MaybeInsertGlobalBarrier` @`0x1321ac20`, which walks the func ops and builds the `AllocateAtOffsetOp(sflag)` + `scf.for(tpu.sem_signal / tpu.sem_wait)` tree barrier. Type 2/3 (per-key) flows into `RunPasses` @`0x13202780`, whose nested pass reaches `GetSyncFlagForBarrierId` and the SparseCore `sc_tpu.sync_*` emission — the same `AllocateAtOffsetOp(sflag)` primitive, different atomic op family. The full lowering, the three RetCheck legality gates (`non-communicating custom call` / `skip_device_barrier` / `unsupported core type`), and the global-vs-per-key fork are in [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) and the per-core window in [Global-Barrier Window](global-barrier-window.md).

---

## 4. Barrier kinds (sibling pages)

The barrier kinds the producers can emit, and the lowering machinery, are each a sibling page. This section is the index; each row links the page that derives it byte-by-byte.

| Kind / facet | What it is | Page |
|---|---|---|
| **Global barrier** | `GLOBAL(1)`; all cores rendezvous on `base+count+4`; `id=-1` sentinel; the `tpu.sem_signal`-all-then-`tpu.sem_wait` tree | [Global-Barrier Window](global-barrier-window.md), [Tree-Barrier / vSync](tree-barrier-vsync.md) |
| **Replica barrier** | `REPLICA(2)`; within-replica-group tree barrier; shared id (`count−1` when set by the normaliser) | [Replica Barrier](replica-barrier.md) |
| **TensorCore barrier** | the TC-substrate signal/wait barrier and its coloring-chosen `CUSTOM(3)` ids | [TensorCore Barrier](tensorcore-barrier.md) |
| **Megacore barrier** | `MEGACORE(4)`; two-TensorCore-per-chip barrier on `base+count`; `Megacore()`-gated; not config-produced | (accessor only; see §5) |
| **Tree barrier / vSync** | the SparseCore per-core tree barrier over the Mosaic user-region window | [Tree-Barrier / vSync](tree-barrier-vsync.md) |
| **Barrier coloring** | the greedy interference-graph engine that decides barrier sharing (the producer's `has_conflict` input) | [Barrier Coloring](barrier-coloring.md) |
| **Infer barrier config** | the pincer-fusion `CUSTOM → GLOBAL/REPLICA` normaliser (§3.2) | [Infer Barrier Config](infer-barrier-config.md) |
| **Barrier → SFLAG binding** | `CustomKernelEmitter` lowering of `BarrierConfig` to a chip SFLAG memref (§3.3) | [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) |
| **Special-purpose sync flags** | the `compiler_reserved` range + four named scalars that source the per-gen blocks (§5) | [Special-Purpose Sync Flags](special-purpose-sync-flags.md) |
| **Remote SFLAG encoders** | the cross-chip SFLAG addressing used by ICI barriers | [Remote SFLAG Encoders](remote-sflag-encoders.md) |
| **Per-codename `compiler_reserved`** | the literal per-`(codename, deployment)` SFLAG-range integers (memfile-resolved) | [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) |

The cross-host Megascale (DCN) barrier is **not** part of this subsystem — no on-chip barrier pass calls into it; it is a separate orchestration layer ([Megascale](../megascale/overview.md)). The collectives that *consume* these barriers are documented in [Collectives](../collectives/overview.md).

---

## 5. The per-generation SFLAG memory map

The SFLAG numbers a barrier can take are not hard-coded; they come from a per-core-type **reserved range** in the chip config, carved into a base/count block at `Target::Init`. The full proto→runtime→block chain is below; the literal integers per generation are an embedded-memfile dependency (LOW confidence, see the GOTCHA), but the *block geometry* is gen-independent and byte-confirmed.

### 5.1 The source: `compiler_reserved`

The per-core-type SFLAG range is the `compiler_reserved` repeated-int32 field of a `SpecialPurposeSyncFlags` proto message, one per `TpuCoreType`, carried in `TpuChipConfigProto.special_purpose_sync_flags` (field 13). At runtime these land in an `EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>` at `TpuChipConfig+0x2a0` (stride `0x40`, presence bitmask at `+0x360`), accessed by `GetSpecialPurposeSyncFlags(core)` @`0x20afcf40`:

```c
function GetSpecialPurposeSyncFlags(chip_config, core):       // 0x20afcf40
    mask = *(chip_config + 0x360)                             // per-core-type presence bitmask
    if (!(mask >> core & 1)) return NULL                      // bt core,mask; jae → 0
    if (core >= 3) ud1                                        // CHECK core ∈ {0,1,2}
    return chip_config + 0x2a0 + (core << 6)                  // element `core`, 0x40-byte stride
```

> **CORRECTION (P-3-424) —** an earlier reading had the accessor return `+0x2a0 + core` (a byte index). The decompile shows `shl $6` — the index is `core << 6` = `core * 0x40` (element stride of the `EnumMap`). A reimplementation using `+core` reads garbage for `kSparseCore`/`kBarnaCore`. The TensorCore entry (`core=0`) is **mandatory**: `Target::Init` dereferences the result and dies via `DieBecauseNull` (`"chip_config.GetSpecialPurposeSyncFlags(::tpu::TpuCoreType::kTensorCore)"`) if absent.

The `SpecialPurposeSyncFlags` message also carries four named scalar SFLAG numbers — `sequencer_overlay` (f4), `tile_overlay` (f5), `global_barrier_sflag` (f6), `local_barrier_sflag` (f7) — at proto offsets `+0x30..+0x3c. Whether these survive into the runtime element (vs being consumed in `FromProto`) was not separately traced; `Target::Init` reads only the `compiler_reserved` vector. See [Special-Purpose Sync Flags](special-purpose-sync-flags.md).

### 5.2 The carve: `Target::Init` / `SparseCoreTarget::Init`

`Target::Init` @`0x1d60fc20` copies `compiler_reserved(TensorCore)`, CHECKs it is a contiguous ascending int range, and writes `base = arr[0]` → `Target+0x8c0`, `count = size − 5` → `Target+0x8c4`. The top 5 of the range are reserved for the named barrier slots (§5.3). `SparseCoreTarget::Init` @`0x1d612b20` does the same for `compiler_reserved(SparseCore)` into `SparseCoreTarget+0x1d0` / `+0x1d4`, but **without the `−5`** — the SC block is full and reserves its global-barrier id *within* `[SC_base, SC_base+SC_count)`. TC and SC ranges are disjoint by construction (different `SpecialPurposeSyncFlags` message per core type).

### 5.3 The TC reserved-slot map (the `−5`)

The top 5 slots of the TC range are the named cross-core barrier sync flags. All three accessor formulas were re-derived byte-exact from the decompile (`this[560]` = `Target+0x8c0` = base, `this[561]` = `Target+0x8c4` = count):

| Slot | Accessor | Formula | Confidence |
|---|---|---|---|
| `base+count+0` | `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0` | `base + count` (`Megacore()`-gated; CHECK `"…chip_config().Megacore()"`) | CERTAIN |
| `base+count+1` | (gap; `GetAllReduceSyncFlagNumber(0)` is illegal — CHECK `phase > 0`) | `base+count+1` | CERTAIN |
| `base+count+2` | `GetAllReduceSyncFlagNumber(1)` @`0x1d60f440` | `base + 1 + count + 1` (pincer InitSyncFlags) | CERTAIN |
| `base+count+3` | `GetAllReduceSyncFlagNumber(2)` @`0x1d60f440` | `base + 2 + count + 1` (pincer InitSyncFlags) | CERTAIN |
| `base+count+4` | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` | `base + count + 4` (global / tree barriers) | CERTAIN |

`GetAllReduceSyncFlagNumber(phase)` is `LogMessageFatal`-bounded to `0 < phase < 3` (lines 143/144), so the `+1` slot is a permanent gap. The usable per-id window `[base, base+count)` (REPLICA/CUSTOM ids, `id < count`) sits *below* these 5.

### 5.4 The per-gen table (parametric)

Let `CR_TC = compiler_reserved(TensorCore)` and `CR_SC = compiler_reserved(SparseCore)` for a given `(codename, deployment-name)` chip config. The structure is **identical across every generation** — only the integers differ:

| Gen (codename) | TC block (`Target+0x8c0`/`+0x8c4`) | SC block (`SCTgt+0x1d0`/`+0x1d4`) | TC top-5 reserved (within block) |
|---|---|---|---|
| JF (`kJellyfish`, v2) | `base=CR_TC[0]`, `count=|CR_TC|−5` | `base=CR_SC[0]`, `count=|CR_SC|` (no `−5`) | mega `b+c`, gap `b+c+1`, ar1 `b+c+2`, ar2 `b+c+3`, glob `b+c+4` |
| DF (`kDragonfish`, v3) | same | same | same 5-slot map |
| PF (`kPufferfish`, v4) | same | same | same 5-slot map |
| VF (`kViperfish`, v5p) | same | same | same 5-slot map |
| GL (`kGhostlite`, v6e) | same | same | same 5-slot map |
| GF (`k6acc60406`, v7) | same | same | same 5-slot map |

The `−5` is a compile-time constant in `Target::Init` (`add $0xfffffffb`), gen-independent: every generation reserves exactly the 5 named top slots. Megacore deployments (`megacore*`, `megachip`) are the ones for which `CoresPerChip(TensorCore) == 2` → `BarrierMegacore` is active and the `base+count` megacore slot is consumed; other deployments leave it reserved-unused.

> **GOTCHA —** the literal per-`(codename, deployment-name)` integers (`CR_TC[0]`, `|CR_TC|`, `CR_SC[0]`, `|CR_SC|`) are **not statically extractable** from `.rodata`. They live in embedded chip-config memfile binarypb blobs (`tpu_chip_config_memfile_{default,megacore,megachip,…}_embed_internal_create` @`0x20b18fa0..`), resolved at runtime via a `flat_hash_map<tuple<TpuVersion, name, TpuCoreType>, FileToc*>` keyed by `FLAGS_deepsea_chip_config_name` @`0x224714b0`. The block geometry above is CONFIRMED; the integers are LOW (memfile-dependency). See [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md).

> **CORRECTION (P-3-409) —** `SparseCoreTarget+0x90` is **not** an SFLAG-window base — it is `TpuCoreParts::SequencerCount(TpuSequencerType=5)`, a per-core sequencer count. `+0x1fc` is the `GetMemoryReservation → GetUserRegion` length (the Mosaic per-core tree-barrier window, jellyfish `MemorySpace::kSparseCoreSequencerSmem` = 14), a third disjoint region not drawn from the SFLAG vector. Neither is part of the `compiler_reserved` block.

---

## 6. Two producers, one config field

It is worth stating plainly that the `BarrierConfig` field has **two** writers that a reimplementer must keep distinct:

| Producer | When it runs | Writes | id source | Confidence |
|---|---|---|---|---|
| `DetermineBarrierConfigForKey` @`0x109c6fa0` | HLO barrier-assignment pass (per key) | `GLOBAL(1)` / `CUSTOM(3 fresh)` / `REPLICA(2 shared)` | `-1` (GLOBAL) / fresh / shared key id | HIGH |
| `InferBarrierConfig` @`0x1376c240` | pincer fusion emit (per fusion, 8 callers) | `CUSTOM→GLOBAL(1, id=-1)` if channelled; `CUSTOM→REPLICA(2, id=count−1)` if not | `-1` (GLOBAL) / `count−1` (REPLICA) | CERTAIN |

`DetermineBarrierConfigForKey` is the authoritative coloring producer at HLO-pass time. `InferBarrierConfig` is a per-fusion *normaliser*: it only downgrades a `CUSTOM` choice to `GLOBAL`/`REPLICA` when the pincer fusion's actual participant set is known, and never upgrades or rewrites an already-set `GLOBAL`/`REPLICA`. Neither writes `MEGACORE(4)`. Both results feed the same lowering (§3.3) → the same SFLAG number space (§5).

---

## 7. Verification notes

> **[CONFIRMED]** The following were re-derived from the IDA decompile of `libtpu.so` v0.0.40 for this page:
> - `InferBarrierConfig` @`0x1376c240`: the singleton predicate `*((__int64*)a4 + 1) <= 1 && *((__int64*)a4 + 2) <= 1` (Strategy+0x8/+0x10); `if (v35 == 3)` (CUSTOM); `channel_id(a3)` then `v15 == 1` → `v35 = 1` (GLOBAL), `v16 = -1`; else `v35 = 2` (REPLICA), `v16 = *((int*)a2 + 561) - 1` (Target+0x8c4 − 1); RetCheck line 115 `"barrier.barrier_type() != BarrierType::BARRIER_INVALID"`; **no `movl $4`** anywhere — exact.
> - `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`: `this[561] + this[560] + 4` = `base + count + 4` — exact.
> - `GetAllReduceSyncFlagNumber` @`0x1d60f440`: CHECK `phase > 0` / `phase < 3`; `this[560] + phase + this[561] + 1` = `base + count + phase + 1` — exact.
> - `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0`: `Megacore()`-gated (`"topology_->chip_config().Megacore()"`), `this[560] + this[561]` = `base + count` — exact.
> - `GetSpecialPurposeSyncFlags` @`0x20afcf40`: `bt core, *(chip+0x360)` gate; `core >= 3` → `ud1`; `return chip + 0x2a0 + (core << 6)` — exact (confirms the `core<<6` correction, not `+core`).
> - Symbol confirmation: `DetermineBarrierConfigForKey(...HloModuleConfig, bool)` @`0x109c6fa0` and both `BarrierColoring<…>::Run` policies present in the decompile.
>
> **[LOW]** The literal per-generation `compiler_reserved` integers (§5.4) — the proto field, the carve formula, and the memfile lookup are CONFIRMED, but the integers are runtime-resolved from embedded binarypb blobs and were not statically extracted. The `BarrierType` numeric values `2`/`4` (`REPLICA`/`MEGACORE`) are recovered from `movl`/`cmp` byte patterns and proto-value arithmetic; only `INVALID(0)`, `GLOBAL(1)`, `CUSTOM(3)` appear as named `.rodata` strings.

---

## Cross-References

**Barrier algorithms (this section)**
- [Barrier Coloring](barrier-coloring.md) — the greedy interference-graph engine feeding `DetermineBarrierConfigForKey`'s `has_conflict`
- [Infer Barrier Config](infer-barrier-config.md) — the pincer-fusion `CUSTOM → GLOBAL/REPLICA` normaliser (full byte-exact tree)
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — `CustomKernelEmitter` lowering of `BarrierConfig` to a chip SFLAG memref
- [Global-Barrier Window](global-barrier-window.md) — the `base+count+4` global slot and the per-core barrier window
- [Replica Barrier](replica-barrier.md) — the within-replica-group tree barrier (`REPLICA(2)`)
- [TensorCore Barrier](tensorcore-barrier.md) — the TC-substrate signal/wait barrier and coloring-chosen `CUSTOM` ids
- [Tree-Barrier / vSync](tree-barrier-vsync.md) — the SparseCore per-core tree barrier over the Mosaic user-region window
- [Special-Purpose Sync Flags](special-purpose-sync-flags.md) — the `compiler_reserved` range + four named scalars (proto source of the blocks)
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the literal per-`(codename, deployment)` SFLAG-range integers
- [Remote SFLAG Encoders](remote-sflag-encoders.md) — cross-chip SFLAG addressing for ICI barriers

**Sibling subsystems**
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the SFLAG atomic-counter substrate every barrier is built on
- [Collectives](../collectives/overview.md) — the collective ops that consume these barriers (producer runs in their pipeline)
- [Megascale](../megascale/overview.md) — the cross-host (DCN) barrier; a separate orchestration layer, not on-chip SFLAG
- [back to index](../index.md)
