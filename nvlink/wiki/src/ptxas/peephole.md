# Peephole Optimization

> **Note**: This page documents the peephole optimization subsystem inside the embedded ptxas copy in nvlink v13.0.88 -- specifically the early SASS-level cluster at `0x406377`, the ORI named-phase pass pipeline inside MercConverter, and the late `OptimizeNaNOrZero`/`TexNodep` passes. The standalone ptxas binary uses three monolithic 233--280 KB peephole mega-dispatchers (generic, SM120, post-schedule) at completely different addresses; for that architecture see [ptxas Peephole Optimization](../../ptxas/codegen/peephole.html).

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

### ORI Named Phase Manager -- Architecture

`sub_197A120` (49 KB, 1,850 lines) is the ORI named-phase **sequence builder**. It does not execute passes directly; instead it constructs an opcode array that the pass executor (`sub_1AEECD0`) then iterates. The architecture has three layers:

```
sub_197C4B0  (entry: allocates opcode array, calls sub_197A120 + sub_1AEECD0)
  |
  +-- sub_197A120  (reads option strings, maps names to opcode IDs, builds array)
  |     |
  |     +-- sub_1AEDF30  (name-to-ID lookup: binary search in table at 0x2445D60)
  |     +-- sub_175D4B0  (option string parser: "key=value" pairs from compiler options)
  |
  +-- sub_1AEECD0  (pass executor: iterates opcode array, dispatches via vtable)
        |
        +-- per-pass vtable->execute()
              |
              +-- sub_1979420  (per-instruction ORI entry point)
                    |
                    +-- sub_1977B70  (pass merger: per-instruction lowering)
                    +-- sub_19756C0  (copy propagation engine, 1,008 lines)
                    +-- sub_19733B0  (opcode dispatcher: switch on ~150 opcodes)
                    +-- sub_1976C60  (instruction emitter, 936 lines)
```

The opcode table at `0x2445D60` (returned by `sub_1AEAA80`) is a sorted array of `{name_ptr, opcode_id}` pairs. The lookup function `sub_1AEDF30` performs case-insensitive binary search and returns the opcode integer, or 158 (the `NOP`/unknown sentinel) on failure.

### ORI Phase Names and Configuration

The 14 named phases are not independent pass implementations. They are **configuration parameters** that control a single pass execution engine. The named-phase manager reads each name from the option string and interprets it as a repeat count:

| Phase Name | Type | Parameter Semantics |
|---|---|---|
| `swap1` | Repeat count (0--256) | Number of iterations for operand swap class 1 |
| `swap2` | Repeat count (0--256) | Number of iterations for operand swap class 2 |
| `swap3` | Repeat count (0--256) | Number of iterations for operand swap class 3 |
| `swap4` | Repeat count (0--256) | Number of iterations for operand swap class 4 |
| `swap5` | Repeat count (0--256) | Number of iterations for operand swap class 5 |
| `swap6` | Repeat count (0--256) | Number of iterations for operand swap class 6 |
| `cpy1` | Repeat count (0--256) | Iterations for copy propagation round 1 |
| `cpy2` | Repeat count (0--256) | Iterations for copy propagation round 2 |
| `cpy3` | Repeat count (0--256) | Iterations for copy propagation round 3 |
| `dce1` | Repeat count (0--256) | Iterations for dead code elimination round 1 |
| `dce2` | Repeat count (0--256) | Iterations for dead code elimination round 2 |
| `dce3` | Repeat count (0--256) | Iterations for dead code elimination round 3 |
| `OriPerformLiveDead` | Injected opcode | Liveness analysis (inserted before DCE rounds) |
| `OriCopyProp` | Injected opcode | Global copy propagation (inserted before copy rounds) |

Additionally, two meta-parameters control the overall pipeline:

| Parameter | Type | Effect |
|---|---|---|
| `shuffle` | Repeat count (0--256) | Number of random pass-order permutations to apply |
| `reps` | Repeat count (0--256) | Number of times to repeat the entire pass sequence |
| `NamedPhases` | Mode flag | Enables the configurable `p0`, `p1`, ... phase slot system |

### Sequence Construction Algorithm

The manager builds the pass opcode array in `dest[]` (up to 304 entries). The algorithm, reconstructed from decompilation of `sub_197A120`:

```c
// sub_197C4B0: entry point
void ori_run_phases(context_t *ctx) {
    opcode_table_t *table = sub_1AEC480(ctx);  // initialize opcode table
    int32_t dest[256] = {0};
    dest[0] = 158;  // NOP sentinel

    // sub_197A120: build the sequence
    int count = ori_build_sequence(ctx, table, dest);

    // sub_1AEECD0: execute the sequence
    ori_execute_sequence(ctx, dest, count);
}

// Reconstructed from sub_197A120 (1,850 lines)
int ori_build_sequence(context_t *ctx, opcode_table_t *table, int32_t *dest) {
    // Step 1: Read the default opcode sequence from the global table (0x2445D60)
    //         This is the "base" pass ordering compiled into the binary.
    int base_count = table->count;
    int32_t *base_opcodes = table->opcodes;

    // Step 2: Check if custom options are provided (knob 298)
    bool has_options = check_knob(ctx, 298);
    if (has_options) {
        // Parse "key=value" pairs from compiler option string
        // via sub_175D4B0 into s2[] (keys) and nptr[] (values)
        parse_options(ctx, s2, nptr, v343, 256);
    }

    // Step 3: Check for "NamedPhases" mode
    if (find_option(s2, "NamedPhases")) {
        // Each slot p0..p242 can be overridden to a specific opcode.
        // Slots 243..303 get hardcoded opcodes 256..303.
        for (int i = 0; i < 243; i++) {
            sprintf(s, "p%d", i);
            if (find_option(s2, s))
                dest[i] = strtol(get_value(s2, nptr, s), NULL, 10);
            else
                dest[i] = base_opcodes[i + 13]; // offset into base table
        }
        // Slots 243-303: fixed assignment (opcodes 256-303)
        return 304;
    }

    // Step 4: Check for "shuffle" mode -- randomize pass order
    if (find_option(s2, "shuffle")) {
        int reps = parse_int_option(s2, nptr, "reps", 0, 256);
        // Read per-class iteration counts
        int swap1_n = parse_int_option(s2, nptr, "swap1", 0, 256);
        int swap2_n = parse_int_option(s2, nptr, "swap2", 0, 256);
        // ... swap3 through swap6 ...
        int dce1_n  = parse_int_option(s2, nptr, "dce1",  0, 256);
        int dce2_n  = parse_int_option(s2, nptr, "dce2",  0, 256);
        int dce3_n  = parse_int_option(s2, nptr, "dce3",  0, 256);
        int cpy1_n  = parse_int_option(s2, nptr, "cpy1",  0, 256);
        int cpy2_n  = parse_int_option(s2, nptr, "cpy2",  0, 256);
        int cpy3_n  = parse_int_option(s2, nptr, "cpy3",  0, 256);

        // Build interleaved sequence: for each base opcode, optionally
        // inject OriPerformLiveDead before DCE rounds and OriCopyProp
        // before copy propagation rounds.
        int pos = 0;
        for (int i = 0; i < base_count; i++) {
            // If dce1, dce2, or dce3 repeat count matches this iteration,
            // inject OriPerformLiveDead opcode
            if (dce2_n == i || dce1_n == i || dce3_n == i) {
                dest[pos++] = lookup("OriPerformLiveDead");  // via sub_1AEDF30
            }
            // If cpy1, cpy2, or cpy3 repeat count matches this iteration,
            // inject OriCopyProp opcode
            if (cpy2_n == i || cpy1_n == i || cpy3_n == i) {
                dest[pos++] = lookup("OriCopyProp");  // via sub_1AEDF30
            }
            dest[pos++] = base_opcodes[i];
        }

        // Apply shuffle permutations: swap adjacent elements at
        // offsets derived from swap1-6 counts
        for (int r = 0; r < reps; r++) {
            // 6 pairs of swaps per repetition
            swap_at(dest, (swap1_n + r) % pos, ...);  // swap class 1
            swap_at(dest, (swap2_n + r) % pos, ...);  // swap class 2
            swap_at(dest, (swap3_n + r) % pos, ...);  // swap class 3
            swap_at(dest, (swap4_n + r) % pos, ...);  // swap class 4
            swap_at(dest, (swap5_n + r) % pos, ...);  // swap class 5
            swap_at(dest, (swap6_n + r) % pos, ...);  // swap class 6
        }
        return pos;
    }

    // Step 5: Default mode -- copy base opcodes directly
    memcpy(dest, base_opcodes, base_count * 4);
    return base_count;
}
```

