# __global__ Function Constraints

The `__global__` attribute designates a CUDA kernel -- a function that executes on the GPU and is callable from host code via the `<<<...>>>` launch syntax. Of all CUDA execution space attributes, `__global__` imposes the most constraints. cudafe++ enforces these constraints across three separate validation passes: **attribute application** (when `__global__` is first applied to an entity), **post-declaration validation** (after all attributes on a declaration are resolved), and **semantic analysis** (during template instantiation, redeclaration merging, and lambda processing). This page documents all constraint checks, their implementation in the binary, the entity node fields they inspect, and the diagnostics they emit.

## Key Facts

| Property | Value |
|---|---|
| Source files | `attribute.c` (apply handler), `nv_transforms.c` (post-validation), `class_decl.c` (redeclaration, lambda), `decls.c` (template packs) |
| Apply handler (variant 1) | `sub_40E1F0` (89 lines) |
| Apply handler (variant 2) | `sub_40E7F0` (86 lines) |
| Post-validation | `sub_6BC890` (`nv_validate_cuda_attributes`, 161 lines) |
| Attribute kind byte | `0x58` = `'X'` |
| OR mask applied | `entity+182 \|= 0x61` (bits 0 + 5 + 6) |
| HD combined flag | `entity+182 \|= 0x80` (set when `__global__` applied to function already marked `__host__`) |
| Total constraint checks | 37 distinct error conditions |
| Entity fields read | `+81`, `+144`, `+148`, `+152`, `+166`, `+176`, `+179`, `+182`, `+183`, `+184`, `+191` |
| Relaxed mode flag | `dword_106BFF0` (suppresses certain conflict checks) |
| main() entity pointer | `qword_126EB70` (compared to detect `__global__ main`) |

## Two Variants of `apply_nv_global_attr`

Two nearly identical functions implement the `__global__` application logic. Both perform the same 11 validation checks and apply the same `0x61` bitmask. The difference is purely structural: `sub_40E1F0` uses a `for` loop with a null-terminated break for the parameter default-init iteration, while `sub_40E7F0` uses a `do-while` loop with an explicit null check and early return. Both exist because EDG's attribute subsystem may route through different call paths depending on whether the attribute appears on a declaration or a definition.

