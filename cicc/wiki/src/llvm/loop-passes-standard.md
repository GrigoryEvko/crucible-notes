# Standard Loop Passes

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

CICC v13.0 includes a full complement of LLVM loop transformation passes beyond the major ones (LoopVectorize, LoopUnroll, LICM, LSR) that have their own pages. This page covers the remaining loop passes: LoopInterchange, IRCE, IndVarSimplify, LoopDistribute, LoopIdiom, LoopRotate, LoopSimplify, and LCSSA. Most are stock LLVM with default thresholds, but IndVarSimplify carries three NVIDIA-specific knobs that materially change behavior on GPU code. LoopRotate appears multiple times in the pipeline as a canonicalization prerequisite for LICM and unrolling. The canonicalization trio -- LoopSimplify, LCSSA, and LoopRotate -- run so frequently they constitute the backbone of loop pass infrastructure in cicc.

**Barrier awareness.** None of these 8 passes have explicit barrier (`__syncthreads()`) awareness. Barrier handling in cicc occurs through dedicated NVIDIA passes: Dead Barrier Elimination (`sub_2C83D20`) and convergence control token verification (`sub_E35A10`). The structural passes (LoopRotate, LoopSimplify, LCSSA) do not move instructions across basic blocks in ways that could reorder barriers. LoopInterchange and LoopDistribute could theoretically reorder barriers, but barriers in CUDA kernels typically occur outside perfectly-nested loop bodies (interchange) or create non-distributable loop bodies (distribution).

**Occupancy interaction.** None of the 8 passes interact with occupancy or register pressure directly. Occupancy-aware loop optimization occurs in LSR (register pressure tracking at `a1+32128` with occupancy ceiling), LoopUnroll (TTI-based register pressure estimation), and register allocation. These 8 passes are IR-level transforms that run before register allocation.

**Address space awareness.** None of the 8 passes distinguish between addrspace(0) (generic), addrspace(1) (global), addrspace(3) (shared), or addrspace(5) (local). Only LSR has address space awareness via the `disable-lsr-for-sharedmem32-ptr` knob. This is a notable gap: LoopInterchange's cost model should ideally weight global memory coalescing higher than shared memory locality, and LoopDistribute could benefit from knowing that shared-memory and global-memory partitions have different cost characteristics.

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
| Knob constructor | `ctor_208` at `0x4E39E0` |
| NVIDIA delta | **None** -- stock LLVM algorithm and threshold |

**Required analyses** (from `sub_19743F0`): ScalarEvolution (`unk_4F9A488`), LoopInfoWrapperPass (`unk_4F96DB4`), DominatorTreeWrapperPass (`unk_4F9E06C`), AAResultsWrapperPass (`unk_4F9920C`), DependenceAnalysisWrapperPass (`unk_4F98D2D`), OptimizationRemarkEmitter (`unk_4FB66D8`), TargetTransformInfoWrapperPass (`unk_4FB65F4`), LoopAccessLegacyAnalysis (`unk_4F99CB0`). The pass preserves both DominatorTree and LoopInfo.

**Algorithm.** The pass collects the loop nest as a SmallVector by walking the single-subloop chain (enforcing the "perfectly nested" constraint -- each loop must have exactly one child). For nests with fewer than two levels, it returns immediately. It then builds direction vectors for every memory-dependence pair via `DependenceInfo` (`sub_13B1040`), encoding each dimension as one of `<` (forward), `>` (backward), `=` (equal), `S` (scalar), `I` (independent), or `*` (unknown). A hard bail-out fires if the number of dependence pairs exceeds 100 (`0x960` bytes at 24 bytes per entry) -- a compile-time safety valve.

For each candidate pair from outermost inward, the decision pipeline runs five checks in sequence:

1. **Dependence safety** -- any `*` or backward-carried dependence that would be reversed by interchange bails with remark `"Dependence"`. The safety check uses two bitmasks: `0x803003` for valid direction combination and `0x400801` for the "all equal-like before inner" pattern. A special case allows inner `>` when all preceding levels are `=` or `S` (zero distance in those dimensions).
2. **Call instructions** -- calls in the inner body that are not provably readonly intrinsics bail with `"CallInst"`. The intrinsic check calls `sub_1560260(callee, -1, 36)` and `sub_1560260(callee, -1, 57)` for two classes of safe intrinsics.
3. **Tight nesting** -- extra computation between the loops (non-PHI, non-terminator instructions) bails with `"NotTightlyNested"`. Checks `sub_15F3040` (extra computation), `sub_15F3330` (volatile/atomic operations), and `sub_15F2ED0` (calls with side effects).
4. **Exit PHI validation** -- complex PHI nodes at the loop exit bail with `"UnsupportedExitPHI"`. For each exit PHI, the pass walks the use chain checking operand count via `(v287 & 0xFFFFFFF)`, verifying each operand references the latch block and that `sub_157F120` (`hasLoopInvariantOperands`) returns true.
5. **Cost model** -- counts memory subscripts with stride in the inner vs. outer loop. Net cost = `benefit - penalty`. Interchange proceeds only if `cost >= -threshold` (default: `>= 0`) AND all direction vectors show a parallelism improvement (outer dimension becomes scalar/independent while inner becomes equal).

