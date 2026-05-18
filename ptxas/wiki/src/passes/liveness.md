# Liveness Analysis & Dead Code Elimination

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Liveness analysis is the most frequently repeated computation in the ptxas pipeline. Six dedicated phases perform liveness analysis combined with dead code elimination (DCE), and at least four additional subsystems recompute liveness on demand. The core algorithm is a standard backward dataflow analysis over the CFG, but the implementation is notable for its SSE2-accelerated bitvector library, per-register-file liveness tracking, and the `orWithAndNotIfChanged` fused transfer function that implements the entire dataflow step in a single SIMD pass.

| | |
|---|---|
| **Dedicated liveness phases** | 6 (phases 10, 16, 19, 33, 61, 84) |
| **Core bitvector library** | `0xBDBA60`--`0xBDDD40` (15+ functions; bulk Boolean ops are SSE2) |
| **BitVector object size** | 20 bytes header + dynamic word array |
| **Word size** | 32-bit (`uint32_t`) -- indexed by `>> 5` and `& 0x1F` |
| **Transfer function** | `out = gen \| (in - kill)` via `orWithAndNot` |
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

### Formal Set Equations

Let V be the set of all virtual register IDs tracked by the analysis (|V| <= 255 for R registers, <= 64 for UR registers). For each basic block B in CFG = (Blocks, Edges), define:

- **gen(B)** = { r in V | r is used in B before any definition of r in B } (upward-exposed uses)
- **kill(B)** = { r in V | r is defined in B } (regardless of whether also used)

The gen/kill sets are pre-computed once by scanning each block's instructions in reverse order (`sub_774370`). For each instruction I visited bottom-to-top: every source operand r where r not in kill(B) is added to gen(B); every destination operand d is added to kill(B). Both gen and kill are constant for the duration of the fixed-point iteration.

The dataflow equations over the lattice L = (2^V, subset-eq, union, empty-set):

```
LiveOut(B)  =  Union { LiveIn(S) | S in succ(B) }           -- meet (union)
LiveIn(B)   =  gen(B)  union  ( LiveOut(B) \ kill(B) )      -- transfer
```

The transfer function for block B is F_B(X) = gen(B) union (X \ kill(B)), which ptxas implements as a single fused operation `orWithAndNotIfChanged` (`sub_BDD560`):

```
dst |= gen | (in & ~kill)     -- SSE2: _mm_or_si128(_mm_or_si128(gen, dst), _mm_andnot_si128(kill, in))
```

This computes F_B(LiveOut(B)) and unions it into LiveIn(B) without materializing the intermediate set (LiveOut \ kill), returning a boolean `changed` flag by scanning `(~dst_old & new_bits) != 0` before writing.

### Iterative Fixed-Point Solver

The solver (`sub_774370`, called via `sub_775010` -> guard at `ctx+1370` bit 6) operates in two stages:

**Stage 1 -- Initialization.** For every block B in RPO order (array at `ctx+512`, computed by `sub_BDE150`):

```
LiveIn^0(B)  = empty-set
LiveOut^0(B) = empty-set          -- bottom of lattice L
```

Each bitvector is allocated via `sub_BDBAD0` with `(ctx+520)+1` bits, then zeroed. Exit blocks have their LiveOut initialized to the set of callee-saved / return-value registers via `sub_BDBB80` (setBit per register at `block+144`).

**Stage 2 -- Iteration.** Repeat until no set changes (boolean `changed` stays false for an entire pass):

```
for each block B in reverse RPO order (ctx+512, index ctx+520 downto 0):
    // Meet: LiveOut(B) = Union { LiveIn(S) | S in succ(B) }
    for each successor S in successor list at block+128:
        changed |= LiveOut(B).orIfChanged( LiveIn(S) )          // sub_BDCF40

    // Transfer: LiveIn(B) = gen(B) union (LiveOut(B) \ kill(B))
    changed |= LiveIn(B).orWithAndNotIfChanged(                  // sub_BDD560
                    gen(B), LiveOut(B), kill(B))
```

The `orIfChanged` at `sub_BDCF40` scans `(~dst & src)` word-by-word; if no bit differs it returns false without writing memory. The `orWithAndNotIfChanged` at `sub_BDD560` performs the same early-exit scan then applies the fused `dst |= gen | (in & ~kill)` in a single SSE2 pass starting from the first differing word.

For the dominator-respecting variant (called with flag `a2=1` via `sub_773140`), the iteration additionally intersects LiveOut with the dominator frontier set using `andIfChanged` (`sub_BDC790`) in a secondary inner loop. This prunes liveness across irreducible edges.

