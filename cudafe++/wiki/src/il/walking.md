# IL Tree Walking

The IL tree walking framework is the backbone of every operation that must visit the complete IL graph: debug display, device code marking, IL serialization, and IL copying for template instantiation. The framework lives in `il_walk.c` (with entry-kind dispatch logic auto-generated from `walk_entry.h`). It provides a generic, callback-driven traversal engine consisting of two core functions: `walk_file_scope_il` (sub_60E4F0), which orchestrates the top-level iteration over all global entry-kind lists, and `walk_entry_and_subtree` (sub_604170), which recursively descends into a single entry's children according to the IL schema. Five global function-pointer slots allow each client to customize the walk's behavior without modifying the walker itself.

The framework follows a strict separation of traversal and action. The walker knows how to navigate the IL graph; the callbacks decide what to do at each node. This design enables the same walker to serve four fundamentally different purposes: pretty-printing, transitive-closure marking, pointer remapping during copy, and entry filtering during serialization.

## Key Facts

| Property | Value |
|---|---|
| Source file | `il_walk.c` (EDG 6.6) |
| Header (auto-generated dispatch) | `walk_entry.h` |
| Assert path | `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/il_walk.c` |
| Top-level file-scope walker | `sub_60E4F0` (`walk_file_scope_il`), 2043 lines |
| Recursive entry walker | `sub_604170` (`walk_entry_and_subtree`), 7763 lines / 42KB |
| Routine-scope walker | `sub_610200` (`walk_routine_scope_il`), 108 lines |
| Hash table reset | `sub_603B30` (`clear_walk_hash_table`), 23 lines |
| Anonymous union lookup | `sub_603FE0` (`find_parent_var_of_anon_union_type`), 127 lines |
| Entry kinds covered | 85 (switch cases 0--84) |
| Recursive self-calls | ~330 (in `walk_entry_and_subtree`) |
| Callback slots | 5 global function pointers |

## Callback Slot Architecture

The five callback slots are stored as global function pointers. Before any walk, the caller saves all five values, installs its own set, and restores the originals on exit. This save/restore discipline makes walks re-entrant -- a callback can itself trigger a nested walk with different callbacks.

| Address | Slot Name | Signature | Purpose |
|---|---|---|---|
| `qword_126FB88` | `entry_callback` | `entry_ptr(entry_ptr, entry_kind)` | Called for each entry visited; may return a replacement pointer |
| `qword_126FB80` | `string_callback` | `void(char_ptr, kind_26, strlen+1)` | Called for each string field (file names, `compiler_version`, `time_of_compilation`) |
| `qword_126FB78` | `pre_walk_check` | `int(entry_ptr, entry_kind)` | Called before descending into an entry; returns nonzero to skip the subtree |
| `qword_126FB70` | `entry_replace` | `entry_ptr(entry_ptr, entry_kind)` | Called to remap an entry pointer (used during IL copy to translate old pointers to new ones) |
| `qword_126FB68` | `entry_filter` | `entry_ptr(entry_ptr, entry_kind)` | Called on linked-list heads to filter entries; returning NULL removes the entry from the list |

The `pre_walk_check` slot is the only one whose return value controls flow: nonzero means "already handled, skip this subtree." The keep-in-il pass uses this to avoid revisiting already-marked entries (preventing infinite recursion on cyclic references). The `entry_replace` slot is used during IL copy operations to translate pointers from the source IL region to the destination region.

### Walk State Globals

In addition to the five callback slots, four state variables track the walker's context:

| Address | Name | Description |
|---|---|---|
| `dword_126FB5C` | `is_file_scope_walk` | 1 during `walk_file_scope_il`, 0 during `walk_routine_scope_il` |
| `dword_126FB58` | `is_secondary_il` | 1 if the current scope belongs to the secondary IL region |
| `dword_106B644` | `current_il_region` | Toggles per IL region; used to stamp bit 2 of entry flags |
| `dword_126FB60` | `walk_mode_flags` | Bitmask controlling walk behavior (e.g., strip template info) |

All four are saved and restored alongside the callback slots, making the entire walk context atomically swappable.

## walk_file_scope_il (sub_60E4F0)

This is the central traversal entry point. Every operation that needs to visit the entire file-scope IL calls this function with its desired callbacks. It takes six arguments:

```c
void walk_file_scope_il(
    entry_callback_t   a1,   // entry visitor (qword_126FB88)
    string_callback_t  a2,   // string visitor (qword_126FB80)
    entry_replace_t    a3,   // pointer remapper (qword_126FB70)
    entry_filter_t     a4,   // list filter (qword_126FB68)
    pre_walk_check_t   a5,   // pre-visit gate (qword_126FB78)
    int                a6    // walk_mode_flags (dword_126FB60)
);
```

### Initialization

The function begins by saving all five callback slots and all four walk state variables, then installs the caller's values:

```c
// Save current state
saved_entry_cb      = qword_126FB88;
saved_string_cb     = qword_126FB80;
saved_pre_walk      = qword_126FB78;
saved_entry_replace = qword_126FB70;
saved_entry_filter  = qword_126FB68;
saved_is_file_scope = dword_126FB5C;
saved_is_secondary  = dword_126FB58;
saved_il_region     = dword_106B644;
saved_mode_flags    = dword_126FB60;

// Install new callbacks
qword_126FB88 = a1;
qword_126FB80 = a2;
qword_126FB70 = a3;
qword_126FB68 = a4;
qword_126FB78 = a5;
dword_126FB60 = a6;
dword_126FB5C = 1;  // mark as file-scope walk
```

An assertion fires if `pre_walk_check` is NULL and the primary scope is in secondary IL (bit 1 of flags byte set):

```c
if (!a5 && is_secondary)
    assert_fail("il_walk.c", 270, "walk_file_scope_il");
```

This prevents unguarded walks into secondary IL regions, which would produce incorrect results because secondary entries need canonical-entry delegation.

### Walk Order

The function visits IL entries in a fixed, deterministic order. This order is significant for serialization (the IL binary format expects entries in this exact sequence) and for display (the `--dump_il` output follows this structure).

**Phase 1: Primary scope (kind 23)**

```c
primary_scope = xmmword_126EB60[1];  // second qword of IL header

// If entry_replace callback exists, remap the scope pointer first
if (entry_replace)
    primary_scope = entry_replace(primary_scope, 23);

// Determine IL region flags from the scope's prefix byte
is_secondary_il = (*(primary_scope - 8) & 0x02) != 0;
current_il_region = ((*(primary_scope - 8) >> 2) ^ 1) & 1;

walk_entry_and_subtree(primary_scope, 23);
```

The scope entry (kind 23) is walked first because it is the root of the scope tree. Walking the scope recursively visits all nested scopes and their member lists.

**Phase 2: Source file entries (kind 1)**

```c
for (entry = xmmword_126EB60[0]; entry; entry = entry->child_file) {
    if (entry_filter && !entry_filter(entry, 1))
        continue;  // filtered out
    walk_entry_and_subtree(entry, 1);
}
```

Source file entries form a linked list via offset +56 (`child_file`). Each entry holds the file name, full path, and `name_as_written` strings.

**Phase 3: String entries (kind 26 = `other_text`)**

Two specific string entries from the IL header are walked:

```c
// compiler_version (qword_126EB78)
if (entry_replace)
    compiler_version = entry_replace(compiler_version, 26);
if (compiler_version) {
    if (trace_verbosity > 4)
        fprintf(s, "Walking IL tree, string entry kind = %s\n", "other text");
    if (string_callback)
        string_callback(compiler_version, 26, strlen(compiler_version) + 1);
}

// time_of_compilation (qword_126EB80) -- same pattern
```

Strings are walked with kind 26 (`other_text`) and the string callback receives the raw character pointer, the kind, and the length including the null terminator.

**Phase 4: Orphaned entities list (kind 55)**

```c
for (entry = qword_126EBA0; entry; entry = entry->next) {
    if (entry_filter && !entry_filter(entry, 55))
        continue;
    walk_entry_and_subtree(entry, 55);
}
```

Kind 55 entries are orphaned entities -- declarations that lost their parent scope (e.g., after template instantiation cleanup). They are stored in a separate linked list headed at `qword_126EBA0`.

**Phase 5: Global entry-kind lists (kinds 1--72)**

The bulk of the walk iterates 45+ global linked lists, one per entry kind. Each list head is stored at a fixed address in the `0x126E610`--`0x126EA80` range, spaced 16 bytes apart:

```c
// Kind 1 (source_file_entry) at qword_126E610
// Kind 2 (constant) at qword_126E620
// Kind 3 (parameter) at qword_126E630
// Kind 4 (routine_type_supplement) at qword_126E640
// Kind 5 (routine_type_extra) at qword_126E650
// Kind 6 (type) at qword_126E660
// Kind 7 (variable) at qword_126E670
// Kind 8 (field) at qword_126E680
// Kind 9 (exception_spec) at qword_126E690
// Kind 10 (exception_spec_type) at qword_126E6A0
// Kind 11 (routine) at qword_126E6B0
// ...continues through...
// Kind 63 (name_qualifier) at qword_126EA70
// Kind 72 (attribute) at qword_126EA80
```

For each non-empty list, the walk applies the `entry_replace` callback (if present) to each entry before descending, and follows the `next` pointer (at entry-specific offsets, typically offset -16 in the raw allocation, which is the `next_in_list` link in the entry prefix).

**Phase 6: Special trailing lists**

Three additional lists are walked after the main kind-indexed sequence:

```c
// seq_number_lookup entries (kind 64) at qword_126EBE8
for (entry = qword_126EBE8; entry; entry = entry->next) {
    if (entry_filter) ...
    walk_entry_and_subtree(entry, 64);
}

// External declarations (kind 6) at qword_126EBE0
// -- uses entry_filter with kind 6 and follows offset +104 links

// Kind 83 entries at qword_126EC00
for (entry = qword_126EC00; entry; entry = entry->next) {
    if (entry_filter) ...
    walk_entry_and_subtree(entry, 83);
}
```

### Cleanup

After all phases complete, the function restores all saved state:

```c
dword_126FB5C = saved_is_file_scope;
dword_126FB58 = saved_is_secondary;
dword_106B644 = saved_il_region;
dword_126FB60 = saved_mode_flags;
qword_126FB88 = saved_entry_cb;
qword_126FB80 = saved_string_cb;
qword_126FB78 = saved_pre_walk;
qword_126FB70 = saved_entry_replace;
qword_126FB68 = saved_entry_filter;
```

If tracing is active (`dword_126EFC8`), the function emits trace-leave via `sub_48AFD0`.

## walk_entry_and_subtree (sub_604170)

This is the recursive engine -- the second-largest function in the entire cudafe++ binary at 7763 lines / 42KB of decompiled code. It takes an entry pointer and its kind, then recursively walks every child entry according to the IL schema.

### Entry Protocol

Before descending into any entry, the function executes a two-path check:

```c
while (true) {
    if (pre_walk_check != NULL) {
        // Callback path: delegate decision to the callback
        if (pre_walk_check(entry, entry_kind))
            return;  // callback says skip
    } else {
        // Default path: check flags
        flags = *(entry - 8);

        // If not file-scope walk and entry has file-scope bit: skip
        if (!is_file_scope_walk && (flags & 0x01))
            return;

        // If entry's il_region bit matches current_il_region: skip
        if (((flags & 0x04) != 0) == current_il_region)
            return;

        // Stamp the entry's il_region bit to match current region
        *(entry - 8) = (4 * (current_il_region & 1)) | (flags & 0xFB);
    }

    // Trace output at verbosity > 4
    if (trace_verbosity > 4)
        fprintf(s, "Walking IL tree, entry kind = %s\n",
                il_entry_kind_names[entry_kind]);

    // Dispatch on entry kind
    switch ((char)entry_kind) { ... }
}
```

The `while(true)` loop structure exists because certain cases (particularly linked-list tails) use `continue` to re-enter the check with a new entry, avoiding redundant function-call overhead for tail chains.

The default-path flags check serves two purposes:
1. **Scope isolation**: File-scope entries encountered during a routine-scope walk are skipped (they belong to the outer walk).
2. **Region tracking**: The `current_il_region` toggle prevents visiting the same entry twice within a single walk -- once stamped, an entry's bit 2 matches `current_il_region`, and the equality check causes the walker to skip it.

### Entry Kind Dispatch

The giant switch covers all 85 entry kinds. Each case knows the exact layout of that entry type and recursively calls `walk_entry_and_subtree` on every child pointer. The three callbacks are invoked at appropriate points:

- **`entry_replace`**: Called on each child pointer before recursion, potentially replacing it with a remapped pointer.
- **`string_callback`**: Called on string fields (file names, identifier text), receiving the string pointer, kind 26, and byte length including null terminator.
- **`entry_filter`**: Called on linked-list head pointers, returning NULL to remove the entry from the list.

### Coverage by Entry Kind

The following table shows the major entry kinds and what the walker visits for each:

| Kind | Name | Children Walked |
|---|---|---|
| 1 | `source_file_entry` | `file_name` (string), `full_name` (string), `name_as_written` (string), child file list (kind 1, linked via offset +56) |
| 2 | `constant` | Type reference (kind 6), sub-switch on constant_kind (0--13): string data, address target, complex parts, template param constant |
| 3 | `parameter` | Type (kind 6), declared_type (kind 6), default_arg_expr (kind 13), attributes (kind 72) |
| 6 | `type` | Base type (kind 6), member field list (kind 8), template info (kind 58), scope (kind 23), base class list (kind 24), class_info supplement (kind 39) |
| 7 | `variable` | Type (kind 6), initializer expression (kind 13), attributes (kind 72), declared_type (kind 6) |
| 8 | `field` | Next field (kind 8), type (kind 6), bit_size_constant (kind 2) |
| 9 | `exception_spec` | Type list (kind 10), noexcept expression (kind 13) |
| 11 | `routine` | Return type (kind 6), parameter list (kind 3), body scope (kind 23), template info (kind 58), exception spec (kind 9), attributes (kind 72) |
| 12 | `label` | Break label (kind 12), continue label (kind 12) |
| 13 | `expression` | Sub-expressions (kind 13), operand entries, type references (kind 6); sub-switch on expression operator covers ~120 operator kinds |
| 16 | `switch_case` | Statement (kind 21), case_value constant (kind 2) |
| 17 | `switch_info` | Case list (kind 16), default case (kind 16), sorted case array |
| 18 | `catch_entry` | Parameter (kind 7), statement (kind 21), dynamic_init expression (kind 13) |
| 21 | `statement` | Sub-statements (kind 21), expressions (kind 13), labels (kind 12); sub-switch on statement kind |
| 22 | `object_lifetime` | Variable (kind 7), lifetime scope boundary |
| 23 | `scope` | Variables list (kind 7), routines list (kind 11), types list (kind 6), nested scopes (kind 23), namespaces (kind 28), using-declarations (kind 29), hidden names (kind 56), labels (kind 12) |
| 24 | `base_class` | Next (kind 24), type (kind 6), derived_class (kind 6), offset expression |
| 27 | `template_parameter` | Default value, constraint expression (kind 60), template param supplement |
| 28 | `namespace` | Associated scope (kind 23), flags |
| 29 | `using_declaration` | Target entity, position, access specifier |
| 30 | `dynamic_init` | Expression (kind 13), associated variable (kind 7) |
| 39 | `class_info` | Constructor initializer list (kind 41), friend list, base class list (kind 24) |
| 41 | `constructor_init` | Next (kind 41), member/base expression, initializer expression |
| 55 | `orphaned_entities` | Entity list, scope reference |
| 58 | `template` | Template parameter list (kind 61), body, specializations list |
| 72 | `attribute` | Attribute arguments (kind 73), next attribute (kind 72) |

### Error Strings

The function contains diagnostic strings from `walk_entry.h` that fire on unexpected sub-kind values:

| String | Triggers When |
|---|---|
| `"walk_entry_and_subtree: bad address const kind"` | Unknown `address_constant_kind` in constant entry (kind 2) |
| `"walk_entry_and_subtree: bad template param constant kind"` | Unknown `template_param_constant_kind` in constant entry (kind 2) |

These strings confirm the function name and that the dispatch code is generated from `walk_entry.h` rather than hand-written.

## walk_routine_scope_il (sub_610200)

The routine-scope counterpart of `walk_file_scope_il`. It takes a routine index and walks that routine's scope chain:

```c
void walk_routine_scope_il(int routine_index, ...) {
    // Same 5-callback + 4-state save/restore pattern
    // Trace: "walk_routine_scope_il"
    // Assert: il_walk.c, line 376

    dword_126FB5C = 0;  // NOT file-scope walk

    scope = qword_126EB90[routine_index];  // routine_scope_array
    while (scope) {
        walk_entry_and_subtree(scope, 23);
        if (entry_replace)
            scope = entry_replace(scope, 23);
        scope = scope->next;
    }
}
```

The key difference from `walk_file_scope_il` is that `is_file_scope_walk` is set to 0, which changes the entry protocol in `walk_entry_and_subtree`: entries with the file-scope bit set in their flags byte are skipped, because they belong to the file-scope IL and should not be processed during a routine-scope walk.

## Callers and Use Cases

The walk framework serves four distinct purposes. Each caller installs a different callback configuration.

### IL Display

The `--dump_il` debug output uses the walk framework with `display_il_entry` (sub_5F4930) as the entry callback:

```c
// sub_5F76B0 (display_il_header)
walk_file_scope_il(
    display_il_entry,    // a1: entry callback = sub_5F4930
    NULL,                // a2: no string callback
    NULL,                // a3: no replace
    NULL,                // a4: no filter
    NULL,                // a5: no pre-walk check
    0                    // a6: no special flags
);
```

With all callbacks NULL except `entry_callback`, the walker visits every entry in walk order and calls `display_il_entry` on each, which dispatches on entry kind to print formatted field dumps. The `pre_walk_check` is NULL, so the default flags-based skip logic applies -- the `current_il_region` toggle prevents double-visiting.

### Keep-in-IL Marking

The device code selection pass (`mark_to_keep_in_il`, sub_610420) installs the prune callback as `pre_walk_check` and NULL for everything else:

```c
// sub_610420 (mark_to_keep_in_il)
qword_126FB88 = NULL;                    // no entry callback
qword_126FB80 = NULL;                    // no string callback
qword_126FB78 = prune_keep_in_il_walk;   // sub_617310
qword_126FB70 = NULL;                    // no replace
qword_126FB68 = NULL;                    // no filter
```

The `prune_keep_in_il_walk` callback (sub_617310) sets bit 7 (0x80) of each entry's flags byte and returns 1 for already-marked entries (preventing infinite recursion). The actual subtree walk is handled by a specialized copy of `walk_entry_and_subtree` (sub_6115E0, `walk_tree_and_set_keep_in_il`, 4649 lines) that directly sets the keep bit on every reachable child rather than using callbacks. See [Keep-in-IL](./keep-in-il.md) for the full mechanism.

### IL Serialization

IL binary output (when `IL_SHOULD_BE_WRITTEN_TO_FILE` would be enabled, or for device IL output) uses all five callback slots:

- **`entry_callback`**: Records each entry's position in the output stream
- **`string_callback`**: Serializes string data with length prefix
- **`entry_replace`**: Translates IL pointers to output-stream offsets
- **`entry_filter`**: Skips entries that should not appear in the output (e.g., entries without `keep_in_il` for device IL)
- **`pre_walk_check`**: Prevents re-serializing entries already written

### IL Copy (Template Instantiation)

When EDG instantiates a template, it copies the template's IL subtree into a new region. The copy operation uses `entry_replace` to remap all pointers from the source region to the destination:

- **`entry_replace`**: For each child pointer, allocates a new entry in the destination region, copies the source entry's contents, and returns the new pointer
- **`string_callback`**: Copies string data into the destination region
- **`pre_walk_check`**: Tracks which entries have already been copied (using the visited-set hash table at `qword_126FB50`)

## Hash Table for Visited Set

The walk framework includes a visited-set hash table for cycles and deduplication:

| Address | Name | Description |
|---|---|---|
| `qword_126FB50` | `hash_table_array` | Pointer to hash table bucket array |
| `dword_126FB48` | `hash_table_count` | Number of entries in hash table |
| `qword_126FB40` | `visited_set` | Pointer to visited-set data |
| `dword_126FB30` | `visited_count` | Number of visited entries |

The hash table is reset by `sub_603B30` (`clear_walk_hash_table`) before each walk operation. It uses open addressing and is primarily employed during IL copy operations to map source entry pointers to their destination counterparts.

## Helper Functions

Several helper functions support the walk framework:

| Address | Identity | Lines | Purpose |
|---|---|---|---|
| `sub_603B30` | `clear_walk_hash_table` | 23 | Zeros the visited-set hash table (`qword_126FB50`, `dword_126FB48`) |
| `sub_603FE0` | `find_parent_var_of_anon_union_type` | 127 | Searches scope member lists for the variable that owns an anonymous union type |
| `sub_603BB0` | `find_var_in_nested_scopes` | 333 | Recursively searches nested scopes for a variable (deeply unrolled, 8+ levels) |
| `sub_603B00` | (trivial getter) | 9 | Walk-state accessor |
| `sub_610200` | `walk_routine_scope_il` | 108 | Routine-scope walker (counterpart to `walk_file_scope_il`) |

### Keep-in-IL Specialized Walkers

The keep-in-il pass uses parallel implementations of the walk framework that bypass the callback mechanism for performance:

| Address | Identity | Lines | Purpose |
|---|---|---|---|
| `sub_6115E0` | `walk_tree_and_set_keep_in_il` | 4649 | File-scope variant -- sets bit 7 directly on every reachable entry |
| `sub_618660` | `walk_entry_and_set_keep_in_il_routine_scope` | 3728 | Routine-scope variant |
| `sub_61CE20`--`sub_620190` | (keep-in-il helpers) | various | Per-kind helpers for template args, exception specs, array bounds, expressions, statements |

These specialized walkers are structurally identical to `walk_entry_and_subtree` but replace callback invocations with direct `*(entry - 8) |= 0x80` operations. They exist as separate functions rather than callback-based walks because the keep-in-il marking is performance-critical -- it runs on every CUDA compilation, and eliminating the function-pointer indirection across ~330 recursive calls provides measurable speedup.

## Global Entry-Kind List Map

The `walk_file_scope_il` function iterates global entry-kind linked lists stored at fixed addresses. The complete mapping:

| Address | Kind | Name | Next-Pointer Offset |
|---|---|---|---|
| `qword_126EBA0` | 55 | `orphaned_entities` | +0 |
| `qword_126E610` | 1 | `source_file_entry` | -16 (prefix next) |
| `qword_126E620` | 2 | `constant` | -16 |
| `qword_126E630` | 3 | `parameter` | -16 |
| `qword_126E640` | 4 | `routine_type_supplement` | -16 |
| `qword_126E650` | 5 | `routine_type_extra` | -16 |
| `qword_126E660` | 6 | `type` | -16 |
| `qword_126E670` | 7 | `variable` | -16 |
| `qword_126E680` | 8 | `field` | -16 |
| `qword_126E690` | 9 | `exception_specification` | -16 |
| `qword_126E6A0` | 10 | `exception_spec_type` | -16 |
| `qword_126E6B0` | 11 | `routine` | -16 |
| ... | 12--62 | (sequential, 16 bytes apart) | -16 |
| `qword_126EA70` | 63 | `name_qualifier` | -16 |
| `qword_126EA80` | 72 | `attribute` | -16 |
| `qword_126EBE8` | 64 | `seq_number_lookup` | +0 (special) |
| `qword_126EBE0` | 6 | `external_declarations` (type list) | +104 |
| `qword_126EC00` | 83 | (extended kind) | +0 |

The lists for kinds 1--72 are contiguous in memory starting at `0x126E610`, with 16-byte spacing. Kinds 64, 6 (external), and 83 are walked separately after the main loop because they use different linking structures.

## Walk Order Diagram

```
walk_file_scope_il(callbacks...)
  |
  +-- [save 5 callbacks + 4 state vars]
  +-- [install caller's callbacks]
  |
  +-- Phase 1: walk_entry_and_subtree(primary_scope, 23)
  |     |
  |     +-- Recursively visits all nested scopes,
  |         their member lists (vars, routines, types),
  |         and all subtrees
  |
  +-- Phase 2: source_file list (kind 1)
  |     +-- for each file: walk(file, 1)
  |         +-- walks file_name, full_name, child files
  |
  +-- Phase 3: string entries
  |     +-- string_callback(compiler_version, 26, len)
  |     +-- string_callback(time_of_compilation, 26, len)
  |
  +-- Phase 4: orphaned_entities list (kind 55)
  |     +-- for each orphan: walk(orphan, 55)
  |
  +-- Phase 5: global lists (kinds 1, 2, 3, ..., 72)
  |     +-- for each kind:
  |           for each entry in list:
  |             entry_replace(entry, kind)
  |             walk(entry, kind)
  |
  +-- Phase 6: trailing lists (kinds 64, 6-ext, 83)
  |
  +-- [restore saved state]
```

