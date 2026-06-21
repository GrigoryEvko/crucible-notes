# IL Overview

The Intermediate Language (IL) is the central data structure of the EDG frontend — a typed, scope-linked graph of every declaration, type, expression, statement, and template in the translation unit. cudafe++ (EDG 6.6) builds the IL during parsing, walks it for CUDA device/host separation, and emits it as the `.int.c` output. The IL never touches disk: `IL_SHOULD_BE_WRITTEN_TO_FILE=0` forces in-memory-only operation. All IL nodes live in a region-based arena allocator, organized into file-scope (region 1) and per-function (region N) memory pools.

The IL is versioned as `IL_VERSION_NUMBER="6.6"` and carries the compile-time flag `ALL_TEMPLATE_INFO_IN_IL=1`, meaning template definitions, specializations, and instantiation directives are fully represented in the IL graph rather than deferred to a separate template database.

## Key Configuration Constants

| Constant | Value | Meaning |
|---|---|---|
| `IL_VERSION_NUMBER` | `"6.6"` | IL format version, matches EDG version |
| `IL_SHOULD_BE_WRITTEN_TO_FILE` | `0` | IL is never serialized to disk |
| `ALL_TEMPLATE_INFO_IN_IL` | `1` | Full template data in IL graph |
| `IL_FILE_SUFFIX` | (string) | Suffix for IL file names if serialization were enabled |
| `sizeof_il_entry` sentinel | `9999` | Validated at init time (guard value in `qword_E6C580`) |

## IL Entry Kind System

Every IL node carries an `entry_kind` byte that identifies its type. The name table `off_E6DD80` (aliased as `il_entry_kind_names` at `off_E6E020`) maps these bytes to human-readable strings. The `il_one_time_init` function (`sub_5CF7F0`) validates that this table ends with a `"last"` sentinel.

There are 85 defined entry kind values (0-84). Some are primary node types with their own linked lists; others are auxiliary records displayed inline by their parent.

### Complete il_entry_kind Table

