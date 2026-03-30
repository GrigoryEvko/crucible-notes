# SCEV Invalidation & Delinearization

SCEV analysis results are expensive to compute and are cached aggressively. When the IR mutates -- a loop is unrolled, a value is replaced, a block is deleted -- cached SCEV expressions, range information, and backedge-taken counts can become stale. The invalidation subsystem (`forgetLoop`, `forgetValue`, `forgetAllLoops`) determines exactly which cache entries must be discarded after each transformation. Get it wrong in either direction and the compiler either produces incorrect code (stale data) or wastes time recomputing everything (over-invalidation).

Delinearization is the complementary recovery problem: given a flat pointer expression like `base + i*N*M + j*M + k`, recover the original multi-dimensional subscripts `[i][j][k]`. This is critical for GPU code because memory coalescing analysis needs to know whether adjacent threads in a warp are accessing adjacent addresses -- a question that can only be answered by examining per-dimension subscripts against the thread index structure.

In cicc v13.0, both subsystems carry NVIDIA-specific modifications. The invalidation engine has an extended exit-analysis depth threshold and an early-out for simple two-operand AddRec expressions common in GPU loops. The delinearization engine has a polymorphic predicate collector that supports GPU-aware strategies for shared memory bank conflict detection and coalescing analysis, plus at least 9 configuration knobs not present in upstream LLVM.

| Property | Value |
|----------|-------|
| `forgetLoop` address | `sub_DE2750` (`0xDE2750`) |
| `forgetLoop` size | 10,051 bytes (~2,271 asm lines) |
| `forgetValue` address | `sub_D9EE30` (`0xD9EE30`) |
| `forgetValue` size | ~9 KB |
| `forgetAllLoops` address | `sub_D9D700` (`0xD9D700`) |
| `forgetAllLoops` size | ~8 KB |
| `delinearize` address | `sub_DE9D10` (`0xDE9D10`) |
| `delinearize` size | 3,614 bytes (~849 asm lines) |
| `collectParametricTerms` address | `sub_DE8D20` (`0xDE8D20`) |
| Hash function | `(key >> 9) ^ (key >> 4) & (capacity - 1)` |
| Empty sentinel | `0xFFFFFFFFFFFFF000` |
| Tombstone sentinel | `0xFFFFFFFFFFFFE000` |

## Cache Invalidation

### The Seven Caches

SCEV maintains seven distinct cache tables that must be kept consistent. Each has its own eviction path inside `forgetLoop`:

| # | Cache | Entry size | Key | Value | Context offset |
|---|-------|-----------|-----|-------|----------------|
| 1 | ValueExprMap (primary) | 16 bytes | `Value*` | `SCEV*` | main SE object |
| 2 | Unsigned range cache | 40 bytes | `SCEV*` | `ConstantRange` | `+976` |
| 3 | Signed range cache | 40 bytes | `SCEV*` | `ConstantRange` | `+1008` |
| 4 | BTC cache | 168 bytes (`0xA8`) | loop `SCEV*` | `BackedgeTakenInfo` | `+0x290` |
| 5 | Per-exit BTC cache | 16 bytes | exit `SCEV*` | exit count | `+0x490` |
| 6 | AddRec folding cache | per-expression | AddRec pair | folded form | per-expression |
| 7 | Predicated BTC cache | 16 bytes | loop `SCEV*` | predicated count | secondary table |

All hash tables use the same open-addressing scheme with the hash function `(key >> 9) ^ (key >> 4)`, the same sentinel values, and the same growth policy: resize when `4 * (entries + 1) >= 3 * capacity`, rehash-in-place when tombstones exceed `capacity / 8`.

### forgetLoop: The 8-Phase Algorithm

`sub_DE2750` is the largest invalidation function -- 10 KB of machine code organized into eight sequential phases. It is called after every loop transformation that might invalidate SCEV data.

**Signature:**

```c
void forgetLoop(
    ScalarEvolution *SE,    // rdi -- the SE context
    Loop *L,                // rsi -- loop being forgotten
    BasicBlock *Header,     // rdx -- loop header block
    ExitInfo *Exits,        // rcx -- exit block info (nullable)
    int DepthFlag,          // r8d -- 0=shallow, 1=deep, >1=bounded
    int ExtraFlag,          // r9d -- controls AddRec early-out
    SmallDenseSet *Visited  // stack -- prevents cycles in nested loops
);
```

