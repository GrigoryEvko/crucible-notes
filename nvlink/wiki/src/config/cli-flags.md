# CLI Flags Reference

Quick-reference table of every command-line flag recognized by nvlink v13.0.88. Flags are sorted alphabetically for fast lookup. For implementation details (registration addresses, global variable mappings, post-extraction validation, mutual-exclusion rules) see [pipeline/cli-options.md](../pipeline/cli-options.md).

| | |
|---|---|
| **Total flags** | 65 (including 4 hidden/internal) |
| **Parser entry** | `nvlink_parse_options` at `0x427AE0` (30,272 bytes) |
| **Registration call** | `option_register` at `0x42F130`, called once per flag |
| **Binary** | nvlink v13.0.88, CUDA 13.0 |

## Reading the Table

**Type** -- the parser's internal type code: `bool` (1-byte 0/1), `string` (8-byte pointer), `int` (4-byte dword), `file-list` (linked list of positional args).

**Default** -- the value the parser assigns if the flag is absent from the command line. `--` means no default (value is zero-initialized or not applicable for booleans).

**Visibility** -- whether the flag appears in `--help` output. Hidden flags have registration flag bit 2 (`0x04`) or bit 3 (`0x08`) set.

## Alphabetical Flag Table

| # | Flag | Short | Type | Default | Visibility | Description |
|---|---|---|---|---|---|---|
| 1 | `--allow-undefined-globals` | -- | bool | false | hidden | Allow undefined globals and their relocations in linked executable. |
| 2 | `--arch` | `-arch` | string | *(none)* | public | Specify the `sm_` name of the target GPU architecture. Validated against the supported SM table; must be > sm\_19. |
| 3 | `--cpu-arch` | `-cpu-arch` | string | `unknown` | public | Specify the host CPU architecture. Allowed values: `unknown`, `X86`, `X86_64`, `ARMv7`, `AARCH64`, `PPC64LE`. |
| 4 | `--cuda-api-version` | `-cuda-api-version` | string | *(none)* | hidden | CUDA API version for linking. Parsed as `%u.%u`; major must match toolkit version. |
| 5 | `--debug` | `-g` | bool | false | public | Mark this as a debug compile. Enables DWARF processing and forces verbose tkinfo. |
| 6 | `--device-stack-protector` | `-device-stack-protector` | bool | false | public | Enable device-side stack protectors. |
| 7 | `--device-stack-protector-frame-size-threshold` | `-device-stack-protector-frame-size-threshold` | int | 0 | hidden | Set the minimum frame size (bytes) that triggers stack protector insertion. |
| 8 | `--disable-infos` | `-disable-infos` | bool | false | public | Suppress all informational messages. |
| 9 | `--disable-smem-reservation` | `-disable-smem-reservation` | bool | false | hidden | Disable shared memory reservation. Accepts `true`/`false` value. |
| 10 | `--disable-warnings` | `-w` | bool | false | public | Inhibit all warning messages. |
| 11 | `--dlto` | `-dlto` | bool | false | public | Enable link-time optimization (alias for `--link-time-opt`). Sets the LTO master flag. |
| 12 | `--dont-reserve-null-pointer` | `-dont-reserve-null-pointer` | bool | false | hidden | Do not reserve address 0 as NULL. Always overrides `--reserve-null-pointer` regardless of order. |
| 13 | `--dot-file` | `-dot` | string | *(none)* | hidden | Write callgraph in DOT format to the specified file. |
| 14 | `--dump-callgraph` | `-dump-callgraph` | bool | false | public | Dump callgraph information to stderr. Mutually exclusive with `--dump-callgraph-no-demangle`. |
| 15 | `--dump-callgraph-no-demangle` | `-dump-callgraph-no-demangle` | bool | false | public | Dump callgraph without C++ name demangling. Mutually exclusive with `--dump-callgraph`. |
| 16 | `--edbg` | `-edbg` | int | 0 | **internal** | Internal ELF debugging output level. Flag bits = `0x08` (strongest hiding). |
| 17 | `--emit-ptx` | `-emit-ptx` | bool | false | public | Emit PTX intermediate file when LTO is used. Requires `--lto` or `--dlto`. |
| 18 | `--enable-extended-smem` | `-enable-extended-smem` | bool | false | hidden | Enable extended (> 48 KB) shared memory. Accepts `true`/`false` value. |
| 19 | `--extra-warnings` | `-extrawarn` | bool | false | public | Emit extra warnings about possible linking problems. |
| 20 | `--fdcmpt` | `-fdcmpt` | bool | false | hidden | Forward-compatibility flag. Requires `--uumn`; without it a warning is emitted. Both set on SM <= 69 is a fatal error. |
| 21 | `--force-partial-lto` | `-force-partial-lto` | bool | false | hidden | Force partial LTO when `--dlto` is specified. Mutually exclusive with `--force-whole-lto`. |
| 22 | `--force-rela` | `-force-rela` | bool | false | hidden | Force RELA relocations in executables instead of REL. |
| 23 | `--force-whole-lto` | `-force-whole-lto` | bool | false | hidden | Force whole-program LTO when `--dlto` is specified. Mutually exclusive with `--force-partial-lto`. |
| 24 | `--gen-host-linker-script` | `-ghls` | string | `lcs-abs` | public | Generate a host linker script. Allowed values: `lcs-aug` (augmented), `lcs-abs` (absolute). |
| 25 | `--help` | `-h` | bool | -- | public | Print help information and exit. |
| 26 | `--host-ccbin` | `-host-ccbin` | string | *(none)* | hidden | Path to host compiler binary (gcc/clang). |
| 27 | `--host-linker-options` | `-Xlinker` | string | *(none)* | hidden | Options forwarded directly to the host linker. Multi-value (accumulates). Ignored by device linker. |
| 28 | `--ignore-host-info` | `-ignore-host-info` | bool | false | public | Ignore host reference information; do not remove potentially host-referenced device code. Mutually exclusive with `--use-host-info`. |
| 29 | `--keep-system-libraries` | `-keep-system-libraries` | bool | false | public | Do not optimize away system library code (e.g. cudadevrt). |
| 30 | `--kernels-used` | `-kernels-used` | string | *(none)* | public | Substring-match list of kernels to keep; all others are treated as dead code. Multi-value. |
| 31 | `--library` | `-l` | string | *(none)* | public | Specify libraries for linking. Searched on `-L` paths. Multi-value. |
| 32 | `--library-path` | `-L` | string | *(none)* | public | Specify library search directories. Multi-value. |
| 33 | `--link-time-opt` | `-lto` | bool | false | public | Enable link-time optimization. Requires `--nvvmpath`. |
| 34 | `--machine` | `-m` | int | 64 | public | Specify machine word size. Only 64 is accepted; 32 on SM > 72 is a fatal error. |
| 35 | `--maxrregcount` | `-maxrregcount` | int | 0 | public | Maximum register count per thread. Forwarded to ptxas during LTO. |
| 36 | `--no-opt` | `-no-opt` | bool | false | **internal** | Disable linker optimization of data resources. Mutually exclusive with `--optimize-data-layout`. |
| 37 | `--nv-host` | `-nv-host` | string | *(none)* | hidden | Path to nv.host file (NVIDIA internal infrastructure). |
| 38 | `--nvvmpath` | `-nvvmpath` | string | *(none)* | public | Path to `libnvvm.so` / `nvvm64_*.dll`. Required when `--lto` is specified. |
| 39 | `--Ofast-compile` | `-Ofc` | string | `0` | public | Fast-compile level for LTO. Allowed values: `0` (full opt), `min`, `mid`, `max` (fastest compile). Requires `--lto`/`--dlto`. |
| 40 | `--optimize-data-layout` | `-optimize-data-layout` | bool | false | **internal** | Force linker optimization of data resources. Mutually exclusive with `--no-opt`. |
| 41 | `--options-file` | `-optf` | file-list | *(none)* | public | Read additional command-line options from the specified file. Multi-value. Supports `@file` syntax. |
| 42 | `--output-file` | `-o` | string | *(none)* | public | Name and location of the output file. |
| 43 | `--preserve-relocs` | `-preserve-relocs` | bool | false | public | Preserve resolved relocations in linked executable. Warning on SM > 89: not supported. |
| 44 | `--register-link-binaries` | -- | string | *(none)* | public | Output file listing `cudaRegister` routine names for each linked input. |
| 45 | `--relocatable-link` | `-r` | bool | false | public | Perform relocatable (incremental) link. Forces `--ignore-host-info` and partial LTO mode. |
| 46 | `--report-arch` | `-report-arch` | bool | false | public | Include SM target architecture name in error messages. |
| 47 | `--reserve-null-pointer` | `-reserve-null-pointer` | bool | false | hidden | Reserve address 0 as NULL pointer. Overridden by `--dont-reserve-null-pointer`. |
| 48 | `--shared` | `-shared` | bool | false | hidden | Propagate nvcc `-shared` flag for nvlink consumption. |
| 49 | `--split-compile` | `-split-compile` | int | 1 | public | Maximum threads NVVM may use for split compilation. Only effective with LTO. |
| 50 | `--split-compile-extended` | `-split-compile-extended` | int | 1 | public | Maximum threads the linker may use for extended split compilation. Only effective with LTO. |
| 51 | `--suppress-arch-warning` | `-suppress-arch-warning` | bool | false | public | Suppress warnings about objects not containing code for the target architecture. |
| 52 | `--suppress-debug-info` | `-suppress-debug-info` | bool | false | public | Do not preserve debug symbols in output. Requires `--debug`; fatal error otherwise. |
| 53 | `--suppress-stack-size-warning` | `-suppress-stack-size-warning` | bool | false | public | Suppress warnings when stack size cannot be determined. |
| 54 | `--syscall-const-offset` | `-syscall-const-offset` | int | 0 | hidden | Byte offset where syscall constants begin in the constant bank. |
| 55 | `--time` | `-time` | string | *(none)* | public | Append CSV timing data to the specified file. Use `-` for stdout. |
| 56 | `--tool-name` | `-tool-name` | string | *(none)* | hidden | Override the tool name shown in diagnostics. |
| 57 | `--trap-into-debugger` | `-_trap_` | bool | false | **internal** | Install signal handlers that trap into a debugger on assertion failure or crash. Flag bits = `0x08`. |
| 58 | `--uidx-file` | `-uidx` | string | *(none)* | public | Path to uidx (unified index) file. |
| 59 | `--uumn` | `-uumn` | bool | false | hidden | Undocumented companion to `--fdcmpt`. No help text in binary. |
| 60 | `--use-host-info` | `-use-host-info` | bool | true | public | Use host reference information to remove unused device code. Default when neither host-info flag is specified. Mutually exclusive with `--ignore-host-info`. |
| 61 | `--variables-used` | `-variables-used` | string | *(none)* | public | Substring-match list of variables to keep; others are candidates for dead-code elimination. Multi-value. |
| 62 | `--verbose` | `-v` | bool | false | public | Enable verbose mode; print code generation statistics. |
| 63 | `--verbose-keep` | `-vkeep` | bool | false | **internal** | Show nvlink pipeline steps and keep intermediate files. |
| 64 | `--verbose-tkinfo` | `-verbose-tkinfo` | bool | false | hidden | Emit object name and command-line arguments into the tkinfo section. Forced on by `--debug`. |
| 65 | `--version` | `-V` | bool | -- | public | Print version information and exit. |
| 66 | `--warning-as-error` | `-Werror` | bool | false | public | Promote all warnings to errors. |
| 67 | `--Xnvvm` | `-Xnvvm` | string | *(none)* | public | Options forwarded to NVVM (cicc) during LTO. Multi-value. |
| 68 | `--Xptxas` | `-Xptxas` | string | *(none)* | public | Options forwarded to ptxas during LTO. Multi-value. |