| Kind | Hex | Name | Bytes | Display | Notes |
|---|---|---|---|---|---|
| 0 | 0x00 | `none` | — | — | Null/invalid sentinel |
| 1 | 0x01 | `source_file_entry` | 80 | Case 1 | File name, line ranges, include flags |
| 2 | 0x02 | `constant` | 184 | Case 2 | 16 sub-kinds (ck_*) |
| 3 | 0x03 | `param_type` | 80 | Case 3 | Parameter type in function signature |
| 4 | 0x04 | `routine_type_supplement` | 64 | Inline | Embedded in routine type node |
| 5 | 0x05 | `routine_type_extra` | — | Inline | Additional routine type data |
| 6 | 0x06 | `type` | 176 | Case 6 | 22 sub-kinds (tk_*) |
| 7 | 0x07 | `variable` | 232 | Case 7 | Variables, parameters, structured bindings |
| 8 | 0x08 | `field` | 176 | Case 8 | Class/struct/union members |
| 9 | 0x09 | `exception_specification` | 16 | Case 9 | noexcept, throw() specs |
| 10 | 0x0A | `exception_spec_type` | 24 | Case 0xA | Type in exception specification |
| 11 | 0x0B | `routine` | 288 | Case 0xB | Functions, methods, constructors, destructors |
| 12 | 0x0C | `label` | 128 | Case 0xC | Goto labels, break/continue targets |
| 13 | 0x0D | `expr_node` | 72 | Case 0xD | 36 sub-kinds (enk_*) |
| 14 | 0x0E | (reserved) | — | Inline | Skipped in display |
| 15 | 0x0F | (reserved) | — | Inline | Skipped in display |
| 16 | 0x10 | `switch_case_entry` | 56 | Case 0x10 | Case value + range for switch |
| 17 | 0x11 | `switch_info` | 24 | Case 0x11 | Switch statement descriptor |
| 18 | 0x12 | `handler` | 40 | Case 0x12 | try/catch handler entry |
| 19 | 0x13 | `try_supplement` | 32 | Inline | Try block extra info |
| 20 | 0x14 | `asm_supplement` | — | Inline | Inline asm statement data |
| 21 | 0x15 | `statement` | 80 | Case 0x15 | 26 sub-kinds (stmk_*) |
| 22 | 0x16 | `object_lifetime` | 64 | Case 0x16 | Destruction ordering |
| 23 | 0x17 | `scope` | 288 | Case 0x17 | 9 sub-kinds (sck_*) |
| 24 | 0x18 | `base_class` | 112 | Case 0x18 | Inheritance record |
| 25 | 0x19 | `string_text` | 1* | — | Raw string literal bytes |
| 26 | 0x1A | `other_text` | 1* | — | Compiler version, misc text |
| 27 | 0x1B | `template_parameter` | 136 | Case 0x1B | Template param with supplement |
| 28 | 0x1C | `namespace` | 128 | Case 0x1C | Namespace declarations |
| 29 | 0x1D | `using_declaration` | 80 | Case 0x1D | Using declarations/directives |
| 30 | 0x1E | `dynamic_init` | 104 | Case 0x1E | 9 sub-kinds (dik_*) |
| 31 | 0x1F | `local_static_variable_init` | 40 | Case 0x1F | Static local init records |
| 32 | 0x20 | `vla_dimension` | 48 | Case 0x20 | Variable-length array bound |
| 33 | 0x21 | `overriding_virtual_func` | 40 | Case 0x21 | Virtual override info |
| 34 | 0x22 | (reserved) | — | Inline | Skipped in display |
| 35 | 0x23 | `derivation_path` | 24 | Case 0x23 | Base-class derivation step |
| 36 | 0x24 | `base_class_derivation` | 32 | — | Derivation detail record |
| 37 | 0x25 | (reserved) | — | Inline | Skipped in display |
| 38 | 0x26 | (reserved) | — | Inline | Skipped in display |
| 39 | 0x27 | `class_info` | 208 | Case 0x27 | Class type supplement |
| 40 | 0x28 | (reserved) | — | — | Skipped in display |
| 41 | 0x29 | `constructor_init` | 48 | Case 0x29 | Ctor member/base initializer |
| 42 | 0x2A | `asm_entry` | 152 | Case 0x2A | Inline assembly block |
| 43 | 0x2B | `asm_operand` | — | Case 0x2B | Asm constraint + expression |
| 44 | 0x2C | `asm_clobber` | — | Case 0x2C | Asm clobber register |
| 45 | 0x2D | (reserved) | — | Inline | Skipped in display |
| 46 | 0x2E | (reserved) | — | Inline | Skipped in display |
| 47 | 0x2F | (reserved) | — | Inline | Skipped in display |
| 48 | 0x30 | (reserved) | — | Inline | Skipped in display |
| 49 | 0x31 | `element_position` | 24 | — | Designator element position |
| 50 | 0x32 | `source_sequence_entry` | 32 | Case 0x32 | Declaration ordering |
| 51 | 0x33 | `full_entity_decl_info` | 56 | Case 0x33 | Full declaration info |
| 52 | 0x34 | `instantiation_directive` | 40 | Case 0x34 | Explicit instantiation |
| 53 | 0x35 | `src_seq_sublist` | 24 | Case 0x35 | Source sequence sub-list |
| 54 | 0x36 | `explicit_instantiation_decl` | — | Case 0x36 | extern template |
| 55 | 0x37 | `orphaned_entities` | 56 | Case 0x37 | Entities without parent scope |
| 56 | 0x38 | `hidden_name` | 32 | Case 0x38 | Hidden name entry |
| 57 | 0x39 | `pragma` | 64 | Case 0x39 | Pragma records (43 kinds) |
| 58 | 0x3A | `template` | 208 | Case 0x3A | Template declaration |
| 59 | 0x3B | `template_decl` | 40 | Case 0x3B | Template declaration head |
| 60 | 0x3C | `requires_clause` | 16 | Case 0x3C | C++20 requires clause |
| 61 | 0x3D | `template_param` | 136 | Case 0x3D | Template parameter entry |
| 62 | 0x3E | `name_reference` | 40 | Case 0x3E | Name lookup reference |
| 63 | 0x3F | `name_qualifier` | 40 | Case 0x3F | Qualified name qualifier |
| 64 | 0x40 | `seq_number_lookup` | 32 | Case 0x40 | Sequence number index |
| 65 | 0x41 | `local_expr_node_ref` | — | Case 0x41 | Local expression reference |
| 66 | 0x42 | `static_assert` | 24 | Case 0x42 | Static assertion |
| 67 | 0x43 | `linkage_spec` | 32 | Case 0x43 | extern "C"/"C++" block |
| 68 | 0x44 | `scope_ref` | 32 | Case 0x44 | Scope back-reference |
| 69 | 0x45 | (reserved) | — | Inline | Skipped in display |
| 70 | 0x46 | `lambda` | — | Case 0x46 | Lambda expression |
| 71 | 0x47 | `lambda_capture` | — | Case 0x47 | Lambda capture entry |
| 72 | 0x48 | `attribute` | 72 | Case 0x48 | C++11/GNU attribute |
| 73 | 0x49 | `attribute_argument` | 40 | Case 0x49 | Attribute argument |
| 74 | 0x4A | `attribute_group` | 8 | Case 0x4A | Attribute group |
| 75 | 0x4B | (reserved) | — | Inline | Skipped in display |
| 76 | 0x4C | (reserved) | — | Inline | Skipped in display |
| 77 | 0x4D | (reserved) | — | Inline | Skipped in display |
| 78 | 0x4E | (reserved) | — | Inline | Skipped in display |
| 79 | 0x4F | `template_info` | — | Case 0x4F | Template instantiation info |
| 80 | 0x50 | `subobject_path` | 24 | Case 0x50 | Address constant sub-path |
| 81 | 0x51 | (reserved) | — | Inline | Skipped in display |
| 82 | 0x52 | `module_info` | — | Case 0x52 | C++20 module metadata |
| 83 | 0x53 | `module_decl` | — | Case 0x53 | Module declaration |
| 84 | 0x54 | `last` | — | — | Sentinel for table validation |

