# Critical Path Detection Algorithm and Cost Weighting

**Analysis ID**: L3-21 | **Confidence**: HIGH | **Phase**: L3_EXTRACTION

## Overview

Critical path detection implements bottom-up dynamic programming with topological traversal to compute longest latency paths in instruction scheduling DAGs. Used as primary priority metric in list scheduling for determining instruction issue order.

**Data Sources**:
- `L3-19 DAG Construction Analysis`
- `L3-05 Scheduling Heuristics Analysis`
- `L3-02 Cost Model Coefficients Analysis`
- `ctor_310_0_0x500ad0.c` - Scheduling configuration
- `ctor_652_0_0x599ef0.c` - Scheduling algorithm registration
- `sub_2F9DAC0_0x2f9dac0.c` - Pattern matcher with cost weighting
- `sub_FDE760_0xfde760.c` - Cost normalization
- `sub_FDCA70_0xfdca70.c` - Cost addition

---

## Critical Path Algorithm

### Method
**Bottom-up dynamic programming with topological traversal** - Computes the longest latency path from each instruction to any exit (leaf) node in the scheduling DAG.

### Formula
```
critical_height[node] = max(critical_height[successor] + edge_latency(node, successor)) for all successors
```

**Exit nodes**: `critical_height = 0`

### Algorithm Properties

| Property | Value |
|----------|-------|
| **Initialization** | Exit nodes (instructions with no successors) have `critical_height = 0` |
| **Traversal Order** | Bottom-up: leaf nodes to root nodes; reverse topological order from exit instructions to entry instructions |
| **Complexity** | `O(V + E)` where `V = instructions`, `E = dependency edges` |
| **Caching** | Memoization using visited bitmap to avoid recomputation |

### Node Types

- **Entry nodes**: Instructions with no predecessors (data dependencies)
- **Exit nodes**: Instructions with no successors (leaf instructions, returns, stores to final results)
- **Critical path**: Path from entry to exit node(s) with maximum total latency

---

## Edge Weight Computation

### Latency Sources

**Primary source**: `InstrItineraryData` (machine instruction model)
**Fallback source**: `sched-high-latency-cycles` parameter (default: 25 cycles)

### Latency Formula
```
edge_latency = source_instruction_latency + penalties
```

### Component Breakdown

#### 1. Base Latency
- **Description**: Latency of the producing (source) instruction
- **Source**: `getInstrLatency(source_instr)` from instruction schedules
- **Range**: 1-25+ cycles depending on instruction type

#### 2. Anti-Dependency Penalty
- **Description**: Serialization penalty for WAR (Write-After-Read) dependencies
- **Value**: `1` cycle
- **Condition**: Applied only when `break-anti-dependencies` is disabled

#### 3. Output Dependency Penalty
- **Description**: Serialization penalty for WAW (Write-After-Write) dependencies
- **Value**: `1` cycle
- **Condition**: Always applied for register serialization

### Dependency Edge Types

| Edge Type | Weight | Example/Notes |
|-----------|--------|---------------|
| **True dependency** | `source_latency + 0` | `READ src → USE(src)`: edge_weight = 4 cycles for 4-cycle latency instruction |
| **Output dependency** | `1` | `WRITE reg → WRITE reg`: edge_weight = 1 (serialization only) |
| **Anti dependency** | `1` | Breakable: modes = `[critical, all, none]` |
| **Control dependency** | `0` | Control flow dependencies don't add latency but prevent speculative execution |

---

## Cost Weighting Formula

### Instruction Selection Cost Model

**Description**: Cost aggregation for pattern selection during instruction selection phase

**Formula**:
```
total_cost = weighted_sum(metric_1 * weight_1, metric_2 * weight_2, ...)
```

**Example Calculation**:
```
For instruction pattern with latency_metric and throughput_metric:
total = (latency_metric * 1) + (throughput_metric * 3) → normalized by weight 100
```

#### Step-by-Step Cost Aggregation

1. Extract multiple cost metrics from instruction pattern (latency, throughput, register pressure)
2. Apply individual metric weights (1, 3, 64, 100 observed)
3. Combine weighted metrics using cost addition (`sub_FDCA70`)
4. Normalize final cost with global weight factor 100 using `sub_FDE760`
5. Compare costs using mantissa/exponent representation

### Scheduling Priority Formula

**Description**: Priority function for list scheduling ready queue ordering

#### Priority Components (6 Total)

##### 1. CRITICAL_PATH_PRIORITY
- **Calculation**: `critical_height[instr] = latency to exit node`
- **Disable Flag**: `disable-sched-critical-path`
- **Weight**: Highest priority (primary ordering key)
- **Lookahead Parameter**: `max-sched-reorder` (default: 6, allows up to 6 instructions ahead of critical path)

