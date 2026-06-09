# Cubin Loading

A cubin is a CUDA device ELF — an ELF binary with `e_machine == 190` (`EM_CUDA`) containing compiled SASS instructions, constant data, and NVIDIA-specific metadata sections. When nvlink encounters a cubin input (either directly on the command line or extracted from a fatbin container), it must validate the ELF structure, confirm the SM architecture matches the link target, distinguish SASS from PTX-only cubins, classify every symbol into kernels / device functions / globals, and decide whether the object requires pre-link finalization. This page documents the complete path from raw bytes to a validated cubin whose sections, symbols, and metadata have been integrated into the linker state ready for the merge phase.

**Confidence:** HIGH for the detection, validation, architecture-match, and FNLZR paths (all decompiled functions read end-to-end from `sub_43D970`, `sub_43D9A0`, `sub_43D9B0`, `sub_43DA40`, `sub_43DA80`, `sub_43DD30`, `sub_43E100`, `sub_43E260`, `sub_43E2F0`, `sub_43E420`, `sub_43E610`, `sub_43E6F0`, `sub_426570`, `sub_4275C0`). HIGH for section-name dispatch and `.nv.compat` TLV parsing (extracted from `sub_45E3C0` and `sub_45E7D0` lines 975-1012 and 1804-1851). MEDIUM for the exact STT type branches in `sub_45D180` (read but not line-by-line verified against SysV ELF spec) and HIGH for the absence of LZ4/zlib/SHF_COMPRESSED cubin decompression (the only compression symbols in the binary live inside the bundled libcompiler OCG at addresses `0x1CExxxx`--`0x1D3xxxx`, not on the cubin-loading path).

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_43D970` | `is_elf` | 19 B | Checks 4-byte ELF magic (`0x7F454C46`) |
| `sub_43D9A0` | `is_elf64` | 18 B | Tests ELF class byte (`e_ident[EI_CLASS] == 2`) |
| `sub_43D9B0` | `is_host_elf` | 42 B | Tests `e_type == ET_REL` (1) to distinguish host from device |
| `sub_43DA40` | `is_sass_cubin` | 52 B | Checks SASS flag in `e_flags` (class-dependent bitmask) |
| `sub_43DA80` | `elf_extent` | 420 B | Computes max (sh_offset + sh_size, ph_offset + ph_size) over all sections and program headers |
| `sub_43DD30` | `validate_elf_structure` | 536 B | Full structural validation of section/program headers against buffer size |
| `sub_43E100` | `load_cubin_from_file` | 232 B | Elf32 file loader: open, read, validate, return in-memory buffer |
| `sub_43E260` | `get_nv_cuinfo_section` | 200 B | Resolves `.note.nv.cuinfo` / `.note.nv.cuver` or indexed via `e_ident[19]` |
| `sub_43E2F0` | `get_nv_tkinfo_section` | 180 B | Resolves `.note.nv.tkinfo` note section (legacy compat format) |
| `sub_43E420` | `get_elf_toolkit_version` | 116 B | Extracts toolkit version from `e_flags` or `.nv.compat` section |
| `sub_43E6F0` | `has_abi_suffix` | 172 B | Detects the `a` suffix flag (ABI variant) in `e_flags` |
| `sub_43E610` | `read_nv_compat` | 168 B | Parses the `.nv.compat` section for extended arch metadata |
| `sub_426570` | `validate_arch_and_add` | 7,427 B | Validates architecture match, configures link mode, adds cubin to linker |
| `sub_4275C0` | `post_link_transform` | 3,989 B | FNLZR (Finalizer) — post-link binary rewriting for Mercury/SASS targets |
| `sub_4878A0` | `arch_string_match` | 328 B | Compares input arch string against target `--arch` value |
| `sub_45E7D0` | `merge_cubin_into_elfw` | 52,000+ B | Symbol-table iteration, section-name dispatch, `.nv.compat` TLV parsing, `.nv.info` attribute patching |
| `sub_45E3C0` | `classify_and_register_section` | 2,800 B | Maps section name to NVIDIA section type; the standalone section-classifier entry point |
| `sub_45D180` | `add_function_symbol` | 16,000+ B | STT_FUNC handler: weak-symbol resolution, register-count check, new symbol creation |
| `sub_4504B0` | `get_or_create_nv_info` | 350 B | Lazily creates `.nv.info` or `.nv.info.<function>` target section |

## Detection: Is It a Cubin?

Cubin detection is a two-step test performed inside `main()` after the 56-byte header probe:

1. **ELF magic check** (`sub_43D970`): The first 4 bytes must be `0x7F454C46` (`"\x7fELF"`).
2. **Machine type check**: The `e_machine` field in the ELF header must be `190` (`0xBE`, `EM_CUDA`).

```c
// sub_43D970 -- is_elf
// Returns true if the buffer starts with ELF magic
bool is_elf(uint32_t *buf) {
    if (!buf) return false;
    return *buf == 0x464C457F;  // "\x7fELF" as little-endian uint32
}
```

The `e_machine` check happens inline in `main()` after the ELF magic matches. Any ELF with `e_machine != 190` is classified as a host ELF and handled separately.

`EM_CUDA = 190 = 0xBE` is NVIDIA's assigned `e_machine` value in the ELF machine-ID registry. It is the single most important discriminator in the entire linker: it distinguishes a CUDA device binary from every other ELF on the system. The check happens in three distinct places in the cubin-loading path:

1. `sub_43E100` line 52 (`load_cubin_from_file` final gate): `if (get_elf32_header(buf)->e_machine != 190) return NULL;`
2. The main file-dispatch loop: before calling `sub_426570`, main() reads `e_machine` from the header probe and dispatches cubins vs host ELFs.
3. `sub_43DD30` does **not** check `e_machine` — it only validates the structural integrity of the header and section arrays. A host ELF that accidentally matches `e_machine==190` would still fail later in `sub_426570` due to the architecture mismatch (host ELFs have no valid `e_flags` SM version).

The `EM_CUDA` constant is hardcoded as a literal in three decompiled files:
- `sub_43E100` line 52: `*(_WORD *)(sub_46B590(v10) + 18) == 190`
- `sub_43DFC0` (auxiliary) at offset `+18`
- Indirectly via `sub_448360` (Elf64 header accessor) callers who check `e_machine`

No other value (`EM_NVIDIA_GPU`, `EM_MERCURY`, etc.) is ever accepted as a cubin. Mercury-format objects still carry `e_machine == 190`; the Mercury distinction is encoded in `e_type == 0xFF00` and `e_ident[EI_OSABI] == 0x41`, not in `e_machine`.

### ELF Class Detection

`sub_43D9A0` reads `e_ident[EI_CLASS]` at byte offset 4 of the ELF buffer:

```c
// sub_43D9A0 -- is_elf64
bool is_elf64(void *elf_buf) {
    if (!elf_buf) return false;
    return ((uint8_t *)elf_buf)[4] == 2;  // ELFCLASS64
}
```

This determines whether the cubin uses Elf32 or Elf64 structures. All modern CUDA targets (sm\_20+) use Elf64 when compiled with `-m64`. The Elf32 path exists for legacy 32-bit device code (deprecated since CUDA 12).

### Cubin vs Host ELF Distinction

`sub_43D9B0` distinguishes a cubin (device ELF) from a host `.o` file by checking `e_type`:

```c
// sub_43D9B0 -- is_host_elf
// Returns true if the ELF has e_type == ET_REL (1)
// For cubins emitted by ptxas, e_type is ET_REL (1) or the Mercury type 0xFF00;
// ET_EXEC (2) appears only on cubins produced by a previous nvlink pass.
bool is_host_elf(void *elf_buf) {
    if (!elf_buf) return false;
    if (((uint8_t *)elf_buf)[4] == 2)  // ELFCLASS64
        return get_elf64_header(elf_buf)->e_type == 1;  // ET_REL
    else
        return get_elf32_header(elf_buf)->e_type == 1;
}
```

Device cubins produced by ptxas have `e_type == ET_REL` (1) on legacy targets or the Mercury custom type `0xFF00` on sm >= 100; ptxas never emits `ET_EXEC`. `ET_EXEC` (2) appears only on cubins produced as the final output of a previous nvlink invocation — these are rejected by `sub_426570` because fully-linked executables cannot be re-linked. All variants still carry `e_machine == 190`. The combination of `e_machine == 190` with any `e_type` value routes through the cubin handler; `sub_43D9B0` is used later (e.g., in the LTO path) to distinguish host `.o` files from device cubins by testing `e_type == ET_REL`.

## SASS Flag Detection

`sub_43DA40` determines whether a cubin contains SASS (compiled machine code) or is a PTX-only stub:

```c
// sub_43DA40 -- is_sass_cubin
bool is_sass_cubin(void *elf_buf) {
    if (!elf_buf) return false;
    if (((uint8_t *)elf_buf)[4] != 2)  // Must be ELFCLASS64
        return false;

    Elf64_Ehdr *ehdr = get_elf64_header(elf_buf);

    uint32_t sass_flag;
    if (ehdr->e_ident[EI_OSABI] != 0x41)   // 0x41 = NVIDIA CUDA OSABI (65)
        sass_flag = 0x4000;                  // Elf32-style: bit 14
    else
        sass_flag = 0x2;                     // Elf64/Mercury: bit 1

    return (ehdr->e_flags & sass_flag) != 0;
}
```

The flag semantics:

| ELF OSABI | SASS flag bit | Hex mask | Meaning |
|---|---|---|---|
| `!= 0x41` (legacy) | bit 14 | `0x4000` | Legacy Elf32-style flag layout in `e_flags` |
| `== 0x41` (NVIDIA CUDA) | bit 1 | `0x2` | Modern 64-bit flag layout (Mercury / sm >= 100) |

When the SASS flag is set, the cubin contains actual machine instructions. When clear, it is a PTX-only cubin that serves as a compatibility fallback and cannot execute directly.

## Cubin File Loader: sub\_43E100

`sub_43E100` is a standalone cubin loader that reads a cubin from a file path, validates it, and returns a heap-allocated buffer. The endianness gate reads `e_ident[EI_DATA]` (byte 5) and requires `1` (ELFDATA2LSB, little-endian) — there is **no** ELFCLASS check here, and the loader accepts both ELFCLASS32 and ELFCLASS64 cubins as far as this entry-level validation is concerned. The 4-byte magic compare is a little-endian `int32_t` against `0x464C457F` (`\x7fELF`):

```c
// sub_43E100 -- load_cubin_from_file
void *load_cubin_from_file(const char *path) {
    FILE *fp = fopen(path, "rb");
    if (!fp) return NULL;

    // Get file size
    if (fseek(fp, 0, SEEK_END) == -1) { fclose(fp); return NULL; }
    long size = ftell(fp);
    if (size == -1 || fseek(fp, 0, SEEK_SET) == -1 || size <= 52) {
        fclose(fp); return NULL;
    }

    // Allocate and read into arena memory
    void *buf = arena_alloc(arena, size);
    if (!buf) { arena_oom(arena, size); fclose(fp); return NULL; }

    size_t nread = fread(buf, 1, size, fp);
    fclose(fp);

    // Validate: correct read length, little-endian (EI_DATA == 1), ELF magic
    uint8_t *e_ident = sub_46B590(buf);
    if (nread != size
        || e_ident[5] != 1                              // EI_DATA == ELFDATA2LSB (offset 5, 1 byte)
        || *(int32_t *)e_ident != 0x464C457F) {          // e_ident magic (offset 0, 4 bytes, LE)
        arena_free(buf, 1);
        return NULL;
    }

    // Full structural validation (verifies offsets, sizes, section count)
    if (!sub_43DD30(buf, size)) {
        arena_free(buf, size);
        return NULL;
    }

    // Must be EM_CUDA: e_machine at offset 0x12 = 18, read as uint16_t LE
    if (*(uint16_t *)(sub_46B590(buf) + 18) != 190) {
        arena_free(buf, size);
        return NULL;
    }

    return buf;
}
```

Key details:
- The minimum file size threshold is **52 bytes**, which happens to equal the size of an Elf32 header but is a buffer-size floor, not a class check. Elf64 headers (64 bytes) trivially clear it. The actual class discrimination happens later inside `sub_43DD30`.
- The endianness gate reads `e_ident[EI_DATA]` (offset 5, 1 byte) and requires `1` (`ELFDATA2LSB`). Big-endian cubins are rejected here — there are no big-endian SM targets and the rest of nvlink reads multibyte ELF fields as native little-endian without byteswap.
- The 4-byte magic comparison is a native little-endian `int32_t` against `0x464C457F`; the bytes on disk are `7F 45 4C 46`.
- Memory is allocated from the linker's arena allocator, not `malloc`.
- `sub_43DD30` (`validate_elf_structure`) performs a thorough check of all section headers and program headers, verifying that every offset+size pair falls within the buffer bounds. It branches on the byte at offset 4 (`EI_CLASS`) to pick the Elf32 or Elf64 layout.
- The `e_machine == 190` check is the final gate; the field is at offset `0x12` (decimal 18) and is read as a 2-byte little-endian word.

## Structural Validation: sub\_43DD30

`sub_43DD30` validates the complete ELF structural integrity of an in-memory cubin before any further processing. It checks both Elf32 and Elf64 paths:

**For Elf32:**
- `e_shentsize == 40` (sizeof Elf32\_Shdr); read from offset 46 in the Elf32 ehdr
- `e_phnum == 0` OR `e_phentsize == 32` (sizeof Elf32\_Phdr); e_phnum is at offset 44, e_phentsize at offset 42
- Program header table offset (`e_phoff`) is within the buffer and `e_phoff > 0x33` (beyond the ELF header)
- Total program header table size (`e_phoff + e_phentsize * e_phnum`) fits in buffer
- Section header table offset (`e_shoff`) and total size fits in buffer
- For each section: if the section type is not `SHT_NOBITS` (8) and not in the NVIDIA-specific range (`0x70000007`--`0x70000015`, which includes `SHT_CUDA_INFO`, `SHT_CUDA_CALLGRAPH`, etc.), the section data range `[sh_offset, sh_offset + sh_size)` must fit within the buffer

**For Elf64:**
- `e_shentsize == 64` (sizeof Elf64\_Shdr); read from offset 58 in the Elf64 ehdr
- `e_phnum == 0` OR `e_phentsize == 56` (sizeof Elf64\_Phdr); e_phnum is at offset 56, e_phentsize at offset 54
- Same offset/size boundary checks as Elf32, adjusted for 64-bit field widths
- Overflow protection: checks `sh_offset + sh_size` does not wrap around

The NVIDIA-specific section types that are exempted from the data-range check (they may be virtual/metadata-only):

| Type value | Constant name | Hex | Reason for exemption |
|---|---|---|---|
| `0x70000007` | `SHT_CUDA_GLOBAL` | `0x70000007` | Uninitialized device globals (`__device__` BSS); reclassified from `SHT_NOBITS` |
| `0x70000009` | `SHT_CUDA_LOCAL` | `0x70000009` | Per-kernel thread-private storage; no file content |
| `0x7000000A` | `SHT_CUDA_SHARED` | `0x7000000A` | Per-kernel `__shared__` memory; no file content |
| `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `0x70000015` | Reserved-shmem markers (`.nv.reservedSmem*`); offset-only metadata |

