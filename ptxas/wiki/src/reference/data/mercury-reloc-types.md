# R_MERCURY Relocation Types

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The 64-entry `R_MERCURY_*` relocation catalog, decoded byte-exact from the `mercury_reloc_info[]` table in `.rodata` at VMA `0x23FF080` (stride `0x40` = 64 B per entry; entries 0..63 carry ELF type codes `0x10000..0x1003F`). Entry 64 (type `0x10040`) is the first record of the contiguous `sass_reloc_info[]` (`R_CUDA_*`) table that follows immediately in `.rodata` — the two relocation tables are adjacent, which is why the master resolver `sub_1CD48C0` indexes Mercury relocs as `mercury_reloc_info[r_type − 0x10000]`.

Each entry's layout is `{ const char *name (8 B); uint32 handler; action[3] × {uint32 start_bit, num_bits, sym_value, src_start_bit} }`. The **handler** field (`NVRH_*`) drives the finalizer-vs-driver split: only the `PROG_REL` family (`PROG_REL64/32/32_LO/32_HI` plus the byte-lane `PROG_REL8_0..56`) is `NVRH_FINALIZER` and is resolved by the Mercury finalizer in place; everything else is deferred to the driver or linker. The **sym_value** column names the `NVRS_*` symbol-value code that selects what value gets patched in (full address, low/high half, byte lane, PC-relative delta, unified address, or clear-to-zero).

`UNIFIED32_LO` (`0x1003E`) and `UNIFIED32_HI` (`0x1003F`) are present only in this build, using sym_value codes 55/56 (`NVRS_LOUADDR` / `NVRS_HIUADDR`); older ptxas builds ended the table at `R_MERCURY_ABS_PROG_REL64` (`0x1003D`) followed by the sentinel. The remaining 62 entries are byte-identical across builds.

