# NVIDIA Debug Extensions

nvlink recognizes six proprietary debug sections that extend beyond the standard DWARF sections carried in CUDA device ELF objects. These sections provide SASS-level line information, register allocation debug data, register type annotations, embedded PTX source text, PTX-level debug information, and shared memory debug metadata. The linker processes these sections through a dedicated classification chain (`sub_1CED7C0` at `0x1CED7C0`), a section-to-offset dispatcher (`sub_1CEDD50` at `0x1CEDD50`), and three concatenation-based output writers (`sub_181B050`, `sub_181B160`, `sub_181B270`). During Mercury linking, each NVIDIA debug section acquires a `.nv.merc.` prefix and is dispatched through a parallel code path (`sub_1CF1690` at `0x1CF1690`).

For standard DWARF section processing, see [DWARF Processing](dwarf-processing.md). For line table merging details, see [Line Table Merging](line-tables.md). For Mercury-specific debug sections, see [Mercury Debug Sections](mercury-debug.md).

## Key Facts

| Property | Value |
|---|---|
| Section classifier | `sub_1CED7C0` at `0x1CED7C0` (315 lines) |
| Section-to-offset mapper | `sub_1CEDD50` at `0x1CEDD50` (148 lines) |
| `.debug_frame` writer | `sub_181B050` at `0x181B050` (60 lines) |
| `.nv_debug_info_reg_sass` writer | `sub_181B160` at `0x181B160` (60 lines) |
| `.nv_debug_info_reg_type` writer | `sub_181B270` at `0x181B270` (60 lines) |
| Mercury section dispatcher | `sub_1CF1690` at `0x1CF1690` (545 lines) |
| Section filter (skip predicate) | `sub_1CECBB0` at `0x1CECBB0` (131 lines) |
| Debug prefix matcher | `sub_1672F50` at `0x1672F50` (uses `.nv_debug_` and `.debug_` prefixes) |
| PTX-level debug info parser | `sub_1D1D2F0` at `0x1D1D2F0` (handles `.nv_debug_info_ptx`) |
| Total NVIDIA-specific sections | 6 |

## NVIDIA Debug Section Catalog

| Section Name | Purpose | Writer Function | Mercury Output Name |
|---|---|---|---|
| `.nv_debug_line_sass` | SASS-level line number mappings | (line table pipeline) | `.nv.merc.nv_debug_line_sass` |
| `.nv_debug_info_reg_sass` | Register allocation debug data | `sub_181B160` | `.nv.merc.nv_debug_info_reg_sass` |
| `.nv_debug_info_reg_type` | Register type annotations | `sub_181B270` | `.nv.merc.nv_debug_info_reg_type` |
| `.nv_debug_ptx_txt` | Embedded PTX source text | (prefix-matched, opaque passthrough) | `.nv.merc.nv_debug_ptx_txt` |
| `.nv_debug_info_ptx` | PTX-level debug information | (DWARF parser at `sub_1D1D2F0`) | -- |
| `.nv_debug.shared` | Shared memory debug metadata | (filter predicate only) | -- |

## Section Descriptions

### .nv_debug_line_sass

SASS-level line number mappings that associate machine instruction addresses with source locations. Unlike the standard `.debug_line` section which maps PTX-level source positions, this section records line information at the SASS (native GPU ISA) level. The line table pipeline (documented in [Line Table Merging](line-tables.md)) produces this section when the builder index `a3 > 0`, using the same DWARF line program encoding as `.debug_line` but with SASS instruction addresses as the program counter values.

In the section-to-offset mapper (`sub_1CEDD50`), `.nv_debug_line_sass` maps to context offset `+88`, adjacent to `.debug_line` at `+80`. The Mercury dispatcher (`sub_1CF1690`) recognizes both the bare name and the `.nv.merc.nv_debug_line_sass` prefixed variant, assigning both to the same slot at `+88`.

### .nv_debug_info_reg_sass

Register allocation debug information that records which hardware registers are assigned to which variables or temporaries at each program point. This section is emitted by the SASS-level code generator (ptxas) and carried through the linker as an opaque data blob. The linker concatenates per-CU fragments from multiple input objects via `sub_181B160`.

In the section-to-offset mapper, `.nv_debug_info_reg_sass` maps to context offset `+96`. The writer reads from a linked list at struct offset `+408`, accumulates total size from `+424`, and writes the concatenated result to the buffer at `+416`.

### .nv_debug_info_reg_type

