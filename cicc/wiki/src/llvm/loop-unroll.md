# Loop Unrolling

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/Scalar/LoopUnrollPass.cpp` (decision engine), `llvm/lib/Transforms/Utils/LoopUnroll.cpp` (transformation engine), `llvm/lib/Transforms/Utils/LoopUnrollRuntime.cpp` (runtime unrolling) (LLVM 20.0.0)

Loop unrolling in cicc is one of the most heavily tuned transformations in the entire pipeline. On a GPU, unrolling directly trades register pressure against instruction-level parallelism: every additional copy of the loop body increases live register count, which reduces SM occupancy and the number of concurrent warps available to hide memory latency. Conversely, too little unrolling leaves performance on the table by failing to expose independent instructions that the hardware scheduler can overlap. NVIDIA's unroller resolves this tension through a priority-based decision cascade with GPU-specific heuristics that have no upstream equivalent -- most notably a local-array threshold multiplier, power-of-two factor enforcement, and a pragma threshold 200x larger than stock LLVM. The transformation engine itself is a lightly modified version of upstream `llvm::UnrollLoop`, but the decision engine (`computeUnrollCount`) is substantially reworked.

The pass appears twice in the cicc pipeline. The first invocation (`sub_197E720`) runs early, interleaved with loop vectorization in the main optimization sequence. The second invocation (`sub_19C1680`) runs later as a cleanup pass, gated by `opts[1360]` (the `nv-disable-loop-unrolling` flag). Both share the same decision engine; the second invocation operates on loops that were created or exposed by intervening passes (InstCombine, SROA, EarlyCSE).

| Property | Value |
|---|---|
| Decision engine | `sub_19BB5C0` / `computeUnrollCount` (50 KB, ~1681 lines) |
| Transformation engine | `sub_2A15A20` / `UnrollLoop` (85 KB, ~2434 lines) |
| Top-level driver | `sub_19BE360` / `tryToUnrollLoop` |
| Runtime-check unroller | `sub_2A25260` / `UnrollLoopWithRuntimeChecks` (91 KB) |
| Pipeline slot (early) | `sub_197E720` -- runs once in main opt pipeline |
| Pipeline slot (late) | `sub_19C1680` -- conditional on `!opts[1360]` |
| Disable knob | `-Xcicc "-disable-LoopUnrollPass"` or `opts[1360]` |
| LLVM base | `LoopUnrollPass` from LLVM 20.0.0 |


## Why Unrolling Matters More on GPU

On a CPU, the primary benefit of unrolling is reducing branch overhead and enabling wider SIMD scheduling. On a GPU, the calculus is different in three ways that all trace back to the [GPU execution model](../gpu-execution-model.md):

First, unrolling increases register pressure, and register pressure determines [occupancy](../gpu-execution-model.md#register-pressure-and-occupancy). If unrolling pushes a kernel from 64 to 96 registers per thread, the SM drops from 32 to 21 resident warps -- a 34% reduction. Fewer warps means less [latency hiding](../gpu-execution-model.md#no-out-of-order-execution), so the unroll factor selection must be conservative in ways that a CPU unroller never needs to be.

Second, there is [no out-of-order execution](../gpu-execution-model.md#no-out-of-order-execution) within a warp; the hardware issues instructions in program order. Unrolling creates independent instructions that the compiler (ptxas) can interleave, particularly independent loads that can overlap with arithmetic. This is the ILP benefit, and it is the primary argument *for* aggressive unrolling.

Third, GPU loops often access shared memory (`__shared__`) or local memory arrays indexed by `threadIdx`. Unrolling these loops enables the backend to promote array elements to registers and to rearrange memory accesses to avoid bank conflicts. NVIDIA's local-array heuristic (see below) exists specifically to exploit this opportunity.

The unroller's job is to find the sweet spot: enough copies to saturate the instruction pipeline, few enough to keep register pressure within occupancy targets.


## The Decision Engine: computeUnrollCount

The decision engine at `sub_19BB5C0` implements a strict six-level priority cascade. Each level is tried in order; the first level that produces a valid unroll factor wins. Every decision is logged through optimization remarks, making the logic traceable from `-Rpass-analysis=loop-unroll`.

### UnrollParams Struct Layout

The decision communicates its result through a struct passed by pointer (`a12` / `v14`):

| Offset | Field | Type | Description |
|---|---|---|---|
| +0 | Threshold | u32 | Cost budget for full unroll |
| +4 | MaxPercentThresholdBoost | u32 | Max boost percentage (default 400) |
| +12 | PartialThreshold | u32 | Cost budget for partial unroll |
| +20 | Count | u32 | Chosen unroll factor (primary output) |
| +24 | PeelCount | u32 | Loop peel iteration count |
| +28 | DefaultUnrollCount | u32 | Fallback count when no factor found |
| +32 | MaxCount | u32 | Hard cap on unroll factor |
| +36 | FullUnrollMaxCount | u32 | Max trip count for full unroll |
| +40 | FixedCost | u32 | Non-scaling cost (IV increments, branches) |
| +44 | AllowPartial | u8 | Partial unrolling permitted |
| +45 | AllowRemainder | u8 | Remainder loop generation permitted |
| +46 | UserProvidedCount | u8 | True when pragma supplies count |
| +48 | (reserved) | u8 | -- |
| +49 | AllowUpperBound | u8 | Use max-trip-count when exact unknown |

### The Cost Model

Every decision in the cascade uses the same linear cost model to estimate unrolled loop size:

```
estimated_size = FixedCost + Count * (LoopBodySize - FixedCost)
```

`LoopBodySize` is the instruction cost of one iteration (parameter `a11`, computed by LLVM's `CodeMetrics`). `FixedCost` captures instructions that do not replicate with unrolling -- induction variable increments, the backedge branch, loop overhead. The difference `(LoopBodySize - FixedCost)` is the per-copy marginal cost.

For full unrolls, an additional dynamic cost simulation (`sub_19B9A90`) constant-folds through the unrolled body. If the loop contains iteration-dependent simplifications (constant array indices, strength-reduced expressions), the simulation reports a cost lower than worst-case. The effective budget for this check is boosted:

```
dynamic_budget = Threshold * MaxPercentThresholdBoost / 100
```

With the default boost of 400%, this means a loop whose body simplifies substantially after unrolling gets 4x the normal cost budget.


### Priority Cascade (Pseudocode)

```c
int computeUnrollCount(Loop *L, SE, TTI, TripCount, MaxTripCount,
                       BodySize, UnrollParams *UP, bool *AllowRuntime) {

    // PRIORITY 1: Local array threshold multiplier (NVIDIA-specific)
    int localSize = computeLocalArraySize(L);  // scans for AS5 allocas
    int multiplier = min(max(localSize, 1), 6);
    int effectiveThreshold = multiplier * UP->Threshold;

    // PRIORITY 2: #pragma unroll N
    int pragmaCount = getMetadataCount(L, "llvm.loop.unroll.count");
    if (pragmaCount != 0) {
        if (pragmaCount == 1) {
            UP->Count = 1;  // disable unrolling
            return UNROLL_DISABLED;
        }
        UP->Count = pragmaCount;
        int estSize = UP->FixedCost + pragmaCount * (BodySize - UP->FixedCost);
        if (estSize > multiplier * PragmaUnrollThreshold) {
            // too large -- try to find smaller factor
            searchSmallerDivisibleFactor(UP, TripCount);
        }
        if (TripMultiple % pragmaCount != 0)
            emitRemark("remainder loops not allowed");
        return UNROLL_PRAGMA;
    }

    // PRIORITY 3: #pragma unroll (full, no count)
    if (hasMetadata(L, "llvm.loop.unroll.full")) {
        if (TripCount > 0 && TripCount <= UP->FullUnrollMaxCount) {
            int estSize = UP->FixedCost + TripCount * (BodySize - UP->FixedCost);
            if (estSize <= effectiveThreshold) {
                if (simulateLoopBody(L, TripCount, dynamicBudget))
                    { UP->Count = TripCount; return FULL_UNROLL; }
            }
        }
        // fallthrough to lower priorities
    }

    // PRIORITY 4: Loop peeling
    int peelCount = computePeelCount(L, SE, UP);
    if (peelCount > 0) {
        UP->PeelCount = peelCount;
        UP->Count = 1;
        return PEEL;
    }

    // PRIORITY 5: Static partial unrolling (known trip count)
    if (TripCount > 0 && (UP->AllowPartial || pragmaOversize) && isInnermost(L)) {
        int count = UP->Count ? UP->Count : UP->DefaultUnrollCount;

        // Size clamp
        if (UP->PartialThreshold < UP->FixedCost + count * (BodySize - UP->FixedCost))
            count = (UP->PartialThreshold - UP->FixedCost) / (BodySize - UP->FixedCost);
        count = min(count, UP->MaxCount);

        // Power-of-two + trip-divisible search
        while (count > 0) {
            if (TripCount % count == 0 && isPowerOfTwo(count))
                break;
            count--;
        }

        // Fallback: halve DefaultUnrollCount until it fits
        if (count == 0 && UP->UserProvidedCount) {
            count = UP->DefaultUnrollCount;
            while (UP->PartialThreshold <
                   UP->FixedCost + count * (BodySize - UP->FixedCost))
                count >>= 1;
        }

        if (count > 1) { UP->Count = count; return PARTIAL_UNROLL; }
    }

    // PRIORITY 6: Runtime unrolling (unknown trip count)
    if (!hasMetadata(L, "llvm.loop.unroll.runtime.disable")
        && RuntimeUnrollThreshold >= BodySize
        && isInnermost(L)) {

        int rtTripCount = computeRuntimeTripCount(L, SE);
        if (rtTripCount < FlatLoopTripCountThreshold) return NO_UNROLL;

        int count = UP->Count ? UP->Count : UP->DefaultUnrollCount;
        // same halving + threshold logic as Priority 5
        while (UP->PartialThreshold <
               UP->FixedCost + count * (BodySize - UP->FixedCost))
            count >>= 1;
        count = min(count, UP->MaxCount);

        if (count > 1) {
            UP->Count = count;
            *AllowRuntime = true;
            return RUNTIME_UNROLL;
        }
    }

    // Small-function override (tiny kernels get aggressive unrolling)
    if (functionInstructionCount < SmallFunctionThreshold)
        return handleSmallFunction(L, UP, BodySize);

    return NO_UNROLL;
}
```


## Local Array Heuristic

The function `sub_19B5DD0` (`computeLocalArraySize`) is entirely NVIDIA-specific. It scans every basic block in the loop for load/store instructions that access address space 5 (GPU local memory). For each such access, it traces back to the underlying `alloca`, determines the array type, and computes the product of array dimensions. If any dimension is unknown at compile time, it substitutes the `unroll-assumed-size` knob (default 4). The returned value is the maximum local-array size found across all accesses.

This value becomes a threshold multiplier, capped at 6:

```c
int computeLocalArraySize(Loop *L) {
    int maxSize = 0;
    for (BasicBlock *BB : L->blocks()) {
        for (Instruction &I : *BB) {
            if (!isLoadOrStore(I) || getAddressSpace(I) != 5) continue;
            Value *base = getUnderlyingAlloca(I);
            if (!base || !isArrayType(base->getType())) continue;
            int size = 1;
            for (int dim : getArrayDimensions(base))
                size *= (dim > 0) ? dim : UnrollAssumedSize;  // default 4
            maxSize = max(maxSize, size);
        }
    }
    return maxSize;
}
```

The rationale: GPU kernels frequently use `__shared__` or local arrays indexed by `threadIdx`. Unrolling such loops by a factor proportional to the array size enables register promotion of individual array elements and eliminates bank-conflict-prone access patterns. The cap at 6 prevents pathological explosion when arrays are large.


## Power-of-Two Factor Enforcement

The partial-unroll factor search at Priority 5 requires the chosen count to satisfy two constraints simultaneously: it must evenly divide the trip count **and** must be a power of two. The implementation uses the classic bitmask test:

```c
while (count > 0) {
    if (tripCount % count == 0 && (count & (count - 1)) == 0)
        break;
    count--;
}
```

This is a GPU-specific requirement. Warp size is 32 (a power of two), and many GPU memory access patterns, shared-memory bank calculations, and reduction operations assume power-of-two alignment. An unroll factor of, say, 6 would create asymmetric loop bodies that interact poorly with warp-level execution.


## Pragma Handling

The frontend (`sub_9305A0` / `emitUnrollPragma`) translates CUDA pragmas to LLVM metadata during codegen:

| CUDA Source | LLVM Metadata |
|---|---|
| `#pragma unroll` (bare) | `!{!"llvm.loop.unroll.full"}` |
| `#pragma unroll N` (N > 1) | `!{!"llvm.loop.unroll.count", i32 N}` |
| `#pragma unroll 1` | Disables unrolling at Priority 2 |

