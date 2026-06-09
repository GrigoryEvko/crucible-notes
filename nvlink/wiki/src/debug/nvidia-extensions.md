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

| Section Name | Description | Writer Function | Mercury Output Name |
|---|---|---|---|
| `.nv_debug_line_sass` | SASS-level line number mappings | (line table pipeline) | `.nv.merc.nv_debug_line_sass` |
| `.nv_debug_info_reg_sass` | Register allocation debug data | `sub_181B160` | `.nv.merc.nv_debug_info_reg_sass` |
| `.nv_debug_info_reg_type` | Register type annotations | `sub_181B270` | `.nv.merc.nv_debug_info_reg_type` |
| `.nv_debug_ptx_txt` | Embedded PTX source text | (prefix-matched, opaque passthrough) | `.nv.merc.nv_debug_ptx_txt` |
| `.nv_debug_info_ptx` | PTX-level debug information | (DWARF parser at `sub_1D1D2F0`) | — |
| `.nv_debug.shared` | Shared memory debug metadata | (filter predicate only) | — |

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

The section is passed through as opaque data — there is no dedicated writer or parser for the content. The Mercury variant `.nv.merc.nv_debug_ptx_txt` is similarly prefix-matched in `sub_1CED0E0` during output emission.

### .nv_debug_info_ptx

PTX-level debug information encoded in a DWARF-like compilation unit format. Unlike the opaque passthrough sections, this section is actively parsed by the DWARF subsystem at `sub_1D1D2F0`. The parser processes `.nv_debug_info_ptx` through the same compilation unit loop as `.debug_info`, reading DWARF headers (length, version, abbreviation offset, pointer size) and dispatching to `sub_1D1BE80` for attribute processing.

At `sub_1D1D2F0` line 348–362, the parser checks:
```c
if (memcmp(section_name, ".debug_info", 12) == 0)
    goto process_cu;
if (memcmp(section_name, ".nv_debug_info_ptx", 19) == 0)
    goto process_cu;
```

This means `.nv_debug_info_ptx` uses the same DWARF abbreviation/attribute/form encoding as standard `.debug_info`, but contains PTX-level scope, variable, and type information rather than source-level debug data.

This section has a single xref at `0x1D1D6B3` in `sub_1D1D2F0` and does not appear in the Mercury namespace — it is consumed during linking and its information is folded into the standard debug sections.

### .nv_debug.shared

Shared memory debug metadata, used during section filtering to exclude shared-memory sections from certain link phases. The section filter predicate `sub_1CECBB0` at `0x1CECBB0` checks for this name (via exact `strcmp` at line 72) and returns 0 (skip) when encountered. This prevents shared memory debug sections from being treated as relocatable content during the merge phase.

The string `.nv_debug.shared` is referenced from three functions: `sub_4377B0` (at `0x437946`), `sub_437BB0` (at `0x437D76`), and `sub_1CECBB0` (at `0x1CECC3E`). The first two are in the ELF section classifier subsystem, while the third is the section filter.

## Section Content Format

This section documents the on-disk byte layout, producer, and consumer for each of the six NVIDIA debug sections. The information is derived from the decompiled classifier (`sub_1CED7C0`), the section-to-offset mapper (`sub_1CEDD50`), the three concatenation writers (`sub_181B050` / `sub_181B160` / `sub_181B270`), and the DWARF CU parser (`sub_1D1D2F0`). All producer information refers to the upstream toolchain (ptxas); nvlink is always the consumer.

Note on scope: the six sections catalogued below are the **complete set** of NVIDIA-proprietary debug sections that nvlink v13.0.88 recognises by name. There are no `.debug_cuda_line`, `.debug_cuda_ranges`, or `.nv.debug.cuda_version` sections in this binary — those names do not appear in `nvlink_strings.json` and are not referenced by any decompiled function. The `.nv.uft` family (at strings `0x12422`, `0x12449`, `0x12595`) is the Unified Function Table mechanism for cross-module function pointer resolution, not a debug section, and is documented separately in [UFT: Unified Function Table](../elf/uft.md).

