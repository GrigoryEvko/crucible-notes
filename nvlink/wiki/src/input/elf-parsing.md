# ELF Parsing (Elf32 / Elf64)

nvlink operates directly on in-memory ELF images. Every input cubin — whether loaded from disk, extracted from a fatbin, or produced by the embedded ptxas — is a complete ELF file mapped into an arena-allocated buffer. The linker never uses `libelf` or any external ELF library; it implements its own accessor functions that interpret raw header bytes at fixed offsets from the buffer base. There are two parallel sets of accessors: one for Elf64 (the normal case for 64-bit CUDA targets) and one for Elf32 (the legacy path, e.g. the OSABI `0x33` 32-bit device ABI). Both classes are accepted by the disk loaders and the structural validator; the class byte at `e_ident[EI_CLASS]` (offset 4 from the ELF base) selects which accessor family is used rather than gating acceptance. Modern targets (sm\_75 and above as registered in the active profile database) emit Elf64 in practice, but the parser will walk an Elf32 cubin just as readily.

## Key Facts

| Property | Value |
|---|---|
| Class selector | `e_ident[4]` (EI_CLASS): `2` = Elf64, anything else = Elf32 |
| Endianness | Little-endian only; `e_ident[EI_DATA] == 1` is enforced by the disk loaders (`sub_43E100`, `sub_43DFC0`) but not re-checked by the accessor family or `sub_43DD30` |
| Elf64 section header size | 64 bytes (`e_shentsize` at offset 58, checked == 64) |
| Elf64 program header size | 56 bytes (`e_phentsize` at offset 54, checked == 56) |
| Elf64 symbol entry size | 24 bytes (hardcoded stride, not read from `sh_entsize`) |
| Elf32 section header size | 40 bytes (`e_shentsize` at offset 46, checked == 40) |
| Elf32 program header size | 32 bytes (`e_phentsize` at offset 42, checked == 32) |
| File loading | `sub_476BF0` — fopen/fseek/ftell/fread into arena buffer |
| ELF validation | `sub_43DD30` — bounds-checks all headers and sections |
| ELF magic check | `sub_43D970` — tests `*(uint32_t*)base == 0x464C457F` |
| ELF class check | `sub_43D9A0` — tests `e_ident[EI_CLASS] == 2` |
| REL-type check | `sub_43D9B0` — tests `e_type == ET_REL (1)` |
| Symbol access (Elf64) | `sub_448600` / `sub_4486A0` / `sub_448750` |
| String access (Elf64) | `sub_448590` (resolves through `e_shstrndx` strtab) |
| Symbol access (Elf32) | None — open-coded in callers |

## Confidence Tags for Core Claims

| Claim | Confidence | Evidence |
|---|---|---|
| Class selector at `e_ident[4]`: `2`=Elf64, else=Elf32 | HIGH | Byte-load-and-compare at offset 4 visible at `sub_43D9A0`, `sub_43DD30`, `sub_43DA40`, `sub_43DA80`, and `merge_elf` (`sub_45E7D0`) |
| ELF magic check tests `*(uint32_t*)base == 0x464C457F` | HIGH | Literal `0x464C457F` in `sub_43D970`; matches `\x7fELF` little-endian |
| Elf64 section-header size = 64 bytes, program-header size = 56 bytes (checked exactly) | HIGH | `e_shentsize` at +58 compared `== 64`, `e_phentsize` at +54 compared `== 56` in `sub_43DD30` |
| Elf64 symbol entry stride = 24 bytes (hardcoded, not from `sh_entsize`) | HIGH | Symbol accessors `sub_448600`/`sub_4486A0`/`sub_448750` multiply index by 24 directly |
| `EI_DATA` validated only by disk-loader entry points (`sub_43E100`, `sub_43DFC0`); accessor/validator family ignores it | HIGH | `sub_43E100` line 45 and `sub_43DFC0` line 46 test `e_ident[5] == 1` (ELFDATA2LSB); the structural validator `sub_43DD30` and every Elf64/Elf32 accessor read `e_ident[4]` only. ELFs that bypass the disk loaders (e.g. embedded fatbin payloads) are never re-checked for endianness |
| File loading via fopen/fseek/ftell/fread in `sub_476BF0`, no `mmap` for ELF input | HIGH | Direct PLT imports for `fopen`/`fread`/`fseek`/`ftell`/`fclose` visible in disassembly of `sub_476BF0`; no `mmap` xref from the same function |
| `null_terminate` flag distinguishes binary inputs from PTX text input | HIGH | Parameter branch writes `((char*)buf)[size]='\0'` only when flag is set |
| No size-limit check before arena allocation | HIGH | Result of `ftell` is passed straight to `arena_alloc`; no bounds compare visible |

## File Loading: `sub_476BF0`

Before any ELF parsing occurs, the file must be loaded into memory. nvlink reads entire files into contiguous arena-allocated buffers using standard C I/O — there is no `mmap` usage for ELF inputs.

```c
// sub_476BF0 -- read_entire_file(filename, null_terminate)
// Address: 0x476BF0
QWORD *read_entire_file(const char *filename, char null_terminate)
{
    FILE *fp = fopen(filename, "rb");
    if (!fp)
        error_emit(ERROR_CANNOT_OPEN, filename);   // fatal

    fseek(fp, 0, SEEK_END);           // seek to end
    size_t size = ftell(fp);           // get file size
    fseek(fp, 0, SEEK_SET);           // seek back to start

    void *arena = get_arena_metadata(fp);  // sub_44F410 -- get owning arena
    void *buf = arena_alloc(arena, size + (null_terminate ? 1 : 0));

    if (fread(buf, 1, size, fp) != size)
        error_emit(ERROR_READ_FAILED, filename);   // fatal, but continues

    fclose(fp);

    if (null_terminate)
        ((char *)buf)[size] = '\0';    // NUL-terminate for text files (PTX)

    return buf;
}
```

The `null_terminate` parameter distinguishes binary inputs (cubins, fatbins) from text inputs (PTX source files). When set, an extra byte is allocated and zeroed at the end. The entire file lives in a single arena allocation, so no explicit `free` is needed — the arena handles lifetime.

There is no size limit check. The file size comes from `ftell` and is passed directly to `arena_alloc`. For a 4 GB cubin, this would attempt a 4 GB allocation from the arena, which would fall through to the `mmap` path in the arena allocator (`sub_44ED60`).

## ELF Class Dispatch

Throughout the linker, the ELF class determines which accessor set is used. The dispatch is always a check of the byte at offset 4 from the ELF base:

```c
if (*(uint8_t *)(elf_base + 4) == 2)    // ELFCLASS64
    // use Elf64 accessors (sub_4483xx family)
else
    // use Elf32 accessors (sub_46B5xx family)
```

This check appears in `sub_43DD30` (validation), `sub_43DA40` (Mercury capability detection), `sub_43DA80` (ELF extent computation), and throughout `merge_elf` at `sub_45E7D0`.

## Elf64 Accessor Functions

All Elf64 accessors live in a tight address cluster at `0x448360`--`0x448730`. They take a raw pointer to the start of the ELF image (the `\x7fELF` magic) and compute offsets directly from the Elf64 header layout.

### Elf64 Header Layout (offsets used in the binary)

| Offset | Size | Elf64 field | nvlink usage |
|---|---|---|---|
| 0 | 4 | `e_ident[0..3]` | Magic check: `0x7F454C46` |
| 4 | 1 | `e_ident[EI_CLASS]` | Class dispatch: `2` = 64-bit |
| 7 | 1 | `e_ident[EI_OSABI]` | Mercury detection: `0x41` = device ABI (checked in `sub_43DA40`) |
| 32 | 8 | `e_phoff` | Program header table offset |
| 40 | 8 | `e_shoff` | Section header table offset |
| 48 | 4 | `e_flags` | ELF flags (Mercury capability bits checked in `sub_43DA40`) |
| 54 | 2 | `e_phentsize` | Program header entry size (validated == 56) |
| 56 | 2 | `e_phnum` | Number of program headers |
| 58 | 2 | `e_shentsize` | Section header entry size (validated == 64) |
| 60 | 2 | `e_shnum` | Number of section headers (0 = extended) |
| 62 | 2 | `e_shstrndx` | Section name string table index (0xFFFF = extended) |

### `sub_448360` — `elf64_header`

```c
// Returns pointer to the Elf64 header (identity function -- base IS the header)
void *elf64_header(void *elf_base) { return elf_base; }
```