Register type debug information that annotates each register with its data type (integer, float, predicate, etc.) and bit width. This section complements `.nv_debug_info_reg_sass` by providing type classification rather than allocation location. The linker concatenates per-CU fragments via `sub_181B270`.

In the section-to-offset mapper, `.nv_debug_info_reg_type` maps to context offset `+104`. The writer reads from a linked list at struct offset `+432`, accumulates total size from `+448`, and writes the concatenated result to the buffer at `+440`.

### .nv_debug_ptx_txt

Embedded PTX source text carried verbatim through the linker. This section contains the raw PTX assembly text of the compilation unit, enabling debuggers to display PTX source alongside SASS disassembly. The section classifier (`sub_1CED7C0`) uses the prefix-matching function `sub_44E3A0` rather than exact `strcmp`/`memcmp` to recognize this section, testing whether the section name starts with `.nv_debug_ptx_txt`. This is the only NVIDIA debug section matched by prefix rather than exact name in the classifier.

The section is passed through as opaque data -- there is no dedicated writer or parser for the content. The Mercury variant `.nv.merc.nv_debug_ptx_txt` is similarly prefix-matched in `sub_1CED0E0` during output emission.

### .nv_debug_info_ptx

PTX-level debug information encoded in a DWARF-like compilation unit format. Unlike the opaque passthrough sections, this section is actively parsed by the DWARF subsystem at `sub_1D1D2F0`. The parser processes `.nv_debug_info_ptx` through the same compilation unit loop as `.debug_info`, reading DWARF headers (length, version, abbreviation offset, pointer size) and dispatching to `sub_1D1BE80` for attribute processing.

At `sub_1D1D2F0` line 348--362, the parser checks:
```c
if (memcmp(section_name, ".debug_info", 12) == 0)
    goto process_cu;
if (memcmp(section_name, ".nv_debug_info_ptx", 19) == 0)
    goto process_cu;
```

This means `.nv_debug_info_ptx` uses the same DWARF abbreviation/attribute/form encoding as standard `.debug_info`, but contains PTX-level scope, variable, and type information rather than source-level debug data.

This section has a single xref at `0x1D1D6B3` in `sub_1D1D2F0` and does not appear in the Mercury namespace -- it is consumed during linking and its information is folded into the standard debug sections.

### .nv_debug.shared

Shared memory debug metadata, used during section filtering to exclude shared-memory sections from certain link phases. The section filter predicate `sub_1CECBB0` at `0x1CECBB0` checks for this name (via exact `strcmp` at line 72) and returns 0 (skip) when encountered. This prevents shared memory debug sections from being treated as relocatable content during the merge phase.

The string `.nv_debug.shared` is referenced from three functions: `sub_4377B0` (at `0x437946`), `sub_437BB0` (at `0x437D76`), and `sub_1CECBB0` (at `0x1CECC3E`). The first two are in the ELF section classifier subsystem, while the third is the section filter.

## Section Classifier: sub_1CED7C0

The section name classifier is a chain of `memcmp`/`strcmp` calls that identifies whether a given section is a recognized debug section. It accepts a linker context pointer (`a1`) and a section header record pointer (`a2`), resolves the section name via `sub_448590`, and returns 1 if the section is any recognized debug section, or 0 otherwise.

### Recognition Order

The classifier checks section names in this fixed order:

1. `.debug_abbrev` (memcmp, 14 bytes)
2. `.debug_aranges` (memcmp, 15 bytes)
3. `.debug_frame` (memcmp, 13 bytes)
4. `.debug_info` (memcmp, 12 bytes)
5. `.debug_loc` (memcmp, 11 bytes)
6. `.debug_macinfo` (memcmp, 15 bytes)
7. `.debug_pubnames` (memcmp, 16 bytes)
8. `.debug_pubtypes` (strcmp)
9. `.debug_ranges` (strcmp)
10. `.debug_str` (strcmp)
11. `.nv_debug_info_reg_sass` (strcmp)
12. `.nv_debug_info_reg_type` (strcmp)
13. `.nv_debug_ptx_txt` (prefix match via `sub_44E3A0`)
14. `.debug_line` (strcmp)
15. `.nv_debug_line_sass` (strcmp)

The first seven checks use `memcmp` with an explicit length. This means a section name with extra characters after the matched prefix would still match (e.g., `.debug_info.dwo` would match the `.debug_info` check). The remaining checks use `strcmp` for exact matching, except `.nv_debug_ptx_txt` which uses the prefix-matching helper.

### Section Type Preprocessing

