# IL Display

The IL display subsystem produces a human-readable text dump of the entire Intermediate Language graph. It is compiled from EDG's `il_to_str.c` (source path `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/il_to_str.c`, confirmed by an assertion at line 6175 in `form_float_constant`). The display code occupies address range `0x5EB290`--`0x603A00` in the binary (roughly 90KB), with the main dispatch functions at `0x5EC600`--`0x5F7FD0` and formatting helpers continuing through `0x6039E0`.

Activation is via the `il_display` CLI flag (flag index 10 in the boolean flag table), which triggers `display_il_file` after the frontend completes parsing. The output goes to stdout through an indirectable callback mechanism (`qword_126F980`). When active, every IL entry in every memory region is printed with labeled fields, 25-column-aligned formatting, and scope/address annotations.

## Key Facts

| Property | Value |
|---|---|
| Source file | `il_to_str.c` (EDG 6.6) |
| Address range | `0x5EB290`--`0x6039E0` |
| Top-level entry point | `sub_5F7DF0` (`display_il_file`), 56 lines |
| Header + file-scope | `sub_5F76B0` (`display_il_header`), 174 lines |
| Main dispatcher | `sub_5F4930` (`display_il_entry`), 1,686 lines |
| Single-entity display | `sub_5F7D50` (`display_single_entity`), 38 lines |
| CLI flag | `il_display` (index 10, boolean) |
| Output callback | `qword_126F980` (function pointer, default `sub_5EB290` = `fputs(s, stdout)`) |
| Display-active flag | `byte_126FA16` (set to 1 during display) |
| Scope context flag | `dword_126FA30` (1 = file-scope region, 0 = function-scope) |
| Entry kind name table | `off_E6DD80` (~84 entries, indexed by entry kind byte) |

## Top-Level Control Flow

```
display_il_file (sub_5F7DF0)
│
│  printf("Display of IL file \"%s\", produced by the compilation of \"%s\"\n",
│         il_file_name, source_file_name)
│
├── display_il_header (sub_5F76B0)
│   │  dword_126FA30 = 1                              // file-scope mode
│   │  puts("\n\nIntermediate language for memory region 1 (file scope):")
│   │  puts("\nil_header:")
│   │  ... 30+ header fields ...
│   │
│   └── walk_file_scope_il(display_il_entry, ...)      // sub_60E4F0
│       └── display_il_entry (sub_5F4930)              // callback per entity
│
└── for region = 2 .. dword_126EC80:
        dword_126FA30 = 0                              // function-scope mode
        // lookup function name from scope table
        printf("\n\nIntermediate language for memory region %ld (function \"%s\"):\n",
               region, function_name)
        walk_routine_scope_il(region, display_il_entry, ...)   // sub_610200
        └── display_il_entry (sub_5F4930)              // callback per entity
```

Memory region 1 is always file-scope (global declarations, types, templates). Regions 2+ correspond to individual function bodies. The scope table at `qword_126EB90` maps each region index to its owning scope entry; the display code checks `scope.kind == 17` (`sck_function`) and extracts the routine name for the banner.

## IL Header Fields

`display_il_header` (`sub_5F76B0`) prints the translation-unit-level metadata stored in BSS at `0x126EB60`--`0x126EBF8`:

| Field | Type | Notes |
|---|---|---|
| `primary_source_file` | IL pointer | Source file entry for the main `.cu` file |
| `primary_scope` | IL pointer | File-scope scope entry |
| `main_routine` | IL pointer | `main()` routine entry, if present |
| `compiler_version` | string | EDG compiler version string |
| `time_of_compilation` | string | Build timestamp |
| `plain_chars_are_signed` | bool | Default `char` signedness |
| `source_language` | enum | `sl_Cplusplus` (0) or `sl_C` (1), from `dword_126EBA8` |
| `std_version` | integer | C/C++ standard version (e.g., 201703 for C++17), from `dword_126EBAC` |
| `pcc_compatibility_mode` | bool | PCC compatibility |
| `enum_type_is_integral` | bool | Whether enum underlying type is integral |
| `default_max_member_alignment` | integer | Default structure packing alignment |
| `gcc_mode` | bool | GCC compatibility mode |
| `gpp_mode` | bool | G++ compatibility mode |
| `gnu_version` | integer | GNU compatibility version number |
| `short_enums` | bool | `-fshort-enums` behavior |
| `default_nocommon` | bool | Default `-fno-common` |
| `UCN_identifiers_used` | bool | Universal character names in identifiers |
| `vla_used` | bool | Variable-length arrays present |
| `any_templates_seen` | bool | Whether any templates were parsed |
| `prototype_instantiations_in_il` | bool | Template prototype instantiations included |
| `il_has_all_prototype_instantiations` | bool | All prototypes included (ALL_TEMPLATE_INFO_IN_IL=1) |
| `il_has_C_semantics` | bool | C-language semantics active |
| `nontag_types_used_in_exception_or_rtti` | bool | Non-tag types in EH/RTTI |
| `seq_number_lookup_entries` | integer | Count of source sequence entries |
| `target_configuration_index` | integer | Target configuration selector |

After printing the header, `display_il_header` calls `walk_file_scope_il` (`sub_60E4F0`) with `display_il_entry` as the per-entity callback. This iterates every IL entry in file-scope region 1.

## The Main Dispatcher: display_il_entry

`display_il_entry` (`sub_5F4930`, 1,686 lines) is the central per-entity display function. It receives an entry pointer and an entry kind byte, and dispatches to the appropriate per-kind display function.

### Transparent (Inline) Kinds

The first switch handles kinds that are displayed inline by their parent and should not appear as standalone entries. These return immediately without output:

```
Transparent kinds (early return):
  4   routine_type_supplement      15  (reserved)
  5   routine_type_extra           19  try_supplement
  14  (reserved)                   20  asm_supplement
  27  template_parameter_suppl     34  (reserved)
  37  (reserved)                   38  (reserved)
  46  (reserved)                   47  (reserved)
  48  (reserved)                   75  (reserved)
  76  (reserved)                   77  (reserved)
  78  (reserved)                   81  (reserved)
```

### Entry Header Line

For non-transparent kinds, the dispatcher prints a scope-annotated header:

```
file-scope type@7f3a4b200100
func-scope variable@7f3a4b300200
```

The scope prefix comes from `dword_126FA30` (1 = `"file-scope"`, 0 = `"func-scope"`). The kind name is looked up from `off_E6DD80[kind_byte]`. The address is the raw entry pointer value. For entries in function-scope regions while `dword_126FA30 == 1`, a warning `"**NON FILE SCOPE PTR**"` is emitted.

### Dispatch Table

