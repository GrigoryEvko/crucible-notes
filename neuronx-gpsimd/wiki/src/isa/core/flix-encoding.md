# The FLIX VLIW Encoding

This is the authoritative **encoding** reference for the Vision-Q7 "Cairo" (`ncore2gp`)
FLIX/VLIW ISA: the complete **14-format × 46-slot** byte-level table a reimplementer encodes
*and* decodes against. Where the
[FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) page documents the *decode
loop* end to end and works one bundle byte-for-byte, this page **enumerates the tables that
loop indexes**: every format's selector encoding, length class and slot composition; every
slot's bit offset, width, functional unit and the formats that carry it; the format-selection
and length-decode mechanisms as annotated C against the real `libisa-core.so` symbols; the
narrow-vs-wide split; and the encode-thunk ABI (`C7 07 imm32 [C7 47 04 imm32] C3`). Read this
page when you need to *lay out a bundle's bits*; read the companion when you need to *walk a
byte stream*. The two are mutual inverses and are kept numerically consistent.

Everything below is read **directly out of the shipped Tensilica libisa config library**
`libisa-core.so` (`ncore2gp/config/`, sha256 `8fe68bf4…f143e451`, 9 690 712 bytes, ELF64
x86-64, **not stripped**): its `formats[]` / `slots[]` tables, the `format_decoder` /
`length_decoder` bodies, the 256-cell `length_table`, the 12 569 `Opcode_*_encode` thunks, the
46 `Slot_*_get` extractors and the 3237 `Field_*_get` operand accessors are all resolvable
symbols, and every table cell, mask and template constant quoted here was re-disassembled
in-checkout. The device-native `xtensa-elf-objdump`/`xtensa-elf-as` (`XTENSA_CORE=ncore2gp`)
serve as an independent encode/decode **oracle**. `[HIGH/OBSERVED]` throughout except where a
format interior is flagged `[MED/INFERRED]`.

> **Scope — what is NOT here.** The **NCFW management core** is a scalar Xtensa-LX, **not**
> FLIX; nothing on this page applies to NCFW IRAM (see the
> [decoding companion §11](../../reference/flix-decoding.md)). This page documents the
> Vision-Q7 device ISA only.

---

## 1. Encoding key facts

| Fact | Value | Binary source |
|---|---|---|
| Config / uarch | `Xm_ncore2gp` / *Cairo*, `Xtensa24`/XEA3, `NX1.1.4` | `config_table` @ `0x85ea40` |
| `num_formats` | **14** (`0x0E`) | `num_formats` @ `0x3b65e0` → `mov $0xe,%eax` |
| `num_slots` | **46** (`0x2E`) | `num_slots` @ `0x3b6510` → `mov $0x2e,%eax` |
| `num_regfiles` | **8** | `num_regfiles` @ `0x3b5c20` → `mov $0x8,%eax` |
| `interface_version` | `0x76` = **118** | `interface_version` @ `0x3b5b20` |
| Distinct byte-lengths | **4** = `{2, 3, 8, 16}` | `XCHAL_OP0_FORMAT_LENGTHS` row |
| Length-class outcomes | **7** = `{2, 3, 16, 8-narrow, 16-wide(op0=F), illegal, …}` | `length_table` value census |
| `formats[]` | `0x6cd980` (.data.rel.ro), stride **24**, 14 entries | `format_*` accessors |
| `slots[]` | `0x6cdb00` (.data.rel.ro), stride **48**, 46 entries | `slot_*` accessors |
| `length_table[256]` | `0x3d4100` (.rodata, VMA == file off), 256 × `int32` | `length_decoder` |
| `format_decoder` | `0x3b5970` | `w → format 0..13 / −1` |
| `length_decoder` | `0x3b5a50` | raw bytes → byte-length via table |
| `Opcode_*_encode` thunks | **12 569** (`opcodedefs[]` @ `0x6e9640`, stride 24) | `nm \| rg -c` = 12569 |
| `Slot_*_Format_*_get` | **46** (one per slot) | `nm \| rg -c` = 46 |
| `Field_*_get` | **3237** (per `(field × slot)`) | `nm \| rg -c` = 3237 |

> **QUIRK — "7 length-class outcomes" vs "4 distinct byte sizes" are two different numbers; state
> both.** The static Tensilica macro `XCHAL_OP0_FORMAT_LENGTHS = {3×8, 2×6, 16, 8}` yields only
> **4 distinct instruction byte-lengths** `{2, 3, 8, 16}`. The shipped *runtime*
> `length_decoder` (`0x3b5a50`) + 256-entry `length_table` (`0x3d4100`) classifies a word into
> **7 length-class outcomes** — because for `op0==0xF` the length is split further on `byte3`'s
> low nibble into *narrow-8*, *wide-16* and *illegal(−1)*, on top of the `{2,3,16}` it shares
> with the static rule. "7" is the *class* count; "4" is the *distinct size* count. Neither
> supersedes the other; a correct decoder reproduces all 7 classes but only ever advances the
> sweep pointer by one of 4 sizes. `[HIGH/OBSERVED]`

The two entry points are hardcoded, not pointer-table-dispatched: `decode_format_fn` /
`decode_length_fn` return the literal addresses `0x3b5970` / `0x3b5a50`. A reimplementation
hardcodes one format decoder and one length decoder for the whole ISA.

---

## 2. The 14 formats — selector, length, slot composition

The public `formats[]` table (`0x6cd980`, stride 24) stores each format as
`{ char *name; int length; encode_fn }`. Read straight from the bytes (file offset == VMA in
`.data.rel.ro` minus the 0x200000 section delta — applied), the 14 rows are:

| idx | name | len | #slots | issue profile (S0…Sn) | width | `op0` | `b3lo` | encode_fn |
|---|---|---|---|---|---|---|---|---|
| 0 | `x24` | 3 | 1 | `Inst` (base RRR 24-bit) | scalar | 0–7 | any | `0x3b57c0` |
| 1 | `x16a` | 2 | 1 | `Inst16a` (density) | scalar | 8–B | any | `0x3b57d0` |
| 2 | `x16b` | 2 | 1 | `Inst16b` (density) | scalar | C–D | any | `0x3b57f0` |
| 3 | `F0` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 1,5 | `0x3b5810` |
| 4 | `F11` | 16 | **5** | Ld · ALU · Mul · ALU · ALU | wide | E | 8–F | `0x3b5830` |
| 5 | `F1` | 16 | 4 | LdStALU · Ld · Mul · ALU | wide | F | 3,B | `0x3b5850` |
| 6 | `F2` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0x3b5870` |
| 7 | `F3` | 16 | **5** | LdSt · Ld · Mul · ALU · ALU | wide | E | 0–7 | `0x3b5890` |
| 8 | `F4` | 16 | 4 | Ld · Ld · Mul · ALU (dual-load) | wide | F | 9,D | `0x3b58b0` |
| 9 | `F6` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0x3b58d0` |
| 10 | `F7` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0x3b58f0` |
| 11 | `N1` | 8 | 3 | LdSt · None · Mul | narrow | F | 8,A,C,E | `0x3b5910` |
| 12 | `N2` | 8 | 2 | LdSt · Ld | narrow | F | 8,A,C,E | `0x3b5930` |
| 13 | `N0` | 8 | 4 | LdSt · None · None · ALU | narrow | F | 0,2,4,6 | `0x3b5950` |

**Slot-count census:** `1+1+1+4+5+4+4+5+4+4+4+3+2+4 = 46 = num_slots`. There is **no**
`F5`/`F8`/`F9`/`F10` — the gaps in the `Fn` numbering are real. The two 5-slot formats are
`F3` and `F11`. `F4` (dual-Ld) and `F11` carry **no** dedicated store slot. `None` slots are
NOP-only filler positions (1–3 bit encodings) the narrow formats use to pad.

> **GOTCHA — the table index order is not the name order.** In `formats[]`, index 4 is `F11`
> and index 7 is `F3` (the 5-slot formats sit at idx 4 and 7, not in `Fn`-numeric order); the
> narrow formats are `N1`(11), `N2`(12), `N0`(13) — `N0` is *last*, not first. Drive everything
> off the format **name**, not its table index.

### 2.1 Format signature templates (`Format_<F>_encode`)

Each `Format_<F>_encode` is a 4-instruction stub that `movdqa`s a 16-byte zero-template from a
`.rodata` constant pool (`CONST_TBL_ai4c_0` region, `0x3e2c70…0x3e2d10`) into the bundle and
writes it twice (covering both 8-byte halves). The first 4 template bytes are the **format
signature**: `byte0` carries `op0` in `[3:0]`, `byte3` carries the sub-format selector. Read
verbatim from `.rodata`:

| format | template bytes (byte0..byte3) | `op0` | `byte3` | decodes back to |
|---|---|---|---|---|
| `F0` | `0f 00 00 01` | F | `0x01` | F0 |
| `F11` | `0e 00 00 08` | E | `0x08` | F11 |
| `F1` | `0f 00 00 03` | F | `0x03` | F1 |
| `F2` | `0f 00 00 33` | F | `0x33` | F2 |
| `F3` | `0e 00 00 00` | E | `0x00` | F3 |
| `F4` | `0f 00 00 09` | F | `0x09` | F4 |
| `F6` | `0f 00 00 23` | F | `0x23` | F6 |
| `F7` | `0f 00 00 13` | F | `0x13` | F7 |
| `N1` | `0f 00 00 08` | F | `0x08` | N1 |
| `N2` | `0f 00 00 18` | F | `0x18` | N2 |
| `N0` | `0f 00 00 00` | F | `0x00` | N0 |

Every signature byte feeds back through the `format_decoder` predicate ladder (§3) and returns
its own format — the encoder and decoder are mutual inverses **at the framing level**. The
operand slotfill never touches `byte0[3:0]` (selector) — the minimum operand bit is 4 — so
selector and operand fields are provably disjoint. `[HIGH/OBSERVED]`

> **NOTE — `F1`/`F2`/`F6`/`F7` share `op0=F, b3lo∈{3,B}`.** The four near-identical 4-slot
> wide formats are separated *only* by the `byte3` high bits (24/25/28/29), giving the four
> distinct full-byte3 signatures `0x03`/`0x33`/`0x23`/`0x13`. Their **mask constants are
> `[HIGH/OBSERVED]`**; *why* four near-identical LdSt·Ld·Mul·ALU formats exist (operand-size
> sub-classes) is `[MED/INFERRED]`. They differ structurally in their Mul/ALU slot bit offsets
> (e.g. F1 mul@41 vs F2 mul@27; F6 alu@36 vs F7 alu@31 — see §5).

---

## 3. Format selection — `format_decoder` (`0x3b5970`)

`format_decoder` reads `w = *(uint32_le*)inst` and runs a fixed mask-and-match ladder; first
hit wins, else `−1`. Reproduced from the disassembled body — **every mask/compare immediate
below was read directly from the `and $imm` / `cmp $imm` bytes** (`f6 c2 08`, `83 e0 0c`,
`81 e1 0f 00 00 0b`, …):

```c
int format_decoder(const uint8_t *inst) {
    uint32_t w = inst[0] | (inst[1]<<8) | (inst[2]<<16) | ((uint32_t)inst[3]<<24);
    if ((w & 0x00000008) == 0)          return  0; // x24    op0 0..7        (test $0x8,%dl; je)
    if ((w & 0x0000000C) == 0x8)        return  1; // x16a   op0 8..B
    if ((w & 0x0000000E) == 0xC)        return  2; // x16b   op0 C..D
    if ((w & 0x0B00000F) == 0x0100000F) return  3; // F0     op0=F, b3lo {1,5}
    if ((w & 0x0800000F) == 0x0800000E) return  4; // F11    op0=E, byte3 bit27 set
    if ((w & 0x3700000F) == 0x0300000F) return  5; // F1     op0=F, b3lo {3,B}, sel 0x03
    if ((w & 0x3700000F) == 0x3300000F) return  6; // F2     op0=F, b3lo {3,B}, sel 0x33
    if ((w & 0x0800000F) == 0x0000000E) return  7; // F3     op0=E, byte3 bit27 clear
    if ((w & 0x0B00000F) == 0x0900000F) return  8; // F4     op0=F, b3lo {9,D}
    if ((w & 0x3700000F) == 0x2300000F) return  9; // F6     op0=F, b3lo {3,B}, sel 0x23
    if ((w & 0x3700000F) == 0x1300000F) return 10; // F7     op0=F, b3lo {3,B}, sel 0x13
    if ((w & 0x1900000F) == 0x0800000F) return 11; // N1     op0=F, b3lo {8,A,C,E}, sel 0x08
    if ((w & 0x1900000F) == 0x1800000F) return 12; // N2     op0=F, b3lo {8,A,C,E}, sel 0x18
    if ((w & 0x0900000F) == 0x0000000F) return 13; // N0     op0=F, b3lo {0,2,4,6}
    return -1;                                      // illegal: op0=F, b3lo {7,F}
}
```

> **GOTCHA — the shipped body is a `cmovne`, not a clean fall-through.** The real machine code
> (`3b5a27: and $0x900000f; cmp $0xf; cmovne %edx,%eax; ret`) computes the last predicate
> with a conditional move and the F3 case as a bare `cmp $0xe` against the `op0=E` masked word
> (`3b59e6: cmp $0xe,%esi; je`). The C above is the *behavioural* equivalent and was verified
> equal to the binary by 256-cell simulation (§4); reproduce the *behaviour*, not the exact
> instruction selection.

The `F1`/`F2`/`F6`/`F7` quartet all enter at `op0==0xF, b3lo∈{3,B}` and are separated purely by
the `0x3700000F`-masked byte-3 selector matching `0x03`/`0x33`/`0x23`/`0x13`. `N1`/`N2` share
`b3lo∈{8,A,C,E}` and split on the `0x1900000F`-masked value `0x08`/`0x18`. `[HIGH/OBSERVED]`

---

## 4. Length decode — `length_decoder` (`0x3b5a50`) + `length_table` (`0x3d4100`)

`length_decoder` takes **raw bytes** (not the assembled word), forms an 8-bit index from
`byte3.low4` (row) and `byte0.low4` (column), and indexes the 256-entry `int32` table.
Disassembled verbatim (`movzbl 0x3(%rdi); shl $4; movzbl %al; movzbl (%rdi); and $0xf; or;
mov (tbl,rax,4)`):

```c
int length_decoder(const uint8_t *inst) {
    unsigned idx = ((unsigned)(inst[3] << 4) & 0xFF) | (inst[0] & 0x0F);
    return length_table[idx];                  // .rodata 0x3d4100, 256 × int32 LE
}
```

The table is `byte3.low4` (row 0..15) × `byte0.low4` (column 0..15). Read straight from
`.rodata`: the `b3lo==0` row is the literal bytes `03 03 03 03 03 03 03 03 02 02 02 02 02 02
10 08` — i.e. `3,3,3,3,3,3,3,3,2,2,2,2,2,2,16,8`. Only the **last column** (`op0==0xF`) varies
with `b3lo`:

| `b3lo` ↓  `op0` → | 0–7 | 8–B | C–D | E | F |
|---|:---:|:---:|:---:|:---:|:---:|
| `0,2,4,6,8,a,c,e` (even) | 3 | 2 | 2 | 16 | **8** (narrow `N0`/`N1`/`N2`) |
| `1,3,5,9,b,d` (odd) | 3 | 2 | 2 | 16 | **16** (wide `F0`/`F1`/`F2`/`F4`/`F6`/`F7`) |
| `7, f` | 3 | 2 | 2 | 16 | **−1** (illegal) |

Value census over the 256 cells: `{3:128, 2:96, 16:22, 8:8, −1:2}`. Spot-verified rows in this
pass: `b3lo=0` last cell = `0x08`; `b3lo=1` last cell = `0x10`(16); `b3lo=7` and `b3lo=f` last
cell = `0xffffffff`(−1) — all byte-exact. `[HIGH/OBSERVED]`

> **CORRECTION — the static `XCHAL_BYTE0_FORMAT_LENGTHS` macro is lossy and will desync you.**
> The Tensilica static macro keys length on `byte0` *alone* and flattens `op0==0xF` to a
> constant **8** bytes. The shipped runtime `length_decoder` does **not**: it also reads
> `byte3`'s low nibble, so a real `op0==0xF` word decodes to 8, 16, **or** illegal. A linear
> sweep that assumes "`op0==0xF` ⇒ 8 bytes" mis-lengths every `F0`/`F1`/`F2`/`F4`/`F6`/`F7`
> bundle (those are `op0==0xF`, 16 bytes) as two 8-byte reads and desyncs. **The binary
> `length_table` is authoritative.**

**Self-consistency proof (re-run this pass).** Simulating the `format_decoder` C above over the
full `(byte0.low4 × byte3)` space and comparing each result's format length against
`length_table` yields **0 mismatches / 4096 combos** — every cell the decoder calls a wide
format is 16, every narrow is 8, every illegal is −1; all 14 formats are reachable. The two
firmware bundles in §7 classify byte-exactly (`4f 80 a0 80` → N0/8; `4f ff 42 41` → F0/16).
`[HIGH/OBSERVED]`

---

## 5. The 46-slot roster — bit offsets, widths, units, formats

A bundle's bits do **not** divide into contiguous slot windows. Each slot is read by one
`Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<bitoff>_get` thunk, which **scatters** the slot's bits out
of the (up to four) 32-bit lanes into a normalized 32-bit `slotbuf`. The `slots[]` table
(`0x6cdb00`, stride 48 = `{name, format, nop, position, get, set}`) and the thunk *symbol name*
agree by address: the get address embedded in the symbol equals `slots[i].get`.

The complete roster — `bitoff` = the slot field's start bit (HIGH: symbol `<bitoff>` token +
get-code first read agree); `width` = decoded slot-word bit-length (`[MED/INFERRED]`,
machine-code-emulated, ±1–2 bits on scattered FLIX slots); `pos` = sequential issue index;
`get` from `slots[i]` table, all verified byte-exact this pass:

| # | slot | fmt | unit | pos | bitoff | width | get_fn |
|---|---|---|---|:--:|:--:|:--:|---|
| 0 | `Inst` | x24 | whole | 0 | 0 | 24 | `0x3b0450` |
| 1 | `Inst16a` | x16a | whole | 0 | 0 | 16 | `0x3b04b0` |
| 2 | `Inst16b` | x16b | whole | 0 | 0 | 16 | `0x3b0500` |
| 3 | `F0_S0_LdSt` | F0 | ldst | 0 | 4 | 32 | `0x3b0550` |
| 4 | `F0_S1_Ld` | F0 | ld | 1 | 16 | 26 | `0x3b0880` |
| 5 | `F0_S2_Mul` | F0 | mul | 2 | 28 | 28 | `0x3b0a10` |
| 6 | `F0_S3_ALU` | F0 | alu | 3 | 36 | 32 | `0x3b0c70` |
| 7 | `F11_S0_Ld` | F11 | ld | 0 | 4 | 30 | `0x3b0ee0` |
| 8 | `F11_S1_ALU` | F11 | alu | 1 | 16 | 21 | `0x3b10c0` |
| 9 | `F11_S2_Mul` | F11 | mul | 2 | 41 | 22 | `0x3b1220` |
| 10 | `F11_S3_ALU` | F11 | alu | 3 | 31 | 25 | `0x3b13e0` |
| 11 | `F11_S4_ALU` | F11 | alu | 4 | 24 | 24 | `0x3b15e0` |
| 12 | `F1_S0_LdStALU` | F1 | ldstalu | 0 | 4 | 32 | `0x3b17e0` |
| 13 | `F1_S1_Ld` | F1 | ld | 1 | 16 | 26 | `0x3b19f0` |
| 14 | `F1_S2_Mul` | F1 | mul | 2 | 41 | 29 | `0x3b1ba0` |
| 15 | `F1_S3_ALU` | F1 | alu | 3 | 31 | 32 | `0x3b1dd0` |
| 16 | `F2_S0_LdSt` | F2 | ldst | 0 | 4 | 32 | `0x3b1fd0` |
| 17 | `F2_S1_Ld` | F2 | ld | 1 | 16 | 25 | `0x3b2250` |
| 18 | `F2_S2_Mul` | F2 | mul | 2 | 27 | 30 | `0x3b2400` |
| 19 | `F2_S3_ALU` | F2 | alu | 3 | 31 | 31 | `0x3b2680` |
| 20 | `F3_S0_LdSt` | F3 | ldst | 0 | 4 | 29 | `0x3b28e0` |
| 21 | `F3_S1_Ld` | F3 | ld | 1 | 16 | 22 | `0x3b2a60` |
| 22 | `F3_S2_Mul` | F3 | mul | 2 | 28 | 22 | `0x3b2b90` |
| 23 | `F3_S3_ALU` | F3 | alu | 3 | 33 | 26 | `0x3b2d20` |
| 24 | `F3_S4_ALU` | F3 | alu | 4 | 24 | 24 | `0x3b2f90` |
| 25 | `F4_S0_Ld` | F4 | ld | 0 | 4 | 31 | `0x3b3170` |
| 26 | `F4_S1_Ld` | F4 | ld | 1 | 16 | 24 | `0x3b32f0` |
| 27 | `F4_S2_Mul` | F4 | mul | 2 | 28 | 32 | `0x3b34a0` |
| 28 | `F4_S3_ALU` | F4 | alu | 3 | 36 | 32 | `0x3b36b0` |
| 29 | `F6_S0_LdSt` | F6 | ldst | 0 | 4 | 32 | `0x3b38c0` |
| 30 | `F6_S1_Ld` | F6 | ld | 1 | 16 | 26 | `0x3b3c40` |
| 31 | `F6_S2_Mul` | F6 | mul | 2 | 41 | 26 | `0x3b3e70` |
| 32 | `F6_S3_ALU` | F6 | alu | 3 | 36 | 32 | `0x3b40d0` |
| 33 | `F7_S0_LdSt` | F7 | ldst | 0 | 4 | 32 | `0x3b4340` |
| 34 | `F7_S1_Ld` | F7 | ld | 1 | 16 | 25 | `0x3b45d0` |
| 35 | `F7_S2_Mul` | F7 | mul | 2 | 41 | 29 | `0x3b4780` |
| 36 | `F7_S3_ALU` | F7 | alu | 3 | 31 | 32 | `0x3b49c0` |
| 37 | `N1_S0_LdSt` | N1 | ldst | 0 | 4 | 26 | `0x3b4c30` |
| 38 | `N1_S1_None` | N1 | none | 1 | 54 | 3 | `0x3b4db0` |
| 39 | `N1_S2_Mul` | N1 | mul | 2 | 16 | 28 | `0x3b4e70` |
| 40 | `N2_S0_LdSt` | N2 | ldst | 0 | 4 | 32 | `0x3b5090` |
| 41 | `N2_S1_Ld` | N2 | ld | 1 | 16 | 25 | `0x3b5250` |
| 42 | `N0_S0_LdSt` | N0 | ldst | 0 | 4 | 24 | `0x3b5400` |
| 43 | `N0_S1_None` | N0 | none | 1 | 58 | 1 | `0x3b5530` |
| 44 | `N0_S2_None` | N0 | none | 2 | 59 | 1 | `0x3b55b0` |
| 45 | `N0_S3_ALU` | N0 | alu | 3 | 16 | 32 | `0x3b5610` |

All 46 names, positions, get addresses **and** `<bitoff>` symbol tokens were re-read from
`slots[]` + the `Slot_*_get` symtab this pass; every cell agrees.

### 5.1 Functional-unit vocabulary

| unit | meaning | where |
|---|---|---|
| `whole` | the whole base/density instruction (not a FLIX slot) | x24 / x16a / x16b s0 |
| `ldst` | combined load/store slot | s0 of F0,F2,F3,F6,F7,N0,N1,N2 |
| `ldstalu` | load/store **+** ALU fused slot | F1 s0 only |
| `ld` | load-only slot | s1 of every wide format; F4 s0; F11 s0; N2 s1 |
| `mul` | multiply slot | s2 of every wide format; N1 s2; **absent in N0, N2** |
| `alu` | arithmetic/logic slot | last slot(s); N0 s3; **F3 & F11 carry two/three** |
| `none` | NOP-only placeholder (1–3 bit width), no real issue unit | N0 s1/s2, N1 s1 |

> **GOTCHA — three "load/store" rules a reimplementer must internalize.**
> (1) **Bit offset ≠ `slots[].position`.** `slot_position` (struct +0x18) returns the
> *sequential* index 0..N-1; the real bundle offset lives only in the get thunk's `<bitoff>`
> token / scatter body. (2) **`width ≠ next_offset − offset`** — above its low byte each slot
> scatters into high bits, so the gather body is authoritative, not the offset arithmetic.
> (3) **Issue order is not bit order.** `Format_*_slots[]` is in *issue* order, which is
> physically out-of-order for the 5-slot formats: e.g. `F3`'s S4 ALU sits at bit 24, *before*
> S2 Mul (28) and S3 ALU (33); `F11`'s S4 ALU at bit 24 likewise precedes S3 (31) and S2 Mul
> (41). Every `s0` starts at bit 4 because bits `[3:0]` are always the `op0` selector.

> **NOTE — the unit *label* is not a strict store-capability gate.** `F4`'s s0 is named `Ld`
> yet carries 3 true integer stores (`S32I`/`S16I`/`S8I`); `F11`'s s0 is also `Ld` but carries
> **zero** stores — `F11` is the genuinely no-store wide format. `F6`/`F7` s0 are full `LdSt`.
> The store capability is set per-mnemonic via `opcodes[].flags` bit 5, not by the slot name.

### 5.2 A gather thunk, disassembled (`N0_S0_LdSt`)

`Slot_n0_Format_n0_s0_ldst_4_get` (`0x3b5400`) zero-fills `slotbuf[1..7]` then assembles
`slotbuf[0]` from seven scattered ranges across the two 8-byte-bundle lanes (`insn0 = *(%rdi)`,
`insn1 = *(0x4+%rdi)`):

```c
uint32_t n0_s0_ldst_get(uint32_t insn0, uint32_t insn1) {
    uint32_t o  =  (insn0 & 0x00000F00) >> 8;          // bundle [11:8]  -> slotbuf [3:0]
             o |=  (insn0 & 0x000000F0);               // bundle [7:4]   -> slotbuf [7:4]
             o |=  (insn0 & 0x0000F000) >> 4;          // bundle [15:12] -> slotbuf [11:8]
             o |= ((insn0 & 0x00200000) >> 21) << 12;  // bundle bit 21  -> slotbuf bit 12
             o |=  (insn1 & 0x00000006) << 12;         // bundle [33:32] -> slotbuf [14:13]
             o |=  (insn1 & 0x03FC0000) >> 3;          // bundle [57:50] -> slotbuf [22:15]
             o |= ((insn1 & 0x40000000) >> 30) << 23;  // bundle bit 62  -> slotbuf bit 23
    return o;
}
```

The wide-format gathers are the same shape but read up to three lanes:
`Slot_f0_Format_f0_s0_ldst_4_get` (`0x3b0550`) reads `(%rdi)`, `0x4(%rdi)` **and** `0x8(%rdi)`,
confirming a wide slot's bits genuinely span the bundle's lanes. The companion's
[worked carve](../../reference/flix-decoding.md) walks this exact thunk to the operand; this
page does not duplicate that — it expands the *roster* the carve sampled.

---

## 6. Operand placement — the encode side of the codec

Operands are deposited per `(opcode × slot)` by the 12 569 `Opcode_<mnem>_Slot_<slot>_encode`
thunks (`opcodedefs[]` @ `0x6e9640`) and per `(field × slot)` by the 3237 `Field_*_get` /
`Field_*_set` accessors. The **opcode-selector** template is written by the encode thunk; the
**operand** bits are deposited separately by `field_set`. The selector and operand bit-regions
are disjoint (operand bits ≥ 4; selector bits 0–3 = `op0`), which is why encode and decode round-trip.

### 6.1 The universal encode-thunk ABI

Every `Opcode_*_encode` is a tiny `.text` stub of one fixed shape:

```
void Opcode_<mnem>_Slot_<slot>_encode(uint32_t *slotword /*rdi*/) {
    *(uint32_t*)(rdi+0) = WORD0_TEMPLATE;     //  C7 07 <imm32>        (always)
    [ *(uint32_t*)(rdi+4) = WORD1; ]          //  C7 47 04 <imm32>     (2-lane / wide only)
    return;                                   //  C3
}
```

Byte form: **`C7 07 <imm32>  [ C7 47 04 <imm32> ]  C3`**.

* `WORD0_TEMPLATE` = the fixed **opcode-selector** word for that `(mnemonic, slot)`; operand
  fields are zero in the template and deposited later by `field_set`.
* The optional second `movl` (`C7 47 04`) appears for slots whose normalized opcode word spills
  past bit 32. **`WORD1 == 0x00000000` is an invariant** across all 12 569 thunks (the upper
  lane carries no selector bits; it is merely cleared) — verified with zero exceptions.

**Spot-checks (byte-exact this pass):**

| thunk | bytes | template |
|---|---|---|
| `Opcode_addi_Slot_n0_s0_ldst_encode` (`0x3389b0`) | `c7 07 00 00 24 00  c3` | `0x00240000` |
| `Opcode_xor_Slot_inst_encode` (`0x33a000`) | `c7 07 00 00 30 00` | `0x00300000` |
| `Opcode_rsr_sar_Slot_inst_encode` (`0x33cca0`) | `c7 07 00 03 03 00` | `0x00030300` |
| `Opcode_wsr_sar_Slot_inst_encode` (`0x33ccb0`) | `c7 07 00 03 13 00` | `0x00130300` |
| `Opcode_xsr_sar_Slot_inst_encode` (`0x33ccc0`) | `c7 07 00 03 61 00` | `0x00610300` |
| `Opcode_ivp_dselnx16t_Slot_f0_s3_alu_encode` (`0x35c7e0`) | `c7 07 00…  c7 47 04 00 00 00 00  c3` | word0=0, **word1=0** (2-lane) |

The **symbol mangling rule** maps the `opcodedefs[]` row to the symtab: `Opcode_<mnem>_Slot_<slot>_encode`
where (1) `'.'→'_'` in the mnemonic (`add.n`→`add_n`, `wur.fsr`→`wur_fsr`) and (2) the slot
token is lowercased (`Inst`→`inst`, `F2_S0_LdSt`→`f2_s0_ldst`). Both transforms are required;
the mangling is injective. `[HIGH/OBSERVED]`

### 6.2 The two-tier selector model

The bit that distinguishes a signed/unsigned, lane-`_0`/`_1`, fp-predicate, post-increment or
shift-type variant is **not** a single global bit. It is a **two-tier** system:

* **Tier (i) — `opcodes[].flags` (`+0x18`)** carries the *global, format-independent* semantic
  markers: bit0 relative-immediate/branch (59), bit2 loop (6), bit3 call (4), bit4 load (125),
  bit5 store (118), bit7 `t`-suffix (32), bit8 core/serializing (98), **bit11 post-update**
  (`_ip`/`_xp`, 80), bit17 scatter (12), bit19 vector sub-class (193). These are uniform across
  formats.
* **Tier (ii) — the per-slot encode template** carries the *format-local* opcode-selector bits,
  where width/precision/predicate/lane/shift-type variants are *distinct opcodes* (own iclass)
  realized by per-format packing. Examples, byte-exact this pass:
  * fp-compare predicate on `F1_S3_ALU` is a clean `+0x1000` nibble step:
    `ivp_oeqn_2xf32`=`0x2705c800`, `ivp_olen_2xf32`=`0x2706c800`, `ivp_oltn_2xf32`=`0x2707c800`.
  * the *same* eq/le/lt distinction on `N1_S2_Mul` is a `bits[7:4]` nibble, and its per-format
    XOR drifts wildly across formats — so `+0x1000` is **not** a roster-wide bit.

> **CORRECTION — the "u-bit=bit0 / lane=bit13 / fp-pred=bit12" triad is REFUTED as a global
> invariant.** Where a bit-region coincidence occurs it is a *format-local enumerated field*,
> not a single global bit; on most formats the same semantic distinction lands at a different
> bit or is a distinct opcode entirely. The x24 `Inst` template (canonical Xtensa RST0/RRR
> `op2`/`t` enumerations) is the selector ground truth. `[HIGH/OBSERVED]`

The scalar `Inst` template **is** the literal 24-bit Xtensa instruction word (AR fields zeroed).
The SR family is `(op1,op2)`-selected with the SR number in bits `[15:8]`: `RSR` base
`0x00030000`, `WSR` `0x00130000`, `XSR` `0x00610000`; e.g. SAR(0x03) → `rsr.sar`=`0x00030300`,
`wsr.sar`=`0x00130300`, `xsr.sar`=`0x00610300` (spot-checked above). `WUR` base `0x00f30000 |
(UR<<8)`. The full assemble direction, `field_set`/`do_reloc` and the operand semantics
(sign-extension, scaling, PC-relative rebasing) are on the
[libisa Table Schema & Codec ABI](libisa-table-schema.md) page.

### 6.3 Per-slot placement census

The `opcodedefs[]` matrix is fully populated — **all 46 slots host ≥ 1 opcode**, summing to
**12 569**. Re-counted this pass by grouping all `Opcode_*_Slot_<slot>_encode` symbols:

```
F0:   S0_LdSt 348  S1_Ld 260  S2_Mul 322  S3_ALU 564                      = 1494
F1:   S0_LdStALU 542  S1_Ld 260  S2_Mul 449  S3_ALU 558                   = 1809
F2:   S0_LdSt 213  S1_Ld 257  S2_Mul 535  S3_ALU 544                      = 1549
F3:   S0_LdSt 342  S1_Ld 256  S2_Mul 244  S3_ALU 503  S4_ALU 91           = 1436
F4:   S0_Ld 192  S1_Ld 249  S2_Mul 61  S3_ALU 251                         =  753
F6:   S0_LdSt 329  S1_Ld 266  S2_Mul 203  S3_ALU 247                      = 1045
F7:   S0_LdSt 348  S1_Ld 257  S2_Mul 521  S3_ALU 548                      = 1674
F11:  S0_Ld 93  S1_ALU 66  S2_Mul 203  S3_ALU 233  S4_ALU 91             =  686
N0:   S0_LdSt 167  S1_None 1  S2_None 1  S3_ALU 483                       =  652
N1:   S0_LdSt 176  S1_None 1  S2_Mul 381                                  =  558
N2:   S0_LdSt 360  S1_Ld 220                                              =  580
x24:  Inst 319     x16a: Inst16a 4     x16b: Inst16b 10                   =  333
                                                                  TOTAL  = 12569
