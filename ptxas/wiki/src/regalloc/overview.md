# Allocator Architecture

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas register allocator is a fat-point allocator with a Chaitin-Briggs-style simplify-select ordering. Before the core allocation loop runs, the ordering function (`sub_93FBE0`, 940 lines) classifies every virtual register into one of six membership lists based on interference degree, constraint count, and register width. Low-interference and unconstrained vregs are drained first (simplify phase), followed by high-interference vregs selected by lowest spill cost (potential-spill phase). The resulting ordering is pushed onto an assignment stack at `alloc+744`. The core fat-point allocator (`sub_957160`) then pops this stack in reverse, assigning each vreg to the physical register slot with the lowest interference count in a per-physical-register pressure histogram (512-DWORD array). High-interference vregs that were deferred during simplify are colored optimistically -- if the fat-point scan finds a slot below threshold, they succeed without spilling.

This hybrid design combines the Chaitin-Briggs simplify-select priority ordering with a fat-point conflict resolution step that replaces the traditional interference-graph adjacency check. There is no explicit interference graph in the main allocation path; instead, per-physical-register pressure histograms serve as the conflict representation. The fat-point scan trades graph-coloring's theoretical optimality for speed on the very large register files of NVIDIA GPUs (up to 255 GPRs per thread).

## Six-List Classification (`sub_93FBE0`)

The ordering function walks the vreg linked list (`alloc+736`) and classifies each vreg into one of six doubly-linked lists. Classification is based on the vreg's interference cost (computed by `sub_938EA0`), constraint chain presence (`vreg+144`), width (`vreg+74`), and flag bits (`vreg+48`):

| List | Offset | Classification criterion | Drain order |
|------|--------|--------------------------|-------------|
| LowConf | `alloc+72` | Low interference (`sub_939220` returns false), not in spill-retry mode | 4th (select phase) |
| LowLate | `alloc+96` | Low interference in spill-retry mode (a3 == 99); assigned decreasing priority | 5th (select phase) |
| High | `alloc+120` | Spill cost exceeds threshold (`alloc+768`), or flag bit `0x80000` set | 3rd |
| VeryHigh | `alloc+144` | Paired-mode vregs with `(flags ^ 0x300000) & 0x300000 != 0` | 2nd |
| Unconstrained | `alloc+168` | No constraint chain (`vreg+144 == NULL`) | 1st |
| LargeVec | stack-local | Width > 4 registers; merged into VeryHigh after classification | (merged) |

**Simplify phase:** Lists are drained in order Unconstrained, VeryHigh, High. Each vreg is popped, processed by `sub_93F130` (which computes final ordering metadata), and pushed onto the assignment stack at `alloc+744`.

**Select phase:** The LowConf and LowLate lists are processed last. Within each, the vreg with the lowest spill-cost-to-interference ratio is selected first (scanning via `vreg+40` / `vreg+92`), with tie-breaking on register width (`vreg+72`) and pre-allocation priority (`vreg+84`). The selected vreg is removed from the list, processed, and pushed onto the assignment stack.

The core fat-point allocator then iterates the assignment stack, coloring each vreg by scanning the 512-DWORD pressure histogram for the minimum-cost slot. High-interference vregs that land late in the stack are colored optimistically -- if a slot exists below the discard threshold (knob 684, default 50), the assignment succeeds without spilling.

A secondary live-range-based infrastructure (~80 functions at `0x994000`--`0x9A1000`) supports coalescing, splitting, and pre-coloring but feeds results into the fat-point allocator rather than replacing it.

| | |
|---|---|
| **Entry point** | `sub_9721C0` (1086 lines) |
| **Per-class driver** | `sub_971A90` (355 lines) -- NOSPILL then SPILL retry |
| **Allocation ordering** | `sub_93FBE0` (940 lines) -- 6-list simplify-select classification |
| **Core allocator** | `sub_957160` (1658 lines) -- fat-point coloring engine |
| **Assignment** | `sub_94FDD0` (155 lines) -- write physical reg, propagate aliases |
| **Spill guidance** | `sub_96D940` (2983 lines) -- per-class priority queues |
| **Spill codegen** | `sub_94F150` (561 lines) -- emit spill/reload instructions |
| **Pre-coloring** | `sub_991790` (2677 lines) -- full-function pre-assignment |
| **Address range** | `0x8FE000` -- `0x9D3000` (~860 KB, ~950 functions) |
| **Knobs** | 87 OCG knobs (`RegAlloc*` / `RegTgt*` / `RegUsageLevel`, indices 613--699) |

## Pipeline Position

The register allocator runs in the late pipeline, after all optimization passes and instruction scheduling preparation, but before final SASS encoding:

```
... optimization passes ...
  Late Legalization / Expansion
  AdvancedPhaseAllocReg gate         <-- pipeline entry guard
  HoistInvariants                    <-- sub_8FFDE0 (optional)
  ConvertMemoryToRegisterOrUniform   <-- sub_910840
  Pre-coloring                       <-- sub_991790
  Instruction lowering               <-- sub_98F430 / sub_98B160
  Register allocation entry          <-- sub_9721C0
    Per-class allocation x 7         <-- sub_971A90 for classes 1..6
      6-list simplify-select order   <-- sub_93FBE0
      Core fat-point allocator       <-- sub_957160
  Post-allocation fixup
  Instruction scheduling
  SASS encoding
```

## Register Classes

The allocator processes 7 register classes. Class 0 (unified) is skipped in the normal per-class loop; it is used for cross-class constraint propagation. Classes 1--6 are allocated independently in order:

| ID | Name | Width | Arch cap | Effective budget source | Description |
|----|------|-------|----------|--------------------------|-------------|
| 0 | -- | -- | -- | -- | Unified / cross-class (skipped in main loop) |
| 1 | R | 32-bit | 255 | `min(maxrregcount, .maxnreg, occupancy-derived)` | General-purpose registers (R0--R254) |
| 2 | R (alt) | 32-bit | 255 | mirror of class 1 | GPR variant (RZ sentinel, stat-collector alternate) |
| 3 | UR | 32-bit | 63 | fixed by arch profile | Uniform general-purpose registers (UR0--UR62) |
| 4 | UR (ext) | 32-bit | 63 | mirror of class 3 | Uniform GPR variant (extended uniform) |
| 5 | P / UP | 1-bit | 7 | fixed by arch profile | Predicate registers (P0--P6, UP0--UP6) |
| 6 | Tensor/Acc | 32-bit | per-arch | profile vtable | Tensor/accumulator registers (MMA/WGMMA) |

The arch cap is the architectural ceiling reported as `regfile_params[3]`. The effective budget written into `RegClassDesc.max_regs` (`alloc+884+32*class_id`) is the minimum of the CLI/PTX inputs and the occupancy-derived budget from `evaluate_budget_fraction()` (see below). Classes 3--5 are not exposed to `--maxrregcount`; their budget equals the architectural cap.

Barrier registers (B, UB) have `reg_type = 9`, which is above the `<= 6` allocator cutoff and are handled by a separate mechanism.

Special registers that are always skipped during allocation:
- Indices 41--44: `PT`, `P0`--`P3` (architectural predicates)
- Index 39: special register

The class ID is the `reg_type` value at `vreg+64`. The allocator distribution loop in `sub_9721C0` reads this field directly and uses it as the bucket index.

Pair modes (`vreg+48`, bits 20--21): `0` = single, `1` = lo-half of pair, `3` = double-width (consumes two physical slots).

## Entry Point: sub\_9721C0

The top-level register allocation driver (1086 lines). Called once per function after the `AdvancedPhaseAllocReg` pipeline gate.

```
function regalloc_entry(alloc_state, compilation_ctx):
    // 1. Rebuild liveness
    rebuild_basic_blocks(compilation_ctx, 1)          // sub_781F80
    compute_liveness(compilation_ctx, 1)              // sub_A10160

    // 2. Initialize 7 register classes
    for class_id in 1..6:
        vtable[896](alloc_state, class_id)            // init register file state

    // 3. Sort instructions by priority
    sort_instructions_by_priority(alloc_state)        // sub_9375C0

    // 4. Distribute vregs into per-class linked lists
    for each vreg in function:
        class = vreg.register_class
        append(class_lists[class], vreg)

    debug("\nREGALLOC GUIDANCE:\n")

    // 5. Allocate each class independently
    for class_id in 1..6:
        alloc_with_spill_retry(                       // sub_971A90
            alloc_state, compilation_ctx, class_id)

    // 6. Post-allocation fixup
    fix_load_opcode_187(alloc_state)
    fix_call_saved_registers(alloc_state)

    // 7. Handle OptixIR mode (ctx+896 == 4 or 5)
    if is_optix_ir(compilation_ctx):
        record_register_counts(compilation_ctx)
```

The entry point calls `sub_789280` when a pre-allocation fixup bit (flag bit 2) is set, handles live-through-call register counting at lines 343--352, and sets up rematerialization lists at `alloc_state[161..175]`.

## Per-Class Driver: sub\_971A90

The outer retry loop (355 lines) that wraps the core allocator with a two-phase strategy:

**Phase 1 -- NOSPILL:** Attempt allocation without allowing spills. Debug string: `"-CLASS NOSPILL REGALLOC: attemp "` (note the typo -- present in the binary).

**Phase 2 -- SPILL:** If NOSPILL fails, invoke spill guidance (`sub_96D940`) and retry with spilling enabled.

