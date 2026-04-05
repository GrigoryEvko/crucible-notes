# Liveness Analysis & Dead Code Elimination

Liveness analysis is the most frequently repeated computation in the ptxas pipeline. Six dedicated phases perform liveness analysis combined with dead code elimination (DCE), and at least four additional subsystems recompute liveness on demand. The core algorithm is a standard backward dataflow analysis over the CFG, but the implementation is notable for its SSE2-accelerated bitvector library, per-register-file liveness tracking, and the `orWithAndNotIfChanged` fused transfer function that implements the entire dataflow step in a single SIMD pass.

| | |
|---|---|
| **Dedicated liveness phases** | 6 (phases 10, 16, 19, 33, 61, 84) |
| **Core bitvector library** | `0xBDBA60`--`0xBDE150` (15+ functions, SSE2) |
| **BitVector object size** | 20 bytes header + dynamic word array |
| **Word size** | 32-bit (`uint32_t`) -- indexed by `>> 5` and `& 0x1F` |
| **Transfer function** | `out = gen | (in - kill)` via `orWithAndNot` |
| **Fixed-point detection** | `orIfChanged` / `andIfChanged` return `bool` |
| **Liveness storage** | Code Object `+832` (main), `+856` (uniform) |
| **NamedPhases override** | `"OriPerformLiveDead"` controls all 4 instances |
| **Related phase** | 138 `OriSplitHighPressureLiveRanges` (late cleanup) |

## Pipeline Placement

The six liveness-related phases are distributed across the entire optimization pipeline. Each runs after a group of transformations that may have introduced dead code or invalidated previous liveness information:

```
Phase  10  EarlyOriSimpleLiveDead         ── Initial Setup
Phase  16  OriPerformLiveDeadFirst         ── Early Optimization
Phase  19  OriSplitLiveRanges             ── Early Optimization
Phase  33  OriPerformLiveDeadSecond        ── Mid-Level Optimization
Phase  61  OriPerformLiveDeadThird         ── Late Optimization
Phase  84  OriPerformLiveDeadFourth        ── Legalization

Phase 138  OriSplitHighPressureLiveRanges  ── Late Cleanup (related)
```

The four `OriPerformLiveDead` instances are identical passes invoked at different pipeline positions. They share the same vtable execute function and differ only in when they run. The `NamedPhases` system addresses all four through the single name `"OriPerformLiveDead"`.

### Why Four Instances?

Each instance cleans up dead code introduced by the preceding optimization group:

| Phase | Runs After | Cleans Up |
|-------|-----------|-----------|
| 16 (First) | Branch optimization, switch optimization | Dead branches, unreachable code from CFG simplification |
| 33 (Second) | GeneralOptimize, loop unrolling, pipelining, strength reduction | Dead loop induction variables, redundant computations from unrolling |
| 61 (Third) | GeneralOptimizeLate, loop fusion, VTG expansion, late expansion | Dead code from loop fusion, expanded macro instructions |
| 84 (Fourth) | Backward copy propagation, late arch optimization | Dead copies, redundant moves from backward propagation |

Without these intermediate liveness passes, dead code would accumulate through the pipeline, inflating register pressure and increasing compile time for downstream passes.

## Dataflow Algorithm

### Classical Backward Liveness

ptxas implements textbook backward dataflow analysis. For each basic block B, the analysis computes:

```
LiveIn(B)  = gen(B) | (LiveOut(B) - kill(B))
LiveOut(B) = Union over all successors S of LiveIn(S)
```

Where:
- **gen(B)**: registers used in B before any definition in B (upward-exposed uses)
- **kill(B)**: registers defined in B (regardless of whether they are also used)
- **LiveIn(B)**: registers live at the entry of B
- **LiveOut(B)**: registers live at the exit of B

### Iterative Fixed-Point Solver

The analysis iterates in reverse post-order (RPO) until no LiveIn/LiveOut set changes:

```
function compute_liveness(func):
    compute_RPO(func)                              // sub_BDE150
    
    // Initialize gen/kill sets per block
    for each block B in func:
        gen(B)  = {}
        kill(B) = {}
        for each instruction I in B (reverse order):
            for each source operand r of I:
                if r not in kill(B):
                    gen(B) |= {r}
            for each destination operand d of I:
                kill(B) |= {d}
    
    // Initialize LiveOut to empty for all blocks
    for each block B:
        LiveOut(B) = {}
    
    // Iterate until fixed point
    changed = true
    while changed:
        changed = false
        for each block B in reverse RPO:
            // LiveOut = union of successors' LiveIn
            for each successor S of B:
                changed |= LiveOut(B).orIfChanged(LiveIn(S))
            
            // LiveIn = gen | (LiveOut - kill)
            //        implemented as: orWithAndNotIfChanged
            changed |= LiveIn(B).orWithAndNotIfChanged(
                            gen(B), LiveOut(B), kill(B))
```

The key optimization is the fused `orWithAndNotIfChanged` operation (`sub_BDD560`), which computes `dst |= gen | (in & ~kill)` and returns whether any bit changed -- all in a single SSE2 pass over the bitvector words. This avoids materializing intermediate bitvectors for `(LiveOut - kill)`.

### Convergence

The analysis converges because:
1. All sets are initialized to empty (bottom of the lattice).
2. Each iteration can only add bits (the transfer function is monotone).
3. The lattice has finite height (bounded by the total number of virtual registers).
4. RPO traversal order minimizes the number of iterations -- typically 2--3 passes for acyclic code, proportional to loop nesting depth for loops.

## BitVector Implementation

The bitvector library at `0xBDBA60`--`0xBDE150` is the most performance-critical infrastructure in ptxas dataflow analysis. All operations are SSE2-accelerated with manual alignment handling.

### Layout

```c
struct BitVector {       // 20 bytes total
    uint32_t* data;      // +0:  pointer to word array (heap-allocated)
    int32_t   word_count; // +8:  number of 32-bit words in use
    int32_t   capacity;  // +12: allocated words (>= word_count)
    int32_t   bit_count; // +16: number of valid bits
};
```

Word count is computed from bit count: `word_count = (bit_count + 31) >> 5`. Memory is allocated via the pool allocator (vtable dispatch at allocator `+24` for alloc, `+32` for free). Reallocation occurs only when the new word count exceeds the current capacity.

### Core Operations

| Address | Operation | Signature | Notes |
|---------|-----------|-----------|-------|
| `sub_BDBA60` | `allocate` | `(bv*, alloc*, num_bits)` | Grow-only; no shrink |
| `sub_BDBFB0` | `setBit` | `(bv*, bit_index)` | `data[i>>5] |= (1 << (i&31))` |
| `sub_BDC0E0` | `clearBit` | `(bv*, bit_index)` | `data[i>>5] &= ~(1 << (i&31))` |
| `sub_BDC200` | `testBit` | `(bv*, bit_index) -> bool` | `(data[i>>5] >> (i&31)) & 1` |
| `sub_BDCDE0` | `operator\|=` | `(dst*, src*)` | SSE2 `_mm_or_si128` loop |
| `sub_BDCF40` | `orIfChanged` | `(dst*, src*) -> bool` | Scans `(~dst & src) != 0` first |
| `sub_BDC5F0` | `operator&=` | `(dst*, src*)` | SSE2 `_mm_and_si128`; zeroes tail |
| `sub_BDC790` | `andIfChanged` | `(dst*, src*) -> bool` | Scans `(~src & dst) != 0` first |
| `sub_BDDAA0` | `operator^=` | `(dst*, src*)` | SSE2 `_mm_xor_si128` |
| `sub_BDC3F0` | `assignAND` | `(dst*, a*, b*)` | `dst = a & b` |
| `sub_BDD300` | `orWithAndNot` | `(dst*, gen*, in*, kill*)` | `dst \|= gen \| (in & ~kill)` |
| `sub_BDD560` | `orWithAndNotIfChanged` | `(dst*, gen*, in*, kill*) -> bool` | Core transfer function |
| `sub_BDBD60` | `extractBits` | `(out[], start, end)` | Cross-word boundary handling |
| `sub_BDD8C0` | `popcount` | `(bv*) -> int` | Count set bits |
| `sub_BDDC00` | `clear` | `(bv*)` | `memset(data, 0, ...)` |
| `sub_BDCA60` | `operator=` | `(dst*, src*)` | Copy with possible realloc |
| `sub_BDCC20` | `isSubsetOf` | `(a*, b*) -> bool` | Tests `(a & ~b) == 0` |

### SSE2 Loop Structure

All bulk operations follow the same pattern:

```c
// Alignment prologue: process scalar words until 16-byte aligned
int align_count = (-(uintptr_t)(dst_ptr) >> 2) & 3;
for (int i = 0; i < min(align_count, word_count); i++)
    dst_ptr[i] |= src_ptr[i];

// SSE2 main loop: process 4 words (128 bits) per iteration
int sse_count = (word_count - align_count) >> 2;
for (int i = 0; i < sse_count; i++) {
    __m128i d = _mm_load_si128(&dst_ptr[aligned_offset + 4*i]);
    __m128i s = _mm_loadu_si128(&src_ptr[aligned_offset + 4*i]);
    _mm_store_si128(&dst_ptr[aligned_offset + 4*i], _mm_or_si128(d, s));
}

// Scalar epilogue: remaining 0-3 words
for (remaining words)
    dst_ptr[j] |= src_ptr[j];
```

The `orWithAndNot` transfer function fuses three operations into one SSE2 expression:

```c
__m128i result = _mm_or_si128(
    _mm_or_si128(gen_vec, dst_vec),
    _mm_andnot_si128(kill_vec, in_vec)   // in & ~kill
);
```

The `IfChanged` variants first scan for any bit that would change (`~dst & new_bits`), then apply the operation only from the first differing word forward. This early-exit optimization avoids unnecessary writes when the analysis has already converged for most blocks.

## Per-Register-File Liveness

GPU register allocation manages multiple independent register files. ptxas tracks liveness separately for each:

| Register File | Bit Range | Storage |
|--------------|-----------|---------|
| **R** (GPR, 32-bit) | Bits 0..254 | Code Object `+832` (main bitvector) |
| **UR** (uniform GPR) | Bits 0..63 | Code Object `+856` (uniform bitvector) |
| **P** (predicate, 1-bit) | Separate tracking | Operand type `(v >> 28) & 7 == 5` |
| **UP** (uniform predicate) | Separate tracking | Flag at Code Object `+1378` bit 4 |
| **B** (barrier) | Indices 20, 21 | Special-cased in dependency graph |

The main liveness bitvector at Code Object `+832` covers R registers. The uniform register bitvector at `+856` is conditionally allocated: it exists only when the flag at Code Object `+1378` bit 4 is set (indicating the function uses uniform registers). The scheduling pass (`sub_A06A60`) allocates both bitvectors via `sub_BDBAD0` and processes them in parallel.

Predicate registers are handled at the operand level during scheduling: the operand type check `((operand >> 28) & 7) == 5` identifies predicate operands, which are tracked in a separate per-block set rather than the main bitvector.

Barrier registers (IDs 20, 21 for sm >= 4.0) receive special treatment in the dependency graph builder (`sub_A0D800`): they generate ordering dependencies rather than data dependencies, since barriers enforce execution ordering constraints independent of register values.

## Phase 10: EarlyOriSimpleLiveDead

The earliest liveness pass, running immediately after initial IR construction (after `ReportInitialRepresentation` at phase 9). This is a simplified liveness + DCE pass that removes obviously dead instructions from the freshly-lowered IR.

**Pipeline context:** At this point, the IR has just been lowered from PTX. Many PTX instructions expand to multiple Ori instructions, some of which produce values that are immediately dead (e.g., condition codes that are never tested, intermediate values from multi-instruction expansions). EarlyOriSimpleLiveDead removes this low-hanging dead code before the main optimization pipeline begins, reducing the working set for all subsequent passes.

**Implementation evidence:** The sweep at `p1.10` (`W010`) confirms this pass uses the bitvector infrastructure at `sub_BDBA60`--`sub_BDE150` for liveness computation. The "simple" in the name may indicate a local-only (per-BB) analysis that avoids the cost of full global iterative dataflow -- sufficient for removing obviously dead definitions that have no uses within the same block.

## Phases 16, 33, 61, 84: OriPerformLiveDead

The four instances of the full liveness + DCE pass. These perform global iterative dataflow analysis followed by dead instruction removal.

### Algorithm

```
function OriPerformLiveDead(func):
    // 1. Rebuild basic block metadata
    rebuild_basic_blocks(func, mode)        // sub_781F80
    
    // 2. Compute global liveness (iterative fixed-point)
    compute_global_liveness(func)           // iterative solver
    
    // 3. Dead code elimination
    for each block B in func:
        for each instruction I in B:
            if all destinations of I are dead (not in LiveOut):
                if I has no side effects:
                    remove(I)
    
    // 4. Update IR metadata
    //    (instruction counts, block sizes, etc.)
```

