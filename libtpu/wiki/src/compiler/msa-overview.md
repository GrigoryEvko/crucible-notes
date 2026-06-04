# MSA Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

Memory-Space Assignment (**MSA**) is the compile-time pass that decides, for every `HloValue` across its live range, which physical memory tier it lives in — `kDefault` (HBM, the abundant tier) versus `kAlternate` (the scarce on-chip tier: VMEM on a TensorCore, CMEM where present) — and then *inserts the async copies* that move buffers between tiers at the right logical times. It is XLA's analogue of register allocation, except the "registers" are tens of megabytes of VMEM, the "spills" are HBM↔VMEM DMAs, and the cost of a wrong decision is paid in HBM bandwidth, not register pressure. MSA is Phase 7 of the [TPU compile pipeline](compile-phases.md#phases-710--msa-scheduling-bundle-packing-serialization): it runs on a fully-laid-out, fused HLO module after [layout assignment](layout-assignment.md) and before scheduling, because the scheduler must price spill/refill in cycles against memory spaces MSA has already chosen.

The reader who knows LLVM should hold two analogies and then watch them break. MSA is a *coloring* problem over a `GlobalDecreasingSizeBestFitHeap<HloValue>` — buffers ranked by a memory-boundedness score, placed largest-and-hottest first into a single best-fit heap with a multi-space twist. That is the greedy path, and it is what runs on the hot path for almost every module. The second analogy is the "ILP variant" that upstream XLA literature describes: a mixed-integer program over (value, space, time) placement booleans. **That solver is not in this binary.** What libtpu ships under the ILP banner is `MemoryBoundLoopOptimizer` (MBLO) — a per-loop sub-pass that unrolls *two adjacent steady-state iterations* of a software-pipelined loop and solves their coupled placement with a greedy-with-even/odd-coupling heap. No `operations_research::CpSolver`, `MPSolver`, `glop`, or CP-SAT call is reachable from any MSA symbol; the OR-Tools that is linked into libtpu serves SparseCore and the autotuner, never `MsaAlgorithm`.

This is the orientation page for the MSA sub-cluster. It fixes the role of the pass, names the jellyfish driver that builds the OSS `Options` and gates the pass per chip generation, lays out the `Run → Finish` control flow at the level a reimplementer needs to see the *shape*, frames the greedy-vs-MBLO split precisely (and why the "ILP" is loop-local, not module-global), and maps the rest of the sub-cluster. It does **not** reproduce the per-buffer `AllocateSegment` cascade, the per-generation numeric defaults, or the HBM/reservation policy — each owns its own page, cross-referenced below.

For reimplementation, the orientation contract is:

- **The memory-space vocabulary and the abstract HBM/VMEM split.** What `kDefault`/`kAlternate` mean physically, and why MSA's whole job is the *scarce* tier.
- **The jellyfish driver and its gating.** `RunMemorySpaceAssignment` builds the OSS `Options` from the per-version `TpuCompilationEnvironment` and only runs the pass when `IsMemorySpaceAssignmentEnabled` says the chip generation has the tier.
- **The greedy core: `Run → FindAllocationSequence → MsaAlgorithm::Finish`.** A best-fit heap ranked by memory-boundedness, with cross-program prefetch and scoped reservations carved out first.
- **The ILP-vs-greedy split.** MBLO is greedy-with-coupling over a two-iteration window, *not* a classical MILP; the only true "solver" surface is the offline IOR autotuner whose result is replayed as a packed sort-order permutation.

