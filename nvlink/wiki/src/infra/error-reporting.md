# Error Reporting System

nvlink's diagnostic infrastructure routes every warning, error, and fatal message through a two-function pipeline: `diag_emit` (`sub_467460`) is the variadic entry point called from ~200 sites across the binary, and `diag_format` (`sub_467A70`) is the heavy formatter that renders severity prefixes, source locations, ANSI color tokens, multi-line alignment, and optional source-code snippets. Both functions dispatch through a per-thread state block obtained via `tls_get_state` (`sub_44F410`), a pthread TLS system that allocates a 0x118-byte structure per thread. Fatal-severity messages terminate the process via `longjmp` back to the pipeline's error-recovery point, or through `fatal_exit` (`sub_44A440`) which calls `abort()` under `--trap-into-debugger` and `exit(1)` otherwise. Diagnostic descriptors are statically allocated in a BSS table of 88 confirmed 16-byte entries spanning `0x2A5B530`--`0x2A5BB70`, each encoding a severity level and a printf-style format string pointer. An additional 5 BSS objects in the same region serve as error-status return values or suppression bitmaps rather than diagnostic descriptors.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_467460` | `diag_emit` | 1,552 B | Variadic entry point; dispatches descriptor + va_args to formatter |
| `sub_467A70` | `diag_format` | 13,105 B | Full formatter with location, color, source snippets, output routing |
| `sub_44F410` | `tls_get_state` | 432 B | Pthread TLS; allocates 0x118-byte per-thread diagnostic/sync state |
| `sub_44A030` | `diag_fprintf` | 48 B | Simple `vfprintf` to diagnostic stream (stderr or redirected) |
| `sub_44A010` | `diag_vfprintf` | 48 B | `vfprintf` variant taking pre-built `va_list` |
| `sub_44A0E0` | `diag_write_buffered` | 336 B | Buffered output with per-line callback dispatch |
| `sub_44A260` | `diag_printf` | 48 B | Variadic wrapper around `diag_write_buffered` |
| `sub_44A440` | `fatal_exit` | 16 B | Fatal handler: `abort()` if debugger trap, else `exit(1)` |
| `sub_44A410` | `fatal_dispatch` | 8 B | Indirect call through fatal handler function pointer |
| `sub_44A420` | `exit_dispatch` | 8 B | Indirect call through exit handler function pointer |
| `sub_44A3F0` | `install_handlers` | 32 B | Installs custom fatal/exit handler function pointers |
| `sub_44A000` | `set_diag_stream` | 8 B | Sets `qword_2A5F3A0` (diagnostic output FILE*) |
| `sub_458000` | `assertion_failure` | 64 B | Prints "Assertion failure at %s, line %d: " and message |
| `sub_457FF0` | `set_assert_location` | 16 B | Stores file and line for next assertion failure |
| `sub_42FA60` | `set_trap_flag` | 8 B | Sets `byte_2A5F358 = 1` (--trap-into-debugger) |
| `sub_45CAC0` | `fatal_alloc` | 16 B | Emits fatal via `&unk_2A5BB70` (out-of-memory descriptor) |
| `sub_4684A0` | `has_color_support` | 16 B | Returns `tls_state[51]` (ANSI color enable flag) |
| `sub_44FB20` | `strbuf_create` | 96 B | Allocates 40-byte string buffer for message assembly |
| `sub_44FDC0` | `strbuf_finalize` | 160 B | Flattens string buffer chain into a single C string |
| `sub_44FE60` | `strbuf_append_str` | 288 B | Appends a string to the buffer chain |
| `sub_44FF90` | `strbuf_append_char` | 304 B | Appends a single character to the buffer |
| `sub_4500A0` | `strbuf_vsprintf` | 496 B | `vsnprintf` into string buffer (1024-byte stack fast path) |
| `sub_450280` | `strbuf_sprintf` | 32 B | Variadic wrapper around `strbuf_vsprintf` |
| `sub_4504A0` | `strbuf_length` | 8 B | Returns `*(qword*)(buf + 8)` -- current buffer length |
| `sub_44FD40` | `strbuf_snapshot` | 112 B | Snapshots buffer content without consuming it |
| `sub_45B7B0` | `channel_write` | 192 B | Multi-mode output: string buffer, FILE*, sprintf, stdout |
| `destr_function` | `tls_destructor` | 212 B | Pthread key destructor; tears down per-thread state |

## Severity Levels

The first DWORD of each diagnostic descriptor (`*a1`) encodes the severity. `diag_emit` reads this field and dispatches accordingly. Six levels are defined:

| Level | Value | Prefix String | ANSI Token | Output Channel | Effect |
|---|---|---|---|---|---|
| Note | 0 | *(none)* | -- | -- | Silent; `diag_emit` returns immediately |
| Info | 1 | `""` (empty) | `@I@` | `qword_2A5F8A0[0]` (info) | Informational; suppressed by `--disable-infos` |
| Info (labeled) | 2 | `"info    "` | `@O@` | `qword_2A5F8A0[0]` (info) | Same as Info but with explicit prefix |
| Warning | 3 | `"warning "` | `@W@` | `qword_2A5F8A0[1]` (warning) | May be promoted to error by `-Werror`; suppressed by `-w` |
| Error (soft) | 4 | `"error*  "` | `off_1D3B7A8` | `qword_2A5F8A0[1]` (warning) | Records error flag but does not terminate |
| Error | 5 | `"error   "` | `off_1D3B7A8` | `qword_2A5F8A0[2]` (error) | Records error flag; sets "had errors" state |
| Fatal | 6 | `"fatal   "` | `off_1D3B7A8` | `qword_2A5F8A0[4]` (fatal) | Terminates via `longjmp` or `fatal_exit` |

The `off_1D3B7A8` pointer references the ANSI color-reset/error-color token (red), shared by error* / error / fatal.

### Severity Dispatch Logic

```c
diag_emit(descriptor, ...):
    severity = descriptor->level          // DWORD at offset 0
    if descriptor->suppressed:            // BYTE at offset 4
        return

    if severity == 3 (warning):
        if tls_state[49]:                 // warnings suppressed (-w)
            return
        if tls_state[50]:                 // -Werror active
            severity = 5                  // promote to error
            prefix = "error   "

    if severity == 2 (info):
        if tls_state[48]:                 // infos suppressed (--disable-infos)
            return

    if severity == 0 (note):
        return
