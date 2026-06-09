# CLI Flag Inventory

## Quick Reference: 20 Most Important CUDA-Specific Flags

| Flag (via `-Xcudafe`) | nvcc Equivalent | ID | Effect |
|---|---|---|---|
| `--diag_suppress=N` | `--diag-suppress=N` | 39 | Suppress diagnostic number N (comma-separated) |
| `--diag_error=N` | `--diag-error=N` | 42 | Promote diagnostic N to error |
| `--diag_warning=N` | `--diag-warning=N` | 41 | Demote diagnostic N to warning |
| `--display_error_number` | — | 44 | Show `#NNNNN-D` error codes in output |
| `--target=smXX` | `--gpu-architecture=smXX` | 245 | Set SM architecture target (parsed via `sub_7525E0`) |
| `--relaxed_constexpr` | `--expt-relaxed-constexpr` | 104 | Allow constexpr cross-space calls |
| `--extended-lambda` | `--expt-extended-lambda` | 106 | Enable `__device__`/`__host__ __device__` lambdas in host code (`dword_106BF38`) |
| `--device-c` | `-rdc=true` | 77 | Relocatable device code (separate compilation) |
| `--keep-device-functions` | `--keep-device-functions` | 71 | Do not strip unused device functions |
| `--no_warnings` | `-w` | 22 | Suppress all warnings |
| `--promote_warnings` | `-W` | 23 | Promote all warnings to errors |
| `--error_limit=N` | — | 32 | Maximum errors before abort (default: unbounded) |
| `--force-lp64` | `-m64` | 65 | LP64 data model (pointer=8, long=8) |
| `--output_mode=sarif` | — | 274 | SARIF JSON diagnostic output |
| `--debug_mode` | `-G` | 82 | Full debug mode (sets 3 debug globals) |
| `--device-syntax-only` | — | 72 | Device-side syntax check without codegen |
| `--no-device-int128` | — | 52 | Disable `__int128` on device |
| `--zero_init_auto_vars` | — | 81 | Zero-initialize automatic variables |
| `--fe-inlining` | — | 54 | Enable frontend inlining |
| `--gen_c_file_name=path` | — | 45 | Set output `.int.c` file path |

These are the flags most commonly passed through `-Xcudafe` for CUDA development. The full inventory of 276 flags follows below.

---

cudafe++ accepts 276 command-line flags registered in a flat table at `dword_E80060`. The flags are not parsed directly from the binary's argv — NVIDIA's driver compiler `nvcc` decomposes its own options and invokes cudafe++ with the appropriate low-level flags. Users never run cudafe++ directly; instead, they pass options through `nvcc -Xcudafe <flag>`, which strips the `-Xcudafe` prefix and forwards the remainder as a bare argument to the cudafe++ process.

The flag system is implemented in three functions within `cmd_line.c`:

| Function | Address | Lines | Role |
|---|---|---|---|
| `register_command_flag` | `sub_451F80` | 25 | Insert one entry into the flag table |
| `init_command_line_flags` | `sub_452010` | 3,849 | Register all 276 flags (called once) |
| `proc_command_line` | `sub_459630` | 4,105 | Main parser: match argv against table, dispatch to 276-case switch (IDs 0..275, case 0 = default sink for unknown flags) |
| `default_init` | `sub_45EB40` | 470 | Zero 350 global config variables + flag-was-set bitmap |

## Flag Table Structure

Each flag occupies a 40-byte entry in a contiguous array beginning at `dword_E80060`, with a maximum capacity of 552 entries (overflow triggers a panic via `sub_40351D`). The current count is tracked in `dword_E80058`.

```c
struct flag_entry {                    // 40 bytes per entry
    int32_t   case_id;                 // dword_E80060[idx*10]    -- switch dispatch ID
    char*     name;                    // qword_E80068[idx*5]     -- long flag name string
    int16_t   short_char;              // word_E80070[idx*20]     -- single-char alias (0 if none)
    int8_t    is_valid;                // word_E80070[idx*20]+1   -- always 1
    int8_t    takes_value;             // byte_E80072[idx*40]     -- flag requires =<value> argument
    int32_t   visible;                 // dword_E80080[idx*10]    -- mode/action classification
    int8_t    is_boolean;              // byte_E80073[idx*40]     -- flag is on/off toggle
    int64_t   name_length;             // qword_E80078[idx*5]     -- strlen(name), precomputed
};
```

The flag-was-set bitmap at `byte_E7FF40` spans `0x110` bytes (272 flag slots). When a flag is matched during parsing, the corresponding bit is set to record that the user explicitly provided it. This bitmap is zeroed by `default_init` before every compilation.

### Registration Protocol

`register_command_flag` (`sub_451F80`) is called approximately 275 times from `init_command_line_flags`. Its prototype:

```c
void register_command_flag(
    int    case_id,        // dispatch ID for the switch statement
    char*  name,           // "--name" (without the dashes)
    char   short_opt,      // single-letter alias, 0 for none
    char   takes_value,    // 1 if the flag requires =<value>
    int    mode_flag,      // visibility / classification
    char   enabled         // whether the flag is active
);
```

Some flags are registered as **paired toggles** — `--flag` and `--no_flag` share the same `case_id` but set the target global to 1 or 0 respectively. These pairs are registered either by two calls to `register_command_flag` or by inline table population within `init_command_line_flags`.

## Parsing Flow

`proc_command_line` (`sub_459630`) is the master CLI parser. It:

