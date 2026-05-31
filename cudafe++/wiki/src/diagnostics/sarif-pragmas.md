# SARIF Output & Pragma Diagnostic Control

cudafe++ supports two diagnostic output formats -- traditional text (default) and SARIF v2.1.0 JSON -- controlled by the `--output_mode` flag (flag index 274, stored in `dword_106BBB8`). Alongside the output format, the pragma diagnostic system allows per-error severity overrides at arbitrary source positions through `#pragma nv_diag_*` directives, which record a stack of severity modifications binary-searched at emission time. A companion colorization subsystem adds ANSI escape sequences to text-mode output, governed by environment variables and terminal detection. This page covers the internals of all three subsystems.

For the diagnostic pipeline architecture, severity levels, and error message formatting, see [Diagnostic Overview](./overview.md). For the CUDA error catalog and tag-name suppression, see [CUDA Errors](./cuda-errors.md). For the unified registry, lifecycle, and dispatch table that hosts `nv_diag_*` alongside every other `#pragma` and preprocessor directive, see [The Pragma Engine](../edg/pragma-engine.md).

## SARIF Output Mode

### Activation

SARIF mode is activated by passing `--output_mode sarif` on the command line. The flag handler (case 274 in the CLI parser at `sub_454160`) performs a simple string comparison:

```c
// sub_454160, case 274
if (strcmp(arg, "text") == 0)
    dword_106BBB8 = 0;        // text mode (default)
else if (strcmp(arg, "sarif") == 0)
    dword_106BBB8 = 1;        // SARIF JSON mode
else
    error("unrecognized output mode (must be one of text, sarif): %s", arg);
```

When `dword_106BBB8 == 1`, three changes take effect globally:

1. `write_init` (`sub_5AEDB0`) emits the SARIF JSON header instead of nothing
2. `check_severity` (`sub_4F1330`) routes each diagnostic through the SARIF JSON builder instead of `construct_text_message`
3. `write_signoff` (`sub_5AEE00`) emits `]}]}\n` instead of the error/warning summary line

All other pipeline behavior -- severity computation, pragma overrides, error counting, exit codes -- is identical in both modes. Exit codes in SARIF mode skip the text messages (`"Compilation terminated."`, `"Compilation aborted."`) but use the same numeric values (0, 2, 4, 11).

### SARIF Header (`sub_5AEDB0`)

`write_init` is called once at the start of compilation. In SARIF mode, it writes the JSON envelope to `qword_126EDF0` (the diagnostic output stream, typically stderr):

```json
{
  "version": "2.1.0",
  "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
  "runs": [{
    "tool": {
      "driver": {
        "name": "EDG CPFE",
        "version": "6.6",
        "organization": "Edison Design Group",
        "fullName": "Edison Design Group C/C++ Front End - 6.6",
        "informationUri": "https://edg.com/c"
      }
    },
    "columnKind": "unicodeCodePoints",
    "results": [
```

The version strings (`"6.6"`) are hardcoded in the binary via two `%s` format arguments that both resolve to the static string `"6.6"`. The `runs` array is opened but not closed -- each diagnostic result is appended as the compilation proceeds, and the array is closed by `write_signoff`.

An assertion guards the mode value: if `dword_106BBB8` is neither 0 nor 1, the function fires `sub_4F2930` with `"write_init"` at `host_envir.c:2017`.

### SARIF Result Object

Each diagnostic emitted through `check_severity` (`sub_4F1330`) produces one JSON result object. The construction happens inline within `check_severity` at LABEL_91, building the JSON into the SARIF buffer `qword_106B478`:

```json
{
  "ruleId": "EC<error_code>",
  "level": "<severity_string>",
  "message": {"text": "<expanded_message>"},
  "locations": [{"physicalLocation": <location_object>}],
  "relatedLocations": [<related_location_objects>]
}
```

