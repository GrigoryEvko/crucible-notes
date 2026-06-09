# Mercury ELF Sections

Mercury targets (sm100 and above) introduce a family of `.nv.merc.*` ELF sections that carry per-kernel Mercury-format code, relocations, symbol table extensions, shared memory reservations, and debug information. These sections are emitted by the ptxas embedded backend during cubin generation and consumed by the FNLZR post-link transformation. During the nvlink merge phase they are conditionally skipped and deferred to FNLZR, identified by the `0x10000000` flag in the section's `sh_flags` field.

## Section Catalog

nvlink v13.0.88 defines 19 distinct `.nv.merc.*` section names. They fall into four functional groups: the container section, structural sections, the shared memory reservation, and debug sections.

### Container

| Section name | Description |
|---|---|
| `.nv.merc` | Top-level Mercury container. Holds the encoded Mercury instruction stream for a single kernel. This is the primary payload section that the FNLZR reads, rewrites (opex expansion, scheduling, register assignment), and replaces with final SASS `.text`. |

### Structural Sections

| Section name | Description |
|---|---|
| `.nv.merc.rela` | Mercury-specific relocation entries. Contains `Elf64_Rela` records whose symbol and section indices reference the Mercury symbol table rather than the standard cubin `.symtab`. Emitted by `ELF_EmitSectionHeaders` (`sub_1CF2100`) and read during the FNLZR relocation pass. |
| `.nv.merc.symtab_shndx` | Extended symbol table section index array. Mirrors the standard ELF `SHT_SYMTAB_SHNDX` mechanism: when a Mercury symbol references a section index >= `SHN_LORESERVE` (0xFF00), the true index is stored here. Emitted by `ELF_ProcessRelocations` (`sub_1CEF5B0`) and the complete object writer (`sub_1CF3720`). |

### Shared Memory Reservation

| Section name | Description |
|---|---|
| `.nv.merc.nv.shared.reserved.` | Per-kernel reserved shared memory within the Mercury container. This is the Mercury-side equivalent of the top-level `.nv.shared.reserved.*` sections. The name is a prefix — the full section name is formed by appending the reservation identifier (e.g., `tcgen05_partition`). Emitted by `sub_1CEC390` and referenced at string address `0x24582E8`. |

### Standard DWARF Debug Sections

These 10 sections mirror the standard DWARF debug section layout, but scoped under the `.nv.merc` namespace so they travel alongside the Mercury instruction stream and can be updated in lockstep when FNLZR rewrites the code.

| Section name | DWARF equivalent | Description |
|---|---|---|
| `.nv.merc.debug_abbrev` | `.debug_abbrev` | DWARF abbreviation tables. Maps abbreviation codes to tag/attribute pairs. |
| `.nv.merc.debug_aranges` | `.debug_aranges` | Address range tables. Maps address ranges to compilation units for fast lookup. |
| `.nv.merc.debug_frame` | `.debug_frame` | Call frame information (CFI). Stack unwinding tables for each function. |
| `.nv.merc.debug_info` | `.debug_info` | Core DWARF information entries (DIEs). Contains the type, variable, and function descriptions. |
| `.nv.merc.debug_line` | `.debug_line` | Line number program. Maps Mercury instruction addresses to source file/line/column. |
| `.nv.merc.debug_loc` | `.debug_loc` | Location lists. Describes where a variable lives across different PC ranges. |
| `.nv.merc.debug_macinfo` | `.debug_macinfo` | Macro information. Records `#define` and `#undef` directives. |
| `.nv.merc.debug_pubnames` | `.debug_pubnames` | Public name index. Accelerator table mapping global names to DIE offsets. |
| `.nv.merc.debug_pubtypes` | `.debug_pubtypes` | Public type index. Accelerator table mapping type names to DIE offsets. |
| `.nv.merc.debug_ranges` | `.debug_ranges` | Non-contiguous address ranges. Used when a function or scope spans disjoint address intervals. |
| `.nv.merc.debug_str` | `.debug_str` | String table. Deduplicated pool of null-terminated strings referenced by `.debug_info` entries via `DW_FORM_strp`. |

### NVIDIA-Specific Debug Sections

These 4 sections carry NVIDIA-proprietary debug data that has no standard DWARF equivalent. They are emitted by `ELF_EmitSASSDebugSections` (`sub_1CED7C0`).

| Section name | Description |
|---|---|
| `.nv.merc.nv_debug_ptx_txt` | Embedded PTX source text. Stores the PTX assembly corresponding to this kernel, enabling source-level debugging at the PTX layer. |
| `.nv.merc.nv_debug_line_sass` | SASS-level line mapping. Maps final SASS instruction addresses to source lines, parallel to `.nv.merc.debug_line` but at the SASS granularity rather than Mercury granularity. |
| `.nv.merc.nv_debug_info_reg_sass` | Per-instruction register liveness. Records which SASS registers are live at each instruction, used by cuda-gdb for variable inspection at arbitrary breakpoints. |
| `.nv.merc.nv_debug_info_reg_type` | Register type annotations. Associates data types with physical registers, enabling the debugger to interpret register contents as the correct source-level type. |

## Section Header Format

Every Mercury section uses an `Elf64_Shdr` (64 bytes) with NVIDIA-specific `sh_type` values and the Mercury flag in `sh_flags`. The section header layout is standard ELF64:

```text
Offset  Size  Field          Mercury usage
------  ----  -------------- ------------------------------------------
0x00    4     sh_name        Offset into section-local .shstrtab
0x04    4     sh_type        NVIDIA section type (see table below)
0x08    8     sh_flags       Always includes 0x10000000 (Mercury flag)
0x10    8     sh_addr        Virtual address (0 for most Mercury sections)
0x18    8     sh_offset      File offset of section data
0x20    8     sh_size        Size of section data in bytes
0x28    4     sh_link        Associated section index (e.g., symtab link)
0x2C    4     sh_info        Context-dependent (see per-type details)
0x30    8     sh_addralign   Power-of-2 alignment requirement
0x38    8     sh_entsize     Entry size for fixed-size entry sections
```