Before each name comparison, the function inspects the section header's `sh_type` field (`a2[1]`). The magic constants `1879048198` through `1879048292` correspond to NVIDIA-specific ELF section types (`SHT_LOOS`-based ranges). A bitmask `0x5D05` is used to quickly classify section types into "possibly debug" or "definitely not debug" categories, avoiding expensive string comparisons for clearly non-debug sections.

## Section-to-Offset Mapper: sub_1CEDD50

The section-to-offset mapper accepts a context object (`a1`), a section header record (`a2`), and a section index (`a3`), and returns the pointer stored at the context slot corresponding to the given section name. This function serves as the lookup mechanism that converts a section name into the accumulated data pointer for that section's concatenation buffer.

### Offset Map

| Section Name | Context Offset | Content |
|---|---|---|
| `.debug_line` | `+80` | Standard DWARF line program data |
| `.debug_frame` | `+72` | Standard DWARF frame unwind data |
| `.nv_debug_line_sass` | `+88` | SASS line number data |
| `.nv_debug_info_reg_sass` | `+96` | Register allocation data |
| `.nv_debug_info_reg_type` | `+104` | Register type data |
| `.debug_info` | `+112` | Standard DWARF compilation unit data |
| `.debug_loc` | `+120` | Standard DWARF location list data |

For any section name not in this table, the function falls through to `sub_464DB0` which performs a generic hash-map lookup on the linker context's section map at `a1 + 8`.

### Lookup Order

The mapper checks names in this order: `.debug_line`, `.debug_frame`, `.nv_debug_line_sass`, `.debug_info`, `.debug_loc`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`. The ordering differs from the classifier because this function optimizes for the sections that are most frequently looked up during concatenation.

## Concatenation Writers

Three functions with identical structure handle the final emission of concatenated debug sections into the output ELF. Each function walks a linked list of per-CU data fragments collected during the input parsing phase, allocates a single contiguous buffer, copies all fragments into it via `memcpy`, frees the fragment nodes, and registers the result as an output ELF section via `sub_434BC0`/`sub_434290`.

### Common Algorithm

```
writer(context, elf_writer):
    list_head = context[linked_list_offset]
    total_size = context[size_offset]
    
    // Flatten the linked list
    flat_list = flatten(list_head)          // sub_4649E0
    
    // Allocate output buffer
    alloc_ctx = get_allocator(list_head, elf_writer)  // sub_44F410
    buffer = allocate(alloc_ctx, total_size)           // sub_4307C0
    if (!buffer) fatal_error(alloc_ctx, total_size)    // sub_45CAC0
    context[buffer_offset] = buffer
    
    // Concatenate fragments
    write_pos = 0
    for node in flat_list:
        data = node->data    // +0: pointer to fragment bytes
        size = node->size    // +8: fragment byte count (uint32)
        memcpy(buffer + write_pos, data, size)
        write_pos += size
        free(node->data)     // sub_431000
        free(node)           // sub_431000
    
    // Register output section
    section_id = create_section(elf_writer, section_name, 0, 1, 0)
    emit_section(elf_writer, section_id, buffer, 1, total_size)