**Comma handling:** When `qword_126ED90 + qword_126ED98 > 1` (more than one diagnostic has been emitted), a comma is prepended before the opening `{` to maintain valid JSON array syntax.

#### Rule ID Format

The rule ID is always `"EC"` followed by the **internal** error code (0--3794), not the display code:

```c
sub_6B9CD0(sarif_buf, "\"ruleId\":", 9);
sub_6B9CD0(sarif_buf, "\"EC", 3);
sprintf(s, "%lu", *(uint32_t*)(record + 176));  // internal error code
sub_6B9CD0(sarif_buf, s, strlen(s));
sub_6B9CD0(sarif_buf, "\"", 1);
```

For a CUDA error with internal code 3499 (display code 20042), the rule ID is `"EC3499"`, not `"EC20042"`. This differs from the text-mode format which uses `"EC%lu"` with the same internal code in `construct_text_message`.

#### Level Mapping

The `level` field is derived from the diagnostic severity byte at record offset 180. When severity <= `byte_126ED68` (the error-promotion threshold) and severity <= 7, it is promoted to `"error"` before level selection. The mapping:

| Severity | `level` String | SARIF Standard? |
|---|---|---|
| 4 (remark) | `"remark"` | Non-standard extension |
| 5 (warning) | `"warning"` | Standard |
| 7 (error, soft) | `"error"` | Standard |
| 8 (error, hard) | `"error"` | Standard |
| 9 (catastrophic) | `"catastrophe"` | Non-standard extension |
| 11 (internal) | `"internal_error"` | Non-standard extension |

Any other severity value triggers the assertion at `error.c:4886`:

```c
sub_4F2930(..., "write_sarif_level",
    "determine_severity_code: bad severity", 0);
```

Notes (severity 2) and command-line diagnostics (severity 6, 10) never reach the SARIF level mapper -- notes are suppressed below the minimum severity gate, and command-line diagnostics bypass the SARIF path entirely.

#### Message Object (`sub_4EF8A0`)

The message text is produced by `write_sarif_message_json` (`sub_4EF8A0`), which wraps the expanded error template in a JSON `{"text":"..."}` object:

1. Appends `{"text":"` to the SARIF buffer
2. Calls `write_message_to_buffer` (`sub_4EF620`) to expand the error template with fill-in values into `qword_106B488`
3. Null-terminates the message buffer
4. JSON-escapes the message: iterates each character, prepending `\` before any `"` (0x22) or `\` (0x5C) character
5. Appends `"}` to close the message object

The escaping is minimal -- only double-quote and backslash are escaped. Control characters (newlines, tabs) are not escaped, relying on the fact that EDG error messages do not contain embedded newlines.

#### Physical Location (`sub_4ECB10`)

When the diagnostic record has a valid file index (offset 136 != 0), a `locations` array is emitted containing one physical location object:

```json
{
  "physicalLocation": {
    "artifactLocation": {"uri": "file://<canonical_path>"},
    "region": {"startLine": <line>, "startColumn": <column>}
  }
}
```

The function `sub_4ECB10` (`write_sarif_physical_location`):

1. Calls `sub_5B97A0` to resolve the source-position cookie at record offset 136 into file path, line number, and column number
2. Calls `sub_5B1060` to canonicalize the file path
3. Emits the `artifactLocation` with a `file://` URI prefix
4. Emits `startLine` unconditionally
5. Emits `startColumn` only when the column value is non-zero (the `v4` check: `if (v4)`)

The `startColumn` conditional emission means that diagnostics without column information (e.g., command-line errors) produce location objects with only `startLine`.

#### Related Locations

Sub-diagnostics (linked at record offset 72, the `sub_diagnostic_head` pointer) are serialized into the `relatedLocations` array:

```c
if (record->sub_diagnostic_head) {
    append(",\"relatedLocations\":[");
    int first = 1;
    for (sub = record->sub_diagnostic_head; sub; sub = sub->next) {
        sub->parent = record;          // back-link at offset 16
        append("{\"message\":");
        write_sarif_message_json(sub);  // expand sub-diagnostic message
        if (sub->file_index)
            write_sarif_physical_location(sub);
        append("}");
        if (!first)
            append(",");               // note: comma AFTER closing }
        first = 0;
    }
    append("]");
}
```

Each related location has its own `message` object and an optional `physicalLocation`. The comma is placed **after** the closing brace of each entry except the first, yielding `[{...}{...},{...},...]` -- this is a bug in the JSON generation that produces malformed output when there are three or more related locations, since the first separator comma is missing.

### SARIF Footer (`sub_5AEE00`)

`write_signoff` closes the JSON structure:

```c
if (dword_106BBB8 == 1) {
    fwrite("]}]}\n", 1, 5, qword_126EDF0);
    return;
}
```

This closes: `results` array (`]`), the run object (`}`), the `runs` array (`]`), and the top-level object (`}`), followed by a newline.

In text mode, `write_signoff` instead prints the error/warning summary (e.g., `"3 errors, 2 warnings detected in file.cu"`), using message-table lookups via `sub_4F2D60` with IDs 1742--1748 and 3234--3235 for pluralization.

### Complete SARIF Output Example

```json
{"version":"2.1.0","$schema":"https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json","runs":[{"tool":{"driver":{"name":"EDG CPFE","version":"6.6","organization":"Edison Design Group","fullName":"Edison Design Group C/C++ Front End - 6.6","informationUri":"https://edg.com/c"}},"columnKind":"unicodeCodePoints","results":[{"ruleId":"EC3499","level":"error","message":{"text":"calling a __device__ function(\"foo\") from a __host__ function(\"main\") is not allowed"},"locations":[{"physicalLocation":{"artifactLocation":{"uri":"file:///path/to/test.cu"},"region":{"startLine":10,"startColumn":5}}}]}]}]}
```

## Pragma Diagnostic Control

### Pragma Actions

cudafe++ processes `#pragma nv_diag_*` directives through the preprocessor, which records them as pragma action entries on a global stack. Six action codes are defined:

| Code | Pragma Directive | Severity Effect | Internal Name |
|---|---|---|---|
| 30 | `#pragma nv_diag_suppress` | Set severity to 3 (suppressed) | `ignored` |
| 31 | `#pragma nv_diag_remark` | Set severity to 4 (remark) | `remark` |
| 32 | `#pragma nv_diag_warning` | Set severity to 5 (warning) | `warning` |
| 33 | `#pragma nv_diag_error` | Set severity to 7 (error) | `error` |
| 35 | `#pragma nv_diag_default` | Restore from `byte_1067920[4 * error_code]` | `default` |
| 36 | `#pragma nv_diag_push` / `pop` | Scope boundary marker | push/pop |

Note the gap: action code 34 is not used. Actions 30--33 modify severity, 35 restores the compile-time default, and 36 provides push/pop scoping to allow localized overrides.

The pragmas accept either a numeric error code or a diagnostic tag name:

```c
#pragma nv_diag_suppress 20042              // by display code
#pragma nv_diag_suppress calling_a_constexpr__host__function  // by tag name
```

Display codes >= 20000 are converted to internal codes by `sub_4ED170`:

```c
int internal_code = (display_code > 19999) ? display_code - 16543 : display_code;
```

### Pragma Stack (`qword_1067820`)

The pragma stack is a dynamically-growing array of 24-byte records stored at `qword_1067820`. The array is managed as a sorted-by-position sequence to enable binary search.

Each 24-byte stack entry has the following layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `position_cookie` | Source position (sequence number) |
| 4 | 2 | `column` | Column number within the line |
| 8 | 1 | `action_code` | Pragma action (30--36) |
| 9 | 1 | `flags` | Bit 0: is push/pop with saved index |
| 16 | 8 | `error_code` or `saved_index` | Target error code, or -1/saved push index for scope markers |

