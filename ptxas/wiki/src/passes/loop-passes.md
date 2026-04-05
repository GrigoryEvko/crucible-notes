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

**Correction (P1-04):** The W023 report incorrectly listed `sub_83EF00` as the unrolling driver. That function is the MainPeepholeOptimizer (confirmed by p1.06a sweep). The actual unrolling call chain starts at `sub_1392E30`.

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_1392E30` | 25 lines | Phase 22 execute entry: guards, calls initializer + driver + cleanup | HIGH |
| `sub_1389AF0` | 593 lines | Unrolling context initializer: reads all knobs from OCG profile | HIGH |
| `sub_1390B30` | 1,598 lines | Main unrolling driver: per-loop decision, factor selection, dispatch | HIGH |
| `sub_138A6E0` | 774 lines | Post-unroll cleanup: frees working structures | HIGH |
| `sub_7E5120` | 19 lines | Nounroll/skip check: pragma flag, convergence, knob 91 | HIGH |
| `sub_7F5D20` | 99 lines | Rejection recording: indexes string table at `0x21D1EA0` | HIGH |
| `sub_138E3E0` | 125 lines | Loop body scanner: three-pass analysis (header, forward, backward) | HIGH |
| `sub_13858C0` | 42 lines | Loop back-edge locator | HIGH |
| `sub_1385E90` | ~200 lines | Trip count bound extractor (init, limit, stride from IV) | MEDIUM |
| `sub_1383620` | 1,157 lines | Full unroll profitability evaluator (foldable constants, addresses) | MEDIUM |
| `sub_1387C30` | ~400 lines | Partial unroll body replicator | MEDIUM |
| `sub_13880F0` | ~200 lines | Post-unroll CFG fixup | MEDIUM |
| `sub_1385950` | ~300 lines | Induction variable analysis | MEDIUM |
| `sub_138E9C0` | ~400 lines | IV stride/direction verification | MEDIUM |
| `sub_1385CC0` | ~200 lines | IV constant detection | MEDIUM |
| `sub_13829F0` | ~200 lines | Profitability: foldable constant load counting | MEDIUM |
| `sub_A3A7E0` | 1,236 lines | Post-unroll statistics (DUMPIR output) | HIGH |

### Unrolling Decision Algorithm

The unrolling decision is a multi-stage pipeline implemented in `sub_1390B30`. The function iterates over loops in reverse RPO order (innermost first, matching the RPO array at code_object+512) and applies a series of eligibility checks, trip count analysis, factor selection, and profitability evaluation before committing to the unroll.

#### Entry Guard (`sub_1392E30`)

```
function OriLoopUnrolling_Execute(code_object):
    if code_object.flags[1368] & 1 == 0:           // optimization disabled
        return
    if code_object.flags[1397] & 0xC0 == 0x40:     // global nounroll override
        return
    if DUMPIR_skip("LoopUnrolling"):                // sub_799250
        return
    if CountBlocks(code_object) <= 2:               // sub_7DDB50
        return
    if not QueryKnob(487, true):                    // master loop pass guard
        return

    ctx = InitializeContext(code_object)             // sub_1389AF0
    RunUnrolling(ctx)                                // sub_1390B30
    Cleanup(ctx)                                     // sub_138A6E0