```
function alloc_with_spill_retry(alloc_state, ctx, class_id):
    no_retarget = query_knob(638)                     // RegAllocNoRetargetPrefs (bool)
    num_trials  = query_knob(639)                     // RegAllocNumNonSpillTrials (int)

    // Phase 1: NOSPILL
    pre_allocation_pass(alloc_state)                  // sub_94A020
    secondary_driver(alloc_state, ctx)                // sub_95DC10
    result = fatpoint_allocate(alloc_state, ctx, NOSPILL)  // sub_957160
    record_best_result(alloc_state, result)            // sub_93D070

    if result == SUCCESS:
        return

    // Phase 2: SPILL retry loop
    for attempt in 1..num_trials:
        guidance = compute_spill_guidance(ctx, attempt)    // sub_96D940
        result = fatpoint_allocate(alloc_state, ctx, SPILL)
        record_best_result(alloc_state, result)

        if result == SUCCESS:
            break

    if result == FAILURE:
        final_fallback(alloc_state)                   // sub_936FD0

    post_allocation_finalize(alloc_state)             // sub_9714E0
```

For SMEM spilling (modes 3/6 when `ctx+896 == 5`), the driver activates `sub_939BD0` (spill setup) followed by `sub_94F150` (spill codegen) before entering the retry loop.

## Core Fat-Point Allocator: sub\_957160

The central allocation function (1658 lines). This is where physical registers are actually chosen.

### Data Structures

Two 2056-byte arrays (512 DWORDs + 2-DWORD sentinel each):

| Array | Role |
|-------|------|
| Primary (`v12`) | Per-physical-register interference count |
| Secondary (`v225`) | Per-physical-register secondary cost (tie-breaking) |

Both arrays are zeroed with SSE2 vectorized loops at the start of each allocation round.

### Algorithm

```
function fatpoint_allocate(alloc_state, ctx, mode):
    // sub_957160 lines 421-435
    maxRegs = max(alloc_state.hw_limit + 7,           // alloc+756 + headroom
                  alloc_state.max_regs_aligned)       // alloc+1556 = (budget+4) & ~3

    // CSSA modes widen the histogram for paired / quad-grouped slots
    if mode in {CSSA(3), CSSA_PAIRED(6)}:
        widen = (code_obj[+1369] & 0x40) ? 4 : 2      // mode-bit gates 4x vs 2x
        maxRegs *= widen
    alloc_state[+60] = maxRegs                        // committed bound

    primary[512]   = {0}                              // SSE2 memset
    secondary[512] = {0}

    threshold = query_knob(684)                       // RegAllocThresholdForDiscardConflicts, default 50

    for each vreg in alloc_state.register_list:       // linked list at +744
        // Populate interference bitmaps for this vreg
        build_interference_bitmaps(vreg, primary, secondary)   // sub_957020

        // Scan for minimum-pressure physical register.
        // maxRegs is clamped against the 512-DWORD histogram capacity;
        // assignments above 512 are unreachable in practice because the
        // architectural cap is 255 even with CSSA 4x widening (1020 < 2052B).
        best_slot = -1
        best_cost = MAX_INT
        for slot in 0..min(maxRegs, 512):
            if primary[slot] > threshold:
                continue                              // too congested
            cost = primary[slot]
            if cost < best_cost:
                best_cost = cost
                best_slot = slot
            elif cost == best_cost:
                // tie-break on secondary bitmap
                if secondary[slot] < secondary[best_slot]:
                    best_slot = slot

        if best_slot == -1:
            emit_error("Register allocation failed with register count of '%d'")
            return FAILURE

        // Assign physical register
        assign_register(alloc_state, ctx, mode,       // sub_94FDD0
                        vreg, best_slot)

    return alloc_state.register_count + 1
```

The interference threshold (`RegAllocThresholdForDiscardConflicts`, knob 684, default 50) is the key heuristic parameter. Slots with interference above this value are discarded (skipped entirely), forcing the allocator toward less-contested register slots even if they are not globally minimal.

## Register Assignment: sub\_94FDD0

The assignment function (155 lines) writes the physical register and propagates through alias chains:

```
function assign_register(alloc, ctx, mode, vreg, regclass_info, slot, cost):
    max_regs = regclass_info.max_regs                 // at +16

    if slot >= max_regs and not vreg.is_spilled():    // flag 0x4000
        vreg.set_needs_spill()                        // flag 0x40000
        return

    if vreg.needs_spill():                            // flag 0x40000
        setup_spill_allocator(alloc)                  // sub_939BD0
        generate_spill_code(alloc, vreg)              // sub_94F150
        return

    // Non-spill path: commit assignment
    consumption = compute_consumption(vreg)            // sub_939CE0
    update_peak_usage(alloc, consumption)
    vreg.physical_register = slot

    // Check for pre-allocated candidate
    apply_preallocated_candidate(alloc, vreg)         // sub_950100

    // Propagate through alias chain
    alias = vreg.alias_parent                         // vreg+36
    while alias != NULL:
        alias.physical_register = slot
        alias = alias.alias_parent
```

Register consumption computation (`sub_939CE0`, 23 lines) accounts for paired registers: it returns `assignment + (1 << (pair_mode == 3)) - 1`, effectively consuming two slots for double-width registers.

## Constraint System

The fat-point interference builder (`sub_926A30`, 4005 lines) processes 15+ constraint types extracted from instruction operand descriptors. Each operand encodes: bits 28--30 = operand type, bits 0--23 = register index.

| Type | Name | Description |
|------|------|-------------|
| 0 | Point interference | Single-instruction conflict at a specific program point |
| 1 | Register operand | Standard read/write interference |
| 2 | Immediate operand | No register interference generated |
| 3 | Paired register | Double-width; bit 23 distinguishes hi/lo half |
| 4 | Exclude-one | Specific physical register excluded from assignment |
| 5 | Exclude-all-but | Only one physical register permitted |
| 6 | Below-point | Interference active below the current program point |
| 7 | Range | Interference over an interval of program points |
| 8 | Phi-related | CSSA phi instruction (opcode 195) constraint |
| 9 | Barrier | Barrier register class constraint |
| 10--15 | Extended | Additional constraint variants |

The builder uses FNV-1a hashing (seed `0x811C9DC5`, prime `16777619`) for hash-table lookups into the pre-allocation candidate table. It contains SSE2-vectorized inner loops for bulk interference weight accumulation and dispatches through 7+ vtable entries for OCG knob queries.

## Spilling Overview

Spilling triggers when the fat-point allocator cannot find a physical register within the budget. The subsystem has three components:

**Spill guidance** (`sub_96D940`, 2983 lines): Computes which registers to spill and in what order. Builds a 7-element guidance array (one per register class), each backed by an 11112-byte working structure containing 128-element bitmask arrays. Constructs priority queues of spill candidates using bitvector-based live range analysis. The function contains 7 near-identical code blocks (one per class), likely unrolled from a template.

**Spill codegen** (`sub_94F150`, 561 lines): Emits actual spill/reload instructions. Allocates a per-register spill info array (12 bytes per entry, initialized to `{0, -1, -1}`). Default spill cost is `15.0`, reduced to `3.0` for certain architecture modes. Handles loop nesting via block frequency callbacks (vtable offset `+8`) and provides special handling for uniform registers (bit `0x200` in flags).

**Spill memory targets:**

| Target | Description |
|--------|-------------|
| LMEM (local memory) | Default spill destination. Per-thread private memory. |
| SMEM (shared memory) | Alternative spill destination. Faster but shared across CTA. Assertion: "Smem spilling should not be enabled when functions use abi." |

Spill setup (`sub_939BD0`, 65 lines) selects configuration based on `RegAllocEstimatedLoopIterations` (knob 623) and the cost threshold at `alloc+776`:

| Condition | Bucket size | Alignment | Max size |
|-----------|-------------|-----------|----------|
| Cost threshold == 0 | 8 | 4 | 1 MB |
| Cost threshold != 0 | 16 | 16 | 1 MB |

### Guidance consumption by the retry loop

The retry driver `sub_9714E0` (290 lines) triggers `sub_96D940` on each failed allocation attempt with a decremented register target (`alloc+1560 = target - 1`). The guidance engine builds the 11,112-byte structure, then the result is consumed in two phases depending on whether the current register class uses a "fast-path" mode (byte at `ctx + 32*class_id + 893`).

**Phase A -- Instruction walk and candidate insertion** (lines 1663--2757 of `sub_96D940`): walks the instruction list (`ctx+280`) and for each instruction with register operands calls `sub_9680F0` (per-instruction assignment) and `sub_9365A0` (candidate removal) to populate the per-class priority queues in the stack-local 7-entry array (`v514[3 * class_id]`).

**Phase B -- Guidance extraction** (`sub_93C0B0`, 582 lines): after the walk completes, the allocator reads the guidance output and marks VRs for spilling on the next attempt.