### .nv_debug_line_sass

| Property | Value |
|---|---|
| Section name | `.nv_debug_line_sass` |
| Purpose | SASS-level line number program mapping native GPU ISA PCs to source locations |
| Section type | `SHT_PROGBITS` (1) |
| Binary layout | DWARF-2/3 `.debug_line` line-number program: header (unit_length, version, header_length, minimum_instruction_length, default_is_stmt, line_base, line_range, opcode_base, standard_opcode_lengths, include_directories, file_names) followed by opcode stream. Uses NVIDIA-extended opcodes documented in [Line Table Merging](line-tables.md); the address register holds a SASS PC instead of a host address |
| Record granularity | Variable-length opcodes: `DW_LNS_*` (standard, 1-byte opcode + LEB128 operands), `DW_LNE_*` (extended, 0 + LEB128 length + opcode byte + operands), NVIDIA-specific extended opcodes for function index, inline stack, and correlation ID |
| Producer | ptxas debug info emitter — emitted only when PTX was compiled with `--generate-line-info` and a SASS-level builder index `a3 > 0` is active during merge |
| Consumer | nvlink line-table merge pipeline — recognised by classifier `sub_1CED7C0` (`strcmp` at decompiled line 296), slot-mapped by `sub_1CEDD50` to context offset `+88` (decompiled line 78, `memcmp(..., ".nv_debug_line_sass", 0x14u)`), then merged by the same DWARF line-program merger used for `.debug_line` |
| Concatenation writer | None; line tables are *merged* (re-encoded), not concatenated. See [Line Table Merging](line-tables.md) for the merger function addresses |
| Mercury variant | `.nv.merc.nv_debug_line_sass` at string `0x2458431`, dispatched to the same slot `+88` by `sub_1CF1690` at decompiled line 490 |
| DWARF relationship | Parallel to `.debug_line`: identical opcode encoding but distinct address space (SASS PCs instead of source PCs). Both occupy adjacent slots (`+80` vs `+88`) in the context structure |
| String table | `.nv.merc.nv_debug_line_sass` at `0x2458431`; bare name appears as a suffix of this string, matched by `memcmp(..., 20)` in the classifier (length 20 = strlen + NUL) |

### .nv_debug_info_reg_sass

| Property | Value |
|---|---|
| Section name | `.nv_debug_info_reg_sass` |
| Purpose | Register allocation map: records which SASS hardware register holds which PTX virtual register or source variable at each program point |
| Section type | `SHT_PROGBITS` (1) |
| Binary layout | Opaque concatenation-friendly byte stream. nvlink does not interpret the contents; it only concatenates per-CU fragments byte-for-byte. ptxas-internal format, typically a sequence of `(reg_id, pc_begin, pc_end, location_expr)` records, but nvlink never parses individual records |
| Record granularity | Not known to nvlink — the entire per-CU blob is treated as a flat byte array with a single 32-bit length field at fragment-node offset `+8` |
| Producer | ptxas register allocator after final assignment pass — one blob per compilation unit carried through the cubin into nvlink |
| Consumer | nvlink concatenation writer `sub_181B160` at `0x181B160`. Recognised by classifier `sub_1CED7C0` via `strcmp` at decompiled line 246 (`if (!strcmp(v18, ".nv_debug_info_reg_sass")) return 1;`). Slot-mapped by `sub_1CEDD50` to context offset `+96` via `strcmp` at decompiled line 134 |
| Concatenation writer | `sub_181B160` at `0x181B160` (60 lines). Reads linked-list head at context offset `+408`, total size at `+424`, writes output buffer pointer to `+416`, emits section name literal `".nv_debug_info_reg_sass"` to `sub_434BC0` (decompiled line 58) |
| Mercury variant | `.nv.merc.nv_debug_info_reg_sass` at `0x2458450`, dispatched to slot `+96` by `sub_1CF1690` at decompiled line 441 |
| DWARF relationship | Orthogonal to standard DWARF: no DW_AT attribute or DIE references into this section. Consumers (debuggers) cross-correlate via SASS PC ranges that also appear in `.debug_info`'s `DW_AT_low_pc` / `DW_AT_high_pc` attributes |
| String table | `.nv_debug_info_reg_sass` at `0x241282C` |

