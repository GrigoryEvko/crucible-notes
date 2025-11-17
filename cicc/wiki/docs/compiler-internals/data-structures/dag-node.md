# DAG Node - Instruction Scheduling Graph

## Structure Definitions

### DAGNode

Structure representing a single instruction node in the scheduling DAG.
Size: 60 bytes (0x3C). All instructions in a basic block get one DAGNode.

```c
struct DAGNode {
    // Instruction Reference
    IRNode*         instr;              // +0x00: Machine instruction pointer

    // Edge Lists (dual representation for efficient traversal)
    DAGEdge*        succs;              // +0x08: Successor edge list head
    DAGEdge*        preds;              // +0x10: Predecessor edge list head

    // Dependency Counts
    uint32_t        num_succs;          // +0x18: Number of successor edges
    uint32_t        num_preds;          // +0x1C: Number of predecessor edges

    // Latency Metrics (critical path information)
    uint32_t        critical_height;    // +0x20: Max latency from this node to exit (cycles)
                                        //        Used as PRIMARY scheduling priority
                                        //        Range: 0 (exit nodes) to total_makespan

    uint32_t        scheduled_height;   // +0x24: Max latency path from entry to this node
                                        //        Used as SECONDARY scheduling priority
                                        //        Range: 0 (entry nodes) to total_makespan

    // Scheduling Priority
    uint32_t        priority;           // +0x28: Composite 6-component scheduling priority
                                        //        Computed via ComputePriority():
                                        //        = critical_height*10000
                                        //        + scheduled_height*1000
                                        //        + reg_pressure_delta*100
                                        //        + live_use_count*10
                                        //        + no_stall_bonus*1
                                        //        + physreg_join_bonus*1

    // Register Pressure Metrics
    int32_t         reg_pressure_delta; // +0x2C: Delta in live register count if scheduled
                                        //        Negative = reduces register pressure (preferred)
                                        //        Positive = increases register pressure (avoided)

    uint32_t        live_use_count;     // +0x30: Count of live uses of this instruction's results
                                        //        Higher count = prioritize scheduling early

    // Schedule Assignment
    uint32_t        scheduled_cycle;    // +0x34: Assigned cycle post-scheduling
                                        //        Populated after list scheduling complete
                                        //        Range: 0 to schedule_length

    // Identification
    uint16_t        node_id;            // +0x38: Unique node identifier (0 to num_nodes-1)
                                        //        Used as tie-breaker in priority comparison

    // Traversal Control
    uint8_t         visited;            // +0x3A: Traversal marker bitmap
                                        //        Used during graph traversals (DFS, critical path)
                                        //        Prevents revisiting nodes in cycles

    // Node Classification Flags
    uint8_t         flags;              // +0x3B: Node property flags
                                        //        See flag definitions below
};  // Total Size: 0x3C (60 bytes)

// Structure Layout in Memory:
// 0x00-0x07: instruction pointer (8 bytes)
// 0x08-0x0F: successor list pointer (8 bytes)
// 0x10-0x17: predecessor list pointer (8 bytes)
// 0x18-0x1B: successor count (4 bytes)
// 0x1C-0x1F: predecessor count (4 bytes)
// 0x20-0x23: critical_height latency (4 bytes)
// 0x24-0x27: scheduled_height latency (4 bytes)
// 0x28-0x2B: priority value (4 bytes)
// 0x2C-0x2F: register pressure delta (4 bytes, signed)
// 0x30-0x33: live use count (4 bytes)
// 0x34-0x37: scheduled cycle (4 bytes)
// 0x38-0x39: node ID (2 bytes)
// 0x3A: visited bitmap (1 byte)
// 0x3B: flags (1 byte)

// Node flags
#define DAG_NODE_ENTRY          0x01    // No predecessors
#define DAG_NODE_EXIT           0x02    // No successors
#define DAG_NODE_ON_CRITICAL    0x04    // On critical path
#define DAG_NODE_SCHEDULED      0x08    // Cycle assigned
#define DAG_NODE_READY          0x10    // Dependencies satisfied
```

### DAGEdge

Represents a single directed dependency edge between two instructions.
Size: 20 bytes (0x14). Multiple edges per node for different dependencies.

