# __grid_constant__

The `__grid_constant__` attribute marks a `__global__` function parameter as read-only across the entire kernel grid. When applied, the parameter is loaded once from host memory into GPU constant memory at grid launch, and all threads in the grid read from this cached copy instead of loading from the parameter buffer in global memory. The attribute was introduced in CUDA 11.7 and requires compute capability 7.0 or later (Volta+).

cudafe++ enforces 8 validation checks on `__grid_constant__` parameters, distributed across three phases: **attribute application** (checking type constraints -- const qualification, no reference types, SM version), **post-declaration validation** (checking that the annotation appears only on `__global__` function parameters), and **redeclaration/template merging** (checking consistency of annotations between declarations). A ninth related check (error 3669) in the `__global__` apply handler issues an advisory when a kernel parameter lacks a default initializer in device compilation mode, suggesting that `__grid_constant__` would be appropriate.

## Key Facts

| Property | Value |
|---|---|
| Internal keyword | `grid_constant` (stored at `0x82bf0f`), displayed as `__grid_constant__` (at `0x82bf1d`) |
| Attribute category | Optimization (parameter-level) |
| Minimum architecture | compute_70 (Volta), gated by `dword_126E4A8 >= 70` |
| Entity node flag | `entity+164` bit 2 (`0x04`) -- set on the parameter entity during attribute application |
| Type node flag | `type+133` bit 5 (`0x20`) -- checked by `sub_7A6B60` (type chain query) |
| Parameter node flag | `param+32` bit 1 (`0x02`) -- checked during post-declaration validation in `sub_6BC890` |
| Total diagnostics | 8 unique error strings + 1 related advisory (3669) + 1 memory space conflict (3577) |
| Diagnostic tag prefix | `grid_constant_*` (8 tags in `.rodata` at `0x84810f`--`0x857770`) |
| Message string block | `0x88d8b0`--`0x88dbe8` (contiguous block in `.rodata`) |

## Why __grid_constant__ Exists

A parameter annotated `__grid_constant__` tells the CUDA runtime and compiler three things:

**1. The parameter value is identical for every thread in the grid.**
This is inherently true for all kernel parameters -- they are passed by value through the kernel launch API -- but the annotation makes this guarantee explicit and mechanically exploitable.

**2. The parameter lives in constant memory, not the parameter buffer.**
Without the annotation, kernel parameters are placed in a parameter buffer that threads read from global memory (or a dedicated parameter memory space with limited caching). With `__grid_constant__`, the runtime loads the parameter into the GPU's constant memory cache at launch time. This provides:

- **Broadcast reads**: all 32 threads in a warp reading the same constant-memory address execute in a single memory transaction. The uniform cache serves a broadcast at full throughput.
- **Separate cache hierarchy**: constant memory has a dedicated L1 cache (the "uniform cache") separate from the general L1/L2 data caches. Using it for grid-wide parameters reduces pressure on the main cache hierarchy.
- **Reduced register pressure**: the compiler can re-read the parameter from constant memory at any point instead of keeping it pinned in a register. This frees registers for other values, improving occupancy.

**3. The parameter must be const-qualified.**
Since the value is shared across the grid and cached in constant memory, writes would be nonsensical. The hardware constant memory is read-only from the kernel's perspective. cudafe++ enforces this at the type level.

**4. The parameter must not be a reference type.**
References to host memory are meaningless on the device. Kernel parameters are already copied to the device by the CUDA runtime. A reference would dangle because it would point into host address space. Even a reference to device memory is not valid here -- `__grid_constant__` parameters must be values, not indirections.

### SM_70+ Requirement Rationale

The compute_70 (Volta) minimum exists because Volta significantly rearchitected the constant memory subsystem. Pre-Volta GPUs (Maxwell, Pascal) have a more restricted constant memory subsystem with a fixed 64 KB window per kernel. Volta introduced:

- Larger effective constant memory through improved caching
- Per-thread-block constant buffer indexing
- Hardware support for grid-wide parameter broadcasting with the new parameter cache architecture

