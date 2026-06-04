# Cost Model Overview

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The TPU compiler in `libtpu.so` prices every LLO instruction, every VLIW bundle, and every fused HLO region against a per-generation cost model. That model is **entirely data-table driven** — there is no learned predictor in the shipping build (`nm -CD` finds only `LearnedCostModelClientOptions` proto methods, never a `Predict`/`Train` implementation; see [Learned Cost-Model Client](learned-cost-model-client.md)). The model is rooted in three independent class families, each selected per `tpu::TpuVersion` through a different registry mechanism, and unified at the bottom by a per-gen `Performance` object that holds the actual cycle integers.

The reader who knows LLVM should map this to two familiar pieces and then note the divergence. The `Performance` latency array is the analogue of an LLVM `MCSchedModel` `WriteLatency` table — one cycle count per opcode bucket. The `CycleTable` is the analogue of the per-target `TargetSchedModel` query surface — it collapses the ~462-opcode LLO universe into ~33 "buckets" and answers throughput queries. The divergence is that XLA's cost model runs at the **HLO level** (it feeds the `LatencyHidingScheduler` and the fusion cost analysis), and runs *in parallel* with a second, completely independent LLVM `TPUWriteLatencyTable` that prices the post-LLO MIR. The two layers have different cycle constants and never share a number; this page documents only the XLA HLO-level model.

Three families, three dispatchers. The `LatencyTable` family answers *dependency latency* (read-after-write stalls) and is selected by a version-indexed `absl::InlinedVector` registry. The `CycleTable` family answers *throughput* (issue-rate cycles) and is selected by a `util_registration::FunctionRegistry` hash map keyed on `Target`. The `Performance` family holds the raw per-(opcode, resource) cycle data and splits into two architectures: a polymorphic `platforms_deepsea::jellyfish::isa::Performance{Jf,Df}` for v2/v3 and non-polymorphic `xla::<gen>::<Gen>Performance` value-types for v4+. The bottom of the page ties all three to one per-gen class table and explains the generation-merge pattern (five classes for six versions).

For reimplementation, the contract is:

- The two registry mechanisms — the version-indexed `InlinedVector` for `LatencyTable` and the `Target`-keyed `FunctionRegistry` for `CycleTable` — including the SOO encoding, bounds checks, and static-init population.
- The `LatencyTable::Create(TpuVersion)` factory dispatch and the per-gen factory→ctor→vtable chain it invokes.
- The `Performance::CreateTensorCore` `DeviceIdentifiers` dispatch that splits v2 (Jf) from v3 (Df) behind one `LatencyTable` class.
- The per-gen class table (five `LatencyTable` + five `CycleTable` + the `Performance` family) and the silicon-seam merge that produces it.
- The TC clock-frequency wiring that converts bundle cycles to wall-clock seconds.

| | |
|---|---|
| **LatencyTable factory** | `xla::jellyfish::LatencyTable::Create(TpuVersion)` @ `0x1c89fba0` |
| **LatencyTable registry** | `(anon)::registry` @ `0x225799f8` — version-indexed `absl::InlinedVector<AnyInvocable,8>` |
| **CycleTable factory** | `xla::jellyfish::CycleTable::Create(Target const&)` @ `0x1c89cc00` |
| **CycleTable registry** | `GetCycleTableRegistry()::r` @ `0x225799e8` — `util_registration::FunctionRegistry` |
| **Performance (v2/v3) factory** | `platforms_deepsea::jellyfish::isa::Performance::CreateTensorCore` @ `0x1d4927e0` |
| **Generations** | `TpuVersion` 0..5 → jellyfish / dragonfish / pufferfish / viperfish / ghostlite / 6acc60406 |
| **TC clock accessor** | `Target::TensorCoreFrequencyInMegaHertz` @ `0x1d615b60` (reads `Target+0x90c`) |
| **Cost-model layer** | HLO-level (LHS + fusion cost); independent of LLVM `TPUWriteLatencyTable` |

