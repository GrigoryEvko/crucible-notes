# Embedded ptxas Options

nvlink v13.0.88 embeds a complete copy of the ptxas assembler/compiler backend. When that backend is invoked (for LTO compilation, PTX JIT, or Mercury finalization), it accepts its own independent set of command-line options separate from nvlink's own CLI flags. Two functions define this option set: `sub_1103030` builds the option definition table (long/short names, types, defaults, help text), and `sub_1104950` parses argv against that table and populates a compiler-state structure at known offsets.

| | |
|---|---|
| **Option definition builder** | `sub_1103030` at `0x1103030` (29,803 bytes / 1,249 lines) |
| **Option parser / extractor** | `sub_1104950` at `0x1104950` (37,578 bytes / 1,208 lines) |
| **Compilation driver (consumer)** | `sub_1112F30` at `0x1112F30` (65,018 bytes / 2,164 lines) |
| **Feature flag configurator** | `sub_1100E50` at `0x1100E50` (13,759 bytes / 451 lines) |
| **Shared parser infrastructure** | `sub_42F130` (register), `sub_42E390` (extract), `sub_42E580` (was-specified) |
| **Version string** | `"Cuda compilation tools, release 13.0, V13.0.88"` |

## How Options Reach the Embedded ptxas

The embedded ptxas backend never reads directly from the user's command line. Options flow through two paths:

1. **`-Xptxas` passthrough.** nvlink's own CLI parser (`sub_427AE0`) accumulates `-Xptxas` values into a linked list at `qword_2A5F238`. The LTO pipeline's `sub_429BA0` serializes this list into a space-separated string that becomes part of the argv vector passed to the embedded ptxas entry point.

2. **Programmatic forwarding.** `sub_429BA0` also appends options derived from nvlink's own state: `-arch=sm_N` from the target architecture, `-maxreg=N` from `--maxrregcount`, `-Ofast-compile=LEVEL`, `-g` from `--debug`, `-generate-line-info`, and several others. See [Option Forwarding to cicc](../lto/option-forwarding.md) for the complete forwarding logic.

The combined argv vector is passed to `sub_1104950`, which calls `sub_1103030` to build the option table, then parses the vector using the shared `sub_42E5A0` parser. Parsed values are extracted with `sub_42E390` into a compiler-state structure (base pointer `a3`), where each option writes to a specific byte offset.

## Option Definition Table (`sub_1103030`)

`sub_1103030` creates a fresh option parser instance via `sub_42DFE0(0)` and registers all options via `sub_42F130`. Each `sub_42F130` call passes:

```
sub_42F130(parser, long_name, short_name, type, multiplicity, flags, context,
           allowed_keywords, reserved, default_value, reserved2,
           value_placeholder, help_text)
```

Option types: 0 = file-list, 1 = bool, 2 = string, 4 = integer, 5 = int64, 7 = special.
Multiplicity: 0 = flag-only (bool), 1 = single value, 2 = multi-value (accumulates), 3 = multi-value with keyword validation.

After registering all options, `sub_1103030` invokes `sub_42E5A0` to parse the passed argc/argv against the table, then checks for `--trap-into-debugger` (calls `sub_42FA60` to install signal handlers), `--tool-name` (updates the internal program name), `--help` (prints help and exits), and `--version` (prints version banner and exits). The version banner reads:

```
ptxas: NVIDIA (R) Ptx optimizing assembler
Copyright (c) 2005-2025 NVIDIA Corporation
Built on Wed_Aug_20_01:55:12_PM_PDT_2025
Cuda compilation tools, release 13.0, V13.0.88
Build cuda_13.0.r13.0/compiler.36424714_0
```

## Complete Option Catalog

The following catalog lists every option registered by `sub_1103030`, in registration order. The "State Offset" column shows where `sub_1104950` stores the parsed value within the compiler-state structure (base pointer `a3`). The "Size" column indicates the `sub_42E390` copy size.

### Debug and Diagnostic Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `device-debug` | `g` | bool | `""` | `""` | `a3+288` | 1 | Generate debug information for device code |
| `suppress-debug-info` | `suppress-debug-info` | bool | `""` | `""` | (via ID) | 1 | Do not generate debug info sections in final output. Ignored without `--device-debug` or `--generate-line-info` |
| `generate-line-info` | `lineinfo` | bool | `""` | `""` | `a3+170` | 1 | Generate line-number information for device code |
| `sp-bounds-check` | `sp-bounds-check` | bool | -- | -- | `a3+292` | 1 | Generate stack-pointer bounds-checking code. Enabled automatically with `-g` or `-O0` |
| `device-stack-protector` | `device-stack-protector` | bool | `false` | `<true\|false>` | `a3+345` | 1 | Enable stack canaries. Compiler uses heuristics to assess per-function risk |
| `device-stack-protector-frame-size-threshold` | `device-stack-protector-size` | int | `16` | `<N>` | `a3+348` | 4 | Stack frame size threshold for canary insertion. 0 = instrument all frames |
| `debug-info` | `debug-info` | string | `""` | `<String>` | (via ID) | 8 | File path for DWARF information output |
| `link-info` | `link-info` | string | `""` | `<String>` | `a3+176` | 8 | File path for imported/exported symbol names |
| `verbose-tkinfo` | `verbose-tkinfo` | bool | `false` | `<true\|false>` | `a3+612` | 1 | Emit object name and command-line in tkinfo section. Auto-enabled with `-g` |
| `g-tensor-memory-access-check` | `g-tmem-access-check` | bool | -- | -- | `a3+645` | 1 | Enable tensor memory access checks for tcgen05 ops. Default with `-g` |
| `gno-tensor-memory-access-check` | `gno-tmem-access-check` | bool | -- | -- | `a3+646` | 1 | Disable tensor memory access checks. Overrides `g-tensor-memory-access-check` |
| `compiler-annotations` | `annotate` | bool | -- | -- | `a3+647` | 1 | Annotate compiler-internal information in binary output |

