# Megacore Collective Fusion

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VMA == file offset, build `libtpu_lts_20260413_b_RC00`). Other versions will differ; treat every VA as version-pinned.*

## Abstract

A *megacore* chip carries **two TensorCores per logical device** that the compiler folds into a single XLA device. A collective issued by that logical device must therefore be split across the two physical cores (the **even/odd** pair) and then re-synchronized so the two halves agree before the result leaves the chip. This page owns the three pieces of that fold a reimplementer cannot recover from the op surface or from the per-axis ring schedule:

- **The `MEGACORE(4)` marker is never materialized.** `BarrierType` has five arms but only `GLOBAL(1)` / `REPLICA(2)` / `CUSTOM(3)` are ever written into a `BarrierConfig`; `BARRIER_INVALID(0)` is the all-zeros default. **`MEGACORE(4)` is never produced** — no compiler pass writes `barrier_type == 4`, and the type-4 arm of `net_util::GetBarrierSyncFlag` (`0x1c69ad00`) is structurally dead. The actual TensorCore megacore barrier is a **runtime** construct — `LloRegionBuilder::BarrierMegacore` (`0x1d5222a0`) — gated by `TpuChipConfig::Megacore()`, completely outside the `BarrierConfig → GetBarrierSyncFlag` path.
- **The two-core fold.** `BarrierMegacore` and its program-descriptor twin `net_util::SynchronizeProgramDescriptorStatesMegacore` (`0x1c697540`) each build a `Fingerprint2011`-derived barrier id, then run a `CoreIndex → SmodU32(core_count=2) → SaddS32` even/odd sync-add on the **reserved** megacore SFLAG slot (`Target::GetMegacoreBarrierSyncFlagNumber` `0x1d60f4e0`, address label `"megacore barrier sync flag"`), with `CHECK(core_count == 2)` / `CHECK(TensorCoresPerLogicalDevice() == 2)` enforcing the even/odd pair.
- **The TC SFLAG block init.** `Target::Init` (`0x1d60fc20`) carves the megacore / AllReduce / Global accessors out of the chip's special-purpose SFLAG range: `base = arr[0] → Target+0x8c0`, `count = arr.size() − 5 → Target+0x8c4`. The `−5` reserves the **top five slots** for the named runtime accessors, of which `base+count+0` is the megacore slot.

The even/odd *twist topology* (which physical core is "primary" vs "secondary", how the two cores map onto the torus) is on **[Megacore Even/Odd Split](../twist/megacore-even-odd.md)**; the SFLAG *handshake protocol* (`VsyncAdd` / `VwaitGeSV` semantics, the full `BarrierType` taxonomy) is in **[Barriers](../barrier/overview.md)** / **[TensorCore Barrier](../barrier/tensorcore-barrier.md)** / **[SpecialPurposeSyncFlags](../barrier/special-purpose-sync-flags.md)**. This page concentrates on the *`MEGACORE(4)` marker, the two-core collective fold, and the megacore SFLAG slot init*.

| | |
|---|---|
| **Megacore TC barrier (runtime fold)** | `LloRegionBuilder::BarrierMegacore` — `0x1d5222a0` |
| **Program-descriptor state fold** | `net_util::SynchronizeProgramDescriptorStatesMegacore` — `0x1c697540` |
| **Megacore SFLAG-slot accessor** | `Target::GetMegacoreBarrierSyncFlagNumber` — `0x1d60f4e0` (`base+count`) |
| **SFLAG-block filler** | `Target::Init` — `0x1d60fc20` (`+0x8c0 = arr[0]`, `+0x8c4 = size − 5`) |
| **Special-purpose SFLAG source** | `TpuChipConfig::GetSpecialPurposeSyncFlags(TpuCoreType)` — `0x20afcf40` |
| **The dead type-4 arm** | `net_util::GetBarrierSyncFlag` — `0x1c69ad00` |
| **Megacore mode gate** | `TpuChipConfig::Megacore()`; `TpuChipConfig+0x7c ≥ 2`; `CoresPerChip(TC) == 2` |
| **Source TUs** | `platforms/xla/service/jellyfish/target.cc`, `.../llo_region_builder.cc`, `.../lowering/net_util.cc` |

---

## 1. The `MEGACORE(4)` Marker Is Never a `BarrierConfig`

