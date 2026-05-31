# Instruction Scheduling

> **Note**: This page documents the embedded ptxas copy within nvlink v13.0.88. The standalone ptxas binary has its own comprehensive wiki -- see the [ptxas Reverse Engineering Reference](../../ptxas/index.html) for the full compiler reference. For the standalone ptxas scheduling pipeline, see [ptxas Scheduling overview](../../ptxas/scheduling/overview.html), [algorithm](../../ptxas/scheduling/algorithm.html), [latency model](../../ptxas/scheduling/latency-model.html), and [scoreboards](../../ptxas/scheduling/scoreboards.html).

The embedded ptxas backend in nvlink v13.0.88 contains two complete instruction scheduling subsystems: the **pre-register-allocation scheduler** (three named strategy modes operating on IR-level instructions) and the **tepid scheduler** (a post-register-allocation pipeline simulator that assigns stall counts, yield hints, and scoreboard barriers to the final SASS instruction stream). Both subsystems run per-function and per-basic-block. Together they span approximately 1.2 MB of code across three address ranges: `0x1680000`--`0x16E0000`, `0x16F6000`--`0x1740000`, and `0x1850000`--`0x186F000`, plus scoreboard/dependency tracking at `0x1B40000`--`0x1B60000`.

## Overview of the Two Schedulers

The compilation pipeline invokes scheduling at two distinct points:

1. **Pre-RA scheduling** (`ScheduleInstructions` and variants). Runs before register allocation on the internal IR. Its goal is to maximize instruction-level parallelism (ILP) and hide memory latency while respecting a register pressure budget. The three strategy modes -- `ScheduleInstructions`, `ScheduleInstructionsReduceReg`, and `ScheduleInstructionsDynBatch` -- are selected per-function based on workload characteristics and knob configuration.

2. **Post-RA scheduling** (the "tepid" scheduler). Runs after register allocation on the SASS instruction stream with physical register assignments finalized. It models the GPU hardware pipeline, computes concrete stall counts, assigns scoreboard barriers, determines dual-issue pairing, inserts yield hints, and sets the scheduling control words that appear every 4th instruction in the SASS binary.

The two schedulers communicate indirectly through the register allocation pass: the pre-RA scheduler's instruction ordering influences register pressure, which in turn determines spill/fill counts that the post-RA scheduler must accommodate.

## Confidence Tags for Core Claims

| Claim | Confidence | Evidence |
|---|---|---|
| `sub_1851DC0` is `ScheduleInstructions_main_driver` (85 KB, 2,938 lines) | HIGH | Three mode-name strings `ScheduleInstructions`, `ScheduleInstructionsReduceReg`, `ScheduleInstructionsDynBatch` referenced from this function's callers; companion-pass strings `HoistInvariants`, `OptimizeNaNOrZero`, `ConvertMemoryToRegisterOrUniform` invoked here |
| Three pre-RA modes (Default / ReduceReg / DynBatch) | HIGH | Each mode name is a distinct `.rodata` literal; strategy selector at `0x1857990` chooses between them |
| List-scheduling DAG with RAW/WAR/WAW/memory edges | MEDIUM | Pattern match against standard list-scheduler structure; edge builder at `sub_1850760` has four type tags in its dispatch |
| 184-byte per-BB scheduling record layout | MEDIUM | Field offsets reconstructed from struct accesses; capacity tracking at +840 and overflow hash at +864 visible in growable-array pattern |
| Post-RA "tepid" scheduler models hardware pipeline and emits sched control words every 4th instruction | MEDIUM | Stall/yield/scoreboard accessors at fixed offsets in the SASS instruction record; control-word emission cadence matches Volta+ SASS layout known from open driver headers |
| CUTLASS pattern detector at `0x1866CF0` | LOW | Inferred from pattern-matching shape and call site upstream of `DynBatch` driver; no direct string |

## Pre-RA Scheduling: ScheduleInstructions

### Entry Point and Driver Hierarchy

The main entry point is `sub_1851DC0` (85 KB, 2,938 lines) -- `ScheduleInstructions_main_driver`. This is one of the largest single functions in the scheduling subsystem. It takes a compilation context, an IR module, and a function pointer, and orchestrates the entire pre-RA scheduling pass for one function.

The driver hierarchy is:

```text
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

| Mode | Pass Name | Description | When Selected |
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

```text
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

