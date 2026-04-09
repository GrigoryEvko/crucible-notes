# Finalization Phase

The finalization phase is the last major transformation before ELF serialization. After the relocation engine has patched all instruction and data bytes, `sub_445000` (55,681 bytes / 2,047 lines) performs a complete reindexing of the ELF wrapper's internal data structures -- renumbering symbols, renumbering sections, computing final sizes and offsets, sorting sections into canonical ELF order, and writing the ELF header fields. The result is a fully self-consistent device ELF ready to be serialized to bytes by the output phase.

The timing infrastructure brackets this work with `sub_4279C0("finalize")`. For Mercury targets (sm >= 100), a separate FNLZR post-link pass (`sub_4275C0`) runs after serialization rather than inside this function -- the two are architecturally distinct despite the shared "finalize" naming.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_445000` |
| Size | 55,681 bytes (2,047 decompiled lines) |
| Timing label | `"finalize"` (via `sub_4279C0`) |
| Callees | ~165 distinct functions |
| Called by | `main()` after the relocation phase |
| Signature | `(elfw *elf, a2, a3, a4, elf_class, a6) -> uint64` |
| Internal globals | `byte_2A5F2D8` (verbose flag) |
| Post-phase hook | Verbose stats via `sub_43D2A0` when `--verbose` is set |

## Position in the Pipeline

```
Layout Phase (sub_439830 -- address assignment)
  |
  v
Relocation Phase (sub_469D60 -- R_CUDA patching)
  |
  v
*** Finalization Phase (sub_445000) ***    <-- this page
  |  1. Shared memory fixup (relocatable)
  |  2. Pre-finalize metadata creation
  |     a. Root kernel detection
  |     b. EIATTR serialization (sub_44C030)
  |     c. .nv.callgraph section (sub_44D200)
  |     d. .nv.prototype section (sub_44D9D0)
  |     e. Debug section finalization (sub_4478F0)
  |  3. Symbol reindexing (positive + negative arrays)
  |  4. List replacement (old -> new symbol lists)
  |  5. Section symbol-index fixup (sh_link patching)
  |  6. Supplementary computations
  |     a. Callgraph symbol remap (sub_44CA40)
  |     b. Prototype symbol remap (sub_44CBC0)
  |     c. Entry property computation (sub_451D80)
  |     d. Resolved relocation emission (sub_46ADC0)
  |     e. List finalization (sub_464400)
  |  7. Section allocation and ordering
  |  8. Symbol section-index patching
  |  9. ELF header finalization
  |
  v
FNLZR Post-Link (sub_4275C0, Mercury only)
  |
  v
Output Phase (sub_45BF00 -- ELF serialization)
```

### Concrete Sub-Phase Call Ordering

The following is the exact call order within `sub_445000`, with function addresses for each sub-phase:

| Step | Address | Function | Description |
|---|---|---|---|
| 1 | `0x439640` | `sub_439640` | Shared memory fixup (relocatable `ET_REL` only) |
| 1b | `0x438BD0` | `sub_438BD0` | Virtual section index remap (Mercury `0xFF00` only) |
| 2 | `0x44DB00` | `sub_44DB00` | Pre-finalize: root kernel detect, call sub_44C030 |
| 2a | `0x44C030` | `sub_44C030` | EIATTR serialization -- write `.nv.info` section content |
| 2b | `0x44D200` | `sub_44D200` | Create `.nv.callgraph` section (type `0x70000001`) |
| 2c | `0x44D9D0` | `sub_44D9D0` | Create `.nv.prototype` section (type `0x70000002`) |
| 2d | `0x4478F0` | `sub_4478F0` | Debug section finalization |
| 3a | `0x448C00` | `sub_448C00` | Symbol name hash table re-enumeration |
| 3b | `0x440060` | `sub_440060` | Symbol reindex callback (sequential index assignment) |
| 3c | `0x464DD0` | `sub_464DD0` | Positive/negative symbol array reindex via `sub_442520` |
| 4 | -- | (inline) | Old list destroy + new list install |
| 5a | `0x44CA40` | `sub_44CA40` | Callgraph symbol index remap |
| 5b | `0x44CBC0` | `sub_44CBC0` | Prototype symbol index remap |
| 5c | `0x451D80` | `sub_451D80` | Entry property computation (97,969 bytes -- largest function) |
| 5d | `0x46ADC0` | `sub_46ADC0` | Resolved relocation emission |
| 5e | `0x464400` | `sub_464400` | List structure finalization |
| 6 | -- | (inline) | Section classification into 8 priority buckets |
| 7 | -- | (inline) | Two-pass counting sort + address assignment |
| 8 | -- | (inline) | Symbol `st_shndx` patching via section remap array |
| 9 | -- | (inline) | ELF header geometry (ehsize, shentsize, shoff, flags) |

## Phase 1: Pre-finalization Fixups

### Shared Memory Fixup (Relocatable Mode)

For relocatable links (`elfw+16 == 2`, i.e. `ET_REL`), if certain conditions on the arch flags (`elfw+48`) are not met and `byte elfw+99` is set, `sub_439640` is called to apply a final shared-memory adjustment pass. This handles the case where shared memory layout was deferred because the output is a relocatable object rather than a final executable.

For Mercury ELF type (`elfw+16 == 0xFF00`), the function handles virtual section index remapping. If the elfw has a non-zero section count at `elfw+248`, it validates the virtual-to-physical section index mapping (`elfw+472` and `elfw+368`) and calls `sub_438BD0` on the target section. The "secidx not virtual" assertion fires if the mapping is inconsistent.

### Section Predicate Filtering and Metadata Section Creation

If `byte elfw+81` is not set, `sub_44DB00` (67 lines, 1,473 bytes) is called. This function performs several critical setup operations:

1. **Sets `byte elfw+81 = 1`** (pre-finalize flag) to prevent re-entry.

2. **Root kernel detection** (for `ET_EXEC` with callgraph enabled at `byte elfw+84`): Iterates all callgraph entries at `elfw+408`, testing each symbol for the entry-point predicate (`bit 0x10` in `byte sym+5` AND `sub_43FB20` returning true). If exactly one root kernel is found, stores its symbol index at `elfw+568`. If multiple root kernels exist, sets `elfw+568 = 0`. With verbose output: `"root_kernel = %d"`.

3. **Calls `sub_44C030`** (10,170 bytes): The EIATTR serialization builder that prepares `.nv.info` section content from the collected EIATTR attribute records. This function walks the internal EIATTR record list (`elfw+392`) and serializes each record into the TLV binary format documented in [.nv.info Metadata](../elf/nv-info.md). It handles both global `.nv.info` (for module-scoped attributes) and per-function `.nv.info.<name>` sections (for per-kernel attributes keyed by symbol index via the format-0x04 indexed payload).

4. **For callgraph-enabled ELFs** (`byte elfw+84`):
   - Calls `sub_44D200` (8,545 bytes) to create the `.nv.callgraph` section (type `0x70000001`, alignment 4, entry size 8) via `sub_441AC0`.
   - Calls `sub_44D9D0` (1,577 bytes) to create the `.nv.prototype` section (type `0x70000002`, alignment 4, entry size 8) via `sub_441AC0`. Each prototype entry contains a function symbol index and its signature index.

5. **Debug section finalization**: If verbose bit 0 of `byte elfw+64` is set, calls `sub_4478F0` (15,098 bytes) to finalize debug information sections.

## Phase 2: Symbol Reindexing

The core of finalization begins with symbol table reconstruction. The function rebuilds both the local and global symbol arrays from scratch.

### Positive Symbol Array (elfw+344)

```
// Allocate new index-to-symbol mapping array
new_sec_map = arena_alloc(elfw_arena, 8 * (elfw->sec_count + 1));  // elfw+312
memset(new_sec_map, 0, 8 * (elfw->sec_count + 1));
elfw->sec_index_map = new_sec_map;       // stored at elfw+336
elfw->sec_count = 0;                      // reset counter at elfw+312

