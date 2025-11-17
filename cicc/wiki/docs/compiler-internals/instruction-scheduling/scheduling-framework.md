# Instruction Scheduling Framework

## Overview

CICC implements multi-phase list scheduling with DAG construction, weighted edge latencies from InstrItineraryData, and priority-based instruction selection. Pre-RA scheduling (4 algorithms) optimizes for latency/ILP before register allocation; Post-RA scheduling (3 algorithms) performs final ordering with optional anti-dependency breaking. Critical path analysis drives primary scheduling priority using bottom-up dynamic programming.

## Scheduling Phases

### Pre-RA Scheduling (Pre-Register Allocation)

- **Timing**: Before register allocation pass in compilation pipeline
- **Control Flag**: `enable-misched` (default: `true`)
- **Binary Address**: `0x500ad0` (ctor_310_0_0x500ad0.c:line_ref)
- **Purpose**: Optimize instruction order for latency hiding and ILP while considering future register pressure impact
- **Available Schedulers**: 4 variants
  - `list-burr` (0x1d05200): Bottom-up register reduction
  - `source` (0x1d05510): Source-order preserving
  - `list-hybrid` (0x1d05820): Balance latency and register pressure
  - `list-ilp` (0x1d04dc0): Balance ILP and register pressure

**Binary Evidence**:
- Registration: ctor_282_0_0x4f8f80.c:18-30
- Enable flag: ctor_310_0_0x500ad0.c

**Pre-RA Scheduling Pass Pseudocode**:
```c
void preRAScheduling(MachineBasicBlock *MBB) {
    // Phase 1: Optional topological sort
    if (topo_sort_begin) {
        topologicalSort(MBB->instructions);
    }

    // Phase 2: Build scheduling DAG
    ScheduleDAG *DAG = buildSchedulingDAG(MBB);

    // Phase 3: Compute edge weights from InstrItineraryData
    for (ScheduleDAGEdge *edge : DAG->edges) {
        if (edge->type == TRUE_DEPENDENCY) {
            edge->weight = getInstrLatency(edge->source);
        } else if (edge->type == OUTPUT_DEPENDENCY || edge->type == ANTI_DEPENDENCY) {
            edge->weight = 1;  // Serialization
        } else {
            edge->weight = 0;  // Control/memory ordering only
        }
    }

    // Phase 4: Compute critical heights (longest path to leaves)
    computeCriticalHeights(DAG);

    // Phase 5: List scheduling with priority queue
    PriorityQueue<Instruction*> readyList;
    std::vector<Instruction*> scheduled;

    // Initialize ready list with leaf instructions
    for (Instruction *instr : DAG->nodes) {
        if (instr->successors.empty()) {
            instr->critical_height = 0;
            readyList.insert(instr, computePriority(instr));
        }
    }

    unsigned cycle = 0;
    while (!readyList.empty()) {
        Instruction *instr = readyList.pop();

        // Schedule at earliest cycle respecting dependencies
        unsigned earliest = cycle;
        for (ScheduleDAGEdge *pred : instr->predecessors) {
            earliest = std::max(earliest,
                               pred->source->scheduled_cycle + pred->weight);
        }

        instr->scheduled_cycle = earliest;
        scheduled.push_back(instr);

        // Add newly ready predecessors
        for (ScheduleDAGEdge *pred : instr->predecessors) {
            if (allSuccessorsScheduled(pred->source)) {
                readyList.insert(pred->source, computePriority(pred->source));
            }
        }

        cycle = std::max(cycle, earliest + 1);
    }

    // Emit scheduled instructions
    emitScheduledCode(scheduled);
}

unsigned computePriority(Instruction *instr) {
    unsigned priority = 0;

    // Heuristic 1: Critical path (primary)
    if (!disable_sched_critical_path) {
        priority += WEIGHT_CRITICAL * instr->critical_height;
    }

    // Heuristic 2: Scheduled height
    if (!disable_sched_height) {
        unsigned max_height = 0;
        for (ScheduleDAGEdge *succ : instr->successors) {
            max_height = std::max(max_height,
                                 succ->dest->critical_height + succ->weight);
        }
        priority += WEIGHT_HEIGHT * max_height;
    }

    // Heuristic 3: Register pressure
    if (!disable_sched_reg_pressure) {
        int pressure_delta = estimateRegPressureDelta(instr);
        priority += WEIGHT_PRESSURE * (-pressure_delta);  // Negative = reduce pressure
    }

    // Heuristic 4: Live uses
    if (!disable_sched_live_use) {
        unsigned live_uses = countLiveUses(instr);
        priority += WEIGHT_LIVE_USE * live_uses;
    }

    // Heuristic 5: No-stall (avoid resource conflicts)
    if (!disable_sched_stalls) {
        bool can_execute = checkResourceAvailability(instr);
        priority += WEIGHT_NO_STALL * (can_execute ? 1 : 0);
    }

    // Heuristic 6: Physical register join
    if (!disable_sched_physreg_join) {
        unsigned join_benefit = estimatePhysRegJoinBenefit(instr);
        priority += WEIGHT_PHYSREG * join_benefit;
    }

    return priority;
}
```

