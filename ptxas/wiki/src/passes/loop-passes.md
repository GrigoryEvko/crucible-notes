# Loop Passes

Eight phases in the ptxas pipeline transform loops in the Ori IR: one canonicalizer (phase 18), one unroller (phase 22), one software pipeliner (phase 24), four LICM instances (phases 35, 66, 79, 88), and one fusion pass (phase 59). Together they account for the largest category of repeated-pass instances in the pipeline -- the LICM family alone runs four times because intervening transformations (predication, legalization, GMMA fixup) continuously expose new invariants.

ptxas is not built on LLVM. Its loop infrastructure is a custom, non-SSA representation operating directly on the Ori IR's basic-block graph. Loop detection is performed by `AnalyzeControlFlow` (phase 3), which identifies back-edges, computes dominators, and annotates each basic block with a loop nesting depth stored at block offset +144. This nesting depth is the primary loop identity used by all eight passes.

| | |
|---|---|
| **OriLoopSimplification** | Phase 18 -- vtable at `off_22BD898` |
| **OriLoopUnrolling** | Phase 22 -- vtable at `off_22BD938` |
| **OriPipelining** | Phase 24 -- vtable at `off_22BD988` |
| **OriHoistInvariantsEarly** | Phase 35 -- vtable at `off_22BDB40` |
| **OriLoopFusion** | Phase 59 -- vtable at `off_22BDF00` |
| **OriHoistInvariantsLate** | Phase 66 -- vtable at `off_22BE018` |
| **OriHoistInvariantsLate2** | Phase 79 -- vtable at `off_22BE220` |
| **OriHoistInvariantsLate3** | Phase 88 -- vtable at `off_22BE388` |
| **Phase factory** | `sub_C60D30` cases 18, 22, 24, 35, 59, 66, 79, 88 |
| **Phase object size** | 16 bytes (standard `{vtable_ptr, allocator_ptr}`) |
| **IR level** | Ori -- SASS opcodes with virtual registers, pre-RA |
| **Loop detection** | `AnalyzeControlFlow` (phase 3) -- back-edges, dominators, nesting depth |
| **Related passes** | 3 `AnalyzeControlFlow`, 19 `OriSplitLiveRanges`, 21 `OriStrengthReduce`, 108 `OptimizeHotColdInLoop` |

## Pipeline Placement

```
Phase   3  AnalyzeControlFlow              ── builds CFG, identifies loops, computes dominators
Phase  13  GeneralOptimizeEarly            ── const fold + copy prop (feeds loop analysis)
Phase  15  OriBranchOpt                    ── branch simplification (may change loop shape)
Phase  16  OriPerformLiveDeadFirst         ── DCE removes dead loop bodies
Phase  18  OriLoopSimplification           ── CANONICALIZATION: single entry, preheader insertion
Phase  19  OriSplitLiveRanges              ── splits live ranges at loop boundaries
Phase  21  OriStrengthReduce               ── induction variable strength reduction
Phase  22  OriLoopUnrolling                ── UNROLLING: full/partial based on trip count
Phase  23  GenerateMovPhi                  ── SSA phi insertion (after unrolling changes CFG)
Phase  24  OriPipelining                   ── SOFTWARE PIPELINING: overlaps iterations
    ...
Phase  35  OriHoistInvariantsEarly         ── LICM #1: after GVN, before mid-expansion
    ...
Phase  59  OriLoopFusion                   ── FUSION: merges adjacent compatible loops
    ...
Phase  66  OriHoistInvariantsLate          ── LICM #2: after predication
    ...
Phase  79  OriHoistInvariantsLate2         ── LICM #3: after late unsupported-op expansion
    ...
Phase  88  OriHoistInvariantsLate3         ── LICM #4: after GMMA fixup
    ...
Phase 108  OptimizeHotColdInLoop           ── separates hot/cold paths within loops (post-RA)
```

### Ordering Rationale

The eight loop passes are deliberately spread across the pipeline rather than clustered together. Each occupies a specific position dictated by what has been lowered or optimized upstream:

1. **Phase 18** (simplification) must run before strength reduction (21) and unrolling (22) because both require canonical loop forms.
2. **Phase 22** (unrolling) runs after strength reduction so that induction variable simplifications are already applied, avoiding redundant computation in unrolled copies.
3. **Phase 24** (pipelining) runs after unrolling because pipelining targets loops that were *not* fully unrolled.
4. **Phase 35** (early LICM) runs after `GeneralOptimize` at phase 29, which performs partial CSE, giving it common subexpressions to hoist.
5. **Phase 59** (fusion) runs after late expansion (phase 55) because expansion can split a single operation into a loop pair that fusion can reunite.
6. **Phases 66, 79, 88** (late LICM instances) each follow a major transformation that can create new loop-invariant code: predication (63), unsupported-op expansion (78), and GMMA fixup (87), respectively.