**Inline entries** (kinds 4, 5, 14, 15, 19, 20, 27, 34, 37, 38, 40, 45-48, 69, 75-78, 81) are displayed as part of their parent node rather than as standalone IL entries. The display dispatcher (`sub_5F4930`) returns immediately for these kinds.

## IL Header Structure

The IL header lives in the BSS segment at `0x126EB60` and is printed by `display_il_header_and_file_scope` (`sub_5F76B0`). It records translation-unit-level metadata:

```c
struct il_header {                        // at xmmword_126EB60
    il_entry*   primary_source_file;      // +0x00  head of source file list
    scope*      primary_scope;            // +0x08  file-scope root
    routine*    main_routine;             // +0x10  main() if present
    char*       compiler_version;         // +0x18  "6.6" version string
    char*       time_of_compilation;      // +0x20  build timestamp
    uint8_t     plain_chars_are_signed;   // +0x28  signedness of plain char
    uint32_t    source_language;          // +0x2C  0=C++, 1=C (dword_126EBA8)
    uint32_t    std_version;             // +0x30  e.g. 201703 (dword_126EBAC)
    uint8_t     pcc_compatibility_mode;   // +0x34  PCC compat flag
    uint8_t     enum_type_is_integral;    // +0x35
    uint32_t    default_max_member_align; // +0x38
    uint8_t     gcc_mode;                // +0x3C  GCC compatibility
    uint8_t     gpp_mode;                // +0x3D  G++ compatibility
    uint32_t    gnu_version;             // +0x40  e.g. 40201
    uint8_t     short_enums;             // +0x44
    uint8_t     default_nocommon;         // +0x45
    uint8_t     UCN_identifiers_used;     // +0x46
    uint8_t     vla_used;                // +0x47
    uint8_t     any_templates_seen;       // +0x48
    uint8_t     prototype_instantiations_in_il;     // +0x49
    uint8_t     il_has_all_prototype_instantiations; // +0x4A
    uint8_t     il_has_C_semantics;       // +0x4B
    uint8_t     nontag_types_used_in_exception_or_rtti; // +0x4C
    il_entry*   seq_number_lookup_entries; // +0x50
    uint32_t    target_configuration_index; // +0x58
};
```

The `source_language` field selects the display string `"sl_Cplusplus"` or `"sl_C"`. When `source_language == 1` (C mode) and `std_version > 199900`, the routine display additionally prints C99 pragma state fields (`fp_contract`, `fenv_access`, `cx_limited_range`).

## Memory Region System

IL entries are allocated in numbered memory regions managed by a bump allocator (`sub_6B7D60`):

| Region | Purpose | Lifetime | Globals |
|---|---|---|---|
| 1 | File scope | Entire translation unit | `dword_126EC90` (region ID), `dword_126F690`/`dword_126F694` (base offset / prefix size) |
| 2..N | Per-function scope | Duration of function body processing | `dword_126EB40` (current region), `dword_126F688`/`dword_126F68C` (base offset / prefix size) |

Region 1 contains all file-scope declarations: types, global variables, function declarations, namespaces, templates. Regions 2+ are allocated one per function definition and hold that function's local variables, statements, expressions, labels, and temporaries. The region table at `qword_126EC88` maps region indices to their memory, while `qword_126EB90` maps region indices to their associated scope entries. `dword_126EC80` tracks the total number of regions.

The allocator selects file-scope vs function-scope by comparing `dword_126EB40 == dword_126EC90`. When equal, the node goes into region 1; otherwise it goes into the current function region. Some node types force a specific region:

- **Labels** (`alloc_label` at `sub_5E5CA0`): Assert that the current region is NOT file scope
- **Templates** (`alloc_template` at `sub_5E8D20`): Always file-scope only
- **Sequence number lookups** (`sub_5E9170`): Force region 1 by temporarily setting TU-copy mode

The display system (`sub_5F7DF0`) iterates all regions:

```c
// File scope
printf("Intermediate language for memory region 1 (file scope):");
walk_file_scope_il(display_il_entry, ...);   // sub_60E4F0

// Per-function regions
for (int r = 2; r <= region_count; r++) {
    scope* s = scope_table[r];
    routine* fn = s->assoc_routine;
    printf("Intermediate language for memory region %ld (function \"%s\"):",
           r, fn->name);
    walk_routine_scope_il(r, display_il_entry, ...);  // sub_610200
}
```

## IL Entry Prefix

Every IL node has a multi-qword prefix preceding the node body. The prefix size depends on allocation mode: 24 bytes (3 qwords) in normal file-scope mode, 16 bytes (2 qwords) in TU-copy mode, and 8 bytes (1 qword) for function-scope allocations. The allocator (`sub_6B7D60`) allocates a contiguous block and the caller returns a pointer past the prefix, so the prefix occupies negative offsets from the returned node pointer.

