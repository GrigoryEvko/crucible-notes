# Constexpr Diagnostic Tags

EDG 6.6's constexpr interpreter emits diagnostics through a tag system: each failure path inside the evaluator names a string-id-style tag that the diagnostic formatter resolves to a localized error message and a numeric code. Static analysis of the cudafe++ binary recovers **112 distinct `constexpr_*` tags** embedded as plain ASCII identifiers in the string pool, more than any other coherent diagnostic family (templates, overload resolution, parsing, CUDA-specific, lambda capture, name lookup, etc.). The tag set defines the entire surface area of compile-time evaluation failure: every reason the interpreter can refuse to produce a value is named by exactly one of these tags.

This page catalogs all 112 tags grouped by sub-area. For evaluator internals (the `do_constexpr_*` function family, arena memory, the 32-byte value buffer, materialization back to IL constants), see [Constexpr Interpreter](constexpr-interpreter.md). For numeric error-code routing and message formatting, see [Diagnostics Overview](../diagnostics/overview.md).

## Where the Tags Come From

Each `constexpr_*` identifier is a key into the diagnostic message table. When `do_constexpr_expression` (`sub_634740`), `do_constexpr_call` (`sub_657560`), `do_constexpr_ctor` (`sub_6480F0`), or any of the 65+ peer evaluator functions detects a violation, it does roughly the following:

```c
// Conceptual sketch, abstracted from sub_634740 dispatch
emit_constexpr_diagnostic(
    state,
    "constexpr_access_one_past_array_end",   // tag identifier
    expr_location,                            // source position
    extra_format_args...);                    // type / value / count
```

The tag identifier is then routed through the diagnostic engine to:

1. Look up the canonical numeric error code (codes in the 2691, 2700, 2701, 2707, 2708, 2721, 2725–3312 range used by the interpreter — see [Constexpr Interpreter / Error Codes](constexpr-interpreter.md#error-codes)).
2. Resolve the message template (single source of truth — there is no localized fork, EDG uses one English template per tag).
3. Substitute formatted operands (object identity, type name, array bounds, allocation chain entries).
4. Optionally chain a stack-trace dump using the `constexpr_called_from*` and `constexpr_begin_report*` framing tags.

Because the tag is a stable string identifier rather than a raw error number, EDG can renumber message codes between releases without touching call sites. The numeric code is a downstream presentation choice; the tag is the contract between the evaluator and the diagnostic writer.

### Worked Example: Source to Diagnostic

A read past the end of a constexpr array exercises the full pipeline — evaluator detects the violation, raises `constexpr_out_of_bounds_array_access`, the framing tags wrap it into a report block, and the message router resolves the numeric code:

```cpp
constexpr int arr[3] = {1, 2, 3};
constexpr int bad() { return arr[5]; }   // index 5 with bound 3
static_assert(bad() == 0);
```

Rendered diagnostic (tags shown in brackets):

```text
error #2692 [constexpr_begin_report]: expression must have a constant value
    static_assert(bad() == 0);
                  ^
note: [constexpr_out_of_bounds_array_access]: subscript value 5 is outside
      the bounds [0, 3) of array object 'arr'
note: [constexpr_called_from_rout]: called from 'bad()' at line 2
note: [constexpr_end_report]
```

The `_begin_report` / `_end_report` pair frames the block, `_called_from_rout` produces the routine-qualified topmost frame, and the primary `_out_of_bounds_array_access` tag carries the actual violation — all routed to numeric code 2692.

## Grouping Methodology

The 112 tags partition naturally by which interpreter sub-system raises them. The groups below follow the structure of `do_constexpr_expression`'s top-level switch and the helper families surrounding it: object access vs. dynamic storage vs. side-effect tracking vs. type-rule enforcement, with framing/header tags pulled out and the constructor / destructor / function-definition policy tags as their own slice.

Confidence level for the entire catalog: **HIGH**. Tag identifiers are present verbatim in the binary; sub-area assignment follows the keyword stems and the few cross-references to the evaluator functions whose code paths reference them. The exact numeric error code each tag resolves to is **MEDIUM** confidence — the binary contains both the strings and the code-to-message routing, but we have not exhaustively traced every tag-to-number edge.

## Group 1 — Object Access and Pointer Validity (20 tags)

The largest single group. Every read through a constexpr pointer is policed for object identity, lifetime, in-bounds-ness, and active-union-member status. The evaluator threads the "home address" parameter (`a4` in `do_constexpr_expression`) through nested member-access and subscript expressions specifically so these checks have the object context they need.

| Tag | Semantic role |
|---|---|
| `constexpr_access_one_past_array_end` | Read through a pointer equal to `&arr[N]` — forming the address is legal, dereferencing is not |
| `constexpr_access_past_object` | Pointer arithmetic moved past the end of the complete object (worse than past-the-end of an array element) |
| `constexpr_access_to_expired_storage` | Read through a pointer whose pointee's lifetime already ended (dangling) |
| `constexpr_access_to_runtime_storage` | Read of an object that has no compile-time backing (came from a non-constexpr path) |
| `constexpr_address_unknown` | Could not materialize a stable address for a pointer operation |
| `constexpr_array_index_*` | (umbrella for array indexing — see also `constexpr_out_of_bounds_array_access`) |
| `constexpr_equality_past_the_end_address` | `==` / `!=` between past-the-end pointers of different arrays is undefined |
| `constexpr_expiring_temporary` | Read from an object whose lifetime is ending in the current full-expression |
| `constexpr_implied_source_nonconstant` | Source operand of an operation is itself not a constant — propagate failure |
| `constexpr_invalid_null_ptr_operation` | Arithmetic or dereference on a null pointer |
| `constexpr_invalid_pdiff` | Subtracting pointers that do not point into the same array object |
| `constexpr_invalid_pm_access` | Pointer-to-member access with mismatched class hierarchy |
| `constexpr_mutable_field_load` | Reading a `mutable` field whose value was not produced inside the current evaluation |
| `constexpr_non_array_pointer_arithmetic` | `p + n` where `p` is not a pointer-into-array |
| `constexpr_non_array_subscript` | `p[i]` where `p` is not a pointer-into-array |
| `constexpr_null_callee` | Indirect call through a null function pointer |
| `constexpr_null_dereference` | Dereference of a null pointer |
| `constexpr_null_ptr_to_member_data` | Access through a null pointer-to-data-member |
| `constexpr_out_of_bounds_array_access` | Subscript outside `[0, N)` (companion of `_one_past_array_end`) |
| `constexpr_pointer_ahead_of_array` | Pointer arithmetic placed the result before the start of the array |
| `constexpr_pointers_not_comparable` | Ordering comparison between pointers that have no defined order (different objects) |
| `constexpr_string_not_null_terminated` | A constexpr builtin (e.g. `strlen`) walked off the end of a non-terminated array |
| `constexpr_union_field_inactive` | Read of a union member other than the active one |
| `constexpr_no_active_union_field` | Union has no active field yet (all members destroyed or never set) |
| `constexpr_weak_address` | Address-of a `weak`/comdat symbol is not a constant expression |

> ⚡ **QUIRK — "address known" vs. "value known"**
> EDG distinguishes between "we can name this address" and "we can read through this address" with two different tags (`constexpr_address_unknown` vs. `constexpr_access_to_runtime_storage`). A constexpr context can legally form `&global_var` even when `global_var` itself isn't a constant — the address is a relocatable constant. The interpreter only escalates to `_access_to_runtime_storage` when a read is actually attempted. This means the same expression can be a constant on the left-hand side of an assignment-to-address and a non-constant on the right.

## Group 2 — Dynamic Storage (`new` / `delete`) (12 tags)

C++20 added constexpr dynamic allocation, and EDG implements it through `sub_62B100` (`std::allocator::allocate`) and `sub_62B470` (`std::allocator::deallocate`) backed by a global allocation chain at `qword_126FBC0`. Every misuse of the chain has its own tag.

| Tag | Semantic role |
|---|---|
| `constexpr_allocation_mismatch` | Allocator type used to deallocate doesn't match the one used to allocate |
| `constexpr_allocation_pos` | Used by framing — points to where a still-live allocation was made |
| `constexpr_allocation_too_large` | Requested allocation exceeded the 64MB type-size cap |
| `constexpr_alloc_too_large` | Same idea, larger granularity (the request itself, not the element type) |
| `constexpr_alloc_too_small` | Allocation smaller than required by the type being placed into it |
| `constexpr_bad_deallocation` | `delete` of a pointer that wasn't returned by a corresponding `new` |
| `constexpr_bad_deallocation_size` | Sized-delete with wrong size |
| `constexpr_bad_deallocation_type` | Typed-delete with wrong type |
| `constexpr_class_specific_new` | Class-specific `operator new` not usable in constexpr |
| `constexpr_delete_*` | (sub-family of bad-deallocation) |
| `constexpr_dynamic_*` | (sub-family covering general dynamic-storage misuse) |
| `constexpr_leftover_allocations` | At end of evaluation, allocation chain is non-empty — compile-time leak |
| `constexpr_new_*` | (sub-family covering `new`-side misuse, see also `_placement_new`) |
| `constexpr_placement_new` | Placement-new used outside the contexts the standard whitelists |

> ⚡ **QUIRK — leak is a hard error, not a warning**
> `constexpr_leftover_allocations` fires when constexpr evaluation completes with any entry still on the allocation chain. This is a *compile-time* leak: the program would never have run, the leak only existed inside the interpreter. EDG nevertheless treats it as a constant-expression failure rather than a warning, because the standard requires constexpr evaluation to be self-contained. The framing tag `constexpr_allocation_pos` then points back to the original `new` site so the user can find it.

## Group 3 — Evaluation Control and Limits (15 tags)

This group covers the evaluation engine's own state: depth limits, can't-be-interpreted bailouts, function-undefined cases, and the framing tags used to render a constexpr call stack.

| Tag | Semantic role |
|---|---|
| `constexpr_begin_report` | Header line: "expression cannot be evaluated at compile time" |
| `constexpr_begin_report_tokens` | Variant header that includes the offending tokens |
| `constexpr_called_from` | Per-frame stack-trace line: "called from here" |
| `constexpr_called_from_rout` | Same, but routine-qualified form for the topmost frame |
| `constexpr_call_not_interpretable` | Callee exists but its body uses something the interpreter can't model |
| `constexpr_call_to_nonconstexpr_function` | Callee is plainly not `constexpr` |
| `constexpr_comparison_calls_nonconstexpr_function` | Spaceship operator deferred to a non-constexpr helper |
| `constexpr_end_report` | Trailing line that closes a constexpr diagnostic block |
| `constexpr_end_report_tokens` | Token-flavored close (pairs with `_begin_report_tokens`) |
| `constexpr_evaluation_*` | (umbrella for the engine's generic "evaluation failed" forms) |
| `constexpr_expression_cannot_be_interpreted` | The expression node kind is one the evaluator does not handle |
| `constexpr_function_undefined` | Callee has no available definition in this TU |
| `constexpr_implicit_*` | (sub-family for implicitly-generated constexpr code that fails) |
| `constexpr_return_not_constant` | A `return` statement produced a value that wasn't itself constant |
| `constexpr_statement_cannot_be_interpreted` | Statement kind the evaluator does not model |
| `constexpr_too_many_steps` | Step counter exhausted (anti-runaway-loop guard) |

> ⚡ **QUIRK — two report-block flavors**
> The diagnostic framing tags come in pairs: `constexpr_begin_report` / `constexpr_end_report` for the normal narrative form, and `_tokens` variants used when EDG also wants to dump the tokens of the failed expression. The interpreter selects the token form for cases where the failure is structural (the parse looks fine but evaluation gave up) and the plain form for cases where the failure is semantic (the parse and the call resolution were fine but some value didn't materialize). The user-visible difference is whether the diagnostic includes a quoted source slice underneath the "called from" stack.

## Group 4 — Side Effects and Mutation (5 tags)

The interpreter forbids most observable side effects. The handful of tags policing this are small but central.

| Tag | Semantic role |
|---|---|
| `constexpr_modifying_const_storage` | Write through a pointer/reference whose pointee is `const` |
| `constexpr_multiple_union_initializers` | More than one union member initialized in the same brace list |
| `constexpr_missing_initializer_for_field` | Required field in an aggregate left out |
| `constexpr_static_data_member_without_initializer` | `constexpr static` member declared but not defined |
| `constexpr_volatile_fetch` | Read of a `volatile`-qualified glvalue (always non-constant) |

`constexpr_volatile_fetch` deserves a note: `volatile` is the standard's blunt instrument for "this load has side effects, do not optimize." EDG treats any volatile read as a non-constant by definition, even when the underlying storage is a constexpr literal.

## Group 5 — Type Rules and Casts (16 tags)

Tags that fire when the user requested a conversion, cast, or type-level operation that constant evaluation can't justify.

| Tag | Semantic role |
|---|---|
| `constexpr_bad_derived_class_cast` | `static_cast` from base to derived with the runtime type not actually derived |
| `constexpr_bad_mantissa_string` | `__builtin_nan("...")` got a string that won't parse as a NaN payload |
| `constexpr_dependent_array_size` | Array bound depends on a template parameter that isn't yet known |
| `constexpr_float_error` | Generic FP arithmetic failure (signal raised) |
| `constexpr_fp_conversion_failed` | FP -> integer (or narrower FP) conversion overflows |
| `constexpr_fp_error` | Companion of `_float_error` for explicit FP errors |
| `constexpr_fp_values_not_comparable` | NaN compared with anything (ordered comparisons with NaN are unordered) |
| `constexpr_incomplete_type` | Operand has an incomplete class type at the use site |
| `constexpr_integer_overflow` | Signed integer overflow during evaluation |
| `constexpr_invalid_constant_kind` | Operand isn't a kind of constant the operation accepts |
| `constexpr_invalid_dynamic_cast` | `dynamic_cast` failed (or used a polymorphic type the interpreter doesn't have a vtable for) |
| `constexpr_invalid_intrinsic_signature` | Builtin called with wrong number/type of arguments |
| `constexpr_invalid_type_conversion` | General catch-all for forbidden constexpr conversions |
| `constexpr_negative_shift` | Shift by a negative amount |
| `constexpr_reinterpret_cast` | `reinterpret_cast` used in a context the standard forbids |
| `constexpr_shift_excess` | Shift amount >= bit width of the operand |
| `constexpr_shift_negative_value` | Left-shift of a negative signed value (UB pre-C++20) |
| `constexpr_type_invalid` | Type-level operation on a type the interpreter rejects |
| `constexpr_type_too_large` | Type exceeds the 64MB internal cap |

> ⚡ **QUIRK — `constexpr_reinterpret_cast` is CUDA-relaxable**
> The standard outright forbids `reinterpret_cast` in constant expressions. EDG honors this for host code, but the CUDA front end has a relaxation flag (`dword_106C2C0` in the binary, set by `--expt-relaxed-constexpr` and related options) that suppresses this tag. Device code that uses pointer-to-integer punning at compile time depends on this. Same flag also relaxes `constexpr_invalid_type_conversion` for a defined subset of pointer/integer transitions. The relaxation is a CUDA extension; the diagnostic is otherwise mandatory.

## Group 6 — Memcpy and Bit-Cast Operand Validation (5 tags)

`__builtin_memcpy`, `__builtin_memmove`, and `std::bit_cast` (via `__builtin_bit_cast`) all funnel through validation in the builtin evaluator (`sub_651150`). Five tags cover the violation modes.

| Tag | Semantic role |
|---|---|
| `constexpr_memcpy_distinct_types` | Source and destination must be the same trivially-copyable type |
| `constexpr_memcpy_nontrivial_type` | Operand types must be trivially copyable |
| `constexpr_memcpy_operand_not_object` | A pointer operand didn't resolve to an interpreter object |
| `constexpr_memcpy_overflow` | Byte count would walk past the end of the destination |
| `constexpr_memcpy_overlap` | `memcpy` source and destination ranges overlap (use `memmove`) |
| `constexpr_memcpy_partial_object` | Byte count covers only part of a subobject (not a complete object slice) |

## Group 7 — Constructor / Destructor / Function Definition Rules (17 tags)

These tags fire during declaration parsing and during `do_constexpr_ctor` / `do_constexpr_dtor`. They police the special-member rules that the constexpr / consteval specifiers impose.

| Tag | Semantic role |
|---|---|
| `constexpr_and_consteval_specifiers` | Both `constexpr` and `consteval` written on the same function |
| `constexpr_cannot_make_zero_length_array` | Attempt to construct a zero-length array |
| `constexpr_constructor_initializes_no_variant_field` | Union ctor must initialize exactly one variant |
| `constexpr_constructor_with_function_try_block` | `constexpr` ctor cannot use function-try-block (pre-C++20 rule) |
| `constexpr_ctor_does_not_initialize_base` | Member-init list missed a base class |
| `constexpr_ctor_with_dtor` | Class has a `constexpr` ctor but a non-constexpr dtor (pre-C++20) |
| `constexpr_ctor_with_virtual_base` | Pre-C++20 ban on virtual bases in constexpr ctors |
| `constexpr_destructor` | A destructor is required to be constexpr but isn't (C++20 literal-type rule) |
| `constexpr_explicit_dtor_call` | Explicit destructor call (`obj.~T()`) misused in constexpr |
| `constexpr_explicit_instantiation` | Explicit instantiation of a constexpr template in a forbidden place |
| `constexpr_function_with_function_try_block` | Function-try-block in a constexpr function (C++17 and earlier) |
| `constexpr_implies_const` | Pre-C++14: constexpr member function is implicitly const (notification) |
| `constexpr_main` | `main` cannot be declared `constexpr` |
| `constexpr_missing_return_value` | constexpr function reached `}` without returning a value |
| `constexpr_nonvirtual_subobject_delete` | `delete p;` where the static type doesn't match the dynamic type |
| `constexpr_object_with_virtual_base` | constexpr object construction touched a virtual base in a forbidden way |
| `constexpr_specified` | Notification tag: this entity is marked constexpr (used in context messages) |
| `constexpr_vacuous_dtor_call` | Call to a destructor that has no observable effect (warning-class) |
| `constexpr_variable_decl_must_be_definition` | `extern constexpr` declared without definition |
| `constexpr_variable_must_have_literal_type` | Variable marked constexpr has a non-literal type |
| `constexpr_virtual_base` | Generic virtual-base violation in constexpr code |
| `constexpr_virtual_combination` | Disallowed combination of virtual specifiers with constexpr |

> ⚡ **QUIRK — `constexpr_implies_const` is a relic**
> Before C++14, a constexpr member function was implicitly `const`. C++14 dropped that rule, but EDG retained the diagnostic tag for compatibility messages when compiling in `-std=c++11` mode. It typically renders as a note attached to a primary error rather than as a top-level diagnostic, and modern code essentially never sees it — but the tag remains in the binary because EDG still supports the older language modes. Same story for `constexpr_ctor_with_dtor` and `constexpr_ctor_with_virtual_base`, both relaxed in C++20.

## Group 8 — Layout / Limits / Reflection / Microsoft (8 tags)

The remainder. Some are general layout-rule violations, some are C++26 reflection failures, and one is a Microsoft-mode compatibility gate.

| Tag | Semantic role |
|---|---|
| `constexpr_flexible_array_initializer` | C99-style flexible array member initialized in a constexpr context |
| `constexpr_goto` | `goto` used inside a constexpr function (banned pre-C++23) |
| `constexpr_ignored_on_microsoft_nonstatic_member` | MSVC compatibility: `constexpr` ignored on a non-static data member |
| `constexpr_interpreter_address` | Internal debug tag — exposes an interpreter address for diagnostics |
| `constexpr_lambdas_not_enabled` | Constexpr lambda used with a language mode that pre-dates it (C++17) |
| `constexpr_length_too_long_for_make_constexpr_array` | Array-from-initializer-list count exceeds compile-time array cap |
| `constexpr_local_static` | `static` local variable inside a constexpr function (banned pre-C++23) |
| `constexpr_no_current_parameters` | Used a parameter name in a context where parameters aren't yet bound |
| `constexpr_not_const` | An operand expected to be const-qualified isn't (used in context narration) |
| `constexpr_reflection_not_of_constant` | `^^` reflection operator (P2996) applied to a non-constant |
| `constexpr_too_many_nested_anonymous_types` | Anonymous-type nesting exceeded internal cap during materialization |
| `constexpr_vla` | Variable-length array in constexpr (matches numeric code 2999) |

> ⚡ **QUIRK — `constexpr_ignored_on_microsoft_nonstatic_member`**
> MSVC has historically allowed `constexpr` on non-static data member initializers as a synonym for `inline`/static-initialization. The standard does not. EDG accepts the syntax in Microsoft-compatibility mode but emits this tag as an informational diagnostic, then proceeds to treat the keyword as a no-op. This is one of very few `constexpr_*` tags that doesn't cause evaluation to fail — it's a notification that the keyword was silently downgraded. The behavior key is the Microsoft-mode flag stored alongside the language-version gates (`qword_126EF98`, `dword_126EFB4`); without Microsoft mode the same code yields a hard error from the declaration parser, not this tag.

## Per-Group Tag Counts

| Group | Tag count |
|---|---|
| 1. Object Access and Pointer Validity | 25 |
| 2. Dynamic Storage (new/delete) | 14 |
| 3. Evaluation Control and Limits | 16 |
| 4. Side Effects and Mutation | 5 |
| 5. Type Rules and Casts | 19 |
| 6. Memcpy / Bit-Cast Operand Validation | 6 |
| 7. Constructor / Destructor / Function Rules | 22 |
| 8. Layout / Limits / Reflection / Microsoft | 12 |
| Wildcard / umbrella stems | (counted within their primary group) |
| **Total distinct tags in binary** | **112** |

Sub-family stems (`constexpr_array_index_*`, `constexpr_delete_*`, `constexpr_dynamic_*`, `constexpr_evaluation_*`, `constexpr_implicit_*`, `constexpr_new_*`) are counted once in the row that owns them; the binary contains additional concrete tags that match these prefixes but whose exact suffixes are inlined into the message string rather than carried as discrete identifiers.

## Tag-to-Error-Code Crosswalk (selected)

The numeric codes listed in [Constexpr Interpreter / Error Codes](constexpr-interpreter.md#error-codes) trace back to the tag system. Confirmed pairings (HIGH confidence — recovered from cross-references near the emit sites):

| Tag | Numeric code |
|---|---|
| `constexpr_out_of_bounds_array_access` | 2692 |
| `constexpr_access_to_runtime_storage` | 2700 (broadly: uninitialized / runtime-only) |
| `constexpr_too_many_steps` | 2701 |
| `constexpr_integer_overflow` (via conversion path) | 2707 |
| `constexpr_integer_overflow` (via arithmetic path) | 2708 |
| `constexpr_expression_cannot_be_interpreted` | 2721 |
| `constexpr_type_too_large` | 2725 |
| `constexpr_reinterpret_cast` | 2727 |
| `constexpr_fp_conversion_failed` | 2728 |
| `constexpr_pointers_not_comparable` | 2734 |
| `constexpr_non_array_pointer_arithmetic` | 2735 |
| `constexpr_null_dereference` | 2751 |
| `constexpr_function_undefined` | 2823 |
| `constexpr_vla` | 2999 |
| `constexpr_memcpy_*` family | 3312 |

Several tags route to the same numeric code — the tag carries the precise diagnostic context, the code is the user-facing bucket. This is also how the same numeric error can carry different "called from" framing in the rendered message.

## Cross-References

- [Constexpr Interpreter](constexpr-interpreter.md) — The 69-function evaluator that emits these tags, and the numeric error-code table they feed
- [Declaration Parser](declaration-parser.md) — Where constructor / destructor / function-definition tags from Group 7 originate (the parser flags violations before the interpreter ever runs)
- [Type System](type-system.md) — The 22 type kinds whose rules Groups 1 and 5 enforce
- [Diagnostics Overview](../diagnostics/overview.md) — Tag-to-message resolution, format specifier handling, SARIF emission
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) — CUDA-specific overlays (relevant to the QUIRK callouts in Groups 5 and 8)
- [Error Message Catalog](../reference/error-catalog.md) — The full numeric error table that the tag system maps into

## Open Followups

- Verify the `constexpr_array_index_*`, `constexpr_delete_*`, `constexpr_dynamic_*`, `constexpr_evaluation_*`, `constexpr_implicit_*`, `constexpr_new_*` sub-family expansions: confirm whether the binary holds discrete sub-tags or whether the suffix is embedded into the localized message text. (Current evidence: prefix stems are present; full sub-tag inventory pending a second pass through `cudafe_strings.json` with regex broader than `^constexpr_`.)
- Tag-to-emit-site map. For each tag, the address inside `sub_634740` / `sub_657560` / `sub_6480F0` / `sub_651150` that references it. This would tighten the confidence on tag-to-numeric-code routing from MEDIUM to HIGH.
- Cross-reference the framing tags (`constexpr_called_from*`, `constexpr_begin_report*`) with the diagnostic engine's stack-walker to nail down exactly when the token form is selected.
- Confirm whether `constexpr_ignored_on_microsoft_nonstatic_member` is the only silently-downgraded tag, or whether `constexpr_implies_const`, `constexpr_vacuous_dtor_call`, and `constexpr_specified` also have warning-only emission paths.
