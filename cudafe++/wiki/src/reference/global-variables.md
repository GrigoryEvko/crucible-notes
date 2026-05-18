# Global Variable Index

cudafe++ v13.0 uses approximately 400+ global variables scattered across the `.bss` and `.data` segments. These variables fall into clear functional categories: compilation mode selectors, error/diagnostic state, I/O handles, CUDA-specific flags, translation unit management, scope tracking, IL allocation, lexer state, template instantiation, lambda transforms, and memory management. Every address listed below was confirmed through binary analysis of the x86-64 Linux ELF shipped with CUDA Toolkit 13.0 (8,910,936 bytes; see [Binary Layout](../binary-layout.md) for the canonical identity table). This page serves as the canonical cross-reference for all other wiki articles. Confidence: HIGH (addresses are decompiler-direct).

The variables cluster into three address regions: `0x106xxxx` (NVIDIA-added configuration flags, typically set during CLI processing), `0x126xxxx` (EDG core compiler state, used throughout parsing, IL generation, and code emission), and `0x12Cxxxx` / `0x128xxxx` (template instantiation, lambda transform, and arena allocator state). A few tables live in the read-only `.rodata` segment at `0xE6xxxx`--`0xE8xxxx`.

## Compilation Mode and Language Standard

These globals control the fundamental compilation dialect -- C vs C++, which standard version, which vendor extensions are active, and whether the compiler is in CUDA mode.

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_126EFB4` | 4 | `language_mode` | Master dialect selector. `1` = C, `2` = C++. Checked in virtually every subsystem. In some contexts (p1.12) interpreted as `device_il_mode` when value is 2. |
| `dword_126EF68` | 4 | `cpp_standard_version` | `__cplusplus` value. `199711` = C++98, `201103` = C++11, `201402` = C++14, `201703` = C++17, `202002` = C++20, `202302` = C++23. For C mode: `199000` (pre-C99), `199901` (C99), `201112` (C11), `201710` (C17), `202311` (C23). |
| `dword_126EFAC` | 4 | `extended_features` | EDG extended features / GNU compatibility mode flag. Also used as CUDA mode indicator in several paths. |
| `dword_126EFA8` | 4 | `gcc_extensions` | GCC extensions mode (`1` = enabled). Also used as GPU compilation mode flag in device/host separation. |
| `dword_126EFA4` | 4 | `clang_extensions` | Clang extensions mode. Dual-use: also serves as device-code-mode flag during device/host separation (`1` = compiling device side). |
| `dword_126EFB0` | 4 | `gnu_extensions_enabled` | GNU extensions active (set alongside `dword_126EFA8`). Also used as `strict_c_mode` and `relaxed_constexpr` in some paths. |
| `qword_126EF98` | 8 | `gcc_version` | GCC compatibility version, encoded as `major*10000+minor*100+patch`. Default `80100` (GCC 8.1.0). Compared as hex thresholds (e.g., `0x9E97` = 40599). |
| `qword_126EF90` | 8 | `clang_version` | Clang compatibility version. Default `90100`. Used for feature gating (compared against `0x78B3`, `0x15F8F`, `0x1D4BF`). |
| `qword_126EF78` | 8 | `msvc_version` | MSVC compatibility version. Default `1926`. |
| `qword_126EF70` | 8 | `version_threshold_max` | Upper version bound. Default `99999`. |
| `dword_126EF64` | 4 | `cpp_extensions_enabled` | C extension level (nonstandard extensions). |
| `dword_126EF80` | 4 | `feature_flag_80` | Miscellaneous feature flag, default `1`. |
| `dword_126EF48` | 4 | `auto_parameter_mode` | Auto parameter support flag (inverse of input). |
| `dword_126EF4C` | 4 | `auto_parameter_support` | Auto-parameter enabled (C++20 auto function params). |
| `dword_126EEFC` | 4 | `digit_separators_enabled` | C++14 digit separator (`'`) support. |
| `dword_126EF0C` | 4 | `feature_flag_0C` | Miscellaneous feature flag, default `1`. |
| `dword_126E4A8` | 4 | `sm_architecture` | Target SM architecture version (set by `--nv_arch` / case 245). |
| `dword_126E498` | 4 | `signed_chars` | Whether plain `char` is signed. |

## CUDA-Specific Flags

Flags controlling CUDA-specific behavior: device code generation, extended lambdas, relaxed constexpr, OptiX mode.

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_1065850` | 4 | `device_stub_mode` | Device stub mode toggle. Toggled by expression `dword_1065850 = (dword_1065850 == 0)` in `gen_routine_decl`. `0` = forwarding body pass, `1` = static stub pass. |
| `dword_106BF38` | 4 | `extended_lambda_mode` | NVIDIA extended lambdas enabled (`--expt-extended-lambda`). Gates the lambda wrapper generation pipeline. |
| `dword_106BF40` | 4 | `lambda_host_device_mode` | Lambda host-device mode flag. Controls whether `__device__` function references are allowed in host code. |
| `dword_106BF34` | 4 | `lambda_validation_skip` | Skip lambda validation checks. |
| `dword_106BFDC` | 4 | `skip_device_only` | Skip device-only code generation. When clear, deferred function list accumulates at `qword_1065840`. |
| `dword_106BFF0` | 4 | `relaxed_attribute_mode` | NVIDIA relaxed override mode. Controls permissive `__host__`/`__device__` attribute mismatch handling. Default `1` in CLI defaults. |
| `dword_106BFBC` | 4 | `whole_program_mode` | Whole-program mode (affects deferred function list behavior). |
| `dword_106BFD0` | 4 | `device_registration` | Enable CUDA device registration / cross-space reference checking. |
| `dword_106BFCC` | 4 | `constant_registration` | Enable CUDA constant registration / another cross-space check flag. |
| `dword_106BFB8` | 4 | `emit_symbol_table` | Emit symbol table in output. |
| `dword_106BF6C` | 4 | `alt_host_compiler_mode` | Alternative host compiler mode. |
| `dword_106BF68` | 4 | `host_compiler_flag` | Host compiler attribute support flag. Also `dword_106BF58`. |
| `dword_106BDD8` | 4 | `optix_mode` | OptiX compilation mode flag. |
| `dword_106B670` | 4 | `optix_kernel_index` | OptiX kernel index (combined with `dword_106BDD8` for error 3689). |
| `qword_106B678` | 8 | `optix_kernel_table` | OptiX kernel info table pointer. |
| `dword_106C2C0` | 4 | `gpu_mode` | GPU/device compilation mode. Controls `reinterpret_cast` semantics, pointer dereference, and keyword detection in device context. |
| `dword_106C1D8` | 4 | `relaxed_constexpr_ptr` | Controls pointer dereference in device constexpr (`--expt-relaxed-constexpr` related). |
| `dword_106C1E0` | 4 | `device_typeid` | Controls `typeid` availability in device constexpr context. |
| `dword_106C1F4` | 4 | `device_class_lookup` | CUDA device class member lookup flag. |
| `dword_E7C760` | 4[6] | `exec_space_table` | Execution space bitmask table (6 entries). `a1 & dword_E7C760[a2]` tests space compatibility. |
| `dword_106B640` | 4 | `keep_in_il_active` | Assertion guard: set to `1` before `keep_in_il` walk, cleared to `0` after. |
| `dword_E85700` | 4 | `host_runtime_included` | Flag: `host_runtime.h` already included in `.int.c` output. |
| `dword_126E270` | 4 | `cpp17_noexcept_type` | C++17 noexcept-in-type-system flag. Gates noexcept variant emission for lambda wrappers. |
| `dword_106BF80` | 4-ptr | `module_id_file` | Module-ID file path (for CRC32 calculation). |
| `qword_1065840` | 8 | `deferred_function_list` | Linked list of deferred functions (used when `dword_106BFDC` is clear). |

## Error and Diagnostic State

The diagnostic subsystem uses a set of globals to track error/warning counts, severity thresholds, output format, and per-error suppression state.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126ED90` | 8 | `error_count` | Total errors emitted. Also used as error-recovery-mode flag (nonzero = in recovery). |
| `qword_126ED98` | 8 | `warning_count` | Total warnings emitted. |
| `qword_126EDF0` | 8 | `error_output_stream` | `FILE*` for diagnostic output. Default `stderr`. Initialized during `ctor_002`. |
| `qword_126EDE8` | 8 | `current_source_position` | Current source position for error reporting. Mirrored from `qword_1065810`. |
| `qword_126ED60` | 8 | `error_limit` | Maximum error count before abort. |
| `byte_126ED69` | 1 | `min_severity_threshold` | Minimum severity for diagnostic output (default threshold). |
| `byte_126ED68` | 1 | `error_promotion_threshold` | Severity at or above which warnings become errors. |
| `dword_126ED40` | 4 | `suppress_assertion_output` | Suppress assertion output flag. |
| `dword_126ED48` | 4 | `no_catastrophic_on_error` | Disable catastrophic error on internal assertion. |
| `dword_126ED50` | 4 | `no_caret_diagnostics` | Disable caret (^) diagnostics. |
| `dword_126ED58` | 4 | `max_context_lines` | Maximum source context lines in diagnostics. |
| `dword_126ED78` | 4 | `has_error_in_scope` | Error occurred in current scope. |
| `dword_126ED44` | 4 | `name_lookup_kind` | Name lookup kind for diagnostic formatting. |
| `byte_126ED55` | 1 | `device_severity_override` | Default severity for device-mode diagnostics. |
| `byte_126ED56` | 1 | `warning_level_control` | Warning level control byte. |
| `dword_106BBB8` | 4 | `output_format` | Output format selector. `0` = plaintext, `1` = SARIF JSON. |
| `dword_106C088` | 4 | `warnings_are_errors` | Treat warnings as errors (`-Werror` equivalent). |
| `dword_126ECA0` | 4 | `colorization_requested` | Color output requested. |
| `dword_126ECA4` | 4 | `colorization_active` | Color output currently active (after TTY detection). |
| `off_88FAA0` | 8[3795] | `error_message_table` | Array of 3,795 `const char*` pointers indexed by error code. |
| `byte_1067920` | 1[3795] | `default_severity_table` | Default severity for each error code. |
| `byte_1067921` | 1[3795] | `current_severity_table` | Current (possibly pragma-modified) severity. |
| `byte_1067922` | 4[3795] | `per_error_flags` | Per-error tracking: bit 0 = first occurrence, other bits = suppression state. |
| `off_D481E0` | -- | `label_fill_in_table` | Diagnostic label fill-in table (`{name, cond_index, default_index}` entries). |
| `qword_106B488` | 8 | `message_text_buffer` | Growable message text buffer (initial 0x400 bytes via `sub_6B98A0`). |
| `qword_106B480` | 8 | `location_prefix_buffer` | Location prefix buffer (initial 0x80 bytes). |
| `qword_106B478` | 8 | `sarif_json_buffer` | SARIF JSON output buffer (initial 0x400 bytes). |
| `dword_106B470` | 4 | `terminal_width` | Terminal width for word wrapping. |
| `dword_106B4A0` | 4 | `fill_in_alloc_count` | Fill-in entry allocation counter. |
| `qword_106B490` | 8 | `fill_in_free_list` | Free list for 40-byte fill-in entries. |
| `dword_106B4B0` | 4 | `catastrophic_error_guard` | Re-entry guard for catastrophic error processing. |
| `dword_1065928` | 4 | `assertion_reentry_guard` | Re-entry guard for assertion handler. |
| `qword_1067860` | 8 | `entity_formatter_callback` | Entity name formatting callback (`sub_5B29C0`). |
| `qword_1067870` | 8 | `entity_formatter_buffer` | Entity formatter output buffer. |
| `byte_10678F1` | 1 | `diag_is_c_mode` | Diagnostic C mode flag (`dword_126EFB4 == 1`). |
| `byte_10678F4` | 1 | `diag_is_pre_cpp11` | Diagnostic pre-C++11 flag. |
| `byte_10678FA` | 1 | `diag_name_lookup_kind` | Name lookup kind for entity display. |
| `qword_106BCD8` | 8 | `suppress_all_but_fatal` | When set, suppress all errors except 992 (fatal). |
| `dword_106BCD4` | 4 | `predefined_macro_file_mode` | Predefined macro file mode (affects error case). |
| `qword_10658F8` | 8 | `pragma_scratch_buffer` | Scratch buffer for pragma bsearch operations. |
| `dword_106B4BC` | 4 | `werror_emitted_guard` | Prevents recursion in warnings-as-errors emission. |

