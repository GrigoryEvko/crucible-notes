# Output Writing

The output phase is the final stage of nvlink's linking pipeline. After finalization has produced a self-consistent ELF wrapper with all sections ordered, symbols reindexed, and header fields written, the output phase serializes the in-memory ELF representation into bytes and delivers them to a destination -- a file on disk, a memory buffer, or (for Mercury/capmerc targets) an intermediate buffer that passes through the FNLZR post-link transform before reaching disk. Two secondary output modes also live in this phase: `--register-link-binaries` emits C macro definitions for CUDA runtime registration, and `--dot-file` writes a Graphviz callgraph.

The timing infrastructure brackets this work with `sub_4279C0("write")`. The phase runs unconditionally for every successful link invocation that produces an output ELF.

## Key Facts

| Property | Value |
|---|---|
| ELF-to-file entry | `sub_45C920` at `0x45C920` |
| ELF-to-memory entry | `sub_45C950` at `0x45C950` |
| Size computation | `sub_45C980` at `0x45C980` |
| Serialization engine | `sub_45BF00` at `0x45BF00` (13,258 bytes, 532 lines) |
| Polymorphic writer | `sub_45B6D0` at `0x45B6D0` |
| Program header emitter | `sub_45BAA0` at `0x45BAA0` (5,657 bytes, 228 lines) |
| Writer cleanup | `sub_45B6A0` at `0x45B6A0` |
| Callgraph DOT output | `sub_44CCF0` at `0x44CCF0` |
| FNLZR post-link | `sub_4275C0` at `0x4275C0` |
| Vector append helper | `sub_44FC10` at `0x44FC10` |
| Timing label | `"write"` |
| Called from | `main()` after finalization |
| CLI options | `-o` (output path), `--register-link-binaries`, `--dot-file` |

## Pipeline Position

```
Finalization Phase (sub_445000)
  |
  v
FNLZR Pre-Link (sub_4275C0, Mercury pre-link on inputs)
  |
  v
*** Output Phase ***    <-- this page
  |
  |-- Direct path (non-Mercury):
  |     sub_45C920 -> sub_45BF00 -> sub_45B6D0 (fwrite)
  |
  |-- Mercury path (sm >= 100):
  |     sub_45C980 (compute size)
  |     sub_45C950 -> sub_45BF00 -> sub_45B6D0 (memcpy to buffer)
  |     sub_4275C0 (FNLZR post-link transform)
  |     fwrite(buffer, size, file)
  |
  |-- Register-link-binaries (--register-link-binaries):
  |     fprintf(file, "DEFINE_REGISTER_FUNC(%s)\n", ...)
  |
  |-- DOT callgraph (--dot-file):
  |     sub_44CCF0 -> fprintf("digraph callgraph { ... }")
  |
  v
Host Linker Script (--gen-host-linker-script, separate path)
```

## Output Dispatch in main()

The output decision tree in `main()` (starting around decompiled line 1447) selects one of two serialization paths depending on whether Mercury mode is active (`byte_2A5F222`, set when arch > sm\_99):

```
if (error_flag)  ->  skip output entirely

fopen(output_filename, "wb")       // ::filename global
if (!file)  ->  fatal "cannot open output file"

if (byte_2A5F222) {                // Mercury mode
    size = sub_45C980(elfw);       // compute ELF byte count
    buffer = arena_alloc(0, size);
    sub_45C950(buffer, elfw);      // serialize to memory
    if (byte_2A5F29B)              // --extract debug mode
        write_extract_debug(buffer, size);
    sub_4275C0(&buffer, filename, arch, &out_size, 1);  // FNLZR post-link
    fwrite(buffer, 1, out_size, file);
} else {
    sub_45C920(file, elfw);        // direct serialize to FILE*
}
fclose(file);
```

