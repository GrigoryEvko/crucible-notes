# Keep-in-IL (Device Code Selection)

cudafe++ compiles a single `.cu` translation unit that contains both host and device code. After the EDG frontend builds the complete IL tree, cudafe++ must split the two worlds: host-side declarations feed into the `.int.c` output, while device-side declarations feed into the binary IL emitted for cicc. The **keep-in-il** mechanism performs this split. It is a transitive-closure walk that starts from known device entities (functions with `__device__`/`__global__` attributes, `__shared__`/`__constant__`/`__managed__` variables) and recursively marks every IL entry they reference. Entries that survive the mark phase are written to the device IL; entries without the mark are stripped by the elimination pass.

The entire mechanism lives in `il_walk.c` (the mark/walk side) and `il.c` (the elimination side). It runs as pass 3 of `fe_wrapup`, after IL lowering (pass 2) and before C++ class finalization (pass 4).

## Key Facts

| Property | Value |
|---|---|
| Source file | `il_walk.c` (mark), `il.c` (eliminate) |
| Mark entry point | `sub_610420` (`mark_to_keep_in_il`), 892 lines |
| Recursive worker | `sub_6115E0` (`walk_tree_and_set_keep_in_il`), 4649 lines / 23KB |
| Prune callback | `sub_617310` (`prune_keep_in_il_walk`), 127 lines |
| Elimination entry point | `sub_5CCBF0` (`eliminate_unneeded_il_entries`), 345 lines |
| Template cleanup | `sub_5CCA40` (`clear_instantiation_required_on_unneeded_entities`), 86 lines |
| Body removal | `sub_5CC410` (`eliminate_bodies_of_unneeded_functions`), ~200 lines |
| Trigger | `fe_wrapup` pass 3, argument 23 (scope entry kind) |
| Guard flag | `dword_106B640` (set=1 before walk, cleared=0 after) |
| Key bit | Bit 7 (0x80) of byte at `entry_ptr - 8` |

## The Keep-in-IL Bit

Every IL entry is preceded by an 8-byte prefix. The byte at offset -8 from the entry pointer contains per-entry flags:

```text
Byte at (entry_ptr - 8):
  bit 0  (0x01)  is_file_scope          Entry belongs to file-scope IL region
  bit 1  (0x02)  is_in_secondary_il     Entry is in the secondary IL (second TU)
  bit 2  (0x04)  current_il_region      Toggles per IL region (0 or 1)
  bits 3-6       (reserved)
  bit 7  (0x80)  keep_in_il             DEVICE CODE MARKER
```

The sign bit of this byte doubles as the keep-in-il flag. This allows a fast check: `*(signed char*)(entry - 8) < 0` means "keep this entry." The elimination pass exploits this: it tests `*(char*)(entry - 8) >= 0` to identify entries to remove.

Two additional "keep definition" flags exist on specific entity types:

| Entity kind | Field | Bit | Meaning |
|---|---|---|---|
| Type (kind 6, class/struct) | `entry + 162` | bit 7 (0x80) | `keep_definition_in_il` — retain full class body |
| Routine (kind 11) | `entry + 187` | bit 2 (0x04) | `keep_definition_in_il` — retain function body |

The `keep_definition_in_il` flag is stronger than the base `keep_in_il` flag. A type marked with only `keep_in_il` may be emitted as a forward declaration; one marked with `keep_definition_in_il` retains its full member list, base classes, and nested types.

## Pipeline Context

```text
fe_wrapup (sub_588F90)
  |
  +-- Pass 1:  sub_588C60   per-file IL wrapup
  +-- Pass 2:  sub_707040   IL lowering
  +-- Pass 3:  dword_106B640 = 1                          <<<< guard ON
  |            sub_610420(file_scope, 23)                  <<<< MARK PHASE
  |            dword_106B640 = 0                           <<<< guard OFF
  +-- Pass 4:  sub_5CCA40   clear_instantiation_required   <<<< CLEANUP
  |            sub_5CC410   eliminate_bodies_of_unneeded    (if deferred)
  |            sub_5CCBF0   eliminate_unneeded_il_entries   (if deferred)
  +-- Pass 5:  sub_588D40   per-file part_3
```

