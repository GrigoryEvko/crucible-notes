# Shader Constant Extraction

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`ExtractShaderConsts` is the ptxas pass that identifies values which are warp-uniform-and-loop-invariant (CTA-invariant across an entire kernel invocation, more precisely) and rewrites their per-thread computation into a single load from constant memory. It runs **twice** at fixed pipeline positions — phase 34 (`ExtractShaderConstsFirst`) and phase 51 (`ExtractShaderConstsFinal`) — but both positions dispatch into one shared implementation, `sub_1C72640` (4,582 bytes, 171 basic blocks, 37 outgoing calls). The two wrappers `sub_C5FDA0` (phase 34) and `sub_C5FDD0` (phase 51) differ in exactly one parameter — a single byte passed as the second argument selects whether the call site is "first" (0) or "final" (1) — and that flag controls one additional finalization step inside the engine. Everything else, including the candidate-collection state machine, the rank-and-select loop, and the IR rewrite, is identical between the two runs.

The pass exists because the partial-SSA IR window between phases 23 and 73 deliberately preserves expressions that *look* uniform but were either materialised inline by an earlier lowering (kernel parameter arithmetic, surface descriptor reads, sampler binding indices, ABI register staging) or exposed as uniform only after a preceding analysis. Shader-const extraction is what closes the gap: it intercepts those expressions, allocates a constant-bank slot, and replaces the original compute chain with a `LDC c[bank][offset]` that the back end can broadcast to every lane via the uniform-datapath registers.

| | |
|---|---|
| **Phase indices** | 34 (`ExtractShaderConstsFirst`), 51 (`ExtractShaderConstsFinal`) |
| **Binary phase indices** | 39, 59 |
| **Category** | Optimization (per `passes/index.md`) |
| **Entry wrapper — first** | `sub_C5FDA0` (34 B, 4 BBs) — calls `sub_1C72640(ctx, 0, ...)` |
| **Entry wrapper — final** | `sub_C5FDD0` (34 B, 4 BBs) — calls `sub_1C72640(ctx, 1, ...)` |
| **Opt-level gate** | `sub_7DDB50` (156 B, leaf) — `getOptLevel(ctx) > 1` required to enter |
| **Shared engine** | `sub_1C72640` (4,582 B, 171 BBs, 37 callees) |
| **IR scanner / scratch builder** | `sub_1BD9200` (2,456 B, 46 BBs) — populates 688-byte `v150` scratch |
| **Scratch teardown** | `sub_1BD8620` (3,040 B, 140 BBs) — symmetric destructor for `v150` |
| **Candidate collector** | `sub_1C6F590` (1,957 B, 99 BBs) — walks instructions, seeds candidates |
| **Ranker** | `sub_1C6DD40` (4,013 B, 209 BBs) — bin-packing + cost model |
| **Rewriter** | `sub_1C6A230` (949 B, 60 BBs) — emits `LDC` and unlinks dead chain |
| **Optional xmm sub-pass** | `sub_1C72370` (707 B, 28 BBs) — runs only when bitvector at `v159` non-empty |
| **Final-position finalize** | `sub_1C68760` (1,018 B, 11 BBs) — runs only when `a2 != 0` |
| **Knob — master** | 487 (general optimization enable; same gate as `OriLinearReplacement`) |
| **Knob — body-size threshold** | 453 (used to clamp `v17` before comparing against `min_body_size`) |
| **Stack frame** | 1,392 bytes (one of the largest in the optimizer; 688 B alone are the `v150` IR-scratch) |
| **Pipeline window** | Partial-SSA (phases 23–73); both runs land inside |
| **Data-table ref** | `0x21DBEF8` — 91-entry destruction-vtable used by the per-pass cache record allocator at the post-step validation |

## What "Shader Constant" Means Here

The "shader" in the pass name is historical — ptxas inherits the term from the graphics-pipeline ancestor of the cubin format, where constant banks carried texture-sampler bindings, vertex-attribute strides, and material parameters that were *constant for the duration of a draw call* but had to be loaded out of memory rather than encoded inline. In the CUDA compilation pipeline, the same machinery now hoists three structurally identical classes of value:

1. **CTA-invariant kernel parameters and their derivatives.** `ld.param` lowers to `LDC c[0][offset]`; the result is broadcast to every lane. Any uniform arithmetic chain rooted in `LDC c[0]` is a candidate for re-extraction into a higher constant bank index if downstream cost analysis predicts gains.
2. **Surface and sampler descriptors.** Reading a 32-byte texture descriptor from `c[bank][offset]` produces a uniform-by-construction value whose subsequent address arithmetic (mipmap-level stride, slice offset, array stride) is also uniform.
3. **Compile-time-known launch parameters.** `gridDim`, `blockDim`, and other launch attributes the host driver patches into `c[0]` before the kernel starts. Same propagation rule.

