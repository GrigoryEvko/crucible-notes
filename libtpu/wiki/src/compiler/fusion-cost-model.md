# Fusion Cost Model

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, **not** stripped — every symbol is a demangled C++ name). Section map: `.text`/`.rodata` VMA == file offset. All addresses are virtual addresses. Other libtpu builds will differ.*

## Abstract

TPU instruction fusion is **priority-driven**, not a single bottom-up sweep. Every producer in a computation is assigned a `double` priority by a cost function, inserted into an ordered queue, and the highest-priority producer is fused first; after each fuse the affected neighbours are re-scored. This page documents the **numeric scoring** — the floating-point priority formula, the per-op cost weights it consumes, and the hard VMEM gate that overrides the score — for the two interchangeable cost models the queue can run, plus the separate profit number that ranks multi-output (sibling) fusion. It does **not** document the *pattern predicates* that decide whether a fusion is structurally legal at all; those — the ~30 `FusionDecision` rejection sites of `ShouldFuseImpl`, the slice-like / output-fusion / duplicate-expensive gates, the custom-call registry hook — live on [Fusion Patterns](fusion-patterns.md). The relationship is: the predicate cascade is a **filter** (legal/illegal); the cost model is a **ranker** (how good) over the survivors, with one shared hard gate (`FusionWouldExceedVmemCapacity`) that both consult.

There are two cost models and they are genuinely different functions, selected per-producer (and per-edge) by a dispatcher. The **current** model — the default — prices fusion as *HBM bytes saved* (expressed in TensorCore cycles) minus *added compute* (a coarse opcode-weight ladder) scaled by how many expensive conv/reduce-window ops the fusion would duplicate. The **bundle-aware** model prices the *actual VLIW bundle cycles saved* (`total_unfused − total_fused`), which captures cross-functional-unit packing the linear model cannot see. Both feed the same priority queue and obey the same VMEM hard gate.

The reader who knows LLVM should hold one analogy and one divergence. The analogy: the priority loop is a greedy list scheduler whose "ready" set is the producer queue and whose priority function is this cost model — much like an LLVM `MachineScheduler` `SchedBoundary` picking the best `SUnit`. The divergence: there is **no register-pressure term in the score**. Register/VMEM pressure is a binary admission gate (`FusionWouldExceedVmemCapacity`), checked both in the priority pre-pass and again in the predicate cascade; a candidate that fits is ranked purely on bytes/cycles saved, and one that does not fit is rejected outright (priority `-1.0`), never softly penalised.

For reimplementation, the contract is:

- **The priority key is a 3-tuple `(double primary, double secondary, long tie)`** inserted into a `std::map`; the queue dequeues the **largest** key (highest priority first). Both `double`s are NaN-trapped with a fatal `CHECK` before insertion.
- **Current-model score: `priority = mem_reduce − compute × conv_rw_count`.** `mem_reduce` is HBM bytes saved in TC cycles; `compute` is the opcode-weight ladder result; `conv_rw_count` is the number of Conv + ReduceWindow ops the fusion duplicates, so duplicating an expensive op is penalised proportionally.
- **Bundle-aware score: `priority = total_unfused − total_fused`**, both in bundle cycles from the `ResourceVector` machinery; this is the only model that sees VLIW packing.
- **Three priority sentinels:** `-1.0` = do-not-fuse; `FLT_MAX` (current) / `×100.0` boost (bundle) = must-fuse; the `long` tie-break is `0x3ffffffffffffffe` (current) / `INT64_MAX` (bundle).
- **VMEM is a hard gate, not a score term.** Any user whose fusion would exceed VMEM forces the producer's priority to `-1.0`.
- **The compute-weight ladder is a coarse opcode `switch`** to a handful of scalar tiers (`1.0` default, `4.0`, `10.0`, `42.0`, plus conv-flop and fusion-recurse escapes), each multiplied by `Target::ChunksIn(shape)`.
- **Multi-output (sibling) fusion has its own profit number** = bytes of the shared operand read once instead of once-per-sibling, ranked in a separate priority queue and gated by a 4 MiB per-reduce-output cap, a max-operands cap, and an HBM-pressure cap.
- **The scoring code is generation-invariant.** Per-gen behaviour enters only through `Target` constants, the per-gen `CycleTable` latencies, and flag defaults — the formulas and the `switch` are one shared implementation.

