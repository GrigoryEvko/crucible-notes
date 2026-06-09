# R_MERCURY Relocations

nvlink defines 65 Mercury-specific ELF relocation types for the capsule Mercury (capmerc) binary format, used on sm100+ architectures (Blackwell and later). These types are stored in `.rela.*` sections of capmerc ELF files and are consumed by the same relocation application engine that handles R_CUDA types, but through a separate descriptor table. Each R_MERCURY type in the ELF is encoded as the table index plus `0x10000` — the relocation engine subtracts this offset at dispatch time to index into the Mercury-specific descriptor table.

Mercury relocations are structurally simpler than their R_CUDA counterparts. Where R_CUDA has 119 types covering six generations of SASS instruction encoding (each with different bit-field positions for the same logical operation), R_MERCURY has 65 types that target a single 128-bit instruction format. The R_MERCURY set eliminates the per-bit-position variants (`R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32`) in favor of position-independent types (`R_MERCURY_ABS32`) — the bit-field position is encoded in the 64-byte descriptor rather than in the type name.

## Key Facts

| Property | Value |
|---|---|
| Machine type | `EM_CUDA` (190) with ELF class byte `0x41` (`'A'`) |
| Total unique type names | 65 (indices 0–64) |
| ELF type encoding | Table index + `0x10000` |
| Mercury name table | `off_1D371E0` (65 entries, pointers at string addresses `0x1D35A17`--`0x1D35F4C`) |
| Mercury descriptor table | `off_1D3CBE0` (65 entries, 64 bytes each = 4,160 bytes) |
| CUDA descriptor table | `off_1D3DBE0` (117 entries, 64 bytes each = 7,488 bytes) |
| Descriptor size | 64 bytes per type (12-byte header + 3 actions x 16 bytes + 4-byte sentinel) |
| Application engine | `sub_468760` at `0x468760` (14,322 bytes, shared with R_CUDA) |
| Relocation phase entry | `sub_469D60` at `0x469D60` (performs table selection and symbol resolution) |
| Relocation record builder | `sub_469B50` at `0x469B50` (creates reloc linked-list entries) |
| Validation function | `sub_42F6C0` at `0x42F6C0` (shared with R_CUDA) |
| Attribute validation | `sub_42F760` at `0x42F760` (three-way dispatch: warn/error/ignore) |
| Bitfield extract helper | `sub_468670` at `0x468670` (multi-word recursive extraction) |
| Bitfield write helper | `sub_4685B0` at `0x4685B0` (multi-word bit splicing) |
| Table limit check | Index < `0x41` (65) for Mercury; < `0x75` (117) for CUDA |
| Third name table copy | `0x2459160` (capmerc uplift / self-check path) |
| Architecture scope | sm100 (Blackwell), sm103 (Blackwell Ultra), sm110, sm120, sm121 |

## ELF Type Encoding and Dispatch

When the linker loads a relocation entry from a capmerc ELF file, the `r_info` type field contains the Mercury type index plus `0x10000`. The dispatch logic in the relocation phase function (`sub_469D60`) detects this offset and routes to the Mercury descriptor table. The code from the decompiled `sub_469D60` at lines 188–215 shows the exact branching:

```c
// sub_469D60 at 0x469D60 -- relocation phase dispatcher
// v2 = linker context, v5 = rela entry (16-byte __m128i), v9 = reloc type

v7 = 1;
v8 = rela_entry->type_and_sym;          // r_info from Elf64_Rela
if (*(byte*)(context + 7) != 0x41)       // check ELF class byte at offset +7
    v7 = 0x80000000;                     // non-Mercury flag
v9 = rela_entry->type_and_sym;

if ((v7 & *(uint32*)(context + 48)) != 0) {  // Mercury path
    if ((uint32_t)v8 != 0) {
        if ((uint32_t)v8 <= 0x10000) {
            fatal("unexpected reloc");        // Mercury types must be > 0x10000
        }
        descriptor_table = &off_1D3CBE0;      // Mercury descriptor table
        type_index = v9 - 0x10000;            // subtract offset to get raw index
    } else {
        descriptor_table = &off_1D3CBE0;
        type_index = 0;                       // R_MERCURY_NONE
    }
} else {                                      // CUDA path
    type_index = rela_entry->type;
    descriptor_table = &off_1D3DBE0;          // CUDA descriptor table
}
```

The `0x10000` namespace separation ensures that R_MERCURY and R_CUDA type numbers never collide. A raw ELF type value of `0x10000` maps to Mercury index 0 (`R_MERCURY_NONE`); `0x10001` maps to index 1 (`R_MERCURY_G64`); and so on through `0x10040` for index 64 (`R_MERCURY_NONE_LAST`).

The ELF class byte at offset `+7` of the linker context distinguishes Mercury from CUDA. The value `0x41` (ASCII `'A'`) indicates a Mercury ELF. When this byte is `0x41`, the dispatcher sets the Mercury flag to `1`; otherwise it sets `0x80000000` (the CUDA/traditional cubin flag). The AND with the e_flags word at context offset `+48` determines which path is taken.

### Relocation Record Builder: sub_469B50

Before the relocation engine runs, the record builder at `sub_469B50` creates linked-list entries for each relocation. This function also performs the table selection and index normalization:

```c
// sub_469B50 at 0x469B50 -- builds relocation records
v6 = 0x80000000;
if (*(byte*)(context + 7) == 0x41)       // Mercury ELF?
    v6 = 1;

// Validate the relocation type against the target architecture
result = sub_42F6C0(reloc_type, arch_class, (v6 & e_flags) != 0, ...);
if (!result) return;

// Select descriptor table for action-type checking
if ((v6 & e_flags) != 0) {               // Mercury path
    normalized_index = reloc_type - 0x10000;
    table = &off_1D3CBE0;
    if (normalized_index <= 0x3F) {        // index < 64
        // Check if action type indicates a special reloc (codes 12-15)
        if ((uint32_t)(table[8 * normalized_index + 2].hi - 12) <= 3)
            sub_44C010(context, section_idx);   // mark section for special handling
    }
} else {                                  // CUDA path
    normalized_index = reloc_type;
    table = &off_1D3DBE0;
    if (normalized_index <= 0x73) {        // index < 116
        // Same action-type check for CUDA
        ...
    }
}
```

The action-type range check `(action_type - 12) <= 3` catches types with action codes 12 through 15, which correspond to special relocation operations that require the target section to be marked for deferred processing.

## Naming Convention

R_MERCURY type names follow a simpler pattern than R_CUDA:

```text
R_MERCURY_<category>[<bits>][_<byte_offset>]
```

Because Mercury targets a single 128-bit instruction format (not the variable 64/128-bit encodings of previous SASS generations), there is no need for per-bit-position suffixes. The components are:

- **Category**: the semantic class of the relocation (`ABS`, `G`, `PROG_REL`, `FUNC_DESC`, `UNIFIED`, `TEX`, `SAMP`, `SURF`)
- **Bits**: the width of the relocated value (`8`, `16`, `32`, `64`)
- **Byte offset**: for byte-level variants, the bit offset within the 64-bit word (`_0`, `_8`, `_16`, `_24`, `_32`, `_40`, `_48`, `_56`)
- **HI/LO**: for split relocations, which half of a 32-bit value is patched

## Complete Type Catalog

### Sentinel Types

| Index | ELF Type | Name | Description |
|---|---|---|---|
| 0 | `0x10000` | `R_MERCURY_NONE` | No relocation (placeholder / deleted entry) |
| 64 | `0x10040` | `R_MERCURY_NONE_LAST` | Sentinel marking end of valid type range |

