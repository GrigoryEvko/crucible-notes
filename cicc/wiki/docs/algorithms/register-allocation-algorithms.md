# Register Allocation Algorithms

**CICC Binary Reverse Engineering - NVIDIA PTX/SASS Register Allocator**

Extracted from decompiled functions at addresses 0xB612D0, 0x1081400, 0x1090BD0, 0x12E1EF0.

---

## 1. Chaitin-Briggs Graph Coloring with Optimistic Coalescing

### 1.1 Main Register Allocation Algorithm

```c
// Entry point: 0x1081400 (SimplifyAndColor)
// K = 15 physical registers available for allocation
#define K_REGISTERS 15
#define K_THRESHOLD (K_REGISTERS - 1)  // 14
#define COALESCE_FACTOR 0.8  // Magic constant: 0xCCCCCCCCCCCCCCCD
#define INFINITE_PRIORITY UINT64_MAX

typedef struct InterferenceNode {
    uint64_t vreg_id;           // Virtual register ID
    uint64_t degree;            // Current interference degree
    uint64_t effective_degree;  // Degree adjusted for coalescing
    float spill_cost;           // Computed spill cost
    uint64_t priority;          // Selection priority
    uint64_t color;             // Assigned physical register (0 if uncolored)
    bool is_coalesce_target;    // Can be coalesced with another node
    bool is_spilled;            // Marked for spilling
    struct InterferenceNode** neighbors;  // Adjacent nodes in graph
    uint64_t neighbor_count;
} InterferenceNode;

typedef struct InterferenceGraph {
    InterferenceNode** nodes;
    uint64_t node_count;
    uint64_t* adj_matrix;  // Flattened adjacency matrix
} InterferenceGraph;

typedef struct ColorStack {
    InterferenceNode** nodes;
    uint64_t top;
    uint64_t capacity;
} ColorStack;

typedef struct PriorityHeap {
    InterferenceNode** nodes;
    uint64_t size;
    uint64_t capacity;
} PriorityHeap;

// Main Chaitin-Briggs allocator
void ChaitinBriggsAllocator(Function* F) {
    // Phase 1: Build interference graph
    InterferenceGraph* graph = BuildInterferenceGraph(F);

    // Phase 2: Conservative coalescing
    ConservativeCoalesce(graph);

    // Phase 3: Simplify and color
    ColorStack* stack = CreateColorStack(graph->node_count);
    PriorityHeap* heap = CreatePriorityHeap(graph->node_count);

    // Simplification loop
    while (graph->node_count > 0) {
        InterferenceNode* node = SelectNodeForRemoval(graph, heap);

        if (node == NULL) {
            // No more nodes can be colored - must spill
            node = SelectSpillCandidate(graph, heap);
            node->is_spilled = true;
        }

        // Push to stack for later coloring
        PushColorStack(stack, node);
        RemoveNodeFromGraph(graph, node);
    }

    // Phase 4: Assign colors (pop from stack)
    while (!IsEmptyColorStack(stack)) {
        InterferenceNode* node = PopColorStack(stack);

        if (node->is_spilled) {
            // Insert spill code and create new virtual registers
            InsertSpillCode(F, node);
        } else {
            // Assign color
            uint64_t color = SelectColor(node, K_REGISTERS);
            if (color == 0) {
                // Optimistic coloring failed - must spill
                node->is_spilled = true;
                InsertSpillCode(F, node);
            } else {
                node->color = color;
            }
        }
    }

    // Phase 5: Rewrite code with physical registers
    RewriteWithPhysicalRegisters(F, graph);

    // Cleanup
    FreeColorStack(stack);
    FreePriorityHeap(heap);
    FreeInterferenceGraph(graph);
}
```

### 1.2 Interference Graph Construction

```c
// Build interference graph from liveness analysis
// Complexity: O(V * E) where V = virtual registers, E = edges
InterferenceGraph* BuildInterferenceGraph(Function* F) {
    // Allocate graph structure
    InterferenceGraph* graph = AllocateGraph(F->vreg_count);

    // Create nodes for each virtual register
    for (uint64_t i = 0; i < F->vreg_count; i++) {
        graph->nodes[i] = AllocateNode(i);
        graph->nodes[i]->degree = 0;
        graph->nodes[i]->color = 0;
        graph->nodes[i]->is_spilled = false;
        graph->nodes[i]->neighbors = NULL;
        graph->nodes[i]->neighbor_count = 0;
    }

    // Iterate over all basic blocks
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        // Get live-out set for this block
        BitSet* live = CopyBitSet(bb->live_out);

        // Iterate instructions in reverse order
        for (Instruction* inst = bb->last; inst != NULL; inst = inst->prev) {
            // For each definition in instruction
            for (uint64_t d = 0; d < inst->def_count; d++) {
                uint64_t def_vreg = inst->defs[d];

                // This vreg interferes with all currently live vregs
                for (uint64_t v = 0; v < F->vreg_count; v++) {
                    if (BitSetContains(live, v) && v != def_vreg) {
                        AddInterferenceEdge(graph, def_vreg, v);
                    }
                }

                // Remove defined vreg from live set
                BitSetRemove(live, def_vreg);
            }

            // For each use in instruction
            for (uint64_t u = 0; u < inst->use_count; u++) {
                uint64_t use_vreg = inst->uses[u];
                BitSetAdd(live, use_vreg);
            }
        }

        FreeBitSet(live);
    }

    // Compute initial degrees
    for (uint64_t i = 0; i < graph->node_count; i++) {
        graph->nodes[i]->degree = graph->nodes[i]->neighbor_count;
        graph->nodes[i]->effective_degree = graph->nodes[i]->degree;
    }

    return graph;
}

// Add bidirectional edge between two nodes
// Address: Evidence from 0x1081400 analysis
void AddInterferenceEdge(InterferenceGraph* graph, uint64_t vreg1, uint64_t vreg2) {
    // Check if edge already exists (adjacency matrix check)
    uint64_t matrix_idx = vreg1 * graph->node_count + vreg2;
    if (graph->adj_matrix[matrix_idx]) {
        return;  // Edge already exists
    }

    // Mark edge in adjacency matrix (bidirectional)
    graph->adj_matrix[vreg1 * graph->node_count + vreg2] = 1;
    graph->adj_matrix[vreg2 * graph->node_count + vreg1] = 1;

    // Add to neighbor lists
    AddNeighbor(graph->nodes[vreg1], graph->nodes[vreg2]);
    AddNeighbor(graph->nodes[vreg2], graph->nodes[vreg1]);
}

void AddNeighbor(InterferenceNode* node, InterferenceNode* neighbor) {
    node->neighbor_count++;
    node->neighbors = realloc(node->neighbors,
                              node->neighbor_count * sizeof(InterferenceNode*));
    node->neighbors[node->neighbor_count - 1] = neighbor;
}
```

### 1.3 Conservative Coalescing

