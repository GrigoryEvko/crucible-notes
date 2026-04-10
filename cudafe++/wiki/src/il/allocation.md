# IL Allocation

Every IL node in cudafe++ is allocated through a region-based bump allocator implemented in `il_alloc.c` (EDG 6.6 source at `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/il_alloc.c`). The allocator manages 70+ distinct IL entry types across two memory region categories -- file-scope (persistent for the entire translation unit) and per-function-scope (transient, freed after each function body is processed). Free-lists recycle high-churn node types to reduce region pressure. The allocation subsystem occupies address range `0x5E0600`-`0x5EAF00` in the binary, roughly 43KB of compiled code covering 100+ functions.

## Key Facts

| Property | Value |
|---|---|
| Source file | `il_alloc.c` (EDG 6.6) |
| Address range | `0x5E0600`-`0x5EAF00` |
| Core allocator | `sub_6B7D60` (`region_alloc(region_id, size)`) |
| File-scope allocator | `sub_5E03D0` (`alloc_in_file_scope_region`) |
| Dual-region allocator | `sub_5E02E0` (`alloc_in_region`) |
| Scratch-region allocator | `sub_5E0460` (`alloc_in_scratch_region`) |
| Stats dump | `sub_5E99D0` (`dump_il_table_stats`), 340 lines |
| Init function | `sub_5EAD80` (`init_il_alloc`) |
| Reset watermarks | `sub_5EAEC0` (`reset_region_offsets`) |
| Clear free-lists | `sub_5EAF00` (`clear_free_lists`) |
| Node types tracked | 70+ (each with per-type counter) |
| Free-list types | 6 (template_arg, constant_list, expr_node, constant, param_type, source_seq_entry) |

## Region-Based Bump Allocator

The core allocation primitive is `sub_6B7D60` (`region_alloc`), a bump allocator that takes a region ID and requested size, and returns a pointer to the allocated block within the region's memory. The caller then writes prefix fields and returns a pointer past the prefix to the node body.

### region_alloc Pseudocode

```c
// sub_6B7D60 -- region_alloc(region_id, total_size)
// Returns pointer to start of allocated block within the region.
void* region_alloc(int region_id, int64_t requested_size) {
    // Step 1: Align requested size to 8-byte boundary, add 8 for capacity margin
    int64_t aligned_size = requested_size;
    if (requested_size == 0) {
        aligned_size = 8;              // minimum allocation
    } else if (requested_size & 7) {
        aligned_size = (requested_size + 7) & ~7;   // round up to 8
    }
    int64_t check_size = aligned_size + 8;           // capacity check includes margin

    // Step 2: Get current region block
    mem_block_t* block = region_table[region_id];    // qword_126EC88[region_id]
    void* alloc_ptr = block->next_free;              // block[2] = bump pointer

    // Step 3: Check if current block has enough space
    if (block->end - alloc_ptr < check_size) {
        // Not enough space -- try free-list or allocate new block
        bool is_reuse = block->is_reusable;          // block byte +40
        int64_t block_size;
        if (is_reuse) {
            block_size = 2048;                       // small reuse block
        } else {
            flush_region(region_table[region_id]);   // sub_6B68D0
            block_size = 0x10000;                    // 64KB default
        }

        // Search free-list (qword_1280730) for a suitable block
        block = find_free_block(aligned_size + 56, block_size);
        if (!block) {
            // Allocate fresh block from heap
            if (block_size < aligned_size + 56)
                block_size = aligned_size + 56;
            block_size = (block_size + 7) & ~7;      // align to 8
            block = malloc(block_size);
            if (!block) fatal_error(4);              // out of memory
            block->capacity = block_size;
            block->end = (char*)block + block_size;
            block->next_free = block + 6;            // skip 48-byte header
        }

        // Link new block into region's block chain
        block->is_reusable = 0;
        alloc_ptr = block->next_free;
        block->next = region_table[region_id];
        region_table[region_id] = block;
    }

    // Step 4: Bump the pointer
    total_allocated += aligned_size;                 // qword_1280700
    block->next_free = (char*)alloc_ptr + aligned_size;
    alignment_waste += aligned_size - requested_size; // qword_12806F8
    per_region_total[region_id] += aligned_size;     // qword_126EC50[region_id]

    return alloc_ptr;
}
```

### Region Architecture

```
dword_126EC90  = file_scope_region_id   (region 1, persistent)
dword_126EB40  = current_region_id      (file-scope or per-function)
dword_126F690  = file-scope base offset (typically 0)
dword_126F694  = file-scope prefix size (24 normal, 16 TU-copy)
dword_126F688  = function-scope base offset
dword_126F68C  = function-scope prefix size (8)
qword_126EC88  = region_table           (region index -> memory block)
qword_126EB90  = scope_table            (region index -> scope entry)
dword_126EC80  = total_region_count
```