### Optimization Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `opt-level` | `O` | int | `3` | `<N>` | `a3+148` | 4 | Optimization level (0-3) |
| `Ofast-compile` | `Ofc` | string | `0` | `<0\|min\|mid\|max>` | `a3+152` | 8 | Fast-compile level. `max` = fastest compile, `mid` = balanced, `min` = minimal impact, `0` = disabled |
| `register-usage-level` | `regUsageLevel` | int | `5` | `<0..10>` | `a3+160` | 4 | Aggressiveness of register-usage optimizations. Higher = more regs for better code |
| `fast-compile` | `fc` | bool | -- | -- | `a3+477` | 1 | EXPERIMENTAL: optimize compilation time at runtime performance cost |
| `allow-expensive-optimizations` | `allow-expensive-optimizations` | bool | -- | `<true\|false>` | `a3+408` | 1 | Allow compiler to use maximum resources. Default: enabled for `-O2` and above |
| `fmad` | `fmad` | bool | `true` | `<true\|false>` | (via ID) | 1 | Enable contraction of FP multiply+add into FMAD/FFMA/DFMA |
| `fastimul` | `fastimul` | bool | `""` | `""` | `a3+168` | 1 | Enable 24-bit integer multiplication |
| `limit-fold-fp` | `limit-fold-fp` | bool | `false` | `<true\|false>` | `a3+340` | 1 | Enable/disable constant folding of float operations |
| `optimize-float-atomics` | `opt-fp-atomics` | bool | -- | -- | `a3+341` | 1 | Enable FP atomic optimizations that may affect precision |
| `opt-pointers` | `Op` | bool | -- | -- | `a3+322` | 1 | Optimize 64-bit pointers by truncating to 32-bit |
| `cloning` | `cloning` | string | `yes` | `<yes\|no>` | (via `v138`) | 8 | Enable/disable cloning of device functions |
| `noFwdPrg` | `noFwdPrg` | bool | -- | -- | `a3+568` | 1 | Disable forward progress optimization (internal) |
| `cimm` | `cimm` | bool | -- | -- | `a3+232` | 1 | Use immediate values for literal constants |
| `disable-optimizer-constants` | `disable-optimizer-consts` | bool | -- | -- | `a3+232` | 1 | Disable use of optimizer constant bank. Shares offset with `cimm` |
| `no-fastreg` | `no-fastreg` | bool | -- | -- | `a3+233` | 1 | Disable fast register allocation |

### Register and Launch Configuration

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `maxrregcount` | `maxrregcount` | string | -- | `<archmax/archmin/N>` | (via `nptr`) | 8 | Maximum registers per GPU function. Accepts `archmax`, `archmin`, or integer N. Values below ABI minimum are bumped |
| `device-function-maxrregcount` | `func-maxrregcount` | string | -- | `<archmax/archmin/N>` | (via `v140`) | 8 | Per-device-function register limit. Only effective with `--compile-only`. Overrides `maxrregcount` for device functions |
| `minnctapersm` | `minnctapersm` | int | -- | `<N>` | `a3+492` | 4 | Minimum CTAs per SM. Ignored if `--maxrregcount` is used |
| `maxntid` | `maxntid` | string | -- | `<Comma separated list>` | (via `v141`) | 8 | Maximum thread-block dimensions (up to 3 comma-separated values). Ignored if `--maxrregcount` is used |
| `override-directive-values` | `override-directive-values` | bool | -- | -- | `a3+496` | 1 | CLI values override PTX directives for `minnctapersm`, `maxntid`, `maxrregcount` |

### Compilation Mode Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `compile-only` | `c` | bool | -- | -- | `a3+342` | 1 | Generate relocatable object (separate compilation) |
| `compile-as-tools-patch` | `astoolspatch` | bool | -- | -- | `a3+343` | 1 | Compile patch code for CUDA tools. Forces `maxrregcount` to ABI minimum |
| `extensible-whole-program` | `ewp` | bool | -- | -- | `a3+505` | 1 | Extensible whole-program compilation mode |
| `compile-functions` | `f` | string | -- | `<Comma separated list>` | (via list) | 8 | Compile only the named function(s) |
| `entry` | `e` | string | -- | `<entry function>` | (via list) | 8 | Entry function name |
| `slr` | `slr` | bool | -- | -- | `a3+353` | 1 | (Internal flag) |
| `abi-compile` | `abi` | string | `yes` | `<yes>` | (via `v137`) | 8 | Enable ABI-compliant function compilation |

### Target Architecture

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `gpu-name` | `arch` | string | (dynamic) | `<gpu name>` | `a3+192` | 8 | Target GPU. `.target sm_XY` compiles to `sm_MN` where `MN >= XY`. Also accepts virtual architectures (parsing only) |
| `machine` | `m` | int | `64` | `<bits>` | `a3+280` | 4 | Host architecture bits. Only 64-bit supported |

