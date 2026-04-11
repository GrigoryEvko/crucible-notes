# Loop Passes

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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

| Knob Name | Default | Description |
|---|---|---|
| `LoopInversion` | enabled | Enable loop inversion (do-while to while conversion) |
| `LoopInversionBudget` | unset | Maximum instruction count for loop inversion |
| `LoopPeelInversion` | disabled | Enable loop peeling combined with inversion |
| `EnableSingleThreadPeelingLoops` | unset | Enable peeling for single-thread execution paths |
| `GenPeelingLoopsForSyncs` | unset | Generate peeling loops around sync instructions |
| `AssertIfPeelingLoopForTexSurf` | unset | Assert (debug) if peeling a loop for texture/surface ops |

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
| `sub_1385CC0` | 43 lines | IV constant detection: operand-list register match | HIGH |
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
| `ctx+192` (float) | +31392 | **20.0** | `UnrollInstLimit` |
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
        // body_info 68-byte struct (sub_138C900 accumulates per instruction):
        //   +0  body_size            instruction count (++per insn)
        //   +4  foldable_ldc_count   constant loads foldable after unrolling
        //   +16 tex_count            texture/surface instructions
        //   +24 call_weight          9 per inlinable single-block call
        //   +28 mem_flag_count       instructions with memory flag bit 6
        //   +32 mem_count            memory access instructions
        //   +36 load_count           qualifying load instructions
        //   +40 store_count          qualifying store instructions (opcode 18/12)
        //   +44 has_cross_edges      byte: set if CFG edge leaves loop
        //   +52 branch_count         internal branches (opcode != 18)
        //   +56 single_exit_reg      byte: exit register analysis flag
        //   +60 killed_def_count     register defs killed inside body
        //   +64 inner_nounroll_cnt   inner blocks that are nounroll
        if body_info.has_cross_edges:    continue

        // ── Step G: budget computation ──
        // Knob 898 controls the penalty scale for inner nounroll blocks.
        // Access pattern: profile_obj+64728 (type byte), +64736 (double value).
        // Default 0.5 encoded as IEEE-754 0x3FE0000000000000.
        budget_scale = QueryKnobDouble(898, 0.5)     // default 0.5
        nounroll_penalty = (int)(budget_scale * body_info.inner_nounroll_cnt)
        remaining = body_info.call_weight
                  + body_info.body_size
                  - body_info.foldable_ldc_count
                  - body_info.killed_def_count
                  - nounroll_penalty
        //
        // Binary: v196 = v224 + (DWORD)v221 - HIDWORD(v221)
        //                     - HIDWORD(v229) - v61
        // where v61 = (int)(budget_scale * v230)
        //
        // 'remaining' is an adjusted cost metric: higher means more expensive
        // to unroll.  Used later (Step J) as rejection threshold against
        // 10 * tex_count, and in the factor-selection loop (Step K) to
        // bound the maximum unroll factor.

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

        // ── Step K: factor selection (three budget paths) ──
        if force_unroll:
            factor = 1 << ctx.force_factor           // power-of-2 override
        else if known_trip_count:
            // Full-unroll gate uses UnrollFullInstLimit (hardcoded 200 in
            // binary at LABEL_160), NOT UnrollBudget (140).
            // Three sub-paths depending on trip_count magnitude:
            factor = trip_count
            if trip_count <= 2:                       // (v97 - 1) <= 1
                // Path A: small trip count -- attempt full unroll.
                // Test: 200 / trip_count > body_cost  (lines 652-653)
                if 200 / trip_count <= body_cost:
                    factor = 4 / trip_count          // too large; reduced factor
            else:
                // Path B: trip count > 2 -- factor forced to 0.
                factor = 0                           // will reject or defer to K2
            if factor <= 0:
                Reject(block, 12); continue
            // Note: mult-of-4 rounding (factor &= ~3 when factor > 4 and
            // trip_count == 1) is applied later in the profitability evaluator
            // (sub_1387980 / Step N budget loop), not in initial selection.
        else:                                        // unknown trip count
            // Sub-case K1: compute tex budget threshold
            tex_inst = body_info.tex_inst_count       // DWORD2(v226)
            if QueryKnob(900):                        // LoopUnrollNumInstTex
                tex_budget = ctx.LoopUnrollNumInstTex  // ctx+224
            else:
                tex_budget = min(5 * tex_inst + 22, 100)

            // Sub-case K2: small tex-heavy body (body_size<=49, tex_count>0)
            if body_info.tex_count > 0 and remaining <= 49:
                if is_hot and is_multiblock:
                    if 10 * body_info.tex_count >= remaining:
                        factor = 2                    // tex-dominated small loop
                    else:
                        // fall through to clamp logic (LABEL_152)
                        if factor > 2: factor = 2
                else:
                    // not hot+multiblock: use UnrollUnknownCount
                    factor = 4                        // default UnrollUnknownCount
                    need_epilogue = false

            // Sub-case K3: large body or no textures
            else:
                if tex_budget <= remaining:
                    // body fits within tex budget -> clamp logic
                    if factor > 2: factor = 2
                else:
                    // body exceeds tex budget -> use UnrollUnknownCount
                    factor = 4                        // default UnrollUnknownCount
                    need_epilogue = false

            // Sub-case K4: final allow_non_innermost fallback
            // If factor still 0 after above and allow_non_innermost is set:
            //     factor = 2

        // ── Step L: knob override ──
        if QueryKnob(429):                           // LoopUnrollFactor INT
            factor = GetKnobInt(429)

        // ── Step M: IV analysis ──
        // AnalyzeIV (sub_1385950): traces SSA def chain from a source operand
        // of the latch comparison, pattern-matching IADD3(199)/ISETP(201)/LEA(78)
        // to locate the canonical IV increment instruction.  Returns it or NULL.
        //
        // function AnalyzeIV(ctx, operand, cmp_type):        // sub_1385950
        //   if operand.type != REG: return NULL              // bits 28-30 must be 1
        //   if operand.word1 & 0xFE000000: return NULL       // no extended flags
        //   regs = *(*(ctx) + 88)                            // vreg descriptor array
        //   def = regs[operand.index].def_instr              // vreg+56
        //   if !def: return NULL
        //   opc = def.opcode                                 // instr+72
        //   if (opc-199) & ~2 != 0: return NULL              // must be 199 or 201
        //   if def.op[0].word1 & 0x603FFFF: return NULL      // dest needs clean flags
        //   if opc == 201: goto check_isetp                  // ISETP -> skip ahead
        //   // opc==199 (IADD3): validate & follow op[1]
        //   if op[1].type != REG or not OperandOK(op[1], cmp_type): return NULL
        //   def = regs[op[1].index].def_instr
        //   if !def: return NULL
        //   if def.opcode == 78:                             // LEA -- look through it
        //     if op[1].type!=REG or not OperandOK(op[1],cmp_type): return def
        //     def = regs[op[1].index].def_instr
        //     if !def: return NULL
        //   if def.opcode != 201: return def                 // need ISETP
        // check_isetp:                                       // validate ISETP chain
        //   if op[1].type != REG or op[1].word1 & 1: return def  // negate bit
        //   next = regs[op[1].index].def_instr
        //   if !next or next.opcode != 201: return def
        //   if next.op[1].type!=REG or next.op[1].word1&1: return def
        //   if (def.op[2].type - 2) > 1: return def         // src2 must be P(2)/UR(3)
        //   if not IsLoopInvariant(def.op[2], ctx): return def  // sub_7DB410
        //   if def.op[3].index & 0xFFFFF7 != 5: return def  // cmp-mode filter
        //   return next                                      // the IV increment
        //
        // OperandOK(op, cmp_type):                           // inline filter
        //   if op.word1 & 0x1000000: return false            // pair flag set
        //   if !(op.word1 & 0xFE000000): return true         // no ext flags -> ok
        //   if op.word1 & 0x38000000: return false
        //   t = cmp_type & ~8; return t==5 or t==2           // const or pred type
        iv_info = AnalyzeIV(ctx, latch_operand, cmp_type)     // sub_1385950
        if not iv_info:             Reject(block, 14); continue
        if not ValidateIV(ctx, iv_info):             // sub_1387870
                                    Reject(block, 14); continue
        //
        // ExtractBound (sub_1385E90): traces from the IV instruction backward
        // through the def chain to locate the canonical bound instruction --
        // an IMAD_WIDE (opc 2) or UBMSK (opc 139) whose operands encode
        // {init_reg, limit_reg, stride, direction}.  Returns it or NULL.
        //
        // function ExtractBound(ctx, iv, out_dir, out_init, allow_swap,
        //                       strict_block, chase_copy, strict_stride):
        //   regs = *(*(ctx) + 88)                      // vreg descriptor array
        //   if iv.opcode == 2: goto validate            // fast path
        //   if iv.opcode != 201: return NULL            // must be ISETP
        //   src1 = iv.op[1]
        //   if src1.type != REG or src1.word1 & 0xFF000000: return NULL
        //   def = regs[src1.index].def_instr
        //   // chase MOV copy (opcode 137) when chase_copy flag set
        //   if def and chase_copy and def.opcode == 137:
        //       if def.op[1].type==REG and !(def.op[1].byte99 & 1):
        //           def = regs[def.op[1].index].def_instr
        //   // chase IADD3 carry (opcode 130) when allow_swap set
        //   if allow_swap and def and def.opcode == 130:
        //       if def.op[1].type!=REG or (def.op[1].byte99 & 1):
        //           *out_dir=2; goto try_src2
        //       def = regs[def.op[1].index].def_instr
        //   *out_dir = 2                               // tentative descending
        //   // check iv.op[2] type for immediate/uniform operand
        //   t2 = iv.op[2].type
        //   if t2==CONST(2) or (allow_swap and t2==UR(3)): goto resolve
        //   if t2==UR(3): goto resolve
        //   // fallback: ctx+214 flag, optional sub_7DEC60 filter
        //   if !ctx_byte214: return NULL
        //   if *(*(ctx)+1371) & 0x10:
        //       if sub_7DEC60(&iv.op[2], *(ctx)): return NULL
        //   if iv.op[2].type==REG and !(iv.op[2].word1 & 0xFE000000):
        //       *out_init = iv.op[2].index
        //       if def and def.opcode in {2,139}
        //           and (def.block_id==iv.block_id or !strict_block):
        //           goto validate
        //       *out_dir=1; *out_init=iv.op[1].index   // swap: ascending
        //       def = regs[iv.op[2].index].def_instr
        //   if !def: return NULL
        //   resolve:                                    // look through LEA(78)
        //   if def.opcode == 78:
        //       if def.op[1].type!=REG: return NULL
        //       if def.op[1].word1 & 0x1000000: return NULL  // pair flag
        //       if def.op[0].flags & 0x603FFFF: return NULL
        //       if def.op[1].word1 & 0xFE000000: return NULL
        //       def = regs[def.op[1].index].def_instr
        //       if !def or (def.block_id != iv.block_id
        //           and def.block_id != orig.block_id): return NULL
        //   if def.opcode != 2 and def.opcode != 139: return NULL
        //   validate:                                   // final checks
        //   if def.op[1].type != REG: return NULL
        //   if def.op[0].flags & 0x603FFFF: return NULL // dest clean
        //   if def.op[1].word1 & 0xFE000000: return NULL
        //   if (def.op[2].type - 2) > 1:               // stride not CONST/UR
        //       if strict_stride: return NULL
        //       blocks = *(*(ctx)+296)
        //       depth = blocks[iv.block_id]+148         // nesting depth
        //       if depth<=0 or depth != blocks[def.block_id]+148: return NULL
        //       if !sub_1385E20(ctx, &def.op[2]):       // invariance check
        //           hdr = *(*(ctx)+512)+4*depth          // loop header
        //           if !sub_1385D80(ctx, regs[def.op[2].index],
        //                           blocks[*hdr]): return NULL
        //   return def
        bound = ExtractBound(ctx, iv_info)           // sub_1385E90
        if not bound or bound.opcode != 2:
                                    Reject(block, 16); continue
        if bound.def_block.predecessor_count != 1:
                                    Reject(block, 17); continue
        if bound.init_reg == bound.limit_reg:
                                    Reject(block, 18); continue
        stride_ok = VerifyStride(ctx, block, latch, iv_info, bound)
        //
        // ── sub_138E9C0 pseudocode (VerifyStride) ─────────────────────
        // Params (12): ctx, block, latch, iv_info, bound, init_reg,
        //   limit_reg, init_use_count*, back_edge, extra_reg,
        //   limit_use_count*, direction_score*
        //
        // Walks all instructions in the loop body (linked list from
        // *block through latch, next pointer at instr+8).  For each
        // instruction's operand array (count at instr+80, base at
        // instr+84, 8 bytes per slot):
        //
        //   word = operand_slot[i]   // DWORD at base + 8*i
        //   type = (word >> 28) & 7  // 1 = register operand
        //   neg  = byte at base+8*i+7, bit 0  // negate flag
        //   reg  = word & 0xFFFFFF   // 24-bit vreg index
        //
        // Phase 1 -- zone tracking (per instruction):
        //   If instr == back_edge: past_back_edge=true, disable
        //     both use-counting flags.
        //   If instr == bound: past_bound=false.
        //   not_at_iv = (bound != instr) and (iv_info != instr)
        //
        // Phase 2 -- per-operand DEF/USE classification:
        //   DEF (type==1 and word<0, high bit set):
        //     vreg_table[reg].field_28 = 0  // clear liveness mark
        //     If neg==0 (true def, not negated predicate):
        //       if reg==init_reg and not_at_iv:  result |= 2
        //       if reg==limit_reg:               result |= 1
        //       If direction_score* and vreg[reg].type==5:
        //         insert reg into def_set     // sub_768AB0
        //         if vreg[reg].field_24==1 and
        //             !(vreg[reg].field_48 & 0x40):
        //           running_depth++
        //   USE (type==1 and word>=0 and neg==0):
        //     if reg==init_reg and count_init and not_at_iv:
        //       (*init_use_count)++
        //     if reg==extra_reg and count_limit:
        //       (*limit_use_count)++
        //     If direction_score* and vreg[reg].type==5
        //         and (reg-41)>3:  // exclude predicate regs 41-44
        //       insert reg into use_set       // sub_768AB0
        //
        // Phase 3 -- direction score (only when direction_score*):
        //   Iterate def_set (RB-tree-backed bitvector) via tzcnt.
        //   For each reg, probe use_set via sub_7554F0:
        //     found:     matched++
        //     not found: unmatched++
        //   Final score:
        //     if running_depth>0:
        //       *direction_score = running_depth + unmatched
        //     else:
        //       *direction_score = unmatched + (matched>0 ? 1 : 0)
        //
        // Returns: bitmask. Bit 1 = init_reg redefined in body,
        //   bit 0 = limit_reg redefined.  Unrolling caller passes
        //   last 5 args as (NULL,0,-1,NULL,NULL) -- pure def check.
        //   Software pipelining passes all 12 for full analysis.
        // ──────────────────────────────────────────────────────────────
        if stride_ok & 2:          Reject(block, 17); continue
        if stride_ok & 1:          Reject(block, 18); continue

        // ── Step N: detect constant trip count ──
        const_iv = DetectConstantIV(ctx, iv_info)    // sub_1385CC0
        //
        // DetectConstantIV searches the IV info record's operand list for
        // a register matching a target ID.  The IV info is a linked list;
        // each node carries an operand array at +0x54 (count at +0x50).
        // Each operand is an 8-byte pair {word0, word1}.
        //
        // function DetectConstantIV(ctx, iv_list):      // sub_1385CC0
        //   sentinel = iv_list.head                     // [rsi+0]
        //   node     = iv_list.first                    // [rsi+8]
        //   if node == sentinel: return NULL
        //   target_reg = ctx.block_reg_id               // edx (param a3)
        //   for each node in list until node == sentinel:
        //     count = node.operand_count                // DWORD at node+0x50
        //     if count <= 0: advance; continue
        //     w0 = node.operands[0].word0               // DWORD at node+0x54
        //     if w0 >= 0: advance; continue             // sign bit = has-def flag
        //     for i = 0 to count-1:
        //       w0 = node.operands[i].word0             // 8-byte stride from +0x54
        //       if i > 0 and w0 >= 0: break             // sign bit clear -> stop
        //       type = (w0 >> 28) & 7
        //       if type != 1: continue                  // must be REG (type 1)
        //       pair = node.operands[i].word1 & 0x1000000
        //       if pair: continue                       // pair flag must be clear
        //       reg_id = w0 & 0xFFFFFF
        //       if reg_id == target_reg: return node    // found constant IV definer
        //     advance: node = node.next                 // linked-list ptr at node+0
        //   return NULL

        // ── Step O: profitability gate + full unroll ──
        //
        // Two-stage decision.  sub_13829F0 (CountFoldableOps) scans the
        // loop body and returns nonzero when constant-load or address
        // folding makes full unroll worthwhile.  If it passes, sub_1383620
        // (EvaluateAndEmitFullUnroll, 1157 lines, 14 parameters) performs
        // a deeper profitability check that can still reject (returning 0)
        // and, on acceptance, emits the unrolled code in place.
        //
        // ── sub_1383620 pseudocode (1157 lines, 14 params) ────────────
        // Params: ctx, loop_header, iv_info, factor, block_reg_id,
        //   latch, stride_int(XMM), stride_fp(dbl), [unused], iter_index
        //   (0=single-exit, 1=multi-exit), exit_instr, back_edge,
        //   needs_epilogue, is_outermost
        //
        // Phase 1 — stride classification and overflow guard
        //   exit_opc  = exit_instr->opcode          // field +76
        //   latch_opc = (latch->field_108 >> 0) & 0xFFFFFF
        //   if iter_index == 1:                      // multi-exit
        //       latch_opc = vtable_dispatch(ctx, latch_opc)
        //   iv_dir = classify(exit_opc, latch_opc)
        //       // 2 = ascending,  5 = descending,  13 = unknown
        //   if IsIntegerOp(exit_instr->opcode):      // sub_7D6780
        //       stride_val = ComputeIntStride(exit_instr, ctx)  // sub_7DB140
        //       total_stride = stride_val * (factor - 1)
        //       // overflow check per signedness:
        //       if opc == 11 (signed):   if (factor-1) != total_stride / stride_val: return 0
        //       if opc == 12 (unsigned): if total_stride / (uint)stride_val != factor-1: return 0
        //       if opc == 10 (pointer):  if total_stride / stride_val != factor-1: return 0
        //   else:  // floating-point stride
        //       stride_fp_val = ComputeFPStride(exit_instr, ctx)  // sub_7DB1E0
        //       total_stride = (factor - 1.0) * stride_fp_val
        //
        // Phase 2 — convergence + operand setup
        //   if *(ctx+1784) active and mode==1: suppress direction-swap
        //   build 4-5 InsertAfter anchors (sub_931920 chain)
        //   new_vreg = AllocVReg(ctx, class=5)        // sub_91BF30
        //
        // Phase 3 — body replication
        //   for i = 0 to factor-1:
        //       DuplicateIteration(ctx, ...)           // sub_13832A0
        //
        // Phase 4 — power-of-two epilogue decomposition
        //   // Only when is_outermost AND factor > 2 AND !direction_swap
        //   levels = 31 - CLZ(factor - 1)             // _BitScanReverse
        //   for k = levels downto 1:
        //       emit ISETP+BRA block; replicate body 2^k times
        //       if convergence: update table via sub_13826D0
        //
        // Phase 5 — per-iteration bound comparison emission
        //   // Generates the unrolled comparison chain.
        //   // Integer path: emits ISETP (opcode 0xC9) + BRA (opcode 0x5F)
        //   //   per iteration with adjusted bounds:
        //   //     bound_i = init + stride * i  (via sub_7DAFF0)
        //   // FP path: emits FSETP + BRA with FP bounds.
        //   // Direction flag selects comparison sense:
        //   //   ascending  => 0x5FFFFFFD (LT)
        //   //   descending => 0x5FFFFFFC (GT)
        //
        // Phase 6 — finalization
        //   emit loop-exit branch (ISETP/FSETP for final iteration)
        //   mark block as unrolled: block->flags |= 0x2000
        //   if convergence active:
        //       transfer convergence ownership to unrolled copies
        //   return 1   // success; return 0 only on overflow in Phase 1
        //
        // ─────────────────────────────────────────────────────────────────
        if factor == trip_count and single_block_body:
            foldable = CountFoldableOps(ctx, header, back_edge,
                           block_reg_id, is_single_exit)  // sub_13829F0
            if foldable:
                ok = EvaluateAndEmitFullUnroll(ctx, header, iv_info,
                         factor, block_reg_id, latch, stride_int,
                         stride_fp, ..., iter_idx, exit_instr,
                         back_edge, needs_epilogue,
                         is_outermost)                     // sub_1383620
                if ok:
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
| 12 | Budget | Factor selection yielded 0: known trip count > 2 with no fallback, or body exceeds `UnrollFullInstLimit` (200) for trip counts 1-2; also emitted when profitability evaluator budget loop reduces factor to 1 |
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

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `LoopUnroll` | BOOL | **true** | Master enable for loop unrolling |
| `LoopUnrollFactor` | INT | **0** | Override unroll factor (0 = heuristic) |
| `UnrollBudget` | INT | **140** | Maximum total instruction count after unrolling |
| `UnrollInstLimit` | FLOAT | **20.0** | Maximum instructions in a single unrolled loop body |
| `UnrollFullInstLimit` | INT | **200** | Budget for known-trip-count full unrolling; hardcoded as `200/trip_count > body_cost` gate at LABEL_160 |
| `UnrollFlexableFullLimit` | FLOAT | **0.25** | Flexible full-unroll limit (adjusted by loop characteristics) |
| `UnrollSmallLoopLimit` | INT | **4** | Body size threshold below which loops are always fully unrolled |
| `UnrollPregThreshold` | INT | **50** | Maximum predicate register pressure for unrolling |
| `UnrollMultiBlockLoops` | BOOL | **true** | Allow unrolling of multi-basic-block loop bodies |
| `UnrollVariableBounds` | BOOL | **true** | Allow unrolling when trip count is not compile-time constant |
| `UnrollUnknownCount` | INT | **4** | Default trip count assumption when count is unknown |
| `UnrollUnknownInstLimit` | INT | **0** | Maximum body size for unrolling with unknown trip count |
| `UnrollExtraInstPerPercentSaving` | INT | **2** | Instructions allowed per percent of cycle saving |
| `UnrollTex3DPercentSavedThreshold` | INT | **0** | Minimum savings percent for 3D texture loops |
| `UnrollProfiledColdInstsScale` | INT | **0** | Scale factor for instruction count in profiled-cold blocks |
| `LoopUnrollExtraFoldableLdcWeight` | INT | **0** | Extra weight for foldable constant loads in unroll benefit |
| `LoopUnrollFoldableAddrWeight` | INT | **0** | Weight for foldable address computations |
| `LoopUnrollLargePartOfShaderPct` | DOUBLE | **0.4** | Percentage threshold: loop is "large part of shader" |
| `LoopUnrollNumExtraInstBase` | INT | **46** | Base extra instruction allowance per unroll iteration |
| `LoopUnrollNumInstSmallLoop` | INT | **0** | Instruction count defining "small loop" |
| `LoopUnrollNumInstTex` | INT | **0** | Texture instruction count bonus for unrolling |
| `LoopUnrollSingleLoopSavedPctFactor` | INT | **0** | Savings factor for single-loop shaders |
| `LoopUnrollNonInnermost` | BOOL | **true** | Allow unrolling of non-innermost loops |
| `LoopUnrollUnknownMultiBlock` | BOOL | **false** | Allow multi-block unroll with unknown bounds |
| `EpilogueLoopUnrollCount` | INT | **0** | Unroll count for epilogue (remainder) loops |
| `DisablePartialUnrollOverflowCheck` | BOOL | **false** | Skip overflow check on partial unroll count |

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
| `sub_926A30` | 22,116 bytes | Per-instruction operand latency annotator and encoding rewriter | HIGH |
| `sub_91A0F0` | 5,550 bytes | Opcode-to-latency-class classifier (~350 opcodes, 13 distinct classes) | HIGH |
| `sub_9203A0` | 4,881 bytes | **Constant-folding engine** for FP type conversions (FP32/FP64/FP16/int, IEEE 754 rounding). Previously misidentified as ResMII cost calculator -- see Correction LOOP-11 in Phase 3 and LOOP-12 | LOW |
| `sub_921820` | 1,592 bytes | Compile-time constant-folding evaluator for math intrinsics | HIGH |
| `sub_9202D0` | 207 bytes | Two-operand pipeline feasibility check (returns 60=reject, 130=accept) | HIGH |
| `sub_91E610` | 399 bytes | Register-class-based latency lookup (class 4→26, class 5/2→20) | HIGH |
| `sub_91E900` | 470 bytes | Pipe-assignment-based stall cycle calculator (32/64 cycle caps) | HIGH |
| `sub_92C0D0` | 358 bytes | Per-instruction annotation wrapper (calls `sub_926A30`, checks opcode changes) | HIGH |
| `sub_92C240` | 8,033 bytes | Extended GEMM-loop pipeliner (SM90+ TMA pipeline depth management) | MEDIUM |
| `sub_8B9390` | 22,841 bytes | Post-RA software pipelining scheduling variant (in scheduler subsystem) | MEDIUM |

