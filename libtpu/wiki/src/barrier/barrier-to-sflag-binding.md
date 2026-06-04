# Barrier → SFLAG Number Binding

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). The binary ships with full C++ symbols; `.text` VMA == file offset. Other versions will differ.*

## Abstract

Every TensorCore barrier the compiler emits ends as a single integer: a **chip SFLAG number**, the index of one atomic counter in the on-chip sync-flag tier that participating cores signal and spin-wait on ([SFLAG Sync-Flag Tier](../memory/sflag-protocol.md)). This page owns the **arithmetic that turns a `BarrierType` into that number** for the three named cross-core slots — global, all-reduce-phase, and megacore — and the **base/count window** they are all measured against. It is the bottom of the barrier datapath: the [coloring engine](barrier-coloring.md) and the [`InferBarrierConfig` normaliser](infer-barrier-config.md) decide *which* `BarrierType` and `id` a collective carries; this page is where those choices become hardware addresses.

The numbers are not stored in a table. `Target::Init` reads a per-core-type **`compiler_reserved`** integer range out of the chip-config proto, CHECKs it is a contiguous ascending block, and stashes two `int32` scalars on the `Target` object: `base = CR_TC[0]` at `Target+0x8c0` and `count = |CR_TC| − 5` at `Target+0x8c4`. The `−5` is the whole trick — it carves the top five SFLAG numbers of the range off the usable per-id window `[base, base+count)` and reserves them for the named cross-core barriers. Three tiny `const` accessors then compute each named slot as a fixed offset above `base+count`, so the named barriers live in `[base+count, base+count+5)` and the per-key/replica barriers live below them. There is no struct field per named slot; the offset *is* the binding.

A reimplementer must reproduce four things, in this order: the **window read** (`GetSpecialPurposeSyncFlags(kTensorCore)` → contiguity CHECK → `base`/`count = size − 5`); the three **accessor formulas** (`base+count` / `base+count+phase+1` / `base+count+4`); the two **CHECK gates** that bound them (`0 < phase < 3` on all-reduce, `Megacore()` on megacore); and the fact that the SparseCore side uses a *different* counter (`GetSparseCoreBarrierSyncFlagCount` reads `SparseCoreTarget+0x1d4`, with **no `−5`**), so the two ranges never alias. The per-color `CUSTOM` and per-ring SparseCore barriers that index *into* `[base, base+count)` are owned elsewhere — see [Barrier Coloring](barrier-coloring.md) and [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md); this page documents the five reserved slots above the window and the window read itself.