---

## The Three-Family Architecture

The cost model is not one object — it is three class families, each per-gen, wired together so that one query (`GetLatencyBetween`, `GetCyclesForThroughput`) reaches the right per-gen cycle integer.

```text
TpuVersion (0..5, from Target+0x398)
   │
   ├─ LatencyTable::Create(version)        ── dependency latency (RAW stalls)
   │     registry @0x225799f8 (InlinedVector, version-indexed)
   │        └─ {Jellyfish, Pufferfish, Viperfish, Ghostlite, Gf/Gfc} subclass
   │
   ├─ CycleTable::Create(Target)           ── throughput (issue-rate cycles)
   │     FunctionRegistry @0x225799e8 (FlatHashMap, Target-keyed)
   │        └─ {JfCycleTable, PfCycleTable, VfCycleTable, GlcCycleTable, GfcCycleTable}
   │
   └─ Performance (the raw cycle integers)
         ├─ v2/v3: isa::Performance{Jf,Df}  (polymorphic, CreateTensorCore)
         └─ v4+ :  xla::<gen>::<Gen>Performance (value-types in the CycleTable ctor)
```

> **NOTE —** both the `LatencyTable` and `CycleTable` query *the same* per-gen `Performance` object; they differ only in the lookup method (`GetLatency`/`GetLatencyBetween` vs `GetCyclesForThroughput`). The throughput cycle and the dependency latency are two views of one cycle table — see [Performance Family Overview](performance-overview.md) and [CycleTable Family](cycletable-family.md).

The two registry mechanisms are deliberately *different*, and that asymmetry is the most surprising structural fact about the cost model. `LatencyTable` uses a raw `InlinedVector` indexed directly by the `TpuVersion` ordinal — O(1), no hashing, no mutex. `CycleTable` uses a general-purpose `util_registration::FunctionRegistry<TpuVersion, ...>` — a mutex-guarded `FlatHashMap` whose factory takes a full `Target const&` rather than a bare version. A reimplementation that uses one mechanism for both will still behave correctly, but it will not match the binary's dispatch cost or its fatal-error message paths.

---

## LatencyTable Factory — `LatencyTable::Create`

### Purpose

`LatencyTable::Create(TpuVersion)` returns the per-gen object that prices read-after-write dependency latency for the scheduler's `GetLatencyBetween` query. It is a **registry dispatch over a version-indexed vector**, not a hardcoded `switch`.

### Entry Point

```text
LatencyTable::Create(TpuVersion)            @0x1c89fba0
  └─ registry[version].invoker(entry, version)  ── the AnyInvocable LocalInvoker
        └─ new <size> + LatencyTable<Gen>::ctor(version)
```

### Algorithm

```c
function LatencyTable_Create(version):              // sub_1C89FBA0
    if VLOG(2) enabled:                             // cmp esi,0x2 + VLogSite @0x2236e060
        log("Creating latency table for deepsea version: ", version)
    if registry == null:                            // (anon)::registry @0x225799f8
        LogFatal("registry != nullptr")             // latency_table.cc:120
    if (s64)version < 0:
        LogFatal("index >= 0")                      // latency_table.cc:122
    reg0 = *registry                                // SOO header word
    size = reg0 >> 1                                // InlinedVector size = header>>1
    if size <= version:
        LogFatal("index < registry->size()")        // latency_table.cc:123
    buf  = (reg0 & 1) ? *(registry+0x10) : registry+0x10   // inline vs heap buffer
    entry   = buf + version * 0x20                  // element stride 0x20
    factory = *(entry + 0x18)                       // AnyInvocable invoker fn ptr
    if factory == null:
        LogFatal("(*registry)[index]")              // latency_table.cc:124
    return factory(entry, version)                  // call rax @0x1c89fc0f
```