The validation function computes these exemptions with a bitmask check on `(section_type - 0x70000007)`:

```c
uint32_t relative = section_type - 0x70000007;
bool exempt = (section_type == SHT_NOBITS);
if (relative <= 14)
    exempt |= (0x400D >> relative) & 1;  // bits 0, 2, 3, 14 set
```

The bitmask `0x400D` in binary is `0100 0000 0000 1101`, exempting offsets 0, 2, 3, and 14 relative to `0x70000007` — i.e. types `0x70000007` (`SHT_CUDA_GLOBAL`), `0x70000009` (`SHT_CUDA_LOCAL`), `0x7000000A` (`SHT_CUDA_SHARED`), and `0x70000015` (`SHT_CUDA_SHARED_RESERVED`). All four are "virtual" sections that occupy device memory at runtime but have no backing bytes in the cubin file.

> **QUIRK — the exemption base `0x70000007` is not `SHT_LOPROC`.** Standard ELF defines `SHT_LOPROC = 0x70000000`, but the validator uses `0x70000007` (`SHT_CUDA_GLOBAL`) as the bit-shift origin. This is purely a code-size optimization: the only NVIDIA section types that can be data-less are clustered in the `0x70000007`..`0x70000015` range, so re-basing at `0x70000007` keeps the shift amount and the bitmask both in the 0..14 range, which fits in a 16-bit immediate. Section types `0x70000000`..`0x70000006` (info, callgraph, prototype, resolvedrela, metadata, ...) all carry real file content and are not exemption candidates.