### Post-RA Scheduling (Post-Register Allocation)

- **Timing**: After register allocation pass in compilation pipeline
- **Control Flag**: `enable-post-misched` (default: `true`)
- **Binary Address**: `0x500ad0` (ctor_310_0_0x500ad0.c)
- **Purpose**: Final instruction ordering for latency hiding with physical register constraints
- **Available Schedulers**: 3 variants
  - `converge` (0x1e76f50): Standard converging scheduler
  - `ilpmax` (0x1e6ecd0): Maximize ILP
  - `ilpmin` (0x1e6ec30): Minimize ILP (power-constrained)
- **Anti-Dependency Breaking**: `break-anti-dependencies` = `"none"` | `"critical"` | `"all"` (default: `"none"`)

**Binary Evidence**:
- Registration: ctor_310_0_0x500ad0.c:334-338
- Anti-dep breaking: ctor_316_0_0x502ea0.c

**Post-RA Scheduling Pass Pseudocode**:
```c
void postRAScheduling(MachineBasicBlock *MBB) {
    // Build DAG with physical register constraints
    ScheduleDAG *DAG = buildSchedulingDAG(MBB);

    // Optionally break anti-dependencies
    if (break_anti_dependencies == "all") {
        for (ScheduleDAGEdge *edge : DAG->edges) {
            if (edge->type == ANTI_DEPENDENCY) {
                DAG->removeEdge(edge);  // Remove WAR constraint
            }
        }
    } else if (break_anti_dependencies == "critical") {
        for (ScheduleDAGEdge *edge : DAG->edges) {
            if (edge->type == ANTI_DEPENDENCY) {
                // Only break if both endpoints on critical path
                if (edge->source->critical_height > critical_threshold &&
                    edge->dest->critical_height > critical_threshold) {
                    DAG->removeEdge(edge);
                }
            }
        }
    }

    // Re-compute critical heights after anti-dep breaking
    computeCriticalHeights(DAG);

    // List scheduling (same algorithm as pre-RA)
    scheduleDAG(DAG, MBB);
}
```

## Machine Model Integration

### InstrItineraryData

CICC uses InstrItineraryData structures to define execution stages and latencies per functional unit. This provides cycle-accurate scheduling models.

**Binary Address**: 0x509ca0 (ctor_336_0x509ca0.c)
**Control Flag**: `scheditins` vs `schedmodel`