```c
// Pseudocode for apply_nv_global_attr (sub_40E1F0 / sub_40E7F0)
// a1: attribute node, a2: entity node, a3: target kind
entity_t* apply_nv_global_attr(attr_node_t* a1, entity_t* a2, uint8_t a3) {

    // Gate: only applies to functions (kind 11)
    if (a3 != 11)
        return a2;

    // ---- Phase 1: Linkage / constexpr lambda check ----
    // Bits 47 and 24 of the 48-bit field at +184
    if ((a2->qword_184 & 0x800001000000) == 0x800000000000) {
        // Constexpr lambda with internal linkage but no local flag
        char* name = get_entity_display_name(a2, 0);  // sub_6BC6B0
        emit_error(3469, a1->src_loc, "__global__", name);
        return a2;   // bail out, do not apply __global__
    }

    // ---- Phase 2: Structural constraints ----

    // 2a. Static member function check
    if ((signed char)a2->byte_176 < 0 && !(a2->byte_81 & 0x04))
        emit_warning(3507, a1->src_loc, "__global__");  // severity 5

    // 2b. operator() check
    if (a2->byte_166 == 5)
        emit_error(3644, a1->src_loc);  // severity 7

    // 2c. Exception specification check (uses return type chain)
    type_t* ret = a2->type_chain;  // entity+144
    while (ret->kind == 12)        // skip cv-qualifier wrappers
        ret = ret->referenced;     // type+144
    if (ret->prototype->exception_spec)  // proto+152 -> +56
        emit_error(3647, a1->src_loc);   // auto/decltype(auto) return

    // 2d. Execution space conflict
    uint8_t es = a2->byte_182;
    if (!relaxed_mode && (es & 0x60) == 0x20)  // already __device__ only
        emit_error(3481, a1->src_loc);
    if (es & 0x10)                              // already __host__ explicit
        emit_error(3481, a1->src_loc);

    // 2e. Return type must be void
    if (!(a2->byte_179 & 0x10)) {  // not constexpr
        if (a2->byte_191 & 0x01)   // is lambda
            emit_error(3506, a1->src_loc);
        else {
            type_t* base = skip_typedefs(a2->type_chain);  // sub_7A68F0
            if (!is_void_type(base->referenced))            // sub_7A6E90
                emit_error(3505, a1->src_loc);
        }
    }

    // 2f. Variadic (ellipsis) check
    type_t* proto_type = a2->type_chain;  // +144
    while (proto_type->kind == 12)
        proto_type = proto_type->referenced;
    if (proto_type->prototype->flags_16 & 0x01)  // bit 0 of proto+16
        emit_error(3503, a1->src_loc);

    // ---- Phase 3: Apply the bitmask ----
    a2->byte_182 |= 0x61;   // device_capable + device_annotation + global_kernel

    // ---- Phase 4: Additional checks (after bitmask set) ----

    // 4a. Local function (constexpr local)
    if (a2->byte_81 & 0x04)
        emit_error(3688, a1->src_loc);

    // 4b. main() function check
    if (a2 == main_entity && (a2->byte_182 & 0x20))
        emit_error(3538, a1->src_loc);

    // ---- Phase 5: Parameter iteration (__grid_constant__ warning) ----
    if (a1->flags & 0x01) {  // attr_node+11 bit 0: applies to parameters
        // Walk parameter list from prototype
        proto_type = a2->type_chain;
        while (proto_type->kind == 12)
            proto_type = proto_type->referenced;
        param_t* param = *proto_type->prototype->param_list;  // deref +152

        source_loc_t loc = a1->src_loc;  // +56
        for (; param != NULL; param = param->next) {
            // Peel cv-qualifier wrappers
            type_t* ptype = param->type;  // param[1]
            while (ptype->kind == 12)
                ptype = ptype->referenced;

            // Check: is type a __grid_constant__ candidate?
            if (!has_grid_constant_flag(ptype) && scope_index == -1) {
                // sub_7A6B60: checks byte+133 bit 5 (0x20)
                int64_t scope = scope_table_base + 784 * scope_table_index;
                if ((scope->flags_6 & 0x06) == 0 && scope->kind_4 != 12) {
                    type_t* ptype2 = param->type;
                    while (ptype2->kind == 12)
                        ptype2 = ptype2->referenced;
                    if (!ptype2->default_init)  // type+120 == NULL
                        emit_error(3669, &loc);
                }
            }
        }
    }

    // ---- Phase 6: HD combined flag ----
    if (a2->byte_182 & 0x40)       // __global__ now set
        a2->byte_182 |= 0x80;      // mark as combined HD

    return a2;
}
```

### Execution Order Detail

The `0x61` bitmask is applied **before** the local-function (3688) and main() (3538) checks but **after** all structural checks (3507, 3644, 3647, 3481, 3505/3506, 3503). This means the bitmask is set even when errors are emitted -- cudafe++ continues processing after errors to collect as many diagnostics as possible in a single compilation pass.

The constexpr-lambda check at the top (error 3469) is the only check that causes an early return. If the function is a constexpr lambda with wrong linkage, the bitmask is NOT set and no further validation is performed.

## Validation Error Catalog

The 37 validation errors are organized by the phase in which they are checked and by semantic category. Error codes below are cudafe++ internal diagnostic numbers; severity values match the `sub_4F41C0` severity parameter (5 = warning, 7 = error, 8 = hard error).

### Category 1: Return Type

| Error | Severity | Check | Message |
|---|---|---|---|
| 3505 | 7 | `!is_void_type(skip_typedefs(entity+144)->referenced)` | `a __global__ function must have a void return type` |
| 3506 | 7 | `entity+191 & 0x01` (lambda) and non-void | `a __global__ function must not have a deduced return type` |
| 3647 | 7 | `entity+152 -> +56 != NULL` (exception spec present on return proto) | auto/decltype(auto) deduced return type |

Error 3505 and 3506 are mutually exclusive paths guarded by the `byte+179 & 0x10` constexpr flag. When the function is not constexpr, the handler checks whether it is a lambda (3506 path, which checks `byte+191` bit 0) or a regular function (3505 path, which resolves through `skip_typedefs` via `sub_7A68F0` and tests `is_void_type` via `sub_7A6E90`). The `skip_typedefs` function follows the type chain while `type->kind == 12` (cv-qualifier wrapper) and `type->byte_161 & 0x7F == 0` (no qualifier flags). The `is_void_type` function follows the same chain and returns `kind == 1` (void).

