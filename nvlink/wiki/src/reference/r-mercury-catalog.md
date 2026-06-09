# R_MERCURY Relocation Catalog

Complete reference listing of all 65 R_MERCURY relocation types defined in nvlink v13.0.88 for the capsule Mercury (capmerc) binary format. The binary contains 71 total `R_MERCURY`/`MERCURY`-bearing strings: 65 relocation type names, 2 trailing-space formatting duplicates, and 4 `.nv.info` attribute names — all documented below. For semantic details, descriptor table structure, and dispatch logic, see [R_MERCURY Relocations](../mercury/r-mercury-relocations.md). For the CUDA counterpart catalog, see [R_CUDA Relocation Catalog](r-cuda-catalog.md).

## Encoding

Every R_MERCURY type is stored in ELF `r_info` as `index + 0x10000`. The relocation engine subtracts `0x10000` at dispatch time to index into the Mercury descriptor table at `off_1D3CBE0`. The name table lives at `off_1D371E0` with 65 string pointers. The actual name strings occupy a contiguous pool at `0x1D35A17`--`0x1D35F5F` (1,353 bytes including null terminators).

## Complete Type Table

65 entries, sorted by index. The "String Address" column gives the `.rodata` address of each name string as found in `nvlink_strings.json`.

| Index | ELF Value | Name | String Address | Category | R_CUDA Equivalent |
|------:|----------:|------|---------------:|----------|-------------------|
| 0 | `0x10000` | `R_MERCURY_NONE` | `0x1D35A17` | Sentinel | `R_CUDA_NONE` |
| 1 | `0x10001` | `R_MERCURY_G64` | `0x1D35A26` | Global address | `R_CUDA_G64` |
| 2 | `0x10002` | `R_MERCURY_ABS64` | `0x1D35A34` | Absolute data | `R_CUDA_64` |
| 3 | `0x10003` | `R_MERCURY_ABS32` | `0x1D35A44` | Absolute data | `R_CUDA_32` |
| 4 | `0x10004` | `R_MERCURY_ABS16` | `0x1D35A54` | Absolute data | `R_CUDA_ABS16_*` (4 types) |
| 5 | `0x10005` | `R_MERCURY_ABS32_LO` | `0x1D35A64` | Absolute data | `R_CUDA_ABS32_LO_*` (4 types) |
| 6 | `0x10006` | `R_MERCURY_ABS32_HI` | `0x1D35A77` | Absolute data | `R_CUDA_ABS32_HI_*` (4 types) |
| 7 | `0x10007` | `R_MERCURY_PROG_REL64` | `0x1D35A8A` | PC-relative | — (Mercury-specific) |
| 8 | `0x10008` | `R_MERCURY_PROG_REL32` | `0x1D35A9F` | PC-relative | `R_CUDA_PCREL_IMM24_*` (24-bit) |
| 9 | `0x10009` | `R_MERCURY_PROG_REL32_LO` | `0x1D35AB4` | PC-relative | — (Mercury-specific) |
| 10 | `0x1000A` | `R_MERCURY_PROG_REL32_HI` | `0x1D35ACC` | PC-relative | — (Mercury-specific) |
| 11 | `0x1000B` | `R_MERCURY_TEX_HEADER_INDEX` | `0x1D35AE4` | Texture/sampler/surface | `R_CUDA_TEX_HEADER_INDEX` |
| 12 | `0x1000C` | `R_MERCURY_SAMP_HEADER_INDEX` | `0x1D35AFF` | Texture/sampler/surface | `R_CUDA_SAMP_HEADER_INDEX` |
| 13 | `0x1000D` | `R_MERCURY_SURF_HEADER_INDEX` | `0x1D35B1B` | Texture/sampler/surface | `R_CUDA_SURF_HEADER_INDEX` |
| 14 | `0x1000E` | `R_MERCURY_UNUSED_CLEAR64` | `0x1D35B37` | Clear | `R_CUDA_UNUSED_CLEAR64` |
| 15 | `0x1000F` | `R_MERCURY_FUNC_DESC_64` | `0x1D35B50` | Function descriptor | `R_CUDA_FUNC_DESC_64` |
| 16 | `0x10010` | `R_MERCURY_8_0` | `0x1D35B67` | Byte-level | `R_CUDA_8_0` |
| 17 | `0x10011` | `R_MERCURY_8_8` | `0x1D35B75` | Byte-level | `R_CUDA_8_8` |
| 18 | `0x10012` | `R_MERCURY_8_16` | `0x1D35B83` | Byte-level | `R_CUDA_8_16` |
| 19 | `0x10013` | `R_MERCURY_8_24` | `0x1D35B92` | Byte-level | `R_CUDA_8_24` |
| 20 | `0x10014` | `R_MERCURY_8_32` | `0x1D35BA1` | Byte-level | `R_CUDA_8_32` |
| 21 | `0x10015` | `R_MERCURY_8_40` | `0x1D35BB0` | Byte-level | `R_CUDA_8_40` |
| 22 | `0x10016` | `R_MERCURY_8_48` | `0x1D35BBF` | Byte-level | `R_CUDA_8_48` |
| 23 | `0x10017` | `R_MERCURY_8_56` | `0x1D35BCE` | Byte-level | `R_CUDA_8_56` |
| 24 | `0x10018` | `R_MERCURY_G8_0` | `0x1D35BDD` | Global byte-level | `R_CUDA_G8_0` |
| 25 | `0x10019` | `R_MERCURY_G8_8` | `0x1D35BEC` | Global byte-level | `R_CUDA_G8_8` |
| 26 | `0x1001A` | `R_MERCURY_G8_16` | `0x1D35BFB` | Global byte-level | `R_CUDA_G8_16` |
| 27 | `0x1001B` | `R_MERCURY_G8_24` | `0x1D35C0B` | Global byte-level | `R_CUDA_G8_24` |
| 28 | `0x1001C` | `R_MERCURY_G8_32` | `0x1D35C1B` | Global byte-level | `R_CUDA_G8_32` |
| 29 | `0x1001D` | `R_MERCURY_G8_40` | `0x1D35C2B` | Global byte-level | `R_CUDA_G8_40` |
| 30 | `0x1001E` | `R_MERCURY_G8_48` | `0x1D35C3B` | Global byte-level | `R_CUDA_G8_48` |
| 31 | `0x1001F` | `R_MERCURY_G8_56` | `0x1D35C4B` | Global byte-level | `R_CUDA_G8_56` |
| 32 | `0x10020` | `R_MERCURY_FUNC_DESC_8_0` | `0x1D35C5B` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_0` |
| 33 | `0x10021` | `R_MERCURY_FUNC_DESC_8_8` | `0x1D35C73` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_8` |
| 34 | `0x10022` | `R_MERCURY_FUNC_DESC_8_16` | `0x1D35C8B` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_16` |
| 35 | `0x10023` | `R_MERCURY_FUNC_DESC_8_24` | `0x1D35CA4` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_24` |
| 36 | `0x10024` | `R_MERCURY_FUNC_DESC_8_32` | `0x1D35CBD` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_32` |
| 37 | `0x10025` | `R_MERCURY_FUNC_DESC_8_40` | `0x1D35CD6` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_40` |
| 38 | `0x10026` | `R_MERCURY_FUNC_DESC_8_48` | `0x1D35CEF` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_48` |
| 39 | `0x10027` | `R_MERCURY_FUNC_DESC_8_56` | `0x1D35D08` | Func desc byte-level | `R_CUDA_FUNC_DESC_8_56` |
| 40 | `0x10028` | `R_MERCURY_ABS_PROG_REL32_LO` | `0x1D35D21` | Abs PC-relative | — (Mercury-specific) |
| 41 | `0x10029` | `R_MERCURY_ABS_PROG_REL32_HI` | `0x1D35D3D` | Abs PC-relative | — (Mercury-specific) |
| 42 | `0x1002A` | `R_MERCURY_PROG_REL8_0` | `0x1D35D59` | PC-rel byte-level | — (Mercury-specific) |
| 43 | `0x1002B` | `R_MERCURY_PROG_REL8_8` | `0x1D35D6F` | PC-rel byte-level | — (Mercury-specific) |
| 44 | `0x1002C` | `R_MERCURY_PROG_REL8_16` | `0x1D35D85` | PC-rel byte-level | — (Mercury-specific) |
| 45 | `0x1002D` | `R_MERCURY_PROG_REL8_24` | `0x1D35D9C` | PC-rel byte-level | — (Mercury-specific) |
| 46 | `0x1002E` | `R_MERCURY_PROG_REL8_32` | `0x1D35DB3` | PC-rel byte-level | — (Mercury-specific) |
| 47 | `0x1002F` | `R_MERCURY_PROG_REL8_40` | `0x1D35DCA` | PC-rel byte-level | — (Mercury-specific) |
| 48 | `0x10030` | `R_MERCURY_PROG_REL8_48` | `0x1D35DE1` | PC-rel byte-level | — (Mercury-specific) |
| 49 | `0x10031` | `R_MERCURY_PROG_REL8_56` | `0x1D35DF8` | PC-rel byte-level | — (Mercury-specific) |
| 50 | `0x10032` | `R_MERCURY_UNIFIED` | `0x1D35E0F` | Unified table | `R_CUDA_UNIFIED` |
| 51 | `0x10033` | `R_MERCURY_UNIFIED_32` | `0x1D35E21` | Unified table | `R_CUDA_UNIFIED_32` |
| 52 | `0x10034` | `R_MERCURY_UNIFIED_8_0` | `0x1D35E36` | Unified byte-level | `R_CUDA_UNIFIED_8_0` |
| 53 | `0x10035` | `R_MERCURY_UNIFIED_8_8` | `0x1D35E4C` | Unified byte-level | `R_CUDA_UNIFIED_8_8` |
| 54 | `0x10036` | `R_MERCURY_UNIFIED_8_16` | `0x1D35E62` | Unified byte-level | `R_CUDA_UNIFIED_8_16` |
| 55 | `0x10037` | `R_MERCURY_UNIFIED_8_24` | `0x1D35E79` | Unified byte-level | `R_CUDA_UNIFIED_8_24` |
| 56 | `0x10038` | `R_MERCURY_UNIFIED_8_32` | `0x1D35E90` | Unified byte-level | `R_CUDA_UNIFIED_8_32` |
| 57 | `0x10039` | `R_MERCURY_UNIFIED_8_40` | `0x1D35EA7` | Unified byte-level | `R_CUDA_UNIFIED_8_40` |
| 58 | `0x1003A` | `R_MERCURY_UNIFIED_8_48` | `0x1D35EBE` | Unified byte-level | `R_CUDA_UNIFIED_8_48` |
| 59 | `0x1003B` | `R_MERCURY_UNIFIED_8_56` | `0x1D35ED5` | Unified byte-level | `R_CUDA_UNIFIED_8_56` |
| 60 | `0x1003C` | `R_MERCURY_ABS_PROG_REL32` | `0x1D35EEC` | Abs PC-relative | — (Mercury-specific) |
| 61 | `0x1003D` | `R_MERCURY_ABS_PROG_REL64` | `0x1D35F05` | Abs PC-relative | — (Mercury-specific) |
| 62 | `0x1003E` | `R_MERCURY_UNIFIED32_LO` | `0x1D35F1E` | Unified table | `R_CUDA_UNIFIED32_LO_32` |
| 63 | `0x1003F` | `R_MERCURY_UNIFIED32_HI` | `0x1D35F35` | Unified table | `R_CUDA_UNIFIED32_HI_32` |
| 64 | `0x10040` | `R_MERCURY_NONE_LAST` | `0x1D35F4C` | Sentinel | `R_CUDA_NONE_LAST` |

## Category Groups

### Group 1: Sentinels (2 types)

| Index | Name | Description |
|------:|------|-------------|
| 0 | `R_MERCURY_NONE` | No-op placeholder, used for deleted or eliminated relocation entries |
| 64 | `R_MERCURY_NONE_LAST` | End-of-range sentinel, bounds the valid index space |

The validation function `sub_42F6C0` rejects any index >= 65 (0x41) with the diagnostic `"unknown attribute"`. `R_MERCURY_NONE` produces an all-zero descriptor; the application engine returns immediately without patching.

### Group 2: Global Address (1 type)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 1 | `R_MERCURY_G64` | 64 | `S + A` |

Primary relocation for global memory addresses (`.nv.global`, `.nv.shared`). Writes the full 64-bit result. Mercury has no 32-bit global counterpart (R_CUDA has both `R_CUDA_G32` and `R_CUDA_G64`).

### Group 3: Absolute Data (5 types)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 2 | `R_MERCURY_ABS64` | 64 | `S + A` |
| 3 | `R_MERCURY_ABS32` | 32 | `S + A` (truncated to 32 bits) |
| 4 | `R_MERCURY_ABS16` | 16 | `S + A` (truncated to 16 bits) |
| 5 | `R_MERCURY_ABS32_LO` | 16 | `lo16(S + A)` |
| 6 | `R_MERCURY_ABS32_HI` | 16 | `hi16(S + A)` |

Used for data sections and instruction immediates with absolute addressing. The `_LO`/`_HI` pair splits a 32-bit address across two instruction fields. Mercury uses a single position-independent type per width, unlike R_CUDA which has separate types per bit-position (`R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32`). The bit-field position is stored in the 64-byte descriptor entry, not the type name.

### Group 4: PC-Relative (4 types)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 7 | `R_MERCURY_PROG_REL64` | 64 | `(S + A) - PC` |
| 8 | `R_MERCURY_PROG_REL32` | 32 | `(S + A) - PC` |
| 9 | `R_MERCURY_PROG_REL32_LO` | 16 | `lo16((S + A) - PC)` |
| 10 | `R_MERCURY_PROG_REL32_HI` | 16 | `hi16((S + A) - PC)` |

Branch and call instructions (`BRA`, `CALL`, `BRX`). The `PROG_REL` prefix distinguishes these from the CUDA naming convention (`PCREL_IMM24`). Mercury supports 64-bit and 32-bit full-width PC-relative offsets plus a 16-bit split pair, versus CUDA which has only 24-bit PC-relative with bit-position variants. These four types have no direct R_CUDA equivalent (CUDA has `R_CUDA_PCREL_IMM24_26` and `R_CUDA_PCREL_IMM24_23` only).

### Group 5: Texture / Sampler / Surface (3 types)

| Index | Name | Description |
|------:|------|-------------|
| 11 | `R_MERCURY_TEX_HEADER_INDEX` | Texture header table index |
| 12 | `R_MERCURY_SAMP_HEADER_INDEX` | Sampler header table index |
| 13 | `R_MERCURY_SURF_HEADER_INDEX` | Surface header table index |

These patch merged header table indices into instruction operands. During linking, per-module header tables are merged into a single global table; these relocations update references to the correct merged entry. Direct semantic equivalents of the R_CUDA `*_HEADER_INDEX` types.

Mercury drops the `SLOT`, `HW_DESC`, `HW_SW_DESC`, and `BINDLESSOFF` variants present in R_CUDA (18 types reduced to 3), consolidating resource binding into the header index mechanism alone.

### Group 6: Clear (1 type)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 14 | `R_MERCURY_UNUSED_CLEAR64` | 64 | Write zeros |

Zeroes a 64-bit field in merged data sections or nullifies unused instruction fields. Mercury has only the 64-bit variant; R_CUDA additionally has `R_CUDA_UNUSED_CLEAR32`.

### Group 7: Function Descriptor (1 type)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 15 | `R_MERCURY_FUNC_DESC_64` | 64 | `S + A` |

Resolves a reference to a function descriptor entry for indirect calls, virtual function tables, and device-side function pointers. Mercury has only the 64-bit variant; R_CUDA additionally has `R_CUDA_FUNC_DESC_32` and 9 positional `R_CUDA_FUNC_DESC32_*` types.

### Group 8: Byte-Level (8 types)

| Index | Name | Byte | Computation |
|------:|------|-----:|-------------|
| 16 | `R_MERCURY_8_0` | 0 | `byte_0(S + A)` |
| 17 | `R_MERCURY_8_8` | 1 | `byte_1(S + A)` |
| 18 | `R_MERCURY_8_16` | 2 | `byte_2(S + A)` |
| 19 | `R_MERCURY_8_24` | 3 | `byte_3(S + A)` |
| 20 | `R_MERCURY_8_32` | 4 | `byte_4(S + A)` |
| 21 | `R_MERCURY_8_40` | 5 | `byte_5(S + A)` |
| 22 | `R_MERCURY_8_48` | 6 | `byte_6(S + A)` |
| 23 | `R_MERCURY_8_56` | 7 | `byte_7(S + A)` |

Patch a single byte within a 64-bit data word. The suffix denotes the bit offset (byte 0 = bits 0–7, byte 7 = bits 56–63). Used for descriptor tables, attribute sections, and data structures requiring individual byte resolution. Direct equivalents of `R_CUDA_8_0` through `R_CUDA_8_56`.

### Group 9: Global Byte-Level (8 types)

| Index | Name | Byte | Computation |
|------:|------|-----:|-------------|
| 24 | `R_MERCURY_G8_0` | 0 | `byte_0(S + A)` (global) |
| 25 | `R_MERCURY_G8_8` | 1 | `byte_1(S + A)` (global) |
| 26 | `R_MERCURY_G8_16` | 2 | `byte_2(S + A)` (global) |
| 27 | `R_MERCURY_G8_24` | 3 | `byte_3(S + A)` (global) |
| 28 | `R_MERCURY_G8_32` | 4 | `byte_4(S + A)` (global) |
| 29 | `R_MERCURY_G8_40` | 5 | `byte_5(S + A)` (global) |
| 30 | `R_MERCURY_G8_48` | 6 | `byte_6(S + A)` (global) |
| 31 | `R_MERCURY_G8_56` | 7 | `byte_7(S + A)` (global) |

Byte-granularity variants of `R_MERCURY_G64`. Write a single byte of a global address at the specified position. Direct equivalents of `R_CUDA_G8_0` through `R_CUDA_G8_56`.

### Group 10: Function Descriptor Byte-Level (8 types)

| Index | Name | Byte | Computation |
|------:|------|-----:|-------------|
| 32 | `R_MERCURY_FUNC_DESC_8_0` | 0 | `byte_0(S + A)` (func desc) |
| 33 | `R_MERCURY_FUNC_DESC_8_8` | 1 | `byte_1(S + A)` (func desc) |
| 34 | `R_MERCURY_FUNC_DESC_8_16` | 2 | `byte_2(S + A)` (func desc) |
| 35 | `R_MERCURY_FUNC_DESC_8_24` | 3 | `byte_3(S + A)` (func desc) |
| 36 | `R_MERCURY_FUNC_DESC_8_32` | 4 | `byte_4(S + A)` (func desc) |
| 37 | `R_MERCURY_FUNC_DESC_8_40` | 5 | `byte_5(S + A)` (func desc) |
| 38 | `R_MERCURY_FUNC_DESC_8_48` | 6 | `byte_6(S + A)` (func desc) |
| 39 | `R_MERCURY_FUNC_DESC_8_56` | 7 | `byte_7(S + A)` (func desc) |

Byte-level function descriptor address patching. Mirrors the `R_MERCURY_FUNC_DESC_64` / byte-level split pattern. Direct equivalents of `R_CUDA_FUNC_DESC_8_0` through `R_CUDA_FUNC_DESC_8_56`.

### Group 11: Absolute PC-Relative (4 types)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 40 | `R_MERCURY_ABS_PROG_REL32_LO` | 16 | `lo16(\|S + A - PC\|)` |
| 41 | `R_MERCURY_ABS_PROG_REL32_HI` | 16 | `hi16(\|S + A - PC\|)` |
| 60 | `R_MERCURY_ABS_PROG_REL32` | 32 | `\|S + A - PC\|` |
| 61 | `R_MERCURY_ABS_PROG_REL64` | 64 | `\|S + A - PC\|` |

Hybrid absolute/PC-relative relocations computing an unsigned distance rather than a signed offset. Used for instruction encodings that require a non-negative displacement. The `_LO`/`_HI` split (indices 40–41) and the full-width variants (indices 60–61) appear at non-contiguous positions in the table, suggesting the split variants were defined first and full-width types were appended later.

No R_CUDA equivalent exists. This is an entirely Mercury-specific category.

### Group 12: PC-Relative Byte-Level (8 types)

| Index | Name | Byte | Computation |
|------:|------|-----:|-------------|
| 42 | `R_MERCURY_PROG_REL8_0` | 0 | `byte_0((S + A) - PC)` |
| 43 | `R_MERCURY_PROG_REL8_8` | 1 | `byte_1((S + A) - PC)` |
| 44 | `R_MERCURY_PROG_REL8_16` | 2 | `byte_2((S + A) - PC)` |
| 45 | `R_MERCURY_PROG_REL8_24` | 3 | `byte_3((S + A) - PC)` |
| 46 | `R_MERCURY_PROG_REL8_32` | 4 | `byte_4((S + A) - PC)` |
| 47 | `R_MERCURY_PROG_REL8_40` | 5 | `byte_5((S + A) - PC)` |
| 48 | `R_MERCURY_PROG_REL8_48` | 6 | `byte_6((S + A) - PC)` |
| 49 | `R_MERCURY_PROG_REL8_56` | 7 | `byte_7((S + A) - PC)` |

Byte-level PC-relative patching for data tables (jump tables, exception tables) where PC-relative distances are stored in byte-addressable format. No R_CUDA equivalent exists. This is an entirely Mercury-specific category.

### Group 13: Unified Table (12 types)

| Index | Name | Bits | Computation |
|------:|------|-----:|-------------|
| 50 | `R_MERCURY_UNIFIED` | special | Generic UDT/UFT marker |
| 51 | `R_MERCURY_UNIFIED_32` | 32 | `S + A` (unified offset) |
| 52 | `R_MERCURY_UNIFIED_8_0` | 8 | `byte_0(S + A)` (unified) |
| 53 | `R_MERCURY_UNIFIED_8_8` | 8 | `byte_1(S + A)` (unified) |
| 54 | `R_MERCURY_UNIFIED_8_16` | 8 | `byte_2(S + A)` (unified) |
| 55 | `R_MERCURY_UNIFIED_8_24` | 8 | `byte_3(S + A)` (unified) |
| 56 | `R_MERCURY_UNIFIED_8_32` | 8 | `byte_4(S + A)` (unified) |
| 57 | `R_MERCURY_UNIFIED_8_40` | 8 | `byte_5(S + A)` (unified) |
| 58 | `R_MERCURY_UNIFIED_8_48` | 8 | `byte_6(S + A)` (unified) |
| 59 | `R_MERCURY_UNIFIED_8_56` | 8 | `byte_7(S + A)` (unified) |
| 62 | `R_MERCURY_UNIFIED32_LO` | 16 | `lo16(S + A)` (unified) |
| 63 | `R_MERCURY_UNIFIED32_HI` | 16 | `hi16(S + A)` (unified) |

Relocations for the Unified Descriptor Table (UDT) and Unified Function Table (UFT), used for CUDA Dynamic Parallelism and indirect function calls. `R_MERCURY_UNIFIED` (index 50) is a generic marker resolved to type 0 (no-op) before the relocation engine runs, because the unified table manager computes final offsets earlier in the pipeline.

The `UNIFIED32_LO`/`UNIFIED32_HI` pair (indices 62–63) is non-contiguous with the main unified block (50–59), placed after the `ABS_PROG_REL` types. Direct semantic equivalents of the R_CUDA `UNIFIED*` family.

## Category Summary

| Category | Indices | Count | Computation | Description |
|----------|---------|------:|-------------|-------------|
| Sentinel | 0, 64 | 2 | — | No-op / bounds marker |
| Global address | 1 | 1 | `S + A` | 64-bit global memory addresses |
| Absolute data | 2–6 | 5 | `S + A` (full / lo / hi) | Data section and immediate addresses |
| PC-relative | 7–10 | 4 | `(S + A) - PC` (full / lo / hi) | Branch / call targets |
| Texture / sampler / surface | 11–13 | 3 | Header index lookup | Bindable resource references |
| Clear | 14 | 1 | Write zeros | Unused field cleanup |
| Function descriptor | 15 | 1 | `S + A` | Indirect call targets |
| Byte-level | 16–23 | 8 | `byte_n(S + A)` | Byte-granular data patching |
| Global byte-level | 24–31 | 8 | `byte_n(S + A)` | Global address byte patching |
| Func desc byte-level | 32–39 | 8 | `byte_n(S + A)` | Descriptor byte patching |
| Abs PC-relative | 40–41, 60–61 | 4 | `\|S + A - PC\|` (lo / hi / 32 / 64) | Unsigned distance fields |
| PC-relative byte-level | 42–49 | 8 | `byte_n((S + A) - PC)` | PC-relative byte patching |
| Unified table | 50–59, 62–63 | 12 | `S + A` (generic / 32 / byte / lo / hi) | UDT/UFT table references |
| **Total** | — | **65** | — | — |

## R_CUDA Equivalence Summary

Of the 65 Mercury types, 50 have a direct R_CUDA semantic equivalent and 15 are Mercury-specific additions.

**50 types with R_CUDA equivalents:**
- Sentinel: `NONE`, `NONE_LAST` (2)
- Global: `G64` (1)
- Absolute: `ABS64`, `ABS32`, `ABS16`, `ABS32_LO`, `ABS32_HI` (5)
- PC-relative: `PROG_REL32` maps loosely to `R_CUDA_PCREL_IMM24_*` (1)
- Texture/sampler/surface: `TEX_HEADER_INDEX`, `SAMP_HEADER_INDEX`, `SURF_HEADER_INDEX` (3)
- Clear: `UNUSED_CLEAR64` (1)
- Function descriptor: `FUNC_DESC_64` (1)
- Byte-level: `8_*` (8), `G8_*` (8), `FUNC_DESC_8_*` (8) = 24
- Unified: `UNIFIED`, `UNIFIED_32`, `UNIFIED_8_*` (10), `UNIFIED32_LO`, `UNIFIED32_HI` = 12

**15 Mercury-specific types (no R_CUDA counterpart):**

| Mercury-Specific Type | Index | Semantic Group |
|-----------------------|------:|----------------|
| `R_MERCURY_PROG_REL64` | 7 | 64-bit PC-relative |
| `R_MERCURY_PROG_REL32_LO` | 9 | Split PC-relative |
| `R_MERCURY_PROG_REL32_HI` | 10 | Split PC-relative |
| `R_MERCURY_ABS_PROG_REL32_LO` | 40 | Absolute PC-relative |
| `R_MERCURY_ABS_PROG_REL32_HI` | 41 | Absolute PC-relative |
| `R_MERCURY_PROG_REL8_0` | 42 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_8` | 43 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_16` | 44 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_24` | 45 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_32` | 46 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_40` | 47 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_48` | 48 | PC-relative byte-level |
| `R_MERCURY_PROG_REL8_56` | 49 | PC-relative byte-level |
| `R_MERCURY_ABS_PROG_REL32` | 60 | Absolute PC-relative |
| `R_MERCURY_ABS_PROG_REL64` | 61 | Absolute PC-relative |

The Mercury-specific types break into three families:
1. **PC-relative extensions** (indices 7, 9–10, 42–49): 64-bit PC-relative, split 32-bit PC-relative, and a full byte-level PC-relative set. R_CUDA has only 24-bit immediate PC-relative (`R_CUDA_PCREL_IMM24_23`, `R_CUDA_PCREL_IMM24_26`).
2. **Absolute PC-relative** (indices 40–41, 60–61): Unsigned distance computation (`|S + A - PC|`). No R_CUDA counterpart.
3. **Naming simplification** (not counted above): Where R_CUDA encodes the bit-position in the type name (e.g., `R_CUDA_ABS32_20`, `R_CUDA_ABS32_23`, `R_CUDA_ABS32_26`, `R_CUDA_ABS32_32` — four types for the same semantic), Mercury uses a single position-independent type (e.g., `R_MERCURY_ABS32`). The bit-field position is encoded in the 64-byte descriptor, not the type name. This collapses 25 CUDA `ABS*_<bitpos>` instruction types into 5 Mercury `ABS*` types.

## R_CUDA Categories Absent from R_MERCURY

| R_CUDA Category | R_CUDA Count | Reason for Omission |
|-----------------|-------------:|---------------------|
| `ABS*_<bitpos>` (instruction field) | 25 | Position-independent `R_MERCURY_ABS*` |
| `PCREL_IMM24_*` | 2 | Replaced by `PROG_REL32/64` |
| `CONST_FIELD*` | 10 | Constant bank references handled differently in Mercury |
| `BINDLESSOFF*` / `TEX_BINDLESSOFF*` | 7 | Bindless mechanism redesigned for Mercury ISA |
| `TEX_SLOT` / `SAMP_SLOT` / `SURF_SLOT` | 3 | Header index only, no slots |
| `SURF_HW_DESC` / `SURF_HW_SW_DESC` | 2 | HW descriptor types eliminated |
| `INSTRUCTION64` / `INSTRUCTION128` | 2 | Whole-instruction replacement not used in Mercury |
| `YIELD_*` | 2 | Yield conversion absent or handled differently |
| `UNUSED_CLEAR32` | 1 | Mercury has 64-bit clear only |
| `QUERY_DESC21_37` | 1 | Query descriptor absent |
| `6_31` / `2_47` | 2 | Narrow-field types not needed |
| `G32` | 1 | Mercury uses 64-bit global only |
| `FUNC_DESC_32` / `FUNC_DESC32_*` | 10 | 64-bit descriptor + byte-level only |

Total: 68 R_CUDA types have no R_MERCURY counterpart, reducing 117 to 50 shared types.

## String Table Details

The 65 primary name strings occupy a contiguous region at `0x1D35A17`--`0x1D35F5F` (1,353 bytes). The strings are densely packed with no padding between entries (each string is null-terminated and the next string begins at the next byte).

| First String | Address | Length |
|---|---|---|
| `R_MERCURY_NONE` | `0x1D35A17` | 15 bytes + NUL |
| `R_MERCURY_G64` | `0x1D35A26` | 14 bytes + NUL |
| ... | ... | ... |
| `R_MERCURY_NONE_LAST` | `0x1D35F4C` | 20 bytes + NUL |

The R_CUDA name strings begin immediately after at `0x1D35F60` (`R_CUDA_NONE`), forming a single contiguous string pool for all relocation type names.

### Trailing-Space Duplicates

Two additional strings with trailing spaces exist at separate `.rodata` addresses just before the Mercury descriptor table:

| Address | String | Size |
|---------|--------|------|
| `0x1D3CB71` | `"R_MERCURY_UNIFIED_8_0 "` | 22 bytes + NUL |
| `0x1D3CB88` | `"R_MERCURY_UNIFIED_8_8 "` | 22 bytes + NUL |

The corresponding R_CUDA trailing-space duplicates are adjacent:

| Address | String | Size |
|---------|--------|------|
| `0x1D3CB9F` | `"R_CUDA_UNIFIED_8_0 "` | 20 bytes + NUL |
| `0x1D3CBB3` | `"R_CUDA_UNIFIED_8_8 "` | 20 bytes + NUL |

All four trailing-space strings cluster at `0x1D3CB71`--`0x1D3CBC6`, 26 bytes before the Mercury descriptor table at `0x1D3CBE0`. These are formatting artifacts in `.rodata` — name strings embedded within or adjacent to descriptor table entries, not distinct relocation types. Only the `UNIFIED_8_0` and `UNIFIED_8_8` entries have this artifact; the other 63 entries reference strings from the primary name pool.

### EIATTR / EICOMPAT Attribute Names

Four additional Mercury-related strings appear outside the relocation name table:

| Address | String | Context |
|---------|--------|---------|
| `0x1D36F31` | `EIATTR_MERCURY_ISA_VERSION` | `.nv.info` attribute: Mercury ISA version |
| `0x1D37170` | `EIATTR_MERCURY_FINALIZER_OPTIONS` | `.nv.info` attribute: finalizer options |
| `0x245EF08` | `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | Compatibility: major.minor ISA version |
| `0x245EF38` | `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | Compatibility: patch-level ISA version |

These are `.nv.info` section attribute names processed alongside relocations during the attribute handling phase, not relocation types themselves.

**String accounting**: 65 relocation names + 2 trailing-space duplicates + 4 EIATTR/EICOMPAT attributes = **71 total** `MERCURY`-bearing strings in the binary.

## Binary Addresses

| Structure | Address | Size | Notes |
|-----------|---------|------|-------|
| Mercury name table | `off_1D371E0` | 65 entries x 16 bytes = 1,040 bytes | Pointer + metadata per entry |
| Mercury descriptor table | `off_1D3CBE0` | 65 entries x 64 bytes = 4,160 bytes | 3 actions per entry |
| CUDA descriptor table | `off_1D3DBE0` | 117 entries x 64 bytes = 7,488 bytes | Starts at Mercury entry 64 |
| Name string pool | `0x1D35A17`--`0x1D35F5F` | 1,353 bytes | 65 null-terminated strings |
| Trailing-space strings | `0x1D3CB71`--`0x1D3CBC6` | ~86 bytes | 4 strings (2 Mercury, 2 CUDA) |
| Application engine | `sub_468760` | 14,322 bytes | Shared CUDA/Mercury |
| Validation function | `sub_42F6C0` | ~250 bytes | Shared CUDA/Mercury |
| Table limit | `0x41` (65) | — | Immediate constant in validation |

### Descriptor Table Overlap

The Mercury descriptor table's last entry (`R_MERCURY_NONE_LAST` at index 64, address `0x1D3DBE0`) occupies the same 64 bytes as the CUDA descriptor table's first entry (`R_CUDA_NONE` at index 0, address `0x1D3DBE0`). Both are sentinel no-op entries with identical all-zero descriptors. The binary exploits this by sharing the memory:

```text
Mercury table:  [entry 0 ... entry 63] [entry 64 == CUDA entry 0]
                0x1D3CBE0              0x1D3DBE0
