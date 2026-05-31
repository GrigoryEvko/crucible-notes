# Backend Code Generation

The backend is the final stage of the cudafe++ pipeline (stage 7 in the [overview](./overview.md)). It lives in a single function, `process_file_scope_entities` (`sub_489000`, 723 decompiled lines, 4520 bytes), whose job is to walk the EDG source sequence produced by the frontend and emit a `.int.c` file that the host C++ compiler (gcc, clang, or cl.exe) can compile. The function resides in `cp_gen_be.c` at EDG source lines around 19916-26628, and it delegates per-entity code generation to `gen_template` (`sub_47ECC0`, 1917 decompiled lines), which dispatches on entity kind to specialized generators for variables, types, routines, namespaces, and templates.

The backend is gated by the skip-backend flag (`dword_106C254`): if set to 1 (errors occurred during the frontend), `main()` never calls `sub_489000` and proceeds directly to exit.

## Key Facts

| Property | Value |
|---|---|
| Function | `sub_489000` (`process_file_scope_entities`) |
| Binary address | `0x489000` |
| Binary size | 4520 bytes (723 decompiled lines) |
| EDG source | `cp_gen_be.c` |
| Callees | ~140 distinct call targets |
| Output | `.int.c` file (or stdout when filename is `"-"`) |
| Main dispatcher | `sub_47ECC0` (`gen_template`, 1917 lines) |
| Host reference emitter | `sub_6BCF80` (`nv_emit_host_reference_array`) |
| Module ID writer | `sub_5B0180` (`write_module_id_to_file`) |
| Skip-backend flag | `dword_106C254` |
| Backend timing label | `"Back end time"` |

## Output Primitives

All output to the `.int.c` file passes through a small set of character-level emitters. Understanding these is essential for reading the decompiled backend code, since every line of generated C/C++ is assembled from these calls:

| Function | Address | Identity | Behavior |
|---|---|---|---|
| `sub_467D60` | `0x467D60` | `emit_newline` | Writes `\n` via `putc(10, stream)`. Increments `dword_1065820` (line counter). Resets `dword_106581C` (column counter) and `dword_1065830` to 0. Calls `sub_403730` (write error abort) on failure. |
| `sub_467DA0` | `0x467DA0` | `emit_line_directive` | Checks `dword_1065818` (needs-line-directive flag). If the current source position (`qword_1065810`) differs from the output line counter, calls `sub_467EB0` to emit a `#line N "file"` directive. Resets `dword_1065818` to 0. Handles close-range line gaps (within 5 lines) by emitting blank lines instead of a `#line` directive. |
| `sub_467E50` | `0x467E50` | `emit_string` | If `dword_1065818` is set, calls `emit_line_directive` first. Writes each character of the string via `putc`. Increments `dword_106581C` by the string length. |
| `sub_467EB0` | `0x467EB0` | `emit_line_number` | Emits `#line N "file"` or `# N "file"` (short form when `dword_106C28C` or MSVC EDG-native mode is set). Constructs the directive in a stack buffer starting with `#line `, appends the decimal line number, then the quoted filename via `sub_5B1940`. Sets `dword_1065820` to the target line number. Resets column counters. |
| `sub_468150` | `0x468150` | `emit_char` | If `dword_1065818` is set, calls `emit_line_directive` first. Writes a single character via `putc`. Increments `dword_106581C` by 1. |
| `sub_468190` | `0x468190` | `emit_raw_string` | Like `emit_string` but without `strlen` -- walks the string character by character, incrementing `dword_106581C` per character. Calls `emit_line_directive` first if `dword_1065818` is set. |
| `sub_468270` | `0x468270` | `emit_decimal` | Writes an unsigned integer as decimal digits. Has fast paths for 1-5 digit numbers (manual digit extraction via division by powers of 10). Falls back to `sub_465480` (sprintf-style) for larger numbers. Calls `emit_line_directive` first if needed. |
| `sub_46BC80` | `0x46BC80` | `emit_line_start` | If the column counter is nonzero, first emits a newline. Increments `dword_1065834` (indent level). Calls `emit_line_directive` if needed. Then writes the string character by character. Used for the first token on a new line (e.g., `#define`, `#ifdef`). |

### Output State Variables

| Variable | Address | Type | Role |
|---|---|---|---|
| `stream` | `0x106583x` | `FILE*` | Output file handle for `.int.c` |
| `dword_1065834` | `0x1065834` | int | Indent level counter. Incremented by `emit_line_start`, decremented after each directive block. Not used for actual indentation emission -- tracks logical nesting depth for `#line` management. |
| `dword_1065820` | `0x1065820` | int | Output line counter. Tracks the current line number in the generated `.int.c` file. Incremented by every `\n` written. |
| `dword_106581C` | `0x106581C` | int | Output column counter. Tracks the current column position. Reset to 0 after each newline. |
| `dword_1065830` | `0x1065830` | int | Column counter after last newline (secondary tracking). Reset to 0 with `dword_106581C`. |
| `dword_1065818` | `0x1065818` | int | Needs-line-directive flag. Set to 1 when the source position changes. Checked by every output primitive; when set, a `#line` directive is emitted before the next output. |
| `qword_1065810` | `0x1065810` | qword | Current source position (line number from the original `.cu` file). Updated when processing each entity. |
| `qword_1065828` | `0x1065828` | qword | Current source file index. Compared against new file references to decide whether to emit a `#line` with filename. |
| `qword_126EDE8` | `0x126EDE8` | qword | Mirror of `qword_1065810`. Updated in parallel; used by other subsystems to query current position. |