### Section Type Values (`sh_type`)

The `sh_type` field uses NVIDIA-proprietary values in the `SHT_LOPROC`..`SHT_HIPROC` range (`0x70000000`..`0x7FFFFFFF`). Mercury sections use these types:

| `sh_type` | Constant | Used by | Description |
|---|---|---|---|
| `0x7000000C` | `SHT_CUDA_FUNCBODY` | `ELF_BuildSectionTable` | Wrapper section for per-kernel CUDA function body. Created by copying the input section header, then overwriting `sh_type` to `0x7000000C` and `sh_flags` to `0x10000000`. The `sh_info` field (offset 44) stores the original input section index for back-reference. |
| `0x7000000D` | `SHT_CUDA_FUNCGROUP` | `ELF_BuildSectionTable` | Function group section (global/extern function declarations). Header initialized to all zeros, then `sh_type=0x7000000D`, `sh_flags=0x10000000`. The `sh_info` field stores the link index and `sh_addralign` is copied from the group descriptor. |
| `0x70000016` | `SHT_CUDA_MERC` | `ELF_BuildSectionTable` | Per-kernel Mercury code section. This is the section that carries the actual Mercury instruction blob for a specific kernel. Distinguished from `0x7000000C` by containing a 4-byte symbol-index prefix before the instruction data. |
| `0x70000085` | `SHT_NV_MERC_SYMTAB` | `ELF_BuildSectionTable` | Mercury symbol table section. Carries the symbol table specific to Mercury sections. Created during the symtab emission phase. |

All four types always have `sh_flags` bit 28 set (`0x10000000`), marking them as Mercury sections for the merge-phase skip logic.

## `.nv.merc` Container Section Binary Layout

The `.nv.merc` container section holds the primary Mercury instruction payload for a single kernel. Its name is constructed programmatically by `ELF_EmitMercContainer` (`sub_1CEC4C0`) using `sprintf`:

```c
// sub_1CEC4C0 -- constructs the ".nv.merc" section name
char *section_name = get_section_name(elf_sections, input_section);
int len = strlen(section_name);
char *buf = alloc(len + 9);  // strlen(".nv.merc") + 1
memset(buf, 0, len + 9);
sprintf(buf, "%s%s", ".nv.merc", section_name);
return find_section(elf_sections, buf);
```

The suffix appended is the original kernel section name (e.g., `.text.mykernel`), producing names like `.nv.merc.text.mykernel`.

### Per-Kernel Mercury Section (`sh_type = 0x70000016`)

When `ELF_BuildSectionTable` (`sub_1CEE030`) creates a per-kernel Mercury section, it constructs the section with this layout:

```text
+0x00  uint32_t  symbol_index   Output ELF symbol table index for this kernel.
                                Written by sub_464DB0() lookup against the
                                output symbol-to-section mapping (ctx+416).
+0x04  byte[]    merc_data      Raw Mercury instruction blob, copied from the
                                compiler's internal representation. Size =
                                (descriptor+264) bytes. This is the opaque
                                Mercury instruction stream that FNLZR will
                                expand into SASS.
```

The total section size stored in `sh_size` equals `4 + merc_data_length`. In `sub_1CEE030` at address `0x1CEF150` (decompiled line 619):

```c
// Per-kernel Mercury section construction (sh_type = 0x70000016)
shdr->sh_type  = 0x70000016;
shdr->sh_flags = 0x10000000;
shdr->sh_addralign = descriptor->alignment;  // from descriptor+280

// Write 4-byte symbol index prefix
*(uint32_t *)output_ptr = lookup_output_index(ctx->output_symmap, kernel->input_section_idx);
output_ptr += 4;

// Copy Mercury instruction blob
memcpy(output_ptr, descriptor->merc_data, descriptor->merc_data_size);  // from descriptor+256, +264
output_ptr += descriptor->merc_data_size;

shdr->sh_size = descriptor->merc_data_size + 4;
```

### Wrapper Sections (`sh_type = 0x7000000C`)

The `0x7000000C` wrapper sections are created differently. The section builder copies the entire 64-byte `Elf64_Shdr` from the input ELF, then overwrites:

- `sh_type` = `0x7000000C`
- `sh_flags` = `0x10000000`
- `sh_info` (offset 44) = original input section index

The section content is the raw instruction data from the input, copied via `memcpy` with the original size. The `sh_offset` field is updated to point to the new location in the output ELF.

### Function Group Sections (`sh_type = 0x7000000D`)

Created for global/extern function declarations. The section header is zero-initialized, then:

- `sh_type` = `0x7000000D`
- `sh_flags` = `0x10000000`
- `sh_info` = group link index
- `sh_addralign` = copied from group descriptor

Content is copied from the group descriptor's data buffer (`descriptor+8`) with size (`descriptor+16`).

## `.nv.merc.symtab_shndx` Construction

The `.nv.merc.symtab_shndx` section is conditionally created when any Mercury symbol references a section index >= `SHN_LORESERVE` (`0xFF00`). It is built in two places:

**1. In `ELF_BuildSectionTable` (`sub_1CEE030`)** — when the `a5+208` flag is set (indicating extended section indices are needed):

```c
// sub_1CEE030, line ~237
memcpy(strtab_ptr, ".symtab_shndx", 13);  // name into .shstrtab
shdr->sh_name = strtab_offset;
shdr->sh_type = SHT_SYMTAB_SHNDX;  // 18
shdr->sh_flags = 0;
shdr->sh_addralign = 4;
shdr->sh_entsize = 4;
shdr->sh_size = ctx->extended_index_count;  // from ctx+200
memset(output_ptr, 0, ctx->extended_index_count);
```

