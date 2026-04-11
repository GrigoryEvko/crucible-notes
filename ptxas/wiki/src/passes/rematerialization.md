# Rematerialization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Rematerialization is the compiler technique of recomputing a value near its use instead of keeping the original definition live across a long range. In ptxas, rematerialization is implemented through three cooperating pipeline phases and tightly integrated with the register allocator's spill-vs-remat decision logic. On GPUs, where register pressure directly determines occupancy and therefore throughput, aggressive rematerialization is one of the most performance-critical optimizations in the entire pipeline.

| | |
|---|---|
| **Phase 28** | SinkRemat -- sinks instructions closer to uses, marks remat candidates |
| **Phase 54** | OriDoRematEarly -- sets remat mode flag (`ctx+1552 = 4`) |
| **Phase 69** | OriDoRemat -- late rematerialization after predication and fusion |
| **Address range (phase 28)** | Execute: `sub_C5FC20`, core: `sub_913A30` -> `sub_A0F020` |
| **Address range (phase 69)** | Execute: `sub_C5F910`, core: `sub_A112C0` -> `sub_A11060` -> `sub_A107B0` |
| **Minimum opt level** | Phase 28: requires level > 4 (knob 487); Phase 69: requires level > 1 |
| **Operand kind 7** | "Remat" marker in the Ori IR operand classification |
| **Vreg flags (offset +80)** | `0x80000001` = remat candidate; `0x80000007` = remat with predication; `0x80000008` = remat committed |
| **Regalloc integration** | `sub_93AC90` (remat check), `sub_99A9D0`/`sub_99AA50` (range remat cost) |
| **DUMPIR name** | `SinkRemat`, `OriDoRematEarly`, `OriDoRemat` |

## Why Rematerialization Matters on GPUs

On NVIDIA GPUs, register count per thread inversely determines the number of concurrent warps (occupancy). Each additional register consumed by a kernel reduces the number of warps that can be resident on an SM. Since GPU performance depends on hiding memory latency through massive parallelism, even a single extra register can measurably degrade throughput.

Rematerialization trades instruction count for register pressure reduction. Instead of keeping a computed value alive in a register from its definition to its last use, the compiler recomputes it where needed. This is profitable when:

1. The original instruction is cheap (single-cycle ALU: IADD, IMAD, MOV, SEL, LOP3, SHF)
2. All source operands are still available at the use point (not overwritten)
3. The live range of the result is long enough to actually cause register pressure
4. The instruction has no side effects (no memory writes, no barrier interactions)

On GPUs, the cost-benefit tradeoff is skewed much further toward remat than on CPUs. A single spill/refill pair (STL + LDL) costs 20--100 cycles of local memory latency, while a rematerialized IADD costs 1 cycle. More importantly, the spill itself consumes a register for the address computation, potentially cascading into more spills.

## Pipeline Position

```
Phase 23   GenerateMovPhi          SSA phi nodes -> MOV instructions
Phase 24   OriPipelining           Software pipelining
Phase 25   StageAndFence           Memory fence insertion
Phase 26   OriRemoveRedundantBarriers
Phase 27   AnalyzeUniformsForSpeculation
Phase 28   SinkRemat               *** Sink + remat candidate marking ***
Phase 29   GeneralOptimize         Bundled mid-level optimizations
  ...
Phase 53   OriPropagateVaryingFirst
Phase 54   OriDoRematEarly         *** Sets remat mode flag ***
Phase 55   LateExpansion
  ...
Phase 63   OriDoPredication        If-conversion (creates new opportunities)
  ...
Phase 66   OriHoistInvariantsLate
Phase 67   DoKillMovement
Phase 68   DoTexMovement
Phase 69   OriDoRemat              *** Late rematerialization ***
Phase 70   OriPropagateVaryingSecond
```

The three-phase design is deliberate:

- **Phase 28 (early)**: Runs after SSA construction and pipelining but before the main optimization passes. Sinks instructions closer to their uses and identifies candidates. This is the most complex of the three phases.
- **Phase 54 (mode setter)**: A trivial phase that writes `4` to `ctx+1552` (the pipeline progress counter), signaling to downstream passes that rematerialization mode is active. Its `isNoOp()` returns 1 in the default vtable, meaning the dispatch loop **skips** its `execute()` by default. The phase is only active when an architecture backend overrides the vtable to return 0, at which point the single-store execute body runs.
- **Phase 69 (late)**: Runs after predication (phase 63) and loop fusion (phase 59), which restructure control flow and create new rematerialization opportunities that did not exist at phase 28 time. Also runs after `OriHoistInvariantsLate` (phase 66), which may have extended live ranges by hoisting invariants.

## Phase 28: SinkRemat

### Entry and Guard Logic

The execute function (`sub_C5FC20`) applies two layers of gating:

```
function SinkRemat_execute(phase, ctx):
    opt_level = getOptLevel(ctx)           // sub_7DDB50
    if opt_level <= 1:
        return
    return sub_913A30(ctx)                 // actual implementation
```

`sub_913A30` (131 lines) performs additional checks before invoking the core:

1. **Optimization level >= 5**: Required for the full sink+remat pass
2. **Knob 487**: Must be enabled (queried via `vtable+152` dispatch on `ctx+1664`)
3. **Cutlass detection** (`sub_8F47E0`): Checks if the function name contains "cutlass" via `strstr()`. Cutlass kernels receive special treatment
4. **Flag check** (`ctx+1368` bit 0): Must be set (compilation is in SSA window)
5. **Feature flags** (`ctx+1376`): Must have bit 26 set (`0x4000000`) but NOT bit 53 (`0x20000000000000`) simultaneously