**Cost model details.** For each memory instruction (opcode byte `0x38` at offset `-8`), the pass extracts the subscript count via `(*(_DWORD*)(instr-4) & 0xFFFFFFF)` and calls `sub_146F1B0(ScalarEvolution, operand)` to get the SCEV expression. Strides are classified per-loop. Subscripts with stride in both loops are counted as penalties (ambiguous). The net cost is `locality_benefit - locality_penalty`. The parallelism override requires ALL direction vectors to have the outer dimension as `S` (83) or `I` (73) and the inner dimension as `=` (61) -- even a non-negative cost is rejected if this pattern fails, with remark `"InterchangeNotProfitable"`.

**Post-interchange bookkeeping.** After transformation, the pass: (a) calls `sub_1AF8F90` to update LCSSA form for inner loop first, then outer; (b) reruns legality check via `sub_1975210` as a safety recheck after LCSSA updates; (c) swaps direction-vector columns and loop-list positions; (d) decrements indices to try the next pair inward. The TTI availability boolean at `a1+192` (checked via `sub_1636850`) is passed to the LCSSA updater as its 4th argument, controlling rewrite aggressiveness.

**GPU considerations.** The cost model counts memory accesses generically via SCEV stride analysis. There is no visible special handling for address spaces (shared vs. global vs. texture). The standard "stride-1 is good" locality model applies uniformly. For a reimplementation targeting GPUs, you would want to weight global-memory accesses (addrspace 1) far more heavily than shared-memory accesses (addrspace 3), since shared memory has no coalescing requirement. The 100-pair dependence limit prevents the pass from even being considered for CUDA kernels with massive shared-memory access patterns (e.g., tiled matrix multiplication). The pass does not check for barriers -- perfectly-nested loops with `__syncthreads()` in the inner body would be blocked by the call-instruction check unless the barrier is lowered to an intrinsic classified as safe (which it is not).

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

**Stack frame and signature.** The function allocates ~0x960 bytes (2400 bytes) of local state. Signature: `sub_194D450(void *this_pass, void *Loop, void *LoopAnalysisManager, void *LoopStandardAnalysisResults, void *LPMUpdater)`. Returns `PreservedAnalyses` by value.

**Algorithm (8 phases).**

*Phase 1 -- Early validation.* Extracts ScalarEvolution, DominatorTree, LoopInfo, and BranchProbabilityInfo from `LoopStandardAnalysisResults`. Loads block count threshold from `dword_4FB0000` and bails if the loop exceeds it. Checks simplify form (single latch, single exit, proper preheader).

*Phase 2 -- Range check discovery.* IRCE scans conditional branches in the loop body for ICmp instructions comparing the induction variable against loop-invariant bounds. The ICmp predicate dispatch table:

| Predicate value | LLVM predicate | Range check kind |
|---|---|---|
| `0x20` (32) | SLT (signed less-than) | UPPER |
| `0x22` (34) | SGT (signed greater-than) | LOWER (swapped operands) |
| `0x24` (36) | SGE (signed greater-equal) | LOWER |
| `0x26` (38) | UGE (unsigned greater-equal) | LOWER |
| `0x28` (40) | ULT (unsigned less-than) | UPPER |

Each candidate is classified into one of four kinds:

```
RANGE_CHECK_UNKNOWN = 0   (skip)
RANGE_CHECK_LOWER   = 1   (indvar >= lower_bound)
RANGE_CHECK_UPPER   = 2   (indvar < upper_bound)
RANGE_CHECK_BOTH    = 3   (lower <= indvar < upper)
```

The `InductiveRangeCheck` structure is 40 bytes (`0x28`), iterated with stride `0x28`: `Begin` (SCEV, +0x00), `Step` (SCEV, +0x08), `End` (SCEV, +0x10), `CheckUse` (Use\*, +0x18), `Operand` (Value\*, +0x20), `Kind` (uint32, +0x24).

*Phase 3 -- Filtering and validation.* Calls `sub_1949EA0` (`classifyRangeCheckICmp`) to validate each candidate. A bitvector (allocated at `[rbp+var_460]`) tracks valid checks. The "constrained" relaxation flag (`byte_4FAFBA0`) routes to `sub_1949670` (`canHandleRangeCheckExtended`), allowing range checks where the induction variable relationship is slightly non-canonical -- useful for GPU thread-coarsened loops with strided access patterns. Validation requires: constant step (+1 or -1), loop-invariant bounds, simplify form, and SCEV-computable trip count.

*Phase 4 -- SCEV-based bound computation.* For each valid check, computes the safe iteration range `[safe_begin, safe_end)` using SCEV. Calls `sub_145CF80` (SCEV `getConstant`), `sub_147DD40` (SCEV `getAddRecExpr` / max/min), and `sub_3870CB0` (`isSafeToExpandAt`). If expansion safety fails, the check is abandoned.

*Phase 5 -- Preloop creation.* Calls `sub_194C320` (`createPreLoop`, ~1200 bytes) to clone the loop for iterations `[0, safe_begin)`. Creates basic blocks named `"preloop"` and `"exit.preloop.at"`. The clone remaps instructions and PHI nodes, creates the branch from preloop exit to mainloop entry, and updates dominator tree and loop info.

*Phase 6 -- Postloop creation.* Calls `sub_194AE30` (`createPostLoop`, ~1300 bytes) for iterations `[safe_end, trip_count)`. Calls `sub_1949270` (`adjustSCEVAfterCloning`) to refresh SCEV expressions invalidated by cloning.

