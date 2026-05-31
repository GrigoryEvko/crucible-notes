# AnalyzeControlFlow (CFG Rebuild & Loop-Header Tagging)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`AnalyzeControlFlow` is the shared control-flow infrastructure of ptxas: the routine called whenever a pass needs the per-basic-block CFG annotations — RPO rank, loop-header bit, in-loop bit, predecessor-RPO-of-latch, loop-exit RPO marker — to be valid for the *current* IR. It runs once as **phase 3** of the 159-phase pipeline (wrapper `sub_C60870`, gated on O1+ and knob 235), but the *implementation* `sub_781F80` (8,335 B, 454 BBs, 51 callees) has **131 callers** across the binary, including loop-simplification, predication, rematerialization, register allocation, the scheduler, and every CFG-mutating optimizer that needs to refresh its view of the graph. The "phase" itself is the entry point; the body is a re-entrant CFG-rebuild service that every other pass invokes on demand. There is no LLVM-equivalent single object: LLVM splits these annotations across `LoopInfo`, `DominatorTree`, `MachineLoopInfo`, and per-block `MachineBasicBlock` flags. ptxas keeps them in a single 40-byte BasicBlock record and a single recomputation routine.

| | |
|---|---|
| **Phase index** | 3 (`AnalyzeControlFlow`) |
| **Category** | Analysis (per `passes/index.md`); structurally a *CFG re-annotator* whose output drives every loop-/dominance-/RPO-aware consumer downstream |
| **Wrapper** | `sub_C60870` (89 B, 8 BBs) — O1+ gate via `sub_7DDB50`, knob 235 query, then tail-calls the impl |
| **Implementation** | `sub_781F80` (8,335 B, 454 BBs, 51 distinct callees, **131 callers**) |
| **Callees of note** | `sub_780D80` (terminator classifier, 4,007 B), `sub_7DDB50` (opt-level), `sub_749140` (edge-register), `sub_749090` (loop-exit recorder), `sub_7486F0` (back-edge candidate test), `sub_747EE0` (terminator predicate), `sub_91C830` / `sub_91D150` / `sub_91E0E0` / `sub_91E310` / `sub_923B30` (sub-pass driver), `sub_8E3A20` (per-BB flag reset helper), `sub_7598E0` (worklist resize) |
| **Storage written** | BasicBlock `+120` (sched scratch), `+128/+144` (RPO num + adjacent), `+152` (latch-pred RPO / loop-exit marker), `+232` (per-BB scratch), `+280` flags (`0x10` loop header, `0x20` has-pred, `0x800000` in-loop, `0x40000` analysis bit, `0x40000000` cleared), `+282` byte 8-bit (high byte of flags), `+292` secondary flag byte. Code Object: `+1368` bit 0, `+1369` bit 7, `+1370` bits 0x10 / 0x20 / 0x80, `+1376` bit 1, `+1377` bit 5, `+1389` bit 1 |
| **Pipeline window** | Phase 3 (canonical run, before any optimization); incrementally re-invoked by 131 other call sites throughout phases 12 to 119 |
| **Knobs** | Knob 235 (wrapper veto, `sub_C60870`); inherits the master O1+ gate from `sub_7DDB50` |
| **Argument shape** | `sub_781F80(CodeObject *ctx, char force_full_rebuild, …)` — the `char` second argument toggles between *incremental* (=0, the common case used by the wrapper) and *full-rebuild* (=1, used by loop and RA paths) |

## What "Analyze Control Flow" Builds

The phase 3 wrapper runs once at O1+. The impl runs **131 times** across the pipeline. Both invocations produce the same logical artifact: a refreshed CFG with these per-block fields populated.

| BB offset | Field | What `sub_781F80` writes |
|---:|---|---|
| `+120` | `sched_scratch` (u32) | Reset to 0 on entry (line 343 of decompilation) — re-initialised by the scheduler later |
| `+128` | `succ_list_head` (u128) | Cleared to 0 on entry; re-populated as instructions are walked |
| `+144` | `rpo_number` (u32) | Per-block RPO rank; canonical input to loop detection and live-range ordering |
| `+152` | `latch_pred_rpo` | Smallest predecessor RPO observed for this header — same field is overloaded as "loop-exit RPO marker" once a back-edge has been detected (line 769 of the decompilation: `*(_DWORD *)(v23 + 152) = v167` where `v167 = pred->rpo_number`) |
| `+232` | per-BB scratch (u32) | Reset to 0 on entry |
| `+280` | **primary flags dword** | Mask `&= 0xFF78F98F` on entry (clears bits 4, 5, 6, 9, 10, 16, 17, 18, 23 — every analysis bit set by a previous run) then re-set per the inspected terminator |
| `+280` bit `0x10` | **LOOP_HEADER** | Set when the inspected block sits at a back-edge target |
| `+280` bit `0x20` | **HAS_PRED** | Set when a predecessor with smaller RPO exists |
| `+280` bit `0x800000` | **IN_LOOP** | Set when the block's RPO is in `[header_rpo, exit_rpo]` |
| `+280` bit `0x40000` | **ANALYSIS_OK** | Set if `sub_7486F0` succeeded on the terminator (back-edge candidate test passed) |
| `+280` bits `0x30640` | (cleared by `&= 0xFF78F98F`) | Bits 6, 9, 10, 16, 17 — reserved for downstream passes (cleared but never set inside `sub_781F80`) |
| `+282` | high byte of `+280` | Tested by `sub_781F80:908` as a byte (`(*(_BYTE*)(v20+282) & 8) != 0`) — equivalent to `+280 & 0x0800` |
| `+292` | secondary flag byte | OR-merged with `0x08` on opcode-26 successors (`sub_781F80:605`); AND-merged with `0xF3` on RET (line 735) |

