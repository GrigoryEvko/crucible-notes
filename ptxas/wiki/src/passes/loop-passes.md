# Loop Optimization Passes

Eight phases in the ptxas pipeline handle loop transformations: canonicalization, unrolling, software pipelining, loop-invariant code motion (four instances), and loop fusion. These passes operate on the Ori IR's backedge-based loop representation -- there is no dedicated "loop tree" data structure. Loop detection is a byproduct of CFG analysis (phase 3), which identifies back edges, and each loop pass re-derives the information it needs from the backedge hash map and dominator relationships.

| | |
|---|---|
| **Loop passes** | 8 phases (18, 22, 24, 35, 59, 66, 79, 88) |
| **Loop representation** | FNV-1a hash map of back edges at Code Object `+680` |
| **Loop nesting** | Determined by dominator depth at `BB+144` |
| **RPO array** | Code Object `+720` (int*, used by all loop passes) |
| **NamedPhases overrides** | `"LoopMakeSingleEntry"`, `"HoistInvariants"`, `"Pipelining"`, `"LoopFusion"` |
| **Knob 381** | Controls LICM aggressiveness per-block |
| **Knob 487** | Master gate for optimization-level-dependent passes |
| **Knob 220** | Controls invariant hoisting safety threshold |
| **Unrolling rejection codes** | 24 distinct codes (`0x80000001`--`0x80000018`) |
| **Pipelining core** | `sub_1395850` (732 lines, `0x1395850`) |
| **Unrolling core** | `sub_887F00` (1590 lines, `0x887F00`) |
| **LICM core** | `sub_8FF780` (366 lines, `0x8FF780`) |
| **Fusion core** | `sub_1397360` (432 lines, `0x1397360`) |

## Pipeline Placement

```
Phase  18  OriLoopSimplification       -- Early Optimization
Phase  22  OriLoopUnrolling            -- Early Optimization
Phase  24  OriPipelining               -- Early Optimization
Phase  35  OriHoistInvariantsEarly     -- Mid-Level Optimization
Phase  59  OriLoopFusion               -- Late Optimization
Phase  66  OriHoistInvariantsLate      -- Late Optimization
Phase  79  OriHoistInvariantsLate2     -- Legalization
Phase  88  OriHoistInvariantsLate3     -- Legalization (post-GMMA)
```

The ordering is deliberate. Simplification (18) canonicalizes loop structure before unrolling (22) can analyze trip counts. Unrolling runs before pipelining (24) because unrolled loops may no longer benefit from software pipelining. LICM runs at four points because intervening passes continuously expose new invariants: predication (63) converts conditional code to straight-line predicated code, revealing loop-invariant predicated operations; late unsupported-op expansion (78) introduces new invariant materialization sequences; GMMA fixup (87) inserts sequences whose setup computations are often loop-invariant.

---

## Loop Representation in Ori IR

