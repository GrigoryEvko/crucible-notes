# Section Merging

Section merging is the process by which nvlink combines identically-named sections from multiple input cubin ELF objects into single output sections. This spans two pipeline phases: during the **merge phase** (`sub_45E7D0`), input sections are classified, mapped to output sections, and their data appended; during the **layout phase** (`sub_439830`), the merged sections receive final addresses and sizes via the section layout engine. This page covers the section-level mechanics -- how sections are found, created, mapped, populated, and how overlapping data is validated.

## Overview

A conventional linker merges `.text` from N objects into one `.text`. A GPU device linker does something fundamentally different: CUDA sections represent distinct GPU memory spaces (global, shared, local, constant) with hardware-specific alignment and overlap rules. nvlink must:

1. Classify each input section by its CUDA memory space, not just its ELF `sh_type`.
2. Create output sections on demand, indexed by both name and type.
3. Accumulate data contributions into linked lists (not flat buffers), deferring final layout.
4. Validate that overlapping data regions are identical (multiple TUs may define the same global).
5. Handle per-kernel sections that must be split by entry point (constant banks, local data, shared memory).

The section merging infrastructure is built on four core primitives -- `find_section_by_name`, `section_create`, `section_data_copy`, and `section_layout_engine` -- plus five specialized overlap-merge functions for each memory space.

## Section Classification

During the merge phase, input sections are reclassified from standard ELF types into NVIDIA CUDA section types. The dispatch logic in `merge_elf` (`sub_45E7D0`) uses both the `sh_type` field and the section name prefix:

```
Input sh_type      Section name prefix        Output type
--------------------------------------------------------------
SHT_NOBITS (8)     .nv.global                 SHT_CUDA_GLOBAL          (0x70000007)
SHT_NOBITS (8)     .nv.shared.                SHT_CUDA_SHARED          (0x7000000A)
SHT_NOBITS (8)     .nv.shared.reserved.       SHT_CUDA_SHARED_RESERVED (0x70000015)
SHT_NOBITS (8)     .nv.local.                 SHT_CUDA_LOCAL           (0x70000009)
SHT_PROGBITS (1)   .nv.constant<N>            SHT_CUDA_CONSTANT0 + N   (0x70000064 + N)
SHT_PROGBITS (1)   .nv.global.init            SHT_CUDA_GLOBAL_INIT     (0x70000008)
SHT_PROGBITS (1)   (other)                    original sh_type
```

The constant bank number is parsed from the section name suffix: `strtol(name + 12, NULL, 10)`. This means `.nv.constant0` becomes type `0x70000064`, `.nv.constant2` becomes `0x70000066`, and so on up to `.nv.constant17` (`0x70000075`).

The reclassification is critical because the ELF types alone are ambiguous. A `SHT_NOBITS` section could be global data, shared memory, or local data -- only the name distinguishes them. After reclassification, the type code drives all subsequent dispatch.

## Section Registry

nvlink maintains two hash tables on the elfw (ELF wrapper) object for section lookup:

| Offset | Hash table | Keyed by | Maps to |
|---|---|---|---|
| `+288` | Name-to-symbol table | Section name string | Symbol index (positive for local, negative for global) |
| `+296` | Name-to-section table | Section name string | Section index in the section vector |

Both tables use the same hash/compare infrastructure (`sub_4489C0` / `sub_449A80` / `sub_448E70`). The name-to-section table is the primary lookup mechanism for section merging.

### `find_section_by_name` (`sub_4411D0`)

```c
// sub_4411D0 -- look up section index by name
// Returns: section index, or 0 if not found
uint32_t find_section_by_name(elfw *ctx, const char *name) {
    uint32_t *entry = hash_lookup(ctx->section_name_table, name);
    if (entry)
        return *entry;
    return 0;
}
```

This is a thin wrapper over `sub_449A80` (hash table lookup). It returns the section index stored at `elfw+296`, or 0 for "not found." Section index 0 is reserved (the ELF null section), so 0 is an unambiguous sentinel.