**Structure** (inferred):
```c
struct InstrItinerary {
    unsigned FirstStage;        // Index of first execution stage
    unsigned LastStage;         // Index of last execution stage
    unsigned FirstCycle;        // Cycle offset to first result
    unsigned LastCycle;         // Cycle offset to last result
};

struct InstrItineraryData {
    unsigned NumStages;
    const InstrStageData *Stages;
    unsigned NumOperandCycles;
    const unsigned *OperandCycles;
    unsigned NumProcItineraries;
    const InstrItinerary *Itineraries;
};
```

**Latency Computation**:
```c
unsigned getInstrLatency(MachineInstr *MI) {
    if (InstrItineraryData *IID = getItineraryData()) {
        unsigned ItinClass = MI->getDesc().getSchedClass();
        const InstrItinerary *IT = &IID->Itineraries[ItinClass];
        return IT->LastCycle - IT->FirstCycle;
    }

    // Fallback: estimate for long-latency instructions
    if (MI->mayLoad() || MI->mayStore() || MI->isFPOperation()) {
        return sched_high_latency_cycles;  // Default: 25
    }

    return 1;  // Default arithmetic latency
}
```

**Evidence**: ctor_336_0x509ca0.c, ctor_283_0x4f9b60.c

### Functional Unit Reservation

Edge weights account for functional unit reservation times:

```c
unsigned computeEdgeWeight(MachineInstr *Producer, MachineInstr *Consumer) {
    unsigned base_latency = getInstrLatency(Producer);

    // Check for resource conflicts
    if (Producer->FU == Consumer->FU && Producer->FU->isPipelined == false) {
        // Non-pipelined unit: must serialize
        base_latency = std::max(base_latency, Producer->FU->reservation_cycles);
    }

    return base_latency;
}
```

### Cycle-Level Precision

**Control Flag**: `disable-sched-cycles` (default: `false`)
**Effect**: When `false`, cycle-accurate latencies are used; when `true`, simplified ordering without cycle precision

## DAG Construction Algorithm

**Four-Phase Process**:

1. **Optional Topological Sort**: Initial ordering (controlled by `topo-sort-begin`, default: `true`)
2. **Dependency Analysis**: Build edges from instruction operand use/def chains
3. **Edge Weight Computation**: Assign latencies from InstrItineraryData
4. **Critical Path Analysis**: Bottom-up DP to compute longest path to leaves

**Complete DAG Construction Pseudocode**:
```c
ScheduleDAG* buildSchedulingDAG(MachineBasicBlock *MBB) {
    ScheduleDAG *DAG = new ScheduleDAG();

    // Create nodes for all instructions
    for (MachineInstr *MI : MBB->instructions()) {
        DAGNode *node = new DAGNode(MI);
        DAG->addNode(node);
    }

    // Build dependency edges
    std::map<unsigned, MachineInstr*> last_def;  // Register -> last defining instr
    std::map<unsigned, std::vector<MachineInstr*>> last_uses;  // Reg -> using instrs

    for (MachineInstr *MI : MBB->instructions()) {
        // True dependencies (RAW): uses depend on last def
        for (unsigned reg : MI->uses()) {
            if (last_def.count(reg)) {
                DAG->addEdge(last_def[reg], MI, TRUE_DEPENDENCY,
                            getInstrLatency(last_def[reg]));
            }
        }

        // Output dependencies (WAW): defs depend on last def
        for (unsigned reg : MI->defs()) {
            if (last_def.count(reg)) {
                DAG->addEdge(last_def[reg], MI, OUTPUT_DEPENDENCY, 1);
            }
        }

        // Anti-dependencies (WAR): defs depend on last uses
        for (unsigned reg : MI->defs()) {
            if (last_uses.count(reg)) {
                for (MachineInstr *use : last_uses[reg]) {
                    ScheduleDAGEdge *edge = DAG->addEdge(use, MI, ANTI_DEPENDENCY, 1);
                    edge->breakable = true;
                }
                last_uses[reg].clear();
            }
        }

        // Update tracking
        for (unsigned reg : MI->defs()) {
            last_def[reg] = MI;
        }
        for (unsigned reg : MI->uses()) {
            last_uses[reg].push_back(MI);
        }
    }

    // Memory dependencies (conservative analysis)
    analyzeMemoryDependencies(DAG, MBB);

    // Control dependencies
    analyzeControlDependencies(DAG, MBB);

    return DAG;
}

void analyzeMemoryDependencies(ScheduleDAG *DAG, MachineBasicBlock *MBB) {
    unsigned window_size = 100;  // max-mem-dep-window-instrs
    std::map<std::pair<MachineInstr*, MachineInstr*>, bool> cache;

    std::vector<MachineInstr*> loads;
    std::vector<MachineInstr*> stores;

    for (MachineInstr *MI : MBB->instructions()) {
        if (MI->mayLoad()) loads.push_back(MI);
        if (MI->mayStore()) stores.push_back(MI);
    }

    // Conservative: assume all loads/stores may alias
    for (MachineInstr *load : loads) {
        for (MachineInstr *store : stores) {
            if (store->comesBefore(load)) {
                // Check cache
                auto key = std::make_pair(store, load);
                bool may_alias;

                if (cache_memory_deps && cache.count(key)) {
                    may_alias = cache[key];
                } else {
                    may_alias = mayAlias(store, load);
                    if (cache_memory_deps) {
                        cache[key] = may_alias;
                    }
                }

                if (may_alias) {
                    DAG->addEdge(store, load, MEMORY_DEPENDENCY, 0);
                }
            }
        }
    }
}
```