```

### Warning-as-Error Promotion

When `-Werror` is active (`tls_state[50] != 0`), any warning (severity 3) is promoted to severity 5 (hard error). The prefix changes from `"warning "` to `"error   "`. This applies in both `diag_emit` and `diag_format`. The promotion is per-emission, not per-descriptor -- the descriptor itself remains severity 3 so that the same diagnostic can be a warning in one invocation and an error in another.

## Diagnostic Descriptor Table

Each diagnostic site calls `diag_emit` with a pointer to a static descriptor object as the first argument. These descriptors live in the BSS region at addresses `0x2A5B530` through `0x2A5BB70`, spaced at 16-byte intervals. 88 unique diagnostic descriptors have been confirmed across 380+ call sites in the decompiled codebase, referenced from 257 distinct functions.

### Descriptor Structure

```text
DiagDescriptor (16 bytes)
=======================================================
Offset  Size  Field         Description
-------------------------------------------------------
  0      4    level         Severity level (0-6, see table above)
  4      1    suppressed    If non-zero, diag_emit returns immediately
  5      3    padding
  8      8    format_str    Pointer to printf-style format string
```

The `level` field is read as `*a1` (the first DWORD). The `suppressed` field is checked as `*(BYTE*)(a1 + 4)`. The format string at offset 8 is passed as the format argument to `vsnprintf`/`vfprintf` during message assembly.

### Complete Descriptor Catalog

98 unique descriptor addresses have been identified across the BSS range `0x2A5B500`--`0x2A5BFE0`. Of these, 88 are true diagnostic descriptors passed to `diag_emit`/`diag_format`, 3 are error-status objects used as return values (`unk_2A5BFC0`, `unk_2A5BFD0`, `unk_2A5BFE0`), and the remainder serve as BSS anchors for `__cxa_atexit` or warning-suppression bitmaps (`unk_2A5B500`, `unk_2A5BB80`).

The tables below are organized by subsystem. Severity is inferred from `diag_emit` dispatch logic: descriptors whose callers continue execution after the call are Error (5) or Warning (3); descriptors whose callers never reach the next instruction (the call site is the function's last reachable point) are Fatal (6). The `Sites` column counts the number of distinct `sub_467460` call sites referencing each descriptor.

#### Input Processing and File I/O (sub_427A10, sub_42AF40, main)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B550` | 5 | 2 | Input file not found (LTO module add) | `"%s"` (file path) |
| `unk_2A5B5B0` | 3 | 1 | Input not a device ELF and not cudadevrt | `"%s"` (file name) |
| `unk_2A5B700` | 5 | 3 | Required input file cannot be opened | `"%s"` (file path) |
| `unk_2A5B710` | 5 | 11 | Output file cannot be opened for writing (`fopen` failure) | `"%s"` (file path) |
| `unk_2A5B730` | 3 | 2 | Input file fails magic/format validation | `"%s"` (file path) |
| `unk_2A5B890` | 5 | 2 | Timing CSV file cannot be opened | `"%s"` (file path) |

#### Architecture Validation (sub_426570, sub_42A2D0)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B5C0` | 3 | 1 | Input ELF ISA version mismatch (non-fatal, older ABI) | `"%s"` (input file) |
| `unk_2A5B610` | 5 | 1 | Architecture mismatch between input files | `"%s"` (file), `%s` (arch) |
| `unk_2A5B620` | 3 | 1 | sm\_90 required but input built for sm <= 0x77 | `"%s"` (input file) |
| `unk_2A5B630` | 3 | 1 | sm\_50 required but input built for sm <= 0x40 | `"%s"` (input file) |
| `unk_2A5B640` | 3 | 2 | Input ISA class exceeds max supported by target arch | `"%s"` (file), `%d` (class), `%d` (max) |
| `unk_2A5B660` | 5 | 3 | Unsupported architecture for non-cudadevrt input | `%s` (arch), `"%s"` (file) |
| `unk_2A5B680` | 3 | 4 | Address size mismatch (`e_machine != EM_CUDA`) | `"%s"` (input file) |
| `unk_2A5B690` | 3 | 1 | 32/64-bit address mode mismatch between files | `"%s"` (file), `"%s"` (`-m32`/`-m64`) |
| `unk_2A5B6A0` | 3 | 1 | Target arch not supported by input ELF | `"%s"` (file), `%s` (arch) |
| `unk_2A5B6B0` | 3 | 1 | ELF class mismatch (e.g., ELFCLASS32 vs ELFCLASS64) | `"%s"` (file), `%d` (class), `%d` (expected) |
| `unk_2A5B6E0` | 3 | 1 | Arch requires split-compile but was disabled; forces mode | `%d` (arch) |

#### CLI Option Parsing (sub_427AE0, sub_429BA0, sub_42BC30--sub_42DBC0)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B530` | 3 | 1 | Arch > 0x48 with 32-bit address mode unsupported | `%d` (arch code) |
| `unk_2A5B540` | 3 | 2 | Conflicting split-compile options (e.g., `-emit-ptx` + `-split-compile-extended`) | `"%s"`, `"%s"`, `"%s"` (option names) |
| `unk_2A5B560` | 3 | 3 | Ignored option due to conflict (e.g., `-use-host-info` with `-kernels-used`) | `"%s"` (message) |
| `unk_2A5B570` | 3 | 1 | Missing required prerequisite flag for arch | `"%s"` (arch string) |
| `unk_2A5B580` | 3 | 1 | `-fdcmpt` option no longer supported | `"%s"` (option) |
| `unk_2A5B590` | 3 | 1 | `--preserve-relocs` option used in non-applicable context | `"%s"` (option) |
| `unk_2A5B5A0` | 3 | 2 | Deprecated option used (`-fdcmpt`, `-m32`) | `"%s"` (option) |
| `unk_2A5B5D0` | 3 | 1 | LTO option mismatch across input files; disabling LTO | `"%s"` (context) |
| `unk_2A5B5E0` | 3 | 1 | Conflicting `-maxrregcount` values across files | `"%s"` (option), `"%s"` (option), `%d` (value1), `%d` (value2) |
| `unk_2A5B5F0` | 3 | 1 | Conflicting `-lineinfo` settings across files | `"%s"` (option), `"%s"` (option) |
| `unk_2A5B600` | 3 | 6 | Per-file option conflict (`-ftz`, `-prec-div`, `-prec-sqrt`, `-fmad`, `-maxrregcount`, `-split-compile`) | `"%s"` (option name) |
| `unk_2A5B650` | 3 | 5 | Option requires `-dlto` or `-g` not present | `"%s"` (option), `"%s"` (required flag) |
| `unk_2A5B6D0` | 3 | 1 | Arch code too low (sm <= 0x59, pre-Volta) | `%d` (arch code) |
| `unk_2A5B6F0` | 3 | 1 | Target GPU ISA version too old (below 0x13) | `%s` (arch string) |
| `unk_2A5B720` | 3 | 5 | Option value overridden or conflicting | `"%s"` (option name) |
| `unk_2A5B740` | 5 | 1 | No output file specified and not in dry-run mode | `"%s"` (option context) |
| `unk_2A5B750` | 5 | 2 | Final output file cannot be written or linked | `"%s"` (file path) |
| `unk_2A5B760` | 3 | 1 | Implicit arch selection when explicit arch given | `"%s"` (arch string) |
| `unk_2A5B770` | 3 | 1 | No output path specified | `"%s"` (context) |