Region selection uses a simple identity test: when `dword_126EB40 == dword_126EC90`, the current scope is file-scope and nodes go into region 1. When the values differ, the current scope is a function body, and nodes go into the current function's region. Some allocators force a specific behavior:

- **File-scope only**: `alloc_in_file_scope_region` (`sub_5E03D0`) always uses `dword_126EC90`
- **Dual-region**: `alloc_in_region` (`sub_5E02E0`) branches on the identity test
- **Scratch region**: `alloc_in_scratch_region` (`sub_5E0460`) temporarily sets TU-copy mode, allocates from region 1, and restores state
- **Same-region-as**: Used by `alloc_class_list_entry` (`sub_5E2410`) and `alloc_based_type_list_member` (`sub_5E29C0`) -- inspects the prefix byte of an existing node to determine which region it lives in, then allocates the new node in that same region

### Allocation Protocol

Every IL node allocator follows a consistent protocol. The prefix size varies by mode: 24 bytes for file-scope (normal), 16 bytes for file-scope (TU-copy mode), and 8 bytes for function-scope.

**File-scope allocation (normal mode, `dword_126F694 = 24`):**

```
 1. if (dword_126EFC8) trace_enter(5, "alloc_<name>")
 2. raw = region_alloc(file_scope_region, entry_size + 24)
 3. ptr = raw + dword_126F690                       // base offset (typically 0)
 4. *(ptr+0)  = 0                                   // zero TU copy address slot (8 bytes)
    ++qword_126F7C0                                 // TU copy addr counter
 5. *(ptr+8)  = 0                                   // zero the next-in-list pointer (8 bytes)
    ++qword_126F750                                 // orphan pointer counter
 6. ++qword_126F7D8                                 // IL entry prefix counter
 7. *(ptr+16) = flags_byte:                         // prefix flags (8-byte qword, flags in low byte)
        bit 0 = 1                                   // allocated
        bit 1 = 1                                   // file_scope (not TU-copy)
        bit 3 = dword_126E5FC & 1                   // language flag (C++ vs C)
 8. node = ptr + 24                                 // skip 24-byte prefix
 9. ++qword_126F8xx                                 // per-type counter
10. initialize type-specific fields
11. copy 96-byte common header from template globals
12. if (dword_126EFC8) trace_leave()
13. return node
```

**Function-scope allocation (`dword_126F68C = 8`):**

```
 1. raw = region_alloc(current_region, entry_size + 8)
 2. ptr = raw + dword_126F688                       // function-scope base offset
 3. *(ptr+0) = flags_byte:                          // prefix flags (8-byte qword)
        bit 1 = !dword_106BA08                      // file_scope flag
        bit 3 = dword_126E5FC & 1                   // language flag
    (no TU copy slot, no next-in-list slot)
 4. node = ptr + 8                                  // skip 8-byte prefix
 5. return node
```

The returned pointer skips the prefix, so all field offsets documented in the IL are relative to this returned pointer. The prefix flags byte is always at `node - 8` regardless of allocation mode. The next-in-list link (file-scope only) is at `node - 16`, and the TU-copy address (normal file-scope only) is at `node - 24`.

### Common IL Header Template

Every IL node contains a 96-byte common header, copied from six `__m128i` template globals initialized by `init_il_alloc` (`sub_5EAD80`):

```
xmmword_126F6A0  [+0..+15]    16 bytes, zeroed
xmmword_126F6B0  [+16..+31]   16 bytes (high qword zeroed)
xmmword_126F6C0  [+32..+47]   16 bytes, zeroed
xmmword_126F6D0  [+48..+63]   16 bytes, zeroed
xmmword_126F6E0  [+64..+79]   16 bytes (from qword_126EFB8 = source position)
xmmword_126F6F0  [+80..+95]   16 bytes (low word = 4, high qword = 0)
qword_126F700    [+96..+103]  8 bytes (current source file reference)
```

This template captures the current source position and language state at the moment of allocation. The template is refreshed when the parser advances through source positions, so each newly-allocated node carries the file/line/column of the construct it represents.

## IL Entry Prefix

Every IL entry has a variable-size raw prefix preceding the node body. The prefix is 24 bytes in normal file-scope mode, 16 bytes in TU-copy file-scope mode, and 8 bytes in function-scope mode.

