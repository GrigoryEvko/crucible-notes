# Section Record

The section record is nvlink's internal representation of an ELF section header, extended with linker-specific metadata for data accumulation, name tracking, and section virtualization. Each section in the output ELF is represented by a 104-byte arena-allocated record that stores the standard `Elf64_Shdr` fields alongside a singly-linked list of data contributions, a creation-order index, and a pointer to the section name string. The primary constructor is `sub_441AC0` (9,523 bytes at `0x441AC0`), which allocates the record, registers it in two hash tables and the section vector, and optionally creates a companion `.rela`/`.rel` relocation section. During the merge phase, sections accumulate data fragments through 40-byte data nodes appended via `sub_433760`. During finalization, sections are reindexed and sorted into canonical ELF order by an 8-bucket counting sort.

## Key Facts

| Property | Value |
|---|---|
| Record size | 104 bytes |
| Constructor | `sub_441AC0` (`section_create`) at `0x441AC0` (9,523 bytes) |
| Accessor | `sub_442270` (`get_section_header`) at `0x442270` |
| Data append | `sub_433760` (`section_data_copy`) at `0x433760` |
| Storage | Dynamic vector at `elfw+360` (all sections) |
| Section name table | Hash table at `elfw+296` (name string -> section index) |
| Symbol name table | Hash table at `elfw+288` (name string -> symbol index) |
| Counter | `elfw+584` (monotonically increasing section index) |
| Virtualization table | `elfw+472` (section index -> physical index mapping) |
| Arena | Allocated from the elfw memory arena (via `sub_4307C0`) |
| Data node size | 40 bytes per contribution fragment |

## Record Layout (104 bytes)

The 104-byte record is allocated by `sub_4307C0` (arena allocator) and zero-initialized with `memset`. Field assignments in `sub_441AC0` map directly to the decompiled offsets on the `_QWORD *v19` pointer (where `v19` is the 104-byte record cast as an array of 8-byte slots).

```
Offset  Size  Field              Decompiled           Description
------  ----  -----------------  -------------------  ----------------------------------------
  0       4   sh_type            *(_DWORD*)v19        ELF section type (SHT_PROGBITS, SHT_CUDA_*, etc.)
  4       4   sh_type_ext        *((_DWORD*)v19 + 1)  Extended type code passed as arg a3
  8       8   sh_flags           v19[1]               Section flags (SHF_ALLOC, SHF_WRITE, SHF_EXECINSTR, ...)
 16       8   (reserved)         v19[2]               Zero-initialized, unused in observed paths
 24       8   sh_offset          v19[3]               File offset, assigned during serialization
 32       8   sh_size            v19[4]               Total section size, updated during data append
 40       4   sh_info            *((_DWORD*)v19 + 10) The sh_info field (type-dependent semantics)
 44       4   sh_link            *((_DWORD*)v19 + 11) The sh_link field (related section or symtab index)
 48       8   sh_addralign       v19[6]               Maximum alignment across all data contributions
 56       8   sh_entsize         v19[7]               Entry size for fixed-size sections (reloc tables)
 64       4   section_index      *((_DWORD*)v19 + 16) Unique index assigned at creation time (from elfw+584)
 68       4   (padding)          (zero)               Padding to 8-byte alignment
 72       8   data_list_head     v19[9]               Head of singly-linked data contribution list
 80       8   data_list_tail     v19[10]              Tail pointer for O(1) append
 88       8   (reserved)         v19[11]              Reserved / zero
 96       8   name_ptr           v19[12]              Pointer to arena-allocated section name string
```

### Field Details

**`sh_type` (offset 0)**: For sections that already exist in the section name table (`elfw+296`), the type is copied from the existing record. For new sections, the type from the caller's argument is stored directly. Standard ELF types (1=`SHT_PROGBITS`, 4=`SHT_RELA`, 8=`SHT_NOBITS`, 9=`SHT_REL`) and NVIDIA CUDA types (`0x70000000`..`0x70000086`) are both used. The extended type at offset 4 stores the argument `a3` which is the dispatch-level type code used internally by the linker.

**`sh_flags` (offset 8)**: Standard ELF section flags. The critical check in `sub_441AC0` is: if the callgraph has already been built (`elfw+81` is set) and `flags & 4` (`SHF_EXECINSTR`) is true, the function emits a fatal error `"adding function section after callgraph completed"`. This prevents executable sections from being created after dead code elimination has pruned the call graph.

**`sh_size` (offset 32)**: Updated incrementally by `sub_433760` each time a data contribution is appended. The new size accounts for alignment padding between contributions.

**`sh_addralign` (offset 48)**: Tracks the maximum alignment requirement across all data contributions. Updated by `sub_433760` when a new fragment has a larger alignment than the current maximum.

