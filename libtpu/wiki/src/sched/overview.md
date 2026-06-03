# TPU Scheduling Pipeline

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

TPU code is scheduled **twice, at two different IR levels, by two different algorithms**, and the second one depends on a separate resource-assignment pass that has no analogue in a CPU or GPU backend. This page is the map of that stack. The first scheduler is the HLO-level `LatencyHidingScheduler` (LHS) — a greedy, bottom-up, critical-path list scheduler that reorders an already-memory-minimized HLO sequence so long-latency *async* work (collective starts, host/ICI DMA, async copies) is issued early and hidden under independent compute. The second is the LLO-level bundle packer — a forward greedy "earliest-legal-bundle" list scheduler that assigns each low-level op to a slot inside a fixed-width VLIW bundle, plus a separate modulo scheduler that software-pipelines inner hardware loops. Between the two sits the MXU-sequence assignment and MRB (matrix-result-buffer) allocation pass, which decides matmul accumulation chains and result-FIFO placement that the bundle packer's latch ordering then depends on. Pricing every one of these decisions is the `ResourceType` model and `AsyncTracker` from Part VII's cost model.

The reader who knows LLVM should hold the analogy stage by stage. The LHS is a classic `MachineScheduler`-style ready-set list scheduler, but it builds the sequence *in reverse* (a `current_time` clock that walks back from the roots) and its priority is dominated by async critical-path depth/height rather than plain dependency height. The bundle packer is the TPU's equivalent of LLVM's `VLIWPacketizer` (`DFAPacketizer`), except the resource model is a per-generation `BundleRequirement` bitmap rather than a deterministic finite automaton, and there is no spill-to-next-bundle search — when a slot is full, a new empty bundle is appended and the op retries. The modulo scheduler is textbook iterative modulo scheduling with RecMII/ResMII initiation-interval search, riding on LLVM's `ScheduleDAGMI` over `MachineInstr`. The MXU/MRB pass has no LLVM analogue at all: it is a domain-specific bin-packer over the systolic array's accumulation buffers.

This is an orientation page. It frames the four stages, fixes the stage ordering, and links each sub-page that documents the detail. It does not duplicate the algorithms — the LHS comparator's 22 keys, the per-gen bundle slot matrix, the MRB reservation timeline, the modulo II search — each lives on its own page, cross-referenced below.

For reimplementation, the stack contract is:

- **The two schedulers operate on disjoint IRs and never share state.** The LHS reorders `HloInstruction`s and writes a `module->schedule`; the bundle packer reorders/packs `LloInstruction`s (or `MachineInstr`s) into `Bundle` records. The HLO scheduling annotations do **not** flow into bundle packing; they only steer async-overlap of HLO computations.
- **The LHS requires a base schedule as a precondition.** It does not build a schedule from scratch — `HloMemorySchedulerWithBrkgaFallback` lays down a memory-pressure-minimizing order first, and the LHS replaces it with one that overlaps async edges. The four LHS pages are *variants of one shared body*; they differ only in pipeline placement and `SchedulerConfig`/`AsyncTracker` inputs.
- **MXU-sequence assignment runs between the schedulers and feeds the packer.** `AssignMxusForSequenceGroup` decides matmul accumulation chains, latch placement, and MRB FIFO entries; the bundle packer's per-op latch ordering and the per-gen encoder's latch serialization read that assignment downstream.
- **Every scheduling decision is priced by the bundle cost model.** Node cost (`MaxResourceCycles` over a 23-slot bundle vector) and dependency latency (`LatencyBetween`) feed the LHS priority; the `ResourceType`/`AsyncTracker` model gates which async ops may overlap by physical resource.