When the cutlass flag (`ctx+1381` bit 6) is set, the pass enters an iterative mode:

```
function sub_913A30(ctx):
    if opt_level <= 4:
        return
    if not knob_enabled(487):
        return
    is_cutlass = function_name_contains("cutlass")
    if not (flag_byte(ctx+1368) & 1):
        return
    if not is_cutlass and not (flag_byte(ctx+1381) & 0x40):
        return

    // Feature flag gating
    features = *(ctx+1376) & 0x20000004000000
    if features != 0x4000000:
        return

    // Cutlass iterative mode
    if flag_byte(ctx+1381) & 0x40:
        max_iters = 5                      // default
        if hw_config->field_62064:         // architecture-specific override
            max_iters = getKnob(862)       // configurable iteration limit
            if max_iters <= 0: goto sinkRemat_core
        for iter in 0..max_iters:
            sub_8F5220(&state, ctx)        // initialize iteration state
            changed = sub_911030(&state, iter)  // core sink+remat
            if not changed or sub_8F59C0(&state):  // convergence check
                break
            sub_8F5AD0(&state)             // update state for next iter
            sub_909A20(&state)             // propagate changes
            // clean up 4 bitvectors + 2 hash tables
        return

    // Non-cutlass path: single invocation
    sinkRemat_core:
    if is_cutlass:
        // Instruction count limit check
        if *(ctx+1584)->field_372 > 0x7FFF:
            // Warn via vtable dispatch (diagnostic knob 356, severity 2)
        sub_A0F020(ctx)                    // CORE: sink + remat driver
        vtable_callback()                  // post-processing hook
        sub_781F80(ctx, 1)                 // rebuild liveness
        sub_8F4820(ctx, &worklist)         // build remat worklist
        // Process worklist in reverse order
        for item in worklist (descending):
            sub_8F4F90(ctx, &item)         // apply remat decisions
```

### Core Sink+Remat Driver: sub_A0F020

`sub_A0F020` (494 lines) is the main workhorse of phase 28. It operates on the entire function body, processing basic blocks in reverse postorder through the dominator tree.

The algorithm has two main stages:

**Stage 1: Per-block sinking analysis** (via `sub_A06A60` calling `sub_A08250`)

For each basic block in reverse postorder:
1. Walk the instruction list backward
2. For each instruction, check if it has a single use in a dominated block
3. If so, sink the instruction to the use block (moves the instruction node in the linked list)
4. Track whether any changes were made for convergence

**Stage 2: Cross-block rematerialization** (via `sub_A06A60` calling `sub_A07DA0`)

For each basic block in reverse postorder:
1. Walk the instruction list
2. For each rematerialization-eligible instruction, check if the cost model approves duplication
3. If profitable, clone the instruction at the use site and mark the original's result register with the remat flag

The pass alternates between sinking and rematerialization in a fixed-point loop, repeating until no more changes occur. The two worklist callbacks (`sub_A08250` for sinking, `sub_A07DA0` for remat) operate on a per-block basis through a generic block visitor (`sub_A06A60`).

The block visitor manages per-block liveness bitvectors:
- `block+16`: live-in bitvector
- `block+40`: live-out bitvector
- `block+64`: kill set
- `block+112`: live-through set (computed as intersection of live-in and live-out)

## Phase 69: OriDoRemat (Late Rematerialization)

### Entry Logic

The execute function (`sub_C5F910`) checks the optimization level and delegates:

```
function OriDoRemat_execute(phase, ctx):
    opt_level = getOptLevel(ctx)           // sub_7DDB50
    if opt_level <= 1:
        return
    return sub_A112C0(ctx, -1)             // -1 = process all blocks
```

### Remat State Initialization: sub_A0C310

`sub_A112C0` first initializes a rematerialization state object (88+ bytes) via `sub_A0C310`:

```
struct RematState {
    +0    compilation_ctx*  ctx
    +8    RefCountedObj*    block_worklist     // refcounted allocator object
    +16   int              hash_capacity       // hash table for seen blocks
    +20   int              hash_count
    +24   HashNode**       hash_buckets
    +32   int              hash_bucket_count
    +72   int              iteration_count     // convergence counter
    +80   RefCountedObj*   instruction_pool    // for cloned instructions
    +88   int              pass_id             // -1 for late remat
    +92   byte             requires_tex_check  // hw_config->field_17064 == 1
};
```

The pass ID `-1` distinguishes OriDoRemat from OriDoRematEarly. When the hardware configuration at `hw_config+17064` is 1 and `hw_config+17072 != 0`, the `requires_tex_check` flag is set, enabling additional texture-instruction awareness.

### Iterative Remat Loop: sub_A112C0 + sub_A11060

The late remat pass runs in a convergence loop:

```
function sub_A112C0(ctx, pass_id):
    init_remat_state(&state, ctx, pass_id)

    // Iterative convergence loop
    while sub_A11060(&state) and getOptLevel(ctx) != 1
          and sub_785E20(ctx, 0):           // instruction budget check
        continue

    // Cleanup: drain worklist, release refcounted objects
    ...
```

### Per-Iteration Worker: sub_A11060

Each iteration of `sub_A11060` (155 lines) processes the entire instruction list:

```
function sub_A11060(state):
    ctx = state->ctx
    sub_7E6090(ctx, 0, 1, 0, 0)           // rebuild use-def chains
    // Reset all basic block depth markers to 0x80000000 (unvisited)
    for bb in ctx->block_list:
        bb->field_76 = 0x80000000

    // Drain hash table back into instruction pool
    drain_hash_table(state)

    first_pass = !state->requires_tex_check
    changed = false

    // Walk instructions in program order
    instr = ctx->first_instruction         // ctx+280
    while instr:
        if first_pass:
            first_pass = false
            while instr:
                opcode = instr->opcode & 0xFFFFCFFF
                if opcode == 97:           // STG in ROT13; used as definition anchor/label marker
                    changed |= sub_A10DF0(state, instr)
                next = instr->next
                sub_A107B0(state, instr, &sink_flag, &changed_flag,
                          &remat_flag, true)
                instr = next
        else:
            // Non-first-pass: skip MOV processing
            while instr:
                next = instr->next
                sub_A107B0(state, instr, &sink_flag, &changed_flag,
                          &remat_flag, true)
                instr = next

        if not changed_flag:
            goto check_second_pass
        // Decrement iteration counter, check convergence
        if --state->iteration_count == 0:
            return sink_flag

    check_second_pass:
    if remat_flag and *(ctx+1552) > 4:
        // Second pass: walk block list for cross-block opportunities
        for bb in ctx->block_list:
            if (bb->field_20 & 1) == 0 or bb->size <= 0
               or (bb->field_20 & 6) == 6:
                continue                   // skip empty/dead/cold blocks
        instr = ctx->first_block_instruction
        while instr:
            instr = sub_A0C540(state, instr, &changed, ...)
        if changed:
            // Reset depth markers and loop
            continue

    --state->iteration_count
    return sink_flag
```

### Per-Instruction Remat Worker: sub_A107B0

`sub_A107B0` (316 lines) is the core per-instruction decision function called from both phases 28 and 69. It determines whether a specific instruction should be sunk, rematerialized, or left alone.

```
function sub_A107B0(state, instr, sink_flag_out, changed_out, remat_flag_out,
                     allow_remat):
    // Quick rejection: check if instruction is sinkable
    result = sub_A105F0(state, instr, sink_flag_out, changed_out)
    if result:
        return result                      // already sunk, done

    num_operands = instr->operand_count    // at instr+80
    if num_operands <= 0:
        return 0

    // Walk destination operands
    for i in 0..num_operands:
        operand = instr->operands[i]       // at instr+84 + 8*i
        operand_type = (operand >> 28) & 7

        if operand_type == 7:              // barrier register
            // Track barrier liveness
            continue
        if operand_type != 1:              // not a GPR destination
            continue

        // GPR destination operand
        if operand < 0:                    // bit 31 set = definition
            vreg = lookup_vreg(ctx, operand & 0xFFFFFF)
            vreg->flags |= 0x80000001     // mark as remat candidate
            if has_predication_flag and last_operand_is_0x20:
                vreg->flags |= 0x80000007 // enhanced remat with predication
            if sub_A0C410(state, vreg, instr, allow_remat):
                // Remat is profitable: clear depth flag, update block assignment
                vreg->field_76 = ~instr->block_id
            else:
                // Not profitable: process as regular definition
                // Check for multi-use definitions
                ...
        else:
            // Source operand: track liveness contribution
            ...

    return result
```

### Sinkability Check: sub_A105F0

`sub_A105F0` (77 lines) determines if an instruction can be sunk to a single-use block. It enforces strict criteria:

1. **Opcode filter**: Only opcode `0x5F` (95; `STS` in the ROT13 name table, used here as a constant/immediate load variant marker) with `state->byte_92` clear
2. **Single-use check** via `sub_A07940`: The instruction must have exactly one use
3. **Dominator check**: The use must be in a block dominated by the definition block
4. **MOV chain check**: If the instruction feeds opcode 93 (`OUT_FINAL` in ROT13; used here as a MOV-like chain link), verifies through an FNV-1a hash table that the definition matches the expected pattern
5. **Cost check** via `sub_A0C4A0`: Verifies that sinking reduces pressure (returns the pressure delta)

When sinking succeeds, the instruction is physically moved in the linked list via `sub_92E1B0` (insert at new position) and `sub_9253C0` (remove from old position).

## Rematerialization Eligibility Criteria

The eligibility check spans multiple functions. An instruction is rematerializable if it passes ALL of these filters:

### Opcode Whitelist

From `sub_911030`, all three eligibility-check sites use identical logic (decompiled from the first site at offset +0x447):

```
masked = raw_opcode & 0xFFFFCFFF      // clear bits 12-13 (variant/subop)
result = 0
if (masked - 22) <= 0x3D:             // unsigned: masked in [22, 83]
    result = (0x2080000010000001 >> (LOBYTE(raw_opcode) - 22)) & 1
eligible = result | (masked == 297) | (masked == 352)
```

The `& 0xFFFFCFFF` mask strips bits 12--13, collapsing instruction variants (e.g. `.WIDE`, `.X`) to their base opcode before testing. The range guard `(masked - 22) <= 0x3D` is an unsigned comparison that rejects values below 22 (which wrap to large unsigned) and above 83 (83 - 22 = 61 = 0x3D). For the shift itself, `LOBYTE(raw_opcode)` is used -- safe because all in-range opcodes fit in a single byte and the variant bits sit above bit 11, outside the low byte.

