# Spill Mechanism

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

When the fat-point register allocator cannot fit all simultaneously-live virtual registers into the hardware register budget, it spills excess values to memory and reloads them on demand. The spill subsystem is the second-largest component of the register allocator by code volume, spanning roughly 25 functions and 12,000+ lines of decompiled code. It implements a cost-driven, retry-based spill strategy with two memory targets (local memory and shared memory), a per-class priority queue guidance engine, and a multi-attempt allocation loop that progressively relaxes constraints until allocation succeeds or fails fatally.

| | |
|---|---|
| **Spill trigger** | `sub_94FDD0` (155 lines) -- sets flag `0x40000` when assignment exceeds budget |
| **Spill guidance** | `sub_96D940` (2983 lines) -- builds 7 priority queues of spill candidates |
| **Spill codegen** | `sub_94F150` (561 lines) -- inserts spill stores and refill loads |
| **LMEM setup** | `sub_939BD0` (65 lines) -- local memory slot allocator configuration |
| **SMEM allocator** | `sub_9539C0` (1873 lines) -- shared memory spill alternative |
| **Retry driver** | `sub_971A90` (355 lines) -- NOSPILL then SPILL retry loop |
| **Finalization** | `sub_9714E0` (290 lines) -- commit spills, emit errors on failure |
| **SASS codegen** | `sub_9850F0` (520 lines) -- generate LDL/STL instruction sequences |
| **Key knobs** | 623 (spill mode), 638/639 (retry limits), 684 (interference threshold) |

## Spill Trigger

The spill trigger fires inside the per-virtual-register assignment function `sub_94FDD0` (155 lines). When the fat-point allocator (`sub_957160`) selects a physical slot for a virtual register, it calls `sub_94FDD0` to commit the assignment. If the chosen slot index equals or exceeds the per-class register budget, the function does not commit -- instead it marks the virtual register for spilling.

```c
function assign_register(alloc, ctx, mode, vreg, regclass_info, slot, cost):
    max_regs = regclass_info.max_regs           // at regclass_info+16

    // Budget check
    if slot >= max_regs and not vreg.has_flag(0x4000):  // not already spilled
        vreg.flags |= 0x40000                    // set "needs-spill" bit
        return                                   // do NOT commit assignment

    // Spill path: flag was set on a previous call
    if vreg.has_flag(0x40000):                   // needs-spill
        setup_spill_allocator(alloc)             // sub_939BD0
        generate_spill_code(alloc, ctx, 1)       // sub_94F150
        return

    // Non-spill path: commit the assignment
    consumption = compute_consumption(vreg)       // sub_939CE0
    update_peak(alloc+1564, consumption)
    update_peak(alloc+1528, consumption)
    vreg.physical_register = slot                 // vreg+68

    // Accumulate spill cost even for successful assignments
    *(double*)(alloc+1568) += *(float*)(vreg+40)  // store weight
    *(float*)(alloc+1576)  += load_weight          // load weight

    apply_preallocated_candidate(alloc, vreg)     // sub_950100

    // Propagate through alias chain
    alias = vreg.alias_parent                     // vreg+36
    while alias != NULL:
        alias.physical_register = slot
        alias = alias.alias_parent
```

The two flag bits at `vreg+48` encode spill state:

| Bit | Mask | Meaning |
|-----|------|---------|
| 14 | `0x4000` | Already spilled -- prevents the same vreg from being spilled again |
| 18 | `0x40000` | Needs spill -- triggers spill codegen on the next `sub_94FDD0` call |

Register consumption (`sub_939CE0`, 23 lines) accounts for paired registers. For double-width registers (pair mode 3 at `vreg+48` bits 20--21), it returns `assignment + 1`, consuming two physical slots.

## Spill Retry Loop

The outer allocation driver `sub_971A90` (355 lines) wraps the core allocator in a two-phase strategy: first attempt allocation without spilling, then retry with progressively more aggressive spill guidance.

```c
function alloc_with_spill_retry(alloc, ctx, class_id):
    // Phase 1: NOSPILL
    pre_allocation_pass(alloc)                        // sub_94A020
    per_class_driver(alloc, ctx, class_id)            // sub_95DC10
    result = fatpoint_allocate(alloc, ctx, attempt=0) // sub_957160
    record_best_result(&best, class_id, 0, result)    // sub_93D070

    if alloc.target >= adjusted_result:
        goto finalize                                 // success

    // Phase 2: SPILL retry
    max_attempts = query_knob(638)     // default varies
    if knob_639_set:
        max_attempts = query_knob(639) // override

    for attempt in 0..max_attempts:
        reset_alloc_state(alloc, ctx, attempt)        // sub_93FBE0
        if attempt == 0:
            build_interference_masks(alloc, class_id) // sub_956130
        result = fatpoint_allocate(alloc, ctx, attempt)

        debug("-CLASS NOSPILL REGALLOC: attemp %d, used %d, target %d",
              attempt, result, alloc.target)

        record_best_result(&best, class_id, attempt, result)
        if alloc.target >= adjusted_result:
            break

    if all_attempts_failed and no_spill_recorded:
        result = final_fallback(&best, result)        // sub_936FD0

    // Finalize
    status = finalize_allocation(alloc, result, class_id, &best)  // sub_9714E0
    if HIBYTE(status):
        clear_all_assignments_to_minus_one()          // allocation failed
    else:
        commit_results()
```

The debug string `"-CLASS NOSPILL REGALLOC: attemp "` (note the typo -- present in the binary) is printed for every attempt.

For SMEM spilling (modes 3/6 when `ctx+896 == 5`), the driver activates spill setup before entering the retry loop:

```c
if (class_id == 3 or class_id == 6) and device_type == 5:
    if vreg_count > 0:
        setup_spill_allocator(alloc)      // sub_939BD0
        generate_spill_code(alloc, ctx, 1) // sub_94F150
    alloc.spill_done_flag = 1             // alloc+865
```

## Spill Guidance Engine

`sub_96D940` (2983 lines, 84 KB decompiled) computes which registers should be spilled and in what order. It is the largest spill-related function and one of the largest in the entire allocator.

### Structure

The function contains 7 near-identical code blocks, one per register class (R, P, B, UR, UP, UB, tensor/accumulator). Each block is approximately 400 lines of bitvector iteration and set intersection. This repetition strongly suggests C++ template instantiation or macro expansion in the original source.

### Spill Guidance Structure (11,112 bytes)

The guidance engine allocates a single 11,112-byte working structure from the arena (vtable `+24`). The structure is organized into five regions.

**Region 0 -- Header and core pointers (bytes 0--271)**

| Byte offset | QWORD idx | Type | Init | Field |
|-------------|-----------|------|------|-------|
| 0 | [0] | ptr | ctx | Back-pointer to allocation context |
| 24 | [3] | ptr | alloc+16 | Pointer into allocator state object |
| 32 | [4] | QWORD | 0 | Run counter / iteration state |
| 40 | [5] | QWORD | 0 | Class processing counter |
| 48 | [6] | QWORD | 0 | Spill mode flags |
| 96 | [12] | ptr | arena | Arena allocator pointer (from `ctx+16`) |
| 104 | [13] | ptr | arena | Queue header base / candidate list base |
| 112 | [14] | DWORD+DWORD | -1, 0 | Max element index sentinel, entry count |
| 136 | [17] | ptr | arena | Third arena reference |
| 144 | [18] | QWORD | 0 | Queue 0 entry count |
| 152 | [19] | QWORD | -1 | Queue 0 sentinel |
| 160 | [20] | ptr | arena | Fourth arena reference |
| 168 | [21] | QWORD | 0 | Queue 0b entry count |
| 176 | [22] | QWORD | -1 | Queue 0b sentinel |
| 184 | [23] | ptr | ctx | Back-pointer to context |
| 192 | [24] | ptr | node | Candidate node list head (24-byte arena node) |
| 200 | [25] | ptr | node | Candidate node list tail |
| 208 | [26] | QWORD | 0 | Node count |
| 216 | [27] | QWORD | 0 | Node capacity |
| 240 | [30] | ptr | node | Sentinel node (same as initial node at [24]) |
| 248 | [31] | QWORD | 0 | Free list head |
| 256 | [32] | QWORD | 0 | Free list count |