### `get_section_header` (`sub_442270`)

```c
// sub_442270 -- retrieve section header record by index
// Returns: pointer to 104-byte section record
section_record *get_section_header(elfw *ctx, uint32_t section_idx) {
    if (ctx->has_virtual_sections) {    // ctx+82
        uint32_t mapped = ctx->virtual_map[section_idx];  // ctx+472
        if (mapped != 0) {
            assert(ctx->reverse_map[mapped] == section_idx,
                   "secidx not virtual");
        }
    }
    return vector_get(ctx->all_sections, section_idx);  // ctx+360
}
```

The virtual section mapping (`ctx+472`) supports section index indirection for advanced linking modes. In normal operation, it is a direct index into the section vector at `ctx+360`.

## Section Creation (`sub_441AC0`)

When `find_section_by_name` returns 0, a new output section must be created. This is handled by `sub_441AC0`, which allocates a 104-byte section record, registers it in the section vector and name table, and optionally creates an associated relocation section.

```c
// sub_441AC0 -- create a new section in the output ELF
// a1: elfw context
// a2: section name (e.g., ".nv.global")
// a3: section type (e.g., 0x70000007 for SHT_CUDA_GLOBAL)
// a4: section flags (e.g., SHF_ALLOC | SHF_WRITE = 3)
// a5: sh_info value (typically 0)
// a6: sh_link value (parent section index for reloc sections)
// a7: sh_addralign
// a8: sh_entsize
// Returns: assigned section index
uint32_t section_create(elfw *ctx, const char *name, uint32_t sh_type,
                        uint64_t sh_flags, uint32_t sh_info,
                        uint32_t sh_link, uint64_t sh_addralign,
                        uint64_t sh_entsize);
```

### Section Record Layout (104 bytes)

The 104-byte section record stores the output ELF section header fields plus internal linked list pointers:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `sh_type` |
| 4 | 4 | `sh_type_ext` (extended type for dispatch) |
| 8 | 8 | `sh_flags` |
| 16 | 8 | reserved |
| 24 | 8 | `sh_offset` (assigned during write) |
| 32 | 8 | `sh_size` (total section size, updated during layout) |
| 40 | 4 | `sh_info` |
| 44 | 4 | `sh_link` |
| 48 | 8 | `sh_addralign` (maximum alignment across all contributions) |
| 56 | 8 | `sh_entsize` |
| 64 | 4 | `section_index` (assigned by creation order) |
| 68 | 4 | padding |
| 72 | 8 | `symbol_list_head` (linked list of data contributions) |
| 80 | 8 | `symbol_list_tail` (for O(1) append) |
| 88 | 8 | reserved |
| 96 | 8 | `name_ptr` (pointer to section name string) |

### Creation Algorithm

1. **Look up existing section** by name in `ctx+296`. If found, retrieve the existing section record and reuse its type/flags metadata.

2. **Allocate section record**: `arena_alloc(104)`, zero-initialize via `memset`.

3. **Guard check**: If the callgraph has already been built (`ctx+81` set) and the new section has executable flags (flags & 4), emit a fatal error: `"adding function section after callgraph completed"`. This prevents section creation after DCE has already pruned the callgraph.

4. **Initialize fields**: Copy `sh_type`, `sh_flags`, `sh_info`, `sh_link`, `sh_addralign`, `sh_entsize` from the arguments.

5. **Assign section index**: Increment the global section counter at `ctx+584` and store it in the record at offset 64.

6. **Register in section vector**: Call `sub_464C30` to append the record to the all-sections vector at `ctx+360`.

7. **Register in name table**: If this is a new name (not found in `ctx+296`), allocate a 12-byte name-index node via `arena_alloc(12)`, copy the name string into arena memory, and insert into the hash table at `ctx+296` via `sub_448E70`.

