# ISA Batch 18 — fp16 Fused Multiply-Add (hp_fma)

This batch is the **half-precision (IEEE binary16) fused-multiply-add datapath** of the Vision-Q7
*Cairo* (`ncore2gp`) vector ISA: the `fp_sem_hp_fma` semantic group — one true single-rounding FMA
primitive `d = round_rm(±(a·b) ± c)`, applied independently across the 32 binary16 lanes of a 512-bit
`vec` register. It is the fp16 half of the engine's floating-point arithmetic core; its fp32 peer is
[B17 — fp32 Fused Multiply-Add (spfma)](b17-spfma.md), which is the **same** s0/s1/s2 multiply →
align → normalize tree placed at `N_2XF32` (16×binary32) lane width. The two pages are framed in
parallel: identical selector/operand/timing/semantic structure, only the lane width and significand
size differ. The min/max/minnum/maxnum/compare/neg/abs/clsfy fp16 ops are a **different** semantic
group (the fp ALU slice) and are not owned here — this batch owns arithmetic FMA only.

It owns exactly **27 mnemonics** across three spellings of one shared op-class set, all in package
`xt_ivp32`, all on the `vec` regfile. The [partition classifier](template-and-partition.md) routes
these here by the `fp_sem_hp_fma` semantic membership (a clean partition slice — 0 cross-group
overlap, §1); it sits between the integer multiply-accumulate of
[B04](b04-mac-integer.md)/[B05](b05-mac-mixed.md) and the fp32 FMA of [B17](b17-spfma.md).

