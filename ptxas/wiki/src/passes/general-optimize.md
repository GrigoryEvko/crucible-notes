# GeneralOptimize Bundles

The `GeneralOptimize*` passes are compound optimization bundles that run multiple sub-transformations in sequence on each basic block, repeating until no further changes occur (fixed-point iteration). They serve as the primary IR cleanup mechanism throughout the pipeline: after any major transformation introduces new dead code, redundant copies, or foldable constants, a GeneralOptimize pass re-normalizes the IR before the next major phase.

Six instances exist at strategic positions in the 159-phase pipeline. Despite sharing the "GeneralOptimize" name prefix, the six instances decompose into three distinct implementation families -- a lightweight block-iteration variant, a heavyweight bitvector-tracked orchestrator, and an indirect vtable dispatch stub. Each family shares a common architectural pattern (per-block iteration with convergence check) but invokes different sub-pass combinations and has different gate conditions.

| | |
|---|---|
| **Instances** | 6 (phases 13, 29, 37, 46, 58, 65) |
| **Pattern** | Per-block iteration with convergence check |
| **Sub-passes** | Copy propagation, constant folding, structural equivalence elimination, dead code elimination, predicate simplification, register promotion (Phase 37) |
| **Convergence** | Boolean change flag per iteration; stops when no sub-pass reports a change |
| **Iteration cap** | Knob-controlled (option 464); breaks loop if knob returns false |
| **Single-function fast path** | Phases 13 and 65 have direct tail-call paths bypassing the multi-function dispatch |
| **Multi-function gate** | All variants check `sub_7DDB50(ctx) > 1` before entering the main loop |
| **Code range** | Execute functions at `0xC5F940`--`0xC60870`; sub-pass bodies at `0x7917F0`--`0x910840` |

## Instance Map

| Phase | Name | Vtable | `execute()` | Sub-pass Body | Gate Conditions |
|---|---|---|---|---|---|
| 13 | `GeneralOptimizeEarly` | `off_22BD7D0` | `0xC5F940` | `sub_7917F0` (multi-func) / `0x1C64BF0` (single-func) | `bit 2` of `ctx+1382` must be set |
| 29 | `GeneralOptimize` | `off_22BDA50` | `0xC5FC50` | `sub_908EB0` | Option 487 enabled; option 231 not set; option 461 pass |
| 37 | `GeneralOptimizeMid` | `off_22BDB90` | `0xC5FD70` | `sub_910840` | `sub_8F3EA0` pre-check; option 487; "ConvertMemoryToRegisterOrUniform" name-gate |
| 46 | `GeneralOptimizeMid2` | `off_22BDCF8` | `0xC60840` | indirect via `[*(ctx+1584)]->vtable[0x1C0]` | Vtable dispatch; skips if target == `sub_7D6DD0` (no-op sentinel) |
| 58 | `GeneralOptimizeLate` | `off_22BDED8` | `0xC5FF20` | `sub_8F7080` | Function count > 2; bits 4-5 of `ctx+1396` != `0x20`; option 31 checked |
| 65 | `GeneralOptimizeLate2` | `off_22BDFF0` | `0xC60550` | indirect via `[*(ctx+1584)]->vtable[392]` | Function count > 1; indirect dispatch through compilation unit vtable |

## Architecture: Three Structural Variants

### Variant A: Block-Iteration with Explicit Fixed-Point Loop (Phases 13, 29)

The Early and standard GeneralOptimize passes iterate over basic blocks with an explicit convergence loop. Phase 13 (`GeneralOptimizeEarly`) at `sub_7917F0` is the simplest and best-documented:

```c
// sub_7917F0 -- GeneralOptimizeEarly (multi-function path)
void GeneralOptimizeEarly(int64_t ctx) {
    if (!(*(uint8_t*)(ctx + 1382) & 4))   return;   // gate: optimization flag

    // Option 214 check -- uses vtable fast-path comparison:
    //   if vtable[72] == sub_6614A0, reads *(config + 15408) directly
    //   otherwise calls the virtual getOption(214)
    if (getOption(ctx, 214))               return;   // gate: skip if set

    // Option 487 check -- uses vtable[152] fast-path:
    //   if vtable[152] == sub_67EB60, calls sub_7468B0(config, 487)
    //   otherwise calls the virtual isOptionSet(487, 1)
    if (!getOption_v2(ctx, 487))           return;   // gate: general opt enable

    if (*(int64_t*)(*(int64_t*)ctx + 1056)) return;  // gate: already processed

    sub_785E20(ctx, 0);                    // reset per-block change tracking
    sub_781F80(ctx, 1);                    // initialize instruction flags
    sub_7E6090(ctx, 0, 0, 0, 0);          // prepare operand use/def chains
    sub_7E6AD0(ctx, 0, ...);              // build def-use/use-def links

    // Iterate over basic blocks (block_count at ctx+520)
    int bb_count = *(int32_t*)(ctx + 520);
    for (int i = 1; i <= bb_count; i++) {
        // block_order at ctx+512, block_table at ctx+296
        int bb_idx = *(int32_t*)(*(int64_t*)(ctx + 512) + 4*i);
        BasicBlock* bb = *(BasicBlock**)(*(int64_t*)(ctx + 296) + 8*bb_idx);

        // Fixed-point loop on this block
        int64_t state[...];   // stack-allocated state at rbp-0x88
        while (true) {
            bool changed = sub_753600(&state, bb);   // run sub-passes
            if (!changed)  break;

            // Iteration cap: knob 464
            if (!getOption_v2(ctx, 464))  break;

            sub_753B50(&state);            // apply instruction rewrites
        }
    }

    if (any_changed)
        sub_785E20(ctx, 0);                // re-normalize if anything changed
}
```

The inner function `sub_753600` runs on a single basic block and returns a boolean indicating whether any transformation fired. When it returns `true`, `sub_753B50` applies the accumulated changes (instruction replacement, operand rewriting, def-use chain updates), and the loop re-runs `sub_753600` on the same block to check if the new IR enables further simplifications.

The convergence check for option 464 acts as an emergency brake: if the knob returns false, the loop breaks even if changes were detected. This prevents pathological cases where mutual transformations oscillate indefinitely.

**Phase 29** (`sub_C5FC50`) follows the same pattern but delegates to `sub_908EB0`, which implements a more complex instruction walk with additional opcode dispatch (opcodes 97, 18, 124) and predicate-aware propagation.

### Variant B: Full-Program Sub-Pass Orchestration (Phases 37, 58)

The Mid and Late variants operate at a higher level: they construct a multi-field context structure, initialize bitvector tracking infrastructure, and call a heavyweight sub-pass orchestrator.

#### Phase 37 -- GeneralOptimizeMid (`sub_910840`)

1. Calls `sub_8F3EA0` -- a pre-condition check (returns false to skip the entire pass)
2. Checks option 487 (general optimization enable) via the same vtable fast-path pattern
3. Calls `sub_799250` with the string `"ConvertMemoryToRegisterOrUniform"` (at `0x21DD228`) -- a named phase gate that allows the pass to be selectively disabled via `--no-phase`
4. Constructs a **0x408-byte context object** on the stack with vtable pointer `off_21DBEF8` at offset 0. The layout is:
   ```
   GeneralOptimizeMid Context (0x408 bytes)
     +0x000  vtable_ptr     = off_21DBEF8
     +0x008  allocator      = *(ctx + 16)
     +0x010  (zero-init)    ...
     +0x018  (zero-init)    ...
     +0x020  (zero-init)    ...
     +0x030  int count      = 0
     +0x040  sub_context    -- initialized by sub_905B50 (bitvectors, register tracking)
     ...
   ```
5. Calls `sub_905B50` -- a 500+ line setup function that creates bitvector arrays for tracking register definitions, use-def chains, and per-block change flags. Allocates three pairs of {bitvector, metadata, capacity} structures for tracking definition reach, register liveness, and fold eligibility
6. Calls `sub_90FBA0` -- the main optimization loop that iterates over all blocks, running sub-passes per instruction

After `sub_90FBA0` returns, the function destroys three RAII-style bitvector containers at offsets `+0x200`, `+0x228`, and `+0x1E0` by invoking their vtable destructors via `*(vtable + 32)`.

#### Phase 58 -- GeneralOptimizeLate (`sub_8F7080`)

1. Checks function count > 2 via `sub_7DDB50` (stricter than other variants that check > 1)
2. Checks optimization level bits at `ctx+1396`: the condition `(flags & 0x30) != 0x20` ensures the pass is skipped at certain reduced optimization levels
3. Checks option 31 via the vtable fast-path; when option 31 reports as "extended" (value at `config+2232` is 1 with non-zero extra word at `config+2240`), an additional `sub_7DC0E0` check determines a secondary control flag `v7`
4. Constructs a **0x168-byte context** on the stack with 7 sub-pass tracking groups. Each group occupies 56 bytes (three `__int128` values + a boolean changed-flag + a counter):
   ```
   GeneralOptimizeLate Context (0x168 bytes)
     +0x000  ctx_ptr     = ctx (the compilation context)
     +0x008  flag_a      -- initialized from (ctx+1396 & 4)
     +0x009  flag_b      -- initialized from (ctx+1396 & 8)
     +0x00C  counter_0   = 0   |
     +0x010  changed_0   = 0   | Sub-pass group 0 (56 bytes)
     +0x018  ...              |
     +0x048  counter_1   = 0   | Sub-pass group 1
     ...
     +0x12C  counter_6   = 0   | Sub-pass group 6
     +0x130  changed_6   = 0   |
     +0x138  ...              |
   ```
5. Calls `sub_8F6FA0` -- the block iterator

The block iterator `sub_8F6FA0` initializes per-context flags from `ctx+1396`:
- Bit 2 (`& 4`): stored at `context+9`, controls whether opcode-7 instructions are processed
- Bit 3 (`& 8`): stored at `context+8`, controls whether opcode-6 (MOV variant) instructions are processed

