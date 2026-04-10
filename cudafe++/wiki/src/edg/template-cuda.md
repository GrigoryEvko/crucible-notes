# CUDA Template Restrictions

CUDA's split-compilation model imposes restrictions on C++ templates that have no counterpart in standard C++. When a `__global__` function template is instantiated, cudafe++ generates a host-side stub whose mangled name must exactly match what the device compiler (`cicc`) independently produces. This agreement is only possible if both compilers can derive the complete mangled name from the template's signature and arguments. Types that are invisible to one side -- host-local types, unnamed types, private class members, certain lambda closures -- break this invariant and are therefore rejected. The same constraints apply to variable templates used in device contexts, and additional structural restrictions prevent variadic `__global__` templates from producing ambiguous mangled names. This page documents all 24 CUDA-specific template restriction errors across 8 categories, the implementation functions that enforce them, and the `__NV_name_expr` mechanism that relies on these guarantees.

## Key Facts

| Property | Value |
|---|---|
| Source file | `cp_gen_be.c` (EDG 6.6 backend code generator) |
| Access checker | `sub_469F80` (`template_arg_is_accessible`, 144 lines) |
| Cache engine | `sub_469480` (`cache_access_result_for`, 670 lines) |
| Arg list walker | `sub_46A230` (walks template arg lists, 182 lines) |
| Pre-unnamed check | `sub_46A5B0` (`arg_before_unnamed_template_param_arg`, 396 lines) |
| Scope resolver | `sub_469F30` (resolves scope via hash lookup, 23 lines) |
| Callback for scope walk | `sub_46ACC0` (passed as callback into `sub_61FE60`) |
| Cache hash table | `xmmword_F05720` (384 KB, 16,382-entry table, 24 bytes per slot) |
| Entity lookup table | `unk_FE5700` (512 KB, used by `sub_469F30`) |
| Free list head | `qword_F05708` (recycled cache entries) |
| Total restriction errors | 24 across 8 categories |

## Why These Restrictions Exist

The CUDA compilation model splits a single `.cu` source file into two compilation paths:

1. **Host path**: cudafe++ generates a `.int.c` file containing host stubs. The host compiler (gcc, clang, MSVC) compiles these stubs and produces a host object file. Each `__global__` function template instantiation becomes a `__wrapper__device_stub_` function.

2. **Device path**: The same source is compiled by `cicc` into PTX. The device compiler independently instantiates the same templates and produces the device-side function bodies.

At link time, the CUDA runtime matches host stubs to device functions by **mangled name**. Both compilers must produce identical mangled names for every `__global__` template instantiation. This is only possible when all template arguments are types that both compilers can see, name, and mangle identically. A host-only local type, for example, exists only in the host compiler's scope -- `cicc` cannot see it and cannot produce a matching mangled name. The restrictions documented below enforce this invariant.

The same logic applies to `__device__`/`__constant__` variable templates, which must also match across the host/device boundary for registration and symbol lookup.

## Category A: \_\_global\_\_ Declaration Restrictions (8 errors)

These errors prevent `__global__` function templates from using C++ features that would prevent host stub generation or violate kernel ABI constraints.

| Tag | Message | Reason |
|---|---|---|
| `global_function_constexpr` | `A __global__ function or function template cannot be marked constexpr` | Kernels are not evaluated at compile time; `constexpr` is meaningless for device launch. |
| `global_function_consteval` | `A __global__ function or function template cannot be marked consteval` | `consteval` requires compile-time evaluation, incompatible with runtime kernel launch. |
| `global_class_decl` | `A __global__ function or function template cannot be a member function` | Kernels have no `this` pointer; the launch ABI has no slot for an object reference. |
| `global_friend_definition` | `A __global__ function or function template cannot be defined in a friend declaration` | Friend definitions have limited visibility, conflicting with the requirement for a globally-linkable stub. |
| `global_exception_spec` | `An exception specification is not allowed for a __global__ function or function template` | GPU hardware has no exception unwinding mechanism. |
| `global_function_in_unnamed_inline_ns` | `A __global__ function or function template cannot be declared within an inline unnamed namespace` | Unnamed namespaces produce TU-local linkage, but kernel stubs must have external linkage for runtime registration. |
| `global_function_with_initializer_list` | `a __global__ function or function template cannot have a parameter with type std::initializer_list` | `std::initializer_list` holds a pointer to backing storage that cannot be transparently transferred to device memory. |
| `global_va_list_type` | `A __global__ function or function template cannot have a parameter with va_list type` | Variadic argument lists require stack-based access that does not exist on GPU hardware. |