The array header (pointed to by `qword_1067820`) contains:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | Pointer to entry array base |
| 8 | 8 | Array capacity |
| 16 | 8 | Entry count |

### Recording Pragma Entries (`sub_4ED190`)

When the preprocessor encounters a `#pragma nv_diag_*` directive, `record_pragma_diagnostic` (`sub_4ED190`) creates a new stack entry:

```c
void record_pragma_diagnostic(uint error_code, uint8_t severity, uint *position) {
    // Hash: (column+1) * (position+1) * error_code * (severity+1)
    uint64_t hash = (*(uint16_t*)(position+2) + 1) * (*position + 1)
                    * error_code * (severity + 1);
    uint64_t bucket = hash % 983;     // 0x3D7

    entry = allocate(32);
    entry->error_code_field = error_code;    // offset 8
    entry->severity = severity;              // offset 12
    entry->position = *position;             // offset 16
    entry->saved_index = 0xFFFFFFFF;         // offset 24 = -1

    // Insert at head of hash chain
    entry->next = hash_table[bucket];        // qword_1065960
    hash_table[bucket] = entry;
}
```

This function serves double duty: it records the pragma entry for the per-diagnostic suppression hash table (`qword_1065960`, 983 buckets) used by `check_pragma_diagnostic` (`sub_4ED240`), and it simultaneously records the entry on the position-sorted pragma stack.

The bit `byte_1067922[4 * error_code] |= 4` is set to mark that this error code has at least one pragma override, enabling the fast-path check in `check_for_overridden_severity`.

### Per-Diagnostic Suppression Check (`sub_4ED240`)

`check_pragma_diagnostic` (`sub_4ED240`) is the fast-path check called from `check_severity` to determine whether a specific diagnostic at a specific source position should be suppressed. It operates on the hash table rather than the sorted stack:

```c
bool check_pragma_diagnostic(uint error_code, uint8_t severity, uint *position) {
    uint64_t hash = (position->column + 1) * (position->cookie + 1)
                    * error_code * (severity + 1);
    entry = hash_table[hash % 983];

    // Walk hash chain matching all four fields
    while (entry) {
        if (entry->error_code == error_code &&
            entry->severity == severity &&
            entry->position == position->cookie &&
            entry->column == position->column)
            break;
        entry = entry->next;
    }
    if (!entry) return false;

    // Scope check: compare current scope ID
    scope = scope_table[current_scope_index];
    if (entry->saved_scope_id != scope->id || scope->kind == 9) {
        entry->saved_scope_id = scope->id;
        entry->emit_count = 0;
        return true;    // first time in this scope → suppress
    }

    // Already seen in this scope → check error limit
    entry->emit_count++;
    return entry->emit_count <= error_limit;
}
```

### Severity Override Resolution (`sub_4F30A0`)

`check_for_overridden_severity` (`sub_4F30A0`) is the position-based pragma stack walker. It is called from `create_diagnostic_entry` (`sub_4F40C0`) for any diagnostic with severity <= 7, and determines the effective severity by walking the pragma stack backward from the diagnostic's source position.

**Entry conditions:**

```c
void check_for_overridden_severity(int error_code, char *severity_out,
                                    int64_t position, ...) {
    char current_severity = byte_1067921[4 * error_code];

    // Fast path: if no pragma override exists for this error code, skip
    if ((byte_1067922[4 * error_code] & 4) == 0)
        goto done;

    // Ensure pragma stack exists and has entries
    if (!qword_1067820 || !qword_1067820->count)
        goto done;
```

**Binary search phase:**

When the diagnostic position is **before** the last pragma stack entry (i.e., the position comparison at offset 0/4 shows the diagnostic comes before the final entry), the function uses `bsearch` with comparator `sub_4ECD20` to find the nearest pragma entry at or before the diagnostic position:

```c
// Construct search key from diagnostic position
search_key.position = position->cookie;
search_key.column = position->column;

qword_10658F8 = 0;  // scratch: will hold the best-match pointer
result = bsearch(&search_key, stack_base, entry_count, 24, comparator);
```

The comparator `sub_4ECD20` compares position cookies first, then columns. It has a side effect: whenever the comparison result is `>= 0` (the search key is at or after the candidate), it stores the candidate pointer in `qword_10658F8`. This means after `bsearch` completes, `qword_10658F8` holds the rightmost entry that is at or before the search key -- the "floor" entry.

**Backward walk phase:**

After finding the starting position (either via binary search or by starting from the last entry), the function walks backward through the stack:

```c
while (1) {
    uint8_t action = *(uint8_t*)(entry + 8);

    if (action == 36) {             // push/pop marker
        if ((*(uint8_t*)(entry+9) & 1) == 0)
            goto skip;              // plain pop: no saved index
        int64_t saved_idx = *(int64_t*)(entry + 16);
        if (saved_idx == -1)
            goto skip;              // push without matching pop
        // Jump to the push point
        entry = &stack_base[24 * saved_idx];
        continue;
    }

    if (*(uint32_t*)(entry + 16) == error_code) {
        switch (action) {
            case 30: current_severity = 3; goto apply;     // suppress
            case 31: current_severity = 4; goto apply;     // remark
            case 32: current_severity = 5; goto apply;     // warning
            case 33: current_severity = 7; goto apply;     // error
            case 35:                                        // default
                current_severity = byte_1067920[4 * error_code];
                goto done;
            default:
                assertion("get_severity_from_pragma", error.c:3741);
        }
    }

skip:
    if (entry == stack_base)
        goto done;                  // reached bottom of stack
    entry -= 24;                    // previous entry
}

done:
    if (current_severity)
        *severity_out = current_severity;

apply:
    *severity_out = current_severity;
```

The key insight is the push/pop handling: action code 36 entries with `flags & 1` set contain a saved index at offset 16 that points to the corresponding push entry. The walker jumps to the push entry, effectively skipping all pragma entries within the pushed scope, restoring the severity state from before the push.

An out-of-bounds entry pointer triggers the assertion at `error.c:3803`:

```c
if (entry < stack_base || entry >= &stack_base[24 * count])
    assertion("check_for_overridden_severity", error.c:3803);
```

### GCC Diagnostic Pragma Output

cudafe++ generates `#pragma GCC diagnostic` directives in its output (the transformed C++ sent to the host compiler) to suppress host-compiler warnings on code that cudafe++ knowingly generates or transforms. These are **not** the same as the `nv_diag_*` pragmas that control cudafe++'s own diagnostics.

The output pragmas are emitted via `sub_467E50` (the line-output function) with hardcoded strings:

```c
// Emitted around certain code regions
sub_467E50("#pragma GCC diagnostic push");
sub_467E50("#pragma GCC diagnostic ignored \"-Wunused-local-typedefs\"");
sub_467E50("#pragma GCC diagnostic ignored \"-Wattributes\"");
// ... generated code ...
sub_467E50("#pragma GCC diagnostic pop");
```

The full set of GCC warnings suppressed in output:

| Warning Flag | Context |
|---|---|
| `-Wunevaluated-expression` | `decltype` expressions in init-captures (when `dword_126E1E8` = GCC host) |
| `-Wattributes` | CUDA attribute annotations on transformed code |
| `-Wunused-parameter` | Device function stubs with unused parameters |
| `-Wunused-function` | Forward-declared device functions not called in host path |
| `-Wunused-local-typedefs` | Type aliases generated for CUDA type handling |
| `-Wunused-variable` | Variables in constexpr-if discarded branches |
| `-Wunused-private-field` | Private members of device-only classes |

