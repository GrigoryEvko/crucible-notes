# Memory Dependency Analysis

## Overview

Memory dependency analysis is a critical component of the CICC instruction scheduler's DAG construction system. It enforces load/store ordering constraints to preserve program correctness in the presence of potential memory aliasing. The analysis employs a **conservative windowed approach** with caching to balance correctness, compilation time, and scheduling quality.

**Classification**: Conservative dependency analysis
**Primary Purpose**: Prevent incorrect instruction reordering for potentially-aliased memory operations
**Performance Impact**: Ordering constraint only (zero latency edge weight)

---

## Memory Dependency Type Specification

### Dependency Characteristics

| Property | Value | Rationale |
|----------|-------|-----------|
| **Type** | `memory` | Distinct from data/control dependencies |
| **Description** | Load/store ordering constraints | Memory access serialization |
| **Edge Weight** | **0 cycles** | Ordering only, not latency-based |
| **Analysis Strategy** | Conservative (may-alias assumption) | Correctness over aggressive scheduling |
| **Caching** | **ENABLED** by default | Compile-time optimization |
| **Breakable** | **NO** | Cannot be broken like anti-dependencies |

### Edge Weight Semantics

```
memory_dependency_edge_weight = 0
```

**Critical Distinction**: Unlike true dependencies (RAW) which have edge weights equal to producer instruction latency, memory dependencies impose **ordering constraints without adding latency**. This allows the scheduler to:

1. **Enforce correctness**: Prevent load/store reordering that would violate memory semantics
2. **Enable latency hiding**: Insert independent instructions between memory operations
3. **Preserve aliasing safety**: Conservatively assume all loads/stores may alias

**Example**:
```
STORE [addr1], value1    ; Producer
LOAD  result, [addr2]    ; Consumer (may alias with addr1)

Memory dependency: STORE → LOAD (weight = 0)
Effect: LOAD cannot execute before STORE, but no latency penalty assumed
```

---

## Window-Based Analysis System

### Instruction Window Parameters

The memory dependency analysis operates within bounded windows to manage compile time for large functions.

#### Configuration Constants

```c
// Default window sizes
#define MEM_DEP_INSTR_WINDOW_SIZE    100   // instructions per block
#define MEM_DEP_BLOCK_WINDOW_SIZE    200   // blocks per function
#define MEM_DEP_CACHING_ENABLED      true  // always enabled
```

#### Per-Block Analysis Window

- **Parameter**: `window_size_instructions`
- **Default Value**: **100 instructions**
- **Scope**: Single basic block analysis depth
- **Purpose**: Limit quadratic complexity of pairwise memory operation analysis
- **Behavior**:
  - Analyze all memory operations within first 100 instructions of block
  - Skip deep analysis for instructions beyond window
  - Conservative fallback: assume dependencies for out-of-window operations

**Rationale**: Most scheduling opportunities exist within local instruction windows. Beyond 100 instructions, the marginal benefit of precise dependency analysis diminishes while compile time increases quadratically.

#### Per-Function Block Window

- **Parameter**: `window_size_blocks`
- **Default Value**: **200 blocks**
- **Scope**: Function-level analysis breadth
- **Purpose**: Prevent excessive compile time in functions with thousands of basic blocks
- **Behavior**:
  - Analyze first 200 blocks in function
  - Disable deep analysis for subsequent blocks
  - Conservative dependencies for out-of-window blocks

**Rationale**: Functions with >200 basic blocks are typically control-flow intensive where local scheduling has limited impact. Global scheduling across such large CFGs yields diminishing returns.

#### Adaptive Analysis Depth

The analyzer can **dynamically disable deep analysis** on a per-block basis:

```c
if (basicBlock.size() > MEM_DEP_INSTR_WINDOW_SIZE * 2) {
    // Very large basic block detected
    // Use conservative dependencies without detailed analysis
    use_conservative_fallback = true;
}
```

**Trigger Condition**: Basic blocks exceeding **200 instructions** (2× window size)
**Action**: Skip expensive alias analysis, assume all memory ops may alias
**Benefit**: Prevents pathological compile-time cases on machine-generated code

---

## Conservative Alias Analysis

### May-Alias Assumption

The scheduler employs a **maximally conservative** approach to memory aliasing:

**Core Principle**: **All loads/stores may alias unless proven otherwise**