Everything below is **grounded in the shipped binaries**: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte; `Field_fld_*` get/set
thunks executed in-process to recover the operand bit map); the **value semantics** by *executing* the
matching `module__xdref_*_16f` leaves in `libfiss-base.so` live via `ctypes` (every output-bit pattern
below is a real return value of the shipped binary, license-free); the **issue/timing model** from the
per-format `*_fp_sem_hp_fma_semantic_stage<N>` leaves in `libcas-core.so`; and a byte-exact
encode/decode oracle from the device-native `xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`).
The page default is `[HIGH/OBSERVED]` (read-from-byte / proven-by-execution); claims that depart from
that default carry an explicit tag per [the Confidence & Walls model](../../reference/confidence-model.md)
— `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic and binaries.** Three binaries are cited, all under
> `extracted/` (gitignored; reach with `fd --no-ignore` or an absolute path):
> `libisa-core.so` (sha256 `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`, 9 690 712 B,
> ET_DYN x86-64, **not** stripped — the encode/field thunks are local symbols, resolved by address);
> `libfiss-base.so` (sha256 `260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94`, 12 330 016 B —
> `.text` VMA `0x190430` and `.rodata` VMA `0x88ff00` are **VMA == file-offset**, so the `xdref` addresses
> below are valid file offsets; the fp16 value leaves are **exported** `T` symbols, callable by name);
> `libcas-core.so` (sha256 `7f1d86da52891b3c65533d394ace4902b101536fedb31dff7ed976dc40b1041a`, 45 878 080 B —
> DWARF, the timing leaves). For `libisa-core.so`, `.text`/`.rodata` are VMA==file-offset; `.data.rel.ro`
> (VMA `0x67bb00` ↔ file `0x47bb00`) carries the per-binary delta **`0x200000`** — **not** libtpu's
> `0x400000`.

---

## 0. Headline

* **One datapath, three spellings, 27 mnemonics.** The group is nine op-classes (ADD, SUB, MUL,
  MADD/MULA, MADDN/MULAN, MSUB/MULS, MSUBN/MULSN, MULSONE, DIVN) × three mnemonic spellings (IVP vector
  base `*nxf16`, IVP predicated `*nxf16t`, Tensilica-FP scalar `.H`). All 27 are present as distinct
  opcodes in `libisa-core.so` (each with multiple slot placements), `nm`-verified.
* **True single rounding.** The product `a·b` is kept exact (full 22-bit significand) into the aligned
  add; the result is rounded **once** to binary16. Proven by *executing* the shipped `madd16` leaf on a
  catastrophic-cancellation input where fused and two-step rounding differ: the binary returns the fused
  answer `0x0ffe`, not the two-step `0x0000` (§5.3).
* **Directional NaN propagation** (the trap a naive reference gets wrong): ADD/SUB return the **first**
  NaN operand, MUL returns the **second**, the fused `a + b·c` propagates in **a, then c, then b** order;
  signalling NaNs are quieted with sign+payload preserved. All live-confirmed bit-for-bit (§5.2).
* **Round-half-to-even** under the FCR 2-bit `RoundMode` {0=RNE, 1=RZ, 2=RU, 3=RD}: the `0.1+0.2` fp16
  sum is the exact midpoint of `0x34cc`/`0x34cd`; the live binary returns `0x34cc` (even) under RNE,
  `0x34cd` under RU, `0x34cc` under RD/RZ (§5.1).
* **This page reclaims the fp16 FMA forms that [B04](b04-mac-integer.md)/[B05](b05-mac-mixed.md)
  over-counted as integer MAC.** The nine `ivp_mula/mulan/muls/mulsn*nxf16` and the four `madd/maddn/
  msub/msubn.h` spell like the integer `mul`/`mula` MAC family but are **fp16 FMA** (semantic group
  `fp_sem_hp_fma`, not the integer MAC group). See the [CORRECTION in §1](#1-group-identity--roster).

---

## 1. Group identity & roster

**Semantic group:** `fp_sem_hp_fma` — 27 members, a clean partition slice (every member belongs to this
group and to none of the other semantic groups). **Lane format:** `NXF16` = 32 × IEEE binary16
(1 sign / 5 exp / 10 mantissa, bias 15). **Regfile:** `vec` (512-bit, viewed here as 32×binary16); the
`vbr` predicate of the `t`-forms is on `vbool`. The operands bind to the FMA as:

* **a** (first multiplicand) = `vs` — `opnd_fp_sem_hp_fma_vs`
* **b** (second multiplicand) = `vr` — `opnd_fp_sem_hp_fma_vr`
* **c** (addend / accumulator) = `vt` — `opnd_fp_sem_hp_fma_vt` (read only by the accumulate classes)
* **d** (result) = `vt` — written by every class

The 27 members, grouped by op-class (all `nm`-verified present as distinct opcodes — `27/27`, none
missing):

| op-class | IVP base | IVP pred (`t`) | scalar `.H` | value `d` (single round) |
|---|---|---|---|---|
| ADD | `ivp_addnxf16` | `ivp_addnxf16t` | `add.h` | `d = a + b` *(degenerate FMA)* |
| SUB | `ivp_subnxf16` | `ivp_subnxf16t` | `sub.h` | `d = a - b` *(degenerate FMA)* |
| MUL | `ivp_mulnxf16` | `ivp_mulnxf16t` | `mul.h` | `d = a·b` |
| MADD / MULA | `ivp_mulanxf16` | `ivp_mulanxf16t` | `madd.h` | `d = c + a·b` *(c = vt, acc)* |
| MADDN / MULAN | `ivp_mulannxf16` | `ivp_mulannxf16t` | `maddn.h` | `d = c + a·b` *(no-imprecise path)* |
| MSUB / MULS | `ivp_mulsnxf16` | `ivp_mulsnxf16t` | `msub.h` | `d = c - a·b` *(c = vt, acc)* |
| MSUBN / MULSN | `ivp_mulsnnxf16` | `ivp_mulsnnxf16t` | `msubn.h` | `d = c - a·b` *(no-imprecise path)* |
| MULSONE | `ivp_mulsonenxf16` | `ivp_mulsonenxf16t` | `mulsone.h` | `d = 1 - a·b` *(c forced 1.0)* |
| DIVN | `ivp_divnnxf16` | `ivp_divnnxf16t` | `divn.h` | Newton division step (§5.5) |

The IVP `mula`/`mulan`/`muls`/`mulsn` and the `.H` `madd`/`maddn`/`msub`/`msubn` are **distinct
mnemonics** (distinct selector CONSTs, §3) that bind the **same** internal op-class — the spelling is a
decode-time mux (`op_scalar`/`op_vector`, §4). The `.H` scalar forms have **no `t` predicated variant**
(predication is the IVP `*nxf16t` spelling only — verified: no `*_h*t_Slot_*` opcodes exist). `[HIGH/OBSERVED]`

> **CORRECTION — `mula/madd`, `muls/msub` are fp16 FMA, not integer MAC.** The
> [B04](b04-mac-integer.md)/[B05](b05-mac-mixed.md) coverage walk lists `mul`/`mula`-spelled mnemonics
> under the integer multiply-accumulate family. The nine `ivp_{mula,mulan,muls,mulsn,mulsone}nxf16` and
> their `.H` peers `{madd,maddn,msub,msubn,mulsone}.h` are **half-precision FMA**: their semantic group
> is `fp_sem_hp_fma`, they read the FCR `RoundMode`, they emit the IEEE FSR flags, and their value leaves
> are `module__xdref_{mula,madd,muls,msub,mulsone}_…_16f_…` in `libfiss-base.so` (executed live below) —
> *not* the integer `mul_24_8_8` / `mul_48_16_16` accumulator leaves. The disambiguator is the `nxf16`
> lane-format suffix (fp16) vs the integer-MAC width suffixes, plus the semantic-group membership read
> from the binary. B04/B05 must subtract these from any integer-MAC denominator they quote; this page is
> the authoritative home of the fp16 multiply-add forms.

---

## 2. Operand signature & field map

From the ICLASS ARG_LIST the data operands are `vt`/`vs`/`vr` (+`vbr` for the `t`-forms). The **direction**
distinguishes the op-classes — this is the heart of the FMA structure:

| op-class | `vt` | `vr` | `vs` | `vbr` | accumulator binding |
|---|---|---|---|---|---|
| ADD/SUB/MUL/MULSONE | OUT only | IN (b) | IN (a) | — | no accumulator read (c implicit = 1.0 for MULSONE) |
| MADD/MADDN/MSUB/MSUBN | IN + OUT (RMW) | IN (b) | IN (a) | — | **c = vt**, read-modify-write |
| DIVN | IN + OUT (RMW) | IN | IN | — | vt = running quotient estimate |
| `*t` forms | as above | IN (b) | IN (a) | IN | `vbr` = per-lane keep/kill mask (`vbool`) |

### 2.1 Slot-local field bit map (F0/S3 ALU — the primary carrier)

The bit positions below were recovered by **executing the `Field_fld_fp_sem_hp_fma_*_Slot_f0_s3_alu_{get,set}`
thunks in-process** (resolved by address — they are local symbols) on a known register pattern and reading
back. The thunks operate on the slot-local encode word (the 32-bit value the encode thunk writes to
`(%rdi)`); the recovered positions are:

| field | bits (slot-local word) | width | role |
|---|---|---|---|
| `vt` (OUT / IN-acc d/c) | `[14:10]` | 5 | contiguous |
| `vs` (IN a) | `[9:8] ‖ [3:1]` | 5 | MSB-first split |
| `vr` (IN b) | `[7:4] ‖ [0]` | 5 | MSB-first split |
| `vbr` (pred, `t` only) | `[18:15]` | 4 | `vbool` select |

> **CORRECTION — `vs`/`vt` are swapped relative to a transposed field-map reading; the worked slot words
> are `…0801`, not `…0404`.** Driving the `set` thunks for `ivp_mulanxf16 v2,v0,v1` (`vt=2`, `vs=0`, `vr=1`)
> and reading the `get` thunks back yields the slot-local word **`0x80960801`** (selector `0x1012c<<15`
> plus operand bits `0x801`), and `ivp_addnxf16 v2,v0,v1` yields **`0x86bf0801`**. The contiguous `[14:10]`
> field holds **`vt`** (the destination/accumulator), and the split `[9:8]‖[3:1]` field holds **`vs`** —
> a field-map that places `vs` at the contiguous slot and `vt` at the split would invert this and produce
> the wrong operand bits (`0x…0404`). The `set`→`get` round-trip is fully binary-driven (no inference):
> reading back gives `vt=2, vs=0, vr=1` exactly.

For an asymmetric pattern `ivp_mulanxf16 v7,v3,v5` (`vt=7`, `vs=3`, `vr=5`), the same thunks produce
slot-local word **`0x80961c27`**, and the device oracle (§7) round-trips the N1 bundle
`00a54e094a43352f` back to `ivp_mulanxf16 v7,v3,v5` — confirming the field placement end-to-end.
Other slots use distinct splits (the `Field_fld_*_Slot_<fmt>_<slot>_*` thunk set spans
`f0_s3_alu`, `f1_s2_mul`, `f1_s3_alu`, `f2_s2_mul`, `f2_s3_alu`, `f3_s3_alu`, `f7_s2_mul`,
`f7_s3_alu`, `n1_s2_mul` — the full per-slot scatter is the [FLIX decoder](../core/fp-sub-isa.md)'s
domain). `[HIGH/OBSERVED]`

---

## 3. Encoding — formats, slots, opcode-selector CONSTs

### 3.1 Slot placement

Each op is placed across the wide FLIX formats {F0, F1, F2, F3, F7} and the narrow N1 bundle. The fp-FMA
op lives in the ALU slot **S3** and, in F1/F2/F7, additionally borrows the **Mul** slot **S2** (so a wide
bundle can co-issue two FMAs — the "2×FMA" of [vfpu-ieee](../../uarch/vfpu-ieee.md)). Placement counts
(`nm | rg -c` on the `Opcode_*_Slot_*_encode` thunks):

| op-class | base placements | `t` placements |
|---|---|---|
| ADD / SUB / MUL / MULSONE | 9 | 9 |
| MADD / MADDN / MSUB / MSUBN / DIVN | 7 | 7 (DIVN`t`: 6) |

The **F0/S3 ALU** placement (20-bit selector at slot-local bits `[34:15]`) is the canonical carrier and is
used for the worked examples (§7). `[HIGH/OBSERVED]`

### 3.2 Per-format selector widths (the slot-local bit-range the CONST must equal)

| format/slot | base selector | `t` selector |
|---|---|---|
| F0/S3 ALU | bits `[34:15]` (20b) | bits `[34:19]` (16b); `vbr` @ `[18:15]` |
| F1/S2 Mul | bits `[28:15]` (14b) | — |
| F1/S3 ALU | bits `[31:15]` (17b) | — |
| F2/S2 Mul | `[29:20]`+`[8]`+`[3:0]` (composite) | `[29:20]` only |
| F2/S3 ALU | bits `[30:15]` (16b) | `[30:24]`+`[19:15]` |
| F3/S3 ALU | bits `[25:15]` (11b) | — |
| F7/S2 Mul | bits `[28:15]` (14b) | — |
| F7/S3 ALU | bits `[32:15]` (18b) | — |
| N1/S2 Mul | bits `[27:15]` (13b) | — |

### 3.3 Primary (F0/S3, 20-bit) selector CONST — the authoritative discriminator

Every value below was read **byte-for-byte** from the `Opcode_<mnem>_Slot_f0_s3_alu_encode` thunk body
(`movl $imm,(%rdi); ret`) and divided down: the F0/S3 20-bit selector is `enc >> 15`, verified by the
in-process `set`/`get` round-trip (the slot word equals `selector<<15 | operand-bits`).

| op-class | `IVP_*NXF16` (base) | `.H` scalar | `IVP_*NXF16T` (pred) |
|---|---|---|---|
| ADD | `0x10d7e` | `0x10d7d` | `0x1049` |
| SUB | `0x101cc` | `0x101ad` | — |
| MUL | `0x1014c` | `0x1018d` | — |
| MADD/MULA | `0x1012c` | `0x1010d` | `0x104f` |
| MADDN/MULAN | `0x1010c` | `0x101ec` | `0x104d` |
| MSUB/MULS | `0x1018c` | `0x1014d` | `0x108d` |
| MSUBN/MULSN | `0x1016c` | `0x1012d` | `0x108b` |
| MULSONE | `0x101ac` | `0x1016d` | `0x108f` |
| DIVN | `0x10d5f` | `0x10d5e` | `0x104b` |

The IVP base and the `.H` spelling differ by a small constant per op-class (ADD: `0x10d7e` vs `0x10d7d`;
DIVN: `0x10d5f` vs `0x10d5e`) — adjacent decode points into the same op-class mux, separated by the
`op_vector`/`op_scalar` prefanout (§4). The SUB/MUL `t` forms are not placed at F0/S3 (no F0/S3
`t`-encode thunk; their predicated variants live in other slots).

> **CORRECTION — the IVP base selectors for SUB and MUL are `0x101cc` and `0x1014c`.** The encode-thunk
> bodies read `movl $0x80e60000,(%rdi)` (SUB) and `movl $0x80a60000,(%rdi)` (MUL), giving
> `0x80e60000>>15 = 0x101cc` and `0x80a60000>>15 = 0x1014c`. A reading that lists the IVP base SUB/MUL
> selectors as `0x101ad`/`0x1018d` has accidentally copied the **`.H` scalar** selectors (`sub.h`=`0x101ad`,
> `mul.h`=`0x1018d`) into the IVP column — the IVP and `.H` selectors are *not* equal for SUB/MUL (they
> differ by `0x1f` and `0x41` respectively, exactly as they differ for every other op-class). The
> remaining seven IVP base selectors above match independent transcription; only SUB/MUL needed the fix.
> The F0/S3 `t`-form ADD selector is **`0x1049`** (from `movl $0x82480000`, `>>19`), not `0x1089`.

### 3.4 State / exception args per op-class

All carry `STATE_IN=CPENABLE`, `EXC=Coprocessor1Exception` (the cp1 gate — raised at issue, op squashed
before any datapath effect), and `OUT VectorPipeImpreciseErr` (the deferred fp exception) *except* the
`N` forms. The fp control inputs differ by class — the op-specific IEEE-enable subset:

| op-class | RoundMode | Invalid | Overflow | Underflow | Inexact | ImpreciseErr |
|---|---|---|---|---|---|---|
| ADD / SUB | yes | yes | yes | — | yes | yes |
| MUL / MULSONE | yes | yes | yes | yes | yes | yes |
| MADD / MSUB / MULA / MULS | yes | yes | yes | yes | yes | yes |
| MADDN / MSUBN / MULAN / MULSN (`N`) | — | — | — | — | — | — |
| DIVN | yes | — *(no Invalid)* | yes | yes | yes | yes |
| `*t` variants | = their non-`t` base class | | | | | |

ADD/SUB omit `UnderflowEnable` (a sum cannot create a new denormal that a product can); DIVN omits
`InvalidEnable` (the Newton step does not signal Invalid — the seed/recip stage does); the `N` forms take
neither `RoundMode` nor enables nor emit flags (the fast un-flagged path used inside iterative kernels).
`RoundMode` is a 2-bit FCR field {0=RNE, 1=RZ, 2=RU(+∞), 3=RD(−∞)}, reset `2'h0`=RNE; the
`{Inv,Ovf,Unf,Inx}Flag` FSR fields are `shared_or` (the 32 lanes sticky-OR into one IEEE flag). FCR/FSR
are read/written by the `rur/wur fcr/fsr` group; the full register model is in
[vfpu-ieee](../../uarch/vfpu-ieee.md).