```
// sub_93C0B0 -- consume guidance to mark spill candidates
function extract_spill_marks(ctx, guidance, class_id):
    if ctx[32 * ctx.alloc_mode + 893]:             // fast-path class: skip
        return
    queue_base = guidance[13]                       // offset +104: candidate queue base
    pressure   = queue_base[3]                      // +12: max live pressure count
    if pressure <= 0:
        return

    // Build a 256-byte cost profile from spill/reload instruction pairs
    cost_profile[256] = {0}                         // stack-local
    overflow_bitvec = bitvec_alloc(pressure >> 2)   // sub_BDBA60
    insn = *(ctx+8).insn_list_head                  // ctx->code_obj+280

    // Walk instructions, accumulate per-slot cost
    for each insn in instruction_list:
        opcode = insn+72
        if opcode == 54:                            // BB_ENTRY marker
            // save/restore queue state from queue_base
            continue
        if opcode == 52:                            // BB_END marker
            bitvec_clear(overflow_bitvec)            // sub_BDC080
            // scan block's live-out set for VRs exceeding budget
            for vr in block_live_out(insn):
                if vr.class == class_id AND vr.assigned > ctx.attempt_budget:
                    if NOT vr.flags & 0x200:        // skip uniform registers
                        slot = 4 * (vr.assigned - ctx.spill_start)
                        if vr.flags & 0x800000: slot += 2  // paired-hi half
                        bitvec_set(overflow_bitvec, slot / 4)
                        if vr.flags & 0x300000 match: // double-width
                            bitvec_set(overflow_bitvec, slot/4 + 1)
            continue

        if opcode_masked == 183 OR opcode_masked == 288:  // STL/LDL spill ops
            // Match spill instruction signature against queue_base[0..1]
            operand_tail = insn+84 + 8*(insn.num_operands - adjust - 5)
            if operand_tail[0..7] != queue_base[0..1]:
                continue
            // Compute cost contribution from spill/reload width
            width    = (operand_tail[8*N] & 7) + 1     // register group size
            slot_idx = int24(operand_tail[-8]) / 4      // LMEM slot index
            cost     = vtable_call(ctx.profile, insn.block_id)  // block frequency
            cost_per_slot = (cost * width + 3) >> 2     // round-to-DWORD
            // Fill cost_profile[slot_idx .. slot_idx+cost_per_slot-1]
            for i in slot_idx .. min(slot_idx + cost_per_slot, 256):
                cost_profile[i] = max(cost_profile[i], cost_per_slot)

    // Phase 2: scan cost_profile to find compactable gaps
    max_slots = min((total_cost + 3) >> 2, 256)
    gap_start = -1
    relocation_map[256] = {0}                       // SSE2-vectorized fill

    for i in 0 .. max_slots:
        cost = cost_profile[i]
        if gap_start >= 0:
            if cost <= 0: continue                  // extend gap
            // Align gap_start to cost alignment boundary
            if cost == 2:      gap_start = (gap_start+1) & ~1
            elif cost in 2..4: gap_start = (gap_start+3) & ~3
            if gap_start < i:
                delta = 4 * (i - gap_start)
                vectorized_fill(relocation_map, i, cost, delta)  // SSE2 broadcast
                gap_start += cost
            else:
                gap_start = i + 1
        else:
            gap_start = i; i += 1

    // Patch STL/LDL operand offsets if compaction found savings
    savings = 4 * gap_start
    if savings < total_cost AND mode_permits_patching:
        for insn in instruction_list:
            if insn.opcode_masked in {183, 288}:
                operand = &insn.operands[tail_offset + 24]
                old_slot = int24(*operand)
                idx = old_slot >> 2
                if idx <= 255: new_slot = old_slot - relocation_map[idx]
                else:          new_slot = old_slot - 4*(256 - ((gap_start+3) & ~3))
                if old_slot != new_slot:
                    *operand = (new_slot & 0xFFFFFF) | (*operand & 0xFF000000)
        total_cost = savings

    queue_base[3] = total_cost                      // write back updated pressure
```

The outer retry loop in `sub_9714E0` reads the updated `queue_base[3]` pressure values. If pressure remains positive, it invokes `sub_96D940` recursively with `alloc+1560 = target - 1`, iterating until either (a) allocation succeeds within budget, (b) the retry count (knob 638/639) is exhausted, or (c) the fatal error fires:

```
// sub_9714E0 lines 99-264 -- recursive descent with guidance feedback
function finalize_with_spill(alloc, max_attempts, class_id):
    alloc.attempt_counter++                          // alloc+1524
    if alloc.attempt_counter >= max_attempts: return FAIL

    if NOT alloc.spill_done_flag:                    // alloc+865
        setup_spill_allocator(alloc)                 // sub_939BD0
        generate_spill_code(alloc, ctx, 1)           // sub_94F150
    alloc.current_attempt = alloc.attempt_counter + 1   // alloc+1516

    // Query profile for dynamic attempt limit override (vtable+848/856)
    if profile.can_retry(class_id, 0):
        alloc.spill_extra = profile.get_extra_attempts(alloc.current_attempt)

    build_interference(alloc, ctx)                   // sub_940EF0

    // Stall check: if extra attempts granted but acceptance gate fails,
    // undo extra -- walk VR list, decrement queue sizes, then rebuild
    if alloc.spill_extra > 0 AND NOT vtable[+280].accepts(alloc):
        alloc.spill_extra = 0
        for vr in vreg_list: queue_size[vr.class]--  // at profile+16*class+8
        build_interference(alloc, ctx)

    result = fatpoint_allocate(alloc, ctx, 99)       // sub_957160

    // Spill feasibility gate (when no progress between attempts)
    if result == alloc.last_result AND alloc.spill_extra > 0:
        non_uniform_spill = count(vr: vr.flags & 0x40000 AND NOT vr.flags & 0x200)
        threshold = profile.spill_threshold          // profile+47960
        budget    = ctx.class_info[alloc.active_class].max - .min + 1
        pressure  = (budget - spill_start) * interpolation_factor + spill_start_cost
        if (non_uniform_spill + threshold + budget) > pressure:
            force_extra_spill_marks(guidance)         // sub_948B80

    // Recursive descent: lower target by 1 and rebuild guidance
    if alloc.target != result OR alloc.spill_extra <= 0 OR gate_flags:
        alloc.spill_target = result - 1              // alloc+1560
        compute_spill_guidance(alloc, ctx, class_id) // sub_96D940 (recursive)
        profile.committed[class_id] = alloc.peak     // at profile+384+4*class_id
```

See [Spilling](./spilling.md) for the full spill subsystem analysis.

## Pre-Allocation and Mem-to-Reg

Two important pre-passes run before the main allocator:

### ConvertMemoryToRegisterOrUniform

Entry: `sub_910840` (327 lines). Promotes stack variables to registers or uniform registers. Gated by `sub_8F3EA0` (eligibility check) and `NumOptPhasesBudget` (knob 487, budget type).

**Eligibility check** (`sub_8F3EA0`, 96 lines) -- determines whether mem-to-reg is profitable:

```
is_promotable(code_obj):
  mode   = code_obj->ctx->options[33120]    // promotion mode byte
  budget = code_obj->ctx->options[33128]    // promotion budget (mode 1 only)
  if mode == 1:                             // conditional: check budget
    if budget == 0: return false
    if budget <= 2 AND code_obj[1412] < 0: return false
    if code_obj->alloc_mode in {4, 5}: return budget > 1
  else if mode != 0: return false

  if code_obj->alloc_mode NOT in {4, 5}:   // fast path: flag-based
    flags = code_obj[912]
    if flags == 0: return false
    if flags & 1: return code_obj[328] > 0  // has local variables
    return true

  // alloc_mode 4/5: walk BBs, sum operand sizes, cap at 64 KB
  total = 0
  for bb in code_obj->basic_blocks:
    for instr in bb->instrs:
      total += instr->operand_size_field
      if total > 0x10000: return false
  return total > 0
```

**Orchestrator** (`sub_910840`):

```
ConvertMemoryToRegisterOrUniform(code_obj):
  if NOT is_promotable(code_obj): return
  if NOT consume_budget(ctx, knob=487): return   // NumOptPhasesBudget
  candidates = build_promotion_candidates(code_obj)   // sub_905B50, 1046 lines
  analyze_def_use_chains(candidates)                   // sub_911030, 2408 lines
  execute_promotion(candidates)                        // sub_90FBA0, 653 lines
  rewrite_phis(candidates)                             // sub_914B40, 1737 lines
```

**Def-use analysis engine** (`sub_911030`, 2408 lines) -- core of the promotion decision. Numbers basic blocks sequentially (`bb[52] = index`, `bb[48] = -1`), then for each stack variable in `code_obj->field_99` walks the def-use chain. Uses the same `PREALLOC_MASK` bitmask (`0x2080000010000001 >> (opcode - 22)`) as the pre-allocator plus opcodes 297/352. Classifies register operands via per-opcode handlers: `sub_7E40E0` (R2P/22), `sub_7E36C0` (EXIT/77), `sub_7E3790` (UFMUL/297), `sub_7E3800` (SEL/352), `sub_7E3640` (TEX/83); ATOM/50 uses a packed lookup table. Builds a BST of def-use nodes keyed by BB index, tracking earliest/latest definitions to bound promotion scope.

### Pre-Allocation Pass

Entry: `sub_94A020` (331 lines). Assigns physical registers to high-priority operands before the main allocator runs.

```
pre_allocate(alloc):
  code_obj = alloc->code_obj
  ctx      = code_obj->ctx
  alloc->mac_force    = get_knob(ctx, 628)   // RegAllocMacForce
  alloc->coalescing   = get_knob(ctx, 618)   // RegAllocCoalescing
  alloc->vreg_order   = get_knob(ctx, 629)   // RegAllocMacVregAllocOrder
  alloc->uniform_mac  = (code_obj[1414] & 0x10) AND (code_obj[1381] & 0x40)

  // clear 3 candidate hash tables at alloc+240, +280, +320  (24 bytes each)
  for tbl in {alloc+240, alloc+280, alloc+320}:
    drain_freelist(tbl, alloc->arena)
    clear(tbl)

  if NOT (alloc->vreg_order OR alloc->uniform_mac): return
  if alloc->mode NOT in {3, 5, 6}: return

  // Phase 1: per-block scanning
  build_liveness(code_obj, 0, 0, 0, 0)       // sub_7E6090
  for bb in code_obj->basic_blocks: bb->field_80 = 0
  for bb in code_obj->basic_blocks:
    scan_block_for_prealloc(alloc, bb)        // sub_9499E0

  // Phase 2: per-instruction pre-assignment
  for insn in code_obj->instructions:
    opcode = insn->opcode & 0xFFFFCFFF        // mask out modifier bits 12-13
    if NOT is_prealloc_eligible_narrow(opcode):
      if opcode NOT in {51, 110..114, 279, 289}: continue     // wide-only set
      else: continue
    if alloc->mac_force:
      pre_assign_operands(alloc, insn, block_idx)   // sub_93ECB0
    else if knob_match(ctx, 646, insn):             // RegAllocPrefMacOperands
      priority = get_knob_value(ctx, 646, insn)
      if priority > 0 AND knob_match(ctx, 443, insn):
        pre_assign_operands(alloc, insn, block_idx)
    else if alloc->uniform_mac AND code_obj->reg_desc[insn->bb_idx]->field_148:
      if is_prealloc_eligible_narrow(opcode):       // bitmask-only recheck
        pre_assign_operands(alloc, insn, block_idx)
```