### Convergence Argument

The analysis is a forward Kleene iteration on a complete lattice and is guaranteed to converge:

1. **Lattice.** L = (2^V, subset-eq) is a complete lattice with bottom = empty-set and top = V. Height h(L) = |V| (at most 255 for R-class registers, 64 for UR-class).

2. **Monotonicity.** F_B is monotone: if X subset-eq Y then gen(B) union (X \ kill(B)) subset-eq gen(B) union (Y \ kill(B)). The meet operator (union) is likewise monotone. The `orIfChanged` / `orWithAndNotIfChanged` implementations enforce monotonicity structurally -- they can only set bits, never clear them (`dst |= ...`).

3. **Initialization at bottom.** LiveIn^0(B) = LiveOut^0(B) = empty-set for all B. Since empty-set subset-eq F_B(empty-set) for any B, the iterates form an ascending chain.

4. **Termination.** Each iteration either adds at least one bit to some set (strictly ascending) or detects no change and halts. Since h(L) = |V| and there are |Blocks| sets, the maximum number of iterations is bounded by |V| * |Blocks|. In practice, RPO traversal ensures convergence in d+2 iterations where d is the loop nesting depth -- typically 2--3 passes for acyclic code, confirmed by the `changed` boolean collapsing to false within 2 full RPO sweeps on straight-line CFGs.

## BitVector Implementation

The bitvector library at `0xBDBA60`--`0xBDDD40` is the most performance-critical infrastructure in ptxas dataflow analysis. The bulk Boolean operations (`|=`, `&=`, `^=`, the `IfChanged` and `orWithAndNot` variants) are SSE2-accelerated with manual alignment handling; small-scalar ops (setBit/clearBit/testBit) use plain integer arithmetic. The next address range (`0xBDDDF0`--`0xBDFB10`) contains CFG hash maps and RPO code, not bitvector primitives.

### Layout

```c
struct BitVector {       // 20 bytes total
    uint32_t* data;      // +0:  pointer to word array (heap-allocated)
    int32_t   word_count; // +8:  number of 32-bit words in use
    int32_t   capacity;  // +12: allocated words (>= word_count)
    int32_t   bit_count; // +16: number of valid bits
};
```

Word count is computed from bit count: `word_count = (bit_count + 31) >> 5`. Memory is allocated via `pool_alloc` (`sub_424070`) and freed via `pool_free` (`sub_4248B0`). Reallocation occurs only when the new word count exceeds the current capacity.

### Core Operations

| Address | Operation | Signature | Notes |
|---------|-----------|-----------|-------|
| `sub_BDBA60` | `allocate` | `(bv*, alloc*, num_bits)` | Grow-only; no shrink |
| `sub_BDBFB0` | `setBit` | `(bv*, bit_index)` | `data[i>>5] \|= (1 << (i&31))` |
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

GPU register allocation manages multiple register files. ptxas tracks liveness separately for each file, using independent bitvectors and solvers:

| Register File | Bit Range | Storage | Solver |
|--------------|-----------|---------|--------|
| **R** (GPR, 32-bit) | Bits 0..254 | Code Object `+832` | Main iterative dataflow |
| **UR** (uniform GPR) | Bits 0..63 | Code Object `+856` | Parallel solve, conditionally allocated |
| **P** (predicate, 1-bit) | Per-block set | Operand-level: `(v >> 28) & 7 == 5` | Per-block set operations during scheduling |
| **UP** (uniform predicate) | Per-block set | Tracked when UR enabled | Same path as P |
| **B** (barrier, `reg_type=9`) | -- | **Excluded from GEN/KILL** | Ordering deps only (`sub_A0D800`) |

### Independent Solve Per File

The R and UR bitvectors are solved independently: each has its own GEN, KILL, and live-in/live-out sets, and the iterative fixed-point converges separately for each. The scheduling entry point (`sub_A0F970`) allocates the R bitvector unconditionally at `func+832` via `sub_BDBAD0`, then conditionally allocates the UR bitvector at `func+856` only when the flag at Code Object `+1368` bit 4 is set (indicating the function uses uniform registers). The per-block scheduler (`sub_A06A60`) processes both bitvectors in the same instruction walk -- for each instruction it updates R liveness at `+832` and, when the `v76` flag (`+1368` bit 4) is set, also updates UR liveness at `+856`. Both updates use the same `orWithAndNotIfChanged` transfer function but operate on separate bitvector objects. When the first scheduling pass fails, `sub_A0F970` supports a "retry without uniform regs" fallback (v63 toggle) that disables UR tracking for the retry attempt.

