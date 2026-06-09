# CUDA Error Catalog

cudafe++ reserves internal error indices 3457--3794 (338 slots) for CUDA-specific diagnostics. These are displayed to users as numbers 20000--20337 using the formula `display = internal + 16543`. Of the 338 slots, approximately 210 carry unique message templates; the remainder are reserved or share templates with parametric fill-ins. Every CUDA error can be controlled by its numeric code or diagnostic tag name via `--diag_suppress`, `--diag_warning`, `--diag_error`, or the `#pragma nv_diagnostic` system.

This page is a flat lookup table. For the diagnostic pipeline architecture (severity stack, pragma scoping, SARIF output), see [Diagnostic Overview](../diagnostics/overview.md). For narrative discussion of each category with implementation details, see [CUDA Errors](../diagnostics/cuda-errors.md).

**Scope note.** This catalog enumerates the **CUDA-extension namespace** (~248 documented tags spanning internal codes 3457--3794, plus 6 pragma-action tags listed for completeness). The full cudafe++ binary defines roughly **859 snake_case diagnostic tags** across all of EDG 6.6's C++ frontend; the remaining ~605 tags govern standard C++ diagnostics (templates, constexpr evaluation, modules, attributes, type mismatches, overload ambiguity, etc.) inherited from EDG and shared with the wider EDG ecosystem. Those tags use the original error-code range 0--3456 and are accepted by `--diag_suppress` / `#pragma nv_diag_suppress` identically to CUDA tags but are not enumerated here. See [EDG Diagnostic Namespace](#edg-diagnostic-namespace-outside-this-catalog) at the bottom of this page for a category-prefix breakdown. Confidence: **HIGH** for the 859 total (counted directly from `cudafe_strings.json`), **MED** for the 248 / 605 split (prefix-based bucketing).

## Numbering and Display Format

```text
User-visible:  file.cu(42): error #20042-D: calling a __device__ function from ...
                                    ^^^^^
                                    display code = internal + 16543
```

| Direction | Formula | Example |
|---|---|---|
| Display to internal | `internal = display - 16543` | 20042 maps to internal 3499 |
| Internal to display | `display = internal + 16543` | 3457 maps to display 20000 |

The `-D` suffix appears when severity is 7 or below (note, remark, warning, soft error). Hard errors (severity 8+) omit the suffix.

## Severity Codes

| Code | Level | Suppressible |
|---|---|---|
| 2 | note | yes |
| 4 | remark | yes |
| 5 | warning | yes |
| 6 | command-line warning | no |
| 7 | error (soft) | yes |
| 8 | error (hard, from pragma) | no |
| 9 | catastrophic error | no |
| 10 | command-line error | no |
| 11 | internal error | no |

## How to Use This Catalog

Suppress by numeric code:
```bash
nvcc --diag_suppress=20042
```

Suppress by tag name:
```bash
nvcc --diag_suppress=unsafe_device_call
```

In source code:
```c
#pragma nv_diag_suppress unsafe_device_call
#pragma nv_diag_suppress 20042
```

---

## Category 1: Cross-Space Calling

Checks performed by the call-graph walker comparing the execution-space byte at entity offset `+182` of caller vs. callee.

### Standard Cross-Space Calls (6 messages)

| Tag | Sev | Message |
|---|---|---|
| `unsafe_device_call` | W | calling a \_\_device\_\_ function(%sq1) from a \_\_host\_\_ function(%sq2) is not allowed |
| `unsafe_device_call` | W | calling a \_\_device\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed |
| `unsafe_device_call` | W | calling a \_\_host\_\_ function(%sq1) from a \_\_device\_\_ function(%sq2) is not allowed |
| `unsafe_device_call` | W | calling a \_\_host\_\_ function(%sq1) from a \_\_global\_\_ function(%sq2) is not allowed |
| `unsafe_device_call` | W | calling a \_\_host\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed |
| `unsafe_device_call` | W | calling a \_\_host\_\_ function from a \_\_host\_\_ \_\_device\_\_ function is not allowed |

### Constexpr Cross-Space Calls (6 messages)

These fire when `--expt-relaxed-constexpr` is not enabled.

| Tag | Sev | Message |
|---|---|---|
| `unsafe_device_call` | W | calling a constexpr \_\_device\_\_ function(%sq1) from a \_\_host\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |
| `unsafe_device_call` | W | calling a constexpr \_\_device\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |
| `unsafe_device_call` | W | calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_device\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |
| `unsafe_device_call` | W | calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_global\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |
| `unsafe_device_call` | W | calling a constexpr \_\_host\_\_ function(%sq1) from a \_\_host\_\_ \_\_device\_\_ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |
| `unsafe_device_call` | W | calling a constexpr \_\_host\_\_ function from a \_\_host\_\_ \_\_device\_\_ function is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this. |

---

## Category 2: Virtual Override Mismatch

Override checker (`sub_432280`) extracts the `0x30` mask from the execution-space byte. `__global__` is excluded because kernels cannot be virtual.

| Tag | Sev | Message |
|---|---|---|
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_device\_\_ function, but overriding entity (%n2) is a \_\_host\_\_ function |
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_device\_\_ function, but overriding entity (%n2) is a \_\_host\_\_ \_\_device\_\_ function |
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_host\_\_ function, but overriding entity (%n2) is a \_\_device\_\_ function |
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_host\_\_ function, but overriding entity (%n2) is a \_\_host\_\_ \_\_device\_\_ function |
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_host\_\_ \_\_device\_\_ function, but overriding entity (%n2) is a \_\_device\_\_ function |
| — | E | execution space mismatch: overridden entity (%n1) is a \_\_host\_\_ \_\_device\_\_ function, but overriding entity (%n2) is a \_\_host\_\_ function |

---

## Category 3: Redeclaration Mismatch

Checked in `decl_routine` (`sub_4CE420`) and `check_cuda_attribute_consistency` (`sub_4C6D50`).

### Incompatible Redeclarations (error-level)

| Tag | Sev | Message |
|---|---|---|
| `device_function_redeclared_with_global` | E | a \_\_device\_\_ function(%no1) redeclared with \_\_global\_\_ |
| `global_function_redeclared_with_device` | E | a \_\_global\_\_ function(%no1) redeclared with \_\_device\_\_ |
| `global_function_redeclared_with_host` | E | a \_\_global\_\_ function(%no1) redeclared with \_\_host\_\_ |
| `global_function_redeclared_with_host_device` | E | a \_\_global\_\_ function(%no1) redeclared with \_\_host\_\_ \_\_device\_\_ |
| `global_function_redeclared_without_global` | E | a \_\_global\_\_ function(%no1) redeclared without \_\_global\_\_ |
| `host_function_redeclared_with_global` | E | a \_\_host\_\_ function(%no1) redeclared with \_\_global\_\_ |
| `host_device_function_redeclared_with_global` | E | a \_\_host\_\_ \_\_device\_\_ function(%no1) redeclared with \_\_global\_\_ |

### Compatible Promotions (warning-level, promoted to HD)

| Tag | Sev | Message |
|---|---|---|
| `device_function_redeclared_with_host` | W | a \_\_device\_\_ function(%no1) redeclared with \_\_host\_\_, hence treated as a \_\_host\_\_ \_\_device\_\_ function |
| `device_function_redeclared_with_host_device` | W | a \_\_device\_\_ function(%no1) redeclared with \_\_host\_\_ \_\_device\_\_, hence treated as a \_\_host\_\_ \_\_device\_\_ function |
| `device_function_redeclared_without_device` | W | a \_\_device\_\_ function(%no1) redeclared without \_\_device\_\_, hence treated as a \_\_host\_\_ \_\_device\_\_ function |
| `host_function_redeclared_with_device` | W | a \_\_host\_\_ function(%no1) redeclared with \_\_device\_\_, hence treated as a \_\_host\_\_ \_\_device\_\_ function |
| `host_function_redeclared_with_host_device` | W | a \_\_host\_\_ function(%no1) redeclared with \_\_host\_\_ \_\_device\_\_, hence treated as a \_\_host\_\_ \_\_device\_\_ function |

---

## Category 4: \_\_global\_\_ Function Constraints

### Return Type and Signature

| Tag | Sev | Message |
|---|---|---|
| `global_function_return_type` | E | a \_\_global\_\_ function must have a void return type |
| `global_function_deduced_return_type` | E | a \_\_global\_\_ function must not have a deduced return type |
| `global_function_has_ellipsis` | E | a \_\_global\_\_ function cannot have ellipsis |
| `global_rvalue_ref_type` | E | a \_\_global\_\_ function cannot have a parameter with rvalue reference type |
| `global_ref_param_restrict` | E | a \_\_global\_\_ function cannot have a parameter with \_\_restrict\_\_ qualified reference type |
| `global_va_list_type` | E | A \_\_global\_\_ function or function template cannot have a parameter with va\_list type |
| `global_function_with_initializer_list` | E | a \_\_global\_\_ function or function template cannot have a parameter with type std::initializer\_list |
| `global_param_align_too_big` | E | cannot pass a parameter with a too large explicit alignment to a \_\_global\_\_ function on win32 platforms |

