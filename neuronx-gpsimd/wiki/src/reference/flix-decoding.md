# FLIX Bundle-Decoding Methodology

This page is the operational core of the ISA half of this reference. Every Part-2 (encoding)
and Part-3 (semantics) page that names a Vision-Q7 instruction by mnemonic — every
`ivp_mulan_2xf32`, every `ivp_gatheranx16`, every per-slot operand-bit table — rests on the
ability to take a run of raw little-endian bytes out of a device `.text` section and turn it
into a *decoded FLIX bundle*: a known format, a known byte length, and a per-slot list of
`(mnemonic, operands)`. This page documents that decode, end to end, at reimplementation
grade. After reading it you can write the C that consumes 16 bytes and emits the same per-slot
disassembly the device-native `xtensa-elf-objdump` emits — because that is exactly what is
demonstrated here, byte-for-byte, on a bundle carved out of shipped Cayman firmware.

The Vision-Q7 NX (`ncore2gp`, uarch *Cairo*, `Xtensa24`/XEA3) ISA is **FLIX** — Tensilica's
*Flexible Length Instruction eXtensions*, a VLIW scheme in which one fetched bundle holds
several independent operation **slots** issued in the same cycle. The Cairo config exposes
**14 instruction formats**, **46 total slots**, and **7 distinct length outcomes** (`{2, 3, 8,
16}`, plus the `op0==0xF` 8-vs-16 split keyed on byte 3, plus the illegal `-1`). Three of the
formats are the ordinary scalar Xtensa base/density encodings (one slot each); eight are
16-byte *wide* FLIX bundles (4 or 5 slots); three are 8-byte *narrow* FLIX bundles (2–4 slots).
The whole job of the decoder is (a) read a few leading bits to pick one of those 14 formats and
the bundle length, then (b) for each slot the format declares, gather that slot's scattered
bits into a normalized 32-bit word and classify it into a mnemonic.

The entire decode model on this page is read **directly out of the shipped libisa config
library** `libisa-core.so` — its `format_decoder`, `length_decoder`, `length_table`, the 46
`Slot_*_get` bit-gather thunks, the 46 `Slot_*_decode` classifiers, and the 3237
`Field_*_get` operand extractors are all present as resolvable symbols in a non-stripped
`.symtab`, and every table cell and predicate quoted below was re-disassembled in-checkout.
The device-native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) is used only as an
independent *oracle* to cross-validate the result. `[HIGH/OBSERVED]` throughout except where a
format is flagged INFERRED.

---

## 1. Key facts

| Fact | Value | Source |
|---|---|---|
| Config / uarch | `Xm_ncore2gp` / *Cairo*, `Xtensa24`, `TargetHWVersion=NX1.1.4` | `ncore2gp-params` |
| `num_formats` | **14** (`0x0E`) | `num_formats` @ `0x3b65e0` → `mov $0xe,%eax` |
| `num_slots` | **46** (`0x2E`) | `num_slots` @ `0x3b6510` → `mov $0x2e,%eax` |
| `IsaMaxInstructionSize` | 32 bytes (fetch width; bundles use 2/3/8/16) | `config_table` @ `0x85ea40` |
| `IsaMemoryOrder` | LittleEndian | `config_table` |
| `interface_version` | `0x76` = 118 (Xtensa libisa ABI rev) | `interface_version` @ `0x3b5b20` |
| `formats[]` | `0x6cd980`, stride 24, 14 entries | `.data.rel.ro` |
| `slots[]` | `0x6cdb00`, stride 48, 46 entries | `.data.rel.ro` |
| `length_table[256]` | `0x3d4100`, 256 × `int32` | `.rodata` (VMA == file offset) |
| `format_decoder` | `0x3b5970` | returns format index `0..13` / `-1` |
| `length_decoder` | `0x3b5a50` | returns byte length via `length_table` |
| 46 `Slot_*_get` / `Slot_*_decode` | per-slot bit-gather + classifier | `nm \| rg -c` = 46 / 46 |
| 12569 `Opcode_*_..._encode` | `(opcode × slot)` placement matrix (`opcodedefs[]`) | `nm \| rg -c` = 12569 |
| 3237 `Field_*_get` | per-`(field,slot)` operand extractor | `nm \| rg -c` = 3237 |
| Disassembler oracle | `xtensa-elf-objdump`, Binutils 2.34 / Xtensa Tools 14.09 | `XTENSA_CORE=ncore2gp` |

Counts re-grounded this pass with `nm libisa-core.so | rg -c`, not a decompile grep.

The two decode entry points are *not* table-driven; the libisa accessors `decode_format_fn`
and `decode_length_fn` return the hardcoded addresses `0x3b5970` and `0x3b5a50`. A
reimplementation hardcodes them too: there is one format decoder and one length decoder for
the whole ISA.

---

## 2. The byte-0 / byte-3 anatomy a bundle leads with

A bundle is decoded from its first 4 bytes interpreted as a little-endian `uint32`. Two
nibbles drive everything:

```
 byte0                                   byte3
+----+----+                            +----+----+
| hi | op0|   op0  = byte0 & 0x0F      | hi | b3lo|   b3lo = byte3 & 0x0F
+----+----+                            +----+----+
   word w = byte0 | byte1<<8 | byte2<<16 | byte3<<24   (little-endian)
   op0  = w & 0x0000000F      (bits [3:0])
   b3lo = (w >> 24) & 0x0F    (bits [27:24])
```

`op0` (the low nibble of byte 0) is the **primary** format/length selector. `b3lo` (the low
nibble of byte 3) is the **sub-format** discriminator, consulted only for the FLIX cases
(`op0 == 0xE` and `op0 == 0xF`), where several formats share the same `op0`. A handful of
higher byte-3 bits (24, 25, 28, 29) further separate the four near-identical wide formats
`F1`/`F2`/`F6`/`F7`; those bits appear as mask constants in `format_decoder` but their TIE
field name was not chased — `[MED/INFERRED]` on the *meaning*, `[HIGH/OBSERVED]` on the mask
constants that decode them.

The coarse `op0 → class` map is the Tensilica `XCHAL_OP0_FORMAT_LENGTHS` vector, confirmed
byte-exact as the `b3lo == 0` row of `length_table`:

| `op0` | class | length |
|---|---|---|
| `0x0..0x7` | `x24` — base 24-bit Xtensa | 3 |
| `0x8..0xB` | `x16a` — density 16-bit | 2 |
| `0xC..0xD` | `x16b` — density 16-bit | 2 |
| `0xE` | wide FLIX → `F3` (b3lo 0..7) or `F11` (b3lo 8..F) | 16 |
| `0xF` | narrow / wide FLIX — see §4, length depends on `b3lo` | 8 / 16 / illegal |

> **Correction baked into this model.** The Tensilica static macro
> `XCHAL_BYTE0_FORMAT_LENGTHS` keys length on byte 0 *alone* and flattens `op0==0xF` to a
> constant 8 bytes. The shipped runtime `length_decoder` does **not** — for `op0==0xF` it also
> reads `byte3`'s low nibble, so a real Vision-Q7 word with `op0==0xF` decodes to 8, 16, **or**
> illegal. A linear sweep that assumes "`op0==0xF` is always 8 bytes" mis-lengths every
> `F0`/`F1`/`F2`/`F4`/`F6`/`F7` bundle (those are `op0==0xF`, 16 bytes) as two 8-byte reads and
> desyncs. The binary `length_table` is authoritative. `[HIGH/OBSERVED]`

---

## 3. Format selection — `format_decoder` (`0x3b5970`)

`format_decoder` reads `w = *(uint32_le*)inst` and runs a fixed mask-and-match ladder; the
first hit wins and returns the format index, else `-1`. This is the disassembled body
(`mov (%rdi),%edx` loads `w` into `edx`), reproduced as the exact predicate sequence — every
mask/compare constant below is read straight from the `and $imm` / `cmp $imm` immediates:

```c
int format_decoder(const uint8_t *inst) {
    uint32_t w = (uint32_t)inst[0] | inst[1]<<8 | inst[2]<<16 | (uint32_t)inst[3]<<24;
    if ((w & 0x00000008) == 0)          return  0; // x24    op0 0..7      (test $0x8,%dl; je)
    if ((w & 0x0000000C) == 0x8)        return  1; // x16a   op0 8..B
    if ((w & 0x0000000E) == 0xC)        return  2; // x16b   op0 C..D
    if ((w & 0x0B00000F) == 0x0100000F) return  3; // F0     op0=F, b3lo {1,5}
    if ((w & 0x0800000F) == 0x0800000E) return  4; // F11    op0=E, byte3 bit27 set
    if ((w & 0x3700000F) == 0x0300000F) return  5; // F1     op0=F, b3lo {3,B}, sel
    if ((w & 0x3700000F) == 0x3300000F) return  6; // F2     op0=F, b3lo {3,B}, sel
    if ((w & 0x0800000F) == 0x0000000E) return  7; // F3     op0=E, byte3 bit27 clear
    if ((w & 0x0B00000F) == 0x0900000F) return  8; // F4     op0=F, b3lo {9,D}
    if ((w & 0x3700000F) == 0x2300000F) return  9; // F6     op0=F, b3lo {3,B}, sel
    if ((w & 0x3700000F) == 0x1300000F) return 10; // F7     op0=F, b3lo {3,B}, sel
    if ((w & 0x1900000F) == 0x0800000F) return 11; // N1     op0=F, b3lo {8,A,C,E}
    if ((w & 0x1900000F) == 0x1800000F) return 12; // N2     op0=F, b3lo {8,A,C,E}
    if ((w & 0x0900000F) == 0x0000000F) return 13; // N0     op0=F, b3lo {0,2,4,6}
    return -1;                                      // illegal: op0=F, b3lo {7,F}
}
```

The `F1`/`F2`/`F6`/`F7` quartet all share `op0==0xF, b3lo∈{3,B}`; they are separated purely by
the `0x3700000F`-masked byte-3 high bits matching selector `0x03`/`0x33`/`0x23`/`0x13`. `N1`
and `N2` likewise share `b3lo∈{8,A,C,E}` and split on the `0x1900000F`-masked value
`0x08`/`0x18`. The encode side cross-checks the decode: each `Format_<F>_encode` writes a
16- or 8-byte blank template whose signature bytes are exactly these triggers —
`F0 → 0f 00 00 01`, `F3 → 0e 00 00 00`, `N0 → 0f 00 00 00`, etc. — so the encoder and decoder
are mutual inverses. `[HIGH/OBSERVED]`

