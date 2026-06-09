# LoopVectorize and VPlan (GPU-Adapted)

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/Vectorize/LoopVectorize.cpp`, `llvm/lib/Transforms/Vectorize/VPlan*.cpp` (LLVM 20.0.0). VPlan infrastructure lives in `llvm/lib/Transforms/Vectorize/VPlan.cpp`, `VPlanRecipes.cpp`, `VPlanTransforms.cpp`, and related files.
>
> **LLVM version note:** CICC v13.0 is based on LLVM 20.0.0 trunk. Evidence includes histogram-pattern support (merged in LLVM 19), early-exit vectorization (LLVM 20 experimental feature, gated by `byte_500CDA8`), and the VPlan-native path. The VPlan object size (656 bytes) is consistent with LLVM 17/18+ layout. Scalable vectors are always disabled for NVPTX.

NVIDIA's cicc ships a heavily modified copy of LLVM's `LoopVectorizePass`, the single largest pass in the vectorization pipeline at 16.4 KB binary text (2,612 decomp lines in `sub_2AF1970`). The modifications do not change the pass's fundamental architecture — it still builds VPlans, selects a vectorization factor (VF) through cost modeling, and transforms IR through VPlan execution — but the cost model, VF selection heuristics, interleave count logic, and legality checker are all tuned for a target where "vectorization" means something fundamentally different than on a CPU. On a CPU, loop vectorization fills SIMD lanes: a VF of 4 on SSE processes four `float` elements per vector instruction. On an NVIDIA GPU, there are no SIMD lanes in the CPU sense — each thread already executes scalar code, and the warp executes 32 threads in lockstep. The reasons to vectorize on GPU are: (1) **memory coalescing** — adjacent threads issuing adjacent loads produce 128-byte cache line transactions, and vectorizing a per-thread loop body with VF=2 or VF=4 produces `ld.v2`/`ld.v4` wide loads that maximize bytes-per-transaction; (2) **reducing instruction count** — a single `ld.global.v4.f32` replaces four `ld.global.f32` instructions, saving fetch/decode/issue bandwidth; (3) **register-to-memory width matching** — PTX supports 32-, 64-, and 128-bit load/store widths, and vectorization widens narrow scalar accesses to fill these naturally.

## Key Facts

| Property | Value |
|---|---|
| Registration | New PM #400, parameterized: `no-interleave-forced-only;...` |
| Runtime positions | Not in Tier 0/1/2/3 tables; invoked via LLVM standard sub-pipeline `sub_1A62BF0` when vectorization is enabled (see [Pipeline](pipeline.md)) |
| Main entry point | `sub_2AF1970` (0x2AF1970) — `LoopVectorizePass::processLoop()` |
| Binary size | 16.4 KB binary text, 2,612 decomp lines |
| VPlan builder | `sub_2AEE460` (0x2AEE460) — `tryToBuildVPlanWithVPRecipes()`, 11.3 KB binary |
| VPlan object size | 656 bytes (0x290), consistent with LLVM 17/18 layout |
| LLVM base | LLVM 20 trunk (evidence: histogram-pattern support, early-exit vectorization, VPlan-native path) |
| Scalable vectors | Always disabled — `sub_DFE610` returns `false` for NVPTX |
| Register bit width (TTI) | 32 bits fixed (`TypeSize::getFixed(32)` in upstream `NVPTXTTIImpl`) |
| Pass name string | `"vectorize-loops"` at 0x439F095 |
| Address cluster | 0x2AA0000–0x2C20000 (loop vectorizer + VPlan infrastructure) |

## Why Vectorize on GPU

GPU vectorization is not about filling SIMD lanes — the [SIMT model](../gpu-execution-model.md#simt-warp-execution) already replicates scalar code across 32 threads. Vectorization targets three orthogonal benefits related to [memory coalescing](../gpu-execution-model.md#memory-coalescing) and instruction throughput:

**Memory coalescing width.** The GPU memory subsystem services requests in [128-byte transactions](../gpu-execution-model.md#memory-coalescing). If a single thread's inner loop accesses 4 consecutive floats in sequence, those 4 accesses become 4 separate scalar loads issued over 4 iterations. Vectorizing with VF=4 converts them into one `ld.global.v4.f32`, which the memory subsystem can service in a single wider transaction per thread. Across the warp, this multiplies the effective memory bandwidth.

**Instruction count reduction.** PTX's `ld.v2` and `ld.v4` instructions load 2 or 4 elements with a single instruction. The instruction issue pipeline has finite throughput (typically 1-2 instructions per clock per scheduler), so halving instruction count directly improves throughput-bound kernels.

**Register-width matching.** PTX has [32-bit typed registers](../reference/register-classes.md). A 128-bit `ld.v4.f32` loads directly into four consecutive registers via a single instruction, which is strictly better than four separate 32-bit loads (each requiring its own address computation).

These benefits are bounded by **register pressure** — the primary constraint that does not exist on CPU. On a GPU, every additional register per thread can cross an [occupancy cliff](../gpu-execution-model.md#occupancy-cliffs), potentially losing an entire warp group. A VF=4 vectorization that quadruples the live register count may halve occupancy and lose net throughput.

## The 8-Phase Pipeline

The main function `sub_2AF1970` implements eight phases, closely following upstream LLVM's structure but with GPU-specific decision points at each stage.

### Phase 1: Legality Pre-Check

```c
sub_31A4FD0(legalityCtx, Loop, Function, ORE, SE)    // init legality scratch
TTI = *(**(Loop+32) + 72)                             // Loop->getHeader()->getParent()->getTTI()
if (!sub_31A91F0(legalityCtx, TTI, Loop, LoopInfo))   // canVectorize() quick check
    return false

