# ISA Batch 06 — Vector Loads + valign priming

This is the per-instruction reference for the **vector-load** family of the Vision-Q7 *Cairo*
(`ncore2gp`) ISA: the **103** shipped `ivp_l*` / `malign` / `zalign` mnemonics that move data **from
memory into a vector register** — the aligned loads (`lvn*`/`lv*`), the structured streaming loads
(`lsn*`/`lsr*`), the **align-load** family (`la*`/`lav*`/`lan*`) that performs an *unaligned* read by
rotate-merging across two memory rows, and the **`valign` priming** ops (`lalign*`/`malign`/`zalign`)
that load and manage the four 512-bit alignment registers `u0..u3`. It is reimplementation-grade: every
mnemonic's FLIX slot, opcode-selector template, addressing-mode field bits and the valign rotate-merge
math are read **directly out of the shipped binaries** — `libisa-core.so` encode thunks and `Field_*_get`
bit-extractors, the `ncore2gp` `xtensa-elf-as`/`objdump` device round-trip, and the `libfiss-base.so`
funnel-shift leaf driven **live** by ctypes — and tagged. This page owns **loads only**; the symmetric
**stores** (`svn*`/`ssn*`/`salign*`) are [**Batch 07**](b07-stores.md), and the boundary is stated and
slot-checked in [§9](#9-the-b06--b07-boundary).

This page inherits the certified-perfect denominator from
[the coverage tally](../core/coverage-tally.md): the `1534 / 12569` shipped mnemonic/placement cover.
Counts are grounded with `nm | rg -c` against the binary `.symtab`, never a decompile grep; the
`extracted/` tree is gitignored (reach it with `fd --no-ignore` or an absolute path). The page default
is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag,
following [the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte /
immediate / symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over
OBSERVED; `CARRIED` = re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is
binary / static-analysis derived only.

> **Scope in one line.** B06 = aligned vector loads (`lvn*`/`lv*`), structured stream loads
> (`lsn_NxM*`/`lsnx*`/`lsr*`), the align-load rotate-merge family (`la*`/`lav*`/`lan*`/`l2a*`/`l2u*`),
> the bit/bool loads (`lb*`), and the `valign` priming/management ops (`lalign*`/`malign`/`zalign`), in
> the four addressing modes **`_i` / `_ip` / `_x` / `_xp`** (immediate / immediate-post-update /
> AR-indexed / AR-indexed-post-update). **103 mnemonics, 1484 placements.**

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B06 mnemonics (loads + align mgmt) | **103** | classifier over `nm libisa-core.so` `Opcode_*` roster ([§9](#9-the-b06--b07-boundary)); two independent derivations agree |
| B06 placements (`mnemonic × slot`) | **1484** | summed `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` over the 103 |
| Issue slots | **`s0 = LdSt/LdStALU/Ld`** and **`s1 = Ld`** of every format | `Opcode_*_Slot_<f>_s0_ldst_encode` / `_s1_ld_encode` (13–17 placements typical) |
| Load destination | **`vec`** (idx 2, 512-bit, 32 entries) | [register-files §3](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster); device decode `v0..v31` |
| Address operands | **`AR`** base (idx 0, 32-bit, 64 entries) + AR stride / immediate | same; field getters `i_bimm4*` / `vr` / `ar` |
| Alignment state | **`valign`** (idx 4, **512-bit × 4**, `u0..u3`) | [register-files §3](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster); `u4` rejected by assembler |
| valign register field | **bits `[1:0]` of `word0`** (2 bits → exactly 4 values) | `Field_fld_ivp_sem_ld_st_valignr_Slot_f0_s0_ldst_get` = `mov (%rdi),%eax; and $0x3; ret` |
| Addressing modes | `_i` (imm, no update) / `_ip` (imm post-update) / `_x` (AR-idx) / `_xp` (AR-idx post-update) / `_ipi` (imm post-update, imm stride) | mnemonic suffix; device decode |
| Unaligned read primitive | **funnel byte-shift over `{carry_row ‖ new_row}`** | `module__xdref_lashift_512_512_6` driven live ([§5](#5-the-valign-extract--driven-live)) |
| Load pipeline depth | **12 stages** (`stage0..stage11`); valign read `@10`, write `@12` | `libcas-core.so` `IVP_LV2NX8_I_inst_stage0..11`; [register-files §5](../core/register-files.md#5-readwrite-semantics-per-file) |
| Bundle byte-size | **16** (wide F-formats) / **8** (narrow N-formats) | [flix-encoding §2](../core/flix-encoding.md#2-the-14-formats--selector-length-slot-composition) |
| Value leaves | **none for the load itself** (loads are *addressing* ops, not value transforms) | `nm libfiss-base.so \| rg 'xdref_(lvn\|lsn\|la_)'` = 0 ([§5.4](#54-loads-have-no-xdref-value-leaf)) |

The load family is the **second-densest** sub-batch of the vector ISA (after the MAC matrix) because two
orthogonal axes multiply out: **access shape** (aligned `lvn` / streaming `lsn_NxM` / unaligned `la`)
× **addressing mode** (`_i`/`_ip`/`_x`/`_xp`), and each (shape, mode) pair is placed in **every** format
slot that carries a memory port — typically 13–17 placements per mnemonic. The roster below is organized
on the shape axis; the addressing modes are the within-family rows.

---

## 2. Roster — the 103 vector-load + valign-priming mnemonics

Columns: `mnemonic` · `FLIX format·slot` (the memory-port slots hosting it) · `opcode-sel imm` (the
`F0_S0_LdSt` encode-thunk template `WORD0`, the
[universal `C7 07 imm32 C3` ABI](../core/flix-encoding.md#61-the-universal-encode-thunk-abi); per-format
packing differs) · `dest/addr regfiles + field bits` · `bytes` (16 wide / 8 narrow) · one-line
semantics. Templates are byte-exact from `objdump -d`; field bits from the `Field_*_get`
thunks and the `xtensa-elf-as` device round-trip ([§3](#3-encoding--where-loads-live-in-the-flix-grid)).
Every row of §2.1–§2.4 is `[HIGH/OBSERVED]` except `ivp_lat2nx8_xp`, marked `[MED/OBSERVED]` inline.

### 2.1 Aligned vector loads (`lvn*` / `lv*` — the streaming spine)

A 512-bit load whose address is a multiple of 64 bytes. Reads one whole memory row straight into a
`vec`. Four dtypes (signed/unsigned `8`-bit and `16`-bit; the `2x16s/2x16u` carry a sign-class), four
addressing modes each.

| mnemonic | fmt·slot | opcode-sel imm (F0·s0) | dest/addr (field bits) | bytes | semantics |
|---|---|---|---|---|---|
| `ivp_lvnx8u_i` | F0/F1/F2/F3/F4/F6/F7/N0/N1/N2 · s0/s1 | `0x10900000` | `vr`=vec dest · `ar`=AR base · `i_bimm` (×8 scale) | 16/8 | aligned 512b load, `u8` lanes, no addr update |
| `ivp_lvnx8u_ip` | (same) | `0x10f2c100` | + AR base **post-incremented** by imm | 16/8 | aligned load, then `base += imm` |
| `ivp_lvnx8u_x` | (same) | `0x10ed0000` | `vr`·`ar` base·`art` AR index | 16/8 | aligned load at `base + AR_index`, no update |
| `ivp_lvnx8u_xp` | (same) | `0x10ed0100` | + AR base **post-incremented** by AR stride | 16/8 | aligned load, then `base += AR_stride` |
| `ivp_lvnx8s_{i,ip,x,xp}` | (same 16 each) | `0x10900000` / `0x10f2c100` / `0x10ed0000` / `0x10ed0100` | as above, **signed** `s8` lane class | 16/8 | aligned 512b load, `s8` lanes |
| `ivp_lvn_2x16s_{i,ip,x,xp}` | (same) | `0x10940000`(i) | `vec` dest · 32×`s16` lanes | 16/8 | aligned 512b load, signed 16-bit lanes |
| `ivp_lvn_2x16u_{i,ip,x,xp}` | (same) | — | 32×`u16` lanes | 16/8 | aligned 512b load, unsigned 16-bit lanes |
| `ivp_lv2nx8_{i,ip,x,xp}` | (same) | — | 64×`8`-bit lanes (untyped byte vector) | 16/8 | aligned 512b byte load |

> **NOTE — `lvn` requires a 64-byte-aligned address; the hardware does not fault, it *masks*.** The
> aligned loads drop the low 6 address bits (the `vec` is one full row). A reimplementer that hands a
> non-multiple-of-64 base to `lvn` gets the **floor-aligned** row, silently — the misaligned-read story
> is the `la*` family ([§2.3](#23-align-loads--the-unaligned-rotate-merge-read)), which is why it exists.
> `[HIGH/INFERRED]` from the `i_bimm*` scaling and the `lashift` model.

### 2.2 Structured stream loads (`lsn_NxM*` / `lsnx*` / `lsr*`)

The `lsn_NxM` forms load a **multi-row structured block** (e.g. `lsn_4x64` = four 64-bit sub-rows,
`lsn_16x256` = a 16×256-bit tile) used for matrix-tile and transpose-feed streaming; `lsr*` is the
strided/rotated streaming variant. All four addressing modes each.

| mnemonic | shape (`N×M`) | opcode-sel imm (F0·s0) | bytes | semantics |
|---|---|---|---|---|
| `ivp_lsn_2x16s_{i,ip,x,xp}` | 32×16b signed | (per-mode) | 16/8 | streaming load, signed 16-bit lanes |
| `ivp_lsn_2x32_{i,ip,x,xp}` | 16×32b | — | 16/8 | streaming load, 32-bit lanes |
| `ivp_lsn_4x64_{i,ip,x,xp}` | 4 × 64b sub-rows | `0x…`(per-fmt) | 16/8 | structured 4-lane 64-bit tile load (see device decode below) |
| `ivp_lsn_8x128_{i,ip,x,xp}` | 8 × 128b | — | 16/8 | 8-lane 128-bit tile load |
| `ivp_lsn_16x256_{i,ip,x,xp}` | 16 × 256b | — | 16/8 | 16-lane 256-bit tile load (widest stream form) |
| `ivp_lsnx8s_{i,ip,x,xp}` / `ivp_lsnx16_{…}` | 8b·16b | — | 16/8 | narrow stream load, sign-class typed |
| `ivp_ls2nx8_{i,ip,x,xp}` | 64×8b | — | 16/8 | byte stream load |
| `ivp_lsr2nx8_{…}` / `ivp_lsrn_2x32_{…}` / `ivp_lsrn_4x64_{…}` / `ivp_lsrnx16_{…}` | 8b/32b/64b/16b | — | 16/8 | **rotated/strided** stream load (`lsr` = ld-stream-rotate; extra Ld-slot placements) |

A device-decoded `lsn_4x64` streaming sequence (assembled in `libhal.a`, decoded with the `ncore2gp`
core) shows the canonical four-lane tile readout co-issued with a predicate extract:

```
c1: 00070b3208b3ab000ec823a60112000f
    { ivp_extrpr64n_4x64 pr0, v0, 0; ivp_lsn_4x64_i v0, a2, 8; nop; nop }
1a1:0006cb322033ab0c0ac801a621f200ef
    { ivp_extrpr64n_4x64 pr14, v0, 0; ivp_lsn_4x64_i v0, a2, 120; addi.a a2, a2, -128; nop }
```

The `lsn_4x64_i v0, a2, 8` advances the in-bundle immediate offset each iteration (`8, 16, 24, …`);
the closing bundle rewinds `a2` with `addi.a a2, a2, -128` — a software-managed circular tile walk.

### 2.3 Align-loads — the unaligned rotate-merge read (`la*` / `lav*` / `lan*`)

The heart of the batch. An *unaligned* 512-bit read at an arbitrary byte address. It cannot read one
aligned row; the wanted vector **spans two consecutive 64-byte rows**, so the data is assembled by a
**byte rotate-merge** of the *carry* bytes held in a `valign` register and the *newly-loaded* row
([§4](#4-the-valign-prime--use--update-idiom)). The `la*` op simultaneously (a) loads the next aligned
row, (b) extracts the aligned 512b vector from the `{carry ‖ new}` concat, and (c) updates the carry
register for the next iteration — a one-instruction streaming unaligned read.

| mnemonic | role | fmt·slot | opcode-sel imm (F0·s0) | operands | semantics |
|---|---|---|---|---|---|
| `ivp_la2nx8_ip` | use+update | s0/s1 (17) | `0x10fa0080` | `v, u, a` | unaligned byte load via `u`, implicit post-incr |
| `ivp_la2nx8_ipi` | use+update | s0/s1 | — | `v, u, a, imm` | unaligned byte load, explicit imm post-incr |
| `ivp_la2nx8_xp` | use+update | s0/s1 + **F4 dual-Ld** | `0x10c30000` | `v, u, a, ar_stride` | unaligned byte load, AR-stride post-incr |
| `ivp_lan_2x16{s,u}_{ip,xp}` | use+update | s0/s1 | — | `v, u, a[, str]` | unaligned 16-bit load (sign-typed) |
| `ivp_lanx8{s,u}_{ip,xp}` | use+update | s0/s1 | — | `v, u, a[, str]` | unaligned 8-bit load (sign-typed) |
| `ivp_lav2nx8_xp` | use+update (variable) | s0/s1 | `0x10c98000` | `v, u, a, str` | variable-stride unaligned byte load |
| `ivp_lavn_2x16{s,u}_xp` / `ivp_lavnx8{s,u}_xp` | use+update (variable) | s0/s1 | — | `v, u, a, str` | variable-stride unaligned typed load |
| `ivp_lat2nx8_xp` | use+update (transpose-feed) | s0/s1 | — | `v, u, a, str` | unaligned byte load, transpose-tap variant — `[MED/OBSERVED]` |
| `ivp_labvdcmprs2nx8_xp` | use+update (compress) | s0/s1 (2) | — | `v, u, a, str` | **bool-mask decompressing** unaligned load (`dcmprs`) |
| `ivp_l2a4nx8_ip` / `ivp_l2au2nx8_{ip,ipi,xp}` / `ivp_l2u2nx8_xp` | dual-load | s0/s1 | — | `v, u, a[, str]` | **two-row** dual align-load (loads a 1024-bit window) |

> **GOTCHA — `lavn`/`lav` is *variable*-stride; `lan`/`la` is fixed.** The `v` infix (`lav`/`lavn`)
> marks the **variable-stride** align-load: the post-increment is an **AR-supplied** byte stride, so the
> walk can have a non-row stride (the classic strided convolution-row read). The non-`v` `la`/`lan`
> forms post-increment by the **fixed access width** (`_ip`) or a single AR stride (`_xp`). A
> reimplementer's loop builder must pick `lavn_*_xp` when the row stride ≠ the vector width. Verified by
> the operand grammar: `ivp_lavn_2x16s_xp v0, u0, a3, a4` (four operands incl. explicit stride `a4`)
> vs `ivp_la2nx8_ip v0, u0, a2` (implicit).

### 2.4 valign priming + management (`lalign*` / `malign` / `zalign`) and bit-loads (`lb*`)

The ops that **establish** the carry state before the first `la*`, and the bit/bool loads.

| mnemonic | fmt·slot | opcode-sel imm (F0·s0) | operands | semantics |
|---|---|---|---|---|
| `ivp_lalign_i` | s0/s1 (18) | `0x11000000` | `u, a, imm` | **prime**: load 512b row into `valign u`, no addr update |
| `ivp_lalign_ip` | s0/s1 (18) | `0x11020400` | `u, a, imm` | prime + `base += imm` post-update |
| `ivp_la_pp` | s0/s1 | `0x11060400` | `u, a` | **priming-pair**: prime `u` from `[a]` (no vec dest) |
| `ivp_la_ppxu` | s0/s1 | `0x11000400` | `u, a, str` | priming-pair, AR-stride, **circular** (`xu`) |
| `ivp_malign` | s0/s1 (20) | `0x11060420` | `u, u'` | **make**: valign→valign move/derive (no memory access) |
| `ivp_zalign` | s0/s1 | `0x11060424` | `u` | **zero**: clear `valign u` (`= malign \| 0x04`) |
| `ivp_lb2n_i` / `ivp_lb2n_ip` | **s1_ld only** | (per-fmt) | `vb, a, imm` | bit/bool vector load into `vbool` (`vb0..vb15`) |
| `ivp_lbn_i` / `ivp_lbn_ip` / `ivp_lbn_2_i` / `ivp_lbn_2_ip` | s1_ld | — | `vb, a, imm` | bool load (1-row / 2-row variants) |

> **QUIRK — `zalign` = `malign | 0x04`, and `malign` is a register-only move.** Byte-exact:
> `Opcode_ivp_malign_Slot_f0_s0_ldst_encode` = `movl $0x11060420,(%rdi)`,
> `Opcode_ivp_zalign_Slot_f0_s0_ldst_encode` = `movl $0x11060424,(%rdi)` — a single `+0x04` selector
> bit toggles "derive from another valign reg" → "zero". And `malign u0, u1` takes **two valign
> registers, no memory operand** (assembler rejects an AR operand): it is the valign-file internal
> move used to re-seed a fresh stream from an in-flight one. `zalign u0` clears the carry so the first
> `la*` of a stream that *starts* row-aligned reads correctly.

---

## 3. Encoding — where loads live in the FLIX grid

### 3.1 Every load is an `s0`/`s1` memory-port opcode

Loads occupy the **memory ports** of the FLIX grid: slot `s0` (the `LdSt` / `LdStALU` / `Ld` slot,
depending on format) and slot `s1` (the second `Ld` slot, present in the wide formats and in `F4`'s
**dual-load** layout). Querying the encode-thunk symtab for an aligned load:

```
nm libisa-core.so | rg 'Opcode_ivp_lvn_2x16s_i_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+'
  →  f0_s0_ldst  f0_s1_ld   f1_s0_ldstalu f1_s1_ld
     f2_s0_ldst  f2_s1_ld   f3_s0_ldst    f3_s1_ld
     f4_s0_ld    f4_s1_ld   f6_s0_ldst    f6_s1_ld
     f7_s0_ldst  f7_s1_ld   n0_s0_ldst    n2_s0_ldst   (16)
```

This is the memory-port roster from
[flix-encoding §2](../core/flix-encoding.md#2-the-14-formats--selector-length-slot-composition): `LdSt` =
`s0` of F0/F2/F3/F6/F7/N0/N2, `LdStALU` = `s0` of F1, `Ld` = `s0` of F4/F11 and `s1` of every wide
format. So a load can co-issue with a second load (`s1`), a MAC (`s2`) and 1–3 ALU ops (`s3+`) in one
16-byte bundle — the structural basis of the streaming inner loop (a `lvn` feeding a `mul` while a
`lalign` primes the next unaligned read; see the real two-load bundle in
[§4.3](#43-a-real-load-bundle-from-device-code)). The bit-loads (`lb*`) place **only** in `s1_ld` (they
target the `vbool` file).

The placement total over the 103 B06 mnemonics is **1484** (summed `nm | rg -c` per mnemonic). The
spread: most loads carry **13–17** placements (every memory-port slot of every format), the bit-loads
carry fewer (`s1_ld` only), and the dual/special forms (`labvdcmprs`, `l2a4nx8`) carry **1–2**. `1484`
is the contribution of B06 to the certified `12569` placement cover
([coverage-tally §1](../core/coverage-tally.md#1-the-five-headline-numbers--re-derived-from-the-binary)).
`[HIGH/OBSERVED]`

### 3.2 The addressing mode is a selector cluster, not a single bit

The four addressing modes (`_i`/`_ip`/`_x`/`_xp`) are distinct opcodes packed into the same slot, **not**
a global "post-update bit". Reading the `F0_S0_LdSt` templates byte-exact for the unsigned-byte load:

```
ivp_lvnx8s_i    WORD0 = 0x10900000     immediate offset, no addr update
ivp_lvnx8s_ip   WORD0 = 0x10f2c100     immediate offset, base += imm    (whole-different selector word)
ivp_lvnx8s_x    WORD0 = 0x10ed0000     AR-indexed,        no addr update
ivp_lvnx8s_xp   WORD0 = 0x10ed0100     AR-indexed,        base += AR     (+0x100 over _x)
```

For the **AR-indexed** pair the post-update is a clean `+0x100` selector step (`_x` → `_xp`), but the
**immediate** pair (`_i` → `_ip`) is a *wholly different* selector word (`0x10900000` → `0x10f2c100`):
the post-incrementing immediate load lives in a different iclass and is packed differently (the
auto-increment amount is folded into the selector). This mirrors the
[two-tier selector model](../core/flix-encoding.md#62-the-two-tier-selector-model): a reimplementer's
assembler must carry the full `(mnemonic, slot) → template` table — it cannot synthesize `_ip` from `_i`
by OR-ing a constant.

### 3.3 Operand field windows (read from the `Field_*_get` thunks)

The operand bit-windows in the `s0` LdSt slot, read from the `Field_fld_ivp_sem_ld_st_<fld>_Slot_…_get`
extractor bodies and **confirmed by an `xtensa-elf-as` field sweep** ([§3.4](#34-the-device-field-sweep)):

| operand | regfile | field root | bit-window (F0·s0 getter body) | role |
|---|---|---|---|---|
| dest vector | `vec` | `vr` / `vbr` | high nibble of the dest byte | `v0..v31` load target |
| dest bool | `vbool` | `vbr` | (bit-loads) | `vb0..vb15` bit-load target |
| align reg | `valign` | **`valignr`** | **`(word0) & 0x3`** (bits `[1:0]`) | `u0..u3` carry register |
| align update | `valign` | `uul` (load) / `uus` (store) | dedicated update field | the prime/use scoreboard role ([§4.1](#41-the-valign-register-as-a-scoreboard)) |
| AR base | `AR` | `ar` | low nibble of the addr byte | `a0..a63` base pointer |
| AR stride/index | `AR` | `art` | high nibble of the stride byte | `_x`/`_xp` index/stride register |
| immediate offset | — | `i_bimm{4,6,8}{,x1,x2,x4,x8,x16,x32}` | funnel-assembled, **scaled by access width** | `_i`/`_ip` literal offset |

> **GOTCHA — the immediate offset is *width-scaled*, and the scale is in the field name.** The offset
> field for a byte load is `i_bimm4x1`/`i_bimm6x1`; for a 16-bit load `…x2`; for a 32-bit load `…x4`;
> the structured tile forms reach `…x16`/`…x32`. The getter body for `i_bimm4x2` is a funnel
> (`mov (%rdi),%eax; shl; shr $0x1c | mov (%rdi),%eax; and $0xf; shl`) that re-assembles a 4-bit
> immediate scattered across the word top, then the decoded value is multiplied by the access width. A
> reimplementer that treats the immediate as a raw byte offset will under-stride by the element width.
> The device sweep confirms the scale: `ivp_lvn_2x16s_i v0, a2, 64` and `… a2, 128` step the encoded
> immediate by one unit each ([§3.4](#34-the-device-field-sweep)).

### 3.4 The device field sweep (OBSERVED bit positions)

Assembling each operand-variant with the `ncore2gp` `xtensa-elf-as` and disassembling the raw bytes
isolates each field. The bundle `029c48309c04020f` (= `ivp_lvn_2x16s_i v0, a2, 0; nop`) is the baseline;
varying one operand moves exactly one nibble (little-endian, byte index 0 = the format/length selector):

```
DEST vec   v0→v3 :  …020f → …120f → …220f → …320f   (dest index = byte6 high nibble)
BASE AR    a2→a5 :  …020f → …030f → …040f → …050f   (AR base   = byte6 low  nibble)
IMM off 0,64,128 :  …020f → …022f → …024f           (imm scale = byte7;  off=512 bumps byte3)
VALIGN     u0→u3 :  …62 00… → …62 02… → …62 04… → …62 06…   (valign idx = byte5 step 2; = bits[1:0])
ADDR mode  i/ip/x/xp : 4810 / 5e72 / 5ce4 / 5ce6   (the _xp post-update bit = +0x02 in byte4 over _x)
STRIDE AR  a3→a6 (xp): …3f → …4f → …5f → …6f         (stride AR  = byte7 high nibble)
```

Every field above was produced by the **shipped assembler** round-trip, then read back by the shipped
disassembler — an OBSERVED encode↔decode certificate for each bit-window. `[HIGH/OBSERVED]`

---

## 4. The `valign` prime → use → update idiom

The unaligned streaming read is a **stateful three-phase loop** built on the four 512-bit `valign`
registers (`u0..u3`). The state a `valign` register carries is the **tail bytes of the previous row** —
the part of the previous aligned load that belongs to the *next* unaligned vector. Each `la*` op rolls
the loop one step:

```
PRIME : lalign(_ip)  u, [base], +incr     // load row N into u; base += incr        (no vec produced)
        OR la_pp     u, [base]            // priming-pair form (pure prime, no dest)
        OR zalign    u                    // start row-aligned: carry = 0
LOOP  : la(vn)_xp    v, u, [base], stride // (1) load row N+1 into a temp
                                          // (2) v = extract( {u ‖ rowN+1}, align )  ← rotate-merge
                                          // (3) u = rowN+1                           ← carry forward
                                          // (4) base += stride                       ← post-update
```

The `valign` register is **never read or written by the program directly** — it is an opaque carry
threaded `lalign → la → la → …`. Phase (2) is the byte funnel-shift proven live in
[§5](#5-the-valign-extract--driven-live); phase (3) is the *update* (the new row becomes next iteration's
carry); phase (4) is the AR post-update modeled in [§5.3](#53-the-address-post-update--driven-live).

### 4.1 The valign register as a scoreboard

The simulator models the `valign` register-file as a small indexed store with a hazard scoreboard. The
operand-semantic function `my_valign_0_opnd_ivp_sem_ld_st_valignr_set_use` (`libcas-core.so`
`@0x17a6ec0`), disassembled:

```asm
mov  0xac70(%rdi),%eax        ; eax = current valign write index (the rotating u-pointer)
mov  %esi,%ecx                ; ecx = the operand's valign id (0..3)
and  $0x1f,%eax               ; index &= 0x1f   (the file's circular write window)
mov  %edx,0xc174(%rdi,%rax,4) ; valignfile[index] = value          ← WRITE the carry
mov  $0x1,%eax ; shl %cl,%eax ; or %eax,0xac68(%rdi)   ; mark valign[id] live (use-bitmap)
ret
```

The matching `…_valignr_use` (`@0x17a72b0`) reads it back with a **load-latency window** check
(`lea 0x1(%rsi),%r10d; cmp $0xa,%r10d`): the carry is only valid once the priming load has retired —
the `0xa`/`0xb` constants are the read-stage bound, consistent with the **12-deep load pipeline**
(`stage0..stage11`) and the `valign @10 read / @12 write` timing from
[register-files §5](../core/register-files.md#5-readwrite-semantics-per-file). The three operand roles —
`valignr` (the carry register), `uul` (align-**l**oad update), `uus` (align-store update, B07's side) —
each have a `set_use`/`use` pair; B06 owns `valignr` + `uul`.

> **QUIRK — there are exactly four `valign` registers, and the assembler proves it.** The field is 2
> bits (`Field_…_valignr_…_get` = `and $0x3`), so the encoding can name only `u0..u3`. Assembling
> `ivp_lalign_i u4, a2, 0` is **rejected**: `Error: register number out of range`. This is the
> binary-level confirmation of the [`valign` idx-4 geometry: 512b × 4 = 2048
> bits](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster). With only four
> carry registers, at most **four** independent unaligned streams can be in flight; a fifth aliases a
> live carry — a software-scheduling constraint a reimplementer's register allocator must honor.
> `[HIGH/OBSERVED]`

### 4.2 Annotated C — the rotate-merge unaligned load

The per-iteration `la*` semantics, reconstructed from the `lashift` funnel leaf
([§5](#5-the-valign-extract--driven-live)), the `valignr` scoreboard ([§4.1](#41-the-valign-register-as-a-scoreboard)),
and the device decode:

```c
// One iteration of a streaming unaligned 512b load: ivp_la<dtype>_xp v, u, a, stride.
// `align` = (initial base) mod 64 — the byte misalignment of the stream, frozen at prime time.
// `u` (valign) holds the *carry row* = the previous aligned 64-byte row.
// vec512 = 64-byte register; rows are 64-byte (512-bit) aligned.

vec512 la_step(valign *u, ar_t *base, int stride, int align)
{
    // (1) Load the next aligned row.  The hardware floor-aligns `base` to 64B and reads one row.
    vec512 new_row = load_aligned_row(*base & ~63);          // the s0/s1 memory port

    // (2) EXTRACT — funnel byte-shift over the two-row concat {carry ‖ new}, window @ `align`.
    //     out[j] = concat[align + j]  for j in 0..63, where concat = {*u (low) ‖ new_row (high)}.
    //     This is module__xdref_lashift_512_512_6 (proven bit-exact, §5.1).
    vec512 v = lashift(/*A=*/ *u, /*B=*/ new_row, /*shift=*/ align);

    // (3) UPDATE — the new row becomes next iteration's carry (the _xp/_ip "update" half).
    *u = new_row;                                            // valignr write (scoreboard, §4.1)

    // (4) POST-UPDATE the AR pointer.  _xp: += stride;  _ip: += fixed access width;  modular mod 2^32.
    *base = (*base + stride) & 0xFFFFFFFF;                   // §5.3, wraps, never faults

    return v;
}
```

For the **variable-stride** `lavn*`/`lav*` forms, step (4) uses the AR-supplied `stride`; for the fixed
`la*`/`lan*` forms it is the access width (`_ip`) or a single AR stride (`_xp`). For the **dual** `l2a*`
forms, step (1) loads *two* rows and step (2) extracts a 1024-bit window. For the **decompressing**
`labvdcmprs`, step (2) is gated by a `vbool` mask (`module__xdref_dcmprs_2nx8_512_512_64`) — only the
selected lanes are filled. `[HIGH/OBSERVED]` on the structure and the funnel; `[MED/INFERRED]` on the
exact dual/compress lane-selection wiring (the leaf names confirm the kind; the per-lane mux is the
multiplier's internal, not separately exposed).

### 4.3 A real load bundle from device code

`libhal.a` (the shipped `ncore2gp` HAL archive) contains assembled vector code; decoded with
`XTENSA_CORE=ncore2gp`, the priming idiom appears verbatim:

```
 b: 029c48091852020f   { ivp_lv2nx8_i v0, a2, 0;       ivp_lalign_i u3, a2, 0x140 }
13: 0007193208c2ab00…  { ivp_lalign_i u1, a2, 192;     ivp_lalign_i u2, a2, 0x100; nop; ivp_movfsv v0 }
23: 021c6301dcd2022f   { ivp_lalign_i u0, a2, 128;     ivp_lb2n_i  vb13, a2, 0x1e8 }
```

Bundle `b` co-issues an **aligned load** (`lv2nx8_i v0`) on the `s0` port with a **valign prime**
(`lalign_i u3`) on the `s1` Ld port — one bundle, two memory ops, priming the unaligned stream while the
aligned spine advances. Bundle `13` primes **two** valign registers in a single 16-byte bundle (`u1`,
`u2`) — the dual-Ld formats let a routine seed multiple unaligned streams in parallel.

---

## 5. The valign extract — driven LIVE

`libfiss-base.so` is **callable in-process via ctypes with no license**
([coverage-tally §5](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)).
The load itself has no value leaf (it is an addressing op, [§5.4](#54-loads-have-no-xdref-value-leaf)),
but the **rotate-merge extract** that `la*` performs *is* a `libfiss-base` leaf —
`module__xdref_lashift_512_512_6` — so the unaligned-read math is **proven by execution**, not decoded
and guessed.

> **NOTE — the valign funnel has two names (one datapath).** The fiss value leaf
> `module__xdref_lashift_512_512_6` (and the width-parameterized siblings
> `module__xdref_wideldshift_*_512_6`) is the *same* shifter the TIE layer names
> `xdsem_ld_shifter_512` (load) / `xdsem_st_shifter_512` (store) in `libcas-core.so`. This is a
> **naming-layer** difference (fiss leaf vs TIE module), not two distinct datapaths. See
> [Formal Semantics II](../semantics/group-semantics-ii.md) §3, [B08 reduce](b08-reduce.md), and
> [B12 shift](b12-shift.md).

### 5.1 The two-row funnel shift (executed, bit-exact)

The `lashift` leaf is a byte-granular **funnel shift** over a 128-byte concatenation of two 512-bit
inputs. Its body (disassembled `@0x85cc40`) copies input A (`%rdx`) to a stack buffer at offset `-0x38`,
input B (`%rsi`) immediately above it, converts the byte-shift count to a bit offset
(`lea 0x0(,%rcx,8),%edx`), and emits each output word from a `shl|shr`-merged window spanning two source
words — the textbook double-word funnel. Driven live (A = carry row, B = new row,
`shift = align`):

```python
import ctypes
lib = ctypes.CDLL("libfiss-base.so")
V512 = ctypes.c_uint8 * 64
f = lib.module__xdref_lashift_512_512_6
f.restype = None
f.argtypes = [ctypes.c_int, ctypes.POINTER(V512), ctypes.POINTER(V512),  # ctx, *B(new), *A(carry)
              ctypes.c_int, ctypes.POINTER(V512)]                        # shift(bytes), *out
A = V512(*range(64))                  # carry row  A[i] = i
B = V512(*[0x40+i for i in range(64)])# new row    B[i] = 0x40+i
out = V512()
for sh in (0,1,5,11,16,63):
    f(0, ctypes.byref(B), ctypes.byref(A), sh, ctypes.byref(out))
    # reference: window [sh..sh+63] of the concat {A ‖ B}
```

Sweep (OBSERVED-by-execution), each diffed against the `{A‖B}` window model:

```
shift= 0 : out[0:8] = 00 01 02 03 04 05 06 07     match
shift= 1 : out[0:8] = 01 02 03 04 05 06 07 08     match
shift= 5 : out[0:8] = 05 06 07 08 09 0a 0b 0c     match
shift=11 : out[0:8] = 0b 0c 0d 0e 0f 10 11 12     match
shift=16 : out[0:8] = 10 11 12 13 14 15 16 17     match
shift=63 : out[0:8] = 3f 40 41 42 43 44 45 46     match   ← crosses the row boundary (0x3f|0x40)
```

At `shift=63` the output **crosses the row boundary** — the first byte is the last byte of the carry row
(`0x3f`), the rest come from the new row (`0x40, 0x41, …`). This is exactly the unaligned-read
behaviour: the wanted 512b vector at a near-end-of-row address is assembled from the carry tail plus the
freshly-loaded head. A full sweep over all 64 shift values returned **0 mismatches** against the
`{carry‖new}` window model — the OBSERVED-by-execution certificate for the `la*`/`lavn*` extract.
`[HIGH/OBSERVED by execution]`

### 5.2 The same result via the byte-select leaf (cross-check)

A second, independent leaf — `module__xdref_sel_2nx8_512_512_512_512`, the 64-lane byte-select — was
driven with a control derived from `align` (`ctrl[j] = (align+j < 64) ? 64+(align+j) : (align+j-64)`,
the leaf's `{B‖A}` index convention recovered by probing). For `align = 11` over an arbitrary 128-byte
stream it reproduced the true unaligned vector `stream[11:75]` with **0 mismatches** across all 64 lanes,
including the boundary bytes. Two distinct `libfiss-base` primitives agree on the rotate-merge math.

### 5.3 The address post-update (driven live)

The `_ip`/`_xp` post-update is a plain 32-bit AR add (`base += stride`), modeled by
`module__xdref_add_32_32_32` (`@0x5bc340`). Driven live:

```
base=0x00001000 += 64   -> 0x00001040     base=0x00001040 += 64   -> 0x00001080
base=0x00002000 += -128 -> 0x00001f80     base=0xffffffc0 += 64   -> 0x00000000   (wraps mod 2^32)
```

The AR pointer is a **32-bit modular** address: the post-update wraps at `2^32` with **no fault** — the
basis of the circular-buffer walk a streaming kernel uses (the `addi.a a2, a2, -128` rewind in the
device decode of [§2.2](#22-structured-stream-loads-lsn_nxm--lsnx--lsr) is the negative-stride form of
the same add).

### 5.4 Loads have no `xdref` value leaf

`nm libfiss-base.so | rg 'module__xdref_(lvn|lsn|la_|lalign|malign)'` returns **zero** matches —
deliberately. A load performs **no per-element value transform**: its semantics are entirely *address
computation* (base + scaled offset, post-update) plus, for `la*`, the *byte rotate-merge* (which is the
`lashift` leaf, not a per-lane value leaf). The OBSERVED-by-execution coverage for B06 is therefore the
**`lashift`/`sel` extract** plus the **`add_32_32_32` address update** — both proven above — and the
**`xtensa-elf-as`/`objdump` encode↔decode round-trip** of [§3.4](#34-the-device-field-sweep). The
compares `le*`/`lt*` *do* have value leaves (`le_1_8_8`, `lt_1_8_8`) — which is exactly why they are
**not** in B06 ([§9](#9-the-b06--b07-boundary)).

---

## 6. The load latency and pipeline

`libcas-core.so` exposes a **12-stage** pipeline model for every load (`…_inst_stage0` …
`…_inst_stage11`, confirmed by `nm | rg -o 'IVP_LV2NX8_I_inst_stage[0-9]+' | sort -u | wc -l` = 12 and
the identical count for `IVP_LALIGN_I`). The memory result is available late in the pipe; the `valign`
carry is **read `@10` and written `@12`** ([register-files §5](../core/register-files.md#5-readwrite-semantics-per-file)),
matching the `cmp $0xa` window in the `valignr_use` scoreboard ([§4.1](#41-the-valign-register-as-a-scoreboard)).
Each load also carries an `_issue` and a `_stall` hazard function
(`F0_F0_S0_LdSt_4_inst_IVP_MALIGN_issue` / `…_stall`), the scheduler's structural-hazard guard for the
single memory port per slot. A reimplementer models a load as a **deep, in-order, II-1** operation: a
load issues every cycle, but a dependent consumer must wait the result latency, and a `la*` consumer of a
`lalign` carry must wait the prime to retire (the scoreboard enforces it). `[HIGH/OBSERVED]` on the stage
count and the `@10/@12` valign timing; `[MED/INFERRED]` on the exact load-result latency cycle (the
12-stage model bounds it; the precise use-stage is the consumer op's, not separately exposed here).

---

## 7. Per-batch coverage tally

| number | value | binary witness |
|---|--:|---|
| B06 **mnemonics** `m` | **103** | classifier over `nm libisa-core.so` `Opcode_*` roster; two independent derivations (roster-file and direct prefix `rg`) agree |
| B06 **placements** `p` | **1484** | `Σ` over the 103 of `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` |
| B06 **value leaves** `v` | **0 direct** (loads are addressing ops) + the `lashift`/`sel`/`add_32_32_32` extract/address leaves it *reuses* | `nm libfiss-base.so \| rg 'xdref_(lvn\|lsn\|la_)'` = 0; the reused leaves are shared with the shuffle/ALU batches |

`m = 103` rolls into the **1065** `ivp_`-prefix vector axis; `p = 1484` rolls into the **12569** placement
cover, neither cross-paired with the `1607/12642` pre-fold superset
([coverage-tally](../core/coverage-tally.md), [template §6](template-and-partition.md)). The batch is
complete: every mnemonic in the load/align-prime slice has a roster row; the placement sum equals its
`nm`-counted total; and every row's *semantics* are grounded either by the executed extract/address
leaves ([§5](#5-the-valign-extract--driven-live)) or by the device encode↔decode round-trip
([§3.4](#34-the-device-field-sweep)).

---

## 8. Sub-family census (the 103, by shape)

| sub-family | members | count | what it is |
|---|---|--:|---|
| aligned vec loads | `lvnx8{s,u}_*`, `lvn_2x16{s,u}_*`, `lv2nx8_*` | **20** | 64-byte-aligned 512b row read into `vec` |
| stream loads | `lsn_{2x16s,2x32,4x64,8x128,16x256}_*`, `lsnx{8s,16}_*`, `ls2nx8_*` | **32** | structured multi-row tile / streaming reads |
| rotated-stream loads | `lsr2nx8_*`, `lsrn_{2x32,4x64}_*`, `lsrnx16_*` | **16** | `lsr` strided/rotated stream variant |
| align-loads (rotate-merge) | `la2nx8_*`, `lan_2x16{s,u}_*`, `lanx8{s,u}_*`, `lav*`, `lavn*`, `lat2nx8_xp`, `labvdcmprs2nx8_xp`, `lalign_*`, `la_pp`, `la_ppxu` | **22** | unaligned read via `valign` rotate-merge |
| dual/double loads | `l2a4nx8_ip`, `l2au2nx8_{ip,ipi,xp}`, `l2u2nx8_xp` | **5** | two-row (1024-bit window) align-loads |
| bit/bool loads | `lb2n_{i,ip}`, `lbn_{i,ip}`, `lbn_2_{i,ip}` | **6** | bit-mask vector load into `vbool` |
| valign management | `malign`, `zalign` | **2** | valign→valign move / clear (no memory) |
| **total** | | **103** | |

`20 + 32 + 16 + 22 + 5 + 6 + 2 = 103`. ✓ `[HIGH/OBSERVED]`

---

## 9. The B06 / B07 boundary

B06 (this page) is **memory → register** (loads). [B07](b07-stores.md) is **register → memory** (stores).
The split is mechanical and slot-checked — **no double count**:

| token / op | direction | batch |
|---|---|---|
| `ivp_l…` load prefix (`lvn`/`lsn`/`la`/`lalign`/`lb`/`l2a`…) | memory → vec | **B06** |
| `ivp_malign` / `ivp_zalign` | valign-file move / clear (load-side priming) | **B06** |
| `ivp_s…` store prefix (`svn`/`ssn`/`sbn`/`sa`/`sav`) | vec → memory | B07 |
| `ivp_salign_{i,ip}` | **flush** valign carry to memory | B07 |
| `ivp_sapos_fp{,xp}` | store-position / flush variants | B07 |

> **GOTCHA — `lalign`/`malign`/`zalign` are B06; `salign`/`sapos` are B07; the `valign` register is
> shared.** The four `valign` registers are written by the **load-side** prime (`lalign`, `la_pp`) and
> the management ops (`malign`/`zalign`), and read by the **store-side** flush (`salign` — the
> rotate-merge that writes a partial first/last row of an unaligned store). They use the *same* file
> (`u0..u3`) and the *same* scoreboard, but opposite directions: `valignr_set_use`/`uul` (load) vs
> `uus` (store). B06 owns the load-side `valignr`+`uul` roles; B07 owns `uus`. The disambiguator is the
> `l`/`s`/`m`/`z` lead letter, **not** the `align` token. Mis-binning `salign` into B06 would
> double-count and corrupt both batches' tallies. Slot-checked: `salign_i` =
> `Opcode_ivp_salign_i_Slot_f0_s0_ldst_encode` = `0x11000200` (store-direction bit `0x200` set),
> 10 placements → B07.

> **NOTE — `le*`/`lt*`/`ltr*` are NOT loads, despite the `l` prefix.** `ivp_le2nx8`/`ivp_lt2nx8`
> (less-equal / less-than compares) place in `s2_mul`/`s3_alu`/`s4_alu` **and** have value leaves
> `le_1_8_8`/`lt_1_8_8` (a 1-bit boolean result) — they are **ALU compares**, owned by the vector-ALU /
> `vbool` batches (B01/B11), not B06. The `ltr*`/`ltrn*` forms (value leaves `ltr*_64_32`) are
> **load-transpose** taps that place in `s1_alu`/`s1_ld` and carry a transpose value transform; they
> belong with the transpose/shuffle family, not the pure addressing loads. B06 excludes all of
> `{le,leu,lt,ltu,ltr,ltrn,ltrs,ltrsn}` — 21 mnemonics — on the OBSERVED grounds that they (a) issue in
> compute slots and (b) have `xdref` value leaves, which no true load has.

---

## 10. Adversarial self-verification — 5 strongest claims, re-challenged

1. **"103 load+align-prime mnemonics, 1484 placements."** Derived two ways: (a) the refined roster
   file (true-load prefixes ∪ `malign`/`zalign`, minus the `le/lt/ltr` compares) = **103**; (b) a direct
   `nm | rg -o 'Opcode_(ivp_(lvn|lv2|lsn|…|lb)…|ivp_malign|ivp_zalign)' | sort -u`, then `rg -v
   '^ivp_(le|lt|lash)'` = **103**. The sub-family census ([§8](#8-sub-family-census-the-103-by-shape))
   sums `20+32+16+22+5+6+2 = 103`. The placement sum = **1484**. *Challenge:* could `le2nx8` (a compare)
   have been wrongly counted as a load? No — it places in `s2_mul`/`s3_alu` and resolves to value leaf
   `le_1_8_8`; no true load issues in a compute slot or has a value leaf. **Confirmed.**

2. **"There are exactly four `valign` registers, field = bits `[1:0]`."** *Challenge:* maybe the field
   is wider and only `u0..u3` are used. The getter
   `Field_fld_ivp_sem_ld_st_valignr_Slot_f0_s0_ldst_get` = `mov (%rdi),%eax; and $0x3; ret` — a **2-bit**
   field, structurally capping at 4. And the shipped assembler **rejects** `ivp_lalign_i u4, a2, 0`
   (`register number out of range`). Two witnesses (field width + assembler) agree with the
   [idx-4 file geometry](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster).
   **Confirmed.**

3. **"The `la*` unaligned read is a funnel byte-shift over `{carry ‖ new}`, bit-exact."** *Challenge:*
   maybe the model is approximate at the row boundary. Drove `module__xdref_lashift_512_512_6` live over
   all 64 shift values; every output matched the `{A‖B}` window model with **0 mismatches**, including
   `shift=63` which crosses the boundary (`…3f 40 41…`). A second independent leaf
   (`sel_2nx8_512_512_512_512`) reproduced the unaligned vector `stream[11:75]` with **0 mismatches**.
   Two distinct primitives agree. **Confirmed.**

4. **"The addressing mode is a distinct opcode, not a global post-update bit."** *Challenge:* the
   AR-indexed `_x → _xp` delta is a clean `+0x100`, which looks like a bit. Counter-evidence: the
   **immediate** `_i (0x10900000) → _ip (0x10f2c100)` is a *wholly different* selector
   word (the auto-increment is folded into a different iclass), and `lalign_i (0x11000000) → lalign_ip
   (0x11020400)` likewise. No single bit produces both deltas; the mode is a distinct
   `(mnemonic, slot)` template. **Confirmed.**

5. **"Loads have no `xdref` value leaf; the executed coverage is the extract + address leaves."**
   *Challenge:* maybe a load *does* have a hidden value leaf. `nm libfiss-base.so | rg
   'module__xdref_(lvn|lsn|la_|lalign|malign)'` = **0**. The only load-adjacent leaves are the
   *extract* (`lashift`, `sel`, `dcmprs`) and the *address add* (`add_32_32_32`) — exactly the ops B06
   reuses and proves live ([§5](#5-the-valign-extract--driven-live)). Conversely the compares `le/lt`
   *do* have leaves — which is the OBSERVED grounds for excluding them ([§9](#9-the-b06--b07-boundary)).
   **Confirmed.**

---

## 11. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| 103 load+align-prime mnemonics; 1484 placements; census sums to 103 | `[HIGH/OBSERVED]` | `nm libisa-core.so` `Opcode_*` roster + classifier + per-mnemonic `rg -c`; two derivations agree |
| Loads occupy `s0` (LdSt/LdStALU/Ld) + `s1` (Ld); 13–17 placements typical; `lb*` = `s1_ld` only | `[HIGH/OBSERVED]` | `Opcode_*_Slot_<f>_s{0,1}_*_encode` symtab |
| Opcode-sel templates (byte-exact `F0_S0_LdSt` `WORD0`) | `[HIGH/OBSERVED]` | `objdump -d` of the encode thunks |
| Four addressing modes are distinct opcodes (no global update bit) | `[HIGH/OBSERVED]` | non-uniform template deltas (`_i→_ip` ≠ `_x→_xp`) |
| Operand field windows (valign bits `[1:0]`, vec/AR/imm nibbles, width-scaled imm) | `[HIGH/OBSERVED]` | `Field_*_get` thunk bodies + `xtensa-elf-as` device field sweep |
| Exactly 4 `valign` registers (`u0..u3`); `u4` rejected | `[HIGH/OBSERVED]` | 2-bit field + assembler `register number out of range` |
| valign prime→use→update idiom; carry threaded `lalign → la → la` | `[HIGH/OBSERVED]` | `valignr_set_use`/`use` scoreboard + device-decoded `libhal.a` bundles |
| `la*` extract = funnel byte-shift over `{carry ‖ new}`, bit-exact | `[HIGH/OBSERVED by execution]` | `lashift_512_512_6` driven live over all 64 shifts, 0 mismatches; `sel` cross-check |
| AR post-update = 32-bit modular `base += stride`, no fault | `[HIGH/OBSERVED by execution]` | `add_32_32_32` driven live incl. `2^32` wrap |
| 12-stage load pipeline; valign read `@10` / write `@12` | `[HIGH/OBSERVED]` structure; `[MED/INFERRED]` exact result-use cycle | `libcas-core` `*_inst_stage0..11` + `valignr_use` `cmp $0xa` window |
| Loads have no `xdref` value leaf (addressing ops) | `[HIGH/OBSERVED]` | `nm libfiss-base.so \| rg 'xdref_(lvn\|lsn\|la_)'` = 0 |
| `lavn`/`lav` = variable-stride; `la`/`lan` = fixed | `[HIGH/OBSERVED]` | operand-grammar arity (4 operands vs 3) via assembler |
| B06 = loads; `salign`/`sapos` (flush) = B07; `le/lt/ltr` (compare/transpose) excluded | `[HIGH/OBSERVED]` | store-direction selector bit + compute-slot placement + value-leaf presence |

---

## 12. Cross-references

- [The FLIX VLIW Encoding (14 format / 46 slot)](../core/flix-encoding.md) — the `LdSt`/`Ld` memory-port
  slots (`s0`/`s1`) these ops occupy, the F4 dual-load format the align-loads exploit, and the
  encode-thunk `WORD0` ABI.
- [The Eight Register Files](../core/register-files.md) — the `vec` (load destination), `valign` (idx 4,
  512b × 4 carry registers), and `AR` (address/stride) geometry, and the `@10/@12` valign timing.
- [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified `12569`
  placement denominator this batch's `1484` contributes to, and the no-cross-pair law.
- [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the per-instruction
  template this page follows and the B06 partition row.
- [ISA Batch 07 — Vector Stores](b07-stores.md) — the register → memory direction; the `svn*`/`ssn*`
  stores and the store-side `salign*` flush that shares the `valign` file. Boundary is
  [§9](#9-the-b06--b07-boundary).
- [ISA Batch 21 — Select / Shuffle](b21-select-shuffle.md) — the `sel`/`dsel` byte-select primitive the
  valign extract cross-checks against ([§5.2](#52-the-same-result-via-the-byte-select-leaf-cross-check)).
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED`/`INFERRED`/
  `CARRIED` tags and the proven-by-execution value lane used throughout
  [§5](#5-the-valign-extract--driven-live).

---

*Provenance: the encode templates and slot placements are `[HIGH/OBSERVED]` — disassembled
in-checkout from `libisa-core.so` (`ncore2gp/config/`); the operand field windows are `[HIGH/OBSERVED]`
from the `Field_*_get` thunks **and** an `xtensa-elf-as`/`objdump` device round-trip; the valign
rotate-merge extract and the AR post-update in [§5](#5-the-valign-extract--driven-live) are
`[HIGH/OBSERVED by execution]` — the `libfiss-base.so` `lashift`/`sel`/`add_32_32_32` leaves were loaded
via ctypes and run on the inputs shown; the pipeline stages and valign scoreboard are `[HIGH/OBSERVED]`
from `libcas-core.so` symbol tags (exact result-use cycle `[MED/INFERRED]`). The `extracted/` carving is
gitignored; counts are `nm | rg -c` against the binary `.symtab`. All prose reads as derived from
shipped-artifact static analysis (lawful interoperability RE).*