The compiler lowers `__grid_constant__` parameters to `ld.const` (constant-space load) PTX instructions, which rely on the Volta constant memory architecture to function correctly. On pre-Volta hardware, the constant memory hardware cannot serve this use case.

## Where Validation Happens

The `__grid_constant__` validation logic is spread across multiple compilation phases because the checks require different kinds of information. The type-level checks (const, reference) can be performed as soon as the attribute is applied. The context check (must be on a `__global__` parameter) requires the function's execution space to be resolved. The redeclaration checks require both the old and new declarations to be available.

### Phase 1: Attribute Application

Checks 1 (const), 2 (reference), and 4 (architecture) execute during attribute application, when the `__grid_constant__` attribute handler runs. This handler is registered in EDG's attribute descriptor table under the kind byte for `__grid_constant__`. It receives the attribute node, the entity node, and the target kind. The handler inspects the parameter's type node to verify const-qualification and absence of reference semantics, and checks `dword_126E4A8` against the threshold value 70.

### Phase 2: Post-Declaration Validation

Check 3 (must be on `__global__` parameter) executes in `nv_validate_cuda_attributes` (`sub_6BC890`). This function runs after all attributes on a declaration have been applied and resolved. It walks the function's parameter list and checks whether any parameter carries the `__grid_constant__` flag (`param+32` bit 1) on a non-`__global__` function.

### Phase 3: Redeclaration/Template Merging

Checks 5--8 (consistency across redeclarations, template redeclarations, specializations, and explicit instantiations) execute during the declaration merging passes in `class_decl.c`, `decls.c`, and `template.c`. These passes compare the `entity+164` bit 2 flag on corresponding parameters of the old and new declarations.

## Validation Check 1: const-Qualified Type

| Property | Value |
|---|---|
| Tag | `grid_constant_not_const` (at `0x848146`) |
| Message | `a parameter annotated with __grid_constant__ must have const-qualified type` (at `0x88d8b0`) |
| Severity | error |
| Phase | Attribute application |

The parameter's type must carry the `const` qualifier. The check peels through the type chain, following cv-qualifier wrapper nodes (`kind == 12`) to reach the underlying type, then verifies the const flag is present.

The type-level check works on the same type chain navigation pattern used throughout EDG's type system:

```c
// Conceptual logic (from the __grid_constant__ attribute handler)
type_t* ptype = param->type;
while (ptype->kind == 12)        // skip cv-qualifier wrapper nodes
    ptype = ptype->referenced;   // follow chain at type+144

if (!(ptype->cv_quals & CONST_FLAG))
    emit_error("grid_constant_not_const", param->src_loc);
```

If the user writes:

```cuda
__global__ void kernel(__grid_constant__ int x) { ... }
```

cudafe++ emits `grid_constant_not_const` because `int` is not const-qualified. The correct form is:

```cuda
__global__ void kernel(__grid_constant__ const int x) { ... }
```

## Validation Check 2: No Reference Type

| Property | Value |
|---|---|
| Tag | `grid_constant_reference_type` (at `0x84815e`) |
| Message | `a parameter annotated with __grid_constant__ must not have reference type` (at `0x88d900`) |
| Severity | error |
| Phase | Attribute application |

The parameter must not be a reference (`&` or `&&`). This check fires independently of the const check -- both can fire on the same parameter.

In EDG's type system, reference types have `kind == 7` (lvalue reference) or `kind == 19` (rvalue reference). The check walks the type chain through cv-qualifier wrappers and tests the final type kind:

```c
type_t* ptype = param->type;
while (ptype->kind == 12)
    ptype = ptype->referenced;

if (ptype->kind == 7 || ptype->kind == 19)   // lvalue ref or rvalue ref
    emit_error("grid_constant_reference_type", param->src_loc);
```

Example that triggers this error:

```cuda
__global__ void kernel(__grid_constant__ const int& x) { ... }
```

The rationale is that kernel parameters are copied across the host-device boundary by the CUDA runtime. A reference to host memory would be invalid on the device, and a reference to device memory does not participate in the kernel launch parameter copying mechanism. The `__grid_constant__` attribute specifically requests constant-memory placement of the parameter *value* -- a reference has no value to place.

