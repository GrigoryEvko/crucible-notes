# Instruction Movement Engine

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Binary phases 78 (`DoKillMovement`) and 79 (`DoTexMovement`), together with two unnamed sibling movement phases, are four thin wrappers (`sub_C5FE00`, `sub_C5FE30`, `sub_C5FE60`, `sub_C5FE90`) that all tail-call the same 573-byte engine, `sub_8FFDE0`. The wrappers differ in exactly one register: the second argument passed to the engine — a movement-kind discriminator with values 0, 1, 2, 3. The engine then applies that discriminator at three internal decision points to select the direction of motion (down vs up), the destination block class (last-use vs preheader), and whether the cleanup-emission helper `sub_785E20` is invoked at the end. The entire scheme is a single parameterised movement primitive masquerading as four phases.

The engine reuses the LICM dataflow built earlier in the pipeline by `OriHoistInvariantsLate` (wiki 66 / binary 88). It is gated on the same `HoistInvariants` named-phase token that gates the late LICM pass, and the underlying movement worker `sub_A112C0` is the same function that LICM itself calls. The "movement" phases are therefore best understood as **post-LICM positional fixups** that drive the LICM worker in different modes to relocate specific instruction classes (kill markers, TEX, and two unnamed classes) into more profitable basic blocks.

| | |
|---|---|
| **Binary phases covered** | 78 (`DoKillMovement`), 79 (`DoTexMovement`), and two adjacent unnamed phases sharing the same engine |
| **Wiki phases covered** | 67 (`DoKillMovement`), 68 (`DoTexMovement`); two sibling movements have no separate wiki entry |
| **Category** | Optimization (post-LICM positional fixup) |
| **Pipeline position** | Between `OriHoistInvariantsLate` (binary 88) and `OriDoRemat` (binary 93); after `SinkCodeIntoBlock` (binary 89) and around `LateExpansionUnsupportedOps` (binary 90) |
| **Wrapper functions** | `sub_C5FE00` (esi=0), `sub_C5FE30` (esi=1), `sub_C5FE60` (esi=2), `sub_C5FE90` (esi=3) — 34 bytes / 12 instructions each |
| **Shared engine** | `sub_8FFDE0` — 573 bytes, 37 BBs, 129 instructions, 9 outgoing callees |
| **Movement worker** | `sub_A112C0` — same function that `OriHoistInvariantsLate` calls; loops while `sub_A11060` returns true |
| **Gate (wrapper)** | `sub_7DDB50(ctx) > 1` — optimization level (`*(ctx+2104)`) must be `> O1` |
| **Gate (engine)** | `*(ctx+1368) & 1` master bit AND `sub_7DDB50(ctx) > 2` (so the engine itself requires `> O2`) AND named-phase token `"HoistInvariants"` not in `DisablePhases` |
| **Per-function gate** | `*(ctx+520)` (function count) must be `> 0`, and per function `sub_7A1A90(km, 381, F)` must succeed |
| **Knob 499 path** | When the knob manager's vtable slot at `+72` is `sub_6614A0`, knob 499 is read directly from `*(km+9) + 35928`; otherwise via virtual call. Throttled by `*(km+9) + 35936/35940` counter |
| **SM-tier gate** | Implicit through `*(ctx+1704) > 5` branch at `0x8ffe48` — splits the per-function check into two `sub_7A1A90` calls (early-SM tail vs Blackwell-class) |

## Why "Movement"

