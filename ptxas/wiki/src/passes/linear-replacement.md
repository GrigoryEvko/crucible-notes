# Linear Replacement

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Phase 31 (`OriLinearReplacement`) is the multi-pattern affine-expression linearizer of ptxas. It walks every instruction in every function and, when the operand chain feeding an arithmetic or addressing-mode opcode forms a linear expression `base + scale·index + offset` over already-strength-reduced inputs, it rewrites the chain into a single emitted instruction (LEA-style IADD3 / IADD.X / IMAD.WIDE with a folded immediate) and deletes the intermediates. At 7,084 bytes and 241 basic blocks it is the largest single-function pass in the Ori optimization pipeline; the size is entirely driven by the breadth of its opcode catalog — eleven distinct input shapes are recognized in one flat dispatch over the source-instruction opcode, each with its own legality test, range check, and emission template.

The pass runs immediately after `DoSwitchOptSecond` (phase 30) and before `CompactLocalMemory` (phase 32). Its placement is deliberate: switch lowering exposes new address-computation chains (jump-table base + scaled index + per-target offset) that look syntactically identical to the IADD3-fold targets the linearizer already handles, and local-memory compaction relies on the linearizer having pulled all scale·index multiplies through any intermediate MOV/SEL so that the surviving stack-slot offsets are direct immediates rather than expression trees.

| | |
|---|---|
| **Phase index** | 31 (wiki); 35 (binary phase-table) |
| **Phase name** | `OriLinearReplacement` |
| **Category** | Optimization |
| **Pipeline position** | Stage 2 (Mid-Early Optimization), between `DoSwitchOptSecond` (phase 30) and `CompactLocalMemory` (phase 32) |
| **`execute()` wrapper** | `sub_C5FD10` (4 BBs) — calls the core if knob 487 is enabled |
| **Core logic** | `sub_7EC4B0` (7,084 B, 241 BBs, ~1,400 instructions, 71 outgoing calls) |
| **Gate knob** | 487 (master "general optimization" enable; same gate as `OriStrengthReduce`) |
| **Inner gate knob** | 416 (per-iteration linearization enable; checked at start of every instruction) |
| **SM-tier gate** | bit `0x2` of `*(ctx+1368)` combined with `*(ctx+896) ∈ {4, 5}` — Blackwell datacenter (sm_100/sm_103) only enables the IMAD.WIDE half-fold variant via `sub_7846D0` |
| **Memoization cache** | `sub_7EC080`: 29-bucket open hash table over `(opcode, arity, operand[0..5])`, keyed by an XOR-shift-2 mixer |
| **Per-BB scratch** | per-block fields zeroed at entry: `bb+15` (worklist link), `bb+13` (replacement count), `bb+18` (saved opcode), `bb+3`/`+7` (state/dom-depth) |
| **Outer iteration count** | `*(ctx+520)` — once per function in the compilation unit |
| **Stack frame** | 840 bytes (largest local frame in the pass set) — one cached state record (`v181`, `v182..v209`) plus the worklist arena (`v193[232]`) |
| **Data-table ref** | `0x21DBEF8` — 91-entry destruction-vtable used by the cache record allocator (`sub_8E3A20` family) |

## Why "Linear"

The name refers to **linear (affine) expressions of the form `a·x + b·y + c`** where `x` and `y` are virtual registers and `a`, `b`, `c` are 32-bit signed immediates. Every input shape the pass recognizes is a fragment of such an expression that has been left fragmented across multiple instructions by an earlier pass (canonicalization, IMUL strength reduction, switch lowering, or inline assembly expansion). The linearizer flattens the fragment into a single LEA-style IADD3 / IMAD chain in which all scaling and offset is encoded as immediate operands of one machine instruction.

Concretely the pass targets four equivalence classes of fragment:

1. **`IADD(IADD(x, c1), c2) → IADD(x, c1 + c2)`** — constant-fold of a two-step addition produced by separate phases (e.g. stack-slot base + per-call-site frame offset).
2. **`IADD(MUL(x, k), c) → IMAD.WIDE.U32(x, k, c)`** — collapse of an explicit `index*scale` followed by an offset add into the natively-supported IMAD multiply-add. Targets array-indexing `&a[i] = base + i*sizeof(T)`.
3. **`SHL(SHL(x, n1), n2) → SHL(x, n1+n2)` (and the cross-form with IMUL)** — shift-chain collapse; only fires when `n1+n2 ≤ 31` and the intermediate is single-use, otherwise leaves the chain for the later peephole BFE-fold.
4. **`SEL(p, x, y) → predicated MOV / IADD with folded constant`** — when one arm of a select is a compile-time constant, the linearizer canonicalizes the encoding so that later predication can fold the constant into the same IADD that consumes the live arm.

Confidence: **HIGH** on the existence of the four classes (each has a distinct case in the opcode switch and a distinct emission template); **MED** on the precise predicate-folding semantics of class 4 (uses `sub_748440`, which is a thin wrapper that returns a register-id-encoded constant when the operand is a flag-9 special, and a constant-pool index otherwise).

## Algorithm Overview