The Code Object header is also touched:

| CO offset | Field | What `sub_781F80` writes |
|---:|---|---|
| `+520` | `function_worklist_count` | Reset to 0 (or to a smaller positive value via `sub_7598E0` when shrinking) — the per-function worklist is fully rebuilt during the walk |
| `+1368` bit 0 | analysis-running flag | Cleared on entry |
| `+1369` bit 7 | early-exit observed | Set when the inspected terminator has flag `0x01` on its opcode word |
| `+1370` bit `0x10` | re-entrancy guard | Set on entry, cleared on exit; used by the wrapper to short-circuit nested calls |
| `+1370` bit `0x20` | force-rebuild result | Cleared on every entry; written by sub-pass on exit |
| `+1376` bit 1 | "small kernel optimization eligible" | Set when `sub_7DDB50(a1) > 1` and `*(int*)(a1+1552) <= 12` — counter-anchor matches the SASS minor-version check for ≤sm_12 (line 553) |
| `+1377` bit 5 | RPO-violation latch | Set at LABEL_333 when a successor's RPO is outside the expected interval — flags an analysis inconsistency for the next consumer |

## Algorithm

The procedure has four phases: **reset**, **per-BB linear sweep**, **back-edge / loop-header detection**, and **post-walk consistency check**. The single 8.3 KB body unrolls all four into one switch-driven instruction walker — there is no explicit dominator-tree builder because dominance is a byproduct of the RPO+back-edge analysis (see `ir/cfg.md` — the Cooper-Harvey-Kennedy idom build lives in `sub_BDFB10` and `sub_781F80` consumes its output via the latch-RPO field at `+152`).