These checks occur during attribute application in `apply_nv_global_attr` (`sub_40E1F0` / `sub_40E7F0`) and in the post-validation pass `nv_validate_cuda_attributes` (`sub_6BC890`). The checks apply equally to non-template `__global__` functions and `__global__` function templates.

## Category B: Variadic \_\_global\_\_ Template Constraints (2 errors)

Standard C++ allows multiple parameter packs in a template and does not require packs to be the last parameter. CUDA restricts this for `__global__` templates because the host stub ABI requires unambiguous argument layout.

| Tag | Message |
|---|---|
| `global_function_pack_not_last` | `Pack template parameter must be the last template parameter for a variadic __global__ function template` |
| `global_function_multiple_packs` | `Multiple pack parameters are not allowed for a variadic __global__ function template` |

### Rationale

The kernel launch wrapper (`<<<grid, block>>>`) must marshal each argument into a contiguous parameter buffer. For a variadic template like `template<typename... Ts> __global__ void kernel(Ts... args)`, the compiler generates the buffer layout at instantiation time. If the pack is not last, or if multiple packs are present, the positional mapping between template parameters and launch arguments becomes ambiguous -- the compiler cannot determine which arguments belong to which pack without full deduction context that may not be available at stub generation time.

### Example

```cuda
// OK: single pack, last position
template<typename T, typename... Ts>
__global__ void kernel(T first, Ts... rest);

// Error: pack not last
template<typename... Ts, typename T>
__global__ void kernel(Ts... args, T last);  // global_function_pack_not_last

// Error: multiple packs
template<typename... Ts, typename... Us>
__global__ void kernel(Ts... a, Us... b);    // global_function_multiple_packs
```

## Category C: Template Argument Visibility for \_\_global\_\_ (6 errors)

These are the core name-mangling restrictions. Every type used as a template argument to a `__global__` function template instantiation must be visible and nameable by both the host and device compilers.

### C.1: Host-local types

| Tag | Message |
|---|---|
| `global_func_local_template_arg` | `A type defined inside a __host__ function (%t) cannot be used in the template argument type of a __global__ function template instantiation` |

A type defined inside a `__host__` function exists only within that function's scope. The device compiler never sees it and cannot produce a matching mangled name.

```cuda
void host_function() {
    struct LocalType { int x; };
    kernel<LocalType><<<1,1>>>();  // error: host-local type
}
```

### C.2: Private/protected class members

| Tag | Message |
|---|---|
| `global_private_type_arg` | `A type that is defined inside a class and has private or protected access (%t) cannot be used in the template argument type of a __global__ function template instantiation, unless the class is local to a __device__ or __global__ function` |

Private/protected nested types are accessible only through the enclosing class's access control. While C++ allows friend access and member function access to these types, the device compiler processes templates independently and may not have the same access context. The exception for types local to `__device__`/`__global__` functions reflects that both compilers see device function bodies.

```cuda
class Outer {
    struct Inner { int x; };     // private
    friend void launch();
};

void launch() {
    kernel<Outer::Inner><<<1,1>>>();  // error: private type
}
```

### C.3: Unnamed types

| Tag | Message |
|---|---|
| `global_unnamed_type_arg` | `An unnamed type (%t) cannot be used in the template argument type of a __global__ function template instantiation, unless the type is local to a __device__ or __global__ function` |

Unnamed types (anonymous structs, unnamed enums) have no canonical name. Itanium ABI mangling for unnamed types relies on positional encoding within the enclosing scope, which may differ between host and device compilers if they process the enclosing scope differently. Types local to `__device__`/`__global__` functions are exempt because the device compiler processes those scopes identically.