It then calls `sub_7E6090` to rebuild use-def chains and walks the block list calling `sub_8F6530` per block.

### Variant C: Indirect Vtable Dispatch (Phases 46, 65)

The Mid2 and Late2 variants use indirect vtable dispatch to call their sub-pass bodies, making the exact implementation architecture-dependent:

**Phase 46** (`GeneralOptimizeMid2`) at `0xC60840`:
```asm
mov  rdi, [rsi+0x630]      ; load compilation_unit->field_1584
mov  rax, [rdi]             ; load vtable
mov  rax, [rax+0x1C0]      ; load vtable slot 56 (offset 0x1C0 = 448)
cmp  rax, 0x7D6DD0          ; compare against no-op sentinel
jne  call_it                ; if not sentinel, call it
ret                          ; otherwise, return (phase is no-op)
call_it:
jmp  rax                    ; tail-call the vtable method
```

**Phase 65** (`GeneralOptimizeLate2`) at `sub_C60550`:
```c
// sub_C60550 -- GeneralOptimizeLate2 execute
int64_t GeneralOptimizeLate2(int64_t phase, int64_t ctx) {
    int64_t result = sub_7DDB50(ctx);       // get function count
    if ((int)result > 1) {
        int64_t comp_unit = *(int64_t*)(ctx + 1584);
        return (*(int64_t(**)(int64_t, int64_t))(*(int64_t*)comp_unit + 392))(comp_unit, ctx);
    }
    return result;
}
```

This indirection means the actual optimization behavior for phases 46 and 65 is determined by the compilation unit's vtable, which varies by target architecture and optimization level. The no-op sentinel `sub_7D6DD0` (for phase 46) indicates that some architectures skip this pass entirely.

## Sub-Pass Decomposition

The sub-passes that run inside a GeneralOptimize iteration are not named individually in the binary -- they are inline code within the per-block processing functions. Based on the decompiled logic, the following sub-transformations are identifiable:

### Copy Propagation Algorithm

**String evidence:** `"OriCopyProp"` at `0x21E6CE1` appears in the phase name table at index 22, confirming that copy propagation is a recognized sub-pass within the system.

Two distinct copy propagation algorithms exist across the GeneralOptimize variants:

#### Algorithm A: Chain-Matching Copy Propagation (Phase 13 -- `sub_753600`)

Phase 13's copy propagation operates by matching structurally equivalent instruction pairs connected through single-use def-use chains. The 253-line function `sub_753600` uses a **state structure** (8 `int64_t` fields, allocated on the stack at `rbp-0x88` in `sub_7917F0`) that accumulates matched chain endpoints:

```
sub_753600 State Layout (8 qwords)
  state[0] = ctx           -- Code Object pointer (set by caller)
  state[1] = match_start   -- first matched instruction in chain
  state[2] = match_end     -- last matched instruction in chain
  state[3] = def_entry_a   -- first definition chain entry (from sub_753520)
  state[4] = reg_entry     -- register/BB entry for replacement target
  state[5] = def_entry_b   -- extended chain entry (second level)
  state[6] = reg_entry_b   -- extended register/BB entry
```

The algorithm proceeds in eight steps:

```
// sub_753600 -- Phase 13 copy propagation (decompiled pseudocode)
function copy_prop_early(state, basic_block):
    ctx = state[0]
    first_instr = *(basic_block[1])              // head of instruction list

    // Step 1: Entry gate -- only process blocks starting with EXIT/RET
    if first_instr.opcode != 95: return false    // opcode 95 = EXIT/RET
    if first_instr.operand_count != 5: return false
    format = first_instr[25] & 7
    if format != 3 and format != 4: return false // must be imm or reg source

    // Step 2: Single-use chain check
    use_link = basic_block[17]                   // use-def chain link
    if use_link == NULL: return false
    if *use_link == NULL: return false
    if **use_link != NULL: return false           // must be SINGLE consumer

    // Step 3: Follow to defining instruction via opcode-97 anchor
    next_instr = *(basic_block[1] + 8)           // linked list next
    if next_instr.opcode != 97: return false     // must be def anchor
    reg_entry = *(ctx+296)[ next_instr.bb_index ] // BB/def lookup

    // Step 4: Walk def-use chain to find structural match
    chain_a = follow_chain_filtered(state, reg_entry)  // sub_753520
    if chain_a == NULL: return false
    state[3] = chain_a

    // Step 5: Walk reverse chain from chain_a
    chain_b = follow_reverse_chain(state, chain_a)     // sub_753570
    if chain_b == NULL: return false
    state[1] = chain_b
    state[2] = chain_b

    // Step 6: Structural equivalence check
    endpoint_instr = *(chain_b[1])
    if endpoint_instr.opcode != 95: return false
    if !instruction_equivalent(first_instr, endpoint_instr): return false
                                                       // sub_7E7380

    // Step 7: Operand-level matching
    if operand formats differ (format-4 parity mismatch): return false
    if reg_indices match AND metadata matches AND modifiers match:
        goto apply   // direct match

    // Step 7b: Deep sub-DAG equivalence (for non-trivial patterns)
    if both sources are register type (bits 28-30 == 1)
       and both have use_count <= 1
       and both defining instructions have opcode 119
       and no aliasing hazards (sub_748570)
       and sub_1245740(ctx, def_a, def_b, 2):   // depth-2 DAG compare
        goto apply

    return false

apply:
    // Step 8: Record replacement target
    state[4] = register_entry_for_replacement
    // Optionally follow one more chain level for state[5]/state[6]
    return true   // caller invokes sub_753B50 to rewrite
```

The **chain walker** `sub_753480` (43 lines) is the core of this algorithm. It follows single-use, single-def chains within a basic block:

```
// sub_753480 -- def-use chain walker (at 0x753480)
function follow_chain(ctx, entry, &skip_flag):
    skip_flag = false
    if entry == NULL: return NULL
    current = entry
    loop:
        if check_multi_condition_skip(current):   // sub_7E5120
            skip_flag = true                      // chain crossed a skip point

        if current[16] == NULL: break             // no next-use link
        if *current[16] != NULL: break            // MULTI-USE: stop

        if current[17] == NULL: break             // no def link
        if *current[17] != NULL: break            // MULTI-DEF: stop

        def_bb_idx = *(current[17] + 8)
        instr_bb_idx = *(current[1] + 8).bb_index  // at +24
        if def_bb_idx != instr_bb_idx: break      // CROSS-BB: stop

        next_instr = *(current[1] + 8)
        if next_instr.opcode == 97:               // def anchor
            current = *(ctx+296)[ def_bb_idx ]    // follow chain
            continue
        else:
            return NULL                           // chain broken

    return current                                // last valid entry
```

Key properties of this walker:
- Only follows **single-use** chains (`current[16]` must have exactly one consumer)
- Only follows **single-def** chains (`current[17]` must have exactly one producer)
- Only follows **intra-block** chains (definition and use must share the same BB index)
- Only traverses through **opcode 97** (definition anchor) instructions
- The `check_multi_condition_skip` (`sub_7E5120`, 18 lines) tests four conditions: vtable dispatch at `ctx+1784`, block ordering bounds at `ctx+1776`, instruction flags at `+283` bit 0, and knob 91

The helper `sub_753520` wraps `sub_753480` with an additional opcode-93 gate: the chain endpoint's instruction must have opcode 93 (internal CALL variant) and the use-chain at `entry[16]` must be empty. `sub_753570` performs the reverse direction check, verifying that following the chain backward from a given entry reaches the expected starting point with matching register indices.

#### Algorithm B: Forward Walk with Flag Marking (Phase 29 -- `sub_908EB0`)

Phase 29's copy propagation walks the instruction linked list sequentially from `*(ctx+272)` (instruction list head) and marks eligible operands with flag bits for later consumption. The 217-line function `sub_908EB0` maintains three key state variables:

| Variable | Type | Purpose |
|----------|------|---------|
| `v10` | `bool` | "previous instruction was a recognized copy" -- gates liveness fallback |
| `v11` | `int64_t` | Current definition tracking entry (BB array pointer, from opcode 97) |
| `v21` | `char` | Architecture-allows-predicate-marking flag (from vtable at `**(ctx+1584)+1312`) |

```
// sub_908EB0 -- Phase 29 forward copy propagation (decompiled pseudocode)
function copy_prop_forward(ctx):
    // Gate checks: option 487, option 231, option 461, function count,
    // architecture check via sub_7DC0E0, vtable dispatch at +1312
    v21 = check_arch_predicate_marking(ctx)

    sub_781F80(ctx, 1)         // initialize per-instruction flags
    v10 = initial_gate_flag    // from option 487 check
    v11 = 0                    // no current definition context

    for instr in instruction_list(ctx+272):
        opcode = instr.opcode & ~0x3000          // mask bits 12-13

        switch opcode:
        case 97:   // DEFINITION ANCHOR
            v10 = initial_gate_flag              // reset copy-tracking
            v11 = *(ctx+296)[ instr.operand[0] & 0xFFFFFF ]
            // Updates definition context -- subsequent opcodes 18/124
            // reference v11 for their propagation decisions
            continue

        case 18:   // SET-PREDICATE (FSETP/ISETP)
            if sub_8F2E50(ctx, instr):           // eligible?
                v10 = false                       // suppress liveness check
                if v21:                           // arch supports pred marking?
                    dst_idx = count + ~((opcode>>11) & 2)
                    instr.operand[dst_idx] |= 0x400   // mark: propagated-under-predicate
            continue

        case 124:  // CONDITIONAL SELECT
            if !sub_8F2E50(ctx, instr): continue
            dst = instr.operand[ count + ~((opcode>>11) & 2) ]

            if (ctx+1379) & 7 == 0:              // simple mode
                dst |= 0x100                      // mark: propagated
                continue

            if (dst & 0xF) == 1:                  // integer constant type
                if !sub_8F29C0(ctx): continue     // arch check
                // fall through to direct marking
            else:
                if !sub_8F29C0(ctx) or (ctx+1379 & 0x1B) != 0:
                    // Two-pass predicate simplifier
                    sub_908A60(ctx, v11, instr, 1, &hit, &partial)  // forward
                    if hit: goto mark_propagated
                    if !partial:
                        sub_908A60(ctx, v11, instr, 0, &hit, &partial) // backward
                        if hit: goto mark_propagated
                        if !partial: continue     // no match at all
                // Direct propagation: convert operand type
                dst = (dst & 0xFFFFFDF0) | 0x201  // clear type, set reg+deferred
                continue

            // Liveness-gated propagation check for extended chains
            if !v10 or v21:
                mark_propagated:
                instr.operand[dst_idx] |= 0x100   // mark: propagated
            else:
                // Follow definition chain from v11 for additional candidates
                follow_and_check_chain(ctx, v11, instr)
            continue

        default:
            if !v10:                               // no prior copy recognized
                status = sub_7DF3A0(instr, ctx)   // liveness check
                v10 = (*status & 0xC) != 0        // live uses exist?
            continue
```