**Confidence: HIGH** -- the control flow, string comparisons, `strtol` calls, and `sub_1AEDF30` lookups are directly visible in the decompilation. The `sprintf(s, "p%d", i)` call for `NamedPhases` mode is at line ~467 of the decompilation.

### Pass Execution Engine

`sub_1AEECD0` (276 lines) executes the opcode array. For each entry:

1. Looks up the pass object: `pass = pass_table[16 * opcode_id]`
2. Calls `pass->getName()` via vtable offset +8 to get the pass name string
3. Records the pass in the diagnostic timeline (with `"Before "` prefix for IR dumps)
4. Calls `pass->execute()` via vtable offset +16

The pass objects live in a table indexed by opcode ID. Each pass object has a standard vtable layout:

| Vtable offset | Method | Description |
|---|---|---|
| +0 | `destructor` | Cleanup |
| +8 | `getName()` | Returns pass name string (e.g., `"swap1"`) |
| +16 | `execute()` | Run the pass on current function |

### What Each Phase Actually Does

#### swap1 through swap6 -- Operand Canonicalization

The swap phases do not each target a distinct class of commutativity. Instead, the `swap1`--`swap6` names are **iteration count parameters** that control how many adjacent-element swaps to apply at different offsets within the pass ordering. The decompiled shuffle loop (lines 1693--1729 of `sub_197A120`) shows six swap pairs per iteration:

```c
for (int r = 0; r < reps; r++) {
    // Each swap exchanges dest[offset % count] with dest[(offset + r + 1) % count]
    adjacent_swap(dest, (swap1_n + r) % count);
    adjacent_swap(dest, (swap2_n + r) % count);
    adjacent_swap(dest, (swap3_n + r) % count);
    adjacent_swap(dest, (swap4_n + r) % count);
    adjacent_swap(dest, (swap5_n + r) % count);
    adjacent_swap(dest, (swap6_n + r) % count);
}
```

Each swap class provides an independent starting offset for the permutation. With six classes and a repeat count, NVIDIA can systematically explore pass orderings. This is a standard compiler testing technique: randomize pass order to verify that optimizations are order-independent (or to find the best ordering empirically).

When not in shuffle mode, the swap opcodes in the base pass table perform actual operand rewriting. The per-instruction dispatcher `sub_19733B0` handles these through the general opcode switch (cases covering all ~150 SASS opcodes), where operand positions are swapped to canonical form -- for example, ensuring the smaller register number appears first in commutative operations, or normalizing FMA source order.

**Confidence: HIGH** -- the shuffle loop with modular arithmetic on six offset variables is clearly visible in the decompilation.

#### dce1, dce2, dce3 -- Dead Code Elimination (Three Rounds)

The three DCE phases remove instructions whose definitions are never used. Each phase is parameterized by an iteration count that controls when `OriPerformLiveDead` is injected before it:

```
Round 1 (dce1):  OriPerformLiveDead -> [base passes] -> DCE scan
Round 2 (dce2):  OriPerformLiveDead -> [base passes] -> DCE scan
Round 3 (dce3):  OriPerformLiveDead -> [base passes] -> DCE scan
```

The injection logic (lines 1549--1565 of `sub_197A120`) checks whether the current base-pass index matches any of `dce1_n`, `dce2_n`, or `dce3_n`, and if so, prepends an `OriPerformLiveDead` opcode. This ensures liveness information is recomputed before each DCE round.

The DCE pass itself iterates over all instructions in the function. For each instruction with a destination register, it checks whether the register appears in the live-out set of the basic block (computed by `OriPerformLiveDead`). If the destination is dead and the instruction has no side effects (no memory writes, no control flow), the instruction is removed and its source operands' use counts are decremented. This may create new dead definitions, which is why multiple DCE rounds are beneficial.

**Example (before/after dce1):**

```
// Before: R5 is computed but never read
IADD3 R5, R2, R3, RZ ;   // dead definition
IMAD  R7, R4, R6, R8 ;   // live
STG   [R0], R7       ;   // R7 is consumed here

// After dce1: R5 definition removed
IMAD  R7, R4, R6, R8 ;
STG   [R0], R7       ;
```

**Confidence: MEDIUM** -- the injection logic and `OriPerformLiveDead` string are directly observed; the specific DCE algorithm is inferred from standard compiler practice and the structure of the pass table.

#### cpy1, cpy2, cpy3 -- Copy Propagation (Three Rounds)

The three copy propagation phases replace uses of a register that holds a copy of another register with the original source. Like DCE, each phase has its own injection point for `OriCopyProp`:

```
Round 1 (cpy1):  OriCopyProp -> [base passes] -> copy forward
Round 2 (cpy2):  OriCopyProp -> [base passes] -> copy forward
Round 3 (cpy3):  OriCopyProp -> [base passes] -> copy forward
```

The injection logic (lines 1645--1649) checks whether the current index matches any of `cpy1_n`, `cpy2_n`, or `cpy3_n`, and if so, inserts an `OriCopyProp` opcode.

The copy propagation engine `sub_19756C0` (1,008 lines) is the largest individual pass implementation. It operates on NVIDIA's machine-level IR linked-list structure:

1. **Walk the instruction list** in program order (the `while(2)` loop starting at line 161)
2. **Build def-use chains** using a per-register tracking structure (64-byte nodes allocated at line 166)
3. **For each MOV-like instruction** (opcode 97 = `MOV`, or other copy-equivalent opcodes detected by operand analysis at `sub_1972240`): record the mapping `dest_reg -> src_reg`
4. **For each use of a copied register**: if the source register is still available (not redefined since the copy), replace the use with the original source. The availability check involves walking the def-use chain and verifying no intervening write.

The three rounds handle chains of copies: `R3 <- R2`, `R5 <- R3` becomes `R5 <- R2` in round 1, and the now-dead `R3 <- R2` is removed by the subsequent DCE round, enabling further propagation in round 2.

**Example (before/after cpy1 + dce1):**

```
// Before: chain of copies
MOV   R3, R2         ;   // copy
IADD3 R5, R3, R4, RZ ;   // uses copy

// After cpy1: R3 replaced by R2
MOV   R3, R2         ;   // now dead
IADD3 R5, R2, R4, RZ ;   // direct use

// After dce1: dead copy removed
IADD3 R5, R2, R4, RZ ;
```

**Confidence: MEDIUM** -- the `sub_19756C0` linked-list traversal and node allocation are directly observed; the exact copy-equivalence detection logic is partially inferred.

#### OriPerformLiveDead -- Liveness Analysis

This pass computes per-basic-block live-in and live-out register sets using backward dataflow analysis. It is not a standalone named pass that users configure; rather, it is automatically injected before DCE rounds by the sequence builder.

