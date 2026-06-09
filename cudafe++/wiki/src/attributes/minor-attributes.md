# Minor CUDA Attributes

cudafe++ defines several CUDA-specific attributes beyond the core execution-space, memory-space, and launch-configuration families. These attributes serve diverse purposes: optimization hints for the downstream compiler, parameter passing strategy selection, inline control that bridges the EDG front-end with cicc's code generation, and internal annotations for tile/cooperative infrastructure. Most are undocumented by NVIDIA. This page covers each in detail: what the attribute does, why it exists, how cudafe++ validates and stores it, and where the flags end up in the entity node.

## Attribute Summary

| Kind | Hex | ASCII | Display Name | Category | Handler / Flag |
|------|------|-------|--------------|----------|----------------|
| 110 | 0x6E | `'n'` | `__nv_pure__` | Optimization | entity+183 (via IL propagation) |
| — | — | — | `__nv_register_params__` | ABI | `sub_40B0A0` (38 lines), entity+183 bit 3 |
| — | — | — | `__forceinline__` | Inline control | entity+177 bit 4 |
| — | — | — | `__noinline__` | Inline control | `sub_40F5F0` / `sub_40F6F0`, entity+179 bit 5, entity+180 bit 7 |
| — | — | — | `__inline_hint__` | Inline control | entity+179 bit 4 |
| 89 | 0x59 | `'Y'` | `__tile_global__` | Internal | (no handler observed) |
| 95 | 0x5F | `'_'` | `__tile_builtin__` | Internal | (no handler observed) |
| 94 | 0x5E | `'^'` | `__local_maxnreg__` | Launch config | `sub_411090` (67 lines) |
| 108 | 0x6C | `'l'` | `__block_size__` | Launch config | `sub_4109E0` (265 lines) |

