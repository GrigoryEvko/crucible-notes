# R_CUDA Relocation Catalog

Complete reference table of all **117 canonical** R_CUDA relocation type names extracted from nvlink v13.0.88, indexed across **182 total descriptor rows** (117 standard + 65 attribute). The raw `nvlink_strings.json` pool contains 119 R_CUDA string entries — 117 unique names plus two trailing-whitespace duplicates of `R_CUDA_UNIFIED_8_0` and `R_CUDA_UNIFIED_8_8` (see Confidence Assessment). Types are organized into two descriptor tables: the standard table at `off_1D37600` (117 entries, indices 0--116) and the attribute table at `off_1D371E0` (65 entries, indices 0x10000--0x10040). See [R_CUDA Relocations](../linker/r-cuda-relocations.md) for the relocation engine, descriptor format, action types, and architecture class system.

## Standard Table (off_1D37600)

117 entries. Index = raw `r_type` value in `.rela.*` sections. Bit width and bit position are derived from the type name and confirmed against the 64-byte descriptors at `off_1D3DBE0`.

| Index | Name | Type | Bit Width | Bit Position |
|------:|------|----------|----------:|-------------:|
| 0 | `R_CUDA_NONE` | sentinel | — | — |
| 1 | `R_CUDA_32` | data | 32 | 0 |
| 2 | `R_CUDA_64` | data | 64 | 0 |
| 3 | `R_CUDA_G32` | global | 32 | 0 |
| 4 | `R_CUDA_G64` | global | 64 | 0 |
| 5 | `R_CUDA_ABS32_26` | abs-instr | 32 | 26 |
| 6 | `R_CUDA_TEX_HEADER_INDEX` | texture | special | special |
| 7 | `R_CUDA_SAMP_HEADER_INDEX` | sampler | special | special |
| 8 | `R_CUDA_SURF_HW_DESC` | surface | special | special |
| 9 | `R_CUDA_SURF_HW_SW_DESC` | surface | special | special |
| 10 | `R_CUDA_ABS32_LO_26` | abs-instr | 16 | 26 |
| 11 | `R_CUDA_ABS32_HI_26` | abs-instr | 16 | 26 |
| 12 | `R_CUDA_ABS32_23` | abs-instr | 32 | 23 |
| 13 | `R_CUDA_ABS32_LO_23` | abs-instr | 16 | 23 |
| 14 | `R_CUDA_ABS32_HI_23` | abs-instr | 16 | 23 |
| 15 | `R_CUDA_ABS24_26` | abs-instr | 24 | 26 |
| 16 | `R_CUDA_ABS24_23` | abs-instr | 24 | 23 |
| 17 | `R_CUDA_ABS16_26` | abs-instr | 16 | 26 |
| 18 | `R_CUDA_ABS16_23` | abs-instr | 16 | 23 |
| 19 | `R_CUDA_TEX_SLOT` | texture | special | special |
| 20 | `R_CUDA_SAMP_SLOT` | sampler | special | special |
| 21 | `R_CUDA_SURF_SLOT` | surface | special | special |
| 22 | `R_CUDA_TEX_BINDLESSOFF13_32` | bindless | 13 | 32 |
| 23 | `R_CUDA_TEX_BINDLESSOFF13_47` | bindless | 13 | 47 |
| 24 | `R_CUDA_CONST_FIELD19_28` | const | 19 | 28 |
| 25 | `R_CUDA_CONST_FIELD19_23` | const | 19 | 23 |
| 26 | `R_CUDA_TEX_SLOT9_49` | texture | 9 | 49 |
| 27 | `R_CUDA_6_31` | misc | 6 | 31 |
| 28 | `R_CUDA_2_47` | misc | 2 | 47 |
| 29 | `R_CUDA_TEX_BINDLESSOFF13_41` | bindless | 13 | 41 |
| 30 | `R_CUDA_TEX_BINDLESSOFF13_45` | bindless | 13 | 45 |
| 31 | `R_CUDA_FUNC_DESC32_23` | func-desc | 32 | 23 |
| 32 | `R_CUDA_FUNC_DESC32_LO_23` | func-desc | 16 | 23 |
| 33 | `R_CUDA_FUNC_DESC32_HI_23` | func-desc | 16 | 23 |
| 34 | `R_CUDA_FUNC_DESC_32` | func-desc | 32 | 0 |
| 35 | `R_CUDA_FUNC_DESC_64` | func-desc | 64 | 0 |
| 36 | `R_CUDA_CONST_FIELD21_26` | const | 21 | 26 |
| 37 | `R_CUDA_QUERY_DESC21_37` | misc | 21 | 37 |
| 38 | `R_CUDA_CONST_FIELD19_26` | const | 19 | 26 |
| 39 | `R_CUDA_CONST_FIELD21_23` | const | 21 | 23 |
| 40 | `R_CUDA_PCREL_IMM24_26` | pc-rel | 24 | 26 |
| 41 | `R_CUDA_PCREL_IMM24_23` | pc-rel | 24 | 23 |
| 42 | `R_CUDA_ABS32_20` | abs-instr | 32 | 20 |
| 43 | `R_CUDA_ABS32_LO_20` | abs-instr | 16 | 20 |
| 44 | `R_CUDA_ABS32_HI_20` | abs-instr | 16 | 20 |
| 45 | `R_CUDA_ABS24_20` | abs-instr | 24 | 20 |
| 46 | `R_CUDA_ABS16_20` | abs-instr | 16 | 20 |
| 47 | `R_CUDA_FUNC_DESC32_20` | func-desc | 32 | 20 |
| 48 | `R_CUDA_FUNC_DESC32_LO_20` | func-desc | 16 | 20 |
| 49 | `R_CUDA_FUNC_DESC32_HI_20` | func-desc | 16 | 20 |
| 50 | `R_CUDA_CONST_FIELD19_20` | const | 19 | 20 |
| 51 | `R_CUDA_BINDLESSOFF13_36` | bindless | 13 | 36 |
| 52 | `R_CUDA_SURF_HEADER_INDEX` | surface | special | special |
| 53 | `R_CUDA_INSTRUCTION64` | instr | 64 | 0 |
| 54 | `R_CUDA_CONST_FIELD21_20` | const | 21 | 20 |
| 55 | `R_CUDA_ABS32_32` | abs-instr | 32 | 32 |
| 56 | `R_CUDA_ABS32_LO_32` | abs-instr | 16 | 32 |
| 57 | `R_CUDA_ABS32_HI_32` | abs-instr | 16 | 32 |
| 58 | `R_CUDA_ABS47_34` | abs-instr | 47 | 34 |
| 59 | `R_CUDA_ABS16_32` | abs-instr | 16 | 32 |
| 60 | `R_CUDA_ABS24_32` | abs-instr | 24 | 32 |
| 61 | `R_CUDA_FUNC_DESC32_32` | func-desc | 32 | 32 |
| 62 | `R_CUDA_FUNC_DESC32_LO_32` | func-desc | 16 | 32 |
| 63 | `R_CUDA_FUNC_DESC32_HI_32` | func-desc | 16 | 32 |
| 64 | `R_CUDA_CONST_FIELD19_40` | const | 19 | 40 |
| 65 | `R_CUDA_BINDLESSOFF14_40` | bindless | 14 | 40 |
| 66 | `R_CUDA_CONST_FIELD21_38` | const | 21 | 38 |
| 67 | `R_CUDA_INSTRUCTION128` | instr | 128 | 0 |
| 68 | `R_CUDA_YIELD_OPCODE9_0` | yield | 9 | 0 |
| 69 | `R_CUDA_YIELD_CLEAR_PRED4_87` | yield | 4 | 87 |
| 70 | `R_CUDA_32_LO` | data | 16 | 0 |
| 71 | `R_CUDA_32_HI` | data | 16 | 0 |
| 72 | `R_CUDA_UNUSED_CLEAR32` | clear | 32 | 0 |
| 73 | `R_CUDA_UNUSED_CLEAR64` | clear | 64 | 0 |
| 74 | `R_CUDA_ABS24_40` | abs-instr | 24 | 40 |
| 75 | `R_CUDA_ABS55_16_34` | abs-instr | 55 | 34 |
| 76 | `R_CUDA_8_0` | byte | 8 | 0 |
| 77 | `R_CUDA_8_8` | byte | 8 | 8 |
| 78 | `R_CUDA_8_16` | byte | 8 | 16 |
| 79 | `R_CUDA_8_24` | byte | 8 | 24 |
| 80 | `R_CUDA_8_32` | byte | 8 | 32 |
| 81 | `R_CUDA_8_40` | byte | 8 | 40 |
| 82 | `R_CUDA_8_48` | byte | 8 | 48 |
| 83 | `R_CUDA_8_56` | byte | 8 | 56 |
| 84 | `R_CUDA_G8_0` | global | 8 | 0 |
| 85 | `R_CUDA_G8_8` | global | 8 | 8 |
| 86 | `R_CUDA_G8_16` | global | 8 | 16 |
| 87 | `R_CUDA_G8_24` | global | 8 | 24 |
| 88 | `R_CUDA_G8_32` | global | 8 | 32 |
| 89 | `R_CUDA_G8_40` | global | 8 | 40 |
| 90 | `R_CUDA_G8_48` | global | 8 | 48 |
| 91 | `R_CUDA_G8_56` | global | 8 | 56 |
| 92 | `R_CUDA_FUNC_DESC_8_0` | func-desc | 8 | 0 |
| 93 | `R_CUDA_FUNC_DESC_8_8` | func-desc | 8 | 8 |
| 94 | `R_CUDA_FUNC_DESC_8_16` | func-desc | 8 | 16 |
| 95 | `R_CUDA_FUNC_DESC_8_24` | func-desc | 8 | 24 |
| 96 | `R_CUDA_FUNC_DESC_8_32` | func-desc | 8 | 32 |
| 97 | `R_CUDA_FUNC_DESC_8_40` | func-desc | 8 | 40 |
| 98 | `R_CUDA_FUNC_DESC_8_48` | func-desc | 8 | 48 |
| 99 | `R_CUDA_FUNC_DESC_8_56` | func-desc | 8 | 56 |
| 100 | `R_CUDA_ABS20_44` | abs-instr | 20 | 44 |
| 101 | `R_CUDA_SAMP_HEADER_INDEX_0` | sampler | special | special |
| 102 | `R_CUDA_UNIFIED` | unified | special | special |
| 103 | `R_CUDA_UNIFIED_32` | unified | 32 | 0 |
| 104 | `R_CUDA_UNIFIED_8_0` | unified | 8 | 0 |
| 105 | `R_CUDA_UNIFIED_8_8` | unified | 8 | 8 |
| 106 | `R_CUDA_UNIFIED_8_16` | unified | 8 | 16 |
| 107 | `R_CUDA_UNIFIED_8_24` | unified | 8 | 24 |
| 108 | `R_CUDA_UNIFIED_8_32` | unified | 8 | 32 |
| 109 | `R_CUDA_UNIFIED_8_40` | unified | 8 | 40 |
| 110 | `R_CUDA_UNIFIED_8_48` | unified | 8 | 48 |
| 111 | `R_CUDA_UNIFIED_8_56` | unified | 8 | 56 |
| 112 | `R_CUDA_UNIFIED32_LO_32` | unified | 16 | 32 |
| 113 | `R_CUDA_UNIFIED32_HI_32` | unified | 16 | 32 |
| 114 | `R_CUDA_ABS56_16_34` | abs-instr | 56 | 34 |
| 115 | `R_CUDA_CONST_FIELD22_37` | const | 22 | 37 |
| 116 | `R_CUDA_NONE_LAST` | sentinel | — | — |