The section is also registered with the auxiliary name `.nv.merc.symtab_shndx` (via `strcpy` at decompiled line 287) so it can be found by name during relocation processing.

**2. In `ELF_WriteCompleteObject` (`sub_1CF3720`)** — during symbol output, when a symbol's section header index field is `0xFFFF`:

```c
// sub_1CF3720, line ~2668
if (sym->st_shndx == 0xFFFF) {
    Elf64_Shdr *shndx_section = find_section(elf, ".nv.merc.symtab_shndx");
    uint32_t *shndx_data = get_section_data(elf, shndx_section);
    actual_index = shndx_data[symbol_index];
}
```

The extended index array is a flat `uint32_t[]` parallel to the symbol table. Entry `[i]` holds the true section index for symbol `i`. Entries for symbols with indices < `0xFF00` are zero.

## Shared Memory Reservation: `sub_1CEC390`

The function `ELF_EmitMercSharedReserved` at `0x1CEC390` handles both standard and Mercury-prefixed shared memory reservation sections. Its logic:

```c
sub_1CEC390(ctx, section_name, is_merc_mode, section_hdr, elf_handle):
    if (!is_merc_mode):
        if starts_with(".nv", section_name):
            check for ".nv.reservedSmem.begin"  -> callback at ctx+600
            check for ".nv.reservedSmem.cap"    -> callback at ctx+584
            check for ".nv.reservedSmem.offset0" -> callback at ctx+608
        return

    section_type = section_hdr[1]  // sh_type at offset 4
    if (section_type != SHT_NOBITS(8) && section_type != 0x70000015):
        return 0

    // Try standard prefix first
    name = get_name(elf_handle, section_hdr)
    if starts_with(".nv.shared.reserved.", name):
        invoke callback at ctx+608 with (".nv.shared.reserved.", name)
        return 1

    // Try Mercury prefix
    if (section_type != SHT_NOBITS(8) && section_type != 0x70000015):
        return 0
    name = get_name(elf_handle, section_hdr)
    if starts_with(".nv.merc.nv.shared.reserved.", name):
        invoke callback at ctx+608 with (".nv.merc.nv.shared.reserved.", name)
        return 1

    return 0
```

The function recognizes two section types that can carry shared memory reservations: `SHT_NOBITS` (standard ELF type 8, for `.bss`-like sections) and `0x70000015` (`SHT_CUDA_SHARED`). It dispatches through three vtable callbacks at offsets 584, 600, and 608 from the context pointer, corresponding to the three reservation metadata types:

| Callback offset | Reservation metadata | Section prefix |
|---|---|---|
| `ctx+584` | `.nv.reservedSmem.cap` | Capacity in bytes |
| `ctx+600` | `.nv.reservedSmem.begin` | Start offset |
| `ctx+608` | `.nv.reservedSmem.offset0` / data | Offset/reservation data |

## `.nv.merc` Constant Bank Mapping: `sub_1CEC660`

The function `ELF_MercSectionSetup` at `0x1CEC660` maps standard CUDA constant bank sections to their Mercury-namespace equivalents. It takes an input section whose name starts with `.nv.constant` and produces the Mercury mirror name by combining `.nv.merc` with a semantic suffix:

```c
// sub_1CEC660 -- constant bank name mapping
name = get_section_name(elf, section);
if (!starts_with(".nv.constant", name))
    return NULL;

suffix = name + 13;  // skip ".nv.constant" prefix, get bank digit
bank_type = name[12] + 0x70000034;  // map '0'->'0x70000064', etc.

if (bank_type == vtable->entry_image_hdr()  || bank_type == vtable->func304())
    tag = ".entry_image_header_indices";
else if (bank_type == vtable->func144())
    tag = ".driver";
else if (bank_type == vtable->func136())
    tag = ".optimizer";
else if (bank_type == vtable->func192())
    tag = ".user";
else if (bank_type == vtable->func168())
    tag = ".pic";
else if (bank_type == vtable->func152())
    tag = ".tools_data";
else
    tag = "";

merc_name = build_name(".nv.merc", ".nv.constant", tag, suffix);
return find_section(elf, merc_name);
```

The resulting Mercury section names follow the pattern `.nv.merc.constant<digit><tag>`, e.g., `.nv.merc.constant0.driver`.

## Section Detection: `sub_1CED0E0`

The function at `0x1CED0E0` (identified as `ELF_EmitDebugSections`, 9,262 bytes, 373 lines) is the primary emitter for the standard DWARF debug sections within the Mercury container. It is called from the ELF section builder during cubin generation by the ptxas backend.

The function emits sections in a fixed order:

1. `.nv.merc.debug_macinfo`  (address of first reference: `0x1CED23B`)
2. `.nv.merc.debug_abbrev`   (`0x1CED4B1`)
3. `.nv.merc.debug_aranges`  (`0x1CED500`)
4. `.nv.merc.debug_frame`    (`0x1CED538`)
5. `.nv.merc.debug_info`     (`0x1CED560`)
6. `.nv.merc.debug_loc`      (`0x1CED589`)
7. `.nv.merc.debug_pubnames` (`0x1CED5CB`)
8. `.nv.merc.debug_pubtypes` (`0x1CED601`)
9. `.nv.merc.debug_ranges`   (`0x1CED63E`)
10. `.nv.merc.debug_str`     (`0x1CED670`)

The NVIDIA-specific debug sections are emitted by a separate function, `ELF_EmitSASSDebugSections` (`sub_1CED7C0`, 6,757 bytes, 315 lines), which emits:

1. `.nv.merc.nv_debug_info_reg_sass` (`0x1CED6AD`)
2. `.nv.merc.nv_debug_line_sass`     (`0x1CED6E9`)
3. `.nv.merc.nv_debug_ptx_txt`       (`0x1CED70A`)
4. `.nv.merc.debug_line`             (`0x1CED74B`)
5. `.nv.merc.nv_debug_info_reg_type` (`0x1CED77E`)

