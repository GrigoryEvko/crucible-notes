# Bundle-Aware Cost

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.rodata` addresses are virtual addresses; for this binary `.rodata` VMA == file offset (section `[11]` at `0x84a0000`), and `.text` VMA == file offset.*

## Abstract

A TPU TensorCore issues one VLIW [bundle](../isa/bundle-model-overview.md) per cycle, firing every populated slot at once into a distinct functional unit. The cost model prices a bundle accordingly: it does **not** sum the per-slot op costs, it takes their **maximum** — the slowest occupied functional-unit lane is what stalls the issue, every other lane overlaps for free. This page documents how that maximum is computed (`ResourceVector::MaxResourceCycles`), how the per-op throughput cycles are deposited into the per-lane accumulator before the reduction (`Acc` via `AccumulateInstructionUsage`), how two cost vectors combine when ops or sub-computations are packed together (`Add`), how a per-iteration cost is scaled by a loop trip count with amortized DMA startup (`ScaleResource` / `GetSubset`), and how the resulting bundle cycle count feeds the dependency-latency axis (`LatencyBetween`) and the `LatencyHidingScheduler`.

This is the bundle-level *consumer* of the slot model. The 23-slot `ResourceVector`, the `Acc` deposit, and the three-group structure of the `MaxResourceCycles` reduction are introduced on the [Resource Enum](resource-enum.md) page; the per-class throughput integers that get deposited come from the [CycleTable](cycletable-family.md) / [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) pages. This page connects them: it shows the full op-level → bundle-level → loop-level → schedule-level pipeline, byte-anchored at each step, and pins the overlap semantics the reduction encodes.

The reader who knows LLVM should hold one analogy and one divergence. The analogy: `MaxResourceCycles` is the TPU equivalent of an LLVM `MCSchedModel` resource-pressure query that asks "given the ProcResource occupancies this group consumes, what is the bottleneck cycle count?" The divergence: there is no scoreboard and no out-of-order window — the bundle word *is* the issue packet, so the max is taken once per bundle over a fixed 23-slot vector, and the only resources that serialize rather than overlap are the memory-transfer terms and the two-lane vector-ALU port balance. Everything else, including back-to-back MXU pushes, overlaps.

For reimplementation, the contract is:

- **Bundle cost is `max` over per-lane occupancies, not `sum`.** `MaxResourceCycles` reduces the 23-slot `ResourceVector` to one scalar with three rules: a 50% port-balance blend on the vector-ALU triad, a serial sum on the four memory-transfer terms, and a plain `max` over everything else (MXU, XLU, vector ports, ICI, SparseCore).
- **Per-op deposit precedes the reduction.** Each LLO op adds `(double)GetCyclesForThroughput(class)` into slot `GetResource(class)`; the bundle's vector is the slot-wise sum of its ops' deposits, then `MaxResourceCycles` collapses it.
- **Cross-vector combination ADDs every slot except the two DMA-latency slots, which `max`-combine.** `Add` (and the loop-prologue `GetSubset`) treat DMA startup as paid-once, DMA bandwidth and compute as paid-per-unit.
- **Loop scaling multiplies per-slot cost by the trip count but skips the two DMA-latency slots** (`ScaleResource`), so a software-pipelined loop pays DMA startup once and bandwidth/compute per iteration.
- **The latency axis is separate and combines per-op-pair, also by `max`.** `LatencyBetween` returns the read-after-write stall between two `LloValue`s as the max over the applicable per-gen latency-array fields, with hard floors; the scheduler combines throughput cost (this page) and dependency latency to place ops in bundles.

| | |
|---|---|
| **Bundle reduction** | `ResourceVector::MaxResourceCycles()` @ `0x1c89b9e0` — 23-slot → scalar `max` |
| **Per-op deposit** | `ResourceVector::Acc(Resource, double)` @ `0x1c89adc0`; driver `AccumulateInstructionUsage::operator()` @ `0x144fd720` |
| **Cross-vector combine** | `ResourceVector::Add(rv, AddOptions)` @ `0x1c89b820`; defaults `AddOptions::Defaults` @ `0x1c89ada0` |
| **Loop scale** | `ResourceVector::ScaleResource(Resource, double, ScaleOptions)` @ `0x1c89b6a0`; defaults `ScaleOptions::Defaults` @ `0x1c89ad80` |
| **Loop subset** | `ResourceVector::GetSubset(SubsetOptions)` @ `0x1c89bb00` |
| **Bundle assembly** | `CostModel::GetCycles` @ `0x130aade0` — `GetHloResourcesImpl` + `Add` |
| **Latency axis** | `LatencyTable::LatencyBetween(LloValue,LloValue)` @ `0x1c89f820`; JF `LatencyBetweenInternal` @ `0x1c8a0d60` |
| **Schedule entry** | `CostModelLatencyEstimator::GetLatencyBetween(HloGraphNode,HloGraphNode)` @ `0x10ff8f00` |
| **Blend constant** | `qword_A2DF5C8` = `0.5` (vector-ALU port-balance factor) |

---

## The Three-Layer Cost Pipeline

The cost of a scheduled region is computed bottom-up in three layers, each with its own combination rule. A reimplementer must keep them distinct — using `sum` where the binary uses `max`, or scaling a slot the binary leaves un-scaled, mis-prices entire op classes.

```text
op level     ── per LLO op: Acc(GetResource(class), GetCyclesForThroughput(class))
   │              into one shared 23-slot ResourceVector  (slot-wise SUM across ops)
   ▼