The metadata is attached to the backedge branch as a self-referential `!llvm.loop` node. A guard flag (`dword_4D046B4`) skips pragma processing entirely in fast-codegen mode.

The pragma threshold is 32768 (0x8000), compared to upstream LLVM's 16384 (0x4000). This means `#pragma unroll` succeeds on loop bodies up to approximately 32K cost units -- covering virtually any realistic GPU kernel loop. When even this generous budget is exceeded, the decision engine falls through to lower priorities and attempts partial unrolling.

The `__launch_bounds__` attribute does not directly feed the unroll decision. Instead, it constrains register allocation downstream, which indirectly limits the benefit of aggressive unrolling. There is no feedback loop from register pressure estimation back into the unroll factor at this stage of the pipeline; that coordination happens implicitly through the `PartialThreshold` provided by TTI.


## Runtime Unrolling

Runtime unrolling (Priority 6) handles loops whose trip count is unknown at compile time. cicc enables it by default (`unroll-runtime = true`), with several GPU-specific twists:

**Convergent instruction support.** The knob `unroll-runtime-convergent` (default `true`, NVIDIA-specific) allows unrolling loops that contain convergent operations like warp-level primitives (`__shfl_sync`, `__ballot_sync`). Upstream LLVM refuses to unroll such loops because it cannot guarantee all threads in the warp execute the same iterations. cicc overrides this, relying on the waterfall-epilogue mechanism to preserve convergence.