### Cache Policy Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `def-load-cache` | `dlcm` | string | `""` | -- | `a3+304` (processed) | 4 | Default cache modifier on global/generic load |
| `force-load-cache` | `flcm` | string | `""` | -- | `a3+312` (processed) | 4 | Force cache modifier on global/generic load |
| `def-store-cache` | `dscm` | string | `""` | -- | `a3+308` (processed) | 4 | Default cache modifier on global/generic store |
| `force-store-cache` | `fscm` | string | `""` | -- | `a3+316` (processed) | 4 | Force cache modifier on global/generic store |

Cache string values are converted to integer codes by `sub_1102260`. Specifying both `force-load-cache` and `def-load-cache` simultaneously is an error -- force takes precedence and def is zeroed. Same for the store pair.

### Warning and Diagnostic Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `warn-on-local-memory-usage` | `warn-lmem-usage` | bool | -- | -- | `a3+472` | 1 | Warn if local memory is used |
| `warn-on-spills` | `warn-spills` | bool | -- | -- | `a3+473` | 1 | Warn if registers are spilled to local memory |
| `warn-on-double-precision-use` | `warn-double-usage` | bool | -- | -- | `a3+474` | 1 | Warn if double-precision instructions are used |
| `suppress-double-demote-warning` | `suppress-double-demote-warning` | bool | -- | -- | `a3+321` | 1 | Suppress warning when double-precision appears on non-DP-capable SM |
| `suppress-stack-size-warning` | `suppress-stack-size-warning` | bool | -- | -- | `a3+504` | 1 | Suppress warning when stack size cannot be determined |
| `suppress-async-bulk-multicast-advisory-warning` | (same) | bool | -- | -- | `a3+614` | 1 | Suppress advisory for `.multicast::cluster` |
| `suppress-sparse-mma-advisory-info` | (same) | bool | -- | -- | `a3+615` | 1 | Suppress advisory info for `mma.sp` |
| `warning-as-error` | `Werror` | bool | -- | -- | `a3+323` | 1 | Treat all warnings as errors |
| `disable-warnings` | `w` | bool | -- | -- | `a3+324` | 1 | Inhibit all warning messages |

### Profiling and Statistics

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `compiler-stats` | `compilerStats` | string | `""` | `<String>` | `a3+72` | 8 | Print compiler statistics. Values: `time/t`, `memory/m`, `phase-wise/p`, `detailed/d` |
| `compiler-stats-file` | `compilerStatsFile` | string | -- | `<String>` | `a3+80` | 8 | File for `--compiler-stats` output. Requires `--compiler-stats` |
| `fdevice-time-trace` | `timeTraceFile` | string | -- | `<String>` | `a3+88` | 8 | Input trace JSON file for Chrome trace format output |
| `use-trace-pid` | `use-trace-pid` | int64 | -- | `<N>` | `a3+96` | 8 | PID for flamechart generation. Requires `--fdevice-time-trace` |
| `ftrace-phase-after` | `ftracePhaseAfter` | string | -- | `<String>` | `a3+104` | 8 | Phase name for ftrace when ptxas invoked as library |
| `verbose` | `v` | bool | -- | -- | `a3+18` | 1 | Print code generation statistics |
| `profile-options` | `po` | string | `""` | `""` | `a3+296` | 8 | Profile-specific options |

### Code Generation Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `dont-merge-basicblocks` | `no-bb-merge` | bool | -- | -- | `a3+16` | 1 | Prevent basic block merging. Useful for debuggable code at slight performance cost |
| `return-at-end` | `ret-end` | bool | -- | -- | `a3+17` | 1 | Preserve final return instruction. Needed for breakpoints at function end |
| `disable-smem-reservation` | `disable-smem-reservation` | bool | `false` | `<true\|false>` | `a3+234` | 1 | Disable shared memory reservation. Arch-gated: rejected for SM < sm_100 |
| `force-rela` | `force-rela` | bool | -- | -- | `a3+569` | 1 | Force RELA relocations instead of REL |
| `position-independent-code` | `pic` | bool | `false` | `<true\|false>` | `a3+606` | 1 | Generate PIC. Enabled by default for whole-program compilation |
| `preserve-relocs` | `preserve-relocs` | bool | -- | -- | `a3+464` | 1 | Generate relocatable variable references and preserve relocations in linked executable |
| `force-externals` | `fext` | bool | -- | -- | (via ID) | 1 | Generate device shadow variables as externals instead of statics (debug flow) |
| `make-errors-visible-at-exit` | (same) | bool | -- | -- | `a3+344` | 1 | Generate instructions at exit to make memory faults visible |
| `set-texmode-raw` | `set-texmode-raw` | bool | `false` | `<true\|false>` | `a3+599` | 1 | Set texture mode to raw (internal) |
| `assume-extern-functions-do-not-sync` | (same) | bool | `true` | `<true\|false>` | `a3+576` | 1 | Assume extern functions do not synchronize. Rejected for SM < sm_90 |

### Synchronization and Warp Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `legacy-bar-warp-wide-behavior` | (same) | bool | -- | -- | `a3+402` | 1 | Retain legacy behavior where any thread executing `bar` counts as whole warp. Ignored for `sm_70`+. Deprecated |
| `no-membermask-overlap` | (same) | bool | `false` | `<true\|false>` | `a3+642` | 1 | Assert no sync instruction uses different overlapping masks |
| `membermask-overlap` | (same) | bool | `true` | `<true\|false>` | `a3+643` | 1 | Assert sync instructions may use overlapping masks |
| `print-potentially-overlapping-membermasks` | (same) | bool | -- | -- | `a3+644` | 1 | Print locations of sync instructions with potentially overlapping masks. SM70-75 only |
| `blocks-are-clusters` | (same) | bool | `false` | `<true\|false>` | `a3+665` | 1 | Treat thread blocks as clusters. Rejected for SM < sm_100 |