**Correction (P1-06):** The original function map listed `sub_926A30` as the "main pipelining engine (modulo scheduling)." Decompilation reveals it is the per-instruction operand latency annotator -- it iterates over each operand of an instruction, calls `sub_91A0F0` to classify the operand's latency class, and rewrites the operand encoding with the latency annotation. The modulo scheduling loop transformation is distributed across the remaining functions, with `sub_9203A0` computing stage costs.

**Correction (LOOP-12):** `sub_921820` was originally labeled "Prolog/epilog code generator." Decompilation shows it is a **compile-time constant-folding evaluator** for pipelined loop bodies. It dispatches on the Ori opcode of an instruction whose operand is a known constant, evaluates the operation at compile time using the host math library, and replaces the instruction with the resulting immediate. The dispatch covers 12 opcodes:

| Opcode | Value | Operation | Implementation |
|---|---|---|---|
| 38 | 0x26 | cos | `cos(x)` |
| 215 | 0xD7 | sin | `sin(x)` |
| 59 | 0x3B | exp2 | `pow(2.0, x)` |
| 106 | 0x6A | log2 | `log(x) / ln(2)` (guarded: x > 0) |
| 221 | 0xDD | sqrt | `sqrt(x)` |
| 192 | 0xC0 | rsqrt | `1.0 / sqrt(x)` (with div-by-zero -> infinity handling) |
| 180 | 0xB4 | rcp | `1.0 / x` (with div-by-zero -> infinity handling) |
| 33 | 0x21 | ceil | sign-preserving integer ceiling |
| 68 | 0x44 | floor | sign-preserving integer floor |
| 199 | 0xC7 | cvt.f32.f64 | constant f64-to-f32 narrowing |
| 130 | 0x82 | mov.imm | immediate passthrough (no evaluation needed) |
| 133/137 | 0x85/0x89 | (compound) | delegates to `sub_9216C0` for multi-operand folding |

