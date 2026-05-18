# Relocations & Symbols

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas defines two parallel relocation type systems for CUBIN ELF files: **R_CUDA_\*** (117 types, ordinals 0--116) for SASS-encoded cubins targeting SM 30--90a, and **R_MERCURY_\*** (65 types, ordinals 0--64) for Mercury-encoded cubins targeting SM 100+ (Blackwell and later). Both systems use standard `Elf64_Rela` relocation entries in `.rela.text.<funcname>` sections, with a custom resolution algorithm that handles alias redirection, dead function filtering, UFT/UDT pseudo-relocations, PC-relative branch validation, and sub-byte instruction patching. The symbol table (`.symtab`) follows standard ELF `Elf64_Sym` format with CUDA-specific symbol types and an extended section index mechanism (`.symtab_shndx`) for programs exceeding 65,280 sections.

| | |
|---|---|
| **Relocation resolver** | `sub_1CD48C0` (4,184 bytes binary, 22 KB decompiled, 17 callees) |
| **Relocation writer** | `sub_1CD5920` (1,985 bytes binary, 11 KB decompiled) |
| **Relocation creator (SASS)** | `sub_1CD4510` (860 bytes binary) |
| **Relocation creator (Mercury)** | `sub_1CD46B0` (540 bytes binary) |
| **Relocation pre-scan** | `sub_1CD43A0` (560 bytes binary) |
| **Bit-field patcher** | `sub_1CD34E0` (3,700 bytes binary, sub_1CD33F0/sub_1CD3330 helpers) |
| **Symbol table builder** | `sub_1CB68D0` (9,578 bytes binary, 49 KB decompiled, 36 callees) |
| **Symbol fixup** | `sub_1CB2CA0` (2,038 bytes binary, 4 call sites) |
| **Section index remap** | `sub_1C99BB0` (4,900 bytes binary) |
| **UFT manager** | `sub_1CD22E0` (1,979 bytes binary, 10 KB decompiled) |
| **UFT slot validator** | `sub_1CD2AA0` (~800 bytes binary) |
| **Bindless handler** | `sub_1CAB300` (2,157 bytes binary, 12 KB decompiled) |
| **R_CUDA table address** | `off_2408B60` (117 entries x 64 bytes) |
| **R_MERCURY table address** | `off_2407B60` (65 entries x 64 bytes) |

## Relocation Type Systems

### Table Selection Logic

The ELFW object stores the ELF class byte at offset 7 and a flags word at offset 48. The relocation subsystem selects between the two tables based on the `IsPIC` flag combined with the ELF class:

```c
// Table selection (reconstructed from sub_1CD48C0, sub_1CD4510, sub_1CD5920)
uint32_t test_bit = (elfw->ei_class == 'A') ? 1 : 0x80000000;
bool is_mercury = (test_bit & elfw->flags) != 0;

if (is_mercury) {
    // SM 100+ Mercury encoding: off_2407B60
    // Type codes start at 0x10000; subtract to index the table
    table = &R_MERCURY_table;           // off_2407B60
    index = raw_type - 0x10000;         // range check: index <= 0x3F (63)
} else {
    // SM 30-90a SASS encoding: off_2408B60
    table = &R_CUDA_table;              // off_2408B60
    index = raw_type;                   // range check: index <= 0x73 (115)
}
```

Mercury relocation type codes are stored with a `0x10000` offset in the internal relocation entry's type field. This lets a single code path handle both systems -- the table selection just subtracts the offset for Mercury types.

### Relocation Descriptor Table Format

Each entry in the relocation type descriptor table is 64 bytes (8 qwords). The layout is accessed through pointer arithmetic patterns like `table[8 * index + N]` where the table pointer type is `char**` (8-byte stride):

```c
// Relocation type descriptor -- 64 bytes per entry (reconstructed)
struct reloc_type_desc {
    const char* name;       // +0:  R_CUDA_* or R_MERCURY_* name string
    uint32_t    unknown_04; // +8:  unknown field
    uint32_t    unknown_08; // +12: unknown field
    uint32_t    bit_start;  // +16: starting bit position in instruction
    uint32_t    bit_width;  // +20: field width in bits
    uint32_t    patch_mode; // +24: patching mode (0=none, 1=direct, 6/7=split)
    uint32_t    flags_hi;   // +28: high flags (value 12-15 triggers callgraph)
    // ... remaining 32 bytes: additional patching parameters
};
```

The `patch_mode` field at offset +24 drives the bit-field patching logic in `sub_1CD34E0`. The switch statement handles these modes:

| Mode | Description | Types |
|---|---|---|
| 0 | No-op (sentinel/terminator) | R_CUDA_NONE, R_CUDA_NONE_LAST |
| 1, 0x12, 0x2E | Direct bit-field write (full or partial 64-bit word) | Most absolute/PC-relative types |
| 6, 0x37 | Split low-word patching (handles cross-qword boundaries) | LO types, sub-byte 8_N types |
| 7, 0x38 | Split high-word patching (uses HIDWORD of value) | HI types |

When `flags_hi` (at descriptor offset +28) is in the range 12--15, the relocation creator calls `sub_1CBD0D0` to register the relocation's target section in the call graph. This triggers call graph edge creation for function descriptors and branch targets.

## R_CUDA_\* Relocation Types

117 types from `R_CUDA_NONE` (ordinal 0) to `R_CUDA_NONE_LAST` (ordinal 116). String addresses span `0x23FBE0E`--`0x23FC6B6` in the ptxas binary, confirming these are contiguous in the read-only data section. Ordinals are assigned by string table order.

### Absolute Address Relocations