`BarrierType` is a 5-valued enum, stored as `BarrierConfig.barrier_type` at struct offset `+0x20` (field 1). The decisive structural result of this fold: **only three of the five arms are ever serialized into a `BarrierConfig`.**

| `BarrierType` | Produced as a `BarrierConfig`? | Realized as | SFLAG slot | Confidence |
|---|---|---|---|---|
| `BARRIER_INVALID(0)` | default (all-zeros, never written) | `GetBarrierSyncFlag` CHECK-fail target | — (illegal) | Confirmed |
| `GLOBAL(1)` | yes — `DetermineBarrierConfigForKey` / `InferBarrierConfig` | `GetGlobalBarrierSyncFlagNumber` | `base+count+4` (TC) | Confirmed |
| `REPLICA(2)` | yes — same producers | per-id window | `base + id` (`id < count`) | Confirmed |
| `CUSTOM(3)` | yes — same producers | per-id window | `base + id` (`id < count`) | Confirmed |
| `MEGACORE(4)` | **NO — never written anywhere** | runtime `BarrierMegacore` (not a config) | `base+count` (separate accessor) | Confirmed |

The two functions that write `BarrierConfig.barrier_type` — `DetermineBarrierConfigForKey` (`0x109c6fa0`, writes `1` / `2` / `3` only) and the normalizer `InferBarrierConfig` (`0x1376c240`, rewrites `CUSTOM→GLOBAL` on a singleton channel, can set `REPLICA`, never `4`) — both confirmed to never emit `4`. There is **no megacore-specific assignment pass**. The `MEGACORE` enumerator is an enum value that is never materialized into a config.

### 1.1 The dead type-4 arm of `GetBarrierSyncFlag`

`net_util::GetBarrierSyncFlag` (`0x1c69ad00`) is the lowering of a `BarrierConfig` to an SFLAG number. The decompile shows `barrier_type` (`a1+0x20`, i.e. `*(int*)(config+32)`) drives exactly **two** branches plus a CHECK — there is no per-type split for 2 vs 3 vs 4:

```c
// net_util::GetBarrierSyncFlag — 0x1c69ad00 (decompiled, condensed)
v3 = *(_DWORD *)(a1 + 32);             // barrier_type (field 1, offset 0x20)
if (v3 == 1) {                          // GLOBAL
    n = Target::GetGlobalBarrierSyncFlagNumber(target);   // base+count+4
    return SflagImmPtr(n, "global barrier sync flag", 24);
}
if (!v3)                                // BARRIER_INVALID
    CHECK("barrier.barrier_type() != BarrierType::BARRIER_INVALID");  // net_util.cc:2065
// ---- ALL OF {2,3,4} share this one arm: ----
v4 = *(_QWORD *)(a1 + 24);              // barrier.id()
CHECK(v4 < *(int*)(target + 2244));     // id < GetBarrierSyncFlagCount()  (target+0x8c4)  net_util.cc:2070
n = v4 + *(_DWORD *)(target + 2240);    // base(+0x8c0) + id
return SflagImmPtr(n, "barrier sync flag number", 24);
```

`2240 = 0x8c0` (base), `2244 = 0x8c4` (count). `REPLICA(2)`, `CUSTOM(3)`, and the unreachable `MEGACORE(4)` would **all** resolve identically to `base + id` (the per-id window), bound-checked `id < count`. There is no byte that distinguishes them — the per-type semantics live entirely upstream in *which emitter runs*. Since neither producer ever writes `4`, this arm is dead for type 4, and — critically — even *if* a type-4 config were synthesized it would resolve to `base + id`, **not** the reserved `base+count` megacore slot. The megacore barrier deliberately bypasses `BarrierConfig` for exactly this reason.

> **NOTE —** the full per-codename `BarrierConfig` producer/consumer taxonomy (the `DetermineBarrierConfigForKey` coloring pass, the `InferBarrierConfig` normalizer, the GLOBAL `base+count+4` slot, the per-id window) lives in [Barriers](../barrier/overview.md), [InferBarrierConfig](../barrier/infer-barrier-config.md), and [SpecialPurposeSyncFlags](../barrier/special-purpose-sync-flags.md). This page only needs the result: type 4 is not a config.

---

## 2. The TensorCore Megacore Barrier (`BarrierMegacore`)

The actual TensorCore megacore barrier is emitted directly by the LLO region builder. `LloRegionBuilder::BarrierMegacore` (`0x1d5222a0`) folds the two physical TensorCores of one logical device on the reserved megacore SFLAG slot.

