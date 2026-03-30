# LoopVectorize and VPlan (GPU-Adapted)

NVIDIA's cicc ships a heavily modified copy of LLVM's `LoopVectorizePass`, the single largest pass in the vectorization pipeline at 88 KB of decompiled output (2,612 lines in `sub_2AF1970`). The modifications do not change the pass's fundamental architecture -- it still builds VPlans, selects a vectorization factor (VF) through cost modeling, and transforms IR through VPlan execution -- but the cost model, VF selection heuristics, interleave count logic, and legality checker are all tuned for a target where "vectorization" means something fundamentally different than on a CPU. On a CPU, loop vectorization fills SIMD lanes: a VF of 4 on SSE processes four `float` elements per vector instruction. On an NVIDIA GPU, there are no SIMD lanes in the CPU sense -- each thread already executes scalar code, and the warp executes 32 threads in lockstep. The reasons to vectorize on GPU are: (1) **memory coalescing** -- adjacent threads issuing adjacent loads produce 128-byte cache line transactions, and vectorizing a per-thread loop body with VF=2 or VF=4 produces `ld.v2`/`ld.v4` wide loads that maximize bytes-per-transaction; (2) **reducing instruction count** -- a single `ld.global.v4.f32` replaces four `ld.global.f32` instructions, saving fetch/decode/issue bandwidth; (3) **register-to-memory width matching** -- PTX supports 32-, 64-, and 128-bit load/store widths, and vectorization widens narrow scalar accesses to fill these naturally.

## Key Facts

| Property | Value |
|----------|-------|
| Main entry point | `sub_2AF1970` (0x2AF1970) -- `LoopVectorizePass::processLoop()` |
| Binary size | 88 KB decompiled, 2,612 lines |
| VPlan builder | `sub_2AEE460` (0x2AEE460) -- `tryToBuildVPlanWithVPRecipes()`, 56 KB |
| VPlan object size | 656 bytes (0x290), consistent with LLVM 17/18 layout |
| LLVM base | LLVM 20 trunk (evidence: histogram-pattern support, early-exit vectorization, VPlan-native path) |
| Scalable vectors | Always disabled -- `sub_DFE610` returns `false` for NVPTX |
| Register bit width (TTI) | 32 bits fixed (`TypeSize::getFixed(32)` in upstream `NVPTXTTIImpl`) |
| Pass name string | `"vectorize-loops"` at 0x439F095 |
| Address cluster | 0x2AA0000--0x2C20000 (loop vectorizer + VPlan infrastructure) |

## Why Vectorize on GPU

GPU vectorization is not about filling SIMD lanes. Each CUDA thread runs scalar code, and 32 threads form a warp that executes in lockstep on one SM. Vectorization targets three orthogonal benefits:

**Memory coalescing width.** The GPU memory subsystem services requests in 128-byte (or 32-byte sector) transactions. When a warp of 32 threads accesses 32 consecutive `float` values (128 bytes total), the hardware coalesces this into a single transaction. But if a single thread's inner loop accesses 4 consecutive floats in sequence, those 4 accesses become 4 separate scalar loads issued over 4 iterations. Vectorizing with VF=4 converts them into one `ld.global.v4.f32`, which the memory subsystem can service in a single wider transaction per thread. Across the warp, this multiplies the effective memory bandwidth.

**Instruction count reduction.** PTX's `ld.v2` and `ld.v4` instructions load 2 or 4 elements with a single instruction. The instruction issue pipeline has finite throughput (typically 1-2 instructions per clock per scheduler), so halving instruction count directly improves throughput-bound kernels.

**Register-width matching.** PTX has 32-bit registers. A 128-bit `ld.v4.f32` loads directly into four consecutive registers via a single instruction, which is strictly better than four separate 32-bit loads (each requiring its own address computation).

These benefits are bounded by **register pressure** -- the primary constraint that does not exist on CPU. On a CPU, using wider vector registers has no occupancy cost. On a GPU, every additional register per thread reduces the number of threads that fit on an SM, potentially crossing an occupancy cliff. A VF=4 vectorization that quadruples the live register count may halve occupancy and lose net throughput.

## The 8-Phase Pipeline

The main function `sub_2AF1970` implements eight phases, closely following upstream LLVM's structure but with GPU-specific decision points at each stage.