bundle level ── MaxResourceCycles(rv)  →  ONE scalar = slowest occupied lane
   │              (plain MAX, except VectorAlu blend + MemXfer serial sum)
   ▼
loop level   ── Add(rv_iter)            combine two regions  (per-slot SUM, DMA-latency MAX)
                ScaleResource(rv, trip)  scale per-iteration  (per-slot ×trip, DMA-latency skipped)
                GetSubset(rv, opts)      prologue/tail split  (keep input-DMA / keep compute+output)
   │
   ▼
schedule     ── LatencyBetween(a,b)     RAW dependency stall (per-op-pair MAX over latency fields)
                + bundle cycle cost   →  LatencyHidingScheduler list priority
```

> **NOTE —** the op-level deposit and the bundle-level reduction use the *same* `ResourceVector` object — ops accumulate into slots (`+=`), then the whole vector is reduced once. The vector is never reduced per-op. This is why two ops on different units cost the max of their two cycles (one bundle), while two ops on the same unit cost their sum (the slot accumulated both). See [Resource Enum](resource-enum.md) for the slot layout.

---

## Per-Op Deposit — `Acc` via `AccumulateInstructionUsage`

Before any reduction, each LLO op's throughput cycles land in exactly one slot. The canonical driver is the lambda the SDC sequence-checker uses to re-derive a bundle's usage; it is the cleanest decompile of the gen-invariant deposit path, byte-exact:

```c
// AccumulateInstructionUsage(...)::$_0::operator()  @ 0x144fd720 (decompiled, exact)
int operator()(CycleTable *ct, ResourceVector *rv, CycleTable::Instruction cls) {
    Resource r = CycleTable::GetResource(ct, cls);          // op→slot  (flat LUT 0xb438aec)
    int64 cyc  = ct->vtable[+0x10](ct, cls);                // GetCyclesForThroughput(cls)
    ResourceVector::Acc(rv, r, (double)cyc);                // rv[r*8] += cyc
    return 1;
}
```

Three facts are pinned here. (1) The op→slot map `GetResource` is a single flat `.rodata` lookup, identical across generations ([CycleTable Family](cycletable-family.md)). (2) The throughput cycle is the per-gen virtual at vtable slot `+0x10` (`GetCyclesForThroughput`). (3) The deposit is `Acc` — a `+=` into the named slot, not an overwrite, so multiple ops on the same lane accumulate. `Acc` (`0x1c89adc0`) bounds-checks `resource < 0x17` (23 slots) with a trapping `ud1` and does `rv[resource*8] += cycles` over the 8-byte `double` stride.

For non-MXU ops the emitter `Record*` paths deposit directly: `cost_model_util::RecordInputMemXferCycles` / `RecordOutputMemXferCycles` deposit into the four `MemXfer*` slots (`R[9..12]`), `RecordHloCycles` (`0x130bbfe0`) wraps the `Acc(GetResource, GetCyclesForThroughput)` path for HLO-level pricing, and the matmul/matprep family routes through the per-gen [MxuLatencyTable](mxu-latency-overview.md). All of them write the *same* `ResourceVector` that `MaxResourceCycles` will reduce.

---

## The Bundle Reduction — `MaxResourceCycles`

`MaxResourceCycles` @ `0x1c89b9e0` is the heart of bundle-aware cost. It takes a filled 23-slot `ResourceVector` and returns one `double`: the bundle's issue cost in cycles. The decompile is a flat assembly block; reduced to its three rules:

```c
// xla::jellyfish::ResourceVector::MaxResourceCycles()  @ 0x1c89b9e0 (decompiled, exact shape)
double MaxResourceCycles(ResourceVector *rv) {
    double a = rv[3], b = rv[4], c = rv[5];        // VectorAlu0 / VectorAlu1 / VectorAluAny
    if (c > 0.0) {                                  // (A) 2-lane vector-ALU port balance
        double d = min(a - b, c);                   //   move "any" work to the less-busy lane
        c -= d; b += d;                             //   (vsubsd/vminsd/vsubsd/vaddsd, [+0x18..+0x28])
        c *= 0.5;                                    //   residual overlaps at 50% (qword_A2DF5C8)
        a += c; b += c;
    }
    double acc = max(a, b);                          //   vec_alu bottleneck
    double mem = rv[9] + rv[10] + rv[11] + rv[12];   // (B) MemXfer serial sum [+0x48..+0x60]
    acc = max(acc, mem);
    // (C) plain MAX over every other slot, in physical order:
    acc = max(acc, rv[0]);   // R[0]  Matpush      [+0x00]
    acc = max(acc, rv[1]);   // R[1]  Matmul       [+0x08]
    acc = max(acc, rv[2]);   // R[2]  Xlu          [+0x10]
    acc = max(acc, rv[6]);   // R[6]  VectorEup    [+0x30]
    acc = max(acc, rv[7]);   // R[7]  VectorLoad   [+0x38]
    acc = max(acc, rv[8]);   // R[8]  VectorStore  [+0x40]
    for (r in 13..22)        // R[13..18] ICI, R[19..21] SparseCore, R[22] reserved
        acc = max(acc, rv[r]);                       // [+0x68..+0xb0]
    return acc;
}
```

The decompile reads the slots in exactly this physical order — `[+0x18]`/`[+0x20]`/`[+0x28]` for the blend triad, `[+0x48]`+`[+0x50]`+`[+0x58]`+`[+0x60]` for the memory sum, then a run of `vmaxsd` over `[+0x00]`, `[+0x08]`, `[+0x10]`, `[+0x30]`, `[+0x38]`, `[+0x40]`, and `[+0x68]` through `[+0xb0]`. The `0.5` is the literal `qword_A2DF5C8`.

The three rules encode three hardware overlap models:

| Group | Slots | Rule | Hardware meaning |
|---|---|---|---|
| (A) vector-ALU | `R[3]`,`R[4]`,`R[5]` | port-balance + 50% residual | two dedicated ALU lanes + one "any" lane that load-balances; un-balanceable residual overlaps half |
| (B) memory | `R[9]`,`R[10]`,`R[11]`,`R[12]` | serial **sum** | input-DMA startup + input bytes + output-DMA startup + output bytes happen in sequence |
| (C) everything else | `R[0..2]`,`R[6..8]`,`R[13..22]` | plain **max** | independent functional units (MXU pipes, XLU, vector ports, ICI links, SparseCore) overlap fully |

> **GOTCHA — the MXU pipes are in the plain-`max` group.** `R[0]` (Matpush) and `R[1]` (Matmul) overlap, so back-to-back MXU gain-pushes and matmul issues in one bundle cost the max of their cycles, not the sum. A cost model that serializes MXU ops over-prices every matmul-bound bundle by a large factor — on `6acc60406` a single bf16 matmul/latch costs 212 cycles ([Per-Opcode Cycle Constants](per-opcode-cycle-constants.md)); summing two of them yields 424 where the hardware (and this model) charges 212. Trust the max.

> **GOTCHA — only memory transfers serialize.** The single `sum` in the whole reduction is the four `MemXfer*` terms. Everything else is `max`/blend. A reimplementer who serializes any compute lane diverges from the binary immediately.

### Worked example

A bundle with (all cycle integers are the `6acc60406` column of [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md)): one bf16 matmul/latch issue (class `0x05`, deposits 212 into `R[1]`), one primary matrix-result read (class `0x1b`, deposits 127 into `R[2]`), and an input DMA (deposits 30 startup into `R[9]` + 64 bandwidth into `R[10]`; the DMA terms are illustrative, not byte-pinned).

```text
R[1]=212  R[2]=127  R[9]=30  R[10]=64                  (other slots 0)
mem  = R[9]+R[10]+R[11]+R[12] = 30 + 64 + 0 + 0 = 94    (group B serial sum)
acc  = max( vec_alu=0, mem=94 ) = 94
acc  = max( 94, R[0]=0, R[1]=212, R[2]=127, ... ) = 212  (group C plain max)
bundle cost = 212 cycles
```

The matmul lane is the bottleneck; the matrix-result read and the DMA overlap under it for free. This is the entire point of the model.

---

## Bundle Assembly — `CostModel::GetCycles`

`CostModel::GetCycles` @ `0x130aade0` is where a single HLO instruction's per-op resources are gathered and combined into the accumulator the caller will reduce. It is the bundle-level entry the scheduler calls per node.

```c
// xla::jellyfish::CostModel::GetCycles(hlo, opts, *out_rv)  @ 0x130aade0 (decompiled, paraphrased)
ResourceVector tmp = {};                                 // 184-byte zeroed RV (v31)
Status s = CostModel::GetHloResourcesImpl(&tmp, hlo, opts, ...);  // fill slots via Acc/Record*
if (!s.ok()) return s;                                   // AddSourceLocationImpl(cost_model.cc:2025)
double scalar = tmp.scalar_cycles;                       // var_220 → var_430, the cross-bundle scalar
AddOptions ao = ResourceVector::AddOptions::Defaults();
ResourceVector::Add(out_rv, &tmp, &ao);                  // combine tmp into the running accumulator
out_rv->scalar = scalar;                                 // [rbx+8]
```

`GetHloResourcesImpl` populates `tmp` slot-by-slot (the `Acc`/`Record*` deposit family above). `GetCycles` then combines `tmp` into the caller's accumulator with `Add` using the default `AddOptions`. The scalar at `[rbx+8]` carries the non-vectorizable cycle term (e.g. fixed transcendental estimates) alongside the vector. The caller later calls `MaxResourceCycles` on the accumulated vector and adds the scalar — see the loop emitter below for the explicit `MaxResourceCycles + scalar` sequence.

---

## Cross-Vector Combination — `Add`

When two regions are packed (two ops in a fused computation, two sub-bundles), their cost vectors combine with `Add` @ `0x1c89b820`. The rule is **per-slot sum for almost everything, but `max` for the two DMA-latency slots**:

```c
// xla::jellyfish::ResourceVector::Add(this, other, AddOptions)  @ 0x1c89b820 (decompiled, paraphrased)
//   most slots:     this[i] += other[i]
//   slots 9 and 11: if (AddOptions.flag == 1)  this[i] = max(this[i], other[i])   else  +=
for (int i = 0; i < 23; ++i) {
    bool is_dma_latency = (i & 0x1D) == 9;               // selects exactly i==9 and i==11
    if (is_dma_latency && AddOptions.flag == 1)
        this[i] = max(this[i], other[i]);                // DMA startup paid once, not twice
    else
        this[i] += other[i];                             // bandwidth + compute accumulate
}
AddToOperandBytesAccessed(this, other);                  // merge per-operand byte maps (+0xb8)
AddToOutputBytesAccessed(this, other);                   // merge per-output  byte maps (+0xd8)
```

The mask `(i & 0x1D) == 9` is the binary's compact selector for indices 9 and 11: `9 & 0x1D == 9` and `11 & 0x1D == 9`, while every other index in `[0,22]` maps elsewhere. Those two indices are `MemXferInputLatency` (`R[9]`) and `MemXferOutputLatency` (`R[11]`) — the DMA *startup* terms. The leading SIMD block in the decompile (`vcmpnltpd` + `vmaskmovpd` over `[+0x40]`/`[+0x50]`) is the vectorized form of this same "max the latency slots, add the bandwidth slots" decision for the `R[8..11]` window; the scalar tail loop handles the rest.

> **NOTE — the amortized-DMA model.** Combining two sub-vectors should not double-count a DMA's fixed startup latency: if both sub-regions read from the same input stream, the engine pays the startup once and the bandwidth twice. `Add` encodes exactly that — `max` on the two latency terms, `sum` on the two bandwidth terms (`R[10]`, `R[12]`) and on all compute. The `AddOptions.flag` (its `Defaults` is `AddOptions::Defaults` @ `0x1c89ada0`) toggles this; with the flag off, latency terms also `sum`.

---

## Loop Scaling — `ScaleResource` and `GetSubset`

A software-pipelined loop body is priced once and multiplied by the trip count, but the DMA startup must not scale. `ScaleResource` @ `0x1c89b6a0` multiplies one slot by a scalar, **skipping the two DMA-latency slots**:

```c
// xla::jellyfish::ResourceVector::ScaleResource(Resource r, double factor, ScaleOptions)  @ 0x1c89b6a0
if ((r & 0xFFFFFFFD) == 9 && !ScaleOptions.flag)         // r==9 or r==11  (DMA latency)
    return;                                              //   startup is paid once → do not scale
