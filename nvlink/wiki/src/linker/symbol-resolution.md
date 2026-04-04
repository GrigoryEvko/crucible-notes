# Symbol Resolution

nvlink manages symbols through a dual-array storage scheme where positive and negative indices address separate arrays, a name-based hash map for O(1) lookup, a virtual section index mechanism for ELFs with more than 65,279 sections, and an old-to-new index remapping system that keeps references stable across dead code elimination. This page documents the internal machinery at the function level, reconstructed from the decompiled binary.

## ELF Writer Symbol Storage

The ELF writer object (the "elfw" context, first argument `a1` in most functions below) stores symbols in two separate dynamic arrays:

| Offset in elfw | Type | Role |
|---|---|---|
| `+344` | `dyn_array*` | **Positive symbol array** -- local symbols, section symbols, and non-global definitions. Indexed by non-negative indices (`>= 0`). |
| `+352` | `dyn_array*` | **Negative symbol array** -- global and weak symbols. Indexed by negative indices (stored as `-index`). |
| `+288` | `hash_map*` | **Name lookup hash map** -- maps symbol name string to signed symbol index. |
| `+296` | `hash_map*` | **Section name lookup hash map** -- maps section name string to signed section index. |
| `+304` | `uint32` | **Name counter** -- total distinct names registered in the hash map. |
| `+360` | `dyn_array*` | **Section descriptor array** -- section records indexed by section index. |
| `+368` | `uint32*` | **Virtual section indirection table** -- maps virtual section ordinal to real section index. |
| `+456` | `uint32*` | **Positive remap table** -- old positive symbol index to new positive symbol index. |
| `+464` | `uint32*` | **Negative remap table** -- old negative symbol index (absolute value) to new symbol index. |
| `+592` | `dyn_array*` | **Extended section index positive array** -- for sections with index > 0xFEFF. |
| `+600` | `dyn_array*` | **Extended section index negative array** -- corresponding negative side. |
| `+624` | `uint32` | **Warning level** -- passed to `sub_42F850` for STO_CUDA_OBSCURE diagnostics. |

Each symbol record is a 48-byte structure (allocated via `sub_4307C0` with size 48):

```
struct symbol_record {              // 48 bytes
    uint32_t  st_name;              // [0:4]   string table offset
    uint8_t   st_info;              // [4]     high nibble = binding, low nibble = type
    uint8_t   st_other;             // [5]     visibility / other
    uint16_t  st_shndx;            // [6:8]   section index (0xFFFF = virtual/extended)
    uint64_t  st_value;             // [8:16]  symbol value
    uint64_t  st_size;              // [16:24] symbol size
    int32_t   sym_index;            // [24:28] signed index into pos/neg array
    int32_t   func_ordinal;         // [28:32] function ordinal (for callgraph)
    void*     name_str;             // [32:40] pointer to name string
    uint32_t  flags;                // [40:44] internal flags
};
```

The `st_info` byte encodes binding in the high nibble and type in the low nibble, following ELF conventions:

| Binding (high nibble) | Value | Meaning |
|---|---|---|
| `STB_LOCAL` | 0 | File-local symbol |
| `STB_GLOBAL` | 1 | Global symbol (strong definition) |
| `STB_WEAK` | 2 | Weak definition |

| Type (low nibble) | Value | Meaning |
|---|---|---|
| `STT_NOTYPE` | 0 | No type |
| `STT_OBJECT` | 1 | Data object |
| `STT_FUNC` | 2 | Function (entry point / device kernel) |
| `STT_SECTION` | 3 | Section symbol |

## Positive/Negative Index Scheme

The central dispatch function `sub_440590` illustrates the index convention:

```c
// sub_440590 -- resolve symbol index to symbol record pointer
int64_t elfw_get_symbol(elfw* ctx, int index) {
    if (index < 0)
        return dyn_array_get(ctx->neg_symbols, -index);   // offset +352
    else
        return dyn_array_get(ctx->pos_symbols, index);     // offset +344
}
```

The underlying `dyn_array_get` (`sub_464DB0`) is a bounds-checked array access:

```c
int64_t dyn_array_get(dyn_array* arr, uint64_t index) {
    if (index >= arr->count)
        return 0;                        // NULL -- out of bounds
    return arr->data[index];             // 8 bytes per slot
}
```

**Why two arrays?** In ELF, the symbol table is conventionally sorted with local symbols first, then global symbols. nvlink mirrors this by giving local/section symbols non-negative indices and global/weak symbols negative indices. During the add-symbol operation (`sub_440BE0`, `sub_442CA0`), the sign of the index determines which array receives the new entry:

```c
if (binding == STB_GLOBAL) {
    int slot = dyn_array_count(ctx->neg_symbols);     // sub_464BB0
    sym->sym_index = -slot;                            // store as negative
    dyn_array_push(sym, ctx->neg_symbols);             // sub_464C30
} else {
    int slot = dyn_array_count(ctx->pos_symbols);
    sym->sym_index = slot;                             // store as positive
    dyn_array_push(sym, ctx->pos_symbols);
}
```

`sub_464BB0` returns `*(qword*)(arr + 8)` -- the element count. `sub_464C30` appends an element, growing the backing allocation (doubling capacity) when full.

## Name Lookup Hash Map

Every symbol and section name is registered in a hash map at elfw offset `+288`. The lookup function `sub_4411B0` wraps the generic hash map probe:

```c
// sub_4411B0 -- find symbol index by name
int elfw_find_symbol_by_name(elfw* ctx, const char* name) {
    uint32_t* slot = hash_map_lookup(ctx->name_map, name);  // sub_449A80
    if (slot)
        return *slot;          // signed symbol index (positive or negative)
    return 0;                  // not found
}
```

There is a parallel function `sub_4411D0` for the section name hash map at offset `+296`:

```c
// sub_4411D0 -- find section index by name
int elfw_find_section_by_name(elfw* ctx, const char* name) {
    uint32_t* slot = hash_map_lookup(ctx->section_name_map, name);  // +296
    if (slot)
        return *slot;
    return 0;
}
```

The hash map implementation (`sub_449A80`) supports three hashing modes selected by a type field at `map+84`:

| Mode | Hash function | Key comparison |
|---|---|---|
| 0 | Custom function pointer at `map+0` | Custom comparator at `map+8` (or `map+16`/`map+24` when context present at `map+32`) |
| 1 | `(key >> 11) ^ (key >> 8) ^ (key >> 5)` | Direct integer equality |
| 2 | `key & mask` | Direct integer equality |

For symbol/section name lookups (mode 0), the hash map uses string-based hash and comparison functions. The bucket array is at `map+104`, with `map+40` holding the mask (`bucket_count - 1`). Each bucket is a pointer to an array of 32-bit entry indices terminated by `0xFFFFFFFF` (-1 sentinel). The entry array at `map+88` stores 16-byte key-value pairs: 8 bytes for the key (string pointer or integer) and 8 bytes for the value.

Insertion into the hash map is performed by `sub_448E70` (a 611-line function), which handles bucket allocation, rehashing on high load, and collision chain management.

## Section Index Resolution

ELF section indices are 16-bit values (`st_shndx`). When an ELF has more than 65,279 (0xFEFF) sections, indices cannot fit in the standard field. nvlink uses the marker value `0xFFFF` as a "virtual" indicator, meaning the real section index must be looked up through an indirection layer.

### The 0xFFFF Virtual Marker

`sub_440350` implements section index resolution with indirection:

```c
// sub_440350 -- resolve section index for a symbol
uint16_t resolve_section_index(elfw* ctx, symbol_record* sym, ...) {
    uint16_t shndx = sym->st_shndx;       // offset +6 in the symbol record
    
    if (shndx != 0xFFFF)
        return shndx;                       // fast path: direct section index
    
    // Virtual section: resolve through extended tables
    int32_t ext_idx = sym->sym_index;       // offset +24 in symbol record
    
    if (ctx->ext_neg_array) {               // offset +600
        if (ext_idx >= 0)
            return dyn_array_get(ctx->ext_pos_array, ext_idx);   // +592
        else
            return dyn_array_get(ctx->ext_neg_array, -ext_idx);  // +600
    }
    
    // Fallback: remap through old-to-new tables
    uint32_t* pos_remap = ctx->pos_remap;   // offset +456
    uint32_t* neg_remap = ctx->neg_remap;   // offset +464
    
    if (!pos_remap || ext_idx == 0) {
        ext_idx = 0;                         // unmapped: treat as SHN_UNDEF
    } else if (ext_idx <= 0) {
        uint32_t abs_idx = -ext_idx;
        ext_idx = neg_remap[abs_idx];
        if (ext_idx == 0)
            fatal("reference to deleted symbol");
    } else {
        uint32_t new_idx = pos_remap[ext_idx];
        if (new_idx == 0)
            fatal("reference to deleted symbol");
        ext_idx = new_idx;
    }
    
    return dyn_array_get(ctx->ext_pos_array, ext_idx);  // +592 via offset +74*8
}
```

