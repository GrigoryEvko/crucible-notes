# Function Map

> **Binary**: ptxas v13.0.88, 37.7 MB stripped ELF, ~40,000 functions  
> **Documented**: 2,063 unique functions across 70 wiki pages  
> **This page**: Top ~100 most cross-referenced functions, plus routing tables  
> **Complete listings**: Each wiki page has its own Function Map section with full details

This page is the central lookup index for identified functions in ptxas. It lists the functions that appear most frequently across the wiki (cross-cutting infrastructure and major entry points), and provides routing tables to find any function by address range or subsystem.

**Confidence levels**: CERTAIN = named in symbols or strings. HIGH = strong evidence from strings and call patterns (>90%). MEDIUM = structural analysis with partial string evidence (70-90%).

---

## Core Infrastructure

These functions appear in 10+ wiki pages -- they are the universal building blocks called by nearly every subsystem.

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x424070` | `pool_alloc(pool, size)` | 19 | 3,809 | Custom slab allocator, 8-byte aligned |
| `0x4248B0` | `pool_free(ptr)` | 8 | 1,215 | Coalescing free, boundary tags |
| `0x4280C0` | `get_thread_local_context` | 10 | 3,928 | Most-called function in ptxas; 280-byte TLS struct |
| `0x42BDB0` | `fatal_OOM_handler` | 8 | 3,825 | Called on every allocation failure |
| `0x426150` | `hashmap_put(map, key, value)` | 11 | 2,800 | Open-addressing + chaining, auto-resize |
| `0x426D60` | `hashmap_get(map, key)` | 11 | 422 | Returns value or 0 |
| `0x425CA0` | `hashmap_create(hash_fn, cmp_fn, cap)` | 7 | 127 | Integer/pointer/custom hash modes |
| `0x427630` | `murmurhash3_x86_32(str)` | 5 | 73 | Constants: 0xcc9e2d51, 0x1b873593 |
| `0x42D850` | `hashset_insert(set, key)` | 4 | 282 | Hash set variant |
| `0x42FBA0` | `diagnostic_emit(desc, loc, fmt...)` | 7 | 2,350 | Central error/warning reporter |
| `0x42F590` | `fatal_internal_error(desc, ...)` | 8 | 3,825 | Assertion handler |
| `0x4279D0` | `starts_with(str, prefix)` | 4 | 185 | Returns suffix pointer or 0 |
| `0x42CA60` | `list_push_front(node, head_ptr)` | 4 | 298 | Pool-allocated linked list |
| `0xBDBA60` | `bitvector_allocate` | 8 | many | (bits+31)>>5 word count |
| `0xBDCDE0` | `bitvector_or_assign` (SSE2) | 5 | many | `_mm_or_si128` on 128-bit chunks |

> **Details**: [Memory Pools](infra/memory-pools.md), [Hash & Bitvector](infra/hash-bitvector.md), [Threading](infra/threading.md)

---

## Compilation Driver & CLI

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x409460` | `main` | 5 | 1 | Delegates to `0x446240` |
| `0x446240` | `real_main` (top-level driver) | 13 | 1 | Orchestrates entire pipeline |
| `0x4428E0` | `ptx_input_setup` | 6 | 1 | Version/target validation |
| `0x43CC70` | `per_entry_compile_unit` | 5 | 1 | Processes each entry through pipeline |
| `0x43F400` | `function_abi_config` | 4 | 1 | Parameter regs, return addr, scratch |
| `0x43A400` | `compilation_target_config` | 7 | 1 | SM-specific defaults |
| `0x43B660` | `register_constraint_calculator` | 5 | 1 | Balances .maxnreg, occupancy |
| `0x432A00` | `option_registration` | 9 | 1 | CLI option definitions |
| `0x434320` | `option_parser` | 9 | 1 | Validates combinations, applies state |

> **Details**: [Pipeline Entry](pipeline/entry.md), [Pipeline Overview](pipeline/overview.md), [CLI Options](config/cli-options.md)

---

