# ScalarEvolution Overview & Construction

ScalarEvolution (SCEV) is the foundational analysis that models how values change across loop iterations. Every loop optimization in cicc -- vectorization, unrolling, strength reduction, interchange, distribution -- depends on SCEV to answer three questions: "what is the trip count?", "what is the stride?", and "what is the value range?" NVIDIA's cicc v13.0 ships an LLVM 7.x-era ScalarEvolution with three categories of proprietary extensions: a **complexity control system** (`simple_mode`) that prevents SCEV from spending unbounded time on GPU kernels with hundreds of induction variables, **GPU-specific SCEV sources** that inject thread index bounds and launch configuration constraints into the analysis, and **recognition of CUDA-specific loop idioms** (warp-stride and grid-stride patterns) that have no analog in CPU code. This page documents SCEV expression construction -- the core `getSCEV` / `createSCEV` / `createNodeForInstruction` call chain. Range computation and trip count analysis are covered in [SCEV Range Analysis & Trip Counts](./scev-range-btc.md); cache invalidation and delinearization in [SCEV Invalidation & Delinearization](./scev-invalidation.md).

## Key Facts

| Property | Value |
|----------|-------|
| LLVM base version | 7.x-era `ScalarEvolution.cpp` |
| Top-level entry | `sub_DD8400` (`getSCEV`) |
| Core builder | `sub_DD65B0` (`createNodeForInstruction`, 1103 lines) |
| Worklist driver | `sub_DD8130` (non-recursive worklist `createSCEV`, 154 lines) |
| Instruction decomposer | `sub_D94080` (452 lines) |
| PHI handler | `sub_DD92B0` (`createNodeForPHI`) |
| GEP handler | `sub_DD3A70` (`getGEPExpr`) |
| Cache lookup | `sub_D98300` (`lookupSCEV`) |
| Cache store | `sub_DB77A0` (`insertSCEV`) |
| NVIDIA complexity scorer | `sub_DB3670` (expression size estimator) |
| SE object size | >1572 bytes (fields documented through offset +1572) |
| Calling conventions bypassing budget | CC 42, CC 43 (PTX kernel entry points) |

## ScalarEvolution Object Layout

The `ScalarEvolution` context (`SE`) is a large heap-allocated object. The fields relevant to SCEV construction:

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| `+0` | `Module*` | LLVM module / context pointer | |
| `+8` | `TargetLibraryInfo*` | TLI | Used for intrinsic recognition |
| `+32` | `DominatorTree*` | Dominator tree | Required for PHI analysis |
| `+40` | `LoopInfo*` | Loop analysis | AddRec construction needs this |
| `+48` | `void*` | Analysis pointer | Used by complexity scorer |
| `+320` | `SmallDenseSet` | PHI visited set | Prevents infinite recursion |
| `+976` | `void*` | Unsigned range cache table | 40-byte entries, open addressing |
| `+992` | `uint32_t` | Unsigned range cache capacity | Power-of-two |
| `+1008` | `void*` | Signed range cache table | Same structure |
| `+1024` | `uint32_t` | Signed range cache capacity | |
| `+1560` | `uint8_t` | `simple_mode` flag | 0 = normal, 1 = NVIDIA complexity control |
| `+1564` | `uint32_t` | `failure_count` | Simple mode: bailed instructions |
| `+1568` | `uint32_t` | `recursion_count` | Normal mode: depth counter |
| `+1572` | `uint8_t` | Complexity config bits | Tuning for the scorer |

The SE object also contains the `ValueExprMap` (primary SCEV cache mapping `Value*` to `SCEV*`), the backedge-taken count cache at offset `+648`/`+656`/`+672`, and the per-exit BTC cache at `+1168`/`+1184`. These are documented in the [range/BTC page](./scev-range-btc.md).

## The getSCEV Entry Point

`sub_DD8400` (`getSCEV`) is the single entry point for obtaining a SCEV expression for any LLVM `Value*`. Every consumer -- LoopVectorize, LoopUnroll, LSR, IndVarSimplify, LoopInterchange -- calls this function. The algorithm:

```
SCEV* getSCEV(SE *se, Value *V) {
    // 1. Memo-table check
    SCEV *cached = lookupSCEV(se, V);      // sub_D98300
    if (cached) return cached;

    // 2. Dispatch based on mode
    if (se->simple_mode == 0) {
        // NORMAL PATH
        CallingConv cc = V->getParent()->getParent()->getCallingConv();
        if (cc == 42 || cc == 43) {
            // PTX kernel entry: bypass budget entirely
            return createSCEV(se, V);
        }
        se->recursion_count++;
        if (se->recursion_count <= MaxRecursionDepth) {
            return createSCEV(se, V);
        }
        return getUnknown(se, V);           // budget exceeded
    }

    // NVIDIA SIMPLE MODE (complexity control)
    if (se->failure_count > MaxExprFailures) {
        SCEV *u = getUnknown(se, V);
        insertSCEV(se, V, u);              // cache the Unknown
        return u;
    }
    uint64_t complexity = computeExprSize(se, V);  // sub_DB3670
    if (complexity > MaxExprSize) {
        se->failure_count++;
        SCEV *u = getUnknown(se, V);
        insertSCEV(se, V, u);
        return u;
    }
    // Expression is small enough: run normal path with mode toggled off
    se->simple_mode = 0;
    se->recursion_count = 0;
    SCEV *result = createSCEV(se, V);
    se->simple_mode = 1;
    return result;
}
```

The **PTX kernel bypass** (calling conventions 42 and 43) is significant: kernel functions always receive full SCEV analysis regardless of budget. NVIDIA considers kernels important enough that truncating their analysis would lose more performance than the extra compile time costs. Device helper functions, by contrast, are subject to the budget.

### NVIDIA Simple Mode (Complexity Control)

Upstream LLVM uses a single recursion counter to bound `getSCEV`. NVIDIA replaces this with a two-stage gating system called `simple_mode` (enabled by the `scalar-evolution-complexity-control` flag, default **true**):

1. **Expression size scoring** (`sub_DB3670`): Before committing to building a SCEV expression, the system estimates how large the resulting expression tree would be. The scorer considers loop nesting depth, operand count, and configuration bits at `SE+1572`. If the score exceeds `scalar-evolution-max-expr-size` (default **384**), the instruction is classified as `SCEVUnknown` immediately.

2. **Failure counting** (`SE+1564`): Each time an instruction is classified as Unknown due to the size threshold, the failure counter increments. After `scalar-evolution-max-expr-failures` (default **100**) failures, ALL subsequent `getSCEV` calls for that function bail out immediately. This prevents a single pathological function from burning O(N^2) time trying to score thousands of instructions.

3. **Mode toggle**: When an instruction passes the size check, `simple_mode` is temporarily set to 0 and the recursion counter reset to 0 before calling `createSCEV`. This prevents double budget-checking (the normal recursion counter would immediately start counting from 0 for the sub-expression tree).

The practical effect: GPU kernels with hundreds of address computations (common in tiled matrix multiply, convolution stencils) hit the complexity wall early for outer variables, but the important inner loop induction variables -- which have simple affine structure -- always get analyzed.

## Worklist-Driven createSCEV

`sub_DD8130` implements a non-recursive worklist to avoid deep stack frames. NVIDIA replaced the upstream recursive `createSCEV` with this iterative approach to handle GPU kernels that can have extremely deep expression trees (deeply nested address computations involving multiple grid dimensions).

The worklist stores `Value*` pointers with tag bits in the low 3 bits:

| Bit | Meaning |
|-----|---------|
| Bit 2 (`0x4`) | First visit: needs full `createNodeForInstruction` |
| Bits 0-1 clear | Post-processing: operands have been evaluated, collect results |

Algorithm:

1. Push initial value with bit 2 set.
2. Pop top entry.
   - If bit 2 set: call `sub_DD80F0` (createSCEV wrapper), which checks `isSCEVable(V->getType())` via `sub_D97040`, then delegates to `sub_DD65B0` (`createNodeForInstruction`).
   - If the result is immediately available: cache it via `sub_DB77A0` and continue.
   - If operands are needed: push operands (without bit 2) for deferred processing.
3. Repeat until worklist empty.
4. Return `lookupSCEV(initial_value)`.

The `isSCEVable` check (`sub_D97040`) accepts integer types and pointer types. Floating-point values and aggregate types produce `SCEVUnknown`.

## Instruction Decomposer

Before the main opcode dispatch, `sub_D94080` (`decomposeIRInstruction`) analyzes each instruction and fills a 48-byte decomposition struct:

```c
struct SCEVDecomp {          // 48 bytes
    uint32_t kind;           // +0   decomposition opcode
    void    *operandL;       // +8   left operand (Value*)
    void    *operandR;       // +16  right operand (Value*)
    bool     hasNUW;         // +24  no-unsigned-wrap flag
    bool     hasNSW;         // +25  no-signed-wrap flag
    void    *extra;          // +32  third operand / loop variable
    bool     valid;          // +40  decomposition succeeded
};
```

The decomposer extracts NUW/NSW flags from `inst->byte[1]` (bit 2 = NUW, bit 1 = NSW), and these flags are only captured for opcodes matching the bitmask `0x40540000000000` -- covering add, sub, mul, shl, and related flag-bearing arithmetic. The `kind` field values:

| Kind | Decimal | SCEV Construction |
|------|---------|-------------------|
| `0x0D` | 13 | Add/Sub -- iterative addend collection |
| `0x0F` | 15 | MulRec -- multiply-recurrence (loop-carried) |
| `0x11` | 17 | Multiply -- iterative multiplicand collection |
| `0x13` | 19 | UDiv |
| `0x16` | 22 | UMax select pattern |
| `0x19` | 25 | Shl -- converted to multiply by 2^N |
| `0x1A` | 26 | Generic shift/bitop fallback |
| `0x1B` | 27 | LShr -- complex truncate+extend chain |
| `0x1C` | 28 | AShr -- sign-extend analysis |
| `0x1D` | 29 | ICmp / comparison |
| `0x1E` | 30 | And (bitwise) -- pointer truncation patterns |

The decomposer includes a **GPU-specific PHI detection path** (kind 64): when a PHI node's incoming value chain traces through a comparison instruction (`byte == 0x55`) whose operand is a function-entry value (`byte == 0`) that resolves to one of the recognized NVIDIA builtins (intrinsic IDs 312, 333, 339, 360, 369, 372), the decomposer creates a specialized recurrence form. This is how `threadIdx.x`-bounded loop variables become proper AddRec expressions.

## createNodeForInstruction: The Core Builder

`sub_DD65B0` (1103 lines) is the largest function in the SCEV subsystem. It operates in three phases:

### Phase 1: Fast Path (lines 300-312)

Checks the instruction's type byte. Constants (byte 17) go directly to `getConstant`. Non-instruction values go to `getUnknown`. Real instructions check loop depth via `LoopInfo` -- if the instruction's loop nesting exceeds the maximum tracked depth, it bails to `getUnknown` with a simplified operand from `sub_ACADE0`.

### Phase 2: Decomposition-Based Dispatch (lines 336-933)

After calling the instruction decomposer, dispatches on `decomp.kind`:

**Add/Sub (kind 13):** Iteratively collects addends into a `SmallVector`. For each operand with a non-zero `extra` field (the loop iteration variable), checks the SCEV cache, and if the operand has a known loop context (from `sub_DD86E0` / `getLoopForExpr`), builds an `SCEVAddRecExpr`. Otherwise recursively calls `getSCEV` and optionally negates (for subtraction via `getNegativeSCEV`). Final result: `getAddExpr(collected_operands)`.

**Multiply (kind 17):** Same iterative structure as Add but builds `getMulExpr`. For loop-carried chains, constructs `getAddRecExpr(start, step, flags)`.

**Shl (kind 25):** Converts shift-left to multiplication by a power of two. When the shift amount is a constant: extracts the shift amount, verifies it fits in the type width (`sub_986EE0`), then builds `getMulExpr(getSCEV(base), getConstant(1 << shamt), flags)`. Handles nested shl-of-shl by re-decomposing.

**LShr (kind 27):** When shifting right by a constant amount, builds a chain of `getMulExpr + getTruncateExpr + getZeroExtendExpr` to represent the bit extraction pattern. Falls back for non-constant shifts.

**AShr (kind 28):** Complex bit-extraction logic. For constant shifts, analyzes known bits to determine whether the shift extracts only zeros from the sign position. If provable, builds `getSignExtendExpr(getTruncateExpr(getSCEV(base), intermediate_type), original_type)`. For non-constant shifts, tries SMin/SMax pattern matching.

**And (kind 30):** Handles pointer truncation patterns. When the mask equals `(1 << ptr_bits) - 1` (a ptrtoint-then-mask pattern), builds `getPtrToIntExpr + getSignExtendExpr`. Otherwise bails.

