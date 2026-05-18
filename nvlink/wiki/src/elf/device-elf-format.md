# Device ELF Format

CUDA device code is packaged as ELF binaries with an NVIDIA-proprietary OS/ABI and a dedicated machine type. nvlink both consumes and produces these device ELFs. This page documents the complete device ELF format as understood from reverse engineering the elfw constructor (`sub_4438F0`) and the ELF serialization path (`sub_45BF00`), cross-referenced with the header validation functions and the main linking loop. The format is a strict superset of standard ELF64 (or ELF32 for legacy targets) -- every field follows the System V ELF specification, but NVIDIA overloads the `e_ident`, `e_type`, `e_flags`, and section type spaces with GPU-specific semantics.

## Key Facts

| Property | Value |
|---|---|
| `e_machine` | `0xBE` (190) -- `EM_CUDA`, registered with the ELF standards committee |
| OS/ABI (64-bit GPU) | `0x41` (65) at `e_ident[EI_OSABI]` -- device ABI for 64-bit CUDA targets |
| OS/ABI (32-bit GPU) | `0x33` (51) -- device ABI for 32-bit CUDA targets (legacy) |
| ELF class | `ELFCLASS64` (2) for all modern targets; `ELFCLASS32` (1) for 32-bit device code |
| `e_type` values | `ET_REL` (1) relocatable, `ET_EXEC` (2) executable, `0xFF00` Mercury relocatable |
| Constructor | `sub_4438F0` (`elfw_create`) -- 14,821 bytes, allocates 672-byte `elfw` struct |
| Serializer | `sub_45BF00` (`write_elf_to_buffer`) -- 13,258 bytes |
| File writer | `sub_45C920` -- writes elfw to file descriptor |
| Memory writer | `sub_45C950` -- writes elfw to arena buffer |
| Initial sections | `.shstrtab`, `.strtab`, `.symtab`, `.symtab_shndx` (always); `.note.nv.tkinfo`, `.note.nv.cuinfo` (device ELF only) |

## ELF Identification (`e_ident`)

The 16-byte ELF identification array carries both standard ELF metadata and NVIDIA-specific ABI tags.

### Byte Layout

| Offset | Field | Device ELF (64-bit) | Device ELF (32-bit) | Notes |
|---|---|---|---|---|
| 0--3 | `EI_MAG` | `7F 45 4C 46` | `7F 45 4C 46` | Standard ELF magic |
| 4 | `EI_CLASS` | `02` (ELFCLASS64) | `01` (ELFCLASS32) | Set from `a2` parameter: `(a2 != 0) + 1` |
| 5 | `EI_DATA` | `01` (little-endian) | `01` (little-endian) | Hardcoded via `*(_WORD *)(v17 + 5) = 257` (0x0101) |
| 6 | `EI_VERSION` | `01` (EV_CURRENT) | `01` (EV_CURRENT) | Upper byte of the 0x0101 word |
| 7 | `EI_OSABI` | `0x41` (65) | `0x33` (51) | Selects the `e_flags` encoding scheme |
| 8 | `EI_ABIVERSION` | `a3` (ELF ABI version) | `a3` | Passed through from caller |

The OS/ABI byte determines far more than ABI compatibility -- it selects which `e_flags` encoding is used and how the SM architecture is packed into the header. When `a9 & 0x8000` is set in the constructor, the ELF is marked as a device ELF (OSABI `0x41`); otherwise it is a 32-bit device ELF (OSABI `0x33`).

### OS/ABI Selection Logic

```c
// sub_4438F0 -- elfw_create, offset into ELF header setup
// a9 is the merge_flags parameter
if (a9 & 0x8000) {
    // Device ELF -- 64-bit GPU ABI
    v17->e_ident[EI_OSABI] = 0x41;   // 65
} else {
    // Legacy / 32-bit GPU ABI
    v17->e_ident[EI_OSABI] = 0x33;   // 51
}
v17->e_ident[EI_ABIVERSION] = a3;
v17->e_machine = 190;                // EM_CUDA, always
```

## ELF Type (`e_type`)

Device ELFs use three `e_type` values, each representing a different linking stage:

| `e_type` | Value | Meaning | When produced |
|---|---|---|---|
| `ET_REL` | `1` | Relocatable object | `nvlink -r` (relocatable link), or unlinked `.o` from ptxas |
| `ET_EXEC` | `2` | Executable cubin | Normal final link output (non-Mercury) |
| `0xFF00` | 65280 | Mercury relocatable | Pre-link Mercury object (`sm >= 100`) before finalization |

The `e_type` is stored at `elfw+16` (the standard ELF header offset for `e_type` in Elf64). It is set directly from the first parameter (`a1`) of `elfw_create`:

```c
// e_type assignment in elfw_create (sub_4438F0)
v114 = (__int16)a1;                    // truncate first parameter to 16 bits
*(WORD *)(v17 + 16) = v114;           // e_type = a1
```

The caller determines the value:

```c
// In sub_1406B40 (link output path):
v39 = 65280;                           // 0xFF00 (Mercury)
if (!is_mercury)
    v39 = (is_relocatable == 0) + 1;   // 1 = ET_REL, 2 = ET_EXEC
sub_4438F0(v39, ...);

// In main_0x409800:
sub_4438F0((byte_2A5F1E8 == 0) + 1, ...);   // 1 or 2
```

**Important**: The constructor also writes values `1` or `4` to `*(DWORD *)(v17 + 48)` (byte offset 48 = `e_flags`), which are the `link_state` flags seeded into `e_flags` before the SM architecture is ORed in. These are **not** `e_type` writes despite appearing at a confusingly similar DWORD offset in decompiled output.

For the legacy (OSABI `0x33`) path, the `e_type` is also set from the first parameter.

## ELF Flags (`e_flags`)

The `e_flags` field encodes the SM architecture and a link-state tag. The encoding differs between OSABI `0x41` and OSABI `0x33`. Debug state and other ABI attributes are stored in the `merge_flags` field of the `elfw` struct (offset 76), not in `e_flags` itself.

### OSABI 0x41 (64-bit GPU) -- `e_flags` Layout

| Bits | Width | Field | Description |
|---|---|---|---|
| [7:0] | 8 | `link_state` | Link state flags: `0x01` = relocatable, `0x04` = executable (SASS present). The SM architecture reader (`sub_4402A0`) ignores these bits entirely. |
| [23:8] | 16 | `sm_major` | SM major architecture number (e.g., 90 = 0x5A for Hopper). Extracted by the reader as `(uint16_t)(e_flags >> 8)`. Current SM values (all < 256) only occupy bits [15:8]; bits [23:16] are zero but architecturally part of the field. |
| [31:24] | 8 | `reserved` | Zero in all observed outputs |

The SM minor version is **not** stored in `e_flags` for OSABI `0x41`. It is stored internally in the `elfw` struct at offset 134 (`*(WORD *)(elfw + 134) = a5`).

The constructor packs the architecture as `(sm_major << 8) | link_state`:

```c
// Device ELF (OSABI 0x41) flags encoding in sub_4438F0
// a4 = SM major version
// Step 1: set link_state at DWORD offset 12 (= byte offset 48 = e_flags)
if (relocatable)
    *(DWORD *)(v17 + 48) = 1;       // link_state = 0x01 (relocatable)
else
    *(DWORD *)(v17 + 48) = 4;       // link_state = 0x04 (executable)

// Step 2: OR in sm_major shifted left 8
v22 = *(DWORD *)(v17 + 48);         // read back 1 or 4
*(DWORD *)(v17 + 48) = (a4 << 8) | v22;   // e_flags = (sm_major << 8) | link_state
```

The SM architecture reader (`sub_4402A0`) confirms this encoding:

```c
// sub_4402A0 -- get_sm_arch(elfw)
uint32_t flags = *(DWORD *)(elfw + 48);       // e_flags
if (*(BYTE *)(elfw + 7) == 0x41)              // OSABI == 0x41?
    return (uint16_t)(flags >> 8);             // bits [23:8] = sm_major
return (uint8_t)flags;                         // bits [7:0] for OSABI 0x33
```

The relocatable flag is checked by `sub_443260`:

```c
// sub_443260 -- relocatable check
uint32_t mask = 0x80000000;          // OSABI 0x33 default
if (*(BYTE *)(elfw + 7) == 0x41)
    mask = 1;                         // OSABI 0x41: bit 0 = relocatable
if ((mask & *(DWORD *)(elfw + 48)) == 0)
    // Not relocatable
```

### OSABI 0x33 (32-bit GPU) -- `e_flags` Layout

| Bits | Width | Field | Description |
|---|---|---|---|
| [7:0] | 8 | `sm_major` | SM architecture number directly (e.g., 75 for Turing). Extracted by `sub_4402A0` as `(uint8_t)e_flags`. |
| [15:8] | 8 | `reserved` | Zero / unused gap |
| [23:16] | 8 | `sm_minor` | SM minor version. Packed via `(a5 << 16)` in the constructor. |
| [30:24] | 7 | `reserved` | Zero |
| [31] | 1 | `is_relocatable` | Set to `1` (making the full DWORD `0x80000000`) when relocatable |