### 2.1 The megacore-mode gate

The function is a no-op unless **all three** of the megacore conditions hold (decompiled):

```c
// BarrierMegacore — 0x1d5222a0 (gate, decompiled)
v8 = target;                                         // region()->...->target
if (TpuChipConfig::Megacore(chip_config(target))) {  // Megacore() mode
    if (*(int*)(chip_config + 124) >= 2) {           // TpuChipConfig+0x7c >= 2  (cores per megachip)
        v9 = Target::CoresPerChip(target, 0);        // TC cores
        if ((int)v9 >= 2) {
            CHECK(!region()->InPrimaryOrSecondaryRegion());  // llo_region_builder.cc:1751
            ...                                       // fold body
        }
    }
}
// otherwise: no barrier emitted
```

- `TpuChipConfig::Megacore()` — the megacore-mode discriminant (offset path `target[+0x3b8] → +0x18 → Megacore()`).
- `TpuChipConfig+0x7c ≥ 2` — the cores-per-megachip field (offset confirmed; the `≥ 2` test is the megacore condition).
- `CoresPerChip(kTensorCore) == 2` — re-checked as `CHECK(core_count == 2)` at `llo_region_builder.cc:1772` (scalar path) and `:1788` (vector path); the function aborts if a megacore chip ever reports anything other than exactly two TensorCores. This is the **even/odd pair invariant**.
- `CHECK(!region()->InPrimaryOrSecondaryRegion())` (`llo_region_builder.cc:1751`, VLOG `"BarrierMegacore in primary or secondary region, hlo: "`) — the barrier may not be emitted *inside* one of the two per-core sub-regions; it is emitted at the fold point that joins them.

### 2.2 The fingerprint barrier id

Unlike a `BarrierConfig` (which carries a *colored* `id`), the megacore barrier id is a **per-instruction fingerprint** built at emit time — stable across the two cores so both sides compute the same SFLAG cookie:

```c
// BarrierMegacore — 0x1d5222a0 (id construction, decompiled)
v19 = Fingerprint2011(operand_ptr, ...);                 // hash of the region's operand
if (hlo) {                                                // HloInstruction*
    FormatPack(&buf, "%d", /*one arg=*/2);                // "2" -> a literal discriminator
    v24 = Fingerprint2011(buf, ...);
    v19 = FingerprintCat2011(v19, v24);
    HloInstruction::unique_id(hlo);                       // folded into the str-format args
}
// per-module monotone cookie:
v27 = *(int*)(module + 68);                               // cookie_ordinal_
CHECK(v27 < INT32_MAX, "cookie_ordinal_ < std::numeric_limits<int32_t>::max()");  // llo_module.h:147
*(int*)(module + 68) = v27 + 1;                           // post-increment
v28 = FingerprintCat2011(v19, v27);
cookie = (uint32_t)(v28 ^ (v28 >> 32));                   // fold to 32 bits
// annotation: "BarrierMegacore cookie: %u @ %s:%d"
```

The cookie is annotated `"BarrierMegacore cookie: %u @ %s:%d"` (with the `absl::SourceLocation` file/line) so the two cores' barriers are recognizable as a matched pair in dumps. The barrier id therefore comes from `HloInstruction::unique_id()` + a per-module `cookie_ordinal_`, **not** from a `BarrierConfig.id()`.

### 2.3 The even/odd sync-add fold

The fold itself: load the megacore SFLAG slot, then per-partner emit a remote sync-flag add to the *other* core, and wait for the partner's bump.

```c
// BarrierMegacore — 0x1d5222a0 (fold, core_count == 2, decompiled & condensed)
chip   = LloRegionBuilder::ChipId(this, 0);
core   = LloRegionBuilder::CoreIndex(this);               // this core's index (even/odd)
n      = Target::GetMegacoreBarrierSyncFlagNumber(target); // = base+count  (reserved megacore slot)
sflag  = LloRegionBuilder::SflagImmPtr(this, n, "megacore barrier sync flag", 26);

for (v62 = 1; v62 != core_count /*==2*/; ++v62) {         // one partner when core_count==2
    delta   = ScalarU32Constant(v62);                     // +1
    partner = SaddS32(core, delta);                       // core + 1
    partner = SmodU32(partner, core_count /*==2*/);        // (core + 1) mod 2  ->  the OTHER core
    addr    = EncodeRemoteSyncFlagAddress(this, sflag, &chip, /*multicast=*/0);
    op      = LloInstruction::CreateVectorSyncFlagAddRemote(addr, ScalarU32Constant(1), region, ...);
    LloRegion::AppendInstruction(region, op, 0);          // bump partner's megacore SFLAG by 1
}
wait = VwaitGeSV(this, sflag, ScalarU32Constant(core_count - 1) /*==1*/, 0);  // wait for partner's bump
wait.set_annotation_if_not_constant("barrier-megacore-wait");
VsyncAdd(this, sflag, ScalarU32Constant(1 - core_count) /*== -1*/);          // decrement back to 0
// ... ScheckEq verification on the slot ...
```

