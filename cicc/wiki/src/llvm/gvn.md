# GVN (Global Value Numbering)

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/Scalar/GVN.cpp`, `llvm/lib/Transforms/Scalar/NewGVN.cpp` (LLVM 20.0.0)

CICC v13.0 ships two GVN implementations: the classic GVN pass at `0x1900BB0` (11.5 KB binary, ~2,314 decomp lines) and a NewGVN pass at `0x19F99A0` (12.3 KB binary, ~2,460 decomp lines). Both are derived from upstream LLVM but carry substantial NVIDIA modifications for GPU-specific value numbering, store splitting, and intrinsic-aware CSE. The knob constructor at `ctor_201` (`0x4E0990`) registers eleven tunables that control PRE, store splitting, PHI removal, dominator caching, and recursion depth.

## Key Facts

| Property | Value |
|---|---|
| Pass name (pipeline) | `gvn` (parameterized) |
| Registration | New PM #397, parameterized: `no-pre;pre;no-load-pre;load-pre;...` |
| Runtime positions | Tier 0 #5 (via `sub_1C6E800`); also appears at NewGVN/GVNHoist position #6; see [Pipeline](pipeline.md) |
| Classic GVN entry | `sub_1900BB0` (11.5 KB binary / 2,314 decomp lines) |
| NewGVN entry | `sub_19F99A0` (12.3 KB binary / 2,460 decomp lines) |
| Knob constructor | `ctor_201` at `0x4E0990` |
| Upstream source | `llvm/lib/Transforms/Scalar/GVN.cpp`, `NewGVN.cpp` (LLVM 20.0.0) |

## Knob Inventory

Knobs are registered in `ctor_201` at `0x4E0990`. Bool knobs use `cl::opt<bool>` (vtable `0x49EEC70`); int knobs use `cl::opt<int>` (vtable `0x49EEB70`). The store-split limit knobs route through a custom NVIDIA registrar at `sub_190BE40` that accepts an `int**` default initializer.

| Knob | Type | Default | Global Address | Purpose |
|------|------|---------|----------------|---------|
| `enable-pre` | bool | true | `0x4FAEEE0` | Enable Partial Redundancy Elimination |
| `enable-load-pre` | bool | true | `0x4FAEE00` | Enable load PRE (load sinking across edges) |
| `enable-split-backedge-in-load-pre` | bool | false | `0x4FAED20` | Allow splitting backedges during load PRE |
| `enable-phi-remove` | int | 2 | `0x4FAEC40` | PHI removal aggressiveness (0=off, 2=aggressive) |
| `dump-phi-remove` | int | 0 | `0x4FAEB60` | Dump PHI removal decisions (debug) |
| `no-split-stores-below` | int | -1 | `0x4FAEA80` | Minimum store width in bits for splitting (-1 = no limit) |
| `no-split-stores-above` | int | -1 | `0x4FAE9A0` | Maximum store width in bits for splitting (-1 = no limit) |
| `split-stores` | bool | true | `0x4FAE8C0` | Master enable for store splitting |
| `profusegvn` | bool | true | `0x4FAE7E0` | Verbose diagnostics via NVIDIA profuse framework |
| `gvn-dom-cache` | bool | true | `0x4FAE700` | Cache dominator tree query results (cache size 32) |
| `max-recurse-depth` | int | 1000 | `0x4FAE620` | Maximum recursion depth during simplification |

## IR Before/After Example

GVN eliminates redundant computations and forwards store values to loads. The following shows a common GPU pattern: a redundant load eliminated via value numbering, and a store-to-load forward.

**Before:**
```llvm
define void @f(ptr addrspace(1) %p, ptr addrspace(1) %q) {
  %a = load float, ptr addrspace(1) %p, align 4
  %b = fmul float %a, 2.0
  %c = load float, ptr addrspace(1) %p, align 4        ; redundant load (same %p, no intervening store)
  %d = fadd float %b, %c
  store float 42.0, ptr addrspace(1) %q, align 4
  %e = load float, ptr addrspace(1) %q, align 4        ; load from location just stored to
  ret void
}
```

**After:**
```llvm
define void @f(ptr addrspace(1) %p, ptr addrspace(1) %q) {
  %a = load float, ptr addrspace(1) %p, align 4
  %b = fmul float %a, 2.0
  ; %c eliminated -- replaced with %a (same value number)
  %d = fadd float %b, %a
  store float 42.0, ptr addrspace(1) %q, align 4
  ; %e eliminated -- forwarded from store (value 42.0)
  ret void
}
```

The second `load` from `%p` is eliminated because GVN assigns it the same value number as `%a`. The `load` from `%q` after the `store` is forwarded directly from the stored constant. On GPU, eliminating memory loads is especially valuable because each avoided `ld.global` saves hundreds of cycles of memory latency.

## Classic GVN Algorithm

The main entry point is `GVN::runOnFunction` at `sub_1900BB0`. The pass object is approximately 600 bytes and carries four scoped hash tables plus a dominator tree reference.

### Pass Object Layout

| Offset | Field | Purpose |
|--------|-------|---------|
| +0 | vtable | Pass vtable pointer |
| +16 | `Function*` | Current function being processed |
| +72 | `MemoryDependenceResults*` | MemDep analysis handle |
| +88 | `DominatorTree*` | Dominator tree |
| +240 | LeaderTable | Hash: value number to canonical leader |
| +392 | StoreExprTable | Hash: store expressions |
| +544 | LoadExprTable | Hash: load expressions |
| +592 | RPO counter | Current block's RPO number |

### Complexity

Let N = number of instructions, B = number of basic blocks, and D = depth of the dominator tree. The classic GVN traversal visits every instruction exactly once during the RPO walk: O(N). Each instruction is hashed (O(1) amortized via the scoped hash tables) and looked up in the leader table (O(1) amortized). Memory dependence queries (`getDependency`) are O(D) per load in the worst case, cached by MemDep to amortize across the function. PRE insertion adds at most O(N) new instructions. Store splitting is bounded by the number of stores times the split factor (controlled by `no-split-stores-below/above`). The `gvn-dom-cache` (size 32) converts repeated dominance queries from O(D) to O(1). PHI removal (`replaceAndErase`) is O(U) per replaced value where U = number of uses. Overall: O(N * D) in the worst case due to dominance queries; O(N) in practice with the dominator cache enabled (default). NewGVN's partition-based algorithm is O(N * alpha(N)) amortized where alpha is the inverse Ackermann function from union-find, though the fixpoint iteration can degrade to O(N^2) on pathological inputs.

### Traversal Strategy

The pass walks the dominator tree in reverse post-order using an explicit segmented stack rather than recursion. The initial allocation is an 8-slot array of segment pointers (`sub_22077B0(64)`), each segment holding 64 pointers (512 bytes). The stack grows by allocating new segments and shrinks by freeing segments when popping past a boundary.

Each dominator tree node is a 136-byte structure (`sub_22077B0(136)`) containing RPO in/out numbers, basic block pointer, child pointers, scope chain links for all four hash tables, an undo list for backtracking, and a visited flag at offset +128.

### Main Processing Loop

For each dominator tree node popped from the stack, the pass:

1. Sets the RPO number from the node's `RPO_in` field.
2. Skips already-visited nodes (checked via the byte at offset +128).
3. Iterates every instruction in the basic block.
4. Attempts `SimplifyInstruction` (`sub_1AE9990`) first; if it succeeds, replaces all uses and erases via `sub_19003A0`.
5. Dispatches on the instruction opcode byte at offset +16:
   - **Case 4 (call/intrinsic):** Classifies purity via bitmask `0x1F133FFE23FFFF`, checks volatility through `sub_1560260` (flag 36), looks up in the LeaderTable via `sub_18FDEE0` (hash) + `sub_18FB980` (compare). Inserts new leaders via `sub_18FEF10`.
   - **Case 79 (load):** Queries memory dependence, checks four NVIDIA intrinsic IDs for special pointer extraction, then attempts store-to-load forwarding or PRE.
   - **Case 114 (store):** Inserts into the StoreExprTable using a 5-element hash key (opcode, type, pointer, value, alignment) via `sub_18FEB70` / `sub_18FFC60`.
   - **Default:** General expression numbering through `sub_13E3350`, with sub-dispatch for branches (opcode 57), loads (54/55), and call-like instructions (78).

## NVIDIA Intrinsic-Aware Value Numbering

The classic GVN recognizes four NVIDIA-specific LLVM intrinsic IDs and extracts their pointer operands with non-standard indices:

| Intrinsic ID | Name | Pointer Operand Index | Semantics |
|---|---|---|---|
| 4057 | `llvm.nvvm.ldu` | `1 - numOperands` | Load from uniform memory; aggressively CSE-able |
| 4085 | `llvm.nvvm.ldg` | `1 - numOperands` | Load via texture/global cache; CSE if same address |
| 4492 | (NVIDIA-specific) | `2 - numOperands` | Variant load with 2-operand pointer extraction |
| 4503 | (NVIDIA-specific) | `2 - numOperands` | Variant load with 2-operand pointer extraction |

These intrinsics bypass the standard volatility checks and use custom operand extraction, allowing CSE of texture and surface loads that upstream LLVM GVN would not touch.

```c
/* NVIDIA intrinsic-aware load value numbering.
 * Called from the case-79 (load) arm of GVN::runOnFunction's main loop
 * (sub_1900BB0). For four NVIDIA intrinsic IDs the pointer operand
 * is at a non-standard index, and the standard volatile check is
 * skipped because these intrinsics are documented side-effect-free. */
