# Lambda Restrictions

Extended lambdas are CUDA's most constraint-heavy feature. Before a lambda can be wrapped in `__nv_dl_wrapper_t` or `__nv_hdl_wrapper_t` for device transfer, cudafe++ must verify that the closure type is serializable: no reference captures (device memory cannot hold host-side pointers), no function-local types in the public interface (device compiler has no access to them), no unnamed parent classes (the wrapper tag requires a mangleable name), and dozens of other structural invariants. The restriction checker runs as Phase 4 of `scan_lambda` (`sub_447930`, lines 626--866 of the 2113-line function) and continues through per-capture validation in `make_field_for_lambda_capture` (`sub_42EE00`) and recursive type walks in `sub_41A3E0` / `sub_41A1F0`. Together, these functions enforce 39 distinct diagnostic tags covering 35+ error categories and approximately 45 unique error code call sites.

All restrictions apply only when `dword_106BF38` (`--extended-lambda` / `--expt-extended-lambda`) is set and the lambda has an explicit `__device__` or `__host__ __device__` annotation. Standard C++ lambdas and lambdas defined inside `__device__` / `__global__` function bodies are exempt.

## Key Facts

| Property | Value |
|---|---|
| Primary validator | `sub_447930` (`scan_lambda`, Phase 4, ~240 lines within 2113-line function) |
| Per-capture validator | `sub_42EE00` (`make_field_for_lambda_capture`, 551 lines) |
| Type hierarchy walker | `sub_41A3E0` (`validate_type_hd_annotation`, 75 lines) |
| Array/element checker | `sub_41A1F0` (`walk_type_for_hd_violations`, 81 lines) |
| Type walk callback | `sub_41B420` (33 lines, issues errors 3603/3604/3606/3607/3610/3611) |
| Diagnostic tag count | 39 unique tags for extended lambda errors |
| Error code range | 3592--3635, 3689--3691 |
| Error severity | All severity 7 (error), except 3612 (warning) and 3590 (error) |
| Enable flag | `dword_106BF38` (`--extended-lambda`) |
| OptiX gate | `dword_106BDD8` && `dword_106B670` (triggers 3689) |

## Restriction Categories

The tables below list every restriction enforced by the extended lambda validator, organized by the phase of validation in which each check occurs. The **Error** column gives the internal error index (displayed to users as 20000-series with the renumbering formula `code + 16543`). The **Tag** column gives the diagnostic tag name usable with `--diag_suppress` / `#pragma nv_diag_suppress`.

### Category 1: Capture Restrictions

These checks run in two phases. The per-lambda checks (3593, 3595) occur in `scan_lambda` Phase 4 and in `sub_4F9F20` (capture count finalization). The per-capture checks (3596--3599, 3616) run inside `make_field_for_lambda_capture` (`sub_42EE00`), which calls `sub_41A1F0` for array dimension and constructibility analysis.

