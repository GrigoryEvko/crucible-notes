# Loop Optimization Passes

Loop optimization is the single most performance-sensitive area of the cicc pipeline. On a CPU, loop passes optimize for instruction count, branch prediction, and cache locality. On an NVIDIA GPU, the constraints are fundamentally different: **register pressure dominates** (every additional register per thread reduces SM occupancy), **memory coalescing replaces cache locality** as the primary memory optimization target, and **warp divergence** caused by loop-carried control flow destroys SIMT efficiency. NVIDIA's cicc v13.0 addresses these constraints by shipping a mix of stock LLVM loop passes, LLVM passes with GPU-specific threshold overrides, and fully proprietary loop transformations -- all orchestrated through a carefully ordered pipeline where the position of each pass reflects hard-won engineering tradeoffs between register pressure, instruction count, and memory access patterns.

This page provides the big-picture view of loop optimization in cicc: what passes exist, how they are ordered, what analyses they share, and why the ordering matters for GPU targets. Each pass links to a dedicated sub-page with full algorithmic detail.

## Why Loop Optimization Is Different on GPU

Four properties distinguish GPU loop optimization from the CPU case that upstream LLVM targets:

**Register pressure is the primary constraint.** Each SM has a fixed register file (65,536 x 32-bit on sm_70+) partitioned across all resident threads. A kernel using 32 registers/thread allows 2,048 resident threads (64 warps); at 64 registers/thread, occupancy halves. The relationship has discrete cliffs -- adding one register can drop occupancy by a full warp group. Every loop transformation that increases live values (unrolling, vectorization, LICM hoisting) must be evaluated against this budget. CPU compilers never face this tradeoff.

**Memory coalescing replaces cache line optimization.** CPUs optimize loops for spatial locality in L1/L2 cache hierarchies. GPUs have no L1 data cache in the traditional sense (sm_70+ has a unified L1/shared memory, but it serves a different role). Instead, the memory subsystem coalesces warp-wide accesses: when 32 threads access 32 consecutive addresses, the hardware merges them into one 128-byte transaction. Loop transformations that improve stride-1 access patterns (interchange, vectorization) improve coalescing; transformations that increase the number of live pointers (unrolling, distribution) may degrade it by interleaving access streams.

**No out-of-order execution.** GPU warps execute instructions in program order. The only latency-hiding mechanism is warp-level multithreading: while one warp stalls on a memory access, another warp issues instructions. Unrolling creates ILP within a single warp by exposing independent instructions that the ptxas backend can interleave, but the benefit is bounded by the register pressure cost.

**Address space semantics.** GPU memory is partitioned into address spaces (global `addrspace(1)`, shared `addrspace(3)`, local `addrspace(5)`, constant `addrspace(4)`) with different pointer widths, hardware addressing modes, and performance characteristics. Loop passes that rewrite address computations (LSR, IndVarSimplify) must respect these distinctions -- strength-reducing a 32-bit shared memory pointer into 64-bit generic form defeats the backend's ability to emit efficient `.shared::` instructions.

## Pipeline Ordering

The loop passes execute within the main optimization pipeline assembled by `sub_12E54A0`. The ordering below reflects the Tier 1/2/3 optimization path (the normal path for `-O1` and above). Passes marked with **(N)** are NVIDIA-specific or have significant NVIDIA modifications; unmarked passes are stock LLVM with at most threshold overrides.

```
LoopSimplify + LCSSA                   (canonicalization)
    |
    v
LoopRotate                             (do-while canonical form)
    |
    v
LICM (hoist)                           (move invariants out)
    |
    v
LoopIndexSplit **(N)**                 (split index-dependent branches)
    |
    v
IndVarSimplify **(N)**                 (canonicalize IVs, LFTR)
    |
    v
LoopIdiomRecognize                     (memcpy/memset/mismatch idioms)
    |
    v
LoopDistribute                         (fission for vectorization)
    |
    v
LoopVectorize **(N)**                  (widen scalar loops to v2/v4)
    |
    v
LoopUnroll **(N)**                     (replicate body, GPU-tuned)
    |
    v
LoopInterchange                        (swap nest levels for coalescing)
    |
    v
IRCE                                   (range check elimination)
    |
    v
NVLoopStrengthReduce **(N)**           (NVIDIA custom LSR solver)
    |
    v
LoopDeletion                           (remove dead loops)
    |
    v
LoopSink / LICM (sink)                 (demote unprofitable hoists)
```