On MSVC host compilers, the equivalent mechanism uses `__pragma(warning(push))` / `__pragma(warning(pop))` instead.

## Colorization

### Initialization (`sub_4F2C10`)

Colorization is initialized by `init_colorization` (`sub_4F2C10`), called from the diagnostic pipeline setup. The function determines whether color output should be enabled and parses the color specification.

**Decision sequence:**

```text
1. Assert dword_126ECA0 != 0       (colorization was requested via --colors)
2. Check getenv("NOCOLOR")         → if set, disable
3. Check sub_5AF770()              → if stderr is not a TTY, disable
4. If still enabled, parse color spec
5. Set dword_126ECA4 = dword_126ECA0  (activate colorization)
```

Step 3 calls `sub_5AF770` (`check_terminal_capabilities`), which:

- Verifies `qword_126EDF0` (diagnostic output FILE*) exists
- Calls `fileno()` + `isatty()` on it
- Calls `getenv("TERM")` and rejects `"dumb"` terminals
- Returns 1 if interactive, 0 otherwise

The `--colors` / `--no_colors` CLI flag pair controls `dword_126ECA0` (colorization requested). When `--no_colors` is set or NOCOLOR is in the environment, colorization is unconditionally disabled regardless of terminal capabilities.

### Color Specification Parsing (`sub_4EC850`)

The color specification string is sourced from environment variables with a fallback chain:

```c
char *spec = getenv("EDG_COLORS");
if (!spec) {
    spec = getenv("GCC_COLORS");
    if (!spec)
        spec = "error=01;31:warning=01;35:note=01;36:locus=01:quote=01:range1=32";
}
```

Note: although the string `"DEFAULT_EDG_COLORS"` appears in the binary (as a compile-time macro name), the actual default is hardcoded. The `EDG_COLORS` variable takes priority over `GCC_COLORS`, allowing EDG-specific customization while maintaining GCC compatibility.

The specification format is `category=codes:category=codes:...` where:
- `category` is one of: `error`, `warning`, `note`, `locus`, `quote`, `range1`
- `codes` is a semicolon-separated sequence of ANSI SGR parameters (digits and `;` only)
- `:` separates category assignments

`sub_4EC850` (`parse_color_category`) is called once for each of the 6 configurable categories:

```c
sub_4EC850(2, "error");      // category code 2
sub_4EC850(3, "warning");    // category code 3
sub_4EC850(4, "note");       // category code 4
sub_4EC850(5, "locus");      // category code 5
sub_4EC850(6, "quote");      // category code 6
sub_4EC850(7, "range1");     // category code 7
```

For each category, the parser:

1. Uses `strstr()` to find the category name in the spec string
2. Checks that the character after the name is `=`
3. Extracts the value up to the next `:` (or end of string)
4. Validates that the value contains only digits (0x30--0x39) and semicolons (0x3B)
5. Stores the pointer and length in `qword_126ECC0[2*code]` and `qword_126ECC8[2*code]`
6. If validation fails (non-digit, non-semicolon character), nullifies the entry

### Color Category Codes

Seven category codes are used internally, with code 1 reserved for reset:

| Code | Category | Default ANSI | Escape | Applied To |
|---|---|---|---|---|
| 1 | reset | `\033[0m` | `ESC [ 0 m` | End of any colored region |
| 2 | error | `\033[01;31m` | `ESC [ 01;31 m` | Error/catastrophic/internal severity labels |
| 3 | warning | `\033[01;35m` | `ESC [ 01;35 m` | Warning/command-line-warning labels |
| 4 | note/remark | `\033[01;36m` | `ESC [ 01;36 m` | Note and remark severity labels |
| 5 | locus | `\033[01m` | `ESC [ 01 m` | Source file:line location prefix |
| 6 | quote | `\033[01m` | `ESC [ 01 m` | Quoted identifiers in messages |
| 7 | range1 | `\033[32m` | `ESC [ 32 m` | Source-range underline markers |