Error 3647 is checked independently of 3505/3506. The check examines the exception specification pointer at prototype offset `+56`. In EDG's type system, `auto` and `decltype(auto)` return types are represented with a non-null exception specification node on the return type's prototype -- this is a repurposed field that indicates the return type is deduced.

### Category 2: Parameters

| Error | Severity | Check | Message |
|---|---|---|---|
| 3503 | 8 | `proto+16 & 0x01` (has ellipsis) | `a __global__ function cannot have ellipsis` |
| 3702 | 7 | `param_flags & 0x02` (rvalue ref) | `a __global__ function cannot have a parameter with rvalue reference type` |
| -- | 7 | Parameter with `__restrict__` on reference type | `a __global__ function cannot have a parameter with __restrict__ qualified reference type` |
| -- | 7 | Parameter of type `va_list` | `A __global__ function or function template cannot have a parameter with va_list type` |
| -- | 7 | Parameter of type `std::initializer_list` | `a __global__ function or function template cannot have a parameter with type std::initializer_list` |
| -- | 7 | Oversized alignment on win32 | `cannot pass a parameter with a too large explicit alignment to a __global__ function on win32 platforms` |
| 3669 | 8 | Device-scope parameter without default init | `__grid_constant__` parameter warning (device-side check) |

Error 3503 (ellipsis) is checked in the apply handler by testing bit 0 of the function prototype's flags word at offset `+16`. This bit indicates the parameter list ends with `...`.

Error 3702 (rvalue reference) is checked in the **post-validation** pass (`sub_6BC890`), not in the apply handler. The post-validator walks the parameter list and checks byte offset `+32` (bit 1) of each parameter node.

The `__restrict__` reference, `va_list`, `initializer_list`, and win32 alignment checks are scattered across separate validation functions in `nv_transforms.c` and are triggered during declaration processing rather than during attribute application.

Error 3669 is checked in the apply handler's parameter iteration loop. It walks each parameter, resolves through cv-qualifier wrappers, and tests whether `sub_7A6B60` returns false (meaning the parameter type has bit 5 of `byte+133` clear -- not a `__grid_constant__` type) AND the scope lookup produces a non-array, non-qualifier type without a default initializer at `type+120`.

### Category 3: Modifiers

| Error | Severity | Check | Message |
|---|---|---|---|
| 3507 | 5 | `(signed char)byte_176 < 0 && !(byte_81 & 0x04)` | `A __global__ function or function template cannot be marked constexpr` (warning for static member) |
| 3688 | 8 | `byte_81 & 0x04` (local function) | `A __global__ function or function template cannot be marked constexpr` (constexpr local) |
| 3481 | 8 | Execution space conflict (see matrix) | Conflicting CUDA execution spaces |
| -- | 7 | Function is consteval | `A __global__ function or function template cannot be marked consteval` |
| 3644 | 7 | `byte_166 == 5` (operator function kind) | `An operator function cannot be a __global__ function` |
| -- | 7 | Defined in friend declaration | `A __global__ function or function template cannot be defined in a friend declaration` |
| -- | 7 | Exception specification present | `An exception specification is not allowed for a __global__ function or function template` |
| -- | 7 | Declared in inline unnamed namespace | `A __global__ function or function template cannot be declared within an inline unnamed namespace` |
| 3538 | 7 | `a2 == qword_126EB70` (is `main()`) | `function main cannot be marked __device__ or __global__` |

Error 3507 deserves special attention. The decompiled code shows:
```c
if ((signed char)a2->byte_176 < 0 && !(a2->byte_81 & 0x04))
    emit_warning(3507, ...);
```
The `signed char` cast means `byte_176 >= 0x80` (bit 7 set = static member function). The `!(byte_81 & 0x04)` condition ensures it is NOT a local function. The emitter uses severity 5 (warning via `sub_4F8DB0`), meaning this is a **warning**, not an error -- NVIDIA chose to warn rather than reject `__global__` on static members, though the official documentation says it is not allowed. The displayed string is `"A __global__ function or function template cannot be marked constexpr"` with `"__global__"` as the attribute name parameter, though the actual semantic is "static member function" per the field being checked.

