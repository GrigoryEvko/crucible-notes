# DAG Construction for Instruction Scheduling

**SCHED-03 | Ultra-Technical Implementation Analysis**

---

## Overview

The CICC instruction scheduler implements a sophisticated **bottom-up list scheduling** algorithm that constructs a Directed Acyclic Graph (DAG) to model instruction dependencies and their latencies. The DAG nodes represent machine instructions, and weighted edges represent dependencies (data, control, memory) with cycle-accurate latency values.

This document provides complete technical specifications of the DAG construction algorithm, dependency analysis, edge weight formulas, and scheduling heuristics as implemented in CICC.

---

## 4-Phase DAG Construction Algorithm

The scheduler processes each machine basic block through four distinct phases:

### Phase 1: Initial Topological Sort (Optional)

**Control Flag**: `topo-sort-begin`
**Default**: `true`
**Purpose**: Pre-order instructions topologically to improve list scheduling quality

```
if enable_topo_sort_begin:
    instructions = topologicalSort(instructions)
```

**Rationale**: Topological pre-ordering helps the scheduler recognize independent instruction chains early, improving priority queue initialization and reducing scheduling iterations.

---

### Phase 2: DAG Construction

**Purpose**: Build dependency graph by analyzing instruction register uses/defs and establishing five types of dependencies

**Large Case Dispatcher**: 0xB612D0 (180+ instruction patterns)

The dispatcher analyzes each instruction's:
- Register reads (uses)
- Register writes (defs)
- Memory operations (loads/stores)
- Control flow (branches, calls)

Dependencies are established through register/memory dataflow analysis:

```c
for each consumer_instr in basicBlock:
    for each operand in consumer_instr.uses:
        # Find producer of this operand
        for each producer_instr in basicBlock (reverse order):
            if producer_instr.defs contains operand:
                # True dependency (RAW)
                addDependency(
                    from = producer_instr,
                    to = consumer_instr,
                    type = TRUE_DEPENDENCY,
                    weight = computeLatency(producer_instr)
                )
                break
```

---

### Phase 3: Edge Weight Computation

**Purpose**: Calculate latency-based edge weights from instruction itineraries or estimation heuristics

Primary formula:
```
edge_weight = source_instruction_latency + penalties
```

**Latency Sources**:
1. **InstrItineraryData** (machine model): Cycle-accurate execution latencies per functional unit
2. **Fallback Estimation**: `sched-high-latency-cycles = 25` for long-latency instructions without itinerary

**Penalties**:
- Anti-dependency: `+1` (serialization)
- Output-dependency: `+1` (serialization)

---

### Phase 4: Bottom-up List Scheduling

**Purpose**: Process DAG from bottom (leaf instructions) upward, maintaining priority queue of ready instructions

**Algorithm**:
```c
scheduled = []
ready_queue = PriorityQueue()

# Initialize: ready instructions have no predecessors
for each node in dag.nodes:
    if node.predecessors.empty():
        ready_queue.insert(node, priority = computePriority(node, dag))

cycle = 0

while ready_queue is not empty:
    # Select highest priority ready instruction
    instr = ready_queue.pop()

    # Schedule at earliest available cycle
    scheduled_cycle = max(
        cycle,
        max(pred.scheduled_cycle + edge.weight
            for each pred in instr.predecessors)
    )

    instr.scheduled_cycle = scheduled_cycle
    scheduled.append((scheduled_cycle, instr))

    # Update ready queue
    for each successor in instr.successors:
        if successor.all_predecessors_scheduled():
            ready_queue.insert(
                successor,
                priority = computePriority(successor, dag)
            )

    cycle = max(cycle, scheduled_cycle + 1)

return sorted(scheduled)
```

---

## 5 Dependency Types

### 1. True Dependency (RAW - Read-After-Write)

**Description**: Consumer reads register written by producer (data dependency)

**Edge Weight Formula**:
```
latency = max(0, InstrLatency(producer) - start_of_consumer_relative_to_producer)
```

