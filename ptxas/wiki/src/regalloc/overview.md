# Allocator Architecture

The ptxas register allocator is a fat-point greedy allocator, not a graph-coloring allocator. There is no interference graph, no Chaitin-Briggs simplify-select-spill loop, and no graph coloring in the main allocation path. Instead, the allocator maintains per-physical-register pressure histograms (512-DWORD arrays) and greedily assigns each virtual register to the physical slot with the lowest interference count. This design trades theoretical optimality for speed on the very large register files of NVIDIA GPUs (up to 255 GPRs per thread).

A secondary live-range-based infrastructure (~80 functions at `0x994000`--`0x9A1000`) supports coalescing, splitting, and pre-coloring but feeds results into the fat-point allocator rather than replacing it.

| | |
|---|---|
| **Entry point** | `sub_9721C0` (1086 lines) |
| **Per-class driver** | `sub_971A90` (355 lines) -- NOSPILL then SPILL retry |
| **Core allocator** | `sub_957160` (1658 lines) -- fat-point coloring engine |
| **Assignment** | `sub_94FDD0` (155 lines) -- write physical reg, propagate aliases |
| **Spill guidance** | `sub_96D940` (2983 lines) -- per-class priority queues |
| **Spill codegen** | `sub_94F150` (561 lines) -- emit spill/reload instructions |
| **Pre-coloring** | `sub_991790` (2677 lines) -- full-function pre-assignment |
| **Address range** | `0x8FE000` -- `0x9D3000` (~860 KB, ~950 functions) |
| **Knobs** | 73+ OCG knobs (`RegAlloc*` prefix) |

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
      Core fat-point allocator       <-- sub_957160
  Post-allocation fixup
  Instruction scheduling
  SASS encoding
```

## Register Classes

The allocator processes 7 register classes. Class 0 (unified) is skipped in the normal per-class loop; it is used for cross-class constraint propagation. Classes 1--6 are allocated independently in order:

| ID | Name | Width | HW Limit | Description |
|----|------|-------|----------|-------------|
| 0 | -- | -- | -- | Unified / cross-class (skipped in main loop) |
| 1 | R | 32-bit | 255 | General-purpose registers (R0--R254) |
| 2 | R (alt) | 32-bit | 255 | GPR variant (RZ sentinel, stat collector alternate) |
| 3 | UR | 32-bit | 63 | Uniform general-purpose registers (UR0--UR62) |
| 4 | UR (ext) | 32-bit | 63 | Uniform GPR variant (extended uniform) |
| 5 | P / UP | 1-bit | 7 | Predicate registers (P0--P6, UP0--UP6) |
| 6 | Tensor/Acc | 32-bit | varies | Tensor/accumulator registers (MMA/WGMMA) |

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
    max_attempts = query_knob(638)                    // default varies
    attempt_limit = query_knob(639)

    // Phase 1: NOSPILL
    pre_allocation_pass(alloc_state)                  // sub_94A020
    secondary_driver(alloc_state, ctx)                // sub_95DC10
    result = fatpoint_allocate(alloc_state, ctx, NOSPILL)  // sub_957160
    record_best_result(alloc_state, result)            // sub_93D070

    if result == SUCCESS:
        return

    // Phase 2: SPILL retry loop
    for attempt in 1..max_attempts:
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
    maxRegs = alloc_state.hw_limit + 7               // from alloc+756
    if mode == CSSA_PAIRED (6):  maxRegs *= 2
    if mode == CSSA (3):         maxRegs *= 4

    primary[512]   = {0}                              // SSE2 memset
    secondary[512] = {0}

    threshold = query_knob(684)                       // default 50

    for each vreg in alloc_state.register_list:       // linked list at +744
        // Populate interference bitmaps for this vreg
        build_interference_bitmaps(vreg, primary, secondary)   // sub_957020

        // Scan for minimum-pressure physical register
        best_slot = -1
        best_cost = MAX_INT
        for slot in 0..maxRegs:
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

The interference threshold (knob 684, default 50) is the key heuristic parameter. Slots with interference above this value are skipped entirely, forcing the allocator toward less-contested register slots even if they are not globally minimal.

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

Spill setup (`sub_939BD0`, 65 lines) selects configuration based on OCG knob 623 and the cost threshold at `alloc+776`:

| Condition | Bucket size | Alignment | Max size |
|-----------|-------------|-----------|----------|
| Cost threshold == 0 | 8 | 4 | 1 MB |
| Cost threshold != 0 | 16 | 16 | 1 MB |

See [Spilling](./spilling.md) for the full spill subsystem analysis.

## Pre-Allocation and Mem-to-Reg

Two important pre-passes run before the main allocator:

### ConvertMemoryToRegisterOrUniform

Entry: `sub_910840` (327 lines). Promotes stack variables to registers or uniform registers. Gated by `sub_8F3EA0` (eligibility check) and OCG knob 487.

```
sub_910840 (entry, string: "ConvertMemoryToRegisterOrUniform")
  sub_905B50 (1046 lines)  build promotion candidates
  sub_911030 (2408 lines)  detailed analysis engine (def-use chains, dominance)
  sub_90FBA0 (653 lines)   execute promotion, insert phi nodes
  sub_914B40 (1737 lines)  post-promotion rewrite / phi-resolution