| Ordinal | Name | Bit Field | Purpose |
|---|---|---|---|
| 0 | `R_CUDA_NONE` | -- | Sentinel / no relocation |
| 1 | `R_CUDA_32` | 32-bit | Absolute 32-bit address |
| 2 | `R_CUDA_64` | 64-bit | Absolute 64-bit address |
| 5 | `R_CUDA_ABS32_26` | 32-bit at bit 26 | Absolute address, 26-bit encoding |
| 10 | `R_CUDA_ABS32_LO_26` | low 32 at bit 26 | Low half of 64-bit address |
| 11 | `R_CUDA_ABS32_HI_26` | high 32 at bit 26 | High half of 64-bit address |
| 12 | `R_CUDA_ABS32_23` | 32-bit at bit 23 | Absolute address, 23-bit encoding |
| 13 | `R_CUDA_ABS32_LO_23` | low 32 at bit 23 | Low half, 23-bit encoding |
| 14 | `R_CUDA_ABS32_HI_23` | high 32 at bit 23 | High half, 23-bit encoding |
| 15 | `R_CUDA_ABS24_26` | 24-bit at bit 26 | 24-bit absolute address |
| 16 | `R_CUDA_ABS24_23` | 24-bit at bit 23 | 24-bit absolute, 23-bit encoding |
| 17 | `R_CUDA_ABS16_26` | 16-bit at bit 26 | 16-bit absolute address |
| 18 | `R_CUDA_ABS16_23` | 16-bit at bit 23 | 16-bit absolute, 23-bit encoding |
| 42 | `R_CUDA_ABS32_20` | 32-bit at bit 20 | Volta+ encoding format |
| 43 | `R_CUDA_ABS32_LO_20` | low 32 at bit 20 | Low half, 20-bit encoding |
| 44 | `R_CUDA_ABS32_HI_20` | high 32 at bit 20 | High half, 20-bit encoding |
| 45 | `R_CUDA_ABS24_20` | 24-bit at bit 20 | 24-bit, 20-bit encoding |
| 46 | `R_CUDA_ABS16_20` | 16-bit at bit 20 | 16-bit, 20-bit encoding |
| 55 | `R_CUDA_ABS32_32` | 32-bit at bit 32 | Ampere+ encoding format |
| 56 | `R_CUDA_ABS32_LO_32` | low 32 at bit 32 | Low half, 32-bit position |
| 57 | `R_CUDA_ABS32_HI_32` | high 32 at bit 32 | High half, 32-bit position |
| 58 | `R_CUDA_ABS47_34` | 47-bit at bit 34 | 47-bit wide field |
| 59 | `R_CUDA_ABS16_32` | 16-bit at bit 32 | 16-bit, 32-bit position |
| 60 | `R_CUDA_ABS24_32` | 24-bit at bit 32 | 24-bit, 32-bit position |
| 74 | `R_CUDA_ABS24_40` | 24-bit at bit 40 | 24-bit at offset 40 |
| 75 | `R_CUDA_ABS55_16_34` | 55-bit, 16+34 split | Split wide field |
| 100 | `R_CUDA_ABS20_44` | 20-bit at bit 44 | 20-bit at offset 44 |
| 114 | `R_CUDA_ABS56_16_34` | 56-bit, 16+34 split | Split wide field |
| 70 | `R_CUDA_32_LO` | low 32 | Low half of 64-bit |
| 71 | `R_CUDA_32_HI` | high 32 | High half of 64-bit |

The naming convention encodes the bit-field geometry: `R_CUDA_ABS<width>_<start_bit>` indicates that `<width>` bits of the resolved address are patched into the instruction at bit position `<start_bit>`. The `LO`/`HI` suffix indicates low or high 32 bits of a 64-bit value. The different start positions (20, 23, 26, 32, 34, 40, 44) correspond to different SASS instruction encoding formats across SM generations: Kepler (26), Maxwell/Pascal (23), Volta/Turing (20), Ampere/Ada/Hopper (32).

### Global Address Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 3 | `R_CUDA_G32` | Global-space 32-bit address |
| 4 | `R_CUDA_G64` | Global-space 64-bit address |
| 84 | `R_CUDA_G8_0` | Global-space byte 0 of 64-bit instruction |
| 85 | `R_CUDA_G8_8` | Global-space byte 1 |
| 86 | `R_CUDA_G8_16` | Global-space byte 2 |
| 87 | `R_CUDA_G8_24` | Global-space byte 3 |
| 88 | `R_CUDA_G8_32` | Global-space byte 4 |
| 89 | `R_CUDA_G8_40` | Global-space byte 5 |
| 90 | `R_CUDA_G8_48` | Global-space byte 6 |
| 91 | `R_CUDA_G8_56` | Global-space byte 7 |

Global address relocations target `.nv.global` and `.nv.global.init` sections. The `G8_*` sub-byte variants patch individual bytes within a 64-bit instruction word, used when the instruction encoding requires the address to be spread across non-contiguous bit fields.

### PC-Relative Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 40 | `R_CUDA_PCREL_IMM24_26` | PC-relative 24-bit immediate at bit 26 |
| 41 | `R_CUDA_PCREL_IMM24_23` | PC-relative 24-bit immediate at bit 23 |

PC-relative relocations resolve branch and call targets. The resolver enforces a critical constraint:

```
"PC relative branch address should be in the same section"
```

This means intra-function branches use PC-relative relocations, but cross-function calls use absolute or function descriptor relocations. The 24-bit immediate provides a +/-8 MB range from the instruction address, sufficient for any single kernel.

### Constant Field Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 24 | `R_CUDA_CONST_FIELD19_28` | 19-bit constant bank offset at bit 28 |
| 25 | `R_CUDA_CONST_FIELD19_23` | 19-bit constant bank offset at bit 23 |
| 36 | `R_CUDA_CONST_FIELD21_26` | 21-bit constant bank offset at bit 26 |
| 38 | `R_CUDA_CONST_FIELD19_26` | 19-bit constant bank offset at bit 26 |
| 39 | `R_CUDA_CONST_FIELD21_23` | 21-bit constant bank offset at bit 23 |
| 50 | `R_CUDA_CONST_FIELD19_20` | 19-bit constant bank offset at bit 20 |
| 54 | `R_CUDA_CONST_FIELD21_20` | 21-bit constant bank offset at bit 20 |
| 64 | `R_CUDA_CONST_FIELD19_40` | 19-bit constant bank offset at bit 40 |
| 66 | `R_CUDA_CONST_FIELD21_38` | 21-bit constant bank offset at bit 38 |
| 115 | `R_CUDA_CONST_FIELD22_37` | 22-bit constant bank offset at bit 37 |

Constant field relocations patch `.nv.constant0.<func>` bank offsets into load constant (`LDC`) instructions. The field width (19, 21, or 22 bits) determines the maximum addressable constant bank size: 19-bit supports 512 KB, 21-bit supports 2 MB, 22-bit supports 4 MB. During resolution, the constant bank deduplication pass (`sub_1CA6890`) may adjust the relocation offset:

```
"optimize ocg constant reloc offset from %lld to %lld"
```