```c
struct DAGEdge {
    DAGNode*        target;             // +0x00: Pointer to destination node
                                        //        The node that depends on source node

    DAGEdge*        next;               // +0x08: Next edge in linked list
                                        //        Allows efficient traversal of all edges
                                        //        from a single node

    uint16_t        latency;            // +0x10: Edge weight in cycles
                                        //        Latency that consumer must wait after producer
                                        //        Type-dependent calculation (see below):
                                        //        - True dep (RAW): instruction latency (1-25+ cycles)
                                        //        - Output (WAW): 1 cycle (serialization)
                                        //        - Anti (WAR): 1 cycle (breakable)
                                        //        - Control: 0 cycles (ordering only)
                                        //        - Memory: latency based on alias analysis

    uint8_t         type;               // +0x12: Dependency type (see type flags below)
                                        //        Single byte with bits for:
                                        //        DEP_TRUE, DEP_OUTPUT, DEP_ANTI, DEP_CONTROL, DEP_MEMORY
                                        //        Can have multiple bits set for same edge

    uint8_t         flags;              // +0x13: Edge property flags
                                        //        EDGE_ARTIFICIAL: Compiler-inserted for correctness
                                        //        EDGE_BREAKABLE: Can be removed (anti-deps)
                                        //        EDGE_CRITICAL: Part of critical path
};  // Total Size: 0x14 (20 bytes)

// Edge List Traversal Pattern:
// for (DAGEdge* e = node->succs; e != NULL; e = e->next) {
//     DAGNode* consumer = e->target;
//     uint16_t dep_latency = e->latency;
//     uint8_t dep_type = e->type;
// }

// Edge types (dependency classes)
#define DEP_TRUE        0x01    // RAW: Read-After-Write (data dependency)
#define DEP_OUTPUT      0x02    // WAW: Write-After-Write
#define DEP_ANTI        0x04    // WAR: Write-After-Read
#define DEP_CONTROL     0x08    // Control flow dependency
#define DEP_MEMORY      0x10    // Memory ordering constraint

// Edge flags
#define EDGE_ARTIFICIAL     0x01    // Compiler-inserted
#define EDGE_BREAKABLE      0x02    // Anti-dep breakable
#define EDGE_CRITICAL       0x04    // On critical path
```

## Priority Computation

### 6-Component Priority Formula (list-ilp)

This is the primary priority function used in the list-ilp scheduler (address: 0x1D04DC0).
Each component can be individually disabled via compiler flags.

```c
uint32_t ComputePriority(DAGNode* node) {
    uint32_t pri = 0;

    // Component 1: CRITICAL_PATH_PRIORITY (PRIMARY)
    // Calculation: critical_height (max latency from node to exit)
    // Weight: 10000x (dominates all other components)
    // Flag: disable-sched-critical-path
    // Enabled: true (default)
    // Purpose: Schedule critical path nodes first to minimize makespan
    if (!is_disabled("disable-sched-critical-path")) {
        pri += node->critical_height * 10000;
    }

    // Component 2: SCHEDULED_HEIGHT_PRIORITY (SECONDARY)
    // Calculation: max_latency_path_from_instruction
    // Weight: 1000x (secondary latency metric)
    // Flag: disable-sched-height
    // Enabled: true (default)
    // Purpose: Schedule high-latency chains early
    if (!is_disabled("disable-sched-height")) {
        pri += node->scheduled_height * 1000;
    }

    // Component 3: REGISTER_PRESSURE_PRIORITY (TERTIARY)
    // Calculation: live_range_length = def_cycle - last_use_cycle
    // Weight: 100x
    // Flag: disable-sched-reg-pressure
    // Enabled: true (default)
    // Goal: minimize (MAX_LIVE_RANGE - live_range) to reduce register demand
    if (!is_disabled("disable-sched-reg-pressure")) {
        int32_t live_range = node->last_use - node->first_def;
        pri += (MAX_LIVE_RANGE - live_range) * 100;
    }

    // Component 4: LIVE_USE_PRIORITY
    // Calculation: number_of_live_uses_of_instruction
    // Weight: 10x
    // Flag: disable-sched-live-uses
    // Enabled: true (default)
    // Purpose: Schedule uses close to definitions to shorten live ranges
    if (!is_disabled("disable-sched-live-uses")) {
        pri += node->live_use_count * 10;
    }

    // Component 5: NO_STALL_PRIORITY
    // Calculation: can_execute_without_stall(node) → 1 if available, 0 otherwise
    // Weight: 1x
    // Flag: disable-sched-stalls
    // Enabled: true (default)
    // Purpose: Avoid execution unit stalls by preferring executable instructions
    if (!is_disabled("disable-sched-stalls")) {
        pri += CanExecuteWithoutStall(node) ? 1 : 0;
    }

    // Component 6: PHYSICAL_REG_JOIN_PRIORITY
    // Calculation: has_physreg_join_opportunity(node) → 1 if reuse, 0 otherwise
    // Weight: 1x
    // Flag: disable-sched-physreg-join
    // Enabled: true (default)
    // Purpose: Improve register allocation quality via physical register reuse
    if (!is_disabled("disable-sched-physreg-join")) {
        pri += HasPhysregJoinOpportunity(node) ? 1 : 0;
    }

    return pri;
}

// Priority Ordering: Higher value = higher scheduling priority
// Decimal Weight Breakdown: 10000 > 1000 > 100 > 10 > 1 > 1
// Ties broken by node_id for determinism (node with lower ID scheduled first)

// Relative Component Strength:
// Critical Path      : 10000x (absolute dominant - one critical path unit worth 10000 base units)
// Scheduled Height   : 1000x  (10x weaker than critical path)
// Register Pressure  : 100x   (100x weaker than critical path)
// Live Uses          : 10x    (1000x weaker than critical path)
// No-Stall           : 1x     (minimal tie-breaker)
// PhysReg Join       : 1x     (minimal tie-breaker)
```

