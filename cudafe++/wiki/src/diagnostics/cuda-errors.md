# CUDA Error Catalog

cudafe++ reserves error indices 3457--3794 for CUDA-specific diagnostics. These 338 slots are displayed to the user as error numbers 20000--20337 with a `-D` suffix (for suppressible severities), produced by the renumbering logic in `construct_text_message` (`sub_4EF9D0`): when the internal error code exceeds 3456, the display code is `error_code + 16543`. Of the 338 slots, approximately 210 carry unique error message templates; the remainder are reserved or share templates with parametric fill-ins (`%s`, `%sq`, `%t`, `%n`, `%no`). Every CUDA error can be suppressed, promoted, or demoted by its **diagnostic tag name** via `--diag_suppress`, `--diag_warning`, `--diag_error`, or the `#pragma nv_diagnostic` system.

This page is a searchable reference catalog organized by error category for the **CUDA extension namespace only**. The remaining ~573 diagnostic tags inherited from EDG 6.6 (constexpr interpreter, template engine, modules, attributes, overload resolution, type system) follow the same suppression machinery but use the original error-code range; for a category-prefix summary of that EDG-base namespace see [Error Message Catalog § EDG Diagnostic Namespace](../reference/error-catalog.md#edg-diagnostic-namespace-outside-this-catalog). For the diagnostic pipeline mechanics (severity levels, pragma stack, output formatting), see [Diagnostic Overview](./overview.md).

## Error Numbering Scheme

```c
// construct_text_message (sub_4EF9D0), error.c:3153
int display_code = error_code;
if (display_code > 3456)
    display_code = error_code + 16543;   // 3457 -> 20000, 3794 -> 20337
sprintf(buf, "%d", display_code);

// Suffix: "-D" appended when severity <= 7 (note, remark, warning, soft error)
const char *suffix = (severity > 7) ? "" : "-D";
```

**User-visible format:** `file(line): error #20042-D: calling a __device__ function from a __host__ function is not allowed`

**Mapping formula:**

| Direction | Formula |
|---|---|
| Display to internal | `internal = display - 16543` (for display >= 20000) |
| Internal to display | `display = internal + 16543` (for internal > 3456) |

## Diagnostic Tag Names and Suppression

Each CUDA error has an associated **diagnostic tag name** -- a snake_case identifier that can be passed to `--diag_suppress`, `--diag_warning`, `--diag_error`, or `--diag_default` instead of the numeric code. The tag names are also accepted by `#pragma nv_diag_suppress`, `#pragma nv_diag_warning`, etc.

```bash
# Suppress a specific CUDA error by tag name
nvcc --diag_suppress=calling_a_constexpr__host__function_from_a__device__function

# Suppress by numeric code (equivalent)
nvcc --diag_suppress=20042

# In source code
#pragma nv_diag_suppress device_function_redeclared_with_host
```

The pragma actions understood by cudafe++:

| Pragma | Internal Code | Effect |
|---|---|---|
| `nv_diag_suppress` | 30 | Set severity to 3 (suppressed) |
| `nv_diag_remark` | 31 | Set severity to 4 (remark) |
| `nv_diag_warning` | 32 | Set severity to 5 (warning) |
| `nv_diag_error` | 33 | Set severity to 7 (error) |
| `nv_diag_default` | 35 | Restore original severity |
| `nv_diag_once` | -- | Emit only on first occurrence |

## Category 1: Cross-Space Calling (12 messages)

Cross-space call validation is the highest-frequency CUDA diagnostic category. The checker walks the call graph and emits an error whenever a function in one execution space calls a function in an incompatible space. Six variants cover non-constexpr calls; six more cover constexpr calls (which can be relaxed with `--expt-relaxed-constexpr`).

### Standard Cross-Space Calls

| Tag | Message Template |
|---|---|
| `unsafe_device_call` | `calling a __device__ function(%sq1) from a __host__ function(%sq2) is not allowed` |
| `unsafe_device_call` | `calling a __device__ function(%sq1) from a __host__ __device__ function(%sq2) is not allowed` |
| `unsafe_device_call` | `calling a __host__ function(%sq1) from a __device__ function(%sq2) is not allowed` |
| `unsafe_device_call` | `calling a __host__ function(%sq1) from a __global__ function(%sq2) is not allowed` |
| `unsafe_device_call` | `calling a __host__ function(%sq1) from a __host__ __device__ function(%sq2) is not allowed` |
| `unsafe_device_call` | `calling a __host__ function from a __host__ __device__ function is not allowed` |

### Constexpr Cross-Space Calls

These fire when `--expt-relaxed-constexpr` is not enabled. The message explicitly suggests the flag.

| Tag | Message Template |
|---|---|
| `unsafe_device_call` | `calling a constexpr __device__ function(%sq1) from a __host__ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |
| `unsafe_device_call` | `calling a constexpr __device__ function(%sq1) from a __host__ __device__ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |
| `unsafe_device_call` | `calling a constexpr __host__ function(%sq1) from a __device__ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |
| `unsafe_device_call` | `calling a constexpr __host__ function(%sq1) from a __global__ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |
| `unsafe_device_call` | `calling a constexpr __host__ function(%sq1) from a __host__ __device__ function(%sq2) is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |
| `unsafe_device_call` | `calling a constexpr __host__ function from a __host__ __device__ function is not allowed. The experimental flag '--expt-relaxed-constexpr' can be used to allow this.` |

**Implementation:** Cross-space checks are performed by the call-graph walker in the CUDA validation pass. The checker compares the execution space byte at entity offset `+182` of the callee against the caller. When the mask test fails, the appropriate variant is selected based on whether either function is constexpr and whether the callee has named fill-ins or uses the anonymous (no `%sq`) form.

## Category 2: Virtual Override Mismatch (6 messages)

When a derived class overrides a virtual function, the execution space of the override must match the base. Six combinations cover all mismatched pairs among `__host__`, `__device__`, and `__host__ __device__`.

| Tag | Message Template |
|---|---|
| -- | `execution space mismatch: overridden entity (%n1) is a __device__ function, but overriding entity (%n2) is a __host__ function` |
| -- | `execution space mismatch: overridden entity (%n1) is a __device__ function, but overriding entity (%n2) is a __host__ __device__ function` |
| -- | `execution space mismatch: overridden entity (%n1) is a __host__ function, but overriding entity (%n2) is a __device__ function` |
| -- | `execution space mismatch: overridden entity (%n1) is a __host__ function, but overriding entity (%n2) is a __host__ __device__ function` |
| -- | `execution space mismatch: overridden entity (%n1) is a __host__ __device__ function, but overriding entity (%n2) is a __device__ function` |
| -- | `execution space mismatch: overridden entity (%n1) is a __host__ __device__ function, but overriding entity (%n2) is a __host__ function` |

**Implementation:** The override checker (`sub_432280`, `record_virtual_function_override`) extracts the `0x30` mask from the execution space byte of both the base and derived function entities. If they differ, the appropriate pair is selected and emitted. The `__global__` space is not included because `__global__` functions cannot be virtual (see Category 4).

## Category 3: Redeclaration Mismatch (12 messages)

When a function is redeclared with a different execution space annotation, cudafe++ either emits an error (incompatible combination) or a warning (compatible promotion to `__host__ __device__`).

### Error-Level Redeclarations (4 messages)

| Tag | Message Template |
|---|---|
| `device_function_redeclared_with_global` | `a __device__ function(%no1) redeclared with __global__` |
| `global_function_redeclared_with_device` | `a __global__ function(%no1) redeclared with __device__` |
| `global_function_redeclared_with_host` | `a __global__ function(%no1) redeclared with __host__` |
| `global_function_redeclared_with_host_device` | `a __global__ function(%no1) redeclared with __host__ __device__` |

### Warning-Level Redeclarations (Promoted to HD, 5 messages)

| Tag | Message Template |
|---|---|
| `device_function_redeclared_with_host` | `a __device__ function(%no1) redeclared with __host__, hence treated as a __host__ __device__ function` |
| `device_function_redeclared_with_host_device` | `a __device__ function(%no1) redeclared with __host__ __device__, hence treated as a __host__ __device__ function` |
| `device_function_redeclared_without_device` | `a __device__ function(%no1) redeclared without __device__, hence treated as a __host__ __device__ function` |
| `host_function_redeclared_with_device` | `a __host__ function(%no1) redeclared with __device__, hence treated as a __host__ __device__ function` |
| `host_function_redeclared_with_host_device` | `a __host__ function(%no1) redeclared with __host__ __device__, hence treated as a __host__ __device__ function` |

### Global Redeclarations (3 messages)

| Tag | Message Template |
|---|---|
| `global_function_redeclared_without_global` | `a __global__ function(%no1) redeclared without __global__` |
| `host_function_redeclared_with_global` | `a __host__ function(%no1) redeclared with __global__` |
| `host_device_function_redeclared_with_global` | `a __host__ __device__ function(%no1) redeclared with __global__` |

**Implementation:** Redeclaration checking occurs in `decl_routine` (`sub_4CE420`) and `check_cuda_attribute_consistency` (`sub_4C6D50`). The checker compares the execution space byte from the prior declaration against the new declaration's attribute set. When bits differ, it selects the message based on which bits changed and whether the result is a compatible promotion.

## Category 4: \_\_global\_\_ Function Constraints (37 messages)

`__global__` (kernel) functions have the most extensive constraint set of any execution space. These errors enforce the CUDA programming model requirement that kernels have specific signatures, cannot be members, and cannot use certain C++ features.

### Return Type and Signature

| Tag | Message Template |
|---|---|
| `global_function_return_type` | `a __global__ function must have a void return type` |
| `global_function_deduced_return_type` | `a __global__ function must not have a deduced return type` |
| `global_function_has_ellipsis` | `a __global__ function cannot have ellipsis` |
| `global_rvalue_ref_type` | `a __global__ function cannot have a parameter with rvalue reference type` |
| `global_ref_param_restrict` | `a __global__ function cannot have a parameter with __restrict__ qualified reference type` |
| `global_va_list_type` | `A __global__ function or function template cannot have a parameter with va_list type` |
| `global_function_with_initializer_list` | `a __global__ function or function template cannot have a parameter with type std::initializer_list` |
| `global_param_align_too_big` | `cannot pass a parameter with a too large explicit alignment to a __global__ function on win32 platforms` |

### Declaration Context

| Tag | Message Template |
|---|---|
| `global_class_decl` | `A __global__ function or function template cannot be a member function` |
| `global_friend_definition` | `A __global__ function or function template cannot be defined in a friend declaration` |
| `global_function_in_unnamed_inline_ns` | `A __global__ function or function template cannot be declared within an inline unnamed namespace` |
| `global_operator_function` | `An operator function cannot be a __global__ function` |
| `global_new_or_delete` | *(internal -- __global__ on operator new/delete)* |
| -- | `function main cannot be marked __device__ or __global__` |

### C++ Feature Restrictions

| Tag | Message Template |
|---|---|
| `global_function_constexpr` | `A __global__ function or function template cannot be marked constexpr` |
| `global_function_consteval` | `A __global__ function or function template cannot be marked consteval` |
| `global_function_inline` | *(internal -- __global__ with inline)* |
| `global_exception_spec` | `An exception specification is not allowed for a __global__ function or function template` |

### Template Argument Restrictions

| Tag | Message Template |
|---|---|
| `global_private_type_arg` | `A type that is defined inside a class and has private or protected access (%t) cannot be used in the template argument type of a __global__ function template instantiation, unless the class is local to a __device__ or __global__ function` |
| `global_private_template_arg` | `A template that is defined inside a class and has private or protected access cannot be used in the template template argument of a __global__ function template instantiation` |
| `global_unnamed_type_arg` | `An unnamed type (%t) cannot be used in the template argument type of a __global__ function template instantiation, unless the type is local to a __device__ or __global__ function` |
| `global_func_local_template_arg` | `A type defined inside a __host__ function (%t) cannot be used in the template argument type of a __global__ function template instantiation` |
| `global_lambda_template_arg` | `The closure type for a lambda (%t%s) cannot be used in the template argument type of a __global__ function template instantiation, unless the lambda is defined within a __device__ or __global__ function, or the flag '-extended-lambda' is specified and the lambda is an extended lambda (a __device__ or __host__ __device__ lambda defined within a __host__ or __host__ __device__ function)` |
| `local_type_used_in_global_function` | `a local type %t (defined in %sq1) used in global function %sq2 template argument, the global function cannot be launched from host code.` |

### Variadic Template Constraints

| Tag | Message Template |
|---|---|
| `global_function_multiple_packs` | `Multiple pack parameters are not allowed for a variadic __global__ function template` |
| `global_function_pack_not_last` | `Pack template parameter must be the last template parameter for a variadic __global__ function template` |

### Variable Template Restrictions (parallel to kernel template)

| Tag | Message Template |
|---|---|
| `variable_template_private_type_arg` | `A type that is defined inside a class and has private or protected access (%t) cannot be used in the template argument type of a variable template instantiation, unless the class is local to a __device__ or __global__ function` |
| `variable_template_private_template_arg` | *(private template template arg in variable template)* |
| `variable_template_unnamed_type_template_arg` | `An unnamed type (%t) cannot be used in the template argument type of a variable template template instantiation, unless the type is local to a __device__ or __global__ function` |
| `variable_template_func_local_template_arg` | `A type defined inside a __host__ function (%t) cannot be used in the template argument type of a variable template template instantiation` |
| `variable_template_lambda_template_arg` | `The closure type for a lambda (%t%s) cannot be used in the template argument type of a variable template instantiation, unless the lambda is defined within a __device__ or __global__ function, or the lambda is an 'extended lambda' and the flag --extended-lambda is specified` |

### Launch Configuration Attributes

| Tag | Message Template |
|---|---|
| `bounds_attr_only_on_global_func` | `%s is only allowed on a __global__ function` |
| `maxnreg_attr_only_on_global_func` | *(maxnreg only on __global__)* |
| -- | `The %s qualifiers cannot be applied to the same kernel` |
| -- | `Multiple %s specifiers are not allowed` |
| -- | `no __launch_bounds__ specified for __global__ function` |
| `cuda_specifier_twice_in_group` | *(duplicate CUDA specifier on same declaration)* |

## Category 5: Extended Lambda Restrictions (35 messages)

Extended lambdas (`__device__` or `__host__ __device__` lambdas defined within host code, enabled by `--extended-lambda`) are one of the most constraint-heavy features in CUDA. The restriction set enforces that the lambda's closure type can be serialized for device transfer.

### Capture Restrictions

| Tag | Message Template |
|---|---|
| `extended_lambda_reference_capture` | `An extended %s lambda cannot capture variables by reference` |
| `extended_lambda_pack_capture` | `An extended %s lambda cannot capture an element of a parameter pack` |
| `extended_lambda_too_many_captures` | `An extended %s lambda can only capture up to 1023 variables` |
| `extended_lambda_array_capture_rank` | `An extended %s lambda cannot capture an array variable (type: %t) with more than 7 dimensions` |
| `extended_lambda_array_capture_assignable` | `An extended %s lambda cannot capture an array variable whose element type (%t) is not assignable on the host` |
| `extended_lambda_array_capture_default_constructible` | `An extended %s lambda cannot capture an array variable whose element type (%t) is not default constructible on the host` |
| `extended_lambda_init_capture_array` | `An extended %s lambda cannot init-capture variables with array type` |
| `extended_lambda_init_capture_initlist` | `An extended %s lambda cannot have init-captures with type std::initializer_list` |
| `extended_lambda_capture_in_constexpr_if` | `An extended %s lambda cannot first-capture variable in constexpr-if context` |
| `this_addr_capture_ext_lambda` | `Implicit capture of 'this' in extended lambda expression` |
| `extended_lambda_hd_init_capture` | `init-captures are not allowed for extended __host__ __device__ lambdas` |
| -- | `Unless enabled by language dialect, *this capture is only supported when the lambda is either __device__ only, or is defined within a __device__ or __global__ function` |

### Type Restrictions on Captures and Parameters

| Tag | Message Template |
|---|---|
| `extended_lambda_capture_local_type` | `A type local to a function (%t) cannot be used in the type of a variable captured by an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_capture_private_type` | `A type that is a private or protected class member (%t) cannot be used in the type of a variable captured by an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_call_operator_local_type` | `A type local to a function (%t) cannot be used in the return or parameter types of the operator() of an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_call_operator_private_type` | `A type that is a private or protected class member (%t) cannot be used in the return or parameter types of the operator() of an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_parent_local_type` | `A type local to a function (%t) cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_parent_private_type` | `A type that is a private or protected class member (%t) cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended __device__ or __host__ __device__ lambda` |
| `extended_lambda_parent_private_template_arg` | `A template that is a private or protected class member cannot be used in the template argument of the enclosing parent function (and any parent classes) of an extended %s lambda` |

### Enclosing Parent Function Restrictions

| Tag | Message Template |
|---|---|
| `extended_lambda_enclosing_function_local` | `The enclosing parent function (%sq2) for an extended %s1 lambda must not be defined inside another function` |
| `extended_lambda_inaccessible_parent` | `The enclosing parent function (%sq2) for an extended %s1 lambda cannot have private or protected access within its class` |
| `extended_lambda_enclosing_function_deducible` | `The enclosing parent function (%sq2) for an extended %s1 lambda must not have deduced return type` |
| `extended_lambda_cant_take_function_address` | `The enclosing parent function (%sq2) for an extended %s1 lambda must allow its address to be taken` |
| `extended_lambda_parent_non_extern` | `On Windows, the enclosing parent function (%sq2) for an extended %s1 lambda cannot have internal or no linkage` |
| `extended_lambda_parent_class_unnamed` | `The enclosing parent function (%sq2) for an extended %s1 lambda cannot be a member function of a class that is unnamed` |
| `extended_lambda_parent_template_param_unnamed` | `The enclosing parent function (%sq2) for an extended %s1 lambda cannot be in a template which has a unnamed parameter: %nd` |
| `extended_lambda_nest_parent_template_param_unnamed` | `The enclosing parent %n for an extended %s lambda cannot be a template which has a unnamed parameter` |
| `extended_lambda_multiple_parameter_packs` | `The enclosing parent template function (%sq2) for an extended %s1 lambda cannot have more than one variadic parameter, or it is not listed last in the template parameter list.` |

### Nesting and Context Restrictions

| Tag | Message Template |
|---|---|
| `extended_lambda_enclosing_function_generic_lambda` | `An extended %s1 lambda cannot be defined inside a generic lambda expression(%sq2).` |
| `extended_lambda_enclosing_function_hd_lambda` | `An extended %s1 lambda cannot be defined inside an extended __host__ __device__  lambda expression(%sq2).` (note: double space before "lambda" is present in the binary) |
| `extended_lambda_inaccessible_ancestor` | `An extended %s1 lambda cannot be defined inside a class (%sq2) with private or protected access within another class` |
| `extended_lambda_inside_constexpr_if` | `For this host platform/dialect, an extended lambda cannot be defined inside the 'if' or 'else' block of a constexpr if statement` |
| `extended_lambda_multiple_parent` | `Cannot specify multiple __nv_parent directives in a lambda declaration` |
| `extended_host_device_generic_lambda` | `__host__ __device__ extended lambdas cannot be generic lambdas` |
| -- | `If an extended %s lambda is defined within the body of one or more nested lambda expressions, each of these enclosing lambda expressions must be defined within the immediate or nested block scope of a function.` |

### Specifier and Annotation

| Tag | Message Template |
|---|---|
| `extended_lambda_disallowed` | `__host__ or __device__ annotation on lambda requires --extended-lambda nvcc flag` |
| `extended_lambda_constexpr` | `The %s1 specifier is not allowed for an extended %s2 lambda` |
| -- | `The operator() function for a lambda cannot be explicitly annotated with execution space annotations (__host__/__device__/__global__), the annotations are derived from its closure class` |

## Category 6: Device Code Restrictions (13 messages)

General restrictions that apply to any code executing on the GPU. These errors are emitted when C++ features unsupported by the NVPTX backend appear in `__device__` or `__global__` function bodies.

| Tag | Message Template |
|---|---|
| `cuda_device_code_unsupported_operator` | `The operator '%s' is not allowed in device code` |
| `unsupported_type_in_device_code` | `%t %s1 a %s2, which is not supported in device code` |
| -- | `device code does not support exception handling` |
| -- | `device code does not support coroutines` |
| -- | `operations on vector types are not supported in device code` |
| `undefined_device_entity` | `cannot use an entity undefined in device code` |
| `undefined_device_identifier` | `identifier %sq is undefined in device code` |
| `thread_local_in_device_code` | `cannot use thread_local specifier for variable declarations in device code` |
| `unrecognized_pragma_device_code` | `unrecognized #pragma in device code` |
| -- | `zero-sized parameter type %t is not allowed in device code` |
| -- | `zero-sized variable %sq is not allowed in device code` |
| -- | `dynamic initialization is not supported for a function-scope static %s variable within a __device__/__global__ function` |
| -- | `function-scope static variable within a __device__/__global__ function requires a memory space specifier` |

## Category 7: Kernel Launch (6 messages)

Errors related to `<<<...>>>` kernel launch syntax.

| Tag | Message Template |
|---|---|
| `device_launch_no_sepcomp` | `kernel launch from __device__ or __global__ functions requires separate compilation mode` |
| `missing_api_for_device_side_launch` | `device-side kernel launch could not be processed as the required runtime APIs are not declared` |
| -- | `explicit stream argument not provided in kernel launch` |
| -- | `kernel launches from templates are not allowed in system files` |
| `device_side_launch_arg_with_user_provided_cctor` | `cannot pass an argument with a user-provided copy-constructor to a device-side kernel launch` |
| `device_side_launch_arg_with_user_provided_dtor` | `cannot pass an argument with a user-provided destructor to a device-side kernel launch` |

## Category 8: Memory Space and Variable Restrictions (15 messages)

### Variable Access Across Spaces

| Tag | Message Template |
|---|---|
| `device_var_read_in_host` | `a %s1 %n1 cannot be directly read in a host function` |
| `device_var_written_in_host` | `a %s1 %n1 cannot be directly written in a host function` |
| `device_var_address_taken_in_host` | `address of a %s1 %n1 cannot be directly taken in a host function` |
| `host_var_read_in_device` | `a host %n1 cannot be directly read in a device function` |
| `host_var_written_in_device` | `a host %n1 cannot be directly written in a device function` |
| `host_var_address_taken_in_device` | `address of a host %n1 cannot be directly taken in a device function` |

### Variable Declaration Restrictions

| Tag | Message Template |
|---|---|
| `illegal_local_to_device_function` | `%s1 %sq2 variable declaration is not allowed inside a device function body` |
| `illegal_local_to_host_function` | `%s1 %sq2 variable declaration is not allowed inside a host function body` |
| -- | `the __shared__ memory space specifier is not allowed for a variable declared by the for-range-declaration` |
| -- | `__shared__ variables cannot have external linkage` |
| `device_variable_in_unnamed_inline_ns` | `A %s variable cannot be declared within an inline unnamed namespace` |
| -- | `member variables of an anonymous union at global or namespace scope cannot be directly accessed in __device__ and __global__ functions` |

### Auto-Deduced Device References

| Tag | Message Template |
|---|---|
| `auto_device_fn_ref` | `A non-constexpr __device__ function (%sq1) with "auto" deduced return type cannot be directly referenced %s2, except if the reference is absent when __CUDA_ARCH__ is undefined` |
| `device_var_constexpr` | *(constexpr rules for __device__ variables)* |
| `device_var_structured_binding` | *(structured bindings on __device__ variables)* |

## Category 9: \_\_grid\_constant\_\_ (8 messages)

The `__grid_constant__` annotation (compute_70+) marks a kernel parameter as read-only grid-wide. Errors enforce that the parameter is on a `__global__` function, is const-qualified, and is not a reference type.

| Tag | Message Template |
|---|---|
| `grid_constant_non_kernel` | `__grid_constant__ annotation is only allowed on a parameter of a __global__ function` |
| `grid_constant_not_const` | `a parameter annotated with __grid_constant__ must have const-qualified type` |
| `grid_constant_reference_type` | `a parameter annotated with __grid_constant__ must not have reference type` |
| `grid_constant_unsupported_arch` | `__grid_constant__ annotation is only allowed for architecture compute_70 or later` |
| `grid_constant_incompat_redecl` | `incompatible __grid_constant__ annotation for parameter %s in function redeclaration (see previous declaration %p)` |
| `grid_constant_incompat_templ_redecl` | `incompatible __grid_constant__ annotation for parameter %s in function template redeclaration (see previous declaration %p)` |
| `grid_constant_incompat_specialization` | `incompatible __grid_constant__ annotation for parameter %s in function specialization (see previous declaration %p)` |
| `grid_constant_incompat_instantiation_directive` | `incompatible __grid_constant__ annotation for parameter %s in instantiation directive (see previous declaration %p)` |

## Category 10: JIT Mode (5 messages)

JIT mode (`-dc` for device-only compilation) restricts host constructs. These errors guide users toward the `-default-device` flag for unannotated declarations.

| Tag | Message Template |
|---|---|
| `no_host_in_jit` | `A function explicitly marked as a __host__ function is not allowed in JIT mode` |
| `unannotated_function_in_jit` | `A function without execution space annotations (__host__/__device__/__global__) is considered a host function, and host functions are not allowed in JIT mode. Consider using -default-device flag to process unannotated functions as __device__ functions in JIT mode` |
| `unannotated_variable_in_jit` | `A namespace scope variable without memory space annotations (__device__/__constant__/__shared__/__managed__) is considered a host variable, and host variables are not allowed in JIT mode. Consider using -default-device flag to process unannotated namespace scope variables as __device__ variables in JIT mode` |
| `unannotated_static_data_member_in_jit` | `A class static data member with non-const type is considered a host variable, and host variables are not allowed in JIT mode. Consider using -default-device flag to process such data members as __device__ variables in JIT mode` |
| `host_closure_class_in_jit` | `The execution space for the lambda closure class members was inferred to be __host__ (based on context). This is not allowed in JIT mode. Consider using -default-device to infer __device__ execution space for namespace scope lambda closure classes.` |

## Category 11: RDC / Whole-Program Mode (4 messages)

Diagnostics related to relocatable device code (`-rdc=true`) and whole-program compilation (`-rdc=false`).

| Tag | Message Template |
|---|---|
| -- | `An inline __device__/__constant__/__managed__ variable must have internal linkage when the program is compiled in whole program mode (-rdc=false)` |
| `template_global_no_def` | `when "-static-global-template-stub=true" in whole program compilation mode ("-rdc=false"), a __global__ function template instantiation or specialization (%sq) must have a definition in the current translation unit. To resolve this issue, either use separate compilation mode ("-rdc=true"), or explicitly set "-static-global-template-stub=false" (but see nvcc documentation about downsides of turning it off)` |
| `extern_kernel_template` | `when "-static-global-template-stub=true", extern __global__ function template is not supported in whole program compilation mode ("-rdc=false"). To resolve the issue, either use separate compilation mode ("-rdc=true"), or explicitly set "-static-global-template-stub=false" (but see nvcc documentation about downsides of turning it off)` |
| -- | `address of internal linkage device function (%sq) was taken (nv bug 2001144). mitigation: no mitigation required if the address is not used for comparison, or if the target function is not a CUDA C++ builtin. Otherwise, write a wrapper function to call the builtin, and take the address of the wrapper function instead` |

## Category 12: Atomics (26 messages)

CUDA atomics are lowered to PTX instructions with specific size, type, scope, and memory order constraints. These diagnostics enforce hardware limits.

### Architecture and Type Constraints

| Tag | Message Template |
|---|---|
| `nv_atomic_functions_not_supported_below_sm60` | `__nv_atomic_* functions are not supported on arch < sm_60.` |
| `nv_atomic_operation_not_in_device_function` | `atomic operations are not in a device function.` |
| `nv_atomic_function_no_args` | `atomic function requires at least one argument.` |
| `nv_atomic_function_address_taken` | `nv atomic function must be called directly.` |
| `invalid_nv_atomic_operation_size` | `atomic operations and, or, xor, add, sub, min and max are valid only on objects of size 4, or 8.` |
| `invalid_nv_atomic_cas_size` | `atomic CAS is valid only on objects of size 2, 4, 8 or 16 bytes.` |
| `invalid_nv_atomic_exch_size` | `atomic exchange is valid only on objects of size 4, 8 or 16 bytes.` |
| `invalid_data_size_for_nv_atomic_generic_function` | `generic nv atomic functions are valid only on objects of size 1, 2, 4, 8 and 16 bytes.` |
| `non_integral_type_for_non_generic_nv_atomic_function` | `non-generic nv atomic load, store, cas and exchange are valid only on integral types.` |
| `invalid_nv_atomic_operation_add_sub_size` | `atomic operations add and sub are not valid on signed integer of size 8.` |
| `nv_atomic_add_sub_f64_not_supported` | `atomic add and sub for 64-bit float is supported on architecture sm_60 or above.` |
| `invalid_nv_atomic_operation_max_min_float` | `atomic operations min and max are not supported on any floating-point types.` |
| `floating_type_for_logical_atomic_operation` | `For a logical atomic operation, the first argument cannot be any floating-point types.` |
| `nv_atomic_cas_b16_not_supported` | *(16-bit CAS not supported)* |
| `nv_atomic_exch_cas_b128_not_supported` | *(128-bit exchange/CAS not supported)* |
| `nv_atomic_load_store_b128_version_too_low` | *(128-bit load/store requires newer arch)* |

### Memory Order and Scope

| Tag | Message Template |
|---|---|
| `nv_atomic_load_order_error` | `atomic load's memory order cannot be release or acq_rel.` |
| `nv_atomic_store_order_error` | `atomic store's memory order cannot be consume, acquire or acq_rel.` |
| `nv_atomic_operation_order_not_constant_int` | `atomic operation's memory order argument is not an integer literal.` |
| `nv_atomic_operation_scope_not_constant_int` | `atomic operation's scope argument is not an integer literal.` |
| `invalid_nv_atomic_memory_order_value` | *(invalid memory order enum value)* |
| `invalid_nv_atomic_thread_scope_value` | *(invalid thread scope enum value)* |

### Scope Fallback Warnings

| Tag | Message Template |
|---|---|
| `nv_atomic_operations_scope_fallback_to_membar` | `atomic operations' scope argument is supported on architecture sm_60 or above. Fall back to use membar.` |
| `nv_atomic_operations_memory_order_fallback_to_membar` | `atomic operations' argument of memory order is supported on architecture sm_70 or above. Fall back to use membar.` |
| `nv_atomic_operations_scope_cluster_change_to_device` | `atomic operations' scope of cluster is supported on architecture sm_90 or above. Using device scope instead.` |
| `nv_atomic_load_store_scope_cluster_change_to_device` | `atomic load and store's scope of cluster is supported on architecture sm_90 or above. Using device scope instead.` |

## Category 13: ASM in Device Code (6 messages)

Inline assembly constraints are more restrictive in device code (NVPTX backend supports fewer constraint letters than x86).

| Tag | Message Template |
|---|---|
| `asm_constraint_letter_not_allowed_in_device` | `asm constraint letter '%s' is not allowed inside a __device__/__global__ function` |
| -- | `an asm operand may specify only one constraint letter in a __device__/__global__ function` |
| -- | `The 'C' constraint can only be used for asm statements in device code` |
| -- | `The cc clobber constraint is not supported in device code` |
| `cuda_xasm_strict_placeholder_format` | *(strict placeholder format in CUDA asm)* |
| `addr_of_label_in_device_func` | `address of label extension is not supported in __device__/__global__ functions` |

## Category 14: #pragma nv\_abi (10 messages)

The `#pragma nv_abi` directive controls the calling convention for device functions, adjusting parameter passing to match PTX ABI requirements.

| Tag | Message Template |
|---|---|
| `nv_abi_pragma_bad_format` | *(malformed #pragma nv_abi)* |
| `nv_abi_pragma_invalid_option` | `#pragma nv_abi contains an invalid option` |
| `nv_abi_pragma_missing_arg` | `#pragma nv_abi requires an argument` |
| `nv_abi_pragma_duplicate_arg` | `#pragma nv_abi contains a duplicate argument` |
| `nv_abi_pragma_not_constant` | `#pragma nv_abi argument must evaluate to an integral constant expression` |
| `nv_abi_pragma_not_positive_value` | `#pragma nv_abi argument value must be a positive value` |
| `nv_abi_pragma_overflow_value` | `#pragma nv_abi argument value exceeds the range of an integer` |
| `nv_abi_pragma_device_function` | `#pragma nv_abi must be applied to device functions` |
| `nv_abi_pragma_device_function_context` | `#pragma nv_abi is not supported inside a host function` |
| `nv_abi_pragma_next_construct` | `#pragma nv_abi must appear immediately before a function declaration, function definition, or an expression statement` |

## Category 15: \_\_nv\_register\_params\_\_ (4 messages)

The `__nv_register_params__` attribute forces all parameters to be passed in registers (compute_80+).

| Tag | Message Template |
|---|---|
| `register_params_not_enabled` | `__nv_register_params__ support is not enabled` |
| `register_params_unsupported_arch` | `__nv_register_params__ is only supported for compute_80 or later architecture` |
| `register_params_unsupported_function` | `__nv_register_params__ is not allowed on a %s function` |
| `register_params_ellipsis_function` | `__nv_register_params__ is not allowed on a function with ellipsis` |

## Category 16: \_\_CUDACC\_RTC\_\_name\_expr (6 messages)

The `__CUDACC_RTC__name_expr` intrinsic is used by NVRTC to form the mangled name of a `__global__` function or `__device__`/`__constant__` variable at compile time.

| Tag | Message Template |
|---|---|
| `name_expr_parsing` | *(error during name expression parsing)* |
| `name_expr_non_global_routine` | `Name expression cannot form address of a non-__global__ function. Input name expression was: %sq` |
| `name_expr_non_device_variable` | `Name expression cannot form address of a variable that is not a __device__/__constant__ variable. Input name expression was: %sq` |
| `name_expr_not_routine_or_variable` | `Name expression must form address of a __global__ function or the address of a __device__/__constant__ variable. Input name expression was: %sq` |
| `name_expr_extra_tokens` | *(extra tokens after name expression)* |
| `name_expr_internal_error` | *(internal error in name expression processing)* |

## Category 17: Texture and Surface Variables (8 messages)

Texture and surface objects have special memory semantics. These errors enforce that they are not used in ways incompatible with the GPU texture subsystem.

| Tag | Message Template |
|---|---|
| `texture_surface_variable_in_unnamed_inline_ns` | `A texture or surface variable cannot be declared within an inline unnamed namespace` |
| -- | `A texture or surface variable cannot be used in the non-type template argument of a __device__, __host__ __device__ or __global__ function template instantiation` |
| `reference_to_text_surf_type_in_device_func` | `a reference to texture/surface type cannot be used in __device__/__global__ functions` |
| `reference_to_text_surf_var_in_device_func` | `taking reference of texture/surface variable not allowed in __device__/__global__ functions` |
| `addr_of_text_surf_var_in_device_func` | `cannot take address of texture/surface variable %sq in __device__/__global__ functions` |
| `addr_of_text_surf_expr_in_device_func` | `cannot take address of texture/surface expression in __device__/__global__ functions` |
| `indir_into_text_surf_var_in_device_func` | `indirection not allowed for accessing texture/surface through variable %sq in __device__/__global__ functions` |
| `indir_into_text_surf_expr_in_device_func` | `indirection not allowed for accessing texture/surface through expression in __device__/__global__ functions` |

## Category 18: \_\_managed\_\_ Variables (7 messages)

`__managed__` unified-memory variables have significant restrictions because they must be accessible from both host and device.

| Tag | Message Template |
|---|---|
| `managed_const_type_not_allowed` | `a __managed__ variable cannot have a const qualified type` |
| `managed_reference_type_not_allowed` | `a __managed__ variable cannot have a reference type` |
| `managed_cant_be_shared_constant` | `__managed__ variables cannot be marked __shared__ or __constant__` |
| `unsupported_arch_for_managed_capability` | `__managed__ variables require architecture compute_30 or higher` |
| `unsupported_configuration_for_managed_capability` | `__managed__ variables are not yet supported for this configuration (compilation mode (32/64 bit) and/or target operating system)` |
| `decltype_of_managed_variable` | `A __managed__ variable cannot be used as an unparenthesized id-expression argument for decltype()` |
| -- | *(dynamic initialization restrictions for __managed__ variables)* |

## Category 19: Device Function Signature Constraints (5 messages)

Restrictions on `__device__` and `__host__ __device__` functions that are distinct from `__global__` constraints.

| Tag | Message Template |
|---|---|
| `device_function_has_ellipsis` | `__device__ or __host__ __device__ function with ellipsis requires compute_30 or higher architecture` |
| `device_func_tex_arg` | *(device function with texture argument restriction)* |
| `no_host_device_initializer_list` | *(std::initializer_list in __host__ __device__ context)* |
| `no_host_device_move_forward` | *(std::move/forward in __host__ __device__ context)* |
| `no_strict_cuda_error` | *(relaxed error checking mode)* |

## Category 20: \_\_wgmma\_mma\_async Builtins (4 messages)

Warp Group Matrix Multiply-Accumulate builtins (sm_90a+).

| Tag | Message Template |
|---|---|
| `wgmma_mma_async_not_enabled` | `__wgmma_mma_async builtins are only available for sm_90a` |
| `wgmma_mma_async_nonconstant_arg` | `Non-constant argument to __wgmma_mma_async call` |
| `wgmma_mma_async_missing_args` | `The 'A' or 'B' argument to __wgmma_mma_async call is missing` |
| `wgmma_mma_async_bad_shape` | `The shape %s is not supported for __wgmma_mma_async builtin` |

## Category 21: \_\_block\_size\_\_ / \_\_cluster\_dims\_\_ (8 messages)

Architecture-dependent launch configuration attributes.

| Tag | Message Template |
|---|---|
| `block_size_unsupported` | `__block_size__ is not supported for this GPU architecture` |
| `block_size_must_be_positive` | *(block size values must be positive)* |
| `cluster_dims_unsupported` | `__cluster_dims__ is not supported for this GPU architecture` |
| `cluster_dims_must_be_positive` | *(__cluster_dims__ values must be positive)* |
| `cluster_dims_too_large` | *(__cluster_dims__ exceeds maximum)* |
| `conflict_between_cluster_dim_and_block_size` | `cannot specify the second tuple in __block_size__ while __cluster_dims__ is present` |
| -- | `cannot specify max blocks per cluster for this GPU architecture` |
| `shared_block_size_must_be_positive` | *(shared block size must be positive)* |

## Category 22: Inline Hint Conflicts (2 messages)

| Tag | Message Template |
|---|---|
| -- | `"__inline_hint__" and "__forceinline__" may not be used on the same declaration` |
| -- | `"__inline_hint__" and "__noinline__" may not be used on the same declaration` |

## Category 23: Miscellaneous CUDA Errors

Remaining CUDA-specific diagnostics that do not fall into the above categories.

| Tag | Message Template |
|---|---|
| `cuda_displaced_new_or_delete_operator` | *(displaced new/delete in CUDA context)* |
| `cuda_demote_unsupported_floating_point` | *(unsupported floating-point type demoted)* |
| `illegal_ucn_in_device_identifer` | `Universal character is not allowed in device entity name (%sq)` |
| `thread_local_for_device_vars` | *(thread_local on device variables)* |
| -- | `__global__ function or function template cannot have a parameter with va_list type` |
| `global_qualifier_not_allowed` | *(execution space qualifier not allowed here)* |

## Complete Diagnostic Tag Index (286 tags)

The following table lists all 286 CUDA-specific diagnostic tag names extracted from the cudafe++ binary. Each tag can be used with `--diag_suppress`, `--diag_warning`, `--diag_error`, or `#pragma nv_diag_suppress` / `nv_diag_warning` / `nv_diag_error`.

Tags are organized alphabetically within functional groups.

### Cross-Space / Execution Space

| Tag Name |
|---|
| `unsafe_device_call` |

### Redeclaration

| Tag Name |
|---|
| `device_function_redeclared_with_global` |
| `device_function_redeclared_with_host` |
| `device_function_redeclared_with_host_device` |
| `device_function_redeclared_without_device` |
| `global_function_redeclared_with_device` |
| `global_function_redeclared_with_host` |
| `global_function_redeclared_with_host_device` |
| `global_function_redeclared_without_global` |
| `host_device_function_redeclared_with_global` |
| `host_function_redeclared_with_device` |
| `host_function_redeclared_with_global` |
| `host_function_redeclared_with_host_device` |

### \_\_global\_\_ Constraints

| Tag Name |
|---|
| `bounds_attr_only_on_global_func` |
| `cuda_specifier_twice_in_group` |
| `global_class_decl` |
| `global_exception_spec` |
| `global_friend_definition` |
| `global_func_local_template_arg` |
| `global_function_consteval` |
| `global_function_constexpr` |
| `global_function_deduced_return_type` |
| `global_function_has_ellipsis` |
| `global_function_in_unnamed_inline_ns` |
| `global_function_inline` |
| `global_function_multiple_packs` |
| `global_function_pack_not_last` |
| `global_function_return_type` |
| `global_function_with_initializer_list` |
| `global_lambda_template_arg` |
| `global_new_or_delete` |
| `global_operator_function` |
| `global_param_align_too_big` |
| `global_private_template_arg` |
| `global_private_type_arg` |
| `global_qualifier_not_allowed` |
| `global_ref_param_restrict` |
| `global_rvalue_ref_type` |
| `global_unnamed_type_arg` |
| `global_va_list_type` |
| `local_type_used_in_global_function` |
| `maxnreg_attr_only_on_global_func` |
| `missing_launch_bounds` |
| `template_global_no_def` |

### Extended Lambda

| Tag Name |
|---|
| `extended_host_device_generic_lambda` |
| `extended_lambda_array_capture_assignable` |
| `extended_lambda_array_capture_default_constructible` |
| `extended_lambda_array_capture_rank` |
| `extended_lambda_call_operator_local_type` |
| `extended_lambda_call_operator_private_type` |
| `extended_lambda_cant_take_function_address` |
| `extended_lambda_capture_in_constexpr_if` |
| `extended_lambda_capture_local_type` |
| `extended_lambda_capture_private_type` |
| `extended_lambda_constexpr` |
| `extended_lambda_disallowed` |
| `extended_lambda_discriminator` |
| `extended_lambda_enclosing_function_deducible` |
| `extended_lambda_enclosing_function_generic_lambda` |
| `extended_lambda_enclosing_function_hd_lambda` |
| `extended_lambda_enclosing_function_local` |
| `extended_lambda_enclosing_function_not_found` |
| `extended_lambda_hd_init_capture` |
| `extended_lambda_illegal_parent` |
| `extended_lambda_inaccessible_ancestor` |
| `extended_lambda_inaccessible_parent` |
| `extended_lambda_init_capture_array` |
| `extended_lambda_init_capture_initlist` |
| `extended_lambda_inside_constexpr_if` |
| `extended_lambda_multiple_parameter_packs` |
| `extended_lambda_multiple_parent` |
| `extended_lambda_nest_parent_template_param_unnamed` |
| `extended_lambda_no_parent_func` |
| `extended_lambda_pack_capture` |
| `extended_lambda_parent_class_unnamed` |
| `extended_lambda_parent_local_type` |
| `extended_lambda_parent_non_extern` |
| `extended_lambda_parent_private_template_arg` |
| `extended_lambda_parent_private_type` |
| `extended_lambda_parent_template_param_unnamed` |
| `extended_lambda_reference_capture` |
| `extended_lambda_too_many_captures` |
| `this_addr_capture_ext_lambda` |

### Device Code

| Tag Name |
|---|
| `addr_of_label_in_device_func` |
| `asm_constraint_letter_not_allowed_in_device` |
| `auto_device_fn_ref` |
| `cuda_device_code_unsupported_operator` |
| `cuda_xasm_strict_placeholder_format` |
| `illegal_ucn_in_device_identifer` |
| `no_strict_cuda_error` |
| `thread_local_in_device_code` |
| `undefined_device_entity` |
| `undefined_device_identifier` |
| `unrecognized_pragma_device_code` |
| `unsupported_type_in_device_code` |

### Device Function

| Tag Name |
|---|
| `device_func_tex_arg` |
| `device_function_has_ellipsis` |
| `no_host_device_initializer_list` |
| `no_host_device_move_forward` |

### Kernel Launch

| Tag Name |
|---|
| `device_launch_no_sepcomp` |
| `device_side_launch_arg_with_user_provided_cctor` |
| `device_side_launch_arg_with_user_provided_dtor` |
| `missing_api_for_device_side_launch` |

### Variable Access

| Tag Name |
|---|
| `device_var_address_taken_in_host` |
| `device_var_constexpr` |
| `device_var_read_in_host` |
| `device_var_structured_binding` |
| `device_var_written_in_host` |
| `device_variable_in_unnamed_inline_ns` |
| `host_var_address_taken_in_device` |
| `host_var_read_in_device` |
| `host_var_written_in_device` |
| `illegal_local_to_device_function` |
| `illegal_local_to_host_function` |

### Variable Template

| Tag Name |
|---|
| `variable_template_func_local_template_arg` |
| `variable_template_lambda_template_arg` |
| `variable_template_private_template_arg` |
| `variable_template_private_type_arg` |
| `variable_template_unnamed_type_template_arg` |

### \_\_managed\_\_

| Tag Name |
|---|
| `decltype_of_managed_variable` |
| `managed_cant_be_shared_constant` |
| `managed_const_type_not_allowed` |
| `managed_reference_type_not_allowed` |
| `unsupported_arch_for_managed_capability` |
| `unsupported_configuration_for_managed_capability` |

### \_\_grid\_constant\_\_

| Tag Name |
|---|
| `grid_constant_incompat_instantiation_directive` |
| `grid_constant_incompat_redecl` |
| `grid_constant_incompat_specialization` |
| `grid_constant_incompat_templ_redecl` |
| `grid_constant_non_kernel` |
| `grid_constant_not_const` |
| `grid_constant_reference_type` |
| `grid_constant_unsupported_arch` |

### Atomics

| Tag Name |
|---|
| `floating_type_for_logical_atomic_operation` |
| `invalid_data_size_for_nv_atomic_generic_function` |
| `invalid_nv_atomic_cas_size` |
| `invalid_nv_atomic_exch_size` |
| `invalid_nv_atomic_memory_order_value` |
| `invalid_nv_atomic_operation_add_sub_size` |
| `invalid_nv_atomic_operation_max_min_float` |
| `invalid_nv_atomic_operation_size` |
| `invalid_nv_atomic_thread_scope_value` |
| `non_integral_type_for_non_generic_nv_atomic_function` |
| `nv_atomic_add_sub_f64_not_supported` |
| `nv_atomic_cas_b16_not_supported` |
| `nv_atomic_exch_cas_b128_not_supported` |
| `nv_atomic_function_address_taken` |
| `nv_atomic_function_no_args` |
| `nv_atomic_functions_not_supported_below_sm60` |
| `nv_atomic_load_order_error` |
| `nv_atomic_load_store_b128_version_too_low` |
| `nv_atomic_load_store_scope_cluster_change_to_device` |
| `nv_atomic_operation_not_in_device_function` |
| `nv_atomic_operation_order_not_constant_int` |
| `nv_atomic_operation_scope_not_constant_int` |
| `nv_atomic_operations_memory_order_fallback_to_membar` |
| `nv_atomic_operations_scope_cluster_change_to_device` |
| `nv_atomic_operations_scope_fallback_to_membar` |
| `nv_atomic_store_order_error` |

### JIT Mode

| Tag Name |
|---|
| `host_closure_class_in_jit` |
| `no_host_in_jit` |
| `unannotated_function_in_jit` |
| `unannotated_static_data_member_in_jit` |
| `unannotated_variable_in_jit` |

### RDC / Whole-Program

| Tag Name |
|---|
| `extern_kernel_template` |
| `template_global_no_def` |

### #pragma nv\_abi

| Tag Name |
|---|
| `nv_abi_pragma_bad_format` |
| `nv_abi_pragma_device_function` |
| `nv_abi_pragma_device_function_context` |
| `nv_abi_pragma_duplicate_arg` |
| `nv_abi_pragma_invalid_option` |
| `nv_abi_pragma_missing_arg` |
| `nv_abi_pragma_next_construct` |
| `nv_abi_pragma_not_constant` |
| `nv_abi_pragma_not_positive_value` |
| `nv_abi_pragma_overflow_value` |

### \_\_nv\_register\_params\_\_

| Tag Name |
|---|
| `register_params_ellipsis_function` |
| `register_params_not_enabled` |
| `register_params_unsupported_arch` |
| `register_params_unsupported_function` |

### name\_expr

| Tag Name |
|---|
| `name_expr_extra_tokens` |
| `name_expr_internal_error` |
| `name_expr_non_device_variable` |
| `name_expr_non_global_routine` |
| `name_expr_not_routine_or_variable` |
| `name_expr_parsing` |

### Texture / Surface

| Tag Name |
|---|
| `addr_of_text_surf_expr_in_device_func` |
| `addr_of_text_surf_var_in_device_func` |
| `indir_into_text_surf_expr_in_device_func` |
| `indir_into_text_surf_var_in_device_func` |
| `reference_to_text_surf_type_in_device_func` |
| `reference_to_text_surf_var_in_device_func` |
| `texture_surface_variable_in_unnamed_inline_ns` |

### \_\_wgmma\_mma\_async

| Tag Name |
|---|
| `wgmma_mma_async_bad_shape` |
| `wgmma_mma_async_missing_args` |
| `wgmma_mma_async_nonconstant_arg` |
| `wgmma_mma_async_not_enabled` |

### \_\_block\_size\_\_ / \_\_cluster\_dims\_\_

| Tag Name |
|---|
| `block_size_must_be_positive` |
| `block_size_unsupported` |
| `cluster_dims_must_be_positive` |
| `cluster_dims_too_large` |
| `cluster_dims_unsupported` |
| `conflict_between_cluster_dim_and_block_size` |
| `shared_block_size_must_be_positive` |
| `shared_block_size_too_large` |

### Miscellaneous

| Tag Name |
|---|
| `cuda_demote_unsupported_floating_point` |
| `cuda_displaced_new_or_delete_operator` |
| `thread_local_for_device_vars` |

## Internal Representation

Each CUDA error message is stored as a `const char*` entry in the error template table at `off_88FAA0`. The diagnostic tag names are stored in a separate string-to-integer lookup table; the tag name resolver (`sub_4ED240` and related functions) performs a binary search on this table to match tag strings against internal error codes.

The format specifiers embedded in CUDA error messages use the same system as EDG base errors:

| Specifier | Meaning | Example in CUDA messages |
|---|---|---|
| `%sq` | Quoted entity name | Function name in cross-space call |
| `%sq1`, `%sq2` | Indexed quoted names | Caller and callee in call errors |
| `%no1` | Entity name (omit kind) | Function name in redeclaration |
| `%n1`, `%n2` | Entity names | Override base/derived pair |
| `%nd` | Entity name with decl location | Template parameter |
| `%s`, `%s1`, `%s2` | String fill-in | Execution space keyword |
| `%t` | Type fill-in | Type name in template arg errors |
| `%p` | Source position | Previous declaration location |

For full format specifier documentation, see [Format Specifiers](./format-specifiers.md).