## I/O and File Management

Globals controlling input/output filenames, streams, include paths, and preprocessor output.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126EEE0` | 8 | `input_filename` | Current output/source filename (write-protected name). Compared against `"-"` for stdout mode. |
| `qword_106BF20` | 8 | `output_filename_override` | Output C file path (set by `--gen_c_file_name` / case 45). |
| `qword_106C040` | 8 | `output_filename_alt` | Alternative output filename (used in signoff). |
| `qword_106C280` | 8 | `output_file` | `FILE*` for `.int.c` output (stdout or file). |
| `qword_126EE98` | 8 | `include_path_list` | Include search path linked list head. |
| `qword_126F100` | 8 | `include_path_free_list` | Free list for recycled search path nodes. |
| `qword_126F0E8` | 8 | `path_normalize_buffer` | Growable buffer for path normalization (0x100 initial). |
| `dword_126EE58` | 4 | `backslash_as_separator` | Backslash as path separator (Windows mode). |
| `dword_126EE54` | 4 | `windows_drive_letter` | Recognize Windows drive-letter paths. |
| `dword_126EEE8` | 4 | `bom_detection_enabled` | Byte-order mark detection enabled. |
| `dword_126F110` | 4 | `once_guard` | One-time initialization guard for source file processing. |
| `qword_126F0C0` | 8 | `cached_module_id` | Cached module ID string (CRC32-based). |
| `qword_106BF80` | 8 | `module_id_file_path` | Module-ID file path for external ID override. |
| `qword_106C038` | 8 | `options_hash_input` | Command-line options hash input for module ID. |
| `qword_106C248` | 8 | `macro_alias_map` | Hash table: macro define/alias mappings. |
| `qword_106C240` | 8 | `include_path_map` | Include path list for CLI processing. |
| `qword_106C238` | 8 | `sys_include_map` | System include path map. |
| `qword_106C228` | 8 | `sys_include_map_2` | Additional system include map. |
| `dword_106C29C` | 4 | `preprocess_mode` | Preprocessing-only mode (`1` = active). Set by CLI cases 3,4. |
| `dword_106C294` | 4 | `no_line_commands` | Suppress `#line` directives in output. |
| `dword_106C288` | 4 | `preprocess_output_mode` | Preprocess output: `0` = suppress, `1` = emit preprocessed text. |
| `dword_106C254` | 4 | `skip_backend` | Skip backend code generation entirely. |

## Scope Stack

