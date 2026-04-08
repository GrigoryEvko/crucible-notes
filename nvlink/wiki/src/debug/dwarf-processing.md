# DWARF Processing

nvlink contains a complete DWARF-2/3 debug information processing subsystem at address range `0x1D10000`--`0x1D20570`. This subsystem parses, validates, and re-emits standard DWARF sections carried in CUDA device ELF objects, handling abbreviation tables, compilation units, type information, location expressions, and line number programs. The implementation supports both 32-bit and 64-bit address sizes and recognizes seven vendor-specific attribute extensions from four vendors: NVIDIA (`DW_AT_NV_general_flags`), MIPS/GCC (`DW_AT_MIPS_linkage_name`), GNU (`DW_AT_GNU_pubnames`), and PGI (`DW_AT_PGI_lbase`/`soffset`/`lstride`). Of these, `DW_AT_MIPS_linkage_name` and the PGI triplet have unique processing logic; the others are recognized for display but passed through opaquely. All DWARF encoding uses LEB128/ULEB128 variable-length integers, decoded through a shared codec subsystem with SSE-accelerated variants at `0x1D00000`--`0x1D0FFF0`.

This page documents the core parsing functions. For the NVIDIA-specific extensions and Mercury debug section variants, see [NVIDIA Debug Extensions](nvidia-extensions.md). For line-table merging during the link phase, see [Line Table Merging](line-tables.md).

## Key Facts

| Property | Value |
|---|---|
| DWARF subsystem range | `0x1D10000`--`0x1D20570` (~140 functions, ~0.6 MB) |
| LEB128 codec range | `0x1D00000`--`0x1D0FFF0` (~20 functions, ~0.5 MB) |
| DWARF versions supported | 2 and 3 |
| Address sizes | 4 bytes (32-bit) and 8 bytes (64-bit) |
| Abbreviation buffer | 2048 bytes initial, grows by 2x when full |
| Abbreviation entry size | 32 bytes per slot |
| Maximum attributes per DIE | 256 (hard limit with fatal error) |
| Vendor-specific attributes | 7 total: NVIDIA (1), MIPS/GCC (1), GNU (1), PGI (3), sentinel (1) |
| Section type classifier | `sub_12D4370` at `0x12D4370` |
| Top-level entry point | `sub_1D166F0` at `0x1D166F0` (allocates 95,968-byte context) |

## Standard DWARF Sections

nvlink processes the following standard DWARF sections from input ELF objects. The section type classifier at `sub_12D4370` assigns numeric IDs used internally to dispatch processing:

| Section Name | Type ID | Description |
|---|---|---|
| `.debug_info` | 1 | Compilation units and DIE trees |
| `.debug_loc` | 2 | Location lists for variables |
| `.debug_abbrev` | 3 | Abbreviation tables |
| `.nv_debug_ptx_txt` | 4 | NVIDIA PTX source text |
| `.debug_line` | 5 | Line number programs |
| `.debug_str` | 6 | String table for `DW_FORM_strp` |

Additional sections processed by the ELF emitter (`sub_1CED0E0` at `0x1CED0E0`) but not given classifier IDs include `.debug_frame`, `.debug_ranges`, `.debug_aranges`, `.debug_pubnames`, `.debug_pubtypes`, and `.debug_macinfo`. These are carried through as opaque blobs during linking and re-emitted in the output under the Mercury namespace prefix `.nv.merc.`:

| Output Section | Source Section |
|---|---|
| `.nv.merc.debug_info` | `.debug_info` |
| `.nv.merc.debug_abbrev` | `.debug_abbrev` |
| `.nv.merc.debug_line` | `.debug_line` |
| `.nv.merc.debug_str` | `.debug_str` |
| `.nv.merc.debug_frame` | `.debug_frame` |
| `.nv.merc.debug_loc` | `.debug_loc` |
| `.nv.merc.debug_ranges` | `.debug_ranges` |
| `.nv.merc.debug_aranges` | `.debug_aranges` |
| `.nv.merc.debug_pubnames` | `.debug_pubnames` |
| `.nv.merc.debug_pubtypes` | `.debug_pubtypes` |
| `.nv.merc.debug_macinfo` | `.debug_macinfo` |
| `.nv.merc.nv_debug_info_reg_sass` | SASS register debug info |
| `.nv.merc.nv_debug_line_sass` | SASS line numbers |
| `.nv.merc.nv_debug_ptx_txt` | PTX source text |
| `.nv.merc.nv_debug_info_reg_type` | Register type info |

## Top-Level Entry Point: sub_1D166F0

The top-level DWARF processing entry point allocates a 95,968-byte (`0x176E0`) context structure and dispatches to the section-level parser.

### Signature

```c
int64_t dwarf_process_sections(
    void*    section_data,     // a1: raw section bytes
    size_t   section_size,     // a2: byte count
    char*    section_name,     // a3: e.g. ".debug_info"
    uint64_t flags             // a4: processing flags
);
// Returns byte count processed, or -64 on allocation failure.
```

### Context Structure Layout

The function allocates a context via `malloc(0x176E0)` and initializes it before calling `sub_1D15E00` (the section-level dispatcher). Key offsets within this context:

| Offset | Type | Field |
|---|---|---|
| `+128` (`0x80`) | `void*` | Abbreviation table pointer |
| `+136` (`0x88`) | `uint64_t` | Abbreviation table capacity (bytes) |
| `+144` (`0x90`) | `uint8_t` | Abbreviation table valid flag |
| `+152` (`0x98`) | `uint64_t` | Current abbreviation index (1-based) |
| `+164` (`0xA4`) | `int32_t` | Nesting depth counter |
| `+168` (`0xA8`) | `void*` | Current CU data pointer |
| `+176` (`0xB0`) | `uint64_t` | Current CU data size |
| `+184` (`0xB8`) | `uint8_t` | Current CU data valid flag |
| `+192` (`0xC0`) | `int32_t` | CU total length |
| `+196` (`0xC4`) | `int32_t` | CU header size (11 for DWARF-2/3) |
| `+200` (`0xC8`) | `int32_t` | CU content length |
| `+204` (`0xCC`) | `int32_t` | DWARF version number |
| `+208` (`0xD0`) | `int32_t` | Address (pointer) size |
| `+212` (`0xD4`) | `int32_t` | Abbreviation section offset |
| `+216` (`0xD8`) | `int32_t` | Matched abbreviation base index |
| `+224` (`0xE0`) | `uint64_t` | Magic/state marker (`38110068`) |
| `+30104` | `int32_t` | End-of-data flag |
| `+30176` | `int32_t` | Format flags |
| `+30204` | `int32_t` | Stream position flag |
| `+30228` | `int32_t` | Extended format flag |

The magic value `38110068` stored at offset `+224` acts as a state sentinel: it is set at the start of each compilation unit's processing and cleared to zero when parsing is complete.

## Abbreviation Table Parser: sub_1D17C90

The abbreviation table parser (`sub_1D17C90` at `0x1D17C90`, 18,519 bytes, 675 decompiled lines) reads `.debug_abbrev` section data and builds an in-memory lookup table. Each abbreviation entry maps an abbreviation number to a DW_TAG code, a has-children flag, and a list of (attribute, form) pairs.

### Signature

```c
int dwarf_parse_abbrev_table(
    dwarf_context* ctx,         // a1: context with abbrev table storage
    uint64_t       section_base, // a2: start of .debug_abbrev data
    int            section_size, // a3: byte count
    int            verbose       // a4: 1 = print parsed entries to stdout
);
```

### Abbreviation Table Storage

The parser stores entries in a contiguous buffer at `ctx+128`. Initial allocation is 2048 bytes. When the buffer is full (entry count reaches `capacity >> 5`), the parser doubles the capacity, allocates a new buffer via `sub_1D16C20`, copies existing entries with `memcpy`, frees the old buffer, and swaps in the new one.

Each abbreviation entry is a 32-byte record:

```
Offset   Size   Field
------   ----   -----
+0       4      DW_TAG code (from first ULEB128 of attr/form pair loop)
+4       4      DW_FORM code (companion to the attribute)
+8       1      has_children flag (1 = DW_CHILDREN_yes)
+12      4      Number of attribute/form pairs for this abbreviation
+16      4      Byte offset within the .debug_abbrev section
+24      8      Pointer to heap-allocated (attr, form) pair array
```

The pair array at `+24` stores each attribute/form pair as an 8-byte record: 4 bytes for `DW_AT_*` code, 4 bytes for `DW_FORM_*` code. The maximum number of pairs per abbreviation is 256; exceeding this triggers a fatal error:

```
unexpectedly too many dwarf attributes for any DW_TAG entry!
```

### Parse Loop

The parser is a `while(1)` loop that reads ULEB128-encoded abbreviation numbers from the byte stream. For each non-zero abbreviation number:

1. Read the DW_TAG code (ULEB128) via `sub_1D1FAD0`.
2. Read the has-children byte (single byte after the tag).
3. Read attribute/form pairs in a loop until both attribute and form are zero (the null terminator).
4. Allocate heap storage for the pair array, copy from a 256-entry stack buffer (`v151`, 2048 bytes on stack).
5. Store the completed entry into the abbreviation table at the current index.
6. Increment the abbreviation index at `ctx+152`.

When `verbose` (a4) is non-zero, the parser prints each entry to stdout:

```
Contents of the .debug_abbrev section:

  Number  TAG
   1      0x11 DW_TAG_compile_unit      [has children]
   DW_AT_producer(0x25)          DW_FORM_strp(0xe)
   DW_AT_language(0x13)          DW_FORM_data1(0xb)
   ...
```

The DW_TAG code-to-name lookup uses the string table at `off_245F080`, which contains 67 entries (indices 0 through `0x42`). Tag codes beyond this range produce `<unknown>`.

## DW_FORM Name Lookup: sub_1D16C60

A simple switch-based lookup (`sub_1D16C60` at `0x1D16C60`, 80 lines) that maps DWARF form codes to their string names. The complete mapping:

| Code | Name | Encoding |
|---|---|---|
| 1 | `DW_FORM_addr` | Target address (4 or 8 bytes based on CU pointer size) |
| 3 | `DW_FORM_block2` | 2-byte length + data block |
| 4 | `DW_FORM_block4` | 4-byte length + data block |
| 5 | `DW_FORM_data2` | 2-byte unsigned integer |
| 6 | `DW_FORM_data4` | 4-byte unsigned integer |
| 7 | `DW_FORM_data8` | 8-byte unsigned integer |
| 8 | `DW_FORM_string` | Null-terminated inline string |
| 9 | `DW_FORM_block` | ULEB128 length + data block |
| 10 | `DW_FORM_block1` | 1-byte length + data block |
| 11 | `DW_FORM_data1` | 1-byte unsigned integer |
| 12 | `DW_FORM_flag` | 1-byte boolean |
| 13 | `DW_FORM_sdata` | Signed LEB128 |
| 14 | `DW_FORM_strp` | 4-byte offset into `.debug_str` |
| 15 | `DW_FORM_udata` | Unsigned LEB128 |
| 16 | `DW_FORM_ref_addr` | Address-sized offset into `.debug_info` |
| 17 | `DW_FORM_ref1` | 1-byte CU-relative reference |
| 18 | `DW_FORM_ref2` | 2-byte CU-relative reference |
| 19 | `DW_FORM_ref4` | 4-byte CU-relative reference |
| 20 | `DW_FORM_ref8` | 8-byte CU-relative reference |
| 21 | `DW_FORM_ref_udata` | ULEB128 CU-relative reference |
| 22 | `DW_FORM_indirect` | ULEB128 form code followed by actual value |

This covers all forms defined in DWARF-2 and DWARF-3. Form code 2 (`DW_FORM_block2` gap in the standard) is not handled -- the standard reserves but does not assign it. Unknown form codes produce a diagnostic to stderr:

```
Unknown FORM value %d
```

## DW_AT Attribute Name Lookup: sub_1D16DF0

The attribute name lookup (`sub_1D16DF0` at `0x1D16DF0`, 330 lines) is a deeply nested if/else tree (not a switch) that maps DWARF attribute codes to string names. It covers the full DWARF-2/3 standard attribute set plus several vendor extensions.

### Standard Attributes (Codes 1--90)

| Code | Name | Code | Name |
|---|---|---|---|
| 1 | `DW_AT_sibling` | 46 | `DW_AT_stride_size` |
| 2 | `DW_AT_location` | 47 | `DW_AT_upper_bound` |
| 3 | `DW_AT_name` | 49 | `DW_AT_abstract_origin` |
| 9 | `DW_AT_ordering` | 50 | `DW_AT_accessibility` |
| 10 | `DW_AT_subscr_data` | 51 | `DW_AT_address_class` |
| 11 | `DW_AT_byte_size` | 52 | `DW_AT_artificial` |
| 12 | `DW_AT_bit_offset` | 53 | `DW_AT_base_types` |
| 13 | `DW_AT_bit_size` | 54 | `DW_AT_calling_convention` |
| 15 | `DW_AT_element_list` | 55 | `DW_AT_count` |
| 16 | `DW_AT_stmt_list` | 56 | `DW_AT_data_member_location` |
| 17 | `DW_AT_low_pc` | 57 | `DW_AT_decl_column` |
| 18 | `DW_AT_high_pc` | 58 | `DW_AT_decl_file` |
| 19 | `DW_AT_language` | 59 | `DW_AT_decl_line` |
| 20 | `DW_AT_member` | 60 | `DW_AT_declaration` |
| 21 | `DW_AT_discr` | 61 | `DW_AT_discr_list` |
| 22 | `DW_AT_discr_value` | 63 | `DW_AT_external` |
| 23 | `DW_AT_visibility` | 64 | `DW_AT_encoding` |
| 24 | `DW_AT_import` | 65 | `DW_AT_frame_base` |
| 25 | `DW_AT_string_length` | 66 | `DW_AT_friend` |
| 26 | `DW_AT_common_reference` | 67 | `DW_AT_identifier_case` |
| 27 | `DW_AT_comp_dir` | 68 | `DW_AT_macro_info` |
| 28 | `DW_AT_const_value` | 69 | `DW_AT_namelist_item` |
| 29 | `DW_AT_containing_type` | 70 | `DW_AT_priority` |
| 30 | `DW_AT_default_value` | 71 | `DW_AT_segment` |
| 32 | `DW_AT_inline` | 72 | `DW_AT_specification` |
| 33 | `DW_AT_is_optional` | 73 | `DW_AT_static_link` |
| 34 | `DW_AT_lower_bound` | 74 | `DW_AT_type` |
| 37 | `DW_AT_producer` | 75 | `DW_AT_use_location` |
| 39 | `DW_AT_prototyped` | 76 | `DW_AT_variable_parameter` |
| 42 | `DW_AT_return_addr` | 77 | `DW_AT_virtuality` |
| 44 | `DW_AT_start_scope` | 78 | `DW_AT_vtable_elem_location` |

### DWARF-3 Attributes (Codes 79--91)

| Code | Name |
|---|---|
| 79 | `DW_AT_associated` |
| 80 | `DW_AT_allocated` |
| 81 | `DW_AT_data_location` |
| 82 | `DW_AT_stride` |
| 83 | `DW_AT_entry_pc` |
| 84 | `DW_AT_extension` |
| 85 | `DW_AT_use_UTF8` |
| 86 | `DW_AT_ranges` |
| 87 | `DW_AT_trampoline` |
| 88 | `DW_AT_call_column` |
| 89 | `DW_AT_call_file` |
| 90 | `DW_AT_call_line` |
| 91 | `DW_AT_description` |