## Visibility Legend

| Label | Flag Bits | Meaning |
|---|---|---|
| **public** | `0x00` | Shown in `--help` output |
| **hidden** | `0x04` | Not shown in `--help`; accepted silently |
| **internal** | `0x08` | Strongest hiding; truly internal/debug-only |

Public flags use flag bits `0x00` or `0x10` (the `0x10` bit enables `--no-<name>` negation, not hiding). Hidden flags have bit 2 set. Internal flags have bit 3 set and are reserved for NVIDIA developer use.

## Notes

**Multi-value flags.** Flags marked "Multi-value" in the Description column accept repeated occurrences; each use appends to a linked list. Example: `-lcudadevrt -lm -L/usr/local/cuda/lib64`.

**Boolean-with-value flags.** Four bool flags are registered with multiplicity 1, meaning they accept an explicit `true`/`false` argument rather than being simple presence-toggles: `--disable-smem-reservation`, `--enable-extended-smem`, `--verbose-tkinfo`, `--device-stack-protector`.

**Response files.** nvlink supports `--options-file <path>` / `-optf <path>` and the shorthand `@<path>`. Both are recursive: a response file may reference other response files.

## Cross-References

- [CLI Option Parsing](../pipeline/cli-options.md) -- parser infrastructure, option entry layout, registration sequence, post-extraction validation, mutual-exclusion rules, dependency rules, architecture-gated behavior, global variable map.
- [Pipeline Overview](../pipeline/overview.md) -- how parsed flags drive mode dispatch.
- [LTO Option Forwarding](../lto/option-forwarding.md) -- how `--Xptxas`, `--Xnvvm`, `--maxrregcount`, and `--Ofast-compile` are forwarded to cicc/ptxas.
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- how `--kernels-used`, `--variables-used`, `--use-host-info`, and `--ignore-host-info` drive DCE.
- [Debug Options](../debug/options.md) -- detailed semantics of `--debug`, `--suppress-debug-info`, `--edbg`.