### Declaration Context

| Tag | Sev | Message |
|---|---|---|
| `global_class_decl` | E | A \_\_global\_\_ function or function template cannot be a member function |
| `global_friend_definition` | E | A \_\_global\_\_ function or function template cannot be defined in a friend declaration |
| `global_function_in_unnamed_inline_ns` | E | A \_\_global\_\_ function or function template cannot be declared within an inline unnamed namespace |
| `global_operator_function` | E | An operator function cannot be a \_\_global\_\_ function |
| `global_new_or_delete` | E | *(\_\_global\_\_ on operator new/delete)* |
| — | E | function main cannot be marked \_\_device\_\_ or \_\_global\_\_ |

### C++ Feature Restrictions

| Tag | Sev | Message |
|---|---|---|
| `global_function_constexpr` | E | A \_\_global\_\_ function or function template cannot be marked constexpr |
| `global_function_consteval` | E | A \_\_global\_\_ function or function template cannot be marked consteval |
| `global_function_inline` | E | *(\_\_global\_\_ with inline)* |
| `global_exception_spec` | E | An exception specification is not allowed for a \_\_global\_\_ function or function template |

### Template Argument Restrictions

| Tag | Sev | Message |
|---|---|---|
| `global_private_type_arg` | E | A type that is defined inside a class and has private or protected access (%t) cannot be used in the template argument type of a \_\_global\_\_ function template instantiation, unless the class is local to a \_\_device\_\_ or \_\_global\_\_ function |
| `global_private_template_arg` | E | A template that is defined inside a class and has private or protected access cannot be used in the template template argument of a \_\_global\_\_ function template instantiation |
| `global_unnamed_type_arg` | E | An unnamed type (%t) cannot be used in the template argument type of a \_\_global\_\_ function template instantiation, unless the type is local to a \_\_device\_\_ or \_\_global\_\_ function |
| `global_func_local_template_arg` | E | A type defined inside a \_\_host\_\_ function (%t) cannot be used in the template argument type of a \_\_global\_\_ function template instantiation |
| `global_lambda_template_arg` | E | The closure type for a lambda (%t%s) cannot be used in the template argument type of a \_\_global\_\_ function template instantiation, unless the lambda is defined within a \_\_device\_\_ or \_\_global\_\_ function, or the flag '-extended-lambda' is specified and the lambda is an extended lambda |
| `local_type_used_in_global_function` | W | a local type %t (defined in %sq1) used in global function %sq2 template argument, the global function cannot be launched from host code. |

### Variable Template Restrictions (parallel set)

| Tag | Sev | Message |
|---|---|---|
| `variable_template_private_type_arg` | E | *(private/protected type in variable template instantiation)* |
| `variable_template_private_template_arg` | E | *(private template template arg in variable template)* |
| `variable_template_unnamed_type_template_arg` | E | An unnamed type (%t) cannot be used in the template argument type of a variable template instantiation, unless the type is local to a \_\_device\_\_ or \_\_global\_\_ function |
| `variable_template_func_local_template_arg` | E | A type defined inside a \_\_host\_\_ function (%t) cannot be used in the template argument type of a variable template instantiation |
| `variable_template_lambda_template_arg` | E | The closure type for a lambda (%t%s) cannot be used in the template argument type of a variable template instantiation, unless the lambda is defined within a \_\_device\_\_ or \_\_global\_\_ function, or the lambda is an 'extended lambda' and the flag --extended-lambda is specified |

### Variadic Template Constraints

| Tag | Sev | Message |
|---|---|---|
| `global_function_multiple_packs` | E | Multiple pack parameters are not allowed for a variadic \_\_global\_\_ function template |
| `global_function_pack_not_last` | E | Pack template parameter must be the last template parameter for a variadic \_\_global\_\_ function template |

### Launch Configuration Attributes

| Tag | Sev | Message |
|---|---|---|
| `bounds_attr_only_on_global_func` | E | %s is only allowed on a \_\_global\_\_ function |
| `maxnreg_attr_only_on_global_func` | E | *(\_\_maxnreg\_\_ only on \_\_global\_\_)* |
| `missing_launch_bounds` | W | no \_\_launch\_bounds\_\_ specified for \_\_global\_\_ function |
| `cuda_specifier_twice_in_group` | E | *(duplicate CUDA specifier on same declaration)* |
| `bounds_maxnreg_incompatible_qualifiers` | E | *(\_\_launch\_bounds\_\_ and \_\_maxnreg\_\_ conflict)* |
| — | E | The %s qualifiers cannot be applied to the same kernel |
| — | E | Multiple %s specifiers are not allowed |
| — | E | incorrect value for launch bounds |

---

## Category 5: Extended Lambda Restrictions

Extended lambdas (`__device__` or `__host__ __device__` lambdas in host code, enabled by `--extended-lambda`) must have closure types serializable for device transfer.

### Capture Restrictions

| Tag | Sev | Message |
|---|---|---|
| `extended_lambda_reference_capture` | E | An extended %s lambda cannot capture variables by reference |
| `extended_lambda_pack_capture` | E | An extended %s lambda cannot capture an element of a parameter pack |
| `extended_lambda_too_many_captures` | E | An extended %s lambda can only capture up to 1023 variables |
| `extended_lambda_array_capture_rank` | E | An extended %s lambda cannot capture an array variable (type: %t) with more than 7 dimensions |
| `extended_lambda_array_capture_assignable` | E | An extended %s lambda cannot capture an array variable whose element type (%t) is not assignable on the host |
| `extended_lambda_array_capture_default_constructible` | E | An extended %s lambda cannot capture an array variable whose element type (%t) is not default constructible on the host |
| `extended_lambda_init_capture_array` | E | An extended %s lambda cannot init-capture variables with array type |
| `extended_lambda_init_capture_initlist` | E | An extended %s lambda cannot have init-captures with type std::initializer\_list |
| `extended_lambda_capture_in_constexpr_if` | E | An extended %s lambda cannot first-capture variable in constexpr-if context |
| `this_addr_capture_ext_lambda` | W | Implicit capture of 'this' in extended lambda expression |
| `extended_lambda_hd_init_capture` | E | init-captures are not allowed for extended \_\_host\_\_ \_\_device\_\_ lambdas |
| — | E | Unless enabled by language dialect, \*this capture is only supported when the lambda is either \_\_device\_\_ only, or is defined within a \_\_device\_\_ or \_\_global\_\_ function |

### Type Restrictions on Captures and Parameters