```c
// Distilled from sub_C5FD10 → sub_7EC4B0.
// Argument: a CodeObject* (the per-function IR container).
// Returns: nothing; rewrites the IR in place.

void OriLinearReplacement(CodeObject *ctx) {
    // ── Step 0: master gate ────────────────────────────────────
    // knob 487 is the same master gate as OriStrengthReduce and
    // every other "general optimization" enabled phase.
    if (!knob_enabled(ctx->knob_mgr, 487))
        return;

    // ── Step 1: re-seed use-def / dominance state ──────────────
    sub_781F80(ctx, 1, ...);    // 8.3 KB helper: rebuild SSA use-def chains
    sub_775010(ctx);             // small leaf: clear per-pass scratch
    if ((ctx->target_flags & 0x2) && (ctx->sm_class - 4) <= 1) {
        // SM tier in {sm_100, sm_103} — Blackwell datacenter
        sub_7846D0(ctx, ...);    // enable IMAD.WIDE half-fold variant
    }
    sub_7D92F0(ctx, 1);          // 2 KB helper: rebuild dominator-depth view

    // Reset per-BB scratch on every basic block in the linked list at ctx+104.
    for (bb *B = ctx->bb_head; B; B = B->next) {
        B->worklist_next = -1;          // *((dword*)B + 7) = -1
        B->rewrite_count = 0;           // B[15] = 0
        B->folded_const  = 0;           // B[13] = 0
        B->cache_ptr     = 0;           // B[18] = 0
        B->state         = 0;           // *((dword*)B + 3) = 0
    }

    // ── Step 2: outer loop over functions ──────────────────────
    // ctx+520 holds the function count for this compilation unit
    // (PTX modules can contain multiple device functions).
    int n_functions = ctx->function_count;
    if (n_functions <= 0) goto cleanup;

    int v168 = 4;       // byte offset into ctx->function_id_array
    int v164 = 1;       // current function 1-based index
    LinearCache cache = init_cache(ctx);   // sub_7EB830 — clears the 29-bucket hash

    for (;;) {
        int fn_id = ctx->function_id_array[v168 / 4];
        Function *F = ctx->function_table[fn_id];
        sub_7EB830(&cache);     // reset cache between functions

        // ── Step 3: walk every instruction in F ─────────────────
        Instruction *I = F->first_instr;
        if (I->next == F->first_instr) goto next_function;   // empty function

        do {
            // ── Step 3a: per-iteration gate (knob 416) ──────────
            if (!knob_enabled(ctx->knob_mgr, 416)) goto cleanup;

            // ── Step 3b: source-operand pre-pass ────────────────
            // Walk source operands of I looking for ALREADY-replaced
            // registers (the replacement-id was set by an earlier
            // pass — OriStrengthReduce or a previous iteration of
            // this loop). When found, follow the replacement chain
            // and either (a) materialize a new MOV-like (opcode 130)
            // when the chain bottoms out at an immediate, or (b)
            // emit a folded IADD with the absorbed displacement.

            for (int op = 0; op < I->arity; ++op) {
                if (!is_register(I->operand[op])) continue;
                if (!I->operand[op].flag & 0x01000000) continue;  // dead-link bit

                VReg *def = lookup_vreg(ctx, I->operand[op]);
                int rep_id = def->replacement_id;        // def+28
                if (rep_id < 0) continue;

                ReplaceRec *r = &ctx->replace_table[rep_id];
                // Reject if dominance violation OR loop-depth bump
                // would increase register pressure.
                if (loop_depth(r->def_bb) != loop_depth(I->bb) &&
                    (ctx->sm_class - 4 > 1 ||
                     rpo_depth(r->def_bb) < rpo_depth(I->bb))) {
                    continue;
                }

                // Compute the absorbed displacement.
                int64_t disp = r->displacement;
                if (disp >= 0) {
                    // small positive displacement: fold straight in
                    disp += immediate_of_operand(ctx, &I->operand[op]);
                    if (fits_signed_32(disp))
                        emit_folded_iadd(ctx, I, r, op, disp);
                } else {
                    // negative displacement: requires 0x80000000 bias
                    // tweak to keep the encoder in signed-32 range.
                    int64_t base = ctx->stack_imm_table[I->operand[op].imm_idx];
                    int64_t total = base + disp + 0x80000000LL;
                    if (total > 0xFFFFFFFFLL)
                        goto biased_path;   // ← LABEL_102 in the binary
                    emit_folded_iadd(ctx, I, r, op, disp);
                }
            }

            // ── Step 3c: opcode-keyed main dispatch ─────────────
            // The big switch. v54 = I->opcode & 0xFFFFCFFF (mask out
            // modifier bits 12..13 — same masking convention as
            // OriStrengthReduce).
            uint32_t op = I->opcode & 0xFFFFCFFFu;
            VReg *dst = lookup_vreg(ctx, I->operand[0]);
            if (dst->reg_class != 6) {     // class 6 = "general integer"
                goto walk_next;
            }

            switch (op) {
            // ── Pattern A: IADD3 / LEA fold ────────────────────
            case 2:    // IADD
            case 5:    // ISUB
                if (op == 5 && sub_730A80(I)) goto fallthrough_canon;
                pattern_iadd3_or_lea(ctx, I, &cache);
                break;

            // ── Pattern B: branchless idiom canonicalization ───
            case 98:   // LOP3-like / pre-IMAD logical
                pattern_force_branchless(ctx, I, &cache);
                break;

            // ── Pattern C: IMAD scale·index + offset fold ──────
            case 110:  // IMAD (3-operand)
                pattern_imad(ctx, I, /*off_arr=*/8, /*off_scale=*/16,
                                       /*arity=*/3,  /*chk_op=*/2, &cache);
                break;
            case 112:  // IMAD with predicate / 4-operand
                if (((I->operand[arity-1].flag >> 1) & 3) == 1 || sub_730A80(I))
                    goto fallthrough_canon;
                pattern_imad(ctx, I, /*off_arr=*/16, /*off_scale=*/24,
                                       /*arity=*/4,  /*chk_op=*/3, &cache);
                break;

            // ── Pattern D: MOV-like passthrough fold ───────────
            case 130:  // post-strength-reduce MOV marker
                pattern_mov_passthrough(ctx, I, &cache);
                break;

            // ── Pattern E: scale-by-immediate fold (IMUL/IMAD) ─
            case 139:  // IMUL.HI-class
            case 141:  // IMUL.WIDE-class
                pattern_scale_by_imm(ctx, I, /*idx=*/2, &cache);
                break;

            // ── Pattern F: select with constant arm ────────────
            case 195:  // SEL (predicated)
                pattern_sel_with_const(ctx, I, &cache);
                break;

            // ── Pattern G: SHL+SHL collapse ────────────────────
            case 213:  // SHL
                pattern_shl_chain(ctx, I, &cache);
                break;

            default:
                goto fallthrough_canon;
            }

walk_next:
            // ── Step 3d: chain bookkeeping ──────────────────────
            // If we deleted I, advance to its replacement; if we
            // converted in place, leave I->next as the cursor.
            I = I->next;
        } while (I != F->first_instr);

next_function:
        // ── Step 4: post-function fixups ────────────────────────
        // Walk the deferred-emit list (cache.pending_head) and
        // commit any folded IADDs that were held back to allow
        // further pattern matching upstream.
        for (Instr *p = cache.pending_head; p; p = p->next) {
            if (p->rewrite_count > 0)
                sub_7EB390(&cache, p);   // commit pending IADD
        }
        cache.pending_head = NULL;

        v164++;
        v168 += 4;
        if (v164 > n_functions) break;
    }

cleanup:
    destroy_cache(&cache);
    destroy_state(ctx);
}
```

