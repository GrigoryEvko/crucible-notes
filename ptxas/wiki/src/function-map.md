# Function Map

> **Binary**: ptxas v13.0.88, 37.7 MB stripped ELF, ~40,000 functions  
> **Scope**: ~200 key identified functions organized by subsystem  
> **Source**: Extracted from p1.01-p1.30 sweep reports (40 files, 34,880 lines of analysis)

This page catalogs the most important identified functions in ptxas, organized by subsystem in approximate pipeline order. All addresses are for the v13.0.88 binary.

**Confidence levels**: CERTAIN = named in symbols or strings, structure fully understood. HIGH = strong evidence from strings and call patterns (>90%). MEDIUM = structural analysis with partial string evidence (70-90%).

---

## 1. Entry Point & Static Initialization

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x409460` | 84 B | `main` (program entry) | CERTAIN | 1 | Sets unbuffered stdio, delegates to `0x446240` |
| `0x4094C0` | 204 B | `ctor_001` -- thread infra init | HIGH | 0 | pthread_key_create, mutex init, priority range |
| `0x4095D0` | 17,007 B | `ctor_003` -- PTX opcode name table | HIGH | 0 | ~900 ROT13-encoded PTX mnemonic entries |
| `0x40D860` | 80,397 B | `ctor_005` -- tuning knob registry | HIGH | 0 | 2000+ ROT13 Mercury knob names + hex defaults |
| `0x421290` | 7,921 B | `ctor_007` -- scheduler knob registry | HIGH | 0 | 98 ROT13 scheduler knobs (XBlockWait, WarDeploy, etc.) |

`ctor_005` is the largest static initializer in the binary (80 KB). All three ROT13 ctors use character rotation A-M+13, N-Z-13 as a light obfuscation layer over internal NVIDIA names.

---

## 2. Memory Allocator

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x424070` | 2,098 B | `pool_alloc(pool, size)` | HIGH | 3,809 | Custom slab allocator, 8-byte aligned |
| `0x4248B0` | 923 B | `pool_free(ptr)` | HIGH | 1,215 | Coalescing free, boundary tags for large blocks |
| `0x424C50` | 488 B | `pool_realloc(ptr, new_size)` | MEDIUM | 27 | Delegates to pool_alloc + memcpy |
| `0x42BDB0` | 14 B | `fatal_OOM_handler` | HIGH | 3,825 | Tiny wrapper, called on every allocation failure |

Pool struct layout: free list bins at +2128, mutex at +7128. Small allocs (<=4999 B) use size-binned free lists; large allocs use boundary-tag coalescing. Thread-safe via pthread_mutex_lock/unlock.

---

## 3. Thread-Local Storage

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x4280C0` | 597 B | `get_thread_local_context` | HIGH | 3,928 | The most-called function in ptxas |

Returns a 280-byte per-thread struct via `pthread_getspecific`. Contains error/warning flags, memory pool pointer, diagnostic suppression flags, pthread_cond_t (+128), pthread_mutex_t (+176), sem_t (+216).

---

## 4. Hash Map Infrastructure

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x425CA0` | 114 B | `hashmap_create(hash_fn, cmp_fn, cap)` | HIGH | 127 | Detects integer/pointer hash modes |
| `0x425D20` | 121 B | `hashmap_destroy(map)` | MEDIUM | 63 | Frees buckets, entries, map struct |
| `0x426150` | 2,534 B | `hashmap_put(map, key, value)` | HIGH | 2,800 | Open-addressing + chained buckets, auto-resize |
| `0x426D60` | 345 B | `hashmap_get(map, key)` | HIGH | 422 | Returns value or 0 if not found |
| `0x426EC0` | 349 B | `hashmap_contains(map, key)` | HIGH | 29 | Returns 1/0 |
| `0x427630` | 273 B | `murmurhash3_x86_32(str)` | HIGH | 73 | Constants: 0xcc9e2d51, 0x1b873593 |
| `0x42D850` | 2,531 B | `hashset_insert(set, key)` | HIGH | 282 | Hash set variant with auto-resize |

Three hash modes selected by flags at offset 84 bits 4-7: mode 0 = custom function pointers, mode 1 = pointer hash (`key>>11 ^ key>>8 ^ key>>5`), mode 2 = integer hash.

---