#### Target Opcodes in Copy Propagation Context

| Opcode | IR Meaning | Role in Copy Prop | Evidence |
|--------|-----------|-------------------|---------|
| 97 | Definition anchor / label marker | Updates the current definition tracking context (`v11`). Its operand `instr+84 & 0xFFFFFF` is an index into the BB array at `ctx+296`, retrieving the BasicBlock descriptor for the definition point. All subsequent propagation decisions for opcodes 18 and 124 reference this context. | `sub_908EB0` lines 74--78: `v11 = *(*(a1+296) + 8 * (*(v9+84) & 0xFFFFFF))` |
| 18 | `FSETP`/`ISETP` (set predicate) | A predicate-setting comparison instruction. Copy propagation treats it as a "predicated copy" target: when source operands have type 2 or 3 (predicate/uniform register) and pass `sub_91D150` register constraint checks, the destination predicate can be folded into consumers. Marked with `0x400` when the architecture supports it. | `sub_908EB0` lines 84--96, `sub_8F2E50` lines 19--61 |
| 124 | Conditional select (phi-like) | A two-source selection instruction controlled by a predicate. Copy propagation attempts to simplify it to a direct assignment when one source is a constant or when structural analysis shows the predicate is trivially true/false. Marked with `0x100` or type-converted via `(operand & 0xFFFFFDF0) | 0x201`. | `sub_908EB0` lines 82--198, `sub_8F2E50` lines 42--51 |

#### Flag Bit Semantics

The propagation marks operands with three flag bits on the destination operand word at `instr + 84 + 8*dst_idx`:

| Bit | Mask | Name | Set When | Effect |
|-----|------|------|----------|--------|
| 8 | `0x100` | Propagated | Conditional select (opcode 124) is eligible for propagation AND the architecture/mode checks pass | Downstream apply-changes passes replace all uses of this destination with its source operand. Checked as a guard in `sub_8F2E50`: `if (dst & 0x100) return false` prevents double-propagation. |
| 9 | `0x200` | Deferred cleanup | Combined with type-field rewriting: `(operand & 0xFFFFFDF0) | 0x201` | Signals that the operand encoding was converted from immediate/constant format to register format during propagation. The apply-changes pass must update the instruction's encoding to reflect the new operand type. Bit 9 is always set together with type bits `0x1` (register type). |
| 10 | `0x400` | Propagated under predicate | Set-predicate instruction (opcode 18) is eligible AND the architecture flag `v21` is true (vtable dispatch at `**(ctx+1584)+1312` returned non-zero) | Marks a conditional propagation: the destination predicate can be folded into consumers, but only if the guarding predicate is maintained. Distinguished from `0x100` because the propagation is predicate-dependent rather than unconditional. |

#### Eligibility Checker: `sub_8F2E50`

The 64-line function `sub_8F2E50` is the central gatekeeper for both opcodes 18 and 124. Decompiled logic:

```c
// sub_8F2E50 -- copy/fold eligibility (from decompiled code at 0x8F2E50)
function is_eligible(ctx, instr):
    opcode = instr[18] with BYTE1 &= 0xCF       // mask bits 12-13

    if opcode == 18:                              // set-predicate
        dst = instr[2 * (count + ~((opcode>>11)&2)) + 21]
        type_nibble = (dst >> 2) & 0xF
        if type_nibble == 10: return false        // type 10: never foldable
        if type_nibble == 0 and !(dst & 0x400):   // no type bits, not yet marked
            // Architecture-gated source operand check
            vtable_fn = **(ctx+1584) + 1320
            if vtable_fn == sub_7D7240:           // sentinel: direct check
                if (instr[23] >> 28) & 7 not in {2, 3}: return false
            else:
                if vtable_fn() returns true: goto opcode_124_check
            // Register constraint check on both source operands
            if sub_91D150(ctx, instr[23] & 0xFFFFFF): goto opcode_124_check
            if sub_91D150(ctx, instr[25] & 0xFFFFFF): goto opcode_124_check
            return true
        return false

    opcode_124_check:
    if opcode == 124:                             // conditional select
        dst = instr[2 * (count + ~((opcode>>11)&2)) + 21]
        if dst & 0x100: return false              // already propagated
        if dst & 0x70: return false               // has modifier bits
        type = dst & 0xF
        sm_version = *(*(ctx+1584) + 372)
        if (type == 1 or type == 2)               // integer or float
           and (sm_version <= 20479 or !(dst & 0x1C00)):  // SM gate
            return true

    return false
```

The SM version threshold **20479** (`0x4FFF`) divides pre-Turing architectures (where constant propagation through conditional selects is unconditionally safe) from newer architectures that require the constraint bits at `dst & 0x1C00` to be zero.

#### Architecture Predicate Query: `sub_8F29C0`

The 9-line function `sub_8F29C0` at `0x8F29C0` determines whether the compilation unit's target architecture supports predicate-aware copy propagation:

```c
// sub_8F29C0 -- architecture predicate query (decompiled verbatim)
bool check_arch_predicate(int64_t ctx) {
    int64_t comp_unit = *(int64_t*)(ctx + 1584);
    return sub_7DC0E0(comp_unit)          // primary arch capability
        || sub_7DC050(comp_unit)          // secondary arch capability
        || sub_7DC030(comp_unit);         // tertiary arch capability
}
```

This same query is used inside `sub_908A60` (the two-pass predicate simplifier) to initialize the default "safe to transform" flag before instruction-level analysis refines the answer.

#### Two-Pass Predicate Simplifier: `sub_908A60`

When simple eligibility checks pass for opcode 124 but additional predicate analysis is needed (specifically: when `sub_8F29C0` returns false OR `ctx+1379 & 0x1B` has bits set), the two-pass predicate simplifier `sub_908A60` at `0x908A60` is invoked. It takes a direction argument (`1` = forward, `0` = backward) and scans the instruction stream in the specified direction looking for matching definitions:

- **Forward pass** (`a4=1`): Starts from the current definition context `v11`, walks forward through the block's instruction list. For each instruction, dispatches on opcode: 97 updates tracking context, 124/18 checks eligibility via `sub_8F2E50`, others check liveness. Uses a hash-set membership test (`sub_767240`) to avoid visiting the same instruction twice.
- **Backward pass** (`a4=0`): Starts from the definition chain at `v11+136`, walks backward through linked definitions with the same opcode dispatch logic.

The function outputs two flags: `out_a` (full match found -- propagation is safe) and `out_b` (partial match found -- further analysis may help). Phase 29 invokes forward first; if forward finds neither a full nor partial match, it invokes backward. This handles PHI-like merge patterns where the definition chain has both forward paths (normal control flow) and backward paths (loop back-edges).

#### Comparison of Algorithm A vs Algorithm B

| Aspect | Phase 13 (`sub_753600`) | Phase 29 (`sub_908EB0`) |
|--------|------------------------|------------------------|
| Pattern | Chain matching (pair structural equivalence) | Forward walk with flag marking |
| Opcodes handled | 95 (entry gate), 93 (chain gate), 97 (anchor), 119 (deep eq) | 97 (anchor), 18 (pred copy), 124 (cond select) |
| Chain depth | Multi-level (follows through opcode 97 anchors) | Single-level (immediate operand check) |
| Result mechanism | Direct instruction rewriting via `sub_753B50` | Flag marking (`0x100`/`0x200`/`0x400`), consumed later |
| Convergence | Fixed-point loop in `sub_7917F0` (option 464 cap) | Single pass, flags consumed by subsequent iterations |
| Complexity | 253 lines + 5 helper functions | 217 lines + 4 helper functions |
| Scope | Intra-block, single-use chains only | Intra-block, all instructions in sequence |

### Constant Folding Patterns

Constant folding in GeneralOptimize is a two-level mechanism. At the ORI IR level (phases 29 and 37), the fold-eligibility check `sub_8F2E50` at `0x8F2E50` decides which operands can be marked as constant-propagation-eligible. Separately, at the SASS level, the peephole pass `sub_1249B50` performs instruction-combining folds on ALU operations whose sources are both MOV-from-immediate. The ORI-level fold does not evaluate arithmetic at compile time -- it marks operands with flag bits that downstream passes consume to replace registers with immediates.

#### The Eligibility Check: `sub_8F2E50`

The central gatekeeper, called by `sub_908EB0` (phase 29) and `sub_908A60` (predicate simplifier). Returns boolean: 1 = foldable, 0 = not foldable. Two dispatch paths based on the masked opcode at `instr[18] & ~0x3000`:

```c
// sub_8F2E50 -- Fold eligibility check (complete, annotated)
bool is_fold_eligible(int64_t ctx, uint32_t* instr) {
    uint32_t raw = instr[18];
    uint32_t opcode = raw;
    BYTE1(opcode) &= 0xCF;    // clear bits 12-13 (predication variant)

    // --- Path A: opcode 18 (predicated copy) ---
    if (opcode == 18) {
        int dest_idx = instr[20] + ~((raw >> 11) & 2);   // last-operand index
        int dest = instr[2 * dest_idx + 21];
        int type_nibble = (dest >> 2) & 0xF;

        if (type_nibble == 10) return false;   // operand type 10: never foldable

        // Require both type nibble == 0 AND no predicate-propagated flag (0x400)
        if (type_nibble != 0 || (dest & 0x400))
            return false;

        // Vtable dispatch at comp_unit->vtable[1320]:
        //   sentinel sub_7D7240 -> check source operand type bits directly
        //   otherwise -> call virtual method
        fn = *(comp_unit->vtable + 1320);
        if (fn == sub_7D7240) {
            src_type = (instr[23] >> 28) & 7;
            if (src_type - 2 > 1) return false;   // only types 2,3 eligible
        } else {
            if (fn() != 0) goto check_opcode_124;
            src_type = (instr[23] >> 28) & 7;
            if (src_type - 2 > 1) return false;
        }
        // Verify register constraints via sub_91D150 for both sources
        if (sub_91D150(ctx, instr[23] & 0xFFFFFF))
            goto check_opcode_124;
        src2_type = (instr[25] >> 28) & 7;
        if (src2_type - 2 <= 1 && !sub_91D150(ctx, instr[25] & 0xFFFFFF))
            return true;
        // Fall through to opcode 124 check on constraint failure
    }

check_opcode_124:
    // --- Path B: opcode 124 (conditional select / phi-like move) ---
    if (opcode == 124) {
        int dest_idx = instr[20] + ~((raw >> 11) & 2);
        int dest = instr[2 * dest_idx + 21];
        if (dest & 0x100) return false;     // already propagated
        if (dest & 0x70)  return false;     // has modifier bits (neg/abs/sat)

        int type = dest & 0xF;
        int sm_ver = *(int32_t*)(*(int64_t*)(ctx + 1584) + 372);

        if ((type == 1 || type == 2) &&
            (sm_ver <= 20479 || (dest & 0x1C00) == 0))
            return true;
    }
    return false;
}
```

The function `sub_91D150` is a trivial lookup into a per-register constraint array: `return *(uint32_t*)(*(ctx+440) + 4 * reg_index)`. A return value of 0 means the register has no fold-blocking constraint.

#### Fold Eligibility Table

| ORI Opcode | Operation | Foldable? | Conditions | Evidence |
|---|---|---|---|---|
| 18 | Predicated copy | Yes | Source operand types must be 2 (predicate) or 3 (uniform); operand type nibble must be 0; no `0x400` flag; both source registers pass `sub_91D150` constraint check | `sub_8F2E50` lines 17--61 |
| 124 | Conditional select | Yes | Dest type 1 (integer) or 2 (float); no modifier bits (`& 0x70 == 0`); not already propagated (`& 0x100 == 0`); SM-version-dependent constraint check | `sub_8F2E50` lines 42--51 |
| 97 | Register-to-register move | Propagated, not folded | Dest register replaced by source in all uses (copy propagation); no type/SM checks | `sub_908EB0` lines 75--79 |
| 98 | Local load (LDL) | Cost-exempt fold target | In phase 37 only; target symbol looked up from constant bank; foldable if symbol is in constant bank | `sub_90FBA0` lines 261--270 |
| 130 | HSET2 (packed FP16x2 compare) | Cost-exempt | Phase 37 bitmask: opcode in {130,133,134,137} bypasses fold cost penalty | `sub_90FBA0` bitmask `0x99` |
| 133 | (SM-range-dependent ALU) | Cost-exempt | Same bitmask as 130 | `sub_90FBA0` |
| 134 | (SM-range-dependent ALU) | Cost-exempt | Same bitmask as 130 | `sub_90FBA0` |
| 137 | (SM-range-dependent ALU) | Cost-exempt | Same bitmask as 130 | `sub_90FBA0` |
| 272 | Extended instruction | Cost-exempt | Phase 37: excluded from cost penalty alongside 273 | `sub_90FBA0` line 226 |
| 273 | Extended instruction | Cost-exempt | Same as 272 | `sub_90FBA0` |
| Others | General ALU/memory | Not directly foldable | GeneralOptimize does not attempt to fold; deferred to peephole passes | `sub_8F2E50` returns 0 |

**"Cost-exempt"** means that when phase 37 (`GeneralOptimizeMid`) encounters these opcodes, it does not apply the standard cost penalty to the fold decision. Other opcodes accumulate a cost of 1.0 per fold, while these accumulate only `v104` (default 0.25, adjustable via knob 474). Exception: even cost-exempt opcodes pay full cost if `sub_91E860` reports a use count exceeding 4, preventing over-folding of widely-referenced constants.

#### SM Version 20479 Threshold

The value at `*(*(ctx+1584) + 372)` encodes the target SM architecture. The threshold `20479` (0x4FFF) appears in three locations in the binary:

1. `sub_8F2E50` line 47: fold eligibility for opcode 124
2. `sub_908A60` line 107: predicate simplifier's opcode-124 handling
3. Transitively through `sub_8F2E50` calls from `sub_908A60`

The threshold divides two immediate-encoding regimes:

| SM range | Encoded value | Fold rule | Rationale |
|---|---|---|---|
| <= 20479 | Legacy encoding | Integer (type 1) and float (type 2) constants in conditional selects fold unconditionally | Legacy architectures use fixed-width immediate slots with no sign/width constraints |
| > 20479 | Extended encoding | Same types fold only if `(dest & 0x1C00) == 0` -- constraint bits at operand positions 10--12 must all be zero | Extended architectures introduced variable-width immediate encoding with sign-extension rules; bits 10--12 encode width/signedness constraints that prevent certain constants from being represented as immediates |

The exact mapping from SM architecture numbers (e.g., sm_50, sm_75, sm_86) to the encoded value at `comp_unit+372` is not a simple identity -- an architecture-dependent multiplier is applied during target initialization. Based on the threshold position, the boundary falls approximately between the sm_80 (Ampere) and sm_86 (Ampere+) generations, aligning with the introduction of extended immediate formats in the Ampere refresh.

#### How Fold Results Are Consumed

When `sub_8F2E50` returns true in `sub_908EB0` (phase 29), the action depends on the opcode:

**Opcode 18 (predicated copy)**: If the `v21` flag is set (vtable dispatch at `comp_unit+1312` returned non-zero, confirming the target supports this transformation), the destination operand is marked with `0x400` (propagated-under-predicate). No immediate evaluation occurs.

**Opcode 124 (conditional select)**: A tier flag at `ctx+1379` controls aggressiveness:

```c
// In sub_908EB0, after sub_8F2E50 returns true for opcode 124:
int tier = *(uint8_t*)(ctx + 1379) & 7;
if (tier == 0) {
    // AGGRESSIVE: mark dest byte-1 |= 1 (fold-committed, fast path)
    dest_operand[1] |= 1;
} else {
    // CONSERVATIVE: type-dispatched analysis required
    if ((dest & 0xF) == 1) {              // integer immediate
        if (sub_8F29C0(ctx))              // predicate analysis passes
            dest = (dest & 0xFFFFFDF0) | 0x201;  // clear type, set propagated+eligible
    } else {                              // float or other
        if (!sub_8F29C0(ctx) || (*(ctx+1379) & 0x1B) != 0) {
            // Two-pass predicate simplifier (forward, then backward)
            sub_908A60(ctx, reg, instr, 1, &out_a, &out_b);  // forward
            if (!out_a && !out_b[0])
                sub_908A60(ctx, reg, instr, 0, &out_a, &out_b);  // backward
        }
        dest = (dest & 0xFFFFFDF0) | 0x201;  // set propagated+eligible
    }
}
```

The tier value at `ctx+1379 & 7` distinguishes:
- **0** = aggressive fold (unconditional fast path, no predicate analysis)
- **1--7** = conservative fold (requires `sub_8F29C0` predicate analysis and potentially `sub_908A60` two-pass simplification)

The actual constant value is not computed during GeneralOptimize. The fold marks operands with flag bits (`0x100`, `0x200`, `0x400`, byte-1 `|= 1`) that downstream passes consume: the apply-changes function `sub_753B50` rewrites instruction lists, and the peephole/codegen passes emit the actual immediates.

#### The `limit-fold-fp` Knob

| | |
|---|---|
| **String** | `"limit-fold-fp"` at `0x1CE3D23` |
| **Help text** | `"Enable/disable constant folding of float operations."` at `0x1CE63B0` |
| **Type** | Boolean |
| **Default** | `"false"` (FP folding is NOT limited -- folding is enabled) |
| **Config offset** | `config + 340` (registered at `sub_434320` line 268) |
| **Category** | Optimization control (registration category 4) |
| **Visibility** | Internal (not exposed on public CLI) |

Despite the name, `limit-fold-fp` follows the convention that `limit-X = true` means *restrict/disable X*. When set to `true`:

1. The `config+340` byte propagates into per-function context flags at `ctx+1379` during compilation context setup
2. The `ctx+1379 & 7` tier value becomes non-zero, forcing all type-2 (float) operands through the conservative fold path
3. Conservative fold requires predicate analysis via `sub_8F29C0` and potentially the two-pass `sub_908A60` simplifier, which rejects folds where predicate conditions are ambiguous
4. This prevents FP constants from being folded when the fold could alter precision semantics -- for example, folding an FMA source operand might lose the fused multiply-add precision guarantee that the original instruction provided

The predicate analysis helper `sub_8F29C0` (11 lines) performs three sequential checks on the compilation unit at `ctx+1584`: `sub_7DC0E0`, `sub_7DC050`, and `sub_7DC030`. If any returns true, the predicate allows safe propagation. These check architecture capability flags for predicated constant operations.