**Epilog vs. prolog remainder.** The choice is controlled by a cascade:
1. If `waterfall-unrolling-force-epilogue` is `true` (default, NVIDIA-specific) and the loop has runtime trip count: epilog mode is selected.
2. If the loop body contains function calls (`hasCallInLoop` / `sub_2A10B40` checks for opcode 17): epilog mode is forced. This preserves the property that all threads in a warp participate in calls, which matters for convergent operations.
3. Otherwise, `unroll-runtime-epilog` (default `false`) determines the mode.

In practice, GPU loops almost always use epilog-style remainders.

**Flat-loop exclusion.** If the estimated runtime trip count is below `flat-loop-tripcount-threshold` (default 5), runtime unrolling is skipped. The overhead of generating the modulo check and epilog loop is not worth it for loops that iterate fewer than 5 times.

**Body size gate.** Runtime unrolling only proceeds if `runtime-unroll-threshold` (default 95) is greater than or equal to the loop body size. This is more conservative than the static partial-unroll threshold, preventing code explosion for large loop bodies when the trip count is unknown.


## Thresholds: NVIDIA vs. Upstream LLVM

| Parameter | Upstream LLVM (O3) | Upstream LLVM (NVPTX TTI) | cicc v13.0 |
|---|---|---|---|
| Threshold | 300 | 300 | From TTI (300), then multiplied by local-array factor (1-6x) |
| PartialThreshold | 150 | 75 (`Threshold / 4`) | From TTI (75), plus local-array scaling |
| MaxPercentThresholdBoost | 400% | 400% | 400% (same) |
| PragmaUnrollThreshold | 16384 | 16384 | **32768** |
| RuntimeUnrollThreshold | -- | -- | **95** (NVIDIA addition) |
| FlatLoopTripCountThreshold | 5 | 5 | 5 (same) |
| MaxUpperBound | 8 | 8 | 8 (same) |
| MaxPragmaUpperBound | -- | -- | **64** (NVIDIA addition) |
| DefaultUnrollRuntimeCount | 8 | 8 | From TTI |
| AllowPartial | false | **true** | true (from TTI) |
| Runtime | false | **true** | true (from TTI) |
| AllowRemainder | true | true | true |
| MaxIterationsCountToAnalyze | 10 | 10 | 10 (same) |
| UnrollAssumedSize | -- | -- | **4** (NVIDIA addition) |