```
Normal file-scope (24-byte prefix, ptr = raw + 24):
+0   [8 bytes]  TU copy   ptr - 24   translation_unit_copy_address
+8   [8 bytes]  next      ptr - 16   next_in_list link
+16  [8 bytes]  flags     ptr - 8    prefix flags byte (+ 7 padding)
+24  [...]      body      ptr + 0    node-specific fields

TU-copy file-scope (16-byte prefix, ptr = raw + 16):
+0   [8 bytes]  next      ptr - 16   next_in_list link
+8   [8 bytes]  flags     ptr - 8    prefix flags byte (+ 7 padding)
+16  [...]      body      ptr + 0    node-specific fields

Function-scope (8-byte prefix, ptr = raw + 8):
+0   [8 bytes]  flags     ptr - 8    prefix flags byte (+ 7 padding)
+8   [...]      body      ptr + 0    node-specific fields
```

### Prefix Flags Byte

| Bit | Mask | Name | Set When |
|---|---|---|---|
| 0 | `0x01` | `allocated` | Always set on fresh allocation |
| 1 | `0x02` | `file_scope` | `!dword_106BA08` (not in TU-copy mode) |
| 2 | `0x04` | `is_in_secondary_il` | Entry from secondary translation unit |
| 3 | `0x08` | `language_flag` | `dword_126E5FC & 1` (C++ mode indicator) |
| 7 | `0x80` | `keep_in_il` | Set by device code marking pass |

Bit 7 is the CUDA-critical `keep_in_il` flag used to select device-relevant declarations. See [Keep-in-IL](./keep-in-il.md) for the marking algorithm. The flags byte is always at `entry - 8` regardless of allocation mode, and the sign-bit position allows a fast test: `*(signed char*)(entry - 8) < 0` means "keep this entry."

Some allocators preserve bit 7 across free-list recycling (notably `alloc_local_constant` at `sub_5E1A80` and `alloc_derivation_step` at `sub_5E1EE0`), ensuring that the keep-in-il status is not lost when a node is reclaimed and reissued.

## Complete Node Size Table

The stats dump function `sub_5E99D0` prints the allocation table with exact names and per-unit sizes for all 70+ IL entry types. Sizes listed are the allocation unit in bytes -- the values passed to `region_alloc`.

### Primary IL Nodes

| IL Entry Type | Size (bytes) | Counter Global | Allocator | Region |
|---|---|---|---|---|
| type | 176 | `qword_126F8E0` | `sub_5E3D40` | file-scope |
| variable | 232 | `qword_126F8C0` | `sub_5E4D20` | dual (kind-dependent) |
| routine | 288 | `qword_126F8A8` | `sub_5E53D0` | file-scope |
| expr_node | 72 | `qword_126F880` | `sub_5E62E0` | dual + free-list |
| statement | 80 | `qword_126F818` | `sub_5E7060` | dual |
| scope | 288 | `qword_126F7E8` | `sub_5E7D80` | dual |
| constant | 184 | `qword_126F968` | `sub_5E11C0` | dual |
| field | 176 | `qword_126F8B0` | `sub_5E4F70` | file-scope |
| label | 128 | `qword_126F888` | `sub_5E5CA0` | function-scope only |
| asm_entry | 152 | `qword_126F890` | `sub_5E57B0` | dual |
| namespace | 128 | `qword_126F7F8` | `sub_5E7A70` | file-scope |
| template | 208 | `qword_126F720` | `sub_5E8D20` | file-scope |
| template_parameters | 136 | `qword_126F728` | `sub_5E8A90` | file-scope |
| template_arg | 64 | `qword_126F900` | `sub_5E2190` | file-scope + free-list |

### Type Supplements

Auxiliary structures allocated alongside type nodes by `set_type_kind` (`sub_5E2E80`):

| Supplement | Size | Counter | For Type Kinds |
|---|---|---|---|
| integer_type_supplement | 32 | `qword_126F8E8` | `tk_integer` (2) |
| routine_type_supplement | 64 | `qword_126F958` | `tk_routine` (7) |
| class_type_supplement | 208 | `qword_126F948` | `tk_class` (9), `tk_struct` (10), `tk_union` (11) |
| typeref_type_supplement | 56 | `qword_126F8F0` | `tk_typeref` (12) |
| templ_param_supplement | 40 | `qword_126F8F8` | `tk_template_param` (14) |

### Expression Supplements

Allocated inline by `set_expr_node_kind` (`sub_5E5F00`) for expression kinds that need extra storage:

| Supplement | Size | Counter | For Expression Kind |
|---|---|---|---|
| new/delete supplement | 56 | `qword_126F868` | `enk_new_delete` (7) |
| throw supplement | 24 | `qword_126F860` | `enk_throw` (8) |
| condition supplement | 32 | `qword_126F858` | `enk_condition` (9) |

### Statement Supplements

Allocated inline by `set_statement_kind` (`sub_5E6E20`):