For rsqrt and rcp, when the result would be infinity (divisor is zero), the function constructs a literal infinity encoding (IEEE 754 `+/-inf` for f32/f16) via `sub_91CDD0`/`sub_91CF00` rather than emitting a division instruction. All successful folds call `sub_91BA60` to write the folded constant back into the operand array and advance the instruction pointer.

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

The DDG that feeds RecMII is constructed during step 3 of the Phase 24 algorithm, after operand latency annotation. Each node is a loop-body instruction; edges carry two weights:

| Edge field | Meaning |
|---|---|
| latency | Pipeline latency between producer and consumer, looked up via `sub_91E900` (stall cycle calculator, 32/64-cycle caps by pipe class) |
| distance | Iteration distance -- 0 for intra-iteration edges, 1+ for loop-carried edges where the def is in iteration *i* and the use is in iteration *i+k* |

Loop-carried edges are detected by matching register definitions against uses whose operand encoding references a register defined in a prior iteration. The Ori IR stores def-use chains in the virtual register descriptor array at `code_object+88`; the pipelining pass walks these chains and marks cross-iteration edges with `distance >= 1`.

**Cycle detection status (P1-06, LOW confidence):** The specific function that enumerates DDG cycles to compute RecMII has not been individually traced in the decompilation. The address range `0x91A0F0`--`0x92C240` was swept; every decompiled function with more than 100 lines was examined. All resolved to operand classifiers (`sub_91A0F0`, `sub_91EFC0`, `sub_91F5A0`), encoding rewriters (`sub_921E60`, `sub_922210`, `sub_926A30`), or cost calculators (`sub_91E900`). No function matching the signature of a graph DFS, SCC decomposition, or explicit cycle enumeration was found.