## Validation Check 3: Only on __global__ Parameters

| Property | Value |
|---|---|
| Tag | `grid_constant_non_kernel` (at `0x84812d`) |
| Message | `__grid_constant__ annotation is only allowed on a parameter of a __global__ function` (at `0x88db38`) |
| Error code | 3702 |
| Severity | 7 (standard error) |
| Phase | Post-declaration validation (`sub_6BC890`) |

This check enforces that `__grid_constant__` only appears on parameters of `__global__` (kernel) functions. Parameters of `__device__` or `__host__ __device__` functions do not participate in the kernel launch mechanism and have no grid-wide constant memory optimization path.

The check executes in `nv_validate_cuda_attributes` (`sub_6BC890`, 161 lines, `nv_transforms.c`). The validator navigates from the function entity to its parameter list, then walks each parameter testing for the `__grid_constant__` flag. The reconstructed pseudocode:

```c
// From nv_validate_cuda_attributes (sub_6BC890)
// a1: function entity node
// a2: pointer to source location for diagnostics

void nv_validate_cuda_attributes(entity_t* a1, source_loc_t* a2) {

    if (!a1 || (a1->byte_177 & 0x10))
        return;   // null entity or suppressed

    type_t* type_chain = a1->type_chain;   // entity+144
    uint8_t exec_space = a1->byte_182;     // execution space bitfield

    // Skip parameter walk under certain execution space conditions
    if (!type_chain || ((exec_space & 0x30) == 0x20 &&
                        (exec_space & 0x60) != 0x20))
        goto skip_param_walk;

    // Navigate through cv-qualifier wrappers to reach the function type
    while (type_chain->kind == 12)
        type_chain = type_chain->referenced;   // type+144

    // Get parameter list from prototype (double dereference)
    param_t* param = **(param_t***)(type_chain + 152);

    // Walk each parameter
    while (param) {
        if (param->byte_32 & 0x02) {
            // __grid_constant__ flag is set on a non-__global__ parameter
            emit_error(7, 3702, a2);   // grid_constant_non_kernel
        }
        param = param->next;
    }

    // ... (continues with __launch_bounds__ validation below)
}
```

The `param->byte_32 & 0x02` test checks bit 1 of the parameter node's byte at offset `+32`. This bit is the `__grid_constant__` flag on the parameter entity node -- it is set by the `__grid_constant__` attribute application handler when the attribute is first applied, and checked here to verify the containing function is actually a kernel.

The error fires for any execution space that is NOT `__global__`. The condition skip at the top of the function (`(exec_space & 0x30) == 0x20 && (exec_space & 0x60) != 0x20`) is a pre-filter that handles certain host-side function configurations -- it does NOT suppress the parameter walk for `__global__` functions (which have bit 6 = `0x40` set).

## Validation Check 4: compute_70+ Architecture

| Property | Value |
|---|---|
| Tag | `grid_constant_unsupported_arch` (at `0x857770`) |
| Message | `__grid_constant__ annotation is only allowed for architecture compute_70 or later` (at `0x88db90`) |
| Severity | error |
| Phase | Attribute application |

The target architecture, stored in `dword_126E4A8` (set by the `--target` CLI flag via case 245 in `proc_command_line`), must be >= 70. The architecture code is an integer representation: `sm_70` maps to 70, `sm_80` to 80, `sm_90` to 90, etc.

```c
// Architecture gate in the __grid_constant__ attribute handler
if (dword_126E4A8 < 70)
    emit_error("grid_constant_unsupported_arch", param->src_loc);
```

If the user compiles with `-arch=compute_60` or lower and uses `__grid_constant__`, this error fires. The check is a straightforward integer comparison -- no bitmask, no table lookup.