## PTX Front End

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x46E000` | `instruction_table_builder` | 9 | 1 | 93 KB, 1168 callees, one per PTX opcode |
| `0x451730` | `parser_setup` (special register init) | 9 | 1 | %ntid, %laneid, %clock, etc. |
| `0x4CE6B0` | `bison_parser` (directive/decl) | 7 | 1 | .local_maxnreg, .alias, .pragma |
| `0x720F00` | `flex_lexer` (ptxlex / yylex) | 8 | 2 | ~550 Flex rules, DFA scanner |
| `0x4B2F20` | `ptx_validator_general` | 4 | 1 | Validates texture, surface, cvt, call |
| `0x4C5FB0` | `ptx_validator_mma_wmma_tcgen05` | 4 | 1 | MMA, WMMA, tensor core validation |
| `0x71F630` | `preprocessor_dispatch` | 4 | 1 | .MACRO, .ELSE, .INCLUDE |
| `0x489050` | `ptx_to_ori_converter` | 5 | 1 | PTX AST to ORI IR translation |

> **Details**: [PTX Parser](pipeline/ptx-parser.md), [PTX Directives](pipeline/ptx-directives.md), [PTX to ORI](pipeline/ptx-to-ori.md)

---

## Static Initialization

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x4094C0` | `ctor_001` -- thread infra init | 4 | 0 | pthread_key_create, mutex |
| `0x4095D0` | `ctor_003` -- PTX opcode name table | 6 | 0 | ~900 ROT13-encoded PTX mnemonics |
| `0x40D860` | `ctor_005` -- tuning knob registry | 6 | 0 | 80 KB, 2000+ ROT13 knob names |
| `0x421290` | `ctor_007` -- scheduler knob registry | 4 | 0 | 98 ROT13 scheduler knobs |

> **Details**: [Pipeline Entry](pipeline/entry.md), [Binary Layout](binary-layout.md)

---

## Phase Manager & Optimization Framework

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0xC60D30` | `phase_factory` (159-case switch) | 12 | 1 | Allocates phase objects |
| `0xC62720` | `PhaseManager_ctor` | 10 | 2 | 159-entry phase table |
| `0xC64F70` | `phase_dispatch_loop` | 5 | 2 | Executes phases, reports timing |
| `0xC64310` | `per_phase_timing_reporter` | 5 | 1 | "[Total N KB] [Freeable N KB]" |
| `0xC641D0` | `phase_name_to_index_lookup` | 5 | 3 | Binary search, case-insensitive |
| `0x7DDB50` | `phase_run_dispatch` | 14 | many | Vtable-based phase execution |
| `0x9F4040` | `NamedPhases_parse_and_build` | 6 | 1 | "shuffle", "OriCopyProp", etc. |
| `0x798B60` | `NamedPhases_parser` | 4 | 2 | PTXAS_DISABLE env var parsing |
| `0x799250` | `IsPassDisabled` | 5 | 4 | Checks knob index 185 |
| `0xA36360` | `pass_sequence_builder` | 6 | 1 | Constructs NvOptRecipe pass list |

> **Details**: [Phase Manager](passes/phase-manager.md), [Pass Inventory](passes/index.md), [Optimizer Pipeline](pipeline/optimizer.md)

---

## ORI IR & Instruction Access

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x9253C0` | `instruction_operand_get` | 11 | many | Operand accessor on ORI instructions |
| `0x7E6090` | `instruction_modifier_set` | 10 | many | IR modification helper |
| `0x781F80` | `instruction_iterator` | 12 | many | Doubly-linked list traversal |
| `0x7DF3A0` | `instruction_property_query` | 5 | many | Instruction flag/attribute checker |
| `0x91BF30` | `register_type_query` | 8 | many | Register class/type inspection |
| `0x9314F0` | `register_class_id_query` | 7 | 1,547 | Most-called non-trivial regalloc fn |
| `0x931920` | `register_class_compat_checker` | 6 | 328 | Pair register class handling |
| `0x934630` | `register_id_packer` | 9 | 856 | Packs reg#/class/type into 32-bit |
| `0xB28E00` | `ir_node_type_query` | 5 | many | Node kind discrimination |
| `0xB28E90` | `ir_node_field_accessor` | 6 | many | Generic field getter |
| `0xA50650` | `CodeObject_EmitRecords` | 1 | 8 | 74 KB, ORI record serializer (56 section types) |
| `0xA53840` | `EmitRecords_wrapper` | 1 | 1 | Thin wrapper, adds type-44 header |