| | |
|---|---|
| **Priority dispatcher** | `TpuPriorityFusionQueue::CalculateProducerPriority(HloInstruction*)` @ `0x1308fa20` |
| **Current model** | `…::CalculateProducerPriorityWithCurrentCostModel` @ `0x13096160` |
| **Bundle-aware model** | `…::CalculateProducerPriorityWithBundleAwareCostModel` @ `0x130954c0` |
| **Enqueue + NaN guard** | `…::EnqueueToProducerPriorityQueue(tuple<double,double,long>, HloInstruction*)` @ `0x1308fb20` |
| **Compute term** | `…::NormalizedComputationCost(HloInstruction*, long)` @ `0x130989a0` |
| **Memory term** | `…::GetNormalizedMemoryCostReductionIfFusing` @ `0x13099700` |
| **VMEM hard gate** | `CostModel::FusionWouldExceedVmemCapacity(HloInstruction*, HloInstruction*)` @ `0x130c4a80` |
| **Bundle cycles** | `…::GetHloCycles` @ `0x13097a00`; `CostModel::GetCyclesIfFused` @ `0x130aba40` |
| **MOF profit** | `TpuMultiOutputFusion::GetProfit(HloInstruction*, HloInstruction*)` @ `0x110dd0a0` |
| **Source files** | `tpu_instruction_fusion.cc` (priority); `cost_model/cost_model.cc` (cycles, VMEM) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Where the Score Sits in the Pass

The pass is `TpuInstructionFusion`, an `xla::InstructionFusion` subclass. The base class drives a generic priority loop; the TPU subclass supplies two things: the **queue** (`GetFusionQueue` @ `0x13083c40` constructs a `TpuPriorityFusionQueue`) and the **legality predicate** (`ShouldFuseImpl`, documented on [Fusion Patterns](fusion-patterns.md)). The cost model on this page *is the queue's priority function*.

```text
InstructionFusion::Run (base)
  ├─ GetFusionQueue(comp)                      ── build TpuPriorityFusionQueue
  │     └─ for each producer:
  │           CalculateProducerPriority(p)     ── THIS PAGE: the score
  │           EnqueueToProducerPriorityQueue   ── insert (double,double,long) → map
  └─ loop:
        DequeueNextInstructionAndOperandsToFuseInOrder()  ── pop LARGEST key
        ├─ ShouldFuseImpl(consumer, operand_idx)  ── FUSION-PATTERNS PAGE: legal?
        │     └─ shares the VMEM hard gate with the score (below)
        ├─ if legal: Fuse(), then
        │     OnFusingInstruction / InvalidateCachedCostModelState
        │     re-score affected neighbours (priorities change once an edge internalises)
        └─ else: drop / mark -1.0
```

Two consequences a reimplementer must preserve. First, the order is **global-greedy with incremental re-scoring**: the largest-byte/cycle-saving fusion fires first, and fusing it changes the priority of its neighbours (an internalised edge no longer saves its HBM traffic), so those are re-scored before the next dequeue. Second, the cost model and the legality predicate are **separate concerns that share exactly one gate** — `FusionWouldExceedVmemCapacity`. The score never encodes "is this legal"; it only ranks among the legal. If you collapse the two, you will either rank illegal fusions or reject legal-but-low-value ones.

There is no fixed instruction-count cap on the main fusion loop; it runs until the queue empties — every producer has either fused or been marked `-1.0` / boundary. The natural stop is "no remaining candidate has positive priority and fits in VMEM." (Multi-output fusion does have explicit budget caps; see its section.)

---

## The Priority Key — a 3-Tuple in an Ordered Map

`EnqueueToProducerPriorityQueue` (@ `0x1308fb20`) inserts into a `std::map<std::tuple<double,double,long>, HloInstruction*>`. The mangled signature is unambiguous: `…EnqueueToProducerPriorityQueueENSt3__u5tupleIJddlEE…` — `tuple<d,d,l>`. `std::map` orders ascending; the loop dequeues the **last** (largest) element, so the highest priority fuses first.

```c
// EnqueueToProducerPriorityQueue(tuple<double,double,long> key, HloInstruction*)  @ 0x1308fb20 (decompiled)
// — both doubles are NaN-trapped before insertion:
__asm { vucomisd xmm0, xmm0 }                    // get<0>(key) == itself ?
if (parity)                                       // NaN
  LogMessageFatal(".../tpu_instruction_fusion.cc", 1678, "!std::isnan(std::get<0>(key))");
__asm { vucomisd xmm0, xmm0 }                    // get<1>(key) NaN-check (second double)
// ... then __tree __emplace_unique into the map keyed on the tuple.
```