The pass does not invent new constants; it only re-bins existing ones. Specifically: when an expression tree rooted in `c[bank_A][offset]` produces a derived value that is *also* warp-uniform and that is used at three or more dynamic sites (the threshold is `v18 > 3` in the gate at line `1c7281c`), the engine allocates a new constant-bank slot in `c[bank_B]`, materialises the derived value once at module-load time via the constant-bank pre-staging mechanism, and rewrites every use site to a single-instruction `LDC` from `c[bank_B]`.

Confidence: **MED** on the three-class taxonomy (each class has a distinct seeding rule in `sub_1BD9200` and a distinct cost weight in `sub_1C6DD40`, but the names are reconstructed from the constant-bank index conventions documented in `cubin/constant-banks.md`, not from any string the binary embeds).

## Why Two Pipeline Positions

Both runs share one engine; what differs is the *state of the IR* when each runs.

| Wrapper | Phase | Pipeline neighbours | What changes vs. the other position |
|---|---:|---|---|
| `sub_C5FDA0` | 34 (`ExtractShaderConstsFirst`) | After `OriPerformLiveDeadSecond` (33), before `OriHoistInvariantsEarly` (35) | First chance to catch easy candidates immediately after early-DCE has eliminated the obvious noise. `a2 = 0` ⇒ the finalize sub-step is **skipped**. The pass is permitted to leave partially-rewritten chains for the second position to clean up. |
| `sub_C5FDD0` | 51 (`ExtractShaderConstsFinal`) | After `OriReassociateAndCommon` (50), before `OriReplaceEquivMultiDefMov` (52) | Reassociation has just exposed new uniform sub-expressions that were not syntactically uniform on the first run. `a2 = 1` ⇒ the finalize sub-step (`sub_1C68760`) **runs**, committing all pending extractions to the constant bank and closing the per-function bookkeeping. |

The seventeen phases between the two positions include the most aggressive expression-rewriting work of the entire optimizer — strength reduction (21), pipelining (24), loop unrolling (22), the linear replacement engine (31), the macro-instruction creator (8), and the GvnCse/reassociate pair (49–50). Every one of these can synthesise new uniform expressions that did not exist when phase 34 ran. Running shader-const extraction only at phase 51 would miss the candidates that the early passes (35–48) want to consume; running only at phase 34 would miss everything the late-rewriters expose. The two-position design is the standard "snapshot now, refine later" pattern that ptxas uses across the partial-SSA window.

> ⚡ **QUIRK — one engine, two pipeline positions, single byte selector**
> The two wrappers `sub_C5FDA0` and `sub_C5FDD0` are byte-for-byte identical except for the literal `0` vs `1` passed as `a2`. The engine `sub_1C72640` reads `a2` exactly once — at the gate where it controls whether `sub_1C68760` (the finalize) runs (line `1c72cb2`). Every other piece of behaviour is identical between the two calls. A re-implementation that treated each phase as a distinct pass would duplicate ~4.5 KB of code; the ptxas convention is to keep the body unique and parameterise the call site. Confidence: **HIGH** (visible in both wrapper decompilations side-by-side).

## Algorithm

The body of `sub_1C72640` is a textbook three-stage rewrite — *gate / collect / commit* — with each stage further subdivided into knob checks, IR scratch allocation, and worklist drain.