Two structural observations constrain the implementation:

1. **Single-block loops only.** Phase 24 operates on single-basic-block loop bodies (the feasibility check at `sub_9202D0` rejects multi-operand forms, and multi-block handling is gated to the unroller via `UnrollMultiBlockLoops`). In a single-block DDG where every instruction executes once per iteration, the only cycles are recurrences -- chains where instruction A feeds B feeds ... feeds A across iteration boundaries. The count of such cycles is bounded by the number of loop-carried edges, typically small (1--4 for register recurrences).

2. **Implicit via constraint propagation.** The post-RA SoftwarePipeline variant (`sub_8B9390`) tracks `maxDependencyCycle` (+92) and `maxPredecessorCycle` (+88) in the per-instruction 96-byte scheduling record. These fields propagate forward during the modulo scheduling placement loop: when B depends on A with latency L and distance D, the earliest slot for B is `A.scheduled_time + L - D * II`. If no valid placement exists at the current II, II is incremented and the MRT is rebuilt. This means RecMII is effectively computed as the smallest II for which all recurrence constraints are satisfiable, rather than being pre-computed by a separate cycle-enumeration pass.

**ResMII** (resource-constrained): Computed by accumulating per-pipe FP64 instruction costs and dividing by per-pipe issue width. The process uses `sub_91E610` (which wraps `sub_91A0F0`) to classify each instruction's latency class, then maps the class through `vtable+904` (`PipeAssignment`) to obtain a pipe index into the 7-entry resource table at `code_object+16`.

**Correction (LOOP-11):** The function map (line 672) lists `sub_9203A0` as the ResMII cost calculator. Decompilation reveals `sub_9203A0` (4,881 bytes) is a constant-folding engine for FP type conversions (FP32/FP64/FP16/integer with IEEE 754 rounding modes -- see LOOP-12). The ResMII accumulation is performed inline by the pipelining driver that iterates over the loop body, calling `sub_91E610`/`sub_91E900` per instruction and accumulating into a 7-element FP64 cost vector.

```
function ComputeResMII(loop_body, code_object):
    pipe_counts[0..6] = {0.0}              // FP64 accumulators
    for each instruction in loop_body:
        lat0 = ClassifyLatency(instruction, operand=0)   // sub_91E610
        lat1 = ClassifyLatency(instruction, operand=1)
        pipe = PipeAssignment(code_object, lat0)          // vtable+904
        if pipe > 6: pipe = 6                             // clamp to table size
        pipe_counts[pipe] += cost_weight(lat0)            // FP64 addition

    ResMII = ceil(max(pipe_counts[i] / pipe_width[i] for i in 0..6))
```

**FP cost weights by latency class.** `sub_91A0F0` returns one of 13 distinct latency class codes. Each class contributes a cost weight of 1.0 to its assigned pipe counter (the cost is uniform; the latency class value determines *which* pipe accumulator receives the increment, not the increment magnitude). The latency-to-pipe mapping via `vtable+904`:

| Latency class | Instruction category | Pipe index | Pipe name |
|---|---|---|---|
| 1 | Predicate move, predicated tail | 0 | ALU |
| 4 | FP64 conversion misc | 2 | DFMA |
| 6 | Texture fetch (TEX, TLD, TXQ) | 5 | TEX |
| 7 | FP16 conversion, narrow FP | 1 | FMA |
| 8 | Special cases (lookup `dword_21E1340`) | 8 | SFU |
| 9 | Type conversion (I2F, F2I, I2I) | 0 | ALU |
| 10 | Integer multiply, IMAD, shifts | 0 | ALU |
| 11 | Address computation, LEA | 0 | ALU |
| 12 | Memory ops, FP32, barriers, atomics | 4 | LSU |
| 14 | Wide memory, FP64 stores | 4 | LSU |
| 16 | FP64 special variants | 2 | DFMA |
| 20 | Texture/uniform loads, reg class 5/2 | 5 | TEX |
| 26 | Global memory loads, reg class 4 | 4 | LSU |
| 31 | Scoreboard/barrier operands | 6 | BRA |

**Pipe width values from `code_object+16`.** The 7-entry int32 array at `code_object+16` stores pipe widths (issue slots per cycle per SM sub-partition), populated by the per-SM profile constructor (`sub_8E7300`--`sub_8E97B0`). Representative values for sm_80 (Ampere):

| Index | Pipe name | Width | Meaning |
|---|---|---|---|
| 0 | ALU | 4 | 4 integer ALU dispatch slots per cycle |
| 1 | FMA | 4 | 4 FP32 FMA dispatch slots per cycle |
| 2 | DFMA | 1 | 1 FP64 slot (half-rate or less) |
| 3 | MMA | 1 | 1 tensor core dispatch slot |
| 4 | LSU | 2 | 2 load/store unit dispatch slots |
| 5 | TEX | 1 | 1 texture unit dispatch slot |
| 6 | BRA/SMEM | 1 | 1 control/shared-memory slot |

ResMII is the ceiling of the maximum ratio `pipe_counts[i] / pipe_width[i]` across all 7 classes. For example, a loop body with 8 FP32 instructions and 3 global loads yields `max(8/4, 3/2) = max(2.0, 1.5) = 2`, so ResMII = 2 cycles.

#### Phase 4: Post-RA Software Pipeline Scheduling (`sub_8B9390`)

**Correction (LOOP-09):** Phases 4 and 5 previously contained textbook IMS (Iterative Modulo Scheduling) pseudocode that did not correspond to any function in the binary. The actual implementation is the post-RA scheduling variant `sub_8B9390` (22,841 bytes), which operates **after** Phase 24 has already determined the initiation interval and assigned instructions to pipeline stages. It does not search for an II -- it receives the stage assignment and performs cycle-level instruction placement using physical registers.

`sub_8B9390` takes three parameters: the scheduling context (`ctx`), a loop descriptor (`loop_desc`), and a per-stage bitmask (`stage_mask`). The algorithm has three phases:

```
function SoftwarePipelineSchedule(ctx, loop_desc, stage_mask):
    block_id    = *(loop_desc+28)                     // basic block index
    prologue_sz = block_id * 24                       // offset into stage arrays

    // ── Phase A: cross-iteration dependency registration ──
    if byte(ctx+48):                                  // has_cross_iter_deps
        pipe_ctx = *(ctx+56)
        for stage_id in *(pipe_ctx+84) .. *(pipe_ctx+88):
            iter = IteratorInit(pipe_ctx+40, stage_id) // sub_8AD570
            while iter.valid AND iter.trip_distance > 0:
                dep_node = LookupNode(ctx.dag, block_id) // sub_8A4DA0
                if dep_node AND dep_node.ref_count > 0:
                    for each succ in dep_node.successors:
                        slot = ctx.slot_table[succ.id * 96 + 24]
                        MarkStageDependency(slot, stage_id) // sub_8B5E20
                    dep_record[block_id*24 + stage_id].latency
                        = iter.trip_distance
                iter.advance()                        // sub_8A73B0

    // ── Phase B: per-stage liveness bitmap + instruction dispatch ──
    num_stages = *(ctx+120)
    if byte(ctx+140): num_stages += 1                 // has_epilogue
    total = *(ctx+120) + (byte(ctx+128) == 0) - 1
    for stage_idx in num_stages .. total:
        if stage_mask & (1 << stage_idx) == 0: continue
        slot = *(ctx+264) + (stage_idx << 6)          // 64-byte slot descriptor
        if byte(slot) == 0: continue
        base_lat = ctx.slot_table[block_id*96+8][stage_idx]
                 - *(*(loop_desc+8)+128)[1]
        // Build read/write liveness from compressed bitmaps at slot+8, slot+32
        for each reg in BitmapWalk(slot+8):           // sub_8ACDE0 + sub_8A7330
            ctx.dep_table[block_id*24 + reg].live_in |= (1<<stage_idx)
        for each reg in BitmapWalk(slot+32):
            ctx.dep_table[block_id*24 + reg].live_out |= (1<<stage_idx)
        // Both loops propagate to cross-block successors via sub_8A4820

        // ── Phase C: per-instruction placement ──
        instr = slot.first_instr                      // slot + 16
        while instr != sentinel:
            dep_info = *(instr.sched_node + 112)
            flags    = byte(dep_info + 48)

            // Route 1: modulo-scheduled (bit 4, stage in bits 5-7)
            if (flags & 0x10) AND (flags >> 5) == stage_idx:
                FastPathEmit(ctx, loop_desc, 1<<stage_idx)  // sub_8B9230
                goto next

            // Route 2: cross-iteration carried (bit 0, stage in bits 1-3)
            if (flags & 0x01) AND ((flags>>1) & 7) == stage_idx:
                // 7-class register bank partition (ctx+16 = int[7])
                B = (int*)(ctx+16);  r = encoded_reg_id
                if   r < B[1]: cls=0; base=B[0]
                elif r < B[2]: cls=1; base=B[1]
                elif r < B[3]: cls=2; base=B[2]
                elif r < B[4]: cls=3; base=B[3]
                elif r < B[5]: cls=4; base=B[4]
                elif r < B[6]: cls=5; base=B[5]; tensor=true
                else:          cls=6; base=B[6]

                // Tensor-pipe gate (class 5 only)
                if tensor:
                    prof = *(*(*(ctx[0])+312)+72)
                    mode = byte(prof+5112)
                    if mode==1: tensor &= (*(prof+5120)==0)
                    if mode != 0 AND tensor:
                        FastPathEmit(ctx, loop_desc, 1<<stage_idx)
                        goto next

                BankAwarePlacement(ctx, loop_desc, instr,
                    cls, r - base, 1, stage_idx)      // sub_8B81F0
            next: instr = instr.next
```