**Evidence**: dag_construction.json, TECHNICAL_IMPLEMENTATION.txt

## Critical Path Analysis

### Algorithm

Bottom-up dynamic programming computing longest latency path from each instruction to any leaf node.

**Formula**: `critical_height[node] = max(critical_height[successor] + edge_latency(node, successor))` for all successors

**Complexity**: O(V + E) where V = instructions, E = dependency edges

**Pseudocode**:
```c
void computeCriticalHeights(ScheduleDAG *DAG) {
    std::vector<bool> visited(DAG->nodes.size(), false);

    // Reverse topological order traversal
    for (DAGNode *node : DAG->reverseTopologicalOrder()) {
        if (node->successors.empty()) {
            node->critical_height = 0;  // Leaf node
        } else {
            unsigned max_height = 0;
            for (ScheduleDAGEdge *edge : node->successors) {
                unsigned height_through_succ =
                    edge->dest->critical_height + edge->weight;
                max_height = std::max(max_height, height_through_succ);
            }
            node->critical_height = max_height;
        }
        visited[node->id] = true;
    }
}
```

**Binary Evidence**: critical_path_detection.json, ctor_310_0_0x500ad0.c:102

### Cyclic Critical Path

**Control Flag**: `enable-cyclic-critical-path` (default: `false`)
**Binary Address**: 0x500ad0
**Purpose**: Handle loop-carried dependencies (recurrence cycles)
**Recurrence Limit**: `recurrence-chain-limit = 3` (binary: 0x502360, ctor_314_0x502360.c)

**Algorithm**:
```c
void analyzeCyclicCriticalPath(ScheduleDAG *DAG) {
    unsigned recurrence_limit = 3;

    for (std::vector<DAGNode*> cycle : DAG->findCycles()) {
        if (cycle.size() > recurrence_limit) continue;

        unsigned cycle_latency = 0;
        for (size_t i = 0; i < cycle.size(); ++i) {
            DAGNode *src = cycle[i];
            DAGNode *dst = cycle[(i + 1) % cycle.size()];
            ScheduleDAGEdge *edge = DAG->findEdge(src, dst);
            cycle_latency += edge->weight;
        }

        // Check if commuting operands can break cycle
        if (cycle_latency > threshold && canCommute(cycle)) {
            unsigned benefit = evaluateCommutationBenefit(cycle);
            if (benefit > 0) {
                commuteOperands(cycle);
            }
        }
    }
}
```

