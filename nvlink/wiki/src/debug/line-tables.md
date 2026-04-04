# Line Table Merging

Line table merging is the process by which nvlink constructs DWARF `.debug_line` sections in the output cubin. Two distinct subsystems handle this depending on the compilation path: the **linker-side merger** (functions at `0x470000`--`0x480000`) operates during regular linking, collecting per-CU line programs from input ELF objects and concatenating them into merged output sections; the **LTO-side generator** (functions at `0x12D0000`--`0x12D5000` and `0x181A320`) operates during link-time optimization, building DWARF line tables from scratch for freshly compiled SASS code. Both paths ultimately produce two output sections: `.debug_line` (DWARF standard, indexed by `a3 == 0`) for source-level PTX line information, and `.nv_debug_line_sass` (NVIDIA extension, indexed by `a3 > 0`) for SASS-level machine instruction line mappings.

This page documents the LTO-side line table generator in reimplementation-grade detail, as this is the more complex and more heavily used path during `--lto` builds. For the linker-side approach (section-level concatenation), see [Section Merging](../linker/section-merging.md).

## Key Facts

| Property | Value |
|---|---|
| Header builder | `sub_181A320` at `0x181A320` (16,225 bytes) |
| Line program encoder | `sub_12D04E0` at `0x12D04E0` (33,592 bytes) |
| Master debug section builder | `sub_12D2010` at `0x12D2010` (56,450 bytes) |
| State machine initializer | `sub_12D1990` at `0x12D1990` (7,032 bytes) |
| State machine object | 464 bytes, three 8 KB buffers + one 256 KB buffer |
| Directory deduplication | Hash map via `sub_4489C0`/`sub_449A80`/`sub_448E70` |
| DWARF version | 2 or 3, selected per compilation unit |
| Address size | 4-byte (DWARF32) or 8-byte (DWARF64) |
| Section names | `.debug_line` (index 0), `.nv_debug_line_sass` (index > 0) |
| Grid dimension encoder | `sub_12D4440` at `0x12D4440` (7,692 bytes) |

## Architecture Overview

The LTO line table pipeline has three phases:

1. **Collection** (`sub_12D2010`): The master debug section builder iterates all functions in the compiled module. For each function, it walks the instruction stream (32-byte stride records), classifies debug record types (1, 49, 51, 52, 55, 56, 58, 83), and collects source location entries -- file index, line number, column, instruction address, is_stmt flag, and inline context id.

2. **Encoding** (`sub_12D04E0`): The line program encoder receives the collected entries as a flat array of 12-byte records (file:u16, is_stmt:u16, line:u32, address:u32) and encodes them into DWARF line number program opcodes using the standard state machine model: special opcodes for compact line+address deltas, standard opcodes (`DW_LNS_advance_pc`, `DW_LNS_advance_line`, `DW_LNS_set_file`, `DW_LNS_copy`, `DW_LNS_negate_stmt`) for large deltas, and extended opcodes for context/prologue markers.

3. **Serialization** (`sub_181A320`): The header builder assembles the final `.debug_line` or `.nv_debug_line_sass` section by concatenating the DWARF header (version, prologue length, header fields), directory table, file name table, and the encoded line program. It registers the section in the output ELF via `sub_4411B0`/`sub_434BC0` and emits relocations as needed.

```
sub_12D2010 (master)
  |
  +---> iterate functions in module
  |       |
  |       +---> walk instruction records (32-byte stride)
  |       |       classify: type 1  = line entry
  |       |                 type 49 = inline start
  |       |                 type 51 = scope entry
  |       |                 type 52 = convergent boundary
  |       |                 type 55 = function end
  |       |                 type 56 = prologue marker
  |       |                 type 58 = debug frame entry
  |       |                 type 83 = SASS offset marker
  |       |
  |       +---> collect file entries, resolve $LDWend labels
  |       |
  |       +---> call sub_12D04E0 (encode line program)
  |
  +---> call sub_181A320 (build section header + serialize)
```

## State Machine Object Layout

