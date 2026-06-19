# ISA Batch 13 — fp32 Convert (sp_cvt)

This is the per-instruction reference for the **single-precision (fp32) convert slice** of the
Vision-Q7 *Cairo* (`ncore2gp`) ISA: every opcode that moves a value **to or from fp32**
(`N_2XF32`, 16 lanes, 1-8-23 bias 127) — `int32 ↔ fp32` with a Q-format scale exponent
(`float`/`ufloat`/`trunc`/`utrunc`), the five fp32 **round-to-integral** ops that keep the result
in fp32 (`firound`/`ficeil`/`fifloor`/`firint`/`fitrunc`), and the **fp16 ↔ fp32 widen** that
lands on the fp32 side (`cvtf32f16`, `cvtf32nxf16`). It owns **23 shipped mnemonics**, summing to
**138 placements** of the certified-perfect `12569` cover
([coverage tally](../core/coverage-tally.md)). The semantic group the binary itself names for this
family is `ivpep_sem_sp_cvt` — the literal `opnd_ivpep_sem_sp_cvt_*` operand strings in
`libisa-core.so` (`strings -a | rg sem_sp_cvt`, this pass) are where the page title comes from.

This page is the **value-semantics-and-encoding** companion to the control-view
[fp sub-ISA page](../core/fp-sub-isa.md): that page owns the FCR/FSR round-mode/exception state
(the 2-bit architectural RoundMode, the **third internal convert-core bit** that selects the
`away` mode, the five `SHARED_OR` sticky flags, the two-level RNE-vs-RZ rounding model); **this**
page owns *what each convert opcode computes, bit-exact, and how it is encoded into a slot*. Every
value claim below was **proven by execution** — computed by calling the matching `module__xdref_`
leaf in `libfiss-base.so` live via `ctypes` (license-free), the binary acting as the arbiter of its
own arithmetic. `[HIGH/OBSERVED·exec]` throughout where a value fact is stated, per the
[confidence model](../../reference/confidence-model.md).

