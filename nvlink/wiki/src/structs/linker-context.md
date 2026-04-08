# Linker Context Object

The linker context is a 672-byte arena-allocated structure (`elfw` -- ELF wrapper) that serves as the single mutable state object threaded through every phase of nvlink's pipeline. Created by `sub_4438F0` (`elfw_create`) during Phase 5 and destroyed by `sub_4475B0` (`elfw_destroy`) at the end of `main()`, this object holds the ELF header fields, all section and symbol storage arrays, hash tables for name-based lookup, architecture-specific vtable pointers, relocation lists, index mapping tables, and configuration flags decoded from the merge-flags bitmask. Every pipeline function -- merge, layout, relocation, finalization, serialization -- receives this structure as its first argument (`a1`).

This page documents the internal layout of the 672-byte structure at reimplementation depth, the allocation and initialization sequence, the destruction sequence, and how the context flows through the linker pipeline.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_4438F0` | `elfw_create` | 14,821 B | Allocates 672-byte struct, initializes ELF header, creates hash tables, sorted arrays, core sections |
| `sub_4475B0` | `elfw_destroy` | 3,023 B | Destroys all sub-structures, frees arrays, hash tables, and the struct itself |
| `sub_443260` | `elfw_get_section` | ~640 B | Resolves a signed section index to a section data record pointer (core accessor) |
| `sub_443500` | `elfw_is_callee` | ~560 B | Checks whether a symbol is a cross-function callee |
| `sub_443730` | `elfw_setup_phdrs` | ~560 B | Initializes program header name strings at offset 192 |
| `sub_444690` | `elfw_reset_arch_state` | ~480 B | Resets arch vtable at offset 488 and clears flag state |
| `sub_444710` | `elfw_set_flags` | 16 B | OR-merges bits into `e_flags` at offset 48 |
| `sub_444720` | `elfw_remap_symbol` | ~416 B | Remaps a signed symbol index through the mapping table at offsets 456/464 |
| `sub_4447B0` | `elfw_is_syscall` | ~288 B | Checks if a symbol name is a CUDA syscall (offset 496 hash table or static table) |
| `sub_444830` | `elfw_is_syscall_32f` | 16 B | Exact match for `__cuda_syscall_32f3056bbb` |
| `sub_444840` | `elfw_is_cnp_syscall` | ~288 B | Checks syscall status and `cnp` prefix |
| `sub_447570` | `elfw_layout_relocate_finalize` | ~64 B | Orchestrator: calls layout, relocation, finalization in sequence |
| `sub_4478F0` | `elfw_dump_structure` | 15,098 B | Debug dump of entire context (all sections, symbols, relocations) |
| `sub_4504B0` | `elfw_init_callgraph` | ~large | Initializes callgraph tracking structures |

## Structure Layout (672 bytes)

The structure combines three functional regions: an embedded ELF header template (bytes 0--101), configuration/buffer space (bytes 102--287), and the linker's mutable state (bytes 288--671). All offsets are from the base of the 672-byte allocation, expressed as byte offsets. Array element notations like `ctx[36]` refer to 8-byte QWORD indexing (`ctx + 36 * 8 = ctx + 288`).

### Region 1: ELF Header Template (offsets 0--101)

These fields are written directly into the output ELF during serialization.

```
Offset  Size  QWORD  Field                   Description
------  ----  -----  -----                   -----------
  0      4    [0]    elf_magic               0x464C457F ("\x7fELF")
  4      1           ei_class                1=Elf32, 2=Elf64 (computed as (is_64bit != 0) + 1)
  5      1           ei_data                 1 (ELFDATA2LSB, little-endian)
  6      1           ei_version              1 (EV_CURRENT)
  7      1           ei_osabi                0x41 (ELFOSABI_CUDA_DEVICE) or 0x33 (32-bit)
  8      1           ei_abiversion           SM ABI version (a3 parameter)
 16      2    [2]    e_type                  1=ET_REL, 2=ET_EXEC, 0xFF00=Mercury relocatable
 18      2           e_machine               190 (0xBE = EM_CUDA), always
 20      4           e_version_or_api        API version (device) or 1
 48      4    [6]    e_flags                 Architecture flags (encoding depends on OSABI)
 62      2           shstrtab_section_idx    Index of .shstrtab section
 64      1    [8]    verbose_flags           Verbose output control byte
 68      4           link_mode_bits          merge_flags & 0x70000
 72      4    [9]    sm_arch_major           SM major version (e.g., 100 for Blackwell)
 76      4           merge_flags             Full merge-flags bitmask (a9 | 0x80000 if relocatable)
 80      1   [10]    debug_flag              Debug info generation flag (a6 parameter)
 82      1           section_virtualization  Non-zero if section virtualization is active (set by merge)
 83      1           has_shstrtab            Non-zero if shstrtab section index was stored
