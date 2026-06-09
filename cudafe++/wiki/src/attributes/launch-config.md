# Launch Configuration Attributes

cudafe++ supports five attributes that control CUDA kernel launch parameters: `__launch_bounds__`, `__cluster_dims__`, `__block_size__`, `__maxnreg__`, and `__local_maxnreg__`. All five store their values into a shared 56-byte **launch configuration struct** pointed to by `entity+256`. The struct is lazily allocated on first use by `sub_5E52F0` and initialized with sentinel values (`-1` for all int32 fields, `0` for the two leading int64 fields, flags cleared). Each attribute handler parses its arguments through a shared constant-expression evaluation pipeline (`sub_461640` for value extraction, `sub_461980` for sign checking), validates positivity and 32-bit range, then writes results into specific offsets of the struct. A post-declaration validation pass (`sub_6BC890` in `nv_transforms.c`) enforces cross-attribute constraints: launch config attributes require `__global__`, cluster dimensions must not exceed `__launch_bounds__`, and `__maxnreg__` is mutually exclusive with `__launch_bounds__`.

## Key Facts

| Property | Value |
|---|---|
| Source files | `attribute.c` (apply handlers), `nv_transforms.c` (post-validation) |
| `__launch_bounds__` handler | `sub_411C80` (98 lines) |
| `__cluster_dims__` handler | `sub_4115F0` (145 lines) |
| `__block_size__` handler | `sub_4109E0` (265 lines) |
| `__maxnreg__` handler | `sub_410F70` (67 lines) |
| `__local_maxnreg__` handler | `sub_411090` (67 lines) |
| Post-validation | `sub_6BC890` (`nv_validate_cuda_attributes`, 160 lines) |
| Struct allocator | `sub_5E52F0` (42 lines) |
| Constant value extractor | `sub_461640` (`const_expr_get_value`, 53 lines) |
| Constant sign checker | `sub_461980` (`const_expr_sign_compare`, 97 lines) |
| Dependent-type check | `sub_7BE9E0` (`is_dependent_type`) |
| Entity field | `entity+256` — pointer to `launch_config_t` (56 bytes, NULL if no launch attrs) |
| Entity extended flags | `entity+183` bit 6 (0x40): cluster_dims intent (set by zero-argument `__cluster_dims__`) |
| Total error codes | 17 distinct diagnostics across all five attributes and post-validation |

## Attribute Kind Codes

Each CUDA attribute carries a kind byte at `attr_node+8`. The five launch config attributes use these values from the `attribute_display_name` (`sub_40A310`) switch table:

| Kind | Hex | ASCII | Attribute | Handler |
|------|------|-------|-----------|---------|
| 92 | 0x5C | `'\'` | `__launch_bounds__` | `sub_411C80` |
| 93 | 0x5D | `']'` | `__maxnreg__` | `sub_410F70` |
| 94 | 0x5E | `'^'` | `__local_maxnreg__` | `sub_411090` |
| 107 | 0x6B | `'k'` | `__cluster_dims__` | `sub_4115F0` |
| 108 | 0x6C | `'l'` | `__block_size__` | `sub_4109E0` |

Kinds 92–94 are part of the original dense block (86–95). Kinds 107 and 108 were added later for cluster/Hopper-era features, occupying gaps in the ASCII range.

## Launch Configuration Struct Layout

The struct is allocated by `sub_5E52F0` and returned with a 16-byte offset from the raw allocation base. All handlers access the struct through the pointer stored at `entity+256`. The allocator initializes all int32 fields to `-1` (sentinel for "not set") and zeroes the two leading int64 fields and the flags byte.

```c
struct launch_config_t {                  // 56 bytes (offsets from entity+256 pointer)
    int64_t  maxThreadsPerBlock;          // +0   from __launch_bounds__ arg 1 (init: 0)
    int64_t  minBlocksPerMultiprocessor;  // +8   from __launch_bounds__ arg 2 (init: 0)
    int32_t  maxBlocksPerCluster;         // +16  from __launch_bounds__ arg 3 (init: -1)
    int32_t  cluster_dim_x;              // +20  from __cluster_dims__ / __block_size__ (init: -1)
    int32_t  cluster_dim_y;              // +24  from __cluster_dims__ / __block_size__ (init: -1)
    int32_t  cluster_dim_z;              // +28  from __cluster_dims__ / __block_size__ (init: -1)
    int32_t  maxnreg;                    // +32  from __maxnreg__ (init: -1)
    int32_t  local_maxnreg;              // +36  from __local_maxnreg__ (init: -1)
    int32_t  block_size_x;              // +40  from __block_size__ (init: -1)
    int32_t  block_size_y;              // +44  from __block_size__ (init: -1)
    int32_t  block_size_z;              // +48  from __block_size__ (init: -1)
    uint8_t  flags;                      // +52  bit 0: cluster_dims_set
                                         //       bit 1: block_size_set
    // +53..+55: padding
};
```

The struct packs integer fields of mixed widths. The first two fields (`maxThreadsPerBlock` and `minBlocksPerMultiprocessor`) are 64-bit to accommodate the full range of CUDA launch bounds values. The cluster dimensions, block sizes, and register counts are 32-bit because individual values cannot exceed hardware limits. The `flags` byte at offset `+52` records which dimension-setting attributes have been applied, enabling mutual exclusion enforcement between `__cluster_dims__` and `__block_size__`.

### Allocator: sub_5E52F0

The allocator performs arena allocation via `sub_6B7D60`, then initializes every field:

```c
// sub_5E52F0 -- allocate_launch_config
launch_config_t* allocate_launch_config() {
    void* raw = arena_alloc(pool_id, launch_config_pool_size + 56);
    char* base = pool_base + raw;

    if (!abi_mode) {              // dword_106BA08 == 0
        ++alloc_counter_prefix;
        base += 8;
        *(int64_t*)(base - 8) = 0;   // 8-byte ABI prefix
    }

    ++alloc_counter_main;

    // Zero the int64 fields
    *(int64_t*)(base + 0)  = 0;       // becomes returned+0:  maxThreadsPerBlock = 0
    *(int64_t*)(base + 8)  = 0;       // padding (base+8..15)
    *(int64_t*)(base + 16) = 0;       // becomes returned+0..7 after offset

    // Initialize all int32 fields to -1 (sentinel = "not set")
    *(int32_t*)(base + 32) = -1;      // returned+16: maxBlocksPerCluster
    *(int32_t*)(base + 36) = -1;      // returned+20: cluster_dim_x
    *(int32_t*)(base + 40) = -1;      // returned+24: cluster_dim_y
    *(int32_t*)(base + 44) = -1;      // returned+28: cluster_dim_z
    *(int32_t*)(base + 48) = -1;      // returned+32: maxnreg
    *(int32_t*)(base + 52) = -1;      // returned+36: local_maxnreg
    *(int32_t*)(base + 56) = -1;      // returned+40: block_size_x
    *(int32_t*)(base + 60) = -1;      // returned+44: block_size_y
    *(int32_t*)(base + 64) = -1;      // returned+48: block_size_z
    base[68] &= 0xFC;                // returned+52: clear flags bits 0 and 1

    // Set internal flags byte combining ABI mode, device mode, marker
    base[8] = (8 * (device_flag & 1)) & 0x7F
            | (2 * (!abi_mode))       & 0x7E
            | 1;

    return (launch_config_t*)(base + 16);   // return with 16-byte offset
}
```

The sentinel value `-1` (0xFFFFFFFF as unsigned, -1 as signed) is semantically meaningful throughout: handlers and the post-validator test `field >= 0` or `field > 0` to determine whether a field has been set. A value of `-1` always fails both tests, so unset fields are correctly treated as absent. The two leading int64 fields use `0` as their sentinel since they store `__launch_bounds__` arguments where zero means "not specified."

## Constant-Expression Evaluation Pipeline

All five attribute handlers share the same two-function pipeline for parsing attribute argument values from EDG's internal 128-bit constant representation.

### sub_461980 — const_expr_sign_compare

Compares a constant expression's value against a 64-bit threshold. Returns `+1` if the expression value is greater, `-1` if less, `0` if equal. The comparison operates on the 128-bit extended-precision value stored at offsets `+152` through `+166` (eight 16-bit words) of the expression node.

```c
// sub_461980 -- const_expr_sign_compare(expr_node, threshold)
// Returns: +1 if expr > threshold, -1 if expr < threshold, 0 if equal
int32_t const_expr_sign_compare(expr_node_t* expr, int64_t threshold) {
    // Decompose threshold into eight 16-bit words with sign extension
    uint16_t thresh_words[8];
    // ... sign-extension propagation through all 8 words ...

    // Navigate to base type, skipping cv-qualifier wrappers (kind == 12)
    type_t* type = expr->type_chain;    // expr+112
    while (type->kind_132 == 12)
        type = type->referenced;        // type+144

    // Determine signedness from base type
    bool is_signed = (type->kind_132 == 2
                      && is_signed_type_table[type->subkind_144]);

    if (is_signed && (expr->word_152 & 0x8000)) {
        // Negative expression value
        if (!(threshold_high & 0x8000))
            return -1;    // negative < non-negative
    } else if (!is_signed) {
        if (threshold_high & 0x8000)
            return 1;     // non-negative > negative threshold
    }

    // Word-by-word comparison from most-significant to least
    // expr+152 (MSW) through expr+166 (LSW) vs threshold words
    for (int i = 0; i < 8; i++) {
        if (expr->words[152 + 2*i] > thresh_words[i]) return 1;
        if (expr->words[152 + 2*i] < thresh_words[i]) return -1;
    }
    return 0;  // equal
}
```

The handlers call `const_expr_sign_compare(expr, 0)` to check positivity:
- `<= 0` means non-positive (used by `__cluster_dims__`, `__block_size__`, `__maxnreg__`, `__local_maxnreg__`)
- `< 0` means strictly negative (used by `__launch_bounds__` arg 3, where zero is allowed)

### sub_461640 — const_expr_get_value

Extracts a `uint64_t` value from a constant expression node's 128-bit representation. Sets an overflow flag if the value does not fit in 64 bits (accounting for sign).

```c
// sub_461640 -- const_expr_get_value(expr_node, *overflow_flag)
// Returns: uint64_t value; *overflow_flag = 1 if truncation occurred
uint64_t const_expr_get_value(expr_node_t* expr, int32_t* overflow) {
    // Navigate to base type
    type_t* type = expr->type_chain;    // expr+112
    while (type->kind_132 == 12)
        type = type->referenced;

    uint16_t sign_word = expr->word_152;    // most-significant of 128-bit value
    bool is_signed = (type->kind_132 == 2
                      && is_signed_type_table[type->subkind_144]);

    int16_t expected_high;
    if (is_signed) {
        *overflow = 0;
        expected_high = -(sign_word >> 15);     // -1 if negative, 0 if positive
    } else {
        *overflow = 0;
        expected_high = 0;
    }

    // Verify that the upper 64 bits match the expected sign-extension pattern
    bool has_overflow = (sign_word != (uint16_t)expected_high);
    if (expr->word_154 != (uint16_t)expected_high) has_overflow = true;
    if (expr->word_156 != (uint16_t)expected_high) has_overflow = true;
    if (expr->word_158 != (uint16_t)expected_high) has_overflow = true;

    // Reconstruct 64-bit value from the lower four 16-bit words
    uint64_t result = ((uint64_t)expr->word_160 << 48)
                    | ((uint64_t)expr->word_162 << 32)
                    | ((uint64_t)expr->word_164 << 16)
                    | ((uint64_t)expr->word_166);

    if (!is_signed) {
        if (has_overflow) { *overflow = 1; }
        return result;
    }
    // Signed: verify sign bit consistency
    if (((uint16_t)expected_high) != (uint16_t)(result >> 63)
        || has_overflow
        || (int16_t)sign_word < 0) {
        *overflow = 1;
    }
    return result;
}
```

