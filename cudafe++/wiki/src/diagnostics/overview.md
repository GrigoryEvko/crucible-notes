# Diagnostic System Overview

The cudafe++ diagnostic system is a 7-stage pipeline rooted in EDG 6.6's `error.c`. It manages 3,795 error message templates, 9 severity levels, per-error suppression tracking, `#pragma` diagnostic overrides, and two output formats (text and SARIF JSON). The most-connected function in the entire binary — `sub_4F2930` (assertion handler) with 5,185 call sites — feeds into this system, making error handling the single largest cross-cutting concern in cudafe++.

Each of the 3,795 numeric codes also carries a **snake_case diagnostic tag** accepted by `--diag_suppress` and `#pragma nv_diag_*`. The binary's string table holds approximately **859 such tags**: ~254 in the CUDA extension range (codes 3457–3794, displayed as 20000–20337) and ~605 in the EDG base range (codes 0–3456, displayed unchanged). The CUDA-extension tags are enumerated in [CUDA Errors](./cuda-errors.md) and the [Error Message Catalog](../reference/error-catalog.md); the EDG base namespace is summarized by category prefix at the bottom of the [reference catalog](../reference/error-catalog.md#edg-diagnostic-namespace-outside-this-catalog).

## Error Table

The error message template table lives at `off_88FAA0`: an array of 3,795 `const char*` pointers indexed by error code (0–3794).

| Range | Count | Origin | Display Format |
|---|---|---|---|
| 0–3456 | 3,457 | Standard EDG 6.6 | `#N-D` |
| 3457–3794 | 338 | NVIDIA CUDA extensions | `#(N+16543)-D` (20000–20337-D series) |

The renumbering logic in `construct_text_message` (`sub_4EF9D0`):

```c
int display_code = error_code;
if (display_code > 3456)
    display_code = error_code + 16543;   // 3457 → 20000, 3794 → 20337
sprintf(buf, "%d", display_code);
```

The `-D` suffix is appended only when severity <= 7 (warnings and below). Errors with severity > 7 (catastrophic, command-line error, internal) omit the suffix:

```c
const char *suffix = "-D";
if (severity > 7)
    suffix = "";
```

Any access with error code > 3794 triggers `sub_4F2D30` (`error_text`), which fires an assertion: `"error_text: invalid error code"` (error.c:911).

## Severity Levels

Nine severity values are stored as a single byte at offset 180 of the diagnostic record:

| Value | Name | Display String (lowercase) | Display String (uppercase) | Colorization | Exit Behavior |
|---|---|---|---|---|---|
| 2 | note | `"note"` | `"Note"` | cyan (code 4) | continues |
| 4 | remark | `"remark"` | `"Remark"` | cyan (code 4) | continues |
| 5 | warning | `"warning"` | `"Warning"` | magenta (code 3) | continues |
| 6 | command-line warning | `"command-line warning"` | `"Command-line warning"` | magenta (code 3) | continues |
| 7 | error (soft) | `"error"` | `"Error"` | red (code 2) | continues, counted |
| 8 | error (hard) | `"error"` | `"Error"` | red (code 2) | continues, counted, not suppressible by pragma |
| 9 | catastrophic | `"catastrophic error"` | `"Catastrophic error"` | red (code 2) | immediate exit(4) |
| 10 | command-line error | `"command-line error"` | `"Command-line error"` | red (code 2) | immediate exit(4) |
| 11 | internal error | `"internal error"` | `"Internal error"` | red (code 2) | immediate exit(11) via abort path |

Uppercase display strings are used when `dword_106BCD4` is set, indicating the diagnostic originates from a predefined macro file context (e.g., `"In predefined macro file: Error #..."`).

The special string `"nv_diag_remark"` at offset +8 yields `"remark"` — an NVIDIA-specific annotation kind for CUDA diagnostic remarks.

### Severity Byte Arrays

Three parallel byte arrays, indexed as `[4 * error_code]`, track per-error severity state:

| Array | Address | Purpose |
|---|---|---|
| `byte_1067920` | `0x1067920` | **Default severity** — the compile-time severity assigned to each error code |
| `byte_1067921` | `0x1067921` | **Current severity** — the effective severity after `#pragma` overrides |
| `byte_1067922` | `0x1067922` | **Tracking flags** — bit 0: first-time guard, bit 1: already-emitted, bit 2: has pragma override |

