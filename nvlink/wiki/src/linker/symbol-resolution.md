# Symbol Resolution

nvlink manages symbols through a dual-array storage scheme where positive and negative indices address separate arrays, a name-based hash map for O(1) lookup, a virtual section index mechanism for ELFs with more than 65,279 sections, and an old-to-new index remapping system that keeps references stable across dead code elimination. This page documents the storage scheme, the index convention, the name lookup hash map, the section-index resolver, and the DCE remap tables. The detailed insertion algorithm for `sub_440BE0` / `sub_442CA0` lives in the companion page [Symbol Addition](symbol-addition.md); a step-by-step worked example is in [Symbol Resolution Walkthrough](symbol-resolution-walkthrough.md); and the section-symbol resolver `sub_4411F0` is documented in [Extended Symbol Resolution](extended-symbol-resolution.md).

## ELF Writer Symbol Storage

The ELF writer object (the "elfw" context, first argument `a1` in most functions below) stores symbols in two separate dynamic arrays:

| Offset in elfw | Type | Role |
|---|---|---|
| `+344` | `dyn_array*` | **Positive symbol array** — local symbols, section symbols, and non-global definitions. Indexed by non-negative indices (`>= 0`). |
| `+352` | `dyn_array*` | **Negative symbol array** — global and weak symbols. Indexed by negative indices (stored as `-index`). |
| `+288` | `hash_map*` | **Name lookup hash map** — maps symbol name string to signed symbol index. |
| `+296` | `hash_map*` | **Section name lookup hash map** — maps section name string to signed section index. |
| `+304` | `uint32` | **Name counter** — total distinct names registered in the hash map. |
| `+360` | `dyn_array*` | **Section descriptor array** — section records indexed by section index. |
| `+368` | `uint32*` | **Virtual section indirection table** — maps virtual section ordinal to real section index. |
| `+456` | `uint32*` | **Positive remap table** (`symbol_index_mapping`) — old positive symbol index to new symbol index (DCE remap source). |
| `+464` | `uint32*` | **Negative remap table** (`neg_symbol_index_mapping`) — old negative symbol index (absolute value) to new symbol index (DCE remap target). |
| `+592` | `SortedArray*` | **Merged symbol array** (`merged_symbol_array`) — holds the resolved section indices for symbols whose `st_shndx == 0xFFFF`. Indexed by the remapped value from `+456`/`+464`, or directly by `sym+24` when the extended store is present. |
| `+600` | `SortedArray*` | **Extended symbol store** (`extended_symbol_store`) — set during advanced merge paths; when non-NULL, takes precedence over the remap chain and resolves negative `sym+24` values directly. |
| `+624` | `uint32` | **Arch class constant** (`option_parser_result`) — result of `sub_42F8B0`, which returns the constant `5`. The `sub_42F850` / STO\_CUDA\_OBSCURE warning level is sourced elsewhere; this slot is not that counter. |

Each symbol record is a 48-byte structure (allocated via `sub_4307C0` with size 48):

```c
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

`sub_464BB0` returns `*(qword*)(arr + 8)` — the element count. `sub_464C30` appends an element, growing the backing allocation (doubling capacity) when full. The dynamic array primitives are documented in [Symbol Addition](symbol-addition.md#dynamic-array-primitives).

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

Insertion into the hash map is performed by `sub_448E70` (a 611-line function), which handles bucket allocation, rehashing on high load, and collision chain management. See [Hash Tables](hash-tables.md) for the MurmurHash3 implementation and bucket-growth policy.

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

The special value `0xFFF2` (`SHN_COMMON`) bypasses this path entirely — common symbols are stored directly in `st_shndx` without going through `merged_symbol_array`.

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

The remap tables are `uint32_t` arrays (4 bytes per entry), indexed by the absolute value of the old symbol index. A zero entry means the symbol was deleted — any attempt to reference it triggers the `"reference to deleted symbol"` fatal error via `sub_467460`.

This function is called from the relocation engine (`sub_469D60`), from section index resolution (`sub_440350`), and from the extended symbol resolution function (`sub_4411F0` — see [Extended Symbol Resolution](extended-symbol-resolution.md)). The pattern of checking the remap table, falling through to the `"reference to deleted symbol"` error, and then re-reading the table entry (as a defensive measure against the fatal handler returning) is repeated verbatim across all call sites; it is documented in [Symbol Addition](symbol-addition.md#reference-to-deleted-symbol-error).

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x440590` | `elfw_get_symbol` | 73 B | Dispatch symbol index through positive/negative arrays |
| `0x4411B0` | `elfw_find_symbol_by_name` | 51 B | Look up symbol index in name hash map at +288 |
| `0x4411D0` | `elfw_find_section_by_name` | 51 B | Look up section index in name hash map at +296 |
| `0x440350` | `elfw_resolve_section_index` | 579 B | Resolve `st_shndx` with 0xFFFF virtual indirection and remap |
| `0x444720` | `elfw_remap_symbol_index` | 251 B | Translate old symbol index through remap tables at +456/+464 |
| `0x440BE0` | `elfw_add_symbol` | 7,034 B | Add symbol with duplicate detection, hash map, callgraph (see [Symbol Addition](symbol-addition.md)) |
| `0x442CA0` | `elfw_add_function_symbol` | 7,159 B | Add function symbol with UFT merge and callgraph (see [Symbol Addition](symbol-addition.md)) |
| `0x4411F0` | `elfw_resolve_symbol_to_section` | 12,184 B | Extended symbol-to-section resolution (see [Extended Symbol Resolution](extended-symbol-resolution.md)) |
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

- [Symbol Addition](symbol-addition.md) — `sub_440BE0` / `sub_442CA0` insertion algorithm and the `"reference to deleted symbol"` diagnostic path
- [Symbol Resolution Walkthrough](symbol-resolution-walkthrough.md) — end-to-end worked example with hash values, line-level traces, and the resolution-rules matrix
- [Extended Symbol Resolution](extended-symbol-resolution.md) — `sub_4411F0` symbol-to-output-section resolver with the linear-scan fallback
- [Merge Phase](../pipeline/merge.md) — symbol addition during input processing
- [Weak Symbol Handling](weak-symbols.md) — weak resolution policy that drives symbol replacement
- [Dead Code Elimination](dead-code-elimination.md) — creates the remap tables after removing unreachable symbols
- [Relocation Engine](relocation-engine.md) — consumer of `elfw_get_symbol` and `elfw_remap_symbol_index`
- [Section Merging](section-merging.md) — section-level counterpart to symbol resolution