The dispatch is byte-exact in the decompile: the `absl::InlinedVector` header word is `2*size | inline_bit`, so `size = header >> 1` and `inline_bit = header & 1`. Each element is `0x20` bytes; the `AnyInvocable`'s manager sits at `+0x10` and its invoker at `+0x18`. `Create` calls the invoker with `rdi = entry` (the `AnyInvocable`'s storage) and `esi = version`.

> **GOTCHA —** the `cmp esi, 0x2` at the top of `Create` is **not** a dispatch arm — it is the `VLOG(2)` verbosity gate (`VLogSite` @ `0x2236e060`). A reimplementer reading the disassembly literally would mistake it for "version >= 2 → some path". It only controls whether a trace line is emitted. The actual bound is `version < size` against the registry.

### Registry Population — `LatencyTable::Register`

The registry is populated at static-init by `LatencyTable::Register(version, AnyInvocable)` @ `0x1c89fac0`. It resizes the `InlinedVector` to `version + 1` (lazily allocating `operator new(272, 16)` on first call), then stores the `AnyInvocable`'s manager at slot `version*0x20 + 0x10` and its invoker at `+0x18`. The store offsets are identical to the ones `Create` reads back — confirming the registry is **direct-indexed by the `TpuVersion` ordinal**.

Six `Register` calls run from five translation-unit initializers:

| TpuVer | codename / gen | static-init TU | factory invoker | ctor (new size) | subclass |
|---|---|---|---|---|---|
| 0 | jellyfish / v2 | `latency_table_jf.cc` @ `0x21353860` | jf `$_0` @ `0x1c8a1280` | `LatencyTableJellyfish` @ `0x1c8a0c20` (0x58) | `xla::jellyfish::LatencyTableJellyfish` |
| 1 | dragonfish / v3 | `latency_table_jf.cc` @ `0x21353860` | jf `$_1` @ `0x1c8a12c0` | `LatencyTableJellyfish` @ `0x1c8a0c20` (0x58) | `xla::jellyfish::LatencyTableJellyfish` |
| 2 | pufferfish / v4 | `latency_table_pf.cc` @ `0x213538d0` | pf `$_0` @ `0x1c8a31c0` | `LatencyTablePufferfish` @ `0x1c8a1960` (0x1e0) | `xla::pufferfish::LatencyTablePufferfish` |
| 3 | viperfish / v5p | `latency_table_vf.cc` @ `0x21353920` | vf `$_0` @ `0x1c8a5280` | `LatencyTableViperfish` @ `0x1c8a3f20` (0x1e0) | `xla::viperfish::LatencyTableViperfish` |
| 4 | ghostlite / v6e | `latency_table_gl.cc` @ `0x21353970` | gl `$_0` @ `0x1c8b28e0` | `LatencyTableGhostlite` @ `0x1c8b0c00` (0x1e0) | `xla::ghostlite::LatencyTableGhostlite` |
| 5 | 6acc60406 / v7 | `latency_table_gf.cc` @ `0x213539c0` | gf `$_0` @ `0x1c8bb180` | (anon-ns gf ctor) @ `0x1c8b9520` (0x1e0) | (Gf/Gfc anon-ns) |

Five subclasses serve six versions: `LatencyTableJellyfish` is registered twice (slots 0 and 1), differing only by the `TpuVersion` it forwards to its ctor. The Jellyfish object is small — `0x58` bytes — because the v2/v3 latency model is 15 inline fields with no heap `MxuLatencyTable`. The PF/VF/GL/GF objects are `0x1e0` (480 bytes): they carry a heap table layer including the `+0x1d8` `MxuLatencyTable` (VF/GL) and a `~30`-entry XLU conflict penalty table.

