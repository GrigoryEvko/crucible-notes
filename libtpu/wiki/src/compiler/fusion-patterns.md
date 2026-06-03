# Fusion Patterns

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ. All findings derive from static analysis of the stripped ELF; the original source is closed.*

## Abstract

`xla::jellyfish::TpuInstructionFusion` is the main fusion pass of the TPU backend. It is an `xla::InstructionFusion` subclass (the same base class XLA's CPU/GPU backends use), but it runs **late** — after `LayoutAssignment` and after `DotCanonicalizer`/`ConvolutionFolding` — so by the time it sees a module every `kDot` has already become a `kConvolution`, every convolution has a settled layout, and elementwise chains are shape-canonical. The pass therefore matches a far narrower pattern space than upstream `xla::InstructionFusion` does, and it organizes that space around one axis the GPU backend never has: **which TPU engine the fused op lands on** — PE (the MXU/systolic array), ACT (the post-MXU vector ALU), or both packed into one VLIW bundle.

This page owns the **decision logic and the recognized shapes**: the `ShouldFuseImpl` predicate cascade (the lambda set `$_0`–`$_30` that votes a producer/consumer pair fusable or not), the catalog of HLO shapes the pass rewrites into a `kFusion` (conv+bias+activation, attention/softmax, layer-norm/RMSNorm tails, copy/packing fusions, collective fusions), and the legality gates that veto a candidate regardless of how profitable it looks. It does **not** own the numeric profitability scoring — the floating-point priority formula inside `TpuPriorityFusionQueue::CalculateProducerPriority*` and the per-generation MXU latency tables live on [fusion-cost-model.md](fusion-cost-model.md). The two pages share one fact: `TpuInstructionFusion::GetFusionQueue` returns a `TpuPriorityFusionQueue`, so candidates are *both* predicate-filtered (this page) and priority-ranked (cost-model page) before any rewrite happens.

The structure below: the predicate model first (how `ShouldFuse` → `ShouldFuseImpl` reaches a `FusionDecision`), then the hard legality gates (the no-fuse-no-matter-what checks), then the recognized-shape catalog grouped by target engine, then the input/output op-fuser dispatch that translates a recognized HLO subgraph into the `InputFusionOp`/`OutputFusionOp` MLIR carrier, and finally the multi-output and collective pattern families.

For reimplementation, the contract is:

- **The decision pipeline:** `ShouldFuse(consumer, operand_index)` → `ShouldFuseImpl` → a `FusionDecision` (fuse / reason-string-rejected), gated first by hard legality predicates, then by the priority queue's cost rank.
- **The predicate set:** the ~31 `$_*` lambda predicates inside `ShouldFuseImpl` and the named legality methods (`FusionFitsInVmem`, `ProducerCanBeLoopFused`, `CheckReduceBroadcastIntoReduceWindowFusionRequirements`, the MOF `TooMany*`/`TooMuch*` family).
- **The recognized shapes:** the HLO match expressions the pass rewrites, keyed by recognizing method and target lowering, and which generation each shape is available on.
- **The op-fuser dispatch:** how a recognized subgraph is carried as an `OutputFusionOp`/`InputFusionOp` via the anonymous-namespace `OutputFusionOp::Create<MlirOp>` template, and why transcendental activations bypass that path.

| | |
|---|---|
| **Pass class** | `xla::jellyfish::TpuInstructionFusion : public xla::InstructionFusion` |
| **`RunImpl`** | `0x13080dc0` |
| **Decision entry** | `ShouldFuse` `0x13089b20` → `ShouldFuseImpl` `0x13086660` (~1779 decompiled lines) |
| **Predicate lambdas** | `$_0`–`$_30` referenced inside `ShouldFuseImpl` (raw count ~29; decompile shows refs up to `$_30`) |
| **Fusion queue** | `GetFusionQueue` `0x13083c40` → `TpuPriorityFusionQueue` (anon namespace) |
| **VMEM gate** | `FusionFitsInVmem` `0x13084b40`; budget `xla_jf_fusion_max_vmem_mib` (default 8 MiB) |
| **Pipeline phase** | "Main fusion" (Phase 5), see [compile-phases.md](compile-phases.md) |
| **Cost scoring (linked)** | `CalculateProducerPriorityWith{Current,BundleAware}CostModel` — [fusion-cost-model.md](fusion-cost-model.md) |
| **Source file** | `platforms/xla/service/jellyfish/tpu_instruction_fusion.cc` (string in `.rodata`) |

---

## The Decision Pipeline

### Purpose

`InstructionFusion` (the base class) walks each computation, and for every (consumer, operand) edge asks the subclass: *should this operand be fused into this consumer?* The answer is a `FusionDecision` — either "yes" or a human-readable reason string. `TpuInstructionFusion` overrides three hooks; the substance is in one private method.

### Entry Point

```text
TpuInstructionFusion::RunImpl  (0x13080dc0)
  ├─ pre-passes (rewrite the graph before priority fusion):
  │    CreateFusionsAroundConvolutions   (0x1307c2c0)  ── wrap each bare Conv in a kCustom fusion
  │    BitcastConvOperands               (0x1307c960)  ── fold Bitcast into Conv window-config
  │    PrefuseReduceBroadcastReuse        (0x1307d9a0)  ── keep Reduce result in MXU latch
  │    MoveReduceBroadcastTogether        (0x1307f9c0)  ── reorder so Broadcast can fuse Reduce
  │    CreateLoopFusionAroundFusionLowerableHlo (0x13080780)
  │    DoExtendedAnalysisForCustomCallConsumerFusion (0x13082680)
  └─ base InstructionFusion::Run drives the queue:
       GetFusionQueue (0x13083c40) ── returns TpuPriorityFusionQueue
         └─ for each dequeued (producer, consumer):
              ShouldFuse              (0x13089b20)   ── public hook
                └─ ShouldFuseImpl     (0x13086660)   ── the predicate cascade
              ShouldFuseIntoMultiOutput (0x13089ce0) ── MOF variant
              ChooseKind              (0x13084a60)   ── kInput / kOutput / kLoop / kCustom
              Fuse                    (0x1308d820)   ── perform the rewrite
```

### Algorithm

`ShouldFuseImpl` is the heart of the page. Its body (decompiled, ~1779 lines) is a long cascade of guard predicates; the first one that votes "no" returns a `FusionDecision` carrying a reason string. Predicates are implemented as `$_*` lambdas (`$_0`–`$_30` referenced) plus calls to named legality methods. The structure below reconstructs the cascade from the decision strings and the call sites recovered in the decompile.

```c
function ShouldFuseImpl(consumer, operand_index):          // 0x13086660
    producer = consumer->operand(operand_index)
    // 0. base-class structural gate, run as a lambda predicate $_0
    //    (FusionDecision(producer, consumer, AliasInfo, InPlaceFusionOptions))
    d = run_lambda_$_0(producer, consumer)                  // line ~650: __policy_func<FusionDecision(...)>
    if d.is_no(): return d                                  // "Not fusing"

    // 1. scalar-constant fast paths — always fuse (graph cosmetics / index folding)
    if producer is scalar constant feeding consumer:
        return Fuse("Did fuse: fusing scalar constant to make graph look nicer.")
    if producer is scalar constant used as dynamic-slice index:
        return Fuse("Did fuse: fusion scalar constant to dynamic slice index.")
    if producer is scalar constant used as DUS index:
        return Fuse("Did fuse: fusion scalar constant to fusible dynamic update slice index.")

    // 2. output-fusion enable gate
    if consumer is output-fusion candidate and !output_fusion_enabled:
        return NoFuse("No fusing; output fusion is disabled.")

    // 3. VMEM capacity — the dominant hard gate
    if !FusionFitsInVmem(producer, consumer):               // 0x13084b40, line ~679
        return NoFuse("No fusing: result is a fusion which will use too "
                      "much VMEM for its operands.")        // line ~688

    // 4. duplication cost — refuse to clone an expensive producer
    if NumProducerDuplicationsIfFused(producer, consumer) > 0    // 0x130896a0
       and producer is expensive:
        return NoFuse("No fusing: producer is duplicated and expensive.")

    // 5. RNG single-use rule
    if producer->opcode == kRng and producer has multiple users:
        return NoFuse("no fusing: rng is used by multiple users")

    // 6. elementwise guard (some elementwise producers are not output-fusable)
    if Should-not-fuse-elementwise(producer, consumer):
        return NoFuse("No fusing: Should not fuse with elementwise")

    // 7. slice-like keep-unfused flag
    if producer is slice-like and FLAGS_xla_tpu_keep_slice_like_instructions_unfused:
        return NoFuse("No fusing: slice-like instruction kept unfused due to "
                      "flag xla_tpu_keep_slice_like_instructions_unfused")

    // 8. convolution-input legality: only "trivial" inputs may fuse into a conv
    if consumer is convolution-like and producer is non-trivial:
        return NoFuse("Refusing to fuse a non-trivial inputs into a "
                      "convolution-like. Producer: ...")

    // 9. effective-scalar reachability checks (DUS / fusible-user routing)
    if producer produces effective scalar without fusible DUS user reachable:
        return NoFuse("... produces an effective scalar and does not have a "
                      "fusible DUS user reachable through consumer: ")

    // 10. bitcast / reduce-window window legality
    if !CheckReduceBroadcastIntoReduceWindowFusionRequirements(...):  // 0x1307ee60, line ~975
        return NoFuse("Cannot find window to lower for bitcast reduce fusion: ...")
    if dim-collapsing bitcast and not must_fuse mode:
        return NoFuse("Dim collapsing bitcast is fused only in must_fuse mode.")

    // 11. loop-fusion / gather-fusion routing (logged, not necessarily reject)
    can_loop  = ProducerCanBeLoopFused(producer, consumer, target_)  // 0x1307f800, line ~1202
    log("producer_can_be_loop_fused: ", can_loop)
    log("producer_can_be_gather_fused: ", ...)

    // 12. boundary predicate $_29 (structural, no log of its own):
    //     a producer that already forms the outer fusion boundary
    //     cannot be fused any further (string emitted by caller ShouldFuse)
    if run_lambda_$_29(producer, consumer, target_, reachability_map):  // 0x130899c0
        return NoFuse(/* "Producer forms the fusion boundary. ..." */)

    return Fuse("Fusing producer: ... into consumer: ...")
```

> **NOTE — the lambdas, not the named methods, carry most of the logic.** `ShouldFuseImpl` references `$_0`–`$_30` (28 distinct refs survive in the decompile; the raw count is "~29"). Several are tiny structural predicates with no log string of their own (e.g. `$_29` at `0x130899c0`, which takes `(producer, consumer, consumer, Target&, HloReachabilityMap*)` and is the fusion-boundary test). The named methods (`FusionFitsInVmem`, `ProducerCanBeLoopFused`, `CheckReduceBroadcastIntoReduceWindowFusionRequirements`) are the heavyweight gates; the lambdas are the cheap structural filters that run first. [Confidence: HIGH — call sites and strings are in the decompile; the exact per-lambda body of each `$_*` was not individually unwound.]

> **CORRECTION (FUSE-01) — the HardSwish anti-fuse string is NOT in the TPU fusion pass.** The string `" HardSwish pattern was found, so fusion failed."` resolves to `tensorflow::grappler::Remapper::Optimize` (`0x105aa960`), a CPU TensorFlow graph rewriter compiled into the same `.so`, not to `TpuInstructionFusion`. The functional claim still holds on the TPU side — HardSwish has no direct ACT ALU opcode and so is not output-fusable (see [the activation discussion below](#why-some-activations-never-fuse)) — but the *string* is mis-attributed in the raw notes. Do not key a TPU reimplementation off that log line. [Confidence: CONFIRMED by symbol resolution.]

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `RunImpl` | `0x13080dc0` | Pass entry; runs pre-passes then base `Run` | CONFIRMED |
| `ShouldFuse` | `0x13089b20` | Public hook; emits `"Not fusing MOF"` / boundary strings | CONFIRMED |
| `ShouldFuseImpl` | `0x13086660` | The predicate cascade (~1779 lines) | CONFIRMED |
| `ShouldFuseIntoMultiOutput` | `0x13089ce0` | MOF variant of the decision | CONFIRMED |
| `FusionFitsInVmem` | `0x13084b40` | VMEM capacity gate | CONFIRMED |
| `NonBroadcastOperandsSize` | `0x13084d80` | Aux measure for the VMEM gate | HIGH |
| `ProducerCanBeLoopFused` | `0x1307f800` | kLoop-fusion legality | HIGH |
| `CheckReduceBroadcastIntoReduceWindowFusionRequirements` | `0x1307ee60` | RWB window legality | HIGH |
| `RwbPreliminaryCandidateCheck` | `0x13085700` | RWB pre-filter | HIGH |
| `NumProducerDuplicationsIfFused` | `0x130896a0` | Duplication-cost count | HIGH |
| `$_29` lambda | `0x130899c0` | Fusion-boundary structural predicate | HIGH |
| `ChooseKind` | `0x13084a60` | Picks `kInput`/`kOutput`/`kLoop`/`kCustom` | CONFIRMED |
| `GetFusionQueue` | `0x13083c40` | Returns `TpuPriorityFusionQueue` | CONFIRMED |

### Related Knobs

| Knob | Type | Default | What it gates |
|---|---|---|---|
| `xla_jf_conv_input_fusion` | BOOL | **true** | Enables the input-side (above-conv) elementwise chain fusion |
| `xla_jf_conv_output_fusion` | BOOL | **true** | Enables the output-side (below-conv) chain; trips the "output fusion is disabled" reject when off |
| `xla_jf_conv_reshape_fusion` | BOOL | **true** | Allows `ReshapeFuser` to absorb reshapes into a conv fusion |
| `xla_tpu_keep_slice_like_instructions_unfused` | BOOL | **false** | When true, slice-like producers are vetoed (predicate 7) |
| `xla_jf_fusion_max_vmem_mib` | INT | **8** | VMEM budget the `FusionFitsInVmem` gate enforces (MiB) |
| `xla_jf_enable_final_priority_fusion` | BOOL | **true** | Drives the `TpuPriorityFusionQueue` priority walk |

---

## Hard Legality Gates

### Purpose

Some checks are unconditional: even a candidate the priority queue ranks first is dropped if it fails one. They split into (a) structural/legality vetoes inside `ShouldFuseImpl` and (b) the multi-output-fusion (MOF) cost predicates inside `TpuMultiOutputFusion`. Each veto emits a distinct `.rodata` string, which is the reimplementer's ground truth for *why* a fusion did not form.

### Reject Predicates

| Veto | Source method | Decision string (`.rodata`) | Confidence |
|---|---|---|---|
| VMEM capacity (whole fusion) | `FusionFitsInVmem` `0x13084b40` | `"No fusing: result is a fusion which will use too much VMEM for its operands."` | CONFIRMED |
| Nested-dot VMEM | `ShouldFuseImpl` + `xla_tpu_nested_dot_fusion_vmem_fraction` | `"Nested dot fusion would exceed vmem capacity"` | HIGH |
| Custom-call VMEM | `DoExtendedAnalysisForCustomCallConsumerFusion` `0x13082680` | `"Custom Fusion would exceed vmem capacity"` | HIGH |
| Producer duplicated + expensive | `ShouldFuseImpl` + `NumProducerDuplicationsIfFused` | `"No fusing: producer is duplicated and expensive."` | CONFIRMED |
| RNG with multiple users | `ShouldFuseImpl` | `"no fusing: rng is used by multiple users"` | CONFIRMED |
| Elementwise not output-fusable | `ShouldFuseImpl` | `"No fusing: Should not fuse with elementwise"` | CONFIRMED |
| Slice-like kept unfused (flag) | `ShouldFuseImpl` | `"No fusing: slice-like instruction kept unfused due to flag ..."` | CONFIRMED |
| Non-trivial input into conv | `ShouldFuseImpl` | `"Refusing to fuse a non-trivial inputs into a convolution-like. ..."` | CONFIRMED |
| Bitcast-reduce window not found | `CheckReduceBroadcastIntoReduceWindowFusionRequirements` | `"Cannot find window to lower for bitcast reduce fusion: ..."` | CONFIRMED |
| Dim-collapsing bitcast outside must_fuse | `ShouldFuseImpl` | `"Dim collapsing bitcast is fused only in must_fuse mode."` | CONFIRMED |
| Effective scalar w/o fusible DUS user | `ShouldFuseImpl` | `"... produces an effective scalar and does not have a fusible DUS user ..."` | CONFIRMED |
| Output fusion globally disabled | `ShouldFuseImpl` | `"No fusing; output fusion is disabled."` | CONFIRMED |
| MOF creates a cycle | `xla::MultiOutputFusion::Perform` `0x14bdb5a0` | `"multi-output fusion creates a cycle"` | CONFIRMED |
| Too many result operands | `TpuMultiOutputFusion::TooManyResultOperands` `0x110ddec0` | (no string — silent cost pre-check) | CONFIRMED |
| Too much reduce-output MOF | `TpuMultiOutputFusion::TooMuchReduceOutputMultiOutput` `0x110e1060` | `"TooMuchReduceOutputMultiOutput: "` | CONFIRMED |
| HBM pressure high if fused | `TpuMultiOutputFusion::IsHBMPressureHighIfFused` `0x110de640` | (no string) | CONFIRMED |
| Already has `must_fuse` (CCF) | `TpuUserGuidedFusionVerifier::VerifyMustFuseCalls` | `" already has must-fuse attribute, skipping ..."` | HIGH |

> **GOTCHA — VMEM is the gate that surprises a port.** The TPU has no general-purpose register file the way a GPU SM does; fusion materializes the whole fused region into VMEM scratch, and the union of *operand windows* (not just the output) must fit. `FusionFitsInVmem` calls `EstimateFusionCost` on that union and rejects if it exceeds `xla_jf_fusion_max_vmem_mib` minus already-used scratch. A naive port that only checks output size will accept fusions that overflow VMEM and miscompile. The scavenging flags (`xla_tpu_scavenge_vmem_for_fusions`) let the queue *retry* a rejected fusion after other fusions free VMEM — so VMEM rejection is not necessarily final within one pass. The cycle-budget mechanics of that retry are owned by [fusion-cost-model.md](fusion-cost-model.md).

### Considerations

The MOF predicates (`TooManyResultOperands`, `TooMuchReduceOutput`, `IsHBMPressureHighIfFused`) gate the *fan-out* direction (one producer → many consumers, or many producers → one tuple root) and are checked by `TpuMultiOutputFusion::LegalToFuse` (`0x110ddc20`) before `GetProfit` (`0x110dd0a0`) is consulted. The fan-out limit and operand-count caps are set by `xla_tpu_multi_output_fusion_limit` and `xla_tpu_multioutput_fusion_max_operands`.

---

## Recognized Shapes — PE-Anchored (conv / matmul + epilogue)

### Purpose

The dominant TPU fusion shape is a convolution (or a matmul that `DotCanonicalizer` already rewrote to a convolution) with an elementwise epilogue fused below it and/or an elementwise chain fused above it. Every shape in this section lowers through the conv emitter (`FusedSpatialMajorConvolution::EmitOneChunk`) with the epilogue emitted on the ACT engine in the same per-chunk loop.

### Shape Catalog

| Pattern | HLO match expression | Recognizing method | Target lowering | Confidence |
|---|---|---|---|---|
| **Conv+Bias** | `Add(Conv(act,kernel), Broadcast(bias))` | `TpuInstructionFusion` output-fusion | MXU result + `vadd.f32`/`vadd.bf16` epilogue, same chunk | HIGH |
| **Conv+Bias+ReLU** | `Maximum(Add(Conv(a,w),Broadcast(b)),Broadcast(0))` | output-fusion → `TpuLoopFusionEnhancer` | as above + `vmax.f32(_,0)` / per-gen `Relux` | HIGH |
| **Conv+Bias+Sigmoid** | `Logistic(Add(Conv(a,w),Broadcast(b)))` | output-fusion (elementwise tail) | as above + `ShiftedSigmoid` ACT op (per-gen) | HIGH |
| **Conv+Bias+Tanh** | `Tanh(Add(Conv(a,w),Broadcast(b)))` | output-fusion (elementwise tail) | as above + `vtanh` ACT op | HIGH |
| **Conv+Bias+GELU** | `Mul(Add(Conv,b), Mul(0.5, Add(1, Erf(...))))` | output-fusion + `ElementwiseOutputFuser` | as above + `verf` + add/mul chain (ACT) | MEDIUM |
| **Conv+Activation (no bias)** | `<act>(Conv(a,w))`, `<act>∈{Relu,Tanh,Sigmoid,Exp,Erf}` | output-fusion (no-bias variant) | as Conv+Bias+act, minus the bias add | HIGH |
| **MatMul+Bias+ReLU (dense)** | post-`DotCanonicalizer`: identical to Conv+Bias+ReLU | `TpuInstructionFusion` | identical conv lowering | HIGH |
| **MatMul→LayerNorm tail** | `Subtract(x,ReduceMean(x)); Mul(...); Rsqrt(ReduceMean(Square(...)))` | `TpuInstructionFusion` + `TpuLoopFusionEnhancer` | PE matmul + ACT (`vrsqrt`,`vmul`,`vadd`) bundle | MEDIUM |
| **MatMul→RMSNorm tail** | `Mul(x, Rsqrt(ReduceMean(Square(x)) + eps))` | `TpuInstructionFusion` | ACT: `vrsqrt.f32` + `vmul.f32` | MEDIUM |
| **AttentionMatMul (Q·Kᵀ)** | `Dot(Q,K)` (softmax in a separate fusion) | `TpuInstructionFusion` (dot-as-conv) | standard MXU lowering | HIGH |
| **Attention + Softmax** | `Dot` then `Exp/Sum/Div` chain | gated by `xla_tpu_enable_multi_level_nested_dot_fusion` | one nested `kFusion` if enabled, else two | MEDIUM |
| **DotDot (A·B·C)** | `Dot(Dot(A,B),C)` → two Convs | gated by `xla_tpu_dot_dot_fusion` | one nested super-fusion; needs VMEM for B+C | MEDIUM |
| **RWB (Reduce-Window-Broadcast)** | `Broadcast(ReduceWindow(input,window))` matched as conv | `RwbPreliminaryCandidateCheck` + `CheckReduceBroadcastIntoReduceWindowFusionRequirements`, gated by `xla_tpu_rwb_fusion` | lowered as Conv with broadcast output-fusion | HIGH |

> **QUIRK — every matmul fusion is a convolution fusion.** Because `DotCanonicalizer` runs *before* the main fusion pass, `TpuInstructionFusion` never sees a `kDot`. A dense layer (`MatMul+Bias+ReLU`) and a conv layer (`Conv+Bias+ReLU`) are the *same* fusion shape by the time this pass runs, and they share one lowering path. A reimplementation that branches on `kDot` vs `kConvolution` inside the fuser will find the `kDot` branch is dead — the canonicalizer already collapsed it. Match on `kConvolution` only.

### The shape-restructuring pre-passes

Four `RunImpl` pre-passes reshape the graph so more candidates become matchable, before any priority fusion runs:

| Pre-pass | Addr | What it rewrites | Confidence |
|---|---|---|---|
| `CreateFusionsAroundConvolutions` | `0x1307c2c0` | Wraps each bare `Conv` in a `kCustom` fusion so its operand windows are explicit | HIGH |
| `BitcastConvOperands` | `0x1307c960` | Folds `Bitcast(a)`/`Bitcast(w)` into the conv's window-config (avoids materializing the reshape) | HIGH |
| `PrefuseReduceBroadcastReuse` | `0x1307d9a0` | Keeps a `Reduce` result in the MXU latch so a downstream `Broadcast` amortizes it | HIGH |
| `MoveReduceBroadcastTogether` | `0x1307f9c0` | Reorders so a `Reduce` and its distant `Broadcast` become adjacent and fusable | HIGH |

---

## Recognized Shapes — Multi-Output and Collective

### Multi-output (TpuMultiOutputFusion)

`TpuMultiOutputFusion : public xla::MultiOutputFusion` (`RunImpl` inherited at `0x14bdaa80`) fuses the fan-out direction: a producer feeding several consumers, or several producers tied into one tuple root. It exposes two drivers — `DoProducerConsumerMultiOutputFusion` (`0x110e43e0`) and `DoAdvancedMultiOutputFusion` (`0x110e3300`).

| Pattern | HLO shape | Driver | Lowering | Confidence |
|---|---|---|---|---|
| **MultiOutput conv** | one `Conv` → {ReLU, ReLU_grad} | `DoProducerConsumerMultiOutputFusion` | one `kFusion` with tuple root; PE+ACT bundle reused | HIGH |
| **MultiOutput reduce** | two `Reduce` ops sharing an operand | `DoAdvancedMultiOutputFusion` | one `kFusion`, PE-side + ACT-side reduce in one iteration | HIGH |

The MOF legality chain is `ShapesCompatibleForFusion` (`0x110dcca0`) → `IsFusible` (`0x110dce20`) → `LegalToFuse` (`0x110ddc20`, includes the cycle check) → `GetProfit` (`0x110dd0a0`). The `TooMany*`/`TooMuch*`/`IsHBMPressureHighIfFused` predicates (see [hard gates](#reject-predicates)) run inside this chain.

### Collective and copy/packing shapes

These are recognized by dedicated passes that run in the "Pre main fusion" phase (B2) *before* `TpuInstructionFusion`, or are gated sub-modes of it.

| Pattern | HLO shape | Recognizing pass / gate | Confidence |
|---|---|---|---|
| **Async collective** | `AllGatherStart → … → AllGatherDone` | `AsyncCollectiveFusion::RunImpl` `0x109b4ec0` | HIGH |
| **AllReduce+Scatter** | `AllReduce(x) → Slice(x, my_shard)` | `TpuAllReduceScatterFusion::RunImpl` `0x127acd40` → `__tpu$AllReduceScatter` | HIGH |
| **Mosaic kernel** | `CustomCall(target="XlaMosaic")` + neighbours | `MosaicFusion` (in `HloPassFix`) `0x10f12500` → `tpu_custom_call` | HIGH |
| **Megacore conv+AR** | `Conv` paired with `AllReduce` across cores | `MegacoreFusion::RunImpl` `0x110d8f00` | HIGH |
| **Copy (data-format)** | layout-only `Copy` ≥ `xla_tpu_copy_fusion_threshold` bytes | `TpuInstructionFusion` + `xla_tpu_enable_copy_fusion` | HIGH |
| **Copy-permute-minor** | `Copy` swapping the 2nd-minor dim | + `xla_tpu_enable_copy_permute_minor_fusion` | HIGH |
| **Pad/Unpad copy** | `Copy(Pad(x))` with ratio < `xla_tpu_copy_fusion_pad_unpad_ratio` | `TpuInstructionFusion` | MEDIUM |
| **Bf16-packed matmul** | two bf16 ops sharing an operand | `FusedSpatialMajorConvolution::EmitPackedBf` | MEDIUM |
| **Int8 (x8)-packed matmul** | two int8 matmuls sharing an operand | `ConvolutionLoweringStrategy.x8_packed` | MEDIUM |
| **DS_CC_DUS** | `DynamicSlice → CustomCall → DynamicUpdateSlice` | `TpuInstructionFusion-AdvancedDS_CC_DUS` (log string) | MEDIUM |

> **NOTE — collective and Mosaic shapes are recognized before the main pass.** `AsyncCollectiveFusion`, `TpuAllReduceScatterFusion`, and `MosaicFusion` run in the "Pre main fusion" pipeline (Phase B2), so by the time `TpuInstructionFusion` runs in "Main fusion" (B3) these are already `kFusion`/`tpu_custom_call` nodes it treats as opaque. Per-pass placement is owned by [compile-phases.md](compile-phases.md); this page owns only the match shapes.

---

## Op-Fuser Dispatch — Carrying a Shape into MLIR

### Purpose

Once `TpuInstructionFusion` has decided a subgraph is one `kFusion`, the MLIR lowering must rebuild that subgraph op-by-op inside an `InputFusionOp` (elementwise chain *above* the conv) or `OutputFusionOp` (chain *below* it) region. A small set of `FusionOpFuser` subclasses do the per-op translation. This is where a recognized shape stops being an HLO pattern and becomes a concrete MLIR carrier.

### The recognition gate is `OutputFusionOp::Create`, not a switch

The single most important reimplementation fact: `ElementwiseOutputFuser::CanFuse` (`0x10f36aa0`) does **not** contain a hardcoded opcode switch. It calls the anonymous-namespace `OutputFusionOp::Create(op)` and returns whether the create succeeded.

```c
function ElementwiseOutputFuser::CanFuse(op):              // 0x10f36aa0
    OutputFusionOp::Create(&tmp, op)   // anon-namespace factory; sets tmp.ok flag
    if tmp.ok == 1:
        if tmp.has_cleanup_fn: run_cleanup(); return 1     // op IS recognized
    return 0                                               // op not in the carrier set
```

`OutputFusionOp::Create<MlirOp>` is template-instantiated for exactly the 16 MLIR arith/math ops below (RTTI confirms the same set on the `InputFusionOp` side — the carrier is symmetric):

```text
arith::AddFOp  arith::AddIOp  arith::SubFOp  arith::SubIOp
arith::MulFOp  arith::MulIOp  arith::DivFOp  arith::DivSIOp
arith::MaximumFOp  arith::MaxSIOp  arith::MinimumFOp  arith::MinSIOp
arith::NegFOp  math::AbsFOp  math::AbsIOp  math::ExpOp
```

> **GOTCHA — transcendental activations bypass the elementwise carrier.** `Tanh`, `Sqrt`, `Rsqrt`, `Erf`, `Logistic`, `Sin`, `Cos`, `Atan2` are **not** in the 16-op `OutputFusionOp` set above, even though Conv+Tanh and Conv+Sigmoid are listed as recognized shapes. They are lowered to **dedicated LLO ops** (`llo.vtanh`, `llo.vrsqrt`, `llo.verf`, …) directly, not via the elementwise `OutputFusionOp` path. A reimplementation that drives the epilogue only off the 16-op carrier will silently drop every transcendental activation. The split is: the carrier holds the *algebraic* ops (add/mul/min/max/abs/exp/neg), and the LLO dialect holds the *transcendental* ops as first-class instructions.

### Fuser Map

| Fuser | Addr (`GetFusionOp` / `CanFuse,Fuse`) | HLO ops handled | Confidence |
|---|---|---|---|
| `BinaryOpFuser` | `0x10f1be60` | Add/Sub/Mul/Div/Max/Min (F and I variants) | HIGH |
| `UnaryOpFuser` | `0x10f2bf00` | `math::ExpOp`, `AbsFOp`, `AbsIOp`, `arith::NegFOp` | HIGH |
| `TernaryOpFuser` | `0x10f2b7a0` | Select / Clamp (FMA-style ternary) | MEDIUM |
| `CompareFuser` | `0x10f1e760` | `arith::CmpFOp`, `arith::CmpIOp` (all predicates) | HIGH |
| `ConvertFuser` | `0x10f1f100` | ExtF/TruncF/SIToFP/UIToFP/FPToSI/FPToUI (~13 cases) | HIGH |
| `BroadcastFuser` | `0x10f1c5a0` | `kBroadcast` → `vector::BroadcastOp` / `llo.vbcast_sublane_chunk` | HIGH |
| `ReduceFuser` | `0x10f20120` | `kReduce` → `vector::MultiDimReductionOp` → `llo.vmax.{x,s}lane.*` | HIGH |
| `ReshapeFuser` | `0x10f22680` | `kReshape` → `vector::ShapeCastOp` / `tensor::CollapseShapeOp` | HIGH |
| `ConvertOutputFuser` | `0x10f2c480` / `0x10f2c4a0` | output-side `kConvert` (f32 → bf16 / f8 downcast) | CONFIRMED |
| `ElementwiseOutputFuser` | `0x10f36aa0` / `0x10f36e20` | the 16-op carrier set above (via `OutputFusionOp::Create`) | CONFIRMED |
| `ReduceOutputFuser` | `0x10f37920` / `0x10f37940` | `kReduce` consumed by the fusion root | CONFIRMED |
| `CustomCallOutputFuser` | `0x10f2dac0` / `0x10f2dae0` | `kCustomCall` with target in `xla_tpu_nested_dot_fusion_supported_custom_ops` | HIGH |

### Why some activations never fuse

Independent of the carrier split, three activation families are never output-fusable on any generation and must lower as standalone HLO computations reading the matmul output through VMEM:

- **HardSwish** — no direct ACT ALU opcode (see [CORRECTION FUSE-01](#algorithm) on the mis-attributed string).
- **LeakyReLU / ELU / SELU / Mish / Swish** — synthesized via `select` or multi-op polynomial; the cost model rejects the polynomial expansion.
- **Softmax** when `xla_tpu_enable_multi_level_nested_dot_fusion=false` — kept as a separate fusion.

Which *transcendental* opcodes a given generation has (and thus which activations fuse directly vs. fall back to a polynomial) is owned by the activation-inventory analysis; the relevant fact for *this* page is that the recognized shape (e.g. Conv+Bias+GELU) is only matchable when the target gen has the `Erf` ACT opcode.

---

## Related Components

| Component | Relationship |
|---|---|
| `TpuPriorityFusionQueue` | Ranks the candidates this page's predicates admit; numeric formula on [fusion-cost-model.md](fusion-cost-model.md) |
| `DotCanonicalizer` / `ConvolutionFolding` | Run before fusion; turn every `kDot` into the `kConvolution` this pass matches — see [dot-conv-mxu-lowering.md](dot-conv-mxu-lowering.md) |
| `LayoutAssignment` | Runs before fusion; fusion legality depends on settled tile layouts — see [layout-assignment.md](layout-assignment.md) |
| `MxuLmrTransform` | Post-lowering; collapses (matprep+matmul+matres)→LMR, freeing the ACT VLIW slots that make PE+ACT bundle-packing physically possible |
| `TpuLoopFusionEnhancer` | Runs after the main pass; extends existing `kLoop` fusion boundaries to absorb more elementwise leaves |

## Cross-References

- [compile-phases.md](compile-phases.md) — the top-level phase ordering; "Main fusion" is Phase 5, after layout assignment
- [fusion-cost-model.md](fusion-cost-model.md) — the numeric profitability half: `CalculateProducerPriority*` coefficients, MXU latency tables, VMEM scavenging cycle budget
- [dot-conv-mxu-lowering.md](dot-conv-mxu-lowering.md) — `DotCanonicalizer`/`ConvolutionFolding`/`SpatialMajorConvolution`; why this pass only ever sees convolutions
- [layout-assignment.md](layout-assignment.md) — the layout fixing that precedes and constrains fusion legality
- [overview.md](overview.md) — the compiler pipeline overview and where the fusion pass family sits
- [../cost/overview.md](../cost/overview.md) — the cost-analysis subsystem `EstimateFusionCost`/`GetHloCycles` consult for the VMEM and cycle estimates