#### Phase 37 Fold Cost Model

`sub_90FBA0` (the main loop for `GeneralOptimizeMid`) integrates fold decisions into a cost-weighted convergence model rather than a simple boolean. Key elements:

**Opcode classification** (lines 226--228 of the decompiled output): a bitmask `0x99` applied to the range 130--137 classifies opcodes as cost-exempt. The expression `~(0x99 >> ((uint8_t)opcode + 126)) & v15` clears `v15` (the cost flag) for opcodes where the corresponding bit in `0x99` is set. Combined with the range check for 272--273:

| Cost-exempt opcodes | Bitmask bit | Interpretation |
|---|---|---|
| 130 | bit 0 of 0x99 | FP16x2 compare (HSET2 family) |
| 133 | bit 3 of 0x99 | SM-range-dependent ALU |
| 134 | bit 4 of 0x99 | SM-range-dependent ALU |
| 137 | bit 7 of 0x99 | SM-range-dependent ALU |
| 272, 273 | Direct check | Extended load/store variants |

**Cost computation**: For cost-exempt opcodes, fold cost = `v104 * v35` where `v104` defaults to 0.25 (overridable via knob 474) and `v35` is 0.0 if the instruction is dead (checked via `sub_7DF3A0`), 1.0 otherwise. For non-exempt opcodes, fold cost = `1.0 * v35` (full weight).

**Use-count gate**: Even cost-exempt opcodes pay full cost if `sub_91E860` (use-count estimator) reports more than 4 uses, preventing over-folding of widely-referenced constants.

**Convergence**: Accumulated costs at `context+26` (weighted) and `context+27` (unweighted) are doubles. The loop continues until the cost delta falls below the threshold (default 0.25 from knob 474; overridable by knob 135 when `*(config+9720)` is set).

#### Register Constraint Validation: `sub_8F3FE0`

Phase 37 uses `sub_8F3FE0` to validate that folding an instruction's operands respects register-class constraints. The function:

1. Queries `comp_unit->vtable[904]` for the per-element operand size of the instruction
2. Queries `comp_unit->vtable[936]` (if not sentinel `sub_7D7040`) for per-instruction fold metadata
3. Iterates over all source operands:
   - Requires operand type bits `(>> 28) & 7` to be 2 or 3 (predicate or uniform register)
   - Calls `sub_91D150` to look up the register constraint for each source operand
   - Compares against a previously cached constraint at `context + 7` (8-byte stride per fold group)
   - Returns 0 (fold invalid) on any constraint mismatch
4. Loop count is determined by the destination operand format field (`& 7`)
5. Returns 1 only if all source operands have consistent register constraints

### Algebraic Simplification and Structural Equivalence