Normal file-scope mode (`dword_106BA08 == 0`, `dword_126F694 = 24`):

```text
Raw allocation layout (normal file-scope, 24-byte prefix):
Offset  Size  Field
------  ----  -----
+0      8     translation_unit_copy_address   (qword, zeroed in normal mode)
+8      8     next_in_list                    (qword, linked list pointer)
+16     8     prefix flags qword              (flags byte at +16, 7 bytes padding)
+24     ...   node body starts here           (returned pointer)

Node pointer perspective (ptr = raw + 24):
ptr - 24  = TU copy address   (8 bytes at raw+0)
ptr - 16  = next pointer       (8 bytes at raw+8)
ptr - 8   = prefix flags byte  (8 bytes at raw+16, flags in low byte)
ptr + 0   = first byte of node body
```

TU-copy mode (`dword_106BA08 != 0`, `dword_126F694 = 16`):

```text
Raw allocation layout (TU-copy mode, 16-byte prefix):
+0      8     next_in_list                    (no TU copy slot)
+8      8     prefix flags qword
+16     ...   node body starts here           (returned pointer)
```

Function-scope allocations (`dword_126F68C = 8`):

```text
Raw allocation layout (function-scope, 8-byte prefix):
+0      8     prefix flags qword              (no TU copy, no orphan slot)
+8      ...   node body starts here           (returned pointer)
```

The prefix flags byte is at `ptr - 8` from the returned node pointer (in all modes). The `next_in_list` pointer at `ptr - 16` is the linked list link used by the IL walker to traverse all entries of a given kind (file-scope only). The `translation_unit_copy_address` at `ptr - 24` stores the original address when a node is copied between translation units; it is zeroed in normal mode and absent in TU-copy and function-scope modes.

The keep_in_il test throughout cudafe++ uses `*(signed char*)(entry - 8) < 0` to check bit 7 of the prefix flags byte — this works because the flags byte is always at offset `-8` from the node pointer regardless of allocation mode.

### Prefix Flags Byte

The prefix flags byte (at offset `-8` from the returned node pointer) encodes scope and language information:

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | 0x01 | `allocated` | Always set on allocation |
| 1 | 0x02 | `file_scope` | Set when `!dword_106BA08` (not in TU-copy mode) |
| 2 | 0x04 | `is_in_secondary_il` | Entry came from secondary translation unit |
| 3 | 0x08 | `language_flag` | Copies `dword_126E5FC & 1` (C++ vs C mode indicator) |
| 7 | 0x80 | `keep_in_il` | **CUDA-critical**: marks entry for device IL output |

Bit 7 (`keep_in_il`) is the mechanism by which cudafe++ selects device-relevant declarations. The `mark_to_keep_in_il` pass in `il_walk.c` sets this bit on all entries that are needed for device compilation. See [Device/Host Separation](../cuda/device-host-separation.md) and [keep-in-il](./keep-in-il.md) for details.

## Sub-Kind Systems

Most primary IL entry kinds use a secondary kind byte to discriminate between variants. These sub-kind enums are the core classification taxonomy of the IL.

### Type Kinds (tk_*)

The type kind byte lives at offset `+132` in the type node body. 22 values, dispatched by `set_type_kind` (`sub_5E2E80`) and displayed by `display_type` (`sub_5F06B0`):

| Value | Name | Supplement | Size | Notes |
|---|---|---|---|---|
| 0 | `tk_error` | — | — | Error/placeholder type |
| 1 | `tk_void` | — | — | void |
| 2 | `tk_integer` | integer_type_supplement | 32 | int, char, bool, enum, wchar_t, char8/16/32_t |
| 3 | `tk_float` | — | — | float, double, long double |
| 4 | `tk_complex` | — | — | _Complex float/double/ldouble |
| 5 | `tk_imaginary` | — | — | _Imaginary (C99) |
| 6 | `tk_pointer` | — | — | Pointer, reference, rvalue reference |
| 7 | `tk_routine` | routine_type_supplement | 64 | Function type (return + params) |
| 8 | `tk_array` | — | — | Fixed and variable-length arrays |
| 9 | `tk_class` | class_type_supplement | 208 | class types |
| 10 | `tk_struct` | class_type_supplement | 208 | struct types |
| 11 | `tk_union` | class_type_supplement | 208 | union types |
| 12 | `tk_typeref` | typeref_type_supplement | 56 | typedef, using, decltype, typeof |
| 13 | `tk_ptr_to_member` | — | — | Pointer-to-member |
| 14 | `tk_template_param` | templ_param_supplement | 40 | Template type parameter |
| 15 | `tk_vector` | — | — | SIMD vector type |
| 16 | `tk_scalable_vector` | — | — | Scalable vector (SVE) |
| 17 | `tk_nullptr` | — | — | std::nullptr_t |
| 18 | `tk_mfp8` | — | — | 8-bit floating point |
| 19 | `tk_scalable_vector_count` | — | — | Scalable vector predicate |
| 20 | (auto/decltype_auto) | — | — | Placeholder types |
| 21 | (typeof_unqual/typeof_type) | — | — | C23 typeof |