## Diagnostic Strings

| String | Source | Condition |
|---|---|---|
| `"walk_file_scope_il"` | `sub_60E4F0` | Trace enter (`dword_126EFC8` nonzero) |
| `"walk_routine_scope_il"` | `sub_610200` | Trace enter |
| `"Walking IL tree, entry kind = %s\n"` | `sub_604170` | `dword_126EFCC > 4` |
| `"Walking IL tree, string entry kind = %s\n"` | `sub_604170` / `sub_60E4F0` | `dword_126EFCC > 4` |
| `"walk_entry_and_subtree: bad address const kind"` | `sub_604170` | Unknown constant sub-kind |
| `"walk_entry_and_subtree: bad template param constant kind"` | `sub_604170` | Unknown template param constant sub-kind |
| `"find_parent_var_of_anon_union_type"` | `sub_603FE0` | Assert at lines 511, 523 |
| `"find_parent_var_of_anon_union_type: var not found"` | `sub_603FE0` | Variable lookup failed |

## Function Map

| Address | Identity | Confidence | Lines | EDG Source |
|---|---|---|---|---|
| `sub_60E4F0` | `walk_file_scope_il` | 99% | 2043 | `il_walk.c:270` |
| `sub_604170` | `walk_entry_and_subtree` | 99% | 7763 | `il_walk.c` / `walk_entry.h` |
| `sub_610200` | `walk_routine_scope_il` | 98% | 108 | `il_walk.c:376` |
| `sub_603B30` | `clear_walk_hash_table` | 85% | 23 | `il_walk.c` |
| `sub_603FE0` | `find_parent_var_of_anon_union_type` | 99% | 127 | `il_walk.c:511` |
| `sub_603BB0` | `find_var_in_nested_scopes` | 85% | 333 | `il_walk.c` |
| `sub_603B00` | (trivial walk-state accessor) | 80% | 9 | `il_walk.c` |
| `sub_6115E0` | `walk_tree_and_set_keep_in_il` | 98% | 4649 | `il_walk.c` |
| `sub_618660` | `walk_entry_and_set_keep_in_il_routine_scope` | 88% | 3728 | `il_walk.c` |
| `sub_61CE20` | (keep-in-il helper: template args) | 80% | 100 | `il_walk.c` |
| `sub_61D0C0` | (keep-in-il helper: exception spec) | 80% | 108 | `il_walk.c` |
| `sub_61D330` | (keep-in-il helper: array bound) | 80% | 97 | `il_walk.c` |
| `sub_61D570` | (keep-in-il helper: overriding virtual) | 80% | 120 | `il_walk.c` |
| `sub_61D7F0` | (keep-in-il helper: base class) | 80% | 69 | `il_walk.c` |
| `sub_61D9B0` | (keep-in-il helper: attributes) | 80% | 202 | `il_walk.c` |
| `sub_61DEC0` | (keep-in-il helper: using-decl) | 80% | 101 | `il_walk.c` |
| `sub_61E160` | (keep-in-il helper: object lifetime) | 80% | 76 | `il_walk.c` |
| `sub_61E370` | (keep-in-il helper: expressions) | 80% | 369 | `il_walk.c` |
| `sub_61ECF0` | (keep-in-il helper: statements) | 80% | 466 | `il_walk.c` |
| `sub_61F420` | (keep-in-il helper: additional exprs) | 80% | 631 | `il_walk.c` |
| `sub_61FEA0` | (keep-in-il helper: decl sequence) | 80% | 173 | `il_walk.c` |

## Cross-References

- [IL Overview](./overview.md) -- entry kind table, IL header structure
- [IL Allocation](./allocation.md) -- entry prefix layout, flags byte definition
- [Keep-in-IL](./keep-in-il.md) -- device code marking pass using this framework
- [IL Display](./display.md) -- `display_il_entry` callback
- [Pipeline Overview](../pipeline/overview.md) -- when walks are triggered
- [Device/Host Separation](../cuda/device-host-separation.md) -- higher-level context