The 4-byte stride means each error code occupies a 4-byte slot across all three arrays, with only the first byte of each slot used. This layout allows the pragma override system (`sub_4F30A0`) to efficiently look up and modify per-error severity.

## 7-Stage Diagnostic Pipeline

```text
  caller emits error
       |
       v
  [1] create_diagnostic_entry     sub_4F40C0
       Allocate ~200-byte record, set error_code + severity
       |
       v
  [2] check_for_overridden_severity   sub_4F30A0
       Walk #pragma diagnostic stack, apply push/pop overrides
       |
       v
  [3] check_severity              sub_4F1330      ← 62 callers, 77 callees
       Central dispatch: suppress/promote, error limit, output routing
       |
       ├─── text path ──────────────────────────────────────┐
       |                                                     |
       v                                                     v
  [4] write_message_to_buffer    sub_4EF620       [6] write_sarif_message_json  sub_4EF8A0
       Expand %XY format specifiers from template             JSON-escape + wrap
       |                                                     |
       v                                                     v
  [5] construct_text_message     sub_4EF9D0 (6.5 KB)       SARIF JSON buffer → stderr
       file:line prefix, severity label, word wrap,
       caret lines, template context, include stack
       |
       v
  [4a] process_fill_in           sub_4EDCD0 (1,202 lines)
       Expand %T/%n/%s/%p/%d/%u/%t/%r specifiers
       |
       v
       output → stderr or redirect file
```

### Stage 1: create_diagnostic_entry (`sub_4F40C0`)

Allocates a diagnostic record via `sub_4EC940` and initializes it:

```c
record = allocate_diagnostic_record();
record->kind = 0;              // primary diagnostic
record->error_code = a1;       // offset 176
if (severity <= 7)
    check_for_overridden_severity(a1, &severity, position);
record->severity = severity;   // offset 180
// resolve source position → file, line, column
// link into global diagnostic chain (qword_106BA10)
```

The wrapper `sub_4F41C0` sets `dword_106B4A8` (file index mode) to -1 for command-line and fatal severities (6, 9, 10, 11), disabling file-index tracking for diagnostics that have no meaningful source location.

Sub-diagnostics are created by `sub_4F5A70` with `kind=2`, linked to their parent's sub-diagnostic chain at offsets 40/48 of the parent record.

### Stage 2: check_for_overridden_severity (`sub_4F30A0`)

Walks the `#pragma diagnostic` stack stored in `qword_1067820`. Each stack entry is a 24-byte record containing a source position, a pragma action code, and an optional error code target.

Pragma action codes and their effect on severity:

| Code | Pragma | Effect |
|---|---|---|
| 30 | `ignored` | Set severity to 3 (suppress) |
| 31 | `remark` | Set severity to 4 |
| 32 | `warning` | Set severity to 5 |
| 33 | `error` | Set severity to 7 |
| 35 | `default` | Restore from `byte_1067920[4 * error_code]` |
| 36 | push/pop marker | Scope boundary for push/pop tracking |

The function uses binary search (`bsearch` with comparator `sub_4ECD20`) to find the nearest pragma entry that applies at the current source position, then walks backward through the stack to resolve nested push/pop scopes.

### Stage 3: check_severity (`sub_4F1330`)

The central dispatch function (601 decompiled lines, 62 callers, 77 callees). This is the most complex function in the error subsystem.

**Complete decision tree pseudocode** (derived from the decompiled `sub_4F1330`):

