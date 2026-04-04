# Instruction Scheduling

The embedded ptxas backend in nvlink v13.0.88 contains two complete instruction scheduling subsystems: the **pre-register-allocation scheduler** (three named strategy modes operating on IR-level instructions) and the **tepid scheduler** (a post-register-allocation pipeline simulator that assigns stall counts, yield hints, and scoreboard barriers to the final SASS instruction stream). Both subsystems run per-function and per-basic-block. Together they span approximately 1.2 MB of code across three address ranges: `0x1680000`--`0x16E0000`, `0x16F6000`--`0x1740000`, and `0x1850000`--`0x186F000`, plus scoreboard/dependency tracking at `0x1B40000`--`0x1B60000`.

## Overview of the Two Schedulers

The compilation pipeline invokes scheduling at two distinct points:

1. **Pre-RA scheduling** (`ScheduleInstructions` and variants). Runs before register allocation on the internal IR. Its goal is to maximize instruction-level parallelism (ILP) and hide memory latency while respecting a register pressure budget. The three strategy modes -- `ScheduleInstructions`, `ScheduleInstructionsReduceReg`, and `ScheduleInstructionsDynBatch` -- are selected per-function based on workload characteristics and knob configuration.

2. **Post-RA scheduling** (the "tepid" scheduler). Runs after register allocation on the SASS instruction stream with physical register assignments finalized. It models the GPU hardware pipeline, computes concrete stall counts, assigns scoreboard barriers, determines dual-issue pairing, inserts yield hints, and sets the scheduling control words that appear every 4th instruction in the SASS binary.

The two schedulers communicate indirectly through the register allocation pass: the pre-RA scheduler's instruction ordering influences register pressure, which in turn determines spill/fill counts that the post-RA scheduler must accommodate.

## Pre-RA Scheduling: ScheduleInstructions

### Entry Point and Driver Hierarchy

The main entry point is `sub_1851DC0` (85 KB, 2,938 lines) -- `ScheduleInstructions_main_driver`. This is one of the largest single functions in the scheduling subsystem. It takes a compilation context, an IR module, and a function pointer, and orchestrates the entire pre-RA scheduling pass for one function.

The driver hierarchy is:

```
ScheduleInstructions_main_driver  (0x1851DC0, 85 KB)
  -> ScheduleInstructions_per_function_driver  (0x1860A40, 47 KB)
       -> ScheduleInstructions_per_block_driver  (0x185B870, 28 KB)
            -> ScheduleInstructions_block_scheduler_core  (0x1867D60, 22 KB)
                 -> schedule_list_scheduler_core  (0x1864ED0, 17 KB)
```

The main driver also conditionally invokes two companion passes that are tightly integrated with scheduling:

- `HoistInvariants` -- loop-invariant code motion, lifted out of loops before scheduling.
- `OptimizeNaNOrZero` -- a peephole pass that simplifies NaN-producing and zero-producing instruction sequences.
- `ConvertMemoryToRegisterOrUniform` -- promotes shared memory accesses to register or uniform register operations where safe.

### Three Scheduling Modes

The strategy selection function at `0x1857990` (13 KB) chooses between three modes:

| Mode | Pass Name | Purpose | When Selected |
|---|---|---|---|
| Default | `ScheduleInstructions` | Maximize ILP and latency hiding | General-purpose kernels |
| ReduceReg | `ScheduleInstructionsReduceReg` | Minimize register pressure | When register pressure exceeds budget |
| DynBatch | `ScheduleInstructionsDynBatch` | Dynamic batching for throughput | CUTLASS workloads and GEMM patterns |

The mode name appears as a literal string in the binary for each pass invocation. The strategy selector at `0x1857990` checks multiple configuration knobs (10 vtable calls observed) and returns a boolean indicating whether ReduceReg mode should be used. DynBatch selection is driven by CUTLASS pattern detection.

**ScheduleInstructions** (default mode) uses a standard list-scheduling algorithm. The core loop at `0x1864ED0` (17 KB) maintains a ready queue, selects the highest-priority instruction from the ready set, issues it, and updates the dependency DAG. Priority is computed by the critical-path calculator and takes into account instruction latencies, functional unit pressure, and memory-hierarchy distance.