### Function Descriptor Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 31 | `R_CUDA_FUNC_DESC32_23` | 32-bit function descriptor at bit 23 |
| 32 | `R_CUDA_FUNC_DESC32_LO_23` | Low 32 of descriptor at bit 23 |
| 33 | `R_CUDA_FUNC_DESC32_HI_23` | High 32 of descriptor at bit 23 |
| 34 | `R_CUDA_FUNC_DESC_32` | Full 32-bit function descriptor |
| 35 | `R_CUDA_FUNC_DESC_64` | Full 64-bit function descriptor |
| 47 | `R_CUDA_FUNC_DESC32_20` | 32-bit function descriptor at bit 20 |
| 48 | `R_CUDA_FUNC_DESC32_LO_20` | Low 32 of descriptor at bit 20 |
| 49 | `R_CUDA_FUNC_DESC32_HI_20` | High 32 of descriptor at bit 20 |
| 61 | `R_CUDA_FUNC_DESC32_32` | 32-bit function descriptor at bit 32 |
| 62 | `R_CUDA_FUNC_DESC32_LO_32` | Low 32 of descriptor at bit 32 |
| 63 | `R_CUDA_FUNC_DESC32_HI_32` | High 32 of descriptor at bit 32 |
| 92--99 | `R_CUDA_FUNC_DESC_8_0` -- `R_CUDA_FUNC_DESC_8_56` | Sub-byte function descriptor patches |

Function descriptors are used for indirect calls through function pointers. The descriptor contains the target function's entry point address and is loaded by the GPU's indirect call mechanism. The sub-byte `FUNC_DESC_8_*` variants patch individual bytes of the descriptor into instruction encoding slots, used in wide instruction formats where the descriptor address is spread across multiple fields. When the relocation creator detects a `flags_hi` value of 12--15 in the descriptor table entry, it calls `sub_1CBD0D0` to register the call edge in the call graph.

### Texture, Sampler, and Surface Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 6 | `R_CUDA_TEX_HEADER_INDEX` | Texture header table index |
| 7 | `R_CUDA_SAMP_HEADER_INDEX` | Sampler header table index |
| 8 | `R_CUDA_SURF_HW_DESC` | Surface hardware descriptor |
| 9 | `R_CUDA_SURF_HW_SW_DESC` | Surface hardware+software descriptor |
| 19 | `R_CUDA_TEX_SLOT` | Texture binding slot |
| 20 | `R_CUDA_SAMP_SLOT` | Sampler binding slot |
| 21 | `R_CUDA_SURF_SLOT` | Surface binding slot |
| 26 | `R_CUDA_TEX_SLOT9_49` | 9-bit texture slot at bit 49 |
| 52 | `R_CUDA_SURF_HEADER_INDEX` | Surface header table index |
| 101 | `R_CUDA_SAMP_HEADER_INDEX_0` | Sampler header index variant |

These relocations connect texture/sampler/surface operations to their runtime-allocated descriptor table entries. The CUDA driver fills in the actual descriptor indices at launch time based on the kernel's resource binding.

### Bindless Texture/Surface Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 22 | `R_CUDA_TEX_BINDLESSOFF13_32` | Bindless texture offset, 13-bit at bit 32 |
| 23 | `R_CUDA_TEX_BINDLESSOFF13_47` | Bindless texture offset, 13-bit at bit 47 |
| 29 | `R_CUDA_TEX_BINDLESSOFF13_41` | Bindless texture offset, 13-bit at bit 41 |
| 30 | `R_CUDA_TEX_BINDLESSOFF13_45` | Bindless texture offset, 13-bit at bit 45 |
| 51 | `R_CUDA_BINDLESSOFF13_36` | Bindless offset, 13-bit at bit 36 |
| 65 | `R_CUDA_BINDLESSOFF14_40` | Bindless offset, 14-bit at bit 40 |

Bindless texture/surface relocations are handled by `sub_1CAB300`, which creates `$NVLINKBINDLESSOFF_<name>` symbols for each bindless reference. During resolution:

```
"change reloc symbol from %d to %d"
"no bindless ref in section %s"
"unexpected usage of non-unified surface descriptors"
```

### Sub-Byte Patch Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 76--83 | `R_CUDA_8_0` -- `R_CUDA_8_56` | Patch byte 0--7 of 64-bit instruction |

These relocations patch a single byte at a specific 8-bit-aligned position within a 64-bit instruction word. They are used when the resolved value must be inserted into a non-standard bit position that does not align with the instruction encoding's immediate field boundaries.

### Miscellaneous Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 27 | `R_CUDA_6_31` | 6-bit field at bit 31 |
| 28 | `R_CUDA_2_47` | 2-bit field at bit 47 |
| 37 | `R_CUDA_QUERY_DESC21_37` | Query descriptor, 21-bit at bit 37 |
| 53 | `R_CUDA_INSTRUCTION64` | Whole 64-bit instruction replacement |
| 67 | `R_CUDA_INSTRUCTION128` | Whole 128-bit instruction replacement |
| 68 | `R_CUDA_YIELD_OPCODE9_0` | YIELD opcode, 9-bit at bit 0 |
| 69 | `R_CUDA_YIELD_CLEAR_PRED4_87` | Clear YIELD predicate, 4-bit at bit 87 |
| 72 | `R_CUDA_UNUSED_CLEAR32` | Zero out 32-bit unused field |
| 73 | `R_CUDA_UNUSED_CLEAR64` | Zero out 64-bit unused field |
| 116 | `R_CUDA_NONE_LAST` | Sentinel marking end of relocation table |

The `R_CUDA_INSTRUCTION64` and `R_CUDA_INSTRUCTION128` types replace entire instruction words, used for instruction-level patching by the linker when the instruction encoding changes based on the final resolved address.

The `R_CUDA_YIELD_*` types handle YIELD-to-NOP conversion. When a kernel has forward-progress requirements that prevent yielding, the resolver converts YIELD instructions to NOPs:

```
"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement."
```

The `R_CUDA_UNUSED_CLEAR*` types zero out instruction fields that are unused in the final encoding, ensuring deterministic output.

### Unified Address Space Relocations

| Ordinal | Name | Purpose |
|---|---|---|
| 102 | `R_CUDA_UNIFIED` | Unified address (generic pointer) |
| 103 | `R_CUDA_UNIFIED_32` | 32-bit unified address |
| 104--111 | `R_CUDA_UNIFIED_8_0` -- `R_CUDA_UNIFIED_8_56` | Unified address sub-byte patches |
| 112 | `R_CUDA_UNIFIED32_LO_32` | Low 32 of unified at bit 32 |
| 113 | `R_CUDA_UNIFIED32_HI_32` | High 32 of unified at bit 32 |