```c
void check_severity(diagnostic_record *record) {
    dword_1065938 = 0;                    // reset caret-position cache
    uint8_t min_sev = byte_126ED69;       // minimum severity threshold

    // ── Gate 1: Minimum severity filter ──
    if (min_sev > record->severity) {
        if (min_sev == 3)                 // severity 3 = suppress sentinel
            ASSERT_FAIL("check_severity", error.c:3859);
        goto count_and_exit;              // silently discard
    }

    // ── Gate 2: System-header / suppress-all promotion ──
    if (is_system_header(record->source_sequence_number)) {
        min_sev = 8;                      // promote to hard error
    } else if (qword_106BCD8) {           // suppress-all-but-fatal mode
        min_sev = 7;                      // treat as error-level floor
    } else if (min_sev == 3) {
        ASSERT_FAIL("check_severity", error.c:3859);
    }

    if (record->severity < min_sev)
        goto count_and_exit;

    // ── Gate 3: Per-error tracking flags ──
    uint8_t *flags = &byte_1067922[4 * record->error_code];
    if (record->severity <= 7) {          // suppressible severities only
        uint8_t old = *flags;
        *flags |= 2;                      // mark as emitted
        if ((old & 1) && (old & 2))       // first-time guard + already-emitted
            goto suppressed;              // skip: already seen in this scope
    } else {
        *flags |= 2;                      // hard errors: always mark, never skip
    }

    // ── Gate 4: Pragma diagnostic check ──
    if (dword_126C5E4 != -1) {            // scope tracking active
        if (check_pragma_diagnostic(record->error_code,
                                     record->severity,
                                     &record->source_seq)) {
suppressed:
            // Update error/warning counters even when suppressed
            uint8_t sev = record->severity;
            if (sev <= 7 && sev < byte_126ED68)  // promote to error threshold
                sev = sev;                        // keep as-is
            else
                sev = 7;                          // count as error
            update_suppressed_counts(sev, &qword_126EDC8);
            goto count_and_exit;
        }
        // Record pragma scope if applicable
        if (in_template_scope() || has_special_scope_flags())
            record_pragma_diagnostic(record->error_code, record->severity);
    }

    // ── Gate 5: Suppress-all-but-fatal redirect ──
    if (qword_106BCD8 && !dword_106BCD4 && record->error_code != 992) {
        emit_error_992();                 // replace with fatal error 992
        return;                           // guard against catastrophic loop
    }

    // ── Severity promotion: warning → error threshold ──
    uint8_t effective_sev = record->severity;
    if (effective_sev <= 7 && effective_sev >= byte_126ED68) {
        effective_sev = 7;                // promote to error
        if (dword_126C5C8 == -1) {
            update_counts(7, &qword_126ED80);
            if (!dword_126ED78)           // no further counting needed
                goto skip_extra_counts;
            goto update_additional_counts;
        }
    } else if (effective_sev > 7 || effective_sev < byte_126ED68) {
        // Already at error+ or below promotion threshold
    }
    update_counts(effective_sev, &qword_126ED80);
    if (dword_126ED78 && (effective_sev - 9) > 2)  // not catastrophic/internal
        goto update_additional_counts;

skip_extra_counts:
    if (qword_126EDC0)
        update_counts(effective_sev, qword_126EDC0);

    // ── Allocate output buffers (first use) ──
    if (!qword_106B488) {
        qword_106B488 = alloc_buffer(0x400);
        qword_106B480 = alloc_buffer(0x80);
    }
    reset_buffer(qword_106B488);
    reset_buffer(qword_106B480);

    // ── Catastrophic loop detection ──
    if (record->severity == 9) {
        if (dword_106B4B0) {              // already processing catastrophic
            fprintf(stderr, "%s\n", "Loop in catastrophic error processing.");
            emergency_exit(9);            // never returns
        }
        dword_106B4B0 = 1;               // set catastrophic guard
        if (record->error_code == 3709 || !dword_126ED48)
            goto emit_message;
    } else if (record->severity == 11 || record->error_code == 3709) {
        goto emit_message;                // internal error or warnings-as-errors
    }

    // ── Template context expansion ──
    int context_count = 0;
    for (int scope = dword_126C5E4; scope > 0; scope--) {
        context_count += format_scope_context(scope);
    }
    // Include-file context
    if (dword_126EE48 && qword_106B9F0 && has_include_context()) {
        file_info = lookup_source_file(record->source_seq);
        if (file_info != current_file) {
            context_count++;
            // Emit error 1063/1064 (include-stack context)
            create_sub_diagnostic(record, (context_count != 1) ? 1064 : 1063);
        }
    }
    // Context elision (when context_limit is set)
    if (dword_126ED58 > 0 && dword_126ED58 + 1 < context_count) {
        // Emit error 1150: "%d context lines elided"
    }

emit_message:
    // ── Output routing ──
    reset_buffer(qword_106B488);
    if (dword_106BBB8 == 1) {
        // SARIF JSON path
        write_sarif_json(record);         // → qword_106B478
        fputs(sarif_buffer, stderr);
        fflush(stderr);
    } else {
        construct_text_message(record);   // → sub_4EF9D0
    }

    // ── Termination for fatal severities ──
    if (record->severity >= 9 && record->severity <= 11) {
        cleanup();                        // flush output, close files
        emergency_exit(record->severity); // exit with severity as code
        // unreachable
    }

    // ── Error limit enforcement ──
    if (qword_126ED90 + qword_126ED98 >= qword_126ED60) {
        fprintf(stderr, "%s\n", "Error limit reached.");
        if (qword_106C260)                // raw listing file
            fwrite("C \"\" 0 0 error limit reached\n", 1, 29, listing);
        cleanup();
        emergency_exit(9);               // exit(catastrophic)
    }

    // ── Warnings-as-errors promotion ──
    if (record->severity == 5 && dword_106C088 && !dword_106B4BC) {
        uint8_t saved_min = byte_126ED69;
        byte_126ED69 = 4;                // temporarily lower threshold
        dword_106C088 = 0;               // prevent recursion in self
        dword_106B4BC = 1;               // prevent recursion guard
        emit_diagnostic(4, 3709, ...);   // "warnings treated as errors"
        byte_126ED69 = saved_min;        // restore threshold
        dword_106C088 = 1;               // restore mode
    }

    // ── File index update ──
    if (dword_106B4A8 != -1)
        update_file_index(record);

count_and_exit:
    return;
}
```