---

## Loop Representation in Ori IR

ptxas does not use a dedicated loop descriptor data structure (no `LoopInfo` object like LLVM's). Instead, loop membership is implicit in the CFG through annotations computed by `AnalyzeControlFlow` (phase 3):

| BB Field | Offset | Type | Meaning |
|---|---|---|---|
| `loop_depth` | +144 | `int` | Loop nesting depth (0 = not in loop) |
| `loop_depth_equal` | +152 | `int` | Copy of loop_depth, used for sibling detection |
| `predecessor_list` | +128 | `linked_list*` | List of predecessor block indices |
| `successor_list` | +136 | `linked_list*` | List of successor block indices |

A loop header is a block whose `loop_depth` equals its own back-edge source's depth. Back-edge information is stored in the Code Object's back-edge hash map at offset +680. Diagnostic output from `sub_BDEA50` prints this information as `bix%d -> backedge's successor BB: %d`.

The block iteration order is controlled by a reverse-post-order (RPO) array stored at Code Object offset +512. All loop passes iterate over this array, ensuring they visit headers before inner blocks. The array length is at Code Object offset +520.

---

## Phase 18 -- OriLoopSimplification

### Purpose

Canonicalizes loop structure to simplify downstream analysis. Ensures each natural loop has a single entry edge, inserts dedicated preheader blocks where needed, and normalizes back-edge shapes. This is a prerequisite for strength reduction, unrolling, and pipelining, all of which assume canonical loop form.

### Entry Point

```
sub_C5FB00 (34 bytes)          ── vtable execute(), calls sub_7DDB50
  └─ sub_78B430 (1,172 bytes)  ── LoopMakeSingleEntry core
       ├─ sub_7753F0            ── pre-pass: loop peeling setup
       ├─ sub_789BE0            ── canonicalize back-edges
       ├─ sub_781F80            ── rebuild instruction list
       └─ sub_9253C0            ── split edges / insert preheader
```

### Algorithm

```
function LoopSimplification(code_object):
    if code_object.flags[1368] & 1 == 0:          // optimization disabled
        return

    // Phase 1: optional loop peeling for O4+ or flagged functions
    if opt_level not in {4,5} and flags[1382] & 4 set:
        peeled = PeelOuterEdges(code_object, 0)         // sub_7753F0
        canonicalized = CanonicalizeBackEdges(code_object, peeled)  // sub_789BE0
    else:
        canonicalized = CanonicalizeBackEdges(code_object, 0)

    if code_object.flags[1368] & 1 == 0:          // re-check after canon
        return

    // Phase 2: single-entry enforcement
    if not QueryKnob("LoopMakeSingleEntry", knob_487):  // OCG knob 487
        return

    RebuildInstructionList(code_object, 1)               // sub_781F80
    for each block in RPO order:
        if block.loop_depth > 0 and block is loop header:
            // find the deepest-nesting back-edge target
            // if multiple entries exist, split into single-entry form
            // insert preheader block between external predecessors and header
            InsertPreheaderIfNeeded(code_object, block)  // sub_9253C0
```

### GPU-Specific Considerations

The simplification pass checks the optimization level at offset +896 of the code object. Levels 4 and 5 (`-O4`, `-O5`) enable aggressive loop peeling via `sub_7753F0` before canonicalization. At the default `-O2`, peeling is suppressed to avoid code size growth that could cause instruction cache thrashing.

The `LoopMakeSingleEntry` knob (OCG knob 487) is the master enable. When disabled, only back-edge canonicalization runs -- preheader insertion is skipped. This knob is checked via the standard OCG knob query at offset +152 of the allocator vtable.

The pass also inspects the convergence flag at offset +1380 (bit 7). When set, it indicates a convergent execution context (e.g., warp-synchronous code), and certain edge-splitting transformations are suppressed to avoid disrupting convergence guarantees.

### Related Knobs

| Knob Name | Description |
|---|---|
| `LoopInversion` | Enable loop inversion (do-while to while conversion) |
| `LoopInversionBudget` | Maximum instruction count for loop inversion |
| `LoopPeelInversion` | Enable loop peeling combined with inversion |
| `EnableSingleThreadPeelingLoops` | Enable peeling for single-thread execution paths |
| `GenPeelingLoopsForSyncs` | Generate peeling loops around sync instructions |
| `AssertIfPeelingLoopForTexSurf` | Assert (debug) if peeling a loop for texture/surface ops |

---

## Phase 22 -- OriLoopUnrolling

### Purpose

Performs full unrolling of loops with known small trip counts and partial unrolling of larger loops to amortize loop overhead and expose instruction-level parallelism. This is one of the most impactful optimization passes for GPU code, where loops over texture coordinates, reduction accumulators, and matrix tiles dominate execution time.

### Function Map

| Function | Size | Role |
|---|---|---|
| `sub_83EF00` | 29,444 bytes | Top-level unrolling driver (trip count analysis, heuristics, body duplication) |
| `sub_80B700` | 1,408 bytes | Per-loop unroll decision (eligibility check, parameter lookup) |
| `sub_80BC80` | 130 bytes | Single-loop unroll wrapper (calls `sub_80B700`) |
| `sub_A1F5D0` | 7,402 bytes | Unroll body replication engine (copies instructions, adjusts IVs) |
| `sub_7E39B0` | 181 bytes | Unroll rejection diagnostic table lookup |
| `sub_A3A7E0` | 1,236 bytes | Post-unroll statistics (DUMPIR output) |

### Unroll Rejection Table

When a loop cannot be unrolled, the pass records a coded reason from a 24-entry rejection table at `0x21D1980`. Each entry is a 36-byte structure with fields `{a2, a3, a4, loop_type, is_single_iteration, param5, param6, param7, result_param}`. The rejection codes and their meanings:

| Code | Category | Reason |
|---|---|---|
| `0x80000001` | Irregular loop | Multiple back-edges or irreducible control flow |
| `0x80000002` | Irregular loop | Loop body contains complex CFG (cross-edges between inner blocks) |
| `0x80000003` | Ineligible instruction | Loop body contains a barrier (`BAR.SYNC`) |
| `0x80000004` | Ineligible instruction | Loop body contains an indirect jump |
| `0x80000005` | Ineligible instruction | Loop body contains a function call |
| `0x80000006` | Ineligible instruction | Loop body modifies the stack pointer |
| `0x80000007`--`0x8000000C` | Performance | Heuristic rejected: body too large, register pressure too high, or savings insufficient |
| `0x8000000D` | Unsupported loop type | Do-while with non-standard exit |
| `0x8000000E`--`0x80000017` | Unsupported exit condition | Exit condition is not a simple compare-and-branch against an induction variable |
| `0x80000010`--`0x80000012` | Unsupported index variable | Induction variable has non-unit stride, is used as a pointer, or has complex update |
| `0x80000018` | Unsupported loop type | Infinite loop or loop with no analyzable exit |

### Unrolling Algorithm

```
function OriLoopUnrolling(code_object):
    for each loop in RPO order (innermost first):
        // Step 1: Eligibility check
        rejection = CheckEligibility(loop)          // sub_80B700
        if rejection:
            RecordRejection(loop, rejection)         // 36-byte table entry
            continue

        // Step 2: Analyze trip count
        trip_count = AnalyzeTripCount(loop)          // known constant, variable, or unknown

        // Step 3: Determine unroll factor
        if trip_count is compile-time constant:
            if trip_count * body_size <= UnrollFullInstLimit:
                factor = trip_count                  // full unroll
            else:
                factor = ComputePartialFactor(trip_count, body_size)
        else:
            if body_size <= UnrollSmallLoopLimit:
                factor = UnrollUnknownCount          // knob-controlled default
            else:
                factor = 1                           // no unroll

        // Step 4: Profitability check
        if not IsProfitable(loop, factor):
            RecordRejection(loop, PERFORMANCE_REJECTION)
            continue

        // Step 5: Replicate body
        UnrollBody(loop, factor)                     // sub_A1F5D0
        AdjustInductionVariables(loop, factor)
        UpdateCFG(loop, factor)

        // Step 6: Handle remainder
        if trip_count % factor != 0 and trip_count is variable:
            EmitEpilogueLoop(loop, trip_count % factor)
```

### Heuristic Thresholds (Knobs)

The unrolling decision is controlled by a rich set of OCG knobs. All knob names are stored ROT13-encoded in the binary:

| Knob Name | Type | Description |
|---|---|---|
| `LoopUnroll` | BOOL | Master enable for loop unrolling |
| `LoopUnrollFactor` | INT | Override unroll factor (0 = heuristic) |
| `UnrollBudget` | INT | Maximum total instruction count after unrolling |
| `UnrollInstLimit` | INT | Maximum instructions in a single unrolled loop body |
| `UnrollFullInstLimit` | INT | Maximum body size for *full* unrolling |
| `UnrollFlexableFullLimit` | INT | Flexible full-unroll limit (adjusted by loop characteristics) |
| `UnrollSmallLoopLimit` | INT | Body size threshold below which loops are always fully unrolled |
| `UnrollPregThreshold` | INT | Maximum predicate register pressure for unrolling |
| `UnrollMultiBlockLoops` | BOOL | Allow unrolling of multi-basic-block loop bodies |
| `UnrollVariableBounds` | BOOL | Allow unrolling when trip count is not compile-time constant |
| `UnrollUnknownCount` | INT | Default trip count assumption when count is unknown |
| `UnrollUnknownInstLimit` | INT | Maximum body size for unrolling with unknown trip count |
| `UnrollExtraInstPerPercentSaving` | INT | Instructions allowed per percent of cycle saving |
| `UnrollTex3DPercentSavedThreshold` | INT | Minimum savings percent for 3D texture loops |
| `UnrollProfiledColdInstsScale` | INT | Scale factor for instruction count in profiled-cold blocks |
| `LoopUnrollExtraFoldableLdcWeight` | INT | Extra weight for foldable constant loads in unroll benefit |
| `LoopUnrollFoldableAddrWeight` | INT | Weight for foldable address computations |
| `LoopUnrollLargePartOfShaderPct` | INT | Percentage threshold: loop is "large part of shader" |
| `LoopUnrollNumExtraInstBase` | INT | Base extra instruction allowance per unroll iteration |
| `LoopUnrollNumInstSmallLoop` | INT | Instruction count defining "small loop" |
| `LoopUnrollNumInstTex` | INT | Texture instruction count bonus for unrolling |
| `LoopUnrollSingleLoopSavedPctFactor` | INT | Savings factor for single-loop shaders |
| `LoopUnrollNonInnermost` | BOOL | Allow unrolling of non-innermost loops |
| `LoopUnrollUnknownMultiBlock` | BOOL | Allow multi-block unroll with unknown bounds |
| `EpilogueLoopUnrollCount` | INT | Unroll count for epilogue (remainder) loops |
| `DisablePartialUnrollOverflowCheck` | BOOL | Skip overflow check on partial unroll count |

### GPU-Specific Unrolling Concerns

**Register pressure.** GPU threads share a fixed register file per SM. Unrolling increases live ranges, potentially reducing occupancy (the number of concurrent warps). The unroller queries register pressure estimates and compares against `UnrollPregThreshold` before committing.

**Instruction cache.** GPU instruction caches are small (typically 128KB L1i per SM). Aggressive unrolling of large loop bodies can cause i-cache thrashing. The `UnrollBudget` knob caps the total instruction growth.

**Texture instruction scheduling.** Texture fetches have high latency (hundreds of cycles). Unrolling loops containing texture operations is especially profitable because it exposes independent fetches that the scheduler can overlap. The `LoopUnrollNumInstTex` and `UnrollTex3DPercentSavedThreshold` knobs give extra weight to texture-heavy loops.

**PTX `nounroll` pragma.** The PTX string `nounroll` at `0x1CFE126` is parsed during PTX-to-Ori lowering and sets a flag on the loop that suppresses unrolling unconditionally.

### DUMPIR Statistics

When diagnostics are enabled, the pass outputs:

```
# [partially unrolled loops=N] [non-unrolled loops=M]
```

This line appears in eight SM-variant statistics printers (`sub_ABBA50` through `sub_ABEB50`), each a 1,771-byte clone specializing output format for a specific SM generation.

---

## Phase 24 -- OriPipelining

### Purpose

Performs modulo software pipelining on loops that were not fully unrolled. The pass overlaps successive loop iterations by interleaving instructions from different iterations within a single loop body, hiding functional unit and memory latency. This is the single most complex loop transformation in ptxas.

### Function Map

| Function | Size | Role |
|---|---|---|
| `sub_926A30` | 22,116 bytes | Main pipelining engine (modulo scheduling, stage assignment, prolog/epilog generation) |
| `sub_91A0F0` | 5,550 bytes | Instruction latency classifier (maps Ori opcodes to latency classes) |
| `sub_9203A0` | 4,881 bytes | Pipeline stage builder (assigns instructions to stages, resolves dependencies) |
| `sub_921820` | 1,592 bytes | Pipeline prolog/epilog generator |
| `sub_9202D0` | 207 bytes | Two-phase schedule attempt (tries different initiation intervals) |
| `sub_91E610` | 399 bytes | Pipeline entry: register pressure check before attempting |
| `sub_91E900` | 470 bytes | Pipeline retry with adjusted II |
| `sub_92C0D0` | 358 bytes | Pipeline invocation wrapper (called from multiple contexts) |
| `sub_92C240` | 8,033 bytes | Extended pipelining for GEMM-like loops |

### Software Pipelining Model

ptxas implements a variant of modulo scheduling. The algorithm:

1. **Builds a data dependence graph (DDG)** within the loop body, classifying each instruction by its latency class. The large opcode switch in `sub_91A0F0` maps approximately 350 Ori opcodes to integer latency values, with special cases for:
   - Memory operations (opcode categories `0x3C`--`0x4F`): latency 12--26 depending on address space
   - Texture operations (opcode `0x46`, `0xF3`--`0x106`): latency class determined by `PipelineMIOVQToInstRatio`
   - Integer arithmetic (opcodes `0x3`--`0x24`): latency 4--10
   - Floating-point (opcodes `0x55`--`0x6F`): latency 4--8
   - Barriers and sync (opcodes `0x5B`, `0x5C`, `0x137`): not pipelineable (returns failure code)

2. **Computes the minimum initiation interval (MII)** as `max(RecMII, ResMII)`:
   - `RecMII`: recurrence-constrained MII, determined by the longest cycle in the DDG divided by the number of iterations it spans.
   - `ResMII`: resource-constrained MII, determined by the most-used functional unit class.

3. **Attempts modulo scheduling** at `II = MII`, incrementing II on failure up to a configurable limit. For each candidate II, the scheduler places instructions into a modulo reservation table (MRT) and checks for resource conflicts.

4. **Generates kernel, prolog, and epilog** (`sub_921820`):
   - The **kernel** is the steady-state loop body containing instructions from multiple iterations.
   - The **prolog** fills the pipeline by executing partial iterations before the kernel starts.
   - The **epilog** drains the pipeline after the last kernel iteration.

### GEMM Pipelining

The `GemmPipeliner*` family of knobs controls a specialized pipelining mode for GEMM (matrix multiply) loops:

| Knob Name | Description |
|---|---|
| `GemmPipelinerEnabled` | Master enable for GEMM-specific pipelining |
| `GemmPipelinerPipelineDepthEnforceDeltaFull` | Pipeline depth adjustment for full enforcement |
| `GemmPipelinerPipelineDepthEnforceDeltaPartial` | Pipeline depth adjustment for partial enforcement |
| `GemmPipelinerDependenciesPopbl` | Dependency resolution policy |
| `GemmPipelinerScoreboardHashPopbl` | Scoreboard hash policy for GEMM |
| `GemmPipelinerUseRegisterCalculation` | Use register-based calculation for pipeline depth |

The extended pipelining in `sub_92C240` (8KB) handles GEMM-like patterns where the loop body contains WGMMA/IMMA instructions. It coordinates with the GMMA pipeline infrastructure (phases 85, 87) to ensure asynchronous matrix operations are correctly staged across pipeline iterations. On SM90+ (Hopper), asynchronous memory operations (TMA, bulk copies) have a hardware pipeline depth of up to 8 stages, and the GEMM pipeliner must match or approximate this depth for optimal throughput.

### Other Pipelining Knobs

| Knob Name | Description |
|---|---|
| `OkToPipelineNoUnroll` | Allow pipelining even when unrolling was also suppressed |
| `PipelineHoistCondLimit` | Maximum condition complexity for hoisting in pipelined loops |
| `PipelineHoistRRegPressureLimit` | R-register pressure limit for hoisting inside pipelined body |
| `PipelineHoistPRegPressureLimit` | P-register pressure limit for hoisting inside pipelined body |
| `PipelineMIOVQToInstRatio` | MIOVQ-to-instruction ratio threshold for pipeline profitability |
| `PipelineMultiOutputTex` | Enable pipelining of loops with multi-output texture instructions |
| `PipelineSpecUsesInHeadOnly` | Restrict speculative uses to loop header only |

### GPU-Specific Pipeline Concerns

**Warp divergence.** Pipelined loops assume all threads in a warp execute the same number of iterations. If the trip count is warp-divergent, the prolog/epilog handling must account for early-exit threads. The pass checks the varying analysis (phases 53, 70) to determine divergence.

**Barrier placement.** Pipelined loops containing `BAR.SYNC` or `MEMBAR` instructions cannot be pipelined (the latency classifier returns a failure code for barrier opcodes). The pipeline does not attempt modulo scheduling when barriers are present in the loop body.

**Memory pipeline depth.** The `sub_92C240` extended pipeliner for GEMM-like loops specifically manages the hardware memory pipeline on SM90+. The DUMPIR diagnostic output includes `For Dma Loop` and `For Math Loop` sections from `sub_7A4500`, indicating the pipeliner explicitly distinguishes between DMA (memory) and compute (math) loop stages.

---

## Phases 35, 66, 79, 88 -- OriHoistInvariants (LICM)

### Purpose

Hoists computations that produce the same result on every loop iteration out of the loop body and into the preheader. This reduces the dynamic instruction count proportionally to the trip count. The four instances are not redundant -- each targets invariants created by different intervening transformations.

### Function Map

All four instances share the same core implementation:

| Function | Size | Role |
|---|---|---|
| `sub_C5FE00` | 34 bytes | Phase 35 execute wrapper |
| `sub_C5FE30` | 34 bytes | Phase 66 execute wrapper |
| `sub_C5FE60` | 34 bytes | Phase 79 execute wrapper |
| `sub_C5FE90` | 34 bytes | Phase 88 execute wrapper |
| `sub_7DDB50` | 156 bytes | Optimization guard: checks knob 499, block count > 2 |
| `sub_8FFDE0` | 573 bytes | HoistInvariants orchestrator: iterates blocks, queries knob 381, dispatches inner worker |
| `sub_8FF780` | 1,622 bytes | LICM inner worker: identifies and moves invariant instructions |
| `sub_8F8BC0` | -- | Instruction movement helper |
| `sub_74D720` | -- | Loop boundary analysis |
| `sub_74F500` | -- | Preheader location finder |

### Execute Flow

```
sub_C5FExxx(phase_obj)                         // 34-byte vtable dispatch
  └─ sub_8FFDE0(code_object, pass_id)          // orchestrator
       ├─ sub_7DDB50(code_object)              // guard: returns block count, checks knob 499
       ├─ sub_799250(allocator, "HoistInvariants", &skip)  // DUMPIR check
       └─ sub_8FF780(context)                  // per-loop LICM core
            ├─ sub_781F80                       // rebuild instruction list
            ├─ sub_7E6090                       // recompute register pressure
            ├─ sub_773140                       // recompute loop depths
            ├─ sub_74D720                       // analyze loop boundaries
            ├─ sub_74F500                       // find preheader
            ├─ sub_7A1A90 / sub_7A1B80         // query knob 381 per block
            └─ sub_8F8BC0                       // move instruction to preheader
```

### Why Four Instances?

| Phase | Pass ID (`a2`) | Pipeline Position | What Creates New Invariants |
|---|---|---|---|
| 35 (`Early`) | 0 | After `GeneralOptimize` (29), `ExtractShaderConsts` (34) | CSE eliminates redundant expressions, exposing loop-invariant results; shader constant extraction hoists uniform loads |
| 66 (`Late`) | 1 | After predication (63), `GeneralOptimizeLate2` (65) | Predication converts conditional branches to predicated instructions; if the condition is loop-invariant, the entire predicated instruction becomes invariant |
| 79 (`Late2`) | 2 | After `LateExpansionUnsupportedOps` (78) | Late expansion splits compound operations into sequences; address computations and constant sub-expressions in expanded sequences are often invariant |
| 88 (`Late3`) | 3 | After `FixupGmmaSequence` (87) | GMMA fixup reorders/inserts instructions for wgmma hardware constraints; descriptor loads and accumulator setup become visible as invariants |

### Pass ID Controls Aggressiveness

The pass_id parameter (parameter `a2` of `sub_8FFDE0`) affects which loops are processed and how aggressively hoisting is performed. From the decompiled logic at `sub_8FFDE0`:

```c
// sub_8FFDE0 lines 58-89 (simplified)
v7 = sub_7A1B80(allocator, 381, block);   // query knob 381 for this block
if (v7 == 1) {                             // knob says "inner loops only"
    if (pass_id == 1) goto hoist_block;    // Late pass: proceed
    goto skip_block;                       // Early pass: skip
}
if (v7 == 3) {                             // knob says "never"
    if (pass_id <= 1) goto handle_conservative;
    goto skip_block;
}
if (v7 == 0) {                             // knob says "always"
    if (pass_id == 0) goto hoist_aggressively;
    goto skip_block;
}
```

- **pass_id = 0** (Early): Hoists aggressively and calls `sub_A112C0(code_object, 1)` to re-run sub-analyses afterward. This is the most aggressive pass.
- **pass_id = 1** (Late): Includes inner-loop-only blocks, but skips the re-analysis call.
- **pass_id >= 2** (Late2, Late3): Most conservative -- only hoists from blocks where knob 381 returns 0 (always-hoist).

### Per-Block Knob 381 Policy

The LICM pass queries OCG knob 381 (`sub_7A1A90` / `sub_7A1B80`) per basic block to determine the hoisting policy:

| Knob 381 Result | Meaning |
|---|---|
| 0 | Always hoist from this block |
| 1 | Hoist from inner loops only |
| 3 | Never hoist from this block |

This per-block granularity allows the knob system to selectively disable hoisting in specific loop nests (e.g., those known to be register-pressure-critical).

### LICM Algorithm (sub_8FF780)

```
function HoistInvariantsCore(context):
    code_object = context.code_object
    pass_id = context.pass_id

    // Read configuration from allocator offsets
    max_iterations = read_config(allocator + 34632)  // 0 = unlimited
    if max_iterations == 1:
        max_iterations = read_config(allocator + 34640)

    allow_nested_hoist = read_config(allocator + 20016) != 0

    RebuildInstructionList(code_object, 1)            // sub_781F80
    RecomputeRegisterPressure(code_object, 1, 0, 0, 0) // sub_7E6090
    RecomputeLoopDepths(code_object, 0)               // sub_773140

    if code_object.flags[176] & 2 and pass_id > 1:
        RecomputeLoopNesting(code_object)             // sub_789280

    // Iterate from innermost loop outward
    rpo = code_object.rpo_array                       // offset +512
    block_count = code_object.rpo_count               // offset +520
    start = blocks[rpo[block_count]]                  // innermost loop header
    deepest_back = -1

    while start is valid:
        if start has no predecessors or no successors:
            advance; continue

        // Determine loop header and nesting
        header_depth = start.loop_depth               // offset +144
        back_depth = start.loop_depth_equal           // offset +152

        // Find preheader: predecessor with strictly lower loop depth
        preheader = FindPreheader(code_object, start)
        if not preheader:
            continue

        // Analyze loop boundaries
        AnalyzeBoundaries(code_object, header_depth, back_depth, &info)
        if info.has_cross_edges or info.has_breaks:
            continue

        // Query knob 381 for hoisting policy
        policy = QueryKnob381(allocator, 381, start)
        if not ShouldHoist(policy, pass_id):          // pass_id-dependent filter
            continue

        // Find insertion point in preheader
        insert_pt = FindInsertionPoint(code_object, start, preheader)

        // Scan all instructions in the loop body
        for each instruction in loop body (forward order):
            if AllOperandsDefinedOutsideLoop(instruction, header_depth):
                if IsSafeToHoist(instruction):        // no side effects, no barriers
                    MoveInstruction(instruction, insert_pt)  // sub_8F8BC0
                    context.changed = true
                    context.hoisted_count++

        // Post-hoist: optionally re-analyze
        if context.changed and pass_id <= 2:
            if context.hoisted_tex or context.hoisted_cbo:
                RebuildDependencies(code_object)
                RerunAnalysis(code_object, pass_id == 0 ? 1 : -1)
```

### Hoisting Knobs

| Knob Name | Description |
|---|---|
| `HoistBudget` | Maximum number of instructions to hoist per loop |
| `HoistLoopInvBudget` | Budget specifically for loop-invariant hoisting |
| `HoistConservativeScale` | Scale factor for conservative mode (reduces budget) |
| `HoistLate` | Enable/disable late LICM passes (66, 79, 88) |
| `HoistCBOMode` | Constant-buffer-object hoisting mode |
| `HoistCBOLoad` | Enable hoisting of CBO load instructions |
| `HoistCBOFromLoopWithColdNest` | Hoist CBO loads even from loops with cold nesting |
| `HoistCBOHighCostSBInstRatioThreshold` | Scoreboard cost threshold for CBO hoisting |
| `HoistCBOLoadIDOMTravseLimit` | IDOM traversal limit for CBO load hoisting |
| `HoistCBORRegPressureLimitApplyRate` | R-register pressure limit application rate |
| `HoistTexToInstRatioHigh` | High texture-to-instruction ratio threshold for aggressive hoisting |
| `HoistTexToInstRatioLow` | Low texture-to-instruction ratio threshold for conservative hoisting |
| `DisableNestedHoist` | Disable hoisting from nested loops |
| `NestedHoistInnerThreshold` | Inner loop instruction threshold for nested hoisting |
| `NestedHoistOuterThreshold` | Outer loop instruction threshold for nested hoisting |
| `UseNewLoopInvariantRoutineForHoisting` | Use updated invariance check routine |
| `MaxMidHeaderSizeRateForAggressiveHoist` | Header size rate threshold for aggressive hoisting |
| `EnableHoistLowLatencyInstMidBlock` | Hoist low-latency instructions from mid-block positions |
| `MovWeightForSinkingHoisting` | Weight for MOV instructions in sink/hoist decisions |

### GPU-Specific LICM Concerns

**Constant buffer loads.** GPU shaders frequently load from constant buffers (`LDC`). These loads are loop-invariant by definition (the buffer is read-only during kernel execution). The `HoistCBO*` knobs control a specialized path that aggressively hoists these loads, trading register pressure for reduced memory traffic.

**Register pressure vs. occupancy.** Every hoisted instruction extends its live range from the preheader through the entire loop. On GPUs, this directly reduces occupancy. The four LICM passes use increasingly conservative heuristics (controlled by pass_id) to avoid excessive register growth in later pipeline stages where register allocation is imminent.

**Texture instruction hoisting.** Texture fetches (`TEX`, `TLD`, `TLD4`) are high-latency and loop-invariant when their coordinates are loop-invariant. The `HoistTexToInstRatio*` knobs provide thresholds for deciding when to hoist texture instructions -- a tradeoff between reducing loop body latency and increasing preheader register pressure.

---

## Phase 59 -- OriLoopFusion

### Purpose

Fuses adjacent loops with compatible bounds and no inter-loop data dependencies into a single loop. This reduces loop overhead (branch, induction variable update) and creates opportunities for the scheduler to overlap instructions from the formerly separate loop bodies.

### Knobs

| Knob Name | Description |
|---|---|
| `PerformLoopFusion` | Master enable for loop fusion |
| `PerformLoopFusionBudget` | Maximum instruction count in fused body |

### Fusion Criteria

Two adjacent loops `L1` followed by `L2` are candidates for fusion when:

1. **Same trip count.** Both loops iterate the same number of times (same induction variable bounds and stride, or equivalent after normalization).
2. **No violated inter-loop dependencies.** No flow dependence (write in L1, read in L2) that crosses iteration boundaries differently after fusion. Since both loops are sequential pre-fusion, this reduces to: L2 must not read a value written by L1 at a *different* iteration index.
3. **Compatible loop structure.** Both must be single-basic-block bodies (or the fused body must remain within the `PerformLoopFusionBudget` instruction limit).
4. **No intervening barriers.** No `BAR.SYNC`, `MEMBAR`, or fence instructions between the two loop bodies.

### Pipeline Position Rationale

Phase 59 runs after `GeneralOptimizeLate` (phase 58) and before predication (phase 63). This position is chosen because:

- Late expansion (phase 55) may have split a single operation into a pair of loops (e.g., an atomic-reduce pattern becomes a compare loop followed by an exchange loop).
- After fusion, the merged loop body gives predication (phase 63) a larger basic block to work with, improving if-conversion opportunities.
- The subsequent LICM (phase 66) can hoist invariants from the fused loop that were not hoistable from either original loop individually (because they appeared in the "between-loops" region).

---

## Loop Infrastructure Functions

Several utility functions are shared across the loop passes:

| Function | Address | Size | Purpose |
|---|---|---|---|
| `sub_781F80` | `0x781F80` | -- | Rebuild instruction linked list after CFG modification |
| `sub_789280` | `0x789280` | -- | Recompute loop nesting depths (called when `flags[176] & 2` set) |
| `sub_773140` | `0x773140` | -- | Recompute register pressure estimates |
| `sub_7E6090` | `0x7E6090` | 2,614 | Create complex multi-operand instruction (used in unroll body duplication) |
| `sub_7753F0` | `0x7753F0` | -- | Loop peeling setup (splits first/last iterations) |
| `sub_789BE0` | `0x789BE0` | -- | Back-edge canonicalization |
| `sub_74D720` | `0x74D720` | -- | Loop boundary analysis (determines header, latch, exit) |
| `sub_74F500` | `0x74F500` | -- | Find preheader block for a given loop |
| `sub_9253C0` | `0x9253C0` | -- | Edge splitting / preheader block insertion |
| `sub_7A1A90` | `0x7A1A90` | -- | OCG knob query (boolean) |
| `sub_7A1B80` | `0x7A1B80` | -- | OCG knob query (multi-valued) |
| `sub_799250` | `0x799250` | -- | Named-phase DUMPIR check (string match against phase name) |
| `sub_A112C0` | `0xA112C0` | -- | Trigger sub-analysis re-run (liveness, CFG refresh) |
| `sub_BDEA50` | `0xBDEA50` | -- | Back-edge information printer (`bix%d -> backedge's successor BB: %d`) |

---

## Related Passes

| Phase | Name | Relationship |
|---|---|---|
| 3 | `AnalyzeControlFlow` | Builds the CFG, identifies loops, computes dominators -- prerequisite for all loop passes |
| 19 | `OriSplitLiveRanges` | Splits live ranges at loop boundaries to reduce register pressure post-simplification |
| 20 | `PerformPGO` | Applies profile data that informs unrolling and pipelining heuristics |
| 21 | `OriStrengthReduce` | Reduces induction variable strength before unrolling |
| 23 | `GenerateMovPhi` | Inserts SSA phi nodes after unrolling changes the CFG |
| 25 | `StageAndFence` | Inserts memory fences needed by pipelined loops |
| 56 | `SpeculativeHoistComInsts` | Speculatively hoists common instructions above branches (related to LICM) |
| 108 | `OptimizeHotColdInLoop` | Post-RA hot/cold partitioning within loop bodies |
| 138 | `OriSplitHighPressureLiveRanges` | Last-resort splitter when unrolling or LICM caused excessive register pressure |

---

## Cross-References

- [Pass Inventory & Ordering](index.md) -- complete 159-phase table
- [Strength Reduction](strength-reduction.md) -- phase 21, IV simplification before unrolling
- [Predication](predication.md) -- phase 63, creates new LICM opportunities for phase 66
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- phases 85, 87, creates LICM opportunities for phase 88
- [Late Legalization](late-legalization.md) -- phase 78, creates LICM opportunities for phase 79
- [Hot/Cold Partitioning](hot-cold.md) -- phase 108, loop-interior hot/cold splitting
- [Liveness Analysis](liveness.md) -- phases 16, 33, 61, 84 -- liveness drives unroll register pressure
- [Knobs System](../config/knobs.md) -- knob infrastructure, ROT13 encoding
- [Scheduling Architecture](../scheduling/overview.md) -- pipelined loops interact with the instruction scheduler