### .nv_debug_info_reg_type

| Property | Value |
|---|---|
| Section name | `.nv_debug_info_reg_type` |
| Purpose | Register type annotations: records the data type (integer, float, predicate) and bit width (8/16/32/64/128) for each SASS hardware register at each program point |
| Section type | `SHT_PROGBITS` (1) |
| Binary layout | Opaque concatenation-friendly byte stream, same treatment as `.nv_debug_info_reg_sass`. ptxas-internal record format, likely `(reg_id, type_code, bit_width, pc_begin, pc_end)` tuples, but nvlink does not parse individual records |
| Record granularity | Not known to nvlink — treated as a flat byte array per CU |
| Producer | ptxas register allocator — emitted in parallel with `.nv_debug_info_reg_sass`, with matching record counts |
| Consumer | nvlink concatenation writer `sub_181B270` at `0x181B270`. Recognised by classifier `sub_1CED7C0` via `strcmp` at decompiled line 254. Slot-mapped by `sub_1CEDD50` to context offset `+104` via `strcmp` at decompiled line 145 |
| Concatenation writer | `sub_181B270` at `0x181B270` (60 lines, byte-for-byte structural clone of `sub_181B160`). Reads linked-list head at context offset `+432`, total size at `+448`, writes output buffer pointer to `+440`, emits section name literal `".nv_debug_info_reg_type"` to `sub_434BC0` (decompiled line 58) |
| Mercury variant | `.nv.merc.nv_debug_info_reg_type` at `0x2458470`, dispatched to slot `+104` by `sub_1CF1690` at decompiled line 420 |
| DWARF relationship | Orthogonal to standard DWARF. Paired with `.nv_debug_info_reg_sass` — one provides the *where* (which hardware register), the other the *what* (type signature) |
| String table | `.nv_debug_info_reg_type` at `0x2412844` |

### .nv_debug_ptx_txt

| Property | Value |
|---|---|
| Section name | `.nv_debug_ptx_txt` (prefix-matched; accepts any section whose name begins with this string) |
| Purpose | Embedded PTX source text carried verbatim, enabling debuggers to display PTX assembly alongside SASS disassembly |
| Section type | `SHT_PROGBITS` (1) |
| Binary layout | Raw, uncompressed 7-bit ASCII PTX source code with Unix-style (LF) line endings. No framing, no length prefix, no NUL terminator — the section size equals the PTX text length in bytes |
| Record granularity | Byte stream (not record-oriented). Each `.nv_debug_ptx_txt*` section contains exactly one compilation unit's PTX text |
| Producer | ptxas front-end — copies the PTX input stream into the output cubin as an opaque blob before parsing |
| Consumer | nvlink classifier `sub_1CED7C0` recognises this section via `sub_44E3A0(".nv_debug_ptx_txt", section_name)` at decompiled line 279 — the only prefix match in the entire classifier. This means section names like `.nv_debug_ptx_txt.main`, `.nv_debug_ptx_txt_v2`, or `.nv_debug_ptx_txt.CU42` all classify as debug sections |
| Concatenation writer | None. Passed through as opaque data by the generic section emitter — there is no dedicated slot in the context structure, no entry in the section-to-offset mapper (`sub_1CEDD50`), and no NVIDIA-specific merge logic |
| Mercury variant | `.nv.merc.nv_debug_ptx_txt` at `0x2458403`, recognised as a prefix match by `sub_1CED0E0` during Mercury output emission |
| DWARF relationship | Parallel to `.debug_str`: both carry pure text data that debuggers reference by offset. Unlike `.debug_str`, `.nv_debug_ptx_txt` is not pointed to by any DWARF form value — debuggers locate it by section name |
| String table | Literal `.nv_debug_ptx_txt` is embedded as a suffix inside `.nv.merc.nv_debug_ptx_txt` at `0x2458403`; the classifier references the string through an offset computation rather than a standalone entry |
| Prefix rationale | The prefix match allows ptxas to emit multiple `.nv_debug_ptx_txt.<suffix>` variants (e.g., per-entry-function PTX) without nvlink requiring an update. All variants share the same passthrough behaviour |

