# Mercury Debug Sections

Mercury targets (sm100 and above) carry debug information in a parallel set of `.nv.merc.debug_*` and `.nv.merc.nv_debug_*` ELF sections that mirror the standard DWARF layout but are scoped under the Mercury namespace. These sections travel alongside the Mercury intermediate instruction stream so that FNLZR can update addresses, register assignments, and line mappings in lockstep when it rewrites Mercury IR into final SASS. nvlink emits 15 Mercury debug section variants through two dedicated emitter functions in the ptxas embedded backend, defers them during the merge phase via the `0x10000000` section flag, and validates them through the `--self-check` mechanism after SASS reconstitution.

## Key Facts

| Property | Value |
|---|---|
| Total Mercury debug sections | 15 (11 standard DWARF + 4 NVIDIA-specific) |
| Standard DWARF emitter | `sub_1CED0E0` (`ELF_EmitDebugSections`) at `0x1CED0E0`, 9,262 bytes |
| SASS debug emitter | `sub_1CED7C0` (`ELF_EmitSASSDebugSections`) at `0x1CED7C0`, 6,757 bytes |
| Relocation emitter | `sub_1CF1690` (`ELF_EmitRelocationTable`) at `0x1CF1690`, 16,049 bytes |
| Mercury section flag | `0x10000000` (bit 28 of `sh_flags`, within `SHF_MASKPROC` range) |
| Merge behavior | Skipped when `is_mercury_compatible` is true |
| Self-check error | `"Self check for capsule mercury debug section failed"` at `0x2458F70` |
| Detailed failure | `"Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125'..."` at `0x1F44288` |
| FNLZR prefix match | `".nv.merc."` (9 bytes, trailing dot) at `0x1D40605` |
| String table cluster | `0x245832A`--`0x2458470` (contiguous `.nv.merc.debug_*` / `.nv.merc.nv_debug_*` names) |

## Section Catalog

### Standard DWARF Mirror Sections (11)

These sections replicate the standard DWARF debug section layout under the `.nv.merc` namespace. Each carries debug information at the Mercury instruction address granularity -- addresses that are not yet final and will change after FNLZR performs opex expansion, instruction scheduling, and register assignment.

| Section name | DWARF equivalent | String address | Description |
|---|---|---|---|
| `.nv.merc.debug_abbrev` | `.debug_abbrev` | `0x245832A` | Abbreviation tables mapping codes to tag/attribute pairs |
| `.nv.merc.debug_aranges` | `.debug_aranges` | `0x2458340` | Address range tables for compilation unit lookup |
| `.nv.merc.debug_frame` | `.debug_frame` | `0x2458357` | Call frame information (CFI) for stack unwinding |
| `.nv.merc.debug_info` | `.debug_info` | `0x245836C` | Core DWARF information entries (DIEs) -- types, variables, functions |
| `.nv.merc.debug_line` | `.debug_line` | `0x245841D` | Line number program mapping Mercury addresses to source locations |
| `.nv.merc.debug_loc` | `.debug_loc` | `0x2458380` | Location lists describing variable storage across PC ranges |
| `.nv.merc.debug_macinfo` | `.debug_macinfo` | `0x2458393` | Macro information (`#define` / `#undef` records) |
| `.nv.merc.debug_pubnames` | `.debug_pubnames` | `0x24583AA` | Public name accelerator table (global names to DIE offsets) |
| `.nv.merc.debug_pubtypes` | `.debug_pubtypes` | `0x24583C2` | Public type accelerator table (type names to DIE offsets) |
| `.nv.merc.debug_ranges` | `.debug_ranges` | `0x24583DA` | Non-contiguous address range lists for disjoint scopes |
| `.nv.merc.debug_str` | `.debug_str` | `0x24583F0` | Deduplicated string pool referenced via `DW_FORM_strp` |

### NVIDIA-Specific Debug Sections (4)

These sections carry NVIDIA-proprietary debug data with no standard DWARF equivalent. They are emitted by `ELF_EmitSASSDebugSections` (`sub_1CED7C0`).