The guard flag `dword_106B640` is an assertion variable: `mark_to_keep_in_il` asserts it is nonzero during the walk. If the assertion fires, it triggers `internal_error` at `il_walk.c:1959`. This prevents accidental invocation of the marking logic outside the designated pass window.

## Mark Phase: mark_to_keep_in_il (sub_610420)

This is the top-level entry point for device code marking. It orchestrates the entire transitive-closure walk.

### Callback Installation

The function saves all five IL tree-walk callback slots and installs a specialized configuration:

```c
// Save existing callbacks
saved_entry_cb      = qword_126FB88;    // entry_callback
saved_string_cb     = qword_126FB80;    // string_callback
saved_pre_walk      = qword_126FB78;    // pre_walk_check
saved_entry_replace = qword_126FB70;    // entry_replace
saved_entry_filter  = qword_126FB68;    // entry_filter

// Install keep-in-il configuration
qword_126FB88 = NULL;                   // no entry callback
qword_126FB80 = NULL;                   // no string callback
qword_126FB78 = prune_keep_in_il_walk;  // sub_617310
qword_126FB70 = NULL;                   // no replacement
qword_126FB68 = NULL;                   // no filter
dword_126FB60 = 0;                      // walk_mode_flags = 0
dword_126FB58 = (flags_byte & 0x02) != 0;  // is_secondary_il
```

All callbacks are restored on exit, making the function safely re-entrant from nested invocations (the prune callback can recursively call `mark_to_keep_in_il` for canonical entries in secondary IL).

### Scope Initialization

For scope entries (kind 23), the function handles two cases:

1. **Scope already has `keep_in_il` set** (byte at `entry + 28` is nonzero): Call `walk_tree_and_set_keep_in_il` directly. The scope was previously identified as device-relevant.

2. **Fresh scope** (byte at `entry + 28` is zero): Clear bit 7 of the entry's flags byte, then walk. This is the file-scope entry point where the walk begins with the keep bit initially cleared, allowing the recursive walk to set it transitively.

```c
if (entry_kind == 23) {             // scope
    if (*(entry + 28) != 0) {
        walk_tree_and_set_keep_in_il(entry, 23);
    } else {
        *(entry - 8) &= 0x7F;      // clear keep_in_il
        // Debug: "Beginning file scope keep_in_il walk"
        walk_tree_and_set_keep_in_il(entry, 23);
        if (dword_126EFB4 == 2)     // C++ mode
            walk_scope_and_mark_routine_definitions(entry);  // sub_6175F0
    }
}
```

### Global Entry-Kind List Walk

After processing the scope, `mark_to_keep_in_il` iterates all 45+ global entry-kind linked lists. These lists at `0x126E610`--`0x126EA80` hold every file-scope entity indexed by entry kind. The function visits each list and calls `walk_tree_and_set_keep_in_il` on every entry:

```c
// Orphaned scope list (kind 55) -- only entries with keep_definition flag
for (entry = qword_126EBA0; entry; entry = entry->next) {
    if (entry->routine_byte_187 & 0x04)   // keep_definition_in_il set
        walk_tree_and_set_keep_in_il(entry, 55);
}

// Source files (kind 1), constants (kind 2), parameters (kind 3), ...
// through to concepts (kind 72)
for (int kind = 1; kind <= 72; kind++) {
    for (entry = global_list[kind]; entry; entry = entry->next)
        walk_tree_and_set_keep_in_il(entry, kind);
}
```

The iteration order mirrors `walk_file_scope_il` (sub_60E4F0), processing kinds 1 through 72 with some gaps (kinds 24–26, 31–32, 43–45, 49–58, 60, 64–71 are skipped because those lists are empty or handled differently).

### Using-Declaration Fixup

After the main walk, the function processes using-declarations attached to scopes. This is a fixed-point loop that repeats until no new entities are marked:

```c
do {
    changed = 0;
    process_using_decl_list(scope->using_decls, is_class_scope, &changed);
} while (changed);
```

For each scope region (iterated via `entry + 264`), it walks the using-declaration chain and handles six declaration kinds:

| Using-decl kind byte | Name | Action |
|---|---|---|
| `0x33` | Simple using | If target entity is marked, mark the using-decl |
| `0x34` | Using with namespace | If target entity is marked, mark using-decl + namespace |
| `0x35` | Nested scope | Recurse via `sub_6170C0` |
| `0x36` | Using with template | If target entity is marked, mark using-decl + template |
| `6` | Type alias (typedef) | Special: if typedef of a class/struct with `has_definition` flag, and the underlying class is marked, mark the typedef too |
| `66` | Using-everything | Force-mark unconditionally, set `changed = 1` |

The typedef case (kind 6) deserves attention. When a typedef aliases a marked class, the typedef entry gets marked so that device code can reference the class through its alias name. The check verifies `entry + 132 == 12` (typedef type kind), the underlying type is a class/struct/union (kinds 9–11), and the `has_definition` flag (`entry + 161, bit 2`) is set.

## Recursive Worker: walk_tree_and_set_keep_in_il (sub_6115E0)

This 23KB function is structurally identical to the generic `walk_entry_and_subtree` (sub_604170) but specialized: instead of invoking callbacks, it directly sets the `keep_in_il` bit on every reachable sub-entry and recurses.

The function dispatches on entry kind (approximately 80 cases) and for each child pointer it encounters, performs:

```c
if (child != NULL) {
    *(child - 8) |= 0x80;                    // set keep_in_il
    walk_tree_and_set_keep_in_il(child, child_kind);  // recurse
}
```

Key entry kinds and what they transitively mark:

| Entry kind | ID | Children marked |
|---|---|---|
| `source_file` | 1 | file_name, full_name, child files |
| `constant` | 2 | type, string data, address target |
| `parameter` | 3 | type, declared_type, default_arg_expr, attributes |
| `type` | 6 | base_type, member fields, template info, scope, base classes |
| `variable` | 7 | type, initializer expression, attributes |
| `field` | 8 | next field, type, bit_size_constant |
| `routine` | 11 | return_type, parameters, body, template info, exception specs |
| `expression` | 13 | sub-expressions, operands, type references |
| `statement` | 21 | sub-statements, expressions, labels |
| `scope` | 23 | all member lists (variables, routines, types, nested scopes) |
| `template_parameter` | 39 | default values, constraints |
| `namespace` | 28 | associated scope |

The function also handles cross-references in template instantiations: when it encounters a template specialization, it follows the primary template pointer and marks the template definition too. This ensures that if device code uses `vector<int>`, the `vector` template itself is retained.

### Pre-Walk Check Integration

Before recursing into any entry, the walk checks the `pre_walk_check` callback (`qword_126FB78`), which is set to `prune_keep_in_il_walk`. This callback returns 1 (skip) if the entry is already marked, preventing infinite recursion on cyclic references (classes referencing their own members) and avoiding redundant work.

## Prune Callback: prune_keep_in_il_walk (sub_617310)

This callback is installed as the `pre_walk_check` during the keep-in-il walk. It runs before the walker descends into each entry.

### Decision Logic