```cuda
enum { A, B, C };                     // unnamed enum
kernel<decltype(A)><<<1,1>>>();       // error: unnamed type
```

### C.4: Lambda closures

| Tag | Message |
|---|---|
| `global_lambda_template_arg` | `The closure type for a lambda (%t%s) cannot be used in the template argument type of a __global__ function template instantiation, unless the lambda is defined within a __device__ or __global__ function, or the flag '-extended-lambda' is specified and the lambda is an extended lambda (a __device__ or __host__ __device__ lambda defined within a __host__ or __host__ __device__ function)` |

Lambda closures are compiler-generated anonymous types. Without `--extended-lambda`, there is no protocol for both compilers to agree on the closure type's mangled representation. The extended lambda mechanism (`--extended-lambda` / `--extended-lambda`) establishes a naming convention for lambdas annotated with `__device__` or `__host__ __device__`, enabling cross-compiler name agreement.

```cuda
auto f = [](int x){ return x*2; };
kernel<decltype(f)><<<1,1>>>();       // error unless extended lambda
```

### C.5: Private/protected template template arguments

| Tag | Message |
|---|---|
| `global_private_template_arg` | `A template that is defined inside a class and has private or protected access cannot be used in the template template argument of a __global__ function template instantiation` |

The same access-control problem as C.2, but for template template parameters. A private class template used as a template template argument cannot be guaranteed visible in the device compiler's independent instantiation context.

### C.6: Texture/surface non-type arguments

| Message |
|---|
| `A texture or surface variable cannot be used in the non-type template argument of a __device__, __host__ __device__ or __global__ function template instantiation` |

Texture and surface objects have special hardware semantics. Their runtime addresses are not fixed at compile time (they are bound through the texture subsystem), so they cannot serve as non-type template arguments whose values must be known to produce a deterministic mangled name.

## Implementation: The Access Checking Pipeline

The template argument restriction checks are implemented in a three-function pipeline within `cp_gen_be.c`:

### sub_469F80 — template_arg_is_accessible

This is the primary entry point. It dispatches on the template argument kind (byte at `arg+8`):

```c
int template_arg_is_accessible(arg_t *a1, int scope_depth, char check_scope, int *cache_miss) {
    arg->flags_25 |= 0x10;               // mark: currently checking
    int kind = arg->kind;                 // offset +8
    
    switch (kind) {
    case 0:  // type argument
        type = arg->value;               // offset +32
        result = cache_access_result_for(type, 6, scope_depth, cache_miss);
        if (!result && (check_scope & 1)) {
            // walk through typedef chains (type_kind == 12)
            while (type->kind == 12)
                type = type->canonical;   // offset +144
            result = cache_access_result_for(type, 6, scope_depth, cache_miss);
            if (!result) {
                sub_469F30(&type_holder, 0);   // resolve via entity lookup
                result = (type_holder != original_type);
            }
        }
        break;
        
    case 1:  // template argument (template template parameter)
        entity = arg->value;             // offset +32
        // Check class accessibility via derivation chain
        if (entity->base_class) {        // offset +128
            // Use IL walker sub_61FE60 with callback sub_46ACC0
            sub_61EC40(visitor_state);
            visitor_state[0] = sub_46ACC0;   // the callback
            sub_61FE60(entity->base_class, visitor_state);
            result = (visitor_state->found == 0);
        }
        break;
        
    case 2:  // non-type argument
        result = cache_access_result_for(arg->value, 58, scope_depth, cache_miss);
        break;
        
    default:
        __assert_fail("template_arg_is_accessible", "cp_gen_be.c", 2448);
    }
    
    arg->flags_25 &= ~0x10;              // clear: done checking
    return result;
}
```

The `flags_25 |= 0x10` / `&= ~0x10` pattern is a recursion guard: it marks the argument as "currently being checked" to prevent infinite loops through mutually-referential template arguments.

### sub_469480 — cache_access_result_for

