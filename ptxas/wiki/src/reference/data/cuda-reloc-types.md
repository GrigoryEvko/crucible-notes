# R_CUDA Relocation Types

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The complete `R_CUDA_*` relocation enumeration ptxas emits into SASS cubins, re-extracted from the binary's name-pointer table at VMA `0x23FD4C0` (stride 16 B, 117 entries 0..116, terminating on the first NULL pointer) and the parallel action-record table at file offset `0x2000080` (64 B stride). Each entry's name maps 1:1 to its ELF relocation type code (the `abi_value` column). Column meanings:

- **handler** — the resolver class from the action table's handler field: `NVRH_LINKER` (nvlink resolves), `NVRH_DRIVER` (CUDA driver resolves), `NVRH_EITHER` (linker or driver), `NVRH_NONE` (no action).
- **bit actions** — the SASS instruction-word field each reloc patches: `bits[a..b]=VALUE>>shift`.

Codes 114 and 115 are present only in this build; `R_CUDA_NONE_LAST` sits at 116.

| Code | Name | Handler | Bit actions |
|---:|---|---|---|
| 0 | `R_CUDA_NONE` | NVRH_NONE | (none) |
| 1 | `R_CUDA_32` | NVRH_EITHER | bits[0..31]=ADDR>>0 |
| 2 | `R_CUDA_64` | NVRH_EITHER | bits[0..63]=ADDR>>0 |
| 3 | `R_CUDA_G32` | NVRH_DRIVER | bits[0..31]=GADDR>>0 |
| 4 | `R_CUDA_G64` | NVRH_DRIVER | bits[0..63]=GADDR>>0 |
| 5 | `R_CUDA_ABS32_26` | NVRH_EITHER | bits[26..57]=ADDR>>0 |
| 6 | `R_CUDA_TEX_HEADER_INDEX` | NVRH_DRIVER | bits[0..19]=HEADER>>0 |
| 7 | `R_CUDA_SAMP_HEADER_INDEX` | NVRH_DRIVER | bits[20..31]=HEADER>>0 |
| 8 | `R_CUDA_SURF_HW_DESC` | NVRH_DRIVER | bits[0..31]=HWDESC>>0 |
| 9 | `R_CUDA_SURF_HW_SW_DESC` | NVRH_DRIVER | bits[0..31]=HWSWDESC>>0 |
| 10 | `R_CUDA_ABS32_LO_26` | NVRH_EITHER | bits[26..57]=LOADDR>>0 |
| 11 | `R_CUDA_ABS32_HI_26` | NVRH_EITHER | bits[26..57]=HIADDR>>32 |
| 12 | `R_CUDA_ABS32_23` | NVRH_EITHER | bits[23..54]=ADDR>>0 |
| 13 | `R_CUDA_ABS32_LO_23` | NVRH_EITHER | bits[23..54]=LOADDR>>0 |
| 14 | `R_CUDA_ABS32_HI_23` | NVRH_EITHER | bits[23..54]=HIADDR>>32 |
| 15 | `R_CUDA_ABS24_26` | NVRH_LINKER | bits[26..49]=ADDR>>0 |
| 16 | `R_CUDA_ABS24_23` | NVRH_LINKER | bits[23..46]=ADDR>>0 |
| 17 | `R_CUDA_ABS16_26` | NVRH_LINKER | bits[26..41]=ADDR>>0 |
| 18 | `R_CUDA_ABS16_23` | NVRH_LINKER | bits[23..38]=ADDR>>0 |
| 19 | `R_CUDA_TEX_SLOT` | NVRH_LINKER | bits[32..39]=SLOT>>0 |
| 20 | `R_CUDA_SAMP_SLOT` | NVRH_LINKER | bits[40..44]=SLOT>>0 |
| 21 | `R_CUDA_SURF_SLOT` | NVRH_LINKER | bits[26..31]=SLOT>>0 |
| 22 | `R_CUDA_TEX_BINDLESSOFF13_32` | NVRH_LINKER | bits[32..44]=SHIFT2>>0 |
| 23 | `R_CUDA_TEX_BINDLESSOFF13_47` | NVRH_LINKER | bits[47..59]=SHIFT2>>0 |
| 24 | `R_CUDA_CONST_FIELD19_28` | NVRH_LINKER | bits[28..41]=SHIFT2>>0 ; bits[42..45]=CBANK>>0 ; bits[26..26]=CBANK4>>0 |
| 25 | `R_CUDA_CONST_FIELD19_23` | NVRH_LINKER | bits[23..36]=SHIFT2>>0 ; bits[37..41]=CBANK>>0 |
| 26 | `R_CUDA_TEX_SLOT9_49` | NVRH_LINKER | bits[49..57]=SLOT>>0 |
| 27 | `R_CUDA_6_31` | NVRH_LINKER | bits[31..36]=ADDR>>0 |
| 28 | `R_CUDA_2_47` | NVRH_LINKER | bits[47..48]=ADDR>>0 |
| 29 | `R_CUDA_TEX_BINDLESSOFF13_41` | NVRH_LINKER | bits[41..53]=SHIFT2>>0 |
| 30 | `R_CUDA_TEX_BINDLESSOFF13_45` | NVRH_LINKER | bits[45..57]=SHIFT2>>0 |
| 31 | `R_CUDA_FUNC_DESC32_23` | NVRH_DRIVER | bits[23..54]=FDESC>>0 |
| 32 | `R_CUDA_FUNC_DESC32_LO_23` | NVRH_DRIVER | bits[23..54]=LOFDESC>>0 |
| 33 | `R_CUDA_FUNC_DESC32_HI_23` | NVRH_DRIVER | bits[23..54]=HIFDESC>>32 |
| 34 | `R_CUDA_FUNC_DESC_32` | NVRH_DRIVER | bits[0..31]=FDESCA>>0 |
| 35 | `R_CUDA_FUNC_DESC_64` | NVRH_DRIVER | bits[0..63]=FDESCA>>0 |
| 36 | `R_CUDA_CONST_FIELD21_26` | NVRH_LINKER | bits[26..41]=ADDR>>0 ; bits[42..46]=CBANK>>0 |
| 37 | `R_CUDA_QUERY_DESC21_37` | NVRH_LINKER | bits[37..52]=ADDR>>0 ; bits[53..57]=CBANK>>0 |
| 38 | `R_CUDA_CONST_FIELD19_26` | NVRH_LINKER | bits[26..39]=SHIFT2>>0 ; bits[40..44]=CBANK>>0 |
| 39 | `R_CUDA_CONST_FIELD21_23` | NVRH_LINKER | bits[23..38]=ADDR>>0 ; bits[39..43]=CBANK>>0 |
| 40 | `R_CUDA_PCREL_IMM24_26` | NVRH_LINKER | bits[26..49]=PCREL>>0 |
| 41 | `R_CUDA_PCREL_IMM24_23` | NVRH_LINKER | bits[23..46]=PCREL>>0 |
| 42 | `R_CUDA_ABS32_20` | NVRH_EITHER | bits[20..51]=ADDR>>0 |
| 43 | `R_CUDA_ABS32_LO_20` | NVRH_EITHER | bits[20..51]=LOADDR>>0 |
| 44 | `R_CUDA_ABS32_HI_20` | NVRH_EITHER | bits[20..51]=HIADDR>>32 |
| 45 | `R_CUDA_ABS24_20` | NVRH_LINKER | bits[20..43]=ADDR>>0 |
| 46 | `R_CUDA_ABS16_20` | NVRH_LINKER | bits[20..35]=ADDR>>0 |
| 47 | `R_CUDA_FUNC_DESC32_20` | NVRH_DRIVER | bits[20..51]=FDESC>>0 |
| 48 | `R_CUDA_FUNC_DESC32_LO_20` | NVRH_DRIVER | bits[20..51]=LOFDESC>>0 |
| 49 | `R_CUDA_FUNC_DESC32_HI_20` | NVRH_DRIVER | bits[20..51]=HIFDESC>>32 |
| 50 | `R_CUDA_CONST_FIELD19_20` | NVRH_LINKER | bits[20..33]=SHIFT2>>0 ; bits[34..38]=CBANK>>0 |
| 51 | `R_CUDA_BINDLESSOFF13_36` | NVRH_LINKER | bits[36..48]=SHIFT2>>0 |
| 52 | `R_CUDA_SURF_HEADER_INDEX` | NVRH_DRIVER | bits[0..19]=HEADER>>0 |
| 53 | `R_CUDA_INSTRUCTION64` | NVRH_DRIVER | bits[0..63]=INST>>0 |
| 54 | `R_CUDA_CONST_FIELD21_20` | NVRH_LINKER | bits[20..35]=ADDR>>0 ; bits[36..40]=CBANK>>0 |
| 55 | `R_CUDA_ABS32_32` | NVRH_EITHER | bits[32..63]=ADDR>>0 |
| 56 | `R_CUDA_ABS32_LO_32` | NVRH_EITHER | bits[32..63]=LOADDR>>0 |
| 57 | `R_CUDA_ABS32_HI_32` | NVRH_EITHER | bits[32..63]=HIADDR>>32 |
| 58 | `R_CUDA_ABS47_34` | NVRH_DRIVER | bits[34..80]=SHIFT2>>0 |
| 59 | `R_CUDA_ABS16_32` | NVRH_LINKER | bits[32..47]=ADDR>>0 |
| 60 | `R_CUDA_ABS24_32` | NVRH_LINKER | bits[32..55]=ADDR>>0 |
| 61 | `R_CUDA_FUNC_DESC32_32` | NVRH_DRIVER | bits[32..63]=FDESC>>0 |
| 62 | `R_CUDA_FUNC_DESC32_LO_32` | NVRH_DRIVER | bits[32..63]=LOFDESC>>0 |
| 63 | `R_CUDA_FUNC_DESC32_HI_32` | NVRH_DRIVER | bits[32..63]=HIFDESC>>32 |
| 64 | `R_CUDA_CONST_FIELD19_40` | NVRH_LINKER | bits[40..53]=SHIFT2>>0 ; bits[54..58]=CBANK>>0 |
| 65 | `R_CUDA_BINDLESSOFF14_40` | NVRH_LINKER | bits[40..53]=SHIFT2>>0 |
| 66 | `R_CUDA_CONST_FIELD21_38` | NVRH_LINKER | bits[38..53]=ADDR>>0 ; bits[54..58]=CBANK>>0 |
| 67 | `R_CUDA_INSTRUCTION128` | NVRH_DRIVER | bits[0..127]=INST>>0 |
| 68 | `R_CUDA_YIELD_OPCODE9_0` | NVRH_LINKER | bits[0..8]=INST_OPCODE>>0 |
| 69 | `R_CUDA_YIELD_CLEAR_PRED4_87` | NVRH_LINKER | bits[87..90]=INST_FIELD_CLEAR>>0 |
| 70 | `R_CUDA_32_LO` | NVRH_EITHER | bits[0..31]=LOADDR>>0 |
| 71 | `R_CUDA_32_HI` | NVRH_EITHER | bits[0..31]=HIADDR>>32 |
| 72 | `R_CUDA_UNUSED_CLEAR32` | NVRH_LINKER | bits[0..31]=CLEAR>>0 |
| 73 | `R_CUDA_UNUSED_CLEAR64` | NVRH_LINKER | bits[0..63]=CLEAR>>0 |
| 74 | `R_CUDA_ABS24_40` | NVRH_LINKER | bits[40..63]=ADDR>>0 |
| 75 | `R_CUDA_ABS55_16_34` | NVRH_DRIVER | bits[16..23]=SHIFT2>>2 ; bits[34..80]=SHIFT8>>10 |
| 76 | `R_CUDA_8_0` | NVRH_EITHER | bits[0..7]=ADDR8_0>>0 |
| 77 | `R_CUDA_8_8` | NVRH_EITHER | bits[0..7]=ADDR8_8>>8 |
| 78 | `R_CUDA_8_16` | NVRH_EITHER | bits[0..7]=ADDR8_16>>16 |
| 79 | `R_CUDA_8_24` | NVRH_EITHER | bits[0..7]=ADDR8_24>>24 |
| 80 | `R_CUDA_8_32` | NVRH_EITHER | bits[0..7]=ADDR8_32>>32 |
| 81 | `R_CUDA_8_40` | NVRH_EITHER | bits[0..7]=ADDR8_40>>40 |
| 82 | `R_CUDA_8_48` | NVRH_EITHER | bits[0..7]=ADDR8_48>>48 |
| 83 | `R_CUDA_8_56` | NVRH_EITHER | bits[0..7]=ADDR8_56>>56 |
| 84 | `R_CUDA_G8_0` | NVRH_DRIVER | bits[0..7]=GADDR8_0>>0 |
| 85 | `R_CUDA_G8_8` | NVRH_DRIVER | bits[0..7]=GADDR8_8>>8 |
| 86 | `R_CUDA_G8_16` | NVRH_DRIVER | bits[0..7]=GADDR8_16>>16 |
| 87 | `R_CUDA_G8_24` | NVRH_DRIVER | bits[0..7]=GADDR8_24>>24 |
| 88 | `R_CUDA_G8_32` | NVRH_DRIVER | bits[0..7]=GADDR8_32>>32 |
| 89 | `R_CUDA_G8_40` | NVRH_DRIVER | bits[0..7]=GADDR8_40>>40 |
| 90 | `R_CUDA_G8_48` | NVRH_DRIVER | bits[0..7]=GADDR8_48>>48 |
| 91 | `R_CUDA_G8_56` | NVRH_DRIVER | bits[0..7]=GADDR8_56>>56 |
| 92 | `R_CUDA_FUNC_DESC_8_0` | NVRH_DRIVER | bits[0..7]=FDESC8_0>>0 |
| 93 | `R_CUDA_FUNC_DESC_8_8` | NVRH_DRIVER | bits[0..7]=FDESC8_8>>8 |
| 94 | `R_CUDA_FUNC_DESC_8_16` | NVRH_DRIVER | bits[0..7]=FDESC8_16>>16 |
| 95 | `R_CUDA_FUNC_DESC_8_24` | NVRH_DRIVER | bits[0..7]=FDESC8_24>>24 |
| 96 | `R_CUDA_FUNC_DESC_8_32` | NVRH_DRIVER | bits[0..7]=FDESC8_32>>32 |
| 97 | `R_CUDA_FUNC_DESC_8_40` | NVRH_DRIVER | bits[0..7]=FDESC8_40>>40 |
| 98 | `R_CUDA_FUNC_DESC_8_48` | NVRH_DRIVER | bits[0..7]=FDESC8_48>>48 |
| 99 | `R_CUDA_FUNC_DESC_8_56` | NVRH_DRIVER | bits[0..7]=FDESC8_56>>56 |
| 100 | `R_CUDA_ABS20_44` | NVRH_LINKER | bits[44..63]=ADDR>>0 |
| 101 | `R_CUDA_SAMP_HEADER_INDEX_0` | NVRH_DRIVER | bits[0..11]=HEADER>>0 |
| 102 | `R_CUDA_UNIFIED` | NVRH_DRIVER | bits[0..63]=UADDR>>0 |
| 103 | `R_CUDA_UNIFIED_32` | NVRH_DRIVER | bits[0..31]=UADDR>>0 |
| 104 | `R_CUDA_UNIFIED_8_0` | NVRH_DRIVER | bits[0..7]=UADDR8_0>>0 |
| 105 | `R_CUDA_UNIFIED_8_8` | NVRH_DRIVER | bits[0..7]=UADDR8_8>>8 |
| 106 | `R_CUDA_UNIFIED_8_16` | NVRH_DRIVER | bits[0..7]=UADDR8_16>>16 |
| 107 | `R_CUDA_UNIFIED_8_24` | NVRH_DRIVER | bits[0..7]=UADDR8_24>>24 |
| 108 | `R_CUDA_UNIFIED_8_32` | NVRH_DRIVER | bits[0..7]=UADDR8_32>>32 |
| 109 | `R_CUDA_UNIFIED_8_40` | NVRH_DRIVER | bits[0..7]=UADDR8_40>>40 |
| 110 | `R_CUDA_UNIFIED_8_48` | NVRH_DRIVER | bits[0..7]=UADDR8_48>>48 |
| 111 | `R_CUDA_UNIFIED_8_56` | NVRH_DRIVER | bits[0..7]=UADDR8_56>>56 |
| 112 | `R_CUDA_UNIFIED32_LO_32` | NVRH_DRIVER | bits[32..63]=LOUADDR>>0 |
| 113 | `R_CUDA_UNIFIED32_HI_32` | NVRH_DRIVER | bits[32..63]=HIUADDR>>32 |
| 114 | `R_CUDA_ABS56_16_34` | NVRH_DRIVER | bits[16..23]=SHIFT2>>2 ; bits[34..81]=SHIFT8>>10 |
| 115 | `R_CUDA_CONST_FIELD22_37` | NVRH_LINKER | bits[37..53]=ADDR>>0 ; bits[54..58]=CBANK>>0 |
| 116 | `R_CUDA_NONE_LAST` | NVRH_NONE | (none) |

## Handler distribution

The handler field is decoded directly from the action-record table. In this build the enum is `0=NVRH_NONE`, `1=NVRH_LINKER` (nvlink — 39 `R_CUDA_*` types), `2=NVRH_DRIVER` (CUDA runtime/driver — 52 types), `3=NVRH_FINALIZER` (no `R_CUDA_*` type uses it — it is reserved for the Mercury `PROG_REL*` family), `4=NVRH_EITHER` (linker-or-driver — 24 types). Older ptxas builds used a four-value enum `{NONE, LINKER, DRIVER, EITHER}=0..3`; this build inserts `FINALIZER` at value 3 and pushes `EITHER` to 4, so the bracketed numeric values shifted even though the semantic class names above are stable.