### Escape Sequence Emission

Two functions handle color escape output, depending on context:

**`sub_4ECDD0` (`emit_colorization_escape`):** Used within `construct_text_message` for inline color markers. Writes a 2-byte internal marker (ESC byte 0x1B followed by the category code) into the output buffer. These markers are later expanded into full ANSI sequences during the final output pass.

```c
void emit_colorization_escape(buffer *buf, uint8_t category_code) {
    buf_append_byte(buf, 0x1B);     // ESC
    buf_append_byte(buf, category_code);
}
```

**`sub_4F3E50` (`add_colorization_characters`):** Used during word-wrapped output to emit full ANSI escape sequences. For category 1 (reset), it writes `ESC [ 0 m`. For categories 2--7, it writes `ESC [` followed by the parsed ANSI codes from `qword_126ECC0`, followed by `m`.

```c
void add_colorization_characters(uint8_t category) {
    if (category > 7)
        assertion("add_colorization_characters", error.c:862);

    if (category == 1) {
        // Reset: ESC [ 0 m
        buf_append(sarif_buf, ESC);
        buf_append(sarif_buf, '[');
        buf_append(sarif_buf, '0');
        buf_append(sarif_buf, 'm');
    } else if (color_pointer[category]) {
        // ESC [ <codes> m
        buf_append(sarif_buf, ESC);
        buf_append(sarif_buf, '[');
        buf_append_n(sarif_buf, color_pointer[category], color_length[category]);
        buf_append(sarif_buf, 'm');
    }
}
```

The assertion at `error.c:862` fires if a category code > 7 is passed, which would indicate a programming error in the diagnostic formatter.

### Word Wrapping with Colors

`construct_text_message` (`sub_4EF9D0`) has two code paths for word wrapping:

1. **Non-colorized:** Simple space-scanning algorithm that breaks at the terminal width (`dword_106B470`)
2. **Colorized:** Tracks visible character width separately from escape sequence bytes. When the formatted string contains byte 0x1B (ESC), the wrapping logic counts only non-escape characters toward the column width, ensuring that ANSI codes do not prematurely trigger line breaks.

The terminal width `dword_106B470` defaults to a reasonable value (typically 80 or derived from the terminal) and controls the column at which output lines are wrapped.

### Colorization State Variables

| Variable | Address | Purpose |
|---|---|---|
| `dword_126ECA0` | `0x126ECA0` | Colorization requested (`--colors` flag) |
| `dword_126ECA4` | `0x126ECA4` | Colorization active (after init_colorization) |
| `qword_126ECC0` | `0x126ECC0` | Color spec pointer array (2 qwords per category) |
| `qword_126ECC8` | `0x126ECC8` | Color spec length array (paired with pointers) |
| `dword_106B470` | `0x106B470` | Terminal width for word wrapping |

## Diagnostic Counter System (`sub_4F3020`)

The function `update_diagnostic_counter` (`sub_4F3020`) is called from `check_severity` to increment per-severity counters. These counters drive the summary output in `write_signoff` and the error-limit check:

```c
void update_diagnostic_counter(uint8_t severity, uint64_t *counter_block) {
    switch (severity) {
        case 2:  break;              // notes: not counted
        case 4:  counter_block[0]++; break;  // remarks
        case 5:
        case 6:  counter_block[1]++; break;  // warnings
        case 7:
        case 8:  counter_block[2]++; break;  // errors
        case 9:
        case 10:
        case 11: counter_block[3]++; break;  // fatal
        default:
            assertion("update_diagnostic_counter: bad severity", error.c:3223);
    }
}
```

The primary counter block is at `qword_126ED80` (4 qwords: remark_count, warning_count, error_count, fatal_count). The global totals `qword_126ED90` (total errors) and `qword_126ED98` (total warnings) are updated from a different counter block `qword_126EDC8` after pragma-suppressed diagnostics are processed.