```

#### Context Initialization and Knob Defaults (`sub_1389AF0`)

The initializer reads unrolling parameters from the OCG profile object. Each knob uses a three-valued flag: 0 = use hardcoded default, 1 = use integer override, 2 = use float override, 3 = use double override. The defaults recovered from binary:

| Context Field | Profile Offset | Default | Knob Name (inferred) |
|---|---|---|---|
| `ctx+168` (int32) | +31320 | **140** | `UnrollBudget` |
| `ctx+172` (float) | +31032 | **0.25** | `UnrollFlexableFullLimit` |
| `ctx+176` (int32) | +30960 | **4** | `UnrollUnknownCount` |
| `ctx+180` (int32) | +30816 | **4** | `UnrollSmallLoopLimit` |
| `ctx+184` (dbl) | +64656 | **0.4** | `LoopUnrollLargePartOfShaderPct` |
| `ctx+192` (float) | +31392 | **15.0** | `UnrollInstLimit` |
| `ctx+196` (int32) | +64872 | **50** | `UnrollPregThreshold` |
| `ctx+200` (int32) | +31248 | **2** | `UnrollExtraInstPerPercentSaving` |
| `ctx+204` (int32) | +31176 | **200** | `UnrollFullInstLimit` |
| `ctx+208` (int32) | +64296 | **46** | `LoopUnrollNumExtraInstBase` |

Boolean and integer knobs read via vtable dispatch:

| Knob ID | Profile Offset | Default | Knob Name |
|---|---|---|---|
| 437 | +31464 | true | `LoopUnroll` (master enable) |
| 894 | +64368 | true | `LoopUnrollNonInnermost` |
| 897 | +64584 | true | `UnrollMultiBlockLoops` |
| 902 | +64944 | true | `UnrollVariableBounds` |
| 896 | +64512 | 0 | `LoopUnrollFactor` (INT override; 0 = heuristic) |
| 895 | +64440 | 0 | `EpilogueLoopUnrollCount` |
| 900 | +64800 | 0 | `LoopUnrollNumInstTex` |
| 903 | +65016 | false | `DisablePartialUnrollOverflowCheck` |

String knob: knob 427 (profile+30744) returns the `LoopUnrollFactor` per-block override string, with the format `"-N-"` to skip block N, `"+N+"` to force-unroll block N, `"-"` to skip all, `"+"` to force all.

#### Nounroll Pragma Check (`sub_7E5120`)

Returns true (suppress unrolling) when **any** of these conditions hold:

1. **Convergence constraint**: The back-edge analysis context at code_object+1784 is active, and the loop header's entry in the back-edge table (code_object+1776+16) is valid and within the convergence limit. This suppresses unrolling of warp-synchronous loops.
2. **PTX `nounroll` pragma**: Byte 292 of the block descriptor at `(code_object+368 + 8*block_idx)` has bit 1 set. This bit is set during PTX-to-Ori lowering when the `nounroll` pragma string (at `0x1CFE126`) is parsed.
3. **Instruction-level marker**: Byte 283 of the loop header instruction has bit 0 set.
4. **Per-block knob**: OCG knob 91 is set for this block (queried via `sub_7A1A90`).

#### Main Decision Flowchart (`sub_1390B30`)

```
function RunUnrolling(ctx):
    code_object = ctx.code_object

    // Phase 1: Read master enable and per-block override string
    master_enable = QueryKnob(437)                   // LoopUnroll
    override_string = QueryKnobString(427)           // "-N-" / "+N+" format
    RecomputeRegisterPressure(code_object)            // sub_7E6090
    RebuildInstructionList(code_object)               // sub_781F80

    // Phase 2: Pre-scan -- count inlinable calls and non-unrollable instructions
    for each instruction in code_object.instruction_list:
        if opcode == 97 (BRX):
            if callee.entry_block == callee.exit_block:
                inlinable_calls++
                if trip_count > 1:
                    multi_exit |= AnalyzeMultiExit(ctx, callee)

    // Phase 3: Iterate loops in reverse RPO (innermost first)
    rpo_count = code_object.rpo_count                // offset +520
    for idx = rpo_count-1 downto 0:
        block = code_object.blocks[code_object.rpo[idx]]

        // ── Step A: nounroll annotation propagation ──
        if block.nounroll_annotation:                // byte +246
            propagate nounroll to all blocks at >= same nesting depth

        // ── Step B: eligibility filter ──
        if block.loop_depth == 0:          continue  // not a loop
        if block.loop_depth != block.loop_depth_equal: continue
        if block.nounroll and not ctx.force_all:     continue

        // ── Step C: structure analysis ──
        latch = LocateBackEdge(ctx, block)           // sub_13858C0
        if not latch:                    continue
        exit_inst = latch.last_instruction
        if exit_inst.opcode != 95:                   // not conditional branch
            Reject(block, 13); continue              // indirect jump

        // ── Step D: nounroll / convergence check ──
        if CheckNounroll(block, code_object):        // sub_7E5120
            Reject(block, 11); continue

        // ── Step E: execution frequency analysis ──
        freq_header = code_object.freq_table[header_reg]
        freq_latch  = code_object.freq_table[latch_reg]
        is_hot = (freq_latch > 999) and (freq_header > 0)
                 and (freq_latch / freq_header > 3)

        // ── Step F: body analysis ──
        body_info = ScanLoopBody(ctx, block, latch)  // sub_138E3E0
        // body_info contains: tex_count, body_size, foldable_ldc_count,
        //                     has_cross_edges, mem_count
        if body_info.has_cross_edges:    continue

        // ── Step G: budget computation ──
        budget_scale = QueryKnobDouble(898, 0.5)     // default 0.5
        scaled_body = (int)(budget_scale * body_size)
        remaining = total_budget - body_size - scaled_body - ...

        // ── Step H: per-block override check ──
        if override_string:
            needle = "-{block_id}-"
            if override_string == "-" or strstr(override_string, needle):
                continue                             // skip this block
            needle = "+{block_id}+"
            if override_string == "+" or strstr(override_string, needle):
                force_unroll = true

        // ── Step I: pragma force-unroll ──
        if flags[1397] & 0xC0 == 0x80:              // PTX pragma force
            force_unroll = true

        // ── Step J: non-innermost filter ──
        if not ctx.allow_non_innermost and not force_unroll:
            if 10 * body_info.tex_count < remaining:
                Reject(block, 7); continue

        // ── Step K: factor selection ──
        if force_unroll:
            factor = 1 << ctx.force_factor           // power-of-2 override
        else if known_trip_count:
            factor = trip_count
            // Budget-constrain: while factor * body_cost > UnrollBudget:
            //     factor--
            if factor > 4 and trip_count == 1:
                factor &= ~3                         // round to mult-of-4
            if factor <= 1:
                Reject(block, 12); continue
        else:
            if body_size <= 49 and body_info.tex_count > 0:
                factor = 2                           // conservative default
            else:
                factor = max(1, UnrollBudget / body_cost)

        // ── Step L: knob override ──
        if QueryKnob(429):                           // LoopUnrollFactor INT
            factor = GetKnobInt(429)

        // ── Step M: IV analysis ──
        iv_info = AnalyzeIV(ctx, latch)              // sub_1385950
        if not iv_info:             Reject(block, 14); continue
        if not ValidateIV(ctx, iv_info):             // sub_1387870
                                    Reject(block, 14); continue
        bound = ExtractBound(ctx, iv_info)           // sub_1385E90
        if not bound or bound.opcode != 2:
                                    Reject(block, 16); continue
        if bound.def_block.predecessor_count != 1:
                                    Reject(block, 17); continue
        if bound.init_reg == bound.limit_reg:
                                    Reject(block, 18); continue
        stride_ok = VerifyStride(ctx, block, latch, iv_info, bound)
        if stride_ok & 2:          Reject(block, 17); continue
        if stride_ok & 1:          Reject(block, 18); continue

        // ── Step N: detect constant trip count ──
        const_iv = DetectConstantIV(ctx, iv_info)    // sub_1385CC0

        // ── Step O: profitability for full unroll ──
        if factor == trip_count and single_block_body:
            if CheckFoldableProfitability(ctx, block, iv_info, factor):
                ReplicateFullUnroll(ctx, block, factor) // sub_1383620
                stats.unrolled_count++
                continue

        // ── Step P: partial unroll execution ──
        if factor >= 2:
            remainder = trip_count % factor
            iterations_per_copy = (trip_count - remainder) / factor
            block.iterations_per_copy = iterations_per_copy
            if remainder > 0:
                for r = 0 to remainder-1:
                    DuplicateBody(ctx, block)         // sub_932E40
            ReplicatePartialUnroll(ctx, block, latch,
                factor, remainder)                    // sub_1387C30
            stats.unrolled_count++
        else:
            Reject(block, 24)                         // budget exceeded

    // Phase 4: Post-unroll fixup
    stats.non_unrolled = total_loops - stats.unrolled - stats.failed
    if any_unrolled:
        RebuildBackEdges(code_object)                 // sub_7846F0
        RerunLiveness(code_object)                    // sub_A0F020
        RerunControlFlow(code_object)                 // sub_752E40
        MarkModified(code_object)                     // sub_7B52B0
