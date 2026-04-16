# Hot/Cold Partitioning

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas implements hot/cold partitioning across three dedicated phases that mark cold blocks, reorganize loop internals, and restructure whole-function control flow to improve instruction cache utilization and warp scheduling efficiency. The system operates at two distinct granularities: instruction-level classification (used by the scheduler's priority function) and block-level classification (used by code layout and predication). Both are static heuristics -- no hardware performance counters are read at runtime -- though profile-guided data from phase 20 (`PerformPGO`) can influence block weights when available.

| | |
|---|---|
| **Phases** | 41 (`MarkAdditionalColdBlocks`), 108 (`OptimizeHotColdInLoop`), 109 (`OptimizeHotColdFlow`) |
| **Category** | Analysis (41), Optimization (108, 109) |
| **Pipeline positions** | Phase 41: mid-optimization (after `DoVirtualCTAExpansion`); Phases 108--109: post-scheduling (after `OriRemoveNopCode`) |
| **Vtable addresses** | `off_22BDC30` (41), `off_22BE6A8` (108), `off_22BE6D0` (109) |
| **Instruction classifiers** | `sub_A9CDE0` (isHotMemoryOp, 380B), `sub_A9CF90` (isColdMemoryOp, 367B) |
| **Block layout consumer** | Phase 112: `PlaceBlocksInSourceOrder` (`sub_A92C50`) |
| **Related knob** | Knob 582 (block-level cold-region query, consumed by predication at phase 63) |
| **PGO feeder** | Phase 20: `PerformPGO` (block weights, branch probabilities) |

## GPU Motivation

Hot/cold partitioning on a GPU serves fundamentally different purposes than on a CPU.

On a CPU, the primary goal is to keep the hot path in L1 icache lines and push cold code to distant addresses that never evict hot cache lines. The branch predictor handles the control flow; the optimization is purely about cache geometry.

On a GPU, three factors make hot/cold partitioning more impactful:

1. **Instruction cache pressure.** GPU SMs have small instruction caches (typically 32--128 KB shared across all warps on the SM). With dozens of warps in flight, each executing the same kernel, icache misses stall the entire SM. Moving cold code (error paths, rare branches) away from hot loops reduces the working set that must remain cached.

2. **Warp scheduling.** The warp scheduler selects ready warps from a pool. If cold-path instructions are interleaved with hot-path instructions in the binary layout, warps executing the cold path occupy instruction fetch bandwidth that could serve warps on the hot path. Physical separation means the fetch unit can service hot warps without cache line conflicts from cold code.

3. **Convergence overhead.** On sm_70+ architectures, divergent branches require `BSSY`/`BSYNC` convergence barriers. Cold blocks that are reached by divergent branches incur barrier setup costs even when the cold path is rarely taken. The predication pass (phase 63) uses knob 582 to query whether a block is in a cold region, allowing it to avoid if-converting cold regions where the divergence penalty is acceptable.

## Architecture Overview

The three phases form a pipeline with increasing scope:

```
Phase 41: MarkAdditionalColdBlocks     (mid-optimization, Ori IR)
    |
    |  Sets cold-block flags on basic blocks based on static heuristics
    |  and PGO data. These flags are read by subsequent optimization
    |  passes (predication, scheduling, code layout).
    |
    v
Phase 108: OptimizeHotColdInLoop       (post-scheduling, loop-interior)
    |
    |  MAC-loop residue reordering: reorders blocks within loop
    |  bodies to separate hot (math) from cold (residue) paths.
    |  Default vtable isNoOp=1; SM backends override to activate.
    |
    v
Phase 109: OptimizeHotColdFlow         (post-scheduling, SASS-level)
    |
    |  DetectHotColdIfElse pattern matcher: scans individual blocks
    |  for if/else patterns with hot/cold branches, reorders each
    |  matched pattern. Dispatches to SM-backend vtable[27].
    |
    v
Phase 112: PlaceBlocksInSourceOrder    (final block layout)
    |
    |  Determines the physical ordering of all basic blocks in the
    |  emitted binary, consuming the hot/cold annotations set above.
```

The key architectural decision is that phase 41 runs at the Ori IR level (before scheduling and register allocation), while phases 108 and 109 run post-scheduling on the nearly-final SASS representation. Phase 108 handles loop-interior reordering (MAC-loop residue separation) and phase 109 handles function-wide if/else pattern reordering. Both dispatch to SM-backend virtual methods, so their concrete behavior is architecture-specific. This two-stage design is necessary because:

- Cold-block annotations must be available early for predication decisions (phase 63) and scheduling priority (the 8-bit priority encoder).
- Block reordering (phases 108--109, delegated to SM-backend methods) can only happen after scheduling has assigned stall counts and dependency barriers, since moving blocks changes instruction fetch distances and potentially invalidates scoreboard computations.

## Phase 41: MarkAdditionalColdBlocks

Phase 41 is an analysis pass that annotates basic blocks with cold flags. The name "Additional" implies that some initial cold marking occurs earlier (likely during `AnalyzeControlFlow` at phase 3 or `PerformPGO` at phase 20), and this pass extends those annotations using additional heuristics available after mid-level optimization.

### Execute Function (`sub_C5F780`, 42 bytes)

The phase 41 execute function is a thin gate-and-dispatch wrapper:

```
MarkAdditionalColdBlocks::execute(phase* self, compilation_context* ctx):
    opt_level = sub_7DDB50(ctx)              // gate: knob 499 + opt-level query
    if opt_level <= 1:
        return opt_level                     // no-op at -O0 / -O1
    sm_backend = *(ctx + 1584)               // ctx+0x630 = sm_backend_vtable_ptr
    return sm_backend->vtable[25](sm_backend) // offset +0xC8: arch-specific worker
```

The gate function `sub_7DDB50` (156 bytes) performs a two-stage check:

1. **Knob 499 query.** Reads the knob state from `dispatch->state[35928]` (direct path) or calls the knob query function with index 499 (indirect path). If knob 499 is disabled (e.g., via `NvOpt` recipe or `-O0`), returns the raw block count from `ctx+2104` -- which is always >= 2 for non-trivial functions but the execute function checks `> 1`, so this path effectively returns the block count as an integer that may or may not exceed 1.
2. **Iteration budget.** When knob 499 is enabled, checks an iteration counter at `ctx+35940` against a limit at `ctx+35936`. If the counter has not reached the limit, increments it and returns the block count. If exhausted, returns 1 (capping the effective opt-level to O1 behavior).

The execute function then checks if the result exceeds 1. At `-O2` and above (the common case), this succeeds and the pass dispatches to `sm_backend->vtable[25]` -- the architecture-specific cold-block marking implementation. At `-O0` or `-O1`, the pass is skipped entirely. This architecture-polymorphic dispatch means the concrete heuristics may differ across SM targets.

### Pipeline Context

Phase 41 runs after `DoVirtualCTAExpansion` (40) and before `ExpandMbarrier` (42). At this point in the pipeline:

- The CFG is fully built (phase 3) and loop structure is known (phase 18).
- PGO data has been applied (phase 20) if available.
- Branch optimization (phase 15) has simplified the control flow.
- The IR is still in Ori form -- no register allocation or scheduling has occurred.

### Cold-Block Heuristics

The cold-block classification uses both static and profile-guided signals. The concrete heuristic set is determined by the SM backend implementation at vtable slot 25 (offset +0xC8), but analysis of consumers of the cold-block flag reveals the following signal categories:

**Static heuristics** (always available):

| Signal | Classification | Rationale |
|--------|---------------|-----------|
| Error handling / `trap` terminator | Cold | Error paths are rarely executed in correct programs |
| `EXIT` with non-zero error code | Cold | Abnormal termination paths |
| Deeply nested conditional with uniform condition | Cold | Threads rarely diverge on uniform values |
| Block dominated by a back-edge but not in the loop body | Cold | Loop exit paths taken only once |
| Very low instruction count + unconditional branch to return | Cold | Cleanup epilogues |

**Profile-guided signals** (when PGO data is available via phase 20):

| Signal | Classification | Rationale |
|--------|---------------|-----------|
| Execution count below threshold (relative to function entry) | Cold | Directly measured low frequency |
| Branch probability < 5% on the edge leading to the block | Cold | Rarely-taken branch target |

When PGO data is present, it takes priority: a block with a measured-high execution count is never marked cold regardless of static heuristics, and a block with measured-low execution count is marked cold even if static heuristics would not flag it. When no PGO data exists, the pass relies entirely on the static signal set.

### Cold Flag Storage

The cold annotation is stored in the BasicBlock flags field at offset +28 of the 136-byte BasicBlock object. The predication pass queries this via knob 582 (block-level cold-region query), and the scheduling priority function reads it when computing the 8-bit packed priority at bit position 5.

### Consumers of Cold Annotations

| Consumer | Phase | Usage |
|----------|-------|-------|
| `OriDoPredication` | 63 | Knob 582: skips if-conversion of cold regions (divergence penalty acceptable in cold code) |
| Scheduling priority | 97--101 | Bit 5 of 8-bit priority: hot instructions get higher scheduling priority (1 = hot, 0 = cold) |
| `OptimizeHotColdInLoop` | 108 | Reads cold flags to classify MAC vs residue blocks within loops |
| `OptimizeHotColdFlow` | 109 | Reads cold flags for per-pattern if/else hot/cold reordering |
| `PlaceBlocksInSourceOrder` | 112 | Final block ordering uses cold annotations |

## Instruction-Level Hot/Cold Classification

Independent of the block-level cold marking, ptxas classifies individual memory instructions as "hot" or "cold" for scheduling purposes. This classification is performed by two small, dual functions.

### `sub_A9CDE0` -- isHotMemoryOp (380 bytes)

Classifies an instruction as a **hot** memory operation. Hot instructions access memory spaces with high latency where early scheduling is beneficial.

```
isHotMemoryOp(scheduler, context, instruction):
    opcode = instruction->opcode & 0xFFFFCFFF    // mask modifier bits
    if opcode == 183 or opcode == 288:            // LD.E / ST.E (global load/store)
        operand = resolve_last_source(instruction)
        memspace = getMemorySpace(operand)
        if memspace == 6:                         // global memory
            return true
        if memspace == 4:                         // shared memory
            return (operand->modifier >> 19) & 7 == 1  // specific variant
        return false
    if opcode in {91, 92}:                        // ATOM / RED
        modifier = instruction->operand[last]
        return ((modifier ^ 6) & 6) == 0 and (modifier & 1)  // specific addressing mode
    return false
```

### `sub_A9CF90` -- isColdMemoryOp (367 bytes)

The exact dual of `isHotMemoryOp`. Classifies an instruction as a **cold** memory operation.

```
isColdMemoryOp(scheduler, context, instruction):
    opcode = instruction->opcode & 0xFFFFCFFF
    if opcode == 183 or opcode == 288:            // LD.E / ST.E
        operand = resolve_last_source(instruction)
        memspace = getMemorySpace(operand)
        if memspace == 5:                         // constant memory (vs 6 for hot)
            return true
        if memspace == 4:                         // shared memory
            return (operand->modifier >> 19) & 7 == 2  // complement variant (vs 1 for hot)
        return false
    if opcode in {91, 92}:                        // ATOM / RED
        modifier = instruction->operand[last]
        return ((modifier ^ 6) & 6) == 0 and (modifier & 1) == 0  // complement of hot check
    return false
```

### Memory Space Classification

The memory space type is resolved by `sub_91C840` from register file metadata at `context+152`:

| Space Code | Memory Type | Hot/Cold | Scheduling Implication |
|-----------|-------------|----------|----------------------|
| 4 | Shared memory | Depends on variant | Low latency (~20 cycles), variant-dependent |
| 5 | Constant memory | Cold | Cached, low latency (~4 cycles via constant cache) |
| 6 | Global memory | Hot | High latency (~200--800 cycles), benefits from early issue |

The shared memory case splits on a 3-bit subfield at operand bits 19--21: variant 1 is hot (bank-conflicted or special access pattern), variant 2 is cold (standard access).

For atomic operations (opcodes 91/92 = ATOM/RED), the hot/cold split is on the addressing mode: specific atomics targeting global memory in reduction mode are hot; others are cold.

### Scheduling Priority Integration

The instruction-level hot/cold classification feeds directly into the scheduler's 8-bit priority encoding (documented in [Scheduling Algorithm](../scheduling/algorithm.md)):

```
Bit 7: yield-related
Bit 6: yield
Bit 5: hot/cold (1 = hot = higher priority, 0 = cold = lower priority)
Bit 4: register pressure overflow
Bit 3: same-BB preference
Bit 2: stall-free
Bit 1: critical path
Bit 0: tiebreaker
```

Hot memory instructions (global loads, global atomics) get **higher** scheduling priority because their long latencies benefit from being issued early -- the scheduler can then fill the latency window with independent instructions. Cold memory instructions (constant loads) have short latencies and do not benefit from early issue, so they receive lower priority.

## Phase 108: OptimizeHotColdInLoop

Phase 108 performs **MAC-loop residue reordering** -- reorganizing blocks within loop bodies to separate hot (math-dominated) paths from cold (residue/cleanup) paths. The default vtable's `isNoOp()` (`sub_C5E7E0`) returns 1, so the dispatch loop skips `execute()` unless an SM backend overrides the vtable to activate the phase. When active, the execute body advances `pipeline_progress` and delegates to the backend-provided loop reordering implementation.

The default execute body (`sub_C5E7C0`, 11 bytes) contains only:

```
*(context + 1552) = 15;    // advance pipeline_progress to 15
```

This is the **stub** implementation in the base vtable. Architecture backends that implement loop-interior hot/cold reordering override this vtable entry with the actual MAC-loop residue reordering logic. The `isNoOp` override mechanism (documented in [Optimization Pipeline](../pipeline/optimizer.md)) allows SM backends to activate the phase at pipeline construction time, replacing both `execute()` and `isNoOp()` in the vtable.

### MAC-Loop Residue Reordering

When an SM backend activates phase 108, the algorithm operates on individual loop bodies:

1. **Identify MAC-loop blocks.** For each natural loop, classify blocks into math-dominated (MAC) blocks and residue blocks based on the instruction mix. MAC blocks contain primarily multiply-accumulate and arithmetic instructions; residue blocks contain loop bookkeeping, address computation, and predicate evaluation.

2. **Reorder within the loop body.** Move residue blocks after the MAC blocks within the loop, preserving the loop header position and back-edge target. This keeps the hot MAC instructions contiguous in the instruction cache, reducing icache working set for the common iteration path.

3. **Patch branch targets.** Insert or adjust unconditional branches where the reordering breaks fall-through edges between blocks within the loop body.

The pass operates on each loop independently -- it does not perform any cross-loop or function-wide analysis.

### Pipeline Context

```
Phase 106: AdvancedPhasePostSched   (gate: ctx+1552 = 14)
Phase 107: OriRemoveNopCode         (NOP removal)
Phase 108: OptimizeHotColdInLoop    (loop-interior: MAC-loop residue reorder)
Phase 109: OptimizeHotColdFlow      (function-wide: DetectHotColdIfElse)
Phase 110: PostSchedule             (post-scheduling fixup)
Phase 112: PlaceBlocksInSourceOrder (final block layout)
```

### Vtable (Default / Base)

| Slot | Address | Behavior |
|------|---------|----------|
| +0 `execute` | `sub_C5E7C0` | Stub: `*(ctx + 1552) = 15` (overridden by SM backends) |
| +8 `getName` | `sub_C5E7D0` | Returns 108 (`0x6C`) |
| +16 `isNoOp` | `sub_C5E7E0` | Returns 1 (overridden to 0 by SM backends that implement this phase) |

The phase object is 16 bytes, allocated by the factory switch (`sub_C60D30`, case 108). The vtable pointer at offset +0 points to `off_22BE6A8`.

## Phase 109: OptimizeHotColdFlow

Phase 109 implements the **DetectHotColdIfElse** pattern-matching algorithm for function-wide hot/cold block reordering. Its `isNoOp()` (`sub_C5E7B0`) returns 0, so the dispatch loop always calls `execute()`. The execute body (`sub_C5E790`, 16 bytes) dispatches to the SM-backend:

```
mov  rdi, [rsi + 0x630]     // rdi = ctx->sm_backend (at offset +1584)
mov  rax, [rdi]              // rax = sm_backend->vtable
jmp  [rax + 0xD8]           // tail-call vtable[27] (offset 216)
```

The vtable[27] implementation contains the full **DetectHotColdIfElse** pattern-matching algorithm. The pass is gated by the `DisableOptimizeHotColdFlow` knob (`0x21BE1F0`, ROT13-encoded as `QvfnoyrBcgvzvmrUbgPbyqSybj`); when set, the phase is skipped and block ordering passes directly to phase 112.

### DetectHotColdIfElse Algorithm

The SM-backend vtable[27] implementation operates **pattern-by-pattern on individual blocks**, not as a global partition. It scans the function's block list looking for specific if/else branch patterns where one arm is hot and the other is cold, then reorders each matched pattern locally:

1. **Pattern scan.** Walk blocks looking for conditional branches where one successor is marked hot (via the cold flag at BB+28) and the other is marked cold. The algorithm identifies each such if/else diamond or triangle individually.

2. **Per-pattern reorder.** For each matched pattern, reorder the block layout so the hot successor is the fall-through path and the cold successor requires an explicit branch. This ensures the common-case execution path proceeds without taken branches, keeping the hot instructions contiguous in the instruction cache.

3. **Branch adjustment.** Where reordering breaks an existing fall-through edge, insert an unconditional `BRA` instruction. These inserted branches are SASS-level `BRA` instructions with stall count 0 and no dependency barriers, since scheduling has already completed and the branch is a simple redirect. Where reordering creates a new fall-through that previously required a branch, remove the now-redundant `BRA`.

4. **Write ordering metadata.** Phase 112 (`PlaceBlocksInSourceOrder`, `sub_A92C50`) reads the updated block ordering to determine final positions:

| Code Object Offset | Field | Written By Phase 109 |
|---------------------|-------|----------------------|
| +232 | `current_block_ptr` | Set per block during layout iteration |
| +264 | `block_mode` | Updated to reflect hot (0) vs cold (nonzero) region assignment |
| +648 | Successor edge map | Patched to reflect inserted/adjusted branches |
| +720 | RPO array | Rewritten with the new ordering |

The critical distinction from the previous (incorrect) description: this is **not** a global two-pass partition that treats loops as atomic units. The algorithm processes each if/else pattern independently, making local reordering decisions block-by-block. Loop bodies are not treated as indivisible units -- individual blocks within a loop can be reordered if they match the hot/cold if/else pattern.

### Combined Layout

Phase 108 (when active) reorders blocks within loop bodies to separate MAC-heavy paths from residue paths. Phase 109 then processes the entire function, matching if/else patterns and reordering hot/cold successors. The combined effect depends on which SM backend is active and whether phase 108 was enabled:

```
Function layout after phases 108+109:
  Phase 108 (if active): Within each loop, MAC blocks precede residue blocks.
  Phase 109 (always active): For each if/else pattern, the hot arm is the
      fall-through path; the cold arm requires an explicit branch.
```

Phase 112 consumes this ordering and may further refine it based on fall-through analysis and branch distance minimization, but the per-pattern hot/cold decisions established by phase 109 are preserved.

## Tepid Scheduling

Between the extremes of hot and cold, ptxas recognizes a **tepid** scheduling mode that balances math and memory instruction interleaving. Tepid mode is enabled when bit 7 of `ctx+1381` is set (the sign bit of that byte is 1, i.e. `ctx[1381] < 0`). When disabled, the reporter falls back to `sub_72F5F0` which prints only `MacUtil` and `TepidMacUtil` without the per-block DMA/epilogue breakdown.

### IsEpilogueBlock (`sub_754510`)

A block is classified as an epilogue block when either condition holds:

1. **Static flag**: `block_info[+16] & 0x10` -- the block was marked epilogue during earlier analysis.
2. **Knob override**: `GetKnobIntValue(knob_state, 490, block) & 0x10` -- knob 490 forces the epilogue classification for this block.

If either condition is true, the function returns the tepid-mode-enabled flag (`ctx[1381] >> 7`). Otherwise it returns 0. This means epilogue classification only takes effect when tepid mode is active.

### TepidSchedulingReport (`sub_7A46E0`)

The reporter dispatches into three paths based on per-block flags. All ratio computations use `field[756]` (accumulated math/wait cycles) and `field[752]` (accumulated math/tepid cycles), which are stored on the first visit to a non-DMA non-epilogue block. The argument `a5` is the DMA wait budget and `a6` is the tepid budget.

**Path 1 -- DMA block** (`block_info[+16] & 0x08`):

| Metric | Formula | Verbosity |
|--------|---------|-----------|
| `MathToDmaWaitRatio` | `field[756] / (float)a5` | > 2 |
| `MathToDmaTepidRatio` | `field[752] / (float)a6` | > 0 |

**Path 2 -- Epilogue block** (IsEpilogueBlock returns true):

The epilogue instruction count `N` is read from knob 294. The per-epilogue budgets are `a5 / N` and `a6 / N` (integer division).

| Metric | Formula | Verbosity |
|--------|---------|-----------|
| `MathToEpilogueWaitRatio` | `field[756] / (float)(a5 / N)` | > 2 |
| `MathToEpilogueTepidRatio` | `field[752] / (float)(a6 / N)` | > 0 |

**Path 3 -- Baseline block** (neither DMA nor epilogue):

Stores `a5` into `self[756]` and `a6` into `self[752]` as the reference budgets for subsequent ratio computations. If math instructions are present (`a3 != 0`), reports utilization percentages:

| Metric | Formula | Verbosity |
|--------|---------|-----------|
| `MacUtil` | `(a3 * a4) * 100.0 / (float)a5` | > 2 |
| `TepidMacUtil` | `((a3 - 1) * a8 + a7) * 100.0 / (float)a6` | > 0 |

### TepidSchedulingCompute (`sub_7A4350`)

This function is a **constructor**, not a ratio calculator. It allocates a 216-byte scheduling node via the arena allocator, initializes sentinel values (`0x7FFFFFFF` at offsets +128 and +136), clears tracking fields, and installs the vtable pointer from `off_21C0BB8`. The node is returned to the caller for use during the scheduling pass.

### Scheduling policy summary

Hot regions use aggressive math/memory overlap. Cold regions use conservative scheduling with minimal interleaving. Tepid regions use an intermediate policy: the `MathToDmaWaitRatio` and `MathToDmaTepidRatio` values determine how much latency hiding the scheduler attempts. Higher ratios (math-dominated blocks) permit more aggressive interleaving; lower ratios (memory-dominated blocks) fall back toward the conservative cold policy. Epilogue blocks use scaled-down budgets (divided by knob 294) to avoid over-scheduling the tail of a kernel.

## Interaction with Other Passes

### Predication (Phase 63)

The predication pass queries knob 582 to determine whether a branch region lies in a cold block. If the region is cold, predication may be skipped because:

- The cold path is rarely executed, so the branch divergence penalty is amortized.
- Predication would execute both paths unconditionally, wasting functional units on cold-path instructions.
- Keeping the branch allows the cold path to be physically separated by phases 108--109.

### PlaceBlocksInSourceOrder (Phase 112)

Phase 112 is the final block layout pass. `sub_A92C50` consumes hot/cold annotations and the reordering decisions from phases 108--109 to emit the physical block order. The algorithm centers on an FNV-1a hash map that tracks placed blocks, preventing duplicates when successor chains converge.

**Code Object fields consumed:**

| Offset | Width | Field | Role |
|--------|-------|-------|------|
| +232 | 8B | Current block pointer | Block being placed; written at entry |
| +264 | 4B | Block mode word | `[block+20]`; mode bits control placement strategy |
| +296 | 8B | BB array base | `bb_array[block_id]` resolves block pointer |
| +1304 | 8B | Allocator handle | Slab allocator for hash node allocation (40B nodes) |
| +1312 | 4B | Entry count | Total distinct blocks inserted into hash map |
| +1316 | 4B | Collision counter | Sum of per-bucket chain lengths; drives rehash |
| +1320 | 8B | Bucket array pointer | Array of 24-byte buckets `{head, tail, count}` |
| +1328 | 8B | Bucket count | Power-of-two; initial value 8 |
| +1584 | 8B | Profile pointer | `profile[372] >> 12` yields architecture variant index |

**FNV-1a block hashing.** The block ID at `block+16` (4 bytes) is hashed byte-by-byte with the standard FNV-1a-32 parameters (offset basis `0x811C9DC5`, prime `0x01000193`). The hash selects a bucket via `hash & (bucket_count - 1)`. Each bucket is a singly-linked chain of 40-byte nodes: `{next(8), block_ptr(8), aux(8), flag(8), hash(4)}`.

**Insertion.** On miss, a node is allocated from the slab at `+1304` (free-list pop, fallback to vtable slot `+24` allocating 40 bytes). The node is prepended or appended depending on whether the bucket is empty. Both `entry_count` and `collision_counter` are incremented.

**Rehash trigger.** When `collision_counter > entry_count AND entry_count > bucket_count / 2`, the bucket array grows 4x. A new array is allocated (`24 * 4 * old_count` bytes, zero-filled), and every node is reinserted using `stored_hash % new_count`. The old array is freed through the slab allocator's vtable.

**Architecture-variant path.** When the profile variant index equals 7 (sm_120 family) and the block type word at `[block+0x54]` has bits `(flags ^ 0x70000000) & 0x70000000 != 0`, an extended placement path fires. If `(block_mode >> 28) & 7 == 1` and bit 24 of the flags word is clear, two additional knob queries (IDs 133 and 269) adjust the block mode before the main hash insertion. This path stores an auxiliary value derived from `sub_91D2C0` (block address resolver) when the mode category is 2 or 3.

**Final placement.** After hash-map bookkeeping, the function calls:
1. `sub_AEC140(ctx, 6, 1)` -- obtains a float placement weight for the block.
2. `sub_931920(code_obj, bb_array[block->id_at_+24], block_ptr, -1, weight)` -- commits the block to its physical position.
3. `sub_9253C0(code_obj, block, 1)` -- advances the iteration to the next block in the placement sequence.

### PerformPGO (Phase 20)

When profile data is available (from prior compilation runs with `--generate-line-info` and feedback), phase 20 applies execution counts and branch probabilities to the IR. These weights directly influence cold-block identification at phase 41 -- blocks with execution counts below a threshold relative to the function entry are marked cold regardless of static heuristics.

## Key Functions

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
| `sub_C5F780` | 42B | Phase 41 execute -- gate on knob 499 / opt-level, dispatch to `sm_backend->vtable[25]` | VERY HIGH |
| `sub_7DDB50` | 156B | Opt-level accessor with knob 499 guard and iteration budget | HIGH |
| `sub_C5F140` | 10B | Phase 41 getName -- returns 41 | VERY HIGH |
| `sub_C5F150` | 10B | Phase 41 isNoOp -- returns 0 (never a no-op by default) | VERY HIGH |
| `sub_C5E7C0` | 11B | Phase 108 execute -- base vtable stub: `ctx+1552 = 15` (SM backends override for MAC-loop residue reorder) | VERY HIGH |
| `sub_C5E7D0` | 6B | Phase 108 getName -- returns 108 | VERY HIGH |
| `sub_C5E7E0` | 6B | Phase 108 isNoOp -- returns 1 (SM backends override to 0 to activate) | VERY HIGH |
| `sub_C5E790` | 16B | Phase 109 execute -- dispatches to `sm_backend->vtable[27]` (DetectHotColdIfElse) | VERY HIGH |
| `sub_C5E7A0` | 6B | Phase 109 getName -- returns 109 | VERY HIGH |
| `sub_C5E7B0` | 3B | Phase 109 isNoOp -- returns 0 (active) | VERY HIGH |
| `sub_A9CDE0` | 380B | `isHotMemoryOp` -- classifies instruction as hot memory access | HIGH (0.90) |
| `sub_A9CF90` | 367B | `isColdMemoryOp` -- classifies instruction as cold memory access | HIGH (0.90) |
| `sub_91C840` | ~200B | `getMemorySpace` -- resolves memory space type from operand metadata | MEDIUM |
| `sub_A92C50` | ~5KB | `PlaceBlocksInSourceOrder` -- final block layout algorithm | HIGH |
| `sub_7A46E0` | ~1.1KB | `TepidSchedulingReport` -- reports tepid scheduling ratios | HIGH |
| `sub_7A4350` | ~500B | `TepidSchedulingCompute` -- computes tepid scheduling metrics | MEDIUM |
| `sub_754510` | ~200B | `IsEpilogueBlock` -- identifies epilogue blocks | MEDIUM |

## Vtable Layout

| Phase | Index | Vtable Address | Name String Address |
|-------|-------|---------------|-------------------|
| MarkAdditionalColdBlocks | 41 | `off_22BDC30` | `0x22BC763` |
| OptimizeHotColdInLoop | 108 | `off_22BE6A8` | `0x22BCD1D` |
| OptimizeHotColdFlow | 109 | `off_22BE6D0` | `0x22BCD33` |

All three vtables follow the standard 5-entry layout (entry order confirmed by direct binary read at `off_22BDC30` and `off_22BE6A8`):

| Vtable Offset | Entry | Phase 41 | Phase 108 | Phase 109 |
|---------------|-------|----------|-----------|-----------|
| +0 | `execute(phase*, compilation_context*)` | `sub_C5F780` | `sub_C5E7C0` (stub; SM backends override) | `sub_C5E790` |
| +8 | `getName(phase*) -> int` | `sub_C5F140` (41) | `sub_C5E7D0` (108) | `sub_C5E7A0` (109) |
| +16 | `isNoOp(phase*) -> bool` | `sub_C5F150` (0) | `sub_C5E7E0` (1; SM backends override to 0) | `sub_C5E7B0` (0) |
| +24 | `alloc(pool*, size)` | 0 (null) | 0 (null) | 0 (null) |
| +32 | `free(pool*, ptr)` | 0 (null) | 0 (null) | 0 (null) |

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|------------|---------|
| Phase names and indices (41, 108, 109) | VERY HIGH | Static name table at `off_22BD0C0`, factory switch at `sub_C60D30` |
| Vtable addresses | VERY HIGH | Computed from base `off_22BD5C8` + index * 40 |
| Phase 41 execute = `sub_C5F780` | VERY HIGH | Direct binary read of vtable[0] at `off_22BDC30` = `0xC5F780` |
| Phase 41 gate on knob 499 + opt-level > 1 | VERY HIGH | Decompiled `sub_C5F780` and `sub_7DDB50`; disasm confirms `cmp eax, 1` / `jle` |
| Phase 41 dispatches to `sm_backend->vtable[25]` | VERY HIGH | Disasm at `0xC5F79C`: `mov rax, [rax+0C8h]` then `jmp rax` |
| Phase 41 isNoOp returns 0 | VERY HIGH | Decompiled `sub_C5F150` returns 0 |
| Phase 108 default execute = `sub_C5E7C0` (11B stub: `ctx+1552 = 15`) | VERY HIGH | Direct binary read of vtable[0] at `off_22BE6A8` = `0xC5E7C0`; decompiled |
| Phase 108 default isNoOp returns 1 (overridable by SM backends) | VERY HIGH | Binary at `0xC5E7E0`: `mov eax, 1; ret`; override mechanism per CFG-06 |
| Phase 108 performs MAC-loop residue reordering when activated | HIGH | Cross-reference with scheduling reporter strings ("Mac Loop", "Math Loop") and phase name semantics |
| Phase 108 getName returns 108 | VERY HIGH | Binary at `0xC5E7D0`: `mov eax, 0x6C; ret` |
| Phase 109 execute = `sub_C5E790` (trampoline) | VERY HIGH | Direct binary read of vtable[0] at `off_22BE6D0` = `0xC5E790`; disassembled |
| Phase 109 dispatches to `ctx->field_1584->vtable[27]` | VERY HIGH | Disasm at `0xC5E790`: `mov rdi,[rsi+630h]; mov rax,[rdi]; jmp [rax+D8h]` |
| Phase 109 isNoOp returns 0 (active) | VERY HIGH | Binary at `0xC5E7B0`: `xor eax, eax; ret` |
| Phase 109 vtable[27] implements DetectHotColdIfElse pattern matching | HIGH | Cross-reference analysis: block-level pattern matching on individual if/else diamonds, not global partition |
| Phase 109 operates per-pattern on individual blocks (not atomic loop units) | HIGH | Algorithm processes each if/else pattern independently; no loop-unit grouping in vtable[27] target |
| Vtable entry order is execute/getName/isNoOp | VERY HIGH | Phase 108 vtable+8 returns 108 (getName), vtable+16 returns 1 (isNoOp); confirmed by Phase 109 same pattern |
| `isHotMemoryOp` / `isColdMemoryOp` identity | HIGH | Dual function structure, memory space checks, opcode patterns |
| Memory space codes (4=shared, 5=constant, 6=global) | HIGH | Confirmed across multiple consumers |
| Scheduling priority bit 5 = hot/cold | HIGH | Decompiled priority function at `sub_8C9320` |
| Phase 41 runs before scheduling | VERY HIGH | Factory index and pipeline ordering table |
| Phases 108--109 run post-scheduling | VERY HIGH | Pipeline ordering table, position after `OriRemoveNopCode` |
| Knob 582 cold-region query in predication | HIGH | Decompiled predication pass at `sub_1381010` |
| Block layout consumer at phase 112 | HIGH | `sub_A92C50` identified via string xref to `PlaceBlocksInSourceOrder` |
| Cold-block flag in BB+28 | MEDIUM | Inferred from consumer patterns; exact bit position unconfirmed |
| `DisableOptimizeHotColdFlow` knob gates phase 109 | HIGH | Knob string at `0x21BE1F0`, ROT13 match, category J disable switches |
| Tepid scheduling ratios | HIGH | String evidence from decompiled `sub_7A46E0` |
| PGO influence on cold marking | MEDIUM | Inferred from pipeline ordering (PGO at 20, cold marking at 41) |

## Cross-References

- [Pass Inventory](index.md) -- phases 41, 108, 109, 112 in the complete 159-phase table
- [Basic Blocks & CFG](../ir/cfg.md) -- BasicBlock object layout, RPO computation, edge hash maps
- [Scheduling Algorithm](../scheduling/algorithm.md) -- 8-bit priority encoding, hot/cold bit 5
- [Scheduler Overview](../scheduling/overview.md) -- hot/cold classification in scheduling context
- [Predication](predication.md) -- knob 582 cold-region gate
- [Instruction Format](../ir/instructions.md) -- instruction +72 opcode, +80 operand count, +84 operand array
- [Optimization Pipeline](../pipeline/optimizer.md) -- dispatch loop and phase execution order