Note: `__nv_register_params__`, `__forceinline__`, `__noinline__`, and `__inline_hint__` do not have CUDA attribute kind codes. They are processed through different paths (EDG's standard attribute system, pragma-like registration at startup, or direct flag manipulation). Only `__nv_pure__`, `__tile_global__`, `__tile_builtin__`, `__local_maxnreg__`, and `__block_size__` have dedicated CUDA kind bytes in the `attribute_display_name` switch table.

---

## `__nv_pure__` (Kind 0x6E = `'n'`)

### Purpose

`__nv_pure__` marks a function as having no observable side effects: given the same inputs, it always returns the same result and does not modify any state visible to the caller. This is an **optimization hint for cicc** (the CUDA compiler backend). A pure function can be:

- **Common-subexpression eliminated (CSE):** if `f(x)` appears twice in the same basic block, the second call can be replaced by the first call's result.
- **Hoisted out of loops:** if `f(x)` is invariant across loop iterations, it can be computed once before the loop (LICM — loop-invariant code motion).
- **Dead-code eliminated:** if the result of `f(x)` is never used and the function has no side effects, the call can be removed entirely.

This is semantically equivalent to GCC's `__attribute__((pure))` and LLVM's `readonly` function attribute, but expressed through NVIDIA's internal attribute system rather than the standard GNU attribute path. The choice of a separate internal attribute rather than reusing the GNU `pure` attribute reflects cudafe++'s design of routing all CUDA-specific semantics through its own kind-byte dispatch, keeping the NVIDIA optimization pipeline cleanly separated from EDG's standard attribute handling.

### Binary Encoding

In the attribute kind enum, `__nv_pure__` has kind value 110 (0x6E, ASCII `'n'`). This is the highest kind value in the CUDA attribute range, added later than the original dense block (86--95).

The `attribute_display_name` switch (`sub_40A310`) maps it:

```c
case 'n': return "__nv_pure__";
```

### Application Behavior

In the `apply_one_attribute` constraint checker (`sub_413240`), kind `'n'` has the following entry:

```c
case 'n':
    if (target_kind == 28)   // target is a namespace-level entity
        goto LABEL_21;       // -> pass through (no per-entity modification)
    goto LABEL_8;            // -> attribute doesn't apply to this target
```

The handler does **not** modify any entity node fields directly. Unlike `__host__` or `__device__` which set bitmask flags at `entity+182`, `__nv_pure__` propagates through the attribute node list itself. The attribute node with kind `0x6E` remains attached to the entity's attribute chain and is consumed later by:

1. The `.int.c` output generator (`sub_5565E0` and related functions), which emits the `__nv_pure__` attribute into the intermediate C output. In the IL code generator, kind `0x6E` shares handling with `__launch_bounds__` (`0x5C`):

```c
case 0x5C:
case 0x6E:
    a2->kind_field = 25;    // IL node type for "function attribute"
    sub_540560(0, 0, a2, a4, ...);  // emit attribute to .int.c
    break;
```

2. cicc then reads the `__nv_pure__` annotation from the `.int.c` output and applies the corresponding LLVM-level optimization attributes (`readonly`, `willreturn`, etc.) to the function in the NVVM IR.

### Why It Exists

CUDA device code has optimization opportunities that GCC's `pure` does not capture. Device functions execute in a constrained environment (no system calls, no I/O, deterministic memory model), which makes purity easier to verify and more valuable to exploit. By providing `__nv_pure__` as a separate internal attribute, NVIDIA can:

- Gate it behind CUDA mode (it only appears in device compilation flows).
- Attach it to internal runtime functions (`__shfl_sync`, math intrinsics, etc.) that NVIDIA knows are pure but that cannot carry GCC `pure` through the host compilation path.
- Avoid interactions with EDG's GNU attribute conflict checking, which has its own rules for `pure` vs `const` vs `noreturn`.

### String Evidence

The string table contains exactly one reference to `__nv_pure__` at address `0x829848`, and a diagnostic tag `nv_pure` at `0x88cc08`. The low reference count confirms this is an internal optimization attribute not exposed to user code through documented CUDA APIs.

---

## `__nv_register_params__` (Entity+183 bit 3)

### Purpose

`__nv_register_params__` tells cicc to pass kernel parameters in **registers** instead of through **constant memory**. By default, CUDA kernel parameters are loaded via `ld.param` instructions, which access a dedicated constant memory bank visible to the kernel launch mechanism. This works well when parameter counts are large (the constant memory bank is 4 KB per kernel), but for small parameter counts, passing values directly in registers avoids the latency of the constant memory load path.

Register parameter passing eliminates the constant-bank load latency (typically 4--8 cycles on modern architectures) and removes potential bank conflicts when multiple warps read the same parameters. The trade-off is that it consumes registers from the limited register file, which can reduce occupancy if the kernel already uses many registers.

### Requirements

The attribute has four validation checks, enforced across two separate locations:

1. **Enablement flag** (`dword_106C028`): a compiler internal flag that must be set. If not set, the handler emits error 3659 with the message `"__nv_register_params__ support is not enabled"`. This flag is controlled by an internal nvcc option, not exposed to users.

2. **Architecture check** (implied by error string): the string `"__nv_register_params__ is only supported for compute_80 or later architecture"` exists in the binary at `0x88cb80`. This check is performed outside the apply handler, in the post-validation or downstream pipeline.

3. **Function type restriction** (implied by error string): the string `"__nv_register_params__ is not allowed on a %s function"` at `0x88cbd0` shows that certain function types (likely `__host__` or non-kernel functions) are rejected. The post-validation in `sub_6BC890` checks: if `entity+183 & 0x08` is set (register_params flag) but the execution space at `entity+182` is `__global__` (bit 6) or the function is not a pure `__device__` function, it emits error 3661 with the relevant space name.

4. **Ellipsis (variadic) check**: the apply handler (`sub_40B0A0`) traverses the function's return type chain to reach the prototype, then checks `prototype+16 & 0x01` (the variadic flag). If set, it emits error 3662 with the message `"__nv_register_params__ is not allowed on a function with ellipsis"`. Variadic functions cannot use register parameter passing because the parameter count is not known at compile time.

### Apply Handler: `sub_40B0A0` (38 lines)

```c
// sub_40B0A0 -- apply_nv_register_params_attr (attribute.c:10537)
entity_t* apply_nv_register_params_attr(attr_node_t* a1, entity_t* a2, uint8_t a3) {
    assert(a3 == 11);  // functions only

    bool enabled = true;
    if (!dword_106C028) {       // enablement flag not set
        emit_error(7, 3659, a1->src_loc);  // "support is not enabled"
        enabled = false;
    }

    if (!a2) return a2;

    // Walk return type chain to get function prototype
    type_t* ret_type = a2->type_at_144;
    if (!ret_type) goto set_flag;

    while (ret_type->kind == 12)     // skip cv-qualifier wrappers
        ret_type = ret_type->next;   // +144

    // Check variadic flag
    if (ret_type->prototype->flags_16 & 0x01) {
        emit_error(7, 3662, a1->src_loc);  // "not allowed on variadic"
        return a2;
    }

set_flag:
    if (enabled)
        a2->byte_183 |= 0x08;  // set register_params bit
    return a2;
}
```

The flag is stored at `entity+183` bit 3 (0x08), the same byte that holds the cluster_dims intent flag (bit 6, 0x40). These two flags coexist without conflict because they serve orthogonal purposes.

### Post-Declaration Validation

In `sub_6BC890` (`nv_validate_cuda_attributes`), if `entity+183 & 0x08` is set:

```c
if (entity->byte_183 & 0x08) {
    uint8_t es = entity->byte_182;
    if (es & 0x40) {                    // __global__ function
        emit_error(7, 3661, src, "__global__");
    } else if ((es & 0x30) != 0x20) {   // not pure __device__
        emit_error(7, 3661, src, "__host__");
    }
    // else: pure __device__ function -- register_params is valid
}
```

This means `__nv_register_params__` is only valid on `__device__` functions (not `__global__`, not `__host__`, not `__host__ __device__`). Kernel functions (`__global__`) have their own parameter passing ABI dictated by the CUDA runtime, and host functions use the host ABI.

### Registration at Startup

The function `sub_6B5E50` (called during compiler initialization) registers `__nv_register_params__` as a preprocessor macro expansion. It looks up the name via `sub_734430`, and if not found, creates a new macro definition node and registers it in the symbol table via `sub_749600`. The macro body is a 40-byte token sequence that, when expanded, produces the `__attribute__((__nv_register_params__))` syntax that EDG's attribute parser can consume. This macro-based registration is why `__nv_register_params__` does not have a CUDA kind byte — it enters the attribute system through the standard GNU `__attribute__` path, not through the CUDA attribute descriptor table.

The same startup function also registers `__noinline__` with a similar mechanism, and `_Pragma` (if Clang compatibility mode requires it).

---

## Inline Control Attributes

cudafe++ provides three inline control attributes that interact with EDG's inline heuristic system. These attributes do not have CUDA kind bytes; they are processed through EDG's standard attribute infrastructure and NVIDIA's own flag-setting paths.

### Entity Node Fields

```text
entity+177  cuda_flags (byte):
    bit 4 (0x10) = __forceinline__

entity+179  more_cuda_flags (byte):
    bit 4 (0x10) = __inline_hint__
    bit 5 (0x20) = __noinline__ (EDG internal noinline)

entity+180  function_attrs (byte):
    bit 7 (0x80) = __noinline__ (GNU attribute form)
```

### `__forceinline__`

`__forceinline__` requests that the compiler always inline the function, overriding cost-based heuristics. It is stored at `entity+177` bit 4 (0x10). This bit is checked during cross-execution-space call validation (`sub_505720`): a `__forceinline__` function is treated as **implicitly host-device**, meaning it suppresses cross-space call errors. The logic in the cross-space checker:

```c
if (entity->byte_177 & 0x10)    // __forceinline__
    // treat as implicitly __host__ __device__
```

This relaxation exists because `__forceinline__` functions are expected to be inlined at the call site, so their execution space becomes the caller's execution space. There is no separate call to resolve, hence no cross-space violation.

In the `.int.c` output, `__forceinline__` is emitted so that cicc can apply it during NVVM IR generation. cicc translates it to LLVM's `alwaysinline` attribute.

### `__noinline__`

`__noinline__` prevents the compiler from inlining a function, regardless of heuristics. It has **two separate handlers** because it can arrive through two syntactic paths:

**Path 1: EDG internal form** (`sub_40F5F0`, 51 lines)

This handler is invoked when `__noinline__` is recognized as a CUDA-specific attribute (source_mode 3 or with the scoped-attribute bit set). It sets `entity+179 |= 0x20`. In C mode (`dword_126EFB4 == 2`), it additionally creates an ABI annotation node by calling `sub_5E5130` and linking it to the function's prototype exception-spec chain at `prototype+56`. This ABI node carries flags `0x19` and signals to the code generator that the noinline directive should be preserved across compilation boundaries.

```c
// sub_40F5F0 -- apply_noinline_attr (EDG internal path)
if (target_kind == 11) {  // function
    if (attr->kind) {
        entity->byte_179 |= 0x20;         // noinline flag
        if (attr->source_mode == 3 && dword_126EFB4 == 2) {
            // Create ABI annotation for C mode
            extract_func_type(entity+144, &ft_out);
            if (!ft_out->prototype->abi_info) {
                abi_node_t* n = alloc_abi_node();
                *n |= 0x19;
                ft_out->prototype->abi_info = n;
            }
        }
    }
    return entity;
}
// else: emit error 1835 (wrong target) or 2470 (alignas context)
```

**Path 2: GNU attribute form** (`sub_40F6F0`, 37 lines)

This handler is invoked when `__noinline__` arrives through the `__attribute__((__noinline__))` GNU attribute path. It sets a **different bit**: `entity+180 |= 0x80`. This separation allows the compiler to distinguish between the CUDA-specific noinline directive and the GNU portable one, although in practice both prevent inlining.

Additionally, when the function is a device function (byte+176 bit 7 set = static member, source_mode indicates GNU/Clang, byte+81 bit 2 set = local, byte+187 bit 0 clear), it calls `sub_5CEE70(28, entity->attr_chain)` to record the noinline directive for device-side compilation.

```c
// sub_40F6F0 -- apply_noinline_attr (GNU form)
if (target_kind == 11) {
    entity->byte_180 |= 0x80;
    if ((signed char)entity->byte_176 < 0
        && (attr->source_mode == 2 || (attr->flags & 0x10))
        && (entity->byte_81 & 0x04)
        && !(entity->byte_187 & 0x01)) {
        sub_5CEE70(28, entity->attr_chain);
    }
} else {
    // emit error 1835/2470 with appropriate severity
}
```

### `__inline_hint__`

`__inline_hint__` is an internal NVIDIA attribute that provides a non-binding suggestion to the compiler's inlining heuristics. Unlike `__forceinline__`, which mandates inlining, `__inline_hint__` merely biases the cost model in favor of inlining. It is stored at `entity+179` bit 4 (0x10).

The attribute is registered through the same startup mechanism as `__nv_register_params__` in `sub_6B5E50`, and its handler `apply_nv_inline_hint_attr` (referenced at address `0x40A999` within `sub_40A8A0`) sets the flag. The diagnostic tag `nv_inline_hint` exists at `0x82bf2f` in the string table, suggesting diagnostic messages exist for conflicts.

### Mutual Exclusion

`__forceinline__` and `__noinline__` are mutually exclusive. The diagnostic system includes 2 messages for inline hint conflicts (identified in the W053 error report). When both are applied to the same function, the compiler emits a diagnostic. However, `__inline_hint__` can coexist with either, as it is merely a suggestion that the other directives override.

The mutual exclusion is enforced through the constraint checker in `apply_one_attribute` (`sub_413240`) and through post-validation checks. The constraint string for the `'r'` (routine/function) constraint class includes property codes `m` (for member/constexpr) and `v` (for virtual), with `+` and `-` qualifiers controlling whether the attribute is allowed or forbidden. Error codes 1835--1843 and 1858--1871 cover the various conflict scenarios.

### IL Output

In the `.int.c` output, the inline control attributes are emitted as standard GNU `__attribute__` annotations:

```c
// emitted for __noinline__:
__attribute__((noinline))

// emitted for __forceinline__:
__attribute__((always_inline))
```

cicc reads these and translates them to LLVM's `noinline` and `alwaysinline` function attributes respectively.

---

## `__tile_global__` (Kind 0x59 = `'Y'`)

### Purpose

`__tile_global__` is an **internal** execution-space attribute that appears in the `attribute_display_name` switch table but has no user-facing documentation. Its kind value (89, `'Y'`) places it in the original dense block of CUDA attributes between `__global__` (88, `'X'`) and `__shared__` (90, `'Z'`).

The name strongly suggests this attribute is related to NVIDIA's **tile-based cooperative group infrastructure** or the **Tensor Memory Accelerator (TMA)** programming model, where "tile global" would denote a function that operates on a tile of global memory. In the cooperative groups model, tiled partitions allow threads to cooperatively access contiguous memory regions, and a `__tile_global__` function might be the kernel entry point for such a tiled execution pattern.

### Binary Evidence

The attribute is defined in the kind enum (the `attribute_display_name` switch case), but no handler function has been identified in the binary. In the `apply_one_attribute` dispatcher (`sub_413240`), there is no case for kind `'Y'`. This means:

- The attribute can be parsed and stored in an attribute node.
- It has a display name for diagnostics.
- It does **not** modify entity node fields through the standard apply pipeline.

This is consistent with the attribute being consumed downstream by cicc or another tool in the compilation pipeline, rather than requiring cudafe++ to perform validation beyond basic parsing. Alternatively, it may be a reserved placeholder for future functionality.

---

## `__tile_builtin__` (Kind 0x5F = `'_'`)

### Purpose

`__tile_builtin__` is another **internal** attribute in the CUDA kind enum, with kind value 95 (0x5F, ASCII `'_'`). Its kind value is the last in the original dense block (86--95).

The name suggests this attribute marks functions that are **tile-level builtins** — compiler intrinsics that implement tile-based operations. These would be functions like `cooperative_groups::tiled_partition::shfl()`, `cooperative_groups::tiled_partition::ballot()`, or TMA copy intrinsics, which are compiled by cudafe++ as ordinary function calls but need special handling by cicc for efficient code generation.

### Binary Evidence

Like `__tile_global__`, `__tile_builtin__` has no handler in the `apply_one_attribute` dispatcher. It appears only in the `attribute_display_name` switch table. The attribute node with kind `0x5F` passes through cudafe++ without entity node modification and is consumed by the downstream compiler.

The pairing of `__tile_global__` (Y) and `__tile_builtin__` (_) suggests a two-part infrastructure:
- `__tile_global__` marks kernel-level entry points for tiled execution.
- `__tile_builtin__` marks the intrinsic operations available within that tiled execution context.

---

## `__local_maxnreg__` (Kind 0x5E = `'^'`)

### Purpose

`__local_maxnreg__` sets a per-function register limit, as opposed to `__maxnreg__` which is per-kernel. The distinction matters for `__device__` helper functions called from kernels: `__maxnreg__` can only be applied to `__global__` functions, but `__local_maxnreg__` can be applied to any device function. This allows fine-grained register pressure tuning at the function level without requiring the entire kernel to be constrained.

When cicc compiles a `__device__` function with `__local_maxnreg__`, it sets the target register limit for that specific function during register allocation, potentially spilling more aggressively to local memory. The surrounding kernel can use a different register budget.

### Apply Handler: `sub_411090` (67 lines)

The handler is structurally identical to `sub_410F70` (`__maxnreg__`), differing only in the offset within the launch config struct where it stores the value:

```c
// sub_411090 -- apply_nv_local_maxnreg_attr
entity_t* apply_nv_local_maxnreg_attr(attr_node_t* a1, entity_t* a2, ...) {
    // Allocate launch config struct if needed
    if (!entity->launch_config)
        entity->launch_config = allocate_launch_config();  // sub_5E52F0

    // Skip if template-dependent argument
    if (is_dependent_type(arg))
        return entity;

    // Validate: must be positive
    if (const_expr_sign_compare(arg, 0) <= 0) {   // sub_461980
        emit_error(7, 3786, a1->src_loc);          // non-positive value
        return entity;
    }

    // Validate: must fit in int32
    int64_t val = const_expr_get_value(arg);        // sub_461640
    if (val > INT32_MAX) {
        emit_error(7, 3787, a1->src_loc);           // value too large
        return entity;
    }

    entity->launch_config->local_maxnreg = (int32_t)val;  // offset +36
    return entity;
}
```

### Post-Validation Difference from `__maxnreg__`

In `sub_6BC890`, `__maxnreg__` (stored at launch_config+32) is validated to require `__global__` (error 3715: `"__maxnreg__ is only valid on __global__ functions"`). **`__local_maxnreg__` has no such check in post-validation.** This is intentional: it is designed to work on `__device__` functions as well. The post-validation function only checks the `maxnreg` field (offset +32) for the `__global__` requirement; the `local_maxnreg` field (offset +36) is left unchecked.

### Diagnostics

| Error | Message | Condition |
|-------|---------|-----------|
| 3786 | Non-positive `__local_maxnreg__` value | `const_expr_sign_compare(arg, 0) <= 0` |
| 3787 | `__local_maxnreg__` value too large | Value exceeds int32 range |

---

## `__block_size__` (Kind 0x6C = `'l'`)

### Purpose

`__block_size__` specifies the thread block dimensions (and optionally cluster dimensions) for a kernel at compile time. Unlike `__launch_bounds__`, which provides **hints** for the compiler's register allocator, `__block_size__` declares the **actual block geometry**. This enables the compiler to optimize based on known block dimensions: unrolling loops by the block dimension, computing shared memory bank conflict patterns at compile time, and statically determining the number of warps.

### Apply Handler: `sub_4109E0` (265 lines)

This is the largest of the launch config attribute handlers. It accepts up to **6 arguments**: three block dimensions (x, y, z) and three cluster dimensions (x, y, z).

```c
// sub_4109E0 -- apply_nv_block_size_attr (simplified)
entity_t* apply_nv_block_size_attr(attr_node_t* a1, entity_t* a2, ...) {
    // Allocate launch config struct if needed
    if (!entity->launch_config)
        entity->launch_config = allocate_launch_config();

    launch_config_t* lc = entity->launch_config;

    // Parse block dimensions (arguments 1-3)
    // Each: validate positive, validate fits in int32
    for (int i = 0; i < 3 && arg_exists; i++) {
        if (const_expr_sign_compare(arg, 0) <= 0)
            emit_error(7, 3788, src);   // non-positive
        else {
            int64_t val = const_expr_get_value(arg);
            if (val > INT32_MAX)
                emit_error(7, 3789, src);  // too large
            else
                lc->block_size[i] = (int32_t)val;  // +40, +44, +48
        }
    }

    // Parse optional cluster dimensions (arguments 4-6)
    if (cluster_args_present) {
        // Check for conflict with prior __cluster_dims__
        if (lc->flags & 0x01)
            emit_error(7, 3791, src);  // conflict

        for (int i = 0; i < 3 && arg_exists; i++) {
            // same positive/range validation
            lc->cluster_dim[i] = (int32_t)val;  // +20, +24, +28
        }
    } else if (!(lc->flags & 0x01)) {
        // Default cluster dims to (1,1,1) when no cluster args
        // and no prior __cluster_dims__
        lc->cluster_dim_x = 1;
        lc->cluster_dim_y = 1;
        lc->cluster_dim_z = 1;
    }

    lc->flags |= 0x02;   // mark block_size_set
    return entity;
}
```

### Conflict with `__cluster_dims__`

`__block_size__` and `__cluster_dims__` have a bidirectional conflict. Each handler checks the other's flag:

- `__block_size__` checks `flags & 0x01` (cluster_dims_set) before writing cluster dims: error 3791.
- `__cluster_dims__` checks `flags & 0x02` (block_size_set) before writing cluster dims: error 3791.

However, neither handler returns early on this conflict. Both continue to set their respective flag bits, so after conflict the flags byte can be `0x03` (both bits set). The error diagnostic is emitted but the compilation continues.

### Diagnostics

| Error | Message | Condition |
|-------|---------|-----------|
| 3788 | Non-positive `__block_size__` dimension | `const_expr_sign_compare(arg, 0) <= 0` |
| 3789 | `__block_size__` dimension too large | Value exceeds int32 range |
| 3791 | Conflicting `__cluster_dims__` and `__block_size__` | Both attributes applied to same entity |

---

## Global State and Registration

### Startup Registration (`sub_6B5E50`)

The function `sub_6B5E50` runs during compiler initialization and registers three names as preprocessor macro definitions:

1. **`__nv_register_params__`**: looked up via `sub_734430`; if not found, creates a new macro via `sub_749600` and associates it with a 40-byte token sequence. The token body encodes the magic values 8961 (`0x2301`) as a prefix, followed by attribute argument tokens. If the symbol already exists (the macro was predefined), it appends the token body to the existing definition's expansion via `sub_6AC190`.

2. **`__noinline__`**: registered with the same mechanism. The token body contains the string `"oinline))"` as a suffix (the decompiled code shows `strcpy((char*)(v11+20), "oinline))");`), which reconstructs the full `__attribute__((__noinline__))` expansion.

3. **`_Pragma`**: conditionally registered if `dword_106C0E0` is set. The `_Pragma` macro registration enables MSVC-compatible pragma handling in certain compilation modes.

Additionally, if Clang compatibility mode is active (`dword_126EFA4` set, `qword_126EF90 > 0x2BF1F` = Clang >= 3.0, and specific extension flags are enabled), the function registers ARM SVE attribute macros (`__arm_in`, `__arm_inout`, `__arm_out`, `__arm_preserves`, `__arm_streaming`, `__arm_streaming_compatible`).

### Entity Node Field Summary

```text
entity+177  bit 4 (0x10): __forceinline__
entity+179  bit 4 (0x10): __inline_hint__
entity+179  bit 5 (0x20): __noinline__ (EDG path)
entity+180  bit 7 (0x80): __noinline__ (GNU path)
entity+181  bit 5 (0x20): __forceinline__ relaxation flag
entity+182  [byte]:       execution space (see overview)
entity+183  bit 3 (0x08): __nv_register_params__
entity+183  bit 6 (0x40): __cluster_dims__ intent
entity+256  [pointer]:    launch_config_t* (for __local_maxnreg__, __block_size__)
```

## Function Map

| Address | Size | Identity | Source |
|---------|------|----------|--------|
| `sub_40A310` | 83 lines | `attribute_display_name` | `attribute.c:1307` |
| `sub_40A8A0` | 23 lines | `apply_nv_inline_hint_attr` (contains) | `attribute.c` |
| `sub_40B0A0` | 38 lines | `apply_nv_register_params_attr` | `attribute.c:10537` |
| `sub_40F5F0` | 51 lines | `apply_noinline_attr` (EDG path) | `attribute.c` |
| `sub_40F6F0` | 37 lines | `apply_noinline_attr` (GNU path) | `attribute.c` |
| `sub_40F7B0` | 61 lines | `apply_noinline_scoped_attr` | `attribute.c` |
| `sub_4109E0` | 265 lines | `apply_nv_block_size_attr` | `attribute.c` |
| `sub_411090` | 67 lines | `apply_nv_local_maxnreg_attr` | `attribute.c` |
| `sub_413240` | 585 lines | `apply_one_attribute` (dispatch) | `attribute.c` |
| `sub_6B5E50` | 160 lines | Startup registration | `nv_transforms.c` adjacent |
| `sub_6BC890` | 160 lines | `nv_validate_cuda_attributes` | `nv_transforms.c` |

## Diagnostic Tag Index

| Error | Diagnostic Tag | Attribute |
|-------|---------------|-----------|
| 3659 | `register_params_not_enabled` | `__nv_register_params__` |
| 3661 | `register_params_unsupported_function` | `__nv_register_params__` |
| 3662 | `register_params_ellipsis_function` | `__nv_register_params__` |
| — | `register_params_unsupported_arch` | `__nv_register_params__` |
| 3786 | `local_maxnreg_negative` | `__local_maxnreg__` |
| 3787 | `local_maxnreg_too_large` | `__local_maxnreg__` |
| 3788 | `block_size_must_be_positive` | `__block_size__` |
| 3789 | (block_size dimension overflow) | `__block_size__` |
| 3791 | `conflict_between_cluster_dim_and_block_size` | `__block_size__` / `__cluster_dims__` |
| 1835 | (attribute on wrong target) | `__noinline__` |
| 2470 | (attribute in alignas context) | `__noinline__` |

## Cross-References

- [Attribute System Overview](overview.md) — kind enum, descriptor table, application pipeline
- [Launch Configuration Attributes](launch-config.md) — shared launch_config_t struct, `__launch_bounds__`, `__maxnreg__`, `__cluster_dims__`
- [\_\_global\_\_ Function Constraints](global-function.md) — post-validation checks in `sub_6BC890`
- [Entity Node Layout](../structs/entity-node.md) — entity+177, +179, +180, +182, +183 field definitions
- [Cross-Space Validation](../cuda/cross-space-validation.md) — `__forceinline__` relaxation in cross-space calling
- [Architecture Feature Gating](../cuda/arch-gating.md) — `__nv_register_params__` compute_80 requirement
- [__nv_* Builtin Intrinsic Names](nv-builtin-intrinsics.md) — companion catalogue of `__nv_*` names that are *not* attributes (intrinsics, lambda machinery)