sub_31AF060(costCtx, ForceVectorization)              // canVectorize() full check
// ForceVectorization = qword_500D340[17] ("vectorize-loops" knob)
```

The legality checker (`sub_31AF060`) performs standard LLVM legality analysis: loop simplify form, single exit, computable backedge-taken count, no irreducible control flow. The NVIDIA-specific addition is early-exit loop handling:

```c
if (hasUncountableEarlyExit && !byte_500CDA8)         // -enable-early-exit-vectorization
    emit "UncountableEarlyExitLoopsDisabled"
    return false
```

This knob (`byte_500CDA8`) gates an LLVM 20 feature that NVIDIA includes but disables by default. Early-exit vectorization requires predicated execution, which on GPU means divergent warps — typically unprofitable.

### Phase 2: Outer vs Inner Loop Dispatch

```c
if (Loop->getSubLoops().size() > 0)
    goto outerLoopPath                                // PATH A (rarely taken on GPU)
else
    goto innerLoopPath                                // PATH B (the main path)
```

Outer loop vectorization is controlled by `byte_500D208` (`-force-vector-width-outer`). When enabled and TTI-based VF selection returns VF <= 1, the pass forces VF=4 — a hardcoded NVIDIA override for kernel patterns where outer-loop vectorization benefits warp-level memory access patterns. In practice, inner loop vectorization (Path B) handles the vast majority of GPU kernels.

### Phase 3: Trip Count and Safety Checks

```c
tripCount = getSmallBestKnownTC(PSE, Loop)            // sub_2AA7EC0
if (tripCount < VectorizerMinTripCount                // dword_500EAE8
    && !isForceVectorize(legalCtx)
    && !(exactTC >= userVF))
    emit "LowTripCount"
    reduce hint to interleave-only

if (hasAttribute(TTI, NoImplicitFloat))               // attribute 30
    bail "NoImplicitFloat"

if (hasUnsafeFPOps && !canReorderFP(override))
    bail "UnsafeFP" / "CantReorderFPOps"