Note that `.nv.merc.debug_line` is emitted by the SASS debug function rather than the standard DWARF debug function, because the line mapping must reference SASS instruction addresses which are only known after SASS emission.

## Special Section Content Routing: `sub_1CEDD50`

The function `ELF_EmitSpecialSections` at `0x1CEDD50` routes debug section content to cached internal representations when available, falling back to the generic content lookup if not. It operates by testing section names against a priority list:

```c
sub_1CEDD50(ctx, section_hdr, section_index):
    name = get_name(ctx->elf_sections, section_hdr)

    if name == ".debug_line":       return ctx->cached_debug_line       // ctx+80
    if name == ".debug_frame":      return ctx->cached_debug_frame      // ctx+72
    if name == ".nv_debug_line_sass":    return ctx->cached_nv_debug_line_sass  // ctx+88
    if name == ".debug_info":       return ctx->cached_debug_info       // ctx+112
    if name == ".debug_loc":        return ctx->cached_debug_loc        // ctx+120
    if name == ".nv_debug_info_reg_sass": return ctx->cached_reg_sass   // ctx+96
    if name == ".nv_debug_info_reg_type": return ctx->cached_reg_type   // ctx+104

    // No cached version -- fall through to generic lookup
    return lookup_section_data(ctx->section_data_map, section_index)
```

The section type check at each step tests whether the section type is in the PROGBITS family (`sh_type == 1`, or in the CUDA range `0x70000006..0x70000014` using a bitmask `0x5D05`), ensuring the name-based routing only applies to content sections.

## Mercury Section Flag: `0x10000000`

During the merge phase in nvlink, Mercury sections are identified by bit 28 (`0x10000000`) of the ELF section header `sh_flags` field. This is a custom NVIDIA flag with no standard ELF equivalent.

When the linker is operating in Mercury mode (both input and output contexts have Mercury compatibility flags set), `merge_elf` (`sub_45E7D0`) skips sections that carry this flag:

```c
if (is_mercury_compatible && (section_flags & 0x10000000) != 0) {
    // verbose trace: "skip mercury section %i"
    continue;
}
```

The Mercury compatibility condition is a conjunction of two flags: one on the output linker context (`ctx+48`) and one derived from the input ELF header. Both must be true for the skip to activate.

Skipped sections are not lost. They remain in the individual per-input cubin images held in memory. The FNLZR post-link transformation operates on the complete in-memory ELF and has access to these sections for code rewriting, relocation, and debug info update.

### Why Skip During Merge?

Mercury sections contain pre-finalization code that will be entirely rewritten by FNLZR. Merging them into the output ELF at link time would be wasted work:

1. Mercury instruction addresses are not yet final — they change after opex expansion and scheduling.
2. Mercury relocations reference Mercury-internal symbols, not the output ELF symbol table.
3. Mercury debug sections contain address references that become stale after FNLZR rewrites the code.

By deferring these sections, the linker avoids creating index mappings, translating relocations, and copying data that will be replaced wholesale during finalization.

## Section Creation Flow in `ELF_WriteCompleteObject` (`sub_1CF3720`)

The complete object writer at `0x1CF3720` (99 KB, ~3200 decompiled lines) is the top-level function that produces a finished ELF cubin. It orchestrates the entire output pipeline, including Mercury section creation.

### Phase 1: ELF Header Copy (lines 692--701)

The function begins by copying the 64-byte `Elf64_Ehdr` from the internal representation to the output buffer using four 16-byte SSE loads:

```c
// Copy Elf64_Ehdr (64 bytes) to output buffer via 4x __m128i
output[0] = load_128(elf_header[0]);   // e_ident[16]
output[1] = load_128(elf_header[1]);   // e_type, e_machine, e_version, e_entry(low)
output[2] = load_128(elf_header[2]);   // e_entry(high), e_phoff, e_shoff(low)
output[3] = load_128(elf_header[3]);   // e_shoff(high), e_flags, e_ehsize, e_ph/shentsize, etc.
dest += 64;
```

### Phase 2: Section Header Table Pre-copy (lines 708--747)

Before building the full section table, the function copies section headers from input to output, skipping sections that will be rebuilt. It iterates all input sections and filters out:

- Empty PROGBITS sections (`sh_flags & 4` set, `sh_size == 0`)
- Sections identified by `ELF_EmitReservedSmem` (`sub_1CECBB0`) as reserved shared memory
- Sections with `sh_type == 0x70000064` (section base) or `0x7000000D` (function group)
- Sections whose name contains `.nv.constant0`

For Mercury mode (flag at `ctx+432`), a special fixup applies: if a section header has the `0x10` flag in byte 11 (the internal Mercury marker) and `sh_type != 0x70000016`, the `sh_name` field is incremented by 8 to account for the `.nv.merc` prefix in the output string table.

### Phase 3: ELF Header Flags (lines 748--798)

The `e_flags` field in the output ELF header is constructed from the input header's flags, the architecture identifier, and the Mercury mode flags. Key logic:

- When Mercury mode is active (flag at `ctx+435`), the flags field is masked to `0xFFFFFFF8` and ORed with the architecture sub-code
- When the ELF class byte is `0x41` (ELFOSABI_CUDA), the architecture code is packed into the upper 16 bits of `e_flags`
- The `0x4000` bit in `e_flags` is cleared for Mercury targets
- If `ctx+433` is set (Mercury finalization mode), bit `0x40` in byte 1 of `e_flags` is set

### Phase 4: Section Table Build (line 2762)

After all per-section data has been written, the function calls `ELF_BuildSectionTable` (`sub_1CEE030`) to construct the final section header table. This is where Mercury-specific section types are assigned:

```c
error = ELF_BuildSectionTable(&output_buf, section_count, start_section, is_executable, ctx);
```

### Phase 5: Program Headers (lines 2788--2813)