### Vendor Extensions

| Code (decimal) | Code (hex) | Name | Vendor | Unique Handling |
|---|---|---|---|---|
| 8199 | `0x2007` | `DW_AT_MIPS_linkage_name` | MIPS/GCC | Yes -- name priority, pubnames/pubtypes |
| 8500 | `0x2134` | `DW_AT_GNU_pubnames` | GNU | No -- name lookup only |
| 9987 | `0x2703` | `DW_AT_NV_general_flags` | NVIDIA | No -- name lookup only |
| 14848 | `0x3A00` | `DW_AT_PGI_lbase` | PGI | Yes -- DW_OP expression decoding |
| 14849 | `0x3A01` | `DW_AT_PGI_soffset` | PGI | Yes -- DW_OP expression decoding |
| 14850 | `0x3A02` | `DW_AT_PGI_lstride` | PGI | Yes -- DW_OP expression decoding |
| 16383 | `0x3FFF` | `DW_AT_hi_user` | Standard | No -- sentinel value |

Unknown attribute codes produce a diagnostic to stderr:

```
Unknown Attribute value %d
```

The if/else tree structure in `sub_1D16DF0` (not a compiler-generated switch table) suggests this was hand-written or came from a legacy code generator. The vendor attribute codes fall in the DWARF user-defined ranges: `0x2000`--`0x2FFF` for vendor-specific use (MIPS, GNU, NVIDIA), and `0x3A00`--`0x3FFF` for the upper user range (PGI, sentinel).

### DW_AT_MIPS_linkage_name (0x2007) -- Linkage Name Priority

`DW_AT_MIPS_linkage_name` is the most extensively handled vendor attribute in the DWARF subsystem. Originally defined by SGI for the MIPS ABI, it was adopted by GCC and Clang as the de facto standard for encoding the mangled (C++ linkage) name of a symbol before DWARF-4 introduced `DW_AT_linkage_name` (code 0x76). The CUDA toolchain emits it in device ELF objects for mangled kernel names.

nvlink gives `DW_AT_MIPS_linkage_name` **priority over `DW_AT_name`** when extracting the canonical name of a DIE. This affects three functions:

**DIE tree walker (`sub_1D1BE80`)**: When processing `DW_TAG_subprogram` (tag 46), the walker has a special check at the attribute dispatch level. If the current attribute is `DW_AT_name` (3) and the previous attribute stored in the DIE context (offset `+48`) was `DW_AT_MIPS_linkage_name` (8199), the walker **skips** the `DW_AT_name` extraction entirely -- the linkage name already captured is the canonical identifier. Conversely, if the attribute *is* `DW_AT_MIPS_linkage_name`, the walker proceeds directly to the name extraction path. The pseudocode for the relevant check:

```c
// Inside DIE tree walker, attribute dispatch for DW_TAG_subprogram (46):
if (current_attr == DW_AT_name) {
    if (die_ctx->prev_attr == DW_AT_MIPS_linkage_name)
        goto skip;   // linkage name already captured, ignore DW_AT_name
}
if (current_attr == DW_AT_MIPS_linkage_name) {
    goto extract_name;  // treat as the canonical name
}
```

**Pubnames emitter (`sub_1D19900`)** and **pubtypes emitter (`sub_1D193E0`)**: Both use an identical priority pattern when building the `.debug_pubnames` / `.debug_pubtypes` name index. For each abbreviation entry's attribute list, they check:

```c
if (attr == DW_AT_MIPS_linkage_name ||
    (attr == DW_AT_name && prev_captured_name != DW_AT_MIPS_linkage_name))
{
    // Extract name string from the stream, allocate arena copy
    prev_captured_name = attr;
}
```

This means: if a DIE has both `DW_AT_name` and `DW_AT_MIPS_linkage_name`, the mangled linkage name always wins. The `DW_AT_name` is only used as a fallback when no linkage name is present. After capturing via `DW_AT_MIPS_linkage_name`, encountering `DW_AT_name` has no effect on the stored name. This guarantees that pubnames/pubtypes entries use the mangled C++ name when available, which matches what host-side linkers and debuggers expect.

### DW_AT_GNU_pubnames (0x2134) -- GCC .debug_gnu_pubnames

`DW_AT_GNU_pubnames` is a boolean attribute added to `DW_TAG_compile_unit` DIEs by GCC when the `.debug_gnu_pubnames` section is present. This is the GCC extension for accelerated name lookup (later standardized as `.debug_names` in DWARF-5). nvlink recognizes the attribute name for display in verbose mode but does **not** perform any special processing on its value -- the attribute is decoded generically through the form value reader like any other boolean or constant. The `.debug_pubnames` section itself is carried through as an opaque blob in the Mercury output (`.nv.merc.debug_pubnames`).

### DW_AT_NV_general_flags (0x2703) -- NVIDIA GPU Function Properties