**ScheduleInstructionsReduceReg** reorders instructions to reduce the number of simultaneously live registers. The reordering pass at `0x185D760` (9 KB) copies instruction buffers via `memcpy` and rearranges them to shorten live ranges. This mode is engaged when the pre-RA scheduler detects that the default ordering would cause register allocation to spill.

**ScheduleInstructionsDynBatch** is specialized for dense linear-algebra kernels (CUTLASS GEMM patterns). The DynBatch heuristic at `0x185F980` (6 KB) groups instructions into dynamic batches that can be issued as a unit, maximizing throughput on the tensor core and memory pipelines. The CUTLASS workload detector at `0x1866CF0` (3.5 KB) and the CUTLASS pattern handler at `0x1868E50` (19 KB) identify and special-case these patterns.

### List Scheduling Data Structures

The scheduler operates on a per-basic-block dependency DAG. Key data structures:

**184-byte per-BB scheduling records.** Stored in a growable array at context offset `+832`. Each record contains:

```
Offset  Size    Field
+0      8B      basic block pointer
+4      4B      scheduling latency / timing info
+8      128B    constraint arrays for instruction positions
+136    4B      instruction count
+140    4B      scheduled instruction count
+144    4B      max register pressure seen
+148    4B      current cycle count
+152    32B     scoreboard state snapshot
```

Capacity tracking at offset `+840`. Overflow entries use a hash-table/linked-list at offset `+864`. Scheduling contexts (192 bytes each) are arena-allocated at offset `+848`.

**DAG construction.** The DAG builder at `0x1858730` (12 KB) constructs dependency edges via `schedule_build_dependency_edge` at `0x1850760` (5 KB). Edges encode four hazard types: RAW (read-after-write), WAR (write-after-read), WAW (write-after-write), and memory ordering dependencies. The DAG is compacted by `0x1858FA0` (4.6 KB) after construction to remove redundant transitive edges.

**Ready queue.** The ready-list selector at `0x18592C0` (6 KB) picks the next instruction to schedule from among all instructions whose predecessors have been scheduled. Selection priority factors include: critical-path height, instruction latency, functional unit availability, and (in ReduceReg mode) the register-pressure delta.

### Register Pressure Tracking

Register pressure is tracked incrementally during scheduling by the pressure tracker at `0x185C40` (12 KB). The delta function at `0x1859F10` (4.3 KB) computes the net register pressure change from issuing a given instruction (positive for definitions, negative for last uses). In ReduceReg mode, the scheduler penalizes instructions that increase pressure beyond a target threshold.

The live-range interference checker at `0x185D4B0` (4.3 KB) detects cases where scheduling two instructions back-to-back would create an interference that register allocation cannot resolve without spilling.

### CUTLASS-Aware Scheduling

Six functions specifically handle CUTLASS workloads:

| Address | Size | Function |
|---|---|---|
| `0x1866CF0` | 3.5 KB | `schedule_check_cutlass_workload` -- detects CUTLASS pattern |
| `0x1866FA0` | 28 KB | `schedule_optimize_nan_or_zero` -- NaN/zero peephole with CUTLASS awareness |
| `0x1868E50` | 19 KB | `schedule_handle_cutlass_pattern` -- CUTLASS GEMM scheduling |
| `0x186A9F0` | 14 KB | `schedule_reorder_memory_ops` -- memory operation reordering for CUTLASS |
| `0x186BE40` | 14 KB | `schedule_optimize_texture_ops` -- texture op scheduling for CUTLASS |
| `0x185F980` | 6 KB | `schedule_dynbatch_heuristic` -- DynBatch mode heuristic |

The CUTLASS detection mechanism checks function names and instruction patterns for the characteristic GEMM structure: interleaved tensor-core MMA instructions with global memory loads and shared memory stores. When detected, the scheduler applies specialized reordering that overlaps MMA computation with memory transfers, a pattern critical for achieving peak throughput on tensor cores.

## HoistInvariants Pass

