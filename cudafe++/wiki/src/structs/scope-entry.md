# Scope Entry

The scope entry is the 784-byte record that forms the elements of the scope stack, the central data structure in cudafe++ for tracking nested lexical scopes during C++ parsing and semantic analysis. The scope stack is a flat array at `qword_126C5E8`, indexed by `dword_126C5E4` (current depth). Every time the parser enters a new scope -- file, block, function body, class definition, template declaration, namespace -- a new 784-byte entry is pushed onto this stack. When the scope closes, the entry is popped and all associated cleanup runs: symbol table housekeeping, using-directive deactivation, name collision discriminator assignment, template parameter restoration, and memory region disposal.

This page documents the scope stack entry layout, the scope kind enum, the key flag bytes, the CUDA-specific additions (device/host scope context), the template instantiation depth counters, and the major push/pop functions.

## Key Facts

| Property | Value |
|---|---|
| Entry size | 784 bytes (constant, verified by "Stack entry size: %d\n" in debug statistics) |
| Stack base pointer | `qword_126C5E8` (global, array of 784-byte entries) |
| Current depth index | `dword_126C5E4` (global, 0-based index of topmost entry) |
| Function scope index | `dword_126C5D8` (-1 if not inside a function scope) |
| Class scope index | `dword_126C5C8` (-1 if not inside a class scope) |
| File scope index | `dword_126C5DC` |
| EDG source file | `scope_stk.c` (address range `0x6FE160`-`0x7106B0`, ~160 functions) |
| Push function | `sub_700560` (`push_scope_full`, 1476 lines, 13 parameters) |
| Pop function | `sub_7076A0` (`pop_scope`, 1142 lines) |
| Index arithmetic | `784 * index` for byte offset; reverse via multiply by `0x7D6343EB1A1F58D1` and shift right (division by 784 = 16 * 49) |

## Scope Stack Global Variables

| Global | Type | Meaning |
|---|---|---|
| `qword_126C5E8` | `void*` | Base pointer to the scope stack array |
| `dword_126C5E4` | `int32` | Current scope stack top index (0-based) |
| `dword_126C5D8` | `int32` | Current function scope index (-1 if none) |
| `dword_126C5DC` | `int32` | File scope index / secondary depth marker |
| `dword_126C5AC` | `int32` | Saved depth for template instantiation |
| `qword_126C5D0` | `void*` | Current routine descriptor pointer |
| `dword_126C5B8` | `int32` | `is_member_of_template` flag |
| `dword_126C5C8` | `int32` | Class scope index (-1 if none) |
| `dword_126C5C4` | `int32` | Nested class / lambda scope index (-1 if none) |
| `dword_126C5E0` | `int32` | Scope hash / identifier |
| `dword_126C5B4` | `int32` | Namespace scope index |
| `dword_126C5BC` | `int32` | Class scope depth counter |
| `qword_126C598` | `void*` | Pack expansion context pointer |

## Full Offset Map

The table documents every field observed in the 784-byte scope stack entry. These are **scope stack** entry fields, not IL scope node fields (the IL scope is a separate 288-byte structure pointed to from offset `+192`).

