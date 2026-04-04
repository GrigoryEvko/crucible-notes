# Host ELF Embedding

When a CUDA application is compiled with `nvcc`, the host compiler (gcc, clang, MSVC) produces `.o` or `.so` files that contain both host CPU code and embedded device code. The device code is stored inside special ELF sections that nvlink must locate, extract, and feed into the fatbin extraction pipeline. This is the "host ELF embedding" path -- the mechanism by which nvlink recovers device fatbins from host object files that it would otherwise have no reason to process.

The host ELF embedding path is triggered in `main()` when an input file is a valid ELF but fails all device-specific checks: it is not a CUDA device ELF (`e_machine != 190`), not an archive, and not a shared library (extension is not `.so`). In this case, nvlink loads the entire host ELF into memory, scans it for known fatbin-bearing section names, extracts the embedded fatbin data, and re-enters the standard fatbin extraction pipeline.

| | |
|---|---|
| **Host ELF loader** | `sub_476E80` at `0x476E80` (thunk to `sub_43DFC0`) |
| **ELF reader/validator** | `sub_43DFC0` at `0x43DFC0` (344 bytes) |
| **Fatbin section scanner** | `sub_476D90` at `0x476D90` (240 bytes) |
| **Section-exists predicate** | `sub_476EC0` at `0x476EC0` (71 bytes) |
| **Section data accessor** | `sub_476F10` at `0x476F10` (79 bytes) |
| **Host ELF memory release** | `sub_476EA0` at `0x476EA0` (thunk to `arena_free`) |
| **Trigger** | Input file is ELF, not `e_machine == 190`, not `.so`, not `.a` |
| **Output** | Fatbin data buffer passed to `sub_42AF40` (fatbin extraction) |

## Trigger Conditions in main()

The host ELF path is a fallback. The input file dispatch in `main()` evaluates several conditions before reaching it:

```
For each input file:
  1. Read 56-byte header probe
  2. Check extension: if ".a" -> archive handler
  3. Check magic: if 0xBA55ED50 -> fatbin handler
  4. Check magic: if ELF magic (0x7F454C46):
     a. If e_machine == 190 (EM_CUDA) -> cubin handler
     b. If extension == "so" -> skip (shared library, no embedded device code)
     c. If is_host_elf(buf):
        - If ELF magic present AND e_machine == 190 -> cubin handler (redundant)
        - Otherwise -> HOST ELF EMBEDDING PATH  <-- this page
  5. Check for PTX, NVVM, LTO IR, bc formats
```

The specific decompiled logic from `main()` at the host ELF decision point:

```c
// main() at ~line 788: after eliminating archives, fatbins, cubins, .so
// s1 = file extension, ptr = 56-byte header buffer, v74 = filename
v192 = is_archive_magic(ptr, 56);                          // sub_487A90
if (!v192) {
    if (extension != "so") {                                // *s1!=115 || s1[1]!=111 || s1[2]
        v193 = is_host_elf(ptr);                            // sub_43D9B0: e_type == ET_REL
        if (v193) {
            if (extension != "o" || !is_elf(ptr) || get_elf64_header(ptr)->e_machine != 190) {
                // Not a device cubin -- this is a host .o with embedded fatbins
                arena_free(extension);                       // sub_431000
                host_elf_buf = load_host_elf(filename);      // sub_476E80
                fatbin_data = search_fatbin_sections(host_elf_buf, filename);  // sub_476D90 via sub_4BDB70
                report_input_type(fatbin_data, filename);    // sub_4297B0

                if (fatbin_data) {
                    // Extracted fatbin -> feed into standard fatbin pipeline
                    fatbin_extract(fatbin_data, host_elf_buf, filename, ...);  // sub_42AF40
                } else {
                    // No fatbin found; if --register-link-binaries, extract module IDs
                    if (register_link_binaries_path && host_elf_buf) {
                        extract_module_ids(host_elf_buf, filename, &module_list);  // sub_4298C0
                    }
                    byte_2A5F212 = 1;  // mark that host objects were seen
                }
                arena_free(host_elf_buf);                    // sub_476EA0
            }
        }
    }
}
```

The extension comparisons are performed as raw byte checks: `*s1 == 115` is `'s'`, `s1[1] == 111` is `'o'` -- checking for the `.so` extension. Similarly, `*s1 == 111` and `s1[1] == 0` checks for `.o`.

## Host ELF Loading: sub\_43DFC0

`sub_43DFC0` (called through the thunk `sub_476E80`) loads a host ELF file into memory and validates it as a legitimate ELF before returning the buffer. This is distinct from the device ELF loader `sub_476BF0` -- the host loader applies more stringent validation because the file is not expected to be a device ELF, and an invalid file simply returns NULL rather than raising a fatal error.

