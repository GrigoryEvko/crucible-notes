# FLIX Co-Issue Matrix + Cache/Local-Memory Timing

This page closes the **microarchitecture** half of the Vision-Q7 *Cairo* (`ncore2gp`) FLIX VLIW
story: given the byte-level **format/length decode** and the **46-slot roster** that the
[FLIX VLIW Encoding](../isa/core/flix-encoding.md) page lays out, *which* combinations of ops can
**co-issue in one bundle**, *what each issue slot may carry*, and *how a memory access threads the
pipeline* — the I-cache / IRAM / DataRAM / AXI port timing, the stage-9 vector memory port, the
stage-10 vec-dest, the stage-11 store commit, and the scalar load-use bubble. It is the
co-issue / memory-port companion to the [Pipeline Timing Model](pipeline-timing.md) (latency) and
the [Local-Memory / LSU Model](lsu-memory.md) (port detail); read this page when you need to know
*how many of what* a scheduler may pack into one 128-bit bundle and *when* their operands land.

Everything here is read **directly from the shipped `ncore2gp` artifacts**: the
`libisa-core.so` `format_decoder` / `length_decoder` / `length_table` / `slots[]` / 12 569
`Opcode_*_Slot_*_encode` thunks (the **encoding** structure); `libcas-core.so`'s
`slot_issue_functions` / `slot_semantic_functions` / `slot_stall_functions` dispatch tables and its
per-`(format,slot,opcode,stage)` issue/stage/stall function bodies (the **cycle/reservation**
model); `core-isa.h` / `system.h` / `memmap.xmm` (the cache/RAM/bus geometry); and an independent
**device cross-check** — `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) on the shipped GPSIMD device
library `libneuroncustomop.a`, **1479** real FLIX bundles decoded and classified. Confidence tags
follow [the Confidence & Walls Model](../reference/confidence-model.md): `OBSERVED` =
byte/immediate/symbol read from the shipped binary (or computed by executing the shipped
simulator); `INFERRED` = reasoned over OBSERVED facts; `CARRIED` = re-used at a cited page's
confidence; crossed with `HIGH`/`MED`/`LOW`. The page default is `[HIGH/OBSERVED]`; claims that
depart from it carry an explicit tag. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orienting context).

> **GOTCHA — count only with `nm | rg -c`, never a decompile, and never an opcode-*name* grep.**
> Two traps converge on this page. (1) Counting a symbol family in the IDA/Hex-Rays decompile
> inflates **2–12×**. (2) **Co-issue legality is a *slot/functional-unit* property, not an
> opcode-name property.** A grep like `Opcode_ivp_mul.*` catches `ivp_mulsgnnx16` (a sign-extract
> **ALU** op) and lands it in S3/S4 ALU slots — manufacturing a phantom "multiply in the ALU
> lane." The arbiter is the **slot the placement names** (`…_Slot_<fmt>_s<N>_<unit>_…`), read from
> the `Opcode_*_Slot_*_encode` symbol and cross-checked against the `Slot_*_get` thunk's `<unit>`
> token. Below, every "lane" claim is grounded in the slot/unit, then re-challenged against the
> opcode-name superset.

> **GOTCHA — the `.data.rel.ro` VMA↔file delta is `0x200000` for these `ncore2gp` DLLs (not
> libtpu's `0x400000`).** `format_decoder` / `length_decoder` / the count getters and the per-port
> issue/stage functions are `.text` (VMA == file offset); the `slots[]`, `formats[]`,
> `length_table`, and `slot_issue_functions` tables they index are in `.rodata` (VMA == file
> offset) or `.data.rel.ro` / `.data` (file = VMA − `0x200000`). Confirmed per-section with
> `readelf -SW`: `libisa-core.so` `.data.rel.ro` VMA `0x67bb00` / file `0x47bb00`;
> `libcas-core.so` `.data.rel.ro` VMA `0x2070900` / file `0x1e70900` — both exactly `0x200000`.
> `extracted/` is gitignored; reach it with an absolute path or `fd --no-ignore`.

---

## 1. Two decoders, one bundle — the byte-level format + length decode

A FLIX-aware front end runs **two** independent table-driven decoders on each candidate instruction
word, both in `libisa-core.so`, both hardcoded (no pointer-table dispatch):

| decoder | input | output | symbol |
|---|---|---|---|
| `format_decoder` | the assembled 32-bit word `w` | format index `0..13` or `−1` | `0x3b5970` |
| `length_decoder` | the **raw bytes** `inst[0]`, `inst[3]` | byte length `{2,3,8,16}` or `−1` | `0x3b5a50` + `length_table` `0x3d4100` |

The format index selects the **slot roster** (which lanes exist); the length advances the sweep
pointer. The two are **mutually exact** — every word the format decoder calls *wide* is length 16,
every *narrow* is 8, every *illegal* is −1 — proven below by a 4096-combo simulation with **0
mismatches**.

### 1.1 The length key — `byte3.low4 × byte0.low4`

`length_decoder` (`0x3b5a50`) forms an 8-bit index from `byte3`'s low nibble (row) and `byte0`'s
low nibble (column = `op0`) and indexes a 256-entry `int32` table. Disassembled verbatim:

```c
// libisa-core.so  length_decoder @ 0x3b5a50  + length_table @ 0x3d4100
int length_decoder(const uint8_t *inst) {
    unsigned idx = ((unsigned)(inst[3] << 4) & 0xFF) | (inst[0] & 0x0F);
    return length_table[idx];          // .rodata, 256 × int32 LE
}
```

The 256-cell `length_table` was read straight from `.rodata` (file offset `0x3d4100`, VMA == file
offset). Only the **last column** (`op0 == 0xF`) varies with `byte3.low4`; the value
census over all 256 cells is `{3:128, 2:96, 16:22, 8:8, −1:2}`:

| `byte3.low4 (b3lo)` ↓  `op0` → | 0–7 | 8–B | C–D | E | F |
|---|:---:|:---:|:---:|:---:|:---:|
| even `{0,2,4,6,8,a,c,e}` | 3 | 2 | 2 | 16 | **8** (narrow `N0`/`N1`/`N2`) |
| odd `{1,3,5,9,b,d}` | 3 | 2 | 2 | 16 | **16** (wide `F0`/`F1`/`F2`/`F4`/`F6`/`F7`) |
| `{7, f}` | 3 | 2 | 2 | 16 | **−1** (illegal) |

So the length rule is: `op0 ∈ 0..7` ⇒ **3** (core `x24`); `op0 ∈ 8..B` ⇒ **2** (`x16a`); `op0 ∈ C..D`
⇒ **2** (`x16b`); `op0 == 0xE` ⇒ **16** (the 5-slot wide class `F3`/`F11`); `op0 == 0xF` splits on
`b3lo` parity into narrow-8 / wide-16 / illegal.

> **CORRECTION — the static `XCHAL_BYTE0_FORMAT_LENGTHS` macro is lossy and will desync you.** The
> Tensilica static macro keys length on `byte0` alone and flattens `op0 == 0xF` to a constant **8**
> bytes. The shipped runtime `length_decoder` does **not**: it also reads `byte3.low4`, so a real
> `op0 == 0xF` word decodes to 8, 16, **or** illegal. A linear sweep that assumes "`op0==0xF` ⇒ 8"
> mis-lengths every 16-byte `F0`/`F1`/`F2`/`F4`/`F6`/`F7` bundle as two 8-byte reads and locks onto
> the bundle's interior. The binary `length_table` is authoritative.

### 1.2 The format key — `format_decoder` (`0x3b5970`)

`format_decoder` runs a flat mask-and-match ladder on `w`, first hit wins, else `−1`. **Every mask
and compare immediate below was read directly from the disassembled `and $imm` / `cmp $imm` bytes**
(`f6 c2 08`, `83 e0 0c`, `81 e1 0f 00 00 0b`, …):

```c
// libisa-core.so  format_decoder @ 0x3b5970  (behavioural equal of the cmovne body)
int format_decoder(const uint8_t *inst) {
    uint32_t w = inst[0] | (inst[1]<<8) | (inst[2]<<16) | ((uint32_t)inst[3]<<24);
    if ((w & 0x00000008) == 0)          return  0; // x24   op0 0..7   (test $0x8,%dl; je)
    if ((w & 0x0000000C) == 0x8)        return  1; // x16a  op0 8..B
    if ((w & 0x0000000E) == 0xC)        return  2; // x16b  op0 C..D
    if ((w & 0x0B00000F) == 0x0100000F) return  3; // F0    op0=F, byte3-sel 0x01  (4-slot, 16B)
    if ((w & 0x0800000F) == 0x0800000E) return  4; // F11   op0=E, bit27 set       (5-slot, 16B)
    if ((w & 0x3700000F) == 0x0300000F) return  5; // F1    op0=F, byte3-sel 0x03  (4-slot, 16B)
    if ((w & 0x3700000F) == 0x3300000F) return  6; // F2    op0=F, byte3-sel 0x33  (4-slot, 16B)
    if ((w & 0x0800000F) == 0x0000000E) return  7; // F3    op0=E, bit27 clear     (5-slot, 16B)
    if ((w & 0x0B00000F) == 0x0900000F) return  8; // F4    op0=F, byte3-sel 0x09  (4-slot, 16B)
    if ((w & 0x3700000F) == 0x2300000F) return  9; // F6    op0=F, byte3-sel 0x23  (4-slot, 16B)
    if ((w & 0x3700000F) == 0x1300000F) return 10; // F7    op0=F, byte3-sel 0x13  (4-slot, 16B)
    if ((w & 0x1900000F) == 0x0800000F) return 11; // N1    op0=F, byte3-sel 0x08  (Mul, 8B)
    if ((w & 0x1900000F) == 0x1800000F) return 12; // N2    op0=F, byte3-sel 0x18  (2-slot, 8B)
    if ((w & 0x0900000F) == 0x0000000F) return 13; // N0    op0=F, b3lo {0,2,4,6}  (ALU, 8B)
    return -1;                                      // illegal: op0=F, b3lo {7,F}
}
```

The shipped body computes the last predicate with a `cmovne` (`3b5a27: and $0x900000f; cmp $0xf;
mov $-1,%edx; cmovne %edx,%eax; ret`) and the `F3` case as a bare `cmp $0xe,%esi` against the
`op0=E`-masked word; the C above is the verified-equal *behavioural* form.

> **NOTE — the wide-format selector lives in `byte3`, not in a `[25:24]/[26:24]` length-class
> field.** The binary `format_decoder` is a single flat ladder keyed on `op0` (bits `[3:0]`) plus the
> `byte3` selector (bits `[31:24]`). Some prior internal write-ups re-expressed the same selector as
> a two-step "length-class `[26:24]` then sub-format" decode (e.g. *l128b* `[25:24]==01`, *l128c*
> `[26:24]==011`). That re-expression is **numerically equivalent** — `F0`'s `byte3-sel 0x01` is
> `bit24=1, bit25=0` ⇒ `[25:24]==01`; `F4`'s `0x09` is `bit24=1, bit27=1` ⇒ `[25:24]==01` — but it
> is a *derived view*, not how the shipped code branches. **The shipped decoder keys on the full
> `byte3` selector byte against the masks above; reproduce that.**

### 1.3 Format → length → slot-roster summary

The 14 formats, their op0/byte3 selector, length, slot count and lane composition (slot roster and
all 46 bit offsets are on the [FLIX VLIW Encoding §5](../isa/core/flix-encoding.md) page; here we
need only the **issue profile** the scheduler reasons over):

| idx | format | len | #slots | real width | issue profile (S0…Sn) | `op0` |
|---:|---|:--:|:--:|:--:|---|:--:|
| 3 | `F0` | 16 | 4 | 4 | LdSt · Ld · Mul · ALU | F |
| 4 | `F11` | 16 | **5** | **5** | Ld · ALU · Mul · ALU · ALU | E |
| 5 | `F1` | 16 | 4 | 4 | **LdStALU** · Ld · Mul · ALU | F |
| 6 | `F2` | 16 | 4 | 4 | LdSt · Ld · Mul · ALU | F |
| 7 | `F3` | 16 | **5** | **5** | LdSt · Ld · Mul · ALU · ALU | E |
| 8 | `F4` | 16 | 4 | 4 | Ld · Ld · Mul · ALU (dual-load) | F |
| 9 | `F6` | 16 | 4 | 4 | LdSt · Ld · Mul · ALU | F |
| 10 | `F7` | 16 | 4 | 4 | LdSt · Ld · Mul · ALU | F |
| 11 | `N1` | 8 | 3 | **2** | LdSt · None · Mul | F |
| 12 | `N2` | 8 | 2 | 2 | LdSt · Ld | F |
| 13 | `N0` | 8 | 4 | **2** | LdSt · None · None · ALU | F |
| 0 | `x24` | 3 | 1 | 1 | one 24-bit core insn | 0–7 |
| 1 | `x16a` | 2 | 1 | 1 | one density insn | 8–B |
| 2 | `x16b` | 2 | 1 | 1 | one density insn | C–D |

Slot-count census: `4+5+4+4+5+4+4+4+3+2+4+1+1+1 = 46 = num_slots`. The two **5-slot** formats are
`F3` and `F11` (the `op0==0xE` class). `F5`/`F8`/`F9`/`F10` do not exist — the `Fn` numbering is
sparse.

### 1.4 Decoder self-consistency — 0 mismatch / 4096 combos

Simulating `format_decoder` over the full `(byte0.low4 × byte3)` space (4096 combos — bytes 1,2 do
not affect either decoder) and comparing each result's format length against `length_table`:

```
mismatches: 0 / 4096        formats reachable: 14/14
every format the decoder calls wide → length 16; narrow → 8; illegal → −1.
```

This is an independent, internal proof that the §1.1 length table and the §1.2 format ladder are
mutually exact: the bits the format ladder masks (`op0` + the `byte3` selector) are exactly the bits
the length table keys on, and they never disagree.

---

## 2. Per-slot opcode-class assignment — what each lane may issue

The 12 569 `Opcode_<mnem>_Slot_<slot>_encode` thunks are the **placement matrix**: one thunk per
legal `(mnemonic × slot)` pair. Grouping them by slot gives the per-slot opcode population
(from `nm libisa-core.so | rg -c`; the sum is exactly `12569`):

```
F0:   S0_LdSt 348  S1_Ld 260  S2_Mul 322  S3_ALU 564                    = 1494
F1:   S0_LdStALU 542  S1_Ld 260  S2_Mul 449  S3_ALU 558                 = 1809
F2:   S0_LdSt 213  S1_Ld 257  S2_Mul 535  S3_ALU 544                    = 1549
F3:   S0_LdSt 342  S1_Ld 256  S2_Mul 244  S3_ALU 503  S4_ALU 91         = 1436
F4:   S0_Ld 192  S1_Ld 249  S2_Mul 61  S3_ALU 251                       =  753
F6:   S0_LdSt 329  S1_Ld 266  S2_Mul 203  S3_ALU 247                    = 1045
F7:   S0_LdSt 348  S1_Ld 257  S2_Mul 521  S3_ALU 548                    = 1674
F11:  S0_Ld 93  S1_ALU 66  S2_Mul 203  S3_ALU 233  S4_ALU 91            =  686
N0:   S0_LdSt 167  S1_None 1  S2_None 1  S3_ALU 483                     =  652
N1:   S0_LdSt 176  S1_None 1  S2_Mul 381                                =  558
N2:   S0_LdSt 360  S1_Ld 220                                            =  580
x24:  Inst 319     x16a: Inst16a 4     x16b: Inst16b 10                 =  333
                                                              TOTAL  = 12569