| | |
|---|---|
| **Stage 1 — HLO scheduler** | `xla::LatencyHidingScheduler::RunImpl` @ `0x136321a0`; per-comp drain `DefaultSchedulerCore::ScheduleComputation` @ `0x1362eb60`; comparator `FindAndExtractBestNodeAvailable` @ `0x13618880` |
| **Stage 1 driver** | `(anon)::RunHloScheduler` @ `0x1096fac0` — two pipelines: `final_scheduler` (base) → `async_scheduling` (LHS) |
| **Stage 2 — MXU/MRB assignment** | `AssignMxusForSequenceGroup` @ `0x10f753c0` (worker `0x10f77ca0`); `MrbChainAllocator::ExtendMrbReservation` @ `0x10f58800`; `MxuAssigner::AllocateMrbEntriesAsFifo` @ `0x10f3ef80`, `BounceBetweenMsrs` @ `0x10f3fae0` |
| **Stage 3 — LLO bundle packer** | `PackBundles` @ `0x10a30a20`; `GlobalBundlePacker::Pack` @ `0x10a86420`; `BundlePacker::Feed` @ `0x14021f20` |
| **Stage 3b — modulo (loops)** | `llvm::TPUScheduleDAGModulo::findSchedule` @ `0x13b1d7c0`; II search `calculateResourceMII` @ `0x13c0bee0`, `calculateLargestLatencyMII` @ `0x13c0b840` |
| **Pricing — cost model** | `MaxResourceCycles` @ `0x1c89b9e0`; `LatencyBetween` @ `0x1c89f820`; `TpuAsyncTracker::GetResourceHazardType` @ `0x110015e0` |
| **IR levels** | Stage 1: HLO (`HloInstruction`) · Stages 2–3: LLO (`LloInstruction`) / LLVM `MachineInstr` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Stage Ordering

The four stages run strictly in pipeline order, each consuming the previous stage's output. The two list schedulers bookend the matrix-resource assignment; the cost model is queried by all three.

```text
HLO module (post layout-assignment, post main-fusion)
   │
   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 1 — HLO LATENCY-HIDING SCHEDULER          RunHloScheduler        │
│   final_scheduler   : HloMemorySchedulerWithBrkgaFallback (base order) │   0x1096fac0
│   async_scheduling  : LatencyHidingScheduler::RunImpl (overlap rewrite)│   0x136321a0
│     └─ DefaultSchedulerCore::ScheduleComputation (reverse list drain)  │   0x1362eb60
│          └─ FindAndExtractBestNodeAvailable (22-key ReadySetLt)        │   0x13618880
│     priced by  GetLatencyBetween → MaxResourceCycles / LatencyBetween  │
│     gated by   TpuAsyncTracker (ResourceType hazard classes)           │
└──────────────────────────────────────────────────────────────────────┘
   │  (HLO → MHLO/TLP → tpu dialect → LLO IR descent; not on this page)
   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 2 — MXU SEQUENCE + MRB ALLOCATION                                │
│   AssignMxusForSequenceGroup (bin-pack matmuls into MXU sequences)     │   0x10f753c0
│     └─ MrbChainAllocator (accumulation-chain reservation timeline)     │   0x10f58800
│     └─ AllocateMrbEntriesAsFifo / BounceBetweenMsrs (result FIFO/MSR)  │   0x10f3ef80
│     └─ SetLatchIndices (commit latch ids onto each MxuSequence)        │   0x10f3b4c0
└──────────────────────────────────────────────────────────────────────┘
   │  (latch indices + MRB entries are inputs to slot legality below)
   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STAGE 3 — LLO BUNDLE PACKING                    PackBundles            │   0x10a30a20
│   GlobalBundlePacker::Pack (forward greedy, topological)               │   0x10a86420
│     └─ BundlePacker::Feed (earliest-legal-bundle + SlotTracker)        │   0x14021f20
│     └─ per-gen TpuBundleRestrictions (slot legality, latch ordering)   │
│   STAGE 3b (inner loops only): TPUScheduleDAGModulo (II-search SWP)    │   0x13b1d7c0
└──────────────────────────────────────────────────────────────────────┘
   │
   ▼
vector<Bundle> ─→ per-gen Encoder*::EncodeBundle (latch serialization) ─→ raw bytes
```

The ordering is not arbitrary. Stage 1 runs on HLO precisely because async overlap is a *macro* decision — which collective starts before which fusion — that must be made while the program is still a high-level dataflow graph with whole-tensor dependency edges. Stage 3 runs on LLO because slot legality is a *micro* decision — which scalar/vector/MXU lane each individual op occupies in one VLIW word — that only exists after the program has been lowered to hardware ops. Stage 2 sits between them because the MXU's accumulation buffers (MRB) and result latches are a physical resource whose assignment is finer-grained than HLO but is a *precondition* for slot legality, not a consequence of it: the bundle packer needs to know which latch each matmul result lands in before it can decide whether two matmul ops conflict in a bundle.