The engine name does not appear as a string in the binary; only `DoKillMovement` and `DoTexMovement` are present in the `.rodata` string pool. Both names belong to the family of LICM-derivative passes that move existing instructions to different basic blocks without rewriting them. The four discriminator values map onto four motion modes (reconstructed from the engine's three decision points and the call to `sub_A112C0` with `±1` direction argument):

| Discriminator (`a2`) | Wrapper | Phase | Movement direction | Targets |
|---:|---|---|---|---|
| **0** | `sub_C5FE00` | `DoKillMovement` (bin 78) | Downward — toward last use | Kill annotations: synthetic `KILL` pseudo-instructions that mark vreg end-of-life for the allocator |
| **1** | `sub_C5FE30` | sibling A (unnamed) | Upward — toward definition | Likely a kill-mode variant (cleanup helper `sub_785E20` not invoked); reuses the kill data structures (`sub_A11060` predicate) |
| **2** | `sub_C5FE60` | `DoTexMovement` (bin 79) | Downward — toward last use | Texture fetches: `TEX`/`TLD`/`TXQ` pseudo-instructions whose latency must be hidden by surrounding compute |
| **3** | `sub_C5FE90` | sibling B (unnamed) | Upward — toward latest hoist point | Likely a tex-mode variant; `a2 > 2` skips the early-return for the cleanup helper path |

Confidence: **HIGH** that all four wrappers tail-call the same engine with discriminator ∈ {0, 1, 2, 3} (directly visible in each wrapper's decompilation). **MED** on the precise semantics of discriminators 1 and 3 — they are not named in the `.rodata` string pool, and only the engine's branch structure (`if (a2 <= 1)`, `if (a2 <= 2)`, `if (!a2)`, `if (a2 == 1)`) reveals their existence. They could equally be **second-pass invocations** of the kill/tex movements (some kernels need two passes to settle when a kill moves past another kill's last-use block) rather than independent phases. **LOW** on whether the two unnamed siblings are surfaced by the phase manager at all — they may be invoked only internally from `sub_A112C0` recursion rather than appearing in the 159-phase vtable.

## Algorithm Overview

```c
// Distilled from sub_8FFDE0 (the shared engine).
// Argument a2 is the movement-kind discriminator (0, 1, 2, or 3).

void MovementEngine(CodeObject *ctx, int kind /*a2*/) {
    // ── Step 0: master gates ───────────────────────────────────
    // bit 0 of ctx+1368 is the same master flag as OriHoistInvariantsLate
    if ((ctx->target_flags & 1) == 0)
        return;
    // Engine requires opt level > O2 (wrapper already filtered > O1).
    // sub_7DDB50 returns the effective O-level after applying knob 499
    // and the per-function throttle counter at km+9+35936.
    if (opt_level(ctx) <= 2)
        return;

    // ── Step 1: named-phase disable check ──────────────────────
    // If user passed --disable-named-phase=HoistInvariants (or the
    // equivalent through the OCG namedPhases mechanism), this
    // unified token disables ALL FOUR movement variants at once,
    // since they share the same name with the late LICM pass.
    char disabled = 0;
    sub_799250(ctx->knob_mgr, "HoistInvariants", &disabled);
    if (disabled)
        return;

    // ── Step 2: function count and per-function loop ───────────
    int n_funcs = ctx->function_count;       // *(ctx+520)
    if (n_funcs == 0) return;

    int byte_offset = 4;
    int end_offset  = 4 * (n_funcs - 1) + 8;

    while (true) {
        int fn_id = ctx->fn_id_array[byte_offset / 4];     // *(ctx+512)
        Function *F = ctx->function_table[fn_id];          // *(ctx+296)

        // Empty function fast-skip.
        if (F->first_instr == NULL || F->last_instr == NULL)
            goto next_function;

        // ── Step 3: SM-tier-gated per-function predicate ───────
        // *(ctx+1704) > 5 selects the Blackwell-class fast path.
        // Both arms call sub_7A1A90(km, 381, F) — knob 381 is the
        // per-function "this function eligible for movement" flag.
        if (ctx->sm_tier > 5) {
            if (!sub_7A1A90(km, 381, F)) {
                // sub_7A1A90 returned false on Blackwell: hard fail.
                break;
            }
        } else {
            if (!sub_7A1A90(km, 381, F)) {
                // Pre-Blackwell: distinguish skip-this-function (kind<=1)
                // from skip-this-phase-entirely (kind>1).
                if (kind <= 1) goto kill_path_29;
                goto next_function;
            }
        }

        // ── Step 4: per-function 3-way knob 381 dispatch ───────
        int v17 = sub_7A1B80(km, 381, F);   // tri-state result

        if (v17 == 1) {
            // Variant 1: do the movement only for kind==1 (sibling A);
            // for everything else, skip this function.
            if (kind == 1) goto kill_path_27;
            goto next_function;
        }
        if (v17 == 3) break;                // global stop signal
        if (v17 != 0) {
            // Variant 11: pre-Blackwell fallback path.
            if (kind <= 1) goto kill_path_29;
            goto next_function;
        }

        // v17 == 0: most common path. Both 'kind==0' and the
        // continue-to-the-tex-path are encoded here.
        if (kind == 0) goto tex_path_19;
        // kind ∈ {1, 2, 3}: fall through to the per-function
        // upward-movement block, then continue to next function.

next_function:
        byte_offset += 4;
        if (byte_offset == end_offset) return;
        continue;
    }

    // ── Step 5: kill-class fixed-direction emission ────────────
    // Reached for kinds 0 and 1 from the "succeed and emit"
    // shortcut paths. Direction:
    //   kind == 0:  v22 = +1 (downward toward last use)
    //   kind == 1:  v22 = -1 (upward toward definition)
    if (kind <= 1) {
kill_path_29:
        int direction = (kind != 0) ? -1 : +1;
kill_path_27:
        if (kind != 0) direction = -1;
        // sub_A112C0 is the LICM movement worker. With direction=+1
        // it sinks kill markers; with -1 it hoists them.
        sub_A112C0(ctx, direction, a3, a4, *(double*)a5.lo, a6, ...);
        // No return here — fall through into the tex-path's
        // post-movement bookkeeping (constructive cleanup).
    }

    // ── Step 6: tex-class per-function post-movement ───────────
    // Reached unconditionally for kinds 0, 1; conditionally for 2, 3.
    // The stack record (v26..v38) is the input to sub_8FF780, which
    // computes the post-movement profile (kill-survivor count,
    // tex-survivor count, etc.) and writes flag bytes v31..v36.
tex_path_19:
    StackRecord r = { .ctx = ctx, .kind = kind, /* zeroes */ };
    sub_8FF780(&r);     // analyze post-movement IR; sets r.flags

    // Re-emit cleanup if the kind allows AND the analysis says
    // there are surviving moveables.
    if (r.flag_31) {
        if (kind <= 2) {
            // Kinds 0, 1, 2: invoke sub_785E20 for one more pass
            // through the IR (rebuilds use-def post-movement).
            sub_785E20(ctx, 0, ...);
            if (r.flag_33 || r.flag_34) goto final_emit;
        }
    } else if ((r.flag_33 || r.flag_34) && kind <= 2) {
final_emit:
        // Direction: kind==0 → +1 (down), else → -1 (up).
        // sub_A112C0 is re-invoked here for the post-cleanup
        // sweep, which fixes positions exposed by sub_785E20.
        sub_A112C0(ctx, (kind == 0) ? 1 : -1, ...);
    }
    // (kind == 3 falls off the end without a final_emit.)
}
```

The dispatch is intentionally **flat and discriminator-driven** rather than vtable-polymorphic. ptxas avoids C++ inheritance throughout the optimizer because the phase manager's `execute()` indirection is already one virtual call per pass; a second layer of polymorphism inside the movement engine would double that cost without reducing branch-predictor pressure (the discriminator value is constant for the whole engine invocation). Confidence: **HIGH** on the control-flow shape (each `if (a2 …)` is directly visible at `0x8ffe70..0x8ffeac`); **MED** on the mapping of discriminators 1 and 3 to "upward" vs "second-pass" — both interpretations fit the surviving evidence.

## The Discriminator Bit Table

The engine reads `a2` (esi) at exactly three decision points. Each decision point splits the four kinds into two groups. The full 4×3 truth table is small enough to enumerate:

| Decision point | Condition | kind=0 | kind=1 | kind=2 | kind=3 |
|---|---|:-:|:-:|:-:|:-:|
| **D1** at engine entry → `LABEL_11` | `a2 <= 1` | YES | YES | no | no |
| **D2** after `v17 == 1` → `LABEL_27` | `a2 == 1` | no | YES | no | no |
| **D3** after `v17 == 0` → `LABEL_19` | `!a2` | YES | no | no | no |
| **D4** post-analysis cleanup | `a2 <= 2` | YES | YES | YES | no |
| **D5** final direction | `a2 == 0 ? +1 : -1` | +1 | −1 | −1 | −1 |

Reading the columns:
- **kind 0** (`DoKillMovement`): hits D1 and D3, gets direction +1 in D5 → downward movement of kill markers.
- **kind 1** (sibling A): hits D1 and D2, gets direction −1 in D5 → upward movement of (likely) kill markers, possibly invoked recursively from `DoKillMovement`'s post-cleanup.
- **kind 2** (`DoTexMovement`): hits no early-return path, gets direction −1 in D5 → upward movement of texture instructions toward the hoist point chosen by the underlying LICM analysis.
- **kind 3** (sibling B): hits no path including D4, so `sub_785E20` cleanup is **skipped** → "raw" upward TEX movement without rebuilt use-def.

The structure makes biological sense: a downward pass naturally produces a *forward dataflow* edit, while the upward direction depends on the *backward dataflow* that LICM already computed. The cleanup helper `sub_785E20` is only useful when downward edits could have exposed new movement candidates — hence its restriction to `kind <= 2`.

Confidence: **HIGH** for D1–D4 (each is a single comparison directly visible in the decompilation at the addresses listed in the verification table below); **MED** for the +1/−1 direction interpretation in D5 (the second argument to `sub_A112C0` is signed, and `sub_A11060` examines it via `&v44[2]` to gate the kill-vs-tex selection inside the worker, but the "downward" vs "upward" labelling is inferred from how the resulting positional edits chain through `sub_A112C0`'s while loop rather than from any directional string).

## Shared-Engine Pattern

The "one engine, four discriminators" idiom appears throughout the ptxas optimizer wherever a family of phases differs only in a selection mask. Other instances visible in the binary include:

- **`Vectorization` / `LateVectorization`** (binary phases 47 / 85) — two wrappers, one engine, discriminator selects pre- vs post-predication vector candidates.
- **`OriCommoning` / `LateOriCommoning`** (binary phases 65 / 86) — likewise two wrappers, one engine.
- **`OriHoistInvariantsEarly` / `OriHoistInvariantsLate` / `OriHoistInvariantsLate2` / `OriHoistInvariantsLate3`** (binary phases 46 / 88 / 92 / 104) — four wrappers, **one** LICM engine (`sub_94F150` and its callees), discriminator selects loop-class scope.
- **`OriPerformLiveDeadFirst..Fourth`** — four wrappers, one liveness engine.

What makes the movement family distinguishable is that its wrappers are **adjacent in memory** (0xC5FE00, 0xC5FE30, 0xC5FE60, 0xC5FE90 — exactly 0x30 = 48 bytes apart, matching the 34-byte body + 14-byte alignment slot) and **collectively register-class indistinguishable**: each is 34 bytes long, has 12 instructions, 4 basic blocks, 1 try-block, and references only the constant `1` (plus `2` for `C5FE60`, `3` for `C5FE90`). This memory layout — a stride-48 array of 34-byte trampolines — is the same pattern used for the [Late Expansion thunks](late-legalization.md) and the per-phase Mercury dispatch helpers. Confidence: **HIGH** (the addresses, sizes, and constants-referenced are direct from `ptxas_functions.json`).

> ⚡ **QUIRK — `HoistInvariants` named-phase token disables four movements at once**
> The engine's first action after the master-bit and opt-level gates is `sub_799250(km, "HoistInvariants", &disabled)`. Passing `-Xptxas --disable-named-phase=HoistInvariants` (or its equivalent through the OCG `DisablePhases` mechanism, which is a sequence-of-strings stored at `*(km+72)+13328`) disables **all four** movement passes plus the LICM pass `OriHoistInvariantsLate` itself, because they all share the single token "HoistInvariants" rather than four distinct tokens. Setting `DisablePhases=DoTexMovement` does **not** disable TEX movement specifically — the token is looked up only via the unified name. Confirmed at `0x8ffe18` (the `sub_799250` call site with the string at `aHoistinvariants`). Confidence: HIGH.

> ⚡ **QUIRK — `DoKillMovement` requires `> O2`, not the `> O1` that its wrapper checks**
> The wrapper `sub_C5FE00` gates on `sub_7DDB50(ctx) > 1` (i.e. opt-level > O1, meaning O2 or O3), but the engine then re-tests `sub_7DDB50(ctx) <= 2` and returns immediately at O2. The wrapper's check is therefore strictly weaker than the engine's; phase manager calls the wrapper on `nvcc -O2`, the wrapper proceeds, the engine bails. The wrapper's `> 1` is best understood as a "fast reject for `-O0`/`-O1`" rather than a true gate. The same pattern repeats in the three sibling wrappers. Net effect: **all four movement variants are silently dead at `-O2`** despite the phase being listed as enabled in `--dump-named-phases`. Confidence: HIGH (both comparisons are direct in the decompilation: wrapper at `0x8ffde0`-relative `cmp eax, 1` vs engine at `0x8ffdfc`-relative `cmp eax, 2`).

> ⚡ **QUIRK — the engine's "function count" loop unsafely indexes from offset 4**
> The per-function loop initializes `v13 = 4` (byte offset into `*(ctx+512)`, which is a `int[]` of function IDs) and computes the terminator as `v14 = 4 * (n_funcs - 1) + 8`, then reads `*(int *)(*(ctx+512) + v13)`. Translation: the array is being indexed as **`fn_id_array[1], fn_id_array[2], ..., fn_id_array[n_funcs]`** — skipping element 0, and reading one element past what a naïve C `for (i = 1; i <= n; ++i)` would suggest. Cross-check with `sub_C5FD10` (the LinearReplacement wrapper, identical 1-based-with-overshoot loop) confirms this is a deliberate convention: element 0 of `*(ctx+512)` is the **count** itself (in older builds) and element `n_funcs+1` is a guard sentinel. Touching element `n_funcs` (the last real entry) reads the sentinel, not the array — but the loop's terminator `v14 == v13` prevents that read from completing. The off-by-one is structural, not a bug. Confidence: MED (the reading pattern is consistent across all four wrappers and the LinearReplacement family, but no comment or string confirms the "element 0 = count" interpretation).

## Function Map

| Address | Size | Role | Source-of-truth |
|---|---:|---|---|
| `sub_C5FE00` | 34 B (4 BBs) | Wrapper for `DoKillMovement` (binary 78) — gates on `sub_7DDB50 > 1`, tail-calls engine with `esi=0` | `ptxas_functions.json` |
| `sub_C5FE30` | 34 B (4 BBs) | Wrapper for sibling A (unnamed; likely 2nd-pass kill) — same gate, `esi=1` | `ptxas_functions.json` |
| `sub_C5FE60` | 34 B (4 BBs) | Wrapper for `DoTexMovement` (binary 79) — same gate, `esi=2` | `ptxas_functions.json` |
| `sub_C5FE90` | 34 B (4 BBs) | Wrapper for sibling B (unnamed; likely TEX no-cleanup pass) — same gate, `esi=3` | `ptxas_functions.json` |
| `sub_8FFDE0` | 573 B (37 BBs, 129 insns) | **The shared movement engine.** Three discriminator-driven decision points, two emission paths (`sub_A112C0`) | This page |
| `sub_7DDB50` | 87 B (4 BBs) | Effective opt-level lookup — virtual call through `km->vtable[152/8]`, throttled by knob 499 counter at `km+9+35936/35940` | Called by every wrapper and once inside engine |
| `sub_799250` | 76 B (3 BBs, leaf) | Named-phase token check — looks up string `a2` in the `DisablePhases` table at `km->vtable[72/8]+13328`; writes boolean into `*a3` | Engine entry |
| `sub_7A1A90` | (small) | Per-function knob-381 boolean check — "this function is eligible for movement" | Called twice per function (SM-tier gated) |
| `sub_7A1B80` | (small) | Per-function knob-381 **tri-state** check — returns 0, 1, 3, or other to select the within-function variant | Called once per function |
| `sub_A112C0` | ~2.3 KB | **Movement worker.** Same function called by `OriHoistInvariantsLate`. Loops while `sub_A11060` returns true; direction passed in second argument | Called from kill-path AND from final_emit |
| `sub_8FF780` | ~1 KB | Post-movement profile builder — populates the `v25..v38` stack record with byte flags `v31..v36` that gate the cleanup phase | Called once after the main loop |
| `sub_785E20` | (small) | Use-def rebuild — same helper called after every IR-mutating pass; rewires source operands for instructions whose def has moved | Cleanup path for `kind <= 2` |
| `sub_A11060` | (small) | Per-instruction movement predicate — "is this instruction a movement candidate of the current kind"; reads `v44[2]` for the kind discriminator | Inner loop of `sub_A112C0` |
| `sub_A0C310` | (small) | Per-function movement-state initializer — sets up the `v41..v50` stack record for `sub_A112C0`'s loop | Called from `sub_A112C0` prologue |

Confidence: **HIGH** for the wrapper roles (each is fully decompiled and the only differing register is `esi`); **HIGH** for the engine's control flow (37 BBs, 152 BB blocks in the inner switch table, all visible); **MED** for the worker roles (`sub_A112C0` is large and shared with LICM; its movement-direction interpretation depends on the discriminator argument the engine passes).

## Pipeline Context

```
Phase 88  OriHoistInvariantsLate           ┐  builds the LICM dataflow that the
                                            │  movement engine reuses
Phase 89  SinkCodeIntoBlock                │  ⊖ (skipped phase; code-sinking that
                                            │     unaffected by kill/tex)
Phase 90  ★ DoKillMovement   (esi=0) ──────┤
Phase ?   sibling A          (esi=1) ──────┤  ── four wrappers, one engine
Phase 92  ★ DoTexMovement    (esi=2) ──────┤     (sub_8FFDE0)
Phase ?   sibling B          (esi=3) ──────┘
Phase 93  OriDoRemat                         consumes the moved positions when
                                             selecting remat candidates
```

The movement family runs after `OriHoistInvariantsLate` (binary 88) so the LICM dataflow is fresh, and before `OriDoRemat` (binary 93) so rematerialization sees the moved kill markers and chooses remat candidates that respect the new register-pressure profile. The engine deliberately does not invalidate the LICM dataflow; instead it relies on `sub_785E20` (the post-cleanup helper) to repair the parts of use-def that are affected by movement.

> ⚡ **QUIRK — sibling phases inherit `DoKillMovement`'s phase manager entry**
> The phase manager (wiki [phase-manager.md](phase-manager.md)) lists only `DoKillMovement` (wiki 67) and `DoTexMovement` (wiki 68), with no separate entries for siblings A and B. This is because the binary phase vtable at `0x21DBEF8`-family stores only two distinct names for the four wrappers — the unnamed wrappers reuse the previous name's slot, and `--dump-named-phases` reports them as identical to their named predecessor. Toggling `DUMPIR=DoKillMovement` actually dumps IR around **both** kill-class wrappers (esi=0 and esi=1), and `DUMPIR=DoTexMovement` dumps around **both** tex-class wrappers (esi=2 and esi=3). Confidence: MED (consistent with the absence of additional strings in `ptxas_strings.json`, but the DUMPIR behaviour is inferred from the named-phase token check rather than directly observed).

## Storage Layout

The engine's stack frame (122 bytes for the engine itself; 8 bytes for each wrapper) is dominated by the `v26..v38` cluster fed to `sub_8FF780`:

```
v25  (offset +1):  disabled flag — written by sub_799250(HoistInvariants)
v26  (offset +2):  (_QWORD*)ctx — engine forwards the context pointer
v27  (offset +A):  kind — the discriminator value verbatim
v28  (offset +12): __int128 zero — reserved profile slot
v29  (offset +22): __int64 zero — reserved profile slot
v30..v36 (+2A..+30): seven byte flags written by sub_8FF780:
                     v31 = "any survivors after primary movement"
                     v33 = "kill survivors specifically"
                     v34 = "tex survivors specifically"
                     v30, v32, v35, v36: unused at this entry point
v37  (offset +32): __int64 zero — secondary profile slot
v38  (offset +3A): __int128 zero — secondary profile slot
```

The flag bytes `v31`, `v33`, `v34` form a 3-bit decision vector that drives the cleanup path: if any survivor exists, run `sub_785E20`; if kill or tex survivors specifically remain, run `sub_A112C0` again with the appropriate direction. The arrangement of zeros around the active flag bytes is the standard ptxas "post-pass profile record" layout (visible across `sub_8FF780`'s 6 callers). Confidence: MED — the flag-name labels here are reconstructed from the post-`sub_8FF780` branch tests (`if (v31)`, `if (v33 || v34)`), not from any string evidence.

## Worked Example

Consider a kernel with a TEX instruction whose result is consumed three basic blocks later:

```
BB0:                                BB0:
  ...                                 ...
BB1:                                BB1:
  R10 = TEX.LD.B [s0, R0]             ...   ← TEX no longer here
  ...                               BB2:
BB2:                                  ...
  R11 = IADD R5, R3                 BB3:
BB3:                                  R10 = TEX.LD.B [s0, R0]   ← moved down
  R12 = IMAD R10, R11, R6             R12 = IMAD R10, R11, R6   ← uses R10
```

`DoTexMovement` (kind=2) drives `sub_A112C0` with `direction=-1` (i.e., "move toward last use"). The worker:

1. Iterates over instructions in BB1. The first one selected by `sub_A11060` is the TEX with predicate "is TEX-class and has uses dominated by `BB3`".
2. The LICM dataflow says: `R10`'s only use is in `BB3:IMAD`; `BB3` post-dominates `BB1`; therefore TEX can be safely sunk into `BB3`.
3. The worker calls `sub_A0C310` to set up movement state, sinks the TEX into `BB3` immediately before the consuming `IMAD`, and continues the loop.

After the main loop, `sub_8FF780` profiles the resulting IR and sets:
- `v31 = 1` (at least one TEX was moved)
- `v33 = 0` (no kill survivors — this was a TEX pass)
- `v34 = 1` (one TEX survivor exists post-movement)

The cleanup branch is taken: `sub_785E20` rebuilds use-def around `BB3`, then `sub_A112C0` is called again with direction `-1` to handle any newly-exposed TEX movement candidates (typically none, but the second pass is required for the survivor counts to reach a fixed point).

Net: one TEX instruction relocated three basic blocks downward, hiding ~80 cycles of texture latency behind the unrelated IADD computation in BB2.

> ⚡ **QUIRK — kill markers don't move; their *anchors* do**
> `DoKillMovement` (kind=0) operates on `KILL` pseudo-instructions, which are zero-cost SASS-invisible markers used by the register allocator to indicate "vreg's last use is here". Moving a kill marker downward extends the apparent live range of its target vreg, **increasing** register pressure locally — the opposite of what the name suggests. The actual purpose is to **align kill positions with SASS basic-block boundaries** so the allocator's spill heuristic sees blocks where multiple vregs die simultaneously (which is cheaper to spill than scattered single-vreg deaths). The downward direction is therefore not an optimization in the usual sense; it is a **regularization pass** for the allocator's heuristic. Confidence: MED — the regularization interpretation matches `OriPerformLiveDeadFirst..Fourth`'s usage of the same `KILL` markers, but no string in the binary explicitly states this rationale.

## Verification Anchors

| Claim | Anchor in raw data |
|---|---|
| Four wrappers at 0xC5FE00, 0xC5FE30, 0xC5FE60, 0xC5FE90 with 0x30 stride | `ptxas_functions.json` entries at lines 3286407, 3286443, 3286482, 3286521 |
| Each wrapper is 34 bytes / 12 instructions / 4 BBs | `ptxas_functions.json` `size`/`insn_count`/`block_count` fields |
| Each wrapper tail-calls `sub_8FFDE0` with a distinct `esi` ∈ {0, 1, 2, 3} | Direct from decompilation in `decompiled/sub_C5FE00..C5FE90_0xc5fe*.c` |
| Wrappers gate on `sub_7DDB50(a2) > 1` | `if ( (int)sub_7DDB50(a2) > 1 )` in each wrapper |
| Engine size 573 B, 37 BBs, 129 instructions | `ptxas_functions.json` line 1696180 |
| Engine has exactly 4 callers (the four wrappers) | `ptxas_functions.json` callers array at line 1696186 |
| Engine master gate `*(ctx+1368) & 1` | `if ( (*(_BYTE *)(a1 + 1368) & 1) == 0 ) return` at engine entry |
| Engine opt-level gate `sub_7DDB50(a1) > 2` | `if ( (int)sub_7DDB50(a1) <= 2 ) return` |
| Engine `HoistInvariants` token check | `sub_799250(*(_QWORD *)(a1 + 1664), "HoistInvariants", &v25)` |
| Engine per-function loop initializer `v13 = 4`, terminator `4*(n-1)+8` | `v13 = 4; v14 = 4LL * (unsigned int)(v12 - 1) + 8` at line 44-45 of decompilation |
| Knob 381 used for per-function eligibility | `sub_7A1A90(v21, 381, ...)` and `sub_7A1B80(..., 381, ...)` |
| SM-tier branch at `*(ctx+1704) > 5` | `if ( *(int *)(a1 + 1704) > 5 )` at line 54 |
| Final-emit direction `(kind == 0) ? +1 : -1` | `a2 == 0 ? 1 : -1` at line 121 of decompilation |
| `sub_785E20` cleanup only for `kind <= 2` | `if ( a2 <= 2 )` guards at lines 111, 118 |

## Cross-References

- [Pass Inventory & Ordering](index.md) — binary phases 78 (`DoKillMovement`) and 79 (`DoTexMovement`) entries; siblings A and B at adjacent unnamed positions
- [Phase Manager Infrastructure](phase-manager.md) — the vtable-based dispatch that calls all four wrappers and the named-phase token mechanism that gates them
- [Loop Passes](loop-passes.md) — `OriHoistInvariantsLate` (binary 88, wiki 66) shares the `"HoistInvariants"` token and the underlying `sub_A112C0` worker
- [Rematerialization](rematerialization.md) — `OriDoRemat` (binary 93) consumes the moved kill positions when selecting remat candidates; movement runs immediately before remat
- [Liveness Analysis](liveness.md) — `OriPerformLiveDead*` passes use the same `KILL` pseudo-instructions that `DoKillMovement` repositions
- [Late Expansion & Legalization](late-legalization.md) — runs at binary phase 90, immediately after the movement family; consumes a stabilized IR
- [Knobs System](config/knobs.md) — knobs 381 (per-function movement eligibility) and 499 (throttled opt-level lookup)
- [DUMPIR & NamedPhases](config/dumpir.md) — the `DisablePhases` and `--dump-named-phases` mechanisms that interact with the unified `"HoistInvariants"` token
