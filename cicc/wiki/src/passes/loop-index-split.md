# Loop Index Split

`loop-index-split` is a loop transformation pass that splits or peels loops when a condition inside the loop body depends on the loop induction variable. The pass was originally part of upstream LLVM 2.x (circa 2008--2009) but was removed around LLVM 3.0 due to correctness concerns and limited applicability. NVIDIA revived and heavily modified it for CUDA workloads, where loops with index-dependent conditionals are extremely common — boundary handling in stencil computations, tile edge processing, and index-based predication are pervasive GPU kernel patterns. The NVIDIA version is substantially more sophisticated than the original, implementing three distinct transformation modes with full SCEV-based analysis.

By eliminating index-dependent branches from loop bodies, the pass reduces warp divergence on NVIDIA GPUs. When threads in a warp take different paths through a branch, the GPU must serialize both paths (predicated execution or divergent branch), wasting throughput. Splitting the loop so that each resulting loop has a uniform body eliminates this divergence entirely within the split regions, restoring full SIMT efficiency.

## Pipeline Position

| Field | Value |
|---|---|
| Pass name (pipeline) | `loop-index-split` |
| Display name | `Index Split Loops` |
| Pass type | LoopPass (NVIDIA-custom, revived from LLVM 2.x) |
| Class | `llvm::LoopIndexSplitPass` |
| Legacy PM registration | `sub_1C76080` |
| New PM registration | `sub_2CBEC60` |
| Pass ID | `dword_4FBD4A8` / `unk_4FBD4AC` |
| New PM vtable | `off_4A25510` |

## Transformation Modes

The pass implements three transformation strategies, attempted in priority order. When the first applicable transformation is found, it is applied and the pass moves on.

### Mode A: All-But-One Iteration Peel (`processAllButOneIterationLoop`)

**When:** The loop body contains a condition that is true for all iterations except exactly one (typically `i == K` for a constant `K`).

**What:** The pass peels the single exceptional iteration out of the loop and removes the condition from the remaining iterations.

**Before:**
```c
for (i = 0; i < N; i++) {
    if (i == K) special();
    else normal();
}
```

**After:**
```c
for (i = 0; i < K; i++) normal();
special();
for (i = K+1; i < N; i++) normal();
```

This eliminates the branch from both resulting loops entirely. On a GPU, this means warps executing the pre-K or post-K loops never diverge on this condition.

**Implementation:** `sub_2CC3FF0` (2.7 KB native, New PM) / part of `sub_1C77080` (10 KB native, Legacy PM).

### Mode B: Only-One-Iteration Collapse (`processOnlyOneIterationLoop`)

**When:** The condition is true for exactly one iteration, and the loop body does nothing useful on other iterations.

**What:** The pass replaces the entire loop with a guarded single execution of the body.

**Before:**
```c
for (i = 0; i < N; i++) {
    if (i == K) doWork();
}
```

**After:**
```c
if (K >= 0 && K < N) doWork();
```

This transforms an O(N) loop into O(1) code — a dramatic optimization when the original loop's only purpose was to find and execute a single iteration.

**Implementation:** `sub_2CC4A70` (3.7 KB native, New PM) / part of `sub_1C77080` (10 KB native, Legacy PM).

### Mode C: Range Split (`processSplitRangeLoop`)

**When:** The condition splits the iteration space into two contiguous ranges (e.g., `i < M` vs `i >= M`).

**What:** The pass splits the loop at the boundary point so each resulting loop has a simpler, branch-free body.

**Before:**
```c
for (i = 0; i < N; i++) {
    if (i < M) a(); else b();
}
```

**After:**
```c
for (i = 0; i < min(M, N); i++) a();
for (i = M; i < N; i++) b();
```

This is the most common transformation for GPU boundary handling code, where the first/last few iterations of a tile perform padding or clamping.

**Implementation:** `sub_2CC5900` (11 KB native, New PM) / `sub_1C7B2C0` (11 KB native, Legacy PM). The loop cloning and rewiring logic is in `sub_2CC1B10` (9 KB native), with split point computation in `sub_2CC0040` (0.5 KB native) and `sub_2CC0CC0` (1.5 KB native).

## Algorithm Detail

The main driver (`sub_2CC5900`, 11 KB native) proceeds as follows:

1. **Verify loop structure:** The loop must have exactly one exit, a preheader, a latch block, and an identifiable header.
2. **Initialize SCEV analysis:** Obtains the ScalarEvolution result for the loop to identify the induction variable and compute trip counts.
3. **Find the induction variable and exit condition** from the loop's back-edge.
4. **Scan the loop body** for `ICmp` or `Select` instructions that compare the IV against a loop-invariant value.
5. **Validate the comparison** uses constant integer bounds (checked via `APInt` extraction at multiple points).
6. **Safety checks** (lines 760--830 of `sub_2CC5900`):
   - Iterate all loop BBs, checking each instruction:
     - Opcode 85 (Call): reject if callee may have side effects
     - Opcodes 34--85: checked against bitmask `0x8000000000041` for safe operations
     - Store instructions: checked for non-interference with the split
   - No volatile loads permitted
   - No memory operations that prevent reordering
7. **Determine which transformation applies:**
   - Try `processAllButOneIterationLoop` first
   - Try `processOnlyOneIterationLoop` second
   - Fall back to `processSplitRangeLoop`
8. **For range splits:** Compute the split point, clone the loop (including all basic blocks, PHI nodes, and branch conditions), adjust iteration bounds, and rewire predecessors/successors.

### Comparison Classifiers

Four small functions classify how the `ICmp` operands relate to the induction variable:

| Function | Purpose |
|---|---|
| `sub_2CBED80` | Determine which operand is the IV |
| `sub_2CBED00` | Determine which operand is the bound |
| `sub_2CBEE00` | Classify comparison direction (ascending/descending) |
| `sub_2CBEE80` | Extended classification for range splits |

### Legality Validation

| Function | Size | Purpose |
|---|---|---|
| `sub_2CBFC80` | — | Validate split is legal (check exit conditions) |
| `sub_2CBF770` | — | Validate loop structure for splitting |
| `sub_2CBF180` | — | Create new loop preheader for split result |

## Diagnostic Strings