The DWARF line program state machine is allocated by `sub_12D1990` as a 464-byte object with four attached buffers:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `allocator` | Memory allocator context pointer |
| +64 | 8 | `opcode_buffer` | Pointer to 8,000-byte line program opcode buffer |
| +72 | 4 | `opcode_capacity` | Current capacity (initially 8,000) |
| +76 | 4 | `min_instr_length` | Minimum instruction length in bytes |
| +77 | 1 | `default_is_stmt` | Default value of `is_stmt` register |
| +78 | 1 | `line_base` | Line base for special opcode computation |
| +79 | 1 | `line_range` | Line range for special opcode computation |
| +80 | 1 | `opcode_base` | Opcode base (10 for DWARF standard) |
| +82--90 | 9 | `std_opcode_lengths` | Standard opcode operand count table |
| +96 | 8 | `directory_buffer` | Pointer to 8,000-byte directory table buffer |
| +104 | 4 | `directory_capacity` | Directory buffer capacity |
| +108 | 4 | `directory_size` | Bytes written to directory buffer |
| +112 | 8 | `file_buffer` | Pointer to 8,000-byte file name table buffer |
| +120 | 4 | `file_capacity` | File buffer capacity |
| +124 | 4 | `file_size` | Bytes written to file buffer |
| +128 | 8 | `program_buffer` | Pointer to 256,000-byte assembled output |
| +136 | 8 | `program_size` | Bytes written to program buffer |
| +160 | 1 | `is_64bit` | DWARF64 mode flag (affects address encoding) |
| +176--200 | -- | `file_entry_array` | Pointer + count for internal file records |
| +208 | 8 | `total_program_length` | Total length of encoded program data |

The four buffers use a doubling growth strategy: when a write would exceed capacity, a new buffer at 2x the current size is allocated, the old data is copied, and the old buffer is freed via `sub_431000`.

## Header Builder: sub_181A320

### Signature

```c
void dwarf_build_line_section(
    line_ctx*    ctx,          // a1: line table context (160-byte stride per index)
    elfw*        elf_writer,   // a2: output ELF wrapper
    uint32_t     section_index,// a3: 0 = .debug_line, >0 = .nv_debug_line_sass
    hash_map*    symbol_map,   // a4: directory deduplication source
    const char*  comp_dir      // a5: compilation directory (NULL if already set)
);
```

### Section Selection

The function begins by choosing the target section name based on `a3`:

```c
if (a3 == 0) {
    section_id = find_section_by_name(elf_writer, ".debug_line");
    if (!section_id)
        section_id = section_create(elf_writer, ".debug_line", 0, 1, 0);
} else {
    section_id = find_section_by_name(elf_writer, ".nv_debug_line_sass");
    if (!section_id)
        section_id = section_create(elf_writer, ".nv_debug_line_sass", 0, 1, 0);
}
```

This uses `sub_4411B0` (find by name) and `sub_434BC0` (create) -- the same section registry infrastructure documented in [Section Merging](../linker/section-merging.md).

### Source File Collection

When source files have not yet been collected (indicated by the source file count at `ctx + 160*a3 + 216` being zero for `.debug_line`, or `ctx + 160*a3 + 376` for `.nv_debug_line_sass`), the header builder performs a full file scan:

1. **Retrieve file list**: Call `sub_449F00(a4)` to extract all file entries from the input symbol map. Sort them via `sub_4647D0` using comparator `sub_181A2F0`.

2. **Allocate file records**: Allocate `file_count * 40` bytes for the file record array (40 bytes per entry: filename pointer, directory pointer, directory index, file serial number, modification time, file size).

3. **Allocate directory pointer array**: Allocate `(file_count + 1) * 8` bytes, with slot 0 reserved (DWARF convention: directory index 0 means "current compilation directory").