The overflow flag is used by all handlers with a consistent check pattern:

```c
int32_t overflow;
uint64_t val = const_expr_get_value(expr, &overflow);
if (overflow || val > 0x7FFFFFFF)
    emit_error(OVERFLOW_ERROR_CODE, src_loc);
else
    launch_config->field = (int32_t)val;
```

### Template-Dependent Argument Bailout

Before evaluating constant expressions, all five handlers walk the attribute argument list checking for template-dependent types via `sub_7BE9E0` (`is_dependent_type`). The walk follows a linked list of argument nodes (head at `attr_node+32`), where each node has:

| Offset | Field | Description |
|--------|-------|-------------|
| `+0` | `next` | Next argument node in list |
| `+10` | `kind` | Argument kind: 3 = type-qualified, 4 = expression, 5 = indirect expression |
| `+32` | `expr` | Expression/type pointer (accessed as `node[4]` in decompiled code) |

If any argument has a dependent type, the handler returns immediately without modifying the entity. This defers attribute processing to template instantiation time, when concrete values are available:

```c
// Common bailout pattern (appears in all 5 handlers)
arg_node_t* walk = *(arg_node_t**)(attr_node + 32);
while (walk) {
    switch (walk->kind_10) {
        case 3:   // type-qualified argument
            if (walk->expr[4]->kind_148 == 12)    // cv-qualifier wrapper
                return entity;                     // dependent -- bail
            break;
        case 4:   // expression argument
            if (is_dependent_type(walk->expr[4]))  // sub_7BE9E0
                return entity;
            if (walk->kind_10 != 5)
                break;
            // fallthrough to case 5
        case 5:   // indirect expression
            if (is_dependent_type(*(walk->expr[4])))
                return entity;
            break;
        default:
            break;
    }
    walk = walk->next;
}
// All args are concrete -- proceed with evaluation
```

## __launch_bounds__ (sub_411C80)

**Syntax:** `__launch_bounds__(maxThreadsPerBlock [, minBlocksPerMultiprocessor [, maxBlocksPerCluster]])`

Accepts 1 to 3 arguments. Registered at kind byte `0x5C` (`'\\'`).

```c
// sub_411C80 -- apply_nv_launch_bounds_attr (attribute.c, 98 lines)
// a1: attribute node, a2: entity node
entity_t* apply_nv_launch_bounds(attr_node_t* attr, entity_t* entity) {

    // ---- Error 3535: launch_bounds on local function ----
    // Note: does NOT return early -- continues to store values
    if (entity->byte_81 & 0x04)
        emit_error_with_name(7, 3535, attr->src_loc, "__launch_bounds__");

    // ---- Parse argument list ----
    arg_list_t* args = attr->arg_list;    // attr+32
    if (!args)
        return entity;

    // ---- Allocate launch config if needed ----
    launch_config_t* lc = entity->launch_config;   // entity+256
    if (!lc) {
        lc = allocate_launch_config();              // sub_5E52F0
        entity->launch_config = lc;
    }

    // ---- Arg 1: maxThreadsPerBlock (required, stored as int64) ----
    // Copied directly from constant expression value -- no sign/overflow check
    lc->maxThreadsPerBlock = args->const_value;     // +0, int64

    // ---- Arg 2: minBlocksPerMultiprocessor (optional, stored as int64) ----
    arg_node_t* arg2_list = *args;                  // first child
    if (!arg2_list)
        return entity;

    expr_node_t* arg2_expr = *arg2_list;            // expression node
    lc->minBlocksPerMultiprocessor = arg2_list[4];  // +8, int64, raw copy

    // ---- Check for arg 3 existence ----
    if (!arg2_expr)
        goto process_arg3;

    // ---- Template-dependent bailout for remaining args ----
    arg_node_t* walk = *(arg_node_t**)(attr + 32);
    if (!walk)
        goto process_arg3;
    // ... dependent type walk (same pattern as documented above) ...
    // If any arg is dependent, return entity unchanged

process_arg3:
    // ---- Arg 3: maxBlocksPerCluster (optional, int32, uses full pipeline) ----
    expr_node_t* expr3 = arg2_expr->const_value;   // 3rd arg expression
    if (!expr3)
        return entity;

    if (const_expr_sign_compare(expr3, 0) < 0) {
        // Error 3705: negative maxBlocksPerCluster
        emit_error(7, 3705, attr->src_loc);
    } else {
        int32_t overflow;
        uint64_t val = const_expr_get_value(expr3, &overflow);
        if (overflow || val > 0x7FFFFFFF) {
            // Error 3706: overflow
            emit_error(7, 3706, attr->src_loc);
        } else if (val != 0) {
            lc->maxBlocksPerCluster = (int32_t)val;   // +16
        }
        // val == 0: not stored, sentinel -1 remains (means "use default")
    }

    return entity;
}
```

### Argument Semantics

| Arg | Field | Offset | Type | Validation | Description |
|-----|-------|--------|------|------------|-------------|
| 1 (required) | `maxThreadsPerBlock` | +0 | int64 | None — raw copy | Maximum threads per block. Guides register allocation in `ptxas`. |
| 2 (optional) | `minBlocksPerMultiprocessor` | +8 | int64 | None — raw copy | Minimum resident blocks per SM. Guides occupancy optimization. |
| 3 (optional) | `maxBlocksPerCluster` | +16 | int32 | `sign_compare < 0` (3705), overflow (3706) | Maximum blocks per cluster (CUDA 11.8+). |

### Critical Implementation Details

**First two args bypass the sign/overflow pipeline.** Arguments 1 and 2 are copied directly from the constant expression node's value field as 64-bit quantities. They do not pass through `const_expr_sign_compare` or `const_expr_get_value`. This means negative or excessively large values for `maxThreadsPerBlock` and `minBlocksPerMultiprocessor` are accepted at parse time — downstream consumers (ptxas) are responsible for rejecting them.

