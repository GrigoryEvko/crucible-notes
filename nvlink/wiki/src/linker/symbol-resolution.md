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
| `+456` | `uint32*` | **Positive remap table** (`symbol_index_mapping`) -- old positive symbol index to new symbol index (DCE remap source). |
| `+464` | `uint32*` | **Negative remap table** (`neg_symbol_index_mapping`) -- old negative symbol index (absolute value) to new symbol index (DCE remap target). |
| `+592` | `SortedArray*` | **Merged symbol array** (`merged_symbol_array`) -- holds the resolved section indices for symbols whose `st_shndx == 0xFFFF`. Indexed by the remapped value from `+456`/`+464`, or directly by `sym+24` when the extended store is present. |
| `+600` | `SortedArray*` | **Extended symbol store** (`extended_symbol_store`) -- set during advanced merge paths; when non-NULL, takes precedence over the remap chain and resolves negative `sym+24` values directly. |
| `+624` | `uint32` | **Arch class constant** (`option_parser_result`) -- result of `sub_42F8B0`, which returns the constant `5`. The `sub_42F850` / STO\_CUDA\_OBSCURE warning level is sourced elsewhere; this slot is not that counter. |

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
    
    if (ctx->extended_symbol_store) {       // offset +600
        if (ext_idx >= 0)
            return dyn_array_get(ctx->merged_symbol_array, ext_idx);     // +592
        else
            return dyn_array_get(ctx->extended_symbol_store, -ext_idx);  // +600
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
    
    return dyn_array_get(ctx->merged_symbol_array, ext_idx);  // +592 via offset +74*8
}
```

The two-level resolution works as follows:

1. **Direct path**: If `st_shndx` is a standard value (0 through 0xFEFE, or the special value 0xFFF2 for `SHN_COMMON`), return it immediately.

2. **Extended store path**: If `extended_symbol_store` at `+600` is non-NULL, use the symbol's `sym_index` sign to either (a) look up the positive index directly in `merged_symbol_array` at `+592`, or (b) look up the absolute-valued negative index in `extended_symbol_store` at `+600`.

3. **Remap-then-merged path**: If the extended store does not exist but the DCE remap tables at `+456` (`symbol_index_mapping`, remap source) and `+464` (`neg_symbol_index_mapping`, remap target) do, first translate the old index through the appropriate remap table, then look up the result in `merged_symbol_array` at `+592`. A zero result from the remap table triggers a fatal `"reference to deleted symbol"` error.

### merged\_symbol\_array and extended\_symbol\_store (+592 / +600)

When a symbol is added whose resolved section index exceeds `0xFEFF` (65,279), `sub_440BE0` and `sub_442CA0` lazily create the pair of sorted arrays used by the extended resolution chain:

```c
if (section_index > 0xFEFF && section_index != 0xFFF2) {
    sym->st_shndx = 0xFFFF;                           // mark as virtual

    if (!ctx->merged_symbol_array) {                   // first time: allocate both
        ctx->merged_symbol_array  = dyn_array_create(0x10000);  // +592, sub_464AE0
        ctx->extended_symbol_store = dyn_array_create(0x10000); // +600
    }

    // Store the real section index, keyed by the symbol's pos/neg sym_index
    if (sym->sym_index < 0)
        dyn_array_set(ctx->extended_symbol_store, -sym->sym_index, section_index);
    else
        dyn_array_set(ctx->merged_symbol_array, sym->sym_index, section_index);
}
```

The terminology used here matches [Linker Context](../structs/linker-context.md): `merged_symbol_array` at `+592` (`ctx[74]`) and `extended_symbol_store` at `+600` (`ctx[75]`). Both are created together with initial capacity `0x10000` (65,536 slots). `sub_464AE0` allocates a 24-byte dynamic array header (`data`, `count`, `capacity`); `sub_464D10` performs the set-at-index write, growing the array if necessary.

These arrays serve a dual role:
- **Write side** (add-symbol path, `sub_440BE0`): they hold the real section index for symbols whose `st_shndx` had to be set to `0xFFFF` because the index exceeded the 16-bit range.
- **Read side** (resolve path, `sub_440350`): when `extended_symbol_store` is present, the resolver uses it as an override that bypasses the DCE remap chain entirely. `merged_symbol_array` is the target of the remap-then-lookup branch when the override is absent.

The special value `0xFFF2` (`SHN_COMMON`) bypasses this path entirely -- common symbols are stored directly in `st_shndx` without going through `merged_symbol_array`.

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

`sub_440BE0` (7,034 bytes at `0x440BE0`) adds a symbol to the output ELF with full duplicate detection, hash map registration, and callgraph integration. Its signature:

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

### Reconstructed Pseudocode

The following pseudocode is reconstructed from the decompiled `sub_440BE0` with annotations mapping each block back to the binary. Variable names are assigned by tracing data flow through the decompiled output.

```c
// sub_440BE0 -- elfw_add_symbol
// 7,034 bytes, address 0x440BE0
//
// Adds a symbol to the ELF writer context. Handles section index resolution,
// duplicate detection via hash map, name registration, positive/negative array
// insertion, extended section indices, STO_CUDA_OBSCURE diagnostics, and
// callgraph registration for function symbols.
//
// Returns: the signed symbol index (positive or negative) assigned to this symbol.

