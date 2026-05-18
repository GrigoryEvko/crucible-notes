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

The parser object (`sub_42DFE0` return value) is allocated from the `"nvlink option parser"` memory arena. Order matches the constructor's writes in `sub_42DFE0`:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Hash table pointer: long-name lookup (`sub_4489C0` of `sub_44E000`/`sub_44E180`) |
| 8 | 8 | Hash table pointer: short-name lookup (`sub_4489C0` of `sub_44E000`/`sub_44E180`) |
| 16 | 8 | Pointer to default file-list option entry (`"Options"`, name = `" "`); set after registration |
| 24 | 1 | `numbered` flag (constructor `a1` argument; written as byte) |
| 25 | 7 | (padding) |
| 32 | 8 | Chain head -- pointer to first 16-byte wrapper `{next, option_entry*}` in the registered-option linked list; NULL before any registration |
| 40 | 8 | Tail cursor -- pointer-to-pointer; initialised to `v6 + 4` (i.e. self+32 = `&chain_head`) via the standard `*(parser+40) = parser+32` self-referencing-tail trick, then advanced to each new wrapper's next-slot as options are appended |
| 48 | 8 | Pointer to the unknown-option fallback entry (`"__internal_unknown_opt"`); set after the second registration call |

The linked list at `+32`/`+40` is the iteration order used by `option_print_help` (`sub_42F560` reads `*(parser+32)` and feeds it to `sub_464700`, which walks a singly-linked list of 16-byte wrappers). The two hash tables at `+0`/`+8` are populated by `sub_42F130` (`option_register`) for O(1) lookup by long or short name during argv parsing.

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
2. For each of 68 options:
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

The validation cascade partitions the active SM tiers into three regimes: PTX-only output for sm_75 / sm_80 / sm_86 / sm_87 / sm_88 / sm_89 (the `sm > 72` branch enables the new-style ELF flag without forcing SASS mode); SASS-required for sm_90 (`elif sm > 89`); and Mercury+SASS for sm_100 / sm_103 / sm_110 / sm_120 / sm_121 (the `sm > 99` branch). sm_70 and sm_72 reach the validator but are rejected downstream by the profile lookup because they have been removed from the active database (see [Architecture Profiles](../targets/arch-profiles.md), "Deprecated Architecture Numbers").

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
- `"min"`: More minimal impact on both compile time and runtime, minimizing some expensive optimizations
- `"0"`: Disables fast-compile (the option is disabled by default)

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

## Option Globals Cross-Reference Matrix

Cross-reference of every BSS global in the option-state region `0x2A5F1FC`--`0x2A5F330`, mined from `nvlink_xrefs.json`. The **Setter** column names the function that issues the write (the CLI extractor `sub_427AE0`, the derivation site, or a runtime updater); the **Top Consumers** column lists the functions that read the global most often, in descending call-count order. Function tags use the short-name conventions already established elsewhere in the wiki: `main` = `0x409800`, `nvlink_parse_options` = `sub_427AE0`, the LTO compile-driver = `sub_426CD0`, the per-module link iterator = `sub_426570`, the inner load loop = `sub_426AE0`, the SASS-mode dispatcher = `sub_4275C0`, the merge consistency tracker = `sub_42AF40`, the tkinfo emitter = `sub_429BA0`, the kernel/variable-keep applier = `sub_42A680`, the arch profile resolver = `sub_42A2D0`, the relocator entry point = `sub_42A190`.

The W/R columns report exact write- and read-site counts from the cross-reference index (call-flow xrefs from `sub_427AE0` that are not data references, such as the `option_get_value` parameter pass, are counted under "Other" in the underlying dataset and excluded here).