**Third argument uses the strict pipeline.** Only argument 3 (`maxBlocksPerCluster`) passes through both `const_expr_sign_compare` and `const_expr_get_value` with the overflow check. This argument was added later (CUDA 11.8 cluster launch) and uses the newer, stricter validation pattern.

**Zero is acceptable for arg 3.** The sign check uses `const_expr_sign_compare(expr, 0) < 0` (strictly negative), not `<= 0`. A zero value passes the sign check but is not written (`else if (val != 0)` guard), leaving the sentinel `-1` in place. This means zero effectively means "use default."

**Error 3535 does not abort.** The local-function check fires but does NOT return early. Processing continues, arguments are stored, and the launch config struct is populated even after emitting the error. This is consistent with cudafe++'s design of collecting as many diagnostics as possible in a single compilation pass.

## __cluster_dims__ (sub_4115F0)

**Syntax:** `__cluster_dims__(x [, y [, z]])` or `__cluster_dims__()`

Accepts 0 to 3 arguments. Missing dimensions default to 1. Sets flag bit 0 at `+52`. Registered at kind byte `0x6B` (`'k'`).

```c
// sub_4115F0 -- apply_nv_cluster_dims_attr (attribute.c, 145 lines)
entity_t* apply_nv_cluster_dims(attr_node_t* attr, entity_t* entity) {

    arg_list_t* args = attr->arg_list;    // attr+32

    // ---- No-argument form: set intent flag only ----
    if (args->kind_10 == 0) {             // no arguments present
        entity->byte_183 |= 0x40;        // cluster_dims intent flag
        return entity;
    }

    // ---- Extract argument expressions (up to 3) ----
    expr_node_t* expr_x = args->value;
    arg_node_t* child1 = args->first_child;
    expr_node_t* expr_y = child1 ? child1->value : NULL;
    expr_node_t* expr_z = NULL;
    if (child1 && child1->first_child)
        expr_z = child1->first_child->value;

    // ---- Template-dependent bailout ----
    // ... same walk pattern as __launch_bounds__ ...

    // ---- Allocate launch config if needed ----
    launch_config_t* lc = entity->launch_config;
    if (!lc) {
        lc = allocate_launch_config();
        entity->launch_config = lc;
    }

    // ---- Conflict check: __block_size__ already set cluster dims ----
    if (lc->flags & 0x02) {               // bit 1 = block_size_set
        emit_error(7, 3791, attr->src_loc);
        lc = entity->launch_config;       // reload after error emit
    }

    // ---- Set cluster_dims flag ----
    lc->flags |= 0x01;                    // bit 0 = cluster_dims_set

    // ---- Arg 1: cluster_dim_x ----
    if (!expr_x) {
        lc->cluster_dim_x = 1;            // +20, default
    } else if (const_expr_sign_compare(expr_x, 0) <= 0) {
        emit_error_with_name(7, 3685, attr->src_loc, "__cluster_dims__");
        lc = entity->launch_config;       // reload
    } else {
        int32_t overflow;
        uint64_t val = const_expr_get_value(expr_x, &overflow);
        if (overflow || val > 0x7FFFFFFF)
            emit_error(7, 3686, attr->src_loc);
        else
            lc->cluster_dim_x = (int32_t)val;
    }

    // ---- Arg 2: cluster_dim_y (defaults to 1) ----
    if (!expr_y) {
        lc->cluster_dim_y = 1;            // +24
    } else {
        // Same sign_compare/get_value/3685/3686 pattern
        // Stores at lc->cluster_dim_y (+24)
    }

    // ---- Arg 3: cluster_dim_z (defaults to 1) ----
    if (!expr_z) {
        lc->cluster_dim_z = 1;            // +28
    } else {
        // Same pattern, stores at lc->cluster_dim_z (+28)
    }

    return entity;
}
```

### Key Observations

**Zero-argument form.** When `__cluster_dims__()` is called with no arguments, the handler does not allocate the launch config struct. It sets `entity+183 |= 0x40` (the "cluster_dims intent" flag) and returns. This intent flag is checked during post-validation to detect `__cluster_dims__` on non-`__global__` functions (error 3534) even when no dimensions were specified.

**Conflict check with __block_size__.** Before storing dimensions, the handler checks `lc->flags & 0x02` (bit 1 = `block_size_set`). If `__block_size__` was already applied, error 3791 fires. Crucially, the handler does NOT return early after this error — it continues to set the flag and attempt to store values. The reverse conflict (applying `__block_size__` after `__cluster_dims__`) is checked in `sub_4109E0` with the same error code, testing `lc->flags & 0x01`.

**Strict positivity (zero rejected).** All three dimensions use `const_expr_sign_compare(expr, 0) <= 0`, rejecting zero. Error 3685 fires with the attribute name `"__cluster_dims__"` as a format argument. Error 3686 fires for values exceeding `0x7FFFFFFF`.

**Defaults to 1.** Unspecified dimensions default to `1`, not `0`. A cluster dimension of 1 means "no clustering in that dimension" — the neutral value. The default is written explicitly (`lc->cluster_dim_x = 1`), overwriting the `-1` sentinel from allocation.

## __block_size__ (sub_4109E0)

**Syntax:** `__block_size__(bx [, by [, bz [, cx [, cy [, cz]]]]])`

Accepts up to 6 arguments: three block dimensions followed by three optional cluster dimensions. Registered at kind byte `0x6C` (`'l'`). At 265 lines, this is the largest launch config handler.