Several passes appear more than once. LICM runs in both hoist and sink mode. LoopUnroll has an early invocation in the main pipeline and a late invocation gated by `opts[1360]` (`nv-disable-loop-unrolling`). IndVarSimplify runs before vectorization to canonicalize induction variables, then again after unrolling to clean up newly exposed IVs. LoopSimplify and LCSSA are implicit -- they run as required analyses whenever any loop pass requests them, ensuring loops remain in canonical form throughout.

The ordering reflects a deliberate strategy: **canonicalize first** (LoopSimplify, LoopRotate, IndVarSimplify), **transform for parallelism** (LoopDistribute, LoopVectorize, LoopInterchange), **replicate for ILP** (LoopUnroll), and **clean up addressing** (LSR, LoopDeletion, LoopSink). Reordering these passes produces measurably different code: running LSR before LoopVectorize would pollute the cost model with strength-reduced IVs that confuse SCEV; running LoopUnroll before LoopVectorize would prevent vectorization of unrolled-but-still-vectorizable loops.

## LoopPassManager Structure

cicc uses the LLVM New Pass Manager's `LoopPassManager` infrastructure. Loop passes are grouped inside a `FunctionPassManager` that contains a `LoopToFunctionPassAdaptor` wrapping the `LoopPassManager`. The adaptor iterates over all loops in the function in reverse post-order of the loop forest (innermost first), running the full sequence of loop passes on each loop before moving to the next.

The `LoopStandardAnalysisResults` struct is threaded through all loop passes, providing shared access to:

| Analysis | Typical Accessor | Purpose |
|----------|-----------------|---------|
| `ScalarEvolution` | `AR.SE` | Trip counts, strides, value ranges |
| `LoopInfo` | `AR.LI` | Loop structure, nesting depth |
| `DominatorTree` | `AR.DT` | Dominance queries for code motion |
| `AssumptionCache` | `AR.AC` | `__builtin_assume` facts |
| `TargetTransformInfo` | `AR.TTI` | Cost model, addressing modes |
| `MemorySSA` | `AR.MSSA` | Memory alias queries for LICM/DSE |
| `AAResults` | `AR.AA` | Alias analysis chain |

Passes that structurally modify loops (LoopUnroll, LoopDistribute, IRCE) call `LPMUpdater::markLoopAsDeleted()` or `LPMUpdater::addSiblingLoops()` to inform the pass manager of changes. SCEV is invalidated per-loop via `SE.forgetLoop()` after any transformation that changes the loop's backedge-taken count.

## Complete Pass Inventory

The table below lists every loop pass present in cicc v13.0 with its pipeline position, NVIDIA modification status, and primary function address.

| Pass Name | Pipeline Position | NVIDIA Modified | Entry Address | Status |
|-----------|------------------|----------------|---------------|--------|
| `loop-simplify` | Infrastructure (on demand) | No | stock LLVM | Canonicalizes loop form |
| `lcssa` | Infrastructure (on demand) | No | stock LLVM | Ensures loop-closed SSA |
| `loop-rotate` | Early, before LICM | No | stock LLVM | Converts to do-while form |
| `licm` | Early (hoist) + Late (sink) | Threshold only | stock LLVM | Invariant code motion |
| `loop-index-split` | After LICM, before IndVars | **Yes (proprietary)** | `sub_2CBEC60` (New PM) | Splits index-dependent branches |
| `indvars` | Before vectorize | **Yes (3 knobs)** | `sub_19489B0` | IV canonicalization + LFTR |
| `loop-idiom` | Before distribute | No | stock LLVM | Memcpy/memset/mismatch recognition |
| `loop-distribute` | Before vectorize | Threshold only | `sub_1A8CD80` | Loop fission for vectorization |
| `loop-vectorize` | Main loop slot | **Yes (cost model)** | `sub_2AF1970` | Vectorize inner loops to v2/v4 |
| `loop-unroll` | After vectorize (x2) | **Yes (decision engine)** | `sub_19BE360` | Replicate loop body |
| `loop-interchange` | After unroll | Threshold only | `sub_1979A90` | Swap loop nest levels |
| `irce` | After interchange | No | `sub_194D450` | Range check elimination |
| `loop-reduce` | Late, after unroll | **Yes (complete rewrite)** | `sub_19CE990` (NV wrapper) | Strength reduction for GPU |
| `loop-deletion` | Late | No | stock LLVM | Remove dead/empty loops |
| `loop-sink` | Late | No | stock LLVM | Sink invariants back into loops |
| `loop-instsimplify` | Utility | No | stock LLVM | Simplify instructions in loops |
| `loop-flatten` | Utility | No | stock LLVM | Flatten nested counted loops |
| `loop-guard-widening` | Utility | No | stock LLVM | Widen loop guards |
| `loop-predication` | Utility | No | stock LLVM | Predicate unswitched loops |
| `loop-reroll` | Utility | No | stock LLVM | Reverse unrolling (rarely used) |