```

### Writer Instance Table

| Function | Address | Section Name | List Offset | Size Offset | Buffer Offset |
|---|---|---|---|---|---|
| `sub_181B050` | `0x181B050` | `.debug_frame` | `+384` | `+400` | `+392` |
| `sub_181B160` | `0x181B160` | `.nv_debug_info_reg_sass` | `+408` | `+424` | `+416` |
| `sub_181B270` | `0x181B270` | `.nv_debug_info_reg_type` | `+432` | `+448` | `+440` |

The three functions are byte-for-byte identical in structure, differing only in the struct field offsets and the section name string passed to `sub_434BC0`. The stride between consecutive instances is exactly 24 bytes in the context structure (`+384` to `+408` to `+432` for the list head; `+400` to `+424` to `+448` for the size; `+392` to `+416` to `+440` for the buffer).

### Fragment Node Layout

Each node in the linked list has this structure:

| Offset | Size | Field |
|---|---|---|
| `+0` | 8 | `next` pointer (NULL for tail) |
| `+8` | 8 | Pointer to inner data record |

The inner data record at the pointer from `+8`:

| Offset | Size | Field |
|---|---|---|
| `+0` | 8 | Pointer to raw section bytes |
| `+8` | 4 | Byte count of this fragment |

After concatenation, both the inner data record's byte pointer and the inner record itself are freed via `sub_431000`. The flattened list returned by `sub_4649E0` is freed via `sub_464520`.

## Mercury Section Dispatcher: sub_1CF1690

The Mercury section dispatcher (`sub_1CF1690` at `0x1CF1690`, 545 lines) handles section recognition and slot assignment for debug sections in Mercury-format ELF objects. It recognizes both bare section names (e.g., `.debug_frame`) and Mercury-prefixed names (e.g., `.nv.merc.debug_frame`), dispatching each to the same context slot. A flag byte at context offset `+432` controls whether Mercury-prefixed sections are accepted.

### Dispatch Table

For each section, the dispatcher first checks the bare name. If the bare name does not match but the section has the Mercury attribute flag (`byte +11 bit 4` set in the section header), the dispatcher tries the `.nv.merc.` prefixed name.

| Bare Name | Mercury Name | Context Slot |
|---|---|---|
| `.debug_frame` | `.nv.merc.debug_frame` | `+72` |
| `.debug_line` | `.nv.merc.debug_line` | `+80` |
| `.nv_debug_line_sass` | `.nv.merc.nv_debug_line_sass` | `+88` |
| `.nv_debug_info_reg_sass` | `.nv.merc.nv_debug_info_reg_sass` | `+96` |
| `.nv_debug_info_reg_type` | `.nv.merc.nv_debug_info_reg_type` | `+104` |
| `.debug_info` | `.nv.merc.debug_info` | `+112` |
| `.debug_loc` | `.nv.merc.debug_loc` | `+120` |

When a section matches, the dispatcher stores the per-CU data record pointer (`v17`, a 64-byte zero-initialized record allocated via `sub_4307C0`) into the context slot. The function returns 0 on successful dispatch and 2 when the section was already known (duplicate).

### Mercury Acceptance Gate

The flag at context offset `+432` acts as a gate for Mercury-prefixed sections. When this byte is zero, the dispatcher skips the Mercury-prefixed name checks and only recognizes bare names. When non-zero, both bare and prefixed names are accepted. This allows the linker to selectively enable Mercury debug section handling based on the link mode (standard CUDA vs. Mercury/capmerc).

## Section Filter Predicate: sub_1CECBB0

The section filter at `sub_1CECBB0` determines whether a section should be included in the output. For debug-related sections, it applies special rules:

- `.nv_debug.shared` is **always excluded** (returns 0). The function checks this name via exact `strcmp` when the section type is `SHT_PROGBITS` (type 8) and the name did not match `.nv.shared.` or `.nv.local.` or `.nv.global` prefixes.
- Standard string table sections (`.strtab`, `.shstrtab`) are excluded.
- Sections with NVIDIA-specific types in the `SHT_LOOS` range (`1879048193`--`1879048326`) are selectively included based on a bitmask filter (`0x34B`).
- Debug sections (type 1 or types in the `1879048198`--`1879048292` range) are included only if the section header's flag byte at `+8` has bit 2 set.

## Prefix Detection: sub_1672F50

The function `sub_1672F50` at `0x1672F50` uses the string constants `.nv_debug_` (10 characters) and `.debug_` (7 characters) as prefix tests to identify whether a section belongs to the debug namespace. This is used during the output section naming phase to decide whether a section name needs the Mercury `.nv.merc.` prefix transformation. The prefix strings at `0x226B814` and `0x226B81F` respectively have single xrefs into this function.

## Integration with DWARF Subsystem

The NVIDIA debug extensions integrate with the standard DWARF processing pipeline at several points:

1. **Input parsing**: The section classifier (`sub_1CED7C0`) identifies NVIDIA debug sections alongside standard DWARF sections, allowing the input parser to collect them into the appropriate linked lists.

2. **Section merging**: The section-to-offset mapper (`sub_1CEDD50`) provides fast context-slot lookups during the merge phase. Both standard and NVIDIA sections are accessed through the same dispatch pattern, with NVIDIA sections occupying slots `+88` through `+104` between the standard `.debug_line` slot (`+80`) and `.debug_info` slot (`+112`).

3. **Output emission**: The three concatenation writers run after all input objects have been processed. They flatten per-CU fragment lists into contiguous buffers and register them as output ELF sections. The `.debug_frame` writer (`sub_181B050`) uses the same algorithm as the NVIDIA-specific writers, confirming that `.debug_frame` is treated as a concatenation-based section rather than a DWARF-parsed section.

4. **Mercury output**: The ELF emitter (`sub_1CED0E0`) handles the mapping from bare names to `.nv.merc.` prefixed names when producing Mercury-format output. All six NVIDIA debug sections have Mercury variants, as do the eleven standard DWARF sections.