**Phase 1 -- Block value collection** (`0xDE27C9`). Iterates the loop's basic blocks and collects all Values that have cached SCEV entries. The block array is at `loop[+0x20] -> [+0x10]` (pointer) / `[+0x18]` (count), stored as 32-byte entries. For each Value, a dominance check (`sub_B19D00`) confirms it belongs to the loop, then the SCEV index is extracted from a 27-bit field at `value[+4] & 0x7FFFFFF`. Collected pointers are stored in a SmallVector (inline capacity 6) with bit 2 set as a tag.

**Phase 1B -- Scope chain collection** (`0xDE28A7`). Walks a scope chain obtained via `sub_B6AC80(SE[0][+0x28], 0x99)`, where `0x99` is the SCEV scope identifier. Filters to `SCEVUnknown` entries (type byte `0x55`) with specific flag conditions (byte `[+0x21] & 0x20`), verifying loop membership and dominance. This captures values not directly in the loop's blocks but semantically part of its analysis scope.

**Phase 2 -- Exit block processing** (`0xDE29D9`). Enumerates exit blocks via `sub_AE6EC0` and processes their AddRec chains. For each exit, reads the chain at `[exit+0x30] & ~7` (stripping tag bits), checks expression kind byte (range `0x1E`--`0x28`), and extracts operands. For the common case of simple `{start, +, step}` two-operand recurrences, an early-out stops after processing 2 operands when `ExtraFlag != 0`. If the loop has exactly 2 exits and `ExtraFlag >= qword_4F88DC8` (a global threshold for maximum exit analysis depth), deep exit analysis is skipped entirely.

**Phase 3 -- Expression dependency analysis** (`0xDE2BC5`). The core invalidation loop. Iterates the collected values in **reverse order** and builds a transitive closure of all dependent SCEV expressions. Uses a stack-based worklist (SmallVector, inline capacity 8) and a SmallDenseSet for visited tracking. The dependency walk dispatches on expression type:

```
Type 0x52 ('R' = AddRec):  Follow Start and Step operands via getSCEV,
                            compare ranges with getRangeRef
Type 0x56 ('V' = variant):  Check function pointer equality at [-0x60]
                            and [+0x08], follow if simple recurrence
Type 0x39/0x3A (flagged):   Check bit 6 of flags byte, follow base pointer
                            or compute canonical form from 27-bit index
General:                    Follow underlying object, check for pointer
                            types (0x11/0x12), verify integer type
```

**Phase 4 -- Primary cache eviction** (`0xDE2DFF`). For each expression identified by Phase 3, looks it up in the ValueExprMap, computes both unsigned and signed ranges via `getRangeRef` (`sub_DBB9F0`), compares old and new ranges via `ConstantRange::contains` (`sub_AB1BB0`), and clears validity bits in the range cache (`[entry+0x20]` for unsigned, `[entry+0x21]` for signed). Wide APInt buffers (>64 bits) are freed through `__libc_free`.

**Phase 5 -- BTC eviction** (`0xDE3D2F`). For each collected value, looks it up in the BTC hash table. On hit: writes `TOMBSTONE`, decrements entry count, increments tombstone count, then calls `forgetMemoizedResults` (`sub_DE2690`) to recursively invalidate any expressions that depended on this backedge-taken count. Also evicts the corresponding predicated BTC entry from the secondary table.

**Phase 6 -- AddRec folding cache cleanup** (`0xDE3230`). For AddRec expressions (type `0x52`), invalidates pre-computed folding results. Extracts the 6-bit opcode from `[expr+2] & 0x3F` and dispatches:

- Opcode `0x20` (shift/power-of-two multiply): checks via `countPopulation` whether the step is a power of two, then calls `tryFoldAddRecWithStep` (`sub_DCFD50`)
- Opcodes `0x22`--`0x29` (binary operations): constructs the appropriate folded expression per operation type and marks it for invalidation
- Opcode `0x24` with pointer type (`0x0E`): skips pointer-integer cast invalidation