| Offset | Size | Field | Evidence |
|---|---|---|---|
| `+0` | 4 | `scope_number` | Unique identifier for this scope instance; checked in pop_scope assertions |
| `+4` | 1 | `scope_kind` | Scope kind enum byte (see table below) |
| `+5` | 1 | `scope_flags_1` | General flags |
| `+6` | 2 | `scope_flags_2` | Bit 0 = void return flag; **bit 1 = device scope context** (NVIDIA addition); bit 2 = inline namespace; in some contexts bit 1 = `is_extern`, bit 5 = `inline_namespace` |
| `+7` | 1 | `access_flags` | Bit 0 = in class context; bit 1 = has using-directives; bit 4 = lambda body |
| `+8` | 1 | `scope_flags_4` | Template/class/reactivation bits; bit 5 (`0x20`) = `is_template_scope` |
| `+9` | 1 | `scope_flags_5` | **Bit 0 = needs cleanup / scope pop control** -- when set, triggers `sub_67B4E0()` cleanup of template instantiation artifacts before popping |
| `+10` | 1 | `scope_flags_6` | Bit 0 = `in_template_context` |
| `+11` | 1 | sign bit | `in_template_dependent_context` |
| `+12` | 1 | `scope_flags_7` | Bit 0 = `in_template_arg_scan`; bit 2 = `suppress_diagnostics`; bit 4 = `has_concepts` / `void_return_warned` |
| `+13` | 1 | `scope_flags_8` | Bit 4 = `warned_no_return` |
| `+14` | 1 | `flags3` | Bit 2 = `in_device_code` (NVIDIA-specific, marks whether code in this scope is device code) |
| `+24` | 8 | `symbol_chain_or_hash_ptr` | Pointer to the name symbol chain or hash table for name lookup |
| `+32` | 8 | `hash_table_ptr` | Hash table pointer (when scope uses hashing for lookup) |
| `+32`-`+144` | 112 | Inline tail info | When `+24` is 0, this region contains inline tail pointers for entity lists: `+40` = variables tail, `+48` = types tail, `+56` = routines next, `+88` = asm tail, `+112` = namespaces tail, `+144` = templates tail |
| `+192` | 8 | `il_scope_ptr` | Pointer to the associated 288-byte IL scope node (the persistent representation that survives scope pop) |
| `+200` | 8 | `local_static_init_list` | List of local static variable initializers |
| `+208` | 8 | `vla_dimensions_list` / `scope_depth` | VLA dimension tracking (C mode); scope depth integer |
| `+216` | 8 | `class_type_ptr` / `tu_ptr` | For class scopes: pointer to the class type symbol. For template instantiation scopes: pointer to the translation unit descriptor |
| `+224` | 8 | `routine_descriptor` | Pointer to the current routine descriptor (set for function scopes) |
| `+232` | 8 | `namespace_entity` | For namespace scopes: pointer to the namespace entity |
| `+240` | 4 | `region_number` | Memory region number (-1 = invalid sentinel, set by `alloc_scope`) |
| `+256` | 4 | `parent_scope_index` | Index of the enclosing scope in the stack (reported at both +240 and +256 in different sweeps -- likely `+240` = region, `+256` = parent) |
| `+272` | 8 | `name_hiding_list` | Linked list of names hidden by declarations in this scope |
| `+296` | 8 | `local_vars_tail` | Tail pointer for the local variables list |
| `+368` | 8 | `source_begin` | Source position at scope entry |
| `+376` | 8 | `associated_entity` / `parent_template_info` | Associated entity pointer / template information pointer |
| `+384` | 8 | `template_argument_list` | Template argument list for instantiation scopes |
| `+408` | 4 | `try_block_index` / `enclosing_class_scope_index` | Try block index (-1 = none); in class contexts, index to enclosing class scope |
| `+416` | 8 | `module_info` | Module information pointer (C++20 modules support) |
| `+424` | 4 | `line_number` | Line number at scope open (for diagnostics) |
| `+496` | 8 | `root_object_lifetime` | Root of the object lifetime tree for this scope |
| `+512` | 8 | `freed_lifetime_list` | List of freed object lifetimes awaiting reuse |
| `+560` | 4 | `enclosing_scope_index` | Parent scope index for pop validation |
| `+576` | 4 | `template_instantiation_depth_counter` | **Nested instantiation depth counter** -- incremented on recursive template instantiation push, decremented on pop; when > 0, pop just decrements without actually popping the scope stack |
| `+580` | 4 | `orig_depth` | Original scope stack depth at time of template instantiation push; validated during pop |
| `+584` | 4 | `saved_scope_depth` | Saved scope depth; restored via `dword_126C5AC` on template instantiation pop |
| `+608` | 8 | `class_def_info_ptr` | Class definition information pointer |
| `+624` | 8 | `template_info_ptr` | Template information record pointer |
| `+632` | 8 | `template_parameter_list` / `class_info_ptr` | Template parameter list pointer |
| `+704` | 8 | `lambda_counter` | Lambda expression counter within this scope (int64) |
| `+720` | 4 | `fixup_counter` | Deferred fixup counter |
| `+728` | 8 | `has_been_completed` | Completion flag (int64 used as bool) |
| `+736` | 8 | `deferred_fixup_list_head` | Head of deferred fixup linked list |
| `+744` | 8 | `deferred_fixup_list_tail` | Tail of deferred fixup linked list |