`R_MERCURY_NONE` at index 0 serves the same purpose as `R_CUDA_NONE`: it is a no-op relocation used for entries that have been eliminated by dead code removal or relocation merging. The application engine returns immediately when it encounters a descriptor with all-zero action types. `R_MERCURY_NONE_LAST` at index 64 is a bounds sentinel — the validation function rejects any type index >= 65.

### Global Address Relocation

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 1 | `0x10001` | `R_MERCURY_G64` | 64 | 64-bit global memory address |

`R_MERCURY_G64` is the primary relocation for global memory address references. It computes `S + A` (symbol value plus addend) and writes the full 64-bit result. Used for references to symbols in `.nv.global` and similar global data sections. This is the Mercury equivalent of `R_CUDA_G64`.

### Absolute Data Relocations

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 2 | `0x10002` | `R_MERCURY_ABS64` | 64 | 64-bit absolute address |
| 3 | `0x10003` | `R_MERCURY_ABS32` | 32 | 32-bit absolute address |
| 4 | `0x10004` | `R_MERCURY_ABS16` | 16 | 16-bit absolute address |
| 5 | `0x10005` | `R_MERCURY_ABS32_LO` | 16 | Low 16 bits of 32-bit absolute |
| 6 | `0x10006` | `R_MERCURY_ABS32_HI` | 16 | High 16 bits of 32-bit absolute |

These relocations apply to data sections and instruction immediate fields using absolute addressing. The engine computes `S + A` and writes the result (or a 16-bit portion of it for `_LO`/`_HI` variants).

The `ABS32_LO` / `ABS32_HI` pair is used when a 32-bit absolute address must be split across two instruction immediates. One instruction loads the low 16 bits, the other loads the high 16 bits. The linker resolves both from the same symbol.

Unlike R_CUDA, which has separate types for each bit-position within the instruction word (`R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32`), the Mercury types are position-independent — the bit-field offset is stored in the 64-byte descriptor entry.

### PC-Relative Relocations

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 7 | `0x10007` | `R_MERCURY_PROG_REL64` | 64 | 64-bit PC-relative offset |
| 8 | `0x10008` | `R_MERCURY_PROG_REL32` | 32 | 32-bit PC-relative offset |
| 9 | `0x10009` | `R_MERCURY_PROG_REL32_LO` | 16 | Low 16 bits of 32-bit PC-relative |
| 10 | `0x1000A` | `R_MERCURY_PROG_REL32_HI` | 16 | High 16 bits of 32-bit PC-relative |

PC-relative relocations compute `(S + A) - PC`, where `PC` is the address of the instruction being patched. These are used for branch instructions (`BRA`, `CALL`, `BRX`) in Mercury's 128-bit instruction format.

The 64-bit variant (`PROG_REL64`) supports the full address space. The 32-bit variant limits branch distances to +/- 2 GB, sufficient for all practical kernel sizes. The `_LO`/`_HI` split variants handle cases where the PC-relative offset must be encoded in two separate instruction fields.

In the application engine, PC-relative relocations use action type `0x10` (pc_rel), which computes `(int32_t)(S + A) - section_offset`. The relocation phase function (`sub_469D60`) validates at line 409–410 that PC-relative branch targets reside in the same section:

```c
if (descriptor[5] == 16 && rela_entry.section_idx != target_section_idx)
    fatal("PC relative branch address should be in the same section");
```

### Texture, Sampler, and Surface Relocations

| Index | ELF Type | Name | Description |
|---|---|---|---|
| 11 | `0x1000B` | `R_MERCURY_TEX_HEADER_INDEX` | Texture header table index |
| 12 | `0x1000C` | `R_MERCURY_SAMP_HEADER_INDEX` | Sampler header table index |
| 13 | `0x1000D` | `R_MERCURY_SURF_HEADER_INDEX` | Surface header table index |

These relocations resolve bindable resource references — texture, sampler, and surface objects — by patching the merged header table index into instruction fields. During linking, individual per-module header tables are merged into a single global table; these relocations update the instruction operands to reference the correct entry in the merged table.

These are direct equivalents of `R_CUDA_TEX_HEADER_INDEX`, `R_CUDA_SAMP_HEADER_INDEX`, and `R_CUDA_SURF_HEADER_INDEX`. Mercury drops the `SLOT`, `HW_DESC`, and `HW_SW_DESC` variants that exist in R_CUDA, consolidating texture resource binding into the header index mechanism alone.

### Clear Relocation

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 14 | `0x1000E` | `R_MERCURY_UNUSED_CLEAR64` | 64 | Zero 64 bits (clear unused field) |

Writes zeros to a 64-bit field. Used to clear placeholder entries in merged data sections or nullify unused instruction fields after linking. Uses action types `0x13` or `0x14` (clear), which simply AND-zero the target bit-field without computing any symbol value. Equivalent to `R_CUDA_UNUSED_CLEAR64`.

### Function Descriptor Relocation

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 15 | `0x1000F` | `R_MERCURY_FUNC_DESC_64` | 64 | 64-bit function descriptor reference |

Resolves a reference to a function descriptor entry. Function descriptors are used for indirect calls, virtual function tables, and device-side function pointers. The relocation writes the 64-bit address of the descriptor entry.

### Byte-Level Relocations (R_MERCURY_8_*)

| Index | ELF Type | Name | Byte offset | Description |
|---|---|---|---|---|
| 16 | `0x10010` | `R_MERCURY_8_0` | 0 | Byte at bit offset 0 |
| 17 | `0x10011` | `R_MERCURY_8_8` | 1 | Byte at bit offset 8 |
| 18 | `0x10012` | `R_MERCURY_8_16` | 2 | Byte at bit offset 16 |
| 19 | `0x10013` | `R_MERCURY_8_24` | 3 | Byte at bit offset 24 |
| 20 | `0x10014` | `R_MERCURY_8_32` | 4 | Byte at bit offset 32 |
| 21 | `0x10015` | `R_MERCURY_8_40` | 5 | Byte at bit offset 40 |
| 22 | `0x10016` | `R_MERCURY_8_48` | 6 | Byte at bit offset 48 |
| 23 | `0x10017` | `R_MERCURY_8_56` | 7 | Byte at bit offset 56 |

Byte-granularity relocations that patch a single 8-bit byte at a specific offset within a 64-bit data word. The eight types cover all byte positions within a 64-bit value. These are used for patching descriptor tables, attribute sections, and other data structures where individual bytes must be resolved separately.

Direct equivalents of `R_CUDA_8_0` through `R_CUDA_8_56`.

### Global Byte-Level Relocations (R_MERCURY_G8_*)

| Index | ELF Type | Name | Byte offset | Description |
|---|---|---|---|---|
| 24 | `0x10018` | `R_MERCURY_G8_0` | 0 | Global byte at bit offset 0 |
| 25 | `0x10019` | `R_MERCURY_G8_8` | 1 | Global byte at bit offset 8 |
| 26 | `0x1001A` | `R_MERCURY_G8_16` | 2 | Global byte at bit offset 16 |
| 27 | `0x1001B` | `R_MERCURY_G8_24` | 3 | Global byte at bit offset 24 |
| 28 | `0x1001C` | `R_MERCURY_G8_32` | 4 | Global byte at bit offset 32 |
| 29 | `0x1001D` | `R_MERCURY_G8_40` | 5 | Global byte at bit offset 40 |
| 30 | `0x1001E` | `R_MERCURY_G8_48` | 6 | Global byte at bit offset 48 |
| 31 | `0x1001F` | `R_MERCURY_G8_56` | 7 | Global byte at bit offset 56 |

Byte-granularity relocations for global memory addresses. These are the byte-level counterparts of `R_MERCURY_G64` — instead of writing a full 64-bit global address, they write a single byte of the address at a specific position. Used when a global address must be assembled byte-by-byte in a data structure.