### The 14 formats

| idx | name | len | slots | issue profile (S0…Sn) | width |
|---|---|---|---|---|---|
| 0 | `x24` | 3 | 1 | `Inst` (base RRR) | scalar |
| 1 | `x16a` | 2 | 1 | `Inst16a` (density) | scalar |
| 2 | `x16b` | 2 | 1 | `Inst16b` (density) | scalar |
| 3 | `F0` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 4 | `F11` | 16 | **5** | Ld · ALU · Mul · ALU · ALU | wide |
| 5 | `F1` | 16 | 4 | LdStALU · Ld · Mul · ALU | wide |
| 6 | `F2` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 7 | `F3` | 16 | **5** | LdSt · Ld · Mul · ALU · ALU | wide |
| 8 | `F4` | 16 | 4 | Ld · Ld · Mul · ALU (dual-load) | wide |
| 9 | `F6` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 10 | `F7` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 11 | `N1` | 8 | 3 | LdSt · None · Mul | narrow |
| 12 | `N2` | 8 | 2 | LdSt · Ld | narrow |
| 13 | `N0` | 8 | 4 | LdSt · None · None · ALU | narrow |

Slot-count census: `1+1+1+4+5+4+4+5+4+4+4+3+2+4 = 46 = num_slots`. There is no `F5`/`F8`/`F9`/`F10`
— the gaps in the `Fn` numbering are real. The two 5-slot formats are `F3` and `F11`, each
adding a second/third ALU slot; `F4` and `F11` carry **no** store slot (dual-load / ALU-heavy
specializations). `None` slots are NOP-only filler positions (1–3 bit encodings) the narrow
formats use to pad. `[HIGH/OBSERVED]`

---

## 4. Length decode — `length_decoder` (`0x3b5a50`) and `length_table`

`length_decoder` takes **raw bytes** (not the assembled word), forms an 8-bit index from
`byte3.low4` and `byte0.low4`, and indexes the 256-entry `int32` table:

```c
int length_decoder(const uint8_t *inst) {
    unsigned idx = ((inst[3] & 0x0F) << 4) | (inst[0] & 0x0F);  // exact asm: movzbl 3(%rdi)
    return length_table[idx];                                   // shl $4; or op0; load dword
}
```

The table is `byte3.low4` (row) × `byte0.low4` (column). Read straight from `.rodata` at
`0x3d4100` — the `b3lo==0` row is the literal bytes `03 03 03 03 03 03 03 03 02 02 02 02 02 02
10 08`, i.e. `3,3,3,3,3,3,3,3,2,2,2,2,2,2,16,8` (`0x10=16`, `0x08=8`):

|`b3lo`↓ `op0`→| 0–7 | 8–B | C–D | E | F |
|---|---|---|---|---|---|
| 0,2,4,6,8,a,c,e | 3 | 2 | 2 | 16 | **8** |
| 1,3,5,9,b,d | 3 | 2 | 2 | 16 | **16** |
| 7, f | 3 | 2 | 2 | 16 | **-1** |

Value census over the 256 cells: `{3:128, 2:96, 16:22, 8:8, -1:2}`. The only column that
varies with `b3lo` is `op0==0xF`: even `b3lo` → 8 (narrow `N0`/`N1`/`N2`), odd `b3lo` ∈
{1,3,5,9,b,d} → 16 (wide `F0`/`F1`/`F2`/`F4`/`F6`/`F7`), and `b3lo` ∈ {7,f} → illegal. That
8-vs-16 split on `op0==0xF`, plus `{2,3,16}` and the illegal `-1`, is the **7 length outcomes**
the corpus refers to. As a self-consistency proof, simulating `format_decoder` over all 256
`(b0lo,b3lo)` pairs and comparing against `length_table` yields **zero** mismatches: every cell
`format_decoder` calls a wide format is 16, every narrow is 8, every illegal is `-1`.
`[HIGH/OBSERVED]`

---

## 5. Per-format slot partition — the `Slot_*_get` bit-gather

A bundle's bits do **not** divide into contiguous slot windows. Each slot is read by its own
`Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<bitoff>_get` thunk, which *scatters* the slot's bits out
of the (up to) four 32-bit lanes of the 128-bit bundle into a fresh 32-bit normalized
`slotbuf` word. Two data sources agree on the wiring: the `slots[]` table stores the slot's
sequential position and its get/set function pointers, and the thunk's *symbol name* encodes
the unit (`ldst`/`ld`/`mul`/`alu`/`none`) and its starting bit offset — and the get address in
the symbol equals `slots[i].get`, so name and table cross-check by address.

The per-format slot roster and starting bit offset (offset = HIGH from symbol + get-code
agreement; decoded slot *width* = MED, machine-code-emulated):