*Phase 7 -- Two-path splitting for BOTH checks.* When kind=3, IRCE creates TWO separate cloning operations, producing three loop clones total. Both `sub_194C320` and a second call produce pre/main/post regions with BOTH range checks eliminated from the center.

*Phase 8 -- Cleanup.* Cleans up InductiveRangeCheck entries (stride `0x40` after alignment). If metadata flag `byte_4FAFF20` is set, propagates `"irce.loop.clone"` metadata to cloned loops via red-black tree manipulation. Releases SCEV expression references via `sub_1649B30`.

**GPU considerations.** The block count threshold (`dword_4FB0000`) protects against pathologically large GPU kernel loops from unrolled or tiled computations. The constrained relaxation mode helps with range checks in GPU kernels where induction variables use non-canonical strides (common after thread coarsening). IRCE has no barrier awareness -- if a loop body contains `__syncthreads()`, the loop cloning would duplicate the barrier into all three clones (pre/main/post), which is correct but increases code size and instruction cache pressure. The pass does not check for convergent calls, so it could clone a loop containing warp-level primitives; this is safe because all three clones execute the same iterations as the original (just partitioned differently).

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
| Upstream knob | `replexitval` at `dword_4FAF860` -- `{never=0, cheap=1, always=2}` |
| All knobs registered | `ctor_203` at `0x4E1CD0` |
| NVIDIA delta | **Significant** -- two custom guard knobs plus depth limiter |

**NVIDIA guards.** Before the core algorithm runs, `sub_19489B0` checks two NVIDIA-specific conditions:

1. **Loop depth gate** (`iv-loop-level`): if `sub_193DD90(loop) > qword_4FAF440[20]`, the pass is skipped entirely. `sub_193DD90` is a recursive `getLoopDepth()` returning 1 for outermost loops. Default 1 means only outermost loops receive IV simplification. This controls compile time on deeply-nested stencil and tensor kernels that commonly have 3-5 nested loops.

2. **Unknown trip count gate** (`Disable-unknown-trip-iv`): if `LOBYTE(qword_4FAF520[20])` is set AND (`sub_1CED350(loop) <= 1` OR `!sub_1CED620(loop, header)`), the pass is skipped. `sub_1CED350` returns the SCEV-computed trip count; values <= 1 indicate unknown or trivial loops. This protects GPU kernels with divergent or dynamic bounds (where trip count depends on `threadIdx` or `blockIdx`) from aggressive IV transforms that can cause correctness issues with warp-level scheduling assumptions.

**Core algorithm (five phases):**

1. **Header PHI collection** -- walks the loop header's instruction list via `**(a2+32)+48`, collecting all PHI nodes (opcode 77) as candidate induction variables into worklist `v342`.

2. **Per-IV rewriting** -- for each PHI, calls `sub_1B649E0` (`SimplifyIndVar::simplifyIVUsers`, via vtable at `off_49F3848`) to fold truncs/sexts/zexts, fold comparisons with known ranges, and eliminate redundant increment chains. Sets changed flag at `a1+448`. Then calls `sub_1943460` (`rewriteLoopExitValues`) to replace uses of the IV outside the loop with closed-form SCEV expressions. New PHIs discovered during rewriting are pushed back to the worklist for fixpoint iteration.

3. **LFTR (Linear Function Test Replace)** -- gated by four conditions: `dword_4FAF860 != 0` (replexitval not "never") AND trip count not constant (`!sub_14562D0`), `!byte_4FAF6A0` (disable-lftr not set), `hasCongruousExitingBlock` (`sub_193E1A0`), and `exitValueSafeToExpand` (`sub_193F280`). Selects the best IV via `sub_193E640` (`isBetterIV`) preferring non-sign-extending, wider IVs with higher SCEV complexity (`sub_1456C90`). Computes a wide trip count via `sub_1940670` (`computeWideTripCount`). Three rewriting strategies:
   - **Strategy A**: Integer IV with matching types -- computes exact exit value via APInt arithmetic, materializes as constant.
   - **Strategy B**: Type mismatch -- expands SCEV expression via `sub_14835F0` (`SCEVExpander::expandCodeFor`), creates `"wide.trip.count"` instruction using ZExt (opcode 37) or SExt (opcode 38).
   - **Strategy C**: Direction check failure -- creates `"lftr.wideiv"` as a truncation (opcode 36, Trunc) down to exit condition type.
   - Finally creates `"exitcond"` ICmp instruction (opcode 51) with computed predicate `v309 = 32 - depth_in_loop_set`.

4. **Exit value replacement** -- materializes closed-form exit values via SCEVExpander. The "cheap" mode (`replexitval=1`) adds a cost gate at `sub_1941790` where `dword_4FAF860 == 1 && !v136 && v31[24]` skips expensive expansions (v136 = simple loop flag, v31[24] = per-candidate "expensive" flag from `sub_3872990`, the SCEV expansion cost model).

5. **Cleanup** -- dead instruction removal (drains worklist at `a1+48..a1+56`, using opcode check: type <= `0x17` = LLVM scalar type), IV computation sinking (walks latch block backwards, tracks live set in red-black tree via `sub_220EF30`/`sub_220EF80`/`sub_220F040`, sinks dead IVs past loop exit via `sub_15F2240`), PHI predecessor fixup (handles Switch opcode 27 and Branch opcode 26 terminators), and `sub_1AA7010` (`deleteDeadPhis`) on the loop header.

