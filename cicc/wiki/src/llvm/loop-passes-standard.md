# Standard Loop Passes

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

CICC v13.0 includes a full complement of LLVM loop transformation passes beyond the major ones (LoopVectorize, LoopUnroll, LICM, LSR) that have their own pages. This page covers the remaining loop passes: LoopInterchange, IRCE, IndVarSimplify, LoopDistribute, LoopIdiom, LoopRotate, LoopSimplify, LCSSA, LoopSimplifyCFG, LoopDeletion, SimpleLoopUnswitch, LoopFlatten, LoopPredication, LoopSink, and LoopVersioning. Most are stock LLVM with default thresholds, but IndVarSimplify carries three NVIDIA-specific knobs that materially change behavior on GPU code and SimpleLoopUnswitch exposes a GPU-critical `unswitch-uniform-only` knob that gates unswitching on warp-uniform conditions. LoopRotate appears multiple times in the pipeline as a canonicalization prerequisite for LICM and unrolling. The canonicalization trio -- LoopSimplify, LCSSA, and LoopRotate -- run so frequently they constitute the backbone of loop pass infrastructure in cicc.

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

## LoopSimplifyCFG

A loop-local variant of SimplifyCFG that performs CFG-level cleanup confined to a single loop nest -- branch folding, dead-block removal, terminator constant-folding, and switch lowering -- without invalidating the surrounding loop pass manager's loop list. Unlike global SimplifyCFG (which can blow away loop structure entirely), LoopSimplifyCFG is safe to interleave with other LPM passes because it preserves the `LoopInfo`, `DominatorTree`, `LCSSA`, and `MemorySSA` invariants the LPM contract requires.

| Property | Value |
|---|---|
| Entry point | `sub_198E0D0` (226 bytes -- thin NewPM wrapper) |
| Type-name string | `"LoopSimplifyCFGPass"` at `0x4374398` |
| Pass name | `"loop-simplifycfg"` at `0x42c02b6` |
| Description string | `"Simplify loop CFG"` at `0x42c02a4` |
| Knob | `enable-loop-simplifycfg-term-folding` at `0x4395ab0` |
| Knob constructor | `ctor_468` (70 bytes -- single bool knob) |
| Wired into pipelines | `sub_233C410`, `sub_2342890`, `sub_235B6A0`, `sub_2368220`, `sub_2377300`, `sub_2382460`, `sub_2394710`, `sub_28487C0` |
| NVIDIA delta | **None** -- stock LLVM |

**What it does.** Walks the loop's basic blocks and applies four cleanup transformations:

1. **Terminator constant-folding** (gated by `enable-loop-simplifycfg-term-folding`) -- when a conditional branch or switch terminator has a constant condition after upstream propagation, replaces it with an unconditional branch and removes the dead successor. This is the most aggressive step: it can delete entire sub-CFGs within the loop.
2. **Dead-block removal** -- blocks with no predecessors (orphaned by terminator folding) are spliced out, their PHI uses repaired in successors.
3. **Branch threading** -- single-predecessor / single-successor block pairs are merged when no PHI nodes block the merge.
4. **Trivial loop deletion** -- if the loop becomes a trivial fall-through (empty header, no backedge), it is removed and the LPM updater is notified via `LPMUpdater::markLoopAsDeleted`.

**Pipeline placement.** Eight call sites distributed across LPM construction. The pass typically runs immediately after LICM/LoopUnswitch/IndVarSimplify to clean up the dead branches those passes leave behind. Note that the pipeline assembler functions (`sub_233C410` etc.) are the family of LPM builders -- LoopSimplifyCFG appears in essentially every flavor of the loop pipeline that cicc constructs.

**GPU considerations.** The `enable-loop-simplifycfg-term-folding` knob is significant for GPU codegen because terminator folding can convert a divergent branch (where some threads in a warp take one path, others take the other) into a uniform branch (when the condition reduces to a constant). This reduces warp divergence pressure for the downstream NVPTX backend. The pass has no explicit divergence model -- it relies on constant-folding having already eliminated the divergence at IR level. There is no barrier awareness: if the deleted dead block contained a `__syncthreads()`, the barrier is removed silently. This is correct (the barrier was on a path that constant analysis proved unreachable) but the removal happens without a remark.

