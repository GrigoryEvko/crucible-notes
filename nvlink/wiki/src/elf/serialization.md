# ELF Serialization

The ELF serialization subsystem converts nvlink's in-memory ELF wrapper (`elfw`) into a flat byte stream suitable for writing to a file or a contiguous memory buffer. The core engine is `sub_45BF00` (13,258 bytes, 532 decompiled lines at `0x45BF00`), which performs a single linear pass through the wrapper, emitting bytes in a strict order through a polymorphic writer abstraction (`sub_45B6D0`). Two top-level entry points drive the process: `sub_45C920` serializes directly to a `FILE*` (non-Mercury targets), and `sub_45C950` serializes into a pre-allocated memory buffer (Mercury targets that require FNLZR post-link transformation). A companion function `sub_45C980` computes the exact byte count of the serialized output without writing anything, used by the Mercury path to pre-allocate the buffer.

This page documents the serialization machinery at reimplementation grade. For the higher-level dispatch logic in `main()` that decides between file and memory output, the FNLZR post-link transform, register-link-binaries, and DOT callgraph output, see [Output Writing](../pipeline/output.md).

## Key Facts

| Property | Value |
|---|---|
| Serialization engine | `sub_45BF00` at `0x45BF00` (13,258 bytes, 532 lines) |
| Polymorphic writer | `sub_45B6D0` at `0x45B6D0` (5 modes) |
| File-mode factory | `sub_45B950` at `0x45B950` (creates mode 3 writer) |
| Memory-mode factory | `sub_45BA30` at `0x45BA30` (creates mode 4 writer) |
| Writer destructor | `sub_45B6A0` at `0x45B6A0` |
| Size computation | `sub_45C980` at `0x45C980` |
| File entry point | `sub_45C920` at `0x45C920` |
| Memory entry point | `sub_45C950` at `0x45C950` |
| Program header emitter | `sub_45BAA0` at `0x45BAA0` (5,657 bytes, 228 lines) |
| Vector append (mode 2) | `sub_44FC10` at `0x44FC10` |
| Writer context size | 40 bytes |
| Error handler | `sub_467460` -- fatal error on write failure |
| Error strings | `"writing file"`, `"Negative size encountered"`, `"section size mismatch"` |

## Entry Points

Both entry points follow the same three-step pattern: construct a writer, serialize the ELF, destroy the writer.

### sub\_45C920 -- Write to FILE\*

```c
void write_elf_to_file(FILE* file, elfw_t* elfw) {
    elf_writer* w = create_file_writer(file, elfw);   // sub_45B950, mode 3
    serialize_elf(w, elfw);                            // sub_45BF00
    destroy_writer(w, elfw);                           // sub_45B6A0
}
```

Called by `main()` for non-Mercury targets. The `FILE*` is opened externally by `main()` with `fopen(output_filename, "wb")` and closed after this call returns.

### sub\_45C950 -- Write to Memory Buffer

```c
void write_elf_to_memory(void* buffer, elfw_t* elfw) {
    elf_writer* w = create_memory_writer(buffer, elfw);  // sub_45BA30, mode 4
    serialize_elf(w, elfw);                              // sub_45BF00
    destroy_writer(w, elfw);                             // sub_45B6A0
}
```

Called by `main()` for Mercury targets (sm >= 100). The buffer is pre-allocated to the exact size returned by `sub_45C980`. After this call, the buffer holds the complete ELF image ready for FNLZR post-link transformation.

## Writer Context: 40-Byte Struct

Both factory functions allocate a 40-byte context object from the memory arena. The context drives the polymorphic dispatch in `sub_45B6D0`.

```
struct elf_writer {                // 40 bytes
    int32_t   mode;                // +0:  backend selector (0..4)
    int32_t   flags;               // +4:  always 0 in observed paths
    uint64_t  callback_state;      // +8:  state for callback mode (mode 0)
    void*     rewind_fn;           // +16: rewind function pointer
    void*     cleanup_fn;          // +24: destructor called by sub_45B6A0
    void*     dest;                // +32: target -- FILE*, buffer ptr, or callback context
};
```

### sub\_45B950 -- File-Mode Factory (Mode 3)

```c
elf_writer* create_file_writer(FILE* file, elfw_t* elfw) {
    arena_t* arena = get_arena(file, elfw);         // sub_44F410 -> arena at +24
    elf_writer* w = arena_alloc(arena, 40);         // sub_4307C0
    w->mode         = 3;
    w->flags        = 0;
    w->callback_state = 0;
    w->rewind_fn    = &rewind;                      // libc rewind()
    w->cleanup_fn   = NULL;
    w->dest         = file;                         // FILE* stored at +32
    return w;
}
```

The `rewind_fn` field stores a pointer to libc `rewind()`. This is not called during normal serialization -- it exists so callers could rewind the output stream for multi-pass writing if needed. The `cleanup_fn` is NULL because the `FILE*` lifetime is managed by `main()`.

### sub\_45BA30 -- Memory-Mode Factory (Mode 4)

```c
elf_writer* create_memory_writer(void* buffer, elfw_t* elfw) {
    arena_t* arena = get_arena(buffer, elfw);
    elf_writer* w = arena_alloc(arena, 40);
    w->mode         = 4;
    w->flags        = 0;
    w->callback_state = 0;
    w->rewind_fn    = NULL;
    w->cleanup_fn   = NULL;
    w->dest         = buffer;                       // write cursor, advances
    return w;
}
```

The `dest` pointer serves as a write cursor that advances after each `memcpy`. By the time serialization completes, `dest` points past the end of the buffer.

## Polymorphic Writer: sub\_45B6D0

Every byte of serialized output passes through this 5-mode dispatch function. The writer context pointer `w` can also be NULL, in which case the function writes to stdout as a fallback.