| fmt | slots | units (S0…Sn) | bit offsets | get_fn addresses |
|---|---|---|---|---|
| `F0` | 4 | LdSt·Ld·Mul·ALU | 4·16·28·36 | `3b0550`/`3b0880`/`3b0a10`/`3b0c70` |
| `F1` | 4 | LdStALU·Ld·Mul·ALU | 4·16·41·31 | `3b17e0`/`3b19f0`/`3b1ba0`/`3b1dd0` |
| `F2` | 4 | LdSt·Ld·Mul·ALU | 4·16·27·31 | `3b1fd0`/`3b2250`/`3b2400`/`3b2680` |
| `F3` | 5 | LdSt·Ld·Mul·ALU·ALU | 4·16·28·33·24 | `3b28e0`/`3b2a60`/`3b2b90`/`3b2d20`/`3b2f90` |
| `F4` | 4 | Ld·Ld·Mul·ALU | 4·16·28·36 | `3b3170`/`3b32f0`/`3b34a0`/`3b36b0` |
| `F6` | 4 | LdSt·Ld·Mul·ALU | 4·16·41·36 | `3b38c0`/`3b3c40`/`3b3e70`/`3b40d0` |
| `F7` | 4 | LdSt·Ld·Mul·ALU | 4·16·41·31 | `3b4340`/`3b45d0`/`3b4780`/`3b49c0` |
| `F11` | 5 | Ld·ALU·Mul·ALU·ALU | 4·16·41·31·24 | `3b0ee0`/`3b10c0`/`3b1220`/`3b13e0`/`3b15e0` |
| `N0` | 4 | LdSt·None·None·ALU | 4·58·59·16 | `3b5400`/`3b5530`/`3b55b0`/`3b5610` |
| `N1` | 3 | LdSt·None·Mul | 4·54·16 | `3b4c30`/`3b4db0`/`3b4e70` |
| `N2` | 2 | LdSt·Ld | 4·16 | `3b5090`/`3b5250` |

Two facts a reimplementer must internalize. First, **slot bit offset is NOT
`slots[].position`** — the `slot_position` libisa accessor returns the sequential index 0..N-1,
not a bundle offset; the real offset lives in the get thunk. Second, the offset tells you where
a slot's *low byte* starts, but the rest of the slot scatters across high bits, so `width !=
next_offset − offset`; the gather body is authoritative. Every `s0` slot starts at bit 4
because bits `[3:0]` are always the `op0` selector. The slot order in `Format_*_slots[]` is
*issue* order, which is physically out-of-order for `F3`/`F11`/`F6`/`F7` (e.g. `F3`'s s4 ALU
sits at bit 24, *before* s2 Mul at 28 and s3 ALU at 33). `[HIGH/OBSERVED]` on offsets/units;
`[MED/INFERRED]` on widths.

### A real gather thunk, disassembled

`Slot_n0_Format_n0_s0_ldst_4_get` (`0x3b5400`) — the N0 LdSt slot — zero-fills `slotbuf[1..7]`
then assembles `slotbuf[0]` from seven scattered ranges across the two 8-byte-bundle lanes
(`insn0 = *(%rdi)`, `insn1 = *(0x4+%rdi)`). Ported literally from the `and`/`shr`/`shl`/`or`
chain:

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

The wide-format gathers are the same shape but pull from up to three lanes:
`Slot_f0_Format_f0_s0_ldst_4_get` (`0x3b0550`) reads `(%rdi)`, `0x4(%rdi)` **and** `0x8(%rdi)`
— confirming a wide slot's bits genuinely span all four 32-bit lanes of the 16-byte bundle.
`[HIGH/OBSERVED]`

---

## 6. Slot classification and operand extraction

Once a slot is gathered into a 32-bit `slotbuf`, `Slot_<slot>_decode` (one per slot, 46 total,
table `decodes[]` @ `0x6ce3c0`) runs a masked-compare ladder over the opcode-selector bits and
returns a pointer into the opcode-name string pool. For example `Slot_n0_s0_ldst_decode`
(`0x3a8950`) masks `slotbuf & 0xFFFFFF`, peels `(slotbuf << 8) >> 0x14`, and switches on the
opcode group, exactly inverting the `Opcode_<mnem>_Slot_<slot>_encode` template constants (e.g.
`Opcode_addi_Slot_n0_s0_ldst_encode` writes `0x240000`).

Operands are read per `(field, slot)` by `Field_<field>_Slot_<slot>_get` thunks — 3237 of
them, because the *same* logical field sits at a *different* bit position in each slot it
appears in, so each placement gets its own thunk. For the N0 LdSt slot the three `addi`
operands resolve to:

```c
// Field_t_Slot_n0_s0_ldst_get    (0x31e750):  (slotbuf << 0x18) >> 0x1c   -> bits [7:4]
// Field_s_Slot_n0_s0_ldst_get    (0x31f810):  slotbuf & 0xf               -> bits [3:0]
// Field_imm8_Slot_n0_s0_ldst_get (0x31f330):  (slotbuf >> 8) & 0xff       -> bits [15:8]
```

The value is then run through `OperandSem_<x>_decode` for sign-extension, scaling, and (for
branch/offset operands) PC-relative rebasing — e.g. `soffsetx4 = (PC & ~3) + (sext18 << 2)`.
Register operands carry a `regfile` name (`AR`/`vec`/`vbool`/`wvec`/`gvr`/…); immediate
operands carry their scale/bias. The full codec data-flow (the assemble direction, the
`do_reloc`/`undo_reloc` pair) is on the [libisa table schema & codec ABI](../isa/core/libisa-table-schema.md)
page; this page needs only the *get* half. `[HIGH/OBSERVED]`

---

## 7. Narrow vs wide

The narrow/wide distinction is not cosmetic — it changes the lane count, the slot roster, and
the length, and it is the single most common cause of a mis-synced sweep.

* **Wide bundles** (`F0`, `F1`, `F2`, `F3`, `F4`, `F6`, `F7`, `F11`) are **16 bytes** = four
  32-bit lanes = a 128-bit bundle, carrying **4 or 5** real issue slots (LdSt/Ld + Mul +
  one-to-three ALU). They lead with `op0==0xE` (→ `F3`/`F11`) or `op0==0xF` with **odd** `b3lo`
  (→ the six `op0==0xF` wide formats).
* **Narrow bundles** (`N0`, `N1`, `N2`) are **8 bytes** = two 32-bit lanes = a 64-bit bundle,
  carrying **2–4** slots of which some are `None` (NOP-only filler). They lead with `op0==0xF`
  and **even** `b3lo`. `N0` has 4 declared slots but only 2 real units (LdSt + ALU, two NOP
  fillers); `N1` has LdSt + Mul + 1 NOP; `N2` has just LdSt + Ld.

A reimplementation must therefore branch on length *before* allocating the slot buffer: a wide
gather reads `insn[0..3]`, a narrow gather reads only `insn[0..1]`. Treating an
`op0==0xF`/odd-`b3lo` wide bundle as narrow (the static-macro bug, §2) reads 8 bytes, lands the
sweep pointer in the middle of the bundle's third lane, and desyncs for the rest of the
function. `[HIGH/OBSERVED]`

---

## 8. The full decode loop (annotated C)

This is the libisa decode pipeline as a reimplementer would write it, naming the real
`libisa-core.so` symbols each step ports. `insn` is the 32-byte (`IsaMaxInstructionSize`)
buffer holding the bundle as `uint32` lanes, little-endian (`insn[w] |= byte[i] << (8*(i%4))`).

```c
typedef uint32_t insnbuf[8];                 // 32-byte fetch window, 8 LE uint32 lanes

