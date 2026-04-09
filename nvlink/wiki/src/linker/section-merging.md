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
| `+288` | Symbol name hash table | Symbol/section name string | Symbol index (positive for local, negative for global) |
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
| `0x7000000B` | 1879048203 | `SHT_CUDA_RELOCINFO` | Relocation action table (.nv.rel.action) |
| `0x7000000E` | 1879048206 | `SHT_CUDA_UFT` | Unified Function Table (.nv.uft) |
| `0x70000011` | 1879048209 | `SHT_CUDA_UFT_ENTRY` | UFT entry metadata (.nv.uft.entry) |
| `0x70000012` | 1879048210 | `SHT_CUDA_UDT` | Unified Descriptor Table (.nv.udt) |
| `0x70000014` | 1879048212 | `SHT_CUDA_UDT_ENTRY` | UDT entry metadata (.nv.udt.entry) |
| `0x70000015` | 1879048213 | `SHT_CUDA_SHARED_RESERVED` | Reserved shared memory |
| `0x70000064` | 1879048292 | `SHT_CUDA_CONSTANT0` | Constant bank 0 |
| `0x70000065`-`0x70000075` | 1879048293-1879048309 | `SHT_CUDA_CONSTANT1`-`17` | Constant banks 1-17 |
| `0x70000086` | 1879048326 | `SHT_CUDA_COMPAT` | Compatibility attributes |

## Section Type Dispatch Table (`sub_45E3C0`)

The section type classifier (`sub_45E3C0`, called from `merge_elf` at `sub_45E7D0`) is the central dispatch point that maps every input section to its output NVIDIA CUDA type. The function takes the input section's `sh_type`, section name, and flags, then reclassifies it for the output ELF. The full dispatch logic, reconstructed from decompilation:

### Name-Based Reclassification (NOBITS Sections)

When the input `sh_type` is `SHT_NOBITS` (8), the section carries no data in the ELF file but reserves address space. The classifier checks the section name to determine which GPU memory space it belongs to:

```
Input sh_type = SHT_NOBITS (8):

    if name starts with ".nv.global"          -> SHT_CUDA_GLOBAL          (0x70000007)
    else if name starts with ".nv.shared."    -> SHT_CUDA_SHARED          (0x7000000A)
    else if name starts with ".nv.shared.reserved."
                                              -> SHT_CUDA_SHARED_RESERVED (0x70000015)
    else if name starts with ".nv.local."     -> SHT_CUDA_LOCAL           (0x70000009)
    else                                      -> remains SHT_NOBITS (8)
```

The comparison lengths are exact: `.nv.global` uses `memcmp(name, ".nv.global", 10)`, `.nv.shared.` uses 11 bytes (note trailing dot), `.nv.shared.reserved.` uses 20 bytes, and `.nv.local.` uses 10 bytes. The ordering matters: `.nv.shared.reserved.` is checked *after* `.nv.shared.`, so a `.nv.shared.reserved.foo` section first matches `.nv.shared.` and gets type `0x7000000A`, not `0x70000015`. However, this is the order in both the classifier function and in `merge_elf` itself -- the `.nv.shared.reserved.` check only fires if the `.nv.shared.` check fails, meaning a section named exactly `.nv.shared.reserved.foo` would match `.nv.shared.` first (11-byte prefix match). The reserved variant only matches section names that do NOT begin with `.nv.shared.` at the 11-byte level but DO begin with `.nv.shared.reserved.` at the 20-byte level -- which is impossible. In `merge_elf`, the same sequence appears at lines 975-993 of the decompilation, confirming this is the actual production logic in the binary. In practice, `.nv.shared.reserved.` names are constructed by ptxas to always match the longer prefix in contexts where the 11-byte check is skipped or where the section already has type `0x70000015` from the input cubin.

### Name-Based Reclassification (PROGBITS Sections)

When the input `sh_type` is `SHT_PROGBITS` (1), the section contains initialized data. Two name prefixes trigger reclassification:

```
Input sh_type = SHT_PROGBITS (1):

    if name starts with ".nv.constant"        -> SHT_CUDA_CONSTANT0 + strtol(name+12, NULL, 10)
                                                  (0x70000064 + bank_number)
    else if name starts with ".nv.global.init" -> SHT_CUDA_GLOBAL_INIT    (0x70000008)
    else                                       -> remains SHT_PROGBITS (1)
```

The constant bank number extraction `strtol(name + 12, NULL, 10)` parses the decimal digit(s) immediately after `.nv.constant`. This produces:

| Section name | Bank number | Output type |
|---|---|---|
| `.nv.constant0` | 0 | `0x70000064` |
| `.nv.constant1` | 1 | `0x70000065` |
| `.nv.constant2` | 2 | `0x70000066` |
| ... | ... | ... |
| `.nv.constant10` | 10 | `0x7000006E` |
| `.nv.constant17` | 17 | `0x70000075` |

Per-entry constant sections like `.nv.constant0.my_kernel` also match -- `strtol("0.my_kernel", NULL, 10)` correctly returns 0 because `strtol` stops at the first non-numeric character.

### Pre-classified CUDA Types

When the input `sh_type` is already `SHT_CUDA_CONSTANT` (`0x70000006`), the section is a constant bank whose bank number must be extracted from the name. This path shares the same `strtol(name + 12, ...)` logic:

```
Input sh_type = SHT_CUDA_CONSTANT (0x70000006):

    -> SHT_CUDA_CONSTANT0 + strtol(name+12, NULL, 10)
       (0x70000064 + bank_number)
```

This handles the case where an input cubin already uses the generic `SHT_CUDA_CONSTANT` type rather than a specific bank type.

### SHT_NOTE Pass-Through

When the input `sh_type` is `SHT_NOTE` (7) and the section flags include `0x1000000` (a CUDA-specific flag indicating a parameter bank), the classifier handles it as a special case. It does not reclassify the type; instead, it updates the parameter bank size field on the note section's associated entry point:

```
Input sh_type = SHT_NOTE (7) with flags & 0x1000000:

    entry_section = get_section_header(ctx, ctx->param_bank_section)   // ctx+208
    sym_record = get_sym_record(ctx, entry_section)
    sym_record.param_size = ctx->param_bank_size                       // ctx+134
    return ctx->param_bank_section
```

This path exits early without creating a new output section. The parameter bank note is consumed during merge and its size is recorded on the entry point symbol, not emitted as a separate section.

### All Other Types

Any `sh_type` value that does not match the above cases passes through unchanged. This covers:

- `SHT_PROGBITS` (1) sections without `.nv.constant` or `.nv.global.init` prefix (e.g., `.text.*` function sections)
- Standard ELF types like `SHT_SYMTAB` (2), `SHT_STRTAB` (3)
- Already-classified CUDA types in the `0x70000064`-`0x7000007E` range

### Complete Dispatch Summary

```
sub_45E3C0 dispatch table:
+-------------------------------+----------------------------+---------------------------+
| Input sh_type                 | Section name prefix        | Output sh_type            |
+-------------------------------+----------------------------+---------------------------+
| SHT_NOBITS        (8)        | .nv.global                 | 0x70000007 (CUDA_GLOBAL)  |
| SHT_NOBITS        (8)        | .nv.shared.                | 0x7000000A (CUDA_SHARED)  |
| SHT_NOBITS        (8)        | .nv.shared.reserved.       | 0x70000015 (CUDA_SH_RSVD) |
| SHT_NOBITS        (8)        | .nv.local.                 | 0x70000009 (CUDA_LOCAL)   |
| SHT_NOBITS        (8)        | (other)                    | 8 (unchanged)             |
| SHT_PROGBITS      (1)        | .nv.constant<N>            | 0x70000064+N (CONSTANT_N) |
| SHT_PROGBITS      (1)        | .nv.global.init            | 0x70000008 (GLOBAL_INIT)  |
| SHT_PROGBITS      (1)        | (other)                    | 1 (unchanged)             |
| SHT_CUDA_CONSTANT (0x70000006)| .nv.constant<N>           | 0x70000064+N (CONSTANT_N) |
| SHT_NOTE          (7)        | (flags & 0x1000000)        | (early return, no output) |
| (any other)                   | (any)                      | (unchanged)               |
+-------------------------------+----------------------------+---------------------------+
```