**Evidence**: ctor_314_0x502360.c, dag_construction.json:270-276

## Edge Weight Formulas

### True Dependency (Read-After-Write)

**Formula**: `weight = getInstrLatency(producer)`

**Example**:
```c
// ADD R1, R2, R3  (latency: 4 cycles)
// MUL R4, R1, R5  (consumer)
// Edge weight: 4
```

**Binary Evidence**: dag_construction.json:44-53

### Output Dependency (Write-After-Write)

**Formula**: `weight = 1` (constant serialization)

**Example**:
```c
// MOV R1, #10
// MOV R1, #20
// Edge weight: 1 (must serialize writes to R1)
```

**Binary Evidence**: dag_construction.json:55-60

### Anti-Dependency (Write-After-Read)

**Formula**: `weight = 1` (breakable serialization)

**Breakable**: Yes (controlled by `break-anti-dependencies`)

**Example**:
```c
// ADD R2, R1, R3  (reads R1)
// MOV R1, #100    (writes R1)
// Edge weight: 1 (can be broken with mode="all" or "critical")
```

**Binary Evidence**: dag_construction.json:62-71, ctor_316_0x502ea0.c

### Control Dependency

**Formula**: `weight = 0` (ordering only, no latency)

**Purpose**: Prevent speculative execution past control flow

**Binary Evidence**: dag_construction.json:72-77

### Memory Dependency

**Formula**: `weight = 0` (conservative ordering)

**Window Sizes**:
- Instructions per block: 100 (default)
- Blocks per function: 200 (default)
- Caching: enabled (default: `true`)

**Binary Address**: 0x49e180 (ctor_081_0x49e180.c)

**Binary Evidence**: dag_construction.json:78-86, README.txt:138-141

### Latency Fallback

When InstrItineraryData unavailable:

**Parameter**: `sched-high-latency-cycles`
**Default**: 25
**Binary Address**: 0x4f9b60 (ctor_283_0x4f9b60.c)

**Formula**:
```c
if (!hasItineraryData(instr)) {
    if (instr->isLongLatency()) {
        return 25;  // sched-high-latency-cycles
    } else {
        return 1;   // default arithmetic
    }
}
```

**Evidence**: ctor_283_0x4f9b60.c, dag_construction.json:96-102

## Scheduling Algorithms

### Algorithm 1: list-burr (Bottom-Up Register Reduction)

**Binary Address**: 0x1d05200
**Registration**: ctor_282_0_0x4f8f80.c:18
**Priority Function**: `live_range_end - live_range_start`
**Goal**: Minimize register pressure

**Pseudocode**:
```c
unsigned computeBURRPriority(Instruction *instr) {
    unsigned def_cycle = instr->def_position;
    unsigned last_use_cycle = 0;

    for (Use *use : instr->uses) {
        last_use_cycle = std::max(last_use_cycle, use->position);
    }

    unsigned live_range = last_use_cycle - def_cycle;
    return MAX_PRIORITY - live_range;  // Shorter live range = higher priority
}
```

**Evidence**: scheduling_heuristics.json:33-58

### Algorithm 2: source (Source-Order Preserving)

**Binary Address**: 0x1d05510
**Registration**: ctor_282_0_0x4f8f80.c:20
**Priority Function**: `source_position + minimal_register_pressure_adjustment`
**Goal**: Preserve source order when dependencies allow

**Pseudocode**:
```c
unsigned computeSourcePriority(Instruction *instr) {
    unsigned source_pos = instr->source_order_index;
    int pressure_adj = estimateRegPressureDelta(instr);

    // Prefer source order, but adjust slightly for pressure
    return (source_pos * 100) + (pressure_adj > 0 ? -10 : 10);
}
```

**Evidence**: scheduling_heuristics.json:59-84

### Algorithm 3: list-hybrid (Balanced Latency/Pressure)

