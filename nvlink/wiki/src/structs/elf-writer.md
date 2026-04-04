# ELF Writer (elfw)

The ELF writer -- internally called `elfw` -- is nvlink's central data structure for building device ELF binaries. Every cubin the linker produces is constructed inside a 672-byte `elfw` object that contains the raw ELF header bytes, section and symbol management tables, string tables, program header lists, architecture state, and a battery of boolean flags derived from the merge-flags bitmask. The constructor `sub_4438F0` (14,821 bytes at `0x4438F0`) allocates this object, initializes the ELF header in place, creates the mandatory sections (`.shstrtab`, `.strtab`, `.symtab`, `.symtab_shndx`), and wires up the internal data structures. When it is time to emit the final binary, a separate 40-byte polymorphic writer context routes all bytes through one of five backends -- callback, no-op, growable vector, `fwrite` to `FILE*`, or `memcpy` to a pre-allocated buffer.

This page documents the 672-byte elfw struct layout and the 40-byte writer context at reimplementation depth. For the serialization logic that walks the elfw and emits bytes through the writer, see [ELF Serialization](../elf/serialization.md). For the ELF format semantics (header fields, section types, program headers), see [Device ELF Format](../elf/device-elf-format.md).

## Key Facts

| Property | Value |
|---|---|
| Struct size | 672 bytes (84 qwords) |
| Allocation | `sub_4307C0` (arena allocator) from the elfw's own memory arena |
| Constructor | `sub_4438F0` (`elfw_create`) at `0x4438F0` -- 14,821 bytes, 600 lines |
| Destructor | `sub_4475B0` (`elfw_destroy`) at `0x4475B0` -- 3,023 bytes, 133 lines |
| Debug dump | `sub_4478F0` (`elfw_dump_structure`) at `0x4478F0` -- 15,098 bytes, 569 lines |
| Section copy | `sub_4411F0` (`elfw_copy_section`) at `0x4411F0` -- 12,184 bytes, 482 lines |
| Add section | `sub_440740` at `0x440740` -- 5,410 bytes |
| Add section+data | `sub_440BE0` at `0x440BE0` -- 7,034 bytes |
| Add reloc section | `sub_441AC0` at `0x441AC0` -- 9,523 bytes |
| Add symbol | `sub_442CA0` at `0x442CA0` -- 7,159 bytes |
| Merge symbols | `sub_442820` at `0x442820` -- 5,371 bytes |
| Writer context size | 40 bytes |
| Polymorphic writer | `sub_45B6D0` at `0x45B6D0` -- 5 modes |
| File-mode factory | `sub_45B950` at `0x45B950` -- creates mode 3 writer |
| Memory-mode factory | `sub_45BA30` at `0x45BA30` -- creates mode 4 writer |
| Writer destructor | `sub_45B6A0` at `0x45B6A0` |

## The 672-Byte elfw Struct

The elfw object begins with raw ELF header bytes at offset 0 (the first 52 or 64 bytes _are_ the `Elf32_Ehdr` / `Elf64_Ehdr`), followed by metadata fields, boolean flags, and pointers to sub-structures. The constructor zeroes the entire 672 bytes via `memset`, then populates fields based on its ten parameters.

### Constructor Parameters

```c
elfw_t* elfw_create(
    uint16_t  elf_type,        // a1: ET_REL(1), ET_EXEC(2), 0xFF00 (Mercury relocatable)
    bool      is_64bit,        // a2: 0 = ELF32, nonzero = ELF64
    uint8_t   abi_version,     // a3: EI_ABIVERSION value
    uint8_t   sm_major,        // a4: SM major version (e.g., 89 for Ada)
    uint8_t   sm_minor,        // a5: SM minor version (e.g., 0x45 = 69)
    bool      debug_flag,      // a6: generate debug sections
    int       api_version,     // a7: CUDA API version
    bool      verbose_flag,    // a8: verbose output
    uint32_t  merge_flags,     // a9: bitmask controlling many behaviors
    bool      mercury_flag     // a10: Mercury-mode indicator
);
```