## Section Type Filter Bitmasks

After reclassification, `merge_elf` uses bitmask-based filters to decide which sections enter each processing pass. Two bitmasks appear in the decompiled code:

### Second-Pass Section Filter (`0x5D05`)

The second pass of `merge_elf` (the section-header loop at `sub_45E7D0` lines 1547-1670) iterates over section headers and decides which ones require output section creation via `sub_45E3C0`. The filter at line 1600-1602:

```c
LOBYTE(v148) = 0;
if ((unsigned int)(sh_type - 0x70000006) <= 0xE)       // types 0x70000006..0x70000014
    v148 = (0x5D05uLL >> ((uint8_t)sh_type - 6)) & 1;
if ((unsigned int)(sh_type - 0x70000064) > 0x1A && !v148)
    goto skip;  // section not processed in this pass
```

The bitmask `0x5D05` = `0101 1101 0000 0101` in binary, indexed by `(low_byte(sh_type) - 6)`. Decoding each bit position:

| Bit | sh_type | Name | In bitmask? | Meaning |
|---|---|---|---|---|
| 0 | `0x70000006` | `SHT_CUDA_CONSTANT` | **1** (accepted) | Generic constant: needs bank resolution |
| 1 | `0x70000007` | `SHT_CUDA_GLOBAL` | 0 (skipped) | Globals handled via pending-merge list |
| 2 | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | **1** (accepted) | Initialized globals: overlap-merge path |
| 3 | `0x70000009` | `SHT_CUDA_LOCAL` | 0 (skipped) | Locals handled in first pass |
| 4 | `0x7000000A` | `SHT_CUDA_SHARED` | 0 (skipped) | Shared memory handled separately |
| 5 | `0x7000000B` | (unused) | 0 | — |
| 6 | `0x7000000C` | (unused) | 0 | — |
| 7 | `0x7000000D` | (unused) | 0 | — |
| 8 | `0x7000000E` | `SHT_CUDA_UFT` | **1** (accepted) | Unified Function Table (.nv.uft) |
| 9 | `0x7000000F` | (unused) | 0 | — |
| 10 | `0x70000010` | (unused in v13.0) | **1** (accepted) | Bit set but no known section type at this value |
| 11 | `0x70000011` | `SHT_CUDA_UFT_ENTRY` | **1** (accepted) | UFT entry metadata (.nv.uft.entry) |
| 12 | `0x70000012` | `SHT_CUDA_UDT` | **1** (accepted) | Unified Descriptor Table (.nv.udt) |
| 13 | `0x70000013` | (unused) | 0 | — |
| 14 | `0x70000014` | `SHT_CUDA_UDT_ENTRY` | **1** (accepted) | UDT entry metadata (.nv.udt.entry) |

Additionally, any type in the constant bank range `0x70000064` through `0x7000007E` (26 values) is unconditionally accepted regardless of the bitmask. This covers `SHT_CUDA_CONSTANT0` through `SHT_CUDA_CONSTANT17` plus 8 reserved constant bank slots.

Sections that pass this filter enter the `sub_45E3C0` call that creates output sections and maps input-to-output section indices.

The types explicitly skipped by this filter (`SHT_CUDA_GLOBAL`, `SHT_CUDA_LOCAL`, `SHT_CUDA_SHARED`) are not ignored -- they are handled by separate dedicated code paths in the first symbol-iteration pass and the post-merge layout phases.

### NOBITS Data Suppression Mask (`0x400D`)