## Scope Stack Kind Enum

The scope stack kind byte at `+4` uses a different, larger enum than the IL scope kind (`sck_*`) at IL scope node `+28`. The scope stack enum includes additional entries for reactivation states, template instantiation context, and module scopes. The mapping is derived from `scope_kind_to_string` (`sub_7000E0`, 77 lines) which contains display string literals for each enum value, and from `display_scope` (`sub_5F2140`) in `il_to_str.c`.

### Scope Stack Kind Values

| Value | Name | Display String | Notes |
|---|---|---|---|
| 0 | `ssk_source_file` | `"source file"` | Top-level file scope. Maps to IL `sck_file` (0). |
| 1 | `ssk_func_prototype` | `"function prototype"` | Function prototype scope (parameter names). Maps to IL `sck_func_prototype` (1). |
| 2 | `ssk_block` | `"block"` | Block scope (compound statement). Maps to IL `sck_block` (2). |
| 3 | `ssk_alloc_namespace` | `"alloc_namespace"` | Namespace scope (first opening). Maps to IL `sck_namespace` (3). |
| 4 | `ssk_namespace_extension` | `"namespace extension"` | Namespace extension (reopened `namespace N { ... }`). |
| 5 | `ssk_namespace_reactivation` | `"namespace reactivation"` | Namespace scope reactivated for out-of-line definition. |
| 6 | `ssk_class_struct_union` | `"class/struct/union"` | Class/struct/union scope. Maps to IL `sck_class_struct_union` (6). |
| 7 | `ssk_class_reactivation` | `"class reactivation"` | Class scope reactivated for out-of-line member definition (e.g., `void MyClass::foo() { ... }`). |
| 8 | `ssk_template_declaration` | `"template declaration"` | Template declaration scope (`template<...>`). Maps to IL `sck_template_declaration` (8). |
| 9 | `ssk_template_instantiation` | `"template instantiation"` | Template instantiation scope (pushed by `push_template_instantiation_scope`). |
| 10 | `ssk_instantiation_context` | `"instantiation context"` | Instantiation context scope (tracks the chain of instantiation sites for diagnostics). |
| 11 | `ssk_module_decl_import` | `"module decl import"` | C++20 module declaration/import scope. |
| 12 | `ssk_module_isolation` | `"module isolation"` | C++20 module isolation scope (module purview boundary). |
| 13 | `ssk_pragma` | `"pragma"` | Pragma scope (for pragma-delimited regions). |
| 14 | `ssk_function_access` | `"function access"` | Function access scope. |
| 15 | `ssk_condition` | `"condition"` | Condition scope (if/while/for condition variable). Maps to IL `sck_condition` (15). |
| 16 | `ssk_enum` | `"enum"` | Scoped enum scope (C++11 `enum class`). Maps to IL `sck_enum` (16). |
| 17 | `ssk_function` | `"function"` | Function body scope (has routine pointer, parameters, ctor init list). Maps to IL `sck_function` (17). |

### Relationship to IL Scope Kinds

The IL scope node (288 bytes, allocated by `alloc_scope` at `sub_5E7D80`) uses a smaller `sck_*` enum at its `+28` field. The scope stack entry at `+192` points to the IL scope that persists after the stack entry is popped. Not all scope stack kinds produce an IL scope -- reactivation kinds (5, 7) and context kinds (9, 10) reuse existing IL scopes.

