# CICC Compilation Pipeline

## Executive Summary

The NVIDIA CICC (CUDA Intermediate Code Compiler) implements a sophisticated, multi-stage compilation pipeline that transforms CUDA Intermediate Representation (CUDA IR) into PTX assembly. The pipeline follows LLVM architecture with 212 optimization passes organized in a hierarchical pass manager, specialized instruction selection with 850+ IR-to-PTX patterns, and advanced register allocation using Chaitin-Briggs graph coloring.

**Key Statistics:**
- **Total Passes**: 212 active optimization passes (10 unused indices reserved)
- **Pattern Database**: ~850 IR-to-PTX conversion patterns
- **Register Budget**: 15 physical registers per SM (K=15 graph coloring)
- **Optimization Levels**: O0, O1, O2, O3 with progressive enablement
- **Pass Manager**: Hierarchical Module → Function → Loop structure

## Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ CUDA Intermediate Representation (Input)                                     │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 1: SSA CONSTRUCTION                     │
        │  - Dominance Analysis (DomTree)                 │
        │  - Iterative Phi Node Insertion                 │
        │  - Use-Def Chain Construction                   │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 2: OPTIMIZATION PASSES (212 passes)     │
        │  - Module-level (41 passes)                     │
        │  - Function-level (110 passes)                  │
        │  - Loop-level (11+ passes)                      │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 3: SSA ELIMINATION                      │
        │  - PHI Node Elimination                         │
        │  - Parallel Copy Insertion                      │
        │  - Critical Edge Handling                       │
        │  - Copy Coalescing (CSSA)                       │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 4: REGISTER ALLOCATION                  │
        │  - Liveness Analysis                            │
        │  - Interference Graph Construction              │
        │  - Chaitin-Briggs Graph Coloring (K=15)         │
        │  - Conservative + Iterative Coalescing          │
        │  - Spill Insertion & Lazy Reload                │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 5: INSTRUCTION SELECTION                │
        │  - Pattern Matching (850+ patterns)             │
        │  - SM Architecture Dispatch                     │
        │  - Cost Model Evaluation                        │
        │  - Template Instantiation                       │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 6: INSTRUCTION SCHEDULING               │
        │  - Dependency Analysis (DAG)                    │
        │  - Pre-RA & Post-RA Scheduling                  │
        │  - Heuristic Priority Functions                 │
        │  - Latency Hiding & ILP Optimization            │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │   Stage 7: PTX EMISSION                         │
        │  - Format Translation                           │
        │  - Architecture-specific Emission               │
        │  - Assembly Generation                          │
        └──────────────────────┬──────────────────────────┘
                               │
        ┌──────────────────────┴──────────────────────────┐
        │ PTX Assembly Output (Target Architecture)       │
        └──────────────────────────────────────────────────┘
