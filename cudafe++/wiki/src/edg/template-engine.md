# Template Engine

The template engine in cudafe++ is EDG 6.6's implementation of C++ template instantiation, argument deduction, partial specialization ordering, and the worklist-driven fixpoint loop that produces all needed template instantiations at translation-unit end. It lives primarily in `templates.c` (160+ functions at `0x7530C0`--`0x794D30`) with supporting cross-TU correspondence logic in `trans_corresp.c` (`0x796E60`--`0x79F9E0`).

Template instantiation in a C++ compiler is fundamentally a deferred operation: the compiler parses template definitions, records their bodies in a declaration cache, and only instantiates when a concrete use forces it. EDG implements this with two pending worklists — one for class templates, one for function/variable templates — that accumulate entries during parsing and are drained by a fixpoint loop at the end of each translation unit. This page documents the complete instantiation pipeline from "entity added to worklist" through "instantiated body emitted into IL."

## Key Facts

| Property | Value |
|---|---|
| Source file | `templates.c` (172 functions), `trans_corresp.c` (36 functions) |
| Address range | `0x7530C0`--`0x794D30` (templates), `0x796E60`--`0x79F9E0` (correspondence) |
| Fixpoint entry point | `sub_78A9D0` (`template_and_inline_entity_wrapup`), 136 lines |
| Worklist walker | `sub_78A7F0` (`do_any_needed_instantiations`), 72 lines |
| Should-instantiate gate | `sub_774620` (`should_be_instantiated`), 326 lines |
| Function instantiation | `sub_775E00` (`instantiate_template_function_full`), 839 lines |
| Class instantiation | `sub_777CE0` (`f_instantiate_template_class`), 516 lines |
| Variable instantiation | `sub_774C30` (`instantiate_template_variable`), 751 lines |
| Pending function/variable list | `qword_12C7740` (linked list head) |
| Pending class list | `qword_12C7758` (linked list head) |
| Function depth limit | `qword_12C76E0` (max 255 = `0xFF`) |
| Class depth limit | Per-type counter at type entry `+56`, via `qword_106BD10` |
| Pending counter | `sub_75D740` (increment) / `sub_75D7C0` (decrement) |
| SSE state save | 4 xmmword registers for functions, 12 for classes |
| Instantiation modes | `"none"` / `"all"` / `"used"` / `"local"` |
| Fixpoint flag | `dword_12C771C` (set=1 when new work discovered, loop restarts) |

## Instantiation Entry Structure

Each pending instantiation is represented as a linked-list node. The function/variable worklist uses entries with the following layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| `+0` | 8 | `entity` | Primary symbol pointer |
| `+8` | 8 | `next` | Next entry in pending list |
| `+16` | 8 | `inst_info` | Instantiation info record (must be non-null) |
| `+24` | 8 | `master_instance` | Canonical template symbol |
| `+32` | 8 | `actual_decl` | Declaration in the instantiation context |
| `+40` | 8 | `cached_decl` | Cached declaration (for kind 7 / function-local) |
| `+64` | 8 | `body_flags` | Deferred/deleted function flags |
| `+72` | 8 | `pre_computed_result` | Result from prior instantiation attempt |
| `+80` | 1 | `flags` | Status bitfield (see below) |

### Flags Byte at `+80`

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | `instantiated` | Entity has been instantiated |
| 1 | `0x02` | `not_needed` | Entity was determined to not need instantiation |
| 3 | `0x08` | `explicit_instantiation` | From explicit `template` declaration |
| 4 | `0x10` | `suppress_auto` | Auto-instantiation suppressed (extern template) |
| 5 | `0x20` | `excluded` | Entity excluded from instantiation set |
| 7 | `0x80` | `can_be_instantiated_checked` | Pre-check already performed |

### Flags Byte at `+28` (on `inst_info` at `+16`)

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | `blocked` | Instantiation blocked (dependency cycle) |
| 3 | `0x08` | `debug_checked` | Already checked by debug tracing path |

## The Fixpoint Loop: template_and_inline_entity_wrapup

`sub_78A9D0` is the top-level entry point, called at the end of each translation unit from `fe_wrapup`. It implements a fixpoint loop that keeps running until no new instantiations are discovered.