### Sanitizer Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `sanitize` | `sanitize` | string | -- | `<string>` | `a3+648` | 8 | Instrumentation sanitizer. Allowed: `memcheck`, `threadsteer`. Incompatible with `--compile-as-tools-patch` |

### Concurrency Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `split-compile` | (same) | int | -- | `<N>` | `a3+284` | 4 | Max concurrent threads for compiler optimizations. 0 = CPU count, 1 = ignored |
| `jobserver` | `jobserver` | bool | -- | -- | `a3+609` | 1 | Enable GNU Jobserver support |

### PTX Input Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `input-as-string` | `ias` | string | -- | `<ptx string>` | (via list) | 8 | Pass PTX as string instead of file. For runtime support or avoiding filesystem |
| `okey` | `ok` | special | -- | -- | `a3+528` | 4 | Deobfuscation key for obfuscated PTX input |
| `ptx-length` | `ptxlen` | special | -- | -- | `a3+536` | 4 | Length of obfuscated PTX string. Requires `--okey` and vice versa |
| `key` | `k` | string | `key` | `<string>` | (via list) | 8 | Hash value representing compiled device code |

### Output Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `output-file` | `o` | string | `elf.o` | `<file>` | `a3+48` | 8 | Output file path |
| `list-version` | `version-ls` | bool | -- | -- | -- | -- | Print supported PTX ISA versions and exit |

### Video and Memory Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `disable-fast-video-emulation` | (same) | bool | `false` | `<true\|false>` | `a3+613` | 1 | Disable fast video emulation |
| `enable-extended-smem` | (same) | bool | `false` | `<true\|false>` | `a3+666` | 1 | Enable extended shared memory (internal) |
| `reserve-null-pointer` | (same) | bool | -- | -- | (via `v126`) | 1 | Reserve address 0 as nil pointer |
| `dont-reserve-null-pointer` | (same) | bool | -- | -- | (via `v127`) | 1 | Do not reserve address 0. Overrides `reserve-null-pointer` regardless of order |

### CUDA API Options

| Long Name | Short | Type | Default | Placeholder | State Offset | Size | Description |
|---|---|---|---|---|---|---|---|
| `cuda-api-version` | (same) | string | -- | `<major>.<minor>` | `a3+580`, `a3+584` | 4+4 | CUDA API version. Parsed as `%u.%u`, stored as major/minor at separate offsets |
| `nv-host` | (same) | string | -- | `<file>` | `a3+672` | 8 | Path to `nv.host` file. Incompatible with `--extensible-whole-program` and `--compile-only` |

### Workaround Flags (Bug-Fix Switches)

These are numbered software-workaround flags corresponding to internal NVIDIA bug IDs. They allow targeted behavior changes without modifying the broader optimization pipeline.

| Long Name | Short | Default | State Offset | Size | Arch Gate |
|---|---|---|---|---|---|
| `sw2614554` | `sw2614554` | `false` | `a3+235` | 1 | -- |
| `sw2837879` | `sw2837879` | `false` | `a3+236` | 1 | -- |
| `sw1729687` | `sw1729687` | `false` | `a3+498` | 1 | SM 87-89 only (`v23` in range 14-16) |
| `sw200428197` | `sw200428197` | `false` | `a3+500` | 1 | SM > 75 only (`v23 > 18`) |
| `sw200387803` | `sw200387803` | `false` | `a3+501` | 1 | -- (info-level diagnostic) |
| `sw200764156` | `sw200764156` | `true` | `a3+502` | 1 | SM 89 only (`v23 == 24`) |
| `sw4575628` | `sw4575628` | `false` | `a3+237` | 1 | SM >= sm_100 only (`v23 > 26`) |
| `sw4915215` | `sw4915215` | `false` | `a3+664` | 1 | SM family 100 only. Rejected for virtual archs |
| `sw4936628` | `sw4936628` | `false` | `a3+503` | 1 | -- |
| `cuda32f3056bbb` | `cudasw32f3056bbb` | `false` | `a3+667` | 1 | -- |

### Utility Options

| Long Name | Short | Type | Default | Description |
|---|---|---|---|---|
| `help` | `h` | bool | -- | Print help and exit |
| `version` | `V` | bool | -- | Print version and exit |
| `options-file` | `optf` | file-list | -- | Include options from specified file |
| `tool-name` | `tool-name` | string | -- | Change internal tool name |
| `trap-into-debugger` | `_trap_` | bool | -- | Install signal handlers for assertion/crash traps |
| `uumn` | `uumn` | bool | -- | (Internal undocumented flag) |
| `fdcmpt` | `fdcmpt` | bool | -- | (Internal compatibility flag, gated by SM and `uumn`) |

## Option Extraction and Validation (`sub_1104950`)

After `sub_1103030` parses the argv, `sub_1104950` calls `sub_42E390` for each option to copy the parsed value into the compiler-state structure at `a3`. The extraction follows a strict order and includes validation logic:

### Dependency Validation

`sub_1104950` enforces mutual exclusion and dependency rules between options. These checks use `sub_42E580` (was-specified) to distinguish "explicitly set" from "has default value":

