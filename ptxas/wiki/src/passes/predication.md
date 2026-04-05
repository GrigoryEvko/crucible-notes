# Predication (If-Conversion)

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
| **SSA window** | Yes -- runs at phase 63, within the partial-SSA window (phases 23--73) |
| **Pipeline position** | After `OriRemoveRedundantMultiDefMov` (62), before `LateOriCommoning` (64) |
| **Gating** | Disabled when bit 5 of `context+1376` flags is set; can be disabled via `PTXAS_DISABLED_PASSES` containing `"Predication"` |
| **Knob controls** | Knob 487 (enable/limit gate), knob 577 (per-region enable), knob 579 (texture-bearing region gate), knob 582 (block-level cold-region query), knob 260 (extra-latency penalty check) |

## GPU Motivation

The SIMT execution model makes predication qualitatively different from its role on scalar CPUs.

On a scalar CPU, a correctly-predicted branch is essentially free -- the branch predictor eliminates the control flow cost. If-conversion on CPUs is a niche optimization applied only when branches are highly unpredictable.

On a GPU, a divergent conditional branch forces the warp to serialize: the hardware executes the taken path with some threads masked off, then executes the not-taken path with the complementary mask. Both paths execute regardless, and the warp reconverges at the post-dominator. The cost is the sum of both paths, not the maximum.

Predication eliminates this divergence penalty entirely. Both paths still execute, but without the overhead of stack-based reconvergence (`BSSY`/`BSYNC` pairs on sm_70+), without the branch instruction itself, and with the ability for the scheduler to interleave the predicated instructions with other independent work. For short regions (a few instructions per side), predication is strictly superior to branching.

```
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
2. **Iterative driver** (`sub_1381CD0`): initializes the scheduler context via vtable dispatch at `sched_ctx+1296`, then calls the main loop up to 3 times (controlled by a knob at options offset 41768) with different aggressiveness settings.
3. **Main RPO loop** (`sub_1381010`): walks the RPO block order, identifies candidate branch regions, evaluates profitability, and applies the transformation.

### Entry Point -- `sub_1381DA0`

```
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

### Iterative Driver -- `sub_1381CD0`

```
sub_1381CD0(state):
    // Initialize scheduler context
    sched_ctx = *(context+1584)
    init_fn = vtable(sched_ctx)+1296
    if init_fn == sub_7D82C0:       // fast path: zero-init
        clear state fields
    else:
        init_fn(sched_ctx, state)    // backend-specific init

    bb_count = *(context+520)
    if bb_count <= 1: return 0       // nothing to if-convert

    // Determine iteration count from knob at options+41760
    iterations = 0
    if *(options+41760) == 1:
        iterations = *(options+41768)

    // First pass: always run
    state[14].byte[8] = 0           // not second-pass mode
    changed = sub_1381010(state)

    // Optional second/third pass with relaxed thresholds
    while changed and iterations > 0:
        state[14].byte[8] = (iterations == 1)
        changed = sub_1381010(state)
        if iterations <= 2: break
```

The iteration mechanism allows the pass to make a second (and potentially third) traversal with progressively relaxed profitability thresholds. The flag at `state[14].byte[8]` signals the final iteration, which changes some size-limit comparisons in the profitability heuristic.

### Main Loop -- `sub_1381010`

The main loop walks basic blocks in RPO order (via the block index array at `context+512`), identifies candidate branch regions, and decides whether to if-convert each one.

```
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

```
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

```
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

```
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

## Region Analysis -- `sub_137E3A0`

This function (`sub_137E3A0`, 367 bytes) validates that a basic block is part of a valid if-conversion candidate. It checks:

1. **Predecessor count**: The merge block must have exactly `header_predecessor_count + 1` predecessors.
2. **Terminator type**: The header's terminator must match opcode 95 after masking bits 12-13 (`STS` in the ROT13 name table; used here as a control-flow terminator class marker, not an actual store-shared instruction).
3. **Branch predicate**: The branch guard must be a non-negated register operand (type field `(>>28)&7 == 1`), from the predicate register file (register file type checked against the state's expected file types 2 or 3, corresponding to R or UR).
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

## Region Scanning -- `sub_137D990`

This function (1,270 bytes) walks all instructions in a candidate block, counting them and checking each for predicability. It builds a cost model:

### Per-Instruction Checks

For each instruction in the candidate block:

1. **Already-predicated check** (opcode bit 12 = `0x1000`): Instructions that already carry a predicate guard are flagged via `state+48` for special handling.

2. **MOV counting** (opcode 130): Instructions with opcode 130 (`HSET2` in the ROT13 name table; the code treats this value as an internal marker for MOV-like operations) that match specific operand patterns increment a separate MOV counter at `state+4`, used to adjust profitability thresholds. The actual SASS MOV instruction is opcode 19.

3. **Predicable instruction check** (`sub_137D8B0`): Each instruction is tested via the backend's `canPredicate` vtable method at `sched_ctx+1424`. Instructions that cannot be predicated (atomics, certain memory operations, barriers) cause the scan to fail.

4. **Speculative execution safety**: For load instructions (opcode 125 after masking), the memory space is queried via `sub_91C840`. The result is tested against the bitmask `0x90E`:
   - Bit 1: register file space -- safe
   - Bit 2: local memory -- safe
   - Bit 3: shared memory -- safe
   - Bit 8: constant memory -- safe
   - Bit 11: (internal type) -- safe
   - Global memory, surface memory, texture memory -- **not safe** (side effects or traps on invalid addresses)

5. **Extra-latency check**: Instructions matching opcodes in the set `{22, 23, 41, 42, 55, 57, 352, 297}` (long-latency operations including texture, surface, and certain memory ops) have their latency contribution tallied at `state+16` via the scheduler's `getExtraLatency` method at `sched_ctx+1392`.

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

        // Speculative memory safety
        if isLoadOp(instr):
            space = getMemorySpace(instr)
            if space is in {global, surface, texture, unmapped}:
                candidate->has_side_effect_load = true

        // Extra latency accounting
        if isLongLatencyOp(instr):
            candidate->extra_latency += getExtraLatency(instr)

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

## Profitability Heuristic -- `sub_1380BF0`

The profitability decision (`sub_1380BF0`, 1,055 bytes) is the most complex part of the pass. It considers multiple factors to decide whether converting a branch region to predicated code is profitable.

### Decision Flow

```
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

    // 5. Speculative execution check
    if false_side->has_side_effect_load:
        return sub_137F800(...)        // detailed speculation analysis

    // 6. Extra-latency penalty
    if CheckKnob260(knob_state):
        if true_side->extra_latency > 0 and false_side->extra_latency > 0:
            return false               // both sides have long-latency ops

    // 7. Size-based thresholds (main heuristic)
    instr_count = true_side->instr_count

    if false_side->has_side_effect_load:
        // Side-effect loads require tighter thresholds
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

    // 9. Extended diamond analysis for remaining cases
    if false_side->has_side_effect_load:
        return true
    return sub_1380810(...)            // fall-through block analysis
```

### Threshold Fields

The state object contains multiple instruction-count thresholds, initialized by the scheduler backend during `sub_1381CD0`:

| State offset (as `int32` index) | Field | Typical role |
|---|---|---|
| `[8]` | `base_limit` | Maximum instructions for simple (non-textured, non-uniform) regions |
| `[9]` | `tex_limit` | Maximum instructions for textured regions (without uniform speculation) |
| `[10]` | `uniform_limit` | Maximum instructions with uniform-speculation enabled |
| `[11]` | `uniform_tex_limit` | Maximum for textured + uniform-speculation regions |
| `[12]` | `threshold` | Hard ceiling on non-MOV instruction count |
| `[13]` | `combined_limit` | Maximum for combined (both-sides) instruction count |
| `[14]` | `fallthrough_limit` | Threshold for fall-through block extension |
| `[15]` | `extended_limit` | Threshold for extended diamond regions |
| `[16]` | `mov_threshold` | MOV count below which standard limits apply |
| `[17]` | `mov_limit` | MOV-specific threshold |

These values are architecture-specific -- the scheduler backend's vtable method at offset 1296 initializes them based on the SM target and optimization level.

## Instruction Predication -- `sub_9324E0`

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

```
// Predicate guard encoding in operand word:
//   guard_pred = predicate_reg_index | 0x60000000
//   (type field 3 = 0x6000_0000 >> 28, register index in low 24 bits)
//
// Example: @P2 IADD3 R0, R1, R2, RZ
//   Original IADD3 operands: [R0_def, R1, R2, RZ]
//   Predicated operands:     [R0_def, R1, R2, RZ, guard_word, P2 | 0x60000000]
```

### Already-Predicated Instructions

Instructions that already have a predicate guard (bit 12 set in original opcode) are handled by `sub_9321B0`, which must compose the existing predicate with the new guard using a predicate-AND or predicate-SEL operation rather than simply replacing the guard.

## Post-Transformation -- `sub_137DE90`

After predicating all instructions in a region, `sub_137DE90` (1,286 bytes) performs cleanup:

1. **Bitvector maintenance**: For each register operand in the predicated instructions, checks whether the register is live in the dominator's bitvector (at `context+832`). If the register is newly defined under the predicate, marks it in the bitvector via `sub_BDBB80`. This ensures later liveness analysis accounts for the conditionally-defined values.

2. **Per-instruction predication**: Walks the block's instruction list and calls `sub_9324E0` on each instruction, passing the predicate register index and the guard operand word.

3. **Predicate register tracking**: If any register was newly exposed to the bitvector, and the guard predicate is a non-negated register operand, marks the predicate register's descriptor at `+76` with bit 0 set, and increments a counter at `state+200`.

4. **Cleanup**: Resets the per-block tracking arrays (stored at `state[27]`/`state[56..57]`) which track which registers were bitvector-updated during this region.

## Speculative Execution Safety -- `sub_137EE50`

After the main if-conversion, `sub_137EE50` (969 bytes) performs a secondary scan to identify instructions that were speculatively moved above their original control-flow guard. This function:

1. **Checks the global predication flag** at `context+1412` and the per-function flag at `context+1392` bit 0. If the function already has speculated instructions from a prior pass, returns immediately.

2. **Scans the true-side block** for load instructions to global or surface memory (opcodes 183 and 288 after masking). For each such load, queries the memory space via `sub_91C840` and checks whether space type 18 (unmapped/invalid) could be accessed.

3. **Records speculatively unsafe instructions** in a tracking hash set (at `state+240`), used by later passes to insert appropriate guard instructions or to avoid further speculation.

4. **Scans the false-side block** with the same logic.

The bitmask `0x90E` identifies memory spaces safe for speculation:

| Bit | Space type | Safe? | Reason |
|---|---|---|---|
| 0 | Generic | No | Could resolve to any space |
| 1 | Register | Yes | Cannot trap |
| 2 | Local | Yes | Thread-private, always mapped |
| 3 | Shared | Yes | Block-scope, always mapped if within bounds |
| 4 | Global | No | May fault on unmapped addresses |
| 5 | Constant (generic) | No | Could be unmapped |
| 6 | Global (explicit) | No | May fault |
| 7 | Texture/surface | No | May trap or return garbage |
| 8 | Constant (banked) | Yes | Always mapped by driver |
| 9-10 | (reserved) | No | |
| 11 | (internal) | Yes | Compiler-internal use |

## Fall-Through Block Analysis -- `sub_1380810`

When the standard profitability check is inconclusive, `sub_1380810` (980 bytes) analyzes the fall-through continuation of the merge block. The idea: even if the region itself is borderline, if the code immediately after the merge point contains long-latency operations (loads, texture fetches), the predicated version may be better because the scheduler can overlap the predicated instructions with those long-latency operations.

The function walks instructions in the merge block's successor(s), counting:
- Long-latency memory operations (via `sched_ctx+1824` predicability check)
- Load instructions to specific memory spaces (again using the `0x90E` mask)
- Total instruction count

If the fall-through region contains enough long-latency work (compared to `state->fallthrough_limit` and `state->extended_limit`), the function returns true, indicating that predication is profitable despite the region being above the standard size threshold.

## Extended Diamond Analysis -- `sub_137FE10`

For complex diamonds where one side has speculative-execution concerns, `sub_137FE10` (2,550 bytes) performs a more thorough analysis. It can "look through" the diamond to the merge block and even one block beyond, checking whether the instruction mix in the continuation makes predication worthwhile.

The function also handles the case where the merge block falls through to another conditional branch that itself is a predication candidate -- effectively analyzing a chain of adjacent diamonds.

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

NVIDIA SASS provides 7 usable predicate registers (P0--P6) plus the hardwired always-true register PT. Every instruction in the SASS ISA can optionally carry a predicate guard:

```
@P0  IADD3 R0, R1, R2, RZ    // executes only if P0 is true
@!P2 FMUL  R3, R4, R5         // executes only if P2 is false
     FADD  R6, R7, R8          // unconditional (implicit @PT)
```

Predicate conditions are set by comparison instructions:

```
ISETP.GT.AND P0, PT, R1, R2, PT   // P0 = (R1 > R2) AND PT
FSETP.LT.AND P1, P2, R3, R4, PT   // P1 = (R3 < R4), P2 = !(R3 < R4)
```

Uniform predicates (UP0--UP6, UPT) are the warp-uniform variant available on sm_75+. When all threads in a warp have the same predicate value, using UP instead of P avoids consuming a per-thread predicate register and enables the hardware to skip the entire instruction rather than masking per-thread.

In the Ori IR, predicate operands are encoded with type field 5 (bits 28-30 of the packed operand word). The guard predicate is appended as a pair of extra operands: the guard control word (type 3, `0x60000000 | reg_index`) followed by the predicate register operand itself.

## Opcode Reference

Key opcodes referenced by the predication pass (after `BYTE1 &= 0xCF` masking to clear bits 12-13):

| Value | Mnemonic | Role in predication |
|---|---|---|
| 93 | `OUT_FINAL` | ROT13 name is OUT_FINAL; used here as a conditional branch marker -- the instruction being eliminated. Actual SASS BRA is opcode 67. |
| 95 | `STS` | ROT13 name is STS; used here as the branch terminator class marker and conditional-select replacement target. Actual SASS EXIT is opcode 77. |
| 97 | `STG` | ROT13 name is STG; used here as a block boundary sentinel for scan termination. Actual SASS CALL is opcode 71. |
| 125 | LD (variant) | Load -- checked for speculative safety |
| 130 | `HSET2` | ROT13 name is HSET2; used here as an internal marker for MOV-like instructions counted separately for profitability. Actual SASS MOV is opcode 19. |
| 183 | LDG | Global load -- speculative-unsafe |
| 188 | (variant) | Remapped to 190 when predicated |
| 263 | MOV.PHI | SSA phi -- not counted in instruction totals |
| 286 | CONV.ALLOC | Convergence allocation marker -- special handling in profitability check |
| 288 | STG | Global store -- speculative-unsafe |
| 352, 297 | (long-latency) | Texture/surface ops -- extra latency penalty |

## Cross-References

- [Pass Inventory](index.md) -- phase 63 in the 159-phase table
- [IR Overview](../ir/overview.md) -- Ori instruction format, operand encoding, register files
- [Copy Propagation & CSE](copy-prop-cse.md) -- phase 64 (LateOriCommoning) runs immediately after
- [GeneralOptimize Bundles](general-optimize.md) -- phase 65 cleans up after predication
- [Loop Passes](loop-passes.md) -- phase 66 (OriHoistInvariantsLate) hoists newly exposed invariants
- [Rematerialization](rematerialization.md) -- phase 69 (OriDoRemat) handles increased register pressure
- [Liveness Analysis](liveness.md) -- liveness rebuilt at entry, bitvectors maintained during transformation
- [Knobs System](../config/knobs.md) -- knobs 260, 487, 577, 579, 582 control predication behavior
- [Scheduling](../scheduling/overview.md) -- scheduler backend initializes profitability thresholds