### Ready Queue Structure

The ready queue is a priority queue managing instructions whose dependencies are satisfied:

```c
struct ReadyQueue {
    DAGNode**   nodes;          // Dynamic array of ready instructions
    uint32_t    num_ready;      // Current queue size
    uint32_t    capacity;       // Allocated capacity

    // Comparison function for priority ordering
    // Returns: nodes[i]->priority > nodes[j]->priority
    // If priorities equal: node[i]->node_id < node[j]->node_id (tie-break)
};

// Insertion: O(log n) heap insertion maintaining priority order
// Pop: O(1) front access (highest priority), O(log n) rebalance
// Total scheduling: O(n log n) across all ready queue operations
```

### Configuration Parameters for Priority Components

| Parameter | Type | Default | Effect |
|-----------|------|---------|--------|
| disable-sched-critical-path | bool | false | Disables component 1 (critical path) |
| disable-sched-height | bool | false | Disables component 2 (scheduled height) |
| disable-sched-reg-pressure | bool | false | Disables component 3 (register pressure) |
| disable-sched-live-uses | bool | false | Disables component 4 (live uses) |
| disable-sched-stalls | bool | false | Disables component 5 (no-stall) |
| disable-sched-physreg-join | bool | false | Disables component 6 (physreg join) |
| disable-sched-vrcycle | bool | false | Virtual register cycle interference check |
| max-sched-reorder | int | 6 | Instructions allowed ahead of critical path |
| sched-high-latency-cycles | int | 25 | Fallback latency for unknown instructions |

### Critical Height Computation

```c
// Bottom-up DP: O(V + E)
uint32_t ComputeCriticalHeight(DAGNode* node, uint8_t* visited) {
    if (visited[node->node_id]) {
        return node->critical_height;
    }

    // Exit nodes: height = 0
    if (node->flags & DAG_NODE_EXIT) {
        node->critical_height = 0;
        visited[node->node_id] = 1;
        return 0;
    }

    // Compute max over all successors
    uint32_t max_height = 0;
    for (DAGEdge* e = node->succs; e != NULL; e = e->next) {
        uint32_t succ_height = ComputeCriticalHeight(e->target, visited);
        uint32_t height_via_succ = succ_height + e->latency;
        if (height_via_succ > max_height) {
            max_height = height_via_succ;
            if (succ_height == 0 || (e->target->flags & DAG_NODE_ON_CRITICAL)) {
                node->flags |= DAG_NODE_ON_CRITICAL;
            }
        }
    }

    node->critical_height = max_height;
    visited[node->node_id] = 1;
    return max_height;
}

// Complexity: O(V + E) with memoization
// Space: O(V) for visited bitmap
```

## Latency Information Per Node

Each DAGNode maintains latency information for critical path calculation:

```c
struct NodeLatencyInfo {
    // Direct latency of the instruction itself
    uint16_t        instr_latency;      // Cycles for this instruction to complete
                                        // Source: InstrItineraryData lookup
                                        // Fallback: sched-high-latency-cycles (default: 25)

    // Critical path metrics (computed during DAG construction)
    uint32_t        critical_height;    // Max latency to exit nodes (PRIMARY priority)
    uint32_t        scheduled_height;   // Max latency from entry nodes (SECONDARY priority)

    // Slack computation for anti-dependency breaking
    uint32_t        slack_to_critical;  // critical_path_len - critical_height
                                        // How much delay is tolerable without extending schedule
};

// Latency Sources:
// 1. InstrItineraryData (machine model) - most accurate
// 2. Fallback: sched-high-latency-cycles parameter (default: 25 cycles)
// 3. Per-edge latency stored in DAGEdge.latency field
```

### Successor/Predecessor List Structures

Nodes use dual linked-list representation for efficient bidirectional traversal:

```c
// Successor List (successors array / linked list)
// Connected to instructions that depend on this node
struct SuccessorList {
    DAGEdge*        head;               // Points to first successor edge
    uint32_t        count;              // Number of successors (cached in num_succs)

    // Traversal:
    // for (DAGEdge* e = node->succs; e != NULL; e = e->next) {
    //     DAGNode* consumer = e->target;  // Instruction depending on this node
    //     uint32_t latency = e->latency;  // How long consumer must wait
    // }
};

// Predecessor List (predecessors array / linked list)
// Connected to instructions this node depends on
struct PredecessorList {
    DAGEdge*        head;               // Points to first predecessor edge
    uint32_t        count;              // Number of predecessors (cached in num_preds)

    // Traversal:
    // for (DAGEdge* e = node->preds; e != NULL; e = e->next) {
    //     DAGNode* producer = e->target;  // Instruction this node depends on
    //     uint32_t latency = e->latency;  // How long to wait after producer
    // }
};

// Ready Condition: All edges in predecessor list are satisfied
// Edge satisfied when: producer scheduled_cycle + producer latency <= consumer scheduled_cycle
```