This is a trivial identity function compiled to a single `mov rax, rdi; ret`. It exists as a named abstraction in the source — a function-pointer table or vtable dispatches through it. The Elf32 counterpart `sub_46B590` is identical.

### `sub_448370` — `elf64_section_by_index`

Returns a pointer to the section header at index `idx`, or NULL if out of bounds.

```c
// sub_448370 -- elf64_section_by_index(elf_base, idx)
// Address: 0x448370
void *elf64_section_by_index(void *elf_base, uint32_t idx)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 60);   // e_shnum

    if (shnum == 0) {
        // Extended section numbering: real count in section[0].sh_size
        uint64_t shoff = *(uint64_t *)(elf_base + 40);
        void *shdr0 = elf_base + shoff;
        if (!shdr0) return NULL;
        shnum = *(uint32_t *)(shdr0 + 32);           // section[0].sh_size
    }

    if (idx >= shnum)
        return NULL;

    uint16_t shentsize = *(uint16_t *)(elf_base + 58);  // e_shentsize
    uint64_t shoff     = *(uint64_t *)(elf_base + 40);   // e_shoff
    return elf_base + shoff + shentsize * idx;
}
```

The function handles two cases:

1. **Normal numbering** (`e_shnum != 0`): The section count is directly in the header. The section header at index `idx` is at `base + e_shoff + e_shentsize * idx`.

2. **Extended numbering** (`e_shnum == 0`): Per the ELF specification, when the section count exceeds 0xFFFF, `e_shnum` is set to 0 and the real count is stored in `section[0].sh_size` (a 32-bit field at offset 32 within the first Elf64 section header). The function reads this field from `base + e_shoff + 32`.

### `sub_448730` — `elf64_section_count`

```c
// sub_448730 -- elf64_section_count(elf_base)
// Address: 0x448730
uint32_t elf64_section_count(void *elf_base)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 60);

    if (shnum == 0) {
        uint64_t shoff = *(uint64_t *)(elf_base + 40);
        void *shdr0 = elf_base + shoff;
        if (shdr0)
            return *(uint32_t *)(shdr0 + 32);  // section[0].sh_size
        return 0;
    }

    return shnum;
}
```

Same extended-numbering logic as `elf64_section_by_index`. Returns a `uint32_t` (not `uint16_t`) to support the extended range.

### `sub_4483B0` — `elf64_section_by_name`

Iterates all section headers, resolving each section's name through the section header string table (`shstrtab`), and returns a pointer to the first section header whose name matches the search string.

```c
// sub_4483B0 -- elf64_section_by_name(elf_base, name)
// Address: 0x4483B0
void *elf64_section_by_name(void *elf_base, const char *name)
{
    uint16_t shnum   = *(uint16_t *)(elf_base + 60);
    uint64_t shoff   = *(uint64_t *)(elf_base + 40);
    void    *shdr0   = elf_base + shoff;
    uint16_t shentsz = *(uint16_t *)(elf_base + 58);

    uint32_t count;
    if (shnum != 0)
        count = shnum;
    else if (shdr0)
        count = *(uint32_t *)(shdr0 + 32);   // extended: section[0].sh_size
    else
        return NULL;

    // Resolve shstrndx (handles 0xFFFF extended index)
    uint16_t shstrndx_raw = *(uint16_t *)(elf_base + 62);
    uint32_t shstrndx = shstrndx_raw;
    if (shstrndx_raw == 0xFFFF)
        shstrndx = *(uint32_t *)(shdr0 + 40);  // section[0].sh_link (extended)

    // Get shstrtab section header
    void *strtab_shdr = elf_base + shoff + shentsz * shstrndx;

    for (uint32_t i = 0; i < count; i++) {
        void *shdr = elf_base + shoff + shentsz * i;

        // Validate shstrtab type is SHT_STRTAB (3)
        if (*(uint32_t *)(strtab_shdr + 4) != 3)   // sh_type
            continue;

        uint32_t sh_name = *(uint32_t *)shdr;       // sh_name offset
        uint64_t strtab_size = *(uint64_t *)(strtab_shdr + 32);  // sh_size
        if (sh_name >= strtab_size)
            continue;

        uint64_t strtab_off = *(uint64_t *)(strtab_shdr + 24);   // sh_offset
        const char *section_name = (const char *)(elf_base + strtab_off + sh_name);

        if (strcmp(section_name, name) == 0)
            return shdr;
    }

    return NULL;
}
```

Key details:

- **Extended `e_shstrndx`**: When `e_shstrndx == 0xFFFF`, the real string table index is in `section[0].sh_link` (offset 40 within the first section header). This is the standard ELF extended numbering mechanism.

- **SHT_STRTAB check**: The function validates that the resolved string table section has `sh_type == 3` (`SHT_STRTAB`) before using it. If the type is wrong, the iteration skips that section (does not abort).

- **Bounds check on `sh_name`**: The section's `sh_name` field is checked against the string table's `sh_size` before indexing into it. This prevents out-of-bounds reads on malformed ELFs.

- **Linear scan**: The search is O(n) in the number of sections. nvlink does not build a hash table over section names for input ELFs — this function is called frequently during merge but the section counts in cubins are typically small (tens to low hundreds).

### `sub_448560` — `elf64_section_data`

```c
// sub_448560 -- elf64_section_data(elf_base, section_header_ptr)
// Address: 0x448560
void *elf64_section_data(void *elf_base, void *shdr)
{
    if (!shdr) return NULL;
    uint64_t sh_offset = *(uint64_t *)(shdr + 24);  // sh_offset
    return elf_base + sh_offset;
}
```

Returns a pointer to the raw section data within the in-memory ELF image. The section header's `sh_offset` field is an offset from the start of the file (and therefore from the start of the buffer, since the entire file is loaded contiguously). No bounds checking is performed here — that is handled by the validation function.

### `sub_448580` — `elf64_section_size`

```c
// sub_448580 -- elf64_section_size(elf_base, shdr)
// Address: 0x448580
uint64_t elf64_section_size(void *elf_base, void *shdr)
{
    if (!shdr) return 0;
    return *(uint64_t *)(shdr + 32);    // sh_size
}
```

The first argument `elf_base` is unused (it is retained for symmetry with the `section_data` accessor signature). Returns zero for a null shdr pointer; otherwise returns the raw `sh_size` field. No validation against file extent.

### `sub_4484F0` — `elf64_section_by_type`

Finds the first section with a matching `sh_type`. This is how `merge_elf` locates `SHT_SYMTAB`, `SHT_STRTAB`, and the `SHT_SYMTAB_SHNDX` section when their indices are not known in advance.

```c
// sub_4484F0 -- elf64_section_by_type(elf_base, sh_type)
// Address: 0x4484F0
void *elf64_section_by_type(void *elf_base, uint32_t sh_type)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 60);      // e_shnum
    void    *shdr  = elf_base + *(uint64_t *)(elf_base + 40);  // e_shoff

    uint32_t count;
    if (shnum != 0) {
        count = shnum;
    } else if (shdr) {
        count = *(uint32_t *)(shdr + 32);               // extended: section[0].sh_size
    } else {
        return NULL;
    }

    for (uint32_t i = 0; i < count; i++) {
        if (*(uint32_t *)(shdr + 4) == sh_type)         // sh_type
            return shdr;
        shdr += 64;                                      // Elf64 shdr stride
    }
    return NULL;
}
```

- **O(n) linear scan**. Same performance concern as `section_by_name`, but called even less frequently (once per symtab lookup per input object, not once per section name lookup per input).

- **Hardcoded stride of 64**. The function does not read `e_shentsize` here — it assumes the validated ELF layout has 64-byte section headers, which is guaranteed by `elf_validate` rejecting anything else upstream.

- **Returns the first match**. ELF files can technically contain multiple sections of the same type (multiple `SHT_PROGBITS` are normal; multiple `SHT_SYMTAB` are not). For linker input cubins, `SHT_SYMTAB` and `SHT_SYMTAB_SHNDX` appear at most once.

### `sub_448590` — `elf64_string_at`

Resolves a string by offset through the **section-header string table** referenced by `e_shstrndx`. Despite the name, this function is used for both section name lookups and general-purpose string access where the caller has a 32-bit offset into the shstrtab.