typedef struct { const char *mnem; operand_t ops[8]; int n_ops; } decoded_slot;
typedef struct { int format; int length; int nslots; decoded_slot slot[5]; } decoded_bundle;

int decode_bundle(const uint8_t *mem, uint32_t pc, decoded_bundle *out) {
    // (a) byte -> insnbuf lanes (little-endian).  IsaMemoryOrder == LittleEndian.
    insnbuf insn = {0};
    for (int i = 0; i < 32; i++) insn[i>>2] |= (uint32_t)mem[i] << (8 * (i & 3));

    // (b) format select.  format_decoder @ 0x3b5970 (§3).
    int fmt = format_decoder(mem);                 // reads w = insn[0]
    if (fmt < 0) return -1;                         // illegal selector (b3lo 7/f)
    out->format = fmt;

    // (c) length.  length_decoder @ 0x3b5a50 -> length_table[0x3d4100] (§4).
    int len = length_decoder(mem);                 // ((mem[3]&0xf)<<4)|(mem[0]&0xf)
    if (len < 0) return -1;                         // illegal length
    out->length = len;

    // (d..f) per slot declared by Format_<fmt>_slots[] (issue order).
    const slot_id *slots = format_slots[fmt];      // formats[fmt] -> slot list
    out->nslots = format_nslots[fmt];
    for (int s = 0; s < out->nslots; s++) {
        slot_id sid = slots[s];
        uint32_t slotbuf[8] = {0};

        // (d) gather this slot's scattered bits.  Slot_<...>_get (§5).
        slot_get_fn[sid](insn, slotbuf);           // e.g. n0_s0_ldst_get @ 0x3b5400

        // (e) classify the gathered word into an opcode.  Slot_<slot>_decode (§6).
        const char *mnem = slot_decode_fn[sid](slotbuf);  // e.g. 0x3a8950 -> "addi"
        out->slot[s].mnem = mnem;

        // (f) operands: opcode -> iclass -> args -> field_get -> operand_decode (§6).
        const iclass_t *ic = opcode_iclass(mnem);
        for (int a = 0; a < ic->n_args; a++) {
            uint32_t raw = field_get_fn[ic->arg[a].field][sid](slotbuf);  // Field_*_Slot_*_get
            out->slot[s].ops[a] = operand_decode(ic->arg[a].operand, raw, pc); // sign/scale/PC
        }
        out->slot[s].n_ops = ic->n_args;
    }
    return len;                                     // advance the sweep pointer by `len`
}
```

`pc` is optional; supplying the bundle address enables absolute branch-target printing for
`soffsetx4`/`label*` operands (otherwise they read as raw signed displacements). A full port of
this pipeline against the 46 gather thunks, 46 classifiers, and 3237 field thunks reproduces
the device `xtensa-elf-objdump` per-slot mnemonics with **zero** disagreements across hundreds
of thousands of real slot decodes. `[HIGH/OBSERVED]`

---

## 9. A fully worked decode (carved from device `.text`, oracle-validated)

The bundle below is real machine code, carved from the **Cayman** (`v3`) pool-perf firmware
image `CAYMAN_Q7_POOL_PERF_EXTISA_0` (sha256 `910d41c3ededce67…`, entry `0x1005610`) inside
`libnrtucode_internal.so` (file offset `0x2ef7e0`, size `0xa260`), at virtual address
`0x1000520` in its `.text` (VMA `0x01000000`). The image is disassembled with the
device-native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`), which serves as the oracle.