The display function references `off_A6FE40` (22 string entries) for type kind names. The typeref sub-kind table at `off_A6F640` has 28 entries covering typedef aliases, decltype expressions, auto, and concept-constrained placeholders.

### Constant Kinds (ck_*)

The constant kind byte lives at offset `+148` in the constant node. 16 values, dispatched by `display_constant` (`sub_5F2720`):

| Value | Name | Notes |
|---|---|---|
| 0 | `ck_error` | Error placeholder |
| 1 | `ck_integer` | Integer value (arbitrary precision via `sub_602F20`) |
| 2 | `ck_string` | String/character literal (char kind + length + raw bytes) |
| 3 | `ck_float` | Floating-point constant |
| 4 | `ck_complex` | Complex constant (real + imaginary) |
| 5 | `ck_imaginary` | Imaginary constant |
| 6 | `ck_address` | Address constant with 7 address sub-kinds (abk_*) |
| 7 | `ck_ptr_to_member` | Pointer-to-member constant |
| 8 | `ck_label_difference` | GNU label address difference |
| 9 | `ck_dynamic_init` | Dynamically initialized constant |
| 10 | `ck_aggregate` | Aggregate initializer (linked list of sub-constants) |
| 11 | `ck_init_repeat` | Repeated initializer (constant + count) |
| 12 | `ck_template_param` | Template parameter constant with 15 sub-kinds (tpck_*) |
| 13 | `ck_designator` | Designated initializer |
| 14 | `ck_void` | Void constant |
| 15 | `ck_reflection` | Reflection entity reference |

Address constant sub-kinds (`abk_*`): `abk_routine`, `abk_variable`, `abk_constant`, `abk_temporary`, `abk_uuidof`, `abk_typeid`, `abk_label`.

Template parameter constant sub-kinds (`tpck_*`): `tpck_param`, `tpck_expression`, `tpck_member`, `tpck_unknown_function`, `tpck_address`, `tpck_sizeof`, `tpck_datasizeof`, `tpck_alignof`, `tpck_uuidof`, `tpck_typeid`, `tpck_noexcept`, `tpck_template_ref`, `tpck_integer_pack`, `tpck_destructor`.

### Expression Node Kinds (enk_*)

The expression kind byte lives at offset `+24` in the expression node. 36 values, dispatched by `display_expr_node` (`sub_5ECFE0`):

| Value | Name | Notes |
|---|---|---|
| 0 | `enk_error` | Error expression |
| 1 | `enk_operation` | Binary/unary/ternary operation (120 operator sub-kinds via eok_*) |
| 2 | `enk_constant` | Constant reference |
| 3 | `enk_variable` | Variable reference |
| 4 | `enk_field` | Field access |
| 5 | `enk_temp_init` | Temporary initialization |
| 6 | `enk_lambda` | Lambda expression |
| 7 | `enk_new_delete` | new/delete expression (56-byte supplement) |
| 8 | `enk_throw` | throw expression (24-byte supplement) |
| 9 | `enk_condition` | Conditional expression (32-byte supplement) |
| 10 | `enk_object_lifetime` | Object lifetime management |
| 11 | `enk_typeid` | typeid expression |
| 12 | `enk_sizeof` | sizeof expression |
| 13 | `enk_sizeof_pack` | sizeof...(pack) |
| 14 | `enk_alignof` | alignof expression |
| 15 | `enk_datasizeof` | NVIDIA __datasizeof extension |
| 16 | `enk_address_of_ellipsis` | Address of variadic parameter |
| 17 | `enk_statement` | Statement expression (GCC extension) |
| 18 | `enk_reuse_value` | Reused value reference |
| 19 | `enk_routine` | Function reference |
| 20 | `enk_type_operand` | Type as operand (e.g., in sizeof) |
| 21 | `enk_builtin_operation` | Compiler builtin (indexed via `off_E6C5A0`) |
| 22 | `enk_param_ref` | Parameter reference |
| 23 | `enk_braced_init_list` | C++11 braced init list |
| 24 | `enk_c11_generic` | C11 _Generic selection |
| 25 | `enk_builtin_choose_expr` | GCC __builtin_choose_expr |
| 26 | `enk_yield` | C++20 co_yield |
| 27 | `enk_await` | C++20 co_await |
| 28 | `enk_fold_expression` | C++17 fold expression |
| 29 | `enk_initializer` | Initializer expression |
| 30 | `enk_concept_id` | C++20 concept-id |
| 31 | `enk_requires` | C++20 requires expression |
| 32 | `enk_compound_req` | Compound requirement |
| 33 | `enk_nested_req` | Nested requirement |
| 34 | `enk_const_eval_deferred` | Deferred constexpr evaluation |
| 35 | `enk_template_name` | Template name expression |