4. **Split paths**: For each file, separate the full path into a directory component and a filename component by scanning backwards for `/` or `\`:

```c
for (each file entry) {
    path = entry->filename;
    len = strlen(path);
    // scan backwards from end for path separator
    for (i = len - 1; i >= 0; i--) {
        if (path[i] == '/' || path[i] == '\\')
            break;
    }
    if (i > 0) {
        dir_string  = strncpy(alloc(i+1), path, i);
        file_string = strcpy(alloc(len-i), &path[i+1]);
    } else {
        dir_string  = NULL;
        file_string = path;
    }
}
```

5. **Directory deduplication**: Each unique directory is inserted into a hash map via `sub_448E70(hash_map, dir_string, directory_index)`. If `sub_449A80(hash_map, dir_string)` returns a non-zero result, the existing directory index is reused. The `directory_pointers` array is populated only for new unique directories, starting at index 1.

6. **File metadata**: For each file, if the input record does not carry modification time and file size, `stat()` is called on the original source path to obtain them. These are stored in the file record at offsets +24 (mtime) and +32 (size).

### Directory Table Encoding

The directory table is a sequence of null-terminated strings written into the directory buffer:

```
directory_1\0 directory_2\0 ... directory_N\0 \0
```

The trailing `\0` marks the end of the directory table (DWARF convention). If only a single directory exists, the table is empty (just the terminal `\0`).

### File Name Table Encoding

The file name table encodes each file as:

```
filename\0  dir_index(ULEB128)  mod_time(ULEB128)  file_size(ULEB128)
```

Each filename is the basename (without directory), and `dir_index` references the directory table (1-based). Modification time and file size are encoded as ULEB128 via `sub_4FA8B0`. On encoding failure, a diagnostic is emitted through `sub_467460`:

- `"when generating LEB128 number for timestamp"` -- mtime encoding overflow
- `"when generating LEB128 number for file size"` -- file size encoding overflow

### DWARF Header Assembly

After the directory and file tables are built, the function assembles the complete DWARF `.debug_line` section header:

```
Offset  Size    Field
------  ------  -----
0       4       total_length (entire section minus 4 bytes)
4       2       version (from ctx+68, typically 2 or 3)
6       4       header_length (bytes from here to first opcode)
10      1       minimum_instruction_length (ctx+76)
11      1       default_is_stmt (ctx+77)
12      1       line_base (ctx+78)
13      1       line_range (ctx+79)
14      1       opcode_base (ctx+80)
15..    N       standard_opcode_lengths[opcode_base-1]
        M       directory_table (includes trailing \0)
        K       file_name_table (includes trailing \0)
