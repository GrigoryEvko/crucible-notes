# Loop Unrolling

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

On a CPU, the primary benefit of unrolling is reducing branch overhead and enabling wider SIMD scheduling. On a GPU, the calculus is different in three ways.

First, GPUs have massive register files partitioned across warps. An SM on Blackwell (sm\_100) provides 65536 32-bit registers shared among all resident warps. If a kernel uses 64 registers per thread, the SM can host `65536 / (64 * 32) = 32` warps. If unrolling increases register usage to 96, the maximum drops to `65536 / (96 * 32) = 21` warps -- a 34% occupancy reduction. The hardware scheduler relies on warp-level multithreading to hide memory latency (typically 200-400 cycles for global memory), so fewer warps means more stalls. This is why cicc's unroll factor selection must be conservative in ways that a CPU unroller never needs to be.

Second, GPU instruction scheduling is fundamentally different. There is no out-of-order execution within a warp; the hardware issues instructions in program order. Unrolling creates independent instructions that the compiler (ptxas) can interleave, particularly independent loads that can overlap with arithmetic. This is the ILP benefit, and it is the primary argument *for* aggressive unrolling.

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


## Cross-References

- [Loop Optimization Passes](loop-passes.md) -- pipeline context and pass ordering
- [LICM](licm.md) -- runs before second unroll invocation, feeds hoisted invariants
- [Loop Strength Reduction](lsr.md) -- runs after unrolling, reduces IV expressions
- [Register Allocation](register-allocation.md) -- occupancy-driven allocation consumes what unrolling produces
- [StructurizeCFG](structurizecfg.md) -- runs after all loop transforms, restructures divergent control flow
- [InstCombine](instcombine.md) -- simplifies unrolled loop bodies between invocations