int32_t elfw_add_symbol(
    elfw*       ctx,             // a1
    const char* name,            // a2
    uint8_t     sym_type,        // a3: STT_* (0..3)
    uint8_t     binding,         // a4: STB_* (0..2)
    uint8_t     visibility,      // a5: STV_*
    int32_t     section_index,   // a6: signed section index
    uint64_t    value,           // a7: symbol value
    int32_t     func_ordinal,    // a8: function ordinal (non-func: passed through)
    uint64_t    size             // a9: symbol size
)
{
    // ---------------------------------------------------------------
    // PHASE 1: Resolve section index to get the actual st_shndx value
    // ---------------------------------------------------------------
    // Lines 69-124 in decompiled.  The section_index argument (a6) is a
    // signed index into the positive or negative section descriptor arrays.
    // We fetch the section record and read its st_shndx field.  If that
    // field is 0xFFFF (virtual), we chase the extended section index tables.

    symbol_record* sec_rec;
    if (section_index < 0)                                     // line 69
        sec_rec = dyn_array_get(ctx->neg_symbols, -section_index);  // +352
    else
        sec_rec = dyn_array_get(ctx->pos_symbols, section_index);   // +344

    uint32_t resolved_shndx = sec_rec->st_shndx;              // +6 in record

    if (resolved_shndx == 0xFFFF) {
        // Virtual section -- resolve through extended chain
        int32_t ext_idx = sec_rec->sym_index;                  // +24 in record
        void* ext_store = ctx->extended_symbol_store;          // +600

        if (ext_store == NULL) {
            // No extended store yet -- try DCE remap tables
            uint32_t* pos_remap = ctx->symbol_index_mapping;   // +456 (remap src)
            if (pos_remap != NULL && ext_idx != 0) {
                if (ext_idx <= 0) {
                    uint32_t abs_idx = -ext_idx;
                    ext_idx = ctx->neg_symbol_index_mapping[abs_idx]; // +464 (remap dst)
                    if (ext_idx == 0) {
                        fatal("reference to deleted symbol");
                        ext_idx = ctx->neg_symbol_index_mapping[abs_idx]; // re-read (defensive)
                    }
                } else {
                    int32_t saved = ext_idx;
                    ext_idx = pos_remap[ext_idx];              // +456
                    if (ext_idx == 0) {
                        fatal("reference to deleted symbol");
                        ext_idx = pos_remap[saved];
                    }
                }
            } else {
                ext_idx = 0;                                   // fallback: SHN_UNDEF
            }
            resolved_shndx = dyn_array_get(ctx->merged_symbol_array, ext_idx);  // +592
        } else if (ext_idx >= 0) {
            resolved_shndx = dyn_array_get(ctx->merged_symbol_array, ext_idx);  // +592
        } else {
            resolved_shndx = dyn_array_get(ext_store, -ext_idx);                // +600
        }
    }

    // ---------------------------------------------------------------
    // PHASE 2: Hash map lookup for duplicate detection
    // ---------------------------------------------------------------
    // Lines 125-129.  Probe the name hash map at ctx+288 for an existing
    // symbol with the same name.  existing_idx is signed: positive means
    // local/section symbol, negative means global/weak.

    hash_map* name_map = ctx->name_map;                        // +288
    int32_t existing_idx = 0;
    int32_t* slot = hash_map_lookup(name_map, name);           // sub_449A80
    if (slot != NULL)
        existing_idx = *slot;

    // ---------------------------------------------------------------
    // PHASE 3: Allocate 48-byte symbol record from arena
    // ---------------------------------------------------------------
    // Lines 130-136.  The arena allocator at sub_44F410 returns the
    // thread-local arena context; sub_4307C0 is the arena alloc with
    // size-class bucketing.  The 48 bytes are zero-initialized.

    void* arena = *(void**)(get_tls_arena(name_map) + 24);    // sub_44F410
    symbol_record* sym = arena_alloc(arena, 48);               // sub_4307C0
    if (sym == NULL)
        fatal_oom(arena, 48);                                  // sub_45CAC0
    memset(sym, 0, 48);                                        // 3x 128-bit zero stores

    // ---------------------------------------------------------------
    // PHASE 3a: Callgraph-completed guard
    // ---------------------------------------------------------------
    // Lines 137-147.  If the callgraph is already finalized (byte flag
    // at ctx+81 is set) and we are adding a function (sym_type == 2),
    // emit an assertion.  This catches late additions after DCE has run.

    if (ctx->callgraph_completed && sym_type == STT_FUNC)      // +81 flag
        fatal("adding function after callgraph completed");

    // ---------------------------------------------------------------
    // PHASE 4: Handle existing symbol (duplicate path)
    // ---------------------------------------------------------------
    // Lines 148-191.  If existing_idx != 0, an entry with this name
    // already exists.  The behavior depends on the new symbol's binding.

    hash_entry* entry_ptr = NULL;      // will point to hash map entry for update

    if (existing_idx != 0) {
        // Fetch the existing symbol record
        symbol_record* existing;
        if (existing_idx < 0)
            existing = dyn_array_get(ctx->neg_symbols, -existing_idx);
        else
            existing = dyn_array_get(ctx->pos_symbols, existing_idx);

        if (existing != NULL) {
            if (binding == STB_GLOBAL) {
                // ---- Global-on-global conflict ----
                // Check if existing is also STB_GLOBAL: (st_info >> 4) == 1
                if ((existing->st_info >> 4) == 1)
                    fatal("adding global symbols of same name");

                // Inherit st_name (string table offset) and name_str pointer
                sym->st_name = existing->st_name;         // copy dword at +0
                sym->name_str = existing->name_str;        // copy qword at +32 (slot 4)

            } else {
                // ---- Local or weak binding ----
                // Copy st_name and name_str from existing
                sym->st_name = existing->st_name;
                sym->name_str = existing->name_str;

                if (binding == STB_LOCAL) {
                    // For locals: if existing has no name_str, treat as fresh
                    if (existing->name_str == NULL)
                        goto allocate_new_entry;
                    // Otherwise fall through to update hash map entry
                }
            }
            // Re-probe hash map to get the entry pointer for update
            entry_ptr = hash_map_lookup(name_map, name);       // sub_449A80
        }

        // Check if we have a name_str to reuse
        if (sym->name_str != NULL)
            goto populate_record;
    }

    // ---------------------------------------------------------------
    // PHASE 5: Allocate new hash map entry and register name
    // ---------------------------------------------------------------
    // Lines 194-213.  Reached when: (a) no existing entry, or (b)
    // existing entry's name_str was NULL (fresh insertion path).
    //
    // Each hash map entry is 12 bytes:
    //   [0:8]  padding (zeroed)
    //   [8:12] sym_index (updated later at PHASE 8)

allocate_new_entry:
    arena = *(void**)(get_tls_arena(/*current*/) + 24);
    entry_ptr = arena_alloc(arena, 12);                        // sub_4307C0
    if (entry_ptr == NULL)
        fatal_oom(arena, 12);
    memset(entry_ptr, 0, 12);                                  // zero 8+4 bytes

    ctx->name_counter++;                                       // +304

    // Copy the name string into arena-owned memory
    size_t name_len = strlen(name);
    arena = *(void**)(get_tls_arena(name) + 24);
    char* name_copy = arena_alloc(arena, name_len + 1);
    if (name_copy == NULL)
        fatal_oom(arena, name_len + 1);
    strcpy(name_copy, name);

    // Store name pointer in symbol record and insert into hash map
    sym->name_str = name_copy;                                 // qword at sym+32
    hash_map_insert(ctx->name_map, name_copy, entry_ptr);      // sub_448E70

    // ---------------------------------------------------------------
    // PHASE 6: Populate the symbol record fields
    // ---------------------------------------------------------------
    // Lines 215-218.  Pack st_info from binding (high nibble) and
    // sym_type (low nibble).  Set visibility in st_other.  Store
    // value and size.

populate_record:
    sym->st_info  = (sym_type & 0xF) | (binding << 4);        // byte at +4
    sym->st_other = visibility;                                // byte at +5
    sym->st_value = value;                                     // qword at +8
    sym->st_size  = size;                                      // qword at +16

    // ---------------------------------------------------------------
    // PHASE 7: Insert into positive or negative symbol array
    // ---------------------------------------------------------------
    // Lines 219-231.  Global symbols (binding == 1) go into the
    // negative array at +352 with a negated index.  All others
    // (local, weak) go into the positive array at +344.

    if (binding == STB_GLOBAL) {
        int32_t neg_count = dyn_array_count(ctx->neg_symbols); // sub_464BB0, +352
        sym->sym_index = -neg_count;                           // dword at sym+24
        dyn_array_push(sym, ctx->neg_symbols);                 // sub_464C30, +352
    } else {
        int32_t pos_count = dyn_array_count(ctx->pos_symbols); // sub_464BB0, +344
        sym->sym_index = pos_count;                            // dword at sym+24
        dyn_array_push(sym, ctx->pos_symbols);                 // sub_464C30, +344
    }

    // ---------------------------------------------------------------
    // PHASE 8: Extended section index handling
    // ---------------------------------------------------------------
    // Lines 232-292.  If resolved_shndx fits in 16 bits normally
    // (<=0xFEFF) or is the SHN_COMMON marker (0xFFF2 = 65522),
    // store it directly.  Otherwise mark as virtual (0xFFFF) and
    // store the real index in the extended section index arrays.

    if (resolved_shndx <= 0xFEFF || resolved_shndx == 0xFFF2) {
        sym->st_shndx = (uint16_t)resolved_shndx;             // word at sym+6
    } else {
        // Mark as virtual
        sym->st_shndx = 0xFFFF;

        // Lazily allocate the extended resolution arrays
        if (ctx->merged_symbol_array == NULL) {                            // +592
            ctx->merged_symbol_array  = dyn_array_create(0x10000);         // sub_464AE0
            ctx->extended_symbol_store = dyn_array_create(0x10000);        // +600
        }

        // Resolve the symbol's own index through remap tables
        // (same three-tier logic as Phase 1, but using sym->sym_index)
        int32_t store_idx = sym->sym_index;                    // dword at sym+24

        if (ctx->extended_symbol_store == NULL) {
            // Remap path (arrays just created, so this branch is only
            // reachable if dyn_array_create returned +592 non-null but
            // +600 null -- in practice unreachable, but the binary
            // contains this defensive code)
            uint32_t* pos_remap = ctx->symbol_index_mapping;   // +456
            if (pos_remap != NULL && store_idx != 0) {
                if (store_idx <= 0) {
                    uint32_t abs_idx = -store_idx;
                    store_idx = ctx->neg_symbol_index_mapping[abs_idx];
                    if (store_idx == 0) {
                        fatal("reference to deleted symbol");
                        store_idx = ctx->neg_symbol_index_mapping[abs_idx];
                    }
                } else {
                    int32_t saved = store_idx;
                    store_idx = pos_remap[store_idx];
                    if (store_idx == 0) {
                        fatal("reference to deleted symbol");
                        store_idx = pos_remap[saved];
                    }
                }
            } else {
                store_idx = 0;
            }
            dyn_array_set(ctx->merged_symbol_array, store_idx, resolved_shndx);
        } else if (store_idx >= 0) {
            dyn_array_set(ctx->merged_symbol_array, store_idx, resolved_shndx);
        } else {
            dyn_array_set(ctx->extended_symbol_store, -store_idx, resolved_shndx);
        }
    }

    // ---------------------------------------------------------------
    // PHASE 9: STO_CUDA_OBSCURE diagnostic (effectively disabled)
    // ---------------------------------------------------------------
    // Line 294.  sub_42F850 checks bit 43 of the symbol record's first
    // qword and the value stored at ctx+624.  The check is:
    //     (sym_dword & 0x80000000000) != 0 && ctx[78] <= 4
    //
    // ctx[78] (offset +624, `option_parser_result`) is initialized by
    // sub_42F8B0 at the end of elfw_create.  sub_42F8B0 returns the
    // constant 5 unconditionally, so the `<= 4` condition never holds
    // and the STO_CUDA_OBSCURE diagnostic is effectively dead code in
    // this build of nvlink.  The field is still written by the binary
    // and is passed through to sub_42F850 here.

    check_sto_cuda_obscure(ctx->option_parser_result, sym->st_name);  // sub_42F850

    // ---------------------------------------------------------------
    // PHASE 10: Update hash map entry with assigned index
    // ---------------------------------------------------------------
    // Lines 295-297.  The sym_index was just determined in Phase 7.
    // If entry_ptr is non-NULL (we have a hash map entry to update),
    // write the sym_index into it so future lookups resolve to this
    // symbol.

    int32_t result = sym->sym_index;                           // dword at sym+24
    if (entry_ptr != NULL)
        entry_ptr->sym_index = result;                         // dword at entry+8

    // ---------------------------------------------------------------
    // PHASE 11: Callgraph registration (STT_FUNC only)
    // ---------------------------------------------------------------
    // Lines 298-309.  If the symbol type is STT_FUNC (2), increment
    // the function ordinal counter, store it in the symbol record, and
    // register the symbol in the callgraph array at ctx+408.  For
    // non-function symbols, the caller-provided func_ordinal (a8) is
    // stored directly.

    if (sym_type == STT_FUNC) {
        int32_t ordinal = ++ctx->func_ordinal_counter;         // +416
        sym->func_ordinal = ordinal;                           // dword at sym+28
        callgraph_register(ctx, result);                       // sub_44B940
        result = sym->sym_index;                               // re-read (may alias)
    } else {
        sym->func_ordinal = func_ordinal;                      // a8 passed through
    }

    // ---------------------------------------------------------------
    // PHASE 12: Clear trailing flags field and return
    // ---------------------------------------------------------------
    // Line 310-311.

    sym->flags = 0;                                            // dword at sym+40
    return result;
}
```

### Detailed Phase Analysis

The function proceeds through twelve distinct phases. The following sections expand on the less obvious behaviors.

### 1. Section Index Resolution

The section index argument (`a6`) is resolved through `sub_440590` to obtain the section record pointer. If the section record's `st_shndx` field is `0xFFFF`, the extended section indirection path kicks in (same three-tier resolution as in `sub_440350`).

The resolution has three tiers in priority order:

1. **Direct**: `st_shndx` is not `0xFFFF` -- return it immediately.
2. **Extended store present** (`extended_symbol_store` at `ctx+600` is non-NULL): use `sym_index` sign to select `merged_symbol_array` at `+592` (positive) or `extended_symbol_store` at `+600` (negative), then look up the real section index.
3. **DCE remap tables present** (`symbol_index_mapping` at `ctx+456` is non-NULL): translate `sym_index` through `+456` (`symbol_index_mapping`, remap source) or `+464` (`neg_symbol_index_mapping`, remap target), then look up via `merged_symbol_array` at `+592`. A zero remap result triggers `"reference to deleted symbol"`.

This three-tier resolution is duplicated verbatim in Phase 8 (lines 237-292 of the decompiled source), where the same logic resolves the symbol's own newly-assigned `sym_index` for extended section index storage. The binary contains two identical copies of the remap-table chase code within this single function.

### 2. Duplicate Detection via Hash Map

The name is looked up in the hash map at `ctx+288`:

```c
int existing_idx = hash_map_lookup(ctx->name_map, name);    // sub_449A80
```

If `existing_idx` is nonzero, the symbol already exists. The function retrieves the existing record via `sub_440590` (following the positive/negative convention) and checks for conflicts:

- **Global-on-global conflict**: If the existing symbol has `binding == STB_GLOBAL` (bit pattern `(st_info >> 4) == 1`) and the new symbol also has `binding == STB_GLOBAL`, the diagnostic `"adding global symbols of same name"` is triggered via `sub_467460`. This is an internal assertion, not a user-facing error -- the multiple-definition user error is handled elsewhere (in `merge_elf`). The decompiled check at line 164 is `*((_BYTE *)v28 + 4) >> 4 == 1`, which reads `st_info` at offset `+4` in the existing record and extracts the binding nibble.

- **Local replacing existing**: If `binding == STB_LOCAL` (0), the existing record's `st_name` (dword at `+0`) and `name_str` (qword at `+32`) are copied into the new record. If the existing `name_str` pointer is NULL, the new record is treated as a fresh insertion -- control jumps to the hash map registration path. This handles the case where a previous local symbol was a placeholder without an allocated name string.

- **Weak binding** (`binding == 2`): The new record inherits `st_name` and `name_str` from the existing record and falls through to re-probe the hash map to get the entry pointer for later update.

In all duplicate paths, the hash map is re-probed (line 183: `v28 = sub_449A80(v21, a2)`) to obtain a direct pointer to the entry, which is then updated in Phase 10 with the new symbol's assigned index.

### 3. Hash Map Registration

If the symbol is new (no existing entry found, or the existing entry had a NULL `name_str`), a 12-byte hash map entry node is allocated:

```c
struct name_entry {
    uint64_t padding;     // 8 bytes (unused, zeroed)
    uint32_t sym_index;   // 4 bytes: signed symbol index
};
```

The name string is copied into arena memory (`strcpy` into freshly allocated buffer), and the entry is inserted into the hash map via `sub_448E70(ctx->name_map, name_copy, &entry)`. The name counter at `ctx+304` is incremented (line 201: `++*(_DWORD *)(a1 + 304)`) to track total distinct names registered.

The arena allocation uses the thread-local arena context obtained via `sub_44F410`. The allocator (`sub_4307C0`) uses size-class bucketing -- for the 12-byte entry, this falls into the smallest bucket. If allocation fails (returns NULL), `sub_45CAC0` triggers a fatal OOM error.

### 4. Array Insertion

The symbol record (48 bytes, zero-initialized) is populated:

```c
sym->st_info  = (binding << 4) | (sym_type & 0xF);     // byte at +4
sym->st_other = visibility;                              // byte at +5
sym->st_value = value;                                   // qword at +8
sym->st_size  = size;                                    // qword at +16
```

For globals (`binding == 1`), the symbol is appended to the negative array at `+352`, and its `sym_index` field is set to `-count` (negative of the current array length before push). For all others, it goes into the positive array at `+344` with a positive index equal to the current count.

The encoding at line 215 (`(a3 & 0xF) + 16 * a4`) matches the ELF `st_info` convention: `ELF64_ST_INFO(bind, type) = (bind << 4) + (type & 0xf)`.

### 5. Extended Section Index Handling

If the resolved section index exceeds `0xFEFF` (but is not the special `0xFFF2` / `SHN_COMMON` marker), the symbol's `st_shndx` is set to `0xFFFF` and the real index is stored in the resolution arrays at `+592`/`+600`, keyed by the symbol's signed index. The conditional at line 232 (`v17 <= 0xFEFF || v17 == 65522`) shows that `SHN_COMMON` (65522 = `0xFFF2`) bypasses the extended path -- it is stored directly in `st_shndx`.

The arrays are allocated on first use (line 241: `sub_464AE0(0x10000)`) with an initial capacity of 65,536 entries. Both `merged_symbol_array` at `+592` and `extended_symbol_store` at `+600` are created together.

For storage, the symbol's newly-assigned `sym_index` determines the array and slot:
- `sym_index >= 0`: store in `merged_symbol_array[sym_index]` (`ctx[74]`, offset +592)
- `sym_index <  0`: store in `extended_symbol_store[-sym_index]` (`ctx[75]`, offset +600)

### 6. STO_CUDA_OBSCURE Check (Dead in This Build)

After insertion, `sub_42F850` is called with the value at `ctx+624` (`option_parser_result`) and the symbol record's first qword. The check in `sub_42F850` is:

```c
// sub_42F850 decompiled
if ((sym_dword & 0x80000000000) != 0 && option_parser_result <= 4) {
    const char* level_str = "";
    if (option_parser_result >= 1 && option_parser_result <= 5)
        level_str = warning_level_strings[option_parser_result - 1];  // off_1D371A0
    diagnostic(&unk_2A5BAB0, "", "STO_CUDA_OBSCURE", level_str);
}
```

The value at `ctx+624` is initialized by the last step of `elfw_create` via:

```c
*((_DWORD *)v17 + 156) = sub_42F8B0();   // elfw_create, offset +624
```

`sub_42F8B0` is a 7-line constant-return function that unconditionally returns **5**. Because `sub_42F850`'s guard is `<= 4`, the STO\_CUDA\_OBSCURE diagnostic is unreachable in this build of nvlink -- the check always fails regardless of which symbols are added. `STO_CUDA_OBSCURE` would have been a CUDA-specific symbol visibility attribute encoded in the `st_other` field's upper bits, but no call site in the shipped binary can actually trigger the warning. The field at `ctx+624` is retained so that the call signature matches (and perhaps to support a future mode where the constant is replaced with a user-controllable warning level).

### 7. Callgraph Registration

If the symbol type is `STT_FUNC` (2), the function ordinal counter at `ctx+416` is incremented, stored in the symbol record at offset `+28`, and the symbol is registered in the callgraph via `sub_44B940(ctx, sym_index)`. This is how entry points and device functions enter the dead code elimination graph.

The callgraph registration function (`sub_44B940`) performs:

```c
// sub_44B940 -- callgraph_register_function
void callgraph_register(elfw* ctx, int32_t sym_index) {
    symbol_record* sym = elfw_get_symbol(ctx, sym_index);     // sub_440590
    dyn_array* callgraph = ctx->callgraph_array;              // +408

    // Check if ordinal slot already occupied
    void* existing = dyn_array_get(callgraph, sym->func_ordinal);
    if (existing != NULL)
        return;  // already registered

    // Allocate a 64-byte callgraph node, zero-initialize
    void* arena = *(void**)(get_tls_arena(callgraph) + 24);
    callgraph_node* node = arena_alloc(arena, 64);
    if (node == NULL) fatal_oom(arena, 64);
    memset(node, 0, 64);   // zero 52 bytes of payload + padding

    node->sym_index = sym_index;                              // dword at +0

    // Store at ordinal position in the callgraph array
    dyn_array_set(callgraph, sym->func_ordinal, node);        // sub_464D10
}
```

The callgraph array at `ctx+408` is indexed by function ordinal (offset `+28` in the symbol record). Each node is 64 bytes with the symbol index at offset `+0` and the remaining 60 bytes holding edge lists and flags used by the dead code elimination sweep (`sub_44AD40`).

For non-function symbols (`sym_type != 2`), the caller-provided `func_ordinal` argument (`a8`) is written directly into offset `+28` of the symbol record without incrementing the counter or calling `sub_44B940`. This allows the caller to pass zero or a pre-assigned ordinal for section symbols and data objects.

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

## Worked Example: Symbol Resolution Walkthrough

This section traces a complete end-to-end symbol resolution scenario through the decompiled code, starting from empty symbol arrays and ending with the final resolved symbol table. Every hash value is the actual `sub_44E000` MurmurHash3\_x86\_32 output for the given name, every line reference is to `decompiled/sub_440BE0_0x440be0.c` (which matches `elfw_add_symbol` at `0x440BE0`) or `decompiled/sub_448E70_0x448e70.c` (`hash_map_insert` at `0x448E70`), and every offset into the elfw context and symbol record matches the values listed in the tables above.

### Scenario

Three inputs drive the linker:

| Input | Symbol | Binding | Type | Section | Notes |
|---|---|---|---|---|---|
| `input1.o` | `main_kernel` | `STB_GLOBAL` | `STT_FUNC` | `.text` (idx 1) | Strong definition |
| `input2.o` | `main_kernel` | `STB_GLOBAL` | `STT_NOTYPE` | `SHN_UNDEF` (0) | Undefined reference |
| `input2.o` | `helper_fn` | `STB_WEAK` | `STT_FUNC` | `.text` (idx 1) | Weak definition |
| `input2.o` | `__nv_sqrt` | `STB_GLOBAL` | `STT_NOTYPE` | `SHN_UNDEF` (0) | Undefined reference |
| `libdevice.a(sqrt.o)` | `__nv_sqrt` | `STB_GLOBAL` | `STT_FUNC` | `.text` | Strong (lazy-loaded) |
| `libdevice.a(helper.o)` | `helper_fn` | `STB_GLOBAL` | `STT_FUNC` | `.text` | Strong (lazy-loaded) |

The linker processes `input1.o`, then `input2.o`, then pulls needed members from `libdevice.a`. Throughout the walkthrough the elfw context has a hash map at `ctx+288` initially sized to 64 buckets (mask `0x3F` at `map+40`), empty positive and negative symbol arrays at `ctx+344` and `ctx+352`, and `ctx+304` (name counter) initialized to zero.

### Step 1: Compute MurmurHash3 Values

The string hash function `sub_44E000` (documented in [Hash Tables](hash-tables.md#string-hash-murmurhash3_x86_32)) produces the following `uint32_t` hash values with seed 0. These were verified against a reference Python implementation of MurmurHash3\_x86\_32 to confirm they match Austin Appleby's published algorithm exactly:

| Name | `murmur3(name)` | Hex | Bucket (mask `0x3F`) |
|---|---|---|---|
| `main_kernel` | `3,328,480,444` | `0xC65D92BC` | `0xC65D92BC & 0x3F = 60` |
| `helper_fn` | `1,000,959,075` | `0x3BA6AA63` | `0x3BA6AA63 & 0x3F = 35` |
| `__nv_sqrt` | `3,170,487,566` | `0xBCFBED0E` | `0xBCFBED0E & 0x3F = 14` |

The bucket index is computed in `sub_448E70` at line 225 of the decompiled source for hashing mode 0 (string keys): `v85 = *((_DWORD *)v3 + 10) & v84;` -- this reads the mask at `map+40` (dword index 10) and ANDs it with the hash output. The bucket array is at `map+104` (qword index 13: `*((_QWORD *)v3 + 13) + 8 * v85`).

### Step 2: Process `input1.o` - Add `main_kernel`

The merge loop calls `elfw_add_function_symbol` (`sub_442CA0`) for the strong global function. Trace through the decompiled code:

**Line 66**: `sub_449A80(ctx+288, "main_kernel")` probes the hash map. The map is empty, so the lookup returns NULL and `v7 = NULL`. Control falls through to the insertion path.

**Lines 73-118**: Section index resolution via `sub_464DB0` on `ctx+344` slot 0 returns the `.text` section record; its `st_shndx` is `1` (not `0xFFFF`), so `v14 = 1` directly.

**Lines 121-125**: A second hash map probe (`sub_449A80(ctx+288, "main_kernel")`) confirms `v16 = 0` (not found).

**Lines 126-132**: Arena allocation of a 48-byte symbol record via `sub_4307C0(arena, 48)`. The three 128-bit zero stores at lines 130-132 (`*(_OWORD *)v23 = 0; *((_OWORD *)v23 + 1) = 0; *((_OWORD *)v23 + 2) = 0;`) clear the record.

**Lines 184-200**: Because `v16 == 0`, control skips the duplicate path and jumps to `LABEL_39`. The 12-byte hash map entry is allocated at line 186: `sub_4307C0(v46, 12)`. The name counter at `ctx+304` is incremented to `1` (line 191). A name buffer of length 12 (`strlen("main_kernel") + 1`) is allocated at line 194, and the name is copied with `strcpy` (line 200). The symbol record's `name_str` (qword at record+32, i.e., `v23[4]`) is set to the copied name pointer at line 202. Finally `sub_448E70(name_map, name_copy, entry)` (line 203) inserts the entry into the hash map.

Inside `sub_448E70`, line 218 computes `v84 = (*(hash_fn))(name_copy)` -- for mode 0 with no context, this calls the function pointer at `map+0`, which is `sub_44E000` (MurmurHash3). The returned value is `0xC65D92BC`. Line 225 computes `v85 = 0xC65D92BC & 0x3F = 60`, and line 226 reads `*(_QWORD *)(map+104 + 8*60)`, which is NULL (empty bucket). Control proceeds to the bucket allocation path at line 456-470: a 12-byte bucket header is allocated (`sub_4307C0(arena, 12)` at line 460), initialized with `*v43 = 1` (one entry), `v43[1] = 29` (the new entry's index in the entry array at `map+88`), and `v43[2] = 0xFFFFFFFF` (the `-1` sentinel terminating the chain). The entry slot itself (line 475-477) stores the name pointer and the `entry_ptr` value at `map+88 + 16*29`.

**Lines 205-208 of `sub_442CA0`**: The symbol record is populated -- `st_other = visibility`, `st_value = 0`, `st_size = 0`, `st_info = 0x12` (`STB_GLOBAL << 4 | STT_FUNC`, computed in `v66 = 16 * a3 + 2` at line 144).

**Lines 209-221**: Because `a3 == 1` (STB_GLOBAL), the symbol goes into the negative array. `sub_464BB0(ctx+352)` returns `0` (empty array), so `v45 = 0` and `*((_DWORD *)v23 + 6) = -0 = 0`. The symbol is pushed into `ctx+352` via `sub_464C30(v23, *(ctx+352))`. **Note**: because `-v45` is also `0`, the first negative-array slot is indexed by `0`, which is also the indexing convention for `SHN_UNDEF`. In practice `elfw_add_symbol` inserts a sentinel into position 0 of the negative array during elfw initialization so that real globals start at index `-1`. For the walkthrough we assume position 0 is already occupied, so `main_kernel` gets `sym_index = -1`.

**Lines 223-224**: `v14 = 1` (the resolved `.text` index), which is `<= 0xFEFF`, so `*((_WORD *)v23 + 3) = 1`. No extended section index path.

**Lines 289-296**: `sub_42F850` checks the STO_CUDA_OBSCURE bit; `v24` (the entry pointer) is non-NULL, so `*(_DWORD *)v24 = v34 = -1`, writing the assigned `sym_index` into the hash map entry at `entry+8`. The function ordinal counter at `ctx+416` is bumped to `1` and written into the symbol record at offset `+28`. `sub_44B940(a1, -1)` registers the function in the callgraph.

**Line 301**: `sub_442820(a1, "main_kernel", visibility, -1)` is the UFT/merge-symbols hook; for a first-time strong global it is a no-op because there is nothing to merge.

**Line 302**: Returns `-1`.

**State after Step 2**:

```
pos_symbols (ctx+344):  [sentinel, ...]
neg_symbols (ctx+352):  [sentinel, main_kernel]        // index -1 = main_kernel
name_map (ctx+288) buckets [mask 0x3F]:
    bucket 60 -> [count=1, entries=[29], -1]           // 29 = entry slot in map+88
    (all other buckets NULL)
