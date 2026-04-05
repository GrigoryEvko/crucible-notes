# Debug Information

ptxas generates DWARF-based debug information for cuda-gdb and other GPU debuggers. The debug subsystem spans three distinct code regions: an early-pipeline DWARF line table generator at `0x45A`--`0x45C` that encodes PTX-level source mappings, a mid-pipeline SASS-level emitter at `0x860`--`0x868` that produces both `.debug_line` and `.nv_debug_line_sass` sections along with register mapping tables, and a late-stage DWARF processor/dumper cluster at `0x1CBF`--`0x1CC9` that handles `.debug_info`, `.debug_abbrev`, `.debug_loc`, and `.debug_frame` parsing and emission. The design follows a two-tier model: PTX-level debug info records source file/line to PTX instruction mappings, while SASS-level debug info records the final PTX-to-SASS address correspondence after all optimizations. NVIDIA extends standard DWARF with proprietary sections (`.nv_debug_line_sass`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`, `.nv_debug_info_ptx`) and Mercury-namespace variants (`.nv.merc.debug_*`) for Capsule Mercury binaries.

| | |
|---|---|
| **DWARF line generator (PTX)** | `sub_45C3A0` (9,041 bytes) -- PTX source line to address mapping |
| **LEB128 encoder** | `sub_45A870` (5,293 bytes) -- variable-length integer encoding for DWARF |
| **Debug line table (SASS)** | `sub_866BB0` (3,273 bytes) -- `.debug_line` / `.nv_debug_line_sass` |
| **Debug top-level entry** | `sub_867880` (100 bytes) -- calls line generator twice (PTX + SASS) |
| **Reg info SASS emitter** | `sub_8679F0` (225 bytes) -- `.nv_debug_info_reg_sass` |
| **Reg type emitter** | `sub_867B00` (230 bytes) -- `.nv_debug_info_reg_type` |
| **Post-RA debug annotator** | `sub_88D870` (2,656 bytes) -- final source line annotation |
| **DWARF form name table** | `sub_1CBF820` (400 bytes) -- `DW_FORM_*` ID-to-string |
| **DWARF attribute name table** | `sub_1CBF9B0` (1,600 bytes) -- `DW_AT_*` ID-to-string |
| **`.debug_abbrev` parser** | `sub_1CC0850` (3,704 bytes) -- abbreviation table handler |
| **`.debug_info` parser** | `sub_1CC4A40` (5,218 bytes) -- DIE tree walker |
| **CU header parser** | `sub_1CC5EB0` (2,023 bytes) -- compilation unit headers |
| **Location expression printer** | `sub_1CC34E0` (3,094 bytes) -- `DW_OP_*` decoder |
| **DWARF info processor** | `sub_1CC24C0` (3,993 bytes) -- non-dump emission mode |
| **Debug section classifier** | `sub_1C9D1F0` (2,667 bytes) -- section name to type ID mapper |
| **Mercury debug classifier** | `sub_1C98C60` (1,755 bytes) -- `.nv.merc.debug_*` classifier |
| **SASS debug classifier** | `sub_1C99340` -- `.debug_*` standard section classifier |
| **Debug section type mapper** | `sub_1C998D0` -- maps section name to internal buffer pointer |
| **DWARF attribute emitter** | `sub_66A0B0` (28 KB) -- emits DWARF attributes during IR lowering |
| **DWARF debug info builder** | `sub_66F4E0` (59 KB) -- main DWARF debug info section builder |
| **DWARF line table builder** | `sub_66E250` (33 KB) -- builds `.debug_line` during IR phase |
| **Debug line number formatter** | `sub_671C00` (11 KB) -- formats line number records |

## CLI Flags

Two flags control debug information generation:

| Flag | Internal field | Effect |
|---|---|---|
| `--device-debug` / `-g` | `deviceDebug` at ELFW context + 432 | Full debug info: all DWARF sections (`.debug_info`, `.debug_abbrev`, `.debug_frame`, `.debug_line`, `.debug_loc`, `.debug_str`, `.debug_aranges`), plus NVIDIA extensions. Disables most optimizations, preserving source-level variable correspondence. |
| `--lineinfo` / `-ln` | `lineInfo` in option context | Line tables only: generates `.debug_line` and `.nv_debug_line_sass` without full DWARF DIE trees. Preserves optimization levels. Sufficient for `cuda-memcheck` and profiler source correlation. |
| `--suppress-debug-info` | suppresses emission | Strips all debug sections from output, even if debug input was provided. |

The cubin entry point `sub_612DE0` reads both `deviceDebug` and `lineInfo` flags and passes them through the ELF output pipeline. The section classifier at `sub_1C9D1F0` checks the byte at context offset +432 (`deviceDebug`) to decide whether to emit `.debug_frame` and `.debug_line` sections -- when this byte is zero (no `-g`), those sections are conditionally suppressed.

The `--lineinfo` flag is described in the CLI as `"Generate debug line table information"` and is orthogonal to `-g`. When only `--lineinfo` is active, ptxas generates the two line table sections but omits the heavyweight `.debug_info`/`.debug_abbrev`/`.debug_loc` sections. The string `"device-debug or lineinfo"` appears in a validation check that prevents `--extensible-whole-program` from being combined with either debug mode.

## Debug Section Catalog

ptxas generates three tiers of debug sections depending on compilation mode. Standard DWARF sections use the conventional `.debug_*` namespace. NVIDIA extensions use `.nv_debug_*` names. Capsule Mercury binaries additionally carry `.nv.merc.debug_*` clones.

### Standard DWARF Sections

| Section | DWARF standard | Content |
|---|---|---|
| `.debug_abbrev` | Yes (DWARF 2+) | Abbreviation table defining DIE tag/attribute schemas |
| `.debug_aranges` | Yes | Address range table mapping compilation units to code ranges |
| `.debug_frame` | Yes | Call frame information (CFA rules for unwinding) |
| `.debug_info` | Yes | DIE tree: compilation units, subprograms, variables, types |
| `.debug_line` | Yes | Line number program (source file/line to PTX address mapping) |
| `.debug_loc` | Yes | Location lists for variables with multiple storage locations |
| `.debug_macinfo` | Yes | Macro information |
| `.debug_pubnames` | Yes | Public name lookup table |
| `.debug_str` | Yes | String table referenced by `DW_FORM_strp` |

### NVIDIA Extension Sections

| Section | Content |
|---|---|
| `.nv_debug_line_sass` | SASS-level line table: maps SASS instruction addresses to PTX source lines. Parallel to `.debug_line` but at the machine code level. |
| `.nv_debug_info_reg_sass` | Register-to-variable mapping for SASS. Records which physical GPU register(s) hold each source variable at each program point. |
| `.nv_debug_info_reg_type` | Type information for register mappings. Associates register locations with DWARF type descriptions. |
| `.nv_debug_info_ptx` | PTX-level debug info section. Created by `sub_1CC5EB0` as a PTX-namespace mirror of `.debug_info`. |
| `.nv_debug.shared` | Debug metadata for shared memory variables. |

### Mercury Namespace Variants

For Capsule Mercury binaries (SM 100+), every debug section is cloned into the `.nv.merc.*` namespace. The Mercury debug classifier `sub_1C98C60` recognizes 15 Mercury-namespaced debug sections:

```
.nv.merc.debug_abbrev      .nv.merc.debug_aranges
.nv.merc.debug_frame        .nv.merc.debug_info
.nv.merc.debug_loc          .nv.merc.debug_macinfo
.nv.merc.debug_pubnames     .nv.merc.debug_str
.nv.merc.debug_line         (and additional variants)
```

These sections carry the PTX-level debug information that travels inside the Mercury capsule, enabling deferred finalization to produce debug-capable SASS without re-invoking the full compiler.

## Debug Information Pipeline

Debug data flows through three pipeline stages. Each stage operates on a different intermediate representation and produces output at a different abstraction level.

```
STAGE 1: PTX PARSING  (0x45A-0x45C)
  PTX source --> .loc directives --> DWARF line number program
  sub_45C3A0: Reads .loc directives from PTX input
              Builds file/directory tables
              Generates DWARF line number program (LEB128-encoded)
              Creates "$LDWend" end-of-program label
              Uses function-to-index map ("function index not found
              in debug function map" on error)
  sub_45A870: LEB128 encoder for all numeric fields:
              file number, prologue size, address advance,
              line advance, context, function offset

STAGE 2: IR LOWERING  (0x66A-0x672)
  Ori IR instructions carry debug info at instruction node offset +20
  sub_66F4E0 (59KB): Main DWARF debug info builder
  sub_66E250 (33KB): DWARF .debug_line builder
  sub_66A0B0 (28KB): DWARF attribute emitter (directory id, time stamp, file size)
  sub_671C00 (11KB): Debug line number formatter (context, functionOffset, line number)

STAGE 3: POST-RA + ELF EMISSION  (0x860-0x868, 0x1CBF-0x1CC9)
  After register allocation, physical register assignments are known.
  sub_88D870:  PostRA debug info annotator -- finalizes source line
               mappings after all code motion/scheduling
  sub_867880:  Top-level debug entry -- calls sub_866BB0 twice:
               once with a3=0 for .debug_line (PTX-level)
               once with a3=1 for .nv_debug_line_sass (SASS-level)
  sub_866BB0:  DWARF .debug_line section generator
  sub_8679F0:  .nv_debug_info_reg_sass emitter
  sub_867B00:  .nv_debug_info_reg_type emitter
```

## Line Number Table Generation

The DWARF `.debug_line` section generator `sub_866BB0` is the central function for line table construction. It produces standard DWARF 2 line number programs that map addresses to source locations.

### Parameters

```c
// sub_866BB0 -- DebugLineTableGenerator
// a1: debug_line_context (pointer to ~460-byte state structure)
// a2: ELF output context (ELFW object)
// a3: section index  (0 = .debug_line,  nonzero = .nv_debug_line_sass)
// a4: unused
// a5: source file path (const char*, used for .nv_debug_line_sass only)
```

### Algorithm

1. **Section selection**: Based on `a3`, selects the target section name. For `a3 == 0`, looks up or creates `.debug_line` via `sub_1CB2C60` / `sub_1CA7AB0`. For `a3 != 0`, uses `.nv_debug_line_sass`.

2. **Source file collection**: If `a5` provides a source file path and the section is the SASS variant, copies the filename into the debug context at offset +272. Iterates source files via `sub_4271E0` (directory iterator), sorts entries using `sub_866B80` (file comparison callback).

3. **Directory table construction**: For each source file, splits the path into directory and filename components. Records unique directories via a hash table (`sub_426150` / `sub_426D60`). Each directory gets a sequential index.

4. **File table construction**: Each source file entry is a 40-byte record:
   - File name string
   - Directory index (LEB128)
   - Modification timestamp from `stat()` (`st_mtim.tv_sec`)
   - File size from `stat()` (`st_size`)

5. **Line number program generation**: Generates the DWARF line number state machine program using standard opcodes (`DW_LNS_copy`, `DW_LNS_advance_pc`, `DW_LNS_advance_line`, `DW_LNS_set_file`, etc.) and special opcodes for compact address/line delta encoding.

6. **Finalization**: Writes the complete section via `sub_1CA7180` (ELF section write).

### Debug Line Context Structure

```
debug_line_context (at a1, ~460 bytes):
  +0:     vtable pointer
  +16:    SASS line info pointer (nonzero triggers second pass)
  +64:    per-section context base (160-byte stride, indexed by a3)
  +96:    filename buffer pointer
  +104:   filename buffer size
  +108:   file_count
  +112:   directory buffer pointer
  +120:   directory_count
  +216:   source file count (.debug_line variant)
  +256:   raw filename pointer
  +268:   raw filename flag
  +272:   filename copy buffer (for .nv_debug_line_sass)
  +280:   filename copy length
  +376:   source file count (.nv_debug_line_sass variant)
  +408:   .nv_debug_info_reg_sass buffer chain (linked list head)
  +416:   .nv_debug_info_reg_sass final buffer pointer
  +424:   .nv_debug_info_reg_sass total size
  +432:   .nv_debug_info_reg_type buffer chain (linked list head)
  +440:   .nv_debug_info_reg_type final buffer pointer
  +448:   .nv_debug_info_reg_type total size
  +456:   memory arena / pool allocator
```

### Top-Level Entry

The top-level debug emitter `sub_867880` is minimal -- it calls the line table generator twice:

```c
// sub_867880 -- DebugInfoTopLevel (simplified)
void emit_debug_info(ctx, elf, aux, source_path) {
    sub_866BB0(ctx, elf, 0, aux, source_path);   // .debug_line
    if (ctx->sass_line_info)                       // offset +16
        sub_866BB0(ctx, elf, 1, aux, source_path); // .nv_debug_line_sass
}
```

## LEB128 Encoding

The DWARF standard uses LEB128 (Little-Endian Base 128) variable-length encoding for integers throughout debug sections. ptxas implements this in `sub_45A870`, which handles encoding for multiple fields. Error strings in this function reveal the field types being encoded:

| Field | Error string on overflow |
|---|---|
| File number | `"when generating LEB128 number for file number"` |
| Prologue marker | `"when generating LEB128 number for setting prologue"` |
| Address advance | `"when generating LEB128 number for address advance"` |
| Line advance | `"when generating LEB128 number for line advance"` |
| Context | `"when generating LEB128 number for setting context"` |
| Function offset | `"when generating LEB128 number for setting function Offset"` |
| File timestamp | `"when generating LEB128 number for timestamp"` (in `sub_866BB0`) |
| File size | `"when generating LEB128 number for file size"` (in `sub_866BB0`) |

## Register-to-Variable Mapping

After register allocation, ptxas knows the physical GPU registers assigned to each source variable. Two NVIDIA extension sections capture this:

### `.nv_debug_info_reg_sass`

Emitted by `sub_8679F0`. Records which physical registers (R0--R255, P0--P7, UR0--UR63) hold which source variables at each SASS instruction address. The data is accumulated during code generation into a linked list of buffer chunks at debug context offsets +408/+416/+424. At emission time, the chunks are concatenated into a contiguous buffer and written as an ELF section:

```c
// sub_8679F0 -- simplified
void emit_reg_sass(debug_ctx, elf) {
    // Collect linked list of buffer chunks from debug_ctx+408
    chunks = linked_list_to_array(debug_ctx->reg_sass_chain);
    buf = allocate(debug_ctx->reg_sass_total_size);
    offset = 0;
    for (chunk in chunks) {
        memcpy(buf + offset, chunk->data, chunk->size);
        offset += chunk->size;
    }
    section = create_section(elf, ".nv_debug_info_reg_sass", 0, 1, 0);
    write_section(elf, section, buf, 1, total_size);
}
```

### `.nv_debug_info_reg_type`

Emitted by `sub_867B00`. Structurally identical to the reg_sass emitter but operates on offsets +432/+440/+448. Associates register locations with DWARF type information, enabling the debugger to interpret register contents correctly (e.g., distinguishing a 32-bit float in R5 from a 32-bit integer in R5).

## DWARF Processing Subsystem

The DWARF processing cluster at `0x1CBF`--`0x1CC9` handles both generation and diagnostic dumping of DWARF sections. The code can operate in two modes: a dump mode that prints human-readable representations (for `--dump-debug-info` or internal diagnostics), and an emission mode that processes raw DWARF bytes for the final binary.

### DWARF Form Table -- `sub_1CBF820`

Maps DWARF form IDs to string names. Supports DWARF 2 forms:

| ID | Form | Encoding |
|---|---|---|
| 1 | `DW_FORM_addr` | Target address |
| 3 | `DW_FORM_block2` | 2-byte length block |
| 4 | `DW_FORM_block4` | 4-byte length block |
| 5 | `DW_FORM_data2` | 2-byte unsigned |
| 6 | `DW_FORM_data4` | 4-byte unsigned |
| 7 | `DW_FORM_data8` | 8-byte unsigned |
| 8 | `DW_FORM_string` | Null-terminated inline |
| 9 | `DW_FORM_block` | ULEB128 length block |
| 10 | `DW_FORM_block1` | 1-byte length block |
| 11 | `DW_FORM_data1` | 1-byte unsigned |
| 12 | `DW_FORM_flag` | Boolean byte |
| 13 | `DW_FORM_sdata` | Signed LEB128 |
| 14 | `DW_FORM_strp` | 4-byte offset into `.debug_str` |
| 15 | `DW_FORM_udata` | Unsigned LEB128 |
| 16 | `DW_FORM_ref_addr` | Address-sized reference |
| 17 | `DW_FORM_ref1` | 1-byte CU-relative reference |
| 18 | `DW_FORM_ref2` | 2-byte CU-relative reference |
| 19 | `DW_FORM_ref4` | 4-byte CU-relative reference |
| 20 | `DW_FORM_ref8` | 8-byte CU-relative reference |
| 21 | `DW_FORM_ref_udata` | ULEB128 CU-relative reference |
| 22 | `DW_FORM_indirect` | Form specified inline |

The absence of DWARF 4/5 forms (e.g., `DW_FORM_sec_offset`, `DW_FORM_exprloc`, `DW_FORM_flag_present`) indicates ptxas targets **DWARF version 2**, consistent with the pointer size and CU header format observed in `sub_1CC5EB0`.

### DWARF Attribute Table -- `sub_1CBF9B0`

Maps DWARF attribute IDs to string names. The function recognizes a comprehensive set of standard attributes. Notable entries include:

- **Location attributes**: `DW_AT_location` (2), `DW_AT_frame_base` (64), `DW_AT_data_member_location` (56)
- **Name/type**: `DW_AT_name` (3), `DW_AT_type` (73), `DW_AT_encoding` (63)
- **Scope**: `DW_AT_low_pc` (17), `DW_AT_high_pc` (18), `DW_AT_stmt_list` (16)
- **Producer**: `DW_AT_producer` (37), `DW_AT_comp_dir` (27), `DW_AT_language` (19)
- **Subprogram**: `DW_AT_inline` (32), `DW_AT_prototyped` (39), `DW_AT_artificial` (52)
- **Array**: `DW_AT_lower_bound` (34), `DW_AT_upper_bound` (47), `DW_AT_count` (55)
- **Calling**: `DW_AT_calling_convention` (54), `DW_AT_return_addr` (42)
- **Accessibility**: `DW_AT_accessibility` (50), `DW_AT_external` (63)
- **C++ support**: `DW_AT_vtable_elem_location` (77), `DW_AT_containing_type` (29)

### `.debug_abbrev` Parser -- `sub_1CC0850`

Parses the abbreviation table that defines the schema for each DIE tag. The dump mode output header is:

```
Contents of the .debug_abbrev section:
  Number  TAG
```

Each entry includes:
- Abbreviation number
- TAG name (e.g., `DW_TAG_compile_unit`, `DW_TAG_subprogram`)
- Children indicator: `[has children]` or `[has no children]`
- Attribute-form pairs

The function includes a safety check: `"unexpectedly too many dwarf attributes for any DW_TAG entry!"` -- a guard against malformed or corrupt abbreviation tables.

### `.debug_info` Parser -- `sub_1CC4A40`

Walks the DIE tree, printing entries with nesting depth indentation:

```
 <%d><%x>:  Abbrev Number: %d   (0x%02x %s)
```

Format: `<nesting_depth><byte_offset>: Abbrev Number: <n> (<tag_hex> <tag_name>)`. Null DIEs are printed as `"      (nill)   "`. Attribute values are formatted by `sub_1CC4100` (the attribute value printer) which dispatches on form type.

### Compilation Unit Header -- `sub_1CC5EB0`

Parses and prints CU headers, and creates the NVIDIA extension `.nv_debug_info_ptx` section:

```
 Compilation Unit @ offset 0x%zx:
  Length: %d
  Version: %d
  Abbrev Offset: %d
  Pointer Size: %d
```

The pointer size field is significant -- it determines the size of `DW_FORM_addr` values and `DW_FORM_ref_addr` references throughout the CU.

### Location Expression Decoder -- `sub_1CC34E0`

Decodes DWARF location expressions (`DW_OP_*` operations) used in `DW_AT_location` and related attributes. The supported operations reveal how ptxas encodes GPU variable locations:

| Operation | String | GPU usage |
|---|---|---|
| `DW_OP_addr` | `"DW_OP_addr: 0x%x"` | Absolute memory address (global/shared/local) |
| `DW_OP_const4u` | `"DW_OP_const4u: %d"` | 4-byte unsigned constant |
| `DW_OP_xderef` | `"DW_OP_xderef"` | Cross-address-space dereference (GPU memory spaces) |
| `DW_OP_plus_uconst` | `"DW_OP_plus_uconst: %llu"` | Add unsigned constant to stack top |
| `DW_OP_lit0`--`DW_OP_lit31` | `"DW_OP_lit%u"` | Push literal 0--31 |
| `DW_OP_reg0`--`DW_OP_reg31` | `"DW_OP_reg%d"` | Variable in register N |
| `DW_OP_breg0`--`DW_OP_breg31` | `"DW_OP_breg%d %lld"` | Register N + signed offset |
| `DW_OP_fbreg` | `"DW_OP_fbreg: %lld"` | Frame base + signed offset (stack variables) |
| `DW_OP_nop` | `"DW_OP_nop"` | No operation |
| `DW_OP_stack_value` | `"DW_OP_stack_value"` | Value is on DWARF expression stack, not in memory |

The presence of `DW_OP_xderef` is particularly noteworthy -- this is a DWARF operation rarely used in CPU debuggers but essential for GPU debugging, where variables may reside in different memory spaces (global, shared, local, constant) that require address-space-qualified access.

## Debug Section Classification

Three classifier functions map section names to internal type IDs. The type IDs route sections to the correct processing pipeline during ELF assembly.

### SASS Classifier -- `sub_1C99340`

Recognizes standard DWARF sections by comparing the section name (obtained via `sub_1CB9E50`) against hardcoded strings. Returns 1 (is-debug-section) for:

```
.debug_abbrev    .debug_aranges   .debug_frame
.debug_info      .debug_loc       .debug_macinfo
.debug_pubnames  .debug_str       .debug_line
```

Plus the NVIDIA extension:
```
.nv_debug_info_reg_sass
```

### Mercury Classifier -- `sub_1C98C60`

The Mercury classifier `sub_1C98C60` checks for the `.nv.merc.` prefix on the same set of debug section names. It uses a `strcmp` chain against 15 Mercury-namespaced section names. This classifier is called from 4 sites, primarily during Capsule Mercury construction when debug sections need to be cloned into the merc namespace.

### Unified Classifier -- `sub_1C9D1F0`

The master debug section classifier `sub_1C9D1F0` (2,667 bytes, 13 callees) handles both SASS and Mercury variants. It:

1. Checks whether the section has the Mercury flag (bit 0x10 in section flags byte at offset +11)
2. For Mercury sections, dispatches to the `.nv.merc.debug_*` name check
3. For standard sections, dispatches to the `.debug_*` name check
4. Recognizes additional NVIDIA extensions: `.nv_debug_line_sass`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`
5. Uses `setjmp`/`longjmp` error recovery for malformed section handling

The function also checks the `deviceDebug` flag at context offset +432 to suppress `.debug_frame` and `.debug_line` when debug info is not requested. This is the gate that prevents line tables from appearing in release builds.

### Section Type Mapper -- `sub_1C998D0`

Maps debug section names to internal buffer pointers within the debug context object:

| Section name | Returns pointer at offset |
|---|---|
| `.debug_line` | context + 80 (`a1[10]`) |
| `.debug_frame` | context + 72 (`a1[9]`) |
| `.nv_debug_line_sass` | context + 88 (`a1[11]`) |
| `.debug_info` | (subsequent check) |
| `.debug_loc` | (subsequent check) |

This enables the ELF emitter to route section data to the correct output buffer during final assembly.

## Instruction-Level Debug Metadata

Each internal instruction node in the Ori IR carries debug metadata at offset +20 (a pointer or encoded value for source line information) and offset +0 (a pointer to the PTX source location). This metadata travels through the entire optimization pipeline:

1. **PTX parsing**: The parser records `.loc` directives and attaches source file/line/column to each instruction as it is lowered from PTX to the Ori IR.

2. **Optimization passes**: Most optimization passes preserve or propagate debug metadata. When instructions are cloned (e.g., loop unrolling), the clone inherits the original's debug info. When instructions are deleted, their debug info is lost -- the debugger will map those addresses to the nearest surviving instruction's source line.

3. **Post-RA annotation** (`sub_88D870`): After register allocation and scheduling, this pass finalizes the source-line-to-SASS-address correspondence. It walks all instructions and records the final mapping that will be encoded into the `.nv_debug_line_sass` section.

4. **ELF emission**: The debug line table generator `sub_866BB0` reads the finalized mappings and encodes them as a DWARF line number program.

The PTX-level line generator `sub_45C3A0` uses the label `$LDWend` as an end-of-debug-range marker. The error message `"function index not found in debug function map"` indicates a function-to-index mapping that translates internal function identifiers to DWARF subprogram indices.

## DWARF Version and Extensions

ptxas generates **DWARF version 2** debug information. Evidence:

- The form table (`sub_1CBF820`) covers exactly forms 1--22, which is the DWARF 2 form set. No DWARF 3+ forms (`DW_FORM_sec_offset` = 0x17, `DW_FORM_exprloc` = 0x18) are present.
- The CU header parser (`sub_1CC5EB0`) prints `"Version: %d"` as a field, consistent with the 11-byte DWARF 2 CU header format.
- The attribute table includes DWARF 2 attributes only.

### CUDA-Specific DWARF Extensions

NVIDIA extends standard DWARF for GPU debugging through:

1. **Address space encoding**: `DW_OP_xderef` is used with address space qualifiers to distinguish GPU memory spaces. The `DW_AT_address_class` attribute (recognized in the attribute table at ID 51) encodes CUDA memory space identifiers.

2. **Parallel execution model**: GPU warps execute 32 threads simultaneously. Debug info must account for the fact that each "program counter" corresponds to 32 concurrent threads, and divergent threads may be at different source locations.

3. **Register mapping**: The `.nv_debug_info_reg_sass` section provides a CUDA-specific register-to-variable mapping that goes beyond standard DWARF location lists. GPU register files are much larger (up to 255 general-purpose registers) and have different allocation semantics than CPU registers.

4. **PTX/SASS duality**: The two-tier line table (`.debug_line` for PTX, `.nv_debug_line_sass` for SASS) reflects the unique compilation model where PTX is an intermediate representation with its own debug significance.

## Section Layout Considerations

The ELF section layout calculator `sub_1C9DC60` applies special handling to `.debug_line`:

- `.debug_line` sections receive special padding during layout to ensure proper alignment
- Debug sections are placed after code and data sections in the ELF file
- The `.debug_line` section name is one of only three section names explicitly checked by the layout calculator (alongside `.nv.constant0` and `.nv.reservedSmem`)

## Key Address Summary

| Address range | Subsystem | Function count |
|---|---|---|
| `0x45A000`--`0x45F000` | PTX-level DWARF line generator + LEB128 | ~6 |
| `0x660000`--`0x675000` | IR-level DWARF builder/emitter | ~8 |
| `0x860000`--`0x869000` | SASS-level debug line + register info | ~8 |
| `0x88D000`--`0x88E000` | Post-RA debug annotator | 1 |
| `0x1C98000`--`0x1C9A000` | Section classifiers (merc + SASS) | ~6 |
| `0x1C9D000`--`0x1C9E000` | Unified section classifier | 1 |
| `0x1CBF000`--`0x1CC7000` | DWARF processor/dumper cluster | ~12 |