The `enk_operation` kind (value 1) carries an additional `operation.kind` byte dispatched through `off_A6F840` (120 entries, the `eok_*` enum) and an `operation.type_kind` byte from `off_A6FE40` (22 entries).

### Expression Operation Kinds (eok_*)

The 120+ operation kinds cover all C++ operators. Key groups:

| Category | Operations |
|---|---|
| Arithmetic | `eok_add`, `eok_subtract`, `eok_multiply`, `eok_divide`, `eok_remainder`, `eok_negate`, `eok_unary_plus` |
| Bitwise | `eok_and`, `eok_or`, `eok_xor`, `eok_complement`, `eok_shiftl`, `eok_shiftr` |
| Comparison | `eok_eq`, `eok_ne`, `eok_lt`, `eok_gt`, `eok_le`, `eok_ge`, `eok_spaceship` |
| Logical | `eok_land`, `eok_lor`, `eok_not` |
| Assignment | `eok_assign`, `eok_add_assign`, `eok_subtract_assign`, `eok_multiply_assign`, etc. |
| Pointer | `eok_indirect`, `eok_address_of`, `eok_padd`, `eok_psubtract`, `eok_pdiff`, `eok_subscript` |
| Member access | `eok_dot_field`, `eok_points_to_field`, `eok_dot_static`, `eok_points_to_static`, `eok_pm_field`, `eok_points_to_pm_call` |
| Casts | `eok_cast`, `eok_lvalue_cast`, `eok_ref_cast`, `eok_dynamic_cast`, `eok_bool_cast`, `eok_base_class_cast`, `eok_derived_class_cast` |
| Calls | `eok_call`, `eok_dot_member_call`, `eok_points_to_member_call`, `eok_dot_pm_call`, `eok_points_to_pm_func_ptr` |
| Increment | `eok_pre_incr`, `eok_pre_decr`, `eok_post_incr`, `eok_post_decr` |
| Complex | `eok_real_part`, `eok_imag_part`, `eok_xconj` |
| Vector | `eok_vector_fill`, `eok_vector_eq`, `eok_vector_ne`, `eok_vector_lt`, `eok_vector_gt`, `eok_vector_le`, `eok_vector_ge`, `eok_vector_subscript`, `eok_vector_question`, `eok_vector_land`, `eok_vector_lor`, `eok_vector_not` |
| Control | `eok_comma`, `eok_question`, `eok_parens`, `eok_lvalue`, `eok_lvalue_adjust`, `eok_noexcept` |
| Variadic | `eok_va_start`, `eok_va_end`, `eok_va_arg`, `eok_va_copy`, `eok_va_start_single_operand` |
| Virtual | `eok_virtual_function_ptr`, `eok_dot_vacuous_destructor_call`, `eok_points_to_vacuous_destructor_call` |
| Misc | `eok_array_to_pointer`, `eok_reference_to`, `eok_ref_indirect`, `eok_ref_dynamic_cast`, `eok_pm_base_class_cast`, `eok_pm_derived_class_cast`, `eok_class_rvalue_adjust` |

### Statement Kinds (stmk_*)

The statement kind byte lives at offset `+32` in the statement node. 26 values:

| Value | Name | Supplement | Notes |
|---|---|---|---|
| 0 | `stmk_expr` | — | Expression statement |
| 1 | `stmk_if` | — | if statement |
| 2 | `stmk_constexpr_if` | 24 bytes | if constexpr (C++17) |
| 3 | `stmk_if_consteval` | — | if consteval (C++23) |
| 4 | `stmk_if_not_consteval` | — | if !consteval (C++23) |
| 5 | `stmk_while` | — | while loop |
| 6 | `stmk_goto` | — | goto statement |
| 7 | `stmk_label` | — | Label statement |
| 8 | `stmk_return` | — | return statement |
| 9 | `stmk_coroutine` | 128 bytes | C++20 coroutine body (full coroutine descriptor) |
| 10 | `stmk_coroutine_return` | — | co_return statement |
| 11 | `stmk_block` | 32 bytes | Compound statement / block |
| 12 | `stmk_end_test_while` | — | do-while loop |
| 13 | `stmk_for` | 24 bytes | for loop |
| 14 | `stmk_range_based_for` | — | C++11 range-for (iterator, begin, end, incr) |
| 15 | `stmk_switch_case` | — | case label |
| 16 | `stmk_switch` | 24 bytes | switch statement |
| 17 | `stmk_init` | — | Declaration with initializer |
| 18 | `stmk_asm` | — | Inline assembly |
| 19 | `stmk_try_block` | 32 bytes | try block |
| 20 | `stmk_decl` | — | Declaration statement |
| 21 | `stmk_set_vla_size` | — | VLA size computation |
| 22 | `stmk_vla_decl` | — | VLA declaration |
| 23 | `stmk_assigned_goto` | — | GCC computed goto |
| 24 | `stmk_empty` | — | Empty statement |
| 25 | `stmk_stmt_expr_result` | — | GCC statement expression result |