Two batch boundaries are sharp and stated up front (§8 expands them): the **fp16-side** narrow
(`cvtf16f32`/`cvtf16n_2xf32`) and the fp16 round/int converts (`*nxf16`, `float16`/`trunc16`) are
**[B20 hp-cvt](b20-hp-cvt.md)**, *not* here — the partition classifier routes a convert mnemonic
to B20 if it is fp16-typed, to B13 if fp32-typed
([template §4.2](template-and-partition.md#42-how-each-batchs-membership-is-derived-the-classifier)).
The **integer wide-accumulator repacks** (`cvt32s24`, `cvt48snx32`, `cvt96u64`, … — 38 mnemonics in
the **`S1_Ld`** slot) are *not* fp converts at all and belong to the `wvec` pack family
([B10](b10-wvec-pack.md)/B22). The fp **arithmetic** (`add`/`mul`) is [B02](b02-vec-alu-fp.md); the
transcendental seeds (`recip0`/`rsqrt0`/`nexp`) are [B14](b14-hp-lookup.md)/[B15](b15-sp-lookup.md).

> **NOTE — the two binaries and the address arithmetic this page uses.** Encoding side:
> `libisa-core.so` (sha256 `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`,
> 9,690,712 B, not stripped; re-confirmed this pass). `.text` (`0x312c10`) and `.rodata`
> (`0x3b6e40`) are **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) and
> `.data` (VMA `0x764040` ↔ file `0x564040`) carry a **`0x200000`** per-binary delta
> (`readelf -SW`, re-read this pass — *not* libtpu's `0x400000`). Value side: `libfiss-base.so`
> (12,330,016 B); its `module__xdref_*` leaves are `.text`, VMA == file. Both are in
> `extracted/` (gitignored; reach with an absolute path or `fd --no-ignore`). `[HIGH/OBSERVED]`

---

## 1. Scope, datapath, and key facts

Every fp32-convert op reads/writes the **`vec`** file (idx 2, 512 b × 32; fp32 occupies 16 lanes of
32 b) and is placed in the **`S3_ALU`** slot — the *same* slot family the
[fp Vector-ALU slice (B02)](b02-vec-alu-fp.md) uses, confirmed by `nm` showing every convert
placement at `Slot_{f0,f1,f2,f3,f7}_s3_alu` + `Slot_n0_s3_alu` (this pass). The round-honoring
forms thread the FCR `RoundMode` and post the FSR `SHARED_OR` sticky flags; the operand model and
the FCR/FSR threading are read from the iclass stateArg/arg tables.

| Fact | Value | Source |
|---|---|---|
| Data file | `vec` idx 2, **512 b × 32**; fp32 = 16 lanes (1-8-23, bias 127) | [register-files §3](../core/register-files.md) |
| Canonical encode slot | **`S3_ALU`** — 5 wide (`F0/F1/F2/F3/F7`, 16 B) + 1 narrow (`N0`, 8 B) | `nm \| rg Slot_.*_s3_alu` |
| Semantic group | **`ivpep_sem_sp_cvt`** (and `sp_lookup` for trunc/utrunc) | `opnd_ivpep_sem_sp_cvt_*` strings |
| Mnemonics this batch | **23** | per-mnemonic `nm` census (§7) |
| Placements this batch | **138** (23 × 6) | §7 |
| Q-format scale operand | **`i_imm5`** (5-bit, 0..31) on `float`/`ufloat`/`trunc`/`utrunc` | `Iclass_*_S_args` operand[2] |
| Round-honoring ops | `firint` (FCR mode), all int↔fp (implicit), narrow widen-pair | leaf threads round / scale arg |
| Fixed-mode ops | `firound` (away-from-zero), `ficeil` (+∞), `fifloor` (−∞), `fitrunc` (→0) | leaf body has no mode arg |
| Saturating ops | `trunc`/`utrunc` (clamp to int range; NaN→max) | §4.2 live probe |
| FSR-raising | Inexact (all rounding forms); Overflow/Invalid (trunc saturate) | iclass stateArgs + live flag probe |
| Writeback stage | **13** (FLOAT/round/widen) · **12** (TRUNC); vec read @10 | `libcas-core.so` ISS `_issue` (§9.1) |
| Value oracle | `libfiss-base.so` `module__xdref_*` leaves, ctypes | [confidence §6.1](../../reference/confidence-model.md) |

> **GOTCHA — `sp_cvt` is the *output*-fp32 family, by name root, not by "touches fp32".** The
> classifier cut is **the mnemonic root, first-match-wins**: `cvtf32*` (root says *produce fp32*) is
> B13; `cvtf16*` (root says *produce fp16*) is B20 — even though both legs of a widen/narrow touch
> *both* widths. `cvtf32f16` (widen, fp16→fp32) is here; its inverse `cvtf16f32` (narrow,
> fp32→fp16) is [B20](b20-hp-cvt.md). Likewise `floatn_2x32`/`truncn_2xf32` (the `_2x32`/`_2xf32`
> int32↔fp32 pair) are here, while `float16nx16`/`trunc16nxf16` (the int16↔fp16 pair) are B20.
> A reimplementer that partitions by "any operand is fp32" double-counts the widen/narrow pair; the
> name root is the non-ambiguous key. `[HIGH/OBSERVED]`

---

## 2. The roster

Every fp32-convert mnemonic this batch owns, with its encoding read at the **canonical `F1_S3_ALU`
slot** (the `movl` immediate in the `Opcode_ivp_<mnem>_Slot_f1_s3_alu_encode` thunk, byte-exact this
pass) and its operand model read from the iclass `args`/`stateArgs` tables. `vec lane in/out` gives
the convert's source→dest lane width; `bytes` is the FLIX format length (16 B for the wide
`F0/F1/F2/F3/F7` slots, 8 B for the narrow `N0`). The `t` mnemonics are the **predicated/throttle**
forms (a separate iclass, `*T`, with a guard-lane input); their `word0` is a *different* selector,
not a bit-flip of the base form.

> **GOTCHA — the opcode-selector immediate is SLOT-LOCAL, not a global opcode number** (the same
> rule [B02 §2 found](b02-vec-alu-fp.md#2-the-roster)). `firoundn_2xf32` carries
> `0x84ca0070` in `F0_S3_ALU`, `0x2e0000b0` in `F1_S3_ALU`, `0x0e0000b0` in `F2_S3_ALU`,
> `0x00868100` in `F3_S3_ALU`, `0x2e0000b0` in `F7_S3_ALU`, and `0x62888071` in `N0_S3_ALU` (all
> byte-exact this pass) — **no additive or single-bit relation** between slots (the `F0` form even
> differs in the high byte). This page tabulates one canonical slot (`F1_S3_ALU`) so the selectors
> are comparable; a reimplementer's assembler reads the placement it actually targets from
> `Opcode_<mnem>_Slot_<that-slot>_encode`. `[HIGH/OBSERVED]`

### 2.1 int32 ↔ fp32 with a Q-format scale (`float`/`ufloat`/`trunc`/`utrunc`)

| mnemonic | fmt·slot | opcode-sel imm (F1_S3) | vec lane in→out | bytes | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_floatn_2x32` | F1·S3_ALU (+5) | `0x27803000` | int32 → fp32 (16×32b) | 16/8 | `fp32(int32) · 2^(−imm5)` (signed Q-format) | H/OBS·exec |
| `ivp_floatn_2x32t` | F1·S3_ALU (+5) | `0x27000000` | int32 → fp32, predicated | 16/8 | `float`, guard-lane masked | H/OBS |
| `ivp_ufloatn_2x32` | F1·S3_ALU (+5) | `0x27803400` | uint32 → fp32 | 16/8 | `fp32(uint32) · 2^(−imm5)` (unsigned) | H/OBS·exec |
| `ivp_ufloatn_2x32t` | F1·S3_ALU (+5) | `0x27000400` | uint32 → fp32, predicated | 16/8 | `ufloat`, guard-masked | H/OBS |
| `ivp_truncn_2xf32` | F1·S3_ALU (+5) | `0x25a18000` | fp32 → int32 | 16/8 | `int32(fp32 · 2^imm5)` toward-zero, **saturating** | H/OBS·exec |
| `ivp_truncn_2xf32t` | F1·S3_ALU (+5) | `0x28080000` | fp32 → int32, predicated | 16/8 | `trunc`, guard-masked | H/OBS |
| `ivp_utruncn_2xf32` | F1·S3_ALU (+5) | `0x25a98000` | fp32 → uint32 | 16/8 | `uint32(fp32 · 2^imm5)`, saturating | H/OBS·exec |
| `ivp_utruncn_2xf32t` | F1·S3_ALU (+5) | `0x28088000` | fp32 → uint32, predicated | 16/8 | `utrunc`, guard-masked | H/OBS |

### 2.2 fp32 round-to-integral (result stays fp32)

| mnemonic | fmt·slot | opcode-sel imm (F1_S3) | vec lane in→out | bytes | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_firoundn_2xf32` | F1·S3_ALU (+5) | `0x2e0000b0` | fp32 → fp32 | 16/8 | round to integral, **half-away-from-zero** (fixed) | H/OBS·exec |
| `ivp_firoundn_2xf32t` | F1·S3_ALU (+5) | `0x27005030` | fp32 → fp32, predicated | 16/8 | `firound`, guard-masked | H/OBS |
| `ivp_ficeiln_2xf32` | F1·S3_ALU (+5) | `0x2e000020` | fp32 → fp32 | 16/8 | round to integral toward **+∞** (ceil) | H/OBS·exec |
| `ivp_ficeiln_2xf32t` | F1·S3_ALU (+5) | `0x27005090` | fp32 → fp32, predicated | 16/8 | `ficeil`, guard-masked | H/OBS |
| `ivp_fifloorn_2xf32` | F1·S3_ALU (+5) | `0x2e000030` | fp32 → fp32 | 16/8 | round to integral toward **−∞** (floor) | H/OBS·exec |
| `ivp_fifloorn_2xf32t` | F1·S3_ALU (+5) | `0x27005020` | fp32 → fp32, predicated | 16/8 | `fifloor`, guard-masked | H/OBS |
| `ivp_firintn_2xf32` | F1·S3_ALU (+5) | `0x2e0000a0` | fp32 → fp32 | 16/8 | round to integral, **FCR RoundMode** (RNE default) | H/OBS·exec |
| `ivp_firintn_2xf32t` | F1·S3_ALU (+5) | `0x270050a0` | fp32 → fp32, predicated | 16/8 | `firint`, guard-masked | H/OBS |
| `ivp_fitruncn_2xf32` | F1·S3_ALU (+5) | `0x2e000021` | fp32 → fp32 | 16/8 | round to integral toward **0** (truncate) | H/OBS·exec |
| `ivp_fitruncn_2xf32t` | F1·S3_ALU (+5) | `0x270050b0` | fp32 → fp32, predicated | 16/8 | `fitrunc`, guard-masked | H/OBS |

### 2.3 fp16 → fp32 widen (lands on the fp32 side)

| mnemonic | fmt·slot | opcode-sel imm (F1_S3) | vec lane in→out | bytes | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_cvtf32f16` | F1·S3_ALU (+5) | `0x27d050f0` | fp16 → fp32 | 16/8 | exact widen (mantissa 10→23, bias 15→127) | H/OBS·exec |
| `ivp_cvtf32nxf16_0` | F1·S3_ALU (+5) | `0x27e050f0` | fp16[lo] → fp32 | 16/8 | widen low-half of 32-lane fp16 vector | H/OBS·exec |
| `ivp_cvtf32nxf16_1` | F1·S3_ALU (+5) | `0x27f050f0` | fp16[hi] → fp32 | 16/8 | widen high-half of 32-lane fp16 vector | H/OBS·exec |
| `ivp_cvtf32nxf16t_0` | F1·S3_ALU (+5) | `0x27005080` | fp16[lo] → fp32, predicated | 16/8 | widen low-half, guard-masked | H/OBS |
| `ivp_cvtf32nxf16t_1` | F1·S3_ALU (+5) | `0x27005010` | fp16[hi] → fp32, predicated | 16/8 | widen high-half, guard-masked | H/OBS |

> **QUIRK — `cvtf32nxf16` is a *two-instruction* widen because fp32 has half the lanes of fp16.**
> A `vec` register holds **32** fp16 lanes but only **16** fp32 lanes, so widening a full fp16
> vector to fp32 cannot fit in one destination — it splits into `_0` (low 16 fp16 lanes → one fp32
> vector) and `_1` (high 16 fp16 lanes → a second fp32 vector). The `_0`/`_1` leaves prove it
> structurally: `module__xdref_cvtf32_1_32f_32f_0` (`@0x5b7f10`) does `movzwl %si` (mask to the low
> 16 bits = pick the fp16 lane) and then **tail-calls** `module__xdref_cvtf32_1_32f_16f` (the single
> widen core, `@0x5b77f0`) — disassembled this pass. `cvtf32f16` is the scalar/single-lane widen;
> `cvtf32nxf16_{0,1}` are the lane-extracting vector halves over the *same* widen core. A
> reimplementer emits a `_0`/`_1` pair to widen 32 fp16 lanes into two fp32 registers.
> `[HIGH/OBSERVED]`

---

## 3. int32 → fp32 — `float` / `ufloat` and the Q-format scale

### 3.1 The operand model — a 5-bit scale exponent, not a round mode

`Iclass_FLOAT_S_args` (`@0x854100`, file `0x654100`) lists **three** operands, read verbatim this
pass:

```
operand[0]  mode='o'  opnd_ivpep_sem_sp_cvt_vt     -> vec destination (fp32)
operand[1]  mode='i'  opnd_ivpep_sem_sp_cvt_vr     -> vec source (int32)
operand[2]  mode='i'  opnd_ivpep_sem_sp_cvt_i_imm5 -> a 5-bit immediate (0..31)
```

The third operand is the **`i_imm5` Q-format scale exponent**. Its slot-local encoding is a
*scattered* field: `Field_fld_ivpep_sem_sp_cvt_i_imm5_Slot_f1_s3_alu_get` (`@0x336090`) reads
`bit0` from `word0[0]` and `bits[4:1]` from `word0[4:1]` (the body is `mov (%rdi),%edx;
mov %edx,%eax; and $0x1,%edx; shr $0x3,%eax; and $0x1e,%eax; or %edx,%eax; ret` — so imm5 =
`word0[0] | (word0[4:1])`). Read the bit-window from the `_get` thunk, never by subtracting offsets
([template §3.2 GOTCHA](template-and-partition.md#32-the-roster-row-record-format-the-observed-ground-truth-a-row-distills)).
`[HIGH/OBSERVED]`

### 3.2 Annotated value model — `int32 → fp32 · 2^(−imm5)`

The value leaf is `module__xdref_float_1_32f_32_32_2` (`@0x87aec0`) — an integer-only soft-float (no
hardware-FP x86 insn; it cracks the int with `shr $0x17`/`shr $0x18` exponent math and a `0x1fdf`
round bias). Its ABI, recovered by disassembly + execution, takes the **scale** as its 3rd argument:

```c
// ivp_floatn_2x32 :  out = (fp32) in_i32 / 2^scale     (Q(31-scale).scale fixed-point -> fp32)
// ABI (confirmed by execution): result lands in the THIRD output pointer.
//   void float_i32(long scratch, int32 in, int scale, uint *flag0, uint *flag1, uint *result);
uint32_t float_f32(int32_t x, int scale /*0..31*/) {
    // 1. magnitude+sign of x; 2. normalize to 1.f form (leading-zero-count);
    // 3. exponent = (31 - lzc) - scale + 127;  4. round mantissa to 23 bits;
    // 5. assemble sign|exp|mantissa.  scale==0 => plain int->float.
}
```

**Q-format scale certificate (proven by execution).** The 3rd argument is a **right-shift /
divide-by-2^scale**, exactly the fixed-point binary-point position:

```
float(2^23,        scale=0) = 8388608.0      (= 2^23, ratio 1.0)
float(2^23,        scale=1) = 4194304.0      (= 2^22, ratio 0.5)
float(2^23,        scale=2) = 2097152.0      (= 2^21, ratio 0.25)
float(2^23,        scale=3) = 1048576.0      (ratio 0.125)   ...halves each step
```

So `ivp_floatn_2x32` interprets its int32 source as a **Q(31−s).s fixed-point** number and yields
`value · 2^(−s)`. `imm5 = 0` is the plain `int→float`. `ufloatn_2x32`
(`module__xdref_ufloat_1_32f_32_32_2` `@0x87b0d0`) is identical with an unsigned source — high bit
is value, not sign. `[HIGH/OBSERVED·exec]`

### 3.3 Precision-loss rounding above 2^24 — and the **RZ leaf-default**

fp32 has a 24-bit significand, so integers `> 2^24` are not all representable and the convert must
round. Driving the leaf at the tie points (`scale=0`):

```
float(2^24)   = 16777216.0  (exact, inexact=0)
float(2^24+1) = 16777216.0  (inexact=1)   float(2^24+3) = 16777218.0  (inexact=1)
float(2^24+5) = 16777220.0                float(2^24+7) = 16777220.0  (?)
above 2^24 the fp32 grid step is 2: representable = ...216, 218, 220...
float(16777217)=16777216  float(16777219)=16777218  float(16777221)=16777220   <-- ALL floor-to-grid
```

> **CORRECTION — the int→fp32 leaf, called with the scale argument supplied, *truncates ties toward
> zero* (RZ), not ties-to-even (RNE).** `16777219` is the exact midpoint of the representable pair
> `{16777218, 16777220}`; RNE-ties-to-even would pick the *even-mantissa* member `16777220`, but the
> leaf returns `16777218` — the **floor-to-grid** value, for every odd tie tested
> (`16777217→...216`, `16777219→...218`, `16777221→...220`, `33554435→...432`). This is **not** a
> bug and **not** a contradiction of the architectural RNE default — it is exactly the
> **two-level rounding rule** the [fp sub-ISA §3.2 CORRECTION](../core/fp-sub-isa.md#32-the-encoding--proven-by-execution)
> and the [confidence model §6.1](../../reference/confidence-model.md) pin: the *architectural FCR
> reset is RNE* (proven there 3000/3000), but the *fiss value leaf, when its round-mode is left
> unthreaded* — and the `float`/`ufloat` leaf threads a **scale** in that argument slot, not a
> 2-bit mode — runs its **default RZ truncate** path (the "RZ-default, proven-by-execution"
> property). The hardware `ivp_floatn_2x32` with FCR=RNE rounds-to-even; the *host leaf with no mode
> threaded* truncates. Both are true at their own level; this page does **not** re-litigate the
> split. A reimplementer modeling the *hardware* op rounds with the live FCR RoundMode; one driving
> the *value leaf* as an oracle either supplies a mode or expects RZ. `[HIGH/OBSERVED·exec]`

**uint32 high-bit + precision certificate.**

```
ufloat(2^31=0x80000000) = 2147483648.0  (exact, inexact=0)   // the bit a SIGNED float would mis-read as -
ufloat(0xffffffff)      = 4294967040.0  (= 2^32 - 256, inexact=1; the low 8 bits round off)
```

`ufloat(0xffffffff)` keeps the value positive and near 2^32 (a *signed* `float` of the same bits
would yield `-1.0`); the low byte is lost to the 24-bit significand with the Inexact flag set.
`[HIGH/OBSERVED·exec]`

---

## 4. fp32 → int32 — `trunc` / `utrunc` (saturating, with the NaN→max convention)

### 4.1 The operand model and toward-zero rounding

The value leaves are `module__xdref_trunc_1_1_32_32f_32` (`@0x87b240`) and
`module__xdref_utrunc_1_1_32u_32f_32` (`@0x87b510`). Their iclass is `TRUNC_S`/`UTRUNC_S`, whose
operands are named under the **`sp_lookup`** semantic group in the binary (a naming quirk —
fp32→int trunc is grouped with the lookup family, not `sp_cvt`, though functionally it is a
convert). Operand[2] is again a **scale immediate** (`i_i…`), so `truncn_2xf32` is `int32(fp32 ·
2^imm5)` — the fixed-point *quantize* inverse of `float`. ABI (recovered by execution): result in
the **third** output pointer, inexact in the second, overflow in the first.

```c
// ivp_truncn_2xf32 :  out = sat_int32( trunc_toward_zero( in_fp32 * 2^scale ) )
//   void trunc_i32(long, uint in_fp32, int scale, uint *ovf, uint *inexact, uint *result, ...);
```

**toward-zero + scale certificate.**

```
trunc(2.9)  =  2   trunc(-2.9) = -2   (truncate toward zero, both signs)
trunc(0.5)  =  0  (inexact=1)         trunc(-0.5) =  0  (inexact=1)
trunc(4.0, scale=0)=4  scale=1=>8  scale=2=>16  scale=3=>32   // scale is a LEFT-shift (×2^scale)
```

Note the scale on `trunc` is the **inverse** of `float`'s: `float` divides by `2^scale` (int→fp),
`trunc` multiplies by `2^scale` (fp→int), so `trunc(float(x, s), s) == x` for in-range `x` — a
matched Q-format encode/decode pair. `[HIGH/OBSERVED·exec]`

### 4.2 Saturation and the NaN/Inf → int convention (proven by execution)

The decisive reimplementation facts are what happens **out of int32 range** and on **NaN/Inf** —
the platform's convention is **not** the x86 `cvttss2si` "indefinite integer `0x80000000`" rule:

| fp32 input | `trunc` result | overflow flag | rule |
|---|---|---|---|
| `2^31` (0x4f000000) | `0x7fffffff` = **INT_MAX** | 1 | positive overflow → **+saturate** |
| `1e30` | `0x7fffffff` = INT_MAX | 1 | positive overflow → +saturate |
| `−1e30` | `0x80000000` = **INT_MIN** | 1 | negative overflow → **−saturate** |
| `+inf` | `0x7fffffff` = INT_MAX | 1 | +∞ → +saturate |
| `−inf` | `0x80000000` = INT_MIN | 1 | −∞ → −saturate |
| **NaN** | `0x7fffffff` = **INT_MAX** | 1 | **NaN → +saturate (INT_MAX)** |
| `INT_MIN` exactly | `0x80000000` | 0 | in-range, exact (no overflow) |

> **QUIRK — `trunc(NaN) = INT_MAX (0x7fffffff)`, not 0 and not INT_MIN.** The Vision-Q7 fp32→int
> convert saturates **NaN to the positive maximum**, with the Overflow flag set — unlike x86
> (`cvttss2si` returns the "integer indefinite" `0x80000000` for NaN *and* both overflows), and
> unlike ARM `FCVTZS` (NaN→0). Out-of-range positives and `+inf` and NaN all clamp to `0x7fffffff`;
> out-of-range negatives and `−inf` clamp to `0x80000000`. A reimplementer that ports an x86
> `cvttss2si` lowering verbatim gets the NaN and the two overflow directions **all three** wrong.
> `[HIGH/OBSERVED·exec]`

**`utrunc` — the negative-input trap.** The unsigned form has a sharper edge: a negative finite
input is **not** clamped to 0.

```
utrunc(-0.0) = 0            utrunc(5.7) = 5  (inexact=1)
utrunc(-2.9) = 0xfffffffe   // NOT 0 — the negative-of-(uint) wrap, with overflow flag set
utrunc(2^32) = 0xffffffff = UINT_MAX  (overflow)   utrunc(+inf) = 0xffffffff   utrunc(NaN) = 0xffffffff
utrunc(-inf) = 0x80000000
```

> **GOTCHA — `utrunc` of a *small negative* finite does not clamp to 0; it produces the two's-
> complement-style negative-magnitude word with Overflow set.** `utrunc(-2.9) = 0xfffffffe`
> (= `(uint32)(-2)`), not `0`. Only `−inf` lands at `0x80000000` and `+inf`/`NaN`/`≥2^32` at
> `0xffffffff`. A clamp-to-`[0, 2^32)` model (the naive "unsigned saturate") is wrong on the small-
> negative path; the hardware passes the truncated signed magnitude through to the unsigned word.
> Model `utrunc` as: truncate toward zero, then if the result exceeds `[0, 2^32)` saturate, **else**
> reinterpret the signed truncation as `uint32` (so `−2 → 0xfffffffe`). `[HIGH/OBSERVED·exec]`

---

## 5. fp32 round-to-integral — `firound` / `ficeil` / `fifloor` / `firint` / `fitrunc`

These five keep the result **in fp32** (round to an integral fp32 value, no int conversion). They
split into **four fixed-mode** ops with no round argument and **one FCR-mode** op (`firint`). The
fixed-mode leaves (`module__xdref_{firound,ficeil,fifloor,fitrunc}_1_32f_32f`,
`@0x87d760/87d3c0/87d5a0/87d8e0`) have a 4-arg ABI `(scratch, in, *flag, *result)` with the fp32
result in the **second** output pointer; `firint` (`module__xdref_firint_1_1_32f_32f_2`, `@0x87d9d0`)
adds the round-mode argument and writes its result to the **third** pointer (ABI confirmed by the
store sites `(%r8)`/`(%r9)` and by execution).

### 5.1 The behavior table (proven by execution)

```
   in  | firound  ficeil  fifloor  fitrunc | firint(RNE)
 ------+----------------------------------+------------
  2.3  |   2.0      3.0     2.0      2.0   |    2.0
  2.5  |   3.0      3.0     2.0      2.0   |    2.0     <- the key tie row
  2.7  |   3.0      3.0     2.0      2.0   |    3.0
  3.5  |   4.0      4.0     3.0      3.0   |    4.0
 -2.3  |  -2.0     -2.0    -3.0     -2.0   |   -2.0
 -2.5  |  -3.0     -2.0    -3.0     -2.0   |   -2.0
 -0.5  |  -1.0     -0.0    -1.0     -0.0   |   -0.0     <- signed-zero preserved
```

`ficeil` = round toward **+∞**, `fifloor` = toward **−∞**, `fitrunc` = toward **0** — all
proven on both signs including the signed-zero edges (`fifloor(-0.5) = -1.0`, `ficeil(-0.5) = -0.0`,
`fitrunc(-0.5) = -0.0`). Inf and NaN pass through unchanged; an sNaN input is **quieted**
(`firound(0x7f800001) = 0x7fc00001`). `[HIGH/OBSERVED·exec]`

### 5.2 `firound` (fixed half-away) vs `firint` (FCR mode) — the `away` bit, proven live

> **QUIRK — `firound` is the only convert reachable to the *fifth* round code `away`; `firint`
> honors the 2-bit FCR mode.** Driving the two on the same tie inputs:
>
> ```
>    in  | firint(RNE,ties-even) | firound (fixed)
>  ------+-----------------------+----------------
>   0.5  |        0.0            |    1.0
>   2.5  |        2.0            |    3.0
>   4.5  |        4.0            |    5.0
>  -2.5  |       -2.0            |   -3.0
> ```
>
> `firint` resolves `0.5→0`, `2.5→2`, `4.5→4` — **round-half-to-even** under FCR `RoundMode=RNE`,
> with the directed modes available (`firint(2.5, RU)=3`, `RD=2`, `RZ=2`, sweep this pass);
> `firound` resolves `0.5→1`, `2.5→3`, `-2.5→-3` — **round-half-away-from-zero**, the C99
> `round()`/`roundf()` rule, **independent of FCR**. This is the architectural surfacing of the
> **third (3-bit) convert-core round field** the
> [fp sub-ISA §3.1](../core/fp-sub-isa.md#31-the-2-bit-fcr-field-vs-the-3-bit-convert-core-field)
> documents: code `100 = away` is **used only by `FIROUND`** and is **not reachable through
> `wur.fcr`'s 2-bit write**. A reimplementer maps `firound` to a hard-wired half-away rounder and
> `firint` to the FCR-mode rounder; conflating them is wrong on every `.5` input.
> `[HIGH/OBSERVED·exec]`

### 5.3 The C model

```c
// ivp_firintn_2xf32 :  out = round_to_integral_fp32(in, FCR.RoundMode)   (stays fp32)
//   RNE(default): ties-to-even ; RZ: toward 0 ; RU: toward +inf ; RD: toward -inf
//   inf/nan pass through; sNaN -> qNaN (quieted); sets Inexact iff a fraction was discarded.
// ivp_firoundn_2xf32 : out = round_to_integral_fp32(in, AWAY)  // half-away, FCR-independent
// ivp_ficeiln  : RU-equivalent (always toward +inf)   ivp_fifloorn : RD-equivalent (toward -inf)
// ivp_fitruncn : RZ-equivalent (always toward 0)
```

`ficeil`/`fifloor`/`fitrunc` are the three *directed* round-to-integral ops with the mode baked in
(equivalent to `firint` under RU/RD/RZ respectively but not reading FCR); `firint` is the one that
*reads* FCR; `firound` is the one that *cannot* be expressed by FCR (the `away` code). All five are
fp32→fp32. `[HIGH/OBSERVED·exec]`

---

## 6. fp16 → fp32 widen — `cvtf32f16` / `cvtf32nxf16`

The widen core is `module__xdref_cvtf32_1_32f_16f` (`@0x5b77f0`). fp16 (1-5-10, bias 15) → fp32
(1-8-23, bias 127) is an **exact** operation for every finite fp16 (23 ≥ 10 mantissa bits, so no
rounding; only the exponent rebias and a possible denormal-to-normal normalization). ABI: result in
the **second** output pointer (`(scratch, h16, *flag, *result, ...)`).

### 6.1 Exact widen certificate (proven by execution)

```
widen(1.0   =0x3c00) -> 0x3f800000 = 1.0          widen(2.5  =0x4100) -> 0x40200000 = 2.5
widen(65504 =0x7bff) -> 0x477fe000 = 65504.0      widen(0.5  =0x3800) -> 0x3f000000 = 0.5
widen(6.1e-5=0x03ff) -> 0x387fc000 ~ 6.1e-05      // fp16 SUBNORMAL widened to a NORMALIZED fp32
widen(+inf  =0x7c00) -> 0x7f800000 = +inf         widen(NaN  =0x7e00) -> 0x7fc00000 = NaN
```

The fp16 *subnormal* `0x03ff` (the largest fp16 denormal) widens to a **normalized** fp32
(`0x387fc000`) — because fp32's exponent range easily holds fp16's denormal magnitudes, the widen
core leading-zero-normalizes the subnormal into the fp32 normal range. inf and NaN map straight
across (exp all-ones; NaN payload preserved into the fp32 NaN). `[HIGH/OBSERVED·exec]`

### 6.2 The fp16 ↔ fp32 round-trip — exact inverse over 2009 values

To certify that widen (B13) and narrow (B20) are an exact inverse pair on representable values, I
drove `widen` (`cvtf32_1_32f_16f`) followed by the B20-side `narrow`
(`module__xdref_cvtf16_1_1_1_1_32f_32f_2_0`, `@0x5b7f90`, result in its 5th output pointer) over a
sweep:

```
round-trip narrow(widen(h)) over 2009 fp16 values (the 9 special encodings + 2000 random):
    mismatches (excluding NaN-payload) = 0
```

Every non-NaN fp16 survives a widen→narrow round-trip bit-exactly — the widen is lossless and the
narrow recovers the original encoding. (NaN payloads differ because the narrow canonicalizes to
`0x7e00`; excluded.) This is an OBSERVED-by-execution certificate that the fp16↔fp32 hub
([fp sub-ISA §6](../core/fp-sub-isa.md#6-the-fp-datatype-set)) is a clean reversible pair.
`[HIGH/OBSERVED·exec]`

### 6.3 The narrow side (B20) — overflow→inf, for boundary context

Although the **narrow** (`cvtf16f32`/`cvtf16n_2xf32`) is owned by [B20](b20-hp-cvt.md), its overflow
rule is the directed-round rule a reimplementer of *either* page must match, and it round-trips
against the widen here, so it is stated for completeness (proven by execution this pass):

```
narrow(70000 > fp16max) RNE=0x7c00(inf) RZ=0x7bff(max-fin) RU=0x7c00 RD=0x7bff   // directed-round overflow
narrow(65520)           RNE=0x7c00(inf, rounds up past max)   narrow(65504)=0x7bff (finite)
narrow(6e-8)            RNE=0x0001 (gradual underflow to smallest fp16 subnormal; RU bumps to 0x0002)
narrow(-0.0)            = 0x8000 (sign preserved)
```

Overflow under RNE/toward-∞ → `±inf`; under RZ/RD (away from the infinity) → `±0x7bff` max-finite —
the same [directed-round-overflow rule](../core/fp-sub-isa.md#51-what-is-full-ieee-754) the
[B02 `mul` overflow](b02-vec-alu-fp.md#33-mul--grs--sticky-single-rounding) follows. The *encoding*
and roster of the narrow ops are B20's; this page owns only the widen and proves the round-trip.
`[HIGH/OBSERVED·exec]`

---

## 7. Batch tally — every fp32-convert mnemonic vs `nm`

The 23 mnemonics and their placement counts, re-counted this pass with
`nm libisa-core.so | rg -c 'Opcode_ivp_<mnem>_Slot_.*_encode'` (the only legitimate count method,
per [coverage tally](../core/coverage-tally.md)):

| group | mnemonics (placements each = 6) | count |
|---|---|---|
| int↔fp32 + scale | `floatn_2x32` `floatn_2x32t` `ufloatn_2x32` `ufloatn_2x32t` `truncn_2xf32` `truncn_2xf32t` `utruncn_2xf32` `utruncn_2xf32t` | 8 |
| round-to-integral | `firoundn_2xf32` `firoundn_2xf32t` `ficeiln_2xf32` `ficeiln_2xf32t` `fifloorn_2xf32` `fifloorn_2xf32t` `firintn_2xf32` `firintn_2xf32t` `fitruncn_2xf32` `fitruncn_2xf32t` | 10 |
| fp16→fp32 widen | `cvtf32f16` `cvtf32nxf16_0` `cvtf32nxf16_1` `cvtf32nxf16t_0` `cvtf32nxf16t_1` | 5 |

```
BATCH TOTAL : 23 mnemonics, 138 placements  (= 23 × 6, every convert is 5 wide F-slots + 1 N0)
```

This `138` is a slice of the `12569`-placement certified cover; it rolls into the `1534 ↔ 12569`
shipped pairing, **never** `12642`
([template §6.2](template-and-partition.md#62-the-placement-roll-up-pairs--12569-never-12642)).

**Value-leaf grounding.** Every value-bearing mnemonic resolves to a `module__xdref_` leaf that was
**driven live this pass**: `float`/`ufloat` (`@0x87aec0`/`0x87b0d0`), `trunc`/`utrunc`
(`@0x87b240`/`0x87b510`), `firound`/`ficeil`/`fifloor`/`fitrunc`/`firint`
(`@0x87d760`/`0x87d3c0`/`0x87d5a0`/`0x87d8e0`/`0x87d9d0`), `cvtf32f16` widen (`@0x5b77f0`) and its
`_0`/`_1` lane halves (`@0x5b7f10`/`0x5b7f50`). The leaf count for this batch is **~12 distinct
value functions** (the `t`-variants share the base leaf — predication is an encoding/lane-mask
property, not a different value function), comfortably inside the `864` value-leaf denominator
([coverage tally §5](../core/coverage-tally.md)). `[HIGH/OBSERVED·exec]`

> **NOTE — ungrounded / deferred items (honestly flagged).**
> (1) The **`t` (predicated) variants** were grounded at the *encoding* level (distinct `word0`
> selectors, the `*T` iclass with a guard-lane operand) but their value semantics were **not**
> swept live — they reuse the base leaf gated by a `vbool` mask, so their value is `[HIGH/INFERRED]`
> (= base op masked), their encoding `[HIGH/OBSERVED]`. (2) Per-instruction **cycle latency** IS
> grounded this pass — see §9.1: the convert writeback stage was read from the `libcas-core.so` ISS
> `..._issue` functions directly (it is **stage 13** for the FLOAT/round family incl. the widen, and
> **stage 12** for the TRUNC family), so latency is `[HIGH/OBSERVED]`, *not* a longer convert-specific
> latency — the converts share the fp-ALU pipeline class. (3) The exact `imm5`
> scale field placement in the **wide** vs **narrow** slot differs per-format; §3.1 reads it from the
> `F1_S3_ALU` `_get` thunk — the per-format drift is `[MED/OBSERVED]` (one slot read, others
> inferred to follow the same scattered pattern).

---

## 8. Batch boundary — what this page does NOT own

| neighbor op(s) | owner page | why not here |
|---|---|---|
| `cvtf16f32`, `cvtf16n_2xf32_0`/`t` (fp32 → fp16 **narrow**) | [B20 hp-cvt](b20-hp-cvt.md) | name root `cvtf16` = *produce fp16*; narrow is the fp16-side leg (round-trips against §6.2 here) |
| `float16nx16`, `ufloat16nx16`, `trunc16nxf16`, `utrunc16nxf16` (int16 ↔ **fp16**) | [B20 hp-cvt](b20-hp-cvt.md) | int↔**fp16** pair, fp16-typed → B20 |
| `firoundnxf16`, `ficeilnxf16`, `fifloornxf16`, `firintnxf16`, `fitruncnxf16` (fp16 round-to-integral) | [B20 hp-cvt](b20-hp-cvt.md) | fp16-typed round family — the `*nxf16` siblings of §5 |
| `cvt32s24`, `cvt48snx32`, `cvt96u64`, `cvtg16nx48`, … (38 ops, **S1_Ld** slot) | [B10 wvec-pack](b10-wvec-pack.md) / B22 | **integer** wide-accumulator (24/48/96-bit) repacks — pure mask/move leaves (`and $0xffffff`), *not* fp converts; live in the Ld slot, not S3_ALU |
| `addn_2xf32`, `muln_2xf32`, `subn_2xf32` (fp32 arithmetic) | [B02 vec-alu-fp](b02-vec-alu-fp.md) | binary fp arithmetic, not a width/format convert |
| `recip0n_2xf32`, `rsqrt0n_2xf32`, `nexp01n_2xf32`, `div0n_2xf32` (fp32 seeds) | [B15 sp-lookup](b15-sp-lookup.md) | transcendental seed lookups (LUT), not value converts |
| `recip0nxf16`, `rsqrt0nxf16`, `nexp0nxf16` (fp16 seeds) | [B14 hp-lookup](b14-hp-lookup.md) | fp16 transcendental seeds |
| `clsfyn_2xf32` (classify) | [B02 §6](b02-vec-alu-fp.md) encoding · [fp sub-ISA §5.4](../core/fp-sub-isa.md#54-the-classify-mask--proven-by-execution) value | reads fp32, emits an 8-bit class mask — not a numeric convert |

> **CORRECTION (carried from B05) — no `sp`/`xf32` MAC form leaks into the convert scope.** The
> [B05 mixed-MAC page](b05-mac-mixed.md) audits whether any single-precision/`xf32` multiply-accumulate
> form was mis-counted as a convert; the fp32-convert family this page owns is **only** the
> `sp_cvt`/`sp_lookup`-grouped `S3_ALU` ops enumerated in §7 (`float`/`trunc`/`fi*`/`cvtf32*`) — the
> `mula*`/`muls*` fp32 forms are MAC (B05/B17), the `recip*`/`div*` fp32 forms are lookup (B15), and
> neither enters this batch's 23/138. No double-count: B13's 138 placements are disjoint from B05's
> MAC tally and B15's lookup tally. `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim re-tested against the binary this pass; a claim survives only if a *second*
independent witness agrees.

1. **The `float`/`ufloat`/`trunc` 3rd argument is a Q-format scale exponent, not a round mode.**
   *Challenge:* could the scaling I saw be an artifact of mis-threading the round-mode arg through a
   shift? *Re-test:* `float(2^23, s)` halves *monotonically* `s=0..5` (`1.0, 0.5, 0.25, 0.125,
   0.0625, 0.03125`) — a clean `2^(−s)`, not the 4-valued behavior a 2-bit round mode would show;
   and `trunc(4.0, s)` *doubles* (`4, 8, 16, 32`), the exact inverse left-shift. Cross-witness: the
   binary's own `Iclass_FLOAT_S_args` operand[2] is named **`i_imm5`** (a 5-bit immediate, 0..31),
   and the `Field_fld_ivpep_sem_sp_cvt_i_imm5_Slot_*` field thunks exist for all five wide slots —
   an *encoded scale field*, not a mode. Two witnesses (execution monotonicity + the named imm5
   field) agree. **Survives.** `[HIGH/OBSERVED·exec]`

2. **`trunc(NaN) = 0x7fffffff` (INT_MAX), with +/−saturation split.** *Challenge:* could the harness
   have read the wrong output pointer (a stale flag word)? *Re-test:* with the result pointer fixed
   by the `trunc(2.0)=2`, `trunc(-2.9)=-2` sanity, NaN→`0x7fffffff`, `+inf`→`0x7fffffff`,
   `−inf`→`0x80000000`, `−1e30`→`0x80000000`, each with the Overflow flag = `1` and `INT_MIN`
   exactly → `0x80000000` with flag `0` — the result *discriminates by sign and finiteness*, so it
   is genuine directed saturation, not a constant. The convention differs from x86 `cvttss2si`
   (which would give `0x80000000` for NaN), confirming it is the device's own rule. **Survives.**
   `[HIGH/OBSERVED·exec]`

3. **`firound` is half-away-from-zero and `firint` is FCR-RNE — two distinct rounders.**
   *Challenge:* maybe both are RNE and the `firound` `.5→away` I saw was a tie-bias coincidence?
   *Re-test:* on the *same* inputs `firint` gives `0.5→0, 2.5→2, 4.5→4` (ties-to-**even**) while
   `firound` gives `0.5→1, 2.5→3, 4.5→5` (always **away**) — the two *disagree on every half*, and
   `firint`'s mode sweep (`firint(2.5, RU)=3, RD=2, RZ=2`) shows it *reads* a mode `firound` ignores.
   This is exactly the [fp sub-ISA §3.1](../core/fp-sub-isa.md#31-the-2-bit-fcr-field-vs-the-3-bit-convert-core-field)
   `away`-code-`100`-only-for-FIROUND prediction, now proven by execution. **Survives.**
   `[HIGH/OBSERVED·exec]`

4. **fp16→fp32 widen is exact and round-trips with the narrow over 2009 values, 0 mismatch.**
   *Challenge:* could a 2000-sample random sweep have simply missed the lossy cases? *Re-test:* the
   widen is *structurally* exact (fp32 mantissa 23 ≥ fp16 mantissa 10, no rounding stage — the leaf
   has no round argument), so losslessness is a width fact, not a sampling claim; the 2009-value
   round-trip (the 9 special encodings — zero, max, denorm, ±inf, NaN, ±0 — plus 2000 random) found
   0 non-NaN mismatches, and the special encodings include exactly the boundary classes a random
   sweep would under-sample. Two witnesses (the no-round-stage structure + the boundary-inclusive
   round-trip) agree. **Survives.** `[HIGH/OBSERVED·exec]`

5. **The fp32-convert family is 23 mnemonics / 138 placements in `S3_ALU`, disjoint from the
   int-width repacks.** *Challenge:* the classifier routes *all* `cvt*` to B13/B20 — do the 38
   integer `cvt*` ops belong here? *Re-test:* the int-width `cvt32s24`/`cvt48snx32`/… ops are placed
   in the **`S1_Ld`** slot (`nm` shows `Slot_f0_s1_ld_encode`, this pass), their value leaves are
   pure integer mask/move (`module__xdref_cvt24u_24_32` = `and $0xffffff; mov; ret` — no fp), and
   they carry no fp16/fp32 dtype token — so they are wide-accumulator repacks (B10/B22), not fp
   converts. The 23 fp32 converts are all `S3_ALU`, all `sp_cvt`/`sp_lookup`-grouped, all with an
   fp32 leg. `23 × 6 = 138` re-counted via `nm`. The two families share neither slot nor leaf.
   **Survives.** `[HIGH/OBSERVED]`

No claim on this page rests on a raw dump, an unnamed symbol, or a single uncorroborated witness;
every value fact carries a differential-execution certificate against the shipped leaf, and every
boundary is pinned by slot + leaf + dtype.

### 9.1 Convert writeback latency — OBSERVED from the ISS, and a stage CORRECTION

The cycle model is **not** in `libisa-core.so` (its scheduling accessors are inert for ALU ops:
`num_funcUnits() = 1` — a single shared memory unit — `num_interfaces() = 0`, and every convert
opcode record carries `num_funcUnit_uses = 0` / `funcUnit_uses = NULL`, identical to a plain
`ivp_addnx16`; only memory ops reserve the unit). It lives in **`libcas-core.so`**, the generated
cycle-accurate ISS, as per-op `F<fmt>_F<fmt>_S3_ALU_<n>_<MNEMONIC>_{stage_functions, inst_stage0..15,
_issue}` symbols — **keyed by FLIX format + issue slot**, not by a global opcode index. Each
`..._issue` function sets a per-port pipeline stage in `%esi` before each register-file port call;
the **final** `mov $0xN,%esi; jmp *%rax` is the destination (writeback) commit stage. Decoded this
pass (neither binary has DWARF — `readelf -S` shows only `.symtab`; both GCC 4.9.4-built — so this is
ISS disassembly, not debug info):

| op family (issue fn) | vec source read | dest writeback stage |
|---|---|---|
| `FLOAT_S` / `FICEIL_S` / `FIRINT_S` / round group + widen (`CVTF32F16`/`CVTF32NXF16`) | stage **10** | **13** (`0xd`) |
| `TRUNC_S` / `UTRUNC` / `FITRUNC` group | stage **10** | **12** (`0xc`) |
| baseline fp ALU `ADD_S` / `MUL_S` (for comparison) | stage **10** | **13** |

Across **all 120** `F0_F0_S3_ALU_36` issue functions the writeback immediate is `0xc` (12) for 44 ops
and `0xd` (13) for 39 ops (the rest are read-only ports at `0xa`/`0xb`); **no S3_ALU op writes back at
stage 11.** `[HIGH/OBSERVED]` for the ops with their own `_issue` symbol; `[HIGH/INFERRED]` for the
shared-handler ops (`UFLOATN`, `UTRUNCN`, `FIROUNDN`, `FIFLOORN`, `FITRUNCN`, `CVTF32F16`,
`CVTF32NXF16`) that alias a family handler — each still has its own populated 16-entry `inst_stage*` /
`stage_functions` table, so they are modeled, not defaulted.

> **CORRECTION — the convert (and fp-ALU) writeback is stage 13/12, *not* "@11"; converts are NOT a
> longer latency class than fp ALU.** The shipped ISS commits a vec destination for the FLOAT/round
> converts (and `cvtf32f16`/`cvtf32nxf16`) at **stage 13** and for the TRUNC/UTRUNC/FITRUNC converts
> at **stage 12**, with vec source reads at **stage 10** — *the same pipeline class as plain fp
> `add`/`mul`* (both stage 13). Two naive readings are refuted: (a) any "writeback to vec @11" figure
> (a 1-cycle ALU framing) does not match the ncore2gp ISS, which uses 12/13; and (b) the multi-step
> int↔fp converts do **not** incur a longer convert-specific latency — they retire in the same
> fp-ALU stage band. A reimplementer schedules `firound`/`float`/`cvtf32*` at the fp-ALU writeback
> stage (13), and `trunc`/`utrunc` one stage earlier (12). `[HIGH/OBSERVED]` (decoded `mov $0xN,%esi`
> immediates in the `libcas-core.so` `_issue` functions, cross-checked against `ADD_S`/`MUL_S` and the
> 120-op S3_ALU distribution).

---

## 10. Confidence ledger

**HIGH / OBSERVED (by execution)** — driven live against `libfiss-base.so` this pass:

* The Q-format scale semantics of `float`/`ufloat` (`out = int · 2^(−imm5)`, the halving sweep) and
  of `trunc`/`utrunc` (the doubling/quantize inverse), with the `imm5`-is-a-scale certificate.
* The int→fp32 precision-loss rounding above 2^24 and its **RZ-truncate leaf-default** (floor-to-grid
  on every odd tie), reconciled to the two-level RNE/RZ rule (not re-litigated).
* `trunc`/`utrunc` saturation: +/−INT_MIN/MAX clamps, the **NaN→INT_MAX** convention, +inf/−inf
  directed saturation, and the `utrunc(−2.9)=0xfffffffe` small-negative trap.
* The five round-to-integral behaviors (`ficeil`=+∞, `fifloor`=−∞, `fitrunc`=→0, `firint`=FCR-mode
  ties-even, `firound`=fixed half-away), including signed-zero edges and sNaN-quieting; the
  `firint` vs `firound` half-disagreement and the `firint` directed-mode sweep.
* fp16→fp32 widen exactness (incl. fp16-subnormal → fp32-normal) and the 2009-value widen→narrow
  round-trip with 0 non-NaN mismatch.

**HIGH / OBSERVED** — read from `libisa-core.so` immediates / disassembly this pass:

* The 23-mnemonic roster, the `F1_S3_ALU` opcode-selector immediates, the slot-local selector
  variance (`firound` across 6 slots), the `S3_ALU` placement (5 wide + 1 narrow), the 138-placement
  census.
* The operand model: `vt`/`vr` vec operands + the `i_imm5` scale immediate (`Iclass_*_S_args`); the
  scattered imm5 bit-window (`_get` thunk); the FCR/FSR stateArg threading on the rounding forms; the
  `cvtf32nxf16_{0,1}` two-half widen structure (the `movzwl` + tail-call to the widen core).
* The convert writeback latency (§9.1) — **stage 13** (FLOAT/round/widen family) / **stage 12**
  (TRUNC family), vec source reads stage 10, decoded from the `libcas-core.so` ISS `_issue` functions;
  `libisa-core.so` carries no convert latency (`num_funcUnit_uses = 0`).

**HIGH / INFERRED**

* The `t` (predicated) variants' value = base op gated by a `vbool` mask (encoding OBSERVED; value
  inferred from the `*T` iclass guard operand, not swept live).
* The shared-handler converts' writeback stage (`UFLOATN`/`UTRUNCN`/`FIROUNDN`/`FIFLOORN`/`FITRUNCN`/
  `CVTF32F16`/`CVTF32NXF16` lack their own `_issue` symbol; grouped to 13/12 by their family sibling,
  each with its own populated 16-entry stage table).

**MED / OBSERVED**

* The per-format drift of the `imm5` scale field beyond the read `F1_S3_ALU` slot.

**Deferred (not in this batch's denominator)**

* fp16-side narrow + int16↔fp16 + fp16 round → [B20](b20-hp-cvt.md); integer wide-accumulator
  repacks → [B10](b10-wvec-pack.md)/B22; fp32 arithmetic → [B02](b02-vec-alu-fp.md); fp32/fp16
  transcendental seeds → [B15](b15-sp-lookup.md)/[B14](b14-hp-lookup.md).

All facts read as derived from shipped-artifact static analysis and license-free in-process
execution of the binary's own value leaves (lawful interoperability RE).

---

## Cross-references

* [The Floating-Point Sub-ISA (FCR/FSR view)](../core/fp-sub-isa.md) — the 2-bit architectural
  RoundMode, the **3-bit convert-core field** with the `away` code (`100`) `firound` uses, the
  two-level RNE-vs-RZ model this page's int→fp default invokes, and the five `SHARED_OR` sticky
  flags the rounding/saturating converts post.
* [B02 vector ALU (fp16/fp32 slice)](b02-vec-alu-fp.md) — the `S3_ALU` slot these converts share,
  the directed-round-overflow rule the narrow round-trip matches, and the fp arithmetic excluded
  here; B02 explicitly defers the `firound`/`ficeil`/`fifloor`/`fitrunc`/`firint`/round-convert
  family to this page.
* [B20 fp16 convert (hp-cvt)](b20-hp-cvt.md) — the fp16-side narrow and int16↔fp16 ops that
  round-trip against §6 here; the partition-mirror of this page on the fp16 axis.
* [B14 fp16 lookup](b14-hp-lookup.md) · [B15 fp32 lookup](b15-sp-lookup.md) — the transcendental
  seeds (`recip0`/`rsqrt0`/`nexp`) excluded from the convert family.
* [B10 wvec-pack](b10-wvec-pack.md) · [B22 unpack-wvec-mov](b22-unpack-wvec-mov.md) — the integer
  wide-accumulator `cvt*_24/48/96` repacks (`S1_Ld` slot) that are *named* `cvt` but are not fp
  converts.
* [The Eight Register Files](../core/register-files.md) — the `vec` (idx 2) datapath the converts
  read/write; the S3_ALU writeback stage (13/12) is read directly from the `libcas-core.so` ISS
  (§9.1), not carried.
* [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the per-instruction
  schema, the `cvt → B20 if fp16 else B13` classifier, and the `1534 ↔ 12569` roll-up this batch's
  138 placements feed.
* [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified denominators
  and the count-with-`nm` discipline.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the
  proven-by-execution value lane, and the "RZ-default un-parameterized leaf call" property this
  page's int→fp tie behavior invokes.