| Supplement | Size | Counter | For Statement Kind |
|---|---|---|---|
| constexpr_if | 24 | `qword_126F798` | `stmk_constexpr_if` (2) |
| block | 32 | `qword_126F830` | `stmk_block` (11) |
| for_loop | 24 | `qword_126F820` | `stmk_for` (13) |
| try supplement | 32 | `qword_126F838` | `stmk_try_block` (19) |
| switch_stmt_descr | 24 | `qword_126F848` | `stmk_switch` (16) |
| coroutine_descr | 128 | `qword_126F828` | `stmk_coroutine` (9) |

### Linked-List Entry Types

| Entry Type | Size | Counter | Notes |
|---|---|---|---|
| class_list_entry | 16 | `qword_126F940` | Region-aware (`sub_5E2410`) or simple (`sub_5E26A0`) |
| routine_list_entry | 16 | `qword_126F938` | `sub_5E2750` |
| variable_list_entry | 16 | `qword_126F930` | `sub_5E2800` |
| constant_list_entry | 16 | `qword_126F928` | Free-list recycled (`sub_5E28B0`) |
| IL_entity_list_entry | 24 | `qword_126F7B8` | `sub_5E94F0` |
| based_type_list_member | 24 | `qword_126F950` | Region-aware (`sub_5E29C0`) |

### Inheritance and Virtual Dispatch

| Entry Type | Size | Counter | Allocator |
|---|---|---|---|
| base_class | 112 | `qword_126F908` | `sub_5E2300` |
| base_class_derivation | 32 | `qword_126F910` | `sub_5E1FD0` |
| derivation_step | 24 | `qword_126F918` | `sub_5E1EE0` |
| overriding_virtual_func | 40 | `qword_126F920` | `sub_5E20D0` |

### Variable and Routine Auxiliaries

| Entry Type | Size | Counter | Allocator |
|---|---|---|---|
| dynamic_init | 104 | `qword_126F8D8` | `sub_5E4650` |
| local_static_var_init | 40 | `qword_126F8D0` | `sub_5E4870` |
| vla_dimension | 48 | `qword_126F8C8` | `sub_5E49C0` |
| variable_template_info | 24 | `qword_126F8B8` | `sub_5E4C70` |
| exception_specification | 16 | `qword_126F8A0` | `sub_5E5130` |
| exception_spec_type | 24 | `qword_126F898` | `sub_5E51D0` |
| param_type | 80 | `qword_126F960` | `sub_5E1D40` (free-list recycled) |
| constructor_init | 48 | `qword_126F810` | `sub_5E7410` |
| handler | 40 | `qword_126F840` | `sub_5E6B90` |
| switch_case_entry | 56 | `qword_126F850` | `sub_5E6A60` |

### Scope and Source Tracking

| Entry Type | Size | Counter | Allocator |
|---|---|---|---|
| source_sequence_entry | 32 | `qword_126F780` | `sub_5E8300` (free-list recycled) |
| src-seq_secondary_decl | 56 | `qword_126F778` | `sub_5E8480` |
| src-seq_end_of_construct | 24 | `qword_126F770` | `sub_5E85B0` |
| src-seq_sublist | 24 | `qword_126F768` | `sub_5E86C0` |
| local-scope-ref | 32 | `qword_126F7E0` | `sub_5E80A0` |
| object_lifetime | 64 | `qword_126F800` | `sub_5E7800` (free-list recycled) |
| static_assertion | 24 | `qword_126F788` | `sub_5E81B0` |

### Templates, Names, and Pragmas

| Entry Type | Size | Counter | Allocator |
|---|---|---|---|
| template_decl | 40 | `qword_126F738` | `sub_5E8C60` |
| requires_clause | 16 | `qword_126F730` | `sub_5E8BB0` |
| name_reference | 40 | `qword_126F718` | `sub_5E90B0` |
| name_qualifier | 40 | `qword_126F710` | `sub_5E8FC0` |
| element_position | 24 | `qword_126F708` | `sub_5E8EB0` |
| pragma | 64 | `qword_126F808` | `sub_5E7570` |
| using-decl | 80 | `qword_126F7F0` | `sub_5E7BF0` |
| instantiation_directive | 40 | `qword_126F758` | `sub_5E8770` |
| linkage_spec_block | 32 | `qword_126F760` | `sub_5E8830` |
| hidden_name | 32 | `qword_126F740` | `sub_5E8980` |

### Attributes and Miscellaneous

| Entry Type | Size | Counter | Allocator |
|---|---|---|---|
| attribute | 72 | `qword_126F7B0` | `sub_5E9600` |
| attribute_arg | 40 | `qword_126F7A8` | `sub_5E96F0` |
| attribute_group | 8 | `qword_126F7A0` | `sub_5E97C0` |
| source_file | 80 | `qword_126F970` | `sub_5E08D0` |
| seq_number_lookup_entry | 32 | `qword_126F7C8` | `sub_5E9170` |
| subobject_path | 24 | `qword_126F790` | `sub_5E0A30` |
| orphaned_list_header | 56 | `qword_126F748` | `sub_5E0800` |