The coroutine statement (kind 9) carries the largest supplement at 128 bytes, containing traits, handle, promise, initial/final suspend calls, unhandled_exception call, get_return_object call, new/delete routines, and parameter copies. A preserved typo in the embedded strings reads `"paramter_copies"` (missing 'e'), confirming genuine EDG lineage.

### Scope Kinds (sck_*)

The scope kind byte lives at offset `+28` in the scope node. 9 observed values:

| Value | Name | Notes |
|---|---|---|
| 0 | `sck_file` | File scope (translation unit root) |
| 1 | `sck_func_prototype` | Function prototype scope |
| 2 | `sck_block` | Block scope (compound statement) |
| 3 | `sck_namespace` | Namespace scope |
| 6 | `sck_class_struct_union` | Class/struct/union scope |
| 8 | `sck_template_declaration` | Template declaration scope |
| 15 | `sck_condition` | Condition scope (if/while/for condition variable) |
| 16 | `sck_enum` | Enum scope (C++11 scoped enums) |
| 17 | `sck_function` | Function body scope (has routine ptr, parameters, ctor inits) |

Scope kinds determine which child lists are displayed. The bitmask `(1 << kind) & 0x20044` (bits 2, 6, 17 = block, class/struct/union, function) and `(1 << kind) & 0x9` (bits 0, 3 = file, namespace) control whether `namespaces`, `using_declarations`, and `using_directives` lists appear.

### Dynamic Init Kinds (dik_*)

The dynamic init kind byte lives at offset `+48`. 9 values:

| Value | Name | Notes |
|---|---|---|
| 0 | `dik_none` | No initialization |
| 1 | `dik_zero` | Zero initialization |
| 2 | `dik_constant` | Constant initializer |
| 3 | `dik_expression` | Expression initializer |
| 4 | `dik_class_result_via_ctor` | Class value via constructor call |
| 5 | `dik_constructor` | Constructor call (routine + args) |
| 6 | `dik_nonconstant_aggregate` | Non-constant aggregate init |
| 7 | `dik_bitwise_copy` | Bitwise copy from source |
| 8 | `dik_lambda` | Lambda initialization |

## Common IL Node Header

All primary IL node types (type, variable, field, routine, scope, namespace, template, etc.) share a 96-byte common header copied from a template at `xmmword_126F6A0..126F6F0`. This header is initialized by `init_il_alloc` (`sub_5EAD80`) and contains:

- Source correspondence (`source_corresp`) block: name, position, parent scope, access specifier, linkage, flags
- The display function `display_source_corresp` (`sub_5EDF40`) prints these fields for every entity type

Key source correspondence fields (printed for all entities):
- `name` and `unmangled_name_or_mangled_encoding`
- `decl_position` (line + column)
- `name_references` list
- `is_class_member` + `access` (from `off_A6F760`: public/protected/private/none)
- `parent_scope` and `enclosing_routine`
- `name_linkage` (from `off_E6E040`: none/internal/external/C/C++)
- Flags: `referenced`, `needed`, `is_local_to_function`, `marked_as_gnu_extension`, `externalized`, `maybe_unused`, `is_deprecated_or_unavailable`

## Initialization and Reset

The IL subsystem initializes in two phases:

### One-Time Init (sub_5CF7F0)

Called once at program startup. Validates 7 name-table arrays end with `"last"` sentinels:

| Table | Address | Content |
|---|---|---|
| `il_entry_kind_names` | `off_E6E020` | 85 IL entry kind names |
| `db_storage_class_names` | `off_E6CD78` | Storage class enum names |
| `db_special_function_kinds` | `off_E6D228` | Special function kind names |
| `db_operator_names` | `off_E6CD20` | Operator kind names |
| `name_linkage_kind_names` | `off_E6E060` | Linkage kind names |
| `decl_modifier_names` | `off_E6CD88` | Declaration modifier names |
| `pragma_ids` | `off_E6CF38` | Pragma identifier names |

Also validates `unsigned_int_kind_of` table (`byte_E6D1AD == 111 == 'o'`) and initializes 60+ allocation pools via `sub_7A3C00` (pool_init) with element sizes ranging from 1 to 1344 bytes.

### Per-TU Init (sub_5CFE20)

Called at the start of each translation unit compilation. Zeroes all pool heads, allocates the constant-sharing hash table (16,312 bytes = 2,039 buckets at `qword_126F228`), and the character-type hash table (3,240 bytes at `qword_126F2F8`). Sets sharing mode flags (`byte_126E558..126E55A = 3`). Tail-calls `sub_5EAF00` to reset float constant caches.

### Secondary Pool Reset (sub_5D0170)

Resets ~80 transient globals in the `126F680..126F978` range between template instantiation passes. Pure state zeroing, no allocation.

## Constant Sharing

IL constants are deduplicated via a 2,039-bucket hash table at `qword_126F228`. The `alloc_shareable_constant` function (`sub_5D2390`) checks `constant_is_shareable` (`sub_5D2210`) — which excludes aggregate constants (kind 10), template parameter constants (kind 12), and string literals when string sharing is disabled (`dword_126E1C0`).