```c
// sub_4109E0 -- apply_nv_block_size_attr (attribute.c, 265 lines)
entity_t* apply_nv_block_size(attr_node_t* attr, entity_t* entity) {

    // ---- Parse up to 6 argument expressions ----
    arg_list_t* args = attr->arg_list;
    expr_node_t* block_x  = args->value;          // arg 1
    expr_node_t* block_y  = NULL;                  // arg 2
    expr_node_t* block_z  = NULL;                  // arg 3
    expr_node_t* cluster_x = NULL;                 // arg 4
    expr_node_t* cluster_y = NULL;                 // arg 5
    expr_node_t* cluster_z = NULL;                 // arg 6
    // ... linked-list traversal to extract args 2-6 ...

    // ---- Template-dependent bailout ----
    // ... same walk pattern ...

    // ---- Allocate launch config ----
    launch_config_t* lc = entity->launch_config;
    if (!lc) {
        lc = allocate_launch_config();
        entity->launch_config = lc;
    }

    // ---- Block dimensions: args 1-3 ----
    // Each uses: sign_compare <= 0 -> error 3788
    //            get_value overflow or > 0x7FFFFFFF -> error 3789
    //            else store at +40/+44/+48
    //            missing args default to 1

    // block_size_x (+40):
    if (!block_x)
        lc->block_size_x = 1;
    else
        validate_positive_int32(block_x, &lc->block_size_x, 3788, 3789, attr);

    // block_size_y (+44): same pattern, default 1
    // block_size_z (+48): same pattern, default 1

    // ---- Cluster dimensions: args 4-6 (only if arg 4 present) ----
    if (!cluster_x) {
        // No cluster dims from __block_size__
        lc->flags &= ~0x02;           // clear bit 1 temporarily

        if (!(lc->flags & 0x01)) {    // cluster_dims NOT already set
            // Write default cluster dims
            lc->cluster_dim_x = 1;     // +20
            lc->cluster_dim_y = 1;     // +24
            lc->cluster_dim_z = 1;     // +28
        }
        return entity;
    }

    // ---- Conflict check: cluster_dims already set ----
    if (lc->flags & 0x01) {           // bit 0 = cluster_dims_set
        emit_error(7, 3791, attr->src_loc);
        lc = entity->launch_config;
    }

    // ---- Set block_size flag ----
    lc->flags |= 0x02;                // bit 1 = block_size_set

    if (lc->flags & 0x01)             // cluster_dims_set -> conflict, bail
        return entity;

    // ---- Parse cluster dims from args 4-6 ----
    // Uses error 3788 for non-positive, 3789 for overflow
    // (same codes as block dims, with "__block_size__" as attr name)
    // Stores at +20/+24/+28, defaults to 1 if absent

    return entity;
}
```

### Key Observations

**Dual-purpose attribute.** `__block_size__` combines block dimensions and cluster dimensions in a single attribute. Arguments 1-3 specify the thread block shape (stored at `+40/+44/+48`); arguments 4-6 specify the cluster shape (stored at `+20/+24/+28`). This is NVIDIA's older, combined syntax, compared to the newer separate `__cluster_dims__` attribute.

**Shared cluster fields.** Both `__block_size__` and `__cluster_dims__` write to the same offsets (`+20/+24/+28`). The `flags` byte (bit 0 for `cluster_dims`, bit 1 for `block_size`) provides mutual exclusion via error 3791.

**Block size fields are separate from launch_bounds.** The block dimensions from `__block_size__` go to `+40/+44/+48`, distinct from `__launch_bounds__`'s `maxThreadsPerBlock` at `+0`. The `__block_size__` attribute specifies exact dimensions; `__launch_bounds__` specifies an upper bound. Both can coexist on the same function.

**Defaulting behavior when no cluster args.** When only 3 arguments are provided (block dims only), the handler checks whether `__cluster_dims__` was already applied (`flags & 0x01`). If not, it writes default cluster dims of `(1, 1, 1)` to `+20/+24/+28`. If `__cluster_dims__` was already applied, it leaves the existing cluster dim values untouched.

**Error 3788/3789.** These are the `__block_size__`-specific equivalents of `__cluster_dims__`'s 3685/3686. Both use strict positivity (`<= 0`), rejecting zero.

## __maxnreg__ (sub_410F70)

**Syntax:** `__maxnreg__(N)`

Accepts exactly 1 argument. Stores at `launch_config+32`. Registered at kind byte `0x5D` (`']'`).

```c
// sub_410F70 -- apply_nv_maxnreg_attr (attribute.c, 67 lines)
entity_t* apply_nv_maxnreg(attr_node_t* attr, entity_t* entity) {
    arg_list_t* args = attr->arg_list;       // attr+32
    if (!args)
        return entity;

    // ---- Template-dependent bailout ----
    // ... same walk pattern ...

    // ---- Allocate launch config ----
    if (!entity->launch_config)
        entity->launch_config = allocate_launch_config();

    // ---- Parse the single argument ----
    expr_node_t* expr = args->const_value;   // argument expression
    if (!expr)
        return entity;

    if (const_expr_sign_compare(expr, 0) <= 0) {
        emit_error(7, 3717, attr->src_loc);       // non-positive register count
    } else {
        int32_t overflow;
        uint64_t val = const_expr_get_value(expr, &overflow);
        if (overflow || val > 0x7FFFFFFF)
            emit_error(7, 3718, attr->src_loc);    // register count too large
        else
            entity->launch_config->maxnreg = (int32_t)val;   // +32
    }

    return entity;
}
```

The `maxnreg` field defaults to `-1` from the allocator. A value `>= 0` in post-validation unambiguously means the attribute was applied with a valid value (since zero would be caught by the `<= 0` check here, the minimum valid value is 1).

### Post-Validation Conflict

The `__maxnreg__` handler does not check for conflicts with `__launch_bounds__` at application time. The mutual exclusion is enforced in post-validation (`sub_6BC890`), which emits error 3719 when both `maxThreadsPerBlock != 0` and `maxnreg >= 0`. This design allows the apply handlers to be called in any order.

## __local_maxnreg__ (sub_411090)

**Syntax:** `__local_maxnreg__(N)`

Structurally identical to `__maxnreg__`. Stores at `launch_config+36`. Registered at kind byte `0x5E` (`'^'`).

