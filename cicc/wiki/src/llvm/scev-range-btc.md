# SCEV Range Analysis & Backedge-Taken Counts

Every loop optimization in cicc ultimately depends on two questions: "what values can this expression take?" and "how many times does this loop iterate?" The SCEV range analysis (`sub_DBB9F0`, corresponding to `ScalarEvolution::getRangeRef`) answers the first by propagating `ConstantRange` intervals through SCEV expression trees. The backedge-taken count (BTC) machinery (`sub_DB9E00` / `sub_DB9040`, corresponding to `computeBackedgeTakenCount` / `computeExitCountForBranch`) answers the second by solving loop exit conditions algebraically. The two systems feed each other: range analysis uses trip counts to bound AddRec expressions, and trip count computation uses ranges to prove overflow behavior. On GPU targets, these analyses gain additional precision from NVIDIA-specific range sources -- thread indices are bounded by block dimensions, `warpSize` is the constant 32, and `__launch_bounds__` metadata constrains block dimensions -- all of which flow into tighter ranges and more computable trip counts.

## Key Facts

| Property | Value |
|---|---|
| Range evaluator | `sub_DBB9F0` (0xDBB9F0), 31 KB |
| BTC dispatcher | `sub_DCF3A0` (0xDCF3A0), mode 0=exact, 1=constant-max, 2=symbolic-max |
| BTC cache builder | `sub_DB9E00` (0xDB9E00), 2,265 bytes |
| Exit count engine | `sub_DB9040` (0xDB9040), 18 KB |
| howFarToZero | `sub_DBA850` (0xDBA850), 8 KB |
| howManyLessThans | `sub_DCE310` (0xDCE310), 317 lines |
| Range cache (unsigned) | `scev_ctx+976`, 40-byte entries, open-addressing |
| Range cache (signed) | `scev_ctx+1008`, 40-byte entries, open-addressing |
| BTC cache | `scev_ctx+656`, 168-byte entries, open-addressing |
| Per-exit BTC cache | `scev_ctx+1168`, 56-byte entries |
| Max range recursion depth | `qword_4F88EA8` (global, configurable) |
| Extended exit analysis flag | `qword_4F88C08` (global, enables Phase D) |
| NVIDIA knobs | `track-trip-count-more`, `aggressive-positive-stride-analysis`, `do-sign-ext-simplify`, `do-sign-ext-expand` |

## ConstantRange Propagation Algorithm

The range evaluator `sub_DBB9F0` takes a SCEV expression, a signedness flag (`is_signed`: 0=unsigned, 1=signed), and a recursion depth counter. It returns a pointer to a cached 32-byte `ConstantRange` representing the half-open interval `[lower, upper)` with wrapping semantics. The algorithm is a recursive descent over the SCEV expression tree with aggressive caching.

### Cache Structure

Two separate hash tables store signed and unsigned ranges:

```
if (is_signed) {
    table    = scev_ctx[+1008];   // signed range cache
    capacity = scev_ctx[+1024];
} else {
    table    = scev_ctx[+976];    // unsigned range cache
    capacity = scev_ctx[+992];
}
```

Each entry is 40 bytes: an 8-byte key (SCEV pointer, with `0xFFFFFFFFFFFFF000` as the empty sentinel) followed by a 32-byte `ConstantRange` value. The hash function is:

```c
slot = ((uint32_t)scev_ptr >> 9) ^ ((uint32_t)scev_ptr >> 4);
slot &= (capacity - 1);  // capacity is always a power of two
```

Linear probing resolves collisions. On a cache hit, the function returns immediately without recomputation.

### Dispatch by SCEV Kind

After a cache miss, the evaluator dispatches on the SCEV opcode at `scev_expr+24` (uint16):