The architecture value reaches cudafe++ through nvcc, which translates user-facing flags like `--gpu-architecture=sm_70` into the internal numeric code and passes it via the `--target` flag. Inside cudafe++, `sub_7525E0` (a 6-byte stub returning `-1`) nominally parses this value, but the actual number is injected by nvcc into the argument string. See [Architecture Feature Gating](../cuda/arch-gating.md) for the full data flow.

## Validation Checks 5--8: Redeclaration Consistency

The four redeclaration consistency checks share the same algorithmic structure but apply to different declaration contexts. They all enforce the invariant that `__grid_constant__` annotations must match between declarations: if the first declaration annotates a parameter with `__grid_constant__`, every subsequent declaration (redeclaration, template redeclaration, specialization, explicit instantiation) must also annotate the corresponding parameter, and vice versa.

### Why These Checks Exist

The `__grid_constant__` attribute affects the kernel's ABI -- specifically, how the CUDA runtime passes the parameter at launch time. If one translation unit sees a declaration with `__grid_constant__` and another sees a declaration without it, they would generate incompatible kernel launch code. In RDC (relocatable device code) mode, where kernels can be declared in one TU and defined in another, this mismatch would cause silent data corruption at runtime. The compiler catches it at declaration merging time to prevent this.

### Check 5: Function Redeclaration

| Property | Value |
|---|---|
| Tag | `grid_constant_incompat_redecl` (at `0x84810f`) |
| Message | `incompatible __grid_constant__ annotation for parameter %s in function redeclaration (see previous declaration %p)` (at `0x88d950`) |
| Phase | Redeclaration merging (`class_decl.c`) |

When a `__global__` function is redeclared, cudafe++ compares the `entity+164` bit 2 (`0x04`) flag on each parameter between the existing and new declarations. If the flags differ for any parameter at the same position, the error fires.

```c
// Redeclaration consistency check (conceptual, in class_decl.c)
param_t* old_param = get_params(old_decl);
param_t* new_param = get_params(new_decl);

while (old_param && new_param) {
    bool old_gc = (old_param->entity->byte_164 & 0x04) != 0;
    bool new_gc = (new_param->entity->byte_164 & 0x04) != 0;

    if (old_gc != new_gc)
        emit_error("grid_constant_incompat_redecl",
                   new_param->name, old_decl->src_loc);

    old_param = old_param->next;
    new_param = new_param->next;
}
```

Example:

```cuda
__global__ void kernel(__grid_constant__ const int x);
__global__ void kernel(const int x);  // ERROR: grid_constant_incompat_redecl
```

The `%s` in the message is expanded to the parameter name, and `%p` is expanded to a source location reference pointing at the previous declaration.

### Check 6: Function Template Redeclaration

| Property | Value |
|---|---|
| Tag | `grid_constant_incompat_templ_redecl` (at `0x857748`) |
| Message | `incompatible __grid_constant__ annotation for parameter %s in function template redeclaration (see previous declaration %p)` (at `0x88d9c8`) |
| Phase | Template redeclaration merging (`class_decl.c`) |

Same logic as check 5, but for function template redeclarations. Template redeclaration merging occurs in a separate code path from regular function redeclaration because template entities have additional metadata (template parameter lists, partial specialization chains) that must be reconciled.

```cuda
template<typename T>
__global__ void kernel(__grid_constant__ const T x);

template<typename T>
__global__ void kernel(const T x);  // ERROR: grid_constant_incompat_templ_redecl
```

### Check 7: Template Specialization

| Property | Value |
|---|---|
| Tag | `grid_constant_incompat_specialization` (at `0x857720`) |
| Message | `incompatible __grid_constant__ annotation for parameter %s in function specialization (see previous declaration %p)` (at `0x88da48`) |
| Phase | Template specialization processing |

When a function template specialization's `__grid_constant__` annotations disagree with the primary template, this error fires. The specialization must preserve the `__grid_constant__` annotation from the primary template because the compiler may have already committed to constant-memory parameter placement based on the primary template's declaration.

```cuda
template<typename T>
__global__ void kernel(__grid_constant__ const T x);

template<>
__global__ void kernel<int>(const int x);  // ERROR: grid_constant_incompat_specialization
```