Direct equivalents of `R_CUDA_G8_0` through `R_CUDA_G8_56`.

### Function Descriptor Byte-Level Relocations (R_MERCURY_FUNC_DESC_8_*)

| Index | ELF Type | Name | Byte offset | Description |
|---|---|---|---|---|
| 32 | `0x10020` | `R_MERCURY_FUNC_DESC_8_0` | 0 | Descriptor byte at bit offset 0 |
| 33 | `0x10021` | `R_MERCURY_FUNC_DESC_8_8` | 1 | Descriptor byte at bit offset 8 |
| 34 | `0x10022` | `R_MERCURY_FUNC_DESC_8_16` | 2 | Descriptor byte at bit offset 16 |
| 35 | `0x10023` | `R_MERCURY_FUNC_DESC_8_24` | 3 | Descriptor byte at bit offset 24 |
| 36 | `0x10024` | `R_MERCURY_FUNC_DESC_8_32` | 4 | Descriptor byte at bit offset 32 |
| 37 | `0x10025` | `R_MERCURY_FUNC_DESC_8_40` | 5 | Descriptor byte at bit offset 40 |
| 38 | `0x10026` | `R_MERCURY_FUNC_DESC_8_48` | 6 | Descriptor byte at bit offset 48 |
| 39 | `0x10027` | `R_MERCURY_FUNC_DESC_8_56` | 7 | Descriptor byte at bit offset 56 |

Byte-level relocations for function descriptor addresses in data sections. These patch individual bytes of a 64-bit function descriptor reference, mirroring the `R_MERCURY_FUNC_DESC_64` / byte-level split pattern used by the other relocation families.

Direct equivalents of `R_CUDA_FUNC_DESC_8_0` through `R_CUDA_FUNC_DESC_8_56`.

### Absolute PC-Relative Relocations

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 40 | `0x10028` | `R_MERCURY_ABS_PROG_REL32_LO` | 16 | Low 16 bits of absolute PC-relative 32-bit |
| 41 | `0x10029` | `R_MERCURY_ABS_PROG_REL32_HI` | 16 | High 16 bits of absolute PC-relative 32-bit |
| 60 | `0x1003C` | `R_MERCURY_ABS_PROG_REL32` | 32 | 32-bit absolute PC-relative offset |
| 61 | `0x1003D` | `R_MERCURY_ABS_PROG_REL64` | 64 | 64-bit absolute PC-relative offset |

These are hybrid relocations that combine absolute and PC-relative semantics. The computation is `|S + A - PC|` or a variant that uses the absolute value of the PC-relative offset, used in instruction encodings that require an unsigned distance rather than a signed offset.

The `_LO`/`_HI` split variants (indices 40–41) appear earlier in the table than the full-width variants (indices 60–61), suggesting they were added in an earlier revision and the full-width types were appended later.

No direct R_CUDA equivalent exists for these types. They appear to be Mercury-specific additions for the 128-bit instruction format's distance-based addressing modes.

### PC-Relative Byte-Level Relocations (R_MERCURY_PROG_REL8_*)

| Index | ELF Type | Name | Byte offset | Description |
|---|---|---|---|---|
| 42 | `0x1002A` | `R_MERCURY_PROG_REL8_0` | 0 | PC-relative byte at bit offset 0 |
| 43 | `0x1002B` | `R_MERCURY_PROG_REL8_8` | 1 | PC-relative byte at bit offset 8 |
| 44 | `0x1002C` | `R_MERCURY_PROG_REL8_16` | 2 | PC-relative byte at bit offset 16 |
| 45 | `0x1002D` | `R_MERCURY_PROG_REL8_24` | 3 | PC-relative byte at bit offset 24 |
| 46 | `0x1002E` | `R_MERCURY_PROG_REL8_32` | 4 | PC-relative byte at bit offset 32 |
| 47 | `0x1002F` | `R_MERCURY_PROG_REL8_40` | 5 | PC-relative byte at bit offset 40 |
| 48 | `0x10030` | `R_MERCURY_PROG_REL8_48` | 6 | PC-relative byte at bit offset 48 |
| 49 | `0x10031` | `R_MERCURY_PROG_REL8_56` | 7 | PC-relative byte at bit offset 56 |

Byte-level PC-relative relocations. These compute `(S + A) - PC` and then extract a single byte at the specified offset from the result. Used for data structures that assemble PC-relative offsets byte-by-byte.

No direct R_CUDA equivalent exists. These are Mercury-specific additions for use in data tables (jump tables, exception tables) where PC-relative distances are stored in byte-addressable format.

### Unified Table Relocations (R_MERCURY_UNIFIED_*)

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 50 | `0x10032` | `R_MERCURY_UNIFIED` | special | Unified table reference (generic marker) |
| 51 | `0x10033` | `R_MERCURY_UNIFIED_32` | 32 | 32-bit unified table offset |
| 52 | `0x10034` | `R_MERCURY_UNIFIED_8_0` | 8 | Unified byte at bit offset 0 |
| 53 | `0x10035` | `R_MERCURY_UNIFIED_8_8` | 8 | Unified byte at bit offset 8 |
| 54 | `0x10036` | `R_MERCURY_UNIFIED_8_16` | 8 | Unified byte at bit offset 16 |
| 55 | `0x10037` | `R_MERCURY_UNIFIED_8_24` | 8 | Unified byte at bit offset 24 |
| 56 | `0x10038` | `R_MERCURY_UNIFIED_8_32` | 8 | Unified byte at bit offset 32 |
| 57 | `0x10039` | `R_MERCURY_UNIFIED_8_40` | 8 | Unified byte at bit offset 40 |
| 58 | `0x1003A` | `R_MERCURY_UNIFIED_8_48` | 8 | Unified byte at bit offset 48 |
| 59 | `0x1003B` | `R_MERCURY_UNIFIED_8_56` | 8 | Unified byte at bit offset 56 |
| 62 | `0x1003E` | `R_MERCURY_UNIFIED32_LO` | 16 | Low 16 bits of 32-bit unified offset |
| 63 | `0x1003F` | `R_MERCURY_UNIFIED32_HI` | 16 | High 16 bits of 32-bit unified offset |

Unified table relocations handle references to the Unified Descriptor Table (UDT) and Unified Function Table (UFT). These tables are used for CUDA Dynamic Parallelism and indirect function calls.

`R_MERCURY_UNIFIED` (index 50) is a generic marker type, not a data-patching relocation. During the relocation phase, unified relocations targeting synthetic symbols (`__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UDT`, `__UFT`, `__UFT_END`, `__UDT_END`) are resolved to type 0 (no-op) because the unified table manager computes final offsets before the relocation engine runs. The code in `sub_469D60` lines 377–395 explicitly checks for the `__UFT_OFFSET` symbol by string comparison and emits a debug trace when matched:

```c
// Check if symbol name is "__UFT_OFFSET" (13-byte comparison)
if (strncmp(symbol_name, "__UFT_OFFSET", 13) == 0) {
    if (debug_flags & 4)
        fwrite("ignore reloc on UFT_OFFSET\n", 1, 0x1B, stderr);
    rela_entry.type = 0;   // convert to R_MERCURY_NONE
    ...
}
```

The `UNIFIED32_LO` and `UNIFIED32_HI` types (indices 62–63) appear after the `ABS_PROG_REL` types in the table rather than adjacent to the other unified types (50–59). This suggests they were added in a later revision.

Two additional trailing-space variants (`"R_MERCURY_UNIFIED_8_0 "` and `"R_MERCURY_UNIFIED_8_8 "`) appear at separate string addresses (`0x1D3CB71` and `0x1D3CB88`) in the Mercury descriptor table region. These are duplicate name strings used by the descriptor table entries themselves, distinct from the primary name table entries (which lack the trailing space). The trailing space is a formatting artifact in the binary's `.rodata`, not a distinct relocation type.