The arithmetic `SmodU32(CoreIndex + step, core_count)` is the **even/odd partner map**: for `core_count == 2` it sends to `(core + 1) mod 2`, i.e. core 0 signals core 1 and core 1 signals core 0. Each core bumps the partner's copy of the reserved megacore SFLAG by 1 (`VectorSyncFlagAddRemote`, addressed through `EncodeRemoteSyncFlagAddress`), waits until its own slot reaches `core_count − 1 == 1` (`VwaitGeSV`, annotated `"barrier-megacore-wait"`), then decrements by `1 − core_count == −1` (`VsyncAdd`) to leave the slot at zero for reuse. A `MegacoreBarrierAnn` annotation variant is attached to the control-flow push (`VCcfPush` / `SCcfPush`) so a later pass can recognize the fold.

> **NOTE —** the page emits **two near-identical bodies**: a *vector* path (`VectorU32Constant → VCcfPush → … → VCcfPop → SimplifyVtos`, taken on the main `core_count == 2` arm) and a *scalar* path (`ScalarU32ConstantImpl → SCcfPush`, taken when the predication byte selects it). Both gate on `core_count == 2` and both run the same `GetMegacoreBarrierSyncFlagNumber → SflagImmPtr("megacore barrier sync flag") → SaddS32/SmodU32 → VsyncAddRemote` fold; the difference is vector-vs-scalar control-flow push. The detailed `VsyncAdd` / `VwaitGeSV` SFLAG handshake semantics are in [Barriers](../barrier/overview.md).

### 2.4 The program-descriptor state fold (the runtime twin)

`net_util::SynchronizeProgramDescriptorStatesMegacore` (`0x1c697540`) is the second consumer of the megacore SFLAG slot. It rendezvouses the two cores' **program-descriptor state** word (an SMEM control word) rather than a data barrier, and is gated identically:

```c
// SynchronizeProgramDescriptorStatesMegacore — 0x1c697540 (decompiled, condensed)
if (!TpuChipConfig::Megacore(chip_config) || *(int*)(chip_config + 124) < 2)
    return;                                              // same Megacore() + 0x7c>=2 gate
// fingerprint id: unique_id + "%d"(2) + ToShortString + cookie_ordinal_ (++)
//   annotation "SynchronizeProgramDescriptorStatesMegacore cookie: %u @ %s:%d"
CHECK(CoresPerChip(0) / LogicalDevicesPerChip(0) == 2,
      "sync_b.target().TensorCoresPerLogicalDevice() == 2");   // net_util.cc:1337
n     = Target::GetMegacoreBarrierSyncFlagNumber(target);      // base+count
sflag = SflagImmPtr(b, n, "megacore barrier sync flag", 26);
pds   = SmemWordImmPtr(b, Target::ProgramDescriptorStateWordOffset(target),
                       "program descriptor state", 24);
// PrimaryCoreRegionBuilder + SecondaryCore -> EnqueueRemoteSst(pds) to the partner;
VwaitGeSV(b, sflag, 1, 0).set_annotation_if_not_constant("...wait");
VsyncAdd(b, sflag, SimmS32(-1));
Sfence(b);
// ScheckEq verification
```

The CHECK `TensorCoresPerLogicalDevice() == 2` (`= CoresPerChip(TC) / LogicalDevicesPerChip(TC)`, `net_util.cc:1337`) is the third independent statement of the **two-cores-per-logical-device** megacore invariant. This fold uses `EnqueueRemoteSst` to copy the primary core's program-descriptor state to the secondary, fenced by the same reserved megacore SFLAG slot.

### 2.5 The megacore SFLAG slot has exactly three consumers