## Edge Weight Computation

### Latency Assignment

```c
uint16_t ComputeEdgeLatency(DAGNode* producer, DAGNode* consumer, uint8_t dep_type) {
    uint16_t latency = 0;

    switch (dep_type) {
        case DEP_TRUE:
            // RAW: Full instruction latency
            latency = GetInstrLatency(producer->instr);
            if (latency == 0) {
                // Fallback: sched-high-latency-cycles (default: 25)
                latency = 25;
            }
            break;

        case DEP_OUTPUT:
            // WAW: Serialization penalty
            latency = 1;
            break;

        case DEP_ANTI:
            // WAR: Minimal serialization (breakable)
            latency = 1;
            break;

        case DEP_CONTROL:
            // Control flow: No latency penalty
            latency = 0;
            break;

        case DEP_MEMORY:
            // Memory ordering: Conservative latency
            latency = GetMemoryLatency(producer->instr, consumer->instr);
            break;
    }

    return latency;
}

// InstrItineraryData lookup (machine model)
uint16_t GetInstrLatency(IRNode* instr) {
    // Query instruction schedules/itinerary tables
    // Returns functional unit latency + pipeline stages
    // Accounts for resource reservation times
}
```

### Dependency Detection

```c
uint8_t DetectDependencyType(IRNode* producer, IRNode* consumer) {
    uint8_t deps = 0;

    // Scan producer defs vs consumer uses
    for (Operand* def = producer->defs; def; def = def->next) {
        for (Operand* use = consumer->uses; use; use = use->next) {
            if (SameRegister(def->reg, use->reg)) {
                deps |= DEP_TRUE;  // RAW
            }
        }
        for (Operand* cdef = consumer->defs; cdef; cdef = cdef->next) {
            if (SameRegister(def->reg, cdef->reg)) {
                deps |= DEP_OUTPUT;  // WAW
            }
        }
    }

    // Scan producer uses vs consumer defs
    for (Operand* use = producer->uses; use; use = use->next) {
        for (Operand* cdef = consumer->defs; cdef; cdef = cdef->next) {
            if (SameRegister(use->reg, cdef->reg)) {
                deps |= DEP_ANTI;  // WAR
            }
        }
    }

    // Control dependencies
    if (IsControlFlow(producer) && !IsSpeculatable(consumer)) {
        deps |= DEP_CONTROL;
    }

    // Memory dependencies (conservative window analysis)
    if (IsMemoryOp(producer) && IsMemoryOp(consumer)) {
        if (MayAlias(producer, consumer)) {
            deps |= DEP_MEMORY;
        }
    }

    return deps;
}
```

## DAG Construction Algorithm

```c
struct DAG {
    DAGNode**   nodes;              // Node array (indexed by instr ID)
    uint32_t    num_nodes;
    DAGNode*    entry_nodes;        // List of nodes with no preds
    DAGNode*    exit_nodes;         // List of nodes with no succs
    uint32_t    critical_path_len;  // Makespan
};

DAG* ConstructDAG(MachineBasicBlock* mbb) {
    DAG* dag = AllocateDAG(mbb->num_instrs);

    // Phase 1: Optional topological sort (topo-sort-begin = true)
    if (Config.topo_sort_begin) {
        TopologicalSort(mbb);
    }

    // Phase 2: Create node per instruction
    uint32_t id = 0;
    for (IRNode* instr = mbb->first; instr; instr = instr->next) {
        DAGNode* node = AllocateNode();
        node->instr = instr;
        node->node_id = id++;
        node->succs = NULL;
        node->preds = NULL;
        node->num_succs = 0;
        node->num_preds = 0;
        node->flags = 0;
        dag->nodes[node->node_id] = node;
    }
    dag->num_nodes = id;

    // Phase 3: Build dependency edges (window-based analysis)
    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node_i = dag->nodes[i];

        // Look ahead in limited window (memory_dep_window = 100 instrs)
        uint32_t window_end = MIN(i + 100, dag->num_nodes);
        for (uint32_t j = i + 1; j < window_end; j++) {
            DAGNode* node_j = dag->nodes[j];

            uint8_t dep_type = DetectDependencyType(node_i->instr, node_j->instr);
            if (dep_type != 0) {
                AddEdge(dag, node_i, node_j, dep_type);
            }
        }
    }

    // Phase 4: Compute edge weights
    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node = dag->nodes[i];
        for (DAGEdge* e = node->succs; e; e = e->next) {
            e->latency = ComputeEdgeLatency(node, e->target, e->type);

            // Mark breakable anti-dependencies
            if (e->type & DEP_ANTI) {
                e->flags |= EDGE_BREAKABLE;
            }
        }
    }

    // Phase 5: Identify entry/exit nodes
    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node = dag->nodes[i];
        if (node->num_preds == 0) {
            node->flags |= DAG_NODE_ENTRY;
            // Link into entry list
        }
        if (node->num_succs == 0) {
            node->flags |= DAG_NODE_EXIT;
            // Link into exit list
        }
    }

    // Phase 6: Compute critical heights (bottom-up from exits)
    uint8_t* visited = AllocateVisitedBitmap(dag->num_nodes);
    memset(visited, 0, dag->num_nodes);

    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        ComputeCriticalHeight(dag->nodes[i], visited);
    }

    // Phase 7: Find critical path length
    dag->critical_path_len = 0;
    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        if (dag->nodes[i]->flags & DAG_NODE_ENTRY) {
            if (dag->nodes[i]->critical_height > dag->critical_path_len) {
                dag->critical_path_len = dag->nodes[i]->critical_height;
            }
        }
    }

    free(visited);
    return dag;
}

void AddEdge(DAG* dag, DAGNode* src, DAGNode* dst, uint8_t type) {
    DAGEdge* edge = AllocateEdge();
    edge->target = dst;
    edge->type = type;
    edge->flags = 0;
    edge->latency = 0;  // Computed later

    // Insert at head of successor list
    edge->next = src->succs;
    src->succs = edge;
    src->num_succs++;

    // Also add to predecessor list (dual representation)
    DAGEdge* pred_edge = AllocateEdge();
    pred_edge->target = src;
    pred_edge->type = type;
    pred_edge->flags = 0;
    pred_edge->latency = 0;
    pred_edge->next = dst->preds;
    dst->preds = pred_edge;
    dst->num_preds++;
}
```