## Execution Flow

The backend proceeds through seven sequential phases within `sub_489000`:

```text
sub_489000 (process_file_scope_entities)
  |
  |-- Phase 1: State initialization (40+ globals zeroed, 4 buffers cleared)
  |-- Phase 2: Output file opening (.int.c or stdout)
  |-- Phase 3: Boilerplate emission (GCC diagnostics, managed runtime, lambda macros)
  |-- Phase 4: Main entity loop (walk source sequence, dispatch to gen_template)
  |-- Phase 5: Empty file guard + scope unwind (sub_466C10)
  |-- [optional] Breakpoint placeholders (qword_1065840 list)
  |-- Phase 6: File trailer (#line, _NV_ANON_NAMESPACE, #include, #undef)
  |-- Phase 7: Host reference arrays (sub_6BCF80 x 6, conditional on dword_106BFD0/BFCC)
  |
  +-- sub_4F7B10: close output file (ID 1701)
```

## Phase 1: State Initialization

The function begins by zeroing approximately 40 global variables and clearing four large buffers. This ensures no state leaks between compilation units (relevant in the recompilation loop, though in practice `sub_489000` runs exactly once).

### Scalar Zeroing

The first 20 lines of the decompiled function zero individual globals:

```c
dword_1065834 = 0;   // indent level
dword_1065830 = 0;   // column after newline
stream        = 0;   // FILE* handle
qword_126EDE8 = 0;   // current source position (low 6 bytes)
qword_1065828 = 0;   // current file index
dword_1065820 = 0;   // output line counter
dword_106581C = 0;   // output column counter
dword_1065818 = 0;   // needs-line-directive flag
qword_1065748 = 0;   // source sequence cursor
qword_1065740 = 0;   // alternate source sequence cursor
qword_126C5D0 = 0;   // (template instantiation tracking)
dword_106573C = 0;
dword_1065734 = 0;
dword_1065730 = 0;
dword_106572C = 0;
qword_1065708 = 0;   // scope stack head
qword_1065720 = 0;   // scope free list
qword_1065700 = 0;   // scope pool head
dword_10656FC = 0;   // current access specifier
// ... additional counters, flags, sequence pointers
```

Additional globals zeroed later (after the callback setup):

```c
dword_1065758 = 0;   dword_1065754 = 0;   dword_1065750 = 0;
dword_10656F8 = 0;   dword_10656F4 = 0;
qword_1065718 = 0;   qword_1065710 = 0;
dword_1065728 = 0;   qword_F05708  = 0;
```

### Buffer Clearing

Four `memset` calls clear hash tables / lookup buffers:

| Buffer Base | Size (hex) | Size (decimal) | Description |
|---|---|---|---|
| `unk_FE5700` | `0x7FFE0` | 524,256 bytes (~512 KB) | Entity lookup hash table |
| `unk_F65720` | `0x7FFE0` | 524,256 bytes (~512 KB) | Type lookup hash table |
| `qword_E85720` | `0x7FFE0` | 524,256 bytes (~512 KB) | Declaration tracking table |
| `xmmword_F05720` | `0x5FFE8` | 393,192 bytes (~384 KB) | Scope/name resolution table |

Total: approximately 1.93 MB of memory zeroed at backend entry.

### Callback Table Setup

After zeroing, the function initializes two tables of function pointers:

**gen_be_info callbacks** (6 entries at `xmmword_1065760..10657B0`):

```c
sub_5F9040(&xmmword_1065760);    // clear the table first
xmmword_1065760 = off_83BD60;    // callback 0: expression gen
xmmword_1065778 = off_83BD68;    // callback 1: type gen
xmmword_1065788 = off_83BD70;    // callback 2: declaration gen
xmmword_10657A0 = off_83BD78;    // callback 3: statement gen
xmmword_10657B0 = qword_83BD80;  // callback 4: scope gen
```

These pointers are loaded from read-only data via SSE (`_mm_loadh_ps`), packing two 8-byte function pointers per 16-byte XMM value.

**Direct callback assignments** (4 entries):

