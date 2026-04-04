# Function Map

Address-to-identity lookup for ~140 key functions across 20 subsystems in `nvlink` v13.0.88 (26.2 MB, ~40,000 total functions). Confidence: **VERY HIGH** = string/symbol evidence, **HIGH** = strong structural evidence, **MEDIUM** = inferred from context/callgraph.

Binary: `/usr/local/cuda-13.0/bin/nvlink`  
SHA256: see [versions](versions.md)  
Entry point: `0x409800` (main)

---

## Top 20 Most-Called Functions

Functions with the highest cross-reference count in the binary. These form the backbone of every subsystem.

| # | Address | Proposed Name | Callers | Size | Role |
|---|---------|---------------|---------|------|------|
| 1 | `0x530FB0` | IRNode_GetOperand | 31,399 | 16B | Return pointer to operand at index (32-byte stride) |
| 2 | `0xA49150` | NVInst_getOperandField | 30,768 | 60B | Query instruction attribute by field ID; dispatches to `0xA7DE70` + `0xA709F0` |
| 3 | `0x4307C0` | arena_alloc | ~10K+ | 10.7KB | Thread-safe arena allocator (small-block free-list + large-block pages) |
| 4 | `0x431000` | arena_free | ~10K+ | 4.7KB | Arena deallocator, returns blocks to size-class free-lists |
| 5 | `0x530E90` | IROperand_IsRegister | ~5K+ | 16B | `return type_tag == 2` |
| 6 | `0x530FC0` | IRNode_GetNumSrcOperands | ~5K+ | 16B | `total_ops + 1 - first_src_index` |
| 7 | `0x530FD0` | IRNode_GetNumDstOperands | ~5K+ | 16B | `return *(a1 + 92)` |
| 8 | `0x530EA0` | IROperand_IsImmediate | ~3K+ | 16B | `return type_tag == 1` |
| 9 | `0x530E80` | IRNode_GetRegClass | ~3K+ | 16B | Identity function / unsigned int extract |
| 10 | `0xA50D10` | encode_GPR | ~3K+ | tiny | Encode register number for destination field |
| 11 | `0x467460` | error_emit | ~2K+ | ~2KB | Variadic error emission (dispatches to `0x467A70`) |
| 12 | `0x448360` | elfw_get_section_header | ~2K+ | <2KB | Section header accessor |
| 13 | `0x44F410` | arena_get_metadata | ~2K+ | <2KB | Look up allocation metadata for a pointer |
| 14 | `0x45CAC0` | oom_handler | ~1K+ | tiny | Out-of-memory handler, calls abort path |
| 15 | `0x45CAE0` | arena_assert | ~1K+ | tiny | Arena validity assertion |
| 16 | `0x4C28B0` | setBitfield | ~1K+ | small | Core bitfield insertion into instruction word |
| 17 | `0x50C790` | getReuse | ~1K+ | small | Read 1-bit reuse flag from encoded instruction |
| 18 | `0x530F80` | IRNode_GetDataType | ~1K+ | 16B | Identity function for data type field at +20 |
| 19 | `0x4489C0` | hash_table_create | ~500+ | small | Create hash table for option/symbol lookup |
| 20 | `0x464460` | linked_list_append | ~500+ | small | Append node to singly-linked list |

---

## A. Main Program Flow

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x409800` | **main** | 58KB | VERY HIGH | Complete nvlink entry point. Parses options, dispatches by input type (cubin/ptx/fatbin/nvvm/ltoir/bc/.o/.so/.a), drives merge/layout/relocate/finalize/write pipeline, handles Mercury post-link, host linker script generation. |
| `0x427AE0` | nvlink_parse_options | 30KB | VERY HIGH | Registers ~60 CLI options via `0x42F130`, extracts all into globals. Validates arch (sm > 19), Mercury mode (sm > 99), LTO constraints. |
| `0x4275C0` | post_link_transform | 4KB | HIGH | FNLZR (Finalizer) entry for Mercury/SASS. Strings: "FNLZR: Post-Link Mode", "FNLZR: Pre-Link Mode". |
| `0x45CCD0` | timing_start | tiny | HIGH | Begin profiling timer for phase tracing. |
| `0x4279C0` | trace_phase | tiny | HIGH | Debug trace with phase names: "init", "read", "merge", "layout", "relocate", "finalize", "write". |

---

## B. Command-Line Option Parsing

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x42DFE0` | option_parser_create | 4.5KB | HIGH | Allocates 56-byte parser struct, creates two hash tables for option lookup. |
| `0x42F130` | option_register | 4.9KB | HIGH | Registers single option (120-byte entry): name, short name, type (1=bool/2=string/4=int/0=file), multiplicity, default, help. Called ~60 times. |
| `0x42E5A0` | option_parse_argv | 9.5KB | HIGH | Iterates argv, matches against registered options, handles `--`, `=`, response files `@file`. |
| `0x42E390` | option_get_value | 2.9KB | HIGH | Extracts parsed option value (1/4/8 byte) into destination variable. Called ~80 times. |
| `0x42D700` | option_format_help | 5.6KB | MEDIUM | Formats single option help entry with defaults, keywords, allowed values. |
| `0x42DBC0` | option_validate_value | 5.1KB | MEDIUM | Validates option value against type constraints ("32-bit integer", "64-bit hex", etc.). |