```

### Unroll Rejection Table

When a loop cannot be unrolled, `sub_7F5D20` records the reason by indexing a string pointer array at `0x21D1EA0`. The diagnostic strings contain hex codes like `"0x80000001 - Not unrolled: Irregular loop"` -- these hex values are part of the printed message text, not the internal array index. The W023 report originally described a 36-byte structure table at `0x21D1980`; that table belongs to the operand range lookup in the peephole optimizer (`sub_7E39B0`), not the unrolling pass. The actual internal rejection codes are simple integers indexing the string array:

| Code | Category | Reason |
|---|---|---|
| 7 | Performance | Body too large relative to texture savings (`10 * tex_count < remaining_budget`) |
| 11 | Pragma/knob | PTX `nounroll` pragma, convergence constraint, or per-block knob 91 |
| 12 | Budget | Partial unroll factor reduced to 1 (no factor >= 2 fits within `UnrollBudget`) |
| 13 | Ineligible | Loop exit contains BRX (indirect jump, opcode 95 with special flags) |
| 14 | Unsupported IV | Induction variable analysis failed (`sub_1385950` or `sub_1387870`) |
| 15 | Unsupported IV | IV register class is not integer (class 1) or pointer (class 2/3) |
| 16 | Trip count | Trip count bound extraction failed (`sub_1385E90`) |
| 17 | Irregular | IV definition block has multiple predecessors, or stride/direction verification failed |
| 18 | Trip count | IV initial value register equals IV limit register (degenerate zero-trip loop) |
| 19 | Unsupported IV | IV stride sign inconsistent between loop header and induction increment |
| 24 | Budget | Catch-all: budget exceeded after all factor reduction attempts |

The diagnostic output is gated by `flags[1421] & 0x20` (DUMPIR verbose mode). When enabled, the rejection string is recorded in a hash map keyed by the loop header instruction node, using FNV-1a hashing of the node's block index.

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

**PTX `nounroll` pragma.** The PTX string `nounroll` at `0x1CFE126` is parsed during PTX-to-Ori lowering and sets bit 1 of byte 292 in the block descriptor at `(code_object+368 + 8*block_idx)`. The check is performed by `sub_7E5120`, which also tests three additional suppression conditions: the convergence constraint (back-edge table at code_object+1776), an instruction-level marker (byte 283 bit 0), and per-block knob 91. Any single condition is sufficient to suppress unrolling for that loop (rejection code 11).

**Convergence constraint.** When the back-edge analysis context at code_object+1784 is active (indicating warp-synchronous code), the unroller checks whether the loop header falls within the convergence region. If it does, unrolling is suppressed to avoid breaking warp-level synchronization guarantees. This is particularly important for cooperative groups and ballot-based algorithms.

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

### Two-Layer Pipelining Architecture

ptxas implements software pipelining in two cooperating layers:

1. **Phase 24 (OriPipelining, pre-RA):** Annotates instruction operands with pipeline latency classes, computes the minimum initiation interval (MII), performs the modulo scheduling loop transformation (iteration overlap, prolog/epilog generation). Operates on the Ori IR before register allocation.

2. **Post-RA SoftwarePipeline (`sub_8B9390`, 23KB):** A scheduling algorithm variant within the post-RA instruction scheduler (address range `0x893000`--`0x8FE000`) that performs instruction-level scheduling of already-pipelined loop bodies using physical registers. One of approximately 12 scheduling variants alongside `DualIssueScheduler`, `TensorScheduler`, `LoopScheduler`, `PrefetchScheduler`, etc.

The two layers cooperate: Phase 24 transforms the loop structure (instruction replication, prolog/epilog construction) before register allocation. The post-RA SoftwarePipeline variant handles the cycle-accurate instruction placement of already-pipelined loops.

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_926A30` | 22,116 bytes | Per-instruction operand latency annotator and encoding rewriter | High |
| `sub_91A0F0` | 5,550 bytes | Opcode-to-latency-class classifier (~350 opcodes, 13 distinct classes) | High |
| `sub_9203A0` | 4,881 bytes | Pipeline stage cost calculator (ResMII computation, FP cost accumulation) | Medium |
| `sub_921820` | 1,592 bytes | Prolog/epilog code generator | Medium |
| `sub_9202D0` | 207 bytes | Two-operand pipeline feasibility check (returns 60=reject, 130=accept) | High |
| `sub_91E610` | 399 bytes | Register-class-based latency lookup (class 4→26, class 5/2→20) | High |
| `sub_91E900` | 470 bytes | Pipe-assignment-based stall cycle calculator (32/64 cycle caps) | High |
| `sub_92C0D0` | 358 bytes | Per-instruction annotation wrapper (calls `sub_926A30`, checks opcode changes) | High |
| `sub_92C240` | 8,033 bytes | Extended GEMM-loop pipeliner (SM90+ TMA pipeline depth management) | Medium |
| `sub_8B9390` | 22,841 bytes | Post-RA software pipelining scheduling variant (in scheduler subsystem) | Medium |