| Error | Tag | Restriction | Enforcement Location |
|---|---|---|---|
| 3593 | `extended_lambda_reference_capture` | Reference capture (`[&]` or `[&x]`) is prohibited. Device memory cannot hold host-side references. Fires when `capture_default == &` and `capture_mode == &` on the same lambda (byte+24 bits 4 and 5 both set). | `sub_447930` Phase 4, line ~825 |
| 3595 | `extended_lambda_too_many_captures` | Maximum 1023 captures. The bitmap system uses 1024 bits (128 bytes) per wrapper type; bit 0 is reserved for the zero-capture primary template, so the usable range is 1--1023. Capture count `> 0x3FE` triggers this error. | `sub_4F9F20` line ~616 |
| 3596 | `extended_lambda_init_capture_array` | Init-captures with array type are not supported. The init-capture's type node is checked for kind 3 (array type) with element kind 1 and sub-kind 21. | `sub_42EE00` line ~508 |
| 3597 | `extended_lambda_array_capture_rank` | Arrays with more than 7 dimensions cannot be captured. The walker `sub_41A1F0` counts array nesting depth via `sub_7A8370` (is_array_type) and `sub_7A9310` (get_element_type). If depth > 7, error fires. The limit matches the generated `__nv_lambda_array_wrapper` specializations (dims 2--8, plus dim 1 as identity). | `sub_41A1F0` lines ~29, ~54 |
| 3598 | `extended_lambda_array_capture_default_constructible` | Array element type must be default-constructible on the host. After unwinding CV-qualifiers (kind 12 loop), calls `sub_550E50(30, element_type, 0)` to check default-constructibility. Failure emits this error. | `sub_41A1F0` line ~40 |
| 3599 | `extended_lambda_array_capture_assignable` | Array element type must be copy-assignable on the host. Calls `sub_5BD540` to get the assignment operator, then `sub_510860(60, ...)` to verify it is callable. Failure emits this error. | `sub_41A1F0` lines ~42--44 |
| 3616 | `extended_lambda_pack_capture` | Cannot capture an element of a parameter pack. After calling `sub_41A1F0` for type validation, `sub_7A8C00` checks whether the capture type involves a pack expansion; if so, this error fires. | `sub_42EE00` line ~517 |
| 3610 | `extended_lambda_init_capture_initlist` | Init-captures with `std::initializer_list` type are prohibited. The type walk callback `sub_41B420` checks kind and class identity. | `sub_41B420` / `sub_4907A0` |
| 3602 | `extended_lambda_capture_in_constexpr_if` | An extended lambda cannot first-capture a variable inside a `constexpr if` branch. The capture must be visible outside the discarded branch. | `sub_447930` Phase 6 |
| 3614 | `extended_lambda_hd_init_capture` | Init-captures are completely prohibited for `__host__ __device__` lambdas. When byte+25 bit 4 is set (HD wrapper) and the lambda has any captures, this error fires and the HD bits are cleared. | `sub_447930` line ~1710 |
| -- | `this_addr_capture_ext_lambda` | Implicit capture of `this` in an extended lambda triggers a warning. Separate from the errors above; fires during capture list processing. | `sub_42FE50` / `sub_42D710` |
| -- | *(no tag)* | `*this` capture requires either `__device__`-only or definition inside `__device__`/`__global__` function, unless enabled by language dialect. | `sub_42FE50` |

### Category 2: Type Restrictions

Type restrictions enforce that every type visible in the lambda's public interface (captures, parameters, return type, and parent function template arguments) is accessible to the device compiler. Three contexts are checked, each with two sub-checks (function-local types and private/protected class member types). Additionally, the parent function's template arguments are checked for private/protected template members.

| Error | Tag | Context | Restriction |
|---|---|---|---|
| 3603 | `extended_lambda_capture_local_type` | Capture variable type | A type local to a function cannot appear in the type of a captured variable. |
| 3604 | `extended_lambda_capture_private_type` | Capture variable type | A private or protected class member type cannot appear in the type of a captured variable. |
| 3606 | `extended_lambda_call_operator_local_type` | `operator()` signature | A function-local type cannot appear in the return or parameter types of the lambda's `operator()`. |
| 3607 | `extended_lambda_call_operator_private_type` | `operator()` signature | A private/protected class member type cannot appear in the `operator()` return or parameter types. |
| 3610 | `extended_lambda_parent_local_type` | Parent template args | A function-local type cannot appear in the template arguments of the enclosing parent function or any parent classes. |
| 3611 | `extended_lambda_parent_private_type` | Parent template args | A private/protected class member type cannot appear in the template arguments of the enclosing parent function or parent classes. |
| 3635 | `extended_lambda_parent_private_template_arg` | Parent template args | A template that is itself a private/protected class member cannot be used as a template argument of the enclosing parent. |

#### Type Walk Dispatch via `dword_E7FE78`

The callback `sub_41B420` uses a global discriminator `dword_E7FE78` to select between the three contexts. Each context is called with a different value:

| `dword_E7FE78` | Context | Local-type error | Private-type error |
|---|---|---|---|
| 0 | Capture variable type | 3603 | 3604 |
| 1 | `operator()` signature | 3606 | 3607 |
| 2 | Parent template args | 3610 | 3611 |

The dispatch formula in `sub_41B420` is `4 * (dword_E7FE78 != 1) + base_error`. For local types, base is 3603; for private types, base is 3604. When `dword_E7FE78 == 0`, the multiplier is 4*1 = 4, yielding 3603+0 / 3604+0. When `dword_E7FE78 == 1`, the multiplier is 4*0 = 0, yielding 3603+3 = 3606 / 3604+3 = 3607. When `dword_E7FE78 == 2` (and `!= 1`), the multiplier is 4*1 = 4, yielding 3603+4 = *(incorrect -- the actual formula uses a conditional)*. In practice the decompiled code shows:

```c
// For function-local type check:
v2 = 3603;
if (dword_E7FE78)
    v2 = 4 * (unsigned int)(dword_E7FE78 != 1) + 3606;
// dword_E7FE78=0 -> 3603
// dword_E7FE78=1 -> 4*0 + 3606 = 3606
// dword_E7FE78=2 -> 4*1 + 3606 = 3610

// For private/protected type check:
v4 = 3604;
if (dword_E7FE78)
    v4 = 4 * (unsigned int)(dword_E7FE78 != 1) + 3607;
// dword_E7FE78=0 -> 3604
// dword_E7FE78=1 -> 4*0 + 3607 = 3607
// dword_E7FE78=2 -> 4*1 + 3607 = 3611
```

The tree walk itself is invoked via `sub_7B0B60(type_node, sub_41B420, error_base)`. The error_base parameter (792 or 795) is stored in a global and used by the walker to control recursion behavior, not error selection.

### Category 3: Enclosing Parent Function Restrictions

The parent function (the function in whose body the extended lambda is defined) must satisfy several naming and linkage constraints. These exist because the device compiler must be able to instantiate the wrapper template at a globally-unique mangled name derived from the parent function's signature.

| Error | Tag | Restriction | Rationale |
|---|---|---|---|
| 3605 | `extended_lambda_enclosing_function_local` | Parent function must not be defined inside another function (local function). | Nested function bodies have no externally-visible mangling; the wrapper tag would be unresolvable. |
| 3608 | `extended_lambda_cant_take_function_address` | Parent function must allow its address to be taken. Checks `entity+80 bits 0-1` for address-taken capability. | The wrapper tag encodes a function pointer to the parent's `operator()`. If address-of is forbidden (e.g., deleted functions), the tag is ill-formed. |
| 3609 | `extended_lambda_parent_class_unnamed` | Parent function cannot be a member of an unnamed class. Walks the scope chain checking `entity+8` (name pointer) for null. | Unnamed classes have no mangled name, making the wrapper tag unresolvable. |
| 3601 | `extended_lambda_parent_non_extern` | On Windows only: parent function must have external linkage. Internal or no linkage is prohibited. | Windows COFF requires external linkage for cross-TU symbol resolution. On Linux ELF this restriction does not apply. Checks `entity+81 bit 2` (has_qualified_scope) and `entity+8` (name). |
| 3608 | `extended_lambda_inaccessible_parent` | Parent function cannot have private or protected access within its class. Checks `entity+80 bits 0-1` (access specifier). | Private/protected member functions are not visible to the device compiler's separate compilation pass. |
| 3592 | `extended_lambda_enclosing_function_deducible` | Parent function must not have a deduced return type (`auto` return). Checks `entity+81 bit 0` (is_deprecated flag used as deducible marker). | Deduced return types are resolved lazily; the wrapper template needs a concrete type. |
| 3600 | *(no dedicated tag)* | Parent function cannot be `= delete`d or `= default`ed. Checks `entity+166` for values 1 or 2 (deleted, defaulted). | A deleted/defaulted function has no body, so the lambda cannot exist. |
| 3613 | *(no dedicated tag)* | Parent function cannot have a `noexcept` specification. Checks `entity+191 bit 0`. | Exception specifications interact with the wrapper's `NeverThrows` template parameter in ways that cannot be validated at frontend time. |
| 3615 | `extended_lambda_enclosing_function_not_found` | The validator (`sub_41A3E0`) could not locate the enclosing function. Fires when the type annotation context byte has bit 0 set but the host-device validation context `a2 == 0`. | Internal consistency check; should not occur in well-formed code. |