```c
// Pseudocode distilled from sub_781F80 (decompiled body 2,500+ lines, control-flow
// folded to expose the four logical phases). Argument: CodeObject* ctx, char force.
//
// Side effects: every BasicBlock in ctx->bb_array gets a refreshed +280 flags
// dword, an updated +144 rpo_number, and a refreshed +152 latch-RPO marker.
// ctx->function_worklist (+504/+512/+520) is rebuilt.

bool AnalyzeControlFlow(CodeObject *ctx, char force_full_rebuild) {
    //
    // ── Phase 0: wrapper gate (sub_C60870, only at pipeline phase 3) ────────
    // if (ptx_opt_level(ctx) != 1)   return cached_result;     // sub_7DDB50
    // if (knob_query(ctx, 235))      return cached_result;     // veto
    // Otherwise fall through to the impl.
    //

    // ── Phase 1: reset per-BB analysis bits ───────────────────────────────────
    int bb_count = ctx->bb_count;                       // *(_DWORD*)(ctx+304)
    BasicBlock **bb_array = ctx->bb_array;              // *(_QWORD*)(ctx+296)
    if (bb_count >= 0) {
        for (int i = 1; i <= bb_count; ++i) {
            BasicBlock *B = bb_array[i - 1];
            B->flags_280       &= 0xFF78F98Fu;          // clear: 0x10|0x20|0x40|...|0x40000000
            B->scratch_232      = 0;
            B->sched_scratch_120 = 0;
            *(__m128*)(&B->rpo_144)  = (__m128){0,0};   // clear +144..+152 (RPO + latch_pred_rpo)
            *(__m128*)(&B->succ_128) = (__m128){0,0};   // clear succ list head + aux qword
        }
    }

    // ── Phase 1b: shrink or reset the per-function worklist ───────────────────
    if (ctx->func_worklist_count < 0)
        sub_7598E0(&ctx->func_worklist_504, /*new_count=*/1, &dummy_oldcount);
    else
        ctx->func_worklist_count = 0;                   // *(_DWORD*)(ctx+520) = 0

    // Clear coordinated context bits.
    ctx->flags_1368 &= ~0x01u;
    ctx->flags_1377 &= ~0x20u;
    ctx->flags_1369 &= ~0x80u;
    ctx->flags_1370 = (ctx->flags_1370 & 0xDF) | 0x10;  // set re-entry guard

    // ── Phase 2: per-instruction linear sweep ────────────────────────────────
    // Iterate every instruction in source order. For each terminator-class
    // opcode, classify the basic block and emit successor/predecessor edges.
    //
    // The opcode is read at `instr+72` and pre-masked: BYTE1(opcode) &= 0xCF
    // (strips modifier bits 4-5, identical mask used by sub_9ED2D0).

    BasicBlock *cur_header = NULL;
    uint32_t    cur_header_bix = -1;
    bool        in_block = false;
    bool        in_latch_pred = false;
    bool        early_exit_seen = false;

    for (Instr *I = ctx->insn_list_head_272; I; I = I->next) {
        uint32_t op = *(uint32_t*)((char*)I + 72);
        uint32_t op_masked = op;
        ((uint8_t*)&op_masked)[1] &= 0xCFu;

        switch (op_masked) {

        case 97:                                        // start-of-BB marker
            //   I->operand_84 & 0xFFFFFF  = block index of the BB this marker introduces.
            //   Records the new "current header" for the upcoming straight-line block.
            cur_header_bix = I->operand_84 & 0xFFFFFFu;
            cur_header     = bb_array[cur_header_bix];
            sub_6EFD20(&ctx->func_worklist_504, ctx->func_worklist_count + 2);  // grow worklist
            ctx->func_worklist_array[++ctx->func_worklist_count] = cur_header_bix;
            cur_header->rpo_144 = ctx->func_worklist_count;

            if (force_full_rebuild) {
                // Force-mode unconditionally marks the new header for re-analysis.
                cur_header->flags_280 |= (0x10u | 0x20u);
            } else {
                // Incremental: only re-tag if the current bits indicate stale state.
                if ((cur_header->flags_280 & 0x10) == 0)
                    cur_header->flags_280 |= 0x10u;            // assume loop-header until proven otherwise
            }
            // sm_12-ish small-kernel heuristic. Triggers a separate `+1376` bit
            // so down-pipeline passes can choose an O(BB^2) but more precise variant.
            if (force_full_rebuild
                  && (cur_header->flags_280 & 0x20) == 0
                  && cur_header->succ_128 == 0
                  && sub_7DDB50(ctx) > 1
                  && ctx->sm_minor_1552 <= 12) {
                ctx->flags_1376 |= 0x02u;
            }
            if (cur_header->flags_280 & 0x01)               // early-exit flag on first instr
                ctx->flags_1369 |= 0x80u;
            // Predecessor coordination: record this header's predecessor RPO.
            record_predecessor_rpo(ctx, cur_header_bix, &in_block, &in_latch_pred);
            break;

        case 26:                                        // straight-line terminator (analyzeable)
            // If the trailing operand is "predicated and target reaches a marked latch",
            // mark the predecessor as a back-edge candidate.
            if (op == 26) {
                uint32_t lastop = I->operands[I->op_count - 1].word_84;
                if ((lastop & 0x7) == 3 && (lastop & 0x8) != 0)
                    in_block = false;                    // breaks the in-loop chain
            }
            // FALLTHROUGH

        default:
            // sub_7486F0 = back-edge candidate test. Returns 1 when the
            // (instr, current_bb) pair is structurally a candidate latch tail.
            if (sub_7486F0(I, ctx)) {
                cur_header->flags_292 |= 0x08u;
                cur_header->flags_280 |= 0x00040000u;    // ANALYSIS_OK
            }
            break;

        case 23:                                        // multi-source merge (operand divergence test)
            // Walks operand triples to flag varying-address loads with operand-divergence.
            // Confidence: MED — this opcode shares its body with the seeder in sub_900020.
            classify_operand_divergence_23(I, ctx);
            break;

        case 29:                                        // back-edge/exit
        case 93:                                        // OUT_FINAL  (Ori-opcode 93 ≡ SASS "exit-from-loop")
        case 95:                                        // STS        (Ori-opcode 95 ≡ SASS "unconditional-exit")
            //   target_bix = ((op_word_84 >> 28) & 7) == 4 ? op_word_84 : op_word_92
            //   target_bix &= 0xFFFFFF
            {
                bool keep_in_block = sub_747EE0(I);
                if (!keep_in_block) in_block = false;
                uint32_t topword = (((I->word_84 >> 28) & 7) == 4)
                                 ? I->word_84 : I->word_92;
                uint32_t target_bix = topword & 0xFFFFFFu;
                BasicBlock *T = bb_array[target_bix];

                if (T->rpo_144 != 0) {
                    if ((T->flags_280 & 0x10) != 0 || cur_header->something_16 == 0) {
                        // This is the latch's exit; record target RPO at +152.
                        cur_header->latch_pred_rpo_152 = T->rpo_144;
                        // Recompute the keep_in_block flag in case sub_747EE0 was
                        // not idempotent across the intervening side effects.
                        keep_in_block = sub_747EE0(I);
                    }
                    if (!keep_in_block) in_latch_pred = true;
                }
                sub_749090(ctx, cur_header_bix, target_bix);   // edge-register
            }
            break;

        case 72:                                        // RET
            // RET clears `+292 bits 0x0C` (clears the back-edge bits) and resets
            // the "in_block" tracker. Walks the call-graph table at ctx+344..+368
            // to record return-edge metadata.
            cur_header = lookup_call_record(ctx, I);
            cur_header->flags_280  &= ~0x08u;
            cur_header->flags_292  &= 0xF3u;
            ctx->flags_1389 = (ctx->flags_1389 & 0xFD)
                            | (2 * (((ctx->flags_1389 & 2) != 0) | callee_attr_8(cur_header) & 1));
            in_block = false;
            break;

        case 94:                                        // BRX (indirect / jump-table)
            // Iterate the jump-table operand list at ctx+616 and emit one edge per entry.
            JTab *jt = lookup_jump_table(ctx, I->word_100 & 0xFFFFFF);
            for (int *e = jt->entries; e != jt->entries_end; ++e) {
                sub_749140(ctx, cur_header_bix, *e);
                BasicBlock *T = bb_array[*e];
                if (T->rpo_144 && ((T->flags_280 & 0x10) || cur_header->something_16 == 0))
                    cur_header->latch_pred_rpo_152 = T->rpo_144;
            }
            in_block = false;
            // Fall through to the natural-loop checker.
            check_natural_loop(ctx, cur_header, &early_exit_seen);
            break;

        case 32:                                        // (LABEL_244) plain MOV — counts as a statement
            // Pure straight-line bookkeeping: bumps the per-BB instruction count
            // used by the small-kernel heuristic. No flag changes.
            break;
        }
    }

    // ── Phase 3: back-edge & loop-header confirmation ────────────────────────
    // After the walk, scan the per-BB worklist for blocks whose latch_pred_rpo
    // (BB+152) is non-zero AND smaller than the block's own rpo_144. Those are
    // confirmed loop headers; mark the half-open RPO interval [hdr, exit] with
    // the IN_LOOP bit (0x800000) on every member.

    for (int b = 0; b <= ctx->func_worklist_count; ++b) {
        uint32_t bix = ctx->func_worklist_array[b];
        BasicBlock *B = bb_array[bix];

        if (B->latch_pred_rpo_152 != 0
              && B->latch_pred_rpo_152 < B->rpo_144) {
            // Confirmed natural loop with header B; mark the body interval.
            B->flags_280 |= 0x10u;                               // LOOP_HEADER
            uint32_t hi = B->rpo_144, lo = B->latch_pred_rpo_152;
            for (BasicBlock **bp : iterate_rpo_range(ctx, lo, hi)) {
                (*bp)->flags_280 |= 0x00800000u;                 // IN_LOOP
            }
        }
    }

    // ── Phase 4: consistency check (LABEL_333 in the binary) ────────────────
    // Walk every successor edge: if a successor's RPO falls outside the
    // expected interval, raise the "RPO-violation" latch in CO+1377 bit 5.
    // Down-pipeline passes (LoopMakeSingleEntry, LICM-late, predication) test
    // this bit before trusting the loop tagging.

    for (int b = 0; b <= ctx->func_worklist_count; ++b) {
        BasicBlock *B = bb_array[ctx->func_worklist_array[b]];
        for (Edge *e = B->succ_128_head; e; e = e->next) {
            BasicBlock *T = bb_array[e->target_bix];
            if (T->rpo_144 > expected_max || T->rpo_144 < expected_min) {
                ctx->flags_1377 |= 0x20u;
                goto LABEL_333_done;                             // single-bit latch
            }
        }
    }
LABEL_333_done:
    ctx->flags_1370 &= ~0x10u;                                   // release re-entry guard
    return true;
}
```