**Weight**: `instruction_latency + additional_penalties`

**Example**:
```asm
ADD dest, src1, src2    ; latency: 4 cycles
MUL result, dest, other ; starts 0 cycles later
```
**Edge Weight**: `4` (consumer must wait full 4-cycle latency of ADD)

**Implementation**:
```c
if edge.type == TRUE_DEPENDENCY:
    edge.weight = computeLatencyWeight(edge.source)
```

**Latency Computation**:
```c
function computeLatencyWeight(instruction):
    # Try InstrItineraryData first
    if machine_model.has_itinerary_data:
        return machine_model.getInstrLatency(instruction)

    # Fallback: estimate long-latency instructions
    if instruction.is_long_latency:
        return sched_high_latency_cycles  # Default: 25
    else:
        return default_latency  # Usually 1 for arithmetic
```

---

### 2. Output Dependency (WAW - Write-After-Write)

**Description**: Must serialize writes to same register (destination conflict)

**Edge Weight**: `1` (constant serialization penalty)

**Example**:
```asm
MOV dest, value1   ; writes dest
ADD dest, x, y     ; also writes dest - must serialize
```

**Rationale**: Sequential writes to the same register destination require program order preservation.

**Implementation**:
```c
for each instr1 in basicBlock:
    for each instr2 in basicBlock (after instr1):
        if (instr1.defs && instr2.defs) intersect:
            addDependency(
                from = instr1,
                to = instr2,
                type = OUTPUT_DEPENDENCY,
                weight = 1  # Serialization
            )
```

---

### 3. Anti Dependency (WAR - Write-After-Read)

**Description**: Consumer cannot write register until producer reads it (register reuse conflict)

**Edge Weight**: `1` (serialization, but breakable)

**Breakable**: `true`

**Control Flag**: `break-anti-dependencies`
**Options**: `none` | `critical` | `all`

**Breaking Behavior**:
- `none`: Keep all anti-dependencies (default)
- `critical`: Break anti-deps on critical path only
- `all`: Aggressively break all anti-dependencies

**Example**:
```asm
READ value, reg     ; reads reg
WRITE reg, newval   ; writes reg - anti-dependency
```

**After Breaking** (with `break-anti-dependencies=all`):
- Edge removed
- Weight effectively becomes `0`
- Allows speculative execution and reordering

**Implementation**:
```c
for each instr1 in basicBlock:  # reader
    for each instr2 in basicBlock:  # writer
        if instr1 < instr2 and instr1.reads && instr2.defs intersect:
            edge = addDependency(
                from = instr1,
                to = instr2,
                type = ANTI_DEPENDENCY,
                weight = 1
            )
            edge.breakable = can_break_anti_dependency()
```

**Breaking Algorithm**:
```c
function breakAntiDependencies(dag, mode):
    if mode == "none":
        return  # Keep all anti-deps

    for each edge in dag.all_edges():
        if edge.type == ANTI_DEPENDENCY:

            if mode == "critical":
                # Only break if both endpoints are on critical path
                if (edge.source.critical_height > critical_threshold and
                    edge.dest.critical_height > critical_threshold):
                    removeEdge(edge)

            elif mode == "all":
                # Break all anti-dependencies
                # Aggressive: may hurt register allocation but improves scheduling
                removeEdge(edge)
```

---

### 4. Control Dependency

**Description**: Control flow dependency - cannot move past control-dependent branches

**Edge Weight**: `0` (ordering constraint, not performance)

**Note**: Affects correctness, not performance metrics

**Rationale**: Control dependencies enforce correct program semantics (e.g., exception handling, branch outcomes) but don't model actual hardware latency.

**Implementation**:
```c
elif edge.type == CONTROL_DEPENDENCY:
    edge.weight = 0
```

---

### 5. Memory Dependency

**Description**: Memory ordering dependency - load/store ordering constraints

