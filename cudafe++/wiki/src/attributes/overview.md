# Attribute System Overview

cudafe++ processes CUDA attributes through NVIDIA's customization of the EDG 6.6 attribute subsystem. EDG provides a general-purpose attribute infrastructure in `attribute.c` (approximately 11,500 lines of source, spanning addresses `0x409350`--`0x418F80` in the binary) that handles C++11 `[[...]]` attributes, GNU `__attribute__((...))`, MSVC `__declspec`, and `alignas`. NVIDIA extends this infrastructure by injecting 14 CUDA-specific attribute kinds into EDG's attribute kind enumeration, registering CUDA-specific handler callbacks, and adding a post-declaration validation pass that enforces cross-attribute consistency rules (e.g., `__launch_bounds__` requires `__global__`).

The attribute system operates in four phases: **scanning** (lexer recognizes attribute syntax and builds attribute node lists), **lookup** (maps attribute names to descriptors via a hash table), **application** (dispatches to per-attribute handler functions that modify entity nodes), and **validation** (post-declaration consistency checks). CUDA attributes participate in all four phases, using the same node structures and dispatch mechanisms as standard C++/GNU attributes.

## CUDA Attribute Kind Enum

Every attribute node carries a **kind byte** at offset `+8`. For standard C++/GNU attributes, EDG assigns kinds from its built-in descriptor table (`byte_82C0E0` in the `.rodata` segment). For CUDA attributes, NVIDIA reserves a block of kind values in the ASCII printable range. The function `attribute_display_name` (`sub_40A310`, from `attribute.c:1307`) contains the authoritative switch table that maps kind values to human-readable names:

| Kind | Hex | ASCII | Display Name | Category | Handler |
|------|------|-------|--------------|----------|---------|
| 86 | 0x56 | `'V'` | `__host__` | Execution space | `sub_4108E0` |
| 87 | 0x57 | `'W'` | `__device__` | Execution space | `sub_40EB80` |
| 88 | 0x58 | `'X'` | `__global__` | Execution space | `sub_40E1F0` / `sub_40E7F0` |
| 89 | 0x59 | `'Y'` | `__tile_global__` | Execution space | (internal) |
| 90 | 0x5A | `'Z'` | `__shared__` | Memory space | `sub_40E0D0` (shared path) |
| 91 | 0x5B | `'['` | `__constant__` | Memory space | `sub_40E0D0` (constant path) |
| 92 | 0x5C | `'\'` | `__launch_bounds__` | Launch config | `sub_411C80` |
| 93 | 0x5D | `']'` | `__maxnreg__` | Launch config | `sub_410F70` |
| 94 | 0x5E | `'^'` | `__local_maxnreg__` | Launch config | `sub_411090` |
| 95 | 0x5F | `'_'` | `__tile_builtin__` | Internal | (internal) |
| 102 | 0x66 | `'f'` | `__managed__` | Memory space | `sub_40E0D0` (managed path) |
| 107 | 0x6B | `'k'` | `__cluster_dims__` | Launch config | `sub_4115F0` |
| 108 | 0x6C | `'l'` | `__block_size__` | Launch config | `sub_4109E0` |
| 110 | 0x6E | `'n'` | `__nv_pure__` | Optimization | (internal) |

The kind values are not contiguous. Kinds 86--95 form a dense block for the original CUDA attributes. Kinds 102, 107, 108, and 110 were added later (managed memory in CUDA 6.0, cluster dimensions in CUDA 11.8, block size and nv_pure more recently), occupying gaps in the ASCII range.

### attribute_display_name (`sub_40A310`)

This function serves dual duty: it formats the display name for diagnostic messages, and its switch table is the canonical enumeration of all CUDA attribute kinds. The logic:

```c
// sub_40A310 -- attribute_display_name (attribute.c:1307)
// a1: pointer to attribute node
const char* attribute_display_name(attr_node_t* a1) {
    const char* name = a1->name;           // +16
    const char* ns   = a1->namespace_str;  // +24

    // If scoped (namespace::name), format "namespace::name"
    if (ns) {
        size_t ns_len = strlen(ns);
        assert(ns_len + strlen(name) + 3 <= 204);  // buffer byte_E7FB80
        sprintf(byte_E7FB80, "%s::%s", ns, name);
        name = intern_string(byte_E7FB80);  // sub_5E0700
    }

    // Override with CUDA display name based on kind byte
    switch (a1->kind) {  // byte at +8
        case 'V': return "__host__";
        case 'W': return "__device__";
        case 'X': return "__global__";
        case 'Y': return "__tile_global__";
        case 'Z': return "__shared__";
        case '[': return "__constant__";
        case '\\': return "__launch_bounds__";
        case ']': return "__maxnreg__";
        case '^': return "__local_maxnreg__";
        case '_': return "__tile_builtin__";
        case 'f': return "__managed__";
        case 'k': return "__cluster_dims__";
        case 'l': return "__block_size__";
        case 'n': return "__nv_pure__";
        default:  return name ? name : "";
    }
}
```

The 204-byte static buffer `byte_E7FB80` is shared across calls (not thread-safe, but cudafe++ is single-threaded per translation unit). The `intern_string` call (`sub_5E0700`) ensures the formatted `"namespace::name"` string is deduplicated into EDG's permanent string pool.

## Attribute Node Structure

Every attribute is represented by a 72-byte IL node (entry kind 0x48 = `attribute`). The node layout:

```
struct attr_node_t {               // 72 bytes, IL entry kind 0x48
    attr_node_t*  next;            // +0   next attribute in list
    uint8_t       kind;            // +8   attribute kind byte (CUDA: 'V'..'n')
    uint8_t       source_mode;     // +9   1=C++11, 2=GNU, 3=MSVC, 4=alignas, 5=clang
    uint8_t       target_kind;     // +10  what entity type this targets
    uint8_t       flags;           // +11  bit 0=applies_to_params
                                   //      bit 1=skip_arg_check
                                   //      bit 4=scoped attribute
                                   //      bit 7=unknown/unrecognized
    uint32_t      _pad;            // +12  (alignment)
    const char*   name;            // +16  attribute name string
    const char*   namespace_str;   // +24  namespace (NULL for unscoped)
    arg_node_t*   arguments;       // +32  argument list head
    void*         source_pos;      // +40  source position info
    void*         decl_context;    // +48  declaration context / scope
    void*         src_loc_1;       // +56  source location
    void*         src_loc_2;       // +64  secondary source location
};
```

For CUDA attributes, the `kind` byte at offset `+8` is the discriminator. When `get_attr_descr_for_attribute` (`sub_40FDB0`) resolves an attribute name, it writes the corresponding kind value from the descriptor table (`byte_82C0E0`) into this field. All subsequent dispatch operates on this byte alone.

The `source_mode` byte at `+9` indicates the syntactic form the user wrote. CUDA attributes like `__host__` are parsed as GNU-style attributes (`source_mode = 2`), because cudafe++ defines them via `__attribute__((...))` internally.

## Attribute Descriptor Table and Name Lookup

### Master Descriptor Table (`off_D46820`)

The attribute descriptor table is a static array in `.rodata` at `off_D46820`, extending to `unk_D47A60`. Each entry is 32 bytes and encodes:

- Attribute name string
- Kind byte (written to `attr_node_t.kind` on match)
- Handler function pointer (the `apply_*` callback)
- Mode/version condition string (e.g., `'g'` for GCC-only, `'l'` for Clang-only)
- Target applicability mask

### Initialization: `init_attr_name_map` (`sub_418F80`)

At startup, `init_attr_name_map` iterates the descriptor table, validates each name is at most 100 characters, and inserts it into the hash table `qword_E7FB60` (created via `sub_7425C0`). This hash table enables O(1) lookup of attribute names during parsing.