### Category 4: Template Parameter Restrictions

The parent function's template parameter list must satisfy naming and variadic constraints to ensure the wrapper tag type can be uniquely instantiated.

| Error | Tag | Restriction |
|---|---|---|
| -- | `extended_lambda_parent_template_param_unnamed` | Every template parameter of the enclosing parent function must be named. Anonymous template parameters (`template <typename>`) prevent the wrapper from referencing the parameter in its tag type. Checked per-parameter during scope walk. |
| -- | `extended_lambda_nest_parent_template_param_unnamed` | Same restriction applied to nested parent scopes (enclosing class templates, enclosing function templates above the immediate parent). |
| -- | `extended_lambda_multiple_parameter_packs` | The parent template function can have at most one variadic parameter pack, and it must be the last parameter. Multiple packs or non-trailing packs prevent the device compiler from deducing the wrapper specialization. |

### Category 5: Nesting and Context Restrictions

| Error | Tag | Restriction | Rationale |
|---|---|---|---|
| -- | `extended_lambda_enclosing_function_generic_lambda` | An extended lambda cannot be defined inside a generic lambda expression. Generic lambdas have template `operator()` which makes the closure type non-deducible for wrapper tag generation. | Generic lambdas produce dependent types that the wrapper system cannot resolve. |
| -- | `extended_lambda_enclosing_function_hd_lambda` | An extended lambda cannot be defined inside another extended `__host__ __device__` lambda. | The wrapper for the outer HD lambda would need to capture the inner wrapper, creating a recursive type dependency. |
| -- | `extended_host_device_generic_lambda` | A `__host__ __device__` extended lambda cannot be a generic lambda (i.e., with `auto` parameters). | The HD wrapper uses type erasure with concrete function pointer types. Generic lambdas would require polymorphic function pointers, which the type erasure scheme cannot express. |
| -- | `extended_lambda_inaccessible_ancestor` | An extended lambda cannot be defined inside a class that has private or protected access within another class. | The wrapper tag must be visible to both host and device compilation passes. A privately-nested class is not accessible from the translation-unit scope where the wrapper template is instantiated. |
| -- | `extended_lambda_inside_constexpr_if` | An extended lambda cannot be defined inside the `if` or `else` block of a `constexpr if` statement (platform/dialect dependent). | Discarded `constexpr if` branches may eliminate the lambda entirely, but the preamble has already been committed. Restriction prevents dangling wrapper specializations. |
| 3590 | `extended_lambda_multiple_parent` | Cannot specify `__nv_parent` more than once in a single lambda's capture list. | `__nv_parent` stores a single parent class pointer at `lambda_info + 32`; only one slot exists. |
| 3634 | *(no dedicated tag)* | `__nv_parent` requires the lambda to be `__device__` annotated. If `__nv_parent` is specified without `__device__` execution space, this error fires. Additionally validates that the enclosing scope has `__host__` but not `__device__` execution space (bits at entity+182). | `__nv_parent` is used to link the device closure to its enclosing class for member access. This is only meaningful in device execution context. |

### Category 6: Specifier and Annotation Restrictions

