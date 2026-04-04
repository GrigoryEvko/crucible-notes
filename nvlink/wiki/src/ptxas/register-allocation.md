# Register Allocation

The register allocation subsystem in nvlink's embedded ptxas backend occupies approximately 400 KB of code across two primary address ranges: `0x189C000`--`0x18FC000` (core regalloc, ~120 functions) and `0x18FC000`--`0x191A000` (setmaxnreg/CTA-reconfig, ~55 functions). An additional ~120 verification functions at `0x196C000`--`0x1A00000` validate allocation correctness post-hoc. Together these form the largest single pass in the compiler backend -- the top-level driver alone (`AllocateRegisters_main_driver` at `0x18988D0`) is 71 KB, and the per-instruction encoding function at `0x18AE2D0` is 155 KB, the largest function in the entire 1.7 MB backend core region.

Register allocation follows a graph-coloring model with iterative spilling, operating across three distinct register classes simultaneously: general-purpose R-registers (R0--R255), uniform UR-registers (UR0--UR63, SM75+), and predicate registers (P0--P6). The allocator supports two alternative spill targets -- local memory (lmem, the traditional spill slot) and shared memory (smem, an NVIDIA-proprietary optimization controlled by a ROT13-obfuscated knob). For Blackwell and later architectures (SM100+), the system integrates with the `setmaxnreg` CTA register reconfiguration infrastructure that enables dynamic register budget adjustment within a kernel.

## Key Facts

| Property | Value |
|---|---|
| Main driver | `sub_18988D0` (`AllocateRegisters_main_driver`) at `0x18988D0` (70,715 bytes / 2,408 lines) |
| Per-instruction encoder | `sub_18AE2D0` at `0x18AE2D0` (155,321 bytes / 4,005 lines) -- largest function in region |
| Full pipeline | `sub_18E54B0` (`AllocateRegisters_full_pipeline`) at `0x18E54B0` (75,131 bytes / 2,738 lines) |
| Graph coloring core | `sub_189C3E0` at `0x189C3E0` (47,807 bytes / 1,734 lines, self-recursive) |
| Spill cost computation | `sub_189F300` at `0x189F300` (37,728 bytes / 1,680 lines, self-recursive) |
| No-spill pass | `sub_18B3AE0` at `0x18B3AE0` (45,720 bytes / 1,525 lines) |
| SMEM spill driver | `sub_18C8790` at `0x18C8790` (20,336 bytes / 764 lines) |
| Budget negotiation | `sub_18E3530` at `0x18E3530` (32,949 bytes / 1,250 lines, self-recursive) |
| setmaxnreg top-level | `sub_1912A00` at `0x1912A00` (33,087 bytes / 1,091 lines) |
| Post-alloc verification | `sub_19D6730` at `0x19D6730` (76,335 bytes / 2,728 lines) |
| Resource usage reporter | `sub_140A6B0` at `0x140A6B0` (5,462 bytes / 183 lines) |
| Register classes | R-regs (general), UR-regs (uniform), predicates (P) |
| SMEM spill knob | `ranoyr_fzrz_fcvyyvat` (ROT13 of `enable_smem_spilling`) |
| Total regalloc functions | ~120 core + ~55 setmaxnreg + ~120 verification = ~295 |

## Pipeline Overview

The register allocation pipeline proceeds through eight stages. Each stage may iterate multiple times if spilling is required and the initial allocation fails.

```
  AllocateRegisters_main_driver (0x18988D0)
    |
    |  1. Classify register classes for each virtual register
    |     regalloc_classify_register_class (0x189B2D0)
    |
    |  2. Compute live ranges per basic block
    |     regalloc_compute_live_ranges (0x18A0DB0)
    |
    |  3. Build interference graph
    |     regalloc_build_interference_graph (0x189E600)
    |
    |  4. Graph coloring (self-recursive)
    |     regalloc_graph_coloring_core (0x189C3E0)
    |       -> spill cost computation (0x189F300, also self-recursive)
    |       -> coalescing (0x189BE00)
    |       -> splitting (0x18A6860)
    |
    |  5. Iterative spill / re-allocate loop
    |     AllocateRegisters_iterative_spill (0x18DEA00)
    |       -> spill pass full (0x18DB260)
    |       -> SMEM spill driver (0x18C8790) [if eligible]
    |       -> spill-retry with budgets (0x18F51E0)
    |
    |  6. Per-instruction encoding with physical registers
    |     AllocateRegisters_per_instruction_encode (0x18AE2D0)
    |     AllocateRegisters_encode_all_instructions (0x18EF990)
    |
    |  7. Budget negotiation + setmaxnreg (Blackwell+)
    |     AllocateRegisters_negotiate_budget (0x18E3530)
    |     setmaxnreg_top_level_driver (0x1912A00)
    |
    |  8. Final reporting + verification
    |     AllocateRegisters_final_reporting (0x18F9A60)
    |     AllocateRegisters_full_verification (0x19D6730)
    v
  Allocated physical registers encoded into all instructions
```