**Opcode eligibility bitmask** (constant `0x2080000010000001`, from `sub_94A020` lines 284/315):

```
PREALLOC_MASK = 0x2080000010000001    // 64-bit; bits {0, 28, 55, 61} set

// Narrow test: bitmask + two explicit opcodes (6 opcodes).
// Used for forced-MAC and uniform-MAC paths (sub_94A020 line 315).
is_prealloc_eligible_narrow(opcode):
  shifted = (uint32)(opcode - 22)
  if shifted < 62:
    if (PREALLOC_MASK >> shifted) & 1: return true
  return opcode == 297 OR opcode == 352

// Wide test: narrow + switch-case fallthrough (14 opcodes total).
// Used as the main eligibility gate (sub_94A020 line 284).
is_prealloc_eligible_wide(opcode):
  if is_prealloc_eligible_narrow(opcode): return true
  if opcode == 51: return true                      // AL2P
  if 110 <= opcode <= 114: return true              // CCTLT..SUATOM
  if opcode == 279 OR opcode == 289: return true    // FENCE_T, UISETP
  return false
```

Bitmask bit decode:

| Bit | Opcode | SASS mnemonic | Category |
|-----|--------|---------------|----------|
|  0  |  22    | R2P           | Predicate register unpack |
| 28  |  50    | ATOM          | Global/generic atomic |
| 55  |  77    | EXIT          | Thread exit |
| 61  |  83    | TEX           | Texture fetch |

Complete eligible set (14 opcodes across both tiers):

| Opcode | SASS | Tier | Per-operand handler |
|--------|------|------|---------------------|
| 22 | R2P | narrow (bitmask) | `sub_7E40E0` |
| 50 | ATOM | narrow (bitmask) | packed lookup table |
| 51 | AL2P | wide only | -- |
| 77 | EXIT | narrow (bitmask) | `sub_7E36C0` |
| 83 | TEX | narrow (bitmask) | `sub_7E3640` |
| 110 | CCTLT | wide only | -- |
| 111 | MEMBAR | wide only | -- |
| 112 | SULD | wide only | -- |
| 113 | SUST | wide only | -- |
| 114 | SUATOM | wide only | -- |
| 279 | FENCE_T | wide only | -- |
| 289 | UISETP | wide only | -- |
| 297 | UFMUL | narrow (explicit) | `sub_7E3790` |
| 352 | SEL | narrow (explicit) | `sub_7E3800` |

The "narrow" set (bitmask + explicit checks) gates the forced-MAC and uniform-MAC paths. The "wide" set additionally gates the knob-646 priority path. Priority levels from `RegAllocPrefMacOperands` (knob 646): `1` = read operands, `2` = write operands, `3` = both. See [algorithm.md](./algorithm.md) for the per-operand assigner `sub_93ECB0`.

## Live Range Infrastructure

An interval-based live range system at `0x994000`--`0x9A1000` (~80 functions) supports auxiliary operations. This is not the main allocator but feeds results into it:

| Subsystem | Range | Count | Key Functions |
|-----------|-------|-------|---------------|
| Live range primitives | `0x994000`--`0x996000` | ~25 | Constructor, interval queries, weight, color get/set |
| Interference graph | `0x996000`--`0x99A000` | ~18 | Node/edge construction, adjacency, degree, coloring |
| Range operations | `0x99C000`--`0x9A1000` | ~35 | Merge, split, interference add/remove, copy detection |
| Register coalescing | `sub_9B1200` | 1 | Copy elimination pass (800 lines) |
| Live range splitting | `sub_9AEF60` | 1 | Interference graph update (900 lines, self-recursive) |
| Range merge engine | `sub_9AD220` | 1 | Coalescing with cost heuristics (700 lines) |
| Range construction | `sub_9A5170` | 1 | Build ranges from def-use chains (750 lines) |

### Register Coalescing Engine (`sub_9B1200`, 800 lines)

Eliminates register-to-register copies by merging live ranges. Lines 1-100 compute an FNV-1a-variant hash (multiplier 1025, XOR-shift-6) over the operand stream, stored at `range_ctx+80` for dedup. Dispatches on mode at `range_ctx+8`:

```
coalesce_pass(alloc, range_ctx):
  hash = fnv1a_fold(alloc->code_obj)          // operand stream hash
  range_ctx->hash = hash                       // +80: dedup key
  mode = range_ctx->mode                       // +8: {1=single, 4=pair}
  if mode != 4 AND mode != 1: return
  if range_ctx->instr_count == 0: return

  max_copies = range_ctx->field_4 = instr_count - 2
  if max_copies <= 1: return cleanup(range_ctx)

  capped = min(max_copies, range_ctx->field_16)
  range_ctx->field_16 = capped                 // cap active window
  range_ctx->field_0 = capped * stride + 1     // stride = 3 if mode==4, else 2
  offset = range_ctx->field_12 % max_copies    // circular start

  // allocate scratch vregs for copy bridge
  src_vreg = alloc_vreg(code_obj, 5)           // sub_91BF30 class=5
  dst_vreg = alloc_vreg(code_obj, 6)           // class=6
  mark_constrained(src_vreg)                   // set bit 6 in flags qword

  // query knob overrides (knobs 531, 529, 530)
  if ctx_supports_knobs(code_obj):
    range_ctx->field_28 = query_knob(ctx, 531) // RegAllocCoalesceMaxFanout
    range_ctx->field_32 = query_knob(ctx, 529) // RegAllocCoalesceMaxDepth
    range_ctx->field_36 = query_knob(ctx, 530) // RegAllocCoalesceMaxWidth

  // walk circular instruction window, emit MOV/PRMT/SEL bridges
  for pos in circular_range(offset, capped, max_copies):
    instr = fetch_instr(code_obj, pos)
    if is_abi_boundary(instr): continue        // skip call/ret edges
    bb = lookup_bb(code_obj, instr)
    emit_copy_bridge(code_obj, range_ctx, instr, bb,
                     src_vreg, dst_vreg, mode)
    update_interference(alloc, range_ctx)       // sub_9262A0
  cleanup(range_ctx)                            // sub_99ABA0
```

#### Coalescing List Insert / Remove (`sub_948B80`)

Inserts a vreg index into the appropriate worklist. List A (+1280) serves mode-6 (paired); list B (+1344) all others. Nodes are 24-byte `{prev, next, vreg_idx}` from the arena at +1336/+1400.

```
insert_coalesce_candidate(alloc, vreg_idx):
  if alloc->alloc_mode == 6:                     // +1508
    list = alloc+1280; arena = alloc+1336; count = alloc+1328  // list A
  else:
    list = alloc+1344; arena = alloc+1400; count = alloc+1392  // list B
  node = arena_alloc_or_recycle(arena, 24)       // freelist at arena+8
  node->prev = 0; node->next = 0; node->vreg_idx = vreg_idx; ++*count
  if list->tail:                                 // prepend at head
    node->next = list+16; node->prev = list->head
    if list->head: list->head->next = node  else: list->tail = node
    list->head = node
  else: list->end = node; list->tail = node      // init empty list
```

Removal is implicit: the merge phase (`sub_9AD220`) drains list nodes, unlinks from hash buckets, returns sub-ranges to the arena freelist. The worklist at +1288 is a growable pointer array (`sub_8FD600`) consumed linearly.

#### Coalescing Candidate Filter Bits (`sub_99ECC0`)

Evaluates whether an instruction is a coalesceable copy. Rejection uses bits on `code_obj+1368`:

| Bit | Mask | Meaning | Set by |
|-----|------|---------|--------|
| 0 | `0x01` | Vreg crosses a call boundary | `sub_749090` |
| 1 | `0x02` | Vreg has a copy instruction | `sub_74A630` |
| 2 | `0x04` | Copy target in callee-saved class | `sub_74A630` |
| 3 | `0x08` | Copy source is predicated | `sub_74A630` |
| 4 | `0x10` | ABI-mandated physical register | `sub_68F690` |
| 5 | `0x20` | CSSA mode (phi-web coalescing) | `sub_67FC80` |
| 7 | `0x80` | Encoding-pinned (Mercury constraint) | `sub_6D9690` |

```
eval_coalesce_candidate(ctx, instr):
  flags = get_instr_flags(instr, code_obj)       // sub_7DF3A0
  REJECT if: flags & 0x01                        // bit 0: pre-colored
  REJECT if: (signed)flags < 0                   // bit 7: encoding-pinned
  REJECT if: (flags+1) & 0x01                    // secondary flag byte
  REJECT if: opc==195 AND subop==81              // SEL with callee-saved
             AND (code_obj+1386 & 0x10 OR code_obj+1368 & 0x04)
  REJECT if: flags & 0x08                        // bit 3: predicated
  if instr->field_13 == 1: return false          // already evaluated
  for slot in alloc->constraint_array[+1296..+1304]:
    if interference_test(code_obj, instr, slot): // sub_995840 + sub_9966A0
      alloc->coalesce_hit_count++; append_to_worklist(alloc+1288, &instr)
      return true                                // +1312 incremented
  instr->field_13 = 1; return true               // mark evaluated, no match
```