### Bookkeeping Counters (No Separate Allocator)

| Counter | Size | Global | Meaning |
|---|---|---|---|
| string_literal_text | 1 | `qword_126F7D0` | Raw string literal bytes (accumulated) |
| fs_orphan_pointers | 8 | `qword_126F750` | File-scope orphan pointer slots |
| trans_unit_copy_addr | 8 | `qword_126F7C0` | TU-copy address slots written |
| IL_entry_prefix | 4 | `qword_126F7D8` | Total prefix flags bytes written |

## Free-List Recycling

Six node types use free-list recycling to avoid allocating fresh memory for high-churn entries. Each free-list is a singly-linked list with the link pointer embedded in the node itself.

### Active Free-Lists

| Node Type | Free-List Head | Link Offset | Alloc Function | Free Function |
|---|---|---|---|---|
| template_arg (64B) | `qword_126F670` | +0 | `sub_5E2190` | `sub_5E22D0` (`free_template_arg_list`) |
| constant_list_entry (16B) | `qword_126F668` | +0 | `sub_5E28B0` | `sub_5E2990` (`return_constant_list_entries_to_free_list`) |
| expr_node (72B) | `qword_126E4B0` | +64 | `sub_5E62E0` | (kind set to 36 = `ek_reclaimed`) |
| constant (184B) | `qword_126E4B8` | +104 | `sub_5E1A80` (`alloc_local_constant`) | `sub_5E1B70` (`free_local_constant`) |
| param_type (80B) | `qword_126F678` | +0 | `sub_5E1D40` (`alloc_param_type`) | `sub_5E1EB0` (`free_param_type_list`) |
| source_seq_entry (32B) | scope+328 | -- | `sub_5E8300` | (per-scope recycling) |
| object_lifetime (64B) | scope+512 | +56 | `sub_5E7800` | (per-scope recycling) |

### Expression Node Recycling

Expression nodes use the most sophisticated free-list protocol. The allocator (`sub_5E62E0`) checks `qword_126E4B0` before allocating fresh memory:

```c
// Pseudocode for alloc_expr_node
if (expr_free_list != NULL) {
    node = expr_free_list;
    assert(node->kind == 36);        // ek_reclaimed sentinel
    expr_free_list = *(node + 64);   // link at offset +64
    // reuse node (preserves bit 7 of prefix)
} else {
    node = region_alloc(region_id, 72);
    // full prefix initialization
}
set_expr_node_kind(node, requested_kind);
++total_expr_count;
++fs_expr_count;
update_rescan_counter(&rescan_expr_count);
```

When expression nodes are freed, their kind byte at offset +24 is set to 36 (`ek_reclaimed`), and their link pointer at offset +64 chains them into the free list. The stats dump walks this free list to count available recycled nodes, printing them as `"(avail. fs expr node)"`.

A source-tracking variant `alloc_expr_node_with_source_tracking` (`sub_5E66B0`) wraps the allocation in `save_source_correspondence`/`restore_source_correspondence` calls (`sub_5B8910`/`sub_5B89C0`). For non-same-region allocations, this variant uses `alloc_permanent(72)` instead of the dual-region allocator because the free list cannot safely cross region boundaries.

### Constant Recycling

Local constants use a separate free-list (`qword_126E4B8`) with the link at offset +104. The `free_local_constant` function (`sub_5E1B70`) validates the node is in-use (bit 0 of prefix) before unlinking. The `check_local_constant_use` assertion function (`sub_5E1D00`) verifies `qword_126F680 == 0` at function boundaries, ensuring all borrowed constants have been returned.

The `duplicate_constant_to_other_region` function (`sub_5E1BB0`) handles the case where a constant must be copied from one region to another. When source and destination are the same region, it works in-place. When they differ, it allocates 184 bytes in the target region, copies contents via `sub_5BA500`, frees the original to the free list, and applies post-copy fixups (`sub_5B9DE0`, `sub_5D39A0`).

## set_type_kind -- Type Kind Dispatch

`set_type_kind` (`sub_5E2E80`, confirmed at `il_alloc.c:2334`) writes the type kind byte at offset `+132` of the type node and allocates any required type supplement. It handles 22 type kinds (0x00-0x15):