## Graph Coloring Core

The graph coloring allocator at `sub_189C3E0` (48 KB, 1,734 lines) implements a Chaitin-Briggs-style interference-graph coloring algorithm. It is self-recursive -- the function calls itself during splitting and re-coloring attempts. The call chain:

1. **Live range computation** (`sub_18A0DB0`, 13.5 KB) -- iterates basic blocks building per-register live intervals using an iterative dataflow analysis. Each virtual register receives a live range spanning from its definition point to its last use.

2. **Interference graph construction** (`sub_189E600`, 11.5 KB) -- for each pair of simultaneously-live virtual registers, adds an interference edge. The interference graph is represented as a bitmatrix or adjacency list (not directly visible from decompilation, but the 4 vtable calls suggest an abstract graph interface).

3. **Coloring** -- the recursive core assigns physical registers (colors) to virtual registers while respecting interference edges. When coloring fails, it selects a spill candidate based on the spill cost computation.

4. **Spill cost computation** (`sub_189F300`, 38 KB, also self-recursive) -- computes the cost of spilling each virtual register. Factors include loop nesting depth, use frequency, rematerialization eligibility, and live range length. The recursive nature handles hierarchical loop structures -- inner loops contribute multiplicatively to spill cost.

5. **Coalescing** (`sub_189BE00`, 7.5 KB) -- attempts to merge virtual registers connected by copy instructions into a single physical register, eliminating the copy. Pre-coloring coalescing (aggressive) and post-coloring coalescing (conservative) paths are both present.

6. **Live range splitting** (`sub_18A6860`, 7.7 KB) -- splits long live ranges into smaller segments that may be independently colorable, reducing interference.

## Spilling and No-Spill Regalloc

The allocator supports two distinct modes: a "no-spill" attempt that tries to fit everything into the register budget without any spill code, and a full spilling mode that iteratively inserts spill/refill instructions until allocation succeeds.

### No-Spill Pass

`AllocateRegisters_nospill_pass` at `sub_18B3AE0` (46 KB, 1,525 lines) attempts allocation without introducing any spill code. String references include `"Smem spilling..."` and `"Register allocation failed..."`, indicating that this pass reports its success or failure. The no-spill result is reported via `regalloc_nospill_report` at `sub_18F9330` (9.3 KB), which emits `"NOSPILL REGALLOC: attemp"` (sic -- the truncated string is verbatim from the binary).

The no-spill pass is attempted first as an optimistic strategy. If it succeeds, the function uses the minimum possible register count with zero spill overhead. If it fails, the full spilling pipeline takes over.

### Full Spilling Pipeline

The full spilling pipeline involves several interconnected functions:

| Function | Address | Size | Role |
|---|---|---|---|
| `AllocateRegisters_spill_pass_full` | `0x18DB260` | 55 KB | Full allocation with spill insertion |
| `AllocateRegisters_iterative_spill` | `0x18DEA00` | 50 KB | Iterative spill/re-allocate loop (self-recursive) |
| `AllocateRegisters_spill_iteration_loop` | `0x18EB9D0` | 48 KB | Inner spill iteration, 44 callees |
| `AllocateRegisters_full_with_spill_retry` | `0x18F51E0` | 84 KB | Full allocation with retry on failure |
| `regalloc_spilling_pass_driver` | `0x18C7480` | 25 KB | Per-function spill pass coordination |
| `regalloc_spilling_pass_per_block` | `0x18C69D0` | 17 KB | Per-basic-block spill insertion |
| `regalloc_spilling_regalloc_driver` | `0x18DD9D0` | 26 KB | Reports `"-CLASS SPILLING REGALLOC ("` |

The spilling flow:

1. **Candidate selection** -- `regalloc_compute_spill_weights_per_block` (`0x18C5470`) and `regalloc_compute_spill_benefit` (`0x18C5B30`) evaluate each virtual register as a spill candidate. Registers with long live ranges, low use frequency, and placement outside inner loops are preferred.

2. **Spill code generation** -- `regalloc_insert_spill_code` (`0x18AD450`) inserts STL (store-local) instructions at each definition of the spilled register, and `regalloc_emit_spill_load` (`0x18BC670`) inserts LDL (load-local) instructions before each use. Spill stores and loads target local memory (stack frame).

3. **Spill slot assignment** -- `regalloc_compute_spill_slot` (`0x18A6E40`) assigns frame offsets for spilled registers. `regalloc_compute_spill_offset` (`0x18ADE70`) computes the final byte offset within the stack frame.

4. **Iteration** -- if the allocation still fails after spilling (the register budget is exceeded even with spill code), the loop tries again with additional candidates. The iterative nature is reflected in the self-recursive call in `AllocateRegisters_iterative_spill`.

5. **Spill optimization** -- `regalloc_optimize_spill_code` (`0x18CCB10`, 9 KB) and `regalloc_coalesce_spill_stores` (`0x18CD130`) clean up redundant spill/refill sequences after the iterative loop converges.

On failure, the allocator emits: `"Register allocation failed with register count of '%d'..."`.