| Section name | String address | Description |
|---|---|---|
| `.nv.merc.nv_debug_ptx_txt` | `0x2458403` | Embedded PTX source text for source-level PTX debugging |
| `.nv.merc.nv_debug_line_sass` | `0x2458431` | SASS-level line mapping (final instruction addresses to source lines) |
| `.nv.merc.nv_debug_info_reg_sass` | `0x2458450` | Per-instruction register liveness for cuda-gdb variable inspection |
| `.nv.merc.nv_debug_info_reg_type` | `0x2458470` | Register type annotations associating data types with physical registers |

## Section Detection: `sub_1CED0E0`

The function at `0x1CED0E0` (`ELF_EmitDebugSections`, 9,262 bytes, 373 decompiled lines) serves a dual role: it is named as the "emitter" in the sweep reports, but the decompiled code reveals it acts as a **section detector** -- a predicate that returns 1 if the input section header describes a Mercury debug section, and 0 otherwise. The function is called from the ELF section builder during cubin generation.

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

A section that passes the type guard must also have the `0x10000000` flag set in its `sh_flags` field (checked via `*(uint64_t*)(a2 + 8) & 0x10000000`) before the name resolution and string comparison proceed.

### String Resolution

Section names are resolved through `sub_448590`, which takes the ELF string table base (from `*a1`) and the section header, returning a pointer to the null-terminated section name. The first comparison (`.nv.merc.debug_abbrev`) uses `memcmp` with a length of 22 bytes; subsequent comparisons use `strcmp`. The `.nv.merc.nv_debug_ptx_txt` comparison uses `sub_44E3A0` (a starts-with predicate) rather than exact string matching.

## SASS Debug Emitter: `sub_1CED7C0`

The companion function at `0x1CED7C0` (`ELF_EmitSASSDebugSections`, 6,757 bytes, 315 decompiled lines) is structurally parallel to `sub_1CED0E0` but operates on the **unprefixed** debug section names. It detects whether a section is a standard debug section (without the `.nv.merc.` prefix) that should be placed into the SASS debug output:

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

## Relocation Processing for Mercury Debug Sections

`ELF_EmitRelocationTable` (`sub_1CF1690`, 16,049 bytes) processes relocations for 7 of the 15 Mercury debug sections. These are the sections that contain address references requiring fixup when code addresses change:

| Section name | Contains address references to |
|---|---|
| `.nv.merc.debug_frame` | Function start addresses, CFI PC ranges |
| `.nv.merc.debug_info` | DIE low_pc/high_pc, address attributes |
| `.nv.merc.debug_loc` | Location list PC ranges |
| `.nv.merc.debug_line` | Line program instruction addresses |
| `.nv.merc.nv_debug_line_sass` | SASS instruction addresses |
| `.nv.merc.nv_debug_info_reg_sass` | SASS instruction addresses for register liveness |
| `.nv.merc.nv_debug_info_reg_type` | Register type ranges |

The remaining 8 sections (`.nv.merc.debug_abbrev`, `.nv.merc.debug_aranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`, `.nv.merc.debug_ranges`, `.nv.merc.debug_str`, `.nv.merc.nv_debug_ptx_txt`) do not carry relocations. The abbreviation tables, string pools, macro records, and PTX source text contain no address references. The `.debug_aranges` and `.debug_ranges` sections are rebuilt from scratch by FNLZR after finalization rather than being patched through relocations. The public name/type accelerator tables are similarly reconstructed.

## Mercury Section Flag: `0x10000000`

All Mercury sections (not just debug) are tagged with bit 28 (`0x10000000`) in their ELF section header `sh_flags` field. This is a custom NVIDIA flag within the processor-specific range `SHF_MASKPROC` (`0xF0000000`). It has no standard ELF equivalent.

The flag serves as the primary discriminator during the merge phase. When `merge_elf` (`sub_45E7D0`) processes input cubins for a Mercury-compatible target, it tests each section's flags:

```c
if (is_mercury_compatible && (section_flags & 0x10000000) != 0) {
    // verbose: "skip mercury section %i"
    continue;
}
```

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