Value *valueNumberLoadOrIntrinsic(Instruction *I, GVNState *S) {
    unsigned id = (I->getOpcode() == OPC_CALL)
                ? getIntrinsicId(I)                   /* call->callee->id */
                : 0;
    Value *ptr;
    bool   skip_volatile_check;

    switch (id) {
    case 4057:  /* llvm.nvvm.ldu                   */
    case 4085:  /* llvm.nvvm.ldg                   */
        ptr = getOperand(I, 1 - getNumOperands(I)); /* tail-relative idx */
        skip_volatile_check = true;
        break;
    case 4492:                                       /* NVIDIA variants  */
    case 4503:
        ptr = getOperand(I, 2 - getNumOperands(I));
        skip_volatile_check = true;
        break;
    default:                                         /* standard load    */
        ptr = getPtrOperand(I);
        skip_volatile_check = false;
        break;
    }

    if (!skip_volatile_check && isVolatile(I))       /* sub_1560260 b36  */
        return NULL;                                 /* don't number     */

    /* Build 5-element key: (opcode, type, ptr, alignment, AS-class) and
     * look up in LoadExprTable (offset +544 in the pass object). */
    ExprKey k = { I->getOpcode(), getType(I), ptr,
                  getAlign(I), getAddrSpace(ptr) };
    if (Value *leader = scopedLookup(S->loadExprTable, &k))   /* sub_18FB980 */
        return leader;
    scopedInsert(S->loadExprTable, &k, I);                    /* sub_18FEF10 */
    return I;
}
```

## Scoped Hash Tables

GVN maintains four `ScopedHashTable` instances, pushed on dominator tree entry and popped on exit. The scope teardown at lines 1858-2101 restores the LoadExprTable via the undo list at offset +120, restores the StoreExprTable via the undo list at offset +72, frees the MemDepTable scope through `sub_18FE3A0`, and deallocates the 136-byte dom node.

The hash function (`sub_18FDEE0`, approximately 140 lines) is NVIDIA-modified. For binary ops (opcodes 35-52), it hashes the opcode and operand pointers with canonicalization (smaller pointer first for commutative operations). For comparisons, it includes the predicate. For GEPs (opcodes 86/87), it hashes the entire index sequence via `sub_1597510`. Hash mixing uses the formula `(ptr >> 9) ^ (ptr >> 4)` with XOR combining. The 5-element store expression variant (`sub_18FEB70`) computes:

```c
hash = (v12>>9)^(v12>>4) ^ (v11>>9)^(v11>>4) ^ (v10>>9)^(v10>>4) ^ (37*v13) ^ (v9>>9)^(v9>>4)
```

## Store Splitting

Three knobs control this NVIDIA-specific extension: `split-stores` (master enable), `no-split-stores-below` and `no-split-stores-above` (bit-width bounds, both default -1 meaning unlimited). The custom registrar at `sub_190BE40` handles the limit knobs.

When GVN discovers a store that partially overlaps with a load, it attempts to split the store into sub-stores that individually satisfy dependence constraints. This is critical for GPU code where vector stores (`float4`, `int4`) partially overlap with subsequent scalar loads, texture/surface stores have alignment constraints, and shared memory bank conflicts may favor different store granularities.

The function `sub_18FECC0` classifies store expressions by instruction type: store (54), atomic store (55), shufflevector (58), extractelement (59), and insertelement (82). The shufflevector/extract/insert handling reflects NVIDIA's lowering of vector operations into intermediate forms before GVN runs.

## Dominator Cache

The `gvn-dom-cache` knob (default true, cache size 32) addresses a known performance bottleneck. GVN's dominance queries are O(n * depth) and can become expensive on deeply nested GPU kernels with many divergent branches. The cache stores recent `dominates(A, B)` results keyed by basic block pointer, converting repeated queries to O(1). The working set size of 32 was chosen empirically: GPU kernels typically have moderate dominator tree depth because shared memory parallelism keeps CFGs relatively flat.

## PHI Removal

After GVN identifies equivalent values, some PHI nodes become trivial. The `enable-phi-remove` knob controls aggressiveness: level 0 disables removal, level 1 removes only trivially redundant PHIs, and level 2 (default) removes PHIs that become trivial after leader substitution.

The core `replaceAndErase` routine (`sub_19003A0`, 2.0 KB binary) iterates all uses of a replaced value, checks each PHI-node use for trivial foldability using a `SmallDenseSet` (opcode 23), and employs a 4-way unrolled loop (lines 301-317) for use scanning. This micro-optimization targets the common case of PHIs with many incoming edges after switch lowering or loop unrolling.

## NewGVN

The NewGVN implementation at `sub_19F99A0` (12.3 KB binary, ~2,460 decomp lines) uses congruence classes instead of simple leader tables, following the partition-based algorithm from Karthik Gargi (2002). The pass object stores a congruence class hash table at offset +1400 with count, bucket array, entry count, tombstone count, and bucket count fields.

The algorithm:

1. Builds initial partitions from the RPO-ordered instruction list.
2. For each worklist instruction, queries the current congruence class and computes the new value expression.
3. If the expression maps to a different class, splits the partition.
4. Repeats until fixpoint (no more splits).

Hash table growth is handled by `sub_19F5120`; insert-or-find by `sub_19E6B80`. Congruence class members are sorted (`sub_19F5A00` + `sub_19F6B20`) for efficient merge operations.

## Memory Dependence Integration

GVN interacts with `MemoryDependenceResults` at offset +72 through three key functions:

| Function | Address | Role |
|----------|---------|------|
| `getDependency` | `sub_1422850` | Returns the memory instruction this load depends on |
| `getDominatorTree` | `sub_1423BA0` | Extracts the DomTree from MemDep for dominance queries |
| `properlyDominates` | `sub_1428550` | Tests strict dominance through the MemDep tree |

The replacement safety check (`sub_18FBB40`) returns true immediately when RPO numbers match, and otherwise chains through `getDependency` -> `getIDom` -> `dominates()`.

## Profuse Diagnostics

The `profusegvn` knob (default true) enables verbose output through NVIDIA's custom profuse diagnostic framework, not the standard LLVM `OptimizationRemark` system. When active, diagnostics are emitted at value replacement decisions, store/load expression matches, and PRE insertion decisions. The framework is likely controlled by environment variables such as `CICC_PROFUSE_DIAGNOSTICS`.

## Key Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| Classic GVN entry (`GVN::runOnFunction` analog) | `sub_1900BB0` | 11.5 KB | Main classic GVN pass |
| Replace-uses + erase helper | `sub_19003A0` | 2.0 KB | RAUW + eraseFromParent for replaced values |
| NewGVN entry | `sub_19F99A0` | 12.3 KB | Partition-based congruence-class GVN |
| GVN knob constructor | `ctor_201` (`0x4E0990`) | -- | Registers 11 GVN `cl::opt` knobs |
| Expression hash | `sub_18FDEE0` | 0.7 KB | Hashes opcode + operand pointers (commutative-canonicalized) |
| Expression equality | `sub_18FB980` | 0.4 KB | Structural compare for hash-table key |
| 5-key store-expression hash | `sub_18FEB70` | 0.3 KB | 5-element store key (opcode, type, ptr, val, align) |
| 5-key store-expression insert | `sub_18FFC60` | 0.4 KB | Scoped insert paired with undo list |
| Leader-table insert | `sub_18FEF10` | 0.9 KB | LeaderTable insertion at offset +240 |
| Store-expression classifier | `sub_18FECC0` | 0.5 KB | Tags store/atomic/shufflevector/extract/insert |
| Dominance replacement check | `sub_18FBB40` | 0.2 KB | RPO fast-path + `getDependency`/`dominates()` chain |
| PRE availability analysis | `sub_18FC460` | 0.4 KB | Partial-availability scan over predecessors |
| PRE insertion | `sub_18FF290` | 1.9 KB | Materialize PRE copies and rewire PHIs |
| Large GVN sub-routine | `sub_18F6D00` | 11.3 KB | PRE / load-forwarding worker (largest GVN helper) |
| PHI-related GVN helper | `sub_18FAA90` | 3.3 KB | Trivial-PHI removal and use-rescan |
| Store-splitting implementation | `sub_1906720` | 4.2 KB | Decomposes vector stores against partially-aliased loads |
| Store-split worklist visitor | `sub_1905CD0` | 2.5 KB | Per-store traversal driving the splitter |
| Post-GVN cleanup | `sub_1908A00` | 1.8 KB | Scope teardown and undo-list replay |
| Final GVN cleanup | `sub_190C3B0` | 1.6 KB | Function-exit cleanup (dom-node frees, hash drops) |

## Expression Classification Bitmask

The bitmask `0x1F133FFE23FFFF` classifies opcodes that are safe for value numbering (pure, side-effect-free). It appears eight times in the main function. Bit positions correspond to `(opcode - 35)`, covering standard arithmetic, logical, comparison, and cast operations, plus NVIDIA-specific opcodes in the extended range.

## Multi-Pass Data Flow: SROA / InstCombine / GVN / DSE

These four passes form the core scalar optimization chain in CICC's mid-pipeline. They execute in sequence (often multiple times through the pipeline), with each pass producing IR transformations that create opportunities for the next. The following diagram traces data flow through a single iteration of the chain, showing what each pass produces and what the next pass consumes.

```text
 SROA (Scalar Replacement of Aggregates)
 ========================================
 Input:  IR with aggregate alloca instructions (structs, arrays)
         Example: %s = alloca %struct.float4   -->  lives in .local memory (AS 5)

 +--------------------------------------------------------------+
 | Phase 1: Slice analysis                                      |
 |   Walk all uses of each alloca, build byte-range slices      |
 |   Group non-overlapping slices into partitions               |
 |                                                              |
 | Phase 2: Partition splitting                                 |
 |   Replace each partition with a scalar alloca or SSA value   |
 |   Insert extractvalue/insertvalue for partial accesses       |
 |   Defer trivially-promotable allocas to mem2reg              |
 |                                                              |
 | Produces:                                                    |
 |   - Scalar SSA values replacing aggregate members            |
 |   - Inserted bitcasts, trunc, zext for type mismatches       |
 |   - Dead aggregate allocas (erased)                          |
 |   - GEP chains pointing at sub-fields (now redundant)        |
 +------------------------------+-------------------------------+
                                |
                                | Scalar SSA values with redundant
                                | casts, dead GEPs, identity ops
                                v
 InstCombine (Instruction Combining)
 ========================================
 Input:  Post-SROA IR with redundant instructions

 +--------------------------------------------------------------+
 | 405KB visitor dispatches across 80 opcode cases:             |
 |                                                              |
 | Consumes from SROA:                                          |
 |   - Redundant bitcasts from type-punned accesses             |
 |   - trunc(zext(x)) chains from width mismatches              |
 |   - Dead GEP arithmetic (base + 0)                           |
 |   - Identity selects from conditional stores                 |
 |                                                              |
 | Canonicalization:                                            |
 |   - Constant folding (sub_101E960)                           |
 |   - Algebraic identities: x+0, x*1, x&-1 (sub_F0F270)      |
 |   - Strength reduction: x*2^n -> x<<n (sub_10BA120)         |
 |   - Cast chain collapse: trunc(zext(x)) -> x or smaller     |
 |   - NVIDIA intrinsic folding (sub_1169C30, 11.2 KB)          |
 |   - computeKnownBits propagation (sub_11A7600, 27.5 KB)      |
 |                                                              |
 | Produces:                                                    |
 |   - Canonical instruction forms (const on RHS, etc.)         |
 |   - Simplified expressions (fewer instructions)              |
 |   - Known-bits metadata on values                            |
 |   - Opportunities for value numbering (same expression       |
 |     in different blocks now looks identical)                  |
 +------------------------------+-------------------------------+
                                |
                                | Canonical IR with duplicate
                                | expressions across blocks
                                v
 GVN (Global Value Numbering)
 ========================================
 Input:  Canonicalized IR from InstCombine

 +--------------------------------------------------------------+
 | Traverses dominator tree in RPO with scoped hash tables:     |
 |                                                              |
 | Consumes from InstCombine:                                   |
 |   - Canonical expression forms (enables hash-table matching) |
 |   - Known-bits info (used in SimplifyInstruction)            |
 |   - Folded NVIDIA intrinsics (enables ldu/ldg CSE)           |
 |                                                              |
 | Value numbering:                                             |
 |   - Hash expression: (opcode, type, operands) -> leader      |
 |   - Scoped tables: LeaderTable, StoreExprTable, LoadExprTable|
 |   - NVIDIA ldu/ldg CSE (intrinsics 4057, 4085, 4492, 4503)  |
 |                                                              |
 | Load forwarding:                                             |
 |   - Query MemoryDependenceResults for store->load forwarding |
 |   - Store splitting: float4 store -> scalar float load       |
 |     (NVIDIA extension, controlled by split-stores knob)      |
 |                                                              |
 | PRE (Partial Redundancy Elimination):                        |
 |   - Insert computations at merge points to enable CSE        |
 |   - Load PRE across edges (enable-load-pre)                  |
 |                                                              |
 | Consumes from alias analysis:                                |
 |   - MemoryDependence results (which store feeds which load?) |
 |   - NVVM AA NoAlias answers for cross-address-space pairs    |
 |                                                              |
 | Produces:                                                    |
 |   - Eliminated redundant computations (replaced with leader) |
 |   - Forwarded loads (replaced with stored value)             |
 |   - Trivial PHIs (from leader substitution)                  |
 |   - Dead stores exposed (stored value is never loaded)       |
 +------------------------------+-------------------------------+
                                |
                                | IR with eliminated redundancies,
                                | forwarded loads, exposed dead stores
                                v
 DSE (Dead Store Elimination)
 ========================================
 Input:  Post-GVN IR with dead stores exposed

 +--------------------------------------------------------------+
 | 91KB across three major functions:                           |
 |                                                              |
 | Consumes from GVN:                                           |
 |   - Stores whose values were forwarded to loads (now dead)   |
 |   - Stores to locations that GVN proved are overwritten      |
 |   - Simplified store patterns from PRE insertion             |
 |                                                              |
 | Consumes from alias analysis:                                |
 |   - MemorySSA graph (which stores are visible to which loads)|
 |   - NVVM AA NoAlias (cross-space stores never conflict)      |
 |   - TBAA metadata (type-based aliasing for struct fields)    |
 |                                                              |
 | Dead store detection:                                        |
 |   - Complete overwrite: later store covers same location     |
 |   - Partial overwrite: float4 store then float4 store with   |
 |     overlapping range (72-byte hash table tracking)          |
 |   - Store chain decomposition: aggregate stores decomposed   |
 |     via GEP into element-level dead-store checks             |
 |                                                              |
 | NVIDIA extensions:                                           |
 |   - Partial store forwarding with type conversion            |
 |     (float4 -> float via GEP + load extraction)              |
 |   - Cross-store 6-element dependency records                 |
 |   - CUDA vector type-aware size computation                  |
 |                                                              |
 | Produces:                                                    |
 |   - Eliminated dead stores (fewer memory writes)             |
 |   - Replacement loads for partial forwards                   |
 |   - Reduced memory traffic (critical for GPU bandwidth)      |
 +--------------------------------------------------------------+