| IL `sck_*` | Value | Corresponding stack kind(s) |
|---|---|---|
| `sck_file` | 0 | 0 |
| `sck_func_prototype` | 1 | 1 |
| `sck_block` | 2 | 2 |
| `sck_namespace` | 3 | 3, 4, 5 |
| `sck_class_struct_union` | 6 | 6, 7 |
| `sck_template_declaration` | 8 | 8 |
| `sck_condition` | 15 | 15 |
| `sck_enum` | 16 | 16 |
| `sck_function` | 17 | 17 |

## CUDA-Specific Fields

NVIDIA added two device/host scope tracking bits to the scope entry, grafted onto the EDG base structure.

### Byte +6, Bit 1: Device Scope Context

```
scope_entry+6, bit 1 (0x02):

  When set: code in this scope is compiled for the device execution space.
  When clear: code in this scope is compiled for the host.
```

This bit is tested by CUDA-specific code paths to determine whether the current compilation context targets device or host. It affects:

- Whether `__device__`-only functions suppress certain diagnostics (e.g., missing return value warning at `check_void_return_okay`, `sub_719D20`)
- Whether device-specific type validation applies
- Severity overrides via `byte_126ED55` (default diagnostic severity for device mode)

The bit is set when entering `__device__` or `__global__` function scopes and cleared when entering `__host__` scopes. This allows mixed host/device compilation to track which context is active at any nesting depth.

### Byte +14, Bit 2: In Device Code

```
scope_entry+14, bit 2 (0x04):

  Secondary device-code marker. Set when the parser is inside a device
  function body. Used in conjunction with dword_106C2C0 (CUDA device
  compilation mode flag).
```

## Template Instantiation Depth Counters

Three fields at offsets `+576`, `+580`, and `+584` form the template instantiation depth tracking system. These fields enable the scope stack to handle nested template instantiations without fully pushing/popping scope entries at every nesting level.

### Mechanism

When `push_template_instantiation_scope` (`sub_709DE0`) sets up a template instantiation, it writes the current scope stack depth into `+580` (`orig_depth`) and the saved global depth into `+584` (`saved_scope_depth`). The `+576` counter starts at 0.

If the same template scope is re-entered for a nested instantiation (e.g., recursive template), `+576` is incremented rather than pushing a full new scope entry. On pop, `pop_template_instantiation_scope` (`sub_708EE0`) checks `+576`:

```
if (scope_entry[576] > 0) {
    scope_entry[576]--;     // just decrement, don't pop
    return;
}
// Full pop: restore scope stack to orig_depth
pop_scopes_to(scope_entry[580]);
restore(dword_126C5AC, scope_entry[584]);
```

This optimization avoids deep scope stack growth during deeply recursive template instantiations (e.g., `std::tuple<T1, T2, ..., TN>` with large N).

### Validation

`pop_template_instantiation_scope_with_check` (`sub_708E90`) validates that `+576` matches the expected depth before calling the actual pop. The assertion is at `scope_stk.c` line 5593. A mismatch triggers an internal error.

## Push Scope: `push_scope_full` (sub_700560)

The core scope push function (1476 lines, 13 parameters, located at `0x700560`). Called directly or via thin wrappers for each scope kind.

### Parameters

The 13-parameter signature handles all scope kinds through a single entry point:

1. Scope kind
2. Associated entity pointer (class type, namespace entity, routine descriptor, etc.)
3. Region number
4. Additional flags
5-13. Kind-specific parameters (template info, reactivation data, etc.)

### Key Operations

1. **Stack growth**: Increments `dword_126C5E4`. If the stack exceeds its allocation, it is reallocated (the base pointer `qword_126C5E8` may change).

2. **Entry initialization**: Zeros the 784-byte entry, then sets:
   - `+0` = scope number (from a global counter)
   - `+4` = scope kind
   - `+240` = region number
   - `+192` = IL scope pointer (newly allocated via `alloc_scope` or reused from a reactivated entity)
   - `+560` = parent scope index