---

## 4. The shared FMA datapath

The `fp_sem_hp_fma` semantic `<STATEMENTS>` body is empty in this config — the compute is the compiled
ISS leaf (the soft-float value function, §5) and the silicon FP pipe. The microarchitecture is recovered
from the `INSTR_SCHEDULE` intermediate USE/DEF signal names (a 3-stage s0/s1/s2 pipeline), confirmed by
the `libcas-core.so` per-format stage leaves (§6):

* **s0 — multiply + classify:** a 2×2 radix-split partial-product grid of the 11-bit significands
  (`m00_mux`/`m01_mux`/`m10_mux`/`m11_mux`) summed into the 22-bit product `s0_axb_sig_pp_h`/`_l`;
  `s0_exp_sum = exp(a)+exp(b)−bias`; `s0_axb_is_neg = sign(a)⊕sign(b)`; `negate_axb_M` negates the product
  leg (MSUB/MULS/MSUBN/MULSN); `s0_c_is_neg`/`s0_negate_c` handle the addend sign; `s0_out_is_inf`/`_nan`/
  `_snan` detect special results early; `s0_roundm` latches the round mode.
* **s1 — align + fused add:** `s1_c_shift` aligns the addend significand to the product exponent;
  `s1_sum_sig_a` is the **single fused add** on the *exact* (not pre-rounded) product — the defining FMA
  property (proven in §5.3).