### Rematerialization

`regalloc_handle_rematerialization` at `0x18BE000` (4.9 KB) identifies instructions that can be cheaply recomputed rather than spilled. Constant loads, address computations, and other low-cost operations are marked as rematerializable -- the allocator regenerates them at each use site instead of inserting a spill/refill pair. This is verified post-hoc by the rematerialization verification pass (see [Post-Allocation Verification](#post-allocation-verification)).

## SMEM Spilling

NVIDIA's shared-memory spilling is a proprietary optimization that uses on-chip shared memory (smem) as a spill target instead of local memory (which is backed by the L1/L2 cache hierarchy and ultimately DRAM). Shared memory has deterministic latency (typically 20-30 cycles vs 200+ cycles for an L2 miss), making it significantly faster for spill/refill patterns with high access frequency.

### The ROT13 Knob

The feature is controlled by the knob `enable_smem_spilling`, which is stored in the binary as the ROT13-encoded string `ranoyr_fzrz_fcvyyvat`. This obfuscation is consistent with NVIDIA's practice of encoding internal knob names via ROT13 to discourage end-user tampering (see the ROT13 decoder at `sub_1A40AC0`). The knob is read via the standard knob-value reader functions `sub_166B370`/`sub_166B340`.

### SMEM Spill Infrastructure

The SMEM spilling subsystem spans approximately 15 functions:

| Function | Address | Size | Role |
|---|---|---|---|
| `regalloc_smem_spilling_driver` | `0x18C8790` | 20 KB | Top-level SMEM spill driver |
| `regalloc_smem_spill_eligibility` | `0x18D1FF0` | 10 KB | Checks ABI eligibility |
| `regalloc_smem_spill_driver_per_function` | `0x18D2AA0` | 15 KB | Per-function SMEM spill |
| `regalloc_smem_spill_transform` | `0x18D3C10` | 9 KB | Transforms spill code for SMEM |
| `regalloc_smem_compute_offsets` | `0x18C9C80` | 5 KB | Computes SMEM offsets |
| `regalloc_smem_compute_slot_size` | `0x18D3690` | 4 KB | Slot size calculation |
| `regalloc_smem_allocate_slot` | `0x18D9CD0` | 4 KB | Slot allocation |
| `regalloc_smem_free_slot` | `0x18DA380` | 3.5 KB | Slot deallocation |
| `regalloc_smem_emit_load` | `0x18DABC0` | 4 KB | Emits SMEM load (refill) |
| `regalloc_perform_smem_spill` | `0x18CA9F0` | 13 KB | Performs individual spill |
| `regalloc_smem_insert_barriers` | `0x18D5450` | 8 KB | Inserts memory barriers |
| `regalloc_smem_fixup_addressing` | `0x18D79A0` | 6 KB | Fixes up SMEM addresses |
| `regalloc_smem_handle_aliasing` | `0x18D9220` | 8 KB | Alias analysis for SMEM |
| `regalloc_smem_compute_base_address` | `0x18D8F50` | 4 KB | Base address computation |
| `regalloc_smem_validate` | `0x18D7FD0` | 4 KB | Validates SMEM spill |

### ABI Restriction

The eligibility checker at `0x18D1FF0` enforces a critical constraint: **SMEM spilling is not permitted when the function uses ABI calls** (device function calls with standard calling conventions). The diagnostic string is explicit:

```
"Smem spilling should not be enabled when functions use abi."
```

This restriction exists because SMEM spilling reserves a portion of shared memory for spill slots. If a called function also uses shared memory (either for its own spill slots or for user-declared `__shared__` variables), the spill region could overlap with the callee's SMEM usage. Since the ABI does not standardize shared memory layout across call boundaries, SMEM spilling is disabled for any function that makes non-inlined device calls.

The reserved SMEM region is referenced by the symbol `__nv_reservedSMEM_offset_0_alias`, which appears in several resource-reporting and codegen functions.

### SMEM Spill Flow

When SMEM spilling is eligible:

1. `regalloc_smem_compute_base_address` -- computes the base address within shared memory for spill slots, after all user-declared `__shared__` variables
2. `regalloc_smem_compute_slot_size` -- determines the size of each spill slot (typically 4 bytes per 32-bit register)
3. `regalloc_smem_allocate_slot` -- assigns SMEM offsets to spilled registers
4. `regalloc_smem_spill_transform` -- rewrites STL/LDL spill instructions to STS/LDS (shared-memory store/load)
5. `regalloc_smem_insert_barriers` -- inserts memory barriers (LDGDEPBAR or similar) to ensure SMEM writes are visible before subsequent reads
6. `regalloc_smem_fixup_addressing` -- adjusts addressing modes for SMEM spill slots

## ABI Call Register Pressure Analysis

When a function contains device calls (ABI calls), the register allocator must account for callee-clobbered registers and the caller-save/callee-save contract. The ABI pressure analysis subsystem reports per-register-class pressure at each call site.

### Key Functions

| Function | Address | Size | Role |
|---|---|---|---|
| `regalloc_ABI_call_pressure_report` | `0x18D5EC0` | 15 KB | Reports `"-CLASS ABI CALL PRESSURE for func"` |
| `AllocateRegisters_pressure_analysis` | `0x18CE9F0` | 29 KB | Per-function pressure computation |
| `regalloc_report_ABI_pressure_per_class` | `0x18E0C70` | 12 KB | Per-class pressure breakdown |
| `regalloc_compute_ABI_pressure` | `0x18E15D0` | 8 KB | Computes actual pressure values |
| `regalloc_handle_function_call_pressure` | `0x18E5080` | 7 KB | Pressure at call sites |
| `regalloc_adjust_for_ABI_call` | `0x18E4E00` | 4 KB | Adjusts budget for ABI calls |
| `regalloc_check_ABI_constraints` | `0x18A9700` | 4 KB | Validates ABI register constraints |

### Pressure Reporting Format

The ABI pressure report uses the format string:

```
"-CLASS ABI CALL PRESSURE for func" ... " at line " ... " regs\n"
```

This is emitted per register class (R-class, UR-class, predicate-class) for each function that contains ABI calls. The report identifies the call site with the highest register pressure, which is the bottleneck for register budget allocation.

The caller-save convention on NVIDIA GPUs requires the caller to save all live registers across a call. The more live registers at a call site, the more spill code is needed, so the ABI pressure metric directly predicts spill cost at call boundaries.

## CTA Reconfiguration (setmaxnreg) for Blackwell+

Starting with Blackwell (SM100), NVIDIA introduced the `setmaxnreg` instruction that allows a kernel to dynamically adjust its register budget at runtime. This enables a programming pattern where different phases of a kernel use different register counts, trading register capacity for thread occupancy within a single kernel launch.

### Concept

In traditional CUDA compilation, each kernel has a fixed register count determined at compile time. The hardware allocates `ceil(regs/granularity) * granularity` registers per thread, which determines the maximum number of concurrent CTAs (thread blocks) per SM. A kernel with 128 registers per thread can run fewer CTAs than one with 32 registers.

`setmaxnreg` allows a kernel to:
- **Allocate** registers: `setmaxnreg.inc` or `setmaxnreg.alloc` increases the register budget, blocking until the hardware can provide the requested registers
- **Deallocate** registers: `setmaxnreg.dec` or `setmaxnreg.dealloc`/`setmaxnreg.release` releases registers back to the hardware, potentially allowing more CTAs to launch

### setmaxnreg Infrastructure

The setmaxnreg subsystem spans `0x18FB000`--`0x191A000` (~55 functions):

| Function | Address | Size | Role |
|---|---|---|---|
| `setmaxnreg_top_level_driver` | `0x1912A00` | 33 KB | Top-level driver |
| `CTA_reconfig_master_driver` | `0x190E080` | 24 KB | CTA reconfig orchestration |
| `setmaxnreg_enforcement_driver` | `0x18FC090` | 22 KB | Enforces register constraints |
| `setmaxnreg_full_transform` | `0x19149A0` | 19 KB | Full transformation pass |
| `CTA_reconfig_insert_setmaxnreg_instructions` | `0x190FDA0` | 18 KB | Instruction insertion |
| `CTA_reconfig_full_analysis` | `0x190C990` | 17 KB | Full analysis pass |
| `setmaxnreg_driver_per_module` | `0x1904F00` | 16 KB | Per-module driver |
| `CTA_reconfig_insert_alloc_dealloc` | `0x19082A0` | 14 KB | Insert alloc/dealloc pairs |
| `CTA_reconfig_validate_pragmas` | `0x1906DE0` | 7 KB | Pragma validation |
| `setmaxnreg_emit_all_warnings` | `0x1906500` | 14 KB | Warning emission |

### Pragma Validation

The CTA reconfig system validates pragma annotations placed in PTX source code. `CTA_reconfig_validate_pragmas` (`0x1906DE0`) checks for:

- `"Conflicting CTA Reconfig pragmas..."` -- multiple incompatible pragmas on the same function
- `"Found an 'alloc' pragma after 'dealloc'"` -- incorrect ordering
- `"Found a 'dealloc' pragma after 'alloc'"` -- incorrect ordering

Pragmas must follow a strict alloc-before-dealloc ordering within a function's control flow.

### Register Budget Computation

`setmaxnreg_compute_register_budget_A` and `_B` (`0x18FB430`, `0x18FBA60`, each 9 KB) compute the allowed register range for each code region. The `_B` variant emits the diagnostic:

```
"setmaxnreg ignored; unable to determine register count at entry"
```

when the entry register count cannot be statically determined (e.g., due to indirect calls or complex control flow).

### Instruction Generation

Several functions emit the actual SASS `SETMAXNREG` instructions:

- `setmaxnreg_emit_alloc_instruction` (`0x18FF510`) -- emits `setmaxnreg.alloc`/`.inc`
- `setmaxnreg_emit_dealloc_instruction` (`0x18FFA80`) -- emits `setmaxnreg.dealloc`/`.release`/`.dec`
- `setmaxnreg_emit_reconfig_code` (`0x1902670`) -- emits surrounding reconfig glue

The register count in `setmaxnreg.dec` instructions is validated:

```
"setmaxnreg.dec has register count..."
"setmaxreg.dealloc/release has register count..."
```

### Minimum Register Requirements

`setmaxnreg_check_minimum_requirements` (`0x18FE630`) enforces a floor on the register count:

```
"setmaxnreg ignored to maintain minimum register requirements"
```

Certain register counts are too low for the function to execute correctly (e.g., if the function needs at least N registers for its innermost loop). The compiler silently ignores the `setmaxnreg` pragma rather than generating incorrect code.

### Debugging Interaction

`setmaxnreg_emit_all_warnings` (`0x1906500`) includes:

```
"setmaxnreg ignored to allow debugging"
```

When device-debugging is enabled (`-G` flag), `setmaxnreg` is disabled because debug information requires consistent register layouts across the entire function.

### Compatibility with Extern Calls

`setmaxnreg_handle_extern_calls` (`0x1902BD0`, 10 KB) handles the interaction between dynamic register budgets and external function calls. When a function calls an external symbol whose register requirements are unknown at compile time, the compiler must ensure the register budget at the call site is sufficient for the callee's worst-case requirements.

## Multi-Class Register Allocation

The NVIDIA GPU register file is divided into three distinct classes, each allocated independently but subject to a shared per-thread register budget:

### R-Registers (General-Purpose)

- **Range**: R0--R255 (SM75+: up to 255 general registers per thread)
- **Width**: 32 bits each; 64-bit values use even-odd pairs (e.g., R4:R5)
- **Usage**: ALU operands, memory addresses, intermediate values
- **Allocation**: The primary target of graph coloring; most of the 120 regalloc functions handle R-regs
- **Special registers**: R255 = RZ (zero register, hardcoded)
- **Pair constraints**: `regalloc_handle_register_pairs` (`0x18C3300`) and `register_pair_allocator` (`0x1ABBCC0`) enforce even-register alignment for 64-bit pairs
- **Bank conflicts**: `register_bank_conflict_resolver` (`0x1ABA8E0`) resolves bank conflicts -- the register file is organized into banks (configurable, typically 4 or 8), and simultaneous reads from the same bank cause stalls

### UR-Registers (Uniform)

- **Range**: UR0--UR63 (SM75+ Turing and later)
- **Width**: 32 bits each
- **Usage**: Values that are uniform across all threads in a warp (e.g., loop bounds, base addresses). Reading a UR-reg does not consume an R-reg read port
- **Allocation**: `uniform_register_allocator` (`0x1AB93C0`, 13 KB) handles UR-reg allocation separately from R-regs
- **Initialization**: `regalloc_init_uniform_state` (`0x18B63E0`) and `regalloc_handle_uniform_registers` (`0x18B5A90`)
- **Verification**: `verify_uniform_register_usage` (`0x19CC6D0`) checks that uniform registers were not used when disallowed, emitting `"Uniform registers were disallowed, but the compiler required..."`

### Predicate Registers (P)

- **Range**: P0--P6 (7 predicate registers per thread)
- **Width**: 1 bit each (boolean)
- **Usage**: Conditional execution (predicated instructions), branch conditions
- **Allocation**: `regalloc_handle_predicate_registers` (`0x18B67B0`, 6 KB)
- **Special**: P7 = PT (always-true predicate, hardcoded); encoding value 31 = PT
- **Spilling**: Predicate spilling uses the P2R (predicate-to-register) and R2P (register-to-predicate) instructions, packing multiple predicates into a single R-register. The verification pass at `0x19DE150` checks `"Failed to establish match for P2R-R2P pattern..."`

### Multi-Class Driver

`regalloc_multi_class_driver` at `0x18D69F0` (15 KB, 10 vtable calls, 16 callees) orchestrates allocation across all three classes. The driver iterates: allocate R-regs, allocate UR-regs, allocate predicates, check combined budget, and retry if the combined allocation exceeds the target register count.

## Register Budget Negotiation

The budget negotiation system determines the final register count per function, balancing multiple competing constraints:

1. **`--maxrregcount`** -- user-specified maximum register count (CLI option)
2. **`__launch_bounds__`** -- PTX-level annotation specifying (maxThreadsPerBlock, minBlocksPerMultiprocessor)
3. **`setmaxnreg` pragmas** -- Blackwell+ dynamic register budget
4. **ABI requirements** -- minimum registers needed for calling convention
5. **Occupancy targets** -- higher register counts reduce occupancy
6. **Spill cost** -- lower register counts increase spill overhead

`AllocateRegisters_negotiate_budget` at `0x18E3530` (33 KB, self-recursive) runs the negotiation loop:

1. Start with the target register count (from `--maxrregcount` or occupancy heuristic)
2. Attempt allocation with that budget via `AllocateRegisters_with_target_count` (`0x18E1BF0`, 46 KB)
3. If allocation fails, increase the budget and retry
4. If allocation succeeds but exceeds launch-bounds constraints, reduce the budget and retry with more aggressive spilling
5. Iterate until a feasible allocation is found or the maximum iteration count is reached

The function `regalloc_compute_register_budget` at `0x18C28E0` (6 KB) computes the initial budget. `regalloc_check_budget_feasibility` (`0x18D4F90`) validates that a proposed budget is achievable. `regalloc_attempt_with_budget` (`0x18D4350`) makes a single allocation attempt with a fixed budget.

### Launch Bounds Compatibility

Launch bounds set a hard floor on occupancy, which translates to a hard ceiling on register count. If `__launch_bounds__(256, 2)` specifies at least 2 blocks per SM with 256 threads each, and the SM has 65,536 registers, the maximum per-thread register count is `65536 / (256 * 2) = 128`. The budget negotiation system respects this ceiling, preferring to spill rather than violate the launch bounds.

## Per-Instruction Encoding

After allocation, every instruction in the function must be rewritten to replace virtual registers with physical register numbers. Two massive functions handle this:

- **`AllocateRegisters_per_instruction_encode`** (`0x18AE2D0`, 155 KB, 4,005 lines, 61 callees, 18 vtable calls) -- processes each instruction individually, substituting virtual-to-physical register mappings into operand fields. Handles special cases: tied operands, implicit definitions, constant operands, register pairs, and CTA-reconfig pragmas.

- **`AllocateRegisters_encode_all_instructions`** (`0x18EF990`, 108 KB, 3,724 lines, 78 callees) -- the second-largest function in the region. Iterates all functions and all basic blocks, calling the per-instruction encoder. Handles error recovery: on `"Internal compiler error."`, attempts fallback encoding. References setmaxnreg and no-spill diagnostics.

Supporting functions:

| Function | Address | Size | Role |
|---|---|---|---|
| `regalloc_assign_physical_register` | `0x18A65E0` | 3 KB | Assigns a physical register |
| `regalloc_apply_register_assignment` | `0x18BF0E0` | 5 KB | Applies assignment to instruction |
| `regalloc_compute_operand_encoding` | `0x18A1990` | 30 KB | Computes operand encoding (self-recursive) |
| `regalloc_build_register_map` | `0x18A7C40` | 25 KB | Virtual-to-physical register map |
| `regalloc_fixup_phi_registers` | `0x18C0CD0` | 6 KB | Fixes up phi-node register assignments |
| `regalloc_resolve_phi_copies` | `0x18C3950` | 16 KB | Resolves phi copies to register moves |
| `regalloc_handle_tied_operands` | `0x18AB9C0` | 7.5 KB | Handles operands tied to same physical register |
| `regalloc_handle_partial_writes` | `0x18C1A90` | 5 KB | Handles partial-width writes |

## Post-Allocation Verification

The post-allocation verification system at `0x19D0000`--`0x1A00000` is one of the most thorough compiler verification subsystems discovered in the binary. It spans approximately 120 functions and validates that register allocation did not introduce correctness bugs.

### Top-Level Verification

| Function | Address | Size | Evidence |
|---|---|---|---|
| `AllocateRegisters_full_verification` | `0x19D6730` | 76 KB | `"TOTAL MISMATCH"`, `"POTENTIAL PROBLEM"`, `"BENIGN (explainable)"` |
| `AllocateRegisters_post_verify_driver` | `0x19E12A0` | 49 KB | `"TOTAL MISMATCH %d   MISMATCH ON OLD %d\n"` |
| `verify_regalloc_spill_correctness` | `0x19D34D0` | 49 KB | Spill/refill pattern verification |
| `verify_rematerialization_correctness` | `0x19D52F0` | 29 KB | `"REMATERIALIZATION PROBLEM..."` |
| `verify_post_regalloc_reaching_defs` | `0x19D1D40` | 35 KB | Reaching-definition analysis |
| `verify_register_allocation_correctness` | `0x198A350` | 44 KB | Top-level allocation validation |

### Verification Strategy

The verifier works by computing reaching definitions before and after register allocation, then comparing them. Any mismatch indicates a potential bug:

1. **Reaching-definition snapshot** -- before register allocation, the verifier records which definitions reach each use point.
2. **Post-allocation comparison** -- after allocation, it recomputes reaching definitions on the physical-register code and compares against the pre-allocation snapshot.
3. **Mismatch classification**:
   - **`"BENIGN (explainable)"`** -- the mismatch is explained by a known transformation (spill/refill, rematerialization, P2R/R2P predicate packing)
   - **`"POTENTIAL PROBLEM"`** -- the mismatch cannot be explained and may indicate a compiler bug
   - **`"TOTAL MISMATCH"`** -- complete failure to match any definitions

### Specific Pattern Checks

The verifier checks several specific correctness patterns:

- **Bit-spill-refill** (`0x19DDC90`) -- validates that spill stores are correctly paired with refill loads: `"Failed to establish match for bit-spill-refill pattern..."`
- **P2R-R2P** (`0x19DE150`) -- validates predicate packing/unpacking correctness: `"Failed to establish match for P2R-R2P pattern..."`
- **Upper-bit clobbering** (`0x19DE510`) -- checks that 32-bit writes to a register do not inadvertently clobber the upper 32 bits when the register is used as part of a 64-bit pair: `"Some instruction(s) are destroying the base of..."`
- **Rematerialization** (`0x19DBE60`) -- verifies that rematerialized values match the original computation: `"REMATERIALIZATION PROBLEM. New Instruction..."`

### Debug Knob

The verification subsystem is activated by the knob `-knob DUMPIR=AllocateRegisters`. When enabled, detailed diagnostics are emitted:

```
"Please use -knob DUMPIR=AllocateRegisters for debugging"
"This def [%d] represents uninitialized value..."
```

The `memcheck` knob enables additional checking granularity. The `"Found %d potentially uninitialized register(s) in function %s"` diagnostic at `0x1984920` reports uninitialized-register usage detected during verification.

## Resource Usage Reporting

After register allocation and codegen complete, the resource usage reporter at `sub_140A6B0` (5.5 KB) emits a comprehensive summary of the function's resource consumption. This is the output visible when compiling with `--resource-usage` or verbose mode.

### Reported Resources

| Resource | Format String | Description |
|---|---|---|
| Registers | `"Used %d registers"` | Physical R-registers allocated |
| Barriers | `", used %d barriers"` | Hardware barriers consumed |
| Global memory | `"%lld bytes gmem"` | Global memory accessed |
| Constant memory | `", %lld bytes cmem[%d]"` | Per-bank constant memory (18 banks, indices 0--17) |
| Shared memory | `", %lld bytes smem"` | Shared memory allocated (including spill if SMEM spilling active) |
| Local memory | `", %lld bytes lmem"` | Stack frame size (spill slots + local variables) |
| Stack size | `", %d bytes cumulative stack size"` | Cumulative stack including callees (verbose mode) |
| Textures | `", %d textures"` | Texture references bound |
| Samplers | `", %d samplers"` | Sampler objects bound |
| Surfaces | `", %d surfaces"` | Surface references bound |
| Compile time | `"Compile time = %.3f ms"` | Per-function compilation time |

The resource counts are retrieved via dedicated accessor functions:
- `sub_43CAA0` -- register count
- `sub_43CBC0` -- barrier count
- `sub_43CD80` -- stack size
- `sub_43C680` -- shared memory size
- `sub_43C780` -- local memory size

### Extended Metrics Header

The metrics emission system at `0x19A1B30` (36 KB) writes a richer statistics header as PTX comments. This is the `# N instructions, M R-regs` comment visible in PTX output:

```
# %d instructions, %d R-regs
# [inst=%d] [texInst=%d] [tepid=%d] [rregs=%d] [urregs=%d] [_lat2inst=%.1f]
# [est latency = %d] [LSpillB=%d] [LRefillB=%d]...
# [est adu=%d] [est alu=%d] [est cbu=%d] [est fma2x=%d]...
# [issue thru=%f] [adu thru=%f] [alu thru=%f]...
# [Occupancy = %f]
# [FP16 inst=%d]
```

The metrics include:
- Instruction count and register count
- Texture instruction count, estimated latency
- Spill bytes (LSpillB) and refill bytes (LRefillB)
- Per-unit instruction estimates: adu, alu, cbu, fma2x, fma, half, transcendental, ipa, lsu, redux, schedDisp, tex, ttu, udp
- Tensor core instruction counts: imma, hmma, dmma, bmma
- Throughput estimates per functional unit
- Occupancy estimate
- FP16 instruction count
- Loop analysis statistics
- SharedMem allocation throughput

### REGALLOC GUIDANCE Output

The final reporting pass at `0x18F9A60` (`AllocateRegisters_final_reporting`, 30 KB) emits structured guidance data:

```
REGALLOC GUIDANCE:
ALLOCATION: ...
```

This includes the final register count, whether spilling occurred, the spill strategy used (local vs SMEM), and any setmaxnreg interactions. The companion `SCHEDULING GUIDANCE:` output (at `0x19C1A70`) provides scheduling-related metrics.

## Function Address Summary

The complete register allocation subsystem, listed by pipeline stage:

### Core Regalloc (0x189C000--0x18FC000)

| Address | Size | Function |
|---|---|---|
| `0x18988D0` | 71 KB | `AllocateRegisters_main_driver` |
| `0x189B2D0` | 4 KB | `regalloc_classify_register_class` |
| `0x189B6B0` | 3 KB | `regalloc_allocate_physical_register` |
| `0x189BB30` | 3 KB | `regalloc_check_register_conflict` |
| `0x189BE00` | 7.5 KB | `regalloc_try_coalesce` |
| `0x189C3E0` | 48 KB | `regalloc_graph_coloring_core` (self-recursive) |
| `0x189E600` | 11.5 KB | `regalloc_build_interference_graph` |
| `0x189F300` | 38 KB | `regalloc_compute_spill_cost` (self-recursive) |
| `0x18A0DB0` | 13.5 KB | `regalloc_compute_live_ranges` |
| `0x18A1990` | 30 KB | `regalloc_compute_operand_encoding` (self-recursive) |
| `0x18A65E0` | 3 KB | `regalloc_assign_physical_register` |
| `0x18A6860` | 8 KB | `regalloc_handle_live_range_split` |
| `0x18A7C40` | 25 KB | `regalloc_build_register_map` |
| `0x18AE2D0` | 155 KB | `AllocateRegisters_per_instruction_encode` |
| `0x18B3AE0` | 46 KB | `AllocateRegisters_nospill_pass` |
| `0x18C28E0` | 6 KB | `regalloc_compute_register_budget` |
| `0x18C7480` | 25 KB | `regalloc_spilling_pass_driver` |
| `0x18C8790` | 20 KB | `regalloc_smem_spilling_driver` |
| `0x18CE9F0` | 29 KB | `AllocateRegisters_pressure_analysis` |
| `0x18D5EC0` | 15 KB | `regalloc_ABI_call_pressure_report` |
| `0x18D69F0` | 15 KB | `regalloc_multi_class_driver` |
| `0x18DB260` | 55 KB | `AllocateRegisters_spill_pass_full` |
| `0x18DD9D0` | 26 KB | `regalloc_spilling_regalloc_driver` |
| `0x18DEA00` | 50 KB | `AllocateRegisters_iterative_spill` (self-recursive) |
| `0x18E1BF0` | 46 KB | `AllocateRegisters_with_target_count` |
| `0x18E3530` | 33 KB | `AllocateRegisters_negotiate_budget` (self-recursive) |
| `0x18E54B0` | 75 KB | `AllocateRegisters_full_pipeline` |
| `0x18EB9D0` | 48 KB | `AllocateRegisters_spill_iteration_loop` |
| `0x18EF990` | 108 KB | `AllocateRegisters_encode_all_instructions` |
| `0x18F51E0` | 84 KB | `AllocateRegisters_full_with_spill_retry` |
| `0x18F9A60` | 30 KB | `AllocateRegisters_final_reporting` |

### setmaxnreg / CTA Reconfig (0x18FC000--0x191A000)

| Address | Size | Function |
|---|---|---|
| `0x18FB430` | 9 KB | `setmaxnreg_compute_register_budget_A` |
| `0x18FBA60` | 9 KB | `setmaxnreg_compute_register_budget_B` |
| `0x18FC090` | 22 KB | `setmaxnreg_enforcement_driver` |
| `0x18FD920` | 11 KB | `setmaxnreg_check_compatibility` |
| `0x1904F00` | 16 KB | `setmaxnreg_driver_per_module` |
| `0x1906500` | 14 KB | `setmaxnreg_emit_all_warnings` |
| `0x1906DE0` | 7 KB | `CTA_reconfig_validate_pragmas` |
| `0x19082A0` | 14 KB | `CTA_reconfig_insert_alloc_dealloc` |
| `0x190C990` | 17 KB | `CTA_reconfig_full_analysis` |
| `0x190E080` | 24 KB | `CTA_reconfig_master_driver` |
| `0x1912A00` | 33 KB | `setmaxnreg_top_level_driver` |
| `0x19149A0` | 19 KB | `setmaxnreg_full_transform` |

### Second-Pass Regalloc (0x1AAA960--0x1AD5B00, from p1.19)

| Address | Size | Function |
|---|---|---|
| `0x1AAA960` | 118 KB | `register_allocation_pass` (ABI-aware, `"max_abi_regs"`) |
| `0x1AB3410` | 15 KB | `register_spill_manager` |
| `0x1AB59F0` | 21 KB | `register_interference_graph_builder` |
| `0x1AB6790` | 14 KB | `register_coalescing_pass` |
| `0x1AB7E50` | 19 KB | `register_coloring_pass` |
| `0x1AB93C0` | 13 KB | `uniform_register_allocator` |
| `0x1ABC360` | 23 KB | `register_liveness_analysis` |

### Verification (0x19D0000--0x1A00000)

| Address | Size | Function |
|---|---|---|
| `0x1984920` | 30 KB | `verify_uninitialized_registers_per_function` |
| `0x198A350` | 44 KB | `verify_register_allocation_correctness` |
| `0x19D1D40` | 35 KB | `verify_post_regalloc_reaching_defs` |
| `0x19D34D0` | 49 KB | `verify_regalloc_spill_correctness` |
| `0x19D52F0` | 29 KB | `verify_rematerialization_correctness` |
| `0x19D6730` | 76 KB | `AllocateRegisters_full_verification` |
| `0x19E12A0` | 49 KB | `AllocateRegisters_post_verify_driver` |
| `0x19FA010` | 22 KB | `verify_full_pass_driver` |
| `0x19FC110` | 5 KB | `verify_post_regalloc_driver` |