```

The three `None` slots (`N0_S1`, `N0_S2`, `N1_S1`) are populated by **exactly one** opcode,
`nop` — they are NOP-only filler, not real issue units. `[HIGH/OBSERVED]`

---

## 7. Narrow vs wide — the lane/slot/length split

The narrow/wide distinction changes lane count, slot roster **and** length, and is the single
most common cause of a mis-synced sweep.

| | wide | narrow |
|---|---|---|
| formats | `F0,F1,F2,F3,F4,F6,F7,F11` (8) | `N0,N1,N2` (3) |
| size | **16 bytes** = four 32-bit lanes (128-bit) | **8 bytes** = two 32-bit lanes (64-bit) |
| real issue slots | **4 or 5** (LdSt/Ld + Mul + 1–3 ALU) | **2–4** (some `None` filler) |
| lead | `op0==0xE` (→F3/F11) or `op0==0xF` + **odd** `b3lo` | `op0==0xF` + **even** `b3lo` |
| operand words read | `word0..word3` (scatters into all 4 lanes) | `word0/word1` only — `word2/word3` **never touched** |

The narrow profiles: `N0` = LdSt + 2 `None` + ALU (ALU-heavy; ALU anchored low at bit 16
because there is no room above 64 bits); `N1` = LdSt + `None` + Mul (Mul-heavy); `N2` = LdSt +
Ld (LdSt-heavy, no Mul/ALU). The narrow immediate scatter is *format-specific*: the RRI8 `imm8`
sign bit lands at bundle bit **50** (N0), **38** (N1), **38** (N2); the mid bit at **21**
(N0/N1) vs **25** (N2); all three sign-extend identically to `-128..127`.

> **GOTCHA — treating an `op0=F`/odd-`b3lo` wide bundle as narrow reads 8 bytes and desyncs.**
> A reimplementation must branch on **length** *before* allocating the slot buffer: a wide
> gather reads `insn[0..3]`, a narrow gather reads only `insn[0..1]`. Mis-lengthing a wide
> bundle lands the sweep pointer in the middle of the bundle's third lane and desyncs for the
> rest of the function. (See the static-macro `CORRECTION` in §4.)

### 7.1 Oracle round-trip (device-native assembler)

The strongest end-to-end check that the encoding model is correct: feed the device-native
`xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) the **mnemonics** and confirm it emits the **exact
bytes** the carved firmware carries, then disassemble back. For the worked `N0` bundle:

```
source : { addi a4, a0, 24 ; nop ; nop ; ivp_dextrprn_2x32 pr12, v2, v0, 1, 6 }
as out : 4f 80 a0 80 11 c3 20 21            (8 bytes; op0=0xF, b3lo=0 -> N0)
objdump: { addi a4, a0, 24; nop; nop; ivp_dextrprn_2x32 pr12, v2, v0, 1, 6 }
```

byte-identical to the firmware bytes at `0x1000520` in the Cayman `EXTISA_0` image
(sha256 `910d41c3…`). The encoder, the model, and the oracle agree byte-for-byte. The
companion's [worked carve](../../reference/flix-decoding.md) demonstrates the inverse
(bytes → per-slot disassembly) on this same bundle. `[HIGH/OBSERVED]`

---

## 8. The 8 register files (operand targets)

Operands name one of **8** register files (`num_regfiles` @ `0x3b5c20` → `mov $0x8,%eax`;
`regfiles[]` @ `0x74a800`, stride 56). They bound the operand index widths the slots carry:

| idx | name | short | bits | entries | package | note |
|---|---|---|--:|--:|---|---|
| 0 | `AR` | `a` | 32 | 64 | xt_xtensa | core address regs (6-bit windowed index, `& 0x3f`) |
| 1 | `BR` | `b` | 1 | 16 | xt_booleans | boolean regs |
| 2 | `vec` | `v` | 512 | 32 | xt_ivp32 | NX16 vector regs (5-bit index) |
| 3 | `vbool` | `vb` | 64 | 16 | xt_ivp32 | per-lane predicate masks |
| 4 | `valign` | `u` | 512 | 4 | xt_ivp32 | alignment regs (load/store align) |
| 5 | `wvec` | `wv` | 1536 | 4 | xt_ivp32 | wide accumulator (3× NX16) |
| 6 | `b32_pr` | `pr` | 64 | 16 | xt_ivp32 | 32-bit predicate/extract regs |
| 7 | `gvr` | `gr` | 512 | 8 | xt_ivp32 | gather/scatter staging regs (flags `0x0d`, the unique extra bit `0x08`) |