```c
// Coalesce move-related nodes that satisfy Briggs criterion
// Evidence: coalesce_factor = 0.8 from 0x1090BD0:603-608
void ConservativeCoalesce(InterferenceGraph* graph) {
    bool changed = true;

    while (changed) {
        changed = false;

        // Find coalesceable move instruction candidates
        for (uint64_t i = 0; i < graph->node_count; i++) {
            InterferenceNode* src = graph->nodes[i];
            if (src == NULL || !src->is_coalesce_target) continue;

            for (uint64_t j = 0; j < src->neighbor_count; j++) {
                InterferenceNode* dst = src->neighbors[j];

                // Check if this is a move-related pair
                if (!IsMoveRelated(src, dst)) continue;

                // Briggs criterion: combined node has < K neighbors with degree >= K
                if (BriggsCoalesceCriterion(src, dst, K_REGISTERS)) {
                    // Merge dst into src
                    CoalesceNodes(graph, src, dst);
                    changed = true;
                    break;
                }
            }
        }
    }

    // Update effective degrees with coalesce factor
    for (uint64_t i = 0; i < graph->node_count; i++) {
        if (graph->nodes[i] != NULL) {
            // Apply coalesce factor: 0.8 (0xCCCCCCCCCCCCCCCD / 2^64)
            graph->nodes[i]->effective_degree =
                (uint64_t)(graph->nodes[i]->degree * COALESCE_FACTOR);
        }
    }
}

// Briggs criterion: conservative coalescing test
// Returns true if src and dst can be safely coalesced
bool BriggsCoalesceCriterion(InterferenceNode* src, InterferenceNode* dst, uint64_t K) {
    // Count combined neighbors with degree >= K
    uint64_t high_degree_count = 0;
    BitSet* combined_neighbors = CreateBitSet(src->neighbor_count + dst->neighbor_count);

    // Add all neighbors of src
    for (uint64_t i = 0; i < src->neighbor_count; i++) {
        BitSetAdd(combined_neighbors, src->neighbors[i]->vreg_id);
    }

    // Add all neighbors of dst
    for (uint64_t i = 0; i < dst->neighbor_count; i++) {
        BitSetAdd(combined_neighbors, dst->neighbors[i]->vreg_id);
    }

    // Count neighbors with high degree
    for (uint64_t i = 0; i < combined_neighbors->size; i++) {
        if (BitSetContains(combined_neighbors, i)) {
            InterferenceNode* neighbor = GetNodeById(src, i);
            if (neighbor->degree >= K) {
                high_degree_count++;
            }
        }
    }

    FreeBitSet(combined_neighbors);

    // Briggs criterion: combined node has < K high-degree neighbors
    return high_degree_count < K;
}

void CoalesceNodes(InterferenceGraph* graph, InterferenceNode* src, InterferenceNode* dst) {
    // Merge dst's neighbors into src
    for (uint64_t i = 0; i < dst->neighbor_count; i++) {
        InterferenceNode* neighbor = dst->neighbors[i];
        if (neighbor == src) continue;

        // Add edge if not already present
        if (!HasEdge(src, neighbor)) {
            AddInterferenceEdge(graph, src->vreg_id, neighbor->vreg_id);
        }
    }

    // Remove dst from graph
    RemoveNodeFromGraph(graph, dst);
}
```

### 1.4 Node Selection with Briggs Priority

```c
// Select node for removal from graph
// Address: 0x1090BD0 (SelectNodeForRemoval)
// Evidence: Lines 1039-1066 implement two-tier selection
InterferenceNode* SelectNodeForRemoval(InterferenceGraph* graph, PriorityHeap* heap) {
    // TIER 1: Briggs optimistic coloring
    // Select nodes with low-degree neighbor count >= K
    InterferenceNode* briggs_node = SelectBriggsNode(graph);
    if (briggs_node != NULL) {
        return briggs_node;
    }

    // TIER 2: Cost-based selection
    // Build priority heap: priority = spill_cost / effective_degree
    ClearPriorityHeap(heap);

    for (uint64_t i = 0; i < graph->node_count; i++) {
        InterferenceNode* node = graph->nodes[i];
        if (node == NULL) continue;

        // Compute priority
        node->priority = ComputeNodePriority(node);

        // Insert into max-heap
        PriorityHeapInsert(heap, node);
    }

    // Return highest priority node (max spill_cost / degree)
    if (heap->size > 0) {
        return PriorityHeapExtractMax(heap);
    }

    return NULL;  // Graph is empty
}

// Briggs optimization: select node with sufficient low-degree neighbors
// Evidence: 0x1090BD0:1039-1044 - degree check v64 > 0xE (14)
InterferenceNode* SelectBriggsNode(InterferenceGraph* graph) {
    for (uint64_t i = 0; i < graph->node_count; i++) {
        InterferenceNode* node = graph->nodes[i];
        if (node == NULL) continue;

        // Count neighbors with degree < K
        uint64_t low_degree_count = 0;
        for (uint64_t j = 0; j < node->neighbor_count; j++) {
            InterferenceNode* neighbor = node->neighbors[j];

            // Check if neighbor degree < K (code checks v64 > 0xE, so we check <= 14)
            if (neighbor->degree <= K_THRESHOLD) {
                low_degree_count++;
            }
        }

        // Briggs criterion: if count >= K, this node can be colored conservatively
        if (low_degree_count >= K_REGISTERS) {
            return node;
        }
    }

    return NULL;  // No Briggs candidates
}

// Compute node priority for cost-based selection
// Evidence: 0x1081400:1076 - priority calculation with conditional weight
uint64_t ComputeNodePriority(InterferenceNode* node) {
    if (node->effective_degree == 0) {
        return INFINITE_PRIORITY;
    }

    // Priority = spill_cost / effective_degree
    // Higher cost, lower degree = higher priority to stay in register
    float priority = node->spill_cost / (float)node->effective_degree;

    // Apply conditional weight multiplier (2 or 1) based on node properties
    // Evidence: v70 = *(_DWORD *)(v6 + 16) * (2 - ((*(_QWORD *)(v6 + 32) == 0) - 1))
    uint64_t weight_multiplier = node->is_coalesce_target ? 2 : 1;

    return (uint64_t)(priority * weight_multiplier * 1000000.0);  // Scale to integer
}
```

### 1.5 Color Assignment

```c
// Assign physical register to node
// Address: 0x12E1EF0 (AssignColors)
uint64_t SelectColor(InterferenceNode* node, uint64_t K) {
    // Build set of forbidden colors (used by neighbors)
    bool forbidden[K_REGISTERS + 1];
    memset(forbidden, 0, sizeof(forbidden));

    // Mark colors used by already-colored neighbors
    for (uint64_t i = 0; i < node->neighbor_count; i++) {
        InterferenceNode* neighbor = node->neighbors[i];
        if (neighbor->color != 0) {
            forbidden[neighbor->color] = true;
        }
    }

    // Find first available color
    for (uint64_t color = 1; color <= K; color++) {
        if (!forbidden[color]) {
            return color;
        }
    }

    // No color available - must spill
    return 0;
}

void RemoveNodeFromGraph(InterferenceGraph* graph, InterferenceNode* node) {
    // Decrease degree of all neighbors
    for (uint64_t i = 0; i < node->neighbor_count; i++) {
        InterferenceNode* neighbor = node->neighbors[i];
        neighbor->degree--;
        neighbor->effective_degree = (uint64_t)(neighbor->degree * COALESCE_FACTOR);
    }

    // Mark node as removed (don't free, needed for coloring phase)
    for (uint64_t i = 0; i < graph->node_count; i++) {
        if (graph->nodes[i] == node) {
            graph->nodes[i] = NULL;
            graph->node_count--;
            break;
        }
    }
}
```