```text
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

The tepid scheduler is NVIDIA's post-register-allocation instruction scheduler. It operates on the final SASS instruction stream with physical register assignments, modeling the actual hardware pipeline to produce optimal scheduling control words. The name "tepid" appears in internal string references (`"TepidMacUtil"`, `"TepidTime"`) and refers to a "warm" scheduling approach -- not as aggressive as full software pipelining, but more than simple in-order emission.

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

```text
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

```text
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

```text
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

## Reconstructed Pre-RA Scheduling Algorithm

The following pseudocode is reconstructed from the decompiled binary. Addresses are given for cross-reference with the decompiled sources.

### Initialization: DAG Setup and Instruction Classification (`sub_1864ED0`, lines 128--338)

Before the scheduling loop runs, the list-scheduler core performs per-basic-block initialization. Each instruction in the block is assigned a scheduling record, classified, and linked into the dependency DAG.

```c
function list_scheduler_init(sched_ctx):
    // sched_ctx is the 840+ byte scheduling context

    max_reg_pressure_threshold = knob_table[624]
    sched_ctx.has_barrier = false
    sched_ctx.barrier_count = 0
    sched_ctx.pending_list = null
    sched_ctx.completed_flag = false
    sched_ctx.reduce_reg_mode = false

    // Clear the ready-set bitset
    clear_bitset(sched_ctx.ready_set)              // offset +248

    // Max instructions before ready-set overflow (knob gated):
    //   if arch_capability <= 28671: max_in_flight = 64
    //   else:                        max_in_flight = INT_MAX (0x7FFFFFFF)
    // Further overridden by knob at offset +55944
    if knob_table[55944] == 1:
        max_in_flight = knob_table[55952]

    // Phase 1: walk every instruction in the basic block
    inst_index = 0
    has_many_calls = false
    has_many_textures = false
    has_many_shared = false
    call_count = 0

    for each instruction inst in basic_block:
        dag_node = inst.dag_info                   // 40-byte node at inst+40
        dag_node.block_position = inst_index
        inst.schedule_order = -1                    // unscheduled sentinel

        // Allocate 84-byte per-instruction scheduling record (offset +672)
        sched_rec = sched_ctx.records[inst_index]  // 48-byte record array at +280
        clear(sched_rec)                           // zero all 20 DWORDs

        // Build dependency edges: call sub_185D760 (register pressure tracker)
        track_register_operands(sched_ctx, inst, &pressure_lo, &pressure_hi)

        // Classify instruction
        opcode = inst.opcode & 0xFFFFCFFF          // mask out irrelevant bits
        if opcode == 96 and sched_ctx.has_cutlass:
            // CUTLASS-relevant instruction: add to pending list
            node = arena_alloc(16)
            node.inst = inst
            node.next = sched_ctx.pending_list      // offset +264
            sched_ctx.pending_list = node

        // Check if instruction is a call/barrier via vtable dispatch
        is_barrier = vtable[45](sched_ctx.target, inst)
        dag_node.flags.is_barrier = is_barrier

        if is_barrier:
            call_count++
            sched_ctx.has_barrier = true
            has_many_calls |= ((inst.flags >> 2) ^ 1) & 1

        // Walk operands to classify destination registers
        for each operand op in inst.operands:
            tag = (op >> 28) & 7
            if tag != 1:                           // not a register reference
                continue
            if op < 0:                             // definition (negative tag)
                reg = lookup_register(op & 0xFFFFFF)
                if reg.type == 4:                  // call-clobbered register class
                    sched_ctx.barrier_count++
                    dag_node.max_pred_height = max(dag_node.max_pred_height,
                                                    block_position)
                if reg.type == 5: has_many_calls++
                if reg.type == 2: has_many_textures++
                if reg.type == 3: has_many_shared++
            else:                                  // use (positive tag)
                reg = lookup_register(op & 0xFFFFFF)
                if reg.live_range > max_in_flight and reg.type == 6:
                    dag_node.flags.has_long_dep = true

        inst_index++
```

### DAG Construction and Critical-Path Computation (`sub_1864ED0`, lines 340--683)

After initialization, the scheduler computes scheduling heights (critical path from each instruction to the block exit), builds predecessor/successor bitsets, and propagates dependency weights.