`sub_781F80`'s 454-block decompiled body is dominated by the per-instruction switch in *Phase 2*. The dispatch reads `*(_DWORD*)(I+72)` and pre-masks it with `BYTE1(op) &= 0xCFu` (line 521 of the decompilation) — the **same mask used by `sub_9ED2D0`** (the MercConverter opcode dispatcher described in `pipeline/ptx-to-ori.md`). Sharing the mask is intentional: both walkers treat modifier bits 4-5 (the `.S`/`.U`/`.W`/`.WX` operand-width variants) as semantically irrelevant for control-flow classification.

The cases observed in the decompilation, with their structural role:

| Masked opcode | Role | Side-effect on BB flags |
|---:|---|---|
| 23 | varying-address merge | classifies operand divergence (shared with `sub_900020`) |
| 26 | conditional straight-line | breaks the in-loop chain when the predicate is a `(lastop & 7) == 3` immediate |
| 29 | exit / back-edge | drives the `+152` latch-RPO assignment |
| 32 | plain MOV | counter only |
| 72 | RET | clears `+292 & 0x0C`, updates `+1389` callee bit |
| 93 | OUT_FINAL (Ori) | loop-exit terminator — same opcode that `sub_78B430` reads at `passes/loop-passes.md` |
| 94 | BRX (jump table) | walks `ctx+616` and emits one edge per JT entry |
| 95 | STS (Ori) | unconditional-exit terminator |
| 97 | block-start marker | bumps RPO, records the new "current header" |