| Address | Setter | W | R | Top Consumers |
|---|---|---|---|---|
| `byte_2A5F1FC` | `sub_427AE0` (`option_was_specified`) | 1 | 2 | `sub_429BA0` x2 |
| `byte_2A5F1FD` | `sub_427AE0` (`enable-extended-smem`) | 0* | 1 | `main` |
| `byte_2A5F1FE` | `sub_427AE0` (`device-stack-protector`) | 0* | 1 | `sub_429BA0` |
| `byte_2A5F1FF` | `sub_427AE0` (`option_was_specified`) | 1 | 2 | `sub_429BA0` x2 |
| `qword_2A5F200` | `sub_427AE0` (`option_generate_tkinfo`) | 1 | 1 | `main` |
| `qword_2A5F208` | `sub_427AE0` (`uidx-file`) | 0* | 2 | `main` x2 |
| `byte_2A5F210` | `sub_427AE0` (`disable-smem-reservation`) | 0* | 2 | `main`, `sub_4275C0` |
| `byte_2A5F211` | `sub_426AE0` (runtime: archive flag) | 1 | 2 | `sub_426CD0` x2 |
| `byte_2A5F212` | `sub_427AE0` + `sub_426AE0` + `main` | 5 | 3 | `sub_427AE0` x2, `sub_426AE0` |
| `byte_2A5F213` | `sub_427AE0` (`use-host-info`, then re-derived) | 2 | 3 | `sub_427AE0` x2, `sub_42A680` |
| `byte_2A5F214` | `sub_427AE0` (derived, two branches) | 2 | 3 | `sub_426CD0` x2, `main` |
| `byte_2A5F215` | `sub_427AE0` (`dump-callgraph-no-demangle`) | 0* | 2 | `main`, `sub_427AE0` |
| `byte_2A5F216` | `sub_427AE0` (`dump-callgraph`) | 0* | 2 | `main`, `sub_427AE0` |
| `qword_2A5F218` | `sub_427AE0` (`cuda-api-version`) | 0* | 6 | `sub_429BA0` x4, `sub_427AE0` x2 |
| `byte_2A5F220` | `sub_426570` (runtime, per-module) | 1 | 1 | `main` |
| `byte_2A5F221` | `sub_426570` (runtime, per-module) | 1 | 2 | `main`, `sub_426570` |
| `byte_2A5F222` | `sub_427AE0` (derived, sm > 99) + `sub_426570` (override) | 2 | 8 | `main` x4, `sub_4275C0` x2, `sub_427AE0`, `sub_42A190` |
| `byte_2A5F223` | `sub_427AE0` (`verbose-tkinfo`, then debug override) | 1 | 3 | `main`, `sub_4275C0`, `sub_427AE0` |
| `byte_2A5F224` | `sub_427AE0` (derived, sm > 72) | 2 | 6 | `main` x4, `sub_426570`, `sub_4275C0` |
| `byte_2A5F225` | `sub_427AE0` (derived) + `sub_426570` (override) | 3 | 13 | `main` x7, `sub_427AE0` x2, `sub_42AF40` x2 |
| `byte_2A5F226` | `sub_427AE0` (`suppress-debug-info`) | 0* | 2 | `main`, `sub_427AE0` |
| `byte_2A5F227` | `sub_427AE0` (`uumn`) | 0* | 1 | `sub_427AE0` |
| `byte_2A5F228` | `sub_427AE0` (`fdcmpt`) | 0* | 2 | `main`, `sub_427AE0` |
| `byte_2A5F229` | `sub_426570` (runtime gate) | 1 | 2 | `main`, `sub_426570` |
| `dword_2A5F22C` | `main` (post-parse override) | 1 | 6 | `sub_429BA0` x4, `main`, `sub_426CD0` |
| `qword_2A5F230` | `sub_427AE0` (`Xnvvm`) | 0* | 1 | `sub_426CD0` |
| `qword_2A5F238` | `sub_427AE0` (`Xptxas`) | 0* | 2 | `sub_429BA0` x2 |
| `dword_2A5F240` | `sub_42AF40` (merge tracker) | 3 | 1 | `sub_42AF40` |
| `byte_2A5F244` | `sub_42AF40` (merge tracker) | 1 | 2 | `sub_426CD0` x2 |
| `dword_2A5F248` | `sub_42AF40` (merge tracker) | 3 | 2 | `main`, `sub_42AF40` |
| `byte_2A5F24C` | `sub_42AF40` (merge tracker) | 1 | 2 | `sub_426CD0` x2 |
| `dword_2A5F250` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `byte_2A5F254` | `sub_42AF40` (merge tracker) | 2 | 3 | `main` x2, `sub_42AF40` |
| `qword_2A5F258` | `sub_427AE0` (`Ofast-compile`) | 0* | 12 | `sub_429BA0` x6, `sub_426CD0` x4, `sub_427AE0` x2 |
| `dword_2A5F260` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `dword_2A5F264` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `dword_2A5F268` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `dword_2A5F26C` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `dword_2A5F270` | `sub_42AF40` (merge tracker) | 5 | 3 | `sub_42AF40` x2, `main` |
| `dword_2A5F274` | `sub_42AF40` (merge tracker) | 2 | 2 | `sub_426CD0`, `sub_42AF40` |
| `qword_2A5F278` | `sub_427AE0` (`nvvmpath`) | 0* | 2 | `main`, `sub_427AE0` |
| `dword_2A5F280` | `sub_427A10` + `sub_42AF40` (module counter) | 2 | 1 | `main` |
| `byte_2A5F284` | `sub_427AE0` (`force-whole-lto`) | 0* | 3 | `sub_427AE0` x2, `main` |
| `byte_2A5F285` | `sub_427AE0` (`force-partial-lto`) + `sub_42A680` | 2 | 10 | `main` x4, `sub_426CD0` x3, `sub_427AE0` x3 |
| `byte_2A5F286` | `sub_427AE0` (derived) + `main` (override) + `sub_42A680` | 4 | 5 | `main` x3, `sub_426CD0` x2 |
| `byte_2A5F287` | `sub_427AE0` (`dlto`) | 0* | 1 | `sub_427AE0` |
| `byte_2A5F288` | `sub_427AE0` (`link-time-opt`) + `main` (alias propagate) | 2 | 10 | `main` x6, `sub_427A10`, `sub_427AE0`, `sub_42A680`, `sub_42AF40` |
| `byte_2A5F289` | `sub_427AE0` (`extra-warnings`) | 0* | 1 | `main` |
| `qword_2A5F290` | `sub_427AE0` (`time`) | 0* | 12 | `main` x9, `sub_42AF40` x3 |
| `byte_2A5F298` | `sub_427AE0` (`suppress-arch-warning`) | 0* | 3 | `sub_42A2D0` x2, `sub_4297B0` |
| `byte_2A5F299` | `sub_427AE0` (`suppress-stack-size-warning`) | 0* | 1 | `main` |
| `byte_2A5F29A` | `sub_427AE0` (`emit-ptx`) | 0* | 6 | `sub_427AE0` x4, `main` x2 |
| `byte_2A5F29B` | `sub_427AE0` (`verbose-keep`) | 0* | 13 | `main` x8, `sub_42AF40` x3, `sub_427A10` x2 |
| `byte_2A5F29C` | `sub_427AE0` (`report-arch`) | 0* | 2 | `main`, `sub_427AE0` |
| `qword_2A5F2A0` | `sub_427AE0` (`cpu-arch`) | 0* | 1 | `sub_42A2D0` |
| `byte_2A5F2A8` | `sub_427AE0` (`optimize-data-layout`) | 0* | 2 | `main`, `sub_427AE0` |
| `byte_2A5F2A9` | `sub_427AE0` (`no-opt`) | 0* | 3 | `main`, `sub_4275C0`, `sub_427AE0` |
| `byte_2A5F2AA` | `sub_427AE0` (`force-rela`) | 0* | 1 | `main` |
| `qword_2A5F2B0` | `sub_427AE0` (`variables-used`) | 0* | 2 | `main`, `sub_427AE0` |
| `qword_2A5F2B8` | `sub_427AE0` (`kernels-used`) | 0* | 2 | `main`, `sub_427AE0` |
| `byte_2A5F2C0` | `sub_427AE0` (derived: `sub_44E4F0(arch)`) | 1 | 7 | `main` x6, `sub_42AF40` |
| `byte_2A5F2C1` | `sub_427AE0` (derived: `sub_44E490(arch)`) | 1 | 5 | `sub_426570` x2, `sub_427AE0` x2, `main` |
| `byte_2A5F2C2` | `sub_427AE0` (`keep-system-libraries`) | 0* | 2 | `main` x2 |
| `byte_2A5F2C3` | `sub_427AE0` (`warning-as-error`) | 0* | 1 | `sub_427AE0` |
| `byte_2A5F2C4` | `sub_427AE0` (`disable-warnings`) | 0* | 1 | `sub_427AE0` |
| `byte_2A5F2C5` | `sub_427AE0` (`disable-infos`) | 0* | 1 | `sub_427AE0` |
| `dword_2A5F2C8` | `sub_427AE0` (`syscall-const-offset`) | 0* | 1 | `main` |
| `byte_2A5F2CC` | `sub_427AE0` (`allow-undefined-globals`) | 0* | 1 | `main` |
| `byte_2A5F2CD` | `sub_427AE0` (derived from two locals) | 1 | 1 | `main` |
| `byte_2A5F2CE` | `sub_427AE0` (`preserve-relocs`) | 0* | 2 | `main`, `sub_427AE0` |
| `qword_2A5F2D0` | `sub_427AE0` (`dot-file`) | 0* | 2 | `main` x2 |
| `byte_2A5F2D8` | `sub_427AE0` (`verbose`) | 0* | 3 | `main` x3 |
| `qword_2A5F2E0` | `sub_427AE0` (`register-link-binaries`) | 0* | 11 | `main` x7, `sub_42A680` x2, `sub_42AF40` x2 |
| `qword_2A5F2E8` | `sub_427AE0` (`host-linker-options`) | 0* | 1 | `main` |
| `qword_2A5F2F8` | `sub_427AE0` (`library`) | 0* | 1 | `main` |
| `qword_2A5F300` | `sub_427AE0` (`library-path`) | 0* | 1 | `main` |
| `dword_2A5F308` | `sub_427AE0` (`edbg`) | 0* | 13 | `sub_4275C0` x6, `main` x4, `sub_42A680` x2, `sub_42AF40` |
| `byte_2A5F310` | `sub_427AE0` (`debug`, cleared by suppress) | 1 | 14 | `main` x6, `sub_42AF40` x3, `sub_426CD0` x2, `sub_427AE0` x2, `sub_4275C0` |
| `dword_2A5F30C` | `sub_427AE0` (`machine`) | 0* | 17 | `main` x9, `sub_42AF40` x4, `sub_427AE0` x2, `sub_426570`, `sub_42A190` |
| `dword_2A5F314` | `sub_427AE0` (derived: `sub_44E3E0(arch)`) | 1 | 31 | `main` x16, `sub_427AE0` x5, `sub_426570` x4, `sub_42AF40` x4, `sub_426CD0`, `sub_42A190` |
| `qword_2A5F318` | `sub_427AE0` (`arch`) | 0* | 20 | `sub_427AE0` x7, `main` x5, `sub_426570` x2, `sub_42A2D0` x2, `sub_42AF40` x2, `sub_4297B0`, `sub_42A190` |
| `qword_2A5F320` | `sub_427AE0` (`output-file`, aka `filename`) | 0* | 18 | `main` x16, `sub_427AE0`, `sub_4299E0` |
| `qword_2A5F328` | `sub_427AE0` (positional list head, `option_get_value(" ")`) | 0* | 1 | `sub_427AE0` |
| `qword_2A5F330` | `sub_427AE0` (positional list tail, `sub_4648C0`) | 1 | 5 | `main` x3, `sub_427AE0` x2 |