// Re-enumerate all sections via callback
list_foreach(elfw->section_array, sub_442400, elfw);  // elfw+360
```

The same pattern repeats for the symbol name hash table (`elfw+288`, strtab count at `elfw+304`). Each symbol is visited via `sub_448C00` (ordered-list iterator) calling `sub_440060` to assign new sequential indices.

The positive and negative symbol arrays at `elfw+344` and `elfw+352` are similarly re-indexed through `sub_464DD0` with callback `sub_442520`.

### Symbol Filtering Loop (Local Symbols)

The first major loop iterates the positive symbol array (`elfw+344`). For each symbol:

1. **Section resolution**: If the symbol's section index is `0xFFFF` (extended index sentinel), the function resolves the actual section through either the extended-section-index list (`elfw+600`) for negative symbol indices, or through the old-to-new symbol mapping tables (`elfw+456` for positive indices, `elfw+464` for negative indices). A "reference to deleted symbol" error fires if the mapping is zero.

2. **Virtual-to-physical section mapping**: If `byte elfw+82` is set (finalized flag -- note this is being set to 1 at the very end of the function), the virtual section index is validated against `elfw+472` and `elfw+368`.

3. **Binding classification**: The symbol's binding field (`byte sym+5 & 0x3`) determines disposition:
   - **Binding 2 (weak)**: Cleared to binding 0 (local). For relocatable output where `elfw+656` is not set, calls `sub_440350` to check if the symbol should be kept.
   - **Binding 1 (global, in local list)**: For Mercury type (`0xFF00`) with type 2 symbols, these may be pruned. Otherwise, if `byte elfw+85` is set and the symbol has a valid value (`sym+8 != -1`), check whether the section has data (`sec+32 != 0`).
   - **Binding 0 (local)**: Standard local symbol, always kept.

4. **Deletion vs. retention**: Symbols that survive filtering are appended to a new ordered list (`v364`) via `sub_464C30`. Dead symbols are removed from the section list via `sub_464D10` and freed via `sub_431000`. The old-to-new index mapping is recorded in `elfw+456`.

5. **Extended symbol table**: If the total symbol count exceeds `0xFEFF` (65,279 -- the ELF `SHN_LORESERVE` threshold), a parallel extended-index list (`v83`) is built to hold the overflow section indices.

### Symbol Filtering Loop (Negative Symbol Array -- elfw+352)

The second major loop processes symbols from the negative symbol array with similar logic but additional checks:

- **Unused section detection**: `sub_4422D0` is called to test whether the symbol's section is marked unused. If so, the symbol is downgraded to binding 1 (local/hidden). When verbose mode is on, this prints `"ignore symbol %s in unused section"`.

- **Undefined globals**: For `__cuda_syscall` symbols (checked via a 14-byte string comparison), undefined references are permitted. For other undefined globals, `sub_449BE0` checks against the allowed-undefined-globals list (`elfw+496`). Violations trigger error `0x2A5BA20` (undefined symbol).

- **Weak-to-local conversion**: Global weak symbols (`binding 2`) in a non-relocatable link are converted to local by clearing the binding bits.

- **Mercury relocatable cleanup**: For `type == 0xFF00` relocatable links, certain global type-2 symbols with type-code `0x20` binding are converted to type 1 (function) if `byte elfw+88` is set.

## Phase 3: List Replacement

After both filtering loops, the old symbol lists are destroyed and replaced:

```c
list_destroy(elfw->neg_symbols);    // elfw+352
elfw->neg_symbols = NULL;
list_destroy(elfw->pos_symbols);    // elfw+344
elfw->pos_symbols = new_list;       // v364
local_sym_count = list_size(new_list);  // v365