| | |
|---|---|
| **Window object** | `xla::jellyfish::Target` — `base` @`Target+0x8c0` (`this[560]`), `count` @`Target+0x8c4` (`this[561]`) |
| **Window source** | `TpuChipConfigProto.special_purpose_sync_flags(kTensorCore).compiler_reserved` (repeated int32) |
| **Window carve** | `Target::Init` @`0x1d60fc20` (line 2067–2068: `base = CR_TC[0]`, `count = \|CR_TC\| − 5`) |
| **Usable per-id window** | `[base, base+count)` — REPLICA / CUSTOM ids, `0 ≤ id < count` |
| **Global slot** | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` → `base + count + 4` |
| **All-reduce slot** | `GetAllReduceSyncFlagNumber(phase)` @`0x1d60f440` → `base + count + phase + 1` (`0 < phase < 3`) |
| **Megacore slot** | `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0` → `base + count` (`Megacore()`-gated) |
| **SC counterpart** | `GetSparseCoreBarrierSyncFlagCount` @`0x10972fa0` → `SparseCoreTarget+0x1d4` (no `−5`) |

---

## 1. The window: `base` and `count`

### Purpose

The named barrier numbers are all relative offsets above one anchor, `base + count`, so the entire binding reduces to knowing those two `int32`s. They are read once, at target construction, from the chip-config proto's per-core-type reserved SFLAG range, and never recomputed. A reimplementer who gets the window read right gets every accessor for free.

### The fields

The two scalars live adjacent on the `Target` object. The decompiler addresses them as `_DWORD` indices off the `Target` base pointer; the byte offsets are the index times four:

| Name | Decompiler form | Byte offset | Meaning |
|---|---|---|---|
| `base` | `*((_DWORD *)this + 560)` | `Target+0x8c0` | first SFLAG number of the TC reserved range = `CR_TC[0]` |
| `count` | `*((_DWORD *)this + 561)` | `Target+0x8c4` | usable per-id slot count = `\|CR_TC\| − 5` |

> **NOTE —** `count` is **not** the size of the reserved range. It is the size *minus the five named top slots*. The reserved range has `|CR_TC|` integers; `count = |CR_TC| − 5` of them are the per-id window, and the remaining five are the named cross-core barriers this page computes. Confusing `count` with `|CR_TC|` over-counts the usable ids by exactly five and collides the per-key barriers with the global slot.

### The read — `Target::Init`

`Target::Init` @`0x1d60fc20` performs the carve near the end of construction (lines 1969–2069). The sequence is a null-checked proto accessor, a contiguity assertion, and two scalar stores:

```c
function Target_Init_carve_sflag_window(target, chip_config):   // 0x1d60fc20, lines 1969-2069
    spsf = chip_config.GetSpecialPurposeSyncFlags(kTensorCore)   // 0x20afcf40
    if (spsf == NULL):                                           // line 1971
        DieBecauseNull("chip_config.GetSpecialPurposeSyncFlags("
                       "::tpu::TpuCoreType::kTensorCore)")        //   TC entry is mandatory

    size = spsf->compiler_reserved.size()   // *(spsf + 16)       // v284, the repeated-int32 length
    data = spsf->compiler_reserved.data()   // *(spsf + 8)        // v285 / v286 (copied)

    // CHECK the range is a contiguous ascending int block: arr[i] == arr[i-1] + 1
    for i in 1 .. size-1:                                        // unrolled x8, lines 1989-2065
        CHECK(data[i] == data[i-1] + 1,                          // RetCheck line 1153
              "compiler_reserved_tensor_core_sync_flags[i] =="
              " compiler_reserved_tensor_core_sync_flags[i - 1] + 1")

    target[0x8c0] = data[0]      // base  = CR_TC[0]   *((_DWORD*)target + 560), line 2067
    target[0x8c4] = size - 5     // count = |CR_TC|-5  *((_DWORD*)target + 561), line 2068
    free(data)
```

Two facts a reimplementer must carry from this:

- **The range must be contiguous and ascending by 1.** The CHECK at line 1153 means the reserved SFLAG numbers are not an arbitrary set — they are an interval `[CR_TC[0], CR_TC[0] + |CR_TC|)`. Because the range is contiguous, `base` plus an integer index *is* the SFLAG number; no per-id lookup table is needed. This is why all the accessors are pure arithmetic.
- **The TensorCore entry is mandatory.** `GetSpecialPurposeSyncFlags(kTensorCore)` returning null is fatal (`DieBecauseNull`, line 1971), so on any supported TC target `base`/`count` are always populated before any barrier is lowered. The accessors never have to defend against an unset window.

> **GOTCHA —** the `−5` is a compile-time literal in `Target::Init` (`add $0xfffffffb` at the byte level, the two's-complement of `−5`), **not** a per-generation value. Every generation reserves exactly five named top slots. A reimplementation that reads the reserved-slot count from the chip config will be wrong; the five is hard-coded and gen-independent. The literal `CR_TC[0]` and `|CR_TC|` integers, by contrast, *are* per-`(codename, deployment)` and are not statically extractable — see [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md).

---

## 2. `GetMegacoreBarrierSyncFlagNumber` — `base + count`

### Purpose

The megacore barrier is the lowest of the five reserved slots, sitting exactly at the top of the usable per-id window: `base + count`. It rendezvouses the two TensorCores that share a chip in a megacore deployment (`CoresPerChip(TensorCore) == 2`). The accessor is gated on the chip actually being in megacore mode, so on a single-TC chip the slot is reserved-but-unused and the accessor traps rather than returning a meaningless number.

### Algorithm

```c
function GetMegacoreBarrierSyncFlagNumber(target):              // 0x1d60f4e0
    // target[119] is topology_; +24 -> chip_config
    if (!topology_->chip_config().Megacore()):                  // line 154
        LogFatal("topology_->chip_config().Megacore()")         //   trap: not a megacore chip
    return (uint32)(target[560] + target[561])                  // base + count