1. Calls `init_command_line_flags` to populate the flag table (once)
2. Allocates four hash tables for accumulating `-D`, `-I`, system include, and macro alias arguments
3. Adjusts nine diagnostic severities by default via `sub_4ED400`: four are suppressed (severity 3: errors 1373, 1374, 1375, 2330) and five are demoted to remark (severity 4: errors 1257, 1633, 111, 185, 175)
4. Enters the main loop over `argv`:
   - Scans for `-` prefix to identify flags
   - Handles `-X` short flags and `--flag-name` long flags
   - Handles `--flag=value` syntax via `parse_flag_name_value` (`sub_451EC0`)
   - Matches flag names against the registered table using `strncmp` against each entry's precomputed `name_length`
   - Dispatches to a giant `switch(case_id)` with 276 cases (IDs 0..275; case 0 is the default sink for unrecognized flag names)
5. Executes post-parsing dialect resolution (described below)
6. Opens output, error, and list files
7. Treats the remaining non-flag `argv` entry as the input filename

### The -Xcudafe Pass-Through

Users never invoke cudafe++ directly. The intended usage path is:

```bash
nvcc --some-option -Xcudafe --diag_suppress=1234 source.cu
```

`nvcc` strips `-Xcudafe` and passes `--diag_suppress=1234` directly to the cudafe++ process as an argv element. Multiple `-Xcudafe` arguments accumulate. Because cudafe++ flags use `--` long-form prefixes, there is no ambiguity with nvcc's own flag namespace.

Certain nvcc flags like `--expt-extended-lambda` and `--expt-relaxed-constexpr` are translated by nvcc into the corresponding cudafe++ internal flags (`--extended-lambda`, `--relaxed_constexpr`) before invocation. Users do not need to know the internal names.

## Flag Catalog by Category

The 276 flags are grouped below by functional category. Each table lists:
- **ID** — the `case_id` used in the dispatch switch
- **Flag** — the `--name` as registered (paired flags shown as `name / no_name`)
- **Short** — single-character alias (dash required: `-E`, `-C`, etc.)
- **Arg** — whether the flag takes a `=<value>` argument
- **Effect** — what the flag does internally

### Core EDG Flags (1–44)

These are standard Edison Design Group frontend options that predate NVIDIA's CUDA modifications.

| ID | Flag | Short | Arg | Effect |
|---|---|---|---|---|
| 1 | `strict` | `-A` | no | Enable strict standards conformance mode |
| 2 | `strict_warnings` | `-a` | no | Strict mode with extra warnings |
| 3 | `no_line_commands` | `-P` | no | Suppress `#line` directives in preprocessor output |
| 4 | `preprocess` | `-E` | no | Preprocessor-only mode (output to stdout) |
| 5 | `comments` | `-C` | no | Preserve comments in preprocessor output |
| 6 | `old_line_commands` | — | no | Use old-style `# N "file"` line directives |
| 7 | `old_c` | `-K` | no | K&R C mode (calls `set_c_mode(1)`) |
| 8 | `dependencies` | `-M` | no | Output `#include` dependency list (preprocessor-only) |
| 9 | `trace_includes` | `-H` | no | Print each `#include` file as it is opened |
| 10 | `il_display` | — | no | Dump intermediate language after parsing |
| 11 | `anachronisms / no_anachronisms` | — | no | Allow/disallow anachronistic C++ constructs |
| 12 | `cfront_2.1` | `-b` | no | Cfront 2.1 compatibility mode |
| 13 | `cfront_3.0` | — | no | Cfront 3.0 compatibility mode |
| 14 | `no_code_gen` | `-n` | no | Parse only, skip code generation |
| 15 | `signed_chars / unsigned_chars` | `-s` | no | Default `char` signedness |
| 16 | `instantiate` | `-t` | yes | Template instantiation mode: `none`, `all`, `used`, `local` |
| 17 | `implicit_include / no_implicit_include` | `-B` | no | Enable/disable implicit inclusion of template definitions |
| 18 | `suppress_vtbl / force_vtbl` | — | no | Control virtual table emission |
| 19 | `dollar` | `-$` | no | Allow `$` in identifiers |
| 20 | `timing` | `-#` | no | Print compilation phase timing |
| 21 | `version` | `-v` | no | Print version banner and continue |
| 22 | `no_warnings` | `-w` | no | Suppress all warnings (sets severity threshold to error-only) |
| 23 | `promote_warnings` | `-W` | no | Promote warnings to errors |
| 24 | `remarks` | `-r` | no | Enable remark-level diagnostics |
| 25 | `c` | `-m` | no | Force C language mode |
| 26 | `c++` | `-p` | no | Force C++ language mode |
| 27 | `exceptions / no_exceptions` | `-x` | no | Enable/disable C++ exception handling |
| 28 | `no_use_before_set_warnings` | `-j` | no | Suppress "used before set" variable warnings |
| 29 | `include_directory` | `-I` | yes | Add include search path (handles `-` for stdin) |
| 30 | `define_macro` | `-D` | yes | Define preprocessor macro (builds linked list) |
| 31 | `undefine_macro` | `-U` | yes | Undefine preprocessor macro |
| 32 | `error_limit` | `-e` | yes | Maximum number of errors before abort |
| 33 | `list` | `-L` | yes | Generate listing file |
| 34 | `xref` | `-X` | yes | Generate cross-reference file |
| 35 | `error_output` | — | yes | Redirect error output to file |
| 36 | `output` | `-o` | yes | Set output file path |
| 37 | `db` | `-d` | yes | Load debug database |
| 38 | `time_limit` | — | yes | Set compilation time limit |
| 39 | `diag_suppress` | — | yes | Suppress diagnostic numbers (comma-separated list) |
| 40 | `diag_remark` | — | yes | Demote diagnostics to remark severity |
| 41 | `diag_warning` | — | yes | Set diagnostics to warning severity |
| 42 | `diag_error` | — | yes | Promote diagnostics to error severity |
| 43 | `diag_once` | — | yes | Emit diagnostic only on first occurrence |
| 44 | `display_error_number / no_display_error_number` | — | no | Show/hide error code numbers in output |

