# Standard Loop Passes

CICC v13.0 includes a full complement of LLVM loop transformation passes beyond the major ones (LoopVectorize, LoopUnroll, LICM, LSR) that have their own pages. This page covers the remaining loop passes: LoopInterchange, IRCE, IndVarSimplify, LoopDistribute, LoopIdiom, LoopRotate, LoopSimplify, and LCSSA. Most are stock LLVM with default thresholds, but IndVarSimplify carries three NVIDIA-specific knobs that materially change behavior on GPU code. LoopRotate appears multiple times in the pipeline as a canonicalization prerequisite for LICM and unrolling. The canonicalization trio -- LoopSimplify, LCSSA, and LoopRotate -- run so frequently they constitute the backbone of loop pass infrastructure in cicc.

---

## LoopInterchange

Swaps the iteration order of a perfectly-nested loop pair to improve memory access locality. On GPUs, interchange can convert non-coalesced global memory accesses (strided across warps) into coalesced ones (consecutive addresses per warp), which is often the single largest performance lever for memory-bound kernels.

| Property | Value |
|---|---|
| Entry point | `sub_1979A90` (69 KB) -- `processLoopList` |
| Legality checker | `sub_1975210` (45 KB) |
| Dependence helper | `sub_1978000` (37 KB) |
| Pass name | `"loop-interchange"` |
| Knob | `loop-interchange-threshold` at `dword_4FB07E0`, default **0** |
| NVIDIA delta | **None** -- stock LLVM algorithm and threshold |

**Algorithm.** The pass collects the loop nest as a SmallVector by walking the single-subloop chain (enforcing the "perfectly nested" constraint -- each loop must have exactly one child). For nests with fewer than two levels, it returns immediately. It then builds direction vectors for every memory-dependence pair via `DependenceInfo` (`sub_13B1040`), encoding each dimension as one of `<` (forward), `>` (backward), `=` (equal), `S` (scalar), `I` (independent), or `*` (unknown). A hard bail-out fires if the number of dependence pairs exceeds 100 (`0x960` bytes at 24 bytes per entry) -- a compile-time safety valve.

For each candidate pair from outermost inward, the decision pipeline runs five checks in sequence:

1. **Dependence safety** -- any `*` or backward-carried dependence that would be reversed by interchange bails with remark `"Dependence"`.
2. **Call instructions** -- calls in the inner body that are not provably readonly intrinsics bail with `"CallInst"`.
3. **Tight nesting** -- extra computation between the loops (non-PHI, non-terminator instructions) bails with `"NotTightlyNested"`.
4. **Exit PHI validation** -- complex PHI nodes at the loop exit bail with `"UnsupportedExitPHI"`.
5. **Cost model** -- counts memory subscripts with stride in the inner vs. outer loop. Net cost = `benefit - penalty`. Interchange proceeds only if `cost >= -threshold` (default: `>= 0`) AND all direction vectors show a parallelism improvement (outer dimension becomes scalar/independent while inner becomes equal).

After interchange, the pass swaps direction-vector columns and loop-list positions, then tries the next pair inward.

**GPU considerations.** The cost model counts memory accesses generically via SCEV stride analysis. There is no visible special handling for address spaces (shared vs. global vs. texture). The standard "stride-1 is good" locality model applies uniformly. For a reimplementation targeting GPUs, you would want to weight global-memory accesses far more heavily than shared-memory accesses, since shared memory has no coalescing requirement.

---

## IRCE (Inductive Range Check Elimination)

Splits a loop into pre/main/post regions so that inductive range checks (bounds checks on the induction variable) can be eliminated from the main loop body, which executes the vast majority of iterations.