**Region 1 -- Bitmask arrays (bytes 272--1327)**

Two 508-byte bitmask arrays (127 DWORDs each), separated by single-byte sentinels:

| Byte range | Content |
|------------|---------|
| 284 | Sentinel byte (set to `0x80` after zeroing) |
| 288--795 | Bitmask array 0: 127 DWORDs for live range set intersection |
| 808 | Sentinel byte (set to `0x80` after zeroing) |
| 812--1319 | Bitmask array 1: 127 DWORDs for second class pair |

Each bitmask array is zeroed via an SSE2 vectorized loop (16 bytes per iteration, `0x1F` iterations). The `0x80` sentinel byte at the start of each array marks initialization completion.

**Region 2 -- Priority queue table blocks (bytes 1328--2063)**

Five embedded priority queue tables, each containing an entry count (QWORD) followed by an array of 6 queue entries (24 bytes each):

| QWORD idx | Byte offset | Content |
|-----------|-------------|---------|
| [166] | 1328 | Queue block 1 entry count (incremented by 7 per pass) |
| [167]--[184] | 1336--1479 | Queue block 1: 6 entries x 24 bytes |
| [188] | 1504 | Queue block 2 entry count |
| [189]--[206] | 1512--1655 | Queue block 2: 6 entries x 24 bytes |
| [210] | 1680 | Queue block 3 entry count |
| [211]--[228] | 1688--1831 | Queue block 3: 6 entries x 24 bytes |
| [232] | 1856 | Queue block 4 entry count |
| [233]--[250] | 1864--2007 | Queue block 4: 6 entries x 24 bytes |
| [256] | 2048 | Queue block 5 (overflow) count |

Each 24-byte queue entry has this layout:

| Entry offset | Type | Init | Field |
|--------------|------|------|-------|
| +0 | ptr | arena | Bitvector storage pointer |
| +8 | QWORD | 0 | Bitvector data pointer |
| +16 | DWORD | -1 | Max element index |
| +20 | DWORD | 0 | Current element count |

Queue entries are built by `sub_8BE190` and sorted by `sub_7553C0`. Candidates are inserted via `sub_9370A0` (with tie-breaking) and removed via `sub_9365A0` (bit-clear in bitvector).

**Region 3 -- Candidate node management (bytes ~2064--10591)**

The largest region (~8,528 bytes). Contains working storage for spill candidate evaluation across all 7 register classes. This region is zeroed during initialization and populated during the instruction walk phase by `sub_93BF50` (candidate evaluation), `sub_936610` (candidate insertion with cost), `sub_9680F0` (cost propagation), and `sub_93A1F0` (interference counting). The exact internal sub-layout varies by register class and virtual register count.

**Region 4 -- Linked list, accumulators, and tail (bytes 10592--11111)**

| Byte offset | QWORD idx | Type | Init | Field |
|-------------|-----------|------|------|-------|
| 10592 | [1324] | QWORD | 0 | Linked list head (spill candidate chain) |
| 10600 | [1325] | ptr | &self[1326] | Forward pointer (circular doubly-linked) |
| 10608 | [1326] | ptr | &self[1324] | Backward pointer |
| 10616 | [1327] | QWORD | 0 | List count |
| 10624 | [1328] | ptr | &self[1324] | Secondary forward pointer |
| 10632 | [1329] | ptr | &self[1326] | Secondary backward pointer |
| 10640 | DWORD | int | 2 | Node type tag |
| 10648 | [1331] | ptr | node | Primary candidate node (24B, type=2) |
| 10656 | [1332] | ptr | node | Secondary candidate node (24B, type=2) |
| 10696 | [1337] | ptr | node | Secondary tail pointer |
| 10704 | [1338] | ptr | 0 | Instruction walk context (knob 622 gate) |
| 10712 | [1339] | QWORD | 0 | Walk state |
| 10720 | [1340] | QWORD | 0 | Walk counter |
| 10728 | BYTE | byte | 0 | Walk active flag |
| 10736 | [1342] | QWORD | 0 | Spill cost accumulator 0 |
| 10744 | [1343] | QWORD | 0 | Spill cost accumulator 1 |
| 10752--10824 | [1344]--[1353] | QWORD | 0 | Additional cost/range counters (10 slots) |
| 10840 | [1355] | QWORD | 0 | Interference counter |
| 10872 | DWORD | int | 0 | Class mask |
| 10888 | [1361] | QWORD | 0 | Result register count |
| 10896 | [1362] | QWORD | 0 | Result cost metric |
| 10904 | [1363] | QWORD | 0 | Result spill count |
| 10912 | [1364] | QWORD | 0 | Result class width |
| 10920 | [1365] | QWORD | 0 | Best-attempt index |
| 10960 | [1370] | QWORD | 0 | Phase indicator |
| 10968 | [1371] | QWORD | 0 | Phase state |
| 10976 | [1372] | QWORD | 0 | Output flag |
| 11008 | [1376] | QWORD | 0 | SMEM spill tracking |
| 11016 | [1377] | QWORD | 0 | SMEM spill state |
| 11048 | [1381] | QWORD | 0 | Output queue pointer |
| 11056 | [1382] | QWORD | 0 | Output queue size |
| 11072 | [1384] | ptr | 0 | Callee-save tracking (freed by `sub_96CFA0`) |
| 11080 | [1385] | ptr | 0 | Callee-save arena ref (freed by `sub_96CFA0`) |
| 11089 | BYTE | byte | 1 | Guidance enabled flag |
| 11096 | [1387] | ptr | 0 | Final candidate object (freed by `sub_96CFA0`) |
| 11104 | [1388] | ptr | 0 | Final candidate arena ref (freed by `sub_96CFA0`) |

The linked list at [1324]--[1329] is initialized as a circular doubly-linked list with self-referential pointers, following the standard intrusive list pattern. The cleanup function `sub_96CFA0` (694 lines) deallocates the candidate node objects at offsets 11072, 11080, 11096, and 11104.

### Candidate node object (24 bytes)

Each candidate node is a 24-byte arena-allocated object used in the doubly-linked list and as priority queue sentinels:

| Offset | Type | Field |
|--------|------|-------|
| +0 | QWORD | Type tag: `2` = priority queue node, `3` = initial sentinel |
| +8 | QWORD | Count / payload |
| +16 | ptr | Arena back-reference |

### Stack-local queue header array

In addition to the 11,112-byte structure, the function maintains a stack-local 7-element queue header array (`v514`, 168 bytes on stack). Each entry is 24 bytes (3 QWORDs) with the same layout as the embedded queue entries above. The 7 entries map to the 7 register classes:

| Index | Class |
|-------|-------|
| 0 | R (general-purpose registers) |
| 1 | P (predicate registers) |
| 2 | B (barrier registers) |
| 3 | UR (uniform registers) |
| 4 | UP (uniform predicates) |
| 5 | UB (uniform barriers) |
| 6 | Acc (tensor/accumulator registers) |

After bitvector iteration, each stack-local queue header is built by `sub_8BE190` and sorted by `sub_7553C0`.

### Algorithm

Three phases: (A) stack-local queue init, (B) structure alloc + instruction walk, (C) finalization.

#### Phase A -- Per-class bitvector copy and sort