## The Sub-Byte Relocation Mechanism

Mercury introduces a sub-byte relocation mechanism unique to GPU instruction patching. The byte-level relocation families (`R_MERCURY_8_*`, `R_MERCURY_G8_*`, `R_MERCURY_FUNC_DESC_8_*`, `R_MERCURY_PROG_REL8_*`, `R_MERCURY_UNIFIED_8_*`) each provide eight variants covering every byte position within a 64-bit word. The mechanism works as follows:

### How Sub-Byte Patching Works

1. **Full address computation**: The engine first computes the full relocation value (`S + A` for absolute, `(S + A) - PC` for PC-relative).

2. **Byte extraction**: The descriptor's action type encodes which byte of the 64-bit result to extract. The action types `0x16`--`0x1D` and `0x2F`--`0x36` (masked_shift operations) use paired SSE constant vectors loaded from `xmmword_1D3F8E0`--`xmmword_1D3F930`. These contain 64-bit masks and shift counts indexed by `(action_type - 22)`:

```c
// sub_468760, case 0x16..0x1D / 0x2F..0x36 (masked_shift)
int idx = action_type - 22;
uint64_t mask  = mask_table[idx];   // from xmmword_1D3F920/xmmword_1D3F930
uint64_t shift = shift_table[idx];  // from xmmword_1D3F8E0..xmmword_1D3F910
value = (value & mask) >> shift;
```

3. **Bit-field insertion**: The extracted byte is written into the target using the descriptor's `bit_offset` and `bit_width` fields, via the `sub_4685B0` helper.

### Multi-Word Boundary Handling

Both the extraction (`sub_468670`) and write (`sub_4685B0`) helpers handle bit-fields that span 64-bit word boundaries — a common situation with 128-bit Mercury instructions. The extraction function is recursive:

```c
// sub_468670 -- bitfield_extract (simplified)
int64_t bitfield_extract(uint64_t* words, int bit_offset, int bit_width) {
    if (bit_offset >= 64) {
        words += bit_offset / 64;
        bit_offset %= 64;
    }
    int end_bit = bit_offset + bit_width;
    if (end_bit <= 64)
        return *words << (64 - end_bit) >> (64 - bit_width);

    // Recursive split: low part from this word, high part from next
    int64_t low  = bitfield_extract(words, bit_offset, 64 - bit_offset);
    int64_t high = words[1] << (128 - end_bit) >> (64 - (end_bit - 64));
    return low | (high << (64 - bit_offset));
}
```

The write function (`sub_4685B0`) similarly handles multi-word spans with an iterative loop that processes one 64-bit word at a time, shifting the value right after each partial write.

### Why Sub-Byte Relocations Exist

The 8-bit relocations serve data sections where 64-bit addresses are stored in structures with byte-aligned fields. Rather than requiring the compiler to emit aligned 64-bit relocations, the linker can patch individual bytes. This is particularly useful for:

- **Descriptor tables**: Hardware descriptor entries that pack multiple fields into bytes
- **Jump tables**: Array entries stored as byte-packed offsets
- **Constant buffer initialization**: Per-byte patching of constant bank data
- **Debug section data**: DWARF entries with byte-level address references

The PC-relative byte-level family (`R_MERCURY_PROG_REL8_*`) is unique to Mercury and has no R_CUDA counterpart. It enables byte-by-byte assembly of PC-relative offsets in data tables, which previous SASS generations did not support.

## Descriptor Table Structure

The Mercury descriptor table at `off_1D3CBE0` has the same 64-byte-per-entry format as the CUDA table at `off_1D3DBE0`. Each entry encodes up to three patching actions:

```text
Entry (64 bytes):
  +0   Header (12 bytes)
       +0   uint32_t  field_0;      // extraction spec for --preserve-relocs
       +4   uint32_t  field_1;      // extraction spec
       +8   uint32_t  field_2;      // extraction spec / action count hint
  +12  Action 0 (16 bytes)
       +12  uint32_t  bit_offset;   // start bit in instruction/data word
       +16  uint32_t  bit_width;    // number of bits to patch
       +20  uint32_t  action_type;  // operation code (see table below)
       +24  uint32_t  reserved;     // flags / padding
  +28  Action 1 (16 bytes)
       +28  uint32_t  bit_offset;
       +32  uint32_t  bit_width;
       +36  uint32_t  action_type;
       +40  uint32_t  reserved;
  +44  Action 2 (16 bytes)
       +44  uint32_t  bit_offset;
       +48  uint32_t  bit_width;
       +52  uint32_t  action_type;
       +56  uint32_t  reserved;
  +60  Sentinel (4 bytes, marks end of action array)
```

The application engine (`sub_468760`) indexes into the table and sets up its action pointer and sentinel:

```c
descriptor_base = table + (type_index << 6);  // type_index * 64
action_ptr = descriptor_base + 12;             // first action at byte +12
end_ptr    = descriptor_base + 60;             // sentinel at byte +60
```

### Action Types Used by Mercury Descriptors

The action types are identical between CUDA and Mercury — the engine is shared. Mercury descriptors use the following subset:

| Code | Name | Computation | Mercury Usage |
|---|---|---|---|
| `0x00` | end | Skip slot, terminate at sentinel | All types (terminates unused slots) |
| `0x01` | abs_full | `S + A` — store all bits | `ABS64`, `ABS32`, `G64`, `FUNC_DESC_64` |
| `0x06` | abs_lo | `(S + A) & mask` — low bits | `ABS32_LO`, `UNIFIED32_LO` |
| `0x07` | abs_hi | `((S + A) >> 32) & mask` — high bits | `ABS32_HI`, `UNIFIED32_HI` |
| `0x09` | abs_shifted | `(S + A) >> 2` — 4-byte aligned | `PROG_REL32` (shifted branch offset) |
| `0x10` | pc_rel | `(int32_t)(S + A) - PC` | `PROG_REL32`, `PROG_REL64` |
| `0x12` | abs_full (alias) | Same as `0x01` | `UNIFIED`, `UNIFIED_32` |
| `0x13` | clear | Zero the bit-field | `UNUSED_CLEAR64` |
| `0x14` | clear (alias) | Same as `0x13` | `UNUSED_CLEAR64` (secondary action) |
| `0x16`--`0x1D` | masked_shift_0..7 | `(value & mask) >> shift` | `8_*`, `G8_*`, `FUNC_DESC_8_*` |
| `0x2E` | abs_full (alias) | Same as `0x01` | `UNIFIED_8_*` |
| `0x2F`--`0x36` | masked_shift_8..15 | `(value & mask) >> shift` | `UNIFIED_8_*`, `PROG_REL8_*` |
| `0x37` | abs_lo (alias) | Same as `0x06` | `PROG_REL32_LO`, `ABS_PROG_REL32_LO` |
| `0x38` | abs_hi (alias) | Same as `0x07` | `PROG_REL32_HI`, `ABS_PROG_REL32_HI` |

Mercury does **not** use action types `0x08` (abs_size), `0x0A` (sec_type_lo), or `0x0B` (sec_type_hi), which are R_CUDA-specific operations for section-type patching and symbol-size computation.

### Difference from R_CUDA Descriptors

Although the table format is identical, the descriptor contents differ from R_CUDA because Mercury's 128-bit instruction words have different field layouts. A relocation that places a 32-bit absolute address at bit position 20 in a 64-bit CUDA instruction will place it at a different bit position in a 128-bit Mercury instruction — and this difference is encoded in the descriptor's `bit_offset` field, not in the type name.