**Correction (P1-06):** The original function map listed `sub_926A30` as the "main pipelining engine (modulo scheduling)." Decompilation reveals it is the per-instruction operand latency annotator -- it iterates over each operand of an instruction, calls `sub_91A0F0` to classify the operand's latency class, and rewrites the operand encoding with the latency annotation. The modulo scheduling loop transformation is distributed across the remaining functions, with `sub_9203A0` computing stage costs and `sub_921820` generating prolog/epilog code.

### Software Pipelining Algorithm

#### Phase 1: Operand Latency Annotation

For each instruction in the loop body, `sub_92C0D0` calls `sub_926A30` to annotate operands:

```
function AnnotateOperandLatencies(code_object, instruction):
    opcode = instruction.word & 0xFFFFCFFF      // strip modifier bits (bits 12-13)
    secondary_opcode = instruction.secondary_opcode
    operand_array = instruction.operands         // offset +84
    operand_count = instruction.operand_count    // offset +80

    for i in 0..operand_count-1:
        operand_type = (operand_array[i].word >> 28) & 7
        if operand_type in {2, 3}:               // register or register pair
            // Adjust count for predicated instructions (bit 12)
            adjusted_count = operand_count - 2 * ((opcode >> 11) & 2 != 0)
            if i < adjusted_count:
                latency_class = ClassifyLatency(opcode, secondary_opcode,
                                                operand_array, adjusted_count, i)
                if latency_class != default:
                    RewriteOperandEncoding(operand_array[i], code_object, latency_class)

        // For register operands: call full rewriter sub_922210
        // For non-register operands: call sub_9267C0
```