---

## C. Memory Arena / Allocator

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4307C0` | arena_alloc | 10.7KB | HIGH | Thread-safe (per-arena mutex). Small path: 625 free-list buckets at arena+2128 (8-byte aligned). Large path: page pool. Falls back to mmap via `0x44ED60`. |
| `0x431000` | arena_free | 4.7KB | HIGH | Returns small blocks to free-list, large blocks to page pool. Checks `byte_2A5BAD0` debug flag. |
| `0x432020` | arena_create_named | 2.2KB | HIGH | Creates named arena. Called with "nvlink option parser", "nvlink memory space". |
| `0x431C70` | arena_destroy | 3.6KB | HIGH | Optionally merges free-lists back into parent arena, or frees all pages via `0x431EC0`. |
| `0x431770` | arena_dump_stats | 8.5KB | HIGH | Prints detailed arena statistics: page counts, block sizes, usage. |
| `0x4882A0` | ocg_memspace_alloc | 2.5KB | HIGH | OCG (On-Chip-Gen) slab/segregated-freelist allocator. 128 size-class buckets, 1MB page allocations. |
| `0x489140` | memspace_statistics_print | 4.4KB | HIGH | Prints "Memory space statistics for 'OCG mem space'". |

---

## D. ELF Structure Management (elfw)

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4438F0` | **elfw_create** | 14.8KB | HIGH | Creates ELF wrapper with sections: `.note.nv.cuinfo`, `.note.nv.tkinfo`, `.shstrtab`, `.strtab`, `.symtab`. Creates "elfw memory space" arena. |
| `0x440740` | elfw_add_section | 5.4KB | HIGH | Adds new section to ELF wrapper. |
| `0x440BE0` | elfw_add_section_with_data | 7.0KB | HIGH | Adds section with initial data copy. |
| `0x441AC0` | elfw_add_reloc_section | 9.5KB | HIGH | Creates `.rela%s` / `.rel%s` relocation sections. |
| `0x442CA0` | elfw_add_symbol | 7.2KB | HIGH | Adds symbol (STB_GLOBAL/WEAK/LOCAL), updates callgraph for function symbols. |
| `0x442820` | elfw_merge_symbols | 5.4KB | HIGH | Merges unified symbols including `__cuda_uf_stub_` / `.nv.uft` stubs. |
| `0x4411F0` | elfw_copy_section | 12.2KB | HIGH | Deep copy of section data, symbols, relocations between elfw objects. |
| `0x4478F0` | elfw_dump_structure | 15.1KB | HIGH | Debug dump of ELF wrapper state: sections, symbols, relocations. |
| `0x448E70` | elfw_section_table_build | 14.6KB | MEDIUM | Rebuilds section header table, computes offsets/sizes for final layout. |
| `0x4475B0` | elfw_destroy | 3.0KB | HIGH | Destroys ELF wrapper and frees associated arena. |

---

## E. Merge Engine

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x45E7D0` | **merge_elf** | 89KB | VERY HIGH | Heart of the linker. Iterates all sections of input ELF, merges symbols/relocations/data. Handles `.nv.global`, `.nv.shared`, `.nv.constant`, `.nv.info`, DWARF debug. 450+ locals. |
| `0x45D180` | merge_weak_function | 26.8KB | HIGH | Resolves weak function conflicts by comparing register counts and PTX versions. |
| `0x42AF40` | extract_fatbin_member | 11.1KB | HIGH | Extracts object from fatbin. Dispatches by type: 1=ptx, 8=nvvm, 16=mercury/capmerc, default=cubin. |
| `0x42A680` | register_module | 11.9KB | HIGH | Registers linked module with module_id extracted from ELF via `0x46F0C0`. |
| `0x426570` | validate_arch_and_merge | 7.4KB | HIGH | Validates cubin architecture matches target ("compute_%d%c", "sm_%d%c"). |

---

## F. Shared Memory Layout

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x439830` | **shared_memory_layout** | 66KB | VERY HIGH | Allocates and lays out all shared memory: global, extern, local, reserved. Computes overlapping set analysis. Handles `.nv.reservedSmem.*`. |
| `0x436BD0` | shared_memory_optimizer | 15.7KB | HIGH | Builds interference graph, groups non-overlapping shared variables to reduce total shared memory usage. |
| `0x438640` | merge_constant_bank_data | 4.0KB | HIGH | Merges data into constant memory banks. Validates `bank SHT not CUDA_CONSTANT_?`. |
| `0x438DD0` | process_bindless_references | 12.8KB | HIGH | Handles bindless texture/surface relocations. Creates `$NVLINKBINDLESSOFF_%s` synthetic symbols. |

---

