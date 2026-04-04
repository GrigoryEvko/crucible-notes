# File Type Detection

nvlink uses a two-stage detection system to classify every input file: first an extension-based hint derived from the file path, then a magic-number validation against the file's actual content. The extension parse happens via `sub_462620` (path\_split), which decomposes a path into directory, basename, and extension components. The magic-number checks are performed against a 56-byte header probe read from the start of each file. Several small predicate functions (`sub_43D970`, `sub_43D9A0`, `sub_43D9B0`, `sub_43DA40`, `sub_487A90`, `sub_4CDF80`) implement individual format tests, while `sub_4CE070` provides a multi-format classifier used specifically for fatbin container contents.

| | |
|---|---|
| **Stage 1** | Extension extraction via `sub_462620` (path\_split) at `0x462620` |
| **Stage 2** | Magic-number validation against 56-byte header probe |
| **Caller** | `main()` at `0x409800` -- the input file loop reads the header and invokes both stages inline |
| **Multi-format classifier** | `sub_4CE070` at `0x4CE070` -- classifies content within fatbin containers |

## Stage 1: Extension Parsing (sub\_462620)

| | |
|---|---|
| **Address** | `0x462620` |
| **Size** | 3,579 bytes |
| **Signature** | `int path_split(const char *path, char **dir_out, char **base_out, char **ext_out)` |
| **Returns** | Always returns via `sub_431000` (arena\_free of the working copy) |

### Algorithm

The function copies the input path into an arena-allocated buffer, then uses two `strrchr` calls to locate the last `/` (directory separator, character 0x2F) and the last `.` (extension separator, character 0x2E). A critical detail: if the last `.` appears before the last `/`, the dot is treated as part of a directory name and the extension pointer is set to NULL. This prevents misinterpreting paths like `/home/user/my.project/binary` as having extension `.project/binary`.

```c
// Reconstructed from sub_462620 at 0x462620
int path_split(const char *path, char **dir_out, char **base_out, char **ext_out) {
    char *copy = arena_strdup(path);       // arena_alloc + strcpy
    char *dot  = strrchr(copy, '.');       // last '.' in path
    char *slash = strrchr(copy, '/');       // last '/' in path

    // If dot appears before slash, it belongs to a directory component
    if (dot < slash)
        dot = NULL;

    // Extract directory: everything before the last '/'
    if (dir_out) {
        if (slash) {
            *slash = '\0';                 // terminate at slash
            *dir_out = arena_strdup(copy); // copy directory portion
            *slash = '/';                  // restore separator
        } else {
            *dir_out = NULL;               // no directory component
        }
    }

    // Extract basename: between last '/' and last '.' (or end)
    if (base_out) {
        if (dot) *dot = '\0';             // temporarily hide extension
        const char *base_start = slash ? slash + 1 : copy;
        *base_out = arena_strdup(base_start);
        if (dot) *dot = '.';             // restore dot
    }

    // Extract extension: everything after the last '.'
    if (ext_out) {
        if (dot)
            *ext_out = arena_strdup(dot + 1);  // skip the dot itself
        else
            *ext_out = NULL;                    // no extension
    }

    arena_free(copy);
    return 0;
}
```

Key observations from the decompiled code:

- Each output component (`dir_out`, `base_out`, `ext_out`) is independently optional. The caller can pass NULL for any component it does not need.
- All output strings are allocated through `sub_4307C0` (arena allocator) via the arena associated with the input string. The caller does not need to free them; arena destruction handles cleanup.
- The function temporarily modifies the working copy in-place by NUL-terminating at separator positions, copies the fragment, then restores the original character. This avoids computing substring lengths manually.
- The extension output does NOT include the leading dot. For input `"kernel.cubin"`, `*ext_out` points to `"cubin"`, not `".cubin"`.

### Extension Strings

The dispatch logic in `main()` compares the extracted extension against these known values:

| Extension | Format | Notes |
|---|---|---|
| `cubin` | CUDA device ELF (cubin) | Always validated by ELF magic + `e_machine == 190` |
| `ptx` | PTX assembly source | May also be detected by `.version` header probe |
| `fatbin` | NVIDIA fatbin container | Always validated by `0xBA55ED50` magic |
| `nvvm` | NVVM IR bitcode | May also be detected by `0x1EE55A01` magic |
| `ltoir` | LTO IR (same format as NVVM IR) | Extension-only detection; content is NVVM bitcode |
| `bc` | LLVM bitcode | Extension-only; triggers assertion `"should never see bc files"` |
| `a` | Static archive | Always validated by `"!<arch>\n"` magic |
| `o` | Object file (host or device) | Validated by ELF magic; sub-classified by `e_machine` |
| `so` | Shared object | Validated by ELF magic; `e_machine != 190` implies host |

## Stage 2: Magic Number Validation

nvlink reads the first 56 bytes of every input file with `fread(header, 1, 56, fp)`. This probe is large enough to cover all required magic numbers and the first portion of an ELF header (including `e_type`, `e_machine`, and `e_flags` in both ELF32 and ELF64 layouts).

### Complete Magic Number Table

| Magic (hex) | Bytes (little-endian) | Size | Format | Check function |
|---|---|---|---|---|
| `0x464C457F` | `7F 45 4C 46` | 4 B | ELF | `sub_43D970` |
| `0xBA55ED50` | `50 ED 55 BA` | 4 B | Fatbin | Inline in `main()`, also in `sub_4CE070` |
| `0x464243BC` | `BC 43 42 46` | 4 B | Fatbin resource container | `sub_4CE070` (as part of 8-byte check) |
| `0x1EE55A01` | `01 5A E5 1E` | 4 B | NVVM IR wrapper | Inline in `main()`, also in `sub_4CE070` |
| `"!<arch>\n"` | `21 3C 61 72 63 68 3E 0A` | 8 B | Archive (regular) | `sub_487A90` |
| `"!<thin>\n"` | `21 3C 74 68 69 6E 3E 0A` | 8 B | Archive (thin) | `sub_487A90` |
| `".version"` | `2E 76 65 72 73 69 6F 6E` | 8 B | PTX assembly | `sub_4CDF80` |

The fatbin resource container magic `0x464243BC` differs from the fatbin archive magic `0xBA55ED50`. The container magic identifies a fatbin ELF resource section (`__nv_relfatbin`), while the archive magic identifies a standalone fatbin file. In `sub_4CE070`, the container check uses a packed 8-byte comparison: `*(uint64_t *)buf == 0x1464243BC`, which simultaneously verifies the 4-byte magic (`0x464243BC`) and a version byte (value `0x01`) at offset 4.

## Individual Detection Functions

### sub\_43D970: ELF Magic Check

| | |
|---|---|
| **Address** | `0x43D970` |
| **Size** | 17 bytes |
| **Signature** | `bool is_elf(const void *buf)` |

Returns true if the first 4 bytes of `buf` equal `0x464C457F` (the ELF magic `\x7fELF`). Returns false if `buf` is NULL.

```c
// Exact decompiled logic
bool is_elf(uint32_t *buf) {
    if (!buf) return false;
    return *buf == 0x464C457F;   // "\x7fELF" in little-endian
}
```

### sub\_43D9A0: ELF Class (64-bit Check)

| | |
|---|---|
| **Address** | `0x43D9A0` |
| **Size** | 17 bytes |
| **Signature** | `bool is_elf64(const void *buf)` |

Returns true if `e_ident[EI_CLASS]` (byte offset 4) equals `ELFCLASS64` (value 2). Returns false if `buf` is NULL.

```c
bool is_elf64(const uint8_t *buf) {
    if (!buf) return false;
    return buf[4] == 2;          // ELFCLASS64
}
```

nvlink requires 64-bit CUDA ELFs for all modern SM architectures. This predicate is used alongside the ELF magic check to validate that the file is not a legacy 32-bit cubin.

### sub\_43D9B0: ET\_REL Check (Relocatable Object)

| | |
|---|---|
| **Address** | `0x43D9B0` |
| **Size** | 55 bytes |
| **Signature** | `bool is_relocatable(const void *buf)` |

Checks the `e_type` field at ELF header offset 16 for the value `ET_REL` (1). Handles both ELF32 and ELF64 formats by dispatching through `sub_448360` (ELF64 header accessor) when `e_ident[EI_CLASS] == 2`, or `sub_46B590` (ELF32 header accessor) otherwise.