### P/UP: Operand-Level Tracking

Predicate registers (P, UP) are not tracked in the main bitvectors. Instead, the scheduling heuristic callback (`sub_A08250`) identifies predicate operands via the operand type discriminator `((operand >> 28) & 7) == 5` and maintains a per-block predicate set separate from the R/UR bitvectors. This lighter-weight tracking suffices because predicate register files are small (7 usable registers per file) and do not benefit from the SSE2-accelerated bitvector machinery designed for the 255-element R file.

### Cross-File Dependency: P2R / R2P

The P2R (predicate-to-register) and R2P (register-to-predicate) instructions create a cross-file data dependency: P2R packs up to 8 predicate bits into a single GPR, and R2P unpacks them back. This coupling matters for two reasons. First, during predicate spilling the allocator uses P2R/R2P pairs to spill predicate registers through GPR stack slots, creating chains where P liveness depends on R liveness of the base GPR. Second, the regalloc verifier (`sub_A55D80`) explicitly validates that every R2P has a matching P2R (case 3: `P2R_R2P_PATTERN_FAILURE`) and that no instruction overwrites the base GPR between the pair (case 8: `P2R_R2P_BASE_DESTROYED`). Despite this coupling, the liveness solvers remain structurally independent -- the cross-file constraint is enforced at the allocator and verifier level rather than by unifying the dataflow lattices.

### Barrier Register Exclusion from GEN/KILL

Barrier registers (`reg_type = 9`, covering B0--B15 and UB0--UB15) are excluded from the standard liveness GEN/KILL computation. The dependency graph builder (`sub_A0D800`, 39 KB) special-cases barrier register operands: rather than adding them to the data-dependency GEN/KILL sets, it creates ordering-only edges in the dependency DAG. This is correct because barrier instructions (BAR, BSSY, BSYNC, DEPBAR) enforce execution ordering constraints between warps or thread groups -- they do not carry data values that participate in the liveness lattice. The barrier register mask at Code Object `+1088` (8 DWORDs) tracks barrier resource availability separately from the per-register-file liveness bitvectors.

## Phase 10: EarlyOriSimpleLiveDead

The earliest liveness pass, running immediately after initial IR construction (after `ReportInitialRepresentation` at phase 9). This is a simplified liveness + DCE pass that removes obviously dead instructions from the freshly-lowered IR.

**Pipeline context:** At this point, the IR has just been lowered from PTX. Many PTX instructions expand to multiple Ori instructions, some of which produce values that are immediately dead (e.g., condition codes that are never tested, intermediate values from multi-instruction expansions). EarlyOriSimpleLiveDead removes this low-hanging dead code before the main optimization pipeline begins, reducing the working set for all subsequent passes.

**Implementation evidence:** The sweep at `p1.10` (`W010`) confirms this pass uses the bitvector infrastructure at `sub_BDBA60`--`sub_BDDD40` (the RPO helper `sub_BDE150` sits past the bitvector range) for liveness computation. The "simple" in the name may indicate a local-only (per-BB) analysis that avoids the cost of full global iterative dataflow -- sufficient for removing obviously dead definitions that have no uses within the same block.

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

### Backward Walk Live Set Update Rule (sub\_A06A60)

The per-block backward walk in `sub_A06A60` maintains a running live set initialized from LiveOut. For each instruction traversed in reverse order, operands are classified by the 3-bit type field `(operand >> 28) & 7`: type 5 identifies predicate registers (tracked separately), type 6 marks the operand-list sentinel (end of operands). The walk applies two updates per instruction:

```
// Per-instruction live set update (backward direction):
for each destination operand d of I:
    reg_id = lookup(ctx+296, d & 0xFFFFFF)   // register table
    live_set |= kill_set(reg_id)              // sub_BDCDE0: OR into block kill
    // (destination is defined here, so it exits liveness)

for each source operand s of I:
    reg_id = lookup(ctx+296, s & 0xFFFFFF)
    setBit(live_set, reg_id)                  // sub_BDBB80: mark as live
    // (source is used here, so it enters liveness)
```

The opcode-specific dispatch handles five special cases that modify this basic rule:

| Masked opcode | Meaning | Live set action |
|---------------|---------|-----------------|
| 93 | Call-like | Walk callee's copy chain (opcode 269) adding source regs; add callee-clobbered regs; OR into block kill set |
| 94 | Block boundary (LDS) | Clear live set (`sub_BDC080`); rebuild from phi-successor list at `ctx+616`; OR reconstructed set into block kill |
| 95 | Block boundary (STS) | OR destination reg's set into current live set via `sub_BDCDE0`; copy live set into block kill via `sub_BDC1B0` |
| 97 | Branch-like | OR destination into block kill via `sub_BDC1B0`; when `ctx+1368` bit 4 is set and first in block, test for change via `sub_BDCF40` |
| 188/190 | Multi-source/predicated | Dispatch to `sub_A06950` to walk the source operand chain; add all reachable source registers to live set |