```c
function compute_dag_heights(sched_ctx):
    // Phase 2: compute scheduling height for each instruction.
    // Height = (target_max_latency - instruction_latency) accounting for
    //          memory hierarchy distance.
    //
    // The base latency comes from the ISA model (vtable call),
    // adjusted by register-class-specific penalties.

    max_latency_in_block = 0

    for each instruction inst in basic_block (forward):
        dag_node = inst.dag_info
        sched_rec = sched_ctx.records[dag_node.block_position]

        // Union predecessor dependency bitsets into this node
        for each predecessor pred of inst:
            bitset_union(sched_rec.dep_set, pred_sched_rec.dep_set)

        // Compute base instruction latency via ISA model
        isa_info = inst.isa_descriptor                // from inst+32
        base_latency = isa_info[2]                    // latency field
        if base_latency > max_latency_in_block:
            max_latency_in_block = base_latency

        // Add memory-hierarchy adjustment based on instruction class flags:
        //   has_many_textures -> add isa_info[4] (texture penalty)
        //   has_many_shared  -> add isa_info[6] (shared mem penalty)
        //   has_many_calls   -> add isa_info[5] (call penalty)
        adjusted_latency = base_latency
        if sched_ctx.has_many_textures:
            adjusted_latency += isa_info[4]
        if sched_ctx.has_many_shared:
            adjusted_latency += isa_info[6]
        if sched_ctx.has_many_calls:
            adjusted_latency += isa_info[5]
        adjusted_latency = scale_latency(sched_ctx, adjusted_latency)
                                                      // sub_167AA60

        // Scheduling height = max_latency - adjusted_latency
        //   (earlier instructions get higher heights -> higher priority)
        sched_rec.height = max_latency_in_block - adjusted_latency

        // Check predecessors for "schedulable" status
        num_preds = count_predecessors(inst)
        if num_preds == 0:
            // Instruction has no unscheduled predecessors -> initially ready
            sched_rec.flags |= READY
            sched_rec.successor_pressure = 0
        else:
            // Count how many pred operand types force serialization
            single_pred = (count of non-register predecessors <= 1)
            sched_rec.flags.single_pred = single_pred

            // Accumulate successor pressure:
            //   For each predecessor that is itself ready and has
            //   a compatible dependency, add its height + successor weight
            successor_pressure = 0
            for each predecessor pred:
                if pred is READY and dependency_is_compatible(inst, pred):
                    pred_rec = sched_ctx.records[pred.block_position]
                    successor_pressure += pred_rec.height + pred_rec.weight
                    pred_rec.flags |= HAS_READY_SUCCESSOR

            sched_rec.successor_pressure = successor_pressure

        // Net register delta: positive if this instruction is a net producer,
        //   negative if net consumer.
        //   height + successor_pressure < 0 => instruction is a net consumer
        //     -> mark as UNREADY (needs predecessors to complete first)
        //     -> add to blocked set
        if sched_rec.height + successor_pressure < 0:
            add_to_blocked_set(sched_ctx, dag_node)
            // Also compute total_blocked_height for scheduling priority
            sched_rec.total_blocked_height = sum of all successors' heights
        else:
            // Ready: add to ready bitset
            clear_bit(sched_rec.flags, UNREADY)
            set_bit(sched_ctx.ready_set, dag_node.block_position)
            set_bit(sched_rec.dep_set, dag_node.block_position)

        // Backward cross-block dependency edges:
        //   Walk operand definitions backwards; if a def's block_position
        //   is earlier, add a dependency edge from that def to this use.
        for each operand op in inst.operands (backwards):
            if op is register_ref and op.def_block_position < inst.block_position:
                bitset_union(records[op.def_block_position].dep_set,
                             sched_rec.dep_set)

        // Store final priority metrics
        sched_rec.weight = successor_pressure (negated for consumers)
        sched_rec.tiebreak = 0
        sched_rec.cumulative_height = 0

    // Phase 3: propagate heights through the DAG
    //   For instructions with positive height, propagate to successors.
    //   For instructions with negative height (consumers), propagate
    //   the negated height as a "pull" to predecessors.
    for each instruction inst in basic_block:
        sched_rec = sched_ctx.records[inst.block_position]
        if sched_rec.height != 0:
            for each successor succ in sched_rec.dep_set:
                succ_rec = sched_ctx.records[succ]
                if sched_rec.height >= 0:
                    succ_rec.tiebreak += sched_rec.height
                else:
                    succ_rec.weight -= sched_rec.height

    // Phase 4: handle CUTLASS pending instructions
    if sched_ctx.pending_list is not null:
        bitset_subtract(sched_ctx.cutlass_set, sched_ctx.ready_set)
        for each inst in sched_ctx.pending_list:
            bitset_union(sched_ctx.ready_set, sched_rec.dep_set)

    // Phase 5: finalize the scheduling window
    //   Mark the last instruction as the scheduling boundary.
    sched_ctx.schedule_limit = -1
    for inst = sched_ctx.last_inst; inst != null; inst = inst.prev:
        if not is_pseudo_instruction(inst):
            break
        retire_instruction(sched_ctx, inst)         // sub_185DC90
```