```c
function compute_spill_guidance(ctx, alloc, attempt):
    // Zero two 127-DWORD bitmask arrays (SSE2, 16B/iter, 0x1F iters).
    // Sentinel 0x80 at offsets +284 and +808.
    zero_sse2(bitmask[0..1], 127); bitmask[0][-1] = bitmask[1][-1] = 0x80
    queue_headers[7]  // stack-local 168B: 0=R,1=P,2=B,3=UR,4=UP,5=UB,6=Acc
    arena = ctx+16
    for class_id in 0..6:
        hdr = { storage=arena, data=0, max_index=-1, count=0 }
        if alloc.max_index[class_id] >= 0:
            n = alloc.max_index[class_id] + 1
            bitvec_resize(hdr, n)                      // sub_6E6650
            sse2_copy(hdr.data + hdr.count, alloc.data, n)
            hdr.count += n
    build_overflow_queue(queue_headers)                 // sub_8BE190
    queue_headers.run_counter += 7
    sort_queue(queue_headers)                           // sub_7553C0

    // Same copy-and-sort for 5 priority queue blocks at QWORD offsets
    // [166], [188], [210], [232], [256] (22 QWORDs each, 6 entries/block).
    for block_id in 0..4:
        qblk = &g[166 + block_id * 22]; qblk.count = 0
        for class_id in 0..6: /* identical init + bitvec copy */
        build_overflow_queue(qblk_end); qblk.count += 7; sort_queue(qblk_end)
```

#### Phase B -- Structure init and instruction walk

```c
    g = arena_alloc(ctx+16, 11112)                     // guidance structure
    g[0] = ctx; g[3] = alloc+16; g[4..6] = 0          // back-ptrs + counters
    // Init circular doubly-linked list [1324]--[1329] (self-referential).
    // Alloc two 24B candidate nodes (type=2) at g[1331], g[1332].
    // Per-class bitvec: sub_957020 allocates ceil(vreg_count/64) QWORDs.
    sub_95BC90(g, ctx.active_class, ctx.block_list, ctx.class_limit, attempt > 5)
    if !ctx.class_nospill[attempt]: sub_93CD20(g)      // candidate pre-eval
    vtable_call(ctx, slot_39)                           // arch-specific init
    if get_knob(622)==1 and !g[1338]:                   // knob 622 gate
        g[1338] = arena_alloc(ctx+16, 128)              // walk context

    for insn in block_list(ctx+280):                    // instruction walk
      switch insn.opcode:
        case 72: update_block_counters(g)               // block boundary
        case 54: load_block_state(g, func_node)         // function entry
        case 97: update_high_water_mark(g)              // block exit
                 check_block_boundary(alloc, insn)      // sub_9363E0
        default: // ordinary instruction -- per-operand walk:
          for op_idx in 0..insn.operand_count:
            op = insn.operands[op_idx]
            if (op>>28)&7 != 1: continue               // not a VR reference
            vr_id = op & 0xFFFFFF
            if vr_id in 41..44: continue               // skip special regs
            vreg = vr_table[vr_id]; cls = vreg+64
            if op < 0:                                  // DEF (bit 31 set)
                sub_91E860(alloc, insn, op_idx)         // remat cost
                cost = sub_93BE80(ctx, insn, op_idx)    // spill cost
                insert_candidate(&qh[cls], vreg, cost)  // sub_9370A0
            else:                                       // USE
                remove_candidate(&qh[cls], vreg)        // sub_9365A0
            // Cross-class demotion (sub_936610): for subsequent operands
            // whose class <= attempt and range-end <= budget, remove them.
            if cls < attempt:
              for j in op_idx+1..insn.operand_count:
                ref = vr_table[insn.operands[j] & 0xFFFFFF]
                if ref+64 <= attempt and ref+68 <= budget[ref+64]:
                    remove_candidate(&qh[ref+64], ref)
          // Cost propagation:
          if first_use_in_block:
              sub_93BF50(ctx, insn, &qh, class_limit)  // candidate eval
          if should_propagate:
              sub_9680F0(g, &qh, insn, ...)             // per-insn assignment
        case 52: // BB entry: set bits for [class_min..class_max] per class,
          // AND-subtract liveout (sub_BDC180 first visit / sub_BDCDE0 re-visit).
          // Iterate set bits; remove VRs with cls<=attempt, end<=budget.
```

#### Phase C -- Finalization

```c
    if ctx.class_nospill[ctx.active_class]:
        sub_96CFA0(g); arena_free(arena, g)            // cleanup only
    else:
        sub_93C0B0(ctx, g, attempt)                     // finalize scores
        if attempt == 6 and ctx.uniform_spill_count > 0:
            sub_9539C0(ctx, g)                          // uniform spill fixup
        for cls in 0..g.max_class_index:                // emit guidance
            entry = g[13] + 24*cls
            if entry.reg_count > 0:
                emit_spill_guidance(alloc.spill_table, entry)
        sub_96CFA0(g); arena_free(arena, g)
    for i in (run_counter-1) downto 0:                  // final re-sort
        sort_queue(&queue_headers[i])                   // sub_7553C0
```

#### Per-VR cost comparison (`sub_7553C0`)

Single-pass bitvector drain: if `max_index < 0` return (empty). Otherwise reset `max_index = -1` and delegate to `vtable_call(storage, slot_4)`. Slot 4 walks the bitvector from highest set bit downward, yielding VR indices in descending cost order (cheapest-to-spill first).

#### Candidate insertion (`sub_9370A0`) and removal (`sub_9365A0`)

Insertion is gated on the VR pair/width field `vreg+48` bits 20--21:

| Bits 20--21 | Width | Insertion behavior |
|-------------|-------|--------------------|
| 0 | single | Set bit `vreg+68` in class bitvector |
| 1 | pair | No insertion (pairs not independently spillable) |
| 3 | quad | Set `vreg+68`; if cost tag == 2, also set `vreg+68 + 1` |

The `a4` tie-break parameter (values 0/4/8) selects single-bit vs double-bit paths for quad-width VRs. Removal clears bit `vreg+68`; for 64-bit pairs (`(vreg+48 ^ 0x300000) & 0x300000 == 0`), also clears `vreg+68 + 1`. Both operations guard against exceeding bitvector capacity (`(max_index+1) << 6`).

The guidance output is consumed by the retry loop: after each failed allocation attempt, the allocator consults the guidance to decide which virtual registers to allow to spill on the next attempt.

## Spill Cost Model

The allocator uses a multi-level cost model to evaluate which registers are cheapest to spill. The model combines per-instruction frequency weighting, knob-controlled thresholds, and a density-based eligibility gate into a single unified cost per virtual register.

### Per-virtual-register weights

| Field | Type | Meaning |
|-------|------|---------|
| `vreg+28` | float | Primary spill cost (accumulated from usage frequency) |
| `vreg+32` | float | Secondary spill cost (use-after-last-def weighting) |
| `vreg+40` | float | Normalized spill cost (primary / live range length) |
| `vreg+76` | float | Alternate cost (architecture-specific adjustments) |
| `vreg+80` | int | Spill flag: 0 = not spilled, 1 = spilled |

### Allocator-level accumulators

| Field | Type | Meaning |
|-------|------|---------|
| `alloc+1568` | double | Total spill-store cost (summed over all spilled vregs) |
| `alloc+1576` | float | Total spill-load cost (summed over all refilled vregs) |

### Unified spill cost formula

```c
function compute_spill_cost(alloc, vreg, regclass):
    // Step 1: Select base weight from register pressure state
    if alloc.high_pressure_flag[regclass]:          // byte at alloc + 32*regclass + 893
        base_weight = 3.0                           // encourage spilling under pressure
    else:
        base_weight = 15.0                          // default: penalize spilling

    // Step 2: Accumulate cost per instruction touching this vreg
    vreg.primary_cost = 0.0                         // vreg+28
    vreg.secondary_cost = 0.0                       // vreg+32
    for each instr that defs or uses vreg:
        freq = block_frequency(instr.block)         // vtable callback at +8; loop depth
        vreg.primary_cost += base_weight * freq

        // Extra penalty for values consumed far from their definition
        if instr is_use and instr.program_point > vreg.last_def_point:
            vreg.secondary_cost += base_weight * freq

    // Step 3: Fold into allocator-wide accumulators on spill commit
    //   (triggered when vreg.flags bit 18 is set = needs-spill)
    alloc.total_store_cost += (double)vreg.primary_cost     // alloc+1568
    alloc.total_load_cost  += vreg.secondary_cost           // alloc+1576
```

### Spill eligibility gate (sub\_9997D0)