Unified address relocations resolve generic pointers that can point to global, shared, or constant memory. During final resolution, the resolver performs a type conversion from unified (type 103) to absolute (type 1):

```c
// In sub_1CD48C0: unified reloc replacement
if (reloc_type == 103)  // R_CUDA_UNIFIED_32
    reloc_type = 1;     // R_CUDA_32
```

## R_MERCURY_\* Relocation Types

65 types from `R_MERCURY_NONE` (ordinal 0) to `R_MERCURY_NONE_LAST` (ordinal 64). String addresses span `0x23FB8C5`--`0x23FBDFA`. Mercury relocations serve the same purpose as R_CUDA types but are designed for the Mercury intermediate representation used on SM 100+ targets.

### Mercury Type Categories

| Category | Types | Purpose |
|---|---|---|
| Address | `R_MERCURY_G64`, `R_MERCURY_ABS64`, `R_MERCURY_ABS32`, `R_MERCURY_ABS16` | Memory addresses |
| Split address | `R_MERCURY_ABS32_LO`, `R_MERCURY_ABS32_HI` | 64-bit address halves |
| Program-relative | `R_MERCURY_PROG_REL64`, `R_MERCURY_PROG_REL32`, `R_MERCURY_PROG_REL32_LO`, `R_MERCURY_PROG_REL32_HI` | Offsets from program base |
| Tex/samp/surf | `R_MERCURY_TEX_HEADER_INDEX`, `R_MERCURY_SAMP_HEADER_INDEX`, `R_MERCURY_SURF_HEADER_INDEX` | Resource descriptors |
| Function | `R_MERCURY_FUNC_DESC_64` | Function descriptor |
| Sub-byte | `R_MERCURY_8_0` -- `R_MERCURY_8_56` (8 types) | Byte-level patches |
| Global sub-byte | `R_MERCURY_G8_0` -- `R_MERCURY_G8_56` (8 types) | Global-space byte patches |
| Func desc sub-byte | `R_MERCURY_FUNC_DESC_8_0` -- `R_MERCURY_FUNC_DESC_8_56` (8 types) | Function descriptor byte patches |
| Abs-program-relative | `R_MERCURY_ABS_PROG_REL32_LO`, `R_MERCURY_ABS_PROG_REL32_HI`, `R_MERCURY_ABS_PROG_REL32`, `R_MERCURY_ABS_PROG_REL64` | Absolute program-relative |
| Program-relative sub-byte | `R_MERCURY_PROG_REL8_0` -- `R_MERCURY_PROG_REL8_56` (8 types) | Program-relative byte patches |
| Unified | `R_MERCURY_UNIFIED`, `R_MERCURY_UNIFIED_32`, `R_MERCURY_UNIFIED_8_0` -- `R_MERCURY_UNIFIED_8_56`, `R_MERCURY_UNIFIED32_LO`, `R_MERCURY_UNIFIED32_HI` | Unified address space |
| Cleanup | `R_MERCURY_UNUSED_CLEAR64` | Zero out unused fields |
| Sentinels | `R_MERCURY_NONE`, `R_MERCURY_NONE_LAST` | Table boundaries |

Mercury introduces **program-relative** relocations (`PROG_REL*`) that do not exist in the R_CUDA set. These compute offsets relative to the program base address rather than absolute virtual addresses, enabling position-independent code for the Mercury deferred finalization model. The Mercury finalizer (running at link or load time) resolves these program-relative relocations after the final code layout is known.

## Relocation Encoding

### ELF Relocation Entry Format

Cubin relocations use standard `Elf64_Rela` entries in `.rela.text.<funcname>` sections:

```c
typedef struct {
    Elf64_Addr  r_offset;   // Byte offset within the section
    Elf64_Xword r_info;     // Symbol index (high 32) | Type (low 32)
    Elf64_Sxword r_addend;  // Addend for the relocation computation
} Elf64_Rela;  // 24 bytes
```

The `r_info` field packs the symbol table index in the upper 32 bits and the R_CUDA/R_MERCURY type code in the lower 32 bits:

```c
#define ELF64_R_SYM(info)   ((info) >> 32)
#define ELF64_R_TYPE(info)  ((info) & 0xFFFFFFFF)
```

For Mercury types, the type code stored in `r_info` is the ordinal plus `0x10000`. The resolver subtracts `0x10000` before indexing the R_MERCURY descriptor table.

### Internal Relocation Entry

The ELFW object maintains relocations in an internal linked list at offset +376 of the ELFW structure. Each internal entry is a 32-byte node:

```c
// Internal relocation entry (reconstructed from sub_1CD4510, sub_1CD46B0)
struct elfw_reloc {
    uint64_t offset;            // +0:  byte offset in target section
    uint64_t type_and_section;  // +8:  (target_section << 32) | reloc_type
    uint64_t addend;            // +16: relocation addend
    uint32_t symbol_index;      // +24: index into ELFW symbol table
    uint32_t alias_index;       // +28: original symbol if aliased, else 0
};
```

The `type_and_section` field encodes both the relocation type code (low 32 bits) and the target section index (high 32 bits) in a single 64-bit field.

### Resolved Relocation Output

Resolved relocations are written by `sub_1CD5920` to `.nv.resolvedrela` sections. Additionally, `.nv.rel.action` sections carry relocation action metadata for the CUDA driver's runtime linker.

## Symbol Table Structure

### .symtab Format

The symbol table uses standard `Elf64_Sym` entries (24 bytes each for 64-bit, 16 bytes for 32-bit):

```c
typedef struct {
    Elf32_Word    st_name;    // String table offset
    unsigned char st_info;    // Type (low 4 bits) | Binding (high 4 bits)
    unsigned char st_other;   // Visibility (low 2 bits) | Flags
    Elf16_Half    st_shndx;   // Section index (or SHN_XINDEX=0xFFFF)
    Elf64_Addr    st_value;   // Symbol value (section offset)
    Elf64_Xword   st_size;    // Symbol size
} Elf64_Sym;
```

### Internal Symbol Representation

The ELFW maintains an internal symbol structure (40+ bytes) with additional metadata:

| Offset | Size | Field | Description |
|---|---|---|---|
| +4 | 1 | `st_info` | Low nibble = type (STT_*), high nibble = binding strength |
| +5 | 1 | `st_other` | Bits 0-1 = visibility, bits 4-7 = CUDA-specific flags |
| +6 | 2 | `st_shndx` | Section index (0xFFFF = use extended index) |
| +8 | 8 | `st_value` | Symbol address; -1 = unallocated |
| +24 | 4 | section_link | Internal section reference |
| +28 | 4 | extra_index | Secondary symbol link |
| +32 | 8 | `name_ptr` | Pointer to symbol name string |

### Symbol Types

| ELF Type | Value | CUDA Usage |
|---|---|---|
| `STT_NOTYPE` | 0 | Undefined/external symbols |
| `STT_OBJECT` | 1 | Global/constant/shared variables |
| `STT_FUNC` | 2 | Kernel entry points, device functions |
| `STT_SECTION` | 3 | Section symbols (one per section) |
| `STT_COMMON` | 5 | Common symbols (`.common symbol`) |
| `STT_CUDA_TEXTURE` | 10 | Texture reference symbols |
| `STT_CUDA_SURFACE` | 11 | Surface reference symbols |
| `STT_CUDA_SAMPLER` | 12 | Sampler reference symbols |
| `STT_CUDA_FUNC_DESC` | 13 | Function descriptor (indirect call target) |

The internal type field at offset +4 uses the low nibble for ELF standard types and the high nibble for binding/scope information. The resolver checks `st_info & 0xF` throughout its processing.

Function descriptor symbols (type 13) receive special handling in the relocation resolver. When the resolver encounters a type-13 symbol, it checks whether the symbol is allocated:

```c
// sub_1CD48C0: function descriptor symbol handling
if ((sym->st_info & 0xF) == 13) {  // STT_CUDA_FUNC_DESC
    shndx = get_section_index(elfw, sym);
    if (shndx == 0) {
        // Unresolved -- check binding and ELFW flags
        if ((sym->st_other & 0xE0) == 0x20  // STB_GLOBAL
            || (sym->st_other & 0x10))       // CUDA-specific extern flag
        {
            // External function descriptor: keep relocation for linker
        }
    }
}
```

### Symbol Binding and Visibility

The `st_other` byte encodes both ELF visibility (bits 0-1) and CUDA-specific binding flags (bits 4-7):

| Bits | Field | Values |
|---|---|---|
| 0-1 | ELF visibility | 0 = `STV_DEFAULT`, 1 = `STV_INTERNAL`, 2 = `STV_HIDDEN`, 3 = `STV_PROTECTED` |
| 4 | Extern flag | 1 = external linkage (for nvlink) |
| 5-6 | Binding strength | 0x20 = `STB_GLOBAL`, 0x80 = `STB_WEAK` |
| 7 | Reserved | Used internally during resolution |

The binding byte at `st_other & 3` (low 2 bits of the high nibble) maps to:

| Value | Meaning | Resolution |
|---|---|---|
| 1 | `STB_LOCAL` / dead | Skip relocation (`"ignore reloc on dead func %s"`) |
| 2 | `STB_GLOBAL` | Normal resolution |
| 3 | `STB_WEAK` | Resolve if available, otherwise use default |

### Symbol Table Builder -- `sub_1CB68D0`

The symbol table builder (9,578 bytes, approximately 1,700 decompiled lines) processes the ELFW internal symbol list in these steps:

1. **Iterate symbols** -- walks the symbol list from the ELFW object
2. **Filter deleted symbols** -- 12 separate checks for `"reference to deleted symbol"` guard against stale entries from dead code elimination
3. **Handle `__cuda_syscall`** -- special-cases the device-side syscall dispatcher symbol
4. **Resolve aliases** -- follows alias chains to find the canonical symbol
5. **Compute values** -- resolves `st_value` from section base + offset
6. **Create section symbols** -- ensures every section has an `STT_SECTION` symbol; emits `"found multiple section symbols for %s"` if duplicates exist
7. **Handle SHN_XINDEX overflow** -- when section index >= `SHN_LORESERVE` (0xFF00 = 65,280), sets `st_shndx = SHN_XINDEX` (0xFFFF) and stores the real index in `.symtab_shndx`
8. **Build .symtab_shndx** -- populates the extended index table for overflow sections

Error strings observed in the builder:

| String | Condition |
|---|---|
| `"reference to deleted symbol"` | Symbol marked deleted but still referenced (12 checks) |
| `"ignore symbol %s in unused section"` | Symbol in eliminated section |
| `"ignore symbol string %s for sym %d"` | Skipping symbol name for unnamed/internal symbol |
| `"found multiple section symbols for %s"` | Duplicate `STT_SECTION` entries |
| `"symbol already assigned"` | Duplicate assignment attempt |
| `"adding global symbols of same name"` | Name collision |
| `"alias to unknown symbol"` | Alias target not found |
| `"unallocated symbol"` | Symbol value is -1 (never assigned an address) |
| `"missing sec strtab"` | String table not initialized |

### Symbol Fixup -- `sub_1CB2CA0`

After dead code elimination removes sections, symbol indices become stale. The fixup pass (2,038 bytes, called from 4 sites) renumbers all symbol `st_shndx` values:

1. For each section in the ELFW:
   - If the section lacks an `STT_SECTION` symbol, create one
   - If the section has multiple `STT_SECTION` symbols, warn
2. Walk the symbol table and remap `st_shndx` values through the section index mapping

The fixup runs at multiple pipeline points: after dead function elimination, after Mercury section cloning, and after any section deletion.

### Section Index Remap -- `sub_1C99BB0`

The companion to `sub_1CB2CA0` for the extended index mechanism. When section indices change, this function updates both `.symtab_shndx` and `.nv.merc.symtab_shndx` to keep the extended index tables consistent.

## Relocation Resolution Algorithm

The master resolver `sub_1CD48C0` implements a 7-step algorithm that processes every relocation entry in the ELFW's linked list:

### Step 1: Symbol Address Computation

For each relocation entry, compute the symbol's resolved address by adding the symbol's `st_value` (from the section base) to the relocation offset:

```c
if (reloc->alias_index) {
    sym = lookup_symbol(elfw, reloc->alias_index);
    reloc->offset += sym->st_value;
}
```

For Mercury cubins (64-bit ELF class 'A' with Mercury flag set), the resolver applies an additional address transformation that accounts for the Mercury instruction stride:

```c
if (is_mercury && sym_value != 0) {
    int stride = 2 * arch_vtable->get_merc_stride();
    reloc->offset += stride * (sym_value >> 7);
}
```

### Step 2: Alias Resolution

If the relocation targets an alias symbol (ELF type `STT_NOTYPE` with section index pointing to another symbol), redirect the relocation to the canonical target:

```
"change alias reloc %s to %s"
```

The resolver follows the alias chain through `sub_1CB1E00` (get section index) and `sub_1CB3D20` (get section by index), replacing the alias with its real target.

### Step 3: Dead Function Filtering

If the relocation's target symbol has local binding (`st_other & 3 == 1`) and is in a deleted section, the relocation is zeroed out:

```
"ignore reloc on dead func %s"
```

The relocation's type is set to 0 (R_CUDA_NONE), effectively removing it. For the output mode `!= 2` (relocatable), dead relocations on `STT_NOTYPE` symbols with a binding prefix of 2 are also removed.

### Step 4: UFT/UDT Pseudo-Relocation Handling

Relocations targeting special synthetic symbols are intercepted:

| Symbol | Action |
|---|---|
| `__UFT_OFFSET` | Record for UFT slot assignment, zero the relocation |
| `__UFT_CANONICAL` | Map to canonical UFT entry |
| `__UDT_OFFSET` | Record for UDT slot assignment |
| `__UDT_CANONICAL` | Map to canonical UDT entry |
| `__UFT`, `__UFT_END` | UFT boundary markers |
| `__UDT`, `__UDT_END` | UDT boundary markers |

The resolver checks if a symbol name starts with `"__UFT_OFFSET"` (exact 13-character comparison in the decompiled code). If matched:

```
"ignore reloc on UFT_OFFSET"
```

The relocation entry is then processed by the UFT manager (`sub_1CD22E0`) which maps UUIDs to UFT slot indices.

### Step 5: PC-Relative Branch Validation

For relocations whose descriptor table entry has `*(table + 8*index + 5) == 16` (indicating a PC-relative type), the resolver validates that the source and target sections are identical:

```c
if (reloc_desc->patch_mode == 16 && reloc->section != target_section)
    fatal("PC relative branch address should be in the same section");
```

### Step 6: YIELD-to-NOP Conversion

If the relocation type is `R_CUDA_YIELD_OPCODE9_0` or `R_CUDA_YIELD_CLEAR_PRED4_87`, and the kernel has forward-progress requirements, the resolver skips the NOP conversion:

```
"Ignoring the reloc to convert YIELD to NOP due to forward progress requirement."
```

### Step 7: Bit-Field Patching

The final step delegates to `sub_1CD34E0`, the bit-field patcher. This function uses the relocation descriptor table entry's parameters to extract the current field value (via `sub_1CD33F0`), add the resolved address, and write the result back (via `sub_1CD3330`):

```c
// sub_1CD34E0 -- bit-field patching (simplified)
bool apply_reloc(reloc_desc_table, index, is_addend, instruction_data,
                 symbol_value, reloc_offset, sym_addr, sym_shndx,
                 section_type_offset, old_value_out) {
    entry = &reloc_desc_table[index * 64 + 12]; // Start at byte 12
    end = &reloc_desc_table[index * 64 + 60];   // 4 operations max

    while (entry < end) {
        uint32_t bit_start = entry[0];
        uint32_t bit_width = entry[1];
        uint32_t mode      = entry[2];

        switch (mode) {
        case 0:  // NOP
            break;
        case 1:  // Direct write: place value at [bit_start, bit_start+bit_width)
        case 0x12: case 0x2E:
            old = extract_bits(instruction_data, bit_start, bit_width);
            insert_bits(instruction_data, resolved_value, bit_start, bit_width);
            break;
        case 6:  // Split low-word write (cross-qword boundary handling)
        case 0x37:
            // Write low portion, advance to next qword if needed
            break;
        case 7:  // Split high-word write
        case 0x38:
            // Write HIDWORD of value
            break;
        }
        entry += 4;  // Next 16-byte operation
    }
    return true;
}
```

If the NVRS (NVIDIA Register Spill) check fails during patching, the resolver emits:

```
"unexpected NVRS"
```

NVRS relocations are special-purpose relocations for register spill slot references. When the bit-field patcher returns false, the relocation is invalid for the current context.

### Post-Resolution

Successfully resolved relocations are either:
- **Removed** from the linked list (the relocation was fully applied to the instruction bytes)
- **Kept** for the output `.nv.resolvedrela` section (the relocation needs runtime resolution by the CUDA driver)

The relocation writer `sub_1CD5920` validates every remaining relocation before serializing it:

| Check | Error |
|---|---|
| Symbol value == -1 | `"symbol never allocated"` |
| Offset >= section size | `"relocation is past end of offset"` |
| Target section unallocated | `"rela section never allocated"` |
| Address not found in section data | `"reloc address not found"` |

## Unified Function Table (UFT) and Unified Data Table (UDT)

### Purpose

UFT and UDT support indirect function calls and generic data references across compilation units. When `nvcc` compiles a program using function pointers, virtual functions, or `__device__` function addresses taken in host code, the compiler generates UFT/UDT entries that the runtime linker resolves at load time.

### Sections

| Section | Purpose |
|---|---|
| `.nv.uft` | Jump slot table (one slot per indirect-callable function) |
| `.nv.uft.entry` | UFT entry metadata (UUID, offset pairs) |
| `.nv.udt` | Data slot table (one slot per externally-referenced data object) |
| `.nv.udt.entry` | UDT entry metadata |
| `.nv.uft.rel` | UFT relocation table |

### UFT Entry Structure

Each UFT entry contains a 128-bit UUID and a 64-bit offset:

```c
struct uft_entry {
    uint64_t uuid_lo;   // Low 64 bits of UUID
    uint64_t uuid_hi;   // High 64 bits of UUID
    uint64_t offset;     // Offset into the jump slot table
};  // 24 bytes per entry
```

### UFT Manager -- `sub_1CD22E0`

The UFT manager (1,979 bytes, 10 KB decompiled) processes UFT/UDT entries across all compilation units:

1. **Build UID-to-key map** -- hashes `uuid_lo ^ uuid_hi` as the lookup key
2. **Detect conflicts** -- reports `"uft map conflict: 0x%llx"` when two entries hash to the same key
3. **Detect duplicates** -- reports `"duplicate ids in uft.entry"` when identical UUIDs appear
4. **Reorder entries** -- `"Re-ordering UFT entries"` / `"Re-ordering UDT entries"` sorts entries for deterministic output
5. **Match UUIDs** -- cross-references UUIDs against the existing UFT for linking: `"matching uuid not found"` if a referenced UUID does not exist
6. **Align UDT** -- `"udt size %lld needs aligning"` pads UDT entries to required alignment

### UFT Slot Validator -- `sub_1CD2AA0`

Validates consistency between `.nv.uft` (jump slots) and `.nv.uft.entry` (metadata):

```
"missing nv.uft.entry"
"Number of .nv.uft jump slots != Number of entries in .nv.uft.entry"
"size of uidx window != nv.uft"
```

### Synthetic Symbols

The resolver recognizes these synthetic symbol names:

| Symbol | Purpose |
|---|---|
| `__UFT_OFFSET` | Points to a UFT jump slot |
| `__UFT_CANONICAL` | Canonical UUID entry for a UFT slot |
| `__UDT_OFFSET` | Points to a UDT data slot |
| `__UDT_CANONICAL` | Canonical UUID entry for a UDT slot |
| `__UFT` / `__UFT_END` | UFT table start/end boundaries |
| `__UDT` / `__UDT_END` | UDT table start/end boundaries |
| `$NVLINKBINDLESSOFF_<name>` | Bindless texture/surface offset symbol |
| `__cuda_syscall` | Device-side syscall dispatcher |

## Extern Shared Memory Relocations

Extern shared memory variables (declared with `extern __shared__`) are handled specially because their addresses are not known until kernel launch. The resolver tracks these through dedicated strings:

```
"extern shared variable %s at offset %lld"
"reloc of extern shared %d replaced with symbol %d"
"new extern shared instance %d"
```

Multiple kernels may reference the same extern shared variable. The linker creates separate instances when necessary and patches the relocation to point to the correct instance.

## Weak Symbol Handling

When `nvlink` encounters a weak symbol that conflicts with a strong definition:

```
"Could not replace weak symbol '%s'"
```

This occurs during the relocation pre-scan (`sub_1CD43A0`) when processing relocations that reference weak symbols. The pre-scan walks all relocations and checks the symbol binding at `sym->st_other & 0xE0`:
- `0x80` = weak: eligible for replacement by a strong definition
- `0x20` = global: normal binding

## Linking Model

### Relocatable Object Mode (`-c`)

When ptxas produces a relocatable object (`.o`), all relocations are preserved in `.rela.text.<func>` sections. The call graph is written to `.nv.callgraph`. Symbols retain their binding information for nvlink to resolve.

```
"No relocatable objects found. Did not generate callgraph."
"Generate relocatable object"
```

The `--preserve-relocs` flag additionally preserves relocations that would normally be resolved internally:

```
"This option will make PTXAS to generate relocatable references for variables and preserve ..."
```

### Executable Mode (default)

In the default mode, ptxas resolves all internal relocations and writes `.nv.resolvedrela` for any relocations that require runtime resolution. External references and function descriptors for indirect calls are preserved as unresolved relocations for the CUDA driver's runtime linker.

### PIC Mode

Position-independent code mode (`IsPIC` flag) changes the relocation encoding. The ELF flags word at ELFW offset +48 encodes this mode. PIC cubins use additional program-relative relocations and avoid absolute addresses where possible.

## CDP Runtime Symbol Table -- `off_2403A60`

The reference to "CUDA Dynamic Parallelism" (CDP, also spelled "cnp" internally for *CUDA Nested Parallelism*) is preserved as a static 34-entry pointer array at virtual address `0x2403A60` in the `.rodata` segment. Each entry is an 8-byte little-endian pointer to a NUL-terminated symbol name elsewhere in `.rodata`. The array is bracketed by an 8-byte NUL gap at `0x2403A58` and terminated at `0x2403B70` by zero padding followed by the unrelated string `" ERROR "`. When ptxas emits a cubin that calls into the device runtime, these are the symbol names it writes into the `.symtab` as `STB_GLOBAL`/`STT_NOTYPE` undefined references for the CUDA driver's runtime linker to resolve against `libcudadevrt`.