---

## LoopDeletion

Removes loops that the optimizer can prove are dead -- either because they execute zero iterations, because their results are entirely unused, or (in the extended NVIDIA-aware configuration) because symbolic execution of the first iteration proves the backedge is never taken. Deleting a loop early in the pipeline is enormously profitable because it eliminates work for every downstream pass.

| Property | Value |
|---|---|
| Entry point | `sub_19681C0` (226 bytes -- NewPM wrapper) |
| Type-name string | `"LoopDeletionPass"` at `0x436ac90` |
| Pass name | `"loop-deletion"` at `0x42bf0bb` |
| Description string | `"Delete dead loops"` at `0x42bf0a9` |
| Disable knob | `disable-LoopDeletionPass` at `0x4282255` (description: `"Disable LoopDeletionPass"` at `0x428226e`) |
| Symbolic-execution knob | `loop-deletion-enable-symbolic-execution` at `0x4394b58` |
| Symbolic-execution description | `"Break backedge through symbolic execution of 1st iteration attempting to prove that the backedge is never taken"` at `0x4394b80` |
| Knob constructor | `ctor_459` (526 bytes) |
| Pipeline call sites | `sub_233C410`, `sub_2342890`, `sub_235B6A0`, `sub_2368220`, `sub_2377300`, `sub_2382460`, `sub_2394710` |
| NVIDIA delta | **Knob present** -- the disable switch is NVIDIA-named; symbolic execution is recent LLVM but exposed as configurable |

**Three deletion conditions.** LoopDeletion fires when ANY of three conditions are proved:

1. **Zero-trip provable via SCEV** -- `ScalarEvolution::getBackedgeTakenCount` returns a constant that is provably <= 0 at the loop's entry condition. The loop body never executes; the entry edge is rewired directly to the exit. This is the cheapest and most common case.
2. **Loop output is dead** -- every value defined inside the loop has no use outside the loop. Combined with proof that the loop terminates (mustprogress attribute, finite-trip SCEV, or explicit `llvm.loop.mustprogress` metadata), the loop is removed wholesale. The LCSSA PHI nodes at the exit are replaced with their entry-edge incoming values (since the loop is provably equivalent to its initial state for any live-out).
3. **Symbolic execution of iteration 1** (gated by `loop-deletion-enable-symbolic-execution`) -- the optimizer symbolically executes the first iteration of the loop with concrete values from the preheader, then checks whether the backedge condition evaluates to false. If so, the loop runs at most once; combined with output-dead analysis or trivial body, this proves zero-or-one iterations and enables deletion of the backedge.

**Pipeline placement.** Seven call sites in the LPM builders. LoopDeletion typically runs at the **end** of the loop pipeline so that earlier passes (IndVarSimplify trip-count refinement, LICM hoisting making the body output-dead, LoopUnswitch eliminating conditions, LoopPredication tightening bounds) have had a chance to expose deletion opportunities.

**GPU considerations.** Loop deletion is unconditionally profitable on GPU. Even loops with very small bodies cost a meaningful fraction of the kernel's runtime because of barrier and synchronization overhead in surrounding code. The symbolic execution mode is particularly valuable for CUDA kernels with thread-coarsening loops where the trip count depends on `blockDim` / `gridDim` -- after constant propagation of grid configuration into the kernel (when known at compile time via launch bounds), the symbolic execution can prove the loop runs zero times for certain block shapes. The pass has no barrier awareness: deleting a loop that contains `__syncthreads()` is **incorrect** if other threads in the warp/block expect to participate in the barrier, but the LLVM loop deletion pass treats convergent calls as side-effecting (preventing deletion), so this is structurally safe.

---

## SimpleLoopUnswitch

Hoists a loop-invariant conditional branch out of the loop by duplicating the loop body once per branch outcome, with each clone specialized for one value of the condition. This is the new-PM replacement for the legacy `LoopUnswitchPass` and is the pass actually invoked in cicc's pipelines.