```

The three `None` slots (`N0_S1`, `N0_S2`, `N1_S1`) host **exactly one** opcode — `nop` — and are
NOP-only filler, not real issue ports.

> **CORRECTION — `F4_S2_Mul` is `61`, not `60`/`65`.** Earlier internal drafts variously stated 60
> and 65 multiply placements for `F4`'s mul slot. The binary truth
> (`nm libisa-core.so | rg -c 'Slot_f4_s2_mul_encode'`) is **`61`**. Use 61.

These per-slot **opcode populations are permissive supersets** (Tensilica lists `vec_alu`-class ops
in nearly every slot). To turn them into a *co-issue* model we need the **class-exclusive** facts —
the ones a scheduler can rely on — derived by asking *which functional class appears in NO other
slot*. Each of the four facts below is grounded in the slot/unit and cross-checked against the
opcode-name superset.

### 2.1 The two memory lanes — `XCHAL_NUM_LOADSTORE_UNITS = 2`

`S0` and `S1` are the **two** memory lanes. The hardware fact is `XCHAL_NUM_LOADSTORE_UNITS = 2` and
`XCHAL_UNIFIED_LOADSTORE = 1` (`core-isa.h`) — a unified two-port LSU. It surfaces three ways, all
OBSERVED:

* **Slot roster:** every format carries S0 (a `ldst`/`ldstalu` unit) + S1 (a `ld` unit). No format
  has a *third* memory slot.
* **Store split:** scalar stores `s32i`/`s16i`/`s8i` and vector stores `ivp_svn*` are placed **only
  in S0** (`S0_LdSt` / `S0_LdStALU`) — checked exhaustively: `Opcode_(s32i|s16i|s8i)_Slot_*_s1_*` =
  **∅** (no scalar store in any S1). S1 is **Load-only**. So a bundle issues at most **2 memory ops,
  of which at most 1 is a store**.
* **Port interfaces:** `libcas-core.so` carries the memory-port hardware interface symbols
  duplicated `_0`/`_1` — `nx_VAddrBase_{0,1}_interface`, `nx_VectorMemDataIn512_{0,1}_interface`,
  `nx_ScalarMemDataIn32_{0,1}_interface` — exactly two port indices, the two LSU lanes. Both are
  live: PLT call-site references count **port_0 = 2256, port_1 = 623** (asymmetric ≈3.6:1, mirroring
  S0=full-LdSt / S1=Load-only).

Scatter/gather (SuperGather) ops concentrate in the memory slots: `S0_LdStALU`/`S0_LdSt` carry
9–19 gather placements each, `S1_Ld` carries 4 each; none appear outside S0/S1 (the lone outlier is
`ivp_scatterw` in the scalar `Inst` x24 slot, a non-FLIX core encoding).

### 2.2 The integer quad-MAC lane — single `S2_Mul`

The genuine integer **quad-MAC** (multiply-accumulate into the 1536-bit `wvec` accumulator) is the
`ivp_mulqa*` family. Its placement is **`S2_Mul`-exclusive**: `Opcode_ivp_mulqa*_Slot_*` outside
`*_s2_mul` = **∅**. Every format has **exactly one** `S2_Mul` slot (the `mul` functional
unit, absent only in `N0`/`N2`). There is **no replicated multiplier** (`XCHAL_HAVE_MAC16 = 0`;
`HAVE_MUL16`/`MUL32`/`MUL32_HIGH = 1`). Therefore **at most one integer quad-MAC issues per bundle.**

> **GOTCHA — do not derive this from an `ivp_mul*` name grep.** `Opcode_ivp_mul.*` also matches
> `ivp_mulsgnnx16` (multiply-**sign**, a sign-extract ALU op), which is placed in S3_ALU / S4_ALU /
> S0_LdStALU across most formats. The name "mul" is not the unit. The S2_Mul-exclusive fact holds
> for the *quad-MAC accumulate* class (`ivp_mulqa*`), grounded in the slot/unit, **not** for
> everything spelled `…mul…`.

### 2.3 The FP fused-multiply-add lane — `S3_ALU` *primarily*, but NOT exclusively

The single/half-precision vector 2× FMACs (`XCHAL_HAVE_VISION_SP_VFPU_2XFMAC = 1`,
`HP_VFPU_2XFMAC = 1`; `DP_VFPU = 0` — Q7-specific) and the scalar `MADD.S`/`MSUB.S` FP fused-MAC are
the FP-FMA class. They live **predominantly** in the `S3_ALU` lane — `add.s`/`sub.s`/`madd.s` in
S3_ALU of every wide format — and the FP-divide-step (`divn.{s,h}`, `ivp_divnn_2xf32`) likewise.

> **CORRECTION — FP-FMA is NOT `S3_ALU`-exclusive; `madd.s`/`msub.s` are also placed in `S2_Mul`.**
> A prior internal claim asserted "MADD/MSUB live in S3_ALU, NOT S2_Mul — S2_Mul has zero MADD." The
> binary refutes this: `nm libisa-core.so | rg 'Opcode_madd_s_Slot'` lists
> `…Slot_f2_s2_mul`, `…Slot_f7_s2_mul`, `…Slot_n1_s2_mul` alongside the S3_ALU placements (and
> `msub_s` identically). On `N1` (which has **no** S3_ALU slot) the only FP-FMA placement *is* in
> `S2_Mul`. So FP-FMA and the integer quad-MAC are **not** disjoint by slot. The two lanes (`S2_Mul`,
> `S3_ALU`) are distinct **issue ports** with **overlapping** opcode eligibility, not class-exclusive
> partitions. `[HIGH/OBSERVED — corrects the earlier exclusivity claim]`

The sound, binary-defensible bound is therefore the **slot-count / port bound**, *not* an
exclusivity claim: a format with both `S2_Mul` and `S3_ALU` (F0/F1/F2/F3/F6/F7/F11) can co-issue
**one op from the mul port + one op from the (S3) ALU port** = at most **2 multiply-class ops/cycle**
of (typically) different datapaths — one integer quad-MAC (S2) and one FP-FMA/FP-ALU (S3). On
formats that place `madd.s` in S2, the FP-FMA can also ride the mul port instead.

### 2.4 The vector / FP ALU lanes — `S3_ALU` (+ `S4_ALU`, + `S1_ALU` on F11)

`S3_ALU` is the universal ALU lane (483 ops even on the narrow `N0`). `F3` and `F11` add `S4_ALU` →
**two** dedicated ALU lanes; `F11` additionally makes its S1 an ALU lane (`S1_ALU`, 66 placements) →
**three** ALU-capable lanes (S1 + S3 + S4). `vec_alu`-class ops appear permissively in the mem/mul
slots too, but the *dedicated* vector/FP-ALU issue ports are S3/S4 (+S1 on F11).

> **NOTE — the unit *name* is not a strict store-capability gate; `F4` is a nuanced "dual-load".**
> `F4`'s S0 is named `Ld`, yet it carries **3 scalar stores** (`s32i`/`s16i`/`s8i`) — confirmed:
> `Opcode_s32i_Slot_f4_s0_ld_encode` exists. But `F4`'s S0 carries **no vector store** (`ivp_svn*` in
> `f4_s0_ld` = **0**). So `F4` is dual-**load** for the *vector* datapath (no vector store lane) while
> still admitting the three scalar integer stores. `F11`'s S0 (also named `Ld`) carries **zero**
> stores of either kind — `F11` is the genuinely no-store wide format. Store capability is set
> per-mnemonic via `opcodes[].flags` bit 5, not by the slot name.

---

## 3. The co-issue legality table + max issue width

### 3.1 Max issue width per format

| format | len | slots | real width | lane composition |
|---|:--:|:--:|:--:|---|
| `F3` | 16 | 5 | **5** | LdSt + Ld + Mul + ALU + ALU |
| `F11` | 16 | 5 | **5** | Ld + ALU(S1) + Mul + ALU + ALU |
| `F0` | 16 | 4 | 4 | LdSt + Ld + Mul + ALU |
| `F1` | 16 | 4 | 4 | LdStALU + Ld + Mul + ALU |
| `F2` | 16 | 4 | 4 | LdSt + Ld + Mul + ALU |
| `F4` | 16 | 4 | 4 | Ld + Ld + Mul + ALU (dual-load) |
| `F6` | 16 | 4 | 4 | LdSt + Ld + Mul + ALU |
| `F7` | 16 | 4 | 4 | LdSt + Ld + Mul + ALU |
| `N0` | 8 | 4 | **2** | LdSt + (None) + (None) + ALU |
| `N1` | 8 | 3 | **2** | LdSt + (None) + Mul |
| `N2` | 8 | 2 | 2 | LdSt + Ld |
| `x24` | 3 | 1 | 1 | one core insn |
| `x16a/b` | 2 | 1 | 1 | one density insn |

**Peak issue width = 5 ops/bundle** (`F3`, `F11`). The `N0`/`N1` `None` slots are NOP-only padding
that round the 64-bit narrow bundle but issue no real op → real width 2.

### 3.2 The co-issue legality table

`Y` = the format has a slot that legally issues that functional class; `·` = no such slot. `mem` = a
true Load/Store or scatter/gather op (escaped `\|` are literal column borders):

| format | mem (S0) | mem (S1) | mul-port (S2) | ALU/FP-FMA (S3) | ALU (S4) | peak |
|---|---|---|:--:|:--:|:--:|:--:|
| `F0` | LdSt | Ld | Y | Y | · | 4 |
| `F1` | LdStALU | Ld | Y | Y | · | 4 |
| `F2` | LdSt | Ld | Y | Y | · | 4 |
| `F3` | LdSt | Ld | Y | Y | **Y** | **5** |
| `F4` | Ld\* | Ld | Y | Y | · | 4 |
| `F6` | LdSt | Ld | Y | Y | · | 4 |
| `F7` | LdSt | Ld | Y | Y | · | 4 |
| `F11` | Ld | **ALU (S1)** | Y | Y | **Y** | **5** |
| `N0` | LdSt | — | · | Y | · | 2 |
| `N1` | LdSt | — | Y | · | · | 2 |
| `N2` | LdSt | Ld | · | · | · | 2 |

\* `F4` S0 takes 3 scalar stores but no vector store (§2.4).

**The co-issue bounds that are provable from the shipped tables:**

* **Issue width ≤ the format's real slot count** — `F3`/`F11` → 5; `F0`–`F7` → 4; `N0`/`N1`/`N2` → 2;
  `x24`/`x16` → 1. *(Hard, from the FORMAT slot roster.)*
* **Memory ops ≤ 2/bundle** (S0 + S1), of which **≤ 1 STORE** (only S0/S0_LdStALU carry stores; S1 is
  Load-only). `F4` = 2 loads + ≤1 scalar store. `N0`/`N1` = 1 memory op. This is the
  `XCHAL_NUM_LOADSTORE_UNITS = 2` unified-LSU bound.
* **Mul-port op ≤ 1/bundle** (the single `S2_Mul` lane). `N0`/`N2` = 0.
* **The densest compute bundle** is `F3`: 2 mem + 1 mul-port + 2 ALU; or `F11`: 1 mem + 1 ALU(S1) + 1
  mul-port + 2 ALU. The headline MAC ceiling is therefore **≤ 1 integer quad-MAC (S2) + ≤ 1 FP-FMA
  (S3) per bundle** — two MAC-class ops of different datapaths — **not** "up to 3 multiply-class," a
  number that was an artifact of the permissive opcode supersets (§2.2–§2.3).

> **CORRECTION (carried) — "F4/F6 admit 3 multiply-class ops" is RETRACTED.** That figure counted
> every opcode in the permissive S0/S1/S2/S3 supersets whose *name* touched a multiply/FMA/divide
> operand and found up to three "mul-capable" slots. Re-grounded to the slot/functional-unit, each
> format has exactly **one** `S2_Mul` lane; the FP-FMA/FP-ALU rides `S3_ALU` (and, on F2/F7/N1, may
> also ride S2). The honest ceiling is **1 (S2) + 1 (S3)**. Per-op latencies are unchanged
> ([Pipeline Timing](pipeline-timing.md)).

### 3.3 Device validation — 1479 real bundles, byte-classified

The shipped GPSIMD device library `libneuroncustomop.a` (`custom_op/neuron/`, elf32-xtensa-le, 30
members) was disassembled with the device-native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
`XTENSA_SYSTEM=…/ncore2gp/config`) and every FLIX `{ … }` bundle counted and classified by the §1
decoders. Reproduced:

* **1479 / 1479** FLIX bundles decoded (`rg -c '{.*}'` = 1479; per-member Σ = 1094 wrapper_api + 122
  start_exit + 110 data_transfer + 66 translation + 43 allocator + 21 stack_switch + 20
  NeuronAllocator + 3 TensorTcmAccessor = 1479). They are **26.8 %** of the 5527 decoded instruction
  lines — the rest are single-issue scalar/density words.
* **Bundle-width histogram (this scalar-dominated library):** 2-op = **721**, 3-op = **472**, 4-op =
  **286**; avg **2.71** ops/bundle; max 4. *(The 5-issue `F3`/`F11` bundles and the multiply-rich
  vector bundles need a vision-SIMD body absent from this management library; their structure is
  validated by §1.4 + the `slots[]` roster, not by a device sample here.)*
* **Format marker:** 100 % of the 1479 bundles have first-byte low nibble ∈ `{e,f}` — the op0 FLIX
  length marker — with 0 unclassified.

> **GOTCHA (MED) — the device cross-check carries a TIE-checksum-mismatch warning.** Every member
> emits `warning: Xtensa configuration may be incompatible (TIE checksum does not match)`: the
> shipped `libneuroncustomop.a` was built against a config-version slightly skewed from the registered
> `ncore2gp`. The **base-ISA FLIX framing decode** (bundle braces, slot count, op0/length) is
> unaffected and reproduces exactly (1479); but custom IVP/TIE opcode *interiors* inside a bundle may
> not all resolve in this checkout. Treat the 1479 framing count as `[HIGH/OBSERVED]`; treat any
> claim about a *specific custom opcode's* presence in a device bundle as `[MED]` pending a
> checksum-matched config. `[MED/OBSERVED]`

---

## 4. The per-port reservation model — present, not empty

A long-standing residual stated the per-port single-issue reservation (the rule beyond "≤ slot
count" — e.g. *can two ALU ops and a mov truly all retire this cycle*) was unrecoverable because the
`MODULE_SCHEDULE` reservation bodies ship **empty**. That is **true of the `libisa-core.so` TIE
tables** (the encoding DB carries no schedule) but **false of `libcas-core.so`** — the cycle model
ships the reservation as fully populated function bodies:

| family (`nm libcas-core.so`) | count | role |
|---|--:|---|
| `*_issue` | **2149** | per-`(format,slot,unit,opcode)` issue/reservation bodies (e.g. `F0_F0_S0_LdSt_4_inst_ADD_A_issue`) |
| `*_stage<N>` | **160 393** | per-`(format,slot,opcode,stage)` pipeline-stage bodies, stages **0–15** |
| `*_stall` | **1746** | per-op stall/hazard bodies |
| dispatch tables | — | `dll_get_issue_functions` (T, `0x17aa340`) → `slot_issue_functions` (`0x227f2c0`); `slot_semantic_functions`; `slot_stall_functions` |

The stage functions tile stages 0–15 densely (≈13k at stage 0 down to ≈4.9k at stage 15); a scalar
`L32I` carries `stage0..stage5+`; a vector `IVP_LVN_2X16S_I` carries through **`stage10`/`stage11`**
— exactly the deep vector memory-port pipe of §5. These bodies call the `nx_*_interface` port
signals via PLT (`call nx_VectorMemDataIn512_0_interface@plt` from stage bodies).

> **CORRECTION — "the `MODULE_SCHEDULE` is structurally empty → only a `1+1` structural bound" is
> over-pessimistic.** The *named* symbol `module_schedule` exists in neither DLL, and the
> `libisa-core.so` encoding DB indeed carries no reservation. But the **cycle/reservation model is
> fully present in `libcas-core.so`** as 2149 issue + 160 393 stage + 1746 stall function bodies. The
> structural `1+1`/slot-count co-issue ceiling (§3.2) remains **sound and authoritative for issue
> legality**; the finer per-port single-issue *timing* is **recoverable in principle** from these
> bodies (it is OBSERVED-present, not walled), it is simply not reduced to a byte-readable literal
> here. State it as: *co-issue legality* = `[HIGH/OBSERVED]` (slot rosters + 2-LSU + 1-Mul + issue
> functions); *exact per-port stall cycle counts* = a **task**, not a wall (the bodies exist). `[HIGH
> on the bodies' presence; the cycle-count extraction is deferred, not impossible]`

> **NOTE — `libcas-core.so` has no DWARF; the model is symbol-table-based.** `readelf -SW` shows
> **zero** `.debug_*` sections; the DLL is "unstripped" only in that it carries a full `.symtab`
> (177 936 symbols, 166 024 text). All co-issue / stage / port facts above are read from **symbol
> names** and PLT relocations, not DWARF type/line info.

---

## 5. Cache / local-memory / system-RAM timing

### 5.1 I-cache geometry (`core-isa.h`)

| field | value | macro |
|---|---|---|
| size | 16 384 B (16 KB) | `XCHAL_ICACHE_SIZE` |
| line | 64 B (LINEWIDTH 6) | `XCHAL_ICACHE_LINESIZE` / `_LINEWIDTH` |
| ways | 4 (WAYS_LOG2 2) | `XCHAL_ICACHE_WAYS` / `_WAYS_LOG2` |
| sets/way | 64 (SETWIDTH 6) → `16384/64/4 = 64 = 2^6` | `XCHAL_ICACHE_SETWIDTH` |
| access granule | 32 B/cycle | `XCHAL_ICACHE_ACCESS_SIZE` |
| ECC width | 4 (parity off) | `XCHAL_ICACHE_ECC_WIDTH` |
| line-lock / dyn-ways | none | `XCHAL_ICACHE_LINE_LOCKABLE 0` |
| fetch width | 32 B (256-bit) | `XCHAL_INST_FETCH_WIDTH` |
| prefetch | present, **8 entries**, 1 castout line | `XCHAL_HAVE_PREFETCH 1`, `_PREFETCH_ENTRIES 8` |

> **I-cache miss penalty `[MED/INFERRED]` — structure pinned, cycle count unrecovered.** The miss is a
> **front-end** stall, not a register-result latency, so it does not appear in the per-op stage model
> (the `stage*` bodies model issue→retire of an *already-fetched* bundle, not the I-fetch). The
> structural penalty is the AXI refill of one **64-B line at the 32-B access granule = 2 AXI 32-B
> beats** into the fill buffer, plus the AXI/PIF round-trip (§5.4). A hit costs **0 extra cycles** (a
> 32-B fetch feeds the 256-bit instbuf each cycle). The 8-entry prefetch hides sequential-line
> misses. The exact miss cycle count is **SoC-fabric-dependent and not pinned by the core config**.
> `[MED]`

### 5.2 Local memory — no D-cache, 4-bank DataRAM (`core-isa.h` / `system.h` / `memmap.xmm`)

| region | base (V=P) | size | banks | ECC | IDMA |
|---|---|---|:--:|:--:|:--:|
| IRAM0 | `0x00000000` | 65 536 B (64 KB) | — | 0 | 0 |
| DataRAM0 | `0x00080000` | 65 536 B (64 KB) | **4** | 0 | 1 |

`XCHAL_DCACHE_SIZE = 0` — **no D-cache**; the data side is local DataRAM + an **8-entry write
buffer** (`XCHAL_NUM_WRITEBUFFER_ENTRIES 8`). `XCHAL_DATA_WIDTH = 64 B` (512-bit data bus),
`XCHAL_DATA_PIPE_DELAY = 1`, `XCHAL_HAVE_IMEM_LOADSTORE = 1`, `XCHAL_HAVE_MEM_ECC_PARITY = 0` (local
RAM unprotected; the AXI master has ECC — `XCHAL_HAVE_AXI_ECC = 1`). With no D-cache, **every data
access is a fixed-latency local-RAM or AXI transaction — there is no data-side miss/refill.** The 4
DataRAM banks supply the 512-bit bus its read/write bandwidth.

> **DataRAM bank-conflict cycle count `[MED]` — structure pinned, count unrecovered.** The 4-bank
> DataRAM is the structural basis for bank-conflict arbitration on a 512-bit access that straddles
> banks (and for gather-scatter accesses spraying across banks). The conflict *arbitration* is **not
> a register-latency** and is not surfaced as a byte-readable literal; the per-op `stage*` bodies in
> `libcas-core.so` assume the common-case conflict-free folded latency (vector @9/10, §5.3). The
> exact extra cycles on an N-way bank conflict are inside those stage bodies / the SoC arbiter, not
> recovered here. `[MED]`

### 5.3 The memory-port pipeline stages (`libcas-core.so` interface signals + stage bodies)

The d-side memory-port stage model, grounded in the `nx_*_interface` port signals (§2.1) and the
per-op `stage<N>` bodies (§4):

**Scalar** (`xt_xtensa`, ~7-stage pipe; stages below in the **ISS `A1/B3/E4/M5/W6`** convention the
`stage<N>` bodies use — TIE-root is `r0/e3/m4/w6`, ISS E/M − 1, W unchanged, see
[pipeline-timing §2](pipeline-timing.md)):

```
L32I / L16UI / L32R / L32EX : AGU base @1 (nx_VAddrBase),
                              data return @5  (nx_ScalarMemDataIn32),  result reg art @5.
                              => dependent ALU reads @4 -> 1-cycle load-use bubble.
S32I / S32EX               : store data art @5,  egress @5 (nx_ScalarMemDataOut32) -> write buffer.
L32EX / S32EX              : + XTSYNC barrier @6 (exclusive-monitor sync; the my_XTSYNC_* funcs).
```

**Vector** (`xt_ivp32`, deep vector pipe — `IVP_LVN_*` carries `stage0..stage11`):

```
IVP_LVN_* (load)  : addr base @1 (nx_VAddrBase), MemDataIs256/512Bits tag @1,
                    THE STAGE-9 MEMORY PORT @9  (nx_VectorMemDataIn512),
                    architectural vec-reg result @10  (the stage-10 vec-dest / regfile read port).
                    => a dependent vector op in the NEXT bundle forwards with 0 bubble;
                       same-cycle is a true dependency.
IVP_SVN_* (store) : store source read @10, 512-bit egress @11 (nx_VectorMemDataOut512).
post-increment _XP/_IP variants update the AR base @1 (cheap scalar-lane add).
scatter/gather    : GSVAddrOffset @10 (nx_GSVAddrOffset_0), result @10/11, in mem slots S0/S1.
```

The `_0`/`_1` duplication of every port signal is the **two LSU lanes**; the `MemDataIs{256,512}Bits`
tag carries the access width.

### 5.4 How a memory access threads the pipeline + the SBUF/AXI path

* **Local DataRAM** (4 banks, 512-bit bus) is the on-core data side; a vector L/S returns at the
  stage-9 port with the §5.2 single-cycle SRAM access folded in. No D-cache ⇒ no miss/refill — fixed
  latency. `[HIGH]`
* **Shared SBUF** is **not** a dedicated compute port: a GPSIMD load/store that targets SBUF is an
  AXI/ACE-Lite master transaction through the `axi2sram` bridge, arbitrating as the DMA slot behind
  the SBUF arbiter (the `LoadStoreAXIDecError`/`LoadStoreAXISlvError`/`LoadStoreSGAccError` exception
  handlers in `libcas-core.so` confirm the AXI-master fault path). The on-core `stage*` model covers
  the core pipe stages; the **SBUF round-trip + arbiter stall** layers on top and is
  SoC-fabric-dependent. `[HIGH config; the AXI-latency add is MED.]`
* **System RAM** (PIF/AXI, **1 GB @ `0x00100000`**, `XSHAL_RAM_SIZE = 0x40000000`; IOBLOCK cached
  `0x70000000` / bypass `0x90000000`; RAM-bypass `0xA0000000`; SimIO `0xC0000000`): outbound AXI
  master with ECC (`XCHAL_HAVE_AXI_ECC = 1`, `XCHAL_HAVE_PIF_WR_RESP = 1`). The 8-entry write buffer
  decouples stores — a store retires into the buffer at @5 (scalar) / @11 (vector) and drains
  asynchronously. AXI/PIF latency is the longest data path. `[HIGH config; cycle latency MED.]`

### 5.5 Consolidated memory-latency table

`*` = SoC-fabric-dependent cycle count (structure pinned, count MED):

| access | result / return stage | load-use | notes |
|---|---|---|---|
| I-cache hit (fetch) | feeds instbuf/cycle | 0 | 32 B/cyc into 256-bit instbuf |
| I-cache miss (refill) | `*` | `*` | 64-B line = 2 × 32-B AXI beats + fabric RTT `[MED]` |
| scalar load (`L32I`/`L16UI`) | @5 (`ScalarMemDataIn32`) | 1 | M-stage data return |
| scalar store (`S32I`) | egress @5 | — | into 8-entry write buffer |
| exclusive `L32EX`/`S32EX` | @5 + XTSYNC @6 | 1 | monitor-sync barrier @6 |
| vector load (`IVP_LVN`) | port @9, result @10 | 0-cyc fwd | stage-10 vec read-port bypass to next bundle |
| vector store (`IVP_SVN`) | src @10, egress @11 | — | 512-bit store egress |
| scatter/gather (SuperGather) | `GSVAddrOffset` @10, result @10/11 | 0–1 | address-vector op, mem slots S0/S1 |
| local DataRAM (4-bank) | folded (scalar @5 / vec @9) | as above | no D-cache; fixed-latency SRAM; bank-conflict `*` `[MED]` |
| SBUF via AXI | on-core stage + AXI `*` | + arbiter | DMA slot behind SBUF arbiter `[MED]` |
| system RAM (PIF/AXI, 1 GB) | AXI RTT `*` | + write-buf | 8-entry write buffer decouples stores `[MED]` |

`[HIGH config; `*` counts MED — SoC-fabric / inside the stage bodies.]`

---

## 6. Adversarial self-verification — the 5 strongest claims, re-challenged

1. **Peak issue width = 5 (F3, F11).** `format_decoder` + `slots[]` give `F3`/`F11` five slots each;
   the slot-count census sums to 46. **Re-challenge:** the device library tops out at 4-op bundles
   (286 of 1479) — does a 5-issue bundle exist? *Verdict:* the *structure* permits 5 (the rosters and
   the `slot_issue_functions` for `F3_S4`/`F11_S4` are present); the management library simply emits
   no vision-SIMD 5-issue bundle. Width-5 is `[HIGH/OBSERVED]` from structure; the device sample
   confirms ≤4 in *this* lib. **Stands (structure HIGH).**

2. **Memory ops ≤ 2/bundle, ≤ 1 store.** **Re-challenge:** could a third memory op hide in an ALU
   slot? *Verdict:* `XCHAL_NUM_LOADSTORE_UNITS = 2`; only S0/S1 carry ld/st/gather; `_0`/`_1` port
   interfaces in `libcas-core.so` = exactly two; scalar stores in S1 = ∅. **CONFIRMED three ways.**

3. **Integer quad-MAC ≤ 1/bundle.** **Re-challenge:** the `ivp_mul*` name grep finds "mul" in ALU
   slots. *Verdict:* that grep catches `ivp_mulsgnnx16` (an ALU sign op). The genuine quad-MAC
   `ivp_mulqa*` is **S2_Mul-exclusive** (0 outside), one S2 slot/format, `HAVE_MAC16 = 0`. **CONFIRMED
   (grounded in slot/unit, not name).**

4. **FP-FMA lane = S3_ALU exclusively.** **Re-challenge:** `nm | rg 'Opcode_madd_s_Slot'`. *Verdict:*
   **REFUTED** — `madd_s`/`msub_s` are placed in `S2_Mul` of F2/F7/N1 too (and N1 has *only* an S2
   placement). FP-FMA is **not** S3-exclusive. **CORRECTED in §2.3** — the bound is the slot-count
   bound (1×S2 + 1×S3), not exclusivity.

5. **Empty `MODULE_SCHEDULE` → only a structural ceiling, finer reservation walled.** **Re-challenge:**
   `nm libcas-core.so | rg -c '_issue$|_stall$|_stage[0-9]'`. *Verdict:* the *named* symbol is absent
   and the libisa encoding DB has no schedule, but `libcas-core.so` ships **2149 issue + 160 393 stage
   + 1746 stall** function bodies (stages 0–15) via `slot_issue_functions`/`slot_stall_functions`.
   **CORRECTED in §4** — the reservation model is *present*; only the reduction of those bodies to
   exact stall cycle counts is deferred, not impossible.

> **The residual, stated honestly.** What is **HIGH/OBSERVED**: the 14-format/46-slot decode tree
> (0 mismatch / 4096), the per-slot opcode census (sums to 12 569), the co-issue legality table, the
> max-issue-width 5, the 2-LSU / 1-Mul structural ceiling, the I-cache/IRAM/DataRAM/AXI geometry, the
> stage-9/10/11 vector memory port (interface signals + stage bodies), and the 1479-bundle device
> framing. What stays **MED**: the **exact cycle counts** for DataRAM bank conflicts, the I-cache miss
> penalty, and the SBUF-AXI round-trip — all SoC-fabric-dependent or buried inside the ~160k stage
> bodies, *structure pinned, counts unrecovered*. These are flagged inline, never invented.

---

## 7. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| `format_decoder` ladder; `length_decoder` + 256-cell table; 0 mismatch / 4096 | `[HIGH/OBSERVED]` | `libisa-core.so` `0x3b5970`/`0x3b5a50`/`0x3d4100` disasm + sim |
| 14 formats / 46 slots; per-slot opcode census sums to 12 569; `F4_S2_Mul=61` | `[HIGH/OBSERVED]` | count getters `0xe`/`0x2e`/`0x3119` + `nm` placement census |
| 2 memory lanes (`NUM_LOADSTORE_UNITS=2`); ≤2 mem, ≤1 store/bundle | `[HIGH/OBSERVED]` | `core-isa.h` + S0/S1 store split + `nx_*_interface _0/_1` |
| integer quad-MAC ≤1 (S2_Mul-exclusive `ivp_mulqa*`); `HAVE_MAC16=0` | `[HIGH/OBSERVED]` | slot/unit census + `nm` |
| FP-FMA is S3 *primarily* but **also S2** (madd.s in F2/F7/N1) | `[HIGH/OBSERVED]` | `nm 'Opcode_madd_s_Slot'` — corrects exclusivity claim |
| max issue width 5 (F3/F11); peak compute = 2 mem + 1 S2 + 2 ALU | `[HIGH/OBSERVED]` | format slot rosters |
| per-port reservation present (2149 issue + 160 393 stage + 1746 stall) | `[HIGH/OBSERVED]` | `libcas-core.so` symtab + dispatch tables |
| I-cache 16K/64B/4-way/64-set; DataRAM 64K/4-bank; no D-cache; 8-WB; 1 GB SysRAM | `[HIGH/OBSERVED]` | `core-isa.h`/`system.h`/`memmap.xmm` |
| vector mem port @9, vec-dest @10, store egress @11; scalar load @5 + XTSYNC @6 | `[HIGH/OBSERVED]` | `nx_VectorMemDataIn512`/`VAddrBase`/`stage0..11` bodies |
| 1479 device FLIX bundles; width hist 721/472/286; 100% op0∈{e,f} | `[HIGH/OBSERVED]`; interiors `[MED]` (TIE-checksum skew) | `xtensa-elf-objdump` on `libneuroncustomop.a` |
| DataRAM bank-conflict / I-cache-miss / SBUF-AXI RTT **cycle counts** | `[MED]` (structure HIGH, counts unrecovered) | not byte-readable; SoC-fabric / inside stage bodies |
| `libcas-core.so` has **no DWARF** — symbol-table model only | `[HIGH/OBSERVED]` | `readelf -SW` (0 `.debug_*`); 177 936 symtab entries |

---

## 8. Cross-references

* [The FLIX VLIW Encoding](../isa/core/flix-encoding.md) — the 14-format / 46-slot byte-level roster,
  the per-slot bit offsets and gather thunks, and the encode-thunk ABI this page's co-issue table
  reasons over (the *encoding*; this page is the *co-issue + timing*).
* [ISA Coverage & the 1534/1607/12642 Tally](../isa/core/coverage-tally.md) — the certified
  `1534/12569` placement denominator and the `1+1` co-issue framing this page sharpens.
* [Pipeline Timing Model](pipeline-timing.md) — the per-op result latencies (int-MAC, FP-FMA, the
  stage model) whose *co-issue* and *memory-port* faces this page supplies.
* [Microarchitecture Synthesis](microarch-synthesis.md) — the consolidating capstone that unifies
  this page's reservation-bodies-present finding (§4) and the `1×S2 + 1×S3` MAC-class ceiling (§3.2)
  with the sibling framings (synthesis §2.2/§2.4 + §8 rows 1, 4).
* [Local-Memory / System-Bus / LSU Model](lsu-memory.md) — the LSU port detail, the SBUF/AXI bridge,
  and the DataRAM bank arbitration whose conflict cycle counts are the MED residual flagged here.
* [The Confidence & Walls Model](../reference/confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[MED/INFERRED]`, and "structure pinned, count unrecovered" mean.