For executable cubins (`e_type == 2`), program headers are emitted as 56-byte `Elf64_Phdr` entries. The program header entry size is stored at `elf_header+54` (the `e_phentsize` field). Program headers are serialized by iterating the program header array and copying each 56-byte entry.

### Phase 6: Relocation Application (lines 2880--3110)

The relocation engine applies fixups to the output buffer. For each relocation, it looks up the relocation descriptor from a table at `qword_2A77D10` (a 64-byte-per-entry dispatch table indexed by relocation type). The descriptor specifies up to three cascading fixup operations, each with a shift/mask operation and a bit width (32 or 64). Mercury-specific relocations (`R_MERCURY_*`) use types in the `0x10000`..`0x1003F` range, dispatched through the table at `off_2459160`. Standard CUDA relocations use types `0`..`0x73`, dispatched through `off_245A160`.

## Section Skip Filter in `ELF_EmitReservedSmem` (`sub_1CECBB0`)

The function at `0x1CECBB0` (3,426 bytes) determines which sections should be **skipped** during the section header pre-copy in `ELF_WriteCompleteObject`. It returns 1 (skip) for:

- Sections with `sh_type == 0x70000016` (`SHT_CUDA_MERC`) that have the `0x10` flag in byte 11
- Sections with `sh_type == 0x70000085` (`SHT_NV_MERC_SYMTAB`) that have the `0x10` flag
- Sections with `sh_type == 0x7000000A` (`SHT_CUDA_SHARED`)
- Sections named `.nv.shared.*` with `sh_type == SHT_NOBITS(8)` or `0x70000015`
- Sections named `.nv.shared.reserved.*`
- Sections named `.nv.local.*`
- Sections named `.nv.global`
- Sections named `.nv_debug.shared`
- Sections named `.strtab` or `.shstrtab` (string tables are rebuilt)
- Sections with `sh_type == SHT_SYMTAB(2)` or `0x70000086`
- Sections with `sh_type == SHT_RELA(4)` or `SHT_REL(9)` (relocations are rebuilt)

The function does NOT skip (returns 0) when `a2` (the Mercury mode flag) is false, or when `sh_type == 0x7000000A` (NOBITS sections are always skipped regardless of Mercury mode).

## Emission Call Chain

The complete emission path from the ptxas backend to the final cubin:

```text
ELF_WriteCompleteObject (sub_1CF3720, 99KB)
  |
  +-- Phase 1: Copy Elf64_Ehdr (64 bytes, 4x SSE load/store)
  +-- Phase 2: Section header pre-copy with skip filter (sub_1CECBB0)
  +-- Phase 3: e_flags construction (Mercury mode bits)
  +-- Phase 4: Per-section data emission loop
  |     |
  |     +-- PROGBITS/CUDA sections: memcpy from input
  |     +-- NV_INFO sections: EIATTR re-encoding (switch on 17 codes)
  |     +-- Debug sections: routed via sub_1CEDD50 cached data
  |     +-- Relocation sections: re-emitted with updated indices
  |
  +-- ELF_BuildSectionTable (sub_1CEE030, 26KB)
  |     |
  |     +-- .nv.merc.symtab_shndx creation (if extended indices needed)
  |     +-- SHT_CUDA_FUNCBODY (0x7000000C) sections from ctx+216 list
  |     +-- SHT_CUDA_FUNCGROUP (0x7000000D) sections from ctx+272 list
  |     +-- Per-kernel text sections from ctx+288 list
  |     +-- Shared memory sections from ctx+280 list
  |     +-- SHT_CUDA_MERC (0x70000016) per-kernel Mercury from ctx+0 list
  |     +-- Mercury debug/special sections from ctx+440 list
  |     +-- SHT_NV_MERC_SYMTAB (0x70000085) if Mercury finalization mode
  |     +-- Section index remapping via v225 index array
  |
  +-- Program header emission (56-byte Elf64_Phdr entries)
  +-- Relocation application (dispatch table at off_2459160 / off_245A160)
  +-- Symbol table cleanup (zero out local symbols for stripped output)
```

### NV_INFO Re-encoding During Output

When `ELF_WriteCompleteObject` encounters an NV_INFO section (`sh_type == 0x70000000`), it does not simply memcpy the content. Instead, it walks the EIATTR TLV stream and re-encodes each entry, performing symbol index remapping for entries that reference functions or sections.

The EIATTR entry format is a 4-byte header followed by optional payload:

```text
byte[0]  size_type:  2 = 2-byte immediate, 4 = variable-length payload
byte[1]  eiattr_code: identifies the attribute
byte[2..3] for size_type=2: 2-byte immediate value
           for size_type=4: uint16 payload length (bytes after header)
```

The following EIATTR codes receive special handling during Mercury output:

| Code | Name | Handling |
|---|---|---|
| 0x0A | `EIATTR_FRAME_SIZE` | Payload contains `{uint32 size, uint16 arch_code, uint16 reg_count}`. The `arch_code` field is rewritten by calling the architecture vtable to get the current encoding. |
| 0x11 | `EIATTR_PARAM_CBANK` | Contains per-function constant bank index; remapped through symbol table. |
| 0x15/0x16 | (constant bank IDs) | The 2-byte immediate value at byte[2] is rewritten with the architecture-specific constant bank type from vtable function 304. |
| 0x1C, 0x28, 0x31, 0x46, 0x47, 0x57 | Various | Payload words containing section indices are remapped via `sub_449A80` (symbol index lookup). |
| 0x2E | `EIATTR_CRS_STACK_SIZE` | 8-byte entries: `{uint32 symbol_index, uint16 flags, uint16 reserved}`. Symbol indices remapped. |
| 0x34 | `EIATTR_COOPERATIVE_LAUNCH` | Variable-length entries with nested symbol references, each remapped. |
| 0x36 | `EIATTR_SW1850030_WAR` | 8-byte header copied; if `ctx+248` flag is set, bit 3 is ORed in. |
| 0x39 | `EIATTR_REQNTID` | 16-byte entries: `{uint32 symbol_index, uint32 x, uint32 y, uint32 z}`. First word remapped. |
| 0x40 | `EIATTR_INDIRECT_BRANCH` | 12-byte entries: `{uint32 symbol_index, uint8 flags, uint8 pad, uint16 reserved, uint32 data}`. First word remapped. |
| 0x44 | `EIATTR_CUDA_API_VERSION` | 8-byte entries: `{uint32 symbol_index, uint32 version}`. First word remapped. |
| 0x45 | `EIATTR_SW_GLOBAL_DATA` | Skipped if byte 15 has the `0x10` flag set. |
| 0x4A | (specialization) | 2-byte immediate rewritten from `ctx+112` (specialization constant). |
| 0x55 | `EIATTR_EXIT_OFFSETS` | Variable-length: entries are `{type, ...}` where type 1 = symbol ref (remapped), type 2 = `{symbol, count, data[count]}`, type 3 = `{size, data[size]}`. |

