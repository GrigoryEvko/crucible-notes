# Hot/Cold Partitioning

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
Phase 108: OptimizeHotColdInLoop       (post-scheduling, SASS-level)
    |
    |  Within each loop body, separates hot and cold paths. Moves cold
    |  blocks to the end of the loop region so that the hot path forms
    |  a contiguous instruction sequence.
    |
    v
Phase 109: OptimizeHotColdFlow         (post-scheduling, SASS-level)
    |
    |  At function scope, restructures control flow to place cold blocks
    |  after all hot blocks. Adjusts branch targets to maintain correctness.
    |
    v
Phase 112: PlaceBlocksInSourceOrder    (final block layout)
    |
    |  Determines the physical ordering of all basic blocks in the
    |  emitted binary, consuming the hot/cold annotations set above.
```

The key architectural decision is that phase 41 runs at the Ori IR level (before scheduling and register allocation), while phases 108--109 run post-scheduling on the nearly-final SASS representation. This two-stage design is necessary because:

- Cold-block annotations must be available early for predication decisions (phase 63) and scheduling priority (the 8-bit priority encoder).
- Block reordering can only happen after scheduling has assigned stall counts and dependency barriers, since moving blocks changes instruction fetch distances and potentially invalidates scoreboard computations.

## Phase 41: MarkAdditionalColdBlocks

Phase 41 is an analysis pass that annotates basic blocks with cold flags. The name "Additional" implies that some initial cold marking occurs earlier (likely during `AnalyzeControlFlow` at phase 3 or `PerformPGO` at phase 20), and this pass extends those annotations using additional heuristics available after mid-level optimization.

### Pipeline Context

Phase 41 runs after `DoVirtualCTAExpansion` (40) and before `ExpandMbarrier` (42). At this point in the pipeline:

- The CFG is fully built (phase 3) and loop structure is known (phase 18).
- PGO data has been applied (phase 20) if available.
- Branch optimization (phase 15) has simplified the control flow.
- The IR is still in Ori form -- no register allocation or scheduling has occurred.

### Cold-Block Heuristics

The cold-block classification uses both static and profile-guided signals. Based on analysis of consumers of the cold-block flag:

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

### Cold Flag Storage

The cold annotation is stored in the BasicBlock flags field at offset +28 of the 136-byte BasicBlock object. The predication pass queries this via knob 582 (block-level cold-region query), and the scheduling priority function reads it when computing the 8-bit packed priority at bit position 5.

### Consumers of Cold Annotations

| Consumer | Phase | Usage |
|----------|-------|-------|
| `OriDoPredication` | 63 | Knob 582: skips if-conversion of cold regions (divergence penalty acceptable in cold code) |
| Scheduling priority | 97--101 | Bit 5 of 8-bit priority: cold instructions get lower scheduling priority (value 1 = cold, 0 = hot) |
| `OptimizeHotColdInLoop` | 108 | Reads cold flags to identify which loop blocks to move |
| `OptimizeHotColdFlow` | 109 | Reads cold flags for whole-function layout |
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
Bit 5: hot/cold (0 = hot = higher priority, 1 = cold = lower priority)
Bit 4: register pressure overflow
Bit 3: same-BB preference
Bit 2: stall-free
Bit 1: critical path
Bit 0: tiebreaker
```

Hot memory instructions (global loads, global atomics) get **higher** scheduling priority because their long latencies benefit from being issued early -- the scheduler can then fill the latency window with independent instructions. Cold memory instructions (constant loads) have short latencies and do not benefit from early issue, so they receive lower priority.

## Phase 108: OptimizeHotColdInLoop

Phase 108 operates at the post-scheduling level, after register allocation and NOP removal have completed. It optimizes the layout of basic blocks within loop bodies.

### Pipeline Context

```
Phase 107: OriRemoveNopCode      (NOP removal)
Phase 108: OptimizeHotColdInLoop  (loop-internal reordering)
Phase 109: OptimizeHotColdFlow    (function-wide reordering)
Phase 110: PostSchedule           (post-scheduling fixup)
Phase 112: PlaceBlocksInSourceOrder (final layout)
```

At this point, instructions have been scheduled and stall counts assigned. The optimization must preserve scheduling correctness while improving spatial locality.

### Algorithm

The pass iterates over each loop in the function (loop structure computed at phase 18, maintained through the pipeline):

1. **Identify loop blocks.** Using the loop header RPO number and exit RPO number, enumerate all blocks in the loop body.

2. **Classify blocks.** Each block in the loop is classified as hot or cold based on the cold-block flags set by phase 41 (and potentially refined by phases between 41 and 108).

3. **Partition.** Hot blocks remain at the top of the loop body; cold blocks are moved to the bottom (higher addresses within the loop region).

4. **Adjust branches.** Branch targets are updated to reflect the new block positions. Cold blocks that were fall-through targets of hot blocks receive explicit branch instructions (since they are no longer adjacent).

The effect is that the hot loop body forms a contiguous instruction sequence that fits in fewer icache lines:

```
Before:                          After:
  loop_header (hot)                loop_header (hot)
  hot_block_1                      hot_block_1
  cold_error_check                 hot_block_2
  hot_block_2                      BRA loop_header
  BRA loop_header                  cold_error_check  (moved)
```

### Constraints

