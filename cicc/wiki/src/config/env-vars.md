# Environment Variables

cicc v13.0 checks **20 distinct environment variables** across 36 files containing `getenv()` calls. Six are NVIDIA-specific, six come from the LLVM infrastructure, three from the EDG frontend, and the remainder from the build system and memory allocator. Two of the NVIDIA variables are obfuscated in the binary using an XOR+ROT13 cipher.

## NVIDIA-Specific Variables

### NVVMCCWIZ

| Property | Value |
|----------|-------|
| Checked in | `sub_8F9C90` (main entry) at `0x8F9C90` |
| Expected value | `"553282"` (magic number) |
| Effect | Sets `byte_4F6D280 = 1` -- unlocks a hidden developer/wizard mode |

The value is parsed via `strtol(v, 0, 10)` and compared against the integer 553282. Any other value is silently ignored. When active, this mode likely enables internal diagnostic output or unlocks hidden flags not present in the standard catalog.

### NVVM\_IR\_VER\_CHK

| Property | Value |
|----------|-------|
| Checked in | `sub_12BFF60` at `0x12BFF60`, `sub_2259720` at `0x2259720` |
| Expected value | `"0"` to disable version checking |
| Effect | Controls NVVM IR bitcode version metadata validation |

When unset or set to a nonzero value, cicc validates `nvvmir.version` and `llvm.dbg.cu` metadata in input bitcode against the expected major/minor version. Setting to `"0"` suppresses version mismatch errors (which would otherwise produce return code 3). Checked 4+ times across two verifier instances.

### LIBNVVM\_DISABLE\_CONCURRENT\_API

| Property | Value |
|----------|-------|
| Checked in | `ctor_104` at `0x4A5810` (global constructor) |
| Expected value | Any non-NULL value |
| Effect | Sets `byte_4F92D70 = 1` -- disables thread-safe libnvvm API usage |

This is a safety valve for environments where concurrent libnvvm compilation causes issues. Any non-NULL value triggers single-threaded API behavior.

### NV\_NVVM\_VERSION (Obfuscated)

| Property | Value |
|----------|-------|
| Checked in | `sub_12B9F70` at `0x12B9F70`, `sub_12BB580` at `0x12BB580`, `sub_8F9C90` |
| Encrypted at | `0x3C23A90` and `0x42812C0` (two copies, same ciphertext) |
| Decryption | XOR with `(-109 * ((addr_byte - base + 97) ^ 0xC5))` then ROT13 |
| Expected values | `"nvvm70"` (suppresses check), `"nvvm-latest"` (forces latest mode) |

Controls NVVM version compatibility mode. When set to `"nvvm70"`, the compiler accepts older IR without complaint. When set to `"nvvm-latest"`, it forces the latest IR version mode. Otherwise, the function returns `(a1 > 0x63)` -- a version-number threshold check.

The variable name is encrypted in the binary's `.rodata` section, suggesting NVIDIA intended to keep this escape hatch undiscoverable through casual string scanning.

### LIBNVVM\_NVVM\_VERSION (Obfuscated)

| Property | Value |
|----------|-------|
| Checked in | `sub_12B9F70` at `0x12B9F70` |
| Encrypted at | `0x42812F0` |
| Expected values | Same as `NV_NVVM_VERSION` |

Functionally identical to `NV_NVVM_VERSION`. Both names are checked by the same function; this provides an alternative name for the same feature.

### LLVM\_OVERRIDE\_PRODUCER

| Property | Value |
|----------|-------|
| Checked in | `ctor_036` at `0x48CC90`, `ctor_154` at `0x4CE640` |
| Expected value | Any string |
| Effect | Overrides the producer identification string in output bitcode metadata |

When set, replaces the default LLVM producer string embedded in the bitcode. This affects the `llvm.ident` metadata and potentially the `producer` field in debug info.

## LLVM Infrastructure Variables

### AS\_SECURE\_LOG\_FILE

Checked in `ctor_720` at `0x5C0D60`. Sets the secure log file path for the integrated assembler, registered as LLVM `cl::opt` `"as-secure-log-file-name"`. Expected: a file path.

### TMPDIR / TMP / TEMP / TEMPDIR

Checked in `sub_16C5C30`, `sub_C843A0`, and `sub_721330`. These are probed in priority order: `TMPDIR` first, then `TMP`, `TEMP`, `TEMPDIR`. The EDG frontend (`sub_721330`) only checks `TMPDIR` and falls back to `"/tmp"`.

### PATH

Checked in `sub_16C5290`, `sub_16C7620`, `sub_C86E60`. Standard `PATH` for `findProgramByName` lookups.

### HOME

Checked in `sub_C83840`. Used by `sys::path::home_directory` with `getpwuid_r()` as fallback.

### PWD

Checked in `sub_16C56A0`, `sub_C82800`. Used for fast current-directory resolution (faster than `getcwd`).

### TERM

Checked in `sub_7216D0` (EDG) and `sub_16C6A40`/`sub_C86300` (LLVM). If `TERM=="dumb"`, terminal colors are disabled. Otherwise, specific terminal type strings (ansi, xterm, screen, linux, cygwin, etc.) are matched by integer comparison to determine color capability.

## EDG Frontend Variables

### NOCOLOR

Checked in `sub_67C750`. Respects the [no-color.org](https://no-color.org/) convention: if set to any value, all diagnostic coloring is disabled.

### EDG\_COLORS

Checked in `sub_67C750`. Custom color specification string for EDG diagnostics. Example: `"error=01;31:warning=01;35:note=01;36:locus=01:quote=01"`.

### GCC\_COLORS

Checked in `sub_67C750`. Fallback if `EDG_COLORS` is not set. Default: `"error=01;31:warning=01;35:note=01;36:locus=01:quote=01:range1=32"`. Provides GCC-compatible diagnostic coloring.

### USR\_INCLUDE

Checked in `sub_720A60`. Overrides the system include path (default: `"/usr/include"`) for the EDG frontend.

### EDG\_BASE

Checked in `sub_7239A0`. Sets the EDG base directory for predefined configuration files. Stored in `qword_4F07578`.

### EDG\_MODULES\_PATH

Checked in `sub_723900`. Adds an additional search path for C++ modules in the EDG frontend.

## Build System / Parallelism

### MAKEFLAGS

Checked in `sub_1682BF0`. Parses for `--jobserver-auth=` with either `fifo:` prefix or `N,M` (pipe file descriptor pair) format. Enables GNU Make jobserver integration for parallel compilation limiting.

## Memory Allocator

### MALLOC\_CONF

Checked in `sub_12FCDB0` (jemalloc initialization). One of five configuration sources for the bundled jemalloc allocator. Expected: jemalloc config string such as `"narenas:2,dirty_decay_ms:0"`.

## Dynamic / Generic Access

Two mechanisms allow runtime access to arbitrary environment variables:

1. **`--trace-env=VARNAME`** CLI flag (in `sub_125FB30` and `sub_900130`): reads the named variable and injects its value into the compilation trace.
2. **`sub_C86120`** (LLVM `sys::Process::GetEnv` wrapper): generic `getenv` helper called with dynamic name parameters by LLVM's option processing infrastructure.

## Decompiler Artifacts

Several `getenv("bar")` calls appear in `ctor_106`, `ctor_107`, `ctor_376`, `ctor_614`. These are **not** real environment variable checks. The pattern `getenv("bar") == (char*)-1` is jemalloc's initialization probe testing whether `getenv` is intercepted by a sanitizer. The string `"bar"` is a dummy.