### NVIDIA CUDA-Specific Flags (45–89)

These flags are NVIDIA additions absent from stock EDG. They control CUDA compilation modes, device code generation, and host/device interaction.

| ID | Flag | Arg | Effect |
|---|---|---|---|
| 45 | `gen_c_file_name` | yes | Set output `.int.c` file path (`qword_106BF20`) |
| 46 | `msvc_target_version` | yes | MSVC version for compatibility (`dword_126E1D4`) |
| 47 | `host-stub-linkage-explicit` | no | Use explicit linkage on host stubs |
| 48 | `static-host-stub` | no | Generate static host stubs |
| 49 | `device-hidden-visibility` | no | Apply hidden visibility to device symbols |
| 50 | `no-hidden-visibility-on-unnamed-ns` | no | Exempt unnamed namespaces from hidden visibility |
| 51 | `no-multiline-debug` | no | Disable multiline debug info |
| 52 | `no-device-int128` | no | Disable `__int128` on device |
| 53 | `no-device-float128` | no | Disable `__float128` on device |
| 54 | `fe-inlining` | no | Enable frontend inlining (`dword_106C068 = 1`) |
| 55 | `modify-stack-limit` | yes | Control stack limit modification (`dword_106C064`) |
| 56 | `fassociative-math` | no | Enable associative floating-point math |
| 57 | `orig_src_file_name` | yes | Original source file name (before preprocessing) |
| 58 | `orig_src_path_name` | yes | Original source path name (full path) |
| 59 | `frandom-seed` | yes | Random seed for reproducible output |
| 60 | `check-template-param-qual` | no | Check template parameter qualifications |
| 61 | `check-clock-call` | no | Validate `clock()` calls in device code |
| 62 | `check-ffs-call` | no | Validate `ffs()` calls in device code |
| 63 | `check-routine-address-taken` | no | Check when device routine address is taken |
| 64 | `check-memory-clobber` | no | Validate memory clobber in inline asm |
| 65 | `force-lp64` | no | LP64 data model: pointer=8, long=8 |
| 66 | `force-llp64` | no | LLP64 data model: pointer=4, long=4 |
| 67 | `pgi_llvm` | no | PGI/LLVM backend mode |
| 68 | `pgi_arch_ppc` | no | PGI PowerPC architecture |
| 69 | `pgi_arch_aarch64` | no | PGI AArch64 architecture |
| 70 | `pgi_version` | yes | PGI compiler version number |
| 71 | `keep-device-functions` | no | Do not strip unused device functions |
| 72 | `device-syntax-only` | no | Device-side syntax check without codegen |
| 73 | `device-time-trace` | no | Enable device compilation time tracing |
| 74 | `force_linkonce_to_weak` | no | Convert linkonce to weak linkage |
| 75 | `disable_host_implicit_call_check` | no | Skip implicit call validation on host |
| 76 | `no_strict_cuda_error` | no | Relax strict CUDA error checking |
| 77 | `device-c` | no | Relocatable device code (RDC) mode |
| 78 | `no-shadow-functions` | no | Disable function shadowing in device code |
| 79 | `disable_ext_lambda_cache` | no | Disable extended lambda capture cache |
| 80 | `no-constant-variable-inferencing` | no | Disable constexpr variable inference on device |
| 81 | `zero_init_auto_vars` | no | Zero-initialize automatic variables |
| 82 | `debug_mode` | no | Full debug mode (sets 3 debug globals to 1) |
| 83 | `gen_module_id_file` | no | Generate module ID file |
| 84 | `include_file_name` | yes | Forced include file name |
| 85 | `gen_device_file_name` | yes | Device-side output file name |
| 86 | `stub_file_name` | yes | Stub file output path |
| 87 | `module_id_file_name` | yes | Module ID file path |
| 88 | `tile_bc_file_name` | yes | Tile bitcode file path |
| 89 | `tile-only` | no | Tile-only compilation mode |

### Architecture and Host Compiler Flags (90–114)

These flags identify the target architecture and host compiler for compatibility emulation.