| Property | Value |
|---|---|
| Entry point | `sub_194D450` (71 KB) -- `InductiveRangeCheckElimination::run` |
| Pass name | `"irce"` |
| Block threshold | `dword_4FB0000` -- max basic blocks before bail-out |
| Debug flag | `byte_4FAFE40` -- prints `"irce: looking at loop"` |
| Constrained mode | `byte_4FAFBA0` -- relaxes canonical-form requirements |
| SCEV verify | `byte_4FAFC80` -- post-transform range verification |
| Metadata flag | `byte_4FAFF20` -- propagate `"irce.loop.clone"` metadata |
| NVIDIA delta | **Minimal** -- stock algorithm, "constrained" mode may help GPU strided patterns |

**Algorithm.** IRCE scans conditional branches in the loop body for ICmp instructions comparing the induction variable against loop-invariant bounds. Each candidate is classified into one of four kinds:

```
RANGE_CHECK_UNKNOWN = 0   (skip)
RANGE_CHECK_LOWER   = 1   (indvar >= lower_bound)
RANGE_CHECK_UPPER   = 2   (indvar < upper_bound)
RANGE_CHECK_BOTH    = 3   (lower <= indvar < upper)
```

The `InductiveRangeCheck` structure is 40 bytes (`0x28`): `Begin` (SCEV), `Step` (SCEV), `End` (SCEV), `CheckUse` (Use\*), `Operand` (Value\*), `Kind` (uint32). After validation (constant step, loop-invariant bounds, simplify form, computable trip count), IRCE computes the safe iteration range `[safe_begin, safe_end)` using SCEV and clones the loop into three copies:

- **Preloop**: iterations `[0, safe_begin)` -- original range check present.
- **Mainloop**: iterations `[safe_begin, safe_end)` -- range check **eliminated**.
- **Postloop**: iterations `[safe_end, trip_count)` -- original range check present.

For `RANGE_CHECK_BOTH` (kind=3), the pass creates two separate cloning operations, producing three loop clones total with both bounds eliminated from the center.

The "constrained" relaxation flag (`byte_4FAFBA0`) allows IRCE to handle range checks where the induction variable relationship is slightly non-canonical -- useful for GPU thread-coarsened loops with strided access patterns.

**Pipeline position.** IRCE runs after LoopSimplify and before LoopUnroll. It consumes canonicalized induction variables produced by IndVarSimplify and feeds into vectorization by removing bounds checks that would otherwise prevent LoopVectorize.

---

## IndVarSimplify

Canonicalizes induction variables: simplifies IV users, performs Linear Function Test Replace (LFTR), replaces exit values with closed-form SCEV expressions, and sinks dead IV computations. This is the pass with the most significant NVIDIA modifications in this group.

| Property | Value |
|---|---|
| Core function | `sub_1945A50` (65 KB) -- `IndVarSimplify::run` |
| NewPM wrapper | `sub_19489B0` -- applies NVIDIA guards before core |
| Pass name | `"indvars"` |
| NVIDIA knob 1 | `Disable-unknown-trip-iv` at `qword_4FAF520` -- skip pass for unknown-trip loops |
| NVIDIA knob 2 | `iv-loop-level` at `qword_4FAF440`, default **1** -- max nesting depth |
| NVIDIA knob 3 | `disable-lftr` at `byte_4FAF6A0` -- disable LFTR entirely |
| Upstream knob | `replexitval` at `dword_4FAF860` -- `{never, cheap, always}` |
| NVIDIA delta | **Significant** -- two custom guard knobs plus depth limiter |

**NVIDIA guards.** Before the core algorithm runs, `sub_19489B0` checks two NVIDIA-specific conditions:

1. **Loop depth gate** (`iv-loop-level`): if `getLoopDepth(loop) > qword_4FAF440[20]`, the pass is skipped entirely. Default 1 means only outermost loops receive IV simplification. This controls compile time on deeply-nested stencil and tensor kernels.

2. **Unknown trip count gate** (`Disable-unknown-trip-iv`): if enabled and the loop's trip count is not statically computable by SCEV (`sub_1CED350` returns <= 1 or `sub_1CED620` fails for the header), the pass is skipped. This protects GPU kernels with divergent or dynamic bounds from aggressive IV transforms that can cause correctness issues with warp-level scheduling assumptions.