The scope stack is an array of 784-byte entries at `qword_126C5E8`, indexed by `dword_126C5E4`. It tracks the nested scope hierarchy (file, namespace, class, function, block, template).

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126C5E8` | 8 | `scope_table_base` | Base pointer to scope stack array. Each entry is 784 bytes. |
| `dword_126C5E4` | 4 | `current_scope_index` | Current top-of-stack index. |
| `dword_126C5DC` | 4 | `saved_scope_index` | Saved scope index (for enum processing, lambda nesting). |
| `dword_126C5D8` | 4 | `function_scope_index` | Enclosing function scope index (`-1` if none). |
| `dword_126C5C8` | 4 | `template_scope_index` | Template scope index (`-1` if not in template). |
| `dword_126C5C4` | 4 | `class_scope_index` | Class/nested-class scope index (`-1` if none). Also used as `friend_scope_index` in some paths. |
| `dword_126C5BC` | 4 | `lambda_body_flag` | Lambda body processing flag / template declaration flag. |
| `dword_126C5B8` | 4 | `class_nesting_depth` | Class nesting depth / `is_member_of_template` flag. |
| `dword_126C5B4` | 4 | `block_scope_counter` | Block scope counter / namespace scope parameter. |
| `dword_126C5AC` | 4 | `saved_depth_template` | Saved scope depth for template instantiation restore. |
| `dword_126C5E0` | 4 | `scope_hash` | Scope hash/identifier. |
| `dword_126C5A4` | 4 | `nesting_scope_index` | Nesting scope index. |
| `dword_126C5A0` | 4 | `scope_misc_flag` | Miscellaneous scope flag. |
| `dword_126C5C0` | 4 | `instantiation_scope_index` | Instantiation scope index. |
| `qword_126C5D0` | 8 | `current_routine_ptr` | Current enclosing function/routine descriptor pointer. Used for execution space checks (offset `+32` -> byte `+177` bit 2 for device, byte `+182 & 0x30` for space mask). |
| `qword_126C598` | 8 | `pack_expansion_context` | Pack expansion context pointer (C++17). |
| `qword_126C590` | 8 | `symbol_hash_table` | Robin Hood hash table for symbol lookup within scope. |

## Lexer and Token State

The lexer maintains its current token, source position, and preprocessor state in these globals.

| Address | Size | Name | Description |
|---|---|---|---|
| `word_126DD58` | 2 | `current_token` | Current token kind (357 possible values). Key values: `7` = identifier, `33` = comma, `55` = semicolon, `56` = `=`, `67` = equals, `73` = CUDA token, `76` = `*`, `142` = `__attribute__`, `161` = `this`, `187` = requires clause. |
| `qword_126DD38` | 8 | `token_source_position` | Source position of current token. |
| `qword_126DD48` | 8 | `token_text_ptr` | Pointer to current identifier/literal text. |
| `dword_126DF90` | 4 | `token_flags_1` | Token flags / current declaration counter. |
| `dword_126DF8C` | 4 | `token_flags_2` | Secondary token flags. |
| `qword_126DF80` | 8 | `token_extra_data` | Token extra data pointer. |
| `dword_126DB74` | 4 | `has_cached_tokens` | Cached token state flag. |
| `dword_126DB58` | 4 | `digit_separator_seen` | C++14 digit separator seen during number scanning. |
| `qword_126DDA0` | 8 | `input_position` | Current position in input buffer. |
| `qword_126DDD8` | 8 | `input_buffer_base` | Input buffer base address. |
| `qword_126DDD0` | 8 | `input_buffer_end` | Input buffer end address. |
| `dword_126DDA8` | 4 | `line_counter` | Current line number in input. |
| `dword_126DDBC` | 4 | `source_line_number` | Source line number (for `#line` directive tracking). |
| `qword_126DD80` | 8 | `active_macro_chain` | Active macro expansion chain head. |
| `qword_126DD60` | 8 | `macro_expansion_marker` | Macro expansion position marker. |
| `dword_126DD30` | 4 | `in_directive_flag` | Currently processing preprocessor directive. |
| `qword_126DD18` | 8 | `current_macro_node` | Current macro being expanded. |
| `qword_126DD70` | 8 | `macro_tracking_1` | Macro position tracking state. |
| `qword_126DDE0` | 8 | `macro_tracking_2` | Secondary macro tracking state. |
| `qword_126DDF0` | 8 | `file_stack` | Include file stack (for `#include` nesting). |
| `dword_126DDE8` | 4 | `preproc_state_1` | Preprocessor state variable. |
| `dword_126E49C` | 4 | `preproc_state_2` | Preprocessor state variable. |
| `qword_126DB40` | 8 | `lexical_state_stack` | Lexical state save/restore stack (linked list of 80-byte nodes). |
| `qword_126DB48` | 8 | `stop_token_table` | Stop token table: 357 entries at offset `+8`, indexed by token kind. |
| `qword_126DD98` | 8 | `raw_string_state` | Raw string literal tracking state. |
| `dword_126EF00` | 4 | `raw_string_flag` | Raw string literal processing flag. |
| `qword_126DDD8` | 8 | `raw_string_base` | Raw string buffer base. |
| `qword_126DDD0` | 8 | `raw_string_end` | Raw string buffer end. |

## Preprocessor and Macro System

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_1270140` | 8 | `macro_definition_chain` | Macro definition chain head. |
| `qword_1270148` | 8 | `free_token_list` | Free list for recycled token nodes. |
| `qword_1270150` | 8 | `cached_token_list` | Cached token list head (for rescan). |
| `qword_1270128` | 8 | `reusable_cache_stack` | Reusable macro cache stack. |
| `qword_106B8A0` | 8 | `pending_macro_arg` | Pending macro argument pointer. |
| `dword_106B718` | 4 | `suppress_pragma_mode` | Suppress pragma processing mode. |
| `dword_106B720` | 4 | `preprocessing_mode` | Preprocessor-only mode active. |
| `dword_106B6EC` | 4 | `line_numbering_state` | Line numbering state for `#line` output. |
| `qword_106B740` | 8 | `pragma_binding_table` | Pragma binding table (0x158 bytes initial). |
| `qword_106B730` | 8 | `pragma_alloc_pool_1` | Pragma allocation pool. |
| `qword_106B738` | 8 | `pragma_alloc_pool_2` | Pragma allocation pool (secondary). |
| `qword_106B890` | 8 | `pragma_name_hash_1` | Pragma name hash table. |
| `qword_106B8A8` | 8 | `pragma_name_hash_2` | Pragma name hash table (secondary). |
| `off_E6CDE0` | -- | `pragma_id_table` | Pragma ID-to-name mapping table. |
| `byte_126E558` | 1 | `stdc_cx_limited_range` | `#pragma STDC CX_LIMITED_RANGE` state. Default `3`. |
| `byte_126E559` | 1 | `stdc_fenv_access` | `#pragma STDC FENV_ACCESS` state. Default `3`. |
| `byte_126E55A` | 1 | `stdc_fp_contract` | `#pragma STDC FP_CONTRACT` state. Default `3`. |
| `dword_126EE48` | 4 | `macro_expansion_tracking` | Macro expansion tracking / secondary IL enabled flag. Set to `1` during init-complete. Also controls shareable-constants feature. |

## Translation Unit State

These globals track the current translation unit, TU list, and per-TU save/restore mechanism.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_106BA10` | 8 | `current_tu` | Pointer to current translation unit descriptor (424 bytes). |
| `qword_106B9F0` | 8 | `primary_tu` | Pointer to first (primary) translation unit. |
| `qword_12C7A90` | 8 | `tu_chain_tail` | Tail of translation unit linked list. |
| `qword_106BA18` | 8 | `tu_stack` | Translation unit stack (for nested TU processing). |
| `dword_106B9E8` | 4 | `tu_stack_depth` | TU stack depth (excluding primary). |
| `dword_106BA08` | 4 | `is_recompilation` | Recompilation / secondary-TU flag. When `0` = primary TU, when `1` = secondary. Affects IL entity flag bits. |
| `qword_106BA00` | 8 | `current_filename` | Current filename string pointer. |
| `dword_106B9F8` | 4 | `has_module_info` | TU has module information. |
| `qword_12C7A98` | 8 | `per_tu_storage_size` | Total per-TU variable buffer size. |
| `qword_12C7AA8` | 8 | `registered_var_list_head` | Registered per-TU variable list head. |
| `qword_12C7AA0` | 8 | `registered_var_list_tail` | Registered per-TU variable list tail. |
| `qword_12C7AB8` | 8 | `stack_entry_free_list` | TU stack entry free list. |
| `qword_12C7AB0` | 8 | `corresp_free_list` | TU correspondence structure free list. |
| `dword_12C7A8C` | 4 | `registration_complete` | Variable registration complete flag. |
| `dword_12C7A88` | 4 | `has_seen_module_tu` | Has seen a module TU. |
| `qword_12C7A70` | 8 | `corresp_count` | TU correspondence allocation counter. |
| `qword_12C7A78` | 8 | `tu_count` | Translation unit allocation counter. |
| `qword_12C7A80` | 8 | `stack_entry_count` | Stack entry allocation counter. |
| `qword_12C7A68` | 8 | `registration_count` | Variable registration allocation counter. |

## IL (Intermediate Language) State

The IL subsystem uses arena-allocated regions for entities. Two primary regions exist: file-scope and function-scope.

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_126EC90` | 4 | `file_scope_region_id` | File-scope IL region ID. Persistent for the entire TU. |
| `dword_126EB40` | 4 | `current_region_id` | Current allocation region ID (file-scope or function-scope). |
| `dword_126EC80` | 4 | `max_region_id` | Maximum allocated region ID. |
| `qword_126EB60` | 16 | `il_header` | IL header (SSE-width, used for expression copy). |
| `qword_126EB70` | 8 | `main_routine` | Main routine entity (`main()` function). Sign-bit used as elimination marker. |
| `qword_126EB78` | 8 | `compiler_version_string` | Compiler version string pointer. |
| `qword_126EB80` | 8 | `compilation_timestamp` | Compilation timestamp string. |
| `byte_126EB88` | 1 | `plain_chars_signed` | Plain chars are signed flag (IL header field). |
| `qword_126EB90` | 8 | `routine_scope_array` | Array indexed by routine number. Also per-region metadata. |
| `qword_126EB98` | 8 | `function_def_table` | Function definition table (16 bytes per entry, indexed 1..`dword_126EC78`). |
| `qword_126EBA0` | 8 | `orphaned_scope_list` | Orphaned scope list head (for dead code elimination). |
| `dword_126EBA8` | 4 | `source_language` | Source language (`0` = C++, `1` = C). |
| `dword_126EBAC` | 4 | `std_version_il` | Standard version for IL header. |
| `byte_126EBB0` | 1 | `pcc_compatibility_mode` | PCC compatibility mode. |
| `byte_126EBB1` | 1 | `enum_type_is_integral` | Enum underlying type is integral. |
| `dword_126EBB4` | 4 | `max_member_alignment` | Default maximum member alignment. |
| `byte_126EBB8` | 1 | `il_gcc_mode` | IL GCC mode. |
| `byte_126EBB9` | 1 | `il_gpp_mode` | IL G++ mode. |
| `byte_126EBD5` | 1 | `any_templates_seen` | Any templates encountered. |
| `byte_126EBD6` | 1 | `proto_instantiations_in_il` | Prototype instantiations present in IL. |
| `byte_126EBD7` | 1 | `il_all_proto_instantiations` | IL has all prototype instantiations. |
| `byte_126EBD8` | 1 | `il_c_semantics` | IL has C semantics. |
| `qword_126EBE0` | 8 | `deferred_instantiation_list` | Deferred/external declaration list head. |
| `qword_126EBE8` | 8 | `seq_number_entries` | Sequence number lookup entries (for IL index build). |
| `dword_126EBF8` | 4 | `target_config_index` | Target configuration index. |
| `dword_126EC78` | 4 | `routine_counter` | Current routine / entity counter. |
| `dword_126EC7C` | 4 | `entity_buffer_capacity` | Entity buffer capacity (grows by 2048). |
| `qword_126EC88` | 8 | `region_block_chains` | Array of block chains indexed by region ID. |
| `qword_126EC50` | 8 | `region_size_tracking` | Array of region size tracking. |
| `qword_126EC58` | 8 | `large_alloc_array` | Large-allocation (mmap) array. |
| `dword_126E5FC` | 4 | `file_scope_constant_flag` | Source-file-info flags (bit 0 = constant region flag). |
| `byte_126E5F8` | 1 | `il_language_byte` | Language standard byte for routine-type init. |
| `qword_126EFB8` | 8 | `null_source_position` | Default/null source position struct. |
| `qword_126F700` | 8 | `current_source_file_ref` | Current source file reference for IL entities. |