entries (map+88):
    slot 29: key="main_kernel" (ptr), value=entry_ptr_29
entry_ptr_29 at arena offset:
    padding (8 bytes zero) | sym_index = -1
name_counter (ctx+304): 1
func_ordinal_counter (ctx+416): 1
```

### Step 3: Process `input2.o` - `main_kernel` Undefined Reference

`input2.o` has an undefined reference to `main_kernel`. The merge loop calls `elfw_add_symbol` (`sub_440BE0`) with `section_index = 0` (`SHN_UNDEF`), `binding = STB_GLOBAL`, `sym_type = STT_NOTYPE`, `value = 0`, `size = 0`.

**Line 78**: Because `a6 == 0` (not negative), `sub_464DB0(ctx+344, 0)` returns the positive-array sentinel. Its `st_shndx` is `0`, not `0xFFFF`, so `v17 = 0` and control falls through to `LABEL_3`.

**Lines 125-129**: `sub_449A80(ctx+288, "main_kernel")` now hits the existing entry. The hash goes to bucket 60 (`0xC65D92BC & 0x3F`), the bucket chain walker reads entry slot 29, compares the key pointer against the new name string. Mode 0 string comparison uses `map+8`/`map+16` which is `strcmp`. The comparison succeeds, the slot's value pointer is returned, and `*v20 = -1` is loaded into `v19`. So `v19 = -1` (the existing `main_kernel` index).

**Lines 137-147**: Callgraph-completed check; callgraph not yet finalized, so skip.

**Lines 148-184**: `v19 != 0`, so enter the duplicate path. `v19 < 0`, so `v28 = sub_464DB0(ctx+352, 1) = existing_main_kernel_record`.

**Line 162**: `a4 == 1` (STB_GLOBAL). **Line 164**: `*((_BYTE *)v28 + 4) >> 4` reads `st_info >> 4` of the existing record, which is `(0x12 >> 4) = 1`. The condition `== 1` is true.

At this point the binary would normally trigger `sub_467460` with `"adding global symbols of same name"` -- **but this is only invoked for actual strong-on-strong duplicate definitions**. For an undefined reference, the merge layer above this function (`sub_45E7D0`, the per-input-object merge loop) short-circuits the call entirely: when `section_index == 0` (undefined) and an existing entry is found, the merge loop does not re-add the symbol, it just records the reference in its own relocation bookkeeping. So in practice control never reaches `sub_440BE0` for this particular symbol. The hash map and symbol arrays are unchanged.

This is one of the key insights of the resolution design: `sub_440BE0` is the low-level insertion function, and the higher-level merge loop in `sub_45E7D0` handles the filtering of undefined-vs-defined cases before calling it. See [Merge Phase](../pipeline/merge.md) for the per-object dispatch logic.

### Step 4: Process `input2.o` - `helper_fn` Weak Definition

Next `input2.o` provides `helper_fn` as `STB_WEAK`, `STT_FUNC`, section `.text` (idx 1). The merge loop calls `elfw_add_function_symbol` (`sub_442CA0`) with `a3 = 2` (STB_WEAK).

**Line 66**: Hash map probe for `"helper_fn"`. `murmur3("helper_fn") = 0x3BA6AA63`, bucket = `35`. Bucket 35 is empty; `sub_449A80` returns NULL, `v7 = NULL`. Fall through.

**Lines 73-118**: Section index resolution returns `v14 = 1` (the `.text` idx from the current input's section table).

**Lines 121-125**: Second probe still returns NULL, `v16 = 0`.

**Lines 126-132**: 48-byte record allocation, zero-fill.

**Line 144**: `v66 = 16 * 2 + 2 = 0x22` (STB_WEAK << 4 | STT_FUNC).

**Line 145 onwards**: `v16 == 0`, branch to `LABEL_39`.

**Line 184 (LABEL_39)**: 12-byte entry allocation, name_counter++ (now `2`), strdup of `"helper_fn"`, `sub_448E70(map, "helper_fn", entry)` inserts into the hash map.

Inside `sub_448E70`: hash function returns `0x3BA6AA63`, bucket = `35`. Bucket 35 is NULL, so the bucket allocation path runs. A 12-byte bucket header is allocated, initialized with `*v43 = 1`, `v43[1] = 42` (the new entry slot in `map+88`), `v43[2] = -1`.

**Lines 205-208**: Populate record: `st_other`, `st_info = 0x22`, `st_value = 0`, `st_size = 0`.

**Line 209**: `a3 == 2` (STB_WEAK, not STB_GLOBAL), so fall through to `LABEL_14`. The symbol goes into the **positive** array at `ctx+344` (weak symbols share the positive array with locals in this implementation; only `STB_GLOBAL` gets the negative array). `sub_464BB0(ctx+344)` returns the current positive count, call it `17` (an arbitrary value after section symbols have been added earlier in merge). `*((_DWORD *)v23 + 6) = 17`. Push into positive array.

**Lines 223-225**: `v14 = 1`, direct `st_shndx` store.

**Lines 289-296**: Entry pointer updated to `17`. Callgraph registered with ordinal `2`.

**Line 301**: `sub_442820(a1, "helper_fn", visibility, 17)` is the weak-merge hook. Since this is the first definition of `helper_fn`, it is a no-op at this level.

**State after Step 4**:

```
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel]
name_map buckets:
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = 17)
name_counter: 2
```

### Step 5: Process `input2.o` - `__nv_sqrt` Undefined Reference

`input2.o` references `__nv_sqrt` as `STB_GLOBAL`, `STT_NOTYPE`, `SHN_UNDEF`. Same as Step 3, the higher-level merge loop detects that this is an undefined reference with no matching definition yet in the output, but instead of dropping the reference entirely it **adds a placeholder entry** to the negative symbol array and marks it as "needs resolution". This placeholder lives in the hash map so subsequent lookups from archive members can find it.

**Line 125-129 in `sub_440BE0`**: `sub_449A80(ctx+288, "__nv_sqrt")` returns NULL (first encounter), so `v19 = 0`.

**Lines 194-213**: Fresh insertion path. Entry allocation, name_counter++ (now `3`), strdup, `sub_448E70(map, "__nv_sqrt", entry)`.

Inside `sub_448E70`: hash = `0xBCFBED0E`, bucket = `14`. Empty, bucket allocation runs, entry slot (say `55`) is assigned.

**Line 215**: `st_info = (0 & 0xF) + 16 * 1 = 0x10` (STB_GLOBAL | STT_NOTYPE).

**Lines 219-223**: STB_GLOBAL -> negative array. `sub_464BB0(ctx+352)` returns `2` (one sentinel + main_kernel pushed earlier). So `-v41 = -2`, `*((_DWORD *)v26 + 6) = -2`. Push into negative array.

**Line 232**: `v17 = 0` (SHN_UNDEF), `0 <= 0xFEFF`, store directly. No extended path.

**Line 295-297**: Entry pointer gets `*(_DWORD *)v28 = -2`.

**State after Step 5**:

```
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt(UND)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = 17)
    slot 55: "__nv_sqrt"   -> entry_ptr_55 (sym_index = -2, SHN_UNDEF)