if (r >= 0x17) trap();                                   // bound: 23 slots
this[r] *= factor;                                       // bandwidth/compute scale linearly
// r==10 / r==12: also scale the per-operand / per-output byte-map entries (flat_hash_map walk)
```

`(r & 0xFFFFFFFD) == 9` is again the "is this a DMA-latency slot" test (clears bit 1, so `9` and `11` both match `9`). When scaling the *bandwidth* slots (`r==10`, `r==12`) the function additionally walks the `flat_hash_map` of per-operand / per-output bytes-accessed and scales each entry, because byte volume scales with the trip count.

`GetSubset` @ `0x1c89bb00` partitions a per-iteration vector into prologue / steady-state / tail subsets, controlled by four boolean `SubsetOptions` flags (`a3[0..3]`). It zero-initializes the destination, then conditionally `+=`-accumulates each source slot subject to the same `(idx & 0x1D) != 9` exclusion of the DMA-latency terms in some flag combinations:

```c
// xla::jellyfish::ResourceVector::GetSubset(SubsetOptions)  @ 0x1c89bb00 (decompiled, paraphrased)
//   dest := 0
//   for each slot i in 0..22, gated by the four flag bits:
//       if slot selected:  dest[i] += src[i]
//   the per-flag predicates exclude the DMA-latency slots (i==9, i==11) via (i & 0x1D) != 9
//   flag[1] also merges operand-bytes (+0xb8);  flag[3] also merges output-bytes (+0xd8)
```

The net effect, combined with `ScaleResource`: a loop's total cost is `prologue (input-DMA startup, paid once)` + `trip_count × (compute + DMA bandwidth)` + `tail (output-DMA, paid once)`. `MaxResourceCycles` reduces each composed vector to a scalar bundle cost.

---

## The Reduction in Context — A Real Caller

`PartialReduceEmitter::EstimateCycles` @ `0x10eac440` shows the full pattern end to end and is the clearest byte-anchored proof that `MaxResourceCycles` is the terminal bundle-cost step:

```c
// xla::jellyfish::PartialReduceEmitter::EstimateCycles(op_windows)  @ 0x10eac440 (decompiled, paraphrased)
ResourceVector rv = {};                                  // local 23-slot vector (v45..)
int64 compute = 0;
for (each input window) {
    compute += per_window_cycles * Product(window_shape); // scalar compute term (v62)
}
CycleTable *ct = CycleTable::Create(target);
for (each operand) {                                      // deposit memory cost into rv slots
    RecordInputMemXferCycles(rv, param, ...);             // → R[9], R[10]
    RecordOutputMemXferCycles(rv, param, ...);            // → R[11], R[12]
}
double bundle = ResourceVector::MaxResourceCycles(&rv);   // reduce 23 slots → scalar
int64  cyc    = (int64)bundle;                            // vcvttsd2si
result->cycles = cyc + compute;                           // bundle bottleneck + compute term
```

The deposits fill the memory slots; `MaxResourceCycles` collapses the vector to the bottleneck lane; the result is `(int)max-lane + compute`. The integer conversion is the literal `vcvttsd2si rax, xmm0` immediately after the `MaxResourceCycles` call, and the add (`rax + v62`) stores into the emitter's cycle field. The same `MaxResourceCycles → (int) → + scalar` shape appears in `BaseCostModelMetricCalculator::Calculate` (`0x1304ee00`), `CostModel::GetLoopFusionOrUnfusedHloCycles` (`0x130b2bc0`), `CostModel::GetCyclesIfFused` (`0x130aba40`), `CostModel::GetOutputFusionOrConvolutionCycles` (`0x130aede0`), and `ParamInput::EstimateComputeCycles` (`0x1126ee20`). The one caller that is *not* a cost emitter is the SDC sequence-checker's `MaybeInjectMxuSequences::$_3` (`0x144fc5c0`), which calls `MaxResourceCycles` to re-derive a candidate bundle's issue cost while validating injected MXU sequences — the same translation unit as the `AccumulateInstructionUsage` deposit lambda above. Those seven functions are the complete caller set for `MaxResourceCycles` in the binary.

---

## The Dependency-Latency Axis — `LatencyBetween`

Bundle throughput cost (above) is one of two inputs to the scheduler. The other is the read-after-write dependency latency between two ops — the minimum cycle gap the scheduler must insert before a consumer can read a producer's result. This is a *different* table family ([CycleTable Family](cycletable-family.md) documents the split) and combines per-op-*pair*, not per-bundle.

`LatencyTable::LatencyBetween(LloValue, LloValue)` @ `0x1c89f820` is the public dispatcher. It calls the per-gen `LatencyBetweenInternal` virtual, then applies two corrections that are gen-invariant:

```c
// xla::jellyfish::LatencyTable::LatencyBetween(a, b)  @ 0x1c89f820 (decompiled, paraphrased)
int lat = this->vtable[+0x18](this);                     // base: per-gen LatencyBetweenInternal
if (this->jitter_bitgen)                                  // optional: +Uniform[0,101) jitter (+0x10)
    lat += UniformInt(0, 101);