| | |
|---|---|
| **Jellyfish driver** | `xla::jellyfish::RunMemorySpaceAssignment` @ `0x12fc3080` (Phase 7 entry) |
| **Per-version gate** | `xla::jellyfish::IsMemorySpaceAssignmentEnabled` @ `0x12fc1280` |
| **Options builder** | `xla::jellyfish::ComputeMemorySpaceAssignmentOptions` @ `0x12fc1440` (55 per-family overrides) |
| **OSS pass driver** | `MemorySpaceAssignment::Run` @ `0x1dc2e200` (vtable `0x21d1b5d8`) |
| **HeapSimulator entry** | `MsaAlgorithm::Finish` @ `0x1dc5b560` (vtable `0x21d1b8f0`) |
| **Heap base class** | `xla::GlobalDecreasingSizeBestFitHeap<xla::HloValue>` (multi-space best-fit) |
| **"ILP" sub-pass** | `MemoryBoundLoopOptimizer`; driven from `MsaAlgorithm::IdentifyAndOptimizeMemoryBoundLoops` `0x1dc4b520`, runs `MemoryBoundLoopOptimizer::Optimize` `0x1dcb9760` (496-byte object, `operator new(0x1f0)`) |
| **Even/odd heap** | `LoopOptimizerBestFitHeap::FindEvenAndOddAllocationBetween` @ `0x1dcb5580` |
| **External solver** | **None on the MSA hot path** — no CpSolver/MPSolver/glop reachable from any MSA symbol |
| **IR level** | XLA HLO (`HloModule`/`HloInstruction`), HLO logical time |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## What MSA Decides

### Purpose

MSA assigns each `HloValue` a memory space for each segment of its live range, and inserts the instructions that realize those assignments. The two spaces that matter to the algorithm are abstract:

- **`kDefault`** — the abundant tier, physically HBM. Every buffer fits here; placing a value in `kDefault` is never a capacity failure. MSA's terminal fallback is "leave it in HBM."
- **`kAlternate`** — the scarce on-chip tier, physically VMEM on the TensorCore (and CMEM on generations that have it). This is the tier MSA actually rations. A value benefits from `kAlternate` when an op that reads it is HBM-bandwidth-bound, so keeping the operand on-chip removes a bandwidth stall.

The vocabulary the binary's verifier accepts is wider than this two-way split — `kHbm`, `kPinnedHbm`, `kVmem`, `kSmem`, `kCmem`, `kSflag`, `kHost`, plus several SparseCore- and BarnaCore-private spaces — but the core decision is always "scarce tier or not." `kPinnedHbm` is a distinct space from `kHbm` for buffers the runtime must lock (peer-DMA inputs); the repacker may never relocate them. `kHost` is the host-RAM spill target, reached through host-offload custom calls rather than the heap. The detailed per-space capacity and constraint matrix lives on [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md).

> **NOTE —** "Alternate" is the algorithm's name for the scarce tier; "VMEM" / "CMEM" are the physical realizations. The greedy heap never sees "VMEM" — it sees a single `[0, max_size_in_bytes)` byte range that the [jellyfish driver](#the-jellyfish-driver) sized from the chip's VMEM word count. On the latest TensorCore generation (top jump-table entry, `tpu_version == 5`) that range is 64 MiB per TensorCore, 512-byte word-aligned; CMEM is absent (`CmemSize == 0`), so the CMEM tier is dormant. See [msa-per-version-defaults.md](msa-per-version-defaults.md).

### The two outputs

The decision produces an `Allocation` object per (value, segment), each carrying an `xla::HeapSimulator::Chunk{offset, size}` (or none, for pure async pieces):

```text
PinnedAllocation        value stays resident in one space for the segment
CopyAllocation          async copy in/out  (kCopyStart … kCopyDone)
SlicedCopyAllocation    multi-slice async copy (latency split across windows)
WindowPrefetchedAllocation   tile-by-tile streaming read into VMEM during the op
ScopedAllocation / ReservedAllocation   bounded / pre-reserved alt-mem
```

The chunks are later lowered (in `MemorySpaceAssignment::Process`) into `ProgramMemoryMetadata` proto entries embedded in the compiled program; at load time the runtime's `tpu::BestFitAllocator` rehydrates the same offsets deterministically (see [memory/hbm-allocator.md](../memory/hbm-allocator.md) and [memory/on-device-compaction.md](../memory/on-device-compaction.md)). MSA is a *static planner*: every offset it picks is frozen into the executable.

---

## The Jellyfish Driver

### Purpose

