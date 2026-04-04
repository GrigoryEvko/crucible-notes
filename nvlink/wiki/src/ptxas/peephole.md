# Peephole Optimization

The nvlink embedded ptxas backend applies peephole optimizations at three distinct points in the compilation pipeline: (1) an early SASS-level peephole pass in the linker finalization path (`0x406377`--`0x4094FD`), operating on already-encoded instruction buffers; (2) the ORI (Operand Rewriting Infrastructure) pass pipeline embedded within the MercConverter instruction lowering phase (`0x1916000`--`0x1960000`), which runs swap, copy-propagation, dead-code elimination, and liveness passes on the machine-level IR; and (3) late peephole passes integrated into the scheduling and verification phases, including `OptimizeNaNOrZero` (`0x1866FA0`) and the `TexNodep` texture node peephole (`0x19938E0`). Together these three layers constitute approximately 350 KB of peephole-related code across ~50 functions.

The peephole infrastructure is unusual relative to standard LLVM: rather than a single instcombine-style pass, nvlink scatters small, targeted transformation passes across the pipeline. The ORI system is entirely NVIDIA-proprietary -- it has no upstream LLVM equivalent and operates on NVIDIA's machine-level IR after instruction selection but before final scheduling and register allocation.

## Pipeline Position

The peephole passes execute at the following pipeline stages:

```
ISel pattern match  ->  MercConverter (with ORI passes)  ->  HoistInvariants
     |                        |                                    |
     |                   swap1-6, cpy1-3,                     OptimizeNaNOrZero
     |                   dce1-3, LiveDead,                         |
     |                   CopyProp                          ScheduleInstructions
     |                        |                                    |
     v                        v                              AllocateRegisters
  [early peephole]     [MercConverter output]                      |
  0x406377-0x4094FD          |                               TexNodep (post-RA)
                        "After MercConverter"                       |
                              |                              Codegen Verification
                         NamedPhases                               |
                              |                              SASS Emission
                         Final Emit
```

## Key Facts

| Property | Value |
|---|---|
| Early peephole address range | `0x406377`--`0x4094FD` (~40 KB, 10 functions) |
| ORI pass manager address | `0x197A120` (49 KB, `ORI_named_phase_manager`) |
| ORI pass merger address | `0x1977B70` (35 KB, `ORI_pass_manager_merge`) |
| MercConverter address | `0x1919030` (92 KB, `MercConverter_instruction_converter`) |
| OptimizeNaNOrZero address | `0x1866FA0` (28 KB, `schedule_optimize_nan_or_zero`) |
| TexNodep address | `0x19938E0` (39 KB, `TexNodep_optimization_pass`) |
| Total peephole code | ~350 KB across ~50 functions |
| Pass name strings | `"swap1"`--`"swap6"`, `"cpy1"`--`"cpy3"`, `"dce1"`--`"dce3"`, `"OriPerformLiveDead"`, `"OriCopyProp"`, `"NamedPhases"`, `"OptimizeNaNOrZero"`, `"TexNodep"` |

## Early SASS-Level Peephole (0x406377 -- 0x4094FD)

This cluster of 10 functions at the very beginning of the `.text` section performs peephole optimization directly on SASS instruction buffers. These functions have no string references -- they are identified entirely by their instruction-level access patterns (checking opcode fields at offsets +72, +76 against constants like 126, 120, 11, 12, 6).