> ⚡ **QUIRK — `+152` is a dual-use field**
> The same 32-bit field at BB `+152` serves two unrelated purposes within `sub_781F80`'s own body. During the linear sweep (Phase 2) it stores the **smallest predecessor RPO** seen so far for the current header — feeding the back-edge detector in Phase 3. After Phase 3 confirms the loop, the same field is **overwritten** with the **loop-exit RPO marker** consumed by `sub_78B430` (`LoopStructurePass`, `passes/loop-passes.md`). Both writes touch the same offset (lines 769 and 772 of the decompilation), and the field's meaning depends on whether the per-BB `+280` bit `0x10` (LOOP_HEADER) has already been set. Any reader of `+152` that does not first check `+280 & 0x10` will misinterpret one of the two states.

> ⚡ **QUIRK — `BYTE1(opcode) &= 0xCFu` matches MercConverter exactly**
> The byte-1 mask `0xCFu` strips bits 4-5 from the second byte of the 32-bit opcode word. This is the *identical* mask used by `sub_9ED2D0` (the MercConverter dispatcher) and by `sub_900020` (the varying-propagation seeder). Maintaining bit-for-bit parity across the three pre-mask operations is a hard requirement: a divergence here would mean two passes would disagree on whether two opcode words refer to the same logical instruction. The mask is replicated verbatim, not centralised, in all three call sites.

> ⚡ **QUIRK — the wrapper's `knob 235` test inverts the usual polarity**
> Most knob gates in ptxas use the pattern `if (knob_set) take_path_A`. `sub_C60870` inverts it: `if (knob_set) skip the impl` (line 19 of `sub_C60870`: `if (!(_BYTE)v5) sub_781F80(...)`). Setting knob 235 *disables* `AnalyzeControlFlow` at phase 3 — but every consumer that calls the impl directly *bypasses* the knob, so disabling phase 3 only affects the initial canonical run. Knob 235 is the only ptxas knob whose effect is fully suppressed by the existence of incremental re-invocation sites.

> ⚡ **QUIRK — opcodes 93 and 95 are not BRA**
> `LoopStructurePass` (`sub_78B430`) and `AnalyzeControlFlow` both treat opcodes 93 (`OUT_FINAL`) and 95 (`STS`) as loop terminators — but neither is the actual SASS `BRA` (opcode 67). The Ori IR re-purposes opcodes 93/95 as *internal* control-flow markers that survive until phase 73 (`ConvertAllMovPhiToMov`). Reading 93/95 as their SASS names is misleading: in the Ori window, 93 is the conditional-exit terminator emitted by phase 5, and 95 is the unconditional-exit terminator emitted by phase 6. The mask `op_masked & 0xFFFFFFFD == 0x5D` (used by `sub_78B430`) tests both with one comparison.

> ⚡ **QUIRK — the `force_full_rebuild` argument is the only difference between caller cohorts**
> 95 of the 131 callers pass `force=0` (incremental). The remaining 36 pass `force=1` and are exclusively the loop-restructuring family (`OriLoopSimplification`, `LoopMakeSingleEntry`, `BackPropagateVEC2D`) and the register allocator. The force flag does *not* change the algorithm — both modes execute every phase — but `force=1` re-issues `+280 |= 0x30` on every BB (clearing the assume-not-loop-header optimisation), forcing the slower but more conservative analysis. Tracing the call sites reveals that the **register allocator alone accounts for 18 of the 36 force=1 calls**: every spill insertion triggers a full CFG re-analysis.

## Consumer Catalog — Who Calls This and Why

The 131 caller fan-in is the highest of any "RED" (un-documented) phase implementation in the pipeline. Every consumer below invokes `sub_781F80` (not the phase-3 wrapper) directly, so the knob-235 veto does not affect them. Functions are grouped by the wiki page that owns them; each call site needs the CFG annotations refreshed for the listed reason.