\* `W=0*` means the CLI extractor writes the global through `option_get_value`'s out-pointer argument (`sub_42E390`), which the xref index records as a call-flow edge rather than a data-write to the global. The corresponding entry in the "Other" category of `nvlink_xrefs.json` covers these stores. Counts shown here are the data-write xrefs proper.

> **QUIRK (multi-source globals).** `byte_2A5F212` (`ignore-host-info`) has the highest write-fan-in of any option global: it is written from `sub_427AE0` (initial CLI extraction), then forced to 1 by the relocatable-link branch in the same function (lines 351-352), again by `main` when `--kernels-used`/`--variables-used` is set, and by `sub_426AE0` from per-module `.nv_info` data (line 121). Five distinct write sites for what looks like a plain boolean option.

> **QUIRK (CLI flag mutated at runtime).** `byte_2A5F225` (SASS output mode) is written three times: once by the CLI as a true derived flag (sm > 99 -> 1, sm > 89 -> 1 via the unset branch), once by the post-extraction recheck, and a third time by `sub_426570` -- the per-module link iterator can *promote* a non-SASS run to SASS mode mid-link based on module attributes. Same pattern with `byte_2A5F222` (mercury mode) and `byte_2A5F220`/`byte_2A5F221` (companion module-scoped state).

> **QUIRK (CLI flag never read by any consumer).** `byte_2A5F227` (`uumn`, undocumented forward-compat helper) has zero reads outside `sub_427AE0` itself. The flag exists solely so that `sub_427AE0` can pair it with `fdcmpt` (`byte_2A5F228`) for the SM <= 69 fatal-error check; once option parsing finishes, no downstream pass observes it. Several other globals (`byte_2A5F2C3`/`byte_2A5F2C4`/`byte_2A5F2C5`) are likewise read only by the parser, because the diagnostic state is forwarded into `sub_468420`/`sub_468430` and the booleans themselves are dead afterward.