A specialization that omits the annotation would require a different ABI for that particular instantiation, which the kernel launch infrastructure cannot accommodate on a per-specialization basis.

### Check 8: Explicit Instantiation Directive

| Property | Value |
|---|---|
| Tag | `grid_constant_incompat_instantiation_directive` (at `0x8576f0`) |
| Message | `incompatible __grid_constant__ annotation for parameter %s in instantiation directive (see previous declaration %p)` (at `0x88dac0`) |
| Phase | Explicit instantiation processing |

This mirrors the specialization check but applies to explicit instantiation declarations and definitions (`template void ...` and `extern template void ...`).

```cuda
template<typename T>
__global__ void kernel(__grid_constant__ const T x) { ... }

template __global__ void kernel<int>(const int x);
// ERROR: grid_constant_incompat_instantiation_directive
```

The instantiation directive must match the primary template's `__grid_constant__` annotation for each parameter.

## Memory Space Conflict Check (Error 3577)

While not one of the 8 `__grid_constant__` validation checks, error 3577 provides a guard in the reverse direction. When `apply_nv_managed_attr` (`sub_40E0D0`) or `apply_nv_device_attr` (`sub_40EB80`) applies a memory space attribute to a variable, they check whether the entity has the `__grid_constant__` flag set at `entity+164` bit 2. If so, and the variable also has a memory space qualifier, error 3577 is emitted with the name of the conflicting memory space.

The check is identical in both handlers. Here is the reconstructed pseudocode from `apply_nv_managed_attr` (`sub_40E0D0`):

```c
// From apply_nv_managed_attr (sub_40E0D0, attribute.c:10523)
// a1: attribute node, a2: entity node, a3: target kind (must be 7 = variable)

entity_t* apply_nv_managed_attr(attr_node_t* a1, entity_t* a2, uint8_t a3) {

    // Gate: variables only
    if (a3 != 7)
        internal_error("apply_nv_managed_attr", "attribute.c", 10523);

    // Apply memory space flags
    uint8_t old_memspace = a2->byte_148;
    a2->byte_149 |= 0x01;        // set __managed__ flag
    a2->byte_148 = old_memspace | 0x01;  // set __device__ flag (managed implies device)

    // Check for conflicting memory space combinations
    if (((old_memspace & 0x02) != 0) + ((old_memspace & 0x04) != 0) == 2)
        emit_error(3481, a1->src_loc);   // both __shared__ and __constant__ set

    if ((signed char)a2->byte_161 < 0)
        emit_error(3482, a1->src_loc);   // thread_local conflict

    if (a2->byte_81 & 0x04)
        emit_error(3485, a1->src_loc);   // local variable conflict

    // Grid constant conflict check
    if ((a2->byte_164 & 0x04) != 0      // has __grid_constant__ flag
        && (*(uint16_t*)(a2 + 148) & 0x0102) != 0)  // __shared__ OR __managed__
    {
        // Determine which memory space to report in the diagnostic
        uint8_t mem = a2->byte_148;
        const char* space;
        if      (mem & 0x04)         space = "__constant__";
        else if (a2->byte_149 & 0x01) space = "__managed__";
        else if (mem & 0x02)         space = "__shared__";
        else if (mem & 0x01)         space = "__device__";
        else                         space = "";

        emit_error_with_string(3577, a1->src_loc, space);
    }

    return a2;
}
```

The `0x0102` mask on the 16-bit word at `a2 + 148` checks two bits: bit 1 of byte `+148` (`__shared__`, value `0x02`) and bit 0 of byte `+149` (`__managed__`, value `0x01` shifted left by 8 bits = `0x0100`). This means the conflict check fires specifically when a `__grid_constant__` parameter also has `__shared__` or `__managed__` -- these memory spaces are incompatible with constant memory placement.

The priority order for the diagnostic message (`__constant__` > `__managed__` > `__shared__` > `__device__`) determines which memory space name appears in the error output when multiple conflicting spaces are present simultaneously.

The `apply_nv_device_attr` handler (`sub_40EB80`) performs the identical check in its variable-handling branch (when `a3 == 7`):