| Variable | Address | Value | Identity |
|---|---|---|---|
| `qword_10657C0` | `0x10657C0` | `sub_46BEE0` | gen_statement_expression (only set when not in MSVC `__declspec` mode) |
| `qword_10657C8` | `0x10657C8` | `loc_469200` | gen_type_operator_expression |
| `qword_10657D0` | `0x10657D0` | `sub_466F40` | gen_be_helper_1 |
| `qword_10657D8` | `0x10657D8` | `sub_4686C0` | gen_be_helper_2 |

### Host Compiler Version Detection

A block of conditionals determines warning suppression behavior based on the host compiler version:

```c
byte_10657F0 = 1;                        // always set
byte_10657F1 = byte_126EBB0;             // copy verbose-line-dir flag
if (dword_126EFB4 == 2                   // CUDA mode
    || dword_126EF68 <= 199900)          // C++ standard <= C++98
{
    byte_10657F4 = (dword_126EFB0 != 0); // copy flag
} else {
    byte_10657F4 = 1;                    // force on for newer standards
}
```

The `byte_1065803` flag is set to 1 when MSVC mode (`dword_126E1D8`) is active or when the GNU/Clang version falls in a specific range (version check `qword_126E1F0 - 40500` with tolerance of 2, i.e., Clang versions 40500-40502).

### Scope Stack Allocation

A dynamic scope tracking structure is allocated (or resized if it exists from a prior run):

```c
if (qword_10656E8) {
    // resize existing: realloc to 16 * (count + 1) bytes
    sub_6B74D0(*(qword_10656E8), 16 * (*(qword_10656E8 + 8) + 1));
} else {
    // allocate fresh: 16-byte header
    v0 = sub_6B7340(16);
    qword_10656E8 = v0;
}
// allocate 1024-byte data block, zero it, attach to header
v2 = sub_6B7340(1024);
// zero 1024 bytes in 16-byte steps (zeroing 64 pointer-sized slots)
*v0 = v2;
v0[1] = 63;   // capacity = 63 entries
```

This creates a 64-slot lookup table (63 usable entries plus sentinel) for tracking entity references during code generation.

## Phase 2: Output File Opening

The function opens the output `.int.c` file. Two paths are possible:

**Stdout mode:** If the output filename (`qword_126EEE0`) equals `"-"`, the function sets `stream = stdout`.

```c
// strcmp(qword_126EEE0, "-")
if (filename_is_dash) {
    stream = stdout;
}
```

**File mode:** Otherwise, the function constructs the output path by appending `.int.c` to the base filename (stripping the original extension):

```c
v55 = qword_106BF20;                       // pre-set output path (CLI override)
if (!v55)
    v55 = sub_5ADD90(qword_126EEE0, ".int.c");  // derive_name: strip ext, add ".int.c"
stream = sub_4F48F0(v55, 0, 0, 0, 1701);   // open_output_file (mode 1701)
```

The `sub_5ADD90` function (`derive_name`) finds the last `.` in the filename, strips the extension, and appends `.int.c`. It handles multi-byte UTF-8 characters correctly when scanning for the dot position. The constant `1701` is the file descriptor identifier used by the file management subsystem.

After opening the file, `sub_5B9A20` is called to initialize the output stream state, and `sub_467EB0` emits the initial `#line 1` directive.

## Phase 3: Boilerplate Emission

Before processing any user declarations, the backend emits several blocks of boilerplate that the host compiler needs. The exact output depends on the host compiler identity (Clang, GCC, MSVC) and the CUDA mode.

### GCC Diagnostic Suppressions

Multiple `#pragma GCC diagnostic` directives suppress host compiler warnings that would be spurious for generated code:

```c
// Conditional on Clang version > 30599 (0x7787) or GNU version > 40799 (0x9F5F)
#pragma GCC diagnostic ignored "-Wunused-local-typedefs"

// Conditional on dword_126EFA8 (attribute mode) && dword_106C07C
#pragma GCC diagnostic ignored "-Wattributes"

// Clang or recent GNU/Clang:
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wunused-function"

// Clang-specific additional suppressions:
#pragma GCC diagnostic ignored "-Wunused-private-field"
#pragma GCC diagnostic ignored "-Wunused-parameter"
```

The version thresholds use the encoded host compiler version from `qword_126EF90` (Clang version) and `qword_126E1F0` (GCC/Clang combined version):

| Hex constant | Decimal | Approximate version |
|---|---|---|
| `0x7787` | 30,599 | Clang ~3.x |
| `0x9D07` | 40,199 | GCC/Clang ~4.0 |
| `0x9E97` | 40,599 | GCC/Clang ~4.1 |
| `0x9F5F` | 40,799 | GCC/Clang ~4.1+ |

### Managed Runtime Boilerplate

A block of C code is emitted unconditionally for `__managed__` variable support:

```c
static char __nv_inited_managed_rt = 0;
static void **__nv_fatbinhandle_for_managed_rt;
static void __nv_save_fatbinhandle_for_managed_rt(void **in) {
    __nv_fatbinhandle_for_managed_rt = in;
}
static char __nv_init_managed_rt_with_module(void **);
```