`DW_AT_NV_general_flags` at code 9987 (`0x2703`) is NVIDIA's sole custom DWARF attribute in the vendor extension range. It is used by the CUDA toolchain (cicc/ptxas) to annotate `DW_TAG_subprogram` DIEs with GPU-specific function properties in device ELF `.debug_info` sections.

Despite being the only NVIDIA-proprietary attribute, `DW_AT_NV_general_flags` has **no special handling** in the nvlink DWARF subsystem beyond the name lookup in `sub_1D16DF0`. The attribute value is:

- Decoded generically by the form value reader (`sub_1D1B540`) according to whatever `DW_FORM_*` is specified in the abbreviation table (typically `DW_FORM_data4` for a 32-bit flags word or `DW_FORM_data2`)
- Not examined, filtered, or modified by the DIE tree walker (`sub_1D1BE80`)
- Not referenced by the pubnames or pubtypes emitters
- Passed through opaquely to the Mercury output

The exact bit layout of the flags value was not determined from decompilation of nvlink alone -- the flags are produced by cicc and consumed by cuda-gdb and other NVIDIA debug tools. The attribute code `0x2703` falls in the `0x2000`--`0x3FFF` user-defined range (specifically in the `0x2700`--`0x27FF` sub-range that appears to be reserved for NVIDIA).

### PGI Extensions (0x3A00--0x3A02) -- Fortran Array Descriptors

The three PGI attributes reflect nvlink's lineage from the PGI (Portland Group / NVIDIA HPC SDK) compiler toolchain. They encode Fortran array descriptor components:

| Attribute | Code | Description |
|---|---|---|
| `DW_AT_PGI_lbase` | `0x3A00` | Lower bound base address of the array descriptor |
| `DW_AT_PGI_soffset` | `0x3A01` | Section (stride) offset within the descriptor |
| `DW_AT_PGI_lstride` | `0x3A02` | Element stride (distance between consecutive elements) |

These are typically encoded as `DW_FORM_block1` values containing DWARF location expressions (`DW_OP_*` sequences). The form value reader (`sub_1D1B540`) explicitly includes all three PGI codes in its location-expression decode check:

```c
// In sub_1D1B540, DW_FORM_block1 handler:
if (attr == DW_AT_location       ||   // 2
    attr == DW_AT_data_location   ||   // 81
    (attr - 14848) <= 2u          ||   // DW_AT_PGI_lbase/soffset/lstride
    attr == DW_AT_stride_size)         // 46
{
    // Invoke DW_OP expression decoder (sub_1D1A920) on block contents
}
```

This means the PGI array descriptor attributes are treated as first-class location expressions by the DWARF subsystem -- their block values are decoded through the full `DW_OP_*` interpreter (`sub_1D1A920`), producing human-readable location descriptions in verbose mode. This is the same treatment given to standard location attributes like `DW_AT_location` and `DW_AT_data_location`.

## DW_OP Expression Decoder: sub_1D1A920

The DW_OP expression decoder (`sub_1D1A920` at `0x1D1A920`, 15,580 bytes, 616 lines) parses DWARF location expressions and prints them into a string buffer. It handles the full set of DWARF-2/3 expression opcodes needed for GPU debug information.

### Signature

```c
uint64_t dwarf_decode_dw_op(
    uint32_t*   addr_size_ctx,   // a1: points to CU address size
    char**      section_name,    // a2: section name pointer (for debug_frame detection)
    int         expr_length,     // a3: byte count of expression data
    string_buf* output,          // a4: output string buffer
    int64_t     reserved1,       // a5
    int64_t     reserved2,       // a6
    void*       expr_data,       // a7: expression byte stream
    uint64_t    expr_capacity    // a8: bounds-checking limit
);
```

### Supported Opcodes

| Opcode(s) | Name | Description |
|---|---|---|
| `0x03` | `DW_OP_addr` | Push address constant (4 or 8 bytes based on CU address size) |
| `0x0C` | `DW_OP_const4u` | Push 4-byte unsigned constant |
| `0x10` | `DW_OP_constu` | Push ULEB128 unsigned constant |
| `0x18` | `DW_OP_xderef` | Extended dereference |
| `0x22` | `DW_OP_plus` | Addition |
| `0x23` | `DW_OP_plus_uconst` | Add ULEB128 constant |
| `0x30`--`0x4F` | `DW_OP_lit0`--`DW_OP_lit31` | Push literal 0--31 (opcode minus `0x30`) |
| `0x50`--`0x6F` | `DW_OP_reg0`--`DW_OP_reg31` | Name register 0--31 (opcode minus `0x50`) |
| `0x70`--`0x8F` | `DW_OP_breg0`--`DW_OP_breg31` | Register 0--31 plus signed LEB128 offset |
| `0x90` | `DW_OP_regx` | ULEB128 register number |
| `0x91` | `DW_OP_fbreg` | Frame base plus signed LEB128 offset |
| `0x92` | `DW_OP_bregx` | ULEB128 register + signed LEB128 offset |
| `0x94` | `DW_OP_deref_size` | Dereference with explicit byte size |
| `0x96` | `DW_OP_nop` | No operation |
| `0x9F` | `DW_OP_stack_value` | DWARF-4 stack value (marks TOS as the value) |

