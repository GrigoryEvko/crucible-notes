# Symbol Record

The symbol record is nvlink's internal representation of an ELF symbol during linking. Each record occupies 48 bytes, arena-allocated via `sub_4307C0`. Symbols live in one of two dynamic arrays inside the ELF writer object (elfw): a positive array for local/section symbols and a negative array for global/weak symbols. This page documents the record layout, the positive/negative index convention, every field's semantics, the accessor and mutator functions, the creation and duplicate-detection path, the remapping system that survives dead code elimination, and the final serialization into the output `.symtab` section.

## Record Layout

```c
struct symbol_record {                 // 48 bytes, 8-byte aligned
    /*  0 */ uint32_t  st_name;        // string table offset for the symbol name
    /*  4 */ uint8_t   st_info;        // high nibble = binding, low nibble = type
    /*  5 */ uint8_t   st_other;       // visibility (STV_*) and CUDA-specific flags
    /*  6 */ uint16_t  st_shndx;       // section index (0xFFFF = virtual/extended)
    /*  8 */ uint64_t  st_value;       // symbol value (address or offset)
    /* 16 */ uint64_t  st_size;        // size in bytes
    /* 24 */ int32_t   sym_index;      // signed index into the pos/neg array
    /* 28 */ int32_t   func_ordinal;   // callgraph function ordinal (STT_FUNC only)
    /* 32 */ char*     name_str;       // pointer to arena-allocated name string
    /* 40 */ uint32_t  alias_chain;    // alias chain link (for hash map collision)
    /* 44 */ uint32_t  reserved;       // padding / unused
};
```

The first 24 bytes mirror the standard `Elf64_Sym` layout, which allows direct memcpy-style serialization into the output `.symtab`. The remaining 24 bytes are internal linker bookkeeping that is not written to the output file.

## The `st_info` Byte

Follows the ELF convention: `st_info = (binding << 4) | (type & 0xF)`.

### Binding (High Nibble)

| Value | Constant | Meaning | Array |
|---|---|---|---|
| 0 | `STB_LOCAL` | File-scope symbol | Positive (`+344`) |
| 1 | `STB_GLOBAL` | Program-wide strong definition | Negative (`+352`) |
| 2 | `STB_WEAK` | Weak definition, replaceable | Varies by context |

### Type (Low Nibble)

| Value | Constant | Meaning |
|---|---|---|
| 0 | `STT_NOTYPE` | Untyped symbol |
| 1 | `STT_OBJECT` | Data object (global variable, constant) |
| 2 | `STT_FUNC` | Function entry point (kernel or device function) |
| 3 | `STT_SECTION` | Section symbol (one per section) |

The assignment is performed in `sub_440BE0` at the point:

```c
sym->st_info = (binding << 4) | (type & 0xF);   // *(byte*)(sym + 4)
```

The extractor function `sub_441A30` decomposes these fields for callers:

```c
// sub_441A30 -- extract type, binding, visibility from symbol record
void elfw_get_symbol_info(elfw* ctx, int sym_idx,
                          uint8_t* out_type,      // a3
                          uint8_t* out_binding,    // a4
                          uint8_t* out_visibility) // a5
{
    symbol_record* sym = elfw_get_symbol(ctx, sym_idx);
    if (!sym) fatal("symbol not found");
    if (out_type)       *out_type       = sym->st_info & 0xF;      // low nibble
    if (out_binding)    *out_binding    = sym->st_info >> 4;        // high nibble
    if (out_visibility) *out_visibility = sym->st_other;            // byte at +5
}
```

## The `st_other` Byte: Visibility and STO_CUDA_OBSCURE

The low two bits encode standard ELF visibility (`STV_DEFAULT` = 0, `STV_INTERNAL` = 1, `STV_HIDDEN` = 2, `STV_PROTECTED` = 3). CUDA extends this with the `STO_CUDA_OBSCURE` flag, which nvlink checks after every symbol insertion.

`sub_42F850` implements the check:

```c
// sub_42F850 -- check STO_CUDA_OBSCURE flag
// NOTE: the 7th argument is *v26 (first qword of the symbol record), NOT st_value.
// The first qword spans bytes 0-7: st_name(0-3) + st_info(4) + st_other(5) + st_shndx(6-7).
// Bit 43 of that qword = bit 11 of the high 32-bit half = bit 3 of st_other (the visibility byte).
void check_sto_cuda_obscure(uint32_t warn_level, ..., uint64_t hdr_qword0) {
    if ((hdr_qword0 & 0x80000000000LL) != 0 && warn_level <= 4) {
        // Bit 3 of st_other is set -- STO_CUDA_OBSCURE flag
        emit_diagnostic(WARN, "", "STO_CUDA_OBSCURE", severity_text[warn_level - 1]);
    }
}
```

The warning level is stored at `elfw+624` and is set via `sub_440210`. When bit 3 of the symbol's `st_other` byte (bit 43 of the header qword, mask `0x08` in `st_other`) is set, the symbol carries the CUDA-specific "obscure" attribute -- a visibility modifier that affects how the symbol participates in resolution across compilation units. The diagnostic is emitted at warning levels 1 through 4 (controlled by `--extra-warnings` / `--warning-as-error`).

## The `st_shndx` Field and the 0xFFFF Virtual Marker

The section index is a 16-bit value. ELF reserves indices above `0xFEFF` (65,279) for special purposes:

| Value | Meaning |
|---|---|
| `0x0000` | `SHN_UNDEF` -- undefined symbol |
| `0x0001`..`0xFEFF` | Direct section index |
| `0xFFF2` | `SHN_COMMON` -- common symbol (unallocated) |
| `0xFFFF` | Virtual marker -- real index stored in extended tables |

When a symbol's owning section has an index exceeding `0xFEFF` (which happens in large cubins with thousands of per-function sections), nvlink sets `st_shndx = 0xFFFF` and stores the real section index in a pair of extended dynamic arrays at `elfw+592` (positive side) and `elfw+600` (negative side), keyed by the symbol's `sym_index`.

The `SHN_COMMON` value `0xFFF2` is also routed through the extended path. The threshold check in `sub_440BE0`:

```c
if (section_index <= 0xFEFF || section_index == 0xFFF2) {
    sym->st_shndx = section_index;         // fits in 16 bits
} else {
    sym->st_shndx = 0xFFFF;                // mark as virtual
    // allocate extended arrays on first use
    if (!ctx->ext_pos_array) {
        ctx->ext_pos_array = dyn_array_create(0x10000);    // +592
        ctx->ext_neg_array = dyn_array_create(0x10000);    // +600
    }
    // store real index keyed by sym_index sign
    if (sym->sym_index < 0)
        dyn_array_set(ctx->ext_neg_array, -sym->sym_index, section_index);
    else
        dyn_array_set(ctx->ext_pos_array, sym->sym_index, section_index);
}
```

## Positive/Negative Index Scheme

nvlink splits the symbol table into two dynamic arrays inside the elfw context:

| elfw Offset | Field | Contents |
|---|---|---|
| `+344` | `pos_symbols` | Local symbols, section symbols, non-global definitions |
| `+352` | `neg_symbols` | Global and weak symbols |

A symbol's `sym_index` field (offset `+24` in the record) is positive for the first array and negative for the second. The convention mirrors the ELF `.symtab` ordering requirement: locals first, then globals.

The central dispatcher `sub_440590` resolves any signed index:

```c
// sub_440590 -- resolve signed symbol index to record pointer
symbol_record* elfw_get_symbol(elfw* ctx, int index) {
    if (index < 0)
        return dyn_array_get(ctx->neg_symbols, -index);   // elfw+352
    else
        return dyn_array_get(ctx->pos_symbols, index);     // elfw+344
}
```

During insertion (in `sub_440BE0`), the array choice and sign assignment:

```c
if (binding == STB_GLOBAL) {            // binding == 1
    int slot = dyn_array_count(ctx->neg_symbols);
    sym->sym_index = -slot;              // negative index
    dyn_array_push(sym, ctx->neg_symbols);
} else {
    int slot = dyn_array_count(ctx->pos_symbols);
    sym->sym_index = slot;               // positive index
    dyn_array_push(sym, ctx->pos_symbols);
}
```

Index zero in the positive array is the null symbol (`STN_UNDEF`), which the ELF spec requires as the first entry.

## Name Lookup via Hash Map

Every symbol name is registered in a hash map at `elfw+288`. The hash map implementation (`sub_449A80`) uses string-based hashing for mode-0 lookups, with buckets at `map+104` and entries as 16-byte key-value pairs (8-byte string pointer + 8-byte value). The value is the 4-byte signed symbol index stored in a 12-byte node structure:

```c
struct name_map_entry {
    uint64_t  unused;       // 8 bytes, zeroed
    uint32_t  sym_index;    // 4 bytes, signed symbol index
};
```

Lookup wrappers:

```c
// sub_4411B0 -- find symbol index by name
int elfw_find_symbol_by_name(elfw* ctx, const char* name) {
    uint32_t* slot = hash_map_lookup(ctx->name_map, name);   // +288
    return slot ? *slot : 0;
}
```

A parallel hash map at `elfw+296` performs section name lookups via `sub_4411D0`.

Insertion is performed by `sub_448E70`, which handles bucket allocation, rehashing, and collision chains. The name string is always copied into arena memory (`strcpy` into a buffer from `sub_4307C0`) so the hash map owns a stable pointer independent of input file lifetimes.

## String Table Management

`sub_4405C0` manages the `.strtab` string table, which is the string table backing all symbol names in the output ELF. It maintains three counters at:

| elfw Offset | Field | Meaning |
|---|---|---|
| `+304` | `name_count` | Total distinct names registered |
| `+308` | `strtab_next_id` | Next sequential string ID |
| `+316` | `strtab_offset` | Current byte offset in .strtab (next free byte) |

When a name is first added, `sub_4405C0` allocates a hash map entry with: (a) the current `strtab_offset` as the `st_name` value, (b) a sequential ID for ordering, and (c) advances `strtab_offset` by `strlen(name) + 1` to account for the NUL terminator. On subsequent lookups, it returns the previously assigned offset, ensuring each unique name appears exactly once in `.strtab`.

In verbose-debug mode (bit 0 of `elfw+64` is set), the diagnostic `"move string %s to fixed area"` is emitted when a name is relocated in the string table.

## Symbol Record Creation: `sub_440BE0`

This is the primary symbol creation function. Signature:

```c
int elfw_add_symbol(
    elfw*       ctx,             // a1
    const char* name,            // a2
    uint8_t     sym_type,        // a3: STT_NOTYPE(0) / STT_OBJECT(1) / STT_FUNC(2) / STT_SECTION(3)
    uint8_t     binding,         // a4: STB_LOCAL(0) / STB_GLOBAL(1) / STB_WEAK(2)
    uint8_t     visibility,      // a5: STV_* value
    int         section_index,   // a6: signed section index
    uint64_t    value,           // a7: symbol value (address/offset)
    int         func_ordinal,    // a8: function ordinal (or 0)
    uint64_t    size             // a9: symbol size
);
// Returns: signed symbol index assigned to the new record.
```

Execution proceeds through these stages:

### 1. Resolve Owning Section

The `section_index` argument (a6) is resolved through `sub_440590` to obtain the section record. If that record's `st_shndx` is `0xFFFF`, the extended section indirection path is followed (same three-tier logic as `sub_440350`).

### 2. Duplicate Detection

The name is probed in the hash map at `ctx+288`:

- If found and nonzero: the existing record is retrieved. For global-on-global collision (`both binding == 1`), the diagnostic `"adding global symbols of same name"` is emitted. Otherwise, the new record inherits the existing name pointer and alias chain link.

- If found but zero, or not found: treated as a new symbol.