**Additional upstream knobs present:** `indvars-post-increment-ranges` (bool, default true), `indvars-predicate-loops` (bool, default true), `indvars-widen-indvars` (bool, default true), `verify-indvars` (bool, default false).

**Pass state object layout:**

| Offset | Type | Content |
|---|---|---|
| +0 | ptr | TargetTransformInfo |
| +8 | ptr | DataLayout / Module |
| +16 | ptr | DominatorTree |
| +24 | ptr | LoopInfo |
| +32 | ptr | DeadInstVector |
| +40 | ptr | ScalarEvolution |
| +48 | ptr | DeadInstWorklist array |
| +56 | u32 | DeadInstWorklist count |
| +60 | u32 | DeadInstWorklist capacity |
| +448 | byte | Changed flag |

**GPU relevance.** The depth limiter is important because CUDA stencil codes often have 3-5 nested loops, and running IndVarSimplify on inner loops can blow up compile time without meaningful benefit (inner loops typically have simple IVs already). The unknown-trip guard prevents miscompiles on kernels where the trip count depends on `threadIdx` or `blockIdx`. The interaction with IV Demotion (`sub_1CD74B0`) is notable: IndVarSimplify runs first and may widen IVs to 64-bit, then IV Demotion (a separate NVIDIA pass) narrows them back to 32-bit where the value range permits, reducing register pressure -- a critical factor for GPU occupancy.

---

## LoopDistribute

Splits a single loop into multiple loops (loop fission), each containing a subset of the original instructions. The primary motivation is separating memory accesses with unsafe dependences from safe ones, enabling LoopVectorize to vectorize the safe partition.

| Property | Value |
|---|---|
| Entry point | `sub_1A8CD80` (63 KB) -- `LoopDistributePass::run` |
| Pass name | `"loop-distribute"` |
| Force flag | `byte_4FB5360` -- force distribution ignoring metadata |
| SCEV check threshold | `qword_4FB5480` -- max runtime checks before bail-out |
| Secondary limit | `qword_4FB53A0` -- max dependence checks per partition |
| Verify flag | `byte_4FB56E0` -- post-distribution verification |
| NVIDIA delta | **None** -- stock LLVM algorithm |

**Stack frame.** ~0x780 bytes (1920 bytes). Signature: `sub_1A8CD80(void *this_pass, void *Function, void *FunctionAnalysisManager)`.

**Algorithm.** The pass runs a gauntlet of six bail-out conditions per loop:

1. `"NotLoopSimplifyForm"` -- `sub_157F0D0` (`Loop::isLoopSimplifyForm`) fails.
2. `"MultipleExitBlocks"` -- `sub_157F0B0` (`Loop::getUniqueExitBlock`) returns null.
3. Metadata `"llvm.loop.distribute.enable"` disabled (checked via `sub_15E0530` MDNode lookup). `byte_4FB5360` (force flag) overrides this.
4. `"NoUnsafeDeps"` -- LAI flag at `+0xDAh` (`HasUnsafeDependences`) is zero.
5. `"MemOpsCanBeVectorized"` -- all memory operations already vectorizable.
6. `"TooManySCEVRuntimeChecks"` -- SCEV check count at LAI `+0x118` exceeds `qword_4FB5480`.

**LoopAccessInfo (LAI) structure** (0x130 = 304 bytes):

| Offset | Content |
|---|---|
| +0x00 | Loop\* TheLoop |
| +0x08 | PredicatedScalarEvolution\* PSE |
| +0x10 | RuntimeCheckingPtrGroup\* PtrRtChecks |
| +0x90 | SmallVector buffer (16-byte aligned) |
| +0xDAh | bool HasUnsafeDependences |
| +0xE0h | MemoryDepChecker::Dependence\* DepArray |
| +0xE8h | uint32 NumDependences |
| +0x108 | SCEVUnionPredicate\* Predicates |
| +0x110 | SCEVCheck\* SCEVChecks |
| +0x118 | uint32 NumSCEVChecks |

**Dependence entry** (0x40 = 64 bytes per entry): source instruction (+0x00), destination instruction (+0x08), dep type info (+0x10), SCEV distance (+0x18), DependenceType byte (+0x28). Stride confirmed at `shl rax, 6` (0x1A8E6B9).

If validation passes, the core phase builds a partition graph. Each instruction starts in its own partition. The partition hash set uses 16-byte slots with NVVM-layer sentinels (-8 / -16) and an additional `-2` value for "unassigned" partitions. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing, and growth policy.

For each unsafe memory dependence pair, the pass either merges source and destination partitions (if the dependence cannot be broken) or marks it as cross-partition. A union-find structure tracks merged partitions. After merging, if at least two distinct partitions remain, `sub_1B1E040` (`distributeLoopBody`, ~2000 bytes) clones the loop body once per partition, removes instructions not belonging to each partition, and wires the clones in dependence order. Optional runtime dependence checks (loop versioning) are added. Post-distribution: `sub_1B1DC30` updates the dominator tree, `sub_197E390` registers new loops, `sub_143AA50` (`ScalarEvolution::forgetLoop`) invalidates SCEV cache. Metadata `"distributed loop"` (16 chars) is attached to prevent future re-distribution.