## Architecture Extraction from e\_flags

The SM architecture version is encoded in `e_flags` with a layout that depends on the ELF OSABI byte:

### Legacy Layout (OSABI != 0x41)

```text
e_flags (Elf32_Ehdr or Elf64_Ehdr):
  bits [7:0]    = SM version number (e.g., 75 for sm_75, 90 for sm_90)
  bit  [11]     = ABI suffix ('a') flag
  bit  [14]     = SASS flag (contains machine code)
  bit  [31]     = relocatable flag (signed: e_flags < 0)
  bits [19:16]  = toolkit version (Elf32 only, from e_flags of Elf32)
```

### Modern Layout (OSABI == 0x41, Mercury)

```text
e_flags (Elf64_Ehdr, always 64-bit for Mercury):
  bits [15:8]   = SM version number (shifted right by 8)
  bit  [1]      = SASS flag
  bit  [2]      = pre-link indicator (controls FNLZR behavior)
  bit  [3]      = ABI suffix ('a') flag
  bit  [10]     = relocatable flag
  bits [31:20]  = toolkit version (from e_flags >> 20, via .nv.compat)
```

`sub_43E420` extracts the toolkit version:

```c
// sub_43E420 -- get_elf_toolkit_version
uint32_t get_elf_toolkit_version(void *elf_buf) {
    if (!elf_buf) return 0;

    if (is_elf64(elf_buf)) {
        Elf64_Ehdr *ehdr = get_elf64_header(elf_buf);
        if (!ehdr) return 0;
        if (ehdr->e_ident[EI_OSABI] != 0x41)
            return ehdr->e_flags >> 16;  // Legacy: version in upper bits
        // Mercury: look up .nv.compat section
        void *compat = find_section(elf_buf, ".nv.compat");
        if (!compat) return 0;
        if (compat->version_field > 1)
            return compat->toolkit_ver_short;  // 16-bit field at offset 28
        // Fallback to a different compat section structure
        void *compat2 = get_compat_v1(elf_buf);
        if (!compat2) return 0;
        return compat2->toolkit_ver;           // 32-bit field at offset 24
    } else {
        Elf32_Ehdr *ehdr = get_elf32_header(elf_buf);
        if (!ehdr) return 0;
        return ehdr->e_flags >> 16;  // bits [31:16]
    }
}
```

`sub_43E6F0` checks the ABI suffix flag:

```c
// sub_43E6F0 -- has_abi_suffix
// Returns 1 if the cubin was compiled with the 'a' variant (e.g., sm_90a)
bool has_abi_suffix(void *elf_buf) {
    if (!elf_buf || !is_elf64(elf_buf)) return false;

    Elf64_Ehdr *ehdr = get_elf64_header(elf_buf);

    if (ehdr->e_ident[EI_OSABI] == 0x41) {   // Mercury
        if (ehdr->e_ident[EI_ABIVERSION] <= 0x59)  // 89 decimal
            return false;  // sm <= 89 never has suffix in Mercury
        // Check .nv.compat section
        void *compat = find_section(elf_buf, ".nv.compat");
        if (!compat) return true;  // default to yes if no compat info
        if (compat->version > 1) {
            // New compat format: explicit flag at byte offset 4
            nv_compat_info info;
            if (read_nv_compat(elf_buf, &info))
                return info.abi_flag == 1;
        }
        // Old compat format: check e_flags bit
        uint32_t flag = (ehdr->e_ident[EI_OSABI] == 0x41) ? 0x8 : 0x800;
        return (ehdr->e_flags & flag) != 0;
    }

    // Legacy path
    uint8_t sm = (uint8_t)ehdr->e_flags;
    if (sm <= 0x59) return false;  // sm <= 89
    return (ehdr->e_flags >> 11) & 1;  // bit 11
}
```

The `a` suffix (e.g., `sm_90a`) indicates architecture-specific features that break forward compatibility. Only SM versions > 89 support this suffix. The threshold `0x59` (89 decimal) appears in both legacy and Mercury code paths.

## Architecture Validation: sub\_426570

`sub_426570` is the central validation function called from `main()` for every cubin input. It validates that the input cubin's SM architecture matches the `--arch` target, handles the SASS-vs-PTX distinction, and configures the link mode. At 7,427 bytes, it is the most complex function in the cubin loading path.

### Inputs

| Parameter | Type | Role |
|---|---|---|
| `a1` | `elfw *` | The output ELF wrapper being built |
| `a2` | `void *` | The input cubin's in-memory ELF buffer |
| `a3` | `const char *` | The input file path (for error messages) |
| `a4` | `bool *` | Output: set to 1 for legacy/32-bit, 0 for SASS cubins |

### Early Rejection

The function immediately rejects `ET_EXEC` objects (`e_type == 2`): fully-linked executables cannot be re-linked. Cubins arrive as `ET_REL` (1) for relocatable device objects or with the Mercury custom type `0xFF00`. Device executables (`ET_EXEC`) that flow back into nvlink are produced by an earlier link pass, and the merger refuses to re-process them; the test is at `e_type == 2`, gating an immediate return of error code 0.