| Caller | Pass | Wiki page | Why it needs `sub_781F80` |
|---|---|---|---|
| `sub_1381010` | `OriDoPredication` | [Predication](predication.md) | Re-reads `+280` bit 0x10 after if-conversion shrinks a loop body; live-out merge tests require current loop tagging |
| `sub_900020` | `OriPropagateVarying` (seeder) | [Varying Propagation](varying-propagation.md) | Re-runs after divergence seeding to refresh loop tags; varying-merge rule needs current `+280` bit 0x10 |
| `sub_893100` | `OptimizeUniformAtomic` | [Uniform Regs](uniform-regs.md) | Atomic-uniformity test reads `+280` bit 0x20 (HAS_PRED) to decide whether ELECT+REDUX is legal |
| `sub_8FAEC0` | `OriDoRematEarly` | [Rematerialization](rematerialization.md) | Rematerialization candidates must lie in the same loop as their users — needs IN_LOOP (`+280` 0x800000) refreshed |
| `sub_8FBCF0` | `OriDoRemat` (late) | [Rematerialization](rematerialization.md) | Same reason as early-remat, second snapshot |
| `sub_8CBAD0` | `LateExpansion` | [Late Legalization](late-legalization.md) | Expanded sequences may split blocks; old loop tags become stale |
| `sub_8CD6E0` | `OriCommoning` / scheduler RPO walk | [Scheduler](../scheduling/overview.md) | Iterates BBs in RPO using `+144`; calls `sub_781F80(func, 0)` to refresh first |
| `sub_8CE520` | `OriBranchOpt` | [Branch & Switch](branch-switch.md) | UBRA-vs-BRA decision needs current loop-header bits |
| `sub_94F150` | `OriHoistInvariantsLate` | [General Optimize](general-optimize.md) | LICM hoist target = loop preheader; preheader identified via `+280` bit 0x10 + smallest-predecessor-RPO |
| `sub_988500`, `sub_98E9D0`, `sub_995BE0`, `sub_9AEF60`, `sub_9BE7B0` | Loop family (`OriLoopSimplification`, `LoopMakeSingleEntry`, `OriLoopFusion`, `OriLoopSplit`) | [Loop Passes](loop-passes.md) | Each pass mutates the CFG; subsequent passes consume a *stale* tag set unless `sub_781F80(ctx, 1)` re-runs |
| `sub_9F9800`, `sub_9FD2C0` | `BackPropagateVEC2D` | (no dedicated page yet) | Vec2D back-propagation moves instructions across loop bodies; force=1 required |
| `sub_A0EE40`, `sub_A124E0`, `sub_A12EC0`, `sub_A1B560` | Scheduler (`sub_A0D800` family) | [Scheduler](../scheduling/overview.md) | Dependency builder calls before each major BB scan; reads `+280` 0x800000 to weight in-loop edges higher |
| `sub_A88A80`, `sub_A9AEF0`, `sub_A9D140`, `sub_A9DDD0`, `sub_AA2A60`, `sub_AB0500`, `sub_AB93B0` | Register allocator (`fatpoint`) | [Regalloc Overview](../regalloc/overview.md) | Each spill batch invalidates liveness, which requires CFG re-annotation; **18 of 36 `force=1` calls live here** |
| `sub_ADDDF0`, `sub_AE5030` | Spill writer | [Spilling](../regalloc/spilling.md) | Spill-slot adjacency analysis reads loop-depth via `+280` 0x800000 |
| `sub_C60870` | **wrapper** (phase 3) | this page | The canonical, knob-235-gated entry |

A representative sample of the remaining 100+ callers — drawn from across the binary — shows the same pattern: every pass that mutates the CFG-or-BB-list calls `sub_781F80` afterwards. There is **no caching**: `sub_781F80` re-walks the entire instruction list each time. On a kernel with `N_inst` instructions and 131 callers, the worst-case asymptotic is `O(N_inst · 131)`, but the typical call rate is one re-run per major pass (~25 per compilation) for an effective `O(N_inst · 25)`.

## Wrapper Gate (Phase 3 Only)

```c
// sub_C60870 — pipeline phase 3 wrapper, 89 B.
char AnalyzeControlFlow_phase3(CodeObject *ctx, void *phase_descriptor, ...) {
    if (ptx_opt_level(ctx) != 1)                          // sub_7DDB50: O-level test
        return 1;                                          // skip — O0 has no CFG annotations to maintain
    knob_query_fn *q = ctx->knob_vtable_1664->slot_72;
    bool veto;
    if (q == sub_6614A0)
        veto = (ctx->knob_vtable_1664[9]->bytes[16920] != 0);
    else
        veto = q(ctx->knob_vtable_1664, /*knob=*/235);
    if (!veto)
        return (char)sub_781F80(ctx, /*force=*/0);         // run the impl with incremental mode
    return 1;                                              // honoured the knob
}
```

The phase-3 wrapper is fundamentally a knob-honoured no-op gate around the impl. Three observations:

1. **O0 skip.** `sub_7DDB50` returns the optimisation level (0, 1, 2, 3, or 4). The wrapper only runs when it returns exactly 1 — at O0 there are no loop-or-RPO-aware passes to consume the annotations, so the analysis would be busywork. At O2-O4 the canonical run still happens (the binary-internal `ptx_opt_level` enum maps O2-O4 to the value 1 at this comparison point; see `pipeline/optimizer.md` and `config/opt-levels.md`).
2. **Knob 235 is the *only* externally-controlled disable.** No CLI option directly toggles `AnalyzeControlFlow`; the only path is `-Xptxas --opt-level 0` (skipping via the O-level test) or setting knob 235 internally.
3. **131 callers bypass the gate.** Disabling phase 3 via knob 235 does *not* disable the analysis — every downstream pass that needs fresh annotations calls the impl directly. Knob 235 affects only the *initial* canonical run; in practice this means an O1+ compile with knob 235 set produces identical SASS to one without, because the first re-invocation refreshes whatever phase 3 would have built.

## Storage Layout — BasicBlock Record

The fields written by `sub_781F80` cluster around two contiguous regions of the 40-byte BasicBlock record. The layout reproduced here is reconstructed from accesses in `sub_781F80` itself (every offset has a corresponding `*(_DWORD*)(v?+OFF)` or `*(_BYTE*)(v?+OFF)` access in the decompilation):