// Extended index list replacement (if overflow)
if (extended_list) {
    list_destroy(elfw->ext_symbol_store);    // elfw+600
    elfw->ext_symbol_store = NULL;
    list_destroy(elfw->merged_symbol_array);   // elfw+592
    elfw->merged_symbol_array = extended_list;
}
```

## Phase 4: Section Symbol-Index Fixup

A third loop iterates the section array (`elfw+360`), fixing up the `sh_link` field (at section+44) for relocation sections. The sh_link stores a symbol index (24-bit, with 8-bit flags in the top byte). The function translates old symbol indices to new ones using the DCE remap tables (`elfw+456` for positive indices, `elfw+464` for negative indices), preserving the top 8 flag bits:

```c
for (sec_idx = 1; sec_idx < list_size(elfw->sections); sec_idx++) {
    section = list_get(elfw->sections, sec_idx);
    sh_type = section->type;    // section+4
    if ((sh_type == SHT_PROGBITS || sh_type == SHT_CUDA_NOINIT)
        && (section->flags & SHF_ALLOC)
        && (section->data || section->compressed_data)) {
        old_link = section->sh_link;    // section+44
        top_byte = old_link & 0xFF000000;
        sym_idx = old_link & 0x00FFFFFF;
        new_idx = old_to_new_sym_map[sym_idx];
        section->sh_link = top_byte | (new_idx & 0x00FFFFFF);
    }
}
```

A similar fixup applies to `elfw+568` (a global symbol index stored outside the section list).

## Phase 5: Supplementary Computations

Five callbacks fire in sequence:

1. **`sub_44CA40`** (callgraph_compat_remap, 2,416 bytes): Called if `byte elfw+84` is set. Performs two passes: (a) iterates callgraph entries at `elfw+408`, remapping each symbol index from old to new via the tables at `elfw+456`/`elfw+464`, including linked callee lists; (b) looks up the `.nv.callgraph` section by name (`sub_4411D0(a1, ".nv.callgraph")`), asserts `"callgraph not found"` if missing, then walks the section's linked list remapping caller and callee symbol indices. A sentinel value of -1 or -4 marks callgraph group boundaries, after which callee indices are also remapped.

2. **`sub_44CBC0`** (prototype_symbol_remap, 1,124 bytes): Called if `byte elfw+84` is set. Looks up the `.nv.prototype` section by name (`sub_4411D0(a1, ".nv.prototype")`). If found, walks the linked list at `section+72` and remaps each prototype entry's symbol index from old to new. Falls through to `sub_444720` for extended symbol table resolution when the direct mapping returns zero.

3. **`sub_451D80`** (compute_entry_properties, 97,969 bytes / 3,029 lines): The largest function in the linker. Computes per-kernel-entry properties: register counts, barrier counts, stack sizes, CRS attributes, cache control, max threads, and more. Propagates these through the callgraph to callee functions. This is called here because the final symbol indices must be stable before entry properties can be written into `.nv.info` attributes. See the detailed EIATTR processing section below.

4. **`sub_46ADC0`** (emit_resolved_relocations, 11,515 bytes): Writes the `.nv.resolvedrela` section when `--preserve-relocs` is active. For each relocation in the list at `elfw+376`, resolves the target symbol, validates the relocation offset against section bounds, and writes a resolved relocation entry. Section names are generated as `".nv.resolvedrela" + section_name`. Error strings: `"symbol never allocated"`, `"relocation is past end of offset"`, `"unexpected reloc"`, `"rela section never allocated"`, `"reloc address not found"`.

5. **`sub_464400`** (~1 KB): Called on the elfw object. Finalizes internal list structures and performs a consistency check on the ordered symbol/section lists.

### EIATTR Processing in compute_entry_properties (sub_451D80)

The largest function in the linker operates in five internal phases:

**Phase A -- Symbol index remapping** (lines 800-918): Walks the `.nv.info` attribute list at `elfw+392`. For each EIATTR record, examines the code byte at `record+1`:

- **Codes 0x02, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13, 0x14, 0x17, 0x23, 0x26, 0x2F, 0x3B, 0x45**: Standard indexed attributes. Remaps the 4-byte symbol index in the payload from old to new via `elfw+456` (positive indices) and `elfw+464` (negative indices). If the mapping returns zero and the code is not 0x45 or 0x17, the record is zeroed out (deleted). For `ET_EXEC` output, an additional check using bitmask `0x800800060000` on codes 0x00-0x2F detects function-scoped attributes referencing deleted sections.

- **Code 0x0F (EIATTR_CALLEE_LIST)**: Multi-symbol remapping. The payload contains `(word+2 >> 2)` symbol indices, each remapped individually.

**Phase B -- Per-entry property collection** (lines 1102-1502): Allocates 15+ per-symbol arrays, each indexed by symbol ordinal (`elfw+416 + 1` entries). The EIATTR code switch dispatches to fill these arrays:

| EIATTR Code | Array | Purpose |
|---|---|---|
| `0x04` (CTAIDZ_USED) | `v730` | 3D grid usage per entry |
| `0x0D` | `src` | CRS stack size (with `"conflicting crs_stack attribute"` conflict check) |
| `0x0F` (CALLEE_LIST) | `v720` | Callee function lists per entry |
| `0x11` (FRAME_SIZE) | via `sub_44C7E0` | Per-thread local frame size |
| `0x12` (MIN_STACK) | via `sub_44C7A0` | Minimum stack requirement |
| `0x1B` (MAX_THREADS) | `v735` | Barrier count (2 bytes per entry) |
| `0x1E` (CRS_STACK_SIZE) | `src` | Call-return stack size |
| `0x23` (MAX_STACK_SIZE) | `s` | Maximum stack depth (4 bytes per entry) |
| `0x26` (CACHECTRL) | `v729`/`v728` | L1 cache enable/disable flags |
| `0x2F` (REGCOUNT) | `v731` | Physical register count per thread |
| `0x38` | `v718` | CRS stack overflow attribute |
| `0x3B` | via `sub_44C880` | Stack attribute recording |
| `0x41` (MAXNTID) | `v704` | Maximum threads per CTA |
| `0x4A` (NUM_BARRIERS) | `v706` | Named barrier count (tracks maximum) |
| `0x4C` (NUM_BARRIERS alt) | `v717` | Barrier count for non-root entries (if `byte elfw+101` set) |
| `0x4F` (CALLEE_LIST variant) | `v719` | Callee tracking via secondary set |
| `0x50` | `v721` | Per-entry attribute (increments counter `v734`) |
| `0x51` | `v703` | Version-gated attribute (requires `elfw+200 > 0x81`) |
| `0x52` | `v699` | Version-gated attribute (requires `elfw+200 > 0x81`) |
| `0x54` | `v705` | Per-entry attribute |

**Phase C -- Callgraph propagation** (lines 1523-1972):

```
sub_44CE00(a1, v731)    // Propagate REGCOUNT through callgraph (if byte a1+96)
sub_44C940(a1)          // Finalize callgraph internal state
sub_450C50(a1, sym, s, src)  // Propagate stack/CRS per entry
sub_450ED0(a1, v735, v731, v717, v705)  // Main register count propagation
```

The `sub_450ED0` (propagate_register_counts, 15,956 bytes / 516 lines) has critical logic for barrier-count migration. For the root kernel and each non-root entry:

1. If a section's flags contain a barrier count (bits 20-26) but no `EIATTR_NUM_BARRIERS` record exists, creates a new one with code word `0x4C02` (19458 decimal).
2. Verbose output: `"Creating new EIATTR_NUM_BARRIERS and moving barcount %d from section flags of %s to nvinfo for entry symbol %s"`.
3. Clears the barrier bits from the section flags: `section->sh_flags &= 0xF80FFFFF`.

**Phase D -- Callgraph-driven EIATTR generation** (lines 1972+): For each entry symbol with callees (resolved via `sub_44C740`), propagates `EIATTR_CTAIDZ_USED` (0x04), `EIATTR_MAXNTID` (0x41), `EIATTR_NUM_BARRIERS` (0x4C), and version-gated attributes (0x51, 0x52) to callee functions that lack them. Creates new EIATTR records via `sub_450B70`.

**Phase E -- UFT section handling** (lines 2271-2293): If `elfw+240` is non-zero, looks up `.nv.uft` via `sub_4411B0` (asserting `"nv.uft not found"` on failure), then creates an EIATTR code-0x50 record referencing the UFT section symbol.

### Resource Summary Computation

The `compute_entry_properties` function (`sub_451D80`) computes the following per-kernel resource summary that is ultimately written into `.nv.info` attributes and used by the GPU driver at launch time:

| Resource | EIATTR Code | Computation |
|---|---|---|
| Register count | `0x2F` | Maximum of kernel's own regcount and all callee regcounts (propagated via `sub_44CE00` through callgraph) |
| Barrier count | `0x4A`/`0x4C` | Maximum barrier count across kernel and callees; migrated from section flags bits 20-26 to `.nv.info` attribute |
| Stack size (max) | `0x23` | Sum of frame sizes along deepest call path; propagated by `sub_450C50` |
| CRS stack size | `0x1E` | Call-return stack allocation; accumulated from `0x0D` attributes; `"conflicting crs_stack attribute"` error if multiple conflicting values |
| Local memory | `0x11` | Per-thread local frame size (FRAME_SIZE attribute) |
| Shared memory | (section flags) | Computed during [layout phase](layout.md), not during finalization |
| Max threads/CTA | `0x41` | MAXNTID propagated from entry to callees that lack it |
| Constant bank sizes | (section sizes) | Per-constant-bank sizes read from sections typed `0x70000004`--`0x70000016`; not recomputed here |

The stack size computation follows the callgraph: `sub_450C50` walks each entry's callee list, adding frame sizes along every path, and records the deepest total in `EIATTR_MAX_STACK_SIZE` (0x23). This value is critical for the driver's call-return stack allocation.

### .nv.info Section Population

The `.nv.info` section is populated in two phases:

1. **Pre-finalization** (`sub_44C030`): Serializes the EIATTR record list (`elfw+392`) into binary TLV format. Each record is written as a 4-byte header (format + attr_code + size) followed by the payload. Global attributes go into the module-level `.nv.info` section; per-function attributes go into `.nv.info.<funcname>` sections with `sh_link` pointing to the function's symbol table entry.

2. **During compute_entry_properties** (`sub_451D80`): New EIATTR records are created via `sub_450B70` for computed properties that did not exist in the input. This includes barrier-count migration from section flags, callgraph-propagated register counts, and inherited MAXNTID/CTAIDZ attributes. These records are appended to the existing `.nv.info` attribute list.

The final `.nv.info` content includes both the original EIATTR records from input cubins (remapped to new symbol indices in Phase A) and the synthesized records from callgraph propagation (Phases C-D).

## Phase 6: Section Allocation and Ordering

### Section Header Array

```c
// Validate minimum section count
if (elfw->e_shnum <= 4)
    fatal_error("missing std sections");