| Rule | Diagnostic |
|---|---|
| `--compiler-stats-file` without `--compiler-stats` | Warning; `a3+80` zeroed |
| `--use-trace-pid` without `--fdevice-time-trace` | Warning; `a3+96` zeroed |
| `--ftrace-phase-after` without `--fdevice-time-trace` | Warning; `a3+104` zeroed |
| `--register-usage-level` > 10 | Warning; reset to 5 |
| `--register-usage-level` with `-O0` | Warning; reset to 5 |
| `--compile-only` + `--fast-compile` | Warning; `fast-compile` disabled |
| `--compile-only` + `--extensible-whole-program` | Warning; `ewp` disabled |
| `--nv-host` + `--extensible-whole-program` or `--compile-only` | Warning; `nv-host` zeroed |
| `--device-debug` + `--generate-line-info` | Warning; `lineinfo` disabled, `sw2614554` cleared |
| `force-load-cache` + `def-load-cache` | Warning; `def-load-cache` zeroed |
| `force-store-cache` + `def-store-cache` | Warning; `def-store-cache` zeroed |
| `--compile-as-tools-patch` + `--extensible-whole-program` | Warning; `ewp` disabled |
| `--compile-as-tools-patch` + `--compile-only` | Warning; `compile-only` disabled |
| `--compile-as-tools-patch` + `--fast-compile` | Warning; `fast-compile` disabled |
| `--sanitize` + `--compile-as-tools-patch` | Warning; `sanitize` zeroed |
| `device-function-maxrregcount` in whole-program mode | Ignored with warning |
| `--okey` without `--ptx-length` | Error |
| `--ptx-length` without `--okey` | Error (unused flag warning) |
| `--blocks-are-clusters` for SM < sm_100 | Warning; flag zeroed |
| `--disable-smem-reservation` for SM < sm_100 | Warning; flag zeroed |
| `--assume-extern-functions-do-not-sync` for SM < sm_90 | Warning; flag zeroed |
| `--print-potentially-overlapping-membermasks` for SM > sm_75 | Warning; flag zeroed |
| `--preserve-relocs` for SM >= sm_100 with smem-reservation | Info diagnostic; flag zeroed |

### Ofast-Compile Interaction

The Ofast-compile level has cascading effects on other options. `sub_1104950` validates the level string (`max`, `mid`, `min`, `0`) and enforces:

| Level | Effect on `-O` | Effect on `cloning` | Effect on `sw2614554` | Other |
|---|---|---|---|---|
| `max` | Forces `-O0` | Forced off | Forced on | -- |
| `mid` | Forces `-O1` | Forced off | Forced on if no `split-compile` | `a3+572` = 1 |
| `min` | Forces `-O1` | Forced off | Forced on if no `split-compile` | `a3+572` = 1; `allow-expensive-optimizations` forced on |
| `0` | No change | No change | No change | -- |

### maxrregcount Resolution

The `maxrregcount` value goes through a multi-step resolution in `sub_1104950`:

1. If the string is `"archmax"`, use `*(arch_profile + 96)` -- the architecture's maximum register count.
2. If the string is `"archmin"`, use `*(arch_profile + 100)` -- the architecture's minimum register count.
3. Otherwise, parse as integer via `strtoll`.
4. If `--compile-as-tools-patch` is active and the value exceeds the tools-patch minimum (`8 * (SM >= 17) + 16`), clamp and warn.
5. If the integer exceeds the architecture maximum, clamp to `*(arch_profile + 96)` and warn.
6. If the integer is below the ABI minimum for the profile, bump up and warn.
7. Store the final value at `a3+136`.

The same resolution applies to `device-function-maxrregcount`, stored at `a3+140`.

### SM Version Derivation

`sub_1104950` calls `sub_15C3DD0` to convert the `gpu-name` string (e.g. `"sm_90"`) to an internal numeric version (`v23`). This integer is used throughout for architecture-gated validation:

| `v23` Range | Architecture | Notes |
|---|---|---|
| 7-10 | SM 50-70 (Maxwell-Volta) | Limited feature support |
| 11-16 | SM 75-89 (Turing-Ada) | `sw1729687` range: 14-16 |
| 17-20 | SM 90-90a (Hopper) | ABI mode threshold |
| 21-26 | SM 100-103 (Blackwell) | smem-reservation enabled |
| 27+ | SM 110+ (future) | Full feature set |

The family lookup table at `dword_1EED2E0` maps `(v23 - 7)` to an architecture family code (e.g. 100 for Blackwell). This family code gates `sw4915215` (only family 100) and `disable-smem-reservation` behavior.

## Compiler-State Structure Layout

The compiler-state structure populated by `sub_1104950` is at least 680 bytes, accessed through base pointer `a3`. Key regions:

| Offset Range | Contents |
|---|---|
| `0-8` | Parsed argv (post-processing) |
| `16-17` | `dont-merge-basicblocks`, `return-at-end` |
| `18-19` | `verbose`, virtual-arch flag |
| `24-40` | Input file lists (PTX files, string inputs) |
| `48-56` | Output file path |
| `72-112` | Profiling: compiler-stats, time-trace, trace-pid, ftrace-phase |
| `136-144` | maxrregcount (entry), maxrregcount (device-function) |
| `148-168` | opt-level, Ofast-compile string, register-usage-level, machine, fastimul |
| `170-176` | lineinfo, link-info |
| `192-200` | gpu-name string pointer |
| `232-237` | cimm/disable-opt-consts, no-fastreg, disable-smem-reservation, sw2614554/2837879/4575628 |
| `280-296` | machine bits, split-compile, device-debug, lineinfo, sp-bounds-check, profile-options |
| `304-324` | Cache policies (load/store def/force), suppress-double-demote, opt-pointers, warning-as-error, disable-warnings |
| `326-358` | ABI mode flags, compile-only, compile-as-tools-patch, make-errors-visible, device-stack-protector, slr, various mode flags |
| `340-344` | limit-fold-fp, optimize-float-atomics, compile-only, compile-as-tools-patch, make-errors-visible-at-exit |
| `402-409` | legacy-bar, allow-expensive-optimizations, reserve-null-pointer |
| `440-504` | Profile path, entry list, compile-functions list, preserve-relocs, warn-lmem/spills/double, fast-compile, maxntid dims, minnctapersm, override-directive-values, sw1729687-sw200764156, sw4936628, suppress-stack-size-warning, extensible-whole-program |
| `505-576` | extensible-whole-program, uumn, noFwdPrg, force-rela, assume-extern-functions-do-not-sync |
| `580-612` | CUDA API version major/minor, smem-reservation flags, PIC, disable-fast-video-emulation, suppress-async/sparse warnings, query-controls, apply-controls, verbose-tkinfo |
| `613-680` | disable-fast-video-emulation, advisory suppressions, query/apply-controls, membermask flags, g/gno-tensor-memory-access-check, compiler-annotations, sanitize, blocks-are-clusters, enable-extended-smem, cuda32f3056bbb, sw4915215, nv-host |