The 7-class register bank partitioning (the cascade of comparisons against `ctx+16[0..6]` at decompiled lines 416-460) maps physical register indices to hardware register file banks. The boundaries come from the same pipe class table at `code_object+16` used by the pre-RA ResMII computation, ensuring consistent resource accounting across both pipelining layers. Class 5 receives special treatment: when the hardware profile's tensor-pipe mode flag at `profile+5112` is nonzero, instructions in this bank bypass `sub_8B81F0` and route to `sub_8B9230` (the fast-path tensor emitter that calls `sub_8B8900` / TensorScheduler).

**`sub_8B81F0`** (bank-aware placement) takes seven parameters: `(ctx, loop_desc, instruction, register_class, bank_offset, is_cross_iteration, stage_index)`. From the call graph it invokes `sub_10AF2C0` (latency query), `sub_8B5E20` (dependency edge update), and the scoreboard chain `sub_10AEBC0`/`sub_10AE9A0`/`sub_10AEB30`, confirming it as the core cycle-level conflict resolver.

**`sub_8B9230`** (fast-path emit) bypasses bank classification entirely, directly calling `sub_8B8900` (TensorScheduler) and the scoreboard chain. Used for modulo-scheduled instructions and tensor-pipe class 5 instructions where bank conflicts do not apply.

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

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `GemmPipelinerEnabled` | BOOL | **false** | Master enable for GEMM-specific pipelining |
| `GemmPipelinerPipelineDepthEnforceDeltaFull` | INT | **0** | Pipeline depth adjustment for full enforcement |
| `GemmPipelinerPipelineDepthEnforceDeltaPartial` | INT | **0** | Pipeline depth adjustment for partial enforcement |
| `GemmPipelinerDependenciesPopbl` | BOOL | **false** | Dependency resolution policy between DMA and compute stages |
| `GemmPipelinerScoreboardHashPopbl` | BOOL | **false** | Scoreboard hash policy for GEMM barrier tracking |
| `GemmPipelinerUseRegisterCalculation` | INT | **0** | Use register-based calculation for pipeline depth vs. fixed |

The extended pipelining in `sub_92C240` (8KB) handles GEMM-like patterns where the loop body contains WGMMA/IMMA instructions. From decompilation:

1. **Activation:** The GEMM pipeliner activates when `code_object+48` (GEMM mode flag) is set and the pipeline context at `code_object+56` has a valid stage range.
2. **Stage iteration:** Iterates from `context+84` (start stage) to `context+88` (end stage), with 96-byte descriptors per stage at `context+136`.
3. **Pipeline depth management:** Uses `sub_8A4DA0` to validate stage depth and `sub_6E6650` for dynamic array resizing when pipeline depth exceeds the current allocation. Writes stage bitmasks (`1 << stage_index`) into the stage descriptor arrays.
4. **Hardware model:** On SM90+ (Hopper), TMA supports up to 8 outstanding asynchronous copy operations. The GEMM pipeliner matches this hardware depth, staging DMA (memory) and compute (math) operations to fill the pipeline.

The DUMPIR diagnostic output includes `For Dma Loop` and `For Math Loop` sections from `sub_7A4500`, confirming the pipeliner explicitly distinguishes between DMA and compute loop stages.

### Other Pipelining Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `OkToPipelineNoUnroll` | INT | **0** (disabled) | Allow pipelining even when unrolling was also suppressed |
| `PipelineHoistCondLimit` | INT | **unset** | Maximum condition complexity for hoisting in pipelined loops |
| `PipelineHoistRRegPressureLimit` | INT | **unset** | R-register pressure limit for hoisting inside pipelined body |
| `PipelineHoistPRegPressureLimit` | INT | **unset** | P-register pressure limit for hoisting inside pipelined body |
| `PipelineMIOVQToInstRatio` | DBL | **unset** | MIOVQ-to-instruction ratio threshold for pipeline profitability |
| `PipelineMultiOutputTex` | INT | **0** (disabled) | Enable pipelining of loops with multi-output texture instructions |
| `PipelineSpecUsesInHeadOnly` | INT | **0** (disabled) | Restrict speculative uses to loop header only |

### GPU-Specific Pipeline Concerns

**Warp divergence.** Pipelined loops assume all threads in a warp execute the same number of iterations. If the trip count is warp-divergent, the prolog/epilog handling must account for early-exit threads. The pass checks the varying analysis (phases 53, 70) to determine divergence.

**Barrier placement.** Pipelined loops containing `BAR.SYNC` or `MEMBAR` instructions are checked by `sub_9202D0` -- if the pipe assignment class for a barrier instruction is <= 3, the instruction is rejected from pipelining. The latency classifier (`sub_91A0F0`) assigns class 12 to barrier operands (opcodes `0x5B`, `0x5C`, `0x137`), but the feasibility check rejects based on pipe class, not latency class.

**Memory pipeline depth.** The `sub_92C240` extended pipeliner for GEMM-like loops manages the hardware memory pipeline on SM90+. It explicitly tracks DMA pipeline depth using 96-byte per-stage descriptors, resizing arrays dynamically when depth exceeds allocation. The stage descriptor at `context+136 + 96*stage` holds bitmask membership, latency counters, and dependency links.

**Pipe class model.** The 7-entry int32 array at `code_object+16` stores per-pipe issue widths (slots per cycle), not class boundaries. Each entry is the denominator in the ResMII ratio `pipe_counts[i] / pipe_width[i]`. The post-RA software pipelining variant (`sub_8B9390`) uses the same table to determine functional unit capacity, ensuring resource conflict detection is consistent between the two pipelining layers. See the pipe width table in Phase 3 for concrete values.

---

## Phases 35, 66, 79, 88 -- OriHoistInvariants (LICM)

### Purpose

Hoists computations that produce the same result on every loop iteration out of the loop body and into the preheader. This reduces the dynamic instruction count proportionally to the trip count. The four instances are not redundant -- each targets invariants created by different intervening transformations.

### Function Map