```c
int64_t elf_write(elf_writer* w, void* data, size_t len) {
    if (w == NULL)
        return fwrite(data, 1, len, stdout);    // NULL writer -> stdout

    switch (w->mode) {
    case 0:  // Callback mode
        return ((write_callback)w->callback_state)(w->dest, data, len);

    case 1:  // No-op / size-counting mode
        return len;                              // consume, write nothing

    case 2:  // Vector-backed growable buffer
        vector_append(w->dest, data, len);       // sub_44FC10
        return len;

    case 3:  // FILE* mode
        if (w->dest)
            return fwrite(data, 1, len, w->dest);
        // dest is NULL -> byte-by-byte putc to stdout
        for (size_t i = 0; i < len; i++)
            _IO_putc(((uint8_t*)data)[i], stdout);
        return len;

    case 4:  // Direct memcpy mode (advancing cursor)
        memcpy(w->dest, data, len);
        w->dest += len;
        return len;

    default:
        return -1;
    }
}
```

### Mode Summary

| Mode | Name | Backend | Destination | Used By |
|------|------|---------|-------------|---------|
| 0 | Callback | User-supplied function pointer at `+8` | Context at `+32` | Internal / extensibility |
| 1 | No-op | Discards data | None | Dry-run size counting |
| 2 | Vector | Growable arena-backed chunk list | Vector header at `+32` | Intermediate buffering |
| 3 | FILE\* | `fwrite` to file descriptor | `FILE*` at `+32` | `sub_45C920` (non-Mercury output) |
| 4 | Memcpy | `memcpy` with advancing pointer | Buffer base at `+32` | `sub_45C950` (Mercury output) |

Mode 0 stores the callback function pointer at offset `+8` (the `callback_state` field), and the opaque context at offset `+32`. The callback receives `(context, data, len)`.

Mode 1 is never explicitly constructed in observed output paths but is a valid mode in the switch. It returns `len` without writing, serving as a dry-run counter.