```c
// 32-bit GPU (OSABI 0x33) flags encoding in sub_4438F0
// a4 = sm_major (v19), a5 = sm_minor, v22 = relocatable_bit
*(DWORD *)(v17 + 48) = v19 | (a5 << 16) | v22;
// v22 = 0x80000000 if relocatable, 0 otherwise
// Result: e_flags = sm_major[7:0] | sm_minor[23:16] | reloc[31]
```

### Flag Bit Decomposition

The constructor extracts individual flag bits from the `merge_flags` parameter (`a9`) into boolean fields in the 672-byte `elfw` struct:

| Bit in `a9` | `elfw` offset | Meaning |
|---|---|---|
| `0x0001` (bit 0) | byte 84 | Debug info present |
| `0x0002` (bit 1) | byte 85 | Extended debug |
| `0x0004` (bit 2) | byte 87 | Preserve relocations |
| `0x0008` (bit 3) | byte 88 | Reserve null pointer |
| `0x0010` (bit 4) | byte 89 | Force RELA (or set if `a10` relocatable) |
| `0x0020` (bit 5) | byte 90 | Optimize data layout |
| `0x0040` (bit 6) | byte 92 | Extra warnings |
| `0x0080` (bit 7) | byte 94 | Combined with `sm_minor > 0x45` check |
| `0x0100` (bit 8) | byte 93 | Verbose keep |
| `0x0200` (bit 9) | byte 86 | Allow undefined globals |
| `0x0400` (bit 10) | -- | Allocate separate arena for ELF writer |
| `0x0800` (bit 11) | byte 96 | Suppress debug info |
| `0x1000` (bit 12) | byte 99 | Inverted: `(flags >> 12) ^ 1) & 1` (legacy mode) |
| `0x2000` (bit 13) | byte 100 | Stack protector |
| `0x4000` (bit 14) | byte 91 | Enable extended shared memory |
| `0x8000` (bit 15) | byte 101 | Device ELF (vs host/32-bit) |
| `0x70000` (bits 16--18) | dword at offset 68 | Link mode flags (stored as `a9 & 0x70000`) |
| `0x80000` (bit 19) | dword at offset 76 | Relocatable flag (forced on when `a10` is set) |

## The `elfw` Struct (672 bytes)

`sub_4438F0` allocates a 672-byte structure via `arena_alloc` that serves as the complete in-memory representation of a device ELF being constructed. This is the central data structure for nvlink's output ELF.

### Layout (reconstructed from `sub_4438F0`)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `elf_magic` | `0x464C457F` -- the `\x7fELF` magic, stored in native byte order |
| 4 | 1 | `ei_class` | `(is_64bit != 0) + 1` -- `1` for Elf32, `2` for Elf64 |
| 5--6 | 2 | `ei_data_version` | `0x0101` -- ELFDATA2LSB + EV_CURRENT |
| 7 | 1 | `ei_osabi` | `0x41` (device) or `0x33` (32-bit) |
| 8 | 1 | `ei_abiversion` | `a3` parameter |
| 16 | 2 | `e_type` | ELF type (1, 2, or 0xFF00) |
| 18 | 2 | `e_machine` | `190` (0xBE) always |
| 20 | 4 | `e_version` | API version (`a7`), or `1` for device ELF |
| 48 | 4 | `e_flags` | OSABI 0x41: `(sm_major << 8) \| link_state`; OSABI 0x33: `sm_major \| (sm_minor << 16) \| reloc_bit` |
| 62 | 2 | `shstrtab_idx` | Section index of `.shstrtab` |
| 64 | 1 | `verbose_flags` | `a8` parameter (verbose output level) |
| 68 | 4 | `link_mode` | `a9 & 0x70000` -- link mode control bits |
| 72 | 4 | `sm_arch` | `a4` parameter (SM major version) |
| 76 | 4 | `merge_flags` | Full `a9` parameter (or `a9 \| 0x80000` if relocatable) |
| 80 | 1 | `debug_flag` | `a6` parameter |
| 83 | 1 | `has_shstrtab` | `1` if section[42] (shstrndx stored) != 0 |
| 84--100 | 17 | `flag_booleans` | Individual boolean flags extracted from merge_flags |
| 101 | 1 | `is_device_elf` | `(a9 & 0x8000) != 0` |
| 108 | 32 | `tkinfo_buffer` | Initialized via `sub_43E490(offset, 1000)` |
| 140 | 32 | `cuinfo_buffer` | Initialized via `sub_43E490(offset, 2000)` |
| 192 | 8 | `string_buffer` | Program header name buffer (set in `sub_443730`) |
| 200--210 | varies | `phdr_offsets` | Program header string offsets |
| 288 | 8 | `section_hash_pos` | Hash table for section lookup by name (positive indices) |
| 296 | 8 | `section_hash_neg` | Hash table for section lookup (negative indices) |
| 304 | 4 | `shstrtab_count` | Count of section name string table entries |
| 312 | 4 | `strtab_count` | Count of symbol string table entries |
| 328 | 8 | `strtab_entries` | Pointer to string table entry array |
| 336 | 8 | `shstrtab_entries` | Pointer to shstrtab entry array |
| 344 | 8 | `pos_sections` | Sorted-array of sections (positive index) |
| 352 | 8 | `neg_sections` | Sorted-array of sections (negative index) |
| 360 | 8 | `section_data` | Array of section data records |
| 368 | 8 | `section_order` | Array mapping virtual indices to physical |
| 376--408 | varies | `sym_tables` | Symbol table management structures |
| 480 | 8 | `file_list` | Linked list of input file names |
| 488 | 8 | `arch_state` | Pointer to architecture-specific state (from `sub_45AC50`/`sub_459640`) |
| 496 | 8 | `entry_hash` | Hash table for entry-point symbols |
| 512 | 8 | `input_files` | Input file tracking for verbose output |
| 520--576 | varies | `sorted_arrays` | Six sorted-arrays for section/symbol management |
| 608 | 8 | `arena_ptr` | Owning memory arena (if `a9 & 0x400`) |
| 616 | 8 | `arena_handle` | Arena handle from `sub_45CAE0` |
| 624 | 4 | `option_flags` | Option parser result from `sub_42F8B0()` |