```

The standard opcode lengths array has `opcode_base - 1` entries, copied byte-by-byte from `ctx+82..90`. For opcode_base=10, this is 9 bytes covering `DW_LNS_copy` through `DW_LNS_set_isa`.

The total section is assembled into a single contiguous allocation (with 256 extra bytes of slack):

```c
alloc_size = header_size + directory_size + file_size + program_size + 256;
output = arena_alloc(alloc_size);
memcpy(output + 0,  &total_length, 4);
memcpy(output + 4,  &version, 2);
memcpy(output + 6,  &header_length, 4);
memcpy(output + 10, header_fields, opcode_base + 4);
memcpy(output + hdr_end, directory_data, directory_size);
memcpy(output + dir_end, file_data, file_size);
memcpy(output + file_end, program_data, program_size);
```

### DWARF64 Support

When `ctx + 160*a3 + 160` (the `is_64bit` flag at the next stride slot) is set, the header gains an additional 4-byte field: a `.debug_str` section reference is created via `sub_4411B0`/`sub_434BC0`, and a relocation entry is emitted via `sub_469B50` to patch the string offset. The relocation type depends on `sub_440260` (ELF class check): type 1 for 32-bit, type 65539 for 64-bit.

### Relocation Emission

After the section data is written via `sub_4343C0(elf_writer, section_id, 0, output, 0, 1, total_size)`, the function checks for pending relocations at `ctx + 160*a3 + 88..100`. Each relocation's offset is adjusted by adding the header size (since the program data starts after the header in the output section). Relocations are emitted via `sub_4699B0`, referencing target sections looked up by name through `sub_4411B0` or created on demand via `sub_440BE0`.

## Line Program Encoder: sub_12D04E0

### Signature

```c
uint64_t dwarf_emit_line_program(
    line_ctx*        ctx,         // a1: line table context
    line_state*      state,       // a2: 464-byte state machine object
    int              entry_count, // a3: number of line entries
    uint16_t*        entries,     // a4: array of 12-byte line entries
    const char*      comp_dir,    // a5: compilation directory name
    bool             is_64bit,    // a6: DWARF64 address encoding
    int64_t*         context_map, // a7: inline context index map (or NULL)
    bool             has_is_stmt  // a8: entries carry is_stmt flag
);
```

### Entry Format

Each line entry in the `a4` array is 12 bytes (accessed as `uint16_t[6]`):

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | `file_number` (1-based DWARF file index) |
| 2 | 2 | `flags` (bit 0 = is_stmt) |
| 4 | 4 | `line_number` (1-based source line) |
| 8 | 4 | `address` (instruction byte offset from function start) |

### Function Name Registration

Before encoding line entries, the encoder registers the current function's source file in the state machine:

1. Allocate a slot in the file tracking array at `state[22]` (with capacity doubling at `state[26]`).
2. Copy the compilation directory name `a5` into arena memory.
3. Write into the line program buffer: opcode 0 (extended opcode escape), length 2, sub-opcode 9 (for DWARF64: `DW_LNE_set_address`) or sub-opcode 5 (for DWARF32: `DW_LNE_define_file`). This is followed by an 8-byte or 4-byte address field that will be patched by relocation.
4. Increment the file counter at `state[25]`.

### File Number Tracking

The encoder emits `DW_LNS_set_file` (opcode 4) whenever the file number changes between consecutive entries:

```c
if (current_file != state->current_file) {
    uleb128_encode(current_file, &leb_buf, 255);
    emit_byte(state, 4);  // DW_LNS_set_file
    emit_bytes(state, leb_buf, leb_len);
    state->current_file = current_file;
}
```

The file number is ULEB128-encoded via `sub_4FA8B0`. On overflow, the diagnostic `"when generating LEB128 number for file number"` is emitted.

### Prologue Tracking

When `has_is_stmt` (a8) is set and the `is_stmt` flag in the current entry differs from the state machine's current value (`state[39]`), the encoder emits a `DW_LNS_negate_stmt` sequence:

```c
if (has_is_stmt && entry->is_stmt != state->is_stmt_current) {
    emit_byte(state, 0);     // extended opcode escape
    emit_byte(state, 0);     // placeholder for length
    emit_byte(state, 0x92);  // NVIDIA-extended: set prologue
    // ULEB128 encode the is_stmt value
    uleb128_encode(entry->is_stmt, &leb_buf, 255);
    emit_bytes(state, leb_buf, leb_len);
    // patch the length byte
    state->buffer[length_offset] = leb_len + 1;
    state->is_stmt_current = entry->is_stmt;
}
```

The opcode `0x92` (146 decimal) is an NVIDIA-proprietary extended opcode not part of the DWARF standard.

### Inline Context Tracking

When a context map (`a7`) is provided and the inline context changes, the encoder emits an NVIDIA-extended opcode `0x90` (144 decimal):

```c
if (context_map && context_map[entry_index].context != state->current_context) {
    emit_byte(state, 0);     // extended escape
    emit_byte(state, 0);     // placeholder
    emit_byte(state, 0x90);  // NVIDIA-extended: set context
    uleb128_encode(new_context, &leb_buf, 255);
    emit_bytes(state, leb_buf, leb_len);
    uleb128_encode(function_offset, &leb_buf2, 255);
    emit_bytes(state, leb_buf2, leb_len2);
    // patch length
    state->buffer[length_offset] = leb_len + leb_len2 + 1;
    state->current_context = new_context;
}
```

The diagnostics for these ULEB128 encodings are:
- `"when generating LEB128 number for setting context"` -- context index overflow
- `"when generating LEB128 number for setting function Offset"` -- function offset overflow

### Line and Address Advance Encoding

The core of the DWARF line program encoding uses the standard special opcode scheme. For each entry, the encoder computes:

```c
line_delta   = entry->line - state->current_line;
addr_delta   = entry->address - state->current_address;
```

If `state->current_address < 0` (uninitialized), the encoder uses standard opcodes for the first entry:

1. **`DW_LNS_advance_pc` (opcode 2)**: Encode `addr_delta` as ULEB128.
2. **`DW_LNS_advance_line` (opcode 3)**: Encode `line_delta` as SLEB128 (signed).
3. **`DW_LNS_copy` (opcode 1)**: Emit a matrix row.

For subsequent entries, the encoder attempts to use **special opcodes** for compact encoding:

```c
if (line_delta + 5 <= 13) {  // fits in line_range window
    special = 14 * addr_delta + line_delta + line_base + 5;
    if (special > 0 && special <= 255) {
        emit_byte(state, special);
        // single byte encodes both line and address advance
        goto done;
    }
}
```

The special opcode formula is: `opcode = (line_delta - line_base) + (line_range * addr_delta) + opcode_base`. With `line_base = -5`, `line_range = 14`, and `opcode_base = 10`, this becomes `opcode = (line_delta + 5) + 14 * addr_delta + 10`. This matches the DWARF standard formula with the NVIDIA-chosen parameters.

When the special opcode does not fit (line delta too large, address delta too large, or either is negative), the encoder falls back to standard opcodes:

- **Line advance only** (`addr_delta == 0`): Emit `DW_LNS_advance_line` + SLEB128, then `DW_LNS_copy`.
- **Address advance only** (`line_delta == 0`): Emit `DW_LNS_advance_pc` + ULEB128. If the context map indicates an end-of-sequence marker, also emit `DW_LNS_copy`.
- **Both** (fallback): Emit `DW_LNS_advance_line` + SLEB128, then `DW_LNS_advance_pc` + ULEB128, then `DW_LNS_copy`.

The associated diagnostics are:
- `"when generating LEB128 number for line advance"` -- line SLEB128 overflow
- `"when generating LEB128 number for address advance"` -- address ULEB128 overflow

### End of Sequence

After all entries are processed, the encoder emits the DWARF end-of-sequence marker:

```c
emit_byte(state, 0);  // extended escape
emit_byte(state, 1);  // length = 1
emit_byte(state, 1);  // DW_LNE_end_sequence
```

The state machine registers are then reset: `state[29] = 0xFFFFFFFF00000001` (line=1, address=0xFFFFFFFF), `state[32] = 0` (context cleared).

## Master Debug Section Builder: sub_12D2010

### Signature

```c
void dwarf_build_debug_sections(
    int64_t     module_ctx,    // a1: module-level compilation context
    uint64_t    a2,            // a2: pointer to function descriptor array
    int64_t     a3,            // a3: directory hash map (or 0 for serial mode)
    int64_t     a4,            // a4: reserved
    const char* comp_dir       // a5: compilation directory string
);
```

### Overview

This 56 KB function is the largest in the entire DWARF subsystem. It orchestrates the complete pipeline: parsing debug records from the compiled instruction stream, building source file maps, handling inline expansion boundaries, and dispatching to `sub_12D04E0` for line program encoding.

### Instruction Record Scanning

The function dereferences `a2` to obtain a pointer to the function's instruction record array. Each record is 32 bytes (accessed with stride 16 as `uint16_t*`), and the first 16-bit word is a record type discriminator:

| Type | Meaning | Processing |
|---|---|---|
| 1 | Line entry | Extracts line number from offset +2 (32-bit), accumulates |
| 49 | Inline function start | Pushes inline context, tracks nesting |
| 51 | Scope entry | Records debug scope boundary |
| 52 | Convergent boundary | Marks convergent control flow region |
| 55 | Function end | Closes current function's line table |
| 56 | Prologue end | Marks end of function prologue for debugger |
| 58 | Debug frame entry | Generates `.debug_frame` relocations via `sub_12B8650` |
| 83 | SASS offset marker | Records SASS byte offset for `.nv_debug_line_sass` |

### Debug Frame Integration

When type-58 records are present alongside type-55 (function end) and type-52 (convergent boundary), the builder generates `.debug_frame` cross-references. For each frame entry, it calls:

```c
sub_12B8650(module, reloc_type, ".debug_frame", ".debug_frame",
            entry_offset - addr_size, target_offset + base_addr);