Error 3644 checks `entity+166 == 5`. This field stores the "operator function kind" enum value, where 5 corresponds to `operator()`. This prevents lambda call operators or functors from being directly marked `__global__`.

Error 3688 is checked **after** the bitmask is set (`byte_182 |= 0x61`). It tests `byte_81 & 0x04`, which indicates a local (block-scope) function. The handler emits with severity 8 (via `sub_4F81B0`, hard error).

Error 3538 compares the entity pointer against `qword_126EB70`, which holds the entity pointer for `main()` (set during initial declaration processing). The condition also requires `byte_182 & 0x20` (device annotation bit set), which is always true after `|= 0x61`.

### Category 4: Template Constraints

| Error | Severity | Check | Message |
|---|---|---|---|
| -- | 7 | Pack parameter is not last template parameter | `Pack template parameter must be the last template parameter for a variadic __global__ function template` |
| -- | 7 | Multiple pack parameters | `Multiple pack parameters are not allowed for a variadic __global__ function template` |

These checks are performed during template declaration processing in `decls.c`, not in the apply handler. They constrain variadic `__global__` function templates: CUDA requires that pack parameters appear last (so the runtime can enumerate kernel arguments), and only a single pack is permitted (the CUDA launch infrastructure cannot handle multiple parameter packs).

### Category 5: Redeclaration

| Error | Severity | Check | Message |
|---|---|---|---|
| -- | 7 | Previously `__global__`, now no execution space | `a __global__ function(%no1) redeclared without __global__` |
| -- | 7 | Previously `__global__`, now `__host__` | `a __global__ function(%no1) redeclared with __host__` |
| -- | 7 | Previously `__global__`, now `__device__` | `a __global__ function(%no1) redeclared with __device__` |
| -- | 7 | Previously `__global__`, now `__host__ __device__` | `a __global__ function(%no1) redeclared with __host__ __device__` |

These four error variants are symmetrical with the reverse direction:
- `a __device__ function(%no1) redeclared with __global__`
- `a __host__ function(%no1) redeclared with __global__`
- `a __host__ __device__ function(%no1) redeclared with __global__`

Redeclaration checks occur during declaration merging in `class_decl.c`. When a function is redeclared and the execution space of the new declaration does not match the original, cudafe++ emits one of these errors. The `%no1` format specifier inserts the function name. These checks run independently of the `apply_nv_global_attr` handler -- they operate on the merged entity after both attribute sets have been processed.

### Category 6: Constexpr Lambda Linkage

| Error | Severity | Check | Message |
|---|---|---|---|
| 3469 | 5 | `(qword_184 & 0x800001000000) == 0x800000000000` | `__global__` on constexpr lambda with wrong linkage |

This is the first check in the apply handler and the only one that causes early return. The 48-bit field at `entity+184` encodes template and linkage properties. Bit 47 (`0x800000000000`) indicates internal linkage or a similar constraint, while bit 24 (`0x000001000000`) indicates a local entity. When bit 47 is set but bit 24 is clear, the entity is a constexpr lambda that cannot legally receive `__global__`. The handler calls `sub_6BC6B0` (`get_entity_display_name`) to format the entity name for the diagnostic message, then returns without setting the bitmask.

### Category 7: Post-Validation (sub_6BC890)

These checks run after all attributes on a declaration have been applied, in the `nv_validate_cuda_attributes` function:

| Error | Severity | Check | Message |
|---|---|---|---|
| 3702 | 7 | Parameter with rvalue reference flag (bit 1 at param+32) | `a __global__ function cannot have a parameter with rvalue reference type` |
| 3661 | 7 | `__nv_register_params__` on `__global__` | `__nv_register_params__ is not allowed on a __global__ function` |
| 3534 | 7 | `__launch_bounds__` on non-`__global__` | `%s attribute is not allowed on a non-__global__ function` |
| 3707 | 7 | maxBlocksPerCluster < cluster product | `total number of blocks in cluster computed from %s exceeds __launch_bounds__ specified limit` |
| 3715 | 7 | `__maxnreg__` on non-`__global__` | `__maxnreg__ is not allowed on a non-__global__ function` |
| 3719 | 7 | Both `__launch_bounds__` and `__maxnreg__` | `__launch_bounds__ and __maxnreg__ may not be used on the same declaration` |
| 3695 | 4 | `__global__` without `__launch_bounds__` | `no __launch_bounds__ specified for __global__ function` (warning) |