| tuple slot | type | meaning | confidence |
|---|---|---|---|
| `get<0>` *(primary)* | `double` | the priority score (current- or bundle-model formula below); the ranking key | CONFIRMED |
| `get<1>` *(secondary)* | `double` | a re-stored copy of the priority value (a stability re-score); used as a same-primary discriminator | HIGH — re-stored from the same `priority` slot at several `rbp` offsets; exact role not byte-pinned |
| `get<2>` *(tie)* | `long` | deterministic tie-break: `InputSizeAt` accumulation / chunk count (current); `INT64_MAX` for must-fuse; ensures stable order across runs | CONFIRMED structurally; exact long composition spans the 0x1d8-byte frame (PARTIAL) |

The NaN guard is a hard `CHECK` at `tpu_instruction_fusion.cc:1678` (and a second `vucomisd` for the second `double`). Any formula that can produce `NaN` — e.g. a `0/0` in a bytes-per-cycle conversion — aborts compilation rather than corrupting the map order. A reimplementer must therefore guarantee finite scores or assert likewise.

> **NOTE — the queue picks the LARGEST key.** Because `std::map` is ascending, "highest priority" = last element. A producer scored `-1.0` sorts to the bottom and is effectively "do not fuse." A producer scored `FLT_MAX` sorts to the top — the must-fuse boost.

---

## The Dispatcher — Current vs Bundle-Aware

`CalculateProducerPriority` (@ `0x1308fa20`) is the single entry the base loop calls. It picks the model per-producer and per-fusible-edge:

```c
// CalculateProducerPriority(HloInstruction* producer)  @ 0x1308fa20 (decompiled, condensed)
if (producer == nullptr) {                                  // queue init / no producer
  if (ShouldAlwaysUseBundleAwareCostModel(flags))           // @0x130d3cc0
    return WithBundleAwareCostModel(producer);
  return WithCurrentCostModel(producer);
}
if (!producer->IsOutputFusion()) {                          // opcode[+12] != kFusion(0x81)
  // not an output-fusion ROOT → priority -1.0, UNLESS a force-priority bit is set:
  if (!(producer[+0xd] & 0x8) &&
      !any_user(u : u.opcode==0x81 /*kFusion*/ && (u[+0xd] & 0x8)))
    return -1.0;                                            // do-not-fuse sentinel
}
// per-edge: if ANY qualifying user wants the bundle model, use it:
return ShouldUseBundleAwareCostModel(user, flags)           // @0x130d3c00
           ? WithBundleAwareCostModel(producer)
           : WithCurrentCostModel(producer);
```

A second, redundant dispatch lives **inside** the current model itself (`0x13096177`): `if (flags[+0xc] == 1) tail-jump to bundle-aware` — i.e. the `xla_tpu_use_bundle_aware_cost_model_for_fusions` byte at flag offset `+0xc` forces the bundle model even when entered via the "current" path. So model selection is layered: a global flag, a per-producer output-fusion gate, and a per-user edge query. **The default is the current model.**

| selector | function / field | effect |
|---|---|---|
| global force | `ShouldAlwaysUseBundleAwareCostModel` @ `0x130d3cc0` | all producers use bundle model |
| flag byte | `flags[+0xc]` (`xla_tpu_use_bundle_aware_cost_model_for_fusions`) | forces bundle model inside the current entry |
| per-edge | `ShouldUseBundleAwareCostModel(user)` @ `0x130d3c00` | any qualifying user opts the edge into bundle model |
| output-fusion gate | `producer->IsOutputFusion()` + force-bit `[+0xd]&0x8` | non-output-fusion producer without the bit → `-1.0` |

---

## Current Cost Model — `mem_reduce − compute × conv_rw_count`

`CalculateProducerPriorityWithCurrentCostModel` (@ `0x13096160`) is the default ranker. It computes a linear estimate: HBM bytes saved minus duplicated compute. The decompiled control flow:

```c
// CalculateProducerPriorityWithCurrentCostModel(producer)  @ 0x13096160 (decompiled, condensed)
users = GetFusibleUsers(producer);                                   // 0x13097220
// (1) VMEM HARD GATE — pre-emptive. If ANY user's fusion would exceed VMEM → reject.
for (user : users)
  if (CostModel::FusionWouldExceedVmemCapacity(producer, user))      // 0x130c4a80
    return {priority = -1.0, long = -1};

conv_rw_count = GetConvAndRWCountsInNestedFusion(producer);          // 0x1454d240  → [rbp-0x88]

// (2) MUST-FUSE shortcuts → FLT_MAX:
if (producer.opcode == 0x1a /*collective*/ &&
    GetTpuCompEnv(producer)[+0x1206] != 0)                          // byte 4614
  return {priority = FLT_MAX, long = 0x3ffffffffffffffe};
if (IsNonFusionCollective(producer) && UserDirectedFuseInfo.IsMustFuse)
  return {priority = FLT_MAX};   // logs "Giving collective producer with must fuse attribute highest priority: "

// (3) THE FORMULA:
compute    = NormalizedComputationCost(producer, 0);                 // 0x130989a0 → [rbp-0x40]
mem_reduce = GetNormalizedMemoryCostReductionIfFusing(producer,users);// 0x13099700 → [rbp-0x50]
multiplier = conv_rw_count;                                          // [rbp-0x88]
priority   = mem_reduce - compute * multiplier;                      // vmulsd ; vsubsd

// (4) PRED single-bit packing correction:
if (producer.shape.element_type == PRED)
  priority *= (double) ElementPackingFactor(topology, PRED, ShouldPackPREDAsSingleBit(...));

// (5) NEGATIVE-PRIORITY FALLBACK:
if (priority < 0.0)
  priority = ActualCostReduction(producer, users);                  // 0x1309a060
```

The two `.text` sites pin the arithmetic byte-exactly: `vmulsd xmm0, xmm0, [rbp+var_88]` then `vsubsd xmm0, xmm1, xmm0` (mem_reduce − compute×multiplier) at `0x130967c1..`; the PRED multiply `vmulsd xmm1, xmm1, xmm0`; the `ActualCostReduction` call.

**Interpretation of the terms.** Both terms are in **cycle** units, so they are directly comparable:

```text
priority =  (HBM bytes saved, converted to HBM-transfer CYCLES)
          − (added MXU/vector compute, in compute CYCLES)
              × (number of Conv/ReduceWindow ops the fusion would DUPLICATE)
```

The `conv_rw_count` multiplier is the key TPU-specific shaping: duplicating an *expensive* op (a convolution lowered to MXU work) is penalised in proportion to how many copies the fusion creates, while fusing pure elementwise (where `compute` is the cheap `1.0` tier and nothing expensive is duplicated) adds almost nothing. There is deliberately **no register-pressure term** — that is the `FusionWouldExceedVmemCapacity` hard gate in step (1).

The **PRED correction** (step 4) handles boolean tensors: when the topology packs `PRED` as a single bit (8 booleans/byte), the byte footprint — and hence the memory-saving term — is scaled by the packing factor (`≈8`), so a large boolean producer is not under-valued. `ShouldPackPREDAsSingleBit` @ `0x1d6b0080`, `ElementPackingFactor` @ `0x1d6b03e0`.

The **negative-priority fallback** (step 5) re-prices with `ActualCostReduction` (@ `0x1309a060`), which recomputes the true reduction accounting for nested-fusion overhead and tuple-result penalties. This rescues a compute-heavy fusion that still nets a memory win from being mis-rejected by the linear estimate.

### The memory term — HBM bytes saved as TC cycles

`GetNormalizedMemoryCostReductionIfFusing` (@ `0x13099700`) returns the HBM traffic eliminated by fusing, expressed in TensorCore cycles:

```text
reduction =  GetNormalizedMemoryCost(producer)            // producer's write to HBM (saved)
           + Σ_users  InputSizeAt(producer, user)         // each user's read of producer (saved)
           − cost_of_fused_result_writes_and_reads        // edges that survive the fusion
```

The per-tensor byte→cycle conversion (from `GetNormalizedMemoryCost` @ `0x1309a620`):

```c
bytes        = OutputSize(inst);                           // 0x13097f20 — granule-aligned Σ subshapes
bytes_per_cy = HbmFullChipBytesPerSecond()                 // 0x1d6172a0
             / LogicalDevicesPerChip(core_type)            // 0x1d615b00
             / (TensorCoreFrequencyInMegaHertz() * 1e6);   // 0x1d615b60 × (qword_A2E0208 = 1.0e6)
hbm_cycles   = bytes * GranuleBytes() / bytes_per_cy;      // 0x1d617f80
```

So the memory term is literally "how many TensorCore cycles of HBM traffic does fusing this producer eliminate." A producer whose output is large and read by several users yields a large positive reduction. `OutputSize` walks all output subshapes (handling `kTuple`); `InputSizeAt` (@ `0x1309a180`, uncached `0x1309ad40`) is the per-edge bytes the consumer reads from the producer — the quantity internalised by fusion — cached on `(producer, consumer)`.

### The compute term — `NormalizedComputationCost`

`NormalizedComputationCost` (@ `0x130989a0`) is the **compute penalty**: one `double` per op, a coarse "expense class" rather than a cycle-accurate latency (the LLVM analogue is `TTI::getInstructionCost` returning a tier, not a count). It is a `switch (opcode)` (decompiled `switch (*((_BYTE*)inst + 12))`) to a few scalar weights, each multiplied by `Target::ChunksIn(shape)` (@ `0x1d619900`, the count of MXU chunk-granule tiles covering the shape):

