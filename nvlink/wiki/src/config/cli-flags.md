# CLI Flags Reference

Quick-reference table of every command-line flag recognized by nvlink v13.0.88. Flags are sorted alphabetically for fast lookup. For implementation details (registration addresses, global variable mappings, post-extraction validation, mutual-exclusion rules) see [pipeline/cli-options.md](../pipeline/cli-options.md).

| | |
|---|---|
| **Total flags** | 68 (43 public, 20 hidden, 5 internal) |
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

## Confidence Assessment

### Aspect-Level Confidence

| Aspect | Confidence | Basis |
|---|---|---|
| Parser entry address (`0x427AE0`) | HIGH | File `decompiled/sub_427AE0_0x427ae0.c` exists (1,299 lines); contains all `sub_42F130` calls |
| Registration function (`0x42F130`) | HIGH | File `decompiled/sub_42F130_0x42f130.c` exists; called 68 times in `sub_427AE0` |
| Type/multiplicity/flags/defaults | HIGH | Extracted directly from `sub_42F130` positional parameters in decompiled code |
| Visibility bits (`0x00`/`0x04`/`0x08`) | HIGH | Read from `sub_42F130` arg-6 (flags) for every registration |
| Mutual-exclusion rules | HIGH | Confirmed in post-extraction validation blocks in `sub_427AE0` |
| Description text | MEDIUM | Paraphrased from help-text strings embedded in the binary; hidden flags lack help text |

### Per-Flag Verification (Exhaustive)

Each row below was verified two ways: (1) the long-name appears as a direct string literal in the `sub_42F130(v2, "<name>", ...)` call at a known line of `decompiled/sub_427AE0_0x427ae0.c`, and (2) the name is visible at a specific address in `nvlink_strings.json` or embedded inside an adjacent help/format string. All 68 registrations use direct string literals at the registration call. The `HIGH*` tag on five flags (`link-time-opt`, `time`, `split-compile`, `split-compile-extended`, `maxrregcount`) marks rows where the matching `sub_42E390` *extraction* call later in `nvlink_parse_options` uses a mid-string integer constant (pointing into a longer format/diagnostic string that happens to contain the option name as a suffix) instead of re-quoting the literal; the registration itself is still a plain string literal.