### Functions

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_406377` | 7,438 B | `peephole_pattern_match` | Pattern match and transform instruction sequences |
| `sub_4069EE` | 4,693 B | `peephole_control_flow` | Branch/jump simplification |
| `sub_406DC0` | 6,830 B | `peephole_optimizer_main` | Main driver -- orchestrates all sub-passes |
| `sub_407634` | 5,320 B | `peephole_instruction_combine` | Combine instruction pairs sharing operands |
| `sub_407C0A` | 3,160 B | `peephole_strength_reduce` | Replace expensive ops with cheaper equivalents |
| `sub_407F94` | 3,692 B | `peephole_constant_fold` | Fold constant operands at instruction level |
| `sub_4083A5` | 2,941 B | `peephole_dead_code` | Remove dead instructions using liveness info |
| `sub_408594` | 6,542 B | `peephole_scheduler` | Local instruction reordering for latency hiding |
| `sub_408C90` | 2,318 B | `peephole_helper` | Instruction property classifier |
| `sub_408EC2` | 7,693 B | `peephole_register_analysis` | Register liveness via bitmap operations |
| `sub_4094FD` | 2,753 B | `peephole_post_sched` | Post-scheduling cleanup pass |

### Algorithm

The main driver (`sub_406DC0`) iterates over an instruction buffer and calls sub-passes in a fixed order:

```c
void peephole_optimizer_main(context_t *ctx, instr_buf_t *buf) {
    // Phase 1: register liveness analysis (bitmap per basic block)
    peephole_register_analysis(ctx, buf);

    // Phase 2: pattern-based transformations (iterate to fixed point)
    bool changed;
    do {
        changed = false;
        changed |= peephole_pattern_match(ctx, buf);     // multi-insn patterns
        changed |= peephole_instruction_combine(ctx, buf); // pairwise combine
        changed |= peephole_constant_fold(ctx, buf);      // constant propagation
        changed |= peephole_strength_reduce(ctx, buf);    // strength reduction
        changed |= peephole_control_flow(ctx, buf);       // branch simplify
        changed |= peephole_dead_code(ctx, buf);          // DCE with liveness
    } while (changed);

    // Phase 3: scheduling-aware reordering
    peephole_scheduler(ctx, buf);

    // Phase 4: post-scheduling cleanup
    peephole_post_sched(ctx, buf);
}
```

The `peephole_instruction_combine` function checks two source operands of each instruction, looks for a dependent instruction chain (checking opcodes against constants 126, 120, 11, 12, 6), validates compatibility of the pair, and rewrites into a single fused instruction. The validation call goes through `sub_1B0DB90` (an external instruction validation function in the SASS emission region).

The `peephole_pattern_match` function implements multi-instruction pattern recognition. It accesses decoded instruction fields at various offsets, performs boolean logic for condition-code analysis, and rewrites matched sequences into more efficient forms.

## ORI Pass Pipeline (0x1916000 -- 0x198A000)

The ORI (Operand Rewriting Infrastructure) is a proprietary NVIDIA machine-level optimization framework that runs as part of the MercConverter instruction lowering phase. It implements a named-phase pass manager that dispatches to 14 distinct sub-passes.

### MercConverter Integration

`MercConverter_instruction_converter` at `sub_1919030` (92 KB, 2,685 lines) is the third-largest function in this region. It converts high-level IR operations to machine-level IR (the "CONVERTING" phase), then invokes ORI passes to clean up the machine code. The string `"CONVERTING"` appears in diagnostic output, and `"Internal compiler error."` is emitted if the conversion encounters an unrepresentable operation.

The MercConverter calls into ORI sub-passes directly (string evidence shows `"swap3"`, `"swap5"`, `"OriCopyProp"` referenced from within the converter), meaning some ORI passes run interleaved with the conversion rather than purely as a post-processing step.

### ORI Named Phase Manager

`sub_197A120` (49 KB, 1,850 lines) is the ORI named-phase manager. It parses phase name strings and dispatches to the corresponding implementation. String references confirm all 14 phase names:

| Phase Name | Category | Purpose |
|---|---|---|
| `swap1` | Operand swap | Canonicalize operand order (first pass) |
| `swap2` | Operand swap | Canonicalize operand order (second pass) |
| `swap3` | Operand swap | Canonicalize operand order (third pass) |
| `swap4` | Operand swap | Canonicalize operand order (fourth pass) |
| `swap5` | Operand swap | Canonicalize operand order (fifth pass) |
| `swap6` | Operand swap | Canonicalize operand order (sixth pass) |
| `cpy1` | Copy propagation | Forward register copies (first pass) |
| `cpy2` | Copy propagation | Forward register copies (second pass) |
| `cpy3` | Copy propagation | Forward register copies (third pass) |
| `dce1` | Dead code elimination | Remove dead definitions (first pass) |
| `dce2` | Dead code elimination | Remove dead definitions (second pass) |
| `dce3` | Dead code elimination | Remove dead definitions (third pass) |
| `OriPerformLiveDead` | Liveness | Compute live/dead register sets |
| `OriCopyProp` | Copy propagation | Global copy propagation pass |

The multiple numbered iterations (swap1-6, cpy1-3, dce1-3) are not identical repetitions -- each pass operates at a different granularity or with different canonicalization rules. The swap passes normalize operand ordering to enable subsequent pattern matches; six passes suggest NVIDIA handles commutative, associative, and fused-multiply-add operand permutations separately. The copy and DCE passes run multiple times because each copy propagation may expose new dead code, and each DCE pass may enable further copy propagation.

### ORI Pass Manager Merge

`sub_1977B70` (35 KB, 1,341 lines) manages the merging and scheduling of ORI passes. String `"After MercConverter"` indicates it runs after the main conversion loop. String `"shuffle"` suggests the pass ordering can be randomized for testing (a common compiler testing technique to verify pass-ordering independence). The `"NamedPhases"` string connects this manager to the named phase dispatcher.

### ORI Algorithm

The ORI pipeline operates on machine-level IR after MercConverter has lowered instructions:

```c
void ori_pipeline(context_t *ctx, function_t *func) {
    // Phase 1: Compute initial liveness
    OriPerformLiveDead(ctx, func);

    // Phase 2: Operand canonicalization (6 swap passes)
    // Each pass normalizes a different class of commutativity:
    //   swap1: basic commutative ops (ADD, MUL)
    //   swap2: fused ops (FMA source ordering)
    //   swap3: comparison operand normalization
    //   swap4: logical ops (LOP3 operand permutation)
    //   swap5: memory address canonicalization
    //   swap6: predicate normalization
    for (int i = 1; i <= 6; i++)
        ori_swap(ctx, func, i);

    // Phase 3: Copy propagation + DCE (interleaved, 3 rounds)
    for (int round = 1; round <= 3; round++) {
        ori_copy_propagate(ctx, func, round);  // cpy{round}
        ori_dead_code_elim(ctx, func, round);  // dce{round}
    }

    // Phase 4: Global copy propagation
    OriCopyProp(ctx, func);

    // Phase 5: Final liveness recomputation
    OriPerformLiveDead(ctx, func);
}
```

The interleaving of copy propagation and dead code elimination in three rounds is a classic fixed-point iteration strategy. Each round of copy propagation replaces register-to-register moves with direct references, which makes the source register's definition dead if it has no other uses. The subsequent DCE pass removes the now-dead definition, potentially enabling further copy propagation in the next round.

## OptimizeNaNOrZero (0x1866FA0)

`sub_1866FA0` (28 KB, 925 lines) implements a peephole optimization pass that runs during the instruction scheduling phase. It is invoked from within `ScheduleInstructions_per_function_driver` (`sub_1860A40`) after the main scheduling loop.

String references: `"cutlass"`, `"OptimizeNaNOrZero"`.

This pass targets a specific pattern common in matrix multiplication kernels (especially CUTLASS GEMM workloads): operations that produce NaN or zero results that can be statically determined from the input operand properties. The optimization recognizes patterns where:

1. A floating-point operation has an operand that is known to be zero (e.g., initialized to zero in a reduction accumulator).
2. A floating-point operation would produce NaN due to `0 * infinity` or similar IEEE 754 edge cases.
3. The result of a NaN-producing operation is subsequently used in a min/max/select that would choose the non-NaN alternative.

In these cases the pass replaces the floating-point computation with a direct move of the known result value, eliminating unnecessary FMA/FADD/FMUL instructions and their associated pipeline latency.

The `"cutlass"` string reference indicates CUTLASS workload detection gates this optimization -- it is conditionally enabled when the scheduler detects a CUTLASS-style GEMM pattern (checked via `sub_1866CF0`, 3,541 bytes).

### Integration with Scheduling

OptimizeNaNOrZero runs as a sub-pass of the scheduling infrastructure. The scheduling pipeline flow:

```
ScheduleInstructions_main_driver (sub_1851DC0, 85 KB)
  -> Strategy selection (sub_1857990) -- choose default/ReduceReg/DynBatch
  -> Per-function driver (sub_1860A40, 47 KB)
       -> OptimizeNaNOrZero (sub_1866FA0, 28 KB)
       -> Per-block scheduling (sub_185B870, 28 KB)
            -> List scheduler core (sub_1864ED0, 18 KB)
       -> HoistInvariants (sub_186C7A0, 24 KB)