3. **Kind-specific setup**:
   - **File** (0): Sets `dword_126C5DC`, initializes file-level state.
   - **Block** (2): Links to enclosing function scope.
   - **Namespace** (3, 4, 5): Sets `+232` to namespace entity. For extensions (4), reuses existing IL scope. For reactivation (5), calls `add_active_using_directives_for_scope`.
   - **Class** (6, 7): Sets `+216` to class type pointer, `dword_126C5C8` to current index. For reactivation (7), walks the class hierarchy to restore template context.
   - **Template declaration** (8): Sets template-related bits in `+8`.
   - **Function** (17): Sets `dword_126C5D8`, `qword_126C5D0`, `+224`.

4. **Parent scope linkage**: Calls `set_parent_scope_on_push` to establish the scope tree.

5. **Memory region**: Calls `get_enclosing_memory_region` to determine the memory arena for allocations within this scope.

### Push Wrappers

| Wrapper | Address | Parameters | Target Kind |
|---|---|---|---|
| `push_scope` (thin) | `sub_704790` | 7 | Various |
| `push_scope_with_using_dirs` | `sub_7047C0` | 29 | Namespace + using |
| `push_template_scope` | `sub_704870` | 7 | Template declaration (8) |
| `push_block_reactivation_scope` | `sub_7048A0` | 32 | Block reactivation |
| `push_namespace_scope_full` | `sub_7024D0` | 40 | Namespace (3) |
| `push_function_scope` | `sub_704BB0` | 13 | Function (17) |
| `push_class_scope` | `sub_704C10` | 17 | Class (6) |
| `push_scope_for_compound_statement` | `sub_70C8A0` | 64 | Block (2) |
| `push_scope_for_condition` | `sub_70C950` | 86 | Condition (15) |
| `push_scope_for_init_statement` | `sub_70CAE0` | 49 | Block (2), C++17 init |

## Pop Scope: `pop_scope` (sub_7076A0)

The core scope pop function (1142 lines, at `0x7076A0`). Complement to `push_scope_full`. Performs all scope cleanup in a specific order.

### Cleanup Sequence

1. **Debug trace**: If `dword_126EFC8` is set, prints `"pop_scope: number = %ld, depth = %d"`.

2. **Scope wrapup**: Calls `wrapup_scope` (`sub_706710`, 381 lines) which:
   - Iterates all symbols in the scope
   - Runs `end_of_scope_symbol_check` (`sub_705440`, 781 lines) for consistency validation
   - Emits needed definitions
   - Reports unreferenced entities

3. **Using-directive deactivation**: Clears active using-directives for this scope via `sub_6FEC10` (debug: clear using-directive).

4. **Template parameter restoration**: If leaving a template scope, calls `restore_default_template_params` (`sub_6FEEE0`) to undo template parameter symbol bindings.

5. **Name collision discriminators**: Assigns ABI discriminator values to entities with the same name in this scope via `assign_discriminators_to_entities_list` (`sub_7036E0`).

6. **C99 inline definitions**: Checks `check_c99_inline_definition` (`sub_703AD0`) for C99-mode inline function rules.

7. **Module/pragma state**: Adjusts STDC pragma state (`byte_126E558`/`559`/`55A`) and module context if applicable.

8. **Stack decrement**: Decrements `dword_126C5E4`. Restores previous scope's global state (function scope index, class scope index, etc.).

9. **Memory region disposal**: Frees the memory arena associated with this scope if the scope kind has one.

### Pop Variants

| Function | Address | Lines | Purpose |
|---|---|---|---|
| `pop_scope` (core) | `sub_7076A0` | 1142 | Full scope pop with all cleanup |
| `pop_scope_full` | `sub_70C440` | 100 | Wrapper calling core + name hiding cleanup |
| `pop_scope` (validation) | `sub_70C620` | 62 | Pop with object lifetime validation: asserts `"pop_scope: curr_object_lifetime is not that of"`, `"pop_scope: unexpected curr_object_lifetime"` |