| Error | Tag | Restriction |
|---|---|---|
| 3612 | `extended_lambda_disallowed` | `__host__` or `__device__` annotation on a lambda when `--extended-lambda` is not enabled. This is a **warning**, not an error. The flag must be explicitly passed on the command line. |
| 3620 | `extended_lambda_constexpr` | The `constexpr` specifier is not allowed on an extended lambda's `operator()`. Also applies to `consteval`. Two separate emit calls: one for `"constexpr"` and one for `"consteval"`. |
| 3621 | *(no dedicated tag)* | The `operator()` function for a lambda cannot be explicitly annotated with execution space annotations (`__host__`/`__device__`/`__global__`). The annotations are derived from the closure class, not the operator. Fires when `entity+182 bits 1-2` are set on the call operator. |
| 3689 | *(no dedicated tag)* | OptiX mode incompatibility. When both `dword_106BDD8` (OptiX) and `dword_106B670` (a secondary OptiX flag) are set, and the lambda body at `qword_106B678 + 176*dword_106B670 + 5` has bit 3 set, this error fires. OptiX has stricter lambda body requirements than standard CUDA. |
| 3690 | `extended_lambda_discriminator` | Lambda numbering lookup failure in the red-black tree (`ptr` / `dword_E7FE48`). The tree maps source positions to lambda indices for unique wrapper tag generation. If the tree search fails, the wrapper cannot be uniquely identified. |
| 3691 | *(no dedicated tag)* | Extended lambda with `__host__ __device__` annotation where the type annotation byte has bit 4 set (HD init-capture validation context). Issued by `sub_41A3E0` as a final post-check. |

### Category 7: Enclosing Scope Miscellaneous

| Error | Tag | Restriction |
|---|---|---|
| 3617 | `extended_lambda_no_parent_func` | No enclosing function could be found for the extended lambda. `sub_6BCDD0` (`nv_find_parent_lambda_function`) walked the scope chain and returned null. The lambda may be at file scope, which is not a valid context for an extended lambda. |
| 3618 | `extended_lambda_illegal_parent` | Ambiguous overload when resolving the enclosing function. `sub_6BCDD0` found multiple candidate functions. Emitted via `sub_4F6E50` with three operands (location, space string, function name). |
| 3619 | *(no dedicated tag)* | Secondary ambiguity variant. Same as 3618 but fires on a different branch (the `v291[0]` check rather than `v287[0]`), indicating the ambiguity was detected through a different resolution path. |
| 3601 | *(duplicate)* | Lambda defined in unnamed namespace (`entity+81 bit 2` set and `entity+8` name pointer is null). The wrapper tag requires a named scope. |
| 3605 | *(duplicate)* | Non-trivially-copyable type in capture scope. When `entity+80 bits 0-1` indicate non-trivial copy semantics, the capture cannot be transferred to device memory. |

## Validation Architecture

### Phase 4 of scan_lambda: Per-Lambda Validation

After parsing the capture list and annotations (Phases 1--3), `scan_lambda` enters the extended lambda validation block. This block is guarded by `dword_106BF38` (extended lambda mode) and the annotation bits at `lambda_info + 25`. The validation proceeds as:

```
sub_447930 (scan_lambda), Phase 4 entry:
  |
  +-- Call sub_6BCDD0 (nv_find_parent_lambda_function)
  |     Returns: parent function node, sets is_device/is_template flags
  |
  +-- If parent == NULL:  emit error 3617
  +-- If ambiguous:       emit error 3618 or 3619
  |
  +-- Validate parent function properties:
  |     entity+81 bit 0  -> error 3592 (deprecated/deducible)
  |     entity+191 bit 0 -> error 3613 (noexcept spec)
  |     entity+166 == 1|2 -> error 3600 (deleted/defaulted)
  |     entity+81 bit 2  -> unnamed scope check -> error 3601
  |     entity+80 bits 0-1 -> address-taken / access check -> error 3608
  |
  +-- Walk parent scope chain for unnamed classes:
  |     entity+8 == NULL -> error 3609
  |     Non-trivial copy   -> error 3605
  |
  +-- Check capture-default conflicts:
  |     byte+24 bits 4+5 both set -> error 3593 (& and = conflict)
  |
  +-- OptiX gate: dword_106BDD8 -> error 3689
  |
  +-- Lambda numbering via red-black tree:
        Lookup failure -> error 3690
```