```

The FP reorder safety check has an override mechanism: `dword_500D508` selects whether the override is active, and `byte_500D588` provides the override value. This lets NVIDIA force-allow FP reordering for specific compilation modes (e.g., `-ffast-math` propagated from nvcc).

### Phase 4: VF Selection

This is where NVIDIA diverges most from upstream. The upstream algorithm queries `TTI::getRegisterBitWidth()` which returns the vector register width (256 for AVX2, 512 for AVX-512), then computes `VF = registerWidth / elementSize`. On NVPTX, `getRegisterBitWidth()` returns **32** — a single scalar register width. This means the upstream formula would always produce VF=1 for 32-bit types.

NVIDIA's VF selection (`sub_2AB8AC0` for outer loops, `sub_2AE08E0` for inner loops via VPlan cost) works differently:

```cpp
// sub_2AB8AC0 — outer loop VF selection (simplified)
elementBits = getWideningElementSize(CostModel)       // sub_2AB4370: top 32 bits
regWidth    = TTI.getRegisterBitWidth(Vector)          // sub_DFE640: returns 32
VF          = regWidth / (elementBits / 8)

if (!isScalable && VF <= 1 && forceOuterMode)          // byte_500D208
    VF = 4                                             // NVIDIA hardcoded override
```

For inner loops (the common path), VF selection goes through the full VPlan cost model:

```cpp
// sub_2AE08E0 — selectBestVF() from VPlan candidates
bestCost = INT64_MAX
for each VPlan in candidatePlans:
    for each VF in VPlan.VFRange:
        cost = computeCostForVF(VPlan, VF)             // sub_2AE0750
        if isBetterThan(cost, bestCost):               // sub_2AB3FE0
            bestVF = VF
            bestCost = cost
return {bestVF, isScalable, bestIC}
```

The cost accumulation uses **saturating arithmetic** — `__OFADD__` overflow detection clamping to `INT64_MAX`/`INT64_MIN` — preventing wrap-around in cost comparisons. This is defensive engineering for GPU kernels with very large loop bodies where naive summation could overflow.

### Phase 5: Cost Model Construction

The cost model object (`sub_2AB2780`, 16 parameters) assembles all analysis results into a single context:

```c
CostModel = {
    Loop*, DominatorTree*, LoopBlocksRPO*, ScalarEvolution*,
    TargetLibraryInfo*, AssumptionCache*, PredicatedScalarEvolution*,
    ValuesToIgnore=0, ORE*,
    /* additional context fields */
}
```

The VPlan planner (`sub_2AF13F0`) generates VPlans for all candidate VFs, then `sub_2AE08E0` selects the best one. Each VPlan recipe provides its own cost through the virtual `getVPCost(VF, CostCtx)` method, which delegates to `NVPTXTargetTransformInfo` for GPU-specific instruction costs.

### Phase 6: Profitability Decision and Interleave Selection

After VF selection, the pass evaluates a decision matrix:

| Condition | Result |
|-----------|--------|
| VF=1, not scalable | `VectorizationNotBeneficial` — bail |
| IC=1 but user wanted more | `InterleavingNotBeneficial` |
| IC>1 but user disabled | `InterleavingBeneficialButDisabled` |
| Histogram loop + scalar interleave | `HistogramPreventsScalarInterleaving` — bail |
| VF=1, IC>1 | Interleave-only path: `executeVPlan(VF=1, IC)` |
| VF>1 | Full vectorization path |

The histogram diagnostic (`HistogramPreventsScalarInterleaving`) is an NVIDIA addition not present in upstream LLVM. It blocks scalar interleaving of histogram-pattern loops where reduction ordering constraints make interleaving incorrect without vectorization.

**Interleave count selection** (`sub_2AED330`) is register-pressure-bounded on GPU:

```cpp
// sub_2AED330 — selectInterleaveCount() (simplified)
maxIC = TTI.getMaxInterleaveFactor(VF)                 // sub_DFB120(TTI+448)
// Override knobs:
if (VF.isScalar() && ForceTargetMaxScalarInterleave)   // dword_500E148
    maxIC = ForceTargetMaxScalarInterleave
if (VF.isVector() && ForceTargetMaxVectorInterleave)   // dword_500E068
    maxIC = ForceTargetMaxVectorInterleave

tripCount = getSmallBestKnownTC(PSE, Loop)
IC = bit_floor(tripCount / (VF * 2))                  // conservative: vector loop runs >= 2x
IC = min(IC, maxIC)

// Small loop boost
if (loopCost < SmallLoopCost)                          // qword_500DC88
    smallIC = min(IC, bit_floor(SmallLoopCost / loopCost))
    IC = max(IC, smallIC)