* **s2 — normalize + round + flag:** LZC normalize, round under `s0_roundm`, generate the IEEE flags.

A decode-time op-class mux routes all 27 mnemonics through this one tree: `add_only`/`mul`/`madd`,
`op_negadd` (SUB/MSUB), `op_maddn` (the `N` no-imprecise path, threaded `s0/s1/s2_maddn`), `op_mulsone`
(c forced 1.0), `op_divn` (the Newton path), `op_pred_t`/`op_pred_f` (the `t` predicate), `op_scalar`/
`op_vector` (the `.H` vs IVP spelling), and `op_mux_sel` (the top-level select, driven by the §3.3
selector CONST). `[signal names HIGH/OBSERVED from the schedule; gate-level meaning INFERRED from the
names + the executed value behaviour]`

---

## 5. Semantics — bit-exact per-lane value functions, driven LIVE

Each op is 32 independent invocations of the scalar binary16 FMA primitive (no cross-lane carry, so a
per-lane reference is a complete vector-op reference). **Every output-bit pattern in this section is a
real return value of `libfiss-base.so`, executed in-process via `ctypes`.** The fp16 value leaves and
their addresses (`nm`-verified, `.text` VMA==file-offset so these are valid file offsets):

```
module__xdref_add_1_1_1_16f_16f_16f_2          @ 0x51c640   (out[3] = result, out[0] = inexact flag)
module__xdref_sub_1_1_1_16f_16f_16f_2          @ 0x51ce70   (out[3] = result)
module__xdref_mul_1_1_1_1_16f_16f_16f_2        @ 0x51d640   (out[4] = result)
module__xdref_madd_1_1_1_1_16f_16f_16f_16f_2   @ 0x51dde0   (out[4] = result; form a + b·c, args a,b,c)
module__xdref_msub_1_1_1_1_16f_16f_16f_16f_2   @ 0x51ea40   (out[4] = result; form a − b·c, args a,b,c)
module__xdref_mula_…_16f_16f_16f_16f_2         @ 0x5252b0   (= madd op-class; verified == madd, §5.4)
module__xdref_mulsone_1_1_1_1_16f_16f_16f_2    @ 0x5b9a80   (out[4] = result; form 1 − a·b)
```

