# R_CUDA Relocation Catalog

Complete reference table of all **117 canonical** R_CUDA relocation type names extracted from nvlink v13.0.88, indexed across **182 total descriptor rows** (117 standard + 65 attribute). The raw `nvlink_strings.json` pool contains 119 R_CUDA string entries -- 117 unique names plus two trailing-whitespace duplicates of `R_CUDA_UNIFIED_8_0` and `R_CUDA_UNIFIED_8_8` (see Confidence Assessment). Types are organized into two descriptor tables: the standard table at `off_1D37600` (117 entries, indices 0--116) and the attribute table at `off_1D371E0` (65 entries, indices 0x10000--0x10040). See [R_CUDA Relocations](../linker/r-cuda-relocations.md) for the relocation engine, descriptor format, action types, and architecture class system.

## Standard Table (off_1D37600)

117 entries. Index = raw `r_type` value in `.rela.*` sections. Bit width and bit position are derived from the type name and confirmed against the 64-byte descriptors at `off_1D3DBE0`.

| Index | Name | Type | Bit Width | Bit Position |
|------:|------|----------|----------:|-------------:|
| 0 | `R_CUDA_NONE` | sentinel | -- | -- |
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
| 116 | `R_CUDA_NONE_LAST` | sentinel | -- | -- |

## Attribute Table (off_1D371E0)

65 entries. Attribute relocations apply to `.nv.info.*` metadata sections rather than instruction streams. The encoded `r_type` in ELF is `0x10000 + index` (e.g., attribute index 3 = `r_type` 0x10003). The attribute table reuses a subset of the same type names as the standard table but with different 64-byte descriptors.

The attribute table entries are not individually named in the binary -- they share string pointers with the standard table. The following mapping lists the 65 attribute indices and the standard-table name each one references. Indices not listed here (gaps) use `R_CUDA_NONE`.

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

## Cross-References

- [R_CUDA Relocations](../linker/r-cuda-relocations.md) -- descriptor format, action types, architecture class system, per-type semantics
- [Relocation Application Engine](../linker/relocation-engine.md) -- bit-field patching engine that consumes these descriptors
- [Bindless Relocations](../linker/bindless-relocations.md) -- resolution pipeline for bindless texture/surface types
- [R_MERCURY Relocation Catalog](r-mercury-catalog.md) -- parallel catalog for Mercury (sm >= 100) types
- [Binary Layout](../binary-layout.md) -- addresses of descriptor tables within the nvlink binary

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
| `R_CUDA_FUNC_DESC_32` (idx 34) | HIGH | String at `0x1D361E4`; descriptor table entry consumed by `sub_46ADC0` (line 135: `v90 = &off_1D3DBE0`) -- resolved relocations emitter |
| `R_CUDA_PCREL_IMM24_26` (idx 40) | HIGH | String at `0x1D3626B`; PC-relative branch handler in relocation engine |
| `R_CUDA_INSTRUCTION64` (idx 53) | HIGH | String at `0x1D3637E`; whole-instruction replacement path in `sub_469D60` (line 214: `v152 = &off_1D3DBE0`) |
| `R_CUDA_BINDLESSOFF14_40` (idx 65) | HIGH | String at `0x1D36471`; bindless-offset handler in texture relocation pipeline |
| `R_CUDA_YIELD_OPCODE9_0` (idx 68) | HIGH | String at `0x1D364B7`; YIELD-opcode rewrite path in `sub_469D60`; cross-referenced in ptxas wiki (`relocations.md` line 244) |
| `R_CUDA_NONE_LAST` (idx 116) | HIGH | String at `0x1D36808`; end-of-range sentinel -- bounds `a1 >= 0x75` check in `sub_42F6C0` (line 26) |

**Cross-reference with the [ptxas Relocations](../../ptxas/output/relocations.html) page:** The ten spot-checked names match verbatim against ptxas lines 80, 124, 140, 155, 179, 195, 217, 242, 244, 248. No ordinal drift between ptxas and nvlink for these entries.

| Aspect | Confidence | Basis |
|---|---|---|
| Type names (all 117 entries) | HIGH | 117/117 canonical names found in `nvlink_strings.json` at addresses `0x1D35F60`--`0x1D36808` (contiguous string-pool region); no missing entries |
| Standard table address (`off_1D37600`) | HIGH | Confirmed in decompiled `sub_42F6C0` line 25 (`v7 = &off_1D37600`); bounds check `a1 >= 0x75` (117 = 0x75) at line 26 |
| Attribute table address (`off_1D371E0`) | HIGH | Confirmed in decompiled `sub_42F6C0` line 17 (`v7 = &off_1D371E0`); bounds check `a1 < 0x41` (65 = 0x41) at line 18 after `a1 -= 0x10000` |
| Descriptor table address (`off_1D3DBE0`) | HIGH | Referenced by three relocation engines: `sub_469620` (line 27), `sub_46ADC0` (line 135), `sub_469D60` (line 214) |
| Entry count (117 standard, 65 attribute) | HIGH | Table bounds encoded as immediate constants in `sub_42F6C0`: `v6 = 117` (line 24) and `v6 = 65` (line 16) |
| Index-to-name mapping | HIGH | Name strings are stored as 16-byte records (ptr, 3xDWORD metadata) at `off_1D37600`; `sub_42F6C0` line 29: `v8 = &v7[2 * a1]` confirms 16-byte stride |
| Bit width / bit position columns | MEDIUM | Parsed by this catalog from type-name suffix convention (`ABS32_26` = 32-bit field at bit 26); the 64-byte descriptors at `off_1D3DBE0` encode these values but were not byte-for-byte decoded for every entry |
| Category classification | MEDIUM | Inferred from naming patterns (`ABS` = absolute, `PCREL` = PC-relative, `CONST_FIELD` = constant bank, `FUNC_DESC` = function descriptor, etc.); no explicit category field in the binary |
| Attribute table shared-name mapping | MEDIUM | Attribute entries at `off_1D371E0` share string pointers with the standard table at `off_1D37600` by pointer equality; the 1:1 index mapping is inferred from array position, not from an explicit mapping table. Gaps in the attribute table (e.g. indices beyond 0x40) fall through to the `"unknown attribute"` diagnostic at `sub_42F6C0` line 21 |
| Action-code bit-field interpretation | LOW | The 64-byte descriptor layout at `off_1D3DBE0` (bit width, shift, action enum, arch-class bitmask) is inferred from consumer code patterns in `sub_469620`/`sub_46ADC0`/`sub_469D60`, not documented as a concrete struct in the binary |