| Flag | Confidence | Evidence |
|---|---|---|
| `--allow-undefined-globals` | HIGH | string at `0x1d325c3`; registered in `sub_427AE0` around line 293 |
| `--arch` | HIGH | embedded in `-arch=%s` (`0x1eec37a`), `-arch=compute_%d` (`0x1d32257`), `-arch=sm_...`; registered in `sub_427AE0` |
| `--cpu-arch` | HIGH | string at `0x1d326cd`; registered in `sub_427AE0` |
| `--cuda-api-version` | HIGH | embedded in `-cuda-api-version=%s` (`0x1d33ed7`), `--cuda-api-version` (`0x1eec1dc`), diagnostic at `0x1d33df0`; registered in `sub_427AE0` |
| `--debug` | HIGH | embedded in `Note: This option is ignored if used without --debug option.` (`0x1d33a..`); registered in `sub_427AE0` |
| `--device-stack-protector` | HIGH | string at `0x1d32891`; registered in `sub_427AE0` |
| `--device-stack-protector-frame-size-threshold` | HIGH | string at `0x1d33b68`; registered in `sub_427AE0` |
| `--disable-infos` | HIGH | string at `0x1d32654`; registered in `sub_427AE0` |
| `--disable-smem-reservation` | HIGH | string at `0x1d3259d`; registered in `sub_427AE0` |
| `--disable-warnings` | HIGH | string at `0x1d325f0`; registered in `sub_427AE0` |
| `--dlto` | HIGH | embedded in `no -dlto` (`0x1d32a89`), `Ignoring -dlto option because no LTO objects found` (`0x1d34998`), `LTO objects found, use -dlto` (`0x1d34f28`); registered in `sub_427AE0` |
| `--dont-reserve-null-pointer` | HIGH | string at `0x1d32583`; long help-text at `0x1d32eb8`; registered in `sub_427AE0` |
| `--dot-file` | HIGH | string at `0x1d3257a`; registered in `sub_427AE0` |
| `--dump-callgraph` | HIGH | string at `0x1d32a1a` (`-dump-callgraph`); registered in `sub_427AE0` |
| `--dump-callgraph-no-demangle` | HIGH | string at `0x1d329fe` (`-dump-callgraph-no-demangle`); registered in `sub_427AE0` |
| `--edbg` | HIGH | direct string literal in `sub_42F130` call; registered in `sub_427AE0` |
| `--emit-ptx` | HIGH | string at `0x1d32a6f` (`-emit-ptx`); registered in `sub_427AE0` |
| `--enable-extended-smem` | HIGH | string at `0x1d328e8`; registered in `sub_427AE0` |
| `--extra-warnings` | HIGH | strings at `0x1d32735` (long) and `0x1d3272b` (`extrawarn` short); registered in `sub_427AE0` |
| `--fdcmpt` | HIGH | string at `0x1d32aa4` (`-fdcmpt`); registered in `sub_427AE0` line 619 |
| `--force-partial-lto` | HIGH | string at `0x1d32a5c` (`-force-partial-lto`), help at `0x1d335a8`; registered in `sub_427AE0` |
| `--force-rela` | HIGH | string at `0x1d326c2`; registered in `sub_427AE0` |
| `--force-whole-lto` | HIGH | string at `0x1d32a4b` (`-force-whole-lto`), help at `0x1d335d0`; registered in `sub_427AE0` |
| `--gen-host-linker-script` | HIGH | string at `0x1d327fc`; `-ghls` diagnostic at `0x1d34e80`; registered in `sub_427AE0` |
| `--help` | HIGH | direct literal in `sub_42F130` call; `use --help for more information` at `0x1d34f28`; registered in `sub_427AE0` |
| `--host-ccbin` | HIGH | string at `0x1d3283d`; registered in `sub_427AE0` |
| `--host-linker-options` | HIGH | string at `0x1d32539`; short-name `Xlinker` at `0x1d32531`; registered in `sub_427AE0` |
| `--ignore-host-info` | HIGH | string at `0x1d32a2a` (`-ignore-host-info`), diagnostic `ignore -use-host-info because...` at `0x1d33d78`; registered in `sub_427AE0` |
| `--keep-system-libraries` | HIGH | string at `0x1d3267c`; registered in `sub_427AE0` |
| `--kernels-used` | HIGH | string at `0x1d32692`; referenced in diagnostic at `0x1d33d78`; registered in `sub_427AE0` |
| `--library` | HIGH | string at `0x1d324e6`; registered in `sub_427AE0` |
| `--library-path` | HIGH | string at `0x1d324f8`; registered in `sub_427AE0` |
| `--link-time-opt` | HIGH* | registered via integer literal `30622350 = 0x1d3428e`, which is `0x1d34289 + 5` = inside `cicc-lto` string at `0x1d34289` (sharing the trailing `lto` substring); registered in `sub_427AE0` line 520 |
| `--machine` | HIGH | string at `0x1d324b2`; registered in `sub_427AE0` |
| `--maxrregcount` | HIGH* | registered via integer literal `32412827 = 0x1ee949b`, which is `0x1ee9496 + 5` = inside `func-maxrregcount` string at `0x1ee9496`; also in `-maxrregcount=%d` (`0x1d33ec6`); registered in `sub_427AE0` |
| `--no-opt` | HIGH | string at `0x1d329f6` (`-no-opt`); registered in `sub_427AE0` |
| `--nv-host` | HIGH | string at `0x1eec1bd` (`--nv-host`); registered in `sub_427AE0` |
| `--nvvmpath` | HIGH | string at `0x1d3277b`; registered in `sub_427AE0` |
| `--Ofast-compile` | HIGH | strings at `0x1d32a79` (`--Ofast-compile`), `0x1d32324` (`-Ofast-compile=`), `0x1d33eec` (`--Ofast-compile=%s`), `0x1eec297..` (`=max/mid/min`); registered in `sub_427AE0` |
| `--optimize-data-layout` | HIGH | string at `0x1d329e0` (`-optimize-data-layout`); registered in `sub_427AE0` |
| `--options-file` | HIGH | string at `0x1d3293b`; registered in `sub_427AE0` |
| `--output-file` | HIGH | string at `0x1d32482`; registered in `sub_427AE0` |
| `--preserve-relocs` | HIGH | string at `0x1d32a92` (`--preserve-relocs`); registered in `sub_427AE0` |
| `--register-link-binaries` | HIGH | string at `0x1d32557`; registered in `sub_427AE0` |
| `--relocatable-link` | HIGH | string at `0x1d32863`; registered in `sub_427AE0` |
| `--report-arch` | HIGH | string at `0x1d326ee`; registered in `sub_427AE0` |
| `--reserve-null-pointer` | HIGH | direct literal in `sub_42F130` call; the value is a suffix-pointer into `dont-reserve-null-pointer` at `0x1d32583 + 5 = 0x1d32588`, so it has no independent entry in `nvlink_strings.json`; registered in `sub_427AE0` |
| `--shared` | HIGH | help text at `0x1d33ad0` (`Percolate the nvcc -shared option for nvlink's consumption`); registered in `sub_427AE0` |
| `--split-compile` | HIGH* | registered via integer literal `32424538 = 0x1eec25a`, which is `0x1eec258 + 2` = inside `--split-compile` string at `0x1eec258`; also in `-split-compile=%d` (`0x1d3229b`); registered in `sub_427AE0` |
| `--split-compile-extended` | HIGH* | registered via integer literal `30614148 = 0x1d32284`, which is `0x1d32283 + 1` = inside `-split-compile-extended` string at `0x1d32283`; also in `-split-compile-extended=%d` (`0x1d32268`); registered in `sub_427AE0` |
| `--suppress-arch-warning` | HIGH | string at `0x1d3246c`; registered in `sub_427AE0` line 69 |
| `--suppress-debug-info` | HIGH | string at `0x1d329cb` (`-suppress-debug-info`); registered in `sub_427AE0` |
| `--suppress-stack-size-warning` | HIGH | string at `0x1d32450`; registered in `sub_427AE0` line 56 |
| `--syscall-const-offset` | HIGH | string at `0x1d325db`; registered in `sub_427AE0` |
| `--time` | HIGH* | registered via integer literal `30614333 = 0x1d3233d`, which is `0x1d32334 + 9` = inside `-compile-time` string at `0x1d32334` (sharing the trailing `time` substring); registered in `sub_427AE0` |
| `--tool-name` | HIGH | string at `0x1d3291b`; registered in `sub_427AE0` |
| `--trap-into-debugger` | HIGH | string at `0x1d3294f`; short-name `_trap_` at `0x1d32948`; registered in `sub_427AE0` |
| `--uidx-file` | HIGH | direct literal in `sub_42F130` call at `sub_427AE0` line 790 |
| `--uumn` | HIGH | direct literal in `sub_42F130` call at `sub_427AE0` line 620 |
| `--use-host-info` | HIGH | string at `0x1d32a3c` (`-use-host-info`); registered in `sub_427AE0` |
| `--variables-used` | HIGH | string at `0x1d326a8`; referenced in diagnostic at `0x1d33d78`; registered in `sub_427AE0` |
| `--verbose` | HIGH | string at `0x1d3256e`; registered in `sub_427AE0` |
| `--verbose-keep` | HIGH | string at `0x1d32700`; registered in `sub_427AE0` |
| `--verbose-tkinfo` | HIGH | string at `0x1d32744`; registered in `sub_427AE0` |
| `--version` | HIGH | direct literal in `sub_42F130` call; registered in `sub_427AE0` |
| `--warning-as-error` | HIGH | string at `0x1d32625`; registered in `sub_427AE0` |
| `--Xnvvm` | HIGH | string at `0x1d327c6`; registered in `sub_427AE0` |
| `--Xptxas` | HIGH | string at `0x1d327bf`; registered in `sub_427AE0`; also embedded in diagnostic at `0x1d39b00` |

**Shared with ptxas/cicc.** Flags directly shared with the standalone `ptxas` tool (per the [ptxas CLI Options](../../ptxas/config/cli-options.html) page): `--maxrregcount`, `--Ofast-compile`, `--split-compile`, `--device-stack-protector`, `--device-stack-protector-frame-size-threshold`, `--cuda-api-version`, `--nv-host`, `--tool-name`, `--trap-into-debugger`, `--help`, `--version`, `--options-file`, `--verbose`, `--machine`, `--warning-as-error`, `--disable-warnings`, `--preserve-relocs`, `--force-rela`, `--suppress-debug-info`, `--suppress-stack-size-warning`, `--disable-smem-reservation`, `--enable-extended-smem`, `--reserve-null-pointer`, `--dont-reserve-null-pointer`, `--verbose-tkinfo`, `--fdcmpt`, `--uumn`, `--debug` (`-g`). These flow directly from the nvlink CLI into the embedded ptxas via `-Xptxas` forwarding (see [LTO Option Forwarding](../lto/option-forwarding.md)). The [cicc wiki](../../cicc/config/cli-flags.html) documents a different option surface (`-opt`, `-lnk`, `-llc`) that does not overlap meaningfully with nvlink's flags.