## Template Instantiation Scope

The template instantiation scope push and pop are separate from the generic scope push/pop. They handle the complex process of binding template parameters to arguments, setting up the correct translation unit context, and managing pack expansions.

### Push: `push_template_instantiation_scope` (sub_709DE0)

The largest function in the `scope_stk.c` range at 1281 lines. Takes 8 parameters: template pointer, association info, and various flags.

**Key operations:**

1. **Translation unit check**: Calls `sub_7418D0` to verify that the template being instantiated belongs to the same translation unit, or that cross-TU instantiation is explicitly allowed (flag `& 0x1000`). Failure triggers the assert `"push_template_instantiation_scope: wrong translation unit"`.

2. **Template parameter binding**: Iterates the template parameter list and the instantiation argument list in parallel, creating bindings. For each template parameter:
   - Type parameters: binds to the supplied type argument
   - Non-type parameters: binds to the supplied expression/value
   - Template template parameters: binds to the supplied template

3. **Pack expansion**: For variadic templates, handles parameter pack expansion. Creates pack instantiation descriptors via `create_pack_instantiation_descr` (`sub_70CF50`, 772 lines).

4. **Scope entry setup**: Writes `+576` = 0, `+580` = current depth, `+584` = `dword_126C5AC`. Sets `+216` to the translation unit pointer.

5. **State save/restore**: Saves `dword_126C5B8` (`is_member_of_template`), `dword_126C5D8` (function scope), `qword_126C5D0` (routine descriptor).

6. **Reactivation flags**: Flag bits `& 0x84000` control class template reactivation behavior. When set, the function enters class reactivation mode via `sub_70BB60`.

### Pop: `pop_template_instantiation_scope` (sub_708EE0)

66 lines. Reverse of the push.

1. Reads `+576` (depth counter). If > 0, decrements and returns early (nested instantiation shortcut).
2. If bit 0 of `+9` is set (`needs_cleanup`), calls `sub_67B4E0()` to clean up template instantiation artifacts.
3. Pops scope entries back to `orig_depth` (`+580`) via `sub_7076A0`.
4. Restores `dword_126C5AC` from `+584`.
5. Calls `sub_6FED20` (debug trace: set using-directive).

### Related Functions

| Function | Address | Lines | Role |
|---|---|---|---|
| `pop_template_instantiation_scope_wrapper` | `sub_708E70` | 7 | Thin wrapper passing through to `sub_708EE0` |
| `pop_template_instantiation_scope_with_check` | `sub_708E90` | 14 | Validates `+576` depth counter before calling `sub_708EE0` |
| `pop_template_instantiation_scope_variant` | `sub_709110` | 71 | Alternative pop with extra `+8` flag processing, returns `int64` |
| `pop_instantiation_scope_for_rescan` | `sub_709000` | 54 | Pop for template argument rescan case |
| `push_instantiation_scope_for_rescan` | `sub_70B900` | 123 | Push for template parameter rescanning |
| `push_instantiation_scope_for_templ_param_rescan` | `sub_70B7C0` | 52 | Push for template parameter rescan |
| `push_instantiation_scope_for_class` | `sub_70BB60` | 131 | Push for class template instantiation |
| `push_class_and_template_reactivation_scope_full` | `sub_7098B0` | 261 | Combined class + template reactivation |

## Using-Directive Activation

When entering a namespace scope that has active `using namespace` directives, those directives must be reactivated so that names from the nominated namespace are visible. The scope stack manages this through two functions:

### add_active_using_directives_for_scope (sub_6FFCC0)

246 lines. Called during scope push when entering a namespace or block that may have inherited using-directives. Walks the using-directive list for the scope and calls `add_active_using_directive_to_scope` for each one.

Debug trace format: `"adding using-dir at depth %d for namespace %s applies at %d"`.