## Constructor Flow: `sub_4438F0` (`elfw_create`)

The constructor takes 10 parameters and initializes the complete output ELF structure:

```
elfw_create(
    a1: type_or_arena,    // elfw type code (e.g., 1 = relocatable)
    a2: is_64bit,         // 0 = 32-bit, nonzero = 64-bit
    a3: abi_version,      // value for e_ident[EI_ABIVERSION]
    a4: sm_major,         // SM architecture major (e.g., 90 for Hopper)
    a5: sm_minor,         // SM minor version / variant letter
    a6: debug_flag,       // debug info generation flag
    a7: api_version,      // CUDA API version or e_version value
    a8: verbose_flags,    // verbose output control
    a9: merge_flags,      // bitmask controlling all link behavior
    a10: is_relocatable   // explicit relocatable flag
)
```

### Initialization Sequence

1. **Arena creation** (if `a9 & 0x400`): Allocates a separate "elfw memory space" arena with 4096-byte page size via `sub_432020`.

2. **Struct allocation**: Allocates 672 bytes from the arena, zeroes the entire buffer.

3. **ELF header setup**: Writes magic bytes, class, data encoding, OSABI, ABI version, machine type.

4. **Flag decomposition**: Extracts individual boolean flags from `a9` into the flag bytes at offsets 84--100.

5. **Architecture state**: Calls `sub_45AC50` (for relocatable) or `sub_459640` (for executable) to initialize architecture-specific state. Fatal error "couldn't initialize arch state" if this fails.

6. **Hash tables**: Creates two 512-bucket hash tables for section name lookup (`sub_4489C0`).

7. **Sorted arrays**: Creates six 16-element sorted arrays and three 64-element sorted arrays for section and symbol management.

8. **Input file record**: Creates a 16-byte `<input>` record with the SM minor version.

9. **Core sections**:
   - `.shstrtab` -- section header string table (SHT_STRTAB = 3, alignment 1)
   - `.strtab` -- symbol string table (SHT_STRTAB = 3, alignment 1)
   - `.symtab` -- symbol table (SHT_SYMTAB = 2, linked to `.strtab`). Entry size is 24 bytes for Elf64 or 16 bytes for Elf32; alignment is 8 or 4 respectively.
   - `.symtab_shndx` -- extended section indices (SHT_SYMTAB_SHNDX = 18, 4-byte entries)

10. **Device-only sections** (when `is_device_elf`):
    - `.note.nv.tkinfo` -- tool kit info note (SHT_NOTE = 7, alignment `0x2000000`)
    - `.note.nv.cuinfo` -- CUDA info note (SHT_NOTE = 7, alignment `0x1000000`)

11. **UFT section** (when `e_type != ET_REL`):
    - `.nv.uft.entry` -- unified function table entry points (section type `0x70000011` = 1879048209, 32-byte entries with 32-byte alignment)

12. **Section name registry**: Populates the section name hash from a static table at `off_1D3A9C0` containing known NVIDIA section name strings.

13. **Entry hash table**: Creates an 8-element hash table for kernel entry point tracking.

14. **Option state**: Calls `sub_42F8B0()` to capture the current option parser state.

