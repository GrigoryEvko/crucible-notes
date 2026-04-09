# CLI Option Parsing

This page documents the command-line option parsing infrastructure in nvlink v13.0.88: the parser framework, option entry struct layout, registration sequence, post-extraction validation, mutual-exclusion enforcement, and global variable mappings. For the complete alphabetically-sorted quick-reference table of all flags, see [CLI Flags Reference](../config/cli-flags.md).

## Parser Infrastructure

Option parsing is a self-contained subsystem at addresses `0x42C510`--`0x42F640`. The parser is a generic, reusable framework (shared with cicc and ptxas) that handles option registration, argv scanning, value extraction, help formatting, and validation. nvlink creates one parser instance at the start of `nvlink_parse_options` (`0x427AE0`) and destroys it implicitly when the function returns.

### Core Functions

| Address | Name (recovered) | Size | Role |
|---|---|---|---|
| `0x42DFE0` | `option_parser_create` | 4539 B | Allocates 56-byte parser struct, creates two hash tables (by long name, by short name), registers default entries |
| `0x42F130` | `option_register` | 4936 B | Registers a single option: allocates 120-byte entry, populates fields, inserts into both hash tables |
| `0x42E5A0` | `option_parse_argv` | 9518 B | Parses argc/argv against registered options, handles `--`/`-` prefix, `=` assignment, response files (`@file`), unknown-option collection |
| `0x42E390` | `option_get_value` | 2910 B | Extracts a parsed option value into a caller-supplied variable (1/4/8 byte copy depending on type) |
| `0x42E580` | `option_was_specified` | 163 B | Returns boolean: was this option present on the command line? Used for presence-detection vs. value extraction |
| `0x42D700` | `option_format_help` | 5589 B | Formats a single option's help entry: `"--%s%s%s%s"`, `"(-%s)"`, appends default/allowed values |
| `0x42DBC0` | `option_validate_value` | 5065 B | Validates parsed value against type constraints: `"32-bit integer"`, `"64-bit integer"`, `"32-bit hex"`, `"64-bit hex"` |
| `0x42F560` | `option_print_help` | 1116 B | Iterates all registered options and calls `option_format_help` for each |
| `0x42F640` | `option_generate_tkinfo` | 430 B | Serializes parsed option state into tkinfo section data |
| `0x42C510` | `option_hash_lookup` | 4190 B | Hash table operations for O(1) option lookup by name |

### Parser Object Layout (56 bytes)

The parser object (`sub_42DFE0` return value) is allocated from the `"nvlink option parser"` memory arena:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Pointer to first option entry (linked list head) |
| 8 | 8 | Pointer to last option entry (linked list tail) |
| 16 | 8 | Hash table pointer: long-name lookup |
| 24 | 8 | Hash table pointer: short-name lookup |
| 32 | 8 | Pointer to default file-list option entry (name = `" "`) |
| 40 | 8 | Arena pointer (for allocations) |
| 48 | 8 | Pointer to `"__internal_unknown_opt"` entry |

### Option Entry Layout (120 bytes)

Each option registered by `option_register` occupies 120 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Long name string pointer (`"output-file"`, `"debug"`, etc.) |
| 8 | 8 | Short name string pointer (`"o"`, `"g"`, etc.), or NULL |
| 16 | 4 | Type code: 0 = file-list, 1 = bool, 2 = string, 4 = integer |
| 20 | 4 | Multiplicity: 0 = none (bool), 1 = single value, 2 = multi-value (accumulates) |
| 24 | 4 | Flags (bitmask, see below) |
| 28 | 4 | (padding) |
| 32 | 8 | Allowed keywords string (`"unknown,X86,X86_64,ARMv7,AARCH64,PPC64LE"`) |
| 40 | 8 | (reserved) |
| 48 | 8 | Default value string (`"64"`, `"0"`, `"false"`, `"unknown"`) |
| 56 | 8 | Default keyword string |
| 64 | 8 | Value placeholder for help (`"<file name>"`, `"<N>"`, `"<gpu architecture name>"`) |
| 72 | 8 | Help text string pointer |
| 80 | 8 | Parsed value storage (for single-value options) |
| 88 | 8 | Parsed value list head (for multi-value options) |
| 96 | 8 | Link: next option in parser's linked list |
| 104 | 8 | Link: hash chain for long-name table |
| 112 | 8 | Link: hash chain for short-name table |

### Type Codes