The ABI (live-probed): `f(xstate, A, B[, C], roundC, *out…)`; the `RoundMode` is `roundC` bits `[1:0]`
{0=RNE 1=RZ 2=RU 3=RD}. The ISS computes this **integer-only** — the `add16` body at `0x51c640…0x51ce70`
contains **0 hardware-FP instructions** (disassembled: no `addss`/`mulss`/`cvtss`/`fadd`/…),
and 13 references to the binary16 field masks `0x7c00`/`0x3ff` — i.e. it cracks the IEEE fields and rounds
in integer ALU, taking `RoundMode` as a parameter. This is a bit-accurate model of the silicon FP pipe;
the two are one value function (see [vfpu-ieee §soft-float reconciliation](../../uarch/vfpu-ieee.md)). `[HIGH/OBSERVED]`

### 5.1 Core FMA + the round-half-to-even trap

```c
// Per lane k, single rounding to binary16 under RoundMode rm. (c = vt; module__xdref_*_16f leaf.)
//   MUL      d = round_rm( a · b )
//   ADD      d = round_rm( a + b )
//   SUB      d = round_rm( a - b )
//   MADD     d = round_rm( c + a·b )    // ONE rounding total; product kept exact into the add
//   MSUB     d = round_rm( c - a·b )
//   MULSONE  d = round_rm( 1.0 - a·b )  // recip-refinement residual
```

**Round-half-to-even, LIVE** — `add16(0.1, 0.2)` where `0x2e66 + 0x3266` is the *exact* midpoint between
`0x34cc` and `0x34cd`:

```
add(0x2e66, 0x3266)  RNE → out[3] = 0x34cc   (ties to EVEN neighbour)   out[0]=1 (inexact)
add(0x2e66, 0x3266)  RU  → out[3] = 0x34cd   (round up — RoundMode is a real free parameter)
add(0x2e66, 0x3266)  RD  → out[3] = 0x34cc   (round down)
add(0x2e66, 0x3266)  RZ  → out[3] = 0x34cc   (truncate)
```

The same input rounds to **three different lanes** under RNE/RU/RD — the round mode is honoured exactly,
and the RNE tie goes to the even neighbour.

### 5.2 NaN propagation (the directional trap), LIVE

A naive reference that "returns whichever NaN it sees first in evaluation order" gets this wrong because
the order is op-class-specific. Driven live (RNE):

```
add(0x7e00, 0xfe00) → 0x7e00   ADD/SUB return the FIRST NaN operand
sub(0x7e00, 0xfe00) → 0x7e00   (same first-operand rule as ADD)
mul(0x7e00, 0xfe00) → 0xfe00   MUL returns the SECOND NaN operand
add(0x7c00, 0xfc00) → 0x7e00   +inf + (−inf) → qNaN 0x7e00 (Invalid)
mul(0x0000, 0x7c00) → 0x7e00   0 · inf → qNaN (Invalid)
add(0x7c01, 0x3c00) → 0x7e01   sNaN + 1 → QUIETED (qNaN MSB set; sign+payload kept)
add(0xfd55, 0x3c00) → 0xff55   −sNaN (payload 0x155) → quieted to 0xff55 (sign+payload kept)
```

For the fused `madd` (form `a + b·c`, args `a, b, c`), the propagation order is **a, then c, then b**
(the addend `c` outranks the multiplicand `b`) — disambiguated live with distinct payloads:

```
madd(a=0x7e01, b=0x7e02, c=0x7e03) → 0x7e01   a NaN wins (highest priority)
madd(a=fin,   b=0x7e02, c=fin)     → 0x7e02   only b NaN → b
madd(a=fin,   b=fin,    c=0x7e03)  → 0x7e03   only c NaN → c
madd(a=fin,   b=0x7e02, c=0x7e03)  → 0x7e03   b AND c NaN → c wins over b
```

> **GOTCHA — the fused-madd NaN order is a, c, b — not a, b, c.** The addend/accumulator `c` (= `vt`) is
> checked **before** the multiplicand `b`. A reference that walks the operands in mnemonic/argument order
> `(a, b, c)` and returns the first NaN will diverge whenever both `b` and `c` are NaN: it returns `b`'s
> payload, the silicon returns `c`'s. Live-confirmed: `madd(fin, 0x7e02, 0x7e03) → 0x7e03`.

### 5.3 Single rounding — the catastrophic-cancellation proof, LIVE

The defining FMA property is that the product is *not* separately rounded. The discriminating input is a
catastrophic cancellation where rounding the product first destroys the residual:

```
b = 0x3c01 = 1 + 2^-10,   c = 0x3bff = 1 - 2^-10  →  b·c = 1 - 2^-20  (exact)
a = 0xbc00 = -1.0
  FUSED   : a + (b·c) = -2^-20            → RNE = 0x0ffe   (a subnormal residual)
  TWO-STEP: a + round(b·c) = -1 + 1 = 0   → RNE = 0x0000
  LIVE  madd(a=0xbc00, b=0x3c01, c=0x3bff) → 0x0ffe   ==  FUSED   (NOT two-step)
```

The shipped `madd16` leaf returns **`0x0ffe`**, the fused single-rounded answer — conclusive evidence
that the product is kept exact into the add and rounded once. A two-step `a + round(b·c)` would have
returned `0x0000`. `[HIGH/OBSERVED — executed]`

### 5.4 Op-class aliasing, overflow, zero-sign (LIVE)

`mula` and `madd` are the same op-class (both return `0x4700` for `mula/madd(1.0, 2.0, 3.0)` = `1 + 2·3 =
7.0`, single round on the exact product 6.0). `msub(1.0, 2.0, 3.0) → 0xc500 = −5.0` confirms the leaf form
`a − b·c`. Overflow is **round-mode-dependent**, and the exact-zero-sum sign follows the IEEE rule:

```
mula(0x3c00,0x4000,0x4200) → 0x4700   madd(0x3c00,0x4000,0x4200) → 0x4700   (same op-class)
add(0x7bff,0x7bff)  RNE → 0x7c00   65504+65504 → +inf
add(0x7bff,0x7bff)  RU  → 0x7c00         RZ → 0x7bff   RD → 0x7bff   (overflow → max-finite away from ∞)
add(0x3c00,0xbc00)  RNE → 0x0000   1 + (−1) → +0
add(0x3c00,0xbc00)  RD  → 0x8000   1 + (−1) under RD(−∞) → −0   (RD-specific exact-zero sign)
add(0x8000,0x8000)  RNE → 0x8000   (−0) + (−0) → −0
```

> **QUIRK — overflow and exact-zero sign are round-mode-dependent on the same operands.** `0x7bff+0x7bff`
> overflows to `+inf` (`0x7c00`) under RNE/RU but clamps to the largest finite `0x7bff` under RZ/RD
> (round-to-max-finite when the mode rounds *away* from the infinity). Independently, `1 + (−1)` is exactly
> zero but its **sign** is `+0` under RNE/RZ/RU and `−0` under RD — the IEEE signed-zero-of-an-exact-sum
> rule. Both are live-confirmed; a constant-fold pass that ignores the active `RoundMode` will mis-fold
> these.

### 5.5 DIVN — the Newton division step

`DIVN.H` / `IVP_DIVNNXF16` is not a full divide; it is one fused Newton–Raphson refinement step of a
reciprocal/quotient estimate held in `vt`, of FMA shape `q' = round_rm(q + q·(1 − d·q))` realised on the
shared datapath (the `s*_divn` stage signals). It carries Overflow/Underflow/Inexact enables but **no
Invalid** (the special-case Invalid is raised by the seed/recip stage — the
[hp lookup recip-seed group](../semantics/group-semantics-i.md), not by the Newton step). The `N` sibling
of the multiply leg inside the loop is MADDN/MULAN (no intermediate round/flag). `[HIGH structure /
MED exact recurrence — the FMA shape + enable subset are OBSERVED; the precise coefficient routing is the
ISS body, INFERRED from the signal names + the FMA algebra]`

### 5.6 Predication (`t` forms)

`d[k] = pred[k] ? <op result>[k] : vt_prev[k]`. The `vbr` (`vbool`) input supplies a per-lane keep/kill
mask (`op_pred_t`/`op_pred_f`); on a killed lane the prior `vt` value passes through unchanged. The
compute on a kept lane is exactly §5.1–5.5 for that op-class. `[HIGH/OBSERVED structure]`

---

## 6. Timing — INSTR_SCHEDULE & cas-ISS stages

From the per-op `<INSTR_SCHEDULE>`/`<USEDEF_LIST>` and the `libcas-core.so` stage leaves:

| event | stage | notes |
|---|---|---|
| USE `vr` (b), `vs` (a), `RoundMode` | 10 | unified vec read port |
| USE `vt` (acc c) | 10 | **only** for accumulate classes (MADD/MADDN/MSUB/MSUBN/DIVN); pure ADD/SUB/MUL/MULSONE do not read `vt` |
| USE `vbr` (predicate) | 10 | `t` forms only |
| USE `CPENABLE` | 3 | the cp1 enable gate, sampled early |
| DEF `vt` (result d) | 13 | 3-cycle FMA latency |
| DEF FSR `{Inv,Ovf,Unf,Inx}Flag` | 14 | sticky shared-OR; gated by FCR enables |
| DEF `VectorPipeImpreciseErr` | 15 | deferred fp exception; **absent on `N` forms** |

The `libcas-core.so` ISS confirms the FMA group has per-format `*_fp_sem_hp_fma_semantic_stage10` (read)
and `*_fp_sem_hp_fma_semantic_stage14` (FSR flag) leaves across formats (F0/S3, F1/S3, F2/S3, F2/S2,
F3/S3, …; 16 stage leaves total). `[HIGH/OBSERVED]`

> **NOTE — the cas-ISS exposes hp_fma stage10/stage14 leaves but folds the stage15 imprecise edge.** At
> the cas-ISS modeling level `fp_sem_hp_fma` has stage10 and stage14 functions; its fp32 peer
> `ivp_sem_spfma` additionally has a `*_semantic_stage15` leaf (the `VectorPipeImpreciseErr` edge). The
> architectural data result @stage13 and the imprecise-error edge @stage15 are carried from the
> `INSTR_SCHEDULE` model; the directly-observed cas anchors are the stage10/stage14 leaves.
> `[stage10/14 HIGH/OBSERVED; result@13 + ImpreciseErr@15 HIGH/CARRIED]`

---

## 7. Worked examples — device round-trip + slot-local words

Oracle: `xtensa-elf-as` → `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=…/XtensaTools/config`).
Each form assembled into a narrow N1 bundle (op0=`0x2f`, the fp op in slot S2, two `nop` companions) and
disassembled back **bit-exact**. The objdump hex column is byte-reversed for display (memory-order
first byte = `0x2f`).

### 7.1 IVP vector base + `t` + `.H` forms (v2 = dst, v0/v1 = inputs)

```
ivp_addnxf16     v2,v0,v1      → 00a54401ae40352f
ivp_subnxf16     v2,v0,v1      → 00a54401ce40352f
ivp_mulnxf16     v2,v0,v1      → 00a54401ca40352f
ivp_mulanxf16    v2,v0,v1      → 00a544014a40352f   (madd: v2 += v0·v1, v2=acc)
ivp_mulsnxf16    v2,v0,v1      → 00a54401cc40352f   (msub: v2 -= v0·v1)
ivp_mulsonenxf16 v2,v0,v1      → 00a544014e40352f   (v2 = 1 − v0·v1)
ivp_divnnxf16    v2,v0,v1      → 00a544014840352f   (Newton div-step)
ivp_mulanxf16    v7,v3,v5      → 00a54e094a43352f   (asymmetric — confirms the field map)
ivp_addnxf16t    v2,v0,v1,vb1  → 00a504008a40352f   ('t': merge under vb1)
add.h     v2,v0,v1            → 00a54401ac40352f    madd.h   v2,v0,v1 → 00a54401e840352f
maddn.h   v2,v0,v1            → 00a544016840352f    mulsone.h v2,v0,v1→ 00a544016c40352f
divn.h    v2,v0,v1            → 00a544012e40352f
```

Note `madd.h` byte `0xe8` ≠ `ivp_mulanxf16` byte `0x4a` — distinct decode points, same op-class via the
`op_scalar`/`op_vector` mux. All round-trips are bit-exact.

### 7.2 Primary-slot (F0/S3, 20-bit selector) slot-local words

Computed by **executing the encode + field `set` thunks in-process** (`Opcode_*_encode` then `Field_*_set`
for `vt`/`vs`/`vr`), then reading back — fully binary-driven:

```
ivp_addnxf16     v2,v0,v1  sel=0x10d7e  →  slot-local word = 0x86bf0801   (for k: v2[k] = round_RNE(v0[k] + v1[k]))
ivp_subnxf16     v2,v0,v1  sel=0x101cc  →  0x80e60801
ivp_mulnxf16     v2,v0,v1  sel=0x1014c  →  0x80a60801
ivp_mulanxf16    v2,v0,v1  sel=0x1012c  →  0x80960801   (for k: v2[k] = round_RNE(v2[k] + v0[k]·v1[k]), fused, v2=acc)
ivp_mulannxf16   v2,v0,v1  sel=0x1010c  →  0x80860801
ivp_mulsnxf16    v2,v0,v1  sel=0x1018c  →  0x80c60801
ivp_mulsnnxf16   v2,v0,v1  sel=0x1016c  →  0x80b60801
ivp_mulsonenxf16 v2,v0,v1  sel=0x101ac  →  0x80d60801
ivp_divnnxf16    v2,v0,v1  sel=0x10d5f  →  0x86af8801
ivp_addnxf16t    v2,v0,v1,vb1  sel=0x1049 (16b @[34:19])  →  0x82488801   (vbr=1 readback confirmed)
```