Inside `sub_45E3C0`, after a section is reclassified and looked up/created, the function decides whether the section should carry data into the overlap-merge function. The bitmask `0x400D` at line 150-151 controls this:

```c
LOBYTE(v28) = (sh_type != 8);         // start with "has data" = not NOBITS
if ((unsigned int)(sh_type - 0x70000007) <= 0xE)
    v28 &= ~(0x400DuLL >> ((uint8_t)sh_type - 7));
```

`0x400D` = `0100 0000 0000 1101` in binary, indexed by `(low_byte(sh_type) - 7)`:

| Bit | sh_type | Name | In bitmask? | Effect |
|---|---|---|---|---|
| 0 | `0x70000007` | `SHT_CUDA_GLOBAL` | **1** | Data pointer suppressed (NOBITS semantics) |
| 1 | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | 0 | Data pointer preserved (PROGBITS) |
| 2 | `0x70000009` | `SHT_CUDA_LOCAL` | **1** | Data pointer suppressed (NOBITS) |
| 3 | `0x7000000A` | `SHT_CUDA_SHARED` | **1** | Data pointer suppressed (NOBITS) |
| 4-13 | `0x7000000B`-`0x70000014` | (various) | 0 | Data pointer preserved |
| 14 | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | **1** | Data pointer suppressed (NOBITS) |

When a bit is set, the data pointer passed to `merge_overlapping_global` (`sub_432B10`) is forced to NULL. This means the section participates in the overlap-merge algorithm for offset/size tracking but no byte-level data comparison occurs. This is correct because these section types represent uninitialized GPU memory reservations -- their ELF representation has no data payload.

The four NOBITS-semantics types (`GLOBAL`, `LOCAL`, `SHARED`, `SHARED_RESERVED`) all originated from `SHT_NOBITS` (8) input sections. After reclassification, they carry a CUDA-specific type code, but the bitmask preserves their original NOBITS behavior during merge.

## Per-Type Merge Handling

Each section type follows a different merge path through the two passes of `merge_elf`. The following table documents the complete handling for every type:

### Pass 1: Symbol-Level Merge (Symbol Iteration)

The first pass iterates over symbols from the input object and dispatches based on the reclassified section type:

| Reclassified type | Merge handler | Data path | Notes |
|---|---|---|---|
| `SHT_PROGBITS` (1) | `sub_45E3C0` -> `sub_432B10` | Overlap merge with data | Text/code sections; creates output via `find_section_by_name` + `section_create` |
| `SHT_NOBITS` (8, unreclassified) | `sub_45E3C0` -> `sub_432B10` | Overlap merge, no data | Rare: NOBITS sections that didn't match any `.nv.*` prefix |
| `SHT_CUDA_GLOBAL` (0x70000007) | `sub_45E3C0` -> `sub_432B10` | Overlap merge, NULL data | Size reservation only; actual data deferred to pending-merge list |
| `SHT_CUDA_GLOBAL_INIT` (0x70000008) | `sub_45E3C0` -> `sub_432B10` | Overlap merge with data | Initialized global data has a real data pointer |
| `SHT_CUDA_LOCAL` (0x70000009) | `sub_45E3C0` -> `sub_432B10` | Overlap merge, NULL data | Per-entry local: size reservation |
| `SHT_CUDA_SHARED` (0x7000000A) | `sub_45E3C0` -> `sub_432B10` | Overlap merge, NULL data | Per-entry shared memory reservation |
| `SHT_CUDA_SHARED_RESERVED` (0x70000015) | `sub_45E3C0` -> `sub_432B10` | Overlap merge, NULL data | tcgen05/reserved shared memory reservation |
| `SHT_CUDA_CONSTANT0`-`17` (0x70000064-0x75) | `sub_438640` (merge_constant_bank) | Per-entry or global constant merge | Dispatches to per-entry (`sub_434BC0`/`sub_435390`) or global (`sub_4343C0`) overlap merge |
| `SHT_CUDA_CONSTANT` (0x70000006) | Reclassified to CONSTANT_N, then as above | — | Never seen as a final type in pass 1 |
| Weak function (`STB_WEAK`, type matches param bank) | `sub_438640` | Per-entry constant merge | Weak entries use the constant bank path with entry function section index |
| Common symbol (`SHN_COMMON` = 0xFFF2) | `sub_440740` -> pending list at `ctx+448` | Deferred to layout | Common symbols accumulate on the pending-globals list |
| Defined global (`STB_GLOBAL`, section != 0) | `sub_45CD30` -> `sub_440740` | Section creation + symbol mapping | Creates output section, maps symbol, records offset |
| Shared memory with entry (`STB_LOCAL`, `.nv.shared.`) | `sub_437BB0` or `sub_4379A0` | Per-entry shared memory merge | Dispatches based on `*(v19+57)` (extended smem mode flag) |