## Interaction with the Compilation Driver

`sub_1112F30` (the main compilation driver) reads options from the compiler-state structure populated by `sub_1104950`. Key interactions:

- **Cache policies** at offsets 304-316 configure the codegen's default and forced load/store cache modifiers. These flow into `sub_110AA30` (per-function codegen init) which reads them to set up instruction-level cache annotations.
- **`compile-as-tools-patch`** (offset 343) triggers a special mode where textures, surfaces, samplers, and constants are handled differently. `maxrregcount` is clamped to ABI minimum.
- **`position-independent-code`** (offset 606) enables PIC code generation. The driver reads this along with `compile-only` (342) and `extensible-whole-program` (505) to determine the compilation model.
- **`device-debug`** (offset 288) causes `sub_1102070` to be called, which configures debug-mode codegen. When `device-debug` is set, `verbose-tkinfo` is auto-enabled.
- **`opt-level`** 0 at offset 148 forces `sp-bounds-check` on and `sw2614554` off, overriding explicit settings.
- **`split-compile`** (offset 284) configures the thread pool created by `sub_464AE0` for concurrent per-function compilation.

## Feature Flag Mapping (`sub_1100E50`)

After option parsing, `sub_1100E50` translates selected options into a feature-flag bitfield consumed by the codegen backend. It reads the SM version and option bytes from the compiler-state structure, then calls `sub_16E3AA0(feature_table, feature_id, bool_value)` for approximately 30 feature flags:

| Feature ID | Source | Condition |
|---|---|---|
| 3 | SM version | SM > 7 (sm_60+) |
| 4 | Option flag 989 | Explicit option |
| 5 | SM version | SM > 10 (sm_75+) |
| 7 | Option flag 739 | Explicit option |
| 8 | Option flag 944 | Explicit option |
| 9 | `extensible-whole-program` (889) | Explicit option |
| 10 | Option flag 784 | Explicit option |
| 18 | Option flag 997 | Explicit option |
| 19 | `compile-as-tools-patch` (727) | Explicit option |
| 20 | Option flag 1026 | Explicit option |
| 21 | Option flag 1027 | Explicit option |
| 33 | SM version + debug | SM == 29 or 30 (sm_89/90) and not flag 618 |

Feature names are retrieved through `sub_12B5EF0` and stored as `"true"`/`"false"` key-value pairs via `sub_448E70` for diagnostic purposes.

## Cross-References

- [CLI Option Parsing](../pipeline/cli-options.md) -- nvlink's own option parser, which accumulates `-Xptxas` values
- [Option Forwarding to cicc](../lto/option-forwarding.md) -- how nvlink builds the ptxas argv from its own state
- [Embedded ptxas: Architecture Overview](../ptxas/overview.md) -- the full embedded ptxas address map
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- SM version dispatch and architecture profiles
- [Register Allocation](../ptxas/register-allocation.md) -- how `maxrregcount` affects the register allocator

## Confidence Assessment

### Aspect-Level Confidence

| Aspect | Confidence | Basis |
|---|---|---|
| Builder function address (`0x1103030`) | HIGH | File `decompiled/sub_1103030_0x1103030.c` exists (1,249 lines); 108 `sub_42F130` registration calls enumerated |
| Parser function address (`0x1104950`) | HIGH | File `decompiled/sub_1104950_0x1104950.c` exists (1,208 lines); 138 `sub_42E390`/`sub_42E580` extraction calls verified |
| Compilation driver (`0x1112F30`) | HIGH | File `decompiled/sub_1112F30_0x1112f30.c` exists |
| Feature flag configurator (`0x1100E50`) | HIGH | File `decompiled/sub_1100E50_0x1100e50.c` exists |
| State offsets (`a3+N`) | HIGH | Each offset read directly from `sub_42E390` calls in decompiled `sub_1104950`; byte-level accuracy |
| Type codes, multiplicity, defaults | HIGH | Read from `sub_42F130` positional parameters in decompiled `sub_1103030` |
| Option count (108 options) | HIGH | Exhaustive enumeration of `sub_42F130` calls in `sub_1103030` (excluding options registered only in PTX-frontend context) |
| Dependency validation rules | HIGH | Each rule corresponds to a specific `if`-block in `sub_1104950` with `sub_42E580` (was-specified) checks |
| Ofast-compile cascading effects | HIGH | Control flow in `sub_1104950` explicitly forces `-O0`/`-O1` and cloning off based on string comparison |
| SM version derivation table | MEDIUM | `v23` range-to-architecture mapping inferred from conditional branches; exact SM-to-v23 mapping not exhaustively enumerated |
| Feature flag mapping (`sub_1100E50`) | MEDIUM | Feature IDs and source conditions read from decompiled code; feature *names* not directly visible (retrieved via `sub_12B5EF0` at runtime) |