| Tag | Sev | Message |
|---|---|---|
| `extended_lambda_capture_local_type` | E | A type local to a function (%t) cannot be used in the type of a variable captured by an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_capture_private_type` | E | A type that is a private or protected class member (%t) cannot be used in the type of a variable captured by an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_call_operator_local_type` | E | A type local to a function (%t) cannot be used in the return or parameter types of the operator() of an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_call_operator_private_type` | E | A type that is a private or protected class member (%t) cannot be used in the return or parameter types of the operator() of an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_parent_local_type` | E | A type local to a function (%t) cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_parent_private_type` | E | A type that is a private or protected class member (%t) cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ lambda |
| `extended_lambda_parent_private_template_arg` | E | A template that is a private or protected class member cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended %s lambda |

### Enclosing Parent Function Restrictions

| Tag | Sev | Message |
|---|---|---|
| `extended_lambda_enclosing_function_local` | E | The enclosing parent function (%sq2) for an extended %s1 lambda must not be defined inside another function |
| `extended_lambda_enclosing_function_not_found` | E | *(no enclosing function found for extended lambda)* |
| `extended_lambda_inaccessible_parent` | E | The enclosing parent function (%sq2) for an extended %s1 lambda cannot have private or protected access within its class |
| `extended_lambda_enclosing_function_deducible` | E | The enclosing parent function (%sq2) for an extended %s1 lambda must not have deduced return type |
| `extended_lambda_cant_take_function_address` | E | The enclosing parent function (%sq2) for an extended %s1 lambda must allow its address to be taken |
| `extended_lambda_parent_non_extern` | E | On Windows, the enclosing parent function (%sq2) for an extended %s1 lambda cannot have internal or no linkage |
| `extended_lambda_parent_class_unnamed` | E | The enclosing parent function (%sq2) for an extended %s1 lambda cannot be a member function of a class that is unnamed |
| `extended_lambda_parent_template_param_unnamed` | E | The enclosing parent function (%sq2) for an extended %s1 lambda cannot be in a template which has a unnamed parameter: %nd |
| `extended_lambda_nest_parent_template_param_unnamed` | E | The enclosing parent %n for an extended %s lambda cannot be a template which has a unnamed parameter |
| `extended_lambda_multiple_parameter_packs` | E | The enclosing parent template function (%sq2) for an extended %s1 lambda cannot have more than one variadic parameter, or it is not listed last in the template parameter list. |
| `extended_lambda_no_parent_func` | E | *(extended lambda has no parent function)* |
| `extended_lambda_illegal_parent` | E | *(extended lambda in illegal parent context)* |

### Nesting and Context Restrictions

| Tag | Sev | Message |
|---|---|---|
| `extended_lambda_enclosing_function_generic_lambda` | E | An extended %s1 lambda cannot be defined inside a generic lambda expression(%sq2). |
| `extended_lambda_enclosing_function_hd_lambda` | E | An extended %s1 lambda cannot be defined inside an extended \_\_host\_\_ \_\_device\_\_ lambda expression(%sq2). |
| `extended_lambda_inaccessible_ancestor` | E | An extended %s1 lambda cannot be defined inside a class (%sq2) with private or protected access within another class |
| `extended_lambda_inside_constexpr_if` | E | For this host platform/dialect, an extended lambda cannot be defined inside the 'if' or 'else' block of a constexpr if statement |
| `extended_lambda_multiple_parent` | E | Cannot specify multiple \_\_nv\_parent directives in a lambda declaration |
| `extended_host_device_generic_lambda` | E | \_\_host\_\_ \_\_device\_\_ extended lambdas cannot be generic lambdas |
| — | E | If an extended %s lambda is defined within the body of one or more nested lambda expressions, each of these enclosing lambda expressions must be defined within the immediate or nested block scope of a function. |

### Specifier and Annotation

| Tag | Sev | Message |
|---|---|---|
| `extended_lambda_disallowed` | E | \_\_host\_\_ or \_\_device\_\_ annotation on lambda requires --extended-lambda nvcc flag |
| `extended_lambda_constexpr` | E | The %s1 specifier is not allowed for an extended %s2 lambda |
| `lambda_operator_annotated` | E | The operator() function for a lambda cannot be explicitly annotated with execution space annotations (\_\_host\_\_/\_\_device\_\_/\_\_global\_\_), the annotations are derived from its closure class |
| `extended_lambda_discriminator` | E | *(extended lambda discriminator collision)* |

---

## Category 6: Device Code Restrictions

General restrictions that apply to all GPU-side code (`__device__` and `__global__` function bodies).

| Tag | Sev | Message |
|---|---|---|
| `cuda_device_code_unsupported_operator` | E | The operator '%s' is not allowed in device code |
| `unsupported_type_in_device_code` | E | %t %s1 a %s2, which is not supported in device code |
| — | E | device code does not support exception handling |
| `no_coroutine_on_device` | E | device code does not support coroutines |
| — | E | operations on vector types are not supported in device code |
| `undefined_device_entity` | E | cannot use an entity undefined in device code |
| `undefined_device_identifier` | E | identifier %sq is undefined in device code |
| `thread_local_in_device_code` | E | cannot use thread\_local specifier for variable declarations in device code |
| `unrecognized_pragma_device_code` | W | unrecognized #pragma in device code |
| — | E | zero-sized parameter type %t is not allowed in device code |
| — | E | zero-sized variable %sq is not allowed in device code |
| — | E | dynamic initialization is not supported for a function-scope static %s variable within a \_\_device\_\_/\_\_global\_\_ function |
| — | E | function-scope static variable within a \_\_device\_\_/\_\_global\_\_ function requires a memory space specifier |
| `use_of_virtual_base_on_compute_1x` | E | Use of a virtual base (%t) requires the compute\_20 or higher architecture |
| — | E | alloca() is not supported for architectures lower than compute\_52 |

---

## Category 7: Kernel Launch

| Tag | Sev | Message |
|---|---|---|
| `device_launch_no_sepcomp` | E | kernel launch from \_\_device\_\_ or \_\_global\_\_ functions requires separate compilation mode |
| `missing_api_for_device_side_launch` | E | device-side kernel launch could not be processed as the required runtime APIs are not declared |
| — | W | explicit stream argument not provided in kernel launch |
| — | E | kernel launches from templates are not allowed in system files |
| `device_side_launch_arg_with_user_provided_cctor` | E | cannot pass an argument with a user-provided copy-constructor to a device-side kernel launch |
| `device_side_launch_arg_with_user_provided_dtor` | E | cannot pass an argument with a user-provided destructor to a device-side kernel launch |

---

## Category 8: Memory Space and Variable Restrictions

### Variable Access Across Spaces

| Tag | Sev | Message |
|---|---|---|
| `device_var_read_in_host` | E | a %s1 %n1 cannot be directly read in a host function |
| `device_var_written_in_host` | E | a %s1 %n1 cannot be directly written in a host function |
| `device_var_address_taken_in_host` | E | address of a %s1 %n1 cannot be directly taken in a host function |
| `host_var_read_in_device` | E | a host %n1 cannot be directly read in a device function |
| `host_var_written_in_device` | E | a host %n1 cannot be directly written in a device function |
| `host_var_address_taken_in_device` | E | address of a host %n1 cannot be directly taken in a device function |

### Variable Declaration Restrictions

| Tag | Sev | Message |
|---|---|---|
| `illegal_local_to_device_function` | E | %s1 %sq2 variable declaration is not allowed inside a device function body |
| `illegal_local_to_host_function` | E | %s1 %sq2 variable declaration is not allowed inside a host function body |
| `shared_specifier_in_range_for` | E | the \_\_shared\_\_ memory space specifier is not allowed for a variable declared by the for-range-declaration |
| `bad_shared_storage_class` | E | \_\_shared\_\_ variables cannot have external linkage |
| `device_variable_in_unnamed_inline_ns` | E | A %s variable cannot be declared within an inline unnamed namespace |
| — | E | member variables of an anonymous union at global or namespace scope cannot be directly accessed in \_\_device\_\_ and \_\_global\_\_ functions |
| `shared_inside_struct` | E | shared type inside a struct or union is not allowed |
| `shared_parameter` | E | *(\_\_shared\_\_ as function parameter)* |

### Auto-Deduced Device References

| Tag | Sev | Message |
|---|---|---|
| `auto_device_fn_ref` | E | A non-constexpr \_\_device\_\_ function (%sq1) with "auto" deduced return type cannot be directly referenced %s2, except if the reference is absent when \_\_CUDA\_ARCH\_\_ is undefined |
| `device_var_constexpr` | E | *(constexpr rules for \_\_device\_\_ variables)* |
| `device_var_structured_binding` | E | *(structured bindings on \_\_device\_\_ variables)* |

---

## Category 9: \_\_grid\_constant\_\_

The `__grid_constant__` annotation (compute\_70+) marks a kernel parameter as read-only grid-wide.

| Tag | Sev | Message |
|---|---|---|
| `grid_constant_non_kernel` | E | \_\_grid\_constant\_\_ annotation is only allowed on a parameter of a \_\_global\_\_ function |
| `grid_constant_not_const` | E | a parameter annotated with \_\_grid\_constant\_\_ must have const-qualified type |
| `grid_constant_reference_type` | E | a parameter annotated with \_\_grid\_constant\_\_ must not have reference type |
| `grid_constant_unsupported_arch` | E | \_\_grid\_constant\_\_ annotation is only allowed for architecture compute\_70 or later |
| `grid_constant_incompat_redecl` | E | incompatible \_\_grid\_constant\_\_ annotation for parameter %s in function redeclaration (see previous declaration %p) |
| `grid_constant_incompat_templ_redecl` | E | incompatible \_\_grid\_constant\_\_ annotation for parameter %s in function template redeclaration (see previous declaration %p) |
| `grid_constant_incompat_specialization` | E | incompatible \_\_grid\_constant\_\_ annotation for parameter %s in function specialization (see previous declaration %p) |
| `grid_constant_incompat_instantiation_directive` | E | incompatible \_\_grid\_constant\_\_ annotation for parameter %s in instantiation directive (see previous declaration %p) |

---

## Category 10: JIT Mode

JIT mode (`-dc` for device-only compilation) restricts host constructs.

| Tag | Sev | Message |
|---|---|---|
| `no_host_in_jit` | E | A function explicitly marked as a \_\_host\_\_ function is not allowed in JIT mode |
| `unannotated_function_in_jit` | E | A function without execution space annotations (\_\_host\_\_/\_\_device\_\_/\_\_global\_\_) is considered a host function, and host functions are not allowed in JIT mode. Consider using -default-device flag to process unannotated functions as \_\_device\_\_ functions in JIT mode |
| `unannotated_variable_in_jit` | E | A namespace scope variable without memory space annotations (\_\_device\_\_/\_\_constant\_\_/\_\_shared\_\_/\_\_managed\_\_) is considered a host variable, and host variables are not allowed in JIT mode. Consider using -default-device flag to process unannotated namespace scope variables as \_\_device\_\_ variables in JIT mode |
| `unannotated_static_data_member_in_jit` | E | A class static data member with non-const type is considered a host variable, and host variables are not allowed in JIT mode. Consider using -default-device flag to process such data members as \_\_device\_\_ variables in JIT mode |
| `host_closure_class_in_jit` | E | The execution space for the lambda closure class members was inferred to be \_\_host\_\_ (based on context). This is not allowed in JIT mode. Consider using -default-device to infer \_\_device\_\_ execution space for namespace scope lambda closure classes. |

---

## Category 11: RDC / Whole-Program Mode

| Tag | Sev | Message |
|---|---|---|
| — | E | An inline \_\_device\_\_/\_\_constant\_\_/\_\_managed\_\_ variable must have internal linkage when the program is compiled in whole program mode (-rdc=false) |
| `template_global_no_def` | E | when "-static-global-template-stub=true" in whole program compilation mode ("-rdc=false"), a \_\_global\_\_ function template instantiation or specialization (%sq) must have a definition in the current translation unit |
| `extern_kernel_template` | E | when "-static-global-template-stub=true", extern \_\_global\_\_ function template is not supported in whole program compilation mode ("-rdc=false") |
| — | W | address of internal linkage device function (%sq) was taken (nv bug 2001144). mitigation: no mitigation required if the address is not used for comparison, or if the target function is not a CUDA C++ builtin |

---

## Category 12: Atomics

CUDA atomics lowered to PTX instructions with size, type, scope, and memory-order constraints.

### Architecture and Type Constraints

| Tag | Sev | Message |
|---|---|---|
| `nv_atomic_functions_not_supported_below_sm60` | E | \_\_nv\_atomic\_\* functions are not supported on arch < sm\_60. |
| `nv_atomic_operation_not_in_device_function` | E | atomic operations are not in a device function. |
| `nv_atomic_function_no_args` | E | atomic function requires at least one argument. |
| `nv_atomic_function_address_taken` | E | nv atomic function must be called directly. |
| `invalid_nv_atomic_operation_size` | E | atomic operations and, or, xor, add, sub, min and max are valid only on objects of size 4, or 8. |
| `invalid_nv_atomic_cas_size` | E | atomic CAS is valid only on objects of size 2, 4, 8 or 16 bytes. |
| `invalid_nv_atomic_exch_size` | E | atomic exchange is valid only on objects of size 4, 8 or 16 bytes. |
| `invalid_data_size_for_nv_atomic_generic_function` | E | generic nv atomic functions are valid only on objects of size 1, 2, 4, 8 and 16 bytes. |
| `non_integral_type_for_non_generic_nv_atomic_function` | E | non-generic nv atomic load, store, cas and exchange are valid only on integral types. |
| `invalid_nv_atomic_operation_add_sub_size` | E | atomic operations add and sub are not valid on signed integer of size 8. |
| `nv_atomic_add_sub_f64_not_supported` | W | atomic add and sub for 64-bit float is supported on architecture sm\_60 or above. |
| `invalid_nv_atomic_operation_max_min_float` | E | atomic operations min and max are not supported on any floating-point types. |
| `floating_type_for_logical_atomic_operation` | E | For a logical atomic operation, the first argument cannot be any floating-point types. |
| `nv_atomic_cas_b16_not_supported` | E | 16-bit atomic compare-and-exchange is supported on architecture sm\_70 or above. |
| `nv_atomic_exch_cas_b128_not_supported` | E | 128-bit atomic exchange or compare-and-exchange is supported on architecture sm\_90 or above. |
| `nv_atomic_load_store_b128_version_too_low` | E | 128-bit atomic load and store are supported on architecture sm\_70 or above. |

### Memory Order and Scope

| Tag | Sev | Message |
|---|---|---|
| `nv_atomic_load_order_error` | E | atomic load's memory order cannot be release or acq\_rel. |
| `nv_atomic_store_order_error` | E | atomic store's memory order cannot be consume, acquire or acq\_rel. |
| `nv_atomic_operation_order_not_constant_int` | E | atomic operation's memory order argument is not an integer literal. |
| `nv_atomic_operation_scope_not_constant_int` | E | atomic operation's scope argument is not an integer literal. |
| `invalid_nv_atomic_memory_order_value` | E | *(invalid memory order enum value)* |
| `invalid_nv_atomic_thread_scope_value` | E | *(invalid thread scope enum value)* |

### Scope Fallback Warnings

| Tag | Sev | Message |
|---|---|---|
| `nv_atomic_operations_scope_fallback_to_membar` | W | atomic operations' scope argument is supported on architecture sm\_60 or above. Fall back to use membar. |
| `nv_atomic_operations_memory_order_fallback_to_membar` | W | atomic operations' argument of memory order is supported on architecture sm\_70 or above. Fall back to use membar. |
| `nv_atomic_operations_scope_cluster_change_to_device` | W | atomic operations' scope of cluster is supported on architecture sm\_90 or above. Using device scope instead. |
| `nv_atomic_load_store_scope_cluster_change_to_device` | W | atomic load and store's scope of cluster is supported on architecture sm\_90 or above. Using device scope instead. |

---

## Category 13: ASM in Device Code

NVPTX backend supports fewer inline-assembly constraint letters than x86.

| Tag | Sev | Message |
|---|---|---|
| `asm_constraint_letter_not_allowed_in_device` | E | asm constraint letter '%s' is not allowed inside a \_\_device\_\_/\_\_global\_\_ function |
| `asm_constraint_must_have_single_letter` | E | an asm operand may specify only one constraint letter in a \_\_device\_\_/\_\_global\_\_ function |
| — | E | The 'C' constraint can only be used for asm statements in device code |
| `cc_clobber_in_device` | E | The cc clobber constraint is not supported in device code |
| `cuda_xasm_strict_placeholder_format` | E | *(strict placeholder format in CUDA asm)* |
| `addr_of_label_in_device_func` | E | address of label extension is not supported in \_\_device\_\_/\_\_global\_\_ functions |

---

## Category 14: #pragma nv\_abi

Controls calling convention for device functions, adjusting parameter passing to match PTX ABI.

| Tag | Sev | Message |
|---|---|---|
| `nv_abi_pragma_bad_format` | E | *(malformed #pragma nv\_abi)* |
| `nv_abi_pragma_invalid_option` | E | #pragma nv\_abi contains an invalid option |
| `nv_abi_pragma_missing_arg` | E | #pragma nv\_abi requires an argument |
| `nv_abi_pragma_duplicate_arg` | E | #pragma nv\_abi contains a duplicate argument |
| `nv_abi_pragma_not_constant` | E | #pragma nv\_abi argument must evaluate to an integral constant expression |
| `nv_abi_pragma_not_positive_value` | E | #pragma nv\_abi argument value must be a positive value |
| `nv_abi_pragma_overflow_value` | E | #pragma nv\_abi argument value exceeds the range of an integer |
| `nv_abi_pragma_device_function` | E | #pragma nv\_abi must be applied to device functions |
| `nv_abi_pragma_device_function_context` | E | #pragma nv\_abi is not supported inside a host function |
| `nv_abi_pragma_next_construct` | E | #pragma nv\_abi must appear immediately before a function declaration, function definition, or an expression statement |

---

## Category 15: \_\_nv\_register\_params\_\_

Forces all parameters to be passed in registers (compute\_80+).

| Tag | Sev | Message |
|---|---|---|
| `register_params_not_enabled` | E | \_\_nv\_register\_params\_\_ support is not enabled |
| `register_params_unsupported_arch` | E | \_\_nv\_register\_params\_\_ is only supported for compute\_80 or later architecture |
| `register_params_unsupported_function` | E | \_\_nv\_register\_params\_\_ is not allowed on a %s function |
| `register_params_ellipsis_function` | E | \_\_nv\_register\_params\_\_ is not allowed on a function with ellipsis |

---

## Category 16: Name Expression (NVRTC)

`__CUDACC_RTC__name_expr` forms the mangled name of a `__global__` function or `__device__`/`__constant__` variable at compile time.

| Tag | Sev | Message |
|---|---|---|
| `name_expr_parsing` | E | Error in parsing name expression for lowered name lookup. Input name expression was: %sq |
| `name_expr_non_global_routine` | E | Name expression cannot form address of a non-\_\_global\_\_ function. Input name expression was: %sq |
| `name_expr_non_device_variable` | E | Name expression cannot form address of a variable that is not a \_\_device\_\_/\_\_constant\_\_ variable. Input name expression was: %sq |
| `name_expr_not_routine_or_variable` | E | Name expression must form address of a \_\_global\_\_ function or the address of a \_\_device\_\_/\_\_constant\_\_ variable. Input name expression was: %sq |
| `name_expr_extra_tokens` | E | Extra tokens found after parsing name expression for lowered name lookup. Input name expression was: %sq |
| `name_expr_internal_error` | E | Internal error in parsing name expression for lowered name lookup. Input name expression was: %sq |

---

## Category 17: Texture and Surface Variables

| Tag | Sev | Message |
|---|---|---|
| `texture_surface_variable_in_unnamed_inline_ns` | E | A texture or surface variable cannot be declared within an inline unnamed namespace |
| — | E | A texture or surface variable cannot be used in the non-type template argument of a \_\_device\_\_, \_\_host\_\_ \_\_device\_\_ or \_\_global\_\_ function template instantiation |
| `reference_to_text_surf_type_in_device_func` | E | a reference to texture/surface type cannot be used in \_\_device\_\_/\_\_global\_\_ functions |
| `reference_to_text_surf_var_in_device_func` | E | taking reference of texture/surface variable not allowed in \_\_device\_\_/\_\_global\_\_ functions |
| `addr_of_text_surf_var_in_device_func` | E | cannot take address of texture/surface variable %sq in \_\_device\_\_/\_\_global\_\_ functions |
| `addr_of_text_surf_expr_in_device_func` | E | cannot take address of texture/surface expression in \_\_device\_\_/\_\_global\_\_ functions |
| `indir_into_text_surf_var_in_device_func` | E | indirection not allowed for accessing texture/surface through variable %sq in \_\_device\_\_/\_\_global\_\_ functions |
| `indir_into_text_surf_expr_in_device_func` | E | indirection not allowed for accessing texture/surface through expression in \_\_device\_\_/\_\_global\_\_ functions |

---

## Category 18: \_\_managed\_\_ Variables

| Tag | Sev | Message |
|---|---|---|
| `managed_const_type_not_allowed` | E | a \_\_managed\_\_ variable cannot have a const qualified type |
| `managed_reference_type_not_allowed` | E | a \_\_managed\_\_ variable cannot have a reference type |
| `managed_cant_be_shared_constant` | E | \_\_managed\_\_ variables cannot be marked \_\_shared\_\_ or \_\_constant\_\_ |
| `unsupported_arch_for_managed_capability` | E | \_\_managed\_\_ variables require architecture compute\_30 or higher |
| `unsupported_configuration_for_managed_capability` | E | \_\_managed\_\_ variables are not yet supported for this configuration (compilation mode (32/64 bit) and/or target operating system) |
| `decltype_of_managed_variable` | E | A \_\_managed\_\_ variable cannot be used as an unparenthesized id-expression argument for decltype() |

---

## Category 19: Device Function Signature Constraints

| Tag | Sev | Message |
|---|---|---|
| `device_function_has_ellipsis` | E | \_\_device\_\_ or \_\_host\_\_ \_\_device\_\_ function with ellipsis requires compute\_30 or higher architecture |
| `device_func_tex_arg` | E | *(device function with texture argument restriction)* |
| `no_host_device_initializer_list` | E | *(std::initializer\_list in \_\_host\_\_ \_\_device\_\_ context)* |
| `no_host_device_move_forward` | E | *(std::move/forward in \_\_host\_\_ \_\_device\_\_ context)* |
| `no_strict_cuda_error` | W | *(relaxed error checking mode)* |

---

## Category 20: \_\_wgmma\_mma\_async Builtins

Warp Group Matrix Multiply-Accumulate builtins (sm\_90a+).

| Tag | Sev | Message |
|---|---|---|
| `wgmma_mma_async_not_enabled` | E | \_\_wgmma\_mma\_async builtins are only available for sm\_90a |
| `wgmma_mma_async_nonconstant_arg` | E | Non-constant argument to \_\_wgmma\_mma\_async call |
| `wgmma_mma_async_missing_args` | E | The 'A' or 'B' argument to \_\_wgmma\_mma\_async call is missing |
| `wgmma_mma_async_bad_shape` | E | The shape %s is not supported for \_\_wgmma\_mma\_async builtin |
| `wgmma_mma_async_bad_A_type` | E | *(invalid type for operand A)* |
| `wgmma_mma_async_bad_B_type` | E | *(invalid type for operand B)* |

---

## Category 21: \_\_block\_size\_\_ / \_\_cluster\_dims\_\_

Architecture-dependent launch configuration attributes.

| Tag | Sev | Message |
|---|---|---|
| `block_size_unsupported` | E | \_\_block\_size\_\_ is not supported for this GPU architecture |
| `block_size_must_be_positive` | E | *(block size values must be positive)* |
| `cluster_dims_unsupported` | E | \_\_cluster\_dims\_\_ is not supported for this GPU architecture |
| `cluster_dims_must_be_positive` | E | *(\_\_cluster\_dims\_\_ values must be positive)* |
| `cluster_dims_too_large` | E | cluster dimension value is too large |
| `conflict_between_cluster_dim_and_block_size` | E | cannot specify the second tuple in \_\_block\_size\_\_ while \_\_cluster\_dims\_\_ is present |
| `max_blocks_per_cluster_unsupported` | E | cannot specify max blocks per cluster for this GPU architecture |
| `max_blocks_per_cluster_negative` | E | max blocks per cluster must not be negative |
| `max_blocks_per_cluster_too_large` | E | max blocks per cluster is too large |
| `too_many_blocks_in_cluster` | E | total number of blocks in cluster computed from %s exceeds \_\_launch\_bounds\_\_ specified limit for max blocks in cluster |
| `shared_block_size_must_be_positive` | E | the block size of a shared array must be greater than zero |
| `shared_block_size_too_large` | E | *(shared block size exceeds maximum)* |
| `mismatched_shared_block_size` | E | shared block size does not match one previously specified |
| `ambiguous_block_size_spec` | E | *(ambiguous block size specification)* |
| `multiple_block_sizes` | E | multiple block sizes not allowed |
| `threads_dimension_requires_definite_block_size` | E | a dynamic THREADS dimension requires a definite block size |
| `shared_nonthreads_dim` | E | *(shared array dimension is not THREADS-based)* |
| `shared_affinity_type` | E | *(shared affinity type mismatch)* |

---

## Category 22: Inline Hint Conflicts

| Tag | Sev | Message |
|---|---|---|
| `inline_hint_forceinline_conflict` | E | "\_\_inline\_hint\_\_" and "\_\_forceinline\_\_" may not be used on the same declaration |
| `inline_hint_noinline_conflict` | E | "\_\_inline\_hint\_\_" and "\_\_noinline\_\_" may not be used on the same declaration |

---

## Category 23: \_\_local\_maxnreg\_\_

| Tag | Sev | Message |
|---|---|---|
| `local_maxnreg` | E | *(\_\_local\_maxnreg\_\_ attribute applied)* |
| `local_maxnreg_attr_only_nonmember_func` | E | *(\_\_local\_maxnreg\_\_ only on non-member functions)* |
| `local_maxnreg_attribute_conflict` | E | *(\_\_local\_maxnreg\_\_ conflicts with existing attribute)* |
| `local_maxnreg_negative` | E | *(\_\_local\_maxnreg\_\_ value is negative)* |
| `local_maxnreg_too_large` | E | *(\_\_local\_maxnreg\_\_ value exceeds maximum)* |
| `maxnreg_attr_only_nonmember_func` | E | *(\_\_maxnreg\_\_ only on non-member functions)* |
| `bounds_attr_only_nonmember_func` | E | *(launch bounds only on non-member functions)* |

---

## Category 24: Miscellaneous CUDA Errors

| Tag | Sev | Message |
|---|---|---|
| `cuda_displaced_new_or_delete_operator` | E | *(displaced new/delete in CUDA context)* |
| `cuda_demote_unsupported_floating_point` | W | *(unsupported floating-point type demoted)* |
| `illegal_ucn_in_device_identifer` | E | Universal character is not allowed in device entity name (%sq) |
| `thread_local_for_device_vars` | E | cannot use thread\_local specifier for a %s variable |
| `global_qualifier_not_allowed` | E | *(execution space qualifier not allowed here)* |
| `unsupported_nv_attribute` | W | *(unrecognized NVIDIA attribute)* |
| `addr_of_nv_builtin_var` | E | *(address-of applied to NVIDIA builtin variable)* |
| `shared_address_immutable` | E | *(\_\_shared\_\_ variable address is immutable)* |
| `nonshared_blocksizeof` | E | *(BLOCKSIZEOF applied to non-\_\_shared\_\_ variable)* |
| `nonshared_strict_relaxed` | E | *(strict/relaxed qualifier on non-\_\_shared\_\_ variable)* |
| `extern_shared` | W | *(extern \_\_shared\_\_ variable)* |
| `invalid_nvvm_builtin_intrinsic` | E | *(invalid NVVM builtin intrinsic)* |
| `unannotated_static_not_allowed_in_device` | E | *(unannotated static not allowed in device code)* |
| `missing_pushcallconfig` | E | *(cudaConfigureCall not found for kernel launch lowering)* |

---

## Complete Diagnostic Tag Index

All 286 CUDA-specific diagnostic tag names extracted from the cudafe++ binary, organized alphabetically within functional groups. Every tag can be used with `--diag_suppress`, `--diag_warning`, `--diag_error`, or `#pragma nv_diag_suppress` / `nv_diag_warning` / `nv_diag_error`.