### Phase 3: Opcode-Based Dispatch (lines 936-1101)

Handles instructions not captured by the decomposer. The normalized opcode maps raw instruction bytes to semantic categories:

**Call/Intrinsic (cases 5, 56):** First tries the intrinsic SCEV lookup table (`sub_B494D0`). For known intrinsics, dispatches on intrinsic ID:

| ID | Hex | SCEV Construction | Likely Intrinsic |
|----|-----|-------------------|------------------|
| 1 | `0x001` | `getNotSCEV(op0)` | bitwise NOT |
| 7 | `0x007` | `getSCEV(op0)` (identity) | `llvm.assume` |
| 292 | `0x124` | `getSCEV(op0)` (identity) | PTX intrinsic passthrough |
| 329 | `0x149` | `getUMinExpr(op0, op1)` | `llvm.umin` |
| 330 | `0x14A` | `getSMinExpr(op0, op1)` | `llvm.smin` |
| 344 | `0x158` | `getSCEV(op0)` (identity) | passthrough |
| 359 | `0x167` | `getSMinExpr + getUDivExpr + getAddExpr` | complex min/div |
| 365 | `0x16D` | `getSMaxExpr(op0, op1)` | `llvm.smax` |
| 366 | `0x16E` | `getSMinExpr(op0, op1)` | `llvm.smin` variant |
| 371 | `0x173` | `getAddRecExpr(op0, getUDivExpr(op0, op1))` | recurrence with division |
| 493 | `0x1ED` | `getConstant(inst->qword[1])` | constant from intrinsic metadata |

**PHI Node (case 34):** Dispatches to `sub_DD92B0` (`createNodeForPHI`). Walks PHI incoming values, checks for loop recurrence. If the PHI forms a recurrence: builds `{start, +, step}` as an `SCEVAddRecExpr`. Otherwise returns `SCEVUnknown`.

**GEP (case 47):** Calls `sub_DD3A70` (`getGEPExpr`). Computes the SCEV of the base pointer, then adds the SCEV of each index scaled by the element size. If the result is `SCEVUnknown`, bails.

**Casts (cases 38-40):** Trunc produces `getTruncateExpr`. SExt produces `getSignExtendExpr`. ZExt has a special optimization: if the source decomposes as a multiply-recurrence (kind 15), it builds separate zero-extensions of start and step, then constructs `getAddRecExpr(zext(start), zext(step), NUW)` -- preserving the recurrence structure across the extension.

**BitCast/AddrSpaceCast (case 49):** If both source and target types are SCEV-able, returns `getSCEV(source)` (transparent). Otherwise `getUnknown`.

**Select (cases 20, 23):** If condition and true-value are loop-invariant (`sub_DBED40`), builds `getUDivExpr` (case 20) or `getUMaxExpr` (case 23) of the branches.

## GPU-Specific SCEV Sources

### Thread and Block Index Builtins

When the instruction decomposer encounters a PHI whose incoming value chain traces to one of NVIDIA's special register intrinsics, it recognizes it as a bounded induction variable. The recognized intrinsic IDs and their SCEV significance:

| Intrinsic ID | CUDA Variable | SCEV Range Bound |
|-------------|---------------|------------------|
| 312 | `blockDim.x` / `gridDim.x` | Dimension query -- provides trip count upper bound |
| 333 | `threadIdx.x` | Range: `[0, blockDim.x)` |
| 339 | `threadIdx.y` / `blockIdx.x` | Range: `[0, blockDim.y)` or `[0, gridDim.x)` |
| 360 | `threadIdx.z` / `blockIdx.y` | Range: `[0, blockDim.z)` or `[0, gridDim.y)` |
| 369 | `blockIdx.z` | Range: `[0, gridDim.z)` |
| 372 | `warpSize` / `laneid` | Range: `[0, 32)` (constant on all architectures) |

These ranges are injected during SCEV construction, not during range analysis. When a PHI node tests a value against `threadIdx.x` (for example, a loop `for (int i = threadIdx.x; i < N; i += blockDim.x)`), the decomposer produces an `SCEVAddRecExpr` whose start value carries the constraint `[0, blockDim.x)`. This propagates through all downstream SCEV consumers.

The CUDA variable to LLVM intrinsic mapping is:

| CUDA | LLVM Intrinsic | PTX Register |
|------|----------------|-------------|
| `threadIdx.x` | `@llvm.nvvm.read.ptx.sreg.tid.x` | `%tid.x` |
| `threadIdx.y` | `@llvm.nvvm.read.ptx.sreg.tid.y` | `%tid.y` |
| `threadIdx.z` | `@llvm.nvvm.read.ptx.sreg.tid.z` | `%tid.z` |
| `blockDim.x` | `@llvm.nvvm.read.ptx.sreg.ntid.x` | `%ntid.x` |
| `blockIdx.x` | `@llvm.nvvm.read.ptx.sreg.ctaid.x` | `%ctaid.x` |
| `gridDim.x` | `@llvm.nvvm.read.ptx.sreg.nctaid.x` | `%nctaid.x` |

### PTX Kernel Calling Convention Bypass

Functions with calling convention 42 or 43 (PTX `__global__` kernels) bypass the SCEV recursion budget entirely. The rationale: kernels are the units of work the programmer explicitly marked for GPU execution. Spending extra compile time to fully analyze their loop structure always pays off because:

1. Kernels are where vectorization decisions have the highest payoff.
2. GPU hardware constraints (occupancy, shared memory) demand precise trip count knowledge.
3. Kernel functions are few per compilation unit, so the budget bypass does not cause compile-time explosion.

Device functions (`__device__`, conventions other than 42/43) remain subject to the standard budget.

### Warp-Stride and Grid-Stride Loop Patterns

Two CUDA-specific loop idioms produce distinctive SCEV expressions:

**Warp-stride loop:**
```c
for (int i = threadIdx.x; i < N; i += warpSize) { ... }
```
Produces: `{threadIdx.x, +, 32}<loop>` -- an `SCEVAddRecExpr` with start = `SCEVUnknown(threadIdx.x)` (range `[0, blockDim.x)`) and step = `SCEVConstant(32)`. The trip count is `udiv(N - threadIdx.x + 31, 32)`.

**Grid-stride loop:**
```c
for (int i = blockIdx.x * blockDim.x + threadIdx.x; i < N; i += blockDim.x * gridDim.x) { ... }
```
Produces: `{blockIdx.x * blockDim.x + threadIdx.x, +, blockDim.x * gridDim.x}<loop>`. The start is an `SCEVAddExpr(SCEVMulExpr(blockIdx.x, blockDim.x), threadIdx.x)`. The step is `SCEVMulExpr(blockDim.x, gridDim.x)`. Both start and step are `SCEVUnknown` values with range constraints derived from the builtin table.

The delinearization system (documented in [SCEV Invalidation & Delinearization](./scev-invalidation.md)) specifically recognizes the grid-stride pattern -- when a `ZeroExtend` or `SignExtend` wraps an `AddRecExpr` whose step matches `blockDim.x * gridDim.x`, the delinearizer treats it as a dimension boundary for multi-dimensional array access recovery.

## SCEV Expression Types and the FoldingSet