```

**Cross-pass data dependency table:**

| Pass | Consumes from predecessor | Produces for successor |
|------|--------------------------|----------------------|
| **SROA** | Aggregate allocas from frontend/inliner | Scalar SSA values, redundant casts/GEPs |
| **InstCombine** | Redundant casts, identity ops from SROA | Canonical expressions, known-bits metadata |
| **GVN** | Canonical forms from InstCombine, MemDep/AA results | Forwarded loads, eliminated redundancies, exposed dead stores |
| **DSE** | Dead stores exposed by GVN, MemorySSA/AA results | Eliminated stores, reduced memory traffic |

**Why this ordering matters for GPU code:** SROA is existential because un-promoted allocas become `.local` memory (200-400 cycle penalty). InstCombine must run before GVN because GVN's hash-table matching requires canonical expression forms -- without InstCombine, `(a + 0)` and `a` would hash differently and miss the CSE opportunity. GVN must run before DSE because GVN's load forwarding is what exposes dead stores: once GVN proves that a load reads a value already available as an SSA register, the store that was keeping that value alive becomes dead. DSE then removes it, reducing the memory write traffic that is the primary bandwidth bottleneck on GPU architectures.

## Optimization Level Behavior

| Level | Classic GVN | NewGVN | PRE | Store Splitting |
|-------|------------|--------|-----|-----------------|
| **O0** | Not run | Not run | N/A | N/A |
| **Ofcmax** | Not run | Not run | N/A | N/A |
| **Ofcmid** | Runs (1 instance) | Not run | Enabled (`enable-pre=true`) | Enabled (`split-stores=true`) |
| **O1** | Runs (1-2 instances in Tier 0/1) | Not run | Enabled | Enabled |
| **O2** | Runs (2-3 instances across Tier 0/1/2) | Not run | Enabled | Enabled |
| **O3** | Runs (2-3 instances, most aggressive inlining exposes more CSE) | Not run | Enabled | Enabled |

GVN is a core mid-pipeline pass that runs at O1 and above. It appears multiple times in the pipeline -- typically once after CGSCC inlining and once in the late scalar cleanup. Each instance benefits from different preceding transformations (inlining, SROA, InstCombine). NewGVN is compiled into the binary but not scheduled in any standard pipeline tier. The `enable-pre` and `enable-load-pre` knobs are both true by default across all levels. See [Optimization Levels](../config/optimization-levels.md) for the complete tier structure.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Store splitting** | Not present; GVN handles stores only for forwarding | Three knobs (`split-stores`, `no-split-stores-below`, `no-split-stores-above`) enable splitting wide vector stores into sub-stores matching load granularity |
| **NVIDIA intrinsic CSE** | No awareness of `nvvm.ldu`, `nvvm.ldg` | Four NVIDIA intrinsic IDs (4057, 4085, 4492, 4503) with custom pointer operand extraction, enabling CSE of texture/global cache loads |
| **Dominator cache** | No caching; dominance queries are O(n * depth) | `gvn-dom-cache` (default true, size 32) caches recent `dominates(A, B)` results for O(1) repeated queries |
| **PHI removal aggressiveness** | Basic trivial PHI cleanup | Three-level `enable-phi-remove` knob (0=off, 1=trivial, 2=aggressive); 4-way unrolled use-scanning loop for PHI-heavy IR |
| **Knob count** | ~4 knobs (`enable-pre`, `enable-load-pre`, `enable-split-backedge-in-load-pre`, `max-recurse-depth`) | 11 knobs including store splitting limits, dominator caching, profuse diagnostics, and PHI removal depth |
| **Diagnostic framework** | Standard `OptimizationRemark` system | `profusegvn` knob (default true) uses NVIDIA's custom profuse diagnostic framework, not LLVM's `ORE` |
| **NewGVN** | Standard partition-based NewGVN | Same algorithm, ships alongside classic GVN at separate address; both carry NVIDIA modifications |

## Diagnostic Strings

All diagnostic strings recovered from the binary. GVN uses NVIDIA's custom profuse diagnostic framework rather than LLVM's `OptimizationRemark` system.

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"profuse for GVN"` | `0x4FAE7E0` (`profusegvn` knob description) | Knob | Knob registration |
| `"enable caching of dom tree nodes"` | `0x4FAE700` (`gvn-dom-cache` knob description) | Knob | Knob registration |
| `"Max recurse depth (default = 1000)"` | `0x4FAE620` (`max-recurse-depth` knob description) | Knob | Knob registration |
| (profuse GVN diagnostic output) | `sub_1909530` (1.1 KB) | Debug | `profusegvn` knob enabled (default true); emits at value replacement, store/load match, and PRE insertion decisions |
| (PHI removal diagnostic output) | `sub_19003A0` region | Debug | `dump-phi-remove` > 0; dumps which PHI nodes are being removed and why |

