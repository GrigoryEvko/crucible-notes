# ISA Batch 20 — fp16 Convert (hp_cvt)

This is the per-instruction reference for the **half-precision (fp16) convert slice** of the
Vision-Q7 *Cairo* (`ncore2gp`) ISA: every opcode that produces an **fp16** result by converting from
an integer or by rounding an fp16 to an integral fp16 value. It is the fp16-axis mirror of the
fp32-side [B13 sp-cvt](b13-sp-cvt.md): the binary itself names the semantic group **`ivpep_sem_hp_cvt`**
(the literal `opnd_ivpep_sem_hp_cvt_*` operand strings live in `libisa-core.so` — `strings -a | rg
sem_hp_cvt`, this pass — and are where the page title comes from). The group splits into exactly two
converter directions over one multiplexed datapath: **`int16 → fp16`** with a 4-bit Q-format scale
(`float16`/`ufloat16`), and the five **fp16 round-to-integral** ops that keep the result in fp16
(`fitrunc`/`ficeil`/`fifloor`/`firint`/`firound`). It owns **21 shipped mnemonics**, summing to **126
placements** (21 × 6).

The fp16↔fp32 *width* converts (the **narrow** `cvtf16f32`/`cvtf16n_2xf32`, fp32→fp16) are physically
the routing hub for every non-fp16/non-fp32 datatype — bf16, fp8 (e3m4/e4m3/e5m2) and fp4 (e2m1)
have **no** native convert opcode and reach fp16 only through this fp32 hub plus a firmware rebias
(§6.4). The narrow ops are *named* `cvtf16*` ("produce fp16") so the classifier routes them to B20 by
name root, but their roster home and the round-trip certificate are split with B13 (§6); this page
states the narrow's overflow/underflow rule because a reimplementer of *either* page must match it.

This page is the **value-semantics-and-encoding** companion to the control-view
[fp sub-ISA page](../core/fp-sub-isa.md): that page owns the FCR/FSR round-mode/exception *state*
(the architectural `RoundMode`, the `away`-only fifth code `firound` reaches, the sticky flags, the
two-level RNE-vs-RZ rounding model); **this** page owns *what each fp16-convert opcode computes,
bit-exact, and how it is encoded into a slot*. Every value claim below was **proven by execution** —
computed by calling the matching `module__xdref_` leaf in `libfiss-base.so` live via `ctypes`
(license-free), the binary acting as the arbiter of its own arithmetic. `[HIGH/OBSERVED·exec]`
throughout where a value fact is stated, per the [confidence model](../../reference/confidence-model.md).