> **Details**: [Instructions](ir/instructions.md), [Registers](ir/registers.md), [Data Structures](ir/data-structures.md), [CFG](ir/cfg.md)

---

## Intrinsic Infrastructure

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x5D1660` | `intrinsic_table_register` (608 entries) | 7 | 1 | Master name-to-ID table |
| `0x5D4190` | `intrinsic_dispatch_builder` | 13 | 1 | PTX opcode -> codegen handler mapping |
| `0x5FF700` | `intrinsic_prototype_emitter` | 5 | 1 | 354 KB -- largest function in binary |
| `0x5C7A50` | `wmma_mma_codegen` | 4 | 1 | 173 KB, all shapes/types/layouts |
| `0x5C10A0` | `mma_codegen` (mma.sync) | 4 | 1 | 120 KB, m8n8k4 through m16n8k256 |
| `0x5BBC30` | `tcgen05_mma_codegen` (Blackwell) | 5 | 1 | 90 KB, 5th-gen tensor core |
| `0x70FA00` | `ocg_intrinsic_handler` | 8 | 1 | OCG-level intrinsic routing |
| `0x6A97B0` | `intrinsic_lowering_main` | 4 | 1 | 26 KB, switch-based lowering |
| `0x6C9EB0` | `ocg_builtin_name_lookup` | 5 | 1 | Blackwell+ OCG name table |

> **Details**: [Intrinsics Index](intrinsics/index.md), [Math Intrinsics](intrinsics/math.md), [Tensor Intrinsics](intrinsics/tensor.md), [Sync & Warp](intrinsics/sync-warp.md)

---

## Register Allocator

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x9721C0` | `regalloc_entry` ("REGALLOC GUIDANCE") | 6 | 1 | Top-level allocator entry |
| `0x957160` | `fatpoint_allocator_core` | 7 | 1 | Core fatpoint graph coloring |
| `0x96D940` | `spill_guidance_engine` | 5 | 1 | Determines spill strategy |
| `0x971A90` | `full_alloc_with_spill_retry` | 4 | 1 | "NOSPILL REGALLOC" path |
| `0x9714E0` | `regalloc_failure_reporter` | 6 | 1 | "Register allocation failed..." |
| `0x926A30` | `interference_graph_builder` | 9 | 7 | 22 KB, SSE bitvectors |
| `0x92C240` | `liveness_bitvector_ops` | 5 | 87 | Set/clear/query with aliasing |
| `0x917A60` | `opcode_to_regclass_mapping` | 4 | 221 | Massive switch |
| `0x910840` | `ConvertMemoryToRegisterOrUniform` | 5 | 1 | Pass driver |

> **Details**: [RegAlloc Overview](regalloc/overview.md), [RegAlloc Algorithm](regalloc/algorithm.md), [Spilling](regalloc/spilling.md), [ABI](regalloc/abi.md)

---