A full `.text` `E8/E9` cross-reference of `Target::GetMegacoreBarrierSyncFlagNumber` (`0x1d60f4e0`) finds exactly three callers — confirming the reserved `base+count` slot is consumed only by runtime/analysis paths, never via a `BarrierConfig`:

| Consumer (VMA) | Role | Confidence |
|---|---|---|
| `LloRegionBuilder::BarrierMegacore` (`0x1d522641`) | the TC even/odd data barrier (§2.3) | Confirmed |
| `net_util::SynchronizeProgramDescriptorStatesMegacore` (`0x1c6977fc`) | the program-descriptor state fold (§2.4) | Confirmed |
| `RaceAnalyzerStepper::PreProcessEvent` (`0x10bb3229`) | reserves/accounts the slot in the LLO race analyzer | Confirmed |

---

## 3. `GetMegacoreBarrierSyncFlagNumber` — the Reserved Slot Accessor

The megacore SFLAG number is computed by `Target::GetMegacoreBarrierSyncFlagNumber` (`0x1d60f4e0`) as `base + count` — i.e. the **first slot above** the usable per-id barrier window:

```c
// Target::GetMegacoreBarrierSyncFlagNumber — 0x1d60f4e0 (decompiled)
__int64 Target::GetMegacoreBarrierSyncFlagNumber(Target *this) {
  if (!TpuChipConfig::Megacore(chip_config(this)))
    CHECK_FAIL("topology_->chip_config().Megacore()");          // target.cc:154
  return (uint32_t)(this[560] + this[561]);                     // base(+0x8c0) + count(+0x8c4)
}
```

`this[560]` and `this[561]` are `_DWORD` indices, i.e. byte offsets `0x8c0` (base) and `0x8c4` (count). The accessor is **`Megacore()`-gated** with a hard CHECK (`target.cc:154`) — calling it on a non-megacore chip aborts. Contrast the two sibling reserved accessors that index the same block top:

| Accessor (VMA) | Formula | Slot | Consumers |
|---|---|---|---|
| `GetMegacoreBarrierSyncFlagNumber` (`0x1d60f4e0`) | `base + count` | `base+count+0` | `BarrierMegacore` / `SyncProgDesc…Megacore` / `RaceAnalyzer` (§2.5) |
| `GetAllReduceSyncFlagNumber(n)` (`0x1d60f440`) | `base + count + n + 1` (`0 < n < 3`) | `+2`, `+3` | `RotatedPincer*` / `AsyncPincer*` `InitSyncFlags` ([Hierarchical / Pincer](allreduce-hierarchical-pincer.md)) |
| `GetGlobalBarrierSyncFlagNumber` (`0x1d60f420`) | `base + count + 4` | `+4` | `GetBarrierSyncFlag(GLOBAL)` + tree barriers |

`GetAllReduceSyncFlagNumber` CHECKs its phase argument `phase > 0` (`target.cc:143`) and `phase < 3` (`target.cc:144`), so `n ∈ {1, 2}` map to `base+count+2` and `base+count+3`; the `base+count+1` slot (the illegal `n=0`) is an **unused gap**. These four occupied slots plus the gap are exactly the five reserved by `Target::Init` (§4).

---

## 4. The TC SFLAG Block Init (`Target::Init`)

`Target::Init` (`0x1d60fc20`, `target.cc`) fills the TensorCore reserved-barrier block from the chip's special-purpose SFLAG range at boot. The two field stores are byte-confirmed in the decompile:

```c
// Target::Init — 0x1d60fc20 (decompiled, the TC barrier-block filler)
SpecialPurposeSyncFlags = TpuChipConfig::GetSpecialPurposeSyncFlags(chip_config /* kTensorCore */);
if (!SpecialPurposeSyncFlags)
    DieBecauseNull("chip_config.GetSpecialPurposeSyncFlags(::tpu::TpuCoreType::kTensorCore)");  // target.cc
// ... copy + contiguity-check the int range (must be a contiguous ascending run) ...
*((_DWORD *)target + 560) = *v286;        // Target+0x8c0 = arr[0]           (TC base)
*((_DWORD *)target + 561) = v284 - 5;     // Target+0x8c4 = arr.size() - 5   (TC usable count)
```