```

The NaN/zero optimization runs before per-block scheduling so the scheduler can account for eliminated instructions in its latency calculations.

## TexNodep -- Texture Node Peephole (0x19938E0)

`sub_19938E0` (39 KB, 1,387 lines) implements a texture node peephole optimization that runs after register allocation, in the codegen verification phase. String reference: `"TexNodep"`.

This pass optimizes texture fetch instruction sequences. On NVIDIA GPUs, texture operations involve complex instruction sequences (address calculation, descriptor load, sampler configuration, the TEX/TLD/TXQ instruction itself, and result extraction). The TexNodep pass identifies opportunities to:

1. Merge texture address calculations with the fetch instruction.
2. Eliminate redundant descriptor loads when multiple texture fetches share the same descriptor.
3. Reorder texture fetch result extraction to improve register allocation quality.
4. Insert texture prefetch hints (`sub_1997140`, `tex_node_insert_prefetch`).
5. Cluster texture operations for better memory access patterns (`sub_19985C0`, `tex_node_cluster`).

### TexNodep Sub-Functions

| Address | Size | Identity | Purpose |
|---|---|---|---|
| `sub_19938E0` | 39 KB | `TexNodep_optimization_pass` | Main driver (self-recursive) |
| `sub_1995100` | 9 KB | `tex_node_analysis` | Analyze texture operation dependencies |
| `sub_1995A50` | 12 KB | `tex_node_transform` | Apply texture node transformations |
| `sub_19963C0` | 5 KB | `tex_node_helper` | Utility classifier |
| `sub_1996890` | 6 KB | `tex_node_rewrite` | Rewrite texture instruction sequences |
| `sub_1996ED0` | 3 KB | `tex_node_check_eligibility` | Check if transformation is legal |
| `sub_1997140` | 4 KB | `tex_node_insert_prefetch` | Insert TEX prefetch hints |
| `sub_19973A0` | 5 KB | `tex_node_optimize_sampler` | Optimize sampler configuration |
| `sub_1997710` | 5 KB | `tex_node_compute_latency` | Compute texture latency for scheduling |
| `sub_1997CE0` | 10 KB | `tex_node_schedule_around` | Schedule instructions around TEX latency |
| `sub_19985C0` | 8 KB | `tex_node_cluster` | Cluster texture operations |
| `sub_1998FA0` | 6 KB | `tex_node_handle_binding` | Handle texture binding state |
| `sub_1999AA0` | 7 KB | `tex_node_dependency_analysis` | Texture dependency chain analysis |
| `sub_199A270` | 4 KB | `tex_node_helper_small` | Small helper utility |
| `sub_199A510` | 8 KB | `tex_node_reorder` | Reorder texture operations |
| `sub_199AC20` | 5 KB | `tex_node_check_coherence` | Coherence validation |
| `sub_199B4E0` | 10 KB | `tex_node_transform_block` | Per-basic-block transformation |
| `sub_199BCF0` | 11 KB | `tex_node_handle_vectorization` | Vectorize texture operations |
| `sub_199C4B0` | 4 KB | `tex_node_helper_vectorize` | Vectorization helper |
| `sub_199C9D0` | 10 KB | `tex_node_merge_operations` | Merge adjacent texture ops |
| `sub_199D580` | 12 KB | `tex_node_compute_metrics` | Compute optimization metrics |
| `sub_199DC10` | 15 KB | `tex_node_optimization_per_block` | Per-block optimization driver |
| `sub_199E6E0` | 28 KB | `tex_node_driver_per_function` | Per-function driver |

The `TexNodep` pass is self-recursive, suggesting it iterates to a fixed point or processes nested texture operation chains. The large number of sub-functions (23) reflects the complexity of GPU texture pipeline optimization.

## HoistInvariants

`sub_186C7A0` (24 KB, 749 lines) implements loop-invariant code motion at the machine level. This is a peephole-style pass that identifies instructions within loops whose operands are all defined outside the loop (invariant), and hoists them to the loop preheader.

String references: `"HoistInvariants"`, `"cutlass"`, `"OptimizeNaNOrZero"`, `"ConvertMemoryToRegisterOrUniform"`.

The hoisting pass has three main components:

| Address | Size | Identity |
|---|---|---|
| `sub_186C7A0` | 24 KB | `HoistInvariants_core` -- core hoisting logic |
| `sub_186D520` | 38 KB | `HoistInvariants_per_function` -- per-function driver |
| `sub_186EE80` | 41 KB | `HoistInvariants_analysis_driver` -- analysis + transformation |

Helper functions in the `0x1871000`--`0x188A000` range (~35 functions) implement:

- Loop body analysis for hoisting candidates (`sub_1871050`, 19 KB)
- Operand invariance checking (`sub_1872550`, 6 KB)
- Memory access classification for aliasing (`sub_1872A20`, 9 KB)
- Cost-benefit analysis for hoisting decisions (`sub_1873030`, 5 KB)
- Side-effect checking (`sub_18744C0`, 5 KB)
- Shared-memory-specific hoisting (`sub_1874B40`, 9 KB)
- Uniform register dependence analysis (`sub_1875310`, 6 KB)

The CUTLASS workload detection influences hoisting aggressiveness: for CUTLASS GEMM kernels, the pass is more aggressive about hoisting address calculations and descriptor loads out of the inner loop, because the register pressure cost is offset by the latency savings in the tight matrix multiplication loop body.

## ROT13-Obfuscated Pass Names

Several internal pass and configuration names in the binary are stored as ROT13-encoded strings. The decoder function `sub_1A40AC0` uses SIMD-accelerated ROT13 (loading 16 bytes at a time via `_mm_load_si128`). Known peephole-related decoded strings:

| ROT13 (in binary) | Decoded | Context |
|---|---|---|
| `ranoyr_fzrz_fcvyyvat` | `enable_smem_spilling` | Hidden feature flag controlling shared-memory spilling (affects post-regalloc peephole behavior) |

SASS opcode mnemonics referenced by peephole passes are also ROT13-encoded in the opcode table (`sub_1A85E40`). Key examples: `VZNQ` = IMAD, `SZHY` = FMUL, `SNQQ` = FADD, `SRAPR` = FENCE, `ZREPHEL` = MERCURY. The peephole passes decode these at runtime to match instruction opcodes by name.

## Tepid Instruction Scheduler (0x16F6000 -- 0x1740000)

The "Tepid" scheduler is a second, independent instruction scheduling pipeline that operates at a different level than the main `ScheduleInstructions` pass. Located in the `0x16F6000`--`0x1740000` range (~296 KB, ~50 functions), it runs peephole-like local scheduling transformations with a focus on latency hiding.

Key string evidence: `"TepidMacUtil"`, `"TepidTime"`, `"MacInsts"`, `"MacReuses"`, latency hiding metrics (`"LDS latency hiding: Num"`, `"LDG latency hiding: Num"`, `"Xu64 latency hiding: Num"`, `"Antidep latency hiding: Num"`).

The Tepid scheduler collects per-basic-block statistics including:

| Metric String | Meaning |
|---|---|
| `tSubBb` | Sub-basic-block count |
| `HeaderBb` | Header basic block indicator |
| `Nvopts` | Number of nvopt scheduling hints |
| `LsuResBusy` | Load-store unit resource busy cycles |
| `Time` | Total scheduled time |
| `TepidTime` | Tepid-phase scheduling time |
| `MacInsts` | MAC (multiply-accumulate) instruction count |
| `MacReuses` | MAC register reuse count |

Key Tepid sub-functions:

| Address | Size | Identity |
|---|---|---|
| `sub_16F35A0` | 36 KB | `scheduler_block_processor` -- main per-block scheduling |
| `sub_16F6390` | 4 KB | `tepid_mac_loop_stats` -- MAC loop statistics |
| `sub_16F7370` | 5 KB | `scheduler_latency_calculator` -- instruction latency computation |
| `sub_16F7830` | 5 KB | `scheduler_resource_tracker` -- resource utilization tracking |
| `sub_16F7BB0` | 4 KB | `scheduler_dependency_checker` -- data dependency checking |
| `sub_16F7F70` | 4 KB | `scheduler_stall_detector` -- scheduling stall detection |
| `sub_16F8640` | 5 KB | `scheduler_reuse_tracker` -- register reuse tracking |
| `sub_16F8B80` | 4 KB | `scheduler_issue_slot_manager` -- issue slot assignment |
| `sub_16F9080` | 8 KB | `scheduler_anti_dependency_resolver` -- anti-dependency resolution |
| `sub_16F9980` | 15 KB | `scheduler_latency_hiding_analyzer` -- latency hiding quality metrics |
| `sub_16FAB00` | 3 KB | `scheduler_small_helper` -- utility |
| `sub_16FAD00` | 10 KB | `scheduler_block_stats_collector` -- per-block statistics |
| `sub_16FB430` | 5 KB | `scheduler_instruction_classifier` -- instruction classification |
| `sub_16FB800` | 6 KB | `scheduler_dual_issue_checker` -- dual-issue compatibility |

The Tepid scheduler queries knob 610 for scheduling aggressiveness level and checks architecture capabilities at offset +43920 in the knob table. It supports dual-issue checking (important for SM7x+ architectures where certain instruction pairs can issue simultaneously).

## Configuration

The peephole passes are controlled by the internal knob system and compiler options:

| Control | Effect |
|---|---|
| `-knob DUMPIR=AllocateRegisters` | Dumps IR after register allocation, useful for inspecting post-peephole state |
| `--opt-level` | Higher optimization levels enable more aggressive peephole patterns |
| `--fast-compile` | Disables some peephole passes for faster compilation |
| Knob 610 | Tepid scheduler aggressiveness (checked via vtable dispatch) |
| `enable_smem_spilling` (ROT13: `ranoyr_fzrz_fcvyyvat`) | Hidden flag affecting post-regalloc peephole |
| CUTLASS detection | `OptimizeNaNOrZero` and `HoistInvariants` aggressiveness |

## Function Map

### Early SASS Peephole (0x406377 -- 0x4094FD)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_406377` | 7,438 B | `peephole_pattern_match` | MEDIUM |
| `sub_4069EE` | 4,693 B | `peephole_control_flow` | LOW |
| `sub_406DC0` | 6,830 B | `peephole_optimizer_main` | MEDIUM |
| `sub_407634` | 5,320 B | `peephole_instruction_combine` | MEDIUM |
| `sub_407C0A` | 3,160 B | `peephole_strength_reduce` | LOW |
| `sub_407F94` | 3,692 B | `peephole_constant_fold` | LOW |
| `sub_4083A5` | 2,941 B | `peephole_dead_code` | LOW |
| `sub_408594` | 6,542 B | `peephole_scheduler` | LOW |
| `sub_408C90` | 2,318 B | `peephole_helper` | LOW |
| `sub_408EC2` | 7,693 B | `peephole_register_analysis` | LOW |
| `sub_4094FD` | 2,753 B | `peephole_post_sched` | LOW |