```c
// sub_43DFC0 -- load_host_elf(filename)
// Returns: arena-allocated buffer containing the ELF, or 0 on failure
uint64_t load_host_elf(const char *filename) {
    FILE *fp = fopen(filename, "rb");
    if (!fp)
        return 0;                              // silent failure -- not fatal

    if (fseek(fp, 0, SEEK_END) == -1) {
        fclose(fp);
        return 0;
    }

    int64_t size = ftell(fp);
    if (size == -1 || fseek(fp, 0, SEEK_SET) == -1 || size <= 52) {
        fclose(fp);
        return 0;                              // too small to be any ELF (Elf32 header = 52 bytes)
    }

    void *arena = get_arena_context(fp);       // sub_44F410
    void *buf = arena_alloc(arena, size);      // sub_4307C0
    if (!buf) {
        alloc_fail_handler(arena, size);       // sub_45CAC0 -- abort
        fclose(fp);
        return 0;
    }

    size_t bytes_read = fread(buf, 1, size, fp);
    fclose(fp);

    if (bytes_read != size)
        goto fail;

    // Validate ELF magic: 0x7F454C46 ("\x7fELF")
    void *ehdr = identity_accessor(buf);       // sub_46B590 (returns buf unchanged)
    if (*(uint8_t *)(ehdr + 5) != 1)           // e_ident[EI_DATA] == ELFDATA2LSB (little-endian)
        goto fail;
    if (*(uint32_t *)ehdr != 0x464C457F)       // ELF magic
        goto fail;

    // Full structural validation (section/program header bounds)
    if (!validate_elf_structure(buf, size))     // sub_43DD30
        goto fail;

    return buf;

fail:
    arena_free(buf);
    return 0;
}
```

### Validation Details

The minimum file size check (`size <= 52`) corresponds to the Elf32 header size. An Elf64 header is 64 bytes, but 52 is the lower bound that prevents obviously-too-small files from reaching the ELF parser.

The ELF data encoding check (`e_ident[EI_DATA] == 1`) enforces little-endian byte order. nvlink only processes little-endian ELFs -- all CUDA device and x86 host ELFs are little-endian.

The structural validator `sub_43DD30` performs exhaustive bounds checking of every section header against the file size. It dispatches on the ELF class byte at offset 4:

- **Elf64** (`e_ident[EI_CLASS] == 2`): Checks `e_shentsize == 64`, validates section header table offset and extent, iterates all sections confirming each section's `sh_offset + sh_size` falls within the buffer.
- **Elf32** (`e_ident[EI_CLASS] != 2`): Checks `e_shentsize == 40`, same offset/size validation with 32-bit fields.

For both classes, sections with type `SHT_NOBITS` (8) and several NVIDIA-specific section types (`0x70000007` through `0x70000015`, checked via bitmask `0x400D`) are exempt from the offset+size bounds check because they have no file backing.

## Fatbin Section Scanning: sub\_476D90

Once the host ELF is loaded and validated, `sub_476D90` searches it for embedded fatbin data. The function probes three section names in priority order:

| Priority | Section name | Naming convention | Notes |
|---|---|---|---|
| 1 | `.nvFatBinSegment` | Dotted, standard ELF | Primary location for embedded fatbins |
| 2 | `__nv_relfatbin` | Non-dotted, linker symbol style | Relocatable fatbin data; this is the one that gets extracted |
| 3 | `.nv_fatbin` | Dotted, standard ELF | Alternate fatbin location |

The search logic is a cascading probe with a twist: the function checks whether `.nvFatBinSegment` exists, then whether `__nv_relfatbin` exists, and only if the first is found but the second is not does it fall through to check `.nv_fatbin`. But crucially, only `__nv_relfatbin` actually triggers fatbin extraction -- the other two sections serve as presence indicators.

```c
// sub_476D90 -- search_fatbin_sections(host_elf_buf, filename)
// Returns: arena-allocated copy of fatbin data, or NULL
void *search_fatbin_sections(void *elf_buf, const char *filename) {
    if (!elf_buf)
        goto error;

    // Probe 1: does .nvFatBinSegment exist?
    if (!section_exists(elf_buf, ".nvFatBinSegment"))
        return NULL;                            // no fatbin segment at all

    // Probe 2: does __nv_relfatbin exist?
    if (!section_exists(elf_buf, "__nv_relfatbin")) {
        // .nvFatBinSegment exists but __nv_relfatbin does not.
        // Check .nv_fatbin as a last resort.
        if (!section_exists(elf_buf, ".nv_fatbin"))
            goto error;                         // no usable fatbin data
        return NULL;
    }

    // __nv_relfatbin found -- extract its data
    void *section_data = get_section_data(elf_buf, "__nv_relfatbin");
    if (!section_data)
        goto error;

    // Validate fatbin magic at the start of the section data
    if (*(uint32_t *)section_data != 0xBA55ED50)   // -1168773808 as signed int32
        goto error;

    // Compute extraction size: wrapper header data_size field + 16 (header itself)
    int64_t extract_size = *(uint64_t *)(section_data + 8) + 16;

    // Allocate and copy the fatbin data
    void *arena = get_arena_context(elf_buf, "__nv_relfatbin");  // sub_44F410
    void *copy = arena_alloc(*(uint64_t *)(arena + 24), extract_size);
    if (!copy)
        alloc_fail_handler(...);               // sub_45CAC0

    memcpy(copy, section_data, extract_size);
    return copy;

error:
    error_emit(dword_2A5BDB0, 30672788, filename);  // sub_467460
    return NULL;
}
```