8. **Create relocation section** (conditional): If the link mode is not relocatable (`ctx+16 != 1`), the DCE flag (`ctx+83`) is set, and the section type is in the constant bank range (`0x70000064..0x7000007E` or `0x70000006`), the function recursively calls itself to create a `.rela<name>` or `.rel<name>` section of type `SHT_RELA` (4) or `SHT_REL` (9), linked back to the parent section.

9. **Register in name-to-symbol table**: Insert the section's assigned symbol index into the hash table at `ctx+288` via the same `sub_448E70` call.

### Section Index Overflow Handling

Section indices are stored as 16-bit values in ELF symbol table entries (`st_shndx`). When the section count exceeds `0xFEFF` (65279), nvlink switches to extended section index tables:

```c
if (section_index > 0xFEFF && section_index != 65522) {
    record->st_shndx = SHN_XINDEX;  // 0xFFFF
    if (ctx->xindex_table == NULL) {
        ctx->xindex_table = vector_create(0x10000);  // ctx+592
        ctx->xindex_reverse = vector_create(0x10000); // ctx+600
    }
    vector_set(ctx->xindex_table, sym_index, section_index);
}
```

This follows the ELF `SHN_XINDEX` convention, storing the real index in a separate `.symtab_shndx`-like vector.

## Data Copy Primitive (`sub_433760`)

Once an output section exists, input data is appended via `sub_433760`. This function does not copy bytes into a flat buffer; instead, it creates a 40-byte **data node** that records the source pointer, alignment, offset, and size. The actual byte-copy happens later during ELF serialization.

```c
// sub_433760 -- append data contribution to a section
// a1: elfw context
// a2: section index (output)
// a3: source symbol index (in input, for tracking provenance)
// a4: alignment requirement
// a5: data size (bytes)
void section_data_copy(elfw *ctx, uint32_t section_idx,
                       uint32_t source_sym, uint32_t alignment,
                       uint64_t data_size);
```

### Data Node Layout (40 bytes)

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | `source_sym` -- source symbol index for provenance tracking |
| 8 | 8 | `offset` -- byte offset within the merged section |
| 16 | 8 | `alignment` -- alignment this contribution requires |
| 24 | 8 | `data_ptr` -- pointer to source data (from input ELF) |
| 32 | 8 | reserved (zeroed) |

### Copy Algorithm

```
section = get_section_header(ctx, section_idx)
if section is NULL: return   // silently skip unmapped sections

if alignment > section.sh_addralign:
    section.sh_addralign = alignment   // track maximum alignment

node = arena_alloc(40)
node.source_sym = source_sym
node.offset = 0
node.alignment = alignment
node.data_ptr = data_ptr

// Compute aligned insertion point
current_size = section.sh_size          // offset +32 in section record
remainder = current_size % alignment
if remainder != 0:
    current_size = current_size + alignment - remainder

node.offset = current_size
section.sh_size = current_size + data_size

// Append to section's linked list via tail pointer
if section.symbol_list_head == NULL:
    list_prepend(node, &section.symbol_list_head)
    section.symbol_list_tail = section.symbol_list_head
else:
    assert(section.symbol_list_tail != NULL,
           "tail data node not found")
    list_insert_after(node, section.symbol_list_tail)
    section.symbol_list_tail = node
```

The linked-list approach means that during the merge phase, the section accumulates an ordered list of data contributions without ever copying bytes. Each node records where the data comes from and at what offset it will land. This is efficient for the common case where hundreds of input objects contribute small fragments to the same section.

## Section Layout Engine (`sub_4325A0`)

After all input objects have been merged, the layout phase calls the section layout engine to assign final offsets to every data contribution in a section. This function sorts the contributions by alignment and computes a packed layout.

```c
// sub_4325A0 -- lay out all symbols within a section
// a1: elfw context
// a2: pointer to section header record
// a3: initial offset (usually 0, but can be nonzero for shared memory)
// Returns: total section size
uint32_t section_layout_engine(elfw *ctx, section_record *section,
                               uint32_t initial_offset);
```