The second switch dispatches to specialized display functions:

| Kind | Hex | Name | Display function | Lines |
|---|---|---|---|---|
| 1 | 0x01 | `source_file_entry` | inline in dispatcher | ~40 |
| 2 | 0x02 | `constant` | `sub_5F2720` (`display_constant`) | 605 |
| 3 | 0x03 | `param_type` | inline in dispatcher | ~30 |
| 6 | 0x06 | `type` | `sub_5F06B0` (`display_type`) | 1,033 |
| 7 | 0x07 | `variable` | `sub_5EE500` (`display_variable`) | 614 |
| 8 | 0x08 | `field` | inline in dispatcher | ~80 |
| 9 | 0x09 | `exception_specification` | inline | ~20 |
| 10 | 0x0A | `exception_spec_type` | inline | ~10 |
| 11 | 0x0B | `routine` | `sub_5EF1A0` (`display_routine`) | 1,160 |
| 12 | 0x0C | `label` | inline | ~30 |
| 13 | 0x0D | `expr_node` | `sub_5ECFE0` (`display_expr_node`) | 534 |
| 16 | 0x10 | `switch_case_entry` | inline | ~15 |
| 17 | 0x11 | `switch_info` | inline | ~10 |
| 18 | 0x12 | `handler` | inline | ~15 |
| 21 | 0x15 | `statement` | `sub_5EC600` (`display_statement`) | 328 |
| 22 | 0x16 | `object_lifetime` | inline | ~20 |
| 23 | 0x17 | `scope` | `sub_5F2140` (`display_scope`) | 177 |
| 28 | 0x1C | `namespace` | inline | ~20 |
| 29 | 0x1D | `using_declaration` | inline | ~20 |
| 30 | 0x1E | `dynamic_init` | `sub_5F37F0` (`display_dynamic_init`) | 248 |
| 31 | 0x1F | `local_static_variable_init` | inline | ~15 |
| 32 | 0x20 | `vla_dimension` | inline | ~10 |
| 33 | 0x21 | `overriding_virtual_func` | inline | ~15 |
| 35 | 0x23 | `derivation_path` | inline | ~10 |
| 36 | 0x24 | `base_class` | inline | ~25 |
| 39 | 0x27 | `class_info` | `sub_5F4030` (`display_class_supplement`) | 366 |
| 41 | 0x29 | `constructor_init` | inline | ~15 |
| 42 | 0x2A | `asm_entry` | inline | ~25 |
| 43 | 0x2B | `asm_operand` | inline | ~15 |
| 44 | 0x2C | `asm_clobber` | inline | ~10 |
| 50 | 0x32 | `source_sequence_entry` | inline | ~15 |
| 51 | 0x33 | `full_entity_decl_info` | inline | ~15 |
| 52 | 0x34 | `instantiation_directive` | inline | ~10 |
| 53 | 0x35 | `src_seq_sublist` | inline | ~10 |
| 54 | 0x36 | `explicit_instantiation_decl` | inline | ~10 |
| 55 | 0x37 | `orphaned_entities` | inline | ~10 |
| 56 | 0x38 | `hidden_name` | inline | ~10 |
| 57 | 0x39 | `pragma` | inline | ~20 |
| 58 | 0x3A | `template` | inline | ~20 |
| 59 | 0x3B | `template_decl` | inline | ~15 |
| 60 | 0x3C | `requires_clause` | inline | ~10 |
| 61 | 0x3D | `template_param` | inline | ~15 |
| 62 | 0x3E | `name_reference` | `sub_5EBC60` (`display_name_reference`) | 84 |
| 63 | 0x3F | `name_qualifier` | inline | ~15 |
| 64 | 0x40 | `seq_number_lookup` | inline | ~10 |
| 65 | 0x41 | `local_expr_node_ref` | inline | ~10 |
| 66 | 0x42 | `static_assert` | inline | ~10 |
| 67 | 0x43 | `linkage_spec` | inline | ~10 |
| 68 | 0x44 | `scope_ref` | inline | ~10 |
| 70 | 0x46 | `lambda` | inline | ~15 |
| 71 | 0x47 | `lambda_capture` | inline | ~15 |
| 72 | 0x48 | `attribute` | inline | ~20 |
| 73 | 0x49 | `attribute_argument` | inline | ~10 |
| 74 | 0x4A | `attribute_group` | inline | ~10 |
| 79 | 0x4F | `template_info` | inline | ~15 |
| 80 | 0x50 | `subobject_path` | inline | ~10 |
| 82 | 0x52 | `module_info` | inline | ~10 |
| 83 | 0x53 | `module_decl` | inline | ~10 |

## Per-Kind Display Functions

### source_file_entry (Kind 1)

Displayed inline in the dispatcher. Fields:

| Field | Type | Notes |
|---|---|---|
| `file_name` | string | Short file name |
| `full_name` | string | Full path |
| `name_as_written` | string | As-written in `#include` |
| `first_seq_number` | integer | First source sequence number in this file |
| `last_seq_number` | integer | Last source sequence number |
| `first_line_number` | integer | First line number |
| `child_files` | IL pointer list | Included files |
| `is_implicit_include` | bool | Implicitly included |
| `is_include_file` | bool | Is an `#include`d file (not the primary TU) |
| `top_level_file` | bool | Top-level compilation unit |

### source_corresp (Shared Prefix)

All named entities (variable, routine, type, field, label, namespace, template_param) share a `source_corresp` sub-record, printed by `display_source_corresp` (`sub_5EDF40`, 170 lines). This is the first thing displayed for each such entity:

```
source_corresp:
  name:                    foo
  unmangled_name_or_mangled_encoding: _Z3foov
  decl_position.seq:       42
  decl_position.column:    5
  name_references:         name_reference@7f3a...
  is_class_member:         TRUE
  access:                  public
  parent_scope:            file-scope scope@7f3a...
  enclosing_routine:       NULL
  referenced:              TRUE
  needed:                  TRUE
  name_linkage:            external
```

Fields displayed by `display_source_corresp`:

| Field | Type | Lookup table |
|---|---|---|
| `name` | string | Direct string |
| `unmangled_name_or_mangled_encoding` | string | Direct string |
| `decl_position` | position | seq + column sub-fields |
| `name_references` | IL pointer | name_reference entry |
| `is_class_member` | bool | -- |
| `access` | enum | `off_A6F760` (4 entries: public/protected/private/none) |
| `parent_scope` | IL pointer | Scope entry |
| `enclosing_routine` | IL pointer | Routine entry |
| `referenced` | bool | -- |
| `needed` | bool | -- |
| `is_local_to_function` | bool | -- |
| `parent_via_local_scope_ref` | IL pointer | -- |
| `name_linkage` | enum | `off_E6E040` (none/internal/external/C/C++) |
| `has_associated_pragma` | bool | -- |
| `is_decl_after_first_in_comma_list` | bool | -- |
| `copied_from_secondary_trans_unit` | bool | -- |
| `same_name_as_external_entity_in_secondary_trans_unit` | bool | -- |
| `member_of_unknown_base` | bool | -- |
| `qualified_unknown_base_member` | bool | -- |
| `marked_as_gnu_extension` | bool | -- |
| `is_deprecated_or_unavailable` | bool | -- |
| `externalized` | bool | -- |
| `maybe_unused` | bool | `[[maybe_unused]]` attribute |
| `source_sequence_entry` | IL pointer | -- |
| `attributes` | IL pointer | Attribute list |

### type (Kind 6)

`display_type` (`sub_5F06B0`, 1,033 lines) handles all 22 type kinds. After calling `display_source_corresp`, it prints common type fields then switches on the type kind byte at offset +132:

**Common type fields:**

| Field | Lookup table |
|---|---|
| `next` | IL pointer |
| `based_types` | Linked list, kind from `off_A6F420` (6 entries) |
| `size` | Integer |
| `alignment` | Integer |
| `incomplete` | bool |
| `used_in_exception_or_rtti` | bool |
| `declared_in_function_prototype` | bool |
| `alignment_set_explicitly` | bool |
| `variables_are_implicitly_referenced` | bool |
| `may_alias` | bool |
| `autonomous_primary_tag_decl` | bool |
| `is_builtin_va_list` | bool |
| `is_builtin_va_list_from_cstdarg` | bool |
| `has_gnu_abi_tag_attribute` | bool |
| `in_gnu_abi_tag_namespace` | bool |
| `type_kind` | Enum from `off_A6FE40` (22 entries) |

**Type kind switch (offset +132):**

| Kind | Name | Key sub-fields |
|---|---|---|
| 2 | integer | `int_kind` (via `sub_5F9110`), `explicitly_signed`, `wchar_t_type`, `char8_t_type`, `char16_t_type`, `char32_t_type`, `bool_type`; for enums: `is_scoped_enum`, `packed`, `originally_unnamed`, `is_template_enum`, `ELF_visibility`, `base_type`, `assoc_template` |
| 3/4/5 | float/double/ldouble | `float_kind` (via `sub_5F93D0`) |
| 6 | pointer | `type_pointed_to`, `is_reference`, `is_rvalue_reference` |
| 7 | function | `return_type`, `param_type_list`, `assoc_routine`, `has_ellipsis`, `prototyped`, `trailing_return_type`, `value_returned_by_cctor`, `does_not_return`, `result_should_be_used`, `is_const`, `explicit_calling_convention`, `calling_convention` (from `off_E6CDA0`), `this_class`, `qualifiers`, `ref_qualifiers`, `prototype_scope`, `exception_specification` |
| 8 | array | `element_type`, `qualifiers`, `is_static`, `is_variable_size_array`, `is_vla`, `element_count`, `bound_constant` |
| 9/10/11 | class/struct/union | `field_list`, `extra_info` (class supplement via `sub_5F4030`), `final`, `abstract`, `any_virtual_base_classes`, `any_virtual_functions`, `originally_unnamed`, `is_template_class`, `is_specialized`, `is_empty_class`, `is_packed`, `max_member_alignment` |
| 12 | typeref | `typeref_type`, `template_arg_list`, `assoc_template`, `typeref_kind` (from `off_A6F640`, 28 entries), `qualifiers`, `predeclared`, `has_variably_modified_type`, `is_nonreal` |
| 13 | member pointer | `class_of_which_a_member`, `type` |
| 14 | template param | `kind` (tptk_param/tptk_member/tptk_unknown), `is_pack`, `is_generic_param`, `is_auto_param`, `class_type`, `coordinates` |
| 15 | vector | `element_type`, `size_constant`, `is_boolean_vector`, `vector_kind` |
| 16 | tuple | `element_type`, `tuple_elements` |

### variable (Kind 7)

`display_variable` (`sub_5EE500`, 614 lines) is one of the most field-heavy display functions. After `display_source_corresp`, it prints:

| Field | Lookup table / Notes |
|---|---|
| `next` | IL pointer |
| `type` | IL pointer |
| `storage_class` | `off_A6FE00` (7 entries: none/auto/register/static/extern/mutable/thread_local) |
| `declared_storage_class` | Same table |
| `asm_name` or `reg` | `off_A6F480` (53 register kind entries) |
| `alignment` | Integer |
| `ELF_visibility` | `off_A6F720` (5 entries) |
| `init_priority` | Integer |
| `cleanup_routine` | IL pointer |
| `container` / `bindings` | Selected by bits at offset +162 |
| `section` | String (ELF section name) |
| `aliased_variable` | IL pointer |
| `declared_type` | IL pointer |
| `template_info` | IL pointer |

**CUDA-specific variable fields:**

| Field | Notes |
|---|---|
| `shared` | `__shared__` memory space |
| `constant` | `__constant__` memory space |
| `device` | `__device__` memory space |

**Boolean flags** (approximately 50 flags spanning bytes 144--208):

`is_weak`, `is_weakref`, `is_gnu_alias`, `has_gnu_used_attribute`, `has_gnu_abi_tag_attribute`, `is_not_common`, `is_common`, `has_internal_linkage_attribute`, `asm_name_is_valid`, `used`, `address_taken`, `is_parameter`, `is_parameter_pack`, `is_pack_element`, `is_enhanced_for_iterator`, `initializer_in_class`, `constant_valued`, `is_thread_local`, `extends_lifetime`, `is_template_param_object`, `compiler_generated`, `is_in_class_specialization`, `is_handler_param`, `is_this_parameter`, `referenced_non_locally`, `modified_within_try_block`, `is_template_variable`, `is_prototype_instantiation`, `is_nonreal`, `is_specialized`, `specialized_with_old_syntax`, `explicit_instantiation`, `class_explicitly_instantiated`, `explicit_do_not_instantiate`, `param_value_has_been_changed`, `param_used_more_than_once`, `is_anonymous_parent_object`, `is_member_constant`, `is_constexpr`, `declared_constinit`, `is_inline`, `suppress_inline_definition`, `superseded_external`, `has_variably_modified_type`, `is_vla`, `is_compound_literal`, `has_explicit_initializer`, `has_parenthesized_initializer`, `has_direct_braced_initializer`, `has_flexible_array_initializer`, `declared_with_auto_type_specifier`, `declared_with_decltype_auto`, `declared_with_class_template_placeholder`

### routine (Kind 11)