The `HoistInvariants` pass is invoked from the scheduling driver before the main scheduling loop. It is a loop-invariant code motion (LICM) pass operating at the SASS IR level.

### Driver Hierarchy

```
HoistInvariants_analysis_driver  (0x186EE80, 41 KB)
  -> HoistInvariants_per_function  (0x186D520, 38 KB)
       -> HoistInvariants_core  (0x186C7A0, 24 KB)
            -> hoist_analyze_loop_body  (0x1871050, 19 KB)
            -> hoist_perform_transformation  (0x1873580, 12 KB)
```

The analysis driver at `0x186EE80` (41 KB, 1,603 lines) identifies hoistable instructions and performs the actual transformation. It takes a context, an instruction, a function, and an output count pointer.

### Hoisting Pipeline

1. **Candidate identification.** `hoist_collect_candidates` (`0x1882F20`) gathers instructions that may be loop-invariant. An instruction is a candidate if all its operands are defined outside the loop or are themselves loop-invariant.

2. **Side-effect check.** `hoist_is_side_effect_free` (`0x1883590`) checks whether the instruction has observable side effects (memory writes, barriers, etc.). Instructions with side effects are not hoisted.

3. **Alias analysis.** `hoist_analyze_memory_aliasing` (`0x1886360`, 13 KB, 13 vtable calls) queries the alias analysis infrastructure to determine whether hoisting a load past a store is safe. `hoist_check_alias_safety` (`0x1886B70`) provides the final safety verdict.

4. **Cost-benefit analysis.** `hoist_compute_cost_benefit` (`0x1873030`) weighs the benefit of removing an instruction from the loop body against the cost of increasing register pressure in the loop preheader. The cost model uses floating-point arithmetic.

5. **Transformation.** `hoist_perform_transformation` (`0x1873580`, 12 KB) moves the instruction from its original basic block to the loop preheader via `hoist_insert_at_preheader` (`0x1884B80`). Phi nodes are updated by `hoist_update_phi_nodes` (`0x1882A70`).

6. **Liveness update.** `hoist_update_liveness` (`0x1876BC0`) recomputes live-in/live-out sets for affected blocks.

The pass is CUTLASS-aware: multiple helper functions (`hoist_handle_shared_memory` at `0x1874B40`, `hoist_handle_texture_ops` at `0x1877200`, `hoist_handle_special_instructions` at `0x1877BF0`) contain special handling for shared-memory, texture, and other CUTLASS-relevant instruction patterns.

## OptimizeNaNOrZero Pass

The `OptimizeNaNOrZero` peephole runs immediately after invariant hoisting. Its entry point is `0x1866FA0` (28 KB) with a core implementation at `0x187C80` (18 KB). The pass identifies floating-point instruction sequences that produce NaN or zero results and simplifies them.

Key sub-passes:

- `nan_zero_check_operand_pattern` (`0x187AA90`) -- matches known NaN-producing patterns (e.g., `0.0 * x` where `x` may be infinity).
- `nan_zero_propagate_through_phi` (`0x187DDD0`, 13 KB) -- propagates NaN/zero knowledge through phi nodes across basic block boundaries.
- `nan_zero_speculative_elimination` (`0x187EB20`) -- speculatively eliminates NaN checks when the producer is known to be non-NaN.
- `nan_zero_transform_branch` (`0x187C0C0`, 9 KB) -- simplifies conditional branches that test for NaN or zero when the condition is statically determinable.

## Tepid Scheduler (Post-RA)

The tepid scheduler is NVIDIA's post-register-allocation instruction scheduler. It operates on the final SASS instruction stream with physical register assignments, modeling the actual hardware pipeline to produce optimal scheduling control words. The name "tepid" appears in internal string references (`"TepidMacUtil"`, `"TepidTime"`) and likely refers to a "warm" scheduling approach -- not as aggressive as full software pipelining, but more than simple in-order emission.

### Address Ranges

The tepid scheduler spans two regions:

1. **`0x16F6000`--`0x1740000`** (~296 KB, ~80 functions): the core tepid scheduling engine, including the main loop, pipeline model, resource tracking, scoreboard management, software pipelining, and statistics.