Followed by the inline initialization helper:

```c
__attribute__((unused))                    // added when dword_106BF6C (alt host mode) is set
static inline void __nv_init_managed_rt(void) {
    __nv_inited_managed_rt = (__nv_inited_managed_rt
        ? __nv_inited_managed_rt
        : __nv_init_managed_rt_with_module(__nv_fatbinhandle_for_managed_rt));
}
```

This boilerplate is surrounded by a `#pragma GCC diagnostic push` / `pop` pair to suppress warnings about unused variables/functions in the boilerplate itself.

After the pop, additional `#pragma GCC diagnostic ignored` directives may be emitted for the remainder of the file (outside the push/pop scope), depending on compiler version.

### Lambda Detection Macros

When extended lambda mode (`dword_106BF38`) is NOT active, three stub macro definitions are emitted:

```c
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
```

Followed by a self-checking `#if defined` block:

```c
#if defined(__nv_is_extended_device_lambda_closure_type) \
 && defined(__nv_is_extended_host_device_lambda_closure_type) \
 && defined(__nv_is_extended_device_lambda_with_preserved_return_type)
#endif
```

When extended lambda mode IS active, these macros are not emitted -- the frontend's keyword registration has already defined them as built-in type traits recognized by the parser. The empty `#if defined / #endif` block serves as a guard that downstream tools can detect.

## Phase 4: Main Entity Loop

This is the core of the backend. The source sequence cursor `qword_1065748` is initialized from the file scope IL node's declaration list at offset `+256`: `qword_1065748 = *(*(xmmword_126EB60 + 8) + 256)`, where the high qword of `xmmword_126EB60` points to the file scope root (set during `fe_wrapup`). The cursor walks this linked list of top-level declarations in the order they appeared in the source file. For each entry, it dispatches based on the entry's kind field at offset `+16`.

### Source Sequence Entry Structure

Each source sequence entry has this layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `next` | Pointer to next entry in the linked list |
| +8 | 1 | `sub_kind` | Sub-classification within the kind |
| +9 | 1 | `skip_flag` | If nonzero, entry has already been processed |
| +16 | 1 | `kind` | Entry kind (see dispatch table below) |
| +24 | 8 | `entity` | Pointer to the EDG entity node for this declaration |
| +32 | 8 | `source_position` | Source file/line encoding |
| +48 | 8 | `pragma_text` | For pragma entries: pointer to raw pragma string |
| +56 | 8 | `stdc_kind / pragma_data` | STDC pragma kind or additional pragma metadata |
| +57 | 1 | `stdc_value` | STDC pragma value (ON/OFF/DEFAULT) |

### Dual-Cursor Iteration

The loop uses two cursors -- `qword_1065748` (primary) and `qword_1065740` (alternate) -- to handle pragma interleavings. When the primary cursor encounters a kind-53 entry (a continuation marker), it switches to the alternate cursor. This mechanism handles the case where pragmas are interleaved between parts of a single declaration:

```c
for (i = qword_1065748; i != NULL; ) {
    if (entry_kind(i) == 53) {          // continuation marker
        // save as alternate, follow continuation chain
        alt_cursor = i;
        i = *(i->entity + 8);          // follow entity's next pointer
        continue;
    }
    if (entry_kind(i) == 57) {          // pragma interleave
        entity = i->entity;
        // advance past pragma entries to find next real entity
        for (i = i->next; i && entry_kind(i) == 53; ) {
            alt_cursor = i;
            i = *(i->entity + 8);
        }
        // handle the pragma inline (see below)
        ...
    } else {
        // non-pragma entity: dispatch to gen_template
        sub_47ECC0(0);
    }
}
```

When the primary cursor is exhausted and an alternate cursor exists, the primary takes the alternate's next pointer and continues. This ensures correct ordering when pragmas split a declaration sequence.

### Full Main Loop Pseudocode

The following pseudocode is derived from the decompiled `sub_489000` (lines 288-558) and shows the complete dispatch logic. The variable `v12` tracks whether any non-pragma entity was emitted (used by the empty file guard in Phase 5). The variable `v14` saves/restores `byte_10657FB` across pragma handling.