> **GOTCHA — there are two unrelated "schedulers" and conflating them mis-prices everything.** Stage 1 (`LatencyHidingScheduler`) and Stage 3 (`BundlePacker`) are both list schedulers, but they share no code, no IR, and no cost units. Stage 1 prices in *bundle cycles* via `MaxResourceCycles` to overlap async transfers; Stage 3 prices in *slot occupancy* via per-gen `BundleRequirement` bitmaps to fit a VLIW word. A reimplementation that runs one and skips the other produces either an un-overlapped async schedule (skip Stage 1) or an unencodable instruction stream with slot conflicts (skip Stage 3).

---

## Stage 1 — HLO Latency-Hiding Scheduler

### Purpose

The LHS is the production HLO scheduler. It is invoked from `RunHloScheduler` (`0x1096fac0`), the final-scheduling phase of `DeepseaCompilerBase::RunHloPasses`, after layout assignment and main fusion. It does not build a schedule from nothing: `RunHloScheduler` first runs a `final_scheduler` pipeline whose `HloMemorySchedulerWithBrkgaFallback` lays down a memory-pressure-minimizing base order (the LHS `has_schedule()` precondition), then runs an `async_scheduling` pipeline whose `LatencyHidingScheduler` replaces that order with one that overlaps async start/done pairs against compute under a TPU resource model.

### Structure

```text
RunHloScheduler                                   0x1096fac0
  ├─ pipeline "final_scheduler"
  │     └─ HloMemorySchedulerWithBrkgaFallback     ── base memory-minimized order
  └─ pipeline "async_scheduling"
        └─ AddPass<LatencyHidingScheduler>          ── shared RunImpl 0x136321a0
              ├─ SchedulerConfig (per-variant POD)
              ├─ SchedulingContext (LatencyEstimator + TpuAsyncTracker)
              └─ DefaultSchedulerCore::ScheduleComputation  0x1362eb60
                    └─ FindAndExtractBestNodeAvailable       0x13618880
```

The single most important structural fact is that the `RunImpl` body (`0x136321a0`) is **shared across all variants**. There is exactly one compiled `RunImpl` — the unmodified upstream `latency_hiding_scheduler.cc` body. The four LHS pages below document variants that differ *only* in two inputs handed to `AddPass<LatencyHidingScheduler>`: the `SchedulerConfig` POD and the `SchedulingContext` (estimator + async tracker). The core algorithm — the reverse-direction drain loop, the 22-key `ReadySetLt` comparator, the precomputed async depth/height, the memory-pressure retry loop — is documented once on the [core page](latency-hiding-scheduler-core.md).

### Variant Map

| Variant | What it changes | Page |
|---|---|---|
| **Core** | the shared `RunImpl` body: drain loop, comparator, async tracker, memory retry | [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) |
| **post_layout** | the *live* placement in 0.0.40 — two-pipeline `RunHloScheduler`, `SchedulerConfig`/estimator/tracker wiring | [LHS post_layout](lhs-post-layout.md) |
| **post_layout_pre_fusion** | the same body wired at a dead "Pre main fusion" slot; config delta only | [LHS post_layout_pre_fusion](lhs-post-layout-pre-fusion.md) |
| **ILP** | the `EnableIlpLatencyHidingScheduler` gate swaps the async *classifier* only — the comparator and `RunImpl` are unchanged. The named `ILPMemoryScheduler` MIP is a **separate, opt-in memory scheduler** (reachable via `GetMemorySchedulerAlgorithm` case 6), not invoked by this flag | [LHS ILP variant](lhs-ilp-variant.md) |