Passes marked "Utility" are registered in the pipeline infrastructure but are not part of the default optimization sequence -- they are available for explicit pipeline specification via `-mllvm -passes=...`.

## Pass Descriptions and Sub-Page Links

### Canonicalization Passes

**LoopSimplify** and **LCSSA** run on demand before any loop transformation pass executes. LoopSimplify ensures each loop has a single preheader, a single backedge (latch), and dedicated exit blocks. LCSSA (Loop-Closed SSA) ensures that values defined inside a loop and used outside it pass through PHI nodes at loop exit blocks. These are stock LLVM utilities with no NVIDIA modifications. Together they establish the invariants that all subsequent loop passes depend on.

**LoopRotate** converts a loop from while-form (`while (cond) { body }`) to do-while form (`do { body } while (cond)`). This creates a single-entry loop body and moves the exit test to the latch, which is the canonical form expected by SCEV, LoopVectorize, and LoopUnroll. Stock LLVM, no NVIDIA modifications.

### NVIDIA-Custom Loop Passes

**[Loop Index Split](../passes/loop-index-split.md)** is a revived and heavily reworked version of a pass removed from upstream LLVM 3.0. It splits loops when the loop body contains a condition that depends on the induction variable (e.g., `if (i == K)`), producing two or three loops where each has a uniform body. On GPU, this eliminates warp divergence caused by index-dependent branches. The pass implements three transformation modes: all-but-one peel (for `i == K`), only-one collapse (for nearly-empty special iterations), and full range split (for `i < K` vs `i >= K`). Proprietary, no upstream equivalent.