Mode 2 uses `sub_44FC10` (vector\_append) to write into a growable arena-backed chunk list. The vector is a linked list of 24-byte chunk headers, each containing `{capacity, remaining, data_ptr}`. When the current chunk cannot hold the write, a new chunk is allocated (sized to at least the vector's default chunk size or the write size, whichever is larger). The vector header at `+32` tracks: `+0` default chunk size, `+8` total bytes written, `+16` chunk list tail pointer, `+24` current chunk, `+32` last chunk.

Mode 3 with a NULL `dest` degrades to byte-by-byte `_IO_putc` to stdout. This path is reachable but not used in practice -- the factory always sets `dest` to a valid `FILE*`.

### Writer Cleanup: sub\_45B6A0

```c
void destroy_writer(elf_writer* w, void* unused) {
    if (w) {
        if (w->cleanup_fn)
            w->cleanup_fn(w->dest);
        arena_free(w, unused);           // sub_431000
    }
}
```

Checks offset `+24` for a cleanup function. If present, calls it with `dest` as argument. Then frees the writer context via the arena deallocator. In both observed paths (modes 3 and 4), `cleanup_fn` is NULL, so only the arena free runs.

## Serialization Order: sub\_45BF00

`sub_45BF00` takes a writer context and the ELF wrapper, then emits the complete ELF in a strict sequential order. The function handles both ELF32 (class 1, `elfw+4 == 1`) and ELF64 (class 2, `elfw+4 == 2`). Every write goes through `sub_45B6D0` and the return value is checked against the expected byte count; any mismatch triggers `sub_467460` with `"writing file"`.

### ELF Wrapper Field Map

The serializer reads these fields from the ELF wrapper:

| Offset | ELF32 | ELF64 | Field |
|--------|-------|-------|-------|
| `+4` | `uint8` | `uint8` | `e_ident[EI_CLASS]` (1=ELF32, 2=ELF64) |
| `+7` | `uint8` | `uint8` | `e_ident[EI_OSABI]` ('A' = 0x41 for special flag handling) |
| `+16` | `uint16` | `uint16` | `e_type` (2 = `ET_EXEC`) |
| `+28` | `uint32` | -- | `e_phoff` (ELF32 program header offset; computed by serializer as `e_shoff + e_shnum * e_shentsize`) |
| `+32` | `uint32` | `uint64` | ELF32: `e_shoff`. ELF64: `e_phoff` (standard Elf64\_Ehdr field; serializer computes `e_shoff + e_shnum * e_shentsize` and writes here before Phase 1) |
| `+40` | -- | `uint64` | `e_shoff` (ELF64 section header offset) |
| `+44` | `uint16` | -- | `e_phnum` (ELF32; written by serializer preamble) |
| `+46` | `uint16` | -- | `e_shentsize` (ELF32, always 40) |
| `+48` | `uint16/uint32` | `uint32` | `e_shnum` / `e_flags` (dual-use by class) |
| `+56` | -- | `uint16` | `e_phnum` (ELF64; written by serializer preamble) |
| `+58` | -- | `uint16` | `e_shentsize` (ELF64, always 64) |
| `+60` | -- | `uint16` | `e_shnum` (ELF64) |
| `+304` | `uint32` | `uint32` | strtab entry count |
| `+312` | `uint32` | `uint32` | shstrtab entry count |
| `+328` | `ptr` | `ptr` | strtab string array |
| `+336` | `ptr` | `ptr` | shstrtab string array |
| `+344` | `ptr` | `ptr` | positive symbol array (`pos_symbol_array`) -- serialized as `.symtab` content |
| `+360` | `ptr` | `ptr` | section array (`section_array`) -- all section records |
| `+368` | `ptr` | `ptr` | section order index array (maps output slot -> section index) |

### Complete Write Sequence

```
Phase 1:  ELF header (52 or 64 bytes)
Phase 2:  1 byte null padding
Phase 3:  .shstrtab contents (null-terminated section name strings)
Phase 4:  .strtab contents (null-terminated symbol name strings)
Phase 5:  Alignment padding to section[3].sh_offset
Phase 6:  .symtab contents (Elf32_Sym / Elf64_Sym entries from pos_symbol_array)
Phase 7:  Section data (sections 4..N-1, with fragment-list traversal)
Phase 8:  Post-section padding to e_shoff
Phase 9:  Section headers (40 or 64 bytes each)
Phase 10: Program header table (sub_45BAA0, conditional)
```

### Phase 1: ELF Header

The serializer writes the raw ELF wrapper structure from offset 0 through the header end. ELF32 headers are 52 bytes (`sizeof(Elf32_Ehdr)`); ELF64 headers are 64 bytes (`sizeof(Elf64_Ehdr)`). The wrapper's first bytes _are_ the ELF header (`e_ident` through `e_shstrndx`), so this is a direct write of the wrapper's leading bytes.

```c
size_t hdr_size = (elf_class == 2) ? 64 : 52;
elf_write(writer, elfw, hdr_size);
```

### Phase 2: Null Padding Byte

A single zero byte is always written immediately after the header. This serves as a null terminator/separator before the string tables:

```c
uint8_t zero = 0;
elf_write(writer, &zero, 1);
```

The running offset counter (`v4`) starts at `hdr_size + 1`.

### Phase 3: Section Header String Table (.shstrtab)

The section name strings are stored in an ordered array at `elfw+336`, with the count at `elfw+312`. Index 0 in the array is always NULL (the `SHN_UNDEF` entry's empty name was already emitted as the zero byte in Phase 2). The loop writes entries starting at index 1:

```c
// Leading null byte already written in Phase 2
for (uint32_t i = 1; i <= shstrtab_count; i++) {
    char* name = shstrtab_array[i];
    if (name) {
        size_t len = strlen(name) + 1;    // include NUL terminator
        elf_write(writer, name, len);
        running_offset += len;
    }
}
```

Each string is written with its null terminator. The running offset tracks total bytes emitted.

### Phase 4: Symbol String Table (.strtab)

Identical structure, sourced from `elfw+328` (count at `elfw+304`). A leading null byte is emitted first (the empty string at `.strtab[0]`):

```c
uint8_t zero = 0;
elf_write(writer, &zero, 1);
running_offset += 1;

for (uint32_t j = 1; j <= strtab_count; j++) {
    char* name = strtab_array[j];
    if (name) {
        size_t len = strlen(name) + 1;
        elf_write(writer, name, len);
        running_offset += len;
    }
}
```

### Phase 5: Alignment Padding

After both string tables, the function looks up section index 3 from the section ordered list via `sub_464DB0(elfw->section_list, 3)`. This section's file offset (`sh_offset` at `sec+16` for ELF32 or `sec+24` for ELF64) determines how many zero-pad bytes are needed:

```c
section_t* sec3 = list_get(elfw->sections, 3);
uint64_t target = (elf_class == 2) ? sec3->sh_offset_64 : sec3->sh_offset_32;
int64_t gap = target - running_offset;

if (gap < 0)
    fatal_error("Negative size encountered");

for (int k = 0; k < gap; k++)
    elf_write(writer, &zero, 1);

running_offset = target;
```

Section 3 is the `.symtab` section in nvlink's canonical ordering: index 0 = null, 1 = `.shstrtab`, 2 = `.strtab`, 3 = `.symtab`. The alignment padding ensures the symbol table (and subsequent sections) begin at their computed file offsets.

The padding pattern is always `0x00` and is emitted one byte per `elf_write` call. The serializer never emits SASS NOP encodings, architecture-specific fill instructions, or any non-zero pattern -- code-section alignment fences and inter-section gaps are byte-for-byte identical to data-section gaps. If a `.text*` section needs intra-section NOP padding for instruction-stream alignment, that padding must already be baked into its fragment list (Phase 7c) by the layout / merge phases before serialization runs.

### Phase 6: Symbol Table Contents (.symtab)

After Phase 5 pads to the `.symtab` section's file offset, the serializer iterates the **positive symbol array** at `elfw+344` (`pos_symbol_array`, a `SortedArray*` of 48-byte `SymbolRecord` entries) and writes the first 24 bytes (ELF64) or 16 bytes (ELF32) of each record. The internal `SymbolRecord` is laid out so that its leading bytes already match the ELF-standard `Elf64_Sym` / `Elf32_Sym` on-disk format, so a direct memcpy produces the `.symtab` contents:

```c
size_t sym_entry_size = (elf_class == 2) ? 24 : 16;   // Elf64_Sym / Elf32_Sym
size_t sym_count = sorted_array_count(elfw->pos_symbol_array);  // sub_464BB0(+344)

for (uint32_t p = 0; p < sym_count; p++) {
    SymbolRecord* sym = sorted_array_get(elfw->pos_symbol_array, p);  // sub_464DB0(+344)
    elf_write(writer, sym, sym_entry_size);
    running_offset += sym_entry_size;
}
```

The `pos_symbol_array` holds all locally-indexed symbols (index 0 = null symbol, then local symbols and section symbols). Global/weak symbols (negative indices) are materialized into the positive array by the finalize phase before serialization; by the time `sub_45BF00` runs, `pos_symbol_array` contains every symbol that belongs in `.symtab`.

Note: this phase writes **symbol table content**, not program headers. nvlink does not emit a program header table here. The ELF-standard program header table, when needed, is constructed and written by `sub_45BAA0` in Phase 10. That function reads the section array at `elfw+360` (via the order array at `elfw+368`) to synthesize `Elf_Phdr` entries from section metadata.

### Phase 7: Section Data

This is the largest and most complex phase. The loop iterates sections 4 through `e_shnum - 1` (sections 0-3 were handled inline as header/string tables/symtab). The section order array at `elfw+368` provides the index mapping.

For each section, the serializer performs four operations:

#### 7a. Inter-Section Alignment Padding

```c
section_t* sec = list_get(elfw->sections, section_order[i]);
uint64_t sec_offset = (elf_class == 2) ? sec->sh_offset_64 : sec->sh_offset_32;
int64_t gap = sec_offset - running_offset;

if (gap < 0)
    fatal_error("Negative size encountered");
if (gap > 0) {
    for (int k = 0; k < gap; k++)
        elf_write(writer, &zero, 1);
    running_offset = sec_offset;
}
```

#### 7b. NOBITS / No-Data Section Skip

Sections that carry no file data are skipped entirely. The check combines standard `SHT_NOBITS` (type 8) with four CUDA-specific no-data section types using a bitmask:

```c
uint32_t sh_type = sec->sh_type;         // at sec+4
bool is_nobits = (sh_type == SHT_NOBITS);   // type 8

uint32_t cuda_idx = sh_type - 0x70000008;
if (cuda_idx <= 14)
    is_nobits |= (0x400D >> cuda_idx) & 1;

if (is_nobits)
    goto next_section;   // skip, emit no bytes
```

The bitmask `0x400D` = binary `0100 0000 0000 1101` selects these offsets from `0x70000008`:

| Bit | Offset | Type Value | Identity |
|-----|--------|------------|----------|
| 0 | +0 | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` (has NOBITS semantics in this bitmask context) |
| 2 | +2 | `0x7000000A` | `SHT_CUDA_SHARED` (shared memory, no file data) |
| 3 | +3 | `0x7000000B` | `SHT_CUDA_RELOCINFO` (relocation action table, no file data in this context) |
| 14 | +14 | `0x70000016` | `SHT_CUDA_MERC` (per-kernel Mercury section, no data in this context) |

Combined with `SHT_NOBITS` (8), these five types produce no bytes in the serialized output. Their `sh_size` contributes to memory-only segments but occupies zero file space.

#### 7c. Fragment-List Data Traversal

For sections with data, the content is stored as a singly-linked list of fragment nodes rooted at `sec+72`. This linked list represents the section's data as a series of possibly non-contiguous fragments, each with an offset within the section and a data pointer. The fragment list is built during the merge phase as section data from multiple input objects is concatenated.

```
Fragment linked list (rooted at sec+72):

  node -> [next_ptr | descriptor_ptr]
            |
            v
          descriptor -> [data_ptr     (+0)  ]
                        [sec_offset   (+8)  ]
                        [reserved     (+16) ]
                        [frag_size    (+24) ]
```

Each node is a pair of pointers: `node[0]` is the next-node pointer, `node[1]` is a pointer to the fragment descriptor. The descriptor contains the data pointer, the offset within the section where this fragment belongs, and the fragment size.

The traversal emits inter-fragment gap padding (zeros) when a fragment's offset exceeds the current write cursor, then writes the fragment data:

```c
frag_node_t* node = sec->frag_list;     // sec+72
uint64_t cursor = 0;                     // position within section

while (node) {
    frag_desc_t* desc = node->descriptor;

    // Gap padding: fill zeros between cursor and fragment offset
    if (desc->sec_offset > cursor && desc->sec_offset != (uint64_t)-1) {
        uint64_t gap = desc->sec_offset - cursor;
        for (uint64_t k = 0; k < gap; k++)
            elf_write(writer, &zero, 1);
        cursor = desc->sec_offset;
        running_offset += gap;
    }

    // Write fragment data
    elf_write(writer, desc->data, desc->frag_size);
    cursor += desc->frag_size;
    running_offset += desc->frag_size;

    node = node->next;
}
```

A `desc->sec_offset` of `(uint64_t)-1` (0xFFFFFFFFFFFFFFFF) is treated as a sentinel meaning "no offset specified; append immediately after the previous fragment." This avoids emitting a spurious gap.

NVIDIA-specific TLV-bearing sections -- `.nv.info`, per-kernel `.nv.info.<func>`, `.nv.shared.<func>`, `.nv.constant{0,2,3}.<func>`, `.nv.callgraph`, `.nv.rel.action`, and the rest of the `0x70000000`-range section family that does **not** match the NOBITS bitmask in 7b -- carry no special emission logic in `sub_45BF00`. They flow through this fragment-list traversal identically to standard sections: the merge / finalize phases assemble the TLV byte stream into one or more fragments hanging off `sec+72`, and the serializer concatenates them at the section's computed `sh_offset`. See [.nv.info](nv-info.md) for the TLV record layout that ends up in those fragments.

#### 7d. Size Validation

After writing all fragments for a section, the total bytes written (`cursor`) is checked against the section's declared `sh_size`:

```c
uint64_t sh_size = (elf_class == 2) ? sec->sh_size_64 : sec->sh_size_32;
// sh_size_32 at sec+20, sh_size_64 at sec+32

if (sh_size < cursor) {
    // Build diagnostic string: "<section_name> section size mismatch"
    char* name = sec->name;            // sec+96
    if (name) {
        size_t nlen = strlen(name);
        char* msg = malloc(nlen + 24);
        memcpy(msg, name, nlen);
        memcpy(msg + nlen, " section size mismatch", 23);  // includes NUL
        fatal_error(msg);
        free(msg);
    } else {
        fatal_error(" section size mismatch");
    }
}
```

The string `" section size mismatch"` is loaded from a 16-byte SSE constant (`xmmword_1D3B870`) and a 7-byte tail (`"smatch"` + NUL), concatenated with the section name at runtime. The diagnostic string is heap-allocated (`malloc`), used in the error call, then freed.

### Phase 8: Post-Section Padding

After all section data, the function pads to reach `e_shoff` (the section header table file offset):

```c
uint64_t shoff = (elf_class == 2) ? elfw->e_shoff_64 : elfw->e_shoff_32;

if (shoff > running_offset) {
    uint64_t gap = shoff - running_offset;
    for (uint64_t k = 0; k < gap; k++)
        elf_write(writer, &zero, 1);
}
```

### Phase 9: Section Headers

The final sequential loop writes the raw section header entries from the section ordered list:

```c
size_t shdr_size = (elf_class == 2) ? 64 : 40;

for (uint32_t s = 0; s < e_shnum; s++) {
    void* shdr = list_get(elfw->sections, section_order[s]);
    elf_write(writer, shdr, shdr_size);
}
```

The section order array at `elfw+368` maps logical indices to the ordered list, ensuring section headers appear in the canonical order that matches the `sh_offset` assignments from the layout phase.

The loop iterates `e_shnum` times (for indices 0 through `e_shnum - 1`), with the termination condition checking `4 * (e_shnum - 1) + 4 != offset` where `offset` advances by 4 per iteration (indexing into the 32-bit section order array).

### Phase 10: Program Header Table (sub\_45BAA0)

This phase is conditional. It executes only when:
1. `e_type == ET_EXEC` (value 2 at `elfw+16`), AND
2. The `v126` flag is set (true when no special `e_flags` masking suppresses program headers)

For ELF64, `v126` is true when `(e_flags & mask) == 0` where `mask` is 1 if `e_ident[7] == 'A'`, else 0x80000000. For ELF32, `v126` is always true when `e_type == ET_EXEC`.

`sub_45BAA0` constructs a proper ELF-standard program header table on the stack and writes it as a single blob at the end of the file. It first iterates all sections to compute cumulative sizes for the `.shstrtab` and `.strtab` regions (using `sub_438BB0` for alignment accumulation), then builds 2 to 4 program header entries.

#### Preamble: e\_phoff and e\_phnum Computation

Before Phase 1 writes the ELF header, `sub_45BF00` computes `e_phoff` and `e_phnum` when `e_type == ET_EXEC` and flag suppression is not active. The function iterates all sections via the order array, capturing the `sh_offset` of the first section with flag bit 0 set (`.shstrtab`-group base) and the first with flag bit 1 set (`.strtab`-group base). The program header count is then:

```c
int phnum = 2;                            // PT_PHDR + final PT_LOAD (always present)
if (strtab_base != 0)   phnum = 3;       // + PT_LOAD for .strtab segment
if (shstrtab_base != 0) phnum++;         // + PT_LOAD for .shstrtab segment

// Write e_phnum into ELF header
// ELF32: *(uint16*)(elfw+44) = phnum
// ELF64: *(uint16*)(elfw+56) = phnum

// Compute e_phoff = e_shoff + e_shnum * e_shentsize
// (program headers placed immediately after section header table)
// ELF32: *(uint32*)(elfw+28) = *(uint32*)(elfw+32) + e_shnum * e_shentsize
// ELF64: *(uint64*)(elfw+32) = *(uint64*)(elfw+40) + e_shnum * e_shentsize
```

These values are written into the wrapper's ELF header fields _before_ Phase 1 serializes the header, so the on-disk header already contains the correct `e_phoff` and `e_phnum`.

#### Section Scan

The function walks all sections via the section order array, classifying each by flags bits at `sec+8` (ELF64) or `sec+8` (ELF32):
- **Flag bit 0 set** (`.shstrtab`-group): For NOBITS-type sections, accumulates the section's alignment-adjusted size via `sub_438BB0`. For non-NOBITS sections, computes `sh_offset + sh_size - shstrtab_base` as the file extent. Tracks the last such section to determine the segment's total file and memory extent.
- **Flag bit 1 set** (`.strtab`-group): Records `sh_offset + sh_size - strtab_base` to compute the `.strtab` segment extent.

For NOBITS-type sections within the `.shstrtab` group, the alignment contribution is accumulated but no file data is counted (same bitmask check as Phase 7b). The accumulated NOBITS size is added to `p_memsz` but not `p_filesz` for the `.shstrtab` segment.

#### Program Header Construction -- ELF64

When `elf_class == 2`, each program header entry is 56 bytes (`sizeof(Elf64_Phdr)`). The entries are built on the stack and written as a single contiguous blob:

```c
Elf64_Phdr phdr[4];
memset(phdr, 0, sizeof(phdr));

// Entry 0 (always present): PT_PHDR -- self-referential program header entry
phdr[0].p_type   = PT_PHDR;          // 6
phdr[0].p_flags  = PF_R | PF_X;      // 5
phdr[0].p_offset = e_phoff;          // = e_shoff + e_shnum * 64
phdr[0].p_filesz = e_phnum * 56;     // program header table size
phdr[0].p_memsz  = e_phnum * 56;
phdr[0].p_align  = 8;

int slot = 1;   // next available slot
int next = 2;   // potential next count after optional entries

// Entry 1 (optional): PT_LOAD for .strtab segment
if (strtab_base != 0) {
    phdr[1].p_type   = PT_LOAD;       // 1
    phdr[1].p_flags  = PF_R | PF_X;   // 5
    phdr[1].p_offset = strtab_base;
    phdr[1].p_filesz = strtab_extent;
    phdr[1].p_memsz  = strtab_extent;
    phdr[1].p_align  = 8;
    slot = 2;
    next = 3;
}

// Entry N (optional): PT_LOAD for .shstrtab segment
if (shstrtab_base != 0) {
    phdr[slot].p_type   = PT_LOAD;    // 1
    phdr[slot].p_flags  = PF_R | PF_W;  // 6
    phdr[slot].p_offset = shstrtab_base;
    phdr[slot].p_filesz = shstrtab_file_extent;
    phdr[slot].p_memsz  = shstrtab_file_extent + nobits_accumulated;
    phdr[slot].p_align  = 8;
    slot = next;
}

// Last entry: PT_LOAD covering the program header table itself
phdr[slot].p_type   = PT_LOAD;       // 1
phdr[slot].p_flags  = PF_R | PF_X;   // 5
phdr[slot].p_offset = e_phoff;
phdr[slot].p_filesz = e_phnum * 56;
phdr[slot].p_memsz  = e_phnum * 56;
phdr[slot].p_align  = 8;

// Write entire array
elf_write(writer, phdr, e_phnum * 56);
```

Note: the first entry is `PT_PHDR` (type 6), which identifies the program header table to the ELF loader. The last entry is `PT_LOAD` (type 1), which ensures the program header table data is actually loaded into memory. Both entries point to the same file region (`e_phoff`), which is standard ELF practice. The `.strtab` segment uses `PF_R|PF_X` (5), while the `.shstrtab` segment uses `PF_R|PF_W` (6).

#### Program Header Construction -- ELF32

When `elf_class == 1`, each entry is 32 bytes (`sizeof(Elf32_Phdr)`). The structure differs in field ordering -- note that `p_flags` is at offset +24 in ELF32 (after `p_filesz`/`p_memsz`), not at offset +4 as in ELF64:

```
Elf32_Phdr: { p_type(+0), p_offset(+4), p_vaddr(+8), p_paddr(+12),
              p_filesz(+16), p_memsz(+20), p_flags(+24), p_align(+28) }
```

The same 2-to-4 entry construction applies with 32-bit addresses and sizes:

```c
phdr32[0].p_type   = PT_PHDR;        // 6
phdr32[0].p_flags  = PF_R | PF_X;    // 5
phdr32[0].p_offset = e_phoff;        // = e_shoff + e_shnum * 40
phdr32[0].p_filesz = e_phnum * 32;
phdr32[0].p_memsz  = e_phnum * 32;
phdr32[0].p_align  = 4;              // 4-byte alignment for ELF32
// ... same entry pattern as ELF64 with 32-bit values ...
```

#### Entry Summary

| Entry | Type | Flags | Offset | Size | Condition |
|-------|------|-------|--------|------|-----------|
| 0 | `PT_PHDR` (6) | `PF_R\|PF_X` (5) | `e_phoff` | `e_phnum * phdr_size` | Always |
| 1 | `PT_LOAD` (1) | `PF_R\|PF_X` (5) | `strtab_base` | `strtab_extent` | `strtab_base != 0` |
| N | `PT_LOAD` (1) | `PF_R\|PF_W` (6) | `shstrtab_base` | file + NOBITS | `shstrtab_base != 0` |
| Last | `PT_LOAD` (1) | `PF_R\|PF_X` (5) | `e_phoff` | `e_phnum * phdr_size` | Always |

## Size Computation: sub\_45C980

Computes the total byte count of the serialized ELF without writing, used by the Mercury path to pre-allocate the buffer.

### ELF32 Path

```c
uint64_t compute_elf_size(elfw_t* elfw) {
    uint32_t e_shnum = elfw->e_shnum;              // +48, uint16
    if (e_shnum == 0)
        e_shnum = list_get(elfw->sections, 0)->sh_size;  // overflow encoding

    uint64_t result = elfw->e_shoff                // +32
                    + e_shnum * elfw->e_shentsize; // * +46

    if (elfw->e_type == ET_EXEC)                   // +16 == 2
        result += 128;                             // 4 * 32-byte phdrs

    return result;
}
```

### ELF64 Path

```c
uint64_t compute_elf_size_64(elfw_t* elfw) {
    uint32_t e_shnum = elfw->e_shnum_64;           // +60, uint16
    if (e_shnum == 0)
        e_shnum = list_get(elfw->sections, 0)->sh_size_64;

    uint64_t result = elfw->e_shoff_64             // +40
                    + e_shnum * elfw->e_shentsize_64;  // * +58

    // Flag-dependent program header reservation
    uint32_t flag_mask = (elfw->e_ident[7] == 'A') ? 0x1 : 0x80000000;
    uint32_t flags = elfw->e_flags & flag_mask;    // +48

    if (elfw->e_type == ET_EXEC && !flags)
        result += 224;                             // 4 * 56-byte phdrs

    return result;
}
```

The constants 128 (4 x 32) and 224 (4 x 56) represent the maximum program header table size -- space for up to 4 entries of the architecture-appropriate `Phdr` size.

The `e_shnum == 0` fallback handles ELF's overflow encoding: when the section count exceeds 65535, `e_shnum` is set to 0 and the actual count is stored in `sh_size` of section header entry 0 (the `SHN_UNDEF` entry).

The `e_ident[7]` check distinguishes ABI variants: when set to `'A'` (0x41), flag bit 0 is tested; otherwise, the high bit (0x80000000) is tested. Both represent a "suppress program headers" signal from the ELF flags field, preventing the 224-byte reservation.

## Canonical Section Ordering

The serialization order reflects nvlink's canonical section layout. The first four section indices have fixed roles:

| Index | Section | Written In |
|-------|---------|------------|
| 0 | null (`SHN_UNDEF`) | Phase 9 (section header only) |
| 1 | `.shstrtab` | Phase 3 (data), Phase 9 (header) |
| 2 | `.strtab` | Phase 4 (data), Phase 9 (header) |
| 3 | `.symtab` | Phase 5-7 (data at computed offset), Phase 9 (header) |
| 4..N-1 | User sections | Phase 7 (data), Phase 9 (headers) |

The string tables are serialized _before_ any section data because their content is needed by the ELF header's `e_shstrndx` and the symbol table's `st_name` fields. The symtab goes next because it references both string tables. All remaining sections follow in the order determined by the layout phase.

## Serialization Trace: Minimal ELF64 Output

This trace illustrates the byte-level output for a minimal ELF64 `ET_EXEC` binary with 6 sections (null, `.shstrtab`, `.strtab`, `.symtab`, `.text`, `.data`), 2 symbols, `.shstrtab` totaling 40 bytes of strings, `.strtab` totaling 20 bytes, `e_shoff = 0x200`, and `e_shentsize = 64`.

```
Preamble (before Phase 1):
  Iterate sections -> find strtab_base, shstrtab_base
  e_phnum = 4  (both bases non-zero)
  e_phoff = 0x200 + 6*64 = 0x380
  Write e_phnum and e_phoff into ELF header fields

Phase 1: [0x000..0x03F]  64 bytes   ELF64 header
  e_ident, e_type=2(ET_EXEC), e_phoff=0x380, e_shoff=0x200,
  e_phnum=4, e_shnum=6, e_shentsize=64

Phase 2: [0x040]          1 byte    NUL separator
  0x00 (serves as .shstrtab[0] empty string terminator)

Phase 3: [0x041..0x068]  ~40 bytes  .shstrtab string data
  ".shstrtab\0" ".strtab\0" ".symtab\0" ".text\0" ".data\0"

Phase 4: [0x069..0x07D]  ~21 bytes  NUL + .strtab string data
  0x00 "main\0" "_start\0"

Phase 5: [0x07E..0x07F]  ~2 bytes   Zero padding to .symtab offset
  Pad from running_offset (0x7E) to section[3].sh_offset (0x80)

Phase 6: [0x080..0x0AF]  48 bytes   .symtab content
  2 x Elf64_Sym (24 bytes each)

Phase 7: [0x0B0..0x1FF]  ~336 bytes Section data for indices 4-5
  Section 4 (.text): inter-section pad + fragment data
  Section 5 (.data): inter-section pad + fragment data

Phase 8: [varies..0x1FF] padding    Zero fill to reach e_shoff
  Pad from running_offset to 0x200

Phase 9: [0x200..0x37F]  384 bytes  6 section headers
  6 x Elf64_Shdr (64 bytes each) in canonical order

Phase 10: [0x380..0x45F]  224 bytes  4 program headers
  Entry 0: PT_PHDR  | PF_R|PF_X | offset=0x380 | size=224
  Entry 1: PT_LOAD  | PF_R|PF_X | strtab_base   | strtab_extent
  Entry 2: PT_LOAD  | PF_R|PF_W | shstrtab_base  | shstrtab_extent
  Entry 3: PT_LOAD  | PF_R|PF_X | offset=0x380  | size=224

Total: 0x460 bytes = e_phoff + 4*56 = 0x380 + 0xE0
       (matches compute_elf_size: e_shoff + shnum*shentsize + 224)
```

## Error Handling

Three fatal error conditions can terminate the serializer:

| Error String | Condition | Root Cause |
|---|---|---|
| `"writing file"` | `elf_write()` returns fewer bytes than requested | I/O failure (disk full, broken pipe, etc.) |
| `"Negative size encountered"` | Section offset < running offset | Layout phase produced overlapping sections |
| `"<name> section size mismatch"` | Fragment data total > `sh_size` | Section data exceeds declared size (corrupted or incompletely laid out) |

All three call `sub_467460` which is the linker's fatal error handler. The first argument (`&unk_2A5B990`) is the error context object; the second is the error message string. The handler does not return.

## Function Reference

| Address | Name | Size | Role |
|---|---|---|---|
| `0x45C920` | `write_elf_to_file` | ~48 bytes | Entry: FILE\* writer -> serialize -> cleanup |
| `0x45C950` | `write_elf_to_memory` | ~48 bytes | Entry: memcpy writer -> serialize -> cleanup |
| `0x45C980` | `compute_elf_size` | ~176 bytes | Returns total serialized byte count |
| `0x45BF00` | `serialize_elf` | 13,258 bytes | Core engine: header, strings, sections, phdrs |
| `0x45B6D0` | `elf_write` | ~256 bytes | Polymorphic 5-mode write dispatcher |
| `0x45B950` | `create_file_writer` | ~112 bytes | Allocates mode 3 writer context |
| `0x45BA30` | `create_memory_writer` | ~112 bytes | Allocates mode 4 writer context |
| `0x45B6A0` | `destroy_writer` | ~48 bytes | Calls cleanup function and frees context |
| `0x45BAA0` | `write_program_headers` | 5,657 bytes | Constructs and writes ELF Phdr table |
| `0x44FC10` | `vector_append` | ~256 bytes | Growable vector write for mode 2 |
| `0x438BB0` | `align_up` | -- | `align_up(value, align)` -- returns `value` if already aligned, else `((value / align) + 1) * align`; used by `sub_45BAA0` to accumulate NOBITS extents in the `.shstrtab` segment's `p_memsz` |
| `0x464DB0` | `list_get` | -- | Ordered list element accessor |
| `0x464BB0` | `list_size` | -- | Ordered list count accessor |
| `0x467460` | `fatal_error` | -- | Fatal error reporter (does not return) |

## Cross-References

**Internal (nvlink wiki):**

- [ELF Writer](../structs/elf-writer.md) -- The 672-byte `elfw` struct layout and the 40-byte polymorphic writer context used by the serializer
- [Program Headers](program-headers.md) -- Phase 10 program header table construction (`sub_45BAA0`) called as the final serialization step
- [Device ELF Format](device-elf-format.md) -- ELF header encoding, `e_shoff`/`e_shnum` fields, and class-dependent field widths
- [NVIDIA Section Types](nvidia-sections.md) -- Section type constants and the NOBITS bitmask (`0x400D`) used in Phase 7b to skip no-data sections
- [Output Writing](../pipeline/output.md) -- Pipeline dispatch that selects between `write_elf_to_file` and `write_elf_to_memory` entry points
- [Layout Phase](../pipeline/layout.md) -- Computes section offsets (`sh_offset`) and `e_shoff` that the serializer uses for alignment padding
- [Mercury FNLZR](../mercury/fnlzr.md) -- Mercury path pre-allocates buffer via `compute_elf_size`, serializes to memory, then passes to FNLZR
- [Memory Arenas](../infra/memory-arenas.md) -- Arena allocator (`sub_4307C0`) used by writer factories and vector-backed mode 2
- [Section Merging](../linker/section-merging.md) -- Builds the fragment linked lists at `sec+72` that Phase 7c traverses during section data emission

**Sibling wikis:**

- [ptxas: ELF Emitter](../../ptxas/output/elf-emitter.html) -- ptxas-side ELF serialization for comparison with nvlink's output path

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| Serialization engine sub_45BF00 (13,258 bytes, 532 lines) | HIGH | Decompiled file sub_45BF00_0x45bf00.c exists |
| Polymorphic writer sub_45B6D0 (5 modes) | HIGH | Verified in sub_45B6D0 decompiled code: switch with cases 0-4, exact mode behavior confirmed |
| Mode 3 = FILE* (fwrite), mode 4 = memcpy with advancing cursor | HIGH | Verified in sub_45B6D0: case 3 calls fwrite, case 4 calls memcpy and advances +32 |
| Mode 2 = vector append (sub_44FC10) | HIGH | Verified in sub_45B6D0: case 2 calls sub_44FC10; decompiled file exists |
| Mode 0 = callback via offset +8, mode 1 = no-op | HIGH | Verified in sub_45B6D0: case 0 calls through +8 pointer, case 1 falls through to return |
| NULL writer defaults to stdout (fwrite) | HIGH | Verified in sub_45B6D0: first `if (!a1)` returns `fwrite(a2, 1, a3, stdout)` |
| File-mode factory sub_45B950 | HIGH | Decompiled file sub_45B950_0x45b950.c exists |
| Memory-mode factory sub_45BA30 | HIGH | Decompiled file sub_45BA30_0x45ba30.c exists |
| Writer destructor sub_45B6A0 | HIGH | Decompiled file sub_45B6A0_0x45b6a0.c exists |
| Writer context = 40 bytes | MEDIUM | Inferred from arena allocation size; consistent across factory functions |
| File entry sub_45C920, memory entry sub_45C950 | HIGH | Both decompiled files exist |
| Size computation sub_45C980 | HIGH | Decompiled file exists; 128/224 constants verified |
| "writing file" error string | HIGH | String at 0x1D3B828 confirmed in nvlink_strings.json |
| "Negative size encountered" error string | HIGH | String at 0x1D3B84C confirmed in nvlink_strings.json, xref to sub_45BF00 |
| "section size mismatch" error string | HIGH | String at 0x1D3B835 confirmed in nvlink_strings.json, xref to sub_45BF00 |
| 10-phase write sequence | HIGH | Phase structure verified against sub_45BF00 decompiled code flow |
| Phase 1: ELF header (64/52 bytes) | HIGH | Header size selection by elf_class verified in decompiled code |
| Phase 2: single NUL padding byte | HIGH | Single zero-byte write visible in decompiled code after header |
| Phase 3-4: shstrtab then strtab string tables | HIGH | Loop over string arrays at +336/+312 and +328/+304 verified |
| Phase 5: alignment padding to section[3] offset | HIGH | Gap computation from running offset to section 3's sh_offset verified |
| Phase 7: fragment list traversal at sec+72 | MEDIUM | Fragment list structure inferred from pointer arithmetic; node layout reconstructed |
| Phase 9: section headers (64/40 bytes each) | HIGH | shdr_size selection verified; loop over section_order array confirmed |
| Phase 10: program header table (sub_45BAA0) | HIGH | Conditional call to sub_45BAA0 verified in sub_45BF00 |
| Phase 10 entry 0 = PT\_PHDR(6), PF\_R\|PF\_X(5) | HIGH | sub_45BAA0 line 139: `v52 = 0x500000006LL`; LODWORD=6=PT\_PHDR, HIDWORD=5=PF\_R\|PF\_X |
| Phase 10 last entry = PT\_LOAD(1), PF\_R\|PF\_X(5) | HIGH | sub_45BAA0 line 170: `v53[v32-1] = 0x500000001LL` |
| Phase 10 p\_offset = e\_phoff (not e\_shoff) | HIGH | sub_45BAA0 line 137: `v53[0]=v25` where `v25=*(a2+32)=e_phoff`; sub_45BF00 line 258 sets `*(a2+32)=*(a2+40)+shnum*shentsize` |
| Phase 10 strtab segment flags = PF\_R\|PF\_X(5) | HIGH | sub_45BAA0 line 146: `0x500000001LL` |
| Phase 10 shstrtab segment flags = PF\_R\|PF\_W(6) | HIGH | sub_45BAA0 line 162: `0x600000001LL` |
| e\_phoff = e\_shoff + shnum \* shentsize | HIGH | sub_45BF00 line 258 (ELF64), line 252 (ELF32) |
| e\_phnum preamble computation (2/3/4 entries) | HIGH | sub_45BF00 lines 239-244: conditional on strtab\_base and shstrtab\_base |
| ELF32 v126=1 always (no flag suppression) | HIGH | sub_45BF00 line 178: `v126 = 1` reached for all ELF32 ET\_EXEC paths |
| NOBITS bitmask 0x400D in Phase 7b | HIGH | Same bitmask appears in sub_45BAA0 and serialization engine |
| Canonical section ordering (0=null, 1=shstrtab, 2=strtab, 3=symtab) | HIGH | Verified from section creation order in sub_4438F0 constructor |
| e_shnum overflow encoding (0 -> sh_size of section 0) | MEDIUM | Standard ELF mechanism; code path exists in sub_45C980 but rarely exercised |