### Ready-Queue Selection and Batch Window (`sub_18592C0`)

The ready-queue selector picks the next instruction to issue from the ready set. It uses a batch-window heuristic to avoid excessive scheduling granularity.

```c
function select_next_instruction(sched_ctx, schedule_state):
    // Reset batch window state
    sched_ctx.min_batch_height = INT_MAX            // +488
    sched_ctx.best_pressure = -1                    // +516
    sched_ctx.max_latency_seen = 0                  // +496
    sched_ctx.batch = empty                         // +536 array of pointers

    // Copy the sorted ready-list snapshot into a traversal iterator
    copy_sorted_readyset(sched_ctx.iterator, schedule_state.sorted_set)

    // Compute batch window size:
    //   target_batch = min(max_batch, num_ready_instructions)
    //   If no anti-dep pressure and target < num_ready:
    //     batch_window = num_ready / ceil(num_ready / target)  [balanced]
    //     or num_ready / 2 if slightly over
    target_batch = sched_ctx.max_batch_size          // +404
    num_ready = sched_ctx.num_ready                  // +396
    batch_window = min(target_batch, num_ready)
    if target_batch < num_ready and not sched_ctx.has_anti_dep_pressure:
        if num_ready >= 2 * target_batch:
            batch_window = num_ready / ceil(num_ready / target_batch)
        else:
            batch_window = num_ready / 2

    // Walk the ready set in priority order (sorted by height descending)
    batch_count = 0

    for each candidate in ready_set (priority order):
        latency = scale_latency(sched_ctx, sched_ctx.current_max_pressure)

        // Skip candidates that would violate scoreboard constraints
        if batch_count > 0:
            opcode = candidate.opcode & 0xFFFFCFFF
            if opcode == 96 and sched_ctx.has_cutlass:
                // CUTLASS barrier: stop batching, yield to next block
                sched_ctx.yield_flag = true
                break

            // Check if candidate would cause a scoreboard stall
            if candidate.dag_info.min_height <= sched_ctx.min_batch_height:
                for each scoreboard entry sb in sched_ctx.scoreboard_window:
                    sb_slot = sb.dag_info.slot_id
                    if sb_slot < (candidate.dep_mask & 0x7FFFFFFF)
                       and bit_is_set(candidate.dep_bitset, sb_slot):
                        // Candidate would stall on this scoreboard entry
                        break to COMMIT_CANDIDATE

            // Check if candidate has no outstanding dependencies
            if candidate has no live predecessors:
                goto SKIP_TO_NEXT

        // Record highest latency seen
        if candidate.isa_info.latency > sched_ctx.max_latency_seen:
            sched_ctx.max_latency_seen = candidate.isa_info.latency
        goto COMMIT_CANDIDATE

    SKIP_TO_NEXT:
        // Detect register-pressure excursion and truncate batch
        cumulative_pressure = sched_ctx.current_pressure
                            + sched_ctx.max_latency_seen
                            - sched_ctx.pressure_baseline
        if cumulative_pressure + sched_ctx.best_pressure >= latency:
            if batch_count > 0:
                sched_ctx.pressure_exceeded = true
                // Truncate batch if pressure would blow budget
                break
            sched_ctx.max_latency_seen = 0
            sched_ctx.pressure_baseline = candidate.isa_info.latency

        // Add candidate to batch
        sched_ctx.min_batch_height = candidate.dag_info.min_height
        sched_ctx.batch[batch_count] = candidate
        batch_count++
        sched_ctx.last_batch_position = candidate.block_position

        if batch_count == batch_window:
            break

        // Track max latency among batch members for tiebreaking
        if candidate.dag_info.max_height > sched_ctx.best_pressure:
            sched_ctx.best_pressure = candidate.dag_info.max_height

    COMMIT_CANDIDATE:
        advance iterator to next candidate

    // Post-selection: if batch is smaller than half the ready set and
    //   pressure is not exceeded, trim the batch further to balance
    //   ILP against register pressure.
    if batch_count < num_ready:
        if 2 * target_batch > num_ready and not pressure_exceeded:
            // Adaptive trimming: walk batch backwards, remove
            //   entries that are in the same scheduling group
            //   (same min_height and negative max_height)
            trim_target = (num_ready + 1) / 2
            or (num_ready - sched_ctx.unpressured_count) / 2
            while batch_count > trim_target:
                last = sched_ctx.batch[batch_count - 1]
                if last.dag_info.min_height > trim_target
                   or last.dag_info.max_height >= 0:
                    break
                batch_count--
            batch_window = min(batch_window, trim_target)
```