## Instruction Scheduling

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x8D0640` | `ScheduleInstructions` (top-level) | 7 | 1 | String: "ScheduleInstructions" |
| `0x688DD0` | `scheduler_engine` (main BB loop) | 5 | 1 | ReduceReg / DynBatch selection |
| `0x8C9320` | `scheduling_priority_function` | 4 | 0 | ~300 locals, core heuristic |
| `0x68B9C0` | `dependency_graph_builder` | 4 | 1 | RAW/WAR/WAW hazard analysis |
| `0x6820B0` | `build_ready_list` | 5 | 1 | Zero-dependency instructions |
| `0x8CD6E0` | `reverse_scheduling_driver` | 4 | 1 | Reverse post-order iteration |
| `0x8CEE80` | `register_budget_with_occupancy` | 4 | 1 | Pressure coeff default 0.045 |
| `0x8E4400` | `hw_profile_table_init` | 6 | 3 | Encoding/latency property tables |
| `0xA9CDE0` | `scheduling_metadata_builder` | 6 | 1 | Per-instruction sched metadata |
| `0xA9CF90` | `scheduling_metadata_accessor` | 5 | many | Sched metadata field queries |
| `0xAED3C0` | `scheduling_optimization_mega_pass` | 4 | 0 | 137 KB, ~560 locals, largest vtable pass |

> **Details**: [Scheduling Overview](scheduling/overview.md), [Scheduling Algorithm](scheduling/algorithm.md), [Latency Model](scheduling/latency-model.md), [Scoreboards](scheduling/scoreboards.md)

---

## Codegen & ISel

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x169B190` | `isel_pattern_dispatch` (master) | 5 | 1 | 280 KB, 65,999 insns -- largest function |
| `0x143C440` | `sm120_peephole_dispatch` | 4 | 1 | SM120 (RTX 50), 373-case switch |
| `0x198BCD0` | `sm100_peephole_dispatch` | 4 | 1 | SM100 (Blackwell), 1336 callees |
| `0x83EF00` | `main_peephole_pass` | 6 | 0 | 29 KB, 392 callees |
| `0x6D9690` | `master_instruction_encoder` | 7 | 1 | 94 KB, opcode switch |
| `0x6E4110` | `sass_codegen_main` | 4 | 1 | EmitSASSForFunction, FNV-1a BB hash |
| `0x6F52F0` | `SASS_pipeline_run_stages` | 5 | 1 | Mercury SASS compilation pipeline |
| `0x9ED2D0` | `MercConverter_entry` | 6 | 1 | ORI to Mercury IR conversion |
| `0x9F1A90` | `MercConverter_builder` | 6 | 1 | Mercury instruction construction |

> **Details**: [ISel](codegen/isel.md), [Encoding](codegen/encoding.md), [Peephole](codegen/peephole.md), [Mercury](codegen/mercury.md), [Templates](codegen/templates.md)

---

## Bitfield Encoding

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x7B9B80` | `bitfield_insert(insn, off, wid, val)` | 9 | **18,347** | Most-called by caller count |
| `0x7BC030` | `encode_register_operand` | 4 | 6,147 | 1-bit + 4-bit type + 10-bit reg |
| `0x7B9D60` | `encode_reuse_flags_predicate` | 4 | 2,408 | 1-bit reuse + 5-bit predicate |
| `0x7BC5C0` | `encode_immediate_const_operand` | 4 | 1,449 | Const buffer index or immediate |
| `0x7BCF00` | `encode_predicate_register` | 4 | 1,657 | PT=14, 2-bit type + 3-bit condition |
| `0x10B6180` | `1_bit_boolean_encoder` | 3 | 8,091 | .S/.U, .STRONG, etc. |

> **Details**: [Encoding](codegen/encoding.md), [SASS Printing](codegen/sass-printing.md)

---

## ELF / CUBIN Output

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x612DE0` | `section_attr_builder` | 11 | 1 | 76 KB, ELF section/attribute config |
| `0x1C9F280` | `master_elf_emitter` | 9 | 1 | Complete CUBIN assembly |
| `0x1CB53A0` | `elf_world_init` | 7 | 1 | 672-byte ELFW context |
| `0x1CB68D0` | `symbol_table_builder` | 5 | 1 | .symtab from internal symbols |
| `0x1CABD60` | `master_section_allocator` | 5 | 1 | Shared/const/local memory |
| `0x1CB3570` | `add_function_section` | 5 | 44 | Creates .text.FUNCNAME + .rela |
| `0x1CD48C0` | `relocation_processor` | 5 | 1 | Relocation section emission |
| `0x1C9B110` | `mercury_capsule_builder` | 4 | 1 | Creates embedded .nv.merc ELF |

> **Details**: [ELF Emitter](output/elf-emitter.md), [Sections](output/sections.md), [Relocations](output/relocations.md), [Debug Info](output/debug-info.md), [Capsule Mercury](codegen/capmerc.md)

---