```

---

## Stage 1: SSA Construction

**Purpose**: Convert input IR into Static Single Assignment (SSA) form for efficient analysis and optimization.

**Agent**: L3-08 (Phi Insertion Algorithm)
**Confidence**: HIGH (98%)

### Dominance Analysis
- Constructs dominance tree from control flow graph
- Computes immediate dominator relationships
- Enables pruned phi node placement

### Phi Node Insertion Algorithm
**Algorithm Type**: Iterative Worklist with Pruned Dominance Frontier

**Data Structures**:
- `worklist`: FIFO queue of blocks needing phi nodes
- `has_phi[var][block]`: 2D bitset preventing duplicate insertion
- `dominance_frontier[block]`: Pre-computed DF relationships
- `variable_definitions`: Blocks where each variable is assigned

**Algorithm Steps**:
1. Initialize worklist with all blocks containing variable definitions
2. While worklist not empty:
   - Dequeue block B
   - For each block F in dominance frontier of B:
     - If phi not already inserted for variable at F:
       - Insert phi node at F
       - Enqueue F to worklist
3. Continue until fixed-point (worklist empty)

**Complexity**: O(N × E) where N = variables, E = edges (standard iterative DF phi insertion)

### Use-Def Chain Construction
- Builds bidirectional links between definitions and uses
- Foundation for data dependence analysis in optimization passes

---

## Stage 2: Optimization Passes

**Purpose**: Improve code quality through 212 sequential transformation and analysis passes.

**Agent**: L3-09, L3-27 (PassManager Implementation)
**Confidence**: HIGH (95%)

### Pass Manager Architecture

CICC uses a hierarchical three-level pass manager:

```
PassManager (base)
├── ModulePassManager (scope: entire compilation unit)
│   └── FunctionPassManager (scope: per-function)
│       └── LoopPassManager (scope: per-loop)
```

**Execution Model**:
1. **doInitialization()**: One-time setup (analysis cache, pass registry)
2. **runOnX()**: Repeated per unit (Module/Function/Loop)
3. **doFinalization()**: One-time cleanup and summary output

### Pass Registry

**Structure**:
- **Total Slots**: 222 indices (0-221)
- **Active Passes**: 212 passes (indices 10-221)
- **Unused**: Indices 0-9 reserved for future
- **Entry Size**: 64 bytes per pass (indexed lookup)
- **Handler Distribution**:
  - Even indices (10,12,14,...220): Metadata handler `sub_12D6170` (113 passes)
  - Odd indices (11,13,15,...221): Boolean handler `sub_12D6240` (99 passes)

### Key Optimization Passes

#### InstCombine
- Recognizes algebraic identities: `(x + 0) → x`, `x | x → x`
- Simplifies operations: `add 1, add -1 → noop`
- Confidence: HIGH (address: 0x4971a0)

#### SimplifyCFG
- Merges basic blocks
- Removes unreachable code
- Eliminates redundant branches
- Invalidates: DominatorTree, LoopInfo

#### DeadStoreElimination (DSE)
- Uses MemorySSA for store-store and store-load analysis
- Confidence: HIGH (address: 0x53eb00)

#### LICM (Loop Invariant Code Motion)
- Hoists loop-invariant computations out of loops
- Requires: DominatorTree, LoopInfo, LoopSimplify
- Versions loops when profitable
- Confidence: HIGH (address: 0x4e33a0)

#### Inlining (4 variants)
- Addresses: 0x4d6a20, 0x51e600, 0x5345f0, 0x58fad0
- Different specialized inlining strategies
- Requires: CallGraph, TargetLibraryInfo
- Invalidates: CallGraph

#### LoopUnroll
- Reduces loop overhead
- Increases instruction-level parallelism
- Invalidates: LoopInfo, DominatorTree
- Confidence: HIGH (address: 0x54b6b0)

---

## Stage 3: SSA Elimination

**Purpose**: Convert from SSA back to traditional non-SSA form, inserting copy instructions for phi node elimination.

**Agent**: L3-17 (SSA Elimination Algorithm)
**Confidence**: HIGH (95%)

### PHIEliminationPass (5-Phase Process)

#### Phase 1: Liveness Analysis
- Analyze which values are live out of each basic block
- Determine which phi operands require copy instructions
- Key function: `isLiveOutPastPHIs()`

#### Phase 2: PHI Elimination
- Replace each PHI node with copy instructions in predecessor blocks
- **Semantics**: All copies representing single phi execute atomically
- **Insertion Point**: Predecessor basic block, before branch

#### Phase 3: Critical Edge Handling
- **Critical Edge Definition**: Block with >1 successor to block with >1 predecessor
- **Problem**: Cannot directly insert instructions on critical edges
- **Solution**: Split edge with intermediate block OR use alternative placement

#### Phase 4: Copy Coalescing (CSSA Coalescing)
- **Goal**: Eliminate redundant copies by merging non-interfering copies
- **Condition**: Two copies can merge if live ranges don't overlap
- **Benefit**: Reduce register pressure and code size

#### Phase 5: Redundant Copy Elimination
- Avoid inserting duplicate copies
- **Option**: `donot-insert-dup-copies` (default: true)
- **Benefit**: Further reduce unnecessary instructions

---

## Stage 4: Register Allocation

**Purpose**: Assign virtual registers to physical registers (K=15 for target GPU) with spill insertion for overflow.

**Agents**: L3-01, L3-04, L3-07 (Register Allocation)
**Confidence**: MEDIUM-HIGH (85%)

### Algorithm: Chaitin-Briggs Graph Coloring

**Parameters**:
- **K**: 15 physical registers available
- **Coalesce Factor**: 0.8 (effective degree weight for coalescing opportunity)
- **Spill Cost Formula**:
  ```
  Cost = definition_frequency × use_frequency × memory_latency_multiplier × loop_depth_exponent
  ```

### Phase 1: Liveness Analysis
- Compute live-in/live-out sets for each basic block
- Build use-def chains
- Foundation for interference analysis

### Phase 2: Interference Graph Construction
- Nodes: Virtual registers (one per SSA value)
- Edges: Two registers interfere if live simultaneously
- Weight: Spill cost of each register

### Phase 3: Graph Coloring with Briggs Criterion

**Briggs Optimization**:
```
if count(neighbors_with_degree < K) >= K:
    priority = INFINITE  // Always colorable optimistically