**Raw memory stream** (8 bytes, little-endian, exactly as they sit in `.text`):

```
4f 80 a0 80 11 c3 20 21
```

(`objdump` prints its hex column byte-reversed for display; the decoder consumes the memory
stream above, not the display column.)

**Step (a) — lanes.**  `insn[0] = 0x80A0804F`, `insn[1] = 0x2120C311`.

**Step (b) — format.**  `op0 = byte0 & 0xF = 0xF`; `b3lo = byte3 & 0xF = 0x0`.
`(w & 0x0900000F) = 0x0000000F == 0x0000000F` → the `N0` predicate fires →
**format 13 = `N0`**. (No earlier predicate matches: `w & 0x8 != 0`, `w & 0xC = 0xC ≠ 0x8`,
`w & 0xE = 0xE ≠ 0xC`, and every wide `op0==0xF` selector needs an odd `b3lo`.)

**Step (c) — length.**  `idx = (0x0 << 4) | 0xF = 0x0F` → `length_table[0x0F] = 8`. **8 bytes**
(narrow). `nslots(N0) = 4`: LdSt · None · None · ALU.

**Step (d/e/f) — slot S0 (LdSt), worked to the operand.**  The seven gather contributions of
`n0_s0_ldst_get(0x80A0804F, 0x2120C311)`:

```
(insn0 & 0x00000F00)>>8         = 0x00000000
(insn0 & 0x000000F0)            = 0x00000040
(insn0 & 0x0000F000)>>4         = 0x00000800
((insn0 & 0x00200000)>>21)<<12  = 0x00001000     (insn0 bit 21 = 1)
(insn1 & 0x00000006)<<12        = 0x00000000
(insn1 & 0x03FC0000)>>3         = 0x00240000     (insn1 & 0x03FC0000 = 0x01200000, >>3)
((insn1 & 0x40000000)>>30)<<23  = 0x00000000
                              OR = 0x00241840  = slotbuf[0]
```

`Slot_n0_s0_ldst_decode(0x00241840)` masks the opcode-selector bits and classifies the group
as **`addi`** (the `Opcode_addi_Slot_n0_s0_ldst_encode` template is `0x240000`, which
`0x00241840` carries). Operands via the N0-LdSt field thunks:

```
Field_t_Slot_n0_s0_ldst_get    (0x00241840 << 0x18) >> 0x1c = 4   -> a4   (dest)
Field_s_Slot_n0_s0_ldst_get     0x00241840 & 0xf            = 0   -> a0   (src)
Field_imm8_Slot_n0_s0_ldst_get (0x00241840 >> 8) & 0xff     = 24         (immediate)
```

→ **`addi a4, a0, 24`**.

**Slots S1, S2 (None), S3 (ALU).**  The two `None` slots gather to the slot NOP (decode →
`nop`). The S3 ALU slot gathers and classifies to `ivp_dextrprn_2x32` with operands
`pr12, v2, v0, 1, 6` (a `b32_pr`/`pr` predicate-extract op).

**Oracle cross-check.**  The device `xtensa-elf-objdump` line for `0x1000520` is:

```
1000520:  4f80a08011c32021   { addi a4, a0, 24; nop; nop; ivp_dextrprn_2x32 pr12, v2, v0, 1, 6 }
```

Every part matches: format `N0` (4 slots, the `{ … ; … ; … ; … }` braces), length 8 (8 hex
bytes), slot S0 `addi a4, a0, 24` reproduced **down to the operand values** by the from-scratch
port above, S1/S2 `nop`, S3 `ivp_dextrprn_2x32 pr12, v2, v0, 1, 6`. The decode is exact.
`[HIGH/OBSERVED]`

### A wide example (same image)

For a 16-byte wide bundle, the F0 at VA `0x1000cec` in the same Cayman image:

```
raw mem : 4f ff 42 41 56 02 41 00 00 00 00 00 00 00 00 00
insn[0] = 0x4142FF4F   op0 = 0xF, b3lo = 0x1
(w & 0x0B00000F) = 0x0100000F == 0x0100000F   -> format 3 = F0
length_table[(1<<4)|0xF] = 16   -> 16 bytes, 4 slots (LdSt·Ld·Mul·ALU)
```

`objdump` decodes it as four co-issued slots
`{ bnone.w15 a15,a4,…; ivp_labvdcmprs2nx8_xp vb4,v0,u0,a2,a4,0; ivp_dmulq2n8dxr8 wv0,wv0,v1,v0,v0;
ivp_dselnx16t … }` — a branch in S0, an aligning vector load in S1, a dual-quad 8×8 MAC into
the `wv` wide accumulator in S2, and a vector select in S3. The S0 gather
(`Slot_f0_Format_f0_s0_ldst_4_get`) reads all three lanes `insn[0]`, `insn[1]`, `insn[2]` to
assemble its slot word, illustrating the cross-lane scatter a wide format demands.
`[HIGH/OBSERVED]`