## Critical Path Detection

### Algorithm: Bottom-Up Longest Path DP

```c
// Topological traversal: reverse order (exits → entries)
void ComputeAllCriticalHeights(DAG* dag) {
    uint8_t* visited = calloc(dag->num_nodes, 1);

    // Process in reverse topological order
    for (int i = dag->num_nodes - 1; i >= 0; i--) {
        ComputeCriticalHeight(dag->nodes[i], visited);
    }

    free(visited);
}

// Mark critical path nodes
void MarkCriticalPath(DAG* dag) {
    // Find entry node(s) on critical path
    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node = dag->nodes[i];
        if ((node->flags & DAG_NODE_ENTRY) &&
            node->critical_height == dag->critical_path_len) {

            node->flags |= DAG_NODE_ON_CRITICAL;
            MarkCriticalSuccessors(node);
        }
    }
}

void MarkCriticalSuccessors(DAGNode* node) {
    for (DAGEdge* e = node->succs; e; e = e->next) {
        uint32_t expected_height = e->target->critical_height + e->latency;
        if (expected_height == node->critical_height) {
            e->flags |= EDGE_CRITICAL;
            e->target->flags |= DAG_NODE_ON_CRITICAL;
            MarkCriticalSuccessors(e->target);
        }
    }
}

// Slack computation
uint32_t ComputeSlack(DAGNode* node, uint32_t critical_path_len) {
    // Slack = how many cycles node can delay without extending makespan
    return critical_path_len - node->critical_height;
}
```

### Time Complexity

| Operation | Complexity | Notes |
|-----------|------------|-------|
| DAG construction | O(n × w) | n = instrs, w = window size (100) |
| Critical height | O(V + E) | V = nodes, E = edges |
| Priority computation | O(1) per node | Precomputed heights |
| Ready list insertion | O(log n) | Priority queue |
| Total scheduling | O(n log n) | List scheduling |

## Scheduling Heuristics (7 Confirmed Variants)

### Pre-RA Schedulers (4 variants)

#### 1. list-burr: Bottom-up Register Reduction
```c
// Address: 0x1D05200
// Strategy: Minimize register pressure by prioritizing instructions
//          with shorter live ranges (instructions exiting registers first)
// Priority: live_range_end - live_range_start
// Use Case: General-purpose code with tight register budgets
struct BURRScheduler {
    char name[32];        // "Bottom-up Register Reduction"
    uint32_t priority_metric;  // Live range length
    bool bottom_up;            // true - traverse reverse topological
    bool register_aware;       // true - prioritize register release
};
```

#### 2. source: Source Order Preserving
```c
// Address: 0x1D05510
// Strategy: Schedule in source order when possible while respecting
//          dependencies and register constraints
// Priority: source_position + minimal_register_pressure_adjustment
// Use Case: Cache optimization, semantically important source order
struct SourceScheduler {
    char name[32];        // "Source Order List Scheduling"
    uint32_t order_bias;       // Prefer source position
    bool register_aware;       // Fallback to pressure when needed
};
```

#### 3. list-hybrid: Latency + Register Pressure Balancing
```c
// Address: 0x1D05820
// Strategy: Balance between latency hiding and register pressure reduction
// Priority: (latency_weight * 0.5) + (register_pressure_weight * 0.5)
// Use Case: Mixed workloads with both latency and register pressure concerns
struct HybridScheduler {
    char name[32];        // "Hybrid List Scheduling"
    float latency_weight;      // 0.5
    float pressure_weight;     // 0.5
    bool adaptive_weighting;   // true
};
```