```

### Pre-Allocation Pass

Entry: `sub_94A020` (331 lines). Assigns physical registers to high-priority operands before the main allocator runs. Gated by knobs 628 (enable), 629 (coalescing), and 618 (uniform).

For allocation modes 3, 5, or 6: iterates basic blocks calling `sub_9499E0` (per-block scanner) and `sub_93ECB0` (per-operand pre-assigner). Priority levels from knob 646: `1` = read operands, `2` = write operands, `3` = both.

Uses an opcode eligibility bitmask table (shift-based membership test on `opcode - 22`) to filter which instructions are candidates for pre-assignment.

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

## Allocator State Object Layout

Partial reconstruction of the main allocator state object, consistent across all allocation functions:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 8 | Vtable pointer (strategy dispatch) |
| +8 | 8 | Function object pointer |
| +240 | 8 | Pre-alloc candidate count |
| +248 | 8 | Pre-alloc hash table base (FNV-1a keyed) |
| +360 | 8 | Exclusion set hash table |
| +440 | 1 | Enable pre-alloc flag (knob 628) |
| +441 | 1 | Enable uniform flag (knob 618) |
| +442 | 1 | Enable coalescing flag (knob 629) |
| +744 | 8 | Register linked list head |
| +756 | 4 | Hardware register limit |
| +776 | 8 | Spill cost threshold (double) |
| +864 | 1 | Bank conflict awareness flag |
| +865 | 1 | Spill-already-triggered flag |
| +1504 | 4 | Allocation mode (3=CSSA, 5=SMEM, 6=paired) |
| +1524 | 4 | Register budget |
| +1528 | 4 | Peak register usage (non-spill) |
| +1564 | 4 | Current max assignment |
| +1568 | 8 | Total spill cost (double accumulator) |
| +1580 | 4 | Current register count |
| +1588 | 1 | Epoch-aware mode flag |
| +1589 | 1 | Paired-register mode flag |

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

73+ OCG knobs control register allocation heuristics. The most important ones identified:

| Knob | Role | Default / Notes |
|------|------|-----------------|
| 381 | HoistInvariants policy | 0=always, 1=inner loops, 3=never |
| 487 | ConvertMemoryToRegisterOrUniform enable | -- |
| 618 | Uniform register pre-allocation enable | -- |
| 623 | Spill mode selector | Value at offset+224 = spill limit |
| 628 | Pre-allocation pass enable | -- |
| 629 | Coalescing pre-allocation enable | -- |
| 638 | Max spill attempts | -- |
| 639 | Spill attempt limit | -- |
| 646 | Pre-assign priority | 1=read, 2=write, 3=both |
| 684 | Interference threshold | Default 50 |
| 934 | Per-block allocation tracking | -- |

## Function Map

| Address | Lines | Role |
|---------|-------|------|
| `sub_8FFDE0` | 119 | HoistInvariants entry |
| `sub_905B50` | 1046 | Mem-to-reg candidate builder |
| `sub_910840` | 327 | ConvertMemoryToRegisterOrUniform entry |
| `sub_911030` | 2408 | Mem-to-reg analysis engine |
| `sub_914B40` | 1737 | Post-promotion rewrite |
| `sub_926A30` | 4005 | Fat-point interference builder |
| `sub_939BD0` | 65 | Spill allocator setup |
| `sub_939CE0` | 23 | Register consumption counter |
| `sub_93D070` | 155 | Best result recorder |
| `sub_93ECB0` | 194 | Pre-assign registers |
| `sub_93FBE0` | 940 | Spill slot assignment |
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