**Key decision points explained:**

**Minimum severity gate:**

The global `byte_126ED69` is the minimum severity threshold — diagnostics below this level are silently discarded. When the threshold is 3 (the "suppress" sentinel), an assertion fires, which prevents the threshold from ever being set to the suppress level directly.

**System-header promotion:**

When a diagnostic originates from a system header (detected by `sub_5B9B60`), its severity is promoted to 8 (hard error, not suppressible by pragma). This applies equally to CUDA system headers.

**Per-error tracking:**

Bit 0 of the tracking flags (`byte_1067922[4 * error_code]`) acts as a first-time guard: if both bit 0 (first-time) and bit 1 (already-emitted) are set, the error has been suppressed-then-seen, and further emissions are skipped depending on the pragma scope.

**Suppress-all-but-fatal mode:**

When `qword_106BCD8` is set and the error is not error 992 (the fatal sentinel), `check_severity` replaces the current diagnostic with error 992 and re-enters.

**Catastrophic loop detection:**

The re-entry guard `dword_106B4B0` prevents infinite recursion when a catastrophic error triggers another catastrophic error during its own processing. The message `"Loop in catastrophic error processing."` is printed directly to stderr followed by `emergency_exit(9)`.

**Error limit enforcement:**

`qword_126ED90` (total errors) + `qword_126ED98` (total warnings) are checked against `qword_126ED60` (error limit). When exceeded, the compiler writes the limit message and exits with catastrophic status. The raw listing file also receives a machine-readable `C "" 0 0 error limit reached` line.

**Warnings-as-errors promotion:**

When `dword_106C088` (warnings-are-errors mode) is set, every warning (severity 5) triggers error 3709 (`"warnings treated as errors"`) as a follow-up diagnostic. The implementation temporarily lowers the minimum severity threshold to 4 (remark), disables warnings-as-errors mode, sets the recursion guard, emits the diagnostic, then restores all three values. This prevents the error-3709 diagnostic from itself triggering another error-3709.

**Output routing:**

```c
if (dword_106BBB8 == 1)
    // SARIF JSON path: sub_4EF8A0 → qword_106B478
else
    sub_4EF9D0(record);   // text path → construct_text_message
```

**Termination for fatal severities:**

Severities 9 (catastrophic), 10 (command-line error), and 11 (internal error) all trigger cleanup via `sub_66B5E0` followed by `sub_5AF2B0(severity)`, which maps severity to the process exit code.

### Stage 4: write_message_to_buffer (`sub_4EF620`)