```c
// sub_448590 -- elf64_string_at(elf_base, name_ptr)
// Address: 0x448590
//
// Note: the second argument is a POINTER to a u32 offset, not the offset itself.
// Callers typically pass a pointer into a section header's sh_name field or
// into a symbol table entry's st_name field.
const char *elf64_string_at(void *elf_base, uint32_t *name_ptr)
{
    if (!elf_base || !name_ptr) return NULL;

    // Resolve shstrndx with extended numbering fallback
    uint32_t shstrndx = *(uint16_t *)(elf_base + 62);
    if (shstrndx == 0xFFFF) {
        void *shdr0 = elf_base + *(uint64_t *)(elf_base + 40);
        shstrndx = *(uint32_t *)(shdr0 + 40);    // shdr[0].sh_link
    }

    // Resolve section count (extended numbering fallback)
    uint32_t shnum = *(uint16_t *)(elf_base + 60);
    if (shnum == 0) {
        void *shdr0 = elf_base + *(uint64_t *)(elf_base + 40);
        if (!shdr0) return NULL;
        shnum = *(uint32_t *)(shdr0 + 32);        // shdr[0].sh_size
    }

    if (shstrndx >= shnum) return NULL;

    // Fetch the shstrtab section header
    void *strtab_shdr = elf_base
                      + *(uint64_t *)(elf_base + 40)
                      + shstrndx * *(uint16_t *)(elf_base + 58);
    if (!strtab_shdr) return NULL;

    // Validate it really is a string table (SHT_STRTAB = 3)
    if (*(uint32_t *)(strtab_shdr + 4) != 3)       // sh_type
        return NULL;

    uint32_t name_off = *name_ptr;
    uint64_t strtab_size = *(uint64_t *)(strtab_shdr + 32);   // sh_size
    if (name_off >= strtab_size)
        return NULL;

    uint64_t strtab_off = *(uint64_t *)(strtab_shdr + 24);    // sh_offset
    return (const char *)(elf_base + strtab_off + name_off);
}
```

Key details:

- **Pointer-to-offset signature**. The second argument is `uint32_t *`, not `uint32_t`. This is a compact optimization: rather than the caller loading the 32-bit offset and passing it, the caller passes the address of the field directly from the ELF structure and the accessor dereferences it inline. This is visible in the decompiler output as `*a2` being loaded at the point of use. Net saving: one load and register shuffle at each call site.

- **Only supports the shstrtab**. This function only reaches the string table referenced by `e_shstrndx`. It cannot be used to look up strings in an arbitrary strtab such as the one referenced from a symtab's `sh_link`. For symbol names, `sub_4486A0` (below) must be used instead — it follows the symtab's `sh_link` explicitly rather than going through `e_shstrndx`.

- **Bounds checking**. Both the shstrndx-vs-shnum bound and the name-offset-vs-sh_size bound are enforced. A malformed ELF with an out-of-range name offset silently returns NULL, which the caller (typically `merge_elf`) treats as an empty-name condition.

- **SHT_STRTAB validation**. The `sh_type == 3` check protects against a valid-looking ELF with a bogus `e_shstrndx` pointing at a non-strtab section.

## Elf64 Symbol Table Accessors

CUDA cubins always carry a `SHT_SYMTAB` with 24-byte Elf64_Sym entries. The linker reads symbol records during the merge phase to build the global symbol table and resolve cross-object references. Three accessors form the symbol access API: index-to-pointer, name lookup, and section-index lookup with `SHN_XINDEX` support.

### Elf64_Sym Layout (24 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `st_name` | Offset into associated strtab |
| 4 | 1 | `st_info` | Binding and type (`ELF64_ST_BIND` / `ELF64_ST_TYPE`) |
| 5 | 1 | `st_other` | Visibility (`STV_DEFAULT`, `STV_HIDDEN`, ...) |
| 6 | 2 | `st_shndx` | Section index; `0xFFFF` = `SHN_XINDEX` (extended) |
| 8 | 8 | `st_value` | Symbol value (address or offset within section) |
| 16 | 8 | `st_size` | Symbol size in bytes |

Confirmed by the arithmetic in `sub_448750` (reads `st_shndx` at offset 6) and `sub_4486A0` (reads `st_name` at offset 0, strides by 24). nvlink never touches `st_info` or `st_other` through these accessors — those are read open-coded in the callers (`merge_elf` and `symbol_resolve`).

### `sub_448600` — `elf64_symbol_by_index`

Returns a pointer to the N-th `Elf64_Sym` entry in the file's `SHT_SYMTAB`. Implicitly finds the symtab section by `sh_type`.

```c
// sub_448600 -- elf64_symbol_by_index(elf_base, sym_idx)
// Address: 0x448600
void *elf64_symbol_by_index(void *elf_base, uint32_t sym_idx)
{
    // Inline scan for the first SHT_SYMTAB (sh_type == 2)
    uint16_t shnum = *(uint16_t *)(elf_base + 60);
    void    *shdr  = elf_base + *(uint64_t *)(elf_base + 40);

    uint32_t count;
    if (shnum != 0) {
        count = shnum;
    } else if (shdr) {
        count = *(uint32_t *)(shdr + 32);
    } else {
        return NULL;
    }

    for (uint32_t i = 0; i < count; i++) {
        if (*(uint32_t *)(shdr + 4) == 2) {          // SHT_SYMTAB
            uint64_t sh_entsize = *(uint64_t *)(shdr + 56);
            uint64_t sh_size    = *(uint64_t *)(shdr + 32);
            uint64_t sh_offset  = *(uint64_t *)(shdr + 24);

            // Bounds check: idx must be < total entries
            if (sh_entsize == 0) return NULL;
            if (sym_idx >= sh_size / sh_entsize)
                return NULL;

            // Stride is HARDCODED to 24 bytes (sizeof(Elf64_Sym))
            // sh_entsize is used only for the bounds check above
            return elf_base + sh_offset + 24 * sym_idx;
        }
        shdr += 64;
    }
    return NULL;
}
```

Important details:

- **sh_entsize is checked but not used as stride**. The bounds check divides `sh_size` by `sh_entsize` to determine the maximum symbol index, but the actual pointer arithmetic uses a hardcoded stride of 24 bytes. If a non-standard Elf64 used a different entry size (e.g., 32 bytes for some vendor extension), this function would silently read garbage at wrong offsets. No real-world CUDA ELF hits this because ptxas always emits exactly 24 bytes per symbol.

- **Inlined SHT_SYMTAB scan**. This function duplicates the logic of `sub_4484F0` (section_by_type) rather than calling it. The compiler did not deduplicate this because the body of the match block differs.

- **Fails open on missing symtab**. If no `SHT_SYMTAB` exists (possible for stripped binaries), the function returns NULL. All callers must null-check.

- **First symtab wins**. ELF permits multiple symbol tables, but CUDA ELFs have at most one `SHT_SYMTAB` (plus optionally a `SHT_DYNSYM`, which is type `11` and ignored here).

### `sub_4486A0` — `elf64_symbol_name`

Resolves a symbol index to a null-terminated string. Critically, this does **not** use `e_shstrndx` — it uses the symbol table's own `sh_link` to find the associated string table, which for `SHT_SYMTAB` points to `.strtab` (not `.shstrtab`).

```c
// sub_4486A0 -- elf64_symbol_name(elf_base, symtab_shdr, sym_idx)
// Address: 0x4486A0
const char *elf64_symbol_name(void *elf_base, void *symtab_shdr, uint32_t sym_idx)
{
    if (!symtab_shdr) return NULL;

    uint64_t sh_entsize = *(uint64_t *)(symtab_shdr + 56);
    if (!sh_entsize) return NULL;

    if (*(uint32_t *)(symtab_shdr + 4) != 2)        // sh_type == SHT_SYMTAB
        return NULL;

    uint64_t sh_size = *(uint64_t *)(symtab_shdr + 32);
    if (sym_idx >= sh_size / sh_entsize)
        return NULL;

    // Resolve the associated strtab via symtab->sh_link
    uint32_t strtab_idx = *(uint32_t *)(symtab_shdr + 40);   // sh_link
    uint32_t shnum = *(uint16_t *)(elf_base + 60);
    if (shnum == 0) {
        void *shdr0 = elf_base + *(uint64_t *)(elf_base + 40);
        if (!shdr0) return NULL;
        shnum = *(uint32_t *)(shdr0 + 32);
    }
    if (strtab_idx >= shnum)
        return NULL;

    void *strtab_shdr = elf_base
                      + *(uint64_t *)(elf_base + 40)
                      + strtab_idx * *(uint16_t *)(elf_base + 58);
    if (!strtab_shdr) return NULL;

    // Validate strtab type
    if (*(uint32_t *)(strtab_shdr + 4) != 3)        // SHT_STRTAB
        return NULL;

    // Fetch st_name from the symbol entry (stride HARDCODED to 24)
    uint64_t sym_off = *(uint64_t *)(symtab_shdr + 24) + 24 * sym_idx;
    uint32_t st_name = *(uint32_t *)(elf_base + sym_off);

    uint64_t strtab_size = *(uint64_t *)(strtab_shdr + 32);
    if (st_name >= strtab_size)
        return NULL;

    return (const char *)(elf_base
                        + *(uint64_t *)(strtab_shdr + 24)
                        + st_name);
}
```