## G. Callgraph & Dead Code Elimination

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x44AD40` | dead_code_elimination | 22.5KB | HIGH | DFS reachability on callgraph, removes unreachable functions and associated `.nv.local`/`.nv.shared` sections. Keeps address-taken functions. |
| `0x44A5D0` | callgraph_detect_recursion | 14.4KB | HIGH | DFS-based recursion detection for stack size requirements. |
| `0x44C030` | callgraph_traverse | 10.2KB | HIGH | Propagates stack sizes and register counts through call chains. |
| `0x44CCF0` | callgraph_dump_dot | small | HIGH | Writes Graphviz DOT format via `digraph callgraph { %s -> %s; }`. |
| `0x44D200` | build_callgraph_section | 8.5KB | HIGH | Generates `.nv.callgraph` section in output ELF. |

---

## H. Data Overlap / Constant Optimization

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x432B10` | merge_overlapping_global | 11.7KB | HIGH | Validates overlapping symbol definitions in `.nv.global` contain identical data. |
| `0x437E20` | merge_overlapping_local | 11.6KB | HIGH | Same pattern for `.nv.local.*` sections. |
| `0x4343C0` | merge_overlapping_constant | 11.8KB | HIGH | Same pattern for `.nv.constant*` sections. |
| `0x4339A0` | optimize_constant_dedup | 13.2KB | HIGH | Deduplicates constant values: "found duplicate value 0x%x, alias %s to %s". Handles 32-bit and 64-bit. |

---

## I. Relocation Engine

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x469D60` | **apply_relocations** | 26.6KB | VERY HIGH | Complete relocation resolution. Handles `__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT`, `__UFT`. Processes `.nv.resolvedrela`. |
| `0x46ADC0` | emit_resolved_relocations | 11.5KB | HIGH | Creates `.nv.resolvedrela` section when `--preserve-relocs`. |
| `0x459640` | reloc_vtable_create | 16.1KB | HIGH | Creates 632-byte vtable with ~70 handler slots, dispatched per arch generation (sm30..sm100+). |

---

## J. Finalization & Output

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x445000` | **finalize_elf** | 56KB | VERY HIGH | Final relocation application and ELF finalization. Architecture-specific relocation encodings, symbol address resolution, final section content generation. |
| `0x451D80` | compute_entry_properties | 98KB | HIGH | Largest function in the linker core. Computes per-kernel register counts, stack sizes, barrier counts. Processes unified function tables. Propagates through callgraph. 500+ locals. |
| `0x450ED0` | propagate_register_counts | 16KB | HIGH | Propagates register/barrier counts from callees to callers. Creates `EIATTR_NUM_BARRIERS`. |
| `0x45C920` | write_elf_to_file | small | HIGH | Wrapper calling `0x45BF00` to serialize ELF to file. |
| `0x45C950` | write_elf_to_memory | small | HIGH | Wrapper calling `0x45BF00` to serialize ELF to buffer. |
| `0x45BF00` | write_elf_to_buffer | 13.3KB | HIGH | Serializes ELF header, program headers, section headers, section data. Validates sizes. |

---

## K. Unified Table (UDT/UFT) Management

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4637B0` | uft_reorder_entries | 10.1KB | HIGH | Reorders unified function/descriptor table entries. UUID-based mapping: "map uid <%llx,%llx> to key=%llx". |
| `0x463F70` | uft_setup_sections | 4.0KB | HIGH | Creates/validates `.nv.udt`, `.nv.uft`, `.nv.uft.entry`, `.nv.udt.entry`. |

---

## L. LTO Pipeline

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4BC6F0` | **nvvm_compile_and_extract** | 13.6KB | VERY HIGH | Calls libNVVM API: `nvvmCompileProgram`, `nvvmGetCompiledResult`, `nvvmGetProgramLog`, `nvvmDestroyProgram`. References `--force-device-c`. |
| `0x4BC4A0` | nvvm_api_wrapper_init | 2.5KB | HIGH | Loads `libnvvm.so` via dlopen, resolves `nvvmCreateProgram` and other API symbols. |
| `0x426CD0` | lto_collect_ir_modules | 7.0KB | MEDIUM | Collects IR modules from input list for LTO compilation. |
| `0x426AE0` | lto_mark_used_symbols | 2.2KB | MEDIUM | Marks symbols as used for dead-code elimination with LTO. Calls `0x44AD40`. |
| `0x43FDB0` | thread_pool_create | small | HIGH | Creates pthread thread pool for split-compile. |
| `0x4264B0` | split_compile_dispatch | small | HIGH | Dispatches compilation units to thread pool workers. |

---