> **NOTE —** the v7 (`Gf/Gfc`) `LatencyTable` subclass is an **anonymous-namespace** type in `latency_table_gf.cc` — it has no named typeinfo symbol, so its exact C++ class name is unrecoverable from the symbol table (MEDIUM confidence on the *name*; its vtable @ `0x21c20930` (offset-to-top at `0x21c20930`, typeinfo at `0x21c20938`, first slot/vptr at `0x21c20940` — the ctor installs `*obj = 0x21c20940`), ctor @ `0x1c8b9520`, and `GhostlitePerformance` delegation are CERTAIN). Its `VectorRawHazardCycles` returns the constant `7`, and its `LatencyBetweenInternal` @ `0x1c8bab40` reuses `GhostlitePerformance::GetResourceUsage` — the v6e/v7 latency backend is shared behind distinct wrapper classes.

### Function Map

| Function | Address | Role |
|---|---|---|
| `LatencyTable::Create` | `0x1c89fba0` | Version-indexed registry dispatch → per-gen factory |
| `LatencyTable::Register` | `0x1c89fac0` | Resize-to-`version+1` + store `{manager,invoker}` at slot `version*0x20` |
| `Storage::Resize<DefaultValueAdapter>` | `0x1c8a07c0` | `InlinedVector` growth in `Register` |
| `LatencyTable` base ctor | `0x1c89f800` | Zero body, set base vtable, forward version |

---

## CycleTable Factory — `CycleTable::Create`

### Purpose

`CycleTable::Create(Target const&)` returns the per-gen object that prices issue-rate throughput cycles via `GetCyclesForThroughput(Instruction)`. It dispatches through a **`Target`-keyed `FunctionRegistry`** — a distinct mechanism from the `LatencyTable` `InlinedVector`.

### Algorithm

```c
function CycleTable_Create(target):                 // sub_1C89CC00
    once: GetCycleTableRegistry()::r = new(0x28)     // __cxa_guard lazy-init @0x225799e8
    version = *(int*)(target + 0x398)                // TpuVersion at Target+0x398
    fn = FunctionRegistry::Get(registry, version)    // sub_1C89CD20: Mutex + FlatHashMap::find
    if fn.missing:                                   // [result+0x10] == 1
        LogFatal("No cycle table registered for platform: ", version)  // cycle_table.cc:960
    return fn(target)                                // call the registered std::function
```

The registry is a `util_registration::FunctionRegistry<TpuVersion, unique_ptr<CycleTable>(Target const&)>` — a `Mutex`-guarded `FlatHashMap`. Its `Get` (@ `0x1c89cd20`) takes a shared lock and probes for the version key; a miss is fatal. The factory it stores is a `std::function` that takes the full `Target const&` (not a bare version), so the per-gen `CycleTable` ctor can read chip parameters directly.

`_GLOBAL__sub_I_cycle_table.cc` @ `0x21353460` performs six `Register` inserts. The five distinct `CycleTable` subclasses (all in the `xla::jellyfish` namespace) and the `Performance` each builds:

| TpuVer | gen | CycleTable subclass | new-size | Performance built | vtable |
|---|---|---|---|---|---|
| 0 (v2) | jellyfish | `JfCycleTable` | 0x18 | `CreateTensorCore` → `PerformanceJf` | `0x21c1ffb8` |
| 1 (v3) | dragonfish | `JfCycleTable` | 0x18 | `CreateTensorCore` → `PerformanceDf` | `0x21c1ffb8` |
| 2 (v4) | pufferfish | `PfCycleTable` | 0x30 | `PufferfishPerformance` | `0x21c20048` |
| 3 (v5p) | viperfish | `VfCycleTable` | 0x20 | `ViperfishPerformance` | `0x21c200c8` |
| 4 (v6e) | ghostlite | `GlcCycleTable` | 0x30 | `GhostlitePerformance` | `0x21c20148` |
| 5 (v7) | 6acc60406 | `GfcCycleTable` | 0x30 | `GhostlitePerformance` (shared) | `0x21c201c8` |

`JfCycleTable` serves both v2 and v3 (two registrations, one class). `GlcCycleTable` and `GfcCycleTable` are distinct wrapper classes that both construct `GhostlitePerformance` — the v6e/v7 throughput backend is shared, mirroring the `LatencyTable` side.