The dispatch is structured as **one flat opcode switch with eleven entries**, not the inheritance hierarchy a C++ compiler would emit if each pattern were a separate visitor class. ptxas favours flat dispatch throughout the optimization pipeline; this trades C++ polymorphism for tight branch predictability and predictable register pressure inside the matcher itself (the function uses 16+ live `r*d`/`e*x` registers across the switch, and a vtable indirection on every entry would defeat the predictor).

Confidence: **HIGH** for the gate-knob/initialization/outer-loop structure (directly visible in the prologue/epilogue); **MED** for the per-pattern semantics tags (the four-letter pattern names are reconstructed from the emitted opcodes and from the helper-function signatures, not from any string the binary embeds).

## Pattern Catalog

The table below enumerates every distinct input shape the matcher recognizes, the legality conditions that must hold, and the SASS shape it produces. Operand offsets in the "Probe" column are relative to the source instruction record at `+84` (operand 0), `+92` (operand 1), `+100` (operand 2), `+108` (operand 3).

| # | Source opcode (v54) | Input shape | Output shape | Enabling conditions | Helpers used |
|---|---:|---|---|---|---|
| 1 | **2** (IADD) | `IADD(IADD(x, c1), c2)` where `c1+c2` fits signed-32 | `IADD3(x, c1+c2, 0)` | dst is class-6 int; both summands have `(flag & 0x603FFFF) == 0`; intermediate IADD is single-use | `sub_7D6780`, `sub_7D87F0`, `sub_7DAFF0`, `sub_7EBBF0` |
| 2 | **2** (IADD) | `IADD(SHL(x, n), c)` | `LEA Rd, x, c, n` (`IADD3 + shift`) | shift count `n ≤ 31`; SHL has single use; type class compatible (`sub_7D6780(opcode_for_type)`) | `sub_7DB140`, `sub_7EBBF0`, `sub_7EC260` |
| 3 | **5** (ISUB) | `ISUB(x, IADD(y, c))` | `IADD3(x, ~y, -c)` (3-way subtract) | not blocked by `sub_730A80` (predicated-operand reject); `c` fits | `sub_7D6780`, `sub_7DAFF0`, `sub_7DB140` |
| 4 | **98** (LOP3-pre) | any LOP3 candidate | LOP3 with both source operands forced through `sub_7EBDF0(1,0)` + `sub_7EBDF0(2,0)` (canonicalization to branchless form) | always when reached — no legality test beyond opcode 98 | `sub_7EBDF0`, `sub_7EC260` |
| 5 | **110** (IMAD3) | `IMAD(x, k, IADD(y, c))` | `IMAD.WIDE(x, k, y) + c` folded as 4th operand of subsequent IADD3 | scale `k` fits in `sub_7D87F0` returned range; `(flag & 0x603FFFF) == 0`; not `v209` (volatile/sync) | `sub_7D87F0`, `sub_7EBBF0`, `sub_7EBDF0` |
| 6 | **112** (IMAD4) | `IMAD(x, k, y, c)` predicated variant | `IMAD.HI / IMAD.WIDE` with merged predicate | `(operand[arity-1].flag >> 1) & 3) != 1`; `sub_730A80` reject false | `sub_7D87F0`, `sub_7D67E0`, `sub_7EBBF0` |
| 7 | **130** (MOV-like) | `MOV(SHR(LDC, n)) → MOV(LDC>>n)` constant load from constant memory bank | folded `LDC.U32 c[bank][offset]` with adjusted bank | inner def is opcode 130 with class-6 dst; outer operand[1] is `c[..]` bank reference (flag 9 in `sub_748440`) | `sub_748440`, `sub_7EB4B0`, `sub_7EB2F0` |
| 8 | **139** (IMUL.HI) | `IMUL.HI(x, c)` with c power of 2 | `SHR.U32(x, log2(c))` if `log2(c) ≤ 31` | `sub_7D6780(operand_type)` returns true; `(flag & 0x603FFFF) == 0`; cache miss | `sub_7D87F0`, `sub_7EBBF0` |
| 9 | **141** (IMUL.WIDE) | `IMUL.WIDE(x, c)` with c power of 2 | `SHF.L(x, log2(c))` (wide shift) | same as #8 plus `v109 = (opcode == 141)` switches `sub_7D67E0` post-fold to negate the sign-correction | `sub_7D67E0`, `sub_7D87F0`, `sub_7EBBF0` |
| 10 | **195** (SEL) | `SEL(p, x, IMM)` or `SEL(p, IMM, x)` | predicated `IADD(x, IMM)` / `MOV(IMM)` with the constant absorbed into the immediate field | `*(operand[1]+24) == 1` (constant-arm tag); `sub_7D6780` accepts the type; **not** vector-side bridged (`sub_730A80` reject); `flag 6` indirection | `sub_748440`, `sub_7EBBF0` |
| 11 | **213** (SHL) | `SHL(SHL(x, n1), n2)` | `SHL(x, n1+n2)` if sum ≤ 31; else fall through to peephole BFE-fold | inner SHL single-use; inner def has `opcode_modifier & 0xCF == 2` (true SHL, not LEA-mode); type tag at `operand[2]` is 2 or 3 (immediate) | `sub_7D6780`, `sub_7DB140`, `sub_7DAFF0`, `sub_92F490`, `sub_92FE10`, `sub_934630` |