#### Phase 2: Pipeline Feasibility Filtering

Each instruction is checked by `sub_9202D0`:

```
function CheckPipelineFeasibility(code_object, instruction):
    // Reject instructions with special operand flags
    if (operand_array[1] & 0x603FFFF) != 0 or (operand_array[3] & 0xF8000000) != 0:
        if optimization_level > 1:
            return REJECT                        // return code 60

    // Reject if pipe assignment class <= 3 (control/barrier pipe)
    pipe_class = PipeAssignment(code_object, primary_opcode)   // vtable+904
    if pipe_class <= 3:
        return REJECT

    // Reject if operand 0 and operand 1 have different latency classes
    lat0 = ClassifyLatency(opcode, secondary_opcode, operand_array, count, 0)
    lat1 = ClassifyLatency(opcode, secondary_opcode, operand_array, count, 1)
    if lat0 != lat1:
        return REJECT                            // asymmetric latencies

    // Reject if extended operands have blocking flags
    if operand_count > 2 and (operand_array[4] & 0xF) or (operand_array[4] >> 4) & 1:
        return REJECT

    // Accept: trim to 2-operand form
    result_operands = &operand_array[2]
    result_count = 2
    return ACCEPT                                // return code 130
```

#### Phase 3: MII Computation

The minimum initiation interval is computed as:

```
MII = max(RecMII, ResMII)
```

**RecMII** (recurrence-constrained): The longest data dependence cycle in the DDG divided by the iteration distance it spans. For a cycle of total latency L spanning D iterations: `RecMII = ceil(L / D)`.

**ResMII** (resource-constrained): Computed by `sub_9203A0` using floating-point cost accumulation. The function classifies each instruction's pipe class using a 7-entry pipe class table at `code_object+16` and accumulates per-pipe instruction counts:

```
function ComputeResMII(loop_body, pipe_table):
    pipe_counts[0..6] = {0}
    for each instruction in loop_body:
        lat0 = ClassifyLatency(instruction, operand=0)
        lat1 = ClassifyLatency(instruction, operand=1)
        pipe = MapLatencyToPipe(lat0, pipe_table)    // 7-entry lookup
        pipe_counts[pipe] += cost(instruction)       // FP cost weights

    ResMII = max(pipe_counts[i] / pipe_width[i] for i in 0..6)
```