## 5. Linked List & String Utilities

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x4279D0` | 51 B | `starts_with(str, prefix)` | HIGH | 185 | Returns suffix pointer or 0 |
| `0x42CA60` | 81 B | `list_push_front(node, head_ptr)` | HIGH | 298 | Allocates 16-byte node via pool_alloc |
| `0x42CC30` | 34 B | `list_count(head)` | HIGH | 48 | Simple `for(n=0; p; p=p->next) n++` |

---

## 6. Diagnostics & Error Reporting

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x42FBA0` | 2,388 B | `diagnostic_emit(desc, loc, fmt...)` | HIGH | 2,350 | Central error/warning/info/fatal reporter |
| `0x42F590` | varies | `fatal_internal_error(desc, ...)` | HIGH | 3,825 | Called from OOM handler and assertions |
| `0x4275F0` | 8 B | `exit_wrapper(code)` | HIGH | 1 | Indirect call through function pointer table |
| `0x403588` | 75 B | `print_usage_and_exit()` | HIGH | 1 | "Usage : %s [options] ..." |

Severity levels in descriptor byte at `*a1`: 0=suppress, 1-2=info, 3=warning (or error via `--Werror`), 4=error\*, 5=error, 6=fatal (triggers `longjmp`). Machine-readable tags: `@E@`, `@W@`, `@O@`, `@I@`. Source-line display caches file offsets every 10 lines.

---

## 7. Command-Line Parsing

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x432A00` | 6,427 B | `option_registration` | HIGH | 1 | Defines all recognized CLI options |
| `0x434320` | 10,289 B | `option_parser` | HIGH | 1 | Validates combinations, applies to state |
| `0x439880` | 2,935 B | `chrome_trace_json_parser` | HIGH | 1 | For `--fdevice-time-trace` |
| `0x43A400` | 4,696 B | `compilation_target_config` | HIGH | 1 | SM-specific defaults (cache, texturing) |
| `0x43B660` | 3,843 B | `register_constraint_calculator` | HIGH | 1 | Balances .maxnreg, occupancy, .minnctapersm |

Options registered include: `--register-usage-level`, `--cloning`, `--verbose`, `--version-ls`, `--compile-functions`, `--input-as-string`, `--suppress-stack-size-warning`, `--fast-compile`, `--warn-on-spills`, `--compiler-stats`, `--fdevice-time-trace`.

---

## 8. Compilation Driver

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x446240` | 11,064 B | `real_main` (top-level driver) | HIGH | 1 | Orchestrates entire pipeline |
| `0x4428E0` | 13,774 B | `ptx_input_setup` | HIGH | 1 | Version/target validation, dummy entries |
| `0x43CC70` | 5,425 B | `per_entry_compile_unit` | HIGH | 1 | Processes each entry through pipeline |
| `0x43F400` | 9,078 B | `function_abi_config` | HIGH | 1 | Parameter regs, return addr, scratch regs |
| `0x441780` | 3,975 B | `tools_patch_handler` | MEDIUM | 1 | `--compile-as-tools-patch`, cuda_sanitizer |

`real_main` prints timing breakdowns: Parse-time, CompileUnitSetup-time, DAGgen-time, OCG-time, ELF-time, DebugInfo-time, plus `CompileTime` and `PeakMemoryUsage`.

---

## 9. PTX Parser & Lexer

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x451730` | 14,135 B | `parser_setup` (special register init) | HIGH | 1 | Inits %ntid, %laneid, %clock, etc. |
| `0x46E000` | 93,037 B | `instruction_table_builder` | HIGH | 1 | 1168 callees, one per PTX opcode |
| `0x4CE6B0` | 48,263 B | `bison_parser` (directive/decl) | HIGH | 1 | .local_maxnreg, .alias, .pragma, etc. |
| `0x720F00` | ~64 KB | `flex_lexer` (ptxlex / yylex) | CERTAIN | 2 | ~550 Flex rules, DFA scanner |
| `0x4B2F20` | 52,600 B | `ptx_validator_general` | HIGH | 1 | Validates texture, surface, cvt, call, etc. |
| `0x4C5FB0` | 28,537 B | `ptx_validator_mma_wmma_tcgen05` | HIGH | 1 | MMA, WMMA, tensor core validation |

### Preprocessor

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x71F630` | ~14 KB | `preprocessor_dispatch` | HIGH | 1 | .MACRO, .ELSE, .INCLUDE dispatch |
| `0x71E2B0` | ~32 KB | `conditional_handler` (.ELSE/.ELIF) | HIGH | 1 | Conditional preprocessing |
| `0x71DCA0` | ~8.4 KB | `macro_definition` (.MACRO) | HIGH | 1 | Handles nested .MACRO definitions |
| `0x71C310` | ~8.3 KB | `include_handler` (.INCLUDE) | HIGH | 1 | Recursive include file processing |