The critical differences: cicc doubles the pragma threshold, introduces a body-size gate for runtime unrolling (95), adds the local-array multiplier (up to 6x on base thresholds), and enforces power-of-two partial factors. The upstream NVPTX TTI enables partial and runtime unrolling but leaves thresholds at modest CPU-oriented values; cicc's decision engine applies substantial additional logic on top.


## Interaction with Loop Vectorization

In the cicc pipeline, loop vectorization (`LoopVectorizePass`) runs before the first unroll invocation. Specifically, `sub_197E720` combines both vectorization and unrolling decisions in the early pipeline slot. The vectorizer decides the vector width first (VF), and if it applies a transformation, the resulting loop (possibly with a scalar epilog) is then presented to the unroller.

This means vectorization and unrolling do not "coordinate" in the planning sense -- the vectorizer runs to completion before the unroller sees the loop. However, the vectorizer's interleave count (IC) serves a similar role to unrolling: it replicates the vectorized loop body to increase ILP. When the vectorizer chooses IC > 1, the subsequent unroller typically finds the loop body too large to unroll further, producing a de facto coordination through cost thresholds.

The second unroll invocation (`sub_19C1680`) runs much later, after InstCombine, SROA, and EarlyCSE have had a chance to simplify the vectorized code. Loops that were too large to unroll earlier may become eligible after dead code elimination within the unrolled-and-vectorized body.