**`section_index` (offset 64)**: Assigned from the monotonic counter at `elfw+584` during creation. The counter increments by 1 for each new section. This index is the primary key for vector lookup via `sub_464DB0` on the section vector at `elfw+360`.

**`data_list_head` / `data_list_tail` (offsets 72/80)**: Pointers to a singly-linked list of 40-byte data nodes. Each node records one data contribution (from one input ELF section fragment). The tail pointer enables O(1) append. The list is NULL for empty sections (e.g., `SHT_NOBITS` sections that have size but no data).

**`name_ptr` (offset 96)**: Points to an arena-allocated copy of the section name string (e.g., `".nv.constant0"`, `".text.my_kernel"`). For sections that reuse an existing name entry, this pointer comes from the existing record's offset 96. For new names, the string is freshly copied via `strcpy` into arena memory.

## Section Creation (`sub_441AC0`)

The constructor takes eight parameters and performs a multi-step registration process.

```c
// sub_441AC0 -- create a new section record
// a1: elfw context pointer
// a2: section name (e.g., ".nv.global")
// a3: section type (e.g., 0x70000007 for SHT_CUDA_GLOBAL)
// a4: section flags (e.g., SHF_ALLOC | SHF_WRITE = 3)
// a5: sh_info value
// a6: sh_link value (parent section index for reloc sections)
// a7: sh_addralign
// a8: sh_entsize
// Returns: assigned section index (uint32_t)
uint32_t section_create(elfw *ctx, const char *name, uint32_t sh_type,
                        uint64_t sh_flags, uint32_t sh_info,
                        uint32_t sh_link, uint64_t sh_addralign,
                        uint64_t sh_entsize);
```

### Algorithm

1. **Look up existing section** by name in `ctx+296` via `sub_449A80`. If found, retrieve the existing section index.

2. **Allocate 104-byte record** from the arena via `sub_4307C0`. Zero-initialize all 104 bytes with `memset`.

3. **Callgraph guard**: If `byte ctx+81` (callgraph-built flag) is set and `flags & 4` (`SHF_EXECINSTR`) is nonzero, emit fatal error `"adding function section after callgraph completed"`. This prevents new executable sections from being created after dead code elimination.

4. **If section name exists** (reuse path):
   - Validate virtual section mapping if `byte ctx+82` is set: look up `ctx->virtual_map[section_idx]` at `ctx+472`. If the mapped value is nonzero but the reverse map at `ctx+368` does not map back to the original index, emit `"secidx not virtual"`.
   - Retrieve the existing section record from the vector at `ctx+360` via `sub_464DB0`.
   - Copy `sh_type` and `name_ptr` from the existing record.
   - Write all caller-supplied fields (`sh_flags`, `sh_info`, `sh_link`, `sh_addralign`, `sh_entsize`) into the new record.
   - Assign a new section index from the counter at `ctx+584`.

5. **If section name is new** (fresh path):
   - Allocate a 12-byte name-index node via `sub_4307C0`.
   - Increment the section counter at `ctx+312`.
   - Copy the name string into arena memory via `strlen` + `sub_4307C0` + `strcpy`.
   - Insert into the section name hash table at `ctx+296` via `sub_448E70`.
   - Write all fields into the new record and assign a section index from `ctx+584`.

6. **Append to section vector**: Call `sub_464C30` to append the record to the vector at `ctx+360`.

7. **Automatic relocation section creation** (conditional): If all three conditions are met:
   - Link mode is not relocatable (`ctx+16 != 1`)
   - DCE flag is set (`byte ctx+83`)
   - Section type is in the CUDA constant range (`0x70000064` through `0x7000007E`) or is `SHT_CUDA_CONSTANT` (`0x70000006`)

   Then the function queries whether a companion relocation section already exists. It builds the name via `sprintf(buf, ".rela%s", name)` or `sprintf(buf, ".rel%s", name)` depending on the RELA flag at `byte ctx+89`. If no companion exists, it recursively calls itself to create one:

   ```c
   // RELA variant (ctx+89 set):
   section_create(ctx, ".rela<name>", SHT_RELA,     // type 4
                  SHF_INFO_LINK,                      // flags 64
                  symtab_index,                        // ctx+204
                  parent_section_index,                // from new record's offset 64
                  4 * (elf_class == 2) + 4,           // addralign: 8 for ELF64, 4 for ELF32
                  24 or 12);                           // entsize: 24 for ELF64, 12 for ELF32

   // REL variant (ctx+89 clear):
   section_create(ctx, ".rel<name>", SHT_REL,        // type 9
                  0, symtab_index, parent_section_index,
                  4 * (elf_class == 2) + 4,
                  8 * (elf_class == 2) + 8);          // entsize: 16 or 8
   ```