### Word Size Validation

Compares the cubin's ELF class against the `--machine` setting (`dword_2A5F30C`, either 32 or 64):

```c
bool cubin_is_64 = is_elf64(cubin);
bool target_is_64 = (dword_2A5F30C == 64);
if (cubin_is_64 != target_is_64) {
    // Fatal error: "expected %s" where %s is "-m32" or "-m64"
    error(ERR_ARCH_MISMATCH, filepath, target_is_64 ? "-m64" : "-m32");
}
```

### Architecture String Construction

For 32-bit cubins, the SM version is extracted from `e_flags & 0xFF` and the ABI suffix from `sub_43E6F0`. The function formats an architecture string:

```c
char arch_str[12];
bool is_ptx_only = byte_2A5F2C1;  // PTX/compute mode flag

if (is_ptx_only)
    snprintf(arch_str, 12, "compute_%d%c", sm_version, has_suffix ? 'a' : 0);
else
    snprintf(arch_str, 12, "sm_%d%c", sm_version, has_suffix ? 'a' : 0);
```

The buffer is 12 bytes, and there is an explicit overflow check: if `snprintf` returns > 11, the error `"specified arch exceeds buffer length"` is raised.

### Architecture Match

`sub_4878A0` (`arch_string_match`) compares the constructed `arch_str` against the global target `qword_2A5F318` (the `--arch` value). The comparison is not a simple string equality — it parses both architecture strings into structured records and applies version compatibility rules:

- **Exact match**: sm\_90 == sm\_90 (passes)
- **Family match**: sm\_90a is compatible with sm\_90 as a target
- **Cross-family rejection**: sm\_75 cubins cannot link into an sm\_90 target

If the match fails and `byte_2A5F221` (SASS mode flag) is set, a fallback path tries to match via the `.nv.compat` section (`sub_43E610` + `sub_4709E0`). The `.nv.compat` section contains extended compatibility information that can declare a cubin is forward-compatible with a range of architectures.

On final failure, the error `"SM Arch ('%s') not found in '%s'"` is emitted (via `sub_467460` with format descriptor `unk_2A5B6A0`), where `%s` is the constructed arch string and the target arch.

### 64-bit / Mercury Path

For 64-bit cubins with OSABI `0x41` (Mercury), the SM version comes from `e_flags >> 8` instead of `e_flags & 0xFF`:

```c
if (ehdr->e_ident[EI_OSABI] == 0x41) {
    sm_version = ehdr->e_flags >> 8;     // Mercury: bits [15:8]
} else {
    sm_version = (uint8_t)ehdr->e_flags; // Legacy: bits [7:0]
}
```

An additional check validates the ELF class byte (`e_ident[7]`, used by NVIDIA as a sub-class indicator). For legacy Elf32-format cubins, the expected class is `7`. For modern cubins (sm > 72, `byte_2A5F224` set), the expected class is `8`. If the cubin does not carry the relocatable flag (`e_flags & 0x400` for OSABI 0x41, or `e_flags & 0x4000` for legacy), an error is raised for a class mismatch.

### SASS vs PTX-Only Mode Selection

The validation function sets two global mode flags based on the cubin type:

```c
if (is_sass_cubin) {
    // SASS cubin: set SASS mode
    if (output_flag) *output_flag = 0;
    byte_2A5F221 = 1;  // SASS mode: enables FNLZR, relaxed compat checking
} else {
    // PTX-only or relocatable cubin
    if (output_flag) *output_flag = 1;
    if (!first_cubin_seen) {
        configure_32bit_mode(elfw, ...);
        byte_2A5F222 = 0;  // disable Mercury mode
        byte_2A5F225 = 0;  // disable Mercury-capable flag
        byte_2A5B510 = 1;  // mark first cubin processed
        byte_2A5F220 = 1;
        dword_2A5B528 = 0;
    }
}
```

When the first non-SASS cubin arrives, the linker locks into legacy mode: Mercury flags are cleared and cannot be re-enabled. This is a one-way transition — once a legacy cubin enters the link, the entire output is legacy.

### Toolkit Version Validation

Two additional checks enforce toolkit consistency:

1. **Minimum version**: If `sub_468560()` (get current toolkit version) returns a value less than the cubin's toolkit version (`sub_43E420`), the cubin was built with a newer toolkit than the linker knows about. This produces an error.

2. **SM-specific version locks**:
   - SM 50 with toolkit version <= 64 (`0x40`): error (too old)
   - SM 90 with toolkit version <= 119 (`0x77`): error (too old for Hopper)

3. **EWP objects** (`e_type == 0xFF00`, Mercury executable): If detected, `byte_2A5F229` is set. All subsequent objects must have toolkit version matching the current linker's version exactly. Error: `"linking with -ewp objects requires using current toolkit"`.

## The FNLZR Post-Link Transform: sub\_4275C0

After the merge-relocate-finalize pipeline produces a linked cubin, Mercury targets (sm >= 100) and certain SASS targets require a post-link binary rewriting pass called the **FNLZR** (Finalizer). `sub_4275C0` orchestrates this transformation.

### Pre-Link vs Post-Link Mode

The Finalizer operates in two modes controlled by the `a5` parameter:

**Post-Link Mode** (`a5 == true`): Applied after the linker has produced the final linked ELF. The Finalizer rewrites instruction encodings, resolves final scheduling, and applies architecture-specific binary patches. This is the normal path for sm >= 100 targets.

```c
// Post-link mode: verify the cubin has the SASS flag set
uint32_t sass_check = (ehdr->e_ident[EI_OSABI] == 0x41) ? 0x1 : 0x80000000;
if (!(ehdr->e_flags & sass_check))
    error("Internal error");  // cubin must contain SASS for post-link
```

**Pre-Link Mode** (`a5 == false`): Applied to individual cubins before merging, when the cubin requires pre-link finalization (e.g., instruction encoding normalization). This mode checks that the cubin does NOT already have the SASS flag set (it should be in pre-link format):

```c
// Pre-link mode: verify NOT already finalized
if (ehdr->e_ident[EI_OSABI] == 0x41) {
    bool already_finalized = (ehdr->e_flags >> 2) & 1;  // bit 2
    if (already_finalized) error("Internal error");
} else {
    bool already_finalized = (ehdr->e_flags & (0x80000000 | 0x4000)) == 0;
    // inverted: if neither bit is set, it's unfinalizable
    if (already_finalized) error("Internal error");
}
```

### Configuration Flags

The Finalizer receives a 160-byte configuration structure (`v28[0..19]`) initialized mostly to zero, with specific fields set from global flags:

| Offset | Source | Meaning |
|---|---|---|
| `v28[3] byte 4` | `byte_2A5F310` (debug) | Debug info preservation |
| `v28[3] byte 7` | `byte_2A5F210` | Extended shared memory flag |
| `v28[13] byte 0` | post-link flag | 1 if Mercury mode active |
| `v28[13] byte 1` | `byte_2A5F225` | Mercury-capable flag |
| `v28[13] byte 2` | always 1 | Finalizer enable |
| `v28[13] byte 3` | `byte_2A5F224` | SM > 72 flag |
| `v28[13] byte 4` | `byte_2A5F223` | Additional arch flag |