## IL Entity Kind Lists

The IL maintains per-kind linked lists for file-scope entities (kinds 1 through 72+).

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126E610` | 8 | `kind_1_list` | Source file entries (kind 1). |
| `qword_126E620` | 8 | `kind_2_list` | Constant entries (kind 2). |
| `qword_126E630` | 8 | `kind_3_list` | Parameter entries (kind 3). |
| ... | | ... | Continues through all 72+ entry kinds. |
| `qword_126EA80` | 8 | `kind_72_list` | Last numbered kind list (kind 72). |

## IL Allocation Counters

Each IL entity type has a dedicated allocation counter used for memory statistics reporting.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F680` | 8 | `local_constant_count` | Local constant allocation count. Asserted zero at region boundaries. |
| `qword_126F748` | 8 | `orphan_ptr_count` | Orphan pointer allocation count. |
| `qword_126F750` | 8 | `entity_prefix_count` | Entity prefix allocation count. |
| `qword_126F790` | 8 | `source_corresp_count` | Source correspondence allocation count. |
| `qword_126F7C0` | 8 | `gen_alloc_header_count` | Gen-alloc header count (TU copy addresses). |
| `qword_126F7D0` | 8 | `string_bytes_count` | String literal bytes counter. |
| `qword_126F7D8` | 8 | `il_entry_prefix_count` | IL entry prefix allocation count. |
| `qword_126F8A0` | 8 | `exception_spec_count` | Exception specification entry count (16 bytes). |
| `qword_126F898` | 8 | `exception_spec_type_count` | Exception spec type count (24 bytes). |
| `qword_126F890` | 8 | `asm_entry_count` | ASM entry count (152 bytes). |
| `qword_126F8A8` | 8 | `routine_count` | Routine entry count (288 bytes). |
| `qword_126F8B0` | 8 | `field_count` | Field entry count (176 bytes). |
| `qword_126F8B8` | 8 | `var_template_count` | Variable template entry count (24 bytes). |
| `qword_126F8C0` | 8 | `variable_count` | Variable entry count (232 bytes). |
| `qword_126F8C8` | 8 | `vla_dim_count` | VLA dimension entry count (48 bytes). |
| `qword_126F8D0` | 8 | `local_static_init_count` | Local static init count (40 bytes). |
| `qword_126F8D8` | 8 | `dynamic_init_count` | Dynamic init entry count (104 bytes). |
| `qword_126F8E0` | 8 | `type_count` | Type entry count (176 bytes). |
| `qword_126F8E8` | 8 | `enum_supplement_count` | Enum type supplement count. |
| `qword_126F8F0` | 8 | `typeref_supplement_count` | Typeref type supplement count (56 bytes). |
| `qword_126F8F8` | 8 | `misc_supplement_count` | Misc type supplement count. |
| `qword_126F900` | 8 | `template_arg_count` | Template argument count (64 bytes). |
| `qword_126F908` | 8 | `base_class_count` | Base class count (112 bytes). |
| `qword_126F910` | 8 | `base_class_deriv_count` | Base class derivation count (32 bytes). |
| `qword_126F918` | 8 | `derivation_step_count` | Derivation step count (24 bytes). |
| `qword_126F920` | 8 | `overriding_count` | Overriding entry count (40 bytes). |
| `qword_126F928` | 8 | `constant_list_count` | Constant list entry count (16 bytes). |
| `qword_126F930` | 8 | `variable_list_count` | Variable list entry count (16 bytes). |
| `qword_126F938` | 8 | `routine_list_count` | Routine list entry count (16 bytes). |
| `qword_126F940` | 8 | `class_list_count` | Class list entry count (16 bytes). |
| `qword_126F948` | 8 | `class_supplement_count` | Class type supplement count. |
| `qword_126F950` | 8 | `based_type_member_count` | Based type list member count (24 bytes). |
| `qword_126F958` | 8 | `routine_supplement_count` | Routine type supplement count (64 bytes). |
| `qword_126F960` | 8 | `param_type_count` | Parameter type entry count (80 bytes). |
| `qword_126F968` | 8 | `constant_alloc_count` | Constant allocation count (184 bytes). |
| `qword_126F970` | 8 | `source_file_count` | Source file entry count. |

## IL Free Lists

Arena allocators recycle nodes through per-type free lists.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126E4B8` | 8 | `constant_free_list` | Constants (linked via offset `+104`). |
| `qword_126E4B0` | 8 | `expr_node_free_list` | Expression nodes (linked via offset `+64`). |
| `qword_126F678` | 8 | `param_type_free_list` | Parameter type entries (linked via offset `+0`). |
| `qword_126F670` | 8 | `template_arg_free_list` | Template argument entries (linked via offset `+0`). |
| `qword_126F668` | 8 | `constant_list_free_list` | Constant list entries (linked via offset `+0`). |

## IL Pools and Region Allocator

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F600` | 104 | `type_node_pool_1` | Type node pool (104-byte entries). |
| `qword_126F580` | 104 | `type_node_pool_2` | Secondary type node pool. |
| `qword_126F500` | 104 | `conditional_pool_1` | Conditional pool (guarded by `dword_106BF68 \|\| dword_106BF58`). |
| `qword_126F480` | 104 | `conditional_pool_2` | Conditional pool (secondary). |
| `qword_126F400` | 112 | `expr_pool_1` | Expression/statement node pool (112 bytes). |
| `qword_126F380` | 112 | `expr_pool_2` | Expression pool (secondary). |
| `qword_126F300` | 112 | `expr_pool_3` | Expression pool (tertiary). |
| `unk_126E600` | 1344 | `scope_pool` | Scope table pool (1344 bytes, 384 initial count). |
| `qword_126E580` | 96 | `common_header_pool` | Common IL header pool (96 bytes). |
| `dword_126F690` | 4 | `region_prefix_offset` | Region allocation prefix offset (0 or 8). |
| `dword_126F694` | 4 | `region_prefix_size` | Region allocation prefix size (16 or 24). |
| `dword_126F688` | 4 | `alt_prefix_offset` | Alternate region prefix offset. |
| `dword_126F68C` | 4 | `alt_prefix_size` | Alternate region prefix size (8). |