The function takes the **symtab shdr pointer** from the caller (typically obtained via `sub_4484F0` with `SHT_SYMTAB`) rather than looking it up internally. This allows `merge_elf` to cache the symtab shdr across many symbol lookups within the same input object.

**Difference from `sub_448590`**: `string_at` uses `e_shstrndx` to find the shstrtab; `symbol_name` follows the symtab's `sh_link` to find `.strtab`. These are two different string tables in a typical ELF. A call to `sub_448590` with a `st_name` offset would return the wrong string (the offset would be looked up in the section name table, not the symbol name table).

### `sub_448750` — `elf64_symbol_shndx` (with SHN_XINDEX support)

Returns the section index associated with a symbol, transparently handling the `SHN_XINDEX` (extended section index) case where the 16-bit `st_shndx` field is insufficient.

```c
// sub_448750 -- elf64_symbol_shndx(elf_base, symtab_shdr, sym_idx)
// Address: 0x448750
//
// Returns the section index for a symbol, resolving SHN_XINDEX through
// the SHT_SYMTAB_SHNDX section when present.
uint32_t elf64_symbol_shndx(void *elf_base, void *symtab_shdr, uint32_t sym_idx)
{
    if (!elf_base || !symtab_shdr) return 0;

    // Read the direct st_shndx field first
    uint16_t st_shndx = *(uint16_t *)(symtab_shdr + 6);    // offset 6 in Elf64_Sym
    if (st_shndx != 0xFFFF)
        return st_shndx;                                    // normal case

    // SHN_XINDEX: scan for the SHT_SYMTAB_SHNDX section (sh_type == 18)
    uint16_t shnum = *(uint16_t *)(elf_base + 60);
    void    *shdr  = elf_base + *(uint64_t *)(elf_base + 40);

    uint32_t count;
    if (shnum != 0) {
        count = shnum;
    } else if (shdr) {
        count = *(uint32_t *)(shdr + 32);
    } else {
        return 0;
    }

    // Scan to find the extended section index table
    for (uint32_t i = 0; i < count; i++) {
        if (*(uint32_t *)(shdr + 4) == 18) {               // SHT_SYMTAB_SHNDX
            uint64_t sh_entsize = *(uint64_t *)(shdr + 56);
            uint64_t sh_size    = *(uint64_t *)(shdr + 32);
            if (!sh_entsize || sym_idx >= sh_size / sh_entsize)
                return 0;

            // Extended table is an array of 4-byte section indices
            uint64_t sh_offset = *(uint64_t *)(shdr + 24);
            return *(uint32_t *)(elf_base + sh_offset + 4 * sym_idx);
        }
        shdr += 64;
    }
    return 0;
}
```

**Important subtleties in the decompiled control flow**:

- The decompiler output for `sub_448750` contains a fall-through path that reads from `v10 = 0` (`*(unsigned int *)(v10 + 4LL * a3)`) when no `SHT_SYMTAB_SHNDX` section is found during the secondary scan. This is a NULL dereference path that would crash the process, but it is unreachable under well-formed inputs because the initial scan that proves `st_shndx == SHN_XINDEX` guarantees the extended table exists (otherwise the first scan would have already bailed). The decompiler is showing a defensive fall-through that the source code relies on never executing.

- **Bug surface**: A malformed ELF with `st_shndx = 0xFFFF` but no `SHT_SYMTAB_SHNDX` section will crash nvlink with a segfault during merge. The validation in `sub_43DD30` does not explicitly check this coupling.

- **Stride 4**. The extended index table is an array of `Elf32_Word` (4-byte) entries indexed by symbol number.

## Elf32 Symbol Table (or Lack Thereof)

The Elf32 accessor family does **not** include dedicated symbol lookup functions. There are no Elf32 counterparts to `sub_448600`, `sub_4486A0`, or `sub_448750`. Inside `merge_elf` (`sub_45E7D0`), the Elf32 symbol handling path is open-coded:

```c
// sub_45E7D0 -- merge_elf (Elf32 path, lines 663-677)
if (!is_elf64(elf_base)) {
    void     *hdr    = elf32_header(elf_base);                    // sub_46B590
    uint32_t *symtab = (uint32_t *)elf32_section_by_type(elf_base, 2);  // sub_46B700, SHT_SYMTAB
    // symtab[6] is sh_link (offset 24 in Elf32 shdr: 6*4 bytes in)
    void *strtab_shdr = elf32_section_by_index(elf_base, symtab[6]); // sub_46B5A0
    uint32_t count = elf32_section_count(elf_base);                // sub_46B810
    // ... further processing uses sub_46B7A0 for string lookup
}
```

This reflects the reality that 32-bit CUDA ELFs are an extreme edge case. CUDA has been 64-bit by default since CUDA 7 (2015), and host-32-bit CUDA was fully removed in CUDA 9. The Elf32 path in nvlink exists only for historical compatibility and is not actively exercised. The lack of symbol accessors means the Elf32 path pays an O(n) section scan per symbol lookup, but since no modern workload hits this code it does not matter.

### Elf32_Sym Layout (16 bytes) — Field Order Differs From Elf64_Sym

`merge_elf`'s open-coded Elf32 symbol walk indexes records at 16-byte stride. The critical trap is that `Elf32_Sym` and `Elf64_Sym` share *field names* but not *field order*: in Elf32, `st_value` and `st_size` sit before `st_info`/`st_other`/`st_shndx`, whereas Elf64 places `st_info`/`st_other`/`st_shndx` immediately after `st_name`.

| Offset | Size | Field | Elf64 equivalent offset | Note |
|---|---|---|---|---|
| 0 | 4 | `st_name` | 0 | Same position |
| 4 | 4 | `st_value` | 8 (8 bytes) | Moved forward, narrower |
| 8 | 4 | `st_size` | 16 (8 bytes) | Moved forward, narrower |
| 12 | 1 | `st_info` | 4 | Pushed to the tail in Elf32 |
| 13 | 1 | `st_other` | 5 | Pushed to the tail in Elf32 |
| 14 | 2 | `st_shndx` | 6 | Pushed to the tail in Elf32 |

A reader that hard-codes Elf64 offsets while walking an Elf32 symtab will read the low half of `st_value` as `st_info`/`st_other`/`st_shndx`, silently corrupting symbol type and binding bits. nvlink avoids this only because the Elf32 symbol path is open-coded with explicit 4/8/12-byte loads rather than sharing helpers with the Elf64 family. There is no `Elf32_Sym` accessor cluster to keep the two layouts honest.

> **QUIRK — 16-byte stride is also hardcoded in the Elf32 path.** Same correctness gap as the Elf64 family: `sh_entsize` from the symtab section header is not consulted when computing the byte offset of the N-th symbol entry; the stride is the literal 16. A non-standard Elf32 with `sh_entsize > 16` would be walked at the wrong cadence.

## Elf32 Accessor Functions

The Elf32 functions mirror the Elf64 family exactly, with adjusted offsets and field widths. They live at `0x46B590`--`0x46B810`.

### Elf32 Header Layout (offsets used in the binary)

| Offset | Size | Elf32 field | nvlink usage |
|---|---|---|---|
| 0 | 4 | `e_ident[0..3]` | Magic: `0x7F454C46` |
| 4 | 1 | `e_ident[EI_CLASS]` | Class dispatch: not `2` = 32-bit |
| 28 | 4 | `e_phoff` | Program header table offset (32-bit) |
| 32 | 4 | `e_shoff` | Section header table offset (32-bit) |
| 42 | 2 | `e_phentsize` | Program header entry size (validated == 32) |
| 44 | 2 | `e_phnum` | Number of program headers |
| 46 | 2 | `e_shentsize` | Section header entry size (validated == 40) |
| 48 | 2 | `e_shnum` | Number of section headers (0 = extended) |
| 50 | 2 | `e_shstrndx` | Section name string table index (0xFFFF = extended) |