Confidence: **HIGH** for cases 1, 2, 5, 6, 8, 9, 10, 11 (each has a unique exit point with a distinct emitter call); **MED** for cases 3 and 7 (the input shape is reconstructed from operand-offset arithmetic in the binary, not from string evidence); **LOW** for case 4 (the v54==98 path is short — three sub_7EBDF0 calls — and could equally well be a "no-op idiom canonicalization" with no semantic intent beyond preparing for the next pass, rather than a true linearization).

### Bias-by-`0x80000000` trick

Patterns 1, 2, 3, 8, 9 all share the same range-check idiom:

```c
if ((uint64_t)(disp + 0x80000000ULL) > 0xFFFFFFFFULL) {
    // displacement overflows signed-32 — try the biased path:
    disp = disp + 0x80000000ULL;
    biased = true;
    if ((uint64_t)(stack_base + disp + 0x80000000ULL) > 0xFFFFFFFFULL) {
        goto biased_path_2;
    }
    // ...
}
```

The expression `(uint64_t)(x + 0x80000000) > 0xFFFFFFFF` is the canonical "does signed-32 fit" test (`INT32_MIN ≤ x ≤ INT32_MAX`). When a fold would not fit in signed 32, the matcher does **not** reject the rewrite outright; instead it adds `0x80000000` to the displacement, marks the result with bit `0x90000000` in the operand type field (instead of the usual `0x10000000`), and emits a different IADD encoding (`sub_92F490` instead of `sub_92FE10`) that the encoder will lower to a `IADD3.X` (carry-extended) instead of a plain `IADD3`. Confidence: **MED** — the `0x90000000` vs `0x10000000` distinction is robust (visible at `0x7ed946` and `0x7ed32a`), but the SASS-level interpretation of the two encodings is inferred from cross-reference with `passes/late-legalization.md` rather than directly stated.

> ⚡ **QUIRK — half-fold is sm_100/sm_103 only**
> The Blackwell-datacenter IMAD half-fold path (case 5 of the catalog, when the multiply produces a 64-bit intermediate and the linearizer wants to merge the low half with a 32-bit displacement) only runs when `(ctx->target_flags & 0x2) != 0` AND `ctx->sm_class ∈ {4, 5}`. On consumer Blackwell (sm_120/sm_121), Hopper (sm_90), and earlier, this path is gated off and the multi-instruction sequence survives. The rationale is the IMAD.WIDE.X variant introduced in Blackwell datacenter that allows a 33-bit immediate displacement; pre-Blackwell, the same fold would require a temporary register holding the upper bits and the rewrite would be a net loss. Confirmed at `0x7ec678..0x7ed253` (the `sub_7846D0` call site is gated on the same two conditions).

## Memoization Cache (`sub_7EC080`)

The linearizer interns synthesized linear sequences in a 29-bucket open hash table keyed by the source-instruction signature. This is critical because the same address-computation chain (`base + i*stride + offset`) typically appears at dozens of memory operations in a single kernel — for example, every load and store inside an unrolled tile of a GEMM kernel computes the same `lane_id * 4 + lane_id_inner * 1024 + iter * 16384` expression. Without caching, the linearizer would re-synthesize the full IADD3 chain for every load, doubling instruction count.

