# Template Instance Record

The template instance record is the 128-byte structure that represents a pending or completed template instantiation in cudafe++ (EDG 6.6). Every template entity that may require instantiation — function templates, class member function templates, variable templates — gets one of these records allocated by `alloc_template_instance` (`sub_7416E0`). The records are chained into a singly-linked worklist for function/variable templates (`qword_12C7740`). A separate worklist of type entries (not instance records) at `qword_12C7758` tracks pending class template instantiations. A fixpoint loop at translation-unit end drains both lists, instantiating entities until no new work remains.

This page documents the instance record layout, the master instance info record, the two worklists, the depth-tracking mechanisms, the parser state save/restore during instantiation, and the fixpoint algorithm that ties everything together.

## Key Facts

| Property | Value |
|---|---|
| Instance record size | 128 bytes (allocated by `sub_7416E0`, `alloc_template_instance`) |
| Master instance info size | 32 bytes (allocated by `sub_7416A0`, `alloc_master_instance_info`) |
| Allocation counter (instances) | `qword_12C74F0` (incremented on each 128-byte allocation) |
| Allocation counter (master info) | `qword_12C74E8` (incremented on each 32-byte allocation) |
| Memory allocator | `sub_6BA0D0` (EDG arena allocator) |
| Function/variable worklist head | `qword_12C7740` |
| Function/variable worklist tail | `qword_12C7738` |
| Class worklist head | `qword_12C7758` |
| Fixpoint entry point | `sub_78A9D0` (`template_and_inline_entity_wrapup`) |
| Worklist walker | `sub_78A7F0` (`do_any_needed_instantiations`) |
| Decision gate | `sub_774620` (`should_be_instantiated`) |
| Source file | `templates.c` (EDG 6.6, path `edg/EDG_6.6/src/templates.c`) |

## Instance Record Layout (128 bytes)