#### 4. list-ilp: Instruction Level Parallelism (Primary)
```c
// Address: 0x1D04DC0
// Strategy: Maximize ILP while controlling register pressure
// 6-Component Priority Function (see section below)
// Use Case: High-throughput codes with instruction parallelism opportunities
struct ILPScheduler {
    char name[32];        // "Instruction Level Parallelism List Scheduling"
    uint32_t ilp_metric;       // successor_dependency_count
    bool bottom_up;            // true
    uint8_t priority_components; // 6 components
};
```

### Post-RA Schedulers (3 variants)

#### 5. converge: Standard Converging Scheduler
```c
// Address: 0x1E76F50 (thunk), actual: 0x1E76650
// Strategy: Hide memory and compute latency by converging toward critical uses
// Priority: latency_distance_to_nearest_use
// Use Case: Memory-latency sensitive, general-purpose code
struct ConvergeScheduler {
    char name[32];        // "Standard Converging Scheduler"
    bool latency_hiding;       // true
    bool critical_aware;       // true
};
```

#### 6. ilpmax: Maximum Instruction Level Parallelism
```c
// Address: 0x1E6ECD0
// Strategy: Schedule bottom-up to maximize instruction level parallelism
// Priority: successor_count + immediate_dependencies
// Use Case: ILP-rich codes, CPU with multiple execution units
struct ILPMaxScheduler {
    char name[32];        // "Maximum ILP Scheduler"
    bool bottom_up;            // true
    bool maximize_ilp;         // true
};
```

#### 7. ilpmin: Minimum Instruction Level Parallelism
```c
// Address: 0x1E6EC30
// Strategy: Schedule bottom-up to minimize instruction level parallelism
// Priority: successor_count - penalty_for_parallelism
// Use Case: Power-constrained systems, resource-contention scenarios
struct ILPMinScheduler {
    char name[32];        // "Minimum ILP Scheduler"
    bool bottom_up;            // true
    bool minimize_ilp;         // true
};
```

## Binary Evidence

### Scheduler Implementations

```
PreRA Schedulers:
0x1D04DC0  sub_1D04DC0  list-ilp    (6-component priority)
0x1D05200  sub_1D05200  list-burr   (register pressure)
0x1D05510  sub_1D05510  source      (source order)
0x1D05820  sub_1D05820  list-hybrid (latency + pressure)

PostRA Schedulers:
0x1E76F50  sub_1E76F50  converge    (latency hiding)
0x1E6ECD0  sub_1E6ECD0  ilpmax      (max parallelism)
0x1E6EC30  sub_1E6EC30  ilpmin      (min parallelism)

Registration:
0x4F8F80   ctor_282_0_0x4f8f80.c    PreRA scheduler table
0x500AD0   ctor_310_0_0x500ad0.c    PostRA scheduler table
0x599EF0   ctor_652_0_0x599ef0.c    Priority flags registration
```

### Configuration Parameters

**Source Files** (priority component registrations):
```
ctor_282_0_0x4f8f80.c - Pre-RA scheduler priority registration
ctor_310_0_0x500ad0.c - Post-RA scheduler and critical path configuration
ctor_652_0_0x599ef0.c - Scheduling algorithm selection and advanced parameters
ctor_283_0x4f9b60.c  - Latency configuration
ctor_314_0x502360.c  - Recurrence chain analysis parameters
```

**Priority Component Disabling** (ctor_282_0_0x4f8f80.c):
```
Line 39:  disable-sched-reg-pressure       Disables register pressure component
Line 55:  disable-sched-live-uses          Disables live use priority component
Line 70:  disable-sched-stalls             Disables no-stall priority component
Line 89:  disable-sched-critical-path      Disables critical path component
Line 106: disable-sched-height             Disables scheduled height component
Line 123: disable-sched-vrcycle            Disables virtual register cycle check
Line 140: disable-sched-physreg-join       Disables physical register join component
```

**Critical Path Analysis** (ctor_310_0_0x500ad0.c):
```
Line 102: print-sched-critical             Debug: prints critical path length to stdout
Line 195: enable-cyclic-critical-path      Enables analysis for loop-carried dependencies
```

**Scheduling Control** (ctor_652_0_0x599ef0.c):
```
Line 216: disable-sched-critical-path      Disables critical path priority (pre-RA)
Line 229: disable-sched-height             Disables scheduled height priority
Line 315: max-sched-reorder (default: 6)   Max instructions allowed ahead of critical path
```

**Latency Configuration** (ctor_283_0x4f9b60.c):
```
sched-high-latency-cycles (default: 25)    Fallback latency for unknown instructions
                                           Used when InstrItineraryData unavailable
```

**Recurrence Analysis** (ctor_314_0x502360.c):
```
recurrence-chain-limit (default: 3)        Max chain length for operand commutation analysis
                                           Determines when to commute operands to break cycles
```