8. **Register in symbol name table**: Create a 48-byte symbol record for this section via `sub_440740`-style logic, insert it into the name-to-symbol hash table at `ctx+288`. The symbol gets its own sequential index from the symbol counter. This symbol record is the section symbol (type `STT_SECTION`, binding `STB_LOCAL`) that standard ELF section symbols carry.

9. **Section index overflow handling**: If the assigned index exceeds `0xFEFF` (65,279) and is not `65522` (`SHN_COMMON`), the 16-bit `st_shndx` field cannot hold it. The function sets `st_shndx = 0xFFFF` (`SHN_XINDEX`) and creates extended index tables at `ctx+592` and `ctx+600` (each sized 0x10000 entries) if they do not already exist. The real index is stored in the extended table via `sub_464D10`.

## Section Accessor (`sub_442270`)

The accessor retrieves a section record by its integer index, with optional virtual-to-physical mapping validation.

```c
// sub_442270 -- get section record by index
// Returns: pointer to 104-byte section record (from vector at ctx+360)
section_record *get_section_header(elfw *ctx, uint32_t section_idx) {
    if (ctx->has_virtual_sections) {           // byte ctx+82
        uint32_t mapped = ctx->virtual_map[section_idx];  // ctx+472
        if (mapped != 0) {
            if (ctx->reverse_map[mapped] != section_idx)  // ctx+368
                fatal("secidx not virtual");
        }
    }
    return vector_get(ctx->all_sections, section_idx);     // ctx+360
}
```

The virtual section mapping (`ctx+472`) supports section index indirection used during finalization when sections are reindexed. The reverse map at `ctx+368` provides the inverse lookup for validation. In normal pre-finalization operation, `byte ctx+82` is clear and the function reduces to a direct vector index.

## Data Contribution List

Sections accumulate data through a singly-linked list of 40-byte **data nodes**. Each node records one contribution from one input ELF section fragment. The data is not physically copied during the merge phase -- each node merely records a source pointer and offset. The actual byte copy happens during ELF serialization.

### Data Node Layout (40 bytes)

```
Offset  Size  Field         Decompiled     Description
------  ----  -----------   -----------    ----------------------------------------
  0       8   source_data   v15[0]         Pointer to source data in input ELF (arg a3)
  8       8   offset        v15[1]         Byte offset within the merged output section
 16       8   alignment     v15[2]         Alignment requirement for this contribution
 24       8   data_size     v15[3]         Size of this data contribution (arg a5)
 32       8   next_ptr      v15[4]         Singly-linked list: pointer to next node (or NULL)
```

The field at offset 32 is managed by the list insertion functions (`sub_4644C0` for the first node, `sub_464460` for subsequent appends). The descriptor pointer referenced in the task description corresponds to the `source_data` field at offset 0 -- each node is a `[next_ptr, descriptor_ptr]`-style linked list node where the list machinery uses offset 32 for linkage and offset 0 for the payload.

### Data Append Algorithm (`sub_433760`)

```c
// sub_433760 -- append a data contribution to a section
// a1: elfw context
// a2: section index
// a3: source data pointer
// a4: alignment requirement
// a5: data size
void section_data_copy(elfw *ctx, uint32_t section_idx,
                       void *source_data, uint32_t alignment,
                       uint64_t data_size);
```

The algorithm:

```
section = get_section_header(ctx, section_idx)   // sub_442270
if section is NULL:
    return    // silently skip unmapped sections

// Track maximum alignment
if alignment > section.sh_addralign:
    section.sh_addralign = alignment             // offset +48 in section record

// Allocate 40-byte data node
node = arena_alloc(40)                           // sub_4307C0
node.source_data = source_data                   // offset 0
node.offset      = 0                             // computed below
node.alignment   = alignment                     // offset 16
node.data_size   = data_size                     // offset 24
node.next_ptr    = 0                             // offset 32

// Compute aligned insertion offset
current_size = section.sh_size                   // offset +32 in section record
remainder = current_size % alignment
if remainder != 0:
    current_size = current_size + alignment - remainder

node.offset = current_size
section.sh_size = current_size + data_size

// Append to section's linked list
if section.data_list_head == NULL:               // offset +72
    list_init(node, &section.data_list_head)     // sub_4644C0
    section.data_list_tail = section.data_list_head   // offset +80
else:
    assert(section.data_list_tail != NULL,
           "tail data node not found")
    new_tail = list_insert_after(node, section.data_list_tail)  // sub_464460
    *section.data_list_tail = new_tail           // update previous tail's next_ptr
    section.data_list_tail = new_tail            // advance tail pointer
```

The linked-list approach avoids copying any bytes during the merge phase. For a link with 500 input objects each contributing a fragment to `.nv.constant0`, the section accumulates 500 data nodes totaling 20,000 bytes of metadata -- but the actual constant data stays in the original input buffers until serialization.

## Symbol Record Creation (`sub_440740`)