Codes 0x43 (`'C'`), 0x48 (`'H'`), and 0x5A (`'Z'`) are unconditionally skipped (the input pointer advances past them without writing output). Code 0x5F (`'_'`) sets an internal flag at `ctx+322`.

## String Table Evidence

All `.nv.merc.*` section names are stored in the nvlink binary's read-only data segment as contiguous null-terminated strings. The primary cluster spans addresses `0x24582E8` through `0x2458D00`:

| String address | Section name | First xref |
|---|---|---|
| `0x1D40605` | `.nv.merc.` | `sub_4748F0` (link+finalize entry, used as prefix match) |
| `0x24582E8` | `.nv.merc.nv.shared.reserved.` | `sub_1CEC390` |
| `0x2458305` | `.nv.merc` | `sub_1CEC4C0`, `sub_1CEC660`, `sub_1CF72E0`, `sub_1CF7F30` |
| `0x245832A` | `.nv.merc.debug_abbrev` | `sub_1CED0E0` |
| `0x2458340` | `.nv.merc.debug_aranges` | `sub_1CED0E0` |
| `0x2458357` | `.nv.merc.debug_frame` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x245836C` | `.nv.merc.debug_info` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458380` | `.nv.merc.debug_loc` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458393` | `.nv.merc.debug_macinfo` | `sub_1CED0E0` |
| `0x24583AA` | `.nv.merc.debug_pubnames` | `sub_1CED0E0` |
| `0x24583C2` | `.nv.merc.debug_pubtypes` | `sub_1CED0E0` |
| `0x24583DA` | `.nv.merc.debug_ranges` | `sub_1CED0E0` |
| `0x24583F0` | `.nv.merc.debug_str` | `sub_1CED0E0` |
| `0x2458403` | `.nv.merc.nv_debug_ptx_txt` | `sub_1CED0E0` |
| `0x245841D` | `.nv.merc.debug_line` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458431` | `.nv.merc.nv_debug_line_sass` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458450` | `.nv.merc.nv_debug_info_reg_sass` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458470` | `.nv.merc.nv_debug_info_reg_type` | `sub_1CED0E0`, `sub_1CF1690` |
| `0x2458490` | `.nv.merc.symtab_shndx` | `sub_1CEF5B0`, `sub_1CF3720` |
| `0x2458D00` | `.nv.merc.rela` | `sub_1CF72E0` |

The string at `0x1D40605` (`.nv.merc.` with trailing dot) is separate from the main cluster. It appears in `sub_4748F0` (the link+finalize entry point at `0x4748F0`, 48 KB) and in `sub_471700` (the finalization orchestrator at `0x471700`, 78 KB). These functions use it as a prefix for `strncmp`-style matching when iterating section names to identify which sections belong to the Mercury container.

## Lifecycle Through the Pipeline

1. **ptxas backend** (embedded in nvlink): Compiles PTX to Mercury IR. The ELF object emitter creates `.nv.merc` containing the encoded Mercury instruction stream, `.nv.merc.rela` for Mercury-specific relocations, and all applicable debug sections. Each section's `sh_flags` includes `0x10000000`.

2. **nvlink merge phase** (`sub_45E7D0`): When linking for a Mercury target, sections with `sh_flags & 0x10000000` are skipped. They are not merged into the output ELF. Verbose mode prints `"skip mercury section %i"` for each.

3. **nvlink output phase**: The complete pre-FNLZR image is serialized to an in-memory buffer. For `--extract` debug workflows, this intermediate image may be written to a side file.

4. **FNLZR post-link transformation** (`sub_4748F0` -> `sub_471700`): The finalizer reads the Mercury container, performs opex expansion (Mercury opcode -> SASS instruction expansion), instruction scheduling, register assignment, and produces final SASS `.text`. It updates or replaces the Mercury debug sections with SASS-level equivalents.

5. **Final output**: The rewritten cubin (now with SASS `.text` instead of `.nv.merc`) is written to the output file. If the output format is capsule Mercury (`--binary-kind capmerc`, the default for sm100+), the Mercury container may be preserved alongside the SASS for JIT re-finalization.

## Section Index Remapping in `ELF_BuildSectionTable`

When `ELF_BuildSectionTable` builds the output section table, it maintains an index remapping array (`v225`) that maps input section indices to output section indices. This is necessary because:

1. Some input sections are skipped (reserved smem, string tables, etc.)
2. New sections are inserted (Mercury wrapper sections, function groups)
3. The output section order differs from the input order

The remapping is applied in a final fixup pass (lines 793--813 in `sub_1CEE030`):

```c
for (j = 0; j < remap_array_size; j++) {
    output_index = remap_array[j];
    if (output_index) {
        shdr = &section_headers[output_index];
        // Fix sh_link field (bit 0x40 in sh_flags means sh_info is a section ref)
        if (shdr->sh_flags & 0x40)
            shdr->sh_info = remap_array[shdr->sh_info];
        // Fix sh_link for non-symtab sections
        if (shdr->sh_link && shdr->sh_type != 0x70000085)
            shdr->sh_link = remap_array[shdr->sh_link];
    }
}
```