### Pass 2: Section-Header Merge (Section Iteration)

The second pass iterates over section headers from the input object. Only section types passing the `0x5D05` bitmask filter or in the constant bank range are processed:

| sh_type | Handler | Processing |
|---|---|---|
| `SHT_CUDA_UFT_ENTRY` (0x70000011) | `sub_45CF00` | UFT entry metadata merge; creates output section of type `0x70000011`, passes `is_rela=1` flag |
| `SHT_CUDA_UDT_ENTRY` (0x70000014) | `sub_45CF00` | UDT entry metadata merge; creates output of type `0x70000014`, passes `is_rela=0` flag |
| `SHT_CUDA_CONSTANT0`-`17` (0x70000064-0x7E) | `sub_45E3C0` | Constant bank sections not yet mapped; creates output section and registers mapping |
| `SHT_CUDA_CONSTANT` (0x70000006) | `sub_45E3C0` | Generic constant: reclassified to specific bank via `strtol` |
| `SHT_CUDA_GLOBAL_INIT` (0x70000008) | `sub_45E3C0` | Initialized global data sections not yet mapped |
| `SHT_CUDA_UFT` (0x7000000E) | `sub_45E3C0` | Unified Function Table (.nv.uft) |
| (0x70000010) | `sub_45E3C0` | Bitmask-accepted but no known section type at this value in v13.0 |
| `SHT_CUDA_UDT` (0x70000012) | `sub_45E3C0` | Unified Descriptor Table (.nv.udt) |
| `SHT_CUDA_SHARED` (0x7000000A) | Direct `sub_45E3C0` | Only when extended smem mode active (`*(v19+57)`) AND not in relocatable mode (`!*(ctx+80)`) |
| `SHT_NOTE` (7) | Direct `sub_45E3C0` | Only when link mode is not relocatable AND DCE flag (`*(ctx+101)`) is set; output type forced to `0x70000000` (CUDA_INFO) |

### Pass 3: Post-Section Merge (Special Types)

After both symbol and section passes, three special section types are processed inline in `merge_elf`:

| sh_type | Handler | Processing |
|---|---|---|
| `SHT_CUDA_INFO` (0x70000000) | Inline in `merge_elf` | Per-entry `.nv.info` sections: EIATTR records are parsed, symbol indices remapped, and data nodes copied to per-function info lists at `ctx+480`. Per-entry sections require resolving the entry function's section mapping. Global `.nv.info` (sh_info=0) is processed directly. |
| `SHT_CUDA_CALLGRAPH` (0x70000001) | Inline in `merge_elf` | Callgraph edge table: entries are 8-byte (caller_sym, callee_sym) pairs. Each entry's symbol indices are remapped through the symbol mapping table. Dead (deleted) symbols are skipped. Three edge types are dispatched: type -1 (`sub_44B9F0`), type -2 (`sub_44BA60`), type -3 (`sub_44BAA0`), type -4 (`sub_44BF90`). |
| `SHT_CUDA_CALLGRAPH_INFO` (0x70000002) | Inline in `merge_elf` | Per-function callgraph metadata: symbol indices are remapped and validated. A mismatch between the function's declared info and the current input triggers a fatal error via `sub_467460`. |
| `SHT_CUDA_FUNCDATA` (0x70000004) | Inline in `merge_elf` | Debug function data records: variable-length records containing (sym_index, name_offset, count, data[count*2]). Each record is copied into an arena allocation and appended to the funcdata list at `ctx+480`. Attribute type 3 entries have their name offsets resolved via `sub_43D690`. |
| `SHT_CUDA_COMPAT` (0x70000086) | Inline in `merge_elf` | Compatibility attribute section: a byte stream of (type, data) pairs. Parsed sequentially with a switch on attribute type (0-7). Dispatches to `sub_451920` for ISA class (type 2), address size (type 3), and other compatibility checks. Validates architecture compatibility. Enforces that `.nv.compat` data is present via fatal error if missing. |

### Relocation Section Merge

Relocation sections (`SHT_RELA`=4 and `SHT_REL`=9) are handled through a dedicated path in the second pass. When `merge_elf` encounters a section with `sh_type` 4 or 9, it:

1. Reads the relocation section's `sh_info` field to find the target section it applies to
2. Loads the target section's header to determine the target's type
3. Validates that the target type is in the expected range using the same `0x5D05` bitmask and constant bank range check. If the target type is unexpected, it emits `"unexpected reloc section"`
4. Maps the target section through `sub_45E3C0` if not already mapped
5. For each relocation entry, remaps the symbol index through the input-to-output symbol mapping table
6. Skips relocations targeting deleted (dead) symbols, with special handling for `.debug_line`, `.nv_debug_line_sass`, and `.debug_frame` sections (these are silently skipped for weak symbols already processed)

The relocation merge also detects weak-entry-related relocations targeting `.nv.constant` sections and removes them when the weak entry has been resolved: `"remove weak reloc for %s"`.

### Reserved Shared Memory Symbols

Sections reclassified to `SHT_CUDA_SHARED` with `STB_LOCAL` binding and specific symbol names trigger reserved shared memory tracking:

| Symbol name | SMEM partition type |
|---|---|
| `__nv_reservedSMEM_tcgen05_partition` | 2 (tcgen05) |
| `__nv_reservedSMEM_allocation_phase` | 1 (general) |
| `__nv_reservedSMEM_allocation_mask` | 1 (general) |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` | 1 (general) |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` | 1 (general) |