```

### Region 2: Configuration Flags and Buffers (offsets 84--287)

Individual boolean flags are decomposed from `merge_flags` during construction. These single-byte flags govern behavior throughout the pipeline without requiring repeated bitmask checks.

```
Offset  Size  Field                   merge_flags bit    Description
------  ----  -----                   ---------------    -----------
 84      1    preserve_relocs         bit 0              --preserve-relocs
 85      1    force_rela              bit 1              --force-rela
 86      1    reserve_null            bit 9              --reserve-null-pointer
 87      1    disable_smem_res        bit 2              --disable-smem-reservation
 88      1    allow_undef_globals     bit 3              --allow-undefined-globals
 89      1    is_relocatable          bit 4 (or forced)  Relocatable link mode
 90      1    optimize_data           bit 5              --optimize-data-layout
 91      1    syscall_const_off       bit 14             --syscall-const-offset
 92      1    no_opt                  bit 6              --no-opt
 93      1    extra_flag              bit 8              (reserved)
 94      1    extended_smem           (conditional)      sm_minor > 0x45 && bit 7
 96      1    dump_callgraph          bit 11             --dump-callgraph
 99      1    no_warn_dead_code       ~bit 12            !(bit 12)
100      1    verbose_keep            bit 13             --verbose-keep
101      1    is_device_elf           (a9 & 0x8000)      Device ELF mode (vs host)
```

Tkinfo and cuinfo buffers are initialized by `sub_43E490`:

```
Offset  Size  Field              Initial capacity
------  ----  -----              ----------------
108      32   tkinfo_buffer      1,000 bytes (for .note.nv.tkinfo content)
140      32   cuinfo_buffer      2,000 bytes (for .note.nv.cuinfo content)
```

Program header name buffer (set by `sub_443730`):

```
Offset  Size  Field              Description
------  ----  -----              -----------
192      8    phdr_string_buf    Pointer to growable string buffer
168      4    phdr_offset_0      Offset for PHDR name 0
172      4    phdr_offset_1      Offset for PHDR name 1
176      4    phdr_offset_2      Offset for PHDR name 2
180      4    phdr_offset_3      Offset for PHDR name 3
184      4    phdr_offset_4      Offset for PHDR name 4
```

Section index tracking for core ELF sections:

```
Offset  Size  Field                Description
------  ----  -----                -----------
200      2    tkinfo_section_idx   .note.nv.tkinfo section index
202      2    symtab_section_idx   .symtab section index
204      2    symtab_shndx_idx     .symtab_shndx section index
206      2    strtab_section_idx   .strtab section index (at word offset 103)
208      2    cuinfo_section_idx   .note.nv.cuinfo section index
210      2    cuinfo_note_idx      Secondary cuinfo note index
228      8    reloc_tracking        Relocation processing state (zeroed during init)
```

### Region 3: Linker Mutable State (offsets 288--671)

This is the core of the linker context. All section, symbol, and relocation data is accessed through these fields.

```
Offset  Bytes  QWORD   Field                        Type / Description
------  -----  ------  -----                        ------------------
288      8     [36]    symbol_name_hash             LinkerHash* -- hash table for symbol lookup by name (512 buckets)
296      8     [37]    section_name_hash            LinkerHash* -- hash table for section lookup by name (512 buckets)
304      4     [38]    shstrtab_entry_count         Number of entries in shstrtab
312      8     [39]    initial_value                Set to 0x100000000 (count=1, flags=0)
320      4     [40]    strtab_entry_count           Set to 1 initially
344      8     [43]    pos_symbol_array             SortedArray* -- positive-index symbols (64-element initial)
352      8     [44]    neg_symbol_array             SortedArray* -- negative-index symbols (64-element initial)
360      8     [45]    section_array                SortedArray* -- section data records (64-element initial)
368      8     [46]    section_order_map            Pointer -- maps virtual section indices to physical
376      8     [47]    reloc_list_0                 SortedArray* -- relocation linked list / pending relocations
384      8     [48]    reloc_list_1                 SortedArray* -- resolved relocation list
392      8     [49]    reloc_list_2                 SortedArray* -- additional relocation list
408      8     [51]    input_section_list           SortedArray* -- per-input section tracking (32-element initial)
416      4     [52]    input_section_count          Counter for input_section_list
448      8     [56]    global_symbol_list           SortedArray* -- (freed during destroy)
456      8     [57]    symbol_index_mapping         Pointer -- maps old symbol indices to new (uint32_t array)
464      8     [58]    neg_symbol_index_mapping     Pointer -- maps old negative symbol indices to new (uint32_t array)
472      8     [59]    section_virtualization_table  Pointer -- maps virtual section indices to physical (uint32_t array)
480      8     [60]    file_list                    LinkedList* -- singly-linked list of input file records (node+8 = data)
488      8     [61]    arch_vtable                  Pointer -- architecture-specific vtable (from sub_45AC50 or sub_459640)
496      8     [62]    entry_hash                   LinkerHash* -- hash table for entry-point/syscall symbol lookup (32 buckets)
504      8     [63]    (reserved)                   (padding or unused pointer)
512      8     [64]    input_file_records           SortedArray* -- 16-byte records: {name_ptr, sm_minor, reserved} (8-element initial)
520      8     [65]    sorted_array_0               SortedArray* -- 16-element sorted array (section management)
528      8     [66]    sorted_array_1               SortedArray* -- 16-element sorted array (section management)
536      8     [67]    sorted_array_2               SortedArray* -- 16-element sorted array (section management)
544      8     [68]    sorted_array_3               SortedArray* -- 16-element sorted array (section management)
552      8     [69]    sorted_array_4               SortedArray* -- 16-element sorted array (section management)
560      8     [70]    sorted_array_5               SortedArray* -- 16-element sorted array (section management)
576      8     [72]    reloc_type_hash              LinkerHash* -- hash table for relocation type tracking (8 buckets, int-keyed)
584      8     [73]    (reserved)                   (unused or padding)
592      8     [74]    merged_symbol_array          SortedArray* -- symbols after merge (used as override for section resolution)
600      8     [75]    extended_symbol_store        SortedArray* -- extended symbol entries (for cross-reference)
608      8     [76]    private_arena_ptr            Arena* -- owning "elfw memory space" arena (only if merge_flags & 0x400)
616      8     [77]    private_arena_handle         Arena handle from sub_45CAE0
624      4     [78]    option_parser_result         Result from sub_42F8B0 (option parser state)
664      8     [83]    end_marker                   Zeroed, marks end of struct
```

## Signed Index Convention

A critical design pattern throughout the linker context is the **signed section/symbol index** convention:

- **Positive index**: Refers to the `pos_symbol_array` (offset 344) or regular section entries
- **Negative index**: Refers to the `neg_symbol_array` (offset 352), with the absolute value as the array index
- **Index 0**: Null / undefined

This dual-array scheme is used because the merge phase introduces new sections/symbols that may conflict with or extend the numbering of the original input. The accessor `sub_443260` implements this dispatch:

```c
SectionRecord *elfw_get_section(elfw *ctx, int signed_index) {
    SymbolRecord *sym;
    if (signed_index < 0)
        sym = SortedArray_get(ctx->neg_symbol_array, -signed_index);   // ctx[44]
    else
        sym = SortedArray_get(ctx->pos_symbol_array, signed_index);    // ctx[43]

    if (!sym) return NULL;

    uint16_t section_idx = sym->section_index;                         // sym + 6
    if (section_idx == 0xFFFF) {
        // Deferred resolution: symbol has an indirect reference at sym+24
        int ref = *(int32_t *)(sym + 24);

        if (ctx->extended_symbol_store) {                              // ctx[75]
            // Resolve through extended store
            if (ref < 0)
                section_idx = SortedArray_get(ctx->extended_symbol_store, -ref);
            // else fall through to merged_symbol_array
        } else {
            // Resolve through mapping tables
            uint32_t *pos_map = ctx->symbol_index_mapping;             // ctx[57]
            uint32_t *neg_map = ctx->neg_symbol_index_mapping;         // ctx[58]
            int remapped = remap_index(pos_map, neg_map, ref);
        }
        section_idx = SortedArray_get(ctx->merged_symbol_array, remapped);  // ctx[74]
    }

    // Virtualization check
    if (ctx->section_virtualization_active) {                          // byte at +82
        uint32_t phys = ctx->section_virtualization_table[section_idx];  // ctx[59]
        if (phys && ctx->section_order_map[phys] != section_idx)
            fatal("secidx not virtual");
    }

    return SortedArray_get(ctx->section_array, section_idx);           // ctx[45]
}
```

The `0xFFFF` sentinel at `sym+6` means "this symbol's section has not been directly assigned yet -- look it up through the mapping chain." This indirection supports lazy resolution during multi-pass merge operations.

## Symbol Index Remapping

The remapping table pair at offsets 456 (`symbol_index_mapping`) and 464 (`neg_symbol_index_mapping`) translates symbol indices from input objects to the output object's symbol numbering. The accessor `sub_444720` implements the remap:

```c
uint32_t elfw_remap_symbol(elfw *ctx, int old_index) {
    uint32_t *pos_map = (uint32_t *)ctx[57];     // offset 456
    if (!pos_map || old_index == 0) return 0;

    if (old_index > 0) {
        uint32_t new_idx = pos_map[old_index];
        if (new_idx == 0)
            fatal("reference to deleted symbol");
        return new_idx;
    } else {
        uint32_t *neg_map = (uint32_t *)ctx[58];  // offset 464
        uint32_t new_idx = neg_map[-old_index];
        if (new_idx == 0)
            fatal("reference to deleted symbol");
        return new_idx;
    }
}
```

The `"reference to deleted symbol"` error appears 14+ times across the binary, always through this remapping path. It fires when dead-code elimination has removed a symbol that is still referenced by a relocation or section link.

## Construction: `sub_4438F0` (`elfw_create`)

### Parameters

```
elfw *elfw_create(
    a1:  type_code,       // Overloaded: arena pointer in some paths, type code in others
    a2:  is_64bit,        // 0 = 32-bit ELF, nonzero = 64-bit ELF
    a3:  abi_version,     // e_ident[EI_ABIVERSION] -- SM ABI version
    a4:  sm_major,        // SM architecture major version (e.g., 90, 100)
    a5:  sm_minor,        // SM architecture minor version / letter code
    a6:  debug_flag,      // Enable debug info generation
    a7:  api_version,     // CUDA API version or e_version
    a8:  verbose_flags,   // Verbose output control
    a9:  merge_flags,     // Master bitmask controlling all link behavior
    a10: is_relocatable   // Explicit relocatable-link flag
)
```

### Initialization Sequence

The construction proceeds in 11 ordered steps:

**Step 1 -- Private arena** (conditional on `merge_flags & 0x400`):

Creates an isolated "elfw memory space" arena via `sub_432020` with 4096-byte page size. Stores the arena at `ctx[76]` and its handle at `ctx[77]`. When this flag is not set, allocations go to the parent arena.

**Step 2 -- Struct allocation and zeroing**:

Allocates 672 bytes from the arena via `sub_4307C0`. Zeroes the entire buffer with `memset`. Sets the end marker at `ctx[83]` to 0.

**Step 3 -- ELF header template**:

Writes `\x7fELF` magic, class byte, data encoding (LSB), version, OSABI (0x41 for device, 0x33 for 32-bit), machine type (190 = EM_CUDA). For device ELF (`merge_flags & 0x8000`), sets `e_type = ET_EXEC` (2) for non-relocatable or `ET_REL` (1) for relocatable. If not device ELF, sets `e_type = 0x80000000` for relocatable.

**Step 4 -- Merge-flags decomposition**:

Extracts the 17 individual boolean flags into bytes 84--100. Forces `is_relocatable = 1` if `a10` is set or if `merge_flags & 0x180000` is nonzero, setting `merge_flags |= 0x80000`.

**Step 5 -- Architecture state initialization**:

Calls `sub_45AC50` (Mercury/relocatable path) or `sub_459640` (non-Mercury path) with the SM version. The returned pointer is stored at `ctx[61]` (offset 488). This is a vtable of ~70 function pointers covering all architecture-specific behaviors: relocation handlers, instruction encoders, section layout rules. If the call returns NULL, the constructor emits `"couldn't initialize arch state"` via `sub_467460` and aborts.

**Step 6 -- Hash table creation**:

Creates two 512-bucket `LinkerHash` instances for symbol name lookup (`ctx[36]`, offset 288) and section name lookup (`ctx[37]`, offset 296). Both use MurmurHash3-based string hashing (`sub_44E000`) and `strcmp` equality (`sub_44E180`).

**Step 7 -- Sorted array creation**:

Creates nine sorted arrays via `sub_465020` (six with 16-element initial capacity) and `sub_464AE0` (three with 64-element initial capacity):

| Field | Offset | QWORD | Initial capacity | Contents |
|---|---|---|---|---|
| `sorted_array_0..5` | 520--560 | [65]--[70] | 16 | Section management (six arrays for different section categories) |
| `pos_symbol_array` | 344 | [43] | 64 | Positive-index symbols |
| `neg_symbol_array` | 352 | [44] | 64 | Negative-index symbols |
| `section_array` | 360 | [45] | 64 | Section data records |

**Step 8 -- Nested sub-structures**:

Allocates a 104-byte sub-structure and links it into `section_array`. Allocates a 48-byte sub-structure and links it into both `pos_symbol_array` and `neg_symbol_array`. These serve as the initial (index-0) sentinel entries.

**Step 9 -- Input file records**:

Creates an 8-element sorted array at `ctx[64]` (offset 512). Allocates a 16-byte `<input>` record containing the string `"<input>"` as the name pointer and the SM minor version as the architecture identifier. Links this record into the array.

**Step 10 -- Core ELF sections**:

Uses `sub_441AC0` (elfw\_add\_section) to create the mandatory sections:

| Section | Type | Link | Alignment | Entry size |
|---|---|---|---|---|
| `.shstrtab` | SHT_STRTAB (3) | 0 | 1 | 0 |
| `.strtab` | SHT_STRTAB (3) | 0 | 1 | 0 |
| `.symtab` | SHT_SYMTAB (2) | `.strtab` index | 8 (Elf64) or 4 (Elf32) | 24 (Elf64) or 16 (Elf32) |
| `.symtab_shndx` | SHT_SYMTAB_SHNDX (18) | `.symtab` index | 4 | 4 |

For device ELF only:
| Section | Type | Alignment | Description |
|---|---|---|---|
| `.note.nv.tkinfo` | SHT_NOTE (7) | 0x2000000 | Toolkit info note |
| `.note.nv.cuinfo` | SHT_NOTE (7) | 0x1000000 | CUDA compilation info |

For non-relocatable output only:
| Section | Type | Entry size | Description |
|---|---|---|---|
| `.nv.uft.entry` | 0x70000011 | 32 | Unified function table entries |

**Step 11 -- Entry-point hash and callgraph**:

Creates a 32-bucket string-keyed `LinkerHash` at `ctx[62]` (offset 496) for entry-point symbol tracking. Populates it with the built-in CUDA syscall names from the static table at `off_1D3A9C0`. Creates an 8-bucket integer-keyed `LinkerHash` at `ctx[72]` (offset 576) for relocation type tracking. Calls `sub_4504B0` to initialize callgraph structures.

## Destruction: `sub_4475B0` (`elfw_destroy`)

The destructor takes the context pointer and a mode flag. It has two code paths:

### Path 1: Private arena (ctx[76] != 0)

When the context owns a private arena, destruction is simple -- destroy the arena and everything allocated from it goes away:

```c
void elfw_destroy(elfw *ctx, uint64_t mode) {
    if (ctx->private_arena_ptr) {                  // ctx[76]
        sub_45CAE0(ctx->private_arena_handle, mode);   // release arena handle
        sub_431C70(ctx->private_arena_ptr, 0);         // arena_destroy(arena, no_merge)
        return;
    }
    // ... individual cleanup follows
}
```

### Path 2: Individual cleanup (ctx[76] == 0)

When no private arena exists, each sub-structure must be freed individually. The sequence walks every field that owns allocated memory:

1. **Index mapping tables** (offsets 456, 464, 472): Free with `arena_free`
2. **Hash tables** (offsets 288, 296): Walk entries via `sub_448C00` calling `sub_440080` (symbol record destructor), then destroy hash tables via `sub_448A40`
3. **Reloc tracking pointers** (offsets 336, 328): Free via `arena_free`
4. **Sorted arrays** at offsets 520--560: Free each via `sub_466E00` with destructor `sub_45CAD0`
5. **Relocation lists** (offsets 376, 384, 392): Destroy via `sub_464550`
6. **Positive symbols** (offset 344): Iterate all entries starting at index 0, free each record, then destroy the array via `sub_464B90`
7. **Negative symbols** (offset 352): Iterate entries starting at index 1 (skipping sentinel), free each record, destroy array
8. **Merged/extended arrays** (offsets 592, 600): Destroy via `sub_464B90` if non-null
9. **Input file records** (offset 512): Iterate entries, free each 16-byte record, destroy array
10. **Global symbol list** (offset 448): Free via `sub_464520`
11. **Additional arrays** (offsets 424, 432, 440): Free via `sub_464520`
12. **Callgraph cleanup**: `sub_44CC60(ctx)` -- destroys callgraph state
13. **Sorted arrays** (offsets 256, 272, 280, 264): Free via `sub_464520`
14. **Section order map** (offset 368): Free via `arena_free`
15. **Section records in section_array** (offset 360): Iterate entries, free section data at `entry+72`, free each record, destroy array
16. **File list** (offset 480): Walk linked list, free each node's data at `node+8`
17. **Entry hash and reloc type hash** (offsets 496, 576): Destroy via `sub_448A40`
18. **File list storage** (offset 480): Free via `sub_464520`
19. **Additional storage** (offset 448): Free via `sub_464520`
20. **Arch state** (offset 488): Destroy via `sub_45B680`
21. **The struct itself**: Free via `arena_free`

The ordering is significant -- hash tables must be walked before the entries they reference are freed, and the arch state must outlive any code that might call through the vtable during teardown.

## Context Flow Through Pipeline Phases

The linker context is the single thread of mutable state that connects all pipeline phases. In `main()`, it is stored in a local variable and passed as the first argument to every phase function.

### Phase 5: Context Creation

```c
elfw *ctx = elfw_create(
    (byte_2A5F1E8 == 0) + 1,     // type: 1=exec, 2=relocatable
    1,                             // 64-bit
    sm_abi_version,                // ABI version
    sm_major,                      // e.g., 100
    sm_minor,                      // e.g., 0x61
    debug_flag,                    // from -g
    api_version,                   // from --cuda-api-version
    verbose_flags,                 // from -v
    merge_flags,                   // accumulated from all CLI options
    is_relocatable                 // from -r
);
```

After creation, `ctx` holds an ELF skeleton with core sections but no application content.

### Phase 7: Input File Loop

Each input cubin passes through `sub_426570` (validate arch) which reads `ctx->sm_arch_major` (offset 72) and `ctx->e_flags` (offset 48) to verify architecture compatibility. Input file records are appended to `ctx[64]` (offset 512).

### Phase 9: Merge

`merge_elf` (`sub_45E7D0`) is called once per input object:

```c
int err = merge_elf(ctx, input_elf, filename, ...);
```

During merge, the function:
- Adds symbols to `pos_symbol_array` / `neg_symbol_array` (offsets 344, 352)
- Hashes symbol names into `symbol_name_hash` (offset 288)
- Adds sections to `section_array` (offset 360)
- Hashes section names into `section_name_hash` (offset 296)
- Appends relocations to `reloc_list_0` / `reloc_list_1` (offsets 376, 384)
- Builds the symbol index mapping tables (offsets 456, 464) for each input
- Populates the callgraph via `sub_4504B0`
- Writes CUDA metadata into `tkinfo_buffer` / `cuinfo_buffer` (offsets 108, 140)

### Phase 10: Shared Memory Layout

`sub_439830` reads the section arrays to identify `.nv.shared.*` and `.nv.global` sections, computes overlapping-set analysis, and updates section offsets in `ctx->section_array`. The `section_virtualization_table` (offset 472) may be populated here.

### Phase 11: Dead Code Elimination

`sub_44AD40` traverses the callgraph built during merge, marks reachable symbols, then removes unreachable sections from `ctx->section_array` and symbols from the symbol arrays. The "reference to deleted symbol" remapping errors can originate from dangling references after this phase.

### Phase 12: Layout

`sub_465720` and related functions read all sections from `ctx->section_array`, sort them, compute file offsets and virtual addresses, and store the results back into section records. The `section_order_map` (offset 368) is built here.

### Phase 13: Relocation

`sub_469D60` reads relocations from the relocation lists (offsets 376--392), resolves symbol addresses through the mapping tables (offsets 456--464), and applies relocation patches to section data. The `arch_vtable` at offset 488 provides architecture-specific relocation handlers.

### Phase 14: Finalization

`sub_445000` performs final relocation application and ELF finalization. It may call `sub_444690` to reset the arch state. The function reads nearly every field of the context.

### Phase 15: Serialization

`sub_45BF00` reads the ELF header template (offsets 0--62), iterates all sections in `ctx->section_array`, and serializes the complete ELF to a byte buffer. The buffer is then written to disk via `sub_45C920` (file) or returned to a caller via `sub_45C950` (memory).

### Phase 16: Destruction

`sub_4475B0` cleans up the context as described in the destruction section above.

## Orchestrator: `sub_447570`

The function at `0x447570` is a thin orchestrator that chains three phases through the context:

```c
void elfw_layout_relocate_finalize(elfw *ctx, bool do_layout, bool do_reloc) {
    if (do_layout)
        sub_439830(ctx);    // shared memory layout
    else
        sub_438C60(ctx);    // alternative layout path

    if (do_reloc)
        sub_469D60(ctx);    // apply relocations

    sub_445000(ctx, ...);   // finalization
}
```

This collapses Phases 10, 13, and 14 into a single call when the pipeline mode requires all three in sequence.

## Architecture Vtable (offset 488)

The `arch_vtable` pointer at offset 488 deserves special attention. It points to a structure created by `sub_459640` (non-Mercury) or `sub_45AC50` (Mercury/relocatable), which allocates a ~632-byte block containing approximately 70 function pointers. Each pointer is a handler for a specific relocation type or architecture-specific operation.

The vtable is dispatched on SM version during creation:

| SM range | Handler set | Notes |
|---|---|---|
| 30--39 | Kepler handlers | Legacy |
| 50--59 | Maxwell handlers | — |
| 60--69 | Pascal handlers | — |
| 70--74 | Volta handlers | — |
| 75--79 | Turing handlers | — |
| 80--89 | Ampere/Ada handlers | — |
| 90--99 | Hopper handlers | — |
| 100+ | Mercury (Blackwell+) handlers | New relocation types |

The vtable is called through the context during relocation:

```c
// sub_469D60 -- apply_relocations
void (*handler)(elfw *ctx, ...) = ctx->arch_vtable->handlers[reloc_type];
handler(ctx, section, offset, addend, symbol_value);
```

## Syscall Symbol Table (offset 496)

The hash table at `ctx[62]` (offset 496) maps CUDA syscall symbol names to boolean presence flags. It is populated during construction from a static table at `off_1D3A9C0` containing the names of all built-in CUDA device runtime syscalls. The accessors `sub_4447B0`, `sub_444830`, and `sub_444840` check this table:

- `sub_4447B0`: Returns 1 if the name is `__cuda_syscall*` or is in the entry hash
- `sub_444830`: Exact match for `__cuda_syscall_32f3056bbb`
- `sub_444840`: Checks syscall status AND verifies the name starts with `cnp` (cooperative launch prefix)

These checks are used during dead-code elimination and cudadevrt handling to distinguish intrinsic syscalls (which cannot be dead-code eliminated) from regular functions.

## Section Virtualization (offset 472)

When section virtualization is active (byte at offset 82 is nonzero), the `section_virtualization_table` at offset 472 maps each virtual section index to its physical section index. This table is populated during the merge phase when sections from different input objects map to the same output section (e.g., multiple `.nv.constant0` sections merging into one).

The invariant enforced by every accessor is:

```c
if (ctx->section_virtualization_active) {
    uint32_t physical = ctx->section_virtualization_table[virtual_idx];
    if (physical != 0 && ctx->section_order_map[physical] != virtual_idx)
        fatal("secidx not virtual");
}
```

This consistency check appears in `sub_443260`, `sub_443500`, and many other accessors. It catches corruption in the merge-time section mapping.

## Cross-References

- [Device ELF Format](../elf/device-elf-format.md) -- wire-format perspective on the same 672-byte structure
- [Symbol Tables & Hash Maps](../linker/hash-tables.md) -- LinkerHash implementation used at offsets 288, 296, 496, 576
- [Pipeline Overview](../pipeline/overview.md) -- the 14-phase pipeline that threads this context
- [Merge Phase](../pipeline/merge.md) -- primary consumer of the context during linking
- [Relocation Engine](../linker/relocation-engine.md) -- uses the arch vtable at offset 488
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- depends on symbol mapping tables at offsets 456/464