The `profusegvn` framework follows the same pattern as `profuseinline` -- it is a custom NVIDIA diagnostic channel likely controlled by environment variables such as `CICC_PROFUSE_DIAGNOSTICS`, not the standard LLVM `OptimizationRemark` / `ORE` system. The `dump-phi-remove` knob (default 0) separately enables diagnostic output during PHI removal.

## Allocation Strategy

The 136-byte domtree nodes and 48-byte expression entries use `sub_145CBF0` (`BumpPtrAllocator`) and `sub_22077B0` (malloc wrapper). This careful memory management addresses the potentially large number of expressions produced by heavily unrolled GPU kernels.

## Test This

The following kernel contains redundant loads from the same global address. GVN should eliminate the second load by recognizing it has the same value number as the first.

```cuda
__global__ void gvn_test(const float* __restrict__ in, float* __restrict__ out, int n) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= n) return;

    float a = in[tid];        // first load
    float b = a * 2.0f;
    float c = in[tid];        // redundant -- same address, no intervening store
    float d = c * 3.0f;

    out[tid] = b + d;
}
```

**What to look for in PTX:**
- Only **one** `ld.global.f32` instruction for `in[tid]`, not two. GVN assigns the same value number to both loads (same pointer, no intervening aliasing store thanks to `__restrict__`) and replaces the second with the first's result.
- The arithmetic should reduce to something equivalent to `in[tid] * 5.0f`. After GVN eliminates the redundant load, InstCombine or the backend may simplify `a*2 + a*3` into `a*5`.
- Remove `__restrict__` and add an intervening store (`out[tid] = b;` between the two loads). Without `__restrict__`, GVN cannot prove the second load is redundant (the store to `out` might alias `in`), so both `ld.global.f32` instructions survive. This demonstrates how alias analysis feeds GVN.
- For store-to-load forwarding: insert `out[tid] = 42.0f;` followed by `float e = out[tid];`. GVN should replace the load with the constant `42.0f` -- no `ld.global` emitted for `e`.