The shared application engine `sub_468760` is type-agnostic: it simply reads the descriptor, executes the action sequence, and patches the bit fields. The distinction between CUDA and Mercury is entirely in which descriptor table is selected at dispatch time.

## Mercury Relocations and FNLZR Finalization

Mercury relocations interact with the FNLZR (Finalizer) subsystem at two critical points in the pipeline.

### Pre-Link Mode: Relocation Passthrough

In pre-link mode (`sub_4275C0` with `a5 == 0`), FNLZR processes individual cubins before they enter the merge phase. Mercury relocations at this stage are stored in `.nv.merc.rela` sections and reference the Mercury-private symbol table. The pre-link pass:

1. Validates the ELF class byte is `0x41` (Mercury)
2. Checks that finalization has not already been applied (`(flags >> 2) & 1` must be 0)
3. Passes the cubin through the `sub_4748F0` engine, which may rewrite relocations during opex expansion
4. Outputs a transformed cubin with potentially modified relocation entries

### Post-Link Mode: Final Application

In post-link mode (`sub_4275C0` with `a5 == 1`), FNLZR runs after the merge phase has applied all standard relocations. The post-link path:

1. Confirms the SASS-present or capmerc bit is set in e_flags
2. Invokes `sub_4748F0` for the capmerc-to-SASS transformation
3. The engine re-resolves any remaining Mercury relocations against the final symbol table
4. Outputs the final SASS binary with all relocations fully applied

The FNLZR diagnostic output shows the relocation interaction:

```text
FNLZR: Input ELF: <filename>
FNLZR: Post-Link Mode
FNLZR: Flags [ 1 | 0 ]           // capmerc=1, sass-only=0
FNLZR: Starting <filename>
  ... mercury relocation application ...
FNLZR: Ending <filename>
```

### Merge Phase: Section Skipping

During the merge phase (`sub_45E7D0`), Mercury-specific sections are conditionally skipped and deferred to FNLZR. The function emits a debug trace when skipping:

```c
// sub_45E7D0 at 0x45F624
fprintf(stderr, "skip mercury section %i\n", section_index);
```

This skip logic is gated by the `0x10000000` flag in the section's `sh_flags` field, which marks Mercury-specific sections that should not be merged by the standard section merger.

### Relocation Phase: Dead Code and YIELD Handling

The relocation phase (`sub_469D60`) contains Mercury-specific dead-code and YIELD-conversion logic:

1. **Dead function elimination**: When a relocation targets a dead function (binding type 1 with no name), the reloc type is zeroed and a debug trace is emitted:
   ```c
   fprintf(stderr, "ignore reloc on dead func %s\n", symbol_name);
   rela_entry.type = 0;   // convert to R_MERCURY_NONE
   ```

2. **YIELD instruction conversion**: Relocation types 68–69 (`0x10044`--`0x10045`, which map to R_CUDA YIELD types when using Mercury indexing) trigger special handling. When the forward-progress requirement flag (`context + 94`) is set, the linker ignores the YIELD-to-NOP conversion relocation:
   ```c
   fwrite("Ignoring the reloc to convert YIELD to NOP due to forward progress requirement.\n",
          1, 0x50, stderr);
   ```

## Capmerc Self-Check and Uplift

### Self-Check Infrastructure

nvlink includes a self-check mode activated by `--self-check` (registered in `sub_4AC380` at `0x4AC380`). When active, the linker verifies that Mercury relocation application produces correct results by comparing the patched output against expected values. The diagnostic messages and their binary addresses:

| String Address | Message | Triggered By |
|---|---|---|
| `0x1D41EC8` | `"Self check for capsule mercury (capmerc)"` | `sub_4AC380` (option registration) |
| `0x2458F38` | `"Self check for capsule mercury text section failed"` | Self-check comparison, `.text` mismatch |
| `0x2458F70` | `"Self check for capsule mercury debug section failed"` | Self-check comparison, debug section mismatch |
| `0x2458FA8` | `"Self check for capsule mercury relocation section failed"` | Self-check comparison, `.rela` section mismatch |
| `0x2458FE8` | `"Invalid elf provided for mercury uplift."` | Bad ELF input for Mercury conversion |
| `0x1F44288` | `"Failure of '%s' section in self-check for capsule mercury..."` | Generic section failure (Jira MERCSW-125) |

The self-check strings at `0x2458F38`--`0x2458FE8` are organized as a pointer array at `0x24590A0`--`0x24590B8`, suggesting a table-driven diagnostic approach where section indices map to error messages.

### Mercury Uplift

The "mercury uplift" path converts non-Mercury binaries to Mercury format. The `sub_4AC380` function also registers related options:

| CLI Flag | Description |
|---|---|
| `--binary-kind` | `mercury`, `capmerc`, or `sass` — specifies target binary format |
| `--cap-merc` | Force Capsule Mercury generation |
| `--self-check` | Enable self-check verification |
| `--out-sass` | Generate reconstituted SASS through self-check |
| `--compile-as-at-entry-patch` | Compile patch as at-entry fragment |
| `--opportunistic-finalization-lvl` | Control finalization aggressiveness (default 0) |

### Third Name Table at 0x2459160

The `R_MERCURY_NONE` string at `0x1D35A17` has three xrefs: the primary name table (`0x1D371E0`), the descriptor table (`0x1D3CBE0`), and a third pointer at `0x2459160`. This third reference is in the capmerc uplift/self-check code region and represents a separate copy of the name table used by the Mercury-to-SASS reconstitution path for relocation name lookup during verification.

## R_MERCURY vs R_CUDA Comparison

The following table maps each R_MERCURY type to its closest R_CUDA equivalent. Mercury's type set is a strict subset of R_CUDA's semantic categories, omitting instruction-encoding-specific types.

| R_MERCURY | Index | R_CUDA Equivalent | Notes |
|---|---|---|---|
| `R_MERCURY_NONE` | 0 | `R_CUDA_NONE` | Identical semantics |
| `R_MERCURY_G64` | 1 | `R_CUDA_G64` | Identical semantics |
| `R_MERCURY_ABS64` | 2 | `R_CUDA_64` | R_CUDA uses plain `_64` |
| `R_MERCURY_ABS32` | 3 | `R_CUDA_32` | R_CUDA uses plain `_32` |
| `R_MERCURY_ABS16` | 4 | `R_CUDA_ABS16_*` | Mercury: position-independent; CUDA: per-bit-position |
| `R_MERCURY_ABS32_LO` | 5 | `R_CUDA_ABS32_LO_*` | Mercury: 1 type; CUDA: 4 types (per bit-position) |
| `R_MERCURY_ABS32_HI` | 6 | `R_CUDA_ABS32_HI_*` | Mercury: 1 type; CUDA: 4 types |
| `R_MERCURY_PROG_REL64` | 7 | — | No direct CUDA equivalent |
| `R_MERCURY_PROG_REL32` | 8 | `R_CUDA_PCREL_IMM24_*` | CUDA uses 24-bit; Mercury uses 32-bit |
| `R_MERCURY_PROG_REL32_LO` | 9 | — | Mercury-specific |
| `R_MERCURY_PROG_REL32_HI` | 10 | — | Mercury-specific |
| `R_MERCURY_TEX_HEADER_INDEX` | 11 | `R_CUDA_TEX_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_SAMP_HEADER_INDEX` | 12 | `R_CUDA_SAMP_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_SURF_HEADER_INDEX` | 13 | `R_CUDA_SURF_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_UNUSED_CLEAR64` | 14 | `R_CUDA_UNUSED_CLEAR64` | Identical semantics |
| `R_MERCURY_FUNC_DESC_64` | 15 | `R_CUDA_FUNC_DESC_64` | Identical semantics |
| `R_MERCURY_8_*` | 16–23 | `R_CUDA_8_*` | Identical semantics |
| `R_MERCURY_G8_*` | 24–31 | `R_CUDA_G8_*` | Identical semantics |
| `R_MERCURY_FUNC_DESC_8_*` | 32–39 | `R_CUDA_FUNC_DESC_8_*` | Identical semantics |
| `R_MERCURY_ABS_PROG_REL32_LO` | 40 | — | Mercury-specific |
| `R_MERCURY_ABS_PROG_REL32_HI` | 41 | — | Mercury-specific |
| `R_MERCURY_PROG_REL8_*` | 42–49 | — | Mercury-specific (no CUDA byte-level PC-rel) |
| `R_MERCURY_UNIFIED` | 50 | `R_CUDA_UNIFIED` | Identical semantics |
| `R_MERCURY_UNIFIED_32` | 51 | `R_CUDA_UNIFIED_32` | Identical semantics |
| `R_MERCURY_UNIFIED_8_*` | 52–59 | `R_CUDA_UNIFIED_8_*` | Identical semantics |
| `R_MERCURY_ABS_PROG_REL32` | 60 | — | Mercury-specific |
| `R_MERCURY_ABS_PROG_REL64` | 61 | — | Mercury-specific |
| `R_MERCURY_UNIFIED32_LO` | 62 | `R_CUDA_UNIFIED32_LO_32` | CUDA includes bit-position |
| `R_MERCURY_UNIFIED32_HI` | 63 | `R_CUDA_UNIFIED32_HI_32` | CUDA includes bit-position |
| `R_MERCURY_NONE_LAST` | 64 | `R_CUDA_NONE_LAST` | Identical semantics |