Error 3695 is a severity-4 diagnostic (informational warning). It fires when a `__global__` function has no associated launch configuration, encouraging developers to specify `__launch_bounds__` for optimal register allocation. This is the only constraint that is a soft advisory rather than a hard or standard error.

## Entity Node Field Reference

The apply handler reads and writes specific fields within the entity node. Complete field semantics:

| Offset | Size | Field Name | Role in `__global__` Validation |
|---|---|---|---|
| `+81` | 1 byte | `local_flags` | Bit 2 (0x04): function is local (block-scope). Checked for 3688 and as exemption for 3507. |
| `+144` | 8 bytes | `type_chain` | Pointer to return type. Followed through `kind==12` cv-qualifier wrappers. |
| `+152` | 8 bytes | `prototype` | Function prototype pointer. At prototype+16: flags (bit 0 = ellipsis). At prototype+56: exception spec pointer. At prototype+0: parameter list head (double deref for first param). |
| `+166` | 1 byte | `operator_kind` | Value 5 = `operator()`. Checked for 3644. |
| `+176` | 1 byte | `member_flags` | Bit 7 (0x80, checked as `signed char < 0`): static member function. Checked for 3507. |
| `+179` | 1 byte | `constexpr_flags` | Bit 4 (0x10): function is constexpr. Guards 3505/3506 check (skipped if constexpr). |
| `+182` | 1 byte | `execution_space` | The primary execution space bitfield. `\|= 0x61` sets global kernel. Read for conflict checks (0x60, 0x10 masks). |
| `+183` | 1 byte | `extended_cuda` | Bit 3 (0x08): `__nv_register_params__`. Checked in post-validation. Bit 6 (0x40): `__cluster_dims__` set. |
| `+184` | 8 bytes | `linkage_template` | 48-bit field encoding template/linkage flags. Only lower 48 bits used; mask `0x800001000000` checks constexpr lambda linkage. |
| `+191` | 1 byte | `lambda_flags` | Bit 0 (0x01): entity is a lambda. Routes to 3506 instead of 3505 for void-return check. |
| `+256` | 8 bytes | `launch_config` | Pointer to launch configuration struct (56 bytes). NULL if no launch attributes applied. Read in post-validation. |

## The 0x61 Bitmask

The OR mask `0x61` sets three bits in the execution space byte:

```
0x61 = 0b01100001

  bit 0 (0x01):  device_capable     -- function can run on device
  bit 5 (0x20):  device_annotation  -- has explicit device-side annotation
  bit 6 (0x40):  global_kernel      -- function is a __global__ kernel
```

Bit 0 is shared with `__device__` (0x23) and `__host__` (0x15). It serves as a "has CUDA annotation" predicate -- any entity with bit 0 set has been explicitly annotated with at least one execution space keyword. This enables fast `if (byte_182 & 0x01)` checks throughout the codebase.

Bit 5 is shared with `__device__`. A `__global__` function is considered device-annotated because kernel code executes on the GPU.

Bit 6 is unique to `__global__`. The mask `byte_182 & 0x40` is the canonical predicate for "is this a kernel function?" used in dozens of locations throughout the binary.

### HD Combined Flag (0x80)

After setting `0x61`, the handler checks whether bit 6 (`0x40`, global kernel) is now set. If so, it ORs `0x80` into the byte. This bit means "combined host+device" and is set as a secondary effect. The logic at the end of the function:

```c
if (a2->byte_182 & 0x40)       // just set via |= 0x61
    a2->byte_182 |= 0x80;      // always true after apply
```

This means every `__global__` function ends up with `byte_182 & 0x80` set, which marks it as "combined" in the execution space classification. This is semantically correct: a kernel has both a host-side stub (for launching) and device-side code (for execution).

## Parameter Iteration for __grid_constant__

The final section of the apply handler iterates the function's parameter list to check for parameters that should be annotated `__grid_constant__`. This check only runs when `attr_node->flags` bit 0 (`a1+11 & 0x01`) is set, indicating the attribute application context includes parameter-level processing.

The iteration follows this structure:

```c
// Navigate to function prototype
type_t* proto_type = entity->type_chain;     // +144
while (proto_type->kind == 12)               // skip cv-qualifiers
    proto_type = proto_type->referenced;      // +144

// Get parameter list head (double dereference)
param_t** param_list = proto_type->prototype->param_head;  // proto+152 -> deref
param_t* param = *param_list;                               // deref again

for (; param != NULL; param = param->next) {
    // Navigate to unqualified parameter type
    type_t* ptype = param[1];    // param->type (offset 8)
    while (ptype->kind == 12)
        ptype = ptype->referenced;

    // sub_7A6B60: checks byte+133 bit 5 (0x20) -- "has __grid_constant__"
    bool has_gc = (ptype->byte_133 & 0x20) != 0;

    if (!has_gc && dword_126C5C4 == -1) {
        // Scope table lookup
        int64_t scope = qword_126C5E8 + 784 * dword_126C5E4;
        uint8_t scope_flags = scope->byte_6;
        uint8_t scope_kind = scope->byte_4;

        // Skip if scope has qualifier flags or is a cv-qualified scope
        if ((scope_flags & 0x06) == 0 && scope_kind != 12) {
            // Re-navigate to unqualified type
            type_t* ptype2 = param[1];
            while (ptype2->kind == 12)
                ptype2 = ptype2->referenced;

            // Check for default initializer
            if (ptype2->qword_120 == 0)
                emit_error(3669, &saved_source_loc);
        }
    }
}
```

The scope table lookup uses a 784-byte scope structure (at `qword_126C5E8` indexed by `dword_126C5E4`) to determine whether the current context is device-side. The `dword_126C5C4 == -1` check verifies we are in device compilation mode. This entire parameter iteration is a device-side warning mechanism: it alerts developers when a kernel parameter lacks a default initializer in a context where `__grid_constant__` would be appropriate.

## Post-Declaration Validation (sub_6BC890)

After all attributes on a declaration are applied, `nv_validate_cuda_attributes` (`sub_6BC890`, 161 lines) performs cross-attribute consistency checks. For `__global__` functions, this function enforces:

### Rvalue Reference Parameters (3702)

```c
// Walk parameter list
type_t* ret = entity->type_chain;
while (ret->kind == 12)
    ret = ret->referenced;
param_t* param = **((param_t***)ret + 19);  // proto -> param list

while (param) {
    if (param->byte_32 & 0x02)  // rvalue reference flag
        emit_error(3702, source_loc);
    param = param->next;
}
```

This check scans all parameters for the rvalue reference flag (bit 1 at parameter node offset `+32`). Kernel functions cannot accept rvalue references because kernel launch involves copying arguments through the CUDA runtime, which does not support move semantics across the host-device boundary.

### __nv_register_params__ Conflict (3661)

```c
if (entity->byte_183 & 0x08) {  // __nv_register_params__ set
    if (entity->byte_182 & 0x40)
        emit_error(3661, ..., "__global__");
    else if ((entity->byte_182 & 0x30) == 0x20)
        emit_error(3661, ..., "__host__");
}
```

The `__nv_register_params__` attribute (bit 3 of `byte+183`) is incompatible with `__global__` because kernel parameter passing uses a fixed ABI that cannot be overridden.

### Launch Configuration Without __global__ (3534)

```c
launch_config_t* lc = entity->launch_config;  // +256
if (lc && !(entity->byte_182 & 0x40)) {
    if (lc->maxThreadsPerBlock || lc->minBlocksPerMultiprocessor)
        emit_error(3534, ..., "__launch_bounds__");
}
```

The `__launch_bounds__`, `__cluster_dims__`, and `__block_size__` attributes require `__global__`. If a non-kernel function has any of these, error 3534 fires.

### Cluster Dimension Product Check (3707)

```c
if (lc->cluster_dim_x > 0 && lc->maxBlocksPerCluster > 0) {
    uint64_t product = lc->cluster_dim_x * lc->cluster_dim_y * lc->cluster_dim_z;
    if (lc->maxBlocksPerCluster < product)
        emit_error(3707, ...);
}
```

### __launch_bounds__ and __maxnreg__ Conflict (3719)

```c
if (lc->maxThreadsPerBlock && lc->maxnreg >= 0)
    emit_error(3719, ..., "__launch_bounds__ and __maxnreg__");
```

These two attributes provide contradictory register pressure hints and cannot coexist.

### Missing __launch_bounds__ Warning (3695)

```c
if ((entity->byte_182 & 0x40) &&
    (!lc || (!lc->maxThreadsPerBlock && !lc->minBlocksPerMultiprocessor)))
    emit_warning(3695);
```