### Cross-Space / Execution Space (1 tag)

| # | Tag Name |
|---|---|
| 1 | `unsafe_device_call` |

### Redeclaration (12 tags)

| # | Tag Name |
|---|---|
| 2 | `device_function_redeclared_with_global` |
| 3 | `device_function_redeclared_with_host` |
| 4 | `device_function_redeclared_with_host_device` |
| 5 | `device_function_redeclared_without_device` |
| 6 | `global_function_redeclared_with_device` |
| 7 | `global_function_redeclared_with_host` |
| 8 | `global_function_redeclared_with_host_device` |
| 9 | `global_function_redeclared_without_global` |
| 10 | `host_device_function_redeclared_with_global` |
| 11 | `host_function_redeclared_with_device` |
| 12 | `host_function_redeclared_with_global` |
| 13 | `host_function_redeclared_with_host_device` |

### \_\_global\_\_ Constraints (30 tags)

| # | Tag Name |
|---|---|
| 14 | `bounds_attr_only_on_global_func` |
| 15 | `bounds_maxnreg_incompatible_qualifiers` |
| 16 | `cuda_specifier_twice_in_group` |
| 17 | `global_class_decl` |
| 18 | `global_exception_spec` |
| 19 | `global_friend_definition` |
| 20 | `global_func_local_template_arg` |
| 21 | `global_function_consteval` |
| 22 | `global_function_constexpr` |
| 23 | `global_function_deduced_return_type` |
| 24 | `global_function_has_ellipsis` |
| 25 | `global_function_in_unnamed_inline_ns` |
| 26 | `global_function_inline` |
| 27 | `global_function_multiple_packs` |
| 28 | `global_function_pack_not_last` |
| 29 | `global_function_return_type` |
| 30 | `global_function_with_initializer_list` |
| 31 | `global_lambda_template_arg` |
| 32 | `global_new_or_delete` |
| 33 | `global_operator_function` |
| 34 | `global_param_align_too_big` |
| 35 | `global_private_template_arg` |
| 36 | `global_private_type_arg` |
| 37 | `global_qualifier_not_allowed` |
| 38 | `global_ref_param_restrict` |
| 39 | `global_rvalue_ref_type` |
| 40 | `global_unnamed_type_arg` |
| 41 | `global_va_list_type` |
| 42 | `local_type_used_in_global_function` |
| 43 | `maxnreg_attr_only_on_global_func` |
| 44 | `missing_launch_bounds` |
| 45 | `template_global_no_def` |