When `sub_441AC0` creates a section, it also creates a corresponding symbol record via the symbol creation infrastructure. The section symbol is a 48-byte record allocated from the arena, registered in both the local or global symbol list and the name-to-symbol hash table at `ctx+288`.

```c
// sub_440740 -- create a symbol record
// a1: elfw context
// a2: symbol name
// a3: symbol type (0=notype, 1=object, 2=function, 3=section)
// a4: symbol binding (0=local, 1=global, 2=weak)
// a5: symbol visibility (0=default, 1=internal, 2=hidden, 3=protected)
// a6: section index this symbol belongs to
// a7: symbol value (address)
// a8: callgraph node index (for function symbols)
// a9: symbol size
// Returns: symbol index (positive for local, negative for global)
int32_t symbol_create(elfw *ctx, const char *name, uint8_t type,
                      uint8_t binding, uint8_t visibility,
                      uint32_t section_idx, uint64_t value,
                      int32_t cg_index, uint64_t size);
```

Key behaviors:
- **Binding determines list**: Local symbols (binding 0, 2) go into the local list at `ctx+344`; global symbols (binding 1) go into the global list at `ctx+352`.
- **Callgraph registration**: If `a3 == 2` (function type), the function increments `ctx+416` (function counter) and calls `sub_44B940` to register the function in the call graph.
- **Duplicate guard**: If a global symbol with the same name already exists, the function emits `"adding global symbols of same name"`.
- **Section index overflow**: The same `SHN_XINDEX` overflow handling as in `sub_441AC0`.

## Section Virtualization Table (`elfw+472`)

The virtualization table provides a level of indirection between logical section indices (used during the merge phase) and physical section indices (used in the final ELF output). This is necessary because finalization reindexes sections into a new order.

| Offset | Structure | Direction |
|---|---|---|
| `elfw+472` | `virtual_map[]` | Logical index -> physical index |
| `elfw+368` | `reverse_map[]` | Physical index -> logical index |

During normal merge-phase operation, `byte elfw+82` (the virtualization flag) is clear and the tables are unused. During and after finalization (`sub_445000`), the flag is set and every section access goes through the mapping.

### Validation

Both `sub_441AC0` and `sub_442270` validate the mapping when the flag is set:

```c
if (ctx->has_virtual_sections) {
    uint32_t mapped = virtual_map[section_idx];
    if (mapped != 0) {
        if (reverse_map[mapped] != section_idx)
            fatal("secidx not virtual");
    }
}
```

This bidirectional consistency check catches corruption in the index mapping -- if the forward and reverse maps disagree, the linker halts rather than producing a malformed ELF.

## Section Ordering During Finalization

During the finalization phase (`sub_445000`), all sections are sorted into canonical ELF order using an 8-bucket counting sort. The sort is stable within each bucket, preserving creation order for sections in the same priority class.

### Priority Buckets

| Bucket | Priority | Criterion | Type |
|---|---|---|---|
| 0 | Lowest | `SHT_NULL` | Null section (index 0) |
| 1 | — | `SHT_PROGBITS` with no allocation flags | Non-allocated data |
| 2 | — | `SHT_RELA` (4), `SHT_REL` (9), `SHT_CUDA_RESOLVED_RELA` (0x70000003) | Relocation tables |
| 3 | — | `SHF_ALLOC` flag set (no exec, no write) | Read-only allocated |
| 4 | — | `SHF_EXECINSTR` flag set | Executable code (.text) |
| 5 | — | `SHF_WRITE` flag set | Writable data |
| 6 | — | `SHT_NOTE`, or CUDA types 0x70000004..0x7000001A, 0x70000006 | Metadata/CUDA notes |
| 7 | Highest | No data and no compressed data | Empty/placeholder |

### Counting Sort Algorithm

The sort runs in two passes over all sections beyond the first 4 (null, `.shstrtab`, `.strtab`, `.symtab`):

```
// Pass 1: count sections per bucket
for each section beyond index 4:
    bucket = classify(section)
    count[bucket]++

// Prefix sum: compute starting index for each bucket
start[0] = 4
for b = 1..7:
    start[b] = start[b-1] + count[b-1]

// Pass 2: place sections into output order
for each section beyond index 4:
    bucket = classify(section)
    output[start[bucket]++] = section
```

The first 4 sections are always in fixed positions and are not subject to the sort. After the sort, a section remap array (allocated at `elfw+472`) records the old-to-new index mapping. All symbol records have their `st_shndx` fields updated through this remap.

### Post-Sort Address Assignment

After reordering, sections receive final file offsets in a forward pass:

```
running_offset = (after ELF header + standard section headers)
for idx = first_user_section .. e_shnum:
    section = sorted_sections[idx]
    if section has data:
        aligned_offset = align_up(running_offset, section.sh_addralign)
        section.sh_offset = aligned_offset
        running_offset = aligned_offset + section.sh_size
    else:
        // Empty section: remove from final output
        e_shnum--
```