```text
template_and_inline_entity_wrapup (sub_78A9D0)
  |
  +-- Assert: qword_106BA18 == 0  (not nested in another TU)
  +-- Check: dword_126EFB4 == 2   (full compilation mode)
  |
  +-- FOR EACH translation_unit IN qword_106B9F0 linked list:
  |     |
  |     +-- sub_7A3EF0: set up TU context (switch active TU)
  |     |
  |     +-- PHASE 1: Process pending class instantiations
  |     |   Walk qword_12C7758 list:
  |     |     For each class entry:
  |     |       if sub_7A6B60 (is_dependent_type) == false
  |     |          AND sub_7A8A30 (is_class_or_struct_type) == true:
  |     |            f_instantiate_template_class(entry)
  |     |
  |     +-- PHASE 2: Enable instantiation mode
  |     |   dword_12C7730 = 1
  |     |
  |     +-- PHASE 3: Process pending function/variable instantiations
  |     |   do_any_needed_instantiations()
  |     |
  |     +-- sub_7A3F70: tear down TU context
  |
  +-- PHASE 4: Check for newly-needed instantiations
  |   if dword_12C771C != 0:
  |     dword_12C771C = 0
  |     LOOP BACK to top          <<<< FIXPOINT
  |
  +-- Check dword_12C7718 for additional pass
```

The fixpoint is necessary because instantiating one template may trigger references to other uninstantiated templates. For example, instantiating `std::vector<Foo>` may require instantiating `std::allocator<Foo>`, `Foo`'s copy constructor, comparison operators, and so on. The loop re-runs until `dword_12C771C` (the "new instantiations needed" flag) remains zero through an entire pass.

### Class-Before-Function Ordering

Classes are instantiated first (Phase 1) because function template instantiations may depend on complete class types. A function template body that accesses `T::value_type` requires `T` to be fully instantiated before the function body can be parsed. The two-phase design avoids forward-reference failures during function body replay.

## Worklist Walker: do_any_needed_instantiations

`sub_78A7F0` walks the pending function/variable instantiation list and processes each entry that passes the `should_be_instantiated` gate.

```c
void do_any_needed_instantiations(void) {
    entry_t *v0 = qword_12C7740;          // pending list head
    while (v0) {
        if (v0->flags & 0x02) {            // already done
            v0 = v0->next;
            continue;
        }
        inst_info_t *v2 = v0->inst_info;   // offset +16, must be non-null
        if (!(v2->flags & 0x08)) {         // not debug-checked
            if (dword_126EFC8)             // debug tracing enabled
                sub_756B40(v0);            // f_is_static_or_inline check
        }
        if (v2->flags & 0x01) {            // blocked
            v0 = v0->next;
            continue;
        }
        if (v0->flags >= 0) {             // bit 7 not set (not pre-checked)
            sub_7574B0(v0);               // f_entity_can_be_instantiated
        }
        if (should_be_instantiated(v0, 1)) {
            instantiate_template_function_full(v0, 1);
        }
        v0 = v0->next;                    // offset +8
    }
}
```

The walk is a simple linear traversal. New entries appended during instantiation will be visited on the current pass if they appear after the current position, or on the next fixpoint iteration otherwise.

Debug tracing output: when `dword_126EFC8` is nonzero, the walker emits `"do_any_needed_instantiations, checking: "` followed by the entity name for each entry it considers.

## Decision Gate: should_be_instantiated

`sub_774620` is the critical decision function that determines whether a pending template entity actually requires instantiation. It implements a chain of rejection checks — an entity must pass all of them to be instantiated.

```c
int should_be_instantiated(entry_t *a1, int a2) {
    // 1. Already done?
    if (a1->flags_28 & 0x01)    return 0;

    // 2. Suppressed by extern template?
    if (a1->flags_80 & 0x20)    return 0;

    // 3. Already instantiated and not explicit?
    if ((a1->flags_80 & 0x08) && !(a1->flags_80 & 0x01))
        return 0;

    // 4. Has valid master instance?
    if (!a1->master_instance)   return 0;    // offset +24

    // 5. Entity kind filter (function-specific)
    int kind = get_entity_kind(a1->master_instance);
    switch (kind) {
        case 10: case 11:   // class member function
        case 17:            // lambda
        case 9:             // namespace-scope function
        case 7:             // variable template
            break;          // eligible
        default:
            return 0;       // not a function/variable entity
    }

    // 6. Implicit include needed?
    if (needs_implicit_include(a1))
        do_implicit_include_if_needed(a1);    // sub_754A70

    // 7. Depth limit check
    if (get_depth(a1) > *qword_106BD10)
        return 0;

    // 8. Depth warning (diagnostic 489/490)
    if (approaching_depth_limit(a1))
        emit_warning(489);  // or 490

    return 1;
}
```

The depth limit at `qword_106BD10` is the configurable maximum instantiation nesting depth. When exceeded, the entity is silently skipped. When approaching the limit, warnings 489 and 490 are emitted to alert the developer.

## Function Instantiation: instantiate_template_function_full

`sub_775E00` (839 lines) is the workhorse for instantiating function templates. It saves global parser state, replays the cached function body through the parser with substituted template arguments, and restores state afterward.

### SSE State Save/Restore

The function saves and restores 4 SSE registers (`xmmword_106C380`--`xmmword_106C3B0`) that hold critical parser/scope state. These 128-bit registers store packed parser context (scope indices, token positions, flags) that must be preserved across instantiation because the parser is stateful and re-entrant:

```text
Save on entry:
    saved_state[0] = xmmword_106C380    // parser scope context
    saved_state[1] = xmmword_106C390    // token stream state
    saved_state[2] = xmmword_106C3A0    // scope nesting info
    saved_state[3] = xmmword_106C3B0    // auxiliary flags

Restore on exit (always, even on error):
    xmmword_106C380 = saved_state[0]
    xmmword_106C390 = saved_state[1]
    xmmword_106C3A0 = saved_state[2]
    xmmword_106C3B0 = saved_state[3]
```

The use of SSE registers for state save/restore is a compiler optimization — the generated code uses `movaps`/`movups` instructions to save 64 bytes of state in 4 instructions rather than 8 individual `mov` instructions. The data itself is ordinary integer/pointer fields packed into 128-bit quantities by the compiler's register allocator.

### Instantiation Flow

```text
instantiate_template_function_full (sub_775E00)
  |
  +-- Save 4 SSE registers (parser state)
  |
  +-- Check pre-existing result: a1[9] (offset +72)
  |   If result exists:
  |     Load associated translation unit
  |     GOTO restore
  |
  +-- Fresh instantiation:
  |   |
  |   +-- Check implicit include needed
  |   +-- Resolve actual declaration via find_corresponding_instance
  |   +-- For class members (kind 20): handle member function templates
  |   |
  |   +-- Depth limit check:
  |   |   if qword_12C76E0 >= 0xFF (255):
  |   |     emit error, GOTO restore
  |   |   qword_12C76E0++
  |   |
  |   +-- Constraint satisfaction check:
  |   |   sub_7C2370 / sub_7C23B0 (C++20 requires-clause)
  |   |
  |   +-- Handle deferred/deleted functions (offset +64 flags)
  |   |
  |   +-- Set up substitution context: sub_709DE0
  |   |   Binds template parameters to concrete arguments
  |   |
  |   +-- Replay cached function body: sub_5A88B0
  |   |   Re-parses the saved token stream with substituted types
  |   |
  |   +-- Emit into IL: sub_676860
  |   |   Processes tokens until end marker (token kind 9)
  |   |
  |   +-- Update canonical entry: sub_79F1D0
  |   |   Links instantiation to cross-TU correspondence table
  |   |
  |   +-- qword_12C76E0--  (decrement depth)
  |
  +-- Restore 4 SSE registers
```

### Depth Counter: qword_12C76E0

This global counter tracks the current nesting depth of function template instantiations. The hard limit is 255 (`0xFF`). Each call to `instantiate_template_function_full` increments it on entry and decrements on exit. When the counter reaches 255, the function emits a fatal error and aborts instantiation.

The 255 limit is a safety valve against infinite recursive template instantiation (e.g., `template<int N> struct S { S<N+1> member; }`). The C++ standard mandates that implementations support at least 1,024 recursively nested template instantiations ([Annex B]), but EDG defaults to 255. This may be configurable via a CLI flag that sets `qword_106BD10`.

## Class Instantiation: f_instantiate_template_class

`sub_777CE0` (516 lines) instantiates class templates. It is structurally similar to the function instantiation path but saves significantly more state (12 SSE registers vs. 4) because class instantiation involves deeper parser state perturbation — class bodies contain member declarations, nested types, and member function definitions.

### SSE State Save/Restore (12 Registers)

```text
Save on entry:
    saved[0]  = xmmword_106C380
    saved[1]  = xmmword_106C390
    saved[2]  = xmmword_106C3A0
    saved[3]  = xmmword_106C3B0
    saved[4]  = xmmword_106C3C0
    saved[5]  = xmmword_106C3D0
    saved[6]  = xmmword_106C3E0
    saved[7]  = xmmword_106C3F0
    saved[8]  = xmmword_106C400
    saved[9]  = xmmword_106C410
    saved[10] = xmmword_106C420
    saved[11] = xmmword_106C430

Restore on exit:
    (reverse order, same 12 registers)
```

The additional 8 registers (beyond the 4 used by function instantiation) capture the extended scope stack state, class body parsing context, base class list, member template processing state, and access specifier tracking that class body parsing requires.

### Class Type Entry Layout

Class instantiation operates on a type entry with the following relevant fields:

| Offset | Size | Field | Description |
|---|---|---|---|
| `+56` | 8 | `instantiation_depth_counter` | Per-type depth limit via `qword_106BD10` |
| `+72` | 8 | `containing_template_decl` | The template declaration this specialization came from |
| `+88` | 8 | `scope_name_info` | Scope and name resolution data |
| `+96` | 8 | `class_body_info` | Pointer to cached class body tokens |
| `+104` | 8 | `base_class_list` | Linked list of base class entries |
| `+120` | 8 | `namespace_lookup_info` | Namespace and extern template info |
| `+132` | 1 | `kind` | Type kind: 9=struct, 10=class, 11=union, 12=alias |
| `+144` | 8 | `canonical_type` | Pointer to canonical type entry (follow kind==12 chain) |
| `+152` | 8 | `parent_scope` | Enclosing scope entry |
| `+160` | 4 | `attribute_flags` | Attribute bits |
| `+176` | 1 | `template_flags` | bit 0 = primary template, bit 7 = inline |
| `+192` | 8 | `template_argument_list` | Substituted template argument list |
| `+200` | 8 | `member_template_list` | Linked list of member templates |
| `+296` | 8 | `associated_constraint` | C++20 constraint expression |
| `+298` | 1 | `extra_flags` | Additional status bits |

### Instantiation Flow

```text
f_instantiate_template_class (sub_777CE0)
  |
  +-- Walk to canonical type entry: follow kind==12 chain at +144
  +-- Get class symbol: sub_72F640
  |
  +-- Check extern template constraints: sub_7C2370/sub_7C23B0
  |
  +-- Save 12 SSE registers
  |
  +-- Depth limit check:
  |   if type_entry[+56] >= *qword_106BD10:
  |     emit error, GOTO restore
  |   type_entry[+56]++
  |
  +-- Set up substitution context: sub_709DE0
  |
  +-- Handle base class list:
  |   sub_415BE0 (parse base-specifier-list)
  |   sub_4A5510 (validate base classes)
  |
  +-- Parse class body from declaration cache
  |   Replay saved tokens with substituted types
  |
  +-- Process member templates:
  |   Loop on member_template_list (offset +200)
  |   sub_7856E0 for each member template
  |
  +-- Perform deferred access checks:
  |   sub_744F60 (perform_deferred_access_checks_at_depth)
  |
  +-- type_entry[+56]--  (decrement depth)
  |
  +-- Restore 12 SSE registers
```

### Per-Type Depth Limit

Unlike function instantiation (which uses a single global counter `qword_12C76E0` with a hard limit of 255), class instantiation uses a per-type counter stored at offset `+56` of the type entry. The limit is still read from `qword_106BD10`. This per-type design prevents one deeply-nested class hierarchy from consuming the entire depth budget — each class type tracks its own instantiation nesting independently.

## Variable Instantiation: instantiate_template_variable

`sub_774C30` (751 lines) handles variable template instantiation. Variable templates (C++14) are less common than function or class templates but follow the same pattern: extract master instance, set up substitution, replay cached declaration.

### Instantiation Flow

```text
instantiate_template_variable (sub_774C30)
  |
  +-- Extract master instance: a1[3]=symbol, a1[4]=decl
  |
  +-- Look up declaration type:
  |   Switch on kind: 4/5, 6, 9/10, 19-22
  |
  +-- Find declaration cache: offset +216 or +264
  |
  +-- Depth limit check: qword_106BD10
  |
  +-- Set up substitution context: sub_709DE0
  |
  +-- Create declaration state:
  |   memset(v77, 0, 0x1D8)    // 472 bytes = declaration state
  |   v77[0]  = symbol
  |   v77[3]  = source position
  |   v77[6]  = type
  |   v77[15] = flags
  |   v77[19] = self-pointer
  |   v77[33] = additional flags
  |   v77[35] = initializer
  |   v77[36] = IL tree
  |
  +-- Perform type substitution: sub_764AE0 (scan_template_declaration)
  |
  +-- Handle constexpr/constinit evaluation
  |
  +-- Handle deferred access checks
  |
  +-- Update canonical entry
  |
  +-- For kind==7 (function-local variable templates):
      Special handling via sub_5C9600, copy attributes from prototype
```

The declaration state structure is 472 bytes (`0x1D8`), stack-allocated and zero-initialized. This is the same structure used by the main declaration parser — variable template instantiation reuses the declaration parsing infrastructure with pre-populated fields.

## Pending Counter Management

Two small functions manage a pending-instantiation counter that tracks how many instantiations are in flight. This counter is used for progress reporting and infinite-loop detection.

### increment_pending_instantiations (sub_75D740)

Called when a new template entity is added to the pending worklist. Increments the counter and checks against a maximum threshold via `too_many_pending_instantiations` (`sub_75D6A0`).

### decrement_pending_instantiations (sub_75D7C0)

Called when an instantiation completes (successfully or by rejection). Decrements the counter.

The counter itself is not directly visible in the sweep report but is inferred from the call pattern: the increment function is called from code paths that add entries to `qword_12C7740` or `qword_12C7758`, and the decrement is called at the end of each `instantiate_template_function_full` / `f_instantiate_template_class` / `instantiate_template_variable` invocation.

## Instantiation Modes

The template engine supports four instantiation modes, controlled by CLI flags that set `dword_12C7730` and related configuration globals:

| Mode | `dword_12C7730` | Behavior |
|---|---|---|
| `"none"` | 0 | No automatic instantiation. Only explicit `template` declarations trigger instantiation. Used for precompiled headers. |
| `"used"` | 1 | Instantiate templates that are actually used (ODR-referenced). This is the default mode. The `should_be_instantiated` function checks usage flags. |
| `"all"` | 2 | Instantiate all templates that have been declared, whether or not they are used. Used for template library precompilation. |
| `"local"` | 3 | Instantiate only templates with internal linkage. Extern templates are skipped. Used for split compilation models. |

The mode transitions during compilation:
1. During parsing: `dword_12C7730 = 0` (collection only, no instantiation)
2. At wrapup entry: `dword_12C7730 = 1` (enable "used" mode)
3. During fixpoint: mode may escalate to "all" if `dword_12C7718` is set

The precompile mode (`dword_106C094 == 3`) skips the fixpoint loop entirely and records template entities for later instantiation in the consuming translation unit.

## Substitution Engine: copy_type_with_substitution

`sub_76D860` (1,229 lines) is the core type substitution function. It takes a type node and a set of template-parameter-to-argument bindings, and produces a new type with all template parameters replaced by their concrete values.

```text
copy_type_with_substitution(type, bindings) -> type
  |
  +-- Dispatch on type->kind:
  |
  +-- Simple types (int, float, void): return type unchanged
  |
  +-- Pointer type (kind 6):
  |   new_pointee = copy_type_with_substitution(type->pointee, bindings)
  |   return make_pointer_type(new_pointee)
  |
  +-- Reference types (kind 7, 19):
  |   new_referent = copy_type_with_substitution(type->referent, bindings)
  |   return make_reference_type(new_referent, type->is_rvalue)
  |
  +-- Array type (kind 8):
  |   new_element = copy_type_with_substitution(type->element, bindings)
  |   new_size = substitute_expression(type->size_expr, bindings)
  |   return make_array_type(new_element, new_size)
  |
  +-- Function type (kind 14):
  |   new_return = copy_type_with_substitution(type->return_type, bindings)
  |   new_params = [substitute each parameter type]
  |   return make_function_type(new_return, new_params, type->cv_quals)
  |
  +-- Template parameter type:
  |   Look up parameter in bindings
  |   return concrete argument type
  |
  +-- Template-id type:
  |   new_args = copy_template_arg_list_with_substitution(type->args, bindings)
  |   return find_or_instantiate_template_class(type->template, new_args)
  |
  +-- Pack expansion (kind 16, 17):
  |   Expand pack with all elements from the binding
  |   return list of substituted types
```

Supporting substitution functions:

| Address | Identity | Description |
|---|---|---|
| `sub_77BA10` | `copy_parent_type_with_substitution` | Substitutes in enclosing class context |
| `sub_77BFE0` | `copy_template_with_substitution` | Substitutes within template declarations |
| `sub_77FDE0` | `copy_template_arg_list_with_substitution` | Substitutes within argument lists (612 lines) |
| `sub_780B80` | `copy_template_class_reference_with_substitution` | Handles class template references |
| `sub_78B600` | `copy_template_variable_with_substitution` | Handles variable template references |
| `sub_793DF0` | `substitute_template_param_list` | Walks parameter list with substitution (741 lines) |

## Template Argument Deduction

The deduction subsystem determines template argument values from function call arguments. Key functions:

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_77CEE0` | `matches_template_type` | 788 | Core deduction: matches actual type against template parameter pattern. Implements [temp.deduct]. |
| `sub_77CA90` | `matches_template_type_for_class_type` | — | Class-specific variant with additional base class traversal |
| `sub_77C720` | `matches_template_arg_list` | — | Matches a sequence of template arguments |
| `sub_77C510` | `matches_template_template_param` | — | Matches template template parameters |
| `sub_77C240` | `template_template_arg_matches_param` | — | Template template argument compatibility check |
| `sub_77E9F0` | `matches_template_constant` | — | Matches non-type template arguments (constant expressions) |
| `sub_77E310` | `parameter_is_more_specialized` | 330 | Partial ordering rule: determines which parameter is more specialized |
| `sub_780FC0` | `all_templ_params_have_values` | 332 | Post-deduction check: verifies all parameters received values |
| `sub_781660` | `wrapup_template_argument_deduction` | — | Finalizes deduction, applies default arguments |
| `sub_781C40` | `matches_partial_specialization` | 316 | Tests actual arguments against a partial specialization |

## Partial Specialization Ordering

When multiple partial specializations match, the engine must select the "most specialized" one. This implements C++ [temp.class.order] and [temp.func.order]:

```text
check_partial_specializations (sub_774470)
  |
  +-- For each partial specialization of the template:
  |   matches_partial_specialization(actual_args, partial_spec)
  |   If matches: add to candidates list
  |     add_to_partial_order_candidates_list (sub_773E40)
  |
  +-- If multiple candidates:
  |   partial_ord (sub_75D2A0)
  |     Pairwise comparison using parameter_is_more_specialized
  |     Select most specialized, or emit ambiguity error
  |
  +-- Return winning specialization (or primary template if no match)