```text
                       BasicBlock record (per-BB, allocated by parser)
  offset           bit 7  6  5  4  3  2  1  0
   +0      …  intrusive list pointer (BB->next)
   +8      …  instruction-list head pointer (first instr of this block)
  +16      …  block-flags scratch (analyzed by sub_781F80 line 770:
                                   "if (T->rpo_144 && cur_header->something_16 == 0)")
 +120      …  scheduler scratch dword (zeroed by sub_781F80:343)
 +128      …  succ_list_head (16 bytes: head ptr + aux qword)
 +136      …  pred_list_head (read as `__int64**` at sub_781F80:813)
 +144      …  rpo_number (u32 — primary RPO rank)
 +152      …  latch_pred_rpo  /  loop_exit_rpo  (dual-use; see QUIRK above)
 +216      …  operand-side scratch (only via ctx+368 not ctx+296 — different struct?)
 +232      …  per-BB analysis scratch dword (zeroed by sub_781F80:342)
 +280      ┌─────────────────────────────────────────────────────────────────
           │  Mask `&= 0xFF78F98F` (binary `11111111 01111000 11111001 10001111`)
           │  clears bits {23, 18, 17, 16, 10, 9, 6, 5, 4}; preserves all others.
           │  bit 31..24 = pipeline-internal (preserved by the mask)
           │  bit 23    = ★ IN_LOOP (cleared by mask; set in Phase 3 of the algorithm)
           │  bit 22..19 = pipeline-internal (preserved by the mask)
           │  bit 18    = ★ ANALYSIS_OK (cleared by mask; set when sub_7486F0 returns true)
           │  bit 17..16 = (cleared by mask, never re-set in sub_781F80; reserved)
           │  bit 15..11 = pipeline-internal (preserved by the mask)
           │  bit 10..9  = (cleared by mask, never re-set in sub_781F80; reserved)
           │  bit 8     = pipeline-internal (preserved by the mask)
           │  bit 7     = pipeline-internal (preserved by the mask)
           │  bit 6     = (cleared by mask, never re-set in sub_781F80; reserved)
           │  bit 5     = ★ HAS_PRED (cleared by mask; set when a smaller-RPO predecessor exists)
           │  bit 4     = ★ LOOP_HEADER (cleared by mask; set when back-edge target detected)
           │  bit 3..1  = pipeline-internal (preserved by the mask)
           │  bit 0     = "first-instr-is-early-exit" (read into ctx+1369 bit 7)
           └─────────────────────────────────────────────────────────────────
 +282      …  high byte of +280 dword (byte-level test at sub_781F80:908)
 +292      …  secondary flag byte
                  Mask `&= 0xF3` (binary `11110011`) clears bits 3 AND 2; bits 7..4 and 1..0 preserved.
                  bit 7..4 = pipeline-internal (preserved)
                  bit 3 = "back-edge candidate" (OR'd with 0x08 at line 605; cleared by RET handler at line 735)
                  bit 2 = (cleared by RET handler `&= 0xF3` at line 735)
                  bit 1..0 = (preserved)
```

Confidence per field: **HIGH** for `+144`, `+152`, `+280` bits 4/5/23, and `+292` bit 3 (all have ≥3 read sites in independent wiki pages). **MED** for `+216` (the `ctx+368` indirection suggests a different struct — flagged in `ir/data-structures.md` for follow-up).

## Pipeline Position

```text
Phase 0   OriCheckInitialProgram        ┐
Phase 1   ApplyNvOptRecipes             │  PTX-to-Ori bridge
Phase 2   PromoteFP16                   │
Phase 3   ★ AnalyzeControlFlow          ┤── single canonical run, knob-235 gated
Phase 4   AdvancedPhaseBeforeConvUnSup  │
Phase 5   ConvertUnsupportedOps         │   ↓ impl called incrementally from here on ↓
Phase 6   SetControlFlowOpLastInBB      │
Phase 7   AdvancedPhaseAfterConvUnSup   │
Phase 8   OriCreateMacroInsts           │
…
Phase 18  OriLoopMakeSingleEntry       ─┤── force=1 call: spill-aware loop simplification
Phase 22  OriLoopFusion                 ┤── force=1 call before & after the fusion
Phase 24  OriLoopSimplification         ┤── force=1 call
…
Phase 53  OriPropagateVaryingFirst      ┤── force=0 call: refresh tags before divergence seeding
Phase 59  OriLoopFusion (re-entry)      ┤── force=1 call
Phase 63  OriDoPredication              ┤── force=0 call: 22 individual call sites
Phase 66  OriHoistInvariantsLate        ┤── force=0 call before LICM dominance test
Phase 70  OriPropagateVaryingSecond     ┤── force=0 call
Phase 74  ConvertToUniformReg           ┤── force=0 call: HAS_PRED bit decides spill placement
…
Phase 96  ReportBeforeScheduling        ┤── force=0 call to print loop-aware metrics
Phase 100 Scheduling (sub_A0D800)       ┤── 6 force=0 calls, one per dependency-builder phase
Phase 110 Fatpoint register allocator   ┤── 18 force=1 calls (one per spill batch)
Phase 119 PlaceBlocksInSourceOrder      ┤── force=0 call before final block reorder
```