### Extended Lambda (38 tags)

| # | Tag Name |
|---|---|
| 46 | `extended_host_device_generic_lambda` |
| 47 | `extended_lambda_array_capture_assignable` |
| 48 | `extended_lambda_array_capture_default_constructible` |
| 49 | `extended_lambda_array_capture_rank` |
| 50 | `extended_lambda_call_operator_local_type` |
| 51 | `extended_lambda_call_operator_private_type` |
| 52 | `extended_lambda_cant_take_function_address` |
| 53 | `extended_lambda_capture_in_constexpr_if` |
| 54 | `extended_lambda_capture_local_type` |
| 55 | `extended_lambda_capture_private_type` |
| 56 | `extended_lambda_constexpr` |
| 57 | `extended_lambda_disallowed` |
| 58 | `extended_lambda_discriminator` |
| 59 | `extended_lambda_enclosing_function_deducible` |
| 60 | `extended_lambda_enclosing_function_generic_lambda` |
| 61 | `extended_lambda_enclosing_function_hd_lambda` |
| 62 | `extended_lambda_enclosing_function_local` |
| 63 | `extended_lambda_enclosing_function_not_found` |
| 64 | `extended_lambda_hd_init_capture` |
| 65 | `extended_lambda_illegal_parent` |
| 66 | `extended_lambda_inaccessible_ancestor` |
| 67 | `extended_lambda_inaccessible_parent` |
| 68 | `extended_lambda_init_capture_array` |
| 69 | `extended_lambda_init_capture_initlist` |
| 70 | `extended_lambda_inside_constexpr_if` |
| 71 | `extended_lambda_multiple_parameter_packs` |
| 72 | `extended_lambda_multiple_parent` |
| 73 | `extended_lambda_nest_parent_template_param_unnamed` |
| 74 | `extended_lambda_no_parent_func` |
| 75 | `extended_lambda_pack_capture` |
| 76 | `extended_lambda_parent_class_unnamed` |
| 77 | `extended_lambda_parent_local_type` |
| 78 | `extended_lambda_parent_non_extern` |
| 79 | `extended_lambda_parent_private_template_arg` |
| 80 | `extended_lambda_parent_private_type` |
| 81 | `extended_lambda_parent_template_param_unnamed` |
| 82 | `extended_lambda_reference_capture` |
| 83 | `extended_lambda_too_many_captures` |
| 84 | `this_addr_capture_ext_lambda` |