// Scheduling-based cap (NVIDIA-specific TTI path)
issueWidth = *(TTI + 56 + 32)                          // scheduling info at TTI+88
latency    = *(TTI + 56 + 36)                          // scheduling info at TTI+92
IC = IC / max(issueWidth, latency)                     // cap by scheduling model

// Aggressive interleave mode
if (byte_500D908)                                      // AggressiveInterleave
    IC = maxIC                                         // bypass all heuristics

IC = clamp(IC, 1, maxIC)
return powerOf2Floor(IC)
```

On CPU, the interleave count is bounded by vector register count (e.g., 16 YMM registers / registers per iteration). On GPU, it is bounded by register pressure impact on occupancy — the TTI scheduling info encodes this constraint. The `AggressiveInterleave` knob (`byte_500D908`) bypasses all heuristics and sets IC to the maximum, useful for benchmarking or known-good kernels.

### Phase 7: VPlan Execution and Epilogue Vectorization

```cpp
mainVPlan = getBestPlanFor(bestVF)                     // sub_2BF1320
executeVPlan(mainVPlan, bestVF, IC)                    // sub_2AE3460

// Epilogue vectorization (when byte_500ED88 is set)
epilogueVF = selectEpilogueVectorizationFactor()       // sub_2ABBD40
if (epilogueVF > 1):
    clonedPlan = cloneVPlan(mainVPlan)                 // sub_2BF7CB0
    epiloguePlan = getBestPlanFor(epilogueVF)
    mergeVPlans(clonedPlan, epiloguePlan)              // sub_2AB0350
    // Remap operands between main and epilogue plans:
    //   recipe types 29 (load/store), 36 (phi), 17 (GEP)
    //   types 19-20 (inttoptr/ptrtoint casts)
    executeVPlan(merged, epilogueVF, epilogueIC, isEpilogue=true)