```c
// Initialize source sequence cursor from file scope node
qword_1065748 = *(xmmword_126EB60_high + 256);  // source sequence list head
byte_10656F0 = (dword_126EFB4 != 2) + 2;        // linkage: 3=C++, 2=C
sub_466E60(...);                                  // init output state
v12 = 0;                                          // no entities emitted yet

while (1) {
    v14 = byte_10657FB;                           // save pragma-in-progress flag

    i = qword_1065748;                            // primary cursor
    alt = qword_1065740;                          // alternate cursor
    modified_primary = false;
    modified_alt = false;

    while (i != NULL) {
        kind = *(byte*)(i + 16);

        if (kind == 57) {
            // --- Pragma interleave ---
            entity = *(qword*)(i + 24);
            // Walk past continuation markers (kind 53)
            for (i = *(qword*)i; i != NULL; ) {
                if (*(byte*)(i + 16) != 53) break;
                alt = i;
                modified_alt = true;
                i = *(qword*)(*(qword*)(i + 24) + 8);  // follow entity next
            }
            if (i == NULL && alt != NULL) {
                i = *(qword*)alt;
                alt = NULL;
                modified_alt = true;
            }
            modified_primary = true;

            if (*(byte*)(entity + 9))              // skip_flag set?
                continue;                          // already processed

            // Commit cursor state
            qword_1065748 = i;
            if (modified_alt) qword_1065740 = alt;
            byte_10657FB = 1;                      // mark pragma context

            // Set source position from pragma entity
            dword_1065818 = 1;                     // needs line directive
            qword_1065810 = *(qword*)(entity + 32);
            qword_126EDE8 = *(qword*)(entity + 32);

            sub_kind = *(byte*)(entity + 8);
            switch (sub_kind) {
                case 26:  // STDC pragma
                    emit_line_start("#pragma ");
                    emit_raw("STDC ");
                    switch (*(byte*)(entity + 56)) {
                        case 1: emit_raw("FP_CONTRACT ");    break;
                        case 2: emit_raw("FENV_ACCESS ");    break;
                        case 3: emit_raw("CX_LIMITED_RANGE "); break;
                        default: assertion("gen_stdc_pragma: bad kind");
                    }
                    switch (*(byte*)(entity + 57)) {
                        case 1: emit_raw("OFF");     break;
                        case 2: emit_raw("ON");      break;
                        case 3: emit_raw("DEFAULT"); break;
                        default: assertion("gen_stdc_pragma: bad value");
                    }
                    emit_newline();
                    break;

                case 21:  // Line directive pragma
                    emit_line_start("#line ");
                    byte_10657F9 = 1;
                    sub_5FCAF0(*(qword*)(entity + 56), 0, &xmmword_1065760);
                    byte_10657F9 = 0;
                    emit_newline();
                    break;

                default:  // Generic pragma (including sub_kind 19)
                    if (!*(qword*)(entity + 48))
                        assertion("gen_pragma: NULL pragma_text");
                    emit_line_start("#pragma ");
                    emit_raw(*(char**)(entity + 48));
                    emit_newline();
                    if (sub_kind == 19)
                        dword_10656F8 = *(int*)(entity + 56);  // track #pragma pack
                    break;
            }
            byte_10657FB = v14;                    // restore saved flag
            continue;                              // next iteration
        }

        // --- Non-pragma entity ---
        if (modified_primary) qword_1065748 = i;
        if (modified_alt)     qword_1065740 = alt;

        if (kind == 53) {
            // Continuation marker: switch to alternate cursor
            alt = i;
            modified_alt = true;
            i = *(qword*)(*(qword*)(i + 24) + 8);
            continue;
        }

        if (kind == 52)  // end_of_construct: should never appear at top level
            sub_4F2930("cp_gen_be.c", 26628,
                       "process_file_scope_entities",
                       "Top-level end-of-construct entry", 0);

        v12 = 1;                                   // mark: entity emitted
        sub_47ECC0(0);                             // gen_template(recursion_level=0)
        // Loop continues from updated qword_1065748
    }

    // Exhausted primary cursor; check for pending alternate
    if (i == NULL && alt != NULL) {
        i = *(qword*)alt;
        alt = NULL;
        // ... continue outer loop
    } else {
        break;  // done
    }
}

// Final cursor cleanup
if (modified_primary) qword_1065748 = 0;
if (modified_alt)     qword_1065740 = alt;
```

### Entity Kind Dispatch

For non-pragma entries (kind != 57), the loop calls `sub_47ECC0(0)` (`gen_template` with recursion level 0), which reads the current entity from `qword_1065748` and dispatches based on the entity's kind:

| Kind | Name | Handler |
|---|---|---|
| 2 | `variable_decl` | `sub_484A40` (`gen_variable_decl`) or inline |
| 6 | `type_decl` | `sub_4864F0` (`gen_type_decl`) |
| 7 | `parameter_decl` | `sub_484A40` |
| 8 | `field_decl` | Inline field handler |
| 11 | `routine_decl` | `sub_47BFD0` (`gen_routine_decl`, 1831 lines) |
| 28 | `namespace` | Inline namespace handler (recursive `sub_47ECC0(0)`) |
| 29 | `using_decl` | Inline using-declaration handler |
| 42 | `asm_decl` | `__asm(...)` generation |
| 51 | `indirect` | Unwrap and re-dispatch |
| 52 | `end_of_construct` | Assertion (kind 52 triggers `sub_4F2930` diagnostic) |
| 54 | `instantiation` | Template instantiation directive |
| 58 | `template` | Template definition |
| 66 | `alias_decl` | Alias declaration (`using X = Y`) |
| 67 | `concept_decl` | Concept handling |
| 83 | `deduction_guide` | Deduction guide |