```c
// Distilled from sub_1C72640 (decompiled/sub_1C72640_0x1c72640.c).
// Argument: a CodeObject* (the per-function IR container), a "is final position" flag.
// Returns: a single byte (last value written to v5).
// Side effects: rewrites use sites of uniform expressions into LDC from constant bank.

char ExtractShaderConsts(CodeObject *ctx, char is_final_pos,
                          double _unused_a3, __m128i _unused_a4, __m128i _unused_a5) {
    // ── Stage 0: shader-info presence check ─────────────────────
    // *(ctx+1584) is the OptionsMgr; *(om+376) is the shader-info pointer.
    // No shader info → not a graphics-pipeline kernel that this pass targets → bail.
    ShaderInfo *si = *(ShaderInfo**)((char*)*(void**)(ctx + 1584) + 376);
    if (!si) return 0;

    // Cache the BB delta from the shader-info counters.
    int bb_lo = *(int*)(si + 16);          // entry-block count or low watermark
    int bb_hi = *(int*)(si + 20);          // exit-block count or high watermark

    // ── Stage 1: knob 487 — master optimization gate ────────────
    // Knob 487 is the same "general optimization" master gate that gates
    // OriLinearReplacement (phase 31) and the rest of the Ori family.
    OptionsMgr *om = *(OptionsMgr**)(ctx + 1664);
    bool opt_enabled = query_knob(om, 487);
    if (opt_enabled) {
        // Per-iteration counter at *(om_state + 35076) bumped against
        // ceiling at *(om_state + 35072). When the budget is exhausted,
        // the gate falls back to the size-threshold path below.
        int used = *(int*)(om_state(om) + 35076);
        if (*(int*)(om_state(om) + 35072) > used) {
            *(int*)(om_state(om) + 35076) = used + 1;
            goto stage_2;     // budget available, proceed
        }
    }
    // Knob 487 not enabled or budget exhausted: fall back to size-only test.

    // ── Stage 2: knob 453 — minimum-body-size gate ──────────────
stage_2:
    bool size_gate_set = query_knob(om, 453);
    int span = bb_hi - bb_lo;
    if (size_gate_set) {
        int min_span = query_knob_value(om, 453);    // e.g. 16 BBs
        if (span > min_span) span = min_span;        // clamp
    }
    int body_size = span - *(int*)(ctx + 928);       // 928 = function preamble size

    // The "is it worth running?" threshold. Below this size, the constant-
    // pool pressure of inserting an extra LDC dominates the savings of
    // hoisting any candidate, so we bail before even allocating scratch.
    if (body_size <= 3) return 0;

    // ── Stage 3: per-function reentrancy guard ──────────────────
    if (*(uint8_t*)(ctx + 936)) return 0;             // already processed this fn

    // ── Stage 4: scratch allocation ─────────────────────────────
    // The engine builds 8 parallel intrusive-list / set / bitvector
    // structures, each rooted at a stack slot. sub_1C68B60 / sub_1C690B0 /
    // sub_1C68BE0 / sub_1C68C60 / sub_1C68CE0 / sub_1C68D60 / sub_1C69030
    // are the seven constructors (each is a 119-byte leaf that zeroes a
    // record and links it onto the pass-local allocator). sub_6E6650
    // resizes the two bitvectors at v161 and v176 to one bit per vreg
    // (read from *((dword*)ctx + 130) = current vreg count).
    Worklist     candidates;     /* v134 = sub_1C68B60(...) */
    PendingSet   pending;        /* v131 = sub_1C690B0(...) */
    ChainMap     chains;         /* v146 = sub_1BD9200(v150, ctx, ...) */
    TreeMap      trees;          /* v154 = sub_1C68BE0(...) */
    BankMap      banks;          /* v158 = sub_1C68C60(...) */
    BitVector    seen_vregs;     /* v161, sized to (n_vregs + 64) / 64 */
    UseSet       uses;           /* v168 = sub_1C68CE0(...) */
    BitVector    cand_vregs;     /* v176, sized identically to seen_vregs */
    EmitQueue    emit_q;         /* v179 = sub_1C69030(...) */
    char         analysis_scratch[688];   /* v150 — IR-walk staging */

    init_scratch_records(&candidates, &pending, &chains, &trees,
                         &banks, &seen_vregs, &uses, &cand_vregs, &emit_q);
    bitvec_resize(&seen_vregs, (n_vregs(ctx) + 64) / 64);
    bitvec_resize(&cand_vregs, (n_vregs(ctx) + 64) / 64);
    sub_1BD9200(analysis_scratch, ctx, /*opts=*/&off_23FAF70);   // IR walker

    // ── Stage 5: three-pass main work ───────────────────────────
    // The candidate collector / ranker / rewriter triple. Each call
    // takes the in-progress state by reference; the worklist of
    // surviving candidates rides in `candidates` (v134) and is drained
    // by the rewriter.

    if (body_size > 3) {
        // 5a. Collect: walk the IR, identify uniform expression trees
        //     rooted in either an LDC operand or a kernel-parameter
        //     register, and stage them in `candidates`.
        sub_1C6F590(&candidates, ...);

        // 5b. Rank: score every candidate. The cost model weighs:
        //     - Number of dynamic uses (loop trip × static use count)
        //     - Depth of the expression tree (deeper = more savings)
        //     - Constant-bank pressure (running tally of slots already used)
        //     - SM-tier-specific bank capacity caps
        //     Survivors stay in `candidates`; losers are demoted to
        //     `pending` for the second pipeline position to retry.
        if (body_size > 3) {                  // checked twice in the binary
            sub_1C6DD40(&candidates);
        }

        // 5c. Rewrite: for each surviving candidate, allocate a fresh
        //     constant-bank slot, emit the materialization stub (a one-
        //     time LDC that writes to the slot at module-load time),
        //     and rewrite every use site to read directly from the
        //     new c[bank][offset].
        sub_1C6A230(&candidates);

        // 5d. Optional xmm-flagged sub-pass. The bitvector `v159` (a
        //     128-bit packed flag set, separate from the per-vreg
        //     bitvectors above) is populated by the ranker when it
        //     detected texture-descriptor-class candidates. These need
        //     a different emission path because the LDC must be tagged
        //     with the descriptor-load modifier.
        if (cand_vregs_xmm_flagged()) {
            sub_1C72370(&candidates);
        }
    }

    // ── Stage 6: finalize (FINAL position only) ─────────────────
    if (is_final_pos) {
        // sub_1C68760 commits pending extractions, sweeps the
        // emit_q for any deferred materializations, and writes the
        // final bank-occupancy summary back to the shader-info struct.
        sub_1C68760(&candidates);
    }

    // ── Stage 7: drain worklists ────────────────────────────────
    // Eight intrusive-list teardowns, each following the same pattern:
    //   1. Walk forward through the list,
    //   2. For each record, splice into the parent allocator's
    //      free chain (the *v126 / ++ref-count / sub_1C68XXX call),
    //   3. Call the record's vtable destructor (offset +32).
    drain_emit_queue(&emit_q);                 // sub_1C69030
    drain_uses(&uses);                          // sub_1C68DE0 + sub_1C68CE0
    drain_bitvecs(&seen_vregs, &cand_vregs);    // direct frees
    drain_bank_map(&banks);                     // sub_1C68C60 family
    drain_tree_map(&trees);                     // sub_1C68BE0 family
    drain_chains(&chains);                      // sub_1C690B0 family
    drain_analysis_scratch(analysis_scratch);   // sub_1BD8620 — the 3 KB destructor

    sub_1C68B60(&candidates);                   // drain candidates last
    sub_661750(&pending);                       // standard ref-counted destructor

    // ── Stage 8: post-pass validation ───────────────────────────
    // Triggered only when knob 488 is set AND knob 489 has a non-zero
    // value AND a specific designated function pointer (*(ctx+932))
    // refers to a non-188-opcode instruction.
    int v5 = *(uint64_t*)(*(uint64_t*)(ctx + 1664) + 72);
    if (*(uint8_t*)(v5 + 59040) == 1 && *(uint32_t*)(v5 + 59048)) {
        int fn_idx = *(int*)(ctx + 932);
        if (fn_idx >= 0) {
            int opcode = *(uint32_t*)(/* ... */) & 0xCFFFFFFF;
            if (opcode != 188) {
                // sub_781F80 = use-def rebuild
                // sub_7E6090 = function-body sanity check
                // sub_A13890 = the actual validator
                sub_781F80(ctx, 0, ...);
                sub_7E6090(ctx, 0, 0, 0, 0);
                sub_A13890(ctx, scratch_v126, &validator_state, ...);
                // ABI-argument descriptor check across the function's BBs:
                walk_bbs_and_check_arg_descriptors(ctx, /* triggers BUG() on mismatch */);
                v5 = sub_8E3A20(scratch_v126);   // tear down validator
            }
        }
    }

    return (char)v5;
}
```