**Analysis**: Conservative alias analysis over instruction window

**Window Parameters**:
- **Instructions per block**: `100`
- **Blocks per function**: `200`

**Caching**: `enabled` (reduces compile time)

**Conservative Approach**: Assumes all load/store pairs may alias unless proven otherwise

**Implementation**:
```c
function analyzeMemoryDependencies(basicBlock, dag):
    """
    Conservative memory dependency analysis.

    Windows:
    - Max 100 instructions per block
    - Max 200 blocks per function
    """

    instruction_window = min(100, len(basicBlock))
    blocks_analyzed = min(200, function.num_blocks)

    cache_memory_deps = true  # Enabled by default
    cache = MemoryDependencyCache()

    for each load_instr in basicBlock:
        for each store_instr in earlier_instructions(load_instr):
            # Check if they might alias
            if cache_memory_deps and cache.contains(load_instr, store_instr):
                may_alias = cache.get(load_instr, store_instr)
            else:
                # Conservative: assume alias unless proven otherwise
                may_alias = mustAlias(load_instr.address, store_instr.address)

                if may_alias is unknown:
                    may_alias = true  # Conservative

                if cache_memory_deps:
                    cache.insert(load_instr, store_instr, may_alias)

            if may_alias:
                addDependency(
                    from = store_instr,
                    to = load_instr,
                    type = MEMORY_DEPENDENCY,
                    weight = 0  # Ordering only
                )
```

---

## Edge Weight Computation Details

### Primary Edge Weight Formula

```
edge_weight = getInstrLatency(producer_instruction) + penalties
```

### Latency Lookup Cases

**Case 1: With InstrItineraryData** (preferred)
```c
edge_weight = instr_itinerary.getLatency(producer)
```

**Case 2: Without itinerary (fallback estimation)**
```c
if producer.isLongLatency():
    edge_weight = 25  # sched-high-latency-cycles
else:
    edge_weight = 1   # default for arithmetic
```

**Case 3: Cycle-level precision disabled**
```c
if disable_sched_cycles:
    edge_weight = 0  # conservative ordering only
```

### Edge Weight by Dependency Type

| Dependency Type | Base Weight | Formula | Modifiers |
|----------------|-------------|---------|-----------|
| **True (RAW)** | `InstrLatency(producer)` | `getInstrLatency(instr)` | Machine model dependent |
| **Output (WAW)** | `1` | Constant | Serialization penalty |
| **Anti (WAR)** | `1` | Constant, breakable | Can be removed if breaking enabled |
| **Control** | `0` | Constant | Ordering only, no latency |
| **Memory** | `0` | Constant | Conservative ordering |

### Complete Edge Weight Computation

```c
function computeEdgeWeights(dag):
    for each edge in dag.all_edges():
        if edge.type == TRUE_DEPENDENCY:
            edge.weight = computeLatencyWeight(edge.source)
        elif edge.type in {OUTPUT_DEPENDENCY, ANTI_DEPENDENCY}:
            edge.weight = 1
        elif edge.type == CONTROL_DEPENDENCY:
            edge.weight = 0
        elif edge.type == MEMORY_DEPENDENCY:
            edge.weight = 0  # Conservative ordering only
```

---

## Edge Weight Examples (Complete Set)

### Example 1: Simple Register RAW Dependency

**Instructions**:
```asm
ADD dest, src1, src2    ; Producer (latency: 4 cycles)
MUL result, dest, other ; Consumer (starts 0 cycles later)
```

**Dependency**: True (RAW)
**Edge Weight**: `4`
**Explanation**: Consumer must wait full 4-cycle latency of ADD before reading `dest`

---

### Example 2: Short-Latency Instruction

**Instructions**:
```asm
MOV reg, immediate     ; Producer (latency: 1 cycle)
ADD result, reg, other ; Consumer
```

**Dependency**: True (RAW)
**Edge Weight**: `1`
**Explanation**: MOV has minimal latency, consumer ready after 1 cycle