```c
// sub_418F80 -- init_attr_name_map (attribute.c:1524)
void init_attr_name_map(void) {
    attr_name_map = create_hash_table();  // qword_E7FB60
    for (attr_descr* d = off_D46820; d < unk_D47A60; d++) {
        assert(strlen(d->name) <= 100);
        insert_into_hash_table(attr_name_map, d->name, d);
    }
    // Also initializes dword_E7F078 and processes config if dword_106BF18 set
}
```

A companion function `init_attr_token_map` (`sub_419070`) creates a second hash table `qword_E7F038` that maps attribute tokens to their descriptors, used during lexer-level attribute recognition.

### Name Normalization: `sub_40A250`

Before looking up an attribute name, EDG strips `__` prefixes and suffixes. The function at `sub_40A250` checks whether the name starts with `"__"` and ends with `"__"`, strips them, and looks up the bare name in `qword_E7FB60`. This means `__host__`, `__attribute__((host))`, and `host` all resolve to the same descriptor. The stripping respects the current language standard (`dword_126EFB4`) and C++ version (`dword_126EF68`).

### Central Dispatch: `get_attr_descr_for_attribute` (`sub_40FDB0`)

This 227-line function is the central attribute resolution path. Given an attribute node with a name, it:

1. Looks up the name in the hash table
2. Checks mode compatibility (GCC mode via `dword_126EFA8`, Clang mode via `dword_126EFA4`, MSVC mode via `dword_106BF68`/`dword_106BF58`)
3. Checks namespace match (`"gnu"`, `"__gnu__"`, `"clang"`) via `cond_matches_attr_mode` (`sub_40C4C0`)
4. Evaluates version-conditional availability via `in_attr_cond_range` (`sub_40D620`)
5. Writes the kind byte from the matched descriptor into `attr_node_t.kind`
6. Returns the descriptor entry (which carries the handler function pointer)

The mode condition strings use a compact encoding: `'g'`=GCC, `'l'`=Clang, `'s'`=Sun, `'c'`=C++, `'m'`=MSVC; `'x'`=extension, `'+'`=positive match, `'!'`=boundary marker.

## Attribute Application Pipeline

### Phase 1: Scanning

The lexer recognizes attribute syntax and calls into the scanning functions:

| Function | Address | Role |
|----------|---------|------|
| `scan_std_attribute_group` | `sub_412650` | Parses `[[...]]` C++11 and `__attribute__((...))` GNU attributes |
| `scan_gnu_attribute_groups` | `sub_412F20` | Handles `__attribute__((...))` specifically |
| `scan_attributes_list` | `sub_4124A0` | Iterates token stream building attribute node lists |
| `parse_attribute_argument_clause` | `sub_40C8B0` | Parses attribute argument expressions |
| `get_balanced_token` | `sub_40C6C0` | Handles balanced parentheses/brackets in arguments |

Scanning produces a linked list of `attr_node_t` nodes. At this stage, the `kind` byte is unset; only the `name` and `namespace_str` fields are populated.

### Phase 2: Lookup and Kind Assignment

When the parser reaches a declaration, `get_attr_descr_for_attribute` resolves each attribute name to a descriptor and writes the kind byte. For CUDA attributes, this assigns values in the `'V'`--`'n'` range.

### Phase 3: Application -- `apply_one_attribute` (`sub_413240`)

The central dispatcher is a 585-line function containing a switch on the kind byte. For each CUDA kind, it calls the corresponding handler:

```c
// sub_413240 -- apply_one_attribute (attribute.c, main dispatch)
// 585 lines, giant switch on attribute kind
void apply_one_attribute(attr_node_t* attr, entity_t* entity, int target_kind) {
    switch (attr->kind) {
        case 'V':  apply_nv_host_attr(attr, entity, target_kind);     break;
        case 'W':  apply_nv_device_attr(attr, entity, target_kind);   break;
        case 'X':  apply_nv_global_attr(attr, entity, target_kind);   break;
        case 'Z':  apply_nv_shared_attr(attr, entity, target_kind);   break;
        case '[':  apply_nv_constant_attr(attr, entity, target_kind); break;
        case '\\': apply_nv_launch_bounds(attr, entity, target_kind); break;
        case ']':  apply_nv_maxnreg_attr(attr, entity, target_kind);  break;
        case '^':  apply_nv_local_maxnreg(attr, entity, target_kind); break;
        case 'f':  apply_nv_managed_attr(attr, entity, target_kind);  break;
        case 'k':  apply_nv_cluster_dims(attr, entity, target_kind);  break;
        case 'l':  apply_nv_block_size(attr, entity, target_kind);    break;
        // ... standard attributes handled similarly ...
    }
}
```

The outer iteration is `apply_attributes_to_entity` (`sub_413ED0`, 492 lines), which walks the attribute list, calls `apply_one_attribute` for each, and handles deferred attributes, attribute merging, and ordering constraints.

### Phase 4: Post-Declaration Validation -- `sub_6BC890`

After all attributes on a declaration are applied, `sub_6BC890` (`nv_validate_cuda_attributes`, from `nv_transforms.c`) performs cross-attribute consistency checking. This function validates that combinations of CUDA attributes are legal:

```c
// sub_6BC890 -- nv_validate_cuda_attributes (nv_transforms.c)
// a1: entity (function), a2: diagnostic location
void nv_validate_cuda_attributes(entity_t* fn, source_loc_t* loc) {
    if (!fn || (fn->byte_177 & 0x10))  // skip if null or already validated
        return;

    uint8_t exec_space = fn->byte_182;  // CUDA execution space bits
    launch_config_t* lc = fn->launch_config;  // entity+256

    // Check 1: parameters with rvalue-reference in __global__ functions
    // Walks parameter list, emits error 3702 for ref-qualified params

    // Check 2: __nv_register_params__ on __host__-only or __global__
    if (fn->byte_183 & 0x08) {
        if (exec_space & 0x40)       // __global__
            emit_error(3661, "__global__");
        else if ((exec_space & 0x30) == 0x20)  // __host__ only (no __device__)
            emit_error(3661, "__host__");
    }

    // Check 3: __launch_bounds__ without __global__
    if (lc && !(exec_space & 0x40)) {
        if (lc->maxThreadsPerBlock || lc->minBlocksPerMultiprocessor)
            emit_error(3534, "__launch_bounds__");
    }

    // Check 4: __cluster_dims__ / __block_size__ without __global__
    if (lc && (fn->byte_183 & 0x40 || lc->cluster_dim_x > 0)) {
        const char* name = (lc->block_size_x > 0) ? "__block_size__" : "__cluster_dims__";
        emit_error(3534, name);
    }

    // Check 5: maxBlocksPerClusterSize exceeds cluster product
    if (lc && lc->cluster_dim_x > 0 && lc->maxBlocksPerClusterSize > 0) {
        if (lc->maxBlocksPerClusterSize <
            lc->cluster_dim_x * lc->cluster_dim_y * lc->cluster_dim_z) {
            emit_error(3707, ...);
        }
    }

    // Check 6: __maxnreg__ without __global__
    if (lc && lc->maxnreg >= 0 && !(exec_space & 0x40))
        emit_error(3715, "__maxnreg__");

    // Check 7: __launch_bounds__ + __maxnreg__ conflict
    if (lc && lc->maxThreadsPerBlock && lc->maxnreg >= 0)
        emit_error(3719, "__launch_bounds__ and __maxnreg__");

    // Check 8: __global__ without __launch_bounds__
    if ((exec_space & 0x40) && (!lc || (!lc->maxThreadsPerBlock && !lc->minBlocksPerMultiprocessor)))
        emit_warning(3695);  // "no __launch_bounds__ specified for __global__ function"
}
```

### Error Codes in Validation

