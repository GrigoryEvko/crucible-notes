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
        // Virtual section -- resolve through extended tables
        int32_t ext_idx = sec_rec->sym_index;                  // +24 in record
        void* ext_neg = ctx->ext_neg_array;                    // +600

        if (ext_neg == NULL) {
            // No extended arrays yet -- try remap tables
            uint32_t* pos_remap = ctx->pos_remap;             // +456
            if (pos_remap != NULL && ext_idx != 0) {
                if (ext_idx <= 0) {
                    uint32_t abs_idx = -ext_idx;
                    ext_idx = ctx->neg_remap[abs_idx];         // +464
                    if (ext_idx == 0) {
                        fatal("reference to deleted symbol");
                        ext_idx = ctx->neg_remap[abs_idx];     // re-read (defensive)
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
            resolved_shndx = dyn_array_get(ctx->ext_pos_array, ext_idx);  // +592
        } else if (ext_idx >= 0) {
            resolved_shndx = dyn_array_get(ctx->ext_pos_array, ext_idx);  // +592
        } else {
            resolved_shndx = dyn_array_get(ext_neg, -ext_idx);            // +600
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

        // Lazily allocate extended section index arrays
        if (ctx->ext_pos_array == NULL) {                      // +592
            ctx->ext_pos_array = dyn_array_create(0x10000);    // sub_464AE0
            ctx->ext_neg_array = dyn_array_create(0x10000);    // +600
        }

        // Resolve the symbol's own index through remap tables
        // (same three-tier logic as Phase 1, but using sym->sym_index)
        int32_t store_idx = sym->sym_index;                    // dword at sym+24

        if (ctx->ext_neg_array == NULL) {
            // Remap path (ext arrays just created, so this branch is
            // only reachable if dyn_array_create returned +592 non-null
            // but +600 null -- in practice unreachable, but the binary
            // contains this defensive code)
            uint32_t* pos_remap = ctx->pos_remap;             // +456
            if (pos_remap != NULL && store_idx != 0) {
                if (store_idx <= 0) {
                    uint32_t abs_idx = -store_idx;
                    store_idx = ctx->neg_remap[abs_idx];
                    if (store_idx == 0) {
                        fatal("reference to deleted symbol");
                        store_idx = ctx->neg_remap[abs_idx];
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
            dyn_array_set(ctx->ext_pos_array, store_idx, resolved_shndx);
        } else if (store_idx >= 0) {
            dyn_array_set(ctx->ext_pos_array, store_idx, resolved_shndx);
        } else {
            dyn_array_set(ctx->ext_neg_array, -store_idx, resolved_shndx);
        }
    }

    // ---------------------------------------------------------------
    // PHASE 9: STO_CUDA_OBSCURE diagnostic
    // ---------------------------------------------------------------
    // Line 294.  sub_42F850 checks bit 43 of the first dword of the
    // symbol record (which holds st_name at this point).  If set and
    // the warning level (ctx+624) is <= 4, emits "STO_CUDA_OBSCURE".
    //
    // The check: (st_name_dword & 0x80000000000) != 0
    // This tests whether the symbol carries a CUDA-specific visibility
    // flag encoded in the upper bits of st_name (which in nvlink's
    // internal format can carry metadata beyond the ELF string offset).

    check_sto_cuda_obscure(ctx->warning_level, sym->st_name);  // sub_42F850

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
2. **Extended arrays present** (`ctx+600` is non-NULL): use `sym_index` sign to select `+592` (positive) or `+600` (negative), then look up the real section index.
3. **Remap tables present** (`ctx+456` is non-NULL): translate `sym_index` through `+456` (positive remap) or `+464` (negative remap), then look up via `+592`. A zero remap result triggers `"reference to deleted symbol"`.

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

If the resolved section index exceeds `0xFEFF` (but is not the special `0xFFF2` / `SHN_COMMON` marker), the symbol's `st_shndx` is set to `0xFFFF` and the real index is stored in the extended arrays at `+592`/`+600`, keyed by the symbol's signed index. The conditional at line 232 (`v17 <= 0xFEFF || v17 == 65522`) shows that `SHN_COMMON` (65522 = `0xFFF2`) bypasses the extended path -- it is stored directly in `st_shndx`.

The extended arrays are allocated on first use (line 241: `sub_464AE0(0x10000)`) with an initial capacity of 65,536 entries. Both the positive array at `+592` and negative array at `+600` are created together.

For storage, the symbol's newly-assigned `sym_index` determines the array and slot:
- `sym_index >= 0`: store in `ext_pos_array[sym_index]`
- `sym_index < 0`: store in `ext_neg_array[-sym_index]`

### 6. STO_CUDA_OBSCURE Check

After insertion, `sub_42F850` is called with the warning level (`ctx+624`) and the symbol record's first qword (which contains `st_name` in bits 0-31). The check in `sub_42F850` tests bit 43 (`& 0x80000000000`). If set and the warning level is `<= 4`, a diagnostic is emitted:

```c
// sub_42F850 decompiled
if ((sym_dword & 0x80000000000) != 0 && warning_level <= 4) {
    const char* level_str = "";
    if (warning_level >= 1 && warning_level <= 5)
        level_str = warning_level_strings[warning_level - 1];  // off_1D371A0
    diagnostic(&unk_2A5BAB0, "", "STO_CUDA_OBSCURE", level_str);
}
```

`STO_CUDA_OBSCURE` is a CUDA-specific symbol visibility attribute encoded in the `st_other` field's upper bits. The warning level selects from five severity strings stored at `off_1D371A0`. This diagnostic is informational -- it alerts the developer that a symbol carries non-standard ELF visibility metadata specific to CUDA's device code model.

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