```c
int prune_keep_in_il_walk(entry_ptr, entry_kind) {
    char flags = *(entry_ptr - 8);

    // Case 1: Secondary IL mismatch -- delegate to canonical
    if (is_secondary_il && !(flags & 0x02)) {
        canonical = lookup_canonical(entry_ptr, entry_kind);  // sub_5B9EE0
        if (dword_126EE48) {   // CUDA mode
            if (canonical && canonical->assoc_entry) {
                target = *canonical->assoc_entry;
                if (target != entry_ptr && (*(target - 8) & 0x02))
                    mark_to_keep_in_il(target, entry_kind);  // recurse
            }
        }
        return 1;  // skip this entry (handled via canonical)
    }

    // Case 2: Already marked -- skip
    if (flags < 0)   // bit 7 set = signed negative
        return 1;

    // Case 3: Type with class/struct/union definition -- mark definition too
    if (entry_kind == 6 && (*(entry + 132) - 9) <= 2) {
        if (is_local || is_imported || !has_name || has_definition)
            set_keep_definition_on_type(entry);  // sub_6111C0
    }

    // Set the keep_in_il bit
    *(entry_ptr - 8) |= 0x80;

    // Debug output
    if (trace_active && trace_filter("needed_flags", entry, kind)) {
        switch (entry_kind) {
            case 6:  fprintf(s, "Setting keep_in_il on type ");  break;
            case 7:  fprintf(s, "Setting keep_in_il on var  ");  break;
            case 11: fprintf(s, "Setting keep_in_il on rout ");  break;
            case 28: fprintf(s, "Setting keep_in_il on namespace "); break;
        }
    }

    // Case 4: Variable/routine in non-guard mode -- check class membership
    if (!dword_106B640) {
        if (!(*(entry + 82) & 0x10)) {
            canonical = lookup_canonical(entry, entry_kind);
            // Assert canonical exists (il_walk.c:1885)
            if (*(canonical + 81) & 0x04) {   // is class member
                class_type = **(canonical + 40 + 32);
                walk_tree_and_set_keep_in_il(class_type, 6);
                set_keep_definition_on_type(class_type);
            }
        }
        return 1;
    }

    // Handle canonical entry in secondary IL (CUDA mode)
    canonical = lookup_canonical(entry, entry_kind);
    if (dword_126EE48 && canonical) {
        assoc = *(canonical + 32);
        if (assoc) {
            target = *assoc;
            if (target != entry && (*(target - 8) & 0x02))
                mark_to_keep_in_il(target, entry_kind);
        }
    }

    return 0;  // continue walking into this entry's children
}
```

The callback's return value controls the walk: returning 1 tells the walker to skip the subtree (entry already processed or delegated to canonical), returning 0 tells it to descend into children.

### Secondary IL Handling

When cudafe++ processes multiple translation units (e.g., through `#include` chains that bring in separate compilation units), it maintains primary and secondary IL regions. The secondary IL flag (bit 1 of the flags byte) distinguishes them. The prune callback handles cross-region references by looking up the canonical (primary) version of each entry via `sub_5B9EE0` and recursively marking that version instead. This ensures the device IL output contains the primary definitions, not secondary duplicates.

## Keep-Definition Logic

### For Types (sub_6111C0 / sub_611300)

When a class/struct/union type needs its definition kept (not just a forward declaration), `set_keep_definition_on_type` performs:

```c
void set_keep_definition_on_type(entry) {
    // Debug: "Setting keep_definition_in_il on <type>"
    *(entry + 162) |= 0x80;           // set keep_definition bit

    // If already marked keep_in_il, clear and re-walk
    // (definition requires deeper traversal than reference)
    if (*(entry - 8) & 0x80) {
        *(entry - 8) &= ~0x80;        // clear keep_in_il
        mark_to_keep_in_il(entry, 6);  // re-walk with full traversal
    }

    // For class/struct: also clear/re-walk the associated scope
    if (entry_kind is class/struct/union) {
        scope = entry->associated_scope;
        *(scope - 8) &= ~0x80;
        // Follow canonical type chain
    }
}
```

The clear-and-re-walk pattern is important: when an entity was initially marked via a shallow reference (e.g., a pointer to the class), only the type entry itself was marked. When the definition is later needed (e.g., the device code accesses a member), the keep bit is cleared and the walk restarts, this time descending into all members, base classes, and nested types.

### For Routines (sub_6113F0 / sub_6181E0)

```c
void set_keep_definition_on_routine(entry) {
    // Debug: "Setting keep_definition_in_il on rout <name>"
    *(entry + 187) |= 0x04;          // set keep_definition bit

    // If template specialization: also mark the primary template
    if (*(entry + 177) & 0x20) {
        primary = lookup_primary_template(entry);  // sub_5BBCC0
        mark_to_keep_in_il(primary, 11);
    }

    // Special member handling (copy/move constructors)
    if (special_member_kind == 1 || special_member_kind == 2) {
        // Recurse on associated class type's ctor/dtor
    }
}
```

## Scope-Level Routine Walk: sub_6175F0

In C++ mode (`dword_126EFB4 == 2`), after the main mark pass, `mark_to_keep_in_il` calls `sub_6175F0` on the file scope. This function performs an additional sweep through all scope hierarchies to ensure routine definitions are correctly retained:

- For each class/struct scope with `keep_in_il` set: recurse into the class scope
- For each namespace (non-alias): recurse into the namespace scope
- For routines in class scopes with external linkage: if marked but not `keep_definition`, call `set_keep_definition_on_routine`

This handles the case where a class method is referenced by device code through a virtual call or template instantiation, requiring the full function body to be available in the device IL.

## Elimination Phase

After the mark phase completes, three functions strip unmarked entities from the IL.

### clear_instantiation_required_on_unneeded_entities (sub_5CCA40)

Runs in pass 4 of `fe_wrapup`, C++ mode only. Prevents unnecessary template instantiations from being triggered during IL output.

The function recursively walks the scope tree and for each routine with template instantiation flags, checks whether the instantiation is still needed:

```c
void clear_instantiation_required_on_unneeded_entities(scope) {
    assert(dword_126EFB4 == 2);  // C++ only

    // Recurse into child scopes (skip namespace aliases)
    for (child = scope->nested_namespaces; child; child = child->next) {
        if (!(child->flags & 0x01))    // not an alias
            recurse(child->associated_scope);
    }

    // Recurse into class scopes
    for (type = scope->types_list; type; type = type->next) {
        if (is_class_struct_union(type) && !is_anonymous(type))
            recurse(type->type_extra->scope_entry);
    }

    // Clear instantiation_required on unneeded routines
    for (rout = scope->routines_list; rout; rout = rout->next) {
        if (!(rout->flags_80 & 0x08)           // not suppressed
            && !(rout->flags_179 & 0x10)        // not already cleared
            && ((rout->flags_179 & 6) == 2 || (dword_126E204 && rout->flags_176 < 0))
            && rout->source_corresp != NULL
            && !(rout->flags_176 & 0x02))       // not locally defined
        {
            clear_instantiation_required(rout->name, 0, 2);  // sub_78A380
        }
    }

    // For non-file scopes: also clear on variable templates
    if (scope->scope_kind != 0) {
        for (var = scope->variables_list; var; var = var->next) {
            if (!(var->flags_80 & 0x08)
                && !(var->flags_162 & 0x40)
                && (var->flags_162 & 0xB0) == 0x10
                && var->name != NULL)
            {
                clear_instantiation_required(var->name, 0, 2);
            }
        }
    }
}
```

### eliminate_bodies_of_unneeded_functions (sub_5CC410)

Walks the IL table (`qword_126EB98`) and removes function bodies for routines that were not marked with `keep_definition_in_il`:

```c
void eliminate_bodies_of_unneeded_functions() {
    for (idx = 1; idx <= dword_126EC78; idx++) {
        scope = qword_126EC88[idx];
        if (!scope) continue;
        if (scope not in current TU) continue;
        if (scope_kind != 17) continue;     // 17 = function body

        routine = scope->owning_entity;
        if (routine->keep_definition_in_il)  // byte+187 & 0x04
            continue;
        if (!(routine->flags_29 & 0x01))
            continue;

        remove_function_body(routine);       // sub_5CAB40
    }
}
```

### eliminate_unneeded_il_entries (sub_5CCBF0)

The main elimination pass. Walks the scope tree and removes all unmarked entities from the IL linked lists.