**GPU relevance.** Distribution is valuable for CUDA kernels that mix shared-memory and global-memory accesses in the same loop -- the shared-memory partition can often be vectorized independently. The `"llvm.loop.distribute.enable"` metadata is controllable via `#pragma clang loop distribute(enable)`. The SCEV runtime check threshold (`qword_4FB5480`) balances runtime check overhead against distribution benefit -- GPU kernels often have simple loop structures but complex pointer arithmetic from tiled access patterns.

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
| Pass name | `"loop-idiom"` (recognizer), `"loop-idiom-vectorize"` (vectorizer) |
| Vectorize knobs | `disable-loop-idiom-vectorize-all`, `loop-idiom-vectorize-style` (masked/predicated), `loop-idiom-vectorize-bytecmp-vf`, etc. |
| NVIDIA delta | **None visible** -- stock LLVM |

**Standard idioms.** The recognizer scans loops for store patterns that correspond to memset (constant value stored on every iteration) and memcpy/memmove (load-store pairs with matching strides). It also detects trip-count-decrement patterns (`"tcphi"`, `"tcdec"`) used in hand-written copy loops. Recognized patterns are lowered to `@llvm.memset` / `@llvm.memcpy` / `@llvm.memmove` intrinsics.

**Vectorized idiom expansion -- MemCmpMismatch (`sub_2AA00B0`).** The expansion generates a two-tier multi-block IR structure:

1. **LoopIdiomExpansionState** structure (80+ bytes): idiom type at +0 (0=byte, 1=word), loop info at +8, DataLayout at +16, alloc context at +24, target info at +32, output blocks at +48 through +80.

2. **11 basic blocks** created in sequence: `"mismatch_end"`, `"mismatch_min_it_check"`, `"mismatch_mem_check"`, `"mismatch_vec_loop_preheader"`, `"mismatch_vec_loop"`, `"mismatch_vec_loop_inc"`, `"mismatch_vec_loop_found"`, `"mismatch_loop_pre"`, `"mismatch_loop"`, `"mismatch_loop_inc"`, `"byte.compare"`.

3. **Page-boundary safety protocol** (shared with string search expansion): `PtrToInt` -> `LShr` by `log2(pagesize)` (from `sub_DFB4D0` via DataLayout) -> `ICmpNE` of start/end page numbers. If both pointers stay within a single page, wider-than-element vector loads are safe; otherwise, `@llvm.masked.load` provides the fallback. The page size is retrieved via `sub_DFB4D0(*a1[32])` from the target DataLayout.

4. **Vector loop body**: dispatches to `sub_2A9D690` (byte-granularity) or `sub_2A9EC20` (word-granularity) based on `*a1` idiom type. Generates vector load + compare + cttz (count trailing zeros via `sub_B34870`).

5. **Scalar fallback**: byte-by-byte comparison with `"mismatch_index"` phi node, induction variable add (`sub_929C50`), and `ICmpULT` (`sub_92B530(0x20)`) loop bound check.

6. **LCSSA verification**: explicit assertion `"Loops must remain in LCSSA form!"` via `sub_D48E00`. SE/LI/DT invalidated/recalculated on exit (`sub_FFCE90`, `sub_FFD870`, `sub_FFBC40`).

**Vectorized idiom expansion -- FindFirst (`sub_2AA3190`).** Implements vectorized first-occurrence search (strstr-like):

1. **7 basic blocks**: `"scalar_preheader"`, `"mem_check"`, `"find_first_vec_header"`, `"match_check_vec"`, `"calculate_match"`, `"needle_check_vec"`, `"search_check_vec"`.

2. **Needle splatting**: `needle[0]` is extracted via `ExtractElement` (`sub_B4DE80`) with index 0, frozen via `sub_B37620`, then splatted across all vector lanes via `ShuffleVector` (`sub_B36550`). The splat enables parallel comparison of the haystack against the needle's first character.

3. **Masked loads**: `@llvm.masked.load` (`sub_B34C20`) provides page-boundary-safe vectorized reads. Same page-boundary protocol as mismatch expansion.

4. **Two nested loops**: outer scans haystack, inner verifies full needle match at candidate positions. PHI nodes: `"psearch"` (haystack), `"pneedle"` (needle position), `"match_start"`, `"match_vec"`.

**GPU considerations.** LoopIdiom is present in cicc but its value on GPU code is limited. GPU memset/memcpy are typically handled by device runtime calls or specialized PTX instructions (`st.global`, `ld.global` with vectorized widths) rather than loop-based patterns. The vectorized mismatch/search expansions target CPU-style byte-level operations that are rare in GPU kernels. The page-boundary safety protocol is irrelevant on GPU (virtual memory page faults work differently -- GPU global memory is always accessible within the allocation). The pass runs but likely fires infrequently. When it does fire, the generated `@llvm.memset`/`@llvm.memcpy` intrinsics are later lowered to PTX-specific sequences by the NVPTX backend.

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

**Pipeline placement.** LoopRotate appears at least four times in the cicc pipeline across different tiers:

1. **Full O1+ pipeline, position 11**: `sub_18A3090()` in `sub_12DE330` -- runs before LICM (`sub_184CD60`) and IndVarSimplify.
2. **Tier 1 passes**: appears alongside SimplifyCFG and InstCombine as part of the canonicalization loop.
3. **Tier 2 passes**: appears again in the LoopRotate+LICM pair.
4. **Pipeline assembler**: `sub_195E880` appears 4 times (labeled "LICM/LoopRotate"), conditional on `opts[1240]` and `opts[2880]`.

This multiple invocation is standard LLVM practice -- rotation may be needed again after other transforms invalidate the rotated form. In the Ofcmid fast-compile pipeline, LoopRotate does not appear as a standalone pass; LICM (which internally depends on rotation) handles it.

**Algorithm.** The pass duplicates the loop header into the preheader (creating a "rotated" header named `"h.rot"` or `"pre.rot"`), then rewires the CFG so the original header becomes the latch. The `header-duplication` parameter controls whether the header is actually duplicated (which increases code size) or only the branch is restructured. After rotation, SCEV's backedge-taken count computation becomes straightforward because the exit test is at the latch.

**SCEV interaction.** LoopRotate requires BTC (backedge-taken count) recomputation after the header/latch swap. This is handled by `ScalarEvolution::forgetLoop` being called by downstream passes that depend on fresh SCEV data.

**GPU considerations.** LoopRotate is purely a structural transformation that does not examine instruction semantics. It has no barrier awareness -- if a barrier (`__syncthreads()`) is in the loop header, it will be duplicated into the preheader during rotation. In practice, barriers in CUDA kernels are rarely in loop headers (they are typically in loop bodies or between loops). The header duplication can increase code size, which affects instruction cache utilization on GPU -- SM instruction caches (L0/L1 I-cache) are small (typically 12-48 KB per SM depending on architecture), so excessive duplication of large loop headers across many loops in a kernel could cause I-cache pressure. The pass does not have a size threshold to prevent this.

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
| Pipeline wrapper (standalone) | `sub_1832270(n)` where n = verify flag |
| Pipeline wrapper (bundled) | `sub_1841180()` -- LoopSimplify + LCSSA combined |
| NVIDIA delta | **None** -- stock LLVM |

**Pipeline placement.** LoopSimplify is the most frequently invoked loop pass in the cicc pipeline:

| Context | Call site | Position |
|---|---|---|
| Full O1+ pipeline | `sub_1841180()` | Position 40 (bundled with LCSSA) |
| Ofcmid pipeline | `sub_1832270(1)` | Position 11 (standalone) |
| Ofcmid pipeline | `sub_1841180()` | Position 15 (bundled with LCSSA) |
| Post-tier insertion | `sub_1841180()` | Tier 2/3 additional invocations |
| As precondition | `sub_157F0D0` (check) | Called by LoopInterchange, LoopDistribute, IRCE, LoopVectorize |

The pass appears at least 5 times across different pipeline tiers. It also runs as a utility called by other loop passes -- LoopInterchange, LoopDistribute, IRCE, and LoopVectorize all check `isLoopSimplifyForm()` (`sub_157F0D0`) and bail out if it fails.

**What it does.** If a loop lacks a single preheader, LoopSimplify creates one by inserting a new basic block on the entry edge (named with `.lr.ph` suffix via `sub_1A5E350`). If multiple latch blocks exist, it merges them into one (inserting `.backedge` blocks). If exit blocks are shared with other loops, it creates dedicated exit blocks via `sub_1A5F590` (42 KB normalization function). After transformation, loop metadata (`"llvm.loop"` nodes) is preserved on the new latch terminator.

**GPU considerations.** LoopSimplify is purely structural and has no GPU-specific implications. However, it is worth noting that StructurizeCFG (which runs after all loop optimizations, during NVPTX code generation) re-canonicalizes the CFG for GPU divergence handling. Loop structures created by LoopSimplify may be further modified by StructurizeCFG when the loop contains divergent branches. The two passes do not interfere because they run in different pipeline phases (IR optimization vs. code generation).

---

## LCSSA (Loop-Closed SSA)

Ensures that every value defined inside a loop and used outside it passes through a PHI node at the loop exit. This invariant simplifies SSA-based transformations: passes can modify loop internals without worrying about breaking uses outside the loop.

| Property | Value |
|---|---|
| Formation pass | `sub_1AE2630` (49 KB) |
| Lightweight form | `sub_1961B00` (13 KB) -- creates `.lcssa` PHI nodes |
| LCSSA updater | `sub_1AF8F90` -- used by LoopInterchange post-transformation |
| Pass name | `"lcssa"` |
| Verify knob | `verify-loop-lcssa` registered at `ctor_094` (~`0x4A2491`) |
| String markers | `".lcssa"` suffix on PHI node names |
| NVIDIA delta | **None** -- stock LLVM |

**Pipeline placement.** LCSSA runs bundled with LoopSimplify via `sub_1841180()` at position 40 in the full pipeline. In the Ofcmid fast-compile pipeline, it appears at position 15 via the same bundled wrapper. It is also maintained incrementally by every pass that modifies loop structure:

- **LoopInterchange** calls `sub_1AF8F90` to update LCSSA form for both inner and outer loops after transformation. The inner loop is updated first. The TTI availability boolean from `a1+192` is passed as the 4th argument to the updater.
- **LoopUnroll** checks LCSSA form via `sub_D49210` and generates `.unr-lcssa` blocks for unrolled iterations.
- **LoopIdiom** expansions (`sub_2AA00B0`, `sub_2AA3190`) end with explicit `verifyLoopLCSSA` assertion (`"Loops must remain in LCSSA form!"`).