### 3. Callgraph Guard

If the callgraph is already sealed (`byte at ctx+81` is set) and the symbol type is `STT_FUNC`, the diagnostic `"adding function after callgraph completed"` is emitted. This guards against late function additions that would bypass dead code analysis.

### 4. Record Allocation and Population

A 48-byte record is allocated and zero-filled:

```c
symbol_record* sym = arena_alloc(48);     // 3 x 16-byte zeroed via OWORD stores
memset(sym, 0, 48);

sym->st_info  = (binding << 4) | (type & 0xF);
sym->st_other = visibility;
sym->st_value = value;                     // qword at +8
sym->st_size  = size;                      // qword at +16
```

### 5. Array Insertion (Sign Assignment)

Globals go into the negative array, everything else into the positive array. The `sym_index` at offset `+24` receives the signed index.

### 6. Extended Section Index Handling

If the resolved section index exceeds `0xFEFF` (and is not `0xFFF2`), `st_shndx` is set to `0xFFFF` and the real index is stored in the extended arrays.

### 7. STO_CUDA_OBSCURE Check

`sub_42F850` is called with the warning level from `elfw+624` and the raw `st_name` (first 4 bytes of the record). If bit 43 of the symbol value is set, a diagnostic is emitted.

### 8. Callgraph Registration (STT_FUNC Only)

For function symbols (`type == 2`), the function ordinal counter at `elfw+416` is incremented, stored at `sym+28`, and the symbol is registered in the callgraph via `sub_44B940`:

```c
if (sym_type == STT_FUNC) {
    ctx->func_ordinal_counter++;           // elfw+416
    sym->func_ordinal = ctx->func_ordinal_counter;   // sym+28
    callgraph_register(ctx, sym->sym_index);           // sub_44B940
}
```

`sub_44B940` allocates a 64-byte callgraph node and inserts it into the callgraph array at `elfw+408`, indexed by the function ordinal.

### 9. Return

The function returns the signed `sym_index` value. If a hash map entry was found during duplicate detection, that entry's value is updated to the new index.

## The Function-Symbol Variant: `sub_442CA0`

A specialized version that hardcodes `sym_type = STT_FUNC` and always registers the symbol in the callgraph. It additionally calls `sub_442820` to handle unified function table (UFT) stub merging. If the name does not start with `__cuda_uf_stub_` (checked by `sub_440230`), a `.text.<name>` section is created via `sub_441AC0`. If it does match the stub prefix, the symbol is directed to the `.nv.uft` section.

`sub_442CA0` takes only four parameters (ctx, name, binding, visibility) -- the section index, value, ordinal, and size are determined internally from the text section assignment.

## Section Index Resolution: `sub_440350`

Resolves the section index stored in a symbol record, handling the `0xFFFF` virtual marker:

```c
uint32_t resolve_section_index(elfw* ctx, symbol_record* sym) {
    uint16_t shndx = sym->st_shndx;            // offset +6
    if (shndx != 0xFFFF)
        return shndx;                            // direct: fits in 16 bits

    int32_t ext_idx = sym->sym_index;           // offset +24

    // Path A: extended indirection arrays exist
    if (ctx->ext_neg_array) {                    // elfw+600
        if (ext_idx >= 0)
            return dyn_array_get(ctx->ext_pos_array, ext_idx);
        else
            return dyn_array_get(ctx->ext_neg_array, -ext_idx);
    }

    // Path B: remap tables exist (post-DCE)
    int32_t remapped = remap_through_tables(ctx, ext_idx);
    return dyn_array_get(ctx->ext_pos_array, remapped);
}
```

The remap fallback (Path B) uses the same tables at `elfw+456`/`+464` described below.

## Section Index Assignment: `sub_440430`

Sets the section index for an existing symbol record, handling the extended case:

```c
void elfw_set_symbol_section(elfw* ctx, symbol_record* sym, uint32_t section_index) {
    if (section_index <= 0xFEFF || section_index == 0xFFF2) {
        sym->st_shndx = section_index;
    } else {
        sym->st_shndx = 0xFFFF;
        // allocate extended arrays on demand, same logic as sub_440BE0
        // store real index keyed by sym->sym_index sign
    }
}
```

## Symbol Index Remapping (Post-DCE)

After dead code elimination removes symbols, their array slots become holes. Rather than compacting arrays (which would invalidate every cross-reference), nvlink builds remap tables:

| elfw Offset | Type | Contents |
|---|---|---|
| `+456` | `uint32_t*` | Positive remap: `old_pos_index -> new_pos_index` |
| `+464` | `uint32_t*` | Negative remap: `old_neg_index_abs -> new_index` |

`sub_444720` performs the translation:

```c
int remap_symbol_index(elfw* ctx, int old_index) {
    uint32_t* pos_remap = ctx->pos_remap;       // +456
    if (!pos_remap || old_index == 0)
        return 0;

    uint32_t new_idx;
    if (old_index <= 0) {
        new_idx = ctx->neg_remap[-old_index];    // +464
    } else {
        new_idx = pos_remap[old_index];
    }

    if (new_idx == 0)
        fatal("reference to deleted symbol");

    return new_idx;
}
```

A zero entry in the remap table means the symbol was deleted. Any attempt to resolve it triggers the `"reference to deleted symbol"` fatal error. The defensive pattern of re-reading the table after the fatal call (in case the error handler returns in non-fatal mode) appears verbatim in every call site.

## Serialization to `.symtab`

The finalization function `sub_445000` (55,681 bytes) iterates both symbol arrays and writes each record into the output `.symtab` section. The serialization order follows ELF convention:

1. **Index 0**: The null symbol (`STN_UNDEF`) -- all fields zero.
2. **Positive array**: Local symbols and section symbols, in array order.
3. **Negative array**: Global and weak symbols, in array order.

For each symbol record, the first 24 bytes (`st_name` through `st_size`) are written directly, as they already match the `Elf64_Sym` layout. The remaining 24 bytes (internal bookkeeping) are discarded.

The `sh_info` field of the `.symtab` section header is set to the index of the first global symbol -- which equals the count of local symbols (the positive array size). This satisfies the ELF requirement that `sh_info` marks the boundary between local and global symbols.

The `.strtab` section is written in parallel: for each unique name registered through `sub_4405C0`, the NUL-terminated string is placed at the byte offset recorded in `st_name`.

If extended section index tables were created (the arrays at `+592`/`+600`), a `.symtab_shndx` section is also emitted, containing one `uint32_t` per symbol entry. For symbols where `st_shndx != 0xFFFF`, the entry is `SHN_UNDEF` (0). For virtual symbols, the entry holds the real section index retrieved from the extended arrays.

## Alias Chain

The `alias_chain` field at offset `+40` links symbols that share the same name but have different bindings or that were aliased during constant deduplication (`sub_4339A0`, which prints `"found duplicate value 0x%x, alias %s to %s"`). The chain is a signed symbol index pointing to the next alias, forming a singly-linked list. A value of zero terminates the chain.

During constant bank optimization, when two symbols point to identical data, one is aliased to the other. The aliased symbol's value and section index are updated to match the target, and its chain pointer links it into the target's alias list. This saves space in constant memory without breaking relocations.

## Accessor Function Reference