The instruction_table_builder (93 KB) is the largest front-end function. It calls one handler-setup function per PTX opcode, registering accepted type combinations (e.g., `F32F32`, `I32I8I8I32`, `_mma.warpgroup`).

---

## 10. Intrinsic Infrastructure

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x5D1660` | ~46 KB | `intrinsic_table_register` (608 entries) | CERTAIN | 1 | Master intrinsic name-to-ID table |
| `0x5D4190` | ~41 KB | `intrinsic_dispatch_builder` | CERTAIN | 1 | PTX opcode -> codegen handler mapping |
| `0x5FF700` | ~354 KB | `intrinsic_prototype_emitter` | CERTAIN | 1 | Giant switch generating .weak .func decls |

The 608 registered intrinsics span: `__cuda_reduxsync_*` (17), `__cuda_sanitizer_*` (7), `__cuda_sm20_*` (70 math), `__cuda_sm70_*` (~370 Volta+: barrier, shfl, vote, wmma), `__cuda_sm80_*` (14 Ampere), `__cuda_sm_9x_*` (38 Hopper sub-byte MMA), `__cuda_sm_10x_*` (10 Blackwell tcgen05).

The dispatch builder maps PTX opcodes to codegen handlers: `div`, `rem`, `rcp`, `sqrt`, `tex`, `wmma.mma`, `mma`, `wgmma.mma_async`, `tcgen05.mma`, `barrier`, `shfl`, `vote`, `ldmatrix`, `cp.async.bulk`, `multimem`, and ~100 more.

The prototype emitter (354 KB) is the single largest function by code size in the entire binary.

---

## 11. Tensor Core Codegen

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x5C7A50` | ~173 KB | `wmma_mma_codegen` | HIGH | 1 | All shapes, types, layouts |
| `0x5C10A0` | ~120 KB | `mma_codegen` (mma.sync API) | HIGH | 1 | m8n8k4 through m16n8k256 |
| `0x5BBC30` | ~90 KB | `tcgen05_mma_codegen` (Blackwell) | HIGH | 1 | 5th-gen tensor core operations |

Each codegen function allocates a 50,000-byte buffer and builds PTX code via sequential `sprintf()` calls. They query instruction properties via accessor functions at `a1+1096`: feature checks, operand counts, data types, accumulator types, layouts, MMA shapes, sparse modes.

---

## 12. OCG Intrinsic Lowering

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x6A97B0` | ~26 KB | `intrinsic_lowering_main` | MEDIUM | 1 | Large switch-based lowering |
| `0x6C9EB0` | ~13 KB | `ocg_builtin_name_lookup` | HIGH | 1 | Master Blackwell+ OCG name table |
| `0x6C0D90` | ~19 KB | `atomic_reduction_lowering` | HIGH | 1 | Validates atomic ops, memory domains |
| `0x6C1CF0` | ~16 KB | `memory_fence_order_lowering` | MEDIUM | 1 | Scope validation, memory ordering |
| `0x6C3470` | ~20 KB | `intrinsic_type_validation` | MEDIUM | 1 | Note: typo "instrinsic" in binary |
| `0x6BC560` | ~4.9 KB | `constant_bank_handler` | HIGH | 1 | Manages `c[%d]` bank references |

The OCG builtin table at `0x6C9EB0` covers: `cp_async_commit`, `cp_async_wait`, `f32add`, `bf16x4`, `acqblk`, `preexit`, `red_async`, `mbarrier`, `tcmma`, `gdesc`, `breuse`, `tcshift`, `memclear`, `sparsify`, `spfactor2to4`.

---

## 13. Instruction Encoding Core

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x6D9690` | ~94 KB | `master_instruction_encoder` | CERTAIN | 1 | Opcode switch: 61=FFMA, 416-468=sm100+ |
| `0x6D4350` | ~30 KB | `secondary_instruction_encoder` | HIGH | 1 | Additional opcode ranges |
| `0x6D7AF0` | ~19 KB | `instruction_format_builder` | MEDIUM | 1 | Non-standard encoding modes |