The pipe class boundaries stored at `code_object+16` define 7 functional unit classes. Each class has a capacity (number of execution slots per cycle). `ResMII` is the maximum ratio of instruction demand to capacity across all pipe classes.

#### Phase 4: Modulo Schedule Construction

```
function ModuloSchedule(loop_body, MII):
    II = MII
    while II <= MAX_II:
        MRT = new ModuloReservationTable(II)     // II rows x pipe_classes columns
        success = true

        for each instruction in priority order:
            earliest = max(data_dependency_constraints)
            latest = earliest + II - 1
            placed = false

            for slot in earliest..latest:
                row = slot mod II
                pipe = instruction.pipe_class
                if MRT[row][pipe] has capacity:
                    MRT[row][pipe] -= 1
                    instruction.scheduled_time = slot
                    instruction.stage = slot / II
                    placed = true
                    break

            if not placed:
                success = false
                break

        if success:
            return (II, schedule)
        II += 1

    return FAILURE                               // could not pipeline
```

#### Phase 5: Prolog/Epilog Generation

Once a valid schedule is found at initiation interval II with S pipeline stages, `sub_921820` generates:

```
function GeneratePrologEpilog(loop, II, num_stages):
    // Prolog: S-1 partial iterations
    for stage in 0..num_stages-2:
        emit instructions assigned to stages 0..stage
        // Each prolog iteration adds one more stage

    // Kernel: steady-state loop body
    emit all instructions from all stages
    // Trip count adjusted: new_trip = original_trip - (num_stages - 1)

    // Epilog: S-1 drain iterations
    for stage in num_stages-2..0:
        emit instructions assigned to stages stage+1..num_stages-1
        // Each epilog iteration removes one stage
```

### Instruction Latency Classifier (sub_91A0F0)

The classifier is a 5.5KB, 1372-line switch statement mapping approximately 350 Ori opcodes to 13 distinct latency class values. It takes five parameters: `(opcode, secondary_opcode, operand_array, operand_count, operand_index)` and returns a class ID -- not a cycle count. The scheduler maps class IDs to actual cycle counts via the hardware profile.

#### Latency Class Table

| Class | Typical opcodes | Meaning |
|---|---|---|
| 1 | Past-end operands, invalid indices | Skip / not used |
| 6 | Simple ALU, bitwise, short integer | Short-pipe latency (~80 opcodes) |
| 7 | Paired register operations | Medium-short (~5 opcodes) |
| 8 | Special cases (via lookup table `dword_21E1340`) | Medium |
| 9 | Type conversions (via lookup table) | Medium |
| 10 | Integer multiply, shifts, `IMAD` | Medium-long (~40 opcodes) |
| 11 | Address computations, `LEA` variants | Medium-long (~15 opcodes) |
| 12 | Memory operations, FP32, barriers | Standard long (~100 opcodes) |
| 14 | Wide memory, atomics, FP64 stores | Extended long (~20 opcodes) |
| 16 | FP64 special variants | Extended long (~3 opcodes) |
| 20 | Texture fetches, uniform loads | Very long (~30 opcodes) |
| 26 | Global memory loads, uncached access | Maximum latency (~25 opcodes) |
| 31 | Scoreboard/barrier-related operands | Special handling (~5 opcodes) |

#### Opcode Family Handling

| Opcode range | Category | Latency behavior |
|---|---|---|
| `0x03`--`0x24` | Integer ALU | Mostly passthrough default; `0x23` always returns 10 |
| `0x3C`, `0x3E`, `0x4E`, `0x4F` | Memory (load/store) | Returns field from `operand_array[4]` bits for operands 0--1 |
| `0x46`, `0xF3`--`0x106` | Texture | Returns 6 normally; 10 for MIO-dependent with extended flag check |
| `0x49`, `0x4A`, `0x51`, `0x143`, `0x15E` | Atomic/reduce | Always returns 12 |
| `0x55`--`0x6F` | Floating-point | Complex per-operand logic; `0x55` uses lookup table `dword_21E1340` |
| `0x5B`, `0x5C`, `0x137` | Barriers/sync | Returns 12 for operand 1, else default |
| `0xB7`, `0x120` | WGMMA setup | Per-operand latency (10--20) based on accumulator flags |
| `0x135` | HMMA/IMMA | Calls `sub_7E39B0`/`sub_7E3A70`/`sub_7E3BA0`/`sub_7E3C30` for matrix latency |
| `0x13D`, `0x13E` | Extended FP | Accumulator-flag-dependent returns (10 or 12) |