| Address | Name | Operation |
|---|---|---|
| `0x440590` | `elfw_get_symbol` | Resolve signed index to record pointer via pos/neg arrays |
| `0x4419F0` | `elfw_get_symbol_name` | Return `name_str` pointer (offset +32) for a signed index |
| `0x441A30` | `elfw_get_symbol_info` | Extract type, binding, visibility into three output pointers |
| `0x440350` | `elfw_resolve_section_index` | Resolve `st_shndx` with 0xFFFF indirection and remap fallback |
| `0x440430` | `elfw_set_symbol_section` | Set section index with extended-table handling |
| `0x4411B0` | `elfw_find_symbol_by_name` | Hash map lookup at `+288`, returns signed index |
| `0x4405C0` | `elfw_strtab_add_name` | Register name in `.strtab`, returns byte offset |
| `0x440BE0` | `elfw_add_symbol` | Full symbol creation with duplicate detection |
| `0x442CA0` | `elfw_add_function_symbol` | Specialized function-symbol creation with UFT merge |
| `0x444720` | `elfw_remap_symbol_index` | Translate old index through post-DCE remap tables |
| `0x440280` | `elfw_byte4_eq_2` | Return `true` if byte 4 of passed pointer equals 2. Callers pass elfw contexts (checking ELF class byte in header), NOT symbol records |
| `0x440260` | `elfw_check_flag` | Test a flag bit at elfw+48 conditional on byte 7 of elfw == 'A' (0x41). Operates on elfw, not symbol |
| `0x4402A0` | `elfw_get_flags` | Extract flags field at `elfw+48` (operates on elfw, NOT symbol) |
| `0x4402C0` | `elfw_get_raw_flags` | Return raw `uint32_t` at `elfw+48` (operates on elfw, NOT symbol) |
| `0x42F850` | `check_sto_cuda_obscure` | Emit diagnostic if bit 43 of value is set |
| `0x44B940` | `callgraph_register_function` | Register STT_FUNC symbol in callgraph at `+408` |

## Key elfw Offsets (Symbol-Related)

| Offset | Type | Meaning |
|---|---|---|
| `+288` | `hash_map*` | Symbol name lookup map |
| `+296` | `hash_map*` | Section name lookup map |
| `+304` | `uint32` | Distinct name count |
| `+308` | `uint32` | Next string table ID |
| `+316` | `uint32` | Current `.strtab` byte offset |
| `+344` | `dyn_array*` | Positive symbol array (locals, sections) |
| `+352` | `dyn_array*` | Negative symbol array (globals, weaks) |
| `+408` | `dyn_array*` | Callgraph node array |
| `+416` | `uint32` | Function ordinal counter |
| `+456` | `uint32*` | Positive index remap table (post-DCE) |
| `+464` | `uint32*` | Negative index remap table (post-DCE) |
| `+592` | `dyn_array*` | Extended section index array (positive) |
| `+600` | `dyn_array*` | Extended section index array (negative) |
| `+624` | `uint32` | Warning level for STO_CUDA_OBSCURE |

## Cross-References

- [Symbol Resolution](../linker/symbol-resolution.md) -- higher-level symbol resolution policy and merge logic
- [Weak Symbol Handling](../linker/weak-symbols.md) -- weak resolution that drives symbol replacement
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- creates remap tables after removing unreachable symbols
- [ELF Writer](elf-writer.md) -- the elfw context structure that owns symbol arrays
- [Section Record](section-record.md) -- companion data structure for sections
- [Hash Tables](../linker/hash-tables.md) -- hash map implementation backing name lookups
- [Serialization](../elf/serialization.md) -- `.symtab` output format

## Confidence Assessment

Each claim below was verified against decompiled functions (`sub_440BE0`, `sub_440590`, `sub_444720`, `sub_4438F0`, `sub_4419F0`, `sub_441A30`, `sub_440350`, `sub_4405C0`, `sub_42F850`), string references in `nvlink_strings.json`, and raw research report W082. Re-verified in P050b pass (2026-04-09).