| Kind | Name | Action |
|---|---|---|
| 0 | `tk_error` | No-op |
| 1 | `tk_void` | No-op |
| 2 | `tk_integer` | Allocates 32-byte `integer_type_supplement`, sets default access=5 |
| 3 | `tk_float` | Sets format byte = 2 |
| 4 | `tk_complex` | Sets format byte = 2 |
| 5 | `tk_imaginary` | Sets format byte = 2 |
| 6 | `tk_pointer` | Zeroes 2 payload fields |
| 7 | `tk_routine` | Allocates 64-byte `routine_type_supplement`, initializes calling convention and parameter bitfields |
| 8 | `tk_array` | Zeroes size and flags fields |
| 9 | `tk_class` | Allocates 208-byte `class_type_supplement`, stores kind at +100 |
| 10 | `tk_struct` | Same as class |
| 11 | `tk_union` | Same as class |
| 12 | `tk_typeref` | Allocates 56-byte `typeref_type_supplement` |
| 13 | `tk_ptr_to_member` | Zeroes fields |
| 14 | `tk_template_param` | Allocates 40-byte `templ_param_supplement` |
| 15 | `tk_vector` | Zeroes fields |
| 16 | `tk_scalable_vector` | Zeroes fields |
| 17-21 | Pack/special types | No-op or zeroes |
| default | -- | `internal_error("set_type_kind: bad type kind")` |

The class type supplement (208 bytes) is the largest supplement. `init_class_type_supplement_fields` (`sub_5E2D70`) initializes it with defaults: `access=1`, `virtual_function_table_index=-1`, and zeroed member lists. The companion function `init_class_type_supplement` (`sub_5E2C70`) accesses the supplement through the type node's pointer at offset +152.

A combined function `init_type_fields_and_set_kind` (`sub_5E3590`, 317 lines) copies the 96-byte template header and then runs the same switch as `set_type_kind` inline. This is used by `alloc_type` (`sub_5E3D40`) to avoid a separate function call.

## set_expr_node_kind -- Expression Kind Dispatch

`set_expr_node_kind` (`sub_5E5F00`, confirmed at `il_alloc.c:3932`) writes the expression kind byte at offset `+24` and zeroes offset `+8`. It handles 36 expression kinds (0-35):

| Kind | Name | Action |
|---|---|---|
| 0 | `enk_error` | No-op |
| 1 | `enk_operation` | Sets operation bytes (0x78=120, 0x15=21, 0, 0), zeroes 2 qwords |
| 2-6 | `enk_constant`..`enk_lambda` | Zeroes 2 qword operand fields |
| 7 | `enk_new_delete` | Allocates 56-byte supplement via permanent alloc |
| 8 | `enk_throw` | Allocates 24-byte supplement |
| 9 | `enk_condition` | Allocates 32-byte supplement |
| 10 | `enk_object_lifetime` | Zeroes 2 qwords |
| 11,25,32 | Address-of variants | 1 qword + flag |
| 12-15 | Cast variants | Sets word=1, 1 qword |
| 16 | `enk_address_of_ellipsis` | No-op |
| 17,18,22,23,29,33,35 | Simple operand | 1 qword |
| 19 | `enk_routine` | 3 qwords |
| 20 | `enk_type_operand` | 2 qwords |
| 21 | `enk_builtin_operation` | Sets byte=117 (0x75), 1 qword |
| 24,26,27,30,31 | Complex operand | 2 qwords |
| 28 | `enk_fold_expression` | 1 qword + 1 dword |
| 34 | `enk_const_eval_deferred` | 1 qword + 1 dword |
| default | -- | `internal_error("set_expr_node_kind: bad kind")` |

The `reinit_expr_node_kind` function (`sub_5E60E0`) performs the same dispatch but additionally resets header fields (flag bits and source position from `qword_126EFB8`) before the kind switch. This is used when an existing expression node is repurposed without reallocation.

## set_statement_kind -- Statement Kind Dispatch

`set_statement_kind` (`sub_5E6E20`, confirmed at `il_alloc.c:4513`) writes the statement kind byte at offset `+32` and zeroes offset `+40`. It handles 26 statement kinds (0x00-0x19):

| Kind | Name | Supplement |
|---|---|---|
| 0 | `stmk_expr` | 1 qword (expression pointer) |
| 1 | `stmk_if` | 2 qwords (condition + body) |
| 2 | `stmk_constexpr_if` | Allocates 24 bytes |
| 3,4 | `stmk_if_consteval` | 2 qwords |
| 5 | `stmk_while` | 1 qword |
| 6,7 | `stmk_goto`/`stmk_label` | 2 qwords |
| 8 | `stmk_return` | 1 qword |
| 9 | `stmk_coroutine` | 1 qword (links to 128-byte coroutine_descr) |
| 10,23,24,25 | Various | No-op |
| 11 | `stmk_block` | Allocates 32 bytes, stores source pos, sets priority |
| 12 | `stmk_end_test_while` | 1 qword |
| 13 | `stmk_for` | Allocates 24 bytes |
| 14 | `stmk_range_based_for` | 2 qwords |
| 15 | `stmk_switch_case` | 2 qwords |
| 16 | `stmk_switch` | Allocates 24 bytes |
| 17 | `stmk_init` | 1 qword |
| 18 | `stmk_asm` | 1 qword + flag |
| 19 | `stmk_try_block` | Allocates 32 bytes |
| 20 | `stmk_decl` | 1 qword |
| 21,22 | VLA statements | 1 qword |
| default | -- | `internal_error("set_statement_kind: bad kind")` |