#### Interference Graph Query (`sub_95DC10` at +1416)

FNV-1a hash table with open chaining at +1408..+1432. Buckets: 24-byte `{chain_ptr, tail_ptr, local_count:u32}`. Nodes: 24-byte `{next_ptr, vreg_idx:u32, hash:u32, degree:u32}`. Grows at `count > capacity/2`.

```
query_or_insert_interference(alloc, vreg_idx, weight):
  if alloc->ig_base == NULL:                     // +1424
    grow_table(alloc+1408, 8)                    // sub_950570, initial 8 slots
  base = alloc->ig_base; cap = alloc->ig_capacity
  h = fnv1a_24bit(vreg_idx)                      // FNV-1a over 3 low bytes
  bucket = &base[24 * (h & (cap - 1))]
  for node = bucket->chain; node; node = node->next:
    if node->vreg_idx == vreg_idx:
      node->degree = max(node->degree, weight); return node
  new = arena_alloc_or_recycle(alloc+1408, 24)   // not found: insert
  new->hash = h; new->vreg_idx = vreg_idx; new->degree = weight
  new->next = bucket->chain                      // prepend
  if NOT bucket->chain: bucket->tail = new
  bucket->chain = new; bucket->local_count++
  alloc->ig_chain_sum += bucket->local_count     // +1420
  alloc->ig_count++                              // +1416
  if ig_chain_sum > ig_count AND ig_count > cap/2:
    grow_table(alloc+1408, 4*cap)                // 4x growth
  return new
```

At allocation start the drain loop (`sub_95DC10` line 1327) walks every bucket, returns chain nodes to `arena+8`, zeroes each slot, and resets `ig_count` to 0.

### Live Range Splitting Engine (`sub_9AEF60`, 900 lines)

Iterative fixed-point splitter, bounded by `alloc+45` (max iterations) and `alloc+46` (max rounds). Each round clears interference flags, drains prior worklists, then scans the vreg-to-physical bitvector for split candidates. Uses `sub_9AEC60` for profitability analysis and materializes splits as new 192-byte range nodes stored in an 8-bucket hash table keyed on `(range_id & 7)`.

```
split_ranges(alloc):
  flags = alloc->field_156
  alloc->split_uniform = (flags & 8) != 0      // +161
  alloc->split_pairs   = (flags & 4) != 0      // +160
  rebuild_liveness(alloc->code_obj, 1)          // sub_781F80
  rebuild_intervals(alloc->code_obj)            // sub_763070
  if code_obj->flags & 0x20:                    // ABI mode
    rebuild_abi_liveness(code_obj, 1)           // sub_763D80
    prepare_abi_split(alloc)                    // sub_9AB370

  round = 0; iter = 0; best_cost = 0.0
  while alloc->field_34 != 2:                   // not converged
    if iter >= alloc->max_iters OR round >= alloc->max_rounds:
      break

    // Phase 1: clear coalesce flags on all interference nodes
    if alloc->field_180:                        // has interference graph
      for node in interference_hash_iter(alloc+89):
        node->flags &= 0xF0                    // clear low nibble

    // Phase 2: invalidate best-program-point cache
    if alloc->field_177 < 0:
      alloc->field_87 = 0; alloc->field_177 = 0

    // Phase 3: drain split worklist → freelist
    if alloc->field_238:                        // worklist populated
      for bucket in alloc->split_buckets[+120]:
        for node in bucket:
          if node->program_point >= 0:
            node->program_point = -1            // invalidate
            if node->ref: vtable_release(node)  // vtable+32
          move_to_freelist(alloc->freelist, node)

    // Phase 4: drain secondary worklist → freelist
    drain_list(alloc+113, alloc->freelist_B)    // sub_69DD70

    // Phase 5: scan vreg bitvectors for split candidates
    for each vreg_desc in code_obj->reg_descs (reverse order):
      if NOT alloc->has_interference: continue
      lookup_interference(alloc+89, vreg_desc+24)
      if result.flags & 1:                      // constrained
        for coalesce_node in coalesce_hash_iter(alloc+165):
          if interferes(code_obj, vreg_desc, node->vreg):
            record_merge_candidate(alloc+170, vreg_desc)
            break

    // Phase 6: evaluate and apply best split
    candidate = pop_best(alloc+12, alloc+13)    // BST min
    while candidate != sentinel:
      profitability = sub_9AEC60(alloc, candidate)
      if NOT profitability.viable: break
      if profitability.cost > alloc->threshold:  // +41 (float)
        // record split into dynamic array (grow 1.5x)
        splits[split_count++] = candidate
        // materialize: allocate 192-byte range node, zero-init
        new_range = arena_alloc(alloc->arena, 192)
        // redistribute sub-ranges into 8-bucket hash
        for sub in old_range->sub_ranges:
          bucket_idx = sub->range_id & 7
          insert_bucket(new_range + 24*bucket_idx, sub)
        round += split_count
      candidate = next_bst(candidate)

    if best_cost > alloc->threshold:
      round += split_count
    else:
      break                                     // no profitable split
```

### Live Range Merge Engine (`sub_9AD220`, 700 lines)

Merges coalesced live ranges by walking the coalesce hash table, checking interference BST for conflicts, and relinking nodes. Returns a pointer to merge statistics.

```
merge_ranges(alloc, a2, a3, budget_lo, budget_hi, stats, ...):
  ctx = alloc + 83                              // coalesce context at +664
  ctx->field_936 = 0                            // reset merge counter
  sub_758AF0(ctx)                               // initialize merge state

  // Phase 1: scan coalesce hash table, remove safe merges
  if alloc->field_332 == 0 OR alloc->bucket_count == 0:
    goto phase2
  for bucket_idx, node in hash_iter(alloc->coalesce_buckets):
    vreg_src = code_obj->reg_desc[node->field_2]  // source vreg
    constraint_list = vreg_src + 128               // linked constraint chain
    if constraint_list == NULL: goto skip_merge

    // walk constraints, check interference BST for conflicts
    for constraint in constraint_list:
      target_vreg = code_obj->reg_desc[constraint->field_2]
      bst_node = node->interference_bst            // +16: BST root
      if bst_node == NULL: goto no_conflict

      // BST search by program point (field_6, 31-bit key)
      pp = constraint->field_2 >> 8
      while bst_node:
        bst_key = (2 * bst_node->field_6) >> 1     // mask sign bit
        if pp < bst_key:  bst_node = bst_node->left
        elif pp > bst_key: bst_node = bst_node->right
        else:                                       // exact match
          // check bit in 256-bit interference vector
          word_idx = (constraint->field_2 & 0xFF) >> 6
          bit_idx  = constraint->field_2 & 0x3F
          if bst_node->bitvec[word_idx + 4] & (1 << bit_idx):
            goto has_conflict
          break

    no_conflict:
      if NOT aliases_compatible(code_obj, vreg_src, target_vreg):
        goto has_conflict
      if target_vreg->spill_weight > vreg_src->spill_weight:  // +148
        goto has_conflict                       // reject: would increase pressure

      // safe to merge: unlink from hash bucket
      prev_node = update_bucket_head(alloc, bucket_idx, node)
      alloc->coalesce_count--                   // +332
      // drain node's sub-ranges → freelist
      drain_list(node + 2, node->freelist_ref)  // sub_69DD70
      release_arena(node + 6)                   // sub_661750
      // relink merged node to sentinel freelist
      link_to_sentinel(alloc->sentinel, node)   // +165
      continue

    has_conflict:
      skip_merge:
      advance_to_next(node)                     // follow bucket chain

  // Phase 2: process remaining merge worklist → freelist drain
  phase2:
  drain_list(alloc+170, alloc->freelist_C)      // secondary worklist

  // Phase 3: scan reg_descs for mergeable pairs with cost check
  for each vreg_idx in code_obj->reg_desc_order (reverse):
    if NOT alloc->has_interference: continue
    lookup_interference(alloc+89, vreg_desc+24)
    if result.flags & 1:                        // constrained
      for coalesce_node in coalesce_hash_iter(alloc+165):
        if interferes(code_obj, vreg_desc, node->vreg):
          record_merge(alloc+170, vreg_desc)
          break

  // Phase 4: compute merge statistics
  for each vreg_desc in code_obj->reg_descs:
    cost = compute_spill_cost(alloc, vreg_desc) * budget_lo
    if cost exceeds threshold: stats->rejected++
    // apply opcode-specific weight adjustments:
    //   opcode 130 (MOV): check operand type bits (bits 28..31)
    //   opcode 96  (COPY): always eligible
    //   opcode 137 (SHFL): check predicate flag
    stats->total_cost += cost
  *budget_hi = stats->total_cost
```

The interference BST uses 31-bit program-point keys with 256-bit bitvectors at each node, enabling O(log N) conflict checks. Constraint records are indexed via `register_class_constraints.json` (72 records per SM generation, 64-byte stride); auxiliary class metadata from `register_class_aux.json` (97 records for sm_10x, 64-byte stride with flag distribution: 88 flag=1, 9 flag=2) provides the sub-variant pairs used during merge eligibility checks.

## Allocator State Object Layout

Full reconstruction from the constructor `sub_947150` (1088 lines), cross-referenced with the core allocator, per-class driver, entry point, and spill subsystem. The object is at least 1748 bytes (last initialized field at +1744). The constructor is called once per function before the allocation pipeline runs.

### Header and Compilation Context (+0 -- +24)

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +0 | 8 | ptr | `&off_21E1648` | Vtable pointer (strategy dispatch, 40+ virtual methods) |
| +8 | 8 | ptr | arg | Compilation context (parent object) |
| +16 | 8 | ptr | `off_21DBEF8` | Secondary vtable (allocation sub-strategy) |
| +24 | 8 | ptr | `ctx->func` | Function object pointer (from `ctx+16`) |