## Knobs System

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x79B240` | `GetKnobIndex` | 6 | 2 | ROT13 name lookup, case-insensitive |
| `0x79D070` | `ReadKnobsFile` | 5 | 1 | Parses `[knobs]` section from file |
| `0x79F540` | `ParseKnobValue` | 4 | 1 | 12-type switch: bool/int/float/string/... |
| `0x79D990` | `ProcessKnobs` (top-level) | 4 | 1 | File + pragma + numbered config |
| `0xA0F020` | `knob_conditional_evaluator` | 5 | many | `[WHEN condition]` handler |

> **Details**: [Knobs](config/knobs.md), [Opt Levels](config/opt-levels.md)

---

## Target-Specific Code

| Address | Identity | Pages | Callers | Notes |
|---------|----------|:-----:|--------:|-------|
| `0x6765E0` | `target_profile_selector` | 7 | 1 | SM-dependent profile dispatch |
| `0x607DB0` | `target_feature_query` | 7 | many | SM feature capability checks |
| `0x896D50` | `sass_mnemonic_table_init` (ROT13) | 4 | 1 | ~400+ SASS instruction names |
| `0x89FBA0` | `instruction_latency_init` | 4 | 3 | Encoding/latency property tables |

> **Details**: [Targets Index](targets/index.md), [Turing-Ampere](targets/turing-ampere.md), [Ada-Hopper](targets/ada-hopper.md), [Blackwell](targets/blackwell.md), [tcgen05](targets/tcgen05.md)

---

## Subsystem Routing Table

To find a specific function, locate it by address range or subsystem topic in this table. Each page contains a detailed Function Map section with complete listings.

### By Subsystem Topic

| Subsystem | Primary Pages | Functions |
|-----------|---------------|----------:|
| Memory allocator, pools | [memory-pools.md](infra/memory-pools.md) | 30 |
| Hash maps, bitvectors, sets | [hash-bitvector.md](infra/hash-bitvector.md) | 51 |
| Threading, TLS, jobserver | [threading.md](infra/threading.md) | 41 |
| CLI parsing, option handling | [cli-options.md](config/cli-options.md) | 17 |
| Tuning knobs (2000+ knobs) | [knobs.md](config/knobs.md) | 56 |
| Optimization levels | [opt-levels.md](config/opt-levels.md) | 14 |
| DumpIR debug output | [dumpir.md](config/dumpir.md) | 14 |
| Compilation pipeline | [overview.md](pipeline/overview.md), [entry.md](pipeline/entry.md) | 56+25 |
| PTX lexer & parser | [ptx-parser.md](pipeline/ptx-parser.md) | 75 |
| PTX directives | [ptx-directives.md](pipeline/ptx-directives.md) | 41 |
| PTX-to-ORI translation | [ptx-to-ori.md](pipeline/ptx-to-ori.md) | 41 |
| Optimizer pipeline | [optimizer.md](pipeline/optimizer.md) | 28 |
| ORI instruction IR | [instructions.md](ir/instructions.md) | 80 |
| CFG construction | [cfg.md](ir/cfg.md) | 18 |
| Register representation | [registers.md](ir/registers.md) | 40 |
| IR data structures | [data-structures.md](ir/data-structures.md) | 74 |
| Phase manager (159 phases) | [phase-manager.md](passes/phase-manager.md) | 26 |
| Copy propagation, CSE, GVN | [copy-prop-cse.md](passes/copy-prop-cse.md) | 65 |
| General optimization passes | [general-optimize.md](passes/general-optimize.md) | 71 |
| Loop optimization (unroll, LICM, SWP) | [loop-passes.md](passes/loop-passes.md) | 92 |
| Branch/switch optimization | [branch-switch.md](passes/branch-switch.md) | 24 |
| Strength reduction | [strength-reduction.md](passes/strength-reduction.md) | 25 |
| Predication | [predication.md](passes/predication.md) | 28 |
| Rematerialization | [rematerialization.md](passes/rematerialization.md) | 55 |
| Liveness analysis | [liveness.md](passes/liveness.md) | 42 |
| Sync barriers | [sync-barriers.md](passes/sync-barriers.md) | 66 |
| Late legalization | [late-legalization.md](passes/late-legalization.md) | 59 |
| Hot/cold splitting | [hot-cold.md](passes/hot-cold.md) | 10 |
| GMMA pipelining | [gmma-pipeline.md](passes/gmma-pipeline.md) | 47 |
| Uniform registers | [uniform-regs.md](passes/uniform-regs.md) | 22 |
| Register allocator core | [algorithm.md](regalloc/algorithm.md) | 50 |
| Spilling | [spilling.md](regalloc/spilling.md) | 54 |
| ABI handling | [abi.md](regalloc/abi.md) | 87 |
| Scheduling overview | [overview.md](scheduling/overview.md) | 112 |
| Scheduling algorithm | [algorithm.md](scheduling/algorithm.md) | 121 |
| Latency model & HW profiles | [latency-model.md](scheduling/latency-model.md) | 78 |
| Scoreboards & barriers | [scoreboards.md](scheduling/scoreboards.md) | 56 |
| ISel pattern matching | [isel.md](codegen/isel.md) | 182 |
| SASS encoding | [encoding.md](codegen/encoding.md) | 92 |
| Peephole optimization | [peephole.md](codegen/peephole.md) | 67 |
| Mercury IR conversion | [mercury.md](codegen/mercury.md) | 79 |
| SASS templates | [templates.md](codegen/templates.md) | 46 |
| SASS printing / renderer | [sass-printing.md](codegen/sass-printing.md) | 96 |
| Capsule Mercury | [capmerc.md](codegen/capmerc.md) | 20 |
| Intrinsic infrastructure | [index.md](intrinsics/index.md) | 159 |
| Math intrinsics | [math.md](intrinsics/math.md) | 42 |
| Tensor core intrinsics | [tensor.md](intrinsics/tensor.md) | 45 |
| Sync & warp intrinsics | [sync-warp.md](intrinsics/sync-warp.md) | 65 |
| SM targets & features | [index.md](targets/index.md) | 70 |
| ELF emitter | [elf-emitter.md](output/elf-emitter.md) | 29 |
| ELF sections | [sections.md](output/sections.md) | 33 |
| Debug info (DWARF) | [debug-info.md](output/debug-info.md) | 33 |
| Relocations | [relocations.md](output/relocations.md) | 19 |

### By Address Range

Functions in the binary are clustered by subsystem. This table maps address ranges to the pages that document them.

| Address Range | Primary Subsystem | Key Pages |
|---------------|-------------------|-----------|
| `0x400000`-`0x424000` | Entry, static init, main | [entry.md](pipeline/entry.md), [binary-layout.md](binary-layout.md) |
| `0x424000`-`0x42E000` | Memory pools, hash maps, lists | [memory-pools.md](infra/memory-pools.md), [hash-bitvector.md](infra/hash-bitvector.md) |
| `0x42E000`-`0x446000` | Diagnostics, CLI parsing | [cli-options.md](config/cli-options.md), [entry.md](pipeline/entry.md) |
| `0x446000`-`0x452000` | Compilation driver | [overview.md](pipeline/overview.md), [entry.md](pipeline/entry.md) |
| `0x452000`-`0x4D5000` | PTX parser & validator | [ptx-parser.md](pipeline/ptx-parser.md), [ptx-directives.md](pipeline/ptx-directives.md) |
| `0x4D5000`-`0x5AA000` | PTX-to-ORI, early IR | [ptx-to-ori.md](pipeline/ptx-to-ori.md), [instructions.md](ir/instructions.md) |
| `0x5AA000`-`0x612000` | Intrinsic infrastructure | [index.md](intrinsics/index.md), [math.md](intrinsics/math.md), [tensor.md](intrinsics/tensor.md) |
| `0x612000`-`0x67F000` | Section builder, target config | [sections.md](output/sections.md), [index.md](targets/index.md) |
| `0x67F000`-`0x6E4000` | Scheduling engine, OCG lowering, encoding | [overview.md](scheduling/overview.md), [encoding.md](codegen/encoding.md) |
| `0x6E4000`-`0x754000` | SASS codegen, SASS pipeline | [mercury.md](codegen/mercury.md), [overview.md](pipeline/overview.md) |
| `0x754000`-`0x7C0000` | Liveness, knobs, bitfield encoding | [liveness.md](passes/liveness.md), [knobs.md](config/knobs.md), [encoding.md](codegen/encoding.md) |
| `0x7C0000`-`0x8FE000` | Peephole, SASS mnemonics, scheduling upper | [peephole.md](codegen/peephole.md), [algorithm.md](scheduling/algorithm.md) |
| `0x8FE000`-`0x9D3000` | Register allocator | [overview.md](regalloc/overview.md), [algorithm.md](regalloc/algorithm.md), [abi.md](regalloc/abi.md) |
| `0x9D3000`-`0xAA8000` | Post-regalloc, named phases, remat | [rematerialization.md](passes/rematerialization.md), [phase-manager.md](passes/phase-manager.md) |
| `0xAA8000`-`0xC52000` | Mega-passes, sync barriers, dataflow | [sync-barriers.md](passes/sync-barriers.md), [general-optimize.md](passes/general-optimize.md) |
| `0xC52000`-`0xD27000` | Phase manager, phase factory | [phase-manager.md](passes/phase-manager.md), [optimizer.md](pipeline/optimizer.md) |
| `0xD27000`-`0x10B7000` | 592 SASS encoder bodies | [encoding.md](codegen/encoding.md), [isel.md](codegen/isel.md) |
| `0x10B7000`-`0x1225000` | Field encoders, ISel helpers | [encoding.md](codegen/encoding.md), [isel.md](codegen/isel.md) |
| `0x1225000`-`0x13CF000` | Bitvector, ISel coordinators | [hash-bitvector.md](infra/hash-bitvector.md), [isel.md](codegen/isel.md) |
| `0x13CF000`-`0x17F8000` | SM-specific ISel, pattern matchers, templates | [isel.md](codegen/isel.md), [templates.md](codegen/templates.md) |
| `0x17F8000`-`0x1C21000` | SASS printing, peephole mega-dispatchers | [sass-printing.md](codegen/sass-printing.md), [peephole.md](codegen/peephole.md) |
| `0x1C21000`-`0x1CE3000` | ELF emitter, capsule mercury, relocations | [elf-emitter.md](output/elf-emitter.md), [capmerc.md](codegen/capmerc.md) |

---

## Statistics

### Top 10 Most-Called Functions

| Rank | Address | Identity | Callers |
|------|---------|----------|--------:|
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
|------|---------|----------|-----:|
| 1 | `0x5FF700` | intrinsic_prototype_emitter | 354 KB |
| 2 | `0x169B190` | isel_pattern_dispatch | 280 KB |
| 3 | `0x198BCD0` | sm100_peephole_dispatch | 233 KB |
| 4 | `0x143C440` | sm120_peephole_dispatch | 233 KB |
| 5 | `0x5C7A50` | wmma_mma_codegen | 173 KB |

### Top 10 Most Cross-Referenced (by wiki page count)

| Rank | Address | Identity | Pages |
|------|---------|----------|------:|
| 1 | `0x424070` | pool_alloc | 19 |
| 2 | `0x7DDB50` | phase_run_dispatch | 14 |
| 3 | `0x446240` | real_main | 13 |
| 3 | `0x5D4190` | intrinsic_dispatch_builder | 13 |
| 5 | `0x781F80` | instruction_iterator | 12 |
| 5 | `0xC60D30` | phase_factory | 12 |
| 7 | `0x9253C0` | instruction_operand_get | 11 |
| 7 | `0x612DE0` | section_attr_builder | 11 |
| 7 | `0x426150` | hashmap_put | 11 |
| 7 | `0x426D60` | hashmap_get | 11 |

### Documentation Coverage

| Metric | Count |
|--------|------:|
| Total unique functions documented | 2,063 |
| Wiki pages with function maps | 70 |
| Functions in 5+ pages (high cross-reference) | 89 |
| Functions in 1 page only (subsystem-internal) | 1,324 |
| Confidence CERTAIN | ~40 |
| Confidence HIGH | ~1,400 |
| Confidence MEDIUM | ~620 |