`display_routine` (`sub_5EF1A0`, ~1,160 lines) is the single largest per-kind display function. After `display_source_corresp`:

| Field | Lookup table / Notes |
|---|---|
| `next` | IL pointer |
| `type` | IL pointer (function type) |
| `function_def_number` | Integer |
| `memory_region` | Integer (region index for function body) |
| `storage_class` | `off_A6FE00` (7 entries) |
| `declared_storage_class` | Same table |
| `special_kind` | `off_A6FC00` (13 entries: none/constructor/destructor/conversion/operator/lambda_call_operator/...) |
| `opname_kind` | `off_A6FC80` (47 entries) |
| `builtin_function_kind` | Integer |
| `ELF_visibility` | `off_A6F720` |
| `virtual_function_number` | Integer |
| `constexpr_intrinsic_number` | Integer |
| `section` | String |
| `aliased_routine` | IL pointer |
| `inline_partner` | IL pointer |
| `ctor_priority` / `dtor_priority` | Integer |
| `asm_name` | String |
| `declared_type` | IL pointer |
| `generating_using_decl` | IL pointer |
| `befriending_classes` | IL pointer list |
| `assoc_template` | IL pointer |
| `template_arg_list` | Via `display_template_arg_list` |

**CUDA-specific routine flags** (byte 182):

| Flag | Bit | Meaning |
|---|---|---|
| `nvvm_intrinsic` | bit 4 | NVVM intrinsic function |
| `device` | bit 5 | `__device__` execution space |
| `global` | bit 6 | `__global__` execution space |
| `host` | bit 4 (byte 183) | `__host__` execution space |

**C99-specific fields** (displayed when `dword_126EBA8 == 1` and `std_version > 199900`):

`fp_contract`, `fenv_access`, `cx_limited_range` -- pragma state values from `off_A6F460` (4 entries).

**Boolean flags** (approximately 60 flags spanning bytes 176--191):

`address_taken`, `is_virtual`, `overrides_base_member`, `pure_virtual`, `final`, `override`, `covariant_return_virtual_override`, `is_inline`, `is_declared_constexpr`, `is_constexpr`, `is_constexpr_intrinsic`, `compiler_generated`, `defined`, `called`, `is_explicit_constructor`, `is_explicit_conversion_function`, `is_trivial_default_constructor`, `is_trivial_copy_function`, `is_trivial_destructor`, `is_initializer_list_ctor`, `is_delegating_ctor`, `is_inheriting_ctor`, `assignment_to_this_done`, `is_prototype_instantiation`, `is_template_function`, `is_specialized`, `specialized_with_old_syntax`, `explicit_instantiation`, `class_explicitly_instantiated`, `explicit_do_not_instantiate`, `has_nodiscard_attribute`, `never_throws`, `is_in_class_specialization`, `never_inline`, `is_pure`, `is_initialization_routine`, `is_finalization_routine`, `is_weak`, `is_weakref`, `is_gnu_alias`, `is_ifunc`, `has_gnu_used_attribute`, `has_gnu_abi_tag_attribute`, `in_gnu_abi_tag_namespace`, `allocates_memory`, `no_instrument_function`, `no_check_memory_usage`, `always_inline`, `gnu_c89_inline`, `implicit_alias`, `has_internal_linkage_attribute`, `contains_try_block`, `contains_local_class_type`, `superseded_external`, `defined_in_friend_decl`, `contains_statement_expression`, `inline_in_class_definition`, `is_lambda_body`, `is_defaulted`, `is_deleted`, `contains_local_static_variable`, `is_raw_literal_operator`, `is_tls_init_routine`, `has_deducible_return_type`, `has_deduced_return_type`, `contains_generic_lambda`, `is_coroutine`, `is_top_level_in_mem_region`, `friend_defined_in_instantiation`, `is_ineligible`, `definition_needed`, `defined_outside_of_parent`, `trailing_requires_clause`

### expr_node (Kind 13)

`display_expr_node` (`sub_5ECFE0`, 534 lines) handles 36 expression node kinds. Common expression fields are printed first:

| Field | Notes |
|---|---|
| `type` | IL pointer (expression type) |
| `orig_lvalue_type` | IL pointer |
| `next` | IL pointer |
| `is_lvalue` | bool |
| `is_xvalue` | bool |
| `result_is_not_used` | bool |
| `is_pack_expansion` | bool |
| `is_parenthesized` | bool |
| `compiler_generated` | bool |
| `volatile_fetch` | bool |
| `do_not_interpret` | bool |
| `type_definition_needed` | bool |

**Expression kind switch (offset +24):**

| Kind | Name | Key sub-fields |
|---|---|---|
| 0 | `enk_error` | (none) |
| 1 | `enk_operation` | `operation.kind` from `off_A6F840` (120 operator kinds), `operation.type_kind` from `off_A6FE40` (22 type kinds), 20+ boolean flags for cast semantics, ADL suppression, virtual call properties, evaluation order |
| 2 | `enk_constant` | Constant value reference |
| 3 | `enk_variable` | Variable reference |
| 4 | `enk_field` | Field access |
| 5 | `enk_temp_init` | Temporary initialization |
| 6 | `enk_lambda` | Lambda expression |
| 7 | `enk_new_delete` | `is_new`, `placement_new`, `aligned_version`, `array_delete`, `global_new_or_delete`, `deducible_type`, `type`, `routine`, `arg`, `dynamic_init`, `number_of_elements` |
| 8 | `enk_throw` | Throw expression |
| 9 | `enk_condition` | Conditional expression |
| 10 | `enk_object_lifetime` | Object lifetime tracking |
| 11 | `enk_typeid` | `typeid` expression |
| 12 | `enk_sizeof` | `sizeof` expression |
| 13 | `enk_sizeof_pack` | `sizeof...` (pack) |
| 14 | `enk_alignof` | `alignof` expression |
| 15 | `enk_datasizeof` | `__datasizeof` |
| 16 | `enk_address_of_ellipsis` | Address of `...` |
| 17 | `enk_statement` | Statement expression |
| 18 | `enk_reuse_value` | Value reuse |
| 19 | `enk_routine` | Function reference |
| 20 | `enk_type_operand` | Type operand |
| 21 | `enk_builtin_operation` | Built-in op from `off_E6C5A0` |
| 22 | `enk_param_ref` | Parameter reference |
| 23 | `enk_braced_init_list` | Braced initializer |
| 24 | `enk_c11_generic` | `_Generic` selection |
| 25 | `enk_builtin_choose_expr` | `__builtin_choose_expr` |
| 26 | `enk_yield` | `co_yield` |
| 27 | `enk_await` | `co_await` |
| 28 | `enk_fold` | Fold expression |
| 29 | `enk_initializer` | Initializer |
| 30 | `enk_concept_id` | Concept ID |
| 31 | `enk_requires` | `requires` expression |
| 32 | `enk_compound_req` | Compound requirement |
| 33 | `enk_nested_req` | Nested requirement |
| 34 | `enk_const_eval_deferred` | Consteval deferred |
| 35 | `enk_template_name` | Template name |