```

For function templates, ordering uses `compare_function_templates` (`sub_7730D0`, 665 lines) which implements the more complex function template partial ordering rules.

## Template Declaration Infrastructure

The declaration side handles parsing `template<...>` prefixes and setting up template entities:

| Address | Identity | Lines | Description |
|---|---|---|---|
| `sub_786260` | `template_declaration` | 2,487 | Main entry point for all template declarations. Handles primary, explicit specialization, partial specialization, and friend templates. |
| `sub_782690` | `class_template_declaration` | 2,280 | Class-specific template declaration processing |
| `sub_78D600` | `template_or_specialization_declaration_full` | 2,034 | Unified handler routing to class, function, or variable paths |
| `sub_764AE0` | `scan_template_declaration` | 412 | Parses the `template<...>` prefix |
| `sub_779D80` | `scan_template_param_list` | 626 | Parses template parameter lists |
| `sub_77AAB0` | `scan_lambda_template_param_list` | — | C++20 lambda template parameter parsing |
| `sub_770790` | `make_template_function` | 914 | Creates function template entity |
| `sub_753870` | `make_template_variable` | — | Creates variable template entity |
| `sub_756310` | `set_up_template_decl` | — | Template declaration state initialization |

## Explicit Instantiation

Explicit instantiation (`template class Foo<int>;` or `template void f<int>();`) is handled by a dedicated path:

```text
explicit_instantiation (sub_791C70, 105 lines)
  |
  +-- Parse 'extern' flag: a2 & 1 = is_extern_instantiation
  +-- Save compilation mode (dword_106C094)
  |
  +-- Determine instantiation kind:
  |   extern:              kind = 16
  |   non-extern, no inline: kind = 15
  |   non-extern, inline:   kind = 18
  |
  +-- For precompiled header mode: mark scope entry
  |
  +-- instantiation_directive (sub_7908E0, 626 lines):
  |   |
  |   +-- Initialize target scope entry (memset 472 bytes)
  |   +-- Check CUDA device-code instantiation pragmas
  |   +-- Parse declaration:
  |   |   For classes:    sub_789EF0 (update_instantiation_flags)
  |   |   For functions:  sub_78D0E0 (find_matching_template_instance)
  |   |                   then sub_7897C0 (update_instantiation_flags)
  |   |   For variables:  similar path
  |   +-- Handle instantiation attributes (dllexport/visibility)
  |   +-- Clean up parser state
  |
  +-- Handle deferred access checks: sub_744F60
  +-- Restore compilation mode
```

`update_instantiation_flags` (`sub_7897C0`, 351 lines) sets the appropriate instantiation-required bits on the template entity after matching an explicit instantiation directive. It checks compilation mode, CUDA device/host targeting, and adjusts flags accordingly.

## CUDA Integration Points

The template engine interacts with CUDA through several mechanisms:

1. **Device/host filtering in `should_be_instantiated`**: The function checks CUDA execution space attributes via `sub_756840` (`sym_can_be_instantiated`) to determine if a template entity should be instantiated for the current compilation target (device or host).

2. **Instantiation directives**: CUDA-specific `#pragma` directives can trigger or suppress template instantiation for device code. The `instantiation_directive` function checks for these at `dword_126EFA8` (GPU mode) and `dword_126EFA4` (device-code flag).

3. **Namespace injection**: CUDA-specific symbols are entered into `cuda::std` via `enter_symbol_for_namespace_cuda_std` (`sub_749330`) and `std::meta` via `enter_symbol_for_namespace_std_meta` (`sub_7493C0`, C++26 reflection support).

4. **Target dialect selection**: `select_cp_gen_be_target_dialect` (`sub_752A80`) determines whether template instantiations emit device PTX code or host code, based on `dword_126EFA8` (GPU mode) and `dword_126EFA4` (device vs. host).

## Cross-TU Correspondence

When compiling with RDC mode or multiple translation units, the same template may be instantiated in different TUs. The `trans_corresp.c` file (`0x796E60`--`0x79F9E0`) handles deduplication and canonical entry selection:

| Address | Identity | Description |
|---|---|---|
| `sub_796E60` | `canonical_ranking` | Determines which of two TU entries is canonical |
| `sub_7975D0` | `may_have_correspondence` | Checks if cross-TU correspondence is possible |
| `sub_7999C0` | `find_template_correspondence` | Finds corresponding template across TUs (601 lines) |
| `sub_79A5A0` | `determine_correspondence` | Establishes correspondence relationship |
| `sub_79B8D0` | `mark_canonical_instantiation` | Marks the canonical version of an instantiation |
| `sub_79C400` | `f_set_trans_unit_corresp` | Sets up cross-TU correspondence (511 lines) |
| `sub_79D080` | `establish_instantiation_correspondences` | Links instantiation results across TUs |
| `sub_79EE80`--`sub_79F1D0` | `update_canonical_entry` (3 variants) | Updates canonical representative after instantiation |
| `sub_79F9E0` | `record_instantiation` | Records an instantiation for cross-TU tracking |

The correspondence system ensures that when `std::vector<int>` is instantiated in TU1 and TU2, both produce structurally equivalent IL, and only one canonical version is emitted to the output.

## Global State

| Address | Name | Description |
|---|---|---|
| `qword_12C7740` | `pending_instantiation_list` | Head of pending function/variable instantiation linked list |
| `qword_12C7758` | `pending_class_instantiation_list` | Head of pending class instantiation linked list |
| `dword_12C7730` | `instantiation_mode_active` | Current instantiation mode (0=none, 1=used, 2=all, 3=local) |
| `dword_12C771C` | `new_instantiations_needed` | Fixpoint flag: set to 1 when new work discovered |
| `dword_12C7718` | `additional_pass_needed` | Secondary fixpoint flag for extra passes |
| `qword_12C76E0` | `instantiation_depth_counter` | Current function template nesting depth (max 0xFF) |
| `qword_106BD10` | `max_instantiation_depth_limit` | Configurable depth limit (read by class and function paths) |
| `xmmword_106C380`--`106C3B0` | `parser_state_save_area` | 4 SSE registers saved by function instantiation |
| `xmmword_106C380`--`106C430` | `parser_state_save_area_full` | 12 SSE registers saved by class instantiation |
| `dword_106C094` | `compilation_mode` | 0=none, 1=normal, 3=precompile |
| `dword_126EFB4` | `compilation_phase` | 2=full compilation (required for fixpoint loop) |
| `qword_106B9F0` | `translation_unit_list_head` | Linked list of TUs for per-TU fixpoint iteration |
| `qword_106BA18` | `tu_stack_top` | Must be 0 (not nested) when fixpoint starts |
| `dword_126EFC8` | `debug_tracing_enabled` | Nonzero enables trace output for instantiation |
| `dword_126EFA8` | `gpu_mode` | Nonzero when compiling CUDA code |
| `dword_126EFA4` | `device_code` | 1=device-side compilation, 0=host stubs |
| `word_126DD58` | `current_token_kind` | Parser state: current token (9=END) |
| `qword_126DD38` | `source_position` | Parser state: current source location |
| `qword_126C5E8` | `scope_table_base` | Array of 784-byte scope entries |
| `dword_126C5E4` | `current_scope_index` | Index into scope table |

## Diagnostic Strings

| String | Source | Condition |
|---|---|---|
| `"do_any_needed_instantiations, checking: "` | `sub_78A7F0` | `dword_126EFC8 != 0` (debug tracing) |
| `"template_and_inline_entity_wrapup"` | `sub_78A9D0` | Assert string |
| `"should_be_instantiated"` | `sub_774620` | Assert string at `templates.c:36894` |
| `"instantiate_template_function_full"` | `sub_775E00` | Assert string at `templates.c:7359` |
| `"f_instantiate_template_class"` | `sub_777CE0` | Assert string at `templates.c:5277` |
| `"instantiate_template_variable"` | `sub_774C30` | Assert string at `templates.c:7814` |
| `"check_template_nesting_depth"` | `sub_7533E0` | Assert string |
| `"instantiation_directive"` | `sub_7908E0` | Assert string at `templates.c:41682` |
| `"explicit_instantiation"` | `sub_791C70` | Assert string at `templates.c:42231` |
| `"template_arg_is_dependent"` | `sub_7530C0` | Assert string at `templates.c:8897` |

## Function Map