The body-size threshold (`v18 <= 3` at line `1c72805`) is the engine's headline filter. Functions with three or fewer "useful" BBs (after subtracting the preamble at `ctx+928`) contain too few use sites for any extraction to break even — the constant-bank slot is allocated at module load and is a permanent cost, while the savings are linear in the number of dynamic use sites. The threshold of 3 is constant, not knob-tunable. Confidence: **HIGH** (single constant comparison at a stable offset).

> ⚡ **QUIRK — body-size budget is consumed even when knob 487 is off**
> The knob-487 path at lines 192–221 updates the per-iteration counter `*(om_state + 35076)` **before** the size gate at line 270 has a chance to reject the function for being too small. This means a kernel module with hundreds of tiny device functions can exhaust the knob-487 budget without ever performing a single useful extraction — the counter ticks once per function regardless of whether the function is even eligible. On heavily template-instantiated codebases (cuBLAS-style headers that expand into dozens of one-line wrappers), this can effectively disable shader-const extraction on the *interesting* kernels by the time the gate gets to them. Confidence: **MED** — the counter update precedes the size gate, but whether the budget is meaningfully large in practice depends on the knob-table defaults (which this page does not enumerate).

### IR Walker (`sub_1BD9200`)

The 2,456-byte `sub_1BD9200` populates the 688-byte `v150` analysis scratch in a single forward sweep over the function's basic blocks. Its job is to convert the in-memory IR into a form the candidate collector can index:

```c
// sub_1BD9200 — distilled from the structural pattern.
//   v150       — 688-byte scratch (caller-provided)
//   ctx        — CodeObject
//   options    — vtable pointer table (off_23FAF70)
void IRScratch_build(uint8_t *scratch, CodeObject *ctx, void *options) {
    // Layout of the 688-byte scratch (offsets approximate, from stack analysis):
    //  +0    16B   xmm scratch (used for bitvector OR)
    //  +16    8B   forward iterator: current BB
    //  +24    8B   forward iterator: current instruction
    //  +32   24B   per-instruction operand triple (cached during scan)
    //  +64  192B   chain-of-eight tables (24 B per table, indexed by class)
    //  +256 256B   small open-hash for "have I seen this vreg as a candidate root"
    //  +512 176B   ranking-side scratch (cost accumulators, see sub_1C6DD40)

    init_iterators(scratch, ctx);

    for (bb *B : ctx->bb_list_at_104) {
        for (instr *I : B->instructions) {
            // The walker classifies each instruction into one of eight
            // candidate-root classes (kernel-param load, descriptor read,
            // immediate cast, etc.). The class index becomes the table
            // selector at scratch+64+24*class.
            int klass = classify_root(I, options);
            if (klass < 0) continue;
            table *T = (table*)(scratch + 64 + 24 * klass);
            insert_or_bump(T, I->dest_vreg);
        }
    }
}
```