| Property | Value |
|---|---|
| Trivial-unswitch entry | `sub_1981A10` (234 bytes -- NewPM wrapper) |
| Non-trivial unswitch entry | `sub_1981CC0` (7,073 bytes -- core implementation) |
| Type-name string | `"SimpleLoopUnswitchPass"` at `0x436aa80` |
| Description string | `"Simple unswitch loops"` |
| Pass name (cluster) | `extra-simple-loop-unswitch-passes` at `0x437c0f0` |
| Driver string | `should-run-extra-simple-loop-unswitch` at `0x437c1c0` |
| Knob constructor | `ctor_484_0` (3,259 bytes -- large knob inventory) |
| Cost-multiplier ctor | `ctor_223` (794 bytes -- `enable-nontrivial-unswitch`) |
| Threshold ctor | `ctor_217` (810 bytes -- `loop-unswitch-threshold`, `Max loop size to unswitch`) |
| Legacy-disable ctor | `ctor_484_0` registers `disable-LoopUnswitchPass` |
| NVIDIA delta | **Significant** -- `unswitch-uniform-only` knob explicitly targets GPU warp uniformity |

**Knob inventory** (all registered by `ctor_484_0` unless noted):

| Knob | Address | Purpose |
|---|---|---|
| `enable-simple-loop-unswitch` | `0x4530eeb` | Master enable (default on; registered by ctor_723_0) |
| `enable-nontrivial-unswitch` | `0x42c3cad` | Allows code-size-increasing unswitching beyond the trivial form (ctor_223) |
| `loop-unswitch-threshold` | `0x42c2031` | Max loop size to unswitch -- `"Max loop size to unswitch"` (ctor_217) |
| `unswitch-threshold-unroll` | `0x4398aa6` | `"The cost threshold for unswitching a fully unrolled loop."` |
| `unswitch-max-switch-cases` | `0x4398ac0` | `"Max switch cases for fully unrolled loops where we decide to unswitch without checking profitability"` |
| `unswitch-uniform-only` | `0x4398a90` | `"Only unswitch uniform conditions."` -- **GPU-relevant** |
| `unswitch-num-initial-unscaled-candidates` | `0x4398ca0` | `"Number of unswitch candidates that are ignored when calculating cost multiplier."` |
| `unswitch-siblings-toplevel-div` | `0x4398c50` | Sibling-loop sharing factor |
| `enable-unswitch-cost-multiplier` | `0x4398bd0` | `"Enable unswitch cost multiplier that prohibits exponential explosion in nontrivial unswitch."` |
| `freeze-loop-unswitch-cond` | `0x4398af6` | `"If enabled, the freeze instruction will be added to condition of loop unswitch to prevent miscompilation."` |
| `simple-loop-unswitch-guards` | `0x4398ada` | `"If enabled, simple loop unswitching will also consider llvm.experimental.guard intrinsics as unswitch candidates."` |
| `simple-loop-unswitch-memoryssa-threshold` | `0x4398e58` | MemorySSA query budget |
| `simple-loop-unswitch-inject-invariant-conditions` | `0x4398f48` | `"Whether we should inject new invariants and unswitch them to eliminate some existing (non-invariant) conditions."` |
| `simple-loop-unswitch-inject-invariant-condition-hotness-threshold` | `0x4398ff8` | `"Only try to inject loop invariant conditions and unswitch on them to eliminate branches that are not-taken 1/<this option> times or less."` |
| `simple-loop-unswitch-drop-non-trivial-implicit-null-checks` | `0x4398da0` | `"If enabled, drop make.implicit metadata in unswitched implicit null checks to save time analyzing if we can keep it."` |
| `disable-loop-unswitching` | `0x4282207` | Disable description: `"Disables loop unswitching."` at `0x4282220` |
| `disable-LoopUnswitchPass` | `0x42823c2` | Disable description: `"Disable LoopUnswitchPass"` at `0x42823db` |

**Two-tier execution.** SimpleLoopUnswitch is run in two distinct modes within the LPM:

1. **Trivial unswitching** (`sub_1981A10`, 234 bytes) -- only unswitches conditions that do not require code duplication. The branch must dominate the loop exit, so the unswitch becomes a guard outside the loop. No body cloning. Cheap and always profitable. This is the variant that runs in O1-level pipelines.

2. **Non-trivial unswitching** (`sub_1981CC0`, 7,073 bytes) -- duplicates the loop body. Each candidate condition multiplies the loop size by the number of branch outcomes. The cost multiplier (`enable-unswitch-cost-multiplier`) prevents exponential blowup by tracking accumulated duplication factor across sibling candidates. Gated by `enable-nontrivial-unswitch`.

The `ShouldRunExtraSimpleLoopUnswitch` analysis (type names at `0x436bcd8`, `0x436e2f8`, `0x436fb60`, `0x4374af8`) is a driver that decides whether to run an extra unswitch pass after other transforms create new unswitching opportunities. The `require<should-run-extra-simple-loop-unswitch>` (`0x437cd90`) and `invalidate<should-run-extra-simple-loop-unswitch>` (`0x437cdc0`) pseudo-passes thread this signal through the pass manager.

**Metadata that controls unswitching:**

| Metadata | Address | Effect |
|---|---|---|
| `llvm.loop.unswitch.partial` | `0x4399125` | Marks a loop as a candidate for partial unswitching |
| `llvm.loop.unswitch.partial.disable` | `0x43990d8` | Disables partial unswitching for this loop |
| `llvm.loop.unswitch.injection` | `0x4399140` | Marks a loop where invariant-condition injection should run |
| `llvm.loop.unswitch.injection.disable` | `0x4399100` | Disables injection |

The string `unswitched.select` appears in the binary -- the select instruction that the unswitch transform creates at the exit to choose between the cloned-loop-result and the unentered case.

**Algorithm sketch.** For each candidate branch in the loop:

1. **Trivial check first** -- does the condition's dominator chain reach a loop exit without revisiting the latch? If yes, hoist as guard (cheap path).
2. **Cost evaluation** -- compute the duplication cost = (loop size in IR instructions) * (number of branch outcomes - 1). Compare against `loop-unswitch-threshold` (or `unswitch-threshold-unroll` if the loop is provably small enough for full unrolling). Apply the cost multiplier from sibling unswitches already performed.
3. **Uniformity gate** (NVIDIA-relevant) -- if `unswitch-uniform-only` is set, the condition must be provably warp-uniform (does not depend on `threadIdx.x` or any divergent value). This avoids creating per-thread-divergent specialized loop clones, which would defeat the purpose of unswitching on GPU.
4. **Freeze insertion** -- if `freeze-loop-unswitch-cond` is set, wrap the condition in a `freeze` instruction to block speculative-execution miscompiles when the condition is undef-poisoned in the original code.
5. **Clone and rewire** -- duplicate the loop, replace the condition with `true`/`false` in each clone, rewire the entry edge to dispatch to the correct clone based on the original condition value. Update LoopInfo, DominatorTree, LCSSA, and (optionally) MemorySSA.
6. **Update remarks** -- `unswitched.select` instructions are inserted at the post-loop merge point to consolidate exit values from the two clones.

**GPU considerations.** Loop unswitching is a code-size-vs-divergence trade-off that is unusually high-stakes on GPU:

- **Code size matters more** -- duplicating a loop body across two clones doubles the static instruction count, increasing SM instruction cache pressure. Many GPU kernels are I-cache-sensitive (especially with small SM I-cache sizes on consumer parts).
- **Divergence matters more** -- if the unswitched condition is divergent (threads in the same warp see different values), the two clones are useless because some lanes execute one clone and some execute the other in lockstep with SIMT predication. The `unswitch-uniform-only` knob exists precisely to prevent this pathology. When enabled (default behavior on GPU pipelines), only conditions provably independent of `threadIdx` / `laneId` / `divergence-tagged values` are unswitched.
- **Cost multiplier is essential** -- nested loops in CUDA kernels create exponential candidate counts. Without the cost multiplier, a 3-deep nested loop with 2 invariant conditions each would explode to 8 clones. The `enable-unswitch-cost-multiplier` knob keeps this in check.
- **Convergence-token interaction** -- if the unswitched condition is computed from a convergent call (e.g., `__ballot_sync`), unswitching could violate the call's reconvergence requirement. SimpleLoopUnswitch does not have explicit convergence-token awareness; the safety relies on `unswitch-uniform-only` blocking non-uniform-condition unswitching, since convergent-call results are typically tagged divergent.