**Binary Address**: 0x1d05820
**Registration**: ctor_282_0_0x4f8f80.c:22
**Priority Function**: `0.5 * latency_weight + 0.5 * register_pressure_weight`
**Goal**: Balance latency hiding and register pressure reduction

**Pseudocode**:
```c
unsigned computeHybridPriority(Instruction *instr) {
    unsigned latency_priority = instr->critical_height;
    unsigned pressure_priority = MAX_PRIORITY - estimateLiveRange(instr);

    // Equal weighting: 50% latency, 50% pressure
    return (latency_priority / 2) + (pressure_priority / 2);
}
```

**Evidence**: scheduling_heuristics.json:85-111

### Algorithm 4: list-ilp (ILP and Pressure Balance)

**Binary Address**: 0x1d04dc0
**Registration**: ctor_282_0_0x4f8f80.c:30
**Priority Function**: Multi-metric with 6 heuristics
**Goal**: Maximize ILP while controlling register pressure

**Six Priority Heuristics**:

1. **Critical Path Priority** (`disable-sched-critical-path`, default: enabled)
2. **Scheduled Height Priority** (`disable-sched-height`, default: enabled)
3. **Register Pressure Priority** (`disable-sched-reg-pressure`, default: enabled)
4. **Live Use Priority** (`disable-sched-live-use`, default: enabled)
5. **No-Stall Priority** (`disable-sched-stalls`, default: enabled)
6. **Physical Register Join Priority** (`disable-sched-physreg-join`, default: enabled)

**Pseudocode** (see Pre-RA section above for full implementation)

**Evidence**: scheduling_heuristics.json:112-149, ctor_282_0_0x4f8f80.c:30-156

## Configuration Parameters

| Parameter | Type | Default | Binary Address | Effect |
|-----------|------|---------|----------------|--------|
| `enable-misched` | bool | `true` | 0x500ad0 | Enable pre-RA machine instruction scheduling |
| `enable-post-misched` | bool | `true` | 0x500ad0 | Enable post-RA machine instruction scheduling |
| `topo-sort-begin` | bool | `true` | (inferred) | Do topological sort at beginning of scheduling pass |
| `disable-sched-cycles` | bool | `false` | (inferred) | Disable cycle-level precision in scheduling |
| `sched-high-latency-cycles` | int | `25` | 0x4f9b60 | Latency estimate for long-latency instructions without itinerary |
| `disable-sched-critical-path` | bool | `false` | 0x4f8f80:39 | Disable critical path priority in list-ilp |
| `disable-sched-height` | bool | `false` | 0x4f8f80:53 | Disable scheduled-height priority in list-ilp |
| `disable-sched-reg-pressure` | bool | `false` | 0x4f8f80:67 | Disable register pressure priority in list-ilp |
| `disable-sched-live-use` | bool | `false` | 0x4f8f80:81 | Disable live use priority in list-ilp |
| `disable-sched-stalls` | bool | `false` | 0x4f8f80:95 | Disable no-stall priority in list-ilp |
| `disable-sched-physreg-join` | bool | `false` | 0x4f8f80:123 | Disable physical register join priority |
| `disable-sched-vrcycle` | bool | `false` | 0x4f8f80:109 | Disable virtual register cycle interference detection |
| `print-sched-critical` | bool | `false` | 0x500ad0:102 | Print critical path length to stdout |
| `enable-cyclic-critical-path` | bool | `false` | 0x500ad0:195 | Enable cyclic critical path analysis |
| `max-sched-reorder` | int | `6` | 0x599ef0:315 | Number of instructions allowed ahead of critical path in list-ilp |
| `break-anti-dependencies` | enum | `"none"` | 0x502ea0 | Anti-dependency breaking mode: "none", "critical", "all" |
| `recurrence-chain-limit` | int | `3` | 0x502360 | Maximum recurrence chain length for operand commutation analysis |
| `max-mem-dep-window-instrs` | int | `100` | 0x49e180 | Memory dependency analysis window (instructions per block) |
| `max-mem-dep-window-blocks` | int | `200` | 0x49e180 | Memory dependency analysis window (blocks per function) |
| `cache-memory-deps` | bool | `true` | 0x49e180 | Cache memory dependency analysis results |
| `scheditins` | bool | (varies) | 0x509ca0 | Use InstrItineraryData for latency lookup |
| `schedmodel` | bool | (varies) | 0x509ca0 | Use machine model (preferred over scheditins) |
| `agg-antidep-debugdiv` | int | `0` | 0x50b430 | Debug divisor for aggressive anti-dep breaker |
| `agg-antidep-debugmod` | int | `0` | 0x50b430 | Debug modulo for aggressive anti-dep breaker |

