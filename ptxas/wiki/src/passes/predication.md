# Predication (If-Conversion)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`OriDoPredication` (phase 63) is the if-conversion pass in ptxas. It transforms short conditional branch regions into predicated straight-line code, eliminating branches by guarding individual instructions with predicate registers. On NVIDIA GPUs, where all threads in a warp execute in lockstep, eliminating divergent branches avoids the performance penalty of serialized path execution under the SIMT model.

| | |
|---|---|
| **Phase index** | 63 |
| **Phase name** | `OriDoPredication` |
| **Category** | Optimization |
| **Entry point** | `sub_1381DA0` (1,517 bytes) |
| **Core driver** | `sub_1381CD0` (206 bytes) |
| **Main loop** | `sub_1381010` (3,249 bytes) |
| **Total code** | ~17 KB across 19 functions in `0x137D8B0`--`0x13829F0` |
| **SSA window** | Yes — runs at phase 63, within the partial-SSA window (phases 23–73) |
| **Pipeline position** | After `OriRemoveRedundantMultiDefMov` (62), before `LateOriCommoning` (64) |
| **Gating** | Disabled when bit 5 of `context+1376` flags is set; can be disabled via `PTXAS_DISABLED_PASSES` containing `"Predication"` |
| **Knob controls** | Knob 487 (enable/limit gate), knob 577 (per-region enable), knob 579 (texture-bearing region gate), knob 582 (block-level cold-region query), knob 260 (extra-latency penalty check) |

## GPU Motivation

The SIMT execution model makes predication qualitatively different from its role on scalar CPUs.

On a scalar CPU, a correctly-predicted branch is essentially free — the branch predictor eliminates the control flow cost. If-conversion on CPUs is a niche optimization applied only when branches are highly unpredictable.

On a GPU, a divergent conditional branch forces the warp to serialize: the hardware executes the taken path with some threads masked off, then executes the not-taken path with the complementary mask. Both paths execute regardless, and the warp reconverges at the post-dominator. The cost is the sum of both paths, not the maximum.

Predication eliminates this divergence penalty entirely. Both paths still execute, but without the overhead of stack-based reconvergence (`BSSY`/`BSYNC` pairs on sm_70+), without the branch instruction itself, and with the ability for the scheduler to interleave the predicated instructions with other independent work. For short regions (a few instructions per side), predication is strictly superior to branching.

```text
Branching (divergent):               Predicated:

  ISETP.NE P0, R4, R5               ISETP.NE P0, R4, R5
  BSSY B0, target                    @P0  IADD3 R6, R6, 1, RZ
  @P0 BRA taken_path                 @!P0 IADD3 R7, R7, 1, RZ
  // not-taken:                      // continues straight-line
  IADD3 R7, R7, 1, RZ
  BRA rejoin
  // taken:
  IADD3 R6, R6, 1, RZ
  // rejoin:
  BSYNC B0
```

The branching version requires 6 instructions (including `BSSY`/`BSYNC` convergence bookkeeping) and forces warp serialization. The predicated version requires 3 instructions and executes without divergence.

## Algorithm Overview

The pass operates in three layers:

1. **Entry and gating** (`sub_1381DA0`): checks the `"Predication"` disable flag and knob 487, initializes working state, calls the driver.
2. **Iterative driver** (`sub_1381CD0`): initializes via the SM backend's vtable dispatch at `sm_backend+1296`, then calls the main loop up to 3 times (controlled by a knob at options offset 41768) with different aggressiveness settings.
3. **Main RPO loop** (`sub_1381010`): walks the RPO block order, identifies candidate branch regions, evaluates profitability, and applies the transformation.

### Entry Point — `sub_1381DA0`

```c
sub_1381DA0(compilation_unit):
    if context+1376 bit 5 set:
        return                       // phase disabled by flag

    knob_state = *(context+1664)     // OCG knob container
    mode = *(*(knob_state+72) + 16416)

    if mode == 0:
        limit = (context+1419 bit 4) != 0
    elif mode == 1:
        limit = *(*(knob_state+72) + 16424)
    else:
        // mode >= 2: skip limit check

    IsPassDisabled(knob_state, "Predication", &disabled)
    if disabled or limit:
        return

    // Check knob 487 iteration limit
    CheckKnob487(knob_state)

    // Set up working state (allocate two pool objects)
    context+1385 |= 1               // mark predication active
    sub_1381CD0(state)               // call driver
    context+1385 &= ~1              // clear predication flag

    // Cleanup: release pool objects and tree structures
```

The `context+1385` byte has bit 0 set during predication execution, which signals downstream code (such as `sub_137EE50`) that the pass is active.

### Iterative Driver — `sub_1381CD0`

```c
sub_1381CD0(state):
    // Initialize via SM backend
    sm_backend = *(context+1584)
    init_fn = vtable(sm_backend)+1296
    if init_fn == sub_7D82C0:       // fast path: zero-init
        clear state fields
    else:
        init_fn(sm_backend, state)   // backend-specific init

    bb_count = *(context+520)
    if bb_count <= 1: return 0       // nothing to if-convert

    // Read iteration knob: options+41760 is the enable byte,
    // options+41768 is the max-extra-passes value
    max_extra = 0;  gate = 0
    opts = *(*(context+1664) + 72)
    if *(opts+41760) == 1:
        max_extra = *(opts+41768)          // e.g. 1, 2, or 3
        gate = (max_extra != 0) ? 1 : 0

    // --- Pass 1 (always runs) ---
    state[14].byte[8] = 0                  // last_iteration = false
    changed = gate & sub_1381010(state)

    // --- Extra passes (re-scan after prior CFG mutations) ---
    // max_extra is read once and never decremented.
    do:
        if !changed: break
        state[14].byte[8] = (max_extra == 1)
        changed = gate & sub_1381010(state)
    while max_extra > 2
```

**Iteration schedule by knob value.** `max_extra` (read from `options+41768`) is a constant for the entire run. The loop exit depends on the knob value and on convergence (pass returning 0 when no further profitable if-conversions exist).