The merge_flags bitmask (`a9`) is the primary behavioral control. Individual bits are unpacked into boolean fields throughout the struct. When bit `0x400` is set, the constructor creates a dedicated `"elfw memory space"` arena (via `sub_432020`) and stores it at offsets +608/+616; otherwise, the global arena is used.

### Field Layout

The layout below is derived from the decompiled constructor (`sub_4438F0`), destructor (`sub_4475B0`), serializer (`sub_45BF00`), and debug dump function (`sub_4478F0`). Offsets are in bytes; `qword[N]` notation references the decompiled `v17[N]` 8-byte array indexing.

#### ELF Header Region (offsets 0--63)

The first bytes overlay the standard ELF header. The constructor writes the magic number and identification bytes directly:

```
+0    uint32_t  e_ident[0..3]      = 0x464C457F  (ELF magic: 7F 45 4C 46)
+4    uint8_t   EI_CLASS           = (is_64bit != 0) + 1  (1=ELF32, 2=ELF64)
+5    uint8_t   EI_DATA            = 1  (ELFDATA2LSB, little-endian)
+6    uint8_t   EI_VERSION         = 1  (EV_CURRENT)
+7    uint8_t   EI_OSABI           = 0x41 (device 64-bit) or 0x33 (device 32-bit)
+8    uint8_t   EI_ABIVERSION      = abi_version parameter
```

The remaining header fields (`e_type`, `e_machine`, `e_version`, `e_entry`, `e_phoff`, `e_shoff`, `e_flags`, `e_ehsize`, `e_phentsize`, `e_phnum`, `e_shentsize`, `e_shnum`, `e_shstrndx`) follow the standard ELF32/ELF64 layout. The constructor sets `e_machine = 190` (`EM_CUDA`), `e_type` from the `elf_type` parameter, and packs `sm_major`/`sm_minor` into `e_flags` using encoding that depends on the OSABI.

For the full header encoding, see [Device ELF Format -- ELF Identification](../elf/device-elf-format.md).

#### Metadata and Flags (offsets 64--103)

| Offset | Size | Field | Set From | Description |
|--------|------|-------|----------|-------------|
| +64 | 1 | `verbose` | `a8` | Verbose output flag |
| +68 | 4 | `e_flags_copy` | `a9` | Cached `e_flags` (merge_flags bits 16-18 extracted) |
| +72 | 4 | `sm_version` | `a4` | SM major version, also at `elfw+18` as `uint32` |
| +76 | 4 | `flags_raw` | `a9` | Full merge_flags value |
| +80 | 1 | `debug_flag` | `a6` | Whether debug sections should be emitted |
| +83 | 1 | `has_section_names` | computed | Nonzero when `elfw+84` (shstrtab) has entries |
| +84 | 1 | `preserve_relocs` | `a9 & 1` | `--preserve-relocs` flag |
| +85 | 1 | `force_rela` | `(a9 & 2) != 0` | `--force-rela` flag |
| +86 | 1 | `allow_undef_globals` | `(a9 & 0x200) != 0` | `--allow-undefined-globals` |
| +87 | 1 | `no_opt` | `(a9 & 4) != 0` | `--no-opt` flag |
| +88 | 1 | `optimize_data` | `(a9 & 8) != 0` | `--optimize-data-layout` |
| +89 | 1 | `mercury_or_bit4` | `(a9 >> 4) & 1 \|\| mercury` | Mercury mode or specific flag |
| +90 | 1 | `emit_ptx` | `(a9 & 0x20) != 0` | `--emit-ptx` flag |
| +91 | 1 | `flag_0x4000` | `(a9 & 0x4000) != 0` | Reserved flag |
| +92 | 1 | `flag_0x40` | `(a9 & 0x40) != 0` | Reserved flag |
| +93 | 1 | `flag_0x100` | `BYTE1(a9) & 1` | Bits 8 flag |
| +94 | 1 | `extended_smem` | `(a5 > 0x45) & ((a9 >> 7) & 1)` | Extended shared memory: sm_minor > 69 AND bit 7 |
| +96 | 1 | `flag_0x800` | `(a9 & 0x800) != 0` | Reserved flag |
| +99 | 1 | `no_debug_info` | `((a9 >> 12) ^ 1) & 1` | Inverted bit 12: suppress debug info |
| +100 | 1 | `flag_0x2000` | `(a9 & 0x2000) != 0` | Reserved flag |
| +101 | 1 | `is_device_elf` | `(a9 & 0x8000) != 0` | Whether this is a device ELF (sets OSABI 0x41) |