Before a vreg enters the spill candidate set, it must pass two knob-controlled thresholds and a density check. The knobs reside in the options block reachable through `ctx->pipeline->options + offset`:

```c
function is_spill_eligible(alloc, vreg, num_candidates):
    // Threshold 1: primary cost floor (default 0.9, knob at opts+38952)
    primary_floor = 0.9
    mode = opts.override_mode[38952]
    if mode == 2:  primary_floor = opts.value[38960]    // user override
    if mode == 1:  primary_floor = 0.0                  // disabled

    if vreg.primary_cost < primary_floor:               // vreg+28
        return false

    // Threshold 2: secondary cost floor (default 1.0, knob at opts+38880)
    secondary_floor = 1.0
    mode = opts.override_mode[38880]
    if mode == 2:  secondary_floor = opts.value[38888]
    if mode == 1:  secondary_floor = 0.0

    if vreg.secondary_cost <= secondary_floor:          // vreg+32
        return false

    // Density gate: registers consumed per candidate must stay under budget
    phys_class = resolve_regclass(vreg)
    regs_per_candidate = alloc.class_reg_count[phys_class] / num_candidates
    return regs_per_candidate < alloc.density_budget    // int at alloc+484
```

### Zero-cost pruning (sub\_999AA0)

After initial cost computation, `sub_999AA0` (162 lines) walks the interference graph and removes any vreg whose `primary_cost == 0.0` from the candidate list. This avoids wasting spill slots on dead or single-use values that would have zero benefit from spilling. The function also decrements the per-node degree count and recycles freed nodes into the allocator's free list at `alloc+144`.

### Max-pressure query (sub\_99A0B0)

`sub_99A0B0` (224 lines) computes the peak number of simultaneously live defs across all blocks spanned by a live range. It walks instructions in program order within each spanned block, counting def operands whose register class matches the target class. Special register IDs 41--44 (PT, P0--P3) are excluded. The peak is stored at `alloc.peak_defs` (DWORD at offset `402*4`) and is used to gauge whether spilling a particular vreg would meaningfully relieve pressure.

### Best-result comparison (sub\_93D070)

Called after every allocation attempt to decide whether the current result improves on the stored best, and if so, snapshot the full VR-to-physical assignment.

**Best-result structure layout** (base pointer `best`, 88 bytes + variable backup array):

| Offset | Field | Stored from |
|--------|-------|-------------|
| `best[2]` | budget threshold | set externally |
| `best[3]` | total attempt count | set externally |
| `best[10]` | best reg count | `v7` (possibly remapped `reg_count`) |
| `best[11]` | class width | `reg_count` (raw input) |
| `best[13]` | inverse density | `128 / reg_count` |
| `best[16]` | best pressure | `max_pressure` |
| `best+56` (double) | best cost | `cost` |
| `best[12]`, `best[17]` | aux counters | from `alloc->subsys+1528`, `+1564` |
| `(QWORD)(best+32)` | backup array ptr | arena-allocated on first call |
| `best+24` (byte) | improved flag | 0 = no improvement, 1 = improved |

```c
function record_best_result(best, ctx, attempt, reg_count, max_pressure, arena, cost):
    // Step 0: class-specific register count remapping via vtable slot 89
    v7 = reg_count
    remap_fn = ctx->alloc->subsys->vtable[89]          // offset 712
    if remap_fn != identity_stub:                       // sub_693720
        v7 = remap_fn(ctx->alloc->subsys, ctx, reg_count)
    inv_density = 128 / reg_count

    // Step 1: first-ever call (attempt == 0, best[3] == 1)
    if attempt == 0:
        best.improved = 1
        if best.attempt_count == 1:                     // single-attempt mode
            if reg_count <= best[2] + 1: return         // within budget, skip save
        best.backup = arena_alloc(arena, (vreg_count + 1) * 4)
        goto save_state

    // Step 2: budget gate -- require current within threshold
    threshold = min(reg_count, best.class_width)
    if best[2] + 1 < threshold:
        // Over budget: fall back to cost/pressure/width comparison
        if best.cost == cost:
            improved = (max_pressure < best.pressure)
            if max_pressure == best.pressure:
                improved = (reg_count < best.class_width)
        else:
            improved = (cost < best.cost)
        best.improved = improved
        if !improved: return
        goto check_shortcircuit

    // Step 3: within budget -- lexicographic (reg_count, inv_density) comparison
    if best.reg_count <= v7 and best.inv_density >= inv_density:
        // Existing best is at least as good on primary+secondary keys
        // Tie-break on pressure, then class_width
        if (best.reg_count != v7 or max_pressure >= best.pressure) \
           and (max_pressure != best.pressure or best.class_width <= reg_count):
            best.improved = 0                           // no improvement
            return
    best.improved = 1

check_shortcircuit:
    if attempt == 99: return                            // probe-only mode
    if attempt == best.attempt_count - 1:               // final attempt
        cap = best[2] + 1
        if reg_count <= cap: return cap                 // clamp to budget

save_state:
    // Step 4: record new best scalars
    best.class_width    = reg_count
    best.reg_count      = v7
    best.inv_density    = 128 / reg_count
    best.pressure       = max_pressure
    best.cost           = cost
    best.aux_counter_a  = ctx->alloc->subsys->child[1528]  // e.g. spill store count
    best.aux_counter_b  = ctx->alloc->subsys->child[1564]  // e.g. spill load count

    // Step 5: snapshot per-VR assignment (skip for special class width 0x989677+8)
    if reg_count != SPECIAL_CLASS_WIDTH:
        vr = vr_list_head(best->ctx)                    // linked list via vreg+120
        while vr != NULL:
            idx         = vr.id                         // vreg+12
            phys_reg    = vr.assigned_reg               // vreg+68
            is_pair_hi  = (vr.pair_width >> 23) & 1     // bit 23 of vreg+48
            best.backup[idx] = is_pair_hi + 2 * phys_reg
            vr = vr.next                                // vreg+120
        // Also snapshot subsystem auxiliary state
        best[18] = ctx->alloc->subsys[408]
        best[20] = ctx->alloc->subsys[400]
```

The backup array encodes each VR's physical register assignment as `2*phys_reg + pair_high_bit`, allowing exact restoration when the allocator commits the best result. The `is_pair_hi` bit distinguishes the low vs high half of a register pair so the restore path can reconstruct pair assignments without re-running the pair allocator.

### Spill cost infrastructure

| Address | Size | Role | Detail |
|---------|------|------|--------|
| `sub_9997D0` | 44 lines | Eligibility gate | Two-threshold + density check (see pseudocode above) |
| `sub_9998A0` | 57 lines | Has-constrained check | Hash table walk at `alloc+712`; true if any vreg has bit 3 |
| `sub_999950` | 74 lines | Constraint-aware compare | Calls `sub_998450` for setup, walks block order checking interference |
| `sub_999AA0` | 162 lines | Zero-cost pruning | Removes cost-0 vregs from interference graph; recycles to free list |
| `sub_999D10` | 102 lines | Phi/copy aggregation | For opcode 96 (phi), aggregates cost across coalesced operand chains |
| `sub_999F00` | 100 lines | Per-block cost array | Grows 12B-per-block array; copies `{block_id, store_cost, load_cost}` tuples |
| `sub_99A0B0` | 224 lines | Max-pressure query | Peak simultaneous defs per live range span (excludes regs 41--44) |
| `sub_9A8270` | ~580 lines | Live range spill driver | 14 KB; drains worklists, checks bitvector membership, commits per-range spills |

#### Has-constrained check (`sub_9998A0`)

Walks the hash table rooted at `alloc+712` (populated by `sub_995110`). Each bucket chain is a linked list of nodes; the function scans every node checking `byte +12 bit 3` (the "constrained" flag). Returns 1 immediately on the first constrained node found, 0 if the entire table is exhausted. When `alloc+720 == 0` (table empty), returns 0 without walking.

#### Constraint-aware interference check (`sub_999950`)