#### Option Value Parsing (sub_42BC30--sub_42DBC0)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B780` | 5 | 18 | Numeric parse failure for option value | `"32-bit integer"`, `"64-bit integer"`, `"32-bit hex"`, `"64-bit hex"`, `"32-bit unsigned integer"`, `"64-bit unsigned integer"` |
| `unk_2A5B790` | 3 | 1 | Response file nesting exceeds limit (>14 levels) | `"%s"` (file path) |
| `unk_2A5B7A0` | 3 | 1 | Unrecognized option in linker script | `"%s"` (option name) |
| `unk_2A5B7B0` | 5 | 1 | Response file cannot be opened for reading | `"%s"` (file path) |
| `unk_2A5B7C0` | 5 | 2 | Duplicate symbol definition in global scope | `"%s"` (symbol name) |
| `unk_2A5B7D0` | 5 | 6 | Option value out of allowed range | `"%s"` (option), `%d` (value) |
| `unk_2A5B7E0` | 3 | 1 | Option value not in allowed set | `"%s"` (value), `"%s"` (option) |
| `unk_2A5B7F0` | 3 | 1 | Linker script command argument count mismatch | `%d` (count) |
| `unk_2A5B800` | 5 | 10 | Trailing characters after numeric option value | `"%s"` (raw value string) |
| `unk_2A5B810` | 5 | 2 | Option type mismatch (expected different value kind) | `"%s"` (value) |
| `unk_2A5B820` | 5 | 1 | Library search path validation failure | `"%s"` (path) |
| `unk_2A5B830` | 5 | 1 | Missing sysroot for library resolution | `"%s"` (library name) |
| `unk_2A5B840` | 3 | 2 | Symbol visibility conflict in linker script | `"%s"` (symbol) |
| `unk_2A5B850` | 3 | 4 | Symbol redefined in linker script version map | `"%s"` (symbol) |
| `unk_2A5B860` | 3 | 1 | Symbol binding override (GLOBAL to LOCAL or vice versa) | `"%s"` (symbol) |
| `unk_2A5B870` | 3 | 1 | Unresolved symbol in linker script export list | `"%s"` (symbol) |

#### Internal Errors / Assertions (sub_4275C0, sub_4298C0, sub_42A2D0, main)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B670` | 6 | 17 | Internal assertion failure; catch-all for "should never happen" | `"Internal error"`, `"cubin not an elf?"`, `"cubin not a device elf?"`, `"fatbin wrong format?"`, `"should never see bc files"`, `"error in LTO callback"`, `"unexpected cpuArch"` |
| `unk_2A5B6C0` | 3 | 1 | Mercury/finalizer phase returned error for function | `"%s"` (function name) |
| `unk_2A5B990` | 6 | 230 | Internal invariant violation (by far the most used descriptor) | See full table below |
| `unk_2A5BAE0` | 6 | 2 | Out-of-memory during TLS state allocation (`sub_44F410`, `sub_44F670`) | -- (format string is the OOM message) |
| `unk_2A5BB70` | 6 | 5 | Catch-all fatal OOM (`fatal_alloc`); recognized by pointer comparison | -- (bypass formatter, direct stderr) |

#### ELF / Section Merging (sub_432870, sub_4325A0--sub_445000)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5B8A0` | 5 | 1 | Architecture state initialization failed | `"%s"` (arch name) |
| `unk_2A5B8B0` | 3 | 4 | Duplicate symbol across input files during merge | `"%s"` (symbol name) |
| `unk_2A5B8C0` | 3 | 2 | Unknown symbol referenced during merge | `"%s"` (symbol name or `"unknown"`) |
| `unk_2A5B8D0` | 3 | 1 | Undefined external symbol encountered in section scan | `"%s"` (symbol name) |
| `unk_2A5B8E0` | 3 | 1 | Target arch (sm <= 0x78) does not support feature | -- |
| `unk_2A5B8F0` | 3 | 1 | Relocation target mismatch during merge | `"%s"` (symbol) |
| `unk_2A5B900` | 3 | 1 | ISA\_CLASS attribute exceeds maximum (arch-specific limit, e.g., > 0x7F for sm\_100a) | `%d` (arch), `"%s"` (`"ISA_CLASS"`), `"%s"` (symbol) |
| `unk_2A5B910` | 3 | 1 | Missing `.nv.compat` section in input ELF | `".nv.compat"` |
| `unk_2A5B920` | 3 | 1 | Register count exceeds architecture maximum | `"%s"` (symbol), `%d` (count), `%d` (max) |
| `unk_2A5B930` | 3 | 2 | Duplicate parameter bank on weak entry symbol | `"%s"` (symbol name) |
| `unk_2A5B940` | 3 | 1 | Conflicting callgraph edge during DCE (dead code elimination) | `"%s"` (callee), `"%s"` (caller) |
| `unk_2A5B950` | 3 | 2 | Common symbol replaced by global.init with smaller size | `"%s"` (symbol), `"%s"` (file) |
| `unk_2A5B960` | 3 | 1 | Call target symbol resolution conflict | `"%s"` (target), `"%s"` (caller), `"%s"` (callee) |
| `unk_2A5B970` | 3 | 1 | Function has address taken but no direct call found | `"%s"` (function name) |
| `unk_2A5B980` | 3 | 1 | Section type mismatch during merge index lookup | `"%s"` (section name) |
| `unk_2A5B9A0` | 3 | 2 | Section reference from unresolved external symbol | `%d` (section index) |
| `unk_2A5B9B0` | 3 | 1 | Weak function replacement failed (old data not freed) | `"%s"` (function name) |
| `unk_2A5B9C0` | 3 | 1 | Section index out of bounds during relocation | `"%s"` (symbol), `"%s"` (source section), `"%s"` (target section) |
| `unk_2A5B9D0` | 3 | 2 | Multiple definitions of same symbol (non-weak) | `"%s"` (symbol), `"%s"` (file1), `"%s"` (file2) |
| `unk_2A5B9E0` | 3 | 1 | Register count for callee exceeds caller's allocation | `"%s"` (callee), `%d` (callee regs), `"%s"` (caller), `%d` (caller regs) |
| `unk_2A5B9F0` | 3 | 1 | Symbol size changed between definitions | `"%s"` (symbol), `"%s"` (file1), `"%s"` (file2) |
| `unk_2A5BA00` | 3 | 2 | Symbol binding mismatch (e.g., LOCAL vs GLOBAL) | `"%s"` (symbol), `"%s"` (file1), `"%s"` (file2) |
| `unk_2A5BA10` | 3 | 3 | Symbol type mismatch between definitions | `"%s"` (symbol), `"%s"` (file1), `"%s"` (file2) |
| `unk_2A5BA20` | 3 | 2 | Undefined symbol reference during final resolution | `"%s"` (symbol), `"%s"` (file) |
| `unk_2A5BA30` | 3 | 1 | Symbol flagged as dead but still referenced | `"%s"` (symbol name) |
| `unk_2A5BA40` | 3 | 5 | Resource limit exceeded (textures, samplers, surfaces) | `%d` (count), `"%s"` (resource type: `"textures"`, `"samplers"`, `"surfaces"`) |
| `unk_2A5BA50` | 1 | 3 | Verbose link-time info dump (function properties, section details) | (formatted multi-line) |
| `unk_2A5BA60` | 3 | 1 | Common symbol max-count equals current architecture limit | `%d` (count) |
| `unk_2A5BA70` | 3 | 1 | Undefined reference during symbol table finalization | `%d` (index) |
| `unk_2A5BA80` | 3 | 3 | Section type conflict during merge (e.g., `"local"` qualifier) | `%d` (type code), `"%s"` (qualifier) |