```c
// From apply_nv_device_attr (sub_40EB80), variable branch
if (a3 == 7) {
    a2->byte_148 |= 0x01;         // set __device__ flag

    // ... shared/constant conflict, thread_local, local variable checks ...

    // Identical grid_constant conflict check
    if ((a2->byte_164 & 0x04) != 0 && (*(uint16_t*)(a2 + 148) & 0x0102) != 0) {
        // Same priority cascade for space name
        // ...
        emit_error_with_string(3577, a1->src_loc, space);
    }
    return a2;
}
```

## Entity Node Fields

Three distinct locations in entity/type/parameter nodes carry `__grid_constant__` state:

### entity+164 bit 2 (0x04): Grid Constant Declaration Flag

Set during attribute application when a parameter is declared `__grid_constant__`. This is the "declaration-side" flag that records the programmer's intent. Used by:
- Memory space conflict check (error 3577) in `apply_nv_managed_attr` and `apply_nv_device_attr`
- Redeclaration consistency checks (checks 5--8)

### type+133 bit 5 (0x20): Type-Level Flag

A flag on the type node (not the entity node) checked by `sub_7A6B60`. This function follows the type chain through cv-qualifier wrappers (`kind == 12`) and tests `byte+133 & 0x20`:

```c
// sub_7A6B60 (types.c)
// In the broader EDG type system, this function checks bit 5 of the
// type's flag byte. For CUDA parameter types, this bit indicates
// __grid_constant__ annotation. The same bit is also used as the
// dependent-type flag in template contexts (hence 299 callers in the binary).
bool type_has_flag_0x20(type_t* type) {
    while (type->kind == 12)       // skip cv-qualifier wrappers
        type = type->referenced;   // follow type chain at +144
    return (type->byte_133 & 0x20) != 0;
}
```

Used by the `__global__` apply handler's parameter iteration to detect parameters that are already annotated with `__grid_constant__`, suppressing the error 3669 advisory for those parameters.

### param+32 bit 1 (0x02): Parameter Node Flag

A flag on the parameter node itself, checked during post-declaration validation (`sub_6BC890`). The validator walks the parameter list and tests each parameter's byte at offset `+32` for bit 1. If set on a parameter of a non-`__global__` function, error 3702 (`grid_constant_non_kernel`) is emitted.

The three flags serve different purposes: the entity flag records the declaration intent and is used for cross-declaration consistency checks, the type flag enables efficient type-level queries during attribute application, and the parameter flag enables the post-validation pass to scan parameter lists without resolving entity or type chains.

## IL Emission Path

Unlike the execution- and memory-space attributes, `__grid_constant__` is **not** a pure entity-bit collapse. The parse-time attribute IL node (kind `0x48`, byte `+8 = '_'` for `__grid_constant__` is *not* assigned -- the attribute lacks a dedicated CUDA kind byte and arrives through the generic GNU/scoped path) deposits state into three locations simultaneously:

1. `entity+164` bit 2 -- declaration-side flag, read by redeclaration consistency checks (5--8).
2. `type+133` bit 5 -- type-level flag, read by `sub_7A6B60` from the `__global__` apply handler to suppress error 3669.
3. `param+32` bit 1 -- parameter-side flag, read by `nv_validate_cuda_attributes` (`sub_6BC890`) to detect non-`__global__` use (error 3702).

After application, the attribute node is **kept on the entity's attribute chain** so the `.int.c` writer can re-emit the `__grid_constant__` token into the kernel parameter declaration. cicc reads the textual annotation and lowers the parameter to a constant-memory (`ld.const`) load instead of the default parameter-buffer (`ld.param`) load. There is no kind-25 IL re-emission like `__launch_bounds__`/`__nv_pure__`; the attribute survives in its original `0x48` form on the chain because parameter-level attributes are emitted inline with their parameter, not as a separate function attribute.

The three-flag layout is deliberate: the declaration flag enables cross-translation-unit ABI consistency, the type flag enables fast queries during expression-time validation without dereferencing the entity, and the parameter flag enables the post-validation pass to walk parameter lists without dereferencing the type chain.