The `DW_OP_addr` handler dispatches on the CU address size: 4-byte addresses use format `"DW_OP_addr: 0x%x"`, while 8-byte addresses use `"DW_OP_addr: 0x%llx"`.

For `DW_OP_bregx` (opcode `0x92`), the decoder has a special code path for `.debug_frame` sections. When the section name matches `"debug_frame"` (compared against the suffix of `".nv.merc.debug_frame"`), it decodes the register number through `sub_1D17460` which maps register numbers to NVIDIA-specific register names with a 24-bit mask (`& 0xFFFFFF`). Otherwise it uses the generic ULEB128 decoder.

Multiple DW_OP operations within a single expression are separated by `"; "` in the output string.

## Form Value Reader: sub_1D1B540

The form value reader (`sub_1D1B540` at `0x1D1B540`, 9,243 bytes, 353 lines) reads and formats a single DWARF attribute value based on its DW_FORM code. This is the central dispatch for all attribute value decoding.

### Signature

```c
int64_t dwarf_read_form_value(
    dwarf_context* ctx,       // a1: parsing context (offset +52 = addr size, +56 = section name)
    void*          allocator, // a2: memory allocator context
    uint16_t       form,      // a3: DW_FORM_* code
    string_buf*    output,    // a4: output buffer for formatted value
    int64_t        reserved1, // a5
    int64_t        reserved2, // a6
    void*          data,      // a7: raw byte stream
    uint64_t       data_size, // a8: remaining bytes
    int64_t        slice_ctx  // a9: slice/validation context
);
// Returns number of bytes consumed from the data stream.
```

### Form Dispatch Table

| Form | Bytes Consumed | Reader | Output Format |
|---|---|---|---|
| `DW_FORM_addr` (1) / `DW_FORM_ref_addr` (16) | 4 or 8 (address size) | `sub_1D17560` (4-byte) or `sub_1D192F0` (8-byte) | `%x` or `%llx` |
| `DW_FORM_block2` (3) | 2 + N | `sub_1D18B20` (read uint16 length), then N bytes | `%5d byte block: %2x %2x ...` |
| `DW_FORM_block4` (4) | 4 + N | `sub_1D17560` (read uint32 length), then N bytes | `%10d byte block: %2x %2x ...` |
| `DW_FORM_data2` (5) | 2 | `sub_1D18B20` | `0x%llx` |
| `DW_FORM_data4` (6) | 4 | `sub_1D17560` | `0x%llx` |
| `DW_FORM_data8` (7) | 8 | `sub_1D192F0` | `0x%llx` |
| `DW_FORM_string` (8) / `DW_FORM_strp` (14) | strlen+1 | `sub_1D18B80` + `sub_1D175B0` | `%s` |
| `DW_FORM_block` (9) | ULEB128 + N | `sub_1D229C0` (read ULEB128 length), then N bytes | `%20lld byte block: %2x ...` |
| `DW_FORM_block1` (10) | 1 + N | `sub_1D17510` (read uint8 length), then N bytes | `%3d byte block: %2x ...` |
| `DW_FORM_data1` (11) | 1 | `sub_1D17510` | `0x%llx` |
| `DW_FORM_flag` (12) | 1 | `sub_1D19350` | `%d` |
| `DW_FORM_sdata` (13) | LEB128 | `sub_1D22B50` (signed LEB128) | `%lld` |
| `DW_FORM_udata` (15) / `DW_FORM_ref_udata` (21) | ULEB128 | `sub_1D229C0` (unsigned LEB128) | `%llu` |
| `DW_FORM_ref1` (17) | 1 | `sub_1D17510` | `<%x>` |
| `DW_FORM_ref2` (18) | 2 | `sub_1D18B20` | `<%x>` |
| `DW_FORM_ref4` (19) | 4 | `sub_1D17560` | `<%x>` |
| `DW_FORM_ref8` (20) | 8 | `sub_1D192F0` | `<%llx>` |
| `DW_FORM_indirect` (22) | -- | -- | Fatal: `exit(1)` |

For block forms (`DW_FORM_block1`, `DW_FORM_block4`, `DW_FORM_block`), after printing the hex dump the reader also invokes the DW_OP expression decoder (`sub_1D1A920`) to produce a human-readable interpretation. The decoded expression is appended in parentheses: `(%s)`.

The `DW_FORM_block1` reader has an additional dispatch based on the attribute code: for location-related attributes (`DW_AT_location` = 2, `DW_AT_data_member_location` = 56, `DW_AT_stride_size` = 46, `DW_AT_address_class` = 51, and PGI attributes 14848--14850), it invokes `sub_1D1A920` with the block contents. For `DW_AT_data_member_location` specifically, it passes the data through a different slice path to handle the member offset encoding.

Encountering `DW_FORM_indirect` triggers a fatal error with `exit(1)` and the message:

```
Warning: we should not get here! - DW_FORM_indirect
```

Any unrecognized form code triggers:

```
Error in get_form_value default
```

## Compilation Unit Parser: sub_1D1D2F0

The `.debug_info` section parser (`sub_1D1D2F0` at `0x1D1D2F0`, 9,191 bytes, 397 lines) iterates over compilation units (CUs) and dispatches to the DIE tree walker. This is called through a thin wrapper `sub_1D1DAE0` which normalizes the parameter order.

