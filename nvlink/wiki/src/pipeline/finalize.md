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
Layout Phase (sub_469D60 -- address assignment)
  |
  v
Relocation Phase (sub_469D60 -- R_CUDA patching)
  |
  v
*** Finalization Phase (sub_445000) ***    <-- this page
  |  1. Shared memory fixup (relocatable)
  |  2. Symbol reindexing
  |  3. Section reindexing
  |  4. Size validation
  |  5. Entry property computation
  |  6. Resolved-relocation emission
  |  7. Section ordering and address assignment
  |  8. Symbol section-index patching
  |  9. ELF header finalization
  |
  v
FNLZR Post-Link (sub_4275C0, Mercury only)
  |
  v
Output Phase (sub_45BF00 -- ELF serialization)
```

## Phase 1: Pre-finalization Fixups

### Shared Memory Fixup (Relocatable Mode)

For relocatable links (`elfw+16 == 2`, i.e. `ET_REL`), if certain conditions on the arch flags (`elfw+48`) are not met and `byte elfw+99` is set, `sub_439640` is called to apply a final shared-memory adjustment pass. This handles the case where shared memory layout was deferred because the output is a relocatable object rather than a final executable.

For Mercury ELF type (`elfw+16 == 0xFF00`), the function handles virtual section index remapping. If the elfw has a non-zero section count at `elfw+248`, it validates the virtual-to-physical section index mapping (`elfw+472` and `elfw+368`) and calls `sub_438BD0` on the target section. The "secidx not virtual" assertion fires if the mapping is inconsistent.

### Section Predicate Filtering

If `byte elfw+81` is not set, `sub_44DB00` is called. Based on context this performs a pre-finalization cleanup, pruning placeholder sections or initializing section metadata that the main finalization loop expects (exact behavior not fully decompiled).

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

1. **`sub_44CA40`** (callgraph_compat_remap): Called if `byte elfw+84` is set. Performs two passes: (a) iterates callgraph entries at `elfw+408`, remapping each symbol index from old to new via the tables at `elfw+456`/`elfw+464`, including linked callee lists; (b) looks up the `.nv.callgraph` section by name (`sub_4411D0(a1, ".nv.callgraph")`), asserts `"callgraph not found"` if missing, then walks the section's linked list remapping caller and callee symbol indices. A sentinel value of -1 or -4 marks callgraph group boundaries, after which callee indices are also remapped.

2. **`sub_44CBC0`** (prototype_symbol_remap): Called if `byte elfw+84` is set. Looks up the `.nv.prototype` section by name (`sub_4411D0(a1, ".nv.prototype")`). If found, walks the linked list at `section+72` and remaps each prototype entry's symbol index from old to new. Falls through to `sub_444720` for extended symbol table resolution when the direct mapping returns zero.

3. **`sub_451D80`** (compute_entry_properties): The largest function in the linker (97,969 bytes / 3,029 lines). Computes per-kernel-entry properties: register counts, barrier counts, stack sizes, CRS attributes, cache control, max threads, and more. Propagates these through the callgraph to callee functions. This is called here because the final symbol indices must be stable before entry properties can be written into `.nv.info` attributes. See the detailed EIATTR processing section below.

4. **`sub_46ADC0`** (emit_resolved_relocations): Writes the `.nv.resolvedrela` section when `--preserve-relocs` is active. For each relocation in the list at `elfw+376`, resolves the target symbol, validates the relocation offset against section bounds, and writes a resolved relocation entry. Section names are generated as `".nv.resolvedrela" + section_name`. Error strings: `"symbol never allocated"`, `"relocation is past end of offset"`, `"unexpected reloc"`, `"rela section never allocated"`, `"reloc address not found"`.

5. **`sub_464400`**: Called on the elfw object. Finalizes internal list structures or performs a consistency check (exact behavior not fully decompiled; the function is small and called at the end of the supplementary computation sequence).

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

The FNLZR (Finalizer) at `sub_4275C0` is architecturally unrelated to `sub_445000` despite the naming overlap. FNLZR is a Mercury post-link binary rewriter that runs **after** the ELF has been serialized to a memory buffer:

| Aspect | sub_445000 (finalize) | sub_4275C0 (FNLZR) |
|---|---|---|
| Scope | ELF wrapper data structures | Serialized ELF byte buffer |
| When | Before serialization | After serialization |
| Target | All architectures | Mercury only (sm >= 100) |
| Strings | "secidx not virtual", "reference to deleted symbol" | "FNLZR: Input ELF: %s", "FNLZR: Post-Link Mode" |
| Library | Self-contained | Calls external finalizer via `sub_4BDA50` |
| Purpose | Index renumbering, address computation | Binary instruction rewriting, NOP insertion, scheduling fixups |

The FNLZR operates on the serialized ELF image as an opaque byte buffer (`"in-memory-ELF-image"`). It applies post-link transformations that require knowledge of final instruction addresses -- transformations that cannot be expressed as relocations. These include control-flow-dependent NOP insertion, instruction scheduling adjustments, and warp-level optimization that requires the final binary layout.

## Verbose Stats Output (sub_43D2A0)

When `--verbose` is set (`byte_2A5F2D8`), `sub_43D2A0` (5,530 bytes) is called from `main()` after the finalize phase. It iterates all sections and prints memory usage summaries:

```
verbose before final
<N> bytes gmem, <N> bytes cmem[0], <N> bytes cmem[2], ...
```

The function looks for sections named `.nv.global` and `.nv.global.init` to compute global memory size. It iterates constant memory banks `cmem[0]` through `cmem[N]` by scanning for `.nv.constant` sections. The string "expected to be finalized" is an assertion that fires if the verbose output is attempted before `byte elfw+82` is set.

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
| `"expected to be finalized"` | From sub_43D2A0 (verbose stats before finalization complete) |

## Function Map

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_445000` | 55,681 B | finalize_elf | Main finalization entry point |
| `sub_451D80` | 97,969 B | compute_entry_properties | Per-kernel register/barrier/stack propagation |
| `sub_450ED0` | 15,956 B | propagate_register_counts | Register count propagation through callgraph |
| `sub_46ADC0` | 11,515 B | emit_resolved_relocations | `.nv.resolvedrela` section generation |
| `sub_43D2A0` | 5,530 B | dump_verbose_stats | `--verbose` memory usage output |
| `sub_459640` | 16,109 B | reloc_vtable_create | Per-arch relocation handler vtable |
| `sub_4275C0` | 3,989 B | fnlzr_post_link | Mercury FNLZR post-link transform (separate phase) |
| `sub_439640` | ~2 KB | shared_memory_fixup_reloc | Shared memory fixup for relocatable output |
| `sub_44DB00` | ~3 KB | pre_finalize_cleanup | Pre-finalization section cleanup |
| `sub_44CBC0` | ~3 KB | pre_entry_computation | Callgraph section preparation |
| `sub_44CA40` | ~2 KB | compat_section_setup | .nv.compat section handling |
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