15. **Finalization**: Calls `sub_4504B0(elfw, 0)` to complete initialization.

## Section Type Encoding

Device ELF sections use both standard ELF section types and NVIDIA vendor types in the `SHT_LOPROC`--`SHT_HIPROC` range (`0x70000000`--`0x7FFFFFFF`).

| Type Value | Name | Description |
|---|---|---|
| `0x00000001` | `SHT_PROGBITS` | Code (`.text.*`), initialized data |
| `0x00000002` | `SHT_SYMTAB` | Symbol table (`.symtab`) |
| `0x00000003` | `SHT_STRTAB` | String tables (`.shstrtab`, `.strtab`) |
| `0x00000007` | `SHT_NOTE` | Note sections (`.note.nv.tkinfo`, `.note.nv.cuinfo`) |
| `0x00000008` | `SHT_NOBITS` | Uninitialized data (`.nv.shared.*`, `.bss`) |
| `0x00000012` | `SHT_SYMTAB_SHNDX` | Extended section indices (`.symtab_shndx`) |
| `0x70000000` | `SHT_CUDA_INFO` | `.nv.info`, `.nv.info.<func>` -- EIATTR metadata records |
| `0x70000007` | `SHT_CUDA_GLOBAL` | `.nv.global` -- uninitialized `__device__` BSS |
| `0x70000009` | `SHT_CUDA_LOCAL` | `.nv.local.<func>` -- per-kernel register spill / local arrays |
| `0x7000000A` | `SHT_CUDA_SHARED` | `.nv.shared.<func>` -- per-kernel `__shared__` memory |
| `0x70000011` | `SHT_CUDA_UFT_ENTRY` | `.nv.uft.entry` -- unified function table entries |
| `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.reservedSmem*` -- reserved shared-memory region markers |
| `0x70000086` | `SHT_CUDA_COMPAT` | `.nv.compat` -- forward/backward compatibility attribute table |

(See [NVIDIA Section Types](nvidia-sections.md) and [Section Catalog](../reference/section-catalog.md) for the full type assignment.)

The data-range validator (`sub_43DD30`) recognizes a *subset* of these -- specifically the four "virtual" types that occupy device memory at runtime but have no file content -- through a bitmask check on `(type - 0x70000007)`:

```c
// Data-less vendor section recognition in elf_validate
uint32_t offset = sh_type - 0x70000007;
if (offset <= 14) {
    if ((0x400D >> offset) & 1)
        // Skip size validation for this section (treated as NOBITS-like)
}
```

The bitmask `0x400D` = `0b0100_0000_0000_1101` selects offsets 0 (`0x70000007 = SHT_CUDA_GLOBAL`), 2 (`0x70000009 = SHT_CUDA_LOCAL`), 3 (`0x7000000A = SHT_CUDA_SHARED`), and 14 (`0x70000015 = SHT_CUDA_SHARED_RESERVED`).

> **QUIRK -- bitmask is based at `SHT_CUDA_GLOBAL` rather than `SHT_LOPROC`.** Standard ELF places the processor-specific range at `SHT_LOPROC = 0x70000000`, but the validator subtracts `0x70000007` (`SHT_CUDA_GLOBAL`) instead. The first seven CUDA types (`SHT_CUDA_INFO`..`SHT_CUDA_METADATA`) all carry real file content and are never exempt; clustering the data-less types into a contiguous run starting at `0x70000007` lets the validator fold the membership test into a single 16-bit immediate `0x400D` and one bit shift. `SHT_CUDA_COMPAT` (`0x70000086`) and `SHT_CUDA_HOST` (`0x70000087`) sit far outside this window and are dispatched separately.

## ELF Serialization

The output ELF is serialized by `sub_45BF00` (`write_elf_to_buffer`), which writes the complete binary image in standard ELF order.

### Write Order

1. **ELF header** (64 bytes for Elf64, 52 bytes for Elf32) -- written from the first 64/52 bytes of the elfw struct, which contain the standard ELF header fields.

2. **Padding byte** -- a single NUL byte after the header. Always written, verified with size check.

3. **Section header string table** (`.shstrtab`) -- all registered section names as NUL-terminated strings, concatenated. Written by iterating the shstrtab entry array.

4. **Symbol string table** (`.strtab`) -- all registered symbol names, same format as shstrtab.

5. **Padding** to `.shstrtab` section offset -- NUL bytes to reach the declared `sh_offset` of section index 3.

6. **Symbol table** (`.symtab`) -- symbol entries in Elf64_Sym (24 bytes each) or Elf32_Sym (16 bytes) format.

7. **Section data** -- remaining sections in index order, with padding between sections to satisfy alignment requirements. Each section's `sh_offset` is validated against the current write position; a mismatch triggers the "Negative size encountered" error.

