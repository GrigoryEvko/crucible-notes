# Allocator Architecture

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

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
    maxRegs = alloc_state.hw_limit + 7               // from alloc+756
    if mode == CSSA_PAIRED (6):  maxRegs *= 2
    if mode == CSSA (3):         maxRegs *= 4

    primary[512]   = {0}                              // SSE2 memset
    secondary[512] = {0}

    threshold = query_knob(684)                       // RegAllocThresholdForDiscardConflicts, default 50

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

See [Spilling](./spilling.md) for the full spill subsystem analysis.

## Pre-Allocation and Mem-to-Reg

Two important pre-passes run before the main allocator:

### ConvertMemoryToRegisterOrUniform

Entry: `sub_910840` (327 lines). Promotes stack variables to registers or uniform registers. Gated by `sub_8F3EA0` (eligibility check) and `NumOptPhasesBudget` (knob 487, budget type).

```
sub_910840 (entry, string: "ConvertMemoryToRegisterOrUniform")
  sub_905B50 (1046 lines)  build promotion candidates
  sub_911030 (2408 lines)  detailed analysis engine (def-use chains, dominance)
  sub_90FBA0 (653 lines)   execute promotion, insert phi nodes
  sub_914B40 (1737 lines)  post-promotion rewrite / phi-resolution
```

### Pre-Allocation Pass

Entry: `sub_94A020` (331 lines). Assigns physical registers to high-priority operands before the main allocator runs. Gated by `RegAllocMacForce` (knob 628, bool), `RegAllocMacVregAllocOrder` (knob 629, int), and `RegAllocCoalescing` (knob 618, bool).

For allocation modes 3, 5, or 6: iterates basic blocks calling `sub_9499E0` (per-block scanner) and `sub_93ECB0` (per-operand pre-assigner). Priority levels from `RegAllocPrefMacOperands` (knob 646): `1` = read operands, `2` = write operands, `3` = both.

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