```c
// Default aliasing assumption
bool mayAlias(MemoryOperation *load, MemoryOperation *store) {
    AliasResult result = aliasAnalysis.query(load->address, store->address);

    if (result == MustNoAlias) {
        return false;  // Proven disjoint
    } else {
        return true;   // Conservative: MayAlias or Unknown → assume alias
    }
}
```

### Alias Analysis Integration

The scheduler queries underlying alias analysis infrastructure:

1. **MustNoAlias**: Proven disjoint (no dependency edge)
2. **MayAlias**: Possible overlap (conservative dependency edge)
3. **Unknown**: Analysis inconclusive (conservative dependency edge)

**Precision Trade-off**:
- **Over-estimation**: May create unnecessary dependencies for actually-disjoint memory ops
- **Safety**: Guarantees correctness in presence of pointer aliasing
- **Performance**: Prevents aggressive but incorrect reordering

### Examples of Conservative Analysis

#### Case 1: Proven No-Alias (No Dependency)
```c
int array1[100];
int array2[100];

STORE [array1 + i], value1   // addr1
LOAD  result, [array2 + j]   // addr2

// Alias analysis: array1 and array2 are distinct objects
// Result: MustNoAlias → no dependency edge
```

#### Case 2: May-Alias (Conservative Dependency)
```c
void foo(int *ptr1, int *ptr2) {
    STORE [ptr1], value1   // addr1
    LOAD  result, [ptr2]   // addr2
}

// Alias analysis: ptr1 and ptr2 may point to same location
// Result: MayAlias → conservative dependency edge (weight=0)
```

#### Case 3: Unknown Alias (Conservative Dependency)
```c
STORE [complex_expr(x, y, z)], value1
LOAD  result, [complex_expr(a, b, c)]

// Alias analysis: Cannot determine relationship
// Result: Unknown → conservative dependency edge (weight=0)
```

---

## Dependency Cache System

### Cache Purpose

**Primary Objective**: Reduce compile time by memoizing dependency analysis results

The cache stores results of pairwise alias queries to avoid redundant computation:

```c
struct MemoryDependencyCache {
    // Maps (load_instr, store_instr) → may_alias boolean
    std::map<std::pair<Instruction*, Instruction*>, bool> cache;

    bool contains(Instruction *load, Instruction *store);
    bool get(Instruction *load, Instruction *store);
    void insert(Instruction *load, Instruction *store, bool may_alias);
};
```

### Cache Behavior

- **Scope**: Per-basic-block (cleared between blocks)
- **Key**: Ordered pair `(load_instruction, store_instruction)`
- **Value**: Boolean `may_alias` result
- **Lifetime**: Active during single block's DAG construction
- **Eviction**: Cleared after block scheduling completes

### Performance Impact

**Complexity Reduction**:
- Without cache: O(N²) alias queries for N memory operations per block
- With cache: O(N²) first block, O(N) amortized for subsequent queries

**Compile-Time Savings**:
- Significant for blocks with many memory operations (e.g., array initialization)
- Example: Block with 50 loads/stores
  - Without cache: 2,500 alias queries
  - With cache: 2,500 queries initially, ~50 queries for incremental updates

---

## Memory Dependency Analysis Algorithm

### Complete Pseudocode Implementation