### Elf32 Section Header Layout (40 bytes)

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `sh_name` |
| 4 | 4 | `sh_type` |
| 8 | 4 | `sh_flags` |
| 12 | 4 | `sh_addr` |
| 16 | 4 | `sh_offset` |
| 20 | 4 | `sh_size` |
| 24 | 4 | `sh_link` |
| 28 | 4 | `sh_info` |
| 32 | 4 | `sh_addralign` |
| 36 | 4 | `sh_entsize` |

### `sub_46B590` — `elf32_header`

Identity function, same as the Elf64 version. Returns its argument unchanged.

### `sub_46B5A0` — `elf32_section_by_index`

```c
// sub_46B5A0 -- elf32_section_by_index(elf_base, idx)
// Address: 0x46B5A0
void *elf32_section_by_index(void *elf_base, uint32_t idx)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 48);   // e_shnum (Elf32 offset)

    if (shnum == 0) {
        uint32_t shoff = *(uint32_t *)(elf_base + 32);  // e_shoff (32-bit)
        void *shdr0 = elf_base + shoff;
        if (!shdr0) return NULL;
        shnum = *(uint32_t *)(shdr0 + 20);              // section[0].sh_size (Elf32)
    }

    if (idx >= shnum)
        return NULL;

    uint16_t shentsize = *(uint16_t *)(elf_base + 46);  // e_shentsize
    uint32_t shoff     = *(uint32_t *)(elf_base + 32);   // e_shoff
    return elf_base + shoff + shentsize * idx;
}
```

Structurally identical to the Elf64 version, but `e_shoff` is a 4-byte value at offset 32 (not 8-byte at offset 40), and `section[0].sh_size` is at offset 20 (not 32) within the section header. The stride per section header is 40 bytes instead of 64.

### `sub_46B810` — `elf32_section_count`

```c
// sub_46B810 -- elf32_section_count(elf_base)
// Address: 0x46B810
uint32_t elf32_section_count(void *elf_base)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 48);

    if (shnum == 0) {
        uint32_t shoff = *(uint32_t *)(elf_base + 32);
        void *shdr0 = elf_base + shoff;
        if (shdr0)
            return *(uint32_t *)(shdr0 + 20);   // section[0].sh_size
        return 0;
    }

    return shnum;
}
```

### `sub_46B5D0` — `elf32_section_by_name`

Identical logic to the Elf64 version, with 32-bit field widths. The iteration stride is `+= 10` (10 x 4-byte dwords = 40 bytes per Elf32 section header). The string table's `sh_offset` is at field offset 16 (`v11[4]`, a 4-byte value) and `sh_size` at offset 20 (`v11[5]`). The extended `e_shstrndx` fallback reads `section[0].sh_link` at offset 24 within the first section header.

### `sub_46B700` — `elf32_section_by_type`

```c
// sub_46B700 -- elf32_section_by_type(elf_base, sh_type)
// Address: 0x46B700
void *elf32_section_by_type(void *elf_base, uint32_t sh_type)
{
    uint16_t shnum = *(uint16_t *)(elf_base + 48);
    void    *shdr  = elf_base + *(uint32_t *)(elf_base + 32);

    uint32_t count;
    if (shnum != 0) {
        count = shnum;
    } else if (shdr) {
        count = *(uint32_t *)(shdr + 20);    // shdr[0].sh_size (Elf32 offset)
    } else {
        return NULL;
    }

    for (uint32_t i = 0; i < count; i++) {
        if (*(uint32_t *)(shdr + 4) == sh_type)
            return shdr;
        shdr += 40;                          // Elf32 shdr stride
    }
    return NULL;
}
```

Same logic as the Elf64 variant, but with a 40-byte stride for Elf32 section headers. The merge_elf Elf32 path calls this with `sh_type == 2` (`SHT_SYMTAB`) to find the symbol table before falling back to open-coded access.

### `sub_46B770` — `elf32_section_data`

```c
// sub_46B770 -- elf32_section_data(elf_base, shdr)
// Address: 0x46B770
void *elf32_section_data(void *elf_base, void *shdr)
{
    if (!shdr) return NULL;
    return elf_base + *(uint32_t *)(shdr + 16);   // sh_offset (Elf32 offset 16)
}
```

The Elf32 `sh_offset` is a 4-byte field at shdr offset 16, compared to Elf64's 8-byte field at offset 24. The return type is still a `void *` into the in-memory buffer.

### `sub_46B790` — `elf32_section_size`

```c
// sub_46B790 -- elf32_section_size(elf_base, shdr)
// Address: 0x46B790
uint32_t elf32_section_size(void *elf_base, void *shdr)
{
    if (!shdr) return 0;
    return *(uint32_t *)(shdr + 20);              // sh_size (Elf32 offset 20)
}
```

First argument unused. Returns the 4-byte `sh_size` field. Note the return type is 32-bit (`uint32_t`) for Elf32, versus `uint64_t` for the Elf64 counterpart `sub_448580`.

### `sub_46B7A0` — `elf32_string_at`

```c
// sub_46B7A0 -- elf32_string_at(elf_base, name_ptr)
// Address: 0x46B7A0
const char *elf32_string_at(void *elf_base, uint32_t *name_ptr)
{
    if (!elf_base || !name_ptr) return NULL;

    uint32_t shstrndx = *(uint16_t *)(elf_base + 50);     // e_shstrndx
    if (shstrndx == 0xFFFF) {
        void *shdr0 = elf_base + *(uint32_t *)(elf_base + 32);
        shstrndx = *(uint32_t *)(shdr0 + 24);              // shdr[0].sh_link (Elf32 offset 24)
    }

    uint32_t shnum = *(uint16_t *)(elf_base + 48);
    if (shnum == 0) {
        void *shdr0 = elf_base + *(uint32_t *)(elf_base + 32);
        if (!shdr0) return NULL;
        shnum = *(uint32_t *)(shdr0 + 20);
    }
    if (shstrndx >= shnum) return NULL;

    void *strtab_shdr = elf_base
                      + *(uint32_t *)(elf_base + 32)
                      + shstrndx * *(uint16_t *)(elf_base + 46);
    if (!strtab_shdr) return NULL;

    if (*(uint32_t *)(strtab_shdr + 4) != 3)              // SHT_STRTAB
        return NULL;

    uint32_t name_off = *name_ptr;
    if (name_off >= *(uint32_t *)(strtab_shdr + 20))      // sh_size
        return NULL;

    return (const char *)(elf_base
                        + *(uint32_t *)(strtab_shdr + 16)  // sh_offset
                        + name_off);
}
```

Identical semantics to `sub_448590` but with 32-bit offsets throughout. The `merge_elf` Elf32 path uses this for all section-name and strtab lookups because no dedicated symbol-name accessor exists for Elf32.

## Elf64 Section Header Layout (64 bytes)

For reference, the full Elf64 section header layout as used by the accessor functions:

| Offset | Size | Field | Usage in nvlink |
|---|---|---|---|
| 0 | 4 | `sh_name` | Index into shstrtab (read by `section_by_name`) |
| 4 | 4 | `sh_type` | Section type (`3` = SHT_STRTAB, `8` = SHT_NOBITS) |
| 8 | 8 | `sh_flags` | Section flags |
| 16 | 8 | `sh_addr` | Virtual address |
| 24 | 8 | `sh_offset` | File offset (used by `section_data`) |
| 32 | 8 | `sh_size` | Section size; also extended `e_shnum` for section[0] |
| 40 | 4 | `sh_link` | Extended `e_shstrndx` for section[0] |
| 44 | 4 | `sh_info` | Additional info |
| 48 | 8 | `sh_addralign` | Alignment constraint |
| 56 | 8 | `sh_entsize` | Size of fixed-size entries |

## ELF Validation: `sub_43DD30`

After loading an ELF into memory, nvlink validates it before processing. The validation function takes the ELF base pointer and the file size, and returns a boolean (1 = valid, 0 = invalid). It rejects the ELF silently on failure — no error message is emitted from this function; the caller reports the error.