**Core algorithm (five phases):**

1. **Header PHI collection** -- walks the loop header's instruction list, collecting all PHI nodes (opcode 77) as candidate induction variables.

2. **Per-IV rewriting** -- for each PHI, calls `SimplifyIndVar::simplifyIVUsers` (`sub_1B649E0`) to fold truncs/sexts/zexts, fold comparisons with known ranges, and eliminate redundant increment chains. Then calls `rewriteLoopExitValues` (`sub_1943460`) to replace uses of the IV outside the loop with closed-form SCEV expressions. New PHIs discovered during rewriting are pushed back to the worklist for fixpoint iteration.

3. **LFTR (Linear Function Test Replace)** -- gated by `replexitval != "never"`, `!disable-lftr`, `hasCongruousExitingBlock`, and `exitValueSafeToExpand`. Selects the best IV for the loop exit test (preferring non-sign-extending, wider IVs with higher SCEV complexity). Computes a wide trip count via `sub_1940670`, then rewrites the exit condition to compare the selected IV against this trip count. Creates named instructions: `"wide.trip.count"`, `"lftr.wideiv"`, `"exitcond"`.

4. **Exit value replacement** -- materializes closed-form exit values via SCEVExpander. The "cheap" mode (`replexitval=1`) adds a cost gate so only inexpensive expansions proceed.

5. **Cleanup** -- dead instruction removal, IV computation sinking past the loop exit, PHI predecessor fixup, and `deleteDeadPhis` on the loop header.

**GPU relevance.** The depth limiter is important because CUDA stencil codes often have 3-5 nested loops, and running IndVarSimplify on inner loops can blow up compile time without meaningful benefit (inner loops typically have simple IVs already). The unknown-trip guard prevents miscompiles on kernels where the trip count depends on `threadIdx` or `blockIdx`.

---

## LoopDistribute

Splits a single loop into multiple loops (loop fission), each containing a subset of the original instructions. The primary motivation is separating memory accesses with unsafe dependences from safe ones, enabling LoopVectorize to vectorize the safe partition.

| Property | Value |
|---|---|
| Entry point | `sub_1A8CD80` (63 KB) -- `LoopDistributePass::run` |
| Pass name | `"loop-distribute"` |
| Force flag | `byte_4FB5360` -- force distribution ignoring metadata |
| SCEV check threshold | `qword_4FB5480` -- max runtime checks before bail-out |
| Verify flag | `byte_4FB56E0` -- post-distribution verification |
| NVIDIA delta | **None** -- stock LLVM algorithm |

**Algorithm.** The pass runs a gauntlet of six bail-out conditions per loop: not in simplify form (`"NotLoopSimplifyForm"`), multiple exit blocks (`"MultipleExitBlocks"`), `"llvm.loop.distribute.enable"` metadata disabled, no unsafe dependences (`"NoUnsafeDeps"`), all memory ops already vectorizable (`"MemOpsCanBeVectorized"`), or too many SCEV runtime checks (`"TooManySCEVRuntimeChecks"`).

If validation passes, the core phase builds a partition graph using LoopAccessInfo (LAI). Each instruction starts in its own partition. For each unsafe memory dependence pair, the pass either merges the source and destination partitions (if the dependence cannot be broken) or marks it as cross-partition. A union-find structure tracks merged partitions. After merging, if at least two distinct partitions remain, the pass clones the loop body once per partition, removes instructions not belonging to each partition, wires the clones in dependence order, and optionally adds runtime dependence checks (loop versioning).

The partition hash set uses LLVM's standard DenseMap with pointer hash `(ptr >> 4) ^ (ptr >> 9)`, 16-byte entries, and 3/4 load factor growth.

**GPU relevance.** Distribution is valuable for CUDA kernels that mix shared-memory and global-memory accesses in the same loop -- the shared-memory partition can often be vectorized independently. The `"llvm.loop.distribute.enable"` metadata is controllable via `#pragma clang loop distribute(enable)`.