## M. Diagnostics / Error Reporting

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x467460` | **error_emit** | ~2KB | VERY HIGH | Variadic error emission entry. First arg is always `&unk_2A5Bxxx` (error descriptor table entry). Dispatches to `0x467A70`. |
| `0x467A70` | diagnostic_report | 13.1KB | HIGH | Formats and emits diagnostics with severity prefixes: "warning ", "info    ", "error   ", "error*  ", "fatal   ". Location format: "%s, line %d; ". Handles suppression and warning-as-error. |
| `0x4B9E70` | allocation_failure_handler | 5.1KB | HIGH | "An allocation failure occurred; heap memory may be exhausted." Also handles "Multiple errors:". |
| `0x4BC290` | elflink_error_handler | 2.5KB | HIGH | "elfLink: unexpected error". Error wrapper for ELF linking subsystem. |

---

## N. GPU Architecture Profiles

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x484F50` | **arch_profile_database_init** | 54KB | VERY HIGH | Registers all GPU architectures: sm_75 (Turing) through sm_121 (DGX Spark). Creates real/virtual/lto profiles. Sets capability vectors via XMM constants. Hash map at `qword_2A5F8D8`. Notable: sm_88 appears (new Ampere variant). "f" variants = forward-compatible. |
| `0x486FF0` | architecture_parse_name | 2.7KB | HIGH | Parses "sm_%2d%s", "compute_%2d%s", "sass_%2d%s" to numeric arch ID. |
| `0x487220` | architecture_name_format | 2.4KB | MEDIUM | Formats arch number back to name string. |
| `0x4709E0` | can_finalize_arch_check | 2.6KB | HIGH | Architecture compatibility for finalization. Maps 104->120, 130->107, 101->110. Returns error codes 24-30. |
| `0x470DA0` | can_finalize_capability | 2.1KB | HIGH | Finalization capability bitmask check. Maps target codes to bitmask: 'd'(100)=1, 'g'(103)=8, 'n'(110)=2, 'y'(121)=64. |

---

## O. Finalization / JIT Pipeline

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4748F0` | nvlink_link_and_finalize | 49KB | HIGH | Top-level 25-parameter entry point. Handles --binary-kind (mercury/capmerc/sass), processes compilation options, calls `0x471700`. This is what nvcc/driver calls into. |
| `0x471700` | nvlink_finalize_object | 79KB | HIGH | Core finalization orchestrator. 460+ locals. Parses "deviceDebug", "lineInfo", "optLevel", "IsCompute", "IsPIC". Allocates 656-byte compilation unit descriptor. Builds compiler flags. |
| `0x491410` | compilation_unit_initialize | 65KB | HIGH | Initializes compilation unit for code generation. Copies architecture info, sets PIC flags, calls backend init via `0xA4C620`. |

---

## P. Compression (LZ4)

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x46EE00` | LZ4_decompress_safe_extDict | 81KB | HIGH | LZ4 decompression with external dictionary. SSE2 copy optimization. |
| `0x46C690` | LZ4_decompress_safe | 20KB | HIGH | Basic LZ4 safe decompression (no dictionary). |
| `0x46FD50` | LZ4_compress | 13.7KB | HIGH | LZ4 compression with hash table match finding. |

---

## Q. IR Node Primitives

The fundamental API for accessing IR instruction fields. Sub_530FB0 alone has 31,399 callers.

| Address | Proposed Name | Size | Tag | Description |
|---------|---------------|------|-----|-------------|
| `0x530FB0` | IRNode_GetOperand | 16B | -- | `return *(a1+32) + 32 * index` (operand array, 32-byte stride) |
| `0x530FC0` | IRNode_GetNumSrcOperands | 16B | -- | `total_ops + 1 - first_src_index` |
| `0x530FD0` | IRNode_GetNumDstOperands | 16B | -- | `return *(a1 + 92)` |
| `0x530E80` | IRNode_GetRegClass | 16B | -- | Identity extract (unsigned int) |
| `0x530F80` | IRNode_GetDataType | 16B | -- | Identity extract for data type field |
| `0x530E90` | IROperand_IsRegister | 16B | tag=2 | `return type == 2` |
| `0x530EA0` | IROperand_IsImmediate | 16B | tag=1 | `return type == 1` |
| `0x530EB0` | IROperand_IsMemRef | 16B | tag=6 | `return type == 6` |
| `0x530EC0` | IROperand_IsAddress | 16B | tag=10 | `return type == 10` |
| `0x530ED0` | IROperand_IsPredicate | 16B | tag=9 | `return type == 9` |
| `0x530EE0` | IROperand_IsCondCode | 16B | tag=5 | `return type == 5` |
| `0x530EF0` | IROperand_IsConstant | 16B | tag=4 | `return type == 4` |
| `0x530F00` | IROperand_IsSymbol | 16B | tag=3 | `return type == 3` |
| `0x530F50` | IROperand_IsBarrier | 16B | tag=7 | `return type == 7` |
| `0x530F90` | IRNode_SetFlagA | 16B | -- | `*(a1 + 14) = a2` |
| `0x530FA0` | IRNode_SetFlagB | 16B | -- | `*(a1 + 15) = a2` |

---