| Opcode | Kind | Range Computation |
|--------|------|-------------------|
| 0 | SCEVConstant | Single-value range from the constant's APInt |
| 1 | SCEVUnknown | `sub_988CD0`: range from ValueTracking / instruction semantics |
| 2 | SCEVTruncate | Recurse on operand, apply `ConstantRange::truncate` |
| 3 | SCEVZeroExtend | Recurse on operand, apply `ConstantRange::zeroExtend` |
| 4 | SCEVSignExtend | Recurse on operand, apply `ConstantRange::signExtend` |
| 5 | SCEVAddExpr | Fold operand ranges with `addWithNoWrap`, respecting NUW/NSW |
| 6 | SCEVMulExpr | Fold operand ranges with `ConstantRange::multiply` |
| 7 | SCEVUDivExpr | `ConstantRange::udiv` of LHS and RHS ranges |
| 8 | SCEVAddRecExpr | Multi-phase analysis (see below) |
| 9-13 | SMax/UMax/SMin/UMin | Fold via lookup table `dword_3F74E60[opcode-9]` + `sub_ABD750` |
| 14 | SCEVCouldNotCompute | Passthrough (identity range) |
| 15 | SCEVSequentialUMin | Complex instruction-level analysis (PHI, intrinsics, metadata) |

Every computed range is intersected with an initial range derived from the type's bit width and any known-bits / sign-bits information before being stored in the cache. This intersection can only narrow the range, never widen it.

### Initial Range Narrowing

Before the SCEV-kind dispatch, the evaluator computes an initial range from type information:

- **Unsigned mode:** calls `sub_DB5510` (getKnownBits) to extract known high zero bits, constructs a range `[0, 2^(bitwidth - leading_zeros))` and intersects it with the full-set range.
- **Signed mode:** calls `sub_DB55F0` (getNumSignBits) and constructs a symmetric signed range from the sign-bit count, e.g., if 3 sign bits are known, the range is `[-2^(bw-3), 2^(bw-3))`.

This pre-narrowing ensures that even when the SCEV-kind dispatch returns a full-set (e.g., for complex expressions at the depth limit), the result still reflects type-level constraints.

### AddRec Range Analysis (The Core)

The `SCEVAddRecExpr` case (opcode 8) is the most complex, executing up to five phases that progressively narrow the range of a loop induction variable `{start, +, step}`:

**Phase A -- NoWrap Start Refinement.** If the AddRec has NUW or NSW flags (bits at `scev_expr+28`), the unsigned range of the start value is computed and intersected. This ensures that the IV's initial value constrains the overall range even before considering the step.

**Phase B -- Step Monotonicity.** If the NSW flag (bit 2, value 0x4) is set:
- `sub_DBED40` checks if all step operands are non-negative (monotone up). If so, the signed minimum of start becomes the lower bound: range `[smin(start), SMAX]`.
- `sub_DBEC80` checks if all steps are non-positive (monotone down). If so, the signed maximum of start becomes the upper bound: range `[SMIN, smax(start)+1]`.

**Phase C -- Trip Count Refinement.** For simple two-operand recurrences (`{start, +, step}` with operand count == 2):
1. Call `sub_DCF3A0(ctx, loop, 1)` to get the max backedge-taken count.
2. If the trip count is computable, compute `range(start + step * [0, trip_count])` for both unsigned (`sub_DBEFC0`) and signed (`sub_DBF480`) domains.
3. Intersect both results into the accumulated range.

This is where range analysis and BTC computation form their feedback loop: the BTC is used to bound the AddRec's range.

**Phase D -- Exit Value Analysis (NVIDIA-gated).** Enabled only when global `qword_4F88C08` is set. Gets the *exact* backedge-taken count (mode=2 via `sub_DCF3A0`), and if the trip count bit width fits within the AddRec's bit width and NSW is set, calls `sub_DE4FD0` to compute the exit value range. This provides the tightest possible bound but is more expensive.

**Phase E -- Cache and Return.** The final accumulated range (from all intersections) is stored in the cache.

### SCEVUnknown and Instruction-Level Analysis

For `SCEVUnknown` (opcode 1) and the complex instruction-level path (opcode 15), the range evaluator performs several specialized analyses:

- **!range metadata:** if the underlying instruction carries `!range` metadata (kind=4), `sub_B91C10` extracts it and `sub_ABEA30` builds a `ConstantRange` directly.
- **Predecessor merging:** `sub_DBB110` computes ranges by analyzing incoming values from predecessor basic blocks, intersecting the results.
- **PHI node analysis:** for PHI nodes (instruction opcode 84), the evaluator iterates all incoming values, computes each one's SCEV range, and unions them. A visited-PHI set at `scev_ctx+320` prevents infinite recursion through cyclic PHIs.
- **Intrinsic ranges:** `sub_988010` identifies specific intrinsics (e.g., `ctpop`, `ctlz`, `cttz`) and constrains their ranges to non-negative values via `sub_ABB6C0`.
- **Stride alignment:** `sub_BD4FF0` computes stride/alignment information for loads and stores, narrowing the range to multiples of the known stride.