The algebraic simplifier in GeneralOptimize is **not** a traditional constant-identity pattern matcher. It does not check operand values against constants (0, 1, -1) to recognize identities like `x+0` or `x*1`. Instead, it is a **structural equivalence-based pattern recognizer** that detects when two instructions in a def-use chain compute identical values, enabling one to be eliminated. Traditional algebraic identity patterns (`x+0->x`, `x*1->x`, `x&0->0`, `x-x->0`, etc.) are handled by the separate [MainPeepholeOptimizer](../codegen/peephole.md) -- see the [comparison table](#algebraic-pattern-location-map) below.

The simplifier lives in `sub_753600` (Phase 13, `GeneralOptimizeEarly`) and is approximately 253 lines of decompiled code. It operates on chains of instructions linked through def-use relationships.

#### Entry Guard

The function only triggers on instructions matching a narrow pattern:

```c
// sub_753600 entry guard
if (instr[18] == 95           // opcode 95 (EXIT/RET-class with guard predicate)
    && instr[20] == 5         // exactly 5 operands
    && (instr[25] & 7) - 3 <= 1)  // operand format 3 (register) or 4 (immediate)
{
    // proceed to chain walk
}
```

The restriction to opcode 95 means this simplifier targets conditional exit/return sequences where a guard predicate or condition is computed redundantly. The 5-operand constraint ensures the instruction has the expected layout: result, predicate, and three source operands.

#### Chain-Walking Algorithm

After the entry guard passes, `sub_753600` executes a 9-step algorithm:

**Step 1 -- Def-chain traversal.** Reads the use-list pointer at `instr[17]` (offset 136). Checks that the use-list head exists, points to a single definition (head's first element is null), and that the next instruction in the chain has opcode 97 (label/scheduling copy).

**Step 2 -- Register resolution.** Follows the register index through the register table at `ctx+296` to resolve the first chain link to a concrete register entry. Both chain paths (via `instr[17]+8` field, "use-list index", and via the register table) must point to the same entry.

**Step 3 -- First pair detection** via `sub_753520`. This helper calls `sub_753480` to walk the single-def chain forward, looking for an instruction with opcode 93 (CALL/branch encoded). At each step, `sub_753480` checks:
- `sub_7E5120` -- is the current entry eligible for chain-following? (checks constant bank membership, block region flags, and opcode 91 via `sub_7A1A90`)
- The use-list pointer at `entry[16]` has a null head (single use)
- The use-list pointer at `entry[17]` has a null head (single def)
- The register index at `entry[17]+8` matches the next instruction's register at `entry[1]+8 -> +24`

**Step 4 -- Second pair detection** via `sub_753570`. Starting from the first pair's result, follows the chain one more step looking for a second opcode-93 instruction that references back to the same register as the first pair's target.

**Step 5 -- Instruction equivalence check** via `sub_7E7380`:

```c
// sub_7E7380 -- structural instruction equivalence
bool instruction_equivalent(Instr* a, Instr* b) {
    bool a_has_pred = (a->opcode & 0x1000) != 0;  // bit 12: predicated
    bool b_has_pred = (b->opcode & 0x1000) != 0;
    if (a_has_pred != b_has_pred)
        return false;
    if (a_has_pred && b_has_pred) {
        // Compare last operand (predicate register): 24-bit register index
        int a_idx = a->operands[a->operand_count - 1] & 0xFFFFFF;
        int b_idx = b->operands[b->operand_count - 1] & 0xFFFFFF;
        if (a_idx != b_idx)  return false;
        // Compare preceding operand pair (full 64-bit equality)
        return a->operands[a->operand_count - 2] == b->operands[b->operand_count - 2];
    }
    return true;  // both unpredicated: structurally equivalent at this level
}
```

This confirms the two instructions have matching predication structure -- same predicate register, same predicate condition encoding.

**Step 6 -- Operand format classification.** Computes the effective operand position as `operand_count - ((opcode >> 11) & 2)` and checks whether it equals 5. When it does, reads the format code at `instr[25] & 7`. Format 3 means register operand, format 4 means immediate. Both instructions must have the same format classification (both register or both immediate).

**Step 7 -- Register index equality.** Compares the 24-bit register index: `(instr_a[v23+21] & 0xFFFFFF) == (instr_b[v24+21] & 0xFFFFFF)`. When equal and the full operand descriptors at `instr[23]` and `instr[24]` also match, the instructions provably compute the same value. The function jumps to the success path.

**Step 8 -- Modifier verification** via `sub_747F40` and `sub_747F80`:

```c
// sub_747F40 -- negation flag extraction
int get_negation(Instr* instr) {
    int eff = instr->operand_count - ((instr->opcode >> 11) & 2);
    if (eff == 5 && (instr->data[25] & 7) - 3 < 2)
        return (instr->data[25] >> 3) & 1;   // bit 3 of format byte
    return 0;
}

// sub_747F80 -- absolute-value flag extraction
int get_abs(Instr* instr) {
    int eff = instr->operand_count - ((instr->opcode >> 11) & 2);
    if (eff == 5 && (instr->data[25] & 7) - 3 < 2)
        return (instr->data[25] >> 4) & 1;   // bit 4 of format byte
    return 0;
}
```

Both instructions must have identical negation and absolute-value flags. If `neg(a) != neg(b)` or `abs(a) != abs(b)`, the pattern is rejected. This prevents incorrectly identifying `neg(x)` as equivalent to `x`.

**Step 9 -- Deep sub-DAG equivalence.** When register indices differ but operand type bits (bits 28-30) equal 1 (register type), the simplifier follows the definition chain to the defining instruction and attempts structural matching at depth:

```c
// Deep equivalence path (sub_753600, lines 149-189)
if (((operand_a >> 28) & 7) == 1) {           // register-type operand
    RegEntry* reg_a = reg_table[operand_a & 0xFFFFFF];
    if (reg_a->use_count_field == 5) {         // field at +64
        Instr* def_a = reg_a->defining_instr;  // field at +56
        // ...same for operand_b...
        if (def_a->opcode == 119 && def_b->opcode == 119) {  // both SHFL
            int res_a = def_a->operands[2 * def_a->operand_count + 19];
            int res_b = def_b->operands[2 * def_b->operand_count + 19];
            if ((res_a & 1) == 0 && (res_b & 1) == 0        // bit 0 clear
                && ((res_a | res_b) & 8) == 0                // bit 3 clear
                && !sub_748570(def_a, ctx)                   // no alias hazard
                && !sub_748570(def_b, ctx)                   // no alias hazard
                && def_a->data[25] == def_b->data[25]        // format match
                && def_a->data[26] == def_b->data[26]        // descriptor match
                && sub_1245740(ctx, def_a, def_b, 2))        // depth-2 DAG eq
            {
                // Match found -- proceed to chain extension
            }
        }
    }
}
```

The depth limit of 2 (fourth argument to `sub_1245740`) prevents exponential blowup in the equivalence check while still catching common patterns like `f(g(x)) == f(g(y))` when `x == y`.

#### Chain Extension and Accumulation

After finding one matching pair, the function extends the search down the chain. It calls `sub_753520` and `sub_753570` on subsequent entries, accumulating the full matching sequence in the state array at `a1[1]` through `a1[6]`. The state layout is:

```
State array (passed as a1, 7 qword slots):
  a1[0] = ctx (compilation context)
  a1[1] = first matched instruction (start of sequence)
  a1[2] = second matched instruction (end of first pair)
  a1[3] = third matched instruction (from sub_753520)
  a1[4] = fourth instruction (next link)
  a1[5] = fifth instruction (from secondary sub_753520)
  a1[6] = sixth instruction (final chain link)
```

The function returns `true` (changed) when the full chain is successfully matched. The caller (`sub_7917F0`) then invokes `sub_753B50` to rewrite the matched sequence.

#### What This Actually Eliminates

The pattern this simplifier catches is: a sequence of conditional exit instructions where the guard predicates, condition codes, and source operands are structurally equivalent. In practice, this arises from lowering transformations that produce redundant conditional exit/return pairs -- for example, when a function has multiple return paths that were not merged during earlier optimization, or when predicated code duplication creates exit sequences with identical conditions.

The rewrite performed by `sub_753B50` replaces the redundant chain with a single exit/return sequence, updating the block's instruction list, register-to-instruction mappings, and def-use chains.

#### Algebraic Pattern Location Map

The following table clarifies which optimization pass handles each category of algebraic simplification:

| Pattern Category | Pass | Location | Evidence |
|---|---|---|---|
| Structural equivalence (identical computation chains) | GeneralOptimize Phase 13 | `sub_753600` | CERTAIN -- decompiled |
| Modifier canonicalization (neg/abs flag matching) | GeneralOptimize Phase 13 | `sub_747F40`, `sub_747F80` | CERTAIN -- decompiled |
| Sub-DAG equivalence (depth-limited tree comparison) | GeneralOptimize Phase 13 | `sub_1245740` | CERTAIN -- decompiled |
| Copy propagation (reg-reg, predicated, conditional) | GeneralOptimize Phase 29 | `sub_908EB0` | CERTAIN -- decompiled |
| Predicate simplification (constant predicates) | GeneralOptimize Phase 29 | `sub_908A60` | CERTAIN -- decompiled |
| Register promotion (memory-to-register conversion) | GeneralOptimize Phase 37 | `sub_90EF70` | CERTAIN -- decompiled |
| Identity: `x+0->x`, `x*1->x`, `x&(-1)->x`, `x\|0->x`, `x^0->x` | MainPeepholeOptimizer | `sub_169B190` et al. | HIGH -- 3,185 pattern matchers |
| Annihilator: `x*0->0`, `x&0->0` | MainPeepholeOptimizer | `sub_169B190` et al. | HIGH -- 3,185 pattern matchers |
| Inverse: `x-x->0`, `x^x->0`, `!!x->x` | MainPeepholeOptimizer | `sub_169B190` et al. | HIGH -- 3,185 pattern matchers |
| Strength reduction: `x*2->x<<1`, `x/1->x` | StrengthReduction (phase 26) | documented separately | CERTAIN -- separate pass |
| Predicate identity: `p&true->p`, `p\|false->p` | MainPeepholeOptimizer + Phase 29 | combined | MEDIUM |

The MainPeepholeOptimizer operates on the full SASS opcode set via three 233-280 KB dispatch functions with 373-case primary switches. Its pattern tables encode the constant-identity rules (IADD3 with zero source becomes MOV, IMAD with unit multiplier becomes shift/add, LOP3 with identity LUT becomes passthrough, etc.) as prioritized rewrite rules. See [Peephole Optimization](../codegen/peephole.md) for full details.

#### Helper Functions: `sub_753E30` and `sub_753F70`

Two additional helpers extend the Phase 13 algebraic simplifier beyond the main `sub_753600` path:

**`sub_753E30`** (67 lines) -- secondary chain matcher that handles the case where the first instruction in the chain has a source register index (`instr[25] & 0xFFFFFF`) that differs from the current block's register at `*(a2 + 24)`. It follows a more complex chain topology involving three register entries (at state slots `a1[7]`, `a1[8]`, `a1[9]`) and validates that the secondary chain loops back to the primary entry. This catches equivalences across register renaming boundaries.

**`sub_753F70`** (49 lines) -- vtable-dispatched transformation that performs the actual rewrite for chains detected by `sub_753E30`. It calls through `comp_unit->vtable[656]` (with sentinel check against `sub_744F30`). When the vtable method returns true, it constructs opcode-93 replacement instructions via `sub_92E1B0` and splices the old chain out via `sub_91E310`. This is the surgical rewrite counterpart to `sub_753B50`'s rewrite for the main path.

**`sub_753DB0`** (33 lines) -- chain tail finder that walks from a given register entry forward through the def-chain, following opcode-97 links via the register table. Returns the last reachable entry in the chain (the "tail") or the entry one step before a broken link. Used by the extended chain detection logic to determine where the equivalence region ends.

### Dead Code Elimination

DCE within GeneralOptimize is lightweight compared to the standalone `OriPerformLiveDead` passes (phases 16, 33, 61, 84). It operates locally within basic blocks using the `sub_7DF3A0` function:

```c
// sub_7DF3A0 -- instruction liveness check
//   Returns pointer to status word
//   Bits 2-3 (mask 0xC): has live uses
//   Bit 0 (mask 0x1): marked dead
int8_t* check_liveness(int64_t instr, int64_t* ctx) {
    // ... examines use-def chains ...
    return status_ptr;   // caller checks (*result & 0xC) != 0
}
```

In `sub_908EB0`, the DCE check appears as the fallback for unrecognized opcodes:

```c
if (!v10) {   // v10 = "previous instruction was a recognized copy"
    int8_t* status = sub_7DF3A0(instr, ctx);
    v10 = (*status & 0xC) != 0;   // live uses exist?
}
```

When `(*status & 0xC) == 0`, the instruction has no live consumers and is effectively dead. In Variant A, dead instructions are not immediately deleted -- they are marked for removal by the convergence loop cleanup phase (`sub_753B50`), which rewires the instruction list to skip dead nodes and updates the block's def-use chains via `sub_931920`, `sub_932E80`, `sub_749090`, and `sub_9253C0`.

In Variant B (phase 58), `sub_8F6530` uses the same `sub_7DF3A0` liveness check but integrates the result into its 7-counter change tracking structure, incrementing the appropriate sub-pass counter when a dead instruction is found.

### Predicate Simplification

A distinct sub-pass handles predicate register operations. The code in `sub_908EB0` at the opcode-18 and opcode-124 branches processes predicated moves and conditional selects:

- **Opcode 18** (predicated move): if the predicate is known-true (from prior constant folding), simplifies to unconditional move. If the `v21` flag is set (indicating the vtable dispatch at `comp_unit+1312` returned non-zero, i.e. the target supports this transformation), marks the destination operand with `0x400`
- **Opcode 124** (conditional select): if both source operands are identical (detected via def-chain comparison), simplifies to an unconditional copy; if the predicate is constant, selects the appropriate source. The two-pass approach via `sub_908A60` handles phi-like patterns where direction matters:
  - Pass 1: `sub_908A60(ctx, reg_entry, instr, 1, &out_a, &out_b)` -- forward direction
  - Pass 2 (if pass 1 found no simplification but detected a partial match): `sub_908A60(ctx, reg_entry, instr, 0, &out_a, &out_b)` -- backward direction

The helper `sub_8F29C0` at `0x8F29C0` performs predicate-specific analysis, determining whether the predicate condition allows safe propagation given the current instruction context.

## The Per-Block Sub-Pass Runner: `sub_8F6530` (Variant B Detail)

The 550-line function `sub_8F6530` is the core of Variant B (phase 58). It processes a single basic block using a **6-slot circular buffer** of instruction pairs, tracked at 56-byte intervals:

```
sub_8F6530 Context (passed as a1)
  +0x000  ctx_ptr                 -- compilation context
  +0x008  flag_ctrl_flow_4        -- from ctx+1396 bit 2 (opcode-7 enable)
  +0x009  flag_ctrl_flow_8        -- from ctx+1396 bit 3 (opcode-6 enable)
  +0x00C  slot_index              -- current slot (modulo 6)
  +0x010  slot_0_changed          -- boolean: did this slot's pair fire?
  +0x014  slot_0_count            -- how many pairs stored in this slot

  Slot layout (each 56 bytes = 7 int64_t):
    +0x00  count/used flag
    +0x04  changed flag
    +0x08  instr_ptr_a            -- first instruction of the pair
    +0x10  instr_ptr_b            -- second instruction of the pair
    +0x18  (reserved)
    ...

  6 slots at offsets: +0x10, +0x48, +0x80, +0xB8, +0xF0, +0x128
```

The slot index increments with `(*(a1+3) + 1) % 6` after each pair is processed. When a new instruction pair is encountered that doesn't match any existing slot, the oldest slot is evicted (slot index advances). Each slot can hold up to 2 instruction pointers.

The function walks the instruction list looking for specific opcode patterns:

1. **Opcodes 139 and 110** (MOV variants with different addressing modes): these are the primary targets. The function checks operand field at `instr+76` for value 6 (register operand) or 7 (immediate operand), with the `flag_ctrl_flow_4` and `flag_ctrl_flow_8` gates controlling which variants are processed
2. For register operands (type field bits 28-30 == 1), it verifies:
   - Use count == 1 (`*(reginfo+24) == 1`)
   - No aliasing flags (`*(reginfo+50) & 1 == 0`)
   - Register class not in range 2-8 (`*(reginfo+20) - 2 > 6`)
3. For instructions with opcode 139 and no modifier bits (`*(instr+88) & 0x603FFFF == 0`), the function attempts to find the instruction in the circular buffer and either promote it (if found) or insert it as a new entry
4. **Option 605** (`getOption(ctx, 605)`) at `0x8F6530+0x1A0`: when enabled, restricts the matching to only instructions already present in the buffer, preventing new insertions. This is an architecture-gated optimization

## Fixed-Point Convergence

### Per-Block Iteration Model

All GeneralOptimize variants use a per-block convergence model: they iterate over basic blocks in linear order (following the block ordering table at `ctx+512`), and for each block, run the sub-passes repeatedly until convergence. This differs from the global worklist model used by other optimizers (GVN-CSE at phase 49 uses a global worklist).

```
for each block B in reverse postorder:
    repeat:
        changed = run_sub_passes(B)
    until !changed OR !getOption(464)
```

The block ordering table is an array of `int32_t` indices at `*(ctx+512)`, with the count at `*(ctx+520)`. Block iteration starts at index 1 (not 0) and proceeds through `bb_count` inclusive. Each index is used to look up the actual basic block pointer via `*(*(ctx+296) + 8 * block_order[i])`.

### Change Detection Mechanism

Changes are detected through different protocols depending on the variant:

- **Variant A** (`sub_753600`): returns a boolean. The return value is the logical OR of all sub-pass fire events. The state machine in `sub_7917F0` stores the result in `v15` (mapped to register `bp`) and accumulates across iterations via `v4 = v15`
- **Variant B, phase 58** (`sub_8F6530`): maintains 7 independent counters at 56-byte intervals in the context structure. Counters are at `*(a1 + 5)`, `*(a1 + 19)`, `*(a1 + 33)`, `*(a1 + 47)`, `*(a1 + 61)`, `*(a1 + 75)`. The corresponding boolean changed-flags are at `*(a1 + 16)`, `*(a1 + 72)`, `*(a1 + 128)`, `*(a1 + 184)`, `*(a1 + 240)`, `*(a1 + 296)`. All are zero-initialized at entry. The caller checks if any counter is non-zero to determine convergence
- **Variant B, phase 37** (`sub_90FBA0`): uses a different approach -- tracks a floating-point "cost" accumulator at `context+25/26/27` (three `double` values representing total cost, weighted cost, and instruction count). Convergence is determined when the cost delta falls below a threshold (initialized to 0.25, adjustable via knob 474 at `0x90FBA0+0x50`). Knob 135 at `0x90FBA0+0x20` controls an initial threshold override when enabled (checked via `*(config+9720)`)

### Iteration Limits

The fixed-point loop is guarded by option 464 in Variant A. In `sub_7917F0`:

```c
while (true) {
    bool changed = sub_753600(&state, bb);
    if (!changed) break;

    // Option 464 check -- same vtable fast-path pattern:
    //   vtable[152] == sub_67EB60  =>  sub_7468B0(config, 464)
    //   otherwise                  =>  vtable[152](config, 464, 1)
    if (!getOption_v2(ctx, 464)) break;

    sub_753B50(&state);   // apply rewrites before re-scanning
}
```

The option 464 check is called after each successful iteration (when `changed == true`). If the option returns false, the loop terminates even though more changes could be made. The exact semantics of option 464 depend on the knob's implementation -- it could be a simple counter that decrements, a boolean that gets cleared after N iterations, or a cost-based threshold. The default behavior (when option 464 always returns true) allows unbounded iteration until convergence.

Variant B (phases 37 and 58) does **not** use option 464 for iteration control. Phase 37 uses the cost-based threshold described above. Phase 58 makes a single pass over the block list via `sub_8F6FA0`, which does not loop -- each block is visited exactly once, with the 6-slot circular buffer providing limited lookback within the walk.

In practice, most basic blocks converge in 1--3 iterations. A block that generates new optimization opportunities typically does so because copy propagation exposes a constant, which enables constant folding, which creates a dead instruction. The second iteration catches any cascading effects, and the third confirms convergence. Blocks requiring more than 3 iterations are rare and typically involve chains of dependent copies or nested predicate simplifications.

## The Apply-Changes Function: `sub_753B50`

After `sub_753600` reports changes, `sub_753B50` applies the accumulated transformations. This is a compact 70-line function that performs instruction-list surgery:

1. **Creates a replacement instruction** via `sub_931920(ctx, state->instr_pair, *(*(state->instr_pair+8)+8), -1)` -- the `-1` argument (`0xFFFFFFFF`) signals "allocate new"
2. **Updates the block's instruction head** at `*(ctx+232)` with the new instruction's head pointer
3. **Clears the block's instruction count** at `*(ctx+264) = 0`
4. **Calls `sub_932E80`** to relink the instruction into the block's doubly-linked list
5. **Propagates flags**: if the original instruction had flag bit 3 of `*(instr+280)` set (indicating a control-flow-sensitive instruction), the replacement inherits it via `new_instr[70] |= 8`
6. **Walks the state's instruction chain** (from `state[1]` through `state[2]`), creating replacements for each and calling `sub_749090` to update register-to-instruction mappings
7. **Final cleanup**: calls `sub_9253C0` to remove the dead instructions from their blocks, and `sub_749290` to update the register numbering, and `sub_91E310` to splice the old instruction range out of the linked list

## Differences Between Early/Mid/Late Variants

### 1. Gate Conditions (Who Runs)

| Phase | Gate Logic |
|---|---|
| 13 (Early) | Requires `ctx->flags_1382 & 4`; skips if option 214 is set; requires option 487; skips if `*(*(ctx)+1056)` is non-null |
| 29 | Requires option 487; skips if option 231 (dump mode) is set; requires `*(config+33192)` check or option 461 pass; skips if function count == 1 |
| 37 (Mid) | Requires `sub_8F3EA0` pre-check; option 487; can be disabled via `--no-phase ConvertMemoryToRegisterOrUniform`; skips if function count == 1 |
| 46 (Mid2) | Indirect dispatch; skips if vtable slot `[0x1C0]` points to no-op sentinel `sub_7D6DD0` |
| 58 (Late) | Requires function count > 2 (not just > 1); checks optimization level bits `(ctx+1396 & 0x30) != 0x20`; checks option 31 with extended-value semantics |
| 65 (Late2) | Requires function count > 1; indirect dispatch through compilation unit vtable slot at offset 392 |

### 2. Sub-Pass Selection (What Runs)

| Phase | Sub-Passes Included |
|---|---|
| 13 (Early) | Structural equivalence detection via `sub_753600` (def-use chain walking, instruction pair matching, modifier verification, depth-2 sub-DAG comparison via `sub_1245740`), instruction rewrite via `sub_753B50`. No instruction-level constant folding. Lightweight -- designed for quick cleanup after initial lowering. |
| 29 | Copy prop with full opcode dispatch (97, 18, 124), predicate-aware propagation via `sub_8F2E50`/`sub_8F29C0`, two-pass predicate simplification via `sub_908A60`, liveness-gated DCE via `sub_7DF3A0`. Flag marking with `0x100`/`0x200`/`0x400` bits. |
| 37 (Mid) | Full sub-pass suite plus `ConvertMemoryToRegisterOrUniform` (memory-to-register promotion). Bitvector-based change tracking. Cost-driven convergence with configurable threshold (default 0.25, knob 474). Most comprehensive instance. |
| 46 (Mid2) | Architecture-dependent (vtable dispatch). May include additional target-specific simplifications. |
| 58 (Late) | 6-slot circular buffer pattern matching over MOV/copy instructions (opcodes 139, 110). Register use-count and aliasing checks. Option-605-gated restriction mode. Per-block single-pass (no iteration). |
| 65 (Late2) | Architecture-dependent (vtable dispatch). Final cleanup before register allocation. |

### 3. Infrastructure Weight (How It Runs)

| Phase | Context Size | Tracking | Complexity |
|---|---|---|---|
| 13 (Early) | Minimal (0x88 bytes on stack) | Boolean changed flag | Low (78 lines in `sub_7917F0`) |
| 29 | Stack frame (~0x60 bytes) | Boolean + instruction flag bits | Medium (218 lines in `sub_908EB0`) |
| 37 (Mid) | 0x408-byte stack context + heap bitvectors | Cost-based convergence (3 doubles) + bitvector arrays | High (500+ lines in setup + 400+ in loop) |
| 46 (Mid2) | Vtable-dependent | Vtable-dependent | Variable |
| 58 (Late) | 0x168-byte stack context | 7 counters at 56-byte stride + 6-slot circular buffer | Medium-high (550 lines in `sub_8F6530`) |
| 65 (Late2) | Vtable-dependent | Vtable-dependent | Variable |

## Initialization Infrastructure

Two large helper functions set up the state required before the sub-passes can run:

### `sub_785E20` -- Change Tracking Reset

Called at the start of phase 13 and after the convergence loop completes (if any changes were made). Resets per-block change flags and instruction state. Takes `(ctx, 0)` -- the second argument selects the reset mode.

### `sub_781F80` -- Instruction Flag Initialization

A large function (~1800 lines) that walks every instruction in every basic block, setting per-instruction optimization flags. Called with argument 1 to enable full initialization. These flags control which instructions are eligible for the sub-passes: instructions marked with certain flag patterns are skipped by copy prop, others are skipped by the algebraic simplifier.

### `sub_7E6090` -- Use-Def Chain Builder

Builds operand use-def chains for copy propagation. Called with `(ctx, 0, 0, 0, 0)` at the start of phases 13 and 58. The zero arguments indicate "build from scratch" rather than incremental update.

### `sub_7E6AD0` -- Def-Use Link Builder

Builds bidirectional def-use/use-def links. Called only by phase 13 (Variant A). Variant B phases use their own bitvector-based tracking instead.

### `sub_905B50` -- Bitvector Infrastructure (Phase 37 Only)

A 500+ line setup function specific to `GeneralOptimizeMid`. Allocates and initializes three major bitvector structures for tracking:
1. Register definition reach (which definitions reach each block entry)
2. Per-register liveness within basic blocks
3. Fold eligibility tracking (which operands have known-constant sources)

These bitvectors are destroyed by RAII-style cleanup after `sub_90FBA0` returns, using vtable destructors at offsets `+32` in the bitvector vtables.

## Pipeline Positioning

The six instances are positioned to clean up after specific groups of transformations:

```
Phase 0-12:  Initial setup, FP16 promotion, unsupported op conversion
  --> Phase 13: GeneralOptimizeEarly  (clean up after lowering artifacts)

Phase 14-28: Branch opt, loop passes, strength reduction, pipelining
  --> Phase 29: GeneralOptimize       (clean up after loop transformations)

Phase 30-36: Switch opt, linear replacement, LICM
  --> Phase 37: GeneralOptimizeMid    (heavy cleanup + mem-to-reg promotion)

Phase 38-45: Nested branch opt, CTA expansion, mbarrier, mid expansion
  --> Phase 46: GeneralOptimizeMid2   (clean up after mid-level expansion)

Phase 47-57: GVN-CSE, reassociation, remat, late expansion, speculative hoist
  --> Phase 58: GeneralOptimizeLate   (clean up after late expansion)

Phase 59-64: Loop fusion, predication, late commoning
  --> Phase 65: GeneralOptimizeLate2  (final cleanup before register work)
```

After phase 65, the pipeline transitions to register-attribute setting (phase 90), synchronization (phase 99), and register allocation (phase 101). No GeneralOptimize instance runs after register allocation -- the post-RA pipeline uses different peephole mechanisms.

## Knobs and Options

| Option | Name/Description | Used By |
|---|---|---|
| 214 | Skip `GeneralOptimizeEarly` when set | Phase 13 only |
| 231 | Dump mode -- skip `GeneralOptimize` when set | Phase 29 only |
| 31 | Architecture-dependent fold eligibility gate | Phase 58; extended-value semantics |
| 135 | Threshold override for cost-based convergence | Phase 37; checked via `*(config+9720)` |
| 461 | Secondary gate for phase 29 | Phase 29; passed through `sub_661470` |
| 464 | **Iteration cap** -- breaks fixed-point loop when returns false | Phase 13, and by extension any Variant A user |
| 474 | Cost convergence threshold (float, default 0.25) | Phase 37 (`sub_90FBA0`) |
| 487 | **General optimization enable** -- master switch for all GeneralOptimize passes | Phases 13, 29, 37 |
| 605 | Restrict circular buffer matching to existing entries only | Phase 58 (`sub_8F6530`) |
| `limit-fold-fp` | `"Enable/disable constant folding of float operations"` (default: `"false"`, config+340, boolean, internal) | When `true`, forces conservative fold path via `ctx+1379` tier flags; prevents FP folds that could alter precision semantics |

The `"ConvertMemoryToRegisterOrUniform"` named-phase gate at `0x21DD228` allows phase 37 to be disabled via the `--no-phase` command-line option.

## Function Map

| Address | Name | Role |
|---|---|---|
| `0xC5F940` | Phase 13 execute | Tail-calls `0x1C64BF0` (single-func) or `sub_7917F0` (multi-func) |
| `0xC5FC50` | Phase 29 execute | Checks count > 1, calls `sub_908EB0` |
| `0xC5FD70` | Phase 37 execute | Checks count > 1, calls `sub_910840` |
| `0xC60840` | Phase 46 execute | Indirect vtable dispatch through `comp_unit->vtable[0x1C0]` |
| `0xC5FF20` | Phase 58 execute | Checks count > 1, calls `sub_8F7080` |
| `0xC60550` | Phase 65 execute | Checks count > 1, indirect dispatch through `comp_unit->vtable[392]` |
| `0x7917F0` | `GeneralOptimizeEarly` body | Multi-function path: iterates blocks, fixed-point loop with `sub_753600` |
| `0x908EB0` | `GeneralOptimize` body | Per-block copy prop + predicate simplification with flag marking |
| `0x910840` | `GeneralOptimizeMid` body | Full suite with mem-to-reg; delegates to `sub_905B50` + `sub_90FBA0` |
| `0x8F7080` | `GeneralOptimizeLate` body | Bitvector-tracked 7-counter pass; calls `sub_8F6FA0` |
| `0x753600` | Per-block sub-pass runner (Early) | Structural equivalence detection on def-use chains; returns boolean changed |
| `0x753B50` | Per-block apply changes (Early) | Instruction rewriting: `sub_931920`, `sub_932E80`, `sub_749090`, `sub_9253C0` |
| `0x753480` | Chain walker (Early) | Walks single-def chain forward, checking `sub_7E5120` eligibility |
| `0x753520` | Pair detector (Early) | Finds opcode-93 instruction in chain via `sub_753480` |
| `0x753570` | Secondary pair detector (Early) | Finds second opcode-93 link referencing back to primary |
| `0x753DB0` | Chain tail finder (Early) | Walks opcode-97 links to find end of chain |
| `0x753E30` | Secondary chain matcher (Early) | Handles register renaming boundaries; stores `a1[7..9]` |
| `0x753F70` | Vtable rewrite dispatcher (Early) | Calls `comp_unit->vtable[656]`; constructs opcode-93 replacements |
| `0x7E5120` | Chain eligibility predicate | Checks constant bank, block region, opcode 91 |
| `0x8F6530` | Per-block sub-pass runner (Late) | 6-slot circular buffer; 7-counter change tracking; 550-line function |
| `0x8F6FA0` | Block iterator (Late) | Walks block list calling `sub_8F6530` per block; single pass, no iteration |
| `0x905B50` | Setup/init (Mid) | ~500 lines; creates bitvector infrastructure; 3 tracked structures |
| `0x90FBA0` | Main loop (Mid) | Cost-based instruction-level iteration with register bank analysis |
| `0x90EF70` | Register promotion (Mid) | Memory-to-register conversion; threshold-based (default 0.93, knob 136) |
| `0x903A10` | Register bank helper (Mid) | Per-instruction register bank assignment for LD/ST materialization |
| `0x8F3FE0` | Register constraint fold validator (Mid) | Validates all source operand types are 2/3 and `sub_91D150` constraints match cached values; queries `vtable[904]` for element size and `vtable[936]` for fold metadata |
| `0x8F2E50` | Fold eligibility check | Two-path dispatch: opcode 18 checks source types 2/3 + `sub_91D150` constraints; opcode 124 checks dest type 1/2 + SM <= 20479 threshold + constraint bits `& 0x1C00` |
| `0x8F29C0` | Architecture predicate query | 9 lines; returns `sub_7DC0E0(cu) \|\| sub_7DC050(cu) \|\| sub_7DC030(cu)` on `ctx+1584` |
| `0x908A60` | Two-pass predicate simplify | Called with direction flag (1 = forward, 0 = backward) |
| `0x785E20` | Change tracking reset | Resets per-block change flags |
| `0x781F80` | Instruction flag init | Initializes per-instruction optimization flags (~1800 lines) |
| `0x7E6090` | Use-def chain builder | Builds operand use-def chains; called with `(ctx, 0, 0, 0, 0)` |
| `0x7E6AD0` | Def-use link builder | Builds def-use/use-def bidirectional links |
| `0x7DF3A0` | Liveness check | Returns status pointer; bits 2-3 (`& 0xC`) indicate live uses |
| `0x7E7380` | Instruction equivalence | Compares two instructions for structural equivalence |
| `0x747F40` | Negation flag extractor | Extracts negation modifier from operand encoding |
| `0x747F80` | Absolute-value flag extractor | Extracts abs modifier from operand encoding |
| `0x748570` | Alias hazard check | Returns true if operand has aliasing hazard |
| `0x1245740` | Sub-DAG equivalence | Compares two instruction sub-DAGs for structural equivalence (arg 2 = depth) |
| `0x91D150` | Register constraint lookup | Trivial: `return *(*(ctx+440) + 4*reg_index)`; 0 = no fold-blocking constraint |
| `0x91E860` | Use-count estimator | Returns estimated use count for cost-based decisions (used by phase 37) |
| `0xA9BD30` | Register-class remapper | Maps opcode indices in set {1,2,3,7,11,15,20,24} via `vtable[632]`; writes `value \| 0x60000000` (constant class marker) |
| `0x1249B50` | SASS-level integer ALU fold | Combines IMAD_WIDE/IADD3/SGXT/CCTLT (opcodes 2,3,5,110) with MOV source pairs via `sub_1249940` and `sub_1245740` |
| `0x1249940` | MOV-pair fold combiner | Matches two MOV-from-immediate (opcode 139) instructions feeding an ALU op; validates structural equivalence at depth 1 and 2 |
| `0x7E19E0` | Operand info extractor | Builds 52-byte operand descriptor for opcodes 2,3,5,6,7; classifies source types and constant bank membership |
| `0x7DC0E0` | Architecture capability check A | Checks compilation unit capability flag; used by `sub_8F29C0` for predicate fold safety |
| `0x7DC050` | Architecture capability check B | Secondary capability check for `sub_8F29C0` |
| `0x7DC030` | Architecture capability check C | Tertiary capability check for `sub_8F29C0` |

## Cross-References

- [Pass Inventory](index.md) -- full 159-phase table with GeneralOptimize instances highlighted
- [Phase Manager](phase-manager.md) -- dispatch loop, vtable protocol, factory switch at `sub_C60D30`
- [Optimization Pipeline](../pipeline/optimizer.md) -- overall pipeline stages
- [Copy Propagation & CSE](copy-prop-cse.md) -- standalone copy propagation passes (phases 49, 50, 64, 83)
- [Liveness Analysis](liveness.md) -- standalone `OriPerformLiveDead` passes (heavier DCE)
- [Peephole Optimization](../codegen/peephole.md) -- MainPeepholeOptimizer; handles constant-identity patterns (x+0, x*1, x&0, etc.)
- [Strength Reduction](strength-reduction.md) -- standalone strength reduction pass (phase 26)
- [Knobs System](../config/knobs.md) -- option 464 (iteration cap), option 487 (general opt enable), `limit-fold-fp`