## Derived Globals

Twelve globals in the `0x2A5F1FC`--`0x2A5F330` window are computed by `sub_427AE0` after extracting raw option values, plus the runtime-promotion globals owned by `sub_426570`. Their decision trees are listed below in source order. All line numbers refer to `decompiled/sub_427AE0_0x427ae0.c` unless otherwise noted.

### `byte_2A5F1FC` -- *device-stack-protector-frame-size-threshold was specified*

```
byte_2A5F1FC = option_was_specified(parser, "device-stack-protector-frame-size-threshold")
```

Pure presence query (`sub_42E580`, line 271). Read by `sub_429BA0` to decide whether to emit the threshold into tkinfo.

### `byte_2A5F1FF` -- *device-stack-protector was specified*

```
byte_2A5F1FF = option_was_specified(parser, "device-stack-protector")
```

Same pattern as the frame-size flag (line 270). Read by `sub_429BA0` for the tkinfo entry.

### `qword_2A5F200` -- *tkinfo data pointer*

```
qword_2A5F200 = (int64)option_generate_tkinfo(parser, ...)            // line 497
```

Set by the parser's serializer (`sub_42F640`) at the tail end of `sub_427AE0`. Consumed by `main` when the linker emits the `.nv_info` section.

### `byte_2A5F213` and `byte_2A5F214` -- *use-host-info / host-info active*