### Signed/Unsigned Cross-Pollination

A critical detail: the AddRec analysis explicitly recurses with the *opposite* signedness flag in certain sub-analyses. Phase A always computes the start in unsigned mode (`is_signed=0`), while Phase B always uses signed mode (`is_signed=1`). This cross-referencing allows information from one domain to constrain the other, producing tighter bounds than either domain alone.

## GPU-Specific Range Sources

Three categories of NVIDIA-specific range information feed into SCEV range analysis, all derived from the CUDA execution model:

### Thread and Block Index Ranges

The intrinsics `@llvm.nvvm.read.ptx.sreg.tid.{x,y,z}` (threadIdx) produce values in `[0, blockDim-1]`. The intrinsics `@llvm.nvvm.read.ptx.sreg.ctaid.{x,y,z}` (blockIdx) produce values in `[0, gridDim-1]`. When these intrinsics appear as `SCEVUnknown` nodes, the range evaluator propagates their constrained ranges through the expression tree.

The block dimension intrinsics `@llvm.nvvm.read.ptx.sreg.ntid.{x,y,z}` are bounded by `__launch_bounds__` metadata when present. Specifically, `nvvm.maxntid` (from `__launch_bounds__(maxThreads)`) provides an upper bound on `ntid.x * ntid.y * ntid.z`, and `nvvm.reqntid` provides an exact value. These bounds are read by `sub_CE8D40` (NvvmMeta_getMaxNTID) and `sub_CE8DF0` (NvvmMeta_getReqNTID).

`warpSize` (`@llvm.nvvm.read.ptx.sreg.warpsize`) is the constant 32 on all architectures from sm_70 onward, producing the singleton range `[32, 33)`.

### Grid-Stride Loop Patterns

SCEV delinearization (`sub_DE9D10`) specifically recognizes the grid-stride pattern:

```
// CUDA:  for (int i = tid + bid * bdim; i < N; i += bdim * gdim)
// SCEV:  {threadIdx.x + blockIdx.x * blockDim.x, +, blockDim.x * gridDim.x}
```

The step `blockDim.x * gridDim.x` inherits known-positive range from both operands, enabling the monotonicity analysis in Phase B to prove the IV is non-decreasing. Combined with the bounded start value (`tid.x + bid.x * bdim.x` is non-negative), the range of the entire AddRec is `[0, N)` rather than full-set.

### KnownBits and DemandedBits Integration

The `sub_99B5E0` post-analysis in SimplifyDemandedBits applies NVIDIA-specific refinements including thread index range constraints (`threadIdx.x < blockDim.x`) and warp-level uniformity assumptions. These propagate through SCEV's `getKnownBits` (`sub_DB5510`) to tighten the initial unsigned range of expressions involving GPU special registers.

## Backedge-Taken Count Computation

The BTC machinery computes how many times a loop's backedge executes before any exit is taken. The result has three variants:

- **Exact count:** the precise number of iterations, or `SCEVCouldNotCompute` if unknown.
- **Constant max:** a constant upper bound on the iteration count.
- **Symbolic max:** a SCEV expression bounding the iteration count (may involve loop-invariant values).

### BTC Cache Layout

The primary BTC cache at `scev_ctx+656` uses 168-byte entries:

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | Key: SCEV pointer (sentinels: empty=`-4096`, tombstone=`-8192`) |
| +0x08 | 128 | Per-exit count data (SmallVector of `{BasicBlock*, SCEV* count, flags}`) |
| +0x88 | 8 | Exact backedge-taken count (SCEV pointer or null) |
| +0x90 | 1 | Flag: exact count is valid |
| +0x98 | 8 | Max backedge-taken count (SCEV pointer or null) |
| +0xA0 | 1 | Flag: max count is valid |

The hash function is identical to the range cache: `((key >> 9) ^ (key >> 4)) & (capacity - 1)`. Load factor threshold is 75% for capacity doubling (via `sub_DB6980`) and 87.5% (only capacity/8 truly empty slots remaining) for in-place rehash to reclaim tombstones.

A secondary per-exit table at `scev_ctx+1168` stores 56-byte entries indexing individual exit block trip counts, avoiding linear scans through the main entry's embedded exit array.