**Bitmask bit decomposition** (`0x2080000010000001` = `0b0010_0000_1000_0000_0000_0000_0000_0001_0000_0000_0000_0000_0000_0000_0000_0001`):

| Bit | Opcode | Identity | Latency |
|-----|--------|----------|---------|
| 0   | 22     | IADD/IADD3 | 1 cycle |
| 28  | 50     | SHF      | 1 cycle |
| 55  | 77     | IMAD     | 1 cycle |
| 61  | 83     | ISETP    | 1 cycle |

**Explicit checks** (outside the 64-bit bitmask range):

| Opcode | Identity | Latency |
|--------|----------|---------|
| 297    | LOP3     | 1 cycle |
| 352    | SEL      | 1 cycle |

All six are side-effect-free single-cycle ALU instructions. Opcodes 93 and 95 are **not** part of this whitelist -- they appear in the separate sinkability check (`sub_A105F0`) and MOV-chain analysis (`sub_A10DF0`) inside `sub_A11060`.

After the eligibility check passes, IMAD (opcode 77) receives extra handling: the instruction's last source operand register class is inspected via `(operand >> 11) & 3 == 2`, and the result is stored at `state+192` as a flag for downstream cost computation.

### Operand Source Liveness

`sub_90C010` (70 lines) checks that all source operands are still available (live) at the proposed remat point:

```
function check_sources_available(state, instr, operand_idx, cost_out):
    operand = &instr->operands[operand_idx]

    // Immediate operand: always available
    if sub_7DEB90(operand, state->ctx):
        return 1

    // Must be a GPR (type 1) and not a definition (bit 31 clear)
    type = (operand->value >> 28) & 7
    if type != 1 or (operand->value_high & 1):
        return 0

    // Check if the source vreg has a single reaching definition
    vreg = lookup_vreg(ctx, operand->value & 0xFFFFFF)
    single_def = vreg->field_56
    if single_def:
        return sub_90B790(state, single_def, cost_out, false)

    // Multiple definitions: walk the def-use chain
    min_cost = UINT_MAX
    for def in vreg->def_chain:         // at instr->field_64 + 8*operand_idx
        cost = sub_90B790(state, def->instruction, cost_out, false)
        if cost == 0:
            return 0                    // any unavailable source -> reject
        // For rematerializable defs, add depth cost
        if def is rematerializable:
            cost += (def->block_depth <= instr->block_depth) ? 1 : 0
        min_cost = min(min_cost, cost)
    return min_cost
```

### Cost Model: sub_90B790

`sub_90B790` (`a1`=state, `a2`=def_instr, `a3`=cost_set_out, `a4`=force_deep) returns a non-negative integer cost where 0 = reject, 1+ = profitable (higher = cheaper remat). The function performs a BFS over the source operand tree of the defining instruction, accumulating the maximum per-node cost:

```
function remat_cost(state, def_instr, cost_set, force_deep):
    ctx       = *(state + 8)
    bb_array  = *(ctx + 296)                    // basic block pointer array
    vreg_map  = *(ctx + 88)                     // vreg descriptor array
    use_instr = *(state + 200)                  // instruction at the use point
    use_bb    = bb_array[*(use_instr + 24)]     // use-point basic block
    use_depth = *(use_bb + 156)                 // loop nesting depth of use

    // --- Phase 1: seed the BFS with def_instr ---
    opcode_word = *(def_instr + 16)             // full opcode + modifier bits
    reg_class   = (opcode_word >> 6) & 3        // 0=R, 1=P, 2=UR, 3=UP
    rpo_index   = opcode_word >> 8              // block RPO as visited-set key
    worklist.push(def_instr)
    visited.insert(rpo_index, reg_class)        // hash set keyed by (rpo, class)

    best_cost = -1                              // track max across all nodes

    // --- Phase 2: BFS over source operands ---
    while worklist is not empty:
        node    = worklist.pop()
        node_bb = bb_array[*(node + 24)]
        depth   = *(node_bb + 156)              // loop depth of this def
        opcode  = *(node + 72) & 0xFFFFCFFF     // base opcode, modifier-stripped

        // 2a. Depth gate: def in shallower/same nest as use -> add to cost set
        if depth >= use_depth and not force_deep:
            cost_set.insert(node)               // record for caller's use

        // 2b. Early rejection for non-rematerializable opcodes
        if opcode == 185:                       // IMMA -- never remat
            goto assign_cost_1
        if opcode == 183:                       // LD variant -- reject shared mem
            space = sub_91C840(last_src_operand(node))
            if space == 4:                      // shared memory
                goto assign_cost_1
        if opcode == 164:                       // side-effecting opcode
            goto assign_cost_1

        // 2c. Find last GPR destination (scan operands backward)
        dst_idx = find_last_gpr_dst(node)       // backward scan of operand array
        if dst_idx < 0:
            goto assign_cost_1                  // no GPR dest -> treat as cost 1

        // 2d. Compute or retrieve cached per-node cost
        cached = *(node + 48)                   // cached cost field
        if cached == -1:                        // first visit -- compute now
            if is_cheap_alu(opcode):            // bitmask + explicit checks
                // Cheap ALU: cost depends on relative loop depth
                if not force_deep:
                    goto assign_cost_0          // reject in normal mode
                if *(use_instr + 52) <= *(node + 52):   // use pos <= def pos
                    if use_depth != depth:
                        goto assign_cost_0
                    *(node + 48) = 2;  best_cost = max(best_cost, 2)
                else:
                    if use_depth - 1 != depth:
                        goto assign_cost_0
                    goto assign_cost_1
            else:
                // Non-cheap: check side-effect flags
                flags = sub_7DF3A0(node, ctx)
                if (*flags & 0xC) != 0:         // has memory/side-effect
                    if not sub_8F47E0(ctx):      // not a cutlass kernel
                        goto assign_cost_0
                // Walk source operands, enqueue single-def sources
                for i in dst_idx downto 0:
                    operand = node->operands[i]
                    if operand < 0: break       // hit definition boundary
                    if (operand >> 28) != 1: skip to prev GPR operand
                    if (operand_high & 0x1000000): continue // skip remat marker
                    if predicated and operand matches pred pair: continue
                    vreg = *(vreg_map + 8 * (operand & 0xFFFFFF))
                    single_def = *(vreg + 56)
                    if single_def:
                        if visited.insert(*(single_def+16)): // new?
                            worklist.push(single_def)
                    else:
                        for def in def_chain(node, i):
                            if visited.insert(*(def+16)):
                                worklist.push(def)
        else:
            if cached == 0:
                goto assign_cost_0              // previously rejected
            // Use cached cost; add depth bonus for cheap ALU in force_deep
            node_cost = cached
            if force_deep and is_cheap_alu(opcode):
                node_cost += (*(def_instr+52) >= *(node+52)) ? 1 : 0
            best_cost = max(best_cost, node_cost)
            continue

    // --- Phase 3: final adjustment ---
    if force_deep and best_cost == 1:
        best_cost = 2                           // minimum useful cost in deep mode
    return max(best_cost, 0)

assign_cost_1:  *(node + 48) = 1;  best_cost = max(best_cost, 1); continue
assign_cost_0:  best_cost = 0;  goto cleanup_and_return
```