| Code | Meaning | Value extraction | Storage |
|---|---|---|---|
| 0 | File list (positional args) | `option_get_value(..., 8)` | `qword` pointer to linked list |
| 1 | Boolean flag | `option_get_value(..., 1)` | `byte` (0 or 1) |
| 2 | String | `option_get_value(..., 8)` | `qword` pointer to string |
| 4 | Integer | `option_get_value(..., 4)` | `dword` |

### Flag Bits

The flags field at offset 24 controls parser behavior:

| Bit | Value | Meaning |
|---|---|---|
| 2 | `0x04` | Internal/hidden option (not shown in `--help`) |
| 3 | `0x08` | Undocumented/internal (stronger hiding) |
| 4 | `0x10` | Accepts negative form (`--no-<name>`) |

### Registration and Extraction Flow

The `nvlink_parse_options` function (`0x427AE0`, 30272 bytes, 1299 lines) follows a strict sequence:

```
1. parser = option_parser_create(0)                    // 0x42DFE0
2. For each of ~65 options:
     option_register(parser, long, short, type,        // 0x42F130
                     mult, flags, keywords, reserved,
                     default, default_kw, placeholder, help)
3. option_parse_argv(parser, argc, argv)               // 0x42E5A0
4. Handle immediate-exit options:
     if option_was_specified("trap-into-debugger"):     // 0x42E580
         install_trap_handler()                         // 0x42FA60
     if option_was_specified("help"):
         print_help_and_exit()                          // 0x42F560 + 0x44A420
     if option_was_specified("version"):
         print_version_and_exit()                       // 0x44A420
5. For each option:
     option_get_value(parser, name, &global_var, size)  // 0x42E390
6. Post-extraction validation:
     - Architecture range checks (sm > 19)
     - Mercury mode detection (sm > 99)
     - LTO consistency checks
     - Mutual-exclusion enforcement
     - CUDA API version parsing
7. tkinfo = option_generate_tkinfo(parser, ...)         // 0x42F640
```

## Option Catalog

For the complete alphabetically-sorted flag table (all 68 entries with types, defaults, and one-line descriptions), see [CLI Flags Reference](../config/cli-flags.md).

The remainder of this page documents the implementation-level details: how each option is registered, what global variable it maps to, validation logic, and inter-option dependencies. Options are grouped by functional category below.

### Output Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `output-file` | `filename` (BSS) | type=string, mult=1, flags=0 |
| `register-link-binaries` | `qword_2A5F2E0` | type=string, mult=1, flags=0 |
| `dot-file` | `qword_2A5F2D0` | type=string, mult=1, flags=4 |
| `gen-host-linker-script` | `qword_2A5F1D0` | type=string, mult=1, flags=0 |
| `relocatable-link` | `byte_2A5F1E8` | type=bool, mult=0, flags=0 |
| `shared` | `byte_2A5F1D8` | type=bool, mult=0, flags=4 |