SCEV expressions are uniqued in a `FoldingSet` (LLVM's hash-based deduplication container). Each expression type is identified by a `uint16` opcode at `scev_expr+24`:

| Opcode | Type | Operands | Notes |
|--------|------|----------|-------|
| 0 | `SCEVConstant` | 1 (APInt) | Leaf: integer constant |
| 1 | `SCEVUnknown` | 1 (Value*) | Leaf: opaque value, possibly with range info |
| 2 | `SCEVTruncateExpr` | 1 + type | Truncation cast |
| 3 | `SCEVZeroExtendExpr` | 1 + type | Zero extension |
| 4 | `SCEVSignExtendExpr` | 1 + type | Sign extension |
| 5 | `SCEVAddExpr` | N-ary | Commutative sum |
| 6 | `SCEVMulExpr` | N-ary | Commutative product |
| 7 | `SCEVUDivExpr` | 2 | Unsigned division |
| 8 | `SCEVAddRecExpr` | 2+ (start, step, ...) | `{start, +, step}<loop>` recurrence |
| 9 | `SCEVSMaxExpr` | N-ary | Signed maximum |
| 10 | `SCEVUMaxExpr` | N-ary | Unsigned maximum |
| 11 | `SCEVSMinExpr` | N-ary | Signed minimum |
| 12 | `SCEVUMinExpr` | N-ary | Unsigned minimum |
| 13 | (variant min/max) | N-ary | Additional min/max form |
| 14 | `SCEVCouldNotCompute` | 0 | Sentinel: analysis failed |
| 15 | `SCEVSequentialUMinExpr` | N-ary | Short-circuit unsigned min |

The expression node layout:

| Offset | Size | Field |
|--------|------|-------|
| `+0` | 8 | Vtable / tag |
| `+24` | 2 | Opcode (SCEV kind) |
| `+28` | 2 | Flags: NUW=`0x2`, NSW=`0x4` |
| `+32` | 8 | Operand array pointer or first operand |
| `+40` | varies | Operand count (for N-ary) or second operand |

Pointer comparisons suffice for SCEV equality because of the uniquing: two `SCEV*` values are equal if and only if they point to the same node.

### SCEV Constructor Functions

Each expression type has a dedicated constructor that canonicalizes and deduplicates:

| Address | Function | Signature |
|---------|----------|-----------|
| `sub_DC8BD0` | `getAddExpr` | `(SmallVector &operands, flags, depth)` |
| `sub_DC7ED0` | `getAddExpr` | `(SCEV *a, SCEV *b, flags, depth)` |
| `sub_DCA690` | `getMulExpr` | `(SCEV *a, SCEV *b, flags, depth)` |
| `sub_DCC810` | `getAddRecExpr` | `(SCEV *start, SCEV *step, flags, depth)` |
| `sub_DCB270` | `getUDivExpr` | `(SCEV *lhs, SCEV *rhs)` |
| `sub_DCFA50` | `getUMaxExpr` | `(SCEV *a, SCEV *b)` |
| `sub_DCEE80` | `getSMinExpr` | `(SCEV *a, SCEV *b)` |
| `sub_DCE050` | `getSMaxExpr` | `(SCEV *a, SCEV *b)` |
| `sub_DCDFA0` | `getUMinExpr` | `(SCEV *a, SCEV *b)` |
| `sub_DC5200` | `getTruncateExpr` | `(SCEV *op, Type *ty, depth)` |
| `sub_DC5000` | `getZeroExtendExpr` | `(SCEV *op, Type *ty, depth)` |
| `sub_DC2B70` | `getSignExtendExpr` | `(SCEV *op, Type *ty, depth)` |
| `sub_DD1D00` | `getPtrToIntExpr` | `(SCEV *ptr)` |
| `sub_DA26C0` | `getConstant` | `(APInt val)` |
| `sub_DA3860` | `getUnknown` | `(Value *V)` |
| `sub_DCAF50` | `getNegativeSCEV` | `(SCEV *expr, flags)` |
| `sub_DCE000` | `getNotSCEV` | `(SCEV *expr, bool isNSW)` -- `-1 - x` |

The N-ary constructors (`getAddExpr`, `getMulExpr`, min/max) canonicalize operand order and fold constants. For example, `getAddExpr({5, x, 3})` folds to `getAddExpr({8, x})` and orders the constant first.

## The SCEV Cache

The primary SCEV cache (`ValueExprMap`) maps `Value*` to `SCEV*` using an open-addressed hash table with the standard hash function used throughout cicc's SCEV subsystem:

```
slot = ((uint32_t)key >> 9) ^ ((uint32_t)key >> 4)
slot &= (capacity - 1)
```

Sentinels: `EMPTY = 0xFFFFFFFFFFFFF000` (`-4096`), `TOMBSTONE = 0xFFFFFFFFFFFFE000` (`-8192`). Capacity is always a power of two. Growth occurs at 75% load factor (doubling), and in-place rehashing (tombstone cleanup) triggers when fewer than 1/8 of slots are truly empty.

Cache lookup (`sub_D98300`) is called at the top of every `getSCEV` invocation. Cache store (`sub_DB77A0`) is called after every successful SCEV construction, and also when the complexity control bails to `SCEVUnknown` (caching the Unknown result prevents re-scoring the same instruction).

The simple mode's failure caching is critical for performance: once an instruction is classified as `SCEVUnknown`, the result is cached so that subsequent queries (from different loop analysis passes) return instantly rather than re-running the complexity scorer.

## How SCEV Feeds Loop Optimizations

SCEV is consumed by every loop optimization in cicc. The key interfaces:

**LoopVectorize** (`sub_DFAE00` and callers): Calls `getBackedgeTakenCount` (`sub_DCF980`) to determine whether the loop has a computable trip count. If not, vectorization is abandoned. Uses `getSmallBestKnownTC` (`sub_2AA7EC0`) for the trip count upper bound, which is compared against `-vectorizer-min-trip-count`. SCEV range analysis (`sub_DBB9F0`) proves that the epilogue trip count is sufficient for the minimum vector factor. Runtime SCEV overflow checks generate `scev.check` basic blocks.

**LoopUnroll** (`sub_19B6690`): The unroll factor selection function extracts `MaxTripCount` from SCEV. Runtime trip counts below `flat-loop-tripcount-threshold` (default 5) mark the loop as "flat" and skip unrolling. Partial unrolling requires `BackedgeCount % UnrollCount` computation. After unrolling, `sub_2A13F00` reconciles SCEV and LoopInfo for the modified loop.

**Loop Strength Reduction** (`sub_19A87A0`): The NVIDIA custom LSR reads SCEV expressions for each loop use (base SCEV at `+0`, stride SCEV at `+8`, loop bounds at `+712`/`+720`). The formula solver generates alternatives by factoring common strides out of SCEV expressions. SCEV normalization (`sub_199D980`) provides canonical forms for hash-table keying.

**IndVarSimplify** (`sub_1945A50`): Uses SCEV to compute exit values, rewrite loop exit conditions, and perform LFTR (Linear Function Test Replace). The NVIDIA `Disable-unknown-trip-iv` knob skips the entire pass when the trip count is not SCEV-computable, protecting GPU-specific loops from incorrect IV transforms.

**LoopInterchange** (`sub_E05-loop-interchange`): Uses SCEV stride analysis to determine which loops carry memory strides. If a subscript has stride in both inner and outer loops, it is marked "ambiguous" and interchange is blocked.

## Configuration: All SCEV Knobs

### NVIDIA-Specific Knobs

| Knob | Default | Effect |
|------|---------|--------|
| `scalar-evolution-complexity-control` | **true** | Enables the `simple_mode` system |
| `scalar-evolution-max-expr-size` | **384** | Max SCEV expression complexity score before bailing to Unknown |
| `scalar-evolution-max-expr-failures` | **100** | Max bailed instructions before giving up on entire function |
| `scalar-evolution-max-add-items` | **500** | Max addends in a single `SCEVAddExpr` |
| `do-sign-ext-expand` | **false** | Expand sign-extensions during SCEV construction |
| `do-sign-ext-simplify` | (bool) | Simplify SCEV on sign-extend expressions |
| `track-trip-count-more` | **true** | More aggressive trip count tracking |
| `common-factor-with-mr265` | **true** | SCEV common factor optimization (internal MR reference) |
| `scalar-evolution-classify-expressions` | **true** | Enable SCEV expression classification |
| `aggressive-positive-stride-analysis` | (bool) | Aggressive stride analysis (see nvbug 3972412) |
| `scev-cgp-tid-max-value` | (int) | Max value of thread ID for SCEV-CGP address mode optimization |

### Upstream LLVM Knobs (Preserved in cicc)

| Knob | Default | Effect |
|------|---------|--------|
| `scalar-evolution-max-recursion-depth` | **100** | Hard counter for `getSCEV` depth in normal mode |
| `scalar-evolution-max-iterations` | **100** | Max iterations for constant evolution |
| `scalar-evolution-max-arith-depth` | **32** | Max arithmetic simplification depth |
| `scalar-evolution-max-cast-depth` | **8** | Max cast folding depth |
| `scalar-evolution-max-ext-depth` | **8** | Max extension analysis depth |
| `scalar-evolution-max-constant-evolving-depth` | **32** | Max depth for constant evolving analysis |
| `scalar-evolution-max-scev-compare-depth` | **32** | Max depth for SCEV comparison |
| `scalar-evolution-max-scev-operations-implication-depth` | **2** | Max depth for implication reasoning |
| `scalar-evolution-max-value-compare-depth` | **2** | Max depth for value comparison |
| `scev-mulops-inline-threshold` | **32** | Max multiply operands before outline |
| `scev-addops-inline-threshold` | **500** | Max add operands before outline |
| `verify-scev` | **false** | Enable SCEV verification |
| `verify-scev-strict` | **false** | Stricter SCEV verification |
| `verify-scev-maps` | **false** | Verify SCEV map consistency |

### SCEV-CGP Knobs (Address Mode Optimization)

| Knob | Effect |
|------|--------|
| `do-scev-cgp` | Enable SCEV-based CodeGenPrepare |
| `do-scev-cgp-aggresively` | Aggressive mode (sic -- typo preserved in binary) |
| `do-function-scev-cgp` | Function-level SCEV-CGP |
| `nv-disable-scev-cgp` | Disable the SCEV-CGP pass entirely |
| `scev-cgp-control` | Control number of transformations |
| `scev-cgp-cross-block-limit` | Max common bases from a single block |
| `scev-cgp-idom-level-limit` | Limit IDOM traversal level |
| `scev-cgp-inst-limit` | Max instructions considered per parameter |
| `scev-cgp-old-base` | Use old base computation method |
| `scev-cgp-tid-max-value` | Max thread ID value for address mode analysis |
| `print-after-scev-cgp` | Print function IR after SCEV-CGP |

## Function Map

| Address | Identity | Lines | Role |
|---------|----------|-------|------|
| `sub_DD8400` | `getSCEV` | 66 | Top-level entry; cache + mode dispatch |
| `sub_DD8130` | Worklist `createSCEV` | 154 | Non-recursive worklist driver |
| `sub_DD80F0` | `createSCEV` wrapper | 14 | Type check + delegate |
| `sub_DD65B0` | `createNodeForInstruction` | 1103 | Core 3-phase opcode dispatch |
| `sub_D94080` | `decomposeIRInstruction` | 452 | Instruction to decomposition struct |
| `sub_DD92B0` | `createNodeForPHI` | -- | PHI to AddRec conversion |
| `sub_DD99C0` | `createNodeForSelectOrPHI` | -- | Select/PHI combined handler |
| `sub_DD6410` | `getExistingExpr` | -- | Fast path for phi recurrence |
| `sub_DD3A70` | `getGEPExpr` | -- | GEP to SCEV conversion |
| `sub_DD86E0` | `getLoopForExpr` | -- | Determine loop context for expression |
| `sub_D98300` | `lookupSCEV` | -- | Cache lookup (ValueExprMap) |
| `sub_DB77A0` | `insertSCEV` | -- | Cache store |
| `sub_DB3670` | Expression size scorer | -- | NVIDIA complexity estimator |
| `sub_D97040` | `isSCEVable` | -- | Type is integer or pointer |
| `sub_D96A50` | `isUnknown` / `isFailedSCEV` | -- | Check SCEVUnknown |
| `sub_D95540` | `getSCEVType` | -- | Extract LLVM Type from SCEV expr |
| `sub_D97050` | `getTypeBitWidth` | -- | Bit width of a type |
| `sub_B494D0` | `lookupIntrinsicSCEV` | -- | Intrinsic fast-path table |
| `sub_988010` | `isIntrinsicCall` | -- | Intrinsic detection |
| `sub_DBED40` | `isLoopInvariant` | -- | Loop invariance check |
| `sub_BCAC40` | `isIntegerTy` | -- | Integer type check |
| `sub_DBB9F0` | `getRangeRef` | -- | ConstantRange evaluator (see [range page](./scev-range-btc.md)) |
| `sub_DB9E00` | `computeBackedgeTakenCount` | -- | BTC computation (see [range page](./scev-range-btc.md)) |
| `sub_DE2750` | `forgetLoop` | -- | Cache invalidation (see [invalidation page](./scev-invalidation.md)) |
| `sub_DE9D10` | `delinearize` | -- | Array delinearization (see [invalidation page](./scev-invalidation.md)) |

## Cross-References

- [LoopVectorize & VPlan](./loop-vectorize.md) -- primary consumer of trip counts and SCEV ranges
- [Loop Unrolling](./loop-unroll.md) -- uses SCEV for unroll factor selection and trip count analysis
- [Loop Strength Reduction (NVIDIA)](./lsr.md) -- uses SCEV expressions for formula generation
- [SCEV Range Analysis & Trip Counts](./scev-range-btc.md) -- ConstantRange computation and backedge-taken count
- [SCEV Invalidation & Delinearization](./scev-invalidation.md) -- cache eviction and multi-dimensional array recovery
- [Builtin Table Structure](../builtins/index.md) -- intrinsic ID assignments for threadIdx/blockIdx/etc.
- [LLVM Knobs (1,689)](../config/knobs.md) -- full knob catalog including all SCEV knobs