8. **Program headers** -- program header table, placed after all section data.

9. **Section headers** -- section header table at the file's end.

### Program Header Count Selection

The serializer computes the number of program headers based on the presence of LOAD segments:

```c
// Program header count selection in write_elf_to_buffer
if (has_text_segment && has_data_segment)
    phnum = 4;          // PT_LOAD(text) + PT_LOAD(data) + 2 more
else if (has_text_segment)
    phnum = 3;          // PT_LOAD(text) + 2 more
else if (has_data_segment)
    phnum = 2;          // PT_LOAD(data) + 1 more
else
    phnum = 2;          // minimal: PHDR + NOTE or similar
```

### Two Write Paths

| Function | Address | Destination | Mechanism |
|---|---|---|---|
| `sub_45C920` | 0x45C920 | File | `sub_45B950` opens output file, `sub_45BF00` serializes, `sub_45B6A0` closes |
| `sub_45C950` | 0x45C950 | Memory buffer | `sub_45BA30` allocates arena buffer, `sub_45BF00` serializes, `sub_45B6A0` finalizes |

Both paths call the same `sub_45BF00` serializer with an abstract write interface, differing only in the setup (file open vs buffer alloc) and teardown.

## Architecture Encoding Examples

Here is how several SM architectures are encoded in `e_flags` under OSABI `0x41`. The formula is `e_flags = (sm_major << 8) | link_state` where `link_state` is `0x01` (relocatable) or `0x04` (executable):

### Relocatable Objects (`link_state = 0x01`)

| Architecture | `sm_major` | `e_flags` (hex) | Breakdown |
|---|---|---|---|
| sm\_50 (Maxwell) | 50 (0x32) | `0x00003201` | `(50 << 8) \| 1` |
| sm\_70 (Volta) | 70 (0x46) | `0x00004601` | `(70 << 8) \| 1` |
| sm\_75 (Turing) | 75 (0x4B) | `0x00004B01` | `(75 << 8) \| 1` |
| sm\_80 (Ampere) | 80 (0x50) | `0x00005001` | `(80 << 8) \| 1` |
| sm\_86 (Ampere) | 86 (0x56) | `0x00005601` | `(86 << 8) \| 1` |
| sm\_89 (Ada) | 89 (0x59) | `0x00005901` | `(89 << 8) \| 1` |
| sm\_90 (Hopper) | 90 (0x5A) | `0x00005A01` | `(90 << 8) \| 1` |
| sm\_100 (Blackwell) | 100 (0x64) | `0x00006401` | `(100 << 8) \| 1` |
| sm\_103 (Blackwell Ultra) | 103 (0x67) | `0x00006701` | `(103 << 8) \| 1` |
| sm\_120 (RTX 50xx) | 120 (0x78) | `0x00007801` | `(120 << 8) \| 1` |
| sm\_121 (DGX Spark) | 121 (0x79) | `0x00007901` | `(121 << 8) \| 1` |

### Executable Cubins (`link_state = 0x04`)

| Architecture | `sm_major` | `e_flags` (hex) | Breakdown |
|---|---|---|---|
| sm\_75 (Turing) | 75 (0x4B) | `0x00004B04` | `(75 << 8) \| 4` |
| sm\_80 (Ampere) | 80 (0x50) | `0x00005004` | `(80 << 8) \| 4` |
| sm\_89 (Ada) | 89 (0x59) | `0x00005904` | `(89 << 8) \| 4` |
| sm\_90 (Hopper) | 90 (0x5A) | `0x00005A04` | `(90 << 8) \| 4` |
| sm\_100 (Blackwell) | 100 (0x64) | `0x00006404` | `(100 << 8) \| 4` |
| sm\_120 (RTX 50xx) | 120 (0x78) | `0x00007804` | `(120 << 8) \| 4` |

### OSABI 0x33 Examples

| Architecture | `sm_major` | `sm_minor` | Relocatable | `e_flags` (hex) |
|---|---|---|---|---|
| sm\_50 (Maxwell) | 50 | 0 | no | `0x00000032` |
| sm\_75 (Turing) | 75 | 0 | yes | `0x8000004B` |

### SM Minor / Variant Handling

The SM minor variant (e.g., the `a` in `sm_90a`) is **not** stored in `e_flags` for OSABI `0x41` device ELFs. It is tracked internally in the `elfw` struct at offset 134 (`*(WORD *)(elfw + 134) = a5`). The `e_ident[EI_ABIVERSION]` field carries an ABI protocol version number (typically 7 or 8), not the SM minor letter.

## Relocatable vs Executable Flag Logic

The constructor has a two-source relocatable detection:

```c
// Relocatable detection in elfw_create (sub_4438F0)
bool is_reloc = a10;                        // explicit flag from caller
if (!is_reloc)
    is_reloc = (a9 & 0x180000) != 0;       // bits 19 or 20 in merge_flags

if (is_reloc) {
    // For OSABI 0x41: seeds e_flags with link_state = 1 (relocatable)
    *(DWORD *)(v17 + 48) = 1;              // e_flags initial = 0x01
    merge_flags |= 0x80000;                // ensure bit 19 is set
    force_rela = true;                      // always use RELA for relocatable output
} else {
    // For OSABI 0x41: seeds e_flags with link_state = 4 (executable)
    *(DWORD *)(v17 + 48) = 4;              // e_flags initial = 0x04
}
// e_type is set separately from the first parameter (a1), NOT here.
```

When bit 19 (`0x80000`) is set in `merge_flags`, the ELF is relocatable. The constructor also forces `force_rela = true` for all relocatable outputs, ensuring `.rela.*` sections (with explicit addends) rather than `.rel.*` sections are used. This simplifies the relocation engine since addends do not need to be read from section data.

## Device vs Host ELF Distinction

The `a9 & 0x8000` test (bit 15) is the master switch between device and host ELF output:

| Flag | OSABI | Section Setup | Architecture State |
|---|---|---|---|
| `a9 & 0x8000` set | `0x41` | `.note.nv.tkinfo`, `.note.nv.cuinfo`, `.nv.uft.entry` | `sub_45AC50` or `sub_459640` |
| `a9 & 0x8000` clear | `0x33` | Standard sections only (no NVIDIA notes) | None (32-bit GPU path) |

Device ELFs receive the NVIDIA-specific note sections for tool kit information and CUDA kernel metadata. The architecture state initializer is called differently depending on whether the output is relocatable (`sub_45AC50`) or executable (`sub_459640`) -- both return a pointer stored at `elfw+488` that provides architecture-specific encoding tables, relocation handlers, and instruction format metadata.

## Cross-References

| Topic | Page |
|---|---|
| Input ELF accessor functions | [ELF Parsing](../input/elf-parsing.md) |
| Cubin validation and loading | [Cubin Loading](../input/cubin-loading.md) |
| Section merge operations | [Section Merging](../linker/section-merging.md) |
| NVIDIA vendor section types | [NVIDIA Section Types](nvidia-sections.md) |
| `.nv.info` metadata format | [.nv.info Metadata](nv-info.md) |
| Constant bank sections | [Constant Banks](constant-banks.md) |
| Unified function tables | [Unified Function Tables](uft.md) |
| Program header layout | [Program Headers](program-headers.md) |
| ELF output serialization | [ELF Serialization](serialization.md) |
| Mercury ELF extensions | [Mercury ELF Sections](../mercury/elf-sections.md) |

## Design Notes

1. **No libelf dependency.** nvlink constructs ELF files by writing raw bytes at computed offsets. The 672-byte `elfw` struct is a custom abstraction that tracks all the state needed to produce a valid ELF, including section ordering, string table construction, and symbol management. There is no `libelf`, `libbfd`, or LLVM `Object` library involved.

2. **Arena-owned memory.** When `a9 & 0x400` is set, the constructor creates a dedicated "elfw memory space" arena. All allocations for this ELF (sections, symbols, strings) come from this arena, enabling bulk deallocation by destroying the arena rather than tracking individual allocations.

3. **Dual encoding schemes.** The OSABI `0x41` vs `0x33` split creates two parallel code paths throughout the constructor. OSABI `0x41` puts the SM major in bits [23:8] of `e_flags` (extracted as `(uint16_t)(e_flags >> 8)` by `sub_4402A0`) with a link-state tag in bits [7:0], and uses device-specific note sections. OSABI `0x33` puts the SM major directly in bits [7:0] and the SM minor in bits [23:16], with a simpler flag layout. Modern CUDA targets always use OSABI `0x41`.

4. **Mercury detection via `e_type`.** Mercury objects are identified by `e_type = 0xFF00` (`ET_LOPROC`), set in the caller (`sub_1406B40`) when the Mercury flag at `a1 + 505` is active. The link-mode bits from `merge_flags` (`a9 & 0x70000`) are stored separately in the `elfw` struct at offset 68, not in `e_flags`. Mercury objects require post-link binary rewriting by the FNLZR (Finalizer).

5. **Eager section creation.** The constructor pre-creates `.shstrtab`, `.strtab`, `.symtab`, and `.symtab_shndx` regardless of whether they will contain data. This simplifies the merge phase, which can unconditionally reference these sections by their fixed internal indices. The `.symtab_shndx` section exists to support more than 65,279 sections (the ELF limit before extended section numbering is required).

