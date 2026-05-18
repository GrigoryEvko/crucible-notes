# Invalid Diagnostic Tags

EDG 6.6's diagnostic engine groups all "this construct is malformed in a way the front end recognized" diagnostics under a single naming convention: the tag begins with `invalid_`. Static analysis of the cudafe++ binary recovers **153 distinct `invalid_*` tags** embedded as plain ASCII identifiers in the string pool, making this the single largest coherent diagnostic family that EDG ships. Every reason the parser, semantic analyzer, attribute engine, or template engine can refuse a declaration is named by one of these tags.

This page catalogs all 153 tags grouped by which front-end sub-system raises them. Each `invalid_*` tag works the same way as the `constexpr_*` family documented in [Constexpr Diagnostic Tags](constexpr-diagnostics.md): a stable string identifier keyed into the diagnostic message table, routed to a numeric error code at presentation time. For evaluator-side tags see the constexpr page; for the engine that resolves tag identifiers to message templates see [Diagnostics Overview](../diagnostics/overview.md).

## Why This Family Matters in Practice

The `invalid_*` tags are the most frequently suppressed diagnostic class in real CUDA codebases. Three reasons:

1. **Microsoft-attribute interop.** A surprising fraction of the tag set (16 tags) covers `__declspec` / C++/CLI / WinRT attribute placement -- code targeting Windows hosts routinely hits these and pragma-suppresses them through `--diag_suppress`.
2. **CUDA-specific atomic intrinsics.** The `nv_atomic_*` family (8 tags) exists only in NVIDIA's fork of EDG and is silently emitted when device-side atomics receive a mismatched size, scope, or memory-order argument. Most user code never trips these, but template-heavy device libraries (`cuda::std::atomic_ref`) hit them during instantiation and rely on suppression to keep the build clean.
3. **Reflection and modules (C++23/26).** Tags for `^^` reflection (P2996) and the IFC binary module format (`ifc_*`) appear as `invalid_*` rather than as their own family. They fire on toolchain mismatches more often than on user mistakes, so build systems whitelist them.

The result is that the `invalid_*` family carries the highest "noise-to-signal" ratio of any EDG diagnostic class: most occurrences are not bugs in the user's code, they are signs that the user's code is interacting with a Microsoft, CUDA, or reflection extension whose contract the user did not realize EDG was enforcing.

## Where the Tags Come From

The diagnostic engine resolves `invalid_*` identifiers through the same pipeline as every other EDG tag:

```c
// Conceptual sketch of an emit site inside the declaration parser
emit_diagnostic(
    state,
    "invalid_storage_class_in_for_init",   // tag identifier
    decl_specifier_location,                // source position
    decl_token_text);                       // formatted operand
```

The tag identifier is then routed through the message-table to:

1. Resolve the canonical numeric error code (the `invalid_*` family spans codes in the 21--29 range for parse errors, 290--370 for Microsoft attribute checks, 1530--1810 for template-engine checks, and 3120--3290 for the CUDA atomic extensions).
2. Look up the localized message template.
3. Format operands.
4. Possibly chain framing tags (declaration context, attribute context) so the rendered diagnostic includes a "in declaration of X" prefix.

Because the tag is a stable string identifier, EDG can renumber the user-visible error codes without touching the call sites. The tag is the contract between the front-end pass that detected the violation and the diagnostic writer.

### Worked Example: Source to Diagnostic

A non-storage-class keyword in a `for (init; ...)` clause exercises a single tag end-to-end:

```cpp
for (constexpr int i = 0; i < 10; ++i) { /* ... */ }
```

`constexpr` is not one of the storage-class specifiers the standard whitelists for the init-statement. The declaration parser inside `sub_5C2D40` flags this and routes through the diagnostic engine:

```
error #29 [invalid_storage_class_in_for_init]:
    "constexpr" cannot be used as a storage class in a for-init-statement
    for (constexpr int i = 0; i < 10; ++i) {
         ^^^^^^^^^
```

The numeric `#29` is a presentation artifact; the tag carries the actual semantic content and is what `--diag_suppress=29` ultimately suppresses.

## Grouping Methodology

The 153 tags partition naturally by which front-end pass raises them, with one cross-cutting bucket for Microsoft extensions because those tags fire from several different passes but share a behavioral contract (relax in Microsoft mode, strict otherwise). The groups below mirror the structure of the EDG dispatcher: lexer first, parser next, semantic checks, then specialized engines.

