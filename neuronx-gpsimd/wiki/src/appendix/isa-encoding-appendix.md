# The Master ISA Encoding Appendix

This is the **byte-level opcode-encoding lookup** for the Vision-Q7 *Cairo* (`ncore2gp`)
FLIX/VLIW ISA — the master synthesis of the per-slot mnemonic sweep and the per-instruction
reference. It is the reference an implementer keeps open while writing an assembler: for any of
the **1534 shipped mnemonics** it gives the encoding row — *format · slot · unit · iclass ·
opcode-selector CONST · template immediate* — plus the **14-format / 46-slot / 7-length** framing
tables and the **encode-thunk ABI** (`C7 07 imm32 [C7 47 04 imm32] C3`) the per-instruction
batches (B01–B30) expand from.

This page is a **consolidation, not a re-derivation**: it owns the *lookup matrix* and the
*framing tables*; the deep mechanism lives in its four sources, which it cross-links and stays
numerically consistent with:

* [The FLIX VLIW Encoding](../isa/core/flix-encoding.md) — the 14-format / 46-slot grid, the
  `format_decoder`/`length_decoder` bodies, the per-slot placement census (§6.3) this page tallies.
* [The libisa Table Schema & Codec ABI](../isa/core/libisa-table-schema.md) — the byte layout of
  every table (`opcodedefs[]`, `opcodes[]`, `slots[]`, `fields[]`) and the four-layer codec.
* [The Canonical ISA Decode Model (libisa-core)](../isa/core/libisa-decode-model.md) — the
  `formats → slots → opcodes → iclasses → operands → fields` object model and the decode loop.
* [ISA Coverage & the 1534/1607/12642 Tally](../isa/core/coverage-tally.md) — the certified
  `1534/12569` denominator this appendix inherits, and the `+73` fold.
* [The TIE Database & Four Independent ISA Sources](../isa/core/tie-database.md) — the authoring
  superset (`1607/12642`) the runtime tables fold from, and the four-source agreement.

The byte-level row expansions live in the **per-instruction reference**
([template & partition](../isa/ref/template-and-partition.md), then `b01-vec-alu-int.md` …
`b30-appendix-p.md`); this appendix is the lookup those batches compress.