## The Transformation Engine: UnrollLoop

The transformation at `sub_2A15A20` takes a loop and an unroll factor and physically duplicates the loop body. It is structurally close to upstream `llvm::UnrollLoop` with the following entry guards:

1. Loop must have a preheader (`sub_D4B130`)
2. Loop must have a single latch (`sub_D47930`)
3. Loop must be in LCSSA form (`sub_D49210`)
4. Header flags must be clean (no special bits set)

The duplication proceeds by iterating `Count - 1` times, each iteration cloning every basic block in the loop body, remapping instructions through a value map, and rewiring PHI nodes so that iteration `i`'s latch feeds iteration `i+1`'s header. After all copies, the backedge of the last copy is reconnected to the first copy's header (for partial unroll) or removed entirely (for full unroll).

For partial unrolls where `TripCount % Count != 0`, a remainder loop is generated by `sub_2A23640`. If remainder generation fails (e.g., multi-exit loops), the engine delegates to `sub_2A25260` which generates the runtime-check variant with prologue/epilogue.

The return value encodes the result: 0 = no change, 1 = partial unroll, 2 = full unroll.


## Configuration Knobs

### Standard LLVM Knobs (with NVIDIA defaults)

| Knob | Default | Global | Effect |
|---|---|---|---|
| `unroll-threshold` | From TTI | `sub_19B7760` struct | Base cost budget for full unroll |
| `unroll-partial-threshold` | From TTI | `0x4FB3140` area | Cost budget for partial unroll |
| `unroll-max-percent-threshold-boost` | 400 | `dword_4FB3100` | Max dynamic cost boost (%) |
| `unroll-max-iteration-count-to-analyze` | 10 | `dword_4FB3020` | Max iterations for cost simulation |
| `unroll-count` | Unset | `dword_4FB2EA8` | Force specific unroll factor |
| `unroll-max-count` | Unset | `sub_19B7760` struct | Hard cap on unroll factor |
| `unroll-full-max-count` | Unset | `0x4FB2CE0` area | Max trip count for full unroll |
| `unroll-peel-count` | Unset | `0x4FB2C00` area | Force specific peel count |
| `unroll-allow-partial` | false | `0x4FB2B20` area | Enable partial unrolling override |
| `unroll-allow-remainder` | false | `0x4FB2A40` area | Enable remainder loop generation |
| `unroll-runtime` | true | `0x4FB2960` area | Enable runtime (dynamic TC) unrolling |
| `unroll-max-upperbound` | 8 | `dword_4FB2920` | Max trip count for upper-bound unroll |
| `pragma-unroll-threshold` | 32768 | `dword_4FB2760` | Cost budget for pragma-directed unrolls |
| `flat-loop-tripcount-threshold` | 5 | `0x4FB2680` area | Min estimated TC for runtime unroll |
| `runtime-unroll-threshold` | 95 | `dword_4FB3560` | Max body size for runtime unroll |
| `max-pragma-upperbound-unroll` | 64 | `dword_4FB2840` | Max upper-bound factor for pragma |
| `unroll-assumed-size` | 4 | `dword_4FB33A0` | Assumed array size for unknown dims |