2. **`0x1B40000`--`0x1B60000`** (~128 KB, ~10 functions): scoreboard dependency tracking and optimization, control-word building.

### Main Loop and Pipeline Model

The tepid scheduler's main loop is `sub_17027F0` (38 KB, 1,216 lines) -- `tepid_scheduler_main_loop`. It walks basic-block instruction lists from begin to end, simulating the GPU execution pipeline.

For each basic block:

1. Count instructions using opcode classification (`sub_17662F0`, opcode 443).
2. Initialize min/max timing values (min initialized to `0x7FFFFFFF`).
3. Iterate through instructions, tracking:
   - `v447`/`v448`: instruction counts
   - `v439`/`v445`: min/max execution times
   - `v436`/`v444`: LDS (shared memory load) timing
   - `v438`/`v440`: LDG (global memory load) timing
   - `v437`/`v443`: Xu64 (64-bit extended) timing
4. Call `__popcountdi2` for population count on register masks (register set analysis).
5. Use vtable callbacks at `a1+16` for ISA-specific scheduling decisions.

The main loop references four distinct loop categories in its string context:
- **"For Mac Loop"** -- multiply-accumulate dominated loops (tensor/GEMM)
- **"For Dma Loop"** -- DMA/memory-transfer dominated loops
- **"For Math Loop"** -- general ALU-dominated loops
- **"For Epilogue"** -- loop epilogue regions

### Pipeline Simulation Components

| Address | Size | Function | Role |
|---|---|---|---|
| `0x16F7370` | 5 KB | `scheduler_latency_calculator` | Compute per-instruction latencies |
| `0x16F7830` | 5 KB | `scheduler_resource_tracker` | Track functional unit availability |
| `0x16F7BB0` | 4 KB | `scheduler_dependency_checker` | Check data dependencies between instructions |
| `0x16F7F70` | 4 KB | `scheduler_stall_detector` | Detect pipeline stalls from resource conflicts |
| `0x16F8640` | 5 KB | `scheduler_reuse_tracker` | Track register reuse opportunities |
| `0x16F8B80` | 4 KB | `scheduler_issue_slot_manager` | Manage instruction issue slot assignment |
| `0x16FF350` | 8 KB | `scheduler_cycle_counter` | Count execution cycles |
| `0x16FF8F0` | 13 KB | `scheduler_pipeline_modeler` | Model GPU pipeline state (539 lines) |

The pipeline modeler at `0x16FF8F0` is the core simulation engine. It models the GPU's execution pipeline as a set of functional unit queues with known latencies. Each instruction is placed into the appropriate queue based on its functional unit class, and the pipeline state is advanced cycle-by-cycle.

### Functional Unit Models

The tepid scheduler contains dedicated handlers for each major GPU functional unit:

| Address | Size | Handler |
|---|---|---|
| `0x1722910` | 6 KB | Math/ALU unit modeler |
| `0x1722C40` | 12 KB | Texture unit handler |
| `0x1724230` | 6 KB | Shared memory handler |
| `0x1724F10` | 7 KB | Global memory handler |
| `0x17271C0` | 7 KB | Special function unit (SFU) handler |
| `0x1727560` | 5 KB | Tensor core (MMA) handler |
| `0x1727BC0` | 11 KB | Tensor core latency model |
| `0x1728320` | 5 KB | DMA engine handler |
| `0x17287E0` | 11 KB | DMA latency model |

Each handler models the specific pipeline depth, throughput, and resource constraints of its target unit. The tensor core and DMA handlers have separate latency models (11 KB each), reflecting the complexity of modeling asynchronous, multi-cycle operations.

### Knob 610: Scheduling Aggressiveness

The tepid scheduler's behavior is controlled by knob 610, queried via vtable dispatch. The block processor at `0x16F35A0` (36 KB) checks this knob to select scheduling aggressiveness:

- **Knob 610 = 0**: Scheduling disabled (early return).
- **Knob 610 = 1**: Standard tepid scheduling.
- **Knob 610 = 2+**: Progressively more aggressive scheduling with deeper pipeline lookahead.