---

## Performance Family — `CreateTensorCore`

### Purpose

The `Performance` object holds the raw cycle integers: a per-instruction latency array plus a 2-D per-(Instruction, Resource) cycle-cost array. There are two architectures.

For v2/v3, `Performance::CreateTensorCore(DeviceIdentifiers)` @ `0x1d4927e0` selects between two polymorphic subclasses by comparing the device identifiers:

```c
function CreateTensorCore(ids):                     // sub_1D4927E0
    if ids == kJellyfishIdentifiers:                // @0xbdf3c0c, byte compare
        return new(0xE00) PerformanceJf(ids)        // vtable 0x21cc74b8
    elif ids == kDragonfishIdentifiers:             // @0xbdf3c18
        return new(0xE00) PerformanceDf(ids)        // vtable 0x21cc7468
    else:
        LogFatal("Don't know how to create performance for ...")  // performance.cc:27
```

This is where the v2-vs-v3 cost split lives. One `JfCycleTable` class and one `LatencyTableJellyfish` class serve both, but the underlying grid is `PerformanceJf` (v2) or `PerformanceDf` (v3). `PerformanceDf` is byte-identical to `PerformanceJf` except for a single patch at offset `+0x28` (storing `66` and `13` as a DWORD pair); that one delta is the entire cost difference between Jellyfish and Dragonfish in this build, and it lives in the dependency-latency path, not the throughput path.

For v4+, the `Performance` is a non-polymorphic `xla::<gen>::<Gen>Performance` value-type (`PufferfishPerformance`, `ViperfishPerformance`, `GhostlitePerformance`) constructed inside the per-gen `CycleTable` ctor. See [Performance Family Overview](performance-overview.md) for the per-gen grid metadata.

---

## The Per-Gen Cost-Model Class Table

The authoritative per-gen table fuses the execution-unit counts (read from the per-gen `VectorIsa` — `Target+0x4a8`=iar, `+0x4ac`=mxu, `+0x4b0`=xlu) with the three class families:

| TpuVer | codename | gen | mxu | xlu | iar | LatencyTable | CycleTable | Performance | LT size | LT vtable |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | jellyfish | v2 | 1 | 1 | 2 | `LatencyTableJellyfish` | `JfCycleTable` | `PerformanceJf` (isa) | 0x58 | `0x21c202d0` |
| 1 | dragonfish | v3 | 2 | 1 | 2 | `LatencyTableJellyfish` | `JfCycleTable` | `PerformanceDf` (isa) | 0x58 | `0x21c202d0` |
| 2 | pufferfish | v4 | 4 | 2 | 2 | `LatencyTablePufferfish` | `PfCycleTable` | `PufferfishPerformance` | 0x1e0 | `0x21c20320` |
| 3 | viperfish | v5p | 4 | 3 | 2 | `LatencyTableViperfish` | `VfCycleTable` | `ViperfishPerformance` | 0x1e0 | `0x21c203f0` |
| 4 | ghostlite | v6e | 2 | 2 | 2 | `LatencyTableGhostlite` | `GlcCycleTable` | `GhostlitePerformance` | 0x1e0 | `0x21c20698` |
| 5 | 6acc60406 | v7 | 2 | 2 | 2 | (Gf/Gfc anon-ns) | `GfcCycleTable` | `GhostlitePerformance` (shr) | 0x1e0 | `0x21c20930` |

> **QUIRK —** the cost model collapses six `TpuVersion`s onto **five** class boundaries at the silicon-architecture seams. v2/v3 share the Jellyfish latency class + `JfCycleTable` + the `isa::Performance` family (split only by `DeviceIdentifiers` → Jf/Df). v4 and v5p each get a dedicated class trio. v6e/v7 share `GhostlitePerformance` behind distinct wrapper classes. This is why the v6e/v7 `VectorIsa` is byte-identical (mxu=2, xlu=2, iar=2) and the GL/GF transcendental estimates match (142 / 151): they are the same backend behind two wrappers. A reimplementation keyed on six versions will allocate two objects where the binary allocates one shared backend — functionally fine, but it will not reproduce the shared-pointer identity the binary relies on.

