# NormalizedComputationCost

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, **not** stripped — every symbol is a demangled C++ name). Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. All addresses are VMA. Other libtpu builds will differ.*

## Abstract

`TpuPriorityFusionQueue::NormalizedComputationCost` (@ `0x130989a0`) is the **compute term** the TPU priority-fusion queue subtracts when it ranks a producer→consumer fusion. It answers one question per HLO op — *how much vector/matrix work does this op add to a fused region* — and returns a single `double`. Unlike the bundle-occupancy model ([TpuHloCostAnalysis](tpu-hlo-cost-analysis.md)), which deposits per-op cycles into a 23-slot `ResourceVector`, this function collapses each op to a scalar weight multiplied by `Target::ChunksIn(shape)` (the op's chunk-granule element count). The LLVM analogue is a `TargetTransformInfo::getInstructionCost` query that returns a coarse "expense class" rather than a cycle-accurate latency — and like `TTI`, it is a ladder of a few discrete weights, not a continuous model.

The weight ladder is a `switch` over `HloOpcode` compiled to a 106-entry self-relative jump table at `.rodata 0xae0dcdc` (`index = opcode − 0x18`, `opcode > 0x81 → default`). It resolves to exactly **six scalar tiers** — `0.0` (free data-layout ops), `1.0` (the default cheap elementwise), `2.0` (a fused parameter read), `4.0` (logistic / reduce / cross-lane broadcast), `10.0` (divide), `42.0` (erf) — plus three structural escapes: a convolution that is priced by `flop_count / peak` rather than the ladder, a `fusion` that is priced by **recursively summing** its body's per-op costs, and a `dot` that is a `CHECK`-fatal because dots must have been lowered to convolutions before this point.

`GetCyclesIfFused` (@ `0x130aba40`) is the other half of the priority formula — the *bundle* cost of fusing a `(producer, consumer)` pair. It is **not** a hand-written producer+consumer merge; it builds a `FusionState` that augments the consumer's operand set with the producer, then re-prices the merged op through the same `GetHloResourcesImpl` machinery used for any fusion. The cross-functional-unit "packing" — back-to-back MXU ops overlap, the producer→consumer HBM round-trip is dropped, the input-DMA startup latency is paid once — happens inside that shared machinery (`ScaleAndSumOutputFusionResourceVectors` @ `0x130b8320`), which is what makes `total_fused < total_unfused` and the fusion priority positive. This page documents both: the opcode→weight table with its byte-exact constants, and the `GetCyclesIfFused` fused-merge driver.

For reimplementation, the contract is:

- The six-tier opcode→weight table, the `.rodata` weight constants, and the per-class rationale (why `divide=10`, `erf=42`, why `reduce` uses the *operand's* `ChunksIn` and `parameter` is `×2` only for the first two slots).
- The LoopFusion operand-accumulation pre-path and its element-type gate mask.
- The broadcast cross-lane path and the convolution flop→cycle formula.
- The `fusion` recursive-sum-and-cache path (it is *not* a pure lookup).
- `GetCyclesIfFused`'s eligibility gates, the conv-like main-op selection, the max-pool `FLT_MAX` sentinel, and the `FusionState → GetHloResourcesImpl → ResourceVector::Add` merge driver.
- The four-sub-emitter `ScaleAndSumOutputFusionResourceVectors` combine with its explicit slot-9/11 MAX-combine.

| | |
|---|---|
| **Compute weight** | `TpuPriorityFusionQueue::NormalizedComputationCost(HloInstruction*, long)` @ `0x130989a0` |
| **Jump table** | `.rodata 0xae0dcdc` — 106 × `i32` self-relative (`index = opcode − 0x18`) |
| **Weight tiers** | `0.0`, `1.0`, `2.0`, `4.0`, `10.0`, `42.0` + conv-flop / fusion-recurse / dot-fatal |
| **Chunk count** | `Target::ChunksIn(Shape&)` @ `0x1d619900` |
| **Fused merge** | `CostModel::GetCyclesIfFused(producer, consumer, opts, ResourceVector*)` @ `0x130aba40` |
| **Merge core** | `ScaleAndSumOutputFusionResourceVectors` @ `0x130b8320` (4 sub-emitters; slots 9/11 MAX) |
| **Eligibility gate** | `IsFusionSupportedHlo` @ `0x130abee0` |
| **Source files** | `tpu_instruction_fusion.cc` (weight) · `cost_model/cost_model.cc` (fused merge) |

---

## NormalizedComputationCost — the Opcode→Weight Ladder

### Purpose

This is the **compute penalty** half of the bundle-aware fusion priority (`priority = total_unfused − total_fused`). For a candidate fusion edge it estimates how much vector/matrix work the consumer (or, recursively, a nested fusion) adds. The result is one `double`; the priority queue subtracts it scaled by the user count. The function takes `(HloInstruction* inst, long operand_index)`: `rbx` is the owning `TpuPriorityFusionQueue this` (holding the conv flop cache and a `Target*` at `this+0x170` / `*((q*)this+46)`); `inst` is the op being priced; `operand_index` drives both a recursion guard and the parameter-slot tier.

### Entry Point

```text
NormalizedComputationCost (0x130989a0)                 ── double, computes a scalar weight
  ├─ (operand_index>0 + binary + iota/broadcast op0)   ── recursion guard → flop/return tail
  ├─ LoopFusion pre-path (0x13098a13)                  ── multi-operand elementwise estimate
  │     └─ Target::ChunksIn (0x1d619900) × (1 + #non-zero-minor operands)
  └─ per-opcode switch (0x13098b62)                    ── jump table @ .rodata 0xae0dcdc
        ├─ 0.0  block  0x130995ee   (layout/metadata ops)
        ├─ 1.0  block  0x13098e2f   (DEFAULT — cheap elementwise)
        ├─ 2.0  block  0x13098de1   (parameter, operand_index ≤ 1)
        ├─ 4.0  blocks 0x13098d8e / 0x13098d74 / 0x13098b87  (logistic / reduce / broadcast)
        ├─ 10.0 block  0x13098d52   (divide)
        ├─ 42.0 block  0x13098e49   (erf)
        ├─ conv block  0x13098db0   (flop_count / peak — see CONV FORMULA)
        ├─ fusion block 0x13098e09  (cached, else recursive sum of body)
        └─ dot block   0x130996c5   (CHECK-fatal "Dots should have been replaced…")
```

### Algorithm

```c
double NormalizedComputationCost(this /*rbx*/, inst /*r14*/, operand_index /*r15*/):  // 0x130989a0
    // (0) RECURSION GUARD — when called per fused-edge with operand_index>0,
    //     a binary op whose operand(0) is iota(0x43) or broadcast(0x1a) returns 0.0.
    if operand_index > 0:
        if (inst.operand_count_field & ~1) == 2:          // [inst+0x10] low form == 2
            op0 = inst.operand(0).opcode;                 // [operand(0)+12]
            if op0 == 0x43 /*iota*/ or op0 == 0x1a /*broadcast*/:
                return 0.0;
        goto per_opcode_switch;                           // LABEL_29

    // (1) LOOP-FUSION PRE-PATH — multi-operand elementwise estimate.
    if operand_index == 0
       and inst.IsLoopFusion()
       and root_element_type in NUMERIC_MASK            // 0x2FFF91FFE | {0x20,0x21} | 0x400048000
       and inst.fused_instructions_computation().instruction_count() <= 254:  // [comp+88]
        multiplier = 1.0;                                 // qword_A2DF230
        if inst.operand_count >= 2:                       // [inst+0x10] >= 2
            for each operand i:
                op_minor   = operand(i).shape.dimensions()[0] >> 1;   // minor dim, halved
                if op_minor != 0: multiplier += 1.0;      // +1 per NON-zero-minor operand (gated)
                root_minor = root.shape.dimensions()[0] >> 1;
                if op_minor >= root_minor: break;         // operand as-wide-or-wider → abandon
            else:
                return Target::ChunksIn(root.shape) * multiplier;     // estimate kept
        // any operand as-wide-or-wider, or 0/1 operands → fall through to the switch
        goto per_opcode_switch;

per_opcode_switch:                                        // 0x13098b62
    xmm0 = 0.0;                                           // pre-zeroed (the 0.0 tier needs no store)
    op = inst.opcode;                                     // byte [inst+0xc]
    if (op - 0x18) > 0x69: goto default_1_0;
    switch (op):                                          // jump table @ .rodata 0xae0dcdc

      case 0x18,0x27,0x29,0x2A,0x43,0x61,0x81:            // bitcast/concat/constant/convert/
        return 0.0;                                       //   iota/reshape/tuple — no compute

      case 0x1A /*broadcast*/:  return BroadcastWeight(inst);          // see BROADCAST PATH
      case 0x2B /*convolution*/: return ConvolutionWeight(this, inst); // see CONV FORMULA
      case 0x32 /*divide*/:     return Target::ChunksIn(root) * 10.0;  // qword_A2DF498
      case 0x38 /*erf*/:        return Target::ChunksIn(root) * 42.0;  // qword_A2DF1A0
      case 0x47 /*logistic*/:   return Target::ChunksIn(root) *  4.0;  // qword_A2DE830
      case 0x5B /*reduce*/:     return Target::ChunksIn(operand(0).shape) * 4.0;  // OPERAND, ×4
      case 0x52 /*parameter*/:
        if operand_index <= 1:  return Target::ChunksIn(root) * 2.0;   // vaddsd self
        else:                   return 0.0;
      case 0x3D /*fusion*/:     return FusionWeight(this, inst);       // see FUSION PATH
      case 0x34 /*dot*/:        LOG(FATAL) "Dots should have been replaced by convolutions.";
                                                          // tpu_instruction_fusion.cc:863

      default:                  return Target::ChunksIn(root) * 1.0;   // vcvtsi2sd, no vmulsd
```

> **NOTE —** the `0.0` tier needs no work because `xmm0` is zeroed before the dispatch (the `vxorpd xmm0,xmm0` at `0x13098b5e`); the `1.0` default emits only a `vcvtsi2sd` of `ChunksIn` with no multiply. Every other tier is a `vmulsd` by a `.rodata` double. A reimplementation can fold the `0.0` and `1.0` cases into the chunk-count computation, but the dot case is a hard `CHECK` — it must abort, not return a number.

### The Weight Table

`root` is `inst.shape`; `ChunksIn(s) = Target::ChunksIn(s)` (@ `0x1d619900`) is the op's chunk-granule element count. Jump-table targets are byte-verified from `.rodata 0xae0dcdc`; weight constants are byte-verified from `.rodata`.

| Weight / path | Constant | Block @ | Opcodes (hex = name) | Confidence |
|---|---|---|---|---|
| `0.0` — no cost | (xmm0 pre-zeroed, `ret`) | `0x130995ee` | `0x18` bitcast, `0x27` concatenate, `0x29` constant, `0x2A` convert, `0x43` iota, `0x61` reshape, `0x81` tuple | CERTAIN |
| `1.0` — `ChunksIn(root)` (DEFAULT) | (no `vmulsd`) | `0x13098e2f` | every opcode `0x19..0x80` not listed below (cheap unary/binary elementwise, structural, collective, control-flow, I/O) | CERTAIN |
| `2.0` — `ChunksIn(root) × 2` | `vaddsd` self | `0x13098de1` | `0x52` parameter — **only** when `operand_index ≤ 1`; else `0.0` | CERTAIN |
| `4.0` — `ChunksIn(root) × 4` | `0xa2de830` = `4.0` | `0x13098d8e` | `0x47` logistic | CERTAIN |
| `4.0` — `ChunksIn(operand(0)) × 4` | `0xa2de830` = `4.0` | `0x13098d74` | `0x5B` reduce (priced over the **reduced-over** operand, not the root) | CERTAIN |
| `4.0` — broadcast cross-lane | `0xa2de830` = `4.0` | `0x13098b87` | `0x1A` broadcast (conditional — see BROADCAST PATH) | CERTAIN |
| `10.0` — `ChunksIn(root) × 10` | `0xa2df498` = `10.0` | `0x13098d52` | `0x32` divide | CERTAIN |
| `42.0` — `ChunksIn(root) × 42` | `0xa2df1a0` = `42.0` | `0x13098e49` | `0x38` erf | CERTAIN |
| conv flop path | (see CONV FORMULA) | `0x13098db0` | `0x2B` convolution | CERTAIN |
| fusion recurse / cache | `flat_hash_map` @ `this+0x80` | `0x13098e09` | `0x3D` fusion | CERTAIN |
| dot CHECK-fatal | `LogMessageFatal` | `0x130996c5` | `0x34` dot | CERTAIN |

The jump table has exactly **11 distinct targets**: the ten listed above plus the shared `0.0` block (`0x130995ee`). Opcode→name resolution follows the alphabetical XLA `HloOpcode` enum (`0x18` kBitcast … `0x81` kTuple); the load-time values used by the function — `0x34` dot (fatal), `0x38` erf, `0x47` logistic, `0x52` parameter, `0x5B` reduce — are corroborated by the function's own opcode comparisons (e.g. `GetCyclesIfFused` tests `opcode != 91` for the reduce special-case, `91 == 0x5B`).

### Considerations — Why the Ladder Has These Tiers

The four scalar tiers above `0.0`/`1.0` rank **elementwise expense**, not op category:

- **`0.0` ops are pure data-layout / metadata** — bitcast, reshape, convert, concatenate, constant, iota, tuple. Fusing them adds no functional-unit work, so they never penalise the priority. This matches the bundle model's ZERO arm ([TpuHloCostAnalysis](tpu-hlo-cost-analysis.md#recordhlocycles--the-opcodeslot-jump-table)).
- **`divide` is `10.0`** — it is the costliest *cheap* elementwise op: a reciprocal-plus-multiply micro-sequence. The bundle model agrees, expanding `divide` into a four-deposit reciprocal sequence.
- **`erf` is `42.0`** — the single most expensive elementwise op in the model: a long polynomial. This `42.0` is the **elementwise-erf** weight, *not* a matmul/conv class. Matmul and convolution are priced by the separate flop path below; there is no `×42` matmul branch.
- **`reduce` uniquely multiplies by the OPERAND's `ChunksIn`** (`operand(0).shape`), not the root's. A reduce's compute scales with the large input it reduces over, not the small reduced output — the priority mirror of `HandleReduce`'s `ExtentProduct(operand)` flop formula.
- **`parameter` is `×2` only for `operand_index ≤ 1`** — the first two fused-parameter slots cost the read-plus-forward of the param tensor; higher slots are free. This is the only tier gated on `operand_index` rather than opcode.

> **CORRECTION (NCC-1) —** an earlier reading of the priority model labeled `42.0` the "matmul/conv heavy class." The byte-exact jump table overturns this: `42.0` is the `erf` *elementwise* weight (block `0x13098e49`, constant `0xa2df1a0`), and convolution takes the **separate** `flop_count / peak` path (block `0x13098db0`). There is no opcode that maps to a `×42` matmul tier. A reimplementation that prices matmul at `×42·ChunksIn` will be wrong by orders of magnitude on large convs.

---

## The LoopFusion Operand-Accumulation Pre-Path

### Purpose

Before the per-opcode switch, a loop (elementwise) fusion gets a special multi-operand estimate at `0x13098a13`. The intuition: a loop fusion's compute is roughly its output chunk count times one unit per operand that must be broadcast/expanded to reach the output width. An operand already as wide as the output costs nothing extra; a narrower operand (needing a lane/sublane expansion) adds one unit.

### Algorithm

```c
// reached only when operand_index == 0
if inst.IsLoopFusion()
   and element_type_in_numeric_mask(root.shape.element_type)
   and fused_instructions_computation().instruction_count() <= 254:   // 0xFE cap, [comp+88]
    multiplier = 1.0;
    if inst.operand_count >= 2:
        for each operand i in [0 .. operand_count):
            op_minor   = operand(i).shape.dimensions()[0] >> 1;     // [operand.shape+8] >> 1
            if op_minor != 0: multiplier += 1.0;                    // gated: skip zero-minor operands
            root_minor = root.shape.dimensions()[0] >> 1;           // re-read [inst.shape+8] each iter
            if op_minor >= root_minor:
                goto per_opcode_switch;          // operand as-wide-or-wider → abandon estimate
        return Target::ChunksIn(root.shape) * multiplier;
    goto per_opcode_switch;                       // 0 or 1 operand → switch on the root opcode
```

The numeric element-type gate is the same mask used throughout the cost model: `_bittest64(0x2FFF91FFE, et)` for `et ≤ 0x21`, OR `(et & ~1) == 0x20`, OR `_bittest64(0x400048000, et)` for `et ≤ 0x22` — covering the numeric and packed types the bundle model can price (the same mask appears in `GetHloResourcesImpl` and `GetCyclesIfFused`). The `≤ 254` instruction-count cap bounds the estimate to small fusions.

> **GOTCHA —** the loop reads `dimensions()[0] >> 1` for *both* the operand and the root minor dimension, and the comparison is `op_minor >= root_minor`. The `>> 1` (halving) applies symmetrically, so it cancels in the comparison — it is the *minor* dimension in chunk units. The estimate is abandoned (falls to the switch on the fusion-root opcode, i.e. case `0x3D`) the instant any operand is as wide as the output; only "all operands strictly narrower than the output" keeps the estimate. Two further byte-level subtleties: (1) the `multiplier += 1.0` increment is **gated on `op_minor != 0`** — an operand whose halved minor dim is zero is counted in the loop but does *not* bump the multiplier (the `vaddsd` result is discarded when `v17 == 0` at `0x13098…`/decompile line 732); (2) `root_minor` is re-read from `inst.shape` on every iteration rather than hoisted. The kept estimate is therefore `ChunksIn(root) × (1 + #non-zero-minor operands)`, not `(1 + #operands)`. A reimplementer who returns the estimate unconditionally, or who bumps the multiplier for every operand, will over-cost fusions whose operands are already full-width or zero-minor.

---

## Broadcast Path (opcode `0x1A`, block `0x13098b87`)

### Algorithm

```c
double BroadcastWeight(inst):                              // 0x13098b87
    target = this.Target;                                  // *((q*)this+46)
    if target[0x398] == 0: return 0.0;                     // broadcast-cost flag off (Target+920)
    operand = inst.operand(0);
    if operand.shape.rank > 3: return 0.0;                  // high-rank broadcasts not priced
    phys = LayoutUtil::MakeLogicalToPhysical(inst.shape.layout);
    sort(inst.dimensions());                                // ascending
    if ShapeUtil::IsEffectiveScalar(operand.shape):         return 0.0;   // splat of a scalar
    if phys.minor_most_dim is a broadcast dimension:        return 0.0;   // free minor-axis splat
    return Target::ChunksIn(inst.shape) * 4.0;             // cross-lane movement, qword_A2DE830
```

A broadcast along the minor-most (sublane/lane) axis is a free register splat; a broadcast that materialises *across* lanes costs the `4.0` mid weight. The whole path is gated by the per-`Target` broadcast-cost flag at `Target+0x398` (= `Target+920`) — when clear, every broadcast is `0.0`.

> **QUIRK —** the bundle-occupancy model prices a broadcast at **zero** unconditionally ([TpuHloCostAnalysis](tpu-hlo-cost-analysis.md#recordhlocycles--the-opcodeslot-jump-table) ZERO arm), while this priority model may charge `4.0·ChunksIn` for a cross-lane broadcast. The two surfaces disagree on layout ops by design — the bundle path treats a broadcast as zero functional-unit occupancy; the priority path accounts for the cross-lane data movement. Do not unify them.

---

## Convolution Formula (opcode `0x2B`, block `0x13098db0`)

### Algorithm

Convolution escapes the scalar ladder entirely: it is priced by its flop count converted to MXU cycles. The flop count is cached per `unique_id` so repeated priority queries are cheap.

```c
double ConvolutionWeight(this, inst):                      // 0x13098db0
    // (1) FLOP CACHE — flat_hash_map<int64,double> at this+0xd0 / this+208
    uid  = inst.unique_id();
    flop = this.flop_cache.find(uid);
    if MISS:
        TpuHloCostAnalysis ca(this.Target, /*model_tpu_specific=*/true);   // ctor 0x130a1620
        st = ca.HandleConvolution(inst);                   // 0x1e480be0
        CHECK(st.ok()) << "hlo_cost_analysis.HandleConvolution(instruction) is OK";
                                                            // tpu_instruction_fusion.cc:871
        flop = ca.flop_count(inst);                        // 0x1e4841e0
        this.flop_cache.emplace(uid, flop)                 // CHECK emplace.second @ :875
    // (2) FLOP → CYCLES
    if inst.batch_group_count() == 1 and inst.feature_group_count() == 1:   // dense conv
        fmt        = LhsFormatForConvInstruction(inst, this.Target);  // 0x1307bd40
        peak_flops = Target::FlopsPerSecond(fmt);          // vtable+0x718, 0x1d61f280
        freq_MHz   = Target::TensorCoreFrequencyInMegaHertz();        // 0x1d615b60
        peak_per_cycle = peak_flops / freq_MHz / 1e6;      // qword_A2E0208 = 1e6
        mxu_cycles = flop / peak_per_cycle;
        valu_slots = Target::VectorAluSlotsPerTensorCore();// vtable+0x500, 0x1d61e380 (int)
        derate     = Target[0x4ac] * (-0.03) + 1.0;        // A2E05A8 = -0.03; A2DF230 = 1.0
        return (valu_slots * mxu_cycles) / derate;
    else:                                                  // grouped / depthwise conv
        return flop * 0.00048828125;                       // A2E0118 = 1/2048
```

A dense conv's cost is its flop count converted to MXU cycles at the per-LHS-format peak rate, scaled by the vector-ALU slot count and divided by a `(1 − 0.03·N)` headroom derate. Grouped convolutions — which the MXU does not accelerate as well — use a flat `flop/2048` estimate. The `HandleConvolution` flop is the same one documented in [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md#convolution--divides-by-both-group-counts) (it divides by **both** group counts); the conv-shaped state it walks is on [ConvolutionCostState](convolution-cost-state.md).

> **NOTE —** the absolute per-gen integers `Target::FlopsPerSecond(format)`, `Target::VectorAluSlotsPerTensorCore()`, and the `Target+0x4ac` derate operand are per-codename / `chip_parts`-sourced and **not** enumerated here (only the v7 chip parameters are embedded in this build). The *formula* is byte-exact; the constants are owned by the per-gen MXU pages ([MXU Latency Overview](mxu-latency-overview.md), [MatmulMode and Modifiers](matmul-mode-modifiers.md)). `LhsFormatForConvInstruction` → `MatmulDataFormat` selection is also not decoded here (MEDIUM on which peak each conv selects).

---

## Fusion Path (opcode `0x3D`, block `0x13098e09`)

### Algorithm

A `fusion` op is priced by the **sum of its body's per-op costs**, recursively, with the result cached per `HloInstruction*`. This is *not* a pure lookup — on a cache miss it walks the fused computation's root chain and recurses through `NormalizedComputationCost` for each fused instruction (with `operand_index` advanced, which is exactly what the recursion guard at the top of the function protects against double-counting iota/broadcast operands).

```c
double FusionWeight(this, inst):                           // 0x13098e09
    // cache: flat_hash_map<HloInstruction*, double> at this+0x80 / this+128
    hit = this.fusion_cache.find(inst);                    // 0x13098e09
    if hit: return hit->second;

    comp = inst.fused_instructions_computation();          // 0x13098f1c
    cost = 0.0;
    // walk the fused instruction list (16-byte stride), skipping null slots,
    // tracking a (lo,hi) index pair into the computation's instruction array:
    for each fused_instruction f in comp:                  // LABEL_105..LABEL_112
        cost += NormalizedComputationCost(this, f, edge_index);   // recursion @0x13098f50
    this.fusion_cache.emplace(inst, cost);                 // SOO flat_hash_map insert
    return cost;
```

> **CORRECTION (NCC-2) —** an earlier reading described case `0x3D` as a pure cache lookup whose miss is "computed elsewhere." The decompile shows the miss branch (`0x13098f18` onward) **computes the cost in place** by recursing `NormalizedComputationCost` over every instruction of `fused_instructions_computation()` and accumulating into `xmm0` (the `vaddsd xmm0, var_30, xmm0` at `0x13098fd0`), then stores the sum into the `this+0x80` map. The cache is populated by `NormalizedComputationCost` itself on the parent fusion's first query, not by an external pass.

The recursion guard at the function's top (`operand_index > 0` + binary op + `operand(0)` is iota/broadcast → `0.0`) prevents a fused binary op from charging for an iota/broadcast operand that the loop pre-path or a sibling edge already accounts for.

---

## GetCyclesIfFused — the Fused Bundle Merge

### Purpose

`GetCyclesIfFused` (@ `0x130aba40`) returns the **bundle cost of fusing a `(producer, consumer)` pair** — the `total_fused` term of the priority formula. Its key structural fact: it does *not* hand-merge two `ResourceVector`s. It treats the consumer as a fusion whose operand set is augmented by the producer (a `FusionState`), then prices that merged op with the same `GetHloResourcesImpl` used for any fusion. The cross-FU packing therefore happens inside the shared machinery, not in this function. The signature is `StatusOr<double> GetCyclesIfFused(producer, consumer, const PerCalculationOptions&, ResourceVector* out)`; `rbx` is the `StatusOr<double>*` return slot, the producer arrives in `a4`/`v19`, the consumer in `a3`, and `out` in `a5`/`a6`.

### Entry Point

```text
GetCyclesIfFused (0x130aba40)                          ── StatusOr<double>
  ├─ IsFusionSupportedHlo      (0x130abee0)            ── eligibility gate → trivial 1.0 cy
  ├─ numeric element-type mask (0x2FFF91FFE | …)       ── non-numeric → 1.0 cy
  ├─ ShapeUtil::IsZeroElementArray                      ── (consumer ≠ reduce) zero-elem → 1.0 cy
  ├─ IsConvLowerable (0x14553620) / ExtractConvLikeHlo (0x1d6aa140)  ── conv-like main-op pick
  │     └─ GetReduceWindowType (0x1454d4a0) == -1/2     ── max-pool → FLT_MAX sentinel
  ├─ FusionState::Create(consumer, producer) (0x130ab320)           ── combined operand set
  ├─ GetHloResourcesImpl(consumer, opts, &fs, isFused=1) (0x130aa580) ── price merged op
  │     └─ ScaleAndSumOutputFusionResourceVectors (0x130b8320)       ── 4-emitter combine
  └─ ResourceVector::Add(out, merged, Defaults) (0x1c89b820)         ── fold into caller's out
```

### Algorithm

```c
StatusOr<double> GetCyclesIfFused(producer /*a4*/, consumer /*a3*/, opts, out /*a5*/):  // 0x130aba40
    // (A) ELIGIBILITY GATES — return a trivial 1.0-cycle cost if the op is not modellable.
    if !IsFusionSupportedHlo(consumer, opts.target)        // 0x130abee0
       or consumer.shape.element_type not in NUMERIC_MASK  // 0x2FFF91FFE | {0x20,0x21} | 0x400048000
       or (consumer.opcode != 0x5B /*reduce*/              // reduce is exempt from the next test
           and ShapeUtil::IsZeroElementArray(producer.shape)):
        out.scalar = 0;                                    // *(this+1) = 0
        return StatusOr<double>(1.0);                      // *(this) = 1  (1 trivial cycle)

    // (B) CONV-LIKE MAIN-OP SELECTION — pick the conv/reduce-window "hero" between the two ops.
    main = consumer;                                       // default
    for cand in {producer, consumer}:                      // probe both
        if IsConvLowerable(cand):                          // 0x14553620
            conv = ExtractConvLikeHlo(cand);               // 0x1d6aa140
            if conv and conv.opcode == 0x5E /*reduce-window*/:
                t = GetReduceWindowType(conv);             // 0x1454d4a0
                // (C) MAX-POOL SENTINEL: a max-pool (t==2) or unknown (t==-1) window is not
                //     bundle-cost-modellable → emit FLT_MAX cycles so it never fuses.
                if t == 2 or t == -1:
                    out.resources[…] = FLT_MAX;            // 0x47EFFFFFE0000000 = qword_A2E0530
                    return StatusOr<double>(out.MaxResourceCycles());   // = FLT_MAX
            else:
                main = cand;                               // this cand is the conv-lowerable main op

    // (D) BUILD THE MERGED FUSION STATE — consumer augmented by producer's operands.
    FusionState fs;
    FusionState::Create(&fs, consumer, producer);          // 0x130ab320
    //   fs records consumer.operands() + producer.operands() as one combined set, and the
    //   per-producer operand-indices at which the consumer USES the producer (the internal edges).

    // (E) PRICE THE MERGED OP AS A FUSION.
    StatusOr<ResourceVector> merged =
        GetHloResourcesImpl(consumer, opts, &fs, /*isFused=*/1);   // 0x130aa580
    if !merged.ok(): return merged.status();               // propagate w/ source loc cost_model.cc:2154

    double fused_cycles = merged.value().scalar_cycles;    // [merged + 0x240-ish]

    // (F) FOLD THE MERGED BYTE/SLOT MAP INTO THE CALLER'S out VECTOR.
    out.Add(merged.value(), AddOptions::Defaults());       // 0x1c89b820 — slots 9/11 MAX, rest ADD
    return StatusOr<double>(fused_cycles);
```

`GetHloResourcesImpl` walks the consumer's fused expression plus the producer (via `fs`). For each operand edge it consults `IsProducerUse` (@ `0x130ab0c0`): edges recorded in the `FusionState` are **internal**, so their input-DMA bytes are *not* deposited into the `MemXfer` slots `R[9..12]` — this is what drops the producer→consumer HBM round-trip from the fused cost. The producer's compute is accumulated into the *same* `ResourceVector` as the consumer's, and the sub-emitter costs are combined by `ScaleAndSumOutputFusionResourceVectors`.

### The Max-Pool FLT_MAX Sentinel

The one early-exit worth calling out: a conv-like op that resolves to a `reduce-window` (opcode `0x5E`) whose `GetReduceWindowType` is `2` (max-pool) or `-1` (unknown) is **not** bundle-cost-modellable. Rather than mis-price it, the function writes the `FLT_MAX` bit pattern `0x47EFFFFFE0000000` (= `.rodata 0xa2e0530`, byte-verified) into the result `ResourceVector`'s slots and returns `MaxResourceCycles()` over it — an effectively infinite cost that guarantees the fusion is never chosen. This mirrors the same `t ∈ {−1, 2}` reduce-window sentinel in `GetHloResourcesImpl`'s routing ([TpuHloCostAnalysis](tpu-hlo-cost-analysis.md#gethloresourcesimpl--the-five-way-routing-dispatch)).

---

## ScaleAndSumOutputFusionResourceVectors — the Per-FU Combine

### Purpose

This (@ `0x130b8320`) is the routine `GetHloResourcesImpl` invokes to fuse the sub-emitter costs of a merged op into one `ResourceVector`. It combines up to **four** sub-emitter vectors — activations, kernel, output, and conv-compute — each scaled by its own iteration count, with the memory-transfer *latency* slots (9 and 11) MAX-combined rather than summed.

### Algorithm

```c
ResourceVector ScaleAndSumOutputFusionResourceVectors(            // 0x130b8320
        out, rv_act,  act_count,
             rv_kern, kern_count,
             rv_out,  out_count,
             rv_conv, conv_count):
    CHECK(conv_count >= act_count)  << "conv_compute_iteration_count >= activations_iteration_count";  // :1567
    CHECK(conv_count >= kern_count) << "… >= kernel_iteration_count";                                   // :1568
    CHECK(conv_count >= out_count)  << "… >= output_iteration_count";                                   // :1569
    out = 0;                                              // zero all 23 slots + byte maps

    // (1) ADD each scaled sub-emitter (default Add: every slot accumulates).
    for (rv, count, subset) in {(rv_act,  act_count,  SubsetOpts@0xae0f0c8),
                                (rv_kern, kern_count, SubsetOpts@0xae0f0cc),
                                (rv_out,  out_count,  SubsetOpts@0xae0f0d0),
                                (rv_conv, conv_count, SubsetOpts@0xae0f0d4)}:
        sub    = rv.GetSubset(subset);                    // 0x1c89bb00
        scaled = sub.GetScaled((double)count, ScaleOptions::Defaults());   // 0x1c89b3c0
        out.Add(scaled, AddOptions::Defaults());          // 0x1c89b820

    // (2) OVERRIDE slots 9 and 11 (MemXfer Input/Output LATENCY) with MAX-across-emitters × count.
    out.Acc(9,  max(rv_act[9],  rv_kern[9],  rv_out[9],  rv_conv[9])  * conv_count);   // +0x48
    out.Acc(11, max(rv_act[11], rv_kern[11], rv_out[11], rv_conv[11]) * conv_count);   // +0x58
    return out;
```

The four `GetSubset` calls use distinct `SubsetOptions` literals at `.rodata 0xae0f0c8 / 0xcc / 0xd0 / 0xd4` (each the 4-byte tuple `{0x00,0x01,0x01,0x01}`, byte-verified) — one subset shape per emitter. The final two `Acc` calls are the **explicit slot-9/11 MAX-combine**: the input-DMA and output-DMA *startup latency* terms are paid once (the maximum across the four sub-emitters), not summed per emitter — a transfer's startup latency overlaps across the fused sub-regions, while its bandwidth (slots 10/12) and the compute slots accumulate.

> **CORRECTION (NCC-3) —** the raw model notes described this routine as returning `MaxResourceCycles()`. The decompile shows it returns after `Acc(this, 11, …)` — it *builds* the combined `ResourceVector` (with slots 9/11 MAX-combined) and returns it; the final scalar reduction to cycles happens in the **caller** via a separate `MaxResourceCycles` (@ `0x1c89b9e0`). The MAX-combine of slots 9/11 is therefore an explicit per-slot override here, distinct from the `MaxResourceCycles` reduction's serial-sum of the full `{9,10,11,12}` memory group ([Resource Enum](resource-enum.md#the-maxresourcecycles-reduction)).

### Why `total_fused < total_unfused`

```text
total_unfused = GetHloCycles(producer)·user_count + Σ_users GetHloCycles(user)
   each op is its OWN bundle: the producer's output is WRITTEN to HBM (output-DMA, R[11]/R[12])
   and each user READS it back (input-DMA, R[9]/R[10]). Those DMA cycles are counted (user_count+1)×.

total_fused = Σ_users GetCyclesIfFused(producer, user)
   the producer→user edge is INTERNAL (IsProducerUse drops its input-DMA), the producer's HBM
   output write is eliminated (it stays in VMEM), and the producer's compute slots OVERLAP the
   user's in the MaxResourceCycles plain-MAX group (e.g. producer Matmul R[1] overlaps user
   VectorAlu R[3..5]) instead of being two serial bundles.

priority = total_unfused − total_fused > 0  whenever the saved DMA + the bundle-packing overlap
           exceed any duplicated compute the fusion introduces.
```

For a single-user producer the priority is `(C_p + C_u) − C_f`; for an `n`-user producer it is `(n·C_p + Σ_i C_u_i) − Σ_i GetCyclesIfFused(producer, user_i)`, where `C_p = MaxResourceCycles(RV_producer)`, `C_u` likewise for the consumer, and `C_f = GetCyclesIfFused(producer, consumer)`.

---

## IsFusionSupportedHlo — the Eligibility Gate

`IsFusionSupportedHlo` (@ `0x130abee0`) is the first gate of `GetCyclesIfFused`. It returns `false` (→ the trivial 1-cycle cost) when the consumer is not bundle-cost-modellable:

- the element type has bit-width 64 (`ShapeUtil::ElementHasBitWidth(shape, 0x40)`), unless the op is a custom fusion or a collective-compute fusion;
- the op is a zero-element array;
- the opcode is in the REJECT set `_bittest(0x2000100000400001, opcode−5)` for opcodes in `[5..0x42]` → `{0x05 all-gather-done, 0x1B call, 0x31 custom-call, 0x42 infeed}` — control-flow / host-I/O ops that have no functional-unit occupancy.

---

## Worked Example — a small elementwise fusion

A loop fusion rooted at `multiply` (`0x4B`, default tier `1.0`), three operands, output `[256,128]`:

```text
operand0 = parameter [256,128]   (root minor = 128)
operand1 = broadcast scalar      (minor << 128)
operand2 = parameter [256,128]   (minor = 128)
```

LoopFusion pre-path:

```text
multiplier = 1.0
operand0: op_minor 128 >= root_minor 128  → break immediately (operand as-wide as root)
→ estimate abandoned → fall to per-opcode switch on the root (kFusion 0x3D) → recurse the body
```

For the body's `multiply` leaf (default tier) the per-op weight is `ChunksIn([256,128]) × 1.0`. Replace the root with `divide` and the leaf is `ChunksIn × 10.0`; with `erf`, `ChunksIn × 42.0`; with a dense `convolution`, `flop_count / peak` (heavy). This is the compute term `total_unfused` subtracts; the bundle term `GetCyclesIfFused` computes via the merge above.

---

## Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuPriorityFusionQueue::NormalizedComputationCost` | `0x130989a0` | opcode→weight scalar + conv/fusion escapes | CERTAIN |
| `Target::ChunksIn(Shape&)` | `0x1d619900` | chunk-granule element count (the ×multiplier base) | CERTAIN |
| `TpuHloCostAnalysis` ctor | `0x130a1620` | conv flop sub-analysis | CERTAIN |
| `HloCostAnalysis::HandleConvolution` | `0x1e480be0` | conv flop emitter | CERTAIN |
| `HloCostAnalysis::flop_count` | `0x1e4841e0` | reads cached flop property | CERTAIN |
| `LhsFormatForConvInstruction` | `0x1307bd40` | conv LHS → `MatmulDataFormat` (peak select) | MEDIUM |
| `Target::FlopsPerSecond` | `0x1d61f280` | per-format peak (vtable+0x718) | HIGH |
| `Target::VectorAluSlotsPerTensorCore` | `0x1d61e380` | VALU slot count (vtable+0x500) | HIGH |
| `Target::TensorCoreFrequencyInMegaHertz` | `0x1d615b60` | TC clock (cycles ← seconds) | CERTAIN |
| `CostModel::GetCyclesIfFused` | `0x130aba40` | fused-pair bundle cost driver | CERTAIN |
| `IsFusionSupportedHlo` | `0x130abee0` | eligibility gate (→ 1-cycle trivial) | CERTAIN |
| `IsConvLowerable` | `0x14553620` | conv-lowerable predicate | CERTAIN |
| `ExtractConvLikeHlo` | `0x1d6aa140` | pull the conv/reduce-window root | CERTAIN |
| `GetReduceWindowType` | `0x1454d4a0` | `−1`/`2` max-pool sentinel | HIGH |
| `FusionState::Create` | `0x130ab320` | combined operand set + internal-edge map | CERTAIN |
| `CostModel::IsProducerUse` | `0x130ab0c0` | drops internal-edge input DMA | HIGH |
| `CostModel::GetHloResourcesImpl` | `0x130aa580` | prices the merged op | CERTAIN |
| `ScaleAndSumOutputFusionResourceVectors` | `0x130b8320` | 4-emitter combine; slots 9/11 MAX | CERTAIN |
| `ResourceVector::Add` | `0x1c89b820` | per-slot accumulate (Defaults) | CERTAIN |
| `ResourceVector::MaxResourceCycles` | `0x1c89b9e0` | scalar bundle-cycle reduction | CERTAIN |

### Weight / Formula Constants (`.rodata`, byte-verified)

| Address | Value | Used by | Confidence |
|---|---|---|---|
| `0xa2df230` | `1.0` | default weight / conv derate `+1.0` / multiplier base | CERTAIN |
| `0xa2de830` | `4.0` | logistic, reduce, cross-lane broadcast | CERTAIN |
| `0xa2df498` | `10.0` | divide | CERTAIN |
| `0xa2df1a0` | `42.0` | erf | CERTAIN |
| `0xa2e0530` | `3.4028e38` (FLT_MAX) | max-pool `GetCyclesIfFused` sentinel | CERTAIN |
| `0xa2e0208` | `1.0e6` | conv `freq_MHz → Hz` | CERTAIN |
| `0xa2e05a8` | `-0.03` | conv derate slope `1 − 0.03·Target[+0x4ac]` | CERTAIN |
| `0xa2e0118` | `0.00048828125` (1/2048) | grouped-conv flop→cost factor | CERTAIN |

---

## Related Components

| Component | Relationship |
|---|---|
| [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) | Supplies the conv `flop_count` this page caches; the bundle-occupancy peer of the scalar ladder |
| [Resource Enum (23-slot)](resource-enum.md) | The `ResourceVector` slots and `MaxResourceCycles` reduction the fused merge feeds |
| [ConvolutionCostState](convolution-cost-state.md) | The conv-shaped state `HandleConvolution` walks before the flop is cached |
| [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) | The `GetReduceWindowType` taxonomy behind the max-pool `FLT_MAX` sentinel |
| [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) | The per-gen cycles deposited into the merged `ResourceVector` |

## Cross-References

- [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) — the flop/byte model that supplies `HandleConvolution` / `flop_count` and the bundle-occupancy `RecordHloCycles` peer surface
- [Cost Model Overview](overview.md) — the three per-gen class families and the `Target` clock wiring the conv formula reads
- [Resource Enum (23-slot)](resource-enum.md) — the slot names, `Acc`, and the `MaxResourceCycles` overlap model the fused merge reduces
- [ConvolutionCostState](convolution-cost-state.md) — the per-conv state built before the flop is priced
- [Reduce-Window / Pooling Cost](reduce-window-pooling-cost.md) — the reduce-window type taxonomy and the max-pool path
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen `GetCyclesForThroughput` integers feeding the merged vector
- [CycleTable Family](cycletable-family.md) — `GetResource` (op→slot) and the `Instruction` bucket enum
- [MXU Latency Overview](mxu-latency-overview.md) — the per-gen `FlopsPerSecond` / VALU-slot integers parameterising the conv formula
- [MatmulMode and Modifiers](matmul-mode-modifiers.md) — the `MatmulDataFormat` selection that picks the conv peak rate
- [dot/conv MXU Lowering](../compiler/dot-conv-mxu-lowering.md) — why a raw `dot` reaching this function is a `CHECK`-fatal
- [Fusion Cost Model](../compiler/fusion-cost-model.md) — the priority formula that consumes these compute and bundle costs