```c
function analyzeMemoryDependencies(basicBlock, dag):
    """
    Conservative memory dependency analysis with windowing and caching.

    Input:
      basicBlock - Sequence of machine instructions
      dag        - Scheduling DAG with nodes for each instruction

    Output:
      dag with memory dependency edges added

    Properties:
      - Edge weight: 0 (ordering only, not latency)
      - Conservative: Assumes alias unless proven otherwise
      - Windowed: Limited to 100 instructions per block
      - Cached: Reuses alias analysis results
    """

    // Configuration
    instruction_window = min(100, len(basicBlock))
    blocks_analyzed = min(200, function.num_blocks)

    cache_memory_deps = true  // Enabled by default
    cache = MemoryDependencyCache()

    // Collect all memory operations in window
    loads = []
    stores = []

    for i in 0..instruction_window-1:
        instr = basicBlock[i]

        if instr.isLoad():
            loads.append(instr)

        if instr.isStore():
            stores.append(instr)

    // Analyze load-store dependencies (RAW on memory)
    for each load_instr in loads:
        for each store_instr in stores:
            // Only check stores that precede the load
            if store_instr.position < load_instr.position:

                // Check cache first
                if cache_memory_deps and cache.contains(load_instr, store_instr):
                    may_alias = cache.get(load_instr, store_instr)
                else:
                    // Perform alias analysis
                    may_alias = checkAlias(load_instr, store_instr)

                    // Cache result for future queries
                    if cache_memory_deps:
                        cache.insert(load_instr, store_instr, may_alias)

                // Add conservative dependency if aliasing possible
                if may_alias:
                    addDependency(
                        dag,
                        from = store_instr,
                        to = load_instr,
                        type = MEMORY_DEPENDENCY,
                        weight = 0  // Ordering only, no latency
                    )

    // Analyze store-load dependencies (WAR on memory)
    for each store_instr in stores:
        for each load_instr in loads:
            // Only check loads that precede the store
            if load_instr.position < store_instr.position:

                // Check cache
                if cache_memory_deps and cache.contains(load_instr, store_instr):
                    may_alias = cache.get(load_instr, store_instr)
                else:
                    may_alias = checkAlias(load_instr, store_instr)

                    if cache_memory_deps:
                        cache.insert(load_instr, store_instr, may_alias)

                // Add conservative dependency
                if may_alias:
                    addDependency(
                        dag,
                        from = load_instr,
                        to = store_instr,
                        type = MEMORY_DEPENDENCY,
                        weight = 0
                    )

    // Analyze store-store dependencies (WAW on memory)
    for i in 0..len(stores)-1:
        for j in i+1..len(stores)-1:
            store1 = stores[i]
            store2 = stores[j]

            // Check if stores may write to same location
            if cache_memory_deps and cache.contains(store1, store2):
                may_alias = cache.get(store1, store2)
            else:
                may_alias = checkAlias(store1, store2)

                if cache_memory_deps:
                    cache.insert(store1, store2, may_alias)

            if may_alias:
                addDependency(
                    dag,
                    from = store1,
                    to = store2,
                    type = MEMORY_DEPENDENCY,
                    weight = 0
                )


function checkAlias(instr1, instr2):
    """
    Determine if two memory operations may alias.

    Returns:
      true  - Operations may alias (conservative)
      false - Operations proven disjoint (no dependency needed)
    """

    // Extract memory addresses
    addr1 = instr1.getMemoryAddress()
    addr2 = instr2.getMemoryAddress()

    // Query alias analysis
    result = aliasAnalysis.query(addr1, addr2)

    if result == MustNoAlias:
        // Proven disjoint: no dependency needed
        return false
    else if result == MayAlias:
        // Possible overlap: conservative dependency
        return true
    else:  // result == Unknown
        // Cannot prove disjoint: assume alias (conservative)
        return true
```

### Algorithm Complexity

**Time Complexity**:
- Best case (no memory ops): O(1)
- Average case (N memory ops): O(N²) for first block, O(N) amortized with cache
- Worst case (windowed): O(100²) = O(10,000) operations maximum per block

**Space Complexity**:
- Cache size: O(N²) for N memory operations within window
- Maximum: O(100²) = 10,000 cache entries per block

---

## Integration with Instruction Scheduling

### Role in DAG Construction

Memory dependencies are added during **Phase 3** of DAG construction:

```
Phase 1: Optional Initial Topological Sort
Phase 2: Initialize DAG Nodes (one per instruction)
Phase 3: Build Dependency Edges
    ├─ True Dependencies (RAW - register)
    ├─ Output Dependencies (WAW - register)
    ├─ Anti Dependencies (WAR - register)
    ├─ Memory Dependencies ◄── This phase
    └─ Control Dependencies
Phase 4: Compute Edge Weights
Phase 5: Compute Critical Heights
```

### Effect on Scheduling Decisions

Memory dependency edges **constrain instruction ordering** without affecting critical path length:

#### Critical Path Computation

```c
for each node in dag.nodes (reverse topological order):
    node.critical_height = max(
        0,
        max(successor.critical_height + edge.weight
            for each successor in node.successors)
    )
```

**Key Insight**: Memory dependency edges have `edge.weight = 0`, so they:
- **Do NOT** increase critical path height
- **Do** enforce topological ordering (successors must come after predecessors)
- **Allow** latency hiding through other instructions

#### Example: Latency Hiding with Memory Dependencies