### Phase 1: Legality Pre-Check

```
sub_31A4FD0(legalityCtx, Loop, Function, ORE, SE)    // init legality scratch
TTI = *(**(Loop+32) + 72)                             // Loop->getHeader()->getParent()->getTTI()
if (!sub_31A91F0(legalityCtx, TTI, Loop, LoopInfo))   // canVectorize() quick check
    return false

sub_31AF060(costCtx, ForceVectorization)              // canVectorize() full check
// ForceVectorization = qword_500D340[17] ("vectorize-loops" knob)
```

The legality checker (`sub_31AF060`) performs standard LLVM legality analysis: loop simplify form, single exit, computable backedge-taken count, no irreducible control flow. The NVIDIA-specific addition is early-exit loop handling:

```
if (hasUncountableEarlyExit && !byte_500CDA8)         // -enable-early-exit-vectorization
    emit "UncountableEarlyExitLoopsDisabled"
    return false
```

This knob (`byte_500CDA8`) gates an LLVM 20 feature that NVIDIA includes but disables by default. Early-exit vectorization requires predicated execution, which on GPU means divergent warps -- typically unprofitable.

### Phase 2: Outer vs Inner Loop Dispatch

```
if (Loop->getSubLoops().size() > 0)
    goto outerLoopPath                                // PATH A (rarely taken on GPU)
else
    goto innerLoopPath                                // PATH B (the main path)
```

Outer loop vectorization is controlled by `byte_500D208` (`-force-vector-width-outer`). When enabled and TTI-based VF selection returns VF <= 1, the pass forces VF=4 -- a hardcoded NVIDIA override for kernel patterns where outer-loop vectorization benefits warp-level memory access patterns. In practice, inner loop vectorization (Path B) handles the vast majority of GPU kernels.

### Phase 3: Trip Count and Safety Checks

```
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

This is where NVIDIA diverges most from upstream. The upstream algorithm queries `TTI::getRegisterBitWidth()` which returns the vector register width (256 for AVX2, 512 for AVX-512), then computes `VF = registerWidth / elementSize`. On NVPTX, `getRegisterBitWidth()` returns **32** -- a single scalar register width. This means the upstream formula would always produce VF=1 for 32-bit types.

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

The cost accumulation uses **saturating arithmetic** -- `__OFADD__` overflow detection clamping to `INT64_MAX`/`INT64_MIN` -- preventing wrap-around in cost comparisons. This is defensive engineering for GPU kernels with very large loop bodies where naive summation could overflow.

### Phase 5: Cost Model Construction

The cost model object (`sub_2AB2780`, 16 parameters) assembles all analysis results into a single context:

```
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
| VF=1, not scalable | `VectorizationNotBeneficial` -- bail |
| IC=1 but user wanted more | `InterleavingNotBeneficial` |
| IC>1 but user disabled | `InterleavingBeneficialButDisabled` |
| Histogram loop + scalar interleave | `HistogramPreventsScalarInterleaving` -- bail |
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

On CPU, the interleave count is bounded by vector register count (e.g., 16 YMM registers / registers per iteration). On GPU, it is bounded by register pressure impact on occupancy -- the TTI scheduling info encodes this constraint. The `AggressiveInterleave` knob (`byte_500D908`) bypasses all heuristics and sets IC to the maximum, useful for benchmarking or known-good kernels.

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

Interleave group recipes are built from `LoopAccessInfo` at `[Planner+0x28]+0x150`. The builder removes individual load/store recipes and replaces them with interleave group recipes via `sub_2AB9570` (`replaceAllUsesWith`), using a hash map with the pointer-hash function `(ptr >> 4) ^ (ptr >> 9) & mask` -- identical to LLVM's `DenseMap` hash.

**Cost annotation** happens in Phase 6 of VPlan construction via `sub_2C2E3C0`, which walks all recipes and annotates them with TTI-derived costs. This is where `NVPTXTargetTransformInfo` shapes the cost model: it prices `ld.v4` cheaper than 4x `ld.f32`, making vectorization profitable even with register pressure increase.

The VPlan verification flag at `0x500D2E8` enables VPlan dump/verify paths -- useful for debugging vectorization decisions with `-mllvm -vplan-verify-or-dont`.