This function caches the result of access checking for a given entity to avoid redundant computation. The cache is a hash table at `xmmword_F05720` with 16,382 buckets (0x3FFF), each 24 bytes wide.

**Cache entry layout** (24 bytes):

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `next` | Pointer to next entry in chain (collision list) |
| +8 | 8 | `entity` | Entity pointer being cached |
| +16 | 4 | `scope_id` | Scope identifier from `qword_1065708` chain |
| +20 | 1 | `result` | Cached access result (1 = accessible, 0 = not) |
| +21 | 1 | `arg_kind` | Template argument kind that was checked |

**Hash function**: The entity pointer is right-shifted by 6 bits, then taken modulo 0x3FFF:

```c
unsigned hash = ((unsigned)(entity >> 6) * 262161ULL) >> 32;
unsigned bucket = (entity >> 6)
    - 0x3FFF * (((hash + ((entity >> 6) - hash) >> 1)) >> 13);
char *slot = &xmmword_F05720[24 * bucket];
```

**Cache hit path**: If `slot->entity == entity` and the scope matches, return the cached result immediately. The function walks the `qword_1065708` chain (the scope stack) to verify that the cached result was computed in a compatible scope context.

**Cache eviction**: When a cached entry's scope no longer matches (the scope stack has changed since caching), the entry is moved to the free list (`qword_F05708`). New entries are allocated from the free list or via `sub_6B7340` (24-byte allocation).

**Fallback (cache miss)**: On cache miss, the function performs the actual accessibility analysis:

1. For type arguments (kind 6): resolves typedefs, checks if the type is a class/struct/enum with access restrictions. Uses `sub_5F9C10` to resolve through elaborated type specifiers. Checks `entity->access_bits` at `+80` (bits 0-1: 0=public, 1=protected, 2=private).

2. For non-type arguments (kind 58): checks the entity's accessibility directly.

3. For class/struct types (kinds 9-11): walks the class's template argument list recursively via `sub_469F80`.

4. For dependent types (kind 14): recursively checks the base type.

5. For function types (kind 7) and pointer-to-member types (kind 13): recursively checks the return type, parameter types, and pointed-to class.

After computing the result, it is stored in the cache for future lookups.

### sub_46A230 — Template Arg List Walker

This function walks a template instantiation's argument list and checks each argument for accessibility. It uses the entity lookup hash table at `unk_FE5700` to find cached resolution results.

```c
__int64 walk_template_args(__int64 hash_table, unsigned __int64 type) {
    // Resolve through typedef chains
    while (type->kind == 12)
        type = type->canonical;           // offset +144
    
    // Hash the type pointer into a bucket
    _QWORD *bucket = hash_table + 32 * ((type >> 6) % 0x3FFF);
    
    // Walk the bucket chain
    while (bucket && bucket[1]) {
        entry = bucket[1];                // the entity entry
        
        // Check if this entry matches our type
        if (entry->canonical != type && !sub_7B2260(entry->canonical, type, 0))
            continue;
        
        // Scope compatibility check
        if (bucket[2] && bucket[2] != qword_126C5D0)
            continue;
        
        // For template entities (kind 10), walk their argument lists
        if (entry->kind == 10) {
            arg_list = *entry->template_args;
            while (arg) {
                if (arg->flags_25 & 0x10)     // already being checked
                    goto next;
                if (!template_arg_is_accessible(arg, 0, 0, &miss))
                    goto not_found;
                arg = arg->next;
            }
        }
        
        // Access check on the entity itself
        if (entry->access_bits != 0)      // private/protected
            if (!sub_467780(entity, 1, 0)) // check access
                goto not_found;
        
        // Cache the resolved entity in bucket[3]
        bucket[3] = qword_10657E8;
        return entry;
    }
    return 0;
}
```

The walker handles three argument kinds:
- **Kind 0 (type)**: Checks the type entity's accessibility and, for class templates (kind 12 with subkind 10), recursively walks nested template arguments.
- **Kind 1 (template)**: Checks the template entity's class ancestry.
- **Kind 2 (non-type)**: Resolves the non-type argument's scope via `sub_5F9BC0`.