name_counter: 3
```

### Step 6: Archive Lazy Loading - Pull `libdevice.a(sqrt.o)`

After all command-line inputs are merged, the merge loop walks the list of archives (from `-l` options) and asks each: "do you provide any symbol that is currently undefined in the output?" This is the archive scan phase, performed by `sub_42A2D0` (`archive_validate_callback`, documented in [Library Search](../infra/library-search.md)).

For `libdevice.a`, the archive index is consulted for each undefined symbol in the output. The undefined set is computed by scanning `neg_symbols` for entries whose `st_shndx == 0`. In this scenario that set is `{__nv_sqrt}` (because `main_kernel` was resolved by `input1.o` and `helper_fn` was weakly defined by `input2.o`).

The archive index lookup finds that `libdevice.a(sqrt.o)` provides `__nv_sqrt`. The member is extracted and its symbols are added to the elfw via the normal merge path. Focus on the `__nv_sqrt` addition:

**Entry to `sub_442CA0`** (because `STT_FUNC`): `a3 = 1` (STB_GLOBAL), `a4 = visibility`.

**Line 66**: `sub_449A80(ctx+288, "__nv_sqrt")` -> hash `0xBCFBED0E`, bucket `14`, chain walk finds entry slot 55, returns pointer to `entry_ptr_55`. `v7 != NULL`, `v8 = *v7 = -2` (the existing UND slot).

**Line 70**: `*v7 != 0`, so goto `LABEL_26` at line 300 -- **the function short-circuits**. Instead of creating a new record, it calls `sub_442820(a1, "__nv_sqrt", visibility, -2)` with the existing index. `sub_442820` (`elfw_merge_symbols`) is the weak/UFT resolution helper; when the existing slot is an UND placeholder and the incoming symbol is a strong definition, it updates the existing negative-array record in place: sets `st_shndx` to the new section index, `st_info = 0x12`, `st_value` to the function's offset in the new section, etc.

So `__nv_sqrt` at negative index `-2` is **upgraded in place** from UND to a strong definition. The hash map entry pointer already points to slot `-2`, so no rebucketing is needed. The entry at slot 55 still references index `-2`, which now holds a resolved record.

**State after Step 6**:

```
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt(RESOLVED)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
name_counter: 3 (unchanged)
```

### Step 7: Archive Lazy Loading - Strong Replaces Weak (`helper_fn`)

The archive scan pass is not limited to UND symbols. If a later `-l` archive provides a strong definition of a symbol that is currently **weak** in the output, standard ELF semantics dictate that the strong definition replaces the weak. nvlink implements this in the weak-resolution helper `sub_442820` (`elfw_merge_symbols`) rather than in `sub_440BE0`.

For `libdevice.a(helper.o)` providing a strong `helper_fn`, the sequence is:

1. Archive member extraction pulls in `helper.o` because its symbol table includes a name that matches an existing entry in the output's hash map (`helper_fn`, slot 42). **Note**: nvlink's lazy-loading criterion is "UND symbol in output matches exported symbol in archive member", not "weak symbol in output matches strong in archive member". So `helper.o` is normally **not** pulled in just because of the weak `helper_fn`. It is only pulled in if it either (a) provides some other UND symbol that the output needs, or (b) the linker is invoked with `--whole-archive` on `libdevice.a`.

2. Assume `--whole-archive` is in effect, so `helper.o` is fully merged. During merge, `sub_442CA0` is called for `helper_fn` with `a3 = 1` (STB_GLOBAL).

3. **Line 66**: `sub_449A80` hits the existing weak entry at slot 42, returns pointer, `v8 = 17`.

4. **Line 70**: `*v7 != 0`, goto `LABEL_26`. Line 301: `sub_442820(a1, "helper_fn", visibility, 17)`.

5. Inside `sub_442820`, the weak-resolution logic takes over (documented in [Weak Symbol Handling](weak-symbols.md)). It detects that the incoming symbol has `binding == STB_GLOBAL` and the existing has `binding == STB_WEAK`. The unconditional replacement path emits the verbose trace `"replace weak function %s"` and performs the four cleanup passes:
   - Remove relocations pointing to the old weak definition (`"remove weak reloc"`).
   - Remove `.nv.info` entries for the old weak function.
   - Remove OCG constant sections belonging to the old weak function.
   - Remove debug relocations.

6. The old weak record at positive index 17 is zeroed out (its section assignments are cleared). A new symbol record is created for the strong `helper_fn` and pushed into the **negative** array at index `-3` (the new slot after `__nv_sqrt` at `-2`). The hash map entry at slot 42 is updated from `17` to `-3`.

**State after Step 7**:

```
pos_symbols (ctx+344): [sentinel, ..., helper_fn@17(ZEROED)]
neg_symbols (ctx+352): [sentinel, main_kernel, __nv_sqrt, helper_fn(strong)]
name_map buckets:
    bucket 14 -> [count=1, entries=[55], -1]
    bucket 35 -> [count=1, entries=[42], -1]
    bucket 60 -> [count=1, entries=[29], -1]