The "options" vtable at `off_23FAF70` is a structure of small inline functions that customise the walker for the shader-const-extraction use case (as opposed to other consumers of `sub_1BD9200`, which exists outside this pass too — see `sub_1BD9200.callers` for the list). The table is 16 bytes, two slots; slot 0 selects the classifier and slot 1 selects the early-termination predicate. Confidence: **MED** — the table contents are reconstructed from the call sites, not directly readable as code.

### Candidate Collector (`sub_1C6F590`)

`sub_1C6F590` (1,957 bytes, 99 BBs) walks the eight tables produced by the scratch builder and produces a flat list of `Candidate` records keyed on a representative vreg. Each `Candidate` carries:

| Field | Bytes | Meaning |
|---|---:|---|
| `root_vreg` | 4 | vreg whose value is candidate for extraction |
| `chain_depth` | 4 | Depth of the uniform expression tree feeding `root_vreg` (1 = leaf load, 2 = leaf + one op, ...) |
| `static_use_count` | 4 | Number of static uses of `root_vreg` in the function |
| `est_dynamic_use_count` | 4 | `static_use_count × max(loop_trip_estimate)` for the deepest enclosing loop |
| `bank_class` | 1 | Which constant bank to target (0 = c[0] kernel params, 1 = c[2] textures, 2 = c[4] driver-patched) |
| `flags` | 1 | bit 0 = "needs descriptor modifier", bit 1 = "tex-class", bit 2 = "force-extract", bit 3 = "pending" |
| `cost_score` | 4 | Filled in by `sub_1C6DD40` |
| `successor_idx` | 4 | Intrusive next-record pointer for the bin-packing pass |

The list lives in `candidates` (v134 / `sub_1C68B60`-allocated). Confidence: **MED** — the field layout is reconstructed from the consume sites in `sub_1C6DD40` and `sub_1C6A230`; the names are descriptive guesses.

### Ranker (`sub_1C6DD40`)

The 4,013-byte ranker is the cost-model heart of the pass. It is the largest individual callee of the engine and is itself essentially a two-loop bin-packing algorithm: outer loop over bank classes, inner loop over candidates within that class. The outer loop's invariant is that bank-class `k` has used at most `bank_capacity[k]` slots by iteration end; the inner loop picks the highest-`cost_score` candidate that fits in the remaining capacity. Both losers (couldn't fit) and tied candidates are demoted to `pending` for the next pipeline position.

> ⚡ **QUIRK — ranker re-runs the size guard at line 416 of the engine**
> The engine checks `body_size > 3` twice: once at line 270 (to decide whether to allocate scratch at all) and once at line 416 (to decide whether to run the ranker). The second check is **redundant** — the only branch that arrives at line 416 has already passed the line 270 gate. The duplicate is a remnant of an earlier code shape where the ranker was a separate phase with its own gate; the merger left the inner check in place. It costs one compare-and-branch per function and is otherwise harmless. Confidence: **HIGH** (visible at both `1c72805` and `1c7359c` in the decompilation).

### Rewriter (`sub_1C6A230`)

The 949-byte rewriter is the smallest of the three main callees. For each surviving candidate, it:

1. Allocates a constant-bank slot via the shader-info bank allocator (writes back to `*(si + 16)` and `*(si + 20)` — the same fields the gate reads).
2. Emits a one-time `LDC` materialization stub. The stub is placed at the kernel entry point, not at each use site — it runs exactly once per kernel invocation and writes the computed value into the constant bank slot via `c[bank].store`. (On Maxwell+ hardware, the constant bank is read-mostly with one driver-controlled writer, so the "stub" is really a metadata entry the runtime patches before launch, not an actual store instruction.)
3. Replaces every use of `root_vreg` with a `LDC c[bank_class][slot_offset]` reading the patched value.
4. Marks the old definition's expression chain for DCE (which `OriPerformLiveDeadSecond` at phase 33 already ran, so the cleanup is delayed until the next liveness pass — phase 61 `OriPerformLiveDeadThird` for first-position rewrites, phase 84 `OriPerformLiveDeadFourth` for final-position).

The intermediate vregs do not survive `OriPerformLiveDeadThird`/`Fourth`; the rewriter does not call DCE itself.

## Function Map