## NVPTXTargetTransformInfo Hooks

The loop vectorizer reaches NVIDIA's TTI through `Loop->getHeader()->getParent()->getTTI()` (recovered as `*(**(Loop+32)+72)`). Key hooks:

| TTI Method | Address | GPU Behavior |
|------------|---------|-------------|
| `getRegisterBitWidth(Vector)` | `sub_DFE640` | Returns 32 (fixed) -- single scalar register width |
| `supportsScalableVectors()` | `sub_DFE610` | Returns `false` -- no SVE/RVV equivalent |
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
| ForceCanReorderFP (selector) | `dword_500D508` | -- | 0 | Whether FP reorder override is active |
| ForceCanReorderFP (value) | `byte_500D588` | -- | -- | FP reorder override value |
| ForceScalarEpilogue (selector) | `dword_500E308` | -- | 0 | Whether scalar epilogue is forced |
| ForceScalarEpilogue (value) | `byte_500E388` | -- | -- | Scalar epilogue override value |
| VectorizerMinTripCount | `dword_500EAE8` | `vectorizer-min-trip-count` | 16 (upstream) | Minimum trip count to attempt vectorization |
| CostThreshold | `qword_500EA08` | -- | -- | Maximum cost for memory reorder safety check |
| EnableEpilogueVectorization | `byte_500ED88` | `-enable-epilogue-vectorization` | true (upstream) | Enables vectorized epilogue loop |
| EpilogueVectorizationForceVF | `qword_500ECA8` | `-epilogue-vectorization-force-VF` | 0 | Forces specific epilogue VF |
| AggressiveInterleave | `byte_500D908` | -- | false | Bypasses IC heuristics, sets IC=max |
| PreferPredicateOverEpilogue | `byte_500DAC8` | `prefer-predicate-over-epilogue` | -- | Uses predication instead of scalar epilogue |
| SmallLoopCost | `qword_500DC88` | `small-loop-cost` | 20 (upstream) | Threshold below which loops get boosted IC |
| ForceTargetMaxScalarInterleave | `dword_500E148` | `force-target-max-scalar-interleave` | 0 | Overrides max IC for scalar loops |
| ForceTargetMaxVectorInterleave | `dword_500E068` | `force-target-max-vector-interleave` | 0 | Overrides max IC for vectorized loops |

**NVIDIA vs upstream defaults:** The upstream `vectorizer-min-trip-count` default is 16. The upstream `small-loop-cost` default is 20. The upstream `enable-epilogue-vectorization` default is true. NVIDIA preserves these defaults from the knob registration code, but the TTI hooks (particularly `getRegisterBitWidth` returning 32 and `getMaxInterleaveFactor` being register-pressure-bounded) shift the effective behavior dramatically. Where a CPU target with AVX-512 might select VF=16 for float, NVPTX typically selects VF=2 or VF=4 -- just enough to use `ld.v2`/`ld.v4` instructions without excessive register pressure.

## Diagnostic Strings

All diagnostic strings are embedded in the binary with `OptimizationRemarkAnalysis` tags:

| Tag | Message | Trigger |
|-----|---------|---------|
| `UncountableEarlyExitLoopsDisabled` | "Auto-vectorization of loops with uncountable early exit is not enabled" | Early-exit loop + knob off |
| `LowTripCount` | "The trip count is below the minial threshold value." | TC < min threshold (note: "minial" is a typo in the NVIDIA binary) |
| `NoImplicitFloat` | "Can't vectorize when the NoImplicitFloat attribute is used" | Function attribute check |
| `UnsafeFP` | "Potentially unsafe FP op prevents vectorization" | FP safety check failure |
| `CantReorderFPOps` | "loop not vectorized: cannot prove it is safe to reorder floating-point operations" | FP reorder proof failure |
| `CantReorderMemOps` | "loop not vectorized: cannot prove it is safe to reorder memory operations" | Memory reorder proof failure |
| `VectorizationNotBeneficial` | "the cost-model indicates that vectorization is not beneficial" | Cost model: VF=1 wins |
| `InterleavingNotBeneficial` | "the cost-model indicates that interleaving is not beneficial" | Cost model: IC=1 wins |
| `HistogramPreventsScalarInterleaving` | "Unable to interleave without vectorization due to constraints on the order of histogram operations" | NVIDIA-specific: histogram loop + scalar IC |
| `ScalableVFUnfeasible` | "Scalable vectorization requested but not supported by the target" | Scalable VF on NVPTX |
| `UncountableEarlyExitUnsupported` | "Auto-vectorization of early exit loops requiring a scalar epilogue is unsupported" | Early-exit + epilogue |