**[IndVarSimplify (NVIDIA)](./loop-passes-standard.md#indvarsimplify)** is upstream LLVM's induction variable canonicalization pass with three NVIDIA-specific extensions: `Disable-unknown-trip-iv` (bool, `qword_4FAF520`) -- bypasses the pass entirely when SCEV cannot compute the trip count, preventing aggressive IV transforms on warp-divergent loops; `iv-loop-level` (int, default 1, `qword_4FAF440`) -- restricts the pass to loops at a maximum nesting depth to control compile time on deeply nested stencil kernels; and `disable-lftr` (bool, `byte_4FAF6A0`) -- disables Linear Function Test Replace when the IV canonicalization would increase register pressure.

**[LoopVectorize (GPU-Adapted)](./loop-vectorize.md)** is the largest single pass in the cicc loop pipeline (88 KB). On GPU, vectorization means generating `ld.v2`/`ld.v4` wide loads rather than filling SIMD lanes. The pass builds VPlans, selects VF through a GPU-aware cost model that penalizes register pressure, and caps VF at 4 for most GPU targets. Scalable vectors are always disabled. The pass includes an outer-loop vectorization path (rarely triggered on GPU) and an inner-loop path (the main code path).

**[Loop Unrolling (GPU-Tuned)](./loop-unroll.md)** ships a substantially reworked `computeUnrollCount` decision engine with GPU heuristics: a local-array threshold multiplier that aggressively unrolls loops over `__shared__` arrays, power-of-two factor enforcement, a pragma threshold 200x larger than stock LLVM, and a register-pressure-aware cost model. The transformation engine is lightly modified upstream `UnrollLoop`. The pass runs twice: once in the main pipeline, once as a late cleanup.

**[NVLoopStrengthReduce (NVIDIA Custom)](./lsr.md)** is the most GPU-specific LLVM pass in cicc. NVIDIA ships a complete replacement formula solver (160 KB, 2688 lines) with 11 custom knobs controlling register pressure checking, address-space-aware formula selection, sign-extension optimization, and 64-bit IV handling. The stock LLVM LSR remains in the binary but the NVIDIA overlay replaces the formula generation and selection phases.

### Standard Loop Passes (Threshold Overrides Only)

**[LICM](./licm-real.md)** (Loop-Invariant Code Motion) hoists loop-invariant computations above the loop and sinks them below it. On GPU, LICM's hoist mode must be conservative: hoisting increases register pressure in the loop preheader, which may push past occupancy cliffs. The sink mode (running later) undoes unprofitable hoists. Stock LLVM with NVIDIA-tuned thresholds.

**LoopInterchange** swaps the nesting order of a perfectly-nested loop pair when doing so improves memory access locality. In cicc, the threshold `loop-interchange-threshold` (`dword_4FB07E0`) defaults to 0, meaning interchange is only performed when the net locality benefit is non-negative AND parallelism improves. The pass has a 100-pair dependence limit (`0x960 bytes`) as a compile-time safety valve. There is no visible CUDA-specific memory space awareness -- the standard LLVM stride-1 locality model applies uniformly. See the [standard loop passes page](./loop-passes-standard.md#loopinterchange) for details.

**IRCE** (Inductive Range Check Elimination) splits a loop into preloop/mainloop/postloop regions, eliminating range checks from the mainloop where the induction variable is provably within bounds. The implementation is stock LLVM with no visible NVIDIA modifications. Configuration globals include a block count threshold (`dword_4FB0000`), a debug flag (`byte_4FAFE40`), and a "constrained" relaxation mode (`byte_4FAFBA0`) that handles slightly non-canonical range checks common in GPU thread-coarsened loops.

**LoopDistribute** (loop fission) splits a single loop into multiple loops to separate unsafe memory dependences from safe ones, enabling LoopVectorize to vectorize the safe partition. Stock LLVM algorithm. The SCEV runtime check threshold (`qword_4FB5480`) is likely GPU-tuned. The pass runs before LoopVectorize in the pipeline.

**LoopIdiomRecognize** detects loops that implement common patterns (byte-by-byte copy, memset, mismatch search, string search) and replaces them with optimized multi-block IR or library calls. The expansion routines generate vectorized mismatch detection (`sub_2AA00B0`, 48 KB) and vectorized first-occurrence string search (`sub_2AA3190`, 40 KB), both with page-boundary-safe masked vector loads. Stock LLVM pass; the expansion quality benefits GPU targets where wide loads are profitable.

**LoopDeletion** removes loops proven dead (no observable side effects). Stock LLVM. **LoopSink** moves loop-invariant operations that were hoisted by LICM back into the loop body when doing so reduces register pressure -- particularly valuable on GPU where the register pressure tradeoff is acute.

## Loop Analysis Infrastructure

All loop passes share three core analysis frameworks.

### ScalarEvolution (SCEV)

SCEV models how values evolve across loop iterations. Every loop pass depends on it for trip count computation, stride analysis, and value range queries. cicc ships an LLVM 7.x-era SCEV with three NVIDIA extensions: a complexity control system (`simple_mode`) that prevents unbounded analysis time, GPU-specific SCEV sources that inject thread index bounds, and recognition of CUDA loop idioms (warp-stride, grid-stride). See [ScalarEvolution Overview](./scev.md), [Range Analysis & Trip Counts](./scev-range-btc.md), and [Invalidation & Delinearization](./scev-invalidation.md).

### LoopInfo

`LoopInfo` provides the loop forest structure: which basic blocks belong to which loops, nesting depth, header/latch/exit identification. It is the primary structural query interface for all loop passes. Stock LLVM, no NVIDIA modifications.

### DependenceInfo

`DependenceInfo` computes memory dependence direction vectors between instruction pairs across loop iterations. LoopInterchange and LoopDistribute are its primary consumers. The analysis uses SCEV to classify dependences as forward (`<`), backward (`>`), equal (`=`), scalar (`S`), independent (`I`), or unknown (`*`). Direction vectors drive the legality checks for loop interchange (no reversed backward-carried dependences after swap) and loop distribution (which instructions must stay in the same partition).

## Loop-Related Knobs Summary

The following table consolidates all loop-pass-specific configuration knobs discovered in cicc v13.0. These are controllable via `-mllvm -<knob>=<value>`.

| Knob | Pass | Type | Default | Effect |
|------|------|------|---------|--------|
| `Disable-unknown-trip-iv` | IndVarSimplify | bool | false | Skip IV canonicalization for unknown-trip loops |
| `iv-loop-level` | IndVarSimplify | int | 1 | Max nesting depth for IV simplification |
| `disable-lftr` | IndVarSimplify | bool | false | Disable Linear Function Test Replace |
| `replexitval` | IndVarSimplify | enum | 1 (cheap) | Exit value replacement strategy: 0=never, 1=cheap, 2=always |
| `indvars-widen-indvars` | IndVarSimplify | bool | true | Allow IV widening to eliminate sign/zero extension |
| `loop-interchange-threshold` | LoopInterchange | int | 0 | Minimum net locality improvement for interchange |
| `vectorize-loops` | LoopVectorize | bool | true | Master vectorization enable |
| `enable-early-exit-vectorization` | LoopVectorize | bool | false | Allow vectorization of early-exit loops |
| `force-vector-width-outer` | LoopVectorize | bool | false | Force VF=4 for outer loops |
| `nv-disable-loop-unrolling` | LoopUnroll | bool | false | Disable the late unroll invocation |
| `disable-unknown-trip-lsr` | NV LSR | bool | false | Skip LSR for unknown-trip loops |
| `lsr-check-rp` | NV LSR | bool | true | Enable register pressure checking in LSR |
| `lsr-rp-limit` | NV LSR | int | ~32-64 | Register pressure ceiling for LSR |
| `filter-bad-formula` | NV LSR | bool | true | NVIDIA custom formula filtering |
| `do-lsr-64-bit` | NV LSR | bool | arch-dep | Enable LSR for 64-bit IVs (false on sm_3x-5x) |
| `count-sxt-opt-for-reg-pressure` | NV LSR | bool | true | Credit sign-ext savings in cost model |
| `lsr-sxtopt` | NV LSR | bool | true | Fold sign-extensions into IV expressions |
| `lsr-loop-level` | NV LSR | int | 0 (all) | Restrict LSR to specific loop nesting depth |
| `lsr-skip-outer-loop` | NV LSR | bool | false | Skip outer loop IVs in nested loops |
| `disable-lsr-for-sharedmem32-ptr` | NV LSR | bool | false | Disable LSR for `addrspace(3)` pointers |
| `disable-lsr-complexity-discount` | NV LSR | bool | false | Disable complexity discount in cost model |
| `irce-block-threshold` | IRCE | int | varies | Max basic blocks before IRCE bails |
| `enable-loop-distribute` | LoopDistribute | bool | false | Force-enable distribution |
| `loop-distribute-scev-check-threshold` | LoopDistribute | int | varies | Max SCEV runtime checks allowed |

## Cross-References

- **Pipeline context:** [LLVM Optimizer](../pipeline/optimizer.md) -- two-phase compilation, tier dispatch, NVVMPassOptions
- **Pipeline ordering:** [Pipeline & Pass Ordering](./pipeline.md) -- complete pass registration table
- **Vectorization:** [LoopVectorize & VPlan](./loop-vectorize.md) -- GPU-adapted vectorizer with full cost model
- **Unrolling:** [Loop Unrolling](./loop-unroll.md) -- decision cascade with GPU-specific heuristics
- **Strength reduction:** [Loop Strength Reduction (NVIDIA)](./lsr.md) -- the most GPU-specific pass in cicc
- **NVIDIA custom passes:** [Loop Index Split](../passes/loop-index-split.md), [NVVM Peephole](../passes/nvvm-peephole.md)
- **SCEV infrastructure:** [ScalarEvolution Overview](./scev.md), [Range Analysis & Trip Counts](./scev-range-btc.md), [SCEV Invalidation](./scev-invalidation.md)
- **Standard loop passes:** [Standard Loop Passes](./loop-passes-standard.md) -- IndVarSimplify, LoopInterchange, IRCE, LoopDistribute, LoopIdiomRecognize details