### Stall Cycle Calculator (sub_91E900)

`sub_91E900` computes the stall penalty for an instruction by mapping latency classes through the pipe assignment function (`vtable+904`):

```
function ComputeStallCycles(code_object, instruction):
    lat0 = ClassifyLatency(instruction, operand=0)
    pipe0 = PipeAssignment(code_object, lat0)         // vtable+904

    if pipe0 == 8:                                     // long-latency pipe
        stall = StallTable[instruction.index]          // code_object+440
        return min(stall, 64)                          // cap at 64 cycles

    lat1 = ClassifyLatency(instruction, operand=1)
    pipe1 = PipeAssignment(code_object, lat1)

    if pipe1 == 8:
        stall = StallTable[instruction.index]
        return min(stall, 64)

    // Neither operand on long pipe
    stall = StallTable[instruction.index]
    return min(stall, 32)                              // cap at 32 cycles
```

The pipe assignment value 8 corresponds to the long-latency functional unit (memory/texture). Instructions on this pipe get a 64-cycle cap; all others are capped at 32 cycles.

### GEMM Pipelining (sub_92C240)

The `GemmPipeliner*` family of knobs controls a specialized pipelining mode for GEMM (matrix multiply) loops:

| Knob Name | Description |
|---|---|
| `GemmPipelinerEnabled` | Master enable for GEMM-specific pipelining |
| `GemmPipelinerPipelineDepthEnforceDeltaFull` | Pipeline depth adjustment for full enforcement |
| `GemmPipelinerPipelineDepthEnforceDeltaPartial` | Pipeline depth adjustment for partial enforcement |
| `GemmPipelinerDependenciesPopbl` | Dependency resolution policy between DMA and compute stages |
| `GemmPipelinerScoreboardHashPopbl` | Scoreboard hash policy for GEMM barrier tracking |
| `GemmPipelinerUseRegisterCalculation` | Use register-based calculation for pipeline depth vs. fixed |

The extended pipelining in `sub_92C240` (8KB) handles GEMM-like patterns where the loop body contains WGMMA/IMMA instructions. From decompilation:

1. **Activation:** The GEMM pipeliner activates when `code_object+48` (GEMM mode flag) is set and the pipeline context at `code_object+56` has a valid stage range.
2. **Stage iteration:** Iterates from `context+84` (start stage) to `context+88` (end stage), with 96-byte descriptors per stage at `context+136`.
3. **Pipeline depth management:** Uses `sub_8A4DA0` to validate stage depth and `sub_6E6650` for dynamic array resizing when pipeline depth exceeds the current allocation. Writes stage bitmasks (`1 << stage_index`) into the stage descriptor arrays.
4. **Hardware model:** On SM90+ (Hopper), TMA supports up to 8 outstanding asynchronous copy operations. The GEMM pipeliner matches this hardware depth, staging DMA (memory) and compute (math) operations to fill the pipeline.

The DUMPIR diagnostic output includes `For Dma Loop` and `For Math Loop` sections from `sub_7A4500`, confirming the pipeliner explicitly distinguishes between DMA and compute loop stages.

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

**Barrier placement.** Pipelined loops containing `BAR.SYNC` or `MEMBAR` instructions are checked by `sub_9202D0` -- if the pipe assignment class for a barrier instruction is <= 3, the instruction is rejected from pipelining. The latency classifier (`sub_91A0F0`) assigns class 12 to barrier operands (opcodes `0x5B`, `0x5C`, `0x137`), but the feasibility check rejects based on pipe class, not latency class.

**Memory pipeline depth.** The `sub_92C240` extended pipeliner for GEMM-like loops manages the hardware memory pipeline on SM90+. It explicitly tracks DMA pipeline depth using 96-byte per-stage descriptors, resizing arrays dynamically when depth exceeds allocation. The stage descriptor at `context+136 + 96*stage` holds bitmask membership, latency counters, and dependency links.

**Pipe class model.** The 7-entry pipe class table at `code_object+16` partitions the functional units into classes. The post-RA software pipelining variant (`sub_8B9390`) uses the same table to determine which functional unit class each instruction uses, ensuring resource conflict detection is consistent between the two pipelining layers.

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