The operation mode (`v28[3]` low dword) is set to:
- **4**: Default (no debug, not debug+pre-link)
- **5**: Debug mode (`byte_2A5F310` set)
- `v28[8]` = **3**: When neither debug nor the pre-link special flag is set

### Invocation

The actual binary rewriting is performed by `sub_4748F0`, which is the FNLZR engine entry point. All 20 qwords from `v28` are passed as arguments (the x86-64 calling convention spills them onto the stack):

```c
int result = fnlzr_engine(
    target_sm,       // a3: the SM version number
    input_elf,       // the cubin to transform
    elf_ptr,         // pointer to pointer (may be updated)
    config[0..19],   // the 160-byte config structure
    0, 0             // reserved
);
if (result != 0)
    error("FNLZR failure", filename);
```

On success, the ELF at `*elf_ptr` has been rewritten in place. On failure, a fatal error is emitted.

### Diagnostic Output

When verbose mode is active (`dword_2A5F308 & 1`), the Finalizer prints to stderr:

```text
FNLZR: Input ELF: <filename>
FNLZR: Post-Link Mode          (or "Pre-Link Mode")
FNLZR: Flags [ <post_link> | <mercury_capable> ]
FNLZR: Starting <filename>
FNLZR: Ending <filename>
```

If no filename is available (in-memory cubin from fatbin extraction), the placeholder `"in-memory-ELF-image"` is used.

## Cubin-Specific Section Handling

Once the cubin has passed architecture validation and (if needed) finalization, `sub_45E7D0` ingests its contents. The core operation is a one-pass iteration over the cubin's section header table. For every section, `sub_45E7D0` extracts the section name and remaps the section type based on the name prefix. This remap is necessary because ptxas emits some sections with generic ELF types (`SHT_PROGBITS = 1`, `SHT_NOBITS = 8`) but the linker internally tracks them as specific NVIDIA types with the `SHT_LOPROC = 0x70000000` prefix.

### Section Name to Type Dispatch

The dispatch logic lives at `sub_45E7D0:975-1012` and verbatim in the standalone helper `sub_45E3C0:67-102`. It inspects the section name prefix and the incoming section type:

```c
// When section type == SHT_NOBITS (8) -- uninitialized data sections
if      (memcmp(name, ".nv.global",          10) == 0)  type = 0x70000007;  // SHT_CUDA_GLOBAL
else if (memcmp(name, ".nv.shared.",         11) == 0)  type = 0x7000000A;  // SHT_CUDA_SHARED
else if (memcmp(name, ".nv.shared.reserved.",20) == 0)  type = 0x70000015;  // SHT_CUDA_SHARED_RESERVED
else if (memcmp(name, ".nv.local.",          10) == 0)  type = 0x70000009;  // SHT_CUDA_LOCAL
// type stays 8 (SHT_NOBITS) otherwise

// When section type == SHT_PROGBITS (1) -- initialized data
if      (memcmp(name, ".nv.constant",        12) == 0)  type = 0x70000024 + strtol(name+12, 0, 10);
         // .nv.constant0 -> 0x70000064, .nv.constant17 -> 0x70000075
else if (memcmp(name, ".nv.global.init",     15) == 0)  type = 0x70000008;  // SHT_CUDA_GLOBAL_INIT
// type stays 1 otherwise (plain .text, .data, .nv.rel, etc.)

// When incoming type is the generic SHT_CUDA_CONSTANT (0x70000006), same strtol+0x70000024 remap
```

Raw numeric values from the decompiled code:

| Decimal (decompiled) | Hex | NVIDIA section type | Source section name |
|---|---|---|---|
| `1879048199` | `0x70000007` | `SHT_CUDA_GLOBAL` | `.nv.global` |
| `1879048200` | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | `.nv.global.init` |
| `1879048201` | `0x70000009` | `SHT_CUDA_LOCAL` | `.nv.local.*` |
| `1879048202` | `0x7000000A` | `SHT_CUDA_SHARED` | `.nv.shared.*` |
| `1879048213` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.shared.reserved.*` |
| `1879048292` | `0x70000064` | `SHT_CUDA_CONSTANT0` | `.nv.constant0` |
| `1879048198` | `0x70000006` | `SHT_CUDA_CONSTANT` (generic) | incoming base for constant banks |

The expression `strtol(name+12, 0, 10) + 1879048292` at line 1001 (`LABEL_427`) reads the decimal digits that follow `.nv.constant` and adds them to the base. `.nv.constant0` becomes `0x70000064`, `.nv.constant17` becomes `0x70000075`. This is how the 18 numbered banks get their distinct section type IDs and how the merge phase later identifies which bank a section belongs to without re-parsing the name. The same encoding is reused for the 7 specialized/named constant banks, which also use `SHT_CUDA_CONSTANT` as an input type — see [Constant Banks](../elf/constant-banks.md) for the full table.

### Per-Section Dispatch After Classification

Once the section type has been normalized, `sub_45E7D0` dispatches based on the classified type:

| Classified type | Dispatch action |
|---|---|
| `0x70000000` (`SHT_CUDA_INFO`, `.nv.info*`) | Parsed inline as TLV records by `sub_45E7D0` itself (record loop at decompiled line ~1854); target section allocated via `sub_4504B0`. *Earlier wiki revisions incorrectly cited `sub_44E8B0` here — that function is a generic string tokenizer used for option/library parsing, not a TLV parser.* |
| `0x70000001` (`SHT_CUDA_CALLGRAPH`, `.nv.callgraph`) | Indexed as a call-graph input for dead-code elimination |
| `0x70000002` (`SHT_CUDA_CALLGRAPH_INFO`, `.nv.callgraph.info`) | Per-function callgraph metadata |
| `0x70000006`-`0x70000075` (constant banks) | Routed through `sub_438640` for bank-specific merging |
| `0x70000007` (`SHT_CUDA_GLOBAL`) | Global-variable placement, processed by layout phase |
| `0x70000008` (`SHT_CUDA_GLOBAL_INIT`) | Carries initialized global data; copied verbatim |
| `0x70000009`, `0x7000000A`, `0x70000015` (local/shared) | Size accumulated for the per-kernel shared-memory budget |
| `0x70000086` (`SHT_CUDA_COMPAT`, `.nv.compat`) | TLV parsed by `sub_43E610` + inline loop at `sub_45E7D0:1804-1851` |
| `0x7` (`SHT_NOTE`) with flag bit `0x1000000` | Note section processed by `sub_442270` / `sub_469D00`, used for `.note.nv.cuinfo` etc. |
| `SHT_PROGBITS` with `.text.<func>` name | Treated as a per-function text segment, merged by standard symbol resolution |

The special-case for `.text.` is implicit: a `SHT_PROGBITS` section whose name starts with `.text.` keeps its generic type but is linked to the STT_FUNC symbol of the same name during the second pass. Unlike host ELFs, cubins typically emit a single monolithic `.text` section per module rather than per-function `.text.*` sections, so this path is rarely exercised.

### Other Cubin-Specific Sections

Besides the name-dispatched sections above, nvlink recognizes several other sections by explicit lookup in `sub_4483B0` (`find_section_by_name`):

| Section name | ELF type | Purpose |
|---|---|---|
| `.nv.compat` | `0x70000086` (`SHT_CUDA_COMPAT`, or `SHT_NOTE 7` with `SHF_CUDA 0x1000000`) | Forward-compatibility attributes (ISA class, ABI variant) |
| `.note.nv.cuinfo` | `SHT_NOTE 7` | CUDA build info note (alternative to `.nv.compat`) |
| `.note.nv.cuver` | `SHT_NOTE 7` | CUDA toolkit version note |
| `.note.nv.tkinfo` | `SHT_NOTE 7` | Toolkit compatibility note (legacy / pre-Mercury) |
| `.nv.info` | `0x70000000` | Global per-cubin metadata TLV |
| `.nv.info.<function>` | `0x70000000` with `SHF_INFO_LINK 0x40` | Per-function metadata TLV |
| `.nv.prototype` | `0x70000002` (`SHT_CUDA_PROTOTYPE`) | Function prototype records for indirect calls |
| `.nv.callgraph` | `0x70000001` (`SHT_CUDA_CALLGRAPH`) | Call-graph edges used by dead-code elimination |
| `.nv.rel` | `SHT_RELA 4` | NVIDIA-specific relocations (see `R_CUDA*` catalog) |
| `.debug_*` | `SHT_PROGBITS 1` | DWARF debug sections |

The resolution order for cubin info sections is:

1. Check `e_ident[EI_ABIVERSION]` (byte 8): if non-zero and not `0xFF`, it is a direct section index into the CU info note.
2. If `0xFF`, look up `.note.nv.cuinfo` or fall back to `.note.nv.cuver`.
3. Otherwise treat `e_ident[EI_ABIVERSION]` as a section index via `sub_448370`.

This logic is implemented in `sub_43E260` and `sub_43E2F0` and reflects the historical evolution of CUDA metadata: early cubins used distinct note sections, later cubins pack everything into `.nv.compat`.

### The .nv.compat TLV Record Format

The `.nv.compat` section (also accepted as an `SHT_NOTE` variant with the `0x1000000` CUDA flag) carries ISA compatibility attributes as a stream of tagged records. `sub_43E610` retrieves the section buffer and delegates the raw parse to `sub_43E500`. For the structured pass, `sub_45E7D0:1806-1851` walks the buffer record-by-record:

```text
Record layout:
  [0]       tag_kind    (1 byte)    // 0x00..0x08 for known kinds
  [1]       tag_id      (1 byte)    // attribute id (0..8)
  [2..3]    length      (2 bytes)   // byte length of attribute value or 16-bit immediate
  [4..4+N]  value       (N bytes)   // opaque, attribute-specific