| ID | Flag | Short | Arg | Effect |
|---|---|---|---|---|
| 90 | `m32` | — | no | 32-bit mode: pointer=4, long=4, all types sized for ILP32 |
| 91 | `m64` | — | no | 64-bit mode (default on Linux x86-64) |
| 92 | `Version` | `-V` | no | Print version with different copyright format, then `exit(1)` |
| 93 | `compiler_bindir` | — | yes | Host compiler binary directory |
| 94 | `sdk_dir` | — | yes | SDK directory path |
| 95 | `pgc++` | — | no | PGI C++ compiler mode |
| 96 | `icc` | — | no | Intel ICC compiler mode |
| 97 | `icc_version` | — | yes | Intel ICC version number |
| 98 | `icx` | — | no | Intel ICX (oneAPI) compiler mode |
| 99 | `grco` | — | no | GRCO compiler mode |
| 100 | `allow_managed` | — | no | Allow `__managed__` variable declarations |
| 101 | `gen_system_templates_from_text` | — | no | Generate system templates from text |
| 102 | `no_host_device_initializer_list` | — | no | Disable HD `initializer_list` support |
| 103 | `no_host_device_move_forward` | — | no | Disable HD `std::move`/`std::forward` |
| 104 | `relaxed_constexpr` | — | no | Relaxed constexpr rules for device code (`--expt-relaxed-constexpr`) |
| 105 | `dont_suppress_host_wrappers` | — | no | Emit host wrapper functions unconditionally |
| 106 | `arm_cross_compiler` | — | no | ARM cross-compilation mode |
| 107 | `target_woa` | — | no | Windows on ARM target |
| 108 | `gen_div_approx_no_ftz` | — | no | Generate approximate division without flush-to-zero |
| 109 | `gen_div_approx_ftz` | — | no | Generate approximate division with flush-to-zero |
| 110 | `shared_address_immutable` | — | no | Shared memory addresses are immutable |
| 111 | `uumn` | — | no | Unnamed union member naming |

### C++ Language Feature Toggle Flags (115–275)

The largest group — approximately 120 paired boolean toggles that control individual C++ language features. Most are inherited from EDG's configuration surface. Each pair shares a `case_id` and sets a global variable to 1 (`--flag`) or 0 (`--no_flag`).

#### Precompiled Headers (115–121)

| ID | Flag | Arg | Effect |
|---|---|---|---|
| 115 | `unsigned_wchar_t` | no | `wchar_t` is unsigned |
| 116 | `create_pch` | yes | Create precompiled header file |
| 117 | `use_pch` | yes | Use existing precompiled header |
| 118 | `pch` | no | Enable PCH mode |
| 119 | `pch_messages / no_pch_messages` | no | Show/hide PCH status messages |
| 120 | `pch_verbose / no_pch_verbose` | no | Verbose PCH output |
| 121 | `pch_dir` | yes | PCH file directory |

#### Core C++ Feature Toggles (122–170)

| ID | Flag | Arg | Default |
|---|---|---|---|
| 122 | `restrict / no_restrict` | no | on |
| 123 | `long_lifetime_temps / short_lifetime_temps` | no | — |
| 124 | `wchar_t_keyword / no_wchar_t_keyword` | no | on |
| 125 | `pack_alignment` | yes | — |
| 126 | `alternative_tokens / no_alternative_tokens` | no | on |
| 127 | `svr4 / no_svr4` | no | — |
| 128 | `brief_diagnostics / no_brief_diagnostics` | no | — |
| 129 | `nonconst_ref_anachronism / no_nonconst_ref_anachronism` | no | — |
| 130 | `no_preproc_only` | no | — |
| 131 | `rtti / no_rtti` | no | on |
| 132 | `building_runtime` | no | — |
| 133 | `bool / no_bool` | no | on |
| 134 | `array_new_and_delete / no_array_new_and_delete` | no | — |
| 135 | `explicit / no_explicit` | no | — |
| 136 | `namespaces / no_namespaces` | no | on |
| 137 | `using_std / no_using_std` | no | — |
| 138 | `remove_unneeded_entities / no_remove_unneeded_entities` | no | on |
| 139 | `typename / no_typename` | no | — |
| 140 | `implicit_typename / no_implicit_typename` | no | on |
| 141 | `special_subscript_cost / no_special_subscript_cost` | no | — |
| 143 | `old_style_preprocessing` | no | — |
| 144 | `old_for_init / new_for_init` | no | — |
| 145 | `for_init_diff_warning / no_for_init_diff_warning` | no | — |
| 146 | `distinct_template_signatures / no_distinct_template_signatures` | no | — |
| 147 | `guiding_decls / no_guiding_decls` | no | on |
| 148 | `old_specializations / no_old_specializations` | no | on |
| 149 | `wrap_diagnostics / no_wrap_diagnostics` | no | — |
| 150 | `implicit_extern_c_type_conversion / no_implicit_extern_c_type_conversion` | no | — |
| 151 | `long_preserving_rules / no_long_preserving_rules` | no | — |
| 152 | `extern_inline / no_extern_inline` | no | — |
| 153 | `multibyte_chars / no_multibyte_chars` | no | — |
| 154 | `embedded_c++` | no | Embedded C++ mode |
| 155 | `vla / no_vla` | no | — |
| 156 | `enum_overloading / no_enum_overloading` | no | — |
| 157 | `nonstd_qualifier_deduction / no_nonstd_qualifier_deduction` | no | — |
| 158 | `late_tiebreaker / early_tiebreaker` | no | — |
| 159 | `preinclude` | yes | — |
| 160 | `preinclude_macros` | yes | — |
| 161 | `pending_instantiations` | yes | — |
| 162 | `const_string_literals / no_const_string_literals` | no | on |
| 163 | `class_name_injection / no_class_name_injection` | no | on |
| 164 | `arg_dep_lookup / no_arg_dep_lookup` | no | on |
| 165 | `friend_injection / no_friend_injection` | no | on |
| 166 | `nonstd_using_decl / no_nonstd_using_decl` | no | — |
| 168 | `designators / no_designators` | no | — |
| 169 | `extended_designators / no_extended_designators` | no | — |
| 170 | `variadic_macros / no_variadic_macros` | no | — |
| 171 | `extended_variadic_macros / no_extended_variadic_macros` | no | — |