| Address | Identity | Confidence | Lines | EDG Source |
|---|---|---|---|---|
| `sub_78A9D0` | `template_and_inline_entity_wrapup` | 100% | 136 | `templates.c:40084` |
| `sub_78A7F0` | `do_any_needed_instantiations` | 100% | 72 | `templates.c:39760` |
| `sub_774620` | `should_be_instantiated` | 95% | 326 | `templates.c:36894` |
| `sub_775E00` | `instantiate_template_function_full` | 95% | 839 | `templates.c:7359` |
| `sub_777CE0` | `f_instantiate_template_class` | 95% | 516 | `templates.c:5277` |
| `sub_774C30` | `instantiate_template_variable` | 95% | 751 | `templates.c:7814` |
| `sub_75D740` | `increment_pending_instantiations` | 95% | — | `templates.c` |
| `sub_75D7C0` | `decrement_pending_instantiations` | 95% | — | `templates.c` |
| `sub_75D6A0` | `too_many_pending_instantiations` | 95% | — | `templates.c` |
| `sub_7574B0` | `f_entity_can_be_instantiated` | 95% | — | `templates.c:37066` |
| `sub_756B40` | `f_is_static_or_inline_template_entity` | 95% | — | `templates.c` |
| `sub_756840` | `sym_can_be_instantiated` | 95% | — | `templates.c` |
| `sub_754A70` | `do_implicit_include_if_needed` | 95% | — | `templates.c` |
| `sub_76D860` | `copy_type_with_substitution` | 95% | 1229 | `templates.c` |
| `sub_77FDE0` | `copy_template_arg_list_with_substitution` | 95% | 612 | `templates.c` |
| `sub_793DF0` | `substitute_template_param_list` | 95% | 741 | `templates.c` |
| `sub_77CEE0` | `matches_template_type` | 95% | 788 | `templates.c` |
| `sub_780FC0` | `all_templ_params_have_values` | 95% | 332 | `templates.c` |
| `sub_781C40` | `matches_partial_specialization` | 95% | 316 | `templates.c` |
| `sub_774470` | `check_partial_specializations` | 95% | 58 | `templates.c` |
| `sub_773E40` | `add_to_partial_order_candidates_list` | 95% | 306 | `templates.c` |
| `sub_75D2A0` | `partial_ord` | 95% | — | `templates.c` |
| `sub_7730D0` | `compare_function_templates` | 95% | 665 | `templates.c` |
| `sub_786260` | `template_declaration` | 95% | 2487 | `templates.c` |
| `sub_782690` | `class_template_declaration` | 95% | 2280 | `templates.c` |
| `sub_78D600` | `template_or_specialization_declaration_full` | 95% | 2034 | `templates.c` |
| `sub_764AE0` | `scan_template_declaration` | 95% | 412 | `templates.c` |
| `sub_779D80` | `scan_template_param_list` | 95% | 626 | `templates.c` |
| `sub_770790` | `make_template_function` | 95% | 914 | `templates.c` |
| `sub_771D50` | `find_template_function` | 95% | 470 | `templates.c` |
| `sub_7621A0` | `find_template_class` | 95% | 519 | `templates.c` |
| `sub_78AC50` | `find_template_variable` | 95% | 528 | `templates.c` |
| `sub_7908E0` | `instantiation_directive` | 95% | 626 | `templates.c:41682` |
| `sub_791C70` | `explicit_instantiation` | 95% | 105 | `templates.c:42231` |
| `sub_7897C0` | `update_instantiation_flags` | 90% | 351 | `templates.c` |
| `sub_7770E0` | `update_instantiation_required_flag` | 95% | 434 | `templates.c` |
| `sub_78D0E0` | `find_matching_template_instance` | 95% | — | `templates.c` |
| `sub_709DE0` | `set_up_substitution_context` | — | — | (likely `templates.c`) |
| `sub_744F60` | `perform_deferred_access_checks_at_depth` | 95% | — | `symbol_tbl.c` |
| `sub_7530C0` | `template_arg_is_dependent` | 95% | — | `templates.c:8897` |
| `sub_762C80` | `template_arg_list_is_dependent_full` | 95% | 839 | `templates.c` |
| `sub_75EF10` | `equiv_template_arg_lists` | 95% | 493 | `templates.c` |
| `sub_7931B0` | `make_template_implicit_deduction_guide` | 95% | 433 | `templates.c` |
| `sub_794D30` | `ctad` | 95% | 990 | `templates.c` |
| `sub_796E60` | `canonical_ranking` | 95% | — | `trans_corresp.c` |
| `sub_7999C0` | `find_template_correspondence` | 95% | 601 | `trans_corresp.c` |
| `sub_79C400` | `f_set_trans_unit_corresp` | 95% | 511 | `trans_corresp.c` |
| `sub_79F1D0` | `update_canonical_entry` | 95% | — | `trans_corresp.c` |
| `sub_79F9E0` | `record_instantiation` | 95% | — | `trans_corresp.c` |

## Cross-References

- [EDG 6.6 Overview](overview.md) — Architecture and NVIDIA modification layers
- [CUDA Template Restrictions](template-cuda.md) — CUDA-specific template constraints
- [Type System](type-system.md) — Type kinds and class layout referenced during substitution
- [Keep-in-IL](../il/keep-in-il.md) — Device code selection interacts with instantiation results
- [Pipeline Overview](../pipeline/overview.md) — Where template wrapup fits in the compilation pipeline
- [Template Instance Record](../structs/template-instance.md) — Data structure for instantiation entries
- [Scope Entry](../structs/scope-entry.md) — 784-byte scope structure used during instantiation
- [Diagnostics Overview](../diagnostics/overview.md) — Warning 489/490 for depth limits