### Side-Effect Preservation

Not all instructions with dead destinations can be removed. The DCE must preserve:
- **Memory stores** (`STG`, `STS`, `STL`, `ATOM`, etc.) -- observable side effects
- **Barrier instructions** (`BAR`, `MEMBAR`) -- synchronization semantics
- **Control flow** (`BRA`, `EXIT`, `RET`, `CALL`) -- program structure
- **Texture operations** with side effects
- **Instructions with volatile flags**

The opcode mask `& 0xCFFF` (seen in `sub_A06A60`) strips modifier bits to obtain the base opcode for side-effect classification. Opcodes 93 (`OUT_FINAL` in the ROT13 name table; used as a call-like marker -- actual CALL is opcode 71), 94 (`LDS`)/95 (`STS`) (used as block boundary markers), 97 (`STG`; used as a branch-like marker -- actual BRA is opcode 67), and 52 (`AL2P_INDEXED`; used as NOP/boundary) receive special handling.

### DCE Integration

The OriPerformLiveDead pass combines liveness computation with DCE in a single pass rather than running them as separate analyses. After computing LiveOut sets for each block, the pass walks each block backward: for each instruction, it checks whether every destination register is absent from the current live set. If so and the instruction has no side effects, it is unlinked from the instruction list. Source operands of removed instructions are themselves removed from the live set, potentially enabling cascading removal of further dead instructions within the same backward walk.

## Phase 19: OriSplitLiveRanges

This phase splits live ranges at loop boundaries to reduce register pressure. It runs after `OriPerformLiveDeadFirst` (phase 16) and `OriLoopSimplification` (phase 18), when the loop structure is canonical.

**String reference:** `"OriSplitLiveRanges"` at `0x22BC5C0`.

### Motivation

On GPUs, register pressure directly determines occupancy (the number of concurrent warps). A value defined before a loop and used only after the loop occupies a register for the entire loop body, even though it is not accessed within the loop. Splitting the live range at the loop boundary -- by inserting a spill before the loop and a reload after -- can free the register for use inside the loop, reducing peak pressure and enabling higher occupancy.

### Algorithm (Reconstructed)

```
function OriSplitLiveRanges(func):
    compute_liveness(func)
    
    for each loop L in func (from loop array at func+512):
        live_through = LiveIn(L.header) & LiveOut(L.exit) - gen(L.body)
        
        for each register r in live_through:
            if benefit(r, L) > threshold:
                // Insert copy before loop header
                insert_move(L.preheader, r, r_split)
                
                // Insert copy after loop exit
                insert_move(L.exit_target, r_split, r)
                
                // Rename uses after loop to r_split
                rename_uses_after_loop(r, r_split, L)
```

The benefit heuristic considers:
- Loop trip count (from PGO data if available, phase 20 runs after this)
- Number of registers live through the loop
- Distance from target occupancy

### Relationship to Phase 138

Phase 138 (`OriSplitHighPressureLiveRanges`) performs a similar transformation but much later in the pipeline (late cleanup stage), targeting live ranges that still cause excessive pressure after all optimization and legalization passes have run. Phase 19 is the early, conservative version; phase 138 is the late, aggressive fallback.

## Liveness Consumers

The liveness information computed by these phases is consumed throughout the pipeline:

### Register Allocator

The fat-point register allocator (`sub_9721C0`) is the primary consumer. Its entry point explicitly rebuilds liveness before allocation:

```c
sub_781F80(ctx, 1);     // rebuild basic blocks
sub_A10160(ctx, 1);     // recompute liveness
```

The allocator uses liveness information for:
- **Interference computation**: Two virtual registers interfere if their live ranges overlap. The interference graph builder (`sub_926A30`, 155KB decompiled) uses bitvector intersection to detect overlaps.
- **Spill cost estimation**: `sub_94E620` computes spill costs weighted by liveness range length and instruction properties.
- **Spill placement**: `sub_9449B0` (liveness range calculator, 1800 bytes) iterates instructions in reverse block order using bitvector operations to determine optimal spill/reload insertion points.

### Instruction Scheduler

The scheduling subsystem maintains its own liveness tracking at Code Object `+832`:

- **Pre-scheduling**: `sub_8DBAF0` (16KB, `LivenessAnalysis`) computes register liveness for the scheduling priority function.
- **Per-BB liveness**: `sub_8DB5F0` (8.4KB, `LivenessCompute`) computes per-basic-block liveness sets.
- **Initialization**: `sub_8DB070` (8.2KB, `LivenessInit`) sets up the liveness data structures.
- **Iterative solver**: `sub_8DE7A0` (12KB) runs the iterative fixed-point computation for scheduling-specific dataflow.

The scheduler uses liveness to:
- Estimate register pressure at each scheduling point
- Identify last-use operands for dead-register marking (`sub_A08250` checks `(1 << reg_num) & *(live_set + 4*(reg_num >> 5))`)
- Compute instruction priority based on register pressure impact

### DAG Construction

The dependency graph builder (`sub_A0F970`, `sub_A0D800`) uses liveness to:
- Determine which registers are live at block boundaries
- Identify anti-dependencies (WAR) that constrain scheduling
- Track callee-clobbered registers at call sites (opcode 93; `OUT_FINAL` in ROT13, used as call-like marker -- actual CALL is opcode 71)

### Multi-Set Register Manager

`sub_A7BC80` (36KB) manages multiple parallel liveness bitvectors for different register classes (R, P, B, UR, UP) during post-allocation scheduling. It allocates and deallocates bitvectors in coordinated groups, updating each set based on instruction defs/uses.

### Uninitialized Register Detector

`sub_A0B5E0` uses liveness information to detect potentially uninitialized registers. After scheduling, it walks each block's entry live set: for each live register, it checks the `0x20` flag at register descriptor offset 48. If the flag is clear, the register is reported as potentially uninitialized via warning strings `"Found %d potentially uninitialized register(s) in function %s"` (warning `0x1E14`).

## Data Flow Infrastructure for Scheduling