The master encoder (94 KB) handles: case 61 = FFMA/FADD, cases 64/66 = integer ALU, cases 416-468 = sm100+ instructions (TCMMA, TMA, barriers). Instruction word prefix `0x60000000` marks the SASS control word. Uses `sub_91D160` for register-to-encoding mapping.

---

## 14. SASS Code Generation

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x6E4110` | ~24 KB | `sass_codegen_main` | HIGH | 1 | EmitSASSForFunction, FNV-1a BB hash |
| `0x6E8EB0` | ~64 KB | `encoder_state_init` | HIGH | 1 | SM-specific: sm100 (XOR 1/8), sm103 (XOR 0x10/0x40) |
| `0x6E66D0` | ~37 KB | `encode_instruction_bytes` | MEDIUM | 1 | Per-instruction byte encoding |
| `0x6E57B0` | ~8.9 KB | `opcode_table_entry_writer` | HIGH | many | Populates encoding descriptor table |

SASS codegen iterates instruction linked list, classifying by type: -1=pseudo, 36=call, 4/137=special, 7/8/38/10/51=branch/jump. Uses FNV-1a hash (seed `0x811C9DC5`, prime 16777619) for branch target resolution.

---

## 15. SASS Pipeline

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x6F52F0` | ~23 KB | `SASS_pipeline_run_stages` | HIGH | 1 | Mercury SASS compilation pipeline |
| `0x6F8AC0` | ~14 KB | `compilation_driver_run_pipeline` | MEDIUM | 1 | Top-level pipeline driver |

---

## 16. Instruction Scheduling

### Scheduling Engine (lower address range)

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x688DD0` | ~20 KB | `scheduler_engine` (main BB loop) | HIGH | 1 | ReduceReg / DynBatch mode selection |
| `0x6820B0` | ~1.6 KB | `build_ready_list` | HIGH | 1 | Finds zero-dependency instructions |
| `0x682490` | ~14 KB | `reg_pressure_delta_analyzer` | HIGH | 1 | 511+538 element stack arrays |
| `0x6833F0` | ~10 KB | `pre_schedule_setup` | HIGH | 1 | 72-byte per-BB records, DAG init |
| `0x68B9C0` | ~46 KB | `dependency_graph_builder` | MEDIUM | 1 | RAW/WAR/WAW hazard analysis |
| `0x68A690` | ~31 KB | `alternate_scheduling_pass` | MEDIUM | 1 | Alternative heuristic strategy |

### Scheduling Orchestrator (upper address range)

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x8D0640` | ~3.9 KB | `ScheduleInstructions` (top-level) | CERTAIN | 1 | String: "ScheduleInstructions" |
| `0x8C9320` | ~10 KB | `scheduling_priority_function` | HIGH | 0 (vtable) | ~300 local vars, core heuristic |
| `0x8CBAD0` | 581 B | `pre_scheduling_bb_scan` | HIGH | 1 | Enforces 4095-instruction BB limit |
| `0x8CD160` | ~1.4 KB | `forward_scheduling_pass` | HIGH | 2 | Per-BB forward scheduling |
| `0x8CD6E0` | 201 B | `reverse_scheduling_driver` | HIGH | 1 | Reverse post-order iteration |
| `0x8CEE80` | ~1.8 KB | `register_budget_with_occupancy` | HIGH | 1 | Knob 740: pressure coeff (default 0.045) |
| `0x8CF880` | ~3.5 KB | `pre_scheduling_analysis` | HIGH | 1 | Instruction scanning and classification |
| `0x8BF890` | 224 B | `dynbatch_context_allocator` | HIGH | 2 | 184-byte context, resource vector |

Three-phase scheduling: (1) ReduceReg -- minimize register pressure (mode=0x39), (2) Reverse scheduling -- main pass via `sub_8CD6E0`, (3) DynBatch -- iterative refinement (max 16 iterations via knob 805). Register budget: `regcount - (regcount >> 6)` = 98.4% utilization.

---