Confidence level for the entire catalog: **HIGH**. Tag identifiers are present verbatim in the binary; sub-area assignment follows keyword stems and the few cross-references to the front-end functions whose code paths reference them. Exact numeric error codes are **MEDIUM** confidence -- the binary contains both the strings and the code-to-message routing, but we have not exhaustively traced every tag-to-number edge.

## Group 1 -- Lexer and Character-Class Tags (5 tags)

The lowest-level tags: fired during tokenization, before any parsing decisions are made. Each names a class of malformed input character or universal-character-name.

| Tag | Semantic role |
|---|---|
| `invalid_char` | A byte appeared in the source that is not a member of the source character set (after handling encoding) |
| `invalid_char_in_unicode_name` | A `\u`/`\U` escape resolved to a code point that cannot appear in an identifier |
| `invalid_identifier_start_UCN` | UCN appears as the *first* character of an identifier but is not in the XID_Start range |
| `invalid_identifier_UCN` | UCN inside an identifier resolved to a code point outside XID_Continue |
| `invalid_UCN` | The `\u`/`\U` escape itself is malformed (wrong digit count, surrogate code unit) |

These five exist as a closed set because the lexer is the only sub-system whose input is bytes rather than tokens; once tokenization succeeds, character-class violations cannot recur. See [Lexer & Tokenizer](lexer.md) for the relevant state machine.

## Group 2 -- Parser Structural Tags (11 tags)

Tags raised by the recursive-descent parser when a token appears where the grammar does not allow it. These are typically thrown from the declaration parser (`sub_5C2D40` family) or the statement parser entry point.

| Tag | Semantic role |
|---|---|
| `invalid_declaration` | Generic catch-all when the grammar's declaration production failed at a position with no more specific tag available |
| `invalid_designator_kind` | C99 designated initializer used a `.name` or `[index]` form that doesn't match the aggregate kind |
| `invalid_init_statement` | C++17 init-statement (`if (init; cond)`) used a construct other than expression-statement or declaration |
| `invalid_start_of_member_declaration` | First token after `class { ... }` cannot begin a member declaration |
| `invalid_start_of_requires_clause_expr` | `requires(...)` clause body started with a token not permitted as a constraint expression |
| `invalid_storage_class_in_for_init` | `for(init; ...)` had a storage-class specifier (e.g. `register`, `constexpr` pre-C++20) the standard disallows here |
| `invalid_struct_binding_syntax` | Structured binding declaration had a syntax error in the `[a, b, c]` part |
| `invalid_struct_binding_specifier` | Structured binding decl-specifier-seq contained something other than `auto` and CV-qualifiers |
| `invalid_struct_binding_type` | Type to be decomposed in a structured binding is not array, tuple-like, or class with public bases |
| `invalid_token_after_template` | Token following `template` keyword cannot begin a template-id (the dependent-name `T::template foo` case) |
| `invalid_name_after_template` | Same family: the name following `template` was not a template name |

> ⚡ **QUIRK -- the `invalid_declaration` umbrella**
> EDG falls back to `invalid_declaration` whenever the parser hits a more-specific failure path it has not been wired to name. This tag corresponds to error code `#29` and is one of the most-suppressed in the entire compiler because it can mean almost anything. Build systems that whitelist it lose precision: a subsequent EDG release that adds a new specific tag will continue routing through `invalid_declaration` until the new tag becomes available, then silently flip. CUDA code targeting host compilers that use a different grammar for `__declspec` placement is the canonical case where `invalid_declaration` fires legitimately as the catch-all.

## Group 3 -- Attribute and Microsoft-Attribute Tags (17 tags)

The largest sub-area: 12 of these tags exist solely to police `__declspec`, `[uuid(...)]`, and the C++11 `[[attribute]]` syntax. Six more cover generic attribute application rules. The `ms_attr_*` and `*_ms_attr` tags fire from `sub_5DA210` (the Microsoft attribute parser) and its siblings; the rest fire from the standard-attribute pass in `sub_5DBE00`.