#### Include Paths and Module Support (167, 172, 256–265)

**Note:** These flags use non-contiguous IDs because `sys_include` and `incl_suffixes` are registered early, while the C++20 module flags use a separate ID range (256+).

| ID | Flag | Arg | Effect |
|---|---|---|---|
| 167 | `sys_include` | yes | System include directory |
| 172 | `incl_suffixes` | yes | Include file suffix list (default `"::stdh:"`) |
| 256 | `modules_directory` | yes | C++20 modules directory |
| 257 | `ms_mod_file_map` | yes | MSVC module file mapping |
| 258 | `ms_header_unit` | yes | MSVC header unit |
| 259 | `ms_header_unit_quote` | yes | MSVC quoted header unit |
| 260 | `ms_header_unit_angle` | yes | MSVC angle-bracket header unit |
| 261 | `ms_mod_interface / no_ms_mod_interface` | no | MSVC module interface mode |
| 262 | `ms_internal_partition / no_ms_internal_partition` | no | MSVC internal partition mode |
| 263 | `ms_translate_include / no_ms_translate_include` | no | MSVC translate `#include` to `import` |
| 264 | `modules / no_modules` | no | Enable/disable C++20 modules |
| 265 | `module_import_diagnostics / no_module_import_diagnostics` | no | Module import diagnostic messages |

#### Host Compiler and Language Feature Toggles (182–239)

**Note:** All IDs below are verified against the decompiled `init_command_line_flags` (`sub_452010`). Flags are registered by `sub_451F80` (explicit call) or by inline array population. IDs are not sequential — gaps exist where flags were removed or repurposed.

| ID | Flag | Arg | Default |
|---|---|---|---|
| 182 | `gcc / no_gcc` | no | GCC compatibility mode |
| 183 | `g++ / no_g++` | no | G++ mode (alias for GCC C++ mode) |
| 184 | `gnu_version` | yes | GCC version number (default 80100 = GCC 8.1.0) |
| 185 | `report_gnu_extensions` | no | Report use of GNU extensions |
| 186 | `short_enums / no_short_enums` | no | Use minimal-size enum representation |
| 187 | `clang / no_clang` | no | Clang compatibility mode |
| 188 | `clang_version` | yes | Clang version number (default 90100 = Clang 9.1.0) |
| 189 | `strict_gnu / no_strict_gnu` | no | Strict GNU mode |
| 190 | `db_name` | yes | Debug database name |
| 191 | `long_long` | no | Allow `long long` type |
| 192 | `context_limit` | yes | Maximum template instantiation context depth |
| 193 | `set_flag / clear_flag` | yes | Raw flag manipulation via `off_D47CE0` lookup table |
| 194 | `edg_base_dir` | yes | EDG base directory (error on invalid path) |
| 195 | `embedded_c / no_embedded_c` | no | Embedded C mode (not relevant to CUDA) |
| 196 | `thread_local_storage / no_thread_local_storage` | no | `thread_local` support |
| 197 | `trigraphs / no_trigraphs` | no | Trigraph processing (default on) |
| 198 | `nonstd_default_arg_deduction / no_nonstd_default_arg_deduction` | no | — |
| 199 | `stdc_zero_in_system_headers / no_stdc_zero_in_system_headers` | no | — |
| 200 | `template_typedefs_in_diagnostics / no_template_typedefs_in_diagnostics` | no | — |
| 202 | `uliterals / no_uliterals` | no | Unicode literals (`u""`, `U""`, `u8""`) |
| 203 | `type_traits_helpers / no_type_traits_helpers` | no | Intrinsic type traits |
| 204 | `c++11 / c++0x` | no | C++11 mode (sets `dword_126EF68` to 201103 or 199711) |
| 205 | `list_macros` | no | List all defined macros after preprocessing |
| 206 | `dump_configuration` | no | Dump full compiler configuration |
| 207 | `dump_legacy_as_target` | yes | Dump legacy configuration in target format |
| 208 | `signed_bit_fields / unsigned_bit_fields` | no | Default bit-field signedness |
| 210 | `check_concatenations / no_check_concatenations` | no | String literal concatenation checks |
| 211 | `unicode_source_kind` | yes | Source encoding: `UTF-8`=1, `UTF-16LE`=2, `UTF-16BE`=3, `none`=0 |
| 212 | `lambdas / no_lambdas` | no | C++ lambda expressions |
| 213 | `rvalue_refs / no_rvalue_refs` | no | Rvalue references |
| 214 | `rvalue_ctor_is_copy_ctor / rvalue_ctor_is_not_copy_ctor` | no | Rvalue constructor treatment |
| 215 | `gen_move_operations / no_gen_move_operations` | no | Implicit move constructor/assignment (default on) |
| 216 | `auto_type / no_auto_type` | no | C++11 `auto` type deduction |
| 217 | `auto_storage / no_auto_storage` | no | `auto` as storage class (C++03 meaning) |
| 218 | `nonstd_instantiation_lookup / no_nonstd_instantiation_lookup` | no | — |
| 219 | `nullptr / no_nullptr` | no | `nullptr` keyword |
| 220 | `gcc89_inlining` | no | GCC 8.9-era inlining behavior |
| 221 | `nonstd_gnu_keywords / no_nonstd_gnu_keywords` | no | GNU extension keywords |
| 222 | `default_nocommon_tentative_definitions / default_common_tentative_definitions` | no | Tentative definition linkage |
| 223 | `no_token_separators_in_pp_output` | no | — |
| 224 | `c23_typeof / no_c23_typeof` | no | C23 `typeof` operator |
| 225 | `c++11_sfinae / no_c++11_sfinae` | no | C++11 SFINAE rules |
| 226 | `c++11_sfinae_ignore_access / no_c++11_sfinae_ignore_access` | no | Ignore access checks in SFINAE |
| 227 | `variadic_templates / no_variadic_templates` | no | Parameter packs and pack expansion |
| 228 | `c++03` | no | C++03 mode (sets `dword_126EF68` to 199711) |
| 229 | `func_prototype_tags / no_func_prototype_tags` | no | — |
| 230 | `implicit_noexcept / no_implicit_noexcept` | no | Implicit `noexcept` on destructors |
| 231 | `unrestricted_unions / no_unrestricted_unions` | no | Unrestricted unions (C++11) |
| 232 | `max_depth_constexpr_call` | yes | Maximum `constexpr` recursion depth (default 200) |
| 233 | `max_cost_constexpr_call` | yes | Maximum `constexpr` evaluation cost (default 256) |
| 234 | `delegating_constructors / no_delegating_constructors` | no | — |
| 235 | `lossy_conversion_warning / no_lossy_conversion_warning` | no | — |
| 236 | `deprecated_string_conv / no_deprecated_string_conv` | no | Deprecated string literal to `char*` conversion |
| 237 | `user_defined_literals / no_user_defined_literals` | no | UDL support |
| 238 | `preserve_lvalues_with_same_type_casts / no_...` | no | — |
| 239 | `nonstd_anonymous_unions / no_nonstd_anonymous_unions` | no | — |