### Using-Directive Debug Traces

| Function | Address | Lines | Trace |
|---|---|---|---|
| Debug: set using-directive | `sub_6FED20` | 74 | `"setting using-dir at depth %d for namespace %s applies at %d"` |
| Debug: clear using-directive | `sub_6FEC10` | 34 | `"clearing using-dir at depth %d for namespace %s applies at %d"` |
| Debug: using-dir set/clear | `sub_704490` | 106 | `"using_dir"`, `"setting"`, `"clearing"` |

## Name Collision Discriminators

When multiple local entities share the same name (e.g., two `struct S` in different blocks within the same function), the Itanium ABI requires a discriminator suffix in the mangled name. The scope stack manages this through:

| Function | Address | Lines | Role |
|---|---|---|---|
| `get_name_collision_list` + `initialize_local_name_collision_table` | `sub_6FE760` | 64 | Manages the name collision table at `qword_12C6FE0` |
| `compute_local_name_collision_discriminator` + `distinct_lambda_signatures` | `sub_702FB0` | 293 | Computes ABI discriminator values for local entities; includes lambda signature discrimination logic |
| `cancel_name_collision_discriminator` | `sub_7034C0` | 118 | Cancels a previously assigned discriminator (7 assertion sites) |
| `assign_discriminators_to_entities_list` | `sub_7036E0` | 46 | Assigns ABI discriminators to a list of entities at scope exit |
| `set_parent_entity_for_closure_types` | `sub_703790` | 91 | Sets parent entity for lambda closure types (needed for correct mangling, 5 assertion sites) |
| `set_parent_routine_for_closure_types_in_default_args` | `sub_703920` | 43 | Sets parent routine for lambdas in default argument contexts |

## Class and Template Reactivation

When defining an out-of-line member function (`void MyClass::foo() { ... }`), the parser must reactivate the class scope so that class member names are visible. For class templates, this also requires reactivating the template instantiation scope.

### reactivate_class_context (sub_7029D0 / sub_70BE50)

Two implementations exist:

- `sub_7029D0` (196 lines, in p1.16): Reactivates a class scope for out-of-line definition. Asserts `"reactivate_class_context: class type has NULL assoc_info"`.
- `sub_70BE50` (130 lines, in p1.17): Additional variant that handles nesting, template flags, and scope_entry `+8` bit manipulation.

### push_class_and_template_reactivation_scope_full (sub_7098B0)

261 lines. Handles the combined case of class template reactivation. Reads symbol flags at offsets `+80`, `+81`, `+161`, `+162`. Processes "specified template decl info" at `+64` of assoc_info. Detects member templates via bit `0x10` at `+81`. When `dword_106BC58` is set, enters class reactivation mode with `sub_70BB60`.

### reactivate_local_context (sub_702670 / sub_70C0F0)

- `sub_702670` (120 lines): Reactivates a previously saved local scope context. Calls `push_scope_full`.
- `sub_70C0F0` (50 lines): Asserts `"reactivate_local_context"`.

## Pack Expansion Support

The scope stack provides infrastructure for variadic template parameter pack expansion during instantiation.

| Function | Address | Lines | Role |
|---|---|---|---|
| `create_pack_instantiation_descr` | `sub_70CF50` | 772 | Creates pack instantiation descriptors; handles sizeof..., fold expressions |
| `create_pack_instantiation_descr_helper` | `sub_70DD60` | 212 | Helper for pack descriptor creation |
| `cleanup_pack_instantiation_state` | `sub_70E130` | 37 | Cleans up pack expansion state |
| `end_potential_pack_expansion_context` | `sub_70E1D0` | 392 | Processes end of pack expansion; checks C++17 via `dword_126EF68 > 199900`; uses `qword_126C598` (pack expansion context) |
| `find_template_arg_for_pack` + `get_enclosing_template_params_and_args` | `sub_6FE9B0` | 140 | Traverses scope stack to find template arguments for parameter packs |

