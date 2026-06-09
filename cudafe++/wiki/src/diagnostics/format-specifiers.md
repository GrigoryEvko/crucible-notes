# Format Specifiers

The cudafe++ diagnostic system uses a custom format specifier language — not printf — to expand parameterized error messages. The expansion engine is `process_fill_in` (`sub_4EDCD0`, 1,202 decompiled lines in error.c), called by `write_message_to_buffer` (`sub_4EF620`, 159 lines) during template string expansion. Each diagnostic record carries a linked list of typed fill-in entries that supply the actual values — type nodes, entity pointers, strings, integers, source positions — which the format engine renders into the final message text.

This page documents the specifier syntax, the fill-in kind system, entity-kind dispatch, suffix options, numeric indexing, and the labeled fill-in mechanism.

## Specifier Syntax

When `write_message_to_buffer` walks an error template string (looked up from `off_88FAA0[error_code]`), it recognizes three format constructs:

| Syntax | Meaning | Example |
|---|---|---|
| `%%` | Literal `%` character | `"100%% complete"` |
| `%XY...Zn` | Fill-in specifier: letter `X`, options `Y...Z`, index `n` | `%nfd2`, `%sq1`, `%t` |
| `%[label]` | Named label fill-in reference | `%[class_or_struct]` |

### Positional Specifier Parsing

The parser (`sub_4EF620`, error.c:4703) processes `%XY...Zn` specifiers as follows:

```c
// After seeing '%', read next char as specifier letter
char spec_letter = template[pos + 1];      // 'T', 'd', 'n', 'p', 'r', 's', 't', 'u'
pos += 2;

// Collect option characters (a-z, A-Z) into buffer, max 29
int opt_count = 0;
char options[30];
while (true) {
    char c = template[pos];
    if (c >= '0' && c <= '9') {
        // Trailing digit = fill-in index (1-based)
        fill_in_index = c - '0';
        break;
    }
    if ((c & 0xDF) < 'A' || (c & 0xDF) > 'Y') {
        // Not a letter -- end of specifier, index defaults to 1
        fill_in_index = 1;
        break;
    }
    options[opt_count++] = c;
    if (opt_count > 29)
        assertion_handler("error.c", 4739,
            "write_message_to_buffer",
            "construct_text_message:",
            "too many option characters");
    pos++;
}
options[opt_count] = '\0';

process_fill_in(diagnostic_record, spec_letter, options, fill_in_index);
```

The maximum of 29 option characters is enforced by an assertion. In practice, specifiers use 0–3 option characters.

## Fill-In Kinds

The specifier letter maps to a fill-in kind value through a switch on `(letter - 84)` in `process_fill_in` (`sub_4EDCD0`, error.c:4297):

| Letter | ASCII | `letter - 84` | Kind | Payload Type | Description |
|---|---|---|---|---|---|
| `%T` | 84 | 0 | 6 | Type node pointer | Type name, uppercase rendering (`"<int, float>"`) |
| `%d` | 100 | 16 | 0 | `int64` | Signed decimal integer |
| `%n` | 110 | 26 | 4 | Entity node pointer | Entity/symbol name with rich formatting |
| `%p` | 112 | 28 | 2 | Source position cookie | Source file + line reference |
| `%r` | 114 | 30 | 7 | byte + pointer | Template parameter reference |
| `%s` | 115 | 31 | 3 | `const char*` | Plain string |
| `%t` | 116 | 32 | 5 | Type node pointer | Type name, lowercase rendering (`"int"`) |
| `%u` | 117 | 33 | 1 | `uint64` | Unsigned decimal integer |

Any other letter triggers the assertion: `"process_fill_in: bad fill-in kind"` (error.c:4297).

### Usage Frequency Across 3,795 Templates

Measured across all error message templates in `off_88FAA0`:

| Specifier | Occurrences | Typical Context |
|---|---|---|
| `%s` | ~470 | String fragments: attribute names, keyword text, flag names |
| `%t` | ~241 | Type names in mismatch diagnostics |
| `%sq` | ~233 | Quoted string fragments in CUDA cross-space messages |
| `%n` | ~179 | Entity names: function, variable, class, template |
| `%p` | ~76 | Source positions: "declared at line N of file.cu" |
| `%d` | ~60 | Numeric values: counts, limits, sizes |
| `%T` | ~40 | Type template parameter lists |
| `%u` | ~20 | Unsigned counts |
| `%r` | ~10 | Template parameter back-references |

## Fill-In Entry Layout

Each fill-in entry is a 40-byte node allocated from a pool (`qword_106B490`) or heap by `alloc_fill_in_entry` (`sub_4F2DE0`):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `kind` | Fill-in kind (0–7, from specifier letter mapping) |
| 4 | 1 | `used_flag` | Set to 1 when consumed during expansion |
| 5 | 3 | (padding) | — |
| 8 | 8 | `next` | Next fill-in in linked list |
| 16 | 8+ | `payload` | Union, varies by kind (see below) |

### Payload Layout by Kind

**Kind 0 (decimal, `%d`) / Kind 1 (unsigned, `%u`) / Kind 3 (string, `%s`) / Kind 5 (type, `%t`) / Kind 6 (type, `%T`):**

| Offset | Size | Field |
|---|---|---|
| 16 | 8 | `value` — int64 for kind 0/1, `const char*` for kind 3, type node pointer for kind 5/6 |

**Kind 2 (position, `%p`):**

| Offset | Size | Field |
|---|---|---|
| 16 | 8 | `position_cookie` — initialized to `qword_126EFB8` (current source position) at allocation time |

**Kind 4 (entity name, `%n`):**

| Offset | Size | Field |
|---|---|---|
| 16 | 8 | `entity_ptr` — pointer to entity node |
| 24 | 4 | `scope_index` — initialized to `0xFFFFFFFF` (invalid) |
| 28 | 1 | `full_qualification_flag` |
| 29 | 1 | `original_name_flag` |
| 30 | 1 | `parameter_list_flag` |
| 31 | 1 | `template_function_flag` |
| 32 | 1 | `definition_flag` |
| 33 | 1 | `alternate_original_flag` |
| 34 | 1 | `template_only_flag` |

**Kind 7 (`%r`):**

| Offset | Size | Field |
|---|---|---|
| 16 | 1 | `param_byte` |
| 17 | 7 | (padding) |
| 24 | 8 | `template_scope_ptr` |

### Fill-In Linked List

Fill-in entries attach to the diagnostic record as a singly-linked list:

- **Head pointer:** diagnostic record offset 184 (`fill_in_list_head`)
- **Tail pointer:** diagnostic record offset 192 (`fill_in_list_tail`)

When `process_fill_in` searches for a matching entry, it walks the list from head, looking for the first entry where `node->kind == requested_kind`. If the specifier includes an index (e.g., `%t2`), it skips `index - 1` matching entries before consuming the target:

```c
const __m128i *node = *(diagnostic + 184);   // fill_in_list_head
if (!node)
    goto fill_in_not_found;

while (node->kind != requested_kind || --index > 0) {
    node = node->next;                        // offset 8
    if (!node)
        goto fill_in_not_found;
}

node->used_flag = 1;                          // mark consumed (offset 4)
// proceed with kind-specific rendering
```

If no matching entry is found, `process_fill_in` triggers an assertion with a diagnostic message identifying the missing fill-in: `"specified fill-in (%X, N) not found for error string: \"...\""` (error.c:4317).

After all format specifiers have been expanded, `construct_text_message` (`sub_4EF9D0`) iterates the entire fill-in list and asserts that every entry has `used_flag == 1`. An unconsumed fill-in triggers: `"construct_text_message: not all fill-ins used for error string: \"...\""` (error.c:4781).

## Numeric Indexing

When a template string must reference multiple fill-ins of the same kind, a trailing digit selects which one:

| Specifier | Meaning |
|---|---|
| `%t` | First type fill-in (index 1, default) |
| `%t1` | First type fill-in (index 1, explicit) |
| `%t2` | Second type fill-in (index 2) |
| `%n1` | First entity name fill-in |
| `%n2` | Second entity name fill-in |
| `%sq1` | First string fill-in, quoted |
| `%sq2` | Second string fill-in, quoted |