## Self-Check Validation

The `--self-check` CLI flag triggers a round-trip validation where the linker reconstitutes SASS from the capmerc binary and compares it against expected output. Debug sections are one of three independently validated categories:

| Check | Error string | String address |
|---|---|---|
| Text section | `"Self check for capsule mercury text section failed"` | `0x2458F38` |
| **Debug section** | `"Self check for capsule mercury debug section failed"` | `0x2458F70` |
| Relocation section | `"Self check for capsule mercury relocation section failed"` | `0x2458FA8` |

When the debug self-check fails, a detailed error message is emitted:

```
Failure of '%s' section in self-check for capsule mercury.
See the Jira confluence page 'MERCSW-125' for more information
that includes some debugging steps.
```

The reconstitution is performed by `sub_5207A0` (`capmerc_reconstitute_sass`, 18,673 bytes), which decodes the Mercury intermediate sections and re-encodes them as SASS using the instruction encoding engine. The debug section comparison verifies that the reconstituted SASS debug sections are byte-identical to the originals, catching any inconsistency in address rewriting, relocation application, or section emission.

## FNLZR Prefix Matching

During finalization, `sub_4748F0` (`nvlink_link_and_finalize_entry`, 48,730 bytes) and `sub_471700` (`nvlink_finalize_object`, 78,516 bytes) iterate over section names using the prefix string `".nv.merc."` (9 bytes, at `0x1D40605`) as a discriminator. The matching function `sub_44E3A0` performs a starts-with check. When a section name matches:

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

The FNLZR uses this stripped name in 4 code paths within `sub_4748F0` (xrefs at `0x4767E4`, `0x476830`, `0x476AD4`, `0x476B1A`), comparing it against expected section names to decide how to process each Mercury debug section during the finalization rewrite.

## Emission Call Chain

The complete emission path from the ptxas backend to the final cubin:

```
ELF_WriteCompleteObject (sub_1CF3720, 99 KB)
  |
  +-- ELF_BuildSectionTable (sub_1CEE030, 26 KB)
  |     |
  |     +-- ELF_EmitConstantSection (sub_1CEC7E0)
  |     +-- ELF_EmitReservedSmem (sub_1CECBB0)
  |     +-- ELF_EmitDebugSections (sub_1CED0E0) --> detect .nv.merc.debug_*
  |     +-- ELF_EmitSASSDebugSections (sub_1CED7C0) --> detect .debug_* for Mercury re-emission
  |     +-- ELF_EmitSpecialSections (sub_1CEDD50)
  |
  +-- ELF_ProcessRelocations (sub_1CEF5B0) --> .nv.merc.symtab_shndx
  +-- ELF_EmitSymbolTable (sub_1CF07A0)
  +-- ELF_EmitRelocationTable (sub_1CF1690) --> relocations for 7 Mercury debug sections
  +-- ELF_EmitSectionHeaders (sub_1CF2100) --> .nv.merc.rela
  +-- ELF_EmitProgramHeaders (sub_1CF72E0)
```

## Lifecycle Through the Pipeline

1. **ptxas backend** (embedded in nvlink): Compiles PTX to Mercury IR. The ELF object emitter creates all 15 debug sections under the `.nv.merc` namespace. Each section's `sh_flags` includes `0x10000000`. Seven of the 15 sections receive relocation entries through `sub_1CF1690`.

2. **nvlink merge phase** (`sub_45E7D0`): When linking for a Mercury target, sections with `sh_flags & 0x10000000` are skipped. They are not merged into the output ELF. Verbose mode prints `"skip mercury section %i"` for each.

3. **nvlink output phase**: The complete pre-FNLZR image is serialized to an in-memory buffer. For `--extract` debug workflows, this intermediate image may be written to a side file.