| Type | Name | Handler | Bit action | sym_value |
|---|---|---|---|---|
| `0x10000` | `R_MERCURY_NONE` | NVRH_NONE | (none) | NVRS_NONE |
| `0x10001` | `R_MERCURY_G64` | NVRH_DRIVER | bits[0..63] | NVRS_ADDR |
| `0x10002` | `R_MERCURY_ABS64` | NVRH_EITHER | bits[0..63] | NVRS_ADDR |
| `0x10003` | `R_MERCURY_ABS32` | NVRH_EITHER | bits[0..31] | NVRS_ADDR |
| `0x10004` | `R_MERCURY_ABS16` | NVRH_EITHER | bits[0..15] | NVRS_ADDR |
| `0x10005` | `R_MERCURY_ABS32_LO` | NVRH_DRIVER | bits[0..31] | NVRS_LOADDR |
| `0x10006` | `R_MERCURY_ABS32_HI` | NVRH_DRIVER | bits[0..31] (>>32) | NVRS_HIADDR |
| `0x10007` | `R_MERCURY_PROG_REL64` | NVRH_FINALIZER | bits[0..63] | NVRS_ADDR |
| `0x10008` | `R_MERCURY_PROG_REL32` | NVRH_FINALIZER | bits[0..31] | NVRS_ADDR |
| `0x10009` | `R_MERCURY_PROG_REL32_LO` | NVRH_FINALIZER | bits[0..31] | NVRS_ADDR |
| `0x1000a` | `R_MERCURY_PROG_REL32_HI` | NVRH_FINALIZER | bits[0..31] (>>32) | NVRS_ADDR |
| `0x1000b` | `R_MERCURY_TEX_HEADER_INDEX` | NVRH_DRIVER | bits[0..19] | NVRS_HEADER |
| `0x1000c` | `R_MERCURY_SAMP_HEADER_INDEX` | NVRH_DRIVER | bits[20..31] | NVRS_HEADER |
| `0x1000d` | `R_MERCURY_SURF_HEADER_INDEX` | NVRH_DRIVER | bits[0..19] | NVRS_HEADER |
| `0x1000e` | `R_MERCURY_UNUSED_CLEAR64` | NVRH_LINKER | bits[0..63] | NVRS_CLEAR |
| `0x1000f` | `R_MERCURY_FUNC_DESC_64` | NVRH_DRIVER | bits[0..63] | NVRS_FDESCA |
| `0x10010` | `R_MERCURY_8_0` | NVRH_EITHER | bits[0..7] | NVRS_ADDR8_0 |
| `0x10011` | `R_MERCURY_8_8` | NVRH_EITHER | bits[0..7] (>>8) | NVRS_ADDR8_8 |
| `0x10012` | `R_MERCURY_8_16` | NVRH_EITHER | bits[0..7] (>>16) | NVRS_ADDR8_16 |
| `0x10013` | `R_MERCURY_8_24` | NVRH_EITHER | bits[0..7] (>>24) | NVRS_ADDR8_24 |
| `0x10014` | `R_MERCURY_8_32` | NVRH_EITHER | bits[0..7] (>>32) | NVRS_ADDR8_32 |
| `0x10015` | `R_MERCURY_8_40` | NVRH_EITHER | bits[0..7] (>>40) | NVRS_ADDR8_40 |
| `0x10016` | `R_MERCURY_8_48` | NVRH_EITHER | bits[0..7] (>>48) | NVRS_ADDR8_48 |
| `0x10017` | `R_MERCURY_8_56` | NVRH_EITHER | bits[0..7] (>>56) | NVRS_ADDR8_56 |
| `0x10018` | `R_MERCURY_G8_0` | NVRH_DRIVER | bits[0..7] | NVRS_GADDR8_0 |
| `0x10019` | `R_MERCURY_G8_8` | NVRH_DRIVER | bits[0..7] (>>8) | NVRS_GADDR8_8 |
| `0x1001a` | `R_MERCURY_G8_16` | NVRH_DRIVER | bits[0..7] (>>16) | NVRS_GADDR8_16 |
| `0x1001b` | `R_MERCURY_G8_24` | NVRH_DRIVER | bits[0..7] (>>24) | NVRS_GADDR8_24 |
| `0x1001c` | `R_MERCURY_G8_32` | NVRH_DRIVER | bits[0..7] (>>32) | NVRS_GADDR8_32 |
| `0x1001d` | `R_MERCURY_G8_40` | NVRH_DRIVER | bits[0..7] (>>40) | NVRS_GADDR8_40 |
| `0x1001e` | `R_MERCURY_G8_48` | NVRH_DRIVER | bits[0..7] (>>48) | NVRS_GADDR8_48 |
| `0x1001f` | `R_MERCURY_G8_56` | NVRH_DRIVER | bits[0..7] (>>56) | NVRS_GADDR8_56 |
| `0x10020` | `R_MERCURY_FUNC_DESC_8_0` | NVRH_DRIVER | bits[0..7] | NVRS_FDESC8_0 |
| `0x10021` | `R_MERCURY_FUNC_DESC_8_8` | NVRH_DRIVER | bits[0..7] (>>8) | NVRS_FDESC8_8 |
| `0x10022` | `R_MERCURY_FUNC_DESC_8_16` | NVRH_DRIVER | bits[0..7] (>>16) | NVRS_FDESC8_16 |
| `0x10023` | `R_MERCURY_FUNC_DESC_8_24` | NVRH_DRIVER | bits[0..7] (>>24) | NVRS_FDESC8_24 |
| `0x10024` | `R_MERCURY_FUNC_DESC_8_32` | NVRH_DRIVER | bits[0..7] (>>32) | NVRS_FDESC8_32 |
| `0x10025` | `R_MERCURY_FUNC_DESC_8_40` | NVRH_DRIVER | bits[0..7] (>>40) | NVRS_FDESC8_40 |
| `0x10026` | `R_MERCURY_FUNC_DESC_8_48` | NVRH_DRIVER | bits[0..7] (>>48) | NVRS_FDESC8_48 |
| `0x10027` | `R_MERCURY_FUNC_DESC_8_56` | NVRH_DRIVER | bits[0..7] (>>56) | NVRS_FDESC8_56 |
| `0x10028` | `R_MERCURY_ABS_PROG_REL32_LO` | NVRH_DRIVER | bits[0..31] | NVRS_PCREL |
| `0x10029` | `R_MERCURY_ABS_PROG_REL32_HI` | NVRH_DRIVER | bits[0..31] (>>32) | NVRS_PCREL |
| `0x1002a` | `R_MERCURY_PROG_REL8_0` | NVRH_FINALIZER | bits[0..7] | NVRS_ADDR8_0 |
| `0x1002b` | `R_MERCURY_PROG_REL8_8` | NVRH_FINALIZER | bits[0..7] (>>8) | NVRS_ADDR8_8 |
| `0x1002c` | `R_MERCURY_PROG_REL8_16` | NVRH_FINALIZER | bits[0..7] (>>16) | NVRS_ADDR8_16 |
| `0x1002d` | `R_MERCURY_PROG_REL8_24` | NVRH_FINALIZER | bits[0..7] (>>24) | NVRS_ADDR8_24 |
| `0x1002e` | `R_MERCURY_PROG_REL8_32` | NVRH_FINALIZER | bits[0..7] (>>32) | NVRS_ADDR8_32 |
| `0x1002f` | `R_MERCURY_PROG_REL8_40` | NVRH_FINALIZER | bits[0..7] (>>40) | NVRS_ADDR8_40 |
| `0x10030` | `R_MERCURY_PROG_REL8_48` | NVRH_FINALIZER | bits[0..7] (>>48) | NVRS_ADDR8_48 |
| `0x10031` | `R_MERCURY_PROG_REL8_56` | NVRH_FINALIZER | bits[0..7] (>>56) | NVRS_ADDR8_56 |
| `0x10032` | `R_MERCURY_UNIFIED` | NVRH_DRIVER | bits[0..31] | NVRS_UADDR |
| `0x10033` | `R_MERCURY_UNIFIED_32` | NVRH_DRIVER | bits[0..63] | NVRS_UADDR |
| `0x10034` | `R_MERCURY_UNIFIED_8_0` | NVRH_DRIVER | bits[0..7] | NVRS_UADDR8_0 |
| `0x10035` | `R_MERCURY_UNIFIED_8_8` | NVRH_DRIVER | bits[0..7] (>>8) | NVRS_UADDR8_8 |
| `0x10036` | `R_MERCURY_UNIFIED_8_16` | NVRH_DRIVER | bits[0..7] (>>16) | NVRS_UADDR8_16 |
| `0x10037` | `R_MERCURY_UNIFIED_8_24` | NVRH_DRIVER | bits[0..7] (>>24) | NVRS_UADDR8_24 |
| `0x10038` | `R_MERCURY_UNIFIED_8_32` | NVRH_DRIVER | bits[0..7] (>>32) | NVRS_UADDR8_32 |
| `0x10039` | `R_MERCURY_UNIFIED_8_40` | NVRH_DRIVER | bits[0..7] (>>40) | NVRS_UADDR8_40 |
| `0x1003a` | `R_MERCURY_UNIFIED_8_48` | NVRH_DRIVER | bits[0..7] (>>48) | NVRS_UADDR8_48 |
| `0x1003b` | `R_MERCURY_UNIFIED_8_56` | NVRH_DRIVER | bits[0..7] (>>56) | NVRS_UADDR8_56 |
| `0x1003c` | `R_MERCURY_ABS_PROG_REL32` | NVRH_DRIVER | bits[0..31] | NVRS_PCREL |
| `0x1003d` | `R_MERCURY_ABS_PROG_REL64` | NVRH_DRIVER | bits[0..63] | NVRS_PCREL |
| `0x1003e` | `R_MERCURY_UNIFIED32_LO` | NVRH_DRIVER | bits[0..31] | NVRS_LOUADDR |
| `0x1003f` | `R_MERCURY_UNIFIED32_HI` | NVRH_DRIVER | bits[0..31] (>>32) | NVRS_HIUADDR |