### NVIDIA-Specific Knobs

| Knob | Default | Global | Effect |
|---|---|---|---|
| `unroll-runtime-convergent` | true | `0x500A440` area | Allow unrolling loops with convergent ops |
| `unroll-runtime-epilog` | false | `qword_500A3E8` | Force epilog-style remainder (override) |
| `waterfall-unrolling-force-epilogue` | true | `qword_500A148` | Force epilog for waterfall patterns |

Knobs are registered in two constructors: standard LLVM knobs in `ctor_216_0` at `0x4E5C30`, NVIDIA-specific knobs in `ctor_501` at `0x559890`.


## Function Map

| Address | Identity | Role |
|---|---|---|
| `0x09305A0` | `emitUnrollPragma` | Frontend: `#pragma unroll` to metadata |
| `0x19B4C50` | `parseUnrollMetadata` | Reads `llvm.loop.unroll.*` metadata |
| `0x19B5DD0` | `computeLocalArraySize` | NVIDIA: local array threshold heuristic |
| `0x19B6500` | `handleSmallFunction` | Special aggressive unroll for tiny kernels |
| `0x19B6690` | `selectUnrollFactor` | Trip count analysis helper |
| `0x19B78B0` | `emitRemainderNotAllowedRemark` | Diagnostic emission |
| `0x19B9A90` | `simulateLoopBody` | Dynamic cost simulation with constant folding |
| `0x19BB5C0` | `computeUnrollCount` | Main decision engine |
| `0x19BE360` | `tryToUnrollLoop` | Top-level driver |
| `0x1B0B080` | `computePeelCount` | Loop peeling logic |
| `0x1B18810` | `computeRuntimeTripCount` | Runtime trip count estimation |
| `0x2A10B40` | `hasCallInLoop` | Checks for call/invoke in loop body |
| `0x2A10DD0` | `createSideExitPHI` | PHI nodes for side-exit unrolled loops |
| `0x2A12AD0` | `cloneInstructionsInBlock` | Instruction-level cloning |
| `0x2A13F00` | `reconcileLoopAfterUnroll` | Post-unroll SCEV/LoopInfo fixup |
| `0x2A15A20` | `UnrollLoop` | Main transformation engine |
| `0x2A1AA10` | `unrollCostModel` | Cost estimation helper |
| `0x2A1CF00` | `UnrollAndJamLoop` | Unroll-and-jam variant |
| `0x2A23640` | `generateRemainderLoop` | Remainder loop construction |
| `0x2A25260` | `UnrollLoopWithRuntimeChecks` | Prologue/epilogue generation |


## Pass Factory and Object Layout

> The following section documents the LoopUnroll pass factory at `sub_19B73C0`, which was originally misidentified as LICM in the P2C.3 sweep due to binary adjacency with the actual LICM pass. The vtable at `unk_4FB224C`, the 7-parameter constructor signature, and diagnostic function strings all confirm LoopUnroll identity.

The pass factory at `sub_19B73C0` allocates a 184-byte pass object and accepts seven parameters that control unroll behavior. When a parameter is -1, the pass uses its compiled-in default.

### Constructor Parameters

| Parameter | Offset | Enable Flag | Semantics |
|-----------|--------|-------------|-----------|
| `a1` (optimization level) | +156 | -- | 2 = standard, 3 = aggressive |
| `a2` (unroll threshold) | +168 | +172 | Trip count threshold; -1 = use default |
| `a3` (unroll count) | +160 | +164 | Explicit unroll factor; -1 = use default |
| `a4` (allow partial) | +176 | +177 | 0 = disable partial unroll, 1 = enable |
| `a5` (runtime unroll) | +178 | +179 | 0 = disable runtime unroll, 1 = enable |
| `a6` (upper bound) | +180 | +181 | 0 = disable upper-bound unroll, 1 = enable |
| `a7` (profile-based) | +182 | +183 | 0 = disable profile-guided unroll, 1 = enable |