The block processor also checks the architecture capability at knob-table offset `+43920` to gate SM-version-specific scheduling features.

### Math-to-DMA Ratio Balancing

A critical heuristic in the tepid scheduler is the math-to-DMA ratio balancer. Two named ratios appear in string references:

- **`MathToDmaWaitRatio`** -- the ratio of math instruction cycles to DMA wait cycles. When this ratio is too low, the scheduler has insufficient math work to hide DMA latency.
- **`MathToDmaTepidRatio`** -- the tepid scheduler's target ratio. The scheduler attempts to interleave math and DMA instructions to approach this target.

Additional ratios for epilogue regions:
- **`MathToEpilogueWaitRatio`**
- **`MathToEpilogueTepidRatio`**

The DMA-math balancer at `0x1717A00` (12 KB) and the epilogue optimizer at `0x1729F10` (19 KB) compute these ratios and adjust instruction ordering to maximize latency hiding.

### Software Pipelining

The tepid scheduler includes software pipelining support for loop bodies:

| Address | Size | Function |
|---|---|---|
| `0x17130F0` | 11 KB | `scheduler_software_pipeline` -- main software pipelining |
| `0x1713930` | 8 KB | `scheduler_software_pipeline_helper` |
| `0x1714870` | 4 KB | `scheduler_modulo_schedule_helper` |
| `0x17151D0` | 6 KB | `scheduler_iteration_interval` -- compute initiation interval |
| `0x17157F0` | 5 KB | `scheduler_stage_assignment` -- assign pipeline stages |
| `0x1712B70` | 4 KB | `scheduler_loop_rotation` -- loop rotation for pipelining |

The iteration interval calculator at `0x17151D0` computes the minimum initiation interval (II) based on resource constraints and recurrence constraints. The stage assignment at `0x17157F0` assigns each instruction to a pipeline stage for modulo scheduling. Loop rotation at `0x1712B70` transforms the loop structure to enable overlapped execution of iterations.

### Dual-Issue Optimization

The dual-issue checker at `0x16FB800` (6 KB) and dual-issue optimizer at `0x170EB20` (7 KB) identify instruction pairs that can be issued simultaneously on the GPU's dual-issue-capable pipelines. Constraints include:

- Instructions must use different functional units.
- No data dependencies between the pair.
- No register bank conflicts (checked by `0x1725C10`, 8 KB).
- Instruction formats must be compatible with the dual-issue encoding.

### Latency Hiding Statistics

The latency hiding analyzer at `0x16F9980` (15 KB) computes and reports scheduling quality metrics:

```
LDS latency hiding: Num=..., Avg=..., Min=...
LDG latency hiding: Num=..., Avg=..., Min=...
Xu64 latency hiding: Num=..., Avg=..., Min=...
Antidep latency hiding: Num=..., Avg=..., Min=...
```

These statistics measure how effectively the scheduler has hidden the latency of shared memory loads (LDS), global memory loads (LDG), 64-bit extended operations (Xu64), and anti-dependency stalls. The anti-dependency resolver at `0x16F9080` (8 KB) specifically handles anti-dependency (WAR) latency hiding by inserting register renaming where possible.

### Per-Block Statistics

The block statistics collector at `0x16FAD00` (10 KB) tracks the following per-basic-block scheduling metrics:

| Field | Description |
|---|---|
| `tSubBb` | Number of scheduling sub-regions within the basic block |
| `HeaderBb` | Whether this block is a loop header |
| `Nvopts` | Number of nvopt-level optimizations applied |
| `LsuResBusy` | LSU (load-store unit) resource busy cycles |
| `Time` | Total estimated execution time |
| `TepidTime` | Time spent in tepid scheduling |
| `MacInsts` | Number of multiply-accumulate instructions |
| `MacReuses` | Number of MAC register reuse opportunities exploited |

## Scoreboard and Dependency Tracking

NVIDIA GPUs use a hardware scoreboard mechanism to track instruction dependencies. Each in-flight instruction is assigned a scoreboard barrier (an integer ID from a limited pool). Subsequent instructions that depend on a pending result must wait on the corresponding barrier. The scheduler must assign barriers efficiently to avoid both correctness violations and unnecessary stalls.