The Mercury path must serialize to memory first because FNLZR (`sub_4275C0`) is an in-place binary rewriter -- it needs the complete ELF image in a contiguous buffer to rewrite instruction encodings, patch control-flow metadata, and produce the final SASS binary. The non-Mercury path writes directly to the file descriptor, avoiding the allocation of an intermediate buffer.

## The Polymorphic Writer: sub\_45B6D0

Every byte of the serialized ELF passes through `sub_45B6D0`, a polymorphic write dispatcher that supports five backend modes selected by an integer tag stored at offset `+0` of a 40-byte writer context object. The writer context is allocated by the path-specific constructors `sub_45B950` (file mode) and `sub_45BA30` (memory mode).

### Writer Context Layout

```
struct elf_writer {           // 40 bytes total
    int32_t  mode;            // +0:  backend selector (0..4)
    int32_t  reserved;        // +4:  always 0
    void*    cleanup_state;   // +8:  state for cleanup callback (unused in practice)
    void*    rewind_fn;       // +16: rewind function pointer (set to &rewind for file mode)
    void*    cleanup_fn;      // +24: destructor callback (called by sub_45B6A0)
    void*    dest;            // +32: destination -- FILE*, buffer pointer, or callback context
};
```

### Mode Dispatch

```c
int64_t elf_write(elf_writer* w, void* data, size_t len) {
    if (w == NULL)                          // NULL writer -> stdout fallback
        return fwrite(data, 1, len, stdout);

    switch (w->mode) {
    case 0:  // Callback mode
        return w->callback(w->dest, data, len);

    case 1:  // No-op / size-counting mode
        return len;                         // consume bytes, write nothing

    case 2:  // Vector-backed growable buffer
        vector_append(w->dest, data, len);  // sub_44FC10
        return len;

    case 3:  // FILE* mode (primary output path)
        if (w->dest)
            return fwrite(data, 1, len, w->dest);
        // fallthrough: dest is NULL -> putc to stdout byte-by-byte
        for (i = 0; i < len; i++)
            _IO_putc(((uint8_t*)data)[i], stdout);
        return len;

    case 4:  // Direct memory copy (Mercury buffer path)
        memcpy(w->dest, data, len);
        w->dest += len;                     // advance write pointer
        return len;

    default:
        return -1;
    }
}
```

Mode 3 is constructed by `sub_45B950` (for `sub_45C920`). It opens the output FILE\* and stores a pointer to `rewind` as the rewind function. Mode 4 is constructed by `sub_45BA30` (for `sub_45C950`). It stores the base address of the pre-allocated buffer and advances `dest` as bytes are written.