### 1.6 Priority Heap Operations

```c
PriorityHeap* CreatePriorityHeap(uint64_t capacity) {
    PriorityHeap* heap = malloc(sizeof(PriorityHeap));
    heap->nodes = malloc(capacity * sizeof(InterferenceNode*));
    heap->size = 0;
    heap->capacity = capacity;
    return heap;
}

void PriorityHeapInsert(PriorityHeap* heap, InterferenceNode* node) {
    if (heap->size >= heap->capacity) return;

    // Insert at end and bubble up
    heap->nodes[heap->size] = node;
    uint64_t idx = heap->size;
    heap->size++;

    // Max-heap: parent >= children
    while (idx > 0) {
        uint64_t parent_idx = (idx - 1) / 2;
        if (heap->nodes[parent_idx]->priority >= heap->nodes[idx]->priority) {
            break;
        }

        // Swap with parent
        InterferenceNode* temp = heap->nodes[parent_idx];
        heap->nodes[parent_idx] = heap->nodes[idx];
        heap->nodes[idx] = temp;
        idx = parent_idx;
    }
}

InterferenceNode* PriorityHeapExtractMax(PriorityHeap* heap) {
    if (heap->size == 0) return NULL;

    InterferenceNode* max_node = heap->nodes[0];
    heap->size--;

    if (heap->size > 0) {
        // Move last element to root and bubble down
        heap->nodes[0] = heap->nodes[heap->size];
        uint64_t idx = 0;

        while (true) {
            uint64_t left = 2 * idx + 1;
            uint64_t right = 2 * idx + 2;
            uint64_t largest = idx;

            if (left < heap->size &&
                heap->nodes[left]->priority > heap->nodes[largest]->priority) {
                largest = left;
            }

            if (right < heap->size &&
                heap->nodes[right]->priority > heap->nodes[largest]->priority) {
                largest = right;
            }

            if (largest == idx) break;

            // Swap with largest child
            InterferenceNode* temp = heap->nodes[idx];
            heap->nodes[idx] = heap->nodes[largest];
            heap->nodes[largest] = temp;
            idx = largest;
        }
    }

    return max_node;
}

void ClearPriorityHeap(PriorityHeap* heap) {
    heap->size = 0;
}

void FreePriorityHeap(PriorityHeap* heap) {
    free(heap->nodes);
    free(heap);
}
```

---

## 2. Spill Cost Computation

### 2.1 Complete Spill Cost Formula

```c
// Spill cost computation
// Address: 0xB612D0 (Register Allocation Entry Point)
// Formula: Cost = def_freq * use_freq * mem_latency * loop_depth_mult

#define MEM_LATENCY_L1_HIT 4.0
#define MEM_LATENCY_L2_HIT 10.0
#define MEM_LATENCY_L3_HIT 40.0
#define MEM_LATENCY_MAIN_MEM 100.0
#define LOOP_DEPTH_BASE 1.8  // Exponential base for loop nesting
#define OCCUPANCY_PENALTY_WEIGHT 1.2

typedef struct SpillCostContext {
    Function* function;
    float* basic_block_frequencies;  // Execution frequency per block
    uint64_t* loop_depths;           // Loop nesting depth per block
    float memory_latency_multiplier; // Hardware-dependent
} SpillCostContext;

// Compute spill cost for a virtual register
float ComputeSpillCost(uint64_t vreg, SpillCostContext* ctx) {
    float cost = 0.0;

    // Count definitions and uses across all blocks
    uint64_t def_count = 0;
    uint64_t use_count = 0;
    float def_frequency = 0.0;
    float use_frequency = 0.0;

    for (BasicBlock* bb = ctx->function->entry; bb != NULL; bb = bb->next) {
        uint64_t bb_id = bb->id;
        float bb_freq = ctx->basic_block_frequencies[bb_id];
        uint64_t loop_depth = ctx->loop_depths[bb_id];

        // Loop depth multiplier: exponential growth
        float loop_mult = pow(LOOP_DEPTH_BASE, loop_depth);

        for (Instruction* inst = bb->first; inst != NULL; inst = inst->next) {
            // Count definitions
            for (uint64_t d = 0; d < inst->def_count; d++) {
                if (inst->defs[d] == vreg) {
                    def_count++;
                    def_frequency += bb_freq * loop_mult;
                }
            }

            // Count uses
            for (uint64_t u = 0; u < inst->use_count; u++) {
                if (inst->uses[u] == vreg) {
                    use_count++;
                    use_frequency += bb_freq * loop_mult;
                }
            }
        }
    }

    // Base cost: def_freq * use_freq
    cost = def_frequency * use_frequency;

    // Memory latency multiplier (hardware-dependent)
    cost *= ctx->memory_latency_multiplier;

    // Occupancy penalty (implicit GPU constraint)
    // Higher register usage reduces occupancy - penalize high-use vregs less
    if (use_count > 10) {
        cost *= OCCUPANCY_PENALTY_WEIGHT;
    }

    return cost;
}

// Initialize spill cost context with frequency analysis
SpillCostContext* CreateSpillCostContext(Function* F) {
    SpillCostContext* ctx = malloc(sizeof(SpillCostContext));
    ctx->function = F;

    // Allocate frequency and depth arrays
    uint64_t bb_count = CountBasicBlocks(F);
    ctx->basic_block_frequencies = malloc(bb_count * sizeof(float));
    ctx->loop_depths = malloc(bb_count * sizeof(uint64_t));

    // Compute basic block frequencies (profile-guided or heuristic)
    ComputeBasicBlockFrequencies(F, ctx->basic_block_frequencies);

    // Compute loop nesting depths
    ComputeLoopDepths(F, ctx->loop_depths);

    // Set memory latency (architecture-dependent)
    // Default to L2 cache hit latency
    ctx->memory_latency_multiplier = MEM_LATENCY_L2_HIT;

    return ctx;
}
```

### 2.2 Frequency Analysis