The two-level resolution works as follows:

1. **Direct path**: If `st_shndx` is a standard value (0 through 0xFEFE, or the special value 0xFFF2 for `SHN_COMMON`), return it immediately.

2. **Extended path with indirection arrays**: If the extended index arrays at `+592`/`+600` exist, use the symbol's `sym_index` sign to select the correct array and retrieve the real section index.

3. **Extended path with remap tables**: If the indirection arrays do not exist but remap tables at `+456`/`+464` do, first translate the old index through the remap table, then look up the result in the section array at `+592`. A zero result from the remap table triggers a fatal `"reference to deleted symbol"` error.

### Extended Section Index Tables

When a symbol is added whose resolved section index exceeds `0xFEFF` (65,279), `sub_440BE0` and `sub_442CA0` create the extended section index arrays on demand:

```c
if (section_index > 0xFEFF || section_index == 0xFFF2) {
    sym->st_shndx = 0xFFFF;                    // mark as virtual
    
    if (!ctx->ext_pos_array) {                   // first time: allocate both arrays
        ctx->ext_pos_array = dyn_array_create(0x10000);   // sub_464AE0
        ctx->ext_neg_array = dyn_array_create(0x10000);
    }
    
    // Store the real section index, keyed by the symbol's pos/neg index
    if (sym->sym_index < 0)
        dyn_array_set(ctx->ext_neg_array, -sym->sym_index, section_index);
    else
        dyn_array_set(ctx->ext_pos_array, sym->sym_index, section_index);
}
```

`sub_464AE0` allocates a 24-byte dynamic array header (`data`, `count`, `capacity`) with an initial capacity of 65,536 (0x10000) slots. `sub_464D10` performs a set-at-index operation, growing the array if needed (doubling capacity, zero-filling new slots).

The special value `0xFFF2` (`SHN_COMMON`) is also handled through this extended path -- common symbols bypass the virtual marker and are stored alongside oversized section indices.

## Symbol Index Remapping

After dead code elimination, symbols may be removed from the output. Rather than compacting the arrays immediately (which would invalidate every cross-reference), nvlink builds remap tables that translate old indices to new indices. `sub_444720` performs this translation:

```c
// sub_444720 -- remap old symbol index to new symbol index
int remap_symbol_index(elfw* ctx, int old_index, ...) {
    uint32_t* pos_remap = *(uint32_t**)(ctx + 456);
    
    if (!pos_remap || old_index == 0)
        return 0;                        // no remap table, or index 0 is always 0
    
    if (old_index <= 0) {
        // Negative index: use negative remap table at +464
        uint32_t abs_idx = -old_index;
        uint32_t new_idx = ctx->neg_remap[abs_idx];     // *(uint32*)(+464 + 4*abs)
        if (new_idx == 0)
            fatal("reference to deleted symbol");
        return new_idx;
    } else {
        // Positive index: use positive remap table at +456
        uint32_t new_idx = pos_remap[old_index];         // *(uint32*)(+456 + 4*idx)
        if (new_idx == 0)
            fatal("reference to deleted symbol");
        return new_idx;
    }
}
```

The remap tables are `uint32_t` arrays (4 bytes per entry), indexed by the absolute value of the old symbol index. A zero entry means the symbol was deleted -- any attempt to reference it triggers the `"reference to deleted symbol"` fatal error via `sub_467460`.

This function is called from the relocation engine (`sub_469D60`), from section index resolution (`sub_440350`), and from the extended symbol resolution function (`sub_4411F0`). The pattern of checking the remap table, falling through to the `"reference to deleted symbol"` error, and then re-reading the table entry (as a defensive measure against the fatal handler returning) is repeated verbatim across all call sites.

## Global Symbol Addition

`sub_440BE0` adds a symbol to the output ELF with full duplicate detection, hash map registration, and callgraph integration. Its signature:

```c
int elfw_add_symbol(
    elfw*       ctx,           // a1: ELF writer context
    const char* name,          // a2: symbol name string
    uint8_t     sym_type,      // a3: STT_* type (0=notype, 1=object, 2=func, 3=section)
    uint8_t     binding,       // a4: STB_* binding (0=local, 1=global, 2=weak)
    uint8_t     visibility,    // a5: STV_* visibility
    int         section_index, // a6: signed section index (positive or negative)
    uint64_t    value,         // a7: symbol value (address/offset)
    int         func_ordinal,  // a8: function ordinal (or 0 for non-functions)
    uint64_t    size            // a9: symbol size
);
```