### Object Construction

The factory allocates 184 bytes via `sub_22077B0`, sets the vtable to `off_49F45F0` (loop-unroll pass vtable), stores pass ID `unk_4FB224C` at offset +16, initializes self-referential linked-list pointers at offsets +80/+88 and +128/+136, sets pass type 2 (FunctionPass) at offset +24, and calls `sub_163A1D0` / `sub_19B71A0` for pass registration.


## Pipeline Invocation Configurations

CICC invokes LoopUnroll with six distinct configurations at different pipeline stages, reflecting NVIDIA's careful tuning of unroll aggressiveness per compilation phase. These are the factory-level parameter sets passed to `sub_19B73C0`; see also the decision engine's per-invocation behavior in [The Decision Engine](#the-decision-engine-computeunrollcount) above.

### Configuration A: Standard Pipeline (O1/O2)

Call site: `sub_12DE330`

```
LoopUnroll(2, -1, -1, -1, -1, -1, -1)
```

All parameters at defaults. Standard unrolling with default thresholds at optimization level 2.

### Configuration B: Code-Size Mode

Call site: `sub_12DE8F0`, when `*(a3+4480) < 0` (NVIDIA code-size flag set)

```
LoopUnroll(a2, -1, -1, 0, 0, 0, 0)
```

All unrolling features disabled: partial, runtime, upper-bound, and profile-based are all zeroed. The pass only unrolls when the trip count is statically known and the benefit is certain. This reflects the constraint that GPU register pressure makes speculative unrolling expensive when code size matters.

### Configuration C: Normal Optimizer

Call site: `sub_12DE8F0`, when `*(a3+4480) >= 0` (normal mode)

```
LoopUnroll(a2, -1, -1, -1, -1, -1, -1)
```

Fully aggressive unrolling with all defaults. The optimization level is passed through from the caller.

### Configuration D: Late Pipeline (Conservative)

Call site: `sub_12DE8F0`, late pipeline position

```
LoopUnroll(a2, -1, -1, 0, 0, -1, -1)
```

Partial and runtime unrolling disabled, but upper-bound and profile-based unrolling retain their defaults. This conservative late-pipeline configuration avoids creating new runtime overhead in code that has already been substantially optimized.

### Configuration E: Aggressive Pipeline (O3)

Call site: `sub_12E54A0`

```
LoopUnroll(3, -1, -1, 0, 0, -1, 0)
```

Optimization level 3 with aggressive thresholds, but partial, runtime, and profile-based unrolling are disabled. Only upper-bound unrolling retains its default. The rationale is that at O3, the higher thresholds already capture most profitable unrolling opportunities without needing speculative runtime checks.

### Configuration F: User-Configured

Call site: `sub_12EA3A0`

```
LoopUnroll(a1[4], a1[5], a1[6], a1[7], a1[8], a1[9], a1[10])
```

All seven parameters are read from a stored configuration object, enabling user-specified unroll behavior via command-line flags or pragmas.


## Threshold Initialization (Pass-Level)

The function `sub_19B6690` (17 KB) configures unroll thresholds based on optimization level and LLVM knobs at pass construction time. These values feed into the `UnrollParams` struct consumed by the decision engine.

### Default Threshold Values

| Offset | Field | Default (O2+) | Default (O1) |
|--------|-------|---------------|--------------|
| +0 | OptThreshold | 405 | 150 |
| +4 | Threshold | 400 | 400 |
| +12 | SmallTripCountThreshold | 150 | 150 |
| +56 | MaxIterationsCountToAnalyze | 60 | 60 |

### Function-Attribute-Aware Override

The threshold initializer queries function attributes via `sub_1560180`:

- **Attribute ID 34** (`minsize`): Reduces `OptThreshold` to `SmallTripCountThreshold` (150).
- **Attribute ID 17** (`optsize`): Same reduction.

This means kernels annotated with size constraints get conservative unroll thresholds regardless of the global optimization level.

### Per-Function Knob Override via BST

The function queries the LLVM option registry (`dword_4FA0208` BST) ten times, each time looking up a different knob address. For each knob, it searches the BST rooted at `dword_4FA0208[2]`, compares the current function hash (`sub_16D5D50`) against node ranges, and applies the override if the knob value meets the threshold. The knob-to-field mapping:

| Knob Address | Override Address | Field |
|---|---|---|
| `dword_4FB3228` | `dword_4FB32C0` | OptThreshold (+0) |
| `dword_4FB3148` | `dword_4FB31E0` | SmallTripCountThreshold (+12) |
| `dword_4FB3068` | `dword_4FB3100` | Threshold (+4) |
| `dword_4FB2DC8` | `dword_4FB2E60` | field +32 |
| `dword_4FB2CE8` | `dword_4FB2D80` | field +36 |
| `dword_4FB2C08` | `dword_4FB2CA0` | field +24 |
| `dword_4FB2B28` | (next value) | field +40 |

The per-function BST lookup keyed by function hash enables fine-grained tuning of unroll behavior per kernel, a capability not present in upstream LLVM.


## Diagnostic Functions

Three diagnostic emission functions produce optimization remarks:

| Function | Address | Diagnostic |
|----------|---------|-----------|
| `emitPragmaCountDiag` | `sub_19B78B0` | Reports when pragma unroll count conflicts with trip multiple |
| `emitThresholdDiag` | `sub_19B7B10` | Reports when unrolled size exceeds threshold |
| `emitLoopSizeDiag` | `sub_19B7D80` | Reports when loop body is too large to unroll |


## Main Loop Processing and Hash Infrastructure

The primary analysis function `sub_19B7FA0` (11 KB) analyzes each candidate loop. The pass uses hash table infrastructure shared with other CICC LLVM passes:

| Function | Address | Size | Role |
|----------|---------|------|------|
| `rehashSmallTable` | `sub_19B60B0` | 5 KB | Small hash table resize |
| `rehashTable` | `sub_19B8820` | 4 KB | Key-value hash table resize |
| `rehashSet` | `sub_19B89E0` | 7 KB | Set hash table resize |
| `insertIntoSet` | `sub_19B8DA0` | -- | Set insert with growth |

All hash tables use the same `(value >> 9) ^ (value >> 4)` hash function and linear probing strategy found throughout CICC's LLVM passes. See [Hash Infrastructure](../infra/hash-infrastructure.md) for the common implementation.


## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Pragma threshold** | `UnrollThreshold` default 150; pragma multiplier ~8x | Pragma threshold 200x larger than stock (`PragmaUnrollThreshold` = 30000); enables aggressive pragma-directed unrolling for GPU kernels |
| **Power-of-two enforcement** | No power-of-two requirement; any profitable factor accepted | Enforces power-of-two unroll factors; non-power-of-two factors are rounded down to avoid irregular loop tails |
| **Local array multiplier** | No concept of local array bonus | Dedicated local-array threshold multiplier boosts unroll budget when loop body accesses `alloca`/`.local` arrays indexed by IV, enabling register promotion |
| **Decision engine** | ~20 KB `computeUnrollCount` | Substantially reworked 50 KB `computeUnrollCount` (`sub_19BB5C0`) with 6-level priority cascade and GPU-specific occupancy heuristics |
| **Register pressure model** | Generic TTI-based unroll cost; no occupancy concept | Occupancy-aware cost model considers register pressure cliffs where one additional register per thread drops warp occupancy |
| **Pipeline invocations** | Single invocation in optimization pipeline | Two invocations: early (interleaved with vectorization) and late (cleanup, gated by `opts[1360]` / `nv-disable-loop-unrolling`) |
| **Transformation engine** | Stock `llvm::UnrollLoop` | Lightly modified `UnrollLoop` (`sub_2A15A20`, 85 KB); decision engine is where the changes concentrate |

## Cross-References

- [Loop Optimization Passes](loop-passes.md) -- pipeline context and pass ordering
- [LICM](licm-real.md) -- runs before second unroll invocation, feeds hoisted invariants
- [Loop Strength Reduction](lsr.md) -- runs after unrolling, reduces IV expressions
- [Register Allocation](register-allocation.md) -- occupancy-driven allocation consumes what unrolling produces
- [StructurizeCFG](structurizecfg.md) -- runs after all loop transforms, restructures divergent control flow
- [InstCombine](instcombine.md) -- simplifies unrolled loop bodies between invocations