Scalar AR fields are 4 bits in the slot (3 index + 1 window/bank bit, XOR-inverted, ×8, plus
window-base, masked to 6 bits → 64 physical AR). Vector `vec` fields are 5-bit scattered indices
(32 registers); `regload` then ×16-gathers each index into a 512-bit value block. The full file
model is on the [Eight Register Files](register-files.md) page. `[HIGH/OBSERVED]`

---

## 9. Confidence ledger & the desync wall

**HIGH / OBSERVED** — read from immediates / `.rodata` / disassembly this pass:

* `num_formats=14`, `num_slots=46`, `num_regfiles=8`, `interface_version=118`; the
  `format_decoder` mask/compare ladder and every constant; the `length_decoder` index math and
  all 256 `length_table` cells (incl. the odd/illegal rows); the 256-cell self-consistency (0
  mismatch / 4096 combos).
* The 14 formats' names/lengths/encode signatures; the full 46-slot roster (names, positions,
  bit offsets, get addresses, `<bitoff>` tokens) — all byte-exact against `slots[]` + symtab.
* The encode-thunk ABI (`C7 07 imm32 [C7 47 04 imm32] C3`), the `word1==0` invariant, the
  mangling rule; the 12 569 placement census summing exactly; the SR/UR/xor/fp-pred selector
  spot-checks.