The change-detection flag returned by `sub_BDCF40` (orIfChanged) drives the iterative fixed-point: if any block's live set changed, the solver re-queues predecessor blocks for another iteration.

## Phase 19: OriSplitLiveRanges

This phase splits live ranges at loop boundaries and across phi/copy chains to reduce register pressure. It runs after `OriPerformLiveDeadFirst` (phase 16) and `OriLoopSimplification` (phase 18), when the loop structure is canonical.

**String reference:** `"OriSplitLiveRanges"` at `0x22BC5C0`.

**Core implementation:** `sub_BEF110` (108KB, 3,414 decompiled lines). Called via `sub_A1D3A0` (vtable execute) -> `sub_BF33D0` (knob-gated entry, reads register budget from `ctx+1624` and knob 456).

### Motivation

On GPUs, register pressure directly determines occupancy (the number of concurrent warps). A value defined before a loop and used only after the loop occupies a register for the entire loop body, even though it is not accessed within the loop. Splitting the live range at the loop boundary -- by inserting a copy before the loop and a copy after -- can free the register for use inside the loop, reducing peak pressure and enabling higher occupancy.

### Algorithm (Decompiled from sub\_BEF110)

The function operates in five distinct phases:

**Phase 1: Pre-analysis** -- Rebuilds basic blocks (`sub_781F80`), allocates three bitvector fields per virtual register (kill at `VR+96`, gen at `VR+24`, live-through at `VR+176`), then runs the standard iterative liveness solver (`sub_775010` + `sub_773140`). Walks the register table checking interference chains: for each VR with a chain at `VR+136`, tests whether the chain target's kill set is a subset of the VR's kill set (`sub_BDC390 = isSubsetOf`). Non-subset cases receive the `+264 bit 1` flag, marking them as interference candidates.

**Phase 2: Work structure allocation** -- Allocates a scratch array `s[]` (one entry per split candidate), a hash table for interference tracking (power-of-2 buckets sized via `_BitScanReverse64`), and an array of 64-byte per-block split records:

```c
struct PerBlockSplitRecord {    // 64 bytes, indexed by block ID
    void*    list_head;         // +0:  interference linked list
    void*    first_in_block;    // +8:  first entry pointer
    void*    sentinel;          // +16: self-pointer
    void*    reserved;          // +24
    void*    last_in_block;     // +32: last entry pointer
    void*    tail;              // +40: tail pointer
    int32_t  count;             // +48: entry count
    int32_t  pad;               // +52
    void*    allocator_ref;     // +56: refcounted allocator
};
```

**Phase 3: Main splitting loop** -- Iterates the ordered register array at `ctx+792` in **reverse** order (highest VR ID first). For each VR, walks the def-use chain via `ctx+296` (register table), classifying instructions by opcode:

| Opcode (masked) | Meaning | Split Action |
|-----------------|---------|-------------|
| 167 (0xA7) | Phi-like | Walk up phi chain, split at each level via `sub_931920` |
| 158 (0x9E) | Copy-like | Similar chain walk with copy-specific handling |
| 188 (0xBC) | Multi-operand special | Check operand types, dispatch to `sub_BE3720` for multi-source split |
| 27 (0x1B) | Register move | Standard split point; emit via `sub_9314F0` with 4 operands |
| 269 (0x10D) | Copy | Lightweight split; emit via `sub_9314F0` with 2 operands |

For each split: allocates a new VR via `sub_931920`, copies the three bitvector fields (`sub_BDBA60` allocates, `sub_BDC1B0` copies `dst |= src`), validates the register class via `sub_9314F0` (called 11 times total across different split patterns), and updates the interference hash via `sub_BEEC80`.

The inline interference check in the hot path (line 2897 of `sub_BEF110`):

```c
// Fast single-bit approximation: is vr_class_id possibly live in the kill set?
if ((1 << vr_class_id) & kill_set[vr_class_id >> 5]) != 0)
    // Bit is clear -- VRs definitely do not interfere; skip expensive test
```

When this single-bit probe returns zero, the candidate is skipped immediately. When it returns non-zero, Phase 4 calls `sub_BEE7F0` for a full bitvector intersection.