## NVRS_* symbol-value codes

The `action.sym_value` integer selects what the bit-patcher (`sub_1CD34E0`, extract `sub_1CD33F0` / insert `sub_1CD3330`) computes for the field. The codes referenced by the Mercury table:

| Code | Name | Patch semantics |
|---:|---|---|
| 0 | `NVRS_NONE` | no-op (ignore action) |
| 1 | `NVRS_ADDR` | `sym + addend`; full-width insert (width-64 @ bit 0 is a whole-qword copy) |
| 3 | `NVRS_HEADER` | tex/samp/surf header index bind (default path, not the bit-patcher) |
| 6 | `NVRS_LOADDR` | low 32 bits of `(sym + addend)` |
| 7 | `NVRS_HIADDR` | high 32 bits `((sym + addend) >> 32)` |
| 15 | `NVRS_FDESCA` | address of the entry-function descriptor |
| 16 | `NVRS_PCREL` | `(signed)(sym_value − reloc_offset)` |
| 20 | `NVRS_CLEAR` | clear the field to zero (symbol deleted) |
| 22–29 | `NVRS_ADDR8_0..56` | byte lane *n* of the address (mask `0xFF`, shift `8n`) |
| 30–37 | `NVRS_GADDR8_0..56` | byte lane *n* of the generic (window-relative) address |
| 38–45 | `NVRS_FDESC8_0..56` | byte lane *n* of the function descriptor |
| 46 | `NVRS_UADDR` | unified address of symbol |
| 47–54 | `NVRS_UADDR8_0..56` | byte lane *n* of the unified address |
| 55 | `NVRS_LOUADDR` | low 32 bits of the unified address |
| 56 | `NVRS_HIUADDR` | high 32 bits of the unified address |