### Layout Algorithm

```
assert(section != NULL, "section not found")

// Sort symbol list by alignment (descending) unless in extended-smem mode
if not ctx->extended_smem_mode or not arch_supports(section.sh_type):
    list_sort(section.symbol_list, alignment_comparator)

current = initial_offset

for each symbol_node in section.symbol_list:
    sym_record = get_sym_record(ctx, symbol_node.sym_index)
    alignment = symbol_node.alignment

    if alignment > 0:
        // Explicit alignment: round up to alignment boundary
        if current % alignment != 0:
            current = current + alignment - (current % alignment)
    elif symbol_node.size > 0:
        // No explicit alignment: use natural alignment, capped at 8
        natural_align = min(symbol_node.size, 8)
        if current % natural_align != 0:
            current = current + natural_align - (current % natural_align)
    else:
        // Zero alignment AND zero size: only valid in no-opt mode
        assert(ctx->no_opt_mode,
               "should only reach here with no opt")

    // Assign offset to both the symbol record and the section-local record
    sym_record.value = current
    symbol_node.value = current
    // verbose: "variable %s at offset %d"

    current += symbol_node.size

section.total_size = current
return current
```

The sorting step is important: by placing the highest-alignment items first, the function minimizes internal fragmentation from alignment padding. The sort is performed by `sub_4647D0` with comparator `sub_432440`.

The `extended_smem_mode` flag (`ctx+100`) disables sorting for shared memory sections when the architecture supports extended shared memory. In that mode, the order from the input objects is preserved.

## Overlapping Data Merge

A GPU linker must handle a case that rarely appears in CPU linking: multiple translation units can define the same global variable with the same initializer data. This is common in CUDA because `__device__` variables at file scope have external linkage by default.

nvlink handles this through five specialized overlap-merge functions, one per memory space:

| Address | Function | Memory space | Size |
|---|---|---|---|
| `0x432B10` | `merge_overlapping_global` | `.nv.global` | 11,683 B |
| `0x437E20` | `merge_overlapping_local` | `.nv.local.*` | 11,580 B |
| `0x4343C0` | `merge_overlapping_constant` | `.nv.constant*` | 11,838 B |
| `0x434BC0` | `merge_overlapping_variant1` | (per-entry constant) | 11,147 B |
| `0x435390` | `merge_overlapping_variant2` | (per-entry data) | 11,156 B |

All five follow the same algorithm, differing only in the section record fields they read and the error messages they emit:

### Overlap Merge Algorithm (`sub_432B10`)

```c
// sub_432B10 -- validate and merge overlapping data
// a1: elfw context
// a2: output section index
// a3: input symbol index
// a4: source data pointer (from input ELF)
// a5: insertion offset (-1 means "auto-compute")
// a6: alignment
// n:  data size
int merge_overlapping_global(elfw *ctx, uint32_t section_idx,
                             uint32_t sym_idx, void *data,
                             uint64_t offset, uint32_t alignment,
                             size_t size);
```

The algorithm walks the section's existing data-node linked list and checks whether the new contribution overlaps with any existing one:

```
for each existing_node in section.symbol_list:
    if new_range overlaps existing_range:
        overlap_start = max(new_offset, existing_offset)
        overlap_end = min(new_offset + new_size, existing_offset + existing_size)
        overlap_size = overlap_end - overlap_start

        // Validate: overlapping bytes must be identical
        if memcmp(new_data + (overlap_start - new_offset),
                  existing_data + (overlap_start - existing_offset),
                  overlap_size) != 0:
            fatal("overlapping non-identical data")

        if overlap spans entire new contribution:
            return   // nothing new to add, existing data subsumes it

        if overlap spans too much:
            fatal("overlapping data spans too much")
```

The key invariant is that overlapping regions must contain byte-for-byte identical data. If they do not, the linker emits a fatal error via `sub_467460`. This catches the case where two TUs define the same `__device__` variable with different initializers -- a programming error that would cause silent data corruption on the GPU.