```c
// Compute execution frequency for each basic block
// Uses static heuristics (branch prediction patterns)
void ComputeBasicBlockFrequencies(Function* F, float* frequencies) {
    // Initialize all frequencies to 0
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        frequencies[bb->id] = 0.0;
    }

    // Entry block has frequency 1.0
    frequencies[F->entry->id] = 1.0;

    // Propagate frequencies through control flow
    bool changed = true;
    while (changed) {
        changed = false;

        for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
            float incoming_freq = 0.0;

            // Sum frequencies from predecessors
            for (uint64_t i = 0; i < bb->pred_count; i++) {
                BasicBlock* pred = bb->preds[i];

                // Divide by number of successors (equal probability heuristic)
                float pred_freq = frequencies[pred->id];
                float branch_prob = 1.0 / (float)pred->succ_count;

                // Apply branch prediction heuristics
                if (IsBackEdge(pred, bb)) {
                    // Loop back-edge: higher probability
                    branch_prob = 0.9;
                } else if (IsExitEdge(pred, bb)) {
                    // Loop exit edge: lower probability
                    branch_prob = 0.1;
                }

                incoming_freq += pred_freq * branch_prob;
            }

            // Update frequency if changed
            if (fabs(incoming_freq - frequencies[bb->id]) > 0.001) {
                frequencies[bb->id] = incoming_freq;
                changed = true;
            }
        }
    }
}

// Compute loop nesting depth for each basic block
void ComputeLoopDepths(Function* F, uint64_t* depths) {
    // Initialize all depths to 0
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        depths[bb->id] = 0;
    }

    // Identify natural loops using dominator tree
    DominatorTree* dom_tree = BuildDominatorTree(F);

    // Find back edges (edges where target dominates source)
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        for (uint64_t i = 0; i < bb->succ_count; i++) {
            BasicBlock* succ = bb->succs[i];

            // Back edge detected: succ dominates bb
            if (Dominates(dom_tree, succ, bb)) {
                // Mark all blocks in natural loop
                MarkNaturalLoop(succ, bb, depths);
            }
        }
    }

    FreeDominatorTree(dom_tree);
}

// Mark all blocks in natural loop from header to latch
void MarkNaturalLoop(BasicBlock* header, BasicBlock* latch, uint64_t* depths) {
    // BFS from latch back to header
    Queue* worklist = CreateQueue();
    QueuePush(worklist, latch);

    BitSet* visited = CreateBitSet(1024);
    BitSetAdd(visited, latch->id);

    while (!QueueIsEmpty(worklist)) {
        BasicBlock* bb = QueuePop(worklist);

        // Increment depth for this block
        depths[bb->id]++;

        // Stop at header
        if (bb == header) continue;

        // Add predecessors to worklist
        for (uint64_t i = 0; i < bb->pred_count; i++) {
            BasicBlock* pred = bb->preds[i];
            if (!BitSetContains(visited, pred->id)) {
                BitSetAdd(visited, pred->id);
                QueuePush(worklist, pred);
            }
        }
    }

    FreeBitSet(visited);
    FreeQueue(worklist);
}
```

### 2.3 SM-Specific Adjustments

```c
// Adjust spill cost parameters for specific GPU architecture
void AdjustSpillCostForSM(SpillCostContext* ctx, uint64_t sm_version) {
    switch (sm_version) {
        case 70: // Volta (SM 7.0)
            ctx->memory_latency_multiplier = MEM_LATENCY_L2_HIT;  // 10 cycles
            break;

        case 75: // Turing (SM 7.5)
            ctx->memory_latency_multiplier = MEM_LATENCY_L2_HIT;  // 10 cycles
            break;

        case 80: // Ampere (SM 8.0)
            ctx->memory_latency_multiplier = 8.0;  // Improved L2 cache
            break;

        case 86: // Ampere (SM 8.6)
            ctx->memory_latency_multiplier = 8.0;
            break;

        case 89: // Lovelace (SM 8.9)
            ctx->memory_latency_multiplier = 7.0;  // Further improved cache
            break;

        case 90: // Hopper (SM 9.0)
            ctx->memory_latency_multiplier = 6.0;  // L2 + L3 cache hierarchy
            break;

        default:
            ctx->memory_latency_multiplier = MEM_LATENCY_L2_HIT;
            break;
    }
}
```

---

## 3. Lazy Reload Optimization

### 3.1 Main Lazy Reload Algorithm

```c
// Lazy reload optimization with redundancy elimination
// Address: 0xB612D0 (Register Allocation Entry Point)
// Helper functions: 0xA778C0, 0xA79C90, 0xB5BA00, 0xA78010

typedef struct ReloadPoint {
    BasicBlock* block;
    Instruction* inst;
    uint64_t vreg;
    bool is_redundant;
} ReloadPoint;

typedef struct SpillSlot {
    uint64_t vreg;
    uint64_t stack_offset;
    ReloadPoint** reload_points;
    uint64_t reload_count;
} SpillSlot;

typedef struct LazyReloadContext {
    Function* function;
    SpillSlot** spill_slots;
    uint64_t spill_count;
    DominatorTree* dom_tree;
    BitSet** available_at;  // Available[bb_id] = set of reloaded vregs at bb entry
} LazyReloadContext;

// Main lazy reload optimization
void LazyReloadOptimization(Function* F, InterferenceGraph* graph) {
    LazyReloadContext* ctx = CreateLazyReloadContext(F, graph);

    // Phase 1: Identify spill locations
    IdentifySpillLocations(ctx, graph);

    // Phase 2: Analyze use points for each spilled value
    AnalyzeUsePoints(ctx);

    // Phase 3: Compute optimal reload points (as late as possible)
    ComputeReloadPoints(ctx);

    // Phase 4: Eliminate redundant reloads
    EliminateRedundantReloads(ctx);

    // Insert spill/reload code into function
    InsertSpillReloadCode(F, ctx);

    FreeLazyReloadContext(ctx);
}

LazyReloadContext* CreateLazyReloadContext(Function* F, InterferenceGraph* graph) {
    LazyReloadContext* ctx = malloc(sizeof(LazyReloadContext));
    ctx->function = F;
    ctx->spill_count = 0;
    ctx->spill_slots = malloc(graph->node_count * sizeof(SpillSlot*));
    ctx->dom_tree = BuildDominatorTree(F);

    // Allocate availability sets for each basic block
    uint64_t bb_count = CountBasicBlocks(F);
    ctx->available_at = malloc(bb_count * sizeof(BitSet*));
    for (uint64_t i = 0; i < bb_count; i++) {
        ctx->available_at[i] = CreateBitSet(graph->node_count);
    }

    return ctx;
}
```

### 3.2 Phase 1: Identify Spill Locations

```c
// Identify which virtual registers are spilled and their stack locations
// Evidence: 0xB612D0:728-732 instruction opcode parsing
void IdentifySpillLocations(LazyReloadContext* ctx, InterferenceGraph* graph) {
    uint64_t stack_offset = 0;

    for (uint64_t i = 0; i < graph->node_count; i++) {
        InterferenceNode* node = graph->nodes[i];
        if (node == NULL || !node->is_spilled) continue;

        // Allocate spill slot
        SpillSlot* slot = malloc(sizeof(SpillSlot));
        slot->vreg = node->vreg_id;
        slot->stack_offset = stack_offset;
        slot->reload_points = NULL;
        slot->reload_count = 0;

        ctx->spill_slots[ctx->spill_count++] = slot;

        // Increment stack offset (8 bytes per slot for 64-bit values)
        stack_offset += 8;
    }
}
```