Three-way decision tree at lines 349-364, after extracting `byte_2A5F212` (`ignore-host-info`) and `byte_2A5F213` (`use-host-info`):

```
if (use_host_info && ignore_host_info):
    fatal_error("-use-host-info", "-ignore-host-info")

if (relocatable_link):                            # line 351
    ignore_host_info = 1                          # force ignore on -r

if (kernels_used || variables_used):              # line 353
    if (use_host_info):
        warning("ignore -use-host-info because -kernels-used or -variables-used")
    byte_2A5F213 = 0                              # clear use-host-info
    byte_2A5F214 = 1                              # host_info_active = 1
elif (!ignore_host_info):                         # line 360 (default branch)
    byte_2A5F213 = 1                              # default use-host-info on
    byte_2A5F214 = 1                              # host_info_active = 1
# else: both stay 0 (ignore-host-info wins, host_info_active stays 0)
```

`byte_2A5F214` is the true "host info is active for this link" signal consumed by `sub_426CD0` (LTO compile-driver) and `main`; `byte_2A5F213` retains the literal CLI intent for diagnostics.

### `byte_2A5F222`, `byte_2A5F224`, `byte_2A5F225` -- *SM-derived output mode triple*

Computed at lines 296-326 after `sub_44E3E0(arch_string)` returns the parsed SM number into `dword_2A5F314`:

```
sm = dword_2A5F314                                # line 296
byte_2A5F224 = (sm > 0x48)                        # new-style ELF: sm > 72 (line 302)

if (sm > 0x48 && machine == 32):                  # line 303
    fatal_error("32-bit not allowed for sm > 72")
    byte_2A5F224 = 0

if (sm > 0x63):                                   # line 309: sm > 99 (Mercury)
    byte_2A5F222 = 1                              # mercury mode
    byte_2A5F225 = 1                              # SASS output
    byte_2A5B510 = 0
elif (byte_2A5F222):                              # line 316: mercury pre-flag set
    byte_2A5F225 = 1                              # SASS output
elif (!byte_2A5F225):                             # line 320: neither mercury nor SASS
    byte_2A5B510 = 1                              # use PTX path
    goto LABEL_28
# Reaches here only if byte_2A5F225 was already 1 OR mercury triggered
byte_2A5B510 = 0
if (sm <= 0x59):                                  # line 326: SASS requires sm > 89
    fatal_error("SASS mode requires sm >= 90")
```

Note that `byte_2A5F222` can be set *before* this code path executes -- `sub_426570` flips it during per-module loading when it discovers Mercury-class metadata in an input. That is why the `elif (byte_2A5F222)` branch exists.

### `byte_2A5F2C0` and `byte_2A5F2C1` -- *arch capability / output-is-archive*

```
byte_2A5F2C1 = sub_44E490(arch_string)            # line 293: output-is-archive flag
byte_2A5F2C0 = sub_44E4F0(arch_string)            # line 295: arch capability flag
byte_2A5B52C = sub_44E4D0(arch_string)            # line 294: arch-supported flag
```

All three are pure functions of the parsed `--arch` string. `byte_2A5F2C1` selects passthrough/archive mode in `dword_2A5B528` (line 367); `byte_2A5F2C0` controls a capability gate consulted by `main` and `sub_42AF40`.

### `byte_2A5F2CD` -- *reserve-null-pointer effective flag*

```
v15 = 0
if (src):                                         # line 335: reserve-null-pointer was passed
    v15 = (v26 == 0)                              # v26 is dont-reserve-null-pointer
byte_2A5F2CD = v15                                # line 337
```

The "dont-" form wins outright if specified; reserve-null is only effective when reserve-null is passed *and* dont-reserve-null is not.

### `byte_2A5F286` -- *partial-LTO active*

Set inside the LTO validation cascade at line 427 (LABEL_68):

```
if (link_time_opt):                               # line 371: byte_2A5F288 == 1
    if (!nvvmpath): fatal("-nvvmpath should be specified with -lto")
    if (relocatable_link):                        # line 375
        force_partial_lto = 1                     # promote -r to partial LTO
    elif (!force_partial_lto):                    # line 379
        if (!emit_ptx):
            goto LABEL_104                        # check Ofast-compile, then mode = LTO

    if (force_whole_lto):                         # line 418
        fatal("-force-partial-lto", "-force-whole-lto")
        ...
    elif (!emit_ptx):
        # LABEL_68
        byte_2A5F286 = 1                          # partial-LTO active
        goto LABEL_104
```

`byte_2A5F286` is *only* set to 1 in this single branch. It also has separate writers in `main` (LTO alias propagation) and `sub_42A680` (kernel/variable-keep applier forces partial-LTO when keep lists are present).

### `dword_2A5F314` -- *parsed SM number*

```
sm = sub_44E3E0(qword_2A5F318, 0, ...)            # line 290: parse "sm_75" -> 75
if (sm <= 0x13):                                  # line 291: sm <= 19 (deprecated)
    fatal_error("unsupported arch", arch_string)
dword_2A5F314 = sm                                # line 296
```

The single most heavily-consumed derived global (31 read sites) -- it drives almost every architecture-conditional code path in the linker, from profile lookup to relocation type selection to ELF flag construction.

### `byte_2A5F220`, `byte_2A5F221`, `byte_2A5F229` -- *runtime-set companion flags*