### sub_46A5B0 — arg_before_unnamed_template_param_arg

This function handles the generation of template arguments that appear before unnamed template parameter arguments. It determines the positional index of each argument relative to the template parameter list and calls the appropriate code-generation routine. The assert at line 4795 guards against an unexpected argument kind (must be 0, 1, or 2; kind 3 is a pack expansion sentinel).

## Category D: Variable Template Parallel Restrictions (5 errors)

Variable templates (`template<typename T> __device__ T var = ...`) used in device contexts carry the same restrictions as `__global__` function templates. The diagnostics mirror Category C exactly:

| Tag | Message |
|---|---|
| `variable_template_private_type_arg` | `A type that is defined inside a class and has private or protected access (%t) cannot be used in the template argument type of a variable template instantiation, unless the class is local to a __device__ or __global__ function` |
| `variable_template_private_template_arg` | *(private template template arg in variable template)* |
| `variable_template_unnamed_type_template_arg` | `An unnamed type (%t) cannot be used in the template argument type of a variable template template instantiation, unless the type is local to a __device__ or __global__ function` |
| `variable_template_func_local_template_arg` | `A type defined inside a __host__ function (%t) cannot be used in the template argument type of a variable template template instantiation` |
| `variable_template_lambda_template_arg` | `The closure type for a lambda (%t%s) cannot be used in the template argument type of a variable template instantiation, unless the lambda is defined within a __device__ or __global__ function, or the lambda is an 'extended lambda' and the flag --extended-lambda is specified` |

The implementation shares the same `cache_access_result_for` / `template_arg_is_accessible` pipeline described in the Category C implementation section. The only difference is the error tag and message string emitted on failure.

### Why Variable Templates Need the Same Restrictions

Variable templates instantiated with `__device__`, `__constant__`, or `__managed__` memory space are registered by the CUDA runtime using their mangled names. The host-side `.int.c` file contains registration arrays (emitted in `.nvHRDE`, `.nvHRDI`, `.nvHRCE`, `.nvHRCI` sections) whose entries are byte arrays encoding mangled variable names. The device compiler independently mangles the same variable template instantiation. Both must produce identical names, so the same visibility constraints apply.

## Category E: Static Global Template Stub (2 errors)

In whole-program compilation mode (`-rdc=false`) with `-static-global-template-stub=true`, template `__global__` functions receive `static` linkage on their host stubs. This prevents ODR violations when the same template kernel is instantiated in multiple translation units. Two scenarios are incompatible with this mode:

| Tag | Message |
|---|---|
| `extern_kernel_template` | `when "-static-global-template-stub=true", extern __global__ function template is not supported in whole program compilation mode ("-rdc=false"). To resolve the issue, either use separate compilation mode ("-rdc=true"), or explicitly set "-static-global-template-stub=false" (but see nvcc documentation about downsides of turning it off)` |
| `template_global_no_def` | `when "-static-global-template-stub=true" in whole program compilation mode ("-rdc=false"), a __global__ function template instantiation or specialization (%sq) must have a definition in the current translation unit. To resolve this issue, either use separate compilation mode ("-rdc=true"), or explicitly set "-static-global-template-stub=false" (but see nvcc documentation about downsides of turning it off)` |

### The Problem

An `extern` template kernel declaration says "this template instantiation exists elsewhere." But if the stub is `static`, there is no way for the linker to resolve the extern reference to a stub in another TU, because `static` symbols are TU-local. Similarly, a template instantiation without a definition in the current TU cannot have a static stub generated for it, because there is no body to inline.

### Resolution Paths

Both diagnostics suggest the same two alternatives:
1. **Switch to `-rdc=true`** (separate compilation): each TU gets its own device object, and cross-TU kernel references are resolved by the device linker (`nvlink`).
2. **Set `-static-global-template-stub=false`**: stubs get external linkage, allowing cross-TU references at the cost of potential ODR violations if the same template is instantiated in multiple TUs.

## Category F: Local Type Prevents Host Launch (1 error)