> **NOTE — the two binaries and the address arithmetic this page uses.** Encoding side:
> `libisa-core.so` (sha256 `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`,
> 9,690,712 B, not stripped; re-confirmed this pass). `.text` (`0x312c10`) and `.rodata`
> (`0x3b6e40`) are **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) and
> `.data` (VMA `0x764040` ↔ file `0x564040`) carry a **`0x200000`** per-binary delta
> (`readelf -SW`, re-read this pass — *not* libtpu's `0x400000`), so every iclass `args`/`stateArgs`
> table read below subtracts `0x200000` from its symbol VMA. Value side: `libfiss-base.so`
> (sha256 `260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94`, 12,330,016 B); its
> `module__xdref_*` leaves are `.text`, VMA == file. Timing side: `libcas-core.so`
> (sha256 `7f1d…041a`, 45,878,080 B), the generated cycle-accurate ISS. All three are in
> `extracted/` (gitignored; reach with an absolute path or `fd --no-ignore`). `[HIGH/OBSERVED]`

---

## 1. Scope, datapath, and key facts

Every fp16-convert op reads/writes the **`vec`** file (idx 2, 512 b × 32; fp16 occupies the full
**32 lanes** of 16 b, 1-5-10 bias 15) and is placed in the **`S3_ALU`** slot — the *same* slot family
the [B13 fp32 converts](b13-sp-cvt.md) and the fp Vector-ALU slice use. `nm` shows every placement at
`Slot_{f0,f1,f2,f3,f7}_s3_alu` + `Slot_n0_s3_alu` (this pass). The `T` (predicated) forms add a
third input from the **`vbool`** file (idx 3, 64 b × 16) and `OperandSem_opnd_sem_vbool_{encode,decode}`
bind that operand class directly (this pass). The round-honoring forms thread the FCR `RoundMode` and
post the FSR sticky flags; the operand and state model is read from the iclass `args`/`stateArgs`
tables (§3.1, §4).

| Fact | Value | Source |
|---|---|---|
| Data file | `vec` idx 2, **512 b × 32**; fp16 = **32 lanes** (1-5-10, bias 15) | [register-files §3](../core/register-files.md) |
| Predicate file (`T` forms) | `vbool` idx 3, **64 b × 16** (per-lane mask) | `OperandSem_opnd_sem_vbool_*` |
| Canonical encode slot | **`S3_ALU`** — 5 wide (`F0/F1/F2/F3/F7`, 16 B) + 1 narrow (`N0`, 8 B) | `nm \| rg Slot_.*_s3_alu` |
| Semantic group | **`ivpep_sem_hp_cvt`** | `opnd_ivpep_sem_hp_cvt_*` strings |
| Mnemonics this batch | **21** | per-mnemonic `nm` census (§7) |
| Placements this batch | **126** (21 × 6) | §7 |
| Q-format scale operand | **`i_imm4`** (4-bit, 0..15) on `float16`/`ufloat16` | `Field_fld_ivpep_sem_hp_cvt_i_imm4_*` |
| Round-honoring ops | `firint` (FCR mode), int→fp16 (`float16`/`ufloat16`, implicit) | iclass `stateArgs` carry `RoundMode` |
| Fixed-mode ops | `firound` (away-from-0), `ficeil` (+∞), `fifloor` (−∞), `fitrunc` (→0) | iclass `stateArgs` carry **no** `RoundMode` |
| FSR-raising | Inexact (rounding forms); Invalid (FI on NaN); Overflow (`ufloat16` only) | iclass `stateArgs` + live flag probe |
| Writeback stage | **13** (all 21); vec read @10; structural ports @14 | `libcas-core.so` ISS `_issue` (§9.1) |
| Value oracle | `libfiss-base.so` `module__xdref_*` leaves, ctypes | [confidence §6.1](../../reference/confidence-model.md) |

> **GOTCHA — `hp_cvt` is the *output*-fp16 family by name root, and it deliberately *excludes* the
> fp16→int trunc.** Two cuts a reimplementer must get right:
> (1) the classifier key is the **mnemonic root, first-match-wins** — `float16`/`ficeilnxf16`
> (root says *produce fp16*) are B20; their fp32 cousins `floatn_2x32`/`ficeiln_2xf32` are
> [B13](b13-sp-cvt.md). (2) `trunc16nxf16`/`utrunc16nxf16` (fp16→**int**) *do* exist in
> `libisa-core.so` (`nm` shows them this pass) but they are **not** in this batch's 21 — fp→int trunc
> is grouped with the lookup family (`ivpep_sem_hp_lookup`), discards the fractional bits
> unconditionally (always round-toward-zero, ignoring the FCR), and is owned by
> [B14 hp-lookup](b14-hp-lookup.md). B20 is exactly the **int→fp16** and **fp16-round-to-integral**
> half. `[HIGH/OBSERVED]`

---

## 2. The roster

Every fp16-convert mnemonic this batch owns, with its encoding read at the **canonical `F1_S3_ALU`
slot** (the `movl` immediate in the `Opcode_<mnem>_Slot_f1_s3_alu_encode` thunk — body
`movl $imm,(%rdi); ret`, byte-exact this pass) and its operand model read from the iclass
`args`/`stateArgs` tables. The `t` mnemonics are the **predicated** forms (a separate iclass, `*T`,
with a `vbr:vbool` guard input); their `word0` is a *different* selector, not a bit-flip of the base
form. The 21 = **7 base families** (`fitrunc`, `ficeil`, `fifloor`, `firint`, `firound`, `float16`,
`ufloat16`) × **3 forms** (`.H` scalar, `nxf16`/`nx16` vector, `T` predicated).

> **GOTCHA — the opcode-selector immediate is SLOT-LOCAL, not a global opcode number** (the same rule
> [B13 §2](b13-sp-cvt.md#2-the-roster) and B02 found). `ivp_fitruncnxf16` carries `0x2e1000a1` in
> `F1_S3_ALU` but a *different* selector in every other format/slot — this page tabulates one
> canonical slot so the selectors are comparable; a reimplementer's assembler reads the placement it
> actually targets from `Opcode_<mnem>_Slot_<that-slot>_encode`. The §6 worked examples are read at the
> default **`N0_S3_ALU`** narrow slot (the 8-byte bundle the device emits), whose selectors differ
> again — both are reproduced and reconciled by XOR-diff (§6.2). `[HIGH/OBSERVED]`

### 2.1 int16 → fp16 with a Q-format scale (`float16`/`ufloat16`)

| mnemonic | fmt·slot | opcode-sel imm (F1_S3) | vec lane in→out | bytes | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `float16.h` | F1·S3_ALU | `0x2f000400` | int16 → fp16 (scalar) | 16/8 | `fp16(int16) · 2^(−imm4)` (signed Q-format) | H/OBS·exec |
| `ivp_float16nx16` | F1·S3_ALU | `0x2f000401` | int16 → fp16 (32×16b) | 16/8 | `float16`, all 32 lanes | H/OBS·exec |
| `ivp_float16nx16t` | F1·S3_ALU | `0x27004000` | int16 → fp16, predicated | 16/8 | `float16`, guard-lane masked | H/OBS |
| `ufloat16.h` | F1·S3_ALU | `0x2f100401` | uint16 → fp16 (scalar) | 16/8 | `fp16(uint16) · 2^(−imm4)` (unsigned) | H/OBS·exec |
| `ivp_ufloat16nx16` | F1·S3_ALU | `0x2f100400` | uint16 → fp16 (32 lanes) | 16/8 | `ufloat16`, all 32 lanes | H/OBS·exec |
| `ivp_ufloat16nx16t` | F1·S3_ALU | `0x27004001` | uint16 → fp16, predicated | 16/8 | `ufloat16`, guard-masked | H/OBS |

The signed/unsigned split is the **bit20** (`+0x100000`) difference in the encode word
(`float16nx16` `0x2f000401` vs `ufloat16nx16` `0x2f100400`), exactly the convention
[B13 §2](b13-sp-cvt.md#2-the-roster) and SX-ISA-39 record for the fp32 `float`/`ufloat` pair. `[HIGH/OBSERVED]`

> **GOTCHA — the scalar/vector bit0 polarity is *swapped* between `float16` and `ufloat16`.** Read
> from the `F1_S3_ALU` encode words: `float16` has `.H = …400` / `NX = …401` (bit0 = 1 means vector),
> but `ufloat16` has `.H = …401` / `NX = …400` (bit0 = 1 means *scalar*). The polarity of the
> scalar/vector discriminator is *inverted* between the two selectors. This is harmless to a correct
> assembler (each mnemonic owns its own thunk) but a reimplementer that hard-codes "bit0 = vector"
> across the whole `int→fp16` pair mis-decodes one of the two. The N0-bundle worked diffs (§6.3)
> isolate this to bundle **bit35**. `[HIGH/OBSERVED]`

### 2.2 fp16 round-to-integral (result stays fp16)

| mnemonic | fmt·slot | opcode-sel imm (F1_S3) | vec lane in→out | bytes | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `firound.h` | F1·S3_ALU | `0x2e100020` | fp16 → fp16 | 16/8 | round to integral, **half-away-from-zero** (fixed) | H/OBS·exec |
| `ivp_firoundnxf16` | F1·S3_ALU | `0x2e100031` | fp16 → fp16 (32 lanes) | 16/8 | `firound`, all 32 lanes | H/OBS·exec |
| `ivp_firoundnxf16t` | F1·S3_ALU | `0x270050d0` | fp16 → fp16, predicated | 16/8 | `firound`, guard-masked | H/OBS |
| `ficeil.h` | F1·S3_ALU | `0x2e000031` | fp16 → fp16 | 16/8 | round to integral toward **+∞** (ceil) | H/OBS·exec |
| `ivp_ficeilnxf16` | F1·S3_ALU | `0x2e1000a0` | fp16 → fp16 (32 lanes) | 16/8 | `ficeil`, all 32 lanes | H/OBS·exec |
| `ivp_ficeilnxf16t` | F1·S3_ALU | `0x27005040` | fp16 → fp16, predicated | 16/8 | `ficeil`, guard-masked | H/OBS |
| `fifloor.h` | F1·S3_ALU | `0x2e0000a1` | fp16 → fp16 | 16/8 | round to integral toward **−∞** (floor) | H/OBS·exec |
| `ivp_fifloornxf16` | F1·S3_ALU | `0x2e1000b0` | fp16 → fp16 (32 lanes) | 16/8 | `fifloor`, all 32 lanes | H/OBS·exec |
| `ivp_fifloornxf16t` | F1·S3_ALU | `0x270050c0` | fp16 → fp16, predicated | 16/8 | `fifloor`, guard-masked | H/OBS |
| `firint.h` | F1·S3_ALU | `0x2e0000b1` | fp16 → fp16 | 16/8 | round to integral, **FCR RoundMode** (RNE default) | H/OBS·exec |
| `ivp_firintnxf16` | F1·S3_ALU | `0x2e100021` | fp16 → fp16 (32 lanes) | 16/8 | `firint`, all 32 lanes | H/OBS·exec |
| `ivp_firintnxf16t` | F1·S3_ALU | `0x27005050` | fp16 → fp16, predicated | 16/8 | `firint`, guard-masked | H/OBS |
| `fitrunc.h` | F1·S3_ALU | `0x2e100030` | fp16 → fp16 | 16/8 | round to integral toward **0** (truncate) | H/OBS·exec |
| `ivp_fitruncnxf16` | F1·S3_ALU | `0x2e1000a1` | fp16 → fp16 (32 lanes) | 16/8 | `fitrunc`, all 32 lanes | H/OBS·exec |
| `ivp_fitruncnxf16t` | F1·S3_ALU | `0x27005060` | fp16 → fp16, predicated | 16/8 | `fitrunc`, guard-masked | H/OBS |

Within the FI family the encode words cluster: `ficeil`/`fifloor` share a selector base differing in
the `[7:4]` sub-nibble, `firint`/`firound` likewise, with the predicated `T` forms collapsing the
selector to a common base (`0x27005xxx`) and moving the rounding member entirely into the `[7:4]`
field — the worked XOR-diffs (§6.2) pin those bits. `[HIGH/OBSERVED]`

---

## 3. int16 → fp16 — `float16` / `ufloat16` and the 4-bit Q-format scale

### 3.1 The operand model — a **4-bit** scale exponent, not a round mode

`Iclass_FLOAT16_H_args` (VMA `0x84f040`, file `0x64f040` after the `0x200000` delta) lists **three**
operands, the 8-byte string pointer + 1-byte mode (`o`=`0x6f` out, `i`=`0x69` in) read verbatim this
pass:

```
operand[0]  mode='o'  opnd_ivpep_sem_hp_cvt_vt     -> vec destination (fp16)
operand[1]  mode='i'  opnd_ivpep_sem_hp_cvt_vr     -> vec source (int16)
operand[2]  mode='i'  opnd_ivpep_sem_hp_cvt_i_imm4 -> a 4-bit immediate (0..15)
```

The third operand is the **`i_imm4` Q-format scale exponent**. Its slot-local encoding is read from
the `_get` thunk, never by subtracting offsets. `Field_fld_ivpep_sem_hp_cvt_i_imm4_Slot_f1_s3_alu_get`
(`@0x3357a0`) is `mov (%rdi),%eax; shl $0x18,%eax; shr $0x1c,%eax; ret` — `shl 24` drops the top 24
bits, `shr 28` keeps the next 4 → **`imm4 = word0[7:4]`**, a contiguous 4-bit field. (Compare B13's
fp32 side, where the same scale is a **5-bit** `i_imm5` read by a *scattered* `word0[0] | word0[4:1]`
field — the half-precision side carries one fewer scale bit and a *contiguous* window.) `[HIGH/OBSERVED]`

> **CORRECTION — the fp16 convert scale is `i_imm4` (4-bit, range 0..15), *not* the `i_imm5` (5-bit,
> 0..31) of the fp32 [B13](b13-sp-cvt.md#31-the-operand-model--a-5-bit-scale-exponent-not-a-round-mode)
> side.** Both are the same Q-format binary-point exponent, but the fp16-output family declares a
> **`hp_cvt_i_imm4`** field with a **4-bit** window (`shl $0x18; shr $0x1c` = `word0[7:4]`) while the
> fp32-output family declares **`sp_cvt_i_imm5`** with a **5-bit** scattered window. A reimplementer
> that shares one scale-field decoder across both width families is wrong by one bit on the fp16
> side: an `imm4` field cannot express scales 16..31. The architectural reason is field budget — the
> fp16 op packs the same `[7:4]` window that the FI forms use as a rounding sub-selector (§6.2), so it
> tops out at 4 bits. `[HIGH/OBSERVED]`

### 3.2 Annotated value model — `int16 → fp16 · 2^(−imm4)`

The value leaves are `module__xdref_float16_1_16f_16_32_2` (`@0x522070`, signed) and
`module__xdref_ufloat16_1_1_16f_16_32_2` (`@0x522380`, unsigned) — integer-only soft-float (no
hardware-FP x86 insn). The signed leaf opens `mov %esi,%r10d; and $0x8000,%r10d; … neg %r11d`
(the sign-extract prologue); the unsigned leaf omits it (the `int16/uint16` distinction is exactly
that prologue, the same shape B13 / SX-ISS-07 read for the fp32 `float`/`ufloat` pair). The ABI,
recovered by disassembly + execution:

```c
// ivp_float16nx16 :  d[k] = round_f16( (int16)vr[k] * 2^(-imm4), RoundMode )      (signed Q-format)
// ivp_ufloat16nx16:  d[k] = round_f16( (uint16)vr[k] * 2^(-imm4), RoundMode )     (unsigned)
//   xdref leaf: module__xdref_float16_1_16f_16_32_2 / module__xdref_ufloat16_1_1_16f_16_32_2
// ABI (confirmed by execution):
//   float16 : void f(long xst, u32 a16, u32 imm4, u32 rmode, u32 *flag /*r8*/, u32 *value /*r9*/);
//   ufloat16: void f(long xst, u32 a16, u32 imm4, u32 rmode, u32 *o1 /*rcx*/, u32 *o2 /*r8*/, u32 *o3=value /*r9*/);
//   (the float16  body stores flag->r8, value->r9; the ufloat16 body uses the firint-shape
//    THREE output pointers, value in the THIRD slot — see §5.1 for the same 3-ptr shape.)
uint16_t float16(int16_t x, int imm4 /*0..15*/, int rmode) {
    // 1. magnitude+sign; 2. normalize (leading-zero shift, `shl %cl`);
    // 3. exponent rebias to fp16 0xf bias minus imm4; 4. round 16-bit magnitude into 11-bit
    //    significand per RoundMode; 5. assemble sign|exp|mant; overflow (>65504) -> +/-Inf.
}
```

**Q-format scale + rounding certificate (proven by execution).** The `imm4` argument is a
**right-shift / divide-by-2^imm4**, exactly the fixed-point binary-point position:

```
float16(24,  imm4=3, RNE) = 0x4200 = 3.0       (= 24 · 2^-3)
float16(-1,  imm4=3, RNE) = 0xb000 = -0.125    (= -1 · 2^-3, sign preserved)
float16(0,   imm4=0, RNE) = 0x0000 = +0.0
float16(-2048,imm4=0,RNE) = 0xe800 = -2048.0   (exact; |i16| <= 32768 always fits fp16)
float16(2049,imm4=0,RNE)  = 0x6800 = 2048.0    (Inexact=1: 2049 rounds to the fp16 grid)
```

`float16(2049)` shows the precision loss: fp16 has only an 11-bit significand, so `2049` is not
representable and the convert rounds to the grid (`2048`), setting Inexact. `float16` **cannot
overflow** — every `|int16|` is `≤ 32768 < 65504` (the fp16 max-normal) — so it has no
`OverflowEnable` state arg (§4); only `ufloat16` does. `[HIGH/OBSERVED·exec]`

**Unsigned high-bit + overflow certificate.**

```
ufloat16(0xFFFF=65535, imm4=3, RNE) = 0x7000 = 8192.0   (= 65535 · 2^-3, Inexact;  positive — a SIGNED float16 would read 0xFFFF as -1)
ufloat16(0xFFFF=65535, imm4=0, RNE) = 0x7c00 = +Inf      (Overflow: 65535 > 65504 = fp16 max-normal)
```

The raw `u16 = 65535` with no scale exceeds fp16's largest finite magnitude (65504), so `ufloat16`
overflows to `+Inf` (under RNE / toward-∞; under RZ/RD it would round to `0x7bff` max-finite — the
directed-round-overflow rule of §6.4) and raises Overflow. This is precisely why `ufloat16` is the
**only** member of this batch carrying an `OverflowEnable` state arg. `[HIGH/OBSERVED·exec]`

---

## 4. The state / exception model — read from the iclass `stateArgs`

Common to all 21: `STATE_IN = CPENABLE` (the vector-coprocessor enable; the op raises
`Coprocessor1Exception` and is squashed *before* any datapath effect iff cp1 is disabled). The IEEE
side-effects are per-op `stateArgs`, read here directly from the iclass tables (each entry = 8-byte
`.rodata` string pointer + mode byte, table at VMA − `0x200000`). Resolved string pointers this pass:

| iclass (`.H`) | `stateArgs` (mode) | meaning |
|---|---|---|
| `FLOAT16_H` (`@0x84f000`) | `RoundMode`(i), `InexactFlag`(m), `InexactEnable`(i), `CPENABLE`(i) | int→fp16: dynamic round + inexact |
| `UFLOAT16_H` (`@0x84ef40`) | `RoundMode`(i), `OverflowFlag`(m), `InexactFlag`(m), `OverflowEnable`(i), `InexactEnable`(i), `CPENABLE`(i) | adds **overflow** |
| `FIRINT_H` (`@0x84e600`) | `RoundMode`(i), `InexactFlag`(m), `InvalidFlag`(m), `InexactEnable`(i), … | dynamic round + inexact + invalid |
| `FITRUNC_H` (`@0x84e680`) | `InvalidFlag`(m), `InvalidEnable`(i), `CPENABLE`(i) | fixed mode: **no `RoundMode`** |
| `FICEIL_H` / `FIFLOOR_H` / `FIROUND_H` | `InvalidFlag`(m), `InvalidEnable`(i), `CPENABLE`(i) | fixed mode: **no `RoundMode`** |

> **QUIRK — the *fixed-mode* FI ops (`fitrunc`/`ficeil`/`fifloor`/`firound`) carry NO `RoundMode`
> state arg; only `firint` and the int→fp16 ops do.** This is the iclass-level surfacing of the
> "rounding-mode-baked-in" property: `fitrunc`/`ficeil`/`fifloor` hard-wire RZ/+∞/−∞ and `firound`
> hard-wires `away`, so they never read the FCR (their `stateArgs` lists `InvalidEnable` but not
> `RoundMode`). `firint` *does* list `RoundMode` and reads the dynamic FCR mode; `float16`/`ufloat16`
> also list `RoundMode` (the int-mantissa-into-significand rounding). A reimplementer that threads the
> FCR mode into `ficeil`/`fifloor`/`fitrunc`/`firound` is over-modeling — those four are
> mode-independent. `[HIGH/OBSERVED]`

The FCR state register layout itself is read from the `libisa-core.so` state descriptor strings
(`strings -a | rg RoundMode`, this pass), in the `<width>:<bitpos>:s:<name>:<reset>` form:

```
2:8:s:RoundMode:0        -> RoundMode field, reset value 0 (= RNE)
1:10:s:InexactEnable:0
1:12:s:OverflowEnable:0
1:14:s:InvalidEnable:0
```

So the architectural **FCR reset RoundMode = 0 = RNE** (round-to-nearest-even), and the three enable
bits reset to 0 (masked). The matching `InvalidFlag`/`InexactFlag`/`OverflowFlag` are the FSR sticky
status bits (`SHARED_OR`), posted as late DEFs (§9.1). `[HIGH/OBSERVED]`

> **GOTCHA — TWO rounding defaults are in play, and convert ops are exactly where it bites.** The
> *architectural* FCR reset is **RNE** (the `2:8:s:RoundMode:0` descriptor above), so a hardware
> `float16`/`firint` with the FCR untouched rounds-to-even. But the *fiss value leaf*, called as an
> oracle with **no round-mode threaded**, runs its **compiled default RZ** (round-toward-zero /
> truncate) — the property the [B13 §3.3 CORRECTION](b13-sp-cvt.md#33-precision-loss-rounding-above-224--and-the-rz-leaf-default)
> and the [confidence model §6.1](../../reference/confidence-model.md) pin, and which the live
> add/sub differential proves (RZ on every inexact). For the value sweeps on this page I *supply* the
> mode arg explicitly (the leaves take it as `%edx`/`%ecx`), so the tie behavior is the requested
> mode; a reimplementer modeling the *hardware* op rounds with the live FCR `RoundMode`, while one
> driving the *value leaf* as an oracle either supplies a mode or expects RZ. Both are true at their
> own level; this page does not re-litigate the split. `[HIGH/OBSERVED·exec]`

---

## 5. fp16 round-to-integral — `firound` / `ficeil` / `fifloor` / `firint` / `fitrunc`

These five keep the result **in fp16** (round to an integral fp16 value, no int conversion). The
value leaves are `module__xdref_{ficeil,fifloor,firound,fitrunc}_1_16f_16f`
(`@0x524430/524570/5246a0/524820`, ABI `(xst, a16=esi, *flag=rdx, *value=rcx)`) and
`module__xdref_firint_1_1_16f_16f_2` (`@0x524900`, ABI `(xst, a16=esi, rmode=edx, *o1=rcx, *o2=r8,
*value=r9)` — the three-output `_1_1_` shape, value in the **third** slot).

### 5.1 The behavior table (proven by execution)

```
   in  | ficeil  fifloor  firound  fitrunc | firint(RNE)
 ------+----------------------------------+------------
  2.5  | 0x4200   0x4000   0x4200   0x4000 |  0x4000     <- ceil=3, floor=2, round=3, trunc=2, rint=2(ties-even)
       |  3.0      2.0      3.0      2.0   |   2.0
 -2.5  | 0xc000   0xc200   0xc200   0xc000 |  0xc000     <- ceil=-2, floor=-3, round=-3, trunc=-2, rint=-2
       | -2.0     -3.0     -3.0     -2.0   |  -2.0
 -0.5  | 0x8000   0xbc00   0xbc00   0x8000 |  (signed-zero preserved on the ceil/trunc legs)
       | -0.0     -1.0     -1.0     -0.0   |
```

`ficeil` = toward **+∞**, `fifloor` = toward **−∞**, `fitrunc` = toward **0**, `firound` =
**half-away-from-zero** — proven on both signs including the signed-zero edges (`ficeil(-0.5)=-0.0`,
`fifloor(-0.5)=-1.0`, `fitrunc(-0.5)=-0.0`). `[HIGH/OBSERVED·exec]`

### 5.2 `firound` (fixed half-away) vs `firint` (FCR mode) — the `away` bit, proven live

> **QUIRK — `firound` is the only fp16 convert reachable to the *fifth* round code `away`; `firint`
> honors only the 2-bit FCR mode.** Driving the two on the same fp16 tie inputs (`firint` mode swept
> 0..3, `firound` fixed):
>
> ```
>    in   | firint(RNE=0)  firint(RZ=1)  firint(RU=2)  firint(RD=3) | firound (fixed)
>  -------+----------------------------------------------------------+----------------
>   2.5   |   0x4000(2.0)    0x4000(2.0)   0x4200(3.0)   0x4000(2.0) |   0x4200 (3.0)
>   0.5   |   0x0000(0.0)    0x0000(0.0)   0x3c00(1.0)   0x0000(0.0) |   0x3c00 (1.0)
>  -2.5   |   0xc000(-2.0)        …             …            …       |   0xc200(-3.0)
> ```
>
> `firint(2.5,RNE)→2.0`, `firint(0.5,RNE)→0.0` — **round-half-to-even**; `firint(2.5,RU)→3.0`,
> `firint(2.5,RD)→2.0` — it *reads* a directed mode. `firint` honors **only the 2-bit dynamic FCR
> field**: passing it mode `4` wraps in the 2-bit field back to RNE (verified live — `firint(2.5,
> mode=4)=0x4000=2.0`, identical to RNE), so the `away` code is *not* reachable through `firint`.
> `firound` resolves `2.5→3.0`, `0.5→1.0`, `-2.5→-3.0` — **round-half-away-from-zero**, the C99
> `roundf()` rule, **independent of FCR**. This is the fp16 mirror of the
> [fp sub-ISA §3.1](../core/fp-sub-isa.md#31-the-2-bit-fcr-field-vs-the-3-bit-convert-core-field)
> 3-bit-convert-core finding: code `100 = away` is **used only by `FIROUND`** and is **not** reachable
> through the 2-bit FCR write. A reimplementer maps `firound` to a hard-wired half-away rounder and
> `firint` to the 2-bit-FCR-mode rounder; conflating them is wrong on every `.5` input.
> `[HIGH/OBSERVED·exec]`

> **NOTE — the `firound` half-away increment is the *integer-round* path, not the *narrow*-convert path.**
> The `2.5→3.0`/`0.5→1.0` half-away increment above is the **FI integer-round** datapath (input and output
> both fp16, rounding to an integral value). On a **narrow** output-rounding convert (e.g. fp32→fp16), the
> `away` arm reads the `R` (round) bit and at the *exact* narrow half does **not** increment — it matches
> RTZ — because the half-quantum sits below the bit the core treats as `R`. So drive `away` from FIROUND,
> not from a narrow exact-half. Consistent with [Formal Semantics II](../semantics/group-semantics-ii.md) §2
> (narrow exact-half correction) and [B13 sp-cvt](b13-sp-cvt.md) §5.2. `[HIGH/OBSERVED]`

### 5.3 Special values (proven by execution)

```c
// ivp_firintnxf16  : d[k] = round_to_integral_fp16(vr[k], FCR.RoundMode)  (stays fp16)
// ivp_firoundnxf16 : d[k] = round_to_integral_fp16(vr[k], AWAY)           (fixed half-away)
// ivp_ficeilnxf16  : RU-equivalent (+inf)    ivp_fifloornxf16 : RD-equivalent (-inf)
// ivp_fitruncnxf16 : RZ-equivalent (toward 0)
//   |x| >= 2^11 (already integral) -> passthrough; inf -> inf (no flag);
//   qNaN -> qNaN (no flag); sNaN -> qNaN + Invalid; |x| < 1 (incl. subnormals) -> SIGNED ZERO.
```

Driven live on `fitrunc.H`:

```
fitrunc(0x4180 = 2.75)  -> 0x4000 = 2.0   (flag 0)      fitrunc(0xc180 = -2.75) -> 0xc000 = -2.0
fitrunc(0x3666 = 0.4)   -> 0x0000 = +0.0  (flag 0)      fitrunc(0xb666 = -0.4)  -> 0x8000 = -0.0
fitrunc(0x0001 subn)    -> 0x0000 = +0.0  (flag 0)      fitrunc(0x8001 -subn)   -> 0x8000 = -0.0
fitrunc(0x8000 = -0.0)  -> 0x8000 = -0.0  (flag 0)
fitrunc(0x7c00 = +inf)  -> 0x7c00 = +inf  (flag 0)      fitrunc(0x7e00 qNaN)    -> 0x7e00 (flag 0, passthrough)
fitrunc(0x7d00 sNaN)    -> 0x7f00          (flag 1, sNaN -> qNaN quieted: |0x200, payload kept)
```

> **QUIRK — `fitrunc` of any `|x| < 1` (the entire fp16 subnormal band *and* small normals like 0.4)
> returns SIGNED ZERO, and `+inf`/`qNaN` pass through unchanged.** A naive "cast to int then back"
> reimplementation overflows or traps on Inf/NaN and loses the zero sign on `-0.4 → -0.0`. The
> sNaN-quieting is "set the fp16 quiet bit (bit 9 = `0x200`), preserve the rest, raise Invalid"
> (`0x7d00 → 0x7f00`, flag 1) — the same payload-preserving quieting the widen does (§6.2). The
> Invalid flag is the only FSR effect of the FI family (their `stateArgs` carry `InvalidEnable`, never
> `OverflowEnable`: a round-to-integral can never exceed the input magnitude). `[HIGH/OBSERVED·exec]`

---

## 6. The fp16↔fp32 hub — widen (B13) and the narrow it owns

The fp16↔fp32 width converts are the only native fp-width pair in the whole ISA, and they are this
batch's connection to every other datatype. The **widen** `cvtf32f16`/`cvtf32nxf16` (fp16→fp32) lands
on the fp32 side and is rostered in [B13 §6](b13-sp-cvt.md#6-fp16--fp32-widen--cvtf32f16--cvtf32nxf16);
the **narrow** `cvtf16f32`/`cvtf16n_2xf32` (fp32→fp16) is named `cvtf16*` ("produce fp16") and routes
to B20 by name root. Both legs are stated here because the FTZ/DAZ + NaN-payload behavior is the
hub's, and a reimplementer of either must match it. The leaves: widen
`module__xdref_cvtf32_1_32f_16f` (`@0x5b77f0`, ABI `(xst, a16=esi, *flag=rdx, *value=rcx)`); narrow
`module__xdref_cvtf16_1_1_1_1_16f_32f_2` (`@0x5b78f0`).

### 6.1 FTZ/DAZ — there is **no flush-to-zero and no denormals-are-zero** (proven by execution)

Widening every class of fp16 input through the widen leaf:

```
widen(0x0001 smallest subn) -> 0x33800000 = 5.960464e-08   (flag 0)   <- fp16 SUBNORMAL -> finite fp32 NORMAL
widen(0x0200 mid subn)      -> 0x38000000 = 3.051758e-05   (flag 0)
widen(0x03ff largest subn)  -> 0x387fc000 = 6.097555e-05   (flag 0)   <- leading-zero-normalized into fp32 normal range
widen(0x0000 = +0)          -> 0x00000000 = +0             (flag 0)
widen(0x8000 = -0)          -> 0x80000000 = -0             (flag 0)   <- zero SIGN preserved
```

> **QUIRK — the GPSIMD fp converter has NO FTZ and NO DAZ: every fp16 subnormal widens to a finite
> (normalized) fp32, and signed zero is preserved.** A reimplementer who models the silicon as
> FTZ/DAZ (a common GPU/DSP default) diverges on the entire fp16 subnormal band — `0x0001` is
> `5.96e-8`, not `0`. The widen leading-zero-normalizes the subnormal magnitude into the fp32 normal
> exponent range (fp32's exponent easily holds fp16's denormal magnitudes), and `-0` widens to `-0`,
> not `+0`. The value path computes full gradual underflow; whether a separate FCR FTZ *mode bit*
> exists is not decided here (the soft-float leaves always compute full denormals). `[HIGH/OBSERVED·exec]`

### 6.2 NaN-payload preservation + sNaN quieting (proven by execution)

```
widen(0x7e00 qNaN)        -> 0x7fc00000   (flag 0)   bit-identical canonical qNaN, no Invalid
widen(0x7e34 qNaN+payload)-> 0x7fc68000   (flag 0)   payload 0x34 left-shifted by 13 (0x34<<13 = 0x68000), qNaN bit kept
widen(0xfe00 -qNaN)       -> 0xffc00000   (flag 0)   SIGN preserved
widen(0x7d00 sNaN)        -> 0x7fe00000   (flag 1)   sNaN -> qNaN: |0x400000 quiet bit, Invalid raised
widen(0x7c01 sNaN+lowbit) -> 0x7fc02000   (flag 1)   payload low bit 0x1 left-shifted 13 (0x1<<13 = 0x2000), quieted, Invalid
```

> **QUIRK — NaN payload is *carried* across the widen (mantissa-width-shifted by 13 = 23−10), and
> sNaN is quieted "set the quiet bit, preserve the rest, raise Invalid".** The payload is not
> canonicalized: `0x7e34`'s `0x34` survives as `0x68000` in the fp32 mantissa (shifted by the 13-bit
> mantissa-width delta), the qNaN bit is preserved, and the sign survives (`-qNaN → 0xffc…`). An sNaN
> input has the fp32 quiet bit (`0x400000`) set and raises Invalid, but the surviving payload bits and
> sign are kept (`0x7c01 → 0x7fc02000`, flag 1). A reimplementer that emits a canonical `0x7fc00000`
> on every NaN loses the payload and the low-bit signature; one that drops the sign mis-signs the
> propagated NaN. `[HIGH/OBSERVED·exec]`

### 6.3 The narrow side (B20-owned) — overflow→inf / gradual-underflow, for boundary context

The narrow `cvtf16f32` rebiases fp32's 8-bit exponent to fp16's 5-bit (`add $0x1f90` in the leaf
body), rounds per the FCR mode (the GRS round block, default RNE — or RZ at the un-threaded leaf
level, §4 GOTCHA), forces `exp = 0x1f` on overflow (large fp32 → fp16 `±Inf`, *not* saturation to
fp16 max-normal `0x7bff`), and produces fp16 **subnormals** on gradual underflow (no output FTZ). The
directed-round-overflow rule is the one [B13 §6.3](b13-sp-cvt.md#63-the-narrow-side-b20--overflowinf-for-boundary-context)
round-trips against this page:

```
narrow(70000 > fp16max)  RNE=0x7c00(+inf)  RZ=0x7bff(max-finite)  RU=0x7c00  RD=0x7bff   <- overflow by mode
narrow(65520)            RNE=0x7c00(rounds up past max -> +inf)    narrow(65504)=0x7bff (finite)
narrow(6e-8)             RNE=0x0001 (gradual underflow to smallest fp16 subnormal)
narrow(-0.0)             = 0x8000 (sign preserved)
```

Overflow under RNE / toward-the-infinity → `±Inf`; under RZ/RD (away from the infinity) → `±0x7bff`
max-finite — the same [directed-round-overflow rule](../core/fp-sub-isa.md#51-what-is-full-ieee-754)
the fp arithmetic follows. The *encoding* of the narrow ops and the 2009-value widen→narrow
round-trip certificate are split with [B13 §6.2](b13-sp-cvt.md#62-the-fp16--fp32-round-trip--exact-inverse-over-2009-values);
this page owns the narrow's roster home (name root `cvtf16`) and states its overflow/underflow rule
for completeness. `[HIGH/OBSERVED·exec]`

### 6.4 bf16 / fp8 / fp4 route through this hub — there is **no native convert** for them

The convert member lists are exhaustive: this batch + B13 hold **only** `int↔fp16`, `int↔fp32`,
`fp16↔fp32`, and the FI integer-rounds. There is **no bf16 convert opcode, no fp8 (e3m4/e4m3/e5m2)
convert opcode, and no fp4 (e2m1) convert opcode** in either convert block — an exhaustive negative
control (zero `8f`/`fp8`/`e4m3`/`e5m2`/`bf16` tokens across the fiss exports, zero
`IVP_*BF16*`/`IVP_*FP8*` cas issue functions). Every other format pair is two legs through the fp32
hub: bf16 = the top 16 bits of fp32 (a shift + RNE round in the firmware Cast kernel); fp8/fp4 =
exponent rebias + mantissa truncate/round + saturating-clamp (nibble-unpack → `ufloat16`/`ufloat` →
scale-MAC → `bmin`/`bmax` clamp). The IVP datapath exposes `fp16↔fp32` + `int↔fp` + saturating pack;
everything else rides those primitives. The stochastic round used elsewhere in the device is the
PE-array PSUM `fp32→bf16` round, a *separate* hardware datapath — **not** an IVP convert mode. `[HIGH/OBSERVED]`

---

## 7. Batch tally — every fp16-convert mnemonic vs `nm`

The 21 mnemonics and their placement counts, re-counted this pass with
`nm libisa-core.so | rg -c 'Opcode_<mnem>_Slot_.*_encode'` (the only legitimate count method, per
[coverage tally](../core/coverage-tally.md)):

| group | mnemonics (placements each = 6) | count |
|---|---|---|
| int16→fp16 + scale | `float16_h` `float16nx16` `float16nx16t` `ufloat16_h` `ufloat16nx16` `ufloat16nx16t` | 6 |
| round-to-integral (`.H`/`nxf16`/`T`) | `ficeil_h` `ficeilnxf16` `ficeilnxf16t` `fifloor_h` `fifloornxf16` `fifloornxf16t` `firint_h` `firintnxf16` `firintnxf16t` `firound_h` `firoundnxf16` `firoundnxf16t` `fitrunc_h` `fitruncnxf16` `fitruncnxf16t` | 15 |

```
BATCH TOTAL : 21 mnemonics, 126 placements  (= 21 × 6, every convert is 5 wide F-slots + 1 N0)
```

Re-counted directly: `nm | rg -c` over the 21-mnemonic alternation = **126** (this pass). Each base
family resolves to a `module__xdref_` leaf driven live above: `float16`/`ufloat16`
(`@0x522070`/`@0x522380`), `ficeil`/`fifloor`/`firound`/`fitrunc`
(`@0x524430`/`@0x524570`/`@0x5246a0`/`@0x524820`), `firint` (`@0x524900`), plus the hub widen/narrow
(`@0x5b77f0`/`@0x5b78f0`). The `t`-variants share the base leaf (predication is an encoding/lane-mask
property, not a different value function), so the leaf count for this batch is **~7 distinct value
functions**, comfortably inside the `864` value-leaf denominator. `[HIGH/OBSERVED·exec]`

> **NOTE — ungrounded / deferred items (honestly flagged).**
> (1) The **`t` (predicated) variants** were grounded at the *encoding* level (distinct `word0`
> selectors, the `*T` iclass with a `vbr:vbool` guard operand, the worked `T`-form XOR-diff §6 of the
> roster) but their value semantics were not swept live — they reuse the base leaf gated by a `vbool`
> mask, so their value is `[HIGH/INFERRED]` (= base op merged per `pred(vbr,k)`), their encoding
> `[HIGH/OBSERVED]`. (2) The exact `imm4` scale field placement in the **wide** vs **narrow** slot
> differs per-format; §3.1 reads it from the `F1_S3_ALU` `_get` thunk (`word0[7:4]`) and the §6 worked
> bundles from `N0_S3_ALU` — the per-format drift is `[MED/OBSERVED]` (two slots read, the others
> inferred to follow the same `[7:4]` window the FI sub-selector also uses).

---

## 8. Worked bit-pattern examples — device-oracle round-tripped

Assembled with `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`,
`XTENSA_SYSTEM=…/ncore2gp/config`) and disassembled with `xtensa-elf-objdump` (this pass). Each op
emits an 8-byte **`N0_S3_ALU`** FLIX bundle `{ nop; nop; nop; <op> }` (the op in slot S3); objdump
prints the bundle as one 64-bit big-endian numeral.

```
ivp_fitruncnxf16  v1, v2       -> 32505718c042452f   "{ nop; nop; nop; ivp_fitruncnxf16  v1, v2 }"
ivp_ficeilnxf16   v1, v2       -> 32505518c042452f
ivp_fifloornxf16  v1, v2       -> 32505518e042452f
ivp_firintnxf16   v1, v2       -> 32505618c042452f
ivp_firoundnxf16  v1, v2       -> 32505618e042452f
ivp_float16nx16   v1, v2, 3    -> 32510c086042452f
ivp_ufloat16nx16  v1, v2, 3    -> 32510d006042452f
ivp_fitruncnxf16t v1, v2, vb3  -> 325015386042452f   (predicated; vbr = vb3)
float16.h         v1, v2, 3    -> 32510c006042452f
ufloat16.h        v1, v2, 3    -> 32510d086042452f
fitrunc.h         v1, v2       -> 32505418e042452f
ficeil.h          v1, v2       -> 32505218e042452f
```

### 8.1 Discriminator bits, isolated by XOR-diff (this pass)

```
ceil ^ floor                = 0x20000000     -> bit 29 ONLY   (the [7:4] 0x6<->0x7 pair-member bit, scattered)
rint ^ round                = 0x20000000     -> bit 29 ONLY   (same pair-member bit)
fitrunc ^ ficeil            = 0x20000000000  -> bit 41 ONLY   (selector low nibble, scattered)
float16nx16 ^ float16.h     = 0x800000000    -> bit 35 ONLY   (slot-bit0 scalar/vector flag -> bundle bit 35)
ufloat16nx16 ^ ufloat16.h   = 0x800000000    -> bit 35 ONLY   (same bit, SWAPPED polarity, §2.1)
float16nx16 ^ ufloat16nx16  = 0x10800000000  -> bits 35, 40   (the +0x100000 unsigned bit20 + the bit0 polarity)
fitruncnxf16 ^ fitruncnxf16t= 0x4220a0000000 -> bits 29,31,37,41,46  (selector narrowing + the vbr field)
```

The single-bit diffs confirm the §2/§6 encoding model directly from real assembled bytes: the FI
rounding-member is one scattered bit (29 for the pair-member, 41 for the selector low nibble), the
scalar/vector flag is bundle bit 35 (with the `ufloat16` polarity swap visible as the *same* bit but
opposite sense), and the predicated `T` form differs in the cluster `{29,31,37,41,46}` (the
selector-narrowing plus the `vbr:vbool` predicate-select field). `[HIGH/OBSERVED]`

### 8.2 The fp16 narrow bundle for completeness

The fp16↔fp32 **narrow** (B20's name-root home, §6) round-trips the same way; the worked widen→narrow
bit-exact certificate over 2009 fp16 values is held by [B13 §6.2](b13-sp-cvt.md#62-the-fp16--fp32-round-trip--exact-inverse-over-2009-values).
The widen lands on the fp32 side and is rostered there; this page proves only the int→fp16 and
fp16-round bundles above. `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim re-tested against the binary this pass; a claim survives only if a *second*
independent witness agrees.

1. **The convert scale is `i_imm4` (4-bit), distinct from B13's `i_imm5` (5-bit).** *Challenge:* did
   I read the wrong field thunk, or is `imm4` just `imm5` with the top bit always 0? *Re-test:* the
   field name in the iclass `args` table is literally `opnd_ivpep_sem_hp_cvt_i_imm4` (string pointer
   resolved this pass), and the `_get` thunk for `F1_S3_ALU` is `shl $0x18; shr $0x1c` = a **4-bit**
   window `word0[7:4]` (drop top 24, keep next 4) — structurally cannot express 16..31. Cross-witness:
   B13's fp32 field is named `i_imm5` with a *5-bit scattered* `word0[0] | word0[4:1]` window. Two
   witnesses (the named `imm4` operand string + the 4-bit `_get` shift pattern) agree, and they
   *differ from* B13's `imm5`. **Survives.** `[HIGH/OBSERVED]`

2. **The fixed-mode FI ops carry no `RoundMode` state arg; `firint`/`float16`/`ufloat16` do.**
   *Challenge:* could the `stateArgs` table I read have aliased the wrong iclass? *Re-test:* the
   `Iclass_FITRUNC_H_stateArgs`/`FICEIL_H`/`FIROUND_H`/`FIFLOOR_H` tables (each at VMA − `0x200000`)
   resolve to `{InvalidFlag, InvalidEnable, CPENABLE}` — **no `RoundMode`** — while `FLOAT16_H`
   resolves to `{RoundMode, InexactFlag, InexactEnable, CPENABLE}` and `FIRINT_H` to `{RoundMode,
   InexactFlag, InvalidFlag, InexactEnable, …}`. Cross-witness: the live behavior matches —
   `firint(2.5, mode)` *changes* with the mode (RU→3.0, RD→2.0) while `fitrunc`/`ficeil`/`fifloor`/
   `firound` are mode-invariant. Two witnesses (the iclass `stateArgs` strings + the live
   mode-sensitivity) agree. **Survives.** `[HIGH/OBSERVED·exec]`

3. **`firound` is fixed half-away and `firint` honors only the 2-bit FCR — `away` is FIROUND-only.**
   *Challenge:* maybe both are RNE and `firound`'s `.5→away` is a tie-bias coincidence? *Re-test:* on
   the *same* fp16 inputs `firint(RNE)` gives `2.5→2.0, 0.5→0.0` (ties-to-**even**) while `firound`
   gives `2.5→3.0, 0.5→1.0, -2.5→-3.0` (always **away**) — they disagree on every half; and
   `firint(2.5, mode=4)` wraps the 2-bit field back to `2.0` (= RNE), proving the `away` code is *not*
   reachable through `firint`. This is the fp16 mirror of the fp sub-ISA `away`-only-for-FIROUND
   prediction, now proven by execution. **Survives.** `[HIGH/OBSERVED·exec]`

4. **No FTZ/DAZ: fp16 subnormals widen to finite fp32 and NaN payload survives the hub.**
   *Challenge:* could a few-sample sweep have missed a flush path? *Re-test:* the smallest fp16
   subnormal `0x0001` widens to `0x33800000 = 5.96e-8` (finite), the largest `0x03ff` to
   `0x387fc000` (a *normalized* fp32), and `-0 → -0` (sign kept) — losslessness is a width fact
   (fp32's exponent range holds fp16's denormal magnitudes), not a sampling claim. NaN payload:
   `0x7e34 → 0x7fc68000` (payload `0x34<<13`), sNaN `0x7d00 → 0x7fe00000` with Invalid raised — both
   the payload-shift and the quiet-bit-set witnessed live. Two witnesses (the width-structural
   normalize + the boundary-class live drive incl. all NaN classes) agree. **Survives.**
   `[HIGH/OBSERVED·exec]`

5. **The fp16-convert family is 21 mnemonics / 126 placements in `S3_ALU`, excluding `trunc16`/
   `utrunc16`.** *Challenge:* `nm` shows `trunc16nxf16`/`utrunc16nxf16` — do they belong here?
   *Re-test:* those are the fp16→**int** trunc, grouped with the lookup family
   (`ivpep_sem_hp_lookup`), have no FCR round decode (always round-toward-zero), and are owned by
   [B14](b14-hp-lookup.md) — not in this batch's 21. The 21 are the **int→fp16** (`float16`/`ufloat16`)
   and **fp16-round-to-integral** (`fitrunc`/`ficeil`/`fifloor`/`firint`/`firound`) families × 3 forms;
   `21 × 6 = 126` re-counted via `nm` this pass. The two families share neither semantic group nor
   round-mode model. **Survives.** `[HIGH/OBSERVED]`

No claim on this page rests on a raw dump, an unnamed symbol, or a single uncorroborated witness;
every value fact carries a differential-execution certificate against the shipped leaf, and every
boundary is pinned by slot + leaf + dtype.

### 9.1 Convert writeback latency — OBSERVED from the ISS

The cycle model lives in **`libcas-core.so`**, the generated cycle-accurate ISS, as per-op
`F<fmt>_F<fmt>_S3_ALU_<n>_inst_IVP_<MNEMONIC>_issue` symbols — keyed by FLIX format + issue slot, not
a global opcode index. Each `_issue` function sets a per-port pipeline stage in `%esi` (`mov $0xN,%esi`)
before each register-file port call; the final stage is the writeback commit. Decoded this pass
(neither binary has DWARF — `readelf -S` shows only `.symtab`; this is ISS disassembly):

| op (issue fn @addr) | structural ports | vec source read | vbool read | dest writeback |
|---|---|---|---|---|
| `FLOAT16NX16T` (`@0x14b6860`) | `0xe` (14) | `0xa` (10) | (vbool) | **`0xd` (13)** |
| `UFLOAT16NX16T` (`@0x14b6910`) | `0xe`, `0xe` (TWO ports) | `0xa` (10) | — | **`0xd` (13)** |
| `FICEILNXF16T` (`@0x14b71f0`) | `0xe` (14) | `0xa` (10) | `0xa` | **`0xd` (13)** |
| `FIRINTNXF16T` (`@0x14b72a0`) | `0xe`, `0xe` | `0xa` (10) | — | **`0xd` (13)** |

> **NOTE — every fp16 convert posts its vec result at stage 13, with vec source reads at stage 10.**
> The ~3-cycle result latency (vr read @10, vt written @13) matches the fp-ALU pipeline class
> ([B13 §9.1](b13-sp-cvt.md#91-convert-writeback-latency--observed-from-the-iss-and-a-stage-correction):
> the fp32 FLOAT/round family also commits at 13). The structural-horizon ports (`0xe = 14`) are the
> WB-port/bypass stall window, not a value latency; `UFLOAT16NX16T` carries an **extra** structural
> port (`0xe` twice) versus the signed `FLOAT16NX16T` — the unsigned-magnitude/overflow staging cost,
> the same extra-port asymmetry the fp32 `ufloat` shows. The CPENABLE coprocessor-enable is sampled at
> the IVP enable stage as for all IVP ops. `[HIGH/OBSERVED]` for the ops with their own `_issue`
> symbol; `[HIGH/INFERRED]` for the non-`T` and the `.H` forms that alias a family handler (each still
> carries its own populated 16-entry `inst_stage*` table, so it is modeled, not defaulted).

---

## 10. Confidence ledger

**HIGH / OBSERVED (by execution)** — driven live against `libfiss-base.so` this pass:

* The Q-format scale semantics of `float16`/`ufloat16` (`d = int · 2^(−imm4)`, the halving sweep), the
  signed-vs-unsigned high-bit (`ufloat16(0xFFFF)` positive), the precision-loss rounding
  (`float16(2049)→2048`, Inexact), and the `ufloat16` overflow→`+Inf` (`65535 > 65504`).
* The five round-to-integral behaviors (`ficeil`=+∞, `fifloor`=−∞, `fitrunc`=→0, `firint`=2-bit-FCR
  ties-even, `firound`=fixed half-away), the `firint` vs `firound` half-disagreement, the
  `firint(mode=4)→RNE` 2-bit-wrap, and the signed-zero / sNaN-quieting / `|x|<1→signed-zero` edges.
* **No FTZ/DAZ**: every fp16 subnormal widens to finite fp32, `-0` widens to `-0`; NaN payload carried
  (shift by 13), sNaN→qNaN with the quiet bit set and Invalid raised.

**HIGH / OBSERVED** — read from `libisa-core.so` immediates / disassembly + `libcas-core.so` ISS this pass:

* The 21-mnemonic roster, the `F1_S3_ALU` opcode-selector immediates (incl. the `+0x100000` unsigned
  bit20 and the swapped scalar/vector bit0 polarity), the `S3_ALU` placement (5 wide + 1 narrow), the
  126-placement census.
* The operand model: `vt`/`vr` vec operands + the **`i_imm4`** 4-bit scale (`word0[7:4]` `_get` thunk),
  the `vbr:vbool` predicate operand on the `T` forms; the FCR state layout (`2:8:s:RoundMode:0` reset
  RNE, the Inexact/Overflow/Invalid enable bits) and the per-iclass `stateArgs` (FI fixed-mode carry
  no `RoundMode`; `ufloat16` alone carries `OverflowEnable`); `CPENABLE` + `Coprocessor1Exception`.
* The N0-bundle worked examples + the XOR-diff discriminator bits (29/41/35/40 + the `T`-cluster), all
  round-tripped through the device `xtensa-elf-as`/`objdump` oracle.
* The writeback latency (stage 13, vec source @10, structural ports @14; the `ufloat16` extra port),
  decoded from the `libcas-core.so` `_issue` functions.

**HIGH / INFERRED**

* The `t` (predicated) variants' value = base op merged per `pred(vbr,k)` mask (encoding OBSERVED;
  value inferred from the `*T` iclass guard operand, not swept live).
* The non-`T`/`.H` ops' writeback stage (alias a family handler; grouped to 13 by their populated
  16-entry stage table).

**MED / OBSERVED**

* The per-format drift of the `imm4` scale field beyond the read `F1_S3_ALU` (`word0[7:4]`) and
  `N0_S3_ALU` slots.

**Deferred (not in this batch's denominator)**

* fp16→int `trunc16`/`utrunc16` → [B14 hp-lookup](b14-hp-lookup.md); the fp32-side int↔fp32 / fp16→fp32
  widen / fp32 round → [B13 sp-cvt](b13-sp-cvt.md); fp16/fp32 transcendental seeds →
  [B14](b14-hp-lookup.md)/[B15](b15-sp-lookup.md); the integer wide-accumulator repacks →
  [B10 wvec-pack](b10-wvec-pack.md)/B22; fp16/fp32 arithmetic → B02.

All facts read as derived from shipped-artifact static analysis and license-free in-process execution
of the binary's own value leaves (lawful interoperability RE).

---

## Cross-references

* [The Floating-Point Sub-ISA (FCR/FSR view)](../core/fp-sub-isa.md) — the architectural `RoundMode`,
  the `away` code (`100`) only `firound` reaches, the two-level RNE-vs-RZ model this page's int→fp16
  default invokes, the directed-round-overflow rule the narrow follows, and the sticky FSR flags the
  rounding/FI converts post.
* [Group Semantics II — Convert / Pack / FP](../semantics/group-semantics-ii.md) — the formal
  reference compute (the `sem_fp_hp_cnv` core, the 5-mode round logic, the special-value selection)
  that the fiss leaves on this page model.
* [ISS — cas / fiss Convert / Pack / FP](../../iss/cas-convert-pack-fp.md) — the `libcas-core` decode/
  timing and `libfiss-base` value-leaf model this page's encoding (§2) and latency (§9.1) sit on.
* [VAL — Convert / Pack / Cast Family](../../validation/convert-pack-cast.md) — the differential
  validation of the FTZ/DAZ, NaN-payload, and round-mode behaviors this page proves live (§6).
* [B13 fp32 convert (sp-cvt)](b13-sp-cvt.md) — the fp32-axis mirror: the int↔fp32 + fp32-round +
  fp16→fp32 **widen** family, the `i_imm5` (5-bit) scale this page's `i_imm4` (4-bit) parallels, and
  the widen→narrow round-trip certificate this page's narrow (§6.3) round-trips against.
* [B14 fp16 lookup](b14-hp-lookup.md) — the fp16→int `trunc16`/`utrunc16` and the fp16 transcendental
  seeds (`recip0`/`rsqrt0`/`nexp`) excluded from this convert family.
* [The Eight Register Files](../core/register-files.md) — the `vec` (idx 2) datapath and the `vbool`
  (idx 3) predicate file the `T` forms read; the S3_ALU writeback stage (13) is read from the
  `libcas-core.so` ISS (§9.1).
* [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the per-instruction
  schema, the `cvt → B20 if fp16 else B13` classifier, and the placement roll-up this batch's 126
  placements feed.
* [ISA Coverage & the Tally](../core/coverage-tally.md) — the certified denominators and the
  count-with-`nm` discipline.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the
  proven-by-execution value lane, and the "RZ-default un-parameterized leaf call" property this page's
  int→fp16 tie behavior invokes.
