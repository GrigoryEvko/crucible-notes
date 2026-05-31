# CLI Processing

`proc_command_line` (`sub_459630`) at `0x459630` is a 21,773-byte function (4,105 decompiled lines, 296 callees) in `cmd_line.c` that parses the entire cudafe++ command line. It registers 276 flags into a flat lookup table, iterates `argv` with prefix-matching against that table, dispatches each matched flag through a 276-case switch statement (IDs 0..275, where case 0 is the default sink for unknown flags), then resolves language dialect settings and opens output files. This function is the second stage of the [pipeline](./overview.md), called directly from `main()` at `0x408950` before any heavy initialization.

Nobody invokes cudafe++ directly. NVIDIA's driver compiler `nvcc` decomposes its own options and passes the appropriate low-level flags via `-Xcudafe <flag>`. The full flag inventory is in [CLI Flag Inventory](../config/cli-flags.md); this page documents the **implementation mechanics** of the parsing system itself.

## Key Facts

| Property | Value |
|---|---|
| Address | `0x459630` |
| Binary size | 21,773 bytes |
| Decompiled lines | 4,105 |
| Source file | `cmd_line.c` |
| Signature | `int64_t proc_command_line(int argc, char** argv)` |
| Direct callees | 296 |
| Flag table base | `dword_E80060` |
| Flag table entry size | 40 bytes |
| Flag table capacity | 552 entries (overflow panics via `sub_40351D`) |
| Registered flags | 276 |
| Switch cases | 276 (IDs 0..275; case 0 is the default sink for unknown flags) |
| Default-suppressed diagnostics | 9 (1257, 1373, 1374, 1375, 1633, 2330, 111, 185, 175) |

## Flag Table Layout

The flag table is a contiguous array starting at `dword_E80060`. Each of the 552 slots occupies 40 bytes. The current entry count is tracked in `dword_E80058`.

```text
Offset   Field            Type       Access pattern
------   -----            ----       --------------
+0       case_id          int32      dword_E80060[idx * 10]
+8       name             char*      qword_E80068[idx * 5]
+16      short_char       int16      word_E80070[idx * 20]    (low byte = char, high byte = 1)
+17      is_valid         int8       (high byte of short_char word, always 1)
+18      takes_value      int8       byte_E80072[idx * 40]
+19      visible          int8       (part of dword_E80080[idx * 10] at +32)
+20      is_boolean       int8       byte_E80073[idx * 40]
+24      name_length      int64      qword_E80078[idx * 5]    (precomputed strlen)
+32      mode_flag        int32      dword_E80080[idx * 10]
```

The flag-was-set bitmap at `byte_E7FF40` spans `0x110` bytes (272 slots). When a flag is matched during parsing, the corresponding byte is set to 1 to record that the user explicitly provided it. The bitmap is zeroed by `default_init` (`sub_45EB40`) before every compilation.

## Registration: sub_452010 (init_command_line_flags)

`sub_452010` at `0x452010` is a 30,133-byte function (3,849 decompiled lines) that populates the entire flag table. It is called once, at line 280 of `proc_command_line`, before the parsing loop begins.

### register_command_flag (sub_451F80)

Each flag is registered through `sub_451F80` (25 lines), called approximately 275 times from `sub_452010`:

```c
void register_command_flag(
    int    case_id,       // dispatch ID for the switch (1-275)
    char*  name,          // flag name without dashes ("preprocess", "timing", etc.)
    char   short_opt,     // single-char alias ('E', '#', etc.), 0 for none
    char   takes_value,   // 1 if the flag requires =<value>
    int    mode_flag,     // visibility/classification (mode vs. action)
    char   enabled        // whether the flag is active (1 = registered, 0 = disabled)
);
```

The function writes into the next free slot at index `dword_E80058`, precomputes `strlen(name)` into `name_length`, always sets the `is_valid` byte to 1, then increments the counter. If the counter reaches 552, it panics via `sub_40351D` -- the table is statically sized.

### Paired Toggle Registration

Approximately half of all flags are boolean toggles registered as pairs: `--flag` and `--no_flag` share the same `case_id` but differ in which value they write. Pairs are registered in two ways:

1. **Two sequential `register_command_flag` calls** -- both point to the same `case_id`; the parsing loop determines whether the matched name starts with `no_` and sets the target global to 0 or 1 accordingly.