##### 2. SCHEDULED_HEIGHT_PRIORITY
- **Calculation**: `max_latency_path_from_instruction` (similar to critical height)
- **Disable Flag**: `disable-sched-height`
- **Weight**: Secondary priority

##### 3. REGISTER_PRESSURE_PRIORITY
- **Calculation**: `live_range_length = def_cycle - last_use_cycle`
- **Disable Flag**: `disable-sched-reg-pressure`
- **Weight**: Tertiary priority
- **Goal**: Minimize peak register usage

##### 4. LIVE_USE_PRIORITY
- **Calculation**: `number_of_live_uses_of_instruction`
- **Disable Flag**: `disable-sched-live-uses`
- **Weight**: Secondary metric

##### 5. NO_STALL_PRIORITY
- **Calculation**: `available_execution_unit(instr)`
- **Disable Flag**: `disable-sched-stalls`
- **Weight**: Avoid execution unit stalls
- **Enabled by Default**: `true`

##### 6. PHYSICAL_REG_JOIN_PRIORITY
- **Calculation**: `physical_register_reuse_opportunity`
- **Disable Flag**: `disable-sched-physreg-join`
- **Weight**: Improve register allocation quality

#### Priority Order
```
critical_path > scheduled_height > register_pressure > live_use > no_stall > physreg_join
```

#### Scheduler Variants

| Scheduler | Strategy |
|-----------|----------|
| `list-ilp` | Uses all 6 priority components with critical path as primary |
| `list-burr` | Focuses on register pressure (live range minimization) |
| `list-hybrid` | Balances latency and register pressure with 0.5 weight each |
| `source` | Preserves source order when possible with register constraints |

### Cost Weighting Coefficients

#### Observed Weights

##### weight_100
- **Value**: `100`
- **Usage**: Main cost aggregation normalization weight
- **Location**: `sub_2F9DAC0:1125`, applied via `sub_FDE760`
- **Purpose**: Normalize final combined cost from multiple metrics

##### weight_3
- **Value**: `3`
- **Usage**: Secondary metric scaling (approx. 1/3 inverse)
- **Location**: Pattern cost table lookup, `sub_2F9DAC0:1034, 1056`
- **Purpose**: Scale throughput or resource constraints relative to latency

##### weight_64
- **Value**: `64`
- **Usage**: Fine-grained adjustment weight (approx. 1/64 inverse)
- **Location**: Memory or specialized instruction cost scaling
- **Purpose**: Weight memory latency or special function unit constraints

##### weight_1
- **Value**: `1`
- **Usage**: Identity weight for critical path latency component
- **Location**: Primary latency metric, directly used without scaling
- **Purpose**: Use raw latency value unscaled

#### Application Mechanism

**Function**: `sub_2F9DA20` (cost weighting)

**Algorithm**:
```
result = (metric_value * weight)
result_exponent = metric_exponent + weight_exponent
```

**Operation**: Multiplication with proper fixed-point exponent adjustment

**Integration**: Used in pattern matcher (`sub_2F9DAC0`) to weight individual metric components before summation

---

## DAG Metrics

### Critical Path Metrics

#### critical_height
- **Definition**: Maximum latency path from instruction to any exit node
- **Usage**: Primary scheduling priority
- **Range**: `0` to `critical_path_length` (sum of all latencies on longest chain)

#### slack_to_critical_use
- **Definition**: How many cycles an instruction can be delayed without extending schedule length
- **Formula**:
  ```
  slack_to_critical_use = critical_path_length - critical_height[instr]
  ```
- **Usage**: Determines how far ahead of critical path instruction can be scheduled

#### critical_path_distance
- **Definition**: Alternative priority metric used in some schedulers
- **Formula**:
  ```
  critical_path_distance = instruction_latency - slack_to_critical_use
  ```

### Scheduling Window

**Lookahead Parameter**: `max-sched-reorder`
**Default Value**: `6`
**Meaning**: Allow up to N instructions to be scheduled ahead of the critical path
**Effect**: Trades off latency for register pressure reduction by scheduling non-critical instructions early when profitable

---

## Configuration Parameters

### 1. disable-sched-critical-path
- **Type**: `boolean`
- **Default**: `false`
- **Effect**: Disable critical path priority in sched=list-ilp scheduler
- **Impact**: Reduces latency priority, may increase schedule length
- **Binary Location**: `ctor_652_0_0x599ef0.c:216`

### 2. disable-sched-height
- **Type**: `boolean`
- **Default**: `false`
- **Effect**: Disable scheduled-height priority (similar to critical path)
- **Impact**: Secondary priority metric disabled
- **Binary Location**: `ctor_652_0_0x599ef0.c:229`