The partition type is stored at `ctx+664`. If different input objects disagree on the partition type, `merge_elf` emits a fatal error (diagnostic descriptor at `unk_2A5B8C0`). These symbols are Blackwell-era (sm_100+) reserved shared memory regions for tensor core generation 05 operations.

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
| `0x45E3C0` | `section_type_classify` | ~700 B | Reclassify input sh_type by name prefix, create/lookup output section |
| `0x45CF00` | `merge_reloc_section` | ~1,500 B | Merge relocation section (CUDA_RELOCINFO / RELOCINFO_EXT) |
| `0x437BB0` | `merge_shared_entry` | ~700 B | Per-entry shared memory merge (extended smem mode) |
| `0x4379A0` | `merge_shared_local` | ~800 B | Per-entry shared/local memory merge (standard mode) |
| `0x440740` | `add_global_symbol` | ~1,200 B | Create global symbol record, add to pending list or section |
| `0x45CD30` | `create_entry_section` | ~600 B | Create output section for a defined entry-point symbol |
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
| `"unexpected reloc section"` | `sub_45E7D0` | Relocation targets a section type not in valid range |
| `"section not mapped"` | `sub_45E7D0` | `.nv.info` references a section with no output mapping |
| `"duplicate param bank on weak entry %s"` | `sub_45E3C0` | Weak entry's param bank already exists (verbose only) |
| `"weak %s already processed"` | `sub_45E7D0` | Weak entry/section already merged from prior object (verbose only) |
| `"skip mercury section %i"` | `sub_45E7D0` | Mercury section skipped during merge (verbose only) |
| `"remove weak reloc for %s"` | `sub_45E7D0` | Weak relocation to constant section removed (verbose only) |
| `"unknown .nv.compat attribute (%x) encoutered."` | `sub_45E7D0` | Unrecognized compat attribute type (verbose warning) |

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_4411D0` at 0x4411D0 looks up section name at elfw+296 | HIGH | Decompiled: `sub_449A80(*(_QWORD *)(a1 + 296), a2)` returns `*v2` or 0 |
| `sub_442270` at 0x442270 retrieves section by index from elfw+360 | HIGH | Decompiled file exists; virtual mapping check at `ctx+472` confirmed in decompiled `sub_440350` pattern |
| `sub_4325A0` at 0x4325A0 asserts `"section not found"` when `a2` is NULL | HIGH | Decompiled line 26: `if (!a2) sub_467460(&unk_2A5B990, "section not found", ...)` |
| `sub_4325A0` sorts symbol list via `sub_4647D0` with comparator `sub_432440` | HIGH | Decompiled line 30: `sub_4647D0(a2 + 72, sub_432440)` |
| `sub_4325A0` uses natural alignment capped at 8 when explicit alignment is 0 | HIGH | Decompiled lines 63-68: `v24 = 8; if (v23 <= 8) v24 = *(_QWORD *)(v10 + 24);` |
| `sub_4325A0` asserts `"should only reach here with no opt"` via elfw+90 check | HIGH | Decompiled line 76-77: `if (!*(_BYTE *)(a1 + 90))` then `"should only reach here with no opt"` string; string at addr 0x1d38758 in `nvlink_strings.json` |
| `sub_4325A0` emits `"variable %s at offset %d"` when verbose | HIGH | Decompiled line 55: `fprintf(stderr, "variable %s at offset %d\n", ...)` with `(*(_BYTE *)(a1 + 64) & 2)` gate; string at addr 0x1d38739 in `nvlink_strings.json` |
| `sub_4325A0` extended\_smem\_mode flag at elfw+100 | HIGH | Decompiled line 27: `if (!*(_BYTE *)(a1 + 100) \|\| ...)` controls sort bypass |
| `sub_433760` at 0x433760 allocates 40-byte data node | HIGH | Decompiled: `sub_4307C0(v10, 40)` confirmed |
| `sub_433760` updates section size at section+32 | HIGH | Decompiled line 43: `v9[4] = v16 + a5;` (QWORD index 4 = offset 32) |
| `sub_433760` asserts `"tail data node not found"` | HIGH | Decompiled line 53: string literal confirmed in code and at addr 0x1d38839 |
| `sub_45E3C0` at 0x45E3C0 classifies NOBITS by name prefix | HIGH | Decompiled: `memcmp(v18, ".nv.global", 0xA)`, `memcmp(v18, ".nv.shared.", 0xB)`, `memcmp(v18, ".nv.shared.reserved.", 0x14)`, `memcmp(v18, ".nv.local.", 0xA)` with hex types 1879048199/202/213/201 |
| `sub_45E3C0` constant bank: `strtol(name+12, 0, 10) + 1879048292` | HIGH | Decompiled line 102: `strtol(v18 + 12, 0, 10) + 1879048292` (1879048292 = 0x70000064) |
| `sub_45E3C0` PROGBITS `.nv.global.init` -> type 1879048200 (0x70000008) | HIGH | Decompiled line 93-94: `memcmp(v18, ".nv.global.init", 0xF)` then `v13 = 1879048200` |
| `sub_45E3C0` SHT\_NOTE (7) with flags & 0x1000000 early return | HIGH | Decompiled line 105: `if (v13 == 7 && (v49 & 0x1000000) != 0)` |
| Section type filter bitmask `0x5D05` in `sub_45E7D0` | HIGH | Decompiled: `(0x5D05uLL >> ((unsigned __int8)v145 - 6)) & 1` at line 1601 |
| NOBITS data suppression bitmask `0x400D` in `sub_45E3C0` | HIGH | Decompiled line 151: `v28 &= ~(0x400DuLL >> ((unsigned __int8)v13 - 7));` |
| `sub_432B10` at 0x432B10 (`merge_overlapping_global`) 11,683 bytes | HIGH | Decompiled file `sub_432B10_0x432b10.c` exists at correct address |
| `sub_4343C0` at 0x4343C0 (`merge_overlapping_constant`) exists | HIGH | Decompiled file confirms function at address; 7 parameters matching wiki signature |
| `sub_438640` at 0x438640 (`merge_constant_bank`) exists | HIGH | Decompiled file confirms function at address with 10 parameters |
| `"overlapping non-identical data"` string | HIGH | String at addr 0x1d387d8 in `nvlink_strings.json` |
| `"overlapping data spans too much"` string | HIGH | String at addr 0x1d387f8 in `nvlink_strings.json` |
| `"offset %lld goes past section %d size"` string | HIGH | String at addr 0x1d38780 in `nvlink_strings.json` |
| `"adding function section after callgraph completed"` string | HIGH | String at addr 0x1d39ec8 in `nvlink_strings.json` |
| `"entry data cannot be GLOBAL"` string | HIGH | String at addr 0x1d38a9f in `nvlink_strings.json` |
| `"bank SHT not CUDA_CONSTANT_?"` string | HIGH | String at addr 0x1d38950 in `nvlink_strings.json` |
| `"unexpected reloc section"` string | HIGH | String at addr 0x1d3bcd0 in `nvlink_strings.json` |
| `"local data should have offset"` string | HIGH | String at addr 0x1d38ade in `nvlink_strings.json` |
| `"unknown .nv.compat attribute (%x) encoutered"` string | HIGH | String at addr 0x1d3b1b8 in `nvlink_strings.json` (note: "encoutered" typo is in the original binary) |
| `__nv_reservedSMEM_tcgen05_partition` string | HIGH | String at addr 0x1d3bd08 in `nvlink_strings.json` |
| `__nv_reservedSMEM_allocation_phase` string | HIGH | String at addr 0x1d3bd30 in `nvlink_strings.json` |
| Section record 104 bytes with fields at documented offsets | MEDIUM | Offset reads at +32 (size), +48 (addralign), +72 (symbol\_list) confirmed across `sub_4325A0` and `sub_433760`; complete layout reconstructed from multiple functions |
| Data node 40 bytes with data\_ptr at +0, alignment at +16, size at +24, sym\_index at +32 | MEDIUM | Offsets confirmed in `sub_433870` and `sub_4325A0` decompiled code; +16 (alignment), +24 (data\_size), +32 (sym\_index) verified |
| Per-entry section lists at elfw+256/+264/+272/+280 | MEDIUM | Inferred from `sub_438640` and `sub_439830` parameter passing patterns; not all offsets individually verified |
| Five overlap-merge functions follow identical algorithm with different error messages | MEDIUM | Two confirmed (`sub_432B10`, `sub_4343C0`) share same structure; remaining three (`sub_437E20`, `sub_434BC0`, `sub_435390`) inferred from file size similarity and address range |

## Cross-References

- [Merge Phase](../pipeline/merge.md) -- the per-object merge loop that drives section merging
- [Layout Phase](../pipeline/layout.md) -- post-merge address assignment using the section layout engine
- [Symbol Resolution](symbol-resolution.md) -- how global/weak/local symbols are resolved during merge
- [Weak Symbols](weak-symbols.md) -- weak function selection policy (register count, PTX version)
- [Data Layout Optimization](data-layout-opt.md) -- constant deduplication and overlap optimization
- [Dead Code Elimination](dead-code-elimination.md) -- callgraph-based section removal before layout