ptxas does not maintain an explicit loop tree structure (unlike LLVM's `LoopInfo`). Instead, loop structure is inferred on demand from two CFG data structures:

### Backedge Hash Map (Code Object `+680`)

A FNV-1a hash map whose keys are basic block indices and whose values are linked lists of backedge targets. A backedge exists from block `src` to block `dst` when `dst` dominates `src` in the CFG -- the standard definition.

The hash function is the FNV-1a variant visible in the backedge debug printer (`sub_BDEA50`):

```c
uint32_t hash = 0x811C9DC5;  // FNV offset basis
hash = 16777619 * (hash ^ byte0(bb_index));
hash = 16777619 * (hash ^ byte1(bb_index));
hash = 16777619 * (hash ^ byte2(bb_index));
hash = 16777619 * (hash ^ byte3(bb_index));
uint32_t bucket = hash & (num_buckets - 1);
```

Each bucket entry is a linked list node `{next_ptr, padding, bb_index, ...}` with 24-byte stride. The hash map supports collision chains.

### Basic Block Loop Metadata

Each basic block entry (40 bytes, at Code Object `+296`) carries:

| Offset | Type | Field | Description |
|--------|------|-------|-------------|
| +16 | `i32` | `loop_depth` | Nesting depth (0 = not in loop) |
| +128 | `ptr` | `predecessors` | Linked list of predecessor BB indices |
| +136 | `ptr` | `backedge_preds` | Linked list of backedge predecessor indices |
| +144 | `i32` | `rpo_number` | Reverse post-order number (dominator proxy) |
| +148 | `i32` | `num_backedge_preds` | Count of backedge predecessors |
| +152 | `i32` | `num_backedge_succs` | Count of backedge successors |
| +280 | `i32` | `loop_flags` | Bit flags for loop properties |

A block is a **loop header** when `num_backedge_preds > 0` (at least one back edge targets it). Loop nesting depth at `+16` is computed during CFG analysis (phase 3) and refreshed by `sub_785E20` after structural transformations.

### Identifying Loop Structure

Loop passes reconstruct loop structure using this algorithm:

```
function identify_loop(header_bb):
    // A header has at least one backedge predecessor
    assert header_bb.num_backedge_preds > 0
    
    // The loop body is all blocks reachable from header
    // that can reach a backedge source without leaving
    // the dominator region of header
    body = {}
    for each block B in function:
        if B.rpo_number >= header_bb.rpo_number:
            if B.rpo_number <= max_backedge_source.rpo_number:
                body.add(B)
    return body
```

The RPO number at `BB+144` serves as a dominator proxy: if block A has a smaller RPO number than block B, A dominates or precedes B. This allows loop containment tests without explicit dominator tree queries.

---

## Phase 18: OriLoopSimplification

**Purpose:** Canonicalizes loop structure to ensure every loop has a single entry edge, a single back edge, and an explicit preheader block.

**Vtable:** `off_22BD898` | **Execute:** `sub_78B430` (255 lines, `0x78B430`)

### Gate Conditions

The pass is gated by multiple conditions in `sub_78B430`:

1. `Code Object +1368` bit 0 must be set (function has loops -- the "has backedges" flag).
2. The optimization level at `+896` must satisfy `(opt_level - 4) > 1` for the full simplification path; otherwise only basic canonicalization runs.
3. The `NamedPhases` system is consulted via `sub_799250(knob_container, "LoopMakeSingleEntry")` -- if the result is true (user disabled it), the pass is skipped entirely.
4. Knob 487 is checked via the knob container's virtual dispatch at vtable offset `+152`.

### Algorithm

Loop simplification iterates over all basic blocks in order:

```
function simplify_loops(func):
    rebuild_predecessors(func)          // sub_7753F0
    rebuild_rpo_and_dominators(func)    // sub_789BE0
    
    if knob("LoopMakeSingleEntry") is disabled:
        return
    if not knob(487):                   // optimization level gate
        return
    
    for each block B (index 0 .. func.bb_index):
        if B.num_backedge_preds == 0:
            continue                    // not a loop header
        if B.num_backedge_preds != B.num_backedge_succs:
            continue                    // irregular loop, skip
        
        header_instr = B.first_instruction
        if (header_instr.opcode & 0xFFFFFFFD) != 0x5D:
            continue                    // header must start with BRA/BRANCH variant
        
        // Find the immediate dominator with the smallest RPO
        idom = find_immediate_dominator(B)
        
        // Find the outermost predecessor (largest RPO before header)
        outer_pred = find_outermost_predecessor(B)
        
        if idom and outer_pred:
            // Insert preheader between outer_pred and B
            preheader = split_edge(func, outer_pred, B)
            
            // If header starts with conditional branch (opcode 93):
            //   Redirect the fall-through case to preheader
            // If header starts with unconditional branch (opcode 95):
            //   Insert a BRA trampoline in preheader
            
            rewrite_branch_targets(func, B, preheader)
    
    if any_changes:
        rebuild_predecessors_and_rpo(func)
```

The critical operation `sub_931920` creates a new basic block by splitting an edge. It allocates a new BB entry, rewires predecessor/successor lists, and inserts a `BRA` instruction to maintain CFG integrity.

### Single-Entry Guarantee

After simplification, every natural loop satisfies:
- Exactly one entry edge (from the preheader to the header)
- All back edges target the same header block
- The preheader dominates the header
- No critical edges exist on loop entry paths

This canonical form is required by unrolling (phase 22) and pipelining (phase 24), which both assume they can safely duplicate the loop body without worrying about multi-entry loops.

---

## Phase 22: OriLoopUnrolling

**Purpose:** Unrolls loops to reduce branch overhead, expose instruction-level parallelism, and enable downstream optimizations (CSE, constant folding across iterations).

**Vtable:** `off_22BD938` | **Core:** `sub_887F00` (1590 lines, `0x887F00`)

### Unrolling Architecture

The unrolling pass is the largest single loop optimization in ptxas at 1590 decompiled lines. It consists of three major stages:

1. **Loop collection** -- Iterates the instruction list, identifies back edges (bit 0 of `instruction+84`), and collects loop headers into an array.
2. **Eligibility analysis** -- For each loop, determines whether it can be unrolled and what factor to use.
3. **Loop body duplication** -- Physically duplicates the loop body, rewriting register references and adjusting trip count computations.

### Rejection Codes

When a loop cannot be unrolled, ptxas records one of 24 rejection codes at `register+80`. These codes are visible in `--stat=phase-wise` output:

| Code | Hex | Rejection Reason | Category |
|------|-----|-----------------|----------|
| 1 | `0x80000001` | Irregular loop | Structure |
| 2 | `0x80000002` | Irregular loop | Structure |
| 3 | `0x80000003` | Ineligible instruction | Content |
| 4 | `0x80000004` | Ineligible instruction | Content |
| 5 | `0x80000005` | Ineligible instruction | Content |
| 6 | `0x80000006` | Ineligible instruction | Content |
| 7 | `0x80000007` | Performance | Heuristic |
| 8 | `0x80000008` | Performance | Heuristic |
| 9 | `0x80000009` | Performance | Heuristic |
| 10 | `0x8000000a` | Performance | Heuristic |
| 11 | `0x8000000b` | Performance | Heuristic |
| 12 | `0x8000000c` | Performance | Heuristic |
| 13 | `0x8000000d` | Unsupported loop type | Structure |
| 14 | `0x8000000e` | Unsupported loop exit condition | Exit |
| 15 | `0x8000000f` | Unsupported loop exit condition | Exit |
| 16 | `0x80000010` | Unsupported loop index variable | Induction |
| 17 | `0x80000011` | Unsupported loop index variable | Induction |
| 18 | `0x80000012` | Unsupported loop index variable | Induction |
| 19 | `0x80000013` | Unsupported loop exit condition | Exit |
| 20 | `0x80000014` | Unsupported loop exit condition | Exit |
| 21 | `0x80000015` | Unsupported loop exit condition | Exit |
| 22 | `0x80000016` | Unsupported loop exit condition | Exit |
| 23 | `0x80000017` | Unsupported loop exit condition | Exit |
| 24 | `0x80000018` | Unsupported loop type | Structure |

The rejection codes break down into four categories:
- **Structure** (codes 1--2, 13, 24): Loop has multiple entries, multiple exits, or irreducible control flow.
- **Content** (codes 3--6): Loop body contains instructions that cannot be safely duplicated (e.g., barriers, atomics with specific ordering constraints, inline PTX `asm` blocks).
- **Heuristic** (codes 7--12): Unrolling was technically possible but rejected by the cost model.
- **Exit/Induction** (codes 14--23): The exit condition or induction variable uses a pattern the unroller cannot analyze (non-affine step, pointer induction, complex exit predicate).

### Trip Count Analysis

The trip count analyzer `sub_697390` extracts loop bounds from the exit instruction. It recognizes several exit instruction patterns by matching the opcode field at `instruction+72`:

| Opcode (masked) | Instruction | Trip Count Extraction |
|---------|-------------|----------------------|
| 7 | `ISETP` (integer set-predicate) | `sub_693CA0` -- extracts start/end/step from ISETP operands |
| 22 | `IMNMX` (integer min/max) | `sub_7E40E0` -- extracts from min/max bound pattern |
| 50 | `IADD3` (3-input integer add) | `sub_693CA0` -- affine induction via add chain |
| 51 | `LOP3` (3-input logic op) | Direct operand extraction |
| 83 | `IMAD` (integer multiply-add) | `sub_7E3640` -- multiply-stride induction |

For each recognized pattern, the analyzer fills a 48-byte trip count descriptor:

```c
struct TripCountInfo {      // 48 bytes
    int32_t reserved_0;     // +0
    int32_t reserved_4;     // +4
    int32_t start;          // +8:  loop start value
    int32_t start_dup;      // +12: copy of start
    int32_t step;           // +16: iteration stride
    int32_t end;            // +20: loop end value
    int32_t range;          // +24: end - start (unsigned)
    int32_t end_dup;        // +28: copy of end
    int32_t init;           // +32: initial value
    int32_t datatype_lo;    // +36: 8 = 8-bit, 16 = 16-bit, 32 = 32-bit
    int32_t datatype_hi;    // +40: upper bound data type
    int32_t trip_count;     // +44: computed trip count (or -1)
};
```

When the trip count cannot be statically determined, `trip_count` is set to `-1` and the loop is only eligible for partial unrolling.

### Unroll Factor Heuristics

The unroll factor computation (`sub_7E39B0`) uses a table-driven approach. A static table at `0x21CF100` (primary) or `0x21CFE80` (alternate) contains pre-computed unroll factor entries. Each entry is a 36-byte record:

```c
struct UnrollTableEntry {   // 36 bytes
    int32_t match[5];       // +0:  matching criteria (trip count pattern, exit type, etc.)
    int32_t base_factor;    // +20: base unroll factor
    int32_t adj_factor;     // +24: adjustment for has-predicate case
    int32_t adj_factor2;    // +28: adjustment for another flag
    int32_t full_factor;    // +32: factor for full unrolling
};
```

The lookup iterates the table searching for a match on `{start, step, exit_type, flags, comparison_type}`. When found, the entry provides up to 5 levels of unroll factor depending on loop characteristics:

```
function compute_unroll_factor(trip_info, has_pred_a, has_pred_b):
    entry = table_lookup(trip_info.start, trip_info.step,
                         exit_type, flags, comparison)
    
    factor[4] = 0                           // innermost (unused)
    factor[3] = entry.full_factor           // full unroll threshold
    factor[2] = entry.base_factor + entry.adj_factor
    if has_pred_a:
        factor[2] = entry.full_factor + 1
    factor[1] = factor[2] + entry.adj_factor2
    if has_pred_b:
        factor[1] = factor[2] + 1
    factor[0] = factor[1] + entry.max_factor
    
    return factor   // array of 5 thresholds
```

### Full vs. Partial Unrolling

The unroller makes a full/partial decision based on trip count vs. factor threshold:

```
function decide_unroll(loop, trip_count, factors):
    if trip_count is known:
        if trip_count <= factors[3]:        // full unroll threshold
            return FULL_UNROLL
        if trip_count <= factors[2]:
            return PARTIAL_UNROLL(trip_count)
    
    // Check register pressure constraint
    body_size = count_instructions(loop)
    estimated_regs = estimate_registers_after_unroll(loop, factor)
    if estimated_regs > max_allowed_regs:
        set_rejection(0x80000007)           // "performance"
        return NO_UNROLL
    
    return PARTIAL_UNROLL(factors[0])
```

### Body Duplication

The physical unrolling is performed by `sub_87BE20`, called from `sub_887F00` in a loop over collected headers. For each unrolling iteration:

1. The loop body instructions are duplicated via instruction cloning.
2. Register references in the cloned body are renamed using a fresh virtual register allocation (from the register file at Code Object `+88`).
3. The induction variable update in each copy is adjusted to reflect the accumulated offset.
4. After all copies are placed, the exit condition is rewritten to compare against the remaining trip count.
5. If the trip count is not evenly divisible by the unroll factor, a **remainder loop** (peel iteration) is emitted after the unrolled body.

### Statistics

After unrolling completes, the pass records statistics in the scheduler context at Code Object `+1584`:

- `sched_ctx + 87*4` = count of partially unrolled loops
- `sched_ctx + 88*4` = count of non-unrolled loops

These appear in `--stat=phase-wise` output as:
```
# [partially unrolled loops=3] [non-unrolled loops=1]
```

---

## Phase 24: OriPipelining

**Purpose:** Software pipelining -- overlaps instructions from multiple loop iterations to hide memory and instruction latency.

**Vtable:** `off_22BD988` | **Entry:** `sub_13967C0` (25 lines, `0x13967C0`) | **Core:** `sub_1395850` (732 lines, `0x1395850`)

### Gate Conditions

Pipelining is gated by three checks:

1. The function must have loops (`+1368` bit 0).
2. The `NamedPhases` system is consulted: `sub_799250(knob_container, "Pipelining")`. If disabled by the user, the pass is skipped.
3. Knob 487 must be enabled (optimization level gate).

### Shared Infrastructure

Pipelining and loop fusion share an initialization function `sub_1389AF0` (593 lines) that constructs an 872-byte (`0x368`) analysis context on the stack. This context contains:

- Loop header identification data
- Instruction dependency chains
- Resource usage tables
- Register pressure estimates

The teardown function `sub_138A6E0` frees this context.

### Algorithm Overview

Software pipelining in ptxas is not classical modulo scheduling (Rau's algorithm). Instead, the approach is closer to **iterative modulo scheduling with a fixed initiation interval (II)**, where the pipeliner attempts to overlap consecutive iterations by sliding instructions earlier:

```
function pipeline_loop(context, loop):
    // Phase 1: Analyze loop body
    build_dependence_graph(loop)
    compute_resource_usage(loop)
    
    // Phase 2: Determine initiation interval
    II = max(resource_II, recurrence_II)
    
    // Phase 3: Schedule with overlap
    for each instruction I in loop body:
        earliest_slot = max(data_dependence_constraint(I),
                            resource_constraint(I, II))
        schedule(I, earliest_slot mod II)
    
    // Phase 4: Generate prologue/epilogue
    emit_prologue(loop, II, num_stages)
    emit_kernel_loop(loop, II)
    emit_epilogue(loop, II, num_stages)
```

The `resource_II` is the minimum II achievable given functional unit constraints -- specifically, for GPU architectures, the number of memory operations divided by the number of LSU pipes, and the number of math operations divided by the FMA pipe count.

The `recurrence_II` is the minimum II due to loop-carried dependencies -- if instruction A in iteration `i` produces a value consumed by instruction B in iteration `i`, and the latency from A to B exceeds the desired II, the II must be increased.

### GPU-Specific Considerations

GPU software pipelining differs from classical CPU pipelining in several important ways:

1. **No rotating register files.** NVIDIA GPUs lack hardware rotating registers, so the pipeliner must use explicit register renaming and MOV instructions to implement the register rotation that hardware provides on Itanium-class CPUs. This increases the overhead of pipelining.

2. **Warp-level execution.** All threads in a warp execute the same instruction sequence. There is no per-thread branching in the prologue/epilogue -- if any thread in the warp needs the prologue, all threads execute it (with predicated no-ops for threads that don't need it).

3. **Memory latency hiding.** The primary motivation for software pipelining on GPUs is hiding memory access latency (global/shared memory loads), not arithmetic latency. The scheduler (phases 97--110) handles arithmetic latency at a later stage.

4. **Register pressure sensitivity.** Pipelining increases register usage roughly proportional to the number of pipeline stages. On GPUs where occupancy is inversely proportional to register count, aggressive pipelining can reduce occupancy enough to cause a net slowdown.

### Pipeline Stage Count

The number of pipeline stages is determined by: `stages = ceil(longest_dependence_chain / II)`. The prologue has `stages - 1` copies of the loop body (with partial execution), and the epilogue has `stages - 1` drain copies.

---

## Phases 35, 66, 79, 88: Loop-Invariant Code Motion (LICM)

**Purpose:** Hoists computations out of loops when they produce the same result on every iteration.

**Vtable addresses:**

| Phase | Name | Vtable |
|-------|------|--------|
| 35 | `OriHoistInvariantsEarly` | `off_22BDB40` |
| 66 | `OriHoistInvariantsLate` | `off_22BE018` |
| 79 | `OriHoistInvariantsLate2` | `off_22BE220` |
| 88 | `OriHoistInvariantsLate3` | `off_22BE388` |

**Shared execute function:** `sub_8FFDE0` (119 lines, `0x8FFDE0`)

**Core algorithm:** `sub_8FF780` (366 lines, `0x8FF780`)

### Entry Point (sub_8FFDE0)

All four LICM instances share `sub_8FFDE0` as their execute function. The `a2` parameter distinguishes the instance:

| Instance | a2 Value | Pipeline Position |
|----------|----------|-------------------|
| `OriHoistInvariantsEarly` | 0 | After `ExtractShaderConstsFirst` (phase 34) |
| `OriHoistInvariantsLate` | 1 | After `GeneralOptimizeLate2` (phase 65) |
| `OriHoistInvariantsLate2` | 2 | After `LateExpansionUnsupportedOps` (phase 78) |
| `OriHoistInvariantsLate3` | 3 | After `FixupGmmaSequence` (phase 87) |

### Gate Conditions

```
function hoistInvariants_execute(func, instance):
    if not func.has_loops:                          // +1368 bit 0
        return
    if count_instructions(func) <= 2:               // sub_7DDB50
        return                                      // trivial function
    if knob("HoistInvariants") is disabled:         // sub_799250
        return
    if func.loop_depth_array is empty:              // +520 == 0
        return
    
    // Per-block invariance check using knob 381
    for each loop block (via depth array at +512):
        bb = bb_array[depth_array[block_index]]
        if bb is empty (no instructions):
            continue
        
        // Query knob 381 for this specific block
        mode = knob_query(381, bb)
        if mode == 1 and instance == 1:
            goto do_hoist                   // "light" for late
        if mode == 3:
            goto do_hoist                   // "aggressive" always
        if mode == 0 and instance == 0:
            goto do_hoist                   // "default" for early
        continue
    
    do_hoist:
        run_licm_core(func, instance)
```

The knob 381 system provides per-block control: the knob container's `sub_7A1A90` and `sub_7A1B80` functions return a mode value (0=default, 1=light, 3=aggressive) that determines which instance should process the block.

### LICM Algorithm (sub_8FF780)

The core LICM pass operates on the loop depth ordering array (Code Object `+512`), which lists basic blocks sorted by decreasing loop depth. This ensures that inner loops are processed before outer loops, allowing invariants hoisted out of inner loops to be further hoisted out of enclosing loops.

```
function licm_core(func, instance):
    // Read knob for max hoisting distance
    knob_data = func.knob_container->options[+72]
    max_distance = 2   // default
    if knob_data[34632] == 1:
        max_distance = knob_data[34640]   // user override
    
    allow_speculative = (knob_data[20016] != 0)
    
    // Prepare function for analysis
    rebuild_predecessors(func)
    compute_def_use_chains(func)
    recompute_rpo(func)
    
    if func has partial SSA and instance > 1:
        rebuild_dominators(func)
    
    // Process loops from innermost outward
    innermost_header = bb_array[depth_array[last_entry]]
    
    for each loop (from innermost_header outward):
        // Find the preheader (block just before header)
        preheader = find_preheader(header)
        
        if header.rpo == preheader.rpo:
            is_self_loop = true         // single-block loop
        else:
            // Compute loop body extent
            analyze_loop_extent(func, header.rpo, max_rpo)
        
        // Identify the target block for hoisted instructions
        target = find_hoist_target(func, header, preheader)
        
        // Scan all instructions in the loop body
        for each instruction I in loop body (RPO order):
            if is_invariant(I, loop_body):
                // Check safety:
                // - No side effects (stores, barriers, calls)
                // - All operands defined outside loop or
                //   defined by already-hoisted instructions
                // - Not predicated (unless speculative allowed)
                // - Not a memory load (unless provably
                //   loop-invariant address)
                
                if safe_to_hoist(I):
                    move_to_block(I, target)
                    mark_moved = true
    
    // Post-processing
    if any instructions moved:
        rebuild_predecessors(func)
        if instance <= 2:
            recompute_def_use(func)
```

### Invariant Detection

An instruction is loop-invariant if all its source operands satisfy one of:
1. Defined by an instruction outside the loop body (checked via RPO number comparison).
2. Defined by an instruction already proven loop-invariant.
3. A constant or immediate value.

The detection iterates instructions within the loop body in RPO order, maintaining a worklist of candidates. An instruction becomes a candidate if all its source register definitions are either outside the loop or in the already-hoisted set. This naturally handles chains of invariant computations.

### Why Four Instances?

Each LICM instance catches invariants that preceding phases expose:

| Instance | Phase | Catches Invariants From |
|----------|-------|------------------------|
| Early (35) | After GeneralOptimize + shader const extraction | Constant-folded loop-invariant address computations, shader constant loads |
| Late (66) | After predication + late commoning | Predicated instructions converted to unconditional invariants, common subexpressions across loop bodies |
| Late2 (79) | After unsupported-op expansion | Materialization sequences for expanded operations (e.g., FP64 emulation setup, address mode computation) |
| Late3 (88) | After GMMA fixup | WGMMA pipeline setup computations, accumulator initialization sequences |

Instance 0 (Early) uses direction `+1` when calling `sub_A112C0` (the def-use chain rebuilder), while instances 1+ use direction `-1`. This controls whether the rebuilder processes blocks in forward or reverse order, which affects which invariants are discovered first.

---

## Phase 59: OriLoopFusion

**Purpose:** Fuses adjacent loops with compatible bounds to reduce loop overhead and improve data locality.

**Vtable:** `off_22BDF00` | **Entry:** `sub_1397CB0` (37 lines, `0x1397CB0`) | **Core:** `sub_1397360` (432 lines, `0x1397360`)

### Gate Conditions

Loop fusion has the strictest gating of any loop pass:

1. The function must have loops (`+1368` bit 0).
2. A two-level knob check at `knob_container->options[+72]`:
   - Byte at offset `37368` must equal `1` (fusion feature enabled).
   - DWord at offset `37376` must be non-zero (fusion aggressiveness > 0).
3. `NamedPhases` consulted: `sub_799250(knob_container, "LoopFusion")`.
4. Knob 487 must be enabled.

This strict gating means loop fusion is only active at certain optimization levels and may be entirely disabled for certain SM targets.

### Shared Infrastructure

Loop fusion shares the same 872-byte analysis context with pipelining (constructed by `sub_1389AF0`, destroyed by `sub_138A6E0`). This context includes:
- Loop header identification
- Dependence analysis results
- Loop bound comparison data

### Algorithm

```
function fuse_loops(context):
    identify_candidate_pairs()
    
    for each pair (loop_A, loop_B) of adjacent loops:
        // Compatibility checks:
        if loop_A.trip_count != loop_B.trip_count:
            continue    // bounds must match
        if loop_A.step != loop_B.step:
            continue    // stride must match
        if has_dependence(loop_A.body, loop_B.body):
            continue    // no cross-loop flow dependence
        if loop_A.nesting_depth != loop_B.nesting_depth:
            continue    // must be at same nesting level
        
        // Perform fusion:
        // 1. Merge loop_B's body into loop_A
        // 2. Remove loop_B's header and exit blocks
        // 3. Rewire control flow
        fuse(loop_A, loop_B)
```

Two loops are "adjacent" if loop_A's exit block immediately precedes loop_B's preheader (no intervening computation other than straight-line code between them). The dependence analysis checks that no instruction in loop_B reads a value that loop_A writes in a way that would be violated by merging the loop bodies.

### Pipeline Position Rationale

Loop fusion runs at phase 59, in the late optimization stage. This late position is deliberate:
- GeneralOptimizeLate (phase 58) has just run, simplifying loop bodies and exposing opportunities for fusion.
- Running after predication (phase 63 is later) would miss opportunities because predication may convert loops into straight-line code, eliminating fusion candidates. But running before predication means fused loops may still benefit from if-conversion.
- The result of fusion feeds into OriPerformLiveDeadThird (phase 61), which cleans up dead code introduced by the merge.

---

## GPU-Specific Loop Concerns

### Divergent Loop Bounds

When threads in a warp have different trip counts (divergent loop bounds), the hardware must keep all threads executing until the thread with the highest trip count finishes. Threads that have "exited" the loop are predicated off. This has several implications for loop optimization:

1. **Unrolling with divergent bounds** creates remainder iterations that must handle per-thread exit. The unroller uses predicated exit sequences rather than branch-based remainder loops.
2. **LICM with divergent loops** is safe because hoisted instructions are executed before any thread enters the loop -- thread divergence only occurs within the loop body.
3. **Loop fusion** requires uniform bounds. If either loop has potentially divergent bounds, fusion is not attempted because merging the bodies would require per-thread tracking of which loop each thread is "in."

### Register Pressure vs. Unroll Factor

On NVIDIA GPUs, the number of registers per thread determines occupancy (the number of concurrent warps). The relationship is:

```
max_regs_per_thread = total_registers_per_SM / (threads_per_warp * warps_per_SM)
```

For example, on SM90 (Hopper): 65536 registers / (32 threads * target_warps). If target occupancy is 4 warps, each thread gets 512 registers -- but higher occupancy requires fewer registers.

The unroller must balance:
- **More unrolling** = fewer branch instructions, more ILP, better instruction scheduling
- **More unrolling** = more live registers (each unrolled copy needs its own temporaries)
- **More registers** = lower occupancy = fewer warps to hide memory latency

The heuristic in `sub_887F00` estimates post-unroll register pressure by multiplying the loop body's live register count by the unroll factor, then checking against the register budget derived from the target occupancy.

### Warp-Level Software Pipelining

Software pipelining on GPUs faces a unique constraint: the prologue and epilogue must be executed by all threads in a warp, even if some threads would not need them (because they have fewer iterations). This is handled by predication: threads that have already "finished" their iterations execute the prologue/epilogue instructions under a false predicate, performing no-ops.

This predication overhead means pipelining is only beneficial when the loop body has sufficient memory latency to hide, and the trip count is large enough that the prologue/epilogue amortize to a negligible fraction.

---

## Knobs

The loop passes are controlled by the following knob system entries:

| Knob ID | Offset | Type | Controls | Default |
|---------|--------|------|----------|---------|
| 487 | vtable+152 | bool | Master optimization-level gate for loop passes | Varies by -O level |
| 381 | via `sub_7A1A90` | enum(0,1,3) | LICM aggressiveness per block (0=default, 1=light, 3=aggressive) | 0 |
| 220 | via `sub_6614A0` | bool | Speculative invariant hoisting safety | false |
| -- | options+34632 | byte | LICM max hoisting distance override (0=disabled, 1=use value at +34640) | 0 |
| -- | options+34640 | i32 | LICM max hoisting distance value (when +34632 == 1) | 2 |
| -- | options+20016 | byte | Allow speculative LICM (hoist potentially-faulting loads) | 0 |
| -- | options+37368 | byte | Loop fusion feature enable (must be 1) | Varies by -O level |
| -- | options+37376 | i32 | Loop fusion aggressiveness (must be > 0) | Varies by -O level |
| -- | options+19368 | byte | Unrolling mode override (0=force max, 1=use value at +19376) | 0 |
| -- | options+19376 | i32 | Forced unroll factor (when +19368 == 1) | N/A |

The `NamedPhases` system provides additional per-pass override capability. Users can disable individual passes via command-line options:

| NamedPhases String | Controls |
|--------------------|----------|
| `"LoopMakeSingleEntry"` | Phase 18 (loop simplification) |
| `"HoistInvariants"` | Phases 35, 66, 79, 88 (all four LICM instances) |
| `"Pipelining"` | Phase 24 (software pipelining) |
| `"LoopFusion"` | Phase 59 (loop fusion) |

Note that `"HoistInvariants"` is a single override controlling all four LICM instances -- there is no way to individually disable a specific instance through NamedPhases.

The `nounroll` pragma (string at `0x18FE126` in the binary) is recognized during PTX parsing and sets a flag on the loop header that causes the unroller to skip the loop unconditionally, regardless of knob settings.

---

## Function Map

| Address | Size | Function | Role |
|---------|------|----------|------|
| `0x78B430` | ~3 KB | `sub_78B430` | Loop simplification execute (phase 18) |
| `0x887F00` | ~20 KB | `sub_887F00` | Loop unrolling core (phase 22) |
| `0x87BE20` | -- | `sub_87BE20` | Loop body duplication helper |
| `0x86CE90` | -- | `sub_86CE90` | Loop collection: marks back-edge instructions |
| `0x872A30` | -- | `sub_872A30` | Instruction advance helper for unrolling |
| `0x869900` | -- | `sub_869900` | Instruction metadata collection for unrolling |
| `0x873630` | -- | `sub_873630` | Unrolling context initialization |
| `0x697390` | ~4.5 KB | `sub_697390` | Trip count extraction from exit instruction |
| `0x7E39B0` | ~0.5 KB | `sub_7E39B0` | Unroll factor table lookup |
| `0xA107B0` | ~4 KB | `sub_A107B0` | Per-loop unrolling eligibility check |
| `0xA07940` | -- | `sub_A07940` | Loop body legality checker for unrolling |
| `0xA0C410` | -- | `sub_A0C410` | Induction variable analysis |
| `0xA0C4A0` | -- | `sub_A0C4A0` | Instruction count within loop body |
| `0x13967C0` | ~0.3 KB | `sub_13967C0` | Pipelining execute (phase 24) |
| `0x1395850` | ~9 KB | `sub_1395850` | Pipelining core algorithm |
| `0x1389AF0` | ~7 KB | `sub_1389AF0` | Pipelining/fusion shared context constructor |
| `0x138A6E0` | -- | `sub_138A6E0` | Pipelining/fusion shared context destructor |
| `0x8FFDE0` | ~1.2 KB | `sub_8FFDE0` | LICM execute (phases 35, 66, 79, 88) |
| `0x8FF780` | ~4.5 KB | `sub_8FF780` | LICM core algorithm |
| `0xA112C0` | -- | `sub_A112C0` | Def-use chain rebuilder (direction-dependent) |
| `0x1397CB0` | ~0.4 KB | `sub_1397CB0` | Loop fusion execute (phase 59) |
| `0x1397360` | ~5 KB | `sub_1397360` | Loop fusion core algorithm |
| `0x74D720` | -- | `sub_74D720` | Loop extent analyzer (header-to-exit range) |
| `0x74F500` | -- | `sub_74F500` | Hoist target finder |
| `0x785E20` | -- | `sub_785E20` | Predecessor/RPO rebuilder |
| `0x789280` | -- | `sub_789280` | Dominator recomputation |
| `0xBDEA50` | -- | `sub_BDEA50` | Backedge debug printer (RPO/backedge dump) |
| `0xA3A7E0` | -- | `sub_A3A7E0` | Statistics emitter (prints loop unrolling stats) |
| `0x931920` | -- | `sub_931920` | Edge splitter (creates new BB for preheader) |
| `0x932E80` | -- | `sub_932E80` | Branch target rewriter |
| `0x92E1B0` | -- | `sub_92E1B0` | Branch instruction insertion |

## Cross-References

- [Pass Inventory & Ordering](index.md) -- Full 159-phase table, pipeline position context
- [Ori IR Overview](../ir/overview.md) -- Code Object field map, backedge hash map description
- [Strength Reduction](strength-reduction.md) -- Phase 21, runs immediately before unrolling
- [Liveness Analysis](liveness.md) -- Phases 16, 33, 61, 84: clean up dead code after loop transforms
- [GeneralOptimize Bundles](general-optimize.md) -- Phases 13, 29, 37, 46, 58, 65: constant folding and copy propagation between loop passes
- [Predication](predication.md) -- Phase 63: if-conversion that exposes new LICM opportunities
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- GMMA fixup (phase 87) that triggers LICM Late3
- [Late Legalization](late-legalization.md) -- Unsupported-op expansion (phase 78) that triggers LICM Late2
- [Knobs System](../config/knobs.md) -- Knob 381, 487, 220 and the NamedPhases mechanism
- [Scheduling](../scheduling/overview.md) -- Instruction scheduling benefits from unrolled loops