## Attribute Table (off_1D371E0)

65 entries. Attribute relocations apply to `.nv.info.*` metadata sections rather than instruction streams. The encoded `r_type` in ELF is `0x10000 + index` (e.g., attribute index 3 = `r_type` 0x10003). The attribute table reuses a subset of the same type names as the standard table but with different 64-byte descriptors.

The attribute table entries are not individually named in the binary — they share string pointers with the standard table. The following mapping lists the 65 attribute indices and the standard-table name each one references. Indices not listed here (gaps) use `R_CUDA_NONE`.

| Attr Index | r_type | Shared Name | Type |
|-----------:|-------:|-------------|----------|
| 0 | 0x10000 | `R_CUDA_NONE` | sentinel |
| 1 | 0x10001 | `R_CUDA_32` | data |
| 2 | 0x10002 | `R_CUDA_64` | data |
| 3 | 0x10003 | `R_CUDA_G32` | global |
| 4 | 0x10004 | `R_CUDA_G64` | global |
| 5 | 0x10005 | `R_CUDA_ABS32_26` | abs-instr |
| 6 | 0x10006 | `R_CUDA_TEX_HEADER_INDEX` | texture |
| 7 | 0x10007 | `R_CUDA_SAMP_HEADER_INDEX` | sampler |
| 8 | 0x10008 | `R_CUDA_SURF_HW_DESC` | surface |
| 9 | 0x10009 | `R_CUDA_SURF_HW_SW_DESC` | surface |
| 10 | 0x1000A | `R_CUDA_ABS32_LO_26` | abs-instr |
| 11 | 0x1000B | `R_CUDA_ABS32_HI_26` | abs-instr |
| 12 | 0x1000C | `R_CUDA_ABS32_23` | abs-instr |
| 13 | 0x1000D | `R_CUDA_ABS32_LO_23` | abs-instr |
| 14 | 0x1000E | `R_CUDA_ABS32_HI_23` | abs-instr |
| 15 | 0x1000F | `R_CUDA_ABS24_26` | abs-instr |
| 16 | 0x10010 | `R_CUDA_ABS24_23` | abs-instr |
| 17 | 0x10011 | `R_CUDA_ABS16_26` | abs-instr |
| 18 | 0x10012 | `R_CUDA_ABS16_23` | abs-instr |
| 19 | 0x10013 | `R_CUDA_TEX_SLOT` | texture |
| 20 | 0x10014 | `R_CUDA_SAMP_SLOT` | sampler |
| 21 | 0x10015 | `R_CUDA_SURF_SLOT` | surface |
| 22 | 0x10016 | `R_CUDA_TEX_BINDLESSOFF13_32` | bindless |
| 23 | 0x10017 | `R_CUDA_TEX_BINDLESSOFF13_47` | bindless |
| 24 | 0x10018 | `R_CUDA_CONST_FIELD19_28` | const |
| 25 | 0x10019 | `R_CUDA_CONST_FIELD19_23` | const |
| 26 | 0x1001A | `R_CUDA_TEX_SLOT9_49` | texture |
| 27 | 0x1001B | `R_CUDA_6_31` | misc |
| 28 | 0x1001C | `R_CUDA_2_47` | misc |
| 29 | 0x1001D | `R_CUDA_TEX_BINDLESSOFF13_41` | bindless |
| 30 | 0x1001E | `R_CUDA_TEX_BINDLESSOFF13_45` | bindless |
| 31 | 0x1001F | `R_CUDA_FUNC_DESC32_23` | func-desc |
| 32 | 0x10020 | `R_CUDA_FUNC_DESC32_LO_23` | func-desc |
| 33 | 0x10021 | `R_CUDA_FUNC_DESC32_HI_23` | func-desc |
| 34 | 0x10022 | `R_CUDA_FUNC_DESC_32` | func-desc |
| 35 | 0x10023 | `R_CUDA_FUNC_DESC_64` | func-desc |
| 36 | 0x10024 | `R_CUDA_CONST_FIELD21_26` | const |
| 37 | 0x10025 | `R_CUDA_QUERY_DESC21_37` | misc |
| 38 | 0x10026 | `R_CUDA_CONST_FIELD19_26` | const |
| 39 | 0x10027 | `R_CUDA_CONST_FIELD21_23` | const |
| 40 | 0x10028 | `R_CUDA_PCREL_IMM24_26` | pc-rel |
| 41 | 0x10029 | `R_CUDA_PCREL_IMM24_23` | pc-rel |
| 42 | 0x1002A | `R_CUDA_ABS32_20` | abs-instr |
| 43 | 0x1002B | `R_CUDA_ABS32_LO_20` | abs-instr |
| 44 | 0x1002C | `R_CUDA_ABS32_HI_20` | abs-instr |
| 45 | 0x1002D | `R_CUDA_ABS24_20` | abs-instr |
| 46 | 0x1002E | `R_CUDA_ABS16_20` | abs-instr |
| 47 | 0x1002F | `R_CUDA_FUNC_DESC32_20` | func-desc |
| 48 | 0x10030 | `R_CUDA_FUNC_DESC32_LO_20` | func-desc |
| 49 | 0x10031 | `R_CUDA_FUNC_DESC32_HI_20` | func-desc |
| 50 | 0x10032 | `R_CUDA_CONST_FIELD19_20` | const |
| 51 | 0x10033 | `R_CUDA_BINDLESSOFF13_36` | bindless |
| 52 | 0x10034 | `R_CUDA_SURF_HEADER_INDEX` | surface |
| 53 | 0x10035 | `R_CUDA_INSTRUCTION64` | instr |
| 54 | 0x10036 | `R_CUDA_CONST_FIELD21_20` | const |
| 55 | 0x10037 | `R_CUDA_ABS32_32` | abs-instr |
| 56 | 0x10038 | `R_CUDA_ABS32_LO_32` | abs-instr |
| 57 | 0x10039 | `R_CUDA_ABS32_HI_32` | abs-instr |
| 58 | 0x1003A | `R_CUDA_ABS47_34` | abs-instr |
| 59 | 0x1003B | `R_CUDA_ABS16_32` | abs-instr |
| 60 | 0x1003C | `R_CUDA_ABS24_32` | abs-instr |
| 61 | 0x1003D | `R_CUDA_FUNC_DESC32_32` | func-desc |
| 62 | 0x1003E | `R_CUDA_FUNC_DESC32_LO_32` | func-desc |
| 63 | 0x1003F | `R_CUDA_FUNC_DESC32_HI_32` | func-desc |
| 64 | 0x10040 | `R_CUDA_CONST_FIELD19_40` | const |