All four instances share the same core implementation:

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_C5FE00` | 34 bytes | Phase 35 execute wrapper | CERTAIN |
| `sub_C5FE30` | 34 bytes | Phase 66 execute wrapper | CERTAIN |
| `sub_C5FE60` | 34 bytes | Phase 79 execute wrapper | CERTAIN |
| `sub_C5FE90` | 34 bytes | Phase 88 execute wrapper | CERTAIN |
| `sub_7DDB50` | 156 bytes | Optimization guard: checks knob 499, block count > 2 | HIGH |
| `sub_8FFDE0` | 573 bytes | HoistInvariants orchestrator: iterates blocks, queries knob 381, dispatches inner worker | HIGH |
| `sub_8FF780` | 1,622 bytes | LICM inner worker: identifies and moves invariant instructions | HIGH |
| `sub_8FEAC0` | 2,053 bytes | Invariance marking: forward/backward operand scan per block | HIGH |
| `sub_8F76E0` | 90 bytes | Per-instruction invariance test: checks output register def-block | HIGH |
| `sub_8F7770` | 810 bytes | Hoisting safety check: operand class + latency analysis | HIGH |
| `sub_8F8CB0` | 658 bytes | Profitability check: budget-weighted score vs latency penalty | HIGH |
| `sub_8F7DD0` | 374 bytes | Transitive invariance propagation through def-use chains | HIGH |
| `sub_8F7AE0` | 558 bytes | Instruction mover: unlinks from loop, inserts at preheader | HIGH |
| `sub_8FF2D0` | 1,186 bytes | Budget computation + invariant marking + hoist dispatch | HIGH |
| `sub_8F8BC0` | 257 bytes | Instruction counting: header/body weight via isNoOp | HIGH |
| `sub_74D720` | 353 bytes | Loop boundary analysis: barrier/jump/predecessor checks | HIGH |
| `sub_74F500` | -- | Preheader location finder | MEDIUM |
| `sub_7DF3A0` | 88 bytes | Opcode flags table lookup (side-effect classification) | HIGH |
| `sub_7E0540` | 156 bytes | Observable side-effect checker (memory, call, barrier) | HIGH |

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

### Guard Function (sub_7DDB50)

Before the LICM core runs, `sub_7DDB50` (156 bytes) gates execution on two conditions:

1. **Knob 499 enabled.** Queries the allocator vtable at +72 for OCG knob 499 (the master LICM switch). If disabled, returns 1 which causes the orchestrator to bail (since 1 <= 2).
2. **Rate limiter.** When knob 499 is enabled, the guard checks a pair of counters at `allocator[9]+35936` (max invocations) and `allocator[9]+35940` (current count). If the current count has reached the maximum, returns 1. Otherwise increments the counter and returns the actual basic block count from `code_object+2104`. This bounds the number of LICM invocations for compile-time control in functions with many loops.
3. **Block count > 2.** The orchestrator (`sub_8FFDE0`) checks the return value: if <= 2, no hoisting is attempted. Single-block functions have no loops; two-block functions have at most a trivial loop not worth processing.

### LICM Invariant Detection Algorithm

The invariance detection pipeline runs inside `sub_8FF2D0` (1,186 bytes), which is called from `sub_8FF780` once per loop nest level. It executes five stages in sequence: budget computation, forward invariance marking, backward non-invariance marking, transitive propagation, and profitability gating.

#### Stage 1: Budget Computation (sub_8FF2D0)

```
function ComputeHoistBudget(context, block, is_simple, num_preds, hoist_mode, is_inner):
    // Base budget from knob 483 (HoistBudget)
    if QueryKnob(483):
        budget = QueryKnobValue(483)                 // 0 = unlimited
    else:
        budget = 10                                  // default

    // CBO budget from knob 482
    if QueryKnob(482):
        cbo_budget = QueryKnobValue(482)
    else:
        cbo_budget = (pass_id == 0) ? 22 : 100

    // Adjust by loop type and depth
    if pass_id > 0 and is_simple:
        budget = (hoist_mode < 2) ? cbo_budget : 300
    else if pass_id == 0 and is_simple:
        budget = (hoist_mode < 2) ? cbo_budget : 200

    // Conservative multiplier for Late3
    if pass_id == 3:
        budget *= 100                                // generous once decided to hoist

    // Split budget among back-edge blocks
    if hoist_mode == 3:                              // processing back-edge block
        budget /= num_preds

    // Inner-loop divisor from knob 380
    if is_inner:
        if QueryKnob(380):
            budget /= QueryKnobValue(380)
        else:
            budget /= 10
```

#### Stage 2: Forward Invariance Marking (sub_8FEAC0, a3=1)

The forward pass iterates every instruction in the basic block and marks each register operand's invariance status based on where it was defined.

```
function MarkInvariants_Forward(context, block_index):
    block = blocks[block_index]
    header_depth = context.header_depth
    loop_depth_range = [context.header_depth, context.max_depth]

    // Two code paths based on knob 934 (UseNewLoopInvariantRoutineForHoisting)
    if QueryKnob(934):
        // Set-based path — see "Set-Based Invariance Alternative" below
        return MarkInvariants_SetBased(context, block_index, forward)

    // Default path: single-pass scan
    for each instruction in block (linked list: block+0 .. sentinel at block+8):
        has_side_effect = isNoOp(instruction)            // vtable+1824
        opcode = instruction+72 (masked: BYTE1 &= 0xCF)
        num_operands = instruction+80

        // Special case: opcode 195 + first dst is reg class 9 (predicate)
        is_predicate_def = (opcode == 195 and dst_reg.class == 9)

        is_invariant = true
        for each operand from LAST to FIRST:             // reverse scan
            operand = instruction + 84 + 8*i
            type = (operand >> 28) & 7

            if type != 1:                                // not a register
                continue                                 // immediates are always invariant

            if IsFixedRegister(operand, code_object):    // sub_7DEB90
                continue                                 // e.g., RZ, PT — always available

            if pass_id == 3:                             // Late3 extra check
                if IsSpecialRegClass(operand, code_object):  // sub_7DA2F0
                    // Exception: IADD3 (opcode 130, flag 0x1000) carry-out
                    if not (opcode == 130 and flag_0x1000 and is_penultimate_operand):
                        continue

            reg = RegisterDescriptor(code_object, operand & 0xFFFFFF)

            if reg.def_block (reg+76) == block_index:
                // Defined in THIS block — not invariant for this loop
                is_invariant = false
            else if context.is_multi_depth:
                def_instr = reg.def_instruction (reg+56)
                if def_instr is null or reg has pinned bit:
                    handle_predicate_invariance()
                else:
                    def_block = blocks[def_instr.block_index]
                    def_depth = def_block.loop_depth (offset +144)
                    if def_depth < header_depth or def_depth > max_depth:
                        reg.use_count (reg+80) = 0       // mark as loop-external
                    else:
                        is_invariant = false
                        reg.def_block (reg+76) = block_index
            else:
                reg.use_count (reg+80) = 0               // simple loop: mark external

        // Side-effect check for the entire instruction
        flags = LookupOpcodeFlags(instruction, code_object)  // sub_7DF3A0
        if (flags & 2) != 0:                             // has memory/control side effect
            is_invariant = false

        if MemoryOverlapsLoopLiveSet(instruction):       // sub_74F5E0
            is_invariant = false

        if is_multi_depth and HasObservableSideEffects(instruction):  // sub_7E0540
            is_invariant = false

        // Mark destination operands
        for each dst_operand (bit 31 set = definition):
            if type == 1 and not pinned:
                if is_invariant:
                    reg.def_block = block_index           // mark for hoisting
                else:
                    reg.use_count += 1                    // count loop-internal uses
```

The key insight is that invariance is determined by **definition site**: if every source register was defined outside the loop (or in a block already processed), the instruction is invariant. Immediates and constants are trivially invariant. The check is not purely structural -- it uses the `reg+76` field which gets updated as hoisting proceeds, allowing transitive invariance discovery.

##### Set-Based Invariance Alternative (knob 934)

When `UseNewLoopInvariantRoutineForHoisting` (knob 934, default **false**) is enabled, `sub_8FEAC0` replaces the single-pass scan with a two-phase set-based algorithm: `sub_768BF0` builds an explicit invariant-register set via fixpoint iteration, then `sub_8F7280` consumes that set to stamp `reg+76`/`reg+80` fields. The caller allocates a 24-byte BST wrapper (freelist + allocator back-pointer + root with `count=2`). When `pass_id == 3`, vtable pair `off_21DD290` is installed, adding the `sub_7DA2F0` register-class filter for IADD3 carry-out exclusion.

**Invariant set data structure.** A BST keyed on `register_id >> 8` (register group). Each 64-byte node stores left/right pointers (`node+0/+8`), group key at `node+24`, and a 256-bit bitmap (4 x u64 at `node+32..+56`). Lookup (`sub_7554F0`): walk by group key, bit-test `node[((id >> 6) & 3) + 4] & (1 << (id & 0x3F))`. Insert (`sub_768AB0`): allocate from freelist at `tree+32+8` or pool at `tree+32+16`, set the bit, balanced-insert via `sub_6A01A0`; duplicate group keys OR into the existing bitmap.

**Phase A -- Fixpoint set construction (`sub_768BF0`):**

```
function BuildInvariantSet(co, block_idx, hdr_depth, max_depth, inv_set, filter, regclass):
    mem_alias_mask = 0
    do:
        changed = false
        for each instruction in blocks[block_idx]:
            all_inv = true
            for each source operand (reverse, type==1, not fixed, not regclass-rejected):
                reg = RegDesc(operand & 0xFFFFFF)
                if InvSet_Contains(inv_set, reg.id):  continue
                if hdr_depth != max_depth:                        // multi-depth loop
                    def = reg.def_instruction;  if def==null or pinned:
                        InvSet_Insert(inv_set, reg.id); changed=true; continue
                    if def_depth outside [hdr_depth..max_depth]:
                        InvSet_Insert(inv_set, reg.id); changed=true; continue
                all_inv = false
            if vtable_1456(instr) or IsPinnedReg(instr) or filter(instr): all_inv=false
            if opcode not in {228,16}: all_inv &= !LookupOpcodeFlags_blocking
            if (flags & 8): mem_alias_mask |= ComputeAlias(instr); changed |= (new bits)
            if (flags & 4) and MemAliasOverlaps(mem_alias_mask, alias): all_inv=false
            for each dst (bit31, type==1, not pinned):
                if all_inv and reg.def_instruction != null:
                    InvSet_Insert(inv_set, reg.id); changed=true
    while changed                                                 // monotone: only adds