#### NVINFO / Attribute Parsing (sub_42F6C0, sub_42F760, sub_42F850)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5BAA0` | 3 | 1 | Unrecognized attribute value in nvinfo section | `"%s"` (attribute name), `"%s"` (value) |
| `unk_2A5BAB0` | 3 | 1 | Unknown relocation attribute in nvinfo section | `"Relocation"`, `%d` (code), `"%s"` (type name) |
| `unk_2A5BAC0` | 3 | 3 | Completely unknown attribute or usage code | `"unknown attribute"`, `"unknown usage"` |

#### External Tool Invocation (sub_42FA70, sub_42FCB0)

| Address | Sev | Sites | Usage Context | Example Message |
|---|---|---|---|---|
| `unk_2A5BB00` | 5 | 1 | External tool (ptxas/fatbinary) terminated by signal | `"%s"` (tool), `%d` (signal), `"%s"` (signal name) |
| `unk_2A5BB10` | 5 | 2 | Response-file parse error: unexpected character | `"%s"` (context) |
| `unk_2A5BB20` | 5 | 2 | Response-file parse error: missing closing bracket | `"%s"` (context) |
| `unk_2A5BB30` | 5 | 6 | Response-file parse error: malformed token | `"%s"` (context) |
| `unk_2A5BB40` | 5 | 1 | External tool produced core dump (signal had `0x80` flag) | `"%s"` (tool) |
| `unk_2A5BB50` | 5 | 1 | External tool path not found or not executable | `"%s"` (tool path) |

#### Non-Descriptor BSS Objects

| Address | Size | Role |
|---|---|---|
| `unk_2A5B500` | -- | BSS module base (used as `__dso_handle` argument in `__cxa_atexit` calls) |
| `unk_2A5BB80` | 512 B | Warning-suppression bitmap array (one bit per descriptor, used for `--Wno-*` granular suppression) |
| `unk_2A5BFC0` | 16 B | Error-status object: "empty/null input" returned as `*a4` in file-open helpers |
| `unk_2A5BFD0` | 16 B | Error-status object: "file read error" returned as `*a4` |
| `unk_2A5BFE0` | 16 B | Error-status object: "file stat error" returned as `*a4` |

### unk_2A5B990: Internal Assertion Messages

This single descriptor accounts for 230 of the 380+ total `diag_emit` call sites. It serves as the universal internal-error descriptor, always severity Fatal (6). Every call site passes a literal string describing the violated invariant. The following table catalogs the 40 unique assertion messages observed:

| Message | Subsystem | Sites |
|---|---|---|
| `"section not found"` | Section lookup (various) | 8 |
| `"expected to be finalized"` | ELF finalization checks | 7 |
| `"symbol not found"` | Symbol table lookup | 5 |
| `"overlapping non-identical data"` | Section data merge | 5 |
| `"bank SHT not CUDA_CONSTANT_?"` | Constant bank validation | 2 |
| `"overlapping data spans too much"` | Section data merge | 2 |
| `"entry data should have offset"` | Constant bank entry | 1 |
| `"local data should have offset"` | Local data layout | 1 |
| `"tail data node not found"` | Section data list | 1 |
| `"callgraph not complete"` | Call graph construction | 1 |
| `"callgraph not found"` | DCE / call graph lookup | 1 |
| `"no callgraph node"` | Call graph node missing | 1 |
| `"reference to deleted section"` | Post-DCE section reference | 1 |
| `"reference to deleted symbol"` | Post-DCE symbol reference | 1 |
| `"symbol already assigned"` | Symbol table construction | 1 |
| `"unallocated symbol"` | ELF output generation | 1 |
| `"adding global symbols of same name"` | Symbol merge conflict | 1 |
| `"alias has not been declared"` | Alias resolution | 1 |
| `"alias must be to function"` | Alias validation | 1 |
| `"alias to unknown symbol"` | Alias target lookup | 1 |
| `"couldn't initialize arch state"` | Architecture init | 1 |
| `"duplicate ids in uft.entry"` | UFT table construction | 1 |
| `"efh not found"` | ELF file header missing | 1 |
| `"entry data cannot be GLOBAL"` | Constant bank binding | 1 |
| `"entry_sym was null"` | Entry point resolution | 1 |
| `"invalid index"` | Array bounds check | 1 |
| `"Invalid Path"` | File path validation | 1 |
| `"malformed uidx input"` | UFT index parsing | 1 |
| `"missing nv.udt.entry"` | Missing UDT section | 1 |
| `"missing nv.uft.entry"` | Missing UFT section | 1 |
| `"missing sec strtab"` | Section string table | 1 |
| `"missing std sections"` | Standard ELF sections | 1 |
| `"Negative size encountered"` | Size validation | 1 |
| `"no regcount?"` | Register count lookup | 1 |
| `"no such new reg count"` | Register count update | 1 |
| `"no such original reg count"` | Register count lookup | 1 |
| `"no symbol for index?"` | Symbol index dereference | 1 |
| `"not uidx input"` | UFT type check | 1 |
| `"null entry_sym"` / `"null esym"` / `"null root_kernel sym"` | Null pointer guards | 3 |
| `"nv.uft not found"` | UFT section lookup | 1 |
| `"overlapped offset < full offset?"` | Merge offset validation | 1 |
| `"secidx not virtual"` | Section type check | 1 |
| `"section not mapped"` | Output section layout | 1 |
| `"should only reach here with no opt"` | Optimization state check | 1 |
| `"size of uidx window != nv.udt"` / `"size of uidx window != nv.uft"` | UFT/UDT size match | 2 |
| `"strsec not found"` / `"symsec not found"` | Section lookup | 2 |
| `"UFT stub match not found"` | UFT stub resolution | 1 |
| `"unexpected bindless type"` | Bindless texture dispatch | 1 |
| `"verbose before final"` | Verbose dump ordering | 1 |
| `"writing file"` | File output state check | 1 |