### Inline Pragma Handling

Kind 57 entries are pragma interleavings that appear between declarations. The backend handles three sub-kinds inline within `sub_489000`:

**Sub-kind 26: STDC Pragma**

Emits `#pragma STDC <kind> <value>`:

```c
// Read pragma kind from offset +56
switch (stdc_kind) {
    case 1:  emit("FP_CONTRACT ");    break;
    case 2:  emit("FENV_ACCESS ");    break;
    case 3:  emit("CX_LIMITED_RANGE "); break;
    default: assertion_failure("gen_stdc_pragma: bad kind");
}
// Read pragma value from offset +57
switch (stdc_value) {
    case 1:  emit("OFF");     break;
    case 2:  emit("ON");      break;
    case 3:  emit("DEFAULT"); break;
    default: assertion_failure("gen_stdc_pragma: bad value");
}
```

The `#pragma` keyword is emitted character-by-character from a hardcoded string at address `0x838441` (`"#pragma "`), followed by `"STDC "` from address `0x83847B`.

**Sub-kind 21: Raw Pragma (Line Directive)**

Calls `sub_5FCAF0` to emit a preprocessor line directive using the pragma's data. The `byte_10657F9` flag is set to 1 during emission and reset to 0 afterward, temporarily changing the line-directive emission format.

**Sub-kind 19 (or other): Generic Pragma**

For all other pragma sub-kinds, the backend reads the raw pragma text from offset `+48` and emits it character by character after a `#pragma ` prefix:

```c
if (!entity->pragma_text)
    assertion_failure("gen_pragma: NULL pragma_text");
emit("#pragma ");
emit_raw_string(entity->pragma_text);
emit_newline();
```

For sub-kind 19 specifically, the function also records the pragma data in `dword_10656F8`, tracking `#pragma pack` state.

### Linkage Specification

The variable `byte_10656F0` tracks the current linkage specification:

| Value | Meaning |
|---|---|
| 2 | `extern "C"` linkage |
| 3 | `extern "C++"` linkage |

Set at initialization: `byte_10656F0 = (dword_126EFB4 != 2) + 2` -- this evaluates to 3 (C++) when in CUDA mode (`dword_126EFB4 == 2`), and 2 (C) otherwise. This controls how the backend wraps declarations that need explicit linkage changes.

## Phase 5: Empty File Guard

After the main loop completes, the function checks whether any entities were actually emitted:

```c
if (!v12 && dword_126EFB4 != 2) {
    sub_467E50("int __dummy_to_avoid_empty_file;");
    sub_467D60();  // newline
}
```

The variable `v12` tracks whether `sub_47ECC0` was called at least once (set to 1 when any non-pragma entity is processed). If no entities were processed AND the mode is not CUDA (`dword_126EFB4 != 2`), a dummy variable declaration is emitted to prevent the host compiler from rejecting an empty translation unit. In CUDA mode, the file always has content due to the managed runtime boilerplate.

## Phase 6: File Trailer

After all entities and the empty-file guard, the function emits a structured trailer. The call to `sub_466C10` performs scope stack unwinding -- it pops any remaining scope entries, restoring entity attributes that were temporarily modified during code generation (specifically, bits in byte `+82` and `+134` of entity nodes).

### #line Reset

Two `#line 1 "<original_file>"` directives bracket the trailer, resetting the host compiler's notion of the current source location back to the original `.cu` file:

```c
sub_46BC80("#");
if (!dword_126E1F8)      // not GNU mode: use long form
    sub_467E50("line");
sub_467E50(" 1 \"");
filename = sub_5AF450(qword_106BF88);   // get original filename
sub_467E50(filename);
sub_468150(34);           // closing quote '"'
```

### _NV_ANON_NAMESPACE Macro

The anonymous namespace support macro is emitted:

```c
#define _NV_ANON_NAMESPACE <unique_id>
```

The unique identifier is generated by `sub_6BC7E0` (`get_anonymous_namespace_name`), which returns `"_GLOBAL__N_<filename>"` -- a mangled name that ensures anonymous namespace entities from different translation units do not collide in the final linked binary.

This is followed by a guard block:

```c
#ifdef _NV_ANON_NAMESPACE
#endif
```