### Hardware Scoreboard Model

The SM target configuration at `0x1A83FB0` sets the maximum scoreboard count:

- **SM70--SM89**: up to 63 scoreboard entries (offset `+616`).
- **SM100+** (Blackwell): up to 255 scoreboard entries (offset `+616`).

Each scoreboard entry tracks one in-flight instruction and its expected completion cycle. The `DEPBAR` instruction in SASS encodes which barriers to wait on, and the control word's stall count encodes how many additional cycles to stall before issuing the next instruction.

### Scoreboard Management Functions

The scoreboard subsystem at `0x1B40000`--`0x1B60000` contains:

| Address | Size | Function |
|---|---|---|
| `0x1B40920` | 38 KB | `scoreboard_dependency_tracker` -- main dependency tracking (1,256 lines) |
| `0x1B41E10` | 23 KB | `wait_barrier_optimizer` -- reduce unnecessary waits |
| `0x1B42E30` | 22 KB | `yield_optimization_pass` -- optimize yield hints |
| `0x1B43E30` | 14 KB | `stall_count_propagation` -- propagate stall counts |
| `0x1B44940` | 13 KB | `control_word_builder` -- build SASS control words |

The dependency tracker at `0x1B40920` (38 KB, 1,256 lines) is the core of scoreboard management. It maintains read/write barrier state, tracks instruction completion status, and updates the scoreboard through simulated instruction execution.

### Barrier Assignment

The barrier assignment pass at `0x1A63610` (14 KB) assigns scoreboard barrier IDs to instructions. The algorithm:

1. Maintain a pool of free barrier IDs.
2. When an instruction with a long-latency result is issued, allocate a barrier ID and record the expected completion cycle.
3. When a dependent instruction is reached, insert a wait on the allocated barrier.
4. When the barrier is no longer needed (all dependents have waited), return the ID to the free pool.

The barrier optimizer at `0x1A64080` (15 KB) post-processes assignments to reduce the total number of active barriers and eliminate waits that are already satisfied by earlier waits.

### SASS Control Word Encoding

SASS instructions are grouped into bundles of three instructions plus one control word. The control word encodes scheduling information for each of the three instructions in the bundle:

```
Control word (64 bits per instruction slot):
  Bits [3:0]   = stall count (0-15 cycles)
  Bit  [4]     = yield hint (1 = suggest warp switch)
  Bits [9:5]   = write barrier index (which scoreboard to signal on completion)
  Bits [14:10] = read barrier mask (which scoreboards to wait on)
  Bits [20:15] = barrier count (dual-issue marker, reserved bits)
```

The control-word builder at `0x1B44940` (13 KB) assembles these fields. The stall-count optimizer at `0x1B1CB00` (11 KB) reduces stall counts by analyzing actual dependency distances -- if the dependent instruction is far enough away in the instruction stream, the stall can be reduced or eliminated.

### Scoreboard Pressure Analysis

The scoreboard pressure analyzer at `0x1A8A5B0` (11 KB) produces diagnostic output under the heading `"SCOREBOARD PRESSURE GUIDANCE"`:

```
SCOREBOARD PRESSURE GUIDANCE (N SBs):
  All Insts: ...
  All Unordered-VQ Insts: ...
  Unordered-VQ INST Stat: ...
  Unordered VQ Stat: ...
```

The companion reporter at `0x1A8ABC0` (11 KB) produces `": SbOverload"` and `", SbStallDiff"` metrics. These diagnostics identify situations where scoreboard pressure is causing performance loss -- when the number of in-flight instructions exceeds the available scoreboard entries, the scheduler must serialize operations.

## Scheduling Guidance Output

The scheduling guidance system at `0x19C1A70` (10 KB) produces the `"SCHEDULING GUIDANCE:"` output section in the compiler's diagnostic output. This includes:

- **Estimated latency** per function and per loop.
- **Bottleneck identification** -- which functional unit is the throughput bottleneck.
- **Resource utilization** estimates for all major functional units.
- **`LOOP STATIC METRICS`** -- per-loop scheduling statistics.

