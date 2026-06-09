# Mercury Debug Sections

Mercury targets (sm100 and above) carry debug information in a parallel set of `.nv.merc.debug_*` and `.nv.merc.nv_debug_*` ELF sections that mirror the standard DWARF layout but are scoped under the Mercury namespace. These sections travel alongside the Mercury intermediate instruction stream so that FNLZR can update addresses, register assignments, and line mappings in lockstep when it rewrites Mercury IR into final SASS. nvlink emits 15 Mercury debug section variants through two dedicated classifier functions in the ptxas embedded backend, defers them during the merge phase via the `0x10000000` section flag, and validates them through the `--self-check` mechanism after SASS reconstitution.

## Key Facts

| Property | Value |
|---|---|
| Total Mercury debug sections | 15 (11 standard DWARF + 4 NVIDIA-specific) |
| Mercury debug section classifier | `sub_1CED0E0` (`ELF_EmitDebugSections`) at `0x1CED0E0`, 1,755 bytes |
| SASS debug section classifier | `sub_1CED7C0` (`ELF_EmitSASSDebugSections`) at `0x1CED7C0`, 6,757 bytes |
| Relocation processor | `sub_1CF1690` (`ELF_EmitRelocationTable`) at `0x1CF1690`, 2,667 bytes |
| Mercury section flag | `0x10000000` (bit 28 of `sh_flags`, within `SHF_MASKPROC` range) |
| Merge behavior | Skipped when `is_mercury_compatible` is true |
| Self-check error | `"Self check for capsule mercury debug section failed"` at `0x2458F70` |
| Detailed failure | `"Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125'..."` at `0x1F44288` |
| FNLZR prefix match | `".nv.merc."` (9 bytes, trailing dot) at `0x1D40605` |
| String table cluster | `0x245832A`--`0x2458470` (contiguous `.nv.merc.debug_*` / `.nv.merc.nv_debug_*` names) |
| DWARF emitter debug prefix | `".nv_debug_"` (10 bytes) at `0x226B814`, xref in `sub_1672F50` |
| Standard debug prefix | `".debug_"` (7 bytes) at `0x226B81F`, xref in `sub_1672F50` |
| Debug timing diagnostic | `"DebugInfo-time        : %.3f ms (%.2f%%)"` at `0x1EED040` |
| Peak debug memory | `"PeakDebugInfoMemoryUsage        : %.3lf KB"` at `0x1EED160` |

## How Mercury Debug Differs from SASS DWARF

Mercury debug information and standard SASS DWARF serve the same purpose — mapping machine instructions back to source lines and variable locations — but differ in four fundamental ways:

### 1. Address Granularity

Standard DWARF in a pre-sm100 cubin (e.g., sm89) uses final SASS instruction addresses. Every PC range in `.debug_info`, `.debug_line`, and `.debug_loc` refers to byte offsets within the `.text` section.

Mercury DWARF uses Mercury intermediate addresses. These addresses correspond to positions in the `.nv.merc` instruction stream, not the final SASS `.text`. Mercury instructions are a higher-level encoding (fewer instructions, wider semantics, no scheduling constraints) that FNLZR later expands, schedules, and register-allocates to produce SASS. Every address in every Mercury debug section will become stale after FNLZR processes the code.

### 2. Namespace Prefixing

All 15 debug sections carry the `.nv.merc.` prefix, placing them in a distinct namespace from the standard unprefixed debug sections:

| Mercury section | Standard equivalent |
|---|---|
| `.nv.merc.debug_info` | `.debug_info` |
| `.nv.merc.debug_line` | `.debug_line` |
| `.nv.merc.nv_debug_line_sass` | `.nv_debug_line_sass` |
| (etc.) | (etc.) |

This namespacing is not cosmetic. It allows both Mercury and SASS debug sections to coexist in the same ELF when the output format is capmerc (the default for sm100+). The Mercury sections are retained for potential JIT re-finalization by the CUDA driver, while the SASS sections are used by tools like cuda-gdb.

### 3. Section Flag Marking

Every Mercury section (debug and non-debug) carries the `0x10000000` flag in its `sh_flags` field. This flag has no standard ELF meaning (it falls within `SHF_MASKPROC`, `0xF0000000`). The merge phase uses this flag as the sole discriminator to skip Mercury sections. Standard SASS debug sections do not carry this flag.

### 4. Relocation Architecture

Mercury debug sections reference Mercury-internal symbols through the Mercury symbol table (`.nv.merc.symtab_shndx`), not the standard cubin `.symtab`. The relocation processor (`sub_1CF1690`) handles both namespaces: it first tries the unprefixed name (e.g., `.debug_frame`), and if that fails and the `0x10000000` flag is set, falls through to the Mercury-prefixed name (e.g., `.nv.merc.debug_frame`). This dual-path design allows the same relocation function to process both standard and Mercury cubins.

## Section Catalog

### Standard DWARF Mirror Sections (11)

These sections replicate the standard DWARF debug section layout under the `.nv.merc` namespace. Each carries debug information at the Mercury instruction address granularity — addresses that are not yet final and will change after FNLZR performs opex expansion, instruction scheduling, and register assignment.

| Section name | DWARF equivalent | String address | Description |
|---|---|---|---|
| `.nv.merc.debug_abbrev` | `.debug_abbrev` | `0x245832A` | Abbreviation tables mapping codes to tag/attribute pairs |
| `.nv.merc.debug_aranges` | `.debug_aranges` | `0x2458340` | Address range tables for compilation unit lookup |
| `.nv.merc.debug_frame` | `.debug_frame` | `0x2458357` | Call frame information (CFI) for stack unwinding |
| `.nv.merc.debug_info` | `.debug_info` | `0x245836C` | Core DWARF information entries (DIEs) — types, variables, functions |
| `.nv.merc.debug_line` | `.debug_line` | `0x245841D` | Line number program mapping Mercury addresses to source locations |
| `.nv.merc.debug_loc` | `.debug_loc` | `0x2458380` | Location lists describing variable storage across PC ranges |
| `.nv.merc.debug_macinfo` | `.debug_macinfo` | `0x2458393` | Macro information (`#define` / `#undef` records) |
| `.nv.merc.debug_pubnames` | `.debug_pubnames` | `0x24583AA` | Public name accelerator table (global names to DIE offsets) |
| `.nv.merc.debug_pubtypes` | `.debug_pubtypes` | `0x24583C2` | Public type accelerator table (type names to DIE offsets) |
| `.nv.merc.debug_ranges` | `.debug_ranges` | `0x24583DA` | Non-contiguous address range lists for disjoint scopes |
| `.nv.merc.debug_str` | `.debug_str` | `0x24583F0` | Deduplicated string pool referenced via `DW_FORM_strp` |