The `#ifdef`/`#endif` block appears to be a deliberate no-op that downstream tools (nvcc's driver) can detect to confirm the file was processed by cudafe++.

### MSVC Pack Reset

In MSVC host compiler mode (`dword_126E1D8`), a `#pragma pack()` is emitted to reset the packing alignment to the compiler default:

```c
if (dword_126E1D8) {
    sub_46BC80("#pragma pack()");
    sub_467D60();
}
```

### Source Re-inclusion

The original source file is re-included via `#include`:

```c
#include "<original_file>"
```

This is the mechanism by which the host compiler sees the original source code: the `.int.c` file first declares all the generated stubs and boilerplate, then `#include`s the original file. The EDG frontend has already parsed the original file and knows which declarations are host-visible; the re-inclusion lets the host compiler process them with the stubs already in scope.

A final `#line 1` directive follows, and then:

```c
#undef _NV_ANON_NAMESPACE
```

This cleans up the macro so it does not leak into subsequent compilation units.

## Phase 7: Host Reference Arrays

The final emission step generates CUDA host reference arrays via `sub_6BCF80` (`nv_emit_host_reference_array`). These arrays are placed in special ELF sections that the CUDA runtime linker uses to discover device symbols at launch time.

The function is called 6 times with different flag combinations:

```c
// Signature: nv_emit_host_reference_array(emit_fn, is_kernel, is_device, is_internal)

sub_6BCF80(sub_467E50, 1, 0, 1);  // kernel,   internal  -> .nvHRKI
sub_6BCF80(sub_467E50, 1, 0, 0);  // kernel,   external  -> .nvHRKE
sub_6BCF80(sub_467E50, 0, 1, 1);  // device,   internal  -> .nvHRDI
sub_6BCF80(sub_467E50, 0, 1, 0);  // device,   external  -> .nvHRDE
sub_6BCF80(sub_467E50, 0, 0, 1);  // constant, internal  -> .nvHRCI
sub_6BCF80(sub_467E50, 0, 0, 0);  // constant, external  -> .nvHRCE
```

| Section | Array Name | Symbol Type | Linkage |
|---|---|---|---|
| `.nvHRKI` | `hostRefKernelArrayInternalLinkage` | `__global__` kernel | Internal (anonymous namespace) |
| `.nvHRKE` | `hostRefKernelArrayExternalLinkage` | `__global__` kernel | External |
| `.nvHRDI` | `hostRefDeviceArrayInternalLinkage` | `__device__` variable | Internal |
| `.nvHRDE` | `hostRefDeviceArrayExternalLinkage` | `__device__` variable | External |
| `.nvHRCI` | `hostRefConstantArrayInternalLinkage` | `__constant__` variable | Internal |
| `.nvHRCE` | `hostRefConstantArrayExternalLinkage` | `__constant__` variable | External |

Each array entry encodes a device symbol's mangled name as a byte array:

```c
extern "C" {
    extern __attribute__((section(".nvHRKE")))
           __attribute__((weak))
    const unsigned char hostRefKernelArrayExternalLinkage[] = {
        0x5f, 0x5a, ... /* mangled name bytes */ 0x00
    };
}
```

The 6 global lists from which these symbols are collected reside at:

| Address | Contents |
|---|---|
| `unk_1286780` | Device-external symbols |
| `unk_12867C0` | Device-internal symbols |
| `unk_1286800` | Constant-external symbols |
| `unk_1286840` | Constant-internal symbols |
| `unk_1286880` | Kernel-external symbols |
| `unk_12868C0` | Kernel-internal symbols |

This phase is conditional: it only executes when `dword_106BFD0` (CUDA device registration) or `dword_106BFCC` (CUDA constant registration) is nonzero.

### Module ID Output

Before the host reference arrays, if `dword_106BFB8` is set, `sub_5B0180` (`write_module_id_to_file`) writes the CRC32-based module identifier to a separate file. This ID is used by the CUDA runtime to match device code fatbinaries with their host-side registration code.

## Breakpoint Placeholders (Between Phase 5 and Phase 6)

After the empty file guard and scope unwinding (`sub_466C10`) but before the file trailer, if the breakpoint placeholder list (`qword_1065840`) is non-empty, the backend emits debug breakpoint functions:

```c
static __attribute__((used)) void __nv_breakpoint_placeholder<N>_<name>(void) {
    exit(0);
}
```

The placeholder list is a linked list where each node contains:

| Offset | Field |
|---|---|
| +0 | `next` pointer |
| +8 | Source position (start) |
| +16 | Source position (end) |
| +24 | Name string (or NULL) |

Each placeholder is numbered sequentially (starting from 0). The `__attribute__((used))` prevents the linker from stripping these symbols, and the `exit(0)` body ensures the function has a concrete implementation that a debugger can set a breakpoint on. The underscore separator before the name distinguishes the placeholder from the numbered prefix.

## Complete .int.c File Structure

Putting all phases together, the output `.int.c` file has this structure:

```cpp
#line 1 "<input>.cu"                          // initial line directive
#pragma GCC diagnostic ignored "-Wunused-local-typedefs"
#pragma GCC diagnostic ignored "-Wattributes"
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wunused-function"
// ... additional suppressions for Clang

// --- managed runtime boilerplate ---
static char __nv_inited_managed_rt = 0;
static void **__nv_fatbinhandle_for_managed_rt;
static void __nv_save_fatbinhandle_for_managed_rt(void **in) { ... }
static char __nv_init_managed_rt_with_module(void **);
static inline void __nv_init_managed_rt(void) { ... }

#pragma GCC diagnostic pop
#pragma GCC diagnostic ignored "-Wunused-variable"

// --- lambda detection macros (when not in extended lambda mode) ---
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
#if defined(...) && defined(...) && defined(...)
#endif

// --- main entity output ---
// [user declarations, type definitions, function stubs, etc.]
// [device-only code wrapped in #if 0 / #endif]
// [__global__ kernels -> __wrapper__device_stub_ forwarding]
// [pragmas interleaved at original positions]

// --- empty file guard (non-CUDA mode only) ---
int __dummy_to_avoid_empty_file;

// --- breakpoint placeholders (if any) ---
static __attribute__((used)) void __nv_breakpoint_placeholder0_name(void) { exit(0); }

// --- file trailer ---
#line 1 "<input>.cu"
#define _NV_ANON_NAMESPACE _GLOBAL__N_<input>
#ifdef _NV_ANON_NAMESPACE
#endif
#pragma pack()                                // MSVC only
#line 1 "<input>.cu"
#include "<input>.cu"                         // re-include original source
#line 1 "<input>.cu"
#undef _NV_ANON_NAMESPACE

// --- host reference arrays (if CUDA registration active) ---
extern "C" { extern __attribute__((section(".nvHRKI"))) ... }
extern "C" { extern __attribute__((section(".nvHRKE"))) ... }
extern "C" { extern __attribute__((section(".nvHRDI"))) ... }
extern "C" { extern __attribute__((section(".nvHRDE"))) ... }
extern "C" { extern __attribute__((section(".nvHRCI"))) ... }
extern "C" { extern __attribute__((section(".nvHRCE"))) ... }
```

## Key Global Variables

| Variable | Address | Type | Role |
|---|---|---|---|
| `stream` | output state | `FILE*` | Output file handle |
| `dword_1065834` | `0x1065834` | int | Indent/nesting level |
| `dword_1065820` | `0x1065820` | int | Output line counter |
| `dword_106581C` | `0x106581C` | int | Output column counter |
| `dword_1065818` | `0x1065818` | int | Needs-line-directive flag |
| `qword_1065810` | `0x1065810` | qword | Current source position |
| `qword_1065828` | `0x1065828` | qword | Current source file index |
| `qword_1065748` | `0x1065748` | qword | Source sequence cursor (primary) |
| `qword_1065740` | `0x1065740` | qword | Source sequence cursor (alternate) |
| `dword_1065850` | `0x1065850` | int | Device stub mode toggle |
| `byte_10656F0` | `0x10656F0` | byte | Current linkage spec (2=C, 3=C++) |
| `dword_10656F8` | `0x10656F8` | int | Current `#pragma pack` state |
| `qword_1065708` | `0x1065708` | qword | Scope stack head |
| `qword_1065700` | `0x1065700` | qword | Scope pool head |
| `qword_1065720` | `0x1065720` | qword | Scope free list |
| `dword_106BF38` | `0x106BF38` | int | Extended lambda mode |
| `dword_106BFB8` | `0x106BFB8` | int | Emit module ID flag |
| `dword_106BFD0` | `0x106BFD0` | int | CUDA device registration flag |
| `dword_106BFCC` | `0x106BFCC` | int | CUDA constant registration flag |
| `dword_106BF6C` | `0x106BF6C` | int | Alternative host compiler mode |
| `dword_126EFB4` | `0x126EFB4` | int | Compiler mode (2 = CUDA) |
| `dword_126E1D8` | `0x126E1D8` | int | MSVC host compiler flag |
| `dword_126E1F8` | `0x126E1F8` | int | GNU/GCC host compiler flag |
| `dword_126E1E8` | `0x126E1E8` | int | Clang host compiler flag |
| `qword_126E1F0` | `0x126E1F0` | qword | GCC/Clang version number |
| `dword_126EF68` | `0x126EF68` | int | C++ standard version (`__cplusplus`) |

## Cross-References

- [Pipeline Overview](./overview.md) -- where stage 7 fits in the full compilation flow
- [Frontend Wrapup](./fe-wrapup.md) -- stage 6, produces the finalized IL that the backend consumes
- [.int.c File Format](../output/int-c-format.md) -- detailed structure of the backend output file
- [Managed Memory Boilerplate](../output/cuda-runtime.md) -- the `__nv_managed_rt` initialization pattern
- [Host Reference Arrays](../output/host-reference-arrays.md) -- `.nvHRKI`/`.nvHRDE` section format
- [Module ID](../output/module-id.md) -- CRC32 module identification
- [Device/Host Separation](../cuda/device-host-separation.md) -- how the backend filters device vs host code
- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- `__wrapper__device_stub_` pattern in `gen_routine_decl`
- [Extended Lambda Overview](../lambda/overview.md) -- lambda wrapper generation
- [Lambda Preamble Injection](../lambda/preamble-injection.md) -- `sub_6BCC20` emission in `gen_template`