**Evidence**: README.txt:174-210, dag_construction.json, critical_path_detection.json:279-346

## Anti-Dependency Breaking

### Modes

**Parameter**: `break-anti-dependencies`
**Binary Address**: 0x502ea0 (ctor_316_0x502ea0.c)
**Default**: `"none"`

| Mode | Behavior | Use Case |
|------|----------|----------|
| `"none"` | No anti-dependency breaking | Conservative, preserves all constraints |
| `"critical"` | Break anti-deps on critical path only | Balanced: improve critical path without excessive register pressure |
| `"all"` | Break all anti-dependencies | Aggressive: maximum scheduling freedom, may increase spills |

**Pseudocode**:
```c
void breakAntiDependencies(ScheduleDAG *DAG, const char *mode) {
    if (strcmp(mode, "none") == 0) return;

    for (ScheduleDAGEdge *edge : DAG->edges) {
        if (edge->type != ANTI_DEPENDENCY) continue;

        bool should_break = false;

        if (strcmp(mode, "all") == 0) {
            should_break = true;
        } else if (strcmp(mode, "critical") == 0) {
            // Only break if both endpoints on critical path
            unsigned critical_threshold = DAG->critical_path_length * 0.9;
            if (edge->source->critical_height >= critical_threshold &&
                edge->dest->critical_height >= critical_threshold) {
                should_break = true;
            }
        }

        if (should_break) {
            DAG->removeEdge(edge);
        }
    }
}
```

**Evidence**: dag_construction.json:237-268, ctor_316_0x502ea0.c

### Aggressive Anti-Dependency Breaker

**Binary Address**: 0x50b430 (ctor_345_0x50b430.c)
**Debug Controls**:
- `agg-antidep-debugdiv`: Debug divisor
- `agg-antidep-debugmod`: Debug modulo

Used for testing aggressive breaking strategies beyond standard modes.

**Evidence**: dag_construction.json:254-268, README.txt:134-135

## Binary Registration Addresses

| Component | Address | File | Description |
|-----------|---------|------|-------------|
| Pre-RA schedulers | 0x4f8f80 | ctor_282_0_0x4f8f80.c | list-burr, source, list-hybrid, list-ilp registration |
| Latency config | 0x4f9b60 | ctor_283_0x4f9b60.c | sched-high-latency-cycles = 25 |
| Scheduling control | 0x500ad0 | ctor_310_0_0x500ad0.c | enable-misched, enable-post-misched, critical path flags |
| Recurrence limit | 0x502360 | ctor_314_0x502360.c | recurrence-chain-limit = 3 |
| Anti-dep breaking | 0x502ea0 | ctor_316_0x502ea0.c | break-anti-dependencies configuration |
| Machine model | 0x509ca0 | ctor_336_0x509ca0.c | scheditins vs schedmodel selection |
| Aggressive anti-dep | 0x50b430 | ctor_345_0x50b430.c | agg-antidep-debugdiv, agg-antidep-debugmod |
| Post-RA duplicate | 0x599ef0 | ctor_652_0_0x599ef0.c | Post-RA scheduler registration (duplicate) |
| Post-RA duplicate | 0x5745b0 | ctor_572_0_0x5745b0.c | Post-RA scheduler registration (duplicate) |
| Memory deps | 0x49e180 | ctor_081_0x49e180.c | Memory dependency window parameters |
| list-burr impl | 0x1d05200 | sub_1D05200_0x1d05200.c | BURR scheduler implementation |
| source impl | 0x1d05510 | sub_1D05510_0x1d05510.c | Source scheduler implementation |
| list-hybrid impl | 0x1d05820 | sub_1D05820_0x1d05820.c | Hybrid scheduler implementation |
| list-ilp impl | 0x1d04dc0 | sub_1D04DC0_0x1d04dc0.c | ILP scheduler implementation |
| converge impl | 0x1e76f50 | sub_1E76F50_0x1e76f50.c | Converging scheduler (thunk to 0x1e76650) |
| ilpmax impl | 0x1e6ecd0 | sub_1E6ECD0_0x1e6ecd0.c | Maximum ILP scheduler |
| ilpmin impl | 0x1e6ec30 | sub_1E6EC30_0x1e6ec30.c | Minimum ILP scheduler |