```c
void eliminate_unneeded_il_entries(scope) {
    emit_info = get_emit_info(scope);       // sub_703C30
    assert(emit_info != NULL);              // il.c:29598

    // Recurse into child scopes (skip namespace aliases)
    for (child = scope->nested_namespaces; child; child = child->next) {
        if (!(child->flags & 0x01))
            eliminate_unneeded_il_entries(child->associated_scope);
    }

    // --- Eliminate variables ---
    prev = NULL;
    for (var = scope->variables_list; var; var = next) {
        next = var->next_in_list;           // offset +104
        if (*(signed char*)(var - 8) < 0) { // keep_in_il set
            prev = var;                     // keep in list
        } else {
            // Unlink from list
            if (prev) prev->next = next;
            else scope->variables_list = next;
            var->next = NULL;
            // C++ mode: walk expression trees to clear hidden names
            if (cpp_mode) {
                walk_tree(var->expr_tree, clear_hidden_name_cb, 147);
                walk_tree(var->alt_tree, clear_hidden_name_cb, 147);
            }
        }
    }
    emit_info[5] = prev;   // last kept variable

    // File scope: also clean orphaned scope list
    if (scope->scope_kind == 0)
        eliminate_unneeded_scope_orphaned_list_entries();  // sub_5CC570

    // --- Eliminate routines (same pattern as variables) ---
    prev = NULL;
    for (rout = scope->routines_list; rout; rout = next) {
        next = rout->next_in_list;
        if (*(signed char*)(rout - 8) < 0) {
            prev = rout;
        } else {
            // Unlink + clear hidden names
        }
    }
    emit_info[6] = prev;

    // Clear global variable reference if unmarked
    if (qword_126EB70 && *(signed char*)(qword_126EB70 - 8) >= 0)
        qword_126EB70 = NULL;

    // --- Eliminate types ---
    prev = NULL;
    for (type = scope->types_list; type; type = next) {
        next = type->next_in_list;

        // Follow typedef chains to find the real type for sign-bit check
        real = type;
        if (real->type_kind == 12 && !real->name) {  // anonymous typedef
            do { real = real->base_type; }
            while (real->type_kind == 12 && !real->name);
        }

        if (*(signed char*)(real - 8) < 0) {   // marked
            prev = type;
            if (is_class_struct_union(type))
                eliminate_unneeded_class_definitions(type);  // sub_5CC1B0
        } else {
            // Unlink + process eliminated class members
            if (is_class_struct_union(type) && cpp_mode)
                process_members_of_eliminated_class(type);
            type->base_type = NULL;
            clear_type_extra_member_lists(type->type_extra);
            type->type_extra->flags |= 0x20;  // mark as eliminated
        }
    }
    emit_info[4] = prev;

    // --- Eliminate hidden names ---
    // (same sign-bit check, unlink unmarked entries from scope->hidden_names)

    // File scope: emit orphaned scopes
    if (scope->scope_kind == 0)
        emit_orphaned_scopes(scope);        // sub_718720

    // Clean external declarations list
    for (ext = qword_126EBE0; ext; ext = next) {
        next = ext->next;
        if (*(signed char*)(ext - 8) >= 0) {
            // Unlink from external declarations list
        }
    }
}
```

The debug output for eliminated vs. retained entities uses a string trick: `"TARG_VERT_TAB_CHAR" + 17` evaluates to `"R"`, so the output reads either `"Removing variable <name>"` (eliminated) or `"Not removing variable <name>"` (retained).

## Global State

| Address | Name | Description |
|---|---|---|
| `dword_106B640` | `keep_in_il_walk_active` | Assertion guard; 1 during pass 3, 0 otherwise |
| `dword_126EFB4` | `cpp_mode` | 2 = C++ mode (enables class/template processing) |
| `dword_126EFC8` | `trace_active` | Nonzero enables diagnostic output |
| `dword_126EFCC` | `trace_verbosity` | Higher = more output (>2 prints elimination details) |
| `dword_126EE48` | `cuda_mode` | Nonzero enables CUDA-specific canonical entry handling |
| `dword_126E204` | `template_compat_flag` | Affects template instantiation clearing criteria |
| `qword_126EBA0` | `orphaned_scope_list` | File-scope orphaned scopes (kind 55 list) |
| `qword_126EB70` | `global_variable_ref` | Cleared if its entry is unmarked |
| `qword_126EBE0` | `external_decl_list` | External declarations; unmarked ones removed |
| `qword_126EB98` | `il_table` | Array of IL scope pointers, indexed by il_table_index |
| `qword_126FB78` | `pre_walk_check` | Callback slot; set to `prune_keep_in_il_walk` during mark |
| `qword_126FB88` | `entry_callback` | Callback slot; NULL during mark phase |
| `dword_126FB58` | `is_secondary_il` | Walk state: 1 if currently in secondary IL region |
| `dword_126FB5C` | `is_file_scope_walk` | Walk state: 1 during file-scope walk |
| `dword_106B644` | `current_il_region` | Walk state: toggles per IL region |
| `dword_126FB60` | `walk_mode_flags` | Walk state: 0 during keep-in-il walk |

## Diagnostic Strings