## R. NVInst Class Hierarchy (Instruction Representation)

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0xA49150` | **NVInst_getOperandField** | 60B | VERY HIGH | 30,768 callers. Calls `0xA7DE70` (hasOperand), then `0xA709F0` (getValue). Returns -1 if field absent. |
| `0xA49120` | NVInst_setOperandField | 16B | HIGH | Thunk to `0xA5B6B0` (180KB switch dispatch). |
| `0xA491D0` | NVInst_setOperandImm | 16B | HIGH | Thunk to `0xA62220` (65KB switch dispatch). |
| `0xA491E0` | NVInst_getOperandFieldSlot | 16B | HIGH | Thunk to `0xA65900` (67KB switch dispatch). |
| `0xA49130` | NVInst_getDefaultOperandValue | 16B | HIGH | Thunk to `0xA67910` (141KB switch dispatch). |
| `0xA49190` | NVInst_hasOperandField | 16B | HIGH | Direct wrapper for `0xA7DE70`. |
| `0xA491A0` | NVInst_copyOperandField | 48B | HIGH | Gets from src via `0xA709F0`, sets on dst via `0xA5B6B0`. |
| `0xA49220` | NVInst_lookupOpcodeDesc | 96B | HIGH | FNV-1a hash lookup in opcode descriptor table. |
| `0xA4AB10` | NVInst_constructor | 11KB | HIGH | Initializes NVInst object with operand vector, hash tables, scheduling info. |

---

## S. Operand Dispatch Mega-Functions

Four giant switch-case functions that implement the complete operand field encoding/decoding dispatch. Each switches on opcode class ID (370+ classes).

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0xA5B6B0` | setOperandField_dispatch | 180KB | HIGH | Sets operand field value on instruction. Switch on opcode class (0x00-0x171). |
| `0xA62220` | setOperandImm_dispatch | 65KB | HIGH | Sets immediate operand value. Same switch structure. |
| `0xA65900` | getOperandField_dispatch | 67KB | HIGH | Gets operand field value for specific slot. |
| `0xA67910` | getDefaultOperandValue_dispatch | 141KB | HIGH | Returns default value for an operand field. |
| `0xA709F0` | InstrFieldOffset_Query | ~180KB | HIGH | 6,491-line switch mapping (opcode_class, field_id) to bit-offset in instruction encoding. Returns -1 if absent. |
| `0xA7DE70` | InstrFieldPresent_Query | ~170KB | HIGH | Same switch structure; returns `(extract != 0)`. Companion to `0xA709F0`. |

---

## T. ISel Pattern Matching

### SM50-SM7x ISel Hub (Maxwell/Pascal/Volta)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0x530FE0` | `0x530FE0-0x5B1AB0` | 1,293 | HIGH | Auto-generated pattern matchers. Signature: `(ctx, node, &opcode, &priority)`. Check attributes via `0xA49150`, operand types/counts, output (target_opcode, priority). 152 distinct opcodes, 36 priority levels. |

### SM75 ISel Hub (Turing)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0xF16150` | `0xF16150-0xFBB780` | 276 | HIGH | SM75 pattern matchers. Same signature. Calls `0xA49150` for attributes, `0x530FD0`/`0x530FB0`/`0x530FC0` for operand queries. |
| `0xFBB810` | -- | 280KB | HIGH | **SM75 ISel mega-hub dispatch**. Calls all 276 matchers, selects highest priority, dispatches to corresponding emitter. Too large to decompile. |

### SM80 ISel Hub (Ampere)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0xCE2000` | `0xCE2000-0xD60000` | 259 | HIGH | SM80 pattern matchers. 19 distinct instruction opcodes (HMMA, IMAD, FFMA, LDG, S2R, etc.). |

### SM100+ ISel (Blackwell)

Blackwell ISel patterns are distributed across the encoding/decoding table regions. The dispatch tables at `0xE43C20` and `0xEFE6C0` use binary search on opcode fields to route to the correct encoder/decoder.

---