```c
bool is_relocatable(const uint8_t *buf) {
    if (!buf) return false;
    Elf_Ehdr *ehdr;
    if (buf[4] == 2)                          // ELFCLASS64
        ehdr = get_elf64_header(buf);         // sub_448360
    else
        ehdr = get_elf32_header(buf);         // sub_46B590
    return ehdr->e_type == ET_REL;            // 1 = relocatable object
}
```

`ET_REL` is the expected ELF type for cubin inputs to a linker. Shared objects (`ET_DYN` = 3) and executables (`ET_EXEC` = 2) are not valid device link inputs.

### sub\_43DA40: SASS Flag Check

| | |
|---|---|
| **Address** | `0x43DA40` |
| **Size** | 48 bytes |
| **Signature** | `bool has_sass(const void *buf)` |

Determines whether a CUDA ELF contains SASS (assembled GPU machine code) as opposed to only intermediate representations. Only applies to 64-bit ELFs (`e_ident[EI_CLASS] == 2`).

The function checks a bit in `e_flags` (ELF header offset 48 in ELF64). The specific bit depends on `e_ident[EI_OSABI]` (byte offset 7):

| `e_ident[EI_OSABI]` | Flag mask | Meaning |
|---|---|---|
| `0x41` (65) | `0x00000002` (bit 1) | Legacy OSABI: SASS-present flag at bit 1 |
| Any other value | `0x00004000` (bit 14) | Current OSABI: SASS-present flag at bit 14 |

```c
bool has_sass(const uint8_t *buf) {
    if (!buf || buf[4] != 2) return false;    // must be ELF64
    Elf64_Ehdr *ehdr = get_elf64_header(buf); // sub_448360
    uint32_t mask;
    if (ehdr->e_ident[EI_OSABI] == 0x41)
        mask = 0x02;                          // legacy OSABI
    else
        mask = 0x4000;                        // current OSABI
    return (ehdr->e_flags & mask) != 0;
}
```

The OSABI value `0x41` (ASCII 'A') represents an older NVIDIA CUDA ELF ABI version. Current versions use a different OSABI value and place the SASS-present indicator at bit 14 of `e_flags`.

### sub\_487A90: Archive Magic Check

| | |
|---|---|
| **Address** | `0x487A90` |
| **Size** | 51 bytes |
| **Signature** | `bool is_archive(const void *buf, size_t size)` |

Tests whether the buffer begins with either the standard archive magic `"!<arch>\n"` or the thin archive magic `"!<thin>\n"`. Both comparisons use `memcmp` with length 8. Returns false if `size < 8` (the buffer is too short to contain the magic).

```c
bool is_archive(const void *buf, size_t size) {
    if (size <= 7) return false;
    if (memcmp(buf, "!<arch>\n", 8) == 0) return true;
    return memcmp(buf, "!<thin>\n", 8) == 0;
}
```

Thin archives contain only references to external member files rather than embedded copies. nvlink handles both formats through the same archive iteration path (`sub_487E10`), with thin archive members resolved via `sub_476BF0`.

### sub\_4CDF80: PTX Detection

| | |
|---|---|
| **Address** | `0x4CDF80` |
| **Size** | 121 bytes |
| **Signature** | `bool is_ptx(const char *buf)` |

Detects PTX assembly by scanning past leading whitespace and comments, then checking whether the first non-trivial content is the `.version` directive. PTX files always begin with a `.version` declaration (e.g., `.version 8.5`).

```c
bool is_ptx(const char *buf) {
    if (!buf) return false;
    const char *p = buf;

    // Skip leading whitespace and comments
    while (*p) {
        // Skip whitespace (space, tab, newline, etc.)
        while (*p && isspace(*p))
            p++;

        // Skip // line comments and /* block comments */
        if (memcmp(p, "//", 2) == 0 || memcmp(p, "/*", 2) == 0) {
            skip_comment(&p);      // sub_45CB90
            continue;
        }
        break;
    }

    // Check for ".version" directive
    return memcmp(p, ".version", 8) == 0;
}
```

The comment-skipping logic (`sub_45CB90` at `0x45CB90`) handles both C-style line comments (`//`) and block comments (`/* ... */`). This means a PTX file with a copyright block comment before the `.version` directive is still correctly identified.

## Multi-Format Classifier: sub\_4CE070

| | |
|---|---|
| **Address** | `0x4CE070` |
| **Size** | 461 bytes |
| **Signature** | `int classify_fatbin_content(fatbin_container *container, const void *content)` |