| String | Source | Condition |
|---|---|---|
| `"Beginning file scope keep_in_il walk"` | `sub_610420` | `trace_active && trace_category("needed_flags")` |
| `"Ending file scope keep_in_il walk"` | `sub_610420` | Same |
| `"Setting keep_in_il on type "` | `sub_617310` | `trace_active && trace_filter("needed_flags", entry, 6)` |
| `"Setting keep_in_il on var  "` | `sub_617310` | Same, kind 7 |
| `"Setting keep_in_il on rout "` | `sub_617310` | Same, kind 11 |
| `"Setting keep_in_il on namespace "` | `sub_617310` | Same, kind 28 |
| `"Setting keep_definition_in_il on "` | `sub_6111C0` | Trace active |
| `"Setting keep_definition_in_il on rout "` | `sub_6113F0` | Trace active |
| `"Removing variable <name>"` | `sub_5CCBF0` | `trace_verbosity > 2` or `trace_filter("dump_elim")` |
| `"Not removing variable <name>"` | `sub_5CCBF0` | Same (for kept entries) |
| `"Removing routine <name>"` | `sub_5CCBF0` | Same |
| `"Removing <type>"` | `sub_5CCBF0` | Same |
| `"eliminate_unneeded_il_entries"` | `sub_5CCBF0` | `trace_active` (level 3 trace enter/exit) |

## Function Map

| Address | Identity | Confidence | Lines | EDG Source |
|---|---|---|---|---|
| `sub_610420` | `mark_to_keep_in_il` | 99% | 892 | `il_walk.c:1959` |
| `sub_6115E0` | `walk_tree_and_set_keep_in_il` | 98% | 4649 | `il_walk.c` |
| `sub_617310` | `prune_keep_in_il_walk` | 99% | 127 | `il_walk.c:1885` |
| `sub_6111C0` | `set_keep_definition_on_type` | 95% | 63 | `il_walk.c` |
| `sub_611300` | `set_keep_definition_on_type_simple` | 92% | 48 | `il_walk.c` |
| `sub_6113F0` | `set_keep_definition_on_routine` | 95% | 81 | `il_walk.c` |
| `sub_6181E0` | `set_keep_definition_on_routine_unconditional` | 90% | 69 | `il_walk.c` |
| `sub_6170C0` | `process_using_decl_list` | 92% | 154 | `il_walk.c` |
| `sub_6175F0` | `walk_scope_and_mark_routine_definitions` | 90% | 634 | `il_walk.c` |
| `sub_616EE0` | `mark_virtual_function_types_to_keep` | 85% | 88 | `il_walk.c` |
| `sub_618370` | `walk_and_set_keep_in_il_helper` | 80% | 119 | `il_walk.c` |
| `sub_618660` | `walk_entry_and_set_keep_in_il_routine_scope` | 88% | 3728 | `il_walk.c` |
| `sub_5CCBF0` | `eliminate_unneeded_il_entries` | 100% | 345 | `il.c:29598` |
| `sub_5CCA40` | `clear_instantiation_required_on_unneeded_entities` | 100% | 86 | `il.c:29450` |
| `sub_5CC410` | `eliminate_bodies_of_unneeded_functions` | 100% | ~200 | `il.c:29231` |
| `sub_5CC1B0` | `eliminate_unneeded_class_definitions` | 100% | ~200 | `il.c` |
| `sub_5CC570` | `eliminate_unneeded_scope_orphaned_list_entries` | 100% | ~200 | `il.c:29398` |
| `sub_5CB920` | `process_members_of_eliminated_class_definition` | 100% | ~300 | `il.c:29097` |
| `sub_5B9EE0` | `lookup_canonical_entry` | — | — | `il_walk.c` |
| `sub_78A380` | `clear_instantiation_required` | — | — | `template.c` |

## Cross-References

- [Pipeline Overview](../pipeline/overview.md) — overall compilation flow
- [IL Overview](./overview.md) — entry kinds, header, regions
- [IL Tree Walking](./walking.md) — generic walker with 5 callbacks
- [Device/Host Separation](../cuda/device-host-separation.md) — higher-level splitting strategy
- [Execution Spaces](../cuda/execution-spaces.md) — how entities get device/host attributes