## 17. SASS Mnemonic Table & HW Profiles

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x896D50` | ~21 KB | `sass_mnemonic_table_init` (ROT13) | CERTAIN | 1 | ~400+ SASS instruction names |
| `0x89FBA0` | ~16 KB | `instruction_latency_init` | HIGH | 3 | Encoding/latency property tables |

ROT13 decoding examples: `NPDOHYX`->ACQBULK, `SNQQ2`->FADD2, `SRAPR.T`->FENCE.G, `VZNQ.JVQR`->IMAD.WIDE, `WZC_VZZ`->JMP\_IMM, `YQTFGF`->LDGSTS, `ONE.FLAP.QRSRE_OYBPXVAT`->BAR.SYNC.DEFER\_BLOCKING.

---

## 18. Peephole Optimization

### IR-Level Peephole Passes

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x83EF00` | ~29 KB | `main_peephole_pass` (vtable Run) | HIGH | 0 (vtable) | Largest peephole, 392 callees |
| `0x8380A0` | ~12 KB | `secondary_peephole_pass` | HIGH | 0 (vtable) | Instruction combining/lowering |
| `0x849C60` | ~13 KB | `peephole_isel_refinement` | HIGH | 0 (vtable) | Instruction selection refinement |
| `0x853380` | ~12 KB | `peephole_rewrite_phase` | MEDIUM | 1 | 182 callees, broad pattern matching |

### SASS-Level Peephole Mega-Dispatchers

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x169B190` | 279,985 B | `isel_pattern_dispatch` (master) | CERTAIN | 1 | **Largest function in ptxas**: 65,999 insns |
| `0x143C440` | ~233 KB | `sm120_peephole_dispatch` | HIGH | 1 | SM120 (RTX 50 / Pro), 373-case switch |
| `0x198BCD0` | ~233 KB | `sm100_peephole_dispatch` | HIGH | 1 | SM100 (datacenter Blackwell), 1336 callees |

`0x169B190` is the single largest function in the entire ptxas binary (280 KB, too large for Hex-Rays). It tries 762 pattern matcher functions against each input PTX instruction, selects the best-scoring match, and records which SASS expansion template to use. Each of the three mega-dispatchers follows the same architecture: primary switch on instruction opcode, inner calls to ~1000+ pattern matchers, instruction rewrite on match.

---

## 19. Pattern Matchers & Template Expanders

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x164E010` | ~2.8 KB | `pattern_matcher` (template 10) | HIGH | 1 | Example: 5 operands, score=18 |
| `0x169A650` | ~4.7 KB | `pattern_matcher` (template 2) | HIGH | 1 | 7 source operands, score=23 |
| `0x1656AC0` | ~8.4 KB | `pattern_matcher` (multi-field) | HIGH | 1 | 12+ modifier checks, 9 src operands |
| `0x170E260` | ~1.6 KB | `ddiv_template_coordinator` | HIGH | 1 | DDIV Newton-Raphson coordinator |
| `0x1705820` | ~7.5 KB | `ddiv_sub_expander` | HIGH | 1 | DDIV multi-instruction expansion |
| `0x1701140` | ~8.7 KB | `template_register_builder` | HIGH | 1 | Virtual register array allocation |
| `0x1718D60` | ~790 B | `drcp_dsqrt_coordinator` | HIGH | 1 | DRCP/DSQRT sequence wrapper |
| `0x17276C0` | ~1.0 KB | `drsqrt_multi_precision_coord` | HIGH | 1 | DRSQRT multi-precision expansion |

762 pattern matcher functions share the same signature: `char match(ctx, instr, *template_id, *priority)`. They validate opcode fields, operand counts, register types, and immediate ranges. On match, they set a template ID and priority score. Template expanders implement Newton-Raphson sequences for DDIV, DRCP, DSQRT, DRSQRT.

---