The index is a single digit 0–9. Index 0 behaves identically to index 1 (the counter is pre-decremented before comparison). In practice, most templates use indices 1 and 2; a few use up to 3.

**Real template example** (CUDA cross-space call, error 3499):

```text
calling a __device__ function(%sq1) from a __host__ function(%sq2) is not allowed
```

Here `%sq1` and `%sq2` are both kind 3 (string) with option `q` (quoted), selecting the first and second string fill-ins respectively. The caller attaches two string fill-ins — the called function's name and the calling function's name.

## Suffix Options

### String Options (`%s`)

The `%s` specifier accepts only one option character: `q` for quoted output.

| Form | Rendering |
|---|---|
| `%s` | Raw string: `foo` |
| `%sq` | Quoted string: `"foo"` |

The `q` option wraps the string in double-quote characters (`"`) and applies colorization if enabled (quote category, code 6 = bold). Any other option character on `%s` triggers: `"process_fill_in: bad option"` (error.c:4364).

Multiple `q` characters are permitted syntactically (the parser loops over all option chars validating each is `q`) but have no additional effect — only one layer of quoting is applied.

### Entity Name Options (`%n`)

The `%n` specifier accepts a rich set of option suffixes that control how an entity is rendered. Options are processed left-to-right, setting flags on the fill-in entry's flag bytes (offsets 28–34):

| Option | Flag Byte | Effect |
|---|---|---|
| `f` | offset 28 (`full_qualification`) | Show fully-qualified name with namespace/class scope chain |
| `o` | offset 29 (`original_name`) | Omit the entity kind prefix (suppress "function ", "variable ", etc.) |
| `p` | offset 30 (`parameter_list`) | Show function parameter types in signature |
| `t` | offset 31 + offset 28 | Show template arguments AND full qualification (sets both flags) |
| `a` | offset 29 + offset 33 | Show original name AND alternate/accessibility info |
| `d` | offset 32 (`definition`) | Append declaration location: `" (declared at line N of file.cu)"` |
| `T` | offset 34 (`template_only`) | Show template specialization context: `" (from translation unit ...)"` |

Options can be combined. Common combinations from the error template table:

| Specifier | Rendering Example |
|---|---|
| `%n` | `function "foo"` |
| `%no` | `"foo"` (no kind prefix) |
| `%nf` | `function "ns::cls::foo"` (fully qualified) |
| `%nfd` | `function "ns::cls::foo" (declared at line 42 of bar.cu)` |
| `%nt` | `function "ns::cls::foo<int>"` (full + template args) |
| `%np` | `function "foo" [with parameters shown]` |
| `%nT` | `function "foo" (from translation unit bar.cu)` |
| `%na` | `"foo" based on template argument(s) ...` |

### No Options for Other Kinds

The `%d`, `%u`, `%p`, `%t`, `%T`, and `%r` specifiers reject all option characters:

```c
if (*options != '\0')
    assertion_handler("error.c", 4372,
        "process_fill_in",
        "process_fill_in: bad option", NULL);
```

## Kind-Specific Rendering

### Kind 0 — Signed Decimal (`%d`)

Renders the 64-bit signed integer payload using `snprintf(buf, 20, "%lli", value)`, then writes the result to the output buffer. The 20-character buffer accommodates the full range of `int64_t` values including the sign.

### Kind 1 — Unsigned Decimal (`%u`)

Formats the payload through `sub_4F63D0`, which renders the unsigned 64-bit value into a dynamically-sized string buffer.

### Kind 2 — Source Position (`%p`)

Calls `sub_4F6820` (`form_source_position`) with the position cookie from the fill-in payload. The rendering includes:

- File name (via `sub_5B15D0` for display formatting)
- Line number
- Contextual text supplied by the caller through three string arguments (prefix, suffix, end-of-source fallback)

The caller passes context strings like `" (declared "`, `")"`, `"(at end of source)"` to frame the position reference. When the position resolves to line 0 or the file is `"-"` (stdin), alternate formats are used.

### Kind 3 — String (`%s` / `%sq`)