```c
// sub_411090 -- apply_nv_local_maxnreg_attr (attribute.c, 67 lines)
entity_t* apply_nv_local_maxnreg(attr_node_t* attr, entity_t* entity) {
    // ... identical structure to __maxnreg__ ...

    if (const_expr_sign_compare(expr, 0) <= 0) {
        emit_error(7, 3786, attr->src_loc);        // error 3786: non-positive
    } else {
        int32_t overflow;
        uint64_t val = const_expr_get_value(expr, &overflow);
        if (overflow || val > 0x7FFFFFFF)
            emit_error(7, 3787, attr->src_loc);     // error 3787: too large
        else
            entity->launch_config->local_maxnreg = (int32_t)val;   // +36
    }

    return entity;
}
```

The `__local_maxnreg__` attribute limits register usage within a specific device function scope rather than at the kernel level. It uses a separate struct field (`+36` vs `+32`) so both can coexist. The post-validator does NOT check `local_maxnreg` for `__global__`-only enforcement — `__local_maxnreg__` is more permissive than `__maxnreg__` and may appear on `__device__` functions.

## Post-Declaration Validation (sub_6BC890)

After all attributes on a declaration have been applied, `nv_validate_cuda_attributes` (`sub_6BC890`, 160 lines, in `nv_transforms.c`) performs cross-attribute consistency checks. This function is called from the declaration processing pipeline and operates on the completed entity node. Multiple errors can be emitted from a single validation pass — cudafe++ does not short-circuit after the first error.

```c
// sub_6BC890 -- nv_validate_cuda_attributes (nv_transforms.c, 160 lines)
// a1: entity pointer, a2: source location for diagnostics
void nv_validate_cuda_attributes(entity_t* entity, source_loc_t* loc) {

    if (!entity || (entity->byte_177 & 0x10))
        return;      // null or suppressed entity

    // ---- Phase 1: Parameter validation (rvalue refs, error 3702) ----
    // Walks parameter list checking for rvalue reference flag
    // [documented on __global__ page]

    // ---- Phase 2: __nv_register_params__ check (error 3661) ----
    // [documented on __global__ page]

    // ---- Phase 3: Launch config attribute checks ----
    launch_config_t* lc = entity->launch_config;   // entity+256
    uint8_t es = entity->byte_182;                  // execution space

    if (!lc)
        goto check_global_advisory;

    if (es & 0x40)                                  // is __global__
        goto cross_attribute_checks;

    // ==== Error 3534: launch config on non-__global__ ====

    // 3534 for __launch_bounds__
    if (lc->maxThreadsPerBlock || lc->minBlocksPerMultiprocessor) {
        emit_error_with_name(7, 3534, &global_loc, "__launch_bounds__");
        lc = entity->launch_config;                 // reload after emit
    }

    // 3534 for __cluster_dims__ or __block_size__
    if ((entity->byte_183 & 0x40) || lc->cluster_dim_x >= 0) {
        const char* name = (lc->block_size_x > 0) ? "__block_size__"
                                                    : "__cluster_dims__";
        emit_error_with_name(7, 3534, &global_loc, name);
        lc = entity->launch_config;
        if (!lc)
            goto check_global_advisory;
    }

cross_attribute_checks:
    // ==== Error 3707: cluster size exceeds maxBlocksPerCluster ====
    if (lc->cluster_dim_x > 0) {
        if (lc->maxBlocksPerCluster > 0) {
            uint64_t cluster_product = (int64_t)lc->cluster_dim_x
                                     * (int64_t)lc->cluster_dim_y
                                     * (int64_t)lc->cluster_dim_z;
            if ((uint64_t)lc->maxBlocksPerCluster < cluster_product) {
                const char* name = (lc->block_size_x > 0) ? "__block_size__"
                                                           : "__cluster_dims__";
                emit_error_with_name(7, 3707, &global_loc, name);
                lc = entity->launch_config;
                if (!lc)
                    goto check_maxnreg;
            }
        }
    }

    // ==== Error 3719: __launch_bounds__ + __maxnreg__ conflict ====
    if (lc->maxnreg >= 0) {
        if (!(es & 0x40)) {
            // ==== Error 3715: __maxnreg__ on non-__global__ ====
            emit_error_with_name(7, 3715, &global_loc, "__maxnreg__");
            lc = entity->launch_config;
            if (lc)
                goto check_maxnreg_conflict;
            goto check_global_advisory;
        }

check_maxnreg_conflict:
        if (!lc->maxThreadsPerBlock) {
            // No __launch_bounds__ -- maxnreg is fine on its own
            // (but this path is for non-__global__, so it already errored)
            goto check_global_advisory;
        }
        // Both __launch_bounds__ and __maxnreg__ present
        emit_error_with_name(7, 3719, &global_loc,
                             "__launch_bounds__ and __maxnreg__");
    }

check_maxnreg:

check_global_advisory:
    // ==== Warning 3695: __global__ without __launch_bounds__ ====
    if (!(es & 0x40))
        return;                  // not __global__, no advisory needed

    lc = entity->launch_config;
    if (!lc) {
        emit_warning(4, 3695, &kernel_decl_loc);
        return;
    }

    if (!lc->maxThreadsPerBlock && !lc->minBlocksPerMultiprocessor) {
        // Launch config exists but no __launch_bounds__ values set
        // (struct was allocated by __cluster_dims__ or __block_size__)
        emit_warning(4, 3695, &kernel_decl_loc);
    }
}
```

### Validation Logic Detail

**Error 3534 — Launch config on non-__global__.** Tests `entity->byte_182 & 0x40` (the `__global__` bit). If clear, any non-default values in the launch config struct trigger error 3534. The error message uses `%s` with the specific attribute name. Notably, the check for `__cluster_dims__` or `__block_size__` tests `lc->cluster_dim_x >= 0` (which is true when any cluster dim handler has run, since they write non-negative values). It also checks the intent flag (`entity->byte_183 & 0x40`) for the zero-argument `__cluster_dims__()` form.