**Phase 7 -- Predicate and assumption cleanup** (`0xDE3856`). Processes the predicate hash table via the loop object's fields. Performs range intersection (`sub_AB0910`), union (`sub_AB0A00`), and emptiness/fullness checks (`sub_AAFBB0`, `sub_AAF760`). If the resulting range is neither empty nor full, stores the updated BTC in the loop's entry.

**Phase 8 -- Final output** (`0xDE3CCD`). Writes `0x0101` to `loop->flags[+0x20]`, marking the loop as SCEV-forgotten (bit 0 = primary cache invalidated, bit 8 = secondary cache invalidated). Frees heap-allocated collection and output buffers.

### forgetValue and forgetAllLoops

`forgetValue` (`sub_D9EE30`, ~9 KB) performs single-value eviction. It removes the value's entry from the ValueExprMap, then walks all expressions that transitively depend on it and evicts those as well. Used when a single instruction is replaced (RAUW) or deleted.

`forgetAllLoops` (`sub_D9D700`, ~8 KB) iterates every loop in the function's LoopInfo and calls `forgetLoop` for each one. Used when the entire function's loop structure changes (e.g., after inlining or full function cloning).

### Which Passes Trigger Invalidation

`forgetLoop` is called after these loop transformations:

| Pass | Why invalidation is needed |
|------|---------------------------|
| LoopUnroll | Trip counts change; unrolled body has different IVs |
| LoopVectorize | Widened types; vector IVs replace scalar ones |
| LoopPeeling | Peeled iterations change the start value of recurrences |
| LoopUnswitching | Exit conditions change; control flow restructured |
| LICM | Hoisted values have new SCEV forms outside the loop |
| LoopSimplify | Preheader/exit block insertion changes loop structure |
| LoopRotate | Header/latch swap requires BTC recomputation |
| LoopDistribute | Original loop split into multiple loops |
| LoopIdiomRecognize | Pattern replacement changes loop body |
| LoopIndexSplit (NVIDIA) | IV range split into subranges |
| MemorySpaceOpt (NVIDIA) | Address space changes invalidate pointer SCEVs |

The `DepthFlag` parameter controls the aggressiveness of invalidation: `0` does shallow invalidation (only direct loop values), `1` follows all dependency chains, and values >1 impose a bounded depth useful for performance in deeply nested loops. The `Visited` parameter (a `SmallDenseSet*`) prevents infinite cycles when nested loops have mutual SCEV dependencies.

The `forget-scev-loop-unroll` knob (boolean) controls whether SCEV cache is invalidated after unrolling -- disabling it is unsound but can be used for compile-time experimentation.

## Delinearization

### The Problem

CUDA kernels routinely access multi-dimensional arrays:

```c
float val = A[blockIdx.x * BLOCK_H + threadIdx.y][blockIdx.y * BLOCK_W + threadIdx.x];
```

By the time this reaches LLVM IR, the address computation has been flattened:

```
%addr = getelementptr float, ptr %A, i64 %flat_idx
; where %flat_idx = (blockIdx.x * BLOCK_H + threadIdx.y) * N + (blockIdx.y * BLOCK_W + threadIdx.x)
```

SCEV sees this as a single polynomial. Delinearization recovers the per-dimension subscripts, which are essential for:

1. **Coalescing analysis**: determining whether adjacent threads (`threadIdx.x`, `threadIdx.x+1`, ...) access adjacent memory addresses (coalesced) or strided addresses (uncoalesced). This requires isolating the dimension where `threadIdx.x` appears.
2. **Shared memory bank conflict detection**: 32 banks, 4-byte stride. Knowing whether the innermost subscript is `threadIdx.x` (conflict-free) vs. `threadIdx.x * stride` (potential conflicts) requires dimensional decomposition.
3. **Dependence analysis**: per-dimension dependence tests (Banerjee, GCD, MIV) are more precise than whole-expression tests. Delinearized subscripts feed `DependenceInfo` for vectorization legality.

### Delinearization Context

The delinearizer (`sub_DE9D10`) operates on a context object:

| Offset | Type | Field | Purpose |
|--------|------|-------|---------|
| `+0x00` | `ScalarEvolution*` | SE | Parent SCEV context |
| `+0x08` | `SCEV*` | ElementSize | Innermost element size |
| `+0x10` | `uint8_t` | Flags | Bit 0: inline cache mode |
| `+0x18` | 64 bytes | InlineCache | 4-slot direct-mapped table (inline mode) |
| `+0x20` | `uint32_t` | Capacity | Heap table capacity (heap mode) |
| `+0x58` | `SCEV*` | TargetArrayPtr | Array being delinearized |
| `+0x60` | `void*` | PredicateCollector | Nullable; collects validity predicates |
| `+0x68` | `SCEV*` | StepRecurrence | AddRec step for innermost dimension |

The inline cache (4 slots of 16 bytes at `+0x18`) is a small-buffer optimization sized for the overwhelmingly common GPU case of 1D or 2D array accesses. Cache entries use the same `(key >> 9) ^ (key >> 4)` hash as all other SCEV tables.

### The Recursive Delinearization Algorithm

`sub_DE9D10` is a recursive function dispatching on 17 SCEV expression kinds via a jump table:

| Kind | Expression type | Handling |
|------|----------------|----------|
| 0, 1, 16 | Constant, TruncateExpr (ident), Unknown | Leaf -- return unchanged |
| 2 | TruncateExpr | Recurse into inner, rebuild with `getTruncateExpr` |
| 3 | SignExtendExpr | Recurse; dimension discovery on AddRec step match |
| 4 | ZeroExtendExpr | Recurse; dimension discovery on AddRec step match |
| 5 | AddExpr | N-ary: delinearize each operand, rebuild with `getAddExpr` |
| 6 | MulExpr | N-ary: delinearize each factor, rebuild with `getMulExpr` |
| 7 | UDivExpr | Delinearize both operands, rebuild with `getUDivExpr` |
| 8 | AddRecExpr | N-ary with wrap flag preservation; critical path |
| 9--13 | SMax/UMax/SMin/UMin/SeqUMin | N-ary: delinearize operands, rebuild |
| 14 | PtrToIntExpr | Recurse into pointer, rebuild |
| 15 | GEP | Primary dimension discovery entry point |

**The N-ary pattern.** Cases 5, 6, 8--13 share a common template:

```c
SmallVector<const SCEV*, 2> NewOps;  // inline capacity 2
bool Changed = false;
for (auto *Op : Expr->operands()) {
    const SCEV *NewOp = delinearize(Ctx, Op);  // recursive
    NewOps.push_back(NewOp);
    if (NewOp != Op) Changed = true;
}
if (!Changed) return Expr;  // pointer identity optimization
return rebuildExpr(SE, Kind, NewOps);
```

The "changed" flag enables pointer identity short-circuiting: if no operand was modified during recursion, the original expression pointer is returned without allocation.

**AddRecExpr (case 8)** is the most critical case for GPU code. Multi-dimensional array accesses manifest as nested AddRec expressions: `{A[0][0], +, dim1}<outer_loop>` wrapping `{init, +, 1}<inner_loop>`. The delinearizer preserves wrap flags (`NSW`/`NUW`/`NW` from bits `[+0x1C] & 7`) and the step value (`[+0x30]`) when reconstructing via `getAddRecExpr` (`sub_DBFF60`).

**ZeroExtend/SignExtend (cases 3, 4)** are secondary dimension discovery points. When the inner operand is an AddRec whose step matches `Ctx->StepRecurrence` (`+0x68`) and the AddRec has exactly 2 operands (the common `{start, +, step}` form), the handler extracts dimension information: it calls `getElementSize` (`sub_D33D80`) and `getConstant` (`sub_DA4270`) to compute the element count, then pushes a new term into the term collector at `Ctx[+0x58]`. This identifies a dimension boundary -- the extend operation wrapping a matching-step AddRec indicates the point where one array dimension ends and another begins.

**GEP (case 15)** is the primary entry for actual dimension discovery. It first checks the predicate collector (`Ctx[+0x60]`). If present, it searches the collector's table for a matching GEP index entry (type field `== 1`, matching `scev_expr`, operation `== 0x20`). If no predicate collector or no match, it falls back to structural delinearization via `sub_DE97B0`, which analyzes the GEP's index computation structure, iterates discovered terms, and classifies them by dimension type. Terms matching `Ctx->StepRecurrence` go to the direct collector; others go through the predicate collector's virtual dispatch (`vtable[+0x10]`).