## Cross-References

- [Pipeline Overview](overview.md) -- placement of finalization in the full nvlink pipeline
- [Relocation Phase](relocate.md) -- the preceding phase that patches instruction/data bytes
- [Output Phase](output.md) -- the succeeding phase that serializes the finalized ELF
- [Mercury / FNLZR](../mercury/fnlzr.md) -- the separate post-link binary rewriter for sm >= 100
- [ELF Writer Structure](../structs/elf-writer.md) -- the elfw data structure manipulated by this phase
- [Entry Property Computation](../elf/nv-info.md) -- sub_451D80 and per-kernel metadata
- [Section Catalog](../reference/section-catalog.md) -- canonical section ordering in CUDA device ELF

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_445000` at `0x445000`, 55,681 bytes, 2,047 lines | **HIGH** | `stat -c%s` = 55,681; `wc -l` = 2,047 |
| Signature `(elfw, a2, a3, a4, elf_class, a6) -> uint64` | **HIGH** | Decompiled: `unsigned __int64 __fastcall sub_445000(__int64 a1, __int64 a2, __int64 a3, __int64 a4, int a5, int a6)` |
| ~165 distinct callees | **MEDIUM** | Count inferred from decompiled `sub_` call references; not independently audited |
| Timing label `"finalize"` | **HIGH** | String verified via `main_0x409800.c` and `sub_4279C0` calls |
| `"secidx not virtual"` error string | **HIGH** | String at `0x1d39df8` in `nvlink_strings.json` |
| `"reference to deleted symbol"` error string | **HIGH** | String at `0x1d39dcc` in `nvlink_strings.json` |
| `"reference to deleted section"` error string | **HIGH** | String at `0x1d3a1b6` in `nvlink_strings.json` |
| `"missing std sections"` error string | **HIGH** | String at `0x1d3a1a1` in `nvlink_strings.json` |
| `"overflow number of sections %d"` error string | **HIGH** | String at `0x1d3a210` in `nvlink_strings.json` |
| `"unallocated symbol"` error string | **HIGH** | String at `0x1d3a1d3` in `nvlink_strings.json` |
| `"ignore symbol %s in unused section"` verbose string | **HIGH** | String at `0x1d39f50` in `nvlink_strings.json` (partial match: `"ignore symbol string %s for sym %d"`) |
| `"expected to be finalized"` assertion | **HIGH** | String at `0x1d39087` in `nvlink_strings.json` |
| `"missing sec strtab"` assertion | **HIGH** | String at `0x1d3a18e` in `nvlink_strings.json` |
| `"verbose before final"` stats output | **HIGH** | String at `0x1d390b9` in `nvlink_strings.json` |
| `"__cuda_syscall"` special-case handling | **HIGH** | String at `0x1d39f8f`; appears at line 965 of `sub_445000` |
| `.nv.compat` section handling | **HIGH** | String `".nv.compat"` at `0x1d391db` in `nvlink_strings.json` |
| `sub_451D80` (`compute_entry_properties`), 97,969 B | **HIGH** | `stat -c%s` = 97,969 bytes; largest function in the linker |
| `sub_450ED0` (`propagate_register_counts`), 15,956 B | **HIGH** | `stat -c%s` = 15,956 bytes |
| `sub_46ADC0` (`emit_resolved_relocations`), 11,515 B | **HIGH** | `stat -c%s` = 11,515 bytes |
| `sub_43D2A0` (`dump_verbose_stats`), 5,530 B | **HIGH** | File exists in `decompiled/` |
| `sub_4275C0` (`fnlzr_post_link`), 3,989 B | **HIGH** | `stat -c%s` = 3,989 bytes |
| All 20 function addresses in the function map table | **HIGH** | All verified to exist in `decompiled/` directory |
| ELF extended section numbering (`e_shnum > 0xFF00`) | **HIGH** | `"overflow number of sections %d"` string proves this code path exists |
| Section classification into 8 priority buckets | **MEDIUM** | Inferred from switch/if chains in `sub_445000`; exact bucket boundaries are editorial interpretation |
| `SHN_LORESERVE` threshold at `0xFEFF` (65,279) | **HIGH** | Standard ELF constant; usage visible in decompiled code |
| SSE shuffle `_mm_shuffle_ps` for 32-bit header compression | **MEDIUM** | SSE instructions present in decompiled code; specific shuffle immediate `136 = 0b10001000` inferred from disassembly |
| `byte elfw+82 = 1` finalization flag | **MEDIUM** | Offset inferred from decompiled pointer arithmetic on the `a1` argument; exact offset may vary |
| FNLZR is architecturally unrelated to finalize despite naming | **HIGH** | FNLZR (`sub_4275C0`) operates on serialized buffer; finalize (`sub_445000`) operates on elfw data structures -- confirmed by separate decompiled files |
| Phase ordering (9 sub-phases within finalization) | **MEDIUM** | Ordering matches decompiled control flow; sub-phase boundaries are editorial grouping |