### Pre-Allocation Candidate Tables (+32 -- +443)

Arena-allocated hash tables for pre-assigned registers. Each table is a 3-QWORD header `{base, size, capacity}` plus an arena node (24 bytes, allocated from the function memory pool with an incrementing class tag).

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +32 | 8 | ptr | 0 | Pre-alloc candidate list A head |
| +40 | 8 | ptr | 0 | Pre-alloc candidate list B head |
| +48 | 4 | DWORD | 0 | Pre-alloc candidate count A |
| +56 -- +208 | 160 | -- | 0 | Per-class registration slots (6 x {ptr, ptr, DWORD} = 24B each) |
| +216 | 8 | ptr | 0 | Registration slots tail |
| +224 | 8 | ptr | alloc(24) | Exclusion set arena node (class tag = 1) |
| +232 | 8 | ptr | alloc(24) | Pre-alloc hash table A arena node (class tag = 2) |
| +240 | 8 | ptr | 0 | Pre-alloc hash table A: base pointer |
| +248 | 8 | ptr | 0 | Pre-alloc hash table A: count |
| +256 | 8 | ptr | 0 | Pre-alloc hash table A: capacity |
| +272 | 8 | ptr | alloc(24) | Pre-alloc hash table B arena node |
| +280 | 24 | -- | 0 | Pre-alloc hash table B: {base, count, capacity} |
| +312 | 8 | ptr | alloc(24) | Pre-alloc hash table C arena node |
| +320 | 24 | -- | 0 | Pre-alloc hash table C: {base, count, capacity} |
| +352 | 8 | ptr | alloc(24) | Exclusion set hash table arena node (class tag = 3) |
| +360 | 8 | ptr | 0 | Exclusion set: base pointer |
| +368 | 8 | ptr | 0 | Exclusion set: count |
| +376 | 8 | ptr | 0 | Exclusion set: capacity |
| +384 | 4 | DWORD | 0 | Exclusion set: element count |
| +392 | 8 | ptr | =+352 | Exclusion alias A (points to same node) |
| +400 | 24 | -- | 0 | Exclusion secondary: {base, count, capacity} |
| +424 | 4 | DWORD | 0 | Exclusion secondary: element count |
| +432 | 8 | ptr | =+352 | Exclusion alias B |
| +440 | 1 | BYTE | 0 | MAC force pre-alloc flag (`RegAllocMacForce`, knob 628) |
| +441 | 1 | BYTE | 0 | Coalescing enable flag (`RegAllocCoalescing`, knob 618) |
| +442 | 1 | BYTE | 0 | MAC vreg alloc order (`RegAllocMacVregAllocOrder`, knob 629) |
| +443 | 1 | BYTE | 0 | Per-class mode flag (set by vtable+296 callback) |

### Per-Class Bitvector Sets (+448 -- +695)

An array of 6 bitvector set entries (one per allocatable register class, classes 1--6). Each entry is 40 bytes: a linked-list header `{head, data, tail, count}` (32 bytes) plus an arena node pointer (8 bytes). The arena nodes carry incrementing class tags (4, 6, 8, 10, 12, 14). The constructor loop starts at `+456` and increments by 40 until `+656`.

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +448 | 8 | QWORD | 0 -> 6 | Bitvector set count (incremented in init loop) |
| +456 | 240 | array | -- | 6 x BitvectorSet (40B each): classes 1--6 |
| +696 | 24 | -- | 0 | Remat candidate list: {base, data, tail} |
| +720 | 4 | DWORD | 0 | Remat candidate list: count |
| +728 | 8 | ptr | alloc(24) | Remat candidate arena node (class tag = 2) |

### Core Allocation State (+736 -- +872)

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +736 | 8 | ptr | 0 | Register linked list: secondary head |
| +744 | 8 | ptr | 0 | Register linked list head (main walk list for `sub_957160`) |
| +752 | 1 | BYTE | 0 | Register list initialized flag |
| +756 | 4 | DWORD | -1 | Hardware register limit (max physical regs, per-class) |
| +760 | 4 | DWORD | -1 | Secondary HW limit |
| +764 | 4 | DWORD | -1 | Pre-alloc constraint count |
| +776 | 8 | double | -1.0 | Spill cost threshold |
| +788 | 4 | DWORD | -1 | Best allocation result (reset to 0 per allocation round) |
| +792 | 1 | BYTE | 0 | Allocation-in-progress flag |
| +800 | 1 | BYTE | 0 | Retry-active flag |
| +808 | 4 | DWORD | (dynamic) | Live range interference state |
| +816 | 8 | ptr | (dynamic) | Live range secondary structure (4-byte DWORD array at +816) |
| +824 | 1 | BYTE | 0 | Pre-coloring done flag |
| +832 | 8 | ptr | 0 -> dyn | Per-function spill info array pointer |
| +840 | 8 | ptr | 0 -> dyn | Per-function spill info arena node |
| +848 | 8 | ptr | 0 | Spill info secondary |
| +856 | 8 | ptr | 0 | Spill info tertiary |
| +864 | 1 | BYTE | 0 | Bank conflict awareness flag |
| +865 | 1 | BYTE | 0 | Spill-already-triggered flag |
| +872 | 8 | ptr | 0 | Debug / trace output state |

### Per-Class Register File Descriptors (+880 -- +1103)

An array of 7 register class descriptors (one per class 0--6), each 32 bytes. Indexed as `alloc + 880 + 32 * class_id`. The per-class driver (`sub_971A90`) accesses max\_regs as `a1[32 * class_id + 884]` and base\_offset as `a1[32 * class_id + 880]`.

**RegClassDesc (32 bytes):**

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +0 | 4 | DWORD | 0 | Base register offset (first physical reg in class) |
| +4 | 4 | DWORD | -1 | Max regs / HW limit (set by `vtable[896]` init callback) |
| +8 | 4 | DWORD | 0 | Current allocation count |
| +12 | 1 | BYTE | 0 | Class active flag |
| +13 | 1 | BYTE | 0 | Class overflow flag |
| +14 | 1 | BYTE | 0 | Class spill flag |
| +15 | 1 | -- | -- | Padding |
| +16 | 4 | DWORD | 148 | Phase ID begin (148 = unset sentinel) |
| +20 | 4 | DWORD | 148 | Phase ID end (148 = unset sentinel) |
| +24 | 8 | QWORD | -1 | Class auxiliary link |

**Concrete addresses:**

| Class | Offset Range | Description |
|-------|-------------|-------------|
| 0 (unified) | +880 -- +911 | Cross-class (skipped in main loop) |
| 1 (R) | +912 -- +943 | GPR 32-bit |
| 2 (R alt) | +944 -- +975 | GPR variant |
| 3 (UR) | +976 -- +1007 | Uniform GPR |
| 4 (UR ext) | +1008 -- +1039 | Uniform GPR variant |
| 5 (P/UP) | +1040 -- +1071 | Predicate registers |
| 6 (Tensor) | +1072 -- +1103 | Tensor / accumulator |

### Extended Class Metadata (+1096 -- +1127)

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1096 | 8 | QWORD | -1 | Class 6 extended auxiliary link |
| +1104 | 8 | ptr | 0 | Extended class info: pointer A |
| +1112 | 8 | ptr | 0 | Extended class info: pointer B |
| +1120 | 4 | DWORD | 0 | Extended class info: count |

### Per-Class Rematerialization Lists (+1128 -- +1271)

Six rematerialization candidate lists (one per allocatable class), each 24 bytes `{ptr base, ptr data, DWORD count}`. Initialized to zero. Populated before the allocation loop in `sub_9721C0` for classes that support rematerialization.

| Class | Offset Range |
|-------|-------------|
| 1 | +1128 -- +1147 |
| 2 | +1152 -- +1175 |
| 3 | +1176 -- +1199 |
| 4 | +1200 -- +1219 |
| 5 | +1224 -- +1243 |
| 6 | +1248 -- +1267 |

### Coalescing / Live Range Lists (+1272 -- +1432)

Self-referential circular linked lists used for register coalescing and live range splitting. Each list has a sentinel structure where `prev` and `next` point into the list body.

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1272 | 8 | ptr | arg2 | Back-pointer to compilation context |
| +1280 | 8 | ptr | 0 | Coalesce list A: sentinel head |
| +1288 | 8 | ptr | self+1296 | Coalesce list A: prev (self-referential) |
| +1296 | 8 | ptr | self+1280 | Coalesce list A: next (circular) |
| +1304 | 8 | ptr | 0 | Coalesce list A: data |
| +1312 | 4 | DWORD | (checked) | Coalesce list A: count (bit 0 = non-empty flag) |
| +1320 | 8 | ptr | self+1296 | Coalesce list A: end marker |
| +1328 | 4 | DWORD | 2 | Coalesce list A: type tag |
| +1336 | 8 | ptr | alloc(24) | Coalesce list A: arena node |
| +1344 | 8 | ptr | 0 | Coalesce list B: sentinel head |
| +1352 | 8 | ptr | self+1360 | Coalesce list B: prev |
| +1360 | 8 | ptr | self+1344 | Coalesce list B: next |
| +1368 | 8 | ptr | 0 | Coalesce list B: data (bit 2 checked as ABI flag) |
| +1376 | 8 | ptr | self+1344 | Coalesce list B: tail |
| +1384 | 8 | ptr | self+1360 | Coalesce list B: end marker |
| +1392 | 4 | DWORD | 2 | Coalesce list B: type tag |
| +1400 | 8 | ptr | alloc(24) | Coalesce list B: arena node |
| +1408 | 8 | ptr | alloc(24) | Interference graph arena node (bit 1 = call-saved mode) |
| +1416 | 8 | ptr | 0 | Interference graph: base |
| +1424 | 8 | ptr | 0 | Interference graph: data (bit 7 checked in `sub_97EC60`) |
| +1432 | 8 | ptr | 0 | Interference graph: capacity |