This function classifies content extracted from a fatbin resource container. It is used during fatbin extraction to determine the format of each embedded member. Unlike the main input loop (which dispatches by extension first), this classifier works purely from content bytes -- fatbin members have no file extensions.

### Precondition

The function first validates that the container's header magic matches `0x1464243BC` as a packed 8-byte value. This encodes the fatbin container magic (`0x464243BC`) in the low 4 bytes and version `0x01` in byte 4. If this check fails, the function returns 2 (invalid container).

### Classification Decision Tree

```
classify_fatbin_content(container, content):
    |
    +-- container == NULL? --> return 1 (null container)
    |
    +-- container->magic != 0x1464243BC? --> return 2 (bad container)
    |
    +-- content == NULL? --> return 1 (null content)
    |
    +-- (content & 0xFFFFFFFFFFFF) == 0x1BA55ED50?
    |       Nested fatbin (magic 0xBA55ED50 + version byte)
    |       --> container->type = 2, return 0
    |
    +-- is_elf(content) AND e_machine == EM_CUDA (190)?
    |       CUDA device ELF (cubin)
    |       --> container->type = 3, return 0
    |
    +-- first_dword == 0x1EE55A01?
    |   OR (first_dword == 0 AND second_dword == 0x1EE55A01)?
    |       NVVM IR (possibly with 4-byte padding)
    |       --> container->type = 1, return 0
    |
    +-- is_ptx(content)?
    |       PTX assembly (.version header)
    |       --> container->type = 4, return 0
    |
    +-- none matched
            --> error diagnostic, return 2
```

### Content Type Codes

The type code is stored at offset 80 in the container structure:

| Code | Format | Detection method |
|---|---|---|
| 1 | NVVM IR | Magic `0x1EE55A01` at offset 0 or 4 |
| 2 | Nested fatbin | Magic `0xBA55ED50` in lower 48 bits |
| 3 | CUDA device ELF (cubin) | ELF magic + `e_machine == 190` |
| 4 | PTX assembly | `.version` directive after whitespace/comments |

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Content successfully classified |
| 1 | NULL container or NULL content pointer |
| 2 | Invalid container magic, or content format not recognized |
| 5 | Error recovery via `setjmp`/`longjmp` (internal error handler tripped) |

### NVVM IR Padding Variant

The NVVM IR check includes a second condition: if the first 4 bytes are zero and the next 4 bytes contain `0x1EE55A01`, the content is still classified as NVVM IR. This handles cases where NVVM bitcode is embedded with 4 bytes of alignment padding at the start -- a layout that occurs in some fatbin container versions.

## Complete Detection Flow

The following pseudocode represents the full two-stage detection as it operates within `main()`:

```c
// Stage 1: Read header probe and extract extension
FILE *fp = fopen(filepath, "rb");
uint8_t header[56];
size_t nread = fread(header, 1, 56, fp);
fclose(fp);

char *dir, *basename, *ext;
path_split(filepath, &dir, &basename, &ext);   // sub_462620

// Stage 2: Magic number checks (priority order)
uint32_t magic32 = *(uint32_t *)header;

if (magic32 == 0x464C457F) {                   // ELF
    // Sub-classify by e_machine
    uint16_t e_machine = get_e_machine(header);
    if (e_machine == 190) {                     // EM_CUDA
        // --> cubin handler (sub_426570)
    } else {
        // --> host ELF / ignore
    }
}
else if (magic32 == 0xBA55ED50) {               // fatbin (signed: -1168773808)
    // --> fatbin extraction (sub_42AF40)
}
else if (magic32 == 0x1EE55A01) {               // NVVM IR
    // --> register IR module (sub_427A10), requires -lto
}
else if (is_archive(header, nread)) {           // "!<arch>\n" or "!<thin>\n"
    // --> archive iteration (sub_4BDAC0 et al.)
}
else if (ext && strcmp(ext, "ptx") == 0) {      // PTX by extension
    // --> ptxas JIT compile (sub_4BD760)
}
else if (is_ptx((char *)header)) {              // PTX by content
    // --> ptxas JIT compile (sub_4BD760)
}
else if (ext && strcmp(ext, "nvvm") == 0) {     // NVVM by extension
    // --> register IR (sub_427A10), requires -lto
}
else if (ext && strcmp(ext, "ltoir") == 0) {    // LTO IR by extension
    // --> register IR (sub_427A10), requires -lto
}
else if (ext && strcmp(ext, "bc") == 0) {       // LLVM bitcode
    // assertion: "should never see bc files"
}
else {
    // --> "ignore input %s", skip
}
```

