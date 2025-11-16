# Data Structures Index

## Structure Inventory

### Core IR Structures

| Structure | Size | Module | Purpose | Details |
|-----------|------|--------|---------|---------|
| IRValueNode | 64 | IR Core | IR instruction representation | [Details](#irvaluenode) |
| SymbolEntry | 128 | Symbol Management | Symbol table entry | [Details](#symbolentry) |
| DAGNode | ~48 | Instruction Scheduler | Scheduling dependency node | [Details](#dagnode) |
| PassMetadata | 16 | Pass Manager | Optimization pass metadata | [Details](#passmetadata) |
| InterferenceNode | 128 | Register Allocator | Graph coloring node | [Details](#interferencenode) |
| Scope | ~256 | Symbol Management | Scope hierarchy node | [Details](#scope) |
| PhiNode | ~32 | SSA Construction | Phi function representation | [Details](#phinode) |
| LoopInfo | ~64 | Loop Analysis | Loop metadata structure | [Details](#loopinfo) |
| TensorCostEntry | ~32 | Instruction Selection | Tensor core cost data | [Details](#tensorcostentry) |

### CUDA-Specific Structures

| Structure | Size | Module | Purpose | Details |
|-----------|------|--------|---------|---------|
| WarpSpecData | ~64 | Warp Specialization | SM90 warp configuration | [Details](#warpspecdata) |
| TMADescriptor | ~128 | Memory Management | SM90+ TMA descriptor | [Details](#tmadescriptor) |
| SparsityMetadata | ~48 | Sparsity Support | SM80+ sparsity pattern | [Details](#sparsitymetadata) |
| BankConflictEntry | ~24 | Bank Conflict Analysis | Conflict detection data | [Details](#bankconflictentry) |

## Size Summary

**Total Structures Documented**: 13
**Size Range**: 16-256 bytes
**Total Memory Footprint** (per compilation unit estimate): ~100KB-1MB

### Largest Structures
1. Scope: ~256 bytes (hierarchical scope management)
2. SymbolEntry: 128 bytes (comprehensive symbol metadata)
3. TMADescriptor: ~128 bytes (SM90+ memory acceleration)
4. InterferenceNode: 128 bytes (register allocation graph)

### Most Frequently Allocated
1. IRValueNode: 64 bytes (1000s per function)
2. DAGNode: ~48 bytes (100s per basic block)
3. PassMetadata: 16 bytes (212 passes per pipeline)
4. SymbolEntry: 128 bytes (100s-1000s per module)

## Memory Allocation

### Pooled Allocation

**IRValueNode Pool**:
- Allocator: `sub_72C930(84)` @ 0x72c930
- Size: 84 bytes (64 base + 20 operand array)
- Alternative: `sub_727670()` (size unknown)
- Frequency: HIGH (per-instruction)

**Symbol Table Hash Buckets**:
- Bucket Count: ~1024 (estimated)
- Load Factor: 0.75 (estimated)
- Collision: Separate chaining
- Allocation: Linked list per bucket

**Scheduling DAG Pool**:
- Per-block allocation
- Nodes: ~48 bytes each
- Edges: Variable weight storage

### Allocation Strategies

| Structure | Strategy | Reason |
|-----------|----------|--------|
| IRValueNode | Pool | High frequency, uniform size |
| SymbolEntry | Hash table | Fast lookup, scoped allocation |
| DAGNode | Per-block pool | Locality, bulk deallocation |
| PassMetadata | Static array | Fixed count (212 passes) |
| InterferenceNode | Graph pool | Graph coloring lifetime |

## Structure Details

### IRValueNode

**Size**: 64 bytes
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/ir_node_exact_layout.json`

**Layout**:
```
Offset 0:  next_use_def        (8B) - Use-def chain pointer
Offset 8:  opcode              (1B) - Operation type
Offset 9:  operand_count       (1B) - Operand count
Offset 10: state_phase         (1B) - Processing phase (1/3/5)
Offset 11: control_flags       (1B) - Traversal control
Offset 12: padding             (4B) - Alignment
Offset 16: type_or_def         (8B) - Type descriptor pointer
Offset 24: value_or_operand    (8B) - Value/operand pointer
Offset 32: next_operand_or_child (8B) - Operand/child link
Offset 40: second_operand      (8B) - Secondary operand
Offset 48: reserved            (8B) - Reserved field
Offset 56: parent_or_context   (8B) - Parent context pointer
```

**Allocators**:
- `sub_72C930(84)` @ 0x72c930 - 84-byte allocation
- `sub_727670()` @ 0x727670 - Base allocation
- `sub_7276D0()` @ 0x7276d0 - Operand node
- `sub_724D80(n)` @ 0x724d80 - Attribute node

**Key Fields**:
- Offset 0: Intrusive linked list for use-def chains
- Offset 8: Opcode values observed: 19, 84
- Offset 10: State phases: 1 (initial), 3 (processed), 5 (complete)
- Offset 11: Control flags: 0x02 (break), 0x10 (skip), 0x80 (special)

**Binary Addresses**:
- Creation: lines 2979-3010 in `sub_672A20_0x672a20.c`
- Traversal: lines 1885-1903 in `sub_672A20_0x672a20.c`

### SymbolEntry

**Size**: 128 bytes
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/symbol_table_exact.json`

**Layout**:
```
Offset 0:   next_in_bucket       (8B) - Collision chain
Offset 8:   symbol_name          (8B) - Name string pointer
Offset 16:  full_qualified_name  (8B) - Qualified name
Offset 24:  symbol_type          (8B) - Type information
Offset 32:  storage_class        (4B) - Storage class enum
Offset 36:  address_or_offset    (8B) - Memory location
Offset 44:  scope_level          (4B) - Nesting depth
Offset 48:  parent_scope         (8B) - Parent scope pointer
Offset 56:  defining_scope       (8B) - Definition scope
Offset 64:  initialization_expr  (8B) - Initial value
Offset 72:  attributes           (4B) - Attribute bitfield
Offset 76:  line_number          (4B) - Source line
Offset 80:  file_index           (4B) - Source file
Offset 84:  cuda_memory_space    (1B) - Memory space enum
Offset 85:  is_cuda_kernel       (1B) - __global__ flag
Offset 86:  is_cuda_device_func  (1B) - __device__ flag
Offset 87:  forward_declared     (1B) - Forward decl flag
Offset 88:  mangled_name         (8B) - C++ mangled name
Offset 96:  template_args        (8B) - Template arguments
Offset 104: overload_chain       (8B) - Overload link
Offset 112: prev_declaration     (8B) - Previous declaration
Offset 120: reserved             (8B) - Reserved/padding
```

**Hash Table Parameters**:
- Bucket Count: ~1024 (power of 2)
- Hash Function: DJB2 or multiplicative (estimated)
- Collision Resolution: Separate chaining
- Load Factor: 0.75 (resize threshold)

**Storage Classes**:
```
EXTERN = 0
STATIC = 1
AUTO = 2
REGISTER = 3
TYPEDEF = 4
PARAMETER = 5
CUDA_SHARED = 6
CUDA_CONSTANT = 7
CUDA_DEVICE = 8
CUDA_GLOBAL = 9
```

**Binary Addresses**:
- Parser: 0x672A20 (25.8 KB function)
- Semantic: 0x1608300 (17.9 KB function)

### DAGNode

**Size**: ~48 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_scheduling/dag_construction.json`

**Fields** (inferred):
- Instruction pointer
- Predecessor list
- Successor list
- Edge weights (latencies)
- Priority value
- Scheduling state

**Edge Types**:
```
TRUE (RAW):     latency = instruction_latency + penalties
OUTPUT (WAW):   latency = 1 (serialization)
ANTI (WAR):     latency = 1 (breakable)
CONTROL:        latency = 0 (correctness only)
MEMORY:         latency = conservative analysis
```

**Scheduling Algorithms**:
- list-burr @ 0x1d05200 (register reduction)
- source @ 0x1d05510 (source order preserving)
- list-hybrid @ 0x1d05820 (latency + reg pressure)
- list-ilp @ implementation_ref (ILP maximization)

**Configuration**:
- High latency cycles: 25 (default)
- Memory window: 100 instructions, 200 blocks
- Anti-dependency breaking: critical/all/none

### PassMetadata

**Size**: 16 bytes
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/complete_pass_ordering.json`

**Structure**:
```
Offset 0:  pass_handler    (8B) - Function pointer
Offset 8:  pass_options    (8B) - Configuration data
```

**Pass Registry**:
- Total Passes: 212
- Index Range: 0x0A - 0xDD (10-221 decimal)
- Handler Functions: 2
  - `sub_12D6170` @ 0x12d6170 (113 passes, even indices)
  - `sub_12D6240` @ 0x12d6240 (99 passes, odd indices)

**Memory Layout**:
- Output Structure: 3,552 bytes (16 × 222 slots)
- Base Address: a1 parameter
- Pass Registry: a2+120

**Binary Addresses**:
- PassManager: 0x12d6300 (4,786 bytes)
- Configuration: ctor_*_0_*.c files (200+ files)

### InterferenceNode

**Size**: 128 bytes (estimated)
**Alignment**: 16 bytes (SSE)
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/register_allocation/graph_coloring_priority.json`

**Fields** (partial layout):
```
Offset 0:  node_data_ptr     (8B) - Node data pointer
Offset 8:  degree            (8B) - Neighbor count
Offset 16: spill_cost        (4B) - Spill priority
... (additional fields inferred from SIMD processing)
```

**Graph Coloring Parameters**:
- K (physical registers): 15
- Coalesce Factor: 0.8 (0xCCCCCCCCCCCCCCCD / 2^64)
- Briggs Threshold: 14 (K-1)

**Priority Formula**:
```
IF neighbor_low_degree_count >= K:
    priority = INFINITE
ELSE:
    priority = spill_cost / effective_degree
```

**Constraints** (by SM version):
```
Register Classes:
  GPR32: R0-254 (255 registers)
  GPR64: RD0-127 (127 pairs, even alignment)
  PRED:  P0-7 (7 predicate registers)
  H16:   H0-255 (255 half-precision)

SM70-89: 64KB register file
SM90+:   128KB register file
```

**Binary Addresses**:
- Graph Construction: 0xb612d0
- Node Selection: 0x1090bd0
- SimplifyAndColor: 0x1081400
- AssignColors: 0x12e1ef0

### Scope

**Size**: ~256 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/SYMBOL_TABLE_EXTRACTION_REPORT.md`

**Fields**:
```
scope_id               (4B) - Unique ID
scope_type             (4B) - Type enum
parent_scope           (8B) - Enclosing scope
symbol_table           (8B) - Hash table pointer (1024 buckets)
symbol_count           (4B) - Symbol count
scope_depth            (4B) - Nesting level
owning_function        (8B) - Function symbol
owning_class           (8B) - Class symbol
cuda_attrs             (12B) - CUDA flags (kernel/device/shared)
implicit_params        (24B) - Vector of implicit parameters
access_control         (4B) - Access level
... (padding and additional fields)
```

**Scope Types**:
- GLOBAL
- FUNCTION
- BLOCK
- CLASS
- CUDA_KERNEL
- NAMESPACE
- METHOD
- SHARED_MEMORY

**Operations**:
- enter_scope(): O(BUCKET_COUNT)
- exit_scope(): O(BUCKET_COUNT)
- lookup_symbol(): O(1) expected, O(n) worst case
- Scope chain traversal: current → parent → ... → global

### PhiNode

**Size**: ~32 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/ssa_construction/ANALYSIS_SUMMARY.md`

**Fields** (inferred):
```
variable_id        (4B) - SSA variable identifier
block_id           (4B) - Basic block containing phi
incoming_count     (4B) - Number of incoming edges
incoming_values    (8B) - Pointer to value array
incoming_blocks    (8B) - Pointer to block array
```

**Algorithm**: Iterative worklist-based with dominance frontier
- Complexity: O(N × E) where N=blocks, E=DF edges
- Termination: Fixed-point convergence
- Pruning: Pruned SSA (minimal phi placement)

**Binary Addresses**:
- Dominance Frontier: 0x22a3c40 (LLVM IR), 0x37f1a50 (Machine IR)
- Phi Insertion: 0x143c5c0 (LLVM IR), 0x104b550 (Machine IR)
- String: ".phi.trans.insert"

### LoopInfo

**Size**: ~64 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/INDEX.md`

**Fields** (inferred):
```
loop_id            (4B) - Unique loop ID
header_block       (8B) - Loop header pointer
parent_loop        (8B) - Enclosing loop
child_loops        (8B) - Nested loops list
back_edges         (8B) - Back edge list
body_blocks        (8B) - Loop body blocks
nesting_depth      (4B) - Depth level
trip_count         (4B) - Estimated iterations
... (additional metadata)
```

**Detection Algorithm**: Dominator-based natural loop detection
- Back edge: (u, v) where v dominates u
- Nesting depth: parent_depth + 1
- Complexity: O(α(V) × (V + E))

**Integration**:
- LoopSimplify: Canonicalization
- LICM: Code motion
- LoopUnroll: Unrolling
- LoopVectorize: Vectorization
- LoopFusion: Fusion
- LoopInterchange: Interchange

### TensorCostEntry

**Size**: ~32 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/README.md`

**Fields** (inferred):
```
sm_version         (4B) - Target SM (70/80/90/100/120)
instruction_id     (4B) - Tensor instruction ID
precision_type     (4B) - Data type (FP32/FP16/INT8/FP8/FP4)
latency_cycles     (4B) - Instruction latency
throughput         (4B) - Operations per cycle
op_density         (4B) - Ops per instruction
sparsity_factor    (4B) - Cost reduction (2:4 or dynamic)
flags              (4B) - Feature flags
```

**Latency by SM**:
```
SM70 (Volta):       2-8 cycles (WMMA)
SM80 (Ampere):      4 cycles (mma.sync)
SM90 (Hopper):      3 cycles (warpgroup_mma)
SM100 (Blackwell):  2 cycles (tcgen05)
```

**Throughput by Precision**:
```
SM70/80: 1.0 ops/cycle (all)
SM90:    0.5-1.0 ops/cycle
SM100:   FP32/TF32/FP16: 1.0
         INT8/FP8:       2.0
         INT4/FP4:       4.0
```

**Instruction Ranges**:
```
SM70 WMMA:     678-744 (67 variants)
SM80 MMA:      40+ variants
SM90 Enhanced: TMA integration
SM100 TCGen05: alloc/dealloc/commit/fence/wait
```

### WarpSpecData

**Size**: ~64 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/WARP_SPECIALIZATION_SM90_ANALYSIS.md`

**Fields** (SM90+ only):
```
warp_id            (4B) - Warp identifier
role               (4B) - Producer/Consumer/Hybrid
thread_group_size  (4B) - Threads per role
sync_method        (4B) - Synchronization type
shared_mem_offset  (8B) - Shared memory region
barrier_id         (4B) - Barrier identifier
priority           (4B) - Scheduling priority
... (SM90-specific metadata)
```

**Warp Roles**:
- Producer: Memory fetch, compute prep
- Consumer: Result processing
- Hybrid: Both roles (dynamic)

### TMADescriptor

**Size**: ~128 bytes (estimated)
**Alignment**: 128 bytes (cache line)
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/` (inferred)

**Fields** (SM90+ only):
```
base_address       (8B) - Global memory base
tensor_dims        (16B) - 4D tensor dimensions
strides            (16B) - Dimension strides
block_dims         (16B) - Thread block mapping
swizzle_mode       (4B) - Memory swizzle pattern
cache_policy       (4B) - L2 cache hint
multicast_mask     (4B) - Multicast configuration
... (TMA-specific fields)
```

**Operations**:
- tcgen05.alloc: Descriptor allocation
- tcgen05.dealloc: Descriptor release
- tcgen05.commit: Multicast sync
- tcgen05.fence: Memory fence
- tcgen05.wait: Barrier wait

### SparsityMetadata

**Size**: ~48 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/SPARSITY_EXTRACTION_SUMMARY.md`

**Fields**:
```
pattern_type       (4B) - 2:4 structured (SM80) / dynamic (SM100)
metadata_ptr       (8B) - Sparsity metadata array
density            (4B) - Actual sparsity ratio
cost_reduction     (4B) - Performance multiplier
alignment_req      (4B) - Alignment constraint
... (format-specific fields)
```

**Sparsity Patterns**:
```
SM80/86/89: 2:4 structured (2 non-zero per 4 elements)
SM100/120:  Dynamic sparsity (arbitrary patterns)
```

**Cost Reduction**:
- SM80: 2:4 pattern → 2x theoretical speedup
- SM100: Dynamic → variable speedup

### BankConflictEntry

**Size**: ~24 bytes (estimated)
**Alignment**: 8 bytes
**Location**: `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/BANK_CONFLICT_ANALYSIS_GUIDE.md`

**Fields**:
```
register_id        (4B) - Register number
bank_id            (4B) - Bank assignment (0-31)
conflict_count     (4B) - Simultaneous accesses
penalty_cycles     (4B) - Serialization cost
access_pattern     (8B) - Bitmask of conflicting threads
```

**Bank Configuration**:
- Banks: 32
- Bank Width: 4 bytes
- Penalty Weight: 2.0
- Penalty Cycles: 32 (serialization)

**Detection**:
- Analysis: Conservative over instruction window
- Mitigation: Instruction reordering, register renaming
- Validation: Runtime profiling

## Quick Lookup

| Need to know... | See page... |
|-----------------|-------------|
| IR node field offsets | [IRValueNode](#irvaluenode) |
| Symbol table hash function | [SymbolEntry](#symbolentry) |
| Pattern matching structures | `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/` |
| Scheduling DAG layout | [DAGNode](#dagnode) |
| Register allocator internals | [InterferenceNode](#interferencenode) |
| Pass manager organization | [PassMetadata](#passmetadata) |
| SSA phi node details | [PhiNode](#phinode) |
| Loop detection metadata | [LoopInfo](#loopinfo) |
| Tensor core cost tables | [TensorCostEntry](#tensorcostentry) |
| SM90 warp specialization | [WarpSpecData](#warpspecdata) |
| TMA descriptor layout | [TMADescriptor](#tmadescriptor) |
| Sparsity metadata format | [SparsityMetadata](#sparsitymetadata) |
| Bank conflict detection | [BankConflictEntry](#bankconflictentry) |

## Binary Layout Map

### Key Structure Definitions

| Structure | Constructor/Initializer | Allocation Address | Definition Address |
|-----------|------------------------|-------------------|-------------------|
| IRValueNode | N/A | 0x72c930, 0x727670 | Inferred from usage |
| SymbolEntry | Parser: 0x672a20 | Hash bucket allocations | Inferred from parser |
| DAGNode | ctor_282: 0x4f8f80 | Per-block pools | Scheduler registration |
| PassMetadata | PassManager: 0x12d6300 | Static array | Pass registration |
| InterferenceNode | Graph: 0xb612d0 | Graph construction | Register allocator |

### Function Addresses

**IR Construction**:
- `sub_672A20` @ 0x672a20 (25.8 KB) - Pipeline main, IR creation
- `sub_727670` @ 0x727670 - IR node allocator
- `sub_7276D0` @ 0x7276d0 - Operand allocator
- `sub_724D80` @ 0x724d80 - Attribute allocator
- `sub_72C930` @ 0x72c930 - Extended allocator (84 bytes)

**Symbol Management**:
- Parser @ 0x672a20 - Symbol creation
- Semantic @ 0x1608300 (17.9 KB) - Symbol resolution

**Scheduling**:
- list-burr @ 0x1d05200 - Bottom-up register reduction
- source @ 0x1d05510 - Source order preserving
- list-hybrid @ 0x1d05820 - Hybrid scheduler

**Register Allocation**:
- Graph Construction @ 0xb612d0 - Build interference graph
- SelectNodeForRemoval @ 0x1090bd0 - Node selection (K=15)
- SimplifyAndColor @ 0x1081400 - Coloring loop
- AssignColors @ 0x12e1ef0 - Color assignment

**Pass Management**:
- PassManager @ 0x12d6300 (4,786 bytes) - Pass registration
- Handler 1 @ 0x12d6170 - Metadata handler (113 passes)
- Handler 2 @ 0x12d6240 - Boolean handler (99 passes)

**SSA Construction**:
- Dominance Frontier (IR) @ 0x22a3c40 - LLVM IR level
- Dominance Frontier (Machine) @ 0x37f1a50 - Machine IR level
- Phi Insertion (IR) @ 0x143c5c0 - LLVM IR phi placement
- Phi Insertion (Machine) @ 0x104b550 - Machine IR phi placement

## Cross-References

### Related Analysis Documents

**L3 Data Structures**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/ir_node_exact_layout.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/symbol_table_exact.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/README.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/data_structures/EXTRACTION_SUMMARY.md`

**L3 Register Allocation**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/register_allocation/register_class_constraints.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/register_allocation/graph_coloring_priority.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/register_allocation/spill_cost_formula.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/register_allocation/INDEX.md`

**L3 Instruction Selection**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/tensor_core_costs.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/README.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_selection/CODE_EVIDENCE_INDEX.md`

**L3 Optimization Framework**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/complete_pass_ordering.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/README.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimization_framework/PASS_ANALYSIS_SUMMARY.md`

**L3 SSA Construction**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/ssa_construction/ANALYSIS_SUMMARY.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/ssa_construction/EVIDENCE_DOCUMENTATION.md`

**L3 Instruction Scheduling**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_scheduling/dag_construction.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_scheduling/scheduling_heuristics.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_scheduling/critical_path_detection.json`

**L3 Optimizations**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/INDEX.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/GVN_IMPLEMENTATION_DETAILS.md`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/optimizations/DSE_QUICK_REFERENCE.md`

**L3 CUDA-Specific**:
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/warp_specialization_sm90.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/sparsity_support_sm100.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/bank_conflict_analysis.json`
- `/home/user/nvopen-tools/cicc/deep_analysis/L3/cuda_specific/divergence_analysis_algorithm.json`

### Algorithm References

**Using IRValueNode**:
- SSA Construction (phi placement)
- Dead Code Elimination
- Global Value Numbering
- Constant Propagation
- All IR-level optimizations

**Using SymbolEntry**:
- Name Resolution
- Type Checking
- Scope Analysis
- Cross-Module Linking
- CUDA Kernel Detection

**Using DAGNode**:
- Instruction Scheduling
- Critical Path Analysis
- Latency Hiding
- ILP Maximization
- Register Pressure Reduction

**Using PassMetadata**:
- Pass Pipeline Configuration
- Optimization Level Dispatch (O0-O3)
- Pass Dependency Management
- Analysis Preservation

**Using InterferenceNode**:
- Graph Coloring Register Allocation
- Spill Cost Calculation
- Coalescing Decisions
- SM-Specific Constraint Enforcement

**Using PhiNode**:
- SSA Form Conversion
- Dominance Frontier Computation
- Variable Renaming
- Mem2Reg Transformation

**Using LoopInfo**:
- Loop Invariant Code Motion
- Loop Unrolling
- Loop Vectorization
- Loop Fusion/Interchange
- Trip Count Analysis

**Using TensorCostEntry**:
- Tensor Core Instruction Selection
- SM-Specific Code Generation
- Cost Model Optimization
- Precision Selection

### Navigation

**Main Index**: `/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/README.md`

**Related Pages**:
- [Architecture Detection](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/architecture-detection.md)
- [Compilation Pipeline](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/compilation-pipeline.md)
- [Instruction Selection](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/instruction-selection.md)
- [Optimization Passes](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/optimization-passes.md)
- [Register Allocation](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/register-allocation.md)
- [Tensor Core Codegen](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/tensor-core-codegen.md)
- [Error Handling](/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/error-handling.md)

---

**Analysis Date**: 2025-11-16
**Source**: L3 Deep Analysis - Complete Data Structure Extraction
**Confidence**: HIGH (95%) for documented structures, MEDIUM (70%) for estimated sizes
**Coverage**: 13 core structures across 9 compiler modules