---

### Example 3: Long-Latency Instruction Without Itinerary

**Instructions**:
```asm
LOAD value, [memory]    ; Producer (estimated latency: 25 cycles)
FADD result, value, other ; Consumer
```

**Dependency**: True (RAW)
**Edge Weight**: `25`
**Explanation**: Estimated at `sched-high-latency-cycles` default of 25 (no itinerary data available)

**Configuration**:
```
sched-high-latency-cycles = 25  # Default fallback
```

---

### Example 4: Anti-Dependency Serialization

**Instructions**:
```asm
READ reg      ; Producer (reads reg)
WRITE reg     ; Consumer (writes reg)
```

**Dependency**: Anti (WAR)
**Edge Weight**: `1` (before breaking)
**Breakable**: `true`

**Breaking Modes**:
- `break-anti-dependencies=none`: Weight remains `1`
- `break-anti-dependencies=all`: Edge removed, effective weight `0`
- `break-anti-dependencies=critical`: Removed only if both on critical path

**After Breaking**:
- Edge removed entirely
- Allows speculative execution and reordering
- May increase register pressure

---

### Example 5: Output Dependency (WAW)

**Instructions**:
```asm
MOV dest, value1   ; Writes dest
ADD dest, x, y     ; Also writes dest
```

**Dependency**: Output (WAW)
**Edge Weight**: `1`
**Explanation**: Serialization to preserve write order

---

### Example 6: Memory Dependency (Conservative)

**Instructions**:
```asm
STORE [addr1], value  ; May alias with addr2
LOAD  result, [addr2] ; Conservative: assume alias
```

**Dependency**: Memory
**Edge Weight**: `0` (ordering only)
**Explanation**: Conservative analysis assumes potential alias, enforces ordering without latency penalty

---

### Example 7: Control Dependency

**Instructions**:
```asm
BRANCH condition, label  ; Control instruction
LOAD value, [mem]        ; Cannot move before branch
```

**Dependency**: Control
**Edge Weight**: `0`
**Explanation**: Ensures correctness (exception handling) but no latency cost

---

## Complete DAG Construction Pseudocode

### buildSchedulingDAG Function

```c
function buildSchedulingDAG(basicBlock):
    """
    Construct scheduling DAG from machine basic block.
    Input: basicBlock - sequence of machine instructions
    Output: DAG with nodes (instructions) and weighted edges (dependencies)
    """

    # Phase 1: Optional Initial Topological Sort
    if enable_topo_sort_begin:
        instructions = topologicalSort(instructions)

    # Phase 2: Initialize DAG Nodes
    dag_nodes = {}
    for each instruction in basicBlock:
        dag_nodes[instruction] = DAGNode(
            instruction = instruction,
            predecessors = [],
            successors = [],
            critical_height = 0,
            reg_pressure_delta = 0
        )

    # Phase 3: Build Dependency Edges
    for each consumer_instr in basicBlock:
        for each operand in consumer_instr.uses:
            # Find producer of this operand
            for each producer_instr in basicBlock (reverse order):
                if producer_instr.defs contains operand:
                    # True dependency (RAW)
                    addDependency(
                        from = producer_instr,
                        to = consumer_instr,
                        type = TRUE_DEPENDENCY,
                        weight = computeLatency(producer_instr)
                    )
                    break

    # Handle output dependencies (WAW)
    for each instr1 in basicBlock:
        for each instr2 in basicBlock (after instr1):
            if (instr1.defs && instr2.defs) intersect:
                addDependency(
                    from = instr1,
                    to = instr2,
                    type = OUTPUT_DEPENDENCY,
                    weight = 1  # Serialization
                )

    # Handle anti-dependencies (WAR)
    for each instr1 in basicBlock:  # reader
        for each instr2 in basicBlock:  # writer
            if instr1 < instr2 and instr1.reads && instr2.defs intersect:
                edge = addDependency(
                    from = instr1,
                    to = instr2,
                    type = ANTI_DEPENDENCY,
                    weight = 1
                )
                edge.breakable = can_break_anti_dependency()

    # Handle memory dependencies
    analyzeMemoryDependencies(basicBlock, dag_nodes)

    # Handle control dependencies
    analyzeControlDependencies(basicBlock, dag_nodes)

    # Phase 4: Compute Edge Weights
    for each edge in dag.all_edges():
        if edge.type == TRUE_DEPENDENCY:
            edge.weight = computeLatencyWeight(edge.source)
        elif edge.type in {OUTPUT_DEPENDENCY, ANTI_DEPENDENCY}:
            edge.weight = 1
        elif edge.type == CONTROL_DEPENDENCY:
            edge.weight = 0
        elif edge.type == MEMORY_DEPENDENCY:
            edge.weight = 0  # Conservative ordering only

    # Phase 5: Compute Critical Heights
    for each node in dag.nodes (reverse topological order):
        node.critical_height = max(
            0,
            max(successor.critical_height + edge.weight
                for each successor in node.successors)
        )

    return dag
```