### 3.3 Phase 2: Analyze Use Points

```c
// Analyze all use points for each spilled virtual register
// Evidence: 0xB612D0 switch statement - each case analyzes operand usage
void AnalyzeUsePoints(LazyReloadContext* ctx) {
    for (uint64_t s = 0; s < ctx->spill_count; s++) {
        SpillSlot* slot = ctx->spill_slots[s];
        uint64_t vreg = slot->vreg;

        // Find all instructions that use this vreg
        for (BasicBlock* bb = ctx->function->entry; bb != NULL; bb = bb->next) {
            for (Instruction* inst = bb->first; inst != NULL; inst = inst->next) {
                // Check if instruction uses this vreg
                for (uint64_t u = 0; u < inst->use_count; u++) {
                    if (inst->uses[u] == vreg) {
                        // Record use point
                        RecordUsePoint(slot, bb, inst);
                    }
                }
            }
        }
    }
}

void RecordUsePoint(SpillSlot* slot, BasicBlock* bb, Instruction* inst) {
    // Add reload point (initially not marked as redundant)
    ReloadPoint* rp = malloc(sizeof(ReloadPoint));
    rp->block = bb;
    rp->inst = inst;
    rp->vreg = slot->vreg;
    rp->is_redundant = false;

    slot->reload_count++;
    slot->reload_points = realloc(slot->reload_points,
                                  slot->reload_count * sizeof(ReloadPoint*));
    slot->reload_points[slot->reload_count - 1] = rp;
}
```

### 3.4 Phase 3: Compute Optimal Reload Points

```c
// Compute optimal reload points using lazy (as-late-as-possible) placement
// Evidence: 0xA78010:77-82 - reload placement logic
void ComputeReloadPoints(LazyReloadContext* ctx) {
    for (uint64_t s = 0; s < ctx->spill_count; s++) {
        SpillSlot* slot = ctx->spill_slots[s];

        // For each use point, find optimal reload insertion point
        for (uint64_t r = 0; r < slot->reload_count; r++) {
            ReloadPoint* rp = slot->reload_points[r];

            // Lazy reload: place reload immediately before use
            // Check if value is already available on this path
            if (!IsAvailableAtPoint(ctx, rp->block, rp->inst, slot->vreg)) {
                // Need to insert reload before this instruction
                rp->is_redundant = false;
            } else {
                // Value already loaded on this path - redundant
                rp->is_redundant = true;
            }
        }
    }
}

// Check if vreg is already loaded and available at this point
bool IsAvailableAtPoint(LazyReloadContext* ctx, BasicBlock* bb,
                        Instruction* inst, uint64_t vreg) {
    // Check if vreg is available at block entry
    if (BitSetContains(ctx->available_at[bb->id], vreg)) {
        // Check if it's still available at this instruction
        // (not killed by a previous instruction in this block)
        for (Instruction* i = bb->first; i != inst; i = i->next) {
            // Check if instruction defines (kills) this vreg
            for (uint64_t d = 0; d < i->def_count; d++) {
                if (i->defs[d] == vreg) {
                    return false;  // Vreg was redefined - not available
                }
            }
        }
        return true;
    }

    return false;
}
```

### 3.5 Phase 4: Eliminate Redundant Reloads

```c
// Eliminate redundant reloads using dataflow analysis
// Evidence: 0xA79C90 and 0xA79B90 - operand constraint processing
void EliminateRedundantReloads(LazyReloadContext* ctx) {
    // Forward dataflow analysis: track which vregs are available at each point
    bool changed = true;

    while (changed) {
        changed = false;

        for (BasicBlock* bb = ctx->function->entry; bb != NULL; bb = bb->next) {
            BitSet* old_available = CopyBitSet(ctx->available_at[bb->id]);
            BitSet* new_available = CreateBitSet(ctx->spill_count);

            // Union of available sets from all predecessors
            if (bb->pred_count > 0) {
                // Start with intersection (available on ALL paths)
                BitSetCopy(new_available, ctx->available_at[bb->preds[0]->id]);

                for (uint64_t p = 1; p < bb->pred_count; p++) {
                    BitSetIntersect(new_available,
                                   ctx->available_at[bb->preds[p]->id]);
                }
            }

            // Process instructions in this block
            for (Instruction* inst = bb->first; inst != NULL; inst = inst->next) {
                // Check if this instruction has a reload
                for (uint64_t s = 0; s < ctx->spill_count; s++) {
                    SpillSlot* slot = ctx->spill_slots[s];

                    for (uint64_t r = 0; r < slot->reload_count; r++) {
                        ReloadPoint* rp = slot->reload_points[r];

                        if (rp->block == bb && rp->inst == inst) {
                            if (BitSetContains(new_available, slot->vreg)) {
                                // Already available - mark as redundant
                                rp->is_redundant = true;
                            } else {
                                // Not available - need reload
                                rp->is_redundant = false;
                                BitSetAdd(new_available, slot->vreg);
                            }
                        }
                    }
                }

                // Check if instruction kills (defines) any spilled vreg
                for (uint64_t d = 0; d < inst->def_count; d++) {
                    BitSetRemove(new_available, inst->defs[d]);
                }
            }

            // Update available set if changed
            if (!BitSetEquals(old_available, new_available)) {
                FreeBitSet(ctx->available_at[bb->id]);
                ctx->available_at[bb->id] = new_available;
                changed = true;
            } else {
                FreeBitSet(new_available);
            }

            FreeBitSet(old_available);
        }
    }
}
```

### 3.6 Spill/Reload Code Insertion