```

**Phase B -- Per-instruction classification (`sub_8F7280`):** walks the block once after the set is final. For each register operand, looks up `reg.class_and_id >> 8` in the BST. If the bitmap bit is set: writes `reg.def_block = block_index` (marking invariant). If the bit is clear and the operand is a definition: increments `reg.use_count` (loop-internal use count). On the forward pass (`a3=1`), clears `reg+84` before the lookup; on the backward pass (`a3=0`), preserves it.

**Why two paths exist.** The default single-pass interleaves invariance detection with destination marking. It misses transitive invariance: if instruction A defines R1 and later instruction B uses R1 to define R2, R2 cannot be marked in the same pass. The default delegates this to Stage 4 (`sub_8F7DD0`). The set-based path solves it directly -- once R1 enters the set, the next fixpoint iteration promotes R2. The cost is memory (64-byte BST nodes per register group) and repeated block scans, hence it remains opt-in behind knob 934.

#### Stage 3: Backward Non-Invariance Marking (sub_8FEAC0, a3=0)

The backward pass calls the same `sub_8FEAC0` with `a3=0`. Five behavioral divergences from the forward pass produce a complementary analysis that revokes false-positive invariance and builds the use-count vector consumed by Stage 5.

**Divergence 1 -- No operand pre-clear.** The forward pass zeros `reg.use_count` (offset +84) for every register operand before the main scan (lines 177-188 in decompilation: iterates operands 0..N-1 forward, writes 0). The backward pass skips this loop entirely (`a3 && v9 > 0` is false), preserving use-count values accumulated so far.

**Divergence 2 -- No external-definition marking on source operands.** When a source register's `def_block` does not match `block_index` in single-depth mode, the forward pass writes `reg.use_count = 0` (LABEL_68) to tag the register as loop-external. The backward pass takes the `!a3` branch (line 255-256) and skips to the next operand without modification. In multi-depth mode the forward pass performs a depth-range check and may write both `reg.def_block = block_index` and `reg.use_count = 0`; the backward pass bypasses the multi-depth logic entirely via the same `!a3` guard.

**Divergence 3 -- Unconditional def_block overwrite on destinations.** Both passes reach the destination-marking region through LABEL_26 (the forced non-invariant path triggered by side-effects, memory overlap, or observable effects). At LABEL_27 the pass sets `v17 = a3`. With a3=0, the unconditional path (LABEL_28, lines 374-396) fires: for every register destination that is not pinned, it writes `reg.def_block = block_index` regardless of the register's current def_block value. The forward pass (a3=1) instead takes the conditional path (LABEL_54, lines 354-372) which writes def_block only when `reg.def_instruction` (offset +56) is null. This unconditional overwrite is the core revocation mechanism -- any destination on a non-invariant instruction gets its def_block stamped to the current block, preventing Stage 4 from treating it as hoistable.

**Divergence 4 -- use_count increment on non-invariant destinations.** After destination def_block marking, both passes evaluate `!v17` at LABEL_38 (lines 402-417). Since the backward pass enters with `v17 = 0` (from `v17 = a3 = 0`), it always executes the counting loop: for each register destination where `reg.def_block != block_index`, it increments `reg.use_count`. The forward pass enters with `v17 = 1` and skips this loop. This is the sole path that populates use_count for the profitability scorer.

**Divergence 5 -- Source-match early exit.** When a source register already has `def_block == block_index` (loop-internal definition found during the operand scan), both passes set `v17 = 0` and break from the operand loop (line 293). The forward pass then re-evaluates the `!a3` condition (false), so it must pass through the full side-effect/memory/observable chain before reaching destination marking. The backward pass (`!a3` is true) falls directly into LABEL_25, reaching the same chain but without the conditional guard -- a minor control-flow simplification since the result is the same.

The net effect: Stage 2 (forward) optimistically marks registers whose definitions appear outside the current block and clears use-counts to prepare a blank slate. Stage 3 (backward) pessimistically re-stamps `def_block` on any destination belonging to a non-invariant instruction, and builds use-count for every register that survived both passes. Only registers with `def_block != block_index` after both passes are candidates for hoisting.

#### Stage 4: Transitive Invariance Propagation (sub_8F7DD0)

After the two marking passes, `sub_8F7DD0` propagates invariance transitively through the instruction chain. This handles the case where instruction A is invariant and defines register R, and instruction B uses R and is otherwise invariant -- the forward pass may have marked B as non-invariant because R's definition was in the loop, but A (the definer) is itself invariant.

```
function PropagateInvariance(context, block_index):
    block = blocks[block_index]
    side_effect_mask = 0

    for each instruction in block:
        aliases_memory = CheckMemoryAlias(code_object, instruction)  // sub_74F5E0

        for each operand (type == 1, register):
            reg = RegisterDescriptor(operand)

            if operand is definition (bit 31 set):
                if isNoOp(instruction):
                    if IsInvariant(instruction, block_index):      // sub_8F76E0
                        side_effect_mask |= reg.flags & 0x3
                    else:
                        reg.flags |= aliases_memory ? 1 : 0
                else:
                    reg.flags |= (has_side_effect ? 1 : 0) | 2
            else:  // use
                if has_side_effect:
                    reg.def_block = block_index            // taint defining register
                else:
                    reg.use_count += 1

    return side_effect_mask
```

#### Stage 5: Profitability Check (sub_8F8CB0)

The final gate before hoisting. Computes a cost-benefit ratio and rejects hoisting if the ratio is unfavorable.

```
function IsProfitable(context, block_index, budget, is_hoist_safe):
    header_weight = context.header_insn_count            // from sub_8F8BC0
    body_weight = context.body_insn_count

    // Scoring weights depend on pass aggressiveness and safety
    if is_hoist_safe:
        noOp_weight = (pass_id == 0) ? 60 : 150
        real_weight = 5
    else:
        noOp_weight = (pass_id == 0) ? 12 : 30
        real_weight = 1

    score = 0
    latency_penalty = 0
    instruction_count = 0

    for each instruction in block:
        instruction_count += 1
        if IsInvariant(instruction, block_index):        // sub_8F76E0
            if isNoOp(instruction):
                score += noOp_weight
            else:
                score += 1
                for each dst_operand with scoreboard flag:
                    score += real_weight
                    latency = GetLatencyClass(instruction)  // sub_91E860
                    latency_penalty += (latency > 4) ? 2 : 1
        else:
            for each high-latency dst_operand:
                latency_penalty += (latency > 4) ? 2 : 1

    // Final decision: weighted score vs latency cost
    if pass_id == 0:                                     // aggressive
        denominator = real_weight * instruction_count
    else:
        denominator = body_weight / 3 + header_weight

    return denominator != 0 and (score * budget) / (real_weight * denominator) >= latency_penalty
```

The profitability check encodes a fundamental GPU tradeoff: hoisting reduces dynamic instruction count (proportional to trip count) but extends live ranges (increasing register pressure and reducing occupancy). The `budget` parameter, which varies by 100x between pass_id 0 and 3, controls how aggressively this tradeoff is resolved. Pass_id 0 (Early) uses the smallest denominator, making it easiest to exceed the threshold.

#### Per-Instruction Invariance Test (sub_8F76E0)

The leaf-level invariance test used by stages 4 and 5 is a simple definition-site check:

```
function IsInvariant(instruction, current_block_index):
    num_operands = instruction.operand_count             // inst+80
    if num_operands == 0:
        return false

    // Find the last "interesting" operand (skip immediates/constants)
    // Immediates have type bits in the 0x70000000 range
    last_operand = scan backwards from operand[num_operands-1]
                   while (operand XOR 0x70000000) & 0x70000000 == 0

    // Check: is this a register definition outside the current block?
    if last_operand is negative (bit 31 = definition)
       and type_bits == 1 (register)
       and not pinned (byte+7 bit 0 == 0):
        reg = RegisterDescriptor(last_operand & 0xFFFFFF)
        return reg.def_block (reg+76) != current_block_index

    return false