## U. Instruction Encoding Infrastructure

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4C28B0` | setBitfield | small | VERY HIGH | `setBitfield(buf, bit_offset, width, value)`. Core bitfield insertion into 128-bit instruction word at buf+544. |
| `0x4C2A60` | encoding_init | small | HIGH | Clears operand remapping table (offsets 468-531), resets operand counter at 532. |
| `0x4C2A90` | encode_predicate | small | HIGH | Encodes predicate guard register from IR node. |
| `0x4C4D60` | encode_register_operand | small | HIGH | Encodes register operand: 1-bit is_output, 4-bit type, 10-bit register number. |
| `0x4C52F0` | encode_immediate_operand | small | HIGH | Encodes constant/immediate operand: 5-bit type + register number. |
| `0x4C5C30` | encode_special_operand | small | HIGH | Encodes predicate/condcode/memory operands with remapping. |
| `0x4C7D10` | encoding_engine_main | 18.6KB | HIGH | Main encoding engine. String: "ENCODING". Converts IR to binary. |
| `0x4CB100` | decoding_engine_entry | 3.4KB | HIGH | Entry point for instruction decoding. String: "DECODING". |

### Per-Architecture Encoding Tables

| Address Range | Target | Encoder Count | Description |
|---------------|--------|---------------|-------------|
| `0x603F60-0x61FA60` | SM50 | 79 | 64-bit instruction words (Maxwell). Format types 1/2/3. |
| `0x620000-0x84DD70` | SM100+ | 1,537 | 128-bit Blackwell SASS. Major opcodes 1/2/8. |
| `0xA87CE0-0xB25D50` | SM90 | 164 | 128-bit Hopper encoding. |
| `0xB9FDE0-0xC9EE60` | SM7x-SM89 | ~270 | Multi-arch encoders: SM70/75/80/86/89. |
| `0xDA0310-0xE436D0` | SM100+ | 438 | Blackwell encoders (second set). |

### Per-Architecture Decoding Tables

| Address Range | Target | Decoder Count | Description |
|---------------|--------|---------------|-------------|
| `0x84DD70-0xA48290` | SM100+ | 1,613 | Instruction descriptor init functions. |
| `0xACECF0-0xB77B60` | SM90 | 139 | Hopper decoders. |
| `0xE43DC0-0xF15A50` | SM100+ | 648 | Blackwell decoders. |

---

## V. MercExpand Engine

The "MercExpand" instruction expansion pass -- NVIDIA's custom ISel/lowering for Mercury (sm100+). Confirmed by string "After MercExpand" at `0x5FF15E`.

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x5FDDB0` | **MercExpand_Dispatch** | 25.5KB | HIGH | Main entry. Switch on IR opcode type: 0=generic, 5/8/9=reg width clamp, 11=complex (shared mem / surface), 12=extended, -1=terminator. Checks attr 200==1107 for MOV special case. |
| `0x5F38E0` | MercExpand_HandleInstruction | 35KB | HIGH | Per-instruction handler. Looks up 184-byte target descriptor, applies resource constraints, handles scheduling hints, 8 constraint categories. |
| `0x5E8710` | MercExpand_BuildFullCFGMaps | 54KB | MEDIUM | Largest MercExpand function. Builds 3 FNV-1a hash maps (offsets 632/648/664). Iterates all basic blocks. |
| `0x5E7B90` | MercExpand_BuildNodeMaps | 24KB | MEDIUM | Builds hash maps for all basic blocks with RPO arrays. |
| `0x5EA250` | CFG_DumpDOTGraph | 2KB | HIGH | Graphviz dump: `digraph f {`, `bix%u`, `bix%d(L%x)`. |
| `0x5EA4F0` | MercExpand_InvalidateRegState | 4.3KB | HIGH | Bumps 15+ generation counters, resets dirty flags. Maps to GPU register file partitions. |
| `0x5FC6B0` | MercExpand_ExpandMOV | 8.3KB | MEDIUM | MOV expansion. Creates target node with opcode 346, sets attribute 227=1233. |
| `0x5FCE20` | MercExpand_ExpandRETURN | 19KB | MEDIUM | Return/exit expansion. Creates nodes with opcode 270, attribute 118=519. |
| `0x5F60E0` | IRTree_Walk | 19KB | HIGH | Recursive tree walker with pre/post callbacks. Manually unrolled to 5 nesting levels. |
| `0x5F8B60` | MercExpand_ApplyResConstraints | 16KB | HIGH | Register resource accounting. Switch on 52 register types (byte_1DFE340 lookup). |

---

## W. Bitvector Operations (SSE-Optimized)

Used by register allocation and liveness analysis throughout the backend.

| Address | Proposed Name | Size | Confidence |
|---------|---------------|------|------------|
| `0x5E4470` | BitVector_AND | 3.2KB | HIGH |
| `0x5E4670` | BitVector_OR | 2.9KB | HIGH |
| `0x5E4810` | BitVector_ANDNOT | 4.4KB | HIGH |
| `0x5E4AE0` | BitVector_XOR | 2.6KB | MEDIUM |
| `0x5E51C0` | BitVector_OR_Changed | 2.9KB | MEDIUM |
| `0x5E55E0` | BitVector_PopCount | 5.4KB | MEDIUM |
| `0x5E5940` | BitVector_FindFirst | 3.0KB | MEDIUM |

---

## X. Debug Info / DWARF Processing

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x47CBC0` | debug_line_decode_replay | 33KB | HIGH | DWARF .debug_line decoder/replayer. Initializes state machine, reads include directories and file tables. |
| `0x478A20` | debug_line_info_encode | 28KB | HIGH | DWARF .debug_line header encoder: version, prologue_length, opcode_base, include_directories[], file_names[]. |
| `0x4783C0` | debug_line_program_serialize | 13KB | HIGH | Serializes DWARF line number program opcodes from individual CUs into combined section. |
| `0x480FB0` | debug_line_merge | 25KB | HIGH | Merges line number tables across compilation units using BST and "%llu_%llu_%llu" keys. |
| `0x482850` | debug_info_complex_merge | 36KB | MEDIUM | Full debug info section merge across CUs. |
| `0x404827` | debug_line_info_builder | 4.3KB | HIGH | Generates DWARF line info for inline functions: "%s+%llu", ".L__$locationLabel$__%d". |

---

## Y. Knobs / Configuration System

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x49B1A0` | knobs_file_read_parse | 59KB | HIGH | Reads knobsfile, parses "[knobs]" section header, processes key=value pairs. Source: `generic_knobs_impl.h`. |
| `0x49D8A0` | parse_knob_value | 24KB | HIGH | Parses single knob value: integer, integer_range, integer_list, double, float, opcode, when-string, value_pair_list. |
| `0x49A0C0` | knob_decode_and_apply | 14KB | MEDIUM | Decodes and applies knobs at pipeline stages: "After Decode", "After Expansion", "After WAR post-expansion", "After Opex". |
| `0x498FE0` | knob_inject_string | 8.7KB | HIGH | Injects string value into knob system. "Invalid knob specified (%s)". |

---