## Function Map

| Address | Identity | Size |
|---------|----------|------|
| `sub_2AF1970` | `LoopVectorizePass::processLoop()` | 88 KB |
| `sub_2AEE460` | `tryToBuildVPlanWithVPRecipes()` | 56 KB |
| `sub_2AF13F0` | `Planner::plan()` -- generate VPlans for candidate VFs | -- |
| `sub_2AE08E0` | `selectBestVF()` -- iterate VPlans, pick lowest cost | -- |
| `sub_2AE0750` | `computeCostForVF()` -- per-VF cost query | -- |
| `sub_2AB3FE0` | `isBetterThan()` -- VF cost comparator | -- |
| `sub_2AE3460` | `executeVPlan()` -- IR transformation from VPlan | -- |
| `sub_2AED330` | `selectInterleaveCount()` -- IC heuristic | -- |
| `sub_2ABBD40` | `selectEpilogueVectorizationFactor()` | -- |
| `sub_2AB2780` | `LoopVectorizationCostModel` constructor (16 params) | -- |
| `sub_2AB8AC0` | `selectVectorizationFactor()` -- outer loop path | -- |
| `sub_2AAEAB0` | `selectVectorizationFactor()` -- hint/pre-check | -- |
| `sub_2AAD640` | `computeExpectedScalarCost()` | -- |
| `sub_31A4FD0` | `LoopVectorizationLegality::init()` | -- |
| `sub_31A91F0` | `canVectorize()` -- pre-check | -- |
| `sub_31AF060` | `canVectorize()` -- full check | -- |
| `sub_2BF1320` | `getBestPlanFor(VF)` -- VPlan lookup | -- |
| `sub_2BF7CB0` | `cloneVPlan()` | -- |
| `sub_2AB0350` | `mergeVPlans()` -- main + epilogue merge | -- |
| `sub_2C06CE0` | `buildInterleaveGroupRecipes()` | -- |
| `sub_2C2E3C0` | VPlan cost annotation pass | -- |
| `sub_2C32950` | VPlan simplification / recipe combining | -- |
| `sub_2C2A390` | VPlan legality re-verification | -- |
| `sub_2AA7EC0` | `getSmallBestKnownTC()` -- trip count upper bound | -- |
| `sub_2AA9E60` | `tryToBuildRecipesForVF()` -- per-VF body builder | -- |
| `sub_2AD9850` | `finalizeRecipesForVF()` -- scaling/widening | -- |
| `sub_DFB120` | `TTI::getMaxInterleaveFactor()` | -- |
| `sub_DFE640` | `TTI::getRegisterBitWidth(Vector)` | -- |
| `sub_DFE610` | `TTI::supportsScalableVectors()` | -- |
| `sub_2AC2B40` | Emit vectorization success remarks | -- |
| `sub_ABDAE0` | VPlan fixup/finalize | -- |

## Related Pages

- [Loop Strength Reduction](lsr.md) -- LSR runs after vectorization and must handle the wider induction variables and address expressions that vectorization introduces. NVIDIA's custom LSR is occupancy-aware and interacts with the same register pressure model.
- [Register Allocation](register-allocation.md) -- The register pressure that bounds VF and IC decisions is ultimately resolved by the register allocator. VF=4 with IC=2 may request 8x the base register count; the allocator must either accommodate this or spill to local memory.
- [Scheduling](scheduling.md) -- The TTI scheduling info (issue width and latency at TTI+56) that caps interleave count comes from the same target model used by instruction scheduling.
- [SelectionDAG](selectiondag.md) -- Vectorized IR produces vector types (`<4 x float>`) that SelectionDAG must lower to PTX `ld.v4`/`st.v4` instructions.
- [SLP Vectorizer](slp-vectorizer.md) -- SLP vectorization (`sub_2BD1C50`) handles straight-line code and horizontal reductions; loop vectorization handles loop bodies. Both share the same TTI cost model.