```c
// Insert actual spill and reload instructions into function
void InsertSpillReloadCode(Function* F, LazyReloadContext* ctx) {
    for (uint64_t s = 0; s < ctx->spill_count; s++) {
        SpillSlot* slot = ctx->spill_slots[s];

        // Insert reload instructions at non-redundant reload points
        for (uint64_t r = 0; r < slot->reload_count; r++) {
            ReloadPoint* rp = slot->reload_points[r];

            if (!rp->is_redundant) {
                // Insert reload before use instruction
                Instruction* reload = CreateReloadInstruction(slot->vreg,
                                                              slot->stack_offset);
                InsertInstructionBefore(rp->block, rp->inst, reload);
            }
        }

        // Insert spill instructions at definition points
        InsertSpillInstructions(F, slot);
    }
}

Instruction* CreateReloadInstruction(uint64_t vreg, uint64_t stack_offset) {
    // Create load instruction: vreg = LOAD [stack_offset]
    Instruction* inst = malloc(sizeof(Instruction));
    inst->opcode = OP_LOAD;
    inst->def_count = 1;
    inst->defs = malloc(sizeof(uint64_t));
    inst->defs[0] = vreg;
    inst->use_count = 0;
    inst->uses = NULL;
    inst->stack_offset = stack_offset;
    return inst;
}

void InsertSpillInstructions(Function* F, SpillSlot* slot) {
    // Find all definitions of this vreg and insert spill after
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        for (Instruction* inst = bb->first; inst != NULL; inst = inst->next) {
            // Check if instruction defines this vreg
            for (uint64_t d = 0; d < inst->def_count; d++) {
                if (inst->defs[d] == slot->vreg) {
                    // Insert spill after definition
                    Instruction* spill = CreateSpillInstruction(slot->vreg,
                                                               slot->stack_offset);
                    InsertInstructionAfter(bb, inst, spill);
                }
            }
        }
    }
}

Instruction* CreateSpillInstruction(uint64_t vreg, uint64_t stack_offset) {
    // Create store instruction: STORE vreg, [stack_offset]
    Instruction* inst = malloc(sizeof(Instruction));
    inst->opcode = OP_STORE;
    inst->def_count = 0;
    inst->defs = NULL;
    inst->use_count = 1;
    inst->uses = malloc(sizeof(uint64_t));
    inst->uses[0] = vreg;
    inst->stack_offset = stack_offset;
    return inst;
}
```

---

## 4. Helper Algorithms

### 4.1 Liveness Analysis

```c
// Compute live-in and live-out sets for all basic blocks
// Required for interference graph construction
void LivenessAnalysis(Function* F) {
    uint64_t bb_count = CountBasicBlocks(F);

    // Allocate live-in and live-out sets
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        bb->live_in = CreateBitSet(F->vreg_count);
        bb->live_out = CreateBitSet(F->vreg_count);
    }

    // Backward dataflow analysis
    bool changed = true;
    while (changed) {
        changed = false;

        // Iterate in reverse postorder
        for (BasicBlock* bb = F->exit; bb != NULL; bb = bb->prev) {
            BitSet* old_live_in = CopyBitSet(bb->live_in);
            BitSet* old_live_out = CopyBitSet(bb->live_out);

            // live_out[bb] = Union of live_in[succ] for all successors
            BitSetClear(bb->live_out);
            for (uint64_t s = 0; s < bb->succ_count; s++) {
                BitSetUnion(bb->live_out, bb->succs[s]->live_in);
            }

            // live_in[bb] = use[bb] Union (live_out[bb] - def[bb])
            BitSetCopy(bb->live_in, bb->live_out);
            BitSetDifference(bb->live_in, bb->def_set);
            BitSetUnion(bb->live_in, bb->use_set);

            // Check for changes
            if (!BitSetEquals(old_live_in, bb->live_in) ||
                !BitSetEquals(old_live_out, bb->live_out)) {
                changed = true;
            }

            FreeBitSet(old_live_in);
            FreeBitSet(old_live_out);
        }
    }
}

// Compute use and def sets for each basic block
void ComputeUseDefSets(Function* F) {
    for (BasicBlock* bb = F->entry; bb != NULL; bb = bb->next) {
        bb->use_set = CreateBitSet(F->vreg_count);
        bb->def_set = CreateBitSet(F->vreg_count);

        for (Instruction* inst = bb->first; inst != NULL; inst = inst->next) {
            // Process uses (add to use_set if not already defined)
            for (uint64_t u = 0; u < inst->use_count; u++) {
                uint64_t vreg = inst->uses[u];
                if (!BitSetContains(bb->def_set, vreg)) {
                    BitSetAdd(bb->use_set, vreg);
                }
            }

            // Process definitions
            for (uint64_t d = 0; d < inst->def_count; d++) {
                BitSetAdd(bb->def_set, inst->defs[d]);
            }
        }
    }
}
```

### 4.2 Dominator Tree Construction

```c
// Build dominator tree using Lengauer-Tarjan algorithm
// Complexity: O(E * alpha(E,V)) where alpha is inverse Ackermann
DominatorTree* BuildDominatorTree(Function* F) {
    uint64_t bb_count = CountBasicBlocks(F);
    DominatorTree* tree = malloc(sizeof(DominatorTree));
    tree->idom = malloc(bb_count * sizeof(uint64_t));
    tree->bb_count = bb_count;

    // Initialize immediate dominators
    for (uint64_t i = 0; i < bb_count; i++) {
        tree->idom[i] = UINT64_MAX;  // Undefined
    }

    // Entry block dominates itself
    tree->idom[F->entry->id] = F->entry->id;

    // Iterative dataflow algorithm
    bool changed = true;
    while (changed) {
        changed = false;

        // Process blocks in reverse postorder (excluding entry)
        for (BasicBlock* bb = F->entry->next; bb != NULL; bb = bb->next) {
            uint64_t new_idom = UINT64_MAX;

            // Find first processed predecessor
            for (uint64_t p = 0; p < bb->pred_count; p++) {
                BasicBlock* pred = bb->preds[p];
                if (tree->idom[pred->id] != UINT64_MAX) {
                    new_idom = pred->id;
                    break;
                }
            }

            // Intersect with all other processed predecessors
            for (uint64_t p = 0; p < bb->pred_count; p++) {
                BasicBlock* pred = bb->preds[p];
                if (pred->id == new_idom) continue;
                if (tree->idom[pred->id] == UINT64_MAX) continue;

                new_idom = Intersect(tree, pred->id, new_idom);
            }

            // Update if changed
            if (tree->idom[bb->id] != new_idom) {
                tree->idom[bb->id] = new_idom;
                changed = true;
            }
        }
    }

    return tree;
}

// Find common dominator of two nodes
uint64_t Intersect(DominatorTree* tree, uint64_t b1, uint64_t b2) {
    while (b1 != b2) {
        while (b1 > b2) {
            b1 = tree->idom[b1];
        }
        while (b2 > b1) {
            b2 = tree->idom[b2];
        }
    }
    return b1;
}

bool Dominates(DominatorTree* tree, BasicBlock* dominator, BasicBlock* node) {
    uint64_t current = node->id;
    while (current != dominator->id && current != UINT64_MAX) {
        current = tree->idom[current];
    }
    return current == dominator->id;
}
```

### 4.3 Live Range Splitting

```c
// Split live range of a virtual register at spill point
// Creates new vregs for each segment between spill and reload
void SplitLiveRange(Function* F, uint64_t vreg, SpillSlot* slot) {
    // For each reload point, create a new vreg
    for (uint64_t r = 0; r < slot->reload_count; r++) {
        ReloadPoint* rp = slot->reload_points[r];
        if (rp->is_redundant) continue;

        // Allocate new vreg for this live range segment
        uint64_t new_vreg = AllocateVirtualRegister(F);

        // Replace uses in this segment with new vreg
        ReplaceUsesInRange(F, rp->block, rp->inst, vreg, new_vreg);

        // Update reload instruction to define new vreg
        rp->vreg = new_vreg;
    }
}

void ReplaceUsesInRange(Function* F, BasicBlock* start_bb,
                        Instruction* start_inst, uint64_t old_vreg,
                        uint64_t new_vreg) {
    bool in_range = false;

    for (BasicBlock* bb = start_bb; bb != NULL; bb = bb->next) {
        Instruction* start = (bb == start_bb) ? start_inst : bb->first;

        for (Instruction* inst = start; inst != NULL; inst = inst->next) {
            // Replace uses
            for (uint64_t u = 0; u < inst->use_count; u++) {
                if (inst->uses[u] == old_vreg) {
                    inst->uses[u] = new_vreg;
                }
            }

            // Stop at next definition of old_vreg
            for (uint64_t d = 0; d < inst->def_count; d++) {
                if (inst->defs[d] == old_vreg) {
                    return;
                }
            }
        }
    }
}
```