### Instruction Issue and Dependency Retirement (`sub_185DC90`)

When an instruction is issued, its scheduling record is updated and all successor dependency counts are decremented.

```c
function issue_instruction(sched_ctx, instruction):
    // Look up the instruction's scheduling record
    dag_node = instruction.dag_info
    block_pos = dag_node.block_position
    sched_rec = sched_ctx.records[block_pos]         // 48-byte record

    if sched_rec.height == 0:
        goto REMOVE_FROM_READY

    if sched_rec.height < 0:
        // This instruction was a net register consumer.
        // Propagate its negative height to all successors, reducing
        // their "weight" (pending predecessor contribution).
        for each successor succ_pos in sched_rec.dep_set:
            succ_rec = sched_ctx.records[succ_pos]
            succ_rec.weight += sched_rec.height      // height is negative
    else:
        // Net producer: propagate positive height to successors,
        // reducing their tiebreak/priority accumulator.
        for each successor succ_pos in sched_rec.dep_set:
            succ_rec = sched_ctx.records[succ_pos]
            succ_rec.tiebreak -= sched_rec.height

REMOVE_FROM_READY:
    // Remove instruction from the ready bitset
    clear_bit(sched_ctx.ready_set, block_pos)
```

### Instruction Latency Calculation (`sub_1850760`)

The latency calculator computes the expected completion time for an instruction, considering the target architecture's pipeline model.

```c
function compute_latency(sched_ctx, instruction):
    dag_node = instruction.dag_info
    isa_encoding = dag_node.encoding_bits & 0x1FF    // 9-bit opcode class
    latency_table_ptr = dag_node.latency_ptr         // ISA-model pointer

    base_latency = dag_node.hardware_latency          // field at +184
    if base_latency is valid (not 0x80000000):
        goto APPLY_MODIFIERS

    // Opcode-specific latency lookup
    switch isa_encoding:
        case 215:  // LDGSTS (async global-to-shared copy)
            base_latency = sched_ctx.ldgsts_latency   // offset +26824
            if base_latency == -1: goto FALLBACK

        case 221:  // LDSM (load shared matrix)
            base_latency = sched_ctx.ldsm_latency     // offset +26828
            if base_latency == -1:
                // Compute from matrix dimensions:
                operand_size = extract_matrix_size(instruction)
                is_transposed = extract_transpose_flag(instruction)
                return lookup_matrix_latency(sched_ctx.latency_model,
                    operand_size, is_transposed, isa_encoding) / 4

        case 2:    // IMAD with special predicate
            if is_special_predicate(instruction):
                base_latency = sched_ctx.imad_special_latency  // +10644
                if base_latency != -1: goto APPLY_MODIFIERS

        case 94, 166:  // TEX / SULD (texture / surface load)
            if not is_texture_opcode_442(instruction):
                base_latency = sched_ctx.tex_base_latency  // +10640
                if base_latency != -1: goto APPLY_MODIFIERS
                base_latency = sched_ctx.default_latency   // +228
                goto APPLY_MODIFIERS

        case 191:  // TLD4 (texture gather)
            if is_texture_opcode_442(instruction):
                operand_shift = extract_operand_size(instruction)
                base_latency = lookup_banked_latency(sched_ctx,
                    sched_ctx.default_latency, 4 << (operand_shift & 3))
                goto APPLY_MODIFIERS

        default:   // Memory instructions with known opcode class
            if is_texture_opcode_442(instruction):
                // Compute strided latency for memory ops
                element_size = lookup_element_size(instruction)
                num_elements = extract_element_count(instruction) + 1
                base_latency = lookup_banked_latency(sched_ctx,
                    sched_ctx.default_latency, element_size * num_elements)
                if base_latency != -1: goto APPLY_MODIFIERS

    FALLBACK:
        if latency_table_ptr is null:
            if isa_encoding == 152 or isa_encoding == 142:
                base_latency = 1    // NOP-like instructions
            else:
                base_latency = lookup_from_isa_model(sched_ctx, instruction)
        else:
            base_latency = hash_table_lookup(sched_ctx.latency_hash,
                                              latency_table_ptr)

    APPLY_MODIFIERS:
        // Apply warp-scheduling modifier if enabled
        if sched_ctx.warp_sched_enabled:  // vtable offset +232
            if is_eligible_for_warp_discount(sched_ctx, instruction):
                base_latency -= sched_ctx.warp_discount  // offset +2400

        return base_latency
```

