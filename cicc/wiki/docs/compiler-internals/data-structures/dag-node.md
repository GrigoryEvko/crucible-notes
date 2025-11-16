# DAG Node - Instruction Scheduling Graph

## Structure Definitions

### DAGNode

```c
struct DAGNode {
    IRNode*         instr;              // +0x00: Machine instruction reference
    DAGEdge*        succs;              // +0x08: Successor edge list head
    DAGEdge*        preds;              // +0x10: Predecessor edge list head
    uint32_t        num_succs;          // +0x18: Successor count
    uint32_t        num_preds;          // +0x1C: Predecessor count
    uint32_t        critical_height;    // +0x20: Max latency to exit node (cycles)
    uint32_t        scheduled_height;   // +0x24: Max latency from entry
    uint32_t        priority;           // +0x28: Composite scheduling priority
    int32_t         reg_pressure_delta; // +0x2C: Live register count change
    uint32_t        live_use_count;     // +0x30: Number of live uses
    uint32_t        scheduled_cycle;    // +0x34: Assigned cycle (post-schedule)
    uint16_t        node_id;            // +0x38: Unique node identifier
    uint8_t         visited;            // +0x3A: Traversal marker bitmap
    uint8_t         flags;              // +0x3B: Node flags (ENTRY|EXIT|CRITICAL)
};  // Size: 0x3C (60 bytes)

// Node flags
#define DAG_NODE_ENTRY          0x01    // No predecessors
#define DAG_NODE_EXIT           0x02    // No successors
#define DAG_NODE_ON_CRITICAL    0x04    // On critical path
#define DAG_NODE_SCHEDULED      0x08    // Cycle assigned
#define DAG_NODE_READY          0x10    // Dependencies satisfied
```

### DAGEdge

```c
struct DAGEdge {
    DAGNode*        target;             // +0x00: Destination node
    DAGEdge*        next;               // +0x08: Next edge in list
    uint16_t        latency;            // +0x10: Edge weight (cycles)
    uint8_t         type;               // +0x12: Dependency type
    uint8_t         flags;              // +0x13: Edge flags
};  // Size: 0x14 (20 bytes)

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

```c
uint32_t ComputePriority(DAGNode* node) {
    uint32_t pri = 0;

    // Component 1: Critical path priority (PRIMARY)
    // Weight: ~10000x base unit
    if (!is_disabled("disable-sched-critical-path")) {
        pri += node->critical_height * 10000;
    }

    // Component 2: Scheduled height (SECONDARY)
    // Weight: ~1000x base unit
    if (!is_disabled("disable-sched-height")) {
        pri += node->scheduled_height * 1000;
    }

    // Component 3: Register pressure reduction
    // Weight: ~100x base unit
    if (!is_disabled("disable-sched-reg-pressure")) {
        int32_t live_range = node->last_use - node->first_def;
        pri += (MAX_LIVE_RANGE - live_range) * 100;
    }

    // Component 4: Live use count
    // Weight: ~10x base unit
    if (!is_disabled("disable-sched-live-uses")) {
        pri += node->live_use_count * 10;
    }

    // Component 5: No-stall priority
    // Weight: ~1x base unit
    if (!is_disabled("disable-sched-stalls")) {
        pri += CanExecuteWithoutStall(node) ? 1 : 0;
    }

    // Component 6: Physical register join
    // Weight: ~1x base unit
    if (!is_disabled("disable-sched-physreg-join")) {
        pri += HasPhysregJoinOpportunity(node) ? 1 : 0;
    }

    return pri;
}

// Priority ordering: Higher value = higher scheduling priority
// Ties broken by node_id for determinism
```

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

```
ctor_282_0_0x4f8f80.c:
  Line 39:  disable-sched-reg-pressure
  Line 55:  disable-sched-live-uses
  Line 70:  disable-sched-stalls
  Line 89:  disable-sched-critical-path
  Line 106: disable-sched-height
  Line 123: disable-sched-vrcycle
  Line 140: disable-sched-physreg-join

ctor_310_0_0x500ad0.c:
  Line 102: print-sched-critical       (debug output)
  Line 195: enable-cyclic-critical-path

ctor_652_0_0x599ef0.c:
  Line 216: disable-sched-critical-path
  Line 229: disable-sched-height
  Line 315: max-sched-reorder (default: 6)

ctor_283_0x4f9b60.c:
  sched-high-latency-cycles (default: 25)

ctor_314_0x502360.c:
  recurrence-chain-limit (default: 3)
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