CUDA table:                            [entry 0 ... entry 116]
                                       0x1D3DBE0              0x1D3F920
```

This saves 64 bytes in `.rodata` by overlapping the Mercury end-sentinel with the CUDA start-sentinel.

### Name Table Entry Format

Each entry in the name table at `off_1D371E0` is 16 bytes (2 qwords = `v7[2 * index]` in `sub_42F6C0`):

```text
+0  char*    name_ptr      // pointer to null-terminated name string
+8  uint32_t field_0       // metadata (used in validation: v8[2] == max_elf_class)
+12 uint32_t field_1       // metadata
```

The validation function accesses `*((_DWORD *)v8 + 2)` (offset +8, field_0) to compare against `a2` (an ELF class parameter), rejecting relocations whose ELF class exceeds the maximum supported by that type. This is the per-type architecture compatibility check.

## Cross-References

**Internal (nvlink wiki):**

- [R\_MERCURY Relocations](../mercury/r-mercury-relocations.md) — Semantic details, descriptor table structure, and dispatch logic for Mercury relocation types
- [Relocation Engine](../linker/relocation-engine.md) — Shared CUDA/Mercury application engine (`sub_468760`) that processes both descriptor tables
- [R\_CUDA Catalog](r-cuda-catalog.md) — The CUDA counterpart catalog with 117 relocation types
- [Mercury Overview](../mercury/overview.md) — High-level Mercury architecture context for the capsule Mercury binary format
- [Capsule Mercury Format](../mercury/capmerc-format.md) — Binary layout of the capmerc format that uses R\_MERCURY relocations
- [Bindless Relocations](../linker/bindless-relocations.md) — Texture/surface relocation handling shared between CUDA and Mercury types

**Sibling wikis:**

- [ptxas: Relocations](../../ptxas/output/relocations.html) — ptxas-side relocation emission that generates the R\_MERCURY entries nvlink processes

## Confidence Assessment

### Spot-Check Table (12 entries)

All 12 spot-checked names found verbatim in `nvlink_strings.json` at their documented addresses:

| Entry | Address | Verified? |
|-------|---------|-----------|
| `R_MERCURY_NONE` | `0x1D35A17` | YES — xrefs to `0x1D371E0` (name table), `0x1D3CBE0` (descriptor table) |
| `R_MERCURY_G64` | `0x1D35A26` | YES |
| `R_MERCURY_ABS64` | `0x1D35A34` | YES |
| `R_MERCURY_ABS32` | `0x1D35A44` | YES |
| `R_MERCURY_PROG_REL64` | `0x1D35A8A` | YES |
| `R_MERCURY_TEX_HEADER_INDEX` | `0x1D35AE4` | YES |
| `R_MERCURY_FUNC_DESC_64` | `0x1D35B50` | YES |
| `R_MERCURY_UNIFIED` | `0x1D35E0F` | YES |
| `R_MERCURY_NONE_LAST` | `0x1D35F4C` | YES |
| `R_MERCURY_UNIFIED32_HI` | `0x1D35F35` | YES |
| `R_MERCURY_8_0` | `0x1D35B67` | YES |
| `R_MERCURY_FUNC_DESC_8_56` | `0x1D35D08` | YES |

### Per-Aspect Assessment

| Aspect | Confidence | Basis |
|--------|------------|-------|
| Type names (all 65 entries) | **HIGH** | All 65 names found in `nvlink_strings.json` at contiguous addresses `0x1D35A17`--`0x1D35F4C`; 12/12 spot-checked — 0 failures |
| String addresses (all 65 entries) | **HIGH** | Addresses extracted directly from `nvlink_strings.json` `addr` field; verified contiguous and densely packed |
| Name table address (`off_1D371E0`) | **HIGH** | Confirmed in decompiled `sub_42F6C0` (line 17: `v7 = &off_1D371E0`); Mercury branch with `a1 -= 0x10000` at line 15 |
| Name table entry size (16 bytes) | **HIGH** | Stride `v7[2 * a1]` in `sub_42F6C0` (line 29); 2 qwords = 16 bytes |
| Descriptor table address (`off_1D3CBE0`) | **HIGH** | Referenced in 8 decompiled files: `sub_469620` line 41, `sub_469790` line 40, `sub_4699B0` line 71, `sub_469B50` line 76, `sub_469D60` lines 202/207, `sub_4698A0` line 41, `sub_46ADC0` line 146 |
| Descriptor entry size (64 bytes) | **HIGH** | Indexing arithmetic `16 * v17` (16 DWORDs = 64 bytes) in `sub_469790` line 40 and `sub_4698A0` line 41 |
| Entry count (65) | **HIGH** | Bounded by `R_MERCURY_NONE` at index 0 and `R_MERCURY_NONE_LAST` at index 64; table limit `0x41` = 65 confirmed at `sub_42F6C0` line 18 |
| ELF encoding (`index + 0x10000`) | **HIGH** | `a1 -= 0x10000` at `sub_42F6C0` line 15; `v7 = (unsigned int)(v17 - 0x10000)` at `sub_469620` line 40 |
| Descriptor table overlap (Mercury[64] == CUDA[0]) | **HIGH** | `0x1D3CBE0 + 64*64 = 0x1D3DBE0 = off_1D3DBE0`; arithmetic identity verified |
| R_CUDA equivalence mapping | **HIGH** | Based on semantic name comparison and identical descriptor action patterns for matching types |
| String table range (`0x1D35A17`--`0x1D35F5F`) | **HIGH** | First string at `0x1D35A17`, last string `R_MERCURY_NONE_LAST` (20 bytes) starts at `0x1D35F4C`, ends at `0x1D35F5F`; R_CUDA starts at `0x1D35F60` |
| Trailing-space duplicates (2 entries) | **HIGH** | Verified at `0x1D3CB71` and `0x1D3CB88` in string table |
| EIATTR/EICOMPAT attribute names (4) | **HIGH** | All 4 found at documented addresses |
| Category computation formulas | **MEDIUM** | `S + A` and `(S + A) - PC` patterns inferred from relocation engine code analysis; not every type individually traced through the engine |
| Name table metadata fields | **MEDIUM** | Field at offset +8 used in validation (`v8[2]` in `sub_42F6C0`); exact semantics of all metadata fields not fully decoded |
| Abs PC-relative computation (`\|S + A - PC\|`) | **MEDIUM** | Inferred from name pattern `ABS_PROG_REL`; not individually traced through the application engine |