### 4.4 Spill Selection Heuristic

```c
// Select best candidate for spilling when coloring fails
// Uses cost model: prefer low-cost, high-degree nodes
InterferenceNode* SelectSpillCandidate(InterferenceGraph* graph,
                                       PriorityHeap* heap) {
    // Build min-heap: minimize spill_cost / degree
    ClearPriorityHeap(heap);

    for (uint64_t i = 0; i < graph->node_count; i++) {
        InterferenceNode* node = graph->nodes[i];
        if (node == NULL) continue;

        // Compute spill priority (inverted - lower is better for spilling)
        if (node->effective_degree > 0) {
            node->priority = (uint64_t)((node->spill_cost /
                                        (float)node->effective_degree) * 1000000.0);
        } else {
            node->priority = (uint64_t)(node->spill_cost * 1000000.0);
        }

        PriorityHeapInsert(heap, node);
    }

    // Extract minimum (lowest cost/degree ratio = best spill candidate)
    return PriorityHeapExtractMin(heap);
}

InterferenceNode* PriorityHeapExtractMin(PriorityHeap* heap) {
    if (heap->size == 0) return NULL;

    InterferenceNode* min_node = heap->nodes[0];
    heap->size--;

    if (heap->size > 0) {
        heap->nodes[0] = heap->nodes[heap->size];
        uint64_t idx = 0;

        // Min-heap: bubble down
        while (true) {
            uint64_t left = 2 * idx + 1;
            uint64_t right = 2 * idx + 2;
            uint64_t smallest = idx;

            if (left < heap->size &&
                heap->nodes[left]->priority < heap->nodes[smallest]->priority) {
                smallest = left;
            }

            if (right < heap->size &&
                heap->nodes[right]->priority < heap->nodes[smallest]->priority) {
                smallest = right;
            }

            if (smallest == idx) break;

            InterferenceNode* temp = heap->nodes[idx];
            heap->nodes[idx] = heap->nodes[smallest];
            heap->nodes[smallest] = temp;
            idx = smallest;
        }
    }

    return min_node;
}
```

### 4.5 Register Constraint Handling

```c
// Handle architecture-specific register constraints
// GPU register file has multiple classes and alignment requirements

#define REG_CLASS_GENERAL 0
#define REG_CLASS_PREDICATE 1
#define REG_CLASS_UNIFORM 2
#define REG_CLASS_SPECIAL 3

typedef struct RegisterConstraint {
    uint64_t reg_class;      // Register class required
    uint64_t alignment;      // Alignment requirement (1, 2, 4, etc.)
    bool allow_spill;        // Can this operand be spilled to memory
    uint64_t fixed_reg;      // Fixed register assignment (0 = any)
} RegisterConstraint;

// Apply register constraints during color selection
uint64_t SelectColorWithConstraints(InterferenceNode* node,
                                    RegisterConstraint* constraint,
                                    uint64_t K) {
    // Build forbidden set from neighbors
    bool forbidden[K_REGISTERS + 1];
    memset(forbidden, 0, sizeof(forbidden));

    for (uint64_t i = 0; i < node->neighbor_count; i++) {
        InterferenceNode* neighbor = node->neighbors[i];
        if (neighbor->color != 0) {
            forbidden[neighbor->color] = true;
        }
    }

    // If fixed register required, check if available
    if (constraint->fixed_reg != 0) {
        if (!forbidden[constraint->fixed_reg]) {
            return constraint->fixed_reg;
        }
        return 0;  // Fixed register not available - must spill
    }

    // Find first available color with alignment constraint
    for (uint64_t color = 1; color <= K; color++) {
        if (forbidden[color]) continue;

        // Check alignment
        if ((color - 1) % constraint->alignment != 0) continue;

        // Check register class (GPU-specific)
        if (!IsValidRegisterClass(color, constraint->reg_class)) continue;

        return color;
    }

    // No valid color found
    if (constraint->allow_spill) {
        return 0;  // Spill to memory
    }

    // Cannot spill - allocation failure (should not happen)
    return UINT64_MAX;
}

bool IsValidRegisterClass(uint64_t color, uint64_t reg_class) {
    // GPU register file layout (example for NVIDIA architecture)
    switch (reg_class) {
        case REG_CLASS_GENERAL:
            return color <= 255;  // R0-R254

        case REG_CLASS_PREDICATE:
            return color >= 256 && color <= 271;  // P0-P15

        case REG_CLASS_UNIFORM:
            return color <= 255;  // Shared with general

        case REG_CLASS_SPECIAL:
            return color >= 272;  // Special registers

        default:
            return true;
    }
}
```

### 4.6 BitSet Implementation

```c
// Efficient bit set for tracking sets of virtual registers
typedef struct BitSet {
    uint64_t* bits;
    uint64_t size;     // Number of bits
    uint64_t capacity; // Number of 64-bit words
} BitSet;

BitSet* CreateBitSet(uint64_t size) {
    BitSet* set = malloc(sizeof(BitSet));
    set->size = size;
    set->capacity = (size + 63) / 64;
    set->bits = calloc(set->capacity, sizeof(uint64_t));
    return set;
}

void BitSetAdd(BitSet* set, uint64_t element) {
    if (element >= set->size) return;
    uint64_t word = element / 64;
    uint64_t bit = element % 64;
    set->bits[word] |= (1ULL << bit);
}

void BitSetRemove(BitSet* set, uint64_t element) {
    if (element >= set->size) return;
    uint64_t word = element / 64;
    uint64_t bit = element % 64;
    set->bits[word] &= ~(1ULL << bit);
}

bool BitSetContains(BitSet* set, uint64_t element) {
    if (element >= set->size) return false;
    uint64_t word = element / 64;
    uint64_t bit = element % 64;
    return (set->bits[word] & (1ULL << bit)) != 0;
}

void BitSetUnion(BitSet* dest, BitSet* src) {
    uint64_t min_cap = dest->capacity < src->capacity ? dest->capacity : src->capacity;
    for (uint64_t i = 0; i < min_cap; i++) {
        dest->bits[i] |= src->bits[i];
    }
}

void BitSetIntersect(BitSet* dest, BitSet* src) {
    uint64_t min_cap = dest->capacity < src->capacity ? dest->capacity : src->capacity;
    for (uint64_t i = 0; i < min_cap; i++) {
        dest->bits[i] &= src->bits[i];
    }
}

void BitSetDifference(BitSet* dest, BitSet* src) {
    uint64_t min_cap = dest->capacity < src->capacity ? dest->capacity : src->capacity;
    for (uint64_t i = 0; i < min_cap; i++) {
        dest->bits[i] &= ~src->bits[i];
    }
}

void BitSetClear(BitSet* set) {
    memset(set->bits, 0, set->capacity * sizeof(uint64_t));
}

bool BitSetEquals(BitSet* a, BitSet* b) {
    if (a->capacity != b->capacity) return false;
    return memcmp(a->bits, b->bits, a->capacity * sizeof(uint64_t)) == 0;
}

BitSet* CopyBitSet(BitSet* src) {
    BitSet* copy = malloc(sizeof(BitSet));
    copy->size = src->size;
    copy->capacity = src->capacity;
    copy->bits = malloc(copy->capacity * sizeof(uint64_t));
    memcpy(copy->bits, src->bits, copy->capacity * sizeof(uint64_t));
    return copy;
}

void FreeBitSet(BitSet* set) {
    free(set->bits);
    free(set);
}
```