## Z. Mercury / Instruction Scheduling

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4A4DC0` | merc_war_process | 24KB | HIGH | Mercury WAR (Write-After-Read) dependency handler. "After MercWARs". |
| `0x4A8690` | merc_opex_expand | 67KB | HIGH | Mercury operand expansion pass. "After MercOpex". Expands Mercury IR operands into final encoding form. |
| `0x4CFBD0` | ptx_obfuscation_transform | 27KB | HIGH | PTX obfuscation transformation pass. "PTX Obfuscation". |
| `0x4CE8C0` | ptx_version_parse_validate | 29KB | HIGH | Parses `.version` directive, validates PTX version compatibility with target. |

---

## AA. Archive / Input Processing

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4876A0` | archive_signature_check | 2.1KB | HIGH | Checks "!<arch>" and "!<thin>" signatures. |
| `0x487C20` | archive_open | 2.5KB | HIGH | Creates archive context from buffer. Detects thin archives. |
| `0x487E10` | archive_iterate_members | 5.6KB | HIGH | Iterates archive members. Handles "__.LIBDEP", long names, thin archive resolution. |
| `0x462620` | path_split | 3.6KB | HIGH | Splits file path into directory, basename, extension. |
| `0x42FCB0` | create_temp_file | 4.0KB | HIGH | Creates `/tmpxft_PPPPPPPP_CCCCCCCC` temporary files. |

---

## BB. Peephole Optimization

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x406DC0` | peephole_optimizer_main | 6.8KB | MEDIUM | Main driver -- orchestrates multiple optimization passes on instruction buffer. |
| `0x407634` | peephole_instruction_combine | 5.3KB | MEDIUM | Combines dependent instruction pairs. 372-byte records, limit 20479. |
| `0x406377` | peephole_pattern_match | 7.4KB | MEDIUM | Matches and transforms instruction patterns. |
| `0x408594` | peephole_scheduler | 6.5KB | LOW | Instruction scheduling within basic blocks. |
| `0x407F94` | peephole_constant_fold | 3.7KB | LOW | Constant propagation in instructions. |
| `0x407C0A` | peephole_strength_reduce | 3.2KB | LOW | Strength reduction (replace expensive ops with cheaper ones). |
| `0x4083A5` | peephole_dead_instruction_elim | 2.9KB | LOW | Removes dead instructions using liveness. |

---

## CC. Hash/Relocation Key Infrastructure

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x4AF3C0` | hrk_section_process | 8.8KB | HIGH | Processes `.nvHRKE` / `.nvHRKI` (Hash Relocation Key External/Internal). |
| `0x4B02A0` | hrc_hrd_section_process | 16.3KB | HIGH | Processes `.nvHRCE` / `.nvHRCI` / `.nvHRDE` / `.nvHRDI` (Hash Relocation Code/Data). |

---

## DD. PTX Assembler Frontend (Embedded ptxas)

Large PTX processing subsystem in the `0x1430000-0x15C0000` range.

| Address | Proposed Name | Size | Confidence | Description |
|---------|---------------|------|------------|-------------|
| `0x15B86A0` | cuda_builtin_prototype_gen | 345KB | HIGH | Giant switch (~608 cases) generating PTX prototype strings for CUDA builtins: div, rem, rcp, sqrt, wmma, shfl, vote, tcgen05, bulk_copy, etc. |
| `0x147EF50` | ptx_instr_semantic_analyzer | 288KB | HIGH | Master instruction validator. SM version gates, texture modes, cache policies, state spaces, vector types, scoping. |
| `0x1487650` | ptx_statement_processor | 240KB | MEDIUM | Top-level PTX statement handler. Processes `.maxnctapersm`, `.reqntid`, kernel parameter limits (4352 bytes), function prototypes. |
| `0x146BEC0` | ptx_load_store_validator | 206KB | HIGH | Memory operation validator. Validates ld/st, atomics, reductions, fence, membar, cp.async, cache eviction, scope. |

---

## Statistics

| Metric | Value |
|--------|-------|
| Binary size | 26.2 MB |
| Estimated total functions | ~40,000 |
| Functions documented here | ~140 key functions |
| ISel pattern matchers (all arches) | ~2,100+ |
| Instruction encoders (all arches) | ~2,500+ |
| Instruction descriptor inits | ~1,600+ |
| Instruction decoders (all arches) | ~800+ |
| Subsystems identified | 30 |
| Largest function | `0x15B86A0` cuda_builtin_prototype_gen (345KB) |
| Most-called function | `0x530FB0` IRNode_GetOperand (31,399 callers) |
| Core linker functions (A-K) | ~50 functions |
| Backend/codegen functions (Q-BB) | ~90 functions |

---

## Address Map (sorted)

Quick reference sorted by address for binary navigation.

