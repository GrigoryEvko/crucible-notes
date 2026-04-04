# Mercury ELF Sections

Mercury targets (sm100 and above) introduce a family of `.nv.merc.*` ELF sections that carry per-kernel Mercury-format code, relocations, symbol table extensions, shared memory reservations, and debug information. These sections are emitted by the ptxas embedded backend during cubin generation and consumed by the FNLZR post-link transformation. During the nvlink merge phase they are conditionally skipped and deferred to FNLZR, identified by the `0x10000000` flag in the section's `sh_flags` field.

## Section Catalog

nvlink v13.0.88 defines 19 distinct `.nv.merc.*` section names. They fall into four functional groups: the container section, structural sections, the shared memory reservation, and debug sections.

### Container

| Section name | Purpose |
|---|---|
| `.nv.merc` | Top-level Mercury container. Holds the encoded Mercury instruction stream for a single kernel. This is the primary payload section that the FNLZR reads, rewrites (opex expansion, scheduling, register assignment), and replaces with final SASS `.text`. |

### Structural Sections

| Section name | Purpose |
|---|---|
| `.nv.merc.rela` | Mercury-specific relocation entries. Contains `Elf64_Rela` records whose symbol and section indices reference the Mercury symbol table rather than the standard cubin `.symtab`. Emitted by `ELF_EmitSectionHeaders` (`sub_1CF2100`) and read during the FNLZR relocation pass. |
| `.nv.merc.symtab_shndx` | Extended symbol table section index array. Mirrors the standard ELF `SHT_SYMTAB_SHNDX` mechanism: when a Mercury symbol references a section index >= `SHN_LORESERVE` (0xFF00), the true index is stored here. Emitted by `ELF_ProcessRelocations` (`sub_1CEF5B0`) and the complete object writer (`sub_1CF3720`). |

### Shared Memory Reservation

| Section name | Purpose |
|---|---|
| `.nv.merc.nv.shared.reserved.` | Per-kernel reserved shared memory within the Mercury container. This is the Mercury-side equivalent of the top-level `.nv.shared.reserved.*` sections. The name is a prefix -- the full section name is formed by appending the reservation identifier (e.g., `tcgen05_partition`). Emitted by `sub_1CEC390` and referenced at string address `0x24582E8`. |

### Standard DWARF Debug Sections

These 10 sections mirror the standard DWARF debug section layout, but scoped under the `.nv.merc` namespace so they travel alongside the Mercury instruction stream and can be updated in lockstep when FNLZR rewrites the code.

| Section name | DWARF equivalent | Purpose |
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

| Section name | Purpose |
|---|---|
| `.nv.merc.nv_debug_ptx_txt` | Embedded PTX source text. Stores the PTX assembly corresponding to this kernel, enabling source-level debugging at the PTX layer. |
| `.nv.merc.nv_debug_line_sass` | SASS-level line mapping. Maps final SASS instruction addresses to source lines, parallel to `.nv.merc.debug_line` but at the SASS granularity rather than Mercury granularity. |
| `.nv.merc.nv_debug_info_reg_sass` | Per-instruction register liveness. Records which SASS registers are live at each instruction, used by cuda-gdb for variable inspection at arbitrary breakpoints. |
| `.nv.merc.nv_debug_info_reg_type` | Register type annotations. Associates data types with physical registers, enabling the debugger to interpret register contents as the correct source-level type. |

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

1. Mercury instruction addresses are not yet final -- they change after opex expansion and scheduling.
2. Mercury relocations reference Mercury-internal symbols, not the output ELF symbol table.
3. Mercury debug sections contain address references that become stale after FNLZR rewrites the code.

By deferring these sections, the linker avoids creating index mappings, translating relocations, and copying data that will be replaced wholesale during finalization.

## Emission Call Chain

The complete emission path from the ptxas backend to the final cubin:

```
ELF_WriteCompleteObject (sub_1CF3720, 99KB)
  |
  +-- ELF_BuildSectionTable (sub_1CEE030, 26KB)
  |     |
  |     +-- ELF_EmitConstantSection (sub_1CEC7E0)
  |     +-- ELF_EmitReservedSmem (sub_1CECBB0)
  |     +-- ELF_EmitDebugSections (sub_1CED0E0) --> .nv.merc.debug_*
  |     +-- ELF_EmitSASSDebugSections (sub_1CED7C0) --> .nv.merc.nv_debug_*
  |     +-- ELF_EmitSpecialSections (sub_1CEDD50)
  |
  +-- ELF_ProcessRelocations (sub_1CEF5B0) --> .nv.merc.symtab_shndx
  +-- ELF_EmitSymbolTable (sub_1CF07A0)
  +-- ELF_EmitRelocationTable (sub_1CF1690)
  +-- ELF_EmitSectionHeaders (sub_1CF2100) --> .nv.merc.rela
  +-- ELF_EmitProgramHeaders (sub_1CF72E0)
```

The relocatable object writer (`sub_1CF7F30`, 44 KB) follows a similar path and also emits `.nv.merc` and `.nv.merc.rela` sections.

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

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1CEC390` | `ELF_EmitMercSharedReserved` | ~300 B | Emits `.nv.merc.nv.shared.reserved.*` sections |
| `0x1CEC4C0` | `ELF_EmitMercContainer` | ~400 B | Emits the `.nv.merc` container section |
| `0x1CEC660` | `ELF_MercSectionSetup` | ~400 B | Configures Mercury section attributes |
| `0x1CECBB0` | `ELF_EmitReservedSmem` | 3,426 B | Emits reserved shared memory sections |
| `0x1CED0E0` | `ELF_EmitDebugSections` | 9,262 B | Emits 10 standard DWARF `.nv.merc.debug_*` sections |
| `0x1CED7C0` | `ELF_EmitSASSDebugSections` | 6,757 B | Emits 5 NVIDIA-specific `.nv.merc.nv_debug_*` sections |
| `0x1CEE030` | `ELF_BuildSectionTable` | 26,362 B | Orchestrates all section emission |
| `0x1CEF5B0` | `ELF_ProcessRelocations` | 22,867 B | Processes relocations, emits `.nv.merc.symtab_shndx` |
| `0x1CF1690` | `ELF_EmitRelocationTable` | 16,049 B | Emits relocation entries for Mercury debug sections |
| `0x1CF2100` | `ELF_EmitSectionHeaders` | 31,261 B | Emits all `Elf64_Shdr` entries including `.nv.merc.rela` |
| `0x1CF3720` | `ELF_WriteCompleteObject` | 99,074 B | Writes complete ELF object, references `.nv.merc.symtab_shndx` |
| `0x1CF72E0` | `ELF_EmitProgramHeaders` | 17,710 B | Emits program headers, references `.nv.merc`, `.nv.merc.rela` |
| `0x1CF7F30` | `ELF_WriteRelocatableObject` | 44,740 B | Writes relocatable object, references `.nv.merc` |
| `0x4748F0` | `nvlink_link_and_finalize_entry` | 48,730 B | Top-level FNLZR entry, uses `.nv.merc.` prefix match |
| `0x471700` | `nvlink_finalize_object` | 78,516 B | Finalization orchestrator, uses `.nv.merc.` prefix match |
| `0x45E7D0` | `merge_elf` | 89,156 B | Merge phase, skips sections with `sh_flags & 0x10000000` |

## Cross-References

- [Mercury Overview](overview.md) -- what Mercury is and why it exists
- [Capsule Mercury Format](capmerc-format.md) -- the capmerc container that wraps Mercury + SASS
- [FNLZR](fnlzr.md) -- the post-link finalizer that consumes Mercury sections
- [R_MERCURY Relocations](r-mercury-relocations.md) -- relocation types used in `.nv.merc.rela`
- [Merge Phase](../pipeline/merge.md) -- where Mercury sections are skipped during linking
- [Section Merging](../linker/section-merging.md) -- general section merge mechanics
- [Output Phase](../pipeline/output.md) -- Mercury output path with FNLZR invocation