```

Epilogue vectorization is particularly relevant on GPU: the scalar remainder loop after vectorization forces warp divergence (some threads in the warp execute the epilogue while others are masked off), which is expensive. A vectorized epilogue with a smaller VF reduces the scalar remainder to fewer iterations, minimizing divergence overhead.

The epilogue VF selection (`sub_2ABBD40`) can be forced via `qword_500ECA8` (`-epilogue-vectorization-force-VF`). When not forced, it uses SCEV range analysis (`sub_DC3A60`, `sub_DBB9F0`) to prove the epilogue trip count is sufficient for the candidate VF.

### Phase 8: Post-Vectorization Metadata

The pass applies follow-up loop metadata (`llvm.loop.vectorize.followup_all`, `llvm.loop.vectorize.followup_epilogue`) and emits optimization remarks through `sub_2AC2B40`. Generated basic blocks use naming conventions `vec.epilog.middle.block` and `vec.epilog.vector.body`.

## VPlan Construction (sub_2AEE460)

The VPlan builder allocates a 656-byte VPlan object and iterates over candidate VFs in powers of 2 (`VF *= 2` each iteration, visible as `add r15d, r15d` in the binary). For each VF, it calls `sub_2AA9E60` (`tryToBuildRecipesForVF`).

**Recipe type tags** observed in the binary:

| Tag | Recipe Type |
|-----|-------------|
| 0x04 | `VPWidenMemoryInstructionRecipe` |
| 0x0F | `VPWidenRecipe` |
| 0x1D | `VPReplicateRecipe` |
| 0x21 | `VPWidenSelectRecipe` |
| 0x43 | `VPWidenCallRecipe` |

Interleave group recipes are built from `LoopAccessInfo` at `[Planner+0x28]+0x150`. The builder removes individual load/store recipes and replaces them with interleave group recipes via `sub_2AB9570` (`replaceAllUsesWith`), using a hash map with the pointer-hash function `(ptr >> 4) ^ (ptr >> 9) & mask` — identical to LLVM's `DenseMap` hash.

**Cost annotation** happens in Phase 6 of VPlan construction via `sub_2C2E3C0`, which walks all recipes and annotates them with TTI-derived costs. This is where `NVPTXTargetTransformInfo` shapes the cost model: it prices `ld.v4` cheaper than 4x `ld.f32`, making vectorization profitable even with register pressure increase.

The VPlan verification flag at `0x500D2E8` enables VPlan dump/verify paths — useful for debugging vectorization decisions with `-mllvm -vplan-verify-or-dont`.

## NVPTXTargetTransformInfo Hooks

The loop vectorizer reaches NVIDIA's TTI through `Loop->getHeader()->getParent()->getTTI()` (recovered as `*(**(Loop+32)+72)`). Key hooks:

| TTI Method | Address | GPU Behavior |
|------------|---------|-------------|
| `getRegisterBitWidth(Vector)` | `sub_DFE640` | Returns 32 (fixed) — single scalar register width |
| `supportsScalableVectors()` | `sub_DFE610` | Returns `false` — no SVE/RVV equivalent |
| `getMaxInterleaveFactor()` | `sub_DFB120` | Queried at TTI+448; register-pressure-bounded |
| `getMaxInterleaveFactor(vectorized)` | `sub_DFB730` | Separate limit for vectorized loops |
| `hasAttribute(47)` | `sub_B2D610` | "alwaysvectorize" check |
| `hasAttribute(30)` | `sub_B2D610` | "noimplicitfloat" check |

The 32-bit register width return is the critical difference from CPU targets. It means the standard VF formula (`regWidth / elemSize`) produces VF=1 for 32-bit types, VF=2 for 16-bit types, and VF=4 for 8-bit types. Wider vectorization (VF=4 for float) must come from the cost model determining that `ld.v4.f32` is profitable despite the VF exceeding the "register width."

The scheduling info at `TTI+56` (with issue width at offset +32 and latency at +36 within that sub-structure) feeds interleave count capping. This models the SM's instruction issue pipeline: even if register pressure allows IC=8, the issue pipeline may saturate at IC=4.

## Knobs and Thresholds

| Knob | Global Address | CLI Name | Default | Effect |
|------|---------------|----------|---------|--------|
| ForceVectorization | `qword_500D340[17]` | `vectorize-loops` | true | Master switch for loop vectorization |
| EnableEarlyExitVectorization | `byte_500CDA8` | `-enable-early-exit-vectorization` | false | Gates LLVM 20 early-exit loop vectorization |
| ForceOuterLoopVectorization | `byte_500D208` | `-force-vector-width-outer` | false | Forces VF=4 for outer loops when TTI returns VF<=1 |
| ForceCanReorderFP (selector) | `dword_500D508` | — | 0 | Whether FP reorder override is active |
| ForceCanReorderFP (value) | `byte_500D588` | — | — | FP reorder override value |
| ForceScalarEpilogue (selector) | `dword_500E308` | — | 0 | Whether scalar epilogue is forced |
| ForceScalarEpilogue (value) | `byte_500E388` | — | — | Scalar epilogue override value |
| VectorizerMinTripCount | `dword_500EAE8` | `vectorizer-min-trip-count` | 16 (upstream) | Minimum trip count to attempt vectorization |
| CostThreshold | `qword_500EA08` | — | — | Maximum cost for memory reorder safety check |
| EnableEpilogueVectorization | `byte_500ED88` | `-enable-epilogue-vectorization` | true (upstream) | Enables vectorized epilogue loop |
| EpilogueVectorizationForceVF | `qword_500ECA8` | `-epilogue-vectorization-force-VF` | 0 | Forces specific epilogue VF |
| AggressiveInterleave | `byte_500D908` | — | false | Bypasses IC heuristics, sets IC=max |
| PreferPredicateOverEpilogue | `byte_500DAC8` | `prefer-predicate-over-epilogue` | — | Uses predication instead of scalar epilogue |
| SmallLoopCost | `qword_500DC88` | `small-loop-cost` | 20 (upstream) | Threshold below which loops get boosted IC |
| ForceTargetMaxScalarInterleave | `dword_500E148` | `force-target-max-scalar-interleave` | 0 | Overrides max IC for scalar loops |
| ForceTargetMaxVectorInterleave | `dword_500E068` | `force-target-max-vector-interleave` | 0 | Overrides max IC for vectorized loops |

**NVIDIA vs upstream defaults:** The upstream `vectorizer-min-trip-count` default is 16. The upstream `small-loop-cost` default is 20. The upstream `enable-epilogue-vectorization` default is true. NVIDIA preserves these defaults from the knob registration code, but the TTI hooks (particularly `getRegisterBitWidth` returning 32 and `getMaxInterleaveFactor` being register-pressure-bounded) shift the effective behavior dramatically. Where a CPU target with AVX-512 might select VF=16 for float, NVPTX typically selects VF=2 or VF=4 — just enough to use `ld.v2`/`ld.v4` instructions without excessive register pressure.

## Diagnostic Strings

All diagnostic strings are embedded in the binary with `OptimizationRemarkAnalysis` tags. Source: `p2-E01-loop-vectorize.txt`.

| Tag | Message | Trigger |
|-----|---------|---------|
| `UncountableEarlyExitLoopsDisabled` | `"Auto-vectorization of loops with uncountable early exit is not enabled"` | Early-exit loop + `byte_500CDA8` knob off |
| `LowTripCount` | `"The trip count is below the minial threshold value."` | TC < `dword_500EAE8` min threshold (note: "minial" is a typo [sic] in the NVIDIA binary) |
| `NoImplicitFloat` | `"Can't vectorize when the NoImplicitFloat attribute is used"` | Function attribute 30 check |
| `UnsafeFP` | `"Potentially unsafe FP op prevents vectorization"` | FP safety check failure |
| `CantReorderFPOps` | `"loop not vectorized: cannot prove it is safe to reorder floating-point operations"` | FP reorder proof failure |
| `CantReorderMemOps` | `"loop not vectorized: cannot prove it is safe to reorder memory operations"` | Memory reorder proof failure |
| `VectorizationNotBeneficial` | `"the cost-model indicates that vectorization is not beneficial"` | Cost model: VF=1 wins |
| `InterleavingNotBeneficial` | `"the cost-model indicates that interleaving is not beneficial"` | Cost model: IC=1 wins |
| `InterleavingNotBeneficialAndDisabled` | (appended: `" and is explicitly disabled or interleave count is set to 1"`) | IC=1 + explicitly disabled |
| `InterleavingBeneficialButDisabled` | (tag only, no message body recovered) | IC>1 but user disabled interleaving |
| `InterleavingAvoided` | `"Ignoring UserIC, because interleaving was avoided up front"` | User-specified IC overridden |
| `HistogramPreventsScalarInterleaving` | `"Unable to interleave without vectorization due to constraints on the order of histogram operations"` | NVIDIA-specific: histogram loop + scalar IC |
| `ScalableVFUnfeasible` | `"Scalable vectorization requested but not supported by the target"` | Scalable VF on NVPTX |
| `UncountableEarlyExitUnsupported` | `"Auto-vectorization of early exit loops requiring a scalar epilogue is unsupported"` | Early-exit + epilogue |
| (success remark) | `"interleaved loop (interleaved count: N)"` | Vectorization/interleaving succeeded via `sub_2AC2B40` |
| (metadata) | `"llvm.loop.vectorize.followup_all"` | Post-vectorization loop metadata tag |
| (metadata) | `"llvm.loop.vectorize.followup_epilogue"` | Post-vectorization epilogue metadata tag |
| (block name) | `"vec.epilog.middle.block"` | Epilogue vectorization middle block |
| (block name) | `"vec.epilog.vector.body"` | Epilogue vectorization body block |
| (block name) | `"scev.check"` | Runtime SCEV overflow check block (`sub_27C1C30`) |
| (VPlan debug) | `"Initial VPlan"` | VPlan builder debug output at `0x2AEFC7B` |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `LoopVectorizePass::processLoop()` | `sub_2AF1970` | 16.4 KB | — |
| `tryToBuildVPlanWithVPRecipes()` | `sub_2AEE460` | 11.3 KB | — |
| `Planner::plan()` — generate VPlans for candidate VFs | `sub_2AF13F0` | — | — |
| `selectBestVF()` — iterate VPlans, pick lowest cost | `sub_2AE08E0` | — | — |
| `computeCostForVF()` — per-VF cost query | `sub_2AE0750` | — | — |
| `isBetterThan()` — VF cost comparator | `sub_2AB3FE0` | — | — |
| `executeVPlan()` — IR transformation from VPlan | `sub_2AE3460` | — | — |
| `selectInterleaveCount()` — IC heuristic | `sub_2AED330` | — | — |
| `selectEpilogueVectorizationFactor()` | `sub_2ABBD40` | — | — |
| `LoopVectorizationCostModel` constructor (16 params) | `sub_2AB2780` | — | — |
| `selectVectorizationFactor()` — outer loop path | `sub_2AB8AC0` | — | — |
| `selectVectorizationFactor()` — hint/pre-check | `sub_2AAEAB0` | — | — |
| `computeExpectedScalarCost()` | `sub_2AAD640` | — | — |
| `LoopVectorizationLegality::init()` | `sub_31A4FD0` | — | — |
| `canVectorize()` — pre-check | `sub_31A91F0` | — | — |
| `canVectorize()` — full check | `sub_31AF060` | — | — |
| `getBestPlanFor(VF)` — VPlan lookup | `sub_2BF1320` | — | — |
| VPlan clone helper | `sub_2BF7CB0` | — | — |
| Main+epilogue VPlan merge helper | `sub_2AB0350` | — | — |
| `buildInterleaveGroupRecipes()` | `sub_2C06CE0` | — | — |
| VPlan cost annotation pass | `sub_2C2E3C0` | — | — |
| VPlan simplification / recipe combining | `sub_2C32950` | — | — |
| VPlan legality re-verification | `sub_2C2A390` | — | — |
| `getSmallBestKnownTC()` — trip count upper bound | `sub_2AA7EC0` | — | — |
| Per-VF recipe builder | `sub_2AA9E60` | — | — |
| Recipe scaling/widening finalizer | `sub_2AD9850` | — | — |
| `TTI::getMaxInterleaveFactor()` | `sub_DFB120` | — | — |
| `TTI::getRegisterBitWidth(Vector)` | `sub_DFE640` | — | — |
| `TTI::supportsScalableVectors()` | `sub_DFE610` | — | — |
| Emit vectorization success remarks | `sub_2AC2B40` | — | — |
| VPlan fixup/finalize | `sub_ABDAE0` | — | — |

## Related Pages

- [Loop Strength Reduction](lsr.md) — LSR runs after vectorization and must handle the wider induction variables and address expressions that vectorization introduces. NVIDIA's custom LSR is occupancy-aware and interacts with the same register pressure model.
- [Register Allocation](register-allocation.md) — The register pressure that bounds VF and IC decisions is ultimately resolved by the register allocator. VF=4 with IC=2 may request 8x the base register count; the allocator must either accommodate this or spill to local memory.
- [Scheduling](scheduling.md) — The TTI scheduling info (issue width and latency at TTI+56) that caps interleave count comes from the same target model used by instruction scheduling.
- [SelectionDAG](selectiondag.md) — Vectorized IR produces vector types (`<4 x float>`) that SelectionDAG must lower to PTX `ld.v4`/`st.v4` instructions.
- [SLP Vectorizer](slp-vectorizer.md) — SLP vectorization (`sub_2BD1C50`) handles straight-line code and horizontal reductions; loop vectorization handles loop bodies. Both share the same TTI cost model.

## What Upstream LLVM Gets Wrong for GPU

Upstream LLVM's LoopVectorize pass was built for CPU SIMD: fill wider vector registers to process more data elements per instruction. On a GPU, every foundational assumption is inverted:

- **Upstream assumes SIMD lanes need filling.** The CPU vectorizer exists to pack 4/8/16 scalar operations into one vector instruction (SSE/AVX/NEON). On GPU, there are no SIMD lanes in the CPU sense — the SIMT model already executes 32 threads in lockstep per warp. "Vectorization" on GPU means widening per-thread memory accesses to `ld.v2`/`ld.v4` for coalescing, not filling SIMD lanes.
- **Upstream computes VF from vector register width.** The standard formula is `VF = registerWidth / elementSize` (e.g., AVX-512 gives VF=16 for float). NVPTX's `getRegisterBitWidth()` returns 32 bits — a single scalar register width — so this formula always produces VF=1 for 32-bit types. Wider VFs must come entirely from the cost model deciding that `ld.v4.f32` is profitable, bypassing the standard VF selection path.
- **Upstream ignores register pressure when selecting VF.** On CPU, VF=16 using 16 ZMM registers has no throughput penalty — there is no occupancy concept. On GPU, VF=4 that quadruples live registers can cross an occupancy cliff, losing an entire warp group and halving net throughput. Every VF and IC decision must be bounded by register pressure impact on occupancy.
- **Upstream assumes scalable vectors are desirable.** LLVM supports SVE/RISC-V V scalable vector types. NVPTX disables them entirely (`supportsScalableVectors() = false`) because PTX has no scalable vector model — only fixed-width `ld.v2`/`ld.v4` instructions exist.
- **Upstream's interleave count is bounded by CPU port pressure.** CPU IC selection considers execution port contention and register file depth (e.g., 16 YMM registers). GPU IC selection is capped by the TTI scheduling model's issue width and latency at `TTI+56`, reflecting the SM's instruction issue pipeline saturation — a completely different bottleneck.

## Optimization Level Behavior

| Level | Scheduled | Max VF | Interleave | Notes |
|-------|-----------|--------|-----------|-------|
| **O0** | Not run | N/A | N/A | No optimization passes |
| **Ofcmax** | Not run | N/A | N/A | Fast-compile skips vectorization entirely |
| **Ofcmid** | Not run | N/A | N/A | Vectorization not in medium fast-compile tier |
| **O1** | Runs (Tier 1) | 4 | Enabled | Single instance after loop canonicalization |
| **O2** | Runs (Tier 1) | 4 | Enabled | Same scheduling as O1; benefits from more aggressive scalar optimization preceding it |
| **O3** | Runs (Tier 1) | 4 | Enabled | Same as O2; additional Tier 3 loop passes (interchange, distribution) may create more vectorization opportunities |

Loop vectorization is a Tier 1 pass, meaning it runs at O1 and above but not in any fast-compile tier. The maximum VF is effectively capped at 4 by the GPU register pressure constraint — higher VFs would multiply live registers past occupancy cliffs. The `vectorize-loops` knob (`qword_500D340[17]`) can force vectorization even when the cost model says it is unprofitable; this knob defaults to off and is typically used only for debugging. Early-exit vectorization (`byte_500CDA8`) is gated separately and defaults to disabled. See [Optimization Levels](../config/optimization-levels.md) for the complete tier structure.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Vectorization purpose** | Fill SIMD lanes (SSE/AVX/NEON) for data parallelism | Memory coalescing (`ld.v2`/`ld.v4`), instruction count reduction, and register-to-memory width matching; no SIMD lanes on GPU |
| **Scalable vectors** | Supported (SVE, RISC-V V) | Always disabled — `sub_DFE610` returns `false` for NVPTX; only fixed-width VF=2/4 |
| **Register bit width (TTI)** | Target-dependent (128/256/512 for x86) | Fixed 32 bits (`TypeSize::getFixed(32)`) reflecting PTX's 32-bit register model |
| **VF selection cost model** | SIMD-width-driven: higher VF fills wider vector registers | Occupancy-bounded: VF must not increase register pressure past warp occupancy cliffs; VF=4 is typically the maximum |
| **Interleave count** | Profile-guided or port-pressure-based (2–8 typical) | Capped by TTI scheduling info at `TTI+56`; conservative due to register pressure cost per interleaved iteration |
| **Early-exit vectorization** | Experimental (behind flag) | Present, gated by `byte_500CDA8` (`-enable-early-exit-vectorization`) |
| **Convergent call handling** | Standard legality rejection | Additional barrier-aware legality: convergent intrinsics (`__syncthreads`, warp shuffles) block vectorization of the containing loop body |