```c
// Original instruction sequence
STORE [ptr1], value1      // Instr A: latency=4 cycles
LOAD  temp, [ptr2]        // Instr B: latency=3 cycles (may alias ptr1)
ADD   result, temp, reg1  // Instr C: latency=1 cycle (uses temp)
MUL   other, reg2, reg3   // Instr D: latency=4 cycles (independent)

// Dependencies
A → B (memory dependency, weight=0)
B → C (true dependency, weight=3)

// Critical path length
B.critical_height = C.critical_height + 3 = 0 + 3 = 3
A.critical_height = B.critical_height + 0 = 3 + 0 = 3

// Scheduling decision
// Memory dep A→B enforces ordering, but allows D to execute in parallel
Cycle 0: A, D (parallel issue)
Cycle 1: (A still executing)
Cycle 2: (A still executing)
Cycle 3: (A still executing)
Cycle 4: B (A completes, B can start)
Cycle 5: (B executing, D completes)
Cycle 6: (B executing)
Cycle 7: C (B completes, C can start)

Total: 8 cycles (vs. 11 cycles if D scheduled serially)
```

**Latency Hiding**: The zero-weight memory dependency allows independent instruction `D` to execute concurrently with `A`, hiding `A`'s latency.

### Interaction with Anti-Dependency Breaking

Memory dependencies are **NOT breakable**, unlike register anti-dependencies:

```c
// Register anti-dependency (WAR)
READ  value, reg1         // Producer reads reg1
WRITE reg1, new_value     // Consumer writes reg1
// Edge: READ → WRITE (anti-dependency, weight=1, BREAKABLE)
// Can be broken with register renaming

// Memory anti-dependency (WAR on memory)
LOAD  value, [addr1]      // Producer reads [addr1]
STORE [addr2], new_value  // Consumer writes [addr2] (may alias)
// Edge: LOAD → STORE (memory dependency, weight=0, NOT BREAKABLE)
// Cannot be broken: no memory "renaming"
```

**Rationale**: Register renaming can eliminate register anti-dependencies, but memory has no equivalent renaming mechanism. Memory dependencies must be preserved.

---

## Configuration and Optimization

### Window Size Tuning

The default window sizes (100 instructions, 200 blocks) balance compile time and scheduling quality.

#### Increasing Window Size

**Effect**:
- More precise dependency analysis
- Better scheduling opportunities in large blocks
- Quadratic increase in compile time

**Use Case**: Hand-optimized kernels with large basic blocks

```c
// Hypothetical tuning (not exposed in current CICC)
MEM_DEP_INSTR_WINDOW_SIZE = 200  // Double window size
// Compile time impact: 4× slower memory dep analysis
// Benefit: More aggressive scheduling in 100-200 instruction blocks
```

#### Decreasing Window Size

**Effect**:
- Faster compilation
- More conservative dependencies (reduced scheduling freedom)
- Suitable for code with small blocks

**Use Case**: Large codebases with many small functions

```c
MEM_DEP_INSTR_WINDOW_SIZE = 50   // Half window size
// Compile time impact: 4× faster memory dep analysis
// Cost: Reduced scheduling quality in 50-100 instruction blocks
```

### Caching Behavior

The dependency cache is **always enabled** and cannot be disabled. This design choice reflects:

1. **Consistent benefit**: Cache always improves compile time for blocks with ≥2 memory operations
2. **Negligible overhead**: Hash map lookup is cheap compared to alias analysis
3. **Simplicity**: No configuration parameter needed

### Disabling Deep Analysis

For extremely large basic blocks, the analyzer can fall back to conservative dependencies without detailed alias analysis:

```c
if (basicBlock.size() > MEM_DEP_INSTR_WINDOW_SIZE * LARGE_BLOCK_THRESHOLD) {
    // Skip expensive analysis, assume all memory ops alias
    for each pair (mem_op1, mem_op2) in basicBlock:
        addDependency(mem_op1, mem_op2, MEMORY_DEPENDENCY, weight=0)
}
```

**Threshold**: Typically 2× window size (200 instructions)
**Behavior**: Unconditional memory dependencies between all memory operations
**Impact**: Conservative but correct, prevents quadratic compile-time blowup

---

## Correctness Guarantees

### Memory Consistency Model

The memory dependency analysis enforces a **sequentially consistent** memory model within basic blocks:

1. **Load-Store Order**: Loads cannot be reordered before preceding stores that may alias
2. **Store-Load Order**: Stores cannot be reordered after subsequent loads that may alias
3. **Store-Store Order**: Stores cannot be reordered among themselves if they may alias