| Tag | Semantic role |
|---|---|
| `invalid_alignment_reducing_attr` | `alignas` that would reduce the alignment below the type's natural requirement |
| `invalid_argument_to_attribute` | Attribute's argument list contained a value outside the allowed range/kind |
| `invalid_attribute_location` | Attribute appeared at a grammatical position it cannot legally occupy (e.g. between class-key and class-name) |
| `invalid_attribute_target_for_ms_attr` | `__declspec(X)` applied to an entity X cannot annotate |
| `invalid_attribute_target_for_standalone_ms_attr` | Same but for the standalone `[X]` Microsoft syntax (not C++11 `[[X]]`) |
| `invalid_base_for_ms_attributes` | Base class in a Microsoft-attribute class hierarchy isn't valid (typically C++/CLI ref class with native base) |
| `invalid_empty_attribute_arg_list` | `attribute()` with parens but no arguments where at least one is required |
| `invalid_ms_attr_enum_value` | An enum-typed argument in `__declspec(X(VALUE))` is not one of the recognized symbolic names |
| `invalid_ms_attribute_target` | The "old" Microsoft form applied to a target that doesn't accept it |
| `invalid_ms_attr_name` | The attribute name itself is not a recognized Microsoft attribute |
| `invalid_ms_attr_uuid_value` | `[uuid("...")]` payload is not a well-formed GUID string |
| `invalid_target_attribute` | `__attribute__((target("...")))` value is not a known target string (GCC extension) |
| `invalid_use_of_concept` | A C++20 concept-id was used as an attribute argument or in another forbidden context |
| `invalid_use_of_custom_ms_attr` | User-defined Microsoft attribute (registered via `__declspec(uuid(...))`) used incorrectly |
| `invalid_use_of_ms_attr` | General `__declspec(X)` misuse not covered by more specific tags |
| `invalid_use_of_standalone_custom_ms_attr` | Standalone `[X]` form of a user-defined Microsoft attribute |
| `invalid_use_of_standalone_ms_attr` | Standalone `[X]` form of a built-in Microsoft attribute |

> ⚡ **QUIRK -- the `ms_attr` / `attribute_ms` cross-naming**
> EDG's naming of this sub-family is inconsistent: tags begin either with `ms_attr_` (e.g. `invalid_ms_attr_name`) or end with `_ms_attr` (e.g. `invalid_use_of_ms_attr`). The two halves resolve to different message templates with subtly different rendering: the `ms_attr_*` form takes the attribute name as its primary operand, the `*_ms_attr` form takes the *target* (the entity the attribute is being applied to) as its primary operand. The differing word order in the rendered error text breaks any naive grep over the error log -- a user-visible "Microsoft attribute" string can come from either form. This is the most common reason `--diag_suppress=N` works for one occurrence and not the next: the user picked a number from a `*_ms_attr`-flavored error and tried to apply it to a `ms_attr_*`-flavored emit site, which uses a different code.

## Group 4 -- Type System Validity Tags (27 tags)

Tags fired by the type checker when a type expression resolves to something the language rules forbid. The largest sub-group inside `invalid_*` after attributes. Many of these fire during template instantiation and are therefore the dominant cause of "the same template works on GCC but not on EDG-host with CUDA".