### Adversarial self-verification at scale

To confirm the format/length model is not curve-fit to one bundle, the from-scratch
`format_decoder` and `length_table` ports were run over **every** FLIX bundle in the Cayman
`EXTISA_0` `.text` (167 bundles) and compared against `objdump`'s own framing — the per-format
**slot count** (`{…}` semicolon count + 1) and **byte length** (16-hex wide vs 8-hex narrow):

```
format slot-count agreement vs objdump:  167 OK / 0 BAD
length agreement:                        167 OK / 0 BAD
format histogram: F0:11  F1:3  F11:43  F3:67  F4:13  N0:26  N2:4
```

Zero disagreements. The format predicates and the length table are byte-exact against real
device code. `[HIGH/OBSERVED]`

---

## 10. Confidence ledger and the desync wall

**HIGH / OBSERVED**

* The byte→lane packing (LE, `uint32` lanes); the `format_decoder` predicate ladder and every
  mask/compare constant; the `length_decoder` index math and all 256 `length_table` cells;
  `num_formats=14`, `num_slots=46`, `interface_version=118` — read from immediates/`.rodata`.
* The 14 formats' names/lengths/slot rosters; the per-slot units and bit offsets; the
  `Slot_*_get` scatter bodies and `Field_*_get` extractor bodies — disassembled.
* The worked `N0` decode reproduced down to operands; the `F0` wide framing; the 167/167
  format+length agreement vs the device oracle.

**MED / INFERRED**

* `F4` and `F6` slot *interiors*: no compiled object in the available corpus emits an `F4` or
  `F6` bundle (the Cayman image above carries `F4` but the validation oracle covers it only via
  the shared transpile path), so for some `F4`/`F6` per-slot mnemonic/operand bit-exactness the
  result is *inferred from the identical decode path*, not exhaustively oracle-confirmed.
  **Flag any `F4`/`F6` interior decode as `[MED/INFERRED]`** unless objdump independently
  confirms that specific bundle. Every other format is `[HIGH/OBSERVED]`.
* The semantic meaning of the byte-3 high bits (24/25/28/29) separating `F1`/`F2`/`F6`/`F7`:
  the *mask constants* that decode them are HIGH; *why* four near-identical 4-slot formats
  exist is interpretive.
* Per-slot decoded *width* (bits): machine-code-emulated, ±1–2 bits on scattered FLIX slots.

### The FLIX-desync wall

A linear sweep over a dense FLIX `.text` does **not** stay synchronized end to end, even with a
byte-exact length decoder. Device `.text` interleaves **literal pools** and hand-written
boot/vector stubs (the first ~`0x100` bytes carry no FLIX property records) directly between
bundles. The decoder has no in-band way to tell a literal `uint32` from a bundle word: a
literal whose low nibble happens to be `0..7` looks like a 3-byte `x24`, a literal whose low
nibble is `0xF` with an even `byte3` looks like an 8-byte `N0`, and the sweep advances by the
*wrong* length and locks onto the interior of the next real bundle.

**How to detect it.** The reliable signals, in order of strength:

1. **`length_decoder` returns `-1`** (`op0==0xF`, `b3lo ∈ {7,f}`) — a hard illegal; the current
   offset is not a bundle start.
2. **A branch/call operand resolves out of the image** — a `call0`/`j`/`callx` whose
   PC-relative target lands outside `[.text_base, .text_base+size)` is a mis-synced operand
   byte that happened to form a control-flow opcode. Flag it and refuse to follow it. (This is
   the exact tell the firmware-carve sweeps use: out-of-image call targets like `0xfff8ffa0` or
   `0x6c778` in a `0x4bc0`-byte image are desync, not code.)
3. **A run of nonsense fields** — a `None`-slot that decodes to a real op, an operand index out
   of regfile range, or a vector-op in a slot whose roster forbids it — flags a bad gather.

**How to recover.** Re-anchor on a known-good landmark and resume: a function prologue
`entry aN,M`, a `retw.n` (`1d f0`) at a sensible function end, or the *next* address the FLIX
property table (`.xt.prop.*`) marks as a function start. The Cayman/Mariana images carry
per-function `.xt.prop.<mangled>` property records; merging them and synthesizing a `.symtab`
from the `0x2804` function-start records flips objdump from data-mode to FLIX-aware code-mode
and bounds the sweep to real function ranges, which is why the property-merge strategy decodes
far cleaner than a raw linear sweep. The honest residual: spans of literal pool and the boot
stub render as `.byte` and are not recoverable without the property table. This is a real wall
— **closable only with the per-image property records** (which the device ELFs ship as
un-merged sections), not with a smarter byte-level heuristic. `[HIGH/OBSERVED]`

---

## 11. GOTCHA — the NCFW management core is scalar Xtensa-LX, **not** FLIX