`xla::jellyfish::RunMemorySpaceAssignment` (`0x12fc3080`) is the TPU adaptor between the per-chip compilation environment and the OSS XLA pass. It is the symbol [compile-phases.md](compile-phases.md#phases-710--msa-scheduling-bundle-packing-serialization) names as Phase 7. Its job is twofold: decide *whether* MSA runs on this chip generation, and *build* the OSS `Options` struct from per-family flags before delegating to the OSS driver. The decompiled body confirms it calls `IsMemorySpaceAssignmentEnabled` for the gate and then the OSS `MemorySpaceAssignment::Run`; no external solver appears anywhere in its call graph.

### Entry Point

```text
xla::jellyfish::RunMemorySpaceAssignment            0x12fc3080
  ├─ IsMemorySpaceAssignmentEnabled(Target, env, module)  0x12fc1280   ── per-version gate
  │     reads Target+0x398 (tpu_version); jump table @ 0xae09ac8 (6 entries)
  ├─ ComputeMemorySpaceAssignmentOptions(...)            0x12fc1440   ── 55 OverwriteFieldIfNotDefault
  │     each option: per-family flag (vf/jf/gf/tpu_cmem) with xla_msa_ global fallback
  └─ MemorySpaceAssignment::Run(module, live_range,            0x1dc2e200  ── OSS pass driver
                                alias_analysis, alias_info, options)
```

### Algorithm

```c
// xla::jellyfish::RunMemorySpaceAssignment  (0x12fc3080)
Status RunMemorySpaceAssignment(MsaOptions opts, Target target,
                                AliasInfo* alias_info, HloModule* module):
    // 1) Gate by chip generation.  AUTO -> enabled iff tpu_version >= 2
    //    (Pufferfish-class and later); per-family flag can override.
    if (!IsMemorySpaceAssignmentEnabled(target, env, module))      // 0x12fc1280
        return OkStatus();          // MSA simply does not run on this chip

    // 2) Materialise the OSS Options from the per-version environment.
    //    55 fields, each read from a per-family flag (xla_{vf,jf,gf}_vmem_*
    //    / xla_tpu_cmem_*) with an xla_msa_ global fallback.  This sizes
    //    max_size_in_bytes from the chip's VMEM word count, sets the overlap
    //    ratios, outstanding-copy caps, repack/retry limits, MBLO options.
    Options options = ComputeMemorySpaceAssignmentOptions(target,    // 0x12fc1440
                                                          alias_info, *module);

    // 3) Delegate to the unmodified OSS XLA pass driver.
    return MemorySpaceAssignment::Run(module, live_range,            // 0x1dc2e200
                                      alias_analysis, alias_info, options);
```

> **QUIRK —** the per-version gate maps internal `TpuVersion` 0 and 1 both to the Jellyfish flag, and the AUTO rule "`tpu_version >= 2`" disables MSA for those lowest two. MSA is effectively on from Pufferfish-class generations up. The two newest generations (`tpu_version` 4 and 5) *both* route to the Ghostlite `gf` flag namespace — the jump table at `0xae09ac8` sends both indices to the same family. A reimplementer who assumes exactly one flag family per generation will mis-gate the newest chip. The exact version→flag table is on [msa-per-version-defaults.md](msa-per-version-defaults.md).

### Function Map

| Function | Address | Role |
|---|---|---|
| `RunMemorySpaceAssignment` | `0x12fc3080` | Phase 7 entry; gate + options build + delegate |
| `IsMemorySpaceAssignmentEnabled` | `0x12fc1280` | Per-version gate; `tpu_version` at `Target+0x398`, jump table `0xae09ac8` |
| `ComputeMemorySpaceAssignmentOptions` | `0x12fc1440` | 55 `OverwriteFieldIfNotDefault` per-family overrides |
| `OverwriteFieldIfNotDefault` | `0x1d73f360` | Single per-family flag → option-field writer |
| `MemorySpaceAssignmentLambdaCollection::IsAsyncSliceImplemented` | `0x12fd64a0` | TPU cost/aliasing/slice callbacks handed to OSS |

---

## The Greedy Core — Run to Finish

### Purpose

`MemorySpaceAssignment::Run` (`0x1dc2e200`) is the OSS pass driver; it builds the heap algorithm, runs it through `HeapSimulator`, then rewrites the module with the chosen allocations. The actual placement logic lives in `MsaAlgorithm`, a subclass of `GlobalDecreasingSizeBestFitHeap<HloValue>`, whose `Finish()` (`0x1dc5b560`) is what `HeapSimulator` dispatches to. This is the heart of MSA, and it is a **greedy best-fit heap**, not a solver.

### Entry Point

```text
MemorySpaceAssignment::Run(module, live_range, alias_analysis, ...)   0x1dc2e200
  ├─ FindAllocationSequence(live_range, alias_analysis)              0x1dc3b060
  │     ├─ builds the MsaAlgorithm instance
  │     ├─ HeapSimulator::Run(MsaAlgorithm-as-HeapAlgorithm, ...)
  │     │       └─ MsaAlgorithm::Finish()                            0x1dc5b560   ── all placement here
  │     └─ pulls out the chosen allocations_ vector
  └─ Process(live_range, alias_analysis)                            0x1dc2f5e0
        ├─ SimplifyGraph()              cleans tuple/GTE chains       0x1dc334c0
        ├─ SetSchedule()                inserts async copy start/done 0x1dc356a0
        ├─ ExportAndColorBuffers()      writes kAlternate/kDefault    0x1dc37820
        └─ ScheduleAsynchronousCopies() orders copy starts            0x1dc327e0
```

### Algorithm

`Finish()` carves out the special tiers first (cross-program prefetch, scoped reservations, colored buffers), then optionally runs the loop optimizer, then walks the remaining buffer intervals in memory-boundedness-sorted order, building an `AllocationRequest` per (value, use) and calling `AllocateSegment`:

```c
// MsaAlgorithm::Finish  (0x1dc5b560) — greedy best-fit core
Status MsaAlgorithm::Finish():
    // 1) Reserve cross-program-prefetch budget at the bottom of alt-mem,
    //    and (if xla_tpu_msa_reduce_scoped_vmem_limit) the scoped reservation.
    long alt_mem_top = ReserveAlternateMemoryForScopedMemoryAllocations();  // 0x1dc5b460

    // 2) Place already-decided block prefetches first (cross-program +
    //    preset assignments inherited from a prior compile or IOR replay).
    AllocateAndScheduleExistingBlockPrefetches(alt_mem_top);                // 0x1dc52960
    CreateNewBlockPrefetches(alt_mem_top);                                  // 0x1dc57660

    AllocateReservedScopedAllocations();                                    // 0x1dc62280
    ProcessColoredBuffers();      // frontend memory_space=kAlternate pins   // 0x1dc50d40

    // 3) Rank every HloValue by the memory-boundedness comparator.
    SortedBufferIntervals sorted =
        GetSortedBufferIntervals();   // MemoryBoundednessBufferIntervalComparator (0x1dcd3100)

    // 4) THE "ILP" SUB-PASS — only if MBLO is enabled for this chip.
    if (options_.memory_bound_loop_optimizer_options.enabled())
        IdentifyAndOptimizeMemoryBoundLoops();                              // 0x1dc4b520

    // 5) Greedy walk: each interval gets an AllocationRequest, then a
    //    per-buffer placement attempt.
    for (BufferInterval* interval : sorted_no_loops) {
        auto values = CreateAllocationValuesFromColocatedIntervals({interval});  // 0x1dc6fc00
        AllocateAllocationValues({values});                                // 0x1dc65600
        //   dispatches each use to AllocateSegment / Prefetch / WindowPrefetch
        //   AllocateSegment (0x1dc73ca0) is the 6-stage cascade — see
        //   msa-allocate-segment.md
    }
    FinalizeAllocations({values_});                                        // 0x1dc6ae80
    return OkStatus();
```

The objective is **implicit**, not written into any solver. It lives in three places: the sort key (`MemoryBoundednessBufferIntervalComparator` at `0x1dcd3100`, ranking by `memory_boundedness_score × size` so the hottest largest buffers go first), the per-prefetch cost in the interval picker (schedule the copy when HBM bandwidth is otherwise idle), and MBLO's `CalculateExecutionTime` (a wall-clock estimate over one loop period). No `lambda`/`mu` weighting constants survive in the disassembly — they are baked into `CostAnalysis`'s benefit and bandwidth-idle computations.

### Function Map

| Function | Address | Role |
|---|---|---|
| `MemorySpaceAssignment::Run` | `0x1dc2e200` | OSS pass driver: FindAllocationSequence + Process |
| `MemorySpaceAssignment::Process` | `0x1dc2f5e0` | Rewrites module with chosen allocations |
| `MsaAlgorithm::Finish` | `0x1dc5b560` | Greedy best-fit core (HeapSimulator entry) |
| `MsaAlgorithm::AllocateAllocationValues` | `0x1dc65600` | Per-value use dispatch |
| `MsaAlgorithm::AllocateSegment` | `0x1dc73ca0` | 6-stage per-buffer cascade — see [msa-allocate-segment.md](msa-allocate-segment.md) |
| `MemoryBoundednessBufferIntervalComparator` ctor | `0x1dcd3100` | The sort key (memory-boundedness × size) |
| `GlobalDecreasingSizeBestFitHeap::GetSortedBufferIntervals` | `0x1e48f400` | Rank-orders intervals for the greedy walk |

---

## The "ILP" Sub-Pass — MemoryBoundLoopOptimizer

### Purpose

`MemoryBoundLoopOptimizer` (MBLO) is the closest thing libtpu has to the "ILP variant" of MSA, and it is the single most misnamed part of the pass. It is **not** a mixed-integer program. It is a per-loop greedy allocator that models the *steady state of a software-pipelined loop* by unrolling two adjacent iterations and solving their coupled buffer placement on a private even/odd best-fit heap. It is gated by `options_.memory_bound_loop_optimizer_options.enabled()` and invoked from `MsaAlgorithm::IdentifyAndOptimizeMemoryBoundLoops` (`0x1dc4b520`) before the outer greedy walk.

### Why it is "ILP-shaped" but not ILP

A classical XLA-MSA ILP introduces a Boolean placement variable per (value, space, time) and hands the constraint system to a CP-SAT or MILP solver. The binary has **no such variable and no such solver call.** Verified across every MSA decompile file: no `operations_research::CpSolver`, `CpModelBuilder`, `MPSolver`, `LinearExpr`, `glop::LinearProgram`, or `math_opt::*` is reachable from `MsaAlgorithm`, `MemoryBoundLoopOptimizer`, `LoopOptimizerBestFitHeap`, or `MemorySpaceAssignment::Run`. The ~80 OR-Tools/CP-SAT symbols present in libtpu serve SparseCore minibatch packing, gRPC routing, and the autotuner — not MSA.

What MBLO has *instead* of an ILP is one genuine coupling constraint that gives it an ILP-like flavor: **the even/odd chunk pair**. When a prefetch crosses the iteration boundary (its copy starts in the previous period and completes in the current one), the same buffer must occupy a chunk in the even-iteration heap *and* a different chunk in the odd-iteration heap, and both must coexist with everything else alive in the steady state. `LoopOptimizerBestFitHeap::FindEvenAndOddAllocationBetween` (`0x1dcb5580`) returns the `(even, odd)` chunk pair atomically or rolls both back. That is the constraint; the rest is greedy.

### Entry Point

```text
MsaAlgorithm::IdentifyAndOptimizeMemoryBoundLoops          0x1dc4b520
  └─ per kWhile candidate (opcode 0x40):
     MsaAlgorithm::OptimizeMemoryBoundLoop(start, end, size) 0x1dc4a140
       └─ MemoryBoundLoopOptimizer::Create(...)            0x1dcb5c40   ── operator new(0x1f0) = 496 B
            ├─ Initialize()                                0x1dcb5d60   ── classify LoopValues
            └─ Optimize()                                  0x1dcb9760
                 = SortLoopValues + AllocateLoopValues + CalculateExecutionTime
```

### Algorithm

```c
// MemoryBoundLoopOptimizer::Optimize  (0x1dcb9760)
void Optimize():
    SortLoopValues();                  // stable_sort by bandwidth score (0x1dcca880)
    AllocateLoopValues();              // 0x1dcb9840 — 5-case allocation_type dispatch
    float t = CalculateExecutionTime(); // 0x1dcbb9a0 — wall-clock estimate over one period

// AllocateLoopValues  (0x1dcb9840) — dispatch by LoopValue.allocation_type
void AllocateLoopValues():
    for (LoopValue& v : loop_values_):
        CHECK(v.allocation_type != kUnsupported);     // log-check; filtered earlier
        switch (v.allocation_type):                   // cmp 4; jmp [table]
            case kTemporary: AllocateTemporary(v);  break;  // 0x1dcbdb40
            case kPinned:    AllocatePinned(v);     break;  // 0x1dcbe180
            case kPrefetch:  collect for batch;     break;  // kLoopCarriedDependency folds into kPinned
    AllocatePrefetches(prefetch_subset);              // 0x1dcbe580 — score-sorted batch
        // per prefetch: AllocatePrefetch (0x1dcc0160) ->
        //   LoopOptimizerBestFitHeap::FindEvenAndOddAllocationBetween (0x1dcb5580)
        //     CreateEvenAllocationBlock (0x1dcb5280) + CreateOddAllocationBlock (0x1dcb5400)
        //     FindAndCommitChunkCandidate(even) ; FindChunkCandidate(odd)
        //     if !disjoint(even, odd): RemoveEvenChunks + RemoveOddChunks (rollback)
```

When `AllocatePrefetch` fails (no disjoint even/odd pair fits), the buffer is simply dropped from MBLO's plan; the outer greedy walk picks it up on the next sort iteration as an ordinary interval. The fallback is implicit — MBLO returns a partial schedule and `MsaAlgorithm` finishes the rest.

> **GOTCHA —** MBLO's window is exactly *two* loop periods (`outer_start`, `outer_start + loop_size`, `outer_start + 2·loop_size`), and a prefetch start index may be **negative** (`begin_idx_in_loop ∈ [-loop_size, +loop_size]`), meaning "starts in the previous period." A reimplementer who models the loop body as a single iteration will fail to allocate any cross-boundary prefetch — and those are the whole point, because they are how the steady state hides HBM latency under compute. The two heaps (even, odd) exist precisely so adjacent iterations' copies of the same buffer never alias.

### Function Map

| Function | Address | Role |
|---|---|---|
| `IdentifyAndOptimizeMemoryBoundLoops` | `0x1dc4b520` | Scans `kWhile` candidates, drives MBLO |
| `OptimizeMemoryBoundLoop` | `0x1dc4a140` | Per-loop wrapper; creates + applies MBLO |
| `MemoryBoundLoopOptimizer::Create` | `0x1dcb5c40` | `operator new(0x1f0)` + ctor + Initialize |
| `MemoryBoundLoopOptimizer::Optimize` | `0x1dcb9760` | SortLoopValues + AllocateLoopValues + cost |
| `AllocateLoopValues` | `0x1dcb9840` | 5-case `allocation_type` jump table |
| `AllocatePrefetch` | `0x1dcc0160` | Even/odd coupling per prefetch |
| `LoopOptimizerBestFitHeap::FindEvenAndOddAllocationBetween` | `0x1dcb5580` | Atomic `(even, odd)` chunk-pair allocation |
| `CalculateExecutionTime` | `0x1dcbb9a0` | Wall-clock estimate (one period + async-copy wait) |

> **NOTE —** the exact `LoopValue.allocation_type` *classifier* (when a buffer is kTemporary vs kPinned vs kPrefetch) lives inside `MemoryBoundLoopOptimizer::Initialize` (`0x1dcb5d60`, ~3 kB) and was not fully traced; only the dispatch table that consumes the classification is decoded. Treat the classifier boundary as **LOW** confidence.

---

## The Only True Solver — The IOR Autotuner Replay

The only place an actual optimal solver enters MSA is **out of process**. The experimental IOR ("Integer-Optimal-Repacker"-style autotuner, flag `xla_msa_experimental_ior_algorithm`) does not solve anything inside libtpu. It loads a precomputed solution from disk (`xla_ior_stored_solution_path`) and replays it. The replay surface is a single proto:

```text
message MemorySpaceAssignmentConfig {   // descriptor @ 0xbed7cdc
  repeated uint64 order = 1 [packed = true];   // a permutation of buffer-interval ranks
}
```

The "stored solution" is therefore just a **packed permutation of buffer-interval indices** — a sort order. The offline solver's output is a ranking, fed back into the same greedy best-fit heap as a pre-sorted order. The in-process MSA stays a greedy heap regardless. Flags `xla_ior_use_stored_solution`, `xla_ior_fast_mem_run_production_msa`, and `xla_ior_fast_mem_round_trip_production_msa` control loading and post-replay validation. The flags `xla_msa_experimental_use_telamalloc`, `xla_tpu_msa_use_minimalloc`, `xla_tpu_msa_use_tinymalloc` select ILP-style repackers that are **not statically linked** in libtpu — they are inert toggles kept for compatibility with internal Google builds.

---

## Map of the MSA Sub-Cluster

| Page | Owns | Key anchors |
|---|---|---|
| **msa-overview.md** *(this page)* | Role, jellyfish driver, greedy `Finish`, ILP-vs-greedy framing | `0x12fc3080`, `0x1dc5b560`, `0x1dcb9760` |
| [msa-allocate-segment.md](msa-allocate-segment.md) | The 6-stage per-buffer cascade (required-assign → pin → no-copy → prefetch → evict → default); `AllocationRequest` (~0x210 B) layout; `AllocationResult` 11-bit failure bitmask | `0x1dc73ca0`, `0x1dc72820`, `0x1dc7bc80` |
| [msa-per-version-defaults.md](msa-per-version-defaults.md) | Version→flag gating matrix (`tpu_version` 4 and 5 share `gf`), the 5-variant per-family override scheme, overlap ratios / outstanding-copy caps per generation, the 64-MiB VMEM / 512-B-word budget on the newest generation | `0x12fc1280`, `0x12fc1440`, table `0xae09ac8` |
| [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md) | Per-memory-space capacity & constraint matrix, scoped + cross-program-prefetch reservations, `kPinnedHbm`/`kHost` policy, the HBM-allocator handoff | `0x1dc5b460`, `0x1dc62280` |

The prefetch interval picker (`CostAnalysisPrefetchIntervalPicker`, ctor `0x1dcd6b60`; `Begin` `0x1dcd7a00` / `Next` `0x1dcd7e20`), the async-copy time-bucket resource model (`AsynchronousCopyResource::HasEnoughResource` `0x1dc78d40`), and the cost model (`CostAnalysis::Create` `0x1dceafc0`) are consulted by `AllocateSegment` and documented on [msa-allocate-segment.md](msa-allocate-segment.md).

---

## Related Components

| Component | Relationship |
|---|---|
| [layout-assignment.md](layout-assignment.md) | Phase 6; fixes physical tile layouts MSA's sizes depend on; runs immediately before MSA |
| [compile-phases.md](compile-phases.md) | Owns the Phase 7 placement and the full ordered phase spine |
| [memory/vmem-allocator.md](../memory/vmem-allocator.md) | The runtime VMEM tier MSA's `kAlternate` chunks land in |
| [memory/hbm-allocator.md](../memory/hbm-allocator.md) | The runtime HBM allocator that rehydrates MSA's static offsets |
| [sched/overview.md](../sched/overview.md) | Phase 8 scheduler; prices spill/refill against the spaces MSA assigned |
| [sched/lhs-ilp-variant.md](../sched/lhs-ilp-variant.md) | A *different* "ILP variant" — the latency-hiding scheduler's, not MSA's |

## Cross-References

- [msa-allocate-segment.md](msa-allocate-segment.md) — the per-buffer placement cascade and request/result layout that `Finish` drives
- [msa-per-version-defaults.md](msa-per-version-defaults.md) — the gating matrix and numeric defaults the jellyfish driver reads
- [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md) — per-space capacity, reservations, and the HBM allocator handoff
- [compile-phases.md](compile-phases.md) — Phase 7 in the ordered TPU compile spine
- [layout-assignment.md](layout-assignment.md) — Phase 6, the predecessor whose layouts size MSA's buffers
- [overview.md](overview.md) — Part V orientation; MSA as XLA's HBM/VMEM register allocator
- [sched/overview.md](../sched/overview.md) — Phase 8, the consumer of MSA's memory-space decisions
- [memory/hbm-allocator.md](../memory/hbm-allocator.md) — runtime allocator that rehydrates MSA's frozen offsets
- [memory/vmem-allocator.md](../memory/vmem-allocator.md) — the physical `kAlternate` tier