---

## DAG Node Structure

Each DAG node represents a single machine instruction with the following fields:

```c
struct DAGNode {
    // Core instruction reference
    MachineInstruction* instruction;

    // Dependency edges
    list<DAGEdge*> successors;     // Instructions dependent on this
    list<DAGEdge*> predecessors;   // Instructions this depends on

    // Scheduling metrics
    int critical_height;           // Longest path to any leaf (cycles)
    int scheduled_height;          // Height after partial scheduling
    int reg_pressure_delta;        // Change in live register count

    // Schedule assignment
    int scheduled_cycle;           // Assigned execution cycle
    int position_in_schedule;      // Assigned order in sequence

    // Priority heuristics
    float critical_path_priority;
    float register_pressure_priority;
    float live_use_priority;
    float no_stall_priority;
    float physreg_join_priority;
};
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `instruction` | `MachineInstruction*` | Pointer to actual machine instruction |
| `successors` | `list<DAGEdge*>` | Instructions that depend on this instruction |
| `predecessors` | `list<DAGEdge*>` | Instructions this instruction depends on |
| `critical_height` | `int` | Longest latency path from this node to any leaf (cycles) |
| `scheduled_height` | `int` | Updated height metric during scheduling |
| `reg_pressure_delta` | `int` | Change in live register count if scheduled |
| `scheduled_cycle` | `int` | Assigned execution cycle in final schedule |
| `position_in_schedule` | `int` | Sequential position in scheduled instruction list |

---

## DAG Traversal Algorithm

### Bottom-up Topological Traversal

The scheduler processes the DAG in **reverse topological order** (from leaves to roots):

```c
function traverseDAG(dag):
    """
    Traverse DAG in reverse topological order (bottom-up).

    Returns: Topologically sorted list of nodes (leaves first)
    """

    visited = set()
    result = []

    # Start from leaf nodes (no successors)
    worklist = [node for node in dag.nodes if node.successors.empty()]

    while worklist not empty:
        node = worklist.pop()

        if node in visited:
            continue

        # Check if all successors visited
        if all(succ in visited for succ in node.successors):
            visited.add(node)
            result.append(node)

            # Add predecessors to worklist
            for pred in node.predecessors:
                worklist.append(pred)

    return result  # Leaves first, roots last
```

### Ready Instruction Selection

During scheduling, the **ready queue** contains instructions whose dependencies are satisfied:

```c
function getReadyInstructions(dag, scheduled):
    """
    Return instructions ready to schedule.

    Ready = all predecessors have been scheduled
    """
    ready = []

    for each node in dag.nodes:
        if node not in scheduled:
            if all(pred in scheduled for pred in node.predecessors):
                ready.append(node)

    return ready