| `max_extra` | Total passes | `last_iteration` flag | Exit condition |
|:-----------:|:------------:|:---------------------:|:---------------|
| 0           | 1            | always 0              | `gate` = 0 masks first-pass result to 0, loop never entered |
| 1           | up to 2      | 0 on pass 1, **1** on pass 2 | `max_extra <= 2` exits after one extra pass |
| 2           | up to 2      | 0 on both passes      | `max_extra <= 2` exits after one extra pass |
| >= 3        | up to N      | 0 on all passes       | `max_extra > 2` keeps looping until convergence |

**Effect of `last_iteration` (state offset +232).** This flag is true only when `max_extra == 1`. In `sub_1380BF0` (the profitability gate), the flag adds a narrowing filter before the normal cost check:

```c
// sub_1380BF0, line 66
if last_iteration:
    if !(candidate_bb+282 bit 3) or !state.byte[76]:
        return 0   // reject candidate
```

The two fields tested by this filter:

- Bit 3 of `candidate_bb+282` is a "retry-eligible" marker set during earlier passes on blocks that were close to the profitability threshold but did not cross it.
- `state.byte[76]` is a secondary relaxation gate populated by the SM backend initializer.

When both conditions hold, the candidate proceeds to normal evaluation; otherwise it is skipped. This narrows the second pass to only the most promising candidates that nearly qualified on pass 1, avoiding wasted compile time on clearly unprofitable blocks.

### Main Loop — `sub_1381010`

The main loop walks basic blocks in RPO order (via the block index array at `context+512`), identifies candidate branch regions, and decides whether to if-convert each one.

```c
sub_1381010(state):
    // Rebuild liveness and CFG
    sub_781F80(context, 1)          // rebuild liveness
    if context+1370 bit 4 set:
        sub_A10160(context, 1)      // rebuild analysis
    sub_7E6090(context, 0,0,0,0)    // refresh CFG
    // Clear block-76 fields
    for each block in chain:
        block+76 = 0
    sub_791F00(context, 0)          // clear RPO numbering

    changed = false
    for rpo_idx = 2 .. bb_count:
        bb = bb_array[rpo_order[rpo_idx]]

        if bb is same as previous region tail:
            // Continuation of prior diamond -- reuse state
            restore saved state
        else:
            // Fresh candidate: analyze new region
            init candidate state
            if not isTriangleDiamondCandidate(bb):
                skip
            if not analyzeRegion(state, candidate):
                skip

        // Region identified -- extract branch info
        header = bb
        true_target = successor of header's terminator
        branch_pred = extractBranchPredicate(header)
        false_target = fallthrough

        // Try to if-convert both sides
        if evaluateProfitability(true_side, false_side):
            applyTransformation(...)
            changed = true

    if changed:
        context+1370 &= ~4          // invalidate CFG
        sub_785E20(context, 0)       // rebuild
    return changed
```

## CFG Pattern Recognition

The pass recognizes three CFG shapes for if-conversion:

### Triangle Pattern

One arm of the branch is empty (falls through directly to the merge point).

```text
         [header]
        /    \
       /      \
   [then]      |
       \      /
        \    /
       [merge]
```

Requirements:
- `header` ends with a conditional branch (opcode 93; `OUT_FINAL` in the ROT13 name table, but checked here as a control-flow terminator marker)
- `then` block has a single predecessor (the header)
- `then` block's sole successor is the `merge` block
- `merge` has exactly two predecessors: `header` and `then`
- No backedges into the region

### Diamond Pattern

Both arms contain instructions.

```text
         [header]
        /    \
       /      \
   [then]  [else]
       \      /
        \    /
       [merge]
```

Requirements (same as triangle, plus):
- The `else` block has a single predecessor (the header)
- The `else` block's sole successor is the same `merge` block
- `merge` has exactly two (or three, for extended diamonds) predecessors

### Extended Diamond Pattern

The pass can also handle diamonds where one or both arms chain through a successor block before merging. The `sub_137FE10` function implements this extended analysis, walking forward through fall-through blocks until it reaches a merge point or encounters a block that fails the candidate check.

```text
         [header]
        /    \
       /      \
   [then]  [else]
      |       |
   [then2] [else2]   (optional chain blocks)
       \      /
        \    /
       [merge]
```

## Region Analysis — `sub_137E3A0`

This function (`sub_137E3A0`, 367 bytes) validates that a basic block is part of a valid if-conversion candidate. It checks:

1. **Predecessor count**: The merge block must have exactly `header_predecessor_count + 1` predecessors.
2. **Terminator type**: The header's terminator must match opcode 95 after masking bits 12-13 (`STS` in the ROT13 name table; used here as a control-flow terminator class marker, not an actual store-shared instruction).
3. **Branch predicate**: The branch guard must be a non-negated register operand. Three independent fields are consulted (parallel to the `.UR` multi-field surface described in [Registers](../ir/registers.md#operand-field-encoding)):
   - (a) operand word0 type tag `(word0 >> 28) & 7 == 1` (register-class operand);
   - (b) operand word1 bit 24 (`& 0x1000000`) must be **clear** (negation flag) — this is the binary-IR counterpart to the textual `!` prefix that `sub_70B780` reads from the operand-descriptor name string at `+2120`;
   - (c) the vreg's `+64` reg_type must match one of the state's two stored predicate file types at `a1[5]` (primary) or `a1[6]` (secondary).

   > **Correction.** The wiki had previously claimed these were "2 or 3, R or UR" — the state slots actually hold the P / UP file-type values, not R/UR, and the constants are loaded from the SM backend's vtable initializer.
4. **No backedges**: The predecessor list must not contain a self-edge.
5. **Merge block successor check**: Validates that the merge block's sole successor leads to the expected continuation block.

```c
// Pseudocode for sub_137E3A0
bool isTriangleDiamondCandidate(state, bb):
    pred_count = bb->predecessor_count    // at bb+144
    if pred_count == 0: return false
    preds = bb->predecessor_list          // at bb+128
    if preds == NULL: return false
    if preds->next != NULL: return false  // must be single-entry

    header = bb_array[preds->block_index]
    if header->predecessor_count + 1 != pred_count:
        return false

    terminator = header->first_instr
    opcode = terminator->opcode & 0xFFFFCFFF   // mask bits 12-13
    if opcode != 95: return false               // opcode 95 = STS in ROT13 table; used as control-flow terminator class

    // Extract branch predicate from last operand
    last_op_idx = terminator->num_operands - ((opcode >> 11) & 2) - 2
    pred_operand = terminator->operands[last_op_idx]
    if operand_type(pred_operand) != 1: return false   // must be register
    if pred_operand is negated: return false

    reg_file = get_register_file(pred_operand)
    if reg_file != state->expected_file: return false

    // Check successor list for backedges
    for each successor of header:
        if successor == bb: continue
        if other_successor exists: return false  // at most one other
    return true
```

## Region Scanning — `sub_137D990`

This function (1,270 bytes) walks all instructions in a candidate block, counting them and checking each for predicability. It builds a cost model:

### Per-Instruction Checks

For each instruction in the candidate block:

1. **Already-predicated check** (opcode bit 12 = `0x1000`): Instructions that already carry a predicate guard are flagged via `state+48` for special handling.

2. **MOV counting** (opcode 130): Instructions with opcode 130 (`HSET2` in the ROT13 name table; the code treats this value as an internal marker for MOV-like operations) that match specific operand patterns increment a separate MOV counter at `state+4`, used to adjust profitability thresholds. The actual SASS MOV instruction is opcode 19.

3. **Predicable instruction check** (`sub_137D8B0`): Each instruction is tested via the SM backend's `canPredicate` vtable method at `sm_backend+1424`. Instructions that cannot be predicated (atomics, certain memory operations, barriers) cause the scan to fail.

4. **Primary memory load classification**: For load instructions (opcode 125 after masking), the memory space is queried via `sub_91C840`. The internal category number is tested against bitmask `0x90E` (`(1 << category) & 0x90E`), which selects the five primary data memory spaces: `.shared` (1), `.local` (2), `.const` (3), `.tex` (8), `.global` (11). When a load targets one of these spaces, the `has_primary_memory_load` flag is set at `candidate+12`, which affects profitability thresholds in the heuristic. See the [Memory Space Classification for Predication](#memory-space-classification-for-predication) section for the full bitmask decode.

5. **Extra-latency check**: Long-latency opcodes contribute an additive penalty to `candidate+16`. Membership is tested with a two-tier check:

    **Bitmask tier** — the masked opcode `op` (after clearing bits 12-13) is tested via `(0x2080000010000001 >> (op - 22)) & 1` when `op - 22 < 62`. The four set bits select these Ori opcodes:

    | Bit position | Ori opcode | SASS mnemonic | Operation class |
    |---|---|---|---|
    | 0 | 22 | `R2P` | Register-to-predicate (cross-file move) |
    | 28 | 50 | `FRND_X` | FP rounding variant (extended-latency FP) |
    | 55 | 77 | `EXIT` | Thread exit / program termination |
    | 61 | 83 | `TEX` | Texture fetch |

    **Explicit tier** — two Mercury extended opcodes are checked by direct comparison: opcode 352 (`MERCURY_addmin_srcs_r_r_0`) and opcode 297 (`MERCURY_barrier_cta_red_popc_srcs_uimm_uimm_0`).

    For every matching instruction, the SM backend's `getExtraLatency` virtual method at vtable offset +1392 is called:

    - The base-class implementation (`sub_7D72B0`) returns 0, so targets that do not override this slot contribute nothing.
    - When the backend does override the slot, the method receives `(sm_backend_obj, instruction)` and returns an `int` latency penalty in cycles.
    - The return value is **summed** into the accumulator at `candidate+16` (`extra_latency += getExtraLatency(instr)`), so the total is the linear sum over all long-latency instructions in the region.
    - The profitability heuristic at step 6 of `sub_1380BF0` then uses this accumulated value: if knob 260 is active and **both** the true-side and false-side candidates have `extra_latency > 0`, if-conversion is rejected outright.

6. **Predicate-register conflict**: If any destination operand writes to the same predicate register that the branch uses as its guard, the region cannot be if-converted (the predicate would be clobbered before all instructions are guarded).

7. **Instruction count limit**: The non-MOV instruction count at `state+8` is compared against a threshold from the state object. If exceeded and the block is not marked as "must-predicate" (`state+20`), the scan returns failure.

```c
// Pseudocode for sub_137D990
bool analyzeRegion(state, candidate):
    bb = candidate->basic_block
    if bb->flags & 2: return false         // block excluded

    first_instr = bb->first_instruction
    // Check if first instruction is speculative-safe
    if isSpeculativelyUnsafe(first_instr, context):
        candidate->has_unsafe = first_instr

    // Extract branch predicate register index
    header = bb_array[bb->predecessor->block_index]
    terminator = header->first_instruction
    branch_pred_idx = extractPredicateIndex(terminator)

    // Walk all instructions in the block
    for instr = first_instr; instr != bb->tail; instr = instr->next:
        // Track already-predicated flag
        candidate->has_predicated |= (instr->opcode & 0x1000) != 0

        // Count MOVs
        if isMOV(instr) and matchesMOVPattern(instr):
            candidate->mov_count++

        // Check speculation safety for uniform operands
        if state->has_uniform_speculation:
            check uniform register SSA chain

        // Check predicability via backend
        if not canPredicateInstruction(state, instr, header):
            fail with "too many instructions"

        // Primary memory load classification (0x90E bitmask)
        if isLoadOp(instr):
            space = getMemorySpace(instr)
            if space is in {shared, local, const, tex, global}:
                candidate->has_primary_memory_load = true

        // Extra latency accounting (bitmask + explicit comparison)
        op = instr->opcode & ~0x3000
        bitmask_hit = (op - 22) < 62 and ((0x2080000010000001 >> (op - 22)) & 1)
        if bitmask_hit or op == 352 or op == 297:
            fn = sm_backend->vtable[+1392]
            latency = (fn != sub_7D72B0) ? fn(sm_backend, instr) : 0
            candidate->extra_latency += latency

        // Count non-trivial instructions
        if not isMOVPHI(instr):         // opcode 263 = MOV.PHI
            candidate->instr_count++
            if not candidate->must_predicate:
                if candidate->instr_count > state->threshold:
                    return false

        // Check for predicate-register clobber
        for each destination operand:
            if dest is register and dest index == branch_pred_idx:
                return false

    candidate->complete = true
    return true
```

## Profitability Heuristic — `sub_1380BF0`

The profitability decision (`sub_1380BF0`, 1,055 bytes) is the most complex part of the pass. It considers multiple factors to decide whether converting a branch region to predicated code is profitable.

### Decision Flow

```c
sub_1380BF0(state, true_side, false_side, is_reverse, result):
    result = false

    // 1. Texture-bearing region check
    if true_side->has_predicated:
        if not CheckKnob579(knob_state):
            return false

    // 2. Must-predicate override
    if true_side->must_predicate:
        return true

    // 3. CONV.ALLOC check
    if state->has_conv_alloc:
        if not (bb->flags & 8) or not state->flag_byte76:
            return false

    // 4. Branch-predicate matching
    //    Check if the branch condition matches a known pattern
    //    (SEL instruction producing the predicate)
    header_terminator = state->header->first_instruction
    pred_operand = extractLastPredicate(header_terminator)
    if predicateMatchesSELPattern(pred_operand):
        return true

    // 5. False-side memory load check
    if false_side->has_primary_memory_load:
        return sub_137F800(...)        // speculation safety analysis

    // 6. Extra-latency penalty
    if CheckKnob260(knob_state):
        if true_side->extra_latency > 0 and false_side->extra_latency > 0:
            return false               // both sides have long-latency ops

    // 7. Size-based thresholds (main heuristic)
    instr_count = true_side->instr_count

    if true_side->has_primary_memory_load:
        // Memory loads route to extended diamond analysis
        return sub_137FE10(...)        // extended diamond analysis

    mov_count = true_side->mov_count
    if mov_count <= state->mov_threshold:
        if state->flag_byte76:
            // Uniform-speculation-aware thresholds
            if true_side->has_predicated:
                return state->uniform_tex_limit >= instr_count
            else:
                return state->uniform_limit >= instr_count
        else:
            if true_side->has_predicated:
                return state->tex_limit >= instr_count
            else:
                return state->base_limit >= instr_count
                       and (true_extra <= 2 or false_extra <= 2)

    // 8. Fallback: combined size check
    combined = true_side->instr_count + false_side->instr_count
    if state->combined_limit < instr_count and combined > state->threshold:
        return false

    // 9. False-side memory loads boost profitability
    if false_side->has_primary_memory_load:
        return true                    // scheduling overlap benefit
    return sub_1380810(...)            // fall-through block analysis
```

### Formal Cost Model

The profitability decision is a static estimate of whether eliminating a branch saves more cycles than the predicated instructions waste. The underlying comparison is:

```text
Cost_branch  = C_bssy + C_branch + C_reconverge + C_serialize
Cost_pred(N) = N * C_issue
```

where `N` is the non-MOV instruction count of the true side and the `C_*` terms are architecture-dependent constants. The heuristic never computes these costs explicitly — instead it reduces the comparison to `N <= L` where `L` is a backend-provided limit that encodes the breakeven point for each instruction category.

The decision tree selects `L` from a 2x2 matrix indexed by two boolean flags:

| `flag_byte76` (uniform speculation) | `has_predicated` (texture ops) | Limit field | `int32` index |
|---|---|---|---|
| 0 | 0 | `base_limit` | `[8]` |
| 0 | 1 | `tex_limit` | `[9]` |
| 1 | 0 | `uniform_limit` | `[10]` |
| 1 | 1 | `uniform_tex_limit` | `[11]` |

Texture operations widen the profitability window because their high latency makes the branch serialization penalty proportionally larger. Uniform speculation (`flag_byte76`, set when the context supports speculative uniform loads) relaxes limits further because predicated uniform loads carry no divergence cost.

For the base case (`base_limit`, neither flag set), an additional constraint applies: at least one side must have `extra_latency <= 2`. This prevents predication of regions where both sides contain long-latency operations, since the predicated version would serialize both latency chains without the scheduling freedom a branch provides.

Entry to the 2x2 matrix requires `mov_count <= mov_threshold` (field `[17]`). When the true side has too many MOVs, the standard limits do not apply and the function falls through to the combined-size check and fall-through analysis.

When the true side has memory loads (`has_primary_memory_load`), the 2x2 matrix is bypassed entirely. Instead, entry to the extended diamond analysis (`sub_137FE10`) is gated by field `[12]`: `instr_count <= state[12]`. The same field `[12]` also serves as the combined-size ceiling in the fallback path (`true_count + false_count > state[12]`), giving it dual purpose as the hard cap on total region size.

### Threshold Initialization — vtable offset 1296

The SM backend populates the threshold fields via `vtable(sm_backend)[1296]`. The dispatch in `sub_1381CD0` (lines 15-29) has a fast-path check:

```c
sm_backend = *(context + 1584)
init_fn    = vtable(sm_backend)[1296]
if init_fn == sub_7D82C0:          // default backend
    memset(state+0, 0, 60)         // fields [0]..[14] = 0, disables predication
else:
    init_fn(sm_backend, state)     // backend populates fields [8]..[17]
```

The default backend (`sub_7D82C0`) zero-initializes all threshold fields. Since every size check is `N <= 0`, this effectively disables predication for architectures that do not register a custom initializer. Non-default backends write architecture-specific values based on the SM target and optimization level.

### Threshold Fields

| `int32` index | Byte offset | Field | Role |
|---|---|---|---|
| `[8]` | 32 | `base_limit` | Max instructions: no texture, no uniform speculation |
| `[9]` | 36 | `tex_limit` | Max instructions: texture present, no uniform speculation |
| `[10]` | 40 | `uniform_limit` | Max instructions: uniform speculation enabled, no texture |
| `[11]` | 44 | `uniform_tex_limit` | Max instructions: uniform speculation + texture |
| `[12]` | 48 | `threshold` | Hard ceiling for extended-diamond gate AND combined-size fallback |
| `[13]` | 52 | `combined_limit` | Per-side ceiling that triggers the combined-size rejection |
| `[14]` | 56 | `fallthrough_limit` | Threshold for `sub_1380810` fall-through analysis |
| `[15]` | 60 | `extended_limit` | Threshold within `sub_137FE10` extended diamond analysis |
| `[16]` | 64 | `mov_threshold` | MOV count below which the 2x2 limit matrix applies |
| `[17]` | 68 | `mov_limit` | MOV-specific threshold in extended analysis |

## Instruction Predication — `sub_9324E0`

Once a region passes the profitability check, each instruction in the region is predicated. The predication is performed by `sub_9324E0` (280 bytes), which transforms each instruction by adding a predicate guard operand.

### Transformation Rules

For a non-branch instruction with opcode `op`:

1. **Copy the operand array**, appending the guard predicate as the new last operand and the predicate register as the penultimate operand.
2. **Set bit 12** of the opcode (`op | 0x1000`) to mark the instruction as predicated.
3. **Special case** for opcode 188: remapped to 190.
4. **Special case** for opcode 93 (`OUT_FINAL` in the ROT13 name table; used here as a branch marker): replaced with opcode 95 (`STS` in the ROT13 name table; used here as a conditional-select construct), not simply predicated.
5. **Emit the new instruction** via `sub_92C240`, which creates the replacement in the code list.
6. **Transfer debug info**: `*new_instr+32 = *old_instr+32` (debug location).
7. **Delete the original** instruction via `sub_9253C0`.

```c
// Predicate guard is appended as TWO consecutive operands (each operand =
// 8 bytes = word0 + word1). sub_9324E0 lines 71-74 write:
//
//   slot N   = *a4               // caller-supplied guard pair (the
//                                 //   predicate-register operand:
//                                 //   word0 = 0x10000000 | P_index   (type 1)
//                                 //   word1 = 0 or 0x1000000 if negated  (@!P)
//   slot N+1: word0 = (P_index & 0xFFFFFF) | 0x60000000  // control word
//             word1 = 0
//
// The "type field" tags in the high nibble of word0:
//     0x1_______  = register (consumed/source)
//     0x6_______  = control / guard reference
// (Bits [30:28] = 1 vs 6; the high nibble carries the operand-class tag.)
//
// Example: @P2 IADD3 R0, R1, R2, RZ
//   Predicated operand list (each | separator = one 8-byte slot):
//     [R0_def | R1 | R2 | RZ | (word0=0x10000002, word1=0) | (word0=0x60000002, word1=0)]
//
// For @!P2 the second slot's word1 has bit 24 set (0x1000000).
```

### Already-Predicated Instructions — `sub_9321B0`

When `sub_9324E0` encounters an instruction with bit 12 already set (predicated by an earlier pass), it delegates to `sub_9321B0` (812 bytes) rather than blindly appending a second guard. The function composes the existing predicate with the new guard by emitting a PLOP3 (Ori opcode 23, three-input predicate logic) that ANDs the two predicates into a freshly-allocated predicate register, then re-predicates the stripped instruction with the combined result.

**Extracting the existing guard.** The function reads the operand count at `instr[20]` and indexes backward by 2 to locate the existing guard pair. Each operand is 8 bytes = (word0, word1):

```c
// instr[+84] is the operand-array base; slot k occupies DWORDs [2k, 2k+1].
// Predicate guard occupies the LAST TWO slots:
//   slot (count-2)  -- the predicate-register operand (type 1)
//     word0 at [2*(count-2)+21]: 0x10000000 | reg_index   (register operand)
//     word1 at [2*(count-2)+22]: flags; bit 24 (0x1000000) = '!' negation
//   slot (count-1)  -- the control / guard-reference operand (type 6)
//     word0 at [2*(count-1)+21] = [2*(count-2)+23]: 0x60000000 | reg_index
//     word1 at [2*(count-1)+22] = [2*(count-2)+24]: zero
//
// `sub_9321B0` reads:
//   existing_pred_word  = instr[2*(operand_count - 2) + 21]   // the register operand
//   existing_pred_index = existing_pred_word & 0xFFFFFF        // register index
//
// `sub_137E3A0` line 50 reads the SAME slot's word1 for negation:
//   negated = (instr[2*(operand_count - 2) + 22] & 0x1000000) != 0
```

**Fast path — accumulator reuse.** The caller passes an optional accumulator pointer (`a9`). When non-null and `*a9 != 0`, its low 5 bits cache the existing predicate index and bits 5-28 cache the previously-allocated combined register from a prior invocation. If the current instruction's existing predicate matches the cached index, the function skips PLOP3 emission: it extracts the combined register as `(*a9 >> 5) & 0xFFFFFF`, builds a type-1 guard operand (`combined_reg | 0x10000000`), strips the old guard (decrement operand count by 2, clear bit 12), and calls `sub_9324E0` to re-predicate with the cached result.

**Slow path — PLOP3 emission.** When no cached result is available:

1. **Allocates a new predicate register** via `sub_91BF30(ctx, 5)` (register file class 5 = predicate). The new register index is `allocated & 0xFFFFFF`.

2. **Updates the accumulator** with `*a9 = (existing_pred_index & 0x1F) | ((new_reg << 5) & 0x1FFFFFE0) | (*a9 & 0xE0000000)`, caching the mapping for subsequent instructions sharing the same existing predicate.

3. **Handles PT / constant predicates.** If the existing predicate index is 13 (PT, always-true), calls `sub_91CDD0(ctx, 0)` to obtain a constant-pool slot for integer 0, encoded as a type-2 operand (`slot | 0x24000000`). If the index is 2, calls `sub_91CDD0(ctx, -1)` for constant -1. These constants serve as truth-table inputs to the PLOP3 LUT so the AND degenerates correctly when one input is a fixed value.

4. **Emits a non-predicated PLOP3** via `sub_92C240(ctx, opcode=0x82, modifier=0x14, operand_count=2, ...)`. This defines the combined predicate register (type 9 def operand, `new_reg | 0x90000000`) using the existing guard values and, in the PT/constant cases, the constant operand.

5. **Emits a predicated PLOP3** via `sub_92C240(ctx, opcode=0x1082, modifier=0x14, operand_count=4, ...)`. Opcode `0x1082` is PLOP3 with bit 12 set (self-predicated). Its four operand pairs are: the combined predicate def (`new_reg | 0x90000000`), the existing guard pair copied from the original instruction, the new guard pair from the caller, and the guard word (`new_reg | 0x60000000`).

6. **Strips the old guard** from the original instruction (clears bit 12, decrements operand count by 2), then calls `sub_9324E0` to re-predicate with the combined predicate register.

```asm
// Example: @P3 FADD R0, R1, R2  (already predicated by P3)
//          new if-conversion guard = P5
//
// sub_9321B0 emits:
//        PLOP3.LUT  Pnew, P3, <const>          ; non-predicated setup
//   @Pnew PLOP3.LUT  Pnew, P3, P5, <guard=Pnew> ; Pnew = P3 AND P5
// then strips @P3 and re-predicates:
//   @Pnew FADD R0, R1, R2                       ; combined guard
```

**Register pressure management.** The predicate file has 7 usable registers (P0-P6) plus hardwired PT.

- Allocation via `sub_91BF30(ctx, 5)` draws from the same pool used by comparison instructions.
- When exhausted, the overflow allocator at `*(*(ctx+32)+8)` is consumed (freelist pop) or a fresh 160-byte descriptor is requested from the context's memory manager.
- The accumulator cache ensures consecutive instructions sharing the same existing predicate reuse a single combined register — the primary mechanism for bounding predicate register pressure during deep if-conversion.

## Post-Transformation — `sub_137DE90`

After predicating all instructions in a region, `sub_137DE90` (1,286 bytes) performs cleanup:

1. **Bitvector maintenance**: For each register operand in the predicated instructions, checks whether the register is live in the dominator's bitvector (at `context+832`). If the register is newly defined under the predicate, marks it in the bitvector via `sub_BDBB80`. This ensures later liveness analysis accounts for the conditionally-defined values.

2. **Per-instruction predication**: Walks the block's instruction list and calls `sub_9324E0` on each instruction, passing the predicate register index and the guard operand word.

3. **Predicate register tracking**: If any register was newly exposed to the bitvector, and the guard predicate is a non-negated register operand, marks the predicate register's descriptor at `+76` with bit 0 set, and increments a counter at `state+200`.

4. **Cleanup**: Resets the per-block tracking arrays (stored at `state[27]`/`state[56..57]`) which track which registers were bitvector-updated during this region.

## Speculative Execution Safety — `sub_137EE50`

After the main if-conversion, `sub_137EE50` (969 bytes) performs a secondary scan to identify instructions that were speculatively moved above their original control-flow guard. This function:

1. **Checks the global predication flag** at `context+1412` and the per-function flag at `context+1392` bit 0. If the function already has speculated instructions from a prior pass, returns immediately.

2. **Scans the true-side block** for load instructions to global or surface memory (opcodes 183 and 288 after masking). For each such load, queries the memory space via `sub_91C840` and checks whether space type 18 (unmapped/invalid) could be accessed.

3. **Records speculatively unsafe instructions** in a tracking hash set (at `state+240`), used by later passes to insert appropriate guard instructions or to avoid further speculation.

4. **Scans the false-side block** with the same logic.

The post-predication speculation safety check targets exclusively category 18 (`.surf`/tensor extended, sm_90+). This is the only memory space that `sub_137EE50` treats as requiring speculative-unsafe tracking; global loads and texture loads are considered acceptable for speculative execution in the predication cost model.

## Memory Space Classification for Predication

The bitmask `0x90E` appears in five functions within the predication pass (`sub_137D990`, `sub_137F560`, `sub_137F220`, `sub_137FB60`, `sub_1380810`). All five use the identical test pattern:

```c
category = sub_91C840(operand);          // classify memory space
if (category <= 0xB && ((1LL << category) & 0x90E) != 0)
    // load targets a primary data memory space
```

### Bitmask Decode

`0x90E` = binary `1001 0000 1110` — bits {1, 2, 3, 8, 11} are set.

| Bit | Category | PTX Space | In `0x90E`? | Role in predication |
|---|---|---|---|---|
| 0 | 0 | Generic (unqualified) | No | Unresolved address space — cannot be classified, excluded |
| 1 | 1 | `.shared` | **Yes** | CTA-scope scratchpad; always mapped for executing CTA; 20–30 cycle latency |
| 2 | 2 | `.local` | **Yes** | Thread-private stack/frame; always mapped; backed by L1/L2 |
| 3 | 3 | `.const` | **Yes** | Constant bank (`c[bank][offset]`); loaded by driver before launch; always mapped |
| 4 | 4 | `.param` | No | Kernel parameter memory; typically constant-folded or register-promoted by earlier passes |
| 5 | 5 | `.const` (extended) | No | Extended constant path (PTX inputs 21, 22); different scheduling model |
| 6 | 6 | `.global` (extended) | No | Extended global variant (PTX input 20); different scheduling model |
| 7 | 7 | Spill space | No | Compiler-generated register spill/fill; handled separately by regalloc |
| 8 | 8 | `.tex` | **Yes** | Texture memory; high latency (200+ cycles); texture cache always valid when bound |
| 9 | 9 | Special (opcode-dep.) | No | Ambiguous classification from case-18 sub-switch in `sub_91C840` |
| 10 | — | (unused) | No | No memory space maps to category 10 |
| 11 | 11 | `.global` | **Yes** | DRAM-backed global memory; highest latency (300+ cycles) |

Categories 12–18 (code/function, uniform, register file, surface, surface/tensor extended) all exceed the `<= 0xB` range check and are excluded from the bitmask test automatically.

### What the Bitmask Selects

The five selected categories — shared, local, const, texture, global — are the **primary data memory spaces**: the ones that involve real data movement through the GPU memory hierarchy and carry meaningful scheduling latency. These are the loads a scheduler can profitably overlap with predicated computation.

The excluded categories are either:
- **Unresolvable** (generic — could be anything)
- **Non-load** in practice (param — folded away, code — function pointers)
- **Compiler-internal** (spill, special — the compiler already knows how to handle these)
- **Out of range** (register file, uniform, surface, surface/tensor — categories > 11)

### How the Bitmask Affects Profitability

The bitmask test does NOT directly determine speculation safety. It sets a `has_primary_memory_load` flag at candidate offset `+12`, which the profitability heuristic (`sub_1380BF0`) uses in three ways:

1. **True-side memory loads** (`a2+12` set): The profitability check routes to the extended diamond analysis (`sub_137FE10`) instead of the standard size-threshold path. This allows larger regions to be if-converted when they contain meaningful loads.

2. **False-side memory loads — speculation guard** (`a3+12` set): If the false side has memory loads AND the SM backend's speculation policy (vtable at `sm_backend+1200`) allows it, the detailed speculation analysis (`sub_137F800`) is invoked. If that analysis flags the loads as risky, predication is rejected.

3. **False-side memory loads — profitability boost** (`a3+12` set, passes safety): If the false side has memory loads and passes safety checks, the profitability heuristic returns `true` directly (line 166 of `sub_1380BF0`). The reasoning: if the false-side code contains real memory loads, converting the branch to predicated straight-line code lets the scheduler overlap those loads with other work.

### Speculation Safety (Separate Mechanism)

The actual speculation safety tracking is handled by `sub_137EE50` (post-predication scan), which uses a **different criterion** from the `0x90E` bitmask:

- Scans both sides for opcodes 183 (LDG) and 288 (STG) after masking
- For each, queries `sub_91C840` and checks if category == 18 (`.surf`/tensor extended)
- Only category 18 loads are tracked as "speculatively unsafe" in the hash set at `state+240`
- The `context+1392` bit 0 flag persists and is checked by `OriHoistInvariantsLate` (phase 66)

This means global loads (category 11) that are speculatively predicated are **not** tracked as unsafe. The ptxas cost model rationale:

- Global memory loads under a predicate guard are acceptable: the hardware issues the load speculatively, and if the predicate is false, the result is simply discarded.
- On architectures with memory access traps (e.g., page faults on unmapped addresses), the hardware masks the fault for lanes where the predicate is false.
- Surface/tensor extended operations (category 18), however, may have side effects that cannot be masked, so they receive the unsafe designation.

## Fall-Through Block Analysis — `sub_1380810`

When the standard profitability check is inconclusive, `sub_1380810` (980 bytes) analyzes the fall-through continuation of the merge block. The idea: even if the region itself is borderline, if the code immediately after the merge point contains long-latency operations (loads, texture fetches), the predicated version may be better because the scheduler can overlap the predicated instructions with those long-latency operations.

The function walks instructions in the merge block's successor(s), using the same `0x90E` bitmask test to identify primary-data-memory loads. Non-load instructions are checked via the SM backend's vtable at `sm_backend+1824`. The function counts:
- Primary-memory-space loads (via the `0x90E` mask)
- Other long-latency operations (via the backend vtable check)
- Total instruction count

If the fall-through region contains enough long-latency work (compared to `state->fallthrough_limit` and `state->extended_limit`), the function returns true, indicating that predication is profitable despite the region being above the standard size threshold.

## Extended Diamond Analysis — `sub_137FE10`

For complex diamonds where one side has primary-memory loads that affect profitability thresholds, `sub_137FE10` (2,550 bytes) performs a more thorough analysis. It can "look through" the diamond to the merge block and even one block beyond, checking whether the instruction mix in the continuation makes predication worthwhile. It invokes `sub_137F560` (which also uses the `0x90E` bitmask) to scan continuation blocks for scheduling-relevant loads.

The function also handles the case where the merge block falls through to another conditional branch that itself is a predication candidate — effectively analyzing a chain of adjacent diamonds.

## Interaction with Later Passes

The predication pass is positioned to maximize the benefit of subsequent passes:

| Phase | Name | Interaction |
|---|---|---|
| 64 | `LateOriCommoning` | Predication may create duplicate computations on both sides of the original branch. Commoning eliminates these by recognizing that `@P0 IADD3 R0, R1, R2, RZ` and `@!P0 IADD3 R0, R1, R2, RZ` with the same inputs can be merged into an unconditional instruction. |
| 65 | `GeneralOptimizeLate2` | The copy propagation and constant folding sub-passes clean up the predicated code: dead predicate definitions, redundant MOVs introduced by the PHI destruction at merge points, and constant-foldable predicates. |
| 66 | `OriHoistInvariantsLate` | Predication can convert loop-varying branches into predicated straight-line code. LICM then hoists any newly-exposed loop-invariant computations. |
| 69 | `OriDoRemat` | Predicated instructions that define values used far from their definition are candidates for rematerialization, reducing register pressure. |
| 70 | `OriPropagateVaryingSecond` | After predication changes the control flow, varying annotations must be recomputed. The second varying-propagation pass updates which values are uniform vs. divergent. |

The `context+1392` bit 0 flag set by `sub_137EE50` persists through these passes and is checked by `OriHoistInvariantsLate` to avoid hoisting speculatively-unsafe instructions out of their guarded context.

## Key Functions

| Address | Size | Function | Role |
|---|---|---|---|
| `sub_1381DA0` | 1,517 B | `OriDoPredication::execute` | Phase entry point; gating, setup, cleanup |
| `sub_1381CD0` | 206 B | `runPredicationDriver` | Iterative driver; calls main loop up to 3 times |
| `sub_1381010` | 3,249 B | `predicationMainLoop` | RPO walk, region identification, transformation dispatch |
| `sub_137E3A0` | 367 B | `isTriangleDiamondCandidate` | CFG pattern validation |
| `sub_137D990` | 1,270 B | `analyzeRegion` | Per-block instruction scan, cost modeling |
| `sub_137D8B0` | 209 B | `canPredicateInstruction` | Single-instruction predicability check |
| `sub_1380BF0` | 1,055 B | `evaluateProfitability` | Multi-factor profitability decision |
| `sub_137FE10` | 2,550 B | `analyzeExtendedDiamond` | Extended diamond and chain analysis |
| `sub_137F800` | 864 B | `analyzeSpeculationSafety` | Speculation safety for side-effect loads |
| `sub_1380810` | 980 B | `analyzeFallThrough` | Fall-through block continuation analysis |
| `sub_137EE50` | 969 B | `markSpeculativeInstructions` | Post-transformation speculative-load tracking |
| `sub_137DE90` | 1,286 B | `applyPredication` | Instruction rewriting and bitvector update |
| `sub_137FB60` | 687 B | `classifyInstruction` | Per-instruction classification during walk |
| `sub_137F560` | 665 B | `scanBlockForUnsafe` | Block scan for speculative safety |
| `sub_137F220` | 828 B | `classifyInstructionExtended` | Classification with bitvector tracking |
| `sub_137E510` | 2,360 B | `moveInstructionsToHash` | Instruction movement during transformation |
| `sub_9324E0` | 280 B | `predicateInstruction` | Adds predicate guard to single instruction |
| `sub_9321B0` | ~800 B | `predicateAlreadyGuarded` | Handles already-predicated instructions |
| `sub_92C240` | (shared) | `createInstruction` | Instruction builder (shared utility) |

## SASS Predicate Model

NVIDIA SASS provides 7 usable predicate registers (P0–P6) plus the hardwired always-true register PT. Every instruction in the SASS ISA can optionally carry a predicate guard:

```asm
@P0  IADD3 R0, R1, R2, RZ    // executes only if P0 is true
@!P2 FMUL  R3, R4, R5         // executes only if P2 is false
     FADD  R6, R7, R8          // unconditional (implicit @PT)
```

Predicate conditions are set by comparison instructions:

```asm
ISETP.GT.AND P0, PT, R1, R2, PT   // P0 = (R1 > R2) AND PT
FSETP.LT.AND P1, P2, R3, R4, PT   // P1 = (R3 < R4), P2 = !(R3 < R4)
```

Uniform predicates (UP0–UP6, UPT) are the warp-uniform variant available on sm_75+. When all threads in a warp have the same predicate value, using UP instead of P avoids consuming a per-thread predicate register and enables the hardware to skip the entire instruction rather than masking per-thread.

In the Ori IR, the predicate **guard** is appended as a pair of operands:

- **register operand** — word0 type tag 1, `0x10000000 | reg_index`; word1 carries the negation flag at bit 24 / `0x1000000`.
- **control word** — word0 type tag 6, `0x60000000 | reg_index`; word1 zero.

> The "type field 5" used elsewhere in the operand-class taxonomy refers to constant-pool indirect, not predicates.

The predicate-register **encoder** at the SASS-binary level (`sub_7BC5C0`, 416 bytes, see [Encoding](../codegen/encoding.md#predicate-encoder--sub_7bc5c0)) emits two contiguous bitfields:

- a 1-bit presence/range flag, and
- a 5-bit predicate value where bits [2:0] select P0..P6/PT and the upper two bits encode the file (P vs UP) and the `.NOT` polarity.

So the **same** negation signal travels through three independent surfaces: Ori operand word1 bit 24, textual `!` in the descriptor name at `+2120`, and the SASS-binary 5-bit field's polarity bit.

## Opcode Reference

Key opcodes referenced by the predication pass (after `BYTE1 &= 0xCF` masking to clear bits 12-13):

| Value | Mnemonic | Role in predication |
|---|---|---|
| 93 | `OUT_FINAL` | ROT13 name is OUT_FINAL; used here as a conditional branch marker — the instruction being eliminated. Actual SASS BRA is opcode 67. |
| 95 | `STS` | ROT13 name is STS; used here as the branch terminator class marker and conditional-select replacement target. Actual SASS EXIT is opcode 77. |
| 97 | `STG` | ROT13 name is STG; used here as a block boundary sentinel for scan termination. Actual SASS CALL is opcode 71. |
| 125 | LD (variant) | Load — checked for speculative safety |
| 130 | `HSET2` | ROT13 name is HSET2; used here as an internal marker for MOV-like instructions counted separately for profitability. Actual SASS MOV is opcode 19. |
| 183 | LDG | Global load — speculative-unsafe |
| 188 | (variant) | Remapped to 190 when predicated |
| 263 | MOV.PHI | SSA phi — not counted in instruction totals |
| 286 | CONV.ALLOC | Convergence allocation marker — special handling in profitability check |
| 288 | STG | Global store — speculative-unsafe |
| 22 | `R2P` | Long-latency: register-to-predicate cross-file move |
| 50 | `FRND_X` | Long-latency: extended FP rounding variant |
| 77 | `EXIT` | Long-latency: thread exit / termination |
| 83 | `TEX` | Long-latency: texture fetch |
| 297 | `MERCURY_barrier_cta_red_popc...` | Long-latency: Mercury barrier reduction (explicit check) |
| 352 | `MERCURY_addmin_srcs_r_r_0` | Long-latency: Mercury addmin (explicit check) |

## Cross-References

- [Pass Inventory](index.md) — phase 63 in the 159-phase table
- [Varying Propagation](varying-propagation.md) — phases 53/70 bracket predication; phase 70 refreshes divergence annotations after CFG changes
- [IR Overview](../ir/overview.md) — Ori instruction format, operand encoding, register files
- [Copy Propagation & CSE](copy-prop-cse.md) — phase 64 (LateOriCommoning) runs immediately after
- [GeneralOptimize Bundles](general-optimize.md) — phase 65 cleans up after predication
- [Loop Passes](loop-passes.md) — phase 66 (OriHoistInvariantsLate) hoists newly exposed invariants
- [Rematerialization](rematerialization.md) — phase 69 (OriDoRemat) handles increased register pressure
- [Liveness Analysis](liveness.md) — liveness rebuilt at entry, bitvectors maintained during transformation
- [Knobs System](../config/knobs.md) — knobs 260, 487, 577, 579, 582 control predication behavior
- [Scheduling](../scheduling/overview.md) — scheduler backend initializes profitability thresholds