Looks up the error template string from the table and expands format specifiers:

```c
const char *template = off_88FAA0[error_code];   // error_code must be <= 3794
```

Format specifier syntax: `%XY...Zn` where:
- `X` = specifier letter (`T`, `d`, `n`, `p`, `r`, `s`, `t`, `u`)
- `Y...Z` = option characters (a-z, A-Z), max 29
- `n` = trailing digit = fill-in index

Special forms:
- `%%` = literal `%`
- `%[label]` = named label fill-in, looked up in `off_D481E0` table

Each specifier dispatches to `process_fill_in` (`sub_4EDCD0`) with the appropriate fill-in kind.

### Stage 5: construct_text_message (`sub_4EF9D0`)

The largest function in the error subsystem at 1,464 decompiled lines (6.5 KB). Formats the complete diagnostic output.

**Output format:**

```text
file(line): severity #code-D: message text
```

Variant formats:
- `"At end of source: ..."` — when line number is 0
- `"In predefined macro file: ..."` — when `dword_106BCD4` is set
- `"Line N"` — when the file name is `"-"` (stdin)

**Sub-diagnostic indentation:**

| Kind | Indent (chars) | Continuation indent |
|---|---|---|
| 0 (primary) | 0 | 10 |
| 2 (sub-diagnostic, same parent) | 10 | 20 |
| 2 (sub-diagnostic, different parent) | 12 | 22 |
| 3 (related) | 1 | 11 |

**Word wrapping:**

The function wraps output text at `dword_106B470` (terminal width) column boundaries. When colorization is disabled, it uses a simple space-scanning algorithm. When colorization is enabled (ESC byte 0x1B in the formatted string), it tracks visible character width separately from escape sequence bytes and wraps only on visual boundaries.

**Fill-in verification:**

After output, the function iterates the fill-in linked list and asserts that every entry has `used_flag == 1`. An unused fill-in triggers: `"construct_text_message: not all fill-ins used for error string: \"...\""` (error.c:4781).

**Raw listing output:**

When `qword_106C260` (raw listing file) is open and the diagnostic is not a continuation (kind != 3), a machine-readable line is emitted:

```text
S "filename" line column message\n
```

Where `S` is a single-character severity code: `R` (remark), `W` (warning), `E` (error), `C` (catastrophic/internal). Internal errors additionally prefix `"(internal error) "` before the message text.

### Stage 6: process_fill_in (`sub_4EDCD0`)

Expands a single format specifier by searching the diagnostic record's fill-in linked list (head at offset 184) for an entry matching the requested kind and index. 1,202 decompiled lines.

Fill-in kind dispatch (from ASCII code of specifier letter):

| Letter | ASCII | Kind | Payload |
|---|---|---|---|
| `%T` | 84 | 6 (type) | Type node pointer |
| `%d` | 100 | 0 (decimal) | Integer value |
| `%n` | 110 | 4 (entity name) | Entity node pointer + options |
| `%p` | 112 | 2 (parameter) | Source position |
| `%r` | 114 | 7 | Byte + pointer |
| `%s` | 115 | 3 (string) | String pointer |
| `%t` | 116 | 5 | (type variant) |
| `%u` | 117 | 1 (unsigned) | Unsigned integer value |

Entity name options (for `%n` specifier):

| Option | Meaning |
|---|---|
| `f` | Full qualification |
| `o` | Omit kind prefix |
| `p` | Omit parameters |
| `t` | Full with template arguments |
| `a` | Omit + show accessibility |
| `d` | Show declaration location |
| `T` | Show template specialization |

## Assertion Handler (`sub_4F2930`)

The most-connected function in the entire cudafe++ binary: 5,185 call sites. Declared `__noreturn`.

**Signature:**

```c
void __noreturn assertion_handler(
    char *source_file,     // EDG source file path
    int   line_number,     // source line number
    const char *func_name, // enclosing function name
    const char *prefix,    // message prefix (or NULL)
    const char *message    // detail message (or NULL)
);
```

**Message format (with prefix):**

```text
assertion failed: <prefix> <message> (<file>, line <line> in <func>)
```

**Message format (without prefix):**