The function proceeds through these stages:

### 1. Section Index Resolution

The section index argument (`a6`) is resolved through `sub_440590` to obtain the section record pointer. If the section record's `st_shndx` field is `0xFFFF`, the extended section indirection path kicks in (same three-tier resolution as in `sub_440350`).

### 2. Duplicate Detection via Hash Map

The name is looked up in the hash map at `ctx+288`:

```c
int existing_idx = hash_map_lookup(ctx->name_map, name);    // sub_449A80
```

If `existing_idx` is nonzero, the symbol already exists. The function retrieves the existing record via `sub_440590` (following the positive/negative convention) and checks for conflicts:

- **Global-on-global conflict**: If the existing symbol has `binding == STB_GLOBAL` (bit pattern `(st_info >> 4) == 1`) and the new symbol also has `binding == STB_GLOBAL`, the diagnostic `"adding global symbols of same name"` is triggered via `sub_467460`. This is an internal assertion, not a user-facing error -- the multiple-definition user error is handled elsewhere (in `merge_elf`).

- **Local replacing existing**: If `binding == STB_LOCAL` (0), the existing record's name pointer and value are copied into the new record. If the existing value pointer is NULL, the new record is treated as a fresh insertion.

- **Global or weak**: The new record inherits the name pointer from the existing record and overwrites the hash map entry.

### 3. Hash Map Registration

If the symbol is new (no existing entry found, or the existing entry had a NULL value), a 12-byte hash map entry node is allocated:

```c
struct name_entry {
    uint64_t padding;     // 8 bytes (unused, zeroed)
    uint32_t sym_index;   // 4 bytes: signed symbol index
};
```

The name string is copied into arena memory (`strcpy` into freshly allocated buffer), and the entry is inserted into the hash map via `sub_448E70(ctx->name_map, name_copy, &entry)`.

### 4. Array Insertion

The symbol record (48 bytes, zero-initialized) is populated:

```c
sym->st_info  = (binding << 4) | (sym_type & 0xF);     // byte at +4
sym->st_other = visibility;                              // byte at +5
sym->st_value = value;                                   // qword at +8
sym->st_size  = size;                                    // qword at +16
```

For globals (`binding == 1`), the symbol is appended to the negative array at `+352`, and its `sym_index` field is set to `-count` (negative of the new array length). For all others, it goes into the positive array at `+344` with a positive index.

### 5. Extended Section Index Handling

If the resolved section index exceeds `0xFEFF` (but is not the special `0xFFF2` common marker), the symbol's `st_shndx` is set to `0xFFFF` and the real index is stored in the extended arrays at `+592`/`+600`, keyed by the symbol's signed index. The arrays are allocated on first use with an initial capacity of 65,536 entries.

### 6. STO_CUDA_OBSCURE Check

After insertion, `sub_42F850` is called with the warning level and the raw symbol value. If the value has bit 43 set (`& 0x80000000000`) and the warning level is 4 or below, a diagnostic is emitted with the string `"STO_CUDA_OBSCURE"`, flagging symbols with CUDA-specific non-standard visibility attributes.

### 7. Callgraph Registration

If the symbol type is `STT_FUNC` (2), the function ordinal counter at `ctx+416` is incremented, stored in the symbol record at offset `+28`, and the symbol is registered in the callgraph via `sub_44B940(ctx, sym_index)`. This is how entry points and device functions enter the dead code elimination graph.

## The `elfw_add_symbol` Variant: `sub_442CA0`

`sub_442CA0` is a specialized version that adds a function symbol specifically. It hardcodes `sym_type = STT_FUNC` (type byte = `0x02`), always increments the function ordinal, always calls `sub_44B940` for callgraph registration, and additionally calls `sub_442820` (`elfw_merge_symbols`) to handle unified function table (UFT) stub merging. It takes fewer parameters:

```c
int elfw_add_function_symbol(
    elfw*       ctx,           // a1
    const char* name,          // a2
    uint8_t     binding,       // a3: 0=local, 1=global
    uint8_t     visibility     // a4
);
```

The internal logic follows the same pattern: hash map lookup, duplicate detection with `"adding global symbols of same name"`, array insertion into positive or negative array, extended section index handling, and callgraph registration. The key difference is the automatic call to `sub_442820` which handles `__cuda_uf_stub_*` symbols and `.nv.uft` sections for CUDA's unified function table mechanism.