```

The gate is `tpu::TpuChipConfig::Megacore()` reached through `*(*((_QWORD*)this + 119) + 24)` — the `topology_` pointer at `Target+0x3b8` (index 119), then its chip-config at `+0x18`. On a non-megacore chip the call is a `LogMessageFatal` at `target.cc:154`; it never returns a wrong number.

> **QUIRK —** this is the one named slot whose *value* coincides with the boundary of the per-id window (`base + count`). It does not overlap the usable ids — `id < count` means the last usable id is `base + count − 1`, one below the megacore slot — but it is the tightest of the five. The four-slot gap above it (`+1` through `+4`) is the all-reduce/global block (§3, §4).

---

## 3. `GetAllReduceSyncFlagNumber(phase)` — `base + count + phase + 1`

### Purpose

The all-reduce barrier is per-*phase*: the pincer all-reduce runs in two phases and each needs its own rendezvous slot, so the accessor takes a `phase` argument and returns a distinct SFLAG number per phase. Valid phases are `1` and `2`; `phase 0` and `phase ≥ 3` are illegal and trap. The two legal phases map to slots `base+count+2` and `base+count+3`, leaving `base+count+1` as a permanent gap (no `phase` produces it).

### Algorithm

```c
function GetAllReduceSyncFlagNumber(target, phase):             // 0x1d60f440
    if (phase <= 0):                                            // line 143
        LogFatal("phase > 0")                                   //   phase 0 is illegal
    if ((uint64)phase >= 3):                                    // line 144
        LogFatal("phase < 3")                                   //   phase 3+ is illegal
    return (uint32)(target[560] + phase + target[561] + 1)      // base + phase + count + 1
```

The bound is a hard `0 < phase < 3` enforced by two `LogMessageFatal` CHECKs (`target.cc:143` `"phase > 0"`, `target.cc:144` `"phase < 3"`). So the function only ever returns two values:

| `phase` | Formula | SFLAG number | Status |
|---|---|---|---|
| `0` | — | — | illegal (CHECK `phase > 0`, line 143) |
| `1` | `base + count + 1 + 1` | `base + count + 2` | valid (pincer phase-0 barrier) |
| `2` | `base + count + 2 + 1` | `base + count + 3` | valid (pincer phase-1 barrier) |
| `≥3` | — | — | illegal (CHECK `phase < 3`, line 144) |

> **GOTCHA —** the slot `base + count + 1` is **never** produced by this accessor — there is no `phase` value that yields it, because `phase 0` is rejected and `phase 1` already maps to `base+count+2`. It is a structural gap in the reserved block, not a usable barrier. A reimplementation that allocates five contiguous named slots starting at `base+count` and assigns the second one to all-reduce phase-1 will collide with nothing in *this* build, but it will not match the original layout: the original leaves `+1` empty and starts all-reduce at `+2`.

---

## 4. `GetGlobalBarrierSyncFlagNumber` — `base + count + 4`

### Purpose

The global barrier is the top reserved slot, `base + count + 4` — the device-wide all-cores rendezvous. It is the `BarrierType::GLOBAL(1)` slot referenced by the normaliser, whose channelled-collective downgrade pins `id = −1` (a sentinel, not an index) precisely because the global barrier does not index the per-id window; it has its own fixed slot at the very top of the reserved range.

### Algorithm

```c
function GetGlobalBarrierSyncFlagNumber(target):               // 0x1d60f420
    return (uint32)(target[561] + target[560] + 4)             // count + base + 4  ==  base + count + 4