Each record is allocated by `alloc_template_instance` (`sub_7416E0`) and zero-initialized. The allocator clears all 128 bytes, then initializes offsets `+84` and `+92` from `qword_126EFB8` (the current source position context). The low nibble of byte `+81` is explicitly masked to zero (`*(_BYTE *)(result + 81) &= 0xF0`).

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `entity_primary` | Primary entity pointer (the instantiation's own symbol) |
| `+8` | 8 | `next` | Next entry in the pending worklist (singly-linked) |
| `+16` | 8 | `inst_info` | Pointer to 32-byte master instance info record (see below) |
| `+24` | 8 | `master_symbol` | Canonical template symbol — the entity being instantiated from |
| `+32` | 8 | `actual_decl` | Declaration entity in the instantiation context |
| `+40` | 8 | `cached_decl` | Cached declaration for function-local templates (partial specialization lookup result) |
| `+48` | 8 | `referencing_namespace` | Namespace that triggered the instantiation (set by `determine_referencing_namespace`, `sub_75D5B0`) |
| `+56` | 8 | (reserved) | Zero-initialized, usage not observed |
| `+64` | 8 | `body_flags` | Deferred/deleted function body flags |
| `+72` | 8 | `pre_computed_result` | Result from a prior instantiation attempt (non-null skips re-instantiation) |
| `+80` | 1 | `flags` | Status bitfield (see flags table below) |
| `+81` | 1 | `flags2` | Secondary flags (bit 0 = on_worklist, bit 1 = warning_emitted) |
| `+84` | 8 | `source_position_1` | Source location context at entry creation (from `qword_126EFB8`) |
| `+92` | 8 | `source_position_2` | Second source location context at entry creation (from `qword_126EFB8`) |
| `+104` | 8 | (reserved) | Zero-initialized |
| `+112` | 8 | (reserved) | Zero-initialized |
| `+120` | 8 | (reserved) | Zero-initialized |

### Allocator Pseudocode

```c
// sub_7416E0 — alloc_template_instance
template_instance_t *alloc_template_instance(void) {
    if (debug_tracing_enabled)
        trace_enter(5, "alloc_template_instance");

    template_instance_t *rec = arena_alloc(128);   // sub_6BA0D0
    alloc_count_instances++;                        // qword_12C74F0

    // Zero all fields
    rec->entity_primary    = NULL;    // +0
    rec->next              = NULL;    // +8
    rec->inst_info         = NULL;    // +16
    rec->master_symbol     = NULL;    // +24
    rec->actual_decl       = NULL;    // +32
    rec->cached_decl       = NULL;    // +40
    rec->ref_namespace     = NULL;    // +48
    rec->reserved_56       = NULL;    // +56
    rec->body_flags        = NULL;    // +64
    rec->precomputed       = NULL;    // +72
    rec->flags             = 0;       // +80
    rec->flags2           &= 0xF0;   // +81: clear low nibble
    rec->source_pos_1      = current_source_context;  // +84 from qword_126EFB8
    rec->source_pos_2      = current_source_context;  // +92 from qword_126EFB8
    rec->reserved_104      = NULL;    // +104
    rec->reserved_112      = NULL;    // +112
    rec->reserved_120      = NULL;    // +120

    if (debug_tracing_enabled)
        trace_leave();
    return rec;
}
```

### Flags Byte at `+80`

Six bits are used. The byte is written by `update_instantiation_required_flag` (`sub_7770E0`) and read by `do_any_needed_instantiations` (`sub_78A7F0`) and `should_be_instantiated` (`sub_774620`).

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | `instantiation_required` | Entity needs instantiation (set by `update_instantiation_required_flag`) |
| 1 | `0x02` | `not_needed` | Entity was determined not to need instantiation (skip on worklist walk) |
| 3 | `0x08` | `explicit_instantiation` | Explicit `template` declaration triggered this entry |
| 4 | `0x10` | `suppress_auto` | Auto-instantiation suppressed (from `extern template` declaration) |
| 5 | `0x20` | `excluded` | Entity excluded from instantiation set |
| 7 | `0x80` | `can_be_instantiated_checked` | Pre-check (`f_entity_can_be_instantiated`) already performed; skip redundant check |

### Flags Byte at `+81`

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | `on_worklist` | Entry has been linked into the pending worklist |
| 1 | `0x02` | `warning_emitted` | Depth-limit warning already emitted for this entry |

The `on_worklist` bit at `+81` bit 0 is the guard that prevents double-insertion into the linked list. When `add_to_instantiations_required_list` sets up the linked list linkage (at `qword_12C7740`/`qword_12C7738`), it checks this bit first and sets it afterward. If the bit is already set, the function takes the "already on worklist" path which may set the `new_instantiations_needed` fixpoint flag (`dword_12C771C = 1`) instead.

## Master Instance Info Record (32 bytes)

Each template entity (class, function, or variable template) has exactly one master instance info record, allocated by `alloc_master_instance_info` (`sub_7416A0`). This record is shared across all instantiations of the same template and is stored at the template's associated scope info (`scope_assoc + 16`). The link between a 128-byte instance record and its master info is at instance `+16`.

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `next` | Next master info in chain (linked list) |
| `+8` | 8 | `back_pointer` | Pointer back to the template instance record that owns this info |
| `+16` | 8 | `associated_scope` | Pointer to the associated scope/translation-unit data |
| `+24` | 4 | `pending_count` | Number of pending instantiations of this template (incremented/decremented by `update_instantiation_required_flag`) |
| `+28` | 1 | `flags` | Status bits (low nibble cleared on allocation) |

### Master Info Flags Byte at `+28`

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | `blocked` | Instantiation blocked (dependency cycle or `extern template`) |
| 2 | `0x04` | `has_instances` | At least one instantiation has been completed |
| 3 | `0x08` | `debug_checked` | Already checked by debug tracing path |

### Allocator Pseudocode

```c
// sub_7416A0 — alloc_master_instance_info
master_instance_info_t *alloc_master_instance_info(void) {
    master_instance_info_t *info = arena_alloc(32);   // sub_6BA0D0
    alloc_count_master_info++;                         // qword_12C74E8

    info->next             = NULL;    // +0
    info->back_pointer     = NULL;    // +8
    info->associated_scope = NULL;    // +16
    info->pending_count    = 0;       // +24
    info->flags           &= 0xF0;   // +28: clear low nibble

    return info;
}
```

### find_or_create_master_instance (sub_753550)

This function connects a 128-byte instance record to its shared master info. It looks up the template's scope association, checks whether a master info record already exists at `scope_assoc + 16`, and creates one if absent.

```c
// sub_753550 — find_or_create_master_instance
void find_or_create_master_instance(template_instance_t *inst) {
    entity_t *sym = inst->master_symbol;            // inst[3], offset +24
    scope_t  *scope = resolve_template_scope(sym);  // sub_73DE50

    // Find the template's canonical entity
    entity_t *canonical;
    if (is_variable(sym))                           // (kind - 7) & 0xFD == 0
        canonical = *find_variable_correspondence(scope);   // sub_79AAA0
    else
        canonical = *find_function_correspondence(scope);   // sub_79FD80
    assert(canonical != NULL, "find_or_create_master_instance");

    scope_assoc_t *assoc = canonical->scope_assoc;  // offset +96
    master_instance_info_t *info = assoc->master_info;  // assoc + 16

    if (info == NULL) {
        // First instantiation of this template — allocate master info
        info = alloc_master_instance_info();         // sub_7416A0
        info->back_pointer = inst;                   // info[1] = inst

        if (sym != inst->actual_decl) {
            // Class members: add to secondary deferred list
            // qword_12C7750 / qword_12C7748
            append_to_deferred_list(info);
        }
        assoc->master_info = info;                   // assoc + 16

        if (debug_tracing_enabled) {
            trace("find_or_create_master_instance: symbol:");
            print_symbol(inst->master_symbol);
        }
    }

    inst->inst_info = info;                          // inst[2], offset +16
}
```

## The Two Worklists

Template instantiation uses two separate worklists — one for class templates, one for function/variable templates. This separation is fundamental to correctness: class templates must be instantiated before function templates within each fixpoint iteration, because function template bodies may reference members of class template instantiations.

### Function/Variable Worklist (`qword_12C7740`)

| Global | Purpose |
|---|---|
| `qword_12C7740` | Head of the singly-linked list |
| `qword_12C7738` | Tail pointer (for O(1) append) |

Entries are 128-byte instance records linked through `+8` (next pointer). New entries are appended at the tail by `add_to_instantiations_required_list` (the tail section of `sub_7770E0`).

### Class Worklist (`qword_12C7758`)

This list holds type entries (not 128-byte instance records) that need class template instantiation. Entries are linked through offset `+0` of the type entry. The list is populated by `update_instantiation_flags` (`sub_789EF0`) and drained by `template_and_inline_entity_wrapup` (`sub_78A9D0`).

### Worklist Insertion: add_to_instantiations_required_list

The tail portion of `update_instantiation_required_flag` (`sub_7770E0`, starting at the label checking `inst->flags2 & 0x01`) implements worklist insertion:

```c
// Tail of sub_7770E0 — add_to_instantiations_required_list
void add_to_instantiations_required_list(template_instance_t *inst) {
    if (inst->flags2 & 0x01) {
        // Already on the worklist — do not re-add.
        // But if instantiation mode is active and instantiation_required is set,
        // signal that a new fixpoint pass is needed.
        if (instantiation_mode_active
            && (inst->flags & 0x01)
            && inst->inst_info != NULL
            && !(inst->inst_info->flags & 0x01))     // not blocked
        {
            new_instantiations_needed = 1;            // dword_12C771C
            tu_ptr->needs_recheck = 1;                // TU + 393
        }
        return;
    }

    // Link into the function/variable worklist
    if (pending_list_head)                            // qword_12C7740
        pending_list_tail->next = inst;               // qword_12C7738->next
    else
        pending_list_head = inst;

    pending_list_tail = inst;
    inst->flags2 |= 0x01;                            // mark as on worklist

    // Verify correct translation unit
    tu_t *tu = trans_unit_for_symbol(inst->master_symbol);  // sub_741960
    assert(tu == current_tu,
           "add_to_instantiations_required_list: symbol for wrong translation unit");
}
```

## The Fixpoint Loop

The fixpoint loop is the algorithm that drives all template instantiation in cudafe++. It runs at the end of each translation unit, after parsing is complete, and iterates until no new instantiation work remains. The entry point is `template_and_inline_entity_wrapup` (`sub_78A9D0`).

### Algorithm

```python
template_and_inline_entity_wrapup (sub_78A9D0):

    assert(tu_stack_top == 0)          // qword_106BA18: not nested in another TU
    assert(compilation_phase == 2)     // dword_126EFB4: full compilation mode

    LOOP:
        FOR EACH translation_unit IN tu_list (qword_106B9F0):

            set_up_tu_context(tu)      // sub_7A3EF0

            // PHASE 1 — Class templates (from qword_12C7758)
            for entry in class_worklist:
                if is_dependent_type(entry)       continue
                if !is_class_or_struct(entry)     continue
                f_instantiate_template_class(entry)

            // PHASE 2 — Enable instantiation mode
            instantiation_mode_active = 1         // dword_12C7730

            // PHASE 3 — Function/variable templates
            do_any_needed_instantiations()        // sub_78A7F0

            tear_down_tu_context()                // sub_7A3F70

        // PHASE 4 — Check fixpoint condition
        new_instantiations_needed = 0             // dword_12C771C
        FOR EACH translation_unit IN tu_list:
            if tu->needs_recheck:                 // TU + 393
                tu->needs_recheck = 0
                set_up_tu_context(tu)
                do_any_needed_instantiations()
                // process inline entities
                tear_down_tu_context()
                additional_pass_needed = 1        // dword_12C7718

            if new_instantiations_needed:
                GOTO LOOP                         // restart fixpoint
```

The fixpoint is necessary because instantiating one template can trigger references to other not-yet-instantiated templates. For example, instantiating `std::vector<Foo>` may require instantiating `std::allocator<Foo>`, `Foo`'s copy constructor, comparison operators, and any other templates used in `std::vector`'s implementation. Each such reference may add a new entry to the worklist, which the next pass will discover and process. The loop terminates when a complete pass produces no new entries.

### Worklist Walker: do_any_needed_instantiations

`sub_78A7F0` performs a linear walk over the function/variable worklist. For each entry, it applies a series of rejection filters, and if the entry passes all of them, dispatches to `instantiate_template_function_full` (`sub_775E00`).

```c
// sub_78A7F0 — do_any_needed_instantiations
void do_any_needed_instantiations(void) {
    template_instance_t *entry = pending_list_head;   // qword_12C7740

    while (entry) {
        // 1. Already processed?
        if (entry->flags & 0x02) {                    // not_needed
            entry = entry->next;
            continue;
        }

        // 2. Get master instance info
        master_instance_info_t *info = entry->inst_info;  // offset +16
        assert(info != NULL, "do_any_needed_instantiations");

        // 3. Blocked by dependency?
        if (info->flags & 0x01) {                     // blocked
            entry = entry->next;
            continue;
        }

        // 4. Check if debug-verified
        if (!(info->flags & 0x08))                    // not debug_checked
            f_is_static_or_inline(entry);             // sub_756B40

        // 5. Pre-check if not already done
        if (!(entry->flags & 0x80))                   // can_be_instantiated not checked
            f_entity_can_be_instantiated(entry);      // sub_7574B0

        // 6. Mode filter
        if (compilation_mode != 1                     // dword_106C094
            && !(entry->flags & 0x01))                // not instantiation_required
        {
            entry = entry->next;
            continue;
        }

        // 7. Blocked after pre-check?
        if (info->flags & 0x01) {
            entry = entry->next;
            continue;
        }

        // 8. Decision gate
        if (!should_be_instantiated(entry, 1)) {      // sub_774620
            entry = entry->next;
            continue;
        }

        // 9. Instantiate
        instantiate_template_function_full(entry, 1); // sub_775E00
        entry = entry->next;
    }
}
```

The walk is a simple forward traversal. Entries appended during instantiation (by new `add_to_instantiations_required_list` calls from within instantiated function bodies) that land after the current position will be visited on this same pass. Entries that land before the current position, or entries whose status changes after being skipped, are caught by the next fixpoint iteration.

### Decision Gate: should_be_instantiated

`sub_774620` (326 lines) is the final filter before instantiation. It implements an eight-step rejection chain. An entity must pass every step to be instantiated.

```c
// sub_774620 — should_be_instantiated
bool should_be_instantiated(template_instance_t *inst, int check_implicit) {
    master_instance_info_t *info = inst->inst_info;     // +16
    assert(info != NULL, "should_be_instantiated");

    // 1. Blocked?
    if (info->flags & 0x01)          return false;

    // 2. Excluded?
    if (inst->flags & 0x20)          return false;      // excluded

    // 3. Explicit but not required?
    if (inst->flags & 0x08) {                           // explicit_instantiation
        if (!(inst->flags & 0x01))   return false;      // not marked required
    }

    // 4. Not required and not in normal mode?
    if (!(inst->flags & 0x01) && compilation_mode != 1)
        return false;

    // 5. Has valid master_symbol?
    entity_t *master = inst->master_symbol;             // +24
    if (!master)                     return false;

    // 6. Entity kind check
    //    Function: kind 10/11 (member), kind 9 (namespace-scope)
    //    Variable: kind 7
    //    class-local function: kind 17 (lambda)
    int kind = master->kind;                            // master + 80
    // ... kind-specific filtering ...

    // 7. Template body available?
    //    Check that the template has a cached body to replay
    if (!has_template_body(inst))     return false;

    // 8. Implicit include?
    if (check_implicit && implicit_include_enabled) {
        do_implicit_include_if_needed(inst);            // sub_754A70
        // re-check body availability after include
    }

    // 9. Depth limit warning (diagnostics 489/490)
    if (approaching_depth_limit(inst)) {
        if (!(inst->flags2 & 0x02)) {                   // warning not yet emitted
            emit_warning(489 or 490, inst->master_symbol);
            inst->flags2 |= 0x02;                       // mark warning emitted
        }
        return false;
    }

    return true;
}
```

## Depth Tracking

Template instantiation depth is tracked at two levels — a global counter for function templates and a per-type counter for class templates — plus a pending-instantiation counter that detects runaway expansion.

### Function Template Depth: `qword_12C76E0`

A single global counter incremented on entry to `instantiate_template_function_full` and decremented on exit. Hard limit: 255 (`0xFF`).

```c
// Inside sub_775E00 (instantiate_template_function_full):
if (instantiation_depth >= 0xFF) {                // qword_12C76E0
    emit_fatal_error(/* depth exceeded */);
    goto restore_state;
}
instantiation_depth++;
// ... perform instantiation ...
instantiation_depth--;
```

The 255 limit is a safety valve against infinite recursive template metaprogramming. Consider:

```cpp
template<int N>
struct factorial {
    static constexpr int value = N * factorial<N-1>::value;
};
```

Without a depth limit, `factorial<256>` would recurse 256 levels deep, each level re-entering the parser to process the template body. At 255, EDG aborts with a fatal error rather than risk a stack overflow. The C++ standard (Annex B) recommends implementations support at least 1,024 recursively nested template instantiations, but EDG defaults to 255 as a practical limit — configurable via `qword_106BD10`.

### Class Template Depth: Per-Type Counter at `type_entry + 56`

Each class type entry has its own depth counter at offset `+56`. The limit is read from `qword_106BD10` (the same configurable limit, typically 500). This per-type design is critical: it prevents one deeply-nested class hierarchy from blocking all other class instantiations.

```c
// Inside sub_777CE0 (f_instantiate_template_class):
uint32_t depth = type_entry->depth_counter;       // type_entry + 56
if (depth >= max_depth_limit) {                   // qword_106BD10
    emit_error(456, decl);
    type_entry->flags |= 0x01;                    // mark completed
    goto restore_state;
}
type_entry->depth_counter++;
// ... perform class instantiation ...
type_entry->depth_counter--;
```

### Pending Instantiation Counter: increment/decrement/too_many

Three functions manage a per-type pending-instantiation counter that detects exponential expansion of template instantiation work.

**increment_pending_instantiations (`sub_75D740`)**: dispatches on the entity kind byte at `entity + 80` to locate the owning type entry, then increments the counter at `type_entry + 56`.

**decrement_pending_instantiations (`sub_75D7C0`)**: mirror of the above, decrements.

**too_many_pending_instantiations (`sub_75D6A0`)**: compares the counter against `qword_106BD10`. If the threshold is met, emits diagnostic 456 and returns true to abort the instantiation.

```c
// sub_75D6A0 — too_many_pending_instantiations
bool too_many_pending_instantiations(entity_t *entity, entity_t *context,
                                     source_pos_t *pos) {
    type_entry_t *type = resolve_owning_type(entity);  // dispatch on entity->kind
    assert(type != NULL, "too_many_pending_instantiations");

    uint32_t count = type->pending_counter;            // type + 56
    if (count >= max_depth_limit) {                    // qword_106BD10
        emit_error(456, pos, context);
        return true;
    }
    return false;
}
```

The entity-kind dispatch is identical across all three functions:

| Entity Kind | Byte `+80` | Type Entry Resolution |
|---|---|---|
| 4, 5 (template member function) | `entity->scope_assoc->field_80` | `*(entity->assoc + 96)->offset_80` |
| 6 (type alias template) | `entity->scope_assoc->field_32` | `*(entity->assoc + 96)->offset_32` |
| 9, 10 (namespace function, class) | `entity->scope_assoc->field_56` | `*(entity->assoc + 96)->offset_56` |
| 19-22 (class member types) | `entity->type_info` | `entity->offset_88` |

### Depth Limit Counter at `type_entry + 432`

Inside `update_instantiation_required_flag` (`sub_7770E0`), a secondary counter at `type_entry + 432` (a 16-bit word) tracks how many times an entity's instantiation-required flag has been toggled. When this counter reaches 200, diagnostic 599 is emitted as a warning. If it exceeds 199, the instantiation is skipped entirely. This catches oscillating patterns where two mutually-dependent templates keep adding and removing each other from the worklist.

```c
// Inside sub_7770E0, compilation_mode == 1 path:
if (!setting_required && is_function_or_variable(master_symbol)) {
    int16_t toggle_count = *(int16_t *)(type_entry + 432);
    toggle_count++;
    *(int16_t *)(type_entry + 432) = toggle_count;
    if (toggle_count == 200)
        emit_warning(599, actual_decl);
    if (toggle_count > 199)
        return;  // stop oscillating
}
```

## Parser State Save/Restore During Instantiation

Template instantiation re-enters the parser: the compiler replays the cached template body tokens with substituted types. This means the parser's global state — scope indices, current token, source position, declaration context — must be saved before instantiation and restored afterward. EDG uses `movups`/`movaps` SSE instructions to bulk-save/restore this state in 128-bit chunks.

### Why SSE?

The global parser state variables are ordinary integers, pointers, and flags laid out at consecutive addresses. The compiler's register allocator (or manual optimization) packs adjacent globals into 128-bit SSE loads/stores, saving 4 or more individual `mov` instructions per save/restore. This is not a quirk of the architecture — it is a deliberate performance optimization for a hot path. Template-heavy C++ codebases (Boost, STL, Eigen) can trigger thousands of instantiations, each requiring a state save/restore pair.

### Function Instantiation: 4 SSE Registers

`instantiate_template_function_full` (`sub_775E00`) saves and restores 4 SSE registers covering 64 bytes of parser state at addresses `0x106C380`--`0x106C3B0`.

```text
Save on entry (before any parser re-entry):
    local[0] = xmmword_106C380      // 16 bytes: parser scope context
    local[1] = xmmword_106C390      // 16 bytes: token stream state
    local[2] = xmmword_106C3A0      // 16 bytes: scope nesting info
    local[3] = xmmword_106C3B0      // 16 bytes: auxiliary flags

Also saved as individual scalars:
    saved_source_pos     = qword_126DD38
    saved_source_col     = WORD2(qword_126DD38)
    saved_diag_pos       = qword_126EDE8
    saved_diag_col       = WORD2(qword_126EDE8)

Restore on exit (always, even on error path):
    xmmword_106C380 = local[0]
    xmmword_106C390 = local[1]
    xmmword_106C3A0 = local[2]
    xmmword_106C3B0 = local[3]
    qword_126DD38   = saved_source_pos
    qword_126EDE8   = saved_diag_pos
```

### Class Instantiation: 11 + 12 SSE Registers (Conditional)

`f_instantiate_template_class` (`sub_777CE0`) saves substantially more state because class body parsing involves deeper parser perturbation — member declarations, nested types, access specifiers, base class processing, and member template definitions all modify global parser state.

The save is conditional on the current token kind (`word_126DD58`). If the token kind is between 2 and 8 inclusive (meaning the parser is mid-expression or mid-declaration when the class instantiation is triggered), the full save executes:

**Primary state block** (always saved when token is 2--8): 11 SSE registers from `xmmword_126DC60`--`xmmword_126DD00`, covering 176 bytes of declaration parser state, plus `qword_126DD10` (8 bytes).

```text
Save:
    local[0]  = xmmword_126DC60     // declaration context
    local[1]  = xmmword_126DC70     // access specifier state
    local[2]  = xmmword_126DC80     // base class list context
    local[3]  = xmmword_126DC90     // member template tracking
    local[4]  = xmmword_126DCA0     // nested type state
    local[5]  = xmmword_126DCB0     // friend declaration context
    local[6]  = xmmword_126DCC0     // using declaration state
    local[7]  = xmmword_126DCD0     // default argument context
    local[8]  = xmmword_126DCE0     // static assertion state
    local[9]  = xmmword_126DCF0     // concept/requires state
    local[10] = xmmword_126DD00     // template parameter context
    saved_dd10 = qword_126DD10      // additional scalar
```

**Extended state block** (saved only when token kind == 8, class definition in progress): 12 more SSE registers from `xmmword_126DBA0`--`xmmword_126DC40`, covering 192 bytes, plus `qword_126DC50`.

```text
    local[11] = xmmword_126DBA0     // class body parse state
    local[12] = xmmword_126DBB0     // virtual function table context
    local[13] = xmmword_126DBC0     // constructor/destructor tracking
    local[14] = xmmword_126DBD0     // initializer list state
    local[15] = xmmword_126DBE0     // exception specification
    local[16] = xmmword_126DBF0     // noexcept evaluation context
    local[17] = xmmword_126DC00     // member initializer state
    local[18] = xmmword_126DC10     // default member init
    local[19] = xmmword_126DC20     // alignment tracking
    local[20] = unk_126DC30         // padding/layout state
    local[21] = xmmword_126DC40     // class completion state
    saved_dc50 = qword_126DC50      // additional scalar
```

The conditional save is a performance optimization: when the parser is in a simple context (token kind outside 2--8), the class instantiation only needs to save the 4 SSE registers from `xmmword_106C380`--`xmmword_106C3B0` (same as function instantiation). The full 23-register save is only needed when a class instantiation is triggered mid-parse (e.g., during elaborated type specifier resolution or SFINAE evaluation).

### Summary of State Save Areas

| Instantiation Kind | Condition | SSE Registers | Bytes Saved | Address Range |
|---|---|---|---|---|
| Function | Always | 4 | 64 | `0x106C380`--`0x106C3B0` |
| Class (minimal) | token not 2--8 | 4 | 64 | `0x106C380`--`0x106C3B0` |
| Class (mid-declaration) | token 2--8 | 4 + 11 | 64 + 184 | `0x106C380`--`0x106C3B0` + `0x126DC60`--`0x126DD10` |
| Class (mid-class-body) | token == 8 | 4 + 11 + 12 | 64 + 184 + 200 | All three ranges |

## The update_instantiation_required_flag Function

`sub_7770E0` (434 lines) is the central function that decides whether to add a template instance to the worklist. Its name in EDG source is `update_instantiation_required_flag`, confirmed by the assert string at `templates.c:38863` and the debug trace `"Setting instantiation_required flag to %s for (options=%d)"`.

This function is called whenever a template entity's instantiation status changes — when a template is first referenced, when it is explicitly instantiated, when its definition becomes available, or when an `extern template` declaration is encountered.

### Parameters

```c
void update_instantiation_required_flag(
    template_instance_t *inst,     // a1: the instance record
    bool                 setting,  // a2 (int cast): true = mark required, false = unmark
    unsigned int         options   // a3: bitmask controlling behavior
);
```

### Options Bitmask

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | Force worklist addition even without inline body |
| 1 | `0x02` | Unmarking: decrement pending count and clear flags |
| 2 | `0x04` | Suppress `should_be_instantiated` check |
| 3 | `0x08` | Check for inline member of class template |

### High-Level Flow

```python
update_instantiation_required_flag(inst, setting, options):

    // 1. Resolve owning type entry (for toggle counter)
    type_entry = resolve_type_from_actual_decl(inst->actual_decl)

    // 2. Check inline member status
    if is_function_or_variable(master_symbol):
        if is_inline_member:
            adjust options based on module/inline status

    // 3. Debug trace
    if debug_tracing_enabled:
        "Setting instantiation_required flag to TRUE/FALSE for (options=N)"
        print_symbol(inst->master_symbol)

    // 4. Ensure inst_info exists
    if inst_info is NULL:
        find_or_create_master_instance(inst)    // sub_753550

    // 5. If setting == true and entity != actual_decl:
    //    — Set inst->flags |= 0x01 (instantiation_required)
    //    — If inst_info exists, increment pending_count
    //    — Set referencing_namespace
    //    — Possibly call should_be_instantiated + instantiate immediately
    //    — Add to worklist via add_to_instantiations_required_list

    // 6. If setting == false and options & 0x02:
    //    — Decrement pending_count
    //    — Clear inst->flags bit 0
    //    — If count < 0: internal error (templates.c:38908)

    // 7. Worklist linkage (add_to_instantiations_required_list tail)
    //    — Check on_worklist bit
    //    — Append to qword_12C7740/qword_12C7738
    //    — Or set fixpoint flag if already on list
```

## Global State Reference

| Address | Name | Type | Description |
|---|---|---|---|
| `qword_12C7740` | `pending_instantiation_list` | `void*` | Head of function/variable worklist |
| `qword_12C7738` | `pending_instantiation_list_tail` | `void*` | Tail of function/variable worklist |
| `qword_12C7758` | `pending_class_list` | `void*` | Head of class template worklist |
| `qword_12C7750` | `deferred_master_info_list` | `void*` | Head of deferred master-info list |
| `qword_12C7748` | `deferred_master_info_list_tail` | `void*` | Tail of deferred master-info list |
| `dword_12C7730` | `instantiation_mode` | `int32` | 0=none, 1=used, 2=all, 3=local |
| `dword_12C771C` | `new_instantiations_needed` | `int32` | Fixpoint flag (1 = restart loop) |
| `dword_12C7718` | `additional_pass_needed` | `int32` | Secondary fixpoint flag |
| `qword_12C76E0` | `function_depth_counter` | `int64` | Current function instantiation depth (max 255) |
| `qword_106BD10` | `max_depth_limit` | `int64` | Configurable depth limit (read by both function and class paths) |
| `qword_12C74F0` | `instance_alloc_count` | `int64` | Total 128-byte records allocated |
| `qword_12C74E8` | `master_info_alloc_count` | `int64` | Total 32-byte master-info records allocated |
| `qword_12C7708` | `inline_entity_list_head` | `void*` | Head of inline entity fixup list |
| `qword_12C7700` | `inline_entity_list_tail` | `void*` | Tail of inline entity fixup list |

## Diagnostic Messages

| Number | Severity | Condition | Message Summary |
|---|---|---|---|
| 456 | Error | Depth counter >= max limit | Excessive template instantiation depth |
| 489 | Warning | Approaching depth limit (explicit instantiation) | Template instantiation depth nearing limit |
| 490 | Warning | Approaching depth limit (auto instantiation) | Template instantiation depth nearing limit |
| 599 | Warning | Toggle counter reaches 200 | Instantiation flag oscillation detected |
| 759 | Error | Entity not visible at file scope | Template entity not accessible for instantiation |

## Function Map

| Address | Identity | Confidence | Lines | Role |
|---|---|---|---|---|
| `sub_7416E0` | `alloc_template_instance` | 95% | 40 | Allocate 128-byte instance record |
| `sub_7416A0` | `alloc_master_instance_info` | 95% | 16 | Allocate 32-byte master info record |
| `sub_753550` | `find_or_create_master_instance` | 95% | 75 | Link instance to shared master info |
| `sub_7770E0` | `update_instantiation_required_flag` | 95% | 434 | Update flags, add to worklist |
| `sub_78A7F0` | `do_any_needed_instantiations` | 100% | 72 | Walk function/variable worklist |
| `sub_78A9D0` | `template_and_inline_entity_wrapup` | 100% | 136 | Fixpoint loop entry point |
| `sub_774620` | `should_be_instantiated` | 95% | 326 | Decision gate |
| `sub_775E00` | `instantiate_template_function_full` | 95% | 839 | Function template instantiation |
| `sub_777CE0` | `f_instantiate_template_class` | 95% | 516 | Class template instantiation |
| `sub_774C30` | `instantiate_template_variable` | 95% | 751 | Variable template instantiation |
| `sub_75D740` | `increment_pending_instantiations` | 95% | — | Increment per-type depth counter |
| `sub_75D7C0` | `decrement_pending_instantiations` | 95% | — | Decrement per-type depth counter |
| `sub_75D6A0` | `too_many_pending_instantiations` | 95% | — | Check depth limit, emit diagnostic 456 |
| `sub_75D5B0` | `determine_referencing_namespace` | 95% | 47 | Find namespace that triggered instantiation |
| `sub_7574B0` | `f_entity_can_be_instantiated` | 95% | — | Pre-check: body available, constraints satisfied |
| `sub_756B40` | `f_is_static_or_inline_template_entity` | 95% | — | Check linkage for instantiation eligibility |
| `sub_789EF0` | `update_instantiation_flags` | 95% | — | Update class instantiation flags, add to class worklist |
| `sub_72ED70` | `alloc_symbol_list_entry` | 95% | 39 | Allocate 16-byte symbol list node (for inline entity list) |

## Cross-References

- [Template Engine](../edg/template-engine.md) — the full instantiation pipeline, substitution engine, argument deduction, partial ordering
- [CUDA Template Restrictions](../edg/template-cuda.md) — CUDA-specific template argument accessibility checks
- [Scope Entry](scope-entry.md) — 784-byte scope stack entry, template instantiation depth counters at `+576`/`+580`/`+584`
- [Entity Node Layout](entity-node.md) — entity kind byte at `+80`, execution space at `+182`
- [Translation Unit Descriptor](translation-unit.md) — TU linked list at `qword_106B9F0`, per-TU `needs_recheck` flag at `+393`