> **CORRECTION (SCHED-1) —** the flag family `xla_tpu_enable_ilp_latency_hiding_scheduler` suggests an integer-linear-programming scheduler replaces the greedy list scheduler. It does not: the `EnableIlpLatencyHidingScheduler` path inside `RunHloScheduler` only swaps which async-op kinds are exposed as overlap candidates to the *same* greedy LHS (the comparator and `RunImpl` are unchanged). This is distinct from the genuine `xla::ILPMemoryScheduler` CP-SAT **memory** scheduler, which is a separate, reachable opt-in: `GetMemorySchedulerAlgorithm` (`0x10abd6a0`) selects it at **case 6** (`MemorySchedulerProto.Value::ILP`) of its 0..9 switch (`Default0 / List1 / DFS2 / PostOrder3 / BRKGA4 / BFS5 / ILP6 / Backtracking7 / BruteForce8 / LocalOrder9`), keyed off `TpuCompilationEnvironment+0x10c`. The MIP is on-demand (not the default), not dead. See the [ILP variant page](lhs-ilp-variant.md).

### What It Consumes

The comparator's per-node cost and per-edge latency come from the cost model: `CostModelLatencyEstimator::GetLatencyBetween` (`0x10ff8f00`) routes compute edges through `CostModel::GetCycles` → `MaxResourceCycles` (bundle throughput) and dependency edges through `LatencyBetween` (RAW stall). Whether two async ops may overlap is decided by the `TpuAsyncTracker` resource model — a 0..46 `ResourceType` enum with per-resource hazard classes. Both the cost feed and the resource model have their own pages: [Bundle-Aware Cost](../cost/bundle-aware-cost.md) and the [ResourceType Taxonomy](scheduler-resourcetype-model.md).

---

## Stage 2 — MXU Sequence Assignment and MRB Allocation

### Purpose

Between the HLO scheduler and the bundle packer, a TPU-specific pass assigns matmul ops to the systolic array's physical resources. Each matmul accumulation chain becomes an `MxuSequence`; the chains are bin-packed onto the available MXU passes (`AssignMxusForSequenceGroup`, `0x10f753c0`, worker `0x10f77ca0`); the matrix-result buffers (MRB) that hold partial sums are reserved on a timeline (`MrbChainAllocator`, `0x10f58800`); and the result FIFOs / matrix-shift-registers (MSR) that drain those buffers are placed (`AllocateMrbEntriesAsFifo` `0x10f3ef80`, `BounceBetweenMsrs` `0x10f3fae0`). The assignment is committed by writing latch indices onto each sequence (`SetLatchIndices`, `0x10f3b4c0`).

This stage has no analogue in a scalar backend. It exists because the TPU MXU is a *reservation-scheduled* unit: a matmul does not complete in one cycle but pushes weights and operands into a systolic pipeline whose result appears in a specific buffer many cycles later, and the compiler must statically reserve that buffer and the latch that reads it. The bundle packer downstream treats those latch indices as a slot-legality input — two ops that would collide on the same latch cannot share a bundle.

### Structure

```text
AssignMxusForSequenceGroup                        0x10f753c0   ── bin-pack matmul sequences onto MXU passes
  ├─ (worker) AssignMxusForSequenceGroupInternal  0x10f77ca0
  ├─ MrbChainAllocator                                          ── reservation timeline for accumulation buffers
  │     ├─ ExtendMrbReservation                   0x10f58800
  │     ├─ SplitAccumulationChain                 0x10f598e0
  │     ├─ ReleaseMrbReservation                  0x10f5f9e0
  │     └─ AdvanceTimeTo                          0x10f5e9e0
  ├─ AllocateMrbEntriesAsFifo                     0x10f3ef80   ── result FIFO placement
  ├─ BounceBetweenMsrs                            0x10f3fae0   ── matrix-shift-register ping-pong
  └─ SetLatchIndices                              0x10f3b4c0   ── commit latch ids onto each MxuSequence
```

### Sub-Pages

| Component | What it owns | Page |
|---|---|---|
| `MxuSequence` / `SequenceInfo` | the per-sequence record and the `set_mxu` commit | [MxuSequence / SequenceInfo](mxu-sequence-struct.md) |
| `AssignMxusForSequenceGroup` | the bin-packing algorithm that places sequences on MXU passes | [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) |
| `MrbChainAllocator` | the accumulation-chain reservation timeline and jitter model | [MRB Chain Allocator](mrb-chain-allocator.md) |
| `AllocateMrbEntriesAsFifo` / `BounceBetweenMsrs` | result-FIFO and MSR placement | [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md) |
| `SetLatchIndices` / overrun handshake | latch-index assignment and the per-gen overrun handshake | [Latch Assignment & Overrun](latch-assignment-overrun.md) |