### Per-Block Scheduling Pass (`sub_1681A70`)

The per-block scheduling pass is the workhorse called for each basic block. It drives the list-scheduling loop that picks instructions from the ready set and emits them in scheduled order.

```c
function schedule_basic_block(sched_ctx, strategy_callback, bb_offset, init_flag):
    // Initialize timing infrastructure
    reset_timing(sched_ctx.compilation_ctx)

    if init_flag:
        initialize_liveness(sched_ctx)
        initialize_register_state(sched_ctx)

    sched_ctx.max_latency = 0
    sched_ctx.total_instructions = 0
    sched_ctx.pressure_overflow = false

    // Allocate per-function register tracking arrays
    num_registers = compilation_ctx.register_count + 1
    alloc_tracking_array(sched_ctx, num_registers)

    // Query knobs for scheduling parameters
    batch_size = query_knob(743)                     // max batch size
    anti_dep_weight = query_knob(747)                // anti-dependency weight

    // Iterate over each sub-function (for split compilation)
    for each sub_function in reverse order:
        is_recursive = check_recursion(sub_function)
        sched_ctx.current_function = sub_function

        // Select strategy based on compilation mode
        if sched_ctx.mode == 1:  // ReduceReg
            check_pass_gate("ScheduleInstructionsReduceReg", sub_function)
        elif sched_ctx.mode == 2:  // DynBatch
            check_pass_gate("ScheduleInstructionsDynBatch", sub_function)

        // Resolve scheduling parameters from ISA model
        pressure_info = lookup_pressure(sched_ctx.isa_model, sub_function)

        // Set up register-pressure snapshot
        subtract_pressure_baseline(sched_ctx, pressure_info)
        update_pressure_from_live_in(sched_ctx)

        // Walk each basic block in the sub-function
        cursor = sub_function.last_block
        while cursor is not null:
            // Get next block to schedule via strategy callback
            //   strategy_callback dispatches to one of:
            //     - default list scheduler
            //     - ReduceReg pressure-minimizing scheduler
            //     - DynBatch throughput scheduler
            next_block = strategy_callback(sched_ctx, cursor, bb_offset)
            if next_block is null:
                break

            // Track max instruction height for this block
            if next_block.dag_info.height > sched_ctx.max_latency:
                sched_ctx.max_latency = next_block.dag_info.height

            // Insert scheduled block into output sequence
            insert_scheduled_block(sched_ctx, next_block, cursor)

            // Update register pressure tracking after scheduling
            update_register_pressure(sched_ctx, next_block)

            // Handle call instructions (opcode 97):
            //   On a call boundary, the scheduler crosses to a new
            //   sub-function. Register state is snapshotted and
            //   the pressure tracker walks forward into the callee.
            if next_block.opcode == 97:
                push_call_context(sched_ctx, next_block)
                cursor = next_block
                continue

            // Record per-block scheduling statistics
            block_max_latency = next_block.isa_info.latency
            if block_max_latency > sched_ctx.total_instructions:
                sched_ctx.total_instructions = block_max_latency
            if not is_recursive:
                if block_max_latency > sched_ctx.non_recursive_max:
                    sched_ctx.non_recursive_max = block_max_latency

            cursor = next_block

        // Record final pressure statistics
        compilation_ctx.peak_pressure = sched_ctx.total_instructions
        compilation_ctx.non_recursive_peak = sched_ctx.non_recursive_max

    // Cleanup
    free_tracking_arrays(sched_ctx)
```

### Three-Mode Strategy Dispatch (`sub_1867D60`)

The block scheduler core selects between the three scheduling strategies and wraps the per-block pass.