```
0x404827  debug_line_info_builder
0x406377  peephole_pattern_match
0x406DC0  peephole_optimizer_main
0x407634  peephole_instruction_combine
0x407C0A  peephole_strength_reduce
0x407F94  peephole_constant_fold
0x408594  peephole_scheduler
0x409800  main
0x426570  validate_arch_and_merge
0x426AE0  lto_mark_used_symbols
0x426CD0  lto_collect_ir_modules
0x4275C0  post_link_transform
0x4279C0  trace_phase
0x427AE0  nvlink_parse_options
0x42A680  register_module
0x42AF40  extract_fatbin_member
0x42DBC0  option_validate_value
0x42DFE0  option_parser_create
0x42E390  option_get_value
0x42E5A0  option_parse_argv
0x42F130  option_register
0x42FCB0  create_temp_file
0x4307C0  arena_alloc
0x431000  arena_free
0x431770  arena_dump_stats
0x431C70  arena_destroy
0x432020  arena_create_named
0x432B10  merge_overlapping_global
0x4339A0  optimize_constant_dedup
0x4343C0  merge_overlapping_constant
0x436BD0  shared_memory_optimizer
0x437E20  merge_overlapping_local
0x438640  merge_constant_bank_data
0x438DD0  process_bindless_references
0x439830  shared_memory_layout
0x4411F0  elfw_copy_section
0x441AC0  elfw_add_reloc_section
0x4438F0  elfw_create
0x440740  elfw_add_section
0x440BE0  elfw_add_section_with_data
0x442820  elfw_merge_symbols
0x442CA0  elfw_add_symbol
0x445000  finalize_elf
0x4475B0  elfw_destroy
0x4478F0  elfw_dump_structure
0x448360  elfw_get_section_header
0x4489C0  hash_table_create
0x448E70  elfw_section_table_build
0x44A5D0  callgraph_detect_recursion
0x44AD40  dead_code_elimination
0x44C030  callgraph_traverse
0x44CCF0  callgraph_dump_dot
0x44D200  build_callgraph_section
0x44F410  arena_get_metadata
0x450ED0  propagate_register_counts
0x451D80  compute_entry_properties
0x459640  reloc_vtable_create
0x45BAA0  write_elf_section
0x45BF00  write_elf_to_buffer
0x45C920  write_elf_to_file
0x45C950  write_elf_to_memory
0x45CAC0  oom_handler
0x45CAE0  arena_assert
0x45CCD0  timing_start
0x45D180  merge_weak_function
0x45E7D0  merge_elf
0x462620  path_split
0x463F70  uft_setup_sections
0x4637B0  uft_reorder_entries
0x464460  linked_list_append
0x467460  error_emit
0x467A70  diagnostic_report
0x469D60  apply_relocations
0x46ADC0  emit_resolved_relocations
0x46C690  LZ4_decompress_safe
0x46EE00  LZ4_decompress_safe_extDict
0x46FD50  LZ4_compress
0x4707D0  debug_info_set_prefix_suffix
0x4709E0  can_finalize_arch_check
0x470DA0  can_finalize_capability
0x471700  nvlink_finalize_object
0x4748F0  nvlink_link_and_finalize
0x47CBC0  debug_line_decode_replay
0x478A20  debug_line_info_encode
0x4783C0  debug_line_program_serialize
0x480FB0  debug_line_merge
0x482850  debug_info_complex_merge
0x484F50  arch_profile_database_init
0x486FF0  architecture_parse_name
0x4876A0  archive_signature_check
0x487C20  archive_open
0x487E10  archive_iterate_members
0x4882A0  ocg_memspace_alloc
0x489140  memspace_statistics_print
0x491410  compilation_unit_initialize
0x498FE0  knob_inject_string
0x49A0C0  knob_decode_and_apply
0x49B1A0  knobs_file_read_parse
0x49D8A0  parse_knob_value
0x4A4DC0  merc_war_process
0x4A8690  merc_opex_expand
0x4AF3C0  hrk_section_process
0x4B02A0  hrc_hrd_section_process
0x4B4E60  nvlink_elf_link_main
0x4B9E70  allocation_failure_handler
0x4BC290  elflink_error_handler
0x4BC4A0  nvvm_api_wrapper_init
0x4BC6F0  nvvm_compile_and_extract
0x4C28B0  setBitfield
0x4C2A60  encoding_init
0x4C2A90  encode_predicate
0x4C4D60  encode_register_operand
0x4C52F0  encode_immediate_operand
0x4C5C30  encode_special_operand
0x4C7D10  encoding_engine_main
0x4CB100  decoding_engine_entry
0x4CE8C0  ptx_version_parse_validate
0x4CFBD0  ptx_obfuscation_transform
0x530E80  IRNode_GetRegClass
0x530E90  IROperand_IsRegister
0x530EA0  IROperand_IsImmediate
0x530FB0  IRNode_GetOperand
0x530FC0  IRNode_GetNumSrcOperands
0x530FD0  IRNode_GetNumDstOperands
0x5E4470  BitVector_AND
0x5E4670  BitVector_OR
0x5E4810  BitVector_ANDNOT
0x5E8710  MercExpand_BuildFullCFGMaps
0x5EA250  CFG_DumpDOTGraph
0x5EA4F0  MercExpand_InvalidateRegState
0x5F38E0  MercExpand_HandleInstruction
0x5F60E0  IRTree_Walk
0x5F8B60  MercExpand_ApplyResConstraints
0x5FDDB0  MercExpand_Dispatch
0xA49150  NVInst_getOperandField
0xA5B6B0  setOperandField_dispatch
0xA62220  setOperandImm_dispatch
0xA65900  getOperandField_dispatch
0xA67910  getDefaultOperandValue_dispatch
0xA709F0  InstrFieldOffset_Query
0xA7DE70  InstrFieldPresent_Query
```