### .nv_debug_info_ptx

| Property | Value |
|---|---|
| Section name | `.nv_debug_info_ptx` |
| Purpose | PTX-level debug information encoded as a DWARF compilation unit. Complements `.debug_info` (which targets source-level debugging) with PTX-level scopes, virtual registers, and types |
| Section type | `SHT_PROGBITS` (1) |
| Binary layout | DWARF-2/3 compilation unit header + DIE tree: 4-byte `unit_length`, 2-byte `version`, 4-byte `debug_abbrev_offset` (indexes into `.debug_abbrev`), 1-byte `address_size`, followed by a tree of Debugging Information Entries encoded via the referenced abbreviation table. Identical encoding to `.debug_info` — only the semantic content (PTX-scoped rather than source-scoped) differs |
| Record granularity | DWARF compilation unit header (11 bytes for DWARF-3) followed by a DIE tree terminated by a null DIE |
| Producer | ptxas debug info emitter — emitted only when the PTX input included `.file`/`.loc` directives and debug mode is enabled. One CU per input PTX compilation unit |
| Consumer | nvlink DWARF CU parser `sub_1D1D2F0` at `0x1D1D2F0`. The parser dispatches on section name at decompiled line 348 (`memcmp(a4, ".debug_info", 0xCu) == 0`) and line 352–361 (12-byte unrolled comparison against `".nv_debug_info_ptx"`, length 19). Both branches fall into `LABEL_63` which invokes `sub_1D1BE80` for DIE processing. This is the **only** NVIDIA debug section that nvlink actively *parses* (rather than concatenates or passes through) |
| CU header processing | `sub_1D1D2F0` reads the 4-byte unit length at offset `+192`/`+200`, the 2-byte version at `+204`, the 4-byte abbreviation offset at `+212`, the 1-byte pointer size at `+208`, and stores the CU base pointer at context `+168`. The abbreviation offset is matched against a cached abbreviation table list at context `+128` (each entry is 32 bytes) to locate the appropriate abbreviation decoder |
| Concatenation writer | None — parsed content is folded into the output's standard debug sections during DIE emission |
| Mercury variant | None — this section is consumed during linking. No `.nv.merc.nv_debug_info_ptx` string exists in `nvlink_strings.json` |
| DWARF relationship | Parasitic on `.debug_abbrev`: its `debug_abbrev_offset` field indexes into the **same** `.debug_abbrev` section used by `.debug_info`. ptxas emits abbreviations that serve both CU types. Missing `.debug_abbrev` causes the linker to skip parsing (diagnostic at string `0x292878`: "skipping .debug_info section due to missing .debug_abbrev section") |
| String table | `.nv_debug_info_ptx` at `0x245E6D4`, single xref at `0x1D1D6B3` inside `sub_1D1D2F0` |

### .nv_debug.shared