```c
function check_constrained_interference(alloc, block_range):
    setup_live_state(alloc, block_range)                 // sub_998450
    if not has_constrained_vreg(alloc):                  // sub_9998A0
        return PROCEED                                   // no constrained -- safe

    // Phase 1: walk successor blocks of the range
    if alloc.pass_count > 1:                             // alloc+756
        succ = block_range.successor_list                // block_range[17]
        while succ:
            if alloc.hash_count > 0:                     // alloc+720
                entry = hash_lookup(alloc+712, bb_node(succ.bb_id))
                if entry and (entry.flags & 0x4):        // constrained bit
                    goto phase2
            succ = succ.next
        return NO_INTERFERENCE

    // Phase 2: walk RPO block list for cross-block check
    phase2:
    found = false
    for i in 1..alloc.block_count:                       // alloc+520
        block = block_table[rpo_order[i]]
        if dominates(alloc, block_range, block):         // sub_76ABE0
            result = check_single_block(alloc, block)    // sub_995740
            if not result:  return PROCEED
            found = result
        elif found:
            return PROCEED                               // past dominated region
    return NO_INTERFERENCE
```

#### Phi/copy aggregation (`sub_999D10`)

For opcode 96 (phi/merge nodes), traces through coalesced operand chains to find the source live range that can absorb the phi cost. Validates a diamond CFG pattern: a phi with exactly two predecessors, each with a single predecessor, both merging from the same dominator. When this pattern holds and the source vreg has `use_count == 1` and is not constrained (bit 1 at `vreg+51`), returns the source vreg pointer for cost aggregation. Returns null (byte `+8 = 0`) when the pattern does not match or the opcode is not 96.

#### Per-block cost array builder (`sub_999F00`)

```c
function build_block_cost_array(alloc):
    block_info = *(alloc+8) -> +72                       // current block descriptor
    if not block_info:  return
    max_id = block_info.max_id                           // +32
    if max_id + 1 > alloc.cost_array_size + 1:           // alloc+592
        // Grow with 1.5x policy
        new_cap = old_cap + (old_cap + 1) / 2
        if new_cap < max_id + 1:  new_cap = max_id + 1
        buf = arena_alloc(arena, 12 * new_cap)           // vtable+24
        memcpy(buf, old_buf, 12 * (old_size + 1))
        arena_free(arena)                                // vtable+32
        alloc.cost_array = buf                           // alloc+584
        alloc.cost_capacity = new_cap                    // alloc+596
    // Zero-fill new slots as {0, 0, 0}
    for i in (old_size+1)..max_id:
        cost_array[i] = {0, 0, 0}                       // 12 bytes each
    alloc.cost_array_size = max_id                       // alloc+592
    // Copy per-block tuples from block descriptor
    src = block_info.entry_base + 16                     // +48 with 16B stride
    for j in 0..block_info.entry_count:                  // +36
        idx = src[j].block_id
        cost_array[idx] = {src[j].val_a, src[j].val_b, src[j].val_c}
```

Each entry is a 12-byte record indexed by basic block ID. The 1.5x growth policy avoids reallocation churn when processing functions with many blocks.

#### Live range spill driver (`sub_9A8270`)

The largest spill cost function (580 lines, 14 KB). Four phases:

```c
function drive_live_range_spills(alloc, loop_info, result_ptr):
    // Phase 1 -- drain 3 worklists (at alloc+123/+138/+156) into free pools
    //   via sub_69DD70 pop; recycle interval nodes (alloc[144..150]) via vtable+32.
    //   If has_secondary_map (DWORD[202]): drain alloc[102..103] -> alloc[100].
    //   If has_bucket_array (DWORD[304]): recycle alloc[153..154] per-bucket.
    drain_all_worklists(alloc)

    // Phase 2 -- early exit
    *result_ptr = 0
    if alloc.pass_count <= 1 and alloc.mode > 1:  return 0
    if alloc.block_count == 0:                     return 0

    // Phase 3 -- per-block RPO walk
    best_range = NULL; conflict_range = NULL; seq = 1
    for block in rpo_order[1..block_count]:
        range = block_to_range(block)
        if not in_live_set(alloc, range):  continue      // sub_995740
        for insn in range.instructions:
            // Bitvector membership: dense alloc[57][vr>>6] bit (vr&63),
            // then sparse BST at alloc+156 keyed by vr>>8.
            if has_interference_map and constrained(insn):
                if is_loop_entry(insn):                  // sub_7E0F50
                    analyze_interval(alloc, insn)        // sub_99CF80
                    if split_required(insn):  continue   // sub_9A3690
                    if insn in inner_loops:
                        mark_inner_loop(alloc, insn, 2)  // sub_9A29C0
                    else:
                        // Commit: add to candidate set (alloc+128),
                        // collect operand VR refs into worklist B (alloc+138),
                        // call try_commit (sub_9A79A0), finalize (sub_9A2D90),
                        // then mark_spill_candidate or try_evict_or_split.
                        // Arch callback: vtable slot 227 (offset 1816).
                        commit_or_evict(alloc, &best_range, range, insn, seq++)

    // Phase 4 -- reconcile: conflict_range must equal best_range (or NULL)
    if conflict_range and conflict_range != best_range:  return 0
    if best_range and *result_ptr:
        if best_range.block_id != result_ptr.block_id:  return 0
    return best_range
```

## Spill Code Generation

`sub_94F150` (561 lines) inserts actual spill-store and refill-load instructions into the IR instruction stream.

### Per-register spill info

The function allocates a tracking array: 12 bytes per virtual register, initialized to `{0, -1, -1}`:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 4 | Spill state (0 = none) |
| +4 | 4 | Spill slot index (-1 = unassigned) |
| +8 | 4 | Refill slot index (-1 = unassigned) |

### Execution flow

```c
function generate_spill_code(alloc, ctx, mode):
    // 1. Allocate per-block tracking
    tracking = arena_alloc(12 * (numBlocks + 1))

    // 2. Set up instruction iteration
    setup_instruction_walk(ctx, walk=1, dir=0)       // sub_7E6090

    // 3. Multi-block liveness (if numBlocks > 1 and mode == 1)
    compute_cross_block_liveness(alloc, ctx)          // sub_9449B0
    //   Uses bitvectors: sub_BDBA60, sub_BDC180, sub_BDCDE0

    // 4. Clear per-instruction spill flags
    for each instr:
        instr.flags &= ~0xE00

    // 5. Walk instruction list
    spill_weight = 15.0
    if high_pressure_class:
        spill_weight = 3.0

    for each instr in instruction_list:
        for each operand:
            vreg = lookup_vreg(operand)
            if vreg.was_previously_spilled:
                // Track for potential refill
                update_refill_tracking(vreg, instr)
            // Accumulate spill cost weighted by block frequency
            vreg.spill_cost += spill_weight * block_frequency(instr.block)

        // Handle call instructions (opcode 97; STG in ROT13, used as CALL marker -- actual CALL is opcode 71)
        if instr.opcode == 97:
            handle_callee_save(alloc, instr)

        // Track "use after last def" for enhanced cost
        update_use_after_def(vreg, instr)

    // 6. Uniform register special handling (flag 0x200)
    if vreg.flags & 0x200:
        apply_uniform_spill_rules(vreg)
```

### Epoch tracking

`sub_936CF0` (81 lines) checks basic block boundaries for epoch increments. It returns true if the block's successor is a CALL instruction (opcode 52) with a target that has opcode 236 (special call), or if allocator flags at `alloc+1588`/`alloc+1589` are set. This mechanism tracks liveness across call boundaries, ensuring that spilled values are correctly reloaded after calls that may clobber registers.

## LMEM Spilling

Local memory (LMEM) is the default spill destination. Each GPU thread has private local memory backed by DRAM and cached in L2.

### Slot allocation

`sub_939BD0` (65 lines) configures the spill slot allocator. It queries OCG knob 623 for a custom spill mode; if the knob is disabled, it selects between two default configurations based on the cost threshold at `alloc+776`:

| Condition | Bucket size | Alignment | Max pool |
|-----------|-------------|-----------|----------|
| Cost threshold == 0.0 | 8 bytes | 4 bytes | 1 MB |
| Cost threshold != 0.0 | 16 bytes | 16 bytes | 1 MB |