On a cache hit, the existing constant is relinked to the front of its bucket chain. On a miss, a new 184-byte constant is allocated and inserted. Statistics are tracked: total allocations (`qword_126F208`), comparisons (`qword_126F200`), region hits (`qword_126F218`), global hits (`qword_126F220`), and new buckets (`qword_126F210`).

## CUDA Extensions to IL

NVIDIA adds several CUDA-specific fields to standard EDG IL nodes:

- **Routine flags** (bytes 182-183): `nvvm_intrinsic`, `global` (__global__), `device` (__device__), `host` (__host__)
- **Variable flags**: `shared` (__shared__), `constant` (__constant__), `device` (__device__), `managed` (__managed__)
- **keep_in_il bit** (prefix byte bit 7): The mechanism for device/host code separation
- **Lambda entries** (kinds 0x46, 0x47): Extended lambda wrapper support

These extensions are what make cudafe++ the CUDA-aware C++ frontend rather than a stock EDG compiler.

## Function Map

| Address | Function | Source | Notes |
|---|---|---|---|
| `sub_5CF7F0` | `il_one_time_init` | il.c | Validates tables, inits 60+ pools |
| `sub_5CFE20` | `il_init` / `il_reset` | il.c | Per-TU initialization |
| `sub_5D0170` | `il_reset_secondary_pools` | il.c | Template instantiation reset |
| `sub_5D01F0` | `il_rebuild_entry_index` | il.c | Build entry pointer index |
| `sub_5D02F0` | `il_invalidate_entry_index` | il.c | Clear entry index |
| `sub_5D0750` | `compare_expressions` | il.c | Deep structural equality |
| `sub_5D1350` | `compare_constants` | il.c | Constant comparison (525 lines) |
| `sub_5D1FE0` | `compare_dynamic_inits` | il.c | Dynamic init comparison |
| `sub_5D2210` | `constant_is_shareable` | il.c | Shareability predicate |
| `sub_5D2390` | `alloc_shareable_constant` | il.c | Hash-table dedup allocation |
| `sub_5D2F90` | `i_copy_expr_tree` | il.c | Deep expression tree copy |
| `sub_5D3B90` | `i_copy_constant_full` | il.c | Deep constant copy |
| `sub_5D47A0` | `i_copy_dynamic_init` | il.c | Deep dynamic init copy |
| `sub_5E2E80` | `set_type_kind` | il_alloc.c | Type kind dispatch (22 kinds) |
| `sub_5E3D40` | `alloc_type` | il_alloc.c | 176-byte type node |
| `sub_5E4D20` | `alloc_variable` | il_alloc.c | 232-byte variable node |
| `sub_5E4F70` | `alloc_field` | il_alloc.c | 176-byte field node |
| `sub_5E53D0` | `alloc_routine` | il_alloc.c | 288-byte routine node |
| `sub_5E5CA0` | `alloc_label` | il_alloc.c | 128-byte label node |
| `sub_5E5F00` | `set_expr_node_kind` | il_alloc.c | Expression kind dispatch |
| `sub_5E62E0` | `alloc_expr_node` | il_alloc.c | 72-byte expression node |
| `sub_5E6E20` | `set_statement_kind` | il_alloc.c | Statement kind dispatch |
| `sub_5E7060` | `alloc_statement` | il_alloc.c | 80-byte statement node |
| `sub_5E7D80` | `alloc_scope` | il_alloc.c | 288-byte scope node |
| `sub_5E7A70` | `alloc_namespace` | il_alloc.c | 128-byte namespace node |
| `sub_5E8D20` | `alloc_template` | il_alloc.c | 208-byte template node |
| `sub_5E99D0` | `dump_il_table_statistics` | il_alloc.c | Print allocation stats |
| `sub_5EAD80` | `init_il_alloc` | il_alloc.c | Initialize common header template |
| `sub_5F4930` | `display_il_entry` | il_to_str.c | Main display dispatcher (~1,686 lines) |
| `sub_5F76B0` | `display_il_header_and_file_scope` | il_to_str.c | IL header + region 1 |
| `sub_5F7DF0` | `display_il_file` | il_to_str.c | Top-level display entry point |
| `sub_60E4F0` | `walk_file_scope_il` | il_walk.c | File-scope tree walker |
| `sub_610200` | `walk_routine_scope_il` | il_walk.c | Per-function tree walker |

## Cross-References

- [IL Allocation](./allocation.md) — Arena allocator details, node sizes, free lists
- [IL Walking](./walking.md) — Tree traversal framework with 5 callback slots
- [keep-in-il](./keep-in-il.md) — Device code selection via bit 7
- [IL Display](./display.md) — Debug dump format and output
- [IL Comparison & Copy](./comparison-copy.md) — Expression/constant comparison and deep copy
- [Device/Host Separation](../cuda/device-host-separation.md) — CUDA IL marking
- [Type System](../edg/type-system.md) — 22 type kinds in detail
