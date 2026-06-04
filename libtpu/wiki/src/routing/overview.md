# ICI Routing — Section Map

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset, base `0xe63c000`). All symbols below are present in the full-symbol binary; demangled names and addresses are cross-checked against the IDA decompile.

## Abstract

This page is the **map of the ICI routing subsystem** — the layer that decides, for every (source chip, destination chip) pair on the 3-D torus, the exact sequence of ICI link hops a transfer takes. Routing sits *between* the collective stack (which decides the per-color ring schedule) and the ICI fabric (which moves the bytes): the collective emitters tell routing "this core sends to that core"; routing answers "out which SerDes port, through which intermediate chips." Concretely it produces a per-chip static routing table, installed once at slice bring-up and indexed thereafter by the on-chip routing engine — there is no runtime reroute.

The subsystem has two logically distinct outputs that are easy to conflate, so the central organizing idea of this section is to keep them apart. The first is the **resilient ICI route table** (`accel_ssw::deepsea::slice_builder`): a per-link auto-routing table that answers "given a destination chip, which output link" for the whole slice, generated once around faulty links by the `*ToroidalWildFirstPaths` algorithm and installed as a `PerLinksRoutingTable`. The second is the **per-collective net_router schedule** (`xla::jellyfish::net_router`): an explicit per-step, per-direction DMA program that answers "at step k, on ICI port d, this core DMAs src→dst," compiled by `CreateRoutingScheduleLiteral` into an `s32[N]` constant and replayed by `EmitRoutingCode`. The two are orthogonal and run in different namespaces; both terminate at the ICI DMA layer.