4. **FNLZR post-link transformation** (`sub_4748F0` -> `sub_471700`): The finalizer reads the Mercury container, strips the `".nv.merc."` prefix from section names (offset +8), and dispatches each debug section through the finalization rewrite. Opex expansion converts Mercury opcodes to SASS instructions, updating all address references. The debug sections are regenerated with SASS-level addresses: `.nv.merc.debug_line` becomes the final `.debug_line`, `.nv.merc.nv_debug_line_sass` becomes `.nv_debug_line_sass`, and so on.

5. **Self-check** (optional, `--self-check`): The reconstitution engine (`sub_5207A0`) converts capmerc back to SASS and compares the debug sections byte-for-byte. Failure triggers the `"Self check for capsule mercury debug section failed"` error with a reference to internal Jira `MERCSW-125`.

6. **Final output**: The rewritten cubin contains SASS `.text` instead of `.nv.merc` code. If the output format is capmerc (default for sm100+), the Mercury container may be preserved alongside SASS for JIT re-finalization by the CUDA driver.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1CED0E0` | `ELF_EmitDebugSections` | 9,262 B | Detects/emits 15 `.nv.merc.debug_*` / `.nv.merc.nv_debug_*` sections |
| `0x1CED7C0` | `ELF_EmitSASSDebugSections` | 6,757 B | Detects/emits 15 unprefixed `.debug_*` / `.nv_debug_*` sections |
| `0x1CF1690` | `ELF_EmitRelocationTable` | 16,049 B | Processes relocations for 7 Mercury debug sections |
| `0x1CEE030` | `ELF_BuildSectionTable` | 26,362 B | Orchestrates all section emission including debug |
| `0x1CF3720` | `ELF_WriteCompleteObject` | 99,074 B | Top-level ELF writer, calls section builder |
| `0x45E7D0` | `merge_elf` | 89,156 B | Merge phase, skips Mercury-flagged sections |
| `0x4748F0` | `nvlink_link_and_finalize_entry` | 48,730 B | FNLZR entry, strips `.nv.merc.` prefix |
| `0x471700` | `nvlink_finalize_object` | 78,516 B | Finalization orchestrator, rebuilds debug sections |
| `0x5207A0` | `capmerc_reconstitute_sass` | 18,673 B | Self-check reconstitution engine |
| `0x448590` | string table resolver | ~200 B | Resolves section name from header + string table |
| `0x44E3A0` | starts-with predicate | ~200 B | Prefix matching for `".nv.merc."` |

## Cross-References

- [Mercury ELF Sections](../mercury/elf-sections.md) -- complete catalog of all 19 `.nv.merc.*` sections (debug + structural)
- [Mercury Overview](../mercury/overview.md) -- what Mercury is and why it exists
- [Capsule Mercury Format](../mercury/capmerc-format.md) -- self-check mechanism and capmerc pipeline
### nvlink Wiki

- [FNLZR](../mercury/fnlzr.md) -- the post-link finalizer that consumes Mercury debug sections
- [Mercury ELF Sections](../mercury/elf-sections.md) -- broader Mercury section catalog including non-debug sections
- [DWARF Processing](dwarf-processing.md) -- core DWARF parser that feeds Mercury debug emission
- [NVIDIA Debug Extensions](nvidia-extensions.md) -- non-Mercury `.nv_debug_*` section catalog
- [Line Table Merging](line-tables.md) -- how `.debug_line` / `.nv_debug_line_sass` are built during LTO
- [Debug Options](options.md) -- debug level flags and FNLZR debug section control
- [Merge Phase](../pipeline/merge.md) -- where Mercury sections are skipped during linking
- [Section Merging](../linker/section-merging.md) -- general section merge mechanics and CUDA type catalog
- [NVIDIA Section Types](../elf/nvidia-sections.md) -- section type constants and the `SHF_CUDA_MERCURY` flag (`0x10000000`)

### Sibling Wikis

- [ptxas: Debug Info](../../ptxas/output/debug-info.html) -- ptxas generates both standard and Mercury-prefixed debug sections; its Mercury debug classifier at `sub_1C98C60` identifies `.nv.merc.debug_*` sections, and the SASS debug classifier at `sub_1C99340` handles unprefixed `.debug_*` sections
- [cicc: Debug Info Pipeline](../../cicc/pipeline/debug-info-pipeline.html) -- cicc's debug metadata generation is upstream of Mercury section creation; the debug info mode (`-g` vs `-generate-line-info`) propagated through nvlink's LTO pipeline determines which Mercury debug sections are populated

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| 15 Mercury debug sections (11 DWARF + 4 NVIDIA) | HIGH | All 15 section name strings confirmed in `nvlink_strings.json` at contiguous addresses `0x245832A`--`0x2458470`: `.nv.merc.debug_abbrev`, `.nv.merc.debug_aranges`, `.nv.merc.debug_frame`, `.nv.merc.debug_info`, `.nv.merc.debug_line`, `.nv.merc.debug_loc`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`, `.nv.merc.debug_ranges`, `.nv.merc.debug_str`, `.nv.merc.nv_debug_ptx_txt`, `.nv.merc.nv_debug_line_sass`, `.nv.merc.nv_debug_info_reg_sass`, `.nv.merc.nv_debug_info_reg_type` |
| String table cluster at `0x245832A`--`0x2458470` | HIGH | Exact addresses confirmed in strings JSON for all 15 entries |
| Section detector `sub_1CED0E0` checks `0x10000000` flag | HIGH | Decompiled: `(*((_QWORD *)a2 + 1) & 0x10000000) == 0` at line 47; first comparison is `.nv.merc.debug_abbrev` at line 60 |
| SASS debug emitter `sub_1CED7C0` -- 15 unprefixed names | HIGH | Decompiled file confirms sequential `memcmp`/`strcmp` chain for unprefixed debug section names |
| Section type ranges `1879048198`--`1879048212` and `1879048292`--`1879048318` | HIGH | Decompiled `sub_1CED0E0`: `v4 - 1879048198` range check and `v4 - 1879048292` range check at lines 52--54 |
| Self-check error strings | HIGH | `"Self check for capsule mercury debug section failed"` at `0x2458F70`, `"Self check for capsule mercury text section failed"` at `0x2458F38`, `"Self check for capsule mercury relocation section failed"` at `0x2458FA8` -- all confirmed |
| Detailed failure string referencing `MERCSW-125` | HIGH | `"Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125'..."` at `0x1F44288` confirmed |
| FNLZR prefix match `".nv.merc."` (9 bytes) at `0x1D40605` | HIGH | String `".nv.merc."` at `0x1D40605` confirmed in strings file |
| Prefix strip uses offset 8 (not 9) | MEDIUM | Documented from decompiled analysis of `sub_4748F0`; the +8 offset producing `".debug_info"` from `".nv.merc.debug_info"` is logically correct but the specific decompiled instruction was not individually re-verified |
| `merge_elf` skip: `"skip mercury section %i"` verbose message | HIGH | String `"skip mercury section %i\n"` at `0x1D3BCB7` confirmed |
| Mercury flag `0x10000000` in `sh_flags` (bit 28) | HIGH | Decompiled `sub_1CED0E0` checks `& 0x10000000`; additionally, decompiled `sub_1CECBB0` checks `(*(_BYTE *)(a3 + 11) & 0x10)` which tests the same flag in the byte-level section header representation |
| Function addresses: `sub_45E7D0`, `sub_4748F0`, `sub_471700`, `sub_5207A0` | HIGH | All four decompiled files present at exact addresses |
| 7 of 15 sections carry relocations | MEDIUM | Claim based on analysis of `sub_1CF1690` (16,049 bytes); the 7 relocatable sections are listed by logical argument (address-containing sections need fixup) but the exact per-section relocation presence was not individually verified in the dense decompiled code |
| Emission call chain (`sub_1CF3720` -> `sub_1CEE030` -> `sub_1CED0E0` etc.) | HIGH | All function addresses confirmed in decompiled/; call hierarchy inferred from xref analysis |
| `ELF_WriteCompleteObject` at `sub_1CF3720` (99 KB) | HIGH | Decompiled file present; 99,074 bytes is consistent with a very large function |