entries:
    slot 29: "main_kernel" -> entry_ptr_29 (sym_index = -1)
    slot 42: "helper_fn"   -> entry_ptr_42 (sym_index = -3)  <-- updated
    slot 55: "__nv_sqrt"   -> entry_ptr_55 (sym_index = -2)
name_counter: 3 (unchanged)
```

The zeroed slot at positive index 17 is garbage-collected by dead code elimination (`sub_44AD40`) during the sweep pass -- it is unreachable from any output symbol because the hash map no longer points to it, and no relocation targets it after the `"remove weak reloc"` cleanup.

### Step 8: Final Resolved Symbol Table

After merge, dead code elimination, and section layout, the output ELF symbol table (as it would appear in the final `.symtab`) contains:

| Output idx | Name | Binding | Type | Section | Value | Source |
|---|---|---|---|---|---|---|
| 0 | (none) | LOCAL | NOTYPE | UND | 0 | ELF sentinel |
| 1 | `main_kernel` | GLOBAL | FUNC | `.text` | `0x0000` | `input1.o` |
| 2 | `__nv_sqrt` | GLOBAL | FUNC | `.text` | `0x0080` | `libdevice.a(sqrt.o)` |
| 3 | `helper_fn` | GLOBAL | FUNC | `.text` | `0x00C0` | `libdevice.a(helper.o)` |

The internal negative indices `-1`, `-2`, `-3` have been linearized to output indices `1`, `2`, `3` by the symbol table writer in `sub_45EB00`, which iterates `neg_symbols` in order and assigns sequential output indices. The positive array is skipped in this walkthrough because it contains only section symbols and the zeroed weak `helper_fn` slot, which DCE eliminated.

### Hash Table State Summary

All four states of bucket 35 (where the `helper_fn` contention played out) across the walkthrough:

```
After Step 2:  bucket 35 -> NULL
After Step 4:  bucket 35 -> [count=1, entries=[42(helper_fn weak, idx=17)], -1]
After Step 6:  bucket 35 -> [count=1, entries=[42(helper_fn weak, idx=17)], -1]  (unchanged)
After Step 7:  bucket 35 -> [count=1, entries=[42(helper_fn strong, idx=-3)], -1]
```

Notice that **the bucket structure itself never changes during the weak-to-strong replacement** -- only the `sym_index` field inside the entry node at `entry+8` is updated. This is why `sub_440BE0` re-probes the hash map at line 183 (`v28 = sub_449A80(v21, a2)`) after handling the duplicate case: it needs the entry pointer to write the new index.

### Collision Handling Example

The scenario above has three names that map to distinct buckets, so no in-bucket collisions occur. To illustrate collision resolution, consider what would happen if the input also had a symbol `"my_kernel"`. Its MurmurHash3 is `0xB294C63C`, and at mask `0x3F` the bucket is `0xB294C63C & 0x3F = 60` -- **the same bucket as `main_kernel`**.

When `sub_448E70` inserts `"my_kernel"` at Step 2.5:

1. Line 218 computes hash `0xB294C63C`.
2. Line 225 computes bucket `60`.
3. Line 226 reads the bucket at `map+104 + 8*60`, which is **non-NULL** (holds the bucket from the earlier `main_kernel` insertion).
4. The collision resolution path (lines 393-455) runs. The bucket header at `v42` holds `*v43 = 1` (current entry count in bucket). The entry index at `v43[1] = 29` is the existing `main_kernel` slot, `v43[2] = -1` is the sentinel.
5. Line 397 checks `if (v43[1] == -1)` -- no, it is `29`. Line 404-410 walks the chain counting entries; `j = 0`, so `v47 = 2`, `v46 = 12`, `v48 = 8`.
6. Line 413 checks `if (*v43 < (unsigned int)v47)` -- `1 < 2`, yes, so the bucket needs to grow. Line 418-452 allocates a new bucket header of doubled size: `4 * (2 * 1 + 2) = 16` bytes. It copies the existing entries, sets `*(_DWORD *)nd = v140 = 2 * 1 = 2` (new capacity), appends the new entry's index at offset `v48 = 8`, and writes the `-1` sentinel at offset `v46 = 12`. The old bucket header is freed via `sub_431000(v43, v152)`.
7. Line 471 writes the new bucket pointer into `*v42` (the bucket slot at `map+104 + 8*60`).

After the collision-resolving insertion:

```
bucket 60 -> [capacity=2, entries=[29(main_kernel, -1), N(my_kernel, -2)], -1]
```

Subsequent lookups for either `"main_kernel"` or `"my_kernel"` at bucket 60 walk the entry chain, compare keys via `strcmp` (the function pointer at `map+8` for mode 0 without context), and return the matching slot. Chain walking is implemented at lines 173-187 of `sub_448E70`:

```c
v91 = *(unsigned int **)(map+104 + 8*bucket);   // bucket header
if (v91) {
    while (1) {
        v92 = *++v91;                            // next entry index
        if ((_DWORD)v92 == -1) break;            // end of chain
        v26 = (char **)(map+88 + 16*v92);        // entry at slot v92
        if (name == *v26)                        // pointer-equal keys
            return &v26[1];                      // found: return value ptr
    }
}
```

(The mode 0 path is at lines 218-247 and uses the `strcmp` function pointer rather than pointer equality.)

### Resolution Rules Matrix

The decision table below combines the low-level `sub_440BE0` logic at lines 148-191 with the merge-level weak/strong arbitration in `sub_442820`. Rows indicate the binding of the existing symbol in the output elfw; columns indicate the binding of the incoming symbol. Each cell describes the resulting action and which function implements it.

| Existing \\ Incoming | `STB_LOCAL` | `STB_GLOBAL` | `STB_WEAK` |
|---|---|---|---|
| **(none)** | New entry: allocate record, insert into `ctx+288` at bucket `hash & mask`, push into `pos_symbols`. `sym_index >= 0`. [`sub_440BE0` L194-213] | New entry: same allocation path, push into `neg_symbols`. `sym_index < 0`. [`sub_440BE0` L219-224] | New entry: same allocation path, push into `pos_symbols`. `sym_index >= 0`. [`sub_440BE0` L225-230] |
| **`STB_LOCAL`** | If existing `name_str == NULL`: treat as fresh, allocate new entry. Otherwise: copy `st_name`/`name_str` from existing, re-probe hash map, fall through to populate. [L174-180] | Copy `st_name`/`name_str` from existing, re-probe hash map, push new global into `neg_symbols`, update entry pointer with new index. [L162-168] | Copy `st_name`/`name_str`, re-probe, push new weak into `pos_symbols`, update entry. [L171-173] |
| **`STB_GLOBAL`** (strong def, `st_shndx != 0`) | Should not reach here (locals do not collide with globals on the same name in a well-formed input). Action: copy name, re-probe, push into `pos_symbols`. [L171-180] | **Fatal**: `"adding global symbols of same name"` via `sub_467460` at L164-165. Detected by `(existing.st_info >> 4) == 1`. | Merge level: existing strong wins, incoming weak ignored. The `sub_442820` helper detects this and returns without modification. [see [Weak Symbol Handling](weak-symbols.md)] |
| **`STB_GLOBAL`** (UND, `st_shndx == 0`) | Not applicable (locals cannot fill UND). | **Upgrade in place**: the existing UND slot in `neg_symbols` is updated with the incoming definition's `st_shndx`, `st_value`, `st_size`, `st_info`. Hash map entry pointer unchanged. [short-circuit at `sub_442CA0` L70] | Same upgrade-in-place path; the weak definition fills the UND slot. DCE will not remove it because the hash map entry still points to it. |
| **`STB_WEAK`** | Local replacing weak is not a standard case; the merge loop at `sub_45E7D0` rejects this combination. | **Replace weak**: `sub_442820` emits `"replace weak function %s"`, zeroes the old weak record at its positive index, pushes new strong into `neg_symbols`, updates the hash map entry to point to the new negative index. Runs four cleanup passes. | Weak-vs-weak tie-breaking: `sub_442820` selects the definition with fewer registers (`"replace weak function %s with weak that uses fewer registers"`), or falls back to newer PTX version (`"replace weak function %s with weak from newer PTX"`). See [Weak Symbol Handling](weak-symbols.md) for the full priority order. |

The matrix uses three conceptual layers:

1. **Low-level insertion** (`sub_440BE0` / `sub_442CA0`): handles the hash map / array insertion mechanics and the `"adding global symbols of same name"` fatal check.
2. **Short-circuit for existing entries** (`sub_442CA0` line 70 `goto LABEL_26`): when the hash map already has an entry with a non-zero `sym_index`, the function delegates to `sub_442820` without allocating a new record.
3. **Merge arbitration** (`sub_442820` = `elfw_merge_symbols`): runs the weak/strong/UND resolution policy, performs cleanup when a weak definition is evicted, and updates the hash map entry pointer to the winning symbol.

The call flow is `merge loop -> sub_442CA0 -> (hash probe) -> either (new insertion path) or (LABEL_26 -> sub_442820 -> conflict resolution)`.

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

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_440590` at 0x440590 dispatches on `a2 < 0` to elfw+352 (neg) or elfw+344 (pos) | HIGH | Decompiled: `if (a2 < 0) return sub_464DB0(*(_QWORD *)(a1 + 352), -a2); else return sub_464DB0(*(_QWORD *)(a1 + 344), a2);` |
| `sub_464DB0` (dyn\_array\_get) returns `data[index]` or 0 if out of bounds | HIGH | Decompiled: `if (a1[1] <= a2) return 0; else return *(_QWORD *)(*a1 + 8 * a2);` |
| `sub_464BB0` (dyn\_array\_count) returns `*(arr+8)` | HIGH | Decompiled: `return *(_QWORD *)(a1 + 8);` |
| `sub_464C30` (dyn\_array\_push) appends at count, doubles capacity on overflow | HIGH | Decompiled: `v7 = 2 * v4;` for capacity doubling, `memset` for zero-fill of new region |
| `sub_464AE0` (dyn\_array\_create) allocates 24-byte header + backing array | HIGH | Decompiled: `sub_4307C0(v3, 24)` for header, then `sub_4307C0(v9, 8 * a1)` for data, `memset(v10, 0, 8 * a1)` |
| `sub_4411B0` at 0x4411B0 looks up symbol name at elfw+288 | HIGH | Decompiled: `sub_449A80(*(_QWORD *)(a1 + 288), a2)` returns `*v2` or 0 |
| `sub_4411D0` at 0x4411D0 looks up section name at elfw+296 | HIGH | Decompiled: `sub_449A80(*(_QWORD *)(a1 + 296), a2)` returns `*v2` or 0 |
| `sub_440350` at 0x440350 reads `st_shndx` at sym+6, checks for 0xFFFF | HIGH | Decompiled line 13-14: `result = *(unsigned __int16 *)(a2 + 6); if ((_WORD)result == 0xFFFF)` |
| `sub_440350` three-tier resolution: direct, extended arrays (+592/+600), remap (+456/+464) | HIGH | Decompiled: `a1[75]` (offset 600), `a1[74]` (offset 592), `a1[57]` (offset 456), `a1[58]` (offset 464) all confirmed |
| `sub_440350` emits `"reference to deleted symbol"` on zero remap result | HIGH | Decompiled lines 29, 39: `"reference to deleted symbol"` string; addr 0x1d39dcc in `nvlink_strings.json` |
| `sub_444720` at 0x444720 remaps via tables at elfw+456/+464 | HIGH | Decompiled: `*(_QWORD *)(a1 + 456)` and `*(_QWORD *)(a1 + 464)` with 4-byte stride access confirmed |
| `sub_444720` returns 0 for NULL remap table or index 0 | HIGH | Decompiled line 11-12: `if (!v6 \|\| !a2) return 0;` |
| `sub_440BE0` at 0x440BE0 is 9-parameter function | HIGH | Decompiled: `sub_440BE0(a1, a2, a3, a4, a5, a6, a7, a8, a9)` |
| `sub_440BE0` allocates 48-byte symbol record | HIGH | Decompiled line 131-132: `sub_4307C0(v21, 48)` with triple 128-bit zero stores (3*16=48 bytes) |
| `sub_440BE0` checks callgraph\_completed at elfw+81 | HIGH | Decompiled line 137: `if (*(_BYTE *)(a1 + 81) && a3 == 2)` |
| `sub_440BE0` emits `"adding function after callgraph completed"` | HIGH | Decompiled line 142: string literal confirmed; addr 0x1d39ec8 in `nvlink_strings.json` |
| `sub_440BE0` emits `"adding global symbols of same name"` for global-on-global | HIGH | Decompiled line 164-165: `if (*((_BYTE *)v28 + 4) >> 4 == 1)` then `"adding global symbols of same name"`; addr 0x1d39e60 in `nvlink_strings.json` |
| `sub_440BE0` allocates 12-byte hash map entry node | HIGH | Decompiled line 196: `sub_4307C0(v47, 12)` |
| `sub_440BE0` increments name counter at elfw+304 | HIGH | Decompiled line 201: `++*(_DWORD *)(a1 + 304)` |
| `sub_440BE0` packs st\_info as `(a3 & 0xF) + 16 * a4` | HIGH | Decompiled line 215: `*((_BYTE *)v26 + 4) = (a3 & 0xF) + 16 * a4;` |
| `sub_440BE0` stores value at sym+8 and size at sym+16 | HIGH | Decompiled lines 217-218: `v26[1] = a7;` (QWORD index 1 = offset 8) and `v26[2] = a9;` (offset 16) |
| `sub_440BE0` negative array at +352 for globals, positive at +344 for others | HIGH | Decompiled lines 221-227: `sub_464BB0(*(_QWORD *)(a1 + 352))` for binding==1, `sub_464BB0(*(_QWORD *)(a1 + 344))` otherwise |
| `sub_44B940` at 0x44B940 allocates 64-byte callgraph node | HIGH | Decompiled line 23: `sub_4307C0(v7, 64)` |
| `sub_44B940` stores sym\_index at node+0, uses ordinal at sym+28 | HIGH | Decompiled: `*(_DWORD *)v8 = a2;` (offset 0) and `sub_464DB0(v4, *(unsigned int *)(v3 + 28))` |
| `sub_44B940` registers in callgraph array at elfw+408 | HIGH | Decompiled line 17: `*(_QWORD *)(a1 + 408)` |
| `"STO\_CUDA\_OBSCURE"` string | HIGH | String at addr 0x1d359fb in `nvlink_strings.json` |
| `"secidx not virtual"` string | HIGH | String at addr 0x1d39df8 in `nvlink_strings.json` |
| `"found multiple section symbols for %s"` string | HIGH | String at addr 0x1d39e88 in `nvlink_strings.json` |
| Symbol record 48 bytes with st\_name at +0, st\_info at +4, st\_other at +5, st\_shndx at +6 | HIGH | Confirmed across `sub_440BE0` (alloc size 48, byte accesses at +4, +5, word at +6), `sub_440350` (word at +6), `sub_440590` (dword at +24 for sym\_index) |
| sym\_index at symbol+24, func\_ordinal at symbol+28, name\_str at symbol+32 | HIGH | Decompiled `sub_440BE0`: `*((_DWORD *)v26 + 6) = -v41;` (offset 24), `sub_44B940` reads offset +28, `sub_433870` reads offset +32 for name string |
| `sub_4411F0` emits `"found multiple section symbols for %s"` | MEDIUM | String confirmed in `nvlink_strings.json`; function file exists at correct address; linear scan behavior reconstructed from decompiled structure |
| Extended section index arrays created with initial capacity 0x10000 | MEDIUM | Decompiled `sub_464AE0` confirms it creates arrays with caller-supplied capacity; the 0x10000 argument is inferred from `sub_440BE0` call site patterns |
| Warning level at elfw+624 | MEDIUM | Inferred from `sub_42F850` call pattern in `sub_440BE0`; offset reconstructed from parameter flow analysis |
| Dynamic array 24-byte header: data at +0, count at +8, capacity at +16 | HIGH | Confirmed from `sub_464AE0` (header alloc 24, `*v6 = v10` data ptr, `v6[1] = 0` count, `v6[2] = a1` capacity) and `sub_464BB0` (`return *(a1+8)` for count) and `sub_464DB0` (`a1[1]` for count, `*a1` for data) |

## Cross-References

- [Merge Phase](../pipeline/merge.md) -- symbol addition during input processing
- [Weak Symbol Handling](weak-symbols.md) -- weak resolution policy that drives symbol replacement
- [Dead Code Elimination](dead-code-elimination.md) -- creates the remap tables after removing unreachable symbols
- [Relocation Engine](relocation-engine.md) -- consumer of `elfw_get_symbol` and `elfw_remap_symbol_index`
- [Section Merging](section-merging.md) -- section-level counterpart to symbol resolution
- [Data Layout Optimization](data-layout-opt.md) -- may modify symbol offsets after merge