| Property | Value |
|---|---|
| Section name | `.nv_debug.shared` |
| Purpose | Shared memory debug metadata — marker section that informs the filter/classifier subsystem about shared-memory debug layout. Carries no linkable content from nvlink's perspective |
| Section type | `SHT_PROGBITS` (8) — `v4 == 8` gate in `sub_1CECBB0` at decompiled line 35 |
| Binary layout | Not parsed by nvlink. The section is recognised by name but always excluded from the output. Its content is presumed to be a ptxas-internal record of per-entry-function shared memory layout (offsets, sizes, variable names), intended for debuggers that read the *input* cubin directly rather than the nvlink output |
| Record granularity | Not known to nvlink (excluded before content inspection) |
| Producer | ptxas shared-memory layout pass — emitted for entry functions that declare `.shared` variables when debug info is enabled |
| Consumer | **Exclusion**, not consumption. `sub_1CECBB0` at decompiled line 72 explicitly matches `strcmp(name, ".nv_debug.shared") == 0` and **returns 0** (skip this section). The section is therefore stripped from the output unconditionally |
| Concatenation writer | None — the section never reaches the writer phase |
| Mercury variant | None — no `.nv.merc.nv_debug.shared` string exists |
| DWARF relationship | Orthogonal. No DWARF form value references this section. It is a pure metadata marker that is filtered out during link |
| String table | `.nv_debug.shared` at `0x1D38995`, with three xrefs: `sub_4377B0` (`0x437946`), `sub_437BB0` (`0x437D76`), `sub_1CECBB0` (`0x1CECC3E`) |
| Filtering evidence | Decompiled `sub_1CECBB0` line 72: `if (!strcmp((const char *)sub_448590(v7, (unsigned int *)v6), ".nv_debug.shared")) return 0;`. The `return 0` path bypasses both the debug-section path (line 114–124) and the generic content path |

### Summary Matrix

| Section | Purpose | Layout | Producer (ptxas role) | Consumer in nvlink | Writer addr |
|---|---|---|---|---|---|
| `.nv_debug_line_sass` | SASS line program | DWARF line-number opcodes | debug info emitter | line table merger | (merged, not written) |
| `.nv_debug_info_reg_sass` | Register allocation | Opaque per-CU blob | register allocator | `sub_181B160` | `0x181B160` |
| `.nv_debug_info_reg_type` | Register type annotations | Opaque per-CU blob | register allocator | `sub_181B270` | `0x181B270` |
| `.nv_debug_ptx_txt` | Embedded PTX text | Raw ASCII | front-end passthrough | generic emitter (opaque) | (passthrough) |
| `.nv_debug_info_ptx` | PTX-level DWARF DIE tree | DWARF-3 CU + DIEs | debug info emitter | `sub_1D1D2F0` (DWARF parser) | (parsed into `.debug_info`) |
| `.nv_debug.shared` | Shared memory metadata | Not parsed | shared-mem layout pass | `sub_1CECBB0` (excluded) | (stripped) |

### Writer Context-Offset Layout

The three active concatenation writers (`sub_181B050` for `.debug_frame`, plus the two NVIDIA-specific writers) share a common 24-byte stride in the context structure. Each writer slot contains three fields:

```text
slot_base +  0:  QWORD  list_head       // linked list of fragment nodes
slot_base +  8:  QWORD  output_buffer   // allocated during writer run
slot_base + 16:  DWORD  total_size      // sum of fragment sizes
slot_base + 20:  DWORD  (padding or reserved)
```

Concrete slot bases in the linker context:

| Writer | List head | Output buffer | Total size | Delta from prev |
|---|---|---|---|---|
| `sub_181B050` (`.debug_frame`) | `+384` | `+392` | `+400` | — |
| `sub_181B160` (`.nv_debug_info_reg_sass`) | `+408` | `+416` | `+424` | +24 |
| `sub_181B270` (`.nv_debug_info_reg_type`) | `+432` | `+440` | `+448` | +24 |

This uniform stride confirms that the writer slots were laid out as a C struct of three consecutive `struct { list_head *head; void *buffer; uint32_t size; }` fields rather than allocated ad hoc, and strongly suggests the ptxas/nvlink shared header defines these debug buffers as a single aggregate type.

### Section-to-Offset Mapper Offsets

| Section | Context slot | Lookup order | Lookup method |
|---|---|---|---|
| `.debug_line` | `+80` | 1 | `memcmp(..., 0xC)` (12 bytes) |
| `.debug_frame` | `+72` | 2 | `memcmp(..., 0xD)` (13 bytes) |
| `.nv_debug_line_sass` | `+88` | 3 | `memcmp(..., 0x14)` (20 bytes) |
| `.debug_info` | `+112` | 4 | `memcmp(..., 0xC)` (12 bytes) |
| `.debug_loc` | `+120` | 5 | `memcmp(..., 0xB)` (11 bytes) |
| `.nv_debug_info_reg_sass` | `+96` | 6 | `strcmp` |
| `.nv_debug_info_reg_type` | `+104` | 7 | `strcmp` |