### Per-Capture Validation: sub_42EE00

For each captured variable, `make_field_for_lambda_capture` runs targeted checks:

```
sub_42EE00 (make_field_for_lambda_capture):
  |
  +-- If byte+25 bit 3 set (device wrapper):
  |     |
  |     +-- Check init-capture for array type
  |     |     type_node+48 == 3 && sub_kind == 21 -> error 3596
  |     |
  |     +-- Call sub_41A1F0 (walk_type_for_hd_violations)
  |     |     Counts array dimensions, checks element type
  |     |     dim > 7 -> error 3597
  |     |     Not default-constructible -> error 3598
  |     |     Not assignable -> error 3599
  |     |
  |     +-- Check for pack expansion
  |           sub_7A8C00 returns true -> error 3616
  |
  +-- (Later) If byte+25 bit 4 set (HD wrapper):
        |
        +-- Call sub_7B0B60 with sub_41B420 callback
              Walks entire type tree, fires 3603/3604 for
              function-local and private/protected types
```

### Type Hierarchy Walker: sub_41A3E0 / sub_41A1F0

`sub_41A3E0` is the outer wrapper that validates the per-capture annotation context. `sub_41A1F0` performs the recursive array dimension walk and element-type validation.

```
sub_41A3E0 (validate_type_hd_annotation):
  |
  +-- Determine context string: "__device__" or "__host__ __device__"
  |     Based on a2 parameter (0 = HD, nonzero = device-only)
  |
  +-- Check annotation byte (a1+32):
  |     bit 0 set && a2==0 -> error 3615
  |     bit 3 set -> check parent visibility:
  |       entity+163 < 0 (private) -> check bit pattern
  |       Both bits 3+4 set with private parent -> error 3635
  |       Otherwise -> error 3593
  |     bit 5 set -> error 3594 (private/protected access)
  |
  +-- Unwrap CV-qualifiers on element type (kind==12 loop)
  |
  +-- Call sub_41A1F0 (walk_type_for_hd_violations):
  |     Recursive array walker:
  |       v6 = dimension counter
  |       Loop: while sub_7A8370(type) returns true
  |         increment v6, follow sub_7A9310 to element type
  |       If v6 > 7: error 3597
  |       Unwrap CV (kind==12 loop)
  |       If not in dependent context (dword_126C5C4 == -1):
  |         Check scope flags (byte+6 bits 1-2)
  |         sub_550E50(30, type, 0) -> error 3598 (not default-constructible)
  |         sub_5BD540 + sub_510860(60, ...) -> error 3599 (not assignable)
  |       Call sub_7B0B60(type, sub_41B420, 792) for deep type walk
  |
  +-- If a3 (third parameter) set:
        Check bit 4 of annotation byte -> error 3691
```

### sub_41B420: Type Walk Callback

This compact callback (33 lines decompiled) is invoked by `sub_7B0B60` for every type node in the capture's type tree. It checks two properties:

1. **Function-local type** -- `entity+81 bit 0` set: the type is defined inside a function body. Error selection uses `dword_E7FE78` to pick between capture context (3603), operator() context (3606), and parent template-arg context (3610).

2. **Private/protected member type** -- `entity+81 bit 2` set AND `entity+80 bits 0-1` in range [1,2] (private or protected access specifier). Error selection parallels the local-type case: 3604, 3607, or 3611 depending on `dword_E7FE78`.

Special case: when `entity+132 == 9` (template parameter dependent type) AND `entity+152` points to a class with `byte+86 bit 0` set AND `entity+72` is non-null, the function-local check is suppressed. This handles template parameters that are not themselves local but instantiate with local types -- the error is deferred to instantiation time.