Every expression case ends with `dump_source_position("position", ...)` to record the source location.

### statement (Kind 21)

`display_statement` (`sub_5EC600`, 328 lines) handles 26 statement kinds. Common fields first:

| Field | Notes |
|---|---|
| `position` | Source position |
| `next` | IL pointer |
| `parent` | IL pointer (enclosing scope/block) |
| `attributes` | IL pointer |
| `has_associated_pragma` | bool |
| `is_initialization_guard` | bool |
| `is_lowering_boilerplate` | bool |
| `is_fallthrough_statement` | bool |
| `is_likely` | bool |
| `is_unlikely` | bool |

**Statement kind switch (offset +32):**

| Kind | Name | Key sub-fields |
|---|---|---|
| 0 | `stmk_expr` | Expression statement |
| 1 | `stmk_if` | if |
| 2 | `stmk_constexpr_if` | `if constexpr` |
| 3 | `stmk_if_consteval` | `if consteval` (C++23) |
| 4 | `stmk_if_not_consteval` | `if !consteval` |
| 5 | `stmk_while` | while loop |
| 6 | `stmk_goto` | goto |
| 7 | `stmk_label` | label |
| 8 | `stmk_return` | return |
| 9 | `stmk_coroutine` | Coroutine body (see below) |
| 10 | `stmk_coroutine_return` | Coroutine return |
| 11 | `stmk_block` | Block/compound: `statements`, `final_position`, `assoc_scope`, `lifetime`, `end_of_block_reachable`, `is_statement_expression` |
| 12 | `stmk_end_test_while` | do-while |
| 13 | `stmk_for` | for loop |
| 14 | `stmk_range_based_for` | Range-for: `iterator`, `range`, `begin`, `end`, `ne_call_expr`, `incr_call_expr` |
| 15 | `stmk_switch_case` | switch case |
| 16 | `stmk_switch` | switch |
| 17 | `stmk_init` | Initialization |
| 18 | `stmk_asm` | Inline assembly |
| 19 | `stmk_try_block` | try block |
| 20 | `stmk_decl` | Declaration |
| 21 | `stmk_set_vla_size` | VLA size |
| 22 | `stmk_vla_decl` | VLA declaration |
| 23 | `stmk_assigned_goto` | Computed goto |
| 24 | `stmk_empty` | Empty statement |
| 25 | `stmk_stmt_expr_result` | Statement expression result |

**Coroutine statement (case 9)** displays the full C++20 coroutine lowering structure:

```
traits, handle, promise, init_await_resume, this_param_copy,
paramter_copies, final_suspend_label, initial_suspend_call,
final_suspend_call, unhandled_exception_call, get_return_object_call,
new_routine, delete_routine, ...
```

The field name `"paramter_copies"` (missing the 'e' in "parameter") is a typo preserved verbatim from the EDG source. This confirms the display strings originate from Edison Design Group's own `il_to_str.c` -- a reimplementation would spell it correctly.

### scope (Kind 23)

`display_scope` (`sub_5F2140`, 177 lines) handles 9 scope kinds:

| Kind | Name | Extra fields |
|---|---|---|
| 0 | `sck_file` | Top-level file scope |
| 1 | `sck_func_prototype` | Function prototype scope |
| 2 | `sck_block` | `assoc_handler` |
| 3 | `sck_namespace` | `assoc_namespace` |
| 6 | `sck_class_struct_union` | `assoc_type` |
| 8 | `sck_template_declaration` | Template declaration scope |
| 15 | `sck_condition` | `assoc_statement` |
| 16 | `sck_enum` | `assoc_type` |
| 17 | `sck_function` | `routine.ptr`, `parameters`, `constructor_inits`, `lifetime_of_local_static_vars`, `this_param_variable`, `return_value_variable` |

**Common scope fields:** `next`, `parent`, `kind`

**Boolean flags:** `do_not_free_memory_region`, `is_constexpr_routine`, `is_stmt_expr_block`, `is_placeholder_scope`, `needed_walk_done`

**Child entity lists:** `assoc_block`, `lifetime`, `constants`, `types`, `variables`, `nonstatic_variables`, `labels`, `routines`, `asm_entries`, `scopes`

**Conditional lists** (controlled by bitmask tests on scope kind):

```c
// Bitmask 0x20044 = bits 2+6+17 = sck_block + sck_class_struct_union + sck_function
// Bitmask 0x9     = bits 0+3    = sck_file + sck_namespace

if ((1LL << kind) & 0x20044) {
    // display: namespaces, using_declarations, using_directives
}
if ((1LL << kind) & 0x9) {
    // display: namespaces, using_declarations, using_directives
}
// Also: dynamic_inits, local_static_variable_inits (function/block scopes)
//       expr_node_refs, scope_refs, vla_dimensions (function scope + C mode)
//       pragmas, hidden_names, templates, source_sequence_list, src_seq_sublist_list
```

### constant (Kind 2)

`display_constant` (`sub_5F2720`, 605 lines) handles 16 constant kinds. After `display_source_corresp`, common fields include `next`, `type`, `orig_type`, `expr`, and approximately 25 boolean flags.

**Constant kind switch (offset +148):**

| Kind | Name | Key sub-fields |
|---|---|---|
| 0 | `ck_error` | (none) |
| 1 | `ck_integer` | Integer value via `sub_602F20` |
| 2 | `ck_string` | `character_kind` (char/wchar_t/char8_t/char16_t/char32_t), `length`, `literal_kind` (see below) |
| 3 | `ck_float` | Float value via `sub_5FCAF0` |
| 4 | `ck_complex` | Complex value |
| 5 | `ck_imaginary` | Imaginary value |
| 6 | `ck_address` | Sub-kind: abk_routine/variable/constant/temporary/uuidof/typeid/label; `subobject_path`, `offset` |
| 7 | `ck_ptr_to_member` | `casting_base_class`, `name_reference`, `cast_to_base`, `is_function_ptr` |
| 8 | `ck_label_difference` | `from_address`, `to_address` |
| 9 | `ck_dynamic_init` | `dynamic_init` pointer |
| 10 | `ck_aggregate` | `first_constant`, `last_constant`, `has_dynamic_init_component` |
| 11 | `ck_init_repeat` | `constant`, `count`, `multidimensional_aggr_tail_not_repeated` |
| 12 | `ck_template_param` | Sub-kinds: tpck_param/expression/member/unknown_function/address/sizeof/datasizeof/alignof/uuidof/typeid/noexcept/template_ref/integer_pack/destructor |
| 13 | `ck_designator` | `is_field_designator`, `is_generic`, `uses_direct_init_syntax` |
| 14 | `ck_void` | (none) |
| 15 | `ck_reflection` | `entity`, `local_scope_number` |