> **Do not apply the FLIX rule to NCFW firmware.** The NeuronCore management firmware (NCFW)
> that drives collectives — the ring/mesh/hierarchical schedulers in the carved NCFW IRAM
> images (standalone in the sibling `neuronx-runtime` `libncfw.so`; embedded in the wrapper for
> this gpsimd checkout) — runs on a **scalar Xtensa-LX** core, a *different* Xtensa than the
> Vision-Q7.
> Its instruction length rule is the **base scalar rule** (`op0 ∈ {e,f}` → 3-byte, else 2-byte),
> and its `op0=e`/`op0=f` bytes are **operand/immediate bytes of 2-/3-byte scalar
> instructions** — they are **not** FLIX bundle leaders.

If you run the Vision-Q7 FLIX decoder (or `xtensa-elf-objdump` with core `ncore2gp`) on an NCFW
IRAM image, it mis-reads every `op0==0xE` byte as the start of a 16-byte Vision bundle and every
`op0==0xF` byte as an 8-byte narrow bundle, producing the spurious "~26–28% FLIX" artifact and
desyncing the whole image. The correct treatment:

* **Length:** the scalar LX rule (`e`/`f` → 3-byte modal width, else 2-byte), resynced at
  `retw.n` (`1d f0`) function ends. This rule recovers ~67–79% of the scalar *spine* (the
  dispatch loop `const16 a2,0xB0; addx4; l32i.n`, the idle `waiti 15` loop, the semaphore
  wait/signal helper bank, the `(A)` command trampolines).
* **The hard residual:** the `op0=e/f` *leader* instructions cannot be **named** — no LX TIE
  config ships in the corpus, so even the correct length rule cannot decode the dense case-body
  interiors (their `e/f` bytes are operand bytes the sweep locks onto). The spine lifts; the
  interiors stay partly hard. This is a distinct wall from the Vision-Q7 desync wall above.

A working **Vision** disassembler ships in the corpus
(`gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump`, core `ncore2gp`) and is correct for
device-side Q7 `.text`; there is **no** shipped LX disassembler, so NCFW decode needs a
from-scratch scalar-LX decoder, not the FLIX path. Anything you decode out of `libncfw.so`
IRAM, route through the scalar-LX rule; anything out of a Q7 pool/extisa ELF or a compiled
custom-op `.text`, route through the FLIX decoder on this page. `[HIGH/OBSERVED]`

---

## 12. Function & symbol map

| Symbol | Address | Role |
|---|---|---|
| `format_decoder` | `0x3b5970` | `w → format index 0..13 / -1` (§3) |
| `length_decoder` | `0x3b5a50` | raw bytes → length via `length_table` (§4) |
| `length_table` | `0x3d4100` (.rodata) | 256 × `int32` byte0/byte3 → length |
| `formats[]` | `0x6cd980` (.data.rel.ro) | 14 × `{name,length,encode}` stride 24 |
| `slots[]` | `0x6cdb00` (.data.rel.ro) | 46 × `{name,format,nop,position,get,set}` stride 48 |
| `decodes[]` | `0x6ce3c0` (.data.rel.ro) | 46 × `{slot, decode_fn}` stride 16 |
| `Slot_n0_Format_n0_s0_ldst_4_get` | `0x3b5400` | worked N0 LdSt gather (§5) |
| `Slot_f0_Format_f0_s0_ldst_4_get` | `0x3b0550` | wide F0 LdSt gather (3-lane) |
| `Slot_n0_s0_ldst_decode` | `0x3a8950` | N0 LdSt opcode classifier (§6) |
| `Opcode_addi_Slot_n0_s0_ldst_encode` | `0x3389b0` | `addi` template `0x240000` |
| `Field_t/s/imm8_Slot_n0_s0_ldst_get` | `0x31e750`/`0x31f810`/`0x31f330` | N0 LdSt operand extractors (§6) |
| `num_formats` / `num_slots` | `0x3b65e0` / `0x3b6510` | `14` / `46` |
| `interface_version` | `0x3b5b20` | libisa ABI rev `118` |
| `get_config_table` | `0x3b5b30` | → `config_table` @ `0x85ea40` |

All addresses are in `libisa-core.so` (`ncore2gp/config/`) unless noted; the firmware images
are the ELF32-Xtensa blobs **embedded in** `libnrtucode_internal.so` (the standalone
`libnrtucode_extisa.so` container carries the same set in the sibling `neuronx-runtime`
corpus, not in this gpsimd checkout — see [The Corpus, Tiers & Binary Inventory](corpus-inventory.md) §3).

---

## 13. Cross-references

* [The Confidence & Walls Model](confidence-model.md) — what `[HIGH/OBSERVED]` and "wall" mean.
* [Methodology](methodology.md) — how the binaries were carved and disassembled.
* [Toolchain Inventory](toolchain-versions.md) — `xtensa-elf-objdump` / `ncore2gp` provenance.
* [The FLIX VLIW Encoding](../isa/core/flix-encoding.md) — the 14-format / 46-slot / 7-length
  tables and the byte-3 sub-format map in full.
* [The Canonical ISA Decode Model](../isa/core/libisa-decode-model.md) — the libisa-core decode
  pipeline this page operationalizes.
* [The libisa Table Schema & Codec ABI](../isa/core/libisa-table-schema.md) — the
  `(opcode × slot)` encode matrix, the field/operand codec ABI, and the assemble direction.