> **NOTE — Stage 2 is a dependency of Stage 3, not a sub-pass of it.** The bundle packer (`BundlePacker::Feed`) reads each op's already-assigned latch when it asks whether the op's `BundleRequirement` fits a candidate bundle. A reimplementation that folds MXU assignment into the packer's per-op loop will deadlock the dependency: latch assignment needs the full accumulation chain in view, which the packer's one-op-at-a-time forward walk does not have.

---

## Stage 3 — LLO Bundle Packing

### Purpose

The bundle packer takes the LLO instruction list — the last IR before raw bundle-byte emission — and assigns each instruction to a slot inside a fixed-width VLIW bundle, respecting per-generation resource constraints (functional-unit count, vector source ports, immediate slots, predicate fields). The output is a `vector<Bundle>` consumed by the per-gen `Encoder*::EncodeBundle`. The algorithm is **forward greedy earliest-legal-bundle list scheduling**: walk the region topologically, and for each op compute the earliest bundle its RAW dependencies allow, then ask `SlotTracker` for the first bundle at or after that point with room for the op's `BundleRequirement`; if none fits, append a new empty bundle and retry. There is no spill-to-next-bundle search and no backtracking.

### Two Packers, One Algorithm

libtpu ships two structurally identical packers at different IR levels: the canonical `xla::jellyfish::BundlePacker` operating on `LloInstruction*` (the path straight to the per-gen encoder), and an `llvm::(anonymous)::BundlePacker` `MachineFunctionPass` operating on `MachineInstr*` (the path through LLVM MC). Both produce the same bundle byte layout; they are two implementations of the same earliest-legal-bundle algorithm against two IRs. The per-gen slot legality is encapsulated in four `TpuBundleRestrictions` subclasses — `JellyfishBundleRestrictions`, `PufferfishBundleRestrictions`, `ViperfishBundleRestrictions`, and `GhostliteBundleRestrictions` (the only `*BundleRestrictions` subclasses present in the binary; there is no `dragonfish`/`6acc60406` restriction table) — each providing `SetLimits`, `AddXluRequirements`, `AddMxuRequirements`, and `MatchScalar` virtuals.

### Structure

```text
PackBundles                                       0x10a30a20   ── top-level entry (timer, numbering, options)
  └─ GlobalBundlePacker::Pack                      0x10a86420   ── orchestrator, topological region walk
        ├─ PackPhis (region)                       0x10a87160   ── SSA-resolution moves into predecessor tails
        ├─ PackInstruction (instr)                 0x10a875a0   ── regular ops
        │     └─ BundlePacker::Feed (instr, lat)   0x14021f20   ── earliest-legal-bundle + SlotTracker
        │           └─ SlotTracker::FindFeasibleBundleAfter  0x140340a0
        └─ PackBranch (instr)                      0x10a877a0   ── opcodes 0x87/0x88/0xef + delay slots
  → ConsumeBundles                                 0x14026c40   ── hand off vector<Bundle>; ValidatePacking 0x14026ca0
```

### Modulo Scheduling for Inner Loops (Stage 3b)

Hardware-loop inner bodies take a separate path: LLVM's `TPUScheduleDAGModulo` performs iterative modulo scheduling — pick an initiation interval (II) from the max of resource and recurrence bounds, then place the loop body across II-many bundles. The II search is `calculateResourceMII` (`0x13c0bee0`, the ResMII bound) and `calculateLargestLatencyMII` (`0x13c0b840`, the recurrence/RecMII bound); `findSchedule` (`0x13b1d7c0`) is the placement driver. This is the textbook software-pipelining path and is invoked only for loops marked `hardware_loop`; the straight-line list packer handles everything else.

### Sub-Pages