## 20. Bitfield Packing & Encoding Helpers

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x7B9B80` | 216 B | `bitfield_insert(insn, off, wid, val)` | CERTAIN | **18,347** | **Most-called function by caller count** |
| `0x7B9D30` | 38 B | `clear_const_buffer_slots` | HIGH | 2,408 | memset(a1+468, 0xFF, 64) |
| `0x7B9D60` | 408 B | `encode_reuse_flags_predicate` | HIGH | 2,408 | 1-bit reuse + 5-bit predicate |
| `0x7BC030` | 814 B | `encode_register_operand` | HIGH | 6,147 | 1-bit presence + 4-bit type + 10-bit reg |
| `0x7BC5C0` | 416 B | `encode_immediate_const_operand` | HIGH | 1,449 | Constant buffer index or immediate |
| `0x7BCF00` | 856 B | `encode_predicate_register` | HIGH | 1,657 | PT=14, 2-bit type + 3-bit condition |

`bitfield_insert` operates on a 1280-bit (160-byte) instruction word at `a1+544`. It inserts `value` at `bit_offset` for `bit_width` bits using 64-bit chunk iteration. Called from every SASS encoder body.

### Tiny Field Encoders

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x10B6180` | 21 B | `1_bit_boolean_encoder` | HIGH | 8,091 | E.g., .S/.U, .STRONG |
| `0x10B6160` | 21 B | `1_bit_encoder` (variant) | HIGH | 2,205 | |
| `0x10B6140` | 21 B | `1_bit_encoder` (variant 2) | HIGH | 1,645 | |
| `0x10B6220` | 22 B | `3_bit_field_encoder` | HIGH | 363 | |
| `0x10B2D90` | 27 B | `2_bit_field_encoder` | MEDIUM | 538 | Data type, addressing mode |
| `0x10B5580` | 25 B | `5_bit_field_encoder` | MEDIUM | 475 | Shift amount, immediate |
| `0x10B4650` | 25 B | `4_bit_field_encoder` | MEDIUM | 330 | |

---

## 21. Knobs System

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x79B240` | 518 B | `GetKnobIndex` | CERTAIN | 2 | ROT13 name lookup, case-insensitive |
| `0x79D070` | 2,312 B | `ReadKnobsFile` | CERTAIN | 1 | Parses `[knobs]` section from file |
| `0x79F540` | 3,640 B | `ParseKnobValue` | CERTAIN | 1 | 12-type switch: bool/int/float/string/... |
| `0x7A0C10` | 1,745 B | `KnobsInit` | HIGH | 4 | State constructor, 72 bytes per knob |
| `0x79D990` | 7,073 B | `ProcessKnobs` (top-level) | HIGH | 1 | File + pragma + numbered config |
| `0x79B530` | 3,296 B | `ProcessKnobLine` | HIGH | 2 | `[WHEN condition] knobname = value` |
| `0x798B60` | 1,776 B | `NamedPhases_parser` | CERTAIN | 2 | PTXAS_DISABLE env var parsing |
| `0x799250` | 56 B | `IsPassDisabled` | HIGH | 4 | Checks knob index 185 |
| `0x7992A0` | 894 B | `IsPassDisabledForFunction` | HIGH | 1 | Per-function overrides via FNV-1a hash |

Knob values are stored in 72-byte slots at `(a2[9] + 72*knob_index)`. ParseKnobValue handles 12 types: (1) boolean, (2) integer, (3) integer+extra, (4) integer range, (5) integer list, (6) float, (7) double, (8/11) string, (9) when-string, (10) value-pair list, (12) opcode list.

---

## 22. Register Allocator

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x9721C0` | varies | `regalloc_entry` ("REGALLOC GUIDANCE") | CERTAIN | 1 | Top-level allocator entry point |
| `0x957160` | varies | `fatpoint_allocator_core` | HIGH | 1 | Core fatpoint graph coloring |
| `0x96D940` | varies | `spill_guidance_engine` | HIGH | 1 | Determines spill strategy |
| `0x971A90` | varies | `full_alloc_with_spill_retry` | HIGH | 1 | "NOSPILL REGALLOC" path |
| `0x9714E0` | varies | `regalloc_failure_reporter` | CERTAIN | 1 | "Register allocation failed..." |
| `0x9539C0` | varies | `smem_spilling_handler` | HIGH | 1 | "Smem spilling should not be enabled..." |
| `0x926A30` | 22,116 B | `interference_graph_builder` | HIGH | 7 | 155 KB decompiled, SSE bitvectors |
| `0x92C240` | 8,033 B | `liveness_bitvector_ops` | HIGH | 87 | Set/clear/query with register aliasing |
| `0x9314F0` | 403 B | `register_class_id_query` | HIGH | 1,547 | Most-called non-trivial in range |
| `0x931920` | 2,007 B | `register_class_compat_checker` | HIGH | 328 | Pair register class handling |
| `0x934630` | 1,213 B | `register_id_packer` | HIGH | 856 | Packs reg#/class/type into 32-bit |
| `0x917A60` | 6,832 B | `opcode_to_regclass_mapping` | HIGH | 221 | Massive switch, pure computation |
| `0x925510` | 341 B | `instruction_reorder` | HIGH | 13 | Doubly-linked list move-before |
| `0x910840` | ~2.1 KB | `ConvertMemoryToRegisterOrUniform` | CERTAIN | 1 | Pass driver |
| `0x8FFDE0` | 573 B | `HoistInvariants` (pass driver) | CERTAIN | 4 | Checks knob, calls sub_A112C0 |

