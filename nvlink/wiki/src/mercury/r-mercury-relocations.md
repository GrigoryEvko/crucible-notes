# R_MERCURY Relocations

nvlink defines 65 Mercury-specific ELF relocation types for the capsule Mercury (capmerc) binary format, used on sm100+ architectures (Blackwell and later). These types are stored in `.rela.*` sections of capmerc ELF files and are consumed by the same relocation application engine that handles R_CUDA types, but through a separate descriptor table. Each R_MERCURY type in the ELF is encoded as the table index plus `0x10000` -- the relocation engine subtracts this offset at dispatch time to index into the Mercury-specific descriptor table.

Mercury relocations are structurally simpler than their R_CUDA counterparts. Where R_CUDA has 119 types covering six generations of SASS instruction encoding (each with different bit-field positions for the same logical operation), R_MERCURY has 65 types that target a single 128-bit instruction format. The R_MERCURY set eliminates the per-bit-position variants (`R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32`) in favor of position-independent types (`R_MERCURY_ABS32`) -- the bit-field position is encoded in the 64-byte descriptor rather than in the type name.

## Key Facts

| Property | Value |
|---|---|
| Machine type | `EM_CUDA` (190) with ELF class byte `0x41` (`'A'`) |
| Total unique type names | 65 (indices 0--64) |
| ELF type encoding | Table index + `0x10000` |
| Mercury name table | `off_1D371E0` (65 entries, pointers at string addresses `0x1D35A17`--`0x1D35F4C`) |
| Mercury descriptor table | `off_1D3CBE0` (65 entries, 64 bytes each = 4,160 bytes) |
| Descriptor size | 64 bytes per type (3 actions x 16 bytes + 12-byte header) |
| Application engine | `sub_468760` at `0x468760` (shared with R_CUDA) |
| Validation function | `sub_42F6C0` at `0x42F6C0` (shared with R_CUDA) |
| Table limit check | Index < `0x41` (65) for attribute relocation path |
| Architecture scope | sm100 (Blackwell), sm103 (Blackwell Ultra), sm110, sm120, sm121 |

## ELF Type Encoding and Dispatch

When the linker loads a relocation entry from a capmerc ELF file, the `r_info` type field contains the Mercury type index plus `0x10000`. The dispatch logic in the relocation engine detects this offset and routes to the Mercury descriptor table:

```c
if (ctx->elf_class == 'A') {              // Mercury / capmerc ELF
    if (reloc_type <= 0x10000)
        fatal("unexpected reloc");         // Mercury types must be >= 0x10001
    reloc_type -= 0x10000;
    descriptor_table = off_1D3CBE0;        // Mercury descriptor table
} else {                                   // CUDA / traditional cubin
    descriptor_table = off_1D3DBE0;        // CUDA descriptor table
}
```

The `0x10000` namespace separation ensures that R_MERCURY and R_CUDA type numbers never collide. A raw ELF type value of `0x10000` maps to Mercury index 0 (`R_MERCURY_NONE`); `0x10001` maps to index 1 (`R_MERCURY_G64`); and so on through `0x10040` for index 64 (`R_MERCURY_NONE_LAST`).

The ELF class byte at offset `+7` of the linker context distinguishes Mercury from CUDA. The value `0x41` (ASCII `'A'`) indicates a Mercury ELF, while other values indicate traditional CUDA cubins.

## Naming Convention

R_MERCURY type names follow a simpler pattern than R_CUDA:

```
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

`R_MERCURY_NONE` at index 0 serves the same purpose as `R_CUDA_NONE`: it is a no-op relocation used for entries that have been eliminated by dead code removal or relocation merging. The application engine returns immediately when it encounters a descriptor with all-zero action types. `R_MERCURY_NONE_LAST` at index 64 is a bounds sentinel -- the validation function rejects any type index >= 65.

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

Unlike R_CUDA, which has separate types for each bit-position within the instruction word (`R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32`), the Mercury types are position-independent -- the bit-field offset is stored in the 64-byte descriptor entry.

### PC-Relative Relocations

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 7 | `0x10007` | `R_MERCURY_PROG_REL64` | 64 | 64-bit PC-relative offset |
| 8 | `0x10008` | `R_MERCURY_PROG_REL32` | 32 | 32-bit PC-relative offset |
| 9 | `0x10009` | `R_MERCURY_PROG_REL32_LO` | 16 | Low 16 bits of 32-bit PC-relative |
| 10 | `0x1000A` | `R_MERCURY_PROG_REL32_HI` | 16 | High 16 bits of 32-bit PC-relative |

PC-relative relocations compute `(S + A) - PC`, where `PC` is the address of the instruction being patched. These are used for branch instructions (`BRA`, `CALL`, `BRX`) in Mercury's 128-bit instruction format.

The 64-bit variant (`PROG_REL64`) supports the full address space. The 32-bit variant limits branch distances to +/- 2 GB, sufficient for all practical kernel sizes. The `_LO`/`_HI` split variants handle cases where the PC-relative offset must be encoded in two separate instruction fields.

### Texture, Sampler, and Surface Relocations

| Index | ELF Type | Name | Description |
|---|---|---|---|
| 11 | `0x1000B` | `R_MERCURY_TEX_HEADER_INDEX` | Texture header table index |
| 12 | `0x1000C` | `R_MERCURY_SAMP_HEADER_INDEX` | Sampler header table index |
| 13 | `0x1000D` | `R_MERCURY_SURF_HEADER_INDEX` | Surface header table index |

These relocations resolve bindable resource references -- texture, sampler, and surface objects -- by patching the merged header table index into instruction fields. During linking, individual per-module header tables are merged into a single global table; these relocations update the instruction operands to reference the correct entry in the merged table.

These are direct equivalents of `R_CUDA_TEX_HEADER_INDEX`, `R_CUDA_SAMP_HEADER_INDEX`, and `R_CUDA_SURF_HEADER_INDEX`. Mercury drops the `SLOT`, `HW_DESC`, and `HW_SW_DESC` variants that exist in R_CUDA, consolidating texture resource binding into the header index mechanism alone.

### Clear Relocation

| Index | ELF Type | Name | Bits | Description |
|---|---|---|---|---|
| 14 | `0x1000E` | `R_MERCURY_UNUSED_CLEAR64` | 64 | Zero 64 bits (clear unused field) |

Writes zeros to a 64-bit field. Used to clear placeholder entries in merged data sections or nullify unused instruction fields after linking. Equivalent to `R_CUDA_UNUSED_CLEAR64`.

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

Byte-granularity relocations for global memory addresses. These are the byte-level counterparts of `R_MERCURY_G64` -- instead of writing a full 64-bit global address, they write a single byte of the address at a specific position. Used when a global address must be assembled byte-by-byte in a data structure.

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

The `_LO`/`_HI` split variants (indices 40--41) appear earlier in the table than the full-width variants (indices 60--61), suggesting they were added in an earlier revision and the full-width types were appended later.

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

`R_MERCURY_UNIFIED` (index 50) is a generic marker type, not a data-patching relocation. During the relocation phase, unified relocations targeting synthetic symbols (`__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UDT`, `__UFT`, `__UFT_END`, `__UDT_END`) are resolved to type 0 (no-op) because the unified table manager computes final offsets before the relocation engine runs.

The `UNIFIED32_LO` and `UNIFIED32_HI` types (indices 62--63) appear after the `ABS_PROG_REL` types in the table rather than adjacent to the other unified types (50--59). This suggests they were added in a later revision.

Two additional trailing-space variants (`"R_MERCURY_UNIFIED_8_0 "` and `"R_MERCURY_UNIFIED_8_8 "`) appear at separate string addresses (`0x1D3CB71` and `0x1D3CB88`) in the Mercury descriptor table region. These are duplicate name strings used by the descriptor table entries themselves, distinct from the primary name table entries (which lack the trailing space). The trailing space is a formatting artifact in the binary's `.rodata`, not a distinct relocation type.

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
| `R_MERCURY_PROG_REL64` | 7 | -- | No direct CUDA equivalent |
| `R_MERCURY_PROG_REL32` | 8 | `R_CUDA_PCREL_IMM24_*` | CUDA uses 24-bit; Mercury uses 32-bit |
| `R_MERCURY_PROG_REL32_LO` | 9 | -- | Mercury-specific |
| `R_MERCURY_PROG_REL32_HI` | 10 | -- | Mercury-specific |
| `R_MERCURY_TEX_HEADER_INDEX` | 11 | `R_CUDA_TEX_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_SAMP_HEADER_INDEX` | 12 | `R_CUDA_SAMP_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_SURF_HEADER_INDEX` | 13 | `R_CUDA_SURF_HEADER_INDEX` | Identical semantics |
| `R_MERCURY_UNUSED_CLEAR64` | 14 | `R_CUDA_UNUSED_CLEAR64` | Identical semantics |
| `R_MERCURY_FUNC_DESC_64` | 15 | `R_CUDA_FUNC_DESC_64` | Identical semantics |
| `R_MERCURY_8_*` | 16--23 | `R_CUDA_8_*` | Identical semantics |
| `R_MERCURY_G8_*` | 24--31 | `R_CUDA_G8_*` | Identical semantics |
| `R_MERCURY_FUNC_DESC_8_*` | 32--39 | `R_CUDA_FUNC_DESC_8_*` | Identical semantics |
| `R_MERCURY_ABS_PROG_REL32_LO` | 40 | -- | Mercury-specific |
| `R_MERCURY_ABS_PROG_REL32_HI` | 41 | -- | Mercury-specific |
| `R_MERCURY_PROG_REL8_*` | 42--49 | -- | Mercury-specific (no CUDA byte-level PC-rel) |
| `R_MERCURY_UNIFIED` | 50 | `R_CUDA_UNIFIED` | Identical semantics |
| `R_MERCURY_UNIFIED_32` | 51 | `R_CUDA_UNIFIED_32` | Identical semantics |
| `R_MERCURY_UNIFIED_8_*` | 52--59 | `R_CUDA_UNIFIED_8_*` | Identical semantics |
| `R_MERCURY_ABS_PROG_REL32` | 60 | -- | Mercury-specific |
| `R_MERCURY_ABS_PROG_REL64` | 61 | -- | Mercury-specific |
| `R_MERCURY_UNIFIED32_LO` | 62 | `R_CUDA_UNIFIED32_LO_32` | CUDA includes bit-position |
| `R_MERCURY_UNIFIED32_HI` | 63 | `R_CUDA_UNIFIED32_HI_32` | CUDA includes bit-position |
| `R_MERCURY_NONE_LAST` | 64 | `R_CUDA_NONE_LAST` | Identical semantics |

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

## Descriptor Table Structure

The Mercury descriptor table at `off_1D3CBE0` has the same 64-byte-per-entry format as the CUDA table at `off_1D3DBE0`. Each entry encodes up to three patching actions:

```
Entry (64 bytes):
  +0   header[12]            // type metadata (name pointer, flags)
  +12  action[0] (16 bytes)  // first patching action
  +28  action[1] (16 bytes)  // second patching action
  +44  action[2] (16 bytes)  // third patching action
  +60  sentinel (4 bytes)    // end marker