The `is_cheap_alu` predicate encodes the compile-time bitmask `0x2080000010000001`:

```
is_cheap_alu(opcode) :=
    opcode in {22, 50, 77, 83}           // IADD3, SHF, IMAD, ISETP via bitmask
    or opcode == 297                     // LOP3, explicit check
    or opcode == 352                     // SEL, explicit check
```

Key properties of the cost function:
- **BFS with visited set**: avoids recomputing cost for shared source operands across a DAG (not just tree)
- **Cached at `instr+48`**: once a node's cost is determined, subsequent queries reuse it in O(1)
- **Depth-relative scoring**: cost 2 (same nesting) vs cost 1 (one level deeper) captures the execution frequency penalty of rematerializing inside a loop
- **Cutlass exemption**: side-effecting instructions that would normally be rejected are permitted in cutlass kernels (`sub_8F47E0`), because cutlass scheduling relies heavily on remat for register pressure control
- **Predication guard**: when `instr+73` bit 4 is set (predicated instruction), the penultimate operand pair is excluded from the source walk to avoid counting the predicate register as a rematerialization dependency

## Cross-Block Rematerialization: sub_A0C540

`sub_A0C540` (228 lines) handles rematerialization across basic block boundaries, invoked in the second pass of `sub_A11060`. It processes definitions that are used in different blocks:

```
function cross_block_remat(state, instr, changed_out):
    // Walk operands in reverse order (destinations first)
    for i in (instr->operand_count - 1) downto 0:
        operand = instr->operands[i]
        if (operand >> 28) & 7 != 1:      // not a GPR
            continue
        if (operand_high & 1):             // skip source operands
            continue

        vreg = lookup_vreg(ctx, operand & 0xFFFFFF)
        if (vreg->flags_48 & 0x22) != 0:  // skip special vregs
            continue
        if vreg->reg_index in [41..44]:    // skip architectural predicates
            continue

        flags80 = vreg->field_80
        if not (flags80 & 1):             // not a remat candidate
            continue
        if vreg->use_count <= 0:
            continue
        if (flags80 & 2) and (flags80 & 4):  // already fully processed
            continue

        // Compute pipe-mapped execution cost for this operand
        cost = cross_block_remat_cost(ctx, instr, i)   // sub_91E860

        if operand < 0:                    // definition
            if cost <= 3:                  // cheap ALU/FMA/DFMA -- mark only
                vreg->field_80 |= 0x80000008  // commit remat, no cloning
                continue
            // Expensive instruction: must clone at use site
            adjust_pressure(state, instr, -1)  // sub_A0C4A0
            duplicate_at_use(ctx, instr)       // vtable dispatch +1280
            adjust_pressure(state, instr, +1)
            vreg->field_80 |= 0x80000008
            vreg->flags_48 &= ~0x300000        // clear live-through bits
            // Rebuild interference for affected ranges
            adjust_pressure(state, instr, -1)
            sub_92C0D0(ctx, instr, 0, ...)     // clone instruction at use
            adjust_pressure(state, instr, +1)
            *changed_out = 1
```

### Cross-Block Remat Cost Function: sub_91E860

`sub_91E860` (10 bytes of logic, 2-step composition) computes the pipe-mapped execution cost for one operand of an instruction. It calls `sub_91E610` to classify the operand into a latency class code, then maps that code through `vtable+904` (`PipeAssignment`) to obtain a pipe index that serves as the cost value returned to the caller.

```
function cross_block_remat_cost(ctx, instr, operand_idx):     // sub_91E860
    latency_class = classify_operand_latency(ctx, instr, operand_idx)  // sub_91E610
    return PipeAssignment(ctx->backend, latency_class)                 // vtable+904
```