**Error 3707 — Cluster product exceeds maxBlocksPerCluster.** Computes `cluster_dim_x * cluster_dim_y * cluster_dim_z` using signed 64-bit arithmetic and compares against `maxBlocksPerCluster`. The multiplication uses the actual stored dimension values. The error message names whichever attribute set the cluster dims (`"__block_size__"` if `block_size_x > 0`, otherwise `"__cluster_dims__"`). This is a compile-time consistency check: if the programmer specifies both a cluster shape and a maximum cluster block count, the shape must fit.

**Error 3715 — __maxnreg__ on non-__global__.** Separate from the general 3534 check. While 3534 covers `__launch_bounds__`/`__cluster_dims__`/`__block_size__`, `__maxnreg__` uses its own code because it appears in a different branch of the validation logic.

**Error 3719 — __launch_bounds__ + __maxnreg__ conflict.** These two attributes provide contradictory register allocation hints: `__launch_bounds__` asks the compiler to choose registers based on occupancy targets; `__maxnreg__` overrides with a hard limit. Detected by `lc->maxThreadsPerBlock != 0 && lc->maxnreg >= 0`.

**Warning 3695 — Missing __launch_bounds__ advisory.** Severity 4 (informational). Fires when a `__global__` function has no `__launch_bounds__` annotation. Tests both `lc == NULL` (no launch config at all) and `maxThreadsPerBlock == 0 && minBlocksPerMultiprocessor == 0` (struct exists but was allocated by other attrs). Not an error; can be suppressed.

## Error Catalog

### Apply-Time Errors

| Error | Sev | Attribute | Condition | Sign test | Emit function |
|-------|-----|-----------|-----------|-----------|---------------|
| 3535 | 7 | `__launch_bounds__` | `entity+81 & 0x04` (local function) | — | `sub_4F79D0` |
| 3685 | 7 | `__cluster_dims__` | `sign_compare(expr, 0) <= 0` | `<= 0` (zero rejected) | `sub_4F79D0` |
| 3686 | 7 | `__cluster_dims__` | `overflow \|\| val > 0x7FFFFFFF` | — | `sub_4F8200` |
| 3705 | 7 | `__launch_bounds__` (arg 3) | `sign_compare(expr, 0) < 0` | `< 0` (zero allowed) | `sub_4F8200` |
| 3706 | 7 | `__launch_bounds__` (arg 3) | `overflow \|\| val > 0x7FFFFFFF` | — | `sub_4F8200` |
| 3717 | 7 | `__maxnreg__` | `sign_compare(expr, 0) <= 0` | `<= 0` | `sub_4F8200` |
| 3718 | 7 | `__maxnreg__` | `overflow \|\| val > 0x7FFFFFFF` | — | `sub_4F8200` |
| 3786 | 7 | `__local_maxnreg__` | `sign_compare(expr, 0) <= 0` | `<= 0` | `sub_4F8200` |
| 3787 | 7 | `__local_maxnreg__` | `overflow \|\| val > 0x7FFFFFFF` | — | `sub_4F8200` |
| 3788 | 7 | `__block_size__` | `sign_compare(expr, 0) <= 0` | `<= 0` | `sub_4F79D0` |
| 3789 | 7 | `__block_size__` | `overflow \|\| val > 0x7FFFFFFF` | — | `sub_4F8200` |
| 3791 | 7 | `__cluster_dims__` / `__block_size__` | `flags & opposite_bit` | — | `sub_4F8200` |

### Post-Validation Errors

| Error | Sev | Condition | Emit function |
|-------|-----|-----------|---------------|
| 3534 | 7 | Launch config attrs on non-`__global__` | `sub_4F79D0` |
| 3695 | 4 | `__global__` without `__launch_bounds__` | `sub_4F8200` |
| 3707 | 7 | `maxBlocksPerCluster < cluster_x * cluster_y * cluster_z` | `sub_4F79D0` |
| 3715 | 7 | `maxnreg >= 0` on non-`__global__` | `sub_4F79D0` |
| 3719 | 7 | `maxThreadsPerBlock != 0 && maxnreg >= 0` | `sub_4F79D0` |

### Sign-Test Summary

| Attribute | Non-positive error | Overflow error | Sign test | Zero allowed? |
|-----------|-------------------|----------------|-----------|---------------|
| `__launch_bounds__` arg 1-2 | (none) | (none) | No check | Yes |
| `__launch_bounds__` arg 3 | 3705 | 3706 | `< 0` | Yes (not stored) |
| `__cluster_dims__` | 3685 | 3686 | `<= 0` | No |
| `__block_size__` | 3788 | 3789 | `<= 0` | No |
| `__maxnreg__` | 3717 | 3718 | `<= 0` | No |
| `__local_maxnreg__` | 3786 | 3787 | `<= 0` | No |

## IL Emission Path

Launch-config attributes have a hybrid emission story. Two paths exist:

**Struct-only path** — `__maxnreg__`, `__local_maxnreg__`, `__cluster_dims__`, `__block_size__` write into specific offsets of the 56-byte `launch_config_t` at `entity+256`. The original attribute IL node (kind `0x48`) is consumed at application time and no further IL output is produced by cudafe++. cicc reads the struct fields through the merged entity (via the host stub generator's serialization of `entity+256`) and emits the corresponding PTX directives (`.maxntid`, `.maxnreg`, `.maxclusterrank`, `.reqntid`) directly.

**Struct + re-emit path** — `__launch_bounds__` writes into `launch_config+0/+8/+16` *and* is re-emitted as an IL kind `25` ("function attribute") node into the `.int.c` output via `sub_540560`. The IL output dispatcher in `cp_gen_be.c` shares this branch with `__nv_pure__` (kind `0x6E`):

```c
// IL output dispatcher (cp_gen_be.c, shared with __nv_pure__)
case 0x5C:                 // __launch_bounds__
case 0x6E:                 // __nv_pure__
    node->kind_field = 25; // IL output kind: "function attribute"
    sub_540560(0, 0, node, ctx, ...);  // serialize into .int.c
    break;
```

This dual-path treatment exists because `__launch_bounds__` is the only launch attribute that affects code generation through *both* a PTX directive (consumed via the struct) and an LLVM function attribute on the kernel symbol (consumed via the textual re-emission). The other four launch attributes only need the PTX directive, so the textual re-emission is skipped.

The launch-config struct itself is never an IL node. It is a flat 56-byte arena allocation owned by the entity and freed when the entity is destroyed. There is no `il_entry_kind` value for "launch config" — the struct is invisible to the IL display dispatcher (`sub_5F4930`) and the IL walker (`sub_60E4F0` / `sub_610200`).

## Attribute Interaction Matrix

| | `__launch_bounds__` | `__cluster_dims__` | `__block_size__` | `__maxnreg__` | `__local_maxnreg__` |
|---|---|---|---|---|---|
| `__launch_bounds__` | — | OK | OK | **3719** | OK |
| `__cluster_dims__` | OK | — | **3791** | OK | OK |
| `__block_size__` | OK | **3791** | — | OK | OK |
| `__maxnreg__` | **3719** | OK | OK | — | OK |
| `__local_maxnreg__` | OK | OK | OK | OK | — |

Additional constraints:
- All attributes except `__local_maxnreg__` require `__global__` execution space (error 3534 / 3715)
- `__launch_bounds__` arg 3 must be `>=` cluster product when cluster dims are set (error 3707)
- `__launch_bounds__` is also rejected on local functions at application time (error 3535)

## Entity Node Field Reference

| Offset | Size | Field | Role in Launch Config |
|--------|------|-------|----------------------|
| `+81` | 1 byte | `local_flags` | Bit 2 (0x04): local function. Checked by `sub_411C80` for error 3535. |
| `+177` | 1 byte | `suppress_flags` | Bit 4 (0x10): entity suppressed. Post-validation skips if set. |
| `+182` | 1 byte | `execution_space` | Bit 6 (0x40): `__global__`. Checked by `sub_6BC890` for 3534, 3695, 3715. |
| `+183` | 1 byte | `extended_cuda` | Bit 6 (0x40): cluster_dims intent (set by zero-arg `__cluster_dims__`). |
| `+256` | 8 bytes | `launch_config` | Pointer to `launch_config_t` (56 bytes). NULL if no launch config attrs. |

## Error Emission Functions

| Address | Identity | Signature | Used for |
|---------|----------|-----------|----------|
| `sub_4F79D0` | `emit_error_with_name` | `(severity, code, loc, name_str)` | 3535, 3685, 3534, 3707, 3715, 3719, 3788 |
| `sub_4F8200` | `emit_error_basic` | `(severity, code, loc)` | 3686, 3705, 3706, 3717, 3718, 3786, 3787, 3789, 3791, 3695 |

`sub_4F79D0` passes a format string argument (the attribute name) into the diagnostic message via `%s`. `sub_4F8200` emits a fixed-format message with no string interpolation. Warning 3695 uses severity 4 through `sub_4F8200`; all other diagnostics use severity 7.

## Function Map

| Address | Identity | Lines | Source File |
|---------|----------|-------|-------------|
| `sub_411C80` | `apply_nv_launch_bounds_attr` | 98 | `attribute.c` |
| `sub_4115F0` | `apply_nv_cluster_dims_attr` | 145 | `attribute.c` |
| `sub_4109E0` | `apply_nv_block_size_attr` | 265 | `attribute.c` |
| `sub_410F70` | `apply_nv_maxnreg_attr` | 67 | `attribute.c` |
| `sub_411090` | `apply_nv_local_maxnreg_attr` | 67 | `attribute.c` |
| `sub_6BC890` | `nv_validate_cuda_attributes` | 160 | `nv_transforms.c` |
| `sub_5E52F0` | `allocate_launch_config` | 42 | `il.c` (IL allocation) |
| `sub_461640` | `const_expr_get_value` | 53 | `const_expr.c` |
| `sub_461980` | `const_expr_sign_compare` | 97 | `const_expr.c` |
| `sub_7BE9E0` | `is_dependent_type` | 15 | `template.c` |
| `sub_4F79D0` | `emit_error_with_name` | — | `error.c` |
| `sub_4F8200` | `emit_error_basic` | — | `error.c` |

## Global Variables

| Address | Name | Purpose |
|---------|------|---------|
| `qword_126EDE8` | `global_source_loc` | Default source location used in post-validation error emission |
| `qword_126DD38` | `kernel_decl_loc` | Source location for kernel declaration (used in 3695 advisory) |
| `dword_126EC90` | `il_pool_id` | Arena allocator pool ID for launch config allocation |
| `dword_126F694` | `launch_config_size` | Size parameter for arena allocator |
| `dword_126F690` | `pool_base` | Base pointer of the IL arena pool |
| `dword_106BA08` | `abi_mode` | ABI compatibility flag; when 0, allocator adds 8-byte prefix |
| `dword_126E5FC` | `device_flag` | Device compilation mode; bit 0 affects launch config flags byte |
| `byte_E6D1B0` | `is_signed_type_table` | Lookup table indexed by type subkind; true if type is signed integer |

## Cross-References

- [Attribute System Overview](overview.md) — dispatch table, attribute node structure, kind enum
- [__global__ Function Constraints](global-function.md) — the `__global__` attribute that launch config attributes require
- [__grid_constant__](grid-constant.md) — parameter attribute that interacts with kernel parameter checks
- [Minor Attributes](minor-attributes.md) — `__nv_register_params__`, `__noinline__`, `__forceinline__`
- [Entity Node Layout](../structs/entity-node.md) — full byte map of entity node with `+256` pointer
- [Execution Spaces](../cuda/execution-spaces.md) — `byte_182` bitfield layout and `__global__` predicate
- [Diagnostics Overview](../diagnostics/overview.md) — error emission functions, severity levels