The opcode for `OriPerformLiveDead` is resolved at runtime via `sub_1AEDF30(table, "OriPerformLiveDead")` (line 1556 of `sub_197A120`). The pass walks basic blocks in reverse postorder, computing:

```
live_out[B] = union(live_in[S]) for all successors S of B
live_in[B]  = (live_out[B] - def[B]) | use[B]
```

This iterates to a fixed point. The result is stored in per-basic-block metadata and consumed by subsequent DCE passes to determine which definitions are dead.

**Confidence: HIGH** -- the string `"OriPerformLiveDead"` and its injection point are directly observed in decompilation. The algorithm is standard backward liveness.

#### OriCopyProp -- Global Copy Propagation

This pass is the global variant of copy propagation, injected before each cpy round. While `cpy1`--`cpy3` control the local per-instruction copy forwarding, `OriCopyProp` performs interprocedural copy analysis across basic block boundaries.

The opcode is resolved via `sub_1AEDF30(table, "OriCopyProp")` (line 1648). The pass builds a global copy graph mapping destination registers to source registers across the entire function, then replaces uses that cross basic block boundaries.

**Confidence: MEDIUM** -- the string and injection point are directly observed; the global scope is inferred from its position relative to the local cpy passes.

### ORI Pass Manager Merge

`sub_1977B70` (35 KB, 1,341 lines) is the per-instruction pass merger. It is called from `sub_1979420` (the per-instruction ORI entry point) and performs instruction-level lowering. For each IR instruction:

1. Reads the instruction opcode from field +72 (with mask `& 0xFFFFCFFF`)
2. For opcode 0x61 (97 decimal = `MOV`): calls `sub_19733B0` for operand rewriting, then performs FNV-1a hash lookup (the characteristic `16777619 * (... ^ 0x811C9DC5)` hash at lines 269--271) into a pattern table to find multi-instruction rewrite rules
3. For other opcodes: dispatches through the instruction emitter `sub_1976C60` with operand descriptors, or directly encodes via `sub_18B8D90` (the SASS instruction builder)

The FNV-1a hash is used for pattern matching in a hash table at offset +752 in the context structure. This enables O(1) lookup of multi-instruction rewrite patterns by instruction hash.

**Confidence: HIGH** -- the FNV-1a constant `16777619` and seed `0x811C9DC5` are unmistakable in the decompilation.

### Per-Instruction Dispatch (sub_19733B0)

`sub_19733B0` (879 lines) is the core per-instruction ORI dispatcher. It reads the instruction opcode from field +72 (masked with `BYTE1(v) &= 0xCF` to clear modifier bits), then dispatches through a switch statement covering ~150 SASS opcode cases to ~50 distinct handler functions:

| Opcode ID(s) | Handler | Type |
|---|---|---|
| 1 | `sub_19606A0` | Simple ALU (single operand rewrite) |
| 2, 3, 4, 5, 7 | `vtable[0]` (virtual dispatch) | Generic instruction handler |
| 6 | `sub_19601E0` | Comparison/predicate ops |
| 8 | `sub_1958520` | Extended precision ops |
| 10, 11, 149, 151, 152, 290, 291 | `sub_195E1C0` | Load/store class |
| 14, 39, 40, 105, 125, 299, 300, 321 | `vtable[7]` | Texture/surface ops |
| 15 | `sub_1972420` | Control flow (branch/jump) |
| 16 | `sub_196EC00` | Barrier/fence ops |
| 17 | `sub_196E090` | Warp-level ops |
| 22 | `sub_195CE90` | Integer shift/rotate |
| 23 | `sub_196B9D0` | Bitwise logical ops |
| 24 | `sub_1960040` | Predicate logic |
| 26 | `sub_196B5A0` | Uniform register ops |
| 27 | `sub_196AC90` | Convert (type cast) |
| 28 | `sub_195FF50` | Float special (RCP, RSQ, LG2, EX2) |
| 32, 271 | `sub_1968520` | Shared memory load/store |
| 38, 59, 106, 180, 182, 192, 194, 215, 221, 242 | `sub_1960790` | Memory with address calc |
| 42, 53, 55, 66 | `sub_195B590` | FMA (fused multiply-add) |
| 52, 54, 72, 97 | `sub_1956AA0` | MOV / copy-equivalent |
| 60, 62, 78, 79 | `sub_196BFC0` | Comparison/select (IMNMX, FMNMX) |
| 61, 63, 80 | `sub_196C6E0` | Set predicate ops |
| 75 | `sub_1966CF0` | PRMT (byte permute) |
| 85, 15 | `sub_1972420` | Control flow |
| 88, 89 | `sub_195BA70` | Double-precision FMA |
| 90 | `sub_1958900` | Packed integer ops |
| 91 | `sub_196D6E0` | Reduction ops |
| 92 | `sub_196D970` | Atomic ops |
| 93, 95 | `sub_1967E20` | Tensor core (HMMA-related) |
| 94 | `sub_1967ED0` | Tensor core (IMMA-related) |
| 96 | `sub_195A2A0` | Global memory atomic |
| 100 | `sub_195D1C0` | Texture fetch (TEX/TLD) |
| 103, 104 | `sub_19692B0` | Constant memory load |
| 124 | `sub_1967990` | Video instruction (VMNMX) |
| 130, 169 | `vtable[29]` | Call/return |
| 201, 202, 204, 285 | `sub_1970D10` | Uniform ALU |
| 205 | `sub_1967340` | Tensorcore MMA ops |
| 269 | NOP (emits 0xFFFF) | No-operation sentinel |
| 158, 167 | `sub_196AAE0` | Unknown/fallback handler |

Each handler function receives the ORI context (`a1`) and the instruction node (`a2`), and returns a boolean indicating whether the instruction was modified. The handler performs:

1. **Operand validation**: check that source/destination operands are in the expected format
2. **Operand rewriting**: apply the specific transformation (swap, propagate, or eliminate)
3. **Instruction replacement**: if the transformation changes the opcode, update the instruction node in the IR linked list

**Confidence: HIGH for the dispatch table** (opcode cases and function addresses are directly from decompilation); **MEDIUM for handler descriptions** (inferred from opcode groupings and handler function sizes).

### ORI Default Pass Ordering

When no custom options are provided, the ORI pipeline uses the default opcode sequence compiled into the binary at `0x2445D60`. This sequence, returned by `sub_1AEAA80`, encodes the default pass ordering as an array of opcode IDs. The `sub_197A120` default path (lines 1776--1785) simply copies this array:

```c
// Default path: no custom options
memcpy(dest, base_opcodes, base_count * 4);
return base_count;
```

The default ordering interleaves the passes as:

```
[OriPerformLiveDead] [swap passes...] [OriCopyProp] [cpy1 passes...]
[dce1 passes...] [OriPerformLiveDead] [OriCopyProp] [cpy2 passes...]
[dce2 passes...] [OriPerformLiveDead] [OriCopyProp] [cpy3 passes...]
[dce3 passes...] [OriPerformLiveDead]
```

The interleaving of copy propagation and dead code elimination in three rounds is a classic fixed-point iteration strategy. Each round of copy propagation replaces register-to-register moves with direct references, which makes the source register's definition dead if it has no other uses. The subsequent DCE pass removes the now-dead definition, potentially enabling further copy propagation in the next round.

### Instruction Emitter (sub_1976C60)