**Phase 4: Interference hash processing** -- Builds a global interference hash table using FNV-1a (`0x811C9DC5` offset basis, `16777619` prime). Walks per-block split records, for each entry scans the kill bitvector (`sub_BDDC00` clears from position, scanning forward) to find concurrently live VRs. Tests interference via `sub_BEE7F0` and emits split instructions via `sub_934630` (opcode 46). The hash table resizes when load factor exceeds 50%.

**Interference hash table layout.** The hash table object (`a2` in `sub_BEEC80`) contains a pointer to a 192-byte bucket array, a bucket count (always a power of two, initially 8), an entry count, and a total-entries counter. The 192-byte array holds 8 buckets of 24 bytes each:

```c
struct HashBucket {           // 24 bytes, indexed by FNV_hash & (bucket_count - 1)
    HashNode*  head;          // +0:  first node in collision chain
    HashNode*  tail;          // +8:  last node (for O(1) append)
    int32_t    count;         // +16: entries in this bucket
    int32_t    pad;           // +20
};

struct HashNode {             // 32 bytes, allocated from pool
    HashNode*  next;          // +0:  collision chain link
    int32_t    key;           // +8:  packed VR-pair identifier
    int32_t    pad;           // +12
    void*      data;          // +16: pointer to interference data
    uint32_t   hash;          // +24: cached FNV-1a hash value
    int32_t    pad2;          // +28
};
```

When the hash table grows (`sub_865E40` at load factor > 50%), a new 192-byte array is allocated and all nodes are rehashed by `node.hash & (new_count - 1)`, redistributing into the new bucket's `head`/`tail` chain. The old array is freed through the refcounted allocator.

**Full bitvector intersection (sub\_BEE7F0, 1166 bytes).** Called as `sub_BEE7F0(result, hash_node, kill_bitvec)`. The function receives two bitvectors as sparse iterators and an RB-tree node from the hash table. It constructs two bitvector iterators via `sub_BDBEF0` (begin) and `sub_BDBF80` (end), then walks the RB-tree rooted at `hash_node+8` in order. Each tree node carries a 4-qword (32-byte) bitvector chunk at offset `+0x20` through `+0x3F`, and a bucket index at `+0x18`.

The core intersection loop synchronizes two cursors -- one over the kill bitvector's qword array and one over the tree node's chunk array. For each tree node visited in order:

1. Computes a composite position `(4 * node[+0x18]) | (chunk_offset)` and advances the kill-bitvector cursor (skipping zero qwords) to match.
2. When positions align, performs a qword AND between the kill-bitvector word and the tree-node chunk word. A non-zero result means interference exists.
3. On hit: executes `tzcnt` to find the lowest set bit within the intersecting qword, packs the result as `(qword_index << 6) | bit_position` into `result+0`, sets `result+4 = 1`, and returns immediately.
4. On exhaustion of either iterator: sets `result[0] = 0, result[4] = 0` (no interference).

The bitvector uses a mixed-width layout: when the declared size (`VR+8`) is odd, the last element is stored as a 32-bit dword at `base + 4*(size-1)` instead of a full qword, handled by a special-case branch at each comparison point. Tree traversal follows standard in-order successor logic (right-child-then-leftmost, or walk-up-to-first-right-parent), advancing through all interference entries for the given hash bucket.

**Phase 5: Cleanup** -- Marks phi/copy chains with the `+245` rewrite flag (triggering opcode mutation from 188 to 93 or 95), frees hash tables and per-block records, clears `ctx+1370 bit 2` to signal liveness invalidation.

```
function OriSplitLiveRanges(func):
    // Phase 1: Pre-analysis
    rebuild_basic_blocks(func, 0)           // sub_781F80
    alloc_kill_bitvectors(func)             // sub_BEAFD0: VR+96
    alloc_gen_bitvectors(func)              // sub_BEB110: VR+24
    compute_liveness(func)                  // sub_775010
    propagate_per_block(func, 0)            // sub_773140
    mark_interference_candidates(func)      // inline: walk chains, test subsets

    // Phase 2: Work structure allocation
    allocate_work_structures(split_candidate_count)

    // Phase 3: Main splitting loop
    for each VR in ordered_array[ctx+792] (reverse):
        walk def-use chain via ctx+296:
            classify instruction by opcode
            if splittable:
                new_vr = allocate_vr(func, vr, def_instr)    // sub_931920
                copy_bitvectors(new_vr, vr)                   // sub_BDBA60 + sub_BDC1B0
                validate_reg_class(new_vr, opcode, operands)  // sub_9314F0
                update_interference_hash(new_vr)               // sub_BEEC80

    // Phase 4: Interference hash processing
    for each entry in interference_hash:
        for each concurrent_vr in kill_bitvector:
            if interferes(entry, concurrent_vr):              // sub_BEE7F0
                emit_split_instruction(entry, concurrent_vr)  // sub_934630

    // Phase 5: Cleanup
    mark_rewrite_flags()                    // byte +245
    free_work_structures()
    ctx[+1370] &= ~4                       // invalidate liveness
```