| Address | Size | Role | Notes |
|---|---:|---|---|
| `sub_C5FDA0` | 34 B (4 BBs) | First-position wrapper. Gates on `getOptLevel > 1`, then `sub_1C72640(ctx, 0, ...)` | Phase 34 entry |
| `sub_C5FDD0` | 34 B (4 BBs) | Final-position wrapper. Same shape but `sub_1C72640(ctx, 1, ...)` | Phase 51 entry |
| `sub_7DDB50` | 156 B (10 BBs, leaf) | Opt-level accessor. Returns 0–5; pass enters only when >1 | Shared with 82+ other phase wrappers |
| `sub_1C72640` | 4,582 B (171 BBs) | The engine. Three-stage gate / collect / commit, plus stage-8 validator | This page |
| `sub_1BD9200` | 2,456 B (46 BBs) | IR-walker / scratch-builder for the 688-byte analysis scratch (`v150`) | Two callers — used by one other early-pipeline scanner |
| `sub_1BD8620` | 3,040 B (140 BBs) | Symmetric destructor for the scratch built by `sub_1BD9200` | Two callers; release-side mirror of `sub_1BD9200` |
| `sub_1C6F590` | 1,957 B (99 BBs) | Candidate collector — walks the 8 root-class tables, emits `Candidate` records | Only callee of the engine for this stage |
| `sub_1C6DD40` | 4,013 B (209 BBs) | Ranker — bin-packing cost model over constant-bank capacity | Largest single callee; runs only when `body_size > 3` |
| `sub_1C6A230` | 949 B (60 BBs) | Rewriter — allocates bank slot, emits `LDC`, marks old chain for DCE | One caller |
| `sub_1C72370` | 707 B (28 BBs) | Optional descriptor-class sub-pass; runs only when xmm-flag bitvector at `v159` is non-zero | One caller |
| `sub_1C68760` | 1,018 B (11 BBs) | **Finalize step — runs only when `a2 != 0`** (i.e., second pipeline position) | The single behavioural difference between phases 34 and 51 |
| `sub_6E6650` | 114 B (7 BBs) | Bitvector resize helper — used to size both per-vreg bitvectors to `(n_vregs + 64) / 64` words | Two call sites in the engine |
| `sub_781F80` | 8,335 B (454 BBs) | Use-def chain rebuild — used by every pass that needs canonical UD chains | Called only in stage 8 (validator path) |
| `sub_7E6090` | 2,614 B (161 BBs) | Function-body sanity check — verifies BB linked list, vreg references | Called only in stage 8 |
| `sub_A13890` | 19 B (1 BB) | Validator thunk — leaf wrapper that dispatches to the actual integrity checker via `off_21B4FD0` | Called only in stage 8 |
| `sub_1C68B60` / `sub_1C690B0` / `sub_1C68BE0` / `sub_1C68C60` / `sub_1C68CE0` / `sub_1C68D60` / `sub_1C69030` | 119 B each (9 BBs each, leaf) | Allocator-constructor family — each builds one intrusive-list / set scaffold (the seven scratch records named in the engine pseudocode) | Each is identical in shape; the seven are kept distinct because each manages a differently-typed record |
| `sub_1C68DE0` | 585 B (40 BBs) | Use-set destructor — drains the `uses` worklist by reverse-traversing its intrusive chain | Stage 7 cleanup |
| `sub_1C69460` | 129 B (8 BBs) | Stage-8 validator helper — accepts a length and resizes the validator's local vector | Stage 8 only |
| `sub_661750` | 119 B (9 BBs, leaf) | Standard ref-counted destructor — same helper used everywhere in the engine | Stage 7 cleanup |
| `sub_8E3A20` | 90 B (6 BBs, leaf) | Validator-vector destructor — first entry of the 91-slot table at `0x21DBEF8` | Stage 8 only |

Confidence: **HIGH** for the seven 119-byte constructor leaves (each is small enough to read in full); **MED** for the cost-model details of `sub_1C6DD40` (the function is large and only partially decompiled in the available data); **MED** for the per-field role descriptions of the eight scratch records (the role assignments are inferred from which subroutine accesses which stack slot).

## The `is_final_pos` Difference in Detail

The two-position pattern is implemented by exactly **one branch** in the engine, at line `1c72cb2`:

```c
if (is_final_pos)                 // a2
    sub_1C68760(&candidates, ...);
```

`sub_1C68760` (1,018 bytes, 11 BBs) is the **finalize** subroutine and is *not* called in any other path. Its responsibilities, reconstructed from the call site context and the 11-BB structure:

1. **Walk the `pending` list** (candidates demoted by the ranker for capacity reasons) and decide which ones to promote to the actually-extracted set, now that this is the last chance.
2. **Compact the constant-bank usage map** — coalesce adjacent free slots, compute the final per-bank occupancy that the cubin emitter will need to allocate.
3. **Commit the bookkeeping** to the shader-info struct at `*(*(ctx+1584) + 376)`. This is the same struct whose `+16/+20` BB-count fields gate stages 1 and 2 of the engine — committing here ensures that any downstream pass which reads the shader-info will see an up-to-date picture.
4. **Mark the function as "shader-consts-finalised"** by setting a bit somewhere in the function descriptor; this bit is then checked by the cubin emitter (`EmitPSI` at phase 36 is too early; the read is in the post-regalloc machinery).