**Operand latency classification (`sub_91E610`, 58 lines)** has four paths by priority:

```
function classify_operand_latency(ctx, instr, op_idx):        // sub_91E610
    operand = instr->operands[op_idx]

    // Path 1: Register-class shortcut for memory/texture destinations
    if operand is GPR destination:
        switch lookup_vreg(ctx, operand & 0xFFFFFF)->reg_class:  // vreg+64
            case 4:  return 26     // global memory -> LSU latency
            case 5:  return 20     // texture/uniform -> TEX latency
            case 2:  return 20     // uniform register -> TEX latency

    // Path 2: Predicated-instruction guard operand match
    if opcode_flags(instr, ctx) & 0x40:                        // sub_7DF3A0
        guard_pos = operand_count - 2*(has_0x1000) - 5
        if operand[guard_pos] has predicate type:
            match = vtable+1608(backend, instr, 8, 0)
            if match.valid and match.index == op_idx: return 10

    // Path 3: Comparison tail operands
    if has_0x1000 and op_idx >= operand_count - 2:
        if op_idx == operand_count - 2:
            return (type 2/3) ? 20 : (type 4) ? 26 : 1
        return 1                   // flag output -> cheapest

    // Path 4: General opcode dispatch (sub_91A0F0, ~350 lines)
    return classify_by_opcode(opcode & 0xFFFFCFFF, subop,
                              operands, operand_count, op_idx)
```

**Threshold 3 rationale.** `PipeAssignment` maps latency classes to pipe indices 0--8. The `cost <= 3` threshold partitions instructions into two categories:

| Pipe cost | Pipe | Latency classes | Decision |
|---|---|---|---|
| 0 | ALU | 1 (pred move), 9 (cvt), 10 (IMAD), 11 (LEA) | Mark only |
| 1 | FMA | 7 (FP16, narrow FP) | Mark only |
| 2 | DFMA | 4 (FP64 cvt), 16 (FP64 special) | Mark only |
| 4 | LSU | 12 (mem/FP32/atomic), 14 (wide mem), 26 (global ld) | Clone at use |
| 5 | TEX | 6 (texture fetch), 20 (uniform load) | Clone at use |
| 6 | BRA | 31 (scoreboard/barrier) | Clone at use |
| 8 | SFU | 8 (special function) | Clone at use |

Instructions at `cost <= 3` are single-cycle ALU/FMA/DFMA operations: the compiler marks the vreg `0x80000008` (committed remat) without inserting any new instructions; the register allocator later re-executes them at each use. Instructions at `cost > 3` occupy long-latency or resource-constrained pipes (LSU, TEX, SFU), so rematerializing requires explicit instruction cloning into each use-site block, with pressure adjustment (`sub_A0C4A0`) and interference rebuild (`sub_92C0D0`) around the clone.

## Interaction with Register Allocator

The rematerialization flags set during phases 28 and 69 are consumed by the fat-point register allocator in several ways:

### Remat Detection During Assignment: sub_93AC90

During per-instruction register assignment (`sub_9680F0`, 3722 lines), the allocator calls `sub_93AC90` (29 lines) to check if a virtual register is a rematerialization candidate:

```
function check_remat_opportunity(alloc, vreg_index, reg_class):
    if alloc->vreg_count == 0:
        BUG()
    entry = hash_lookup(alloc->remat_table, vreg_index)
    cost = entry->field_144[reg_class]
    if cost < entry->threshold:
        return true
    return (cost == entry->threshold) and (reg_class == entry->field_12)
```

### Range Remat Cost: sub_99AA50

The live-range infrastructure at `0x994000`--`0x9A1000` includes remat-aware cost functions. `sub_99AA50` (51 lines) inserts a rematerialization cost node into a range's cost linked list, enabling the allocator to compare spill cost against remat cost when choosing between spilling and rematerializing a value.

### Three-Way Keep/Spill/Remat Decision: `sub_9AEF60`

The live range splitting engine (`sub_9AEF60`, 1415 lines) implements a three-way decision for each interference graph node: **keep** the current assignment, **spill** to local memory, or **rematerialize** the value. The algorithm iterates over split candidates extracted from a bitvector scan and classifies each via `sub_9AEC60` profitability analysis:

```
// Three-way decision per interference node (decompiled lines 1076-1127)
for node in interference_hash_iter(alloc+89):       // alloc+89 = IG hash table
    flags = node->field_3 & 0xF3                     // mask bits 2-3, preserve 0-1
    node->field_3 = flags
    if flags & 1:                                     // bit 0: remat candidate
        // REMAT path: mark node for rematerialization
        sub_9A1AD0(alloc, vreg_desc_ptr, 4)           // flag=4 → remat interference
        if flags & 2:                                 // bit 1: also coalesce candidate
            sub_9A1AD0(alloc, vreg_desc_ptr, 8)       // flag=8 → coalesce+remat
    elif flags & 2:                                   // bit 1 only: coalesce candidate
        sub_9A1AD0(alloc, vreg_desc_ptr, 8)           // flag=8 → spill/coalesce
    else:
        continue                                      // KEEP: no action needed
```