```

This is the simplest of the three — no gate, no argument. The decompiler emits the addends in `count + base + 4` order (`this[561] + this[560] + 4`), which is identical to `base + count + 4` by commutativity. There is no CHECK because the global barrier is always legal on any TC target.

> **NOTE —** there are two distinct "global" mechanisms in this subsystem and only one of them uses this accessor. `GetGlobalBarrierSyncFlagNumber` is the **reserved fixed slot** for runtime / ICI-style all-cores barriers. The **Mosaic func-level TC tree barrier** inserted by `MaybeInsertGlobalBarrier` does *not* call this accessor — it builds a per-core SFLAG window from a different `SparseCoreTarget` field entirely ([Global-Barrier Window](global-barrier-window.md)). Do not assume every `GLOBAL(1)` barrier resolves to `base+count+4`; only the reserved-slot path does.

---

## 5. The reserved five-slot map

Putting the four accessors together, the top of the TC `compiler_reserved` range is laid out as a fixed five-slot block above the usable per-id window. The window `[base, base+count)` holds the `REPLICA(2)` / `CUSTOM(3)` ids ([Infer Barrier Config](infer-barrier-config.md)); the five slots above it are the named cross-core barriers:

```text
SFLAG number space (TC compiler_reserved range = [base, base+|CR_TC|) ):

  base ─────────────────────────────────────────────────────────────────► higher
  │                                                                    │
  │   usable per-id window  [base, base+count)                         │  reserved 5 slots
  │   REPLICA / CUSTOM ids  (id < count)                               │  (the −5)
  ├────────────────────────────────────────────────────┬──────────────┴──────────────────────┐
  │ base+0   base+1   ...   base+count-1                │ +count  +count+1  +count+2  +count+3  +count+4
  │                                                     │ MEGA    (gap)     AR(1)     AR(2)     GLOBAL
  └─────────────────────────────────────────────────────────────────────────────────────────┘
                                                          ▲         ▲        ▲         ▲        ▲
                                       GetMegacoreBarrier ─┘         │  GetAllReduce(1) GetAllReduce(2)  GetGlobalBarrier
                                                            permanent gap (no phase maps here)
```

| Slot | Accessor | Formula | Gate / bound |
|---|---|---|---|
| `base + count + 0` | `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0` | `base + count` | `chip_config().Megacore()` (line 154) |
| `base + count + 1` | — (permanent gap) | `base + count + 1` | not producible by any accessor |
| `base + count + 2` | `GetAllReduceSyncFlagNumber(1)` @`0x1d60f440` | `base + count + phase + 1`, `phase=1` | `0 < phase < 3` (lines 143/144) |
| `base + count + 3` | `GetAllReduceSyncFlagNumber(2)` @`0x1d60f440` | `base + count + phase + 1`, `phase=2` | `0 < phase < 3` (lines 143/144) |
| `base + count + 4` | `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420` | `base + count + 4` | (none) |

The `−5` in `Target::Init` and these five slots are two views of one fact: the carve reserves five integers off the top of the range, and the accessors index back into exactly those five. The gap at `+1` means only four of the five reserved slots are ever materialised by an accessor — the fifth integer is a deliberate spacer.

> **QUIRK —** the named barriers are addressed by *offset arithmetic*, never by a struct field. There is no `Target::megacore_barrier_sflag_` member; the megacore number is recomputed as `base+count` every call. A reimplementation that caches the five numbers in fields is functionally equivalent but diverges from the binary, which stores only `base` and `count` and derives the rest. Keep the two scalars authoritative.

---

## 6. The SparseCore counterpart — a disjoint range, no `−5`

The TensorCore window is not the only reserved SFLAG block. `SparseCoreTarget::Init` performs the analogous carve for `compiler_reserved(kSparseCore)` into `SparseCoreTarget+0x1d0` (base) / `+0x1d4` (count) — but **without subtracting five**. The SC range is fully usable as per-ring barrier ids; its global barrier is reserved *within* the range, not above it. The accessor that exposes the SC count confirms the offset:

```c
function GetSparseCoreBarrierSyncFlagCount(target):            // 0x10972fa0
    if (!target->SupportsSparseCore()):                         // vtable +0x260 (608/8)
        LogFatal("SupportsSparseCore()")                        //   target.h:3027
    return *(uint32)(target[297] + 0x1d4)                       // SparseCoreTarget+0x1d4 = SC count