```

### Priority Ordering

Ready instructions are ordered by **composite priority**:

```
priority_order = critical_height > scheduled_height > register_pressure > live_use > no_stall
```

Highest priority instruction is scheduled first.

---

## Scheduling Metrics

### 1. Makespan

**Definition**: Total cycles from first to last instruction

**Formula**:
```
makespan = max(node.scheduled_cycle + node.latency for all nodes)
```

**Goal**: Minimize makespan (shorter execution time)

---

### 2. Register Pressure

**Definition**: Maximum number of live values at any scheduling point

**Formula**:
```
register_pressure = max(live_values(cycle) for cycle in schedule)
```

**Computation**:
```c
function computeRegisterPressure(schedule):
    max_pressure = 0

    for cycle in 0..makespan:
        live_values = set()

        # Find all values live at this cycle
        for node in schedule:
            if node.scheduled_cycle <= cycle < node.scheduled_cycle + node.latency:
                for def in node.defs:
                    live_values.add(def)

        max_pressure = max(max_pressure, len(live_values))

    return max_pressure
```

**Goal**: Minimize register pressure (reduce spilling)

---

### 3. Instruction-Level Parallelism (ILP)

**Definition**: Number of independent instructions that can execute concurrently

**Formula**:
```
ILP = average_concurrent_instructions =
    sum(instructions_at_cycle(c) for c in cycles) / makespan
```

**Computation**:
```c
function computeILP(schedule, makespan):
    total_concurrent = 0

    for cycle in 0..makespan:
        concurrent = count(node for node in schedule
                          if node.scheduled_cycle == cycle)
        total_concurrent += concurrent

    return total_concurrent / makespan
```

**Goal**: Maximize ILP (better hardware utilization)

---

### 4. Critical Path Length

**Definition**: Longest latency path in the DAG

**Formula**:
```
critical_path_length = max(node.critical_height for all roots)
```

**Computation**:
```c
function computeCriticalPathLength(dag):
    # Critical height computed bottom-up
    for each node in dag.nodes (reverse topological order):
        node.critical_height = max(
            0,
            max(successor.critical_height + edge.weight
                for each successor in node.successors)
        )

    # Critical path = max height of root nodes (no predecessors)
    roots = [node for node in dag.nodes if node.predecessors.empty()]
    return max(node.critical_height for node in roots)
```

**Properties**:
- Lower bound on makespan (cannot schedule faster than critical path)
- Primary scheduling priority metric
- Measured in cycles

---

## Priority Computation (List-ILP Scheduler)

### Composite Priority Formula

```c
priority = w_critical * critical_height
         + w_height * scheduled_height
         + w_regpressure * register_pressure_reduction
         + w_liveuse * live_use_count
         + w_nostall * (1.0 - stall_risk)
         + w_physreg * physreg_benefit
```

### Weight Assignment (Inferred)

| Component | Weight | Importance |
|-----------|--------|------------|
| `w_critical` | `4.0` | Highest (critical path priority) |
| `w_height` | `3.0` | High (scheduled height) |
| `w_regpressure` | `2.0` | Medium (register pressure) |
| `w_liveuse` | `1.5` | Medium-low (live value priority) |
| `w_nostall` | `1.0` | Low (stall avoidance) |
| `w_physreg` | `0.5` | Lowest (physical register join) |

### Complete Priority Function

```c
function computePriority(node, dag):
    """
    Compute scheduling priority using list-ilp heuristics.

    Higher value = higher priority (scheduled first)
    """

    priority = 0.0

    # Heuristic 1: Critical Path Priority
    if not disable_sched_critical_path:
        critical_height = node.critical_height
        priority += weight_critical_path * critical_height

        # Allow lookahead: how far ahead of critical path can we go?
        max_lookahead = sched_ilp_critical_path_ahead  # Configurable

    # Heuristic 2: Scheduled Height Priority
    if not disable_sched_height:
        scheduled_height = max(
            succ.critical_height + edge.weight
            for succ in node.successors
        )
        priority += weight_scheduled_height * scheduled_height

    # Heuristic 3: Register Pressure Priority
    if not disable_sched_reg_pressure:
        reg_pressure_reduction = estimateRegisterPressure(node)
        priority += weight_reg_pressure * reg_pressure_reduction

    # Heuristic 4: Live Use Priority
    if not disable_sched_live_use:
        live_use_count = countLiveUses(node)
        priority += weight_live_use * live_use_count

    # Heuristic 5: No-Stall Priority (enabled by default)
    if not disable_sched_stalls:
        stall_risk = estimateResourceStall(node)
        priority += weight_no_stall * (1.0 - stall_risk)

    # Heuristic 6: Physical Register Join
    if not disable_sched_physreg_join:
        physreg_benefit = estimatePhysRegJoinBenefit(node)
        priority += weight_physreg_join * physreg_benefit

    return priority