| Error | Severity | Message |
|-------|----------|---------|
| 3534 | 7 (error) | `"%s" attribute is not allowed on a non-__global__ function` |
| 3661 | 7 (error) | `__nv_register_params__ is not allowed on a %s function` |
| 3695 | 4 (warning) | `no __launch_bounds__ specified for __global__ function` |
| 3702 | 7 (error) | Parameter with rvalue reference in `__global__` function |
| 3707 | 7 (error) | `total number of blocks in cluster computed from %s exceeds __launch_bounds__ specified limit` |
| 3715 | 7 (error) | `__maxnreg__ is not allowed on a non-__global__ function` |
| 3719 | 7 (error) | `__launch_bounds__ and __maxnreg__ may not be used on the same declaration` |

## Per-Attribute Handler Function Table

Each CUDA attribute has a dedicated `apply_*` function registered in the descriptor table. These functions modify entity node fields (execution space bits, memory space bits, launch configuration) and emit diagnostics for invalid usage.

| Attribute | Handler | Address | Lines | Entity Fields Modified |
|-----------|---------|---------|-------|----------------------|
| `__host__` | `apply_nv_host_attr` | `sub_4108E0` | 31 | `entity+182 \|= 0x15` |
| `__device__` | `apply_nv_device_attr` | `sub_40EB80` | 100 | Functions: `entity+182 \|= 0x23`; Variables: `entity+148 \|= 0x01` |
| `__global__` | `apply_nv_global_attr` | `sub_40E1F0` | 89 | `entity+182 \|= 0x61` |
| `__global__` (variant 2) | `apply_nv_global_attr` | `sub_40E7F0` | 86 | Same as above (alternate entry point) |
| `__shared__` | (via device attr path) | -- | -- | `entity+148 \|= 0x02` |
| `__constant__` | (via device attr path) | -- | -- | `entity+148 \|= 0x04` |
| `__managed__` | `apply_nv_managed_attr` | `sub_40E0D0` | 47 | `entity+148 \|= 0x01`, `entity+149 \|= 0x01` |
| `__launch_bounds__` | `apply_nv_launch_bounds_attr` | `sub_411C80` | 98 | `entity+256` -> launch config `+0`, `+8`, `+16` |
| `__maxnreg__` | `apply_nv_maxnreg_attr` | `sub_410F70` | 67 | `entity+256` -> launch config `+32` |
| `__local_maxnreg__` | `apply_nv_local_maxnreg_attr` | `sub_411090` | 67 | `entity+256` -> launch config `+36` |
| `__cluster_dims__` | `apply_nv_cluster_dims_attr` | `sub_4115F0` | 145 | `entity+256` -> launch config `+20`, `+24`, `+28` |
| `__block_size__` | `apply_nv_block_size_attr` | `sub_4109E0` | 265 | `entity+256` -> launch config `+40`..`+52` |
| `__nv_register_params__` | `apply_nv_register_params_attr` | `sub_40B0A0` | 38 | `entity+183 \|= 0x08` |

## Attribute Registration (`sub_6B5E50`)

The function `sub_6B5E50` (160 lines, in the `nv_transforms.c` / `mem_manage.c` area) registers NVIDIA-specific pseudo-attributes into EDG's keyword and macro systems at startup. It operates after EDG's standard keyword initialization but before parsing begins.

The registration creates macro-like definitions that the lexer expands before attribute processing. The function:

1. **Allocates attribute definition nodes** via `sub_6BA0D0` (EDG's node allocator)
2. **Looks up existing definitions** via `sub_734430` (hash table search) -- if a definition already exists, it chains the new handler onto it via `sub_6AC190`
3. **Creates new keyword entries** via `sub_749600` if no prior definition exists
4. **Registers `__nv_register_params__`** as a 40-byte attribute definition node (kind marker 8961) with chain linkage
5. **Registers `__noinline__`** as a 30-byte attribute definition node (kind marker 6401), including the `"oinline))"` suffix for `__attribute__((__noinline__))` expansion
6. **Conditionally registers ARM SME attributes** (`__arm_in`, `__arm_inout`, `__arm_out`, `__arm_preserves`, `__arm_streaming`, `__arm_streaming_compatible`) via `sub_6ACCB0` when Clang version >= 180000 and ARM target flags are set
7. **Registers `_Pragma`** as an operator-like keyword for `_Pragma("...")` processing

If any registration fails (the existing entry cannot be extended), it emits internal error 1338 with the attribute name and calls `sub_6B6280` (fatal error handler).

## Entity Node: CUDA Attribute Fields

CUDA attributes modify specific byte fields in entity nodes. The key fields for a reimplementation:

### Execution Space (`entity+182`)

```
Bit 0 (0x01): __device__           set by apply_nv_device_attr
Bit 2 (0x04): __host__             set by apply_nv_host_attr
Bit 4 (0x10): (reserved)
Bit 5 (0x20): __host__ explicit    set by apply_nv_host_attr
Bit 6 (0x40): __global__           set by apply_nv_global_attr
Bit 7 (0x80): __host__ __device__  set when both specified
```

Handlers use OR-masks: `__host__` sets `0x15` (bits 0+2+4), `__device__` sets `0x23` (bits 0+1+5), `__global__` sets `0x61` (bits 0+5+6). The overlap at bit 0 means all execution-space-annotated functions have bit 0 set, which serves as a quick "has CUDA annotation" predicate.

### Memory Space (`entity+148`)

```
Bit 0 (0x01): __device__           device memory
Bit 1 (0x02): __shared__           shared memory
Bit 2 (0x04): __constant__         constant memory
```

### Extended Memory Space (`entity+149`)

```
Bit 0 (0x01): __managed__          managed (unified) memory
```

### Launch Configuration (`entity+256`)

A pointer to a separately allocated `launch_config_t` structure (created by `sub_5E52F0`):

```
struct launch_config_t {
    uint64_t  maxThreadsPerBlock;          // +0   from __launch_bounds__(N, ...)
    uint64_t  minBlocksPerMultiprocessor;  // +8   from __launch_bounds__(N, M, ...)
    int32_t   maxBlocksPerClusterSize;     // +16  from __launch_bounds__(N, M, K)
    int32_t   cluster_dim_x;              // +20  from __cluster_dims__(X, ...)
    int32_t   cluster_dim_y;              // +24  from __cluster_dims__(X, Y, ...)
    int32_t   cluster_dim_z;              // +28  from __cluster_dims__(X, Y, Z)
    int32_t   maxnreg;                    // +32  from __maxnreg__(N)
    int32_t   local_maxnreg;              // +36  from __local_maxnreg__(N)
    int32_t   block_size_x;              // +40  from __block_size__(X, ...)
    int32_t   block_size_y;              // +44  from __block_size__(X, Y, ...)
    int32_t   block_size_z;              // +48  from __block_size__(X, Y, Z, ...)
    uint8_t   flags;                      // +52  bit 0=cluster_dims_set
                                          //      bit 1=block_size_set
};
```

This structure is allocated lazily -- only created when a launch configuration attribute is first applied to a function. The allocation function `sub_5E52F0` returns a zero-initialized structure with `maxnreg = -1` and `local_maxnreg = -1` (sentinel for "unset").

## Attribute Processing Global State

| Global | Address | Purpose |
|--------|---------|---------|
| `qword_E7FB60` | `0xE7FB60` | Attribute name hash table (created by `init_attr_name_map`) |
| `qword_E7F038` | `0xE7F038` | Attribute token hash table (created by `init_attr_token_map`) |
| `byte_E7FB80` | `0xE7FB80` | 204-byte static buffer for formatted attribute display names |
| `off_D46820` | `0xD46820` | Master attribute descriptor table (32 bytes per entry, extends to `0xD47A60`) |
| `qword_E7F070` | `0xE7F070` | Visibility stack (for `__attribute__((visibility(...)))` nesting) |
| `qword_E7F048` | `0xE7F048` | Alias/ifunc free list head |
| `qword_E7F058`/`E7F050` | `0xE7F058`/`0xE7F050` | Alias chain list head/tail |
| `dword_E7F080` | `0xE7F080` | Attribute processing flags |
| `dword_E7F078` | `0xE7F078` | Extended attribute config flag |

The function `reset_attribute_processing_state` (`sub_4190B0`) zeroes all of these at the start of each translation unit.

## Function Map

| Address | Identity | Source | Confidence |
|---------|----------|--------|------------|
| `sub_40A250` | `strip_double_underscores_and_lookup` | `attribute.c` | HIGH |
| `sub_40A310` | `attribute_display_name` | `attribute.c:1307` | HIGH |
| `sub_40C4C0` | `cond_matches_attr_mode` | `attribute.c` | HIGH |
| `sub_40C6C0` | `get_balanced_token` | `attribute.c` | HIGH |
| `sub_40C8B0` | `parse_attribute_argument_clause` | `attribute.c` | HIGH |
| `sub_40D620` | `in_attr_cond_range` | `attribute.c` | HIGH |
| `sub_40E0D0` | `apply_nv_managed_attr` | `attribute.c:10523` | HIGH |
| `sub_40E1F0` | `apply_nv_global_attr` (variant 1) | `attribute.c` | HIGH |
| `sub_40E7F0` | `apply_nv_global_attr` (variant 2) | `attribute.c` | HIGH |
| `sub_40EB80` | `apply_nv_device_attr` | `attribute.c` | HIGH |
| `sub_40FDB0` | `get_attr_descr_for_attribute` | `attribute.c:1902` | HIGH |
| `sub_4108E0` | `apply_nv_host_attr` | `attribute.c` | HIGH |
| `sub_4109E0` | `apply_nv_block_size_attr` | `attribute.c` | HIGH |
| `sub_410F70` | `apply_nv_maxnreg_attr` | `attribute.c` | HIGH |
| `sub_411090` | `apply_nv_local_maxnreg_attr` | `attribute.c` | HIGH |
| `sub_4115F0` | `apply_nv_cluster_dims_attr` | `attribute.c` | HIGH |
| `sub_411C80` | `apply_nv_launch_bounds_attr` | `attribute.c` | HIGH |
| `sub_412650` | `scan_std_attribute_group` | `attribute.c:2914` | HIGH |
| `sub_413240` | `apply_one_attribute` | `attribute.c` | HIGH |
| `sub_413ED0` | `apply_attributes_to_entity` | `attribute.c` | HIGH |
| `sub_418F80` | `init_attr_name_map` | `attribute.c:1524` | HIGH |
| `sub_419070` | `init_attr_token_map` | `attribute.c` | HIGH |
| `sub_4190B0` | `reset_attribute_processing_state` | `attribute.c` | HIGH |
| `sub_6B5E50` | `process_nv_register_params` / attribute registration | `nv_transforms.c` | HIGH |
| `sub_6BC890` | `nv_validate_cuda_attributes` | `nv_transforms.c` | VERY HIGH |

## Cross-References

- [__global__ Function Constraints](global-function.md) -- detailed validation rules for `__global__`
- [Launch Configuration Attributes](launch-config.md) -- `__launch_bounds__`, `__cluster_dims__`, `__block_size__`
- [__grid_constant__](grid-constant.md) -- grid-constant parameter attribute
- [__managed__ Variables](managed-variables.md) -- managed memory attribute
- [Minor CUDA Attributes](minor-attributes.md) -- `__noinline__`, `__forceinline__`, `__nv_register_params__`, `__nv_pure__`
- [__nv_* Builtin Intrinsic Names](nv-builtin-intrinsics.md) -- 110 `__nv_*` identifiers that look like attributes but are intrinsics / lambda machinery
- [Entity Node Layout](../structs/entity-node.md) -- full entity structure with CUDA field offsets
- [CUDA Execution Spaces](../cuda/execution-spaces.md) -- how execution space bits drive code generation
- [CUDA Memory Spaces](../cuda/memory-spaces.md) -- memory space bitfield semantics