### Sentinel Descriptor: unk_2A5BB70

The sentinel descriptor `unk_2A5BB70` is special: it is the "catch-all fatal" used by `fatal_alloc` and is recognized by both `diag_emit` and `diag_format` via explicit pointer comparison (`a1 == &unk_2A5BB70`). When this descriptor is detected, the formatters skip the source-location and output-channel logic and fall through directly to the simple stderr path, ensuring that even an out-of-memory condition during formatting still produces output.

## Per-Thread State (TLS)

### Allocation

`tls_get_state` (`sub_44F410`) manages a 0x118-byte (280-byte) per-thread state block via POSIX pthread TLS. On first call from any thread:

1. If `qword_2A5F820` is NULL (global TLS system not initialized), initializes:
   - Creates a `pthread_key` with `destr_function` (`0x44F260`) as destructor
   - Creates a recursive mutex (`PTHREAD_MUTEX_RECURSIVE`, type 1)
   - Queries scheduling priority bounds via `sched_get_priority_max/min(SCHED_RR)`
   - Sets up linked-list head pointers for the thread registry

2. Allocates `0x118` bytes via `malloc` (not the arena allocator -- this is one of the few malloc sites in the binary)

3. Zeroes the entire block, then initializes:
   - `pthread_cond_t` at offset 128
   - `pthread_mutex_t` at offset 176
   - `sem_t` at offset 216

4. Links the new block into a global doubly-linked list protected by the global mutex (offsets 256 and 264 are the prev/next pointers)

5. Stores the block via `pthread_setspecific`

### TLS State Layout

```text
TlsState (0x118 = 280 bytes)
=======================================================
Offset  Size  Field              Description
-------------------------------------------------------
  0      1    had_errors         Set to 1 when severity > 2 (error/fatal)
  1      1    had_fatal          Set to 1 when severity > 4 (error/fatal)
  8      8    longjmp_buf_ptr    Pointer to jmp_buf for fatal recovery
 16      8    last_descriptor    Pointer to descriptor that triggered longjmp
 24      8    arena_ptr          Associated memory arena
 32      8    tool_name_ptr      Tool name string for prefixing messages
 40      8    suffix_ptr         Suffix string appended after messages
 48      1    infos_suppressed   Non-zero if --disable-infos active
 49      1    warnings_suppressed Non-zero if -w (--disable-warnings) active
 50      1    werror_active      Non-zero if -Werror (--warning-as-error) active
 51      1    color_enabled      Non-zero if ANSI color output is active
 52      1    single_line_mode   Non-zero to suppress continuation-line alignment
 56      8    line_callback      Function pointer for per-line output callback
 88      8    strbuf_ptr         Scratch string buffer for output assembly
128     48    cond               pthread_cond_t for thread synchronization
176     40    mutex              pthread_mutex_t for thread-local locking
216     32    semaphore          sem_t for thread pool coordination
248      8    sem_ptr            External semaphore pointer (thread pool join)
256      8    prev               Doubly-linked list: previous TLS block
264      8    next               Doubly-linked list: next TLS block
272      1    destroyed          Flag set by destructor to prevent double-free
```

### Thread Safety

The TLS system ensures that diagnostic output from concurrent threads (e.g., during split-compile LTO) does not interleave mid-message. Each thread assembles its complete diagnostic into its own string buffer before writing to the output channel. The global mutex at `mutex` protects only the thread registry linked list, not the per-thread state itself.

The `longjmp_buf_ptr` at offset 8 is set by the pipeline's error-recovery code (typically in `main` around the merge/relocate/finalize phases). When a fatal diagnostic fires, `diag_emit` calls `longjmp` through this pointer, unwinding to the recovery point. If the pointer is NULL (no recovery point set), execution falls through to `fatal_dispatch` which calls the installed fatal handler.

## ANSI Color Tokens

When color output is enabled (`tls_state[51] != 0`), `diag_format` prepends ANSI color escape sequences via token strings that are expanded by the output channel:

| Token | Meaning | Used For |
|---|---|---|
| `@W@` | Warning color (typically yellow/bold) | Warning-severity messages |
| `@I@` | Info color (typically default/dim) | Info-severity messages (level 1) |
| `@O@` | Other/note color (typically cyan) | Info-severity messages (level 2, labeled) |
| `off_1D3B7A8` | Error/fatal color (typically red/bold) | Error*, error, and fatal messages |

Color support is detected by `has_color_support` (`sub_4684A0`), which simply reads the TLS flag. The flag is set during initialization based on whether stderr is a terminal (isatty) and whether `NO_COLOR` or similar environment variables are present.

The `strbuf_append_str` (`sub_44FE60`) function appends these tokens into the output buffer. The actual ANSI escape expansion happens in the output channel layer, not in the formatter itself -- the tokens are literal strings like `"@W@"` that get translated to `"\033[1;33m"` (or similar) at the final write stage.

## Message Formatting Pipeline

### diag_emit (sub_467460)

The primary entry point. Every diagnostic call site in nvlink invokes this function:

```c
void diag_emit(DiagDescriptor *desc, ...) {
    va_list args;
    va_start(args, desc);

    // Early exit if this descriptor is suppressed
    if (desc->suppressed)
        return;

    int severity = desc->level;

    // Severity-specific suppression checks
    if (severity == 3) {                     // Warning
        if (tls_state->warnings_suppressed)
            return;
        if (tls_state->werror_active) {
            if (desc != &unk_2A5BB70)        // Not the catch-all fatal
                severity = 5;                // Promote to hard error
        }
    }
    if (severity == 2) {                     // Info
        if (tls_state->infos_suppressed)
            return;
    }
    if (severity == 0)                       // Note (silent)
        return;

    // For the catch-all fatal descriptor (unk_2A5BB70), use simple stderr path
    if (desc == &unk_2A5BB70) {
        prefix = severity_prefix_table[severity];
        // Print directly via diag_fprintf: "<tool> <prefix>: <message>\n"
        ...
        goto check_fatal;
    }

    // Normal path: assemble message in string buffer
    strbuf *buf = strbuf_create();
    strbuf_vsprintf(buf, desc->format_str, args);
    char *message = strbuf_finalize(buf);

    strbuf *out = strbuf_create();

    // Select color token based on severity
    color = select_color(severity, tls_state->color_enabled);

    // Append color reset/prefix
    strbuf_append_str(out, color);

    // Append tool name if present
    if (tls_state->tool_name_ptr)
        strbuf_sprintf(out, "%s ", tls_state->tool_name_ptr);

    // Append severity prefix ("warning ", "error   ", etc.)
    char *prefix_start = strbuf_snapshot(out);
    strbuf_sprintf(out, "%s%s", "", severity_prefix);
    int indent = strbuf_length(out) - strlen(prefix_start);
    strbuf_sprintf(out, ": ");

    // Append message body with multi-line alignment
    for (char *p = message; *p; p++) {
        strbuf_append_char(out, *p);
        if (*p == '\n' && !tls_state->single_line_mode) {
            strbuf_append_str(out, prefix_start);
            for (int i = 0; i < indent; i++)
                strbuf_append_char(out, ' ');
            strbuf_append_str(out, ". ");    // continuation marker
        }
    }

    // Append suffix if present
    if (tls_state->suffix_ptr)
        strbuf_sprintf(out, " %s", tls_state->suffix_ptr);

    strbuf_append_char(out, '\n');
    char *final = strbuf_finalize(out);

    // Route to output channel
    FILE *channel = output_channels[channel_index_for_severity];
    if (channel)
        channel_write(channel, "%s%s", "", final);
    else
        diag_printf("%s%s", "", final);

    // Set error/fatal flags
check_fatal:
    if (severity > 2)
        tls_state->had_errors = 1;
    if (severity > 4)
        tls_state->had_fatal = 1;
    if (severity > 5) {
        // Fatal: attempt longjmp recovery
        if (tls_state->longjmp_buf_ptr) {
            tls_state->last_descriptor = desc;
            longjmp(tls_state->longjmp_buf_ptr, 1);
        }
        fatal_dispatch();  // No recovery point; terminate
    }
}
```

### diag_format (sub_467A70)

The extended formatter adds source-location awareness. It takes an additional `SourceLocation *` parameter:

```c
void diag_format(DiagDescriptor *desc, SourceLocation *loc, ...) {
    // ... same suppression/promotion logic as diag_emit ...

    // Extract source location metadata
    if (loc && loc->file_info && loc->file_info->diag_context) {
        uint32_t flags = *loc->file_info->diag_context;
        collect_for_index = flags & 1;        // bit 0: collect into diagnostic index
        show_source       = (flags >> 1) & 1; // bit 1: display source snippet
        embed_in_output   = (flags >> 2) & 1; // bit 2: embed in structured output
    }

    // ... same message assembly ...

    // Insert source location before severity prefix
    if (loc && loc->file_info) {
        if (loc->line != 0x0FFFFFFF)  // sentinel for "no line"
            strbuf_sprintf(out, "%s, line %d; ", loc->file_info->filename, loc->line);
    }

    // ... severity prefix, message body, suffix ...

    // Source snippet display (when show_source flag is set)
    if (show_source) {
        // Open source file (cached per file_info)
        if (current_file != loc->file_info) {
            // Close previous file
            if (line_index)
                hash_table_free(line_index);
            fclose(cached_fp);

            // Open new file and build line-offset index
            fp = fopen(loc->file_info->filename, "r");
            cached_fp = fp;
            line_index = hash_table_create(identity_hash, int_equal, 0x400);

            // Scan file, recording byte offset every 10 lines
            uint32_t line_count = 0;
            while ((ch = fgetc(fp)) != EOF) {
                if (ch == '\n') {
                    line_count++;
                    if (line_count % 10 == 0)
                        hash_insert(line_index, line_count / 10, ftell(fp));
                }
                // skip non-newline chars in tight loop
            }
        }

        // Seek to target line using index (nearest 10-line boundary)
        uint32_t target = loc->line - 1;
        long offset = hash_lookup(line_index, target / 10);
        fseek(cached_fp, offset, SEEK_SET);

        // Read and display remaining lines to reach target
        uint32_t remaining = target % 10;
        do {
            if (feof(cached_fp)) {
                snippet = NULL;
            } else {
                strbuf *line_buf = strbuf_create();
                strbuf_append_str(line_buf, "# ");  // source line prefix
                int ch = fgetc(cached_fp);
                while (ch != '\n' && ch != EOF) {
                    strbuf_append_char(line_buf, ch);
                    ch = fgetc(cached_fp);
                }
                strbuf_append_char(line_buf, '\n');
                snippet = strbuf_finalize(line_buf);
            }
        } while (remaining-- > 0);

        // Prepend snippet to output
        if (snippet)
            output = snippet + output;
    }

    // ... output routing, error flags, longjmp ...

    // If collect_for_index flag is set, store diagnostic record
    if (collect_for_index) {
        DiagRecord *rec = arena_alloc(24);
        rec->location = loc;
        rec->descriptor = desc;
        rec->message = message;
        list_append(loc->file_info->diag_list, rec);
    }
}
```

### Source Snippet Display

When `diag_format` is invoked with a source location that has the "show source" flag set, the formatter opens the referenced source file and displays the offending line prefixed with `# `. The file is kept open and cached between diagnostic emissions for the same file. A hash table index maps every 10th line number to a byte offset in the file, enabling efficient seeking to arbitrary lines without scanning from the start.

Example output with source snippet:

```text
# __global__ void kernel(int *data) {
input.cu, line 5; error   : undefined reference to 'missing_symbol'
```

The `0x0FFFFFFF` sentinel in the line field means "no line number available" and suppresses the `", line %d; "` portion of the location string.

## Output Channels

### Stream Selection

Diagnostic output is routed through a 5-element channel table at `qword_2A5F8A0`:

```text
qword_2A5F8A0[0]  --  Info channel (severity 1-2)
qword_2A5F8A0[1]  --  Warning channel (severity 3-4)
qword_2A5F8A0[2]  --  Error channel (severity 5)
qword_2A5F8A0[3]  --  (unused)
qword_2A5F8A0[4]  --  Fatal channel (severity 6)
```

The channel index is determined by the `byte_1D3C728` severity-to-channel mapping table:

| Severity | `byte_1D3C728[severity]` | Channel |
|---|---|---|
| 0 (note) | 0 | -- (never reached) |
| 1 (info) | 0 | `qword_2A5F8A0[0]` |
| 2 (info labeled) | 0 | `qword_2A5F8A0[0]` |
| 3 (warning) | 1 | `qword_2A5F8A0[1]` |
| 4 (error*) | 1 | `qword_2A5F8A0[1]` |
| 5 (error) | 2 | `qword_2A5F8A0[2]` |
| 6 (fatal) | 4 | `qword_2A5F8A0[4]` |