---

## LoopIdiom

Recognizes loop patterns that correspond to standard library calls (memset, memcpy, memcmp, strstr) and replaces them with optimized implementations. CICC includes both the standard LoopIdiomRecognize pass and the newer LoopIdiomVectorize pass.

| Property | Value |
|---|---|
| Recognizer core | `sub_196FF90` (51 KB) -- `LoopIdiomRecognize::run` |
| Memset detection | `sub_196B740` (10 KB) -- detects `memset_pattern16` |
| Memcpy/memmove | `sub_196E000` (43 KB) |
| Mismatch expansion | `sub_2AA00B0` (48 KB) -- `expandMemCmpMismatch` |
| String search expansion | `sub_2AA3190` (40 KB) -- `expandFindFirst` |
| Pass name | `"loop-idiom"` |
| Vectorize knobs | `disable-loop-idiom-vectorize-all`, `loop-idiom-vectorize-style` (masked/predicated), `loop-idiom-vectorize-bytecmp-vf`, etc. |
| NVIDIA delta | **None visible** -- stock LLVM |

**Standard idioms.** The recognizer scans loops for store patterns that correspond to memset (constant value stored on every iteration) and memcpy/memmove (load-store pairs with matching strides). It also detects trip-count-decrement patterns (`"tcphi"`, `"tcdec"`) used in hand-written copy loops. Recognized patterns are lowered to `@llvm.memset` / `@llvm.memcpy` / `@llvm.memmove` intrinsics.

**Vectorized idiom expansion.** The expansion functions generate multi-block IR with vectorized comparison loops. `expandMemCmpMismatch` creates a two-tier structure: a vector loop with page-boundary safety guards, falling back to a scalar byte-by-byte comparison. Basic blocks include `"mismatch_vec_loop"`, `"mismatch_loop"`, and `"byte.compare"`. `expandFindFirst` implements vectorized first-occurrence search (strstr-like), splatting `needle[0]` across vector lanes for parallel comparison, then verifying full needle matches at candidate positions.

Both expansions use the same page-boundary safety protocol: `PtrToInt` -> `LShr` by `log2(pagesize)` -> `ICmpNE` of start/end pages. If pointers stay within a single page, wider-than-element vector loads are safe; otherwise, `@llvm.masked.load` provides the fallback.

**GPU considerations.** LoopIdiom is present in cicc but its value on GPU code is limited -- GPU memset/memcpy are typically handled by device runtime calls or specialized PTX instructions rather than loop-based patterns. The vectorized mismatch/search expansions target CPU-style byte-level operations that are rare in GPU kernels. The pass runs but likely fires infrequently.

---

## LoopRotate

Transforms loops so that the latch block (back-edge source) becomes the exiting block (where the exit condition is tested). This converts "while" loops into "do-while" form, which is a prerequisite for LICM (the loop body is guaranteed to execute at least once, enabling unconditional hoisting) and simplifies trip count computation for SCEV.

| Property | Value |
|---|---|
| Entry point (legacy) | `sub_18A3090` -- called directly in O1/O2/O3 pipeline |
| Entry point (new PM) | `sub_28448D0` -- `LoopRotatePass` with `"header-duplication;"` param |
| Core implementation | `sub_2A0CFD0` (65 KB) -- `LoopRotation::runOnLoop` |
| String markers | `".lr.ph"` (preheader), `"h.rot"`, `"pre.rot"` |
| Pass name | `"loop-rotate"` |
| Params | `no-header-duplication` / `header-duplication` |
| Pipeline knob | `enable-loop-header-duplication` (bool) -- controls default param |
| NVIDIA delta | **None** -- stock LLVM, but appears **multiple times** in pipeline |

**Pipeline placement.** LoopRotate appears at least twice in the standard cicc O1+ pipeline:

1. **Position 11** in `sub_12DE330`: `sub_18A3090()` -- runs before LICM (`sub_184CD60`) and IndVarSimplify.
2. **Tier 1 passes**: appears again alongside SimplifyCFG and InstCombine as part of the canonicalization loop.