```c
// sub_43DD30 -- elf_validate(elf_base, file_size)
// Address: 0x43DD30
bool elf_validate(void *elf_base, uint64_t file_size)
{
    uint64_t elf_end = elf_base + file_size;

    if (*(uint8_t *)(elf_base + 4) == 2) {
        // --- Elf64 path ---
        void *ehdr = elf64_header(elf_base);

        // Check structural sizes
        if (*(uint16_t *)(ehdr + 58) != 64)     // e_shentsize must be 64
            return false;
        if (*(uint16_t *)(ehdr + 56) != 0 &&    // if e_phnum > 0...
            *(uint16_t *)(ehdr + 54) != 56)      //   e_phentsize must be 56
            return false;

        // Check section header table fits in file
        uint64_t shoff = *(uint64_t *)(ehdr + 40);
        if (file_size < shoff)          return false;
        if (shoff <= 0x3F)              return false;  // must be past ELF header
        if (file_size < shoff + 64)     return false;  // room for at least one shdr

        // Check section header table extent
        uint32_t shnum = elf64_section_count(elf_base);
        if (file_size < shoff + 64 * shnum)
            return false;

        // Check program header table fits
        uint64_t phoff = *(uint64_t *)(ehdr + 32);
        uint16_t phnum = *(uint16_t *)(ehdr + 56);
        uint16_t phentsz = *(uint16_t *)(ehdr + 54);
        if (file_size < phoff || file_size < phoff + phentsz * phnum)
            return false;

        // Validate each section header
        for (uint32_t i = 0; i < shnum; i++) {
            void *shdr = elf64_section_by_index(elf_base, i);
            if (!shdr)                   return false;
            if (elf_end < shdr + 64)     return false;  // shdr fits in buffer

            uint32_t sh_type = *(uint32_t *)(shdr + 4);

            // Skip SHT_NOBITS (8) and CUDA vendor types (0x70000007..0x70000015)
            bool skip = (sh_type == 8);
            uint32_t vendor_off = sh_type - 0x70000007;
            if (vendor_off <= 0xE)
                skip |= (0x400D >> vendor_off) & 1;

            if (!skip) {
                // Section data must fit within file
                uint64_t sh_offset = *(uint64_t *)(shdr + 24);
                uint64_t sh_size   = *(uint64_t *)(shdr + 32);
                if (elf_end < elf_base + sh_offset + sh_size)
                    return false;
                if (sh_size > ~sh_offset)   // overflow check
                    return false;
            }
        }
    }
    else {
        // --- Elf32 path ---
        void *ehdr = elf32_header(elf_base);

        if (*(uint16_t *)(ehdr + 46) != 40)     // e_shentsize must be 40
            return false;
        if (*(uint16_t *)(ehdr + 44) != 0 &&
            *(uint16_t *)(ehdr + 42) != 32)      // e_phentsize must be 32
            return false;

        uint32_t shoff = *(uint32_t *)(ehdr + 32);
        if (file_size < shoff)          return false;
        if (shoff <= 0x33)              return false;  // past Elf32 header (52 bytes)
        if (file_size < shoff + 40)     return false;

        uint32_t shnum = elf32_section_count(elf_base);
        if (file_size < shoff + 40 * shnum)
            return false;

        uint32_t phoff = *(uint32_t *)(ehdr + 28);
        if (file_size < phoff ||
            file_size < phoff + *(uint16_t *)(ehdr + 42) * *(uint16_t *)(ehdr + 44))
            return false;

        for (uint32_t i = 0; i < shnum; i++) {
            void *shdr = elf32_section_by_index(elf_base, i);
            if (!shdr)                   return false;
            if (elf_end < shdr + 40)     return false;

            uint32_t sh_type = *(uint32_t *)(shdr + 4);

            bool skip = (sh_type == 8);
            uint32_t vendor_off = sh_type - 0x70000007;
            if (vendor_off <= 0xE)
                skip |= (0x400D >> vendor_off) & 1;

            if (!skip) {
                uint32_t sh_offset = *(uint32_t *)(shdr + 16);
                uint32_t sh_size   = *(uint32_t *)(shdr + 20);
                if (elf_end < elf_base + sh_offset + sh_size)
                    return false;
            }
        }
    }

    // Final check: computed ELF extent (sub_43DA80) must fit in file
    uint64_t extent = elf_extent(elf_base);    // sub_43DA80
    if (extent == 0) return false;
    return file_size >= extent;
}
```

### CUDA Vendor Section Types

The validation skips bounds-checking for section types that have no file-resident data. The bitmask `0x400D` tested against the range `0x70000007`--`0x70000015` identifies these CUDA-specific section types:

| Type value | Offset from base | Bit | Skipped? | Identity |
|---|---|---|---|---|
| `0x70000007` | 0 | bit 0 of `0x400D` = 1 | Yes | `SHT_CUDA_GLOBAL` |
| `0x70000008` | 1 | bit 1 = 0 | No | — |
| `0x70000009` | 2 | bit 2 of `0x400D` = 1 | Yes | `SHT_CUDA_LOCAL` |
| `0x7000000A` | 3 | bit 3 of `0x400D` = 1 | Yes | `SHT_CUDA_SHARED` |
| `0x7000000B`--`0x70000013` | 4–12 | 0 | No | — |
| `0x70000014` | 13 | bit 13 = 0 | No | — |
| `0x70000015` | 14 | bit 14 of `0x400D` = 1 | Yes | `SHT_CUDA_SHARED_RESERVED` |

These are sections that may have `sh_offset = 0` and `sh_size = 0` in relocatable objects because their data is synthesized during linking, not stored in the input file.

### ELF Extent: `sub_43DA80`

The final check in validation calls `sub_43DA80`, which computes the maximum byte offset referenced by any header or section in the ELF. This is a comprehensive "how big does this file need to be" computation that considers:

1. The section header table: `e_shoff + e_shentsize * section_count`
2. The program header table: `e_phoff + e_phentsize * e_phnum`
3. Every section's `sh_offset + sh_size` (skipping SHT_NOBITS and the vendor types listed above)
4. Overflow detection on all additions (returns 0 on overflow)

The validation passes only if the actual file size is at least as large as this computed extent.

## ELF Magic Check: `sub_43D970`

A minimal magic-number check used in `main()` before full validation:

```c
// sub_43D970 -- is_elf(base)
bool is_elf(void *base) {
    if (!base) return false;
    return *(uint32_t *)base == 0x464C457F;   // "\x7fELF" in little-endian
}
```

This is called during the input file loop in `main()` when a `.cubin` file is encountered. After the magic check passes, `main()` additionally checks `e_machine == 190` (EM_CUDA) to confirm it is a device ELF rather than a host object.

## Mercury Capability Detection: `sub_43DA40`

For Elf64 files, `sub_43DA40` determines whether the cubin contains Mercury (capsule mercury / sm100+) content by inspecting the ELF flags:

```c
// sub_43DA40 -- is_mercury_capable(elf_base)
bool is_mercury_capable(void *elf_base) {
    if (!elf_base) return false;
    if (*(uint8_t *)(elf_base + 4) != 2)   // only for Elf64
        return false;

    void *ehdr = elf64_header(elf_base);
    uint32_t flags = *(uint32_t *)(ehdr + 48);  // e_flags

    int mask;
    if (*(uint8_t *)(ehdr + 7) == 0x41)   // EI_OSABI == 0x41 (CUDA device ABI)
        mask = 2;                           // bit 1 of e_flags
    else
        mask = 0x4000;                      // bit 14 of e_flags

    return (flags & mask) != 0;
}
```

The OSABI byte at offset 7 distinguishes CUDA device ELFs (`0x41`) from generic ELFs. For device ELFs, Mercury capability is indicated by bit 1 of `e_flags`. For non-standard OSABI values, bit 14 is checked instead.

## ELF Predicate Functions

A small family of trivial predicate functions exists alongside the accessors. These are inlined at most call sites (the compiler merges them with their callers) but appear as distinct functions in the symbol table for clarity.

| Address | Name | Returns | Implementation |
|---|---|---|---|
| `0x43D970` | `is_elf_magic` | `*(u32*)base == 0x464C457F` | Magic check |
| `0x43D9A0` | `is_elf64` | `*(u8*)(base + 4) == 2` | EI_CLASS check |
| `0x43D9B0` | `is_rel_elf` | `*(u16*)(hdr + 16) == 1` | `e_type == ET_REL` (dispatches on class) |
| `0x43DA00` | `is_lto_elf` | `(e_flags & mask) != 0` | bit 0 of `e_flags` (OSABI=0x41) or bit 31 otherwise |
| `0x43DA40` | `is_mercury_capable` | `(e_flags & mask) != 0` | bit 1 of `e_flags` (OSABI=0x41) or bit 14 otherwise |