**What it does.** For each instruction defined inside the loop, LCSSA checks all uses outside the loop's exit blocks. For each such use, it inserts a PHI node in the exit block with the defined value as the incoming value from the latch. The PHI node is named with a `.lcssa` suffix. After LCSSA formation, all external uses of loop-internal values go through these PHI nodes, and loop transforms only need to update the PHI nodes rather than chasing all external uses.

**GPU considerations.** LCSSA is purely structural and has no GPU-specific behavior. However, LCSSA PHI nodes interact with the NVPTX backend's divergence analysis: when a loop exit depends on a divergent condition (different threads take different exit iterations), the `.lcssa` PHI node at the exit carries a divergent value. The divergence analysis pass (`NVVMDivergenceLowering`, `sub_1C76260`) must handle these PHIs correctly to avoid generating incorrect predication. This is not an issue with LCSSA itself but with downstream consumers.

---

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `IndVarSimplify::run` (core) | `sub_1945A50` | 65 KB | -- |
| `IndVarSimplifyPass::run` (NewPM wrapper with NVIDIA guards) | `sub_19489B0` | -- | -- |
| `rewriteLoopExitValues` | `sub_1943460` | -- | -- |
| `replaceExitValuesWithCompute` (LFTR commit) | `sub_1941790` | -- | -- |
| `computeWideTripCount` | `sub_1940670` | -- | -- |
| `hasCongruousExitingBlock` | `sub_193E1A0` | -- | -- |
| `getLoopDepth` (recursive, 1 for outermost) | `sub_193DD90` | -- | -- |
| `isBetterIV` (candidate comparison for LFTR) | `sub_193E640` | -- | -- |
| `exitValueSafeToExpand` (SCEV expandability check) | `sub_193F280` | -- | -- |
| `findFinalIVValue` (trace IV to exit value) | `sub_193F190` | -- | -- |
| `hasSafeExitBlock` (exit block LFTR safety) | `sub_193F750` | -- | -- |
| `initPassState` (initialize pass-level state) | `sub_1940CE0` | -- | -- |
| `clearPassState` (cleanup per-iteration state) | `sub_1940B30` | -- | -- |
| `SimplifyIndVar::simplifyIVUsers` | `sub_1B649E0` | -- | -- |
| `LoopInterchange::processLoopList` | `sub_1979A90` | 69 KB | -- |
| `LoopInterchange` legality checker | `sub_1975210` | 45 KB | -- |
| `LoopInterchange` dependence analysis helper | `sub_1978000` | 37 KB | -- |
| `LoopInterchange::getAnalysisUsage` | `sub_19743F0` | -- | -- |
| SmallVector copy helper (dep vector / loop list) | `sub_19742B0` | -- | -- |
| `vector<DepVector>` push\_back | `sub_1974CB0` | -- | -- |
| Swap loop bounds / trip count metadata | `sub_1973F90` | -- | -- |
| `InductiveRangeCheckElimination::run` | `sub_194D450` | 71 KB | -- |
| `createPreLoop` / `cloneLoopForRange` (~1200 bytes) | `sub_194C320` | -- | -- |
| `createPostLoop` / `wirePostLoop` (~1300 bytes) | `sub_194AE30` | -- | -- |
| `classifyRangeCheckICmp` (~800 bytes) | `sub_1949EA0` | -- | -- |
| `canHandleRangeCheck` (~400 bytes) | `sub_1949540` | -- | -- |
| `canHandleRangeCheckExtended` (~300 bytes, constrained mode) | `sub_1949670` | -- | -- |
| `buildInductiveRangeCheck` (~500 bytes) | `sub_1949C30` | -- | -- |
| `adjustSCEVAfterCloning` | `sub_1949270` | -- | -- |
| `simplifyLoopAfterCloning` (~200 bytes) | `sub_1948FD0` | -- | -- |
| `verifyLoopStructure` (~200 bytes) | `sub_1948D70` | -- | -- |
| `LoopDistributePass::run` | `sub_1A8CD80` | 63 KB | -- |
| `distributeLoopBody` (core fission engine, ~2000 bytes) | `sub_1B1E040` | -- | -- |
| `updateDominatorTree` (post-distribution, ~400 bytes) | `sub_1B1DC30` | -- | -- |
| `updateLoopInfo` (post-distribution, ~300 bytes) | `sub_1B1DDA0` | -- | -- |
| `cleanupPartitions` (~400 bytes) | `sub_1B1F0F0` | -- | -- |
| `verifyDistribution` (~300 bytes) | `sub_1B216C0` | -- | -- |
| `cleanupAfterDistribution` (~200 bytes) | `sub_1A8C510` | -- | -- |
| `lookupPartitionForInstruction` (hash table lookup) | `sub_3860240` | -- | -- |
| `hasDirectDependence(partA, partB)` | `sub_385DBB0` | -- | -- |
| `alreadyMerged(partA, partB)` | `sub_385DB90` | -- | -- |
| `isSafeToDistribute` (final safety check) | `sub_1452CB0` | -- | -- |
| `LoopIdiomRecognize::run` | `sub_196FF90` | 51 KB | -- |
| LoopIdiom memset pattern detection | `sub_196B740` | 10 KB | -- |
| LoopIdiom memcpy/memmove patterns | `sub_196E000` | 43 KB | -- |
| `expandMemCmpMismatch` | `sub_2AA00B0` | 48 KB | -- |
| `expandFindFirst` (string search vectorization) | `sub_2AA3190` | 40 KB | -- |
| `expandByteMismatchLoopBody` (type 0) | `sub_2A9D690` | -- | -- |
| `expandWordMismatchLoopBody` (type 1) | `sub_2A9EC20` | -- | -- |
| `replaceUsesOfPhiInSuccessors` (LCSSA fixup) | `sub_2A9D330` | -- | -- |
| `LoopRotation::runOnLoop` | `sub_2A0CFD0` | 65 KB | -- |
| `LoopRotatePass` (NewPM, `"header-duplication;"`) | `sub_28448D0` | -- | -- |
| `LoopRotate` (legacy pipeline call) | `sub_18A3090` | -- | -- |
| `LoopSimplify` canonical form enforcement | `sub_1A5B3D0` | 62 KB | -- |
| `LoopSimplify` DomTree update helper | `sub_1A593E0` | 47 KB | -- |
| LoopSimplify preheader insertion | `sub_1A5E350` | 25 KB | -- |
| LoopSimplify exit block normalization | `sub_1A5F590` | 42 KB | -- |
| `LoopSimplify` pipeline wrapper (with verify flag) | `sub_1832270` | -- | -- |
| `LoopSimplify + LCSSA` bundled pass | `sub_1841180` | -- | -- |
| LCSSA formation pass | `sub_1AE2630` | 49 KB | -- |
| LCSSA lightweight `.lcssa` PHI insertion | `sub_1961B00` | 13 KB | -- |
| LCSSA form updater (used post-interchange) | `sub_1AF8F90` | -- | -- |
| `verifyLoopLCSSA` (assertion: "Loops must remain in LCSSA form!") | `sub_D48E00` | -- | -- |