| Tag | Message |
|---|---|
| `local_type_used_in_global_function` | `a local type %t (defined in %sq1) used in global function %sq2 template argument, the global function cannot be launched from host code.` |

This is a warning-level diagnostic, not a hard error. It fires when a type local to a function (but not a `__host__`-function-local type, which would be Category C.1) is used as a template argument. The kernel can still be instantiated and called from device code, but the host-side launch path is blocked because the local type is not visible to the host stub generator.

This diagnostic differs from `global_func_local_template_arg` in severity and scope: it is a soft warning that the kernel "cannot be launched from host code," rather than a hard error that rejects the instantiation entirely.

## Category G: \_\_grid\_constant\_\_ in Instantiation Directives (1 error)

| Tag | Message |
|---|---|
| `grid_constant_incompat_templ_redecl` | `incompatible __grid_constant__ annotation for parameter %s in function template redeclaration (see previous declaration %p)` |

When a function template is redeclared, the `__grid_constant__` annotations on its parameters must match the original declaration. This is enforced because `__grid_constant__` affects the ABI: a parameter marked `__grid_constant__` is placed in constant memory and accessed through a different addressing mode. If a redeclaration omits the annotation, the host stub and device function would disagree on parameter layout.

The related diagnostic `grid_constant_incompat_instantiation_directive` applies to explicit instantiation directives (`template __global__ void kernel<int>(...)`) and is documented in the [__grid_constant__](../attributes/grid-constant.md) page.

## Category H: Kernel Launches from System File Templates (1 error)

| Message |
|---|
| `kernel launches from templates are not allowed in system files` |