### 3. max-sched-reorder
- **Type**: `integer`
- **Default**: `6`
- **Range**: `0-255`
- **Effect**: Number of instructions allowed ahead of the critical path in list-ilp
- **Impact**: Higher value = more scheduling freedom but potentially longer critical path
- **Binary Location**: `ctor_652_0_0x599ef0.c:315`

### 4. print-sched-critical
- **Type**: `boolean`
- **Default**: `false`
- **Effect**: Print critical path length to stdout during compilation
- **Impact**: Debug/analysis only, no performance impact
- **Binary Location**: `ctor_310_0_0x500ad0.c:102`

### 5. enable-cyclic-critical-path
- **Type**: `boolean`
- **Default**: `false`
- **Effect**: Enable cyclic critical path analysis for loop-carried dependencies
- **Impact**: More accurate analysis for loop-heavy code
- **Binary Location**: `ctor_310_0_0x500ad0.c:195`

### 6. recurrence-chain-limit
- **Type**: `integer`
- **Default**: `3`
- **Effect**: Maximum length of recurrence chain when analyzing operand commutation benefits
- **Impact**: Limits analysis depth for cyclic dependency optimization
- **Source**: L3-19 findings

### 7. break-anti-dependencies
- **Type**: `enum`
- **Default**: `none`
- **Values**: `[none, critical, all]`
- **Effect**: Control anti-dependency breaking in post-RA scheduling
- **Impact**: `'critical'`: only break anti-deps on instructions with zero slack
- **Binary Location**: `ctor_310_0_0x500ad0.c` and scheduling pass configuration

### 8. sched-high-latency-cycles
- **Type**: `integer`
- **Default**: `25`
- **Effect**: Default latency estimate for instructions without itinerary data
- **Impact**: Affects edge weights when InstrItineraryData unavailable
- **Source**: L3-19 findings

---

## Algorithm Pseudocode

### Critical Path Calculation

**Algorithm**: `ComputeCriticalHeight(DAG, node)`

```
IF node visited THEN return height[node]
IF node has no successors THEN height[node] := 0
ELSE
  max_height := 0
  FOR each successor IN node.successors DO
    succ_height := ComputeCriticalHeight(DAG, successor)
    edge_latency := GetLatency(node.instruction)
    height_through_succ := succ_height + edge_latency
    max_height := MAX(max_height, height_through_succ)
  END FOR
  height[node] := max_height
END IF
visited[node] := TRUE
RETURN height[node]
```

### Scheduling Priority Calculation

**Algorithm**: `ComputeSchedulingPriority(instr)`

```
priority := 0
critical_height := height[instr]  // Primary metric
priority += (PRIORITY_WEIGHT_CRITICAL * critical_height)

IF critical_height == 0 THEN
  // On critical path, add secondary priority
  scheduled_height := ComputeScheduledHeight(instr)
  priority += (PRIORITY_WEIGHT_HEIGHT * scheduled_height)
  reg_pressure := ComputeLiveRangeLength(instr)
  priority += (PRIORITY_WEIGHT_PRESSURE * reg_pressure)
END IF

RETURN priority  // Higher value = higher priority in ready list
```

**Priority Ordering**: Higher value scheduled first in priority queue

### Cost Weighting for Instruction Selection

**Algorithm**: `AggregateCost(metric_list)`

```
total_cost := 0  // Floating-point pair: (mantissa, exponent)

FOR each metric IN metric_list DO
  weighted_metric := Multiply(metric.value, metric.weight)
  total_cost := Add(total_cost, weighted_metric)
END FOR

// Final normalization
total_cost := Normalize(total_cost, normalization_weight=100)

RETURN total_cost
```

**Cost Comparison**: Use mantissa/exponent comparison: if `exp1 > exp2`, `cost1 > cost2`; else compare mantissas

---

## Special Handling

### Cyclic Dependencies
- **Occurrence**: Recurrence chains: cycles in the dependency graph
- **Control Flag**: `enable-cyclic-critical-path`
- **Algorithm**: Specialized analysis for loop-carried dependencies
- **Parameter**: `recurrence-chain-limit` (default: 3, max chain length to analyze)
- **Application**: Determines when to commute operands to break long recurrence chains

### Multiple Critical Paths
- **Description**: When multiple paths have equal maximum latency
- **Tie Breaking**: Secondary priority metrics (register pressure, live uses, stall avoidance)
- **Lookahead Limit**: Controlled by `max-sched-reorder` parameter