### Section Name Semantics

The three section names correspond to different stages in the CUDA compilation pipeline:

- **`.nvFatBinSegment`**: Created by the CUDA host compiler integration. When `nvcc` compiles a `.cu` file, it embeds the fatbin in this section of the host object. This section exists in virtually all CUDA host objects that contain device code. The linker script output by `--gen-host-linker-script` collects all `.nvFatBinSegment` sections into a single output segment.

- **`__nv_relfatbin`**: The "relocatable fatbin" section. This is where the actual fatbin wrapper (with `0xBA55ED50` magic) lives. The non-dotted name follows linker symbol naming conventions rather than ELF section naming conventions -- it is designed to be referenced by the CUDA runtime registration code (`DEFINE_REGISTER_FUNC`). This is the section nvlink actually extracts from.

- **`.nv_fatbin`**: An alternate fatbin location used in certain compilation modes. Its presence without `__nv_relfatbin` suggests a host object that was compiled with a different fatbin embedding strategy.

### Magic Validation

After locating the `__nv_relfatbin` section, `sub_476D90` validates that the section data begins with the fatbin wrapper magic `0xBA55ED50`. This is the same magic that `main()` checks when processing standalone `.fatbin` files. The check is performed as a signed 32-bit comparison against `-1168773808` in the decompiled code:

```c
if (*(int32_t *)section_data != -1168773808)    // 0xBA55ED50
    goto error;
```

The 16-byte fatbin wrapper header sits at the start of the section data. The `data_size` field at offset 8 gives the total payload size. The extraction copies `data_size + 16` bytes (the header plus all container data) into a fresh arena buffer that is then passed to the fatbin extraction pipeline.

## Section Lookup Dispatch: sub\_476EC0 and sub\_476F10

Both the section-exists predicate and the section data accessor dispatch on the ELF class to use the appropriate accessor set:

```c
// sub_476EC0 -- section_exists(elf_buf, section_name)
// Returns true if the named section is present in the ELF
bool section_exists(void *elf_buf, const char *name) {
    if (is_elf64(elf_buf))                     // sub_43D9A0: e_ident[4] == 2
        return elf64_find_section(elf_buf, name) != NULL;   // sub_4483B0
    else
        return elf32_find_section(elf_buf, name) != NULL;   // sub_46B5D0
}

// sub_476F10 -- get_section_data(elf_buf, section_name)
// Returns pointer to the section's data within the in-memory ELF
void *get_section_data(void *elf_buf, const char *name) {
    if (is_elf64(elf_buf)) {
        Elf64_Shdr *shdr = elf64_find_section(elf_buf, name);   // sub_4483B0
        return elf64_section_data(elf_buf, shdr);                // sub_448560
    } else {
        Elf32_Shdr *shdr = elf32_find_section(elf_buf, name);   // sub_46B5D0
        return elf32_section_data(elf_buf, shdr);                // sub_46B770
    }
}
```

### Elf64 Section Finder: sub\_4483B0

`sub_4483B0` iterates the Elf64 section header table, resolving each section's name from the section header string table (`SHT_STRTAB` at index `e_shstrndx`) and comparing it against the target name with `strcmp`. The section header table starts at `e_shoff` (offset 40 in the Elf64 header), each entry is 64 bytes, and the string table index is at `e_shstrndx` (offset 62, with the `0xFFFF` extended case handled via `sh_link` of section 0).

The data accessor `sub_448560` returns `elf_buf + shdr->sh_offset` (the `sh_offset` field is at offset 24 in an Elf64 section header).

### Elf32 Section Finder: sub\_46B5D0

`sub_46B5D0` performs the same iteration for Elf32 ELFs. The section header table starts at `e_shoff` (offset 32 in the Elf32 header), each entry is 40 bytes, and the string table index is at `e_shstrndx` (offset 48). The data accessor `sub_46B770` returns `elf_buf + shdr->sh_offset` (at offset 16 in an Elf32 section header).