The `gen-host-linker-script` option accepts keywords `lcs-aug` and `lcs-abs` (default: `lcs-abs`). The value `lcs-aug` sets `dword_2A77DC0 = 1` (standalone SECTIONS fragment, 130 bytes, written to file or stdout), while `lcs-abs` sets it to `2` (full augmented script: `ld --verbose` pipeline + append SECTIONS + `ld -T` validation). The mapping is computed by a byte-by-byte compare against the literal `"lcs-aug"` at `sub_427AE0` lines 1012-1027 (see [Mode Dispatch](mode-dispatch.md#how-ghls-sets-the-mode)).

### Debug Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `debug` | `byte_2A5F310` | type=bool, mult=0, flags=0 |
| `suppress-debug-info` | `byte_2A5F226` | type=bool, mult=0, flags=0 |
| `edbg` | `dword_2A5F308` | type=int, mult=1, flags=8 |

When `--suppress-debug-info` is specified with `--debug`, the effect is to clear `byte_2A5F310` (debug flag) to 0, effectively disabling debug output. If specified without `--debug`, a warning is emitted: `"-suppress-debug-info"` conflicts with `"no -g"`.

### Architecture Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `arch` | `qword_2A5F318` | type=string, mult=1, flags=0 |
| `machine` | `dword_2A5F30C` | type=int, mult=1, flags=16 |
| `cpu-arch` | `qword_2A5F2A0` | type=string, mult=1, flags=0 |
| `report-arch` | `byte_2A5F29C` | type=bool, mult=0, flags=0 |

The `arch` option is registered with an allowed-keywords callback (`sub_486EC0(1)`) that validates the architecture name against the supported SM table. The `machine` option defaults to `"64"` and only accepts value 64; specifying 32 on SM > 72 produces a fatal error.

The `cpu-arch` option accepts keywords: `unknown`, `X86`, `X86_64`, `ARMv7`, `AARCH64`, `PPC64LE` (default: `unknown`).

**Architecture validation logic** (post-extraction):

```
sm = parse_sm_number(arch_string)         // sub_44E3E0
if sm <= 19:
    fatal_error("unsupported arch")
byte_2A5F224 = (sm > 72)                  // "new-style" ELF flag
if sm > 72 && machine == 32:
    fatal_error("sm > 72 requires 64-bit")
    byte_2A5F224 = 0
if sm > 99:                               // Mercury (Blackwell+)
    byte_2A5F222 = 1                      // mercury mode
    byte_2A5F225 = 1                      // SASS output mode
    byte_2A5B510 = 0
elif sm > 89:                             // sm_90+ needs SASS mode
    if sm <= 89:
        fatal_error("SASS mode requires sm >= 90")
```

### Library Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `library` | `qword_2A5F2F8` | type=string, mult=2, flags=16 |
| `library-path` | `qword_2A5F300` | type=string, mult=2, flags=16 |
| `host-linker-options` | `qword_2A5F2E8` | type=string, mult=2, flags=4 |
| `keep-system-libraries` | `byte_2A5F2C2` | type=bool, mult=0, flags=0 |
| `host-ccbin` | `::src` (BSS) | type=string, mult=1, flags=4 |

Both `library` and `library-path` have multiplicity 2 (multi-value), so repeated `-l` / `-L` flags accumulate into linked lists. `host-linker-options` is accepted and stored but ignored by the device linker.

### Linking Behavior Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `preserve-relocs` | `byte_2A5F2CE` | type=bool, mult=0, flags=0 |
| `reserve-null-pointer` | *(local, then byte_2A5F2CD)* | type=bool, mult=0, flags=4 |
| `dont-reserve-null-pointer` | *(local)* | type=bool, mult=0, flags=4 |
| `allow-undefined-globals` | `byte_2A5F2CC` | type=bool, mult=0, flags=4 |
| `disable-smem-reservation` | `byte_2A5F210` | type=bool, mult=1, flags=4 |
| `syscall-const-offset` | `dword_2A5F2C8` | type=int, mult=1, flags=4 |
| `force-rela` | `byte_2A5F2AA` | type=bool, mult=0, flags=4 |
| `no-opt` | `byte_2A5F2A9` | type=bool, mult=0, flags=8 |
| `optimize-data-layout` | `byte_2A5F2A8` | type=bool, mult=0, flags=8 |
| `enable-extended-smem` | `byte_2A5F1FD` | type=bool, mult=1, flags=4 |

The null-pointer reservation logic computes `byte_2A5F2CD = reserve-null-pointer AND NOT dont-reserve-null-pointer`. The `dont-reserve-null-pointer` flag always wins if both are specified.

The `disable-smem-reservation` and `enable-extended-smem` options accept `true`/`false` as values (boolean with multiplicity 1), with default `"false"`.

`no-opt` and `optimize-data-layout` are mutually exclusive; specifying both produces a fatal error.

### Dead Code Elimination Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `kernels-used` | `qword_2A5F2B8` | type=string, mult=2, flags=0 |
| `variables-used` | `qword_2A5F2B0` | type=string, mult=2, flags=0 |
| `use-host-info` | `byte_2A5F213` | type=bool, mult=0, flags=0 |
| `ignore-host-info` | `byte_2A5F212` | type=bool, mult=0, flags=0 |

`use-host-info` and `ignore-host-info` are mutually exclusive. If neither is specified, `use-host-info` defaults to true (`byte_2A5F213 = 1`, `byte_2A5F214 = 1`). If `--relocatable-link` is specified, `ignore-host-info` is forced on. If `--kernels-used` or `--variables-used` is specified, `use-host-info` is forced off with a warning.

### LTO Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `link-time-opt` | `byte_2A5F288` | type=bool, mult=0, flags=0 |
| `dlto` | `byte_2A5F287` | type=bool, mult=0, flags=0 |
| `force-partial-lto` | `byte_2A5F285` | type=bool, mult=0, flags=4 |
| `force-whole-lto` | `byte_2A5F284` | type=bool, mult=0, flags=4 |
| `nvvmpath` | `qword_2A5F278` | type=string, mult=1, flags=0 |
| `emit-ptx` | `byte_2A5F29A` | type=bool, mult=0, flags=0 |
| `split-compile` | `dword_2A5B518` | type=int, mult=1, flags=0 |
| `split-compile-extended` | `dword_2A5B514` | type=int, mult=1, flags=0 |

When `--dlto` is specified, `byte_2A5F288` (the LTO master flag) is set to 1. The LTO options have extensive mutual-exclusion and dependency validation:

- `--nvvmpath` is required with `-lto`; omitting it produces: `"-nvvmpath should be specified with -lto"`
- `--force-partial-lto` and `--force-whole-lto` are mutually exclusive
- `--emit-ptx` requires `-lto`
- `--Ofast-compile` requires `-lto`
- `--force-partial-lto` without `-dlto` is an error
- `--force-whole-lto` without `-dlto` is an error
- `--relocatable-link` forces partial LTO mode

Both `split-compile` and `split-compile-extended` default to `1` (single-threaded). When `--emit-ptx` is specified with multi-threaded split-compile, the threads are forced to 1 with a warning.

### LTO Forwarding Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `Xptxas` | `qword_2A5F238` | type=string, mult=2, flags=0 |
| `Xnvvm` | `qword_2A5F230` | type=string, mult=2, flags=0 |
| `maxrregcount` | `dword_2A5F22C` | type=int, mult=1, flags=0 |
| `Ofast-compile` | `qword_2A5F258` | type=string, mult=1, flags=0 |

The `Ofast-compile` option accepts: `"0"`, `"min"`, `"mid"`, `"max"` (default: `"0"`). Any other value produces a fatal error referencing `"--Ofast-compile"`. The values control optimization aggressiveness during LTO compilation:

- `"max"`: Focus only on the fastest compilation speed, disabling many optimizations
- `"mid"`: Balance compile time and runtime, disabling expensive optimizations
- `"min"`: More minimal impact on both compile time and runtime
- `"0"`: Disables fast-compile (full optimization)

### Warning and Diagnostic Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `disable-warnings` | `byte_2A5F2C4` | type=bool, mult=0, flags=0 |
| `warning-as-error` | `byte_2A5F2C3` | type=bool, mult=0, flags=0 |
| `disable-infos` | `byte_2A5F2C5` | type=bool, mult=0, flags=0 |
| `suppress-stack-size-warning` | `byte_2A5F299` | type=bool, mult=0, flags=0 |
| `suppress-arch-warning` | `byte_2A5F298` | type=bool, mult=0, flags=0 |
| `extra-warnings` | `byte_2A5F289` | type=bool, mult=0, flags=0 |

After extraction, `warning-as-error` and `disable-warnings`/`disable-infos` are applied immediately:

```c
sub_468420(byte_2A5F2C3);   // configure warning-as-error in diagnostics subsystem
sub_468430(byte_2A5F2C4);   // configure disable-warnings
sub_468430(byte_2A5F2C5);   // configure disable-infos
```

### Verbose and Debug Output Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `verbose` | `byte_2A5F2D8` | type=bool, mult=0, flags=0 |
| `verbose-keep` | `byte_2A5F29B` | type=bool, mult=0, flags=8 |
| `verbose-tkinfo` | `byte_2A5F223` | type=bool, mult=1, flags=4 |
| `dump-callgraph` | `byte_2A5F216` | type=bool, mult=0, flags=0 |
| `dump-callgraph-no-demangle` | `byte_2A5F215` | type=bool, mult=0, flags=0 |

`dump-callgraph` and `dump-callgraph-no-demangle` are mutually exclusive.

When `--debug` is specified and `--verbose-tkinfo` was not explicitly given, `byte_2A5F223` is forced to 1 (tkinfo is always verbose in debug builds).

### CUDA API and Compatibility Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `cuda-api-version` | `qword_2A5F218` | type=string, mult=1, flags=4 |
| `nv-host` | `qword_2A5F1F0` | type=string, mult=1, flags=4 |
| `uidx-file` | `qword_2A5F208` | type=string, mult=1, flags=0 |
| `tool-name` | *(extracted, then updates path)* | type=string, mult=1, flags=4 |

The `cuda-api-version` value is parsed with `sscanf("%u.%u")`. The major version must equal the toolkit version (`dword_2A5B50C`); the minor version is clamped to the minimum of the specified value and the built-in default (`dword_2A5B508`).

### Security Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `device-stack-protector` | `byte_2A5F1FE` | type=bool, mult=1, flags=0 |
| `device-stack-protector-frame-size-threshold` | `dword_2A5F1F8` | type=int, mult=1, flags=4 |

Both options have their "was specified" flags stored separately (`byte_2A5F1FF` for `device-stack-protector`, `byte_2A5F1FC` for `device-stack-protector-frame-size-threshold`), retrieved via `option_was_specified`.

### Internal / Undocumented Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `fdcmpt` | `byte_2A5F228` | type=bool, mult=0, flags=4 |
| `uumn` | `byte_2A5F227` | type=bool, mult=0, flags=4 |
| `time` | `qword_2A5F290` | type=string, mult=1, flags=0 |

`fdcmpt` is a forward-compatibility flag. It is validated post-extraction: if `fdcmpt` is set but `uumn` is not set, a warning is emitted (`"-fdcmpt"`). If both are set but SM <= 69, a fatal error is produced. The flag controls data-model compatibility across architectures (exact semantics not fully determined from decompilation).

`uumn` is a companion to `fdcmpt` and has no help text.

The `time` option enables CSV timing output (used by NVIDIA's build infrastructure for performance tracking). When the file argument is `"-"`, timing data goes to stdout.

### Meta Options

**Global variable mappings:**

| Option | Global Variable | Registration |
|---|---|---|
| `help` | *(immediate exit)* | type=bool, mult=0, flags=0 |
| `version` | *(immediate exit)* | type=bool, mult=0, flags=0 |
| `options-file` | *(processed by parser)* | type=file-list, mult=2, flags=0 |
| `trap-into-debugger` | *(immediate action)* | type=bool, mult=0, flags=8 |

`--help` output format:
```
Usage  : nvlink [options] <objects>
```
followed by formatted help for all non-hidden options.

`--version` output format:
```
nvlink: NVIDIA (R) Cuda linker
Copyright (c) 2005-2025 NVIDIA Corporation
Built on Wed_Aug_20_01:58:59_PM_PDT_2025
Cuda compilation tools, release 13.0, V13.0.88
Build cuda_13.0.r13.0/compiler.36424714_0
```

`--options-file` (type 0 = file-list) is handled by the parser itself during `option_parse_argv`: response files are expanded in-place before option matching.

`--trap-into-debugger` installs signal handlers (via `sub_42FA60`) that trap into a debugger on assertion failure, then continues normal option parsing.

## Post-Extraction Validation

After extracting all option values into global variables, `nvlink_parse_options` performs extensive cross-validation. The mutual-exclusion conflicts all use `sub_467460` (the error/warning/fatal dispatcher) with format string `unk_2A5B650` for fatal conflicts:

| Option A | Option B | Result |
|---|---|---|
| `--no-opt` | `--optimize-data-layout` | Fatal error |
| `--dump-callgraph` | `--dump-callgraph-no-demangle` | Fatal error |
| `--use-host-info` | `--ignore-host-info` | Fatal error |
| `--force-partial-lto` | `--force-whole-lto` | Fatal error |
| `--suppress-debug-info` | *(no `--debug`)* | Fatal error |
| `--force-partial-lto` | *(no `--dlto`)* | Fatal error |
| `--force-whole-lto` | *(no `--dlto`)* | Fatal error |
| `--emit-ptx` | *(no `--dlto`)* | Fatal error |
| `--Ofast-compile` | *(no `--dlto`)* | Fatal error |
| `--preserve-relocs` | *(SM > 89)* | Warning: not supported |
| `-m32` | *(SM >= 90)* | Fatal error |

### Compilation Mode Determination

The extracted options determine the overall compilation mode stored in `dword_2A5B528`:

| Mode | Value | Condition |
|---|---|---|
| Normal | 0 | Default |
| Passthrough | 2 | `byte_2A5F2C1` (output-is-archive flag) |
| LTO | 4 | `byte_2A5F288` (link-time-opt) |
| SASS | 6 | `byte_2A5F225` (SASS output mode, SM > 89 or mercury) |

## Global Variable Map

Complete mapping from option name to BSS global variable address, sorted by address:

| Address | Size | Option | Description |
|---|---|---|---|
| `byte_2A5F1FC` | 1 | *(was-specified)* | device-stack-protector-frame-size-threshold was specified |
| `byte_2A5F1FD` | 1 | `enable-extended-smem` | Extended shared memory flag |
| `byte_2A5F1FE` | 1 | `device-stack-protector` | Stack protector enable |
| `byte_2A5F1FF` | 1 | *(was-specified)* | device-stack-protector was specified |
| `dword_2A5F1F8` | 4 | `device-stack-protector-frame-size-threshold` | Frame size threshold |
| `qword_2A5F1F0` | 8 | `nv-host` | Path to nv.host file |
| `byte_2A5F1E8` | 1 | `relocatable-link` | Relocatable/incremental link |
| `byte_2A5F1D8` | 1 | `shared` | Shared library flag |
| `qword_2A5F1D0` | 8 | `gen-host-linker-script` | Host linker script type |
| `qword_2A5F200` | 8 | *(derived)* | tkinfo data pointer |
| `qword_2A5F208` | 8 | `uidx-file` | Path to uidx file |
| `byte_2A5F210` | 1 | `disable-smem-reservation` | Shared memory reservation disable |
| `byte_2A5F212` | 1 | `ignore-host-info` | Ignore host info |
| `byte_2A5F213` | 1 | `use-host-info` | Use host info |
| `byte_2A5F214` | 1 | *(derived)* | Host info active (either explicit or default) |
| `byte_2A5F215` | 1 | `dump-callgraph-no-demangle` | Dump callgraph without demangling |
| `byte_2A5F216` | 1 | `dump-callgraph` | Dump callgraph |
| `qword_2A5F218` | 8 | `cuda-api-version` | API version string |
| `byte_2A5F222` | 1 | *(derived)* | Mercury mode (sm > 99) |
| `byte_2A5F223` | 1 | `verbose-tkinfo` | Verbose tkinfo generation |
| `byte_2A5F224` | 1 | *(derived)* | New-style ELF (sm > 72) |
| `byte_2A5F225` | 1 | *(derived)* | SASS output mode |
| `byte_2A5F226` | 1 | `suppress-debug-info` | Suppress debug symbols |
| `byte_2A5F227` | 1 | `uumn` | Undocumented compatibility flag |
| `byte_2A5F228` | 1 | `fdcmpt` | Forward-compatibility flag |
| `dword_2A5F22C` | 4 | `maxrregcount` | Max register count (LTO) |
| `qword_2A5F230` | 8 | `Xnvvm` | NVVM option list |
| `qword_2A5F238` | 8 | `Xptxas` | ptxas option list |
| `qword_2A5F258` | 8 | `Ofast-compile` | Fast-compile level string |
| `qword_2A5F278` | 8 | `nvvmpath` | Path to libnvvm |
| `byte_2A5F284` | 1 | `force-whole-lto` | Force whole-program LTO |
| `byte_2A5F285` | 1 | `force-partial-lto` | Force partial LTO |
| `byte_2A5F286` | 1 | *(derived)* | Partial-LTO active |
| `byte_2A5F287` | 1 | `dlto` | DLTO alias flag |
| `byte_2A5F288` | 1 | `link-time-opt` | LTO master flag |
| `byte_2A5F289` | 1 | `extra-warnings` | Extra warnings enable |
| `byte_2A5F29A` | 1 | `emit-ptx` | Emit PTX output |
| `byte_2A5F29B` | 1 | `verbose-keep` | Keep intermediates |
| `byte_2A5F29C` | 1 | `report-arch` | Report arch in errors |
| `qword_2A5F290` | 8 | `time` | Timing CSV file path |
| `byte_2A5F298` | 1 | `suppress-arch-warning` | Suppress arch warning |
| `byte_2A5F299` | 1 | `suppress-stack-size-warning` | Suppress stack size warning |
| `qword_2A5F2A0` | 8 | `cpu-arch` | CPU architecture string |
| `byte_2A5F2A8` | 1 | `optimize-data-layout` | Force data optimization |
| `byte_2A5F2A9` | 1 | `no-opt` | Disable optimization |
| `byte_2A5F2AA` | 1 | `force-rela` | Force RELA relocations |
| `qword_2A5F2B0` | 8 | `variables-used` | Variable keep list |
| `qword_2A5F2B8` | 8 | `kernels-used` | Kernel keep list |
| `byte_2A5F2C0` | 1 | *(derived)* | Arch capability flag (sub_44E4F0) |
| `byte_2A5F2C1` | 1 | *(derived)* | Output-is-archive flag (sub_44E490) |
| `byte_2A5F2C2` | 1 | `keep-system-libraries` | Keep system libs |
| `byte_2A5F2C3` | 1 | `warning-as-error` | Werror flag |
| `byte_2A5F2C4` | 1 | `disable-warnings` | Disable warnings |
| `byte_2A5F2C5` | 1 | `disable-infos` | Disable info messages |
| `dword_2A5F2C8` | 4 | `syscall-const-offset` | Syscall constant offset |
| `byte_2A5F2CC` | 1 | `allow-undefined-globals` | Allow undefined globals |
| `byte_2A5F2CD` | 1 | *(derived)* | Reserve-null-pointer effective flag |
| `byte_2A5F2CE` | 1 | `preserve-relocs` | Preserve relocations |
| `qword_2A5F2D0` | 8 | `dot-file` | Callgraph DOT file path |
| `byte_2A5F2D8` | 1 | `verbose` | Verbose mode |
| `qword_2A5F2E0` | 8 | `register-link-binaries` | Registration output path |
| `qword_2A5F2E8` | 8 | `host-linker-options` | Xlinker option list |
| `qword_2A5F2F8` | 8 | `library` | Library list |
| `qword_2A5F300` | 8 | `library-path` | Library search paths |
| `dword_2A5F308` | 4 | `edbg` | ELF debug level |
| `byte_2A5F310` | 1 | `debug` | Debug compile flag |
| `dword_2A5F30C` | 4 | `machine` | Machine bits (64) |
| `dword_2A5F314` | 4 | *(derived)* | Parsed SM number |
| `qword_2A5F318` | 8 | `arch` | Architecture string |
| `qword_2A5F328` | 8 | *(positional)* | Raw input file list |
| `qword_2A5F330` | 8 | *(positional)* | Processed input file linked list |
| `dword_2A5B508` | 4 | *(derived)* | CUDA API minor version |
| `dword_2A5B50C` | 4 | *(derived)* | CUDA API major version |
| `dword_2A5B514` | 4 | `split-compile-extended` | Extended split-compile threads |
| `dword_2A5B518` | 4 | `split-compile` | NVVM split-compile threads |
| `dword_2A5B528` | 4 | *(derived)* | Compilation mode |
| `byte_2A5B52C` | 1 | *(derived)* | Arch-is-supported flag |
| `filename` | 8 | `output-file` | Output file path |
| `::src` | 8 | `host-ccbin` | Host compiler binary path |

## Response File Expansion

The `option_parse_argv` function (`0x42E5A0`) handles response files (also called options files) through the `--options-file` / `-optf` mechanism and the `@file` prefix. When the parser encounters an argument beginning with `@`, it reads the referenced file and expands its contents as additional command-line arguments in-place. This is recursive: a response file can reference other response files.

The `--options-file` option (type 0 = file-list, multiplicity 2) achieves the same effect through the parser's file-list processing infrastructure.

## Interaction with Main

The `nvlink_parse_options` function is called exactly once from `main` (`0x409800`):

```c
// In main():
arena = arena_create_named("nvlink option parser");     // sub_432020
// ... also creates "nvlink memory space" arena ...
nvlink_parse_options(argc, argv);                        // sub_427AE0
// All global variables are now populated.
// main() reads them directly for all subsequent pipeline decisions.
```

After `nvlink_parse_options` returns, the parser object itself is no longer referenced. All option values have been extracted into the global variables listed above, and the rest of the linker pipeline accesses those globals directly.

## Cross-References

- [CLI Flags Reference](../config/cli-flags.md) -- complete alphabetically-sorted quick-reference table of all 68 flags with types, defaults, and visibility
- [Pipeline Overview](overview.md) -- how parsed flags drive mode dispatch and the 14-phase pipeline
- [Entry Point & Main](entry.md) -- `main()` calling context for `nvlink_parse_options`
- [Mode Dispatch](mode-dispatch.md) -- how `dword_2A77DC0` (set during option parsing) selects the code path
- [ptxas Option Forwarding](../config/ptxas-options.md) -- how `--Xptxas` options are forwarded to the embedded ptxas compiler
- [LTO Option Forwarding](../lto/option-forwarding.md) -- how `--Xptxas`, `--Xnvvm`, `--maxrregcount`, and `--Ofast-compile` are forwarded to cicc/ptxas during LTO
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- how `--kernels-used`, `--variables-used`, `--use-host-info`, and `--ignore-host-info` drive DCE
- [Debug Options](../debug/options.md) -- detailed semantics of `--debug`, `--suppress-debug-info`, `--edbg`
- [Environment Variables](../config/env-vars.md) -- environment variables that supplement CLI options (e.g., `LIBRARY_PATH`)
- [Architecture Profiles](../targets/arch-profiles.md) -- how `--arch` maps to the per-architecture vtable used throughout the pipeline
- [Compatibility](../targets/compatibility.md) -- cross-architecture matching rules gated by the parsed SM number
- **cicc wiki**: [CLI Flags](../../cicc/config/cli-flags.html) -- cicc compiler CLI flags. The parser framework (option entry struct, hash table lookup, argv scanning) is shared infrastructure between nvlink, cicc, and ptxas
- **ptxas wiki**: [CLI Options](../../ptxas/config/cli-options.html) -- ptxas CLI options, using the same shared parser framework

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| Parser address range `0x42C510`--`0x42F640` | **HIGH** | All 10 function files exist in `decompiled/` with matching addresses |
| `sub_42DFE0` (`option_parser_create`), 4,539 B | **HIGH** | `stat -c%s` confirms exactly 4,539 bytes |
| `sub_42F130` (`option_register`), 4,936 B | **HIGH** | `stat -c%s` confirms exactly 4,936 bytes |
| `sub_42E5A0` (`option_parse_argv`), 9,518 B | **HIGH** | `stat -c%s` confirms exactly 9,518 bytes |
| `sub_42E390` (`option_get_value`), 2,910 B | **HIGH** | `stat -c%s` confirms exactly 2,910 bytes |
| `sub_42D700` (`option_format_help`), 5,589 B | **HIGH** | `stat -c%s` confirms exactly 5,589 bytes |
| `sub_42DBC0` (`option_validate_value`), 5,065 B | **HIGH** | `stat -c%s` confirms exactly 5,065 bytes |
| `sub_42C510` (`option_hash_lookup`), 4,190 B | **HIGH** | `stat -c%s` confirms exactly 4,190 bytes |
| `sub_42E580` (`option_was_specified`), 163 B | **HIGH** | `stat -c%s` confirms exactly 163 bytes |
| `sub_42F560` (`option_print_help`), 1,116 B | **HIGH** | `stat -c%s` confirms exactly 1,116 bytes |
| `sub_42F640` (`option_generate_tkinfo`), 430 B | **HIGH** | `stat -c%s` confirms exactly 430 bytes |
| `nvlink_parse_options` at `0x427AE0`, 30,272 B, 1,299 lines | **HIGH** | `stat -c%s` = 30,272; `wc -l` = 1,299 |
| 68 option registrations via `sub_42F130` | **HIGH** | `grep -c sub_42F130` in `sub_427AE0` returns exactly 68 |
| Parser object layout (56 bytes) | **MEDIUM** | Inferred from decompiled allocation size and field access patterns; not directly labeled |
| Option entry layout (120 bytes) | **MEDIUM** | Inferred from `sub_42F130` allocation and field offsets; consistent across all call sites |
| Type codes (0=file-list, 1=bool, 2=string, 4=integer) | **HIGH** | Visible in `sub_42E390` value extraction and `sub_42F130` registration code |
| `"nvlink option parser"` arena string | **HIGH** | String at `0x1d34123` in `nvlink_strings.json` |
| `"trap-into-debugger"` option string | **HIGH** | String at `0x1d3294f` in `nvlink_strings.json` |
| `"-nvvmpath should be specified with -lto"` validation | **HIGH** | String at `0x1d33dc8` in `nvlink_strings.json` |
| `"-fdcmpt"` forward-compatibility flag | **HIGH** | String at `0x1d32aa4` in `nvlink_strings.json` |
| `"Ofast-compile"` accepts `0/min/mid/max` | **HIGH** | String at `0x1d32324` in `nvlink_strings.json`; values from decompiled switch-case |
| Global variable address map (80+ entries) | **HIGH** | Cross-verified against decompiled `sub_427AE0`; addresses match `option_get_value` calls |
| Mutual-exclusion table (10 conflict pairs) | **HIGH** | Error calls visible in `sub_427AE0` decompiled code after extraction |
| `dword_2A77DC0 = !v8 + 1` for gen-host-linker-script | **HIGH** | Line 1027 of `sub_427AE0` shows `dword_2A77DC0 = !v8 + 1` |
| Architecture validation: sm > 19, sm > 72, sm > 99 thresholds | **HIGH** | Threshold comparisons visible in `sub_427AE0` decompiled code |
| Compilation mode `dword_2A5B528` values (0/2/4/6) | **MEDIUM** | Values inferred from conditional assignments in `sub_427AE0`; exact semantics partially interpreted |