The hash function is a 2× XOR-shift mixer:

```c
// sub_7EC080 prologue — hash of operand signature
int v4 = arity;                            // *((dword*)a1 + 95)
if (v4 > 0) {
    v4 = (2*(opnd[0] ^ (2*v4))) ^ opnd[2] ^ opnd[0] ^ (2*v4);
    if (arity != 1) {
        v4 = (2*(opnd[3] ^ (2*v4))) ^ opnd[5] ^ opnd[3] ^ (2*v4);
    }
}
int bucket = v4 % 0x1D;     // 29-bucket open hash
```

The mixer is **structurally XOR-shift-2** (left-shift by 1, XOR with the next operand), not a published hash family — its only design constraint is that the lower 5 bits decorrelate well after one round, which the bucket count of 29 (the largest prime ≤ 32) is chosen for. Lookup walks the chain from `bucket * 8` until either an entry matches on all of `(opcode, arity, operand[0..5], v62 flag, displacement, RegClass)` or the chain ends; on hit, the cached emission's destination vreg is reused directly via `sub_7EB2F0`. Confidence: **HIGH** (the mixer is explicit in the decompilation).

> ⚡ **QUIRK — cache is invalidated per-function but not per-BB**
> `sub_7EB830` resets the cache between functions but not between basic blocks within a function. This means a linearized expression in BB#5 can reuse the cached destination vreg from BB#3 even though they may live in different loop nests. The pass relies on the dominance-and-loop-depth guard in step 3b to suppress reuse that would extend a live range across a loop back-edge — but the guard is checked **per instruction**, not at cache insertion, so the cache can transiently hold entries that would be rejected as reuse targets. The net effect is a small upper bound on register-pressure inflation that the later allocator (`AdvPhAllocReg`, phase 101) cleans up via spill+reload.

## Function Map

