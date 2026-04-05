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

```
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

```
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

```
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

```
function compute_spill_guidance(ctx, guidance_array, attempt):
    for class_id in 0..6:
        entry = &guidance_array[class_id]

        // 1. Initialize working bitmask arrays
        zero_fill(entry.bitmasks, 128 elements)

        // 2. Iterate live range bitvectors
        for each live_range in class[class_id]:
            // 3. Compute set intersection with other live ranges
            intersect(entry.bitmasks, live_range.bitvector)

        // 4. Build priority queue of spill candidates
        build_priority_queue(entry)               // sub_8BE190

        // 5. Sort by spill cost (ascending -- cheapest to spill first)
        sort_priority_queue(entry)                // sub_7553C0
```

The guidance output is consumed by the retry loop: after each failed allocation attempt, the allocator consults the guidance to decide which virtual registers to allow to spill on the next attempt.

## Spill Cost Model

The allocator uses a multi-level cost model to evaluate which registers are cheapest to spill.

### Per-virtual-register weights

| Field | Type | Meaning |
|-------|------|---------|
| `vreg+40` | float | Primary spill cost (accumulated from usage frequency) |
| `vreg+76` | float | Secondary spill cost (alternate weighting) |
| `vreg+80` | int | Spill flag: 0 = not spilled, 1 = spilled |

### Allocator-level accumulators

| Field | Type | Meaning |
|-------|------|---------|
| `alloc+1568` | double | Total spill-store cost |
| `alloc+1576` | float | Total spill-load cost |

### Default cost weight

The base spill cost weight is **15.0** for normal register classes, reduced to **3.0** for register classes under high pressure. The selection is made by a per-class flag at `alloc + 32 * class_id + 893`:

```c
float spill_weight = 15.0f;
if (*(uint8_t*)(alloc + 32 * regclass + 893))
    spill_weight = 3.0f;    // high-pressure class: lower cost to encourage spilling
```

### Block frequency weighting

Spill cost is multiplied by the enclosing loop's nesting depth, obtained via a block frequency callback at vtable offset `+8`. Inner-loop spills receive higher penalties, discouraging the allocator from spilling values that are live across loop back-edges.

### Best-result comparison

`sub_93D070` (155 lines) records the best allocation result across retry attempts. Comparison uses tie-breaking priority:

1. Register count (lower is better)
2. Cost metric (double at `result+56`)
3. Spill count
4. Register class width

An inverse density metric `128 / register_count` is used for secondary comparison. The per-variable assignment array is saved to a backup when a new best is found.

### Spill cost infrastructure

A suite of functions at `0x998000`--`0x99E000` implements the cost computation:

| Address | Role |
|---------|------|
| `sub_9997D0` | Spill cost initialization |
| `sub_9998A0` | Spill cost computation |
| `sub_999950` | Spill cost comparison |
| `sub_999AA0` | Spill benefit estimation |
| `sub_999D10` | Spill cost aggregation |
| `sub_999F00` | Spill cost finalization |
| `sub_99A0B0` | Range spill cost query |
| `sub_9A8270` | Live range spill cost (14 KB) |

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

```
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

### SASS instruction sequences

`sub_9850F0` (520 lines) generates the actual SASS load/store instruction sequences for spill traffic. Architecture-specific registers drive the address computation:

| Field | Source | Role |
|-------|--------|------|
| `target_info[400]` | Architecture vtable | Spill base register |
| `target_info[401]` | Architecture vtable | Spill slot stride |
| `target_info[402]` | Architecture vtable | Spill offset register |

**Spill store sequence:**

```
IADD  addr, spill_base, offset       // compute slot address
IMAD  addr, addr, stride, 0          // scale by slot stride
ST    [addr], value                   // store to local memory
```

**Refill load sequence:**

```
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

```
"Smem spilling should not be enabled when functions use abi."
```

This assertion (checked at two call sites within `sub_9539C0`) prevents SMEM spilling for ABI-conformant functions. Functions using the GPU calling convention require a stable stack frame in local memory; shared memory spill slots would conflict with the calling convention's stack layout requirements.

### Activation conditions

SMEM spilling activates when all of the following hold:

1. Register class is 3 (UR) or 6 (Tensor/Acc)
2. Device type is 5 (`ctx+896 == 5`)
3. The class has virtual registers to allocate (`vreg_count > 0`)
4. The function does not use ABI calling conventions

### Algorithm

```
function smem_spill_allocate(alloc, ctx):
    // 1. Assert no ABI conflict
    assert(not function_uses_abi())

    // 2. Allocate per-block tracking (24-byte slots)
    tracking = arena_alloc(24 * numBlocks)

    // 3. Set up SSE-width bitmaps for shared memory tracking
    init_smem_bitmaps(alloc)

    // 4. Walk instruction list, identify spill candidates
    for each vreg marked for spill:
        // 5. Allocate shared memory slot
        slot = allocate_smem_slot(alloc)
        vreg.smem_slot = slot

        // 6. Generate shared memory load/store
        insert_sts_instruction(slot, vreg)   // STS (store to smem)
        insert_lds_instruction(slot, vreg)   // LDS (load from smem)

    // 7. Update shared memory allocation bitmap
    update_smem_allocation(alloc)
```

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

```
# [est latency = %d] [LSpillB=%d] [LRefillB=%d], [SSpillB=%d],
  [SRefillB=%d], [LowLmemSpillSize=%d] [FrameLmemSpillSize=%d]
# [LNonSpillB=%d] [LNonRefillB=%d], [NonSpillSize=%d]
```

The function properties string (used in verbose output):

```
Function properties for %s
    %d bytes stack frame, %d bytes spill stores, %d bytes spill loads
```

### Spill warning

When `--warn-on-spills` is active, the following warning is emitted for any function with spills:

```
Registers are spilled to local memory in function '%s',
    %d bytes spill stores, %d bytes spill loads
```

The flag is registered in `sub_432A00` / `sub_434320` and stored at `compilation_ctx + 473`.

## Allocation Failure

When all spill retry attempts are exhausted and the allocator still cannot fit within the register budget, a fatal error is emitted:

```
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