6. **Section name preloading.** The constructor iterates a static table of known NVIDIA section names and registers them in a hash table at `elfw+496`. This enables O(1) lookup of section names like `.nv.global`, `.nv.constant0`, `.nv.shared.`, etc. during the merge phase, rather than linear scanning.

## ptxas Wiki Cross-References

The device ELF format described here is the same format ptxas generates as output. For the ptxas-side ELF construction (which uses a parallel 672-byte `ELFW` struct), see the ptxas wiki:

- [Custom ELF Emitter](../../ptxas/output/elf-emitter.html) -- ptxas-side ELFW constructor (`sub_1CB53A0`), section creator, serializer
- [Section Catalog & EIATTR](../../ptxas/output/sections.html) -- section types emitted by ptxas, EIATTR encoding
- [Relocations & Symbols](../../ptxas/output/relocations.html) -- R_CUDA and R_MERCURY relocation type definitions

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `e_machine` = 0xBE (190, EM_CUDA) | DEFINITIVE | Verified in sub_4438F0: `*((_WORD *)v17 + 9) = 190` at offset +18 |
| ELF magic = 0x464C457F | DEFINITIVE | Verified in sub_4438F0: `*(_DWORD *)v17 = 1179403647` = 0x464C457F |
| OSABI 0x41 for device ELF (a9 & 0x8000) | DEFINITIVE | Verified: `*((_BYTE *)v17 + 7) = 65` (0x41) when `a9 & 0x8000` |
| OSABI 0x33 for 32-bit GPU path | DEFINITIVE | Verified: `*((_BYTE *)v17 + 7) = 51` (0x33) in else branch |
| e_flags [7:0] = link_state (NOT sm_minor) | DEFINITIVE | Constructor seeds 1 or 4 into e_flags; reader sub_4402A0 extracts SM via `>> 8`; sub_443260 checks bit 0 for relocatable |
| e_flags [23:8] = sm_major (16-bit field) | DEFINITIVE | sub_4402A0 returns `(uint16_t)(e_flags >> 8)` for OSABI 0x41 |
| OSABI 0x33: e_flags = sm_major \| (sm_minor << 16) \| reloc_bit | DEFINITIVE | sub_4438F0 line 224: `v19 \| (a5 << 16) \| v22` |
| e_type = 0xFF00 for Mercury | DEFINITIVE | sub_1406B40 line 202: `v39 = 65280` when `*(BYTE *)(a1 + 505)` set |
| e_type set from first parameter (a1) | DEFINITIVE | sub_4438F0 line 151: `*(WORD *)(v17 + 16) = v114` where `v114 = (int16)a1` |
| 672-byte elfw struct allocation | DEFINITIVE | Verified in sub_4438F0: `sub_4307C0(v14, 672)` |
| EI_CLASS = (a2 != 0) + 1 | DEFINITIVE | Verified: `*((_BYTE *)v17 + 4) = (a2 != 0) + 1` |
| EI_DATA + EI_VERSION = 0x0101 | DEFINITIVE | Verified: `*(_WORD *)((char *)v17 + 5) = 257` (0x0101) |
| EI_ABIVERSION = a3 (protocol version, not sm_minor) | HIGH | Main passes 7 or 8; sub_1406B40 passes 0, 2, 7, or 8 |
| "elfw memory space" arena string | HIGH | String at 0x1D39FA3 confirmed in nvlink_strings.json |
| "couldn't initialize arch state" error | HIGH | String at 0x1D39FE8 confirmed in nvlink_strings.json |
| Flag decomposition bits 0-19 | HIGH | Verified bit-by-bit in sub_4438F0 |
| Relocatable detection: a10 \|\| (a9 & 0x180000) | HIGH | Verified in sub_4438F0 |
| link_state = 0x04 means executable | HIGH | sub_4438F0 line 163; no contradicting reader found |
| Section type bitmask 0x400D validation | MEDIUM | Verified in sub_43DD30, bitmask matches but inner logic complex |
| tkinfo alignment 0x2000000, cuinfo 0x1000000 | HIGH | Verified: `sub_441AC0(... ".note.nv.tkinfo", 7, 0x2000000, ...)` |
| Architecture hex examples (sm_75 etc) | HIGH | Derived from verified `(a4 << 8) \| link_state` formula |
| .symtab_shndx (SHT_SYMTAB_SHNDX = 18) | HIGH | String ".symtab_shndx" exists in constructor xrefs |
| SM minor not in e_flags for OSABI 0x41 | HIGH | sm_minor (a5) only stored at elfw offset 134 in device path; not ORed into e_flags |