Without the `q` option, writes the string pointer payload directly to the output buffer via `strlen` + `sub_6B9CD0` (buffer append).

With the `q` option, wraps the string in double quotes with colorization:

```c
if (colorization_active)
    emit_escape(buffer, 6);       // quote color (bold)
write_char(buffer, '"');
write_string(buffer, payload);
if (colorization_active)
    emit_escape(buffer, 1);       // reset
write_char(buffer, '"');
```

### Kind 5 — Type, Lowercase (`%t`)

Renders the type node through the type formatting subsystem. The rendering pipeline:

1. Set `byte_10678FA = 1` (name lookup kind = type display mode)
2. Write opening `"`
3. Call `sub_600740` (format type for display) with the type node and the entity formatter callback table (`qword_1067860`)
4. Write closing `"`
5. Check via `sub_7BE9C0` if the type has an "aka" (also-known-as) desugared form
6. If yes, append `' (aka "desugared_type")'` — comparing the rendered forms to avoid redundant output when they are identical

The aka check compares the rendered text of the original type against the desugared type. If they produce identical strings (same length, same content via `strncmp`), the aka suffix is suppressed by truncating the buffer back to the pre-aka position.

### Kind 6 — Type, Uppercase (`%T`)

Renders a type template argument list in angle brackets:

```c
write_string(buffer, "\"<");
// Walk the template argument linked list
for (arg = payload; arg != NULL; arg = arg->next) {
    if (arg->kind != 3)   // skip pack expansion markers
        format_template_argument(arg, &entity_formatter);
    if (arg->next && arg->next->kind != 3)
        write_string(buffer, ", ");
}
write_string(buffer, ">\"");
```

Template argument entries with `kind == 3` (at byte offset +8) are pack-expansion markers and are skipped during rendering.

### Kind 7 — Template Parameter Reference (`%r`)

Renders a template parameter by looking up the parameter entity through `sub_5B9EE0` (entity lookup by scope + index). If found and non-null, renders via `sub_4F3970` (unqualified entity name). Otherwise, falls back to `sub_6011F0` (generic template parameter formatting).

## Entity Kind Dispatch (`%n`)

When processing `%n` specifiers, `process_fill_in` reads the entity kind byte at offset 80 of the entity node and dispatches to kind-specific rendering logic. The function first resolves through projection indirection: if `entity_kind == 16` (typedef), it follows the pointer at `entity->info_ptr->pointed_to`; if `entity_kind == 24` (resolved namespace alias), it follows `entity->info_ptr`.

The dispatch handles 25 entity kind values (0–24, with gaps at 14/15/16/24 handled as special cases):

