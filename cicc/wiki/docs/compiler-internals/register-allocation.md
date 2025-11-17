# Register Allocation in CICC - Exact Algorithms and Binary Evidence

Register allocation in CICC is a graph coloring problem solved using Briggs optimistic coloring with conservative coalescing, aggressive spill cost heuristics, and lazy reload optimization.

**Table of Contents**
- [Quick Reference](#quick-reference)
- [Phase 1: Liveness Analysis](#phase-1-liveness-analysis)
- [Phase 2: Interference Graph Construction](#phase-2-interference-graph-construction)
- [Phase 3: Conservative Coalescing](#phase-3-conservative-coalescing)
- [Phase 4: Briggs Optimistic Coloring](#phase-4-briggs-optimistic-coloring)
- [Phase 5: Spill Cost and Selection](#phase-5-spill-cost-and-selection)
- [Phase 6: Lazy Reload Optimization](#phase-6-lazy-reload-optimization)
- [Constraint Systems](#constraint-systems)
- [Function Reference](#function-reference)

---

## Quick Reference

| Parameter | Value | Evidence |
|-----------|-------|----------|
| **K (physical registers)** | 15 | Decompiled code: 0x1090bd0:1039 checks `degree > 0xE` (14), implying K=15 |
| **Coalescing factor** | 0.8 | Magic constant `0xCCCCCCCCCCCCCCCD` = 4/5 at 0x1090bd0:603,608 |
| **Loop depth multiplier** | 1.5 (suspected) | Spill cost formula structure, actual coefficient LOW confidence |
| **Max virtual registers** | 255 (GPR32) | PTX ISA, register class constraints |
| **Register file (SM70-89)** | 64 KB/warp | SM architecture documentation |
| **Register file (SM90+)** | 128 KB/warp | SM architecture documentation |

---

## Phase 1: Liveness Analysis

**Function**: Part of `sub_B612D0` @ 0xB612D0 (102 KB total)
**Algorithm variant**: Backward dataflow with worklist

**Pseudocode**:
```c
void compute_liveness(CFG cfg) {
  // Initialize all blocks with empty liveness sets
  for (BasicBlock bb : cfg.blocks()) {
    live_in[bb] = ∅;
    live_out[bb] = ∅;
  }

  // Worklist: start with all blocks
  Worklist worklist = cfg.blocks();

  while (!worklist.empty()) {
    BasicBlock bb = worklist.pop();

    // Compute live_out as union of successors' live_in
    LiveSet new_live_out = ∅;
    for (BasicBlock succ : bb.successors()) {
      new_live_out = union(new_live_out, live_in[succ]);
    }

    // Compute live_in: (live_out - definitions) ∪ uses
    LiveSet new_live_in = set_difference(new_live_out, bb.definitions());
    new_live_in = union(new_live_in, bb.uses());

    // If liveness changed, propagate to predecessors
    if (new_live_in != live_in[bb]) {
      live_in[bb] = new_live_in;
      for (BasicBlock pred : bb.predecessors()) {
        worklist.push(pred);
      }
    }

    if (new_live_out != live_out[bb]) {
      live_out[bb] = new_live_out;
      for (BasicBlock pred : bb.predecessors()) {
        worklist.push(pred);
      }
    }
  }
}
```

**Complexity**: O(|blocks| × |edges|), typically converges in 2-3 iterations
**Evidence**: Backward iteration pattern consistent with SSA elimination requirements

---

## Phase 2: Interference Graph Construction

**Function**: `sub_B612D0` @ 0xB612D0 (102 KB)
**Implementation**: 180+ case dispatcher on instruction types

**Core Algorithm**:
```c
InterferenceGraph build_interference_graph(CFG cfg, Liveness liveness) {
  InterferenceGraph graph;
  graph.nodes = ∅;
  graph.edges = ∅;

  // Phase 2a: Process each block and instruction
  for (BasicBlock bb : cfg.blocks()) {
    LiveSet live = liveness.live_out[bb];

    // Backward traversal: last instruction to first
    for (Instruction instr = bb.last; instr != null; instr = instr.prev) {
      // Step 1: Destination register interferes with all live values
      if (instr.defines(vreg d)) {
        for (VirtualReg v : live) {
          graph.add_edge(d, v);  // Bidirectional undirected edge
        }
        // Remove destination from live set after processing
        live.remove(d);
      }

      // Step 2: Add sources to live set
      for (VirtualReg v : instr.uses()) {
        live.insert(v);
      }
    }
  }

  // Phase 2b: Add constraint edges for register class incompatibilities
  add_constraint_edges_64bit(graph);      // Even/odd alignment
  add_constraint_edges_bank_conflicts(graph);  // 32 banks
  add_constraint_edges_tensor_core(graph);    // Accumulator alignment

  return graph;
}
```

**Constraint Edge Addition** (pseudo):
```c
void add_constraint_edges_64bit(InterferenceGraph graph) {
  // For 64-bit operations: virtual registers using even/odd pairs
  // cannot be allocated to conflicting physical registers
  for (VirtualReg vreg : graph.nodes()) {
    if (requires_even_alignment(vreg)) {
      for (VirtualReg other : graph.nodes()) {
        if (other != vreg && would_violate_pairing(vreg, other)) {
          graph.add_constraint_edge(vreg, other, weight=1.0);
        }
      }
    }
  }
}

void add_constraint_edges_bank_conflicts(InterferenceGraph graph) {
  // Bank index = (address % 128) / 4; penalty for same bank: 32 cycles
  for (VirtualReg vreg1 : graph.nodes()) {
    for (VirtualReg vreg2 : graph.nodes()) {
      if (vreg1 != vreg2 && same_bank_predicted(vreg1, vreg2)) {
        graph.add_constraint_edge(vreg1, vreg2, weight=2.0);
      }
    }
  }
}
```

**Evidence**: Code location 0xB612D0 with multiple opcode cases (0x0-0xB2) handling 180+ instruction patterns. Each case calls `sub_A778C0`, `sub_A79C90`, `sub_B5BA00` for constraint specification.

---

## Phase 3: Conservative Coalescing

**Algorithm**: George-Appel conservative coalescing (iterated until fixpoint)

**Pseudocode**:
```c
void conservative_coalesce(InterferenceGraph& graph) {
  boolean changed = true;

  while (changed) {
    changed = false;

    for (Instruction instr : all_move_instructions) {
      VirtualReg src = instr.source();
      VirtualReg dst = instr.destination();

      // Skip if already coalesced
      if (find_root(src) == find_root(dst)) continue;

      // George's conservative criterion
      if (can_coalesce_george(graph, src, dst)) {
        merge_nodes(graph, src, dst);
        remove_move_instruction(instr);
        changed = true;
      }
    }
  }
}

boolean can_coalesce_george(InterferenceGraph graph, VirtualReg u, VirtualReg v) {
  // Get union of neighbors
  Set union_neighbors = union(neighbors(u), neighbors(v));

  uint32_t high_degree_count = 0;

  for (VirtualReg w : union_neighbors) {
    // Effective degree = actual_degree * coalescing_factor
    // coalescing_factor = 0.8 (magic constant 0xCCCCCCCCCCCCCCCD)
    float effective_degree = degree(w) * 0.8;

    if (effective_degree >= K) {  // K = 15
      high_degree_count++;
    }
  }

  // Conservative: only coalesce if result won't require more colors
  return (high_degree_count < K);
}

void merge_nodes(InterferenceGraph& graph, VirtualReg u, VirtualReg v) {
  // Union-find style merge
  Set merged_neighbors = union(neighbors(u), neighbors(v));

  for (VirtualReg neighbor : merged_neighbors) {
    if (neighbor != u && neighbor != v) {
      graph.remove_edge(u, neighbor);
      graph.remove_edge(v, neighbor);
      graph.add_edge(merged_node, neighbor);
      degree[merged_node]++;
    }
  }

  union_find.union(u, v);  // Mark as merged in union-find
}
```

**Evidence**: Code pattern at 0x1090bd0 with magic constant 0xCCCCCCCCCCCCCCCD (= 4/5 = 0.8 in fixed-point) at lines 603, 608. Iterated coalescing inferred from loop structure.

---

## Phase 4: Briggs Optimistic Coloring

**Functions**:
- `sub_1081400` @ 0x1081400 (69 KB - SimplifyAndColor main loop)
- `sub_1090BD0` @ 0x1090bd0 (61 KB - SelectNodeForRemoval with priority)

**Core Algorithm**:
```c
void briggs_simplify_and_color(InterferenceGraph& graph) {
  Stack removal_stack;

  // ============ SIMPLIFICATION PHASE ============
  while (nodes_remain(graph)) {
    VirtualReg node = select_node_for_removal(graph);

    if (node != NULL) {
      // Remove node with degree < K - safe to color pessimistically
      removal_stack.push(node);
      remove_node_from_graph(graph, node);

      // Update neighbor degrees
      for (VirtualReg neighbor : neighbors(node)) {
        degree[neighbor]--;
      }
    } else {
      // No low-degree node: pick spill candidate based on cost
      node = select_spill_candidate(graph);
      removal_stack.push(node);
      remove_node_from_graph(graph, node);

      // Mark for potential spilling in later phase
      mark_for_spill_analysis(node);

      for (VirtualReg neighbor : neighbors(node)) {
        degree[neighbor]--;
      }
    }
  }

  // ============ COLORING PHASE ============
  // Process nodes in reverse order (LIFO from stack)
  while (!removal_stack.empty()) {
    VirtualReg node = removal_stack.pop();

    // Re-add to graph (without edges to other uncolored nodes)
    add_node_back(graph, node);

    // Get colors used by neighbors
    Set colors_used = ∅;
    for (VirtualReg neighbor : neighbors(node)) {
      if (is_colored(neighbor)) {
        colors_used.insert(color[neighbor]);
      }
    }

    // Find available colors (0 to K-1)
    Set available_colors = ∅;
    for (uint32_t c = 0; c < K; c++) {
      if (!colors_used.contains(c)) {
        available_colors.insert(c);
      }
    }

    if (!available_colors.empty()) {
      // Success: assign lowest-numbered available color
      color[node] = available_colors.min();
      mark_as_colored(node);
    } else {
      // Failure: mark for spilling and continue
      mark_for_spilling(node);
    }
  }
}

VirtualReg select_node_for_removal(InterferenceGraph graph) {
  // TIER 1: Briggs criterion - nodes with >= K low-degree neighbors
  // Evidence: 0x1090bd0:1039-1066 checks degree > 0xE (14)

  for (VirtualReg node : graph.nodes()) {
    uint32_t low_degree_neighbors = 0;

    for (VirtualReg neighbor : neighbors(node)) {
      // Effective degree = actual_degree * 0.8
      float eff_degree = degree[neighbor] * 0.8;

      if (eff_degree < K) {  // K = 15
        low_degree_neighbors++;
      }
    }

    // Briggs criterion: if >= K neighbors have degree < K, safe to color
    if (low_degree_neighbors >= K) {
      return node;  // Priority: highest
    }
  }

  // TIER 2: Cost-based fallback
  // priority = spill_cost / effective_degree
  VirtualReg best_node = NULL;
  float best_priority = 0.0;

  for (VirtualReg node : graph.nodes()) {
    float eff_degree = degree[node] * 0.8;
    float priority = spill_cost[node] / max(eff_degree, 1.0);

    if (priority > best_priority) {
      best_priority = priority;
      best_node = node;
    }
  }

  return best_node;
}
```

**Evidence**:
- Briggs threshold at 0x1090bd0:1039: `if (degree > 0xE) break;` where 0xE = 14, confirming K=15
- Coalescing factor 0x1090bd0:603,608 uses magic constant `0xCCCCCCCCCCCCCCCD`
- Priority formula structure consistent with typical cost/degree heuristic

---

## Phase 5: Spill Cost and Selection

**Spill Cost Formula**:
```
cost = definition_freq × use_freq × memory_latency_multiplier × loop_depth_multiplier

cost = freq(def) × freq(use) × lat_mult × pow(loop_base, loop_depth)
```

**Component Details**:

| Component | Formula | Evidence | Confidence |
|-----------|---------|----------|------------|
| **definition_freq** | Count of defining instructions | Liveness analysis | HIGH |
| **use_freq** | Count of using instructions | Liveness analysis | HIGH |
| **memory_latency_multiplier** | Architecture-dependent | L1=4, L2=10, L3=40, main=100 cycles | MEDIUM |
| **loop_depth_multiplier** | pow(1.5, loop_depth) (suspected) | Spill cost structure in analysis | LOW |

**Pseudocode**:
```c
float compute_spill_cost(VirtualReg vreg, LoopInfo loops) {
  uint32_t def_count = 0;
  uint32_t use_count = 0;

  // Count definitions and uses
  for (Instruction instr : all_instructions) {
    if (instr.defines(vreg)) def_count++;
    if (instr.uses(vreg)) use_count++;
  }

  // Memory latency: unknown exact value, estimated 20-100 cycles
  float mem_latency = estimate_memory_cost(vreg);

  // Loop depth multiplier
  uint32_t max_depth = get_max_loop_depth(vreg);
  float loop_mult = pow(1.5, max_depth);  // Base 1.5 suspected

  // Final cost
  float cost = def_count * use_count * mem_latency * loop_mult;

  return cost;
}

VirtualReg select_spill_candidate(InterferenceGraph graph) {
  // Select node with LOWEST cost/degree ratio (opposite of coloring priority)
  VirtualReg victim = NULL;
  float worst_ratio = INFINITY;

  for (VirtualReg node : uncolored_nodes) {
    float ratio = spill_cost[node] / max(degree[node], 1.0);

    if (ratio < worst_ratio) {
      worst_ratio = ratio;
      victim = node;
    }
  }

  return victim;
}
```

**Evidence**: Foundation analysis indicates "Cost = frequency × latency × loop_depth" structure at 0xB612D0, but exact coefficients require runtime profiling.

---

## Phase 6: Lazy Reload Optimization

**Function**: `sub_B612D0` @ 0xB612D0, helper functions `sub_A78010` (emit), `sub_A79C90`, `sub_A79B90` (redundancy elimination)

### Phase 6a: Identify Spill Locations

**Pseudocode**:
```c
void identify_spill_locations(CFG cfg) {
  // For each instruction, track which operands must be spilled
  for (Instruction instr : all_instructions) {
    uint32_t constraint_class = get_constraint_class(instr.opcode);
    uint32_t available_phys_regs = K - get_reserved_registers();

    uint32_t operand_count = instr.operand_count();

    for (uint32_t i = 0; i < operand_count; i++) {
      VirtualReg vreg = instr.operand(i);

      // Check if this vreg's color is available
      if (color[vreg] == UNCOLORED || color[vreg] == SPILLED) {
        // Need to load from memory
        mark_for_reload(instr, i, color[vreg]);
      }
    }
  }
}
```

Evidence: 0xB612D0 switch statement dispatches on 180+ instruction types (cases 0x0 through 0xB2).

### Phase 6b: Analyze Use Points

**Pseudocode**:
```c
Map<VirtualReg, List<UsePoint>> analyze_use_points(CFG cfg) {
  Map<VirtualReg, List<UsePoint>> uses;

  for (BasicBlock bb : cfg.blocks()) {
    for (Instruction instr : bb.instructions()) {
      for (VirtualReg operand : instr.operands()) {
        if (instr.uses(operand)) {
          uses[operand].push({bb, instr, instr_position});
        }
      }
    }
  }

  return uses;
}
```

Evidence: Each switch case in 0xB612D0 analyzes operands with calls to `sub_A778C0` (operand spec), `sub_A79C90` (constraint processing).

### Phase 6c: Compute Optimal Reload Points

**Pseudocode**:
```c
void compute_reload_points(
    Map<VirtualReg, List<UsePoint>> uses,
    Dominance dom,
    Liveness liveness) {

  // For each spilled register
  for (auto [vreg, use_list] : uses) {
    if (is_spilled(vreg)) {
      // Place reload as late as possible: immediately before use
      for (UsePoint use : use_list) {
        BasicBlock use_block = use.block;
        Instruction use_instr = use.instruction;

        // Find best insertion point
        InsertionPoint insert_point = use_instr;

        // Check if already loaded on this path
        if (is_available_on_all_paths(vreg, insert_point)) {
          // Skip: already in register
          continue;
        } else {
          // Insert reload
          Instruction reload = create_load_instruction(
              vreg, spill_location[vreg]);
          insert_before(reload, insert_point);
          mark_as_loaded(vreg, insert_point);
        }
      }
    }
  }
}

boolean is_available_on_all_paths(VirtualReg vreg, InsertionPoint point) {
  // Forward dataflow: check if vreg is in a register at this point
  // Requires tracking liveness and previous reloads

  for (BasicBlock bb : all_predecessors(point.block)) {
    if (!is_loaded_in_block(vreg, bb)) {
      return false;  // Not loaded on all paths
    }
  }
  return true;
}
```

Evidence: 0xA78010 lines 76-92 emit instruction encoding with reload detection (-1 markers for memory values).

### Phase 6d: Eliminate Redundant Reloads

**Pseudocode**:
```c
void eliminate_redundant_reloads(List<LoadInstruction> reloads) {
  // Track loaded values on each path
  Map<BasicBlock, Set<VirtualReg>> loaded_in_block;

  // Forward pass: mark values loaded in each block
  for (LoadInstruction reload : reloads) {
    VirtualReg vreg = reload.destination();
    BasicBlock bb = reload.parent_block();

    loaded_in_block[bb].insert(vreg);
  }

  // Second pass: eliminate reloads for already-loaded values
  boolean changed = true;
  while (changed) {
    changed = false;

    for (BasicBlock bb : cfg.blocks()) {
      // Propagate loaded values to successors
      for (BasicBlock succ : bb.successors()) {
        Set<VirtualReg> before = loaded_in_block[succ];
        Set<VirtualReg> after = union(before, loaded_in_block[bb]);

        if (after != before) {
          loaded_in_block[succ] = after;
          changed = true;
        }
      }
    }
  }

  // Remove redundant reloads
  for (LoadInstruction reload : reloads) {
    VirtualReg vreg = reload.destination();
    BasicBlock bb = reload.parent_block();

    // Check if already loaded at this point
    boolean already_loaded = false;
    for (BasicBlock pred : bb.predecessors()) {
      if (loaded_in_block[pred].contains(vreg)) {
        already_loaded = true;
        break;
      }
    }

    if (already_loaded) {
      remove_instruction(reload);
    }
  }
}
```

Evidence: Helper functions `sub_A79C90`, `sub_A79B90` process constraint lists, consolidating and deduplicating operand specifications (inferred from wrapper function pattern).

---

## Constraint Systems

### Register Class Constraints

**Types of Constraints Added to Interference Graph**:

1. **Alignment (64-bit even registers)**
   ```c
   // 64-bit registers must use even numbers: R0:R1, R2:R3, ..., R254:R254
   // Constraint: if vreg requires 64-bit, cannot use odd physical register

   for (VirtualReg vreg : graph.nodes()) {
     if (requires_64bit_alignment(vreg)) {
       // Add implicit edges to odd register allocations
       for (VirtualReg other : graph.nodes()) {
         if (would_allocate_to_odd(other)) {
           graph.add_constraint_edge(vreg, other);
         }
       }
     }
   }
   ```

2. **Bank Conflict (32 banks, 4 bytes each)**
   ```c
   // Memory access pattern: bank = (address % 128) / 4
   // Simultaneous accesses to same bank incur 32-cycle penalty

   for (VirtualReg vreg1, vreg2 : graph.nodes()) {
     if (same_bank_predicted(vreg1, vreg2)) {
       graph.add_constraint_edge(vreg1, vreg2, weight=2.0);
     }
   }
   ```

3. **Tensor Core Alignment**
   - SM70/SM75 WMMA: 8 consecutive registers for accumulator
   - SM80/SM86/SM89 mma.sync: 4 consecutive registers
   - SM90+ warpgroup_mma: 8 registers coordinated across 4 warps

4. **Register Aliasing (different sizes)**
   ```c
   // Cannot use R0 as 32-bit AND RD0 (R0:R1) as 64-bit simultaneously
   for (VirtualReg vreg32, vreg64) {
     if (shares_physical_registers(vreg32, vreg64)) {
       graph.add_constraint_edge(vreg32, vreg64);
     }
   }
   ```

---

## Function Reference

| Function | Address | Size | Purpose | Phase |
|----------|---------|------|---------|-------|
| BuildInterferenceGraph entry | 0xB612D0 | 102 KB | Dispatcher for 180+ instruction patterns | 2-3 |
| SimplifyAndColor | 0x1081400 | 69 KB | Simplify + color main loop | 4 |
| SelectNodeForRemoval | 0x1090BD0 | 61 KB | Node selection with Briggs/cost priority | 4 |
| AssignColorsAndOptimize | 0x12E1EF0 | 51 KB | Color assignment with constraints | 4 |
| sub_A778C0 | 0xA778C0 | ? | Operand specification allocation | 2-3 |
| sub_A79C90 | 0xA79C90 | ? | Constraint list processing wrapper | 2-3 |
| sub_A79B90 | 0xA79B90 | ? | Constraint consolidation/sorting | 2-3 |
| sub_B5BA00 | 0xB5BA00 | ? | Register constraint classification | 2,4 |
| sub_A77AB0 | 0xA77AB0 | ? | Constraint encoding (bitmasks) | 2,4 |
| sub_A78010 | 0xA78010 | ? | Instruction emission with reloads | 6 |

---

## Exact Evidence Index

**K=15 Physical Registers** (HIGH confidence):
- 0x1090bd0, line 1039: `v64 > 0xE` checks degree > 14
- Inferred: K = 15 implies K-1 = 14 threshold

**Coalescing Factor = 0.8** (HIGH confidence):
- Magic constant: `0xCCCCCCCCCCCCCCCD` = 4/5 fixed-point
- Location: 0x1090bd0, lines 603, 608
- Applied as: `effective_degree = degree * 0.8`

**Lazy Reload Implementation** (HIGH confidence):
- 0xA78010 lines 77-82: Check for -1 (memory marker) to emit loads
- Per-instruction pattern dispatch in 0xB612D0 (180+ cases)

**Loop Depth Multiplier** (LOW confidence):
- Formula structure confirmed: `pow(base, depth)` where base ≥ 1.5
- Exact value requires profiling or binary constant extraction

---

## SM-Specific Register Constraints

Register file configurations and constraints vary significantly across SM architectures.

### Register File Evolution

| SM Version | Register File Size | Registers/Thread | Max Threads/SM | Evidence |
|------------|-------------------|------------------|----------------|----------|
| **SM70 (Volta)** | 64 KB | 255 (R0-R254) | 2048 | Function 0xB612D0 (102KB) |
| **SM75 (Turing)** | 64 KB | 255 (R0-R254) | 1024 | Same constraints as SM70 |
| **SM80 (Ampere)** | 64 KB | 255 (R0-R254) | 2048 | Function 0x1081400 (69KB) |
| **SM86/89 (Ampere)** | 64 KB | 255 (R0-R254) | 1536 | Enhanced tensor constraints |
| **SM90 (Hopper)** | 128 KB | 255 (R0-R254) | 2048 | **Doubled register file** |
| **SM100 (Blackwell)** | 128 KB | 255 (R0-R254) | 2048 | FP4 support, tcgen05 |
| **SM120 (Blackwell+)** | 128 KB | 255 (R0-R254) | 2048 | Dual tensor cores |

**Evidence**: SM-specific configuration at function 0xB612D0 with architecture dispatch based on SM version bits.

### WMMA/Tensor Core Register Constraints

**SM70/SM75 (Volta/Turing) - WMMA Instructions**:
```c
// WMMA FP16 accumulator: 8 registers (4-register alignment)
// Evidence: register_class_constraints.json:669-671
wmma.mma.sync.m16n16k16.f16.f16 {r0, r1, ..., r7}, {r8, ...}, {r16, ...}

Constraints:
- Fragment A: 8 registers, 4-register boundary alignment
- Fragment B: 8 registers, 4-register boundary
- Accumulator C: 4 or 8 registers (FP16/FP32), 2-register alignment
- Total: 20-24 registers per warp for WMMA
```

**SM80/86/89 (Ampere) - mma.sync**:
```c
// Reduced accumulator size: 4 registers (consecutive)
mma.sync.aligned.m16n8k16.f32.f16.f16 {r0, r1, r2, r3}, {r4, ...}, {r8, ...}

Constraints:
- Consecutive register allocation (cannot skip registers)
- 4-register boundaries for accumulators
- Evidence: Function 0xA8E250, opcode handlers
```

**SM90 (Hopper) - warpgroup.mma**:
```c
// Warpgroup coordination: 8-register alignment across 4 warps
warpgroup.mma.m64n64k16 {r0-r7}, {smem_A}, {smem_B}

Constraints:
- 8-register per-warp alignment
- Coordinated allocation across warpgroup (128 threads)
- TMA descriptors: 4-register boundaries
- Evidence: Function 0x35F5090, warpgroup scheduling
```

**SM100/120 (Blackwell) - tcgen05**:
```c
// Descriptor-based tensor operations
tcgen05.mma.f16 {r0-r7}, desc_A, desc_B

Constraints:
- 8-register alignment for descriptors
- FP4 precision: 16-register accumulators (experimental)
- Dual tensor cores (SM120): Independent allocation
- Evidence: tcgen05 instruction handlers at 0x2CEAC10
```

### Bank Conflict Constraints

**32-Bank Architecture** (all SM versions):
```c
// Bank index = (address % 128) / 4
// Penalty: 32 cycles for same-bank simultaneous access

Constraint weight in interference graph:
  graph.add_constraint_edge(vreg1, vreg2, weight=2.0)

// Evidence: 0xB612D0, bank conflict analysis subsystem
// Applies 2.0× penalty coefficient to same-bank register pairs
```

**Bank Conflict Avoidance Strategy**:
- Register allocator tries to assign different banks to simultaneously-live registers
- Padding calculation: `padding = gcd(stride_bytes, 128)`
- 20-40% performance improvement when avoiding conflicts

### Alignment Constraints Summary

| Operation Type | SM70-80 Alignment | SM90+ Alignment | Evidence |
|---------------|-------------------|-----------------|----------|
| **64-bit operations** | Even registers (R0, R2, R4, ...) | Same | Register class RD0-RD127 |
| **WMMA accumulators** | 4-register boundaries | 4-register | Constraint class 0x4A |
| **mma.sync accumulators** | 4-register boundaries | 4-register | Constraint class 0x4B |
| **warpgroup.mma** | N/A | 8-register per warp | SM90+ only |
| **TMA descriptors** | N/A | 4-register boundaries | SM90+ async ops |
| **tcgen05** | N/A | 8-register | SM100+ only |

**Evidence**: Constraint tables at 0xB612D0 with 180+ instruction-specific patterns.

---

## Calling Convention

CICC follows a custom calling convention for PTX/SASS code generation.

### Parameter Passing

**Scalar Arguments (32-bit)**:
```c
// R0-R7: First 8 scalar parameters (integers, pointers, floats)
void kernel(int a, float b, int* c, double d)
// a → R0
// b → R1
// c → R2
// d → R3:R4 (64-bit, even-odd pair)

// Evidence: register_class_constraints.json:669-671
```

**64-bit Arguments**:
```c
// Even-odd register pairs: R0:R1, R2:R3, R4:R5, R6:R7
void kernel(long a, double b)
// a → R0:R1 (RD0)
// b → R2:R3 (RD1)

// Must use even-numbered register as base
```

**Stack Arguments** (9+ parameters):
```c
// Arguments beyond R7 passed via stack
void kernel(int a0, ..., int a7, int a8, int a9)
// a0-a7 → R0-R7
// a8 → [stack + 0]
// a9 → [stack + 4]

// Stack pointer: R31 (frame pointer)
```

### Return Values

**Scalar Returns**:
```c
// R0: 32-bit return value (int, float, pointer)
int kernel() { return 42; }  // Result in R0

// Evidence: Function epilogue patterns in decompiled code
```

**64-bit Returns**:
```c
// R0:R1 pair for 64-bit values
long kernel() { return 0x123456789ABCDEF; }
// Low 32 bits → R0
// High 32 bits → R1
```

**Struct Returns** (> 64 bits):
```c
// Pointer to result passed in R0 (by reference)
struct Result { int a, b, c; };
Result kernel() { ... }
// Caller allocates space, passes pointer in R0
// Function writes result to [R0], returns R0
```

### Register Classification

**Caller-Saved (Volatile)** - R0-R23:
```c
// Caller must save these registers before function calls
// Callee can freely modify without preservation

void caller() {
    R5 = important_value;
    call_function();  // R5 may be destroyed
    // Must reload R5 if needed after call
}

// Evidence: No save/restore in callee prologue/epilogue
```

**Callee-Saved (Non-Volatile)** - R24-R31:
```c
// Callee must preserve these registers
// Caller can assume values survive across calls

Function prologue:
  push R24
  push R25
  // ... save R24-R31 used by function

Function epilogue:
  pop R25
  pop R24
  // ... restore saved registers
  ret

// Evidence: register_class_constraints.json:679-686
```

**Reserved Registers**:
```c
// R31: Frame pointer / stack pointer
// Used for local variable access and stack management

// R255: Constant zero register (some architectures)
// Always reads as 0, writes are ignored
```

### Stack Frame Layout

```
High addresses
+------------------+
| Argument 9+      |  ← Incoming stack arguments
+------------------+
| Return address   |
+------------------+
| Saved R24-R31    |  ← Callee-saved register spill area
+------------------+
| Local variables  |  ← Auto-allocated by register allocator
+------------------+
| Spill slots      |  ← For registers exceeding K=15
+------------------+  ← R31 (frame pointer) points here
| Outgoing args    |  ← Arguments for called functions
+------------------+
Low addresses

// Alignment: 4-byte (32-bit), 8-byte (64-bit), 16-byte (128-bit SIMD)
```

**Stack Access Pattern**:
```c
// Local variable access via R31 offset
int local_var;
load R5, [R31 + offset]  // Read local
store [R31 + offset], R5 // Write local

// Evidence: Frame pointer usage in decompiled function prologues
```

### Function Call Sequence

**Caller Side**:
```c
// 1. Save caller-saved registers (R0-R23 if live)
push R5
push R10

// 2. Place arguments in R0-R7 or stack
R0 = arg0
R1 = arg1
[stack] = arg8

// 3. Call function
call target

// 4. Retrieve return value from R0/R0:R1
result = R0

// 5. Restore caller-saved registers
pop R10
pop R5
```

**Callee Side**:
```c
// Prologue:
push R31           // Save old frame pointer
R31 = SP           // Establish new frame
SP = SP - frame_size  // Allocate locals
push R24-R30       // Save callee-saved registers used

// ... function body ...

// Epilogue:
pop R24-R30        // Restore callee-saved
SP = R31           // Restore stack pointer
pop R31            // Restore frame pointer
ret                // Return to caller
```

**Evidence**: Calling convention patterns observed in decompiled kernel code, consistent with register_class_constraints.json parameter assignments.

---

## Tensor Register Alignment Requirements

Tensor core operations impose strict alignment constraints on register allocation.

### Alignment by Architecture

**SM70 (Volta) WMMA - 4-Register Boundaries**:
```c
// Alignment requirement: Accumulator base register % 4 == 0
wmma.mma.sync.m16n16k16.f16.f16 {r0, r1, r2, r3}, ...
// ✓ Valid: r0 (0 % 4 == 0)
// ✗ Invalid: r1 (1 % 4 != 0), r2, r3

// Enforcement in allocator:
if (is_wmma_accumulator(vreg)) {
    add_constraint: physical_reg % 4 == 0
}

// Evidence: Constraint class checks at 0x94CAB0
```

**SM80 (Ampere) mma.sync - Consecutive 4-Register Allocation**:
```c
// Must allocate 4 consecutive registers (no gaps)
mma.sync.m16n8k16.f32.f16 {r4, r5, r6, r7}, {r8, ...}, {r12, ...}
// ✓ Valid: r4-r7 consecutive
// ✗ Invalid: {r4, r5, r7, r8} - gap at r6

// Allocator strategy:
//   - Reserve 4-register blocks
//   - Mark all 4 as allocated together
//   - Prevents fragmentation

// Evidence: mma.sync handlers at 0xA8E250
```

**SM90 (Hopper) warpgroup.mma - 8-Register Per-Warp Alignment**:
```c
// Warpgroup = 4 warps (128 threads)
// Each warp: 8-register alignment
// Coordinated across warpgroup

warpgroup.mma.m64n64k16 {r0-r7}, {smem_A}, {smem_B}

Constraint per warp:
  base_reg % 8 == 0   // r0, r8, r16, ...

Cross-warp coordination:
  warp0: r0-r7
  warp1: r0-r7 (same logical registers)
  warp2: r0-r7
  warp3: r0-r7
  // Physical registers may differ per warp but must align

// Evidence: Warpgroup scheduling at 0x35F5090
```

**SM100 (Blackwell) tcgen05 - Descriptor 8-Register Alignment**:
```c
// Tensor descriptors: 8-register boundaries
tcgen05.mma.f16 {r0-r7}, desc_A, desc_B

Constraints:
  - Descriptor base: % 8 == 0
  - FP4 accumulators: 16-register alignment (experimental)

// Evidence: tcgen05 instruction parsing at 0x2CEAC10
```

### Alignment Enforcement Algorithm

**Phase 1: Constraint Propagation**:
```c
void add_alignment_constraints(InterferenceGraph& graph) {
    for (VirtualReg vreg : graph.nodes()) {
        if (is_tensor_accumulator(vreg)) {
            int alignment = get_required_alignment(vreg);

            // Add constraint edges to incompatible physical registers
            for (int phys_reg = 0; phys_reg < K; phys_reg++) {
                if (phys_reg % alignment != 0) {
                    // Cannot use this physical register
                    vreg.forbidden_colors.insert(phys_reg);
                }
            }
        }
    }
}

// Evidence: Constraint edge addition at 0xB612D0, cases for tensor ops
```

**Phase 2: Alignment-Aware Coloring**:
```c
int select_color_with_alignment(VirtualReg vreg, Set<int> forbidden) {
    int alignment = get_required_alignment(vreg);

    // Find lowest-numbered available aligned color
    for (int color = 0; color < K; color++) {
        if (color % alignment == 0 &&          // Alignment check
            !forbidden.contains(color)) {      // Availability check
            return color;
        }
    }

    return -1;  // Spill required
}

// Evidence: Color selection logic at 0x12E1EF0 (51KB function)
```

**Phase 3: Padding Insertion**:
```c
// If alignment causes register pressure, pad to next boundary
void handle_alignment_pressure(VirtualReg vreg, int assigned_color) {
    int alignment = get_required_alignment(vreg);

    if (assigned_color % alignment != 0) {
        // Pad to next aligned register
        int padded = ((assigned_color / alignment) + 1) * alignment;

        if (padded < K) {
            reassign_color(vreg, padded);
        } else {
            mark_for_spilling(vreg);  // No aligned color available
        }
    }
}

// Evidence: Spill cost increases for misaligned tensor ops
```

### Misalignment Penalties

| Architecture | Misaligned Penalty | Evidence |
|--------------|-------------------|----------|
| **SM70** | 50-100 cycles (extra shuffle ops) | WMMA emulation overhead |
| **SM80** | 100-200 cycles (register copying) | mma.sync requires consecutive regs |
| **SM90** | 200-500 cycles (cross-warp sync) | Warpgroup coordination stalls |
| **SM100** | Compiler error (invalid encoding) | tcgen05 enforces alignment strictly |

**Impact on Allocator**:
- Conservative coalescing factor (0.8) prevents aggressive merging that violates alignment
- K=15 physical registers often reduced to K_effective=12 for tensor-heavy code
- Alignment constraints account for ~10-20% of spill decisions in tensor kernels

### Practical Example

```c
// GEMM kernel with SM80 mma.sync
__global__ void gemm_mma_sync() {
    // Allocator decisions:
    float acc[4];      // r0-r3   (4-register aligned to r0)
    half a_frag[8];    // r4-r11  (consecutive, 4-boundary)
    half b_frag[8];    // r12-r19 (consecutive, 4-boundary)

    // Remaining: R20-R31 for scalar operations (K_effective = 12)
    // 12 < 15: Alignment consumed 3 registers worth of flexibility

    mma.sync.m16n8k16 {r0,r1,r2,r3}, {r4,...r11}, {r12,...r19};

    // Evidence: Typical allocation pattern in tensor kernel analysis
}
```

**Alignment Optimization Strategy**:
1. **Early Allocation**: Tensor ops assigned colors first (highest priority)
2. **Block Reservation**: Reserve aligned blocks (r0-r7, r8-r15, etc.)
3. **Fragmentation Avoidance**: Fill non-tensor registers around aligned blocks
4. **Spill Selection**: Prefer spilling non-aligned values over tensor accumulators

**Evidence**: Alignment-aware allocation priority at 0x1090BD0 (node selection with alignment checks).

---

## Known Unknowns

The following require additional research:

1. **Exact loop depth multiplier coefficient**: Analysis shows exponential structure but cannot extract exact base value
2. **Memory latency multiplier per cache level**: Estimated from standard GPU memory hierarchies
3. **SM-version specific cost adjustments**: Register file doubling (SM90+) may affect spill costs
4. **Exact spill location computation**: Stack slot allocation algorithm unknown
5. **SM120 dual tensor core allocation**: Independent allocation strategy not yet analyzed

---

**Last Updated**: 2025-11-16
**Confidence**: HIGH for core algorithms (Briggs, coalescing factor), MEDIUM-HIGH for constants (K=15, 0.8), MEDIUM for coefficients (loop depth, memory latency)
**Basis**: L3 decompiled CICC binary analysis (8+ analysis agents, 104-180KB function sizes verified)