**Evidence**: README.txt:100-141, EXTRACTION_REPORT.txt:144-156

## Pipeline Integration

```
┌─────────────────────┐
│ Instruction         │
│ Selection           │
└──────────┬──────────┘
           │
           v
┌─────────────────────┐
│ Pre-RA Scheduling   │◄─── enable-misched (default: true)
│ (list-ilp, etc.)    │    4 algorithm variants
└──────────┬──────────┘
           │
           v
┌─────────────────────┐
│ Register            │
│ Allocation          │
└──────────┬──────────┘
           │
           v
┌─────────────────────┐
│ Post-RA Scheduling  │◄─── enable-post-misched (default: true)
│ (converge, etc.)    │    3 algorithm variants
│                     │    break-anti-dependencies control
└──────────┬──────────┘
           │
           v
┌─────────────────────┐
│ Code Emission       │
└─────────────────────┘
```

**Evidence**: DAG_CONSTRUCTION_ANALYSIS.txt:310-325

## References

**Source Files** (all in `/home/grigory/nvopen-tools/cicc/decompiled/`):
- ctor_282_0_0x4f8f80.c: Pre-RA scheduler registration
- ctor_283_0x4f9b60.c: Latency parameter configuration
- ctor_310_0_0x500ad0.c: Scheduling pass control flags
- ctor_314_0x502360.c: Recurrence chain limit
- ctor_316_0x502ea0.c: Anti-dependency breaking
- ctor_336_0x509ca0.c: Machine model selection
- ctor_345_0x50b430.c: Aggressive anti-dep breaker
- ctor_081_0x49e180.c: Memory dependency windows
- sub_1D04DC0_0x1d04dc0.c: list-ilp implementation
- sub_1D05200_0x1d05200.c: list-burr implementation
- sub_1D05510_0x1d05510.c: source implementation
- sub_1D05820_0x1d05820.c: list-hybrid implementation
- sub_1E76F50_0x1e76f50.c: converge implementation
- sub_1E6ECD0_0x1e6ecd0.c: ilpmax implementation
- sub_1E6EC30_0x1e6ec30.c: ilpmin implementation

**Analysis Files** (all in `/home/user/nvopen-tools/cicc/deep_analysis/L3/instruction_scheduling/`):
- README.txt: Overview and quick reference
- TECHNICAL_IMPLEMENTATION.txt: Detailed pseudocode and algorithms
- DAG_CONSTRUCTION_ANALYSIS.txt: Narrative analysis
- EXTRACTION_REPORT.txt: L3-05 scheduler variant extraction
- dag_construction.json: Structured DAG construction data
- scheduling_heuristics.json: Priority function details
- critical_path_detection.json: Critical path algorithm analysis
- COMPLETION_STATUS.json: Analysis completion metadata