### NVIDIA-Specific Debug Sections (4)

These sections carry NVIDIA-proprietary debug data with no standard DWARF equivalent. They are classified by `ELF_EmitSASSDebugSections` (`sub_1CED7C0`).

| Section name | String address | Description |
|---|---|---|
| `.nv.merc.nv_debug_ptx_txt` | `0x2458403` | Embedded PTX source text for source-level PTX debugging |
| `.nv.merc.nv_debug_line_sass` | `0x2458431` | SASS-level line mapping (final instruction addresses to source lines) |
| `.nv.merc.nv_debug_info_reg_sass` | `0x2458450` | Per-instruction register liveness for cuda-gdb variable inspection |
| `.nv.merc.nv_debug_info_reg_type` | `0x2458470` | Register type annotations associating data types with physical registers |

The two register debug sections (`.nv_debug_info_reg_sass` and `.nv_debug_info_reg_type`) also appear at a separate string table location (`0x241282C` and `0x2412844`), referenced by the register debug emitters at `sub_181B160` and `sub_181B270` in the embedded ptxas backend.

## Section Detection: `sub_1CED0E0`

The function at `0x1CED0E0` (`ELF_EmitDebugSections`, 9,262 bytes, 373 decompiled lines) classifies whether an ELF section header describes a Mercury debug section. It returns 1 on match, 0 otherwise. The function is called from the ELF section builder during cubin generation.

### Algorithm

The function takes two parameters: a pointer to the ELF context (`a1`, dereferenced for the string table base) and a pointer to a section header record (`a2`). It proceeds through a sequential chain of 15 string comparisons, one per Mercury debug section name:

```c
int64_t ELF_EmitDebugSections(int64_t* elf_ctx, section_header_t* shdr)
{
    // For each candidate section name, check:
    //   1. Is the section type eligible? (sh_type check against CUDA section types)
    //   2. Does the section carry the Mercury flag? (sh_flags & 0x10000000)
    //   3. Does the resolved section name match the expected string?
    // Return 1 on first match, 0 if no match found.
}
```

The detection loop tests sections in this fixed order:

| Order | Section name | First string reference address |
|---|---|---|
| 1 | `.nv.merc.debug_abbrev` | `0x1CED4B1` |
| 2 | `.nv.merc.debug_aranges` | `0x1CED500` |
| 3 | `.nv.merc.debug_frame` | `0x1CED538` |
| 4 | `.nv.merc.debug_info` | `0x1CED560` |
| 5 | `.nv.merc.debug_loc` | `0x1CED589` |
| 6 | `.nv.merc.debug_macinfo` | `0x1CED23B` |
| 7 | `.nv.merc.debug_pubnames` | `0x1CED5CB` |
| 8 | `.nv.merc.debug_pubtypes` | `0x1CED601` |
| 9 | `.nv.merc.debug_ranges` | `0x1CED63E` |
| 10 | `.nv.merc.debug_str` | `0x1CED670` |
| 11 | `.nv.merc.nv_debug_info_reg_sass` | `0x1CED6AD` |
| 12 | `.nv.merc.nv_debug_info_reg_type` | `0x1CED77E` |
| 13 | `.nv.merc.nv_debug_ptx_txt` | `0x1CED70A` |
| 14 | `.nv.merc.debug_line` | `0x1CED74B` |
| 15 | `.nv.merc.nv_debug_line_sass` | last in chain |

### Section Type Guard

Before each string comparison, the function checks the section's `sh_type` field (at `a2 + 4`, i.e., `*(uint32_t*)(a2 + 4)`) against known CUDA section type ranges. The decompiled code reveals two type ranges that qualify as candidates:

```c
// Range 1: CUDA processor-specific types 0x70000006 through 0x70000014
//   1879048198 <= sh_type <= 1879048212
(v4 - 1879048198) <= 0xE

// Range 2: Constant bank types 0x70000064 through 0x7000007E
//   1879048292 <= sh_type <= 1879048318
(v4 - 1879048292) <= 0x1A
```