| Entity Kind | Value | Kind Label String | Index in `off_88FAA0` | Rendering Logic |
|---|---|---|---|---|
| keyword | 0 | (none — literal `"keyword"`) | — | Write `keyword "`, then the keyword's name string from `entity->name_sym->name` |
| concept | 1 | (from table) | 1462 | Simple: write kind label + quoted name |
| constant template parameter | 2 | `"constant"` or `"nontype"` | — | Check template parameter subkind: type_kind 14 with subkind 2 = `"nontype"`, else `"constant"` |
| template parameter | 3 | (from table) | 1464 or 1465 | Check whether the template parameter is a type parameter (type_kind != 14) → index 1465, else 1464 |
| class | 4 | (from table, CUDA-aware) | 1466–1468 | CUDA mode: `1467` or `1468` (class vs struct); non-CUDA: `1466` |
| struct | 5 | (same as class) | 1466–1468 | Same dispatch as class, differentiated by `v46 != 5` |
| enum | 6 | (from table) | 1472 | Simple: write kind label + quoted name |
| variable | 7 | `"variable"` or `"handler parameter"` | 1474 or 1475 | Check handler-parameter flag (offset 163, bit 0x40). If set: `"handler parameter"` (index 1474). If variable is a structured binding (offset 162, bit 1): use index 2937. Otherwise: `"variable"` (index 1475) with optional template context |
| field | 8 | `"field"` or `"member"` | 1480 or 1481 | CUDA C++ mode: `"member"` (index 1480); C mode: `"field"` (index 1481) |
| member | 9 | `"member"` | 1480 | Always `"member"` with optional template context from scope chain |
| function | 10 | `"function"` or `"deduction guide"` | 1478 or 2892 | Check linkage kind (offset 166 == 7): deduction guide → index 2892. Otherwise `"function"` (1478). Walk qualified type chain to strip cv-qualifiers |
| function overload | 11 | (same as function) | 1478 or 2892 | Same dispatch as function (case 10), merged in the switch |
| namespace | 12 | (from table) | 1463 | Simple: write kind label + quoted name |
| label | 13 | (none) | — | Write quoted name only, no kind prefix, no type info |
| typedef (indirect variable) | 14 | `"variable"` | 1475 | Dereferences through `entity->info_ptr->pointed_to` and renders as variable |
| typedef (indirect function) | 15 | `"function"` | 1478 | Dereferences through `entity->info_ptr`, extracts function entity + routine info |
| typedef | 16 | — | — | Assertion: `"form_symbol_summary: projection of projection kind"` (error.c:2020). Should have been resolved before dispatch |
| using declaration | 17 | (from table) | 1479 | Simple: write kind label + quoted name |
| parameter | 18 | `"parameter"` | 1473 | Simple: write `"parameter"` + quoted name with type info |
| class (anonymous/unnamed) | 19 | (from table) | 1469–1471 or 1889 | Multiple sub-cases: anonymous class bit 0x40 → index 1469; class-template with bit 0x02 → index 1470; deduction_guide bit → index 1889; else index 1471 |
| function template | 20 | `"function template"` | 1485 (lambda) or kind label | Lambda function (offset 189, bit 0x20): index 1485 with scope entity. Otherwise: `"function template"` with type and parameter info |
| variable template | 21 | (from table) | 2750 | Simple: write kind label + quoted name |
| alias template | 22 | (from table) | 3050 | Simple: write kind label + quoted name |
| concept template | 23 | (from table) | 1482 | Simple: write kind label + quoted name |
| resolved namespace alias | 24 | — | — | Assertion: `"form_symbol_summary: projection of projection kind"` (same as kind 16). Should have been resolved |

Any entity kind value outside 0–24 (excluding the gaps that trigger assertions) hits the default case: `"form_symbol_summary: unsupported symbol kind"` (error.c:2023).

### Entity Rendering Pipeline

For entity kinds that produce a fully-formatted name (most non-trivial cases), the rendering proceeds through these stages:

```text
1. Write entity kind label string (e.g., "function ")
   └── sub_6B9EA0(buffer, kind_label_string)
   └── sub_6B9CD0(buffer, " ", 1)

2. Open quote
   └── Optional colorization: sub_4ECDD0(buffer, 6)   // quote color
   └── sub_6B9CD0(buffer, "\"", 1)

3. Render type prefix (if has_type_info and full_qualification)
   └── sub_5FE8B0(type_node, 0, 1, 0, 0, &entity_formatter)

4. Render qualified or unqualified name
   ├── With template context:  sub_737A00(entity, &entity_formatter)
   └── Without template context: sub_4F3970(entity)

5. Render function parameters (if applicable)
   ├── Full parameter types: sub_5FB270(type, 0, 0, &entity_formatter)
   └── Simple type suffix:   sub_6016F0(type, &entity_formatter)

6. Close quote
   └── sub_6B9CD0(buffer, "\"", 1)
   └── Optional colorization: sub_4ECDD0(buffer, 1)   // reset

7. Append accessibility info (if 'a' option)
   └── " based on template argument(s) "
   └── sub_5FA660(template_arg_list, 0, &entity_formatter)

8. Append declaration location (if 'd' option)
   └── sub_4F6820(position, diag, " (declared ", ")", "(at end of source)")

9. Append translation unit info (if 'T' option)
   └── " (from translation unit <filename>)"
```

The `original_name` flag (`o` option) suppresses steps 1 and 3, rendering only the bare quoted name without a kind prefix or type qualification. The `full_qualification` flag (`f` option) enables step 3 and uses `sub_737A00` for fully-qualified name rendering in step 4. The `parameter_list` flag (`p` option) forces step 5 to include full parameter-type rendering.