uint16 oa = a->opcode, ob = b->opcode;
if (oa == 132 && ob == 132)                               // matmul→matmul: floor from AutoOr<int>
    lat = max(lat, autoOr.value_or(16));
else if (oa == 130 && (ob - 130) <= 2 && lat < 3)         // matprep→matres family: floor 2
    lat = 2;
return lat;                                               // VLOG(2): "resolved latency from <a> to <b>: N"
```

The per-gen `LatencyBetweenInternal` is itself a per-op-pair `max` over the relevant fields of the per-gen latency array. The Jellyfish form `LatencyTableJellyfish::LatencyBetweenInternal` @ `0x1c8a0d60` reads byte-exactly as: start `v15 = 1`, then for each matching producer/consumer opcode relationship raise `v15` to the corresponding latency-array field (`v5[6]` for store-then-load chains, `v5[7]` for pseudo-EUP, `v5[8]`/`v5[11]`/`v5[19]`/`v5[20]` for the RPU/result-FIFO classes, `v5[9..18]` for the matprep/matmul/transcendental classes keyed on the latch-mode high bits), with a hard floor of `4` (raised to `5` for an indexed-store-followed-by-load and a set-IAR-followed-by-indexed-load). The combination operator throughout is `if (v15 <= field) v15 = field;` — i.e. `max`. So the dependency latency between two ops is the maximum of all applicable RAW-hazard fields, exactly mirroring the bundle-cost `max`.

> **NOTE — two `max`es, two axes.** `MaxResourceCycles` takes the max over *functional-unit lanes within one bundle* (throughput / occupancy). `LatencyBetween` takes the max over *hazard fields for one op pair* (dependency / stall). Both are `max`, both come out of the same per-gen `Performance` object ([Performance Family Overview](performance-overview.md)), but they answer different questions and the scheduler consumes both.

---

## Feeding the Scheduler

`CostModelLatencyEstimator::GetLatencyBetween(HloGraphNode, HloGraphNode)` @ `0x10ff8f00` is the HLO-graph-level estimator the [LatencyHidingScheduler](../sched/overview.md) queries. It memoizes results in a `flat_hash_map<HloInstruction*, double>` (at estimator `+0x18`/`+248`), and for the compute-bearing opcode pairs it routes through `CostModel::GetCycles` (the bundle-assembly path above) and then through the per-bundle `MaxResourceCycles` reduction. For async / collective edges it applies the collective overlap model instead — PCIe and DCN bandwidth terms (`available_pcie_bandwidth`, `kDcnEstimatedBandwidth`, `kPcieMinimumTime`, `kDcnMinimumTime`, all lazily computed once via `__cxa_guard` on first call) and SparseCore custom-call estimates (`GetSparseCoreCustomCallCostEstimateLatency`, `GetRadixSortEstimatedExecutionCycles`, gather/scatter offload estimators). The cycle counts it returns are converted to wall-clock seconds via `Target::TensorCoreFrequencyInMegaHertz` (the `kCyclesPerMicrosecond` guard variable, equal to the TC clock in MHz; see [Cost Model Overview](overview.md)).

The scheduler then uses these two quantities — per-node bundle cost (this page's `MaxResourceCycles`) and per-edge dependency latency (`LatencyBetween`) — to compute its list-scheduling priority: it overlaps async work under compute when the dependency latency permits, packing independent ops into bundles whose cost is the max-lane occupancy rather than the sum. The scheduler internals (list priority, async tracking, the distinct 47-ID `ResourceType` concurrency model) live on the [Scheduler Overview](../sched/overview.md) and [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) pages.

> **GOTCHA —** The bundle cost is a per-lane `max`, never a global sum. The only addition in the whole pipeline is the four-term memory group inside the reduction and the per-slot `Add` across packed vectors (which itself `max`-combines the two DMA-startup terms). Pricing a bundle as a sum of slot costs mis-prices every multi-lane bundle.

---

## Cross-References

- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector`, the `Acc` deposit, and the introduction of the `MaxResourceCycles` three-group reduction this page consumes.
- [CycleTable Family](cycletable-family.md) — `GetResource` (op→slot) and `GetCyclesForThroughput` (per-class throughput) that feed the per-op deposit; the throughput-vs-latency split.
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen cycle integers deposited into the slots before the reduction.
- [Cost Model Overview](overview.md) — the three class families, the `Target` clock-frequency wiring, and the factory dispatch behind every cycle number.
- [Performance Family Overview](performance-overview.md) — the per-gen `Performance` grid both the throughput and the latency axes read.
- [MXU Latency Overview](mxu-latency-overview.md) — the per-`(MatmulModifier × Resource)` reservation matrices that supply the matmul/matprep deposits.
- [ResourceType Taxonomy](../sched/scheduler-resourcetype-model.md) — the scheduler's distinct 47-ID concurrency-cap enum (not the cost-model `Resource`).
- [Scheduler Overview](../sched/overview.md) — the `LatencyHidingScheduler` consumer of the bundle cost and dependency latency.
- [Bundle Model Overview](../isa/bundle-model-overview.md) — the VLIW bundle word whose issue cost this page prices.
- [Learned Cost-Model Client](learned-cost-model-client.md) — why every number here is a static `.rodata` constant, not an ML prediction.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / Core model — [back to index](../index.md)
