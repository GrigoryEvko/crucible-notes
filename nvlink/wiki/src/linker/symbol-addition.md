# Symbol Addition

This page documents `sub_440BE0` (`elfw_add_symbol`) and its function-symbol variant `sub_442CA0` (`elfw_add_function_symbol`), the two entry points through which every symbol enters the linker's output ELF. It companions [Symbol Resolution](symbol-resolution.md), which covers the underlying storage scheme, hashing, and section-index machinery; this page focuses on the twelve-phase insertion algorithm, the in-place duplicate-detection logic, and the supporting primitives (dynamic arrays, the "reference to deleted symbol" diagnostic path). The walkthrough of a concrete multi-input scenario lives in [Symbol Resolution Walkthrough](symbol-resolution-walkthrough.md).

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

> ⚡ **QUIRK — dead diagnostic, live plumbing**
> The STO\_CUDA\_OBSCURE path is wired through every symbol addition but can never fire, because the warning-level seed at `ctx+624` is a compile-time constant `5` and the guard is `<= 4`. The binary still pays for the call on every symbol and keeps the diagnostic string in `.rodata`; only the user-visible warning is dead.

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

## Cross-References

- [Symbol Resolution](symbol-resolution.md) -- storage scheme, hashing, and section-index machinery that this insertion path drives
- [Symbol Resolution Walkthrough](symbol-resolution-walkthrough.md) -- worked example tracing `sub_440BE0` and `sub_442CA0` end-to-end
- [Extended Symbol Resolution](extended-symbol-resolution.md) -- `sub_4411F0` (the section-symbol fallback consumer of the structures populated here)
- [Weak Symbol Handling](weak-symbols.md) -- the `sub_442820` merge helper invoked from the function-symbol variant
- [Dead Code Elimination](dead-code-elimination.md) -- builds the remap tables whose zero entries trigger `"reference to deleted symbol"`
