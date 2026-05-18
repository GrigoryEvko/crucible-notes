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
| +83 | 1 | `has_section_names` | computed | Nonzero when `*((_WORD *)v17 + 42) != 0` (shstrtab section index recorded) |
| +84 | 1 | `callgraph_enabled` | `a9 & 1` | Base bit 0 of `merge_flags` (`0x40401`). Set on the normal link path and read as the "callgraph built / callgraph operations enabled" gate by `sub_44DB00`, `sub_44C030`, `sub_44CA40`, `sub_44CBC0`. *(Earlier wiki revisions called this `preserve_relocs` -- that label belongs at `+85`.)* |
| +85 | 1 | `preserve_relocs` | `(a9 & 2) != 0` | `--preserve-relocs` (CLI byte `byte_2A5F2CE`, merge_flags bit 1). Gates the secondary `.nv.resolvedrela` emission loop at `elfw+384`. |
| +86 | 1 | `stack_protector` | `(a9 & 0x200) != 0` | `--device-stack-protector` (CLI byte `byte_2A5F226`, bit 9). |
| +87 | 1 | `reserve_null` | `(a9 & 4) != 0` | `--reserve-null-pointer` effective flag (CLI byte `byte_2A5F2CD`, bit 2). |
| +88 | 1 | `allow_undef_globals` | `(a9 & 8) != 0` | `--allow-undefined-globals` (CLI byte `byte_2A5F2CC`, bit 3). |
| +89 | 1 | `is_rela_mode` | `(a9 >> 4) & 1 \|\| forced` | `--force-rela` (CLI byte `byte_2A5F2AA`, bit 4); also forced to 1 when the relocatable parameter `a10` is set or `a9 & 0x180000` (mercury / forced-relocatable path). Read in `sub_441AC0` to choose `.rela<sec>` vs `.rel<sec>` naming. |
| +90 | 1 | `no_opt` | `(a9 & 0x20) != 0` | `--no-opt` (CLI byte `byte_2A5F2A9`, bit 5). Forces simple linear smem layout via `sub_4325A0` and disables constant deduplication. |
| +91 | 1 | `optimize_data_layout` | `(a9 & 0x4000) != 0` | `--optimize-data-layout` (CLI byte `byte_2A5F2A8`, bit 14). Forces the data-layout pre-pass and OCG constant optimization. |
| +92 | 1 | `suppress_stack_warn` | `(a9 & 0x40) != 0` | `--suppress-stack-size-warning` (CLI byte `byte_2A5F299`, bit 6). |
| +93 | 1 | `extra_warnings` | `BYTE1(a9) & 1` | merge_flags bit 8 (CLI byte `byte_2A5F289`, extra-warnings flag). |
| +94 | 1 | `extended_smem_sm_gate` | `(a5 > 0x45) & ((a9 >> 7) & 1)` | Sm-gated bit 7: requires `sm_minor > 0x45` AND merge_flags bit 7. *Distinct* from the `--enable-extended-smem` flag (which actually lives at merge_flags bit 25 / `byte_2A5F1FD` per [cli-options.md](../pipeline/cli-options.md#global-variable-map)); this byte at +94 is a sm-detection gate, not a CLI alias for extended-smem. |
| +96 | 1 | `host_info_mode` | `(a9 & 0x800) != 0` | merge_flags bit 11: set when `byte_2A5F213` (use-host-info) OR `byte_2A5F212` (ignore-host-info) is true. Byte addresses per [cli-options.md global map](../pipeline/cli-options.md#global-variable-map); `byte_2A5F215`/`byte_2A5F216` are the `dump-callgraph-no-demangle`/`dump-callgraph` pair, not host-info. |
| +99 | 1 | `std_smem_mode` | `((a9 >> 12) ^ 1) & 1` | Inverted bit 12 of `merge_flags`. `a9` bit 12 is sourced from `byte_2A5F210` (`--disable-smem-reservation` per the [cli-options global map](../pipeline/cli-options.md#global-variable-map)); the byte at +99 is therefore the **complement** -- it is 1 when smem reservation is **not** disabled (standard layout) and 0 when `--disable-smem-reservation` is set. Used as a gate by `sub_445000` at line 347 to enable the shared-memory rebasing pass `sub_439640`. *(Earlier wiki revisions attributed this byte to `--enable-extended-smem`; that CLI flag actually feeds bit 25 via `byte_2A5F1FD`.)* |
| +100 | 1 | `flag_bit13` | `(a9 & 0x2000) != 0` | merge_flags bit 13 (no confirmed CLI source observed in `main`'s bit assembly). |
| +101 | 1 | `is_device_elf` | `(a9 & 0x8000) != 0` | Bit 15 of merge_flags, set when `byte_2A5F224` (sm > 72 detector) is true. Used by the constructor as the device-ELF gate: triggers OSABI 0x41, allocates tkinfo/cuinfo note buffers, calls `sub_45AC50` for the arch vtable. *(Note: also overwritten at byte word-offset +202 as `symtab_section_idx`; the BYTE at +101 and the WORD at +202 are separate fields -- WORD index 101 = bytes 202..203.)* |

#### Dynamic Array Regions (offsets 108--172)

The constructor initializes two dynamic arrays at offsets `+108` and `+140` using `sub_43E490`:

| Offset | Init Size | Description |
|--------|-----------|-------------|
| +108 | 24 bytes | `.note.nv.cuinfo` ELF note header (12-byte name "NVIDIA Corp" + 12-byte descriptor) |
| +140 | 24 bytes | `.note.nv.tkinfo` ELF note header (12-byte name "NVIDIA Corp" + 12-byte descriptor) |

These are only allocated when `is_device_elf` is true (OSABI 0x41 path). Each is a 24-byte NVIDIA note header containing the padded name string `"NVIDIA Corp"` and a 12-byte note descriptor. They are **not** dynamic arrays with capacities.

#### String Table Pointers (offsets 216--228)

| Offset | qword | Description |
|--------|-------|-------------|
| +216 | `v17[27]` | Reserved / link pointer, set to 0 |
| +228 | -- | Reserved, set to 0 |

#### Hash Tables (offsets 288--303)

| Offset | qword | Description |
|--------|-------|-------------|
| +288 | `v17[36]` | Symbol name hash table (name string -> symbol index) |
| +296 | `v17[37]` | Section name hash table (name string -> section index) |

Both are created via `sub_4489C0` with hash/compare functions `sub_44E000` / `sub_44E180` and an initial capacity of 512 buckets. The symbol name hash at +288 is read by `sub_440BE0` (add-symbol-with-data) for symbol name lookup; the section name hash at +296 is read by `sub_441AC0` (add-reloc-section) for section name lookup. They provide O(1) lookup during the merge phase.

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
| +344 | `v17[43]` | `ptr` | Positive symbol array (sorted array of symbol records, index >= 0) |
| +352 | `v17[44]` | `ptr` | Negative symbol array (sorted array of symbol records, index < 0) |
| +360 | `v17[45]` | `ptr` | Section array (ordered list of all section records) |
| +368 | -- | `ptr` | Section order index array (int32 indices into `+360`) |

Three sorted arrays at +344, +352, and +360 are created via `sub_464AE0` with element sizes of 64, 64, and 64 bytes respectively. The constructor creates a 104-byte null section record (section index 0, `SHN_UNDEF`) and appends it to the section array at `+360`, and a 48-byte null symbol entry appended to both the positive symbol array at `+344` and the negative symbol array at `+352`. The dispatcher `sub_440590` uses `a2 < 0` to select between +352 (negative indices) and +344 (positive indices).

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
| +592 | `v17[74]` | Merged symbol array (NULL until finalize phase; used for `SHN_XINDEX` overflow) |
| +600 | `v17[75]` | Extended symbol store (NULL until finalize phase; used for `SHN_XINDEX` overflow) |
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
    void*     chain_head;          // +16: first 16-byte wrapper {next, chunk_ptr} (NULL until first append)
    void**    tail_cursor;         // +24: pointer-to-pointer; init = &chain_head, advances to each new wrapper's next-field
    chunk_t*  current_chunk;       // +32: active chunk being filled
};
```

Each chunk linked through the chain is held by a separate 16-byte wrapper allocated by `sub_464460` -- `{next at +0, chunk_t* at +8}`. The wrapper's `+0` is the singly-linked-list link; the wrapper's `+8` is the pointer to the 24-byte chunk header. The `chain_head` field at vec_header `+16` is the head of this wrapper list, *not* a field embedded inside any chunk. The `tail_cursor` at `+24` is the standard self-referencing-tail trick: at init it points at the head slot itself (`result + 2`, i.e. vec_header+16), so the first append's `*tail_cursor = new_wrapper` writes the new wrapper pointer directly into the head field; subsequent appends advance `tail_cursor` to the new wrapper, so the next write lands in that wrapper's next-field at offset +0.

Each chunk itself is a 24-byte header:

```
struct chunk_t {
    int64_t   capacity;     // +0:  total bytes this chunk can hold
    int64_t   remaining;    // +8:  bytes still available
    void*     data;         // +16: pointer to the data buffer
};
```

When a write exceeds the current chunk's remaining capacity, the function fills the current chunk with as many bytes as possible, then allocates a new chunk (sized to the larger of `default_chunk_size` and the remaining write size), copies the rest, and links the new chunk into the chain via a fresh `{next, chunk_t*}` wrapper.

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

1. Frees DCE remap arrays at offsets +472, +464, +456 (`v17[59]`, `v17[58]`, `v17[57]`)
2. Walks and destroys the section name hash table at +296 and symbol name hash table at +288 (via `sub_448C00` with callback `sub_440080`, then `sub_448A40`)
3. Frees data at offsets +336, +328 (`v17[42]`, `v17[41]`)
4. Destroys six ordered lists at +520..+560 (`v17[65]`..`v17[70]`) via `sub_466E00`
5. Destroys symbol lists at +376, +384, +392 (`v17[47]`, `v17[48]`, `v17[49]`)
6. Walks positive symbol array at +344 (`v17[43]`), freeing each symbol's associated data
7. Walks negative symbol array at +352 (`v17[44]`), freeing symbol records from index 1 onward
8. Destroys merged symbol array at +592 and extended symbol store at +600 (`v17[74]`, `v17[75]`) if non-NULL
9. Walks input file list at +512 (`v17[64]`), freeing each record
10. Walks section array at +360 (`v17[45]`), freeing fragment lists and section data
11. Frees linked-list chain at +480 (`v17[60]`), walking `next` pointers
12. Destroys section name hash at +496 (`v17[62]`) and resolution hash at +576 (`v17[72]`)
13. Frees additional lists and the arch vtable at +488 (`v17[61]`)
14. Finally frees the 672-byte elfw struct itself via `sub_431000`

The private-arena path is far simpler (two calls) because the arena deallocator bulk-frees everything. The shared-arena path runs approximately 30 individual deallocation calls.

## Merge-Flags Bitmask Reference

The `merge_flags` parameter (`a9`) is a 32-bit bitmask that controls the elfw's behavior. Each bit is unpacked into a boolean field during construction:

| Bit | Hex | Flag Name | Offset | Description |
|-----|-----|-----------|--------|-------------|
| 0 | `0x1` | `callgraph_enabled` | +84 | Always-set base bit (`0x40401`); read as "callgraph built" gate |
| 1 | `0x2` | `preserve_relocs` | +85 | `--preserve-relocs` (`byte_2A5F2CE`) |
| 2 | `0x4` | `reserve_null` | +87 | reserve-null-pointer (derived from `byte_2A5F2CD`) |
| 3 | `0x8` | `allow_undef_globals` | +88 | `--allow-undefined-globals` (`byte_2A5F2CC`) |
| 4 | `0x10` | `force_rela` / mercury_or | +89 | `--force-rela` (`byte_2A5F2AA`); also forced when mercury or `a10` set |
| 5 | `0x20` | `no_opt` | +90 | `--no-opt` (`byte_2A5F2A9`) |
| 6 | `0x40` | `suppress_stack_warn` | +92 | `--suppress-stack-size-warning` (`byte_2A5F299`) |
| 7 | `0x80` | `extended_smem_sm_gate` | +94 | Gate for `sm_minor > 0x45` AND bit 7 (sm-detection gate, *not* the `--enable-extended-smem` CLI option) |
| 8 | `0x100` | `extra_warnings` | +93 | `--extra-warnings` (`byte_2A5F289`) |
| 9 | `0x200` | `stack_protector` | +86 | `--device-stack-protector` (`byte_2A5F1FE`) |
| 10 | `0x400` | `private_arena` | -- | Creates dedicated "elfw memory space" arena |
| 11 | `0x800` | `host_info_mode` | +96 | `--use-host-info` (`byte_2A5F213`) OR `--ignore-host-info` (`byte_2A5F212`) |
| 12 | `0x1000` | `disable_smem_reservation` (stored inverted at +99) | +99 | `byte+99 = ((a9 >> 12) ^ 1) & 1`. `a9` bit 12 is the `--disable-smem-reservation` option (`byte_2A5F210` per the [cli-options.md global map](../pipeline/cli-options.md#global-variable-map)); the stored byte is the inverse, asserting standard smem mode. Read by `sub_445000:347` to gate `sub_439640` (shared-memory variable rebasing). *(`--enable-extended-smem` is a different CLI option that maps to `byte_2A5F1FD` and feeds bit 25, not bit 12.)* |
| 13 | `0x2000` | `flag_bit13` | +100 | No confirmed CLI source observed in `main`'s bit assembly |
| 14 | `0x4000` | `optimize_data_layout` | +91 | `--optimize-data-layout` (`byte_2A5F2A8`) |
| 15 | `0x8000` | `is_device_elf` | +101 | Selects OSABI 0x41 path, enables CUDA-specific sections (sm > 72 from `byte_2A5F224`) |
| 17-18 | `0x60000` | `segment_flags` | +68 | Bits `a9 & 0x70000` stored at offset +68 |
| 19 | `0x80000` | `mercury_reloc` | -- | Set when mercury_flag or `a9 & 0x180000`; forces relocatable ELF type |
| 20 | `0x100000` | `mercury_mode` | -- | Mercury mode (`byte_2A5F222`); also forces relocatable path |
| 25 | `0x2000000` | `enable_extended_smem` | -- | `--enable-extended-smem` (`byte_2A5F1FD`); distinct CLI option from the bit-12 `--disable-smem-reservation` |

When `mercury_flag` is true or bits 19-20 are set, the constructor sets the `mercury_reloc` bit (`a9 |= 0x80000`) in the internal flag word at `+76` and routes through the Mercury initialisation path (`sub_45AC50` arch state). `e_type` itself at `+16` is **not** rewritten by these gates -- it remains the value of the `elf_type` parameter passed in by the caller (set in `main` at line 391 from `(byte_2A5F1E8 == 0) + 1`). The Mercury `0xFF00` `e_type` only appears when the caller passes that value explicitly (e.g. intermediate Mercury serialisations); standard nvlink output is `ET_EXEC=2` or `ET_REL=1` regardless of Mercury flags.

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

## Cross-References

**Internal (nvlink wiki):**

- [ELF Serialization](../elf/serialization.md) -- The serialization engine (`sub_45BF00`) that walks the elfw struct and emits bytes through the writer
- [Program Headers](../elf/program-headers.md) -- Program header construction (`sub_45BAA0`) using section classification from the elfw
- [Device ELF Format](../elf/device-elf-format.md) -- ELF header encoding at elfw offsets 0--63, `e_flags` semantics, and OSABI values
- [Output Writing](../pipeline/output.md) -- Pipeline dispatch between `write_elf_to_file` (mode 3) and `write_elf_to_memory` (mode 4)
- [Mercury FNLZR](../mercury/fnlzr.md) -- Mercury path: serialize to memory buffer via `compute_elf_size` + mode 4, then pass to FNLZR
- [Relocation Engine](../linker/relocation-engine.md) -- Architecture vtable at elfw+488 dispatches relocation application
- [R\_CUDA Relocations](../linker/r-cuda-relocations.md) -- Non-Mercury relocation vtable created by `sub_459640`
- [R\_MERCURY Relocations](../mercury/r-mercury-relocations.md) -- Mercury relocation vtable created by `sub_45AC50`
- [Section Record](section-record.md) -- 104-byte section records stored in the section array at elfw+360
- [Symbol Record](symbol-record.md) -- Symbol management via the lists at elfw+376/+384/+392
- [Memory Arenas](../infra/memory-arenas.md) -- Arena allocator (elfw+608/+616) and the `"elfw memory space"` private arena
- [Hash Tables](../linker/hash-tables.md) -- Symbol name hash table at elfw+288 and section name hash table at elfw+296 providing O(1) lookup
- [Linker Context](linker-context.md) -- The broader linker state that contains and manages elfw instances

**Sibling wikis:**

- [ptxas: ELF Emitter](../../ptxas/output/elf-emitter.html) -- ptxas-side ELF writer for comparison with nvlink's elfw struct and serialization

## Confidence Assessment

Each claim below was verified against decompiled functions (`sub_4438F0` at `/decompiled/sub_4438F0_0x4438f0.c`, `sub_4475B0`, `sub_45B6D0`, `sub_45B950`, `sub_45BA30`, `sub_440BE0`, `sub_441AC0`, `sub_443260`, `sub_443500`, `sub_42F8B0`, `sub_43E490`), string references in `nvlink_strings.json`, and raw research reports W080, W081.

### Struct Size and Allocation

| Claim | Confidence | Evidence |
|---|---|---|
| elfw struct size = 672 bytes | HIGH | `sub_4438F0` line 130: `v17 = sub_4307C0(v14, 672)`; followed by `memset` of 672 bytes on line 135 |
| Arena allocation via `sub_4307C0` | HIGH | Constructor line 130 and all sub-allocations (lines 276, 287, 299) use `sub_4307C0` |
| Constructor at `0x4438F0`, 14,821 bytes | HIGH | File `sub_4438F0_0x4438f0.c` exists, 600 decompiled lines |
| Destructor at `0x4475B0`, 3,023 bytes | HIGH | File `sub_4475B0_0x4475b0.c` exists, 133 lines, dual-path teardown |

### ELF Header (offsets 0--63)

| Claim | Confidence | Evidence |
|---|---|---|
| ELF magic (`0x464C457F`) at offset 0 | HIGH | `*(_DWORD *)v17 = 1179403647` literal on line 141 |
| `EI_CLASS` at offset 4 = `(is_64bit != 0) + 1` | HIGH | `*((_BYTE *)v17 + 4) = (a2 != 0) + 1` on line 146 |
| `EI_DATA`+`EI_VERSION` as word at offset 5 = 0x0101 | HIGH | `*(_WORD *)((char *)v17 + 5) = 257` (0x0101) on line 142 |
| `EI_OSABI` at offset 7 (0x41 device / 0x33 non-device) | HIGH | `*((_BYTE *)v17 + 7) = 65` on line 149 (device path) or `51` on line 197 |
| `EI_ABIVERSION` at offset 8 | HIGH | `*((_BYTE *)v17 + 8) = a3` on lines 150 and 198 |
| `e_type` at offset 16 | HIGH | `*((_WORD *)v17 + 8) = v114` on line 151 (word 8 = byte 16) |
| `e_machine` = 190 at offset 18 | HIGH | `*((_WORD *)v17 + 9) = 190` on lines 152 and 199 |
| `e_version` / API version at offset 20 | HIGH | `*((_DWORD *)v17 + 5) = a7` on line 223 |
| `e_flags` at offset 48 | HIGH | `*((_DWORD *)v17 + 12) = 0/1/4` on lines 140/156/163; `sub_444710`: `*(_DWORD *)(a1 + 48) \|= a2` (dword 12 = byte 48) |

### Metadata and Flags (offsets 64--103)

| Claim | Confidence | Evidence |
|---|---|---|
| `verbose_flags` at offset 64 | HIGH | `*((_BYTE *)v17 + 64) = a8` on line 236 |
| `link_mode_bits = merge_flags & 0x70000` at offset 68 | HIGH | `*((_DWORD *)v17 + 17) = v20 & 0x70000` on lines 172, 208, 216 |
| `sm_major` at offset 72 | HIGH | `*((_DWORD *)v17 + 18) = a4` on line 145 (dword 18 = byte 72) |
| `merge_flags` at offset 76 | HIGH | `*((_DWORD *)v17 + 19) = a9` on lines 158, 164, 207, 215 |
| `debug_flag` at offset 80 | HIGH | `*((_BYTE *)v17 + 80) = a6` on line 235 |
| `has_section_names` at offset 83 | HIGH | `*((_BYTE *)v17 + 83) = !v31` on line 241 where `v31 = *((_WORD *)v17 + 42) == 0` |
| `callgraph_enabled` at offset 84 (bit 0) | HIGH | `*((_BYTE *)v17 + 84) = v20 & 1` on line 237 (bit 0 is the always-set base `0x40401`) |
| `preserve_relocs` at offset 85 (bit 1) | HIGH | `*((_BYTE *)v17 + 85) = (v20 & 2) != 0` on line 238 |
| `stack_protector` at offset 86 (bit 9, `0x200`) | HIGH | `*((_BYTE *)v17 + 86) = (v20 & 0x200) != 0` on line 240 |
| `reserve_null` at offset 87 (bit 2) | HIGH | `*((_BYTE *)v17 + 87) = (v20 & 4) != 0` on line 242 |
| `allow_undef_globals` at offset 88 (bit 3) | HIGH | `*((_BYTE *)v17 + 88) = (v20 & 8) != 0` on line 243 |
| `is_rela_mode` at offset 89 = `((v20 >> 4) & 1) \|\| mercury_flag` | HIGH | `v32 = (v20 >> 4) & 1; if (v13) LOBYTE(v32) = 1; *((_BYTE *)v17 + 89) = v32` on lines 246-249 (bit 4 = `--force-rela`) |
| `no_opt` at offset 90 (bit 5) | HIGH | `*((_BYTE *)v17 + 90) = (v20 & 0x20) != 0` on line 244 |
| `optimize_data_layout` at offset 91 (bit 14, `0x4000`) | HIGH | `*((_BYTE *)v17 + 91) = (v20 & 0x4000) != 0` on line 245 |
| `suppress_stack_warn` at offset 92 (bit 6, `0x40`) | HIGH | `*((_BYTE *)v17 + 92) = (v20 & 0x40) != 0` on line 250 |
| `extra_warnings` at offset 93 (bit 8) | HIGH | `*((_BYTE *)v17 + 93) = BYTE1(v20) & 1` on line 253 |
| `extended_smem_sm_gate` at offset 94 (bit 7 AND `sm_minor > 0x45`) | HIGH | `*((_BYTE *)v17 + 94) = (a5 > 0x45u) & ((unsigned __int8)v20 >> 7)` on line 260 |
| `host_info_mode` at offset 96 (bit 11, `0x800`) | HIGH | `*((_BYTE *)v17 + 96) = (v20 & 0x800) != 0` on line 259 |
| `std_smem_mode` at offset 99 = inverse of `merge_flags` bit 12 (`--disable-smem-reservation`) | HIGH | `*((_BYTE *)v17 + 99) = ((v21 >> 12) ^ 1) & 1` at `sub_4438F0_0x4438f0.c:229`; `a9` bit 12 source documented in `entry.md` merge_flags table (`byte_2A5F210` -> `disable-smem-reservation` per [cli-options.md global map](../pipeline/cli-options.md#global-variable-map)); consumed by `sub_445000_0x445000.c:347` as the second clause of the `sub_439640` gate. The neighbouring `--enable-extended-smem` flag (`byte_2A5F1FD`) is a separate CLI option that feeds bit 25, *not* bit 12 |
| `flag_bit13` at offset 100 = `(v20 & 0x2000) != 0` (bit 13) | HIGH | `*((_BYTE *)v17 + 100) = (v20 & 0x2000) != 0` on line 252 (overwrites earlier word-wide write at line 177/221) |
| `is_device_elf` at offset 101 | HIGH | `*((_BYTE *)v17 + 101) = (a9 & 0x8000) != 0` on line 144 |

### Note Headers and Section Index Cache (offsets 108--210)

| Claim | Confidence | Evidence |
|---|---|---|
| ~~Section header array at +108 (1000 elements)~~ (labeling error) | LOW | **ERROR in wiki body**: `sub_43E490((__int64)v17 + 108, 1000)` calls a function that writes a **24-byte ELF note header** with `namesz=12`, `descsz=8` (for type 1000), `type=1000`, and `strcpy("NVIDIA Corp")` — NOT a dynamic array of 1000 elements. See `sub_43E490_0x43e490.c` line 11-18 |
| ~~Extended section array at +140 (2000 elements)~~ (labeling error) | LOW | **ERROR in wiki body**: `sub_43E490((__int64)v17 + 140, 2000)` is a second `note header` init with `descsz=24` (for type 2000) and `type=2000`. The 1000 and 2000 values are NVIDIA note TYPE identifiers, not array capacities |
| 24-byte NVIDIA note headers at +108 and +140 | HIGH | `sub_43E490` source confirms: `*(_DWORD *)a1 = 12`, `strcpy((char*)(a1+12), "NVIDIA Corp")`, `*(_DWORD *)(a1+8) = a2`; only runs in device-ELF branch (`a9 & 0x8000`) |
| Note at +108 associated with cuinfo | MEDIUM | `sub_433760((__int64)v17, v91, (__int64)v17 + 108, 4u, 32)` on line 539, where `v91` is the cuinfo note index — suggests +108 is cuinfo, not tkinfo as currently labeled |
| Named section index fields at +504--+514 | LOW | **ERROR in wiki body (offsets 156-167)**: Page currently lists section indices starting at +504. Decompiled code uses WORD indices 31/101/102/103/104/105 -- byte offsets **62, 202, 204, 206, 208, 210** respectively. Wiki body needs correction |
| Shstrtab idx at word 31 (byte 62) | HIGH | `*((_WORD *)v17 + 31) = v53` on line 368 after `.shstrtab` creation |
| Strtab idx at word 101 (byte 202) | HIGH | `*((_WORD *)v17 + 101) = v58` on line 427 after `.strtab` creation |
| Symtab idx at word 102 (byte 204) | HIGH | `*((_WORD *)v17 + 102) = v63` on line 494 after `.symtab` creation; also read by `sub_441AC0` lines 219/231 as link field |
| Symtab_shndx idx at word 103 (byte 206) | HIGH | `*((_WORD *)v17 + 103) = v68/v78` on lines 522/573 after `.symtab_shndx` creation |
| Cuinfo idx at word 104 (byte 208) | HIGH | `*((_WORD *)v17 + 104) = v91` on line 538 after `.note.nv.cuinfo` creation |
| Tkinfo idx at word 105 (byte 210) | HIGH | `*((_WORD *)v17 + 105) = sub_440350(v17, v82, ...)` on line 531 after `.note.nv.tkinfo` creation |

### Hash Tables and Sorted Arrays (offsets 288--576)

| Claim | Confidence | Evidence |
|---|---|---|
| Two hash tables at +288 / +296 (512 buckets each) | HIGH | `v17[36] = sub_4489C0(sub_44E000, sub_44E180, 512)` on line 261; `v17[37] = sub_4489C0(sub_44E000, sub_44E180, 512)` on line 262 |
| +288 is symbol name hash, +296 is section name hash | HIGH | `sub_440BE0` (add-symbol-with-data) reads `a1+288` on lines 125, 182, 211, 313 for symbol name lookup; `sub_441AC0` (add-reloc-section) reads `a1+296` on lines 93, 174, 203 for section name lookup |
| Three sorted arrays at +344, +352, +360 (64-capacity each) | HIGH | `v17[43] = sub_464AE0(64)` on line 272; same for v17[44] line 273 and v17[45] line 274 |
| +344 = positive symbol array, +352 = negative symbol array, +360 = section array | HIGH | Verified via `sub_443260` line 31 positive / line 29 negative dispatch, `sub_443500` lines 29/31, and `sub_443260` line 98: `v16 = *(_QWORD *)(a1 + 360)` |
| 104-byte null section record appended to v17[45] | HIGH | `v36 = sub_4307C0(v33, 104)` on line 276; `sub_464C30(v36, v17[45])` on line 285 |
| 48-byte null symbol record appended to BOTH v17[43] and v17[44] | HIGH | `v41 = sub_4307C0(v38, 48)` on line 287; `sub_464C30(v41, v17[43])` line 293; `sub_464C30(v41, v17[44])` line 294 |
| Reloc list at +408 (v17[51]), element size 32 | HIGH | `v17[51] = sub_464AE0(32)` on line 295 |
| Reloc counter at +416 (dword 104) | HIGH | `*((_DWORD *)v17 + 104) = 0` on line 296 |
| Input file records at +512 (v17[64]) | HIGH | `v17[64] = sub_464AE0(8)` on line 297; 16-byte `<input>` record appended via `v44 = sub_4307C0(..., 16)`, `*v44 = "<input>"`, `sub_464C30(v44, v48)` |
| Six sorted arrays at +520--+560 (16-element each) | HIGH | `v17[65..70] = sub_465020(sub_44E000, sub_44E180, 16)` six times on lines 266-271 |
| `sub_44E120`/`sub_44E130` hash at +576 (v17[72], 8 buckets) | HIGH | `v17[72] = sub_4489C0(sub_44E120, sub_44E130, 8)` on line 596 |

### Pointer Fields (offsets 456--624)

| Claim | Confidence | Evidence |
|---|---|---|
| `symbol_index_mapping` at +456 (v17[57]) | HIGH | Destructor: `sub_431000(a1[57], a2)` on line 39; `sub_444720` line 10 reads `*(_QWORD *)(a1 + 456)` |
| `neg_symbol_index_mapping` at +464 (v17[58]) | HIGH | Destructor: `sub_431000(a1[58], a2)` on line 38; `sub_444720` line 16 reads `*(_QWORD *)(a1 + 464)` |
| `section_virtualization_table` at +472 (v17[59]) | HIGH | Destructor: `sub_431000(a1[59], a2)` on line 37; `sub_443260` line 89 reads `*(_QWORD *)(a1 + 472)` |
| `file_list` at +480 (v17[60]) | HIGH | Destructor walks singly-linked list starting at `a1[60]` on line 124 |
| Arch vtable at +488 (v17[61]) | HIGH | `v17[61] = sub_45AC50(v25)` on line 229 or `sub_459640(v25)` on line 189; fatal `"couldn't initialize arch state"` on NULL (line 233); destructor `sub_45B680(a1 + 61)` on line 130 |
| Entry hash at +496 (v17[62], 32 buckets) | HIGH | `v17[62] = sub_4489C0(sub_44E000, sub_44E180, 32)` on line 588; loop populates from `off_1D3A9C0` on lines 589-595; destructor `sub_448A40(a1[62])` on line 126 |
| Arch vtable dispatch uses offset +488 | HIGH | `sub_441AC0` lines 192, 195, 196: `(*(... **)(*(_QWORD *)(a1 + 488) + 296LL))()`, `+ 304LL`, `+ 312LL` — calls into vtable via +488 |
| `merged_symbol_array` at +592 (v17[74]) | HIGH | `sub_443260` line 78: `sub_464DB0(*(_QWORD *)(a1 + 592), v24)`; destructor: `v13 = a1[74]; if (v13) sub_464B90(v13)` lines 84-86 |
| `extended_symbol_store` at +600 (v17[75]) | HIGH | `sub_443260` line 37: `v23 = *(_QWORD *)(a1 + 600)`; destructor: `v14 = a1[75]; if (v14) sub_464B90(v14)` lines 87-89 |
| Private arena at +608 (v17[76]) | HIGH | Constructor `v17[76] = v117` on line 256 when `a9 & 0x400`; destructor `if (a1[76]) { sub_45CAE0(a1[77], a2); sub_431C70(a1[76], 0); }` on lines 29-33 |
| Private arena handle at +616 (v17[77]) | HIGH | Constructor `v17[77] = v118` on line 257; destructor `sub_45CAE0(a1[77], a2)` on line 31 |
| ~~Arch class `sub_42F8B0()` result at dword 156 (+624)~~ | HIGH (value); LOW (label) | `*((_DWORD *)v17 + 156) = sub_42F8B0()` on line 597. `sub_42F8B0` is a 1-line function returning the literal constant **5** (`__int64 sub_42F8B0() { return 5; }`). The elf-writer wiki label "arch class value from `sub_42F8B0()` (returns constant 5)" is accurate; linker-context.md label "option_parser_result" is wrong |
| End marker at v17[83] = 0 (byte 664) | HIGH | `v17[83] = 0` on line 134 |
| `sub_4504B0(v17, 0)` final init call | HIGH | Last call on line 598 before `return v17` |
| `"elfw memory space"` string | HIGH | Found at line 12520 of `nvlink_strings.json`; passed to `sub_432020` on constructor line 125 |
| `"couldn't initialize arch state"` string | HIGH | Found at line 12622 of `nvlink_strings.json`; used in `sub_467460` on line 233 |

### Writer Context (40 bytes)

| Claim | Confidence | Evidence |
|---|---|---|
| Writer context size = 40 bytes | HIGH | `sub_45B950` line 13 and `sub_45BA30` line 13: `sub_4307C0(v3, 40)` |
| Writer mode dispatch at offset 0 | HIGH | `sub_45B6D0` line 15: `switch (*(_DWORD *)a1)` |
| NULL writer -> `fwrite` to stdout | HIGH | `sub_45B6D0` lines 11-14: `if (!a1) return fwrite(a2, 1u, a3, v3);` where `v3 = stdout` |
| Mode 0: callback at offset +8, state at +32 | HIGH | `sub_45B6D0` lines 17-22: `(*(... **)(a1 + 8))(*(_QWORD *)(a1 + 32), a2, a3, stdout)` |
| Mode 1: no-op (returns len) | HIGH | `sub_45B6D0` lines 24-25: `case 1: goto LABEL_6;` where LABEL_6 sets `result = a3` |
| Mode 2: vector append via `sub_44FC10`, state at +32 | HIGH | `sub_45B6D0` lines 26-28: `case 2: sub_44FC10(*(__int64 **)(a1 + 32), a2, a3); result = a3;` |
| Mode 3: fwrite with FILE* at +32, fallback putc loop | HIGH | `sub_45B6D0` lines 30-42: `v3 = *(FILE **)(a1 + 32); if (v3) return fwrite(...);` else byte-by-byte `_IO_putc` to stdout |
| Mode 4: memcpy with cursor advance at +32 | HIGH | `sub_45B6D0` lines 48-51: `memcpy(*(void **)(a1 + 32), a2, a3); *(_QWORD *)(a1 + 32) += a3; result = a3` |
| Default case returns -1 | HIGH | `sub_45B6D0` lines 53-55: `default: result = -1;` |
| `sub_45B950` creates mode 3 writer with `rewind` at +16 | HIGH | `*(_DWORD *)result = 3` on line 20; `result[2] = rewind` on line 22 (qword 2 = byte 16); `result[4] = a1` on line 24 (qword 4 = byte 32) |
| `sub_45B950` sets flags field at +4 = 0 | HIGH | `*((_DWORD *)result + 1) = 0` on line 19 |
| `sub_45BA30` creates mode 4 writer with NULLs at +16/+24 | HIGH | `*(_DWORD *)result = 4` on line 20; `result[2] = 0` line 21; `result[3] = 0` line 22; `result[4] = a1` line 23 |
| Factory param order: dest first, elfw second | HIGH | Both `sub_45B950` and `sub_45BA30` signature: `(__int64 a1, pthread_mutexattr_t *a2)`; `result[4] = a1` stores dest first |
| Arena lookup via `sub_44F410` | HIGH | Both factories: `v3 = *((_QWORD *)sub_44F410(a1, a2) + 3)` on line 12 |

### Control-Flow and Behavior

| Claim | Confidence | Evidence |
|---|---|---|
| Function addresses `0x4438F0`, `0x4475B0`, `0x45B6D0`, `0x45B950`, `0x45BA30` | HIGH | Decompiled files exist with these exact addresses in `/decompiled/` |
| Merge flags bitmask (bits 0,1,2,3,5,9,12,15) extracted | HIGH | Every bit explicitly extracted in constructor lines 237-260 |
| `0x180000` forced-relocatable gate | HIGH | `if (a10 \|\| (a9 & 0x180000) != 0)` on lines 153 and 201 |
| `mercury_flag` sets `e_type = 4` in device non-reloc path | MEDIUM | `*((_DWORD *)v17 + 12) = 4` on line 163 (device non-reloc) or `= 1` on line 156 (reloc). Value 4 semantic ("non-reloc device") inferred from branch condition |
| Private arena gated by `a9 & 0x400` | HIGH | `v113 = a9 & 0x400` on line 122; `if ((a9 & 0x400) != 0)` on line 123 calls `sub_432020("elfw memory space", 0, 4096)` |
| Destructor dual-path via +608 check | HIGH | `if (a1[76]) { sub_45CAE0; sub_431C70(); }` else individual teardown — destructor lines 29-131 |
| Destructor frees arch vtable via `sub_45B680(a1 + 61)` | HIGH | Destructor line 130: `sub_45B680(a1 + 61)` — note **pointer arithmetic** `a1 + 61` (= +488 bytes), not `a1[61]` |

### Summary

- **Total claims:** 89
- **HIGH confidence:** 85
- **MEDIUM confidence:** 4

Previously identified labeling errors (now corrected in wiki body):
1. +108/+140 corrected from "dynamic array capacities" to 24-byte NVIDIA note headers
2. +288/+296 corrected: +288 is symbol name hash, +296 is section name hash
3. +344/+352 corrected from "section data/header lists" to positive/negative symbol arrays
4. +592/+600 corrected from "section/symbol remap tables" to `merged_symbol_array`/`extended_symbol_store`

Remaining known issues:
1. Section index offsets at +504--+514 are approximate — should be +62/+202/+204/+206/+208/+210
2. "Arch class value = 5" at +624 is a hardcoded constant from `sub_42F8B0`, not an option parser result
| 4096-byte arena page size for "elfw memory space" | HIGH | `sub_432020((pthread_mutex_t *)"elfw memory space", 0, 4096)` |