### Debug / Rematerialization Infrastructure (+1440 -- +1496)

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1440 | 8 | -- | (tree) | Remat exclusion set (tree root, queried via `sub_99C5B0`) |
| +1448 | 1 | BYTE | 0 | Remat exclusion: active flag (checked in `sub_962840`, `sub_94E620`) |
| +1452 | 4 | DWORD | 0 | Remat exclusion: instruction threshold |
| +1464 | 16 | OWORD | 0 | Remat exclusion: data block B |
| +1472 | 8 | ptr | 0 | Remat candidate: linked list (freed in `sub_99D190`) |
| +1480 | 16 | -- | 0 | Remat candidate list (iterated by `sub_94BDF0`) |
| +1488 | 4 | DWORD | 0 | Remat candidate: count (checked in `sub_99C690`) |
| +1496 | 8 | ptr | 0 | Remat candidate: root pointer |

### Spill / Retry Control Block (+1504 -- +1594)

The core state for the NOSPILL / SPILL retry loop. Zeroed at allocation start, populated by the per-class driver (`sub_971A90`), read/written by the fat-point allocator (`sub_957160`).

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1504 | 4 | DWORD | 0 | Allocation mode (0=normal, 3=CSSA, 5=SMEM, 6=paired) |
| +1508 | 4 | DWORD | 0 | Spill attempt counter |
| +1512 | 4 | DWORD | 0 -> 44 | Spill instruction count (knob 635, default 44) |
| +1516 | 4 | DWORD | -1 | Budget lower bound |
| +1520 | 4 | DWORD | -1 | Budget lower bound secondary (part of 128-bit at +1516) |
| +1524 | 4 | DWORD | -1 | Register budget (from per-class desc max\_regs) |
| +1528 | 4 | DWORD | (dynamic) | Peak register usage (copied from +1532 per round) |
| +1532 | 16 | \_\_m128i | (global) | Strategy parameters (loaded from `xmmword_21E17F0`) |
| +1540 | 4 | DWORD | 0 | Secondary budget limit (knob 633) |
| +1544 | 4 | DWORD | 0 | Tertiary budget limit (knob 632) |
| +1548 | 4 | float | 4.0 | Spill cost multiplier (knob 680) |
| +1552 | 4 | DWORD | -1 | Rollback sentinel |
| +1556 | 4 | DWORD | -1 | Max regs aligned: `(budget + 4) & ~3` |
| +1560 | 4 | DWORD | -1 | Best result sentinel |
| +1564 | 4 | DWORD | 0 | Current max assignment (zeroed per allocation round) |
| +1568 | 8 | double | 0.0 | Total spill cost accumulator (zeroed per round) |
| +1576 | 4 | DWORD | 0 | Spill event counter (zeroed per round) |
| +1580 | 4 | DWORD | (dynamic) | Effective budget: `max(budget, SMEM_min)` |
| +1584 | 4 | DWORD | (dynamic) | Adjusted budget (from vtable+256 callback) |

### Mode Flags (+1588 -- +1594)

Knob-derived boolean flags controlling allocation strategy. When the function has more than one basic block (`sub_7DDB50 > 1`), flags +1588, +1589, +1590 are all forced to 1.

| Offset | Size | Type | Init | Knob | Field |
|--------|------|------|------|------|-------|
| +1588 | 1 | BYTE | 0 | 682 | Epoch-aware allocation mode |
| +1589 | 1 | BYTE | 0 | 683 | Paired-register allocation mode |
| +1590 | 1 | BYTE | 0 | 619 | SMEM spill enable |
| +1591 | 1 | BYTE | 0 | 627 | Bank-aware allocation |
| +1592 | 1 | BYTE | 0 | -- | Spill status / has-spilled flag |
| +1593 | 1 | BYTE | 1 | 636 | Precolor reuse (default enabled) |
| +1594 | 1 | BYTE | 1 | 649 | ABI compatibility (default enabled; cleared for small kernels) |

### Budget Pressure Model (+1600 -- +1744)

Occupancy-aware register budget interpolation. Computes a dynamic register budget based on thread occupancy, using knob-derived coefficients and a linear interpolation model. The slope at +1736 is `(coeffB - coeffC) / (maxOccupancy - minOccupancy)`, enabling the allocator to trade register count for occupancy.

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1600 | 8 | ptr | `ctx[2]->+208` | Function object pair pointer |
| +1608 | 8 | ptr | 0 | Budget model: auxiliary pointer |
| +1616 | 8 | QWORD | `0xFFFFFFFF` | Budget model: occupancy upper bound |
| +1624 | 4 | DWORD | 119 / knob | Max threads per block (default 119) |
| +1628 | 4 | DWORD | 160 / knob | Pressure threshold (default 160) |
| +1632 | 8 | double | 0.2 | Interpolation coefficient A (knob-overridable) |
| +1640 | 8 | double | 1.0 | Interpolation coefficient B (knob-overridable) |
| +1648 | 8 | double | 0.3 | Interpolation coefficient C (knob-overridable) |
| +1656 | 8 | double | (computed) | Total threads as double |
| +1664 | 8 | double | = coeff A | Interpolation point [0] |
| +1672 | 8 | double | (computed) | Interpolation point [1]: max\_threads as double |
| +1680 | 8 | double | = coeff A | Interpolation point [2] |
| +1688 | 8 | double | (computed) | Interpolation point [3]: threshold as double |
| +1696 | 8 | double | = coeff A | Interpolation point [4] |
| +1704 | 8 | double | (computed) | Interpolation point [5]: 255 minus vtable result |
| +1712 | 8 | double | = coeff B | Interpolation point [6] |
| +1720 | 8 | double | (computed) | Linear model: x\_min (thread count) |
| +1728 | 8 | double | = coeff C | Linear model: y\_min |
| +1736 | 8 | double | (computed) | Linear model: slope |
| +1744 | 8 | ptr | 0 | Budget model: tail sentinel |

#### Piecewise Budget Fraction Evaluation

The interpolation table at +1664 through +1712 encodes a 4-segment piecewise linear function mapping physical register count to a budget fraction in [0.0, 1.0]. The table stores (x, y) pairs as interleaved doubles: odd indices are x-coordinates (thread-count breakpoints), even indices are y-coordinates (fraction values). During initialization (`sub_947150`), points [0], [2], [4] are set to coefficient A (default 0.2) and point [6] is set to coefficient B (default 1.0), while the x-breakpoints come from the max-threads knob, the pressure-threshold knob, and a vtable-derived limit.

```
// sub_937E90 -- piecewise interpolation lookup (lines 112-136 of decompiled)
// Inputs: alloc = allocator context, reg_count = physical regs for this class
// Returns: fraction in [coeffA, coeffB], typically [0.2, 1.0]

double evaluate_budget_fraction(alloc, reg_count):
    total_threads = alloc[+1656]               // (double) from vtable+720, clamped
    x             = (double) reg_count          // register pressure as x-coordinate

    // Table layout (interleaved y, x, y, x, y, x, y):
    //   [0]=y0  [1]=x1  [2]=y1  [3]=x2  [4]=y2  [5]=x3  [6]=y3
    y0 = alloc[+1664]                          // = coeff A  (default 0.2)
    x1 = alloc[+1672]                          // = (double) max_threads_per_block
    y1 = alloc[+1680]                          // = coeff A  (default 0.2)
    x2 = alloc[+1688]                          // = (double) pressure_threshold
    y2 = alloc[+1696]                          // = coeff A  (default 0.2)
    x3 = alloc[+1704]                          // = 255.0 - vtable_adjustment
    y3 = alloc[+1712]                          // = coeff B  (default 1.0)

    if total_threads > x:                      // below minimum -- flat at y0
        return y0
    if x1 > x:                                 // segment 1: [total_threads .. max_threads]
        return y0 + (y1 - y0) / (x1 - total_threads) * (x - total_threads)
    if x2 > x:                                 // segment 2: [max_threads .. threshold]
        return y1 + (y2 - y1) / (x2 - x1) * (x - x1)
    if x3 > x:                                 // segment 3: [threshold .. vtable_limit]
        return y2 + (y3 - y2) / (x3 - x2) * (x - x2)
    return y3                                  // above vtable limit -- flat at coeffB
```

The caller in `sub_937E90` uses this fraction to decide whether to trigger a spill round. It computes `(available_regs + 1) * fraction` and compares against the sum of demanded registers plus a pair-widening penalty, returning true (needs-spill) when demand exceeds the fractional budget:

```
needs_spill = (pair_penalty + demanded + live_count)
            > (phys_range_end - phys_range_start + 1) * fraction
```

**Per-SM register-file parameters** (`extracted/occupancy_constants.json`, eight 16-byte rodata records starting at `0x229C400`):

| Label | VA | Granularity mask | Half regfile | Reg cap | Max warps / SM |
|-------|----|-------------------|--------------|---------|----------------|
| sm53--sm62 regfile | `0x229C440` | -- | 128 | 65536 / 256 | 255 cap |
| sm60--sm70 max warps | `0x229C430` | -- | -- | -- | 32 / 32 / 64 / 32 |
| sm35--sm37 max warps | `0x229C450` | -- | -- | -- | 32 / 32 / 48 / 16 |
| sm3x--sm5x max warps | `0x229C460` | -- | -- | -- | 32 / 32 / 48 / 24 |
| sm70+ granularity | `0x229C470` | 80 | -- | -- | per-warp offset 16 |
| sm90 regfile | `0x229C400` | -- | 128 | 32768 / 256 | 255 cap |
| sm90 granularity | `0x229C410` | 63 | -- | -- | per-warp offset 16 |
| barrier / CTA limits | `0x229C420` | -- | -- | -- | 4 barriers, 2048 threads, 8 CTAs, 2 reserved |