Diagnostic strings recovered from `p2b.4-5-sinking2-loopindexsplit.txt`. The pass emits optimization remarks via the standard LLVM `OptimizationRemark` system.

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"LoopIndexSplit: performed processAllButOneIterationLoop"` | `sub_2CC3FF0` (New PM) / `sub_1C77080` (Legacy PM) | Remark | Mode A transformation applied: single exceptional iteration peeled |
| `"LoopIndexSplit: performed processOnlyOneIterationLoop"` | `sub_2CC4A70` (New PM) / `sub_1C77080` (Legacy PM) | Remark | Mode B transformation applied: entire loop replaced with guarded single body |
| `"LoopIndexSplit: performed processSplitRangeLoop"` | `sub_2CC5900` (New PM) / `sub_1C7B2C0` (Legacy PM) | Remark | Mode C transformation applied: loop split at range boundary |
| `"Index Split Loops"` | `sub_1C76080` / `sub_2CBEC60` | Registration | Display name used in both Legacy PM and New PM pass registration |
| `"loop-index-split"` | Pipeline parser (`sub_2377300` line 3768, `sub_2368220` line 5081) | Registration | Pipeline ID string (16 characters) |
| `"LoopSplitIndex"` / `"LoopIndexSplit"` | Remark infrastructure | Remark tag | Optimization remark tag names (both variants observed in binary) |

## Configuration Knobs

No dedicated `cl::opt` knobs were found for LoopIndexSplit. The pass is enabled or disabled at the pipeline level via the pass name `loop-index-split` in the pipeline string or by including/excluding it during pipeline assembly. It can also be controlled by the global `pass-control` and `disable-passno` mechanisms.

## Analysis Dependencies

| Legacy PM | New PM | Purpose |
|---|---|---|
| `DominatorTreeWrapperPass` (`sub_15CD350`) | `DominatorTreeAnalysis` (`sub_D4AA90`) | Dominance checks for loop cloning |
| `LoopInfoWrapperPass` (`sub_13FBE20`) | `LoopAnalysis` (`sub_B1A2E0`) | Loop structure and nesting |
| `ScalarEvolutionWrapperPass` (`sub_1AE1AE0`) | `ScalarEvolutionAnalysis` (`sub_11CDF60`) | IV identification, trip count, range proofs |
| `LoopAccessAnalysis` (`sub_1AF93A0`) | `LoopAccessAnalysis` (`sub_F67EE0`) | Memory dependence in loops |

SCEV is the critical dependency: it provides induction variable identification, trip count computation, and the mathematical proofs needed to establish that split points are correct and that bounds do not overflow.

## Pass Object Layout

**Legacy PM:** 80-byte pass descriptor.

**New PM:** 176-byte pass object with embedded worklists and float thresholds. Key fields during execution:

| Offset (QWORDs) | Content |
|---|---|
| 0 | Vtable / loop pointer |
| 1--3 | Sub-loop tracking |
| 4 | Sinkable instruction count |
| 5 | Exit condition block |
| 6 | Split condition (`ICmp`/`FCmp` instruction) |
| 7 | Loop bound (lower) |
| 8 | Loop bound (upper) |
| 9 | Split instruction |
| 10 | Instruction counter / worklist |
| 11--13 | `DenseSet` for tracking visited blocks |
| 14 | Iteration counter |
| 18--24 | Computed values (preheader, header, latch, exitBB, etc.) |
| 25 | SCEV analysis result pointer |
| 26 | New loop blocks array (for split range) |

## Function Map

### New PM Implementation

| Function | Address | Size | Role |
|---|---|---|---|
| — | `0x2CBEC60` | — | New PM pass registration |
| — | `0x2CBFF20` | — | New PM factory |
| — | `0x2CC3FF0` | 13KB | `processAllButOneIterationLoop` (Mode A) |
| — | `0x2CC4A70` | 19KB | `processOnlyOneIterationLoop` (Mode B) |
| — | `0x2CC5900` | 68KB | Main driver + `processSplitRangeLoop` (Mode C) |
| — | `0x2CC1B10` | 42KB | Loop cloning and CFG rewiring |
| — | `0x2CC0040` | 7KB | Split boundary computation |
| — | `0x2CC0CC0` | 7KB | Alternate split boundary computation |
| — | `0x2CC9AA0` | 18KB | Helper |
| — | `0x2CCB3B0` | 25KB | Helper |
| — | `0x2CCCE20` | 13KB | Helper |
| — | `0x2CCDD70` | 15KB | Helper |
| — | `0x2CCED30` | 8KB | Helper |
| — | `0x2CCF450` | 57KB | Large helper / alternate path |
| — | `0x2CBED80` | — | Comparison classifier (IV operand) |
| — | `0x2CBED00` | — | Comparison classifier (bound operand) |
| — | `0x2CBEE00` | — | Comparison direction classifier |
| — | `0x2CBEE80` | — | Extended comparison classifier |
| — | `0x2CBFC80` | — | Split legality validation |
| — | `0x2CBF770` | — | Loop structure validation |
| — | `0x2CBF180` | — | Create new preheader |

### Legacy PM Implementation

| Function | Address | Size | Role |
|---|---|---|---|
| — | `0x1C76080` | — | Legacy PM pass registration |
| — | `0x1C76180` | — | Legacy PM factory |
| — | `0x1C76260` | — | Alternate factory |
| — | `0x1C76340` | 7KB | Hash table management for visited set |
| — | `0x1C768C0` | 4KB | Helper |
| — | `0x1C76B50` | 4KB | Block cloning helper |
| — | `0x1C76EB0` | 2.5KB | Recursive loop tree walker |
| — | `0x1C77080` | 46KB | `processAllButOneIterationLoop` + `processOnlyOneIterationLoop` |
| — | `0x1C797A0` | 15KB | Split legality checking |
| — | `0x1C7A300` | 21KB | Loop body cloning |
| — | `0x1C7B2C0` | 84KB | `processSplitRangeLoop` + main driver |

**Total code size:** ~180KB (Legacy PM) + ~260KB (New PM) = ~440KB. This is one of the largest individual passes in cicc.

## GPU-Specific Motivation

Index-dependent conditionals inside loops are ubiquitous in GPU kernels:

- **Boundary handling:** Threads at tile edges must check whether their index falls within the valid data range, leading to `if (threadIdx.x + blockIdx.x * blockDim.x < N)` patterns inside processing loops.
- **Stencil codes:** Halo region processing requires different behavior for the first and last few iterations of a tile.
- **Reduction patterns:** The final iteration of a reduction loop often has special aggregation logic.
- **Predicated execution:** CUDA warp-level programming frequently uses index-based predicates to assign work to specific lanes.

Each of these patterns introduces a branch that causes **warp divergence**: threads in the same warp take different paths, forcing the GPU to serialize both sides. By splitting the loop at the index boundary, the pass ensures that within each resulting loop, all threads in a warp execute the same path. This eliminates divergence entirely within the split regions, recovering full SIMT throughput.

The pass's large code size (~440KB) reflects the complexity of correct loop cloning on GPU IR, where PHI nodes, memory dependencies, and SCEV invariants must all be preserved across the transformation.