### Design Philosophy: Position-Independent Types

The central design difference between R_MERCURY and R_CUDA relocations is the elimination of bit-position encoding from the type name. R_CUDA has 119 types because the same semantic operation (e.g., "patch a 32-bit absolute address into an instruction") requires separate type numbers for each instruction generation's bit-field layout. Mercury consolidates these into single types, pushing the bit-position information into the descriptor table.

This design has several consequences:

1. **Type count reduction**: 65 types vs 119, despite adding 15 new categories (PC-rel 64-bit, byte-level PC-rel, absolute PC-rel)
2. **Forward compatibility**: New Mercury instruction encodings with different bit-field positions need only new descriptor table entries, not new type numbers
3. **Simpler compiler output**: The compiler emits generic relocation types and the linker's descriptor table provides the architecture-specific bit-field mapping
4. **Shared engine**: The `sub_468760` engine works identically for both CUDA and Mercury because the bit-field information is always in the descriptor, never in the type-based code path

### Categories Present in R_CUDA but Absent from R_MERCURY

R_MERCURY omits the following R_CUDA relocation categories entirely:

| R_CUDA Category | Count | Reason for Omission |
|---|---|---|
| `R_CUDA_ABS*_<bitpos>` (instruction) | 25 | Mercury uses position-independent `R_MERCURY_ABS*` |
| `R_CUDA_PCREL_IMM24_*` | 2 | Replaced by `R_MERCURY_PROG_REL32/64` |
| `R_CUDA_CONST_FIELD*` | 10 | Constant bank references handled differently in Mercury |
| `R_CUDA_TEX_BINDLESSOFF*` / `R_CUDA_BINDLESSOFF*` | 7 | Bindless mechanism redesigned for Mercury ISA |
| `R_CUDA_TEX_SLOT` / `SAMP_SLOT` / `SURF_SLOT` | 3 | Mercury uses header index only, no slot numbers |
| `R_CUDA_SURF_HW_DESC` / `SURF_HW_SW_DESC` | 2 | Hardware descriptor types eliminated |
| `R_CUDA_INSTRUCTION64` / `INSTRUCTION128` | 2 | Whole-instruction replacement not used in Mercury |
| `R_CUDA_YIELD_*` | 2 | YIELD conversion handled differently (or absent) |
| `R_CUDA_UNUSED_CLEAR32` | 1 | Mercury only has 64-bit clear |
| `R_CUDA_QUERY_DESC21_37` | 1 | Query descriptor mechanism absent |
| `R_CUDA_6_31` / `R_CUDA_2_47` | 2 | Narrow-field types not needed |
| `R_CUDA_G32` | 1 | Mercury uses 64-bit global only |
| `R_CUDA_FUNC_DESC_32` / `FUNC_DESC32_*` | 10 | Mercury uses 64-bit descriptor + byte-level only |

### Categories Present in R_MERCURY but Absent from R_CUDA

| R_MERCURY Category | Count | Description |
|---|---|---|
| `R_MERCURY_PROG_REL64` | 1 | 64-bit PC-relative (CUDA has only 24-bit) |
| `R_MERCURY_PROG_REL32_LO/HI` | 2 | Split PC-relative (no CUDA equivalent) |
| `R_MERCURY_PROG_REL8_*` | 8 | Byte-level PC-relative (entirely new) |
| `R_MERCURY_ABS_PROG_REL*` | 4 | Hybrid absolute/PC-relative (entirely new) |

## Most Common Mercury Relocations

Based on the type semantics and their positions in the table, the following relocations appear most frequently in typical Mercury binaries:

### Kernel Code Relocations

| Relocation | Typical Use | Frequency |
|---|---|---|
| `R_MERCURY_PROG_REL32` (8) | Branch and call targets within a kernel | Very high — every `BRA`/`CALL`/`BRX` instruction |
| `R_MERCURY_ABS32` (3) | Immediate constant addresses in instruction fields | High — memory access instructions |
| `R_MERCURY_ABS32_LO` / `ABS32_HI` (5, 6) | Split 32-bit address loaded in two instructions (LO/HI pair) | High — `MOV32I` + `IADD3.X` address materialization |
| `R_MERCURY_G64` (1) | Global variable references | Medium — global load/store |
| `R_MERCURY_TEX_HEADER_INDEX` (11) | Texture fetch instructions | Medium — in texture-heavy kernels |
| `R_MERCURY_FUNC_DESC_64` (15) | Indirect call targets and virtual function pointers | Low — only in kernels using function pointers |

### Data Section Relocations

| Relocation | Typical Use | Frequency |
|---|---|---|
| `R_MERCURY_8_0` through `8_56` (16–23) | Patching individual bytes in constant bank initializers | High — one per byte of initialized pointer |
| `R_MERCURY_G8_0` through `G8_56` (24–31) | Global address bytes in descriptor tables | Medium |
| `R_MERCURY_FUNC_DESC_8_0` through `8_56` (32–39) | Function pointer bytes in vtable-like structures | Low |
| `R_MERCURY_ABS64` (2) | Full 64-bit address in data section | Medium |
| `R_MERCURY_UNUSED_CLEAR64` (14) | Zeroing unused descriptor entries after merge | Medium |

### Unified Table Relocations

| Relocation | Typical Use | Frequency |
|---|---|---|
| `R_MERCURY_UNIFIED_8_0` through `8_56` (52–59) | UFT/UDT table entries for dynamic parallelism | Low — only with CDP |
| `R_MERCURY_UNIFIED_32` (51) | 32-bit UFT/UDT offset | Low |
| `R_MERCURY_UNIFIED` (50) | Marker resolved to NONE before application | Low |

### Worked Example: Branch Relocation

A Mercury `BRA` instruction targeting function `_Z6kernelPf` generates a `R_MERCURY_PROG_REL32` relocation:

```text
Section .rela.text, offset 0x180:
  r_offset = 0x00000050    (byte offset of the BRA instruction in .text)
  r_info   = 0x0003 10008  (symbol index 3, type 0x10008 = R_MERCURY_PROG_REL32)
  r_addend = 0x00000000

The linker resolves:
  S = symbol_value(_Z6kernelPf) = 0x00000200
  A = r_addend = 0
  PC = section_base + r_offset = 0x00000050
  result = (S + A) - PC = 0x200 - 0x50 = 0x1B0

The descriptor for type 8 (PROG_REL32) specifies:
  Action 0: bit_offset=X, bit_width=32, action_type=0x10 (pc_rel)
  The engine writes 0x1B0 into bits [X:X+32) of the 128-bit instruction at offset 0x50.
```

### Worked Example: Byte-Level Data Patching

A global variable address `0x00007FFF12345678` must be written byte-by-byte into a descriptor table entry at offset 0x40:

```text
8 relocation entries, one per byte:
  R_MERCURY_G8_0   at offset 0x40: writes 0x78 (byte 0)
  R_MERCURY_G8_8   at offset 0x41: writes 0x56 (byte 1)
  R_MERCURY_G8_16  at offset 0x42: writes 0x34 (byte 2)
  R_MERCURY_G8_24  at offset 0x43: writes 0x12 (byte 3)
  R_MERCURY_G8_32  at offset 0x44: writes 0xFF (byte 4)
  R_MERCURY_G8_40  at offset 0x45: writes 0x7F (byte 5)
  R_MERCURY_G8_48  at offset 0x46: writes 0x00 (byte 6)
  R_MERCURY_G8_56  at offset 0x47: writes 0x00 (byte 7)

Each relocation uses a masked_shift action:
  G8_0 -> action_type=0x16, mask=0x00000000000000FF, shift=0
  G8_8 -> action_type=0x17, mask=0x000000000000FF00, shift=8
  ...
  G8_56 -> action_type=0x1D, mask=0xFF00000000000000, shift=56
```

## ELF Attribute Relocations

Mercury attribute relocations use the same `0x10000` offset mechanism within the attribute relocation table at `off_1D371E0`. When the relocation engine encounters a type >= `0x10000` in an attribute section (`.nv.info.*`), it subtracts `0x10000` and indexes into this table.

The attribute table has 65 entries (indices 0–64), validated with the limit check `type_index >= 0x41` in `sub_42F6C0`. The validation function at `sub_42F760` handles attribute-specific compatibility with a three-way dispatch:

```c
// sub_42F760 at 0x42F760 -- attribute validation
if (type_index > 0x60)       // > 96 -- guard against out-of-bounds
    error("unknown attribute");

if (dword_1D37D68[4 * type_index] > target_arch_class) {
    switch (dword_1D37D68[4 * type_index + 1]) {   // usage field
    case 0: warning("Attribute %s not supported on %s", name, class_name); break;
    case 1: error("Attribute %s not supported on %s", name, class_name); break;
    case 2: /* silent ignore */ break;
    default: error("unknown usage"); break;
    }
}
```

The EIATTR types specific to Mercury are:

| Attribute | Address | Description |
|---|---|---|
| `EIATTR_MERCURY_ISA_VERSION` | `0x1D36F31` | Mercury ISA version embedded in `.nv.info` |
| `EIATTR_MERCURY_FINALIZER_OPTIONS` | `0x1D37170` | Options passed to the Mercury finalizer (fnlzr) |

And the compatibility attributes:

| Attribute | Address | Description |
|---|---|---|
| `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | `0x245EF08` | Major.minor ISA version for compatibility checking |
| `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | `0x245EF38` | Patch-level ISA version |

These attributes are not relocation types but are processed alongside relocations during the `.nv.info` section handling phase. The `EIATTR_MERCURY_FINALIZER_OPTIONS` attribute is particularly important because it controls how the FNLZR processes relocations — it can enable or disable specific finalization passes that affect relocation resolution.

## Summary Table by Category

| Category | Indices | Count | Computation | Engine Action Types |
|---|---|---|---|---|
| Sentinel | 0, 64 | 2 | — | `0x00` (end) |
| Global data | 1 | 1 | `S + A` | `0x01` (abs_full) |
| Absolute data | 2–6 | 5 | `S + A` (full/lo/hi) | `0x01`, `0x06`, `0x07` |
| PC-relative | 7–10 | 4 | `(S + A) - PC` (full/lo/hi) | `0x10` (pc_rel), `0x37`, `0x38` |
| Texture/sampler/surface | 11–13 | 3 | Header index lookup | `0x01` with index computation |
| Clear | 14 | 1 | Write zeros | `0x13`/`0x14` (clear) |
| Function descriptor | 15 | 1 | `S + A` | `0x01` (abs_full) |
| Byte-level | 16–23 | 8 | `byte_n(S + A)` | `0x16`--`0x1D` (masked_shift) |
| Global byte-level | 24–31 | 8 | `byte_n(S + A)` | `0x16`--`0x1D` (masked_shift) |
| Func desc byte-level | 32–39 | 8 | `byte_n(S + A)` | `0x16`--`0x1D` (masked_shift) |
| Abs PC-relative (split) | 40–41 | 2 | `\|S + A - PC\|` lo/hi | `0x37`, `0x38` |
| PC-relative byte-level | 42–49 | 8 | `byte_n((S + A) - PC)` | `0x2F`--`0x36` (masked_shift) |
| Unified table | 50–59, 62–63 | 12 | `S + A` (full/lo/hi/byte) | `0x12`, `0x2E`, `0x06`, `0x07` |
| Abs PC-relative (full) | 60–61 | 2 | `\|S + A - PC\|` (32/64) | `0x01` with abs computation |
| **Total** | — | **65** | — | — |

## Function Addresses