```

This is the most-called function in the LICM pipeline. It checks whether an instruction's primary output register was defined outside the current block -- if so, the instruction is considered invariant (already hoisted or defined in a dominating block).

### Side-Effect Blocking Rules

An instruction is blocked from hoisting if any of the following conditions hold, regardless of operand invariance:

| Check | Function | Condition |
|---|---|---|
| Memory store | `sub_7DF3A0` | Flags byte bits 2-3 set and bit 5 clear |
| Memory barrier | `sub_74D720` | Opcode 159 (`BAR.SYNC`), 32 (`MEMBAR`), or 271 (barrier variant) |
| Indirect jump | `sub_74D720` | Opcode 236 (`BRX`) |
| Volatile/atomic access | `sub_7DFA80` | Called from `sub_7E0540`; detects volatile or atomic memory |
| Function call | vtable+1456 | `isBarrier()` returns true |
| Texture side effect | `sub_7DF3A0` | Flags byte bit 6 set with operand modifier flag |
| Address-space effect | `sub_7E0540` | Opcodes 85/109 (memory ops) with `(flags+20 & 2) != 0` |

The boundary analysis (`sub_74D720`) also produces a 5-byte result array that gates the entire loop:

| Byte | Meaning | Effect |
|---|---|---|
| 0 | Has external predecessor (outside loop depth range) | Skip loop (not a natural loop) |
| 1 | Non-header block with different nesting | Marks as complex multi-depth loop |
| 2 | Contains barrier instruction | Skip loop entirely |
| 3 | Contains indirect jump | Skip loop entirely |
| 4 | Multi-depth safety flag | AND-ed with `sub_7E5120` per inner block |

### Instruction Counting (sub_8F8BC0)

Before the profitability check, `sub_8F8BC0` counts instructions in the loop header and body separately. It walks the instruction linked list for each block in the loop and classifies each instruction using `isNoOp` (vtable+1824):

- **No-op instruction** (scheduling placeholder, predicate set, etc.): weight **1**
- **Real instruction** (ALU, memory, branch, etc.): weight **30**

The header count is stored at `context+64` and the body count at `context+68`. The profitability formula uses these to normalize the hoisting score: a loop with a heavy header relative to the body benefits less from hoisting.

### Instruction Movement (sub_8F7AE0)

After all checks pass, `sub_8F7AE0` physically moves each invariant instruction from the loop body to the preheader:

1. **Invariance re-check.** Calls `sub_8F76E0` one final time per instruction. Instructions whose invariance status changed during the marking passes are skipped.
2. **Knob 484 gate.** Queries the allocator for knob 484; if disabled, no movement occurs. This provides a fine-grained override separate from the loop-level knob 381.
3. **Preheader creation.** On the first hoisted instruction, creates or locates the preheader block:
   - If the loop has an existing preheader block (`context+16` non-null): clones it via `sub_931920`, copies convergence flags from the original preheader's `offset+282 bit 3`, and links it into the CFG via `sub_8F7610`.
   - If no preheader exists: creates a new block via `sub_92E1F0` and links it.
4. **Unlink and reinsert.** For each invariant instruction:
   - `sub_9253C0(code_object, instruction, 1)`: unlinks the instruction from the current block.
   - `sub_91E290(code_object, instruction)`: inserts at the preheader insertion point.
   - Updates the Ori instruction's control word at `instruction+32` (not the SchedNode): sets bit 1 at byte offset +13 to mark the instruction as hoisted (prevents the scheduler from reordering it back into the loop).
5. **Destination register tracking.** For each output operand, if the defining instruction at `reg+56` differs from the current instruction, sets `context+44` (hoisted_cbo flag). For pass_id == 2, additionally sets `reg+48 bit 26` if the register class is in {2, 3, 4} (GPR classes) and the preheader has the convergence flag.
6. **Special IADD3 handling.** For pass_id == 3, instructions with opcode 130 (`IADD3`), flag `0x1000`, and a negative byte at `+90` (carry chain) receive special treatment via `sub_9232B0` which adjusts the carry-out register linkage before movement.

### Multi-Depth Loop Handling

For loops with nesting depth > 1 (inner loops within the hoisting target), `sub_8FF780` performs multiple rounds of `sub_8FF2D0` calls:

1. **Header block.** First call processes the loop header with `hoist_mode = 0`.
2. **Intermediate blocks.** For each depth level between `header_depth+1` and `max_depth`, checks if the block's parent depth (`block+148`) matches the header depth. If the block is a back-edge predecessor of the loop header, uses `hoist_mode = 3`. Otherwise, checks a dominance bitmap at `block[25] + 4*(depth >> 5)`: if bit `(1 << depth)` is set, uses `hoist_mode = 1` (dominated); otherwise `hoist_mode = 2` (non-dominated).
3. **Back-edge block.** Final call with `hoist_mode = 3` and the deepest back-edge block index, ensuring the budget is split among back-edge predecessors.

Multi-depth permission is gated by knob 220 (queried at `allocator[9]+15840` for the fast path) and the `DisableNestedHoist` knob. When hoisting from an inner loop to the header of an outer loop, an additional constraint applies:

```
allow_nested = allow_nested_hoist AND is_simple_loop
               AND body_insn_count > 1
               AND num_predecessors == 1
               AND body_insn_count < header_insn_count * max_iterations
```

This prevents hoisting from inner loops where the cost (extended live range across multiple loop levels) exceeds the benefit (reduced inner-loop dynamic count).

### LICM Outer Loop (sub_8FF780)

The complete outer driver that iterates over all loop nests:

```
function HoistInvariantsCore(context):
    code_object = context.code_object
    pass_id = context.pass_id

    // Read iteration limit from allocator+34632
    config_byte = allocator[34632]
    max_iterations = (config_byte == 0) ? 2
                   : (config_byte == 1) ? allocator[34640]
                   : 0                                   // unlimited

    allow_nested_hoist = (allocator[20016] != 0)

    // Prepare IR
    RebuildInstructionList(code_object, 1)               // sub_781F80
    RecomputeRegisterPressure(code_object, 1, 0, 0, 0)  // sub_7E6090
    RecomputeLoopDepths(code_object, 0)                  // sub_773140

    if code_object.flags[176] & 2 and pass_id > 1:
        RecomputeLoopNesting(code_object)                // sub_789280

    // Clear prior invariance markers
    for each block in instruction list:
        block.marker (offset +76) = 0xFFFFFFFF

    // Iterate from innermost loop outward (last RPO entry first)
    current = blocks[rpo[block_count]]

    while current is valid:
        if current has no predecessors or no first instruction:
            advance; continue

        // Count predecessors at >= current loop depth
        header_depth = current.loop_depth                // offset +144
        for each predecessor:
            if pred.loop_depth >= header_depth:
                num_at_depth++; track deepest back-edge index

        if num_at_depth == 0:                            // not a loop header
            advance; continue

        // Simple vs multi-depth
        if max_depth == header_depth:
            is_simple = true
        else:
            info = AnalyzeBoundaries(code_object, header_depth, max_depth)
            if has_external_pred or has_barrier or has_indirect_jump:
                advance; continue
            if !MultiDepthAllowed(knob_220):
                advance; continue
            context.is_multi_depth = true

        // Find preheader and query knob 381
        context.insert_pt = FindPreheader(code_object, current, ...)
        if !ShouldHoist(QueryKnob381(381, current), pass_id, opt_level):
            advance; continue

        // Count instruction weights
        CountInstructions(context)                       // sub_8F8BC0

        // === CORE HOISTING PIPELINE (per loop) ===
        sub_8FF2D0(context, header_block, ...)           // header block

        if context.is_multi_depth:
            for depth in (header_depth+1 .. max_depth-1):
                sub_8FF2D0(context, block_at_depth, ..., hoist_mode, ...)
            sub_8FF2D0(context, back_edge_block, ..., 3, ...)  // back-edge

        // Post-hoist cleanup
        if context.changed and current.num_back_edge_successors > 1:
            RebuildInstructionList(code_object, 0)

        advance to next loop
```

### Hoisting Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `HoistBudget` | FLOAT | **10** | Maximum number of instructions to hoist per loop (0 = unlimited) |
| `HoistLoopInvBudget` | FLOAT | **22** (early) / **100** (late) | Budget specifically for loop-invariant hoisting; pass_id 0 uses 22, pass_id > 0 uses 100 |
| `HoistConservativeScale` | INT | **10** (divisor) | Inner-loop budget divisor; budget /= scale when hoisting from inner loops |
| `HoistLate` | INT | per-block policy | Per-block hoisting policy (0=always, 1=inner only, 3=never) |
| `HoistCBOMode` | INT | **0** | Constant-buffer-object hoisting mode |
| `HoistCBOLoad` | INT | **unset** | Enable hoisting of CBO load instructions |
| `HoistCBOFromLoopWithColdNest` | INT | **1** (enabled) | Hoist CBO loads even from loops with cold nesting |
| `HoistCBOHighCostSBInstRatioThreshold` | INT | **unset** | Scoreboard cost threshold for CBO hoisting |
| `HoistCBOLoadIDOMTravseLimit` | INT | **4** | IDOM traversal limit for CBO load hoisting |
| `HoistCBORRegPressureLimitApplyRate` | INT | **80** | R-register pressure limit application rate (percentage) |
| `HoistTexToInstRatioHigh` | DBL | **0.045** | High texture-to-instruction ratio threshold for aggressive hoisting |
| `HoistTexToInstRatioLow` | DBL | **0.03** | Low texture-to-instruction ratio threshold for conservative hoisting |
| `DisableNestedHoist` | BOOL | **false** | Disable hoisting from nested loops (false = nested hoisting allowed) |
| `NestedHoistInnerThreshold` | INT | **22** / **100** | Inner loop instruction threshold for nested hoisting (same value as `HoistLoopInvBudget`) |
| `NestedHoistOuterThreshold` | INT | **10** | Outer loop instruction threshold for nested hoisting (same value as `HoistBudget`) |
| `UseNewLoopInvariantRoutineForHoisting` | BOOL | **false** | Use updated set-based invariance check routine (legacy single-pass is default) |
| `MaxMidHeaderSizeRateForAggressiveHoist` | INT | **2** | Maximum LICM iteration count (limits repeated hoisting passes) |
| `EnableHoistLowLatencyInstMidBlock` | BOOL | **false** | Hoist low-latency instructions from mid-block positions |
| `MovWeightForSinkingHoisting` | DBL | **0.25** | Weight for MOV instructions in sink/hoist decisions |

### GPU-Specific LICM Concerns

**Constant buffer loads.** GPU shaders frequently load from constant buffers (`LDC`). These loads are loop-invariant by definition (the buffer is read-only during kernel execution). The `HoistCBO*` knobs control a specialized path that aggressively hoists these loads, trading register pressure for reduced memory traffic.

**Register pressure vs. occupancy.** Every hoisted instruction extends its live range from the preheader through the entire loop. On GPUs, this directly reduces occupancy. The four LICM passes use increasingly conservative heuristics (controlled by pass_id) to avoid excessive register growth in later pipeline stages where register allocation is imminent.

**Texture instruction hoisting.** Texture fetches (`TEX`, `TLD`, `TLD4`) are high-latency and loop-invariant when their coordinates are loop-invariant. The `HoistTexToInstRatio*` knobs provide thresholds for deciding when to hoist texture instructions -- a tradeoff between reducing loop body latency and increasing preheader register pressure.

---

## Phase 59 -- OriLoopFusion

### Purpose

Fuses adjacent loops with compatible bounds and no inter-loop data dependencies into a single loop. This reduces loop overhead (branch, induction variable update) and creates opportunities for the scheduler to overlap instructions from the formerly separate loop bodies.

### Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `PerformLoopFusion` | INT | **0** (disabled) | Master enable for loop fusion; must be explicitly set to a nonzero value |
| `PerformLoopFusionBudget` | FLOAT | **unset** | Maximum instruction count in fused body |

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