Within Range 1, the constant `0x5D05` acts as a bitmask selecting specific section types. The expression `(0x5D05 >> (sh_type - 6)) & 1` checks whether the low bits of `sh_type` (after subtracting 6 from the type code's offset within the range) correspond to an allowed type. In binary, `0x5D05 = 0101_1101_0000_0101`, enabling types at bit positions 0, 2, 8, 10, 11, 12, 14. This maps to CUDA section types `SHT_CUDA_CONSTANT` (0x70000006), `SHT_CUDA_GLOBAL_INIT` (0x70000008), `SHT_CUDA_UFT` (0x7000000E), `SHT_CUDA_UFT_ENTRY` (0x70000011), `SHT_CUDA_UDT` (0x70000012), `SHT_CUDA_UDT_ENTRY` (0x70000014), and others.

An additional fast-path uses the constant `23813` (`0x5D05`) as a 64-bit bitmask through `_bittest64`. This is functionally identical to the shift-and-mask check but uses a single bit-test instruction: `_bittest64(&23813, sh_type - 1879048198)`. The decompiled code alternates between these two representations depending on whether the compiler chose a shift or a BT instruction.

A section that passes the type guard must also have the `0x10000000` flag set in its `sh_flags` field (checked via `*(uint64_t*)(a2 + 8) & 0x10000000`) before the name resolution and string comparison proceed.

### String Resolution

Section names are resolved through `sub_448590`, which takes the ELF string table base (from `*a1`) and the section header, returning a pointer to the null-terminated section name. The first comparison (`.nv.merc.debug_abbrev`) uses `memcmp` with a length of 22 bytes; subsequent comparisons use `strcmp`. The `.nv.merc.nv_debug_ptx_txt` comparison uses `sub_44E3A0` (a starts-with predicate) rather than exact string matching, allowing suffixed variants (e.g., `.nv.merc.nv_debug_ptx_txt.2`).

## SASS Debug Classifier: `sub_1CED7C0`

The companion function at `0x1CED7C0` (`ELF_EmitSASSDebugSections`, 6,757 bytes, 315 decompiled lines) is structurally parallel to `sub_1CED0E0` but operates on the **unprefixed** debug section names. It classifies whether a section is a standard debug section (without the `.nv.merc.` prefix) that should be placed into the SASS debug output:

| Order | Section name compared | Match semantics |
|---|---|---|
| 1 | `.debug_abbrev` | `memcmp`, 14 bytes |
| 2 | `.debug_aranges` | `memcmp`, 15 bytes |
| 3 | `.debug_frame` | `memcmp`, 13 bytes |
| 4 | `.debug_info` | `memcmp`, 12 bytes |
| 5 | `.debug_loc` | `memcmp`, 11 bytes |
| 6 | `.debug_macinfo` | `memcmp`, 15 bytes |
| 7 | `.debug_pubnames` | `memcmp`, 16 bytes |
| 8 | `.debug_pubtypes` | `strcmp` |
| 9 | `.debug_ranges` | `strcmp` |
| 10 | `.debug_str` | `strcmp` |
| 11 | `.nv_debug_info_reg_sass` | `strcmp` |
| 12 | `.nv_debug_info_reg_type` | `strcmp` |
| 13 | `.nv_debug_ptx_txt` | `sub_44E3A0` (prefix match) |
| 14 | `.debug_line` | `strcmp` |
| 15 | `.nv_debug_line_sass` | `strcmp` |

Note the deliberate asymmetry: `sub_1CED0E0` tests `.nv.merc.`-prefixed names (Mercury container sections), while `sub_1CED7C0` tests unprefixed names (standard debug sections). During ELF emission, the ptxas backend uses `sub_1CED7C0` to identify which input debug sections should be re-emitted under the `.nv.merc.` namespace, and uses `sub_1CED0E0` to identify existing Mercury debug sections (e.g., during relocation processing or validation).

The two classifiers do **not** check the `0x10000000` flag identically. `sub_1CED0E0` requires the flag to be set (it is looking for Mercury sections). `sub_1CED7C0` does not check the flag at all — it operates on sections that may or may not be Mercury-marked, because it needs to identify standard debug sections for Mercury re-emission.

## DWARF Emitter Debug Detection: `sub_1672F50`

The ptxas embedded backend contains a separate DWARF emitter at `sub_1672F50` (4,321 bytes) that uses two prefix strings for debug section detection during code generation:

| Prefix | Address | Length | Purpose |
|---|---|---|---|
| `".nv_debug_"` | `0x226B814` | 10 bytes | Identifies NVIDIA-proprietary debug sections |
| `".debug_"` | `0x226B81F` | 7 bytes | Identifies standard DWARF debug sections |

This function performs a prefix match against the section name to determine whether a given section requires DWARF emission. It is called during the ptxas compilation pipeline (before the ELF emission phase) to decide which sections receive debug content. The prefix strings at `0x226B814` and `0x226B81F` are in a separate string table cluster from the Mercury debug strings, reflecting their use in the ptxas codegen rather than the nvlink linker.

## Relocation Processing for Mercury Debug Sections

`ELF_EmitRelocationTable` (`sub_1CF1690`, 2,667 bytes) processes relocations for 7 of the 15 Mercury debug sections. The function implements a dual-lookup pattern: it first tests the unprefixed section name, and if that does not match and the `0x10000000` flag is set in `sh_flags`, it falls through to test the Mercury-prefixed name.

### Dual-Lookup Pattern

For each of the 7 relocatable debug sections, the decompiled code follows this structure:

```c
// First attempt: standard name
if (memcmp(section_name, ".debug_frame", 13) == 0) {
    if (!*(byte*)(ctx + 432))     // Mercury compatibility flag
        ctx->debug_frame_reloc = reloc_table;   // offset +72
}
// ... (fall through other sections) ...

// Second attempt: Mercury name (only if 0x10000000 flag set)
if ((sh_flags_byte11 & 0x10) != 0) {
    if (strcmp(section_name, ".nv.merc.debug_frame") == 0) {
        if (*(byte*)(ctx + 432))
            ctx->debug_frame_reloc = reloc_table;
    }
}
```

The byte at `ctx + 432` serves as a Mercury compatibility discriminator. When it is 0, the standard unprefixed path stores the relocation table pointer; when it is non-zero, only the Mercury-prefixed path succeeds. This ensures that relocation table pointers are stored exactly once regardless of whether the input is a standard or Mercury cubin.

### Relocation Table Assignment Offsets

Each matched section stores its relocation table pointer at a specific offset within the FNLZR context structure:

| Debug section | Context offset | Mercury fallback |
|---|---|---|
| `.debug_frame` / `.nv.merc.debug_frame` | `+72` (`v66`) | `0x1CF1949` |
| `.debug_line` / `.nv.merc.debug_line` | `+80` (`v63`) | `0x1CF1A00` |
| `.nv_debug_line_sass` / `.nv.merc.nv_debug_line_sass` | `+88` (`v65`) | `0x1CF1AB8` |
| `.debug_info` / `.nv.merc.debug_info` | `+112` (`v64`) | `0x1CF1B69` |
| `.debug_loc` / `.nv.merc.debug_loc` | `+120` (`v62`) | `0x1CF1C20` |
| `.nv_debug_info_reg_sass` / `.nv.merc.nv_debug_info_reg_sass` | `+96` (`v68`) | `0x1CF2017` |
| `.nv_debug_info_reg_type` / `.nv.merc.nv_debug_info_reg_type` | `+104` (`v67`) | `0x1CF1FD8` |

### Sections Without Relocations (8)

The remaining 8 sections do not carry relocations:

| Section | Reason |
|---|---|
| `.nv.merc.debug_abbrev` | Abbreviation tables contain no address references |
| `.nv.merc.debug_aranges` | Rebuilt from scratch by FNLZR after finalization |
| `.nv.merc.debug_macinfo` | Macro records contain no address references |
| `.nv.merc.debug_pubnames` | Accelerator tables reconstructed by FNLZR |
| `.nv.merc.debug_pubtypes` | Accelerator tables reconstructed by FNLZR |
| `.nv.merc.debug_ranges` | Rebuilt from scratch by FNLZR after finalization |
| `.nv.merc.debug_str` | String pool contains no address references |
| `.nv.merc.nv_debug_ptx_txt` | PTX source text is address-independent |

## Mercury Section Flag: `0x10000000`

All Mercury sections (not just debug) are tagged with bit 28 (`0x10000000`) in their ELF section header `sh_flags` field. This is a custom NVIDIA flag within the processor-specific range `SHF_MASKPROC` (`0xF0000000`). It has no standard ELF equivalent.

The flag serves as the primary discriminator during the merge phase. When `merge_elf` (`sub_45E7D0`) processes input cubins for a Mercury-compatible target, it tests each section's flags:

```c
if (is_mercury_compatible && (section_flags & 0x10000000) != 0) {
    // verbose: "skip mercury section %i"
    continue;
}
```

The verbose message `"skip mercury section %i\n"` is at `0x1D3BCB7`. The string reference appears in two locations within `sub_45E7D0`: at `0x460D4B` (first section iteration, line 1583 of the decompiled file) and at `0x461549` (second section iteration, line 1711).

The Mercury compatibility condition is a conjunction of two flags:
1. The output context flag at `ctx + 48` (set when the output target is sm100+)
2. A flag derived from the input ELF header (set when the input cubin was compiled for a Mercury target)

Both must be true for the skip to activate. If either is false (e.g., linking legacy SASS cubins with Mercury cubins), the Mercury sections are treated as opaque data and merged normally.

### Why Skip During Merge?

Mercury debug sections are skipped during the merge phase because their content will be entirely rewritten by FNLZR:

1. **Address instability**: Mercury instruction addresses change after opex expansion (Mercury opcode to SASS instruction expansion) and scheduling. All debug sections referencing Mercury addresses become stale.
2. **Symbol namespace isolation**: Mercury relocations reference Mercury-internal symbols, not the output ELF symbol table. Merging them would require unnecessary symbol table translation.
3. **Wholesale replacement**: FNLZR replaces Mercury debug sections with SASS-level equivalents. Merging them into the output ELF would be wasted work that FNLZR would immediately discard.

The skipped sections are not lost. They remain in the per-input cubin images held in memory. The FNLZR post-link transformation operates on the complete in-memory ELF and has access to these sections for code rewriting and debug info regeneration.

## Debug Section Merging During Mercury Linking

Mercury linking introduces a distinctive merge strategy for debug sections that differs from the traditional SASS linking approach.

### Traditional SASS Debug Merge (Pre-sm100)

For pre-Mercury targets, debug sections from multiple input cubins are concatenated and their internal offsets are adjusted through relocation processing. `.debug_info` sections from different compilation units are appended in order, `.debug_abbrev` tables are merged with code deduplication, and `.debug_line` programs are concatenated with adjusted file indices. This is standard DWARF link-time processing.

### Mercury Debug Merge

For Mercury targets, the merge phase **skips all 15 Mercury debug sections entirely**. The per-input cubin Mercury debug sections are preserved in-memory but never combined into the output ELF. Instead:

1. **Per-kernel preservation**: Each input cubin retains its own `.nv.merc.debug_*` sections in memory.
2. **FNLZR per-kernel processing**: When FNLZR processes each kernel (through the per-kernel iteration at `sub_471700`, called from `sub_4748F0` line 1247), it reads the Mercury debug sections from the preserved input image.
3. **Debug regeneration**: FNLZR's compilation pipeline regenerates all debug information from scratch at the SASS level. The Mercury-address debug data is used only as input to the address remapping; the output debug sections contain final SASS addresses.
4. **Final emission**: The output ELF writer (`sub_1CF3720` for complete objects, `sub_1CF7F30` for relocatable) emits both standard `.debug_*` sections (for tool consumption) and, if the output is capmerc format, `.nv.merc.debug_*` sections (for JIT re-finalization).

### Non-debug Mercury Sections in the Merge

Two Mercury structural sections are also skipped during merge:
- `.nv.merc.rela` (Mercury relocations, string at `0x2458D00`)
- `.nv.merc.symtab_shndx` (Mercury extended symbol indices, string at `0x2458490`)

The `.nv.merc` container section (the Mercury code itself, string at `0x2458305`) is likewise skipped.

## FNLZR Debug Serialization: Phase 7

Phase 7 of the FNLZR pipeline (lines 1294--1372 of `sub_4748F0` at `0x4748F0`) handles post-compilation serialization of debug information. It is embedded within Phase 6 of the 10-phase FNLZR pipeline and operates on the compilation output structures built by the ptxas backend.

### Phase 7a: `.debug_line` Serialization

```c
if (debug_line_input) {        // v357 = v419[10]
    sub_477480(debug_out, 0);  // Build debug line table (mode=0)
    sub_4783C0(debug_out, 0);  // Serialize debug line program (mode=0)
    result = sub_477510(debug_out, 0);  // Extract serialized section
    debug_line_input[1] = *(qword*)(result + 8);       // data pointer
    *(dword*)(debug_line_input + 16) = *(dword*)(result + 16) + 1;  // size (+1 for NUL)
}
```

`sub_477480` (at `0x477480`, 55 lines) iterates over the compilation units stored in the debug output structure and calls `sub_464D00` to sort/finalize each unit's line table entries. The mode parameter (0 for `.debug_line`, 1 for `.debug_frame`) selects which slot of the debug output structure to process: mode 0 reads from offsets `+16` / `+24`, mode 1 from offsets `+40` / `+48`.

`sub_4783C0` (at `0x4783C0`, ~200 lines) is the actual DWARF line number program serializer. It walks the sorted line table entries and emits the standard DWARF line number program opcodes (special opcodes, extended opcodes, `DW_LNS_copy`, etc.) into a buffer. The output is a complete `.debug_line` section ready for inclusion in the output ELF.

`sub_477510` (at `0x477510`, 14 lines) is a trivial accessor that returns a pointer to the serialized data. Mode 0 returns offset `+56` of the debug output structure; mode 1 returns offset `+80`.

The `+1` adjustment on the serialized size accounts for the NUL terminator byte that the serializer does not include in its reported size.

### Phase 7b: `.debug_frame` Serialization

```c
if (debug_frame_input) {        // v358 = v419[11]
    sub_477480(debug_out, 1);   // Build debug frame table (mode=1)
    sub_4783C0(debug_out, 1);   // Serialize debug frame program (mode=1)
    result = sub_477510(debug_out, 1);
    debug_frame_input[1] = *(qword*)(result + 8);
    *(dword*)(debug_frame_input + 16) = *(dword*)(result + 16) + 1;
}
```

The `.debug_frame` path is structurally identical to `.debug_line`, differing only in the mode parameter. The same three functions are called with mode=1, accessing the frame-specific slots in the debug output structure.

### Phase 7c: Debug Address Remapping

```c
if (v419[14] && v419[14][4]) {     // relocation entries exist
    sub_4826F0(&bst_root, debug_out, 0);  // Build BST from address map
    for (idx = 0; idx < section_count; idx++) {
        entry = get_entry(v419[14][4], idx);
        if (entry->reloc_type == 0x10008) {     // R_CUDA_ABS32_HI_20
            sym_idx  = entry->sym_index;        // at entry + 28
            symtab   = find_section(elf_data, ".symtab");
            sym_name = resolve_symbol(elf_data, symtab, sym_idx);
            if (strncmp(sym_name, ".debug_line", 12) == 0) {
                // Look up original offset in BST and patch entry
                original_offset = *(dword*)(entry + 8);
                node = bst_root;
                while (node) {
                    if (original_offset < node->key)
                        node = node->left;
                    else if (original_offset > node->key)
                        node = node->right;
                    else {
                        *(qword*)(entry + 8) = node->value;
                        break;
                    }
                }
            }
        }
    }
    sub_4747E0(&bst_root);   // destroy BST
    sub_474760(&bst_aux);    // destroy auxiliary data
}
```

`sub_4826F0` (at `0x4826F0`, ~90 lines) builds a binary search tree (BST) that maps original Mercury-address `.debug_line` section offsets to their new positions in the recompiled output. The relocation type `0x10008` (65,544 decimal) is `R_CUDA_ABS32_HI_20` — the high 20 bits of a 32-bit absolute relocation used for debug section cross-references. The BST lookup patches each relocation entry's offset to reflect the SASS-level address.

## How `-g` Affects Mercury Debug Output

The `-g` flag (`--device-debug`) propagates through the pipeline and controls Mercury debug section generation at multiple levels:

### Flag Propagation

| Level | Flag | Effect |
|---|---|---|
| nvlink CLI | `byte_2A5F310` at `0x2A5F310` | Master switch; auto-enables `verbose-tkinfo` |
| cicc LTO | `-g` forwarded by `sub_426CD0` | Full DWARF metadata generation in IR |
| ptxas embedded | `--device-debug` forwarded by `sub_429BA0` | Full DWARF + NVIDIA extensions in SASS compilation |
| FNLZR config | `v28[3]` bits 32..39 set to 1 | Optimization level set to 5 (debug), debug sections populated |

### Section Population by Debug Level

| Section | `-g` | `--generate-line-info` | Neither |
|---|---|---|---|
| `.nv.merc.debug_info` | populated | empty | absent |
| `.nv.merc.debug_abbrev` | populated | empty | absent |
| `.nv.merc.debug_line` | populated | populated | absent |
| `.nv.merc.debug_frame` | populated | empty | absent |
| `.nv.merc.debug_loc` | populated | empty | absent |
| `.nv.merc.debug_str` | populated | minimal | absent |
| `.nv.merc.debug_ranges` | populated | empty | absent |
| `.nv.merc.debug_aranges` | populated | empty | absent |
| `.nv.merc.debug_pubnames` | populated | empty | absent |
| `.nv.merc.debug_pubtypes` | populated | empty | absent |
| `.nv.merc.debug_macinfo` | populated | empty | absent |
| `.nv.merc.nv_debug_ptx_txt` | populated | empty | absent |
| `.nv.merc.nv_debug_line_sass` | populated | populated | absent |
| `.nv.merc.nv_debug_info_reg_sass` | populated | empty | absent |
| `.nv.merc.nv_debug_info_reg_type` | populated | empty | absent |

The `--generate-line-info` mode populates only the two line-mapping sections (`.debug_line` and `.nv_debug_line_sass`), which is sufficient for source-level profiling tools like Nsight Compute but not for interactive debugging. The `-g` mode populates all 15 sections for full cuda-gdb support.

### The `--suppress-debug-info` Override

When `--suppress-debug-info` is present alongside `-g`, `byte_2A5F310` is cleared to 0 **before** any compilation begins (`sub_427AE0` line 1084). This is a pre-generation suppression: the embedded ptxas never receives the `--device-debug` flag, so no debug sections are generated at all. The Mercury debug classifier functions still exist in the output path but never encounter Mercury debug sections because none were emitted.

### Architecture-Derived Flags

Three architecture-derived flags control Mercury debug emission:

| Flag | Global | Condition | Effect |
|---|---|---|---|
| Extended debug | `byte_2A5F224` | sm > 72 | Enables SASS-level annotations (`.nv_debug_line_sass`, `.nv_debug_info_reg_sass`) |
| SASS mode | `byte_2A5F225` | sm > 89 | Forces SASS output; enables `.nv_debug_line_sass` and `.nv_debug_info_reg_sass` with `-g` |
| Mercury mode | `byte_2A5F222` | sm > 99 | All debug sections get `.nv.merc.` prefix; unlocks 5 additional sections |

The five sections unlocked by Mercury mode that are absent in SASS-only mode (sm90--sm99) are `.nv.merc.debug_aranges`, `.nv.merc.debug_ranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, and `.nv.merc.debug_pubtypes`.

## Self-Check Validation

The `--self-check` CLI flag triggers a round-trip validation where the linker reconstitutes SASS from the capmerc binary and compares it against expected output. Debug sections are one of three independently validated categories:

| Check | Error string | String address |
|---|---|---|
| Text section | `"Self check for capsule mercury text section failed"` | `0x2458F38` |
| **Debug section** | `"Self check for capsule mercury debug section failed"` | `0x2458F70` |
| Relocation section | `"Self check for capsule mercury relocation section failed"` | `0x2458FA8` |

### Self-Check Debug Verification Algorithm

The self-check (Phase 9 of FNLZR, lines 1493--1729 of `sub_4748F0`) performs a recursive invocation of the FNLZR engine itself. The reconstituted SASS is produced by calling `sub_4748F0` again with the output from Phase 6 as input. The debug section comparison proceeds in two stages:

**Stage 1: Section count match** (line 1639). The number of sections in `v419[60]` (the original debug section list) must equal the number of sections in the reconstituted output's debug section list (at `v348 + 24`). If the counts differ, error code 19 is set.

**Stage 2: Per-section content match** (lines 1641--1677). For each section in the original list:
1. The section name is extracted and the `.nv.merc.` prefix is stripped (offset +8) if present.
2. The same stripping is applied to the reconstituted section.
3. The stripped names are compared with `strcmp`. If they match, the section sizes and content are compared byte-by-byte.
4. If any comparison fails, error code 19 is set.

The prefix stripping uses `sub_44E3A0` (starts-with predicate) with the 9-byte string `".nv.merc."` at `0x1D40605`. The stripping advances the pointer by 8 bytes (not 9), producing a result that retains the leading dot (e.g., `".nv.merc.debug_info"` + 8 = `".debug_info"`). This is correct because the `.nv.merc.` prefix is 9 characters including the trailing dot, but the stripped name must retain its own leading dot.

**Stage 3: Relocation section match** (lines 1679--1729). A parallel comparison loop runs for the relocation sections stored in `v419[61]`, using identical prefix stripping and byte comparison. The same error codes apply.

When any self-check fails, the detailed error message is emitted:

```text
Failure of '%s' section in self-check for capsule mercury.
See the Jira confluence page 'MERCSW-125' for more information
that includes some debugging steps.
```

The `%s` is the section name that failed comparison. The string is at `0x1F44288`.

### Self-Check Error Codes

The self-check produces three distinct error codes:

| Code | Meaning | Source |
|---|---|---|
| 17 | Text section content mismatch | `sub_4748F0` line 1631 |
| 18 | Section/relocation count mismatch | `sub_4748F0` lines 1723, 1698 |
| 19 | Debug or relocation section content mismatch | `sub_4748F0` line 1728 |

## FNLZR Prefix Matching

During finalization, `sub_4748F0` (`nvlink_link_and_finalize_entry`, 8,950 bytes) iterates over section names using the prefix string `".nv.merc."` (9 bytes, at `0x1D40605`) as a discriminator. The matching function `sub_44E3A0` performs a starts-with check. When a section name matches:

```c
char* section_name = get_section_name(section);
if (starts_with(".nv.merc.", section_name)) {
    // Strip the ".nv.merc." prefix (advance pointer by 8 bytes)
    // to recover the original section name, e.g.:
    //   ".nv.merc.debug_info" -> ".debug_info" (offset +8, not +9)
    section_name += 8;
}
```

The prefix strip uses offset 8 (not 9), which means the result retains the leading dot: `".nv.merc.debug_info"` + 8 = `".debug_info"`. This is consistent with the standard DWARF section naming convention and allows FNLZR to dispatch the stripped name through the same debug section classification paths used for standard cubins.

The FNLZR uses this stripped name in 4 code paths within `sub_4748F0`:

| Code path | Decompiled line | Purpose |
|---|---|---|
| Debug section comparison | 1648 | Self-check: strip prefix for name comparison |
| Reconstituted section lookup | 1663 | Self-check: match original vs reconstituted |
| Relocation section comparison | 1689 | Self-check: strip prefix for relocation sections |
| Relocation content match | 1703 | Self-check: match original vs reconstituted relocations |

## Emission Call Chain

The complete emission path from the ptxas backend to the final cubin:

```text
ELF_WriteCompleteObject (sub_1CF3720, 15.3 KB)
  |
  +-- ELF_BuildSectionTable (sub_1CEE030, 26 KB)
  |     |
  |     +-- ELF_EmitConstantSection (sub_1CEC7E0)
  |     +-- ELF_EmitReservedSmem (sub_1CECBB0)
  |     +-- ELF_EmitDebugSections (sub_1CED0E0) --> classify .nv.merc.debug_*
  |     +-- ELF_EmitSASSDebugSections (sub_1CED7C0) --> classify .debug_* for Mercury re-emission
  |     +-- ELF_EmitSpecialSections (sub_1CEDD50) --> handles .nv_debug_info_reg_{sass,type}
  |     |     xrefs: sub_1CEDF2B (.nv_debug_info_reg_sass), sub_1CEDF7A (.nv_debug_info_reg_type)
  |
  +-- ELF_ProcessRelocations (sub_1CEF5B0) --> .nv.merc.symtab_shndx
  +-- ELF_EmitSymbolTable (sub_1CF07A0)
  +-- ELF_EmitRelocationTable (sub_1CF1690) --> relocations for 7 Mercury debug sections
  +-- ELF_EmitSectionHeaders (sub_1CF2100) --> .nv.merc.rela
  +-- ELF_EmitProgramHeaders (sub_1CF72E0)
```

## Lifecycle Through the Pipeline

1. **ptxas backend** (embedded in nvlink): Compiles PTX to Mercury IR. The ELF object emitter creates all 15 debug sections under the `.nv.merc` namespace. Each section's `sh_flags` includes `0x10000000`. Seven of the 15 sections receive relocation entries through `sub_1CF1690`. The DWARF emitter (`sub_1672F50`) detects debug sections using the `.nv_debug_` and `.debug_` prefix strings at `0x226B814` and `0x226B81F`. Timing information is tracked via the `"DebugInfo-time"` diagnostic at `0x1EED040` and peak memory via `"PeakDebugInfoMemoryUsage"` at `0x1EED160`.

2. **nvlink merge phase** (`sub_45E7D0`): When linking for a Mercury target, sections with `sh_flags & 0x10000000` are skipped. They are not merged into the output ELF. Verbose mode prints `"skip mercury section %i"` for each. Two independent code paths check the flag (lines 1583 and 1711 of the decompiled merge function), corresponding to the two iteration passes over input sections.

3. **nvlink output phase**: The complete pre-FNLZR image is serialized to an in-memory buffer. For `--extract` debug workflows, this intermediate image may be written to a side file.

4. **FNLZR post-link transformation** (`sub_4748F0` -> `sub_471700`): The finalizer reads the Mercury container, strips the `".nv.merc."` prefix from section names (offset +8), and dispatches each debug section through the finalization rewrite. Phase 7 of the FNLZR serializes `.debug_line` and `.debug_frame` through three helper functions (`sub_477480`, `sub_4783C0`, `sub_477510`), then remaps `.debug_line` relocation offsets through a BST built by `sub_4826F0`. The BST maps Mercury-address offsets to SASS-address offsets, updating relocation type `0x10008` (`R_CUDA_ABS32_HI_20`) entries that reference `.debug_line` symbols.

5. **Self-check** (optional, `--self-check`): Phase 9 recursively invokes `sub_4748F0` on the finalized output, strips `.nv.merc.` prefixes from both the original and reconstituted section names, and compares section contents byte-by-byte. Debug sections are checked in a dedicated loop (lines 1641--1677) with error code 19 on mismatch. Failure triggers the `"Self check for capsule mercury debug section failed"` error with a reference to internal Jira `MERCSW-125`.

6. **Final output**: The rewritten cubin contains SASS `.text` instead of `.nv.merc` code. If the output format is capmerc (default for sm100+), the Mercury container is preserved alongside SASS for JIT re-finalization by the CUDA driver. The capmerc output includes both `.nv.merc.debug_*` sections (for driver JIT) and standard `.debug_*` sections (for tools).

## Note on Section Name Encoding

The Mercury debug section names (`.nv.merc.debug_*`) are straightforward namespace-prefixed strings. There is no ROT13 encoding, Caesar cipher, or other obfuscation applied to Mercury debug section names in nvlink v13.0.88. The only obfuscation mechanism in the binary relates to PTX source text (strings `"obfuscated ptx"` at `0x1EE9918` and `"Error reading obfuscated PTX file"` at `0x1F42CF8`), which is unrelated to debug section naming. The `.nv.merc.` prefix is a plain namespace identifier, not an encoded form.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1CED0E0` | `ELF_EmitDebugSections` | 9,262 B | Classifies 15 `.nv.merc.debug_*` / `.nv.merc.nv_debug_*` sections |
| `0x1CED7C0` | `ELF_EmitSASSDebugSections` | 6,757 B | Classifies 15 unprefixed `.debug_*` / `.nv_debug_*` sections |
| `0x1CEDD50` | `ELF_EmitSpecialSections` | ~4,500 B | Handles `.nv_debug_info_reg_{sass,type}` emission |
| `0x1CF1690` | `ELF_EmitRelocationTable` | 16,049 B | Processes relocations for 7 Mercury debug sections (dual-lookup) |
| `0x1CEE030` | `ELF_BuildSectionTable` | 26,362 B | Orchestrates all section emission including debug |
| `0x1CF3720` | `ELF_WriteCompleteObject` | 99,074 B | Top-level ELF writer, calls section builder |
| `0x1CF7F30` | `ELF_WriteRelocObject` | ~40,000 B | Relocatable ELF writer (alternative to complete) |
| `0x1672F50` | DWARF emitter (ptxas) | 22,076 B | DWARF generation during ptxas compilation |
| `0x181B160` | Register debug emitter (SASS) | ~1,000 B | Emits `.nv_debug_info_reg_sass` content |
| `0x181B270` | Register type emitter (SASS) | ~1,000 B | Emits `.nv_debug_info_reg_type` content |
| `0x45E7D0` | `merge_elf` | 89,156 B | Merge phase, skips Mercury-flagged sections |
| `0x4748F0` | `nvlink_link_and_finalize_entry` | 48,730 B | FNLZR entry, Phase 7 debug serialization, Phase 9 self-check |
| `0x471700` | `nvlink_finalize_object` | 78,516 B | Per-kernel finalization orchestrator |
| `0x477480` | debug line/frame builder | ~200 B | Sorts compilation unit line/frame tables |
| `0x4783C0` | debug line/frame serializer | ~2,000 B | Emits DWARF line/frame program opcodes |
| `0x477510` | debug section accessor | ~40 B | Returns pointer to serialized debug data |
| `0x4826F0` | debug address BST builder | ~300 B | Builds BST for `.debug_line` offset remapping |
| `0x4713E0` | section name hasher | ~300 B | Hashes section names for debug dispatch |
| `0x4746F0` | debug hash installer | ~200 B | Installs hashed name into debug dispatch table |
| `0x47DE50` | debug input processor | ~8,000 B | Processes debug line/frame input during compilation |
| `0x448590` | string table resolver | ~200 B | Resolves section name from header + string table |
| `0x44E3A0` | starts-with predicate | ~200 B | Prefix matching for `".nv.merc."` |
| `0x4AC380` | CLI option registration | ~3,000 B | Registers `--self-check`, `--out-sass` options |

## Cross-References

### nvlink Wiki

- [FNLZR](../mercury/fnlzr.md) — the post-link finalizer that consumes Mercury debug sections; Phase 7 (debug serialization) and Phase 9 (self-check) are the primary debug processing phases
- [Mercury ELF Sections](../mercury/elf-sections.md) — complete catalog of all 19 `.nv.merc.*` sections including non-debug structural sections
- [Mercury Overview](../mercury/overview.md) — what Mercury is and why it exists
- [Capsule Mercury Format](../mercury/capmerc-format.md) — self-check mechanism and capmerc pipeline
- [DWARF Processing](dwarf-processing.md) — core DWARF parser that feeds Mercury debug emission; processes `.debug_info` through classifier at `sub_12D4370`
- [NVIDIA Debug Extensions](nvidia-extensions.md) — non-Mercury `.nv_debug_*` section catalog; the 4 NVIDIA-specific Mercury sections mirror these
- [Line Table Merging](line-tables.md) — how `.debug_line` / `.nv_debug_line_sass` are built during LTO; Phase 7a of FNLZR re-serializes these
- [Debug Options](options.md) — debug level flags and FNLZR debug section control; documents `byte_2A5F310`, `byte_2A5F222`, and the `-g` / `--generate-line-info` / `--suppress-debug-info` interactions
- [Merge Phase](../pipeline/merge.md) — where Mercury sections are skipped during linking, gated by the `0x10000000` flag
- [Section Merging](../linker/section-merging.md) — general section merge mechanics and CUDA type catalog
- [NVIDIA Section Types](../elf/nvidia-sections.md) — section type constants and the `SHF_CUDA_MERCURY` flag (`0x10000000`)

### Sibling Wikis

- [ptxas: Debug Info](../../ptxas/output/debug-info.html) — ptxas generates both standard and Mercury-prefixed debug sections; its Mercury debug classifier at `sub_1C98C60` identifies `.nv.merc.debug_*` sections, and the SASS debug classifier at `sub_1C99340` handles unprefixed `.debug_*` sections
- [cicc: Debug Info Pipeline](../../cicc/pipeline/debug-info-pipeline.html) — cicc's debug metadata generation is upstream of Mercury section creation; the debug info mode (`-g` vs `-generate-line-info`) propagated through nvlink's LTO pipeline determines which Mercury debug sections are populated

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| 15 Mercury debug sections (11 DWARF + 4 NVIDIA) | HIGH | All 15 section name strings confirmed in `nvlink_strings.json` at contiguous addresses `0x245832A`--`0x2458470` |
| String table cluster at `0x245832A`--`0x2458470` | HIGH | Exact addresses confirmed in strings JSON for all 15 entries |
| Section classifier `sub_1CED0E0` checks `0x10000000` flag | HIGH | Decompiled: `(*((_QWORD *)a2 + 1) & 0x10000000) == 0` at line 47; first comparison is `.nv.merc.debug_abbrev` at line 60 |
| SASS debug classifier `sub_1CED7C0` — 15 unprefixed names, no flag check | HIGH | Decompiled file confirms sequential `memcmp`/`strcmp` chain for unprefixed debug section names; no `0x10000000` check present |
| Section type ranges `1879048198`--`1879048212` and `1879048292`--`1879048318` | HIGH | Decompiled `sub_1CED0E0`: `v4 - 1879048198` range check and `v4 - 1879048292` range check at lines 52--54; bitmask constant `0x5D05` and `_bittest64` with `23813` confirmed |
| Dual-lookup pattern in `sub_1CF1690` | HIGH | Decompiled: unprefixed `memcmp`/`strcmp` followed by Mercury-prefixed `strcmp` with `sh_flags & 0x10` check at each stage; all 7 pairs confirmed |
| Relocation context offsets (+72 through +120) | HIGH | Decompiled `sub_1CF1690`: assignments to `a2 + 72`, `a2 + 80`, `a2 + 88`, `a2 + 96`, `a2 + 104`, `a2 + 112`, `a2 + 120` confirmed at LABEL_123/117/121/119/115/127/125 |
| Self-check error strings at `0x2458F38`/`0x2458F70`/`0x2458FA8` | HIGH | All three strings confirmed in `nvlink_strings.json` with xrefs from error table |
| Detailed failure string referencing `MERCSW-125` at `0x1F44288` | HIGH | String confirmed with exact text |
| FNLZR prefix match `".nv.merc."` (9 bytes) at `0x1D40605` | HIGH | String confirmed; 4 xrefs in `sub_4748F0` at lines 1648, 1663, 1689, 1703 |
| Prefix strip uses offset 8 (not 9) | HIGH | Decompiled `sub_4748F0` line 1649: `v304 += 8`; line 1664: `v309 += 8`; line 1690: `v330 += 8`; line 1706: `v335 = s2 + 8`. All four instances confirmed |
| `merge_elf` skip: `"skip mercury section %i"` verbose message at `0x1D3BCB7` | HIGH | String confirmed; decompiled `sub_45E7D0` lines 1583 and 1711 reference the flag check at `0x10000000` |
| Mercury flag `0x10000000` in `sh_flags` (bit 28) | HIGH | Decompiled `sub_1CED0E0` checks `& 0x10000000`; decompiled `sub_1CF1690` checks `(*(_BYTE *)(v9 + 11) & 0x10)` (same flag, byte-level access); decompiled `sub_45E7D0` checks `(v140 & 0x10000000)` |
| Phase 7 debug serialization (lines 1294--1372) | HIGH | Functions `sub_477480`, `sub_4783C0`, `sub_477510` all confirmed at stated addresses; mode 0/1 dispatch confirmed; `+1` size adjustment confirmed |
| Phase 7c BST for `.debug_line` remapping via `sub_4826F0` | HIGH | Decompiled `sub_4826F0` builds BST structure; relocation type `0x10008` confirmed in `sub_4748F0` line 1326; `.debug_line` 12-byte `memcmp` at line 1331 |
| Self-check error codes 17, 18, 19 | HIGH | Decompiled `sub_4748F0`: code 17 at line 1631, code 18 at lines 1698/1723, code 19 at line 1728 |
| DWARF emitter at `sub_1672F50` uses `.nv_debug_` / `.debug_` prefixes | HIGH | Xrefs confirmed: `0x226B814` (`.nv_debug_`) referenced from `sub_1672F50` at `0x1673F58`; `0x226B81F` (`.debug_`) from `0x1673F69` |
| Register debug emitters at `sub_181B160` / `sub_181B270` | HIGH | Xrefs confirmed for `.nv_debug_info_reg_sass` at `0x241282C` from `sub_181B160`; `.nv_debug_info_reg_type` at `0x2412844` from `sub_181B270` |
| No ROT13 encoding of section names | HIGH | Exhaustive search of `nvlink_strings.json` for `rot13`, `caesar`, `obfusc`, `encode`, `mangle` found zero matches related to section names; only `"obfuscated ptx"` and `"Error reading obfuscated PTX file"` found, both relating to PTX source, not section names |
| 7 of 15 sections carry relocations | HIGH | Individually verified in decompiled `sub_1CF1690`: `.debug_frame` (+72), `.debug_line` (+80), `.nv_debug_line_sass` (+88), `.nv_debug_info_reg_sass` (+96), `.nv_debug_info_reg_type` (+104), `.debug_info` (+112), `.debug_loc` (+120). Each has both unprefixed and `.nv.merc.`-prefixed code paths |
| Emission call chain | HIGH | All function addresses confirmed in decompiled/; call hierarchy verified through xref analysis; `sub_1CEDD50` xrefs to `.nv_debug_info_reg_sass` and `.nv_debug_info_reg_type` confirmed |
| `ELF_WriteCompleteObject` at `sub_1CF3720` (15.3 KB) | HIGH | Decompiled file present; 15,263 bytes consistent with file size |
| `-g` flag effect on Mercury debug sections | HIGH | `byte_2A5F310` confirmed in `sub_427AE0`; FNLZR config `v28[3]` bits 32..39 confirmed in `sub_4275C0`; `byte_2A5F222` (Mercury mode) condition sm > 99 confirmed |
| Debug timing / memory diagnostics | HIGH | Strings `"DebugInfo-time"` at `0x1EED040` and `"PeakDebugInfoMemoryUsage"` at `0x1EED160` confirmed in strings JSON |