The scheduling subsystem has its own dataflow infrastructure (separate from the optimizer's OriPerformLiveDead):

| Address | Size | Identity |
|---------|------|----------|
| `sub_8DB070` | 8.2KB | `LivenessInit` -- allocate and initialize per-BB liveness structures |
| `sub_8DB5F0` | 8.4KB | `LivenessCompute` -- compute liveness per basic block |
| `sub_8DBAF0` | 16KB | `LivenessAnalysis` -- full liveness analysis (red-black tree interval structure) |
| `sub_8DC3F0` | 3.0KB | `ComputeDataFlowState` -- scheduling-specific dataflow |
| `sub_8DC620` | 3.3KB | `UpdateDataFlowOnSchedule` -- update flow after scheduling decisions |
| `sub_8DC880` | 10KB | `PropagateDataFlow` -- propagate dataflow information |
| `sub_8DCF20` | 23KB | `BuildDFGForScheduling` -- build scheduling data flow graph |
| `sub_8DE7A0` | 12KB | `IterativeDataFlow` -- iterative fixed-point solver |
| `sub_8DEF90` | 2.0KB | `FinalizeDataFlow` -- finalize dataflow results |

The `sub_8DBAF0` function implements a red-black tree (evidenced by tree rotations and color flags at node offset `+40` in the decompiled code), used to store liveness intervals as an ordered set. This enables efficient range queries: "which registers are live at program point P?" is answered by a tree search in O(log n) rather than a linear scan.

## sub\_781F80: Basic Block Rebuild

This function appears ubiquitously as a prerequisite to liveness computation. It is called with a mode parameter:

- `sub_781F80(func, 0)`: Reset/rebuild basic block metadata for reverse scheduling mode
- `sub_781F80(func, 1)`: Full rebuild for forward analysis (used before register allocation)

Over 50 call sites reference this function across the optimizer, register allocator, and scheduler. It refreshes the basic block linked lists, instruction counts, and block boundary markers that the liveness analysis depends on.

## Key Function Table

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
| `sub_BDBA60` | ~120B | `BitVector::allocate` | HIGH (0.90) |
| `sub_BDBFB0` | ~120B | `BitVector::setBit` | HIGH (0.90) |
| `sub_BDC0E0` | ~120B | `BitVector::clearBit` | HIGH (0.90) |
| `sub_BDC200` | ~140B | `BitVector::testBit` | HIGH (0.90) |
| `sub_BDCDE0` | ~400B | `BitVector::operator\|=` (OR) | HIGH (0.95) |
| `sub_BDCF40` | ~564B | `BitVector::orIfChanged` | HIGH (0.95) |
| `sub_BDC5F0` | ~484B | `BitVector::operator&=` (AND) | HIGH (0.95) |
| `sub_BDC790` | ~800B | `BitVector::andIfChanged` | HIGH (0.95) |
| `sub_BDDAA0` | ~400B | `BitVector::operator^=` (XOR) | HIGH (0.95) |
| `sub_BDC3F0` | ~520B | `BitVector::assignAND` | HIGH (0.90) |
| `sub_BDD300` | ~488B | `BitVector::orWithAndNot` | HIGH (0.92) |
| `sub_BDD560` | ~648B | `BitVector::orWithAndNotIfChanged` | HIGH (0.92) |
| `sub_BDBD60` | ~368B | `BitVector::extractBits` | HIGH (0.88) |
| `sub_BDD8C0` | ~320B | `BitVector::popcount` | MEDIUM (0.80) |
| `sub_BDDC00` | ~140B | `BitVector::clear` | HIGH (0.90) |
| `sub_BDCA60` | ~280B | `BitVector::operator=` (copy) | MEDIUM (0.85) |
| `sub_BDCC20` | ~320B | `BitVector::isSubsetOf` | MEDIUM (0.85) |
| `sub_BDE150` | 9KB | `CFG::computeRPO` | HIGH (0.90) |
| `sub_781F80` | varies | Basic block rebuild | HIGH (0.85) |
| `sub_A10160` | ~2KB | Liveness computation entry | MEDIUM (0.75) |
| `sub_A0BA40` | 15KB | Block-level liveness iteration | HIGH (0.85) |
| `sub_A06A60` | 15KB | Per-block register set tracking | HIGH (0.95) |
| `sub_A0D800` | 39KB | Dependency graph construction | HIGH (0.95) |
| `sub_A0F970` | 10KB | DAG construction entry | HIGH (0.95) |
| `sub_92C240` | 8KB | Liveness bitvector operations (regalloc) | HIGH (87 callers) |
| `sub_9449B0` | 1.8KB | Liveness range calculator (spill codegen) | HIGH |
| `sub_8DBAF0` | 16KB | LivenessAnalysis (scheduling) | HIGH (0.85) |
| `sub_8DB5F0` | 8.4KB | LivenessCompute (per-BB scheduling) | HIGH (0.85) |
| `sub_8DB070` | 8.2KB | LivenessInit (scheduling) | HIGH (0.85) |
| `sub_8DE7A0` | 12KB | IterativeDataFlow (scheduling solver) | HIGH (0.80) |
| `sub_A0B5E0` | varies | Uninitialized register detector | HIGH (0.97) |
| `sub_A7BC80` | 36KB | RegisterSetManager (multi-file liveness) | MEDIUM (0.65) |

## Key Constants

| Value | Meaning |
|-------|---------|
| `+832` | Code Object offset: main register liveness bitvector (R registers) |
| `+856` | Code Object offset: uniform register liveness bitvector (UR registers) |
| `+840` | Code Object offset: max live register count |
| `+848` | Code Object offset: liveness info pointer |
| `+720` | Code Object offset: RPO order array |
| `+984` | Code Object offset: number of basic blocks |
| `+1378` bit 4 | Flag: function uses uniform registers (enables `+856` bitvector) |
| `0xCFFF` | Opcode mask: strips modifier bits for side-effect classification |
| `0x22BC5C0` | String address: `"OriSplitLiveRanges"` |
| `0x22BCFE8` | String address: `"OriSplitHighPressureLiveRanges"` |

## Cross-References

- [Pass Inventory](index.md) -- full 159-phase listing with liveness phase positions
- [Optimizer Pipeline](../pipeline/optimizer.md) -- pipeline stage groupings
- [Ori IR](../ir/overview.md) -- Code Object layout, bitvector infrastructure details
- [Allocator Architecture](../regalloc/overview.md) -- liveness as input to fat-point allocator
- [Spill Mechanism](../regalloc/spilling.md) -- liveness range calculator for spill placement
- [Scheduler Architecture](../scheduling/overview.md) -- scheduling-specific liveness
- [Scheduling Algorithm](../scheduling/algorithm.md) -- priority function's pressure estimates