**Pipeline placement.** Trivial unswitching runs early in the loop pipeline (before LICM, which depends on rotation but benefits from earlier hoisting of trivial guards). Non-trivial unswitching runs later, typically after IndVarSimplify and LICM, when the loop has been canonicalized and remaining invariants are easier to identify. The `extra-simple-loop-unswitch-passes` cluster lets the pipeline re-run unswitching after subsequent transforms create new opportunities.

---

## LoopFlatten

Collapses a perfectly-nested loop pair into a single loop with a wider induction variable. The classic case: `for (i = 0; i < M; ++i) for (j = 0; j < N; ++j) body(i*N + j)` becomes `for (k = 0; k < M*N; ++k) body(k)`. This reduces overhead from the outer loop's exit-check and PHI traffic.

| Property | Value |
|---|---|
| Pass name | `enable-loop-flatten` at `0x437df57` |
| Knob constructor | `ctor_461` (2,043 bytes) |
| Cost knob | `loop-flatten-cost-threshold` at `0x4394e5b` -- `"Limit on the cost of instructions that can be repeated due to loop flattening"` (`0x4394ea8`) |
| Widening knob | `loop-flatten-widen-iv` at `0x4394e77` -- forces IV widening before flattening to avoid overflow |
| Versioning knob | `loop-flatten-version-loops` at `0x4394e8d` -- emits a runtime guard for overflow safety |
| No-overflow knob | `loop-flatten-assume-no-overflow` at `0x4394ef8` -- assumes IV math cannot overflow (skips runtime check) |
| Registered by | `ctor_388_0` (master enable) and `ctor_461` (sub-knobs) |
| NVIDIA delta | **None visible** -- stock LLVM, default-off (`enable-loop-flatten` defaults to false) |

**Algorithm.** Requires the inner loop's bounds to be loop-invariant in the outer loop, all uses of the outer IV inside the inner body to follow the pattern `outer*inner_bound + inner_offset`, and the trip count product to not overflow. The pass either widens the IV (`loop-flatten-widen-iv`) or inserts a runtime overflow check that branches to the original two-loop form on overflow (`loop-flatten-version-loops`).

**GPU considerations.** LoopFlatten is potentially valuable for CUDA stencil kernels with 2-D or 3-D iteration spaces collapsed into thread-block tiles, but in practice the address-computation pattern `i*N + j` is often already coalesced at the IR level by IndVarSimplify and LSR. The default-off state suggests NVIDIA does not rely on this pass. When enabled, the cost threshold guards against duplicating expensive inner-loop preheader code.

---

## LoopPredication

Strengthens loop-invariant predicates (typically guard intrinsics or implicit null checks) into the loop's exit condition so that the predicate is implied by the loop bound rather than checked per-iteration. This eliminates per-iteration branches on conditions that are mathematically subsumed by the IV's range.

| Property | Value |
|---|---|
| Entry point | `sub_28418C0` (12,172 bytes -- core implementation) |
| Type-name string | `"LoopPredicationPass"` at `0x436fd10` |
| Pass name | `"loop-predication"` at `0x42bfe77` |
| Knob constructor | `ctor_210` (1,452 bytes) and `ctor_466` (2,974 bytes) |
| IV-truncation knob | `loop-predication-enable-iv-truncation` at `0x42c0010` |
| Count-down knob | `loop-predication-enable-count-down-loop` at `0x42c0038` |
| Skip-profitability knob | `loop-predication-skip-profitability-checks` at `0x42c0060` |
| Latch-probability scale | `loop-predication-latch-probability-scale` at `0x42c0090` |
| Predicate-widening knob | `loop-predication-predicate-widenable-branches-to-deopt` at `0x43958e8` |
| Insert-assumes knob | `loop-predication-insert-assumes-of-predicated-guards-conditions` at `0x4395980` |
| Pipeline call sites | `sub_1981A10`, `sub_1981CC0`, `sub_233C410`, `sub_2342890`, `sub_235B6A0`, `sub_2368220`, `sub_2377300`, `sub_2382460`, `sub_2394710`, `sub_28418C0` |
| NVIDIA delta | **None visible** -- stock LLVM |