- The loop header block cannot be moved (it must be the entry point).
- Blocks with back-edges to the loop header must maintain their branch reachability.
- The transformation must not change the set of scoreboard/dependency barrier states visible at each instruction (since scheduling has already completed).

## Phase 109: OptimizeHotColdFlow

Phase 109 extends the hot/cold separation to the entire function, operating on blocks that are not inside loops (or that span multiple loops).

### Algorithm

The whole-function pass:

1. **Scan all blocks** in RPO order. Classify each non-loop block as hot or cold.

2. **Partition the function** into a hot region (placed first in the binary) and a cold region (placed last). Loop bodies are treated as atomic units -- the internal ordering was already optimized by phase 108.

3. **Insert or adjust branches** at hot-to-cold and cold-to-hot transitions. Cold-to-hot transitions require a branch back to the hot region.

4. **Update block ordering metadata** consumed by phase 112 (`PlaceBlocksInSourceOrder`).

The combined effect of phases 108 and 109 is a two-level layout:

```
Function layout after phases 108+109:
  [hot loop bodies, internally sorted by phase 108]
  [hot non-loop blocks]
  [cold blocks from all regions]
```

## Tepid Scheduling

Between the extremes of hot and cold, ptxas recognizes a "tepid" scheduling mode that balances math and memory instruction interleaving. The tepid infrastructure lives at `0x7A4350`--`0x7A5000` and computes ratios:

| Metric | Formula | Purpose |
|--------|---------|---------|
| `MathToDmaWaitRatio` | `field[756] / a5` | Ratio of math cycles to memory wait cycles |
| `MathToDmaTepidRatio` | `field[752] / a6` | Ratio of math cycles to memory tepid cycles |
| `MathToEpilogueWaitRatio` | `field[756] / (a5 / epilogue_count)` | Per-epilogue math-to-wait ratio |
| `MathToEpilogueTepidRatio` | `a6 / epilogue_count` | Per-epilogue tepid ratio |

These ratios are computed by `sub_7A4350` (TepidSchedulingCompute) and reported by `sub_7A46E0` (TepidSchedulingReport) when verbosity > 0. Epilogue blocks are identified by `sub_754510` (IsEpilogueBlock), with the epilogue instruction count controlled by knob 294.

The tepid mode affects how aggressively the scheduler interleaves memory and math instructions -- hot regions use aggressive overlap, cold regions use conservative scheduling, and tepid regions use an intermediate policy.

## Interaction with Other Passes

### Predication (Phase 63)

The predication pass queries knob 582 to determine whether a branch region lies in a cold block. If the region is cold, predication may be skipped because:

- The cold path is rarely executed, so the branch divergence penalty is amortized.
- Predication would execute both paths unconditionally, wasting functional units on cold-path instructions.
- Keeping the branch allows the cold path to be physically separated by phases 108--109.

### PlaceBlocksInSourceOrder (Phase 112)

Phase 112 is the final block layout pass. It consumes the hot/cold annotations and the reordering decisions made by phases 108--109 to determine the physical position of every basic block in the emitted binary. The function `sub_A92C50` implements this with a complex block-sorting algorithm that uses FNV-1a hash maps and an explicit work stack.

Key fields consumed from the Code Object:

| Offset | Field | Usage |
|--------|-------|-------|
| +232 | Current block pointer | Block being placed |
| +264 | Block type/mode | Controls placement strategy |
| +296 | BB array | Block pointers for lookup |
| +648 | Successor edge map | Determines fall-through targets |
| +720 | RPO array | Provides initial ordering |

### PerformPGO (Phase 20)

When profile data is available (from prior compilation runs with `--generate-line-info` and feedback), phase 20 applies execution counts and branch probabilities to the IR. These weights directly influence cold-block identification at phase 41 -- blocks with execution counts below a threshold relative to the function entry are marked cold regardless of static heuristics.

## Key Functions

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
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

All three vtables follow the standard 5-entry layout:

| Vtable Offset | Entry |
|---------------|-------|
| +0 | `execute(phase*, compilation_context*)` |
| +8 | `isNoOp(phase*) -> bool` |
| +16 | `getName(phase*) -> int` |
| +24 | `alloc(pool*, size)` |
| +32 | `free(pool*, ptr)` |

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|------------|---------|
| Phase names and indices (41, 108, 109) | VERY HIGH | Static name table at `off_22BD0C0`, factory switch at `sub_C60D30` |
| Vtable addresses | VERY HIGH | Computed from base `off_22BD5C8` + index * 40 |
| `isHotMemoryOp` / `isColdMemoryOp` identity | HIGH | Dual function structure, memory space checks, opcode patterns |
| Memory space codes (4=shared, 5=constant, 6=global) | HIGH | Confirmed across multiple consumers |
| Scheduling priority bit 5 = hot/cold | HIGH | Decompiled priority function at `sub_8C9320` |
| Phase 41 runs before scheduling | VERY HIGH | Factory index and pipeline ordering table |
| Phases 108--109 run post-scheduling | VERY HIGH | Pipeline ordering table, position after `OriRemoveNopCode` |
| Knob 582 cold-region query in predication | HIGH | Decompiled predication pass at `sub_1381010` |
| Block layout consumer at phase 112 | HIGH | `sub_A92C50` identified via string xref to `PlaceBlocksInSourceOrder` |
| Cold-block flag in BB+28 | MEDIUM | Inferred from consumer patterns; exact bit position unconfirmed |
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