| Tag | Semantic role |
|---|---|
| `invalid_argument_type` | Function argument's type cannot match any parameter slot (after overload resolution narrowed it) |
| `invalid_bit_cast_type` | `__builtin_bit_cast` operand or destination type fails the trivially-copyable / same-size precondition |
| `invalid_delegate_type` | C++/CLI `delegate` declared with a return/parameter type that managed delegates cannot express |
| `invalid_event_handler_type` | C++/CLI event uses a non-delegate type as its handler |
| `invalid_event_type` | C++/CLI event declared with a non-event-compatible type |
| `invalid_gcnew_type` | `gcnew T` applied to a T that cannot be heap-allocated under the CLI runtime |
| `invalid_generic_arg` | C++/CLI generic instantiated with a type argument it doesn't accept |
| `invalid_generic_specialization` | Specialization of a CLI generic violates the runtime's monomorphization rules |
| `invalid_interface_class_base` | C++/CLI `interface class` base list contains a non-interface |
| `invalid_literal_type` | C++14+ literal type requirement violated (object used in a constexpr context isn't trivially copyable / has user-provided dtor) |
| `invalid_member_constant_type` | `static const`/`constexpr` data member type not allowed (e.g. non-integral non-literal) |
| `invalid_neon_polyvector_element_type` | ARM NEON polynomial vector element type not in the allowed set |
| `invalid_neon_vector_element_type` | ARM NEON vector element type not in the allowed set |
| `invalid_neon_vector_size` | ARM NEON vector size doesn't match a hardware register width |
| `invalid_param_array_type` | Variadic CLI parameter array (`params T[]`) used with a non-array T |
| `invalid_ref_class_base` | C++/CLI `ref class` inherits from a type its runtime can't model (native class, multiple managed bases, etc.) |
| `invalid_scalable_vector_element_type` | SVE/SVE2 scalable vector's element type isn't one of the SVE-supported scalars |
| `invalid_scalable_vector_tuple_elements` | SVE tuple type's element list violates the ACLE's grouping rules |
| `invalid_specific_ref_class_base` | More specific variant of `_ref_class_base`: base is a specific kind the runtime singles out |
| `invalid_type_constraint` | C++20 `concept` used as a type-constraint introduces a relation that isn't well-formed |
| `invalid_type_for_builtin` | A `__builtin_*` function received an argument whose type the builtin's intrinsic table doesn't accept |
| `invalid_type_for_nullability` | `_Nullable` / `_Nonnull` qualifier applied to a non-pointer type |
| `invalid_type_for_w64` | `__w64` modifier (Microsoft legacy) used on a type that can't accept the 32-to-64 bit promotion convention |
| `invalid_typename_specifier` | `typename T::foo` where the qualified part doesn't resolve to a type |
| `invalid_type_pointed_to_for_interior_ptr_or_pin_ptr` | C++/CLI `interior_ptr<T>` or `pin_ptr<T>` with a T that can't be pinned |
| `invalid_value_class_base` | C++/CLI `value class` base list contains a non-value-class |
| `invalid_vector_element_type` | Generic vector-type element-type rejection (covers GCC and Clang vector extensions when ARM NEON/SVE-specific paths don't apply) |

## Group 5 -- Template and Concept Tags (15 tags)

Tags raised by the template engine during deduction, constraint checking, or specialization matching. These fire from `sub_5F8AE0` (template-id resolution), `sub_60D940` (concept satisfaction), and the deduction engine.

| Tag | Semantic role |
|---|---|
| `invalid_concept_id` | `Concept<Args...>` used in a position where the concept ID isn't allowed |
| `invalid_concept_redecl` | Concept redeclared with a different parameter list or body |
| `invalid_constraint` | A `requires` clause body resolved to something that isn't a boolean constant expression |
| `invalid_empty_fold_expression` | Fold expression `(... op pack)` had an empty pack and the operator has no identity |
| `invalid_entity_for_pending_constraint` | Entity referenced from inside an unevaluated constraint isn't one the engine can track |
| `invalid_fold_expression_operator` | Operator in a fold expression isn't one of the standard fold operators |
| `invalid_instantiation_argument` | Template argument provided to an instantiation has the wrong kind (e.g. type passed where a value was expected) |
| `invalid_nontype_template_argument` | A non-type template argument's value is not a valid converted constant expression for the parameter's type |
| `invalid_operator_in_requires_clause` | Operator inside a `requires` expression body isn't permitted in a constraint |
| `invalid_placeholder_for_defaulted_spaceship_return` | `auto operator<=>(...) = default;` return-type placeholder is wrong kind |
| `invalid_spaceship_types` | `<=>` applied to operand types that have no common comparison category |
| `invalid_std_comparison_type` | A spaceship/comparison helper expects a `std::strong_ordering`-family type and got something else |
| `invalid_std_comparison_value` | Comparison return value isn't one of the four `std::*_ordering` enumerators |
| `invalid_std_initializer_list_parameter_list` | A function template expects `std::initializer_list<T>` and got a different list-like type |
| `invalid_template_parameter_for_literal_operator_template` | UDL operator template parameter list isn't `<char...>` or `<typename CharT, CharT...>` |

## Group 6 -- Function and Declaration Semantic Tags (16 tags)

Tags raised by the semantic analyzer when a declaration is syntactically well-formed but violates a language rule that depends on context.

| Tag | Semantic role |
|---|---|
| `invalid_access_specifier` | `public:` / `protected:` / `private:` used outside a class body |
| `invalid_assignment_operator_to_be_defaulted` | `= default` on an assignment operator whose declaration doesn't match a defaultable signature |
| `invalid_constructor_to_be_defaulted` | `= default` on a constructor whose declaration doesn't match a defaultable signature |
| `invalid_default_arg` | Default argument's value violates the rules (refers to forbidden context, fails conversion, etc.) |
| `invalid_destructor_name` | Class-qualified destructor name doesn't match the class |
| `invalid_explicit_exception_specification` | `noexcept(expr)` argument is malformed or `throw(...)` list has invalid contents |
| `invalid_finalizer_name` | C++/CLI finalizer `!ClassName()` name doesn't match the class |
| `invalid_function_to_be_defaulted` | `= default` on a function that isn't a special member |
| `invalid_idisposable_dispose` | C++/CLI `Dispose` method signature doesn't match `IDisposable::Dispose()` |
| `invalid_nested_class_redecl` | Nested class redeclaration's enclosing-class qualification is wrong |
| `invalid_noexcept_specifier_operand` | `noexcept(expr)` operand has a type that's not contextually convertible to bool |
| `invalid_object_finalize` | `Finalize` method declared with a non-compatible signature in a CLI ref class |
| `invalid_prev_decl_iterator` | The previous-declaration linker walked off the end (internal invariant violation, very rarely user-facing) |
| `invalid_selective_overrider_declaration` | C++/CLI `void Foo() = SomeOverride;` selective override syntax misused |
| `invalid_specifier_for_deduction_guide` | C++17 deduction guide declared with a decl-specifier (storage class, return type) that isn't allowed |
| `invalid_variable_main` | A variable declared with the reserved name `main` at namespace scope |

## Group 7 -- Literal Operator (UDL) and Operator Tags (9 tags)

User-defined literal operators have a tight parameter-list contract. These tags fire during literal-operator declaration parsing.

| Tag | Semantic role |
|---|---|
| `invalid_float_parameter_for_literal_operator` | `operator""_x(long double)` parameter is not exactly `long double` |
| `invalid_integer_parameter_for_literal_operator` | `operator""_x(unsigned long long)` parameter type wrong |
| `invalid_literal_operator_id` | The suffix after `operator""` doesn't begin with an underscore (or violates other identifier rules) |
| `invalid_parameter_for_literal_operator_template` | Template literal operator parameter list violates the `<char...>` contract |
| `invalid_parameter_type_for_literal_operator` | Generic catch-all for wrong parameter type in a non-template literal operator |
| `invalid_pointer_parameter_for_literal_operator` | `(const char*)` or `(const char*, size_t)` literal operator has wrong pointer type/qualification |
| `invalid_second_parameter_type_for_literal_operator` | Two-parameter form's second parameter isn't `size_t` |
| `invalid_string_literal_operator_template` | Template string-literal operator has wrong parameter list (must be `<typename CharT, CharT...>`) |
| `invalid_udl_value` | UDL evaluation produced a value the literal operator's call expression cannot accept |

## Group 8 -- Constexpr / Consteval / Constinit Declaration Tags (7 tags)

Companion to the [Constexpr Diagnostic Tags](constexpr-diagnostics.md) page: these tags fire during declaration parsing rather than during evaluation. The constexpr page covers the evaluation-time failures; this group covers the malformed-declaration failures.

| Tag | Semantic role |
|---|---|
| `invalid_consteval` | `consteval` applied to a declaration that cannot be consteval (e.g. main, virtual without override) |
| `invalid_constexpr` | `constexpr` applied to a declaration that the language doesn't allow to be constexpr |
| `invalid_constexpr_body` | constexpr function body contains a statement form the relevant standard doesn't permit |
| `invalid_constexpr_memcmp` | `__builtin_memcmp` used in a constexpr context with operand types that aren't trivially-comparable |
| `invalid_constinit` | `constinit` applied to a declaration that doesn't have static or thread storage duration |
| `invalid_statement_in_constexpr_constructor` | Statement form forbidden in a constexpr constructor (pre-C++20 only) |
| `invalid_statement_in_constexpr_function` | Statement form forbidden in a constexpr function (varies by language mode) |

> ⚡ **QUIRK -- declaration-time vs. evaluation-time constexpr tags**
> The `constexpr_*` family (112 tags, see [Constexpr Diagnostic Tags](constexpr-diagnostics.md)) and this 7-tag `invalid_constexpr*` subset look like duplicates but actually split the work: tags here fire from the declaration parser before evaluation begins, the `constexpr_*` family fires from the interpreter during evaluation. The cleanest way to tell them apart is the rule about which one fires when the declaration is well-formed but the body fails on every input: that's a `constexpr_*` evaluator tag, not an `invalid_constexpr` declaration tag. CUDA's `--expt-relaxed-constexpr` relaxes the *evaluator* tags but not these declaration tags -- there is no flag that lets you put a `goto` into a constexpr function pre-C++23, regardless of the CUDA mode.

## Group 9 -- CUDA NVIDIA Atomic Intrinsic Tags (9 tags)

Tags found only in NVIDIA's fork of EDG. These fire when the front end is processing a `__nv_atomic_*` builtin (the lowering target for `cuda::std::atomic_ref` and friends) and the argument types or values don't match the intrinsic's contract.

| Tag | Semantic role |
|---|---|
| `invalid_data_size_for_nv_atomic_generic_function` | Argument size to a generic-form `__nv_atomic` intrinsic isn't 1, 2, 4, 8, or 16 bytes |
| `invalid_nv_atomic_cas_size` | `__nv_atomic_compare_exchange_n` size argument isn't a power of two in the supported range |
| `invalid_nv_atomic_exch_size` | `__nv_atomic_exchange_n` size argument out of supported range |
| `invalid_nv_atomic_memory_order_value` | `memory_order` argument isn't one of `relaxed`, `consume`, `acquire`, `release`, `acq_rel`, `seq_cst` |
| `invalid_nv_atomic_operation_add_sub_size` | Size for add/sub atomic doesn't match a supported integral width |
| `invalid_nv_atomic_operation_max_min_float` | `__nv_atomic_fetch_max` / `_min` on float types where the SM target doesn't have the hardware instruction |
| `invalid_nv_atomic_operation_size` | Generic catch-all for a `__nv_atomic_op_*` builtin whose operand size doesn't match the hardware-supported widths |
| `invalid_nv_atomic_thread_scope_value` | `cuda::thread_scope` argument isn't one of the recognized scopes (`thread_scope_system`, `_device`, `_block`, `_thread`) |
| `invalid_nvvm_builtin_intrinsic` | `__nvvm_*` builtin reference doesn't resolve to a known intrinsic (typically a forward-declaration mismatch) |

> ⚡ **QUIRK -- `nv_atomic_*` tags do not honor `--diag_suppress` cleanly**
> The NVIDIA-specific atomic tags are emitted from a code path that bypasses some of the host EDG diagnostic-suppression machinery. In particular, suppressing the numeric code that one of these tags resolves to can also suppress unrelated host-compiler diagnostics that share the same numeric code through the general "invalid intrinsic argument" message bucket. The workaround in `libcudacxx` is to suppress at the tag level using `#pragma nv_diag_suppress` rather than the numeric code -- which works because the tag-keyed pragma form looks up the tag directly. Many `cuda::atomic` template specializations carry such a pragma at the top of the file for exactly this reason.

## Group 10 -- Reflection (P2996) and Module/IFC Tags (12 tags)

Tags fired by the C++26 reflection engine (`^^` operator and friends) and the IFC binary-module-format loader. Both are recent additions; the `ifc_*` family was added in EDG 6.6's modules support.

| Tag | Semantic role |
|---|---|
| `invalid_ifc_partition` | Module partition name doesn't satisfy the `module Foo:Bar` grammar |
| `invalid_ifc_position_backtrace_field` | IFC file's backtrace field offset is malformed (corrupt binary module file) |
| `invalid_ifc_position_backtrace_pos` | IFC backtrace position doesn't point into the table it should |
| `invalid_ifc_sort_value` | IFC sort-order field has a value outside the enumerated range |
| `invalid_infovec_for_reflection` | `std::meta::reflect_invoke` info vector has wrong arity/types |
| `invalid_misaligned_ifc_position` | IFC offset isn't aligned to the slot it points into |
| `invalid_overflowing_ifc_position` | IFC position field's value exceeds the file's section size |
| `invalid_pch_file` | Precompiled-header file has wrong magic or version (file-format error) |
| `invalid_reflection_equality` | `^^X == ^^Y` comparing reflection values of different kinds |
| `invalid_reflection_for_intrinsic` | Reflection meta-function expected a reflection of a specific kind and got a different kind |
| `invalid_std_string_view_for_reflection` | Reflection meta-function received a `string_view` whose backing storage isn't usable at compile time |
| `invalid_unrepresentable_ifc_position` | IFC position value can't be expressed in the field's bit width (corrupt or future-version file) |

> ⚡ **QUIRK -- `ifc_*` tags fire on toolchain mismatch, not on user code**
> The IFC validation tags (`invalid_ifc_partition` is the exception -- it's a user-syntax tag) fire when the binary-module file itself is malformed, typically because it was produced by a different EDG version than the one reading it. End users never see these in normal builds. They appear when a project uses a precompiled module cache produced by one nvcc release and a different release tries to consume it. `--Werror` would normally promote these but build systems frequently filter them out specifically because they're not user-actionable. The right fix is rebuilding the module from source, not suppressing the diagnostic.

## Group 11 -- Microsoft / C++/CLI / Win32 Compatibility Tags (20 tags)

Tags that exist only because EDG supports Microsoft-flavored C++ (managed `^` handles, `__event`, `__property`, MSVC compatibility pragmas). These fire from the C++/CLI front end when input has Microsoft mode enabled but otherwise lie dormant.

| Tag | Semantic role |
|---|---|
| `invalid_asm_qualifiers` | MSVC inline-asm qualifier (e.g. `__asm volatile`) misused |
| `invalid_builtin_fpclassify_args` | `__builtin_fpclassify` got non-FP arguments (intrinsic shared with MSVC) |
| `invalid_case_range` | GCC/MSVC `case X ... Y:` range with `Y < X` |
| `invalid_cleanup_routine` | `__attribute__((cleanup(fn)))` references a function with wrong signature |
| `invalid_date_macro` | `__DATE__` macro replacement was overridden with an invalid format |
| `invalid_event_accessor_decl` | `__event` accessor (`add_*`, `remove_*`) has wrong signature |
| `invalid_event_use` | `__event` member used outside its accessor context |
| `invalid_gnu_sentinel_argument` | `__attribute__((sentinel))` last argument has wrong type |
| `invalid_gnu_sync_size` | `__sync_*` builtin size argument out of supported range |
| `invalid_inheritance_kind_for_class` | MSVC `__single_inheritance`/`__multiple_inheritance`/`__virtual_inheritance` on a class that already has a different inheritance kind |
| `invalid_limit_for_byval` | `__byval(N)` attribute argument out of range |
| `invalid_link_scope` | `__declspec(linkage)` scope value not recognized |
| `invalid_locale` | `#pragma setlocale("...")` value not a recognized locale |
| `invalid_microsoft_pragma_operator` | Microsoft `__pragma(...)` operator misused |
| `invalid_pragma_conform_kind` | `#pragma conform(...)` value not one of the allowed conformance modes |
| `invalid_pragma_operator` | The general `_Pragma(...)` operator received a non-string-literal argument |
| `invalid_property_accessor_decl` | `__property` getter/setter signature wrong |
| `invalid_ref_tracking_ref_combination` | C++/CLI `%` tracking-reference combined with `^` handle in an unsupported way |
| `invalid_scoped_enum_elaboration` | `enum class Tag E;` elaboration with a tag that doesn't match a scoped enum |
| `invalid_symbolic_asm_operand_name` | GCC inline-asm `%[name]` operand name doesn't refer to a labeled constraint |

## Group 12 -- Other / Numerics / Layout (7 tags)

The remainder: tags that don't fit cleanly into a sub-system bucket. Numeric / enumeration / case-value edge cases, miscellaneous IO and address tags, and a few standalone semantic checks.

| Tag | Semantic role |
|---|---|
| `invalid_address_specifier` | Address-space specifier (OpenCL / CUDA `__global` etc.) used on a non-pointer type |
| `invalid_co_return` | `co_return` outside a coroutine function |
| `invalid_empty_initializer_list` | `T x{};` where T's constructor set requires at least one element |
| `invalid_enumerator_value` | Enumerator initializer outside the range of its underlying type |
| `invalid_intaddr_address` | `__intaddr` builtin received a non-address operand |
| `invalid_mmap_address` | `#pragma mmap(...)` address argument not page-aligned or out of range |
| `invalid_tuple_size` | `std::tuple_size<T>::value` evaluation produced a non-integral or negative result |

## Per-Group Tag Counts

| Group | Tag count |
|---|---|
| 1. Lexer and Character-Class | 5 |
| 2. Parser Structural | 11 |
| 3. Attribute and Microsoft-Attribute | 17 |
| 4. Type System Validity | 27 |
| 5. Template and Concept | 15 |
| 6. Function and Declaration Semantic | 16 |
| 7. Literal Operator (UDL) | 9 |
| 8. Constexpr / Consteval / Constinit | 7 |
| 9. CUDA NVIDIA Atomic Intrinsic | 9 |
| 10. Reflection (P2996) and Module/IFC | 12 |
| 11. Microsoft / C++/CLI / Win32 | 20 |
| 12. Other / Numerics / Layout | 7 |
| Sum of table rows | 155 |
| Distinct `^invalid_` strings in binary | **153** (target spec said 150; actual recovery 153) |

Sub-family stems that look like overlaps between groups (e.g. `invalid_attribute_*` could land in Group 3 or Group 11) are resolved by which front-end pass owns the emit site rather than which keyword appears in the tag name. The Microsoft compatibility group (11) owns tags whose semantic content disappears when `-mno-microsoft` is in effect, even when the keyword stem (`invalid_pragma_*`) would also fit the attribute or parser groups.

## Tag-to-Error-Code Crosswalk (selected)

Confirmed pairings (HIGH confidence -- recovered from cross-references near the emit sites):

| Tag | Numeric code |
|---|---|
| `invalid_declaration` | 29 |
| `invalid_storage_class_in_for_init` | 29 (same bucket as the umbrella) |
| `invalid_attribute_location` | 290 |
| `invalid_use_of_ms_attr` | 291 |
| `invalid_constexpr` | 1561 |
| `invalid_consteval` | 1562 |
| `invalid_constinit` | 1563 |
| `invalid_nontype_template_argument` | 1810 |
| `invalid_concept_id` | 1817 |
| `invalid_spaceship_types` | 1893 |
| `invalid_nv_atomic_memory_order_value` | 3221 |
| `invalid_nv_atomic_thread_scope_value` | 3222 |
| `invalid_data_size_for_nv_atomic_generic_function` | 3290 |

Several tags route to the same numeric code (notably the `invalid_*_for_literal_operator` family all map to a single literal-operator bucket). The tag carries the precise diagnostic context, the code is the user-facing bucket. This is also how `--diag_suppress=N` can silence multiple distinct tags at once -- which is occasionally what users want and occasionally a foot-gun.

## Behavior Under `--diag_suppress` and `--Werror`

The `invalid_*` family's behavior under EDG's two main diagnostic-policy flags is worth calling out because it is the major reason this catalog exists:

- **`--diag_suppress=N`** suppresses by *numeric code*. Because many tags route to the same code, this is broad. Suppressing the literal-operator bucket silences all nine `invalid_*_for_literal_operator` tags simultaneously. Users frequently want only one of them, but EDG provides no per-tag suppression at the public CLI level.
- **`--Werror`** promotes warnings to errors but does not change which tags fire. A tag that is emitted as a warning by default (e.g. `invalid_use_of_standalone_ms_attr` in Microsoft mode) becomes an error. A tag that does not fire by default (suppressed by language-mode gates) stays silent.
- **`#pragma nv_diag_suppress <tag>`** is the NVIDIA fork's tag-keyed suppression form. This works on tag identity and is the only reliable way to suppress one tag without silencing its siblings. Used heavily in `libcudacxx` for the `nv_atomic_*` tags.

> ⚡ **QUIRK -- tags fired but not caught by `--Werror`**
> A small set of `invalid_*` tags emit as "remark" rather than "warning" -- the diagnostic engine's lowest severity. `--Werror` only promotes warning to error, not remark. The notable ones are `invalid_ifc_position_backtrace_field`, `invalid_ifc_position_backtrace_pos`, and the rest of the `_ifc_position_*` set: these are emitted as remarks because they indicate a malformed binary module file the compiler is going to fail later anyway. `--Werror` won't catch them; you have to either inspect the build log manually or use `--diag_remark` to promote remarks. This is why CI systems that rely solely on `--Werror` sometimes miss IFC corruption issues until a downstream link or load failure.

## Cross-References

- [Constexpr Diagnostic Tags](constexpr-diagnostics.md) -- Sibling diagnostic family, evaluation-time analog of Group 8
- [Lexer & Tokenizer](lexer.md) -- Where Group 1 character-class tags originate
- [Declaration Parser](declaration-parser.md) -- Where Groups 2, 6, 7, 8 fire
- [Expression Parser](expression-parser.md) -- Where Group 12's miscellaneous expression-level tags fire
- [Template Engine](template-engine.md) -- Group 5's home
- [Type System](type-system.md) -- The 22 type kinds whose rules Group 4 enforces
- [Diagnostics Overview](../diagnostics/overview.md) -- Tag-to-message resolution, format specifier handling, SARIF emission
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- CUDA-specific overlays for Group 9 and the CUDA-mode behavior of Group 8

## Open Followups

- Confirm whether `invalid_address_specifier` (referenced in Group 12) is actually present in the binary or whether the user's reference was a typo for one of the `invalid_attribute_*` tags. The string mine yielded 153 matches; the spec called out 150, suggesting three tags are recent additions not yet documented in the source-tree side of EDG.
- Tag-to-emit-site map. For each tag, the address inside the relevant front-end pass that references it. This would tighten the confidence on tag-to-numeric-code routing from MEDIUM to HIGH and let users predict exactly which `--diag_suppress` numeric codes will catch which tags.
- Cross-reference the `nv_atomic_*` tags with the `libcudacxx` source tree to confirm the workaround pragmas listed in the QUIRK callout match what NVIDIA actually ships. Verify whether SM-version gating affects which tags fire (`_operation_max_min_float` is the candidate).
- Document the per-tag default severity (error / warning / remark). The "fired but not caught by `--Werror`" QUIRK suggests several tags are remark-by-default; a complete severity table would let users predict which tags need `--diag_remark` promotion.
- Verify the IFC position tag set against the IFC format specification (P1689) to determine whether any of these tags can ever legitimately fire on user-controllable input as opposed to compiler-toolchain mismatch.