Mode 2 uses a growable vector backed by arena-allocated chunks. The `sub_44FC10` (vector\_append) function manages this: each chunk is a 24-byte header (`capacity`, `remaining`, `data_ptr`) linked into a list. When the current chunk cannot hold the incoming write, a new chunk is allocated (sized to at least the vector's default chunk size or the write size, whichever is larger), the data is copied, and the chunk is appended to the list. The linker context's total byte count at offset `+8` of the vector is incremented after each write.

Mode 1 (no-op) is never explicitly constructed in the observed output paths but exists as a valid mode in the switch -- likely used internally for dry-run size estimation.

### Writer Cleanup: sub\_45B6A0

After serialization completes, `sub_45B6A0` is called on the writer context. It checks offset `+24` (the cleanup function pointer); if non-null, it calls that function with `w->dest` as the argument. Then it frees the writer context via `sub_431000` (arena deallocator). For mode 3, no cleanup function is registered (the FILE\* is managed externally by `main()`). For mode 4, no cleanup is needed since the buffer is arena-allocated.

## Serialization Order: sub\_45BF00

The core serialization function `sub_45BF00` performs a single linear pass through the ELF wrapper, emitting bytes in a strict order. Every write calls `sub_45B6D0` and checks the return value against the expected byte count; a mismatch triggers `sub_467460` with the error string `"writing file"`.

The function handles both ELF32 (class 1, `elfw+4 == 1`) and ELF64 (class 2, `elfw+4 == 2`). The ELF class determines structure sizes: ELF32 uses 52-byte headers, 40-byte section headers, and 32-byte program headers; ELF64 uses 64-byte headers, 64-byte section headers, and 56-byte program headers.

### Phase 1: ELF Header

```
Write elfw (52 or 64 bytes)       // the ELF header (e_ident through e_shstrndx)
Write 1 byte of zero padding      // null terminator for alignment
```

The header size is determined by the class: 52 bytes for ELF32 (`v5 = 52, v4 = 53`), 64 bytes for ELF64 (`v5 = 64, v4 = 65`). The extra byte of padding (`v133[0] = 0`) is always written immediately after.

### Phase 2: Section Header String Table (.shstrtab)

```
Write 1 byte null (index 0)       // SHN_UNDEF name
for i = 1..shstrtab_count:
    Write shstrtab[i] string + NUL terminator
    running_offset += strlen + 1
```

The section-header string table entries are stored in the ordered list at `elfw+336` (accessed via `elfw+312` for the count). Each entry is a null-terminated section name string. The loop writes each string including its terminator, tracking the running byte count in `v4`.

### Phase 3: Symbol String Table (.strtab)

```
Write 1 byte null (index 0)       // empty string at strtab[0]
for j = 1..strtab_count:
    Write strtab[j] string + NUL terminator
    running_offset += strlen + 1
```

Identical structure to .shstrtab, sourced from `elfw+328` (count at `elfw+304`).

### Phase 4: Padding to Section Data

After both string tables, the function looks up section index 3 from the section list (`elfw+360`) via `sub_464DB0`. This section's file offset (at `sec+16` for ELF32 or `sec+24` for ELF64) determines how many zero-pad bytes must be emitted to reach the correct alignment:

```
target_offset = section[3].sh_offset;    // from layout
pad_count = target_offset - running_offset;
if (pad_count < 0)
    fatal_error("Negative size encountered");
for (k = 0; k < pad_count; k++)
    write(0x00);                          // zero fill
```

### Phase 5: Program Headers

Program headers are stored in the ordered list at `elfw+344`. Each entry is 16 bytes for ELF32, 24 bytes for ELF64 (the raw `Elf_Phdr` size stored in these nvlink internal structures -- note that standard ELF `Phdr` is 32/56 bytes; these are compacted internal representations). The function writes them sequentially:

```
phdr_size = (elf_class == 2) ? 24 : 16;   // per-entry size in internal format
for (p = 0; p < list_size(elfw->phdr_list); p++)
    write(phdr_list[p], phdr_size);
    running_offset += phdr_size;
```

### Phase 6: Section Data

The main serialization loop iterates sections 4 through `e_shnum - 1` (skipping the first 4 standard sections: null, shstrtab, strtab, symtab, which were already serialized inline). For each section:

1. **Alignment padding**: Compute gap between the current running offset and the section's `sh_offset`. If positive, emit zero bytes. If negative, fatal error `"Negative size encountered"`.

2. **NOBITS / empty check**: Sections of type `SHT_NOBITS` (8) or certain CUDA-specific no-data types (`0x7000000{8,A,B,C}` -- bitmask check `(0x400D >> (type - 0x70000008)) & 1`) are skipped entirely -- no data bytes emitted.

3. **Data fragment traversal**: For sections with data, the content is stored as a linked list of data fragments (rooted at `sec+72`). Each fragment node has:
   - `node+0`: next pointer
   - `node+8`: pointer to a fragment descriptor
   - `descriptor+0`: data pointer
   - `descriptor+8`: file offset within the section
   - `descriptor+24`: fragment size

   The function walks this list, emitting inter-fragment padding when the fragment's offset exceeds the running position, then writing the fragment data:

   ```
   cursor = 0;
   for (frag = section->frag_list; frag; frag = frag->next) {
       desc = frag->descriptor;
       if (desc->offset > cursor && desc->offset != (uint64_t)-1) {
           zero_pad(desc->offset - cursor);
           cursor = desc->offset;
       }
       write(desc->data, desc->size);
       cursor += desc->size;
   }
   ```

4. **Size validation**: After writing all fragments, the function checks that the total bytes written matches `sec->sh_size` (at `sec+20` for ELF32, `sec+32` for ELF64). On mismatch, it constructs a diagnostic string by concatenating the section name (`sec+96`) with `" section size mismatch"` and calls `sub_467460` to report the error. The concatenated string is `malloc`'d and `free`'d around the error call.

### Phase 7: Post-Section Padding

After all section data is emitted, the function checks whether the current offset has reached `e_shoff` (the section header table offset, stored at `elfw+32` for ELF32 or `elfw+40` for ELF64). If the running offset is less than `e_shoff`, zero-pad bytes fill the gap.

### Phase 8: Section Headers

The final loop writes the raw section header entries:

```
shdr_size = (elf_class == 2) ? 64 : 40;
for (s = 0; s < e_shnum; s++) {
    shdr = list_get(elfw->sections, section_order[s]);
    write(shdr, shdr_size);
}
```

The section order array at `elfw+368` maps logical section indices to their position in the ordered section list at `elfw+360`.

### Phase 9: Program Header Table (sub\_45BAA0)

If the ELF type is `ET_EXEC` (value 2 at `elfw+16`) and certain flag conditions are met, `sub_45BAA0` is called to write a proper ELF program header table at the end of the file. This function constructs an array of `Elf_Phdr` entries on the stack (up to 4 segments):

1. **PT\_LOAD segment for section headers**: Always present. Covers the section header table region. File offset = `e_shoff`, file size = memory size = `e_shnum * shdr_size`, alignment = 8.

2. **PT\_LOAD segment for .strtab** (if `.strtab` base address is non-zero): Covers the string table region.

3. **PT\_LOAD segment for .shstrtab** (if `.shstrtab` base address is non-zero): Covers the section name string table. The segment's offset is computed relative to the `.shstrtab` base, and its file/memory size includes alignment-adjusted section data sizes accumulated by `sub_438BB0`.

4. **PT\_PHDR segment**: Self-referential segment pointing to the program header table itself. File offset = `e_shoff`, size = `e_phnum * phdr_size`.

The program header count field in the on-stack ELF header fragment (`e_phnum`) is set to 2, 3, or 4 depending on which optional segments are present. The entire phdr array is written via a single `sub_45B6D0` call.

The segment construction differs between ELF32 and ELF64: ELF32 phdrs are 32 bytes with 32-bit fields laid out as `{p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align}`; ELF64 phdrs are 56 bytes with 64-bit offset/address/size fields and `p_flags` placed after `p_type`.

## Size Computation: sub\_45C980

`sub_45C980` computes the total byte count of the serialized ELF without actually writing anything. This is used by the Mercury path to pre-allocate the buffer before calling `sub_45C950`.

For ELF32:
```
e_shnum = elfw->e_shnum;            // +48, uint16
if (e_shnum == 0)
    e_shnum = list_get(elfw->sections, 0)->sh_size;  // overflow encoding
result = elfw->e_shoff + e_shnum * elfw->e_shentsize;  // +32 + count * +46
if (elfw->e_type == ET_EXEC)
    result += 128;                   // space for program header table
```

For ELF64:
```
e_shnum = elfw->e_shnum_64;         // +60, uint16
if (e_shnum == 0)
    e_shnum = list_get(elfw->sections, 0)->sh_size_64;
result = elfw->e_shoff_64 + e_shnum * elfw->e_shentsize_64;  // +40 + count * +58
flags = (elfw->e_ident[7] == 'A') ? (elfw->e_flags & 1) : (elfw->e_flags & 0x80000000);
if (elfw->e_type == ET_EXEC && !flags)
    result += 224;                   // space for 64-bit program header table
```

The 128 / 224 byte constants are the maximum space for 4 program header entries (4 x 32 = 128 for ELF32, 4 x 56 = 224 for ELF64).

## Mercury / Capmerc Path

For Mercury targets (sm >= 100, `byte_2A5F222` set), the output path takes a detour through the FNLZR post-link binary rewriter:

1. **Compute size**: `sub_45C980` calculates the exact byte count.
2. **Allocate buffer**: `sub_4307C0(0, size)` allocates from the global arena (arena index 0).
3. **Serialize to buffer**: `sub_45C950` writes the complete ELF into the buffer using writer mode 4 (direct memcpy with advancing pointer).
4. **Debug extract** (if `byte_2A5F29B`): When the `--extract` debug flag is set, the pre-FNLZR ELF is written to a side file. The filename suffix is chosen based on the target: `"cubin"` for sm < 100, `"sass.cubin"` for hardware-finalized SASS, `"capmerc.cubin"` for Mercury targets, or `"merc.cubin"` for the intermediate Mercury form.
5. **FNLZR transform**: `sub_4275C0(&buffer, filename, arch, &out_size, 1)` invokes the finalizer in post-link mode. The function:
   - Logs `"FNLZR: Post-Link Mode"` and `"FNLZR: Starting <filename>"` to stderr when verbose.
   - Validates that the Mercury executable flag is set in `e_flags`.
   - Constructs a 160-byte configuration structure on the stack with flags derived from `byte_2A5F222` (Mercury), `byte_2A5F225` (capmerc), `byte_2A5F310` (shared flag), `byte_2A5F210`, `byte_2A5F224`, `byte_2A5F223`.
   - Calls `sub_4748F0` -- the actual FNLZR engine entry point -- passing the configuration, the buffer, and the ELF wrapper.
   - On failure, emits a fatal error referencing the filename.
   - Logs `"FNLZR: Ending <filename>"` on success.
   - Returns the finalized buffer via the `out_size` output parameter.
   - Calls `sub_43D990` to release the original (pre-FNLZR) ELF wrapper.
6. **Write finalized**: `fwrite(buffer, 1, out_size, file)` writes the FNLZR-transformed binary to the output file.

The FNLZR transform rewrites SASS instruction encodings, control flow metadata, and scheduling information that can only be determined once the complete binary image is available. This is why Mercury targets require the serialize-then-transform-then-write sequence rather than direct file output.

## Register-Link-Binaries Output

When `--register-link-binaries <path>` is specified (`qword_2A5F2E0` is non-null), `main()` writes a C header file containing registration macros for the CUDA runtime. This runs after the ELF output, at approximately decompiled line 1624:

```c
FILE* f = fopen(register_link_binaries_path, "w");

// Count all linked objects plus additional modules
int total = list_count(object_list) + list_count(additional_modules);
fprintf(f, "#define NUM_PRELINKED_OBJECTS %d\n", total);

// Emit one macro per linked object
for (node = object_list; node; node = node->next)
    fprintf(f, "DEFINE_REGISTER_FUNC(%s)\n", node->data->name);

// Emit one macro per additional module
for (node = additional_modules; node; node = node->next)
    fprintf(f, "DEFINE_REGISTER_FUNC(%s)\n", node->name);

fclose(f);
```

The `DEFINE_REGISTER_FUNC` macro is expected to be defined by the including translation unit. It typically expands to a static constructor that calls `__cudaRegisterFatBinary` and `__cudaRegisterFunction` for each kernel in the named object. The `qword_2A5F1E0` list (additional modules) captures objects registered through the `-l` library mechanism that also need runtime registration.

Before writing, there is a special case: if the path already exists and a `_cuda_device_runtime_` substring is found in its contents, the file is deleted rather than overwritten. This handles the case where libcudadevrt was stripped from the link and its registration entry must be removed.

## DOT Callgraph Output: sub\_44CCF0

When `--dot-file <path>` is specified (`qword_2A5F2D0` is non-null), `main()` writes a Graphviz DOT file representing the device-side callgraph:

```c
FILE* f = fopen(dot_file_path, "w");
sub_44CCF0(f, linker_context);
fclose(f);
```

`sub_44CCF0` iterates the callgraph stored in the ordered list at `elfw+408`. For each entry (starting at index 1, since index 0 is the null sentinel), it resolves the function's symbol record via `sub_440590`, then walks the adjacency list at `entry+16` (a linked list of callee indices). For each edge, it emits a DOT edge line:

```
digraph callgraph {
    kernel_A -> device_func_B;
    kernel_A -> device_func_C;
    device_func_B -> leaf_func_D;
}
```

The function names come from the symbol record's name field at `sym+32`. The output is purely structural -- no attributes, weights, or subgraph clustering. The resulting file can be visualized with `dot -Tpng callgraph.dot -o callgraph.png`.

## Host Linker Script Output

A completely separate output path (controlled by `--gen-host-linker-script` / `-ghls`, stored in `dword_2A77DC0`) generates a linker script for the host linker rather than a device ELF. This path does not call any of the ELF serialization functions. Instead it writes a fixed linker script template:

```
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

Three modes exist, selected by `dword_2A77DC0`:

- **Mode 1**: Write the script directly to the output file, overwriting any existing content.
- **Mode 2**: Extract the host linker's default script via `ld --verbose`, strip the decorative lines with `sed '1,2d;$d'`, append the CUDA sections, then validate the combined script with `ld -T <script>`. This mode uses `sub_42FA70` (popen/system) to invoke the host toolchain.
- **Fallback**: If no output file is specified, write the script to stdout.

This output path is orthogonal to the device ELF output -- it is used when nvlink operates as a wrapper that generates input for the host linker rather than producing a device binary directly.

## Error Handling

All write operations check the return value of `sub_45B6D0` against the expected byte count. On mismatch, the function calls `sub_467460` with error context `&unk_2A5B990` and the message `"writing file"`. This error is fatal and terminates the linker.

Two additional error conditions exist in `sub_45BF00`:

1. **Negative size encountered**: If the gap between the current running offset and a section's target file offset is negative, the layout phase produced inconsistent offsets. This indicates a bug in the finalization/layout phases.

2. **Section size mismatch**: If the total bytes written for a section's data fragments does not match `sh_size`, the section's data was corrupted or incompletely populated during the merge/relocate phases. The error message includes the section name for diagnosis.

## Function Reference

| Address | Name | Role |
|---|---|---|
| `0x45C920` | `write_elf_to_file` | Top-level: create FILE\* writer, serialize, cleanup |
| `0x45C950` | `write_elf_to_memory` | Top-level: create memcpy writer, serialize, cleanup |
| `0x45C980` | `compute_elf_size` | Returns total serialized byte count without writing |
| `0x45BF00` | `serialize_elf` | Core serialization engine (header, strings, sections, phdrs) |
| `0x45B6D0` | `elf_write` | Polymorphic 5-mode write dispatcher |
| `0x45BAA0` | `write_program_headers` | Constructs and writes ELF program header table |
| `0x45B950` | `create_file_writer` | Allocates writer context for FILE\* output (mode 3) |
| `0x45BA30` | `create_memory_writer` | Allocates writer context for buffer output (mode 4) |
| `0x45B6A0` | `destroy_writer` | Calls cleanup function and frees writer context |
| `0x44FC10` | `vector_append` | Growable vector write for mode 2 |
| `0x44CCF0` | `callgraph_dump_dot` | Writes callgraph in Graphviz DOT format |
| `0x4275C0` | `fnlzr_post_link` | FNLZR post-link binary rewriter dispatch |
| `0x4748F0` | `fnlzr_engine` | FNLZR engine entry point (called by `sub_4275C0`) |