If the selected channel pointer is NULL, output falls back to `diag_printf` which writes to `qword_2A5F3A0` (the diagnostic stream) or `stderr` if that pointer is also NULL.

### channel_write (sub_45B7B0)

A polymorphic output function that dispatches based on the channel object's type field at offset 0:

| Type | Behavior |
|---|---|
| 0, 1 | Formats into a string buffer, passes to `sub_45B6D0` (string accumulator) |
| 2 | Appends directly to a `strbuf` object at `channel + 32` |
| 3 | `vfprintf` to a `FILE*` at `channel + 32` (or `stdout` if NULL) |
| 4 | `vsprintf` into buffer at `channel + 32`, advances pointer |

### Default Stream

The diagnostic stream `qword_2A5F3A0` defaults to NULL, causing output to go to `stderr`. It can be redirected via `set_diag_stream` (`sub_44A000`). Both `diag_fprintf` and `diag_vfprintf` check this pointer before each write:

```c
int diag_fprintf(const char *fmt, ...) {
    FILE *f = qword_2A5F3A0;
    if (!f)
        f = stderr;
    return vfprintf(f, fmt, args);
}
```

## Fatal Error Handling

### Recovery via longjmp

The linker pipeline sets up a `setjmp`/`longjmp` recovery point around error-prone phases. The `jmp_buf` pointer is stored in `tls_state->longjmp_buf_ptr` (offset 8). When a fatal diagnostic fires (severity > 5):

1. `diag_emit` checks if `longjmp_buf_ptr` is non-NULL
2. If set, stores the triggering descriptor in `tls_state->last_descriptor` (offset 16)
3. Calls `longjmp(longjmp_buf_ptr, 1)`, unwinding to the recovery point
4. The recovery code in `main` can inspect `last_descriptor` to determine what went wrong
5. If `longjmp_buf_ptr` is NULL, falls through to `fatal_dispatch`

### fatal_dispatch (sub_44A410) and fatal_exit (sub_44A440)

`fatal_dispatch` calls through `off_2A5BA98`, a function pointer that defaults to `fatal_exit`. The handler installation function `install_handlers` (`sub_44A3F0`) allows replacing both the no-arg fatal handler and the exit-with-code handler:

```c
void install_handlers(void (*fatal_handler)(void), void (*exit_handler)(uint8_t)) {
    if (fatal_handler)
        off_2A5BA98 = fatal_handler;   // fatal_dispatch target
    if (exit_handler)
        off_2A5BA90 = exit_handler;    // exit_dispatch target
}
```

`fatal_exit` (`sub_44A440`) implements the final termination:

```c
_Noreturn void fatal_exit(void) {
    if (byte_2A5F358)      // --trap-into-debugger flag
        abort();           // Generate SIGABRT for debugger attachment
    exit_dispatch(1);      // Call exit handler with code 1
}
```

The `--trap-into-debugger` flag (`byte_2A5F358`) is set by `set_trap_flag` (`sub_42FA60`), which is invoked when the `-_trap_` / `--trap-into-debugger` CLI option is present. This causes `abort()` instead of `exit(1)`, generating a core dump and SIGABRT for post-mortem debugging.

### Assertion Failures

The assertion system uses a two-step process:

1. `set_assert_location` (`sub_457FF0`) stores the file path and line number in globals `qword_2A5F880` and `dword_2A5F878`
2. `assertion_failure` (`sub_458000`) formats and prints the failure message:

```c
void assertion_failure(const char *fmt, va_list args) {
    if (has_color_support())
        diag_printf(ERROR_COLOR);     // off_1D3B7A8
    diag_printf("Assertion failure at %s, line %d: ",
                qword_2A5F880, dword_2A5F878);
    diag_write_buffered(fmt, args);
    diag_printf("\n");
}
```

This is invoked by assertion macros scattered throughout the codebase. The location globals are set immediately before the assertion check so that the file/line information is always fresh.

## Exit Codes

| Code | Meaning | Set By |
|---|---|---|
| 0 | Success | Normal completion in `main` |
| 1 | Error | `fatal_exit` default, or `main` detecting `tls_state->had_errors` |
| 2 | Usage error | Option parsing failures in `nvlink_parse_options` |
| -1 | Internal error | Returned by some internal functions on unexpected failure |

## CLI Options Affecting Diagnostics

| Option | Flag | Effect |
|---|---|---|
| `-w` / `--disable-warnings` | `tls_state[49]` | Suppresses all severity-3 (warning) messages |
| `-Werror` / `--warning-as-error` | `tls_state[50]` | Promotes warnings to hard errors (severity 5) |
| `--disable-infos` | `tls_state[48]` | Suppresses all severity-2 (info) messages |
| `--extra-warnings` | (separate flag) | Enables additional diagnostic descriptors |
| `-_trap_` / `--trap-into-debugger` | `byte_2A5F358` | Fatal errors call `abort()` instead of `exit(1)` |

## String Buffer Subsystem

The diagnostic formatter does not use fixed-size `char[]` buffers. Instead, it assembles messages through a linked-list string buffer (`strbuf`) that grows dynamically via the arena allocator.

### strbuf Layout

```text
strbuf (40 bytes)
=======================================================
Offset  Size  Field         Description
-------------------------------------------------------
  0      8    arena_ptr     Arena for allocations (copied from TLS state)
  8      8    total_len     Total bytes appended so far
 16      8    chain_head    Head of the 16-byte wrapper list {next, chunk_ptr};
                            NULL until the first append
 24      8    tail_cursor   Pointer-to-pointer; init = &chain_head (the standard
                            `result[3] = result + 2` self-referencing-tail trick
                            in `sub_44FB20`), then advances to each new wrapper's
                            next-slot so the next push writes through it
 32      8    last_chunk    Pointer to current (last) chunk for fast append
```