```c
function block_scheduler_core(sched_ctx):
    compilation_ctx = sched_ctx.compilation_ctx
    isa_model = compilation_ctx.isa_model_ptr          // offset [198]

    // Run pre-scheduling peepholes
    optimize_nan_or_zero(sched_ctx, compilation_ctx)   // sub_1866FA0
    invoke_vtable_232(sched_ctx)                       // pre-scheduling hook

    // Phase 1: Insert barrier-crossing dependency edges
    //   For each instruction in the function, if the ISA model says
    //   it crosses a barrier (vtable[183]), insert a serialization
    //   edge via sub_18B91C0 (add dependency with kind=8).
    if compilation_ctx.flags & 0x10:
        for each instruction inst in function:
            if isa_model.crosses_barrier(inst):
                add_serialization_edge(compilation_ctx, inst)

    // Phase 2: Gate the pass on the "ScheduleInstructions" knob
    if pass_is_disabled("ScheduleInstructions"):
        return

    // Phase 3: Analyze function structure
    max_block_height = compute_max_block_height(compilation_ctx)
    initialize_scheduling_infrastructure(sched_ctx, max_block_height)

    // Phase 4: Configure for ReduceReg mode
    if compilation_ctx.flags & 0x10:     // extended register file
        register_budget = 2 * (compilation_ctx.register_count + 1)
    else:
        register_budget = compilation_ctx.register_count + 1
    allocate_pressure_tracking(sched_ctx, register_budget)

    // Phase 5: Determine scheduling mode
    //   Query knobs 776 (ReduceReg ILP budget), 778 (ReduceReg latency
    //   budget), scale them through the latency model, then run
    //   "ScheduleInstructionsReduceReg" pass if not gated.
    ilp_budget = query_knob(776)
    latency_budget = query_knob(778)
    ilp_budget = scale_latency(sched_ctx, ilp_budget)
    latency_budget = scale_latency(sched_ctx, latency_budget)

    if pass_not_gated("ScheduleInstructionsReduceReg"):
        // Run ReduceReg mode: sched_ctx.mode = 1
        set_mode(sched_ctx, REDUCE_REG)
        sched_ctx.max_batch = 0x39                     // 57 instructions
        run_scheduling_pass(sched_ctx, mode=0x39)      // sub_1681A70
        // Reset per-instruction state for next pass
        clear_instruction_state(compilation_ctx)

    // Phase 6: Configure default scheduling mode
    set_mode(sched_ctx, DEFAULT)
    sched_ctx.max_batch = query_knob(805)              // batch size
    if sched_ctx.max_batch > 16: sched_ctx.max_batch = 16
    sched_ctx.anti_dep_limit = query_knob(741)         // default: 3
    sched_ctx.pressure_limit = query_knob(761)         // default: 3 or 6 or 12

    // Phase 7: Check CUTLASS workload pattern
    sched_ctx.is_cutlass = check_cutlass_workload(sched_ctx)  // sub_1866CF0

    // Phase 8: Run DynBatch mode if applicable
    if pass_not_gated("ScheduleInstructionsDynBatch"):
        // Check knob 742 or auto-detect based on instruction mix
        if query_knob(742) or auto_detect_dynbatch(compilation_ctx):
            set_mode(sched_ctx, DYNBATCH)
            run_scheduling_pass(sched_ctx, mode=0x41)  // 65 instructions

    // Phase 9: Final default scheduling pass
    set_mode(sched_ctx, DEFAULT)
    run_scheduling_pass(sched_ctx, mode=0x49)          // 73 instructions

    // Cleanup
    free_pressure_tracking(sched_ctx)
```

### Register Pressure Delta Computation (`sub_185D760`)

The register pressure tracker scans each instruction's operands and records which physical register ranges are defined or consumed. This drives the pressure-aware scheduling decisions.

```c
function track_register_operands(sched_ctx, instruction, out_lo, out_hi):
    // Walk operands in reverse order (last operand to first)
    num_ops = instruction.operand_count - 1
    if num_ops < 0:
        return

    arena = sched_ctx.arena                           // offset +840

    for op_index = num_ops downto 0:
        operand = instruction.operands[op_index]
        tag = (operand >> 28) & 7

        if tag != 1:                                  // not a register
            continue
        if (operand & 0xFFFFFF) - 41 <= 3:            // pseudo-register
            continue
        if operand < 0:                               // definition operand
            continue

        // Look up the register descriptor
        reg = register_table[operand & 0xFFFFFF]

        // Allocate a 16-byte tracking node
        node = arena_alloc(arena, 16)
        node.instruction = instruction

        // Link into the register's use chain
        prev_head = reg.use_chain_head                 // offset +104
        if prev_head is null:
            // First use of this register in this block
            add_to_active_list(sched_ctx, reg)         // link at +680
            reg.use_chain_next = null                   // +112
        node.next = prev_head
        reg.use_chain_head = node

        // Check if register is "wide" (exceeds scheduling horizon)
        if sched_ctx.pressure_limit < reg.live_range
           and reg.type == 6
           and reg is not pseudo:
            // Wide register: track in the register-file bitmap
            //   The bitmap at offset +728 records which register
            //   groups are actively tracked for pressure.
            phys_reg = reg.physical_number
            stride = phys_reg * (has_extended_regfile ? 2 : 1)
            bitmap_word = stride >> 6
            bitmap_bit = 1 << stride

            // Grow bitmap if necessary
            ensure_bitmap_capacity(sched_ctx, bitmap_word + 1)
            sched_ctx.register_bitmap[bitmap_word] |= bitmap_bit

            // If extended register file (paired mode),
            //   also set the paired register bit
            if has_extended_regfile:
                paired_stride = stride + 1
                sched_ctx.register_bitmap[paired_stride >> 6] |= (1 << paired_stride)

    // Update output pressure counters
    *out_lo += definitions_in_lo_class
    *out_hi += definitions_in_hi_class
```