### Fixed-Point Iteration

The function itself is a single recursive pass, but its callers implement a fixed-point loop:

1. Initialize the context with an initial guess for dimension sizes
2. Call `sub_DE9D10` to delinearize using those dimensions
3. During recursion, the GEP and extend handlers **collect** new dimension information into `Ctx[+0x58]` (term collector) and `Ctx[+0x60]` (predicate collector)
4. If collected dimensions differ from the initial guess, update and repeat from step 2
5. Terminate when dimensions stabilize or a maximum iteration count is exceeded

The memoization cache ensures unchanged sub-expressions are not recomputed across iterations.

### Parametric vs Fixed-Size Arrays

Upstream LLVM has the `delinearize-use-fixed-size-array-heuristic` knob (default: `false`). When the standard parametric delinearization fails -- typically because dimension sizes are runtime values with no SCEV relationship -- the fixed-size heuristic uses compile-time-known array dimensions from type metadata to guide decomposition.

cicc extends this with an alternative delinearization entry point at `sub_147EE30` (25 KB), which applies additional heuristics controlled by at least 3 of the delinearization config globals (`dword_4F9AB60`, `dword_4F9AE00`, `dword_4F9B340`). This second path is likely NVIDIA-enhanced for cases common in GPU code, such as dynamically-allocated shared memory with dimensions derived from kernel launch parameters.

The dependence analysis subsystem has its own entry points into delinearization (`sub_146F1B0` at 40 KB for `delinearizeAccess`, `sub_146B5E0` at 18 KB for `tryDelinearize`) that combine delinearization with per-dimension dependence testing in a single pass.

### GPU-Specific Delinearization Patterns

**Thread grid indexing.** The canonical GPU pattern `threadIdx.x + blockIdx.x * blockDim.x` produces an AddRec with step = `blockDim.x` (grid stride). The delinearizer recognizes this by matching the step recurrence against `Ctx[+0x68]`. When the step corresponds to a grid dimension, the subscript identifies which dimension of a multi-dimensional array is parallelized across the thread grid.

**Shared memory bank conflicts.** For shared memory arrays, the delinearizer feeds into bank conflict analysis. Shared memory has 32 banks with 4-byte interleaving. If delinearization reveals `A[threadIdx.y][threadIdx.x]` with row stride 32 (or any multiple of 32), every thread in a warp hits the same bank -- a 32-way conflict. If the stride is relatively prime to 32, accesses are conflict-free. This analysis requires knowing per-dimension subscripts, which only delinearization can provide from the flat pointer arithmetic.

**Predicate collector polymorphism.** The `PredicateCollector` at `Ctx[+0x60]` uses virtual dispatch (`vtable[+0x10]`), allowing different delinearization strategies to be plugged in:
- Standard delinearization for host code
- GPU-aware delinearization that considers shared memory bank geometry
- Coalescing-aware delinearization that checks whether the innermost subscript varies with `threadIdx.x`

**High-dimensional tensors.** The term collector at `Ctx[+0x58]` is a growable SmallVector, supporting arrays with arbitrary dimensionality. This matters for tensor operations in CUDA (e.g., CUTLASS library patterns, which cicc special-cases elsewhere -- see the `cutlass` substring check in the dependence analysis region).

## SCEV Term Collection

Before delinearization runs, `collectParametricTerms` (`sub_DE8D20`) walks the SCEV expression tree to extract candidate terms:

- `SCEVAddRecExpr` operands yield stride candidates (the step of each AddRec)
- `SCEVUnknown` and `SCEVMulExpr` nodes yield dimension-size candidates
- `SCEVSignExtendExpr` nodes are also collected (they often wrap dimension-related terms)

These candidates are passed to `findArrayDimensions` (`sub_147B0D0`) which uses product decomposition to determine which terms correspond to array dimensions. The resulting dimension list seeds the delinearization context before `sub_DE9D10` is invoked.

## Configuration

### SCEV Invalidation Knobs

| Knob | Default | Effect |
|------|---------|--------|
| `forget-scev-loop-unroll` | `true` | Enable SCEV invalidation after loop unrolling |
| `verify-scev` | `false` | Verify SCEV consistency after transformations |
| `verify-scev-strict` | `false` | Stricter verification (compare old/new trip counts) |
| `verify-scev-maps` | `false` | Verify SCEV map consistency |
| `qword_4F88DC8` (max exit analysis depth) | unknown | Threshold beyond which deep exit analysis is skipped |