The chunk wrappers themselves (16 bytes each, allocated by `sub_464460`) carry the next-pointer at offset 0 and a pointer to the chunk header at offset 8. Crucially, **`tail_cursor` is not a tail node pointer**: on an empty buffer it aliases `&chain_head`, so the first `*tail_cursor = new_wrapper` write lands directly in the head slot at strbuf+16. Subsequent appends advance the cursor to the new wrapper's next-slot, mirroring the layout in the ELF writer's mode-2 vector header (see [Mode 2: Vector-Backed Writer](../structs/elf-writer.md#mode-2-vector-backed-writer)).

Each chunk is a 24-byte header followed by a data buffer:

```text
strbuf_chunk (24 bytes + data)
=======================================================
Offset  Size  Field        Description
-------------------------------------------------------
  0      8    capacity     Total capacity of this chunk's data buffer
  8      8    remaining    Bytes remaining in this chunk
 16      8    data_ptr     Pointer to the data buffer
```

The fast path in `strbuf_vsprintf` uses a 1024-byte stack buffer. If the formatted message exceeds 1024 bytes, it allocates from the arena.

`strbuf_finalize` walks the chunk chain, copies all data into a single contiguous allocation, and frees the chunks. `strbuf_snapshot` does the same but leaves the buffer intact for further appending.

## Global State Summary

| Address | Type | Name | Description |
|---|---|---|---|
| `qword_2A5F3A0` | `FILE*` | `diag_stream` | Output stream (NULL = stderr) |
| `qword_2A5F820` | `TlsState*` | `tls_head` | Head of per-thread state linked list |
| `qword_2A5F8A0` | `Channel*[5]` | `output_channels` | Per-severity output channel pointers |
| `byte_2A5F358` | `uint8_t` | `trap_into_debugger` | Set by `--trap-into-debugger` |
| `qword_2A5F880` | `char*` | `assert_file` | File path for assertion failure messages |
| `dword_2A5F878` | `uint32_t` | `assert_line` | Line number for assertion failure messages |
| `off_2A5BA98` | `fn_ptr` | `fatal_handler` | Installed fatal handler (default: `fatal_exit`) |
| `off_2A5BA90` | `fn_ptr` | `exit_handler` | Installed exit handler (default: `exit`) |
| `qword_1D3C740` | `char*[7]` | `prefix_table` | Severity-to-prefix-string lookup table |
| `byte_1D3C728` | `uint8_t[7]` | `channel_map` | Severity-to-channel-index mapping |
| `0x2A5B530..0x2A5BB70` | `DiagDescriptor[88]` | `diag_descriptors` | Static diagnostic descriptor table (16 bytes each) |

## Cross-References

**Internal (nvlink wiki):**

- [Memory Arenas](memory-arenas.md) -- Arena allocator used by string buffer functions (`sub_44FB20`, `sub_4307C0`) for message assembly
- [Environment Variables](../config/env-vars.md) -- `__NVLINK_STDERR_REDIRECT` and `NVLINK_DEBUG` env vars that affect diagnostic output routing
- [CLI Flags](../config/cli-flags.md) -- `--disable-warnings`, `-w`, `-Werror`, `--trap-into-debugger` flags that modulate diagnostic severity
- [elfLink Error Codes](../reference/elflink-errors.md) -- elfLink subsystem error codes (0--13) that route through `diag_emit` for user-visible messages
- [Thread Pool](thread-pool.md) -- Per-thread TLS state (`sub_44F410`) shared between the thread pool and diagnostic infrastructure
- [Pipeline Entry](../pipeline/entry.md) -- `main()` error recovery via `setjmp`/`longjmp` that `fatal_exit` and `diag_emit` severity 6 trigger

**Sibling wikis:**

- [ptxas: Threading](../../ptxas/infra/threading.html) -- ptxas-side threading infrastructure that shares the TLS pattern
- [cicc: Diagnostics](../../cicc/infra/diagnostics.html) -- cicc diagnostic subsystem for comparison with nvlink's error reporting architecture
- [cicc: Knobs](../../cicc/config/knobs.html) -- cicc environment variable controls analogous to nvlink's `NVLINK_DEBUG`

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| `diag_emit` at `sub_467460` is the variadic entry point | HIGH | Decompiled `sub_467460` starts with `va_start(arg, a1)` and checks `*(BYTE*)(a1 + 4)` for suppression -- matches descriptor layout |
| Severity levels 0-6 with described prefixes | HIGH | Strings `"error   "` at `0x1d3c672` and `"fatal   "` at `0x1d3c690` confirmed in strings JSON; both 8 chars with trailing spaces |
| `fatal_exit` at `sub_44A440` checks `byte_2A5F358` then abort/exit | HIGH | Decompiled exactly: `if (byte_2A5F358) abort(); sub_44A420(1u);` |
| Assertion failure format string | HIGH | `sub_458000` decompiled: `"Assertion failure at %s, line %d: "` with `qword_2A5F880` / `dword_2A5F878` -- matches string at `0x1d3b7b0` |
| `--trap-into-debugger` flag at `byte_2A5F358` | HIGH | `sub_44A440` reads `byte_2A5F358`; string `"trap-into-debugger"` at `0x1d3294f`; `"Trap into debugger upon assertion failures"` at `0x1d33c80` |
| `-Werror` / `-w` / `--disable-infos` CLI options | HIGH | Strings `"Werror"` at `0x1d3261e`, `"disable-warnings"` at `0x1d325f0`, `"disable-infos"` at `0x1d32654` all confirmed |
| 88 diagnostic descriptors in BSS `0x2A5B530`--`0x2A5BB70` | MEDIUM | Descriptor count derived from systematic xref analysis of `sub_467460` call sites; individual descriptors verified at representative addresses |
| `unk_2A5B990` accounts for 230 call sites (internal assertions) | HIGH | This is the most-referenced descriptor; assertion messages like `"section not found"` (at `0x1f45d28`), `"symbol not found"` (at `0x1d38978`), `"overlapping non-identical data"` (at `0x1d387d8`) all confirmed in strings |
| Sentinel descriptor `unk_2A5BB70` used by `fatal_alloc` | HIGH | `sub_45CAC0` decompiled as one-liner: `return sub_467460(&unk_2A5BB70, ...)` |
| TLS state block is 0x118 bytes (280) via pthread TLS | HIGH | `sub_44F410` decompiled references `pthread_getspecific`/`pthread_setspecific` and allocates via `malloc(0x118)` |
| Per-thread state includes `longjmp_buf_ptr` at offset 8 | MEDIUM | Offset derived from decompiled `sub_467460` code paths that read `*(QWORD*)(tls + 8)` before `longjmp` call |
| Source snippet display with line-index hash table | MEDIUM | Inferred from decompiled `sub_467A70` (13,105 B) which contains `fopen`/`fgetc`/`fseek` patterns; exact hash-table indexing logic complex to verify |
| Output channel table `qword_2A5F8A0[5]` | MEDIUM | Channel array referenced in decompiled `sub_467460` via severity-indexed access; exact array size inferred from severity count |
| `channel_write` at `sub_45B7B0` dispatches by type field | MEDIUM | Function exists (192 B decompiled) with switch-like structure on first field |
| Warning-to-error promotion when `tls_state[50] != 0` | HIGH | Decompiled `sub_467460` shows conditional severity override when `-Werror` flag is set |
| ANSI color tokens `@W@`, `@I@`, `@O@` | MEDIUM | Token strings inferred from decompiled string builder operations; not directly found as standalone strings (embedded in format logic) |
| `has_color_support` at `sub_4684A0` reads TLS flag | HIGH | Function exists in decompiled output (16 B), returns a byte from the TLS state |