#### Dynamic Array Regions (offsets 108--172)

The constructor initializes two dynamic arrays at offsets `+108` and `+140` using `sub_43E490`:

| Offset | Init Size | Description |
|--------|-----------|-------------|
| +108 | 1000 elements | Section header array (for device ELF only) |
| +140 | 2000 elements | Extended section array (for device ELF only) |

These are only allocated when `is_device_elf` is true (OSABI 0x41 path). The section header array is later populated as sections are added. The sizes 1000 and 2000 are initial capacities.

#### String Table Pointers (offsets 216--228)

| Offset | qword | Description |
|--------|-------|-------------|
| +216 | `v17[27]` | Reserved / link pointer, set to 0 |
| +228 | -- | Reserved, set to 0 |

#### Hash Tables (offsets 288--303)

| Offset | qword | Description |
|--------|-------|-------------|
| +288 | `v17[36]` | Section name hash table (lookup by name string) |
| +296 | `v17[37]` | Duplicate section name hash table |

Both are created via `sub_4489C0` with hash/compare functions `sub_44E000` / `sub_44E180` and an initial capacity of 512 buckets. They provide O(1) section lookup by name during the merge phase.

#### Section/Symbol Counters and Pointers (offsets 304--376)

| Offset | qword | Type | Description |
|--------|-------|------|-------------|
| +304 | -- | `uint32` | `.strtab` entry count |
| +308 | -- | `uint32` | Section-level counter |
| +312 | -- | `uint32` | `.shstrtab` entry count |
| +316 | -- | -- | Alignment padding |
| +320 | `v17[40]` | `uint32` | Segment counter, initialized to 1 (`v17[39+1]`) |
| +328 | -- | `ptr` | `.strtab` string pointer array |
| +336 | -- | `ptr` | `.shstrtab` string pointer array |
| +344 | `v17[43]` | `ptr` | Section data list (ordered list for sections with data fragments) |
| +352 | `v17[44]` | `ptr` | Section header list (ordered list of section records) |
| +360 | `v17[45]` | `ptr` | Master section ordered list |
| +368 | -- | `ptr` | Section order index array (int32 indices into `+360`) |

Three ordered lists at +344, +352, and +360 are created via `sub_464AE0` with element sizes of 64, 64, and 64 bytes respectively. The constructor creates a 104-byte null section record (section index 0, `SHN_UNDEF`) and appends it to `+360`, and a 48-byte null entry appended to both `+344` and `+352`.

#### Symbol Management (offsets 376--464)

| Offset | qword | Description |
|--------|-------|-------------|
| +376 | `v17[47]` | Symbol table list (part 1) |
| +384 | `v17[48]` | Symbol table list (part 2) |
| +392 | `v17[49]` | Symbol table list (part 3) |
| +408 | `v17[51]` | Relocation list (created with element size 32) |
| +416 | -- | `uint32` at `v17[104]>>32 bits` -- relocation counter |
| +488 | `v17[61]` | Architecture-specific relocation vtable (from `sub_45AC50` or `sub_459640`) |

The architecture vtable is a 632-byte function pointer table created by `sub_45AC50` (Mercury targets, when `mercury_flag` is true) or `sub_459640` (non-Mercury targets). If neither returns a valid vtable, the constructor calls `fatal_error("couldn't initialize arch state")`.

#### Arch State and Named Section Indices (offsets 488--512)

| Offset | Description |
|--------|-------------|
| +488 (`v17[61]`) | Arch relocation vtable pointer |
| +496 (`v17[62]`) | Well-known section name hash table (populated from `off_1D3A9C0` string list) |
| +504 | `.shstrtab` section index (uint16 at `v17+31*2 = +62`) |
| +506 | `.strtab` section index (uint16 at `v17+101*2 = +202`) |
| +508 | `.symtab` section index (uint16) |
| +510 | `.symtab_shndx` section index (uint16) |
| +512 | `.note.nv.cuinfo` section index (uint16, device ELF only) |
| +514 | `.note.nv.tkinfo` section index (uint16, device ELF only) |