## Scheduling Phases

The compiler uses multi-stage scheduling for optimal instruction ordering:

### Phase 1: Pre-Register Allocation (Pre-RA) Scheduling
```c
struct PreRAScheduling {
    char phase_name[] = "PreRA Machine Instruction Scheduling";
    bool enabled;                          // enable-misched (default: true)
    uint32_t num_variants;                 // 4 scheduler variants available

    // Available schedulers:
    char variant_1[] = "list-ilp";         // ILP-aware (6 priority components) - PRIMARY
    char variant_2[] = "list-burr";        // Register pressure reduction
    char variant_3[] = "list-hybrid";      // Latency + pressure balancing
    char variant_4[] = "source";           // Source order preserving

    // Purpose: Reorder instructions before register allocation
    // Considers: potential register impact, instruction latency, parallelism
    // Output: Scheduled instruction sequence for RA
};
```

### Phase 2: Register Allocation
```c
struct RegisterAllocation {
    char phase_name[] = "Register Allocation";
    // Assigns physical registers to virtual registers
    // Uses schedule hints from PreRA
};
```

### Phase 3: Post-Register Allocation (Post-RA) Scheduling
```c
struct PostRAScheduling {
    char phase_name[] = "PostRA Machine Instruction Scheduling";
    bool enabled;                          // enable-post-misched (default: true)
    uint32_t num_variants;                 // 3 scheduler variants available

    // Available schedulers:
    char variant_1[] = "converge";         // Latency hiding (PRIMARY)
    char variant_2[] = "ilpmax";           // Maximum ILP
    char variant_3[] = "ilpmin";           // Minimum ILP

    // Anti-dependency breaking options:
    enum BreakMode {
        BREAK_NONE      = 0,               // Default: no breaking
        BREAK_CRITICAL  = 1,               // Break only on critical path (slack=0)
        BREAK_ALL       = 2                // Aggressive: break all anti-deps
    } anti_dep_mode;

    // Purpose: Final instruction ordering after register allocation
    // Considers: actual register values, memory latency, execution units
    // Output: Scheduled machine instructions
};
```

### Decompiled Snippets

```c
// ctor_282_0_0x4f8f80.c:18 - list-burr registration
*(void **)(v1 + 96) = &sub_1D05200;  // Bottom-up register reduction

// ctor_282_0_0x4f8f80.c:30 - list-ilp registration
*(void **)(v1 + 112) = &sub_1D04DC0; // ILP + register pressure

// ctor_310_0_0x500ad0.c:334 - converge scheduler
v61 = sub_1E76650;                   // Actual implementation
v60 = sub_1E76F50;                   // Thunk

// ctor_310_0_0x500ad0.c:336 - ilpmax scheduler
*(_BYTE *)(v1 + 32) = 1;             // Bottom-up flag = true

// ctor_310_0_0x500ad0.c:338 - ilpmin scheduler
*(_BYTE *)(v1 + 32) = 0;             // Bottom-up flag = false
```

## Anti-Dependency Breaking

### Critical Path Integration

```c
void BreakAntiDependencies(DAG* dag, BreakMode mode) {
    if (mode == BREAK_NONE) return;

    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node = dag->nodes[i];

        // Iterate over predecessor edges
        DAGEdge* prev = NULL;
        for (DAGEdge* e = node->preds; e; ) {
            if (!(e->type & DEP_ANTI)) {
                prev = e;
                e = e->next;
                continue;
            }

            bool should_break = false;

            if (mode == BREAK_ALL) {
                should_break = true;
            } else if (mode == BREAK_CRITICAL) {
                // Break only if producer on critical path (slack = 0)
                uint32_t slack = ComputeSlack(e->target, dag->critical_path_len);
                should_break = (slack == 0);
            }

            if (should_break && (e->flags & EDGE_BREAKABLE)) {
                // Remove edge from both successor and predecessor lists
                RemoveEdge(dag, e->target, node, e);
                DAGEdge* next = e->next;
                free(e);
                e = next;
                node->num_preds--;
            } else {
                prev = e;
                e = e->next;
            }
        }
    }
}

enum BreakMode {
    BREAK_NONE,      // Default: no breaking
    BREAK_CRITICAL,  // Break anti-deps on critical path only
    BREAK_ALL        // Aggressive: break all anti-deps
};
```

## Memory Dependency Window

```c
#define MEMORY_DEP_WINDOW_INSTRS  100
#define MEMORY_DEP_WINDOW_BLOCKS  200

bool MayAlias(IRNode* producer, IRNode* consumer) {
    // Conservative analysis within window
    // Returns true if cannot prove independence

    if (!IsMemoryOp(producer) || !IsMemoryOp(consumer)) {
        return false;
    }

    // Distance check
    uint32_t distance = consumer->node_id - producer->node_id;
    if (distance > MEMORY_DEP_WINDOW_INSTRS) {
        return false;  // Outside analysis window
    }

    // Alias analysis
    MemoryLocation loc1 = GetMemoryLocation(producer);
    MemoryLocation loc2 = GetMemoryLocation(consumer);

    return !CanProveNoAlias(loc1, loc2);
}
```

