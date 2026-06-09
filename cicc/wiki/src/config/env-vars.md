# Environment Variables

cicc v13.0 checks **24 distinct environment variables** across 36 call sites containing literal-name `getenv()` calls. Seven are NVIDIA-specific (two obfuscated), eight come from the LLVM infrastructure (including the four temp-directory aliases `TMPDIR`/`TMP`/`TEMP`/`TEMPDIR`), six from the EDG frontend, and the remainder from the build system, memory allocator, and shared ptxas/nvptxcompiler infrastructure. Two of the NVIDIA variables have their names encrypted in the `.rodata` section using an XOR+ROT13 cipher to prevent discovery through string scanning.

Build-time configuration is parallel to the environment surface: the EDG frontend ships a **643-entry preprocessor `#define` table** (the `--gen_config` dump rooted at `0xE1` in the EDG dispatcher) that gates every C/C++ language feature, ABI compatibility flag, and target attribute. These are static build-time switches baked into the cicc binary, not runtime knobs. A representative subset is catalogued in [pipeline/edg.md](../pipeline/edg.md#cli-options); the full 643-define surface is currently undocumented (see [Build-Time Gates Stub](#build-time-gates-stub) below).

## String Deobfuscation Engine

The deobfuscation function `sub_8F98A0` at `0x8F98A0` decrypts variable names and option strings from `.rodata` ciphertext. The same engine is also used for hidden CLI option names (see [CLI Flags](./cli-flags.md)).

### Algorithm: sub\_8F98A0

```c
// Reconstructed pseudocode — sub_8F98A0 (0x8F98A0)
// Inputs:
//   ciphertext  — pointer to encrypted bytes in .rodata
//   base        — base address used as key seed (a2)
//   length      — number of bytes to decrypt
//
// Output:
//   plaintext string on stack, null-terminated

char* deobfuscate(const uint8_t* ciphertext, uintptr_t base, size_t length) {
    char buf[64];
    for (size_t i = 0; i < length; i++) {
        uint8_t raw = ciphertext[i];

        // Phase 1: XOR with key derived from position
        uint32_t key = -109 * ((i - base + 97) ^ 0xC5);
        char ch = raw ^ (key & 0xFF);

        // Phase 2: ROT13 on alphabetic characters
        if (ch >= 'A' && ch <= 'Z')
            ch = ((ch - 'A' + 13) % 26) + 'A';
        else if (ch >= 'a' && ch <= 'z')
            ch = ((ch - 'a' + 13) % 26) + 'a';

        buf[i] = ch;
    }
    buf[length] = '\0';
    return buf;
}
```

**Key constant**: The multiplier `-109` (signed, i.e. `0xFFFFFF93`) and the XOR mask `0xC5` together form a position-dependent key stream. The ROT13 phase is applied *after* the XOR, meaning the plaintext must survive two transformations. This is a weak cipher by design — it only needs to defeat `strings(1)` scanning, not serious cryptanalysis.

### Obfuscated String Table in .rodata

All obfuscated strings live in a contiguous region near `0x3C23A7B`--`0x3C23AD6`. Each entry is referenced by its *end* byte address (the deobfuscator walks backward):

| End address     | Length | Decrypted plaintext      | Purpose                                          |
|-----------------|--------|--------------------------|--------------------------------------------------|
| `byte_3C23AD6`  | 14     | *(option prefix)*        | CLI option prefix for `-nvvm-version` matching    |
| `byte_3C23AC3`  | 11     | `"nvvm-latest"`          | Option suffix; sets `v253 = 1` (Path A)           |
| `byte_3C23AB4`  | 6      | `"nvvm70"`               | Option suffix; sets `v253 = 0` (Path B)           |
| `byte_3C23AAD`  | 13     | *(option name)*          | Option name for error message display             |
| `byte_3C23A9F`  | 15     | `"NV_NVVM_VERSION"`      | Environment variable name for `getenv()`          |
| `byte_3C23A82`  | 6      | `"nvvm70"`               | Env var value comparison string                   |
| `byte_3C23A7B`  | 11     | `"nvvm-latest"`          | Env var value comparison string                   |

Additional encrypted copies exist at `0x42812C0` and `0x42812F0` for the two env var names (`NV_NVVM_VERSION` and `LIBNVVM_NVVM_VERSION`) used by `sub_12B9F70`.

### Obfuscated CLI Flag (ctor\_043)

A separate obfuscation instance at `~0x48EE80` in `ctor_043` (`0x48D7F0`) decrypts a **4-byte** hidden `cl::opt` name from data at `unk_3F6F7C7`. The algorithm variant uses FNV-1a-like constants:

```c
v40 = v37 ^ (-109 * ((offset + 97) ^ 0x811C9DC5));
```

The `0x811C9DC5` constant is the FNV-1a 32-bit offset basis. The resulting 4-character option is registered with flag bits `0x87 | 0x38` = hidden + really-hidden, making it invisible even to `--help-hidden`. This is stored at `qword_4F857C0`.

## NVIDIA-Specific Variables

### NVVMCCWIZ

| Property | Value |
|----------|-------|
| Checked in | `sub_8F9C90` (real main) at `0x8F9C90`, specifically `0x8F9D36` |
| Expected value | `"553282"` (magic number = `0x87142`) |
| Effect | Sets `byte_4F6D280 = 1` — unlocks developer/wizard mode |

**Mechanism**: The value is parsed via `strtol(v, 0, 10)` and compared against the integer 553282. Any other value is silently ignored.

**What wizard mode does**: When `byte_4F6D280 = 1`:
- `-v` flag actually enables verbose output (`v259 = byte_4F6D280` instead of 0)
- `-keep` flag actually preserves intermediate files (`v262 = byte_4F6D280`)
- `-dryrun` flag enables verbose as a side effect (`v259 = byte_4F6D280`)
- `-lnk` and `-opt` modes set `v262 = byte_4F6D280` (keep temps)

Without wizard mode, `-v` and `-keep` are *no-ops* — the flags are parsed but have no effect because they set their variables to `byte_4F6D280` which is 0.

### NVVM\_IR\_VER\_CHK

| Property | Value |
|----------|-------|
| Checked in | `sub_12BFF60` at `0x12BFF60` (NVVM IR version verifier, instance 1) |
|            | `sub_2259720` at `0x2259720` (NVVM IR version verifier, instance 2) |
| Expected value | `"0"` to disable version checking |
| Effect | Controls NVVM IR bitcode version metadata validation |

**Detailed mechanism** (from `sub_12BFF60`, 9KB):

1. Reads `getenv("NVVM_IR_VER_CHK")`.
2. If `NULL` or `strtol(env, 0, 10) != 0`: version checking is **enabled** (default).
3. If set to `"0"`: version checking is **disabled** (bypass).

When enabled, the function:
1. Looks up `"nvvmir.version"` named metadata via `sub_1632310(module, &name)`.
2. Also checks `"llvm.dbg.cu"` metadata (debug compile unit presence).
3. Iterates metadata operands, deduplicating via an open-addressing hash table:
   - Hash function: `(value >> 9) ^ (value >> 4) & mask`
   - Tombstone: `0xFFFFFFFFFFFFFFF0` (`-16`)
   - Empty: `0xFFFFFFFFFFFFFFF8` (`-8`)
4. For each unique 2-element version tuple `(major, minor)`:
   - Calls `sub_12BDA30(modules, major, minor)` for IR compatibility check.
   - Special case: `major==2, minor==0` always passes (sentinel for libdevice).
5. For 4-element tuples `(major, minor, debug_major, debug_minor)`:
   - Calls `sub_12BD890(modules, debug_major, debug_minor)` for debug version check.
   - Special case: `debug_major==3, debug_minor<=2` always passes.

The env var is checked **multiple times** per invocation: before IR version validation, before debug IR version validation, and at each version tuple comparison. Return code 3 indicates incompatible version.

**Current expected versions**: `nvvmir.version = {2, minor<=0x62}`, debug version = `{3, minor<=2}`.

### LIBNVVM\_DISABLE\_CONCURRENT\_API

| Property | Value |
|----------|-------|
| Checked in | `ctor_104` at `0x4A5810` (global constructor) |
| Expected value | Any non-NULL value |
| Effect | Sets `byte_4F92D70 = 1` — disables thread-safe libnvvm API usage |

Safety valve for environments where concurrent libnvvm compilation causes issues. Any non-NULL value triggers single-threaded API behavior. See [Concurrent Compilation](../infra/concurrent-compilation.md).

### NV\_NVVM\_VERSION (Obfuscated)

| Property | Value |
|----------|-------|
| Checked in | `sub_12B9F70` at `0x12B9F70`, `sub_12BB580` at `0x12BB580`, `sub_8F9C90` at `0x8F9C90` |
| Encrypted at | `0x3C23A90` and `0x42812C0` (two copies, same ciphertext) |
| Decryption | XOR with `(-109 * ((byte_offset - base + 97) ^ 0xC5))` then ROT13 |
| Expected values | `"nvvm70"` (suppresses check), `"nvvm-latest"` (forces latest mode) |

**How it controls compilation path selection in `sub_8F9C90`**:

The dispatch variable `v253` starts at 2 (default). When `v253` is still 2 at post-parse time (lines 1590--1692):

1. `sub_8F98A0` decrypts the env var name from `byte_3C23A9F[-15..0]`.
2. Calls `getenv(decrypted_name)`.
3. Compares the result against two decrypted reference strings:
   - `"nvvm70"` (from `byte_3C23A82`): sets `v253 = 0` (Path B — NVVM/bitcode pipeline via `sub_1262860` or `sub_1265970`)
   - `"nvvm-latest"` (from `byte_3C23A7B`): sets `v253 = 1` (Path A — PTX pipeline via `sub_902D10` or `sub_905EE0`)
4. If neither matches: uses `(arch > 99)` as the tiebreaker, with further modulation by `-nvc` and `-optixir` flags.

For multi-stage modes (`v263 >= 3`), the resolved path also determines which pipeline flag string is appended:
- Path A (`v253 == 1`): `xmmword_3C23BC0` + `"vm-latest"` (25 bytes total, decodes to `"-nvvm-version=nvvm-latest"`)
- Path B (`v253 == 0`): `xmmword_3C23BC0` + `"vm70"` (20 bytes total, decodes to `"-nvvm-version=nvvm70"`)

The variable name is encrypted in the binary's `.rodata` section because NVIDIA intended to keep this escape hatch undiscoverable through casual `strings(1)` scanning. It controls a fundamental compilation mode choice.

### LIBNVVM\_NVVM\_VERSION (Obfuscated)

| Property | Value |
|----------|-------|
| Checked in | `sub_12B9F70` at `0x12B9F70` |
| Encrypted at | `0x42812F0` |
| Expected values | Same as `NV_NVVM_VERSION` |

Functionally identical to `NV_NVVM_VERSION`. Both names are checked by the same function `sub_12B9F70`; this provides an alternative name for the same feature. Likely exists so that libnvvm API users can set `LIBNVVM_NVVM_VERSION` while standalone cicc users set `NV_NVVM_VERSION`.

### LLVM\_OVERRIDE\_PRODUCER

| Property | Value |
|----------|-------|
| Checked in | `ctor_036` at `0x48CC90`, `ctor_154` at `0x4CE640` |
| Expected value | Any string |
| Effect | Overrides the producer identification string in output bitcode metadata |

**Dual constructor behavior**:
- `ctor_036` (`0x48CC90`): Reads `LLVM_OVERRIDE_PRODUCER`, falls back to `"20.0.0"` (the true LLVM version). Stored in `qword_4F837E0`.
- `ctor_154` (`0x4CE640`): Reads `LLVM_OVERRIDE_PRODUCER`, falls back to `"7.0.1"` (the NVVM IR compatibility marker). Stored separately.

The bitcode writer (`sub_1538EC0`, 58KB) uses the `ctor_154` value, producing `"LLVM7.0.1"` in the `IDENTIFICATION_BLOCK`. This means the output bitcode claims to be LLVM 7.0.1 format, even though cicc is built on LLVM 20.0.0 internally. Setting `LLVM_OVERRIDE_PRODUCER` overrides **both** constructors' values.

### CAN\_FINALIZE\_DEBUG

| Property | Value |
|----------|-------|
| Checked in | `sub_60F290` at `0x60F290`, `sub_4709E0` at `0x4709E0`, `sub_470DA0` at `0x470DA0` |
| Expected value | Controls debug finalization behavior |
| Effect | Gates debug information finalization passes |

Shared with ptxas and nvptxcompiler (same codebase origin). Controls whether debug information finalization passes execute. When unset, the default behavior applies. Three call sites confirmed.

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

| Property | Value |
|----------|-------|
| Checked in | `sub_1682BF0` at `0x1682BF0` |
| Effect | GNU Make jobserver integration for parallel compilation limiting |

Parses for `--jobserver-auth=` with either:
- `fifo:` prefix: FIFO-based jobserver (modern GNU Make)
- `N,M` format: pipe file descriptor pair (classic GNU Make)

When detected, cicc integrates with the jobserver to limit concurrent compilation passes.

## Memory Allocator

### MALLOC\_CONF

Checked in `sub_12FCDB0` (jemalloc `malloc_conf_init`, 15.4 KB — the largest function in its range). One of five configuration sources for the bundled jemalloc allocator. Expected: jemalloc config string such as `"narenas:2,dirty_decay_ms:0"`.

## Dynamic / Generic Access

Two mechanisms allow runtime access to arbitrary environment variables:

1. **`--trace-env=VARNAME`** CLI flag (in `sub_125FB30` and `sub_900130`): reads the named variable and injects its value into the compilation trace. This is a pass-through mechanism for build system integration.
2. **`sub_C86120`** (LLVM `sys::Process::GetEnv` wrapper): generic `getenv` helper called with dynamic name parameters by LLVM's option processing infrastructure.

## Complete Inventory

| # | Env Var Name | Origin | Obfuscated | Category | Key Function |
|---|---|---|---|---|---|
| 1 | `NVVMCCWIZ` | NVIDIA | no | Developer mode | `sub_8F9C90` |
| 2 | `NVVM_IR_VER_CHK` | NVIDIA | no | Version check gate | `sub_12BFF60`, `sub_2259720` |
| 3 | `LIBNVVM_DISABLE_CONCURRENT_API` | NVIDIA | no | Thread safety | `ctor_104` |
| 4 | `NV_NVVM_VERSION` | NVIDIA | **yes** | Version compat / path select | `sub_12B9F70`, `sub_12BB580` |
| 5 | `LIBNVVM_NVVM_VERSION` | NVIDIA | **yes** | Version compat (alias) | `sub_12B9F70` |
| 6 | `LLVM_OVERRIDE_PRODUCER` | LLVM/NVIDIA | no | Bitcode metadata | `ctor_036`, `ctor_154` |
| 7 | `CAN_FINALIZE_DEBUG` | NVIDIA | no | Debug finalization | `sub_60F290`, `sub_4709E0`, `sub_470DA0` |
| 8 | `AS_SECURE_LOG_FILE` | LLVM | no | Assembler logging | `ctor_720` |
| 9 | `TMPDIR` | LLVM/EDG | no | Temp directory | `sub_16C5C30`, `sub_C843A0`, `sub_721330` |
| 10 | `TMP` | LLVM | no | Temp directory (fallback) | `sub_16C5C30`, `sub_C843A0` |
| 11 | `TEMP` | LLVM | no | Temp directory (fallback) | `sub_16C5C30`, `sub_C843A0` |
| 12 | `TEMPDIR` | LLVM | no | Temp directory (fallback) | `sub_16C5C30`, `sub_C843A0` |
| 13 | `PATH` | LLVM | no | Executable lookup | `sub_16C5290`, `sub_16C7620`, `sub_C86E60` |
| 14 | `HOME` | LLVM | no | Home directory | `sub_C83840` |
| 15 | `PWD` | LLVM | no | Working directory | `sub_16C56A0`, `sub_C82800` |
| 16 | `TERM` | LLVM/EDG | no | Terminal type | `sub_7216D0`, `sub_16C6A40`, `sub_C86300` |
| 17 | `NOCOLOR` | EDG | no | Color disable | `sub_67C750` |
| 18 | `EDG_COLORS` | EDG | no | Color scheme | `sub_67C750` |
| 19 | `GCC_COLORS` | EDG | no | Color scheme (fallback) | `sub_67C750` |
| 20 | `USR_INCLUDE` | EDG | no | Include path | `sub_720A60` |
| 21 | `EDG_BASE` | EDG | no | EDG base dir | `sub_7239A0` |
| 22 | `EDG_MODULES_PATH` | EDG | no | Module search path | `sub_723900` |
| 23 | `MAKEFLAGS` | Build | no | Jobserver | `sub_1682BF0` |
| 24 | `MALLOC_CONF` | jemalloc | no | Allocator config | `sub_12FCDB0` |

## Decompiler Artifacts

Several `getenv("bar")` calls appear in `ctor_106`, `ctor_107`, `ctor_376`, `ctor_614`. These are **not** real environment variable checks. The pattern `getenv("bar") == (char*)-1` is jemalloc's initialization probe testing whether `getenv` is intercepted by a sanitizer. The string `"bar"` is a dummy.

`"getenv"` as a string in `ctor_133` (`qword_4F9B700[502]`) is a function name in a libc symbol table used by the EDG frontend for tracking known standard library functions.

`"fegetenv"` in `sub_E42970` is a math library function name in a builtin table.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_8F98A0` | `sub_8F98A0` | ~400B | XOR + ROT13 decryption engine |
| `sub_8F9C90` | `sub_8F9C90` | 10,066B | Main entry; checks NVVMCCWIZ, dispatches via NV_NVVM_VERSION |
| `sub_12B9F70` | `sub_12B9F70` | ~3KB | Reads NV_NVVM_VERSION / LIBNVVM_NVVM_VERSION; compares values |
| `sub_12BB580` | `sub_12BB580` | ~3KB | Second call site for NV_NVVM_VERSION |
| `sub_12BDA30` | `sub_12BDA30` | ~1KB | IR major/minor compatibility check |
| `sub_12BD890` | `sub_12BD890` | ~1KB | Debug IR major/minor compatibility check |
| `sub_12BFF60` | `sub_12BFF60` | 9KB | Full NVVM IR version validator; reads NVVM_IR_VER_CHK |
| `sub_2259720` | `sub_2259720` | 14KB | Second instance of version checker |
| `sub_12FCDB0` | `sub_12FCDB0` | 131,600B | jemalloc config parser; reads MALLOC_CONF |
| `sub_1682BF0` | `sub_1682BF0` | ~2KB | MAKEFLAGS --jobserver-auth parser |
| `sub_C86120` | `sub_C86120` | ~100B | LLVM `sys::Process::GetEnv` wrapper |
| `sub_67C750` | `sub_67C750` | ~2KB | NOCOLOR / EDG_COLORS / GCC_COLORS handler |

## Build-Time Gates Stub

> ⚡ **QUIRK — 643 EDG `#define` build-config keys absent from the wiki**
> The EDG frontend's `--gen_config` handler emits a configuration header containing 643 `#define` keys (e.g. `ABI_COMPATIBILITY_VERSION`, `BACK_END_IS_CP_GEN_BE`, `COROUTINE_ENABLING_POSSIBLE`, all `DEFAULT_*` defaults, every `*_ENABLING_POSSIBLE` feature gate, the `EDG_*`/`GCC_*`/`CLANG_*` compatibility blocks). All values are baked at cicc build time and printed by a single dispatcher branch (`case 0xE1u:`) into the file at `qword_4F07510`. They are **not** environment variables and **not** CLI flags — they are static compile-time switches that determine which language features, ABI quirks, and host emulations the EDG frontend exposes. Confidence: HIGH (string + `fprintf` format extraction). Coverage gap: only ~12 of the 643 are touched in [pipeline/edg.md](../pipeline/edg.md). The full table is a candidate for a dedicated `config/build-defines.md` page.

## Cross-References

- [CLI Flags](./cli-flags.md) — flag-to-pipeline routing, `-v`/`-keep` wizard mode dependency
- [Knobs](./knobs.md) — internal configuration knobs (separate from env vars)
- [Pipeline Entry](../pipeline/entry.md) — `sub_8F9C90` real main, v253 dispatch logic
- [Bitcode I/O](../infra/bitcode-io.md) — LLVM_OVERRIDE_PRODUCER / dual producer mechanism
- [Concurrent Compilation](../infra/concurrent-compilation.md) — LIBNVVM_DISABLE_CONCURRENT_API
- [Libdevice Linking](../infra/libdevice-linking.md) — NVVM_IR_VER_CHK bypass for libdevice
- [Debug Verify](../infra/debug-verify.md) — CAN_FINALIZE_DEBUG, NVVM_IR_VER_CHK interaction
- [NVVM Container](../structs/nvvm-container.md) — NvvmIRVersion/NvvmDebugVersion in container header