## Constant Sharing Hash Table

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F128` | 8 | `constant_hash_table` | Hash table for constant sharing/dedup. |
| `qword_126F130` | 8 | `next_constant_index` | Next constant index (monotonically increasing). |
| `qword_126F228` | 8 | `shareable_constant_hash` | Shareable constant hash table (2039 buckets). |
| `qword_126F200` | 8 | `hash_comparisons` | Hash comparison count (statistics). |
| `qword_126F208` | 8 | `hash_searches` | Hash search count. |
| `qword_126F210` | 8 | `hash_new_buckets` | New hash bucket count. |
| `qword_126F218` | 8 | `hash_region_hits` | Region hit count. |
| `qword_126F220` | 8 | `hash_global_hits` | Global hit count. |
| `qword_126F280` | 8 | `member_ptr_type_count` | Member-pointer / qualified type allocation counter. |
| `qword_126F2F8` | 3240 | `char_string_type_cache` | Character string type cache (405 entries = 3240/8). Indexed by `648*char_kind + 8*length`. |

## Cached Type Nodes

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F2F0` | 8 | `cached_void_type` | Lazy-init cached void type node. |
| `qword_126F2E0` | 8 | `cached_size_t_type` | Lazy-init cached size_t type (for array memcpy). |
| `qword_126F2D0` | 8 | `cached_wchar_type` | Cached `wchar_t` type. |
| `qword_126F2C8` | 8 | `cached_char16_type` | Cached `char16_t` type. |
| `qword_126F2C0` | 8 | `cached_char32_type` | Cached `char32_t` type. |
| `qword_126F2B8` | 8 | `cached_char8_type` | Cached `char8_t` type (C++20). |
| `qword_126F610` | 8 | `cached_char16_variant` | Cached `char16_t` variant type. |
| `qword_106B660` | 8 | `cached_void_fn_type` | Cached void function type (C++ mode). |
| `qword_126E5E0` | 8 | `global_char_type` | Global `char` type. Used with qualifier `1` = `const` for `const char*`. |

## Template Instantiation

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_12C7740` | 8 | `pending_instantiation_list` | Pending function/variable instantiation worklist head. |
| `qword_12C7758` | 8 | `pending_class_list` | Pending class instantiation list. |
| `qword_12C76E0` | 8 | `instantiation_depth` | Current instantiation depth counter (max `0xFF` = 255). |
| `qword_106BD10` | 8 | `max_instantiation_depth` | Maximum template instantiation depth limit. Default `200`. |
| `qword_106BD08` | 8 | `max_constexpr_cost` | Maximum constexpr evaluation cost. Default `256`. |
| `dword_12C7730` | 4 | `instantiation_mode_active` | Instantiation mode active flag. |
| `dword_12C771C` | 4 | `new_instantiations_needed` | Fixpoint flag: new instantiations generated in current pass. |
| `dword_12C7718` | 4 | `additional_pass_needed` | Additional instantiation pass needed flag. |
| `dword_106C094` | 4 | `compilation_mode` | Compilation mode: `0` = none, `1` = normal, `2` = used-only, `3` = precompile. |
| `dword_106C09C` | 4 | `extended_language_mode` | Extended language mode. |
| `qword_12C7B48` | 8 | `template_arg_cache` | Template argument cache. |
| `qword_12C7B40` | 8 | `template_arg_cache_2` | Template argument cache (secondary). |
| `qword_12C7B50` | 8 | `template_arg_cache_3` | Template argument cache (tertiary). |
| `qword_12C7800` | 112[3] | `template_hash_tables` | Three template hash tables (0x70 bytes each = 14 slots). |

## Lambda Transform State

NVIDIA's extended lambda system uses bitmaps and linked lists to track device and host-device lambda closures.

| Address | Size | Name | Description |
|---|---|---|---|
| `unk_1286980` | 128 | `device_lambda_bitmap` | Device lambda capture count bitmap (1024 bits). One bit per closure class index. |
| `unk_1286900` | 128 | `host_device_lambda_bitmap` | Host-device lambda capture count bitmap (1024 bits). |
| `qword_12868F0` | 8 | `entity_closure_map` | Entity-to-closure mapping hash table (via `sub_742670`). |
| `qword_1286A00` | 8 | `cached_anon_namespace_name` | Cached anonymous namespace name (`_GLOBAL__N_<filename>`). |
| `qword_1286760` | 8 | `cached_static_prefix` | Cached static prefix string for mangled names. |
| `byte_1286A20` | 256K | `name_format_buffer` | 256KB buffer for name formatting. |

## Lambda Registration Lists

Six linked lists track device/constant/kernel entities with internal/external linkage for `.int.c` registration emission.

| Address | Size | Name | Description |
|---|---|---|---|
| `unk_1286780` | -- | `device_external_list` | Device entities with external linkage. |
| `unk_12867C0` | -- | `device_internal_list` | Device entities with internal linkage. |
| `unk_1286800` | -- | `constant_external_list` | Constant entities with external linkage. |
| `unk_1286840` | -- | `constant_internal_list` | Constant entities with internal linkage. |
| `unk_1286880` | -- | `kernel_external_list` | Kernel entities with external linkage. |
| `unk_12868C0` | -- | `kernel_internal_list` | Kernel entities with internal linkage. |

## IL Tree Walking

The `walk_tree` subsystem uses global callback pointers for its 5-callback traversal model.

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126FB88` | 8 | `entry_callback` | Called for each IL entry during walk. |
| `qword_126FB80` | 8 | `string_callback` | Called for each string encountered. |
| `qword_126FB78` | 8 | `pre_walk_check` | Pre-walk filter: if returns nonzero, skip subtree. |
| `qword_126FB70` | 8 | `entry_replace` | Entry replacement callback. |
| `qword_126FB68` | 8 | `entry_filter` | Linked-list entry filter callback. |
| `dword_126FB5C` | 4 | `is_file_scope_walk` | `1` = walking file-scope IL. |
| `dword_126FB58` | 4 | `is_secondary_il` | `1` = current scope is in secondary IL region. |
| `dword_126FB60` | 4 | `walk_mode_flags` | Walk mode flags (template stripping, etc.). |
| `dword_106B644` | 4 | `current_il_region` | Current IL region (0 or 1; toggles bit 2 of entry flags). |

## IL Walk Visited-Set

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_126FB30` | 4 | `visited_count` | Count of visited entries in current walk. |
| `qword_126FB40` | 8 | `visited_set` | Visited-entry set pointer. |
| `dword_126FB48` | 4 | `hash_table_count` | Hash table entry count for visited set. |
| `qword_126FB50` | 8 | `hash_table_array` | Hash table array for visited set. |

## IL Display

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F980` | 8 | `display_output_context` | IL-to-string output callback/context. |
| `dword_126FA30` | 4 | `is_file_scope_display` | `1` = displaying file-scope region. |
| `byte_126FA16` | 1 | `display_active` | IL display currently active flag. |
| `byte_126FA11` | 1 | `pcc_mode_shadow` | PCC compatibility mode shadow for display. |
| `qword_126FA40` | -- | `display_string_buffer` | Display string buffer (raw literal prefix, etc.). |

## Constexpr Evaluator

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126FDE0` | 8 | `eval_node_free_list` | Evaluation node free list (0x10000-byte arena blocks). |
| `qword_126FDE8` | 8 | `eval_nesting_depth` | Evaluation nesting depth counter. |
| `qword_126FE00` | 8[11] | `hash_bucket_free_lists` | Hash bucket free lists by popcount size class (11 buckets). |
| `qword_126FE60` | 8[11] | `value_node_free_lists` | Value node free lists by popcount size class (11 buckets). |
| `qword_126FBC0` | 8 | `variant_path_free_list` | Variant path node free list. |
| `qword_126FBB8` | 8 | `variant_path_count` | Variant path allocation count. |
| `qword_126FBC8` | 8 | `variant_path_limit` | Variant path limit. |
| `qword_126FBD0` | 8 | `variant_path_table` | Variant path table pointer. |
| `qword_126FEC0` | 8 | `constexpr_class_hash_table` | Class type hash table base for constexpr. |
| `qword_126FEC8` | 8 | `constexpr_class_hash_info` | Low 32 = capacity mask, high 32 = entry count. |

## Backend Code Generation (cp_gen_be.c)

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_1065834` | 4 | `indent_level` | Current indentation depth in output. |
| `dword_1065820` | 4 | `output_line_number` | Output line counter. |
| `dword_106581C` | 4 | `output_column` | Output column counter (chars since last newline). |
| `dword_1065830` | 4 | `output_column_alt` | Alternate column counter. |
| `dword_1065818` | 4 | `needs_line_directive` | Needs `#line` directive flag. |
| `qword_1065810` | 8 | `output_source_position` | Current source position for `#line` directives. |
| `qword_1065748` | 8 | `source_sequence_ptr` | Current source sequence entry pointer. |
| `qword_1065740` | 8 | `source_sequence_alt` | Secondary source sequence pointer (nested scope iteration). |
| `byte_10656F0` | 1 | `current_linkage_spec` | Current linkage spec: `2` = `extern "C"`, `3` = `extern "C++"`. |
| `qword_1065708` | 8 | `output_scope_stack` | Output scope stack pointer (linked list). |
| `qword_1065870` | 8 | `debug_trace_list` | Debug trace request linked list. |