```

`target[297]` is the `SparseCoreTarget` pointer at `Target+0x948`; `+0x1d4` is the SC count, mirroring the TC `+0x8c4`. Two consequences:

- **The TC and SC ranges never alias.** They come from different `SpecialPurposeSyncFlags` proto messages (one per `TpuCoreType`), so `[TC_base, TC_base+|CR_TC|)` and `[SC_base, SC_base+|CR_SC|)` are disjoint by construction. A SparseCore barrier id and a TensorCore barrier id with the same numeric value are *different* SFLAG counters.
- **The SC side has no five-slot reservation.** `count_SC = |CR_SC|` (full range); the SC global barrier is a reserved id inside the window, reached through `GetSyncFlagForBarrierId` (the per-id arithmetic owned by the SparseCore barrier pages), not through any `base+count+k` accessor. The five named slots above the window are a TensorCore-only construct.

> **GOTCHA —** `SparseCoreTarget+0x90` is **not** an SFLAG-window base. That field is `TpuCoreParts::SequencerCount(kSparseCore sequencer)`, a per-core sequencer count; the tree-barrier window length comes instead from `+0x1fc` (`GetUserRegion`, jellyfish `MemorySpace::kSparseCoreSequencerSmem` = 14) — a third region disjoint from the `compiler_reserved` block. Neither is part of the `[base, base+count)` window this page documents. The TC tree barrier ([Global-Barrier Window](global-barrier-window.md)) is the only "global" path that does **not** use `GetGlobalBarrierSyncFlagNumber`.

---

## 7. Verification notes

> Byte-exact in `libtpu.so` v0.0.40:
> - `GetGlobalBarrierSyncFlagNumber` @`0x1d60f420`: `return (uint32)(this[561] + this[560] + 4)` = `base + count + 4` — exact, no gate.
> - `GetAllReduceSyncFlagNumber` @`0x1d60f440`: CHECK `phase > 0` (`target.cc:143`) and `phase < 3` (`target.cc:144`); `return (uint32)(this[560] + phase + this[561] + 1)` = `base + phase + count + 1`; legal phases `{1,2}` → slots `{base+count+2, base+count+3}` — exact.
> - `GetMegacoreBarrierSyncFlagNumber` @`0x1d60f4e0`: gate `tpu::TpuChipConfig::Megacore(*(*(this+119)+24))` (`target.cc:154`, `"topology_->chip_config().Megacore()"`); `return (uint32)(this[560] + this[561])` = `base + count` — exact.
> - `Target::Init` @`0x1d60fc20` window carve (lines 1969–2069): `GetSpecialPurposeSyncFlags(kTensorCore)` + `DieBecauseNull` (line 1971); contiguity CHECK `"compiler_reserved_tensor_core_sync_flags[i] == compiler_reserved_tensor_core_sync_flags[i - 1] + 1"` (`target.cc:1153`); `*((_DWORD*)target + 560) = data[0]` (base, line 2067); `*((_DWORD*)target + 561) = size - 5` (count, line 2068) — exact.
> - `GetSparseCoreBarrierSyncFlagCount` @`0x10972fa0`: `SupportsSparseCore()` gate (vtable `+0x260`, `target.h:3027`); `return *(uint32)(*(this+297) + 0x1d4)` = `SparseCoreTarget+0x1d4` — exact; confirms the SC range has **no** `−5`.
>
> **[LOW]** The literal per-generation values of `CR_TC[0]` (`base`) and `|CR_TC|` (hence `count`) are **not statically extractable** — they live in embedded chip-config `binarypb` memfile blobs resolved at runtime per `(codename, deployment-name)`. The window *geometry* (the carve, the `−5`, the five-slot map) is CONFIRMED gen-independent; the integers are a memfile dependency. See [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md).

---

## Cross-References

- [Barriers and Sync-Flags — Section Map](overview.md) — the subsystem map; §5.3 summarises these accessors, this page derives them
- [Infer Barrier Config](infer-barrier-config.md) — the normaliser that pins `REPLICA` to `id = count − 1` (the top usable id, one below the megacore slot) and `GLOBAL` to `id = −1`
- [Barrier Coloring](barrier-coloring.md) — the engine that assigns the per-key `CUSTOM(3)` ids that index *into* `[base, base+count)`
- [Global-Barrier Window](global-barrier-window.md) — the Mosaic func-level TC tree barrier; the one "global" path that does **not** use `GetGlobalBarrierSyncFlagNumber`
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the literal per-`(codename, deployment)` `compiler_reserved` integers that fill `base`/`count`
- [Special-Purpose Sync Flags](special-purpose-sync-flags.md) — the `compiler_reserved` proto field and the four named scalar SFLAG numbers the window is read from
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the on-chip atomic-counter substrate every SFLAG number indexes
- [back to index](../index.md)