Stream termination: end of section buffer
Alignment: 4 bytes
```

The parser classifies `tag_id` by a bitmask on `(1 << tag_id)`:

| `tag_id` | `1 << tag_id` | `& 0x6C`? | `& 0x180`? | Handler | Meaning |
|---|---|---|---|---|---|
| 2 | `0x04` | yes | no | `sub_451920(linker, 2, value_byte)` | `ISA_CLASS` — rejected if ISA class > 0x7F on Mercury |
| 3 | `0x08` | yes | no | `sub_451920(linker, 3, value_byte)` | ABI variant |
| 5 | `0x20` | yes | no | `sub_451920(linker, 5, value_byte)` | ISA feature |
| 6 | `0x40` | yes | no | `sub_451920(linker, 6, value_byte)` | ISA feature |
| 7 | `0x80` | no | yes | `sub_451BA0(linker, 7, value_u16)` | Shader-model minimum (16-bit value) |
| 8 | `0x100` | no | yes | `sub_451BA0(linker, 8, value_u16)` | Shader-model cap (16-bit value) |
| 4 | `0x10` | no | no | (length skip only) | Padding / length-prefixed value |

Unknown attributes emit `"unknown .nv.compat attribute (%x) encoutered.\n"` (note the original typo) to stderr when the `0x10` verbose flag is set, then continue. The record-advance logic is `cursor += (tag_kind == 4) ? 4 + length : 4`, so `tag_kind == 4` is a length-prefixed record while other kinds carry the immediate value in the same 4 bytes.

After the walk finishes, a "missing attributes" sweep runs at `sub_45E7D0:1743-1801`. For each required attribute (2, 3, 5, 6, 7), if the corresponding `BYTE(v600, tag_id)` was never set, a default value is installed — and for `tag_id == 2` on Mercury, ISA class > `0x7F` triggers the error `unk_2A5B900` ("ISA_CLASS out of range").

## Symbol Classification: Kernels, Device Functions, and Globals

Once section classification is done, `sub_45E7D0:762-800` walks the symbol table twice. The first pass extracts only STT_FUNC symbols (`(st_info & 0xF) == 2`) and passes each to `sub_45D180` for full classification. The second pass (`sub_45E7D0:802-1014`) handles every other symbol type.

### STT_FUNC Pass (First Pass)

For every `STT_FUNC` symbol, `sub_45D180` makes a binding decision:

| `st_info` upper nibble | ELF binding | Outcome in nvlink |
|---|---|---|
| `0x0` | `STB_LOCAL` | Section-local; recorded but not globally visible |
| `0x1` | `STB_GLOBAL` | Global function; entered in the global symbol table, may resolve conflicts |
| `0x2` | `STB_WEAK` | Weak function; merged against existing definitions with register-count voting |

Inside `sub_45D180`, the function then consults `st_other & 0x10` (NVIDIA's visibility bit) to distinguish a **kernel entry point** from a **device function**:

| Bit | Hex | Meaning |
|---|---|---|
| `st_other[4]` | `0x10` | Kernel entry (`__global__`). Cleared for `__device__` functions. |
| `st_other[5]` | `0x20` | Bindless function (texture/surface binding descriptor) |
| `st_other[6]` | `0x40` | Exported across translation units |
| `st_other[7]` | `0x80` | Internal / intrinsic |

When `(st_other ^ existing_st_other) & 0x10` is non-zero, the function emits `unk_2A5B8F0` — "conflicting definitions for %s, one is a kernel and the other is not" — and aborts. Kernels and device functions with the same mangled name cannot coexist.

The classification then dispatches:

1. **Kernel** (`st_other & 0x10`): symbol is tagged with the kernel bit and placed in the kernel table. A per-function `.nv.info.<name>` section is expected; its absence downgrades to a warning.
2. **Device function** (`st_other & 0x10 == 0`): symbol goes into the regular function table; callers resolve via the call-graph pass.
3. **Weak definition**: sub_45D180 reads the register count from the existing symbol (if any), compares against the new candidate, and picks the higher of the two so the kernel launch allocates enough registers for any caller. Same logic for barrier counts, stack sizes, and shared-memory usage.

### Non-Function Pass (Second Pass)

The second pass (`sub_45E7D0:802-1014`) handles `st_info & 0xF` values 0, 1, 3-12. The important branches are:

| `st_info & 0xF` | ELF type | nvlink handler |
|---|---|---|
| `0` (`STT_NOTYPE`) | untyped | Passed to `sub_4411B0` / `sub_438A00` for opaque placement |
| `1` (`STT_OBJECT`) | global variable | Handled by `sub_440740` with the object's section binding |
| `2` (`STT_FUNC`) | function | **Skipped** here; processed in first pass |
| `10` (`STT_LOOS`, `STT_NV_EXT_TEX`) | texture | Routed through `sub_438A00` (bindless texture path) |
| `11` (`STT_NV_EXT_SURF`) | surface | Routed through `sub_438B20` (surface handle path) |
| `12` (`STT_NV_EXT_SAMP`) | sampler | Routed through `sub_438A90` (sampler handle path) |
| `13` | (reserved, NVIDIA-private) | Routed to the kernel-specific path as if it were a function entry |

For each STT_OBJECT entry (global variable), the code:

1. Reads `st_shndx` from `st_info` (symbol's owning section index).
2. Looks up the corresponding linker section via `v19[1]` — the section index map built earlier in the first pass.
3. Fails with `"section not mapped"` (`unk_2A5B990`) if the source section was filtered out.
4. Adds the symbol via `sub_4411B0` (symbol-add by name) or `sub_440740` (symbol-add with explicit attributes).
5. On collision with an existing definition, checks that the `st_info` types match — if not, emits `unk_2A5BA10` ("symbol type mismatch for %s").

A special case handles `st_shndx == SHN_ABS (0xFFF1)` and `st_shndx == 0`: these symbols bypass section mapping entirely and go straight into the global namespace.

### Symbol Index Remapping

After both passes, `v19[1]` contains the input-to-linker symbol-index map. This map is used during the `.nv.info` TLV parse at `sub_45E7D0:1900-1975`, where symbol-referencing attributes (kinds 2, 6, 7, 8, 9, 10, 15, 17, 18, 19, 20, 23, 35, 38, 47, 55, 59, 69) have their embedded symbol indices rewritten from the cubin-local numbering to the linker-global numbering. The kinds list is the exhaustive switch case from `sub_45E7D0:1917-1977`.

## Constant Bank Extraction

The constant-bank extraction happens as a natural consequence of the section dispatch described above. There is no separate extraction pass — the moment a `.nv.constant<N>` section is classified with type `0x70000064 + N`, it becomes a routine section input for the constant-bank merge pipeline (`sub_438640`).

### From .nv.constant to SHT_CUDA_CONSTANTn

Concrete example from a sample cubin:

```text
Input section header:
  sh_name    -> ".nv.constant0"        (from shstrtab)
  sh_type    -> 1  (SHT_PROGBITS)      or 0x70000006 (SHT_CUDA_CONSTANT)
  sh_flags   -> 0x2 (SHF_ALLOC)
  sh_offset  -> 0x5000
  sh_size    -> 0x140
  sh_link    -> 0 (no associated section)
  sh_info    -> symbol index of owning kernel