#### Late C++/Architecture/Output Flags (240–258)

| ID | Flag | Arg | Effect |
|---|---|---|---|
| 240 | `c++14` | no | C++14 mode (sets `dword_126EF68` to 201402) |
| 241 | `c11` | no | C11 mode (sets `dword_126EF68` to 201112) |
| 242 | `c17` | no | C17 mode (sets `dword_126EF68` to 201710) |
| 243 | `c23` | no | C23 mode (sets `dword_126EF68` to 202311) |
| 244 | `digit_separators / no_digit_separators` | no | C++14 digit separators (`1'000'000`) |
| 245 | `target` | yes | SM architecture string, parsed via `sub_7525E0` into `dword_126E4A8` |
| 246 | `c++17` | no | C++17 mode (sets `dword_126EF68` to 201703) |
| 247 | `utf8_char_literals / no_utf8_char_literals` | no | UTF-8 character literal support |
| 248 | `stricter_template_checking` | no | Additional template constraint checks |
| 249 | `exc_spec_in_func_type / no_exc_spec_in_func_type` | no | Exception spec as part of function type (C++17) |
| 250 | `aligned_new / no_aligned_new` | no | Aligned `operator new` (C++17) |
| 251 | `c++20` | no | C++20 mode (sets `dword_126EF68` to 202002) |
| 252 | `c++23` | no | C++23 mode (sets `dword_126EF68` to 202302) |
| 253 | `ms_std_preprocessor / no_ms_std_preprocessor` | no | MSVC standard preprocessor mode |
| 268 | `partial-link` | no | Partial linking mode |
| 273 | `dump_command_options` | no | Print all registered flag names |
| 274 | `output_mode` | yes | Output format: `text` (0) or `sarif` (1) |
| 275 | `incognito / no_incognito` | no | Incognito mode |