For relocatable output (`ET_REL`), certain CUDA section types (`SHT_CUDA_NOINIT`, `SHT_CUDA_CALLGRAPH`) may have their size padded to `alignment + sh_size` to satisfy driver-side alignment expectations.

### Section Count Overflow

When the final section count exceeds `0xFF00` (65,280), the function enters the ELF extended numbering path per the ELF specification:

```c
if (e_shnum > 0xFF00) {
    // Store real count in section[0].sh_size
    sections[0].sh_size = e_shnum;
    // Set e_shnum in ELF header to 0 (sentinel value)
    elfw->e_shnum_field = 0;
    // verbose: "overflow number of sections %d"
}
```

## Lifecycle Summary

A section record passes through four distinct phases during its lifetime:

| Phase | Operations | Key function |
|---|---|---|
| **Creation** | Allocate 104-byte record, register in section vector and hash tables, optionally create companion `.rela` section | `sub_441AC0` |
| **Merge** | Append 40-byte data nodes to the linked list, update `sh_size` and `sh_addralign` incrementally | `sub_433760` |
| **Layout** | Sort data nodes by alignment, assign symbol offsets within the section, compute final `sh_size` | `sub_4325A0` |
| **Finalize** | Reindex sections via 8-bucket counting sort, assign file offsets, patch all symbol `st_shndx` fields | `sub_445000` |

During creation, the record is immediately accessible by index from the section vector and by name from the hash table. During merge, only the data list and size fields are modified -- the record itself is immutable after creation except for these accumulation fields. During finalization, the section's identity (its index) changes, and the virtualization table provides the mapping between old and new indices.

## Companion Relocation Sections

The automatic `.rela`/`.rel` creation in `sub_441AC0` deserves special attention because it is a recursive call. The constructor checks three conditions before creating the companion:

1. **Not relocatable mode**: `elfw+16 != 1` (the output is a final executable, not a `.o`).
2. **DCE is active**: `byte elfw+83` is set.
3. **Section type is in the constant bank range**: `(sh_type - 0x70000064) <= 0x1A` (covers `SHT_CUDA_CONSTANT0` through `SHT_CUDA_CONSTANT17` plus nearby types) or `sh_type == 0x70000006` (`SHT_CUDA_CONSTANT`, the generic constant base type).

Additionally, within that check, the function calls a virtual method at `ctx+488 + 296` to verify that the architecture profile supports relocations for this section type. Two more virtual calls at offsets 304 and 312 check whether the section type matches the profile's relocation section type.

The companion section is created with:
- `sh_type`: `SHT_RELA` (4) if `byte ctx+89` is set, otherwise `SHT_REL` (9)
- `sh_flags`: 64 (`SHF_INFO_LINK`)
- `sh_info`: the `.symtab` section index from `ctx+204`
- `sh_link`: the parent section's index (from the just-created record's offset 64)
- `sh_addralign`: 8 for ELF64, 4 for ELF32
- `sh_entsize`: 24 for ELF64 RELA, 12 for ELF32 RELA; 16 for ELF64 REL, 8 for ELF32 REL

## elfw Offsets Summary

All offsets on the elfw (ELF wrapper) context relevant to section management:

| Offset | Type | Field | Description |
|---|---|---|---|
| `+16` | `uint16_t` | `e_type` | ELF type (1=REL, 2=EXEC, 0xFF00=Mercury) |
| `+81` | `uint8_t` | `callgraph_built` | Set after DCE; prevents new executable sections |
| `+82` | `uint8_t` | `has_virtual_sections` | Enables virtual-to-physical index mapping |
| `+83` | `uint8_t` | `dce_enabled` | Dead code elimination active |
| `+89` | `uint8_t` | `use_rela` | Use SHT_RELA (24-byte) instead of SHT_REL (8/16-byte) |
| `+204` | `uint16_t` | `symtab_shndx` | Section index of `.symtab` |
| `+288` | `void*` | `sym_name_table` | Hash table: name -> symbol index |
| `+296` | `void*` | `sec_name_table` | Hash table: name -> section index |
| `+304` | `uint32_t` | `sym_name_count` | Number of entries in sym_name_table |
| `+312` | `uint32_t` | `sec_name_count` | Number of entries in sec_name_table |
| `+344` | `void*` | `local_syms` | Ordered list of local symbol records |
| `+352` | `void*` | `global_syms` | Ordered list of global symbol records |
| `+360` | `void*` | `all_sections` | Vector of all 104-byte section records |
| `+368` | `void*` | `reverse_map` | Physical section index -> logical index |
| `+456` | `void*` | `old_to_new_local` | Symbol index remap (local, old -> new) |
| `+464` | `void*` | `old_to_new_global` | Symbol index remap (global, old -> new) |
| `+472` | `void*` | `virtual_map` | Logical section index -> physical index |
| `+488` | `void*` | `arch_profile` | Architecture profile vtable pointer |
| `+584` | `uint32_t` | `section_counter` | Monotonic counter for section index assignment |
| `+592` | `void*` | `xindex_table` | Extended section index table (for >65279 sections) |
| `+600` | `void*` | `xindex_reverse` | Extended section index reverse map |