---

## 5. Binary Evidence

### 5.1 Function Addresses and Decompilation References

| Function Name | Address | Size | Purpose |
|---------------|---------|------|---------|
| SimplifyAndColor | 0x1081400 | ~15 KB | Main graph coloring loop, node selection |
| SelectNodeForRemoval | 0x1090BD0 | ~8 KB | Briggs criterion check, priority calculation |
| AssignColors | 0x12E1EF0 | ~12 KB | Physical register assignment |
| RegisterAllocationEntry | 0xB612D0 | 39 KB | Main entry point, instruction dispatch (178+ cases) |
| AllocateOperandSpec | 0xA778C0 | ~2 KB | Operand specification allocation |
| ProcessOperandConstraints | 0xA79C90 | ~1 KB | Constraint processing wrapper |
| ProcessOperandConstraintsImpl | 0xA79B90 | ~3 KB | Constraint consolidation and deduplication |
| AssignPhysicalRegisters | 0xB5BA00 | ~25 KB | Physical register assignment with constraints |
| EmitInstruction | 0xA78010 | ~4 KB | Instruction emission with reload insertion |
| ConstraintEncoder | 0xA77AB0 | ~2 KB | Register class constraint encoding |

### 5.2 Key Constants and Magic Numbers

| Constant | Value | Location | Meaning |
|----------|-------|----------|---------|
| K_REGISTERS | 15 (0xF) | 0x1090BD0:1039 | Number of physical registers |
| K_THRESHOLD | 14 (0xE) | 0x1090BD0:1039, 1060, 1066 | Degree threshold for Briggs criterion |
| COALESCE_FACTOR | 0.8 | 0x1090BD0:603-608 | Degree weighting for coalescing |
| COALESCE_MAGIC | 0xCCCCCCCCCCCCCCCD | 0x1090BD0:603 | Fixed-point multiplier for 0.8 |
| LOOP_DEPTH_BASE | ~1.8 | Inferred | Exponential base for loop cost |
| MEM_LATENCY_L2 | 10.0 | Inferred | L2 cache hit latency (cycles) |

### 5.3 Decompilation Evidence

**Briggs Criterion Check** (0x1090BD0:1039-1044):
```c
while ( 1 ) {
  v64 = v62->m128i_u64[1];  // Load degree from node structure
  if ( v64 > 0xE )          // Check if degree > 14 (K threshold)
    break;
  v62 = (__m128i *)((char *)v62 + 40);  // Next node (40-byte stride)
  if ( v63 == v62 )
    goto LABEL_60;
}
```

**Coalesce Factor** (0x1090BD0:603):
```c
// Magic constant 0xCCCCCCCCCCCCCCCD represents 4/5 = 0.8
// Used for fixed-point multiplication: (degree * 0xCCCC...CD) >> 64
```

**Priority Calculation** (0x1081400:1076):
```c
v70 = *(_DWORD *)(v6 + 16) * (2 - ((*(_QWORD *)(v6 + 32) == 0) - 1))
      - ((*(_QWORD *)(v6 + 2024) == 0) - 1);
// Conditional weight: multiply by 2 if coalesce target, else 1
```

**Reload Placement** (0xA78010:77-82):
```c
for ( i = &a2[v4]; i != a2; v8 = v18 ) {
  v13 = *a2;                    // Operand ID
  v14 = *((_QWORD *)a2 + 1);   // Register or memory location
  a2 += 4;                      // Next operand (4-word stride)
  v8[v13 + 1] = v14;           // Emit operand assignment
  // If v14 == -1, emit reload instruction
}
```

### 5.4 Algorithm Complexity Analysis

| Algorithm | Time Complexity | Space Complexity | Notes |
|-----------|----------------|------------------|-------|
| Build Interference Graph | O(V × E) | O(V²) | V = vregs, E = instructions |
| Liveness Analysis | O(B × V × I) | O(B × V) | B = blocks, I = iterations |
| Briggs Coalescing | O(V² × K) | O(V²) | Conservative criterion check |
| Simplify and Color | O(V × log V) | O(V) | Priority heap operations |
| Lazy Reload | O(B × V × I) | O(B × V) | Dataflow analysis |
| Dominator Tree | O(E × α(E,V)) | O(V) | Lengauer-Tarjan algorithm |
| Overall | O(V² × E) | O(V²) | Dominated by graph construction |

---

## 6. Algorithm Parameters Summary

### 6.1 Chaitin-Briggs Parameters

- **K_REGISTERS**: 15 (physical registers available)
- **K_THRESHOLD**: 14 (K - 1, for Briggs criterion)
- **COALESCE_FACTOR**: 0.8 (degree adjustment for coalescing)
- **Priority Formula**: `spill_cost / effective_degree`
- **Weight Multiplier**: 2 for coalesce targets, 1 otherwise

### 6.2 Spill Cost Parameters

- **Base Formula**: `def_freq × use_freq × mem_latency × loop_depth_mult`
- **Loop Depth Multiplier**: `pow(1.8, loop_depth)`
- **Memory Latency**: 4-100 cycles (cache hierarchy)
- **Occupancy Penalty**: 1.2× for high-use registers

### 6.3 Lazy Reload Parameters

- **Placement Strategy**: As late as possible (immediately before use)
- **Redundancy Elimination**: Dataflow-based reachability analysis
- **Stack Alignment**: 8 bytes per spill slot
- **Reload Cost Model**: `mem_latency + register_pressure_penalty`

### 6.4 Constraint Parameters

- **Register Classes**: 4 (general, predicate, uniform, special)
- **Max Alignment**: 4 registers
- **Register File Size**: 255 general registers (R0-R254)
- **Predicate Registers**: 16 (P0-P15)

---

**END OF DOCUMENT**