> ⚡ **QUIRK — the first run can leave inconsistent bank state**
> Because `sub_1C68760` runs only in the final position, the first-position run intentionally leaves the constant-bank occupancy map in an *interim* state: candidates have been allocated slots but the per-bank free lists have not been compacted, and the "shader-consts-finalised" bit has not been set. Any pass between phases 34 and 51 that reads the shader-info bank map will see numbers that are larger than the true final occupancy. The only consumer in that window is `OriHoistInvariantsEarly` (phase 35), and it deliberately treats the bank map as advisory rather than authoritative. Re-implementations must replicate this two-stage commit or risk over-committing constant-bank capacity. Confidence: **MED** — the existence of the deferred commit is HIGH; the consumer behaviour in `OriHoistInvariantsEarly` is inferred from cross-references.

> ⚡ **QUIRK — stage-8 validator is the only `BUG()` site in the entire pass**
> The post-pass validator at the end of `sub_1C72640` (stage 8) is the only code path in the engine that can call `BUG()`. The trigger is an ABI argument-descriptor mismatch: when the engine has rewritten a function in a way that changes the visible argument-register classes (e.g., a kernel parameter that was a per-thread `R*` register on entry is now a uniform `UR*` after extraction), the validator compares each BB's argument descriptor (`*((dword*)v120 + 66)`) against the function's signature descriptor (`*(... + 164)`). A mismatch sets the local `v19` flag, and after the BB sweep finishes, `if (v19) BUG()` aborts the compilation. This is a soundness check, not a correctness optimisation — the pass *should not* be able to introduce such a mismatch, and the validator exists to catch the case where it did. The validator is itself gated on two more knobs (488/489 conjunctively), so most production compilations skip it entirely.

## Pipeline Position

```
Phase 30  DoSwitchOptSecond           ┐
Phase 31  OriLinearReplacement        │   uniform expressions exposed by switch
Phase 32  CompactLocalMemory          │   lowering + linearization
Phase 33  OriPerformLiveDeadSecond    │
Phase 34  ★ ExtractShaderConstsFirst  ┤── first sweep: easy candidates
Phase 35  OriHoistInvariantsEarly     │
Phase 36  EmitPSI                     │
...
Phase 49  GvnCse                      │   new uniform sub-expressions exposed
Phase 50  OriReassociateAndCommon     │   by commoning + reassociation
Phase 51  ★ ExtractShaderConstsFinal  ┤── second sweep: catches the late exposures
Phase 52  OriReplaceEquivMultiDefMov  │
Phase 53  OriPropagateVaryingFirst    │   varying analysis observes the rewrites
```

Phase 34 runs immediately after `OriPerformLiveDeadSecond` (33) — so the IR is in a known-clean state with no dead instructions polluting the use counts the cost model relies on. Phase 51 runs immediately after `OriReassociateAndCommon` (50) — which has just maximally exposed any uniform sub-expression by commoning equal trees and reassociating non-canonical orderings. The two positions bracket the entire "Stage 2 / Mid-Early" segment of the pipeline.

> ⚡ **QUIRK — the ranker observes a different cost landscape on each run**
> The ranker (`sub_1C6DD40`) uses loop-trip estimates from `ctx`'s loop-info table to compute `est_dynamic_use_count`. Between phase 34 and phase 51, two passes — `OriLoopFusion` (phase 59 in binary numbering, but its early form `OriLoopSimplification` at phase 18, and the unrolling pre-passes that follow) — can change the trip estimates significantly. A candidate that scored "not worth it" at phase 34 because its enclosing loop was estimated to iterate twice may score "worth it" at phase 51 after unrolling exposed the same loop as iterating 32 times. The two runs are therefore not idempotent in an algebraic sense: running the pass twice is strictly more powerful than running it once even on a fixed input. Confidence: **MED** — the loop-trip read site is clear, but the magnitude of the inter-run differences depends on workload.

## Storage Layout

The 1,392-byte stack frame is one of the largest in the entire optimizer. The breakdown, from the IDA stack-frame dump:

```
+0      16 B  v118 (__m128i)                  — xmm scratch for OR-merging bitvectors
+24    192 B  scalar locals (v119..v127)      — IR-iterator state, scratch
+128    72 B  v128..v134                      — eight scratch-record roots:
                                                  candidates, pending, chains,
                                                  trees, banks, uses, emit_q,
                                                  (one slot reserved for future use)
+208    20 B  v135..v139                      — gate flags and body_size
+216    24 B  v140..v144                      — chains-table iterator state
+248    24 B  v145..v149                      — IR-walker option vtable + cursors
+304   688 B  v150[688]                       — IR-walker analysis scratch
+992    16 B  v151 (__int128)                 — bitvec OR scratch (mirror of v118)
+1008   16 B  v152..v153                      — list-walker iterator
+1024   24 B  v154..v158                      — banks/trees worklist heads
+1048   16 B  v159 (__m128)                   — xmm-flagged-candidates bitvector
                                                  (drives sub_1C72370 optional pass)
+1064  140 B  v160..v182                      — final-loop locals, validator state
```