`sub_1976C60` (936 lines) is the ORI instruction emitter. It is called from `sub_1977AA0` (the pass merger's instruction handler) when the advanced mode flag at offset +1048 is set. It takes a lowered instruction descriptor with fields:

- `a3`: opcode ID (from the base table)
- `a4`: source register class
- `a5`: destination width (3 = 32-bit, other values for 16/64-bit)
- `a6`: modifier flags
- `a7`: operand count

The emitter constructs the machine instruction by:

1. Looking up the instruction template from the opcode table at offset +296 in the context
2. Walking the instruction's operand linked list (via pointer at offset +8 of each operand node)
3. For each operand: encoding register number, immediate value, or predicate into the instruction word
4. Inserting the completed instruction into the output IR linked list

When the advanced mode flag is NOT set, the simpler path through `sub_1977AA0` directly encodes the instruction word:

```c
// Simplified encoding (sub_1977AA0 line 34-38):
word = (operand_count << 13) & 0x1FE000
     | opcode_byte
     | (modifier << 10) & 0x1C00
     | (src_class << 21) & 0x600000
     | (dest_width << 8)
     | 0x60000000;  // ORI instruction marker
```

**Confidence: HIGH** -- the encoding formula is directly visible in `sub_1977AA0` line 34--38.

### ORI Diff Detection

After the pass sequence executes, `sub_197A120` performs a diff between the original opcode array and the modified one (lines 1787--1826). It computes a popcount of the XOR between old and new entries to detect how many passes actually modified the IR:

```c
int changes = 0;
for (int i = 0; i < count; i++) {
    int diff = dest[i] ^ original[i];
    if (diff) {
        // popcount via repeated (x & (x-1)) -- clears lowest set bit
        while (diff) {
            diff &= (diff - 1);
            changes++;
        }
    }
}
bool modified = (changes != 0);
```

If modifications were detected AND custom options were NOT provided, the modified opcode array is written back to the output buffer `a3` (the third argument to `sub_197A120`). Otherwise the `dest[]` local array (which may have been reordered by shuffle) is written back.

**Confidence: HIGH** -- the popcount-via-bit-clearing loop is a well-known pattern and is directly visible in the decompilation.

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

| Address | Size | Identity | Description |
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
| `sub_19733B0` | 17,344 B | `ORI_opcode_dispatcher` (switch on ~150 opcodes) | HIGH |
| `sub_1974470` | 18,560 B | `ORI_pipeline_constructor` | HIGH |
| `sub_19756C0` | 17,792 B | `ORI_copy_propagation_engine` | HIGH |
| `sub_1976C60` | 14,720 B | `ORI_instruction_emitter` | HIGH |
| `sub_1977AA0` | 672 B | `ORI_instruction_handler` (encode or dispatch) | HIGH |
| `sub_1977B70` | 35,066 B | `ORI_pass_merger` (per-instruction lowering) | HIGH |
| `sub_1979420` | 7,808 B | `ORI_per_instruction_entry` | HIGH |
| `sub_197A120` | 49,238 B | `ORI_sequence_builder` (builds opcode array) | HIGH |
| `sub_197C4B0` | 96 B | `ORI_run_phases` (entry: build + execute) | HIGH |
| `sub_1AEAA80` | 16 B | `ORI_get_opcode_table` (returns `0x2445D60`) | HIGH |
| `sub_1AEDF30` | 1,312 B | `ORI_name_to_opcode` (case-insensitive binary search) | HIGH |
| `sub_1AEECD0` | 4,416 B | `ORI_pass_executor` (iterates opcode array) | HIGH |

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

### nvlink Internal
- [Embedded ptxas Overview](overview.md) -- complete address map and compilation pipeline context
- [Instruction Scheduling](scheduling.md) -- the main ScheduleInstructions pass that invokes OptimizeNaNOrZero
- [Register Allocation](register-allocation.md) -- the regalloc pass that TexNodep runs after
- [IR Nodes](ir-nodes.md) -- the IR node structure manipulated by peephole passes
- [Mercury Compiler Passes](../mercury/compiler-passes.md) -- Mercury-specific ORI pass integration

### Sibling Wikis
- [ptxas: Peephole Optimization](../../ptxas/codegen/peephole.html) -- standalone ptxas peephole pass (three 250KB dispatch functions)