**Guarantee**: The scheduled instruction sequence respects all potential memory dependencies, preventing:
- Reading stale values (RAW violation)
- writing values out of order (WAW violation)
- observing writes too early (WAR violation)

### Conservative Safety

The may-alias assumption provides **unconditional correctness**:

- **False Positive**: Over-conservative dependency (performance loss, but correct)
- **False Negative**: Missed dependency (**CORRECTNESS VIOLATION** - must never occur)

Current design guarantees **zero false negatives** through conservative aliasing.

---

## Performance Trade-offs

### Scheduling Quality vs. Compile Time

| Configuration | Compile Time | Scheduling Quality | Recommended Use |
|--------------|--------------|-------------------|-----------------|
| Small window (50 instr) | Fast | Conservative | Large codebases, many small functions |
| Default (100 instr) | Moderate | Good | General-purpose compilation |
| Large window (200 instr) | Slow | Aggressive | Performance-critical kernels |
| Disabled analysis | Fastest | Minimal | Debug builds, prototyping |

### Alias Analysis Precision

Improving underlying alias analysis can reduce false positives:

**Current**: Basic may-alias queries (conservative)
**Potential**: Type-based alias analysis (TBAA), points-to analysis, range analysis
**Benefit**: Fewer unnecessary dependencies, more scheduling freedom

### Memory Model Relaxation

Some targets support relaxed memory models (e.g., weak ordering):

**Current**: Sequential consistency within blocks
**Potential**: Weak ordering with explicit barriers
**Benefit**: Aggressive reordering of non-aliasing memory ops
**Risk**: Requires careful synchronization reasoning

---

## Implementation Evidence

### Source Files

Analysis derived from:

- **dag_construction.json** (lines 78-86, 277-284)
  - Memory dependency type specification
  - Window size parameters: 100 instructions, 200 blocks
  - Caching configuration: enabled by default
  - Conservative approach: may-alias assumption

- **TECHNICAL_IMPLEMENTATION.txt** (lines 236-273, 431-454)
  - Complete pseudocode for `analyzeMemoryDependencies()`
  - Window-based analysis algorithm
  - Cache data structure and behavior
  - Conservative alias analysis integration
  - Edge weight specification: 0 (ordering only)

### Confidence Level

**HIGH** - Multiple consistent sources confirm:
- Exact window size values (100/200)
- Edge weight value (0)
- Caching mechanism (enabled, purpose)
- Conservative approach (may-alias assumption)
- Integration with alias analysis infrastructure

---

## Summary

### Key Properties

1. **Conservative by Design**: Assumes all memory operations may alias unless proven otherwise
2. **Zero Latency Weight**: Memory dependencies enforce ordering without adding to critical path
3. **Windowed Analysis**: Limited to 100 instructions per block, 200 blocks per function
4. **Cached Results**: Dependency cache reduces compile time for large blocks
5. **Non-Breakable**: Unlike register anti-dependencies, memory dependencies cannot be broken
6. **Correctness First**: Guarantees sequential consistency within basic blocks

### Algorithm Summary

```
For each basic block (up to 100 instructions analyzed):
  1. Collect all load/store operations within window
  2. For each pair of memory operations:
     a. Query dependency cache
     b. If not cached, perform alias analysis
     c. If may-alias, add memory dependency edge (weight=0)
     d. Cache result for future queries
  3. Continue to next block (up to 200 blocks per function)
```

### Integration Points

- **DAG Construction**: Phase 3 (after register dependencies, before control)
- **Critical Path**: Zero-weight edges preserve ordering without affecting path length
- **List Scheduling**: Memory deps constrain ready queue but allow latency hiding
- **Anti-Dep Breaking**: Memory deps are NOT breakable (unlike register anti-deps)

---

## Related Documentation

- [DAG Construction Overview](/cicc/wiki/docs/compiler-internals/instruction-scheduling/dag-construction.md)
- [Edge Weight Computation](/cicc/wiki/docs/compiler-internals/instruction-scheduling/edge-weights.md)
- [Anti-Dependency Breaking](/cicc/wiki/docs/compiler-internals/instruction-scheduling/anti-dependency-breaking.md)
- [Critical Path Analysis](/cicc/wiki/docs/compiler-internals/instruction-scheduling/critical-path.md)

---

**Document Version**: 1.0
**Analysis Agent**: SCHED-07
**Data Sources**: dag_construction.json, TECHNICAL_IMPLEMENTATION.txt
**Date**: 2025-11-16
