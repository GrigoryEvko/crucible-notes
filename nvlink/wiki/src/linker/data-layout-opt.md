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
| **CLI enable** | `--optimize-data-layout` (stored in `byte_2A5F2A8`, maps to elfw+83) |
| **CLI disable** | `--no-opt` (stored in `byte_2A5F2A9`, maps to elfw+90) |
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

The reachability check `sub_43FB70` determines whether a constant symbol is referenced by any live entry-point function. It checks the EWP mode flag (`elfw+80`) and the merge-constants flag (`elfw+97`). If neither is set, it queries entry-function reachability via the callgraph: for global symbols (binding bit 4 set), it checks against the function set at `elfw+536`; for local symbols, against `elfw+544`. Both queries use `sub_43EB40`, a string-based set membership test on the symbol name.

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

For values of sizes 12, 16, 20, 24, 32, 48, and 64 bytes, the function delegates to `sub_433870` (`dedup_memcmp`). Each size class has its own linked list (parameters `a6` through `a12`). The linked list stores previously seen values, and deduplication uses byte-level `memcmp`:

```c
// sub_433870 -- dedup_memcmp
// a1: elfw*, a2: list**, a3: size, a4: data_node,
// a5: sym_value_out, a6: sym_name_out, a7: section_size_ptr, a8: section_idx

node = *a2;  // head of seen-values list for this size class
while (node != NULL) {
    existing = node->payload;
    if (existing->alignment == data_node->alignment &&
        memcmp(data_node->data, existing->data, size) == 0) {
        // Match found
        sym = get_symbol(elfw, existing->sym_index);
        // verbose: "found duplicate %d byte value, alias %s to %s"
        *sym_value_out = sym->value;
        return;
    }
    node = node->next;
}
// No match -- copy data to target section
aligned_offset = align_up(*a7, data_node->alignment)
*sym_value_out = aligned_offset
section_data_copy(elfw, section_idx, data, alignment, size)
list_prepend(data_node, a2);
```

The linked-list approach for large values (rather than a hash table) is reasonable because these sizes are rare in practice -- most GPU constants are 4 or 8 bytes (scalar floats, doubles, pointers). The `memcmp` comparison is O(n) in the number of previously seen values for a given size class, but with the small typical count, this is faster than computing a hash over the full value.

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

## The Two Invocation Contexts

### Phase 9c: Merge-Constants Mode

```
Trigger: elfw+97 (merge-constants flag) is set
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

1. **Local symbols** (`elfw+344`): For each symbol with type code `0xD` (constant data) in the source section, calls `sub_43FB70` to check reachability. Reachable constants are copied to a `TEMP_USER_DATA` section and inserted into a dedup hash table. Unreachable constants have their binding changed to LOCAL (bits masked: `sym+5 = (sym+5 & 0xFC) | 1`), effectively hiding them. Verbose: `"local constant %s at offset %lld"` or `"remove unused constant %s"`.

2. **Global symbols** (`elfw+352`): Same logic, but for globally-visible constants. Alignment defaults to `min(size, 8)` if no explicit alignment is recorded. Verbose: `"constant %s at offset %lld"` or `"remove unused constant %s"`.

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

The dedup hash tables are created by `sub_4489C0` with architecture-specific hash and comparison functions:

| Hash table | Hash function | Compare function | Bucket count | Key type |
|---|---|---|---|---|
| 32-bit values (`a4`) | `sub_44E120` | `sub_44E130` | 256 | `uint32_t` zero-extended to 64-bit |
| 64-bit values (`a5`) | `sub_44E150` | `sub_44E160` | 256 | `uint64_t` |

The lookup function `sub_449A80` implements open-addressing hash lookup with three dispatch modes (selected by bits 4..7 of the table header at offset +84):

- **Mode 0**: Uses the table's custom hash function (at offset +0 or +16 depending on a flag at offset +32) to compute the bucket index, then walks a linked list of entries checking equality via the custom compare function.
- **Mode 1**: Hash is computed as `(key >> 11) ^ (key >> 8) ^ (key >> 5)`, masked to bucket count. This is the fast path for small integer keys (used for 32-bit constant values).
- **Mode 2**: Hash is `key & mask` (simple modular hash for 64-bit keys).

Each bucket contains a chain of indices into a flat entry array at table offset +88. Each entry is 16 bytes: the key at offset +0 and the value (symbol pointer) at offset +8. The sentinel value `0xFFFFFFFF` marks the end of a chain.

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
| `byte_2A5F2A8` | `+83` | optimize-data-layout: forces the "optimize space" pre-pass and OCG constant optimization even when sections are within the bank size limit |

When `--no-opt` is active, the layout phase:
- Uses simple linear layout for shared memory (skips interference graph optimization).
- Skips constant deduplication entirely.
- Uses direct offset assignment instead of sorted layout in `sub_4325A0`.
- Produces the assertion `"should only reach here with no opt"` if a zero-alignment symbol is encountered.

When `--optimize-data-layout` is active, the layout phase:
- Forces the "optimize space" pre-pass (`elfw+83` check at line 1757).
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

## Related Pages

- [Layout Phase](../pipeline/layout.md) -- parent phase (sub_439830) containing all 10 layout sub-phases
- [Section Merging](section-merging.md) -- how sections from multiple TUs are combined before dedup
- [Dead Code Elimination](dead-code-elimination.md) -- callgraph-based reachability that feeds into constant liveness
- [Hash Tables](hash-tables.md) -- general hash table infrastructure used by the dedup engine