| Address | Size | Role | Source-of-truth |
|---|---:|---|---|
| `sub_C5FD10` | ~64 B (4 BBs) | `OriLinearReplacement::execute` — vtable wrapper; gates on knob 487, then tail-calls `sub_7EC4B0` | Caller table |
| `sub_7EC4B0` | 7,084 B (241 BBs) | Core matcher + emitter. Contains the eleven-case opcode switch, the per-function outer loop, and the post-function commit | This page |
| `sub_781F80` | 8,335 B (454 BBs) | Use-def chain rebuild — same helper called by every pass that needs canonical UD chains; sets the `v22+128` / `v22+136` next/prev fields used by `sub_7EB4B0` | Called once at entry |
| `sub_775010` | 18 B (4 BBs, leaf) | Scratch reset — clears per-pass flag bits | Called once at entry |
| `sub_7846D0` | 21 B (4 BBs, leaf) | Enable the SM-tier-gated IMAD.WIDE half-fold variant | sm_100/sm_103 only |
| `sub_7D92F0` | 1,993 B (133 BBs) | Dominator-depth / RPO numbering rebuild — populates the `bb+156` depth field that step 3b reads | Called once at entry |
| `sub_7EB830` | 960 B (55 BBs) | Cache reset / per-function init — walks any pending entries and zeroes the 29-bucket array | Called at top of every outer iteration |
| `sub_7EB4B0` | 160 B (13 BBs, leaf) | Instruction unlink — removes an instruction from the BB linked list (the destructive half of the rewrite) | Called from cases 2, 7, 11 |
| `sub_7EB2F0` | 146 B (8 BBs) | Operand patch — writes the new operand triple into a use site after a cache hit | Called from cases 5, 7, 8, 9 |
| `sub_7EC080` | 468 B (29 BBs) | Cache lookup — XOR-shift-2 hash + open-chain walk; returns the cached emission or NULL | Called from every emit path |
| `sub_7EC260` | 580 B (22 BBs) | Cache insert — wraps a freshly emitted instruction in a 72-byte cache record and links it into the bucket chain | Called from every emit path |
| `sub_7EBBF0` | 511 B (29 BBs) | Operand-source rewriter — chases the use-def chain backward from a single operand and applies the absorbed displacement; the workhorse for cases 1, 2, 5, 6, 8, 9, 10 | Called many times per outer iteration |
| `sub_7EBDF0` | 141 B (6 BBs) | Operand-source forcer — same as `sub_7EBBF0` but unconditional (used in case 4 to canonicalize before any pattern match) | Called from case 4 |
| `sub_7EB160` | 394 B (28 BBs) | Range-check materializer — emits the biased-by-`0x80000000` correction sequence when a fold would overflow signed-32 | Called from the `LABEL_106` path |
| `sub_7DF0D0` | 462 B (21 BBs) | Reverse-displacement helper — given an operand and a target type, computes the constant adjustment needed to reproduce the original semantic | Called from the `LABEL_94` (post-fold) path |
| `sub_92F490` | 134 B (1 BB) | IADD3 emitter — opcode 130 (MOV-like) with type-tag-90 operand encoding | Case 11 negative-disp path |
| `sub_92FE10` | 149 B (1 BB) | IADD3 emitter — opcode 2 (true IADD) with type-tag-10 encoding | Cases 1, 11 positive-disp path |
| `sub_934630` | 1,213 B (47 BBs) | Multi-operand emitter — used when the rewrite needs 3 or 4 source operands (IADD3, IMAD.WIDE) | Cases 5, 6, 9, 11 |
| `sub_91BF30` | 535 B (15 BBs) | Virtual register allocator — same as in `OriStrengthReduce`; allocates a 160-byte vreg descriptor | Called once per new replacement |
| `sub_9253C0` | 325 B (24 BBs) | Instruction delete — same helper used everywhere; unlinks the rewritten instruction from BB | Called at the end of each rewrite |
| `sub_7D6780` | 35 B (3 BBs, leaf) | Type-class membership test — returns `(1ULL << opcode_type) & 0x100001FE00LL != 0` (i.e. opcode-type ∈ {9, 10, 11, 12, 13, 14, 15, 16, 32}) | Called as a fast type-class predicate from cases 1, 2, 3, 4, 6, 8, 9, 10, 11 |
| `sub_7D67E0` | 25 B (3 BBs, leaf) | Sign-extension predicate — `(1ULL << opcode_type) & 0xAA00 != 0` (opcode-type ∈ {9, 11, 13, 15}) — checks for the four signed integer widths | Cases 6, 9 |
| `sub_7D6850` | 10 B (1 BB, leaf) | "Is predicate" — `(opcode_type - 9) <= 1` (opcode-type ∈ {9, 10}) | Case 11 |
| `sub_7D87F0` | 73 B (4 BBs) | Scaled-constant extractor — returns the immediate component of an operand (constant-pool index or inline immediate); fast-paths predicate types via `sub_91D2C0`, otherwise via `sub_91D150` | Cases 5, 6, 8, 9, 10, 11 |
| `sub_730A80` | 87 B (4 BBs, leaf) | "Last operand is type-7 (special)" — the matcher uses this to reject instructions whose final operand is a special register (P0..P6, RZ, URZ), which the linearizer cannot validly rewrite | Cases 3, 6, 10 |
| `sub_748440` | 68 B (6 BBs, leaf) | Constant-arm canonicalization — when operand has type-1 register but the vreg's `+64` field is 9 (predicate class), returns `*(vreg+68)` (a packed constant); otherwise returns `sub_91D150` lookup | Cases 7, 10 |
| `sub_7DAFF0` | 171 B (8 BBs) | Operand-encoding emitter — packs a 32-bit immediate plus type tag into the operand triple used by `sub_934630`; handles both signed-32 immediates and `0x60`-prefix constant-pool references | Cases 1, 2, 3, 11 |
| `sub_7DB140` | 157 B (10 BBs) | Stride-mask computer — given an operand and a type, computes `(stride << type_shift)` truncated to 33 bits; used to bias the displacement for type-correct IADD3 emission | Cases 2, 3, 11 |
| `sub_92C0D0` | 358 B (17 BBs) | IADD-from-SHL emitter — used in case 11 when the post-collapse SHL produces an opcode-41-tagged result that must be re-encoded as IADD-of-shifted-immediate | Case 11 sub-path |
| `sub_91D160`, `sub_91D2A0` | 318 / 31 B | Immediate-pool helpers — interns a 64-bit immediate into the per-function constant pool and returns its 24-bit index; `91D2A0` is the short-path for already-pooled constants | Cases 11, helper |
| `sub_8E3970`, `sub_8E3A20` | 162 / 90 B | Cache-record allocator/destroyer — the 72-byte records inserted by `sub_7EC260` and walked at function exit | Allocator infrastructure |
| `sub_7D8C90` | 126 B (8 BBs, leaf) | Knob lookup (knob 416 / 487) — used twice per outer iteration; once at the top of the function loop and once inside the inner loop to short-circuit on knob 416 | Master knob check |

Confidence: **HIGH** for the role descriptions of the leaf helpers (each is small enough that its full body is visible); **MED** for `sub_7EBBF0` and `sub_7EBDF0` (29 + 6 BBs respectively; the role is inferred from the call sites and the operand-mutation pattern, not from any string evidence).

## Pipeline Context

```
Phase 28  SinkRemat                  ┐
Phase 29  GeneralOptimize            │   produces unrolled, strength-reduced,
Phase 30  DoSwitchOptSecond          ┤   switch-lowered IR with many spurious
Phase 31  ★ OriLinearReplacement     │   intermediates that linearizer absorbs
Phase 32  CompactLocalMemory         ┘
Phase 33  OriPerformLiveDeadSecond       cleans up the now-dead temporaries
...
Phase 53  OriPropagateVaryingFirst       sees fewer vregs after linearization
```

Phase 31 runs after:

- **Phase 21 (`OriStrengthReduce`)** has produced opcode-130 (MOV-like) markers that mark "this value is just a copy of its source after strength reduction" — case 7 of the catalog specifically targets these.
- **Phase 29 (`GeneralOptimize`)** has constant-folded local immediates, leaving only the constants that depend on inter-instruction relationships (e.g., switch base + per-target offset).
- **Phase 30 (`DoSwitchOptSecond`)** has lowered switches to indexed-load + indirect-branch, producing the `LDC.U64 c[bank][index*8 + base]` pattern that the linearizer collapses via case 2.

Phase 31 runs before:

- **Phase 32 (`CompactLocalMemory`)** — relies on the linearizer having absorbed all `stack_base + scale*idx + per-call-offset` chains so that surviving local-memory references are direct immediates.
- **Phase 33 (`OriPerformLiveDeadSecond`)** — cleans up the intermediate vregs the linearizer has just orphaned. The pass deliberately does not run DCE itself; that is liveness's job.
- **Phase 101 (`AdvPhAllocReg`)** — operates on the reduced register pressure produced by linearization.

> ⚡ **QUIRK — interaction with strength-reduction is bidirectional**
> Phase 21 (`OriStrengthReduce`) reduces `x * (2^n)` to `x << n` but does **not** then collapse `(x << n1) << n2` to `x << (n1+n2)`. It leaves that case for the linearizer (case 11 here). Conversely, the linearizer's case 2 (`IADD(SHL, c)`) and case 11 (`SHL+SHL`) **only fire when the resulting shift count is `≤ 31`**; for larger counts, the chain is left intact and the peephole BFE-folder in `sub_81DB30` picks it up later. The two passes are thus complementary: strength-reduction does the unary-to-binary reduction, the linearizer does the binary-to-binary collapse, and the peephole does the wide-bit-field cleanup. None of the three alone is sufficient.

> ⚡ **QUIRK — case 4 (LOP3-pre canonicalization) emits no replacement**
> The case-98 branch is the only one in the entire dispatch that does not produce a new instruction. It calls `sub_7EBDF0` twice (operands 1 and 2) and then `sub_7EC260` (cache insert) — but the cache record it inserts is **for the original instruction unchanged**. The effect is purely to mark the operands as "branchless-canonicalized" so that the later peephole pass (`MainPeepholeOptimizer`, `sub_83EF00`, invoked from within the next `GeneralOptimize` bundle at phase 37) will recognize the LOP3-eligible form and fuse it into a single LOP3. The linearizer is doing the bookkeeping work of strength reduction without itself emitting a strength-reduced instruction; the actual fusion happens 6 phases later.

## Storage Layout

Per-instruction state during the linearizer pass (extracted from the v200..v209 / v176..v179 cluster of locals in the decompilation):

```
v176  (offset +0):  current operand triple (32-bit type|reg + 32-bit imm)
v177  (offset +4):  byte flag — bit 0 = "has displacement to absorb"
v199  (+0):         operand[2] of pending emission
v200  (+0):         operand[3] of pending emission  
                    HIDWORD(v200) = vreg ID of the *source* instruction's dst
v201  (+0):         byte flag — 0 = type-2 IADD, 1 = type-3 LEA
v202..v204:         operand[4..6] of pending emission (for IADD3 / IMAD.WIDE)
v205, v206:         pointers — v206 = source instruction, v205 = parent BB
v207, v208:         flag word for the emit call (mode, mask)
v209:               byte flag — "do not commit immediately, queue for post-fn"
```

The pending-emission cluster is constructed in-place by every pattern's emit prologue (visible as the repeated `v200 = 0; v202 = 0; v199 = 0; v205 = 0; v207 = 0; v208 = 0; v206 = v22; HIDWORD(v200) = v_dstid; v201 = 0;` pattern at multiple labels), then passed to either `sub_7EBBF0` (single-operand rewriter) or `sub_934630` (full-instruction emitter) along with a pointer to the original instruction `v22`. Confidence: **MED** — the field layout is consistent across all eleven emit prologues, but the role assignments here are inferred from which operands `sub_934630` and `sub_92FE10` read at known offsets.

## Worked Example

Input IR (after phase 30, before phase 31):

```
   R10 = LDC c[0][0x10]           ; load kernel param "base"
   R11 = S2R SR_TID_X             ; per-lane thread id
   R12 = IMUL.WIDE R11, 4         ; index byte offset
   R13 = IADD R10, R12            ; effective base
   R14 = IADD R13, 32             ; final address = base + i*4 + 32
   R15 = LDG.E [R14]              ; consume
```

The linearizer walks this in source order. Reaching R13, opcode 2 (IADD), case 1 of the catalog:

1. Source operand 1 (`R12`) has its def at the IMUL.WIDE; that's an opcode-141 (case 9) which has already been visited and converted into a strength-reduced IADD-of-shifted-imm marker (opcode 130, case 7). The linearizer pulls the shifted-immediate value `lsh(R11, 2)` directly.
2. Source operand 2 (`R10`) is an LDC; the linearizer adds the LDC offset (`0x10`) to the running displacement.
3. The cache is queried with signature `(IADD, 2, R11, 0, R10_LDC_imm)`. Miss.
4. R13 is converted in place into `IADD3 R13, R11, c[0][0x10], 0` with the shift-by-2 absorbed via case 2 (the second SHL/IADD pattern). The intermediate R12 has no remaining users and is queued for DCE.

Reaching R14, opcode 2 (IADD), case 1 again:

1. Source operand 1 (`R13`) has just been linearized; its replacement record at `def+28` points to the IADD3 emitted in the previous step.
2. Source operand 2 is immediate `32`.
3. The cache is queried with signature `(IADD3, 3, R11, 0x10, 32)`. Hit on the previous record? No, the displacement is different.
4. The linearizer re-emits `IADD3 R14, R11, c[0][0x10]+32, 0` (absorbing the `+32` into the constant offset). The cache insert key is computed; insert succeeds.
5. R13 is now dead and queued for DCE.