// Allocate section-index remap array (old index -> new index)
sec_remap = arena_alloc(arena, 4 * elfw->e_shnum);  // stored at elfw+472
memset(sec_remap, 0, 4 * elfw->e_shnum);

// Identity-initialize: each section maps to itself
for (i = 0; i < elfw->e_shnum; i++)
    sec_remap[i] = i;
```

### Section Classification

Every section beyond the first 4 (null + shstrtab + strtab + symtab) is classified into one of 8 priority buckets based on its type and flags:

| Priority | Criterion | Type |
|---|---|---|
| 7 | No data and no compressed data (empty) | Empty/placeholder |
| 6 | SHT\_NOTE type, or certain CUDA-specific types (0x70000004..0x7000001A, 0x70000006) | Metadata/CUDA note |
| 5 | `SHF_WRITE` flag set | Writable data |
| 4 | `SHF_EXECINSTR` flag set | Executable code |
| 3 | `SHF_ALLOC` flag set | Read-only allocated |
| 2 | SHT\_RELA (4), SHT\_REL (9), SHT\_CUDA\_RESOLVED\_RELA (0x70000003) | Relocation tables |
| 1 | SHT\_PROGBITS (non-empty, no flags above) | Non-allocated data |
| 0 | SHT\_NULL | Null |

A two-pass counting sort assigns sections to their final positions. The first pass counts sections per bucket; prefix sums compute starting indices. The second pass places each section at its bucket position, advancing the bucket pointer. This produces the canonical ELF section ordering: standard header sections first, then text, then read-only data, then writable, then notes, then empties.

Special case: if `qword elfw+264` is non-zero and a section is of type `SHT_CUDA_NOINIT` (0x7000000A) with alignment 16, it is kept in the metadata bucket rather than being pruned as empty.

### Address Assignment

After reordering, sections are assigned final file offsets in a single forward pass:

```c
running_offset = /* after standard headers (ELF hdr + symtab + strtab entries) */;
for (idx = first_user_section; idx < e_shnum; idx++) {
    section = sections[remap[idx]];
    if (section->data || section->compressed_data) {
        // CUDA-specific note types get alignment-based placement
        if (is_cuda_note_type(section->type)) {
            aligned = align_up(running_offset, section->alignment);
            section->sh_offset = aligned;
            running_offset = aligned + section->sh_size;
        } else {
            // Standard section
            section->sh_offset = align_up(running_offset, section->alignment);
            running_offset = section->sh_offset + section->sh_size;
        }
        sec_remap[section->old_index] = ++new_index;
    } else {
        // Empty section: decrement section count
        elfw->e_shnum--;
    }
}
```

For relocatable ELF type (`ET_REL`, class 1), sections of type `SHT_CUDA_NOINIT` or `SHT_CUDA_CALLGRAPH` (0x70000015) may have their size expanded to `alignment + sh_size` to accommodate padding requirements.

### Section Count Overflow

If `e_shnum > 0xFF00` (65,280 -- exceeds `SHN_LORESERVE`), the function enters the ELF extended section numbering path:

```c
if (e_shnum > 0xFF00) {
    if (verbose)
        fprintf(stderr, "overflow number of sections %d\n", e_shnum);
    // Store actual count in section[0].sh_size (ELF standard overflow)
    sections[0]->sh_size = e_shnum;
    elfw->e_shnum_field = 0;  // e_shnum in ELF header set to 0 (sentinel)
}
```

This follows the ELF specification for files with more than 65,279 sections, where `e_shnum` in the header is set to zero and the real count is stored in `sh_size` of section header index 0.

## Phase 7: Symbol Section-Index Patching

With the section remap array built, all symbol records must have their `st_shndx` fields updated from old section indices to new ones. The function iterates the finalized symbol list and for each symbol:

1. Resolves the symbol's section reference through the extended index table or the remap array.
2. Checks for `SHN_XINDEX` overflow: if the new section index exceeds `0xFEFF`, the symbol gets `st_shndx = 0xFFFF` and the actual index goes into the merged symbol array (`elfw+592`) / extended symbol store (`elfw+600`).
3. Handles the special value `0xFFF2` (`SHN_COMMON`) which passes through without remapping when the ELF type is not `ET_EXEC` (type 2).
4. Validates via "reference to deleted section" if a non-zero old index maps to zero in the remap array (indicating the section was pruned).

For Mercury relocatable ELF with type-2 (STT_SECTION) global symbols that were not resolved during linking, the function may downgrade their type from `STT_SECTION | (0x20 << 4)` to plain `STT_FUNC` (type 1).

## Phase 8: Relocation Section Link Fixup

A final loop over all sections updates the `sh_link` and `sh_info` fields of relocation sections (types `SHT_RELA`, `SHT_REL`, `SHT_HASH`, and various CUDA-specific types in the `0x70000000` range):

- For relocation-like sections (types `0x70000004` through `0x7000001A` and `0x70000006`): the `sh_link` field (section+44) is patched from old to new section index via the remap array.
- For standard section types (`SHT_RELA`=4, `SHT_REL`=9, `SHT_CUDA_INFO`=0x70000000, etc.): same sh_link patching.
- For relocatable Mercury output with certain CUDA note types, if the target section was deleted, the note type is downgraded: types in `{0x70000007..0x70000012}` that match a specific bitmask (`0x400D`) become `SHT_NOBITS` (type 8); type `0x70000008` becomes `SHT_PROGBITS` (type 1).

### .nv.compat Section Handling

After the main loop, the function looks up the `.nv.compat` section by name (via `sub_449A80`) in the section name hash table (`elfw+296`). If found, its section index is stored in the program header link field (`section+40`) and the section flags get `SHF_INFO_LINK` (0x40) set. The `.nv.compat` section carries forward-compatibility metadata.

### ELF Flags Encoding

The `e_flags` field in the ELF header (`elfw+48`) is patched with the program header section index, shifted into the top byte. If the index exceeds 0xFE, the top byte is set to 0xFF (indicating overflow, handled via extended section indices).

## Phase 9: ELF Header Finalization

The final step writes the ELF header geometry fields:

### 64-bit ELF (class 2)

```c
elfw->e_ehsize = 64;          // elfw+52
elfw->e_shentsize = 64;       // elfw+58
elfw->e_shoff = align_up(running_offset, 8);  // elfw+40
elfw->e_phentsize = 56;       // elfw+54
```

### 32-bit ELF (class 1)

```c
// Compress section headers from 64-bit internal to 32-bit ELF format
for (i = 0; i < e_shnum; i++) {
    section = sections[remap[i]];
    // Pack: copy 32-bit fields from internal 64-bit layout
    // Uses SSE shuffle (_mm_shuffle_ps with mask 136) to rearrange fields
    section->sh32_offset = section->sh_link;        // 32-bit sh_link
    section->sh32_size   = section->sh64_size;      // truncated to 32-bit
    section->sh32_addr   = (uint32_t)section->sh64_addr;
    // ... similar field compression
}