* The device-assembler oracle round-trip for the `N0` bundle (byte-identical to firmware).

**MED / INFERRED** — flag these:

* `F1`/`F2`/`F6`/`F7` *why-four-formats*: the byte-3 selector **mask constants are HIGH**; the
  *meaning* of the byte-3 high bits (24/25/28/29) and *why* four near-identical 4-slot formats
  exist is interpretive `[MED/INFERRED]`.
* Per-slot decoded **width** (bits): machine-code-emulated, ±1–2 bits on scattered slots.
* `F4`/`F6` slot **interiors**: per the
  [decoding companion](../../reference/flix-decoding.md), some `F4`/`F6` per-slot
  mnemonic/operand bit-exactness is inferred from the *identical* decode path rather than
  exhaustively oracle-confirmed on a specific bundle. **Flag any `F4`/`F6` interior decode as
  `[MED/INFERRED]`** unless objdump independently confirms that bundle.

> **CORRECTION (carried) — N0/N1/N2 ARE inverse-proven.** A prior synthesis noted "no
> `SX-ISS-12`" and tagged the narrow field codecs as ABI-consistent-but-not-cross-validated.
> The narrow slotfill cross-validation *does* exist: every N0/N1/N2 `(slot × field)` cell has
> its `field_set == field_get⁻¹` proven over the full field range and its bundle-bit positions
> matched against the `libisa-core` get-thunk composition with **no mismatch**. The narrow
> formats are inverse-proven, not merely ABI-consistent. `[HIGH/OBSERVED]`