### Template Context in Entity Names

When `dword_126E274` (show template arguments) is non-zero and the entity has template context, the renderer can walk up the template scope chain:

1. Access the entity's routine info (for functions: offset 88 → offset 192 → offset 16)
2. Check for the instantiated-from entity (offset 104 of scope info, guarded by `!(offset_176 & 1)`)
3. If found, use the instantiated-from entity as the display target
4. For class templates (entity_kind == 20): walk the template parameter chain, rendering `<param1, param2, ...>` with pack-expansion markers (`...`) for variadic parameters

### CUDA-Specific Entity Rendering

Several entity kinds have CUDA-aware rendering paths:

- **Class/struct (kinds 4/5):** When `dword_126EFB4 == 2` (CUDA C++ mode) and the entity has an anonymous flag (offset 161, bit 0x80), rendering jumps to the anonymous-class handler (kind 19) instead
- **Field (kind 8):** In CUDA C++ mode, the kind label is `"member"` (index 1480); in C mode, it is `"field"` (index 1481)
- **Class/struct label selection:** In CUDA C++ mode, the kind label index is always 1467; in non-CUDA mode, it depends on whether the entity is class vs struct

## Labeled Fill-Ins (`%[label]`)

The `%[label]` syntax references a named fill-in from the label table at `off_D481E0`. This mechanism allows error templates to include conditional text fragments that vary based on language mode or compilation context.

### Label Table Structure

`off_D481E0` is an array of 24-byte entries (3 pointers per entry):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `name` | Label name string (e.g., `"class_or_struct"`) |
| 8 | 8 | `condition_ptr` | Pointer to condition flag (dword) |
| 16 | 4 | `true_index` | String table index when `*condition_ptr != 0` |
| 20 | 4 | `false_index` | String table index when `*condition_ptr == 0` |

### Label Lookup Algorithm

```c
// write_message_to_buffer, error.c:4714
char *label_start = template + pos + 2;      // skip "%["
char *label_end = strchr(template + pos + 1, ']');
if (!label_end)
    assertion_handler("error.c", 4714, "write_message_to_buffer", NULL, NULL);

size_t label_len = label_end - label_start;

// Walk off_D481E0 table
struct label_entry *entry = off_D481E0;
while (entry->name) {
    if (strncmp(entry->name, label_start, label_len) == 0) {
        // Found matching label
        int string_index;
        if (*entry->condition_ptr)
            string_index = entry->true_index;
        else
            string_index = entry->false_index;

        if (string_index > 3794)
            error_text_invalid_code();     // sub_4F2D30

        // Expand the referenced string directly into the buffer
        const char *text = off_88FAA0[string_index];
        write_to_buffer(buffer, text, strlen(text));
        pos = label_end + 1;
        break;
    }
    entry++;   // advance by 24 bytes
}

if (!entry->name) {
    // Label not found -- fatal
    fprintf(stderr, "missing fill-in label: %.*s\n", label_len, label_start);
    assertion_handler("error.c", 430,
        "get_label_fill_in_entry",
        "get_label_fill_in_entry: no label fill-in found", NULL);
}
```

The label table entries reference string indices in the same `off_88FAA0` table used for error messages. This allows a single error template to produce different text depending on compilation mode — for example, using `"class"` vs `"struct"` based on a language-mode flag, or `"virtual"` vs `""` based on a feature flag.

The label text is written directly to the output buffer without further format specifier processing — labels cannot contain nested `%` specifiers.

## Output Buffer

All rendering targets the global message text buffer at `qword_106B488`:

- Initial allocation: 0x400 bytes (1 KB) via `sub_6B98A0`
- Dynamic growth: `sub_6B9B20` doubles the buffer when capacity is exceeded
- String append: `sub_6B9CD0(buffer, data, length)` — the workhorse write function
- String write: `sub_6B9EA0(buffer, string)` — convenience wrapper (calls `strlen` + `sub_6B9CD0`)

The entity display callback infrastructure at `qword_1067860` allows the type/name formatting subsystem to write to the same buffer through an indirect call:

| Variable | Address | Purpose |
|---|---|---|
| `qword_1067860` | `0x1067860` | Entity formatter callback (set to `sub_5B29C0`) |
| `qword_1067870` | `0x1067870` | Entity formatter output buffer (set to `qword_106B488`) |
| `byte_10678F1` | `0x10678F1` | C mode flag (`dword_126EFB4 == 1`) |
| `byte_10678F4` | `0x10678F4` | Pre-C++11 flag |
| `byte_10678FA` | `0x10678FA` | Name lookup kind (saved/restored around type rendering) |
| `byte_10678FE` | `0x10678FE` | Entity display flags (saved/restored around `%n` processing) |
| `byte_1067902` | `0x1067902` | Type desugaring mode flag (saved/restored around `%t` aka rendering) |

## Colorization Interaction

When `dword_126ECA4` (colorization active) is non-zero, the format engine inserts ANSI escape sequences around quoted names and type references:

| Context | Color Code | ANSI Sequence | Visual |
|---|---|---|---|
| Opening quote (`"`) | 6 (quote) | `\033[01m` | Bold |
| Closing quote (`"`) | 1 (reset) | `\033[0m` | Normal |
| Type rendering context | (inherited) | — | Inherits from diagnostic severity color |

The escape sequences are emitted by `sub_4ECDD0(buffer, color_code)`. The color codes correspond to the categories parsed from `EDG_COLORS` / `GCC_COLORS` environment variables during initialization.

## Function Map

| Address | Name (Recovered) | Size | Role |
|---|---|---|---|
| `0x4EDCD0` | `process_fill_in` | 1,202 lines | Core format specifier expansion |
| `0x4EF620` | `write_message_to_buffer` | 159 lines | Template string walker, `%` parser |
| `0x4F2DE0` | `alloc_fill_in_entry` | 41 lines | Pool allocator for 40-byte fill-in nodes |
| `0x4F2D30` | `error_text_invalid_code` | 12 lines | Assert on invalid error code (> 3794) |
| `0x4F2930` | `assertion_handler` | 101 lines | `__noreturn`, 5,185 callers |
| `0x4F3480` | `format_assertion_message` | ~100 lines | Multi-arg string builder for assertion text |
| `0x4F6820` | `form_source_position` | ~130 lines | Render `%p` source position with file + line |
| `0x4F3970` | `format_entity_unqualified` | — | Render unqualified entity name |
| `0x4F39E0` | `format_entity_with_template` | — | Render entity with template args + accessibility |
| `0x737A00` | `format_qualified_name` | — | Render fully-qualified name through scope chain |
| `0x5FE8B0` | `format_type_with_qualifiers` | — | Render type with cv-qualifiers for `%n` prefix |
| `0x5FB270` | `format_function_parameters` | — | Render function parameter type list |
| `0x6016F0` | `format_simple_type` | — | Render simple type suffix |
| `0x600740` | `format_type_for_display` | — | Render type for `%t` specifier |
| `0x7BE9C0` | `has_desugared_type` | — | Check if type has an "aka" form |
| `0x5FA660` | `format_template_argument_list` | — | Render template argument list for `%n` `a` option |
| `0x5FA0D0` | `format_template_argument` | — | Render single template argument for `%T` |
| `0x5B9EE0` | `lookup_entity_by_scope` | — | Entity lookup for `%r` template parameter |
| `0x4F63D0` | `format_unsigned_decimal` | — | Render unsigned integer for `%u` |
| `0x6B9CD0` | `buffer_append` | — | Write bytes to dynamic buffer |
| `0x6B9EA0` | `buffer_write_string` | — | Write null-terminated string to buffer |
| `0x4ECDD0` | `emit_colorization_escape` | — | Emit ANSI escape sequence |

## Cross-References

- [Diagnostic Overview](./overview.md) — 7-stage pipeline, severity levels, diagnostic record layout
- [CUDA Error Catalog](./cuda-errors.md) — all 338 CUDA-specific error templates with specifier usage
- [SARIF & Pragma Control](./sarif-pragmas.md) — SARIF JSON output and `#pragma nv_diagnostic` system