## set_constant_kind -- Constant Kind Dispatch

`set_constant_kind` (`sub_5E0C60`, confirmed at `il_alloc.c:952`) writes the constant kind byte at offset `+148` and initializes the variant-specific union fields. 16 constant kinds (0-15):

| Kind | Name | Action |
|---|---|---|
| 0 | `ck_error` | Zeroes variant fields |
| 1 | `ck_integer` | Calls `init_target_int` (`sub_461260`) |
| 2 | `ck_string` | Zeroes string fields |
| 3 | `ck_float` | Zeroes float fields |
| 4 | `ck_address` | Allocates 32-byte sub-node in file-scope region |
| 5 | `ck_complex` | Zeroes complex fields |
| 6 | `ck_imaginary` | Zeroes imaginary fields |
| 7 | `ck_ptr_to_member` | Zeroes 2 fields |
| 8 | `ck_label_difference` | Zeroes 2 fields |
| 9 | `ck_dynamic_init` | Zeroes |
| 10 | `ck_aggregate` | Zeroes aggregate list head |
| 11 | `ck_init_repeat` | Zeroes repeat fields |
| 12 | `ck_template_param` | Zeroes, dispatches to `set_template_param_constant_kind` |
| 13 | `ck_designator` | Zeroes |
| 14 | `ck_void` | Zeroes |
| 15 | `ck_reflection` | Zeroes |
| default | -- | `internal_error("set_constant_kind: bad kind")` |

The template parameter constant kind has its own sub-dispatch (`sub_5E0B40`, `il_alloc.c:768`) handling 14 sub-kinds (`tpck_*`), each zeroing variant fields at offsets +160, +168, +176. It validates the parent constant kind is 12 (`ck_template_param`) before proceeding.

## Additional Kind Dispatchers

### set_routine_special_kind

`sub_5E5280` (confirmed at `il_alloc.c:3065`) sets the routine special kind byte at offset `+166`. 8 values (0-7):

| Kind | Action |
|---|---|
| 0 | Sets word at +168 to 0 |
| 1-4 | No-op |
| 5 | Zeroes byte at +168 |
| 6-7 | Zeroes qword at +168 |
| default | `internal_error("set_routine_special_kind: bad kind")` |

### set_dynamic_init_kind

`sub_5E45C0` (confirmed at `il_alloc.c:2506`) sets the dynamic init kind at offset `+48`. 10 values (0-9) controlling what fields are initialized in the dynamic initialization variant union.

## Statistics Dump

`dump_il_table_stats` (`sub_5E99D0`) prints a formatted table of all IL allocation counters. It is invoked when tracing is enabled or on explicit request. The output format:

```
IL table use:
   Table                    Number    Each     Total
   -----                    ------    ----     -----
   source file                  42      80      3360
   constant                   1847     184    339848
   type                        923     176    162448
   variable                    412     232     95584
   routine                     287     288     82656
   expr node                 12847      72    924984
   statement                  5923      80    473840
   scope                       312     288     89856
   ...
   Total                                     2172576
```

The function iterates all 70+ counters, multiplies count by per-unit size, accumulates a running total, and adds the passed argument `a1` (typically the raw region overhead) for the final sum. It also walks the expr_node free list (`qword_126E4B0`) to count available recycled nodes, printing them separately as `"(avail. fs expr node)"`.