```text
cost = Σ_operands ChunksIn(operand.shape) × W_operand  +  ChunksIn(root.shape) × W_root
```

The `.rodata` weight constants used at the `vmulsd` sites (CONFIRMED byte-exact):

| weight const | value | op class (this page's scope) |
|---|---|---|
| `qword_A2DF230` | `1.0` | base / default cheap elementwise |
| `qword_A2DE830` | `4.0` | mid class (reduce / cross-lane / logistic) |
| `qword_A2DF498` | `10.0` | divide |
| `qword_A2DF1A0` | `42.0` | erf / matmul-conv heavy class |

For a convolution the cost is the `flop_count`-based estimate (`HloCostAnalysis::flop_count`, called at the conv block) rather than a flat tier; for a `fusion` producer the cost is the **recursive sum** of its body's per-op costs, cached on `unique_id`. The full opcode→weight `switch` — including the `0.0` layout-op tier, the `2.0` first-parameter tier, and the `dot`-is-CHECK-fatal escape — is enumerated on the dedicated [NormalizedComputationCost](../cost/normalized-computation-cost.md) cost page; this page documents the four-tier ladder the priority formula consumes and defers the exhaustive 106-entry jump-table decode there.

> **NOTE — `dot` must not reach here.** `NormalizedComputationCost` `CHECK`-fatals on a `dot` opcode: by the time fusion runs, every dot has been rewritten to a convolution (see [Dot/Conv MXU Lowering](dot-conv-mxu-lowering.md)). The compute term is conv-aware, not dot-aware.

---

## Bundle-Aware Cost Model — `total_unfused − total_fused`

`CalculateProducerPriorityWithBundleAwareCostModel` (@ `0x130954c0`) prices the **actual VLIW bundle cycles** saved by fusion, using the `ResourceVector` bundle-cost machinery. Because that machinery prices a bundle by the *maximum* occupied functional-unit lane (not the sum), it captures cross-engine overlap — e.g. a producer's matprep packing into the same bundle as the consumer's vector ALU — that the linear current model cannot see.

```c
// CalculateProducerPriorityWithBundleAwareCostModel(producer)  @ 0x130954c0 (decompiled, condensed)
users = GetFusibleUsers(producer);
if (fusion_util::IsMustFuseProducer(producer))               // 0x14559400
  return /* boosted: see the 100.0 constant */;

// (A) UNFUSED: producer's own bundle cycles × #users, plus each user's own cycles.
total_unfused = GetHloCycles(producer) * producer.user_count;   // 0x13097a00 ; vmulsd
for (user : users)
  total_unfused += GetHloCycles(user);                          //          → [rbp-0x38]

// (B) FUSED: the cost of each merged (producer,user) bundle.
total_fused = 0;
for (user : users) {
  ResourceVector rv = {};
  total_fused += CostModel::GetCyclesIfFused(producer, user, opts, &rv);  // 0x130aba40 → [rbp-0x48]
}                                                               // cached on (prod_id, user_id)

// (C) PRIORITY = cycles saved.
priority = total_unfused - total_fused;                         // 0x13095d38 vsubsd
```

The `vsubsd xmm0, xmm0, [rbp+var_48]` at `0x13095d38` and the VLOG strings confirm the formula exactly: the binary logs `" total_unfused_cycles: "`, `" total_fused_cycles: "`, `" new_model_priority: "`, and `" producer.user_count: "` (the "JFF" = JellyFish Fusion logging family). A fusion that lets the producer's work overlap the consumer's in fewer bundles makes `total_fused < total_unfused`, yielding a positive priority.

`GetHloCycles` (@ `0x13097a00`) = `CostModel::GetCycles` (@ `0x130aade0`) = the `ResourceVector::MaxResourceCycles` bundle cost, cached per `unique_id`. `GetCyclesIfFused` (@ `0x130aba40`) prices the same `max`-reduction on the merged producer+consumer bundle. Both are documented in full on [Bundle-Aware Cost](../cost/bundle-aware-cost.md); the fused-merge driver (`ScaleAndSumOutputFusionResourceVectors`, the slot-9/11 `max`-combine) is on [NormalizedComputationCost](../cost/normalized-computation-cost.md). This page owns only the *priority subtraction* that consumes them.

The must-fuse boost is the constant `qword_A2DF5C0 = 100.0` (`vmovsd xmm0, cs:qword_A2DF5C0` when `IsMustFuseProducer` is true) — a user-pinned fusion sorts above any cost-derived score. The bundle model's `long` tie-break is `INT64_MAX` (`0x7fffffffffffffff`).

### Model comparison

| term | current model | bundle-aware model |
|---|---|---|
| benefit | HBM bytes saved (→ HBM cycles) | unfused − fused bundle cycles |
| compute penalty | `NormComputeCost × conv_rw_count` (explicit) | folded into `total_fused` |
| VLIW bundle packing | **not** modelled | modelled (`GetCyclesIfFused`) |
| VMEM / register pressure | hard gate (same) | hard gate (same) |
| must-fuse score | `FLT_MAX` | `×100.0` boost |
| tie-break `long` | input-size accumulation | `INT64_MAX` sentinel |
| default? | **yes** | only when flagged / per-edge |

---

## The Priority Sentinels

Three `.rodata` doubles and two `long`s carry out-of-band ranking signals. All CONFIRMED byte-exact (the doubles verified by reinterpreting the stored bit patterns):

| sentinel | `.rodata` | bit pattern | meaning |
|---|---|---|---|
| `-1.0` | `qword_A2DE728` | `0xbff0000000000000` | do-not-fuse — producer is not an output-fusion root, or a user's fusion would exceed VMEM |
| `FLT_MAX` | `qword_A2E0530` | `0x47efffffe0000000` (`3.4028e+38`) | must-fuse, current model (collective with must-fuse attribute) — sorts to the top |
| `100.0` | `qword_A2DF5C0` | — | must-fuse boost, bundle model (`IsMustFuseProducer`) |
| must-fuse `long` (current) | — | `0x3ffffffffffffffe` | tie-break paired with `FLT_MAX` |
| must-fuse `long` (bundle) | — | `0x7fffffffffffffff` | `INT64_MAX` |
| µs conversion | `qword_A2E0208` | `1.0e6` | cycles↔microsecond factor in the memory/tie-break math |

---

## The VMEM Hard Gate (shared with the predicate cascade)

`CostModel::FusionWouldExceedVmemCapacity` (@ `0x130c4a80`) is the one gate the cost model and the legality predicate both consult — the reason VMEM pressure never appears as a score term. In the **score**, it is a pre-pass in the current model (step 1 above): any user whose fusion would exceed VMEM forces the producer to `-1.0`. In the **predicate** (on [Fusion Patterns](fusion-patterns.md)), the same check rejects with `"Nested dot fusion would exceed vmem capacity"` (`.rodata 0x84b1b50`) or `"Custom Fusion would exceed vmem capacity"` (`0x84b1b7d`), and the operand-fit variant emits `"No fusing: result is a fusion which will use too much VMEM for its operands."` (`0xa0281ee`).

The design intent: VMEM is a correctness/feasibility constraint (the fused region must physically fit on-chip), not a quality trade-off. Encoding it as a binary gate rather than a soft penalty means a fusion either fits and is ranked on its merits, or does not fit and is excluded — there is no "slightly over budget but high value" middle ground. A reimplementer who models VMEM as a penalty will admit fusions that cannot be emitted.

---

## Multi-Output (Sibling) Fusion — `GetProfit` + Budget Gates

`TpuMultiOutputFusion` (ctor @ `0x110dcbe0`, inherits `xla::MultiOutputFusion`) is a separate pass that merges a producer with several consumers, or two siblings sharing an operand, into one tuple-rooted `kFusion`. It does **not** use the priority-queue formula above; it has its own profit number.

`TpuMultiOutputFusion::GetProfit(producer, consumer)` (@ `0x110dd0a0`) returns the **bytes saved** by reading a shared operand once instead of once per sibling:

```text
profit = Σ Target::ShapeSize(shared_operand)      // bytes read once instead of once-per-sibling
```

via `Target::ShapeSize` (@ `0x1d61a8a0`) and `ShapeUtil::TrueNumDimensions` (@ `0x1d6dde20`); the `imul` forms `ShapeSize × count` and compares against a per-`Target` threshold at `Target[+0x13c0]`. A must-fuse attribute (`UserDirectedFuseInfo::IsMustFuse`) forces the pair regardless of profit. The profit feeds `MultiOutputFusion::AddToWorkList(p, c, profit)` (@ `0x14bdf6e0`) → a base-class `priority_queue<ToBeFused>` ordered by profit. VLOG: `"Fusing instr1="` / `" instr2="` / `", the profit is ="`.

The legality and budget gates reject before a merge commits (CONFIRMED unless noted):

| guard | function | rule |
|---|---|---|
| cycle check | `MultiOutputFusion::Perform` @ `0x14bdb5a0` | reject if merge creates a cycle → `"multi-output fusion creates a cycle"` (`0x86cc9cf`) |
| max operands | `TooManyResultOperands` @ `0x110ddec0` | `TotalBufferCount(p, siblings) > Target[+0xc8]` (the ctor `long` arg = `xla_tpu_multioutput_fusion_max_operands`) → reject |
| reduce-output cap | `TooMuchReduceOutputMultiOutput` @ `0x110e1060` | per-output reduce byte cap **`0x400000` = 4 MiB** (decompiled `> 0x400000` at two sites); `CHECK "num_outputs >= instructions.size()"` (`0xa163eb7`) |
| reduce-output cap (pair) | `TooMuchReduceOutput` @ `0x110de000` | pairwise variant of the same 4 MiB cap |
| HBM pressure | `IsHBMPressureHighIfFused` @ `0x110de640` | `Σ ShapeSizeRecursive(outputs) × count > UserAllocationSharedMemoryLimitBytes(...)` (@ `0x1d616680`) → reject |
| structural legality | `LegalToFuse` @ `0x110ddc20`; `ShapesCompatibleForFusion` @ `0x110dcca0`; `IsFusible` @ `0x110dce20` | shapes tile-align for a shared iteration space; no in-place conflict; fusible op class |

The 4 MiB reduce cap prevents merging reductions whose individual outputs are large enough that the merged tuple would blow the on-chip budget; the max-operands cap bounds the fan-in of a single multi-output `kFusion`; the HBM-pressure cap bounds the total output footprint against the per-`Target` shared-memory limit. The pass is bounded by `xla_tpu_multi_output_fusion_limit` (flag string `0x84f98d5`) — a cap on how many multi-output fusions form per pass — and gated by `xla_jf_enable_multi_output_fusion` / `_advanced_…` / `_producer_consumer_…`.

> **NOTE — `Target[+0x13c0]` (MOF profit threshold) and `Target[+0xc8]` (max-operands default) are per-gen / flag-derived** and not constant-folded into `.text`; their absolute values live in the per-gen `chip_parts` resources. Confidence on those two specific integers: LOW. The *rules* that consume them are CONFIRMED.

---

## Generation Invariance of the Scoring Code

There is **exactly one** `TpuInstructionFusion` scoring path in the binary — one `CalculateProducerPriority`, one current model, one bundle model, one `NormalizedComputationCost` `switch`, one `TpuMultiOutputFusion::GetProfit`. No per-codename (viperfish / ghostlite / gfc / pufferfish) override of the cost functions exists. Per-generation behaviour enters the score **only through data**:

1. **`Target` constants** — `ChunksIn` (MXU geometry: different chunk counts per gen → different `NormalizedComputationCost`), `HbmFullChipBytesPerSecond`, `TensorCoreFrequencyInMegaHertz`, `GranuleBytes`, `ShapeSize`, `UserAllocationSharedMemoryLimitBytes`, `Target[+0xc8]`, `Target[+0x13c0]`. Same formulas, per-gen numbers.
2. **Per-gen `CycleTable` latencies** — what `GetHloCycles` / `GetCyclesIfFused` deposit per op (a bf16 matmul varies ~26× across generations), so the bundle-aware priority of a matmul-bound fusion scales with the chip while the subtraction is identical.
3. **Flag defaults** — `xla_tpu_use_bundle_aware_cost_model_for_fusions`, nested-dot / fp8 / packing-fusion enables. These gate *which model runs* and *which predicate branches admit*, but the scoring code is shared.

This mirrors the bundle-cost finding (see [Bundle-Aware Cost](../cost/bundle-aware-cost.md)): gen-invariant code over per-gen data.

---

## What Is Not Byte-Pinned

- **The secondary `double` of the priority tuple** (current model): re-stored from the same `priority` slot at several `rbp` offsets; whether it is a stability re-score or a re-stored `ActualCostReduction` is not byte-confirmed. The `long` tie *is* the input-size/chunk accumulation. (PARTIAL)
- **The absolute per-gen integers** `Target[+0x13c0]` (MOF profit threshold) and `Target[+0xc8]` (max-operands), and the `HbmFullChipBytesPerSecond` / TC-frequency values that convert the memory term to absolute bytes — per-gen, not constant-folded. (LOW for the numbers; CONFIRMED for the formulas that consume them.)
- **The exhaustive opcode→weight assignment** inside `NormalizedComputationCost` — the four weight constants the priority formula uses are confirmed; the full 106-entry jump-table decode (including the `0.0`/`2.0` tiers and the `dot`-fatal) is owned by [NormalizedComputationCost](../cost/normalized-computation-cost.md).
- **The full body of `GetCyclesIfFused`** — confirmed to return a `MaxResourceCycles` bundle cost on the merged op; its two-`ResourceVector` merge is documented on the cost pages.

---

## Function Map

| symbol | address | role |
|---|---|---|
| `TpuPriorityFusionQueue::CalculateProducerPriority` | `0x1308fa20` | dispatcher: current vs bundle, output-fusion `-1.0` gate |
| `…::CalculateProducerPriorityWithCurrentCostModel` | `0x13096160` | `mem_reduce − compute × conv_rw_count` |
| `…::CalculateProducerPriorityWithBundleAwareCostModel` | `0x130954c0` | `total_unfused − total_fused` |
| `…::EnqueueToProducerPriorityQueue` | `0x1308fb20` | insert `tuple<double,double,long>`; NaN `CHECK` at `tpu_instruction_fusion.cc:1678` |
| `…::DequeueNextInstructionAndOperandsToFuseInOrder` | `0x13090120` | pop largest key; demote infrequent-conditional producers |
| `…::OnFusingInstruction` / `InvalidateCachedCostModelState` | `0x13090900` / `0x1309c120` | re-score neighbours after a fuse |
| `…::NormalizedComputationCost` | `0x130989a0` | compute term — opcode-weight ladder × `ChunksIn` |
| `…::GetNormalizedMemoryCostReductionIfFusing` | `0x13099700` | memory term — HBM bytes saved → TC cycles |
| `…::GetNormalizedMemoryCost` / `OutputSize` / `InputSizeAt` | `0x1309a620` / `0x13097f20` / `0x1309a180` | per-tensor byte→cycle, output bytes, per-edge read bytes |
| `…::ActualCostReduction` | `0x1309a060` | negative-priority fallback re-pricing |
| `…::GetHloCycles` | `0x13097a00` (→ `CostModel::GetCycles 0x130aade0`) | per-op bundle cycles |
| `CostModel::GetCyclesIfFused` | `0x130aba40` | merged-bundle cycles |
| `CostModel::FusionWouldExceedVmemCapacity` | `0x130c4a80` | the shared VMEM hard gate |
| `fusion_util::GetConvAndRWCountsInNestedFusion` | `0x1454d240` | `conv_rw_count` multiplier |
| `Target::ChunksIn` / `ShapeSize` / `HbmFullChipBytesPerSecond` | `0x1d619900` / `0x1d61a8a0` / `0x1d6172a0` | per-gen cost constants |
| `TransferSizeUtil::ShouldPackPREDAsSingleBit` / `ElementPackingFactor` | `0x1d6b0080` / `0x1d6b03e0` | PRED single-bit packing correction |
| `TpuMultiOutputFusion::GetProfit` | `0x110dd0a0` | sibling-fusion bytes-saved profit |
| `…::TooManyResultOperands` / `TooMuchReduceOutputMultiOutput` / `IsHBMPressureHighIfFused` | `0x110ddec0` / `0x110e1060` / `0x110de640` | MOF budget gates |
| `MultiOutputFusion::Perform` / `AddToWorkList` | `0x14bdb5a0` / `0x14bdf6e0` | MOF cycle check; profit-ordered work list |

---

## Cross-References

- [Fusion Patterns](fusion-patterns.md) — the **predicate** half: `ShouldFuseImpl`'s ordered rejection cascade, the slice-like / output-fusion / duplicate-expensive gates, the custom-call registry hook. This page ranks the survivors; that page filters.
- [Compiler Overview](overview.md) — where the fusion pass sits in the lowering pipeline.
- [Compile Phases](compile-phases.md) — the pass ordering that places fusion after dot→conv rewriting.
- [Dot/Conv MXU Lowering](dot-conv-mxu-lowering.md) — why `NormalizedComputationCost` `CHECK`-fatals on `dot` (dots are rewritten to convolutions before fusion).
- [Cost Model Overview](../cost/overview.md) — the bundle/`ResourceVector` cost machinery this page's bundle-aware model consumes.
- [TPU HLO Cost Analysis](../cost/tpu-hlo-cost-analysis.md) — the per-op cycle/flop analysis behind the conv-flop compute weight.
- [NormalizedComputationCost](../cost/normalized-computation-cost.md) — the exhaustive opcode→weight `switch` and the `GetCyclesIfFused` fused-merge driver.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — `GetHloCycles` / `MaxResourceCycles`, the bundle-packing model the linear current model cannot see.
- [back to index](../index.md)