### Device Code (13 tags)

| # | Tag Name |
|---|---|
| 85 | `addr_of_label_in_device_func` |
| 86 | `asm_constraint_letter_not_allowed_in_device` |
| 87 | `asm_constraint_must_have_single_letter` |
| 88 | `auto_device_fn_ref` |
| 89 | `cc_clobber_in_device` |
| 90 | `cuda_device_code_unsupported_operator` |
| 91 | `cuda_xasm_strict_placeholder_format` |
| 92 | `illegal_ucn_in_device_identifer` |
| 93 | `no_coroutine_on_device` |
| 94 | `no_strict_cuda_error` |
| 95 | `thread_local_in_device_code` |
| 96 | `undefined_device_entity` |
| 97 | `undefined_device_identifier` |
| 98 | `unrecognized_pragma_device_code` |
| 99 | `unsupported_type_in_device_code` |
| 100 | `use_of_virtual_base_on_compute_1x` |

### Device Function (4 tags)

| # | Tag Name |
|---|---|
| 101 | `device_func_tex_arg` |
| 102 | `device_function_has_ellipsis` |
| 103 | `no_host_device_initializer_list` |
| 104 | `no_host_device_move_forward` |

### Kernel Launch (4 tags)

| # | Tag Name |
|---|---|
| 105 | `device_launch_no_sepcomp` |
| 106 | `device_side_launch_arg_with_user_provided_cctor` |
| 107 | `device_side_launch_arg_with_user_provided_dtor` |
| 108 | `missing_api_for_device_side_launch` |

### Variable Access (11 tags)

| # | Tag Name |
|---|---|
| 109 | `device_var_address_taken_in_host` |
| 110 | `device_var_constexpr` |
| 111 | `device_var_read_in_host` |
| 112 | `device_var_structured_binding` |
| 113 | `device_var_written_in_host` |
| 114 | `device_variable_in_unnamed_inline_ns` |
| 115 | `host_var_address_taken_in_device` |
| 116 | `host_var_read_in_device` |
| 117 | `host_var_written_in_device` |
| 118 | `illegal_local_to_device_function` |
| 119 | `illegal_local_to_host_function` |

### Variable Template (5 tags)

| # | Tag Name |
|---|---|
| 120 | `variable_template_func_local_template_arg` |
| 121 | `variable_template_lambda_template_arg` |
| 122 | `variable_template_private_template_arg` |
| 123 | `variable_template_private_type_arg` |
| 124 | `variable_template_unnamed_type_template_arg` |

### \_\_managed\_\_ (6 tags)