| Component | What it owns | Page |
|---|---|---|
| `GlobalBundlePacker` / `BundlePacker::Feed` / `SlotTracker` / `TpuBundleRestrictions` | the forward greedy algorithm, the per-gen slot matrix, branch/barrier/phi handling | [LLO → Bundle Packing](llo-bundle-packing.md) |
| `TPUScheduleDAGModulo` | the II-search and software-pipelining path for hardware loops | [Bundle Modulo Scheduling](bundle-modulo-scheduling.md) |
| per-gen `Encoder*::EncodeBundle` | how the assigned latch fields serialize into the per-gen bundle word | [Per-Gen Encoder Latch Serialization](encoder-latch-serialization.md) |

> **QUIRK — the packer never spills; it grows the bundle vector.** When `SlotTracker::FindFeasibleBundleAfter` returns "no feasible bundle," the algorithm does not search alternative slots or reorder earlier ops — it `emplace_back`s a fresh empty bundle and retries the same op. A compile failure only occurs when a *single* op's `BundleRequirement` exceeds the gen's per-bundle limit even alone (a miscoded restriction table), not when the schedule is merely dense. A reimplementation that adds a spill/backtrack heuristic will diverge from the binary on every bundle-boundary decision.

---

## How the Stages Are Priced

All three stages query the Part VII cost model, but at different granularities and for different decisions.

| Stage | Decision priced | Cost query | Unit |
|---|---|---|---|
| Stage 1 (LHS) | which async op to issue, how much compute hides its latency | `GetLatencyBetween` → `MaxResourceCycles` (node bundle cost) + `LatencyBetween` (edge RAW stall) | bundle cycles → seconds via TC clock |
| Stage 1 (LHS) | whether two async ops may overlap | `TpuAsyncTracker::GetResourceHazardType` over the 0..46 `ResourceType` enum | hazard class (shareable / serial / unsharable / nonextendable) |
| Stage 2 (MXU) | MRB reservation windows, accumulation jitter | per-gen `CycleTable` matmul/matprep latencies | MXU pass cycles |
| Stage 3 (packer) | whether an op fits a bundle | per-gen `BundleRequirement` slot bitmaps (not the cost model) | slot occupancy |

The cost model itself — the 23-slot `ResourceVector`, the `MaxResourceCycles` `max`-over-lanes reduction, the throughput-vs-latency split — is documented in Part VII; this page only names where each scheduling stage taps it. The two most relevant pages are the [Cost Model Overview](../cost/overview.md) (the three cost-class families and the `Target` clock wiring) and [Bundle-Aware Cost](../cost/bundle-aware-cost.md) (the `MaxResourceCycles` bundle cost and `LatencyBetween` dependency latency that feed the LHS comparator).