The fatpoint-based allocator uses 2052-byte bitmask arrays (512 x 32-bit words = 16,384 bits) to track live registers. The interference graph builder (`0x926A30`) is the largest function in the allocator range at 22 KB binary / 155 KB decompiled.

---

## 23. Post-Regalloc & Named Phases

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x9F4040` | ~49 KB | `NamedPhases_parse_and_build` | CERTAIN | 1 | strcmp: "shuffle", "OriCopyProp", etc. |
| `0xA3A7E0` | varies | `statistics_reporter` | HIGH | 1 | "# %d instructions, %d R-regs" |
| `0xA46CE0` | varies | `scheduling_guidance_reporter` | HIGH | 1 | "SCHEDULING GUIDANCE:" |
| `0xA55D80` | varies | `regalloc_verification` | HIGH | 1 | "REMATERIALIZATION PROBLEM..." |
| `0xAED3C0` | ~137 KB | `scheduling_optimization_mega_pass` | HIGH | 0 (vtable) | ~560 local vars, largest vtable pass |

---

## 24. Phase Manager

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0xC60D30` | 3,554 B | `phase_factory` (159-case switch) | CERTAIN | 1 | Allocates phase objects |
| `0xC62720` | 4,734 B | `PhaseManager_ctor` | CERTAIN | 2 | Sets up 159-entry phase table |
| `0xC61B20` | 1,753 B | `PhaseManager_dtor` | HIGH | 2 | Tears down NvOptRecipe + phases |
| `0xC64F70` | 1,455 B | `phase_dispatch_loop` | CERTAIN | 2 | Executes phases, reports timing |
| `0xC64310` | 3,168 B | `per_phase_timing_reporter` | HIGH | 1 | "[Total N KB] [Freeable N KB]" |
| `0xC62200` | 888 B | `pool_consumption_reporter` | HIGH | 1 | "[Pool Consumption = N]" |
| `0xC641D0` | 305 B | `phase_name_to_index_lookup` | HIGH | 3 | Binary search, case-insensitive |
| `0xC639A0` | 1,535 B | `case_insensitive_quicksort` | HIGH | 1 | Iterative, median-of-three pivot |

The phase factory allocates one of 159 phase objects, each a 16-byte struct with vtable: `+0` execute, `+8` isNoOp, `+16` getName. The dispatch loop prints "All Phases Summary" at the end.

---

## 25. Instruction Encoder Table Bodies

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0xD27000`+ | ~592 funcs | `sass_encoder_bodies` | HIGH | 0 (vtable) | Avg 1,473 B each |

592 nearly-identical functions encoding SASS instruction variants. 16 format groups (10 x 128-bit, 5 x 64-bit). Largest group: Format 3 (`xmmword_23F1DF8`) = 145 encoders. Each encoder: set opcode bitfields, load format descriptor from rodata, encode operands into 1280-bit instruction word.

---

## 26. Bitvector Infrastructure

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0xBDBA60` | varies | `bitvector_allocate` | HIGH | many | (bits+31)>>5 word count |
| `0xBDCDE0` | varies | `bitvector_or_assign` (SSE2) | HIGH | many | `_mm_or_si128` on 128-bit chunks |
| `0xBDCF40` | varies | `bitvector_or_if_changed` | HIGH | many | Returns bool for fixed-point iteration |
| `0xBDC5F0` | varies | `bitvector_and_assign` (SSE2) | HIGH | many | `_mm_and_si128` |
| `0xBDC790` | varies | `bitvector_and_if_changed` | HIGH | many | Backward dataflow fixed-point |
| `0xBDDAA0` | varies | `bitvector_xor_assign` (SSE2) | HIGH | many | `_mm_xor_si128` |

SIMD-accelerated bitvector library. Layout: `{ data_ptr, word_count, capacity, bit_count }`. Manual SSE alignment via `-(ptr>>2) & 3`. The `_if_changed` variants scan for `(~dst & src) != 0` before applying.

---