Entries 0--4 are POSIX-style stdio/heap stubs the compiler synthesizes whenever PTX uses `vprintf`, `malloc`, `free`, `vfprintf`, or `assert`. Entries 5--28 are the device-side launch/sync/event/query API. Entries 29--33 are the CUDA Graphs device-launch additions (this group's name strings live in a separate string pool starting at `0x24038C6`, distinct from the `0x21F4F62`-based pool used by entries 0--28; see the "VA" column below). The terminator dword at `0x2403B70` is `0x00000000` and is what the runtime symbol resolver tests against to know it has reached the end of the table.

| # | Entry VA | Points to | Symbol name | Notes |
|---:|---|---|---|---|
| 0  | `0x2403A60` | `0x21F4F62` | `vprintf` | stdio stub |
| 1  | `0x2403A68` | `0x21F4F6A` | `malloc` | heap stub |
| 2  | `0x2403A70` | `0x21F4F71` | `free` | heap stub |
| 3  | `0x2403A78` | `0x21F4F76` | `vfprintf` | stdio stub |
| 4  | `0x2403A80` | `0x21F4F7F` | `__assertfail` | `assert()` lowering |
| 5  | `0x2403A88` | `0x21F4F8C` | `__profile` | profile counter hook |
| 6  | `0x2403A90` | `0x21F4F96` | `cnpGetParameterBuffer` | CDP arg buffer alloc |
| 7  | `0x2403A98` | `0x21F4FAC` | `cnpLaunchDevice` | child kernel launch (v1) |
| 8  | `0x2403AA0` | `0x21F4FBC` | `cnpCtxSynchronize` | wait on all children |
| 9  | `0x2403AA8` | `0x21F4FCE` | `cnpFuncGetAttribute` | query kernel attrs |
| 10 | `0x2403AB0` | `0x21F4FE2` | `cnpStreamCreate` | device-side stream |
| 11 | `0x2403AB8` | `0x21F4FF2` | `cnpStreamDestroy` | -- |
| 12 | `0x2403AC0` | `0x21F5003` | `cnpStreamWaitEvent` | -- |
| 13 | `0x2403AC8` | `0x21F5016` | `cnpEventCreate` | device-side event |
| 14 | `0x2403AD0` | `0x21F5025` | `cnpEventRecord` | -- |
| 15 | `0x2403AD8` | `0x21F5034` | `cnpEventDestroy` | -- |
| 16 | `0x2403AE0` | `0x21F5044` | `cnpDeviceGetName` | device introspection |
| 17 | `0x2403AE8` | `0x21F5055` | `cnpDeviceGetAttribute` | -- |
| 18 | `0x2403AF0` | `0x21F506B` | `cnpGetDeviceCount` | -- |
| 19 | `0x2403AF8` | `0x21F507D` | `cnpGetDevice` | -- |
| 20 | `0x2403B00` | `0x21F508A` | `cnpGetLastError` | error channel |
| 21 | `0x2403B08` | `0x21F509A` | `cnpSetLastError` | -- |
| 22 | `0x2403B10` | `0x21F50AA` | `cnpGetCacheConfig` | -- |
| 23 | `0x2403B18` | `0x21F50BC` | `cnpGetSharedMemConfig` | -- |
| 24 | `0x2403B20` | `0x21F50D2` | `cnpGetLimit` | -- |
| 25 | `0x2403B28` | `0x21F50DE` | `cnpDeviceGetTotalMem` | -- |
| 26 | `0x2403B30` | `0x21F50F3` | `cnpGetParameterBufferV2` | CDP v2 ABI |
| 27 | `0x2403B38` | `0x21F510B` | `cnpLaunchDeviceV2` | child kernel launch (v2) |
| 28 | `0x2403B40` | `0x21F511D` | `cudaGraphLaunch` | device-side graph launch |
| 29 | `0x2403B48` | `0x24038C6` | `cudaGraphSetConditional` | graph conditional node |
| 30 | `0x2403B50` | `0x24038DE` | `cudaGraphKernelNodeSetParam` | -- |
| 31 | `0x2403B58` | `0x24038FA` | `cudaGraphKernelNodeSetGridDim` | -- |
| 32 | `0x2403B60` | `0x2403918` | `cudaGraphKernelNodeSetEnabled` | -- |
| 33 | `0x2403B68` | `0x2403960` | `cudaGraphKernelNodeUpdatesApply` | -- |
| -- | `0x2403B70` | `0x00000000` | *terminator* | dword zero |

QUIRK: the symbol names use the legacy `cnp` prefix (CUDA Nested Parallelism, the project's original 2011-era codename) rather than the public-facing `cudaDevice*` names exposed in `cuda_device_runtime_api.h`. The driver-side `libcudadevrt` exports both spellings as aliases. Mixing a relocatable object emitted by an older ptxas (which only references `cnp*`) with a newer one (which adds the `cudaGraph*` entries 29--33) therefore still links cleanly, since the alias map covers backwards compatibility -- but the reverse, feeding a `cudaGraph*` reference into a pre-Graphs driver, fails at runtime symbol resolution with `CUDA_ERROR_INVALID_PTX`.

QUIRK: entries 29--33 (`cudaGraph*`) point into a *different* string pool (`0x24038C6` and up) than entries 0--28 (`0x21F4F62` and up). The two pools sit roughly 2 MB apart in `.rodata`, indicating the Graphs entries were appended to the table after the original layout was frozen, and the build system did not relocate the older block to keep all 34 strings adjacent. The terminator dword is plain `0x00000000`, not a sentinel pointer -- the resolver's loop reads a qword and stops when the low 8 bytes are zero, which means a hypothetical 35th entry pointing into the address range `0x0000000100000000` and above (where bits 32--63 are nonzero) would *not* be misidentified as the terminator, but any pointer to the first 4 GB of address space happening to be zero would. ASLR loads ptxas above 4 GB, so the constraint holds in practice.

## Cross-References

- [Custom ELF Emitter](elf-emitter.md) -- ELFW object, header construction, file serialization
- [Section Catalog & EIATTR](sections.md) -- complete section type inventory, EIATTR encoding
- [Debug Information](debug-info.md) -- DWARF section generation
- [Pipeline Overview](../pipeline/output.md) -- where relocation resolution fits in the 11-phase pipeline
- [Capsule Mercury](../codegen/capmerc.md) -- Mercury-specific relocation handling

## Function Map

| Address | Size (binary) | Decompiled | Callers | Callees | Purpose |
|---|---|---|---|---|---|
| `sub_1CD48C0` | 4,184 B | 22 KB | 1 | 17 | Master relocation resolver (7-step algorithm) |
| `sub_1CD5920` | 1,985 B | 11 KB | 1 | 9 | Relocation writer (.nv.resolvedrela) |
| `sub_1CD4510` | ~860 B | 4 KB | -- | -- | Relocation creator (SASS) |
| `sub_1CD46B0` | ~540 B | 4 KB | -- | -- | Relocation creator (Mercury) |
| `sub_1CD43A0` | ~560 B | 3 KB | -- | -- | Relocation pre-scan (weak/extern) |
| `sub_1CD34E0` | 3,700 B | 17 KB | 1 | 2 | Bit-field patcher (sub_1CD33F0 extract, sub_1CD3330 insert) |
| `sub_1CD33F0` | ~300 B | 2 KB | 7 | 1 | Extract bits from instruction word |
| `sub_1CD3330` | ~200 B | 1 KB | 5 | 0 | Insert bits into instruction word |
| `sub_1CD22E0` | 1,979 B | 10 KB | 2 | 20 | UFT manager (UUID-to-slot mapping) |
| `sub_1CD2AA0` | ~800 B | 3 KB | -- | -- | UFT slot validator |
| `sub_1CB68D0` | 9,578 B | 49 KB | 1 | 36 | Symbol table builder (.symtab) |
| `sub_1CB2CA0` | 2,038 B | 8 KB | 4 | 11 | Symbol fixup (post-deletion renumbering) |
| `sub_1C99BB0` | 4,900 B | 25 KB | 1 | 18 | Section index remap (.symtab_shndx) |
| `sub_1CB64A0` | ~500 B | 2 KB | -- | -- | Symbol resolver (checks .nv.* special names) |
| `sub_1CAB300` | 2,157 B | 12 KB | 1 | 19 | Bindless texture/surface handler |
| `sub_1CA6890` | 2,286 B | 15 KB | 2 | 11 | Constant bank deduplication |
| `sub_1CBD0D0` | -- | -- | -- | -- | Call graph edge registration |