The half-regfile value (128 for sm90, 256 for sm53--sm62) is `total_regs / 256` rounded for the per-warp slice. The 255 GPR cap in field [3] is the architectural ceiling reported via the `Used %d registers` diagnostic and the upper bound enforced after `--maxrregcount` clamping.

**Occupancy formula** (`sub_A99FE0`, 7 instructions). The maximum warp occupancy for a given register count is:

```
// Profile fields (set by sub_AAFCF0 from the arch profile vtable):
//   profile[+1440] = granularity_mask   (e.g. 63 for sm90)
//   profile[+1460] = half_reg_file_size (e.g. 128 for sm90 = 32768/256)
//   profile[+636]  = warp_offset        (e.g. regfile_params[3]=255 mapped)

max_warps(regs) = (-granularity_mask & (2 * half_reg_file_size / regs)) - warp_offset
```

For sm90: `granularity_mask=63`, `half_reg_file=128`, so with 64 registers: `(-63 & (256/64)) - offset = (-63 & 4) - offset`. The negation creates a bitmask that rounds down to the granularity boundary, enforcing the hardware's allocation granularity constraint.

The linear model at +1720/+1728/+1736 provides an equivalent fast path used in the main allocation loop (`sub_96D940`), where `fraction = (thread_count - x_min) * slope + y_min` with `slope = (coeffB - coeffC) / (maxOccupancy - minOccupancy)`.

### External Budget Inputs: `--maxrregcount`, `setmaxnreg`, `register-usage-level`

The HW limit (`alloc+756`) and the per-class `RegClassDesc.max_regs` field (`+884 + 32*class_id`) are computed before allocation from three ordered inputs that clamp the same field:

| Source | Front-end token | Effect on budget |
|--------|------------------|------------------|
| Architectural cap | -- | 255 (GPR), 63 (UR), 7 (P/UP). Field [3] of `regfile_params`. |
| `--maxrregcount=N` | `-maxrregcount` | Global cap. Diagnostic `"Too big maxrregcount value specified %d, will be ignored"` fires when `N > 255`; `"global register limit specified"` records the clamped value. Values below the ABI floor are bumped up silently. |
| `--device-function-maxrregcount=N` | `device-function-maxrregcount` | Overrides `-maxrregcount` for `-c` compilations; ignored for whole-program. Diagnostic `"Overriding global maxrregcount %d with entry-specific value %d %s"` records the substitution. |
| `.maxnreg N` (PTX directive) | `of .maxnreg` | Per-function override; takes precedence over the CLI value when `--override-directive-values` is not set. |
| `.minnctapersm N` (PTX) | `minnctapersm` | Drives occupancy target. Ignored if `-maxrregcount` is also given (diagnostic `"... ignored if -maxrregcount option is used"`). |
| `setmaxnreg.inc/dec` (SASS) | `setmaxnreg.inc/dec` | Runtime per-warp register pool adjustment on sm90+. Validated against the static budget; see the bail-out diagnostics below. |
| `--register-usage-level=[0..10]` | `register-usage-level` | Bias knob (default 5). Maps to OCG knob index that scales the spill-cost multiplier at `alloc+1548` (default 4.0). Higher values trade extra registers for fewer spills. |

`setmaxnreg.inc/dec` carries five rejection paths, all emitting `Potential Performance Loss:`:
- `'setmaxnreg' ignored to allow debugging.` (debug builds disable register repartitioning)
- `'setmaxnreg' ignored to maintain compatibility across compilation units.` (separately-compiled callers)
- `'setmaxnreg' ignored; unable to determine register count at entry.` (no static peak)
- `'setmaxnreg' ignored to maintain compatibility into 'extern' call.` (external call edge)
- `'setmaxnreg' ignored to maintain minimum register requirements.` (ABI floor violated)

Two hard errors validate consistency with launch bounds:
- `setmaxreg.dealloc/release has register count (%d) less than launch min target (%d) allowed.`
- `setmaxnreg.dec has register count (%d) which is larger than the largest temporal register count in the program (%d)`

The patch-code path (`--compile-as-tools-patch`) forces the budget to the ABI minimum regardless of `-maxrregcount`. The shape-incompatibility error `"Function '%s' using feature '%s' and with shape '%s' cannot be compiled with specified register target constraints"` fires when the resolved per-function budget cannot satisfy the active feature set (tensor-core groups, paired-mode constraints, etc.).

## Virtual Register Object Layout

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | Next pointer (linked list) |
| +12 | 4 | Register class index |
| +20 | 1 | Flags byte (bit 0x20 = live) |
| +36 | 8 | Alias chain (coalesced parent) |
| +40 | 4 | Spill cost (float, accumulated) |
| +48 | 8 | Flags qword (see below) |
| +64 | 4 | Register type (1=GPR, 3=pred, 9=barrier) |
| +68 | 4 | Physical assignment (-1 = unassigned) |
| +72 | 1 | Size byte (0 = scalar) |
| +76 | 4 | Secondary spill cost (float) |
| +80 | 4 | Spill flag (0 = not spilled, 1 = spilled) |
| +104 | 8 | Use chain head |
| +112 | 8 | Def chain |
| +128 | 8 | Next in linked-register chain |
| +144 | 8 | Constraint list |

**Flag bits at +48:**

| Bit | Mask | Meaning |
|-----|------|---------|
| 9 | `0x200` | Pre-assigned / fixed register |
| 10 | `0x400` | Coalesced source |
| 11 | `0x800` | Coalesced target |
| 14 | `0x4000` | Spill marker |
| 18 | `0x40000` | Needs-spill flag |
| 20--21 | -- | Pair mode (0=single, 1=lo-half, 3=double-width) |
| 22 | `0x400000` | Constrained to architecture limit |
| 23 | `0x800000` | Hi-half of pair |
| 27 | `0x8000000` | Special handling flag |

## Key Knobs

87 OCG knobs (indices 613--699) control register allocation heuristics. The complete catalog with sub-category grouping is in [Knobs System -- Register Allocation Knobs](../config/knobs.md#register-allocation-knobs-87-knobs-indices-613699). The most important ones:

| Knob | Name | Type | Role |
|------|------|------|------|
| 381 | *(not yet decoded)* | -- | HoistInvariants policy: 0=always, 1=inner loops, 3=never |
| 487 | `NumOptPhasesBudget` | BDGT | Budget counter that gates ConvertMemoryToRegisterOrUniform |
| 618 | `RegAllocCoalescing` | bool | Enables register coalescing in the allocator |
| 623 | `RegAllocEstimatedLoopIterations` | STR | Loop iteration estimate hint for spill cost weighting |
| 628 | `RegAllocMacForce` | bool | Forces MAC-level pre-allocation path |
| 629 | `RegAllocMacVregAllocOrder` | INT | Vreg processing order during MAC allocation |
| 638 | `RegAllocNoRetargetPrefs` | bool | Disables retarget-preference optimization |
| 639 | `RegAllocNumNonSpillTrials` | INT | Non-spill allocation trials before allowing spills |
| 646 | `RegAllocPrefMacOperands` | INT | MAC operand preference level (1=read, 2=write, 3=both) |
| 684 | `RegAllocThresholdForDiscardConflicts` | INT | Interference discard threshold. Default 50 |
| 934 | `UseNewLoopInvariantRoutineForHoisting` | bool | Selects new LICM routine for HoistInvariants pre-pass |

## Function Map

| Address | Lines | Role |
|---------|-------|------|
| `sub_8FFDE0` | 119 | HoistInvariants entry |
| `sub_905B50` | 1046 | Mem-to-reg candidate builder |
| `sub_910840` | 327 | ConvertMemoryToRegisterOrUniform entry |
| `sub_911030` | 2408 | Mem-to-reg analysis engine |
| `sub_914B40` | 1737 | Post-promotion rewrite |
| `sub_926A30` | 4005 | Fat-point interference builder |
| `sub_947150` | 1088 | Allocator state constructor (initializes 1748-byte object) |
| `sub_939BD0` | 65 | Spill allocator setup |
| `sub_939CE0` | 23 | Register consumption counter |
| `sub_93D070` | 155 | Best result recorder |
| `sub_93ECB0` | 194 | Pre-assign registers |
| `sub_93FBE0` | 940 | 6-list allocation ordering (simplify-select classification) |
| `sub_94A020` | 331 | Pre-allocation pass |
| `sub_94E620` | 617 | Spill cost accumulator |
| `sub_94F150` | 561 | Spill code generation |
| `sub_94FDD0` | 155 | Register assignment + alias propagation |
| `sub_950100` | 205 | Pre-allocated candidate applier |
| `sub_957160` | 1658 | Core fat-point allocator |
| `sub_9539C0` | 1873 | Shared-memory spill allocator |
| `sub_95A350` | 1390 | Cost / benefit evaluator |
| `sub_95BC90` | 1250 | Allocation retry / refinement |
| `sub_95DC10` | 2738 | Multi-class ABI-aware driver |
| `sub_9680F0` | 3722 | Per-instruction assignment core loop |
| `sub_96D940` | 2983 | Spill guidance (7-class priority queues) |
| `sub_971A90` | 355 | NOSPILL / SPILL retry driver |
| `sub_9721C0` | 1086 | Register allocation entry point |
| `sub_991790` | 2677 | Pre-coloring pass |
| `sub_9A5170` | 750 | Live range construction |
| `sub_9AD220` | 700 | Live range merge / coalescing engine |
| `sub_9AEF60` | 900 | Live range splitting |
| `sub_9B1200` | 800 | Register coalescing / copy elimination |