The guidance computation at `0x19C2740` (5 KB) identifies the bottleneck unit, and the resource usage calculator at `0x19C2B50` (5 KB) estimates utilization of each functional unit category: ADU, ALU, CBU, FMA, FMA2x, HALF, transcendental, IPA, LSU, REDUX, TEX, TTU, UDP.

## Interaction with Register Pressure

The scheduling and register allocation passes form a feedback loop. The key interaction points:

1. **Pre-RA scheduler produces an instruction ordering.** The `ScheduleInstructions` pass reorders instructions to maximize ILP. This ordering determines which virtual registers are simultaneously live.

2. **Register allocator allocates physical registers.** If pressure exceeds the available register budget, the allocator spills to local memory (or shared memory on SM50+). The spilling report at `0x18F8D80` records the outcome.

3. **ReduceReg retry.** If the default scheduling produces too many spills, the scheduler can be re-invoked with `ScheduleInstructionsReduceReg` to reduce pressure at the cost of ILP. The strategy selector at `0x1857990` makes this decision.

4. **Tepid scheduler works with final register assignments.** The post-RA tepid scheduler sees the actual physical register numbers and can optimize register reuse flags and bank conflict avoidance.

5. **Scheduling guidance feeds back to the user.** The `SCHEDULING GUIDANCE` and `REGALLOC GUIDANCE` output sections at `0x19C0000`--`0x1A00000` report the combined effect of scheduling and register allocation decisions.

## Key Address Summary

### Pre-RA Scheduling (`0x1850000`--`0x186F000`)

| Address | Size | Identity |
|---|---|---|
| `0x1851DC0` | 85 KB | `ScheduleInstructions_main_driver` |
| `0x1857990` | 13 KB | Strategy selector (default vs. ReduceReg vs. DynBatch) |
| `0x185B870` | 28 KB | Per-block scheduling driver |
| `0x1860A40` | 47 KB | Per-function scheduling driver |
| `0x1864ED0` | 17 KB | List-scheduler core algorithm |
| `0x1867D60` | 22 KB | Block scheduler core with strategy dispatch |
| `0x1866FA0` | 28 KB | `OptimizeNaNOrZero` peephole |
| `0x186C7A0` | 24 KB | `HoistInvariants` core |
| `0x186D520` | 38 KB | `HoistInvariants` per-function |
| `0x186EE80` | 41 KB | `HoistInvariants` analysis driver |
| `0x1868E50` | 19 KB | CUTLASS pattern handler |

### Tepid Scheduler (`0x16F6000`--`0x1740000`)

| Address | Size | Identity |
|---|---|---|
| `0x16F35A0` | 36 KB | Block processor (knob 610 gated) |
| `0x17027F0` | 38 KB | Main scheduling loop (1,216 lines) |
| `0x1704010` | 12 KB | Instruction selector |
| `0x1704CB0` | 12 KB | Priority calculator |
| `0x1710840` | 25 KB | Global optimizer (854 lines) |
| `0x172FED0` | 30 KB | Main driver (1,101 lines) |
| `0x17130F0` | 11 KB | Software pipelining |
| `0x16F9980` | 15 KB | Latency hiding statistics |
| `0x1717A00` | 12 KB | DMA-math ratio balancer |
| `0x1729F10` | 19 KB | Epilogue optimizer |

### Scoreboard Tracking (`0x1B40000`--`0x1B60000`)

| Address | Size | Identity |
|---|---|---|
| `0x1B40920` | 38 KB | Scoreboard dependency tracker |
| `0x1B41E10` | 23 KB | Wait barrier optimizer |
| `0x1B42E30` | 22 KB | Yield optimization pass |
| `0x1B43E30` | 14 KB | Stall count propagation |
| `0x1B44940` | 13 KB | Control word builder |
| `0x1A8A5B0` | 11 KB | Scoreboard pressure analyzer |
| `0x1A63610` | 14 KB | Barrier assignment pass |
| `0x1A64080` | 15 KB | Barrier optimizer |