| # | Tag Name |
|---|---|
| 125 | `decltype_of_managed_variable` |
| 126 | `managed_cant_be_shared_constant` |
| 127 | `managed_const_type_not_allowed` |
| 128 | `managed_reference_type_not_allowed` |
| 129 | `unsupported_arch_for_managed_capability` |
| 130 | `unsupported_configuration_for_managed_capability` |

### \_\_grid\_constant\_\_ (8 tags)

| # | Tag Name |
|---|---|
| 131 | `grid_constant_incompat_instantiation_directive` |
| 132 | `grid_constant_incompat_redecl` |
| 133 | `grid_constant_incompat_specialization` |
| 134 | `grid_constant_incompat_templ_redecl` |
| 135 | `grid_constant_non_kernel` |
| 136 | `grid_constant_not_const` |
| 137 | `grid_constant_reference_type` |
| 138 | `grid_constant_unsupported_arch` |

### Atomics (26 tags)

| # | Tag Name |
|---|---|
| 139 | `floating_type_for_logical_atomic_operation` |
| 140 | `invalid_data_size_for_nv_atomic_generic_function` |
| 141 | `invalid_nv_atomic_cas_size` |
| 142 | `invalid_nv_atomic_exch_size` |
| 143 | `invalid_nv_atomic_memory_order_value` |
| 144 | `invalid_nv_atomic_operation_add_sub_size` |
| 145 | `invalid_nv_atomic_operation_max_min_float` |
| 146 | `invalid_nv_atomic_operation_size` |
| 147 | `invalid_nv_atomic_thread_scope_value` |
| 148 | `non_integral_type_for_non_generic_nv_atomic_function` |
| 149 | `nv_atomic_add_sub_f64_not_supported` |
| 150 | `nv_atomic_cas_b16_not_supported` |
| 151 | `nv_atomic_exch_cas_b128_not_supported` |
| 152 | `nv_atomic_function_address_taken` |
| 153 | `nv_atomic_function_no_args` |
| 154 | `nv_atomic_functions_not_supported_below_sm60` |
| 155 | `nv_atomic_load_order_error` |
| 156 | `nv_atomic_load_store_b128_version_too_low` |
| 157 | `nv_atomic_load_store_scope_cluster_change_to_device` |
| 158 | `nv_atomic_operation_not_in_device_function` |
| 159 | `nv_atomic_operation_order_not_constant_int` |
| 160 | `nv_atomic_operation_scope_not_constant_int` |
| 161 | `nv_atomic_operations_memory_order_fallback_to_membar` |
| 162 | `nv_atomic_operations_scope_cluster_change_to_device` |
| 163 | `nv_atomic_operations_scope_fallback_to_membar` |
| 164 | `nv_atomic_store_order_error` |

### JIT Mode (5 tags)

| # | Tag Name |
|---|---|
| 165 | `host_closure_class_in_jit` |
| 166 | `no_host_in_jit` |
| 167 | `unannotated_function_in_jit` |
| 168 | `unannotated_static_data_member_in_jit` |
| 169 | `unannotated_variable_in_jit` |

### RDC / Whole-Program (2 tags)

| # | Tag Name |
|---|---|
| 170 | `extern_kernel_template` |
| 171 | `template_global_no_def` |

### #pragma nv\_abi (10 tags)

| # | Tag Name |
|---|---|
| 172 | `nv_abi_pragma_bad_format` |
| 173 | `nv_abi_pragma_device_function` |
| 174 | `nv_abi_pragma_device_function_context` |
| 175 | `nv_abi_pragma_duplicate_arg` |
| 176 | `nv_abi_pragma_invalid_option` |
| 177 | `nv_abi_pragma_missing_arg` |
| 178 | `nv_abi_pragma_next_construct` |
| 179 | `nv_abi_pragma_not_constant` |
| 180 | `nv_abi_pragma_not_positive_value` |
| 181 | `nv_abi_pragma_overflow_value` |

### \_\_nv\_register\_params\_\_ (4 tags)

| # | Tag Name |
|---|---|
| 182 | `register_params_ellipsis_function` |
| 183 | `register_params_not_enabled` |
| 184 | `register_params_unsupported_arch` |
| 185 | `register_params_unsupported_function` |

### Name Expression (6 tags)

| # | Tag Name |
|---|---|
| 186 | `name_expr_extra_tokens` |
| 187 | `name_expr_internal_error` |
| 188 | `name_expr_non_device_variable` |
| 189 | `name_expr_non_global_routine` |
| 190 | `name_expr_not_routine_or_variable` |
| 191 | `name_expr_parsing` |

### Texture / Surface (7 tags)

| # | Tag Name |
|---|---|
| 192 | `addr_of_text_surf_expr_in_device_func` |
| 193 | `addr_of_text_surf_var_in_device_func` |
| 194 | `indir_into_text_surf_expr_in_device_func` |
| 195 | `indir_into_text_surf_var_in_device_func` |
| 196 | `reference_to_text_surf_type_in_device_func` |
| 197 | `reference_to_text_surf_var_in_device_func` |
| 198 | `texture_surface_variable_in_unnamed_inline_ns` |

### \_\_wgmma\_mma\_async (6 tags)

| # | Tag Name |
|---|---|
| 199 | `wgmma_mma_async_bad_A_type` |
| 200 | `wgmma_mma_async_bad_B_type` |
| 201 | `wgmma_mma_async_bad_shape` |
| 202 | `wgmma_mma_async_missing_args` |
| 203 | `wgmma_mma_async_nonconstant_arg` |
| 204 | `wgmma_mma_async_not_enabled` |

### \_\_block\_size\_\_ / \_\_cluster\_dims\_\_ (18 tags)

| # | Tag Name |
|---|---|
| 205 | `ambiguous_block_size_spec` |
| 206 | `block_size_must_be_positive` |
| 207 | `block_size_unsupported` |
| 208 | `cluster_dims_must_be_positive` |
| 209 | `cluster_dims_too_large` |
| 210 | `cluster_dims_unsupported` |
| 211 | `conflict_between_cluster_dim_and_block_size` |
| 212 | `max_blocks_per_cluster_negative` |
| 213 | `max_blocks_per_cluster_too_large` |
| 214 | `max_blocks_per_cluster_unsupported` |
| 215 | `mismatched_shared_block_size` |
| 216 | `multiple_block_sizes` |
| 217 | `shared_affinity_type` |
| 218 | `shared_block_size_must_be_positive` |
| 219 | `shared_block_size_too_large` |
| 220 | `shared_nonthreads_dim` |
| 221 | `threads_dimension_requires_definite_block_size` |
| 222 | `too_many_blocks_in_cluster` |

### Inline Hint (2 tags)

| # | Tag Name |
|---|---|
| 223 | `inline_hint_forceinline_conflict` |
| 224 | `inline_hint_noinline_conflict` |

### \_\_local\_maxnreg\_\_ (7 tags)

| # | Tag Name |
|---|---|
| 225 | `bounds_attr_only_nonmember_func` |
| 226 | `local_maxnreg` |
| 227 | `local_maxnreg_attr_only_nonmember_func` |
| 228 | `local_maxnreg_attribute_conflict` |
| 229 | `local_maxnreg_negative` |
| 230 | `local_maxnreg_too_large` |
| 231 | `maxnreg_attr_only_nonmember_func` |

### Lambda Annotation (1 tag)

| # | Tag Name |
|---|---|
| 232 | `lambda_operator_annotated` |

### Miscellaneous (16 tags)

| # | Tag Name |
|---|---|
| 233 | `addr_of_nv_builtin_var` |
| 234 | `bad_shared_storage_class` |
| 235 | `cuda_demote_unsupported_floating_point` |
| 236 | `cuda_displaced_new_or_delete_operator` |
| 237 | `extern_shared` |
| 238 | `invalid_nvvm_builtin_intrinsic` |
| 239 | `missing_pushcallconfig` |
| 240 | `nonshared_blocksizeof` |
| 241 | `nonshared_strict_relaxed` |
| 242 | `shared_address_immutable` |
| 243 | `shared_inside_struct` |
| 244 | `shared_parameter` |
| 245 | `shared_specifier_in_range_for` |
| 246 | `thread_local_for_device_vars` |
| 247 | `unannotated_static_not_allowed_in_device` |
| 248 | `unsupported_nv_attribute` |

### Diagnostic Pragma Actions (6 tags — not suppressible, but listed for completeness)

| # | Tag Name |
|---|---|
| 249 | `nv_diag_default` |
| 250 | `nv_diag_error` |
| 251 | `nv_diag_once` |
| 252 | `nv_diag_remark` |
| 253 | `nv_diag_suppress` |
| 254 | `nv_diag_warning` |

---

## Cross-Reference: EDG Error Codes Used for CUDA