## 27. DWARF Debug Info

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x45C3A0` | 9,041 B | `dwarf_line_info_generator` | HIGH | 1 | "$LDWend", function debug map |
| `0x45A870` | 5,293 B | `dwarf_leb128_encoder` | HIGH | 1 | File number, prologue, line advance |
| `0x866BB0` | 3,273 B | `debug_line_section_generator` | HIGH | 2 | .debug_line + .nv_debug_line_sass |
| `0x867880` | varies | `debug_info_entry` (top-level) | HIGH | 1 | Calls line table generator twice |

---

## 28. ELF / CUBIN Output

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x1CB53A0` | 3,480 B | `elf_world_init` | HIGH | 1 | 672-byte ELFW context, standard sections |
| `0x1C9F280` | 15,263 B | `master_elf_emitter` (97 KB decomp) | HIGH | 1 | Complete CUBIN assembly |
| `0x1C9DC60` | 5,663 B | `section_layout_calculator` | HIGH | 1 | Section offsets/sizes/alignment |
| `0x1CB68D0` | 9,578 B | `symbol_table_builder` | HIGH | 1 | .symtab from internal symbols |
| `0x1CB3570` | 1,963 B | `add_function_section` | HIGH | 44 | Creates .text.FUNCNAME + .rela |
| `0x1CB91C0` | 2,668 B | `elf_structure_dumper` | HIGH | 1 | Debug print: header, sections, symbols |
| `0x1CABD60` | 11,856 B | `master_section_allocator` (67 KB) | HIGH | 1 | Shared/const/local memory allocation |
| `0x1CA92F0` | 2,804 B | `shared_memory_graph_allocator` | HIGH | 1 | Interference graph for shared objects |

Standard sections created: `.shstrtab`, `.strtab`, `.symtab`, `.symtab_shndx`, `.note.nv.tkinfo`, `.note.nv.cuinfo`, `.nv.uft.entry`. Magic constant `0x70000064` = `SHT_CUDA_INFO`. The master section allocator handles global shared, per-entry shared, extern shared, reserved shared (`.nv.reservedSmem.begin/cap/offset0`), local memory, and OCG constant bank merging.

---

## 29. Capsule Mercury

| Address | Size | Name / Identity | Confidence | Callers | Notes |
|---------|------|----------------|------------|---------|-------|
| `0x1C9B110` | 4,585 B | `mercury_capsule_builder` | HIGH | 1 | Creates embedded .nv.merc ELF |
| `0x1C9C300` | 3,816 B | `capmerc_section_processor` | HIGH | 1 | .nv.capmerc, .merc, KNOBS data |
| `0x1CA2E40` | 3,152 B | `mercury_section_cloner` | HIGH | 1 | Duplicates sections into .nv.merc.* |
| `0x1CA3A90` | 6,289 B | `section_merger_emitter` | HIGH | 1 | Merge/combine pass for merc sections |

---

## Statistics

### Top 10 Most-Called Functions

| Rank | Address | Identity | Callers |
|------|---------|----------|---------|
| 1 | `0x7B9B80` | bitfield_insert | 18,347 |
| 2 | `0x10B6180` | 1-bit boolean encoder | 8,091 |
| 3 | `0x7BC030` | encode_register_operand | 6,147 |
| 4 | `0x4280C0` | get_thread_local_context | 3,928 |
| 5 | `0x42BDB0` | fatal_OOM_handler | 3,825 |
| 6 | `0x424070` | pool_alloc | 3,809 |
| 7 | `0x426150` | hashmap_put | 2,800 |
| 8 | `0x7B9D30` | clear_const_buffer_slots | 2,408 |
| 9 | `0x7B9D60` | encode_reuse_flags_predicate | 2,408 |
| 10 | `0x42FBA0` | diagnostic_emit | 2,350 |

### Top 5 Largest Functions

| Rank | Address | Identity | Size |
|------|---------|----------|------|
| 1 | `0x5FF700` | intrinsic_prototype_emitter | 354 KB |
| 2 | `0x169B190` | isel_pattern_dispatch | 280 KB |
| 3 | `0x198BCD0` | sm100_peephole_dispatch | 233 KB |
| 4 | `0x143C440` | sm120_peephole_dispatch | 233 KB |
| 5 | `0x5C7A50` | wmma_mma_codegen | 173 KB |

### Confidence Distribution

| Level | Count | Description |
|-------|-------|-------------|
| CERTAIN | ~25 | Named in symbols or strings |
| HIGH | ~140 | Strong evidence, >90% confidence |
| MEDIUM | ~30 | Structural analysis, 70-90% |