### ORI Pipeline (0x1916000 -- 0x198A000)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_1919030` | 91,774 B | `MercConverter_instruction_converter` | HIGH |
| `sub_1977B70` | 35,066 B | `ORI_pass_manager_merge` | HIGH |
| `sub_197A120` | 49,238 B | `ORI_named_phase_manager` | HIGH |

### Scheduling-Phase Peepholes (0x1850000 -- 0x19A0000)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_1866FA0` | 27,839 B | `schedule_optimize_nan_or_zero` | HIGH |
| `sub_186C7A0` | 24,457 B | `HoistInvariants_core` | HIGH |
| `sub_186D520` | 37,807 B | `HoistInvariants_per_function` | HIGH |
| `sub_186EE80` | 41,095 B | `HoistInvariants_analysis_driver` | HIGH |
| `sub_19938E0` | 39,040 B | `TexNodep_optimization_pass` | HIGH |
| `sub_199E6E0` | 27,529 B | `tex_node_driver_per_function` | HIGH |

### Tepid Scheduler (0x16F6000 -- 0x1740000)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_16F35A0` | 35,648 B | `scheduler_block_processor` | HIGH |
| `sub_16F9980` | 15,356 B | `scheduler_latency_hiding_analyzer` | HIGH |
| `sub_16FAD00` | 10,216 B | `scheduler_block_stats_collector` | MEDIUM |

## Cross-References

- [Embedded ptxas Overview](overview.md) -- complete address map and compilation pipeline context
- [Instruction Scheduling](scheduling.md) -- the main ScheduleInstructions pass that invokes OptimizeNaNOrZero
- [Register Allocation](register-allocation.md) -- the regalloc pass that TexNodep runs after
- [IR Nodes](ir-nodes.md) -- the IR node structure manipulated by peephole passes