- **`GetSpecialPurposeSyncFlags(kTensorCore)`** (`0x20afcf40`) returns a `{ptr, count}` vector of SFLAG numbers — a per-`TpuCoreType` slice of the chip-config table (`TpuChipConfig+0x2a0 + core`). `Target::Init` copies it, then asserts (an unrolled `inc/cmp/jne` loop) that the numbers form a **contiguous ascending integer range** so that `base + offset` addressing is valid.
- **`Target+0x8c0 = arr[0]`** — the first special-purpose SFLAG number (the block base).
- **`Target+0x8c4 = arr.size() − 5`** — the usable per-id barrier count. The **`−5` reserves the top five slots** of the range for the named runtime accessors:

```text
TpuChipConfig::GetSpecialPurposeSyncFlags(kTensorCore)  =  [ base ............................. base+size-1 ]
                                                              |<--- usable per-id window --->|<-- 5 reserved -->|
   Target+0x8c0 = base
   Target+0x8c4 = size - 5  =  count            base .. base+count-1   :  REPLICA(2)/CUSTOM(3) ids (id < count)
                                                base+count+0           :  MEGACORE   (GetMegacoreBarrierSyncFlagNumber)
                                                base+count+1           :  (unused gap; GetAllReduceSyncFlagNumber(0) illegal)
                                                base+count+2           :  AllReduce phase 1
                                                base+count+3           :  AllReduce phase 2
                                                base+count+4           :  GLOBAL     (GetGlobalBarrierSyncFlagNumber)
```

So the **megacore barrier slot is the first reserved slot** (`base+count+0`), sitting directly above the usable per-id window that `REPLICA`/`CUSTOM` draw from. The base `Target` constructor (`0x1d615340`) zero-defaults the block (`+0x8c0 = 0`, `+0x8c4 = 0`) before `Init` installs the real per-generation values.

> **NOTE —** the literal `GetSpecialPurposeSyncFlags(TC).size()` per chip codename (hence the concrete `Target+0x8c4` count) is populated from the `chip_config` / `chip_parts` proto and was not extracted from the binary; the **writer** and its formula (`base = arr[0]`, `count = size − 5`) are byte-confirmed (LOW only for the per-codename literal). The SparseCore mirror (`SparseCoreTarget::Init`, base `+0x1d0`, count `+0x1d4` with **no** `−5`) and the three-region SFLAG partition are in [SpecialPurposeSyncFlags](../barrier/special-purpose-sync-flags.md) — the SC engine reserves nothing at the top because its barriers carry a reserved id *within* the block.

### 4.1 Why the megacore slot is `base+count`, not `base+id`

This is the crux of why `MEGACORE(4)` deliberately bypasses `BarrierConfig`. A per-collective `REPLICA`/`CUSTOM` barrier lives **inside** the `[base, base+count)` window, indexed by a colored `id` (§1.1). The megacore fold is **not** per-collective — it is a fixed chip-level rendezvous between the two cores, so it needs a *single dedicated slot* that no per-id barrier can collide with. Placing it at `base+count` (above the usable window) and reaching it through `GetMegacoreBarrierSyncFlagNumber` (not through `GetBarrierSyncFlag`) guarantees that. Had `MEGACORE(4)` been a real `BarrierConfig`, it would have resolved to `base + id` — inside the per-collective window — which is exactly the collision the design avoids.

---

## 5. Relation to the Higher-Level `MegacoreFusion` HLO Pass

The runtime fold above is distinct from the compiler-level `xla::MegacoreFusion` HLO pass (`RunImpl` `0x110d8f00`, `DoMegacoreFusion` `0x110d8860`, TU `tpu_megacore_fusion.cc`). That pass runs *before* lowering: it pairs an all-reduce with a compute op (e.g. `FindARPair` `0x110d5560`, `FindConvARsMatch` `0x110d2980`, `GetAllReduceCosts` `0x110d1bc0`) so that one core's collective overlaps the other core's matmul/convolution — a *scheduling* fusion across the megacore pair. The `BarrierMegacore` / `SynchronizeProgramDescriptorStatesMegacore` constructs on this page are the *lowering-time* rendezvous that make such a paired schedule correct: they are what forces the two cores back into agreement at the fold point. A reimplementer must keep the two layers separate — the HLO pass decides *what* overlaps, the runtime fold guarantees the two halves *synchronize* on the reserved megacore SFLAG.

> **NOTE —** the `MegacoreFusion` HLO pass internals (the AR/conv pairing heuristic, the latency-bound matching, the cost model it queries) are out of scope here; this page documents only that it is a separate, earlier layer. The pincer / binomial AllReduce emitters whose arms it may pair are in [Hierarchical / Pincer](allreduce-hierarchical-pincer.md) and [Binomial / Recursive-Doubling](binomial-recursive-doubling.md).