Both finders handle the `SHN_XINDEX` case where `e_shstrndx == 0xFFFF`, falling back to the `sh_link` field of section header entry 0 to locate the actual string table index.

## Re-Entry into the Fatbin Pipeline

Once `sub_476D90` returns a non-NULL fatbin data buffer, `main()` passes it directly to `sub_42AF40` -- the same fatbin extraction entry point used for standalone `.fatbin` files:

```c
if (fatbin_data) {
    fatbin_extract(fatbin_data, host_elf_buf, filename, current_module, 0, 0, 0,
                   &cubin_list, &cubin_count);    // sub_42AF40
}
```

From this point forward, the extraction follows the standard fatbin pipeline documented in [Fatbin Extraction](fatbin-extraction.md): wrapper header parsing, container iteration, architecture matching, member extraction, and cubin delivery to the merge phase.

## Register-Link-Binaries Fallback

When the host ELF contains no fatbin data (all three section probes fail to produce extractable content), nvlink checks whether the `--register-link-binaries` option was specified. If so, `sub_4298C0` scans a different section of the host ELF for module ID definitions:

```c
if (register_link_binaries_path && host_elf_buf) {
    extract_module_ids(host_elf_buf, filename, &module_list);   // sub_4298C0
}
byte_2A5F212 = 1;   // mark that host objects were processed
```

`sub_4298C0` calls `sub_46F0C0` to locate a data section within the host ELF, then parses it for `"def "` prefixed entries -- module ID definitions that the CUDA runtime uses for lazy module registration. Each `"def "` entry contains a null-terminated module name string that is extracted and appended to the module list. These module IDs are later written out in the `DEFINE_REGISTER_FUNC(%s)` format when nvlink generates the registration source file.

## Host Linker Script Generation

On the output side, nvlink can generate a host linker script that collects all three fatbin sections into the final host executable. This is triggered by the `--gen-host-linker-script` (`-ghls`) option and corresponds to `dword_2A77DC0 == 1` (generate-linker-script-only mode) or `dword_2A77DC0 == 2` (augmented mode, where the script is appended to an existing output):

```c
// Linker script content (literal string from the binary)
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

This 130-byte script (`0x82` bytes) is written via `fwrite` to:
- The output file (opened with `"w"`) in generate-only mode
- The output file (opened with `"a"`) in augmented mode, followed by a test invocation of `ld -T <script> 2>&1 | grep 'no input files' > /dev/null` to verify ld can parse it
- `stdout` as a fallback when no output file is specified

The three section collection rules ensure that all fatbin data from all host objects is gathered into well-known locations in the final host binary, where the CUDA runtime can find them at program startup.

## Function Reference

| Address | Reconstructed name | Size | Signature |
|---|---|---|---|
| `0x476D90` | `search_fatbin_sections` | 240 B | `void *(void *elf_buf, const char *filename)` |
| `0x476E80` | `load_host_elf` (thunk) | 7 B | `uint64_t (const char *filename)` |
| `0x43DFC0` | `load_host_elf` (impl) | 344 B | `uint64_t (const char *filename)` |
| `0x476EC0` | `section_exists` | 71 B | `bool (void *elf_buf, const char *name)` |
| `0x476F10` | `get_section_data` | 79 B | `void *(void *elf_buf, const char *name)` |
| `0x476EA0` | `free_host_elf` (thunk) | 7 B | `void (void *buf)` |
| `0x43D9A0` | `is_elf64` | 18 B | `bool (void *buf)` -- class dispatch |
| `0x4483B0` | `elf64_find_section` | 486 B | `Elf64_Shdr *(void *elf, const char *name)` |
| `0x46B5D0` | `elf32_find_section` | 454 B | `Elf32_Shdr *(void *elf, const char *name)` |
| `0x448560` | `elf64_section_data` | 18 B | `void *(void *elf, Elf64_Shdr *shdr)` |
| `0x46B770` | `elf32_section_data` | 18 B | `void *(void *elf, Elf32_Shdr *shdr)` |
| `0x43DD30` | `validate_elf_structure` | 536 B | `bool (void *buf, uint64_t size)` |
| `0x4298C0` | `extract_module_ids` | 476 B | `void (void *elf, const char *filename, void *list)` |

## Key Constants

| Constant | Value | Meaning |
|---|---|---|
| `0xBA55ED50` | `3126193488` (unsigned) / `-1168773808` (signed) | Fatbin wrapper magic |
| `0x464C457F` | `1179403647` | ELF magic (`"\x7fELF"` as little-endian uint32) |
| `190` / `0xBE` | -- | `EM_CUDA` -- CUDA device ELF machine type |
| `52` | -- | Minimum file size (Elf32 header size) |
| `0x82` | `130` | Linker script string length |
| `30672788` | error code | Error emitted when host ELF contains no extractable fatbin |