**Algorithm.** Identifies `llvm.experimental.guard` intrinsics (or `widenable_condition` branches) inside the loop, computes the safe range for each guard's predicate via SCEV, and folds the guard's condition into the loop's exit check. The guard is then removed from the body, replaced by an `assume` outside the loop. The result: the loop body has fewer branches; deoptimization, if needed, happens at the bound check on the IV.

**GPU considerations.** Guard intrinsics are uncommon in CUDA-generated IR (they are mainly used by JVM-like deoptimization scenarios). LoopPredication may fire on hand-written CUDA C++ that uses `__builtin_assume` heavily, but its primary value is for managed-runtime IR. The `predicate-widenable-branches-to-deopt` knob is irrelevant for CUDA (no deopt support on GPU). The pass is wired into many pipeline builders but likely fires rarely on cicc-typical input.

---

## LoopSink

Moves loop-invariant code that LICM hoisted INTO the preheader back DOWN into the loop body, but only into cold paths inside the loop. This is the inverse of LICM: when an invariant is used only on a rarely-taken path, executing it once in the preheader is more expensive than executing it conditionally inside the loop (because the preheader path always pays for it, while the cold-path placement only pays when the condition fires).

| Property | Value |
|---|---|
| Entry point | `sub_1990220` (234 bytes -- NewPM wrapper) |
| Pass name | `"loop-sink"` at `0x42c02d1` |
| Pipeline call sites | `sub_1990220`, `sub_233C410`, `sub_233F860`, `sub_2342890`, `sub_2368220`, `sub_2377300`, `sub_2382460` |
| NVIDIA delta | **None visible** -- stock LLVM |

**Algorithm.** Walks the preheader's instructions in reverse. For each instruction, computes the set of loop-internal use blocks. If all uses are in blocks colder than the preheader (by branch-probability metadata), sinks the instruction to the lowest common dominator of the uses (still inside the loop). The cost model uses `BranchProbabilityAnalysis` and block-frequency information.

**GPU considerations.** LoopSink's interaction with GPU code generation is subtle. Sinking an invariant into a cold path inside the loop:

- **Saves registers** -- the sunk value does not need a register reserved across the entire loop body, freeing it for other uses. This is good for occupancy.
- **Increases dynamic instruction count** on the cold path -- but only when that path executes, so amortized over warps, this is usually a net win.
- **Can introduce divergence** -- if the cold path is divergent (some lanes take it, some don't), the sunk instruction now executes under divergence rather than uniformly in the preheader. For pure data computations this is fine; for instructions with side effects (which LICM wouldn't have hoisted anyway), it would be unsafe.

The pass has no explicit GPU model -- it relies on the upstream LICM having already filtered out side-effecting instructions.

---

## LoopVersioning

Wraps a loop in a runtime check that selects between two versions: an optimized version (with assumptions baked in, e.g., no aliasing, stride==1, bounds satisfied) and a conservative original. Used as a transformation primitive by LoopDistribute, LoopUnrollAndJam, LoopVectorize, and as a standalone licm-versioning mode.

| Property | Value |
|---|---|
| Entry point | `sub_1B1EBF0` (244 bytes -- NewPM wrapper) |
| Type-name string | `"LoopVersioningPass"` at `0x4368b88` |
| Pass name | `"loop-versioning"` at `0x42c738a` |
| Standalone-mode knob | `enable-loop-versioning-licm` at `0x437e0e1` |
| Knob constructor | `ctor_388_0` and `ctor_723_0` |
| Pipeline call sites | `sub_1B1EBF0`, `sub_233C410`, `sub_233F860`, `sub_2342890`, `sub_2368220`, `sub_2377300`, `sub_2382460` |
| NVIDIA delta | **None visible** -- stock LLVM |