### dynamic_init (Kind 30)

`display_dynamic_init` (`sub_5F37F0`, 248 lines) handles 9 dynamic initialization kinds:

| Kind | Name | Key sub-fields |
|---|---|---|
| 0 | `dik_none` | (none) |
| 1 | `dik_zero` | Zero-initialization |
| 2 | `dik_constant` | Constant initialization |
| 3 | `dik_expression` | Expression initialization |
| 4 | `dik_class_result_via_ctor` | Class result through constructor |
| 5 | `dik_constructor` | `routine`, `args`, `is_copy_constructor_with_implied_source`, `is_implicit_copy_for_copy_initialization`, `value_initialization` |
| 6 | `dik_nonconstant_aggregate` | Non-constant aggregate |
| 7 | `dik_bitwise_copy` | `source` |
| 8 | `dik_lambda` | `lambda`, `constant`, `non_constant` |

Common fields: `next`, `variable`, `destructor`, `lifetime`, `next_in_destruction_list`, `unordered`, `init_expr_lifetime`, and approximately 20 boolean flags including `static_temp`, `follows_an_exec_statement`, `inside_conditional_expression`, `has_temporary_lifetime`, `is_constructor_init`, `is_freeing_of_storage_on_exception`, `overlaps_temps_in_inner_lifetime`, `is_reused_value`, `is_creation_of_initializer_list_object`, `master_entry`.

### class_info (Kind 39)

`display_class_type_supplement` (`sub_5F4030`, 366 lines) is not dispatched directly from the kind table but called by `display_type` when the type kind is class/struct/union (kinds 9/10/11). It prints the class supplement record:

| Field | Notes |
|---|---|
| `base_classes` | IL pointer list |
| `direct_base_classes` | IL pointer list |
| `preorder_base_classes` | IL pointer list |
| `primary_base_class` | IL pointer |
| `size_without_virtual_base_classes` | Integer |
| `alignment_without_virtual_base_classes` | Integer |
| `highest_virtual_function_number` | Integer |
| `virtual_function_info_offset` | Integer |
| `virtual_function_info_base_class` | IL pointer |
| `ELF_visibility` | `off_A6F720` |
| `is_lambda_closure_class` | bool |
| `is_generic_lambda_closure_class` | bool |
| `has_lambda_conversion_function` | bool |
| `is_initializer_list` | bool |
| `has_initializer_list_ctor` | bool |
| `has_anonymous_union_member` | bool |
| `anonymous_union_kind` | enum (auk_none/auk_variable/auk_field) |
| `is_va_list_tag` | bool |
| `has_nodiscard_attribute` | bool |
| `has_field_initializer` | bool |
| `removed_from_il` | bool |
| `contains_error` | bool |
| `befriending_classes` | Linked list (checks kind bytes 9/10/11 for class/struct/union) |
| `friend_routines` | IL pointer list |
| `friend_classes` | IL pointer list |
| `assoc_scope` | IL pointer |
| `assoc_template` | IL pointer |
| `template_arg_list` | Via `display_template_arg_list` |
| `lambda_parent.variable` / `.field` / `.routine` | Selected by bits in byte 86 |
| `proxy_of_type` | IL pointer |

## Formatting Infrastructure

### 25-Column Field Labels

`dump_field_label` (`sub_5EB2A0`, 22 lines) is the universal field label formatter. It prints `"field_name:"` then pads with spaces to column 25. If the label plus colon exceeds 24 characters, it prints a newline first to avoid misalignment:

```
storage_class:           static
alignment:               16
is_constexpr:            TRUE
```

This produces the consistent columnar output visible in all IL dumps.

### Boolean Fields

`dump_field_bool` (`sub_5EB450`, 25 lines) prints a label and `"TRUE"` or `"FALSE"`:

```
is_virtual:              TRUE
pure_virtual:            FALSE
```

### Source Position Fields

`dump_source_position` (`sub_5EB4E0`, 82 lines) prints position as two sub-fields when the position is non-zero (seq != 0 or column != 0):

```
position.seq:            42
position.column:         5
```

Reads a 32-bit sequence number at `*position` and a 16-bit column at `*(position + 4)`.

### IL Pointer Annotations

`dump_il_entity_pointer` (`sub_5EB8B0`, 99 lines) is the most comprehensive pointer formatter. For each IL entity pointer, it prints:

1. **Scope prefix**: `"file-scope"` or `"func-scope"` (from bit 0 of the entry prefix byte at `entry_ptr - 8`)
2. **Kind name**: from `off_E6DD80[kind_byte]`
3. **Hex address**: `@%lx`
4. **Entity name** (kind-dependent):
   - Kinds with name at offset +8 (bitmask `0x2000000010001984`): prints the name string
   - Kind 12 (label): prints `"label "` prefix + name
   - Kind 6 (type): calls qualified name formatter
   - Kind 2 (constant): calls type display
   - Kind 0x40 (seq_number_lookup): prints qualified name from offset +0
   - Kind with bit 36 set: prints qualified name from offset +40, plus `"in"` context from +56

```
primary_source_file:     file-scope source_file_entry@7f3a4b100020 "test.cu"
main_routine:            file-scope routine@7f3a4b200100 "main"
```

The variant `dump_il_string_pointer` (`sub_5EB670`) prints the same format but includes the string value from the pointed-to entry. A scope mismatch (e.g., function-scope pointer found during file-scope display) triggers a `"**NON FILE SCOPE PTR**"` warning.

### Entity List Display

`display_entity_list` (`sub_5EC450`, 87 lines) walks a linked list of entity pointers and prints each with scope/kind/address annotations:

```
entities:                file-scope variable@7f3a... "x"
                         func-scope variable@7f3a... "y"
```

It follows the `next` link at offset 0 of each list node until NULL.

### String Literal Display

`dump_string_value` (`sub_5EB300`, 41 lines) prints string values with proper escape handling:

- NULL pointers print `"NULL"`
- Non-printable characters are printed as octal escapes (`\OOO`)
- Backslash and double-quote are backslash-escaped (`\\`, `\"`)
- The octal mask width is controlled by `dword_126E49C` (CHAR_BIT equivalent, typically 8)

```
file_name:               "test.cu"
full_name:               "/home/user/project/test.cu"
```