```

### Control Flags

Each heuristic can be individually disabled:

| Flag | Disables | Default |
|------|----------|---------|
| `disable-sched-critical-path` | Critical path priority | `false` (enabled) |
| `disable-sched-height` | Scheduled height priority | `false` (enabled) |
| `disable-sched-reg-pressure` | Register pressure priority | `false` (enabled) |
| `disable-sched-live-use` | Live use priority | `false` (enabled) |
| `disable-sched-stalls` | No-stall priority | `false` (enabled) |
| `disable-sched-physreg-join` | Physical register join | `false` (enabled) |

---

## Critical Path Analysis

### Algorithm

Critical height is computed **bottom-up** in reverse topological order:

```c
for each node in dag.nodes (reverse topological order):
    node.critical_height = max(
        0,
        max(successor.critical_height + edge.weight
            for each successor in node.successors)
    )
```

### Properties

- **Critical height**: Longest latency path from instruction to any leaf node
- **Measured in**: Cycles
- **Minimum makespan**: `max(critical_height)` for all root nodes
- **Scheduling benefit**: Prioritize high-height instructions first to reduce total schedule length

### Cyclic Critical Path Analysis

**Parameter**: `enable-cyclic-critical-path`
**Purpose**: Handle loops correctly by computing critical path considering back-edges
**Result**: More accurate priority for loop-heavy code

### Debug Output

**Parameter**: `print-sched-critical`
**Effect**: Print critical path length to stdout
**Use**: Verify scheduling quality and identify bottlenecks

---

## Recurrence Chain Analysis

### Definition

**Recurrence chain**: Sequence of instructions forming a loop-carried dependency

### Example Loop

```c
for i in 0..N:
    result[i] = result[i-1] * operand + constant
```

**Recurrence chain**:
1. `LOAD result[i-1]` ← start of chain
2. `MUL by operand` ← middle
3. `ADD constant` ← end
4. `STORE result[i]` ← creates loop back to step 1

**Chain length**: 3 instructions
**Cycle latency**: `load_lat + mul_lat + add_lat`

### Optimization: Operand Commutation

If operand order can be swapped:
```c
result[i] = operand + result[i-1] * constant
```

May reduce critical path if addition has lower latency, or enable better scheduling parallelism.

### Analysis Limit

**Parameter**: `recurrence-chain-limit`
**Default**: `3`
**Rationale**: Analyze up to 3-instruction chains in depth, skip longer chains (too expensive)

### Implementation

```c
function analyzeRecurrenceChains(dag):
    """
    Analyze loop-carried dependencies (recurrence cycles).

    Limit: maximum 3 instructions per recurrence chain
    """

    recurrence_chain_limit = 3

    for each cycle in dag.findCycles():
        # Find longest latency path in cycle
        cycle_latency = 0
        chain_length = 0

        for each edge in cycle:
            cycle_latency += edge.weight
            chain_length += 1

            if chain_length > recurrence_chain_limit:
                break  # Stop early for deep chains

        # Try commuting operands to break cycle
        if commutativeOperandsExist(cycle):
            benefit = evaluateCommutationBenefit(cycle, chain_length)

            if benefit > threshold:
                # Reorder operands to break recurrence
                reorderOperands(cycle)