### Signature

```c
int dwarf_parse_debug_info(
    dwarf_context*  ctx,           // a1: DWARF context
    uint64_t        section_size,  // a2: total .debug_info size
    int64_t         string_table,  // a3: .debug_str base address
    const char*     section_name,  // a4: ".debug_info" or ".nv_debug_info_ptx"
    void*           allocator,     // a5: memory allocator
    uint8_t         alloc_flags,   // a6
    void*           data,          // a7: raw .debug_info bytes
    uint64_t        data_size,     // a8: byte count
    uint8_t         data_valid,    // a9: computed from a7 != NULL && a8 != 0
    uint8_t         verbose        // a10: 1 = print compilation unit headers
);
```

### Compilation Unit Header

Each compilation unit starts with an 11-byte header (DWARF-2/3 32-bit format):

```
Offset   Size   Field
------   ----   -----
+0       4      unit_length: total bytes after this field (excludes the 4-byte length itself)
+4       2      version: DWARF version (2 or 3)
+6       4      debug_abbrev_offset: byte offset into .debug_abbrev for this CU's table
+10      1      address_size: pointer size (4 or 8)
```

The parser reads these fields and stores them in the context:

```c
ctx->cu_total_length    = unit_length;       // +192
ctx->cu_header_size     = 11;                // +196 (constant for DWARF-2/3)
ctx->cu_content_length  = unit_length;       // +200
ctx->cu_version         = version;           // +204
ctx->cu_address_size    = address_size;      // +208
ctx->cu_abbrev_offset   = abbrev_offset;     // +212
```

When verbose mode is active (a10 != 0), the parser prints:

```
 Compilation Unit @ offset 0x%zx:
  Length:           %d
  Version:          %d
  Abbrev Offset:    %d
  Pointer Size:     %d
```

### Abbreviation Table Matching

After reading the CU header, the parser scans the abbreviation table (stored at `ctx+128`) to find the first entry whose byte offset matches the CU's `debug_abbrev_offset`. This establishes the base index for abbreviation lookups within this CU:

```c
for (int i = 1; i <= ctx->abbrev_count; i++) {
    abbrev_entry* entry = &ctx->abbrev_table[i];
    if (entry->section_offset == abbrev_offset) {
        ctx->matched_abbrev_base = i - 1;  // +216
        break;
    }
}
```

### DIE Tree Dispatch