## Global Variables

| Variable | Address | Type | Purpose |
|---|---|---|---|
| `dword_106BBB8` | `0x106BBB8` | int | Output format: 0=text, 1=SARIF |
| `qword_106B478` | `0x106B478` | buffer* | SARIF JSON output buffer (0x400 initial) |
| `qword_106B488` | `0x106B488` | buffer* | Message text buffer (0x400 initial) |
| `qword_106B480` | `0x106B480` | buffer* | Location prefix buffer (0x80 initial) |
| `qword_1067820` | `0x1067820` | array* | Pragma diagnostic stack (24-byte entries) |
| `qword_1065960` | `0x1065960` | ptr[983] | Per-diagnostic suppression hash table |
| `qword_10658F8` | `0x10658F8` | ptr | bsearch scratch: best-match pragma entry |
| `byte_1067920` | `0x1067920` | byte[4*3795] | Default severity per error code |
| `byte_1067921` | `0x1067921` | byte[4*3795] | Current severity per error code |
| `byte_1067922` | `0x1067922` | byte[4*3795] | Per-error tracking flags (bit 2 = has pragma) |
| `dword_126ECA0` | `0x126ECA0` | int | Colorization requested |
| `dword_126ECA4` | `0x126ECA4` | int | Colorization active |
| `qword_126ECC0` | `0x126ECC0` | ptr[] | Color spec pointers (per category) |
| `qword_126ECC8` | `0x126ECC8` | size_t[] | Color spec lengths (per category) |
| `qword_126EDF0` | `0x126EDF0` | FILE* | Diagnostic output stream |

## Function Map

| Address | Name | EDG Source | Size | Role |
|---|---|---|---|---|
| `0x4EC850` | `parse_color_category` | error.c | 47 lines | Parse one `category=codes` from color spec |
| `0x4ECB10` | `write_sarif_physical_location` | error.c | 64 lines | Emit SARIF `physicalLocation` JSON |
| `0x4ECD20` | `bsearch_comparator` | error.c | 15 lines | Position comparator for pragma stack search |
| `0x4ECD50` | `check_suppression_flags` | error.c | 30 lines | Bit-flag suppression test |
| `0x4ECDD0` | `emit_colorization_escape` | error.c | 30 lines | Write ESC+category to buffer |
| `0x4ED100` | `create_file_index_entry` | error.c | 22 lines | Allocate 160-byte file-index node |
| `0x4ED170` | `display_to_internal_code` | error.c | 12 lines | Convert display code >= 20000 to internal |
| `0x4ED190` | `record_pragma_diagnostic` | error.c | 24 lines | Record pragma entry in hash table |
| `0x4ED240` | `check_pragma_diagnostic` | error.c | 39 lines | Hash-based per-diagnostic suppression check |
| `0x4EF8A0` | `write_sarif_message_json` | error.c | 79 lines | JSON-escape and wrap message text |
| `0x4F1330` | `check_severity` | error.c:3859 | 601 lines | Central dispatch, SARIF/text routing |
| `0x4F2C10` | `init_colorization` | error.c:825 | 43 lines | Parse color env vars, set up categories |
| `0x4F3020` | `update_diagnostic_counter` | error.c:3223 | 38 lines | Increment per-severity counters |
| `0x4F30A0` | `check_for_overridden_severity` | error.c:3803 | ~130 lines | Pragma stack walk with bsearch |
| `0x4F3E50` | `add_colorization_characters` | error.c:862 | ~80 lines | Emit full ANSI escape sequence |
| `0x5AEDB0` | `write_init` | host_envir.c:2017 | 28 lines | SARIF header / text-mode no-op |
| `0x5AEE00` | `write_signoff` | host_envir.c:2203 | 131 lines | SARIF footer / text-mode summary |
| `0x5AF770` | `check_terminal_capabilities` | host_envir.c | ~30 lines | TTY + TERM detection |