Diagnostic strings from this family of functions:
- `"offset %lld goes past section %d size"` -- bounds check failure
- `"offset %lld (sym %d) overlaps in section %d"` -- overlap detected (info, not error)
- `"overlapping non-identical data"` -- fatal: data mismatch in overlap region
- `"overlapping data spans too much"` -- fatal: overlap exceeds expected bounds
- `"local data should have offset"` -- (local variant only) missing offset attribute

## Constant Bank Merge (`sub_438640`)

Constant bank sections (`.nv.constant0` through `.nv.constant17`) have special merge logic because they can be either global (shared across all kernels) or per-entry (specific to one kernel entry point). The function `sub_438640` handles both cases.

```c
// sub_438640 -- merge data into a constant bank section
// a1:  elfw context
// a2:  source section pointer
// a3:  symbol binding (1=GLOBAL, other=per-entry)
// a4:  symbol index
// a5:  data offset within source section
// a6:  alignment
// n:   data size
// s:   source data pointer
// a9:  constant bank type (0x70000064 + bank_number)
// a10: entry function section index (0 for global constants)
```

For per-entry constants (`a10 != 0`), the function constructs a per-entry section name:

```c
sprintf(name_buf, "%s.%s", bank_type_name, entry_function_name);
// e.g., ".nv.constant0.my_kernel"
```

It then calls `find_section_by_name` on the composite name. If the section does not exist, it creates it via `section_create` and registers it in the per-entry constant list at `ctx+272`. Data is then merged via the overlap merge function.

The function validates:
- Per-entry data must not have GLOBAL binding: `"entry data cannot be GLOBAL"`
- Per-entry data must have an explicit offset: `"entry data should have offset"`
- The section type must be a valid constant bank: `"bank SHT not CUDA_CONSTANT_?"`

## Symbol Offset Updates

When data is merged into an output section, the input symbol's value (address) must be updated to reflect its position in the merged section. This happens through `sub_440430`, which updates the symbol-to-section mapping:

```c
// sub_440430 -- update symbol's section association
// a1: elfw context
// a2: symbol record pointer
// a3: new section index
void update_symbol_section(elfw *ctx, symbol_record *sym,
                           uint32_t new_section_idx);
```

If the section index exceeds `0xFEFF`, the function uses the extended section index table at `ctx+592`/`ctx+600`. Otherwise, it writes the section index directly into the symbol's `st_shndx` field.

For deleted symbols (from dead code elimination or weak resolution), the function checks the symbol mapping tables at `ctx+456`/`ctx+464` and emits `"reference to deleted symbol"` if a relocation still points to a removed symbol.

## Global Data Pending-Merge List

Global variables (`.nv.global`) have a special accumulation path. During the merge phase, globals are not immediately placed into the output section. Instead, they are appended to a pending-merge list at `ctx+448`. This defers the actual merge until the layout phase, which processes them in Phase 1:

```
// Layout Phase 1 (in sub_439830):
if ctx->pending_globals is not NULL:
    section = find_or_create(".nv.global", SHT_CUDA_GLOBAL)

    // walk to tail of existing symbol list
    tail = section.symbol_list_head
    while tail.next: tail = tail.next
    section.symbol_list_tail = tail

    // drain pending list
    for each pending in ctx->pending_globals:
        sym_record = get_sym_record(ctx, pending.sym_index)
        update_symbol_section(ctx, sym_record, section_index)
        section_data_copy(ctx, section_index, 0,
                          sym_record.alignment, sym_record.data_size)
```

This deferred approach allows the linker to collect all global variable definitions before computing their final layout, which is necessary for overlap validation.

## Per-Entry Section Naming

CUDA kernels have per-kernel sections for shared memory, local data, and constant banks. These follow a naming convention:

```
<base_section_name>.<entry_function_name>
```

