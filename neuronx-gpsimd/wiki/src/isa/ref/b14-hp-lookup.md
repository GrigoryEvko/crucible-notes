# ISA Batch 14 — fp16 Transcendental Seeds (`hp_lookup`)

This is the per-instruction reference for the **fp16 transcendental seed-lookup slice** of the
Vision-Q7 *Cairo* (`ncore2gp`) ISA: the `ivp_recip0nxf16` / `ivp_rsqrt0nxf16` / `ivp_sqrt0nxf16` /
`ivp_nexp0nxf16` / `ivp_nexp01nxf16` family (each with a `…t` **predicated** sibling) that reads a single
`vec` source in fp16 (`NXF16`, 32 lanes) and produces, per lane, a **low-precision initial
approximation** — the *seed* — for a reciprocal, reciprocal-square-root, square-root, or base-2 exp.
These ops do **not** compute the transcendental; they index a 128-entry `.rodata` seed table and emit
a ~7-bit estimate that a software **Newton-Raphson** (recip/rsqrt) or polynomial (exp) kernel then
refines to full fp16 precision in 1–2 iterations. It owns **10 shipped mnemonics** summing to
**60 placements** of the certified-perfect `12569` cover ([coverage tally](../core/coverage-tally.md)).

The cycle-accurate model names this family **`ivpep_sem_hp_lookup`** (half-precision lookup) — the
literal origin of this page's title — paired with the `sp_lookup` (fp32) family that
[B15](b15-sp-lookup.md) owns; both are tagged `S3_ALU` in the simulator's per-slot semantic stages,
matching the encode side exactly (§6). This page is the **value-semantics-and-encoding** companion to
the control-view [fp sub-ISA page](../core/fp-sub-isa.md) (FCR/FSR, NaN, the `.h` scalar siblings) and
sits in the [30-batch partition](template-and-partition.md#41-the-partition-table--b01--b30) as the
fp16 half of the LUT pair: it draws from the `xt_ivp32` package's seed-LUT verb sub-family, between the
fp ALU ([B02](b02-vec-alu-fp.md)) and the FMA refine ops ([B17](b17-spfma.md)/[B18](b18-hp-fma.md)).

Everything below is grounded three ways. The **opcode-selector immediates** are read straight from the
`Opcode_<mnem>_Slot_<slot>_encode` thunk bodies in the non-stripped `libisa-core.so`
(`movl $imm,(%rdi); ret`). The **seed table** (`RECIP_Data8` / `RSQRT_Data8`) is read byte-for-byte
from `libfiss-base.so` `.rodata` and its **construction formula re-derived**. The **value semantics** are
*proven by execution* — every seed claim was computed by calling the matching `module__xdref_` leaf in
`libfiss-base.so` live via `ctypes` (license-free), the binary arbitrating its own arithmetic. The
**device round-trip** (`xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp`) confirms the mnemonics and
bundle bytes. The page default is `[HIGH/OBSERVED]` (by execution where a value fact is stated), per
the [confidence model](../../reference/confidence-model.md); claims that depart from that default
carry an explicit tag.

> **WALL — the seed *table* is validated truth; the device-interior seed *coefficient* algebra is
> CARRIED.** The 128-entry `RECIP_Data8` / `RSQRT_Data8` `.rodata` tables are **OBSERVED** (read
> directly, construction re-derived to 127/128 and 64/64 exact, §3) and the leaf's **output seed is
> OBSERVED-by-execution** (driven live, §4). What remains **CARRIED** (per the
> [FW-42 wall](../../reference/confidence-model.md)) is any claim about the *device hardware's* internal
> coefficient bytes or the exact gate-level reduction between the table read and the lane writeback —
> the fiss-base leaf is the **functional reference model**, bit-exact against the table, but the silicon
> interior of the seed unit is not byte-visible. This page asserts only the table contents and the leaf
> output as OBSERVED; the hardware microcode is flagged CARRIED where it appears. `[HIGH/OBSERVED]` on
> the table+leaf; `[—/CARRIED]` on the device coefficient interior.

---

## 1. Scope, datapath, and key facts

Every `hp_lookup` op is a **1-source, 1-destination** `vec`→`vec` fp16 op placed in the **`S3_ALU`**
class across six slots `{F0, F1, F2, F3, F7, N0}` (the same wide+narrow ALU placement set as the fp ALU
family, [B02 §2](b02-vec-alu-fp.md#2-the-roster)). The lane shape is **`NXF16` = 32 lanes × 16 b over a
512-bit `vec`** ([register-files §3](../core/register-files.md)); the op runs the seed-LUT pipeline
(16 modelled stages, §6) and writes one fp16 seed per lane back to `vec`.

| Fact | Value | Source |
|---|---|---|
| Data file | `vec` idx 2, **512 b × 32**; fp16 = 32 lanes (`NXF16`) | [register-files §3](../core/register-files.md) |
| Op shape | 1-in / 1-out, per-lane element function | leaf ABI (§4.1) |
| Mnemonics this batch | **10** (5 base + 5 `…t` predicated/masked) | `nm libisa-core.so \| rg -c` (§5) |
| Placements this batch | **60** (10 × 6 slots) | per-mnemonic census (§5) |
| Slot set | `{F0,F1,F2,F3,F7}·S3_ALU` (16 B wide) + `N0·S3_ALU` (8 B narrow) | encode-thunk roster |
| Seed tables | `RECIP_Data8` @ `0x958fc0`, `RSQRT_Data8` @ `0x958dc0` — 128 × u32 (8-bit data) | `libfiss-base.so` `.rodata` |
| Seed precision | **~7.1 valid bits** (max rel err **0.732 %**) | full-range live sweep (§4.4) |
| Refine | software Newton-Raphson (recip/rsqrt) or poly (exp), 1–2 iters → bit-exact | live NR demo (§4.5) |
| Value oracle | `libfiss-base.so` `module__xdref_*` leaves, ctypes | [confidence §6.1](../../reference/confidence-model.md) |
| Cycle model name | `ivpep_sem_hp_lookup` / `bbn_sem_vec_sprecip_rsqrt` (16 stages) | `libcas-core.so` symbols (§6) |

> **GOTCHA — these are SEEDs, not transcendentals; even an exact reciprocal gets the seed error.**
> `recip0(2.0)` returns **`0.498047`**, *not* `0.5`, even though `1/2` is exactly representable in fp16.
> The op is a pure table lookup keyed on the input significand's leading bits; it carries no Newton step.
> A reimplementer who treats `ivp_recip0nxf16` as "the reciprocal instruction" is wrong by up to 0.73 %
> on **every** input, including powers of two. The reciprocal is `seed`, then **`seed·(2 − x·seed)`**
> once or twice (§4.5). The fp16 `recip`/`rsqrt`/`sqrt`/`div`/`exp` that a kernel calls are *macros*:
> `seed-lookup → FMA-refine`, where the FMA half lives in [B17](b17-spfma.md)/[B18](b18-hp-fma.md) and
> the divide combiner `divn` in [B23](b23-divide.md).

> **NOTE — `div0nxf16` is a seed op but lives in [B23 divide](b23-divide.md), not here.** The
> [partition classifier](template-and-partition.md#42-how-each-batchs-membership-is-derived-the-classifier)
> routes `recip|rsqrt|sqrt|nexp|exp`-rooted verbs to B14 (fp16) **before** the `div` branch, so
> `ivp_recip0nxf16`…`ivp_nexp01nxf16` are B14 and `ivp_div0nxf16` (which roots on `div`) falls to B23.
> `div0` is the *mantissa-only reciprocal seed* used by the `divn` Newton divider: `div0(2.0)=div0(0.5)
> =div0(4.0)=0.99609` — it returns the **significand reciprocal seed and discards the exponent**
> (handled by `divn`), a deliberately different element function from `recip0` (which reciprocates the
> exponent too). The B14/B23 split is a verb-root partition rule, not a semantic one; this page documents
> `div0`'s relationship in §7 but does **not** tally it.

---

## 2. The roster

Every `hp_lookup` mnemonic this batch owns, with its encoding read at the **canonical `F1_S3_ALU`
slot**. The `opcode-sel imm` is the `movl` immediate in the `Opcode_ivp_<mnem>_Slot_f1_s3_alu_encode`
thunk (disassembled); `byte-size` is `16/8` (16 B in the wide `F`-formats, 8 B in the
narrow `N0`). The `table` column names the `.rodata` seed LUT the leaf indexes; `seed lane in/out` is
`vec(i)→vec(o)` for all (1-source element op). Every row of §2.1–§2.2 is `[HIGH/OBSERVED]`; the §2.1
base forms are additionally **proven by execution**, while the §2.2 `…t` rows are OBSERVED at the
encoding level (their per-lane seed value is the base form's).

### 2.1 Base (non-predicated) forms

| mnemonic | FLIX fmt·slot | opcode-sel imm (F1_S3) | vec lane in/out + table | byte-size | one-line semantics |
|---|---|---|---|---|---|
| `ivp_recip0nxf16` | F1·S3_ALU (+5 slots) | `0x26310106` | `vec(i)→vec(o)`, **RECIP_Data8** | 16/8 | 1/x seed: `mant→RECIP[mant>>3]/256`, exp reciprocated |
| `ivp_rsqrt0nxf16` | F1·S3_ALU (+5) | `0x26310306` | `vec(i)→vec(o)`, **RSQRT_Data8** | 16/8 | 1/√x seed: `idx=(exp&1)<<6 \| mant>>4`, two binades |
| `ivp_sqrt0nxf16` | F1·S3_ALU (+5) | `0x26318106` | `vec(i)→vec(o)`, **RSQRT_Data8** | 16/8 | √x seed (= rsqrt-mantissa form; `sqrt=x·rsqrt0(x)`) |
| `ivp_nexp0nxf16` | F1·S3_ALU (+5) | `0x26308306` | `vec(i)→vec(o)` (range-reduce) | 16/8 | base-2 exp range-reduction seed (negated binade) |
| `ivp_nexp01nxf16` | F1·S3_ALU (+5) | `0x26308106` | `vec(i)→vec(o)` (range-reduce) | 16/8 | `nexp0` 0/1-octave variant (integer-part selector) |

### 2.2 Predicated (`…t`) forms

The `…t` variants are a **distinct opcode group** sharing the `0x2b08_xxxx` selector band (versus the
base group's `0x263x_0xxx`), at the same six slots, same lane shape, same seed value semantics — but
they additionally **consume a `vbool` mask** (the predicate/tail selector), so a reimplementer must wire
a boolean source operand the base form lacks (§2.3 GOTCHA). Selectors at `F1_S3_ALU` (byte-exact):

| mnemonic | opcode-sel imm (F1_S3) | base-form counterpart |
|---|---|---|
| `ivp_recip0nxf16t` | `0x2b080202` | `ivp_recip0nxf16` (`0x26310106`) |
| `ivp_rsqrt0nxf16t` | `0x2b080102` | `ivp_rsqrt0nxf16` (`0x26310306`) |
| `ivp_sqrt0nxf16t` | `0x2b080302` | `ivp_sqrt0nxf16` (`0x26318106`) |
| `ivp_nexp0nxf16t` | `0x2b080002` | `ivp_nexp0nxf16` (`0x26308306`) |
| `ivp_nexp01nxf16t` | `0x2b080300` | `ivp_nexp01nxf16` (`0x26308106`) |

The per-slot selector for `ivp_recip0nxf16` across its six placements, to show the
**slot-locality** of the immediate (the same finding as [B02 §2 GOTCHA](b02-vec-alu-fp.md#2-the-roster)):

| slot | opcode-sel imm | thunk shape |
|---|---|---|
| `F0_S3_ALU` | `0x80df8304` | wide: `word0` + `word1=0x00000000` |
| `F1_S3_ALU` | `0x26310106` | wide |
| `F2_S3_ALU` | `0x08940202` | wide |
| `F3_S3_ALU` | `0x02243000` | wide |
| `F7_S3_ALU` | `0x28948002` | wide |
| `N0_S3_ALU` | `0x6a682000` | **narrow** (single `movl`, no upper lane) |

> **GOTCHA — the `…t` is a separate, *predicated* opcode that reads a `vbool` mask, not an OR-able bit
> and not a "throttle".** Every base/`…t` pair has selectors with **no additive or single-bit relation**
> (`recip0`=`0x26310106` vs `recip0t`=`0x2b080202`: differing high byte `0x26`→`0x2b`, differing low
> word) — a fully distinct `opcodes[]` row with its own 6 placements, exactly like the fp16/fp32
> precision split in [B02](b02-vec-alu-fp.md#21-fp16-nxf16--32-lane). The decisive *behavioral*
> difference is read from the cycle model: the `…t` form's issue callback (e.g.
> `…IVP_RECIP0NXF16T_issue` in `libcas-core.so`) calls **`opnd_sem_vbool_addr`** before the `vec`
> operand reads — i.e. it consumes an extra **`vbool` predicate operand** the base form does not, and
> dedicated `_issue`/`_stall` callbacks exist **only** on the `…t` variants. The seed *value* per
> selected lane is identical to the base form; the mask gates which lanes write. (The literal name
> "throttle" does **not** appear anywhere in `libisa-core.so`/`libcas-core.so`/`libfiss-base.so` — the
> `t` denotes the predicated/tail-masked form.) A reimplementer's assembler selects the `…t` *opcode*
> and must supply the boolean source; it cannot flip a fixed bit on the base form.

> **NOTE — `word1 == 0x00000000` on every wide placement; `N0` is single-word.** The five `F`-format
> placements are 16-byte bundles whose encode thunk writes `word0` then `word1 = 0` (`movl`-count = 2
> for all five base ops at `F7_S3_ALU`); the `N0` placement is an 8-byte
> narrow bundle whose thunk writes only `word0` (`movl`-count = 1). This is the universal encode-thunk
> ABI of [the template](template-and-partition.md#31-required-structure) and
> [FLIX §6.1](../core/flix-encoding.md#61-the-universal-encode-thunk-abi): the selector template is
> `word0`; operand fields (the source/dest `vec` register numbers) are deposited *separately* by
> `field_set`, not by this thunk.

---

## 3. The seed tables — `RECIP_Data8` / `RSQRT_Data8` (the OBSERVED ground truth)

Both leaves index a **128-entry, 4-byte-stride** `.rodata` table holding an **8-bit reciprocal-class
value per entry** (the `Data8` name = 8-bit data widened to `u32`). In `libfiss-base.so` the relevant
sections are `.rodata` (VMA `0x88ff00` **== file `0x88ff00`**, `readelf -SW`) — so the table
addresses are direct file offsets, no delta. (The `0x200000` `.data` delta of
[the meta page](template-and-partition.md#8-function--symbol-map) applies only to the *encoder*
`libisa-core.so`'s writable sections; these tables are read-only `.rodata`.)

> **QUIRK — the leaf indexes a `recip_tab` / `rsqrt_tab` that is *byte-identical* to the canonically
> named `RECIP_Data8` / `RSQRT_Data8`.** Two symbols name the same bytes: `table__recip_tab`
> (`0x9553c0`) is the runtime-indexed copy and `table__RECIP_Data8` (`0x958fc0`) is the canonical seed
> table; a 512-byte `xxd` comparison returns **IDENTICAL**, and likewise
> `table__rsqrt_tab` (`0x9551c0`) ≡ `table__RSQRT_Data8` (`0x958dc0`). The disassembled `recip0` body's
> `lea …# 9553c0 <table__recip_tab>` is therefore reading the `RECIP_Data8` content. Cite the
> `…_Data8` name as the canonical seed source.

### 3.1 `RECIP_Data8` — construction re-derived

The table is monotone decreasing `0xff → 0x81` across the 128 entries. Each entry is the **8-bit
reciprocal of the bucket midpoint**, scaled into the half-open mantissa range:

```c
// RECIP_Data8[i]  ==  round( 256 / xm ),   xm = 1 + (i + 0.5)/128   (bucket midpoint, x in [1,2))
//   i.e. the reciprocal 1/xm of the bucket centre, with 1/xm in (0.5,1], rendered as an 8-bit
//   fraction (×256). The leaf places this byte verbatim as the seed fp16 mantissa.
```

Re-derived over all 128 entries: `RECIP_Data8[i] == round(256/(1+(i+0.5)/128))` matches
**127/128 exact, 128/128 within ±1 ULP-of-byte**. Spot values (read directly):
`RECIP[0]=0xff` (xm≈1.004, 1/xm≈0.996, 0.996·256≈255), `RECIP[64]=0xaa` (xm≈1.504, 1/xm≈0.665,
·256≈170), `RECIP[127]=0x81` (xm≈1.996, 1/xm≈0.501, ·256≈128). `[HIGH/OBSERVED]`

### 3.2 `RSQRT_Data8` — two binades, swapped halves

`RSQRT_Data8` is **two stacked 64-entry tables** because `1/√x` depends on the **parity of the input
exponent** (an even/odd binade selects whether `x` lives in `[1,2)` or `[2,4)` after exponent folding):

```c
// lo64  RSQRT_Data8[i],  i in [0,64):   ODD-exponent binade, x in [2,4)
//   == round( 256 / sqrt( 2*(1+(i+0.5)/64) ) )           // 0xb4 .. 0x80
// hi64  RSQRT_Data8[64+j], j in [0,64):  EVEN-exponent binade, x in [1,2)
//   == round( 256 / sqrt(   1+(j+0.5)/64   ) )           // 0xff .. 0xb5
```

Re-derived: the lo64 (odd, x∈[2,4)) matches **63/64 exact, 64/64 within ±1**; the hi64 (even,
x∈[1,2)) matches **64/64 exact**. The discontinuity at index 64 (`0x80 → 0xff`) is the binade boundary,
**not** a table error — it is precisely where the rsqrt seed jumps from the top of `[2,4)` to the bottom
of `[1,2)`. Spot values: `RSQRT[0]=0xb4`, `RSQRT[63]=0x80`, `RSQRT[64]=0xff`, `RSQRT[127]=0xb5`.

---

## 4. The seed-lookup algorithm — proven by execution

### 4.1 The value-leaf calling convention (recovered by disassembly + execution)

The fp16 seed leaves are element functions with a leading lane/scratch arg, the input value, and a set
of output pointers; the result lands in a **fixed** one of them (read from each leaf's store sites and
confirmed by execution):

```c
// recip0 / rsqrt0  (suffix _1_1_16f_16f): result in the 3rd output pointer (arg-5, %r8):
//   void recip0(long lane, uint in16, uint *o0, uint *o1, uint *seed, uint *o3);   // seed = *o2 slot
// sqrt0 / nexp0 / nexp01 / div0 (suffix _16f_16f): result in the 1st output pointer (arg-3, %rdx):
//   void sqrt0 (long lane, uint in16, uint *seed, ...);                            // seed = *o0 slot
```

`recip0_1_1_16f_16f` @ `0x520110`, `rsqrt0_1_1_16f_16f` @ `0x520310`, `sqrt0_16f_16f` @ `0x520060`,
`nexp0_16f_16f` @ `0x521850`, `nexp01_16f_16f` @ `0x521790`, `div0_16f_16f` @ `0x51fff0` (all in
`libfiss-base.so`). Pass four output buffers and read the correct slot; the
leaf is license-free and callable in-process. `[HIGH/OBSERVED]` — ABI confirmed by `recip0(1.0)→0x3bf8`
landing in `*o2`, `sqrt0(4.0)→0x3bf8` in `*o0`.

### 4.2 The reciprocal seed — `recip0`

```c
// ivp_recip0nxf16 : per-lane 1/x seed.  Ported from module__xdref_recip0_1_1_16f_16f (@0x520110).
//   1.  crack the fp16 input:  s = bit15;  e = (in>>10)&0x1f;  m = in&0x3ff   (10-bit mantissa)
//   2.  NORMAL path (0 < e < 31):
//        idx  = (m >> 3) & 0x7f                      // top 7 mantissa bits -> 128-entry bucket
//        seed_mant8 = RECIP_Data8[idx]               // 8-bit reciprocal of the bucket midpoint
//        e_out = (2*BIAS - 1) - e                    // reciprocate the exponent: 1/2^(e-15)
//        out   = (s<<15) | (e_out<<10) | (seed_mant8 placed as the high mantissa byte)
//   3.  SPECIAL paths:  e==0 (zero/denormal) -> bsr-normalize then index, or +inf on tiny denorm;
//        e==31 -> 1/inf = 0, 1/(-inf) = -0, 1/NaN = quieted-NaN.
//   The element function is piecewise-CONSTANT within each 8-wide mantissa bucket (no interpolation).
```

**Bucket-constancy certificate (proven by execution).** The seed is identical for all 8 mantissas in a
bucket — verified live across **all 128 buckets** of `[1,2)`: every bucket returned exactly one distinct
seed. The seed steps only when `idx = m>>3` increments.

**The seed *is* the table byte (proven by execution, exhaustive).** The crucial reimplementation fact:

```
recip0 seed VALUE  ==  RECIP_Data8[m>>3] / 256       (for x in [1,2), exponent reciprocated)
```

Driven live over **all 1024 mantissas of `[1,2)`: 0 mismatches.** Sample (live):

```
x=1.000  in=0x3c00  idx=0    RECIP[0]=0xff  -> seed=0x3bf8 = 0.99609 = 0xff/256   (1/1.0 seed)
x=1.504  in≈0x3c81  idx=64   RECIP[64]=0xaa -> seed=0x3950 = 0.66406 = 0xaa/256
x=1.996  in≈0x3ffd  idx=127  RECIP[127]=0x81-> seed=0x3808 = 0.50391 = 0x81/256
x=2.000  in=0x4000  idx=0    RECIP[0]=0xff  -> seed=0x37f8 = 0.49805   (same mantissa, exp-1)
x=0.500  in=0x3800  idx=0    RECIP[0]=0xff  -> seed=0x3ff8 = 1.99609   (exp reciprocated up)
```

`recip0(2.0)` and `recip0(0.5)` share the **same mantissa byte** (idx 0) and differ only in the
reciprocated exponent — the table is exponent-independent, exactly as a reciprocal seed table must be.
`[HIGH/OBSERVED·exec]`

### 4.3 The reciprocal-sqrt seed — `rsqrt0` (and `sqrt0`)

```c
// ivp_rsqrt0nxf16 : per-lane 1/sqrt(x) seed.  Ported from module__xdref_rsqrt0_1_1_16f_16f (@0x520310).
//   1.  crack input: e = (in>>10)&0x1f;  m = in&0x3ff
//   2.  NORMAL path:
//        idx  = ((e & 1) << 6) | (m >> 4)            // 7-bit index: exp-parity picks the binade-half,
//                                                    //   top 6 mantissa bits pick the bucket
//        seed_mant8 = RSQRT_Data8[idx]               // 8-bit reciprocal-sqrt of the bucket midpoint
//        e_out = BIAS + ((BIAS - e) >> 1)            // halve-and-negate the exponent for 1/sqrt;
//                                                    //   the >>1 rounding is binade-coupled to the
//                                                    //   (e&1) table-half pick — model the exponent
//                                                    //   together with idx (the exact bias rounding
//                                                    //   for the odd binade is CARRIED interior).
//        out   = (e_out<<10) | (seed_mant8 ...)      // sign always +; rsqrt(neg) is NaN
//   3.  SPECIAL: x<0 -> NaN;  +0 -> +inf;  +inf -> +0;  denormal -> bsr-normalize.
//   sqrt0 reuses RSQRT_Data8 (sqrt(x) = x * rsqrt0(x) in the refine kernel).
```

**The rsqrt seed *is* the table byte (proven by execution, exhaustive).**

```
rsqrt0 seed VALUE  ==  RSQRT_Data8[(e&1)<<6 | (m>>4)] / 256
```

Driven live over **all 1024 mantissas of `[1,2)` (even-exp binade, hi64 half): 0 mismatches**, and over
a sweep of `[2,4)` (odd-exp binade, lo64 half): 0 mismatches. Sample (live):

```
x=1.000  e=15(odd? lsb1) idx=64  RSQRT[64]=0xff -> seed=0.99609 = 1/sqrt(1.0) seed
x=2.000  e=16(lsb0)      idx=0   RSQRT[0]=0xb4  -> seed=0.70312 = 1/sqrt(2) seed (=0.7071 true)
x=4.000  e=17(lsb1)      idx=64  RSQRT[64]=0xff -> seed=0.49805 = 1/sqrt(4) seed (=0.5 true)
x=0.250  e=13(lsb1)      idx=64  RSQRT[64]=0xff -> seed=1.99219 = 1/sqrt(0.25) seed (=2.0 true)
```

The exponent LSB selects the table half (`e&1`): the `[2,4)` binade reads lo64 (`0xb4…0x80`), the
`[1,2)` binade reads hi64 (`0xff…0xb5`).

### 4.4 Edge inputs — denormal, power-of-two, zero, inf, NaN (proven by execution)

Driven live (result slot per §4.1):

```
recip0(+0      0x0000) = 0x7c00  (+inf)      recip0(+inf 0x7c00) = 0x0000  (+0)
recip0(-0      0x8000) = 0xfc00  (-inf)      recip0(-inf 0xfc00) = 0x8000  (-0)
recip0(qNaN    0x7e00) = 0x7ea8  (NaN, payload re-quieted)
recip0(denorm  0x0001) = 0x7c00  (+inf)      // smallest denorm -> reciprocal overflows to inf
recip0(maxden  0x03ff) = 0x7408  (16512)     // large denorm -> finite (bsr-normalize path taken)
recip0(minnrm  0x0400) = 0x73f8  (16320)
recip0(65504   0x7bff) = 0x0102  (1.538e-05) // 1/max-normal -> small normal

rsqrt0(+0      0x0000) = 0x7c00  (+inf)      rsqrt0(+inf 0x7c00) = 0x0000  (+0)
rsqrt0(-1.0    0xbc00) = 0xfff8  (NaN)        // 1/sqrt of negative is NaN
rsqrt0(denorm  0x0001) = 0x6bf8  (4080)       // denormal handled, not flushed
rsqrt0(2.0     0x4000) = 0x39a0  (0.70312)    // 1/sqrt(2) seed
```

**The power-of-two GOTCHA, live:** `recip0(2.0)=0.498`, `recip0(4.0)=0.249023`, `recip0(8.0)=0.124512`
— each off the exact reciprocal by the table's ~0.4 % seed error. Denormals are **not** flushed: the
`e==0` path normalizes via a `bsr` (count-leading-zeros) before indexing, so `recip0(0x03ff)` returns a
finite seed and `rsqrt0(0x0001)` returns `4080`.

### 4.5 The Newton-Raphson refine that consumes the seed (proven by execution)

The seed's *role* is the first iterate of a quadratically-converging Newton-Raphson, run in software with
the fp16 FMA ops of [B18](b18-hp-fma.md). Reciprocal uses `y ← y·(2 − x·y)`; rsqrt uses
`y ← y·(1.5 − 0.5·x·y²)`. Driving the live seed through the refine:

```
recip:  y0 = recip0(x)              # ~7-bit seed
        y1 = y0*(2 - x*y0)          # ~14-bit
        y2 = y1*(2 - x*y1)          # full fp16
  x=1.5    seed=0.66406 (err 0.391%)  NR1=0.666656 (0.0015%)  NR2=0.6666667 (0.00000%)  1/x=0.666667
  x=1.999  seed=0.50391 (err 0.731%)  NR1=0.500223 (0.0053%)  NR2=0.5002501 (0.00000%)  1/x=0.500250

rsqrt:  y0 = rsqrt0(x)
        y1 = y0*(1.5 - 0.5*x*y0*y0)
        y2 = y1*(1.5 - 0.5*x*y1*y1)
  x=2.0    seed=0.70312 (0.563%)  NR1=0.707073 (0.0047%)  NR2=0.7071068 (0.00000%)  1/sqrt=0.707107
  x=3.0    seed=0.57422 (0.542%)  NR1=0.577325 (0.0044%)  NR2=0.5773503 (0.00000%)  1/sqrt=0.577350
```

**Two Newton iterations reach bit-exact fp16** from the seed — the canonical seed-then-refine pattern.
A reimplementer emits `seed-lookup` (this batch) then 1–2 `madd/msub`-class FMAs (B18). The whole
**full-range** seed accuracy, swept live over every positive-normal fp16: **max relative error 0.732 %
= 7.1 valid bits**, which is exactly the precision a 128-entry 8-bit table delivers.

### 4.6 The exp range-reduction seeds — `nexp0` / `nexp01`

`nexp0` / `nexp01` are **not** an exp; they are the range-reduction front-end for a software `exp2`.
Driven live (result slot 0):

```
x=0.0  nexp0=-1.0000  nexp01=-1.0000      x=0.5  nexp0=-1.0000  nexp01=-2.0000
x=0.75 nexp0=-1.5000  nexp01=-3.0000      x=1.0  nexp0=-1.0000  nexp01=-1.0000
x=2.0  nexp0=-1.0000  nexp01=-2.0000      x=2.5  nexp0=-1.2500  nexp01=-2.5000
x=-1.0 nexp0=+1.0000  nexp01=-1.0000      x=-2.0 nexp0=+1.0000  nexp01=-2.0000
```

`nexp0` returns the **negated reduced binade** (the `−2^k`-scaled leading term whose sign tracks the
input sign), and `nexp01` adds the integer-octave selector (`−1`/`−2`/`−3` per binade) — together they
form the `(n, −2^k·f)` decomposition a base-2 exp polynomial consumes (`2^x = 2^n · 2^f`). The exact
poly tail is in the convert/FMA kernels, not in these seeds. `[HIGH/OBSERVED·exec]` on the decomposition;
`[MED/OBSERVED]` on the precise octave-mask interior (read structurally, not exhaustively swept).

---

## 5. Batch tally — every `hp_lookup` mnemonic vs `nm`

The 10 mnemonics and their placement counts, counted with
`nm libisa-core.so | rg -c 'Opcode_ivp_<mnem>_Slot_.*_encode'` (the only legitimate count method, per
[coverage tally §9](../core/coverage-tally.md)):

| family | base form (placements) | predicated `…t` form (placements) | value leaf (fp16) |
|---|---|---|---|
| reciprocal | `recip0nxf16` (6) | `recip0nxf16t` (6) | `module__xdref_recip0_1_1_16f_16f` @ `0x520110` |
| recip-sqrt | `rsqrt0nxf16` (6) | `rsqrt0nxf16t` (6) | `module__xdref_rsqrt0_1_1_16f_16f` @ `0x520310` |
| sqrt | `sqrt0nxf16` (6) | `sqrt0nxf16t` (6) | `module__xdref_sqrt0_16f_16f` @ `0x520060` |
| exp-reduce | `nexp0nxf16` (6) | `nexp0nxf16t` (6) | `module__xdref_nexp0_16f_16f` @ `0x521850` |
| exp-reduce-01 | `nexp01nxf16` (6) | `nexp01nxf16t` (6) | `module__xdref_nexp01_16f_16f` @ `0x521790` |

```
base subtotal       :  5 mnemonics,  30 placements
predicated subtotal :  5 mnemonics,  30 placements
BATCH TOTAL         : 10 mnemonics,  60 placements   (of the 12569-placement certified cover)
```

**Value-leaf grounding.** All five element functions resolve to a `module__xdref_*` leaf that was
**driven live** (recip0/rsqrt0 fully, including the exhaustive table-correspondence and the
edge sweep; sqrt0/nexp0/nexp01 spot-confirmed). The predicated `…t` siblings share the base leaf
semantics (the mask gates lanes, the per-lane seed is unchanged, §2.3) — they add **no** distinct value
leaf, so the batch's
value-leaf contribution is **5** of the 864 (`recip0`, `rsqrt0`, `sqrt0`, `nexp0`, `nexp01` fp16 forms).
`[HIGH/OBSERVED·exec]`

**Roll-up.** `m = 10` enters the **1065** vector axis; `p = 60` enters the **12569** placement total
(never the pre-fold 12642 — these `xt_ivp32` ops carry no fold forms,
[coverage tally §3](../core/coverage-tally.md#3-the-73-fold--how-160712642-collapses-to-153412569)).

---

## 6. The cycle model — `hp_lookup` is one pipelined unit

The cycle-accurate simulator `libcas-core.so` names this family directly in its **symbol table** (it
ships **no DWARF** — the timing is encoded as per-op ISS callbacks, not a `.debug_*` latency table). The
semantic stage functions `F<n>_F<n>_S3_ALU_<k>_ivpep_sem_hp_lookup_semantic_stage10/14` exist for
**slots `{F0,F1,F2,F3,F7,N0}`** — the exact six this batch's encode thunks place — and the per-op bodies
are modelled as a **16-stage chain** (`…IVP_RECIP0NXF16_inst_stage0 … _stage15`, and
`bbn_sem_vec_sprecip_rsqrt_opcode_stage0 … stage15`). The `ivpep_sem_hp_lookup_*`
symbol set (38 symbols: `_opcode_stage`, `_semantic_stage`, `_vr_use`, `_vt_def`, `_vbr_set_use`, …)
shows the unit reads one `vec` source (`vr_use`) and writes one `vec` dest (`vt_def`); the `_vbr_*`
(vbool-read) symbols are exercised by the **predicated `…t` forms**, whose `_issue` callbacks call
`opnd_sem_vbool_addr` (§2.3) — the base forms read no `vbool`, consistent with the 1-in/1-out element
model of §1.

This is independent cross-validation of the whole analysis: the **`hp_lookup`** (this batch / fp16) vs
**`sp_lookup`** ([B15](b15-sp-lookup.md) / fp32) naming in the simulator is the literal source of the
B14/B15 batch titles, and the simulator's `dll_get_lookup_info` / `dll_lookup_info` accessor confirms a
single shared lookup-table descriptor drives the family.

> **NOTE — the seed lookup is a 16-stage pipelined op; the *structure* is OBSERVED, the *cycle count*
> is not.** The `…_inst_stage0..15` / `sprecip_rsqrt_opcode_stage0..15` chains in the cas model show the
> seed unit is **deeply pipelined**, unlike the 1-cycle fp ALU of [B02](b02-vec-alu-fp.md). But no
> explicit per-instruction latency constant was found: `libcas-core.so` carries no DWARF, and the
> Cairo pipe parameters (`core.xparm`: `pipeAStage=1 … pipeDStage=9`, the deferred-execution end stage)
> give the *machine* pipeline depth, not a "RECIP0 = N cycles" figure. So the page states the **16-stage
> pipeline structure as `[HIGH/OBSERVED]`** (from the stage-symbol roster) and treats a **precise
> retirement cycle number as `[—/CARRIED]`** (the cas-core retirement run is license-gated). The per-lane
> *value* is OBSERVED (§4).

---

## 7. Batch boundary — what this page does NOT own

| neighbor op(s) | owner page | why not here |
|---|---|---|
| `ivp_div0nxf16` / `…t` (fp16 divide-step seed) | [B23 divide](b23-divide.md) | roots on `div` (classifier rule); mantissa-only reciprocal seed for `divn`, distinct element fn (§1 NOTE) |
| `ivp_recip0n_2xf32` / `rsqrt0n_2xf32` / `sqrt0n_2xf32` / `nexp0…32` (fp32 seeds) | [B15 sp-lookup](b15-sp-lookup.md) | fp32 (`n_2xf32`) forms — the `sp_lookup` half of the LUT pair (§6) |
| `mula`/`madd`/`msub`/`maddn` fp16 (the FMA **refine**) | [B18 fp16 FMA](b18-hp-fma.md) | the Newton-Raphson FMA step that consumes the seed (§4.5); fp32 refine → [B17](b17-spfma.md) |
| `ivp_divn…` (the Newton divide combiner) | [B23 divide](b23-divide.md) | combines `div0` seed + dividend into the quotient |
| `recip0.h` / `rsqrt0.h` / `nexp0.h` (scalar-FP, `.h` suffix) | [B24 composite](b24-composite.md) + [fp sub-ISA](../core/fp-sub-isa.md) | the **scalar** `xt_ivpn_scalarfp` siblings (1 lane, AR/FR file), not the `ivp_` vector forms |
| `addexp`/`addexpm` (exponent-add scale) | [B02 fp ALU](b02-vec-alu-fp.md#42-addexp--the-exponent-add-range-reduction-primitive) | the power-of-two scale used *inside* the refine, an fp-ALU op |
| `firound`/`fitrunc` (round-to-integral) | [B13](b13-sp-cvt.md)/[B20](b20-hp-cvt.md) | convert family, used in the exp integer-part extraction |

> **CROSS-LINK — the seed TABLE is shared with the ISS oracle (Part 14).** The `RECIP_Data8` /
> `RSQRT_Data8` tables this page reads are the same `.rodata` constants the
> [ISS value-coverage tally](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)
> certifies as 128/128 reproduced (FISS == SEM == TAB agreement). This page does not re-litigate that
> certificate; it re-reads the bytes and re-derives the construction (§3) to ground its own seed claims,
> and cross-links the shared-table fact. The simulator carries the same tables (`CONST_TBL_RECIP_Data8_0`
> @ `0x17bd580`, `CONST_TBL_RSQRT_Data8_0` @ `0x17bd340` in `libcas-core.so`) plus the second-stage
> `fp_recip_qli_lut1/lut2` correction LUTs the `recipqli` quadratic-refine ops use (a [B23](b23-divide.md)
> / scalar-FP concern, not this batch). `[HIGH/CARRIED]` on the 128/128 ISS agreement; `[HIGH/OBSERVED]`
> on the byte re-read + construction here.

> **NOTE — the firmware axis: `RECIPROCAL = 0x48`, but exp/sqrt go through the activation table.** On the
> separate **firmware kernel-lane opcode** axis (the ~140 `NEURON_ISA_TPB_OPCODE` enum, *not* the 1534
> libisa mnemonics — keep the two axes distinct, per
> [the meta page §1](template-and-partition.md#1-what-part-3-is-and-where-this-page-sits)),
> reciprocal is a first-class opcode `NEURON_ISA_TPB_OPCODE_RECIPROCAL = 0x48` (in the
> `NEURON_ISA_TPB_S4D4_TR_STRUCT` tensor-reduce struct, uniform across the sunda/maverick/mariana/cayman
> gens in `instruction_mapping.json`), but there is **no** dedicated firmware EXP or SQRT opcode — the
> exp/sqrt path is `NEURON_ISA_TPB_OPCODE_ACTIVATE` + the `ACTIVATION_TABLE_LOAD` mechanism. The
> firmware `RECIPROCAL` macro is *implemented* by the vector seed-lookup + Newton kernels documented here;
> the firmware-opcode reconciliation is [B30](b30-appendix-p.md)'s job, not this batch's.

---

## 8. Adversarial self-verification — the five strongest claims, re-challenged

A claim survives only if a *second* independent witness agrees.

1. **`recip0` seed == `RECIP_Data8[m>>3]/256` — the seed IS the table byte.** *Challenge:* could the
   leaf be interpolating between table points, the apparent match being coincidence at sampled inputs?
   *Re-test:* the seed is **piecewise-constant within all 128 buckets** (one distinct value per
   8-mantissa bucket, live) — interpolation would vary it within a bucket; and `seed == RECIP_Data8[m>>3]
   /256` holds with **0 mismatches over all 1024 mantissas** of `[1,2)`, not a sample. Cross-witness: the
   table itself reconstructs as `round(256/xm)` to 127/128 exact. **Survives.**

2. **`rsqrt0` indexes by exponent-parity into two binade halves.** *Challenge:* maybe the index is just
   `m>>3` like recip and the "two halves" are an artifact? *Re-test:* `rsqrt0(2.0)` (e=16) and
   `rsqrt0(4.0)` (e=17) read *different* table halves (`RSQRT[0]=0xb4` vs `RSQRT[64]=0xff`) for adjacent
   exponents, and `seed == RSQRT_Data8[(e&1)<<6 | (m>>4)]/256` holds with **0 mismatches over all 1024
   mantissas** of both `[1,2)` and a `[2,4)` sweep. The `0x80→0xff` jump at table index 64 is the binade
   boundary, independently re-derived from `round(256/sqrt(xm))`. **Survives.**

3. **These are seeds, not transcendentals — `recip0(2.0)=0.498`, not `0.5`.** *Challenge:* could the
   0.498 be a harness rounding bug? *Re-test:* `recip0(2.0)=0x37f8` reads bit-exact as `0.498047 =
   0xff/256 · 2^-1` — the exact `RECIP_Data8[0]` byte, not a perturbed `0.5`; and the **full-range** max
   relative error is **0.732 %** (7.1 bits), the precise accuracy a 128×8-bit table delivers. Two Newton
   iterations then reach bit-exact (`recip0(1.5)`: seed 0.391 % → NR2 0.00000 %). **Survives.**

4. **10 mnemonics / 60 placements; the `…t` is a distinct, predicated opcode (not a flag bit, not a
   "throttle").** *Challenge:* maybe the `…t` forms are aliases (no own placements), or the count is
   grepped from the decompile? *Re-test:* `nm | rg -c` gives each of the 10 mnemonics **exactly 6**
   `Opcode_…_Slot_…_encode` symbols (60 total), and the base/`…t` selectors at `F1_S3_ALU` have **no
   bit/additive relation** (`recip0`=`0x26310106` vs `recip0t`=`0x2b080202`) — separate `opcodes[]` rows.
   Cross-witness: the cas-model `…NXF16T_issue` callback calls `opnd_sem_vbool_addr` (an extra `vbool`
   operand the base form omits) and dedicated `_issue`/`_stall` callbacks exist only on the `…t` forms —
   the predicated semantics, not a power flag; the string "throttle" is absent from all three binaries.
   Count witness is the `nm`-symbol population, never the decompile. **Survives.**

5. **The family is the simulator's `hp_lookup` unit, S3_ALU, six slots, ~16-stage pipeline.**
   *Challenge:* could the `S3_ALU` slot assignment be inferred rather than observed? *Re-test:* the
   encode thunks place all 10 ops at `{F0,F1,F2,F3,F7,N0}_s3_alu` (read from the symbol names), **and**
   the independent cas-core simulator names `F<n>_F<n>_S3_ALU_<k>_ivpep_sem_hp_lookup_semantic_stage*`
   for the same six slots — two binaries, same slot set. The `bbn_sem_vec_sprecip_rsqrt_opcode_stage0..15`
   chain gives the 16-stage count. **Survives.**

No claim on this page rests on a raw dump, an unnamed symbol, or a single uncorroborated witness; every
seed value carries a differential-execution certificate against the shipped leaf and the re-derived
`.rodata` table, and the encode/slot facts are double-witnessed by `libisa-core.so` and `libcas-core.so`.

---

## 9. Confidence ledger

**HIGH / OBSERVED (by execution)** — driven live against `libfiss-base.so`:

* `recip0` seed == `RECIP_Data8[m>>3]/256` (0 mismatches / 1024 mantissas), bucket-constancy across all
  128 buckets, exponent reciprocation, full-range 0.732 % / 7.1-bit accuracy.
* `rsqrt0` seed == `RSQRT_Data8[(e&1)<<6 | (m>>4)]/256` (0 mismatches / 1024 mantissas, both binades),
  the two-half binade structure, the `0x80→0xff` boundary.
* The edge behaviours: `recip0(±0)=±inf`, `recip0(±inf)=±0`, `recip0(NaN)=quiet-NaN`,
  denormal-via-`bsr` (not flushed), `rsqrt0(neg)=NaN`, `rsqrt0(+0)=+inf`.
* The Newton-Raphson refine (`recip: y(2−xy)`, `rsqrt: y(1.5−0.5xy²)`) reaching bit-exact fp16 in 2
  iterations from the live seed.
* `nexp0`/`nexp01` returning the `(n, −2^k·f)` exp range-reduction decomposition.

**HIGH / OBSERVED** — read from `libisa-core.so` immediates / disassembly + `libcas-core.so` symbols + the
device assembler:

* The 10-mnemonic roster, the 60-placement census, the `F1_S3_ALU` selectors (base + predicated `…t`), the
  slot-locality of the immediate, the `word1=0` / narrow-`N0` thunk shapes.
* `RECIP_Data8` / `RSQRT_Data8` byte contents + their `recip_tab` / `rsqrt_tab` identity (512-byte `xxd`
  compare), the `round(256/xm)` / `round(256/√xm)` construction (127/128 and 64/64+64/64 exact).
* The `ivpep_sem_hp_lookup` / `bbn_sem_vec_sprecip_rsqrt` cas-model family, the six S3_ALU slots, the
  16-stage pipeline roster. Device round-trip: `recip0.h v3,v4` → `32514d0040cf452f` →
  `{ nop; nop; nop; recip0.h v3, v4 }` (assembler+objdump, `XTENSA_CORE=ncore2gp`).

**MED / OBSERVED**

* `nexp0`/`nexp01` exact octave-mask interior (structure read; not exhaustively swept); `sqrt0` exponent
  fix-up (spot-confirmed via `sqrt(x)=x·rsqrt0(x)`, not full-range).

**CARRIED**

* The device hardware's internal seed-coefficient bytes / gate-level reduction (the FW-42 wall, §intro);
  a precise retirement *cycle count* (cas-core run license-gated — staged structure is OBSERVED, the
  number is CARRIED); the ISS 128/128 FISS==SEM==TAB certificate (re-grounded by byte re-read here).

**Deferred (not in this batch's denominator)**

* `div0nxf16` → [B23](b23-divide.md); fp32 `*0n_2xf32` seeds → [B15](b15-sp-lookup.md); the FMA refine →
  [B18](b18-hp-fma.md)/[B17](b17-spfma.md); scalar `.h` siblings → [B24](b24-composite.md).

All facts read as derived from shipped-artifact static analysis and license-free in-process execution of
the binary's own value leaves and the device-native assembler (lawful interoperability RE).

---

## Cross-references

* [Template & 30-Batch Partition](template-and-partition.md) — the B14 partition row, the canonical
  per-instruction schema, the encode-thunk ABI, and the no-cross-pair count law this batch closes onto.
* [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the `12569`-placement cover
  this batch's `60` is a slice of, the `864/864` value-leaf cover, **and the 128/128 seed-table ISS
  certificate this page's `RECIP_Data8`/`RSQRT_Data8` share** (Part-14 cross-link, §7).
* [The Floating-Point Sub-ISA (FCR/FSR view)](../core/fp-sub-isa.md) — the NaN/round model, the two-level
  RNE/RZ rounding, and the scalar-FP `.h` siblings of these vector seeds.
* [The Eight Register Files](../core/register-files.md) — the `vec` (idx 2) `NXF16` 32-lane datapath
  these ops read and write.
* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid, the
  `{F0,F1,F2,F3,F7}` 16-byte wide / `N0` 8-byte narrow `S3_ALU` slots these selectors target, and the
  encode-thunk `word0`/`word1` ABI.
* [B02 vector ALU (fp slice)](b02-vec-alu-fp.md) — the fp16/fp32 ALU framing, the slot-local-selector and
  precision-bit GOTCHAs this page's predicated-`…t`-opcode GOTCHA parallels, and `addexp` (the scale used in the
  refine).
* [B15 fp32 transcendental seeds (`sp_lookup`)](b15-sp-lookup.md) — the fp32 half of the LUT pair (same
  `recip0`/`rsqrt0`/`sqrt0`/`nexp0` algebra at `n_2xf32` width).
* [B18 fp16 FMA](b18-hp-fma.md) · [B17 fp32 FMA](b17-spfma.md) — the Newton-Raphson FMA refine that
  consumes the seed; [B23 divide](b23-divide.md) — `div0`/`divn`, the divide-step seed + combiner.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the FW-42 seed-coefficient
  wall, and the proven-by-execution value lane (`libfiss-base.so` via ctypes) grounding every seed claim.