### `sub_43D9B0` — `is_rel_elf`

```c
// sub_43D9B0 -- is_rel_elf(elf_base)
// Address: 0x43D9B0
bool is_rel_elf(void *elf_base)
{
    if (!elf_base) return false;
    if (*(uint8_t *)(elf_base + 4) == 2)
        return *(uint16_t *)(elf64_header(elf_base) + 16) == 1;  // e_type at +16
    else
        return *(uint16_t *)(elf32_header(elf_base) + 16) == 1;  // e_type at +16 (same for Elf32)
}
```

`e_type` lives at offset 16 in both Elf32 and Elf64 headers (because the preceding 16 bytes of `e_ident` are identical). The value `1` is `ET_REL` — relocatable. Device cubins from `ptxas` are `ET_REL` (value `1`) on legacy targets or the Mercury custom type `0xFF00` on sm >= 100; `ET_EXEC` (value `2`) is the final output type produced by `nvlink` itself and only re-appears as input when a previously linked cubin is fed back in (where `sub_426570` rejects it). This predicate is also used outside the LTO path — for host vs device disambiguation during the input dispatch — since both host `.o` files and ptxas-produced cubins are `ET_REL`.

### `sub_43DA00` — `is_lto_elf`

```c
// sub_43DA00 -- is_lto_elf(elf_base)
// Address: 0x43DA00
bool is_lto_elf(void *elf_base)
{
    if (!elf_base) return false;
    if (*(uint8_t *)(elf_base + 4) != 2) return false;   // Elf64 only

    void *ehdr = elf64_header(elf_base);
    uint32_t mask = (*(uint8_t *)(ehdr + 7) == 0x41) ? 1u : 0x80000000u;
    return (*(uint32_t *)(ehdr + 48) & mask) != 0;       // e_flags at +48
}
```

Same dispatch shape as `is_mercury_capable`: for CUDA device OSABI (`0x41`), check bit 0 of `e_flags`; for generic OSABI, check bit 31. This identifies LTO bitcode-embedded ELFs that require the LTO merge pipeline instead of the standard link path.

## Endianness Handling

**nvlink assumes little-endian throughout the entire ELF parser.** The disk-loader entry points (`sub_43E100` for cubins, `sub_43DFC0` for host ELFs) gate on `e_ident[EI_DATA] == 1` (ELFDATA2LSB) and reject big-endian files before they reach the accessor family. Once a buffer has passed that gate — or has been delivered to the linker by some other path (embedded fatbin payload, in-memory ELF produced by the integrated ptxas) — nothing else re-checks the byte. The accessors and `sub_43DD30` validator contain no byte-swapping code; every `*(uint16_t *)`, `*(uint32_t *)`, and `*(uint64_t *)` load directly reinterprets the buffer bytes as a host-order integer.

This is correct for all real-world usage:

- CUDA GPUs are always little-endian on the device side
- The host architectures nvlink targets (x86-64, ARM64, ppc64le) are all little-endian
- `ptxas` emits little-endian ELFs regardless of host

A hypothetical big-endian CUDA ELF would produce catastrophically wrong parses: for example, `e_shnum == 0x1400` would be read as `0x0014` (decimal 20), likely passing some validity checks while producing incorrect section counts. `elf_validate` would probably catch most such cases via the file-extent check, but not reliably.

The simplification is deliberate: CUDA does not support big-endian targets, so the code complexity of byte-swapping accessors would be pure dead weight. The cost is that nvlink will not ever link a non-little-endian ELF, which is not a real limitation.

## Latent Correctness Issues

A few subtle behaviors are worth documenting because they could surface as latent bugs under specific malformed inputs:

### Hardcoded Elf64_Sym stride

`sub_448600` and `sub_4486A0` both use a hardcoded 24-byte stride when indexing into the symbol table, while validating the index against `sh_size / sh_entsize`. If an input ELF has `sh_entsize == 32` and 32-byte symbol entries (some vendor extensions or custom ABI), the bounds check will accept more indices than the 24-byte stride can actually reach, and symbol data will be read from wrong offsets. Every CUDA-compiled ELF uses standard 24-byte Elf64_Sym so this never triggers in practice, but a malicious or corrupted input could exploit it for confused-deputy reads.

### SHN_XINDEX without SHT_SYMTAB_SHNDX

`sub_448750` handles the case `st_shndx == SHN_XINDEX` (`0xFFFF`) by scanning for an `SHT_SYMTAB_SHNDX` section. If the ELF says `SHN_XINDEX` but no `SHT_SYMTAB_SHNDX` section exists, the decompiled control flow falls through to a path that dereferences a NULL pointer (`v10 = 0; return *(u32 *)(v10 + 4*idx)`). This would crash nvlink during merge with a NULL-deref segfault. The validation in `sub_43DD30` does not verify the pairing.

### section_by_name per-iteration shstrndx resolution

The `section_by_name` accessor for both Elf32 and Elf64 resolves `e_shstrndx` and looks up the shstrtab section header **inside** the iteration loop rather than once before it. This is technically loop-invariant work and the compiler could hoist it, but it did not. For typical cubins (~50 sections) the cost is negligible. For heavy template-instantiation ELFs with thousands of sections it adds measurable but still small overhead.

### section_by_name allows stale strtab_shdr

A subtle interaction in the `section_by_name` loops: the string-table shdr is re-fetched each iteration, but the `SHT_STRTAB` validation is performed each time as well. If the ELF is maliciously crafted so that the shstrtab section's own `sh_type` differs from `3`, the function returns NULL immediately on the first iteration regardless of whether the requested section exists elsewhere. This is acceptable behavior: an ELF with a broken shstrtab is unusable and no graceful handling is expected.

## Extended Section Numbering

Both the Elf32 and Elf64 accessor families implement the ELF specification's extended numbering mechanism:

| Condition | Normal value | Extended source |
|---|---|---|
| `e_shnum == 0` | Section count | `section[0].sh_size` |
| `e_shstrndx == 0xFFFF` | String table index | `section[0].sh_link` |

Extended numbering is used when the section count exceeds 65,535 (`0xFFFF`). CUDA cubins almost never hit this limit, but nvlink handles it correctly because the same ELF parsing code also processes host ELF objects (for `--use-host-info`), which could theoretically have many sections from heavy template instantiation or LTO.

## Function Address Summary

### Elf64 accessor family (0x448360 — 0x448750)

| Address | Recovered name | Description |
|---|---|---|
| `0x448360` | `elf64_header` | Return pointer to Elf64 header (identity) |
| `0x448370` | `elf64_section_by_index` | Get section header by index |
| `0x4483B0` | `elf64_section_by_name` | Linear search sections by name |
| `0x4484F0` | `elf64_section_by_type` | Linear search sections by `sh_type` |
| `0x448560` | `elf64_section_data` | Get pointer to section data (`base + sh_offset`) |
| `0x448580` | `elf64_section_size` | Return `sh_size` field |
| `0x448590` | `elf64_string_at` | Resolve string via shstrtab (`e_shstrndx`) |
| `0x448600` | `elf64_symbol_by_index` | Get Elf64_Sym pointer by symbol index |
| `0x4486A0` | `elf64_symbol_name` | Resolve symbol name via symtab `sh_link` |
| `0x448730` | `elf64_section_count` | Get section count (handles extended) |
| `0x448750` | `elf64_symbol_shndx` | Resolve `st_shndx` with `SHN_XINDEX` support |

### Elf32 accessor family (0x46B590 — 0x46B810)

| Address | Recovered name | Description |
|---|---|---|
| `0x46B590` | `elf32_header` | Return pointer to Elf32 header (identity) |
| `0x46B5A0` | `elf32_section_by_index` | Get section header by index |
| `0x46B5D0` | `elf32_section_by_name` | Linear search sections by name |
| `0x46B700` | `elf32_section_by_type` | Linear search sections by `sh_type` |
| `0x46B770` | `elf32_section_data` | Get pointer to section data (`base + sh_offset`) |
| `0x46B790` | `elf32_section_size` | Return `sh_size` field (32-bit) |
| `0x46B7A0` | `elf32_string_at` | Resolve string via shstrtab |
| `0x46B810` | `elf32_section_count` | Get section count (handles extended) |