```text
assertion failed at: "<file>", line <line> in <func>
```

The function allocates a 0x400-byte buffer via `sub_6B98A0`, concatenates the message components using `sub_6B9CD0` (buffer append), then calls `sub_4F21C0` (internal_error). Because `sub_4F21C0` is also `__noreturn`, the code after the call is dead — the decompiler shows a loop structure with `sprintf(v20, "%d", v8)` that is never actually reached.

When `dword_126ED40` (suppress assertion output) is set, the message text is replaced with `"<suppressed>"`.

## Internal Error Handler (`sub_4F21C0`)

Creates error 2656 with severity 11 (internal error), outputs it through the standard pipeline, then exits.

```c
void __noreturn internal_error(const char *message) {
    if (dword_1065928) {                     // re-entry guard
        fprintf(stderr, "%s: %s\n", "Internal error loop", message);
        sub_5AF2B0(11);                      // emergency exit
    }
    dword_1065928 = 1;                       // set guard
    diag = sub_4F41C0(2656, &current_pos, 11);  // create diag record
    if (message)
        sub_4F2E90(diag, message);           // attach message as fill-in
    sub_4F1330(diag);                        // route through check_severity
    sub_5AF1D0(11);                          // cleanup + exit(11)
    sub_4F2240();                            // update file index (unreachable)
}
```

The re-entry guard `dword_1065928` prevents infinite recursion: if `internal_error` is called while already processing an internal error (e.g., an assertion fires inside the error formatting code), it prints `"Internal error loop: <message>"` directly to stderr and exits immediately with code 11.

## Exit Codes

| Code | Condition | Trigger |
|---|---|---|
| 0 | Compilation succeeded | Normal exit via `sub_5AF1D0(0)` |
| 2 | Errors encountered | `total_errors > 0` at exit |
| 4 | Catastrophic error | Severity 9 or 10 reached |
| 11 | Internal error | Severity 11 (assertion failure) |
| abort | Double internal error | Re-entry in `sub_4F21C0` or catastrophic loop |

The exit path flows through `sub_5AF2B0`, which maps the severity to the appropriate process exit code. Catastrophic loop detection (`"Loop in catastrophic error processing."`) calls `sub_5AF2B0(9)`, which maps to exit code 4.

## Diagnostic Record Layout

Each diagnostic record is approximately 200 bytes, allocated by `sub_4EC940`:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `kind` | 0=primary, 2=sub-diagnostic, 3=continuation |
| 8 | 8 | `next` | Linked list pointer (global chain) |
| 16 | 8 | `parent` | Parent diagnostic (for sub-diagnostics) |
| 24 | 8 | `related_list` | Related diagnostic chain |
| 40 | 8 | `sub_diagnostic_head` | First sub-diagnostic |
| 48 | 8 | `sub_diagnostic_tail` | Last sub-diagnostic |
| 72 | 8 | `context_head` | Template/include context chain |
| 88 | 8 | `related_info` | Related location info pointer |
| 96 | 8 | `source_sequence_number` | Position in source sequence |
| 136 | 4 | `file_index` | Index into source file table |
| 140 | 2 | `column_end` | End column for caret range |
| 144 | 4 | `line_delta` | Line offset for continuation |
| 152 | 8 | `file_name_string` | Canonical file path |
| 160 | 8 | `display_file_name` | Display-formatted file path |
| 168 | 4 | `column_number` | Column number |
| 172 | 4 | `caret_info` | Caret position data |
| 176 | 4 | `error_code` | Error code (0–3794) |
| 180 | 1 | `severity` | Severity level (2–11) |
| 184 | 8 | `fill_in_list_head` | First fill-in entry |
| 192 | 8 | `fill_in_list_tail` | Last fill-in entry |

## Fill-In Entry Layout

Each fill-in entry is 40 bytes, allocated from a free-list pool (`qword_106B490`) or heap (`sub_6B8070`):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `kind` | Fill-in kind (0–7, mapped from format specifier letter) |
| 4 | 1 | `used_flag` | Set to 1 when consumed during formatting |
| 8 | 8 | `next` | Next fill-in in linked list |
| 16 | 8+ | `payload` | Union: qword for most kinds; int+int for kind 4 (entity name) |