The 8-byte/4-byte configuration handles standard 32-bit register spills. The 16-byte/16-byte configuration handles double-precision or 64-bit values that require stricter alignment.

```c
if (*(double*)(alloc + 776) == 0.0)
    return init_spill_pool(mem_alloc, 8, 4, 0x100000);    // 8B buckets, 4B aligned
else
    return init_spill_pool(mem_alloc, 16, 16, 0x100000);   // 16B buckets, 16B aligned
```

When knob 623 is enabled, the knob value at offset `+224` supplies a custom spill limit, passed to the spill allocator init function via vtable `+24`.

### Spill pool object

The spill pool lives at `alloc+1784` and is accessed through a vtable. Its fields:

| Offset | Type | Field |
|--------|------|-------|
| +0 | ptr | Vtable pointer |
| +8 | u32 | Bucket size (bytes per slot: 8 or 16) |
| +12 | u32 | Alignment (4 or 16) |
| +16 | u32 | Pool tail offset (next free byte, starts at 0) |
| +20 | u32 | Max pool size (0x100000 = 1 MB) |
| +24 | ptr | Free list head (singly-linked freed slots) |
| +32 | u32 | Free list count |

The vtable provides two initialization methods and one allocator:

| Slot | Offset | Signature | Role |
|------|--------|-----------|------|
| 2 | +16 | `init(pool, bucket_size, align, max)` | Default init (from `sub_939BD0`) |
| 3 | +24 | `init_custom(pool, limit)` | Knob-623 custom limit init |
| 4 | +32 | `allocate(pool) -> offset` | Allocate one slot, returns byte offset |
| 5 | +40 | `release(pool, offset)` | Return slot to free list |

### Slot allocation algorithm

```c
function init_spill_pool(pool, bucket_size, alignment, max_size):
    pool.bucket_size = bucket_size          // 8 or 16
    pool.alignment   = alignment            // 4 or 16
    pool.tail_offset = 0                    // bump pointer starts at 0
    pool.max_size    = max_size             // 0x100000 (1 MB)
    pool.free_head   = NULL
    pool.free_count  = 0

function allocate_lmem_slot(pool) -> int:
    // Step 1: Try free-list reuse (slots from dead live ranges)
    if pool.free_head != NULL:
        slot_offset      = pool.free_head.offset
        pool.free_head   = pool.free_head.next
        pool.free_count -= 1
        return slot_offset

    // Step 2: Bump-allocate from pool tail
    offset = align_up(pool.tail_offset, pool.alignment)
    if offset + pool.bucket_size > pool.max_size:
        fatal("local memory pool exhausted")    // 1 MB limit
    pool.tail_offset = offset + pool.bucket_size
    return offset

function release_lmem_slot(pool, offset):
    // Prepend to free list for LIFO reuse
    node          = {offset, pool.free_head}
    pool.free_head = node
    pool.free_count += 1
```

Slot reuse occurs when `sub_93FBE0` resets per-iteration state: it walks all virtual registers, sets `vreg+68 = -1` for any vreg not pinned (flag `0x20` clear and class != 8), and returns their previously-assigned LMEM offsets to the free list via `release_lmem_slot`. On the next allocation attempt, those offsets are recycled for different vregs whose live ranges no longer overlap the original occupants.

### SASS instruction sequences

`sub_9850F0` (520 lines) generates the actual SASS load/store instruction sequences for spill traffic. Architecture-specific registers drive the address computation:

| Field | Source | Role |
|-------|--------|------|
| `target_info[400]` | Architecture vtable | Spill base register |
| `target_info[401]` | Architecture vtable | Spill slot stride |
| `target_info[402]` | Architecture vtable | Spill offset register |

**Spill store sequence:**

```asm
IADD  addr, spill_base, offset       // compute slot address
IMAD  addr, addr, stride, 0          // scale by slot stride
ST    [addr], value                   // store to local memory
```

**Refill load sequence:**

```asm
IADD  addr, spill_base, offset       // compute slot address
IMAD  addr, addr, stride, 0          // scale by slot stride
LD    value, [addr]                   // load from local memory
```

The generated instructions use these SASS opcodes:

| Opcode | SASS mnemonic | Role in spill sequence |
|--------|---------------|------------------------|
| `0xC9` | IADD | Add offset to base register |
| `0x11B` | IMAD | Multiply-add for address |
| `0xC3` | MOV | Move value |
| `0x82` (130) | `HSET2` in ROT13; used as LD/LDL-like | Load from local memory (refill) |
| `0xB7` | ST / STL | Store to local memory (spill) |
| `0x14` | ISETP | Set predicate (conditional spill) |
| `0x8B` | SHL | Shift for alignment |
| `0x110` | LOP3 | Logical operation for masking |
| `0x5F` | BRA | Branch (conditional spill) |
| `0x120` | STS | Store to shared memory (SMEM path) |

At the hardware level, local memory spills become `LDL`/`STL` instructions. The `SETLMEMBASE` (opcode 147) and `GETLMEMBASE` (opcode 148) instructions manage the local memory base pointer for the thread.

## SMEM Spilling

Shared memory (SMEM) spilling is an alternative to local memory spilling. Shared memory is on-chip SRAM, offering significantly lower latency than LMEM (which goes through L2/DRAM). However, shared memory is a finite resource shared across all threads in a CTA, making this optimization applicable only in specific circumstances.

### Entry point

`sub_9539C0` (1873 lines, 54 KB decompiled) implements the SMEM spill allocator.

### Hard restriction

```text
"Smem spilling should not be enabled when functions use abi."
```

This assertion (checked at two call sites within `sub_9539C0`) prevents SMEM spilling for ABI-conformant functions. Functions using the GPU calling convention require a stable stack frame in local memory; shared memory spill slots would conflict with the calling convention's stack layout requirements.

### Activation conditions

SMEM spilling activates when all of the following hold:

1. Register class is 3 (UR) or 6 (Tensor/Acc)
2. Device type is 5 (`ctx+896 == 5`)
3. The class has virtual registers to allocate (`vreg_count > 0`)
4. The function does not use ABI calling conventions

### Two execution paths

The function selects between a **fast path** and a **full path** based on three conditions:

| Condition | Test | Fast path if |
|-----------|------|--------------|
| Candidate count | `vreg_count` from `ctx+104` descriptor | `<= 4` |
| ABI mode | `ctx+1368` bit 2 | clear |
| Extended flag | `ctx+1380` bit 6 | clear |

If any condition fails, the full path runs. The fast path handles 1--4 uniform/accumulator registers with a single linear instruction walk (decompiled lines 1715--1862). The full path builds an explicit cost model and candidate list.

### Per-CTA SMEM pool partitioning

Both paths read `smem_sym = *(ctx+104)->qword[0]`. The runtime SMEM pool available for spilling is partitioned per-CTA. Pool sizes come from the shared memory configuration table at `0x21FB640`: 11 tiers from 0 to 328 KB on SM 8.x+, or three tiers (0/32/64 KB) on SM 7.5 (table at `0x21D9168`). The allocator carves slots in 4-byte quanta (one 32-bit register width), so slot indices are always multiples of 4.

### Algorithm (full path)