Action (16 bytes):
  +0   uint32_t  bit_offset    // start bit in instruction/data word
  +4   uint32_t  bit_width     // number of bits to patch
  +8   uint32_t  action_type   // operation code
  +12  uint32_t  reserved      // flags / padding
```

Although the table format is identical, the descriptor contents differ from R_CUDA because Mercury's 128-bit instruction words have different field layouts. A relocation that places a 32-bit absolute address at bit position 20 in a 64-bit CUDA instruction will place it at a different bit position in a 128-bit Mercury instruction -- and this difference is encoded in the descriptor's `bit_offset` field, not in the type name.

The shared application engine `sub_468760` is type-agnostic: it simply reads the descriptor, executes the action sequence, and patches the bit fields. The distinction between CUDA and Mercury is entirely in which descriptor table is selected at dispatch time.

## ELF Attribute Relocations

Mercury attribute relocations use the same `0x10000` offset mechanism within the attribute relocation table at `off_1D371E0`. When the relocation engine encounters a type >= `0x10000` in an attribute section (`.nv.info.*`), it subtracts `0x10000` and indexes into this table.

The attribute table has 65 entries (indices 0--64), validated with the limit check `type_index >= 0x41`. Mercury and CUDA share the same attribute relocation table because attribute sections use the same format regardless of instruction encoding.

The EIATTR types specific to Mercury are:

| Attribute | Description |
|---|---|
| `EIATTR_MERCURY_ISA_VERSION` | Mercury ISA version embedded in `.nv.info` |
| `EIATTR_MERCURY_FINALIZER_OPTIONS` | Options passed to the Mercury finalizer (fnlzr) |

And the compatibility attributes:

| Attribute | Description |
|---|---|
| `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | Major.minor ISA version for compatibility checking |
| `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | Patch-level ISA version |

These attributes are not relocation types but are processed alongside relocations during the `.nv.info` section handling phase.

## Self-Check Infrastructure

nvlink includes a self-check mechanism specifically for Mercury relocations. When `--self-check` is active, the linker verifies that Mercury relocation application produces correct results by comparing the patched output against expected values. The diagnostic messages are:

| Message | Condition |
|---|---|
| `"Self check for capsule mercury (capmerc)"` | Self-check initiated for Mercury output |
| `"Self check for capsule mercury text section failed"` | `.text` section mismatch after relocation |
| `"Self check for capsule mercury debug section failed"` | Debug section mismatch |
| `"Self check for capsule mercury relocation section failed"` | `.rela` section mismatch |
| `"Failure of '%s' section in self-check for capsule mercury"` | Generic section failure (with MERCSW-125 reference) |
| `"Invalid elf provided for mercury uplift."` | Bad ELF input for Mercury conversion |
| `"skip mercury section %i\n"` | Debug trace: skipping a Mercury-specific section (function `sub_45E7D0`) |

## Summary Table by Category

| Type | Indices | Count | Computation | Description |
|---|---|---|---|---|
| Sentinel | 0, 64 | 2 | -- | No-op / bounds marker |
| Global data | 1 | 1 | `S + A` | Global memory addresses |
| Absolute data | 2--6 | 5 | `S + A` (full/lo/hi) | Data section addresses |
| PC-relative | 7--10 | 4 | `(S + A) - PC` (full/lo/hi) | Branch / call targets |
| Texture/sampler/surface | 11--13 | 3 | Header index lookup | Bindable resource references |
| Clear | 14 | 1 | Write zeros | Unused field cleanup |
| Function descriptor | 15 | 1 | `S + A` | Indirect call targets |
| Byte-level | 16--23 | 8 | `byte_n(S + A)` | Byte-granular data patching |
| Global byte-level | 24--31 | 8 | `byte_n(S + A)` | Global address byte patching |
| Func desc byte-level | 32--39 | 8 | `byte_n(S + A)` | Descriptor byte patching |
| Abs PC-relative (split) | 40--41 | 2 | `\|S + A - PC\|` lo/hi | Unsigned distance fields |
| PC-relative byte-level | 42--49 | 8 | `byte_n((S + A) - PC)` | PC-relative byte patching |
| Unified table | 50--59, 62--63 | 12 | `S + A` (full/lo/hi/byte) | UDT/UFT table references |
| Abs PC-relative (full) | 60--61 | 2 | `\|S + A - PC\|` (32/64) | Unsigned distance full-width |
| **Total** | — | **65** | — | — |

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| 65 unique R_MERCURY type names (indices 0--64) | **HIGH** | 71 R_MERCURY strings in `nvlink_strings.json` (65 unique names + 6 duplicate trailing-space variants). Exact count verified by string scan. |
| Name table at `off_1D371E0` (65 entries, addr range `0x1D35A17`--`0x1D35F4C`) | **HIGH** | All 65 type name strings verified at addresses within stated range. Each string has xrefs to the relocation engine. |
| Descriptor table at `off_1D3CBE0` (65 entries, 64 bytes each = 4,160 bytes) | **HIGH** | Table address and entry size verified from decompiled `sub_468760` (relocation application engine). 64-byte entry format confirmed from descriptor indexing arithmetic. |
| ELF type encoding: table index + `0x10000` | **HIGH** | Verified from decompiled relocation dispatch logic in `sub_468760`. The `0x10000` offset subtraction is explicit in the code. |
| ELF class byte `0x41` ('A') distinguishes Mercury from CUDA | **HIGH** | Verified from decompiled `sub_4275C0` and `sub_4748F0`. The `0x41` check at `hdr+7` gates Mercury-specific code paths. |
| Application engine `sub_468760` shared with R_CUDA | **HIGH** | Decompiled file `sub_468760_0x468760.c` exists. Dual descriptor table selection (Mercury vs CUDA) confirmed in code. |
| Descriptor format: 12-byte header + 3x 16-byte actions + 4-byte sentinel | **MEDIUM** | 64-byte entry size verified. Internal field layout (bit_offset, bit_width, action_type) inferred from decompiled action execution code. Exact header/sentinel structure is derived from field access patterns. |
| R_MERCURY vs R_CUDA comparison table | **HIGH** | Type-by-type comparison verified from both name tables in `nvlink_strings.json`. Semantic equivalence claims based on identical descriptor action patterns for matching types. |
| Categories omitted from R_MERCURY (25 instruction-specific, CONST_FIELD, bindless, etc.) | **HIGH** | Absence verified by complete enumeration of all 65 R_MERCURY names. No `R_MERCURY_CONST_FIELD`, `R_MERCURY_BINDLESS`, `R_MERCURY_INSTRUCTION64/128`, etc. exist in the string table. |
| Mercury-specific types: PROG_REL64, PROG_REL32_LO/HI, PROG_REL8_*, ABS_PROG_REL* | **HIGH** | All type names verified in `nvlink_strings.json`. No corresponding `R_CUDA_PROG_REL64`, `R_CUDA_PROG_REL8_*`, or `R_CUDA_ABS_PROG_REL*` exist. |
| Trailing-space variants at `0x1D3CB71` and `0x1D3CB88` | **MEDIUM** | Two strings with trailing spaces verified in string table. Interpretation as formatting artifacts is inferred (not confirmed by code analysis). |
| Attribute relocation table limit check `type_index >= 0x41` | **HIGH** | Verified from decompiled validation function `sub_42F6C0`. The `0x41` (65) limit is explicit. |
| EIATTR_MERCURY_ISA_VERSION and EIATTR_MERCURY_FINALIZER_OPTIONS | **HIGH** | Both attribute name strings verified at `0x1D36F31` and `0x1D37170` in `nvlink_strings.json`. |
| Self-check infrastructure strings | **HIGH** | All 7 self-check/diagnostic strings verified at exact addresses. |
| Unified table synthetic symbols (`__UFT_OFFSET`, `__UDT_OFFSET`, etc.) | **MEDIUM** | Symbol names verified from decompiled relocation processing code. The "resolved to type 0" behavior is inferred from code flow analysis. |
| `R_MERCURY_UNIFIED32_LO/HI` added later than main unified block | **LOW** | Inferred from non-contiguous index placement (indices 62--63 vs 50--59). No version history available to confirm chronology. |
| `R_MERCURY_ABS_PROG_REL32_LO/HI` added before full-width variants | **LOW** | Same reasoning: non-contiguous indices (40--41 vs 60--61). Chronological ordering is speculative. |

## Cross-References

### nvlink Internal
- [Mercury Overview](overview.md) -- Mercury architecture and string evidence
- [Mercury ELF Sections](elf-sections.md) -- `.nv.merc.rela` section that carries these relocations
- [Capsule Mercury Format](capmerc-format.md) -- capmerc container format
- [FNLZR](fnlzr.md) -- finalizer that applies Mercury relocations
- [R_CUDA Relocations](../linker/r-cuda-relocations.md) -- the 119 R_CUDA relocation types (comparable structure)
- [Section Merging](../linker/section-merging.md) -- relocation processing during merge

### Sibling Wikis
- [ptxas: Capsule Mercury & Finalization](../../../../ptxas/wiki/src/codegen/capmerc.md) -- standalone ptxas capmerc relocation emission (Mercury rela entry format, relocation table layout)