## Scope Stack Query Functions

| Function | Address | Lines | Role |
|---|---|---|---|
| `get_innermost_template_dependent_context` | `sub_6FE160` | 72 | Traverses scope stack to find innermost template-dependent scope |
| `get_outermost_template_dependent_context` | `sub_6FFA60` | 54 | Complement to innermost variant |
| `get_curr_template_params_and_args` (part 1) | `sub_70E7F0` | 321 | Retrieves current template parameters and arguments from scope stack |
| `get_curr_template_params_and_args` (full) | `sub_70F540` | 1002 | Full implementation with default argument handling and pack expansion |
| `is_in_template_context` | `sub_70EE10` | 16 | No-arg predicate, returns bool |
| `is_in_template_instantiation_scope` | `sub_70EDA0` | 27 | 6-arg predicate, returns bool |
| `current_class_symbol_if_class_template` | `sub_704130` | 84 | Returns class symbol if inside a class template definition |
| `is_in_deprecated_context` | `sub_70F440` | 43 | Checks `scope_entry[83]` bit 4 and walks scope stack |
| `get_scope_depth` | `sub_70C600` | 17 | Returns current scope stack depth value |
| `get_template_scope_info_for_entity` | `sub_7106B0` | 74 | Last function in scope_stk.c range |

## Debug and Statistics

### Scope Statistics Dump (sub_702DC0)

95 lines. Prints scope stack statistics when debug tracing is enabled. Output format:

```
Scope stack statistics
Stack entry size: 784
Max. stack depth: <N>
```

Followed by per-scope-kind counts using all scope kind display names.

### Scope Entry Dump (sub_700260 / sub_7002D0)

- `sub_700260` (17 lines): Prints `" scope %d"` with scope kind name via `scope_kind_to_string`. Detects bad depth with `"***BAD SCOPE DEPTH***"`.
- `sub_7002D0` (111 lines): Detailed dump using format `"%s%3ld %3d "` with associated type/symbol information.

## End-of-Scope Processing

### wrapup_scope (sub_706710)

381 lines. Major scope cleanup function called from `pop_scope`. Processes all symbols in the scope, emits needed definitions, runs `end_of_scope_symbol_check`. Debug traces: `"wrapup_scope"`, `"Wrapping up "`, `" scope"`.

### end_of_scope_symbol_check (sub_705440)

781 lines, 6 assertion sites. The largest validation function. Checks:
- Symbol-to-IL-entry parent-class consistency (`"end_of_scope_symbol_check: sym/il-entry parent-class mismatch"`)
- Parameter-to-routine association (`"end_of_scope_symbol_check: parameter with no assoc routine"`)
- Hash table statistics (`"hash_stats"`, `"Hash statistics for: "`)

### set_needed_flags_at_end_of_file_scope (sub_707040)

188 lines. Determines which entities need to be emitted at the end of the translation unit. Validates scope kind (`"set_needed_flags_at_end_of_file_scope: bad scope kind"`). Debug brackets: `"Start of set_needed_flags_at_end_of_file_scope\n"` / `"End of set_needed_flags_at_end_of_file_scope\n"`.

### finish_function_body_processing (sub_6FE2A0)

142 lines. Post-processes function bodies after the scope closes. Determines whether the function needs to be emitted (`"routine_needed_even_if_unreferenced"`, `"Not calling mark_as_needed for"`, `"storage class is %s\n"`).

## Cross-References

- [Entity Node Layout](entity-node.md) -- entity kind enum, execution space byte at `+182`
- [IL Overview](../il/overview.md) -- IL scope kinds (`sck_*`), IL entry kind 23 (scope, 288 bytes)
- [IL Allocation](../il/allocation.md) -- `alloc_scope` allocator for 288-byte IL scope nodes
- [Template Instance Record](template-instance.md) -- template instantiation data structures
- [Translation Unit Descriptor](translation-unit.md) -- TU pointer stored at scope entry `+216`