After sub_45E7D0:1006-1012 classification:
  internal_type -> 0x70000064  (strtol("0", 0, 10) == 0, 0 + 0x70000024 = 0x70000024,
                                then + 0x40 from the NV offset = 0x70000064)
```

Wait — the literal in the decompiled code is `1879048292` which equals `0x70000064` directly. The `0x70000024` base from the earlier description is the value `1879048228`, but the actual code uses `1879048292 = 0x70000064` which already includes the `+64` offset. Correcting the formula:

```text
internal_type = strtol(name + 12, 0, 10) + 0x70000064   // bank 0 at base
              = strtol(name + 12, 0, 10) + 1879048292
```

So `.nv.constant0` -> `0x70000064` (SHT_CUDA_CONSTANT0), `.nv.constant1` -> `0x70000065`, ..., `.nv.constant17` -> `0x70000075`. This matches the [Constant Banks](../elf/constant-banks.md) page's type range exactly.

### Constant Data Flow

Once classified, the constant-bank section is added to the linker state via the same `sub_441AC0` + `sub_440590` + `sub_440350` chain used for all other sections. The raw bytes are copied by `sub_432B10` (memory allocator + memcpy). At merge time, `sub_438640` collects all sections with the same bank number across all input cubins and concatenates/deduplicates them using the hash-based dedup engine `sub_4339A0`. The dedup hash keys on the byte content, so two cubins with identical bank-0 constants share storage.

Relocations that point into constant banks (`R_CUDA_ABS32_LO_0`, `R_CUDA_ABS32_HI_0`, `R_CUDA_CONST_FIELD`, etc.) are resolved after merge in `sub_46C1B0` — see [linker/r-cuda-relocations.md](../linker/r-cuda-relocations.md) for the full relocation set.

## Compressed Section Handling

**nvlink does NOT decompress cubin sections.** There is no LZ4, zlib, zstd, or `SHF_COMPRESSED` (`0x800`) handling on the cubin-loading path. The validator `sub_43DD30` does not even acknowledge the `SHF_COMPRESSED` flag — any section whose bytes appear compressed would simply be treated as opaque bytes, merged verbatim, and almost certainly fail later relocation resolution or FNLZR parsing.

Proof by exhaustion:

1. A keyword search for `LZ4`, `lz4`, `zlib`, `inflate`, `deflate`, `zstd`, `compress`, `ELFCOMPRESS`, `SHF_COMPRESSED`, and `0x800000` across all 40,210 decompiled functions finds only matches inside the bundled OCG (open code generator, libcompiler) in the `0x1CCxxxx`--`0x1D3xxxx` address range. Those functions are invoked from PTX codegen (constant table compression) and LTO bitcode packing, not from the cubin-loading or section-merging paths.
2. `sub_45E7D0` reads section bytes via `&a2->__size[sh_offset]` and passes them straight to `memcpy`. There is no conditional decompression.
3. Ptxas does not emit `SHF_COMPRESSED` sections in cubin output. DWARF debug sections in cubins are uncompressed, unlike their host-ELF counterparts. The host linker (`ld`) may insert `.debug_*` compression into the final executable, but device cubins embedded in a fatbin are raw.
4. Fatbin containers themselves *can* be LZ4-compressed (the fatbin header records a compression method), but that decompression happens in `sub_43B2D0` — the fatbin extractor — **before** the resulting cubin bytes reach `sub_426570`. See [Fatbin Extraction](fatbin-extraction.md) for the decompression details.

This means that a cubin on disk is always in the exact byte format ptxas produced. The linker simply maps it into arena memory and walks it.

## Validation Checks on Cubin Structure

A cubin passes through three layers of validation, each adding another invariant:

### Layer 1: Structural (`sub_43DD30`)

Documented above. This layer verifies:

- Header field sizes (`e_phentsize`, `e_shentsize`, `e_shstrndx`)
- Program header table range within buffer
- Section header table range within buffer
- Per-section data range within buffer (excluding `SHT_NOBITS` and NVIDIA's exempted types)
- No integer overflow in offset + size arithmetic

Return: bool — false on any violation. The caller (`sub_43E100`, the fatbin extractor, `sub_426570`) must discard the cubin on failure.

### Layer 2: Extent (`sub_43DA80`)

`sub_43DA80` computes the high-water mark of the ELF: the maximum of:
- `e_phoff + e_phentsize * e_phnum` (end of program header table)
- `e_shoff + e_shentsize * e_shnum` (end of section header table)
- For each section: `sh_offset + sh_size` (skipping `SHT_NOBITS` and NV-exempt types)

This value must not exceed the buffer length. `sub_43DD30` uses it as the final bounds gate. Arithmetic overflow is checked explicitly: the code guards against `v34 * v33 / v33 != v34` (integer-multiplication overflow) and `v35 > ~v9` (additive overflow).

### Layer 3: Architecture (`sub_426570`)

- `e_type != ET_EXEC (2)` — fully-linked executables (a previous nvlink output) rejected outright; the test reads `*(WORD *)(ehdr + 16) == 2`
- ELF class matches `--machine` (`dword_2A5F30C`, 32 or 64)
- `e_ident[EI_OSABI]` matches the expected value (`0x41` for Mercury, `0` for legacy)
- `e_ident[EI_CLASS]` sub-field (NVIDIA uses byte 7 as a sub-class indicator; expected 7 for Elf32/legacy, 8 for Mercury)
- `e_flags` SM version matches the `--arch` target (with family compatibility)
- `e_flags` relocatable flag (bit `0x400` for Mercury, `0x4000` for legacy)
- Toolkit version from `.nv.compat` / `e_flags` is not newer than the linker
- SM-specific toolkit lower bounds: SM 50 requires toolkit > 64 (`0x40`), SM 90 requires toolkit > 119 (`0x77`)
- First cubin lock-in: after the first non-SASS cubin arrives, Mercury mode is disabled for the rest of the link

### Layer 4: Post-Link Precondition (`sub_4275C0`)

Applied only if FNLZR is invoked:

- **Post-link mode**: cubin MUST have the SASS flag set (`e_flags & 0x1` on Mercury, `e_flags & 0x80000000` on legacy)
- **Pre-link mode**: cubin MUST NOT have the "already finalized" flag (`(e_flags >> 2) & 1` on Mercury, `(e_flags & 0x80004000) == 0` on legacy)

Violations in either case emit `"Internal error"` (`unk_2A5B670`). These are defensive asserts — if the earlier pipeline is correct, they never fire.

### Integer Overflow Protection

`sub_43DD30` and `sub_43DA80` both contain explicit overflow guards:

```c
// sub_43DD30 Elf64 loop, line 105
v23 = sh_offset;
v24 = sh_size;
if (base + v23 + v24 > base + buf_size || v24 > ~v23 || v23 + v24 > ~v19)
    return 0;   // overflow: section would wrap the address space