Kind-specific initialization in `alloc_fill_in_entry` (`sub_4F2DE0`):
- Kind 2 (parameter): payload = `qword_126EFB8` (current source position)
- Kind 4 (entity name): payload = 0, extra = `0xFFFFFFFF`, flags = 0
- Kind 7: byte + qword payload
- Default: payload = 0

## Colorization

Initialized by `sub_4F2C10` (`init_colorization`, error.c:825):

1. Check `NOCOLOR` environment variable — if set, disable colorization
2. Check `sub_5AF770` (isatty) — if stderr is not a terminal, disable
3. Read `EDG_COLORS` or `GCC_COLORS` environment variable
4. Default: `"error=01;31:warning=01;35:note=01;36:locus=01:quote=01:range1=32"`

Category codes used in escape sequences:

| Code | Category | Default ANSI |
|---|---|---|
| 1 | reset | `\033[0m` |
| 2 | error | `\033[01;31m` (bold red) |
| 3 | warning | `\033[01;35m` (bold magenta) |
| 4 | note/remark | `\033[01;36m` (bold cyan) |
| 5 | locus | `\033[01m` (bold) |
| 6 | quote | `\033[01m` (bold) |
| 7 | range1 | `\033[32m` (green) |

Controlled by `dword_126ECA0` (colorization requested) and `dword_126ECA4` (colorization active). The `sub_4ECDD0` function emits escape sequences to the output buffer, and `sub_4F3E50` handles escape insertion during word-wrapped output.

## Key Global Variables

| Variable | Address | Type | Purpose |
|---|---|---|---|
| `off_88FAA0` | `0x88FAA0` | `const char*[3795]` | Error message template table |
| `off_D481E0` | `0xD481E0` | struct[] | Named label fill-in table |
| `byte_1067920` | `0x1067920` | byte[4*3795] | Default severity per error |
| `byte_1067921` | `0x1067921` | byte[4*3795] | Current severity per error |
| `byte_1067922` | `0x1067922` | byte[4*3795] | Per-error tracking flags |
| `byte_126ED68` | `0x126ED68` | byte | Error promotion threshold |
| `byte_126ED69` | `0x126ED69` | byte | Minimum severity threshold |
| `qword_126ED60` | `0x126ED60` | qword | Error limit |
| `qword_126ED90` | `0x126ED90` | qword | Total error count |
| `qword_126ED98` | `0x126ED98` | qword | Total warning count |
| `dword_106B4B0` | `0x106B4B0` | int | Catastrophic error re-entry guard |
| `dword_106B4BC` | `0x106B4BC` | int | Warnings-as-errors recursion guard |
| `dword_106BBB8` | `0x106BBB8` | int | Output format (0=text, 1=SARIF) |
| `dword_106C088` | `0x106C088` | int | Warnings-are-errors mode |
| `dword_1065928` | `0x1065928` | int | Internal error re-entry guard |
| `qword_106BCD8` | `0x106BCD8` | qword | Suppress-all-but-fatal mode |
| `dword_106BCD4` | `0x106BCD4` | int | Predefined macro file mode |
| `qword_106B488` | `0x106B488` | qword | Message text buffer (0x400 initial) |
| `qword_106B480` | `0x106B480` | qword | Location prefix buffer (0x80 initial) |
| `qword_106B478` | `0x106B478` | qword | SARIF JSON buffer (0x400 initial) |
| `dword_106B470` | `0x106B470` | int | Terminal width for word wrapping |
| `qword_126EDF0` | `0x126EDF0` | FILE* | Error output stream (default stderr) |
| `qword_106C260` | `0x106C260` | FILE* | Raw listing output file |

## Function Map