### Exit Count Computation Pipeline

`sub_DB9040` (`computeExitCountForBranch`) is the heavy lifter. For each exiting block, it:

1. Extracts the branch condition's ICmp instruction.
2. Identifies the comparison operands as SCEV expressions.
3. Classifies the exit condition into one of the standard shapes.
4. Dispatches to the appropriate solver.

The two primary solvers are:

**howFarToZero** (`sub_DBA850`, 8 KB) -- handles `x != 0` exit conditions. The exit condition is normalized to `V = LHS - RHS`, so the loop exits when `V == 0`. For affine AddRec `{Start, +, Step}`:

```
// The loop exits when: Start + Step * N = 0 (mod 2^BW)
// Solving: N = -Start / Step (mod 2^BW)
// For positive step (counting up to overflow): N = -Start / Step
// For negative step (counting down to zero):  N = Start / (-Step)
```

For quadratic AddRec `{L, +, M, +, N}`, it solves the quadratic equation via `SolveQuadraticAddRecExact`. If the expression is not affine or quadratic, it returns `CouldNotCompute`.

**howManyLessThans** (`sub_DCE310`, 317 lines) -- handles `x < bound` (signed or unsigned) exit conditions. For affine `IV = {Start, +, Step}` with loop-invariant `Bound`:

```
// Unsigned: count = ceil_div(max(Bound, Start) - Start, Step)
// Signed:   count = ceil_div(max_signed(Bound, Start) - Start, Step)
// With overflow checks based on NUW/NSW flags
```

This function also contains special logic for zero-extended IVs: if the comparison involves `zext(IV) < Bound`, it can infer NUW on the inner AddRec by proving that the bound is small enough that unsigned overflow cannot occur before the exit.

### Loop Shape Handling

The BTC computation handles several loop shapes through the exit condition classification:

- **Countable (for-style):** `for (i = 0; i < N; i++)` produces `{0, +, 1} < N`, solved by howManyLessThans as `N - 0 = N` iterations.
- **While-do:** the exit test precedes the body. Trip count equals the number of backedge traversals, which is one less than the number of condition evaluations.
- **Do-while:** the exit test follows the body. The backedge is taken at least once if the loop is entered. Trip count comes directly from the exit condition solver.
- **Multiple exits:** `computeBackedgeTakenCount` (`sub_DB9E00`) iterates all exiting blocks, computes per-exit counts, and takes the minimum. If any exit is not computable, the exact count is `CouldNotCompute` but the max count may still be known from the computable exits.
- **Exhaustive evaluation:** `sub_DCFD50` (`computeExitCountExhaustively`) brute-force iterates small constant-evolving loops (up to `scalar-evolution-max-iterations` = 100 iterations) to find exit counts that algebraic methods cannot handle.

### Overflow Handling and NoWrap Flags

Trip count precision depends critically on the NoWrap flags (`NUW` = bit 1, `NSW` = bit 2) stored at `scev_expr+28`:

- **NUW (No Unsigned Wrap):** if an AddRec `{Start, +, Step}` has NUW, unsigned arithmetic cannot wrap, so `Start + Step * N` is monotonically increasing in the unsigned domain. This allows howManyLessThans to compute an exact count without overflow guards.
- **NSW (No Signed Wrap):** similarly for signed arithmetic. Enables signed comparison trip counts and the Phase B monotonicity analysis in range computation.
- **Neither flag:** the solver must account for wrapping. howFarToZero solves modular arithmetic; howManyLessThans may fall back to constant-max estimates or `CouldNotCompute`.

The NVIDIA-specific knob `aggressive-positive-stride-analysis` (documented as "See nvbug 3972412") enables more aggressive inference of NUW flags on AddRec expressions with positive strides, particularly for GPU loop patterns where the step is a known-positive grid dimension.

## How BTC Feeds Loop Optimizations

### Loop Unrolling

The unroll decision engine (`sub_19BB5C0`) queries `getSmallBestKnownTC` (`sub_2AA7EC0`) which calls the BTC machinery. The result determines the unroll strategy:

- **Exact trip count known and small:** enables full unrolling -- the loop body is replicated exactly N times with no remainder loop. This is the most profitable case for GPU code since it eliminates all loop overhead.
- **Exact trip count known but large:** enables partial unrolling with an exact remainder. The unroll factor is chosen to divide the trip count, avoiding a remainder loop entirely.
- **Only max trip count known:** enables partial unrolling with a runtime remainder check. The unroll factor is bounded by the max trip count.
- **Trip count unknown:** unrolling is gated by the NVIDIA knob `Disable-unknown-trip-iv` -- when set, IndVarSimplify (`sub_19489B0`) skips loops entirely if the trip count is not computable.

### Loop Vectorization

The vectorizer (`sub_2AE3460`) uses BTC in two ways:

1. **Minimum trip count threshold:** `getSmallBestKnownTC` is compared against `dword_500EAE8` (`-vectorizer-min-trip-count`). If the known trip count is below this threshold, vectorization bails with "LowTripCount" (note the preserved typo: "The trip count is below the minial threshold value.").

2. **Divisibility for epilogue:** when the exact trip count is known, the vectorizer checks if it is divisible by the vectorization factor. If so, no scalar epilogue is needed. If not, it generates an epilogue loop. The exact trip count from SCEV enables eliminating the runtime divisibility check.

### IRCE (Inductive Range Check Elimination)

IRCE (`sub_194D450`) uses SCEV ranges to split loops into pre-loop / main-loop / post-loop regions. The BTC determines the main loop's iteration space, and the range checks within the loop body define the boundaries for the pre/post loops. Tighter SCEV ranges mean tighter pre/post loops (fewer wasted iterations), which is significant for GPU kernels where every wasted iteration occupies a warp lane.

### IndVarSimplify

IndVarSimplify (`sub_1945A50`) uses the exact BTC for Linear Function Test Replacement (LFTR): replacing the original loop exit test with a comparison against the trip count. This is gated by three NVIDIA knobs: `disable-lftr`, `Disable-unknown-trip-iv`, and `iv-loop-level` (default 1, restricting IV simplification to outermost loops only to limit compile-time on deeply nested GPU kernels).

## GPU-Specific Trip Count Patterns

### Grid-Stride Loops

```cuda
for (int i = threadIdx.x + blockIdx.x * blockDim.x;
     i < N;
     i += blockDim.x * gridDim.x)
```

SCEV representation: `{tid.x + ctaid.x * ntid.x, +, ntid.x * nctaid.x}`. The start is bounded by `[0, ntid.x * nctaid.x)` and the step is provably positive (product of two positive values). Trip count: `ceil((N - start) / step)`. With `__launch_bounds__`, the step's range can be computed precisely, enabling exact trip count computation when `N` is loop-invariant.

### Warp-Stride Loops

```cuda
for (int i = threadIdx.x % 32; i < N; i += 32)
```

SCEV representation: `{tid.x urem 32, +, 32}`. The start is `[0, 31]` (since `warpSize=32`), and the step is the constant 32. Trip count: `ceil((N - (tid.x % 32)) / 32)`. This is always computable when `N` is loop-invariant.

### Block-Bounded Loops

```cuda
for (int i = 0; i < blockDim.x; i++)
```

When `nvvm.reqntid` metadata is present, `blockDim.x` has a known constant value, and the loop has a compile-time-known trip count. This enables full unrolling -- common for shared memory initialization and reduction loops.

## Configuration Knobs

| Knob | Default | Effect |
|------|---------|--------|
| `scalar-evolution-max-iterations` | 100 | Max iterations for exhaustive BTC evaluation |
| `scalar-evolution-max-scev-compare-depth` | 32 | Recursion limit for SCEV comparison |
| `scalar-evolution-max-arith-depth` | 32 | Recursion limit for arithmetic simplification |
| `scalar-evolution-max-cast-depth` | 8 | Recursion limit for ext/trunc handling |
| `scalar-evolution-max-ext-depth` | 8 | Recursion limit for extension expressions |
| `scalar-evolution-max-constant-evolving-depth` | 32 | Depth limit for constant evolution |
| `scalar-evolution-max-expr-size` | 384 | Expression complexity budget (NVIDIA simple mode) |
| `scalar-evolution-max-expr-failures` | 100 | Max failures before all expressions bail to Unknown |
| `scev-addops-inline-threshold` | 500 | Max add operands before bailing |
| `scev-mulops-inline-threshold` | 32 | Max mul operands before bailing |
| `scev-cheap-expansion-budget` | (default) | Cost budget for SCEVExpander materialization |
| `track-trip-count-more` | false | "Track loop trip count more aggressively" (NVIDIA-specific) |
| `aggressive-positive-stride-analysis` | true | More aggressive NUW inference for positive strides (nvbug 3972412) |
| `do-sign-ext-simplify` | (default) | Simplify sign-extension SCEV expressions |
| `do-sign-ext-expand` | (default) | Expand sign-extensions during SCEV construction |
| `qword_4F88EA8` | (global) | Max recursion depth for range computation |
| `qword_4F88C08` | (global) | Enable extended exit-value analysis (Phase D) |