```

The triple-condition check validates:
1. Section content fits in the buffer
2. `sh_offset + sh_size` does not overflow (`sh_size > ~sh_offset` means the sum would wrap)
3. The section header address + sh_offset + sh_size does not wrap (corner case for sections near the high end of the ELF)

This level of paranoia is necessary because cubins may be embedded in fatbins of arbitrary origin, and a malicious fatbin could contain a truncated or crafted ELF that tries to index past the end of the allocation.

## Complete Cubin Loading Flow

```text
Input file identified as cubin (ELF magic + e_machine == 190)
  |
  v
sub_43D970: is_elf() -- validate ELF magic
  |
  v
sub_43D9A0: is_elf64() -- determine Elf32 vs Elf64
  |
  +--> Elf32: sub_43E100 loads from file with size >= 52 check
  +--> Elf64: loaded in main() via fread into arena buffer
  |
  v
sub_43DD30: validate_elf_structure() -- bounds-check all headers
  |
  v
sub_426570: validate_arch_and_add()
  |  1. Reject e_type == ET_EXEC (already-linked executables; previous nvlink output)
  |  2. Check word size (32/64) matches --machine
  |  3. Check ELF OSABI byte for class expectations
  |  4. Extract SM version from e_flags
  |  5. Format "sm_XX" or "compute_XX" string
  |  6. Match against --arch via sub_4878A0
  |  7. Fallback: check .nv.compat via sub_43E610
  |  8. Validate toolkit version
  |  9. Set SASS / legacy mode flags
  |
  v
sub_43DA40: is_sass_cubin() -- check SASS flag
  |
  +--> SASS: proceed to merge, later FNLZR post-link
  +--> PTX-only: lock into legacy mode
  |
  v
sub_42A680: register_module_for_linking()
  |
  v
Cubin enters the merge phase (sub_45E7D0)
```

## Error Messages

| Error descriptor | Message pattern | Condition |
|---|---|---|
| `unk_2A5B700` | (null header / corrupt ELF) | ELF header at offset `v12` is NULL |
| `unk_2A5B690` | Architecture word-size mismatch | Cubin is 32-bit but target is `-m64`, or vice versa |
| `unk_2A5B680` | ELF class mismatch | Cubin ELF class byte does not match expected value (7 or 8) |
| `unk_2A5B6A0` | `"SM Arch ('%s') not found in '%s'"` | Cubin SM arch does not match `--arch` target |
| `unk_2A5B6B0` | Architecture requires modern ELF class | Mercury cubin with non-Mercury linker configuration |
| `unk_2A5B670` | `"specified arch exceeds buffer length"` / `"Internal error"` | Buffer overflow in arch string or FNLZR precondition failure |
| `unk_2A5B640` | Toolkit version too new | Cubin toolkit version exceeds linker's known version |
| `unk_2A5B630` | SM 50 requires newer toolkit | SM 50 cubin with toolkit version <= 64 |
| `unk_2A5B620` | SM 90 requires newer toolkit | SM 90 cubin with toolkit version <= 119 |
| `unk_2A5B6E0` | First cubin arch notification | Informational: logs the architecture of the first cubin processed |
| `unk_2A5B6C0` | `"FNLZR failure"` | Post-link binary rewriting failed |
| `unk_2A5B5C0` | Relocatable flag warning | Cubin has unexpected relocatable flag state |

All error messages are emitted through the unified diagnostic function `sub_467460`, which handles severity levels (fatal error, warning, info) based on the descriptor address prefix.

## Cross-References

- [Input File Loop](../pipeline/input-loop.md) — how cubins are dispatched from the main file loop
- [Fatbin Extraction](fatbin-extraction.md) — cubins extracted from fatbin containers follow the same validation path; fatbin LZ4 decompression happens before the cubin bytes reach `sub_426570`
- [168-Byte Input Container](container-struct.md) — the shared opaque struct that fatbin-extracted cubins traverse before re-entering this validation path (content_type 3)
- [ELF Parsing](elf-parsing.md) — the `sub_448360` / `sub_46B590` ELF header accessor functions; `sub_43DD30` validator documented in both pages
- [Constant Banks](../elf/constant-banks.md) — the name-to-index mapping and merge pipeline for sections classified here as `SHT_CUDA_CONSTANTn`
- [.nv.info Metadata](../elf/nv-info.md) — the TLV attribute catalog; sections allocated by `sub_4504B0` and parsed inline by `sub_45E7D0` (merge) / `sub_451D80` (finalize) after section classification
- [NVIDIA Sections](../elf/nvidia-sections.md) — full catalog of `SHT_CUDA_*` section types referenced by the dispatch table
- [Section Merging](../linker/section-merging.md) — the downstream merge stage that consumes classified sections
- [R_CUDA Relocations](../linker/r-cuda-relocations.md) — relocations that reference constant banks and other NV sections after merge
- [Merge Phase](../pipeline/merge.md) — where validated cubins are merged into the output ELF (driver for `sub_45E7D0`)
- [Finalization Phase](../pipeline/finalize.md) — the FNLZR post-link transform context
- [Mercury / FNLZR](../mercury/fnlzr.md) — detailed breakdown of the `sub_4748F0` finalizer engine invoked by `sub_4275C0`
- [Device ELF Format](../elf/device-elf-format.md) — `e_machine == 190`, `e_flags` SM version encoding, and the Mercury `e_type == 0xFF00` variant