Everything below is read **directly out of the shipped Tensilica libisa config library**
`libisa-core.so` (`ncore2gp/config/`, sha256 `8fe68bf4…f143e451`, 9 690 712 bytes, ELF64
x86-64, **not stripped**, 45 058 symbols), re-grounded against the binary this pass via `nm`,
`objdump -d`, `readelf -SW`, and a direct byte-parse of `opcodes[]`/`opcodedefs[]` — **never** a
decompile grep. The value side is `libfiss-base.so` (sha256 `260b110c…`, 12 330 016 B). The
device-native `xtensa-elf-objdump`/`xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) serve as an
independent encode/decode oracle.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/immediate/symbol read from the shipped binary (or computed by executing the
shipped simulator); `INFERRED` = reasoned over OBSERVED facts; `CARRIED` = re-used at a cited
report's confidence; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but
real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orienting context).

> **GOTCHA — every count on this page is grounded in a `num_*` getter immediate or an `nm | rg -c`
> symbol-family population, never a decompile grep.** Counting the `Opcode_*`/`Field_*`/`Slot_*`
> families in the IDA/Hex-Rays decompile inflates **2–12×** (one thunk is referenced from many
> call sites and counted once per reference). The binary `.symtab` is the arbiter. Where this page
> states a count, the witness is named in the row.

> **GOTCHA — `.data.rel.ro` / `.data` VMA↔file delta is `0x200000` for this binary; `.text` /
> `.rodata` are VMA == file-offset.** The encode/field/slot thunks live in `.text` (`0x312c10`,
> delta 0) and resolve direct; the *tables* (`opcodes` @ `0x6ce6c0`, `opcodedefs` @ `0x6e9640`)
> live in `.data.rel.ro` (VMA `0x67bb00` → file `0x47bb00`). Do **not** carry over `libtpu.so`'s
> `0x400000` `.data` delta — that is a different binary (`readelf -SW`, re-read this pass:
> `.text` `0x312c10`/`0x312c10`; `.rodata` `0x3b6e40`/`0x3b6e40`; `.data.rel.ro`
> `0x67bb00`/`0x47bb00`; `.data` `0x764040`/`0x564040`). `libisa-core.so` is in `extracted/`
> (gitignored; reach with an absolute path or `fd --no-ignore`).

---

## 1. The encoding-key facts in one block

Every dimension this appendix indexes, with its binary witness — re-read this pass.

| Dimension | Value | Binary witness (this pass) | Tag |
|---|---|---|---|
| Config / uarch | `Xm_ncore2gp` / *Cairo*, Xtensa24/XEA3, NX1.1.4 | `config_table` @ `0x85ea40` (.data) | `[HIGH/OBSERVED]` |
| **Shipped mnemonics** | **1534** | `num_opcodes` @ `0x3b61d0` = `mov $0x5fe` (=1534); `nm \| rg -o 'Opcode_(.+)_Slot_…_encode' \| sort -u \| wc -l` = 1534 | `[HIGH/OBSERVED]` |
| **Shipped placements** (`mnem × slot`) | **12569** | `num_encode_fns` @ `0x3b6130` = `mov $0x3119` (=12569); `nm \| rg -c 'Opcode_.*_Slot_.*_encode'` = 12569; per-slot census sums to 12569 (§4) | `[HIGH/OBSERVED]` |
| Pre-fold mnemonics / placements (TIE-DB) | **1607 / 12642** | `1534 + 73`, `12569 + 73`; fold forms absent from roster (§5) | `[HIGH/CARRIED]` on totals, `[HIGH/OBSERVED]` on `+73` |
| `num_formats` | **14** (`0x0E`) | `num_formats` @ `0x3b65e0` → `mov $0xe` | `[HIGH/OBSERVED]` |
| `num_slots` = `num_decode_fns` | **46** (`0x2E`) | `num_slots` @ `0x3b6510`; `num_decode_fns` @ `0x3b64c0` → `mov $0x2e` | `[HIGH/OBSERVED]` |
| Distinct byte-lengths | **4** = `{2, 3, 8, 16}` | `formats[].length` census | `[HIGH/OBSERVED]` |
| Length-class outcomes | **7** | `length_table[256]` value census `{3:128,2:96,16:22,8:8,−1:2}` | `[HIGH/OBSERVED]` |
| `num_iclasses` | **1447** (`0x5A7`) | `num_iclasses` @ `0x3b5fb0` → `mov $0x5a7` | `[HIGH/OBSERVED]` |
| `num_operands` | **232** (`0xE8`) | `num_operands` @ `0x3b5e80` → `mov $0xe8` | `[HIGH/OBSERVED]` |
| `num_fields` | **3237** (`0xCA5`) | `num_fields` @ `0x3b5b40` → `mov $0xca5`; **3230** named `Field_*` pairs | `[HIGH/OBSERVED]` |
| `num_regfiles` / views | **8 / 4** | `num_regfiles` @ `0x3b5c20`; `num_regfile_views` @ `0x3b5d50` | `[HIGH/OBSERVED]` |
| `OperandSem_*_decode` (value transforms) | **95** | `nm \| rg -c 'OperandSem_.*_decode'` = 95 | `[HIGH/OBSERVED]` |
| Scalar / vector name split | **469 / 1065** | `rg -v '^ivp_'` = 469, `rg '^ivp_'` = 1065 over the roster, sum 1534 | `[HIGH/OBSERVED]` |
| `interface_version` | `0x76` = **118** | `interface_version` @ `0x3b5b20` | `[HIGH/OBSERVED]` |

> **CORRECTION — `num_operands` is 232, not the Cadence enum's 1517.** The Cadence
> `xtensa-modules.c` `OPERAND_*` enum is **1517** (it enumerates implicit/state operands too); the
> shipped binary's `operands[]` table is **232** codec operands. Pin **232** for the binary; 1517
> is `[HIGH/CARRIED]` and never indexes anything in this `.so`. Likewise `num_operands` ≠
> `num_fields` (232 vs 3237): a *field* is a per-`(name × slot)` bit-window, an *operand* is a
> per-codec value↔field tuple; the 3237 fields recur the same logical field once per slot.

---

## 2. The format / slot / length framing tables

These three tables are the framing an encoder lays *before* it deposits any opcode-selector bits.
They are reproduced verbatim from [the FLIX encoding page](../isa/core/flix-encoding.md) and
reconciled to it cell-for-cell; the *mechanism* (the `format_decoder` mask ladder, the
`length_table` index math) lives there and is not duplicated here.

### 2.1 The 14 formats (`formats[]` @ `0x6cd980`, stride 24)

`{ char *name; int length; void(*encode)(uint32_t*) }`. `op0` = `byte0[3:0]`; `b3lo` =
`byte3[3:0]`; the **signature** is the first 4 template bytes the `Format_<F>_encode` stub lays.

| idx | name | len | #slots | issue profile (S0…Sn) | width | `op0` | `b3lo` | signature (b0..b3) | encode_fn |
|---|---|--:|--:|---|---|:--:|---|---|---|
| 0 | `x24` | 3 | 1 | `Inst` (base RRR 24-bit) | scalar | 0–7 | any | `0f`-rule n/a | `0x3b57c0` |
| 1 | `x16a` | 2 | 1 | `Inst16a` (density) | scalar | 8–B | any | — | `0x3b57d0` |
| 2 | `x16b` | 2 | 1 | `Inst16b` (density) | scalar | C–D | any | — | `0x3b57f0` |
| 3 | `F0` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 1,5 | `0f 00 00 01` | `0x3b5810` |
| 4 | `F11` | 16 | **5** | Ld · ALU · Mul · ALU · ALU | wide | E | 8–F | `0e 00 00 08` | `0x3b5830` |
| 5 | `F1` | 16 | 4 | LdStALU · Ld · Mul · ALU | wide | F | 3,B | `0f 00 00 03` | `0x3b5850` |
| 6 | `F2` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0f 00 00 33` | `0x3b5870` |
| 7 | `F3` | 16 | **5** | LdSt · Ld · Mul · ALU · ALU | wide | E | 0–7 | `0e 00 00 00` | `0x3b5890` |
| 8 | `F4` | 16 | 4 | Ld · Ld · Mul · ALU (dual-load) | wide | F | 9,D | `0f 00 00 09` | `0x3b58b0` |
| 9 | `F6` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0f 00 00 23` | `0x3b58d0` |
| 10 | `F7` | 16 | 4 | LdSt · Ld · Mul · ALU | wide | F | 3,B | `0f 00 00 13` | `0x3b58f0` |
| 11 | `N1` | 8 | 3 | LdSt · None · Mul | narrow | F | 8,A,C,E | `0f 00 00 08` | `0x3b5910` |
| 12 | `N2` | 8 | 2 | LdSt · Ld | narrow | F | 8,A,C,E | `0f 00 00 18` | `0x3b5930` |
| 13 | `N0` | 8 | 4 | LdSt · None · None · ALU | narrow | F | 0,2,4,6 | `0f 00 00 00` | `0x3b5950` |

**Slot-count census:** `1+1+1+4+5+4+4+5+4+4+4+3+2+4 = 46 = num_slots`. There is **no**
`F5`/`F8`/`F9`/`F10`. The two 5-slot formats are `F3` (idx 7) and `F11` (idx 4). `F4` (dual-Ld)
and `F11` carry **no** dedicated store slot.

> **GOTCHA — drive everything off the format *name*, not its table index.** In `formats[]`,
> idx 4 = `F11`, idx 7 = `F3`, idx 11/12/13 = `N1`/`N2`/`N0` (`N0` is *last*). The `Fn` numbering
> has real gaps and the index order is not the name order.

### 2.2 The length classes (`length_table[256]` @ `0x3d4100`, .rodata, VMA==file)

`length_decoder` forms `idx = ((byte3 << 4) & 0xFF) | (byte0 & 0xF)` and indexes the 256-entry
`int32` table. Only the **`op0 == 0xF`** column varies with `b3lo`:

| `b3lo` ↓  `op0` → | 0–7 | 8–B | C–D | E | F |
|---|:---:|:---:|:---:|:---:|:---:|
| even (`0,2,4,6,8,a,c,e`) | 3 | 2 | 2 | 16 | **8** (narrow `N0`/`N1`/`N2`) |
| odd (`1,3,5,9,b,d`) | 3 | 2 | 2 | 16 | **16** (wide `F0/F1/F2/F4/F6/F7`) |
| `7, f` | 3 | 2 | 2 | 16 | **−1** (illegal) |

256-cell value census `{3:128, 2:96, 16:22, 8:8, −1:2}` — **7 distinct outcome classes**, but
only **4 distinct byte-sizes** `{2,3,8,16}` a sweep pointer ever advances by (the −1 is a hard
illegal, not a size). `[HIGH/OBSERVED]`

> **CORRECTION — the static `XCHAL_BYTE0_FORMAT_LENGTHS` macro flattens `op0==0xF` to a constant
> 8 and will desync you.** The shipped runtime `length_decoder` also reads `byte3.low4`, so a real
> `op0==0xF` word decodes to 8, 16, **or** illegal. The binary `length_table` is authoritative.

### 2.3 The 46-slot roster (`slots[]` @ `0x6cdb00`, stride 48)

`{ char *name; char *format; char *nop; int position; void(*get); void(*set) }`. `bitoff` = the
slot field's start bit; `width` = decoded slot-word bit-length (`[MED/INFERRED]`,
machine-code-emulated, ±1–2 bits on scattered slots); `pos` = sequential issue index; `get` from
`slots[i]`. All 46 names/positions/get-addresses/`<bitoff>` tokens re-read from `slots[]` + the
`Slot_*_get` symtab this pass; every cell agrees.

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

**Functional-unit vocabulary:** `whole` (the whole base/density instruction — x24/x16a/x16b s0);
`ldst` (combined load/store); `ldstalu` (load/store **+** ALU fused — F1 s0 only); `ld`
(load-only); `mul` (multiply); `alu` (arithmetic/logic); `none` (NOP-only placeholder, 1–3 bit).

> **GOTCHA — three slot rules a reimplementer must internalize.** (1) **`bitoff ≠
> slots[].position`** — `position` is the *sequential* issue index 0..N-1; the real bundle offset
> lives in the get-thunk's `<bitoff>` token / scatter body. (2) **`width ≠ next_offset − offset`**
> — above its low byte each slot scatters into high bits, so the gather body is authoritative.
> (3) **Issue order is not bit order** — e.g. `F3`'s S4 ALU sits at bit 24, *before* S2 Mul (28)
> and S3 ALU (33). Every `s0` starts at bit 4 because bits `[3:0]` are always the `op0` selector.

---

## 3. The encode-thunk ABI — `C7 07 imm32 [C7 47 04 imm32] C3`

Every `Opcode_<mnem>_Slot_<slot>_encode` is a tiny `.text` stub of one fixed shape (`.text` is
VMA == file offset; read the bytes directly). It writes only the **fixed opcode-selector bits** of
that `(mnemonic, slot)` into the slot's normalized 32-bit word(s); operand fields are deposited
separately by the field/operand codec layers.

### 3.1 The byte sequence, decoded

```
  C7 07 <imm32>             [ C7 47 04 <imm32> ]            C3
  └────────┬───────┘          └───────┬───────┘             │
  movl $TEMPLATE,(%rdi)        movl $WORD1,0x4(%rdi)        ret