These indices are written during construction after the initial sections are created. They enable fast O(1) access to the mandatory sections without hash table lookups.

#### Ordered Lists for ELF Segments (offsets 520--560)

| Offset | qword | Description |
|--------|-------|-------------|
| +520 | `v17[65]` | Ordered list (ptr-keyed, 16-byte elements) |
| +528 | `v17[66]` | Ordered list (ptr-keyed, 16-byte elements) |
| +536 | `v17[67]` | Ordered list (ptr-keyed, 16-byte elements) |
| +544 | `v17[68]` | Ordered list (ptr-keyed, 16-byte elements) |
| +552 | `v17[69]` | Ordered list (ptr-keyed, 16-byte elements) |
| +560 | `v17[70]` | Ordered list (ptr-keyed, 16-byte elements) |

Six ordered lists created via `sub_465020` with hash/compare functions and element size 16. These manage ELF segment assignments -- the six lists correspond to different segment categories (text, data, rodata, bss, etc.) used during the layout phase.

#### Hash Table for Section Resolution (offsets 576--584)

| Offset | qword | Description |
|--------|-------|-------------|
| +576 | `v17[72]` | Section resolution hash table (created via `sub_4489C0` with `sub_44E120`/`sub_44E130` comparators, 8 buckets) |

#### Merge State (offsets 592--624)

| Offset | qword | Description |
|--------|-------|-------------|
| +592 | `v17[74]` | Section remap table (NULL until merge phase) |
| +600 | `v17[75]` | Symbol remap table (NULL until merge phase) |
| +608 | `v17[76]` | Private memory arena (when `merge_flags & 0x400`) |
| +616 | `v17[77]` | Private arena metadata |

When bit `0x400` of merge_flags is set, the constructor creates a dedicated `"elfw memory space"` arena with 4096-byte pages via `sub_432020`. This arena is stored at +608/+616 and used exclusively for this elfw instance. The destructor checks offset +608 and destroys this private arena if present; otherwise it tears down the sub-structures individually.

#### Construction Finalization (offsets 624--672)

| Offset | Description |
|--------|-------------|
| +624 | `v17[156>>1]` -- arch class value from `sub_42F8B0()` (returns constant 5) |
| +664 | `v17[83]` -- end marker, set to 0 |

The constructor ends by calling `sub_4504B0(elfw, 0)` which performs additional section setup (populating the well-known section name hash table from the static string list at `off_1D3A9C0`).

### Input File Record (offset +512)

A 16-byte input file record is created and appended to the list at `v17[64]`:

```c
struct input_file_record {
    const char*  filename;       // +0: "<input>" placeholder
    uint32_t     sm_minor;       // +8: sm_minor parameter
    uint32_t     flags;          // +12: initialized to 0
};
```

This tracks the input files that contributed to this elfw. During the merge phase, each merged cubin adds an entry to this list.

## The 40-Byte Writer Context

The polymorphic writer is a small strategy-pattern object that decouples the serialization engine from the output destination. All serialized bytes flow through `sub_45B6D0`, which dispatches on a mode field at offset 0 of the 40-byte context.

### Struct Layout

```
struct elf_writer {              // 40 bytes
    int32_t   mode;              // +0:  backend selector (0..4)
    int32_t   flags;             // +4:  always 0 in observed paths
    void*     callback_or_state; // +8:  function pointer (mode 0) or reserved
    void*     rewind_fn;         // +16: function pointer for stream rewind
    void*     cleanup_fn;        // +24: destructor called by sub_45B6A0
    void*     dest;              // +32: target -- FILE*, buffer ptr, vector, or callback context
};
```

### Five Dispatch Modes

The central dispatch function `sub_45B6D0` handles a NULL writer pointer as a special case (writes to stdout), then switches on the mode field:

```c
int64_t elf_write(elf_writer* w, void* data, size_t len) {
    if (w == NULL)
        return fwrite(data, 1, len, stdout);

    switch (w->mode) {
    case 0:  // Callback
        return w->callback_or_state(w->dest, data, len);
    case 1:  // No-op (size counting)
        return len;
    case 2:  // Growable vector
        vector_append(w->dest, data, len);   // sub_44FC10
        return len;
    case 3:  // FILE* via fwrite
        if (w->dest)
            return fwrite(data, 1, len, w->dest);
        // NULL dest: byte-by-byte putc to stdout
        for (size_t i = 0; i < len; i++)
            _IO_putc(((uint8_t*)data)[i], stdout);
        return len;
    case 4:  // memcpy with advancing cursor
        memcpy(w->dest, data, len);
        w->dest += len;
        return len;
    default:
        return -1;
    }
}
```

| Mode | Name | dest field | Use case |
|------|------|-----------|----------|
| 0 | Callback | Opaque context for user function at `+8` | Extensibility hook |
| 1 | No-op | Unused | Dry-run size computation |
| 2 | Vector | Growable chunk-list header | Intermediate buffering |
| 3 | FILE\* | `FILE*` from `fopen` | `sub_45C920` -- final output to disk |
| 4 | Memcpy | Buffer pointer, advances on each write | `sub_45C950` -- Mercury in-memory output |

### Factory Functions

**sub_45B950 -- File-Mode Factory (Mode 3)**

Allocates 40 bytes from the elfw's arena, sets `mode = 3`, stores a pointer to libc `rewind()` at offset +16 (for potential stream rewinding), and places the `FILE*` at offset +32. The cleanup function at +24 is NULL because `main()` manages the file descriptor lifetime.

```c
elf_writer* create_file_writer(FILE* file, elfw_t* elfw) {
    elf_writer* w = arena_alloc(get_arena(file, elfw), 40);
    w->mode      = 3;
    w->flags     = 0;
    w->rewind_fn = &rewind;    // libc rewind()
    w->cleanup_fn = NULL;
    w->dest      = file;
    return w;
}
```

**sub_45BA30 -- Memory-Mode Factory (Mode 4)**

Allocates 40 bytes, sets `mode = 4`, and stores the buffer pointer at offset +32. Both `rewind_fn` and `cleanup_fn` are NULL. The `dest` pointer advances during serialization: each `memcpy` call advances it by `len` bytes, so after serialization completes, `dest` points past the end of the buffer.

```c
elf_writer* create_memory_writer(void* buffer, elfw_t* elfw) {
    elf_writer* w = arena_alloc(get_arena(buffer, elfw), 40);
    w->mode      = 4;
    w->flags     = 0;
    w->rewind_fn = NULL;
    w->cleanup_fn = NULL;
    w->dest      = buffer;
    return w;
}
```

Both factories take two parameters whose first is the destination and second is the elfw. They call `sub_44F410` to retrieve the arena metadata pointer from the elfw (at qword offset +3, i.e., byte offset +24 of the arena header), then allocate the 40 bytes via `sub_4307C0`.

### Writer Cleanup: sub_45B6A0

```c
void destroy_writer(elf_writer* w, void* unused) {
    if (w) {
        if (w->cleanup_fn)       // offset +24
            w->cleanup_fn(w->dest);
        arena_free(w, unused);   // sub_431000
    }
}
```

Checks offset +24 for a cleanup function. In both observed paths (modes 3 and 4), `cleanup_fn` is NULL, so only the arena deallocation runs.

## Mode 2: Vector-Backed Writer

Mode 2 uses `sub_44FC10` (vector_append) to write into a growable arena-backed chunk list. This mode is used for intermediate buffering when the final output size is not known in advance.

The vector is a 40-byte header at `dest`:

```
struct vec_header {
    int64_t   default_chunk_size;  // +0:  minimum allocation for new chunks
    int64_t   total_written;       // +8:  cumulative bytes appended
    void*     tail_next_ptr;       // +16: pointer to tail node's next field
    void*     chain_end;           // +24: end-of-chain pointer
    chunk_t*  current_chunk;       // +32: active chunk being filled
};
```

Each chunk is a 24-byte header:

```
struct chunk_t {
    int64_t   capacity;     // +0:  total bytes this chunk can hold
    int64_t   remaining;    // +8:  bytes still available
    void*     data;         // +16: pointer to the data buffer
};
```