### SCEV Analysis Depth Limits (shared with invalidation)

| Knob | Default | Effect |
|------|---------|--------|
| `scalar-evolution-max-iterations` | 100 | Maximum loop iterations for constant evaluation |
| `scalar-evolution-max-scev-compare-depth` | 32 | Maximum SCEV comparison recursion depth |
| `scalar-evolution-max-arith-depth` | 32 | Maximum SCEV arithmetic simplification depth |
| `scalar-evolution-max-ext-depth` | 8 | Maximum sign/zero-extend nesting depth |
| `scalar-evolution-max-cast-depth` | 8 | Maximum cast chain depth |
| `scalar-evolution-max-constant-evolving-depth` | 32 | Maximum constant evolution depth |
| `scalar-evolution-max-expr-size` | 384 | Maximum expression node count |
| `scalar-evolution-max-expr-failures` | 100 | Maximum SCEV creation failures before bailout |
| `scalar-evolution-max-scev-operations-implication-depth` | 2 | Maximum depth for implications |
| `scalar-evolution-max-value-compare-depth` | 2 | Maximum value comparison depth |

### NVIDIA-Specific SCEV Knobs

| Knob | Effect |
|------|--------|
| `aggressive-positive-stride-analysis` | More aggressive positive-stride IV analysis (nvbug 3972412) |
| `do-sign-ext-simplify` | Simplify SCEV sign-extend expressions |
| `do-sign-ext-expand` | Expand sign-extends during SCEV construction |
| `track-trip-count-more` | Track loop trip counts more aggressively |
| `scev-mulops-inline-threshold` (32) | Max MulExpr operands before out-of-line |
| `scev-addops-inline-threshold` (500) | Max AddExpr operands before out-of-line |

### Delinearization Knobs

| Global | Likely identity | Notes |
|--------|----------------|-------|
| `byte_4F9A8C0` | Delinearization enable flag | Master enable for the delinearization subsystem |
| `dword_4F9A620` | Config 1 | Referenced by combined delinearize-and-test |
| `dword_4F9A700` | Config 2 | Referenced by `delinearizeAccess` core |
| `dword_4F9A7E0` | Config 3 | Referenced by `delinearizeAccess` core |
| `dword_4F9AB60` | Config 4 | Referenced by alternative delinearization v2 |
| `dword_4F9AC40` | Config 5 | Referenced by dependence distance with delinearization |
| `dword_4F9AE00` | Config 6 (shared) | Referenced by both combined-test and v2 paths |
| `dword_4F9B260` | Config 7 | Referenced by combined delinearize-and-test |
| `dword_4F9B340` | Config 8 | Referenced by alternative delinearization v2 |
| `da-delinearize` | Try to delinearize array references | `DependenceAnalysis` pass knob (upstream LLVM) |
| `da-miv-max-level-threshold` | MIV test depth limit | `DependenceAnalysis` pass knob (upstream LLVM) |

## Function Map

### Invalidation Functions

| Address | Identity | Size | Notes |
|---------|----------|------|-------|
| `sub_DE2750` | `ScalarEvolution::forgetLoop` | 10,051 B | 8-phase loop invalidation |
| `sub_D9EE30` | `ScalarEvolution::forgetValue` | ~9 KB | Single-value eviction |
| `sub_D9D700` | `ScalarEvolution::forgetAllLoops` | ~8 KB | Invalidate all loops |
| `sub_DE2690` | `forgetMemoizedResults` | small | Recursive BTC invalidation helper |
| `sub_DE5FA0` | `ScalarEvolution::verify` | ~52 KB | Debug verification (old/new trip count comparison) |
| `sub_DE5640` | Loop invalidation helper | ~178 lines | Helper for `forgetLoop` |
| `sub_DCE1C0` | SCEV expression invalidator | small | Callback for AddRec folding cleanup |

### Delinearization Functions