Sections not in the above table fall through to `sub_464DB0(*(QWORD **)(a1 + 8), a3)` — a generic hash-map lookup on the linker context's section map. The ordering places `memcmp`-checked names first (cheaper when the match succeeds early) and `strcmp`-checked names last.

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

Before each name comparison, the function inspects the section header's `sh_type` field (`a2[1]`). The magic constants `1879048198` (`0x70000006`, `SHT_CUDA_CONSTANT` — the generic constant-bank base) through `1879048292` (`0x70000064`, `SHT_CUDA_CONSTANT0`) correspond to NVIDIA-specific ELF section types in the `SHT_LOPROC` range (`0x70000000`..`0x7FFFFFFF`). A bitmask `0x5D05` is used to quickly classify section types into "possibly debug" or "definitely not debug" categories, avoiding expensive string comparisons for clearly non-debug sections.

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

```c
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
- Sections with NVIDIA-specific types in the `SHT_LOPROC` range (`1879048193` = `0x70000001` `SHT_CUDA_CALLGRAPH` through `1879048326` = `0x70000086` `SHT_CUDA_COMPAT`) are selectively included based on a bitmask filter (`0x34B`).
- Debug sections (type 1 or types in the `1879048198`--`1879048292` range) are included only if the section header's flag byte at `+8` has bit 2 set.

## Prefix Detection: sub_1672F50

The function `sub_1672F50` at `0x1672F50` uses the string constants `.nv_debug_` (10 characters) and `.debug_` (7 characters) as prefix tests to identify whether a section belongs to the debug namespace. This is used during the output section naming phase to decide whether a section name needs the Mercury `.nv.merc.` prefix transformation. The prefix strings at `0x226B814` and `0x226B81F` respectively have single xrefs into this function.

## Integration with DWARF Subsystem

The NVIDIA debug extensions integrate with the standard DWARF processing pipeline at several points:

1. **Input parsing**: The section classifier (`sub_1CED7C0`) identifies NVIDIA debug sections alongside standard DWARF sections, allowing the input parser to collect them into the appropriate linked lists.

2. **Section merging**: The section-to-offset mapper (`sub_1CEDD50`) provides fast context-slot lookups during the merge phase. Both standard and NVIDIA sections are accessed through the same dispatch pattern, with NVIDIA sections occupying slots `+88` through `+104` between the standard `.debug_line` slot (`+80`) and `.debug_info` slot (`+112`).

3. **Output emission**: The three concatenation writers run after all input objects have been processed. They flatten per-CU fragment lists into contiguous buffers and register them as output ELF sections. The `.debug_frame` writer (`sub_181B050`) uses the same algorithm as the NVIDIA-specific writers, confirming that `.debug_frame` is treated as a concatenation-based section rather than a DWARF-parsed section.

4. **Mercury output**: The ELF emitter (`sub_1CED0E0`) handles the mapping from bare names to `.nv.merc.` prefixed names when producing Mercury-format output. All six NVIDIA debug sections have Mercury variants, as do the eleven standard DWARF sections.

## Cross-References

**Internal (nvlink wiki) — debug subsystem:**

- [DWARF Processing](dwarf-processing.md) — Core DWARF-2/3 parsing pipeline, abbreviation table cache, vendor-specific attributes (`DW_AT_NV_*`), and form value decoding. Used by `.nv_debug_info_ptx` via `sub_1D1D2F0` and shared with standard `.debug_info` parsing
- [Line Table Merging](line-tables.md) — DWARF line program merging, NVIDIA extended opcodes, and the `.nv_debug_line_sass` SASS-level line-table pipeline that produces the merged output
- [Mercury Debug Sections](mercury-debug.md) — Mercury-specific debug section handling and the `.nv.merc.*` namespace; cross-reference for the Mercury dispatcher (`sub_1CF1690`) and acceptance gate at context offset `+432`
- [Debug Options](options.md) — CLI flags (`-g`, `-lineinfo`, `--strip-debug`) that control which NVIDIA debug sections are emitted by upstream ptxas and which survive nvlink processing

**Internal (nvlink wiki) — ELF and section management:**

- [NVIDIA Section Types](../elf/nvidia-sections.md) — Section type constants for `.nv_debug_*` sections in the CUDA section catalog (the `1879048198`--`1879048292` = `0x70000006`..`0x70000064` `SHT_LOPROC`-range constants that the classifier and filter use as a fast pre-check)
- [Section Catalog](../reference/section-catalog.md) — Alphabetical index entries #62--#66 covering all NVIDIA debug sections, including their full sh_type hex values and creation phase
- [Mercury ELF Sections](../mercury/elf-sections.md) — The Mercury container and DWARF mirror sections that carry debug data for sm100+ targets. Documents the parallel `.nv.merc.*` namespace
- [Section Merging](../linker/section-merging.md) — How debug sections are classified by `merge_elf` name-prefix dispatch and routed to the writers documented above
- [UFT: Unified Function Table](../elf/uft.md) — The `.nv.uft` / `.nv.uft.rel` / `.nv.uft.entry` family is unrelated to debug info despite naming similarity; it implements function-pointer indirection for cross-module calls

**Sibling wikis:**

- [ptxas: Debug Info](../../ptxas/output/debug-info.html) — ptxas-side generation of `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`, and SASS line tables
- [ptxas: ELF Emitter](../../ptxas/output/elf-emitter.html) — How ptxas emits debug sections into the cubin that nvlink later processes
- [cicc: Debug Info Pipeline](../../cicc/pipeline/debug-info-pipeline.html) — cicc's four-stage debug metadata lifecycle; cicc does not directly produce NVIDIA-specific sections but generates the LLVM debug metadata that ptxas converts to `.nv_debug_*` formats

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| Section classifier `sub_1CED7C0` recognizes 15 section names in documented order | HIGH | Decompiled file confirms sequential `memcmp`/`strcmp` chain: `.debug_abbrev` (memcmp 14 bytes) at line 50, `.debug_aranges` (memcmp 15 bytes) at line 76, with `0x5D05` bitmask at line 45 |
| Bitmask `0x5D05` for section type pre-filtering | HIGH | Decompiled: `(0x5D05uLL >> ((unsigned __int8)v4 - 6)) & 1` at line 45; magic constants `1879048198` (`0x70000006`) and `1879048292` (`0x70000064`) confirm the `SHT_LOPROC` range that the bitmask covers |
| Section-to-offset mapper `sub_1CEDD50` — all 7 offset values | HIGH | Decompiled: `.debug_line` -> `*(a1+80)`, `.debug_frame` -> `*(a1+72)`, `.nv_debug_line_sass` -> `*(a1+88)`, `.debug_info` -> `*(a1+112)`, `.debug_loc` -> `*(a1+120)`, `.nv_debug_info_reg_sass` -> `*(a1+96)`, `.nv_debug_info_reg_type` -> `*(a1+104)` — all exact |
| Three writers structurally identical, differing only in offsets and names | HIGH | Decompiled `sub_181B050`/`sub_181B160`/`sub_181B270` are byte-for-byte identical in call sequence; offsets +384/+408/+432, +400/+424/+448, +392/+416/+440 confirmed; section name strings `.debug_frame`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type` at `sub_434BC0` call |
| 6 NVIDIA-specific section names | HIGH | All confirmed in `nvlink_strings.json`: `.nv_debug_line_sass`, `.nv_debug_info_reg_sass` (`0x241282C`), `.nv_debug_info_reg_type` (`0x2412844`), `.nv_debug_ptx_txt`, `.nv_debug_info_ptx` (`0x245E6D4`), `.nv_debug.shared` (`0x1D38995`) |
| `.nv_debug_ptx_txt` matched by prefix via `sub_44E3A0` | MEDIUM | `sub_44E3A0` is a starts-with predicate confirmed by its use in `sub_1CECBB0` (decompiled line 38); its use for `.nv_debug_ptx_txt` in the classifier is documented but not individually verified in the dense `sub_1CED7C0` decompilation |
| `.nv_debug.shared` excluded by `sub_1CECBB0` (returns 0) | HIGH | Decompiled: `strcmp(..., ".nv_debug.shared")` at line 72, returns 0 when matched |
| Fragment node layout (next at +0, inner ptr at +8, data at inner+0, size at inner+8) | HIGH | Decompiled writers: `v15[1]` (inner at +8), `*(const void **)v17` (data at +0), `*(unsigned int *)(v17 + 8)` (size at +8), `*v15` (next at +0) |
| Mercury dispatcher `sub_1CF1690` handles 7 section slots | HIGH | Decompiled file present (16,049 bytes); slot assignments consistent with offset map |
| Prefix detection `sub_1672F50` uses `.nv_debug_` and `.debug_` prefixes | HIGH | String `.nv_debug_` at `0x226B814` confirmed; function takes 11 parameters matching wiki's DWARF emitter description |
| Fallback to `sub_464DB0` hash-map lookup | HIGH | Decompiled `sub_1CEDD50`: `return sub_464DB0(*(_QWORD **)(a1 + 8), a3)` at end of function |
| `.nv_debug_info_ptx` parsed by DWARF CU parser at `sub_1D1D2F0` | HIGH | String `.nv_debug_info_ptx` at `0x245E6D4`; decompiled `sub_1D1D2F0` has `memcmp` checks for both `.debug_info` and `.nv_debug_info_ptx` |
| `.nv_debug_info_ptx` shares CU header format with `.debug_info` (4+2+4+1 = 11 bytes for DWARF-3) | HIGH | Decompiled `sub_1D1D2F0` reads length at `+192`/`+200`, version at `+204`, abbrev offset at `+212`, pointer size at `+208`, base ptr stored at `+168`; both branches converge at `LABEL_63` calling `sub_1D1BE80` |
| Three writer slots form a 24-byte stride aggregate (list/buffer/size pattern) | HIGH | Offsets `+384/+392/+400`, `+408/+416/+424`, `+432/+440/+448` confirmed across all three writers; deltas exactly 24 bytes; size-field stored as 32-bit DWORD per `*(_DWORD *)(a1 + 400)` access pattern |
| `.nv_debug.shared` is excluded (not consumed) by `sub_1CECBB0` | HIGH | Decompiled line 72 unconditionally returns 0 on `strcmp(..., ".nv_debug.shared")` match, before any content inspection |
| `.nv_debug_ptx_txt` is the only prefix-matched section in the classifier | HIGH | All other classifier branches use `memcmp` with explicit length or `strcmp`; only line 279 of `sub_1CED7C0` uses `sub_44E3A0` (the starts-with predicate confirmed by reading its 22-line decompilation) |
| `sub_44E3A0` is a starts-with predicate, not a generic substring search | HIGH | 22-line decompilation: walks both strings in lockstep, returns `a2` at first NUL of `a1` (success) or 0 on first mismatch; classic strncmp-style prefix test |
| No `.debug_cuda_*` or `.nv.debug.cuda_version` sections exist in nvlink v13.0 | HIGH | `nvlink_strings.json` grep for `debug_cuda` and `cuda_version` returns no matches; only the 6 sections catalogued above are referenced |
| `.nv.uft` / `.nv.uft.rel` / `.nv.uft.entry` are NOT debug sections | HIGH | Strings `0x12422`, `0x12449`, `0x12595` are referenced from UFT processing functions, not from any debug subsystem function. UFT is documented in `elf/uft.md` |
| Section-to-offset mapper uses `memcmp` for first 5 names and `strcmp` for last 2 | HIGH | Decompiled `sub_1CEDD50`: `memcmp` at lines 51, 67, 78, 100, 111 for `.debug_line`, `.debug_frame`, `.nv_debug_line_sass`, `.debug_info`, `.debug_loc`; `strcmp` at lines 134, 145 for `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type` |