Severity 4 (advisory). Encourages developers to annotate kernels with `__launch_bounds__` for optimal register allocation.

## Execution Space Conflict Matrix

When `__global__` is applied to a function that already has an execution space annotation, the handler checks for conflicts using two conditions:

```c
// Condition 1: already __device__ only (without relaxed mode)
if (!dword_106BFF0 && (byte_182 & 0x60) == 0x20)
    error(3481);

// Condition 2: already __host__ explicit
if (byte_182 & 0x10)
    error(3481);
```

| Current `byte_182` | Applying `__global__` | `(byte & 0x60) == 0x20` | `byte & 0x10` | Result |
|---|---|---|---|---|
| `0x00` (none) | `\|= 0x61` -> `0x61` | false | false | accepted |
| `0x23` (`__device__`) | | true | false | error 3481 (unless relaxed) |
| `0x15` (`__host__`) | | false | true | error 3481 |
| `0x37` (`__host__ __device__`) | | false | true | error 3481 |
| `0x61` (`__global__`) | | true | false | error 3481 (unless relaxed) -- idempotent bitmask |

In relaxed mode (`dword_106BFF0 != 0`), the first condition is suppressed, allowing `__device__` + `__global__` combinations. The second condition (explicit `__host__`) is never relaxed.

## Helper Functions

| Address | Identity | Lines | Purpose |
|---|---|---|---|
| `sub_6BC6B0` | `get_entity_display_name` | 49 | Formats entity name for diagnostic messages. Handles demangling, strips leading `::`. |
| `sub_7A68F0` | `skip_typedefs` | 19 | Follows type chain through `kind==12` wrappers while `byte_161 & 0x7F == 0`. |
| `sub_7A6E90` | `is_void_type` | 16 | Follows type chain through `kind==12`, returns `kind == 1`. |
| `sub_7A6B60` | `has_grid_constant_flag` | 9 | Follows type chain through `kind==12`, returns `byte_133 & 0x20`. |
| `sub_4F7510` | `emit_error_with_names` | 66 | Emits error with two string arguments (attribute name + entity name). |
| `sub_4F8DB0` | `emit_warning_with_name` | 38 | Emits warning (severity 5) with one string argument. |
| `sub_4F8200` | `emit_error_basic` | 10 | Emits error with severity + code + source location. |
| `sub_4F81B0` | `emit_error_minimal` | 10 | Emits error (severity 8) with code + source location. |
| `sub_4F8490` | `emit_error_with_extra` | 38 | Emits error with one supplementary argument. |

## Additional __global__ Constraints (Outside Apply Handler)

Beyond the apply handler and post-validation, several other subsystems enforce `__global__`-specific rules. These checks occur during template instantiation, lambda processing, and declaration merging:

### Template Argument Type Restrictions

CUDA restricts which types can appear as template arguments in `__global__` function template instantiations:

- **Host-local types** (defined inside a `__host__` function) cannot be used
- **Private/protected class members** cannot be used (unless the class is local to a `__device__`/`__global__` function)
- **Unnamed types** cannot be used (unless local to a `__device__`/`__global__` function)
- **Lambda closure types** cannot be used (unless the lambda is defined in a `__device__`/`__global__` function, or is an extended lambda with `--extended-lambda`)
- **Texture/surface variables** cannot be used as non-type template arguments
- **Private/protected template template arguments** from class scope cannot be used

### Static Global Template Stub

In whole-program compilation mode (`-rdc=false`) with `-static-global-template-stub=true`:
- Extern `__global__` function templates are not supported
- `__global__` function template instantiations must have definitions in the current TU

### Device-Side Restrictions

Functions marked `__global__` (or `__device__`) are subject to additional restrictions during semantic analysis:
- `address of label` extension is not supported
- ASM operands may specify only one constraint letter
- Certain ASM constraint letters are forbidden
- Texture/surface variables cannot have their address taken or be indirected
- Anonymous union member variables at global/namespace scope cannot be directly accessed
- Function-scope static variables require a memory space specifier
- Dynamic initialization of function-scope static variables is not supported

## IL Emission Path

`__global__` collapses entirely into entity bits at application time. The parse-time attribute IL node (kind `0x48`, byte `+8 = 'X'`) is consumed by `apply_nv_global_attr` and never reaches the IL output dispatcher. The post-application footprint is:

- `entity+182` bits 0, 5, 6 (mask `0x61`) -- the canonical "is kernel" predicate read by every downstream pass.
- `entity+182` bit 7 (mask `0x80`) -- the combined HD flag, set automatically after `0x61` is applied.
- `entity+256` -- optional launch-config struct pointer if `__launch_bounds__`/`__cluster_dims__`/`__block_size__` were also applied; written by those handlers, not by `apply_nv_global_attr` itself.

Three downstream emitters react to `byte_182 & 0x40`:

1. **Kernel stub generator** -- emits the host-side launch wrapper (`<<<...>>>` lowering, `cudaPushCallConfiguration`/`cudaLaunchKernel` sequence) into `.int.c`. The stub references the kernel's mangled name and the launch-config struct fields.
2. **Device-IL marker** (`mark_to_keep_in_il`) -- sets the prefix byte bit 7 (`0x80`, "keep in IL") on the routine entry so the device-side IL output retains the kernel definition.
3. **Device-host splitter** -- ensures the function body is preserved in the device IL while the host IL retains only the stub.

There is no `kind 25` re-emission for `__global__` and no preserved chain entry. The kernel-ness of the function is conveyed downstream entirely through entity bytes; cicc reads them via the host stub's serialization and the device IL's keep-in-il marking.

## Function Map

| Address | Identity | Lines | Source File |
|---|---|---|---|
| `sub_40E1F0` | `apply_nv_global_attr` (variant 1) | 89 | `attribute.c` |
| `sub_40E7F0` | `apply_nv_global_attr` (variant 2) | 86 | `attribute.c` |
| `sub_6BC890` | `nv_validate_cuda_attributes` | 161 | `nv_transforms.c` |
| `sub_6BC6B0` | `get_entity_display_name` | 49 | `nv_transforms.c` |
| `sub_7A68F0` | `skip_typedefs` | 19 | `types.c` |
| `sub_7A6E90` | `is_void_type` | 16 | `types.c` |
| `sub_7A6B60` | `has_grid_constant_flag` | 9 | `types.c` |
| `sub_4F7510` | `emit_error_with_names` | 66 | `error.c` |
| `sub_4F8DB0` | `emit_warning_with_name` | 38 | `error.c` |
| `sub_4F8200` | `emit_error_basic` | 10 | `error.c` |
| `sub_4F81B0` | `emit_error_minimal` | 10 | `error.c` |
| `sub_4F8490` | `emit_error_with_extra` | 38 | `error.c` |
| `sub_413240` | `apply_one_attribute` (dispatch) | 585 | `attribute.c` |

## Global Variables

| Global | Address | Purpose |
|---|---|---|
| `dword_106BFF0` | `0x106BFF0` | Relaxed mode flag. When set, suppresses `__device__` + `__global__` conflict (3481). |
| `qword_126EB70` | `0x126EB70` | Pointer to the entity node for `main()`. Compared during 3538 check. |
| `dword_126C5C4` | `0x126C5C4` | Scope index sentinel (`-1` = device compilation mode). Guards 3669 parameter check. |
| `dword_126C5E4` | `0x126C5E4` | Current scope table index. |
| `qword_126C5E8` | `0x126C5E8` | Scope table base pointer. Each entry is 784 bytes. |

## Cross-References

- [Execution Spaces](../cuda/execution-spaces.md) -- bitfield layout, conflict matrix, virtual override checking
- [Attribute System Overview](overview.md) -- dispatch table, attribute node structure, application pipeline
- [__grid_constant__](grid-constant.md) -- the parameter attribute that interacts with the 3669 check
- [Launch Configuration Attributes](launch-config.md) -- `__launch_bounds__`, `__cluster_dims__`, `__block_size__` (post-validation errors 3534, 3707, 3715, 3719, 3695)
- [Entity Node Layout](../structs/entity-node.md) -- full byte map with all CUDA fields
- [Kernel Stubs](../cuda/kernel-stubs.md) -- host-side stub generation triggered by `byte_182 & 0x40`
- [CUDA Template Restrictions](../edg/template-cuda.md) -- template argument type restrictions for `__global__` instantiations
- [Diagnostics Overview](../diagnostics/overview.md) -- error emission functions and severity levels
