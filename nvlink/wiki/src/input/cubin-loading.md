# Cubin Loading

A cubin is a CUDA device ELF -- an ELF binary with `e_machine == 190` (`EM_CUDA`) containing compiled SASS instructions, constant data, and NVIDIA-specific metadata sections. When nvlink encounters a cubin input (either directly on the command line or extracted from a fatbin container), it must validate the ELF structure, confirm the SM architecture matches the link target, distinguish SASS from PTX-only cubins, and decide whether the object requires pre-link finalization. This page documents the complete path from raw bytes to a validated cubin ready for the merge phase.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_43D970` | `is_elf` | 19 B | Checks 4-byte ELF magic (`0x7F454C46`) |
| `sub_43D9A0` | `is_elf64` | 18 B | Tests ELF class byte (`e_ident[EI_CLASS] == 2`) |
| `sub_43D9B0` | `is_host_elf` | 42 B | Tests `e_type == ET_REL` (1) to distinguish host from device |
| `sub_43DA40` | `is_sass_cubin` | 52 B | Checks SASS flag in `e_flags` (class-dependent bitmask) |
| `sub_43DD30` | `validate_elf_structure` | 536 B | Full structural validation of section/program headers against buffer size |
| `sub_43E100` | `load_cubin_from_file` | 232 B | Elf32 file loader: open, read, validate, return in-memory buffer |
| `sub_43E420` | `get_elf_toolkit_version` | 116 B | Extracts toolkit version from `e_flags` or `.nv.compat` section |
| `sub_43E6F0` | `has_abi_suffix` | 172 B | Detects the `a` suffix flag (ABI variant) in `e_flags` |
| `sub_43E610` | `read_nv_compat` | 168 B | Parses the `.nv.compat` section for extended arch metadata |
| `sub_426570` | `validate_arch_and_add` | 7,427 B | Validates architecture match, configures link mode, adds cubin to linker |
| `sub_4275C0` | `post_link_transform` | 3,989 B | FNLZR (Finalizer) -- post-link binary rewriting for Mercury/SASS targets |
| `sub_4878A0` | `arch_string_match` | 328 B | Compares input arch string against target `--arch` value |

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
// For cubins, e_type is typically ET_EXEC (2) or the Mercury type 0xFF00
bool is_host_elf(void *elf_buf) {
    if (!elf_buf) return false;
    if (((uint8_t *)elf_buf)[4] == 2)  // ELFCLASS64
        return get_elf64_header(elf_buf)->e_type == 1;  // ET_REL
    else
        return get_elf32_header(elf_buf)->e_type == 1;
}
```

Device cubins produced by ptxas have `e_type == ET_EXEC` (2). Relocatable device objects (produced with `-r`) have `e_type == ET_REL` (1) but still carry `e_machine == 190`. Mercury objects use the custom type `0xFF00`. The combination of `e_machine == 190` with any `e_type` value routes through the cubin handler; `sub_43D9B0` is used later during architecture validation to handle relocatable cubins specially.

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

## Elf32 File Loader: sub\_43E100

`sub_43E100` is a standalone cubin loader that reads a cubin from a file path, validates it, and returns a heap-allocated buffer. It is the Elf32 loading path (the condition checks `e_ident[EI_CLASS] == 1`):

```c
// sub_43E100 -- load_cubin_from_file (Elf32 path)
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

    // Validate: correct read length, ELFCLASS32, ELF magic
    Elf32_Ehdr *ehdr = get_elf32_header(buf);
    if (nread != size || ehdr->e_ident[EI_CLASS] != 1 || ehdr->e_ident_magic != 0x464C457F) {
        arena_free(buf, 1);
        return NULL;
    }

    // Full structural validation
    if (!validate_elf_structure(buf, size)) {
        arena_free(buf, size);
        return NULL;
    }

    // Must be EM_CUDA
    if (get_elf32_header(buf)->e_machine != 190) {
        arena_free(buf, size);
        return NULL;
    }

    return buf;
}
```

Key details:
- The minimum file size threshold is **52 bytes** (the size of an Elf32 header).
- Memory is allocated from the linker's arena allocator, not `malloc`.
- `sub_43DD30` (`validate_elf_structure`) performs a thorough check of all section headers and program headers, verifying that every offset+size pair falls within the buffer bounds.
- The `e_machine == 190` check is the final gate.

## Structural Validation: sub\_43DD30

`sub_43DD30` validates the complete ELF structural integrity of an in-memory cubin before any further processing. It checks both Elf32 and Elf64 paths:

**For Elf32:**
- `e_phentsize == 40` (sizeof Elf32\_Phdr must be 40 if program headers exist)
- `e_shentsize` is zero or `e_shstrndx == 32` (section header entry size sanity)
- Program header table offset (`e_phoff`) is within the buffer and `e_phoff > 0x33` (beyond the ELF header)
- Total program header table size (`e_phoff + e_phentsize * e_phnum`) fits in buffer
- Section header table offset (`e_shoff`) and total size fits in buffer
- For each section: if the section type is not `SHT_NOBITS` (8) and not in the NVIDIA-specific range (`0x70000007`--`0x70000015`, which includes `SHT_CUDA_INFO`, `SHT_CUDA_CALLGRAPH`, etc.), the section data range `[sh_offset, sh_offset + sh_size)` must fit within the buffer

**For Elf64:**
- `e_phentsize == 64` (sizeof Elf64\_Phdr)
- `e_shentsize` is zero or `e_shstrndx == 56`
- Same offset/size boundary checks as Elf32, adjusted for 64-bit field widths
- Overflow protection: checks `sh_offset + sh_size` does not wrap around

The NVIDIA-specific section types that are exempted from the data-range check (they may be virtual/metadata-only):

| Type value | Constant name | Hex |
|---|---|---|
| `0x70000007` | `SHT_CUDA_INFO` | `0x70000007` |
| `0x70000008` | `SHT_CUDA_CALLGRAPH` (approx) | `0x70000008` |
| `0x7000000A` | `SHT_CUDA_RELOCINFO` (approx) | `0x7000000A` |
| `0x70000015` | `SHT_CUDA_UDT`/`SHT_CUDA_UFT` | `0x70000015` |

The validation function computes these exemptions with a bitmask check on `(section_type - 0x70000007)`:

```c
uint32_t relative = section_type - 0x70000007;
bool exempt = (section_type == SHT_NOBITS);
if (relative <= 14)
    exempt |= (0x400D >> relative) & 1;  // bits 0,2,3,14 set
```

The bitmask `0x400D` in binary is `0100 0000 0000 1101`, exempting offsets 0, 2, 3, and 14 relative to `0x70000007`.

## Architecture Extraction from e\_flags

The SM architecture version is encoded in `e_flags` with a layout that depends on the ELF OSABI byte:

### Legacy Layout (OSABI != 0x41)

```
e_flags (Elf32_Ehdr or Elf64_Ehdr):
  bits [7:0]    = SM version number (e.g., 75 for sm_75, 90 for sm_90)
  bit  [11]     = ABI suffix ('a') flag
  bit  [14]     = SASS flag (contains machine code)
  bit  [31]     = relocatable flag (signed: e_flags < 0)
  bits [19:16]  = toolkit version (Elf32 only, from e_flags of Elf32)
```

### Modern Layout (OSABI == 0x41, Mercury)

```
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

The function immediately rejects `ET_DYN` objects (`e_type == 2`): shared libraries cannot be device-linked.

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

`sub_4878A0` (`arch_string_match`) compares the constructed `arch_str` against the global target `qword_2A5F318` (the `--arch` value). The comparison is not a simple string equality -- it parses both architecture strings into structured records and applies version compatibility rules:

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

When the first non-SASS cubin arrives, the linker locks into legacy mode: Mercury flags are cleared and cannot be re-enabled. This is a one-way transition -- once a legacy cubin enters the link, the entire output is legacy.

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

```
FNLZR: Input ELF: <filename>
FNLZR: Post-Link Mode          (or "Pre-Link Mode")
FNLZR: Flags [ <post_link> | <mercury_capable> ]
FNLZR: Starting <filename>
FNLZR: Ending <filename>
```

If no filename is available (in-memory cubin from fatbin extraction), the placeholder `"in-memory-ELF-image"` is used.

## Complete Cubin Loading Flow

```
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
  |  1. Reject e_type == ET_DYN (shared libraries)
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

- [Input File Loop](../pipeline/input-loop.md) -- how cubins are dispatched from the main file loop
- [Fatbin Extraction](fatbin-extraction.md) -- cubins extracted from fatbin containers follow the same validation path
- [ELF Parsing](elf-parsing.md) -- the `sub_448360` / `sub_46B590` ELF header accessor functions
- [Merge Phase](../pipeline/merge.md) -- where validated cubins are merged into the output ELF
- [Finalization Phase](../pipeline/finalize.md) -- the FNLZR post-link transform context