Examples:
- `.nv.shared.my_kernel` -- shared memory for kernel `my_kernel`
- `.nv.local.my_kernel` -- local memory for kernel `my_kernel`
- `.nv.constant0.my_kernel` -- constant bank 0 for kernel `my_kernel`

The section name is constructed via `sprintf("%s.%s", base_name, entry_name)`. Both `sub_438640` (constant bank merge) and the layout phase (`sub_439830`, Phase 9a) use this convention. The per-entry sections are tracked in linked lists on the elfw object:

| Offset | List | Contents |
|---|---|---|
| `+256` | Per-entry shared sections | `.nv.shared.<kernel>` section indices |
| `+264` | Extern shared sections | `__shared__ extern` placeholder section indices |
| `+272` | Per-entry constant sections | `.nv.constant<N>.<kernel>` section indices |
| `+280` | Per-entry local sections | `.nv.local.<kernel>` section indices |

## Duplicate Detection

The merge phase tracks duplicate definitions through two mechanisms:

1. **Symbol-level duplicates**: When `merge_elf` encounters a global symbol that already exists in the output, it checks binding and size. Strong-strong conflicts produce a multiple-definition error. Common symbols (section index `SHN_COMMON`, `0xFFF2`) resolve to the larger size. The verbose trace prints `"increase size of common %s"` or `"global.init replaces common for %s"`.

2. **Data-level duplicates**: The overlap merge functions (`sub_432B10` family) detect when two input objects contribute data to the same byte range within a section. Identical data is silently merged (no diagnostic); non-identical data is a fatal error.

The constant deduplication engine (`sub_4339A0`, called during layout Phase 9) goes further: it finds identical 32-bit and 64-bit constant values across different symbols and aliases them to save space. Verbose output: `"found duplicate value 0x%x, alias %s to %s"` and `"found duplicate 64bit value 0x%llx, alias %s to %s"`.

## Section Types Reference

Complete list of NVIDIA CUDA section types used during merging:

| Type (hex) | Type (decimal) | Name | Memory space |
|---|---|---|---|
| `0x70000000` | 1879048192 | `SHT_CUDA_INFO` | Metadata (.nv.info) |
| `0x70000001` | 1879048193 | `SHT_CUDA_CALLGRAPH` | Callgraph edges |
| `0x70000002` | 1879048194 | `SHT_CUDA_CALLGRAPH_INFO` | Callgraph per-function info |
| `0x70000004` | 1879048196 | `SHT_CUDA_FUNCDATA` | Debug function data |
| `0x70000006` | 1879048198 | `SHT_CUDA_CONSTANT` | Constant (base type) |
| `0x70000007` | 1879048199 | `SHT_CUDA_GLOBAL` | Global data (.nv.global) |
| `0x70000008` | 1879048200 | `SHT_CUDA_GLOBAL_INIT` | Global init data (.nv.global.init) |
| `0x70000009` | 1879048201 | `SHT_CUDA_LOCAL` | Local data (.nv.local.*) |
| `0x7000000A` | 1879048202 | `SHT_CUDA_SHARED` | Shared memory (.nv.shared.*) |
| `0x70000011` | 1879048209 | `SHT_CUDA_RELOCINFO` | Relocation info (.nv.rel.*) |
| `0x70000014` | 1879048212 | `SHT_CUDA_RELOCINFO_EXT` | Extended reloc (.nv.rela.*) |
| `0x70000015` | 1879048213 | `SHT_CUDA_SHARED_RESERVED` | Reserved shared memory |
| `0x70000064` | 1879048292 | `SHT_CUDA_CONSTANT0` | Constant bank 0 |
| `0x70000065`-`0x70000075` | 1879048293-1879048309 | `SHT_CUDA_CONSTANT1`-`17` | Constant banks 1-17 |
| `0x70000086` | 1879048326 | `SHT_CUDA_COMPAT` | Compatibility attributes |

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x4411D0` | `find_section_by_name` | ~200 B | Hash table lookup: name -> section index |
| `0x4411B0` | `find_symbol_by_name` | ~200 B | Hash table lookup: name -> symbol index |
| `0x441AC0` | `section_create` | 9,523 B | Allocate section record, register in tables, optionally create reloc section |
| `0x433760` | `section_data_copy` | ~600 B | Create 40-byte data node, compute aligned offset, append to section list |
| `0x4325A0` | `section_layout_engine` | ~1,400 B | Sort symbols by alignment, assign offsets with padding |
| `0x440430` | `update_symbol_section` | ~500 B | Update symbol's section index (with xindex overflow handling) |
| `0x442270` | `get_section_header` | ~300 B | Retrieve section record by index (with virtual mapping) |
| `0x440590` | `get_sym_record` | ~300 B | Retrieve symbol record by index |
| `0x440350` | `get_section_of_sym` | ~200 B | Get section index that a symbol belongs to |
| `0x432B10` | `merge_overlapping_global` | 11,683 B | Overlap-validate and merge data in `.nv.global` |
| `0x437E20` | `merge_overlapping_local` | 11,580 B | Overlap-validate and merge data in `.nv.local.*` |
| `0x4343C0` | `merge_overlapping_constant` | 11,838 B | Overlap-validate and merge data in `.nv.constant*` |
| `0x434BC0` | `merge_overlapping_variant1` | 11,147 B | Overlap merge for per-entry constant data |
| `0x435390` | `merge_overlapping_variant2` | 11,156 B | Overlap merge for per-entry data variant |
| `0x435B60` | `merge_overlapping_host` | 11,014 B | Overlap merge with `.nv.host` reference |
| `0x438640` | `merge_constant_bank` | 4,043 B | Merge data into constant bank (global or per-entry) |
| `0x4339A0` | `constant_dedup` | 13,199 B | Find duplicate 32/64-bit constants, alias symbols |
| `0x449A80` | `hash_lookup` | ~500 B | Generic hash table lookup |
| `0x448E70` | `hash_insert` | ~500 B | Generic hash table insert |

## Error Conditions

| Error string | Function | Condition |
|---|---|---|
| `"section not found"` | `sub_4325A0` | Section pointer is NULL when layout is attempted |
| `"should only reach here with no opt"` | `sub_4325A0` | Zero-alignment zero-size symbol in optimized mode |
| `"tail data node not found"` | `sub_433760` | Section has no tail pointer but has a non-empty list |
| `"adding function section after callgraph completed"` | `sub_441AC0` | Executable section created after DCE |
| `"secidx not virtual"` | `sub_442270` | Virtual section mapping inconsistency |
| `"reference to deleted symbol"` | `sub_440430` | Symbol was removed but relocation still targets it |
| `"offset %lld goes past section %d size"` | `sub_432B10` | Data contribution exceeds section bounds |
| `"overlapping non-identical data"` | `sub_432B10` | Two overlapping regions have different byte content |
| `"overlapping data spans too much"` | `sub_432B10` | Overlap exceeds the contribution size |
| `"entry data cannot be GLOBAL"` | `sub_438640` | Per-entry constant has GLOBAL binding |
| `"entry data should have offset"` | `sub_438640` | Per-entry constant missing explicit offset |
| `"bank SHT not CUDA_CONSTANT_?"` | `sub_438640` | Section type not in constant bank range |

## Cross-References

- [Merge Phase](../pipeline/merge.md) -- the per-object merge loop that drives section merging
- [Layout Phase](../pipeline/layout.md) -- post-merge address assignment using the section layout engine
- [Symbol Resolution](symbol-resolution.md) -- how global/weak/local symbols are resolved during merge
- [Weak Symbols](weak-symbols.md) -- weak function selection policy (register count, PTX version)
- [Data Layout Optimization](data-layout-opt.md) -- constant deduplication and overlap optimization
- [Dead Code Elimination](dead-code-elimination.md) -- callgraph-based section removal before layout
