# Function Map

Address-to-identity lookup for ~160 key functions across 20 subsystems in nvlink v13.0.88 (~37 MB, 40,532 total functions). Confidence: **VERY HIGH** = string/symbol evidence, **HIGH** = strong structural evidence, **MEDIUM** = inferred from context/callgraph.

Binary: `/usr/local/cuda-13.0/bin/nvlink`
SHA256: see [versions](versions.md)
Entry point: `0x409800` (`main`)

> **Binary composition**: Approximately **95% of the binary is embedded ptxas compiler backend** (ISel, register allocation, scheduling, instruction encoding). Only about **5% is linker logic** (~1,900 functions in `0x400000`--`0x4C0000`). Of the 40,532 total functions, roughly 38,000 belong to the statically linked ptxas compiler -- these are documented in the [ptxas wiki function-map](https://example.com/ptxas/function-map.md). This page focuses on the ~2,000 linker-specific and infrastructure functions, plus the embedded-ptxas interfaces visible from the linker side.
>
> See [Binary Layout](binary-layout.md) for the full address-range breakdown and composition table.

---

## Top 20 Most-Called Functions

Functions with the highest cross-reference count in the binary. These form the backbone of every subsystem.

| # | Address | Decompiled | Proposed Name | Callers | Size | Role |
|---|---------|------------|---------------|---------|------|------|
| 1 | `0x530FB0` | `sub_530FB0` | IRNode_GetOperand | 31,399 | 16B | Return pointer to operand at index (32-byte stride) |
| 2 | `0xA49150` | `sub_A49150` | NVInst_getOperandField | 30,768 | 60B | Query instruction attribute by field ID; dispatches to `sub_A7DE70` + `sub_A709F0` |
| 3 | `0x4307C0` | `sub_4307C0` | arena_alloc | ~10K+ | 10.7KB | Thread-safe arena allocator (small-block free-list + large-block pages) |
| 4 | `0x431000` | `sub_431000` | arena_free | ~10K+ | 4.7KB | Arena deallocator, returns blocks to size-class free-lists |
| 5 | `0x530E90` | `sub_530E90` | IROperand_IsRegister | ~5K+ | 16B | `return type_tag == 2` |
| 6 | `0x530FC0` | `sub_530FC0` | IRNode_GetNumSrcOperands | ~5K+ | 16B | `total_ops + 1 - first_src_index` |
| 7 | `0x530FD0` | `sub_530FD0` | IRNode_GetNumDstOperands | ~5K+ | 16B | `return *(a1 + 92)` |
| 8 | `0x530EA0` | `sub_530EA0` | IROperand_IsImmediate | ~3K+ | 16B | `return type_tag == 1` |
| 9 | `0x530E80` | `sub_530E80` | IRNode_GetRegClass | ~3K+ | 16B | Identity function / unsigned int extract |
| 10 | `0xA50D10` | `sub_A50D10` | encode_GPR | ~3K+ | tiny | Encode register number for destination field |
| 11 | `0x467460` | `sub_467460` | error_emit | ~2K+ | ~2KB | Variadic error emission (dispatches to `sub_467A70`) |
| 12 | `0x448360` | `sub_448360` | elfw_get_section_header | ~2K+ | <2KB | Section header accessor |
| 13 | `0x44F410` | `sub_44F410` | arena_get_metadata | ~2K+ | <2KB | Look up allocation metadata for a pointer |
| 14 | `0x45CAC0` | `sub_45CAC0` | oom_handler | ~1K+ | tiny | Out-of-memory handler, calls abort path |
| 15 | `0x45CAE0` | `sub_45CAE0` | arena_assert | ~1K+ | tiny | Arena validity assertion |
| 16 | `0x4C28B0` | `sub_4C28B0` | setBitfield | ~1K+ | small | Core bitfield insertion into instruction word |
| 17 | `0x50C790` | `sub_50C790` | getReuse | ~1K+ | small | Read 1-bit reuse flag from encoded instruction |
| 18 | `0x530F80` | `sub_530F80` | IRNode_GetDataType | ~1K+ | 16B | Identity function for data type field at +20 |
| 19 | `0x4489C0` | `sub_4489C0` | hash_table_create | ~500+ | small | Create hash table for option/symbol lookup |
| 20 | `0x464460` | `sub_464460` | linked_list_append | ~500+ | small | Append node to singly-linked list |

---

## 1. Entry & CLI

### Main Program Flow

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x409800` | `main` | **main** | 58KB | VERY HIGH | Complete nvlink entry point. Parses options, dispatches by input type (cubin/ptx/fatbin/nvvm/ltoir/bc/.o/.so/.a), drives merge/layout/relocate/finalize/write pipeline, handles Mercury post-link, host linker script generation. |
| `0x427AE0` | `sub_427AE0` | nvlink_parse_options | 30KB | VERY HIGH | Registers ~60 CLI options via `sub_42F130`, extracts all into globals. Validates arch (sm > 19), Mercury mode (sm > 99), LTO constraints. String evidence: "suppress-stack-size-warning", "suppress-arch-warning". |
| `0x4275C0` | `sub_4275C0` | post_link_transform | 4KB | VERY HIGH | FNLZR (Finalizer) entry for Mercury/SASS. String evidence: "FNLZR: Input ELF: %s", "FNLZR: Pre-Link Mode". |
| `0x45CCD0` | `sub_45CCD0` | timing_start | tiny | HIGH | Begin profiling timer for phase tracing. |
| `0x4279C0` | `sub_4279C0` | trace_phase | tiny | HIGH | Debug trace with phase names: "init", "read", "merge", "layout", "relocate", "finalize", "write". |

> **Details**: [Pipeline Entry](pipeline/entry.md), [Pipeline Overview](pipeline/overview.md), [Mode Dispatch](pipeline/mode-dispatch.md)

### Command-Line Option Parsing

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x42DFE0` | `sub_42DFE0` | option_parser_create | 4.5KB | HIGH | Allocates 56-byte parser struct, creates two hash tables for option lookup. |
| `0x42F130` | `sub_42F130` | option_register | 4.9KB | HIGH | Registers single option (120-byte entry): name, short name, type (1=bool/2=string/4=int/0=file), multiplicity, default, help. Called ~60 times from `sub_427AE0`. |
| `0x42E5A0` | `sub_42E5A0` | option_parse_argv | 9.5KB | HIGH | Iterates argv, matches against registered options, handles `--`, `=`, response files `@file`. |
| `0x42E390` | `sub_42E390` | option_get_value | 2.9KB | HIGH | Extracts parsed option value (1/4/8 byte) into destination variable. Called ~80 times. |
| `0x42D700` | `sub_42D700` | option_format_help | 5.6KB | MEDIUM | Formats single option help entry with defaults, keywords, allowed values. |
| `0x42DBC0` | `sub_42DBC0` | option_validate_value | 5.1KB | MEDIUM | Validates option value against type constraints ("32-bit integer", "64-bit hex", etc.). |

> **Details**: [CLI Options](pipeline/cli-options.md), [CLI Flags](config/cli-flags.md)

### Static Initialization (Constructors)

| Address | Decompiled | Proposed Name | Confidence | Description |
|---------|------------|---------------|------------|-------------|
| `0x40C4F0` | `ctor_001` | ctor_thread_infra | VERY HIGH | pthread_key_create, mutex init, scheduler priority range. Sets up TLS infrastructure. |
| `0x40C5C0` | `ctor_002` | ctor_002 | HIGH | Additional initialization (registered after ctor_001). |
| `0x410830` | `ctor_003` | ctor_003 | HIGH | Registers atexit handler via `__cxa_atexit`. |
| `0x410850` | `ctor_004` | ctor_004 | HIGH | Paired with ctor_003. |
| `0x412750` | `ctor_005` | ctor_knob_table | HIGH | Initializes knob storage array via `sub_44F670`. |
| `0x412790` | `ctor_006` | ctor_006 | HIGH | Paired with ctor_005. |
| `0x426260` | `ctor_008` | ctor_version_constants | VERY HIGH | Sets version constants: `qword_2A74108 = 0x60000000`, `qword_2A74100 = 0x60000001`. |
| `0x426280` | `ctor_009` | ctor_009 | HIGH | Additional version/capability setup. |
| `0x4262B0` | `ctor_010` | ctor_010 | HIGH | Additional initialization. |
| `0x426330` | `ctor_011` | ctor_011 | HIGH | Additional initialization. |

> **Details**: [Pipeline Entry](pipeline/entry.md), [Binary Layout](binary-layout.md)

---

## 2. Pipeline Phases

### Merge Engine (init + read + merge)

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x45E7D0` | `sub_45E7D0` | **merge_elf** | 89KB | VERY HIGH | Heart of the linker. Iterates all sections of input ELF, merges symbols/relocations/data. Handles `.nv.global`, `.nv.shared`, `.nv.constant`, `.nv.info`, DWARF debug. 450+ locals. |
| `0x45D180` | `sub_45D180` | merge_weak_function | 26.8KB | HIGH | Resolves weak function conflicts by comparing register counts and PTX versions. |
| `0x426570` | `sub_426570` | validate_arch_and_merge | 7.4KB | HIGH | Validates cubin architecture matches target ("compute_%d%c", "sm_%d%c"). |
| `0x432B10` | `sub_432B10` | merge_overlapping_global | 11.7KB | HIGH | Validates overlapping symbol definitions in `.nv.global` contain identical data. |
| `0x437E20` | `sub_437E20` | merge_overlapping_local | 11.6KB | HIGH | Same pattern for `.nv.local.*` sections. |
| `0x4343C0` | `sub_4343C0` | merge_overlapping_constant | 11.8KB | HIGH | Same pattern for `.nv.constant*` sections. |
| `0x4339A0` | `sub_4339A0` | optimize_constant_dedup | 13.2KB | HIGH | Deduplicates constant values: "found duplicate value 0x%x, alias %s to %s". Handles 32-bit and 64-bit. |
| `0x438640` | `sub_438640` | merge_constant_bank_data | 4.0KB | HIGH | Merges data into constant memory banks. Validates `bank SHT not CUDA_CONSTANT_?`. |

> **Details**: [Pipeline Merge](pipeline/merge.md), [Section Merging](linker/section-merging.md), [Weak Symbols](linker/weak-symbols.md), [Data Layout Optimization](linker/data-layout-opt.md)

### Layout Phase

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x439830` | `sub_439830` | **shared_memory_layout** | 66KB | VERY HIGH | Allocates and lays out all shared memory: global, extern, local, reserved. Computes overlapping set analysis. Handles `.nv.reservedSmem.*`. |
| `0x436BD0` | `sub_436BD0` | shared_memory_optimizer | 15.7KB | HIGH | Builds interference graph, groups non-overlapping shared variables to reduce total shared memory usage. |
| `0x438DD0` | `sub_438DD0` | process_bindless_references | 12.8KB | HIGH | Handles bindless texture/surface relocations. Creates `$NVLINKBINDLESSOFF_%s` synthetic symbols. |

> **Details**: [Pipeline Layout](pipeline/layout.md), [Bindless Relocations](linker/bindless-relocations.md)

### Relocate Phase

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x469D60` | `sub_469D60` | **apply_relocations** | 26.6KB | VERY HIGH | Complete relocation resolution. Handles `__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT`, `__UFT`. Processes `.nv.resolvedrela`. |
| `0x46ADC0` | `sub_46ADC0` | emit_resolved_relocations | 11.5KB | HIGH | Creates `.nv.resolvedrela` section when `--preserve-relocs`. |
| `0x459640` | `sub_459640` | reloc_vtable_create | 16.1KB | HIGH | Creates 632-byte vtable with ~70 handler slots, dispatched per arch generation (sm30..sm100+). |

> **Details**: [Pipeline Relocate](pipeline/relocate.md), [R_CUDA Relocations](linker/r-cuda-relocations.md), [R_MERCURY Catalog](reference/r-mercury-catalog.md)

### Finalize Phase

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x445000` | `sub_445000` | **finalize_elf** | 56KB | VERY HIGH | Final relocation application and ELF finalization. Architecture-specific relocation encodings, symbol address resolution, final section content generation. |
| `0x451D80` | `sub_451D80` | compute_entry_properties | 98KB | HIGH | Largest function in the linker core. Computes per-kernel register counts, stack sizes, barrier counts. Processes unified function tables. Propagates through callgraph. 500+ locals. |
| `0x450ED0` | `sub_450ED0` | propagate_register_counts | 16KB | HIGH | Propagates register/barrier counts from callees to callers. Creates `EIATTR_NUM_BARRIERS`. |

> **Details**: [Pipeline Finalize](pipeline/finalize.md), [ELF Serialization](elf/serialization.md)

### Write Phase (ELF Output)

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x45C920` | `sub_45C920` | write_elf_to_file | small | HIGH | Wrapper calling `sub_45BF00` to serialize ELF to file. |
| `0x45C950` | `sub_45C950` | write_elf_to_memory | small | HIGH | Wrapper calling `sub_45BF00` to serialize ELF to buffer. |
| `0x45BF00` | `sub_45BF00` | write_elf_to_buffer | 13.3KB | HIGH | Serializes ELF header, program headers, section headers, section data. Validates sizes. |
| `0x45BAA0` | `sub_45BAA0` | write_elf_section | small | HIGH | Writes individual section data to output buffer at computed offset. |

> **Details**: [Pipeline Output](pipeline/output.md), [ELF Writer](structs/elf-writer.md)

---

## 3. Input Processing

### ELF Structure Management (elfw)

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4438F0` | `sub_4438F0` | **elfw_create** | 14.8KB | HIGH | Creates ELF wrapper with sections: `.note.nv.cuinfo`, `.note.nv.tkinfo`, `.shstrtab`, `.strtab`, `.symtab`. Creates "elfw memory space" arena. |
| `0x440740` | `sub_440740` | elfw_add_section | 5.4KB | HIGH | Adds new section to ELF wrapper. |
| `0x440BE0` | `sub_440BE0` | elfw_add_section_with_data | 7.0KB | HIGH | Adds section with initial data copy. |
| `0x441AC0` | `sub_441AC0` | elfw_add_reloc_section | 9.5KB | HIGH | Creates `.rela%s` / `.rel%s` relocation sections. |
| `0x442CA0` | `sub_442CA0` | elfw_add_symbol | 7.2KB | HIGH | Adds symbol (STB_GLOBAL/WEAK/LOCAL), updates callgraph for function symbols. |
| `0x442820` | `sub_442820` | elfw_merge_symbols | 5.4KB | HIGH | Merges unified symbols including `__cuda_uf_stub_` / `.nv.uft` stubs. |
| `0x4411F0` | `sub_4411F0` | elfw_copy_section | 12.2KB | HIGH | Deep copy of section data, symbols, relocations between elfw objects. |
| `0x4478F0` | `sub_4478F0` | elfw_dump_structure | 15.1KB | HIGH | Debug dump of ELF wrapper state: sections, symbols, relocations. |
| `0x448E70` | `sub_448E70` | elfw_section_table_build | 14.6KB | MEDIUM | Rebuilds section header table, computes offsets/sizes for final layout. |
| `0x4475B0` | `sub_4475B0` | elfw_destroy | 3.0KB | HIGH | Destroys ELF wrapper and frees associated arena. |

> **Details**: [ELF Parsing](input/elf-parsing.md), [Device ELF Format](elf/device-elf-format.md), [NVIDIA Sections](elf/nvidia-sections.md)

### Fatbin Extraction & Input Dispatch

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x42AF40` | `sub_42AF40` | extract_fatbin_member | 11.1KB | HIGH | Extracts object from fatbin. Dispatches by type: 1=ptx, 8=nvvm, 16=mercury/capmerc, default=cubin. |
| `0x42A680` | `sub_42A680` | register_module | 11.9KB | HIGH | Registers linked module with module_id extracted from ELF via `sub_46F0C0`. |
| `0x4876A0` | `sub_4876A0` | archive_signature_check | 2.1KB | HIGH | Checks "!<arch>" and "!<thin>" signatures. |
| `0x487C20` | `sub_487C20` | archive_open | 2.5KB | HIGH | Creates archive context from buffer. Detects thin archives. |
| `0x487E10` | `sub_487E10` | archive_iterate_members | 5.6KB | HIGH | Iterates archive members. Handles "__.LIBDEP", long names, thin archive resolution. |
| `0x462620` | `sub_462620` | path_split | 3.6KB | HIGH | Splits file path into directory, basename, extension. |
| `0x42FCB0` | `sub_42FCB0` | create_temp_file | 4.0KB | HIGH | Creates `/tmpxft_PPPPPPPP_CCCCCCCC` temporary files. |

> **Details**: [Fatbin Extraction](input/fatbin-extraction.md), [File Type Detection](input/file-type-detection.md), [Cubin Loading](input/cubin-loading.md)

### PTX Input Processing

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4CE8C0` | `sub_4CE8C0` | ptx_version_parse_validate | 29KB | HIGH | Parses `.version` directive, validates PTX version compatibility with target. |
| `0x4CFBD0` | `sub_4CFBD0` | ptx_obfuscation_transform | 27KB | HIGH | PTX obfuscation transformation pass. "PTX Obfuscation". |

> **Details**: [PTX Input](input/ptx-input.md)

---

## 4. Symbol Resolution & Callgraph

### Symbol Resolution

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x442CA0` | `sub_442CA0` | elfw_add_symbol | 7.2KB | HIGH | Adds global symbol to ELF wrapper's symbol table. STB_GLOBAL/WEAK/LOCAL binding. |
| `0x442820` | `sub_442820` | elfw_merge_symbols | 5.4KB | HIGH | Merges unified symbols; handles `__cuda_uf_stub_` and `.nv.uft` stubs. |
| `0x4489C0` | `sub_4489C0` | hash_table_create | small | HIGH | Creates open-addressing hash table for symbol/option lookup. |
| `0x449A80` | `sub_449A80` | LinkerHash_lookup | 592 B | HIGH | Lookup by key in the linker's open-addressing hash table; 4-mode dispatch via `(flags>>4)&0xF` (string / context-string / pointer / uint64). Returns value pointer or 0. Hot path of symbol-resolution merge -- probes the global name->index map for every incoming ELF symbol. |

> **Details**: [Symbol Resolution](linker/symbol-resolution.md), [Hash Tables](linker/hash-tables.md), [Symbol Resolution Walkthrough](linker/symbol-resolution-walkthrough.md)

### Callgraph & Dead Code Elimination

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x44AD40` | `sub_44AD40` | dead_code_elimination | 22.5KB | HIGH | DFS reachability on callgraph, removes unreachable functions and associated `.nv.local`/`.nv.shared` sections. Keeps address-taken functions. |
| `0x44A5D0` | `sub_44A5D0` | callgraph_detect_recursion | 14.4KB | HIGH | DFS-based recursion detection for stack size requirements. |
| `0x44C030` | `sub_44C030` | callgraph_traverse | 10.2KB | HIGH | Propagates stack sizes and register counts through call chains. |
| `0x44CCF0` | `sub_44CCF0` | callgraph_dump_dot | small | HIGH | Writes Graphviz DOT format via `digraph callgraph { %s -> %s; }`. |
| `0x44D200` | `sub_44D200` | build_callgraph_section | 8.5KB | HIGH | Generates `.nv.callgraph` section in output ELF. |

> **Details**: [Dead Code Elimination](linker/dead-code-elimination.md)

### Unified Table (UDT/UFT) Management

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4637B0` | `sub_4637B0` | uft_reorder_entries | 10.1KB | HIGH | Reorders unified function/descriptor table entries. UUID-based mapping: "map uid <%llx,%llx> to key=%llx". |
| `0x463F70` | `sub_463F70` | uft_setup_sections | 4.0KB | HIGH | Creates/validates `.nv.udt`, `.nv.uft`, `.nv.uft.entry`, `.nv.udt.entry`. |

> **Details**: [Unified Function Tables](elf/uft.md)

---

## 5. Relocation Engine

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x469D60` | `sub_469D60` | **apply_relocations** | 26.6KB | VERY HIGH | Complete relocation resolution. Handles `__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT`, `__UFT`. Processes `.nv.resolvedrela`. |
| `0x468760` | `sub_468760` | reloc_action_dispatcher | 14.3KB | HIGH | Descriptor-driven per-relocation action engine called from `sub_469D60`. Indexes a 64-byte-per-entry descriptor table by `type<<6`; encodes a resolved symbol value into the instruction word via the bit-field writer, handling SHT_RELA (absolute) vs SHT_REL (addend-based) paths. 582 lines decompiled. |
| `0x468670` | `sub_468670` | reloc_bitfield_extract | ~240 B | HIGH | Bit-field extractor used by `sub_468760` to read the existing addend bits out of an instruction word before adding the resolved symbol value. |
| `0x4685B0` | `sub_4685B0` | reloc_bitfield_write | ~240 B | HIGH | Bit-field writer used by `sub_468760` to splice resolved values back into instruction words and data at non-byte-aligned positions. |
| `0x46ADC0` | `sub_46ADC0` | emit_resolved_relocations | 11.5KB | HIGH | Creates `.nv.resolvedrela` section when `--preserve-relocs`. |
| `0x459640` | `sub_459640` | reloc_vtable_create | 16.1KB | HIGH | Creates 632-byte vtable with ~70 handler slots, dispatched per arch generation (sm30..sm100+). |
| `0x4AF3C0` | `sub_4AF3C0` | hrk_section_process | 8.8KB | HIGH | Processes `.nvHRKE` / `.nvHRKI` (Hash Relocation Key External/Internal). |
| `0x4B02A0` | `sub_4B02A0` | hrc_hrd_section_process | 16.3KB | HIGH | Processes `.nvHRCE` / `.nvHRCI` / `.nvHRDE` / `.nvHRDI` (Hash Relocation Code/Data). |

> **Details**: [R_CUDA Relocations](linker/r-cuda-relocations.md), [R_MERCURY Catalog](reference/r-mercury-catalog.md), [Bindless Relocations](linker/bindless-relocations.md)

---

## 6. LTO Integration

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4BC6F0` | `sub_4BC6F0` | **nvvm_compile_and_extract** | 13.6KB | VERY HIGH | Calls libNVVM API: `nvvmCompileProgram`, `nvvmGetCompiledResult`, `nvvmGetProgramLog`, `nvvmDestroyProgram`. References `--force-device-c`. |
| `0x4BC4A0` | `sub_4BC4A0` | nvvm_api_wrapper_init | 2.5KB | HIGH | Loads `libnvvm.so` via dlopen, resolves `nvvmCreateProgram` and other API symbols via dlsym. |
| `0x4BD760` | `sub_4BD760` | ptxas_compile_split | ~12KB | HIGH | Split-aware embedded-ptxas entry. Sets arch/options, adds the PTX input, drives the embedded compiler, retrieves output. Target of the split-compile worker `sub_4264B0`; return value is written into work-item offset +36 as an elfLink error code (0..13). |
| `0x4BD4E0` | `sub_4BD4E0` | ptx_compile_whole_program | small | HIGH | Top-level whole-program PTX compile path -- produces final cubin without splitting. Alternative to the split-compile worker path. |
| `0x4BD240` | `sub_4BD240` | cubin_post_process | small | HIGH | Cubin post-processing after embedded-ptxas compilation. Validates ABI (`-m32` vs `-m64`), invokes the cubin bytecode extractor `sub_4BE350`; surfaces elfLink errors on mismatch. |
| `0x4BD0A0` | `sub_4BD0A0` | nvvm_compile_driver | small | HIGH | NVVM IR compilation driver. Sequences target arch setup (`sub_4CE2F0`), debug mode (`sub_4CE380`), 64-bit mode (`sub_4CE640`), module addition (`sub_4CE070`), final compile (`sub_4CE8C0`). |
| `0x426CD0` | `sub_426CD0` | lto_collect_ir_modules | 7.0KB | MEDIUM | Collects IR modules from input list for LTO compilation. Builds the cicc/NVVM option list (array of string pointers). |
| `0x429BA0` | `sub_429BA0` | lto_ptxas_options_build | 6.7KB | HIGH | Builds the single space-separated `-Xptxas` option string for the embedded ptxas assembler used by the LTO path. Reads from the same set of globals as `sub_426CD0` but emits a flat string rather than an argv vector. Multi-branch early-exit when several optional features are simultaneously off. |
| `0x426AE0` | `sub_426AE0` | lto_mark_used_symbols | 2.2KB | MEDIUM | Marks symbols as used for dead-code elimination with LTO. Calls `sub_44AD40`. |
| `0x43FDB0` | `sub_43FDB0` | thread_pool_create | small | HIGH | Creates pthread thread pool for split-compile. |
| `0x43FC80` | `start_routine` | thread_worker_entry | small | VERY HIGH | Named symbol: `start_routine`. Thread pool worker entry point for parallel compilation tasks. |
| `0x4264B0` | `sub_4264B0` | split_compile_dispatch | small | HIGH | Dispatches compilation units to thread pool workers; unpacks 40-byte work-item structs and forwards their fields to `sub_4BD760`. |

> **Details**: [LTO Overview](lto/overview.md), [LibNVVM Integration](lto/libnvvm-integration.md), [Split Compilation](lto/split-compilation.md), [Option Forwarding](lto/option-forwarding.md)

---

## 7. Mercury / FNLZR

### Finalization / JIT Pipeline

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4748F0` | `sub_4748F0` | nvlink_link_and_finalize | 49KB | HIGH | Top-level 25-parameter entry point. Handles --binary-kind (mercury/capmerc/sass), processes compilation options, calls `sub_471700`. This is what nvcc/driver calls into. |
| `0x471700` | `sub_471700` | nvlink_finalize_object | 79KB | HIGH | Core finalization orchestrator. 460+ locals. Parses "deviceDebug", "lineInfo", "optLevel", "IsCompute", "IsPIC". Allocates 656-byte compilation unit descriptor. Builds compiler flags. |
| `0x491410` | `sub_491410` | compilation_unit_initialize | 65KB | HIGH | Initializes compilation unit for code generation. Copies architecture info, sets PIC flags, calls backend init via `sub_A4C620`. |

> **Details**: [FNLZR Pipeline](mercury/fnlzr.md), [Mercury Overview](mercury/overview.md)

### MercExpand Engine

The "MercExpand" instruction expansion pass -- NVIDIA's custom ISel/lowering for Mercury (sm100+). Confirmed by string "After MercExpand" at `0x5FF15E`.

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x5FDDB0` | `sub_5FDDB0` | **MercExpand_Dispatch** | 25.5KB | HIGH | Main entry. Switch on IR opcode type: 0=generic, 5/8/9=reg width clamp, 11=complex (shared mem / surface), 12=extended, -1=terminator. Checks attr 200==1107 for MOV special case. |
| `0x5F38E0` | `sub_5F38E0` | MercExpand_HandleInstruction | 35KB | HIGH | Per-instruction handler. Looks up 184-byte target descriptor, applies resource constraints, handles scheduling hints, 8 constraint categories. |
| `0x5E8710` | `sub_5E8710` | MercExpand_BuildFullCFGMaps | 54KB | MEDIUM | Largest MercExpand function. Builds 3 FNV-1a hash maps (offsets 632/648/664). Iterates all basic blocks. |
| `0x5E7B90` | `sub_5E7B90` | MercExpand_BuildNodeMaps | 24KB | MEDIUM | Builds hash maps for all basic blocks with RPO arrays. |
| `0x5EA250` | `sub_5EA250` | CFG_DumpDOTGraph | 2KB | HIGH | Graphviz dump: `digraph f {`, `bix%u`, `bix%d(L%x)`. |
| `0x5EA4F0` | `sub_5EA4F0` | MercExpand_InvalidateRegState | 4.3KB | HIGH | Bumps 15+ generation counters, resets dirty flags. Maps to GPU register file partitions. |
| `0x5FC6B0` | `sub_5FC6B0` | MercExpand_ExpandMOV | 8.3KB | MEDIUM | MOV expansion. Creates target node with opcode 346, sets attribute 227=1233. |
| `0x5FCE20` | `sub_5FCE20` | MercExpand_ExpandRETURN | 19KB | MEDIUM | Return/exit expansion. Creates nodes with opcode 270, attribute 118=519. |
| `0x5F60E0` | `sub_5F60E0` | IRTree_Walk | 19KB | HIGH | Recursive tree walker with pre/post callbacks. Manually unrolled to 5 nesting levels. |
| `0x5F8B60` | `sub_5F8B60` | MercExpand_ApplyResConstraints | 16KB | HIGH | Register resource accounting. Switch on 52 register types (`byte_1DFE340` lookup). |

> **Details**: [Mercury Overview](mercury/overview.md), [Compiler Passes](mercury/compiler-passes.md)

### Mercury Instruction Scheduling

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4A4DC0` | `sub_4A4DC0` | merc_war_process | 24KB | HIGH | Mercury WAR (Write-After-Read) dependency handler. "After MercWARs". |
| `0x4A8690` | `sub_4A8690` | merc_opex_expand | 67KB | HIGH | Mercury operand expansion pass. "After MercOpex". Expands Mercury IR operands into final encoding form. |

> **Details**: [Scheduling](ptxas/scheduling.md)

---

## 8. ELF Output / Serialization

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4438F0` | `sub_4438F0` | **elfw_create** | 14.8KB | HIGH | Creates ELF wrapper with initial sections. (Also listed under Input Processing.) |
| `0x445000` | `sub_445000` | **finalize_elf** | 56KB | VERY HIGH | Final relocation application and ELF finalization. (Also listed under Finalize Phase.) |
| `0x45BF00` | `sub_45BF00` | write_elf_to_buffer | 13.3KB | HIGH | Serializes ELF header, program headers, section headers, section data. Validates sizes. |
| `0x45C920` | `sub_45C920` | write_elf_to_file | small | HIGH | Wrapper calling `sub_45BF00` to serialize ELF to file. |
| `0x45C950` | `sub_45C950` | write_elf_to_memory | small | HIGH | Wrapper calling `sub_45BF00` to serialize ELF to buffer. |
| `0x448E70` | `sub_448E70` | elfw_section_table_build | 14.6KB | MEDIUM | Rebuilds section header table, computes offsets/sizes for final layout. |

> **Details**: [Pipeline Output](pipeline/output.md), [ELF Serialization](elf/serialization.md), [ELF Writer](structs/elf-writer.md)

---

## 9. Infrastructure

### Memory Arena / Allocator

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4307C0` | `sub_4307C0` | arena_alloc | 10.7KB | HIGH | Thread-safe (per-arena mutex). Small path: 625 free-list buckets at arena+2128 (8-byte aligned). Large path: page pool. Falls back to mmap via `sub_44ED60`. |
| `0x431000` | `sub_431000` | arena_free | 4.7KB | HIGH | Returns small blocks to free-list, large blocks to page pool. Checks `byte_2A5BAD0` debug flag. |
| `0x432020` | `sub_432020` | arena_create_named | 2.2KB | HIGH | Creates named arena. Called with "nvlink option parser", "nvlink memory space". |
| `0x431C70` | `sub_431C70` | arena_destroy | 3.6KB | HIGH | Optionally merges free-lists back into parent arena, or frees all pages via `sub_431EC0`. |
| `0x431770` | `sub_431770` | arena_dump_stats | 8.5KB | HIGH | Prints detailed arena statistics: page counts, block sizes, usage. |
| `0x4882A0` | `sub_4882A0` | ocg_memspace_alloc | 2.5KB | HIGH | OCG (On-Chip-Gen) slab/segregated-freelist allocator. 128 size-class buckets, 1MB page allocations. |
| `0x489140` | `sub_489140` | memspace_statistics_print | 4.4KB | HIGH | Prints "Memory space statistics for 'OCG mem space'". |
| `0x45CAC0` | `sub_45CAC0` | oom_handler | tiny | HIGH | Out-of-memory handler, calls abort path. |
| `0x45CAE0` | `sub_45CAE0` | arena_assert | tiny | HIGH | Arena validity assertion. |
| `0x44F410` | `sub_44F410` | arena_get_metadata | <2KB | HIGH | Look up allocation metadata for a pointer. |

> **Details**: [Memory Arenas](infra/memory-arenas.md)

### Diagnostics / Error Reporting

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x467460` | `sub_467460` | **error_emit** | ~2KB | VERY HIGH | Variadic error emission entry. First arg is always `&unk_2A5Bxxx` (error descriptor table entry). Dispatches to `sub_467A70`. |
| `0x467A70` | `sub_467A70` | diagnostic_report | 13.1KB | HIGH | Formats and emits diagnostics with severity prefixes: "warning ", "info    ", "error   ", "error*  ", "fatal   ". Location format: "%s, line %d; ". Handles suppression and warning-as-error. |
| `0x4B9E70` | `sub_4B9E70` | allocation_failure_handler | 5.1KB | HIGH | "An allocation failure occurred; heap memory may be exhausted." Also handles "Multiple errors:". |
| `0x4BC290` | `sub_4BC290` | elflink_error_handler | 2.5KB | HIGH | "elfLink: unexpected error". Error wrapper for ELF linking subsystem. |

> **Details**: [Error Reporting](infra/error-reporting.md), [Elflink Errors](reference/elflink-errors.md)

### Threading

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x43FDB0` | `sub_43FDB0` | thread_pool_create | small | HIGH | Creates pthread thread pool for split-compile. |
| `0x43FC80` | `start_routine` | thread_worker_entry | small | VERY HIGH | Named symbol. Thread pool worker entry point. |
| `0x44F260` | `destr_function` | tls_destructor | small | VERY HIGH | Named symbol. pthread TLS destructor for arena cleanup. |
| `0x44EF80` | `func` | atexit_cleanup | small | VERY HIGH | Named symbol. Registered via `__cxa_atexit` for process exit cleanup. |

> **Details**: [Thread Pool](infra/thread-pool.md)

### Compression (LZ4)

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x46EE00` | `sub_46EE00` | LZ4_decompress_safe_extDict | 81KB | HIGH | LZ4 decompression with external dictionary. SSE2 copy optimization. |
| `0x46C690` | `sub_46C690` | LZ4_decompress_safe | 20KB | HIGH | Basic LZ4 safe decompression (no dictionary). |
| `0x46FD50` | `sub_46FD50` | LZ4_compress | 13.7KB | HIGH | LZ4 compression with hash table match finding. |

### Knobs / Configuration System

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x49B1A0` | `sub_49B1A0` | knobs_file_read_parse | 59KB | HIGH | Reads knobsfile, parses "[knobs]" section header, processes key=value pairs. Source: `generic_knobs_impl.h`. |
| `0x49D8A0` | `sub_49D8A0` | parse_knob_value | 24KB | HIGH | Parses single knob value: integer, integer_range, integer_list, double, float, opcode, when-string, value_pair_list. |
| `0x49A0C0` | `sub_49A0C0` | knob_decode_and_apply | 14KB | MEDIUM | Decodes and applies knobs at pipeline stages: "After Decode", "After Expansion", "After WAR post-expansion", "After Opex". |
| `0x498FE0` | `sub_498FE0` | knob_inject_string | 8.7KB | HIGH | Injects string value into knob system. "Invalid knob specified (%s)". |

### GPU Architecture Profiles

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x484F50` | `sub_484F50` | **arch_profile_database_init** | 54KB | VERY HIGH | Registers all GPU architectures: sm_75 (Turing) through sm_121 (DGX Spark). Creates real/virtual/lto profiles. Sets capability vectors via XMM constants. Hash map at `qword_2A5F8D8`. Notable: sm_88 appears (new Ampere variant). "f" variants = forward-compatible. |
| `0x486FF0` | `sub_486FF0` | architecture_parse_name | 2.7KB | HIGH | Parses "sm_%2d%s", "compute_%2d%s", "sass_%2d%s" to numeric arch ID. |
| `0x487220` | `sub_487220` | architecture_name_format | 2.4KB | MEDIUM | Formats arch number back to name string. |
| `0x4709E0` | `sub_4709E0` | can_finalize_arch_check | 2.6KB | HIGH | Architecture compatibility for finalization. Maps 104->120, 130->107, 101->110. Returns error codes 24-30. |
| `0x470DA0` | `sub_470DA0` | can_finalize_capability | 2.1KB | HIGH | Finalization capability bitmask check. Maps target codes to bitmask: 'd'(100)=1, 'g'(103)=8, 'n'(110)=2, 'y'(121)=64. |

> **Details**: [Architecture Profiles](structs/arch-profile.md), [SM100 Blackwell](targets/sm100-blackwell.md), [SM103-SM121](targets/sm103-121.md), [Compatibility](targets/compatibility.md)

---

## 10. Debug Info / DWARF Processing

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x47CBC0` | `sub_47CBC0` | debug_line_decode_replay | 33KB | HIGH | DWARF .debug_line decoder/replayer. Initializes state machine, reads include directories and file tables. |
| `0x478A20` | `sub_478A20` | debug_line_info_encode | 28KB | HIGH | DWARF .debug_line header encoder: version, prologue_length, opcode_base, include_directories[], file_names[]. |
| `0x4783C0` | `sub_4783C0` | debug_line_program_serialize | 13KB | HIGH | Serializes DWARF line number program opcodes from individual CUs into combined section. |
| `0x480FB0` | `sub_480FB0` | debug_line_merge | 25KB | HIGH | Merges line number tables across compilation units using BST and "%llu_%llu_%llu" keys. |
| `0x482850` | `sub_482850` | debug_info_complex_merge | 36KB | MEDIUM | Full debug info section merge across CUs. |
| `0x404827` | `sub_404827` | debug_line_info_builder | 4.3KB | HIGH | Generates DWARF line info for inline functions: "%s+%llu", ".L__$locationLabel$__%d". |
| `0x4707D0` | `sub_4707D0` | debug_info_set_prefix_suffix | small | HIGH | Sets prefix/suffix strings for debug info section naming. |

> **Details**: [DWARF Processing](debug/dwarf-processing.md), [Line Tables](debug/line-tables.md), [NVIDIA Debug Extensions](debug/nvidia-extensions.md)

---

## 11. Embedded ptxas Backend (Compiler Functions)

> **Note**: The following sections document functions from the **embedded ptxas compiler backend**, which constitutes ~95% of the nvlink binary by code size. These are the same compiler passes found in the standalone ptxas binary; see the [ptxas wiki](../ptxas/function-map.html) for comprehensive documentation of the full 40,000-function compiler. This section covers only the most prominent functions visible from the linker's perspective.

### IR Node Primitives

The fundamental API for accessing IR instruction fields. `sub_530FB0` alone has 31,399 callers.

| Address | Decompiled | Proposed Name | Size | Tag | Description |
|---------|------------|---------------|------|-----|-------------|
| `0x530FB0` | `sub_530FB0` | IRNode_GetOperand | 16B | -- | `return *(a1+32) + 32 * index` (operand array, 32-byte stride) |
| `0x530FC0` | `sub_530FC0` | IRNode_GetNumSrcOperands | 16B | -- | `total_ops + 1 - first_src_index` |
| `0x530FD0` | `sub_530FD0` | IRNode_GetNumDstOperands | 16B | -- | `return *(a1 + 92)` |
| `0x530E80` | `sub_530E80` | IRNode_GetRegClass | 16B | -- | Identity extract (unsigned int) |
| `0x530F80` | `sub_530F80` | IRNode_GetDataType | 16B | -- | Identity extract for data type field |
| `0x530E90` | `sub_530E90` | IROperand_IsRegister | 16B | tag=2 | `return type == 2` |
| `0x530EA0` | `sub_530EA0` | IROperand_IsImmediate | 16B | tag=1 | `return type == 1` |
| `0x530EB0` | `sub_530EB0` | IROperand_IsMemRef | 16B | tag=6 | `return type == 6` |
| `0x530EC0` | `sub_530EC0` | IROperand_IsAddress | 16B | tag=10 | `return type == 10` |
| `0x530ED0` | `sub_530ED0` | IROperand_IsPredicate | 16B | tag=9 | `return type == 9` |
| `0x530EE0` | `sub_530EE0` | IROperand_IsCondCode | 16B | tag=5 | `return type == 5` |
| `0x530EF0` | `sub_530EF0` | IROperand_IsConstant | 16B | tag=4 | `return type == 4` |
| `0x530F00` | `sub_530F00` | IROperand_IsSymbol | 16B | tag=3 | `return type == 3` |
| `0x530F50` | `sub_530F50` | IROperand_IsBarrier | 16B | tag=7 | `return type == 7` |
| `0x530F90` | `sub_530F90` | IRNode_SetFlagA | 16B | -- | `*(a1 + 14) = a2` |
| `0x530FA0` | `sub_530FA0` | IRNode_SetFlagB | 16B | -- | `*(a1 + 15) = a2` |

> **Details**: [IR Nodes](ptxas/ir-nodes.md)

### NVInst Class Hierarchy (Instruction Representation)

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0xA49150` | `sub_A49150` | **NVInst_getOperandField** | 60B | VERY HIGH | 30,768 callers. Calls `sub_A7DE70` (hasOperand), then `sub_A709F0` (getValue). Returns -1 if field absent. |
| `0xA49120` | `sub_A49120` | NVInst_setOperandField | 16B | HIGH | Thunk to `sub_A5B6B0` (180KB switch dispatch). |
| `0xA491D0` | `sub_A491D0` | NVInst_setOperandImm | 16B | HIGH | Thunk to `sub_A62220` (65KB switch dispatch). |
| `0xA491E0` | `sub_A491E0` | NVInst_getOperandFieldSlot | 16B | HIGH | Thunk to `sub_A65900` (67KB switch dispatch). |
| `0xA49130` | `sub_A49130` | NVInst_getDefaultOperandValue | 16B | HIGH | Thunk to `sub_A67910` (141KB switch dispatch). |
| `0xA49190` | `sub_A49190` | NVInst_hasOperandField | 16B | HIGH | Direct wrapper for `sub_A7DE70`. |
| `0xA491A0` | `sub_A491A0` | NVInst_copyOperandField | 48B | HIGH | Gets from src via `sub_A709F0`, sets on dst via `sub_A5B6B0`. |
| `0xA49220` | `sub_A49220` | NVInst_lookupOpcodeDesc | 96B | HIGH | FNV-1a hash lookup in opcode descriptor table. |
| `0xA4AB10` | `sub_A4AB10` | NVInst_constructor | 11KB | HIGH | Initializes NVInst object with operand vector, hash tables, scheduling info. |

### Operand Dispatch Mega-Functions

Four giant switch-case functions that implement the complete operand field encoding/decoding dispatch. Each switches on opcode class ID (370+ classes).

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0xA5B6B0` | `sub_A5B6B0` | setOperandField_dispatch | 180KB | HIGH | Sets operand field value on instruction. Switch on opcode class (0x00-0x171). |
| `0xA62220` | `sub_A62220` | setOperandImm_dispatch | 65KB | HIGH | Sets immediate operand value. Same switch structure. |
| `0xA65900` | `sub_A65900` | getOperandField_dispatch | 67KB | HIGH | Gets operand field value for specific slot. |
| `0xA67910` | `sub_A67910` | getDefaultOperandValue_dispatch | 141KB | HIGH | Returns default value for an operand field. |
| `0xA709F0` | `sub_A709F0` | InstrFieldOffset_Query | ~180KB | HIGH | 6,491-line switch mapping (opcode_class, field_id) to bit-offset in instruction encoding. Returns -1 if absent. |
| `0xA7DE70` | `sub_A7DE70` | InstrFieldPresent_Query | ~170KB | HIGH | Same switch structure; returns `(extract != 0)`. Companion to `sub_A709F0`. |

### ISel Pattern Matching

#### SM50-SM7x ISel Hub (Maxwell/Pascal/Volta)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0x530FE0` | `0x530FE0`--`0x5B1AB0` | 1,293 | HIGH | Auto-generated pattern matchers. Signature: `(ctx, node, &opcode, &priority)`. Check attributes via `sub_A49150`, operand types/counts, output (target_opcode, priority). 152 distinct opcodes, 36 priority levels. |

#### SM75 ISel Hub (Turing)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0xF16150` | `0xF16150`--`0xFBB780` | 276 | HIGH | SM75 pattern matchers. Same signature. Calls `sub_A49150` for attributes, `sub_530FD0`/`sub_530FB0`/`sub_530FC0` for operand queries. |
| `0xFBB810` | -- | 280KB | HIGH | **SM75 ISel mega-hub dispatch**. Calls all 276 matchers, selects highest priority, dispatches to corresponding emitter. Too large to decompile. |

#### SM80 ISel Hub (Ampere)

| Address | Range | Count | Confidence | Description |
|---------|-------|-------|------------|-------------|
| `0xCE2000` | `0xCE2000`--`0xD60000` | 259 | HIGH | SM80 pattern matchers. 19 distinct instruction opcodes (HMMA, IMAD, FFMA, LDG, S2R, etc.). |

#### SM100+ ISel (Blackwell)

Blackwell ISel patterns are distributed across the encoding/decoding table regions. The dispatch tables at `0xE43C20` and `0xEFE6C0` use binary search on opcode fields to route to the correct encoder/decoder.

### Instruction Encoding Infrastructure

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x4C28B0` | `sub_4C28B0` | setBitfield | small | VERY HIGH | `setBitfield(buf, bit_offset, width, value)`. Core bitfield insertion into 128-bit instruction word at buf+544. |
| `0x4C2A60` | `sub_4C2A60` | encoding_init | small | HIGH | Clears operand remapping table (offsets 468-531), resets operand counter at 532. |
| `0x4C2A90` | `sub_4C2A90` | encode_predicate | small | HIGH | Encodes predicate guard register from IR node. |
| `0x4C4D60` | `sub_4C4D60` | encode_register_operand | small | HIGH | Encodes register operand: 1-bit is_output, 4-bit type, 10-bit register number. |
| `0x4C52F0` | `sub_4C52F0` | encode_immediate_operand | small | HIGH | Encodes constant/immediate operand: 5-bit type + register number. |
| `0x4C5C30` | `sub_4C5C30` | encode_special_operand | small | HIGH | Encodes predicate/condcode/memory operands with remapping. |
| `0x4C7D10` | `sub_4C7D10` | encoding_engine_main | 18.6KB | HIGH | Main encoding engine. String: "ENCODING". Converts IR to binary. |
| `0x4CB100` | `sub_4CB100` | decoding_engine_entry | 3.4KB | HIGH | Entry point for instruction decoding. String: "DECODING". |

#### Per-Architecture Encoding Tables

| Address Range | Target | Encoder Count | Description |
|---------------|--------|---------------|-------------|
| `0x603F60`--`0x61FA60` | SM50 | 79 | 64-bit instruction words (Maxwell). Format types 1/2/3. |
| `0x620000`--`0x84DD70` | SM100+ | 1,537 | 128-bit Blackwell SASS. Major opcodes 1/2/8. |
| `0xA87CE0`--`0xB25D50` | SM90 | 164 | 128-bit Hopper encoding. |
| `0xB9FDE0`--`0xC9EE60` | SM7x-SM89 | ~270 | Multi-arch encoders: SM70/75/80/86/89. |
| `0xDA0310`--`0xE436D0` | SM100+ | 438 | Blackwell encoders (second set). |

#### Per-Architecture Decoding Tables

| Address Range | Target | Decoder Count | Description |
|---------------|--------|---------------|-------------|
| `0x84DD70`--`0xA48290` | SM100+ | 1,613 | Instruction descriptor init functions. |
| `0xACECF0`--`0xB77B60` | SM90 | 139 | Hopper decoders. |
| `0xE43DC0`--`0xF15A50` | SM100+ | 648 | Blackwell decoders. |

### Bitvector Operations (SSE-Optimized)

Used by register allocation and liveness analysis throughout the backend.

| Address | Decompiled | Proposed Name | Size | Confidence |
|---------|------------|---------------|------|------------|
| `0x5E4470` | `sub_5E4470` | BitVector_AND | 3.2KB | HIGH |
| `0x5E4670` | `sub_5E4670` | BitVector_OR | 2.9KB | HIGH |
| `0x5E4810` | `sub_5E4810` | BitVector_ANDNOT | 4.4KB | HIGH |
| `0x5E4AE0` | `sub_5E4AE0` | BitVector_XOR | 2.6KB | MEDIUM |
| `0x5E51C0` | `sub_5E51C0` | BitVector_OR_Changed | 2.9KB | MEDIUM |
| `0x5E55E0` | `sub_5E55E0` | BitVector_PopCount | 5.4KB | MEDIUM |
| `0x5E5940` | `sub_5E5940` | BitVector_FindFirst | 3.0KB | MEDIUM |

### Peephole Optimization

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x406DC0` | `sub_406DC0` | peephole_optimizer_main | 6.8KB | MEDIUM | Main driver -- orchestrates multiple optimization passes on instruction buffer. |
| `0x407634` | `sub_407634` | peephole_instruction_combine | 5.3KB | MEDIUM | Combines dependent instruction pairs. 372-byte records, limit 20479. |
| `0x406377` | `sub_406377` | peephole_pattern_match | 7.4KB | MEDIUM | Matches and transforms instruction patterns. |
| `0x408594` | `sub_408594` | peephole_scheduler | 6.5KB | LOW | Instruction scheduling within basic blocks. |
| `0x407F94` | `sub_407F94` | peephole_constant_fold | 3.7KB | LOW | Constant propagation in instructions. |
| `0x407C0A` | `sub_407C0A` | peephole_strength_reduce | 3.2KB | LOW | Strength reduction (replace expensive ops with cheaper ones). |
| `0x4083A5` | `sub_4083A5` | peephole_dead_instruction_elim | 2.9KB | LOW | Removes dead instructions using liveness. |

> **Details**: [Peephole](ptxas/peephole.md)

### PTX Assembler Frontend (Embedded ptxas)

Large PTX processing subsystem in the `0x1430000`--`0x15C0000` range.

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x15B86A0` | `sub_15B86A0` | cuda_builtin_prototype_gen | 345KB | HIGH | Giant switch (~608 cases) generating PTX prototype strings for CUDA builtins: div, rem, rcp, sqrt, wmma, shfl, vote, tcgen05, bulk_copy, etc. |
| `0x147EF50` | `sub_147EF50` | ptx_instr_semantic_analyzer | 288KB | HIGH | Master instruction validator. SM version gates, texture modes, cache policies, state spaces, vector types, scoping. |
| `0x1487650` | `sub_1487650` | ptx_statement_processor | 240KB | MEDIUM | Top-level PTX statement handler. Processes `.maxnctapersm`, `.reqntid`, kernel parameter limits (4352 bytes), function prototypes. |
| `0x146BEC0` | `sub_146BEC0` | ptx_load_store_validator | 206KB | HIGH | Memory operation validator. Validates ld/st, atomics, reductions, fence, membar, cp.async, cache eviction, scope. |

### Embedded ptxas Option / Driver Interface

The embedded ptxas backend exposes its own argv-style option surface, separate from nvlink's CLI. These functions form the boundary between the linker driver and the embedded compiler.

| Address | Decompiled | Proposed Name | Size | Confidence | Description |
|---------|------------|---------------|------|------------|-------------|
| `0x1103030` | `sub_1103030` | ptxas_option_table_build | 29.8KB / 1,249 lines | HIGH | Builds the embedded-ptxas option definition table. Creates a fresh parser via `sub_42DFE0(0)`, registers every option via `sub_42F130` (long name, short name, type, default, help), then invokes `sub_42E5A0` to parse argc/argv. Handles `--trap-into-debugger`, `--tool-name`, `--help`, `--version`. |
| `0x1104950` | `sub_1104950` | ptxas_option_extract | 37.6KB / 1,208 lines | HIGH | Parses argv against the table from `sub_1103030` and extracts each option via `sub_42E390` into a compiler-state structure at base pointer `a3`. Each option writes to a fixed byte offset; extraction order is strict and includes validation. |
| `0x1112F30` | `sub_1112F30` | ptxas_compile_driver | 65.0KB / 2,164 lines | HIGH | Embedded-ptxas compilation driver -- the consumer of the option state populated by `sub_1104950`. Top-level orchestrator for PTX-to-SASS within the linker. |
| `0x1100E50` | `sub_1100E50` | ptxas_feature_configurator | 13.8KB / 451 lines | HIGH | Feature flag configurator. Translates the parsed option state into the embedded compiler's internal feature switches before the driver runs. |

> **Details**: [PTX Parsing](ptxas/ptx-parsing.md), [Embedded ptxas Overview](ptxas/overview.md), [ptxas Options](config/ptxas-options.md)

---

## Statistics

| Metric | Value |
|--------|-------|
| Binary file size | ~37 MB |
| `.text` section size | 25.2 MB |
| Total functions (IDA) | 40,532 |
| Linker core functions (`0x400000`--`0x4C0000`) | ~1,900 |
| Embedded ptxas backend functions | ~38,000 |
| Functions documented on this page | ~160 key functions |
| ISel pattern matchers (all arches) | ~2,100+ |
| Instruction encoders (all arches) | ~2,500+ |
| Instruction descriptor inits | ~1,600+ |
| Instruction decoders (all arches) | ~800+ |
| Subsystems identified | 20 |
| Largest function | `sub_15B86A0` cuda_builtin_prototype_gen (345KB) |
| Most-called function | `sub_530FB0` IRNode_GetOperand (31,399 callers) |
| Binary composition | ~5% linker, ~95% embedded compiler backend |

---

## Address Map (sorted)

Quick reference sorted by address for binary navigation. All addresses verified against decompiled files in `nvlink/decompiled/`.

```
0x404827  sub_404827  debug_line_info_builder
0x406377  sub_406377  peephole_pattern_match
0x406DC0  sub_406DC0  peephole_optimizer_main
0x407634  sub_407634  peephole_instruction_combine
0x407C0A  sub_407C0A  peephole_strength_reduce
0x407F94  sub_407F94  peephole_constant_fold
0x4083A5  sub_4083A5  peephole_dead_instruction_elim
0x408594  sub_408594  peephole_scheduler
0x409800  main        main
0x40C4F0  ctor_001    ctor_thread_infra
0x40C5C0  ctor_002    ctor_002
0x410830  ctor_003    ctor_003
0x410850  ctor_004    ctor_004
0x412750  ctor_005    ctor_knob_table
0x412790  ctor_006    ctor_006
0x426260  ctor_008    ctor_version_constants
0x426280  ctor_009    ctor_009
0x4262B0  ctor_010    ctor_010
0x426330  ctor_011    ctor_011
0x4264B0  sub_4264B0  split_compile_dispatch
0x426570  sub_426570  validate_arch_and_merge
0x426AE0  sub_426AE0  lto_mark_used_symbols
0x426CD0  sub_426CD0  lto_collect_ir_modules
0x429BA0  sub_429BA0  lto_ptxas_options_build
0x4275C0  sub_4275C0  post_link_transform (FNLZR)
0x4279C0  sub_4279C0  trace_phase
0x427AE0  sub_427AE0  nvlink_parse_options
0x42A680  sub_42A680  register_module
0x42AF40  sub_42AF40  extract_fatbin_member
0x42DBC0  sub_42DBC0  option_validate_value
0x42DFE0  sub_42DFE0  option_parser_create
0x42E390  sub_42E390  option_get_value
0x42E5A0  sub_42E5A0  option_parse_argv
0x42F130  sub_42F130  option_register
0x42FCB0  sub_42FCB0  create_temp_file
0x4307C0  sub_4307C0  arena_alloc
0x431000  sub_431000  arena_free
0x431770  sub_431770  arena_dump_stats
0x431C70  sub_431C70  arena_destroy
0x432020  sub_432020  arena_create_named
0x432B10  sub_432B10  merge_overlapping_global
0x4339A0  sub_4339A0  optimize_constant_dedup
0x4343C0  sub_4343C0  merge_overlapping_constant
0x436BD0  sub_436BD0  shared_memory_optimizer
0x437E20  sub_437E20  merge_overlapping_local
0x438640  sub_438640  merge_constant_bank_data
0x438DD0  sub_438DD0  process_bindless_references
0x439830  sub_439830  shared_memory_layout
0x43FC80  start_routine  thread_worker_entry
0x43FDB0  sub_43FDB0  thread_pool_create
0x440740  sub_440740  elfw_add_section
0x440BE0  sub_440BE0  elfw_add_section_with_data
0x4411F0  sub_4411F0  elfw_copy_section
0x441AC0  sub_441AC0  elfw_add_reloc_section
0x4438F0  sub_4438F0  elfw_create
0x442820  sub_442820  elfw_merge_symbols
0x442CA0  sub_442CA0  elfw_add_symbol
0x445000  sub_445000  finalize_elf
0x4475B0  sub_4475B0  elfw_destroy
0x4478F0  sub_4478F0  elfw_dump_structure
0x448360  sub_448360  elfw_get_section_header
0x4489C0  sub_4489C0  hash_table_create
0x448E70  sub_448E70  elfw_section_table_build
0x449A80  sub_449A80  LinkerHash_lookup
0x44A5D0  sub_44A5D0  callgraph_detect_recursion
0x44AD40  sub_44AD40  dead_code_elimination
0x44C030  sub_44C030  callgraph_traverse
0x44CCF0  sub_44CCF0  callgraph_dump_dot
0x44D200  sub_44D200  build_callgraph_section
0x44EF80  func        atexit_cleanup
0x44F260  destr_function  tls_destructor
0x44F410  sub_44F410  arena_get_metadata
0x450ED0  sub_450ED0  propagate_register_counts
0x451D80  sub_451D80  compute_entry_properties
0x459640  sub_459640  reloc_vtable_create
0x45BAA0  sub_45BAA0  write_elf_section
0x45BF00  sub_45BF00  write_elf_to_buffer
0x45C920  sub_45C920  write_elf_to_file
0x45C950  sub_45C950  write_elf_to_memory
0x45CAC0  sub_45CAC0  oom_handler
0x45CAE0  sub_45CAE0  arena_assert
0x45CCD0  sub_45CCD0  timing_start
0x45D180  sub_45D180  merge_weak_function
0x45E7D0  sub_45E7D0  merge_elf
0x462620  sub_462620  path_split
0x4637B0  sub_4637B0  uft_reorder_entries
0x463F70  sub_463F70  uft_setup_sections
0x464460  sub_464460  linked_list_append
0x467460  sub_467460  error_emit
0x467A70  sub_467A70  diagnostic_report
0x4685B0  sub_4685B0  reloc_bitfield_write
0x468670  sub_468670  reloc_bitfield_extract
0x468760  sub_468760  reloc_action_dispatcher
0x469D60  sub_469D60  apply_relocations
0x46ADC0  sub_46ADC0  emit_resolved_relocations
0x46C690  sub_46C690  LZ4_decompress_safe
0x46EE00  sub_46EE00  LZ4_decompress_safe_extDict
0x46FD50  sub_46FD50  LZ4_compress
0x4707D0  sub_4707D0  debug_info_set_prefix_suffix
0x4709E0  sub_4709E0  can_finalize_arch_check
0x470DA0  sub_470DA0  can_finalize_capability
0x471700  sub_471700  nvlink_finalize_object
0x4748F0  sub_4748F0  nvlink_link_and_finalize
0x4783C0  sub_4783C0  debug_line_program_serialize
0x478A20  sub_478A20  debug_line_info_encode
0x47CBC0  sub_47CBC0  debug_line_decode_replay
0x480FB0  sub_480FB0  debug_line_merge
0x482850  sub_482850  debug_info_complex_merge
0x484F50  sub_484F50  arch_profile_database_init
0x486FF0  sub_486FF0  architecture_parse_name
0x487220  sub_487220  architecture_name_format
0x4876A0  sub_4876A0  archive_signature_check
0x487C20  sub_487C20  archive_open
0x487E10  sub_487E10  archive_iterate_members
0x4882A0  sub_4882A0  ocg_memspace_alloc
0x489140  sub_489140  memspace_statistics_print
0x491410  sub_491410  compilation_unit_initialize
0x498FE0  sub_498FE0  knob_inject_string
0x49A0C0  sub_49A0C0  knob_decode_and_apply
0x49B1A0  sub_49B1A0  knobs_file_read_parse
0x49D8A0  sub_49D8A0  parse_knob_value
0x4A4DC0  sub_4A4DC0  merc_war_process
0x4A8690  sub_4A8690  merc_opex_expand
0x4AF3C0  sub_4AF3C0  hrk_section_process
0x4B02A0  sub_4B02A0  hrc_hrd_section_process
0x4B9E70  sub_4B9E70  allocation_failure_handler
0x4BC290  sub_4BC290  elflink_error_handler
0x4BC4A0  sub_4BC4A0  nvvm_api_wrapper_init
0x4BC6F0  sub_4BC6F0  nvvm_compile_and_extract
0x4BD0A0  sub_4BD0A0  nvvm_compile_driver
0x4BD240  sub_4BD240  cubin_post_process
0x4BD4E0  sub_4BD4E0  ptx_compile_whole_program
0x4BD760  sub_4BD760  ptxas_compile_split
0x4C28B0  sub_4C28B0  setBitfield
0x4C2A60  sub_4C2A60  encoding_init
0x4C2A90  sub_4C2A90  encode_predicate
0x4C4D60  sub_4C4D60  encode_register_operand
0x4C52F0  sub_4C52F0  encode_immediate_operand
0x4C5C30  sub_4C5C30  encode_special_operand
0x4C7D10  sub_4C7D10  encoding_engine_main
0x4CB100  sub_4CB100  decoding_engine_entry
0x4CE8C0  sub_4CE8C0  ptx_version_parse_validate
0x4CFBD0  sub_4CFBD0  ptx_obfuscation_transform
0x50C790  sub_50C790  getReuse
0x530E80  sub_530E80  IRNode_GetRegClass
0x530E90  sub_530E90  IROperand_IsRegister
0x530EA0  sub_530EA0  IROperand_IsImmediate
0x530EB0  sub_530EB0  IROperand_IsMemRef
0x530EC0  sub_530EC0  IROperand_IsAddress
0x530ED0  sub_530ED0  IROperand_IsPredicate
0x530EE0  sub_530EE0  IROperand_IsCondCode
0x530EF0  sub_530EF0  IROperand_IsConstant
0x530F00  sub_530F00  IROperand_IsSymbol
0x530F50  sub_530F50  IROperand_IsBarrier
0x530F80  sub_530F80  IRNode_GetDataType
0x530F90  sub_530F90  IRNode_SetFlagA
0x530FA0  sub_530FA0  IRNode_SetFlagB
0x530FB0  sub_530FB0  IRNode_GetOperand
0x530FC0  sub_530FC0  IRNode_GetNumSrcOperands
0x530FD0  sub_530FD0  IRNode_GetNumDstOperands
0x5E4470  sub_5E4470  BitVector_AND
0x5E4670  sub_5E4670  BitVector_OR
0x5E4810  sub_5E4810  BitVector_ANDNOT
0x5E4AE0  sub_5E4AE0  BitVector_XOR
0x5E51C0  sub_5E51C0  BitVector_OR_Changed
0x5E55E0  sub_5E55E0  BitVector_PopCount
0x5E5940  sub_5E5940  BitVector_FindFirst
0x5E7B90  sub_5E7B90  MercExpand_BuildNodeMaps
0x5E8710  sub_5E8710  MercExpand_BuildFullCFGMaps
0x5EA250  sub_5EA250  CFG_DumpDOTGraph
0x5EA4F0  sub_5EA4F0  MercExpand_InvalidateRegState
0x5F38E0  sub_5F38E0  MercExpand_HandleInstruction
0x5F60E0  sub_5F60E0  IRTree_Walk
0x5F8B60  sub_5F8B60  MercExpand_ApplyResConstraints
0x5FC6B0  sub_5FC6B0  MercExpand_ExpandMOV
0x5FCE20  sub_5FCE20  MercExpand_ExpandRETURN
0x5FDDB0  sub_5FDDB0  MercExpand_Dispatch
0xA49120  sub_A49120  NVInst_setOperandField
0xA49130  sub_A49130  NVInst_getDefaultOperandValue
0xA49150  sub_A49150  NVInst_getOperandField
0xA49190  sub_A49190  NVInst_hasOperandField
0xA491A0  sub_A491A0  NVInst_copyOperandField
0xA491D0  sub_A491D0  NVInst_setOperandImm
0xA491E0  sub_A491E0  NVInst_getOperandFieldSlot
0xA49220  sub_A49220  NVInst_lookupOpcodeDesc
0xA4AB10  sub_A4AB10  NVInst_constructor
0xA50D10  sub_A50D10  encode_GPR
0xA5B6B0  sub_A5B6B0  setOperandField_dispatch
0xA62220  sub_A62220  setOperandImm_dispatch
0xA65900  sub_A65900  getOperandField_dispatch
0xA67910  sub_A67910  getDefaultOperandValue_dispatch
0xA709F0  sub_A709F0  InstrFieldOffset_Query
0xA7DE70  sub_A7DE70  InstrFieldPresent_Query
0x146BEC0 sub_146BEC0 ptx_load_store_validator
0x147EF50 sub_147EF50 ptx_instr_semantic_analyzer
0x1487650 sub_1487650 ptx_statement_processor
0x15B86A0 sub_15B86A0 cuda_builtin_prototype_gen
0x1100E50 sub_1100E50 ptxas_feature_configurator
0x1103030 sub_1103030 ptxas_option_table_build
0x1104950 sub_1104950 ptxas_option_extract
0x1112F30 sub_1112F30 ptxas_compile_driver
```