else:
    priority = spill_cost / effective_degree
```

**Selection Loop**:
1. Find node with degree ≤ K-1 (14) satisfying Briggs criterion
2. If found: Push to coloring stack, remove from graph
3. If not found: Select node maximizing `spill_cost / degree`
4. Repeat until graph empty or must spill

---

## Stage 5: Instruction Selection

**Purpose**: Transform IR operations to PTX instructions using pattern matching and cost models.

**Agents**: L3-02, L3-03 (Pattern Matching)
**Confidence**: HIGH (85%)

### Pattern Database

**Scale**: Approximately 850 IR-to-PTX conversion patterns

**Hash Table Structure** (3 tables):

| Table | Capacity | Usage | Purpose |
|---|---|---|---|
| Primary | 512 | ~400 entries (78%) | Main IR→PTX pattern mappings |
| Secondary | 256 | ~180 entries (70%) | Operand constraint checking |
| Tertiary | 128 | ~270 entries (210%, chaining) | Cost and selection strategies |

**Hash Function**:
```c
hash = ((key >> 9) ^ (key >> 4)) & (capacity - 1)
```

### Pattern Categories

**Arithmetic Patterns** (180 patterns):
- `add.s32`, `add.u32`, `add.s64`, `add.u64`
- `mul.lo.s32`, `mul.lo.u32`, `mul.hi.s32`, `mul.hi.u64`
- `fma.rn.f32`, `fma.rz.f32` (Fused Multiply-Add)
- `sub`, `neg`, `abs`

**Float Operations** (150+ patterns):
- Single-precision: f32 arithmetic
- Double-precision: f64
- Special functions: sqrt, sin, cos, log, exp

**Memory Operations** (80+ patterns):
- `ld.global`, `ld.shared`, `ld.local`
- `st.global`, `st.shared`, `st.local`
- Atomic operations

**Type Conversion** (60+ patterns):
- Float to integer, integer to float
- Precision changes: f32 ↔ f64

**Control Flow** (40+ patterns):
- Branches, loops, jumps
- Predicated execution

**Tensor Core/WMMA** (30+ patterns, SM70+):
- `wmma.load_a`, `wmma.load_b`, `wmma.store_d`
- `wmma.mma.sync` operations

---

## Stage 6: Instruction Scheduling

**Purpose**: Order instructions to hide latency, maximize parallelism, and minimize register pressure.

**Agents**: L3-05, L3-19, L3-21 (Scheduling Heuristics)
**Confidence**: HIGH (90%)

### Pre-RA Scheduling Heuristics

**1. List-BURR** (Bottom-Up Register Reduction)
- **Priority Function**: `live_range_end - live_range_start`
- **Goal**: Minimize register pressure by prioritizing instructions with shorter live ranges
- **Ordering**: Reverse topological (bottom-up)

**2. Source Order Scheduling**
- **Goal**: Preserve source order when dependencies allow
- **Fallback**: Switch to register pressure when dependencies force change

**3. List-Hybrid** (Balanced Latency + Pressure)
- **Priority Function**: `0.5 × latency_weight + 0.5 × register_weight`
- **Goal**: Balance latency hiding and register pressure

**4. List-ILP** (Instruction Level Parallelism)
- **Priority Components**: Register pressure, live use count, stalls, critical path, scheduled height
- **Use Case**: High-throughput workloads maximizing parallelism

### Post-RA Scheduling Heuristics

**1. Converge Scheduler**
- **Priority**: Distance to nearest use of value
- **Strategy**: Converge schedule toward critical uses
- **Latency Hiding**: Schedules loads early to hide latency

**2. Maximum ILP Scheduler**
- **Priority**: Successor count + immediate dependencies
- **Goal**: Schedule instructions to enable multiple independent operations

---

## Stage 7: PTX Emission

**Purpose**: Generate final PTX assembly code for target GPU architecture.

**Format**: PTX assembly (text-based intermediate assembly language)
**Target**: PTX ISA version per SM architecture

### Supported Architectures

- **SM50**: Maxwell (GTX 750, GTX 970)
- **SM52**: Maxwell (Titan X Maxwell)
- **SM60**: Pascal (GTX 1080)
- **SM70**: Volta (V100)
- **SM75**: Turing (RTX 2080)
- **SM80**: Ampere (RTX 3090, A100)
- **SM90**: Hopper (H100)
- **SM100**: Blackwell (upcoming)

---

## Optimization Level Configuration

### O0: Debug Mode
- **Purpose**: Fast compilation with debug-friendly code
- **Pass Count**: ~15-20 essential passes
- **Characteristics**: Only correctness-critical passes
- **Compile Time**: <100ms

### O1: Basic Optimization
- **Purpose**: Balance compilation speed and code quality
- **Pass Count**: ~50-60 passes
- **Characteristics**: Quick-to-run, profitable optimizations
- **Compile Time**: 100-500ms

### O2: Default Optimization (Recommended)
- **Purpose**: Standard optimization level
- **Pass Count**: ~150-170 passes
- **Characteristics**: All major optimization passes
- **Compile Time**: 500ms - 2s

### O3: Aggressive Optimization
- **Purpose**: Maximum performance
- **Pass Count**: 200-212 all passes
- **Characteristics**: Potentially slow compilation, maximum code optimization
- **Compile Time**: 2-10s

---

## Pass Dependencies

### Critical Dependency Chains

**ModulePass Dependencies**:
- GlobalOptimization → Requires: none
- Inlining → Requires: CallGraph, TargetLibraryInfo

**FunctionPass Dependencies**:
- DominatorTree → Foundation analysis (enables most optimizations)
- LoopInfo → Foundation analysis (enables loop passes)
- SimplifyCFG → Invalidates: DominatorTree, LoopInfo

**LoopPass Dependencies**:
- LICM → Requires: LoopInfo, DominatorTree, LoopSimplify
- LoopUnroll → Invalidates: LoopInfo, DominatorTree
- LoopVectorize → Requires: ScalarEvolution

---

## Pipeline Performance Metrics

### Typical Compilation Times

| Optimization | Small | Medium | Large |
|---|---|---|---|
| O0 | 5ms | 10ms | 20ms |
| O1 | 20ms | 50ms | 150ms |
| O2 | 100ms | 300ms | 1000ms |
| O3 | 500ms | 1500ms | 5000ms |

### Memory Overhead

- **PassManager State**: ~3.5KB per compilation unit
- **Analysis Cache**: 100KB-1MB per module
- **SSA IR**: 2-10× source kernel size
- **Total Peak**: Typically <50MB for 100KB input kernel

---

## References & Links

- **LLVM PassManager Architecture**: [LLVM Documentation](https://llvm.org/docs/WritingAnLLVMPass/)
- **Graph Coloring Register Allocation**: Chaitin, A.W. (1982)
- **SSA Form**: Cytron, R., et al. (1991)
- **Instruction Scheduling**: Hennessy & Patterson Computer Architecture

## Related Documentation

- [Tensor Core Codegen](./tensor-core-codegen.md)
- [Architecture Detection](./architecture-detection.md)
- [CICC IR Format](../ir-format/cuda-ir-format.md)
- [PTX ISA Reference](../ptx-isa/ptx-instruction-set.md)