| Function | Address | Size | Description |
|---|---|---|---|
| `sub_468760` | `0x468760` | 14,322 bytes | Relocation application engine (shared CUDA/Mercury) |
| `sub_469D60` | `0x469D60` | ~4,800 bytes | Relocation phase dispatcher (table selection, symbol resolution) |
| `sub_469B50` | `0x469B50` | ~600 bytes | Relocation record builder (linked-list entry creation) |
| `sub_42F6C0` | `0x42F6C0` | 916 bytes | Relocation type validation (shared CUDA/Mercury) |
| `sub_42F760` | `0x42F760` | 1,367 bytes | Attribute relocation validation (three-way dispatch) |
| `sub_468670` | `0x468670` | 882 bytes | Bitfield extraction helper (recursive multi-word) |
| `sub_4685B0` | `0x4685B0` | 961 bytes | Bitfield write helper (multi-word splice) |
| `sub_4683D0` | `0x4683D0` | 536 bytes | Descriptor table swap (save/restore table pointers) |
| `sub_4275C0` | `0x4275C0` | 3,989 bytes | FNLZR front-end dispatcher (pre/post-link mode) |
| `sub_4748F0` | `0x4748F0` | 8,950 bytes | FNLZR core engine (10-phase pipeline) |
| `sub_4AC380` | `0x4AC380` | ~2,000 bytes | Capmerc CLI option registration and self-check setup |
| `sub_45E7D0` | `0x45E7D0` | 89,156 bytes | Merge function (Mercury section skip logic) |

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| 65 unique R_MERCURY type names (indices 0–64) | **HIGH** | 71 R_MERCURY strings in `nvlink_strings.json` (65 unique names + 2 trailing-space duplicates + 4 EIATTR/EICOMPAT attributes). Exact count verified by string scan. |
| Name table at `off_1D371E0` (65 entries, addr range `0x1D35A17`--`0x1D35F4C`) | **HIGH** | All 65 type name strings verified at addresses within stated range. Xref from `R_MERCURY_NONE` at `0x1D35A17` confirms `0x1D371E0` is a pointer into this table. |
| Descriptor table at `off_1D3CBE0` (65 entries, 64 bytes each = 4,160 bytes) | **HIGH** | Table address `off_1D3CBE0` verified from decompiled `sub_469D60` (line 202: `v152 = &off_1D3CBE0`) and `sub_469B50` (line 76: `v35 = &off_1D3CBE0`). 64-byte entry size confirmed from `type_index << 6` indexing in `sub_468760`. |
| ELF type encoding: table index + `0x10000` | **HIGH** | Verified from decompiled `sub_469D60` at lines 197–203: `v148 = v9 - 0x10000` with guard `v8 <= 0x10000` producing `fatal("unexpected reloc")`. |
| ELF class byte `0x41` ('A') distinguishes Mercury from CUDA | **HIGH** | Verified from decompiled `sub_469D60` (line 190: `*(_BYTE *)(v2 + 7) != 65`), `sub_469B50` (line 40: `*(_BYTE *)(a1 + 7) == 65`), and `sub_4275C0` (line 40: `*(_BYTE *)(v14 + 7) != 65`). |
| Application engine `sub_468760` shared with R_CUDA | **HIGH** | Decompiled file `sub_468760_0x468760.c` (14,322 bytes). Called from `sub_469D60` line 414 with both `off_1D3CBE0` (Mercury) and `off_1D3DBE0` (CUDA) tables. |
| Relocation phase dispatcher `sub_469D60` performs table selection | **HIGH** | Decompiled code explicitly shows Mercury path (lines 196–208) selecting `off_1D3CBE0` and subtracting `0x10000`, vs CUDA path (lines 212–214) selecting `off_1D3DBE0`. |
| Record builder `sub_469B50` validates and creates reloc entries | **HIGH** | Decompiled code at lines 40–76 shows ELF class check, `sub_42F6C0` call, Mercury index normalization (`v9 - 0x10000`), and descriptor table action-type inspection. |
| Descriptor format: 12-byte header + 3x 16-byte actions + 4-byte sentinel | **HIGH** | 64-byte entry size verified from `<< 6` shift in `sub_468760`. Action pointer starts at `+12` (line 130: `v15 = (v12 + 12)`) and sentinel at `+60` (line 132: `v100 = (v12 + 60)`). Three action slots of 4 x uint32 = 16 bytes each confirmed by `v15 += 4` increment per action. |
| Action type dispatch (0x00, 0x01, 0x06, 0x07, 0x10, 0x13, 0x16–0x1D, etc.) | **HIGH** | All action type codes verified from the switch statement in `sub_468760` (lines 137–580). Case labels match the documented table exactly. |
| R_MERCURY vs R_CUDA comparison table | **HIGH** | Type-by-type comparison verified from both name tables in `nvlink_strings.json`. Semantic equivalence claims based on identical descriptor action patterns for matching types. |
| Categories omitted from R_MERCURY (25 instruction-specific, CONST_FIELD, bindless, etc.) | **HIGH** | Absence verified by complete enumeration of all 65 R_MERCURY names. No `R_MERCURY_CONST_FIELD`, `R_MERCURY_BINDLESS`, `R_MERCURY_INSTRUCTION64/128`, etc. exist in the string table. |
| Mercury-specific types: PROG_REL64, PROG_REL32_LO/HI, PROG_REL8_*, ABS_PROG_REL* | **HIGH** | All type names verified in `nvlink_strings.json`. No corresponding `R_CUDA_PROG_REL64`, `R_CUDA_PROG_REL8_*`, or `R_CUDA_ABS_PROG_REL*` exist. |
| Trailing-space variants at `0x1D3CB71` and `0x1D3CB88` | **HIGH** | Two strings with trailing spaces verified in string table at exact addresses. Located within the descriptor table region (between `off_1D3CBE0` and `off_1D3DBE0`). |
| UFT_OFFSET symbol string comparison at sub_469D60 lines 377–395 | **HIGH** | Decompiled code shows 13-byte `strncmp` against `"__UFT_OFFSET"` with debug trace `"ignore reloc on UFT_OFFSET\n"`. |
| PC-relative same-section validation at sub_469D60 line 409–410 | **HIGH** | Decompiled code: `descriptor[5] == 16` (action_type pc_rel) triggers `"PC relative branch address should be in the same section"`. |
| Dead-function reloc elimination trace | **HIGH** | Decompiled code at sub_469D60 line 349: `fprintf(stderr, "ignore reloc on dead func %s\n", ...)`. |
| YIELD conversion suppression for forward-progress | **HIGH** | Decompiled code at sub_469D60 lines 495–504: checks `context + 94` flag, emits 80-byte YIELD message. |
| Self-check strings at `0x2458F38`--`0x2458FE8` | **HIGH** | All 4 self-check strings verified at exact addresses in `nvlink_strings.json` with xrefs to `0x24590A0`--`0x24590B8` pointer array. |
| Third name table copy at `0x2459160` | **HIGH** | R_MERCURY_NONE xref list includes `0x2459160` alongside `0x1D371E0` and `0x1D3CBE0`. Located in capmerc uplift code region. |
| MERCSW-125 Jira reference | **HIGH** | String at `0x1F44288` contains full text including `"Jira confluence page 'MERCSW-125'"`. |
| Attribute validation function `sub_42F760` three-way dispatch | **HIGH** | Decompiled code shows `dword_1D37D68[4 * a1 + 1]` value 0=warning, 1=error, 2=ignore, with guard `a1 > 0x60`. |
| `sub_4AC380` capmerc option registration | **HIGH** | Decompiled code shows `sub_42F130` calls registering `--self-check`, `--binary-kind`, `--cap-merc`, `--out-sass`, `--compile-as-at-entry-patch`, `--opportunistic-finalization-lvl`. |
| `R_MERCURY_UNIFIED32_LO/HI` added later than main unified block | **LOW** | Inferred from non-contiguous index placement (indices 62–63 vs 50–59). No version history available to confirm chronology. |
| `R_MERCURY_ABS_PROG_REL32_LO/HI` added before full-width variants | **LOW** | Same reasoning: non-contiguous indices (40–41 vs 60–61). Chronological ordering is speculative. |
| Action types not used by Mercury (`0x08`, `0x0A`, `0x0B`) | **MEDIUM** | Inferred from the absence of CONST_FIELD and symbol-size relocation types in R_MERCURY. Not verified by exhaustive descriptor table dumping. |

## Cross-References

### nvlink Internal
- [Mercury Overview](overview.md) — Mercury architecture and string evidence
- [Mercury ELF Sections](elf-sections.md) — `.nv.merc.rela` section that carries these relocations
- [Capsule Mercury Format](capmerc-format.md) — capmerc container format
- [FNLZR](fnlzr.md) — finalizer that applies Mercury relocations (`sub_4275C0`, `sub_4748F0`)
- [R_CUDA Relocations](../linker/r-cuda-relocations.md) — the 119 R_CUDA relocation types (shared engine, same descriptor format)
- [R_MERCURY Catalog](../reference/r-mercury-catalog.md) — pure reference listing of all 65 types with string addresses
- [R_CUDA Catalog](../reference/r-cuda-catalog.md) — companion CUDA catalog for cross-reference
- [Relocation Engine](../linker/r-cuda-relocations.md#descriptor-format) — shared `sub_468760` engine pseudocode and action type table
- [Section Merging](../linker/section-merging.md) — relocation processing during merge (Mercury skip logic)
- [Bindless Relocations](../linker/bindless-relocations.md) — bindless texture mechanism (absent from R_MERCURY)
- [UFT](../elf/uft.md) — Unified Function Table referenced by `R_MERCURY_UNIFIED_*` types
- [Function Map](../function-map.md) — master function address index

### Sibling Wikis
- [ptxas: Capsule Mercury & Finalization](../../ptxas/codegen/capmerc.html) — standalone ptxas capmerc relocation emission (Mercury rela entry format, relocation table layout)