### Per-Option Verification

Each row below records a direct `sub_42F130(..., "<long-name>", ...)` call in `decompiled/sub_1103030_0x1103030.c`. The "Evidence" column identifies where the long-name string lives in `nvlink_strings.json` or, when the name only appears as a fragment of a longer help/format string, indicates `substring`. Three options (`uumn`, `cimm`, `slr`) are direct literals in the decompiler output but have no visible standalone entry in the string-table dump -- the backing storage is deduped/overlapped in `.rodata` but the C-string MUST exist to satisfy the call. All 108 options are additionally verified via their extraction sites in `sub_1104950` (`sub_42E390` calls writing to `a3+N`). The "Shared" column marks options that also appear in the [ptxas CLI Options](../../ptxas/config/cli-options.html) page (the standalone ptxas tool), indicating the option surface is shared between the embedded and standalone ptxas binaries.

| Option | Confidence | Evidence | Shared w/ ptxas |
|---|---|---|---|
| `suppress-stack-size-warning` | HIGH | string at `0x1d32450` | yes |
| `key` | HIGH | substring in help/format strings | no |
| `okey` | HIGH | substring in help/format strings | no |
| `ptx-length` | HIGH | substring in help/format strings | yes |
| `entry` | HIGH | substring in help/format strings | yes |
| `compile-functions` | HIGH | string at `0x1ee929f` | yes |
| `input-as-string` | HIGH | substring in help/format strings | yes |
| `verbose` | HIGH | string at `0x1d3256e` | yes |
| `list-version` | HIGH | string at `0x1ee92c9` | yes |
| `uumn` | HIGH | direct literal in `sub_1103030` line 130; no standalone string entry | no |
| `warn-on-local-memory-usage` | HIGH | string at `0x1ee92e6` | yes |
| `warn-on-spills` | HIGH | string at `0x1ee930d` | yes |
| `warn-on-double-precision-use` | HIGH | string at `0x1ee932e` | yes |
| `compiler-stats` | HIGH | string at `0x1ee9359` | yes |
| `compiler-stats-file` | HIGH | string at `0x1ee9383` | yes |
| `fdevice-time-trace` | HIGH | string at `0x1ee93a5` | yes |
| `use-trace-pid` | HIGH | string at `0x1ee93b8` | yes |
| `ftrace-phase-after` | HIGH | string at `0x1ee93d7` | yes |
| `dont-merge-basicblocks` | HIGH | string at `0x1ee93f6` | yes |
| `return-at-end` | HIGH | string at `0x1ee9415` | yes |
| `cimm` | HIGH | direct literal in `sub_1103030` line 277; no standalone string entry | no |
| `disable-optimizer-constants` | HIGH | string at `0x1ee9441` | yes |
| `no-fastreg` | HIGH | string at `0x1ee945d` | yes |
| `disable-smem-reservation` | HIGH | string at `0x1d3259d` | yes |
| `maxrregcount` | HIGH | substring in help/format strings (embedded in `-maxrregcount=%d` at `0x1d33ec6` and `func-maxrregcount` at `0x1ee9496`) | yes |
| `minnctapersm` | HIGH | substring in help/format strings | yes |
| `maxntid` | HIGH | substring in help/format strings | yes |
| `override-directive-values` | HIGH | string at `0x1ee947c` | yes |
| `device-function-maxrregcount` | HIGH | string at `0x1ee94a8` | yes |
| `register-usage-level` | HIGH | string at `0x1ee912b` | yes |
| `device-debug` | HIGH | substring in help/format strings | yes |
| `suppress-debug-info` | HIGH | substring in help/format strings | yes |
| `generate-line-info` | HIGH | substring in help/format strings | yes |
| `sp-bounds-check` | HIGH | string at `0x1ee94d3` | yes |
| `device-stack-protector` | HIGH | string at `0x1d32891` | yes |
| `device-stack-protector-frame-size-threshold` | HIGH | string at `0x1d33b68` | yes |
| `debug-info` | HIGH | substring in help/format strings | no |
| `link-info` | HIGH | string at `0x1ee94ff` | yes |
| `opt-level` | HIGH | substring in help/format strings | yes |
| `Ofast-compile` | HIGH | substring in help/format strings | yes |
| `fastimul` | HIGH | string at `0x1ee9509` | no |
| `output-file` | HIGH | string at `0x1d32482` | yes |
| `gpu-name` | HIGH | string at `0x1ee9534` | yes |
| `suppress-double-demote-warning` | HIGH | string at `0x1eeb108` | yes |
| `force-externals` | HIGH | string at `0x1ee954d` | yes |
| `profile-options` | HIGH | string at `0x1ee955d` | yes |
| `abi-compile` | HIGH | string at `0x1ee958a` | yes |
| `def-load-cache` | HIGH | string at `0x1ee95a1` | yes |
| `def-store-cache` | HIGH | string at `0x1ee95b5` | yes |
| `force-load-cache` | HIGH | string at `0x1ee95ca` | yes |
| `force-store-cache` | HIGH | string at `0x1ee95e0` | yes |
| `machine` | HIGH | string at `0x1d324b2` | yes |
| `opt-pointers` | HIGH | string at `0x1ee95f5` | yes |
| `warning-as-error` | HIGH | string at `0x1d32625` | yes |
| `disable-warnings` | HIGH | string at `0x1d325f0` | yes |
| `cloning` | HIGH | string at `0x1ee917b` | yes |
| `compile-only` | HIGH | substring in help/format strings | yes |
| `compile-as-tools-patch` | HIGH | substring in help/format strings | yes |
| `slr` | HIGH | direct literal in `sub_1103030` line 734; no standalone string entry | no |
| `optimize-float-atomics` | HIGH | string at `0x1ee962b` | yes |
| `preserve-relocs` | HIGH | substring in help/format strings | yes |
| `make-errors-visible-at-exit` | HIGH | string at `0x1ee9642` | yes |
| `reserve-null-pointer` | HIGH | substring in help/format strings | no |
| `dont-reserve-null-pointer` | HIGH | string at `0x1d32583` | yes |
| `fast-compile` | HIGH | substring in help/format strings | yes |
| `sw2614554` | HIGH | substring in help/format strings | yes |
| `sw2837879` | HIGH | substring in help/format strings | yes |
| `sw1729687` | HIGH | substring in help/format strings | yes |
| `sw200428197` | HIGH | substring in help/format strings | yes |
| `sw200387803` | HIGH | substring in help/format strings | yes |
| `sw200764156` | HIGH | substring in help/format strings | yes |
| `sw4575628` | HIGH | substring in help/format strings | yes |
| `set-texmode-raw` | HIGH | string at `0x1ee96e3` | yes |
| `fdcmpt` | HIGH | substring in help/format strings | yes |
| `cuda-api-version` | HIGH | substring in help/format strings | yes |
| `noFwdPrg` | HIGH | substring in help/format strings | no |
| `assume-extern-functions-do-not-sync` | HIGH | string at `0x1eeb6e8` | yes |
| `legacy-bar-warp-wide-behavior` | HIGH | string at `0x1ee96f3` | yes |
| `disable-fast-video-emulation` | HIGH | string at `0x1ee9711` | yes |
| `suppress-async-bulk-multicast-advisory-warning` | HIGH | string at `0x1eeb8c0` | yes |
| `suppress-sparse-mma-advisory-info` | HIGH | string at `0x1eeb928` | yes |
| `limit-fold-fp` | HIGH | string at `0x1ee974b` | yes |
| `sanitize` | HIGH | string at `0x1ee9759` | yes |
| `split-compile` | HIGH | substring in help/format strings (embedded in `-split-compile=%d` at `0x1d3229b`) | yes |
| `jobserver` | HIGH | string at `0x1ee9777` | yes |
| `fmad` | HIGH | substring in help/format strings | yes |
| `allow-expensive-optimizations` | HIGH | string at `0x1ee979f` | yes |
| `extensible-whole-program` | HIGH | substring in help/format strings | yes |
| `force-rela` | HIGH | string at `0x1d326c2` | yes |
| `position-independent-code` | HIGH | string at `0x1ee97c1` | yes |
| `verbose-tkinfo` | HIGH | string at `0x1d32744` | yes |
| `no-membermask-overlap` | HIGH | substring in help/format strings | yes |
| `membermask-overlap` | HIGH | substring in help/format strings | yes |
| `print-potentially-overlapping-membermasks` | HIGH | string at `0x1eebe68` | yes |
| `g-tensor-memory-access-check` | HIGH | string at `0x1ee97ef` | yes |
| `gno-tensor-memory-access-check` | HIGH | string at `0x1eec010` | yes |
| `compiler-annotations` | HIGH | string at `0x1ee982b` | yes |
| `sw4915215` | HIGH | substring in help/format strings | yes |
| `sw4936628` | HIGH | string at `0x1ee9851` | yes |
| `blocks-are-clusters` | HIGH | substring in help/format strings | yes |
| `enable-extended-smem` | HIGH | string at `0x1d328e8` | yes |
| `cuda32f3056bbb` | HIGH | string at `0x1ee986c` | no |
| `nv-host` | HIGH | substring in help/format strings (embedded in `--nv-host` at `0x1eec1bd`) | no |
| `tool-name` | HIGH | string at `0x1d3291b` | yes |
| `help` | HIGH | substring in help/format strings | yes |
| `version` | HIGH | substring in help/format strings | yes |
| `options-file` | HIGH | string at `0x1d3293b` | yes |
| `trap-into-debugger` | HIGH | string at `0x1d3294f` | yes |

**Summary:** 108 options enumerated. 105/108 have direct string evidence in `nvlink_strings.json` (either exact or embedded as substring in a longer help/format string). 3/108 (`uumn`, `cimm`, `slr`) appear only as direct string literals in the decompiled `sub_1103030` calls -- these are short 3-4 character names whose storage is deduped or tail-overlapped with longer strings, invisible to a naive standalone-string dumper but provably present at the call site. 97/108 options (90%) are shared with the standalone ptxas tool documented in the [ptxas CLI Options](../../ptxas/config/cli-options.html) page, confirming that the embedded ptxas in nvlink is substantially the same option-parsing codebase as the separate binary. The nvlink-only options (`key`, `okey`, `ptx-length`, `input-as-string` -- wait, these ARE shared; the true nvlink-only set is: `uumn`, `cimm`, `fastimul`, `slr`, `reserve-null-pointer`, `debug-info`, `nv-host`, `noFwdPrg`, `cuda32f3056bbb`) are either obsolete debugging flags or NVIDIA-internal undocumented behavior switches.