## Expression Parsing State

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_106B970` | 8 | `expr_stack_top` | Current expression stack top pointer. Primary context object for expression parsing. Checked at offset `+17` (flags), `+18`, `+19` (bit flags), `+48`, `+120`. |
| `qword_106B968` | 8 | `expr_stack_prev` | Previous expression stack entry (push/pop). |
| `qword_106B580` | 8 | `saved_expr_context` | Saved expression context (for nested evaluation). |
| `qword_106B510` | 8 | `rewrite_loop_counter` | Rewrite loop counter (limited to 100 to prevent infinite loops). |
| `dword_126EF08` | 4 | `requires_expr_enabled` | Requires-expression enabled (C++20). |

## Overload Resolution

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_E7FE98` | 8 | `override_pending_list` | Virtual function override pending list head (40-byte entries). |
| `qword_E7FEA0` | 8 | `override_free_list` | Override entry free list. |
| `qword_E7FE88` | 8 | `covariant_free_list` | Covariant override free list. |
| `qword_E7FEC8` | 8 | `lambda_hash_table` | Lambda closure class hash table pointer. |
| `qword_E7FED0` | 8 | `template_member_hash` | Template member hash table pointer. |
| `dword_E7FE48` | 4 | `rbtree_sentinel` | Red-black tree sentinel node (for lambda numbering). |
| `qword_E7FE58` | 8 | `rbtree_left_sentinel` | Red-black tree left sentinel (= `&dword_E7FE48`). |
| `qword_E7FE60` | 8 | `rbtree_right_sentinel` | Red-black tree right sentinel (= `&dword_E7FE48`). |
| `qword_E7FE68` | 8 | `rbtree_size` | Red-black tree entry count. |

## Attribute System

| Address | Size | Name | Description |
|---|---|---|---|
| `off_D46820` | 32/entry | `attribute_descriptor_table` | Attribute descriptor table. ~160 entries, stride 32 bytes. Runs to `unk_D47A60`. |
| `qword_E7FB60` | 8 | `attribute_hash_table` | Attribute name hash table (Robin Hood lookup via `sub_742670`). |
| `qword_E7F038` | 8 | `attribute_hash_table_2` | Secondary attribute hash table. |
| `byte_E7FB80` | 204 | `scoped_attr_buffer` | Buffer for scoped attribute name formatting (`"namespace::name"`). |
| `byte_82C0E0` | -- | `attribute_kind_table` | Attribute kind descriptor table (indexed by attribute kind). |
| `dword_E7F078` | 4 | `attr_init_flag` | Attribute subsystem initialization flag. |
| `dword_E7F080` | 4 | `attr_flags` | Attribute system flags. |
| `qword_E7F070` | 8 | `visibility_stack` | Visibility stack linked list. |
| `qword_E7F068` | 8 | `visibility_state` | Current visibility state. |
| `qword_E7F048` | 8 | `alias_ifunc_free_list` | Free list for alias/ifunc entries. |
| `qword_E7F058` | 8 | `alias_list_head` | Alias entry linked list head. |
| `qword_E7F050` | 8 | `alias_list_next` | Alias entry linked list next. |
| `dword_106BF18` | 4 | `extended_attr_config` | Extended attribute configuration flag. Gates additional initialization. |

## Control Flow Tracking

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_12C7110` | 8 | `cf_descriptor_free_list` | Control flow descriptor free list. |
| `qword_12C7118` | 8 | `cf_active_list_tail` | Active control flow list tail. |
| `qword_12C7120` | 8 | `cf_active_list_head` | Active control flow list head. |

## Cross-Reference System

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_106C258` | 8 | `xref_output_file` | Cross-reference output file handle. When nonzero, enables xref emission. |
| `qword_12C7160` | 8 | `xref_callback` | Cross-reference callback (`sub_726F10`). |
| `dword_12C7148` | 4 | `xref_enabled` | Cross-reference generation enabled. |
| `byte_12C71FA` | 1 | `xref_flag_a` | Cross-reference flag A. |
| `byte_12C71FE` | 1 | `xref_flag_b` | Cross-reference flag B. Default `1`. |

## Object Lifetime Stack

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126E4C0` | 8 | `curr_object_lifetime` | Top of object lifetime stack. Used for destructor ordering and scope cleanup. |

## Timing and Debug

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_106C0A4` | 4 | `timing_enabled` | Timing/profiling enabled flag. |
| `dword_126EFC8` | 4 | `debug_trace` | Debug tracing active. When set, calls `sub_48AE00`/`sub_48AFD0` trace hooks. |
| `dword_126EFCC` | 4 | `debug_verbosity` | Debug verbosity level. `>2` = detailed, `>3` = very detailed, `>4` = IL walk trace. |
| `byte_106B5C0` | 128 | `compilation_timestamp` | Compilation timestamp string (from `ctime()`). |

## Memory Allocator (Arena/Pool System)

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_1280730` | 8 | `block_free_list` | Recycled 0x10000-byte block free list. |
| `qword_1280718` | 8 | `total_memory_allocated` | Total memory allocated (watermark). |
| `qword_1280710` | 8 | `peak_memory_allocated` | Peak memory allocated. |
| `qword_1280708` | 8 | `tracked_alloc_total` | Tracked allocation total. |
| `qword_1280720` | 8 | `free_fe_hash_table` | Hash table for `free_fe` tracked allocations. |
| `qword_1280748` | 8 | `alloc_tracking_list` | Linked list of allocation tracking records. |
| `dword_1280728` | 4 | `mmap_mode` | Allocation mode flag. `0` = malloc-based, `1` = mmap-based. Set from `dword_106BF18`. |
| `dword_1280750` | 4 | `tracking_record_count` | Tracking record count (inline up to 1023, then heap). |
| `unk_1280760` | -- | `tracking_record_array` | Inline tracking record array. |

## IL Copy Remap

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F1E0` | 8 | `copy_remap_free_list` | Copy remap entry free list (24 bytes each). |
| `qword_126F1D8` | 8 | `copy_remap_count` | Copy remap entry count. |
| `qword_126F1D0` | 4 | `copy_recursion_depth` | Copy recursion depth counter. |
| `qword_126F1F8` | 8 | `copy_remap_stat_count` | Copy remap statistics count. |
| `qword_126F140` | 8 | `selected_entity` | Selected entity for copy/comparison. |
| `byte_126F138` | 1 | `selected_entity_kind` | Kind of selected entity (7 or 11). |

## IL Deferred Reordering Batch

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F170` | 8 | `reorder_batch` | Batch reordering array (24-byte records: entity, placeholder, source_sequence). |
| `qword_126F158` | 8 | `reorder_ptr_array` | Pointer array for batch reordering. |
| `qword_126F150` | 8 | `reorder_batch_limit` | Batch size limit (100 entries). |

## CLI Processing State

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_E80058` | 4 | `flag_count` | Current registered CLI flag count (panics at 552 via `sub_40351D`). |
| `dword_E7FF20` | 4 | `argv_index` | Current argv parsing index (starts at 1). |
| `byte_E7FF40` | 272 | `flag_was_set_bitmap` | 272-byte bitmap: which CLI flags were explicitly set. |
| `dword_E7FF14` | 4 | `language_already_set` | Guard against switching language mode after initial set. |
| `dword_E7FF10` | 4 | `cuda_compat_flag` | CUDA compatibility flag (set based on `dword_126EFAC && qword_126EF98 <= 0x76BF`). |
| `off_D47CE0` | -- | `set_flag_lookup_table` | Lookup table for `--set_flag` CLI option (name-to-address mapping). |

## EDG Feature Flags (0x106Bxxx-0x106Cxxx Region)

