# Data Layout Optimization

The data layout optimization pass deduplicates constant values, eliminates unused constants, and compacts OCG (object-code-generator) constant bank sections across translation units. It is implemented by `sub_4339A0` (`constant_dedup`, 13,199 bytes at `0x4339A0`) and orchestrated by the layout phase `sub_439830` during phases 9c and 9d. The optimization reduces constant memory pressure by finding identical 32-bit and 64-bit values across compilation units, aliasing duplicate symbols to a single physical copy, and rewriting relocations to target the canonical instance.

The pass is enabled by `--optimize-data-layout` and disabled by `--no-opt`. These two flags are mutually exclusive; specifying both triggers a fatal error. When neither flag is given, the optimization runs automatically if any OCG constant bank section exceeds the architecture's maximum constant bank size (queried via vtable offset +32, typically 64 KB for CUDA constant memory).

| | |
|---|---|
| **Primary function** | `sub_4339A0` at `0x4339A0` (13,199 bytes, 426 decompiled lines) |
| **Caller** | `sub_439830` (layout phase) at phases 9c and 9d |
| **Hash table lookup** | `sub_449A80` at `0x449A80` (hash-table value lookup) |
| **Hash table insert** | `sub_448E70` at `0x448E70` (hash-table key-value insert) |
| **Reachability check** | `sub_43FB70` at `0x43FB70` (symbol entry-function reachability) |
| **Data copy** | `sub_433760` at `0x433760` (section data append with alignment) |
| **Large-value dedup** | `sub_433870` at `0x433870` (byte-level memcmp deduplication for 12..64 byte values) |
| **Overlap validation** | `sub_4343C0` at `0x4343C0` (section data overlap detection and identity check) |
| **32-bit hash function** | `sub_44E120` at `0x44E120` (identity: returns key unchanged) |
| **64-bit hash function** | `sub_44E1C0` at `0x44E1C0` (shift-xor: `(k>>11) ^ (k>>8) ^ (k>>5)`) |
| **64-bit xor-fold hash** | `sub_44E150` at `0x44E150` (custom hash: `lo32 ^ hi32`) |
| **Hash table create** | `sub_4489C0` at `0x4489C0` (allocate 112-byte header, set mode tag) |
| **CLI enable** | `--optimize-data-layout` (stored in `byte_2A5F2A8`, OR'd as `merge_flags` bit 14, decoded into `elfw+91`) |
| **CLI disable** | `--no-opt` (stored in `byte_2A5F2A9`, OR'd as `merge_flags` bit 5, decoded into `elfw+90`) |
| **Verbose gate** | `elfw+64, bit 1` (debug/verbose flags) |

## Position in the Pipeline

```
Phase 9b: Standard constant bank layout (sub_4325A0)
  |
  v
Phase 9c: Constant merging via sub_4339A0     <-- merge-constants mode
  |
  v
Phase 9d: OCG constant optimization            <-- this page (primary focus)
  |     via sub_4339A0
  v
Phase 10: Resource counting + UFT setup
```

The dedup engine `sub_4339A0` is called from two distinct code paths within the layout phase:

1. **Phase 9c (merge-constants mode)**: When `elfw+97` (merge-constants flag) is set, the layout phase creates a `TEMP_MERGED_CONSTANTS` temporary section and calls `sub_4339A0` with the `a13` parameter set to 1, meaning "copy all constants, even unreferenced ones." This mode deduplicates the standard constant bank (`.nv.constant0`) contents.

2. **Phase 9d (OCG constant optimization)**: Triggered when `elfw+91` (force-OCG-optimization) is set or any OCG constant section exceeds the bank size limit. Creates a `TEMP_OCG_CONSTANTS` temporary section and calls `sub_4339A0` with `a13=0` and a non-null overlap-set argument (`a15`), enabling entry-function-level dead constant elimination and per-entry OCG section tracking.

## Function Signature

```c
// sub_4339A0 -- constant_dedup
// Address: 0x4339A0
// Size: 13,199 bytes (~426 lines of decompiled pseudocode)
//
// a1:  elfw*      -- linker context / ELF wrapper
// a2:  section*   -- source constant section record
// a3:  uint32_t   -- target temporary section index
// a4:  htab_32*   -- hash table for 32-bit value dedup (key=value, val=symbol*)
// a5:  htab_64*   -- hash table for 64-bit value dedup (key=value, val=symbol*)
// a6:  list**     -- bucket for 12-byte value linked list
// a7:  list**     -- bucket for 16-byte value linked list
// a8:  list**     -- bucket for 20-byte value linked list
// a9:  list**     -- bucket for 24-byte value linked list
// a10: list**     -- bucket for 32-byte value linked list
// a11: list**     -- bucket for 48-byte value linked list
// a12: list**     -- bucket for 64-byte value linked list
// a13: char       -- copy-all flag (1=copy unreferenced constants, 0=skip dead ones)
// a14: reloc**    -- relocation list head pointer (for OCG reloc rewriting)
// a15: set*       -- per-entry overlap set (NULL in merge-constants mode)
_QWORD* __fastcall sub_4339A0(
    __int64 a1, __int64 a2, unsigned int a3,
    __int64 a4, __int64 a5,
    _QWORD* a6, _QWORD* a7, _QWORD* a8,
    _QWORD* a9, _QWORD* a10, _QWORD* a11,
    _QWORD* a12, char a13,
    _QWORD* a14, __int64 a15);
```

## Deduplication Algorithm

The function iterates the source section's symbol linked list (rooted at `a2+72`). For each symbol, it extracts the data pointer, alignment, and size from the 40-byte symbol node, then dispatches into one of three dedup strategies based on the data size.

### Size-Based Dispatch

```
for each symbol node in source_section->symbol_list:
    sym_record = get_symbol(elfw, node->sym_index)

    if (a13 == true) and (node->sym_index != 0) and reachable(elfw, sym_record):
        // Phase 9c path: force-copy reachable constant
        aligned_offset = align_up(target_section->size, node->alignment)
        sym_record->value = aligned_offset
        section_data_copy(elfw, target_section_idx, data, alignment, size)
        continue

    switch (node->data_size):
        case 4:   dedup_32bit(...)
        case 8:   dedup_64bit(...)
        case 12:  dedup_memcmp(a6, 12, ...)
        case 16:  dedup_memcmp(a7, 16, ...)
        case 20:  dedup_memcmp(a8, 20, ...)
        case 24:  dedup_memcmp(a9, 24, ...)
        case 32:  dedup_memcmp(a10, 32, ...)
        case 48:  dedup_memcmp(a11, 48, ...)
        case 64:  dedup_memcmp(a12, 64, ...)
        default:  section_data_copy(...)  // no dedup, just copy
```

The reachability check `sub_43FB70` determines whether a constant symbol is referenced by any live entry-point function. It checks the debug flag (`elfw+80`) and the `kernels-used / variables-used filter-active` flag (`elfw+97`). When neither is set, the call returns the debug flag itself (typically 1, meaning "no DCE -- keep the symbol"). When the filter is active, it queries entry-function reachability via the per-symbol-class used-set: for global symbols (binding bit 4 set), it checks against the function set at `elfw+536`; for local symbols, against `elfw+544`. Both queries use `sub_43EB40`, a string-based set membership test on the symbol name. The `+97` flag is raised by `sub_43F360` / `sub_43F950` (`--kernels-used` / `--variables-used` loaders) and by the auxiliary loaders `sub_43F510` / `sub_43F040` / `sub_43F020`; it is *not* the merge-constants flag.

### 32-Bit Value Deduplication

For 4-byte constants, the algorithm uses a dedicated hash table (`a4`) keyed on the integer value:

```c
uint32_t val = *(uint32_t*)data_ptr;
uint64_t key = (uint64_t)val;              // zero-extended for hash table
symbol* existing = hash_lookup(a4, key);   // sub_449A80

if (existing != NULL && val != 0) {
    // Duplicate found -- alias this symbol to the existing one
    if (existing != sym_record) {
        // verbose: "found duplicate value 0x%x, alias %s to %s"
        sym_record->value = existing->value;   // offset +8: copy address
    }
} else if (val != 0 || a15 == NULL) {
    // First occurrence or zero value without overlap tracking
    aligned_offset = align_up(target_size, alignment)
    sym_record->value = aligned_offset
    section_data_copy(elfw, target_idx, data, alignment, size)
    hash_insert(a4, key, sym_record)       // sub_448E70
} else {
    // val == 0 and overlap set tracking is active (OCG mode)
    // Check if this symbol's entry-function was already seen
    if (!set_contains(a15, sym_record->name_ptr)):
        // First zero-value in this entry's scope
        aligned_offset = align_up(target_size, alignment)
        sym_record->value = aligned_offset
        section_data_copy(elfw, target_idx, data, alignment, size)
        hash_insert(a4, key, sym_record)
        set_insert(a15, sym_record->name_ptr)
        // Then rewrite OCG relocations targeting this constant
        rewrite_ocg_relocs(elfw, a14, sym_record, aligned_offset)
    else:
        // Already have a zero-value for this entry
        // Rewrite OCG relocations anyway (different offset)
        rewrite_ocg_relocs(elfw, a14, sym_record, sym_record->value)
}
```

The zero-value case receives special treatment. Zero-valued constants are common (uninitialized constant declarations compile to zero-filled entries), and in OCG mode each entry function may have its own instance. The overlap set `a15` tracks which entry-function scopes have already allocated a zero-value slot to prevent redundant copies while still allowing per-entry uniqueness.

When a duplicate is found, the aliasing sets `sym_record->value` (field at offset +8 in the symbol record) to the existing symbol's value. This is the address within the target section. No data is copied -- the duplicate symbol simply points to the same physical location as the original.

### 64-Bit Value Deduplication

The 8-byte case follows the same pattern as 32-bit but uses the second hash table (`a5`) and the full 64-bit value as the key:

```c
uint64_t val = *(uint64_t*)data_ptr;
symbol* existing = hash_lookup(a5, val);

if (existing != NULL && val != 0) {
    if (existing != sym_record) {
        // verbose: "found duplicate 64bit value 0x%llx, alias %s to %s"
        sym_record->value = existing->value;
    }
} else if (val != 0 || a15 == NULL) {
    // same as 32-bit first-occurrence path
    ...
    hash_insert(a5, val, sym_record)
} else {
    // same zero-value OCG tracking path as 32-bit
    ...
}
```

### Large-Value Deduplication (12--64 Bytes)

For values of sizes 12, 16, 20, 24, 32, 48, and 64 bytes, the function delegates to `sub_433870` (`dedup_memcmp`, 216 bytes at `0x433870`). Each size class has its own singly-linked list (parameters `a6` through `a12`). The function signature, reconstructed from the decompiled output:

```c
// sub_433870 -- dedup_memcmp
// Address: 0x433870, Size: 216 bytes
//
// a1: elfw*           -- linker context
// a2: list_head**     -- pointer to head of seen-values list for this size class
// a3: int             -- expected data size (12, 16, 20, 24, 32, 48, or 64)
// a4: data_node*      -- 40-byte data node from the source symbol list
//       +0:  void*    data_ptr       (pointer to raw constant bytes)
//       +8:  uint64_t name_or_id     (symbol name/reference)
//       +16: uint64_t alignment      (required alignment in bytes)
//       +24: uint64_t data_size      (redundant with a3)
//       +32: uint32_t sym_index      (symbol table index)
// a5: int64_t*        -- output: receives the assigned offset in target section
// a6: const char**    -- output: receives the symbol name (for verbose logging)
// a7: int64_t*        -- pointer to target_section->size (updated on copy)
// a8: uint32_t        -- target section index
```

The algorithm walks the linked list, comparing alignment and content byte-for-byte:

```c
void dedup_memcmp(elfw* ctx, list_node** head, int size,
                  data_node* node, int64_t* offset_out,
                  const char** name_out, int64_t* section_size,
                  uint32_t target_idx)
{
    list_node* cur = *head;
    uint64_t align = node->alignment;      // offset +16 of data_node
    void*    data  = node->data_ptr;       // offset +0

    while (cur != NULL) {
        data_node* existing = cur->payload;

        // Two-stage comparison:
        //   1. Alignment must match (fast integer compare, rejects most)
        //   2. Byte-exact content match via memcmp
        if (existing->alignment == align &&
            memcmp(data, existing->data_ptr, size) == 0)
        {
            // Duplicate found -- look up the canonical symbol's offset
            symbol* sym = get_symbol(ctx, existing->sym_index);

            if (ctx->verbose_layout)    // (*(ctx+64) & 2)
                fprintf(stderr,
                    "found duplicate %d byte value, alias %s to %s\n",
                    size, *name_out, sym->name);

            *offset_out = sym->value;   // alias to existing offset
            return;
        }
        cur = cur->next;
    }

    // No match found -- allocate space in the target section
    int64_t current_size = *section_size;
    if (current_size % align)
        current_size = current_size + align - (current_size % align);

    *offset_out = current_size;
    section_data_copy(ctx, target_idx, data, align, node->data_size);

    // Prepend this data_node to the seen-values list (sub_4644C0)
    // so future duplicates will find it
    list_prepend(node, head);
}
```

Key observations from the decompiled code:

- **Alignment check first**: The alignment comparison (`existing->alignment == align`) acts as a cheap filter before the expensive `memcmp`. Constants with different alignment requirements are never considered duplicates even if their byte content matches, because merging them would require the stricter alignment for all users.

- **Prepend, not append**: New entries are prepended to the list head via `sub_4644C0` (generic linked-list prepend). This means recently seen values are found first, which is beneficial when translation units define constants in similar order.

- **No hash, no index**: The linked-list approach for large values (rather than a hash table) is a deliberate tradeoff. These sizes are rare in practice -- most GPU constants are 4 or 8 bytes (scalar floats, doubles, pointers). The O(n) linear scan with `memcmp` is faster than computing and comparing a hash for the small typical population of each size class. A program with 50 distinct `float4` (16-byte) constants would require at most 50 comparisons per lookup, each of which is a single 16-byte `memcmp` -- likely a single SSE instruction on x86-64.

- **Size-class isolation**: Each of the seven supported large sizes has an independent list. A 16-byte constant is never compared against a 32-byte constant. This eliminates the need for a size field in the comparison and keeps each list short.

### Values Outside Standard Size Classes

Constants whose size does not match any of the nine dedup-eligible sizes (4, 8, 12, 16, 20, 24, 32, 48, 64) bypass deduplication entirely. The function falls through to `sub_433760` (section\_data\_copy), which appends the data to the target section at the next aligned offset. This handles unusual sizes like packed structs or non-power-of-two arrays.

If the alignment matches the size exactly for the 4-byte or 8-byte cases, the function uses the fast hash-table path. If alignment differs from size (e.g., a 4-byte value with 8-byte alignment), the function still uses the hash-table dedup but applies the larger alignment during placement.

## OCG Constant Relocation Rewriting

After aliasing or placing a constant in the target section, the function rewrites relocations that reference the original constant section. This is critical because the constants have moved from their original per-TU sections into the merged temporary section.

The relocation rewriting logic is embedded directly in `sub_4339A0` (appearing four times in the decompiled output -- once for each of the 32-bit and 64-bit paths, each with two sub-cases for the overlap-set tracking). The algorithm walks the relocation linked list (`a14`):

```c
// Invoked after placing/aliasing a constant symbol
for each reloc_node in *a14:
    reloc = reloc_node->payload;
    reloc_section = get_section(elfw, reloc->section_idx);

    if (reloc_section->parent == source_section_idx &&
        reloc->offset >= sym_record->original_value &&
        reloc->offset < sym_record->original_value + sym_record->size) {
        // This relocation targets the constant we just processed
        new_offset = reloc->offset + new_value - sym_record->original_value;
        reloc->offset = new_offset;
        // verbose: "optimize ocg constant reloc offset from %lld to %lld"
        // Remove from relocation list (unlink node)
    }
```

The check `reloc->offset >= original_value && reloc->offset < original_value + size` identifies relocations that fall within the address range of the source constant. The offset is then recomputed relative to the new position in the merged section: `new_offset = old_offset + (new_address - old_address)`.

This relocation rewriting is what makes the "optimize ocg constant reloc offset from %lld to %lld" message appear in verbose output. Each time a constant is placed or aliased, all relocations pointing into its original range are patched to point into the new location.

## Section Data Overlap Validation (`sub_4343C0`)

When constant data is copied into a target section, the section data append function delegates to `sub_4343C0` (`overlap_validate`, at `0x4343C0`). This function maintains a sorted linked list of data fragments within each section and validates that new data does not conflict with existing content at overlapping offsets.

### Function Signature

```c
// sub_4343C0 -- overlap_validate
// Address: 0x4343C0
//
// a1: elfw*           -- linker context (reinterpreted as FILE* by decompiler)
// a2: uint32_t        -- symbol index associated with this data
// a3: uint32_t        -- target section index
// a4: void*           -- pointer to data bytes to write
// a5: uint64_t        -- offset within target section (-1 = append at end)
// a6: uint32_t        -- alignment requirement
// a7: size_t          -- data size in bytes
```

### Overlap Detection Algorithm

The function maintains a sorted linked list of data records attached to each section (at section header offset +72). Each record is a 40-byte node:

```
data_record (40 bytes):
  +0:  void*     data_ptr         -- pointer to raw bytes
  +8:  uint64_t  offset           -- offset within section
  +16: uint64_t  alignment        -- alignment used during placement
  +24: uint64_t  size             -- data size
  +32: uint32_t  sym_index        -- originating symbol index
  +36: uint32_t  padding          -- (zeroed)
```

The insertion algorithm searches for the correct position in offset-sorted order, checking for three overlap cases:

```c
void overlap_validate(elfw* ctx, uint32_t sym_idx, uint32_t sec_idx,
                      void* data, uint64_t offset, uint32_t align, size_t size)
{
    section* sec = get_section(ctx, sec_idx);

    // --- Section size update ---
    // If offset == -1 (sentinel): append at current section end
    // If offset + size > section_size: extend section
    // If offset < section_size and offset + size > section_size:
    //   verbose: "offset %lld goes past section %d size"
    //   extend anyway (allow partial overlap past end)

    // --- ELF NOBITS (0x70000002) section special case ---
    // For NOBITS-like sections, pads data to 128 bytes with
    // fill pattern 0xF804002D01 (an NVIDIA NOP encoding) before insertion

    // --- Allocate new data record ---
    data_record* rec = arena_alloc(40);
    rec->data_ptr   = data;
    rec->offset     = offset;
    rec->alignment  = align;
    rec->size       = size;
    rec->sym_index  = sym_idx;

    // Update symbol's value and size from this record
    if (sym_idx != 0) {
        symbol* sym = get_symbol(ctx, sym_idx);
        sym->size = size;
        if (is_defined_symbol(sym))
            sym->value = offset;
    }

    // --- Find insertion point in sorted list ---
    if (offset == -1) {
        // Append to tail of list
        list_append(rec, &sec->data_list);
        return;
    }

    node* prev = NULL;
    node* cur  = sec->data_list;

    if (!cur) {
        // Empty list -- just insert
        list_prepend(rec, &sec->data_list);
        return;
    }

    data_record* cur_rec = cur->payload;
    uint64_t cur_offset  = cur_rec->offset;
    uint64_t cur_size    = cur_rec->size;

    // Walk list to find correct sorted position
    while (cur && cur_rec->offset < offset) {
        if (cur_rec->offset + cur_rec->size > offset) {
            // CASE 1: New data starts inside existing record
            goto validate_partial_overlap;
        }
        prev = cur;
        cur  = cur->next;
        if (cur) cur_rec = cur->payload;
    }

    if (!cur || data == NULL || size == 0 || !cur_rec->size) {
        // No overlap possible -- insert at this position
        list_insert_after(rec, prev, &sec->data_list);
        return;
    }

    if (offset == cur_rec->offset) {
        // CASE 2: Exact start-offset match with existing record
        goto validate_exact_overlap;
    }

    if (cur_rec->size >= size || offset + size != cur_rec->size + cur_rec->offset) {
        // Non-overlapping or size mismatch -- just insert
        list_insert_after(rec, prev ? prev : cur, &sec->data_list);
        return;
    }

    // CASE 3: New data extends to exactly where existing record ends
    // (tail overlap -- new data covers existing record's suffix)
    goto validate_tail_overlap;
}
```

### Three Overlap Cases

**Case 1: Partial forward overlap** -- The new data's start offset falls strictly inside an existing record's range (`existing.offset < new.offset < existing.offset + existing.size`):

```c
validate_partial_overlap:
    // verbose: "offset %lld (sym %d) overlaps in section %d"

    // Verify the new data does not extend past the existing record
    if (offset + size > cur_rec->offset + cur_rec->size)
        fatal_error("overlapping data spans too much");

    // Verify overlapping bytes are identical
    void* overlap_ptr = cur_rec->data_ptr + (offset - cur_rec->offset);
    if (memcmp(data, overlap_ptr, size) != 0)
        fatal_error("overlapping non-identical data");

    // Overlap is valid (identical content) -- merge symbol tracking
    hash_insert(sym_dedup_table, sym_idx, cur_rec->sym_index);
    free_record(rec);
```

**Case 2: Exact offset match** -- The new data starts at exactly the same offset as an existing record. Two sub-cases:

```c
validate_exact_overlap:
    // verbose: "offset %lld (sym %d) overlaps in section %d"

    if (size <= cur_rec->size) {
        // 2a: New data is same size or smaller -- verify prefix match
        if (memcmp(data, cur_rec->data_ptr, size) != 0)
            fatal_error("overlapping non-identical data");
        // Discard new record, merge symbol into existing
        free_record(rec);
        hash_insert(sym_dedup_table, sym_idx, cur_rec->sym_index);
    } else {
        // 2b: New data is LARGER -- it subsumes the existing record
        // Verify the existing content matches the corresponding prefix
        if (memcmp(data, cur_rec->data_ptr, cur_rec->size) != 0)
            fatal_error("overlapping non-identical data");
        // Replace existing record with new (larger) one
        cur->payload = rec;
        free_record(cur_rec);
        hash_insert(sym_dedup_table, cur_rec->sym_index, sym_idx);

        // Check if the next record is also subsumed
        node* next = cur->next;
        if (next) {
            data_record* next_rec = next->payload;
            if (next_rec->offset < offset + size) {
                // Next record overlaps with new data
                if (offset + size != next_rec->offset + next_rec->size)
                    fatal_error("overlapping data spans too much");
                // Verify trailing overlap bytes match
                size_t tail_off = next_rec->offset - offset;
                if (memcmp(data + tail_off, next_rec->data_ptr,
                           size - next_rec->size) != 0)
                    fatal_error("overlapping non-identical data");
                // Absorb next record -- unlink and merge symbol
                cur->next = next->next;
                free_record(next_rec);
                hash_insert(sym_dedup_table, next_rec->sym_index, sym_idx);
            }
        }
    }
```

**Case 3: Tail overlap** -- The new data ends at exactly the same offset as an existing record (`new.offset + new.size == existing.offset + existing.size`) and is larger than the existing record:

```c
validate_tail_overlap:
    // verbose: "offset %lld (sym %d) overlaps in section %d"
    size_t overlap_size = cur_rec->size;
    void* tail_in_new = data + size - overlap_size;

    if (memcmp(tail_in_new, cur_rec->data_ptr, overlap_size) != 0)
        fatal_error("overlapping non-identical data");

    // Replace existing with new larger record
    cur->payload = rec;
    free_record(cur_rec);
    hash_insert(sym_dedup_table, cur_rec->sym_index, sym_idx);
```

### Validation Invariants

The overlap validation enforces three strict invariants:

1. **Content identity**: Any bytes that are covered by both an existing record and a new record must be bitwise identical. A `memcmp` mismatch triggers a fatal error (`"overlapping non-identical data"` via `sub_467460`).

2. **Span containment**: A new record may partially overlap an existing record only if it is fully contained within the existing record's range, or if it fully subsumes the existing record. "Crossing" overlaps where neither contains the other trigger `"overlapping data spans too much"`.

3. **Sorted order**: Records are maintained in ascending offset order within the linked list, enabling the single-pass walk to detect all overlaps.

These checks are critical for correctness: when multiple translation units contribute the same constant (e.g., a `__constant__` variable defined in a header included by multiple `.cu` files), their data regions may legally overlap in the merged section. The validation ensures the link is sound -- overlapping regions contain identical content, so the merged section is well-defined.

## The Two Invocation Contexts

### Phase 9c: Merge-Constants Mode

```
Trigger: elfw+97 (used-set filter active -- set by --kernels-used / --variables-used loaders) is set
         AND elfw+80 (debug_flag) is clear
Target section: TEMP_MERGED_CONSTANTS
Arguments: a13=1, a14=NULL (no reloc list), a15=NULL (no overlap set)
```

In this mode, the function copies all constants into a single unified section. The `a13=1` flag causes reachable constants to be force-copied even if they could be deduplicated (the function still deduplicates when the same value appears, but it does not skip unreachable constants). After `sub_4339A0` returns:

1. The original section's symbol list is freed via `sub_464550`.
2. The original section's symbol list pointer is replaced with the temporary section's list.
3. The original section's size and alignment are updated from the temporary section.
4. The temporary section's pointers are zeroed.

### Phase 9d: OCG Constant Optimization

```
Trigger: elfw+91 (force-OCG-optimization) OR any OCG section size exceeds
         max_constant_bank_size (vtable+32)
Target section: TEMP_OCG_CONSTANTS
Arguments: a13=0, a14=&reloc_list, a15=overlap_set
```

This is the more aggressive path. The layout phase iterates all per-entry constant bank sections (`elfw+272`), and for each OCG constant section (type matching vtable+136) that has data, it calls `sub_4339A0`. The verbose output is:

```
"optimize OCG constants for %s, old size = %lld"
```

After all OCG sections have been processed through the dedup engine, the layout phase checks whether the optimization actually helped:

```c
new_size = temp_section->size;
max_bank = arch_vtable->max_constant_bank_size();

if (new_size <= max_bank) {
    // Optimization succeeded -- replace all OCG sections with merged data
    for each ocg_section in elfw+272:
        if ocg_section->type == OCG_CONSTANT_TYPE:
            free old symbol list
            copy merged symbol list (deep copy of 40-byte nodes)
            set section->size = temp_section->size
            set section->alignment = temp_section->alignment
    // verbose: "new OCG constant size = %lld"
} else {
    // Optimization did not reduce size below the limit
    free temp section's symbol list
    // verbose: "ocg const optimization didn't help so give up"
}
```

The first OCG section receives the merged list directly (move, not copy). Subsequent OCG sections receive deep copies of the merged symbol list, where each 40-byte data node is duplicated via `arena_alloc` and `_mm_loadu_si128` (SSE2 128-bit copy for the 40-byte nodes, which are copied as two 16-byte loads plus one 8-byte load).

## The "optimize space" Pre-Pass

Before the dedup engine runs, the layout phase performs a pre-pass (visible in the decompiled code around line 1757 of `sub_439830`) that iterates local and global symbol vectors to identify and remove unreferenced constants. This pre-pass produces the "optimize space in %s (%d)" verbose message.

The pre-pass walks:

1. **Positive symbol array** (`elfw+344`): For each symbol with type code `0xD` (constant data) in the source section, calls `sub_43FB70` to check reachability. Reachable constants are copied to a `TEMP_USER_DATA` section and inserted into a dedup hash table. Unreachable constants have their binding changed to LOCAL (bits masked: `sym+5 = (sym+5 & 0xFC) | 1`), effectively hiding them. Verbose: `"local constant %s at offset %lld"` or `"remove unused constant %s"`.

2. **Negative symbol array** (`elfw+352`): Same logic, but for globally-visible constants. Alignment defaults to `min(size, 8)` if no explicit alignment is recorded. Verbose: `"constant %s at offset %lld"` or `"remove unused constant %s"`.

After scanning both vectors, the pre-pass lays out the temporary section via `sub_4325A0`, then replaces the original section's contents:

```c
section_layout_engine(elfw, temp_section, reserve_null_ptr_flag);
original_section->symbol_list = temp_section->symbol_list;
original_section->size = temp_section->size;
original_section->alignment = temp_section->alignment;
temp_section->size = 0;
temp_section->symbol_list = NULL;
```

Then relocations targeting the original section are rewritten. For each relocation whose section index matches the source constant section (checked against both the `.rela` section index and the base section index), the function looks up the relocation's target offset in the dedup hash table. If found, it rewrites the relocation's offset:

```c
for each reloc in elfw+376:
    if reloc->section_idx matches source section:
        sym = hash_lookup(dedup_table, reloc->target_offset);
        if sym == NULL:
            // verbose: "no symbol for reloc section %d at offset %lld?"
        else:
            new_offset = sym->value;
            if new_offset != reloc->target_offset:
                // verbose: "change reloc in section %d, offset from %lld to %lld"
                reloc->target_offset = new_offset;
```

## Hash Table Implementation

### Creation and Mode Selection

The dedup hash tables are created by `sub_4489C0` (`htab_create`), which allocates a 112-byte header and sets up the bucket array and entry pool. The constructor accepts three arguments: a hash function pointer, a compare function pointer, and an initial capacity hint.

```c
// sub_4489C0 -- htab_create
// a1: hash_fn   -- hash function pointer
// a2: cmp_fn    -- equality comparison function pointer
// a3: capacity  -- initial capacity hint (rounded up to power of two)
htab* htab_create(hash_fn a1, cmp_fn a2, uint32_t a3) {
    htab* t = htab_alloc(a3);             // sub_448840: allocates header + arrays
    t->hash_fn_custom   = a1;             // offset +0
    t->cmp_fn_custom    = a2;             // offset +8

    // Fast-path recognition: if the function pointers match known
    // simple implementations, set a mode tag in the header to avoid
    // indirect call overhead during lookup/insert.
    if (a2 == sub_44E130 && a1 == sub_44E120) {
        // 32-bit identity hash + int equality
        // Set bits [7:4] of header+84 to 0x2 --> mode 2 (modular)
        t->flags = (t->flags & 0xF00F) | 0x0020;
    }
    if (a2 == sub_44E1E0 && a1 == sub_44E1C0) {
        // Shift-xor hash + int64 equality
        // Set bits [7:4] of header+84 to 0x1 --> mode 1 (shift-xor)
        t->flags = (t->flags & 0xF00F) | 0x0010;
    }
    return t;
}
```

The mode tag stored at `header+84` bits [7:4] controls which fast path the lookup and insert functions use, eliminating indirect function-call overhead for the two most common hash table configurations.

| Hash table | Hash function | Compare function | Mode tag | Key type |
|---|---|---|---|---|
| 32-bit values (`a4`) | `sub_44E120` (identity) | `sub_44E130` (int ==) | 2 (modular) | `uint32_t` zero-extended to 64-bit |
| 64-bit values (`a5`) | `sub_44E1C0` (shift-xor) | `sub_44E1E0` (int64 ==) | 1 (shift-xor) | `uint64_t` |

### Hash Functions

The two hash functions used by the constant dedup tables are trivially simple, optimized for speed over distribution quality -- acceptable because constant values in GPU programs have low collision rates in practice.

**32-bit identity hash** (`sub_44E120` at `0x44E120`):

```c
// The key IS the hash. For 32-bit constant values, this gives perfect
// distribution when values are distinct (common case: each float literal
// has a unique bit pattern).
uint32_t hash_identity(uint32_t key) {
    return key;
}
```

**64-bit xor-fold hash** (`sub_44E150` at `0x44E150`):

```c
// Folds a 64-bit key into 32 bits by XORing the high and low halves.
// For doubles, this combines the exponent+sign bits (high word) with
// the mantissa (low word), producing reasonable spread.
uint32_t hash_xor_fold(uint64_t key) {
    return (uint32_t)key ^ (uint32_t)(key >> 32);
}
```

**Shift-xor hash** (`sub_44E1C0` at `0x44E1C0`):

```c
// Three-way shift-xor used as the fast-path hash for mode 1 tables.
// This is the function recognized by htab_create to trigger mode 1.
// Applied to the 64-bit key; the three shifts (11, 8, 5) mix nearby
// bits to spread clustered values across buckets.
uint32_t hash_shift_xor(uint64_t key) {
    return (uint32_t)(key >> 11) ^ (uint32_t)(key >> 8) ^ (uint32_t)(key >> 5);
}
```

In practice, the 32-bit dedup table uses the identity hash with mode 2 (modular: `key & mask`), and the 64-bit dedup table uses the shift-xor hash with mode 1. Both mode 1 and mode 2 inline the hash computation into the lookup/insert hot path, avoiding the overhead of an indirect call through a function pointer.

### Header Layout (112 Bytes)

The hash table header allocated by `sub_448840` has the following layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `hash_fn` | Custom hash function pointer (mode 0 path) |
| +8 | 8 | `cmp_fn` | Custom compare function pointer (mode 0 path) |
| +16 | 8 | `hash_fn_alt` | Alternate hash function (used when `+32` flag is set) |
| +24 | 8 | `cmp_fn_alt` | Alternate compare function |
| +32 | 8 | `use_alt_fns` | Non-zero selects `+16`/`+24` over `+0`/`+8` in mode 0 |
| +40 | 4 | `bucket_mask` | `bucket_count - 1` (always a power-of-two minus one) |
| +48 | 8 | `entry_count` | Current number of entries stored |
| +56 | 4 | `xor_checksum` | Running XOR of all inserted hash values (offset +56, `*((_DWORD *)v3 + 14)`) |
| +64 | 8 | `load_limit` | Resize threshold (initially `4 << ceil_log2(capacity)`) |
| +72 | 4 | `scan_cursor` | Bitmap scan position for free-slot search |
| +76 | 4 | `entry_capacity` | Current capacity of the entry array |
| +80 | 4 | `bitmap_capacity` | Current capacity of the occupancy bitmap |
| +84 | 2 | `flags` | Bits [1:0]: entry array ownership, bits [3:2]: bitmap ownership, bits [7:4]: mode tag (0/1/2) |
| +88 | 8 | `entries` | Pointer to flat array of 16-byte entries `[key:8, value:8]` |
| +96 | 8 | `bitmap` | Pointer to uint32 occupancy bitmap (1 = slot occupied) |
| +104 | 8 | `buckets` | Pointer to array of `bucket_count` pointers to chain arrays |

### Bucket Chain Structure

Each bucket slot in the `buckets` array (at header offset +104) is a pointer to a dynamically-sized chain array. The chain array layout is:

```
+0:  uint32_t  capacity    -- max number of index entries before realloc
+4:  uint32_t  idx[0]      -- first entry index
+8:  uint32_t  idx[1]      -- second entry index
...
+N:  uint32_t  0xFFFFFFFF  -- sentinel (end of chain)
```

A NULL bucket pointer means no entries hash to that bucket. When a chain needs to grow beyond its capacity, `sub_448E70` allocates a new array of size `2 * old_capacity + 2`, copies the existing indices, and frees the old chain via `sub_431000`.

### Entry Array

The entry pool (at header offset +88) is a flat array of 16-byte slots:

```
entry[i] = {
    +0: uint64_t  key       -- the constant value (zero-extended for 32-bit)
    +8: uint64_t  value     -- pointer to the canonical symbol record
}
```

New entries are allocated by scanning the occupancy bitmap for the first zero bit (using `_BitScanForward` on the bitwise complement). The bitmap at header offset +96 tracks which entry slots are in use -- one bit per slot, packed into 32-bit words.

### Lookup Algorithm (`sub_449A80`)

The lookup function `sub_449A80` implements hash lookup with three dispatch modes selected by bits [7:4] of header+84:

```c
// sub_449A80 -- htab_lookup
// a1: htab*        -- hash table header
// a2: uint64_t     -- key to look up
// Returns: value (symbol*) if found, 0 if not found
symbol* htab_lookup(htab* t, uint64_t key) {
    uint8_t mode = (t->flags >> 4) & 0xF;
    uint32_t bucket_idx;

    switch (mode) {
    case 1:  // Shift-xor fast path (64-bit dedup table)
        bucket_idx = t->bucket_mask &
                     ((uint32_t)(key >> 11) ^
                      (uint32_t)(key >> 8)  ^
                      (uint32_t)(key >> 5));
        break;
    case 2:  // Modular fast path (32-bit dedup table)
        bucket_idx = t->bucket_mask & (uint32_t)key;
        break;
    default: // Mode 0: indirect call through function pointer
        uint32_t h;
        if (t->use_alt_fns)
            h = t->hash_fn_alt(key);
        else
            h = t->hash_fn(key);
        bucket_idx = t->bucket_mask & h;
        break;
    }

    uint32_t* chain = t->buckets[bucket_idx];
    if (!chain)
        return NULL;

    // Walk the chain; chain[0] is capacity, entries start at chain[1]
    uint32_t* p = chain + 1;        // skip capacity field
    while (*p != 0xFFFFFFFF) {
        entry_t* e = &t->entries[*p];
        if (mode == 0) {
            // Use custom compare function
            if (t->use_alt_fns ? t->cmp_fn_alt(e->key, key)
                               : t->cmp_fn(e->key, key))
                return e->value;
        } else {
            // Modes 1 and 2: direct integer equality
            if (e->key == key)
                return e->value;
        }
        p++;
    }
    return NULL;
}
```

### Insert and Resize (`sub_448E70`)

The insert function `sub_448E70` first attempts a lookup; if the key exists, it replaces the value and returns the old value. If the key is new:

1. **Find free entry slot**: Scans the occupancy bitmap starting at `scan_cursor` for the first word with a zero bit. Uses `_BitScanForward(~bitmap[i])` to find the exact bit position. If all existing bitmap words are full, grows the bitmap by doubling.

2. **Grow entry array if needed**: If the free slot index exceeds `entry_capacity`, doubles the entry array via realloc (or arena alloc + memcpy if arena-managed).

3. **Write entry**: Sets `entries[slot].key = key` and `entries[slot].value = value`.

4. **Append to chain**: Finds the bucket chain for the key's hash. If the chain exists, appends the slot index before the sentinel. If the chain's index count exceeds its capacity, reallocates at `2 * old_capacity + 2`. If no chain exists, creates a new 12-byte chain `[capacity=1, slot_idx, 0xFFFFFFFF]`.

5. **Update occupancy**: Sets the bit in the bitmap, XORs the hash into the running checksum, increments `entry_count`.

6. **Resize check**: If `entry_count` exceeds `load_limit`, triggers a full rehash:
   - Doubles `bucket_mask` to `2 * old_mask + 1` (doubles bucket count).
   - Doubles `load_limit`.
   - Allocates a new bucket pointer array, zeroes it.
   - Frees all old chain arrays via `sub_431000`.
   - Walks the occupancy bitmap: for each occupied slot, recomputes the hash (via indirect call through `hash_fn`), and inserts the slot index into the new chain for the new bucket. This rehash loop uses the same chain-append logic with capacity tracking.

The rehash ensures amortized O(1) insert. The load factor threshold is `4 << ceil_log2(initial_capacity)` initially, doubling on each rehash.

### Dedup Table Sizing for Constants

Both dedup tables are created with an initial capacity hint of 256 (passed from the layout phase). After `sub_45CB00` (`ceil_log2`) and `sub_448840`:

- `ceil_log2(256)` = 8
- `bucket_count` = `1 << 8` = 256
- `bucket_mask` = 255
- `load_limit` = `4 << 8` = 1024

So each table starts with 256 buckets and will not rehash until it holds more than 1024 entries. For typical CUDA programs with hundreds of distinct constants, no rehash occurs.

## CLI Option Interaction

The two relevant CLI options are registered in `sub_427AE0` (option parsing):

```
--no-opt                  "Turn off linker optimization of data resources"
                          Type: bool, stored in byte_2A5F2A9

--optimize-data-layout    "Force linker optimization of data resources"
                          Type: bool, stored in byte_2A5F2A8
```

These are mutually exclusive. If both are specified, the option parser emits a fatal error via `sub_467460` with error descriptor `unk_2A5B650`, passing `"-no-opt"` and `"-optimize-data-layout"` as conflicting option names.

The flags propagate to the elfw object during construction:

| Global | Elfw offset | Effect |
|---|---|---|
| `byte_2A5F2A9` | `+90` | no-opt: disables shared memory optimization, constant dedup, forces simple linear layout |
| `byte_2A5F2A8` | `+91` | optimize-data-layout: forces the "optimize space" pre-pass and OCG constant optimization even when sections are within the bank size limit |

When `--no-opt` is active, the layout phase:
- Uses simple linear layout for shared memory (skips interference graph optimization).
- Skips constant deduplication entirely.
- Uses direct offset assignment instead of sorted layout in `sub_4325A0`.
- Produces the assertion `"should only reach here with no opt"` if a zero-alignment symbol is encountered.

When `--optimize-data-layout` is active, the layout phase:
- Forces the "optimize space" pre-pass (`elfw+91` check at line 1757).
- Forces OCG constant optimization (`elfw+91` OR size-exceeded check).
- Enables dead constant elimination via `sub_43FB70` reachability.

When neither flag is set (the default), optimization triggers automatically only when an OCG constant section exceeds the architecture's bank size limit.

## Verbose Trace Strings

All strings are gated by `(*(_BYTE*)(elfw + 64) & 2) != 0` (verbose layout flag, bit 1):

| String | Context | Meaning |
|---|---|---|
| `"optimize space in %s (%d)"` | Pre-pass entry | Starting constant space optimization for a section |
| `"local constant %s at offset %lld"` | Pre-pass local scan | Copying reachable local constant to temp section |
| `"constant %s at offset %lld"` | Pre-pass global scan | Copying reachable global constant to temp section |
| `"remove unused constant %s"` | Pre-pass dead elim | Demoting unreachable constant to LOCAL binding |
| `"no symbol for reloc section %d at offset %lld?"` | Reloc rewrite | No dedup-table entry for a relocation target |
| `"change reloc in section %d, offset from %lld to %lld"` | Reloc rewrite | Updating relocation offset after dedup |
| `"layout and merge section %s"` | Phase 9c entry | Starting merge-constants mode dedup |
| `"found duplicate value 0x%x, alias %s to %s"` | 32-bit dedup | Aliasing a 32-bit constant to existing copy |
| `"found duplicate 64bit value 0x%llx, alias %s to %s"` | 64-bit dedup | Aliasing a 64-bit constant to existing copy |
| `"found duplicate %d byte value, alias %s to %s"` | Large-value dedup | Aliasing a 12..64-byte constant (from `sub_433870`) |
| `"optimize ocg constant reloc offset from %lld to %lld"` | OCG reloc patch | Rewriting relocation after constant placement |
| `"optimize OCG constants for %s, old size = %lld"` | Phase 9d entry | Starting OCG constant optimization for a section |
| `"new OCG constant size = %lld"` | Phase 9d result | Total size after OCG deduplication |
| `"ocg const optimization didn't help so give up"` | Phase 9d abandon | Deduped size still exceeds bank limit |

## Worked Example

Consider two translation units that each define a constant:

```
// TU A: .nv.constant0.kernelA
float c1 = 3.14f;     // 4 bytes, value 0x4048F5C3
float c2 = 2.71f;     // 4 bytes, value 0x402D70A4

// TU B: .nv.constant0.kernelB
float c3 = 3.14f;     // 4 bytes, value 0x4048F5C3  (duplicate of c1)
double c4 = 1.0;      // 8 bytes, value 0x3FF0000000000000
```

After merge, the per-entry constant sections contain:

```
kernelA constants: c1 (0x4048F5C3), c2 (0x402D70A4)  -- 8 bytes total
kernelB constants: c3 (0x4048F5C3), c4 (0x3FF0...)   -- 12 bytes total
```

When `sub_4339A0` processes these:

1. **c1** (value 0x4048F5C3): Not in 32-bit hash table. Insert at offset 0. Hash table: `{0x4048F5C3 -> c1}`.
2. **c2** (value 0x402D70A4): Not in 32-bit hash table. Insert at offset 4. Hash table: `{0x4048F5C3 -> c1, 0x402D70A4 -> c2}`.
3. **c3** (value 0x4048F5C3): Found in hash table -- duplicate of c1. Alias: `c3.value = c1.value = 0`. No data copied. Verbose: `"found duplicate value 0x4048f5c3, alias c3 to c1"`.
4. **c4** (value 0x3FF0...): Not in 64-bit hash table. Insert at offset 8 (aligned to 8). Hash table updated.

Result: merged section is 16 bytes instead of 20, and c3 shares c1's storage.

## Key Implementation Details

**Zero-value special casing.** The hash tables use the constant value as the key, which means a zero value would map to hash bucket 0 and could collide with the "not found" sentinel. The code explicitly checks `val != 0` before looking up in the hash table. Zero-valued constants are handled separately: they are either always copied (when `a15 == NULL`) or tracked per-entry via the overlap set (when `a15 != NULL`).

**Alignment preservation.** When a constant is aliased, the duplicate symbol receives the original's offset but its alignment requirement is not checked against the target. This is safe because the first occurrence was placed with at least its own alignment, and all subsequent duplicates have the same size (and therefore the same natural alignment).

**Deep copy for multi-entry OCG.** In phase 9d, the first OCG section receives the merged symbol list by direct pointer assignment (move semantics). All subsequent OCG sections receive deep copies, where each 40-byte data node is cloned via SSE2 memcpy (`_mm_loadu_si128` x2 + 8-byte load). This ensures each per-entry section has independent node memory that can be freed separately.

**Relocation list surgery.** The OCG relocation rewriting removes matched relocations from the singly-linked list in-place. The predecessor pointer `v42`/`v52`/`v66`/`v72` (depending on which code path) tracks the previous node for unlinking. If the match is the first node, `*a14` (the list head) is updated directly; otherwise, the predecessor's next pointer skips the matched node.

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_4339A0` at 0x4339A0, 15-parameter signature | HIGH | Decompiled `sub_4339A0`: function at 0x4339a0 with parameters `(a1..a15)` including `char a13`, `_QWORD *a14`, `__int64 a15` exactly matching wiki |
| Hash table create `sub_4489C0` mode detection for 32-bit and 64-bit | HIGH | Decompiled `sub_4489C0`: compares `sub_44E130`/`sub_44E120` for mode 2 and `sub_44E1E0`/`sub_44E1C0` for mode 1; bitmask `0xF00F \| 0x20` / `0x10` confirmed |
| Identity hash `sub_44E120` returns key unchanged | HIGH | Decompiled: `return a1;` |
| XOR-fold hash `sub_44E150` returns `lo32 ^ hi32` | HIGH | Decompiled: `return (unsigned int)a1 ^ HIDWORD(a1);` |
| Shift-xor hash `sub_44E1C0` returns `(a>>11)^(a>>8)^(a>>5)` | HIGH | Decompiled: exact expression confirmed |
| `sub_433870` (dedup\_memcmp) 8-parameter function matching wiki signature | HIGH | Decompiled: `sub_433870(a1, a2, a3, a4, a5, a6, a7, a8)` with memcmp at line 33, alignment check `*(_QWORD *)(v13 + 16) == v10` at line 33 |
| `sub_433870` emits `"found duplicate %d byte value, alias %s to %s"` | HIGH | Decompiled line 42: `fprintf(stderr, "found duplicate %d byte value, alias %s to %s\n", ...)` and string at addr 0x1d38858 in `nvlink_strings.json` |
| `sub_433870` checks alignment before memcmp | HIGH | Decompiled line 33: `*(_QWORD *)(v13 + 16) == v10` (alignment at data\_node+16) precedes `memcmp` |
| `sub_433870` uses `sub_4644C0` for list prepend on miss | HIGH | Decompiled line 54: `return sub_4644C0(a4, a2);` |
| `sub_433760` (section\_data\_copy) allocates 40-byte node | HIGH | Decompiled: `sub_4307C0(v10, 40)` at line 28 |
| `sub_433760` updates `sh_addralign` at section offset +48 | HIGH | Decompiled line 25: `if ( v7 > *(_QWORD *)(result + 48) ) *(_QWORD *)(result + 48) = v7;` |
| `sub_433760` emits `"tail data node not found"` assertion | HIGH | Decompiled line 53: string literal confirmed; also at addr 0x1d38839 in `nvlink_strings.json` |
| `sub_433760` computes aligned offset `v16 + v7 - v16 % v7` | HIGH | Decompiled lines 37-40: `v17 = v16 % v7; v18 = v16 + v7 - v16 % v7; if (v17) v16 = v18;` |
| Verbose gate at `elfw+64, bit 1` | HIGH | Decompiled `sub_433870` line 41: `if ( (*(_BYTE *)(a1 + 64) & 2) != 0 )` |
| `"found duplicate value 0x%x, alias %s to %s"` string | HIGH | String at addr 0x1d38888 in `nvlink_strings.json` |
| `"found duplicate 64bit value 0x%llx, alias %s to %s"` string | HIGH | String at addr 0x1d388f0 in `nvlink_strings.json` |
| `"optimize ocg constant reloc offset from %lld to %lld"` string | HIGH | String at addr 0x1d388b8 in `nvlink_strings.json` |
| `"optimize OCG constants for %s, old size = %lld"` string | HIGH | String at addr 0x1d39028 in `nvlink_strings.json` |
| `"optimize space in %s (%d)"` string | HIGH | String at addr 0x1d38ccf in `nvlink_strings.json` |
| `"remove unused constant %s"` string | HIGH | String at addr 0x1d38cf9 in `nvlink_strings.json` |
| `"no symbol for reloc section %d at offset %lld?"` string | HIGH | String at addr 0x1d38fc0 in `nvlink_strings.json` |
| CLI flags `--no-opt` at `byte_2A5F2A9` and `--optimize-data-layout` at `byte_2A5F2A8` | MEDIUM | Inferred from option parsing patterns in `sub_427AE0`; flag offsets reconstructed from layout phase conditional checks |
| Size-class dispatch: 4, 8, 12, 16, 20, 24, 32, 48, 64 bytes | MEDIUM | Reconstructed from `sub_4339A0` switch-case structure matching parameters `a6`..`a12` to specific sizes; confirmed via parameter count and linked-list bucket pattern |
| OCG relocation rewriting appears 4 times in `sub_4339A0` | MEDIUM | Inferred from decompiled code structure showing repeated reloc-walk patterns with predecessor tracking; exact count reconstructed from block analysis |
| Phase 9c uses `a13=1`, Phase 9d uses `a13=0` with non-null `a15` | MEDIUM | Reconstructed from `sub_439830` call sites into `sub_4339A0`; parameter passing patterns match documented behavior |

## Related Pages

- [Layout Phase](../pipeline/layout.md) -- parent phase (sub_439830) containing all 10 layout sub-phases
- [Section Merging](section-merging.md) -- how sections from multiple TUs are combined before dedup
- [Dead Code Elimination](dead-code-elimination.md) -- callgraph-based reachability that feeds into constant liveness
- [Hash Tables](hash-tables.md) -- general hash table infrastructure used by the dedup engine
- [Symbol Resolution](symbol-resolution.md) -- symbol records whose offsets are modified during constant dedup
- [Bindless Relocations](bindless-relocations.md) -- bindless descriptor sections in the same constant banks optimized here
- [R\_CUDA Relocations](r-cuda-relocations.md) -- R\_CUDA\_CONST\_FIELD relocations rewritten during OCG dedup