## Parameter Iteration in the __global__ Apply Handler

The `apply_nv_global_attr` handlers (`sub_40E1F0` and `sub_40E7F0`) contain a parameter iteration loop that interacts with `__grid_constant__`. This loop checks each kernel parameter for types that should be `__grid_constant__` but are not annotated as such. When found in device compilation mode (`dword_126C5C4 == -1`), error 3669 is emitted as an advisory.

```c
// From apply_nv_global_attr (sub_40E1F0), Phase 5: parameter iteration
// This section runs only when attr_node+11 bit 0 is set (applies to parameters)

if (a1->byte_11 & 0x01) {

    // Navigate to function prototype through cv-qualifier chain
    type_t* proto_type = entity->type_chain;     // entity+144
    while (proto_type->kind == 12)
        proto_type = proto_type->referenced;

    // Get parameter list head (double dereference from prototype+152)
    param_t* param = **(param_t***)(proto_type + 152);
    source_loc_t saved_loc = a1->src_loc;        // attr_node+56

    for (; param != NULL; param = param->next) {
        // Peel cv-qualifier wrappers from parameter type
        type_t* ptype = param->type;             // param[1] (offset 8)
        while (ptype->kind == 12)
            ptype = ptype->referenced;

        // sub_7A6B60: returns true if type+133 bit 5 is set
        // (parameter is already __grid_constant__)
        if (!sub_7A6B60(ptype) && dword_126C5C4 == -1) {

            // Scope table lookup (784-byte entries)
            int64_t scope = qword_126C5E8 + 784 * dword_126C5E4;

            // Skip if scope has qualifier flags or is a cv-qualified scope
            if ((scope->byte_6 & 0x06) == 0 && scope->byte_4 != 12) {

                // Re-navigate to unqualified type
                type_t* ptype2 = param->type;
                while (ptype2->kind == 12)
                    ptype2 = ptype2->referenced;

                // If no default initializer, suggest __grid_constant__
                if (ptype2->qword_120 == 0)
                    emit_error(3669, &saved_loc);
            }
        }
    }
}
```

The logic: for each parameter in a `__global__` function, if the parameter type does NOT already have the `__grid_constant__` flag AND we are in device compilation mode AND the current scope is not a cv-qualified context AND the parameter type lacks a default initializer (the `type+120` pointer is null), then emit error 3669 as an advisory. The advisory nudges kernel authors to add `__grid_constant__` annotations for better performance.

The scope table lookup (`qword_126C5E8` indexed by `dword_126C5E4`, 784-byte entries) determines whether the current compilation context is device-side. The `dword_126C5C4 == -1` sentinel explicitly indicates device compilation mode. Together these two conditions ensure the advisory only fires when processing the device-side compilation of a kernel, not during host-side stub generation.

## Keyword Registration

The `__grid_constant__` keyword is registered during `fe_translation_unit_init` (`sub_5863A0`), alongside other CUDA extension keywords (`__device__`, `__global__`, `__shared__`, `__constant__`, `__managed__`, `__launch_bounds__`). The registration inserts both `grid_constant` (bare form, for attribute name lookup) and `__grid_constant__` (double-underscore form, for lexer recognition) into EDG's keyword-to-token-ID mapping.

The attribute name lookup function (`sub_40A250`) strips leading and trailing double underscores before searching the attribute name hash table (`qword_E7FB60`), so `__grid_constant__` resolves to the same descriptor entry as the bare `grid_constant` form.

## Diagnostic Tag Summary