The profitability gate (`sub_9AEC60`) returns a `(viable, cost)` pair. If `cost <= alloc->threshold` (float at alloc+41), the range is kept in its current assignment. If `cost > threshold`, the range enters the split/remat pipeline where the interference flag bits determine the action: bit 0 set triggers rematerialization via `sub_9A1AD0(flag=4)`, bit 1 set triggers spill/coalesce via `sub_9A1AD0(flag=8)`, and both bits set applies both treatments. When a better split is found (`cost > best_cost`), the engine snapshots the current state into `alloc[83..87]` and restores it into the candidate worklist at `alloc[170..179]` before committing.

#### Remat Linked Lists at `alloc+161..+175`

The QWORD-indexed range `alloc[161]..alloc[175]` (byte offsets +1288 through +1400) holds two circular doubly-linked lists used as worklists for coalescing and rematerialization candidates:

| QWORD index | Byte offset | Role |
|-------------|-------------|------|
| 161--168 | +1288--+1344 | **List A** (coalesce/remat): sentinel, prev, next, data, count, end, tag=2, arena |
| 169--175 | +1352--+1400 | **List B** (secondary): sentinel, prev, next, data, tail, end, tag=2, arena |

Each list entry is a 24-byte node `{next_ptr, tail_ptr, count_and_flags}` allocated from the arena at the list's last slot. During each splitting round, the engine drains List B (`alloc[175]`) into the freelist at `alloc[179]` via `sub_69DD70`, then copies the saved best-state from `alloc[85..86]` back into `alloc[175..179]` (a 128-bit `movdqu` restore), and zeroes `alloc[170..172]` plus the candidate counter at `DWORD[346]`. This drain-restore cycle ensures that only the best split's candidate set survives into the next iteration, while prior speculative nodes are returned to the arena.

### Verification: sub_A55D80

The post-allocation verifier (`sub_A55D80`, referenced by "REMATERIALIZATION PROBLEM..." string) validates that rematerialization was applied correctly. Error case 7 in the verifier specifically checks that:
- The rematerialized instruction produces the same value as the original
- The reaching definitions before and after allocation match (modulo known-safe remat transformations)
- No rematerialized instruction references a register that was invalidated by the allocation

## Operand Kind 7: Remat Markers

The Ori IR operand classification includes a dedicated "Remat" kind (value 7) that marks operands participating in rematerialization. This marker is orthogonal to the `vreg+80` flags -- it exists in the instruction's operand descriptors and tells downstream passes that this particular use was created by rematerialization rather than by the original program.

The 10 operand kinds in the Ori IR:

| Kind | Name | Description |
|------|------|-------------|
| 0 | R register | General-purpose register |
| 1 | Offset | Memory offset |
| 2 | P/UP register | Predicate register |
| 3 | Any register | Wildcard |
| 4 | Regular | Immediate or constant |
| 5 | Predicated | Guard predicate |
| 6 | -- | (reserved) |
| **7** | **Remat** | **Rematerialization marker** |
| 8 | Spill-refill | Spill/refill pair |
| 9 | R2P/P2R | Register-to-predicate conversion |

## Vreg Flags at Offset +80

The virtual register's field at offset +80 encodes rematerialization state through a bitmask:

| Bit | Mask | Meaning |
|-----|------|---------|
| 0 | `0x1` | Remat candidate -- this value CAN be recomputed |
| 1 | `0x2` | Remat source processed -- cross-block analysis done |
| 2 | `0x4` | Remat committed -- the allocator should prefer remat over spill |
| 31 | `0x80000000` | Depth marker / unvisited sentinel |

Common flag combinations:
- `0x80000001`: Candidate identified by sub_A107B0, pending cost analysis
- `0x80000007`: Candidate with predication awareness (stronger guarantee for predicated code paths)
- `0x80000008`: Remat committed by cross-block analysis (`sub_A0C540`), allocator should use remat

## Knobs and Configuration

| Knob ID | Role | Default | Notes |
|---------|------|---------|-------|
| 487 | Gate for SinkRemat pass | (enabled) | Must be true for phase 28 to execute |
| 862 | Cutlass iteration limit | 5 | Max iterations in cutlass-specific iterative mode |
| 356 | Instruction count diagnostic | -- | Severity-2 warning when instruction count exceeds 32767 |

The optimization level gating:
- **Level <= 1 (`-O0`/`-O1`)**: All three remat phases are disabled
- **Level <= 4**: Phase 28 runs the non-cutlass path only
- **Level >= 5 (`-O3`+)**: Full sink+remat with cutlass iteration support

## Function Map

### Phase 28 (SinkRemat)

| Address | Function | Size (lines) | Role |
|---------|----------|-------------|------|
| `0xC5FC20` | `sub_C5FC20` | 12 | Phase execute dispatcher |
| `0xC5F2E0` | `sub_C5F2E0` | 7 | getName() -> returns 28 |
| `0xC5F2F0` | `sub_C5F2F0` | 7 | isNoOp() -> returns 0 (always runs) |
| `0x913A30` | `sub_913A30` | 131 | SinkRemat entry with knob/feature gating |
| `0xA0F020` | `sub_A0F020` | 494 | Core sink+remat driver (block visitor loop) |
| `0x911030` | `sub_911030` | 2408 | Per-block promotion/sinking engine |
| `0x90C010` | `sub_90C010` | 70 | Source operand liveness check for remat |
| `0x90B790` | `sub_90B790` | ~350 | Cost model: remat profitability analysis |
| `0x8F47E0` | `sub_8F47E0` | 12 | Cutlass detection (`strstr("cutlass")`) |
| `0x8F4820` | `sub_8F4820` | -- | Build remat worklist |
| `0x8F4F90` | `sub_8F4F90` | -- | Apply remat decisions from worklist |