The counter globals are contiguous in BSS from `qword_126F680` through `qword_126F970`, with 8-byte spacing (qword counters). The full ordered list of counters is documented in the [Complete Node Size Table](#complete-node-size-table) above.

## Initialization and Reset

### init_il_alloc (`sub_5EAD80`)

Called once at compiler startup. Responsibilities:

1. Zeroes the 96-byte common header template (`xmmword_126F6A0`-`xmmword_126F6F0`)
2. Sets the source position portion of the template from `qword_126EFB8`
3. Computes the language mode byte: `byte_126E5F8 = (dword_126EFB4 != 2) + 2` (C++ mode detection)
4. Registers 6 allocator state variables with `sub_7A3C00` (saveable state for region offset save/restore across compilation phases)
5. Optionally calls `sub_6F5D00` if `dword_106BF18` is set (debug initialization)

### reset_region_offsets (`sub_5EAEC0`)

Resets the bump allocator watermarks. Called at region boundaries:

```c
dword_126F690 = 0;               // base offset reset
if (dword_106BA08) {              // TU-copy mode
    dword_126F68C = 8;            // function-scope watermark
    dword_126F688 = 0;            // function-scope base
    dword_126F694 = 16;           // file-scope watermark
} else {
    dword_126F694 = 24;           // file-scope watermark (extra 8 for TU copy addr)
}
```

The different initial watermark values (16 vs 24) reflect the prefix size in each mode: normal mode uses a 24-byte prefix (8 TU-copy + 8 next-link + 8 flags), while TU-copy mode uses a 16-byte prefix (8 next-link + 8 flags, no TU-copy slot). Function-scope allocations use `dword_126F68C = 8` (8-byte prefix: flags only).

### clear_free_lists (`sub_5EAF00`)

Zeroes all 5 global free-list heads:

```c
qword_126F678 = 0;    // param_type free list
qword_126F670 = 0;    // template_arg free list
qword_126E4B8 = 0;    // local_constant free list
qword_126E4B0 = 0;    // expr_node free list
qword_126F668 = 0;    // constant_list_entry free list
```

Called at function-scope exit to prevent dangling pointers into freed regions.

## String Allocation

Two specialized allocators handle string storage in regions:

### copy_string_to_region (`sub_5E0600`, `il_alloc.c:548`)

```c
char* copy_string_to_region(int region_id, const char* str) {
    size_t len = strlen(str);
    char* buf;
    if (region_id == 0)
        buf = heap_alloc(len + 1);                // general heap
    else if (region_id == file_scope_region)
        buf = region_alloc(file_scope, len + 1);   // file-scope region
    else if (region_id == -1)
        buf = persistent_alloc(len + 1);           // persistent heap
    else
        internal_error("copy_string_to_region");
    return strcpy(buf, str);
}
```

### copy_string_of_length_to_region (`sub_5E0700`, `il_alloc.c:572`)

Same three-way dispatch but takes an explicit length parameter and uses `strncpy` with explicit null termination: `result[len] = 0`.

## Special Allocation Patterns

### Labels -- Function-Scope Assertion

`alloc_label` (`sub_5E5CA0`) asserts that `dword_126EB40 != dword_126EC90` (must be in function scope). Labels cannot exist at file scope -- they are always allocated in a function's region:

```c
assert(current_region != file_scope_region);   // il_alloc.c:3588
```

### Variables -- Kind-Dependent Region

`alloc_variable` (`sub_5E4D20`) uses the variable's linkage kind to select the allocation strategy: when `kind > 2` (non-local variables like global, extern, static), it uses the dual-region allocator (`sub_5E02E0`). Otherwise it allocates directly in the file-scope region. This ensures that local variables live in function regions while globals persist in the file-scope region.

### GNU Supplement for Routines

`alloc_gnu_supplement_for_routine` (`sub_5E56D0`, `il_alloc.c:3412`) asserts that no supplement already exists (`*(routine+240) == 0`), then allocates a 40-byte supplement and stores the pointer at `routine+240`. This is for GCC-extension attributes on functions (visibility, alias, constructor/destructor priority).

### Pragma -- 43 Kinds

`alloc_pragma` (`sub_5E7570`, `il_alloc.c:4781`) uses the same-region-as pattern (handling null, non-file-scope, scratch, and same-region-as cases) and dispatches a switch covering 43 pragma kinds (0-42). Most kinds are no-op; kinds 19, 21, 26, 28, 29 have small payload fields.

### Scope -- Routine Association

`alloc_scope` (`sub_5E7D80`) validates that if `assoc_routine` (argument a3) is non-null, the scope kind must be 17 (`sck_function`). Violation triggers `internal_error("assoc_routine is non-NULL")` at `il_alloc.c:4946`. After kind dispatch, it zeroes 26 qword fields (offsets 80-280) and sets `*(result+240) = -1` as a sentinel.

## Global Variable Map

| Address | Name | Purpose |
|---|---|---|
| `dword_126EC90` | `file_scope_region_id` | Region 1 identifier |
| `dword_126EB40` | `current_region_id` | Active allocation region |
| `dword_106BA08` | `tu_copy_mode` | TU-copy mode flag (affects prefix layout) |
| `dword_126EFC8` | `tracing_enabled` | When set, brackets alloc calls with trace_enter/leave |
| `qword_126EFB8` | `null_source_position` | Default source position for new nodes |
| `qword_126F700` | `current_source_file` | Current source file reference |
| `qword_106B9B0` | `compilation_context` | Active compilation context pointer |
| `dword_126E5FC` | `source_file_flags` | Bit 0 = C++ mode indicator |
| `byte_126E5F8` | `language_std_byte` | Language standard (controls routine type init) |
| `dword_106BFF0` | `uses_exceptions` | Exception model flag (set in routine alloc) |