```c
function smem_spill_allocate(alloc, ctx):
    // Step 1 -- ABI guard
    if ctx.device_type == 4 and ctx.flags_1368 & 0x4:
        fatal("Smem spilling should not be enabled when functions use abi.")

    vreg_count = ctx_descriptor.dword[3]        // candidate count
    smem_sym   = ctx_descriptor.qword[0]        // SMEM allocation symbol
    if vreg_count == 0:
        alloc.smem_slot_count = 0               // alloc+1520
        return

    // Step 2 -- cost bitmap: one double per 4-register group
    bitmap_slots = (vreg_count + 3) >> 2        // ceil(vreg_count / 4)
    cost_array   = arena_alloc(8 * bitmap_slots + 8)
    cost_array[0] = bitmap_slots                // length prefix
    memset(&cost_array[1], 0, 8 * bitmap_slots) // all costs = 0.0

    // Step 3 -- callee hash table (FNV-1a indexed, sub_754DE0)
    //   Tracks which callees touch SMEM and need preservation.
    callee_ht = hash_table_new(initial_buckets=8)

    // Step 4 -- first instruction walk: score candidates
    //   Walks ctx.instruction_list (linked list at ctx+272).
    for each instr in instruction_list:
        opc = instr.opcode & 0xFFFFCFFF         // mask out predicate bits

        if opc == 97:   // spill callback
            ctx.vtable_1784[1](instr_operand, 1, 1)

        if opc == 72:   // CALL -- check if callee has SMEM flag
            callee = lookup_function(instr.operand_id)
            if callee.smem_eligible:
                callee_ht.insert(callee.id)      // FNV-1a hash of callee ID

        if opc == 130:  // operand referencing callee in callee_ht
            if callee_ht.contains(operand.callee_id):
                record_candidate(instr)

        if opc == 183 or opc == 288:             // LDS / STS
            if last_source_operand == smem_sym:
                reg_bytes = query_reg_byte_width(ctx, instr.subop)  // vtable+904
                slot_bytes = align4(reg_bytes * (operand.type_bits + 1))
                for offset = 0 to slot_bytes step 4:
                    node = alloc_candidate_node()    // 32 bytes
                    node.slot_id  = base_offset + offset
                    node.width    = (offset == 0) ? slot_bytes : 4
                    can_reuse     = alloc.vtable[36](alloc, instr, offset, callee_ht)
                    cost_idx      = node.slot_id / 4
                    if can_reuse and cost_array[cost_idx] >= 0.0:
                        if opc == 288:               // STS
                            cost_array[cost_idx] += block_freq * 0.5
                        else:                        // LDS (183)
                            cost_array[cost_idx] += block_freq
                    else:
                        cost_array[cost_idx] = -1.0  // permanently ineligible
                    candidate_list.append(node)      // doubly-linked

    // Step 5 -- propagate costs to candidate nodes, then sort
    for each node in candidate_list:
        node.score = cost_array[node.slot_id / 4]
    sort_candidates_descending(candidate_list)       // sub_9535C0

    // Step 6 -- greedy slot assignment with bitmap coalescing
    smem_offset  = alloc.smem_slot_count * 4         // alloc+1520
    assigned_ht  = hash_table_new()                  // sub_7BE7B0
    free_slot_ht = hash_table_new()

    for each node in candidate_list (descending score):
        if node.score <= 0.0:  break                 // no benefit
        if smem_offset + 4 > smem_pool_limit:  break // budget exhausted

        if assigned_ht.contains(align4(node.slot_id)):
            continue                                 // already handled

        // Probe sub-slot coverage; build bitmap v79 = |= 1 << (offset/4)
        coverage = 0;  assigned_count = 0
        for sub = 0 to node.width step 4:
            if not assigned_ht.contains(node.slot_id + sub):
                coverage |= 1 << (sub >> 2)
                assigned_count++

        if assigned_count == 0:
            // Remove node from candidate list; recycle to free list
            continue

        // Coalesce adjacent sub-slots into wider STS/LDS (sub_945C40)
        if (coverage & 0x3) == 0x3:                  // bits 0,1 -> 8B at +0
            emit_coalesced_node(node.slot_id, width=8)
        elif (coverage & 0x1):                       // bit 0 only -> 4B at +0
            emit_coalesced_node(node.slot_id, width=4)
        if (coverage & 0xC) == 0xC:                  // bits 2,3 -> 8B at +8
            emit_coalesced_node(node.slot_id + 8, width=8)

        assigned_ht.insert(node); smem_offset += 4

    // Remove fully-covered or zero-benefit nodes; recycle to pool+8.

    // Step 7 -- physical SMEM offset assignment for free slots
    running_offset = 0
    for each surviving node in candidate_list:
        for sub = 0 to node.width step 4:
            global_id = node.slot_id + sub
            if not free_slot_ht.contains(global_id):
                free_slot_ht.insert(global_id, running_offset)
                running_offset += 4

    // Step 8 -- second instruction walk: emit STS/LDS replacements
    smem_base_vreg = create_vreg(ctx, class=12, flags=0x4000)  // sub_926500

    for each instr where opc in {183, 288} and last_source == smem_sym:
        reg_bytes  = query_reg_byte_width(ctx, instr.subop)
        type_width = (operand.type_bits & 7) + 1
        total_regs = type_width
        if instr.subop == 19:  total_regs = 2 * type_width     // 64-bit
        if instr.subop == 13:  total_regs = ceil(type_width+1, 2) // packed

        for i = 0 to total_regs:
            assigned = assigned_ht.lookup(base + 4*i)
            free_off = free_slot_ht.lookup(base + 4*i)

            if assigned >= 0:
                // SMEM address encoding: slot*32 + lane_offset
                //   Activated when vtable[108] returns > 0 or ctx+1421 bit 0 set
                addr = 32 * (assigned & ~3) + (assigned & 3)
                // sub_930440 emits STS (opcode 0x120) or LDS instruction
                emit_smem_spill(ctx, opc, instr.subop, src_operand,
                                smem_base_sym, addr, vector_width)
                // Adjust byte counters
                if opc == 288: func_props.SSpillB  -= bytes  // +224
                else:          func_props.SRefillB -= bytes  // +228
            else:
                // Emit from free pool using original smem_sym
                emit_smem_spill(ctx, opc, instr.subop, src_operand,
                                smem_sym, free_off, vector_width)

            // Link to original instruction's live range (sub_93CB10)
            if instr.live_range_id > 0:
                link = sub_93CB10(alloc, instr, 4 * i)
                new_instr.dword[12] = link.dword[2]

        delete_instruction(ctx, instr)
        ctx.rewrite_state = 7                        // ctx+240

    alloc.smem_slot_count = smem_offset / 4          // alloc+1520
```

### Fast path (vreg_count <= 4)

When `vreg_count <= 4`, ABI bit is clear, and extended flag 0x40 is clear, the function skips the cost model entirely. It walks the instruction list once, looking for STS (opcode 288) and LDS (opcode 183) that reference `smem_sym`. For each match it directly calls `sub_92E720` to emit the replacement instruction with opcode `0x82` (class 12). If the instruction carries a predicate (bit 12 set), `sub_7DA5A0` handles predicate lowering before emission. The SMEM base address is the constant `268435496` (`0x10000028`), encoding address-space 1 (shared) with offset 0x28. Register width is derived from `vtable+904`; for widths <= 3 bytes, a packed flag (bit 25 or 26) is set in the operand word.

### Bitmap coverage tracking

The coverage bitmap (`v79` in the decompiled source) tracks which 4-byte sub-slots within a candidate have been assigned. It uses a `1 << (offset >> 2)` scheme:

| Bitmap bits | Meaning | Coalesced width |
|-------------|---------|-----------------|
| `0b0001` | Slot +0 assigned | 4 bytes |
| `0b0011` | Slots +0, +4 assigned | 8 bytes (merged STS.64) |
| `0b1100` | Slots +8, +12 assigned | 8 bytes at +8 |
| `0b1111` | All four sub-slots | 16 bytes (two STS.64) |

This coalescing feeds `sub_945C40` which adjusts the candidate list node widths, ensuring the final STS/LDS instructions use the widest possible memory transaction.

### SMEM symbols

The SMEM spill infrastructure uses two internal symbols for allocation tracking:

- `__nv_reservedSMEM_allocation_phase` (address `0x1CFCE80`)
- `__nv_reservedSMEM_allocation_mask` (address `0x1CFCEA8`)

The CLI flag `--disable-smem-reservation` can disable shared memory reservation entirely.

## Spill Statistics

The allocator collects detailed spill metrics into a per-function statistics object. These are used for compilation reporting, performance analysis, and the `--warn-on-spills` diagnostic.

### Statistics fields

The statistics object stores spill counters at fixed DWORD offsets:

| Offset (DWORDs) | Name | Description |
|------------------|------|-------------|
| [12] | `LSpillB` | Local memory spill bytes |
| [13] | `LRefillB` | Local memory refill bytes |
| [14] | `SRefillB` | Shared memory refill bytes |
| [15] | `SSpillB` | Shared memory spill bytes |
| [16] | `LowLmemSpillSize` | Low-bound local memory spill size |
| [17] | `FrameLmemSpillSize` | Frame-level local memory spill size |
| [18] | `LNonSpillB` | Non-spill local memory bytes |
| [19] | `LNonRefillB` | Non-refill local memory bytes |
| [20] | `NonSpillSize` | Total non-spill memory size |

### Format strings

The statistics printing subsystem (`sub_A3A7E0`) emits two lines for spill metrics:

```text
# [est latency = %d] [LSpillB=%d] [LRefillB=%d], [SSpillB=%d],
  [SRefillB=%d], [LowLmemSpillSize=%d] [FrameLmemSpillSize=%d]
# [LNonSpillB=%d] [LNonRefillB=%d], [NonSpillSize=%d]
```

The function properties string (used in verbose output):

```text
Function properties for %s
    %d bytes stack frame, %d bytes spill stores, %d bytes spill loads
```

### Spill warning

When `--warn-on-spills` is active, the following warning is emitted for any function with spills:

```text
Registers are spilled to local memory in function '%s',
    %d bytes spill stores, %d bytes spill loads
```

The flag is registered in `sub_432A00` / `sub_434320` and stored at `compilation_ctx + 473`.

## Allocation Failure

When all spill retry attempts are exhausted and the allocator still cannot fit within the register budget, a fatal error is emitted:

```text
Register allocation failed with register count of '%d'.
Compile the program with a higher register target
```

This error originates from `sub_9714E0` (allocation finalization, 290 lines), which is the last function called in the retry pipeline. Two emission paths exist:

| Path | Function | Context |
|------|----------|---------|
| With source location | `sub_895530` | Includes function name and PTX line number |
| Without source location | `sub_7EEFA0` | Generic error when location unavailable |

The alternate allocator path `sub_964130` (1794 lines) also references this error at six separate points, covering different failure/retry scenarios. A dedicated failure reporter `sub_966490` (474 lines) handles error diagnostic formatting.

## Spill-Refill Pattern Optimization

The Ori IR includes dedicated instruction type markers for spill/refill patterns, enabling post-allocation optimization of spill traffic:

| Type ID | Name | Description |
|---------|------|-------------|
| 8 | Spill-refill | Spill/refill pair marker |
| 10 | Bit-spill | Single-bit spill (predicate register spill to GPR) |

The `SpillRefill` pass attempts to match and optimize these patterns. Error strings reveal three failure modes:

1. `"Failed to find matching spill for refilling load that is involved in this operand computation"` -- the refill load has no corresponding spill store
2. `"Failed to establish match for bit-spill-refill pattern involved in this operand computation"` -- the bit-spill pattern does not match expected form
3. `"Some instruction(s) are destroying the base of bit-spill-refill pattern involved in this operand computation"` -- instructions between spill and refill clobber the base address register

Debug strings include `" spill-regill bug "` and `" bit-spill bug "` (both with typos present in the binary).

### Pattern matching algorithm

The memcheck verifier (`sub_A73F30`, 806 lines) drives the matching. For each post-allocation instruction whose reaching definitions changed, it classifies the instruction and dispatches to one of three pattern-specific matchers:

```c
// sub_A73F30 -- simplified dispatch for spill/refill verification
match_spill_refill_pairs(verifier, post_insn, operand_idx):

    // --- Path A: standard spill-refill (type_id 8) ---
    if is_spill_store(ctx, post_insn):                  // sub_A56CE0: opcode == 183 (STL)
        chain = find_matching_spill(verifier, post_insn) // sub_A677C0
        if chain.insn_id == -1:
            emit_error(1)  // "Failed to find matching spill for refilling load"
            return
        // Walk chain entries; verify each candidate store's slot matches the refill
        for entry in chain.entries:
            if entry.bitmask & class_mask == 0:
                continue
            if entry.store_insn == NULL:
                emit_error(2)  // "refill reads potentially uninitialized memory"
                return
            verify_slot_match(verifier, entry)  // compare slot IDs
        return  // matched

    // --- Path B: P2R/R2P predicate spill (type_id 8, predicate variant) ---
    if is_p2r_r2p(ctx, post_insn):                      // sub_A57590
        if not clobber_check_ok(verifier, post_insn):    // sub_A56F80
            emit_error(8)  // "destroying the base of P2R-R2P pattern"
            return
        return  // matched

    // --- Path C: bit-spill (type_id 10) ---
    if insn.num_operands > 2 and (insn.flags & 0x10):   // predicate-packed bit-spill
        if is_bit_spill_pattern(ctx, post_insn):         // sub_A53DB0
            if not clobber_check_ok(verifier, post_insn): // sub_A56F80
                emit_error(9)  // "destroying the base of bit-spill-refill pattern"
                return
            return  // matched

    // ... fall through to other verifier checks (remat, extra defs, etc.)
```

**`find_matching_spill`** (`sub_A677C0`) uses an FNV-1a hash map keyed on instruction ID to locate the spill chain node corresponding to a refill load. It verifies that the store and load target the same basic block (via the remapped-instruction map), then returns a chain structure containing the matched store instruction and a bitmask of register classes involved.

**`clobber_check_ok`** (`sub_A56F80`) iterates the def-use chain of the matched spill-store node. For each def whose operand index matches the target, it looks up the defining instruction in the remapped-instruction map. If any such instruction was remapped (indicating an intervening write to the same physical register), the check returns false -- the base register was clobbered between spill and refill.

**`is_spill_store` / `is_refill_load`** (`sub_A56CE0` / `sub_A56DE0`) check the instruction opcode (183 for STL, 288 for LDL), then verify the source/dest vreg has spill flags set (bit 14 or bit 17 in `vreg_desc+36`), and confirm the register class == 2.

## Function Map

| Address | Lines | Role | Confidence |
|---------|-------|------|---|
| `sub_939BD0` | 65 | Spill allocator setup (knob 623 dispatch) | HIGH |
| `sub_93C0B0` | 582 | Spill range optimizer | HIGH |
| `sub_93D070` | 155 | Best allocation result recorder | HIGH |
| `sub_93E290` | 397 | Spill candidate node creator (192-byte arena nodes) | HIGH |
| `sub_93F130` | 544 | Spill code inserter | HIGH |
| `sub_93FBE0` | 940 | Per-iteration state reset / slot assignment | HIGH |
| `sub_940EF0` | 764 | Alternate spill slot assignment path | MEDIUM |
| `sub_944740` | 138 | Interference count at program point | HIGH |
| `sub_9449B0` | 418 | Cross-block liveness range calculator | HIGH |
| `sub_94B200` | 642 | Spill weight accumulator | HIGH |
| `sub_94E620` | 617 | Spill cost accumulator / liveness annotator | HIGH |
| `sub_94F150` | 561 | Spill code generation (main entry) | HIGH |
| `sub_94FDD0` | 155 | Register assignment + spill trigger | HIGH |
| `sub_9539C0` | 1,873 | SMEM (shared memory) spill allocator | HIGH |
| `sub_9714E0` | 290 | Allocation finalization / error emission | MEDIUM |
| `sub_96D940` | 2,983 | Spill guidance engine (7 class-parallel queues) | HIGH |
| `sub_971A90` | 355 | NOSPILL / SPILL retry driver | HIGH |
| `sub_9850F0` | 520 | SASS-level spill instruction generator | HIGH |
| `sub_9997D0` | -- | Spill cost initialization | MEDIUM |
| `sub_9998A0` | -- | Spill cost computation | MEDIUM |
| `sub_999950` | -- | Spill cost comparison | MEDIUM |
| `sub_999AA0` | -- | Spill benefit estimation | MEDIUM |
| `sub_9A8270` | -- | Live range spill cost computation (14 KB) | MEDIUM |