```

The `reloc_type` is 2 for 32-bit or 4 for 64-bit (based on byte at `*module + 4`). This creates relocations that allow the debugger to correlate `.debug_frame` unwind entries with `.debug_line` addresses.

### Serial vs. Parallel Mode

The builder operates in two modes based on whether `module_ctx[2]` (the parallel dispatch function pointer) is NULL:

**Serial mode** (`module_ctx[2] == 0`): Calls `sub_12D04E0` directly with a single state machine at `module_ctx + 16` (byte offset +128 from the 8-byte-strided context).

**Parallel mode** (`module_ctx[2] != 0`): Creates additional data structures for thread-safe operation:

1. Allocates a hash map via `sub_4489C0(sub_44E1C0, sub_44E1E0, 0x400)` where `sub_44E1C0`/`sub_44E1E0` are mutex lock/unlock functions and `0x400` (1024) is the initial bucket count.
2. Allocates three dynamically-growing arrays via `sub_464AE0(8, ...)` for tracking file indices, inline contexts, and address mappings.
3. Calls the parallel dispatch function at `module_ctx[2]` with:
   - The function index
   - Output pointers for line data, address data, source file data
   - The is_stmt flag
   - String buffer for filename parsing
   - The five tracking arrays
   - A flag word at `&v484`

The parallel dispatch function populates the tracking arrays, and the builder then processes them in a single-threaded pass to construct the final line program.

### Inline Context Handling

For each compiled function, the builder maintains an inline context stack. When `sub_464BB0` (array size query) reports entries in the context tracking array (`v401`), the builder iterates them in reverse order to build a context map:

```c
for (i = array_size(v401) - 1; i >= 0; i--) {
    context_id  = array_get(v401, i);
    existing    = hash_lookup(context_map, context_id);
    if (existing) {
        // reuse existing context index
    } else {
        // assign new context index
        context_entry[context_index].file      = array_get(v440, i);
        context_entry[context_index].line       = array_get(v417, i);
        context_entry[context_index].func_addr  = function_addresses[i];
        hash_insert(context_map, context_id, context_index);
        // if source file has "+" in name, parse base address via sscanf("%llu")
    }
}
```

The `sscanf` call for `"+"` in filenames handles NVIDIA's convention of encoding inlined function base addresses in the source file name: `"filename.cu+12345"` means the inlined function starts at byte offset 12345.

### $LDWend Label Resolution

A special label `$LDWend` is used to mark the end of the DWARF info contribution for a compilation unit. When the builder encounters this symbol in the input symbol map:

```c
if (symbol_name && strcmp(symbol_name, "$LDWend") == 0) {
    end_address = symbol_value;
}
```

This end address is used to determine where the last line table entry should be placed, ensuring the line table covers the full range of the function's SASS instructions.

### Final Assembly

After all functions have been processed, the builder calls `sub_12D04E0` one final time to encode the accumulated line entries. It passes the complete sorted entry array (with inline context map if parallel mode was used), producing the encoded DWARF line program in the state machine's output buffer. The state machine buffer is then consumed by `sub_181A320` during the header serialization phase.

## NVIDIA-Proprietary Extended Opcodes

nvlink's DWARF line tables include several extended opcodes not defined in the DWARF standard:

| Opcode | Hex | Context | Meaning |
|---|---|---|---|
| 0x90 | 144 | Extended | Set inline context (context_id + function_offset) |
| 0x92 | 146 | Extended | Set prologue/is_stmt flag |

These appear inside DWARF extended opcode sequences (opcode 0, then ULEB128 length, then sub-opcode). Standard DWARF consumers will skip them correctly since the length prefix allows unknown extended opcodes to be ignored.

The standard DWARF opcodes used are:

| Opcode | Value | Meaning |
|---|---|---|
| `DW_LNS_copy` | 1 | Emit a row to the line table matrix |
| `DW_LNS_advance_pc` | 2 | Advance address by ULEB128 operand |
| `DW_LNS_advance_line` | 3 | Advance line by SLEB128 operand |
| `DW_LNS_set_file` | 4 | Set file register to ULEB128 operand |
| `DW_LNS_negate_stmt` | 6 | Toggle is_stmt register |
| `DW_LNE_end_sequence` | 1 (ext) | Mark end of sequence, reset registers |
| Special opcodes | 10--255 | Compact line + address advance |

## DWARF Parameters

nvlink uses the following DWARF line number program parameters:

| Parameter | Value | Description |
|---|---|---|
| `opcode_base` | 10 | First special opcode value |
| `line_base` | -5 | Minimum line delta encodable in special opcode |
| `line_range` | 14 | Number of line values encodable per address step |
| `minimum_instruction_length` | varies | Depends on target (typically 4 or 8 for SASS) |
| `default_is_stmt` | 1 | All lines are statements by default |

The special opcode range with these parameters is:
- Line delta: -5 to +8 (14 values)
- Address delta: 0 to 17 (for opcodes 10 through 255, covering `(255 - 10) / 14 = 17` address steps)

## Buffer Management

All three encoding functions use the same buffer growth pattern:

```c
if (write_position + needed >= capacity - 1) {
    new_capacity = 2 * capacity;
    new_buffer = arena_alloc(new_capacity);
    memset(new_buffer, 0, new_capacity);
    memcpy(new_buffer, old_buffer, old_capacity);
    arena_free(old_buffer);
    buffer_ptr = new_buffer;
    capacity   = new_capacity;
}
```

The initial buffer sizes are:
- Opcode buffer: 8,000 bytes
- Directory table: 8,000 bytes
- File name table: 8,000 bytes
- Assembled output: 256,000 bytes

For large compilation units with thousands of source lines (common in template-heavy CUDA code), the opcode buffer may grow to several megabytes through repeated doubling.

## LEB128 Encoding

The ULEB128 and SLEB128 encoding functions (`sub_4FA8B0` and `sub_4FA910`) are shared infrastructure used across the entire DWARF subsystem:

- **`sub_4FA8B0`** (ULEB128): Encodes an unsigned integer into a caller-provided buffer. Returns 0 on success, 1 if the encoded value exceeds the 255-byte buffer limit.
- **`sub_4FA910`** (SLEB128): Encodes a signed integer using sign-extended 7-bit groups.

Both functions write to a stack-allocated 256-byte buffer and return the encoded length through a by-reference integer parameter (`v316` in the decompiled output). The caller then copies the encoded bytes into the line program buffer.

## Interaction with Section Merging

During regular (non-LTO) linking, `.debug_line` sections from multiple input cubins are merged by the standard section merging infrastructure documented in [Section Merging](../linker/section-merging.md). The linker-side functions at `0x470000`--`0x480000` handle this:

| Function | Address | Role |
|---|---|---|
| `debug_line_table_add_entry` | `sub_4776E0` | Builds line entries (56-byte records) |
| `debug_line_program_header_grow` | `sub_477A60` | Grows header arrays (32-byte records) |
| `debug_line_table_emit_filenames` | `sub_477E10` | Emits file name table from merged inputs |
| `debug_line_program_serialize_ops` | `sub_4783C0` | Concatenates per-CU line programs |
| `debug_line_info_encode` | `sub_478A20` | Encodes DWARF header from merged data |

These functions follow the same DWARF header format as the LTO path but operate on pre-parsed line table entries from input ELF `.debug_line` sections rather than generating them from scratch.

## Cross-References

- [Section Merging](../linker/section-merging.md) -- section creation and data copy primitives
- [DWARF Processing](dwarf-processing.md) -- broader DWARF debug info pipeline
- [NVIDIA Extensions](nvidia-extensions.md) -- `.nv_debug_line_sass` and other NVIDIA-specific debug sections
- [ELF Serialization](../elf/serialization.md) -- final section output to ELF