2. **Inline table population** -- seven additional paired flags (`relaxed_abstract_checking`, `concepts`, `colors`, `keep_restrict_in_signatures`, `check_unicode_security`, `old_id_chars`, `add_match_notes`) are written directly into the array without going through `register_command_flag`.

## Parsing Loop

After flag registration, `proc_command_line` performs five sequential setup steps, then enters the main argv iteration.

### Pre-Loop Setup

```text
Step 1:  Initialize qword_126DD38, qword_126EDE8 (token state / source position)
Step 2:  Call sub_452010() -- register all 276 flags
Step 3:  Allocate 4 hash tables (16-byte header + 256-byte data each):
           qword_106C248  macro define/alias map
           qword_106C240  include path list
           qword_106C238  system include map
           qword_106C228  additional system include map
Step 4:  Suppress 9 diagnostic numbers by default:
           1257, 1373, 1374, 1375, 1633, 2330, 111, 185, 175
         Each via sub_4ED400(number, suppress_severity, 1)
Step 5:  Set dword_E7FF20 = 1 (argv index, skipping argv[0])
```

The default-suppressed diagnostics are EDG warnings that NVIDIA considers noise for CUDA compilation. Diagnostic 111 ("statement is unreachable"), 185 ("pointless comparison of unsigned integer with zero"), and 175 ("subscript out of range") are common false positives in CUDA template-heavy code.

### argv Iteration

The loop processes `argv[dword_E7FF20]` through `argv[argc-1]`. For each argument:

1. **Dash detection** -- if the argument does not start with `-`, it is treated as the input filename (stored in `qword_126EEE0`). Only one non-flag argument is expected.

2. **Short flag matching** -- for single-dash arguments (`-X`), the parser scans the flag table for an entry whose `short_char` matches. If the flag `takes_value`, the next argv element is consumed as the value.

3. **Long flag matching** -- for double-dash arguments (`--flag-name`), the parser calls `parse_flag_name_value` (`sub_451EC0`) to split on `=`:

```c
// sub_451EC0: split "--name=value" into name and value
// Respects backslash escapes and quoted strings
// If no '=' found: *name_out = src, *value_out = NULL
void parse_flag_name_value(char* src, char** name_out, char** value_out);
```

The name portion is then matched against the flag table using `strncmp` with each entry's precomputed `name_length`. The parser iterates all entries and counts exact and prefix matches:

- **Exact match** (length equals `name_length` and `strncmp` returns 0) -- dispatches immediately.
- **Unique prefix match** (only one entry's name starts with the given prefix) -- dispatches to that entry.
- **Ambiguous prefix** (multiple entries match the prefix) -- emits error 923 ("ambiguous command-line option").
- **No match** -- the argument is silently ignored or treated as input.

### Conflict Detection

Before the main loop, `check_conflicting_flags` (`sub_451E80`, 15 lines) validates that mutually exclusive flags were not specified together. It checks `byte_E7FFF2 || byte_E80031 || byte_E80032 || byte_E80033`, corresponding to flags 3, 193, 194, and 195. If any conflict is detected, it emits error 1027 via `sub_4F8480`.

## The Dispatch Switch (276 Cases)

After a flag is matched, its `case_id` indexes into a giant switch statement occupying the bulk of `proc_command_line`. The following sections document the most important cases grouped by function.

### Preprocessor Control (Cases 3--9)

| Case | Flag | Global(s) | Behavior |
|---|---|---|---|
| 3 | `no_line_commands` | `dword_106C29C`=1, `dword_106C294`=1, `dword_106C288`=0 | Suppress `#line` in preprocessor output |
| 4 | `preprocess` | `dword_106C29C`=1, `dword_106C294`=1, `dword_106C288`=1 | Preprocessor-only mode (output to stdout) |
| 5 | `comments` | *(flag bitmap)* | Preserve comments in preprocessor output |
| 6 | `old_line_commands` | *(flag bitmap)* | Use old-style `# N "file"` line directives |
| 8 | `dependencies` | *(multiple)* | Dependencies output mode (preprocessor-only + dependency emission) |
| 9 | `trace_includes` | *(flag bitmap)* | Print each `#include` as it is opened |

### Compilation Mode (Cases 14, 20--26)

| Case | Flag | Global | Behavior |
|---|---|---|---|
| 14 | `no_code_gen` | `dword_106C254 = 1` | Parse-only mode -- sets the skip-backend flag, preventing `process_file_scope_entities` from running |
| 20 | `timing` | `dword_106C0A4 = 1` | Enable compilation phase timing. `main()` checks this flag to decide whether to call `sub_5AF350`/`sub_5AF390` for "Front end time", "Back end time", "Total compilation time" |
| 21 | `version` | *(stdout)* | Print the version banner and continue (does not exit). Banner includes: `"cudafe: NVIDIA (R) Cuda Language Front End"`, `"Portions Copyright (c) 2005, 2024-YYYY NVIDIA Corporation"`, `"Portions Copyright (c) 1988-2018, 2024 Edison Design Group Inc."`, `"Based on Edison Design Group C/C++ Front End, version 6.6"`, `"Cuda compilation tools, release 13.0, V13.0.88"` |
| 22 | `no_warnings` | `byte_126ED69 = 7` | Set diagnostic severity threshold to error-only (suppress all warnings and remarks) |
| 23 | `promote_warnings` | `byte_126ED68 = 5` | Promote all warnings to errors |
| 24 | `remarks` | `byte_126ED69 = 4` | Lower threshold to include remark-level diagnostics |
| 25 | `c` | calls `sub_44C4F0(0)` | Force C language mode (overrides default C++ if currently in C++ mode) |
| 26 | `c++` | calls `sub_44C4F0(2)` | Force C++ language mode |

### Diagnostic Control (Cases 39--44)

Cases 39--43 (`diag_suppress`, `diag_remark`, `diag_warning`, `diag_error`, `diag_once`) share the same value-parsing logic:

```text
1. Read the value string (after '=')
2. Strip leading/trailing whitespace
3. Split on commas
4. For each token:
   a. Parse as integer (diagnostic number)
   b. Call sub_4ED400(number, severity, 1)
```

The severity values map to:
- Suppress = skip entirely
- Remark = informational (level 4)
- Warning = default warning (level 5)
- Error = hard error (level 7)
- Once = emit on first occurrence only

Case 44 (`display_error_number` / `no_display_error_number`) toggles whether error codes appear in diagnostic messages.

### CUDA-Specific Flags (Cases 45--89)

#### Output File Paths

| Case | Flag | Global | Description |
|---|---|---|---|
| 45 | `gen_c_file_name` | `qword_106BF20` | Path for the generated `.int.c` file |
| 85 | `gen_device_file_name` | *(has_arg global)* | Device-side output file name |
| 86 | `stub_file_name` | *(has_arg global)* | Stub file output path |
| 87 | `module_id_file_name` | *(has_arg global)* | Module ID file path |
| 88 | `tile_bc_file_name` | *(has_arg global)* | Tile bitcode file path |

#### Data Model (Cases 65--66, 90--91)

| Case | Flag | Behavior |
|---|---|---|
| 65 | `force-lp64` | LP64 model: pointer size=8, long size=8, specific type encodings for 64-bit |
| 66 | `force-llp64` | LLP64 model (Windows): pointer size=4, long size=4 |
| 90 | `m32` | ILP32 model: all type sizes set for 32-bit (pointer=4, long=4, etc.) |
| 91 | `m64` | 64-bit mode (default on Linux x86-64) |

#### Device Compilation Control

| Case | Flag | Global | Description |
|---|---|---|---|
| 46 | `msvc_target_version` | `dword_126E1D4` | MSVC version for compatibility emulation |
| 47 | `host-stub-linkage-explicit` | boolean | Use explicit linkage on generated host stubs |
| 48 | `static-host-stub` | boolean | Generate static (internal linkage) host stubs |
| 49 | `device-hidden-visibility` | boolean | Apply hidden visibility to device symbols |
| 52 | `no-device-int128` | boolean | Disable `__int128` type support on device |
| 53 | `no-device-float128` | boolean | Disable `__float128` type support on device |
| 54 | `fe-inlining` | `dword_106C068 = 1` | Enable frontend inlining pass |
| 55 | `modify-stack-limit` | `dword_106C064` | Whether `main()` raises the process stack limit via `setrlimit`. Default is ON. Value parsed as integer: nonzero enables, zero disables. |
| 71 | `keep-device-functions` | boolean | Do not strip unused device functions |
| 72 | `device-syntax-only` | boolean | Device-side syntax check without code generation |
| 77 | `device-c` | boolean | Relocatable device code (RDC) mode |
| 82 | `debug_mode` | `dword_106BFC4`=1, `dword_106BFC0`=1, `dword_106BFBC`=1 | Full debug mode (sets three debug globals simultaneously) |
| 89 | `tile-only` | boolean | Tile-only compilation mode |

### Template Instantiation (Case 16)

The `instantiate` flag takes a string value and sets `dword_106C094`:

| Value | `dword_106C094` | Meaning |
|---|---|---|
| `"none"` | 0 | No implicit instantiation |
| `"all"` | 1 | Instantiate all referenced templates |
| `"used"` | 2 | Instantiate only used templates |
| `"local"` | 3 | Local instantiation only |

### Include and Macro Arguments (Cases 29--31)

Cases 29 (`include_directory` / `-I`) and 167 (`sys_include`) append entries to linked lists via `sub_4595D0`:

```c
// sub_4595D0: append_to_linked_list
// Allocates a 24-byte node: {next_ptr, string_ptr, int_field}
// Appends to singly-linked list with head/tail pointers
void append_to_linked_list(list_head*, char* string, int type);
```

A special case: `-I-` (the literal string `"-"`) sets a flag for stdin include mode rather than appending to the path list. It calls `sub_5AD0A0` for the actual path registration.

Case 30 (`define_macro` / `-D`) builds a linked list of macro definitions via `sub_4595D0`. Case 31 (`undefine_macro` / `-U`) allocates the same 24-byte node but marks the `int_field` as 1 to indicate undefine.

### Language Standard Selection (Cases 228, 240--252)

These cases set `dword_126EF68` -- the internal value of `__cplusplus` or `__STDC_VERSION__`:

| Case(s) | Flag | `dword_126EF68` | Standard |
|---|---|---|---|
| 228 | `c++98` | 199711 | C++98/03 |
| 204 | `c++11` | 201103 | C++11 |
| 240 | `c++14` | 201402 | C++14 |
| 246 | `c++17` | 201703 | C++17 |
| 251 | `c++20` | 202002 | C++20 |
| 252 | `c++23` | 202302 | C++23 |
| 178 | `c99` | 199901 | C99 (calls `set_c_mode`) |
| 179 | `pre-c99` | 199000 | Pre-C99 |
| 241 | `c11` | 201112 | C11 |
| 242 | `c17` | 201710 | C17 |
| 243 | `c23` | 202311 | C23 |
| 7 | `old_c` | *(K&R)* | K&R C via `sub_44C4F0(1)` |

### SM Architecture Target (Case 245)

```c
case 245:  // --target=<sm_arch>
    dword_126E4A8 = sub_7525E0(value_string);
```

`sub_7525E0` parses the SM architecture string (e.g., `"sm_90"`, `"sm_100"`) and returns the internal architecture code stored in `dword_126E4A8`. This value gates which CUDA features are available during compilation (see [Architecture Feature Gating](../cuda/arch-gating.md)).

### Host Compiler Compatibility (Cases 182--188)

| Case | Flag | Globals | Behavior |
|---|---|---|---|
| 182 | `gcc / no_gcc` | `dword_126EFA8`, `dword_126EFB0` | Enable/disable GCC compatibility mode + GNU extensions |
| 184 | `gnu_version` | `qword_126EF98` | GCC version number (default: 80100 = GCC 8.1.0). Parsed as integer. |
| 187 | `clang / no_clang` | `dword_126EFA4` | Enable/disable Clang compatibility mode |
| 188 | `clang_version` | `qword_126EF90` | Clang version number (default: 90100 = Clang 9.1.0) |
| 95 | `pgc++` | boolean | PGI C++ compiler mode |
| 96 | `icc` | boolean | Intel ICC mode |
| 97 | `icc_version` | *(has_arg)* | Intel ICC version number |
| 98 | `icx` | boolean | Intel ICX (oneAPI DPC++) mode |

### Raw Flag Manipulation (Case 193)

```c
case 193:  // --set_flag=<name>=<value>  or  --clear_flag=<name>
    // Looks up <name> in off_D47CE0 (a name-to-address lookup table)
    // Sets the corresponding global to <value> (integer)
```

This is a backdoor for nvcc to set arbitrary internal globals by name, used for flags that do not have dedicated `case_id` entries.

### Output Mode (Case 274)

```c
case 274:  // --output_mode=text  or  --output_mode=sarif
    if (strcmp(value, "text") == 0)
        output_mode = 0;     // plain text diagnostics (default)
    else if (strcmp(value, "sarif") == 0)
        output_mode = 1;     // SARIF JSON diagnostics
```

SARIF (Static Analysis Results Interchange Format) output is used by IDE integrations and CI pipelines. When enabled, diagnostic messages are emitted as structured JSON instead of traditional `file:line: error:` format.

### Dump Options (Case 273)

```c
case 273:  // --dump_command_options
    // Iterates the entire flag table
    // For each entry where is_valid == 1:
    //   printf("--%s ", name);
    // Then exits
```

This is a diagnostic/debug mode that prints every registered flag name and exits. Used by nvcc to discover the cudafe++ flag namespace.

## Post-Parsing: Dialect Resolution

After the argv loop exits, `proc_command_line` enters a massive dialect resolution block (approximately 800 lines). This phase reconciles the various mode flags into a consistent configuration.

### Input Filename Extraction

The last non-flag argv element is the input filename, stored in `qword_126EEE0`. This pointer is later passed to `process_translation_unit` (`sub_7A40A0`) in stage 5 of the pipeline.

### Memory Region Initialization

Eleven memory regions (numbered 1--11) are initialized with default configurations. These correspond to CUDA memory spaces (global, shared, constant, local, texture, etc.) and are used by the frontend to track address space qualifiers.

### GCC/Clang Feature Resolution

The resolver checks GCC version thresholds to decide which extensions to enable:

```text
GCC version thresholds (stored as integer * 100):
  40299 (0x9D6B)  -- GCC 4.2.99 boundary
  40599 (0x9E97)  -- GCC 4.5.99 boundary
  40699 (0x9EFB)  -- GCC 4.6.99 boundary
  etc.
```

For each threshold, specific feature flags are conditionally enabled. For example, if GCC version >= 40599, rvalue references and variadic templates are enabled even if the language standard is technically C++03. This emulates how GCC provides extensions ahead of standards.

### C++ Standard Feature Cascade

Based on the value of `dword_126EF68` (`__cplusplus`), the resolver enables feature flags in a cascade:

```text
199711 (C++98):  base features only
201103 (C++11):  + lambdas, rvalue_refs, auto_type, nullptr,
                   variadic_templates, unrestricted_unions,
                   delegating_constructors, user_defined_literals, ...
201402 (C++14):  + digit_separators, generic lambdas, relaxed_constexpr
201703 (C++17):  + exc_spec_in_func_type, aligned_new, if_constexpr,
                   structured_bindings, fold_expressions, ...
202002 (C++20):  + concepts, modules, coroutines, consteval, ...
202302 (C++23):  + deducing_this, multidimensional_subscript, ...
```

### Conflict Validation

Post-dialect resolution performs consistency checks:
- If both `gcc` and `clang` modes are enabled, GCC takes precedence
- If `cfront_2.1` or `cfront_3.0` is set alongside modern C++ features, features are silently disabled
- If `no_exceptions` is set but `coroutines` is requested, coroutines are disabled (they require exceptions)

### Output File Opening

After all flags are resolved:
- The output `.int.c` file is opened (path from case 45/`gen_c_file_name`, or stdout if path is `"-"`)
- The error output file is opened if `--error_output` was specified (case 35)
- The listing file is opened if `--list` was specified (case 33)

## Default Diagnostic Severity Overrides

Nine diagnostic numbers are suppressed by default before any user `--diag_suppress` flags are processed:

| Diagnostic | EDG meaning | Why suppressed |
|---|---|---|
| 1257 | *(C++11 narrowing conversion in aggregate init)* | Common in CUDA kernel argument forwarding |
| 1373 | *(nonstandard extension used: zero-sized array in struct)* | Used in CUDA runtime headers |
| 1374 | *(nonstandard extension used: struct with no members)* | Empty base optimization patterns |
| 1375 | *(nonstandard extension used: unnamed struct/union)* | Windows SDK compatibility |
| 1633 | *(inline function linkage conflict)* | Host/device function linkage edge cases |
| 2330 | *(implicit narrowing conversion)* | Template-heavy CUDA code triggers false positives |
| 111 | statement is unreachable | `__builtin_unreachable()` and device code control flow |
| 185 | pointless comparison of unsigned integer with zero | Generic template code comparing unsigned with zero |
| 175 | subscript out of range | Static analysis false positives in device intrinsics |

Users can override these defaults with explicit `--diag_error=111` (or similar) on the command line, since user-specified severity always wins.

## Key Helper Functions

| Function | Address | Lines | Identity | Role |
|---|---|---|---|---|
| `sub_451E80` | `0x451E80` | 15 | `check_conflicting_flags` | Validates mutually exclusive flags (3/193/194/195) |
| `sub_451EC0` | `0x451EC0` | 57 | `parse_flag_name_value` | Splits `--name=value` on `=`, respecting quotes and backslash escapes |
| `sub_451F80` | `0x451F80` | 25 | `register_command_flag` | Inserts one entry into the flag table |
| `sub_452010` | `0x452010` | 3,849 | `init_command_line_flags` | Registers all 276 flags (called once from `proc_command_line`) |
| `sub_4595D0` | `0x4595D0` | 21 | `append_to_linked_list` | Allocates 24-byte node, appends to `-D`/`-I` argument lists |
| `sub_45EB40` | `0x45EB40` | 470 | `default_init` | Zeros 350 global config variables + flag-was-set bitmap |
| `sub_44C4F0` | `0x44C4F0` | -- | `set_c_mode` | Sets language mode: 0=C, 1=K&R, 2=C++ |
| `sub_44C460` | `0x44C460` | -- | `parse_integer_arg` | Parses string argument as integer (used by `error_limit`, etc.) |
| `sub_4ED400` | `0x4ED400` | -- | `set_diagnostic_severity` | Sets severity for a single diagnostic number |

## Key Global Variables

| Variable | Address | Type | Set by | Description |
|---|---|---|---|---|
| `dword_E80058` | `0xE80058` | int32 | `register_command_flag` | Current flag table entry count (max 552) |
| `dword_E80060` | `0xE80060` | array | `register_command_flag` | Flag table base (40 bytes/entry) |
| `byte_E7FF40` | `0xE7FF40` | byte[272] | Parsing loop | Flag-was-set bitmap |
| `dword_E7FF20` | `0xE7FF20` | int32 | `default_init` | Current argv index (initialized to 1) |
| `qword_126EEE0` | `0x126EEE0` | char* | Post-parse | Input source filename |
| `dword_106C254` | `0x106C254` | int32 | Case 14 | Skip-backend flag (`--no_code_gen`) |
| `dword_106C0A4` | `0x106C0A4` | int32 | Case 20 | Timing enabled (`--timing`) |
| `dword_126EF68` | `0x126EF68` | int32 | Standard flags | `__cplusplus` / `__STDC_VERSION__` value |
| `dword_126EFB4` | `0x126EFB4` | int32 | Mode flags | Language mode (0=unset, 1=C, 2=C++) |
| `dword_126EFA8` | `0x126EFA8` | int32 | Case 182 | GCC compatibility mode enabled |
| `dword_126EFA4` | `0x126EFA4` | int32 | Case 187 | Clang compatibility mode enabled |
| `qword_126EF98` | `0x126EF98` | int64 | Case 184 | GCC version (default 80100 = 8.1.0) |
| `qword_126EF90` | `0x126EF90` | int64 | Case 188 | Clang version (default 90100 = 9.1.0) |
| `dword_126EFB0` | `0x126EFB0` | int32 | Case 182 | GNU extensions enabled |
| `dword_106C064` | `0x106C064` | int32 | Case 55 | Modify stack limit (default 1) |
| `dword_126E4A8` | `0x126E4A8` | int32 | Case 245 | Target SM architecture code |
| `dword_106C094` | `0x106C094` | int32 | Case 16 | Template instantiation mode (0--3) |
| `byte_126ED69` | `0x126ED69` | int8 | Cases 22/24 | Diagnostic severity threshold |
| `byte_126ED68` | `0x126ED68` | int8 | Case 23 | Warning promotion threshold |
| `qword_106BF20` | `0x106BF20` | char* | Case 45 | Output `.int.c` file path |
| `qword_106C248` | `0x106C248` | void* | Pre-loop | Macro define/alias hash table |
| `qword_106C240` | `0x106C240` | void* | Pre-loop | Include path hash table |
| `qword_106C238` | `0x106C238` | void* | Pre-loop | System include map hash table |

## Annotated Parsing Flow

```c
int64_t proc_command_line(int argc, char** argv)
{
    // --- Phase 1: Global state init ---
    qword_126DD38 = 0;                         // zero token state
    qword_126EDE8 = 0;                         // zero source position

    // --- Phase 2: Register all flags ---
    init_command_line_flags();                  // sub_452010: 3849 lines, 276 flags

    // --- Phase 3: Allocate hash tables ---
    qword_106C248 = alloc_hash_table();        // macro defines/aliases
    qword_106C240 = alloc_hash_table();        // include paths
    qword_106C238 = alloc_hash_table();        // system includes
    qword_106C228 = alloc_hash_table();        // additional system includes

    // --- Phase 4: Default diagnostic suppressions ---
    set_diagnostic_severity(1257, SUPPRESS, 1);
    set_diagnostic_severity(1373, SUPPRESS, 1);
    set_diagnostic_severity(1374, SUPPRESS, 1);
    set_diagnostic_severity(1375, SUPPRESS, 1);
    set_diagnostic_severity(1633, SUPPRESS, 1);
    set_diagnostic_severity(2330, SUPPRESS, 1);
    set_diagnostic_severity(111,  SUPPRESS, 1);
    set_diagnostic_severity(185,  SUPPRESS, 1);
    set_diagnostic_severity(175,  SUPPRESS, 1);

    // --- Phase 5: Main parsing loop ---
    for (int i = 1; i < argc; i++) {
        char* arg = argv[i];
        if (arg[0] != '-') {
            qword_126EEE0 = arg;               // input filename
            continue;
        }

        // Split --name=value
        char *name, *value;
        parse_flag_name_value(arg + 2, &name, &value);  // sub_451EC0

        // Match against flag table
        int match_count = 0;
        int matched_id = -1;
        for (int f = 0; f < dword_E80058; f++) {
            if (strncmp(name, flag_table[f].name, strlen(name)) == 0) {
                if (strlen(name) == flag_table[f].name_length) {
                    matched_id = flag_table[f].case_id;  // exact match
                    break;
                }
                match_count++;
                matched_id = flag_table[f].case_id;
            }
        }

        if (match_count > 1) {
            error(923);  // "ambiguous command-line option"
            continue;
        }

        byte_E7FF40[matched_id] = 1;           // mark flag as set

        switch (matched_id) {
            case 3:   /* no_line_commands */ ...
            case 4:   /* preprocess */      ...
            ...
            case 274: /* output_mode */     ...
            case 275: /* incognito */       ...
        }
    }

    // --- Phase 6: Post-parsing dialect resolution ---
    // ~800 lines: resolve gcc/clang versions, cascade C++ features,
    // validate consistency, open output files

    // --- Phase 7: Memory region init (1-11) ---
    // Initialize CUDA memory space descriptors

    return 0;
}
```

## Version Banner

Case 21 (`--version` / `-v`) prints the following banner to stdout (does not exit):

```text
cudafe: NVIDIA (R) Cuda Language Front End
Portions Copyright (c) 2005, 2024-YYYY NVIDIA Corporation
Portions Copyright (c) 1988-2018, 2024 Edison Design Group Inc.
Based on Edison Design Group C/C++ Front End, version 6.6 (BUILD_DATE BUILD_TIME)
Cuda compilation tools, release 13.0, V13.0.88
```

Case 92 (`--Version` / `-V`) prints a different copyright format and then calls `exit(1)`. This variant is used for machine-parseable version queries.

## Relationship to Pipeline

`proc_command_line` is called as stage 2 of the pipeline, after `fe_pre_init` (`sub_585D60`) has initialized signal handlers, locale, working directory, and default config:

```text
main()
  |-- sub_585D60()           [1] fe_pre_init (10 subsystem pre-initializers)
  |-- sub_5AF350()               capture_time (total start)
  |-- sub_459630(argc, argv) [2] proc_command_line  <-- THIS FUNCTION
  |-- setrlimit()                conditional stack raise (gated by dword_106C064)
  |-- sub_585DB0()           [3] fe_one_time_init (38 subsystem initializers)
  ...
```

By the time `proc_command_line` returns, every global configuration variable is set to its final value. The subsequent `fe_one_time_init` phase reads these globals to configure keyword tables, type system parameters, and per-translation-unit state.