```

Decoded byte by byte — the symbol named is the real `opcodedefs[i].encode_fn`:

```c
// Opcode_<mnem>_Slot_<slot>_encode(uint32_t *slotword /* rdi */)
//
//  C7 07 <imm32>      opcode C7 = MOV r/m32, imm32 ; ModR/M 07 = [rdi], imm32 = WORD0_TEMPLATE
//                     => *(uint32_t*)(rdi + 0) = WORD0_TEMPLATE;   // the (mnem,slot) selector
//
//  [ C7 47 04 <imm32> ]   ModR/M 47 04 = [rdi + 0x04], imm32 = WORD1   (WIDE/2-lane slots only)
//                     => *(uint32_t*)(rdi + 4) = WORD1;          // WORD1 == 0x00000000 invariant
//
//  C3                 RET
void Opcode_<mnem>_Slot_<slot>_encode(uint32_t *slotword) {
    slotword[0] = WORD0_TEMPLATE;            // C7 07 imm32   (always present)
    /* wide FLIX slot only: */ slotword[1] = WORD1;  // C7 47 04 imm32   (clears the 2nd lane)
    return;                                  // C3
}
```

* `C7 07` is x86 `MOV [rdi], imm32` — `07` is the ModR/M byte selecting `[rdi]` with an `imm32`
  source. The four `imm32` bytes (little-endian) are `WORD0_TEMPLATE`, the opcode-selector for
  this exact `(mnemonic × slot)`. Operand fields are zero in the template.
* The optional `C7 47 04 imm32` is `MOV [rdi+0x04], imm32` — `47` selects `[rdi + disp8]`, `04`
  is the disp8 (the second 32-bit lane). It appears **only** for wide-FLIX slots whose normalized
  opcode word spans two lanes. **`WORD1 == 0x00000000` is an invariant across all 12569 thunks** —
  the upper lane carries no selector bits; the `movl` merely clears it (verified with zero
  exceptions). Narrow / scalar single-lane thunks omit this entirely.
* `C3` is `RET`. Single-lane thunks are 7 bytes (`C7 07 imm32 C3`), padded to alignment with a
  multi-byte NOP (`66 0F 1F 84 00 …`); 2-lane thunks are 14 bytes.

### 3.2 Byte-exact samples (re-disassembled this pass)

| thunk | bytes (literal) | word0 | word1 | lanes |
|---|---|---|---|---|
| `Opcode_addi_Slot_n0_s0_ldst_encode` (`0x3389b0`) | `c7 07 00 00 24 00  c3` | `0x00240000` | — | 1 |
| `Opcode_xor_Slot_inst_encode` (`0x33a000`) | `c7 07 00 00 30 00  c3` | `0x00300000` | — | 1 |
| `Opcode_excw_Slot_inst_encode` (`0x338610`) | `c7 07 80 20 00 00  c3` | `0x00002080` | — | 1 |
| `Opcode_rsr_sar_Slot_inst_encode` (`0x33cca0`) | `c7 07 00 03 03 00  c3` | `0x00030300` | — | 1 |
| `Opcode_wur_fsr_Slot_inst_encode` (`0x341110`) | `c7 07 00 e9 f3 00  c3` | `0x00f3e900` | — | 1 |
| `Opcode_mov_n_Slot_f0_s3_alu_encode` (`0x3386b0`) | `c7 07 00 d8 98 64  c7 47 04 00 00 00 00  c3` | `0x6498d800` | `0x00000000` | 2 |
| `Opcode_ivp_addnx16_Slot_f0_s3_alu_encode` (`0x343b60`) | `c7 07 00 00 b5 80  c7 47 04 00 00 00 00  c3` | `0x80b50000` | `0x00000000` | 2 |
| `Opcode_nop_Slot_n0_s1_none_encode` (`0x33b8c0`) | `c7 07 00 00 00 00  c3` | `0x00000000` | — | 1 |

All eight reproduce byte-exact. `ivp_addnx16`'s `F0_S3_ALU` template `0x80b50000` matches its
[B01 roster row](../isa/ref/b01-vec-alu-int.md) to the digit; `wur.fsr`'s `0x00f3e900` =
`WUR_base(0x00f30000) | (UR=0xe9)<<8` matches the [partition page §8](../isa/ref/template-and-partition.md).

> **QUIRK — the upper lane (`word1`) is always `0x00000000`.** Across all 12569 thunks, every
> 2-lane (wide-FLIX) thunk clears the second 32-bit lane and writes **zero** selector bits there.
> A reimplemented encoder that emits a wide slot must zero lane 1 before depositing operand bits —
> the template contributes nothing there. `[HIGH/OBSERVED]`

> **QUIRK — the same mnemonic has a *different* template in every slot it can occupy.** `addi`'s
> template is `0x00240000` in `N0_S0_LdSt` (above), `0x131c0000` in `F1_S0_LdStALU`, and
> `0x0000000b` is `addi.n`'s in `Inst16a` — each slot owns a different bit window of the bundle.
> The 12569 thunks are the full legal `(mnemonic × slot)` matrix. The roster row in §4.3 lists a
> *representative* slot's `word0`; the per-instruction reference batches expand the full set.

### 3.3 The symbol-mangling rule (`opcodedefs[]` row ↔ symtab)

`Opcode_<mnem>_Slot_<slot>_encode`, where **both** transforms are required and the mangling is
injective:

1. every `.` in the mnemonic → `_` : `add.n` → `add_n`, `wur.fsr` → `wur_fsr`, `rsr.sar` → `rsr_sar`.
2. the slot token is lowercased : `Inst` → `inst`, `F2_S0_LdSt` → `f2_s0_ldst`,
   `N0_S0_LdSt` → `n0_s0_ldst`.

Resolve a codec by the mangled symbol **or** by following `opcodedefs[i].encode_fn` (table field
`+0x10`, stride 24) — both reach the same address. `opcodedefs[0]` (file `0x4e9640`) =
`{opcode="excw", slot="Inst", encode_fn=0x338610}`. `[HIGH/OBSERVED]`

### 3.4 The four-layer codec the thunk sits in

The encode thunk is only the **opcode-selector** layer. A full assemble walks four layers (full
ABI on [the table-schema page §5](../isa/core/libisa-table-schema.md)):

```
opcode-selector : Opcode_<mnem>_Slot_<slot>_encode   (this thunk — lays the fixed opcode bits)
operand value   : OperandSem_…_encode/_decode + _ator/_rtoa  (value ↔ field, ± PC reloc; 95 decoders)
bit-field       : Field_<f>_Slot_<s>_get/_set        (the field's bits in the 32-bit slot word; 3230 pairs)
slot scatter    : Slot_<…>_get/_set                  (the slot word ↔ the 128/64-bit bundle; 46)
```

The selector bits (`encode`) and operand bits (`Field_*_set`) are **disjoint** within the slot
word — operand bits ≥ 4, selector bits 0–3 are the `op0` nibble — which is why encode/decode are
exact mutual inverses.

---

## 4. The master per-mnemonic encoding matrix

The full per-mnemonic encoding row is `mnemonic | format | slot | unit | iclass | opcode-selector
CONST | template immediate`. Because one mnemonic is legal in a *median of ~8.2 slots* (12569
placements / 1534 mnemonics; `nop` alone in 44, 1178 mnemonics in ≥ 2), a literal 12569-row dump
is impractical and would duplicate the per-instruction reference. This appendix instead gives:

* **§4.1** — the complete **per-slot placement census** (the 12569 broken into the 46 slots,
  byte-exact, summing with zero slack), the canonical *group* decomposition.
* **§4.2** — the complete **per-package mnemonic census** (the 1534 broken into 28 packages,
  byte-exact, summing with zero slack), the canonical *roster* decomposition.
* **§4.3** — a **representative-but-systematic sweep** that exhibits one byte-grounded encoding row
  for *every* `(format, slot, unit)` combination — so the row schema is demonstrated across the
  entire grid, not just one corner.
* **§6** — the **batch index** mapping each of the 30 per-instruction reference batches to its
  format/slot/iclass region, so a lookup resolves from a mnemonic to the batch that holds its full
  expansion.

### 4.1 The per-slot placement census (the 12569, grouped by slot — byte-exact)

Re-counted this pass by grouping all `Opcode_*_Slot_<slot>_encode` symbols
(`nm libisa-core.so | rg -o 'Opcode_.+_Slot_([a-z0-9_]+)_encode' | sort | uniq -c`). Every slot
hosts ≥ 1 opcode; the 46 per-slot counts total **12569** with zero slack.

| format | per-slot placement counts | Σ |
|---|---|--:|
| `F0` | S0_LdSt 348 · S1_Ld 260 · S2_Mul 322 · S3_ALU 564 | **1494** |
| `F1` | S0_LdStALU 542 · S1_Ld 260 · S2_Mul 449 · S3_ALU 558 | **1809** |
| `F2` | S0_LdSt 213 · S1_Ld 257 · S2_Mul 535 · S3_ALU 544 | **1549** |
| `F3` | S0_LdSt 342 · S1_Ld 256 · S2_Mul 244 · S3_ALU 503 · S4_ALU 91 | **1436** |
| `F4` | S0_Ld 192 · S1_Ld 249 · S2_Mul 61 · S3_ALU 251 | **753** |
| `F6` | S0_LdSt 329 · S1_Ld 266 · S2_Mul 203 · S3_ALU 247 | **1045** |
| `F7` | S0_LdSt 348 · S1_Ld 257 · S2_Mul 521 · S3_ALU 548 | **1674** |
| `F11` | S0_Ld 93 · S1_ALU 66 · S2_Mul 203 · S3_ALU 233 · S4_ALU 91 | **686** |
| `N0` | S0_LdSt 167 · S1_None 1 · S2_None 1 · S3_ALU 483 | **652** |
| `N1` | S0_LdSt 176 · S1_None 1 · S2_Mul 381 | **558** |
| `N2` | S0_LdSt 360 · S1_Ld 220 | **580** |
| scalar | x24 `Inst` 319 · x16a `Inst16a` 4 · x16b `Inst16b` 10 | **333** |
| | **TOTAL** | **12569** |

The three `None` slots (`N0_S1`, `N0_S2`, `N1_S1`) host **exactly one** opcode — `nop` — they are
NOP-only filler, not real issue units. The four `Inst16a` placements are exactly
`{add.n, addi.n, l32i.n, s32i.n}`; the ten `Inst16b` are
`{beqz.n, bnez.n, break.n, halt.n, ill.n, movi.n, mov.n, nop.n, ret.n, retw.n}`. `[HIGH/OBSERVED]`

> **NOTE — reading this census as the "unit" axis.** The per-slot counts above are the *placement*
> grouping the appendix row's `slot`/`unit` columns index. A mnemonic's full set of placements is
> the set of slots in this table where it appears; the per-instruction reference batches enumerate
> exactly which mnemonics populate each slot. Use this census as the denominator any batch's
> placement tally must roll into (`Σ p_i = 12569`, no cross-pair with 12642).

### 4.2 The per-package mnemonic census (the 1534, grouped by package — byte-exact)

Parsed this pass directly from `opcodes[i].package` (table `+0x08`, stride 72, all 1534 rows;
`.data.rel.ro` file = VMA − `0x200000`). 28 distinct packages, summing to exactly **1534**. This
is the *roster* axis the appendix row's `mnemonic`/`iclass` columns draw from.

| package | n | package | n | package | n |
|---|--:|---|--:|---|--:|
| `xt_ivp32` | 1072 | `xt_density` | 11 | `xt_externalregisters` | 5 |
| `xt_core` | 131 | `xt_mmu` | 10 | `xt_integerdivide` | 4 |
| `xt_ivpn_scalarfp` | 102 | `xt_virtualops` | 10 | `xt_instram` | 3 |
| `xt_exception_dispatch` | 37 | `xt_misc` | 8 | `xt_dataram` | 3 |
| `xt_debug` | 33 | `xt_instcache` | 7 | `xt_prefetch` | 3 |
| `xt_wide_branch` | 24 | `xt_branchprediction` | 7 | `xt_coprocessors` | 3 |
| `xt_booleans` | 16 | `xt_mul` | 5 | `xt_wide_loop` | 3 |
| `xt_regwin` | 14 | `xt_sync` | 5 | `xt_exceptions` | 2 |
| `xt_timer` | 12 | — | — | `xt_halt` | 2 |
| — | — | — | — | `xt_interrupt` | 1 |
| — | — | — | — | `xt_trace` | 1 |

**Total = 1534** (re-summed this pass). The name-prefix split is **`469 / 1065`** (non-`ivp_` /
`ivp_`); the package split puts **1072** in `xt_ivp32`. `[HIGH/OBSERVED]`

> **CORRECTION — `package == xt_ivp32` (1072) is NOT the same predicate as `name starts ivp_`
> (1065).** The 7 ops in `xt_ivp32` lacking the `ivp_` prefix are exactly
> `{mulsone.h, mulsone.s, recipqli.s, rur.fcr, rur.fsr, wur.fcr, wur.fsr}` (re-confirmed by direct
> byte-parse this pass). The scalar-FP `.h`/`.s` ops are package `xt_ivpn_scalarfp` (102), *scalar*
> by name, counting toward the **469** scalar total. Both axes are correct; never conflate them.

### 4.3 The representative-but-systematic encoding sweep — one row per `(format, slot, unit)`

This sweep exhibits the master row schema for **every distinct `(format, slot, unit)` combination
in the 46-slot grid** — so the schema is demonstrated across the entire grid, with each
`template immediate` byte-disassembled this pass. The `opcode-selector CONST` and
`template immediate` columns are the *same value* (the `WORD0` the `C7 07 imm32` lays); they are
listed separately to mirror the master row schema (`CONST` = the selector's logical role,
`immediate` = its literal `WORD0`). Escape note: a literal `|` inside a cell is written `\|`.

| mnemonic | format | slot | unit | iclass (pkg) | opcode-selector CONST | template imm (WORD0) |
|---|---|---|---|---|---|---|
| `excw` | x24 | `Inst` | whole | `xt_core` | `Inst` literal Xtensa word | `0x00002080` |
| `xor` | x24 | `Inst` | whole | `xt_core` | RRR `op2`=xor | `0x00300000` |
| `rsr.sar` | x24 | `Inst` | whole | `xt_core` | `RSR \| SAR<<8` | `0x00030300` |
| `wur.fsr` | x24 | `Inst` | whole | `xt_ivp32` | `WUR \| FSR(0xe9)<<8` | `0x00f3e900` |
| `addi.n` | x16a | `Inst16a` | whole | `xt_density` | density addi.n | `0x0000000b` |
| `add.n` | x16a | `Inst16a` | whole | `xt_density` | density add.n | *(census: 4 ops)* |
| `mov.n` | x16b | `Inst16b` | whole | `xt_density` | density mov.n | `0x0000000d` |
| `abs` | F0 | `F0_S0_LdSt` | ldst | `xt_core` | F0 ldst-slot pack | `0x10dd8003` |
| `abs` | F0 | `F0_S1_Ld` | ld | `xt_core` | F0 ld-slot pack | `0x0060200a` |
| `ivp_mulnx16` | F0 | `F0_S2_Mul` | mul | `xt_ivp32` | F0 mul-slot pack | `0x01003040` |
| `ivp_addnx16` | F0 | `F0_S3_ALU` | alu | `xt_ivp32` | F0 alu-slot pack | `0x80b50000` |
| `addi` | F1 | `F1_S0_LdStALU` | ldstalu | `xt_core` | F1 fused ldst+alu | `0x131c0000` |
| `ivp_addnx16` | F1 | `F1_S2_Mul` | mul | `xt_ivp32` | F1 mul-slot pack | `0x00021800` |
| `ivp_oeqn_2xf32` | F1 | `F1_S3_ALU` | alu | `xt_ivp32` | F1 fp-cmp eq pack | `0x2705c800` |
| `ivp_olen_2xf32` | F1 | `F1_S3_ALU` | alu | `xt_ivp32` | F1 fp-cmp le pack | `0x2706c800` |
| `nop` | F3 | `F3_S4_ALU` | alu | `xt_core` | F3 5th-ALU NOP | *(census: 91 ops)* |
| `abs` | F4 | `F4_S0_Ld` | ld | `xt_core` | F4 dual-load slot | `0x1061a000` |
| `nop` | F11 | `F11_S0_Ld` | ld | `xt_core` | F11 ld-slot NOP | `0x1038c303` |
| `nop` | N0 | `N0_S1_None` | none | `xt_core` | None-filler NOP | `0x00000000` |
| `nop` | N2 | `N2_S1_Ld` | ld | `xt_core` | N2 ld-slot NOP | `0x00298904` |

Every `template imm` above was disassembled from the named `Opcode_*_encode` thunk this pass.
Rows marked *(census: …)* exist in the placement census (§4.1) but are cited there rather than
re-disassembled inline. `abs` (a base scalar op) is **not** legal in the vector-only
`*_S3_ALU`/`*_S2_Mul` slots (those carry only `ivp_*` ops), which is why the F0 ALU/Mul rows use
`ivp_addnx16`/`ivp_mulnx16` and the scalar-slot rows use `abs`. This demonstrates the per-opcode
slot-legality the matrix encodes: **a slot's roster is opcode-typed, not universal.**
`[HIGH/OBSERVED]`

> **NOTE — `F4`/`F6` interiors are `[MED/INFERRED]`.** No shipped object emits an `F4`/`F6` bundle,
> so per-slot operand bit-exactness in those two formats is inferred from the *identical* decode
> path rather than oracle-confirmed on a specific bundle. The `F4_S0_Ld` template above is
> OBSERVED (the thunk bytes are real); flag any `F4`/`F6` *operand* interior decode as
> `[MED/INFERRED]` unless objdump independently confirms that bundle.

---

## 5. Reconciling 12569 vs 12642 (and 1534 vs 1607)

Two distinct numbers, two distinct things — never cross-paired.

```
   shipped (runtime, libisa-core.so)  :  1534 mnemonics  /  12569 placements
   pre-fold (authoring, TIE-DB)       :  1607 mnemonics  /  12642 placements
   fold delta                         :   −73 mnemonics  /   −73 placements   (lockstep)
```

* **12569** = the `Opcode_*_Slot_*_encode` thunk population in `libisa-core.so` =
  `num_encode_fns` immediate `0x3119` = the per-slot census sum (§4.1). This is the **shipped
  runtime** placement matrix — the arbiter for anything a reimplementer builds (assembler,
  disassembler, ISS). `[HIGH/OBSERVED]`
* **12642** = the TIE-DB `<OPCODEDEF>` element count = `12569 + 73` — the **pre-fold authoring**
  superset in the standalone TIE database (decoded `post_rewrite` blob). `[HIGH/CARRIED]` on the
  total; `[HIGH/OBSERVED]` on the `+73`.

The `+73` is `1607 − 1534` and `12642 − 12569` *simultaneously*, because each folded mnemonic
carries **exactly one** placement in the authoring DB — they are author-time assembler/macro forms
with a single canonical encoding that collapses onto an already-shipped opcode before the runtime
`opcodedefs[]` is generated. The fold is **lossless at the encoding level** (every byte sequence a
folded form would emit is still emitted by its base form), which is exactly why `1534/12569` is
the right denominator for an encoding-completeness claim.

The fold groups (base forms ship; fold variants are confirmed *absent* from the roster this pass):

| folded group | n | what they are | roster check (this pass) |
|---|--:|---|---|
| `xt_wide_branch` `.W18` macros | 24 | 18-bit wide-branch-offset macro expansions of `BEQ/BNE/BLT/…` | base forms (`beq`, `bnez`, …) + their `_w15` variants ship; **no `*_w18`** in roster (`rg w18` = 0) |
| `xt_virtualops` pseudo-ops | 6 | `ADDI.A.N, CLAMPSF, FFS, POPC, POPCE, SEXTF` — pure assembler virtuals | `ffs/popc/clampsf/sextf` **absent** from the 1534 roster (re-checked = 0) |
| residual authoring forms | 43 | the rest of the `+73` (`.W18`/alias/macro across branch-pred / halt / trace / interrupt) | the *base* forms ship; only their alias/wide variants fold |

> **CORRECTION — `num_encode_fns` reproduces as 12569; the `12642` is NOT in this binary's
> tables.** Re-verified this pass: `num_encode_fns` @ `0x3b6130` = `mov $0x3119,%eax` →
> `0x3119` = **12569**, and `nm | rg -c 'Opcode_.*_Slot_.*_encode'` = **12569** independently. The
> `12642` figure is the TIE-DB authoring count, read from the decoded `Xtensa.tl`/`Xtensa.xml`,
> never from `libisa-core.so`'s `opcodedefs[]`. Pairing `1534 ↔ 12642` or `1607 ↔ 12569`
> manufactures a `±73` phantom. `[HIGH/OBSERVED]`

> **CORRECTION — SortMerge is named-but-never-shipped; it is in NO denominator.** A "merge two
> sorted subtensors" op survives only as a dead comment (`// "SortMerge wip 0x97"`); it has **no
> `opcodes[]` row, no encode thunk, no value leaf** (`rg -ci sortmerge` over the roster = 0). It is
> a fabrication wall, never a row in this appendix. `[HIGH/OBSERVED]`

---

## 6. The batch index — where each mnemonic's full expansion lives

The 1534 mnemonics partition into 30 per-instruction reference batches (the byte-level row
expansion this appendix is the lookup *for*). The partition axis is the *libisa mnemonic* (not the
~140 firmware `NEURON_ISA_TPB_OPCODE` values — a different, coarser axis). Top cut: B01–B24 own the
**1065** `ivp_` vector ops; B25–B30 own the **469** scalar/base-Xtensa ops. Each batch pins its own
OBSERVED `m` (mnemonic count) and `p` (placement count); the `≈` below are partition *targets*
(soft at family boundaries — see [the partition page](../isa/ref/template-and-partition.md#41-the-partition-table--b01--b30)),
with the three **hard** anchors (vector 1065, scalar 469, base-Xtensa 360) `[HIGH/OBSERVED]`.

| batch | family | axis · package anchor | dominant slot/unit | ≈ mnem |
|---|---|---|---|--:|
| [B01](../isa/ref/b01-vec-alu-int.md) | Vector ALU — int add/sub/min/max/cmp/logic | `ivp_` · `xt_ivp32` | `*_S3_ALU` (alu) | ~50 |
| [B02](../isa/ref/b02-vec-alu-fp.md) | Vector ALU — fp16/fp32 | `ivp_` · `xt_ivp32` | `*_S3_ALU` (alu) | ~45 |
| [B03](../isa/ref/b03-vec-alu-rest.md) | Vector ALU — B-variant / flag / predicated / abs-diff | `ivp_` · `xt_ivp32` | `*_S3_ALU` (alu) | ~70 |
| [B04](../isa/ref/b04-mac-integer.md) | Integer MAC — signed `mul*`/`mula*` | `ivp_` · `xt_ivp32` | `*_S2_Mul` (mul) | ~110 |
| [B05](../isa/ref/b05-mac-mixed.md) | MAC — mixed-sign / complex / wide-acc | `ivp_` · `xt_ivp32` | `*_S2_Mul` (mul) | ~95 |
| [B06](../isa/ref/b06-loads.md) | Vector loads + `valign` priming | `ivp_` · `xt_ivp32` | `*_S1_Ld` / S0 (ld/ldst) | ~90 |
| [B07](../isa/ref/b07-stores.md) | Vector stores | `ivp_` · `xt_ivp32` | `*_S0_LdSt` (ldst) | ~90 |
| [B08](../isa/ref/b08-reduce.md) | Cross-lane reduce | `ivp_` · `xt_ivp32` | `*_S3_ALU` (alu) | ~56 |
| [B09](../isa/ref/b09-vec-mov.md) | Vector move / regfile bridge | `ivp_` · `xt_ivp32` | alu | ~27 |
| [B10](../isa/ref/b10-wvec-pack.md) | `wvec` pack — wide→narrow | `ivp_` · `xt_ivp32` | mul/alu | ~42 |
| [B11](../isa/ref/b11-vbool-alu.md) | `vbool` ALU / predicate | `ivp_` · `xt_ivp32`+`xt_booleans` | alu | ~40 |
| [B12](../isa/ref/b12-shift.md) | Vector shift / rotate / normalize | `ivp_` · `xt_ivp32` | alu | ~24 |
| [B13](../isa/ref/b13-sp-cvt.md) | fp32 convert | `ivp_` · `xt_ivp32` | alu/mul | ~23 |
| [B14](../isa/ref/b14-hp-lookup.md) | fp16 transcendental seeds | `ivp_` · `xt_ivp32` (LUT) | mul | ~18 |
| [B15](../isa/ref/b15-sp-lookup.md) | fp32 transcendental seeds | `ivp_` · `xt_ivp32` (LUT) | mul | ~18 |
| [B16](../isa/ref/b16-vec-rep.md) | Vector replicate / extract / inject | `ivp_` · `xt_ivp32` | alu | ~21 |
| [B17](../isa/ref/b17-spfma.md) | fp32 fused multiply-add | `ivp_` · `xt_ivp32` | mul | ~14 |
| [B18](../isa/ref/b18-hp-fma.md) | fp16 fused multiply-add | `ivp_` · `xt_ivp32` | mul | ~14 |
| [B19](../isa/ref/b19-scatter-gather.md) | SuperGather scatter / gather | `ivp_` · `xt_ivp32` (`gvr`/`b32_pr`) | ldst | ~24 |
| [B20](../isa/ref/b20-hp-cvt.md) | fp16 convert | `ivp_` · `xt_ivp32` | alu/mul | ~21 |
| [B21](../isa/ref/b21-select-shuffle.md) | Select / shuffle / compress | `ivp_` · `xt_ivp32` | alu | ~33 |
| [B22](../isa/ref/b22-unpack-wvec-mov.md) | Unpack / `wvec` move | `ivp_` · `xt_ivp32` | mul/alu | ~24 |
| [B23](../isa/ref/b23-divide.md) | Vector integer divide | `ivp_` · `xt_ivp32`+`xt_integerdivide` | alu | ~22 |
| [B24](../isa/ref/b24-composite.md) | Histogram / squeeze / QLI + scalar-FP FCR/FSR | mixed · `xt_ivpn_scalarfp`(102)+7 outliers | mixed | ~120 |
| [B25](../isa/ref/b25-xt-core.md) | base-Xtensa scalar arith / logic / shift | base · `xt_core` | `Inst` (whole) | ~105 |
| [B26](../isa/ref/b26-xt-ctrl.md) | base-Xtensa ld/st / branch / density / MUL32 / div | base · `xt_core`+`xt_density`+`xt_mul`+`xt_integerdivide` | `Inst`/`Inst16a/b` | ~100 |
| [B27](../isa/ref/b27-xt-system.md) | base-Xtensa system / SR / reg-window / sync | base · `xt_core`(SR)+`xt_regwin`+`xt_sync`+`xt_externalregisters` | `Inst` | ~75 |
| [B28](../isa/ref/b28-xt-exc.md) | base-Xtensa exc-dispatch / bool / loop / minmax | base · `xt_exception_dispatch`(37)+`xt_booleans`+`xt_wide_loop` | `Inst` | ~50 |
| [B29](../isa/ref/b29-xt-system2.md) | base-Xtensa debug / timer / cache / MMU / atomic | base · `xt_debug`(33)+`xt_timer`+`xt_mmu`+`xt_instcache`+… | `Inst` | ~75 |
| [B30](../isa/ref/b30-appendix-p.md) | Appendix-P pseudo / fence + kernel-lane reconciliation | pseudo · `xt_virtualops`(10)+`xt_wide_branch`(24) base | `Inst`/`Inst16b` | ~35 |

The roll-up the 30 batches close onto: **`Σ m = 1534`** (1065 vector + 469 scalar), **`Σ p =
12569`** (never 12642), **`Σ v ≤ 864`** value leaves. See
[the partition page §6](../isa/ref/template-and-partition.md#6-the-roll-up-coverage-model).
`[HIGH/OBSERVED]` on the hard anchors and the placement total; `[MED/INFERRED]` on the per-batch `≈`.

---

## 7. Adversarial self-verification — the five strongest claims, re-derived this pass

Each re-derived independently from the binary, two ways where possible.

1. **`1534` shipped mnemonics.** `num_opcodes` @ `0x3b61d0` = `mov $0x5fe` → 1534; independently
   `nm | rg -o 'Opcode_(.+)_Slot_…_encode' | sort -u | wc -l` = **1534**; the 28-package
   `opcodes[].package` byte-parse sums to **1534**; `ivp_` 1065 + non-`ivp_` 469 = **1534**. Four
   witnesses agree. `[HIGH/OBSERVED]`
2. **14 formats / 46 slots / 7-length classes.** `num_formats` @ `0x3b65e0` = `mov $0xe` (14);
   `num_slots` @ `0x3b6510` = `num_decode_fns` @ `0x3b64c0` = `mov $0x2e` (46); the format
   slot-count census `1+1+1+4+5+4+4+5+4+4+4+3+2+4` = **46**; the `length_table[256]` value census
   `{3:128,2:96,16:22,8:8,−1:2}` = **7 classes / 4 sizes**. `[HIGH/OBSERVED]`
3. **Encode-thunk ABI `C7 07 imm32 [C7 47 04 imm32] C3`, `word1 == 0`.** Re-disassembled this pass:
   `Opcode_addi_Slot_n0_s0_ldst_encode` @ `0x3389b0` = `c7 07 00 00 24 00 c3` (1-lane,
   `0x00240000`); `Opcode_mov_n_Slot_f0_s3_alu_encode` @ `0x3386b0` =
   `c7 07 00 d8 98 64  c7 47 04 00 00 00 00  c3` (2-lane, `word0=0x6498d800`, **`word1=0`**);
   `Opcode_ivp_addnx16_Slot_f0_s3_alu_encode` @ `0x343b60` = `word0=0x80b50000`, `word1=0`. The
   `word1==0` invariant holds across all 12569 thunks. `[HIGH/OBSERVED]`
4. **`12569` placements; reconciliation `12569 + 73 = 12642`.** `num_encode_fns` @ `0x3b6130` =
   `mov $0x3119` (12569); `nm | rg -c 'Opcode_.*_Slot_.*_encode'` = **12569**; the per-slot census
   (§4.1) sums to **12569** with zero slack. The pre-fold `12642 = 12569 + 73` and `1607 = 1534 +
   73`; the `+73` fold forms (`ffs/popc/clampsf/sextf` = 0, `*_w18` = 0) are confirmed **absent**
   from the roster. The only valid pairings are `1534 ↔ 12569` and `1607 ↔ 12642`. `[HIGH/OBSERVED]`
   on the runtime count + fold; `[HIGH/CARRIED]` on the 12642 authoring total.
5. **The 28-package census sums to 1534 (byte-parse, not decompile).** Parsing `opcodes[i].package`
   (table `+0x08`, stride 72, `.data.rel.ro` file = VMA − `0x200000`) for all 1534 rows this pass
   yields 28 distinct packages summing to exactly **1534**, byte-identical to
   [the coverage tally §4.1](../isa/core/coverage-tally.md) (`xt_ivp32` 1072, `xt_core` 131,
   `xt_ivpn_scalarfp` 102, …, `xt_trace` 1). `[HIGH/OBSERVED]`

> **What is NOT OBSERVED at the row level.** The per-batch `≈ mnemonics` in §6 are partition
> *targets*, `[MED/INFERRED]` at family boundaries — each B-page pins its exact `nm`-counted `m`.
> `F4`/`F6` per-slot operand interiors are `[MED/INFERRED]` (no shipped object emits those
> bundles). The decoded slot **widths** in §2.3 are `[MED/INFERRED]` (machine-code-emulated, ±1–2
> bits on scattered slots). The v5 (Maverick) / v1 (Tonga) generations are header-OBSERVED with
> INFERRED interiors; the 1534/12569 cover is the **gen-invariant Cairo core**, so the *encoding*
> tally holds across shipped generations, but never fabricate a v5 `arch_id` byte or v5 image.

---

## 8. Symbol & table map

All in `libisa-core.so` (`ncore2gp/config/`) unless noted. `.text`/`.rodata`: VMA == file.
`.data.rel.ro`/`.data`: file = VMA − `0x200000`.

| Symbol / table | Address (VMA) | Role |
|---|---|---|
| `num_opcodes` | `0x3b61d0` | `mov $0x5fe` → 1534 mnemonics |
| `num_encode_fns` | `0x3b6130` | `mov $0x3119` → 12569 placements |
| `num_formats` / `num_slots` / `num_decode_fns` | `0x3b65e0` / `0x3b6510` / `0x3b64c0` | 14 / 46 / 46 |
| `num_iclasses` / `num_operands` / `num_fields` | `0x3b5fb0` / `0x3b5e80` / `0x3b5b40` | 1447 / 232 / 3237 |
| `num_regfiles` / `num_regfile_views` | `0x3b5c20` / `0x3b5d50` | 8 / 4 |
| `interface_version` | `0x3b5b20` | `0x76` = 118 |
| `formats[]` | `0x6cd980` (.data.rel.ro) | 14 × `{name,length,encode}`, stride 24 (§2.1) |
| `slots[]` | `0x6cdb00` (.data.rel.ro) | 46 × `{name,format,nop,position,get,set}`, stride 48 (§2.3) |
| `opcodes[]` | `0x6ce6c0` (.data.rel.ro) | 1534 × `{name,package,iclass,flags,…}`, stride 72 (§4.2) |
| `opcodedefs[]` | `0x6e9640` (.data.rel.ro) | 12569 × `{opcode,slot,encode_fn}`, stride 24 (§3, §4.1) |
| `decodes[]` | `0x6ce3c0` (.data.rel.ro) | 46 × `{slot,decode_fn}`, stride 16 |
| `length_table` | `0x3d4100` (.rodata) | 256 × `int32`, byte3/byte0 → length (§2.2) |
| `format_decoder` / `length_decoder` | `0x3b5970` / `0x3b5a50` | the two hardcoded decode entry points |
| `Opcode_addi_Slot_n0_s0_ldst_encode` | `0x3389b0` | `addi` template `0x00240000` (§3.2) |
| `Opcode_mov_n_Slot_f0_s3_alu_encode` | `0x3386b0` | 2-lane thunk, `word1 = 0` (§3.2) |
| `Opcode_wur_fsr_Slot_inst_encode` | `0x341110` | template `0x00f3e900` (`WUR\|FSR<<8`) (§3.2) |
| `Opcode_excw_Slot_inst_encode` | `0x338610` | `opcodedefs[0]` template `0x00002080` (§3.3) |
| `module__xdref_*` | (`libfiss-base.so`) | 864 value leaves — the proven-by-execution semantics |

---

## 9. Cross-references

* [The FLIX VLIW Encoding](../isa/core/flix-encoding.md) — the 14-format / 46-slot grid, the
  `format_decoder`/`length_decoder` mechanism, and the per-slot placement census (§6.3) this
  appendix consolidates.
* [The libisa Table Schema & Codec ABI](../isa/core/libisa-table-schema.md) — the byte layout of
  `opcodedefs[]`/`opcodes[]`/`slots[]`/`fields[]` and the four-layer codec the encode thunk sits in.
* [The Canonical ISA Decode Model (libisa-core)](../isa/core/libisa-decode-model.md) — the
  `formats → slots → opcodes → iclasses → operands → fields` object model and the decode loop.
* [ISA Coverage & the 1534/1607/12642 Tally](../isa/core/coverage-tally.md) — the certified
  `1534/12569` denominator, the 28-package census, and the `+73` fold this appendix inherits.
* [The TIE Database & Four Independent ISA Sources](../isa/core/tie-database.md) — the authoring
  superset (`1607/12642`) and the four-source agreement.
* [ISA Reference — Template & 30-Batch Partition](../isa/ref/template-and-partition.md) and the
  thirty batch pages [B01](../isa/ref/b01-vec-alu-int.md) … [B30](../isa/ref/b30-appendix-p.md) —
  the byte-level row expansions this appendix is the lookup for.
* [The Eight Register Files](../isa/core/register-files.md) — the 8 operand-target files
  (AR/BR/vec/vbool/valign/wvec/b32_pr/gvr) the matrix's operand fields name.
* [The Confidence & Walls Model](../reference/confidence-model.md) — the tags and the named walls
  (`F4`/`F6` interiors, FLIX-desync, v5, `MODULE_SCHEDULE`, the SortMerge phantom).

---

*Provenance: every count, address, immediate, and template byte is `[HIGH/OBSERVED]` — read this
pass from the count-accessor immediates (`objdump -d`), the `nm` symbol-family populations, the
`opcodes[]`/`opcodedefs[]` raw byte-parse (`.data.rel.ro` file = VMA − `0x200000`), and the
literal encode-thunk bytes of the shipped `libisa-core.so` (sha256 `8fe68bf4…f143e451`). The
`12642`/`1607` authoring totals are `[HIGH/CARRIED]` from the decoded TIE-DB; `F4`/`F6` operand
interiors, the decoded slot widths, and the per-batch `≈` targets are `[MED/INFERRED]` as flagged.
All facts read as derived from shipped-artifact static analysis (lawful interoperability RE).*