**Algorithm.** Given a set of `MemoryRuntimeCheck` predicates from `LoopAccessAnalysis` (pointer overlap, stride equality, bounds), versioning clones the loop body, adds a runtime dispatch block that evaluates the predicate set, and routes execution to the unaliased (or stride-safe) clone when the predicates hold and to the original conservative clone otherwise. The two clones share PHI fixup at the merge point.

**GPU considerations.** Runtime pointer-aliasing checks are valuable on GPU because pointer provenance is often opaque to the optimizer (especially with `__device__` function parameters that come from host pointers). However, the overhead of the runtime check itself -- a few integer comparisons and a divergent branch on the dispatch -- is non-trivial on GPU. The `enable-loop-versioning-licm` standalone mode is rarely worth enabling on GPU because the unaliased speedup must outweigh both the check cost and the I-cache pressure of carrying two loop clones.

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
| `LoopSimplifyCFGPass::run` (NewPM wrapper) | `sub_198E0D0` | 226 B | terminator folding + dead-block removal inside a loop |
| `LoopDeletionPass::run` (NewPM wrapper) | `sub_19681C0` | 226 B | zero-trip / dead-output / symbolic-exec deletion |
| `SimpleLoopUnswitchPass::run` (trivial) | `sub_1981A10` | 234 B | guard-style trivial unswitching |
| `SimpleLoopUnswitchPass::run` (non-trivial core) | `sub_1981CC0` | 7,073 B | full loop cloning unswitch |
| `LoopPredicationPass::run` (core) | `sub_28418C0` | 12,172 B | guard widening into loop bounds |
| `LoopSinkPass::run` (NewPM wrapper) | `sub_1990220` | 234 B | sink preheader-hoisted invariants into cold paths |
| `LoopVersioningPass::run` (NewPM wrapper) | `sub_1B1EBF0` | 244 B | runtime predicate dispatch between loop clones |
| `loop-deletion` knob ctor | `ctor_459` | 526 B | registers `loop-deletion-enable-symbolic-execution` |
| `loop-flatten` knob ctor | `ctor_461` | 2,043 B | registers cost-threshold, widen-iv, version-loops, assume-no-overflow |
| `loop-predication` knob ctor (primary) | `ctor_466` | 2,974 B | registers latch-probability-scale, predicate-widenable, insert-assumes |
| `loop-predication` knob ctor (truncation/count-down) | `ctor_210` | 1,452 B | registers enable-iv-truncation, enable-count-down-loop |
| `loop-simplifycfg-term-folding` knob ctor | `ctor_468` | 70 B | single bool knob |
| `loop-unswitch-threshold` ctor | `ctor_217` | 810 B | registers `loop-unswitch-threshold` |
| `enable-nontrivial-unswitch` ctor | `ctor_223` | 794 B | registers the non-trivial unswitch gate |
| `simple-loop-unswitch-*` ctor (omnibus) | `ctor_484_0` | 3,259 B | registers ~16 unswitch knobs including `unswitch-uniform-only` |
| LPM pipeline assembler family | `sub_233C410`, `sub_2342890`, `sub_235B6A0`, `sub_2368220`, `sub_2377300`, `sub_2382460`, `sub_2394710` | -- | wire LoopDeletion, LoopSimplifyCFG, LoopPredication, LoopSink, LoopVersioning into LPM |

---

## NVIDIA QUIRKs

These are concrete, binary-grounded oddities worth knowing when reimplementing or debugging.

### QUIRK 1: `unswitch-uniform-only` is the single most GPU-defining knob in this group

The description `"Only unswitch uniform conditions."` at `0x4398b10` (knob string at `0x4398a90`) reveals an explicit warp-divergence model that no other LLVM-distributed loop pass exposes. When set, SimpleLoopUnswitch refuses to clone a loop unless the unswitching condition is provably warp-uniform. This matters because:

- LLVM's upstream cost model for loop unswitching is purely code-size-based; it has no notion of SIMT execution.
- A non-uniform unswitch on GPU produces two loop clones that are useless: every warp executes both clones in lockstep with predication, so the supposed "specialization" benefit evaporates and only the code-size cost remains.
- The knob is wired through `ctor_484_0` alongside ~15 other unswitch tunables, suggesting NVIDIA tuned the entire unswitch behavior for GPU workloads rather than just adding one filter.

The implementation must consult a divergence analysis (likely the same one NVPTX uses downstream) to classify conditions. If the divergence analysis is unavailable or imprecise, the conservative default is to treat all conditions as divergent, which effectively disables non-trivial unswitching on GPU code. This is consistent with cicc's observed behavior: loop unswitching fires rarely on CUDA kernels even though many CUDA loops contain seemingly invariant conditionals (which are often actually thread-id-dependent).

### QUIRK 2: LoopDeletion's symbolic-execution mode is opt-in via `loop-deletion-enable-symbolic-execution`

The description string at `0x4394b80` is unusually explicit: `"Break backedge through symbolic execution of 1st iteration attempting to prove that the backedge is never taken"`. This mode -- which symbolically executes the first iteration with concrete preheader values to prove the backedge is dead -- is registered by `ctor_459` and is **off by default**. Two consequences:

1. Many "easy" GPU loops that would be deletable via one-iteration symbolic execution (e.g., trip counts computed from launch bounds that happen to evaluate to zero for certain block shapes) survive into later pipeline stages and pay the cost of being processed by every subsequent loop pass.
2. The `disable-LoopDeletionPass` knob exists alongside the symbolic-execution enable, giving two orthogonal axes of control: the entire pass can be disabled (presumably for compile-time debugging) or just the symbolic-execution sub-mode. This pair of knobs only makes sense if cicc users have hit miscompilations or compile-time blowups specific to symbolic execution -- it would not be carried forward as a per-mode toggle otherwise.

### QUIRK 3: Eight LPM builders all wire the same standard-pass set

The strings `loop-deletion`, `loop-simplifycfg`, `loop-predication`, `loop-versioning`, `loop-sink` each list ~7-8 reference functions in the `sub_233xxxxx`/`sub_234xxxxx`/`sub_235xxxxx`/`sub_237xxxxx`/`sub_239xxxxx` ranges. These are the LPM pipeline builders for different optimization tiers (O0, O1, O2, O3, Ofcmid, OS, Oz, etc.). Two observations:

- **Sink does NOT appear in `sub_2394710`** -- it is missing from one specific tier (likely Oz or O0), suggesting a deliberate tier choice rather than uniform inclusion.
- **Versioning does NOT appear in `sub_2394710` or `sub_2377300`** -- versioning is selectively excluded from multiple tiers, consistent with its high runtime-overhead profile.

This selective wiring is the only mechanism by which cicc tunes which standard loop passes run at which optimization level. There is no global "is this pass enabled" gate -- the choice is encoded in the pipeline-construction function calls. For reimplementation, this means each tier's LPM must be assembled by hand from the set of passes appropriate to that tier, not by filtering a master list.

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
| **SimpleLoopUnswitch uniform-only** | No divergence-aware unswitching; cost model is purely code-size | `unswitch-uniform-only` knob (`0x4398a90`, ctor_484_0) blocks unswitching on non-warp-uniform conditions, preventing useless clone duplication under SIMT predication |
| **LoopDeletion configurability** | Single enable/disable | Two-axis control: full pass disable (`disable-LoopDeletionPass` at `0x4282255`) plus separate symbolic-execution toggle (`loop-deletion-enable-symbolic-execution` at `0x4394b58`) |
| **LoopSimplifyCFG term-folding** | Always on | Gated by `enable-loop-simplifycfg-term-folding` (ctor_468) so terminator folding can be selectively disabled |
| **Per-tier pass selection** | Standard pass set per opt level via opt-tool flags | Eight distinct LPM-builder functions (`sub_233C410` family) each wire a hand-picked subset of LoopDeletion / LoopSimplifyCFG / LoopPredication / LoopSink / LoopVersioning -- no global enable list |

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