---

## 6. Reimplementation Checklist

- **Do not emit `MEGACORE(4)`.** No pass writes `BarrierConfig.barrier_type == 4`. Restrict the config producers to `GLOBAL(1)` / `REPLICA(2)` / `CUSTOM(3)`; leave `0`/`4` unreachable. In `GetBarrierSyncFlag`, types `{2,3,4}` share one `base + id` arm bound-checked `id < count` — there is no per-type 2/3/4 logic.
- **Gate the runtime fold three ways.** Emit `BarrierMegacore` only when `TpuChipConfig::Megacore()` ∧ `TpuChipConfig+0x7c ≥ 2` ∧ `CoresPerChip(TC) == 2`. Re-assert `CHECK(core_count == 2)` (and `TensorCoresPerLogicalDevice() == 2` for the descriptor fold) — the even/odd fold is defined only for exactly two cores per logical device.
- **Build the barrier id from a fingerprint, not a config id.** `Fingerprint2011(operand)` ⊕ `"%d"`-format of the discriminator + `HloInstruction::unique_id()` + a per-module monotone `cookie_ordinal_` (CHECK `< INT32_MAX`, post-increment), folded to 32 bits. Annotate `"BarrierMegacore cookie: %u @ %s:%d"`.
- **Run the even/odd sync-add.** Per partner step `v ∈ [1, core_count)`: `partner = SmodU32(SaddS32(CoreIndex, v), core_count)`; `VectorSyncFlagAddRemote(EncodeRemoteSyncFlagAddress(megacore_sflag, chip), +1)`. Then `VwaitGeSV(megacore_sflag, core_count − 1)` (annotate `"barrier-megacore-wait"`) and `VsyncAdd(megacore_sflag, 1 − core_count)` to reset to zero.
- **Reserve the megacore slot.** In `Target::Init`, `base = arr[0] → +0x8c0`, `count = GetSpecialPurposeSyncFlags(TC).size() − 5 → +0x8c4`; the contiguity-checked range's top five are `{Megacore @base+count, gap @+1, AllReduce1 @+2, AllReduce2 @+3, Global @+4}`. Compute the megacore slot as `base + count` via a `Megacore()`-gated accessor — never as `base + id`.
- **Synchronize program-descriptor state too.** `SynchronizeProgramDescriptorStatesMegacore` copies the primary core's `ProgramDescriptorStateWordOffset` SMEM word to the secondary (`EnqueueRemoteSst`), fenced (`Sfence`) on the same reserved megacore SFLAG.

---

## Cross-References

- [Collectives Overview](overview.md) — the two-substrate collective stack and the strategy picker; megacore is a per-chip fold orthogonal to the per-axis ring schedule.
- [AllReduce Hierarchical / Pincer](allreduce-hierarchical-pincer.md) — the bidirectional-ring fusion whose arms the `MegacoreFusion` HLO pass may pair across the two cores; also the consumer of the reserved AllReduce SFLAG slots (`base+count+2`/`+3`).
- [Binomial / Recursive-Doubling](binomial-recursive-doubling.md) — the self-completing butterfly emitter (latency-bound), the other AllReduce family.
- [Physical Core Placement](physical-core-placement.md) — how logical device ids map to physical TensorCores (the even/odd pair this fold synchronizes).
- [ReduceScatter](reduce-scatter.md) — the reduce-scatter phase that runs per core before the megacore fold.
- [Megacore Even/Odd Split](../twist/megacore-even-odd.md) — the twist topology: which core is primary vs secondary and how the pair maps onto the torus.
- [Barriers](../barrier/overview.md) — the SFLAG-based barrier model and the full `BarrierType` taxonomy / handshake protocol.
- [TensorCore Barrier](../barrier/tensorcore-barrier.md) — the TensorCore-side barrier lowering and SFLAG binding.
- [SpecialPurposeSyncFlags](../barrier/special-purpose-sync-flags.md) — the `Target::Init` / `SparseCoreTarget::Init` SFLAG-block fillers and the three-region partition.
- [InferBarrierConfig](../barrier/infer-barrier-config.md) — the `BarrierConfig` normalizer (one of the two type producers, confirmed never to write `4`).
- [back to index](../index.md)