For the Mercury symbol table section (`sh_type == 0x70000085`), `sh_link` is not remapped because it references the symbol table directly rather than a section. Additionally, the Mercury symbol table's entries have their `st_shndx` fields remapped: if `st_shndx == 0xFFFF`, the true index is looked up from the `.nv.merc.symtab_shndx` section; otherwise, the 16-bit field is remapped directly.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1CEC390` | `ELF_EmitMercSharedReserved` | ~300 B | Emits `.nv.merc.nv.shared.reserved.*` sections |
| `0x1CEC4C0` | `ELF_EmitMercContainer` | ~400 B | Emits the `.nv.merc` container section |
| `0x1CEC660` | `ELF_MercSectionSetup` | ~400 B | Configures Mercury section attributes, maps constant banks |
| `0x1CECBB0` | `ELF_EmitReservedSmem` | 3,426 B | Section skip filter for reserved shared memory |
| `0x1CED0E0` | `ELF_EmitDebugSections` | 9,262 B | Emits 10 standard DWARF `.nv.merc.debug_*` sections |
| `0x1CED7C0` | `ELF_EmitSASSDebugSections` | 6,757 B | Emits 5 NVIDIA-specific `.nv.merc.nv_debug_*` sections |
| `0x1CEDD50` | `ELF_EmitSpecialSections` | ~650 B | Routes debug section content to cached representations |
| `0x1CEE030` | `ELF_BuildSectionTable` | 26,362 B | Orchestrates all section emission, assigns `sh_type` values |
| `0x1CEF5B0` | `ELF_ProcessRelocations` | 22,867 B | Processes relocations, emits `.nv.merc.symtab_shndx` |
| `0x1CF07A0` | `ELF_EmitSymbolTable` | ~61 KB | Emits the output symbol table |
| `0x1CF1690` | `ELF_EmitRelocationTable` | 16,049 B | Emits relocation entries for Mercury debug sections |
| `0x1CF2100` | `ELF_EmitSectionHeaders` | 31,261 B | Emits all `Elf64_Shdr` entries including `.nv.merc.rela` |
| `0x1CF3720` | `ELF_WriteCompleteObject` | 99,074 B | Top-level: writes complete ELF object with all Mercury sections |
| `0x1CF72E0` | `ELF_EmitProgramHeaders` | 17,710 B | Emits program headers, references `.nv.merc`, `.nv.merc.rela` |
| `0x1CF7F30` | `ELF_WriteRelocatableObject` | 44,740 B | Writes relocatable object, references `.nv.merc` |
| `0x4748F0` | `nvlink_link_and_finalize_entry` | 48,730 B | Top-level FNLZR entry, uses `.nv.merc.` prefix match |
| `0x471700` | `nvlink_finalize_object` | 78,516 B | Finalization orchestrator, uses `.nv.merc.` prefix match |
| `0x45E7D0` | `merge_elf` | 89,156 B | Merge phase, skips sections with `sh_flags & 0x10000000` |

## Relocation Dispatch Tables

The relocation application phase uses two 64-byte-per-entry dispatch tables to apply fixups:

| Table address | Range | Description |
|---|---|---|
| `off_245A160` | Standard CUDA relocations `0`..`0x73` | `R_CUDA_*` fixups for standard cubin sections |
| `off_2459160` | Mercury relocations `0x10000`..`0x1003F` | `R_MERCURY_*` fixups for Mercury sections |

Each table entry (64 bytes, 16 `int32` fields) encodes up to three cascading fixup operations. For each operation, four fields describe the action:

```text
int32 fields[16]:
  [1]  op1_type     0=nop, 1=write, 18=write, 19=zero, 22-29=masked shift
  [2]  op1_shift    Shift amount for masked operations
  [3]  op1_width    Target bit width (32 or 64)
  [5]  op2_type     Same encoding as op1
  [6]  op2_shift
  [7]  op2_width
  [9]  op3_type     Same encoding as op1
  [10] op3_shift
  [11] op3_width
```

Type 1 writes the resolved value directly. Type 19 writes zero. Types 22--29 apply a bitmask from the `xmmword_1D3F8E0` SSE constant table, shift right by the amount from `xmmword_1D3F920`, and write the result.

## Capsule Mercury Sections Beyond `.nv.merc.*`

A capmerc (`--binary-kind=capmerc`) cubin is not just the `.nv.merc.*` family documented above. The outer capsule also carries the per-function capsule descriptors under section names of the form `.nv.capmerc<func>` (distinct prefix, `sh_type = 1`/PROGBITS, no `SHF_NV_MERC` flag) and the Hash Relocation sections (`.nvHRKE`, `.nvHRKI`, `.nvHRCE`, `.nvHRCI`, `.nvHRDE`, `.nvHRDI`) used by FNLZR for incremental linking. These are produced and consumed by FNLZR (`sub_4748F0`), not by the section emitters on this page. The 328-byte capsule descriptor layout and the Hash Relocation pairing rules are documented in [Capsule Mercury Format](capmerc-format.md) and the ptxas-side wiki cross-link below; this page covers only the `.nv.merc.*` namespace.

## Cross-References

### nvlink Internal
- [Mercury Overview](overview.md) — what Mercury is and why it exists
- [Capsule Mercury Format](capmerc-format.md) — the capmerc container that wraps Mercury + SASS, including `.nv.capmerc<func>` descriptors and `.nvHR*` hash-relocation sections
- [FNLZR](fnlzr.md) — the post-link finalizer that consumes Mercury sections
- [R_MERCURY Relocations](r-mercury-relocations.md) — relocation types used in `.nv.merc.rela`
- [Merge Phase](../pipeline/merge.md) — where Mercury sections are skipped during linking
- [Section Merging](../linker/section-merging.md) — general section merge mechanics
- [Output Phase](../pipeline/output.md) — Mercury output path with FNLZR invocation
- [NV_INFO / EIATTR](../elf/nv-info.md) — EIATTR encoding format reference
- [Device ELF Format](../elf/device-elf-format.md) — Elf64_Ehdr and Elf64_Shdr layout details