### The FLIX-desync wall

A byte-exact length decoder is **necessary but not sufficient** to sweep dense device `.text`.
The image interleaves literal pools and hand-written boot/vector stubs directly between bundles,
with no in-band tag distinguishing a literal `uint32` from a bundle word: a literal whose low
nibble is `0..7` looks like a 3-byte `x24`; a literal with `op0==0xF`/even-`byte3` looks like an
8-byte `N0`; the sweep advances by the wrong length and locks onto the next bundle's interior.
The reliable desync tells are (1) `length_decoder` returning −1 (a hard illegal), (2) a
branch/call operand resolving outside `[.text_base, .text_base+size)`, (3) a `None`-slot
decoding to a real op or an out-of-range operand. Recovery re-anchors on a `entry`/`retw.n`
landmark or the next `.xt.prop.*` function-start record; the residual literal pools and boot
stub render as `.byte` and are **closable only with the per-image property records**, not a
smarter byte heuristic. (Full treatment in the
[decoding companion §10](../../reference/flix-decoding.md).) `[HIGH/OBSERVED]`

---

## 10. Symbol & table map

| Symbol / table | Address | Role |
|---|---|---|
| `formats[]` | `0x6cd980` (.data.rel.ro) | 14 × `{name,length,encode}`, stride 24 (§2) |
| `slots[]` | `0x6cdb00` (.data.rel.ro) | 46 × `{name,format,nop,position,get,set}`, stride 48 (§5) |
| `format_decoder` | `0x3b5970` | `w → format 0..13 / −1` (§3) |
| `length_decoder` | `0x3b5a50` | raw bytes → length via table (§4) |
| `length_table` | `0x3d4100` (.rodata) | 256 × `int32`, byte3/byte0 → length (§4) |
| `opcodedefs[]` | `0x6e9640` | 12 569 × `(opcode×slot)` placement, stride 24 (§6) |
| `regfiles[]` | `0x74a800` (.data.rel.ro) | 8 × regfile, stride 56 (§8) |
| `Slot_n0_Format_n0_s0_ldst_4_get` | `0x3b5400` | worked N0 LdSt gather (§5.2) |
| `Opcode_addi_Slot_n0_s0_ldst_encode` | `0x3389b0` | `addi` template `0x240000` (§6.1) |
| `Format_F0_encode` | `0x3b5810` | F0 signature template `0f 00 00 01` (§2.1) |
| `num_formats`/`num_slots`/`num_regfiles` | `0x3b65e0`/`0x3b6510`/`0x3b5c20` | `14`/`46`/`8` |
| `interface_version` | `0x3b5b20` | libisa ABI rev `118` |

All addresses are in `libisa-core.so` (`ncore2gp/config/`). The slotfill operand decoders that
prove `field_set == field_get⁻¹` live in the sibling `libfiss-base.so`.

---

## 11. Cross-references

* [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — the decode loop, the
  worked carve, and the desync-recovery strategy; the inverse of this page.
* [The Canonical ISA Decode Model (libisa-core)](libisa-decode-model.md) — the libisa-core
  pipeline these tables drive.
* [The libisa Table Schema & Codec ABI](libisa-table-schema.md) — the `(opcode × slot)` encode
  matrix, the `field_set`/`do_reloc` assemble direction, and operand semantics.
* [The Eight Register Files](register-files.md) — the 8 operand-target files in full.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[MED/INFERRED]` and "wall" mean.