This double invocation is standard LLVM practice -- rotation may be needed again after other transforms invalidate the rotated form.

**Algorithm.** The pass duplicates the loop header into the preheader (creating a "rotated" header named `"h.rot"` or `"pre.rot"`), then rewires the CFG so the original header becomes the latch. The `header-duplication` parameter controls whether the header is actually duplicated (which increases code size) or only the branch is restructured. After rotation, SCEV's backedge-taken count computation becomes straightforward because the exit test is at the latch.

---

## LoopSimplify

Enforces LLVM's canonical loop form: single preheader, single latch, single dedicated exit block, and no abnormal edges. Nearly every loop optimization pass requires simplify form as a precondition.

| Property | Value |
|---|---|
| Canonicalization core | `sub_1A5B3D0` (62 KB) |
| DomTree update helper | `sub_1A593E0` (47 KB) |
| Preheader insertion | `sub_1A5E350` (25 KB) |
| Exit block normalization | `sub_1A5F590` (42 KB) |
| Pass name | `"loop-simplify"` |
| String markers | `".backedge"`, `"llvm.loop"` |
| Pipeline wrapper | `sub_1832270(n)` where n = verify flag; also `sub_1841180()` |
| NVIDIA delta | **None** -- stock LLVM |

**Pipeline placement.** LoopSimplify appears in the pipeline both as a standalone pass (`sub_1832270`) and bundled with LCSSA (`sub_1841180`). In the "mid" fast-compile path, it appears explicitly at position 11 before the CGSCC framework. In the full O1+ pipeline, it runs as pass 40 (`sub_1841180`), paired with LCSSA formation. It also runs as a utility called by other loop passes -- LoopInterchange, LoopDistribute, IRCE, and LoopVectorize all check `isLoopSimplifyForm()` (`sub_157F0D0`) and bail out if it fails.

**What it does.** If a loop lacks a single preheader, LoopSimplify creates one by inserting a new basic block on the entry edge (named with `.lr.ph` suffix). If multiple latch blocks exist, it merges them into one (inserting `.backedge` blocks). If exit blocks are shared with other loops, it creates dedicated exit blocks. After transformation, loop metadata (`"llvm.loop"` nodes) is preserved on the new latch terminator.

---

## LCSSA (Loop-Closed SSA)

Ensures that every value defined inside a loop and used outside it passes through a PHI node at the loop exit. This invariant simplifies SSA-based transformations: passes can modify loop internals without worrying about breaking uses outside the loop.

| Property | Value |
|---|---|
| Formation pass | `sub_1AE2630` (49 KB) |
| Lightweight form | `sub_1961B00` (13 KB) -- creates `.lcssa` PHI nodes |
| LCSSA updater | `sub_1AF8F90` -- used by LoopInterchange post-transformation |
| Pass name | `"lcssa"` |
| Verify knob | `verify-loop-lcssa` registered at `ctor_094` |
| String markers | `".lcssa"` suffix on PHI node names |
| NVIDIA delta | **None** -- stock LLVM |

**Pipeline placement.** LCSSA runs bundled with LoopSimplify via `sub_1841180()` at position 40 in the full pipeline. It is also maintained incrementally by every pass that modifies loop structure -- LoopInterchange calls `sub_1AF8F90` to update LCSSA form for both inner and outer loops after transformation, and both LoopIdiom expansions (`sub_2AA00B0`, `sub_2AA3190`) end with an explicit `verifyLoopLCSSA` assertion (`"Loops must remain in LCSSA form!"`).

**What it does.** For each instruction defined inside the loop, LCSSA checks all uses outside the loop's exit blocks. For each such use, it inserts a PHI node in the exit block with the defined value as the incoming value from the latch. The PHI node is named with a `.lcssa` suffix. After LCSSA formation, all external uses of loop-internal values go through these PHI nodes, and loop transforms only need to update the PHI nodes rather than chasing all external uses.