Output IR (after phase 31):

```
   R11 = S2R SR_TID_X
   R14 = IADD3 R11, c[0][0x10]+32, 0    ; single-instruction address computation
   R15 = LDG.E [R14]
```

Three instructions become one. The intermediate vregs (R10, R12, R13) are queued for `OriPerformLiveDeadSecond` (phase 33) to remove. Net: 3 instructions and 3 vregs eliminated in exchange for one slightly wider IADD3.

> ⚡ **QUIRK — the immediate-folding does not always produce a shorter instruction**
> Case 1's `IADD(IADD(x, c1), c2) → IADD(x, c1+c2)` rewrite increases the operand encoding of the surviving IADD by 0–8 bytes (because `c1+c2` may not fit in a small immediate even though `c1` and `c2` individually did). The matcher does not check this — it always prefers the fewer-instruction sequence. In practice the encoder is allowed to spill the wide immediate into the constant bank if it does not fit, which produces a `LDC + IADD` two-instruction sequence — i.e., the same length as the input. The optimization is then a no-op at the SASS level but still cleaner at the Ori IR level. Confidence: **MED** — directly observable in `ptxas -v` output on programs that exhibit `int + small_const + small_const` chains.

## Verification Anchors

| Claim | Anchor in raw data |
|---|---|
| Pass is phase 31 / bin 35 named `OriLinearReplacement` | `passes/index.md` row 458; static phase-name table at `off_22BD0C0` |
| Core function is `sub_7EC4B0` (7,084 B, 241 BBs, 71 callees) | Direct from `ptxas_functions.json` and `decompiled/sub_7ec4b0_0x7ec4b0.c` |
| Single caller `sub_C5FD10` (4 BBs) is the vtable wrapper | `ptxas_functions.json` reverse-callers for `sub_7EC4B0` |
| Master gate is knob 487 | Constant `0x1E7` at `0x7ec65b` (direct call to `sub_7D8C90(v7, 487)`) |
| Per-iteration gate is knob 416 | Constant `0x1A0` at `0x7ec802` (call to `sub_7D8C90(v23, 416)`) |
| SM-tier gate enables Blackwell IMAD.WIDE half-fold | `(*(a1+1368) & 2) != 0 && (unsigned)(*(a1+896) - 4) <= 1` at `0x7ed253` |
| Opcode dispatch covers `{2, 5, 98, 110, 112, 130, 139, 141, 195, 213}` | Direct from decompiled switch at `0x7ed4f8` and following |
| 29-bucket hash table for memoization | `% 0x1Du` at `0x7ec0a4` (in `sub_7EC080`) |
| XOR-shift-2 mixer with 1–2 rounds | `v4 = (2*(opnd[2*v4]^...)) ^ ...` at `0x7ec095..0x7ec0a0` |
| Signed-32 range check via `+ 0x80000000` bias | `(uint64_t)(v75 + 0x80000000LL) > 0xFFFFFFFFLL` at `0x7ed0c0`, `0x7ed106`, others |
| Data table at `0x21DBEF8` is the 91-entry cache destruction vtable | `ptxas_data_tables.json` entry for `0x21dbef8`; 91 slots into `sub_8E3A20` / `sub_8E3970` family |
| 840-byte stack frame | IDA stack-frame dump in `context/sub_7EC4B0_0x7ec4b0.md` |

## Cross-References

- [Pass Inventory & Ordering](index.md) — phase 31 row in the 159-phase table; sequence with `DoSwitchOptSecond` (30) and `CompactLocalMemory` (32)
- [Strength Reduction](strength-reduction.md) — phase 21; produces the opcode-130 MOV-like markers that case 7 of this pass consumes
- [Branch & Switch Optimization](branch-switch.md) — phase 30 (`DoSwitchOptSecond`); produces the LDC-base + scaled-index chains that case 2 collapses
- [GeneralOptimize Bundles](general-optimize.md) — phase 29; constant-folding pre-pass whose output the linearizer further reduces
- [Predication](predication.md) — phase 63; case 10 (SEL fold) prepares the operand encoding that predication later absorbs
- [Peephole Optimization](codegen/peephole.md) — `MainPeepholeOptimizer` (`sub_83EF00`, invoked from inside the GeneralOptimize bundles; not a standalone phase); picks up the BFE-eligible chains that the linearizer leaves intact (shift sums > 31)
- [Instruction Selection](codegen/isel.md) — IADD3 / IMAD.WIDE / LEA-mode encoding that the linearizer's emitters produce
- [Late Expansion & Legalization](late-legalization.md) — handles the `0x90000000` vs `0x10000000` operand type tags introduced by the biased-displacement path
- [Liveness Analysis](liveness.md) — `OriPerformLiveDeadSecond` (phase 33) immediately downstream; cleans up the intermediate vregs that the linearizer has orphaned
- [Rematerialization](rematerialization.md) — operates on the reduced expression trees the linearizer has produced
- [Ori IR Overview](ir/overview.md) — instruction format, opcode encoding, operand type-tag conventions (bits 28..30)
- [Register Model](ir/registers.md) — vreg descriptor layout, the `replacement_id` field at `vreg+28` that this pass writes
- [Knobs System](config/knobs.md) — knob 487 (master) and knob 416 (per-iteration) gating