## Recurrence Chain Analysis

```c
#define RECURRENCE_CHAIN_LIMIT  3

// Detect cyclic dependencies for operand commutation
uint32_t AnalyzeRecurrenceChain(DAGNode* start, DAGNode* current,
                                 uint32_t depth, uint8_t* visited) {
    if (depth > RECURRENCE_CHAIN_LIMIT) {
        return depth;
    }

    if (current == start && depth > 0) {
        return depth;  // Found cycle
    }

    if (visited[current->node_id]) {
        return 0;
    }

    visited[current->node_id] = 1;
    uint32_t max_chain = 0;

    for (DAGEdge* e = current->succs; e; e = e->next) {
        if (e->type & (DEP_TRUE | DEP_OUTPUT)) {
            uint32_t chain_len = AnalyzeRecurrenceChain(start, e->target,
                                                         depth + 1, visited);
            if (chain_len > max_chain) {
                max_chain = chain_len;
            }
        }
    }

    visited[current->node_id] = 0;
    return max_chain;
}

// Determine if operand commutation would break recurrence
bool ShouldCommuteOperands(IRNode* instr) {
    if (!IsCommutative(instr)) return false;

    uint8_t visited[MAX_NODES] = {0};
    DAGNode* node = GetDAGNode(instr);
    uint32_t chain_len = AnalyzeRecurrenceChain(node, node, 0, visited);

    // Commute if recurrence chain exceeds limit
    return chain_len >= RECURRENCE_CHAIN_LIMIT;
}
```

## Resource Usage Bitmap

```c
// Per-node resource reservation tracking
struct ResourceUsage {
    uint64_t  functional_units;     // Bit per FU (ALU, FPU, MEM, etc.)
    uint32_t  reservation_table[8]; // Cycle-level reservation (per FU)
    uint16_t  throughput;           // Instructions per cycle
    uint8_t   issue_width;          // Max parallel issue
};

void ComputeResourceUsage(DAGNode* node) {
    ResourceUsage* ru = &node->resource_usage;
    IRNode* instr = node->instr;

    // Query instruction itinerary
    InstrItinerary* itinerary = GetItinerary(instr);

    ru->functional_units = 0;
    memset(ru->reservation_table, 0, sizeof(ru->reservation_table));

    // Mark functional units used
    for (uint32_t stage = 0; stage < itinerary->num_stages; stage++) {
        FUReservation* res = &itinerary->stages[stage];
        ru->functional_units |= (1ULL << res->fu_id);

        // Reserve cycles in table
        for (uint32_t cycle = 0; cycle < res->cycles; cycle++) {
            uint32_t abs_cycle = res->start_cycle + cycle;
            if (abs_cycle < 8) {
                ru->reservation_table[abs_cycle] |= (1 << res->fu_id);
            }
        }
    }

    ru->throughput = itinerary->throughput;
    ru->issue_width = itinerary->issue_width;
}
```

## Scheduling Lookahead

```c
#define MAX_SCHED_REORDER  6  // Default lookahead limit

// Allow non-critical instructions to be scheduled ahead of critical path
bool CanScheduleAhead(DAGNode* node, DAGNode* critical_node,
                       uint32_t num_ahead) {
    if (num_ahead >= MAX_SCHED_REORDER) {
        return false;  // Lookahead limit reached
    }

    // Allow if not on critical path and reduces register pressure
    if (!(node->flags & DAG_NODE_ON_CRITICAL) &&
        node->reg_pressure_delta < 0) {
        return true;
    }

    return false;
}
```

## DAG Metrics

```c
struct DAGMetrics {
    uint32_t  critical_path_length;   // Makespan (cycles)
    uint32_t  num_nodes;               // Instruction count
    uint32_t  num_edges;               // Dependency count
    uint32_t  avg_degree;              // Avg edges per node
    uint32_t  max_parallelism;         // Max independent instrs
    uint32_t  peak_register_pressure;  // Max live values
    uint32_t  num_anti_deps;           // Anti-dependency count
    uint32_t  num_anti_deps_broken;    // Anti-deps removed
};

void ComputeDAGMetrics(DAG* dag, DAGMetrics* metrics) {
    metrics->num_nodes = dag->num_nodes;
    metrics->critical_path_length = dag->critical_path_len;

    uint32_t total_edges = 0;
    uint32_t anti_deps = 0;

    for (uint32_t i = 0; i < dag->num_nodes; i++) {
        DAGNode* node = dag->nodes[i];
        total_edges += node->num_succs;

        for (DAGEdge* e = node->succs; e; e = e->next) {
            if (e->type & DEP_ANTI) {
                anti_deps++;
            }
        }
    }

    metrics->num_edges = total_edges;
    metrics->avg_degree = total_edges / dag->num_nodes;
    metrics->num_anti_deps = anti_deps;
}
```
