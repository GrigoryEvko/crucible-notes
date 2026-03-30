# ScalarEvolution Overview & Construction

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Analysis/ScalarEvolution.cpp`, `llvm/include/llvm/Analysis/ScalarEvolution.h` (LLVM 20.0.0)
>
> **LLVM version note:** CICC v13.0 is based on LLVM 20.0.0 `ScalarEvolution.cpp`. Evidence: the non-recursive worklist-based `createSCEV` driver (`sub_DD8130`) matches the LLVM 16+ refactoring that replaced the recursive `createNodeForValue`. The `getSmallConstantTripCount`/`getSmallConstantMaxTripCount` API matches LLVM 17+ signatures. NVIDIA's three extension categories -- `simple_mode` complexity control, GPU-specific SCEV sources (thread index bounds), and CUDA loop idiom recognition (warp-stride, grid-stride) -- are layered on top of the stock LLVM 20 analysis with no modifications to the core SCEV algebra.

ScalarEvolution (SCEV) is the foundational analysis that models how values change across loop iterations. Every loop optimization in cicc -- vectorization, unrolling, strength reduction, interchange, distribution -- depends on SCEV to answer three questions: "what is the trip count?", "what is the stride?", and "what is the value range?" NVIDIA's cicc v13.0 ships an LLVM 20.0.0-based ScalarEvolution with three categories of proprietary extensions: a **complexity control system** (`simple_mode`) that prevents SCEV from spending unbounded time on GPU kernels with hundreds of induction variables, **GPU-specific SCEV sources** that inject thread index bounds and launch configuration constraints into the analysis, and **recognition of CUDA-specific loop idioms** (warp-stride and grid-stride patterns) that have no analog in CPU code. This page documents SCEV expression construction -- the core `getSCEV` / `createSCEV` / `createNodeForInstruction` call chain. Range computation and trip count analysis are covered in [SCEV Range Analysis & Trip Counts](./scev-range-btc.md); cache invalidation and delinearization in [SCEV Invalidation & Delinearization](./scev-invalidation.md).

## Key Facts

| Property | Value |
|----------|-------|
| LLVM base version | 20.0.0 `ScalarEvolution.cpp` |
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

Upstream LLVM uses a single recursion counter to bound `getSCEV`. NVIDIA replaces this with a two-stage gating system called `simple_mode` (enabled by the `scalar-evolution-complexity-control` flag, default **true**). The system is stored entirely in four bytes of the SE object:

| Offset | Type | Field | Role |
|--------|------|-------|------|
| `+1560` | `uint8` | `simple_mode` | 0 = normal (upstream-style), 1 = NVIDIA complexity control |
| `+1564` | `uint32` | `failure_count` | Running count of instructions classified as `SCEVUnknown` by the size gate |
| `+1568` | `uint32` | `recursion_count` | Upstream-style depth counter, only active when `simple_mode == 0` |
| `+1572` | `uint8` | `complexity_config` | Tuning bits read by the expression size scorer |

When `scalar-evolution-complexity-control` is true (the default), the SE constructor initializes `simple_mode` to 1. The gating operates in three stages:

**Stage 1 -- Failure gate.** Before scoring anything, `getSCEV` checks `failure_count > scalar-evolution-max-expr-failures` (global `qword_4F88348`, default **100**). If the function has already exceeded the failure budget, the instruction is classified as `SCEVUnknown`, the result is cached via `sub_DB77A0` (`insertSCEV`), and control returns immediately. This prevents a single pathological function from burning O(N^2) time trying to score thousands of instructions that will all fail.

**Stage 2 -- Expression size scoring.** The scorer `sub_DB3670` (`expressionComplexity`, 35KB binary, self-recursive) estimates how large the resulting SCEV expression tree would be. It walks the instruction's def-use chain bottom-up, counting nodes and weighting by expression kind:

```
uint64_t expressionComplexity(SE *se, Value *V) {
    // sub_DB3670 -- self-recursive, calls sub_CF4090 for SCEV node size
    if (V is Constant)     return 1;
    if (V is Argument)     return 1;
    if (!isSCEVable(V))    return 0;      // non-integer/pointer: free

    // Look up V in the SCEV cache; if already a SCEV node,
    // delegate to the node-size estimator
    SCEV *cached = lookupSCEV(se, V);
    if (cached)
        return sub_CF4090(cached);         // count nodes in SCEV tree

    // Not yet in cache: estimate from instruction structure
    Instruction *I = dyn_cast<Instruction>(V);
    if (!I) return 1;

    uint64_t score = 1;                    // 1 for this node
    Loop *L = LoopInfo->getLoopFor(I);
    if (L) {
        uint32_t depth = L->getLoopDepth();
        score += depth;                    // loop nesting multiplier
    }

    // Walk operands, accumulating recursively
    for (unsigned i = 0; i < I->getNumOperands(); i++) {
        score += expressionComplexity(se, I->getOperand(i));
    }

    // Apply configuration scaling from SE+1572
    if (se->complexity_config & 0x1)
        score = score * 3 / 2;            // 50% penalty for aggressive mode
    if (se->complexity_config & 0x2)
        score += depth * 2;               // extra loop nesting weight

    return score;
}
```

The helper `sub_CF4090` counts nodes in an existing SCEV expression tree: it returns 1 for `SCEVConstant` and `SCEVUnknown`, recurses into operands for `SCEVAddExpr`/`SCEVMulExpr`/`SCEVAddRecExpr` (summing child sizes + 1), and handles casts (`Truncate`/`ZeroExtend`/`SignExtend`) as 1 + child size. The node-size estimate is precise because SCEV expressions are uniqued -- the same sub-expression pointer is never double-counted within a single scoring call.

If the total score exceeds `scalar-evolution-max-expr-size` (global `dword_4F88428`, default **384**), the instruction is classified as `SCEVUnknown` and `failure_count` is incremented. The `SCEVUnknown` result is cached immediately so that later queries from different loop passes return instantly rather than re-running the scorer.

**Stage 3 -- Mode toggle.** When an instruction passes the size check (score <= 384), `simple_mode` is temporarily set to 0 and the recursion counter reset to 0 before calling `createSCEV`:

```
se->simple_mode = 0;        // disable complexity gating
se->recursion_count = 0;    // reset upstream counter for this sub-tree
SCEV *result = createSCEV(se, V);
se->simple_mode = 1;        // restore
```

This prevents double budget-checking: the upstream recursion counter inside `createSCEV` starts from 0 for the sub-expression tree rather than inheriting a parent depth. Each `createSCEV` call thus gets a fresh budget of `scalar-evolution-max-recursion-depth` (default 100) for its own sub-tree.

**Practical effect:** GPU kernels with hundreds of address computations (common in tiled matrix multiply, convolution stencils) hit the complexity wall early for outer variables, but the important inner loop induction variables -- which have simple affine structure -- always get analyzed. The two-stage gate (score first, then depth-limit) avoids the upstream problem where a single deep operand chain exhausts the entire recursion budget for the function.

**Why not just raise the upstream recursion limit?** The upstream counter is a *global* depth counter -- raising it means every instruction in the function gets more budget, including ones that will never produce useful SCEV expressions. The NVIDIA approach is *per-instruction*: each instruction is independently scored, and only instructions with manageable complexity get the full treatment. This keeps total SCEV compile time bounded at O(N * max_expr_size) rather than O(N * max_recursion_depth^2).

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

Two CUDA-specific loop idioms produce distinctive SCEV expressions. Neither has an analog in CPU code, and cicc's SCEV subsystem recognizes both at construction time -- not as a post-hoc pattern match.

#### Warp-Stride Loop

```c
for (int i = threadIdx.x; i < N; i += warpSize) { ... }
```

The PHI decomposer (`sub_D94080`) recognizes the increment value as the constant 32 (warpSize is a compile-time constant on all NVIDIA architectures). The resulting SCEV:

```
{threadIdx.x, +, 32}<nuw><loop>
```

- **Start**: `SCEVUnknown(@llvm.nvvm.read.ptx.sreg.tid.x)`, range `[0, blockDim.x)` (injected from the builtin table, intrinsic ID 333).
- **Step**: `SCEVConstant(32)`.
- **Flags**: NUW (no-unsigned-wrap) is set because the start is non-negative and the step is positive. The PHI decomposer sets this flag when the incoming value (intrinsic ID 372 = warpSize) resolves to a constant and the start range has a non-negative lower bound.
- **Trip count**: The backedge-taken count (`sub_DB9E00`) computes:
  ```
  BTC = udiv(N - threadIdx.x + 31, 32)
      = udiv(sext(N) - sext(start) + step - 1, step)
  ```
  This is the standard SCEV `computeExitCountFromICmpUN` path for `i < N` with stride 32.

The NUW flag is critical: it allows the loop vectorizer to prove that the induction variable never wraps, enabling vectorization without a runtime overflow check. Without the warp-stride recognition, the vectorizer would see `SCEVUnknown(threadIdx.x)` as an opaque value and conservatively assume wrapping is possible.

#### Grid-Stride Loop

```c
for (int i = blockIdx.x * blockDim.x + threadIdx.x; i < N; i += blockDim.x * gridDim.x) { ... }
```

The instruction decomposer traces through the PHI's increment chain. The addition `blockDim.x * gridDim.x` is recognized as two calls to special register intrinsics (IDs 312 for blockDim.x and 312 again for gridDim.x) combined in a multiply. The resulting SCEV:

```
{blockIdx.x * blockDim.x + threadIdx.x, +, blockDim.x * gridDim.x}<loop>
```

Decomposition detail:
- **Start**: `SCEVAddExpr(SCEVMulExpr(SCEVUnknown(blockIdx.x), SCEVUnknown(blockDim.x)), SCEVUnknown(threadIdx.x))`.
  - `blockIdx.x` (ID 339): range `[0, gridDim.x)`.
  - `blockDim.x` (ID 312): range `[1, 1024]` (hardware limit).
  - `threadIdx.x` (ID 333): range `[0, blockDim.x)`.
  - The combined start range is `[0, gridDim.x * blockDim.x)` = `[0, total_threads)`.
- **Step**: `SCEVMulExpr(SCEVUnknown(blockDim.x), SCEVUnknown(gridDim.x))` -- this is the total grid size. Both operands are `SCEVUnknown` values with ranges from the builtin table.
- **Trip count**: `computeBackedgeTakenCount` (`sub_DB9E00`) produces:
  ```
  BTC = udiv(N - start + step - 1, step)
  ```
  where `start` and `step` are symbolic. The trip count itself is `SCEVUnknown` (the exact value depends on runtime launch configuration), but the *maximum* trip count can be bounded using the range constraints.

#### Delinearization of Grid-Stride Patterns

The delinearization system (`sub_DE9D10`, documented in [SCEV Invalidation & Delinearization](./scev-invalidation.md)) specifically recognizes the grid-stride pattern. In the `ZeroExtend`/`SignExtend` handlers (cases 3 and 4 of the delinearizer), when an `AddRecExpr` whose step matches the delinearization context's `step_recurrence` field (`ctx+0x68`):

1. The delinearizer checks if `step == blockDim.x * gridDim.x` by comparing the step SCEV pointer against `ctx[+0x68]`.
2. If matched and the `AddRec` has exactly 2 operands (start + step), the delinearizer treats this as a *dimension boundary* -- the step represents the stride of the outer dimension in a multi-dimensional array access.
3. The dimension size is extracted and added to the term collector at `ctx[+0x58]`. The element count is obtained via `sub_D33D80` (`getElementSize`) and `sub_DA4270` (`getConstant`).
4. The delinearizer reconstructs the multi-dimensional subscript by applying `getZeroExtendExpr` (or `getSignExtendExpr`) to the start and step separately, preserving the recurrence structure across the extension.

This is how cicc recovers the original multi-dimensional array indices from grid-stride loops over flattened arrays -- essential for dependence analysis in LoopVectorize and LoopInterchange.

#### Block-Stride Loop (Variant)

A less common but recognized pattern:

```c
for (int i = threadIdx.x; i < N; i += blockDim.x) { ... }
```

Produces: `{threadIdx.x, +, blockDim.x}<loop>`. The step is `SCEVUnknown(blockDim.x)` with range `[1, 1024]`. The trip count is `udiv(N - threadIdx.x + blockDim.x - 1, blockDim.x)` -- symbolic but bounded. This pattern is common in reduction kernels and shared-memory tiling.

#### Aggressive Positive Stride Analysis

The NVIDIA-specific knob `aggressive-positive-stride-analysis` (see nvbug 3972412) enables additional reasoning about stride signs. When enabled, the SCEV range analysis assumes that strides derived from `blockDim.x`, `gridDim.x`, and `warpSize` are always positive (range `[1, ...)` rather than `[0, ...)`). This allows the loop vectorizer and LSR to prove monotonic increase of induction variables, eliminating runtime overflow checks. The knob is registered in `ctor_131_0` (constructor at `0x4E1CD0` area) and can be disabled via `-no-aggressive-positive-stride-analysis`.

The `special-reassociate-for-threadid` knob (description: "Don't move back expressions with threadid") prevents SCEV-based reassociation from hoisting `threadIdx.x` expressions out of their canonical position. Without this guard, the reassociator might combine `threadIdx.x + offset` into a form that obscures the warp/grid-stride pattern for downstream consumers.

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

**IndVarSimplify** (`sub_1945A50`): Uses SCEV to compute exit values, rewrite loop exit conditions, and perform LFTR (Linear Function Test Replace). NVIDIA adds two guards:
- `Disable-unknown-trip-iv` (registered in `ctor_203` at `0x4E1CD0`, global `qword_4FAF520`): When set, the pass is skipped entirely for loops whose trip count is `SCEVCouldNotCompute`. The check in the `run()` wrapper (`sub_19489B0`, lines 119-122) calls `sub_1CED350` (trip count query) and `sub_1CED620` (trip count for header). This protects GPU-specific loops with divergent control flow from incorrect IV transforms.
- `iv-loop-level` (default **1**, global `qword_4FAF440`): Limits IndVarSimplify to loops at nesting depth <= the configured level. `sub_193DD90` (`getLoopDepth`) returns 1 for outermost loops. The default restricts IV simplification to outermost loops only, avoiding compile-time explosion on deeply-nested GPU kernels (stencil, tensor code).

**Loop Strength Reduction** (`sub_19A87A0`): The NVIDIA custom LSR reads SCEV expressions for each loop use (base SCEV at `+0`, stride SCEV at `+8`, loop bounds at `+712`/`+720`). The formula solver generates alternatives by factoring common strides out of SCEV expressions. SCEV normalization (`sub_199D980`) provides canonical forms for hash-table keying. NVIDIA adds `disable-unknown-trip-lsr` to skip LSR entirely for unknown-trip-count loops, plus `lsr-check-rp` / `lsr-rp-limit` to gate LSR on register pressure.

**LoopInterchange** (`sub_E05-loop-interchange`): Uses SCEV stride analysis to determine which loops carry memory strides. If a subscript has stride in both inner and outer loops, it is marked "ambiguous" and interchange is blocked. For grid-stride loops, the step `blockDim.x * gridDim.x` is recognized as an outer-loop stride, allowing interchange when the array subscript depends on a single loop dimension.

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
| `aggressive-positive-stride-analysis` | (bool) | Aggressive stride sign reasoning for blockDim/gridDim/warpSize (see nvbug 3972412) |
| `special-reassociate-for-threadid` | (bool) | Prevent hoisting threadIdx expressions out of canonical position |
| `Disable-unknown-trip-iv` | (bool) | Skip IndVarSimplify for loops with `SCEVCouldNotCompute` trip count |
| `disable-unknown-trip-lsr` | (bool) | Skip Loop Strength Reduction for unknown-trip-count loops |
| `iv-loop-level` | **1** | Max loop nesting depth for IndVarSimplify (1 = outermost only) |
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

### SCEV Global Variables (Binary Addresses)

| Global | Knob String | Default | Used By |
|--------|-------------|---------|---------|
| `dword_4F88268` | `scalar-evolution-max-recursion-depth` | 100 | `getSCEV` normal mode depth counter |
| `qword_4F88348` | `scalar-evolution-max-expr-failures` | 100 | `getSCEV` simple mode failure gate |
| `dword_4F88428` | `scalar-evolution-max-expr-size` | 384 | `expressionComplexity` size threshold |
| `qword_4F88DC8` | (loop iteration bound) | -- | Exit analysis iteration limit |
| `qword_4F88EA8` | (range recursion limit) | -- | `getRangeRef` max recursion depth |

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

## Differences from Upstream LLVM

The cicc v13.0 SCEV subsystem diverges from upstream LLVM 20.0.0 `ScalarEvolution.cpp` in the following ways:

| Feature | Upstream LLVM | cicc v13.0 |
|---------|---------------|------------|
| Budget system | Single `recursion_count` depth counter | Two-stage: expression size scoring (`sub_DB3670`) + failure counting, toggled via `simple_mode` flag |
| Kernel bypass | No concept of calling convention bypass | CC 42/43 (PTX `__global__`) bypass all SCEV budgets |
| `createSCEV` | Recursive | Non-recursive worklist (`sub_DD8130`) to handle deep GPU expression trees |
| GPU builtin ranges | No thread/block index knowledge | Intrinsic IDs 312/333/339/360/369/372 inject ranges at SCEV construction time |
| PHI decomposition | Standard recurrence detection | GPU-specific path (kind 64) traces PHI chains through NVIDIA special register intrinsics |
| Delinearization | Standard dimension recovery | Polymorphic predicate collector recognizes grid-stride patterns; `step_recurrence` field enables GPU memory coalescing analysis |
| Trip count tracking | Standard | `track-trip-count-more` (default true) enables more aggressive BTC computation |
| Stride sign reasoning | Standard | `aggressive-positive-stride-analysis` assumes blockDim/gridDim/warpSize are always positive |
| Expression canonicalization | Standard | `special-reassociate-for-threadid` prevents moving threadIdx expressions |
| SCEV-CGP | Not present | Complete NVIDIA SCEV-based CodeGenPrepare pass with 11 dedicated knobs |
| Knob count | ~15 standard knobs | 15 upstream + 15 NVIDIA-specific + 11 SCEV-CGP = ~41 total SCEV knobs |

The most consequential divergence is the `simple_mode` system: it changes the compile-time complexity class of SCEV analysis from O(N * D^2) (where D is recursion depth) to O(N * S) (where S is the per-instruction size limit), making SCEV analysis tractable on large GPU kernels without sacrificing accuracy on the important inner-loop induction variables.

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
| `sub_DB3670` | `expressionComplexity` | 35KB | NVIDIA expression size scorer; self-recursive, uses `sub_CF4090` |
| `sub_CF4090` | SCEV node size counter | -- | Counts nodes in existing SCEV tree for complexity scoring |
| `sub_DB04E0` | `getSmallConstantTripCount` | 117 | Extract small constant trip count |
| `sub_1495EB0` | `classifyExpressions` / `print` | 20KB | Debug: "Classifying expressions for: " |
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
- [IndVarSimplify](../llvm/loop-passes-standard.md) -- SCEV-dependent IV transforms with `Disable-unknown-trip-iv` guard
- [SCEV-CGP (CodeGenPrepare)](./codegen-prepare.md) -- NVIDIA SCEV-based address mode optimization
- [LLVM Knobs (1,689)](../config/knobs.md) -- full knob catalog including all SCEV knobs
- [GPU Execution Model](../gpu-execution-model.md) -- why GPU kernels need special SCEV treatment