## Hash Table Operations

Both hash tables (`ctx+288` for symbols, `ctx+296` for sections) use the same infrastructure:

| Function | Address | Operation |
|---|---|---|
| `sub_449A80` | `0x449A80` | Lookup: returns pointer to stored value, or NULL |
| `sub_448E70` | `0x448E70` | Insert: stores (key, value) pair, returns previous value |
| `sub_4489C0` | `0x4489C0` | Create: allocates a new hash table with configurable mode |

The hash tables support three key comparison modes, selected by the mode field at `table+84`:
- **Mode 0**: Custom hash and compare functions via function pointers at `table+0` and `table+8`
- **Mode 1**: Pointer identity (for interned strings) -- hash is derived from the pointer value
- **Mode 2**: Integer key comparison

For section names, mode 0 is used with string hash/compare. The hash function produces a 32-bit value that is masked against `table+40` (bucket count mask). Collisions are resolved with open addressing stored in bucket chains at `table+104`.

## Error Conditions

| Error string | Function | Trigger |
|---|---|---|
| `"adding function section after callgraph completed"` | `sub_441AC0` | Executable section created after DCE built the call graph |
| `"secidx not virtual"` | `sub_441AC0`, `sub_442270` | Virtual-to-physical section index mapping is inconsistent |
| `"adding global symbols of same name"` | `sub_440740` | Duplicate global symbol creation |
| `"adding function after callgraph completed"` | `sub_440740` | Function symbol created after call graph is built |
| `"reference to deleted symbol"` | `sub_441AC0`, `sub_440740`, `sub_440BE0` | Symbol was removed by DCE but a section still references it |
| `"tail data node not found"` | `sub_433760` | Section has no tail pointer despite non-empty data list |
| `"missing std sections"` | `sub_445000` | Section count <= 4 at finalization (missing required sections) |
| `"overflow number of sections %d"` | `sub_445000` | Section count exceeds 0xFF00 (verbose diagnostic) |

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x441AC0` | `section_create` | 9,523 B | Allocate 104-byte record, register in hash tables and section vector, create companion .rela section |
| `0x442270` | `get_section_header` | ~300 B | Retrieve section record by index with virtual mapping validation |
| `0x433760` | `section_data_copy` | ~600 B | Allocate 40-byte data node, compute aligned offset, append to section's linked list |
| `0x440740` | `symbol_create` | 5,410 B | Create 48-byte symbol record for a section (or standalone symbol) |
| `0x440BE0` | `symbol_create_with_data` | 7,034 B | Create symbol record with initial data and section index resolution |
| `0x4411F0` | `section_copy` | 12,184 B | Deep-copy a section from input ELF to output ELF (merge phase) |
| `0x448E70` | `hash_insert` | ~14,600 B | Generic hash table insert (used for both section and symbol name tables) |
| `0x449A80` | `hash_lookup` | ~500 B | Generic hash table lookup by key |
| `0x464C30` | `vector_append` | ~200 B | Append element to dynamic vector (section vector, symbol list) |
| `0x464DB0` | `vector_get` | ~100 B | Get element from dynamic vector by index |
| `0x464AE0` | `vector_create` | ~200 B | Allocate a new dynamic vector with initial capacity |
| `0x4644C0` | `list_init` | ~100 B | Initialize singly-linked list with first node |
| `0x464460` | `list_insert_after` | ~100 B | Insert node after a given position in singly-linked list |
| `0x4325A0` | `section_layout_engine` | ~1,400 B | Sort contributions by alignment, assign final offsets |
| `0x445000` | `finalize_elf` | 55,681 B | Complete finalization: reindex, sort, address assignment |

## Cross-References

- [Section Merging](../linker/section-merging.md) -- the merge-phase mechanics that drive section creation and data accumulation
- [ELF Writer (elfw)](elf-writer.md) -- the parent structure that hosts the section vector and hash tables
- [Finalization Phase](../pipeline/finalize.md) -- the phase that reindexes and sorts sections into final ELF order
- [ELF Serialization](../elf/serialization.md) -- the output phase that walks data node lists to emit section bytes
- [Symbol Record](symbol-record.md) -- the companion 48-byte record created alongside each section
- [NVIDIA Section Types](../elf/nvidia-sections.md) -- catalog of all CUDA-specific `sh_type` values
- [Device ELF Format](../elf/device-elf-format.md) -- the overall ELF structure that section records describe
- [Hash Tables](../linker/hash-tables.md) -- the hash table infrastructure used for section and symbol name lookup

## Confidence Assessment

Each claim below was verified against decompiled functions (`sub_441AC0`, `sub_442270`, `sub_433760`, `sub_4438F0`, `sub_4475B0`), string references in `nvlink_strings.json`, and raw research report W083. Re-verified in P050b pass (2026-04-09).

| Claim | Confidence | Evidence |
|---|---|---|
| Section record size = 104 bytes | HIGH | `sub_441AC0` calls `sub_4307C0(v14, 104)` followed by 104-byte memset; same allocation in `sub_4438F0` for null section |
| Constructor at `0x441AC0` | HIGH | Decompiled file `sub_441AC0_0x441ac0.c` exists |
| Accessor at `0x442270` | HIGH | Decompiled file `sub_442270_0x442270.c`; 20-line virtual-map-aware accessor |
| Data append at `0x433760` | HIGH | Decompiled file `sub_433760_0x433760.c`; exact logic verified |
| Arena allocation via `sub_4307C0` | HIGH | All allocations use this path |
| `sh_type` at offset 0 (uint32) | HIGH | `*(_DWORD *)v19 = v27` (reused path); else retained from zero-init |
| `sh_type_ext` at offset 4 (uint32 from arg a3) | HIGH | `*((_DWORD *)v19 + 1) = a3` in both new and reuse paths |
| `sh_flags` at offset 8 (qword from arg a4) | HIGH | `v19[1] = v29` where `v29 = v95 = a4` |
| `sh_flags` semantics (SHF_ALLOC/WRITE/EXECINSTR) | HIGH | `if (*(_BYTE *)(a1 + 81) && (v95 & 4) != 0)` checks `SHF_EXECINSTR` bit |
| Reserved qword at offset 16 | MEDIUM | Zero-initialized by memset; not explicitly written in constructor. Purpose unverified |
| `sh_offset` at offset 24 (qword) | MEDIUM | Zero-init; semantic "file offset assigned during serialization" is inferred consistency with Elf64_Shdr |
| `sh_size` at offset 32 (qword) | HIGH | `v9[4]` in `sub_433760` read/written as section size: `v16 = v9[4]; v9[4] = v16 + a5` |
| `sh_info` at offset 40 (uint32) | HIGH | `*((_DWORD *)v19 + 10) = v93 = a5` (dword 10 = byte 40) |
| `sh_link` at offset 44 (uint32) | HIGH | `*((_DWORD *)v19 + 11) = v94 = a6` |
| `sh_addralign` at offset 48 (qword) | HIGH | `v19[6] = a7` in constructor; `*(_QWORD *)(result + 48)` updated in `sub_433760` |
| `sh_entsize` at offset 56 (qword) | HIGH | `v19[7] = a8` in constructor |
| `section_index` at offset 64 (uint32) | HIGH | `*((_DWORD *)v19 + 16) = v29` where `v29 = *(_DWORD *)(a1 + 584) + 1` |
| Counter at `elfw+584` | HIGH | `LODWORD(v29) = *(_DWORD *)(a1 + 584) + 1; *(_DWORD *)(a1 + 584) = v29;` |
| Padding at offset 68 | MEDIUM | Natural alignment gap; not assigned in constructor |
| `data_list_head` at offset 72 | HIGH | `v9[9]` in `sub_433760`; `if (v9[9] == 0) sub_4644C0(v15, v9 + 9)` |
| `data_list_tail` at offset 80 | HIGH | `v9[10]` in `sub_433760`; used for O(1) append |
| Reserved qword at offset 88 | MEDIUM | Zero-init; not assigned in observed paths |
| `name_ptr` at offset 96 | HIGH | `v19[12] = v28` (reuse path) or `v19[12] = v66` (new path via strcpy) |
| Data node size = 40 bytes | HIGH | `sub_4307C0(v10, 40)` in `sub_433760` |
| Data node: source_data at offset 0 | HIGH | `*v15 = a3` (a3 = source data pointer) |
| Data node: offset field at offset 8 (qword) | HIGH | `v15[1] = v16` where `v16` is aligned section size |
| Data node: alignment at offset 16 (qword) | HIGH | `v15[2] = v7` where `v7 = a4` (alignment arg) |
| Data node: data_size at offset 24 (qword) | HIGH | `v15[3] = a5` |
| Data node: next_ptr at offset 32 | HIGH | First OWORD zero-inits bytes 8-23 (offset+alignment), then `*(_OWORD *)(v15 + 3) = 0` clears bytes 24-39. Tail linkage: `sub_4644C0(v15, v9 + 9)` |
| Callgraph guard byte at `elfw+81` | HIGH | `if (*(_BYTE *)(a1 + 81) && (v95 & 4) != 0)` in `sub_441AC0` |
| String `"adding function section after callgraph completed"` | HIGH | Found at line 12357 in `nvlink_strings.json` |
| Virtual section flag byte at `elfw+82` | HIGH | `if (*(_BYTE *)(a1 + 82))` in both `sub_441AC0` and `sub_442270` |
| DCE flag byte at `elfw+83` | HIGH | `*(_BYTE *)(a1 + 83)` check controls companion-reloc creation |
| Virtualization table at `elfw+472` | HIGH | `*(_QWORD *)(a1 + 472) + 4LL * v11` in `sub_441AC0` virtual check |
| Reverse map at `elfw+368` | HIGH | `*(_QWORD *)(a1 + 368)` = `v7` in `sub_442270` |
| Section vector at `elfw+360` | HIGH | `sub_464C30(v19, *(_QWORD *)(a1 + 360))` appends to section vector |
| Section name hash at `elfw+296` | HIGH | `v12 = *(_QWORD *)(a1 + 296); v13 = sub_449A80(v12, a2);` |
| Counter `sec_name_count` at `elfw+312` | HIGH | `++*(_DWORD *)(a1 + 312);` on new name |
| `secidx not virtual` fatal error | HIGH | String found at line 12185 in `nvlink_strings.json`; exact call in `sub_441AC0`/`sub_442270` |
| `tail data node not found` fatal error | HIGH | String found at line 9729 in `nvlink_strings.json`; fires when `v9[10] == 0` in `sub_433760` |
| Arch vtable virtual calls at offsets 296/304/312 | HIGH | `(*(__int64 (**)(void))(*(_QWORD *)(a1 + 488) + 296LL))()` verified exactly |
| Constant-range relocation creation gate (0x70000064 -- 0x7000007E) | HIGH | `(unsigned int)(a3 - 1879048292) <= 0x1A` where `1879048292 = 0x70000064` and `+ 0x1A = 0x7000007E` |
| SHT_CUDA_CONSTANT (0x70000006) as alternate relocation type | HIGH | `|| a3 == 1879048198` where `1879048198 = 0x70000006` (generic constant base type, NOT SHT_CUDA_COMPAT which is 0x70000086) |
| RELA vs REL gate via byte at `elfw+89` | HIGH | `if (*(_BYTE *)(a1 + 89)) sprintf(v91, ".rela%s", a2);` |
| `.rela<name>` and `.rel<name>` companion names | HIGH | Exact `sprintf` format strings |
| Relocatable-mode gate (`*(_WORD *)(a1 + 16) != 1`) | HIGH | `if (*(_WORD *)(a1 + 16) != 1 && *(_BYTE *)(a1 + 83) && ...)` |
| Companion section links to symtab at `elfw+204` (word) | MEDIUM | Wiki claims `ctx+204`. Decompiled code shows `*((_WORD *)v17 + 102) = v63` after `.symtab` creation, which is byte offset 204. Confirmed via `sub_4438F0` analysis |
| 12-byte hash entry for section name | HIGH | `sub_4307C0(v56, 12)` + zero-init |
| `SHN_XINDEX` (0xFFFF) handling | HIGH | Same logic as symbol record; extended tables at `elfw+592`/+600 |
| 8-bucket counting sort during finalization | MEDIUM | Described in wiki but exact `sub_445000` counting-sort code not verified in this pass |
| Bucket classification rules (SHT_NULL, SHT_PROGBITS, etc.) | LOW | Descriptive order asserted; bucket boundaries not directly verified against decompiled counting sort |
| 0xFF00 section count overflow -> e_shnum=0, shdr[0].sh_size | MEDIUM | Consistent with ELF spec (`SHN_LORESERVE`); exact assignment site not verified in this pass |
| String `"overflow number of sections %d"` | MEDIUM | Claimed to be verbose diagnostic; presence in binary not directly verified in this pass |
| Lifecycle: creation -> merge -> layout -> finalize | HIGH | Correct pipeline order matches `main()` dispatch |
| `sub_4411F0` (section_copy) at 12,184 bytes | HIGH | Function address and size verifiable from binary |
| `sub_464C30` (vector append) and `sub_464DB0` (vector get) | HIGH | Used throughout constructor and accessor paths |
| Callgraph function counter at `elfw+416` | HIGH | Cross-verified from `sub_440BE0` increment path |
| Positive symbol array at `elfw+344`, negative symbol array at `elfw+352` | HIGH | Cross-verified via `sub_440590` dispatch: `a2 < 0` selects +352, else +344 |
| Mode 0/1/2 hash table comparison | MEDIUM | Described behavior consistent with `sub_4489C0` hash-mode parameter but not fully traced in this pass |