The operand bits `0x801` decode as `vt=2` (`[14:10]`), `vs=0` (`[9:8]‖[3:1]`), `vr=1` (`[7:4]‖[0]`); the
`get` thunks read these back exactly. The full 128-bit bundle is produced by the FLIX slot-scatter; the
device objdump decodes the F0 bundle to "IVP_ADDNXF16 v2,v0,v1" in slot S3 with `nop`/move companions
(the N1 round-trip in §7.1 is the device-verified end-to-end check). `[HIGH/OBSERVED]`

### 7.3 Worked values (binary16, executed §5 semantics)

```
IVP_MULANXF16 (madd) lane: v2=0x3c00(1.0), v0=0x4000(2.0), v1=0x4200(3.0), RNE
    → 1.0 + 2.0·3.0 = 7.0 = 0x4700   (single round on the exact 6.0 product)   [LIVE]
IVP_SUBNXF16 lane: v0=0x3c00(1), v1=0x3c00(1)
    → 1−1 = +0 = 0x0000 under RNE, but = 0x8000 (−0) under RD                   [LIVE]
IVP_MULSONENXF16 lane: v0=0x3800(0.5), v1=0x4000(2.0)
    → 1 − 0.5·2.0 = 0.0 = 0x0000   (the recip-refinement residual is 0 when v0=1/v1)  [LIVE]
```

---

## 8. Cross-references

* [B17 — fp32 Fused Multiply-Add (spfma)](b17-spfma.md) — the fp32/binary32 peer; the same s0/s1/s2
  FMA tree at `N_2XF32` (16×binary32) lane width, the same selector/operand/timing structure.
* [B16 — Vector Replicate / Extract (vec_rep)](b16-vec-rep.md) — the preceding committed batch boundary.
* [B04 — integer MAC](b04-mac-integer.md) / [B05 — mixed MAC](b05-mac-mixed.md) — the integer
  multiply-accumulate batches from which this page reclaims the fp16 FMA forms (§1 CORRECTION).
* [fp sub-ISA](../core/fp-sub-isa.md) — the FLIX format/slot-scatter and the per-slot field decode.
* [VFPU & IEEE model](../../uarch/vfpu-ieee.md) — the FCR/FSR register model, the precise cp1 gate vs
  the deferred imprecise exception, and the soft-float ↔ hardware-FP reconciliation.
* [Group semantics I](../semantics/group-semantics-i.md) — the recip/rsqrt/lookup seed group that
  supplies the DIVN/recip Newton-loop seed this group's MULSONE/MADDN refine.
* [CAS MAC/FMAC ISS](../../iss/cas-mac-fmac.md) — the `libcas-core.so` timing/decode model for the
  multiply-accumulate and FMA families.
* [MAC multiply validation](../../validation/mac-multiply.md) — the differential validation harness for
  the multiply/FMA value semantics.
* [Template & partition](template-and-partition.md) — the partition classifier that routes mnemonics to
  this batch by `fp_sem_hp_fma` semantic membership.

---

## 9. Coverage / verification ledger

* `[HIGH/OBSERVED]` Batch 18 = `fp_sem_hp_fma`, **27 members** — all `nm`-verified present as distinct
  opcodes in `libisa-core.so` (27/27, none missing): 9 IVP base + 9 IVP `t` + 9 `.H` scalar.
* `[HIGH/OBSERVED]` F0/S3 selector CONST per op-class read byte-for-byte from the encode thunks and
  divided down (`enc>>15`); the two-place CORRECTION (SUB=`0x101cc`, MUL=`0x1014c`) verified against the
  raw `movl` immediates.
* `[HIGH/OBSERVED]` Operand field map (`vt`=`[14:10]`, `vs`=`[9:8]‖[3:1]`, `vr`=`[7:4]‖[0]`, `vbr`=
  `[18:15]`) recovered by executing the `Field_*_{set,get}` thunks in-process and round-tripping registers.
* `[HIGH/OBSERVED]` Value semantics executed LIVE in `libfiss-base.so`: round-half-to-even
  (0.1+0.2 → 0x34cc/0x34cd/0x34cc by mode), directional NaN propagation (ADD 1st, MUL 2nd, madd a/c/b),
  sNaN quieting (0x7c01→0x7e01, 0xfd55→0xff55), single-rounding fused madd (catastrophic-cancellation
  proof 0x0ffe ≠ two-step 0x0000), round-mode-dependent overflow, RD exact-zero sign.
* `[HIGH/OBSERVED]` Soft-float verified: the `add16` body at `0x51c640` has 0 hardware-FP instructions
  and 13 binary16-mask references — integer-only, `RoundMode` a parameter.
* `[HIGH/OBSERVED]` Timing: `libcas-core.so` per-format `*_fp_sem_hp_fma_semantic_stage10`/`stage14`
  leaves; read@10, FSR-flag@14 directly observed; result@13, ImpreciseErr@15 carried from the schedule.
* `[HIGH/OBSERVED]` All 18 representative bundles round-trip bit-exact through the device
  `xtensa-elf-as`/`objdump` (`XTENSA_CORE=ncore2gp`).
* `[MED/INFERRED]` DIVN's exact Newton coefficients (the ISS body, not in the schedule); the a-vs-b
  multiplicand labelling (`vs` vs `vr`) is symmetric for MUL/MADD and is cosmetic.
* `[LOW]` The full per-slot field scatter in every one of the 9–13 slots is not enumerated per op (only
  the F0/S3 primary map + the per-format selector-width list); the rest is the FLIX decoder's domain.