**Note**: No Elf32 symbol accessor family exists. Elf32 symbol lookups are open-coded in `merge_elf`.

### Supporting functions

| Address | Recovered name | Description |
|---|---|---|
| `0x476BF0` | `read_entire_file` | Load file into arena buffer |
| `0x43DD30` | `elf_validate` | Bounds-check all headers/sections |
| `0x43DA80` | `elf_extent` | Compute maximum referenced offset |
| `0x43D970` | `is_elf_magic` | Magic number check (`0x7F454C46`) |
| `0x43D9A0` | `is_elf64` | `e_ident[EI_CLASS] == 2` |
| `0x43D9B0` | `is_rel_elf` | `e_type == ET_REL (1)` |
| `0x43DA00` | `is_lto_elf` | LTO flag in `e_flags` |
| `0x43DA40` | `is_mercury_capable` | Mercury flag in `e_flags` |

## Confidence Assessment

All functions in this page were decompiled from the nvlink v13.0 binary and hand-analyzed. Confidence is high for every listed accessor because:

- The Elf64 family at `0x448360`--`0x448750` is tightly clustered and every function follows the same documented ELF header layout. Offsets match the System V ELF specification exactly (e.g., `e_shnum` at Elf64 offset 60, `e_shstrndx` at offset 62, `sh_size` at shdr offset 32).

- The Elf32 family at `0x46B590`--`0x46B810` uses the same pattern with Elf32 offsets, providing cross-validation.

- Control flow in the accessors is simple enough that the decompiler output can be traced line-by-line without ambiguity. Only `sub_43DA80` (`elf_extent`) and `sub_43DD30` (`elf_validate`) have enough branching that some paths required careful reading.

- Every accessor address was spot-checked against actual call sites in `sub_45E7D0` (`merge_elf`), which uses these functions hundreds of times per input object. The caller's usage pattern confirms the function semantics (e.g., `sub_448750` is called with a symtab shdr and a symbol index, which matches the documented `elf64_symbol_shndx` signature).

| Function | Confidence | Rationale |
|---|---|---|
| Accessor family (both classes) | High | Simple offset arithmetic, verified against ELF spec |
| `sub_448600` / `sub_4486A0` / `sub_448750` | High | Matches standard Elf64_Sym layout |
| `sub_43DD30` (validate) | High | Every path traced against ELF spec; extent computation verified |
| `sub_43DA80` (extent) | Medium | Complex control flow with multiple overflow checks; semantic meaning of non-canonical vendor-type skips inferred from context |
| `sub_43DA40` / `sub_43DA00` | High | Trivial two-branch flag checks |
| `sub_476BF0` (file load) | High | Standard stdio pattern with arena allocation |
| Identity functions (`_header`) | High | Compiled as `mov rax, rdi; ret` |

## Caller Usage in merge_elf

The primary consumer of these accessors is `sub_45E7D0` (`merge_elf`), the 89KB giant function that processes one input object at a time during the merge phase. A typical Elf64 merge uses the accessors in the following pattern:

```c
// Pseudocode extracted from sub_45E7D0 lines 633-1730
void *hdr     = elf64_header(elf_base);                    // sub_448360
void *symtab  = elf64_section_by_type(elf_base, 2);        // sub_4484F0 (SHT_SYMTAB)
uint32_t link = *(u32 *)(symtab + 40);                     // symtab->sh_link
void *strtab  = elf64_section_by_index(elf_base, link);    // sub_448370
uint32_t sn   = elf64_section_count(elf_base);             // sub_448730

for (uint32_t sym_idx = 1; sym_idx < num_syms; sym_idx++) {
    uint32_t shndx = elf64_symbol_shndx(elf_base, symtab, sym_idx);  // sub_448750
    void    *sym   = elf64_symbol_by_index(elf_base, sym_idx);       // sub_448600
    const char *name = elf64_string_at(elf_base, (u32 *)sym);        // sub_448590
    // ... process symbol into global table ...
}

for (uint32_t sec_idx = 1; sec_idx < sn; sec_idx++) {
    void *shdr = elf64_section_by_index(elf_base, sec_idx);
    void *data = elf64_section_data(elf_base, shdr);                 // sub_448560
    // ... process .nv.* sections, relocations, etc ...
}
```

The Elf32 path is structurally similar but calls `sub_46Bxxx` equivalents where available and open-codes symbol access. The total tour through `merge_elf` may invoke these accessors several thousand times per input cubin.

## Design Notes

**No mmap**. nvlink reads files entirely into arena-allocated heap memory. This simplifies lifetime management (arena teardown frees everything) and avoids page-fault cost during random-access section iteration, at the cost of RSS proportional to total input size. For typical CUDA link jobs (a few megabytes of cubins), this is a non-issue. For LTO jobs with many large IR modules, memory usage can be significant.

**No libelf**. The accessor functions are hand-written byte-offset arithmetic. This avoids a library dependency and keeps the binary self-contained. The trade-off is that any ELF format extension requires updating multiple parallel function families (Elf32 and Elf64).

**Identity header functions**. Both `elf64_header` and `elf32_header` are identity functions (`return a1`). They exist as named abstractions to support a function-pointer dispatch table that the ELF wrapper uses to call the correct family based on class. In a debug build they would contain assertions; in the release build they compile to identity functions.

**Validation is conservative**. The validation function rejects any ELF where any section's data extent exceeds the file size. It does not attempt to repair or truncate. The caller (`main()`) reports the error and aborts with `"cubin not an elf?"` or similar.

**Linear section name search**. The `section_by_name` functions do a linear scan of all section headers for every lookup. This is called hundreds of times per input cubin during merge (once per section to check for `.nv.info`, `.nv.constant`, `.nv.shared`, etc.). For a typical cubin with 20–50 sections, this is fast enough. For a cubin with 1,000+ sections (possible with heavy template instantiation), this becomes quadratic in the merge phase.

**Parallel Elf32/Elf64 code paths**. The two accessor families are structurally identical but separately maintained. Any bug in one family must be fixed in both. In practice, nvlink's Elf32 path is mostly untested — all contemporary CUDA toolchains produce Elf64 cubins — so the Elf32 code has atrophied (e.g., no symbol accessor family exists).

**24-byte symbol stride is hardcoded**. The Elf64 symbol accessors (`sub_448600`, `sub_4486A0`) use a hardcoded stride of 24 rather than reading `sh_entsize`. This creates a minor correctness gap for non-standard ELFs but keeps the arithmetic simple and the code shorter.

**Pointer-to-offset string lookup API**. `sub_448590` takes a `uint32_t *` rather than `uint32_t` for the name offset. This is a tight optimization: callers pass the address of the `sh_name` or `st_name` field directly from the ELF struct and avoid an intermediate load. The trade-off is a slightly unusual API.

**No caching**. Every accessor recomputes everything from the ELF base on every call. There is no per-input-file cache of "the symtab shdr pointer" or "the shstrtab base pointer". Callers like `merge_elf` that make many lookups within a single input object cache these pointers locally themselves.

## See Also

- [File Type Detection](file-type-detection.md) — the 56-byte probe and `is_elf` predicate that triggers ELF parsing
- [Cubin Loading](cubin-loading.md) — architecture validation after ELF header parsing
- [Symbol Record](../structs/symbol-record.md) — the in-memory symbol struct built from Elf64_Sym entries via these accessors
- [Section Record](../structs/section-record.md) — the in-memory section struct built from Elf64_Shdr entries via these accessors
- [Device ELF Format](../elf/device-elf-format.md) — `e_ident`, `e_type`, `e_machine`, `e_flags` fields in CUDA device ELFs
- [NVIDIA Sections](../elf/nvidia-sections.md) — `.nv.*` section types found via the section accessors
- [Merge Phase](../pipeline/merge.md) — the 89KB function that calls these ELF accessors per input object
- [Symbol Resolution](../linker/symbol-resolution.md) — global symbol table built from results of these accessors
- [ELF Serialization](../elf/serialization.md) — the output-side counterpart that writes ELF format
- [Input File Loop](../pipeline/input-loop.md) — the dispatch loop that triggers file loading via `sub_476BF0`
- [Host ELF Embedding](host-elf.md) — host ELF parsing for embedded fatbin extraction
- [Memory Arenas](../infra/memory-arenas.md) — arena allocator backing all ELF buffer allocations