### Three Bitvector Fields per Virtual Register

The splitting pass maintains three independent bitvectors per VR, all using the standard 32-bit-word `BitVector` from `0xBDBA60`--`0xBDDD40`:

| VR Offset | Name | Content | Allocated by |
|-----------|------|---------|-------------|
| `+96` | Kill set | Registers defined by this VR's instructions | `sub_BEAFD0` |
| `+24` | Gen set | Registers used before definition in this VR's range | `sub_BEB110` |
| `+176` | Live-through set | Registers live through the range without kill or gen | Derived |

These per-VR bitvectors differ from the per-block liveness bitvectors used by `OriPerformLiveDead`. The per-block sets track global liveness; the per-VR sets track interference within a single virtual register's live range, enabling the split decision: if two VRs have overlapping kill sets (tested via the fast inline `(1 << id) & word[id >> 5]` check), they interfere and splitting one of them at the boundary reduces the overlap.

### Helper Functions

| Address | Identity | Role |
|---------|----------|------|
| `sub_BEAFD0` | `AllocKillBitvectors` | Allocate `VR+96` kill sets; propagate via interference chain `VR+136` |
| `sub_BEB110` | `AllocGenBitvectors` | Allocate `VR+24` gen sets; scan phi/copy defs (opcodes 158, 167) |
| `sub_BE3390` | `ComputeSplitCount(interference)` | Count split points for interference-chain case |
| `sub_BE3590` | `ComputeSplitCount(clean)` | Count split points for non-interfering case |
| `sub_BE3720` | `ComputeSplitCount(multiSrc)` | Count split points for multi-source operand case |
| `sub_BEE7F0` | `TestInterference` | Test bitvector interference between two VRs |
| `sub_BEEC80` | `UpdateHashWithSplit` | Update per-split hash table (192-byte entries, 8 buckets) |

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

The allocator uses liveness information for interference computation, spill cost estimation, and spill placement (see [Fat-Point Allocation Algorithm](../regalloc/algorithm.md) for full detail).

#### Fat-Point Model vs Traditional Interference Graph

A traditional graph-coloring allocator (Chaitin-Briggs) builds an explicit interference graph -- one node per VR, one edge per pair of simultaneously-live VRs -- then simplifies, selects, and spills. The ptxas allocator builds no such graph. Instead, the interference builder `sub_926A30` (4005 lines) converts liveness overlap into **constraint nodes** attached to each VR at `vreg+144`. During allocation, `sub_957160` iterates these constraints to fill two 512-DWORD pressure histograms (primary and secondary), then picks the physical slot with the lowest accumulated weight. The key structural differences:

| | Traditional IG | ptxas Fat-Point |
|---|---|---|
| Representation | Adjacency matrix/list (N^2 edges) | Per-VR linked list of 24-byte constraint nodes |
| Cost model | Binary (interfere / don't) | Weighted (integer cost per constraint) |
| Pair/alignment | Separate pre-coloring pass | Inline constraint types 5--7, 11--14 |
| Long-range preference | Coalescing heuristic | Type 15 (range) writes to secondary array as tie-breaker |
| Relaxation | Spill-and-retry | Per-iteration soft-constraint skip threshold (OCG knob) |
| Complexity | O(N^2) build + O(N) simplify | O(N * C) per VR, C = constraint count |

The term "fat point" refers to the pressure histogram: each physical register slot is a point in the histogram, and the accumulated weight at that point is the "fat" -- the total interference cost from all constraints that map to that slot.

#### The 15 Constraint Types

Each constraint node stores a type (0--15), a target VR/physical register index, and an integer weight. The type determines which histogram slots receive that weight during the pressure walk:

| Type | Name | Effect on histogram |
|------|------|---------------------|
| 0 | Point | `primary[phys_reg_of(target)] += weight` -- hard same-point interference |
| 1 | Exclude-one | All slots except one get weight -- blocks a single physical register |
| 2 | Exclude-all-but | All slots except target get weight -- forces VR into one register |
| 3 | Below-point | Slots below target get weight -- downward-exposed liveness |
| 4 | (reserved) | Dispatched through vtable\[240\] fallback |
| 5 | Paired-low | Even-indexed slot only -- low half of 64-bit pair |
| 6 | Paired-high | Odd-indexed slot only -- high half of pair |
| 7 | Aligned-pair | Both even and odd slots -- full pair constraint |
| 8 | Phi-related | Stride-2 accumulation across primary -- soft, from CSSA phis |
| 9 | (reserved) | Dispatched through vtable\[240\] fallback |
| 10 | (reserved) | Dispatched through vtable\[240\] fallback |
| 11 | Paired-even-parity | All slots except offset -- bank-conflict avoidance (even parity) |
| 12 | Paired-odd-parity | All slots except inverse offset -- bank-conflict avoidance (odd parity) |
| 13 | Paired-parity-group | Even-only exclusion for bank groups |
| 14 | Paired-parity-extended | Odd-only exclusion for wider register groups (quads) |
| 15 | Range | **Secondary** array only -- interval-proportional weight for tie-breaking |

Type 15 is architecturally distinct: it is the only type that writes to the secondary histogram. All others write to primary. This separation means primary captures hard interference while secondary captures long-range preference signals that break ties when multiple slots have equal primary cost.

The builder uses SSE2-vectorized inner loops for types 0, 8, and 15 when the scan width exceeds 4 slots, and FNV-1a hashing (seed `0x811C9DC5`) for pre-allocation candidate lookups.

#### Liveness-Derived Costs

- **Spill cost estimation**: `sub_94E620` computes spill costs weighted by liveness range length and instruction properties.
- **Spill placement**: `sub_9449B0` (1800 bytes) iterates instructions in reverse block order using bitvector operations to determine optimal spill/reload insertion points.

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

The scheduling subsystem has its own dataflow infrastructure (separate from the optimizer's OriPerformLiveDead). Decompilation reveals these 9 functions implement two reusable data structures -- a **memory allocator** (red-black tree backed free-list) and an **FNV-1a hash table** -- that the scheduler instantiates for its dataflow bookkeeping:

| Address | Size | True identity |
|---------|------|---------------|
| `sub_8DB070` | 8.2KB | `PoolAllocator::free` -- coalesces freed blocks into a segregated free-list; blocks <= 512B go to size-class bins at `+96`; larger blocks go to sorted lists at `+72`/`+80` |
| `sub_8DB5F0` | 8.4KB | `RBTree::rebalance` -- red-black tree fix-up after delete; color flag at node `+40` (0=red, 1=black), children at `+24`/`+32`, parent at `+16` |
| `sub_8DBAF0` | 16KB | `PoolAllocator::allocFromTree` -- allocates from RB-tree ordered free-list; best-fit search for blocks > 512B, splits remainder back into tree; calls `sub_8DB5F0` for rebalancing |
| `sub_8DC3F0` | 3.0KB | `PoolAllocator::alloc` -- sized allocation entry point; tries size-class bin at `+96`, then bitmap scan (`tzcnt`), then falls through to `sub_8DBAF0` (tree) and `sub_8DAC50`/`sub_8DAA10` (slab) |
| `sub_8DC620` | 3.3KB | `PoolAllocator::allocAligned` -- like `sub_8DC3F0` but adds 8 bytes for header alignment; returns `result + 8` to caller |
| `sub_8DC880` | 10KB | `HashMap::insertOrFind` -- FNV-1a hash (seed `0x811C9DC5`, prime `16777619`) on 4-byte key; 8-bucket initial table; rehashes at load-factor > 50% by allocating 4x buckets and redistributing via `hash % new_size` |
| `sub_8DCF20` | 23KB | `HashMap::insertOrFindWide` -- FNV-1a hash on 8-byte key (address + ID pair); includes inline `memcpy` for payload transfer to new node; same rehash policy as `sub_8DC880` |
| `sub_8DE7A0` | 12KB | `HashMap::insertWideWithPayload` -- allocates a 168-byte dataflow node; stores payload at `+32`/`+8`/`+24`; builds two nested hash tables (at offsets `+40` and `+136`); inserts into outer table via FNV-1a on (uint32, uint64) compound key |
| `sub_8DEF90` | 2.0KB | `HashMap::lookupTwoLevel` -- two-level lookup: first hashes (uint32, uint64) into the outer table to get a dataflow-node ID, then hashes that ID into the inner table at `+80` to retrieve the result pointer |

The scheduler instantiates these structures at Code Object `+832` to maintain per-block dataflow state. The RB-tree in `sub_8DBAF0`/`sub_8DB5F0` manages the backing memory pool (node `+40` color flag, rotations for balance), not liveness intervals directly. The hash tables in `sub_8DC880`--`sub_8DEF90` store the actual dataflow facts keyed by (block-ID, register-address) pairs, using the standard FNV-1a hash seen throughout ptxas. The two-level lookup in `sub_8DEF90` enables the iterative solver to efficiently query "what is the dataflow state of register R at block B?" without scanning all blocks.

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
| `sub_8DB070` | 8.2KB | `PoolAllocator::free` (coalesce + return to free-list) | HIGH (0.95) |
| `sub_8DB5F0` | 8.4KB | `RBTree::rebalance` (red-black fix-up after delete) | HIGH (0.95) |
| `sub_8DBAF0` | 16KB | `PoolAllocator::allocFromTree` (RB-tree best-fit alloc) | HIGH (0.95) |
| `sub_8DC3F0` | 3.0KB | `PoolAllocator::alloc` (sized block entry point) | HIGH (0.95) |
| `sub_8DC620` | 3.3KB | `PoolAllocator::allocAligned` (header-aligned alloc) | HIGH (0.95) |
| `sub_8DC880` | 10KB | `HashMap::insertOrFind` (FNV-1a, 4-byte key) | HIGH (0.95) |
| `sub_8DCF20` | 23KB | `HashMap::insertOrFindWide` (FNV-1a, 8-byte key) | HIGH (0.95) |
| `sub_8DE7A0` | 12KB | `HashMap::insertWideWithPayload` (168-byte node + nested tables) | HIGH (0.90) |
| `sub_8DEF90` | 2.0KB | `HashMap::lookupTwoLevel` (outer + inner FNV-1a lookup) | HIGH (0.95) |
| `sub_A0B5E0` | varies | Uninitialized register detector | HIGH (0.97) |
| `sub_A7BC80` | 36KB | RegisterSetManager (multi-file liveness) | MEDIUM (0.65) |
| `sub_BEF110` | 108KB | `OriSplitLiveRanges` core (Phase 19) | HIGH (0.90) |
| `sub_BF33D0` | ~1KB | `OriSplitLiveRanges` knob-gated entry (reads knob 456) | HIGH (0.90) |
| `sub_A1D3A0` | ~0.2KB | `OriSplitLiveRanges` vtable execute | HIGH (0.90) |
| `sub_BEAFD0` | ~2KB | `AllocKillBitvectors` (VR+96 per-VR kill sets) | HIGH (0.85) |
| `sub_BEB110` | ~3KB | `AllocGenBitvectors` (VR+24 per-VR gen sets) | HIGH (0.85) |
| `sub_BE3390` | varies | `ComputeSplitCount(interference)` | MEDIUM (0.80) |
| `sub_BE3590` | varies | `ComputeSplitCount(clean)` | MEDIUM (0.80) |
| `sub_BE3720` | varies | `ComputeSplitCount(multiSrc)` | MEDIUM (0.80) |
| `sub_BEE7F0` | varies | `TestInterference` (BV interference test) | MEDIUM (0.80) |
| `sub_BEEC80` | ~1KB | `UpdateHashWithSplit` (per-split hash update) | MEDIUM (0.80) |
| `sub_BEB9C0` | varies | Hash table init/destroy (secondary) | MEDIUM (0.75) |
| `sub_BEBA40` | varies | Hash table init/destroy (primary) | MEDIUM (0.75) |

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
| `+792` | Context offset: reverse-ordered register array (for live range splitting) |
| `+1370` bit 2 | Flag: liveness invalid (cleared by `sub_BEF110` on exit) |
| `+1624` | Context offset: register budget (double, read by `sub_BF33D0`) |
| `VR+24` | Virtual register offset: gen bitvector (allocated by `sub_BEB110`) |
| `VR+96` | Virtual register offset: kill bitvector (allocated by `sub_BEAFD0`) |
| `VR+136` | Virtual register offset: interference chain (linked list of aliased VRs) |
| `VR+144` | Virtual register offset: register class ID (int32) |
| `VR+176` | Virtual register offset: live-through bitvector |
| `VR+245` | Virtual register byte flag: needs-opcode-rewrite (set by Phase 19 cleanup) |
| `VR+264` | Virtual register flags: bit 0 = has-interference-chain, bit 1 = non-subset, bit 2 = was-split |
| `VR+280` | Virtual register flags: bit 2 = needs-split, bit 4 = propagated, bit 12 = predicate-qualified |
| `0x811C9DC5` | FNV-1a offset basis (used in Phase 19 interference hash) |
| `16777619` | FNV-1a prime (0x01000193) |
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