Magic number checks take strict priority over extension checks. A file named `foo.ptx` that starts with ELF magic will be processed as an ELF, not as PTX. The extension is consulted only after all magic-based checks fail, serving as a fallback for formats that lack a distinctive binary header (such as PTX files that have been truncated or have unusual leading content).

## ELF Sub-Classification Constants

When the ELF magic is detected, several constants are used for deeper classification:

| Constant | Value | Field | Meaning |
|---|---|---|---|
| `ELFCLASS64` | 2 | `e_ident[4]` | 64-bit ELF (required for modern CUDA) |
| `ET_REL` | 1 | `e_type` (offset 16) | Relocatable object (expected for cubin input) |
| `EM_CUDA` | 190 (`0xBE`) | `e_machine` (offset 18) | NVIDIA CUDA device ELF |
| SASS flag (legacy) | `0x02` | `e_flags` (offset 48) | SASS present, when `EI_OSABI == 0x41` |
| SASS flag (current) | `0x4000` | `e_flags` (offset 48) | SASS present, when `EI_OSABI != 0x41` |

The `EM_CUDA` value 190 is NVIDIA's registered ELF machine type. It is the definitive test that separates a cubin from any host object file (x86-64 uses `EM_X86_64 = 62`, AArch64 uses `EM_AARCH64 = 183`).

## Function Map

| Address | Size | Identity | Purpose |
|---|---|---|---|
| `0x462620` | 3,579 B | `path_split` | Decomposes file path into dir / basename / extension |
| `0x43D970` | 17 B | `is_elf` | Checks 4-byte ELF magic `0x464C457F` |
| `0x43D9A0` | 17 B | `is_elf64` | Checks `e_ident[EI_CLASS] == 2` |
| `0x43D9B0` | 55 B | `is_relocatable` | Checks `e_type == ET_REL` (value 1) |
| `0x43DA40` | 48 B | `has_sass` | Tests SASS-present flag in `e_flags` |
| `0x487A90` | 51 B | `is_archive` | Compares against `"!<arch>\n"` and `"!<thin>\n"` |
| `0x4CDF80` | 121 B | `is_ptx` | Skips whitespace/comments, checks for `.version` |
| `0x45CB90` | 100 B | `skip_comment` | Skips `//` and `/* */` comments in PTX detection |
| `0x4CE070` | 461 B | `classify_fatbin_content` | Multi-format classifier for fatbin container members |
| `0x448360` | < 2 KB | `get_elf64_header` | Returns pointer to ELF64 header fields |
| `0x46B590` | < 2 KB | `get_elf32_header` | Returns pointer to ELF32 header fields |

## Error Handling

Detection functions are defensive about NULL pointers. Every predicate (`sub_43D970`, `sub_43D9A0`, `sub_43D9B0`, `sub_43DA40`, `sub_487A90`, `sub_4CDF80`) returns false when passed a NULL buffer. The archive check (`sub_487A90`) additionally validates that the buffer is at least 8 bytes long before attempting `memcmp`. The multi-format classifier (`sub_4CE070`) uses `setjmp`/`longjmp` for error recovery, allowing it to abort classification and return error code 5 if any downstream access faults.

## Cross-References

- [Input File Loop](../pipeline/input-loop.md) -- The dispatch loop that calls these detection functions
- [Cubin Loading](cubin-loading.md) -- Processing after a file is classified as a cubin
- [Fatbin Extraction](fatbin-extraction.md) -- Fatbin container iteration, where `sub_4CE070` classifies members
- [Archive Processing](archives.md) -- Regular and thin archive handling
- [PTX Input & JIT](ptx-input.md) -- PTX compilation path after `.version` detection
- [NVVM IR / LTO IR Input](nvvm-ir-input.md) -- IR module registration path
- [ELF Parsing](elf-parsing.md) -- Deeper ELF header parsing after initial magic validation
- [Entry Point & Main](../pipeline/entry.md) -- The containing `main()` function