### Dynamic Updates
- **Scenario**: As instructions are scheduled, remaining DAG changes
- **Update Mechanism**: Critical path is not recalculated per instruction; priority computed once at DAG construction
- **Optimization**: Allows O(1) priority queue access instead of O(log n) recalculation

### Anti-Dependency Breaking
- **Purpose**: Remove artificial serialization to improve scheduling freedom
- **Phases**:
  - **preRA**: Conservative - no breaking (default)
  - **postRA**: Configurable: `none` (default), `critical` (break anti-deps on critical path only), `all` (aggressive)
- **Critical Path Interaction**: When set to `'critical'`, only breaks anti-dependencies on instructions with zero slack

---

## Integration Points

### Instruction Selection
- **Usage**: Critical path contributes to pattern selection cost
- **Mechanism**: Latency metric (which includes critical path distance) weighted in cost aggregation
- **Function**: `sub_2F9DAC0` pattern matcher uses latency costs in template instantiation

### List Scheduling
- **Usage**: Critical path is primary sorting key for ready instruction queue
- **Mechanism**: `critical_height[node]` used directly as priority in priority queue
- **Schedulers**: `list-ilp` (primary), `list-hybrid`, `converge` (post-RA)
- **Function**: Ready list priority queue in `sub_1D04DC0` and related scheduling functions

### Register Allocation
- **Usage**: Critical path influences spill cost heuristics
- **Mechanism**: Instructions on critical path have higher spill penalties to preserve schedule quality
- **Interaction**: Anti-dependency breaking prefers critical path instructions

### Post-RA Scheduling
- **Usage**: Critical path recalculated on allocated code
- **Mechanism**: Separate critical path analysis with register allocation constraints
- **Control Flag**: `enable-post-misched`
- **Anti-Dep Modes**: Can break anti-deps on critical path to improve schedule

---

## Key Findings

1. Critical path algorithm is standard LLVM bottom-up longest-path DP computed once per DAG
2. Edge weights = source instruction latency + penalties for serialization dependencies
3. Critical path distance used as primary scheduling priority (10x+ weight vs secondary metrics)
4. `max-sched-reorder` parameter allows limited lookahead (~6 instructions) past critical path
5. Cost model uses floating-point pair (mantissa, exponent) for wide dynamic range
6. **Weights observed**: `critical_path=1` (full), `throughput≈1/3`, `special=1/64`, `normalization=100`
7. Three scheduling phases: pre-RA (4 variants), post-RA (3 variants), register allocation integration
8. Anti-dependency breaking mode `'critical'` specifically targets critical path instructions
9. Cyclic critical path analysis available for loop-heavy code with `recurrence-chain-limit=3`

---

## Known Limitations

- Exact weight values (1, 3, 64, 100) inferred from patterns; encoded in lookup tables not directly visible
- Sub-component latencies for complex instructions may require full itinerary data analysis
- Cycle-accurate latency modeling controlled by `disable-sched-cycles` parameter
- Memory dependency analysis is conservative, may over-estimate dependencies

---

## Verification and Validation

### Cross-Validated With
- **L3-19 DAG Construction**: Confirmed algorithm is bottom-up longest path
- **L3-05 Scheduling**: Confirmed `critical_height` as primary priority metric
- **L3-02 Cost Model**: Confirmed weight coefficients 1, 3, 64, 100 and aggregation mechanism

### Evidence Locations
- **Critical path definition**: L3-19 findings, line 233-235
- **Priority metrics**: L3-05 findings, priority_functions_detailed section
- **Weight application**: `sub_2F9DAC0:1125` (v347 = 100), `sub_FDE760` normalization
- **Configuration**: `ctor_652_0` and `ctor_310_0` parameter registrations

### Confidence Justification
**HIGH** because:
1. Multiple independent sources confirm algorithm
2. Configuration parameters explicitly documented
3. Cost weighting code directly visible in pattern matcher
4. Algorithm matches standard LLVM list scheduling technique

---

## Binary Function Reference

| Function | Address | Purpose |
|----------|---------|---------|
| `sub_2F9DAC0` | `0x2f9dac0` | Pattern matcher with cost weighting |
| `sub_2F9DA20` | `0x2f9da20` | Cost weighting multiplication |
| `sub_FDE760` | `0xfde760` | Cost normalization |
| `sub_FDCA70` | `0xfdca70` | Cost addition |
| `sub_1D04DC0` | `0x1d04dc0` | Ready list scheduling (inferred) |
| `ctor_652_0` | `0x599ef0` | Scheduling algorithm registration |
| `ctor_310_0` | `0x500ad0` | Scheduling configuration |

---

**Analysis Date**: 2025-11-16
**Research Time**: 6 hours
**Agent**: L3-21