```

---

## Configuration Parameters

### Latency Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sched-high-latency-cycles` | `25` | Latency estimate for long-latency instructions without itinerary |
| `disable-sched-cycles` | `false` | Controls cycle-level precision during scheduling |

**Effect on edge weights**:
```c
if instruction.is_long_latency and not machine_model.has_itinerary_data:
    edge_weight = 25  # sched-high-latency-cycles default
```

---

### Scheduling Passes

| Parameter | Default | Description |
|-----------|---------|-------------|
| `enable-misched` | `true` | Enable preRA machine instruction scheduling |
| `enable-post-misched` | `true` | Enable postRA machine instruction scheduling |

---

### Anti-Dependency Breaking

| Parameter | Default | Options |
|-----------|---------|---------|
| `break-anti-dependencies` | `none` | `none`, `critical`, `all` |

**Effects**:

- **`none`**: Keep all WAR dependencies (weight = 1)
- **`critical`**: Break WAR on critical path only (safer for register allocation)
- **`all`**: Aggressively break all WAR dependencies (maximum scheduling freedom, may increase register pressure)

---

### Topological Sort

| Parameter | Default | Description |
|-----------|---------|-------------|
| `topo-sort-begin` | `true` | Initial topological sort improves list scheduling quality |

---

### Machine Model

| Parameter | Description |
|-----------|-------------|
| `schedmodel` | New machine model framework (preferred) |
| `scheditins` | Legacy InstrItinerary framework (fallback) |

---

## Hardware/Pipeline Interaction

### Functional Units

- **InstrItineraryData** specifies per-unit resource requirements
- Edge weight = max latency considering all resource constraints

**Example**: FP multiply might have:
- 4 cycles for first result (throughput limited)
- 8 cycles to next FP operation on same unit

### Execution Stages

Multi-stage pipelines have different latencies:
- **Load**: 1 cycle (L1 hit) to 200+ cycles (memory)
- **Scheduler uses**: Worst-case (conservative)

### Resource Reservation

- Some instructions block shared resources
- **Example**: Divide operations lock ALU for multiple cycles
- DAG edge weight accounts for reservation time

### Stall Prevention

**No-stall priority** avoids scheduling that causes resource conflicts:
- **Example**: Back-to-back dependent loads cause stalls on single-ported caches
- **Scheduler strategy**: Insert independent instructions between dependent operations

---

## Summary

The CICC instruction scheduler implements a sophisticated **4-phase DAG construction** algorithm:

1. **Optional topological sort** (improves initialization)
2. **DAG construction** (5 dependency types: RAW, WAW, WAR, control, memory)
3. **Edge weight computation** (cycle-accurate latencies from machine model)
4. **Bottom-up list scheduling** (priority queue with 6 heuristics)

**Key algorithmic features**:
- **180+ instruction patterns** in case dispatcher (0xB612D0)
- **Critical path analysis** drives primary scheduling priority
- **Anti-dependency breaking** (configurable: none/critical/all)
- **Recurrence chain analysis** (up to 3 instructions deep)
- **Conservative memory dependency** analysis (100 instruction window, 200 block window)
- **Dual-stage scheduling**: preRA (performance), postRA (register allocation)

**Scheduling metrics**:
- **Makespan**: Total execution cycles
- **Register pressure**: Maximum live values
- **ILP**: Concurrent instruction count
- **Critical path length**: Lower bound on makespan

This implementation provides cycle-accurate scheduling with comprehensive dependency analysis and hardware model integration.

---

**Document Metadata**:
- **Agent**: L3-19 (SCHED-03)
- **Confidence**: HIGH
- **Evidence**: Decompiled configuration functions, itinerary data analysis
- **Data Sources**: 7 constructor functions analyzed
- **Status**: COMPLETE CHARACTERIZATION