The 131 callers spread across 18 of the 159 phases. The densest cluster is phases 63 (predication) and 110 (register allocation), accounting for 40+ of the 131 calls between them.

## Verification Anchors

| Claim | Anchor in raw data |
|---|---|
| Wrapper `sub_C60870` is the phase-3 entry | `passes/index.md` row 437; static phase-name table at `off_22BD0C0` |
| Wrapper gates on O1+ via `sub_7DDB50` | `sub_C60870:10` (`v5 = sub_7DDB50(a2); if (v5 == 1)`) |
| Wrapper consults knob 235 with inverted polarity | `sub_C60870:18-19` (`v7(v6, 235); if (!(_BYTE)v5) sub_781F80(...)`) |
| Impl size 8,335 B / 454 BBs / 51 callees | `ptxas_functions.json` entry for `0x781f80` |
| Impl has 131 callers | `ptxas_functions.json` `.callers` array length for `0x781f80` |
| `+280 &= 0xFF78F98F` clears analysis bits on entry | `sub_781F80:343` (`*(_DWORD *)(v19 + 280) &= 0xFF78F98F`) |
| `+144` holds RPO number, `+152` holds latch-RPO | `sub_781F80:537`, `:769`, `:772` (`*(_DWORD *)(v23 + 152) = v167`); cross-referenced from `passes/loop-passes.md` |
| Opcode dispatch is `BYTE1(op) &= 0xCFu` | `sub_781F80:521` — same mask as `sub_9ED2D0` (`pipeline/ptx-to-ori.md`) |
| Opcodes 93/95 = Ori-internal exit markers | `sub_78B430` reads `op & 0xFFFFFFFD == 0x5D`; `passes/loop-passes.md` and `ir/cfg.md` confirm |
| `+1370 bit 0x10` is the re-entrancy guard | `sub_781F80:332` (`*(_BYTE *)(a1 + 1370) \|= 0x10u`) and `:363` (`v21 & 0xDF` clears it) |
| `force_full_rebuild` second argument | `sub_781F80` prototype `__int64 __fastcall sub_781F80(__int64 a1, char a2, ...)`; `a2` is tested at lines 328, 552, 615 |
| 18 force=1 calls from the register allocator | grep of caller list against the regalloc family in `regalloc/overview.md` (`sub_A88A80`, `sub_A9AEF0`, `sub_A9D140`, `sub_A9DDD0`, `sub_AA2A60`, `sub_AB0500`, `sub_AB93B0`) |
| Idom build lives in `sub_BDFB10`, not here | `ir/cfg.md` "Dominance" section (Cooper-Harvey-Kennedy correction note) |

## Related Pages

- [Basic Blocks & CFG](../ir/cfg.md) — the dominator, RPO, and back-edge data structures `sub_781F80` populates and consumes
- [Loop Passes](loop-passes.md) — `LoopStructurePass` (`sub_78B430`) and `LoopMakeSingleEntry` are the heaviest consumers of the `+280 bit 0x10` LOOP_HEADER and `+152` latch-RPO fields
- [Liveness Analysis](liveness.md) — liveness is recomputed alongside CFG annotations on every `force=1` call from the register allocator
- [Predication](predication.md) — phase 63 calls `sub_781F80` 22 times during the if-conversion sweep
- [Rematerialization](rematerialization.md) — both remat phases call `sub_781F80` before their candidate selection
- [Varying Propagation](varying-propagation.md) — phases 53 and 70 each refresh CFG tags before the divergence dataflow runs
- [Uniform Register Optimization](uniform-regs.md) — `ConvertToUniformReg` reads `+280` bit 0x20 (HAS_PRED) before placing UR-promotion spills
- [Branch & Switch Optimization](branch-switch.md) — UBRA-vs-BSSY decision reads loop-header bits
- [Scheduler Architecture](../scheduling/overview.md) — `sub_A0D800` family calls `sub_781F80` six times during dependency-graph construction
- [Allocator Architecture](../regalloc/overview.md) — 18 of the 36 `force=1` calls originate here, one per spill batch
- [Spilling](../regalloc/spilling.md) — spill-slot adjacency uses loop-depth derived from `+280` bit 0x800000
- [Late Expansion & Legalization](late-legalization.md) — `LateExpansion` calls `sub_781F80` after each block-split to refresh tags
- [Optimization Pipeline](../pipeline/optimizer.md) — phase 3 in the master ordering; the 131-caller fan-in spreads across 18 downstream phases
- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) — phase 3 detail anchor (`### Phase 3: AnalyzeControlFlow -- CFG Finalization`)
- [Pass Inventory](index.md) — full 159-phase table with binary-to-wiki index translation
- [Data Structure Layouts](../ir/data-structures.md) — BasicBlock record at offsets `+120`, `+128`, `+144`, `+152`, `+232`, `+280`, `+282`, `+292` (every field written here is documented there)