// Compress symbol entries from Elf64_Sym to Elf32_Sym
for (i = 0; i < sym_count; i++) {
    symbol = symbols[i];
    // Rearrange: 32-bit symbol layout packs st_info/st_other/st_shndx
    // into different offsets than 64-bit
}

elfw->e_ehsize = 52;          // 32-bit ELF header size
elfw->e_shentsize = 40;       // 32-bit section header entry size
elfw->e_flags = e_flags;      // already computed
elfw->e_shnum = e_shnum_field;
elfw->e_shstrndx = shstrndx;
elfw->e_shoff = align_up(running_offset, 4);
elfw->e_phentsize = 32;       // 32-bit program header entry size
```

The SSE shuffle operation (`_mm_shuffle_ps` with immediate 136 = `0b10001000`) is used to efficiently repack section header fields from the internal 64-bit representation to the 32-bit ELF format, moving 4-byte words between slots without temporary variables.

### Finalization Flag

The very last instruction sets `byte elfw+82 = 1`, marking the ELF wrapper as finalized. Subsequent operations (like the output phase) check this flag before allowing modifications. This is the single bit that gates all post-finalization validation -- any function that accesses virtual section indices will assert "secidx not virtual" if this flag is set and the indices are stale.

## Relationship to FNLZR (sub_4275C0)

The FNLZR (Finalizer) at `sub_4275C0` (162 lines, 3,989 bytes) is architecturally unrelated to `sub_445000` despite the naming overlap. FNLZR is a Mercury post-link binary rewriter that runs **after** the ELF has been serialized to a memory buffer:

| Aspect | sub_445000 (finalize) | sub_4275C0 (FNLZR) |
|---|---|---|
| Scope | ELF wrapper data structures | Serialized ELF byte buffer |
| When | Before serialization | After serialization |
| Target | All architectures | Mercury only (sm >= 100) |
| Strings | "secidx not virtual", "reference to deleted symbol" | "FNLZR: Input ELF: %s", "FNLZR: Post-Link Mode" |
| Library | Self-contained | Calls `sub_4748F0` (25-parameter finalization engine) |
| Purpose | Index renumbering, address computation | Binary instruction rewriting, NOP insertion, scheduling fixups |

### FNLZR Internal Protocol

The FNLZR has two distinct modes controlled by the `a5` (post_link_flag) parameter:

**Pre-link mode** (`a5 == 0`): Verbose `"FNLZR: Pre-Link Mode"`. Validates architecture flags in the serialized ELF header -- for CUDA ABI (`ELFOSABI` byte at offset+7 == 0x41`), checks bit 2 of `e_flags`; for non-CUDA ABI, checks bits `0x80004000`. Triggers `"Internal error"` on validation failure.

**Post-link mode** (`a5 == 1`): Verbose `"FNLZR: Post-Link Mode"`. Checks bit `0x80000000` (non-CUDA) or bit 1 (CUDA ABI) in `e_flags`. If `byte_2A5F222` is set, enables destructive rewriting mode. Otherwise, optimization-only mode.

Both modes build a 160-byte configuration struct on the stack encoding:

- Optimization level (4 or 5, determined by `byte_2A5F310`)
- Debug flags from `byte_2A5F224` and `byte_2A5F223`
- Pre/post-link flags
- Verbose mode from `byte_2A5F225`

The struct is passed to `sub_4748F0` (the 25-parameter link-and-finalize entry point). On failure, FNLZR emits `"Internal error"` with the filename. On success: verbose `"FNLZR: Ending %s"`, then calls `sub_43D990` on the processed ELF.

### FNLZR Engine: sub_4748F0 (48,730 bytes / 1,830 lines)

The FNLZR engine is the core Mercury compilation orchestrator. It takes 25 parameters encoding the input ELF, output buffer pointers, target architecture, optimization level, and debug configuration. The function has a multi-phase internal pipeline:

| Phase | Lines | Key Calls | Description |
|---|---|---|---|
| 1. Validation | 426-567 | `sub_43D9A0`, `sub_448360` | Parse input ELF, validate ELF type (`ET_EXEC`/`ET_REL` or `0xFF00`), check architecture flags |
| 2. Memory setup | 568-570 | `sub_432020("Final memory space")` | Allocate 4096-byte arena for output ELF |
| 3. Arch check | 580-600 | `sub_43E610` | Read architecture profile (`v388 > 0x101` = unsupported) |
| 4. Fastpath | 842-880 | `sub_470DA0` | Off-target finalization bypass: if input ELF arch differs from target and is compatible, copy ELF verbatim with patched `e_flags`; prints `"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization"` |
| 5. Compilation unit | 785-960 | `sub_488470`, `sub_4B6F40(656)` | Allocate 656-byte compilation unit descriptor; copy 256-byte arch profile via 16 SSE loads; parse `.note.nv.tkinfo` for tool version validation |
| 6. Per-section setup | 1029-1060 | `sub_1CF07A0`, `sub_1CF1690` | Two loops over input sections: first `sub_1CF07A0` classifies sections, then `sub_1CF1690` processes each section |
| 7. Pre-emit config | 1059 | `sub_1CEF440` | Configure emission parameters from compilation unit state |
| 8. Per-kernel OCG | 1235-1255 | `sub_471700` (per kernel) | For each kernel in `v419[0]`, invoke the per-kernel OCG/ptxas backend to compile SASS |
| 9. ELF emission | 1437-1471 | `sub_1CF2100`/`sub_1CF72E0` (emit), `sub_1CF3720`/`sub_1CF7F30` (write) | Emit output ELF from compiled sections; write serialized bytes |
| 10. Self-check | 1493-1722 | Recursive `sub_4748F0` | If `BYTE5(a20)` is set, invoke self recursively on the output to verify section/symbol consistency |

The section classification at phase 6 assigns type codes to each input section entry (extracted from `v70 = *(v46 + 48)`, the section type field):

| Input type | Output code | Meaning |
|---|---|---|
| 2, 3 | 1 (function) | Executable function sections (`.text.<name>`) |
| 1 | 5 | Data section |
| 8 | 7 | BSS / uninitialized data |
| 9 | 9 | Constant data (type 9) |
| 10 | 8 | Constant data (type 10) |
| 11 | 5 | Merged data (same as type 1) |
| 12 | 2 | Constant bank |
| 13 | 1 | Another constant bank variant |
| 14 | 4 | Shared data |
| other | 6 | Miscellaneous |

### Per-Kernel OCG Orchestrator: sub_471700 (78,516 bytes / 2,541 lines)