The `v150` 688-byte block is the dominant single allocation. It is the staging area for the IR walker (`sub_1BD9200`) and contains the eight per-root-class small open-hash tables plus the operand-cache that the candidate collector reads. The size — exactly 688 = 16 + 24×8 + 256 + 176 + 64 — corresponds to the table-aware layout in the IR-walker pseudocode above. Confidence: **MED** (the size is direct from the stack frame; the layout interpretation is inferred from the access patterns in `sub_1C6F590` and `sub_1C6DD40`).

## Cross-Reference: Constant Bank Banks 0, 2, 4

The three "bank classes" the rewriter targets correspond to three distinct constant-memory banks the cubin format reserves. Documentation of the bank conventions lives in the cubin output description; the relevant subset is:

| `bank_class` | Cubin bank | Driver behaviour | Typical contents |
|---:|:---:|---|---|
| 0 | `c[0]` | Driver-patched at launch | Kernel parameters, `gridDim`, `blockDim`, JIT-patched values |
| 1 | `c[2]` | Driver-patched at launch | Surface and sampler descriptors (texture metadata, 32 B each) |
| 2 | `c[4]` | Driver-patched at launch | Driver-internal: ABI staging slots, launch attributes |

The pass does not allocate slots in `c[1]`, `c[3]`, or `c[5]+` — those banks are reserved for either user-controlled `__constant__` arrays or for driver-internal use that the compiler does not see. Confidence: **LOW** on the specific bank-to-class mapping (the assignment is reconstructed from cross-references; the binary references "bank class" as a small integer 0–2 with no string evidence for which cubin bank corresponds to which class). The pass-internal logic is unaffected by this mapping — the engine only cares that the three classes are kept separate so the per-bank capacity caps in the ranker apply correctly.

## Verification Anchors

| Claim | Anchor in raw data |
|---|---|
| Two pipeline positions, one shared engine | Wrapper decompilations `sub_C5FDA0` and `sub_C5FDD0` differ only in the `a2` literal passed to `sub_1C72640` |
| Engine address and size | `ptxas_functions.json` entry for `sub_1C72640`: address `0x1c72640`, size 4,582 B, 171 BBs, 37 callees |
| `is_final_pos` selector controls exactly one branch | `sub_1C72640` line 422–423 (decompiled): `if ( a2 ) sub_1C68760(...)` — the only conditional on `a2` in the engine body |
| Opt-level gate is `sub_7DDB50` | Wrapper calls `v4 = sub_7DDB50(a2); if (v4 > 1) ...` |
| Knob 487 is master, 453 is body-size | Constants `0x1E7` (487) and `0x1C5` (453) visible in the gate decompilation |
| Body-size threshold of 3 | `if (v18 <= 3) return v5;` at line 257 |
| 688-byte IR-walker scratch | Stack frame slot `v150` is 688 bytes (`+304` to `+992`) |
| Stage-8 validator is the only `BUG()` path | Two `BUG()` call sites at `0x1c7380a` and `0x1c735b5`, both inside the validator block |
| Validator gated on knobs 488 + 489 | Knobs at `*(v5+59040) == 1` and `*(v5+59048) != 0` checked conjunctively |
| Shared engine has 37 callees | `ptxas_functions.json` callees list for `sub_1C72640` enumerates 37 distinct functions (note: 25 unique addresses with some called twice) |
| Phase positions 34 (bin 39) and 51 (bin 59) | `passes/index.md` rows for `ExtractShaderConstsFirst` and `ExtractShaderConstsFinal`; binary-to-wiki translation table |
| 91-entry destruction-vtable at `0x21DBEF8` | `ptxas_data_tables.json` entry; appears in the stage-8 validator scratch initialiser |

## Related Pages

- [Pass Inventory & Ordering](index.md) — phases 34 and 51 in the 159-phase table; binary-to-wiki index translation
- [Linear Replacement](linear-replacement.md) — phase 31, runs before both positions; shares knob 487 (master) gate
- [Copy Propagation & CSE](copy-prop-cse.md) — phase 50 `OriReassociateAndCommon`, runs immediately before the final position and is what newly exposes uniform sub-expressions
- [Varying Propagation (Divergence Analysis)](varying-propagation.md) — phase 53, runs immediately after the final position; observes the rewritten IR
- [Uniform Register Optimization](uniform-regs.md) — phase 74 `ConvertToUniformReg`; ultimate consumer of the uniform classification this pass strengthens
- [Liveness Analysis](liveness.md) — phases 33 (before first position) and 56/67 (clean up intermediate vregs the rewriter orphans)
- [Loop Passes](loop-passes.md) — `OriHoistInvariantsEarly` (phase 35) is the immediate successor of the first position and reads the interim bank-map state
- [Phase Manager Infrastructure](phase-manager.md) — `sub_7DDB50` opt-level gate is shared with 82+ phase wrappers
- [Register Model](../ir/registers.md) — vreg descriptor format; the `LDC c[bank][offset]` operand encoding the rewriter emits
- [IR Overview](../ir/overview.md) — partial-SSA window in which both positions land