This error fires when a `<<<...>>>` kernel launch expression appears inside a template function defined in a system header file. System headers are files marked with `#pragma system_header` or located in system include paths (e.g., the CUDA toolkit's `include/` directory).

The restriction exists because system headers are processed with relaxed diagnostics. Kernel launch expressions inside template functions in system headers would be instantiated in user code contexts, but the launch transformation (replacing `<<<...>>>` with `cudaConfigureCall` + stub call) operates during the system header's processing pass where diagnostic state may be suppressed. Rather than risk silent miscompilation, the compiler rejects this pattern outright.

## The \_\_NV\_name\_expr Mechanism (6 errors)

NVRTC (NVIDIA's runtime compilation library) provides a mechanism to obtain the mangled name of a `__global__` function or `__device__`/`__constant__` variable at compile time. This mechanism is exposed through the `__CUDACC_RTC__name_expr` intrinsic, which the frontend processes during lowered name lookup.

### Purpose

NVRTC compiles CUDA code at runtime, producing PTX that is loaded into the driver. The host application needs to look up compiled kernels and device variables by name via `cuModuleGetFunction` / `cuModuleGetGlobal`. The `__NV_name_expr` mechanism bridges this gap: the user provides a C++ name expression (e.g., `my_kernel<int>` or `my_device_var<float>`), and the compiler returns the corresponding mangled name (e.g., `_Z9my_kernelIiEvv`).

### The 6 Errors

| Tag | Message |
|---|---|
| `name_expr_parsing` | `Error in parsing name expression for lowered name lookup. Input name expression was: %sq` |
| `name_expr_extra_tokens` | `Extra tokens found after parsing name expression for lowered name lookup. Input name expression was: %sq` |
| `name_expr_internal_error` | `Internal error in parsing name expression for lowered name lookup. Input name expression was: %sq` |
| `name_expr_non_global_routine` | `Name expression cannot form address of a non-__global__ function. Input name expression was: %sq` |
| `name_expr_non_device_variable` | `Name expression cannot form address of a variable that is not a __device__/__constant__ variable. Input name expression was: %sq` |
| `name_expr_not_routine_or_variable` | `Name expression must form address of a __global__ function or the address of a __device__/__constant__ variable. Input name expression was: %sq` |

### Processing Pipeline

1. **Parsing**: The name expression is parsed as a C++ id-expression. If parsing fails, `name_expr_parsing` is emitted. If tokens remain after a successful parse, `name_expr_extra_tokens` fires.

2. **Lookup**: The parsed expression is resolved via standard C++ name lookup (qualified or unqualified, with template argument deduction if needed).

3. **Validation**: The resolved entity is checked:
   - If it is a function, it must be `__global__` (has the `__global__` execution space byte set). Otherwise: `name_expr_non_global_routine`.
   - If it is a variable, it must be `__device__` or `__constant__` (memory space bits at `entity+148`). Otherwise: `name_expr_non_device_variable`.
   - If it is neither a function nor a variable: `name_expr_not_routine_or_variable`.

4. **Mangling**: If validation passes, the entity is mangled using the Itanium ABI mangler (in `lower_name.c`) and the resulting string is recorded for NVRTC output.

### Connection to Template Restrictions

The `__NV_name_expr` mechanism relies on every template argument being mangeable. All of the Category C restrictions directly support this: if a template argument type cannot be mangled (because it is unnamed, local, private, etc.), the name expression lookup would produce a mangled name that does not match the device-side mangling. The restrictions are enforced at template instantiation time, before any name expression lookup occurs, so that invalid instantiations never reach the mangling stage.

## Data Structures

### Template Argument Node (arg_t)

The template argument node is a linked-list entry used by `sub_469F80` and `sub_46A230`:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `next` | Next argument in the list |
| +8 | 1 | `kind` | Argument kind: 0=type, 1=template, 2=non-type, 3=pack expansion |
| +24 | 1 | `flags_24` | Bit 0: is pack expansion |
| +25 | 1 | `flags_25` | Bit 4 (0x10): currently being checked (recursion guard) |
| +32 | 8 | `value` | Pointer to the type/entity/expression |

### Entity Node (type/symbol)

Relevant fields for accessibility checking:

| Offset | Size | Field | Description |
|---|---|---|---|
| +8 | 8 | `name_entry` | Name string pointer (or next scope for unnamed) |
| +24 | 8 | `alt_name` | Alternative name (for flag bit 3 at +81) |
| +40 | 8 | `scope_info` | Scope information; `+32` from this is the enclosing class/namespace |
| +80 | 1 | `access_bits` | Bits 0-1: access specifier (0=public, 1=protected, 2=private) |
| +81 | 1 | `entity_flags` | Bit 2 (0x04): is template specialization; bit 6 (0x40): is anonymous |
| +128 | 8 | `base_class` | Base class pointer (for class entities) |
| +132 | 1 | `type_kind` | Type kind: 6/8=pointer/ref, 7=function, 9-11=class/struct/enum, 12=typedef, 13=pointer-to-member, 14=dependent |
| +144 | 8 | `canonical` | Canonical type (for typedefs: the underlying type) |
| +148 | 1 | `subtype_kind` | Subkind (for type_kind 12: 10=template-id, 12=elaborated) |
| +152 | 8 | `type_info` | Type-specific data (template args, function params, etc.) |
| +160 | 1 | `template_kind` | For template entities: template kind |
| +161 | 1 | `visibility` | Bit 7 (0x80): private visibility (negative char value) |
| +162 | 2 | `extra_flags` | Bit 7 (0x80) + bit 9 (0x200): cached accessibility state |

## Diagnostic Summary

All 24 errors sorted by category:

| # | Category | Tag | Severity |
|---|---|---|---|
| 1 | A | `global_function_constexpr` | error |
| 2 | A | `global_function_consteval` | error |
| 3 | A | `global_class_decl` | error |
| 4 | A | `global_friend_definition` | error |
| 5 | A | `global_exception_spec` | error |
| 6 | A | `global_function_in_unnamed_inline_ns` | error |
| 7 | A | `global_function_with_initializer_list` | error |
| 8 | A | `global_va_list_type` | error |
| 9 | B | `global_function_pack_not_last` | error |
| 10 | B | `global_function_multiple_packs` | error |
| 11 | C | `global_func_local_template_arg` | error |
| 12 | C | `global_private_type_arg` | error |
| 13 | C | `global_unnamed_type_arg` | error |
| 14 | C | `global_lambda_template_arg` | error |
| 15 | C | `global_private_template_arg` | error |
| 16 | C | *(texture/surface non-type arg)* | error |
| 17 | D | `variable_template_private_type_arg` | error |
| 18 | D | `variable_template_private_template_arg` | error |
| 19 | D | `variable_template_unnamed_type_template_arg` | error |
| 20 | D | `variable_template_func_local_template_arg` | error |
| 21 | D | `variable_template_lambda_template_arg` | error |
| 22 | E | `extern_kernel_template` | error |
| 23 | E | `template_global_no_def` | error |
| 24 | F | `local_type_used_in_global_function` | warning |

Category G (`grid_constant_incompat_templ_redecl`) and Category H (`kernel launches from templates...`) are counted separately as they span the template/non-template boundary.

## Function Map

| Address | Identity | Lines | Role |
|---|---|---|---|
| `sub_469F80` | `template_arg_is_accessible` | 144 | Primary access checker -- dispatches on arg kind |
| `sub_469480` | `cache_access_result_for` | 670 | Hash-cached accessibility analysis |
| `sub_46A230` | *(walks template arg lists)* | 182 | Iterates entity lookup table for arg lists |
| `sub_46A5B0` | `arg_before_unnamed_template_param_arg` | 396 | Handles args before unnamed template params |
| `sub_469F30` | *(scope resolve helper)* | 23 | Resolves scope via `cache_access_result_for` + entity lookup |
| `sub_46ACC0` | *(scope walk callback)* | -- | Callback passed to IL walker `sub_61FE60` |
| `sub_467780` | *(access check)* | -- | Checks C++ access control (public/protected/private) |
| `sub_466F40` | *(output callback)* | -- | Code generation output callback |
| `sub_5BFC70` | *(pack expansion resolver)* | -- | Resolves pack expansion nodes (kind 3) |
| `sub_5F9BC0` | *(scope resolver)* | -- | Resolves entity scope chain |
| `sub_5F9C10` | *(elaborated type resolver)* | -- | Resolves elaborated type specifiers |
| `sub_7B2260` | *(type equivalence)* | -- | Checks structural type equivalence |
| `sub_61EC40` | *(init visitor)* | 27 | Initializes IL tree visitor state |
| `sub_61FE60` | *(walk expression tree)* | 17 | Walks expression tree with callback |

## Global Variables

| Global | Address | Description |
|---|---|---|
| `xmmword_F05720` | `0xF05720` | Access check cache hash table (384 KB, 16,382 entries x 24 bytes) |
| `qword_F05708` | `0xF05708` | Free list head for recycled cache entries |
| `qword_F05730` | `0xF05730` | Scope ID array parallel to cache (4 bytes per entry) |
| `unk_FE5700` | `0xFE5700` | Entity lookup hash table (512 KB) |
| `qword_1065708` | `0x1065708` | Scope stack head (linked list of scope entries) |
| `qword_126C5D0` | `0x126C5D0` | Global scope sentinel |
| `qword_10657E8` | `0x10657E8` | Current scope context for entity resolution |
| `dword_1065848` | `0x1065848` | Extended lambda mode flag |
| `dword_1065850` | `0x1065850` | Device stub mode flag |

## Cross-References

- [Template Engine](template-engine.md) -- instantiation worklist, fixpoint loop, and the `should_be_instantiated` gate
- [\_\_global\_\_ Function Attributes](../attributes/global-function.md) -- attribute application and post-validation checks
- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- host stub emission, `-static-global-template-stub` flag
- [\_\_grid\_constant\_\_](../attributes/grid-constant.md) -- parameter annotation compatibility in template redeclarations
- [CUDA Diagnostics](../diagnostics/cuda-errors.md) -- complete error catalog with all 24+ messages
- [Lambda Device Wrapper](../lambda/device-wrapper.md) -- extended lambda mechanism for closure type template args
- [Execution Spaces](../cuda/execution-spaces.md) -- host/device/global space model
- [Backend Pipeline](../pipeline/backend.md) -- initialization of hash tables used by the access checker
- [Int-C Format](../output/int-c-format.md) -- how the `.int.c` output encodes device symbol registration arrays