### Register Pressure Heuristic in the Priority Function (`sub_1859F10`)

In ReduceReg mode, the priority function penalizes instructions that would increase register pressure beyond a configurable threshold.

```c
function pressure_aware_priority(sched_ctx, instruction, reg_info):
    // Query knob 780 for pressure tracking granularity
    // (auto-incremented on each call)
    granularity = query_knob_780_autoincrement()

    opcode = instruction.opcode & 0xFFFFCFFF
    if opcode != 288 and opcode != 183:   // not a memory or barrier op
        return 0                          // no pressure adjustment

    // Look up the register class and bank that this instruction writes to
    reg_class = reg_info[3]               // register class index
    num_elements = reg_info[2]            // number of registers written

    // Check current pressure in this class
    current_bank_pressure = sched_ctx.class_pressure[reg_class]
    if current_bank_pressure != 0:
        // Compute maximum pressure across all active banks
        max_bank_pressure = 0
        if num_elements == 1:
            // Single-element: check both base and paired register
            for each active bank in (current_bank_pressure bitmask):
                p = max(sched_ctx.base_pressure[bank],
                        sched_ctx.paired_pressure[bank])
                max_bank_pressure = max(max_bank_pressure, p)
        else:
            // Multi-element: check only base pressure
            for each active bank in (current_bank_pressure bitmask):
                max_bank_pressure = max(max_bank_pressure,
                                         sched_ctx.base_pressure[bank])

        // If max pressure exceeds threshold, penalize this instruction
        if max_bank_pressure >= sched_ctx.pressure_threshold:  // offset +1036
            // For definitions that write the first bank, set class=0
            //   to force it to schedule first (relieve pressure)
            if (current_bank_pressure & 1) != 0:
                reg_info[3] = 0
            return                        // signal: instruction is pressure-hot
    else:
        // No active pressure in this class
        // Look up paired class pressure
        paired_pressure = sched_ctx.class_pressure[reg_class + 33]
        if num_elements == 1:
            paired_pressure = max(paired_pressure,
                                   sched_ctx.paired_pressure[reg_class])
        if paired_pressure >= sched_ctx.pressure_threshold:
            return                        // still pressure-hot

    // Extract destination operand information for latency-based
    //   priority: wider destinations get higher priority to free
    //   registers sooner.
    dest_operand = instruction.operands[dest_index]
    dest_reg = register_table[dest_operand & 0xFFFFFF]
    num_dest_regs = compute_dest_width(instruction)
    latency_penalty = compute_latency(sched_ctx, instruction) * num_dest_regs

    return latency_penalty               // higher = schedule sooner
```

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

## Cross-References

### nvlink Internal
- [Embedded ptxas Overview](overview.md) -- scheduling in the 48-pass pipeline (passes 23--38)
- [Register Allocation](register-allocation.md) -- runs before the tepid scheduler
- [ISel Hubs](isel-hubs.md) -- runs before scheduling
- [Peephole](peephole.md) -- peephole passes at `0x1866FA0` interleaved with scheduling
- [Mercury Compiler Passes](../mercury/compiler-passes.md) -- Mercury-specific scheduling passes (MercWARs, MercOpex)

### Sibling Wikis
- [ptxas: Scheduling Overview](../../ptxas/scheduling/overview.html) -- standalone ptxas scheduling infrastructure
- [ptxas: Algorithm](../../ptxas/scheduling/algorithm.html) -- scheduling algorithm details
- [ptxas: Latency Model](../../ptxas/scheduling/latency-model.html) -- per-instruction latency tables
- [ptxas: Scoreboards](../../ptxas/scheduling/scoreboards.html) -- dependency barrier assignment