| Address | Name (Recovered) | EDG Source | Size | Role |
|---|---|---|---|---|
| `0x4EC940` | `allocate_diagnostic_record` | error.c | — | Pool allocator for diagnostic records |
| `0x4ECB10` | `write_sarif_physical_location` | error.c | — | SARIF location JSON fragment |
| `0x4ECDD0` | `emit_colorization_escape` | error.c | — | Emit ANSI escape to buffer |
| `0x4ED190` | `record_pragma_diagnostic` | error.c | — | Record pragma override in scope |
| `0x4ED240` | `check_pragma_diagnostic` | error.c | — | Check if error suppressed by pragma |
| `0x4EDCD0` | `process_fill_in` | error.c:4297 | 1,202 lines | Format specifier expansion |
| `0x4EF620` | `write_message_to_buffer` | error.c:4703 | 159 lines | Template string expansion |
| `0x4EF8A0` | `write_sarif_message_json` | error.c | 79 lines | SARIF message JSON wrapper |
| `0x4EF9D0` | `construct_text_message` | error.c:3153 | 1,464 lines | Full text diagnostic formatter |
| `0x4F1330` | `check_severity` | error.c:3859 | 601 lines | Central severity dispatch |
| `0x4F2190` | `check_severity_thunk` | error.c | 8 lines | Tail-call wrapper |
| `0x4F21A0` | `internal_error_variant` | error.c | 9 lines | check_severity + exit(11) |
| `0x4F21C0` | `internal_error` | error.c | 22 lines | Error 2656, severity 11, re-entry guard |
| `0x4F2240` | `update_file_index` | error.c | 114 lines | LRU source-file index cache |
| `0x4F24B0` | `build_source_caret_line` | error.c | ~100 lines | Source caret underline |
| `0x4F2930` | `assertion_handler` | error.c | 101 lines | 5,185 callers, `__noreturn` |
| `0x4F2C10` | `init_colorization` | error.c:825 | 43 lines | Parse EDG_COLORS/GCC_COLORS |
| `0x4F2D30` | `error_text_invalid_code` | error.c:911 | 12 lines | Assert on code > 3794 |
| `0x4F2DE0` | `alloc_fill_in_entry` | error.c | 41 lines | Pool allocator for fill-ins |
| `0x4F2E90` | `append_fill_in_string` | error.c | — | Attach string fill-in to diagnostic |
| `0x4F30A0` | `check_for_overridden_severity` | error.c:3803 | ~130 lines | Pragma diagnostic stack walk |
| `0x4F3480` | `format_assertion_message` | error.c | ~100 lines | Multi-arg string builder |
| `0x4F3E50` | `emit_colorization_in_wrap` | error.c | — | Escape handling during word wrap |
| `0x4F40C0` | `create_diagnostic_entry` | error.c:5202 | ~50 lines | Base record creator |
| `0x4F41C0` | `create_diagnostic_entry_with_file_index` | error.c | 13 lines | Wrapper with file-index mode |
| `0x4F5A70` | `create_sub_diagnostic` | error.c:5242 | 32 lines | kind=2 sub-diagnostic creator |
| `0x4F6C40` | `format_scope_context` | error.c | — | Extract instantiation context from scope |

## Call Graph

```text
sub_4F2930 (assertion_handler)  [5,185 callers, __noreturn]
  └── sub_4F21C0 (internal_error)
        ├── sub_4F41C0 (create_diagnostic_entry, error=2656, sev=11)
        │     └── sub_4F40C0 (create_diagnostic_entry)
        │           └── sub_4F30A0 (check_for_overridden_severity)
        ├── sub_4F2E90 (append_fill_in_string)
        ├── sub_4F1330 (check_severity)  [62 callers, 77 callees]
        │     ├── sub_4ED240 (check_pragma_diagnostic)
        │     ├── sub_4EF9D0 (construct_text_message)
        │     │     ├── sub_4EF620 (write_message_to_buffer)
        │     │     │     └── sub_4EDCD0 (process_fill_in)
        │     │     ├── sub_4F24B0 (build_source_caret_line)
        │     │     └── sub_4F3E50 (emit_colorization_in_wrap)
        │     ├── sub_4EF8A0 (write_sarif_message_json)
        │     │     └── sub_4EF620 (write_message_to_buffer)
        │     ├── sub_4F5A70 (create_sub_diagnostic)
        │     ├── sub_4F2DE0 (alloc_fill_in_entry)
        │     ├── sub_4F6C40 (format_scope_context)
        │     ├── sub_66B5E0 (cleanup)
        │     └── sub_5AF2B0 (exit)
        ├── sub_5AF1D0 (cleanup + exit)
        └── sub_4F2240 (update_file_index)
```