## Category Summary

| Type | Count (standard) | Description |
|----------|-----------------:|-------------|
| abs-instr | 25 | Absolute address patched into instruction bit-fields |
| bindless | 6 | Bindless texture/surface offset |
| byte | 8 | Byte-granularity data patching (R_CUDA_8_*) |
| clear | 2 | Zero-fill unused fields |
| const | 10 | Constant bank offset (R_CUDA_CONST_FIELD*) |
| data | 4 | Full-width data-section relocations (32, 64, 32_HI, 32_LO) |
| func-desc | 19 | Function descriptor reference |
| global | 10 | Global memory address (R_CUDA_G*) |
| instr | 2 | Whole-instruction replacement (64-bit or 128-bit) |
| misc | 3 | Query descriptor, narrow-field encodings |
| pc-rel | 2 | PC-relative branch offset |
| sampler | 3 | Sampler header/slot binding |
| sentinel | 2 | No-op / end-of-range marker |
| surface | 4 | Surface header/slot/descriptor binding |
| texture | 3 | Texture header/slot binding |
| unified | 12 | Unified descriptor/function table reference |
| yield | 2 | YIELD-to-NOP instruction conversion |
| **Total** | **117** | — |

## Descriptor Byte Layout

The 117 standard and 65 attribute descriptors are stored as 64-byte fixed records in `.rodata`. Both the standard table at `off_1D3DBE0` and the attribute table at `off_1D3CBE0` share the same layout; the relocation engine picks one or the other from the `r_type & 0x10000` attribute bit (see `sub_469620` lines 26--38). This section provides the byte-for-byte decode that confirms the three-action-slot model described in [R_CUDA Relocations § Descriptor Format](../linker/r-cuda-relocations.md#descriptor-format).

### Verified byte layout

```text
offset  size  field             role
------  ----  ----------------  ----------------------------------------------
 0..7    8    name_ptr          LE pointer to type-name string in .rodata
 8..11   4    apply_class       1=instr bit-field, 2=descriptor/global, 4=data,
                                0=sentinel, 0xFF00=poison terminator
12..15   4    slot0.bit_offset  primary bit position to patch
16..19   4    slot0.bit_width   primary bit-width to patch
20..23   4    slot0.action      action enum (0..56)
24..27   4    slot0.aux         per-action immediate (HI-byte addend, byte offset)
28..43  16    slot1             second action: (bit_offset, bit_width, action, aux)
44..59  16    slot2             third action:  (bit_offset, bit_width, action, aux)
60..63   4    sentinel          zero -- terminates the action-slot iterator
```

The 12-byte header in [`r-cuda-relocations.md` § Descriptor Format](../linker/r-cuda-relocations.md#descriptor-format) corresponds to the `name_ptr` (treated as two consumed `_DWORD`s `field_0`/`field_1` by `sub_46ADC0`) followed by the `apply_class` dword. This catalog labels the first 8 bytes as a single name pointer because every observed value is a valid `.rodata` string address.

### ASCII byte map

```text
byte:   0  1  2  3  4  5  6  7   8  9  A  B   C  D  E  F  10 11 12 13  14 15 16 17
       [ name_ptr (qword)     ] [apply_class] [slot0.boff] [slot0.bwid] [slot0.act ]

byte:  18 19 1A 1B  1C 1D 1E 1F  20 21 22 23  24 25 26 27  28 29 2A 2B  2C 2D 2E 2F
       [slot0.aux ] [slot1.boff] [slot1.bwid] [slot1.act ] [slot1.aux ] [slot2.boff]

byte:  30 31 32 33  34 35 36 37  38 39 3A 3B  3C 3D 3E 3F
       [slot2.bwid] [slot2.act ] [slot2.aux ] [ sentinel ]
```

### `apply_class` enum (dword at byte 8)

| Value | Meaning | Count | Examples |
|------:|---------|------:|----------|
| 0 | sentinel | 1 | `R_CUDA_NONE` (idx 0) |
| 1 | instruction-stream bit-field patch | 39 | `R_CUDA_ABS24_26`, `R_CUDA_CONST_FIELD19_28`, `R_CUDA_PCREL_IMM24_26`, `R_CUDA_YIELD_OPCODE9_0`, `R_CUDA_UNUSED_CLEAR32` |
| 2 | descriptor / global / func-desc / unified / instr-replacement | 52 | `R_CUDA_G32`, `R_CUDA_TEX_HEADER_INDEX`, `R_CUDA_FUNC_DESC_32`, `R_CUDA_INSTRUCTION64`, `R_CUDA_UNIFIED` |
| 4 | data-section byte / word patch | 24 | `R_CUDA_32`, `R_CUDA_64`, `R_CUDA_ABS32_26`, `R_CUDA_8_0..R_CUDA_8_56` |
| 0xFF00 | poison terminator | 1 | `R_CUDA_NONE_LAST` (idx 116) — diagonal `0xFF` marker |

This enum is *not* the action selector; it is a coarse classifier consumed by the dispatcher in `sub_4698A0` and `sub_469790` that decides which top-level patch path to take. The fine-grained operation is encoded in `slot0.action`.

### `action` enum (dword at byte 20)

Twenty-three distinct action codes appear in the standard table. The bitmask test in `sub_469620` line 46 (`_bittest64(0x3FFFE002C6, action)`) gates the symbol-resolution branch on actions `{1, 2, 6, 7, 9, 17, 18, 19, 20, 21, 22, ..., 33, 37}` — the entries that need late symbol fix-up.

| Action | Behavior | Representative entries |
|------:|---------|------------------------|
| 0 | no-op | `R_CUDA_NONE` (0) |
| 1 | absolute address into `(bit_offset, bit_width)` | `R_CUDA_32`, `R_CUDA_ABS32_26`, `R_CUDA_ABS20_44` |
| 2 | global-segment address | `R_CUDA_G32`, `R_CUDA_G64` |
| 3 | texture/sampler header index | `R_CUDA_TEX_HEADER_INDEX`, `R_CUDA_SAMP_HEADER_INDEX_0` |
| 4--5 | surface descriptor (HW-only / HW+SW) | `R_CUDA_SURF_HW_DESC`, `R_CUDA_SURF_HW_SW_DESC` |
| 6 | low 16 bits of 32-bit absolute | `R_CUDA_ABS32_LO_26`, `R_CUDA_32_LO` |
| 7 | high 16 bits of 32-bit absolute (`aux = 0x20`) | `R_CUDA_ABS32_HI_26`, `R_CUDA_32_HI` |
| 8 | texture/sampler/surface slot | `R_CUDA_TEX_SLOT`, `R_CUDA_TEX_SLOT9_49` |
| 9 | `SHIFTED_2` (`(S + A) >> 2`); reused as the leading slot for split-field bindless / constant-bank / wide-immediate types (`R_CUDA_TEX_BINDLESSOFF13_32`, `R_CUDA_CONST_FIELD19_28`, `R_CUDA_ABS55_16_34`). See [R_CUDA Relocations § Action Types](../linker/r-cuda-relocations.md#action-types) for the engine semantics. | `R_CUDA_TEX_BINDLESSOFF13_32`, `R_CUDA_CONST_FIELD19_28` |
| 10, 11 | continuation pieces of a split-field reference | seen only in slot1/slot2 of `R_CUDA_CONST_FIELD*` |
| 12--14 | function-descriptor 32-bit (full / LO / HI) | `R_CUDA_FUNC_DESC32_23`, `..._LO_23`, `..._HI_23` |
| 15 | function-descriptor 32/64 raw | `R_CUDA_FUNC_DESC_32`, `R_CUDA_FUNC_DESC_64` |
| 16 | PC-relative branch offset | `R_CUDA_PCREL_IMM24_26`, `R_CUDA_PCREL_IMM24_23` |
| 17 | whole-instruction replacement | `R_CUDA_INSTRUCTION64`, `R_CUDA_INSTRUCTION128` |
| 18 | YIELD opcode rewrite | `R_CUDA_YIELD_OPCODE9_0` |
| 19 | YIELD-clear-predicate rewrite | `R_CUDA_YIELD_CLEAR_PRED4_87` |
| 20 | zero-fill (`UNUSED_CLEAR`) | `R_CUDA_UNUSED_CLEAR32`, `R_CUDA_UNUSED_CLEAR64` |
| 21 (`0x15`) | `piece_cont` — second piece of a split-field type, pairs with a leading action-9 slot | `R_CUDA_ABS55_16_34`, `R_CUDA_ABS56_16_34` |
| 22--29 | 8-bit data patches at byte offsets 0, 8, ..., 56 | `R_CUDA_8_0 .. R_CUDA_8_56` |
| 30--37 | 8-bit global patches | `R_CUDA_G8_0 .. R_CUDA_G8_56` |
| 38--45 | 8-bit func-desc patches | `R_CUDA_FUNC_DESC_8_0 .. R_CUDA_FUNC_DESC_8_56` |
| 46 | unified descriptor full | `R_CUDA_UNIFIED`, `R_CUDA_UNIFIED_32` |
| 47--54 | unified 8-bit patches | `R_CUDA_UNIFIED_8_0 .. R_CUDA_UNIFIED_8_56` |
| 55--56 | unified-32 LO / HI 16-bit pieces | `R_CUDA_UNIFIED32_LO_32`, `R_CUDA_UNIFIED32_HI_32` |

### Slot usage statistics

Of the 117 standard descriptors:

- **108 entries** use slot 0 only — slot 1 and slot 2 are zero, and only one patch action runs.
- **8 entries** use slot 0 + slot 1 — multi-piece encodings split across two bit ranges.
- **1 entry** (`R_CUDA_CONST_FIELD19_28`) uses all three slots: 14 bits at position 28, plus 4 bits at position 42, plus 1 bit at position 26.

The action engine in `sub_468760` iterates `slot0 → slot1 → slot2 → sentinel`, applying each non-zero action in turn. A zero `action` byte in any slot is skipped, not treated as a real type-zero patch.

### Decoded sample entries

The following entries were byte-decoded against `nvlink_rodata.bin` (rodata offset `0xBA40`, equivalent to RVA `0x1D3DBE0`):

| Idx | Name | name_ptr | apply_class | slot0 (boff, bwid, act, aux) | slot1 / slot2 |
|----:|------|---------:|------------:|-----------------------------:|----------------|
| 0 | `R_CUDA_NONE` | `0x1D35F60` | 0 | (0, 0, 0, 0) | — |
| 1 | `R_CUDA_32` | `0x1D35F6C` | 4 | (0, 32, 1, 0) | — |
| 4 | `R_CUDA_G64` | `0x1D35F8B` | 2 | (0, 64, 2, 0) | — |
| 5 | `R_CUDA_ABS32_26` | `0x1D35F96` | 4 | (26, 32, 1, 0) | — |
| 11 | `R_CUDA_ABS32_HI_26` | `0x1D36015` | 4 | (26, 32, 7, **32**) | — |
| 24 | `R_CUDA_CONST_FIELD19_28` | `0x1D36108` | 1 | (28, 14, 9, 0) | slot1=(42, 4, 10, 0); slot2=(26, 1, 11, 0) |
| 34 | `R_CUDA_FUNC_DESC_32` | `0x1D361E4` | 2 | (0, 32, 15, 0) | — |
| 53 | `R_CUDA_INSTRUCTION64` | `0x1D3637E` | 2 | (0, 64, 17, 0) | — |
| 67 | `R_CUDA_INSTRUCTION128` | `0x1D364A1` | 2 | (0, 128, 17, 0) | — |
| 68 | `R_CUDA_YIELD_OPCODE9_0` | `0x1D364B7` | 1 | (0, 9, 18, 0) | — |
| 75 | `R_CUDA_ABS55_16_34` | `0x1D36540` | 2 | (16, 8, 9, 2) | slot1=(34, 47, 21, 10) — 8 + 47 = 55 bits |
| 76 | `R_CUDA_8_0` | `0x1D36553` | 4 | (0, 8, 22, 0) | — |
| 80 | `R_CUDA_8_32` | `0x1D36581` | 4 | (0, 8, 26, **32**) | — |
| 114 | `R_CUDA_ABS56_16_34` | `0x1D367DD` | 2 | (16, 8, 9, 2) | slot1=(34, 48, 21, 10) — 8 + 48 = 56 bits |
| 116 | `R_CUDA_NONE_LAST` | `0xFF` | 0xFF00 | (0, 0xFF0000, 0, 0xFF000000) | slot1=(0, 0, 255, 0); slot2=(0xFF00, 0, 0xFF0000, 0); sentinel=`0xFF000000` |

`bit_offset` and `bit_width` recovered from the descriptors agree byte-for-byte with the values parsed from the type-name suffixes in the standard table above for every entry where the suffix is unambiguous. Where the name encodes a single bit-width (e.g. `R_CUDA_ABS55_16_34`), the descriptor splits it across two slots whose widths sum to the advertised value.

> ⚡ **QUIRK — diagonal-0xFF terminator on `R_CUDA_NONE_LAST`**
> The final standard-table entry (idx 116) is not a real descriptor. Its 64 bytes form an 8×8 grid with `0xFF` on the diagonal and zeros elsewhere, so the name pointer reads as `0x00000000_000000FF`, the apply-class as `0xFF00`, and the sentinel slot at byte 60 contains `0xFF000000` — every parse axis returns garbage. The bounds check at `sub_42F6C0` line 23 (`a1 >= 0x75`) already prevents real dispatch on index 116, so the diagonal pattern functions as a poison page: if anything ever reaches descriptor 116, any field you read will be obviously corrupt. Treat it as a tripwire, not a relocation type.

> ⚡ **QUIRK — `aux` overload for HI-half addend versus byte offset**
> The 4-byte `aux` field at byte 24 (slot0) means different things depending on `action`. For action 7 (`*_HI_*` family) it carries the value `0x20` (32 = "shift right by 32 to extract the high half before insertion"). For actions 22..29 / 30..37 / 38..45 / 47..54 (the eight-way 8-bit patch families) it carries the byte offset within the target word — 0, 8, 16, ... 56 — exactly mirroring the type-name suffix. The same dword slot therefore encodes either a shift-count constant or a byte-offset constant, with the action enum being the sole disambiguator. Pre-decoding code that treats `aux` as a single semantic field will misread one family or the other.

> ⚡ **QUIRK — attribute table reuses payload format unchanged**
> The 65 attribute descriptors at `off_1D3CBE0` are not a separate format — they share the exact same 64-byte layout and the same `apply_class` / `action` / slot semantics as the standard table. The attribute bit (`r_type & 0x10000`) only selects which table the engine indexes (`sub_469620` lines 31--38; `sub_42F6C0` line 14: `a1 -= 0x10000`). String pointers in attribute-table records point into the same `.rodata` string pool used by the standard table, which is why the two tables share names by pointer equality. No attribute-specific action codes exist; the only difference is the consumer — the `.nv.info.*` parser for attribute relocations versus the instruction-stream patcher for standard ones.

### Field cross-check against consumer code

- `sub_469620` line 43: `v5 = HIDWORD(v3[8 * idx + 2])` reads QWORD index `8*idx + 2` from `off_1D3DBE0` cast to `_QWORD *`. That is byte offset `64*idx + 16`; `HIDWORD` of that QWORD is bytes 20..23 = `slot0.action`. Bit-tested against `0x3FFFE002C6`.
- `sub_4698A0` line 37: `*((_DWORD *)&off_1D3DBE0 + 16 * idx + 5)` — with 4-byte DWORD stride, that is byte offset `64*idx + 20` = `slot0.action`. The check `action - 12 <= 3` selects actions 12, 13, 14, 15 — exactly the `R_CUDA_FUNC_DESC32_*` family.
- `sub_469D60` line 309: `*((_DWORD *)v24 + 5) == 16` checks `slot0.action == 16`, i.e. `R_CUDA_PCREL_IMM24_*` — gating the PC-relative branch-offset path.
- `sub_468760` (the application engine): iterates the three 16-byte slots from `desc+12` to `desc+60` using `action += 4` (four DWORDs = 16 bytes per slot), confirming the 16-byte stride between slots and the 4-byte sentinel terminator at byte 60.


- [Relocation Application Engine](../linker/relocation-engine.md) — bit-field patching engine that consumes these descriptors
- [Bindless Relocations](../linker/bindless-relocations.md) — resolution pipeline for bindless texture/surface types
- [R_MERCURY Relocation Catalog](r-mercury-catalog.md) — parallel catalog for Mercury (sm >= 100) types
- [Binary Layout](../binary-layout.md) — addresses of descriptor tables within the nvlink binary

## Confidence Assessment

**Verification scope:** All 117 standard-table names and 65 attribute-table indices were cross-checked against `nvlink_strings.json` (exact-string match) and the decompiled table dispatcher `sub_42F6C0` (`nvlink/decompiled/sub_42F6C0_0x42f6c0.c`). Ten entries were spot-checked end-to-end: string presence, string address, and relocation-engine consumption of `off_1D3DBE0`.

**Totals:** 117 standard entries + 65 attribute entries = **182 catalog rows**. String verification: **119 / 119** raw strings in `nvlink_strings.json` (includes two trailing-whitespace duplicates of `R_CUDA_UNIFIED_8_0` and `R_CUDA_UNIFIED_8_8` at `0x1d3cb9f` / `0x1d3cbb3`) collapse to the 117 canonical names after trimming. **Verified: 117 / 117 canonical names. Unverified: 0.**

### Spot-check table (10 entries)

| Entry | Confidence | Evidence |
|-------|-----------|----------|
| `R_CUDA_NONE` (idx 0) | HIGH | String at `0x1D35F60` (`nvlink_strings.json`); dispatched via `off_1D37600[0]` in `sub_42F6C0` (line 25); sentinel no-op path in relocation engine |
| `R_CUDA_G64` (idx 4) | HIGH | String at `0x1D35F8B`; reachable via `off_1D37600[4]` in `sub_42F6C0`; descriptor consumed by `sub_469620` (line 27: `v8 = &off_1D3DBE0`) |
| `R_CUDA_TEX_HEADER_INDEX` (idx 6) | HIGH | String at `0x1D35FA6`; texture-binding path in bindless relocation engine |
| `R_CUDA_CONST_FIELD19_28` (idx 24) | HIGH | String at `0x1D36108`; 19-bit bitfield encoding at position 28 matches the name suffix |
| `R_CUDA_FUNC_DESC_32` (idx 34) | HIGH | String at `0x1D361E4`; descriptor table entry consumed by `sub_46ADC0` (line 135: `v90 = &off_1D3DBE0`) — resolved relocations emitter |
| `R_CUDA_PCREL_IMM24_26` (idx 40) | HIGH | String at `0x1D3626B`; PC-relative branch handler in relocation engine |
| `R_CUDA_INSTRUCTION64` (idx 53) | HIGH | String at `0x1D3637E`; whole-instruction replacement path in `sub_469D60` (line 214: `v152 = &off_1D3DBE0`) |
| `R_CUDA_BINDLESSOFF14_40` (idx 65) | HIGH | String at `0x1D36471`; bindless-offset handler in texture relocation pipeline |
| `R_CUDA_YIELD_OPCODE9_0` (idx 68) | HIGH | String at `0x1D364B7`; YIELD-opcode rewrite path in `sub_469D60`; cross-referenced in ptxas wiki (`relocations.md` line 244) |
| `R_CUDA_NONE_LAST` (idx 116) | HIGH | String at `0x1D36808`; end-of-range sentinel — bounds `a1 >= 0x75` check in `sub_42F6C0` (line 26) |

**Cross-reference with the [ptxas Relocations](../../ptxas/output/relocations.html) page:** The ten spot-checked names match verbatim against ptxas lines 80, 124, 140, 155, 179, 195, 217, 242, 244, 248. No ordinal drift between ptxas and nvlink for these entries.

| Aspect | Confidence | Basis |
|---|---|---|
| Type names (all 117 entries) | HIGH | 117/117 canonical names found in `nvlink_strings.json` at addresses `0x1D35F60`--`0x1D36808` (contiguous string-pool region); no missing entries |
| Standard table address (`off_1D37600`) | HIGH | Confirmed in decompiled `sub_42F6C0` line 25 (`v7 = &off_1D37600`); bounds check `a1 >= 0x75` (117 = 0x75) at line 26 |
| Attribute table address (`off_1D371E0`) | HIGH | Confirmed in decompiled `sub_42F6C0` line 17 (`v7 = &off_1D371E0`); bounds check `a1 < 0x41` (65 = 0x41) at line 18 after `a1 -= 0x10000` |
| Descriptor table address (`off_1D3DBE0`) | HIGH | Referenced by three relocation engines: `sub_469620` (line 27), `sub_46ADC0` (line 135), `sub_469D60` (line 214) |
| Entry count (117 standard, 65 attribute) | HIGH | Table bounds encoded as immediate constants in `sub_42F6C0`: `v6 = 117` (line 24) and `v6 = 65` (line 16) |
| Index-to-name mapping | HIGH | Name strings are stored as 16-byte records (ptr, 3xDWORD metadata) at `off_1D37600`; `sub_42F6C0` line 29: `v8 = &v7[2 * a1]` confirms 16-byte stride |
| Bit width / bit position columns | HIGH | Byte-decoded from the `width` (offset 16) and `shift` (offset 12) dwords inside the 64-byte descriptors at `off_1D3DBE0`. Values match the type-name suffix convention for every entry where the suffix is unambiguous (see `Descriptor Byte Layout` section). |
| Category classification | MEDIUM | Inferred from naming patterns (`ABS` = absolute, `PCREL` = PC-relative, `CONST_FIELD` = constant bank, `FUNC_DESC` = function descriptor, etc.); no explicit category field in the binary |
| Attribute table shared-name mapping | MEDIUM | Attribute entries at `off_1D371E0` share string pointers with the standard table at `off_1D37600` by pointer equality; the 1:1 index mapping is inferred from array position, not from an explicit mapping table. Gaps in the attribute table (e.g. indices beyond 0x40) fall through to the `"unknown attribute"` diagnostic at `sub_42F6C0` line 21 |
| Action-code bit-field interpretation | HIGH | The 64-byte descriptor layout (`name_ptr`, `kind`, `shift`, `width`, `action`, `extra[10]`) was byte-decoded for all 117 entries from `nvlink_rodata.bin` at rodata offset `0xBA40`. The `action` dword position (bytes 20..23) is confirmed by three independent consumer accesses: `sub_4698A0` line 37 (`...+ 16*idx + 5` DWORD = byte 20), `sub_469620` line 43 (`HIDWORD(v3[8*idx+2])` = byte 20), and `sub_469D60` line 309 (`*((_DWORD *)v24 + 5) == 16` gating the PC-rel path). |
| Descriptor `kind` field (byte 8 dword) | HIGH | Distribution across the 117 entries is fully accounted for: kind=0 (sentinel, 1), kind=1 (instr bit-fields, 39), kind=2 (descriptors/globals/instr-replace, 52), kind=4 (data/byte, 24), kind=0xFF00 (poison sentinel, 1). |
| Split-field `extra[]` continuations | MEDIUM | The piece-count + `(shift_n, width_n, ...)` interpretation matches every observed entry: `R_CUDA_ABS55_16_34` (8 + 47 = 55), `R_CUDA_CONST_FIELD19_28` (14 + 4 + 1 = 19), `R_CUDA_ABS56_16_34` (8 + 48 = 56). The exact role of the per-piece trailing dwords (values 10, 11, 21) is not yet decoded but they appear constant across same-class entries. |
| Attribute table shared 64-byte format | HIGH | `sub_469620` lines 26--38 select between `off_1D3DBE0` and `off_1D3CBE0` purely by the `r_type & 0x10000` bit. The attribute table follows the same 64-byte stride and the same field-by-field layout. |