| Address | Identity | Size | Notes |
|---------|----------|------|-------|
| `sub_DE9D10` | `ScalarEvolution::delinearize` | 3,614 B | Recursive delinearizer (17-case switch) |
| `sub_DE8D20` | `collectParametricTerms` | ~521 lines | Term extraction before delinearization |
| `sub_DE97B0` | Structural GEP delinearization | small | Sub-analysis called from GEP case |
| `sub_D9ABD0` | `canonicalizeExpr` | small | SCEV normalization |
| `sub_D94080` | `computeAccessFunctions` | ~12 KB | Access function computation |
| `sub_CF5550` | `SCEV_delinearize` (dependence region) | 6,276 B | Alternate copy in dependence analysis |

### Dependence Analysis Delinearization

| Address | Identity | Size | Notes |
|---------|----------|------|-------|
| `sub_146F1B0` | `delinearizeAccess` | 40 KB | Core delinearization for dependence analysis |
| `sub_146B5E0` | `tryDelinearize` | 18 KB | Delinearization attempt with fallback |
| `sub_1472640` | Delinearize subscript | 10 KB | Per-subscript extraction |
| `sub_1473850` | Array dimension inference | 12 KB | Infers dimensions from access patterns |
| `sub_1476060` | `collectSubscripts` | 22 KB | Multi-dimensional GEP subscript collection |
| `sub_14747F0` | Dependence distance with delinearization | 15 KB | Computes dependence vectors using delinearized subscripts |
| `sub_147B0D0` | `findArrayDimensions` | 11 KB | Dimension sizes from SCEV product decomposition |
| `sub_147C070` | Combined delinearize-and-test | 34 KB | Delinearize + per-dimension dependence test |
| `sub_147EE30` | Alternative delinearization v2 | 25 KB | NVIDIA-enhanced heuristics |
| `sub_147DF40` | Partial result combiner | 11 KB | Combines partial delinearization results |

### Key SCEV Callees (shared by both subsystems)

| Address | Identity |
|---------|----------|
| `sub_DBB9F0` | `getRangeRef` -- range computation |
| `sub_AB1BB0` | `ConstantRange::contains` |
| `sub_AB0910` | `ConstantRange::intersectWith` |
| `sub_AB0A00` | `ConstantRange::unionWith` |
| `sub_AAFBB0` | `ConstantRange::isEmptySet` |
| `sub_AAF760` | `ConstantRange::isFullSet` |
| `sub_DD8400` | `getSCEV` -- expression resolution |
| `sub_DCFD50` | `tryFoldAddRecWithStep` |
| `sub_DC7EB0` | `getAddExpr` (N-ary) |
| `sub_DC8BD0` | `getMulExpr` (N-ary) |
| `sub_DBFF60` | `getAddRecExpr` |
| `sub_DCB270` | `getUDivExpr` |
| `sub_DC5000` | `getZeroExtendExpr` |
| `sub_DC2B70` | `getSignExtendExpr` |
| `sub_DC5200` | `getTruncateExpr` |
| `sub_DD3A70` | `getPtrToIntExpr` |
| `sub_B19D00` | `DominatorTree::dominates` |
| `sub_C8CC70` | `SmallDenseSet::insert` |
| `sub_DB11F0` | Cache insert (delinearization result memoization) |

## Cross-References

- [ScalarEvolution Overview & Construction](./scev.md) -- SCEV expression creation, the ValueExprMap, and the expression DAG structure that invalidation walks
- [SCEV Range Analysis & Trip Counts](./scev-range-btc.md) -- range caches and BTC caches that invalidation must clear; the `getRangeRef` and BTC computation functions called during eviction
- [LoopVectorize & VPlan](./loop-vectorize.md) -- primary consumer of delinearization results for vectorization legality; calls `forgetLoop` after vectorizing
- [Loop Unrolling](./loop-unroll.md) -- calls `forgetLoop` after unrolling; the `forget-scev-loop-unroll` knob controls this
- [Loop Strength Reduction (NVIDIA)](./lsr.md) -- uses SCEV for IV analysis; its transformations trigger `forgetValue` calls
- [MemorySpaceOpt](../passes/memory-space-opt.md) -- NVIDIA-specific pass that triggers SCEV invalidation after address space transformations
- [Alias Analysis & NVVM AA](../infra/alias-analysis.md) -- delinearization results feed into alias analysis for disambiguating multi-dimensional array accesses