After CU header parsing, the function reads the first ULEB128 from the CU content (the root DIE's abbreviation number) and allocates a 48-byte record for the CU's data pointers, then dispatches to `sub_1D1BE80` (the DIE tree walker) if the section name matches either `".debug_info"` or `".nv_debug_info_ptx"`.

After processing one CU, the parser advances `data` by `unit_length - 7` bytes and loops to process the next CU, continuing until all data is consumed.

## DIE Tree Walker: sub_1D1BE80

The DIE tree walker (`sub_1D1BE80` at `0x1D1BE80`, 27,583 bytes, 1,059 lines) recursively processes all Debug Information Entries within a compilation unit. For each DIE it:

1. Reads the abbreviation number (ULEB128).
2. Looks up the abbreviation entry to determine the DW_TAG, has-children flag, and attribute list.
3. For each attribute, calls the form value reader (`sub_1D1B540`) to consume and format the value.
4. If the DIE has children, recurses to process child DIEs.
5. A zero abbreviation number signals the end of a sibling chain (returns to parent scope).

In verbose mode, each DIE is printed as:

```
 <%d><%x>:  Abbrev Number: %d   (0x%02x %s)
```

where the fields are nesting depth, byte offset from CU start, abbreviation number, DW_TAG code, and DW_TAG name.

The walker recognizes DW_TAG values 5 (`DW_TAG_formal_parameter`) and 52 (`DW_TAG_variable`) as special cases for tracking function parameter and variable debug information through a separate codepath.

## LEB128 Codec Subsystem

The LEB128 codec at `0x1D00000`--`0x1D0FFF0` provides variable-length integer encoding/decoding used throughout the DWARF subsystem. It has four implementation tiers:

| Function | Address | Size | Description |
|---|---|---|---|
| `sub_1CFEDC0` | `0x1CFEDC0` | 55,417 B | LEB128 encoder, 32-bit ELF target |
| `sub_1D00790` | `0x1D00790` | 54,711 B | LEB128 encoder, 64-bit ELF target |
| `sub_1D02320` | `0x1D02320` | 25,838 B | LEB128 decoder, simple variant |
| `sub_1D03090` | `0x1D03090` | 27,414 B | LEB128 decoder, with validation |
| `sub_1D05880` | `0x1D05880` | 53,217 B | ULEB128 encoder |
| `sub_1D07900` | `0x1D07900` | 28,383 B | ULEB128 decoder |
| `sub_1D08D90` | `0x1D08D90` | 53,282 B | SSE-accelerated LEB128 encoder |
| `sub_1D0DFD0` | `0x1D0DFD0` | 69,653 B | SSE-accelerated LEB128 decoder |
| `sub_1D10120` | `0x1D10120` | 69,928 B | SSE-accelerated signed LEB128 decoder |
| `sub_1D13C80` | `0x1D13C80` | 48,315 B | SSE bulk LEB128 encoder |
| `sub_1D238D0` | `0x1D238D0` | 31,937 B | Multi-pass LEB128 encoder |
| `sub_1D0AF40` | `0x1D0AF40` | 17,016 B | LEB128 lookup table initializer |
| `sub_1D0B9A0` | `0x1D0B9A0` | 16,630 B | Compact LEB128 encoder for small values |

### Inline Decoders Used by DWARF

The DWARF parser calls two specific ULEB128/SLEB128 decoders for individual values:

- **`sub_1D229C0`** -- ULEB128 decoder. Returns the decoded unsigned value and stores the byte count consumed into an output parameter. Used for abbreviation numbers, form lengths, unsigned data.
- **`sub_1D22B50`** -- SLEB128 (signed LEB128) decoder. Returns the decoded signed value. Used for `DW_FORM_sdata` and `DW_OP_fbreg`/`DW_OP_breg*` offsets.
- **`sub_1D1FAD0`** -- Another ULEB128 decoder variant used in the abbreviation parser. Returns the decoded value and stores consumed byte count via a `pthread_mutexattr_t*` parameter (reused struct for alignment).

### SSE Acceleration

The SSE-accelerated encoders and decoders process 16 bytes at a time using SSE2 SIMD instructions (`_mm_load_si128`, `_mm_shuffle_epi8`, `_mm_and_si128`, `_mm_or_si128`, `_mm_srli_epi64`). They extract continuation bits in parallel across 16 LEB128 bytes, determine group boundaries, and decode/encode all values in a single pass. These are used for bulk operations on large DWARF sections, not for individual value decoding.

The signed SSE decoder (`sub_1D10120`, 69,928 bytes -- the largest function in the LEB128 subsystem) additionally handles sign extension for negative values, which requires detecting the sign bit position within each variable-length group.

## Helper Functions

| Function | Address | Size | Description |
|---|---|---|---|
| `sub_1D16C20` | `0x1D16C20` | ~200 B | Arena allocator wrapper (allocates via context arena) |
| `sub_1D17510` | `0x1D17510` | ~80 B | Read 1 byte (`uint8_t`) from stream, advance pointer |
| `sub_1D17560` | `0x1D17560` | ~80 B | Read 4 bytes (`uint32_t`) from stream, advance pointer |
| `sub_1D175B0` | `0x1D175B0` | ~100 B | Copy N bytes from stream to buffer |
| `sub_1D17C10` | `0x1D17C10` | ~120 B | Look up abbreviation entry by index from table |
| `sub_1D18B20` | `0x1D18B20` | ~80 B | Read 2 bytes (`uint16_t`) from stream, advance pointer |
| `sub_1D18B80` | `0x1D18B80` | ~100 B | Compute string length (strlen on stream) |
| `sub_1D192F0` | `0x1D192F0` | ~100 B | Read 8 bytes (`uint64_t`) from stream, advance pointer |
| `sub_1D19350` | `0x1D19350` | ~80 B | Read 1 byte as signed, advance pointer |
| `sub_1D193A0` | `0x1D193A0` | ~60 B | Bounds validation helper |
| `sub_1D17460` | `0x1D17460` | ~180 B | NVIDIA register name lookup (for `DW_OP_bregx` in `.debug_frame`) |
| `sub_1D17630` | `0x1D17630` | ~3,752 B | LEB128 decoder with 512-byte working buffer |
| `sub_1D1FAD0` | `0x1D1FAD0` | ~200 B | ULEB128 decoder for abbreviation parsing |

## Pubnames and Pubtypes Emitters

Two functions emit the `.debug_pubnames` and `.debug_pubtypes` lookup sections:

- **`sub_1D18EA0`** (`0x1D18EA0`, 5,152 bytes) -- `.debug_pubnames` emitter. Walks the abbreviation table, and for each DIE with a `DW_AT_name` attribute, emits an entry mapping the name to the DIE offset within `.debug_info`.

- **`sub_1D193E0`** (`0x1D193E0`, 6,101 bytes) -- `.debug_pubtypes` emitter. Similar structure but emits entries for type DIEs (those with `DW_TAG_base_type`, `DW_TAG_typedef`, etc.).

Both follow the DWARF-2/3 pubnames/pubtypes section format: a header with CU offset and CU size, followed by (offset, name) pairs terminated by a zero offset.

## Bounds Checking

The DWARF parser performs pervasive bounds checking through a consistent pattern. Each data access is guarded by three assertions on the context's data triple `(pointer, capacity, valid_flag)`:

1. **Null check**: `if (!pointer) fatal(ASSERT_NOT_NULL);`
2. **Valid flag**: `if (!valid_flag) fatal(ASSERT_VALID);`
3. **Bounds check**: `if (required_offset > capacity) fatal(ASSERT_BOUNDS);`

These correspond to the three error codes referenced as `dword_2A5F0D0` (null pointer), `dword_2A5F0B0` (invalid state), and `dword_2A5F0A0` (out of bounds). The assertions are implemented as calls to `sub_467460` which is the global diagnostic/assertion handler. This pattern appears on virtually every byte read throughout the DWARF subsystem, giving strong protection against malformed input but contributing significantly to code size.