This page documents (1) the **end-to-end route pipeline** from replica groups down to the installed table and the runtime schedule; (2) the **two route families** — freshly generated toroidal paths (`PopulateUnoptimizedRouteCache`) versus precomputed `ToroidalRouteCache` lookups; and (3) the **cache-vs-generate decision** at the heart of `InitRouteSolution`. Each algorithm has its own sibling page; this page links them and does not duplicate their byte-level derivations. Collectives, twisted-torus geometry, and barriers are sibling **sections**, linked from [Cross-References](#cross-references).

For reimplementation, the contract of the routing subsystem is:

- The **generator class hierarchy**: a `RoutingTableGenerator` abstract base with a factory (`RoutingTableGeneratorFactory`) that selects NHop vs limited-ICI routing, the parallel unicast generator (`ParallelRoutingTableGenerator`), and the resilient `*Topology` family that owns `InitRouteSolution` / `GetStaticPath(s)`.
- The **resilient path generator** `*ToroidalWildFirstPaths`: minimal dimension-order routing first, then a one-hop "wild-first" detour around the periodic fault lattice, validated for fault-freedom and tagged with a dateline-symmetry phase.
- The **route cache**: the `ToroidalRouteCache` binarypb schema, its `CompressedToroidalRouteCache` wrapper, the `RouteCacheDeduplicator` cache-key lookup, and the three embedded cache sets.
- The **runtime hand-off**: how a `StaticPath` direction sequence becomes a `PerLinksRoutingTable` row and a DMA-descriptor routing-table index, and how the net_router schedule literal is replayed per core per step.

| | |
|---|---|
| **Resilient namespace** | `accel_ssw::deepsea::slice_builder` (route table, bring-up time) |
| **net_router namespace** | `xla::jellyfish::net_router` (per-collective schedule, compile time) |
| **Generator base / factory** | `RoutingTableGenerator` @`0x1fbd72e0` · `RoutingTableGeneratorFactory::CreateGenerator` @`0x1fbd3dc0` |
| **Resilient orchestrator** | `ResilientToroidalTopology::InitRouteSolution` @`0x1fbdf8a0` |
| **Live path generator** | `RandomizedToroidalWildFirstPaths::PathsWithFaults` @`0x1fbea380` · `NonRandomizedToroidalWildFirstPaths::Paths` @`0x1fbe6ae0` |
| **Cache lookup** | `RouteCacheDeduplicator::Find` @`0x20b59000` → `ToroidalRouteCache::Create` @`0x20b5d6e0` |
| **Schedule literal** | `net_router::CreateRoutingScheduleLiteral` @`0x13822400` → `EmitRoutingCode` @`0x13819ca0` |
| **Routing model** | Static, per-chip, dimension-order on a 3-D torus; no runtime reroute |

---

## 1. The end-to-end route pipeline

A collective's traffic pattern descends through six conceptual stages before any byte moves on a link. Stages 1–2 belong to the collective stack and are documented in [Collectives](../collectives/overview.md); stages 3–6 are this section. The split between the **route table** (stages 3–5, installed once at bring-up) and the **route schedule** (the net_router program compiled per collective instance) is the single most important structural fact and is broken out in §1.1.

```text
replica groups (HLO ReplicaGroup device lists)
        │   [collectives §1.3]  ReplicaGroupsOnNDPlane  →  physical chip coords on the torus
        ▼
[1] inter-node distances        GetDistances(src,dst)        ── per-dim signed torus-reduced vector
        │                       (topology vtable+0xb8; twist-adjusted for _twisted shapes)
        ▼
[2] path generation             one of two families (§2):
        │   (A) toroidal WildFirst — minimal DOR first, then wild-first detour around faults
        │   (B) precomputed ToroidalRouteCache lookup
        ▼
[3] per-hop routing schedule    PathFromDistance → ordered Direction[] (strict dimension order)
        │                       + HalfwayDirections / UpdateSymmetryForHalfway (dateline phase)
        ▼
[4] DMA route-table emission    Direction[] → PerLinksRoutingTable row {output_link_index, next_hop_chip_id}
        │                       installed via Master::SetRoutingTable (bring-up only)
        ▼
[5] net_router runtime          two consumers of the installed table:
            (a) auto-route:  DMA descriptor carries dest chip id → on-chip routing engine indexes the table
            (b) explicit:    EmitRoutingCode replays the per-step CreateRoutingScheduleLiteral program
```

> **NOTE —** stages 1–4 run **once per slice at bring-up** and produce a *static* table; there is no dynamic rerouting in this build. A faulty link is handled by generating around it ahead of time (§3), not by reacting at runtime. This is why the whole subsystem is "static-but-fault-aware": the fault tolerance is entirely in the *generation*, never in the datapath.

### 1.1 Two outputs, one section: route table vs route schedule

The routing subsystem materializes two artifacts that a reimplementer must not merge:

| Aspect | Resilient route table | net_router route schedule |
|--------|-----------------------|---------------------------|
| Namespace | `accel_ssw::deepsea::slice_builder` | `xla::jellyfish::net_router` |
| Question answered | "dest chip → which output link" (per-link auto-route) | "step k, direction d → DMA src_ptr→dst_ptr" (explicit program) |
| When built | once at slice bring-up | per collective instance at compile time |
| Generator | `*ToroidalWildFirstPaths` / `ToroidalRouteCache` | `CreateRoutingSchedule` @`0x1381c6a0` |
| Stored as | per-chip `PerLinksRoutingTable` (installed in hardware) | `s32[N]` `xla::Literal` constant (ConstantMapper Type 5) |
| Consumed by | on-chip routing engine (indexes by dest chip id) | `EmitRoutingCode` @`0x13819ca0` (replays 4 directions/step) |
| Per-page | [Route-Table Generation](route-table-generation.md), [Toroidal Route Cache](toroidal-route-cache.md) | [Create Routing Schedule](create-routing-schedule.md), [Net-Router Pipeline](net-router-pipeline.md) |

Both end at the ICI: the per-step `RoutingTableStartDma` issued by `EmitRoutingCode` carries the DMA descriptor whose routing field is resolved by the installed per-link table. The schedule supplies the *(src_ptr, dst_ptr)* per step/direction; the route table supplies the *multi-hop link path* for that descriptor. See [ICR Node-Fabric DMA](icr-node-fabric-dma.md) and [NF Descriptor](nf-descriptor.md) for the descriptor layout, and the [ICI fabric](../ici/overview.md) section for the DMA layer.

### 1.2 The generator class hierarchy

The resilient route table is produced by a small class family rooted at an abstract `RoutingTableGenerator`. A factory chooses the concrete generator from the routing mode; the resilient generator is one of several `*Topology` implementations of a shared `InitRouteSolution` / `GetStaticPath(s)` interface.

```text
RoutingTableGenerator (abstract)              0x1fbd72e0 (ctor) / GeneratesDeadlockFreeTables 0x1fbd5f60
  ├─ RoutingTableGeneratorFactory
  │     ├─ CreateGenerator        0x1fbd3dc0   ── selects the concrete generator
  │     ├─ IsNHopRouting          0x1fbd3fa0   ── classic N-hop routing mode
  │     └─ IsLimitedIciRouting    0x1fbd3fe0   ── limited-ICI-routing mode (net_router schedule)
  ├─ ParallelRoutingTableGenerator            0x1fbd4ae0 (ctor)
  │     ├─ Generate                0x1fbd4b60  ── parallel route-table build
  │     ├─ CreateUnicastRoutingTables 0x1fbd5340/ CreateSrcDestUnicastRoutingTables 0x1fbd5640
  │     └─ RouteTargetCache::{GetPath 0x1fbd42c0, PopulatePath 0x1fbd4360, PopulateLinks 0x1fbd4680}
  └─ *Topology  (own InitRouteSolution / GetStaticPath(s)):
        ├─ Topology::{InitRouteSolution(base), GetStaticPath 0x20bf7820, GetStaticPaths 0x20bf78c0}
        ├─ ResilientToroidalTopology::{InitRouteSolution 0x1fbdf8a0, GetStaticPath 0x1fbe1ce0, GetStaticPaths 0x1fbe1e20}
        └─ TwistedTorusTopology::{InitRouteSolution 0x20b3f7c0, GetStaticPath 0x20b407c0}   (see ../twist/overview.md)
```

> **NOTE —** `GetStaticPath` (singular) and `GetStaticPaths` (plural) are both real entry points and are *not* interchangeable. Under the randomized policy a (src,dst) class maps to a *set* of equal-cost paths, so the singular accessor hard-errors ("`GetStaticPath` … is not defined when `randomized_paths` is enabled, use `GetStaticPaths` (plural)", `.rodata` @`0xa11432f`). Reimplement the plural accessor as the canonical one; the singular is a convenience that is valid only for the non-randomized (single-`StaticPath`) policy. See [Get Static Path](get-static-path.md).

---

## 2. The two route families

Both families are realizations of the **same** path-generation algorithm; they differ only in delivery. Family B is the production fast path; family A is the live fallback. The decision between them is §3.

### 2.1 Family A — freshly generated toroidal paths (`*ToroidalWildFirstPaths`)

When no precomputed cache matches the slice's fault pattern, `PopulateUnoptimizedRouteCache` @`0x1fbe0a20` runs the live generator at bring-up. It instantiates one of two policy classes and calls `Paths(src,dst)` for every chip pair:

```c
function PopulateUnoptimizedRouteCache():            // 0x1fbe0a20
    if randomized_paths:
        gen = CreateRandomized(...)                  // 0x1fbe0a20 +0x160 → RandomizedToroidalWildFirstPaths
    else:
        gen = CreateNonRandomized(...)               // 0x1fbe0a20 +0x230 → NonRandomizedToroidalWildFirstPaths
    for each (src, dst) chip pair:
        paths = gen.Paths(src, dst)                  // build the per-pair route(s)
    // "Unoptimized" = correct (deadlock-free, fault-avoiding) but not load-balanced like the baked tables
```

The algorithm per pair, in brief (full derivation in [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md)): compute the torus-reduced distance ([Get Distances](get-distances.md)); build the minimal **dimension-order** path (`PathFromDistance` @`0x1fbeea20`); if that path avoids the periodic fault lattice (`IsFaultFreeLink` @`0x1fbede40` — a per-dimension `coord mod symmetry == fault_coord mod symmetry` test, confirmed in the decompile as `v12 % v29 == v15 % v29` per axis plus a `Direction::IsSame` match), accept it. Otherwise enter the **wild-first** loop: take one deliberately non-minimal hop, route the remainder in dimension order, validate fault-freedom, and tag the path with the dateline-symmetry phase. The *randomized* policy keeps all fault-free wild paths (emitted as a `RandomHop` set); the *non-randomized* policy keeps only the first (emitted as a single `StaticPath`).

> **QUIRK —** "Randomized" is **not** a runtime PRNG. It is a deterministic multi-path *policy*: the generator emits a set of equal-cost wild-first candidates, and the runtime spreads traffic across them by a deterministic hash. The choice is captured per-cache as the `RouteScheme` oneof variant (`RandomHop` vs `StaticPath`), not seeded at runtime. The wild-first detour and the periodic-fault model are documented in [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md).

### 2.2 Family B — precomputed `ToroidalRouteCache` lookups

The production fast path loads a pre-baked route table instead of generating one. The embedded caches are the *offline output* of the same WildFirst algorithm, run ahead of time for every supported (fault symmetry, fault count, fault dimension, slice shape, twist) tuple. On a cache hit, `ToroidalRouteCache::Create` @`0x20b5d6e0` decompresses and lowers the matching binarypb into the in-memory per-link table. The schema, the decompression wrapper, and the binarypb codec are sibling pages:

- [Toroidal Route Cache](toroidal-route-cache.md) — the `ToroidalRouteCacheConfigs → ToroidalRouteCache → RouteScheme {StaticPath, RandomHop}` message hierarchy and `ToroidalRouteCache::Create`'s cache-key signature.
- [Route-Cache Codec](route-cache-codec.md) — the packed direction-code encoding (`element_size_in_bits`, the bit-width of each packed `Direction`).
- [Route-Cache Decompress](route-cache-decompress.md) — `Decompress(CompressedToroidalRouteCache)` @`0x20b63320`, the on-disk inflate step.
- [Route-Cache Dedup](route-cache-dedup.md) — `RouteCacheDeduplicator::Find` @`0x20b59000` and the `(fault dimensions, symmetry, twist, dimension sizes)` cache key.

> **GOTCHA —** the two families are not byte-identical outputs. The baked cache is offline-load-balanced; the live `PopulateUnoptimizedRouteCache` result is correct but **un-optimized** (the literal "Unoptimized" in the function name). A reimplementer who falls to the live generator gets deadlock-free, fault-avoiding routes that may carry more traffic on the wild axis than the baked tables would — functionally correct, but a measurable performance cliff. A fault pattern that just misses a baked shape silently takes this slow path.

---

## 3. The cache-vs-generate decision

`ResilientToroidalTopology::InitRouteSolution` @`0x1fbdf8a0` is the orchestrator that decides between the two families. It is invoked once at bring-up after topology discovery. The decompiled control flow (743 lines; key lines cited) is exactly:

```c
function InitRouteSolution():                                  // 0x1fbdf8a0
    // dedup = GetCacheDeduplicator(generation): a per-generation switch (line 260)
    //   selects ONE of three lazily-constructed static deduplicator instances,
    //   each combining the common kRouteCacheSet with at most one generation set.
    switch generation:                                         // HIDWORD(v90)
      case 4:  dedup = gf_deduplicator                        // 6acc60406 / TPU7x generation
                 UpdateDeduplicator(dedup, kRouteCacheSet)     // line 283
                 UpdateDeduplicator(dedup, k6acc60406RouteCacheSet) // line 295
      case 2:  dedup = vf_deduplicator                        // viperfish
                 UpdateDeduplicator(dedup, kRouteCacheSet)     // line 618
                 UpdateDeduplicator(dedup, kViperfishRouteCacheSet) // line 632
      case 1:  dedup = pf_deduplicator                        // pufferfish (default)
                 UpdateDeduplicator(dedup, kRouteCacheSet)     // line 668
    key = ToFaultyDimensions(discovered_faults)                // cache key from the fault pattern
    cache = RouteCacheDeduplicator::Find(key)                  // line 335  — 0x20b59000
    if cache found:                                            // PATH B — cache hit (fast path)
        table = ToroidalRouteCache::Create(Decompress(cache))  // line 470  — 0x20b5d6e0
    else:                                                      // PATH A — cache miss (live fallback)
        table = PopulateUnoptimizedRouteCache()                // line 354  — 0x1fbe0a20
    CreateIciResilientFaults(symmetric_fault_set)              // line 204  — record faults for telemetry/install
    install table  (PerLinksRoutingTable, Master::SetRoutingTable)
    // if any (src,dst) has no fault-free path → "No route solution for topology %s." (.rodata 0xa02af1b)
```

> **NOTE — dedup population is per-generation.** A `switch` inside the inlined `GetCacheDeduplicator(int)` picks one of three lazily-constructed static instances — there is no single deduplicator that loads all sets. `gf_deduplicator` (case 4 — the `6acc60406` / TPU7x generation) loads `kRouteCacheSet` + `k6acc60406RouteCacheSet`; `vf_deduplicator` (case 2 — viperfish) loads `kRouteCacheSet` + `kViperfishRouteCacheSet`; `pf_deduplicator` (case 1 — pufferfish/default) loads `kRouteCacheSet` alone. `kRouteCacheSet` is the common base every generation shares; the generation-specific set is layered on top. The `k6acc60406RouteCacheSet` name surfaces only in the `GetCacheDeduplicator` `CHECK` strings, not as an independent `nm` data symbol. Confidence: HIGH. See [Toroidal Route Cache](toroidal-route-cache.md).

### 3.1 The per-generation deduplicators and their cache sets

`GetCacheDeduplicator(int)` switches on the generation enum and returns one of three lazily-constructed static deduplicator instances. Each combines the common `kRouteCacheSet` with at most one generation-specific set:

| Deduplicator (switch case) | Generation | Cache sets loaded | Set-2 lambda | Confidence |
|---|---|---|---|---|
| `pf_deduplicator` (case 1) | pufferfish (default) | `kRouteCacheSet` only | — (base via `RouteCacheDeduplicator::Create`) | HIGH |
| `vf_deduplicator` (case 2) | viperfish | `kRouteCacheSet` + `kViperfishRouteCacheSet` | `RouteCacheDeduplicator::CreateResilientViperfish` | HIGH |
| `gf_deduplicator` (case 4) | `6acc60406` / TPU7x | `kRouteCacheSet` + `k6acc60406RouteCacheSet` | inlined sub @`0x1fbe4140` (not named in symtab) | HIGH |

`kRouteCacheSet` is the shared base set every generation loads; the generation-specific set is layered on top of it for viperfish and the `6acc60406` generation only. Of the three sets, only `kRouteCacheSet` @`0x21f57380` and `kViperfishRouteCacheSet` @`0x21f57440` resolve as independent `nm` data symbols; `k6acc60406RouteCacheSet` is recovered from the decompile's CHECK strings (it is folded behind `GetCacheDeduplicator`).

Older generations (jellyfish / dragonfish) have **no** resilient cache set of their own — on those, a faulty link cannot be routed around: the fault-symmetry gate fails and bring-up fails or reshapes (see [Route-Table Generation](route-table-generation.md) for the gate and the chip-isolation path). The embedded descriptive inventory keys caches as `route_cache_symmetry_4_4_4_fault_num_{1,2,4}_fault_dim_{x,y,z}_<XxYxZ>[_twisted].binarypb` — full inventory and the cache-key dimensions are in [Route-Cache Dedup](route-cache-dedup.md) and [Toroidal Route Cache](toroidal-route-cache.md).

### 3.2 Chip isolation — when no route exists

The decision can fail outright. There is no "isolate one chip and continue"; the slice is removed. Three failure modes all surface as a `util::Status` error:

| Failure | Cause | Surfaced as |
|---|---|---|
| Broken super-pod symmetry | fault set not on a clean periodic lattice | `MakeErrorImpl<3>` ("ICI resiliency only supports … super-pod fault symmetry", `.rodata` @`0x8583f60`) |
| Gate failure | slice size not a multiple of the fault symmetry in some dim | `MakeErrorImpl<3>` ("The topology size must be a multiple of the fault symmetry", `.rodata` @`0x84b314b`) |
| No path for some (src,dst) | generator/cache found no fault-free route | "No route solution for topology %s." (`.rodata` @`0xa02af1b`) |

The fault-symmetry gate and the periodic-fault lattice that underpin all three are derived in [Route-Table Generation](route-table-generation.md).

---

## 4. How routing feeds the collective emitters

Routing produces the per-link auto-route table at bring-up; the collective emitters then ride it two ways. Both consumers are the `net_router` runtime.

### 4.1 Auto-route — the installed per-link table

For the auto-routing path, the `StaticPath` direction sequence per (src,dst) is compiled into the per-chip `PerLinksRoutingTable`: each hop's `Direction` becomes an `output_link_index` (0..3 SerDes port) plus a `next_hop_chip_id`. At DMA time the descriptor carries only the destination chip id and core coords; the on-chip routing engine indexes the installed table to auto-route 1..N hops. For the limited-ICI fast path the (src,dst) pair maps to a routing-table index via `net_util::MapSrcDstCoreToRoutingTableIndex` — that index *is* the `RouteScheme` index in the cache. The descriptor routing field and the table row layout are in [Unicast Route Emission](unicast-route-emission.md), [NF Descriptor](nf-descriptor.md), and [ICR Node-Fabric DMA](icr-node-fabric-dma.md).

### 4.2 Explicit schedule — the net_router route program

Limited-ICI-routing collectives (AllGather, AllToAll, CollectivePermute) compile an **explicit** per-step DMA program independent of the auto-route table. `CreateRoutingScheduleLiteral` @`0x13822400` builds it as an `s32[N]` `xla::Literal`:

```text
N = ChipBounds.X · ChipBounds.Y · num_steps · 4 + 4         (·4 = 4 ICI directions; +4 = trailing pad record)
record(core_id, step) = core_id · num_steps + step          (core_id = TpuCoreLocation::Id, row-major)
literal[record·4 + d]  = SerializeAction(actions_for_core[core_id][d])   (d ∈ {0,1,2,3} = kAllDirections)

each int32 element (SerializeAction @0x13829300):
  bits 0..12   src_ptr.index   (13b, kActionAddressIndexSize)
  bits 13..14  src_ptr.type    (2b, PointerType)
  bits 15..27  dst_ptr.index   (13b)
  bits 28..29  dst_ptr.type    (2b)
  bit  30      active marker    (+0x40000000; element == 0 ⇒ no DMA on this core/step/port)
```

The three direct producers, confirmed by the full-text caller xref and the decompile, are `CollectivePermuteEmitter::GenerateConstants`, `AllGatherEmitter::GenerateConstants`, and `AllToAllEmitterBase::CreateAllToAllRoutingScheduleTable`. The runtime replay is `EmitRoutingCode` @`0x13819ca0`: per core it computes its base record index (`GetLimitedIciRoutingTableIndex`), reads the 4 direction columns (`GetRoutingTableElement` ×4), and issues `RoutingTableStartDma` / `WaitForDmaInFlight` / `StartPrefetchIfNeeded` around per-step sync flags. Full byte-exact derivation in [Create Routing Schedule](create-routing-schedule.md) and [Net-Router Pipeline](net-router-pipeline.md).

> **GOTCHA —** AllReduce is **not** a direct `CreateRoutingScheduleLiteral` caller: the full-text xref finds only AllGather, AllToAll, and CollectivePermute. AllReduce reaches the per-step program through `EmitRoutingCode`'s direct `CreateRoutingSchedule` @`0x13819d53` (the runtime, non-literal path), not the compiled Type-5 literal. The Type-5 literal is therefore AG/A2A/CP only. Confidence: HIGH.

> **QUIRK —** the schedule literal and the route table answer different questions and a reimplementer must build both. The literal's element encodes *(src_ptr, dst_ptr)* — the buffer pointers a core DMAs between at one step on one port; it says nothing about *which physical chips the bytes traverse*. That multi-hop link path comes from the route table of §1–§3. Confusing the two yields a program that knows what to send but not where to route it.

---

## Related Components

| Component | Relationship |
|---|---|
| [Collectives](../collectives/overview.md) | The consumer — stages 1–2 (replica groups → torus mesh) feed routing; collectives ride the route table/schedule |
| [Twisted Torus](../twist/overview.md) | `TwistedTorusTopology::InitRouteSolution` @`0x20b3f7c0` — the twisted-shape route family; twist adjusts `GetDistances` |
| [Barriers](../barrier/overview.md) | Per-step sync flags (`AllocateScopedSflag`) sequence the schedule's DMAs; barrier InfoTables share the SMEM read path |
| [ICI fabric](../ici/overview.md) | The DMA layer routing ultimately drives; descriptor routing field resolved by the installed per-link table |

## Cross-References

**Route table — generation and the resilient algorithm**
- [Route-Table Generation](route-table-generation.md) — the resilient generator entry, the fault-symmetry gate, the periodic-fault lattice, chip isolation
- [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md) — minimal-DOR-first + wild-first detour, randomized vs non-randomized policy
- [Get Static Path](get-static-path.md) — `GetStaticPath` (singular) vs `GetStaticPaths` (plural) accessors
- [Get Distances](get-distances.md) — `GetDistances` torus-reduced signed distance vector (twist-adjusted)

**Route table — the cache**
- [Toroidal Route Cache](toroidal-route-cache.md) — `ToroidalRouteCacheConfigs`/`RouteScheme` schema, `ToroidalRouteCache::Create` cache key
- [Route-Cache Codec](route-cache-codec.md) — packed direction-code encoding (`element_size_in_bits`)
- [Route-Cache Decompress](route-cache-decompress.md) — `Decompress(CompressedToroidalRouteCache)` inflate
- [Route-Cache Dedup](route-cache-dedup.md) — `RouteCacheDeduplicator::Find` and the cache-key dimensions, the embedded inventory

**Route schedule and runtime**
- [Create Routing Schedule](create-routing-schedule.md) — `CreateRoutingScheduleLiteral` / `SerializeAction`, the per-step DMA program
- [Net-Router Pipeline](net-router-pipeline.md) — `EmitRoutingCode` replay, per-core base index, 4-direction read
- [Unicast Route Emission](unicast-route-emission.md) — `Direction[]` → `PerLinksRoutingTable` row, routing-table index
- [NF Descriptor](nf-descriptor.md) — the DMA descriptor routing field the table resolves
- [ICR Node-Fabric DMA](icr-node-fabric-dma.md) — the node-fabric DMA the per-step schedule drives

**Sibling sections**
- [Collectives](../collectives/overview.md) · [Twisted Torus](../twist/overview.md) · [Barriers](../barrier/overview.md) · [ICI fabric](../ici/overview.md)
- [back to index](../index.md)