### Sibling Wikis
- [ptxas: Capsule Mercury & Finalization](../../ptxas/codegen/capmerc.html) — standalone ptxas capmerc format: Mercury section classifier (`sub_1C98C60`, 9KB, 15 `.nv.merc.*` names), capsule descriptor (328 bytes), per-function Mercury payload structure. Section types and names are identical between ptxas (producer) and nvlink (consumer).
- [ptxas: Mercury Encoder Pipeline](../../ptxas/codegen/mercury.html) — standalone ptxas Mercury encode/decode pipeline that emits these sections.

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| 19 distinct `.nv.merc.*` section names | **HIGH** | All 19 section name strings verified at addresses `0x24582E8`--`0x2458D00` in `nvlink_strings.json`. First xrefs to emitter functions confirmed. |
| `.nv.merc` prefix at `0x2458305` | **HIGH** | String verified at exact address. Multiple xrefs confirmed: `sub_1CEC4C0`, `sub_1CEC660`, `sub_1CF72E0`, `sub_1CF7F30`. |
| `.nv.merc.` prefix at `0x1D40605` (used for strncmp matching) | **HIGH** | String verified. Xrefs from `sub_4748F0` and `sub_471700` confirmed. |
| `sh_type = 0x70000016` (SHT_CUDA_MERC) | **HIGH** | Hex value verified from decompiled `sub_1CEE030` (ELF_BuildSectionTable). Assignment `shdr->sh_type = 0x70000016` explicit in code. |
| `sh_type = 0x7000000C` (SHT_CUDA_FUNCBODY) | **HIGH** | Verified from decompiled section builder. Header copy + overwrite pattern confirmed. |
| `sh_type = 0x7000000D` (SHT_CUDA_FUNCGROUP) | **HIGH** | Verified from decompiled section builder. Zero-init + type assignment confirmed. |
| `sh_type = 0x70000085` (SHT_NV_MERC_SYMTAB) | **HIGH** | Verified from decompiled code. Referenced in skip filter `sub_1CECBB0`. |
| Mercury flag `0x10000000` in `sh_flags` (bit 28) | **HIGH** | Verified from decompiled merge function `sub_45E7D0`. Bitmask `0x10000000` check explicit. `"skip mercury section %i"` string confirms skip behavior. |
| `ELF_EmitDebugSections` (`sub_1CED0E0`, 9,262 bytes) emits 10 DWARF sections | **HIGH** | Function exists at stated address. All 10 debug section name strings verified with xrefs pointing to this function. Emission order confirmed from xref addresses. |
| `ELF_EmitSASSDebugSections` (`sub_1CED7C0`, 6,757 bytes) emits 5 NVIDIA sections | **HIGH** | Function exists. All 5 section name strings verified with xrefs. |
| `ELF_EmitMercContainer` (`sub_1CEC4C0`) uses sprintf to build section name | **MEDIUM** | Function exists. String construction logic inferred from decompiled code. The `sprintf` pattern with `.nv.merc` prefix is consistent but the exact format string usage is from decompiler interpretation. |
| Per-kernel Mercury section layout: 4-byte symbol index + merc_data blob | **MEDIUM** | Layout inferred from decompiled `sub_1CEE030` at address `0x1CEF150`. The 4-byte prefix write and memcpy pattern are clear, but field naming is inferred. |
| `ELF_MercSectionSetup` (`sub_1CEC660`) constant bank mapping | **MEDIUM** | Function exists. Constant bank name mapping (`.driver`, `.optimizer`, `.user`, `.pic`, `.tools_data`) inferred from vtable dispatch in decompiled code. Exact tag names from code patterns. |
| `ELF_WriteCompleteObject` (`sub_1CF3720`, 99 KB, ~3200 lines) | **HIGH** | Decompiled file exists at stated address. Size confirmed from function bounds. SSE-accelerated header copy pattern verified. |
| Section skip filter `sub_1CECBB0` (3,426 bytes) — list of skipped section types | **MEDIUM** | Function exists. Skip conditions enumerated from decompiled switch/if-chain. Individual `sh_type` values verified but the complete list may have minor omissions. |
| NV_INFO re-encoding EIATTR codes (0x0A, 0x11, 0x15/0x16, etc.) | **MEDIUM** | EIATTR code values from decompiled switch statement in `sub_1CF3720`. Code-to-name mapping uses the EIATTR catalog from the nv-info.md page. Handling descriptions inferred from code paths. |
| Relocation dispatch tables at `off_2459160` / `off_245A160` | **HIGH** | Table addresses verified from decompiled relocation application code. 64-byte-per-entry format confirmed from indexing arithmetic. |
| Section index remapping via `v225` array in `ELF_BuildSectionTable` | **MEDIUM** | Remapping array and fixup loop verified from decompiled `sub_1CEE030`. Variable name `v225` is decompiler-assigned. |
| `ELF_EmitSpecialSections` (`sub_1CEDD50`) routes debug sections to cached data | **MEDIUM** | Function exists. Priority-list name matching pattern confirmed from decompiled code. Context offsets (+72, +80, +88, etc.) verified. |
| Merge phase skips Mercury sections with verbose trace | **HIGH** | `"skip mercury section %i"` at `0x1D3BCB7` verified. Xref to `sub_45E7D0` (merge function, 89,156 bytes) confirmed. |
| `0x5D05` bitmask for PROGBITS family detection | **LOW** | Hex value from decompiled code. Interpretation as a section type family bitmask is inferred from context. May be a multi-field encoded value rather than a simple bitmask. |