## Extended Symbol Resolution

`sub_4411F0` (`elfw_copy_section` per the sweep, but functionally an extended symbol-to-section resolver) is a 482-line function that resolves a symbol index to its owning output section index, handling all layers of indirection. It is the most complex resolution function in the symbol subsystem:

```c
// sub_4411F0 -- resolve symbol to output section index (extended)
uint32_t resolve_symbol_to_section(elfw* ctx, int sym_index) {
    // Step 1: Get symbol record
    symbol_record* sym = elfw_get_symbol(ctx, sym_index);   // +344/+352
    uint16_t shndx = sym->st_shndx;
    
    // Step 2: Resolve extended section index if virtual
    if (shndx == 0xFFFF)
        shndx = resolve_extended_section(ctx, sym);
    
    // Step 3: Early exit for SHN_UNDEF
    if (shndx == 0)
        return 0;
    
    // Step 4: Validate via section descriptor array
    // (includes virtual section indirection at +368, +472)
    if (ctx->virtual_section_flag) {            // byte at +82
        uint32_t virt_entry = ctx->virt_map[shndx];    // +472
        if (virt_entry != 0) {
            uint32_t real_idx = ctx->virt_table[virt_entry];  // +368
            if (real_idx != shndx)
                fatal("secidx not virtual");
        }
    }
    
    // Step 5: Look up section name from section descriptor
    section_record* sec = dyn_array_get(ctx->sections, shndx);  // +360
    const char* sec_name = sec->name;           // offset +96 in section record
    if (!sec)
        fatal("section not found");
    
    // Step 6: Look up the section name in the output hash map
    int out_sec_idx = hash_map_lookup(ctx->name_map, sec_name);
    
    // Step 7: If found, remap through old-to-new tables
    if (ctx->pos_remap && out_sec_idx != 0) {
        out_sec_idx = remap_symbol_index(ctx, out_sec_idx);
    }
    
    // Step 8: Resolve the output section index through extended arrays
    uint16_t out_shndx = resolve_section_index_for(ctx, out_sec_idx);
    
    // Step 9: Compare source and output section indices
    // If they match, return the signed symbol index directly
    // If they differ, scan all positive symbols for a section symbol
    // (type == STT_SECTION, info & 0xF == 3) whose section matches
    if (source_shndx != out_shndx) {
        if (verbose)
            fprintf(stderr, "found multiple section symbols for %s\n", sec_name);
        for (int i = 0; i < dyn_array_count(ctx->pos_symbols); i++) {
            symbol_record* candidate = dyn_array_get(ctx->pos_symbols, i);
            uint16_t cand_sec = resolve_extended_section(ctx, candidate);
            uint16_t orig_sec = resolve_extended_section(ctx, sym);
            if (cand_sec == orig_sec) {
                // Skip: same section as the input symbol
                continue;
            }
            if ((candidate->st_info & 0xF) == STT_SECTION)
                return candidate->sym_index;     // offset +24
        }
    }
    
    return out_sec_idx;
}
```

This function is called from `merge_elf` (`sub_45E7D0`) during the section copy phase. When copying a section from an input ELF to the output ELF, relocations reference symbols by their input-local indices. This function translates those references to output-global indices by walking through: symbol record lookup, extended section resolution, remap table translation, name-based hash map lookup in the output, and finally a linear scan for section symbols when the section index has changed (indicating the section was merged into a different output section).

The linear scan at the end (iterating all positive symbols) is a fallback for the case where multiple input sections map to the same output section. The diagnostic `"found multiple section symbols for %s"` is printed when this occurs, and the function returns the `sym_index` of the first `STT_SECTION` symbol whose resolved section matches the source symbol's resolved section.

## "Reference to Deleted Symbol" Error

The string `"reference to deleted symbol"` appears in every function that accesses the remap tables. The pattern is always:

```c
new_idx = remap_table[abs_old_idx];
if (new_idx == 0) {
    fatal("reference to deleted symbol");
    // Re-read after fatal (defensive -- fatal may return in non-fatal mode)
    new_idx = remap_table[abs_old_idx];
}
```

This error occurs when dead code elimination (`sub_44AD40`) removes a symbol but some other part of the ELF still references it. The fact that the code re-reads the table entry after the fatal call suggests that `sub_467460` may not always abort -- in non-fatal diagnostic mode, execution continues with whatever value the table holds (still zero, meaning the reference is unresolvable).

Common triggers:

1. **Relocations referencing removed functions**: A relocation in a kept section points to a symbol that was eliminated. This typically indicates a bug in the dead code elimination's reachability analysis.

2. **`.nv.info` entries referencing deleted functions**: Per-function metadata entries that were not cleaned up during weak symbol resolution or DCE.

3. **Extended section index stale references**: A symbol's `sym_index` stored in the extended arrays at `+592`/`+600` points to a remap table slot that has been zeroed.

The error is emitted through `sub_467460` with the error record at `unk_2A5B990`, which is used for internal assertion failures rather than user-facing diagnostics.

## Dynamic Array Primitives

The symbol arrays and extended section index arrays all use the same dynamic array type, a 24-byte header:

```
struct dyn_array {
    void**   data;       // [0:8]   pointer to backing allocation (8 bytes per element)
    uint64_t count;      // [8:16]  number of elements currently stored
    uint64_t capacity;   // [16:24] allocated slot count
};
```

| Function | Address | Operation |
|---|---|---|
| `sub_464AE0` | `0x464AE0` | **Create** -- allocate header + backing array with given initial capacity, zero-fill |
| `sub_464BB0` | `0x464BB0` | **Count** -- return `arr->count` (offset +8) |
| `sub_464C30` | `0x464C30` | **Push** -- append element at `arr->count`, increment count; grow (2x capacity, zero-fill new region) if full |
| `sub_464D10` | `0x464D10` | **Set** -- write element at arbitrary index; grow if index >= capacity; update count if index >= count |
| `sub_464DB0` | `0x464DB0` | **Get** -- return `arr->data[index]` if index < count, else NULL |

Growth policy: when capacity is exhausted, double the current capacity. If the doubled value is still too small (e.g., for a set-at-index operation far beyond current capacity), use the required index + 1 instead. New slots are zero-filled via `memset`.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x440590` | `elfw_get_symbol` | 73 B | Dispatch symbol index through positive/negative arrays |
| `0x4411B0` | `elfw_find_symbol_by_name` | 51 B | Look up symbol index in name hash map at +288 |
| `0x4411D0` | `elfw_find_section_by_name` | 51 B | Look up section index in name hash map at +296 |
| `0x440350` | `elfw_resolve_section_index` | 579 B | Resolve `st_shndx` with 0xFFFF virtual indirection and remap |
| `0x444720` | `elfw_remap_symbol_index` | 251 B | Translate old symbol index through remap tables at +456/+464 |
| `0x440BE0` | `elfw_add_symbol` | 7,034 B | Add symbol with duplicate detection, hash map, callgraph |
| `0x442CA0` | `elfw_add_function_symbol` | 7,159 B | Add function symbol with UFT merge and callgraph |
| `0x4411F0` | `elfw_resolve_symbol_to_section` | 12,184 B | Extended symbol-to-section resolution with linear fallback scan |
| `0x449A80` | `hash_map_lookup` | ~400 B | Generic hash map probe (3 hashing modes) |
| `0x448E70` | `hash_map_insert` | ~14,646 B | Hash map insertion with rehashing |
| `0x464AE0` | `dyn_array_create` | ~200 B | Create dynamic array with initial capacity |
| `0x464BB0` | `dyn_array_count` | 16 B | Return element count |
| `0x464C30` | `dyn_array_push` | ~300 B | Append element, grow if needed |
| `0x464D10` | `dyn_array_set` | ~300 B | Set element at index, grow if needed |
| `0x464DB0` | `dyn_array_get` | 32 B | Bounds-checked element access |
| `0x42F850` | `check_sto_cuda_obscure` | ~200 B | Warn on STO_CUDA_OBSCURE visibility bit |
| `0x44B940` | `callgraph_register_function` | ~200 B | Register function symbol in callgraph |
| `0x467460` | `fatal_error` | ~500 B | Emit diagnostic/fatal error |

## Cross-References

- [Merge Phase](../pipeline/merge.md) -- symbol addition during input processing
- [Weak Symbol Handling](weak-symbols.md) -- weak resolution policy that drives symbol replacement
- [Dead Code Elimination](dead-code-elimination.md) -- creates the remap tables after removing unreachable symbols
- [Relocation Engine](relocation-engine.md) -- consumer of `elfw_get_symbol` and `elfw_remap_symbol_index`
- [Section Merging](section-merging.md) -- section-level counterpart to symbol resolution
- [Data Layout Optimization](data-layout-opt.md) -- may modify symbol offsets after merge