The per-kernel compilation function invoked from phase 8 of `sub_4748F0`. For each kernel entry in the finalization work list, `sub_471700` performs:

1. **Validates compilation unit state** (lines 544-554): Checks `a2+248` (arch profile pointer) and `a2+248+88` (OCG backend pointer). Returns error 12 if either is NULL.

2. **Allocates 656-byte compilation unit** (line 562): `sub_4B6F40(656, memspace)` creates a new compilation unit descriptor. Stores vtable pointer from `off_1D49C58`.

3. **Copies 256-byte architecture profile** (lines 617-632): 16 SSE `_mm_loadu_si128` loads copy the architecture profile from the parent compilation state at `v4` (offset from `a2+248`).

4. **Parses compiler key-value options** (lines 644-807): Iterates `a1+38` (option list), comparing each key against known strings using inline byte-by-byte loops:
   - `"deviceDebug"` -- sets byte at `v4+24`
   - `"lineInfo"` -- sets byte at `v4+25`
   - `"optLevel"` -- parsed via `strtol`, sets optimization level at `v4+108` unless already forced
   - `"IsCompute"` -- sets compute flag
   - `"IsPIC"` -- sets position-independent code flag

5. **Tokenizes compiler flags** (lines 835-866): Uses `strtok_r` with space delimiter to split compiler flags from `a1+39`. Each token is validated via `sub_4712A0` and accumulated into a concatenated flag string.

6. **Classifies input sections** (lines 990-1188): Each section entry is assigned a 72-byte descriptor. The section type field (`v70`) is mapped to an internal type code as listed above.

7. **Generates per-kernel output section names** (lines 2220-2316):
   - `.sass_map<funcname>` -- SASS-to-source mapping section
   - `.nv.local.<funcname>` -- per-kernel local memory section (if `v517+112` is non-zero)
   - `.nv.constant<N>.<funcname>` -- per-kernel constant bank section (bank number computed via `sub_1CF72C0(a2, *v40) - 1879048292`, which is `type - 0x70000004`)

8. **Thread-safe string table updates** (lines 2109-2329): Uses `pthread_mutex_lock`/`unlock` on `a2+240` to safely update global string table sizes. Four mutex-protected critical sections handle `.sass_map`, `.nv.local`, and `.nv.constant` section name lengths.

9. **Emits per-kernel EIATTR records** (lines 2340-2467): Generates `.nv.info`-style records for the compiled kernel, including callgraph entries via `sub_46F330` when the compilation unit has debug/line info sections (`.debug_line`).

### Per-Kernel vs. Per-Module Finalization

The finalization system operates at two distinct granularities:

**Per-module** (sub_445000): Operates on the entire merged ELF. Runs exactly once per link invocation. Handles symbol reindexing, section ordering, address assignment, and ELF header writing. All symbol-to-section mappings are global. The section remap array (`elfw+472`) covers all sections in the output. This is the "classical" ELF finalization path that works for all architectures.

**Per-kernel** (sub_471700 called from sub_4748F0): Operates on individual kernel entries during Mercury/LTO compilation. Runs once per kernel function in the compilation work list. Each invocation allocates its own 656-byte compilation unit, copies the architecture profile, parses per-kernel compiler options, classifies sections, generates per-kernel output sections (`.nv.local.<name>`, `.nv.constant<N>.<name>`, `.sass_map<name>`), and invokes the OCG backend. The per-kernel results are collected back into the module-level ELF by the parent `sub_4748F0`.

The interaction path is:

```
main()
  |
  +-- sub_445000 (per-module finalize)
  |     |-- sub_44DB00 (pre-finalize: root kernel, EIATTR serialize)
  |     |-- sub_451D80 (entry property computation + callgraph propagation)
  |     |-- section ordering + address assignment
  |     |-- ELF header write
  |     +-- byte elfw+82 = 1 (finalize flag)
  |
  +-- sub_45BF00 (ELF serialization to bytes)
  |
  +-- sub_4275C0 (FNLZR, Mercury only, post-link)
        |-- sub_4748F0 (FNLZR engine)
              |-- sub_1CEF5B0 (OCG compilation setup)
              |-- sub_1CF07A0 / sub_1CF1690 (section classification)
              |-- sub_1CEF440 (emission config)
              |-- for each kernel:
              |     +-- sub_471700 (per-kernel OCG)
              |           |-- 656-byte compilation unit
              |           |-- arch profile copy (256 bytes, 16 SSE loads)
              |           |-- option parsing (deviceDebug, lineInfo, optLevel, ...)
              |           |-- strtok_r flag tokenization
              |           |-- section type classification
              |           |-- output section name generation
              |           +-- mutex-protected string table updates
              |-- sub_1CF2100 / sub_1CF72E0 (ELF emission)
              +-- sub_1CF3720 / sub_1CF7F30 (ELF write)
```

## Verbose Stats Output (sub_43D2A0)

When `--verbose` is set (`byte_2A5F2D8`), `sub_43D2A0` (5,530 bytes / 213 lines) is called from `main()` after the finalize phase. It asserts `"verbose before final"` if `byte elfw+82 == 0` (finalization not complete).

### Global Summary

```
<N> bytes gmem, <N> bytes cmem[0], <N> bytes cmem[2], ...
```

- **gmem**: Sum of `.nv.global` and `.nv.global.init` section sizes (32-bit `sh_size` for class-1 ELF, 64-bit for class-2)
- **cmem[N]**: Iterates constant bank IDs from `0x70000004` through `0x70000016` (up to 18 banks). Uses a vtable callback at `elfw+488+208` to check bank existence and `sub_43C880` to retrieve bank size.

### Per-Function Properties

```
Function properties for '<name>':
used <N> registers, used <N> barriers, <N> stack, <N> bytes smem, <N> bytes cmem[<N>], <N> bytes lmem
```

Per-function accessors (all take `elfw` and optionally a symbol index):

| Accessor | Property |
|---|---|
| `sub_43CAA0` | Register count |
| `sub_43CBC0` | Barrier count |
| `sub_44C810` | Stack size |
| `sub_43C680` | Shared memory (smem) size |
| `sub_43C780` | Local memory (lmem) size |
| `sub_43CDA0(a1, sym, 0xA)` | Texture count |
| `sub_43CDA0(a1, sym, 0xC)` | Surface count |
| `sub_43CDA0(a1, sym, 0xB)` | Sampler count |

Textures, surfaces, and samplers are only printed if non-zero. The function list is obtained via `sub_432740`, and the assertion `"expected to be finalized"` fires if section lookups are attempted before `byte elfw+82` is set.

## Error Conditions