When a write exceeds the current chunk's remaining capacity, the function fills the current chunk with as many bytes as possible, then allocates a new chunk (sized to the larger of `default_chunk_size` and the remaining write size), copies the rest, and links the new chunk into the chain.

## How the Writer is Used in the Serialization Pipeline

The serialization pipeline follows a strict three-step pattern at both entry points:

```
1. Create writer  -->  sub_45B950 (file) or sub_45BA30 (memory)
2. Serialize ELF  -->  sub_45BF00 (13,258 bytes -- walks the entire elfw)
3. Destroy writer -->  sub_45B6A0
```

**File output (sub_45C920)**: Called by `main()` for non-Mercury targets. The `FILE*` is opened by `main()` with `fopen(output_path, "wb")` before calling this function.

**Memory output (sub_45C950)**: Called by `main()` for Mercury targets (sm >= 100). The buffer is pre-allocated to the exact size computed by `sub_45C980` (size computation using mode 1 dry-run logic). After serialization, the buffer holds the complete ELF image ready for FNLZR post-link transformation.

The serialization engine `sub_45BF00` writes the ELF in a strict sequential order through the polymorphic writer:

1. ELF header (52 or 64 bytes -- the raw bytes from the elfw struct)
2. Null padding byte
3. `.shstrtab` contents (section name strings)
4. `.strtab` contents (symbol name strings)
5. Alignment padding to `.symtab` offset
6. Program headers (compact internal format)
7. Section data (sections 4..N-1 with fragment-list traversal)
8. Post-section padding to `e_shoff`
9. Section header table (40 or 64 bytes per entry)
10. ELF program header table (conditional, via `sub_45BAA0`)

Every single write operation checks the return value against the expected byte count. Any mismatch triggers `sub_467460` with `"writing file"` -- a fatal error that terminates the linker.

## Destructor: sub_4475B0

The destructor handles two cases based on whether the elfw has a private memory arena (offset +608):

**Private arena path** (offset +608 is non-NULL): Releases the arena metadata via `sub_45CAE0`, then destroys the entire arena with `sub_431C70`. This single operation frees all memory allocated from that arena, including all section records, symbol data, and the elfw struct itself.

**Shared arena path** (offset +608 is NULL): Individually tears down every sub-structure:

1. Frees remap arrays at offsets +472, +464, +456 (`v17[59]`, `v17[58]`, `v17[57]`)
2. Walks and destroys both section hash tables at +296, +288 (via `sub_448C00` with callback `sub_440080`, then `sub_448A40`)
3. Frees data at offsets +336, +328 (`v17[42]`, `v17[41]`)
4. Destroys six ordered lists at +520..+560 (`v17[65]`..`v17[70]`) via `sub_466E00`
5. Destroys symbol lists at +376, +384, +392 (`v17[47]`, `v17[48]`, `v17[49]`)
6. Walks section data list at +344 (`v17[43]`), freeing each section's fragment chain
7. Walks section header list at +352 (`v17[44]`), freeing section records from index 1 onward
8. Destroys remap tables at +592, +600 (`v17[74]`, `v17[75]`) if non-NULL
9. Walks input file list at +512 (`v17[64]`), freeing each record
10. Walks master section list at +360 (`v17[45]`), freeing fragment lists and section data
11. Frees linked-list chain at +480 (`v17[60]`), walking `next` pointers
12. Destroys section name hash at +496 (`v17[62]`) and resolution hash at +576 (`v17[72]`)
13. Frees additional lists and the arch vtable at +488 (`v17[61]`)
14. Finally frees the 672-byte elfw struct itself via `sub_431000`

The private-arena path is far simpler (two calls) because the arena deallocator bulk-frees everything. The shared-arena path runs approximately 30 individual deallocation calls.

## Merge-Flags Bitmask Reference

The `merge_flags` parameter (`a9`) is a 32-bit bitmask that controls the elfw's behavior. Each bit is unpacked into a boolean field during construction:

| Bit | Hex | Flag Name | Offset | Description |
|-----|-----|-----------|--------|-------------|
| 0 | `0x1` | `preserve_relocs` | +84 | `--preserve-relocs` |
| 1 | `0x2` | `force_rela` | +85 | `--force-rela` |
| 2 | `0x4` | `no_opt` | +87 | `--no-opt` |
| 3 | `0x8` | `optimize_data` | +88 | `--optimize-data-layout` |
| 4 | `0x10` | `flag_bit4` | +89 | Or'd with mercury_flag |
| 5 | `0x20` | `emit_ptx` | +90 | `--emit-ptx` |
| 6 | `0x40` | `flag_bit6` | +92 | Reserved |
| 7 | `0x80` | `extended_smem_gate` | +94 | Gate for extended shared memory (combined with sm_minor > 0x45) |
| 8 | `0x100` | `flag_bit8` | +93 | Reserved |
| 9 | `0x200` | `allow_undef` | +86 | `--allow-undefined-globals` |
| 10 | `0x400` | `private_arena` | -- | Creates dedicated "elfw memory space" arena |
| 11 | `0x800` | `flag_bit11` | +96 | Reserved |
| 12 | `0x1000` | `no_debug_inverted` | +99 | Inverted: `((flags >> 12) ^ 1) & 1` |
| 13 | `0x2000` | `flag_bit13` | +100 | Reserved |
| 14 | `0x4000` | `flag_bit14` | +91 | Reserved |
| 15 | `0x8000` | `is_device_elf` | +101 | Selects OSABI 0x41 path, enables CUDA-specific sections |
| 17-18 | `0x60000` | `segment_flags` | +68 | Bits `a9 & 0x70000` stored at offset +68 |
| 19 | `0x80000` | `mercury_reloc` | -- | Set when mercury_flag or `a9 & 0x180000`; forces relocatable ELF type |

When `mercury_flag` is true or bits 19-20 are set, the constructor forces `e_type` to a relocatable variant and sets `mercury_reloc` in the flags.

## Function Reference

| Address | Name | Size | Role |
|---|---|---|---|
| `0x4438F0` | `elfw_create` | 14,821 bytes | Constructor -- allocates 672-byte struct, initializes all fields |
| `0x4475B0` | `elfw_destroy` | 3,023 bytes | Destructor -- tears down all sub-structures |
| `0x4478F0` | `elfw_dump_structure` | 15,098 bytes | Debug dump of entire elfw state |
| `0x4411F0` | `elfw_copy_section` | 12,184 bytes | Deep-copies a section from one elfw to another |
| `0x440740` | `elfw_add_section` | 5,410 bytes | Adds a new empty section |
| `0x440BE0` | `elfw_add_section_with_data` | 7,034 bytes | Adds a section with initial data buffer |
| `0x441AC0` | `elfw_add_reloc_section` | 9,523 bytes | Creates .rel/.rela section linked to parent |
| `0x442CA0` | `elfw_add_symbol` | 7,159 bytes | Adds a symbol to the symbol table |
| `0x442820` | `elfw_merge_symbols` | 5,371 bytes | Merges symbols including UFT stubs |
| `0x448E70` | `elfw_section_table_build` | 14,646 bytes | Builds/rebuilds section header table |
| `0x45B6D0` | `elf_write` | ~256 bytes | Polymorphic 5-mode write dispatcher |
| `0x45B950` | `create_file_writer` | ~112 bytes | Factory: mode 3 writer (FILE\*) |
| `0x45BA30` | `create_memory_writer` | ~112 bytes | Factory: mode 4 writer (memcpy) |
| `0x45B6A0` | `destroy_writer` | ~48 bytes | Calls cleanup + arena free |
| `0x45BF00` | `serialize_elf` | 13,258 bytes | Core serialization engine |
| `0x45C920` | `write_elf_to_file` | ~48 bytes | Entry: file output path |
| `0x45C950` | `write_elf_to_memory` | ~48 bytes | Entry: memory output path |
| `0x45C980` | `compute_elf_size` | ~176 bytes | Returns total serialized byte count |
| `0x45BAA0` | `write_program_headers` | 5,657 bytes | Constructs ELF Phdr table |
| `0x44FC10` | `vector_append` | ~256 bytes | Growable vector write (mode 2 backend) |
| `0x45AC50` | `mercury_arch_vtable` | 7,496 bytes | Creates Mercury relocation vtable |
| `0x459640` | `arch_vtable_create` | 16,109 bytes | Creates non-Mercury relocation vtable |