### Float Constant Formatting

`form_float_constant` (`sub_5F7FD0`, 302 lines) handles float-to-string conversion with EDG-specific formatting. An assertion at line 6175 guards against buffer overflow (63-byte limit).

**Float kind suffixes:**

| Kind | Suffix | Type |
|---|---|---|
| 0 | (none) | double |
| 2 | `f`/`F` | float |
| 3 | `f32x` | extended float32 |
| 5 | `f64x` | extended float64 |
| 6 | `l`/`L` | long double |
| 7 | `w` | float128/wide |
| 8 | `q` | quad |
| 9 | `bf16` | bfloat16 |
| 10 | `f16` | float16 |
| 11 | `f32` | float32 |
| 12 | `f64` | float64 |
| 13 | `f128` | float128 |

**Special value handling:**

- NaN: `__builtin_nanf("")`, `__builtin_nan("")`, etc. (when compiler version > 30299)
- Infinity: `__builtin_huge_valf()` or `(__extension__ 0x1.0p<exp>f)`
- Division form: `(f/0.0f)` or `(f/(0,0.0f))` (C++ vs C modes, selected by `dword_126E1D8`/`dword_126E1E8`)
- User-defined literals: `(funcname("string_value"))` form

## Data Tables Referenced

The display subsystem relies on approximately 20 string-to-enum lookup tables in the `.rodata` segment:

| Address | Name | Entries | Used by |
|---|---|---|---|
| `off_A6F000` | `attr_arg_kind_names` | 6 | Attribute argument display |
| `off_A6F040` | `attr_location_names` | 24 | Attribute display |
| `off_A6F100` | `attr_family_names` | 5 | Attribute display |
| `off_A6F140` | `attr_kind_names` | 86 | Attribute display |
| `off_A6F3F0` | `class_kind_labels` | 3 | `befriending_classes` display |
| `off_A6F420` | `based_type_kind_names` | 6 | `display_type` based_types |
| `off_A6F460` | `pragma_state_names` | 4 | fp_contract/fenv_access/cx_limited_range |
| `off_A6F480` | `register_kind_names` | 53 | `display_variable` reg field |
| `off_A6F640` | `typeref_kind_names` | 28 | `display_type` typeref |
| `off_A6F720` | `elf_visibility_kind_names` | 5 | ELF visibility (all entity types) |
| `off_A6F760` | `access_specifier_names` | 4 | public/protected/private/none |
| `off_A6F840` | `expr_operator_kind_names` | 120 | `display_expr_node` operations |
| `off_A6FC00` | `special_function_kind_names` | 13 | `display_routine` special_kind |
| `off_A6FC80` | `operator_name_kind_names` | 47 | `display_routine` opname_kind |
| `off_A6FE00` | `storage_class_names` | 7 | Storage class (variable + routine) |
| `off_A6FE40` | `type_kind_names` | 22 | Type kind (all type displays) |
| `off_E6C5A0` | `builtin_operation_names` | varies | `display_expr_node` builtins |
| `off_E6CDA0` | `calling_convention_names` | varies | `display_type` calling conventions |
| `off_E6CDE0` | `pragma_kind_names` | varies | Pragma display |
| `off_E6CF40` | `asm_clobber_reg_names` | varies | Asm clobber display |
| `off_E6D240` | `token_kind_names` | varies | Fold expression / attribute_arg tokens |
| `off_E6DD80` | `il_entry_kind_names` | ~84 | All display functions (entry kind) |
| `off_E6E040` | `linkage_kind_names` | varies | Name linkage (source_corresp) |

All tables use the same bounds-checking pattern:

```c
const char *name = "**BAD STORAGE CLASS**";
if ((unsigned char)value <= 6u)
    name = storage_class_names[value];
puts(name);
```

Out-of-range values produce `"**BAD <KIND>**"` sentinel strings, which serve as diagnostic markers for corrupted IL.

## Global State

| Address | Name | Type | Purpose |
|---|---|---|---|
| `dword_126FA30` | `is_file_scope_region` | int | 1 during file-scope display, 0 during function-scope |
| `qword_126F980` | `output_callbacks` | function ptr | Output function (default: `sub_5EB290` = `fputs(s, stdout)`) |
| `byte_126FA16` | `display_active` | byte | Set to 1 during display, prevents re-entrant calls |
| `byte_126FA11` | `pcc_compat_shadow` | byte | Shadow of PCC compatibility mode during display |
| `dword_126EBA8` | `source_language` | int | 0 = C++, 1 = C |
| `dword_126EBAC` | `std_version` | int | C/C++ standard version number |
| `dword_126EC80` | `total_region_count` | int | Number of memory regions (1 = file scope only) |
| `qword_126EC88` | `region_table` | pointer array | Region index to memory block mapping |
| `qword_126EB90` | `scope_table` | pointer array | Region index to scope entry mapping |
| `qword_126EEE0` | `source_file_name` | string ptr | Name of the source file being compiled |

## Helper Functions (0x5F8000--0x6039E0)

The display subsystem includes approximately 50 additional helper functions in the address range beyond the main dispatchers:

| Address | Lines | Identity | Purpose |
|---|---|---|---|
| `sub_5F85E0` | 78 | `display_bool_field` | Boolean TRUE/FALSE output |
| `sub_5F8760` | 97 | `display_flags_word` | Flags word display |
| `sub_5F8910` | 88 | `display_type_qualifiers` | const/volatile/restrict qualifier flags |
| `sub_5F8A80` | 49 | `display_storage_class` | Storage class enum |
| `sub_5F8BD0` | 139 | `display_access_specifier` | Access with indentation |
| `sub_5F8DF0` | 103 | `display_linkage_kind` | Linkage kind enum |
| `sub_5F9040` | 28 | `init_output_context` | Initialize display callback state |
| `sub_5F9110` | 149 | `display_int_type_kind` | Integer type kind name |
| `sub_5F93D0` | 70 | `display_float_type_kind` | Float type kind name |
| `sub_5F9500` | 70 | `display_int_type_size` | Integer type size name |
| `sub_5F9650` | 99 | `display_qualifier_flags` | Full qualifier flags |
| `sub_5F9820` | 18 | `display_ref_qualifier` | `&` or `&&` |
| `sub_5F9860` | 91 | `display_calling_convention` | Calling convention from `off_E6CDA0` |
| `sub_5F99A0` | 115 | `display_attribute_target` | Attribute target kind |
| `sub_5F9BC0` | 20 | `display_asm_keyword` | `"asm"` or `"volatile"` |
| `sub_5F9C10` | 26 | `display_elaborated_type` | Elaborated type specifier |
| `sub_5F9CA0` | 50 | `display_struct_layout` | Structure layout padding mode |
| `sub_5F9D80` | 89 | `display_member_alignment` | Member alignment field |
| `sub_5F9F70` | 57 | `display_template_kind` | Template kind name |
| `sub_5FA0D0` | 283 | `display_template_arg_list` | Full template argument list |
| `sub_5FA660` | 127 | `display_constraint_expr` | Constraint expression (C++20) |
| `sub_5FA8F0` | 118 | `display_deduction_guide` | Deduction guide info |
| `sub_5FAB70` | 333 | `display_capture_list` | Lambda capture list |
| `sub_5FB270` | 556 | `display_expr_operator_name` | Expression operator name (120 kinds) |
| `sub_5FBCD0` | 571 | `display_expr_details` | Operator-specific expression details |
| `sub_5FCAF0` | 1,319 | `display_float_constant` | Float/complex/imaginary formatting |
| `sub_5FE7C0` | 55 | `display_expr_flag` | Expression flag display |
| `sub_5FE8B0` | 1,659 | `display_expr_operator` | Expression operator details (2nd largest) |
| `sub_600740` | 72 | `display_for_range` | Range-based for details |
| `sub_600870` | 171 | `display_coroutine_info` | Coroutine info (C++20) |
| `sub_600BF0` | 19 | `display_designated_init` | Designated initializer |
| `sub_600C50` | 107 | `display_attribute_entry` | Attribute entry |
| `sub_600E00` | 55 | `display_asm_operand` | Asm operand display |
| `sub_600EF0` | 76 | `display_asm_statement` | Asm statement details |
| `sub_600FF0` | 29 | `display_gcc_builtin_kind` | GCC built-in kind |
| `sub_601070` | 87 | `display_pragma_info` | Pragma info |
| `sub_6011F0` | 155 | `display_declspec_attribute` | `__declspec` attribute |
| `sub_601460` | 92 | `display_thread_local` | Thread-local info |
| `sub_6015A0` | 73 | `display_module_info` | Module info (C++20) |
| `sub_6016F0` | 197 | `display_concept_requires` | Concept/requires expression |
| `sub_601B10` | 48 | `display_pack_expansion` | Pack expansion info |
| `sub_601BE0` | 50 | `display_structured_binding` | Structured binding (C++17) |
| `sub_601CB0` | 562 | `display_additional_expr` | Additional expression info |
| `sub_6027D0` | 144 | `display_deduced_class` | Deduced class info |
| `sub_6029B0` | 190 | `display_decl_sequence` | Declaration sequence entry |
| `sub_602DC0` | 74 | `display_enum_underlying` | Enum underlying type |
| `sub_602F20` | 306 | `display_integer_constant` | Integer constant formatting |
| `sub_603670` | 134 | `display_vendor_attribute` | Vendor attribute details |
| `sub_6038F0` | 26 | `display_cleanup_handler` | Cleanup handler |
| `sub_6039E0` | 78 | `display_sequence_entry` | Last function in il_to_str region |

## The "paramter_copies" Typo

The coroutine statement display (case 9 in `display_statement`) prints the field label `"paramter_copies"` -- missing the 'e' in "parameter." This typo is present in the compiled binary's string table and originates from Edison Design Group's source code. It serves as strong provenance evidence: a clean-room reimplementation would not reproduce this exact spelling error, confirming that cudafe++ links genuine EDG `il_to_str.c` object code.

## Complete Call Graph

```
display_il_file (sub_5F7DF0) ─── TOP LEVEL
├── display_il_header (sub_5F76B0)
│   ├── init_output_context (sub_5F9040)
│   ├── dump_il_entity_pointer (sub_5EB8B0) ×30+ for header fields
│   ├── dump_field_bool (sub_5EB450) ×15+ for header booleans
│   ├── dump_string (sub_5EB790)
│   └── walk_file_scope_il (sub_60E4F0)
│       └── display_il_entry (sub_5F4930) ─── callback per entity
│
└── [loop over regions 2..N]
    └── walk_routine_scope_il (sub_610200)
        └── display_il_entry (sub_5F4930) ─── callback per entity

display_il_entry (sub_5F4930) ─── MAIN DISPATCHER
├── display_source_corresp (sub_5EDF40) ─── shared by named entities
├── display_statement (sub_5EC600) ─── case 0x15
│   ├── display_coroutine_info (sub_600870)
│   └── display_for_range (sub_600740)
├── display_expr_node (sub_5ECFE0) ─── case 0x0D
│   ├── display_expr_operator (sub_5FE8B0)
│   ├── display_expr_operator_name (sub_5FB270)
│   └── display_expr_details (sub_5FBCD0)
├── display_variable (sub_5EE500) ─── case 0x07
│   └── display_init_kind (sub_5EBB50)
├── display_routine (sub_5EF1A0) ─── case 0x0B
│   └── display_template_arg_list (sub_5EBF60 / sub_5FA0D0)
├── display_type (sub_5F06B0) ─── case 0x06
│   ├── display_class_supplement (sub_5F4030)
│   ├── display_int_type_kind (sub_5F9110)
│   └── display_float_type_kind (sub_5F93D0)
├── display_scope (sub_5F2140) ─── case 0x17
├── display_constant (sub_5F2720) ─── case 0x02
│   ├── display_integer_constant (sub_602F20)
│   └── display_float_constant (sub_5FCAF0)
├── display_dynamic_init (sub_5F37F0) ─── case 0x1E
├── display_name_reference (sub_5EBC60) ─── case 0x3E
└── display_entity_list (sub_5EC450) ─── multiple cases

display_single_entity (sub_5F7D50) ─── TARGETED DISPLAY
├── entity_lookup (sub_73D400)
├── resolve_entity (sub_7377D0)
├── get_entity_kind (sub_5C64C0)
├── init_output_context (sub_5F9040)
└── display_il_entry (sub_5F4930)
```

## Relationship to Other Subsystems

The IL display subsystem is read-only: it never modifies the IL graph. It shares the same entry walker functions used by the [IL Tree Walking](walking.md) framework (`walk_file_scope_il` = `sub_60E4F0`, `walk_routine_scope_il` = `sub_610200`) and the [Keep-in-IL](keep-in-il.md) mark phase, but passes `display_il_entry` as the callback instead of a transformation function.

The [IL Allocation](allocation.md) subsystem provides `dump_il_table_stats` (`sub_5E99D0`), which dumps allocation counters rather than IL content -- a complementary diagnostic activated separately.

The field offsets printed by the display functions serve as ground truth for the [IL Overview](overview.md) entry kind table and the [Entity Node Layout](../structs/entity-node.md) documentation.