> **NOTE — Stage 3's "cost" is legality, not latency.** Unlike Stage 1, the bundle packer does not minimize cycles; it satisfies a constraint (fit the slots). Its only latency input is the per-op RAW latency it uses to compute the earliest legal bundle in `BundlePacker::Feed`. The latency *hiding* — overlapping long edges under compute — has already been decided at the HLO level by Stage 1. A reimplementer must not re-run latency-hiding heuristics in the packer; that double-counts the optimization.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Stage 1 LHS is invoked from `RunHloScheduler` as two sequential pipelines (base then overlap) | `RunHloScheduler` @ `0x1096fac0` (Brkga / `final_scheduler` / `async_scheduling` strings) | CONFIRMED |
| LHS `RunImpl` body is shared across all variants | single compiled `RunImpl` @ `0x136321a0` | CONFIRMED |
| LHS comparator is the 22-key `ReadySetLt` chain, reverse-direction drain | `FindAndExtractBestNodeAvailable` @ `0x13618880`; `ScheduleComputation` @ `0x1362eb60` | CONFIRMED |
| Stage 2 bin-packs matmuls into `MxuSequence`s and reserves MRB | `AssignMxusForSequenceGroup` @ `0x10f753c0`; `MrbChainAllocator::ExtendMrbReservation` @ `0x10f58800` | CONFIRMED |
| Stage 2 commits latch indices read downstream by Stage 3 | `SetLatchIndices` @ `0x10f3b4c0`; `AllocateMrbEntriesAsFifo` @ `0x10f3ef80` | CONFIRMED |
| Stage 3 is forward greedy earliest-legal-bundle list scheduling | `PackBundles` @ `0x10a30a20`; `GlobalBundlePacker::Pack` @ `0x10a86420`; `BundlePacker::Feed` @ `0x14021f20` | CONFIRMED |
| Stage 3b modulo scheduler for hardware loops with RecMII/ResMII II search | `TPUScheduleDAGModulo::findSchedule` @ `0x13b1d7c0`; `calculateResourceMII` @ `0x13c0bee0`; `calculateLargestLatencyMII` @ `0x13c0b840` | CONFIRMED |
| Stage 1 prices via `MaxResourceCycles` / `LatencyBetween`; gates via `TpuAsyncTracker` | `MaxResourceCycles` @ `0x1c89b9e0`; `LatencyBetween` @ `0x1c89f820`; `GetResourceHazardType` @ `0x110015e0` | CONFIRMED |
| Per-gen slot legality is four `TpuBundleRestrictions` subclasses (Jellyfish/Pufferfish/Viperfish/Ghostlite) | only four `*BundleRestrictions::SetLimits` symbols exist (`0x1c457a40` / `0x1c457d80` / `0x1c458360` / `0x1c458860`); zero `Trillium`/`dragonfish` restriction symbols | CONFIRMED |
| "ILP-LHS" flag swaps the async classifier only; ILP MIP is a separate opt-in memory scheduler (not dead) | `EnableIlpLatencyHidingScheduler` gate only re-classifies async ops; `ILPMemoryScheduler::Run` @ `0x10acd020` is reached via `GetMemorySchedulerAlgorithm` @ `0x10abd6a0` **case 6** (switch arms Default0..LocalOrder9, ILP=6, constructs `xla::ILPMemoryScheduler`) | CONFIRMED |
| HLO scheduling annotations do not flow into bundle packing | LLO packer reads no HLO annotation; the two operate on disjoint IRs | HIGH |

---

## Cross-References

- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — Stage 1 core: the reverse-direction drain loop, the 22-key comparator, precomputed async depth/height, the memory-pressure retry loop.
- [LHS post_layout](lhs-post-layout.md) — the live Stage 1 placement in 0.0.40: the two-pipeline `RunHloScheduler` and its config/estimator/tracker wiring.
- [LHS post_layout_pre_fusion](lhs-post-layout-pre-fusion.md) — the same LHS body wired at the dead "Pre main fusion" slot; documented by its config delta.
- [LHS ILP variant](lhs-ilp-variant.md) — the `EnableIlpLatencyHidingScheduler` async-classifier swap and the dead `ILPMemoryScheduler` MIP.
- [ResourceType Taxonomy](scheduler-resourcetype-model.md) — the 0..46 `ResourceType` enum, hazard classes, and `AsyncTracker` → core registry that gate Stage 1 overlap.
- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — Stage 2 per-sequence record and the `set_mxu` commit.
- [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) — Stage 2 `AssignMxusForSequenceGroup` bin-packing algorithm.
- [MRB Chain Allocator](mrb-chain-allocator.md) — Stage 2 accumulation-chain reservation timeline and jitter model.
- [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md) — Stage 2 result-FIFO and matrix-shift-register placement.
- [Latch Assignment & Overrun](latch-assignment-overrun.md) — Stage 2 latch-index assignment and the per-gen overrun handshake.
- [LLO → Bundle Packing](llo-bundle-packing.md) — Stage 3 forward greedy packer, per-gen slot matrix, branch/barrier/phi handling.
- [Bundle Modulo Scheduling](bundle-modulo-scheduling.md) — Stage 3b II-search software pipelining for hardware loops.
- [Per-Gen Encoder Latch Serialization](encoder-latch-serialization.md) — how Stage 2's latch fields serialize into the per-gen bundle word.
- [Cost Model Overview](../cost/overview.md) — the three cost-class families and the `Target` clock wiring behind every cycle number.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — the `MaxResourceCycles` bundle cost and `LatencyBetween` dependency latency that feed Stage 1's comparator.
- [Bundle Model Overview](../isa/bundle-model-overview.md) — the VLIW bundle word that Stage 3 packs and Stage 2's latches serialize into.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