---

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_1945A50` | 65 KB | `IndVarSimplify::run` (core) |
| `sub_19489B0` | -- | `IndVarSimplifyPass::run` (NewPM wrapper with NVIDIA guards) |
| `sub_1943460` | -- | `rewriteLoopExitValues` |
| `sub_1941790` | -- | `replaceExitValuesWithCompute` (LFTR commit) |
| `sub_1940670` | -- | `computeWideTripCount` |
| `sub_193E1A0` | -- | `hasCongruousExitingBlock` |
| `sub_193DD90` | -- | `getLoopDepth` (recursive, 1 for outermost) |
| `sub_1979A90` | 69 KB | `LoopInterchange::processLoopList` |
| `sub_1975210` | 45 KB | `LoopInterchange` legality checker |
| `sub_1978000` | 37 KB | `LoopInterchange` dependence analysis helper |
| `sub_194D450` | 71 KB | `InductiveRangeCheckElimination::run` |
| `sub_194C320` | -- | `createPreLoop` / `cloneLoopForRange` |
| `sub_194AE30` | -- | `createPostLoop` / `wirePostLoop` |
| `sub_1949EA0` | -- | `classifyRangeCheckICmp` |
| `sub_1A8CD80` | 63 KB | `LoopDistributePass::run` |
| `sub_1B1E040` | -- | `distributeLoopBody` (core fission engine) |
| `sub_196FF90` | 51 KB | `LoopIdiomRecognize::run` |
| `sub_196B740` | 10 KB | LoopIdiom memset pattern detection |
| `sub_196E000` | 43 KB | LoopIdiom memcpy/memmove patterns |
| `sub_2AA00B0` | 48 KB | `expandMemCmpMismatch` |
| `sub_2AA3190` | 40 KB | `expandFindFirst` (string search vectorization) |
| `sub_2A0CFD0` | 65 KB | `LoopRotation::runOnLoop` |
| `sub_28448D0` | -- | `LoopRotatePass` (NewPM, `"header-duplication;"`) |
| `sub_18A3090` | -- | `LoopRotate` (legacy pipeline call) |
| `sub_1A5B3D0` | 62 KB | `LoopSimplify` canonical form enforcement |
| `sub_1A5E350` | 25 KB | LoopSimplify preheader insertion |
| `sub_1A5F590` | 42 KB | LoopSimplify exit block normalization |
| `sub_1832270` | -- | `LoopSimplify` pipeline wrapper (with verify flag) |
| `sub_1841180` | -- | `LoopSimplify + LCSSA` bundled pass |
| `sub_1AE2630` | 49 KB | LCSSA formation pass |
| `sub_1961B00` | 13 KB | LCSSA lightweight `.lcssa` PHI insertion |
| `sub_1AF8F90` | -- | LCSSA form updater (used post-interchange) |

---

## Cross-References

- **[LoopVectorize & VPlan](./loop-vectorize.md)** -- LoopDistribute feeds vectorization; IRCE removes bounds checks that block it.
- **[Loop Unrolling](./loop-unroll.md)** -- Runs after IndVarSimplify canonicalizes IVs; requires LoopSimplify form.
- **[LICM](./licm-real.md)** -- Requires LoopRotate and LoopSimplify as prerequisites.
- **[ScalarEvolution](./scev.md)** -- IndVarSimplify and IRCE are among the heaviest SCEV consumers; LoopInterchange uses SCEV for stride analysis.
- **[SCEV Invalidation](./scev-invalidation.md)** -- LoopRotate and LoopDistribute call `ScalarEvolution::forgetLoop` after transformation.
- **[Loop Strength Reduction](./lsr.md)** -- Runs after IndVarSimplify; consumes the canonicalized IV forms it produces.
- **[Pipeline & Ordering](./pipeline.md)** -- LoopRotate at position 11, LoopSimplify/LCSSA at position 40 in the full O1+ pipeline.