## Diagnostic Tag Reference

Complete list of all 39 extended lambda diagnostic tags, sorted alphabetically. All tags can be used with `--diag_suppress`, `--diag_warning`, `--diag_error` on the command line, and with `#pragma nv_diag_suppress`, `#pragma nv_diag_warning`, `#pragma nv_diag_error` in source.

| Tag | Category |
|---|---|
| `extended_host_device_generic_lambda` | Nesting |
| `extended_lambda_array_capture_assignable` | Capture |
| `extended_lambda_array_capture_default_constructible` | Capture |
| `extended_lambda_array_capture_rank` | Capture |
| `extended_lambda_call_operator_local_type` | Type |
| `extended_lambda_call_operator_private_type` | Type |
| `extended_lambda_cant_take_function_address` | Parent |
| `extended_lambda_capture_in_constexpr_if` | Capture |
| `extended_lambda_capture_local_type` | Type |
| `extended_lambda_capture_private_type` | Type |
| `extended_lambda_constexpr` | Specifier |
| `extended_lambda_disallowed` | Specifier |
| `extended_lambda_discriminator` | Internal |
| `extended_lambda_enclosing_function_deducible` | Parent |
| `extended_lambda_enclosing_function_generic_lambda` | Nesting |
| `extended_lambda_enclosing_function_hd_lambda` | Nesting |
| `extended_lambda_enclosing_function_local` | Parent |
| `extended_lambda_enclosing_function_not_found` | Parent |
| `extended_lambda_hd_init_capture` | Capture |
| `extended_lambda_illegal_parent` | Parent |
| `extended_lambda_inaccessible_ancestor` | Nesting |
| `extended_lambda_inaccessible_parent` | Parent |
| `extended_lambda_init_capture_array` | Capture |
| `extended_lambda_init_capture_initlist` | Capture |
| `extended_lambda_inside_constexpr_if` | Nesting |
| `extended_lambda_multiple_parameter_packs` | Template |
| `extended_lambda_multiple_parent` | Nesting |
| `extended_lambda_nest_parent_template_param_unnamed` | Template |
| `extended_lambda_no_parent_func` | Parent |
| `extended_lambda_pack_capture` | Capture |
| `extended_lambda_parent_class_unnamed` | Parent |
| `extended_lambda_parent_local_type` | Type |
| `extended_lambda_parent_non_extern` | Parent |
| `extended_lambda_parent_private_template_arg` | Type |
| `extended_lambda_parent_private_type` | Type |
| `extended_lambda_parent_template_param_unnamed` | Template |
| `extended_lambda_reference_capture` | Capture |
| `extended_lambda_too_many_captures` | Capture |
| `this_addr_capture_ext_lambda` | Capture |

## Bitmap Interaction

The capture count limit of 1023 derives from the bitmap architecture. Each wrapper type (device and host-device) uses a 128-byte bitmap (`unk_1286980` / `unk_1286900`) storing 1024 bits. The bitmap setter `sub_6BCBF0` performs:

```c
result[capture_count >> 6] |= 1LL << capture_count;
```

Bit 0 is never emitted as a wrapper specialization (the zero-capture case uses the primary template). Bits 1--1023 map to generated partial specializations. The error check at capture count > 0x3FE (1022) fires before the bitmap set operation, so the effective maximum is 1023 captures. Attempting 1024 or more would overflow the 64-bit word boundary calculation, though in practice the error prevents this.

## Operator() Annotation Derivation

Error 3621 enforces a fundamental design rule: the `operator()` function of an extended lambda **must not** carry explicit execution space annotations. Instead, the execution space is derived from the closure class. During `scan_lambda` Phase 5 (`decl_call_operator_for_lambda`), the code sets the call operator's execution space from `lambda_info + 25`:

```c
// Propagate device/host from lambda_info to call operator
byte[operator+182] = (4 * byte[lambda+25]) & 0x10 | byte[operator+182] & 0xEF;
byte[operator+182] = (16 * byte[lambda+25]) & 0x20 | byte[operator+182] & 0xDF;
```

If the call operator already has execution space bits set (from explicit annotation by the user), error 3621 fires. The rationale is that the wrapper template's tag type already encodes the execution space; having the operator carry its own annotations would create an inconsistency that the device compiler cannot resolve.

## Key Functions

| Address | Name (recovered) | Lines | Role |
|---|---|---|---|
| `sub_447930` | `scan_lambda` | 2113 | Master lambda parser; Phase 4 = restriction validator |
| `sub_42EE00` | `make_field_for_lambda_capture` | 551 | Per-capture field creator with device-lambda validation |
| `sub_41A3E0` | `validate_type_hd_annotation` | 75 | Outer type annotation checker (errors 3593/3594/3615/3635/3691) |
| `sub_41A1F0` | `walk_type_for_hd_violations` | 81 | Recursive array dim / element-type validator (3597/3598/3599) |
| `sub_41B420` | *(type walk callback)* | 33 | Issues 3603/3604/3606/3607/3610/3611 via `dword_E7FE78` dispatch |
| `sub_6BCDD0` | `nv_find_parent_lambda_function` | 33 | Scope chain walk to find enclosing host/device function |
| `sub_6BCBF0` | `nv_record_capture_count` | 13 | Set bit in device or host-device bitmap |
| `sub_4F9F20` | *(capture count finalizer)* | ~620 | Checks capture count > 0x3FE, calls bitmap setter |
| `sub_7B0B60` | *(tree walker)* | -- | Recursive type tree traversal, calls callback for each node |
| `sub_7A8370` | *(is_array_type)* | -- | Returns nonzero if type node is an array type |
| `sub_7A9310` | *(get_element_type)* | -- | Returns the element type of an array type node |
| `sub_550E50` | *(check_default_constructible)* | -- | `sub_550E50(30, type, 0)` tests default-constructibility |
| `sub_510860` | *(check_callable)* | -- | `sub_510860(60, op, type)` tests if operator is callable |

## Global State

| Variable | Address | Purpose |
|---|---|---|
| `dword_106BF38` | `0x106BF38` | Extended lambda mode flag (`--extended-lambda`) |
| `dword_106BDD8` | `0x106BDD8` | OptiX mode flag |
| `dword_106B670` | `0x106B670` | Secondary OptiX lambda flag |
| `qword_106B678` | `0x106B678` | OptiX lambda body array base pointer |
| `dword_E7FE78` | `0xE7FE78` | Type walk context discriminator (0=capture, 1=operator, 2=parent) |
| `ptr` | *(stack)* | Red-black tree root for lambda numbering per source position |
| `dword_E7FE48` | `0xE7FE48` | Red-black tree sentinel node |
| `dword_126C5C4` | `0x126C5C4` | Dependent scope index (-1 = not in dependent context) |
| `dword_126EFAC` | `0x126EFAC` | CUDA mode flag |
| `dword_126EFA4` | `0x126EFA4` | GCC extensions flag |
| `qword_126EF98` | `0x126EF98` | GCC compatibility version |

## Related Pages

- [Extended Lambda Overview](./overview.md) -- end-to-end pipeline, annotation bits, `lambda_info` layout
- [Device Lambda Wrapper](./device-wrapper.md) -- `__nv_dl_wrapper_t` template structure
- [Host-Device Lambda Wrapper](./host-device-wrapper.md) -- `__nv_hdl_wrapper_t` type-erased design
- [Capture Handling](./capture-handling.md) -- `__nv_lambda_field_type`, `__nv_lambda_array_wrapper`
- [Preamble Injection](./preamble-injection.md) -- `sub_6BCC20` emission pipeline
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- complete error index with message templates
- [Cross-Space Call Validation](../cuda/cross-space-validation.md) -- execution space checking infrastructure