These three live in the option-globals region but are written by `sub_426570` (the per-module link iterator), not by the CLI parser. Their decision trees fire while ingesting each input object:

```
# sub_426570 (per-module iteration), lines 93-240
if (!byte_2A5F221 || !sub_43E610(...) || sub_4709E0(...)):
    ...
elif (!byte_2A5F229):
    byte_2A5F229 = 1                              # first matched module marker
...
if (<module exposes target-mode metadata>):
    byte_2A5F220 = 1                              # at least one module forced target mode
...
byte_2A5F221 = 1                                  # marks the first module that flipped state
```

`byte_2A5F221` and `byte_2A5F229` form a two-stage gate that prevents the iterator from applying a module-derived override more than once. Documented here because they share BSS space and registration semantics with the true CLI globals, even though they are derived from input objects rather than argv.

### `byte_2A5F244`, `byte_2A5F24C`, `dword_2A5F240`..`dword_2A5F274` -- *merge-consistency tracker*

Owned by `sub_42AF40` (the merge-consistency tracker called for each input module). Each `dword_2A5F26C/268/264/260/270` field implements the same state machine used to detect cross-module flag disagreement:

```
# Per scalar option that must match across all input modules:
#   state 0: unset
#   state 1: every module read so far had value V (recorded in companion field)
#   state 2: at least one module deviated
#   state 3: cross-module conflict detected
state = current
if (module_has_value):
    if (state == 0):    state, recorded_value = 2, v
    elif (state == 1):
        if (v == recorded_value):  state = 3
        elif (v != recorded_value): state = 4
    ...
else:
    if (state == 2):    state = 3
    elif (state == 0):  state = 1
```

The merge tracker reads back into `sub_426CD0` (which feeds it as `--has-global-host-info` / per-module switches to the embedded ptxas). These globals are CLI-shaped only because they share registration prototype with `byte_2A5F244`/`byte_2A5F24C`, but their values come exclusively from runtime aggregation, not argv.

## Internally-Synthesized Sub-Tool Flags (Not nvlink CLI Surface)

`nvlink_strings.json` contains a number of flag-shaped string literals that are **not** registered with `option_register` and are therefore not part of nvlink's CLI surface. They fall into two groups:

1. **Flags nvlink emits when invoking the embedded ptxas backend.** Constructed in `sub_426CD0` (LTO compile-driver) and consumed inside the embedded ptxas core at `sub_110*`/`sub_111*`. Representative strings:
   - `-link-lto`, `-inline-info`, `-has-global-host-info`, `--device-c`, `--force-device-c` (emitted by `sub_426CD0` based on parsed nvlink globals such as `byte_2A5F244`, `byte_2A5F286`, `byte_2A5F285`)
   - `-generate-line-info`, `--compile-only`, `--extensible-whole-program`, `--fast-compile`, `--device-debug`, `--blocks-are-clusters`, `--assume-extern-functions-do-not-sync`, `--compile-as-tools-patch`, `--legacy-bar-warp-wide-behavior`, `--first-reserved-rreg`, `--no-membermask-overlap`, `--print-potentially-overlapping-membermasks`, `--opportunistic-finalization-lvl`, `--binary-kind`, `--okey`, `--assyscall`, `-forcetext`, `-dump-perf-stats`, `-dump-perf-metrics-file`, `--ptx-length`, `-ptxlen` (all referenced only from ptxas-internal functions in the `0x1104000`-`0x1113000` range)
2. **Format/diagnostic fragments that look like flags** -- e.g. `-arch=compute_%d`, `-cuda-api-version=%s`, `-split-compile=%d`, `-Ofast-compile=`, `-fma=`, `-ftz=`, `-prec-div=`, `-prec-sqrt=`. These are `sprintf` templates the linker uses to construct ptxas/cicc command lines from parsed option values.

Users wanting to control any of the group-1 flags must pass them through `--Xptxas` (forwarded to the embedded ptxas) or `--Xnvvm` (forwarded to cicc), not directly to nvlink. The ptxas wiki documents the full ptxas option surface; see [LTO Option Forwarding](../lto/option-forwarding.md) for how forwarding is wired.

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