### Phase 54 (OriDoRematEarly)

| Address | Function | Size (lines) | Role |
|---------|----------|-------------|------|
| `0xC5EF30` | `sub_C5EF30` | 7 | Phase execute: writes `ctx+1552 = 4` |
| `0xC5EF40` | `sub_C5EF40` | 7 | getName() -> returns 54 |
| `0xC5EF50` | `sub_C5EF50` | 7 | isNoOp() -> returns 1 |

Phase 54 is a degenerate phase. Its execute body is a single store: `*(ctx + 1552) = 4`. Its `isNoOp()` returns 1, so the dispatch loop **skips** `execute()` by default -- the phase does nothing unless an architecture backend overrides the vtable to activate it. When active, the value 4 written to `ctx+1552` advances the pipeline progress counter, which `sub_A11060` checks (`if *(ctx+1552) > 4` triggers the cross-block second pass).

### Phase 69 (OriDoRemat)

| Address | Function | Size (lines) | Role |
|---------|----------|-------------|------|
| `0xC5F910` | `sub_C5F910` | 24 | Phase execute dispatcher |
| `0xC5ED50` | `sub_C5ED50` | 7 | getName() -> returns 69 |
| `0xC5ED60` | `sub_C5ED60` | 7 | isNoOp() -> returns 0 (always runs) |
| `0xA112C0` | `sub_A112C0` | 245 | Late remat entry + cleanup |
| `0xA0C310` | `sub_A0C310` | 45 | RematState initialization |
| `0xA11060` | `sub_A11060` | 155 | Per-iteration remat worker |
| `0xA107B0` | `sub_A107B0` | 316 | Per-instruction remat decision |
| `0xA105F0` | `sub_A105F0` | 77 | Sinkability check (opcode 0x5F) |
| `0xA10DF0` | `sub_A10DF0` | 138 | MOV chain analysis (FNV-1a hash table) |
| `0xA0C540` | `sub_A0C540` | 228 | Cross-block rematerialization |
| `0x91E860` | `sub_91E860` | 10 | Cross-block remat cost (latency class -> pipe index) |
| `0x91E610` | `sub_91E610` | 58 | Operand latency classification (4-path dispatch) |
| `0x91A0F0` | `sub_91A0F0` | ~350 | General opcode-to-latency-class switch |
| `0xA0C4A0` | `sub_A0C4A0` | -- | Pressure adjustment (+1 or -1) |
| `0xA0C410` | `sub_A0C410` | -- | Remat profitability check for a vreg |

### Register Allocator Integration

| Address | Function | Size (lines) | Role |
|---------|----------|-------------|------|
| `0x93AC90` | `sub_93AC90` | 29 | Remat opportunity check during assignment |
| `0x99A9D0` | `sub_99A9D0` | 38 | Range rematerialization cost cleanup |
| `0x99AA50` | `sub_99AA50` | 51 | Range rematerialization cost insertion |
| `0x9AEF60` | `sub_9AEF60` | 1415 | Main allocation driver with remat support |
| `0xA55D80` | `sub_A55D80` | ~800 | Post-allocation remat verification |

## Sinking vs. Rematerialization

The SinkRemat pass (phase 28) and the late OriDoRemat pass (phase 69) both move instructions closer to their uses, but through fundamentally different mechanisms:

**Sinking** moves the original instruction. The definition is physically relocated from its original position to a dominated block closer to the use. This does not increase instruction count but may change schedule. Sinking is legal only when:
- The instruction has exactly one use
- The use is in a block dominated by the current definition block
- Moving the instruction does not cross any barrier or synchronization point
- All source operands remain available at the new position

**Rematerialization** duplicates the instruction. The original definition remains in place (or is deleted if dead), and a fresh copy is inserted near each use. This increases instruction count but can dramatically reduce register pressure. Remat is legal for any instruction in the opcode whitelist, subject to:
- All source operands available at the use point
- The cost model approves the duplication
- The instruction has no side effects

The `sub_A105F0` sinkability check runs first in `sub_A107B0`. Only if sinking fails does the function proceed to the rematerialization path. This prioritizes the cheaper transformation (sinking = zero instruction overhead) before falling back to the more expensive one (remat = duplicated instructions).

## Architectural Notes

The three-phase structure with an interleaved flag-setter (phase 54) suggests the rematerialization infrastructure evolved over multiple ptxas generations. Phase 54's `isNoOp() = 1` default means its `execute()` is skipped unless an architecture backend activates it by overriding the vtable. This indicates the phase was likely once a full pass that was later simplified to a flag write, with its analysis logic migrated into phase 69.

The CUTLASS-specific iterative mode in phase 28 (`sub_913A30`) reveals that NVIDIA's matrix-multiply library is important enough to warrant dedicated compiler heuristics. The `strstr("cutlass")` check is a name-based pattern match on the function name, not a property of the IR itself. This coupling between compiler optimization and library naming conventions is a pragmatic choice for a production compiler targeting known workloads.

The FNV-1a hash (constant `0x811C9DC5`, prime `16777619`) appears in both the rematerialization infrastructure (`sub_A10DF0` for MOV chain tracking) and the register allocator (`sub_926A30` for interference). This shared hash implementation is one of ptxas's standard infrastructure components (see [Hash Tables & Bitvectors](../infra/hash-bitvector.md)).