---

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **IndVarSimplify knobs** | Stock LLVM defaults; no GPU-specific configuration | Three NVIDIA-specific knobs that change IV widening/narrowing behavior for GPU register pressure management |
| **Barrier awareness** | No concept of GPU barriers or synchronization primitives | None of the 8 standard passes have explicit barrier awareness; barrier handling deferred to dedicated NVIDIA passes (Dead Barrier Elimination, convergence token verification) |
| **LoopRotate frequency** | Runs once or twice in pipeline | Appears multiple times as canonicalization prerequisite for LICM and unrolling; forms the backbone of loop pass infrastructure |
| **LoopIdiom patterns** | `memset`, `memcpy` recognition for CPU targets | Same patterns; GPU-specific expansion handled downstream by [MemmoveUnroll](../passes/memmove-unroll.md) pass |
| **IRCE** | Range check elimination for deoptimization-safe targets | Present but effectiveness limited on GPU: no deoptimization support, relies on SCEV range analysis for bound proofs |
| **LoopInterchange** | Cost model driven by cache locality | Same legality checks; profitability analysis implicitly favors stride-1 access (coalescing) over cache line optimization |
| **IV Demotion** | Not present | Downstream NVIDIA pass ([IV Demotion](../passes/iv-demotion.md)) narrows IVs widened by IndVarSimplify back to 32-bit where GPU value ranges permit |

## Cross-References

- **[LoopVectorize & VPlan](./loop-vectorize.md)** -- LoopDistribute feeds vectorization; IRCE removes bounds checks that block it.
- **[Loop Unrolling](./loop-unroll.md)** -- Runs after IndVarSimplify canonicalizes IVs; requires LoopSimplify form. The `unroll-runtime-convergent` knob forces epilogue mode when convergent calls (warp-level primitives) are present -- an interaction with GPU barrier semantics that these 8 standard passes do not handle.
- **[LICM](./licm-real.md)** -- Requires LoopRotate and LoopSimplify as prerequisites.
- **[ScalarEvolution](./scev.md)** -- IndVarSimplify and IRCE are among the heaviest SCEV consumers; LoopInterchange uses SCEV for stride analysis. LoopRotate and LoopDistribute call `ScalarEvolution::forgetLoop` after transformation.
- **[SCEV Invalidation](./scev-invalidation.md)** -- LoopRotate requires BTC recomputation after header/latch swap; LoopDistribute calls forgetLoop after fission.
- **[Loop Strength Reduction](./lsr.md)** -- Runs after IndVarSimplify; consumes the canonicalized IV forms it produces. LSR has address-space-aware chain construction for shared memory (addrspace 3) that these 8 passes lack.
- **[IV Demotion](../passes/iv-demotion.md)** -- NVIDIA's custom pass that narrows IVs widened by IndVarSimplify back to 32-bit where value ranges permit, reducing register pressure for GPU occupancy.
- **[Dead Barrier Elimination](../passes/dead-sync-elimination.md)** -- Handles barrier optimization that these standard loop passes do not address.
- **[Pipeline & Ordering](./pipeline.md)** -- LoopRotate at position 11, LoopSimplify/LCSSA at position 40 in the full O1+ pipeline.
- **[NVVMDivergenceLowering](../passes/other.md)** -- Handles divergent LCSSA PHI nodes at loop exits when different threads take different exit iterations.