The NVIDIA-specific knobs are particularly important. `track-trip-count-more` enables additional effort in BTC computation that upstream LLVM does not attempt -- the exact mechanism is not fully reversed, but the typo in its description string ("aggresively") matches the binary. `aggressive-positive-stride-analysis` is tied to a specific NVIDIA bug (nvbug 3972412) and enables proving NUW on AddRec expressions whose step is known positive from range analysis, creating a positive feedback loop between range computation and NoWrap inference.

## Function Map

| Address | Identity |
|---------|----------|
| `sub_DBB9F0` | `ScalarEvolution::getRangeRef()` -- core range evaluator |
| `sub_DBB110` | `getRangeForAffineARViaRange()` -- predecessor-based range |
| `sub_DBEFC0` | `computeUnsignedRangeFromAddRecTripCount()` |
| `sub_DBF480` | `computeSignedRangeFromAddRecTripCount()` |
| `sub_DE4FD0` | `computeExitValueRange()` -- Phase D exit value analysis |
| `sub_DDFBD0` | `getFullRangeFallback()` -- depth-exceeded fallback |
| `sub_DB0AC0` | `cacheRange()` -- insert range into hash table |
| `sub_DB5510` | `getKnownBits()` for SCEV (unsigned known bits) |
| `sub_DB55F0` | `getNumSignBits()` for SCEV (signed known bits) |
| `sub_DBED40` | `isKnownNonNegative(step)` |
| `sub_DBEC80` | `isKnownNonPositive(step)` |
| `sub_DCF3A0` | `getBackedgeTakenCount(loop, mode)` -- BTC dispatcher |
| `sub_DB9E00` | `computeBackedgeTakenCount()` -- per-loop BTC with caching |
| `sub_DB9040` | `computeExitCountForBranch()` -- exit condition analysis |
| `sub_DBA850` | `howFarToZero()` -- "reaches zero" trip count |
| `sub_DCE310` | `howManyLessThans()` -- "less than" trip count |
| `sub_DCFD50` | `computeExitCountExhaustively()` -- brute-force small loops |
| `sub_DCB270` | `computeExitLimit()` -- exit limit from condition |
| `sub_DB04E0` | `getSmallConstantTripCount()` |
| `sub_DB06C0` | `getSmallConstantMaxTripCount()` |
| `sub_DB6980` | BTC hash table growth / rehash |
| `sub_DE0180` | BTC hash table rehash-in-place (tombstone cleanup) |
| `sub_988CD0` | `getRangeFromUnknownSCEV()` -- range for SCEVUnknown |
| `sub_AB2160` | `ConstantRange::intersectWith()` |
| `sub_AB3510` | `ConstantRange::unionWith()` |
| `sub_ABA0E0` | `ConstantRange::addWithNoWrap()` |
| `sub_AB5480` | `ConstantRange::multiply()` |
| `sub_AB6A50` | `ConstantRange::udiv()` |
| `sub_ABD750` | `ConstantRange::minmax_combine()` |
| `sub_ABEA30` | `ConstantRange` from `!range` metadata |
| `sub_C4B490` | `ConstantRange` from KnownBits |

## Cross-References

- [SCEV Overview & Construction](./scev.md) -- expression creation, caching, simple mode
- [Loop Unrolling](./loop-unroll.md) -- how trip counts drive unroll factor selection
- [LoopVectorize & VPlan](./loop-vectorize.md) -- min trip count threshold, epilogue generation
- [Loop Strength Reduction](./lsr.md) -- IV manipulation driven by SCEV ranges
- [KnownBits & DemandedBits](./known-bits.md) -- GPU-specific known-bits feeding into range analysis
- [LLVM Knobs](../config/knobs.md) -- all SCEV-related knob values