---

## Clock Frequency — Cycles to Wall-Clock

The cost model produces cycle counts; the scheduler needs seconds. The conversion reads a single per-gen field. `Target::TensorCoreFrequencyInMegaHertz` @ `0x1d615b60` returns `*(uint32*)(Target + 0x90c)`, populated in `Target::Init` from the `TpuCorePartsProto.frequency_mhz` field. `NodeCost` (`CostModelLatencyEstimator::NodeCost` @ `0x10ffaba0`) then computes:

```c
time_s = cycles * trip_count / (TC_freq_MHz * 1e6)   // 1e6 const @0xa2e0208
```

`CostModelLatencyEstimator::CyclesPerMicrosecond` @ `0x10ffb3c0` is *not* a constant — it delegates straight to `TensorCoreFrequencyInMegaHertz(target)` (reading the estimator's `Target` ptr at `+0x28`), so `kCyclesPerMicrosecond == TC_freq_MHz`. `TensorCoreFrequencyInMegaHertz` (`0x1d615b60`) is a one-instruction `mov 0x90c(%rdi),%eax` field read; the value is selected per active `TpuVersion` from the `chip_parts` blob loaded into `Target+0x90c`. All nine codename `chip_parts` blobs are embedded in this build (see [chip_parts.binarypb Decode](../targets/chip-parts-binarypb.md)), so the per-gen TC clocks are recoverable, not inferred: v6e = 1750 MHz, v7x = 1900 MHz (CONFIRMED against the `chip_parts` `Core[TC].frequency_mhz` field — both are present as 32-bit `.rodata` constants: `0x76c` = 1900 in the v7 chip_parts block, `0x6d6` = 1750 for v6e). The wiring (`Target+0x90c` → `/freq/1e6`) is gen-invariant.

---

## Related Components

| Component | Relationship |
|---|---|
| [Performance Family Overview](performance-overview.md) | The raw per-(opcode, resource) cycle grids selected by both factories |
| [CycleTable Family](cycletable-family.md) | The throughput-table subclasses dispatched by `CycleTable::Create` |
| [Resource Enum (23-slot)](resource-enum.md) | The `ResourceVector` slots that `Acc`/`MaxResourceCycles` reduce |
| [MXU Latency Overview](mxu-latency-overview.md) | The heap `MxuLatencyTable` layer inside the v4+ `LatencyTable` objects |
| [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) | The scheduler's separate 47-ID resource model (concurrency, not cycle weight) |

## Cross-References

- [Resource Enum (23-slot)](resource-enum.md) — the `ResourceVector` slot model and the `MaxResourceCycles` reduction the cost model feeds
- [Performance Family Overview](performance-overview.md) — the cycle-integer backend shared by both factories
- [CycleTable Family](cycletable-family.md) — `CycleTable` subclass dispatch and the `Instruction` bucket enum
- [JfCycleTable](jf-cycletable.md) — the v2/v3 throughput table and the `0x19FFC0821` bucket mask
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen cycle master table
- [MXU Latency Overview](mxu-latency-overview.md) — the `MxuLatencyTable` layer reached through the v4+ `LatencyTable`
- [Consolidated Per-Gen Counts](iars-per-tensorcore.md) — the mxu/xlu/iar `VectorIsa` counts fused into the class table
- [Learned Cost-Model Client](learned-cost-model-client.md) — why the shipped model is data-table driven, not ML
- [MXU Slot](../isa/slot-mxu.md) — the LLO MXU opcodes the cost model buckets into matmul/matpush/matres
- [Scheduler Overview](../sched/overview.md) — the `LatencyHidingScheduler` consumer of `GetLatencyBetween`