These flags control individual C/C++ language features. Set during CLI processing and standard-version initialization.

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_106C210` | 4 | `exceptions_enabled` | Exception handling enabled. Default `1`. |
| `dword_106C180` | 4 | `rtti_enabled` | RTTI enabled. Default `1`. |
| `dword_106C164` | 4 | `templates_enabled` | Templates enabled. |
| `dword_106C1B8` | 4 | `template_arg_context` | Template argument context flag. |
| `dword_106C194` | 4 | `namespaces_enabled` | Namespaces enabled. Default `1`. |
| `dword_106C19C` | 4 | `arg_dep_lookup` | Argument-dependent lookup. Default `1`. |
| `dword_106C178` | 4 | `bool_keyword` | `bool` keyword enabled. Default `1`. |
| `dword_106C188` | 4 | `wchar_t_keyword` | `wchar_t` keyword enabled. Default `1`. |
| `dword_106C18C` | 4 | `alternative_tokens` | Alternative tokens enabled. Default `1`. |
| `dword_106C1A0` | 4 | `class_name_injection` | Class name injection. Default `1`. |
| `dword_106C1A4` | 4 | `const_string_literals` | Const string literals. Default `1`. |
| `dword_106C134` | 4 | `parse_templates` | Parse templates. Default `1`. |
| `dword_106C138` | 4 | `dep_name` | Dependent name processing. Default `1`. |
| `dword_106C12C` | 4 | `friend_injection` | Friend injection. Default `1`. |
| `dword_106C128` | 4 | `adl_related` | ADL related feature. Default `1`. |
| `dword_106C124` | 4 | `module_visibility` | Module-level visibility. Default `1`. |
| `dword_106C140` | 4 | `compound_literals` | Compound literals. Default `1`. |
| `dword_106C13C` | 4 | `base_assign_default` | Base assign op is default. Default `1`. |
| `dword_106C10C` | 4 | `deferred_instantiation` | Deferred instantiation flag. |
| `dword_106C0E4` | 4 | `exceptions_feature` | Exceptions feature flag (version-dependent). |
| `dword_106C064` | 4 | `modify_stack_limit` | Modify stack limit. Default `1`. |
| `dword_106C068` | 4 | `fe_inlining` | Frontend inlining enabled. |
| `dword_106C0A0` | 4 | `feature_A0` | Miscellaneous feature flag. Default `1`. |
| `dword_106C098` | 4 | `feature_98` | Miscellaneous feature flag. Default `1`. |
| `dword_106C0FC` | 4 | `feature_FC` | Miscellaneous feature flag. Default `1`. |
| `dword_106C154` | 4 | `feature_154` | Miscellaneous feature flag. Default `1`. |
| `dword_106C208` | 4 | `constexpr_if_discard` | Constexpr-if discarded-statement handling. |
| `dword_106C1F0` | 4 | `cpp_mode_feature` | C++ mode feature flag. |
| `dword_106C2A4` | 4 | `feature_2A4` | Default `1`. |
| `dword_106C214` | 4 | `feature_214` | Default `1`. |
| `dword_106C2BC` | 4 | `modules_enabled` | C++20 modules enabled. |
| `dword_106C2B8` | 4 | `module_partitions` | Module partitions enabled. |
| `dword_106BDB8` | 4 | `restrict_enabled` | `restrict` keyword enabled. Default `1`. |
| `dword_106BDB0` | 4 | `remove_unneeded_entities` | Remove unneeded entities. Default `1`. |
| `dword_106BD98` | 4 | `trigraphs_enabled` | Trigraph support. Default `1`. |
| `dword_106BD68` | 4 | `guiding_decls` | Guiding declarations. Default `1`. |
| `dword_106BD58` | 4 | `old_specializations` | Old-style specializations. Default `1`. |
| `dword_106BD54` | 4 | `implicit_typename` | Implicit typename. Default `1`. |
| `dword_106BEA0` | 4 | `rtti_config` | RTTI configuration flag. |
| `dword_106BE84` | 4 | `gen_move_operations` | Generate move operations. Default `1`. |
| `dword_106BC08` | 4 | `nodiscard_enabled` | `[[nodiscard]]` enabled. |
| `dword_106BC64` | 4 | `visibility_support` | Visibility support enabled. |
| `dword_106BDF0` | 4 | `gnu_attr_groups` | GNU attribute groups enabled. |
| `dword_106BDF4` | 4 | `msvc_declspec` | MSVC `__declspec` enabled. |
| `dword_106BCBC` | 4 | `template_features` | Template features flag. |
| `dword_106BFC4` | 4 | `debug_mode_1` | Debug mode flag 1 (set by `--debug_mode`). |
| `dword_106BFC0` | 4 | `debug_mode_2` | Debug mode flag 2. |
| `dword_106BFBC` | 4 | `debug_mode_3` | Debug mode flag 3. |
| `qword_106BCE0` | 8 | `include_suffix_default` | Include suffix default string (`"::stdh:"`). |
| `qword_106BC70` | 8 | `version_threshold` | Feature version threshold. Default `30200`. |

## Host Compiler Target Configuration

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_126E1D4` | 4 | `msvc_target_version` | MSVC target version (`1200` = VC6, `1400` = VS2005, etc.). |
| `dword_126E1D8` | 4 | `is_msvc_host` | Is MSVC host compiler. |
| `dword_126E1DC` | 4 | `is_edg_native` | EDG native mode. |
| `dword_126E1E8` | 4 | `is_clang_host` | Is Clang host compiler. |
| `dword_126E1F8` | 4 | `is_gnu_host` | Is GNU/GCC host compiler. |
| `qword_126E1F0` | 8 | `gnu_host_version` | GCC/Clang host version number. |
| `qword_126E1E0` | 8 | `clang_host_version` | Clang host version number. |
| `dword_126E1EC` | 4 | `backend_enabled` | Backend generation enabled. |
| `dword_126E1BC` | 4 | `host_feature_flag` | Host feature flag. Default `1`. |
| `dword_126DFF0` | 4 | `msvc_declspec_mode` | MSVC `__declspec` mode enabled. |
| `qword_126E1B0` | 8 | `library_prefix` | Library search path prefix (`"lib"`). |
| `dword_126E200` | 4 | `constexpr_init_flag` | Constexpr initialization flag. |
| `dword_126E204` | 4 | `instantiation_flag` | Instantiation control flag. |
| `dword_126E224` | 4 | `parameter_flag` | Parameter handling flag. |

## Type System Lookup Tables (Read-Only)

| Address | Size | Name | Description |
|---|---|---|---|
| `byte_E6D1B0` | 256 | `signedness_table` | Type-code-to-signedness lookup table. |
| `byte_E6D1AD` | 1 | `unsigned_int_kind_sentinel` | Must equal `111` (`'o'`) -- sentinel validation. |
| `byte_A668A0` | 256 | `type_kind_properties` | Type kind property table. Bit 1 = callable, bit 4 = aggregate. |
| `off_E6E020` | -- | `il_entry_kind_names` | IL entry kind name table (last = `"last"`, sentinel = 9999). |
| `off_E6CD78` | -- | `db_storage_class_names` | Storage class name table (last = `"last"`). |
| `off_E6D228` | -- | `db_special_function_kinds` | Special function kind name table. |
| `off_E6CD20` | -- | `db_operator_names` | Operator name table. |
| `off_E6E060` | -- | `name_linkage_kind_names` | Name linkage kind names. |
| `off_E6CD88` | -- | `decl_modifier_names` | Declaration modifier names. |
| `off_E6CF38` | -- | `pragma_ids` | Pragma ID table. |
| `qword_E6C580` | 8 | `sizeof_il_entry_sentinel` | Must equal `9999` -- sizeof IL entry validation. |
| `off_E6DD80` | -- | `il_entry_kind_display_names` | IL entry kind display names (indexed by kind byte). |
| `off_E6E040` | -- | `linkage_kind_display_names` | Linkage kind display names (none/internal/external/C/C++). |
| `off_E6E140` | -- | `feature_init_table` | Feature initialization table (used with `dword_106BF18`). |

## IL Display Tables (Read-Only)

| Address | Size | Name | Description |
|---|---|---|---|
| `off_A6F840` | 8[120] | `builtin_op_names` | Builtin operation kind names (120 entries). |
| `off_A6FE40` | 8[22] | `type_kind_names` | Type kind names (22 entries: void, bool, int, float, ...). |
| `off_A6F760` | 8[4] | `access_specifier_names` | Access specifier names (public/protected/private/none). |
| `off_A6FE00` | 8[7] | `storage_class_display_names` | Storage class display names (7: none/auto/register/static/extern/mutable/thread_local). |
| `off_A6F480` | -- | `register_kind_names` | Register kind names. |
| `off_A6FC00` | -- | `special_kind_names` | Special function kind names (lambda call operator, etc.). |
| `off_A6FC80` | -- | `opname_kind_names` | Operator name kind names. |
| `off_A6F640` | -- | `typeref_kind_names` | Typeref kind names. |
| `off_A6F420` | -- | `based_type_kind_names` | Based type kind names. |
| `off_A6F3F0` | -- | `class_kind_names` | Class/struct/union kind names. |
| `off_E6C5A0` | -- | `builtin_op_table` | Builtin operation reference table. |