The following standard EDG error codes (0--3456) are repurposed or frequently triggered by CUDA-specific validation. These display with their original number (not the 20000-D series).

| Internal # | Display # | Context |
|---|---|---|
| 21 | 21 | CUDA auto type with template deduction |
| 147 | 147 | redeclaration mismatch |
| 149 | 149 | illegal CUDA storage class at namespace scope |
| 246 | 246 | static member of non-class type |
| 298 | 298 | typedef/using with template name |
| 325 | 325 | thread\_local in CUDA |
| 337 | 337 | calling convention mismatch |
| 453 | 453 | in template instantiation context |
| 551 | 551 | not a member function |
| 795 | 795 | definition in class scope with external linkage (CUDA) |
| 799 | 799 | definition in class scope (C++20 CUDA) |
| 891 | 891 | anonymous type in variable declaration |
| 892 | 892 | auto with \_\_constant\_\_ variable |
| 893 | 893 | auto with CUDA variable |
| 948 | 948 | calling convention mismatch on redeclaration |
| 992 | 992 | fatal error (suppress-all sentinel) |
| 1034 | 1034 | explicit instantiation with conflicting attributes |
| 1063 | 1063 | in include file context |
| 1118 | 1118 | CUDA attribute on namespace-scope variable |
| 1150 | 1150 | context lines truncated |
| 1158 | 1158 | auto return type with \_\_global\_\_ |
| 1306 | 1306 | CUDA memory space mismatch on redeclaration |
| 1418 | 1418 | incomplete type in definition |
| 1430 | 1430 | function attribute mismatch in template |
| 1560 | 1560 | CUDA constexpr class with non-trivial destructor |
| 1580 | 1580 | redeclaration with different template parameters |
| 1655 | 1655 | tentative definition of constexpr |
| 2384 | 2384 | constexpr mismatch on redeclaration (CUDA) |
| 2442 | 2442 | extern variable at block scope with CUDA attribute |
| 2443 | 2443 | extern variable at block scope with CUDA attribute (variant) |
| 2502 | 2502 | no\_unique\_address mismatch |
| 2503 | 2503 | no\_unique\_address mismatch (variant) |
| 2656 | 2656 | internal error (assertion failure) |
| 2885 | 2885 | CUDA attribute on deduction guide |
| 2937 | 2937 | structured binding with CUDA attribute |
| 3033 | 3033 | incompatible constexpr CUDA target |
| 3116 | 3116 | restrict qualifier on definition |
| 3414 | 3414 | auto with volatile/atomic qualifier |
| 3510 | 3510 | \_\_shared\_\_ variable with VLA |
| 3566 | 3566 | \_\_constant\_\_ with constexpr with auto |
| 3567 | 3567 | CUDA variable with VLA type |
| 3568 | 3568 | \_\_constant\_\_ with constexpr |
| 3578 | 3578 | CUDA attribute in discarded constexpr-if branch |
| 3579 | 3579 | CUDA attribute at namespace scope with structured binding |
| 3580 | 3580 | CUDA attribute on variable-length array |
| 3648 | 3648 | CUDA \_\_constant\_\_ with external linkage |
| 3698 | 3698 | parameter type mismatch |
| 3709 | 3709 | warnings treated as errors |

---

## Format Specifiers in CUDA Messages

CUDA error messages use the same fill-in system as EDG base errors, expanded by `process_fill_in` (`sub_4EDCD0`).

| Specifier | Kind | Meaning | Example |
|---|---|---|---|
| `%sq` | 3 | Quoted entity name | Function name in cross-space call |
| `%sq1`, `%sq2` | 3 | Indexed quoted names | Caller and callee |
| `%no1` | 4 | Entity name (omit kind prefix) | Function in redeclaration |
| `%n1`, `%n2` | 4 | Entity names | Override base/derived pair |
| `%nd` | 4 | Entity with declaration location | Template parameter |
| `%s`, `%s1`, `%s2` | 3 | String fill-in | Execution space keyword |
| `%t` | 6 | Type fill-in | Type in template arg errors |
| `%p` | 2 | Source position | Previous declaration location |

---

## Architecture Requirements Summary

Quick reference for minimum architecture required by various CUDA features.

| Feature | Minimum Architecture |
|---|---|
| Virtual bases | compute\_20 |
| `__device__`/`__host__ __device__` with ellipsis | compute\_30 |
| `__managed__` variables | compute\_30 |
| alloca() | compute\_52 |
| `__nv_atomic_*` functions | sm\_60 |
| Atomic scope argument | sm\_60 |
| Atomic add/sub for f64 | sm\_60 |
| `__grid_constant__` | compute\_70 |
| Atomic memory order argument | sm\_70 |
| 16-bit atomic CAS | sm\_70 |
| 128-bit atomic load/store | sm\_70 |
| `__nv_register_params__` | compute\_80 |
| Cluster scope for atomics | sm\_90 |
| 128-bit atomic exchange/CAS | sm\_90 |
| `__wgmma_mma_async` builtins | sm\_90a |

---

## EDG Diagnostic Namespace (outside this catalog)

Beyond the 286 CUDA-extension tags above, the cudafe++ binary embeds approximately **573 additional snake_case diagnostic tags** belonging to EDG 6.6's standard C++ frontend. They share the same suppression machinery — `--diag_suppress=<tag>`, `#pragma nv_diag_suppress <tag>`, numeric `#pragma nv_diag_suppress <N>` for codes 0--3456 — but live in the original error-code range and are emitted without the `20000-D` renumbering.

The table below summarizes the top category prefixes harvested from the binary's string table. Counts are tag totals per prefix; the *role* column describes what the prefix governs.

| Category prefix | Tags | Role |
|---|---:|---|
| `invalid_` | 150 | Structural validity (invalid declarator, invalid attribute location, invalid asm qualifier, ...) |
| `constexpr_` | 112 | Constexpr interpreter errors (access past object, allocation mismatch, call not interpretable, undefined behaviour, ...) |
| `type_` | 56 | Type-system inconsistencies (conflicts-with-tag, after-integral-promotion, constraint-failed, ...) |
| `template_` | 48 | Template engine diagnostics (instance linkage conflict, arg involves error entity, depth mismatch, ...) |
| `function_` | 34 | Function declaration/definition issues (body processing, defaulted in friend decl, does-not-match-arguments, ...) |
| `ambiguous_` | 28 | Overload and lookup ambiguity (constructor, conversion function, partial spec, virtual override, ...) |
| `class_` | 26 | Class-layout and inheritance constraints |
| `module_` | 24 | C++20 modules support (import conflict, file-not-found, declaration-position, primary-name, ...) |
| `attribute_` | 24 | GCC/MSVC/standard attribute application rules (cleanup-storage, condition-satisfied, ignored-on-incomplete-class, ...) |
| `static_` | 21 | Static specifier and linkage |
| `virtual_` | 19 | Virtual function rules |
| `symbol_` / `return_` | 19 / 19 | Symbol resolution; return-type mismatches |
| `member_` / `implicit_` | 18 / 18 | Member access; implicit conversion / construction |
| `struct_` / `pragma_` / `lambda_` / `enum_` | 15 each | Struct layout; `#pragma` parsing; lambda restrictions; enum constraints |

> ⚡ **QUIRK — diagnostic tags survive renumbering**
> A CUDA error displayed as `#20042-D` and an EDG error displayed as `#147` are both suppressible by *tag name* through the same global lookup. Internally cudafe++ resolves the tag string against all 859 entries before the per-error-code severity arrays (`byte_1067920` / `byte_1067921` / `byte_1067922`) are consulted, so users never need to know whether a tag belongs to the CUDA extension range or the EDG base range. The numeric `#pragma nv_diag_suppress 20042` form silently subtracts 16543 before indexing the severity arrays.

The EDG-base namespace is intentionally not enumerated tag-by-tag in this wiki: the upstream catalog is maintained by EDG and tags are largely stable across EDG 6.x point releases. For a deep dive into specific subsystems consult:

* [Constexpr Interpreter](../edg/constexpr-interpreter.md) — for the 112 `constexpr_*` tags.
* [Template Engine](../edg/template-engine.md) and [CUDA Template Restrictions](../edg/template-cuda.md) — for `template_*`.
* [Declaration Parser](../edg/declaration-parser.md) — for `invalid_*` declarator and attribute diagnostics.
* [Overload Resolution](../edg/overload-resolution.md) — for `ambiguous_*`.
* [Pragma Engine](../edg/pragma-engine.md) — for `pragma_*` and the suppression machinery.

Confidence: **MED** — prefix bucketing is mechanical, but a handful of tags straddle categories (e.g. `module_id_for_source_corresp` could be either modules or RDC).