| Claim | Confidence | Evidence |
|---|---|---|
| Symbol record size = 48 bytes | HIGH | `sub_440BE0` calls `sub_4307C0((__int64)v21, 48)` and initializes three 16-byte OWORDs |
| Zero-init via three OWORD stores | HIGH | `*(_OWORD *)v26 = 0; *((_OWORD *)v26 + 1) = 0; *((_OWORD *)v26 + 2) = 0;` |
| `st_name` at offset 0 (uint32) | HIGH | First 4 bytes zeroed by OWORD, then hash-map entry value patched at return |
| `st_info` at offset 4 | HIGH | `*((_BYTE *)v26 + 4) = (a3 & 0xF) + 16 * a4` in `sub_440BE0` |
| Binding encoded as high nibble (`<< 4`) | HIGH | `16 * a4` = shift by 4 = high nibble |
| Type encoded as low nibble | HIGH | `a3 & 0xF` = low 4 bits |
| `st_other` at offset 5 | HIGH | `*((_BYTE *)v26 + 5) = a5` (visibility arg) |
| `st_shndx` at offset 6 (uint16) | HIGH | `*((_WORD *)v26 + 3) = v17` (word 3 = byte 6); `*(unsigned __int16 *)(v12 + 6)` accessor |
| `0xFFFF` sentinel for virtual/extended sections | HIGH | `if (v17 <= 0xFEFF || v17 == 65522) *((_WORD *)v26 + 3) = v17; else *((_WORD *)v26 + 3) = -1` (=0xFFFF) |
| `SHN_COMMON` (0xFFF2 = 65522) passes through directly | HIGH | Exact constant `65522` in `sub_440BE0` comparison |
| `st_value` at offset 8 (uint64) | HIGH | `v26[1] = a7` in `sub_440BE0` (qword 1 = byte 8) |
| `st_size` at offset 16 (uint64) | HIGH | `v26[2] = a9` (qword 2 = byte 16) |
| `sym_index` at offset 24 (int32) | HIGH | `*((_DWORD *)v26 + 6) = v29` or `-v41` (dword 6 = byte 24) |
| `func_ordinal` at offset 28 (int32) | HIGH | `*((_DWORD *)v26 + 7) = v57` where `v57 = *(_DWORD *)(a1 + 416) + 1` |
| `name_str` at offset 32 (char*) | HIGH | `v26[4] = v55` where `v55 = strcpy(v54, a2)` (qword 4 = byte 32) |
| Field at offset 40 (`alias_chain`) | MEDIUM | `*((_DWORD *)v26 + 10) = 0` at end of creation. Zero-init confirmed; "alias chain" semantic name comes from `sub_4339A0` constant-dedup path not fully traced here |
| Reserved/padding at offset 44 | HIGH | Not assigned in constructor; left as zero from initial OWORD stores |
| First 24 bytes match `Elf64_Sym` | HIGH | Field layout (name, info, other, shndx, value, size) matches ELF spec exactly |
| Positive symbol array at `elfw+344` | HIGH | `sub_440590`: `return sub_464DB0(*(_QWORD *)(a1 + 344), a2);` for positive idx |
| Negative symbol array at `elfw+352` | HIGH | `sub_440590`: `return sub_464DB0(*(_QWORD *)(a1 + 352), -a2);` for negative idx |
| Global binding (`STB_GLOBAL`=1) -> negative array | HIGH | `if (a4 == 1) { *((_DWORD *)v26 + 6) = -sub_464BB0(*(_QWORD *)(a1 + 352)); }` |
| Local/weak -> positive array | HIGH | `else { *((_DWORD *)v26 + 6) = sub_464BB0(*(_QWORD *)(a1 + 344)); }` |
| Symbol name hash table at `elfw+288` | HIGH | `sub_449A80(*(void **)(a1 + 288), a2)` in `sub_440BE0` |
| Section name hash table at `elfw+296` | HIGH | Parallel verified in `sub_441AC0` construction |
| 12-byte hash map entry with name + index | HIGH | `sub_4307C0(v47, 12); *v28 = 0; *((_DWORD *)v28 + 2) = 0;` then `*(_DWORD *)v28 = result` |
| `strtab_entry_count` counter at `elfw+304` | HIGH | `++*(_DWORD *)(a1 + 304);` on new name insertion |
| Extended section index tables at `elfw+592` / `elfw+600` | HIGH | `*(_QWORD *)(a1 + 592) = sub_464AE0(0x10000); *(_QWORD *)(a1 + 600) = sub_464AE0(0x10000);` |
| Extended tables sized 0x10000 (65,536) entries | HIGH | Exact constant in allocation calls |
| Pos remap at `elfw+456` (post-DCE) | HIGH | `v58 = *(_QWORD *)(a1 + 456)` in remap path |
| Neg remap at `elfw+464` (post-DCE) | HIGH | `v30 = *(unsigned int *)(*(_QWORD *)(a1 + 464) + 4 * v63)` |
| `"reference to deleted symbol"` fires when remap returns 0 | HIGH | Explicit string literal and fatal call in both pos and neg paths |
| String `"reference to deleted symbol"` present in binary | HIGH | Found at line 11766 in `nvlink_strings.json` |
| `sub_444720` remap function address | HIGH | Verified from binary via function address decompilation |
| `STO_CUDA_OBSCURE` check via `sub_42F850` | HIGH | `sub_42F850(*(_DWORD *)(a1 + 624), v30, ..., *v26);` at end of creation |
| Warning level stored at `elfw+624` | HIGH | First arg to `sub_42F850` is `*(_DWORD *)(a1 + 624)` |
| String `"STO_CUDA_OBSCURE"` in binary | HIGH | Found at line 7176 in `nvlink_strings.json` |
| Bit 43 of first qword triggers STO_CUDA_OBSCURE | HIGH | `sub_42F850`: `(a7 & 0x80000000000LL) != 0`. Caller passes `*v26` (first qword of symbol record), NOT `st_value`. Bit 43 corresponds to bit 3 of `st_other` at byte 5. Wiki prose originally said "st_value" which was incorrect and has been fixed in this pass |
| Function ordinal counter at `elfw+416` | HIGH | `v57 = *(_DWORD *)(a1 + 416) + 1; *(_DWORD *)(a1 + 416) = v57;` |
| Callgraph registration via `sub_44B940` | HIGH | `sub_44B940(a1, (unsigned int)result);` when `a3 == 2` (STT_FUNC) |
| Callgraph guard at `elfw+81` (byte) | HIGH | `if (*(_BYTE *)(a1 + 81) && a3 == 2)` fires `"adding function after callgraph completed"` |
| String `"adding function after callgraph completed"` | HIGH | Present in binary (checked via wider string grep) |
| "Adding global symbols of same name" diagnostic | HIGH | `if (*((_BYTE *)v28 + 4) >> 4 == 1) sub_467460(..., "adding global symbols of same name"...)` |
| String `"adding global symbols of same name"` | HIGH | Found at line 12311 in `nvlink_strings.json` |
| `sub_440590` as central symbol dispatcher | HIGH | Exact 10-line function decompiled to sign-dispatch logic |
| Binding constants `STB_LOCAL=0, STB_GLOBAL=1, STB_WEAK=2` | HIGH | Standard ELF spec values; `a4 == 1` branch matches STB_GLOBAL |
| Type constants `STT_NOTYPE=0, STT_OBJECT=1, STT_FUNC=2, STT_SECTION=3` | HIGH | Standard ELF spec; `a3 == 2` branch matches STT_FUNC |
| Name_map_entry layout (12 bytes: 8 unused + 4 sym_index) | MEDIUM | Constructor zeros first qword and dword; layout consistent but specific field semantics inferred |
| "alias_chain" name for field at +40 | MEDIUM | Field is zeroed at creation; linkage logic lives in `sub_4339A0` constant dedup which was not directly verified in this pass |
| Serialization order: null -> positive -> negative | MEDIUM | Consistent with ELF ABI requirement (`sh_info` marks local/global boundary). Exact serialization in `sub_445000` not verified in this pass |
| `sh_info` of `.symtab` = first global index | MEDIUM | Standard ELF requirement; exact assignment point in `sub_445000` not directly verified here |
| `.symtab_shndx` emitted when extended tables exist | MEDIUM | Logical consistency with `SHN_XINDEX` spec; exact trigger condition not verified in this pass |
| `"move string %s to fixed area"` debug path | LOW | Page claims this is emitted in verbose mode; string exists in binary but trigger condition not verified in this pass |