| Error string | Trigger |
|---|---|
| `"secidx not virtual"` | Virtual-to-physical section index mapping is inconsistent |
| `"reference to deleted symbol"` | Old symbol index maps to zero in the remap table (15+ call sites) |
| `"reference to deleted section"` | Old section index maps to zero in section remap |
| `"section not found"` | Section lookup by index returns NULL |
| `"missing sec strtab"` | Section string table index (`e_shstrndx`, elfw+62) is not 1 |
| `"missing std sections"` | `e_shnum` is 4 or fewer (missing null + shstrtab + strtab + symtab) |
| `"overflow number of sections %d"` | Section count exceeds 0xFF00, entering extended numbering |
| `"unallocated symbol"` | Symbol has no section and `sub_440350` returns true (not a valid external) |
| `"ignore symbol %s in unused section"` | Verbose message when a global symbol's section is marked dead |
| `"unexpected reloc"` / `"reloc address not found"` | From sub_46ADC0 (resolved-relocation emission) |
| `"symbol never allocated"` / `"rela section never allocated"` | From sub_46ADC0 |
| `"relocation is past end of offset"` | From sub_46ADC0 (relocation offset exceeds section bounds) |
| `"expected to be finalized"` | From sub_43D2A0 (verbose stats before finalization complete) |
| `"verbose before final"` | From sub_43D2A0 (verbose stats called before finalize flag is set) |
| `"conflicting crs_stack attribute"` | From sub_451D80 (duplicate CRS stack attribute for same entry) |
| `"callgraph not found"` | From sub_44CA40 (`.nv.callgraph` section missing when expected) |
| `"null root_kernel sym"` | From sub_450ED0 (root kernel symbol index resolves to NULL) |
| `"esym was null, but symidx wasn't"` | From sub_451D80 (symbol ordinal has non-zero index but NULL symbol) |
| `"invalid index"` | From sub_451D80 (EIATTR_REGCOUNT references non-existent symbol) |
| `"nv.uft not found"` | From sub_451D80 (`.nv.uft` section missing when UFT attribute expected) |
| `"FNLZR: Internal error"` | From sub_4275C0 (FNLZR architecture validation failure) |
| `"[Finalizer] fastpath optimization applied"` | From sub_4748F0 (off-target arch copy shortcut) |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_445000` | 55,681 B | finalize_elf | Main finalization entry point |
| `sub_451D80` | 97,969 B | compute_entry_properties | Per-kernel EIATTR collection, register/barrier/stack propagation |
| `sub_4748F0` | 48,730 B | fnlzr_engine | FNLZR 25-parameter compilation engine |
| `sub_471700` | 78,516 B | per_kernel_ocg | Per-kernel OCG/ptxas compilation orchestrator |
| `sub_450ED0` | 15,956 B | propagate_register_counts | Register count + barrier count propagation through callgraph |
| `sub_46ADC0` | 11,515 B | emit_resolved_relocations | `.nv.resolvedrela` section generation |
| `sub_4478F0` | 15,098 B | debug_section_finalize | Debug information section finalization |
| `sub_44C030` | 10,170 B | eiattr_serialization | Serializes EIATTR records to `.nv.info` section content |
| `sub_44D200` | 8,545 B | create_callgraph_section | Creates `.nv.callgraph` section (type `0x70000001`) |
| `sub_43D2A0` | 5,530 B | dump_verbose_stats | `--verbose` per-function register/memory stats |
| `sub_459640` | 16,109 B | reloc_vtable_create | Per-arch relocation handler vtable |
| `sub_4275C0` | 3,989 B | fnlzr_post_link | Mercury FNLZR post-link transform (separate phase) |
| `sub_44CE00` | 3,758 B | propagate_regcount_callgraph | Propagates REGCOUNT through callgraph edges |
| `sub_450C50` | 3,100 B | propagate_stack_per_entry | Per-entry stack/CRS size propagation |
| `sub_44CA40` | 2,416 B | callgraph_compat_remap | `.nv.callgraph` symbol index remapping |
| `sub_439640` | ~2 KB | shared_memory_fixup_reloc | Shared memory fixup for relocatable output |
| `sub_44D9D0` | 1,577 B | create_prototype_section | Creates `.nv.prototype` section (type `0x70000002`) |
| `sub_44DB00` | 1,473 B | pre_finalize_cleanup | Root kernel detection, metadata section creation |
| `sub_44CBC0` | 1,124 B | prototype_symbol_remap | `.nv.prototype` symbol index remapping |
| `sub_450B70` | 1,068 B | create_eiattr_record | Allocates and appends new `.nv.info` EIATTR entry |
| `sub_44C940` | 984 B | finalize_callgraph_state | Consolidates callgraph after EIATTR collection |
| `sub_44B880` | 848 B | is_entry_function | Tests if a symbol is a kernel entry point |
| `sub_464400` | ~1 KB | list_finalize | Internal list structure finalization |
| `sub_442400` | ~1 KB | section_reindex_callback | Section reindexing callback for list iteration |
| `sub_440060` | ~1 KB | symbol_reindex_callback | Symbol reindexing callback for ordered iteration |
| `sub_442520` | ~1 KB | reloc_reindex_callback | Relocation reindexing callback |
| `sub_443500` | ~2 KB | check_section_symbol | Validates section symbols for Mercury type |
| `sub_440350` | ~2 KB | is_unallocated_symbol | Tests whether a symbol is unresolved/unallocated |
| `sub_4422D0` | ~2 KB | is_section_unused | Tests whether a section is marked as dead/unused |
| `sub_444A20` | ~1 KB | is_prunable_name | Tests symbol name against pruning patterns |
| `sub_444AD0` | ~1 KB | is_cuda_builtin_name | Tests symbol name against CUDA builtin patterns |
| `sub_438BB0` | ~0.5 KB | align_up | Aligns an offset to a given power-of-2 boundary |
| `sub_470DA0` | ~2 KB | off_target_check | Off-target finalization compatibility check |
| `sub_1CEF5B0` | -- | ocg_compilation_setup | OCG compilation pipeline initialization |
| `sub_1CF07A0` | -- | section_classify_pass1 | First-pass section classification |
| `sub_1CF1690` | -- | section_classify_pass2 | Second-pass section processing |
| `sub_1CEF440` | -- | emission_config | Emission parameter configuration |
| `sub_1CF2100` | -- | elf_emit | ELF emission (non-debug path) |
| `sub_1CF72E0` | -- | elf_emit_debug | ELF emission (debug path) |
| `sub_1CF3720` | -- | elf_write | ELF serialized write (non-debug) |
| `sub_1CF7F30` | -- | elf_write_debug | ELF serialized write (debug) |
| `sub_44C7E0` | 308 B | record_frame_size | Stores EIATTR_FRAME_SIZE per entry |
| `sub_44C7A0` | 298 B | record_min_stack | Stores EIATTR_MIN_STACK per entry |
| `sub_44C830` | 308 B | record_crs_stack | Stores EIATTR_CRS_STACK_SIZE per entry |
| `sub_44C880` | 1,004 B | record_stack_attr | Stores stack attribute (code 0x3B) per entry |
| `sub_43CAA0` | ~0.5 KB | get_register_count | Returns register count for verbose output |
| `sub_43CBC0` | ~0.5 KB | get_barrier_count | Returns barrier count for verbose output |
| `sub_43C680` | ~0.5 KB | get_smem_size | Returns shared memory size for verbose output |
| `sub_43C780` | ~0.5 KB | get_lmem_size | Returns local memory size for verbose output |
| `sub_43CDA0` | ~0.5 KB | get_resource_count | Returns texture/surface/sampler count by type |

## Cross-References

- [Pipeline Overview](overview.md) -- placement of finalization in the full nvlink pipeline
- [Layout Phase](layout.md) -- the preceding phase that assigns addresses to shared memory, global data, and constant banks; shared memory sizes consumed by verbose stats come from layout
- [Relocation Phase](relocate.md) -- the preceding phase that patches instruction/data bytes; finalization runs after all relocations are applied
- [Output Phase](output.md) -- the succeeding phase that serializes the finalized ELF to bytes
- [Mercury / FNLZR](../mercury/fnlzr.md) -- the separate post-link binary rewriter for sm >= 100; documents `sub_4275C0` dispatcher and `sub_4748F0` engine in full detail
- [ELF Writer Structure](../structs/elf-writer.md) -- the elfw data structure manipulated by this phase; field offsets referenced throughout (elfw+82 finalize flag, elfw+392 EIATTR list, etc.)
- [.nv.info Metadata](../elf/nv-info.md) -- TLV record format, EIATTR attribute catalog (97 codes), and the `sub_451D80` dispatch table; finalization is the primary producer of `.nv.info` content
- [Section Catalog](../reference/section-catalog.md) -- canonical section ordering in CUDA device ELF; the 8-bucket priority classification in Phase 6 produces this ordering
- [Constant Banks](../elf/constant-banks.md) -- per-kernel constant bank sections (`.nv.constant<N>.<name>`) generated by `sub_471700` during Mercury OCG

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_445000` at `0x445000`, 55,681 bytes, 2,047 lines | **HIGH** | `stat -c%s` = 55,681; `wc -l` = 2,047 |
| Signature `(elfw, a2, a3, a4, elf_class, a6) -> uint64` | **HIGH** | Decompiled: `unsigned __int64 __fastcall sub_445000(__int64 a1, __int64 a2, __int64 a3, __int64 a4, int a5, int a6)` |
| ~165 distinct callees | **MEDIUM** | Count inferred from decompiled `sub_` call references; not independently audited |
| Timing label `"finalize"` | **HIGH** | String verified via `main_0x409800.c` and `sub_4279C0` calls |
| Sub-phase call ordering (9 phases) | **HIGH** | Ordering verified against decompiled control flow in `sub_445000`; sub-phase boundaries are editorial grouping following the actual code sequence |
| `sub_451D80` (`compute_entry_properties`), 97,969 B | **HIGH** | `stat -c%s` = 97,969 bytes; largest function in the linker |
| `sub_4748F0` (`fnlzr_engine`), 48,730 B, 1,830 lines | **HIGH** | `wc -l` = 1,830; 25-parameter signature verified in decompiled source |
| `sub_471700` (`per_kernel_ocg`), 78,516 B, 2,541 lines | **HIGH** | `wc -l` = 2,541; `strtok_r` calls at lines 835-837; `.sass_map` at line 2234; `.nv.local.` at line 2263; `.nv.constant` at line 2304 |
| `sub_4275C0` (`fnlzr_post_link`), 3,989 B, 162 lines | **HIGH** | `stat -c%s` = 3,989 bytes; `sub_4748F0` call at line 130 |
| FNLZR pre-link vs post-link modes | **HIGH** | Strings `"FNLZR: Pre-Link Mode"` and `"FNLZR: Post-Link Mode"` at lines 38 and 60 of `sub_4275C0_0x4275c0.c` |
| Fastpath optimization message | **HIGH** | `printf("[Finalizer] fastpath optimization applied for off-target %u -> %u finalization\n")` at line 852 of `sub_4748F0_0x4748f0.c` |
| `sub_4748F0` recursive self-check | **HIGH** | Recursive call at line 1569 of `sub_4748F0_0x4748f0.c`; copies 38 dwords (152 bytes) of config at lines 1497-1507 |
| `sub_471700` per-kernel OCG calls from `sub_4748F0` | **HIGH** | Call at line 1247 of `sub_4748F0_0x4748f0.c` inside `for (k = 0; k < list_size(...); ++k)` loop |
| Mutex-protected string table updates in `sub_471700` | **HIGH** | 4 pairs of `pthread_mutex_lock`/`unlock` on `a2+240` at lines 2109/2164, 2199/2219, 2318/2329, 2423/2467 |
| 656-byte compilation unit allocation | **HIGH** | `sub_4B6F40(656, ...)` at line 562 of `sub_471700` and line 785 of `sub_4748F0` |
| 256-byte arch profile copy via 16 SSE loads | **HIGH** | 16 `_mm_loadu_si128` calls at lines 617-632 of `sub_471700` |
| Compiler option parsing (deviceDebug, lineInfo, optLevel, IsCompute, IsPIC) | **HIGH** | String comparisons at lines 644, 662, 681, 706, 748 of `sub_471700` |
| Section type classification table (8 cases) | **HIGH** | Switch statement at line 1149 of `sub_471700` with cases 1, 8, 9, 10, 11, 12, 13, 14 |
| `.note.nv.tkinfo` version check in `sub_4748F0` | **HIGH** | String `.note.nv.tkinfo` at line 991; comparisons against `"nvlink"` and `"nvJIT API"` at line 1017 |
| Build version string `"cuda_13.0.r13.0/compiler.36424714_0"` | **HIGH** | String at line 1403 of `sub_4748F0` |
| EIATTR dispatch table: 20+ case codes in `sub_451D80` | **HIGH** | Switch statement at line 1122 with cases 0x04 through 0x54 verified in decompiled source |
| `sub_450ED0` barrier count migration | **HIGH** | `"Creating new EIATTR_NUM_BARRIERS..."` string at line 187 of `sub_450ED0`; code word `0x4C02` confirmed |
| `"conflicting crs_stack attribute"` from `sub_451D80` | **HIGH** | String at line 1233 of `sub_451D80_0x451d80.c` |
| All error strings in the error conditions table | **HIGH** | All verified via `nvlink_strings.json` entries |
| Section classification into 8 priority buckets | **MEDIUM** | Inferred from switch/if chains in `sub_445000`; exact bucket boundaries are editorial interpretation |
| `SHN_LORESERVE` threshold at `0xFEFF` (65,279) | **HIGH** | Standard ELF constant; usage visible in decompiled code |
| SSE shuffle `_mm_shuffle_ps` for 32-bit header compression | **MEDIUM** | SSE instructions present in decompiled code; specific shuffle immediate `136 = 0b10001000` inferred from disassembly |
| `byte elfw+82 = 1` finalization flag | **MEDIUM** | Offset inferred from decompiled pointer arithmetic; exact offset may vary |
| Per-module vs per-kernel finalization distinction | **HIGH** | `sub_445000` operates on whole ELF (called once from main); `sub_471700` operates per kernel (called in loop from `sub_4748F0` at line 1247) |
| `sub_470DA0` off-target fastpath check | **HIGH** | Called at line 842 of `sub_4748F0`; followed by fastpath printf at line 852 |
| OCG dispatch functions at `0x1CEF5B0`, `0x1CF07A0`, `0x1CF1690`, `0x1CEF440` | **HIGH** | All four addresses verified as call targets in `sub_4748F0` decompiled source at lines 740, 1038, 1785, 1059 |
| ELF emission dispatch (`sub_1CF2100`/`sub_1CF72E0`) based on debug flag | **HIGH** | Conditional at line 1436-1439 of `sub_4748F0`: `if (byte v350+186) sub_1CF72E0 else sub_1CF2100` |