Note: Many IDs in the 240-252 range serve double duty as both C/C++ standard selectors and feature toggles. The standard selection IDs are also cross-referenced in the [Language Standard Selection](#language-standard-selection) section above.

#### Inline-Registered Paired Flags

Seven additional paired flags are registered through inline table population rather than calls to `register_command_flag`. They share the same entry structure but are populated directly into the array:

| Flag | Effect |
|---|---|
| `relaxed_abstract_checking / no_relaxed_abstract_checking` | Relax abstract class checks |
| `concepts / no_concepts` | C++20 concepts support |
| `colors / no_colors` | Colorized diagnostic output |
| `keep_restrict_in_signatures / no_keep_restrict_in_signatures` | Preserve `restrict` in mangled names |
| `check_unicode_security / no_check_unicode_security` | Unicode security checks (homoglyph detection) |
| `old_id_chars / no_old_id_chars` | Legacy identifier character rules |
| `add_match_notes / no_add_match_notes` | Add notes about matching overloads |

## Language Standard Selection

Six language standard flags set `dword_126EF68` (the internal `__cplusplus` / `__STDC_VERSION__` value) and trigger corresponding mode changes:

### C Standards

| ID | Flag | `dword_126EF68` value | C standard |
|---|---|---|---|
| 7 | `old_c` | (K&R) | Pre-ANSI C via `set_c_mode(1)` |
| 179 | `c89` | 198912 | ANSI C / C89 |
| 178 | `c99` | 199901 | C99 |
| 241 | `c11` | 201112 | C11 |
| 242 | `c17` | 201710 | C17 |
| 243 | `c23` | 202311 | C23 |

### C++ Standards

| ID | Flag | `dword_126EF68` value | C++ standard |
|---|---|---|---|
| 228 | `c++03` | 199711 | C++98/03 (also aliased as `c++98` via `--c++11` flag ID 204 with conditional) |
| 204 | `c++11` | 201103 | C++11 (sets 199711 if `dword_E7FF14` is unset or C mode) |
| 240 | `c++14` | 201402 | C++14 |
| 246 | `c++17` | 201703 | C++17 |
| 251 | `c++20` | 202002 | C++20 |
| 252 | `c++23` | 202302 | C++23 |

When a C++ standard is selected, the post-parsing dialect resolution logic automatically enables the corresponding feature flags. For example, selecting `--c++11` (value 201103) enables lambdas, rvalue references, `auto` type deduction, `nullptr`, variadic templates, and other C++11 features. The resolution logic also interacts with GCC/Clang version thresholds to determine which extensions are available.

## Diagnostic Control Flags

The five `diag_*` flags (IDs 39–43) accept comma-separated lists of diagnostic numbers. The parser strips whitespace, splits on commas, and calls `sub_4ED400(number, severity, 1)` for each number:

```bash
--diag_suppress=1234,5678       # suppress errors 1234 and 5678
--diag_warning=20001            # demote CUDA error 20001 to warning
--diag_error=111                # promote diagnostic 111 to error
--diag_remark=185               # demote diagnostic 185 to remark
--diag_once=175                 # emit diagnostic 175 only once
```

The error number system is documented in [Diagnostic System Overview](../diagnostics/overview.md). Numbers above 3456 in the internal range correspond to the 20000-series CUDA errors via the offset formula `display_code = internal_code + 16543`.

## Post-Parsing Dialect Resolution

After the main parsing loop completes, `proc_command_line` executes a large block of dialect resolution logic that:

1. **Resolves host compiler mode conflicts** — If both `--gcc` and `--clang` are set, or `--cfront_2.1` is combined with modern modes, the resolution picks one and adjusts feature flags accordingly
2. **Sets C++ feature flags from `__cplusplus` version** — Based on the value in `dword_126EF68`:
   - `199711` (C++98/03): baseline features only
   - `201103` (C++11): enables lambdas, rvalue refs, auto, nullptr, variadic templates, range-based for, delegating constructors, unrestricted unions, user-defined literals
   - `201402` (C++14): adds digit separators, generic lambdas, relaxed constexpr
   - `201703` (C++17): adds aligned new, exception spec in function type, structured bindings
   - `202002` (C++20): adds concepts, modules, coroutines
   - `202302` (C++23): adds latest features
3. **Applies GCC version thresholds** — When in GCC compatibility mode, certain features are gated on the GCC version number stored in `qword_126EF98` (default 80100 = GCC 8.1.0). Known thresholds:
   - `40299` (0x9D6B): GCC 4.2
   - `40599` (0x9E97): GCC 4.5
   - `40699` (0x9EFB): GCC 4.6
   - Higher versions enable progressively more features
4. **Opens output files** — Error output, listing file, output file
5. **Processes the input filename** — The remaining non-flag argv entry

### Key Globals After Resolution

| Global | Type | Content |
|---|---|---|
| `dword_126EF68` | `int32` | `__cplusplus` / `__STDC_VERSION__` value |
| `dword_126EFB4` | `int32` | Language mode: 0=unset, 1=C, 2=C++ |
| `dword_126EFA8` | `int32` | GCC compatibility enabled |
| `dword_126EFA4` | `int32` | Clang compatibility enabled |
| `qword_126EF98` | `int64` | GCC version (default 80100) |
| `qword_126EF90` | `int64` | Clang version (default 90100) |
| `dword_126EFB0` | `int32` | GNU extensions enabled |
| `dword_126EFAC` | `int32` | Clang extensions enabled |
| `dword_126E4A8` | `int32` | SM architecture code (from `--target`) |
| `dword_126E1D4` | `int32` | MSVC target version |

## The set_flag / clear_flag Mechanism

Flag ID 199 (`--set_flag` / `--clear_flag`) provides a raw escape hatch. The argument is a flag name looked up in the `off_D47CE0` table — an array of `{name, global_address}` pairs. If the name is found, the corresponding global variable is set to the provided integer value (`--set_flag=name=value`) or cleared to 0 (`--clear_flag=name`). This mechanism allows nvcc to toggle internal EDG configuration flags that do not have dedicated CLI flag registrations.

## Default Values

`default_init` (`sub_45EB40`) runs before `proc_command_line` and initializes approximately 350 global configuration variables. Notable non-zero defaults:

| Global | Default | Meaning |
|---|---|---|
| `dword_106C210` | 1 | Exceptions enabled |
| `dword_106C180` | 1 | RTTI enabled |
| `dword_106C178` | 1 | `bool` is keyword |
| `dword_106C194` | 1 | Namespaces enabled |
| `dword_106C19C` | 1 | Argument-dependent lookup enabled |
| `dword_106C1A0` | 1 | Class name injection enabled |
| `dword_106C1A4` | 1 | String literals are const |
| `dword_106C188` | 1 | `wchar_t` is keyword |
| `dword_106C18C` | 1 | Alternative tokens enabled |
| `dword_106C140` | 1 | Compound literals allowed |
| `dword_106C138` | 1 | Dependent name processing enabled |
| `dword_106C134` | 1 | Template parsing enabled |
| `dword_106C12C` | 1 | Friend injection enabled |
| `dword_106BDB8` | 1 | `restrict` enabled |
| `dword_106BDB0` | 1 | Remove unneeded entities enabled |
| `dword_106BD98` | 1 | Trigraphs enabled |
| `dword_106BD68` | 1 | Guiding declarations allowed |
| `dword_106BD58` | 1 | Old specializations allowed |
| `dword_106BD54` | 1 | Implicit typename enabled |
| `dword_106BE84` | 1 | Generate move operations enabled |
| `dword_106C064` | 1 | Stack limit modification enabled |
| `qword_106BD10` | 200 | Max constexpr recursion depth |
| `qword_106BD08` | 256 | Max constexpr evaluation cost |
| `qword_126EF98` | 80100 | Default GCC version (8.1.0) |
| `qword_126EF90` | 90100 | Default Clang version (9.1.0) |
| `qword_126EF78` | 1926 | MSVC version threshold |
| `qword_126EF70` | 99999 | Some upper bound sentinel |

## Conflict Detection

Before the main parsing loop, `check_conflicting_flags` (`sub_451E80`) verifies that flags 3, 193, 194, and 195 (`no_line_commands`, `set_flag`, `clear_flag`, and related flags) are not used in conflicting combinations. If any conflict is detected, error 1027 is emitted.

## Inline-Registered Flags

`init_command_line_flags` (`sub_452010`) calls the helper `register_command_flag` (`sub_451F80`) 251 times. The remaining 25 flags reach the dispatch switch through a different mechanism: their entries are populated directly into the flag table by inline stores in `sub_452010`, bypassing the helper. The two registration mechanisms produce identical runtime behavior — both result in valid 40-byte entries in the table at `dword_E80060`. The distinction is purely a source-level artifact, and is invisible from the parser's perspective.

There are two reasons a flag is registered inline rather than through the helper:

1. **Non-default field values.** A few flags need a `visible` bit pattern, target compiler gate, or `takes_value` configuration that the helper's parameter signature cannot express. Inline stores write the exact byte layout required.

2. **Paired-toggle positive forms.** EDG paired toggles register both `--no_flag` and `--flag` to the same `case_id`. The `--no_flag` form is registered through the helper (with the negation-bit flag set). The `--flag` form is then written inline at the next table slot, sharing the same `case_id` but with the negation bit clear. The helper is not parameterized for the positive form, so inline stores are the only path.

The 25 inline-registered flag names:

| Category | Flag names |
|---|---|
| Standalone (no paired toggle) | `target`, `version`, `output_mode`, `m32`, `m64`, `db`, `icc`, `icx`, `grco`, `uumn`, `incognito`, `xref`, `list` |
| Positive forms of paired toggles | `lambdas`, `modules`, `restrict`, `bool`, `rtti`, `namespaces`, `trigraphs`, `nullptr`, `c++11`, `c++0x`, `c89`, `c99`, `c11`, `c17`, `c23` |

(The first row has 13 names; the second row has 15 names. The "approximately 25" figure in the wave-58 audit collapses the two `c++11`/`c++0x` aliases that share case ID 247, and the `c11`/`c17`/`c23` C-mode group that share case IDs, into the lower count.)

**Sum check.** 251 helper-registered flag entries + 25 inline-registered entries = 276 total table entries. This matches the `init_command_line_flags` exit state, the `dword_E80058` counter immediately after registration completes, and the case count of the dispatch switch (IDs 0..275, where 0 is the default sink for unrecognized command-line input).

## Version Banners

The CLI exposes two distinct version-printing flags. They share the substring "version" in their long name but differ in case (`--version` vs. `--Version`), parsing case ID, format of the printed banner, and whether they terminate the process.

### `--version` (ID 21, `-v`) — continues execution

Case 21 in the dispatch switch writes the cudafe++ version banner to `stdout` via the standard print path, then falls through to the next iteration of the argv loop without calling `exit()`. The flag is used by build systems that want to query the compiler version while still receiving any subsequent flags on the same command line.

Banner format (lines emitted in order):

```text
cudafe: NVIDIA (R) Cuda Language Front End
Portions Copyright (c) 2005, 2024-YYYY NVIDIA Corporation
Portions Copyright (c) 1988-2018, 2024 Edison Design Group Inc.
Based on Edison Design Group C/C++ Front End, version 6.6
Cuda compilation tools, release 13.0, V13.0.88
```

The `YYYY` placeholder is the current compilation year, sourced from a build-time constant. The string `"1988-2018, 2024"` is the EDG copyright span — the two date ranges in a single Edison Design Group copyright line are an explicit choice by EDG, not a formatting artifact.

### `--Version` (ID 92, `-V`) — emits EDG portion banner then `exit(1)`

Case 92 in the dispatch switch writes a single EDG-portion copyright line to `stdout`, then calls `exit(1)` immediately. The exit code is the conventional "success but no compilation performed" value used by GNU-style version flags. No further argv processing occurs after case 92.

Banner string (single line):

```text
Portions Copyright (c) 1988-2016 Edison Design Group Inc.
```

The `"1988-2016"` date range here is intentionally different from the `"1988-2018, 2024"` range printed by `--version`: case 92 emits the older, narrower EDG portion-only copyright form. The two banners do not share string data in `.rodata`; they are two separate string literals, embedded at distinct addresses. Tooling that grep's the binary for either banner should not assume the other will be present in the same form.

## Cross-References

- [Pipeline Overview](../pipeline/overview.md) — Stage 2 is `proc_command_line`
- [Diagnostic System Overview](../diagnostics/overview.md) — `diag_suppress`/`diag_error` flag handling
- [Architecture Detection](arch-detection.md) — `--target` flag and SM version parsing
- [Experimental Flags](experimental-flags.md) — `--set_flag`/`--clear_flag` for internal feature gates
- [EDG 6.6 Overview](../edg/overview.md) — `cmd_line.c` source file context