## PCH and Serialization

| Address | Size | Name | Description |
|---|---|---|---|
| `dword_106B690` | 4 | `pch_mode` | Precompiled header mode. |
| `dword_106B6B0` | 4 | `pch_loaded` | PCH loaded flag. |
| `qword_12C6BA0` | 8 | `pch_string_buffer_1` | PCH string buffer. |
| `qword_12C6BA8` | 8 | `pch_string_buffer_2` | PCH string buffer (secondary). |
| `qword_12C6EA0` | 8 | `pch_write_state` | PCH binary write state. |
| `qword_12C6EA8` | 8 | `pch_misc_state` | PCH miscellaneous state. |
| `dword_12C6C88` | 4 | `pch_config_flag` | PCH configuration flag. |
| `byte_12C6EE0` | 1 | `pch_byte_flag` | PCH byte flag. |
| `dword_12C6C8C` | 4 | `saved_var_list_count` | Saved variable list count (PCH). |
| `qword_12C6CA0` | 8 | `saved_var_lists` | Saved variable list array (PCH). |

## Inline and Linkage Tracking

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_12C6FC8` | 8 | `inline_def_tracking_1` | Inline definition tracking. |
| `qword_12C6FD0` | 8 | `inline_def_tracking_2` | Inline definition tracking (secondary). |
| `qword_12C6FD8` | 8 | `inline_def_tracking_3` | Inline definition tracking (tertiary). |
| `qword_12C6FB8` | 8 | `linkage_stack_1` | Linkage stack. |
| `qword_12C6FC0` | 8 | `linkage_stack_2` | Linkage stack (secondary). |
| `qword_12C6FE0` | 8 | `mangling_discriminator` | ABI mangling discriminator tracking. |
| `qword_12C70E8` | 8 | `misc_tracking` | Miscellaneous definition tracking. |

## Miscellaneous

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126E4C0` | 8 | `curr_object_lifetime` | Top of object lifetime stack. |
| `qword_106B9B0` | 8 | `active_compilation_ctx` | Active compilation context pointer. |
| `dword_126E280` | 4 | `max_object_size` | Maximum object size (for vector/array validation). |
| `dword_106B4B8` | 4 | `omp_declare_variant` | OpenMP `declare variant` active flag. |
| `dword_106BC7C` | 4 | `compressed_mangling` | Compressed name mangling mode. |
| `dword_106BD4C` | 4 | `profiling_flag` | Profiling / performance measurement flag. |
| `dword_106BCFC` | 4 | `traditional_enum` | Traditional (unscoped) enum mode. |
| `dword_106BBD4` | 4 | `char16_variant_flag` | `char16_t` variant selection flag. |
| `dword_106BD74` | 4 | `sharing_mode_config` | IL sharing mode configuration. |
| `dword_126E1C0` | 4 | `string_sharing_enabled` | String sharing enabled in IL. |
| `byte_126E1C4` | 1 | `basic_char_type` | Basic char type code (for `sub_5BBDF0`). |
| `dword_106BD8C` | 4 | `svr4_mode` | SVR4 ABI mode. |
| `byte_126E349` | 1 | `cuda_extensions_byte` | CUDA extensions flag (byte-sized). |
| `byte_126E358` | 1 | `arch_extension_byte` | Extension flag (possibly `__CUDA_ARCH__`). |
| `byte_126E3C0` | 1 | `extension_byte_C0` | Extension flag byte. |
| `byte_126E3C1` | 1 | `extension_byte_C1` | Extension flag byte. |
| `byte_126E481` | 1 | `extension_byte_481` | Extension flag byte. |
| `dword_126F248` | 4 | `il_index_valid` | IL index valid flag (`1` = index built). |
| `qword_126F240` | 8 | `il_index_capacity` | IL index array capacity. |
| `qword_126EBF0` | 8 | `il_index_count` | IL index entry count. |
| `qword_126F230` | 8 | `il_index_aux` | IL index auxiliary pointer. |
| `dword_12C6A24` | 4 | `block_scope_suppress` | Block-scope suppress level. |
| `dword_127FC70` | 4 | `mark_direction` | Mark/unmark direction for entity traversal. |
| `dword_127FBA0` | 4 | `eof_flag` | Input EOF flag. |
| `qword_127FBA8` | 8 | `file_handle` | Current input file handle. |
| `dword_127FB9C` | 4 | `multibyte_mode` | Multibyte character mode (`>1` = active). |
| `qword_126E440` | 8[6] | `char_type_widths` | Character type width table (indexed by char kind: 1,2,4 bytes). |
| `qword_126E580` | 8[11] | `special_type_entries` | Special type entries (11 entries). |
| `qword_126DE00` | -- | `operator_name_table` | Operator name string table. |
| `off_E6E0E0` | -- | `predef_macro_mode_names` | Predefined macro mode name table (sentinel = `"last"`). |
| `qword_126EEA0` | 8 | `predef_macro_state` | Predefined macro initialization state. |
| `dword_106BBA8` | 4 | `c23_features` | C23 features flag (`#elifdef`/`#elifndef`). |
| `dword_106C2B0` | 4 | `preproc_feature_flag` | Preprocessor feature flag. |
| `dword_106BEF8` | 4 | `pch_config_2` | PCH configuration flag (secondary). |

## GCC Pragma State

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_12C6F60` | 8 | `gcc_pragma_stack_1` | GCC pragma push/pop stack. |
| `qword_12C6F68` | 8 | `gcc_pragma_stack_2` | GCC pragma stack (secondary). |
| `qword_12C6F78` | 8 | `gcc_pragma_state` | GCC pragma state. |
| `qword_12C6F98` | 8 | `gcc_pragma_misc` | GCC pragma miscellaneous state. |

## Integer Range Tables (SSE-width)

| Address | Size | Name | Description |
|---|---|---|---|
| `xmmword_126E0E0` | 16 | `integer_upper_bounds` | Upper bounds for integer kinds (populated during init). |
| `xmmword_126E000` | 16 | `integer_lower_bounds` | Lower bounds for integer kinds. |

## IL Common Header Template

The 96-byte (6 x 16 bytes) template copied into every new IL entity:

| Address | Size | Name |
|---|---|---|
| `xmmword_126F6A0` | 16 | IL header template word 0 |
| `xmmword_126F6B0` | 16 | IL header template word 1 |
| `xmmword_126F6C0` | 16 | IL header template word 2 |
| `xmmword_126F6D0` | 16 | IL header template word 3 |
| `xmmword_126F6E0` | 16 | IL header template word 4 |
| `xmmword_126F6F0` | 16 | IL header template word 5 |

## Address Region Summary

| Region | Range | Count | Purpose |
|---|---|---|---|
| `.rodata` | `0x82xxxx`--`0xA7xxxx` | ~30 | Constant tables (attribute descriptors, operation names, type kind names) |
| `.rodata` | `0xD46xxx`--`0xD48xxx` | ~10 | Attribute descriptor table, CLI flag lookup |
| `.rodata` | `0xE6xxxx`--`0xE8xxxx` | ~40 | IL metadata tables (entry kind names, type properties, signedness, pragma IDs) |
| `.data` | `0x88xxxx` | 1 | Error message template table (3795 entries) |
| `.bss` | `0x106Bxxx`--`0x106Cxxx` | ~120 | NVIDIA-added CLI flags, feature toggles, CUDA configuration |
| `.bss` | `0x1065xxx` | ~20 | Backend code generator state (output position, stub mode) |
| `.bss` | `0x1067xxx` | ~10 | Diagnostic per-error tracking, entity formatter |
| `.bss` | `0x126xxxx` | ~200 | EDG core state (scope stack, lexer, IL, error counters, source position) |
| `.bss` | `0x1270xxx` | ~10 | Preprocessor macro chains |
| `.bss` | `0x1280xxx` | ~15 | Arena allocator tracking, lambda bitmaps |
| `.bss` | `0x1286xxx` | ~10 | Lambda transform state, registration lists |
| `.bss` | `0x12C6xxx`--`0x12C7xxx` | ~40 | PCH, template instantiation, TU management |
| `.bss` | `0xE7xxxx` | ~30 | Attribute system, override tracking, red-black tree |