| Tag | Error Code | Message | Phase |
|---|---|---|---|
| `grid_constant_not_const` | -- | `a parameter annotated with __grid_constant__ must have const-qualified type` | Application |
| `grid_constant_reference_type` | -- | `a parameter annotated with __grid_constant__ must not have reference type` | Application |
| `grid_constant_non_kernel` | 3702 | `__grid_constant__ annotation is only allowed on a parameter of a __global__ function` | Post-validation |
| `grid_constant_unsupported_arch` | -- | `__grid_constant__ annotation is only allowed for architecture compute_70 or later` | Application |
| `grid_constant_incompat_redecl` | -- | `incompatible __grid_constant__ annotation for parameter %s in function redeclaration (see previous declaration %p)` | Redeclaration |
| `grid_constant_incompat_templ_redecl` | -- | `incompatible __grid_constant__ annotation for parameter %s in function template redeclaration (see previous declaration %p)` | Template redecl |
| `grid_constant_incompat_specialization` | -- | `incompatible __grid_constant__ annotation for parameter %s in function specialization (see previous declaration %p)` | Specialization |
| `grid_constant_incompat_instantiation_directive` | -- | `incompatible __grid_constant__ annotation for parameter %s in instantiation directive (see previous declaration %p)` | Instantiation |

Error codes for checks 1, 2, 4--8 are not individually mapped in the decompiled code available for this analysis. Error 3702 (check 3) is confirmed from the post-validation function `sub_6BC890`. Error 3577 (memory space conflict) is confirmed from `sub_40E0D0` and `sub_40EB80`.

## Function Map

| Address | Identity | Lines | Source File | Role |
|---|---|---|---|---|
| `sub_7A6B60` | type flag query (`byte_133 & 0x20`) | 9 | `types.c` | Follows type chain, returns grid_constant / dependent flag |
| `sub_40E0D0` | `apply_nv_managed_attr` | 47 | `attribute.c:10523` | Memory space conflict check (3577) for `__managed__` |
| `sub_40EB80` | `apply_nv_device_attr` | 100 | `attribute.c` | Memory space conflict check (3577) for `__device__` |
| `sub_6BC890` | `nv_validate_cuda_attributes` | 161 | `nv_transforms.c` | Post-validation: param walk for 3702 (`grid_constant_non_kernel`) |
| `sub_40E1F0` | `apply_nv_global_attr` (variant 1) | 89 | `attribute.c` | Parameter iteration with grid_constant flag check (3669 advisory) |
| `sub_40E7F0` | `apply_nv_global_attr` (variant 2) | 86 | `attribute.c` | Same parameter iteration (alternate call path, `do-while` loop) |
| `sub_5863A0` | `fe_translation_unit_init` | -- | `fe_init.c` | Registers `__grid_constant__` keyword |
| `sub_40A250` | attribute name lookup | -- | `attribute.c` | Strips `__` prefix/suffix, searches hash table |

## Global Variables

| Global | Address | Purpose |
|---|---|---|
| `dword_126E4A8` | `0x126E4A8` | Target SM architecture code (from `--target`). Must be >= 70 for `__grid_constant__`. |
| `dword_126C5C4` | `0x126C5C4` | Scope index sentinel. `-1` = device compilation mode. Guards 3669 advisory check. |
| `dword_126C5E4` | `0x126C5E4` | Current scope table index. Used in 3669 scope lookup. |
| `qword_126C5E8` | `0x126C5E8` | Scope table base pointer (784-byte entries). Used in 3669 scope lookup. |

## Cross-References

- [Attribute System Overview](overview.md) -- attribute node structure, dispatch pipeline, kind byte enumeration
- [\_\_global\_\_ Function Constraints](global-function.md) -- parameter iteration for `__grid_constant__` advisory (error 3669), full apply handler pseudocode
- [Entity Node Layout](../structs/entity-node.md) -- `entity+164` bit 2 (grid_constant flag), `param+32` bit 1
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- all 8 `grid_constant_*` diagnostic tags
- [CLI Flag Inventory](../config/cli-flags.md) -- `--target` flag setting `dword_126E4A8`
- [Architecture Feature Gating](../cuda/arch-gating.md) -- SM version gating mechanism, `dword_126E4A8` data flow
- [CUDA Memory Spaces](../cuda/memory-spaces.md) -- constant memory semantics, error 3577 conflict
- [RDC Mode](../cuda/rdc-mode.md) -- why redeclaration consistency matters across translation units
