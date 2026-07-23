# The VFPU / IEEE-754 Exception Model

The Vision-Q7 floating-point unit (**VFPU**) is the floating-point arithmetic subset
of the deep vector pipe. This page documents it as silicon a reimplementer must
match cycle-for-cycle and bit-for-bit: the **single, fully-multiplexed fused-multiply-add
(FMA) datapath** that serves both binary16 and binary32 at two lane widths; its internal
**3-stage multiply / align-add / normalize-round tree**; the **FCR/FSR** control/status
register split with its `movscfv`/`movvscf` 11-field pack; the **two-tier exception model**
(the *precise* `Coprocessor1Exception` cp1 gate vs the *deferred imprecise* IEEE arithmetic
exceptions + `VectorPipeImpreciseErr`); and the reconciliation between the **integer-only
soft-float ISS** and the **hardware FP pipe** as a model/implementation pair of one value
function.

Every claim below is anchored to a recovered symbol, byte sequence, state-table struct,
device round-trip, or a **live** drive of the `libfiss-base.so` soft-float oracle. Tags are
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`; untagged claims default to `[HIGH/OBSERVED]`.

> **Scope anchor.** All tokens here (`NXF16`, `N_2XF32`, `S2_Mul`/`S3_ALU`, `stage 10..15`,
> `RoundMode`, `FS0..FS7`, `512`-bit) are **lane-width / flag-register / pipeline-stage /
> FLIX-slot axes of the one ncore2gp "Cairo" config** (`XTENSA_CORE=ncore2gp`) — never one of
> the five silicon generations (sunda/cayman/mariana/maverick/tonga), which are a firmware-image
> axis invisible in these host-side tables.

Binaries (all under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`, accessed by
absolute path; `extracted/` is gitignored):

* `libcas-core.so` — the cycle-accurate simulator. **Not stripped**, full `.symtab`
  (`177,936` symbols, `readelf -SW`), **no DWARF**. Modelled via `nm`/`objdump` on named
  `*_issue` bodies. Each `_issue` body stamps every operand's pipeline stage as a
  `mov $0xN,%esi` immediate — that is the **OBSERVED stage table**.
* `libfiss-base.so` — the host x86-64 ISS value oracle. `866` `module__xdref_*` value
  functions, driven **live** via `ctypes`.
* `libisa-core.so` — the iclass / opcode / state roster. Field membership read from the
  `Iclass_*_stateArgs` arrays; the Xtensa `states[]` table holds each state's width and
  `SHARED_OR` attribute.

> **GOTCHA — section deltas.** For these ncore2gp config DLLs, `.text`/`.rodata` have
> `VMA == file offset`, but `.data`/`.data.rel.ro` carry `VMA − fileoffset = 0x200000`
> (`readelf -SW libcas-core.so`: `.data.rel.ro` VMA `0x2070900` / fileoff `0x1e70900`;
> `libisa-core.so`: `.data` VMA `0x764040` / fileoff `0x564040`). Confirm per-section before
> reading any `.data`-resident struct. This is **not** libtpu's `0x400000`.

---

## 0. Headline findings

All six findings are `[HIGH/OBSERVED]`.

| # | Finding | Strongest anchor |
|---|---------|------------------|
| 1 | The VFPU is **one** fully-multiplexed FMA pipe for binary16 (`NXF16`, 32 lanes) + binary32 (`N_2XF32`, 16 lanes); `d = round_RM(±(a·b) ± c)` with a **single rounding** | `MADD_S/MADD_H/IVP_MULAN_2XF32T/IVP_MULANXF16T` share the s0/s1/s2 skeleton; co-issue round-trips |
| 2 | "2×FMA" = the datapath **placed in two FLIX slots** — `S3_ALU` primary + `S2_Mul` borrow in F2/F7/N1 (on N1, which has no `S3_ALU` slot, the only FP-FMA placement *is* `S2_Mul`) — so one wide bundle co-issues two FMAs | `nm 'Opcode_madd_s_Slot'` = `s2_mul` ×3 (f2/f7/n1) + `s3_alu` ×4 (f0/f1/f2/f7); `{ … ivp_muln_2xf32 v3,v4,v5 ; madd.h v2,v0,v1 }` assembles legal |
| 3 | FCR = `RoundMode(2b)` + 5 enables; FSR = 5 flags each `XTENSA_STATE_IS_SHARED_OR` (lanes sticky-OR to one bit) | `Iclass_rur_fcr_stateArgs` / `Iclass_rur_fsr_stateArgs`; state structs `flags=0x02` on Flags, `0x00` on Enables |
| 4 | The 11-field FCR+FSR pack is `movscfv`/`movvscf`, order `{IxF,UfF,OfF,DzF,IvF, RM, IxE,UfE,OfE,DzE,IvE}` + CPENABLE gate | `Iclass_IVP_MOVVSCF_stateArgs` resolved verbatim |
| 5 | Two-tier exceptions: **precise** `Coprocessor1Exception` (cp1 @stage 3, squash) vs **imprecise** FSR flags @stage 14 + `VectorPipeImpreciseErr` @stage 15 | `Coprocessor1Exception_exc@0x1780ff0`; `nx_VectorPipeImpreciseErr_interface`; `_issue` stage immediates `0xa/0xd/0xe` |
| 6 | Soft-float ISS == hardware FP pipe (one value function). The fp16 add body @`0x51c640` carries **zero** hardware-FP insns | `objdump` count `0` over `addss/mulss/cvtss/movss/fadd/addsd/subss/divss`; 18× binary16 masks `0x7c00/0x3ff/0x1f` |

---

## 1. The VFPU pipeline — structure

The Vision-Q7 runs two coupled pipelines sharing fetch/issue: a 7-stage scalar/address pipe
(the host "ARM-of-the-DSP") and a deep vector pipe spanning roughly stages 2..15. The VFPU is
the FP-arithmetic subset of that deep vector pipe. Its operand read port is at **stage 10** and
its results land at stages 10..13 by latency class.

### 1.1 FP groups and result-latency class

`[HIGH/OBSERVED]` from the `libcas-core.so` `_issue` stage immediates (cross-confirmed on
`uarch/pipeline-timing.md`):

| FP group | members | result stage | dep-lat | anchor (`*_issue`) |
|----------|--------:|-------------:|--------:|--------------------|
| HP fused multiply-add | 27 | **@13** | 3 | `IVP_MULANXF16T @0x14b6e90`, `MADD_H @0x14b3f10` |
| SP fused multiply-add | 27 | **@13** | 3 | `IVP_MULAN_2XF32T @0x14b6020`, `MADD_S @0x14b3340` |
| HP convert | 21 | **@13** | 3 | `IVP_CVTF16N_2XF32T @0x14b7360` |
| SP convert | 31 | **@13** | 3 | (`ivpep_sem_sp_cvt`) |
| SP recip/rsqrt seed | 5 | **@12** | 2 | `IVP_RECIP0N_2XF32T @0x14b5d80` |
| HP/SP table lookup | 30/29 | **@12** | 2 | (`ivpep_sem_*_lookup`) |
| SIMD divide-step | 14 | @12 | 2* | (`ivp_sem_divide`) |

The fp ALU/min/max/compare slice rides the `vec_alu @11` 1-cycle class.

> **NOTE — `TRUNC` is fast (@12), FMA is slow (@13).** `IVP_TRUNCN_2XF32T @0x14b5a90` posts
> its result at stage 12 (2-cycle), one stage *earlier* than the FMA tree. Keep these split —
> see `uarch/pipeline-timing.md` (#650).

### 1.2 The FMA internal 3-stage tree (s0 / s1 / s2)

The "3-cycle result @13" decomposes into three internal datapath stages. The schedule's
`<STATEMENTS>` body is empty, so the gate-level microarch is recovered from the flop **names**
(OBSERVED) and the proven value behaviour (`validation/fp-soft-float.md`); the gate meaning is
**INFERRED** from the names.

```
STAGE s0  MULTIPLY + SPECIAL-CLASSIFY            (~pipe stage 10 -> 11)
  m00_mux / m01_mux / m10_mux / m11_mux          a 2x2 radix-split partial-product grid of
                                                 the 11-bit (HP) / 24-bit (SP) significands
  s0_axb_sig_pp_h / s0_axb_sig_pp_l              the wide product, kept high/low halves
  s0_exp_sum    = exp(a) + exp(b) - bias         the product exponent
  s0_axb_is_neg = sign(a) ^ sign(b)              product sign; negate_axb_M for MSUB/MULS
  s0_c_is_neg / s0_negate_c                      addend (c = vt) sign + conditional negate
  s0_out_is_inf/_nan/_snan, s0_ex_zero/_neg      the inf/zero/NaN special-case algebra
  s0_lzc_limit, s0_roundm                        normalize LZC limit + latched RoundMode

STAGE s1  ALIGN + FUSED ADD                      (~pipe stage 11 -> 12)
  s1_c_shift                                     align the addend significand to the product exp
  s1_sum_sig_a = aligned(c) +/- EXACT(a·b)       the SINGLE fused add; the product enters
                                                 EXACT (full width, NOT pre-rounded)
  s1_out_is_{inf,nan,neg,snan,zero}, s1_exp_limit   propagated special flags

STAGE s2  NORMALIZE + ROUND + FLAG               (~pipe stage 12 -> 13)
  LZC normalize; round under s0_roundm           {RNE/RZ/+inf/-inf}
  s2_exp / s2_ex_special                         final exponent + special-result select
  -> ROUNDED result d  DEF @stage 13             the architectural value
     FSR flags accumulate @stage 14
     VectorPipeImpreciseErr DEF @stage 15
```

> **QUIRK — true single-rounding FMA.** The product is carried into the fused add **exact**
> (`s1_sum_sig_a` adds the full-width product, not `round(a·b)`), so `madd` rounds **once**.
> A two-step `c + round(a·b)` would diverge on inexact products; the soft-float oracle proves
> it does not (`validation/fp-soft-float.md`: madd `1152/1152` bit-exact across round modes).
> A reimplementer **must not** pre-round the product. `[HIGH/CARRIED]`

### 1.3 The op-class mux — one shared tree, many mnemonics

A decode-time op-class mux routes every FMA mnemonic through the *same* hardware. The prefanout
one-hots:

| prefanout | routes |
|-----------|--------|
| `add_only` / `mul_only` / `mul` / `madd` | ADD vs MUL vs MADD |
| `op_negadd` | SUB/MSUB negate-addend select |
| `op_maddn` (+`s0/s1/s2_maddn`) | the **'N' no-imprecise** path threaded through all 3 stages |
| `op_mulsone` | MULSONE: `c` forced `= 1.0` ⇒ `d = 1 − a·b` (recip residual) |
| `op_divn` (+`s0/s1/s2_divn`) | the DIVN Newton division-step path |
| `op_pred_t` / `op_pred_f` | the `'T'` predicate true/false lane-select |
| `op_scalar` / `op_vector` | `.H`/`.S` scalar spelling vs IVP vector spelling |
| `op_mux_sel` | top-level op-class select (the opcode-selector CONST) |

The HP and SP FMAs are the **same** structure at two lane widths: HP uses an 11-bit-significand
`2×2` PP grid; SP uses a 24-bit-significand multiply (the `mulpp` host-callback in the ISS).
The s0/s1/s2 align/normalize/round skeleton is shared. `[HIGH/OBSERVED roster; INFERRED tree]`

---

## 2. The FCR / FSR register model — exact field membership

Read directly from the device iclass `stateArgs` arrays in `libisa-core.so`. Each
`stateArgs` entry is a 16-byte `{ state_name_ptr(8), direction(8) }` pair, where direction
`0x69 = 'i'` (state read / input), `0x6d = 'm'` and `0x6f = 'o'` are write/output forms.

### 2.1 FCR (Floating-point Control Register)

`Iclass_rur_fcr_stateArgs @0x858300` (`.data`, fileoff `0x658300`) — the seven `'i'` state
pointers resolve (in `.rodata`, `VMA == fileoff`) to:

```
RoundMode  InvalidEnable  DivZeroEnable  OverflowEnable  UnderflowEnable  InexactEnable  CPENABLE
(0x3bb7e3) (0x3bb7ad)     (0x3bb779)     (0x3bb7c7)      (0x3bb7ed)       (0x3bb793)     (0x3c9410)
```

⇒ **FCR = the writable policy:** the round mode + which exceptions are enabled. CPENABLE is the
cp1 gate, present on every FP iclass but not part of the arithmetic word.

> **CORRECTION — DivZero is a first-class fifth field.** Earlier passes cited "4 exceptions" for
> the FMA group. The device state table makes `DivZeroEnable`/`DivZeroFlag` first-class
> (resolved above and in §2.2), not folded into Invalid. `[HIGH/OBSERVED]`

Reset `RoundMode = 2'h0 = RNE` (round-to-nearest-even) — live-confirmed in §3.

### 2.2 FSR (Floating-point Status Register)

`Iclass_rur_fsr_stateArgs @0x858200` — the six `'i'` pointers resolve to:

```
InvalidFlag  DivZeroFlag  OverflowFlag  UnderflowFlag  InexactFlag  CPENABLE
(0x3bb7bb)   (0x3bb787)   (0x3bb7d6)    (0x3bb7fd)     (0x3bb7a1)   (0x3c9410)
```

⇒ **FSR = the sticky accumulated status:** each arith op OR-s its lane results into these 5
flags @stage 14; they persist until software clears them via `wur.fsr`. Reading the FSR
(`rur.fsr`) crosses the vector→scalar boundary and lands a scalar word in an AR.

### 2.3 The state structs — width and SHARED_OR

Each FP state is a 24-byte `states[]` struct `{ name_ptr, ?, width(u32), flags(u32), … }`.
Located via the relocation whose addend equals the name string offset, then dumped from
`.data.rel.ro` (delta `0x200000`):

| state | struct VMA | width | flags | meaning |
|-------|-----------:|------:|------:|---------|
| `RoundMode` | `0x6cd700` | **2** | `0x00` | 2-bit IEEE round mode |
| `InvalidFlag` | `0x6cd720` | 1 | **`0x02`** | `XTENSA_STATE_IS_SHARED_OR` |
| `DivZeroFlag` | `0x6cd740` | 1 | **`0x02`** | `XTENSA_STATE_IS_SHARED_OR` |
| `InexactFlag` | `0x6cd7a0` | 1 | **`0x02`** | `XTENSA_STATE_IS_SHARED_OR` |
| `InvalidEnable` | `0x6cd7c0` | 1 | `0x00` | plain (not shared) |

`[HIGH/OBSERVED — struct bytes read.]` So **`flags=0x02` ⇒ SHARED_OR**: all 5 Flags
carry it (the `N` lanes sticky-OR into one bit, IEEE sticky semantics), all 5 Enables are plain,
and `RoundMode` is a 2-bit field. The five `IVP_FS0..FS7` (each 64-bit) are a **separate**
8×64-bit predicate-accumulator file (the `movvfs`/`movfsv` + `fs[0-7]ltu` file), *not* the
FCR/FSR arithmetic control — do not confuse them.

### 2.4 The per-op-class enable subset

`[HIGH/OBSERVED]` — derived from the `_issue` `mov $0xe,%esi` (stage-14 state DEF) **counts**
in each body (each FSR-flag DEF is one `0xe` immediate):

| op-class | RoundMode | Invalid | DivZero | Overflow | Underflow | Inexact | ImpreciseErr | `@14` count |
|----------|:---------:|:-------:|:-------:|:--------:|:---------:|:-------:|:------------:|:-----------:|
| `ADD`/`SUB` (`ADDN_2XF32T`) | yes | yes | — | yes | — | yes | yes | **3** |
| `MUL`/`MULSONE` | yes | yes | — | yes | yes | yes | yes | 4 |
| `MADD`/`MSUB`/`MULA`/`MULS` | yes | yes | — | yes | yes | yes | yes | **4** |
| `MADDN`/`MSUBN`/`MULAN`/`MULSN` (`'N'` scalar) | — | — | — | — | — | — | — | **0** |
| `DIVN` (`IVP_DIVNN_2XF32T`) | yes | — | (div0) | yes | yes | yes | yes | **3** |
| convert (`hp/sp_cvt`) | yes | yes | — | yes | yes | yes | yes | — |
| recip/rsqrt seed | — | (seed posts DivZero @x=0, Invalid @x<0) | — | — | — | — | — | 2 |

Verified counts (`objdump … | rg -oc 'mov +\$0x0e,%esi'`):
`MADD_S` = 4, `MADD_H` = 4, `IVP_MULAN_2XF32T` = 4, `IVP_MULANXF16T` = 4;
`ADDN_2XF32T` = **3** (ADD omits Underflow — a sum cannot create a new denormal that a product
can); `MADDN_S` = `MADDN_H` = **0** (the 'N' fast path posts no flags); `IVP_DIVNN_2XF32T` = **3**
(DIVN omits Invalid — the Newton *step* does not signal Invalid; the seed/recip stage does).

> **QUIRK — the scalar `'N'` forms suppress flags; the SP `divnn` does not.** `MADDN_S/_H` post
> **zero** `@14` state DEFs — no FSR flags and no `VectorPipeImpreciseErr`, the un-flagged path
> inside iterative refinement. But `IVP_DIVNN_2XF32T` (the SP Newton **divide-step**) posts 3
> flags (Overflow/Underflow/Inexact, no Invalid). They are different op-classes: `op_maddn`
> kills the whole flag tree; `op_divn` keeps the non-Invalid flags. `[HIGH/OBSERVED]`

---

## 3. The FCR/FSR bit map — the packed control word + live rounding

Two device mechanisms expose the FCR/FSR fields as one word: `rur`/`wur fcr/fsr` (control or
status ↔ a scalar AR) and `movscfv`/`movvscf` (the full 11-field set ↔ one int32 vector lane).

### 3.1 The 11-field order (device iclass, OBSERVED)

`Iclass_IVP_MOVVSCF_stateArgs @0x84b400` (state→vector READ; all eleven are `'i'`) resolves
verbatim to:

| idx | field | width | bit |
|----:|-------|------:|----:|
| 0 | `InexactFlag` | 1 | 14 |
| 1 | `UnderflowFlag` | 1 | 13 |
| 2 | `OverflowFlag` | 1 | 12 |
| 3 | `DivZeroFlag` | 1 | 11 |
| 4 | `InvalidFlag` | 1 | 10 |
| 5 | `RoundMode` | 2 | 9:8 |
| 6 | `InexactEnable` | 1 | 6 |
| 7 | `UnderflowEnable` | 1 | 5 |
| 8 | `OverflowEnable` | 1 | 4 |
| 9 | `DivZeroEnable` | 1 | 3 |
| 10 | `InvalidEnable` | 1 | 2 |
| (gate) | `CPENABLE` | — | — (not packed) |

ASCII bit map (bit 15 … bit 0):

```
 15  14   13   12   11   10    9   8    7    6    5    4    3    2    1   0
  .  IxF  UfF  OfF  DzF  IvF   RM  RM    .   IxE  UfE  OfE  DzE  IvE   .   .
     (F = Flag, E = Enable; Ix Uf Of Dz Iv = Inexact Underflow Overflow DivZero Invalid)
```

The `movscfv` read model (`out = ((s >> 2) & 0x1f) | ((s >> 3) & 0xfe0)`) packs the 5 enables
dense at `out[4:0]` and `RoundMode ++ 5 flags` dense at `out[11:5]`.

> **CORRECTION — host/device naming inversion.** The device TIE `IVP_MOVSCFV` is
> **vector-IN → 11-states-OUT** (a write of the control/status *from* a vector), while
> `IVP_MOVVSCF` is **11-states-IN → vector-OUT** (a read *into* a vector). The host-fiss leaf
> names label them the *other* way (the host's `movscfv` leaf is the read body). The **field
> set, order, and bit positions are identical**; only the labels disagree. Trust the TIE arg
> direction (`'i'`/`'o'`), not the mnemonic. Confirmed: the device encodes both in slot
> `S2_Mul` (`Opcode_ivp_movscfv_Slot_f0_s2_mul_encode`); a round-trip shows
> `{ … ivp_movscfv v2; nop }` lands in the Mul slot. `[HIGH/OBSERVED]`

The host pack leaf is `module__xdref_movscfv_1_1_1_1_1_2_1_1_1_1_1_32` — five 1-bit flags, one
2-bit RoundMode, five 1-bit enables, one 32-bit GPR — exactly this layout.

### 3.2 Rounding — the two-level model, live-verified

`RoundMode` is a **2-bit** state (modes 0..3). The architectural round-mode encoding (live oracle
+ `isa/core/fp-sub-isa.md`):

| value | mode | direction |
|------:|------|-----------|
| `00`=0 | **RNE** | round-to-nearest, ties-to-even |
| `01`=1 | **RZ / RTZ** | toward zero (truncate) |
| `10`=2 | **+inf / RU** | toward +∞ |
| `11`=3 | **−inf / RD** | toward −∞ |
| (4) | **away / RNA** | round-to-nearest, ties-away — a 3rd-state extension beyond the 2-bit field |

Driven **live** through the soft-float add leaf. ABI of
`module__xdref_add_1_1_1_16f_16f_16f_2 @0x51c640` (recovered by disassembly): scalar inputs in
`(esi = a16, edx = b16, ecx = RoundMode)`, four `*u32` output ports (`r8, r9, stack[+0x38],
stack[+0x40]`); the rounded result lands in the **4th** output port. At the exact half-way tie
`1.0 + ½ulp`:

```
rnd=0 RNE  -> 0x3c00 = 1.0            (ties to even: 0x3c00 is even)
rnd=1 RTZ  -> 0x3c00 = 1.0            (truncate)
rnd=2 +inf -> 0x3c01 = 1.0009765625  (round up)
rnd=3 -inf -> 0x3c00 = 1.0           (round down)
rnd=4 away -> 0x3c00 = 1.0           (no increment at the exact half — see QUIRK)
```

> **QUIRK — `away` does NOT increment at the narrow exact half (RNA reads only the R bit).**
> At the exact half-way tie the ties-away mode returns `1.0`, **not** `1.0009765625`. RNA reads
> the **R** (round) bit only; at a narrow exact-half the sticky bits below R are zero, so there
> is nothing to push the increment. Beyond that, the **raw, un-parameterized** fiss leaf falls
> back to a **round-zero (RZ) two-level default** when the device `RoundMode` parameter is not
> threaded through — observed: at `1.0 + ¾ulp` (sticky set) mode 4 still returned `1.0`,
> tracking RTZ rather than rounding up. This is the documented **two-level rounding** wall: the
> un-parameterized leaf default is RZ; the device round mode must be supplied for the
> nearest/directed modes to engage. `[HIGH/OBSERVED — live oracle]`

> **NOTE — no FTZ/DAZ on fp16 convert.** The HP convert path does not flush denormals; the fp16
> convert scale uses the **4-bit** `i_imm4` field (`fld_ivpep_sem_hp_cvt_i_imm4`), versus the
> fp32 path's **5-bit** `i_imm5` (`fld_ivpep_sem_sp_cvt_i_imm5`). See `isa/ref/b13-sp-cvt.md`
> and `isa/ref/b20-hp-cvt.md`. `[HIGH/OBSERVED]`

### 3.3 Live FSR flag generation

The same add leaf's status ports were probed (the flag ports are `[Inexact, Overflow,
Invalid]`):

```
+inf + (-inf)   -> Invalid set,  result 0x7e00 (qNaN)          ; port[2]=1
maxnorm + maxnorm -> Overflow + Inexact set, result 0x7c00 (+inf) ; port[0]=1, port[1]=1
1.0 + 2^-24     -> Inexact set,  result 0x3c00                 ; port[0]=1
1.0 + 1.0       -> no flags,     result 0x4000 (2.0)
```

⇒ Invalid on `inf − inf` (canonical qNaN `0x7e00`), Overflow+Inexact on overflow, Inexact on a
rounded-away tiny addend. `[HIGH/OBSERVED — live oracle]`

---

## 4. The exception-timing model — precise cp1 gate vs deferred imprecise

Two exception tiers, separated by roughly **eleven** pipeline stages.

### TIER 1 — PRECISE (the cp1 enable gate): `Coprocessor1Exception`

* `CPENABLE` (the cp1 bit) is sampled **@stage 3** — early, before the deep datapath. Every
  IVP/VFPU op carries `STATE_IN = CPENABLE` + `EXC = Coprocessor1Exception`.
* If the cp1 bit is **clear**, the op raises `Coprocessor1Exception` and is **squashed** before
  any datapath or FSR effect.
* This is **precise**: the gate is checked at issue (stage 3, before the deep execute), so it
  pins to the exact instruction. It is the only precise FP-pipe exception and is a **capability**
  gate (coprocessor not enabled), not an arithmetic condition.

Anchors (OBSERVED, `libcas-core.so`): `Coprocessor1Exception_exc @0x1780ff0` — its body wires
`EXCCAUSE`/`EXCINF`/`VECBASE`/`MS_DISPST`/`WB_*` and calls `nx_ExceptionPC_interface@plt`, the
real exception-vector path; `my_CPENABLE_use @0x178e5f0`, `my_CPENABLE_stall @0x178e420`,
`my_CPENABLE_use_commit @0x178e710`. The `CPENABLE_use` body compares the field against
`$0x6`/`$0x7` (the 7-value cp index).

### TIER 2 — IMPRECISE (the IEEE arithmetic exceptions): `VectorPipeImpreciseErr` + FSR

* Operands (`vr = b`, `vs = a`, `vt = acc/c`, `vbr = pred`, `RoundMode`) are read **@stage 10**
  (the unified vec read port — `opnd_sem_vec_addr` calls each stamped `mov $0xa,%esi`).
  Accumulate classes (MADD/MSUB/…/DIVN) read `vt` @10; pure ADD/SUB/MUL/MULSONE do not.
* The **rounded result** is DEF **@stage 13** (the 3-cycle s0/s1/s2 latency — `mov $0xd,%esi`).
* The 5 FSR flags `{Invalid,DivZero,Overflow,Underflow,Inexact}Flag` are DEF **@stage 14**
  (`mov $0xe,%esi`), **one** stage after the value, gated by their FCR `*Enable` bits; the lane
  results `SHARED_OR` into the single sticky FSR bit.
* The deferred error edge `VectorPipeImpreciseErr` is DEF **@stage 15** — one stage after the
  flags, two after the value. Anchor: `nx_VectorPipeImpreciseErr_interface@plt` (the deferred
  report) and, in the ISS core (`libsimxtcore.so`),
  `NX_EXT_BUFFERS::check_mb_imprecise_exception(RefBufEntry*)` — a buffered, late-checked path.
* This is **imprecise**: an fp exception in the ~15-deep vector pipe cannot be pinned to the
  faulting instruction (by stage 14/15 several later ops have issued). Software observes it via
  the sticky FSR flag (`rur.fsr`, polled after the fact) or the `VectorPipeImpreciseErr` report.
* The `'N'` forms (`MADDN/MSUBN/MULAN/MULSN`) post **neither** the FSR flags **nor**
  `VectorPipeImpreciseErr` (their `_issue` bodies have **zero** `mov $0xe,%esi`).

### Timeline (one FMA issue)

```
stage  3   CPENABLE sampled  -> [cp1 clear? -> PRECISE Coprocessor1Exception, SQUASH]
stage 10   read vr / vs / vt / vbr / RoundMode          (mov $0xa,%esi   x3..4)
stage 11   s0  multiply + classify
stage 12   s1  align + fused add
stage 13   s2  normalize + round  -> RESULT vt DEF      (mov $0xd,%esi)   <-- architectural value
stage 14   FSR {Inv,Dz,Ovf,Unf,Inx}Flag DEF (sticky OR), *Enable USE   (mov $0xe,%esi  x0/3/4)
stage 15   VectorPipeImpreciseErr DEF                                   <-- deferred imprecise report
```

> **CONSEQUENCE for software.** A producer posts the **value** at @13 but the **flag** at @14, so
> a dependent op bypasses the value at +1..+3 cycles while software reading the FSR flag sees it
> only after the @14 accumulation — the flag is **always at least one cycle behind the data**.
> This is the architectural reason the FP exception model is **sticky-flag-based (poll the FSR)**
> rather than precise-trap-based. `[HIGH/OBSERVED stages; INFERRED characterization]`

---

## 5. The FP-pipeline block diagram + device round-trip

The FP-FMA op occupies `S3_ALU` (canonical) and **also** `S2_Mul` — in the wide formats **F2/F7**
and the narrow **N1**. (On N1, which carries *no* `S3_ALU` slot, the only FP-FMA placement *is*
`S2_Mul`.) That gives two FMA lanes on a wide bundle, the "2×FMA". One wide bundle can co-issue an
SP-FMA and an HP-FMA (or two HP). The integer quad-MAC is pinned to `S2_Mul`, memory to `S0/S1`.

> **CORRECTION — FP-FMA is NOT `S3_ALU`-exclusive.** `madd_s`/`madd_h` *also* ride `S2_Mul`:
> `nm libisa-core.so | rg 'Opcode_madd_s_Slot'` lists `s2_mul` ×3 (**f2, f7, n1**) alongside
> `s3_alu` ×4 (f0, f1, f2, f7) — `msub_s` identically. The binding constraint is **slot-count**
> (`1×S2 + 1×S3` available), not slot identity; an FMA placed on `S2_Mul` **excludes** a same-bundle
> integer quad-MAC (they contend for the one `S2_Mul` lane), while an FMA on `S3_ALU` co-issues with
> a MAC on `S2`. Confirmed by assembling `{ … ivp_muln_2xf32 v3,v4,v5 ; madd.h v2,v0,v1 }` as a legal
> bundle (two FMAs in one). See [co-issue-matrix §2.3/§3.2](co-issue-matrix.md) and the unified
> ceiling at [microarch-synthesis §2.2](microarch-synthesis.md). `[HIGH/OBSERVED]`

```
FLIX BUNDLE (wide formats F0..F11 / narrow N0..N2)
  |  S0_LdSt   S1_Ld    S2_Mul          S3_ALU            (S4_ALU on F3/F11)
  |  [mem]     [mem]    [intMAC | FP-FMA] [FP-FMA primary | vec-ALU]
  v                         \              /
        +-------------( op-class MUX: op_mux_sel + prefanouts )-----------+
        |  add_only/mul/madd | op_negadd | op_maddn | op_mulsone | op_divn |
        |  op_pred_t/f | op_scalar/op_vector                              |
        +------------------------------+---------------------------------+
                                       |
      a=vs[k]  b=vr[k]  c=vt[k](acc)   |   RoundMode (FCR)        @stage 10
                                       v
 stage 11 +========= s0: SIGNIFICAND MULTIPLY + CLASSIFY =================+
          |  m00/m01/m10/m11 PP grid -> wide product s0_axb_sig_pp_h/_l   |
          |  s0_exp_sum = ea+eb-bias ; s0_axb_is_neg = sa^sb ; negate_*   |
          |  special: s0_out_is_inf/_nan/_snan ; s0_ex_zero/_neg ; lzc_lim|
          +=================================+=============================+
                                            v
 stage 12 +========= s1: ALIGN + FUSED ADD (single, EXACT product) ======+
          |  s1_c_shift (align c to product exp)                          |
          |  s1_sum_sig_a = aligned(c) +/- EXACT(a·b)   <-- ONE fused add |
          +=================================+=============================+
                                            v
 stage 13 +========= s2: NORMALIZE + ROUND (single rounding) ============+
          |  LZC normalize ; round under s0_roundm {RNE/RZ/+inf/-inf}     |
          |  ===> RESULT d = round_RM(±(a·b)±c)  DEF vt @13               |
          |       (pred 'T': d[k] = pred[k] ? result : vt_prev[k])        |
          +=================================+=============================+
                                            |  (NOT on the 'N' forms)
                                            v
 stage 14   FSR FLAG ACCUMULATE: {Inv,Dz,Ovf,Unf,Inx}Flag |= lane-OR
                                  gated by FCR {...}Enable   (SHARED_OR sticky)
                                            |
                                            v
 stage 15   VectorPipeImpreciseErr  DEF     (deferred imprecise fp exception report)

 side gate (early):  stage 3  CPENABLE(cp1)  --clear-->  Coprocessor1Exception (squash)
```

### 5.1 Device round-trip

`XTENSA_SYSTEM=…/XtensaTools/config`, `XTENSA_CORE=ncore2gp`, GNU binutils 2.34.20200201 /
Xtensa Tools 14.09. All assembled + disassembled **bit-exact** (`{ nop; nop; <op> }`
slot framing shows the FP-FMA in S3):

| mnemonic | bytes | note |
|----------|-------|------|
| `add.h  v2,v0,v1` | `00a54401ac40352f` | HP add, slot S3, N1 op0=`0x2f` |
| `madd.h v2,v0,v1` | `00a54401e840352f` | HP madd (`0xe8`) |
| `mul.s  v2,v0,v1` | `00a54401aa40352f` | SP mul (B17 peer) |
| `madd.s v2,v0,v1` | `00a544018e40352f` | SP madd, `vt=acc` |
| `ivp_addn_2xf32  v2,v0,v1` | `00a54400ea40352f` | SP add |
| `ivp_mulan_2xf32 v2,v0,v1` | `00a544010840352f` | SP madd |
| `ivp_divnn_2xf32 v2,v0,v1` | `00a54400ec40352f` | SP Newton div-step |
| `rur.fcr a3` | `32514c007043452f` | FLIX vision-pipe op, slot S3 |
| `rur.fsr a4` | `32514c089043452f` | FLIX vision-pipe op (FSR → AR) |
| `wur.fcr a3` | `f3e830` | 3-byte **narrow** scalar op |
| `wur.fsr a4` | `f3e940` | 3-byte narrow scalar |
| `{ … ivp_muln_2xf32 v3,v4,v5 ; madd.h v2,v0,v1 }` | `001264d8…0465bf` | **two FMAs co-issued** |

> **NOTE — the FCR/FSR access asymmetry.** The **reads** (`rur.*`) are FLIX vision-pipe ops
> (the result crosses vector→scalar @stage 12, lands in an AR), while the **writes** (`wur.*`)
> are narrow scalar ops written from a GPR. This matches the CSR living in the vector "gr" file
> but being written from the scalar core. The UR ids are `0xe8` (FCR) / `0xe9` (FSR) — visible in
> the narrow `f3e8 30` / `f3e9 40` bytes. `[HIGH/OBSERVED bytes; INFERRED rationale]`

The "gr" VFPU control/status file carries zero data operands; it is touched only via `RUR`/`WUR`
(the only such besides `THREADPTR`). See `isa/core/fp-sub-isa.md` for the FCR/FSR-as-UR view.

---

## 6. Soft-float (ISS) vs hardware-FP (silicon) — the reconciliation

The two coexisting "FP" descriptions are a **model/implementation pair of one value function**,
not a conflict to resolve.

**(A) The silicon VFPU = a real hardware FP pipe.** The s0/s1/s2 multiply/align/normalize tree
(§1.2), the 3-cycle result @13, the FSR-flag @14 / `VectorPipeImpreciseErr` @15 — gate-level FP
datapath stages, recovered from the `INSTR_SCHEDULE` flop names. The device decodes
`add.h`/`madd.h`/`mul.s` as real FP-FMA ops (§5.1).

**(B) The ISS VFPU = integer-only soft-float.** The `libfiss-base.so` host model computes the FP
value math with **no host FP**. Confirmed: the fp16 add body
`module__xdref_add_1_1_1_16f_16f_16f_2 @0x51c640` (range `0x51c640..0x51ce70`) contains **zero**
`addss/mulss/cvtss/movss/fadd/addsd/subss/divss` insns and **18** references to the binary16
field masks `0x7c00`/`0x3ff`/`0x1f`. The fp32 add `@0x871790` is likewise zero hardware-FP. The
leaf cracks the IEEE fields (`shr $0xa & 0x1f` = exponent, `and $0x3ff` = mantissa,
`cmp $0x1f` = max-exp special) and does the arithmetic in integer ALU ops, taking `RoundMode` as
a parameter (`ecx`).

**Why both, and why no conflict:**

* The ISS must reproduce the silicon's exact rounded bit pattern under every round mode and the
  full NaN/Inf/denormal algebra. It **cannot** trust the host x86's IEEE rounding (the host rounds
  binary64; the device rounds binary16/32 with device tie-break + device NaN-propagation, e.g. the
  qNaN `0x7e00` observed in §3.3). So the ISS reimplements rounding in integer code — the only way
  to be bit-accurate. "Integer-only soft-float" is therefore a statement about the **model's
  implementation**, not about the silicon.
* `validation/fp-soft-float.md` proves the model == the silicon's value function: a 4-oracle
  bit-exact differential (IEEE exact-rational soft-float, the FLIX decode identity, the numpy sim,
  and the live `libfiss` `xdref`) over thousands of fp16/fp32 pairs × the round modes,
  `>150,000` lane comparisons, **0 mismatch**; plus the seed/refine family
  (`validation/transcendental-seed.md`). The hardware FP pipe and the integer soft-float model
  emit the **same** bits.
* The FCR/FSR semantics map across cleanly: the device `RoundMode` (2-bit state) is the ISS's
  `ecx` round parameter; the device `SHARED_OR` FSR flags are the ISS's status-port
  OR-accumulation (the `[Inexact,Overflow,Invalid]` ports in §3.3); the `CPENABLE` cp1 gate is
  **outside** the value function (a capability check the ISS harness models structurally).

**Conclusion.** One IEEE-754 value function, with a hardware implementation (the s0/s1/s2 VFPU
tree) and a bit-identical integer reference model (the `libfiss` `xdref` leaves) — cross-validated
to 0 mismatch. `[HIGH/OBSERVED: the zero-hardware-FP disasm + the live oracle; the
bit-exactness carried from the VAL fp families.]`

---

## 7. Cross-references and divergence notes

| topic | page | relationship |
|-------|------|--------------|
| FCR/FSR as URs; round-mode encoding | [`isa/core/fp-sub-isa.md`](../isa/core/fp-sub-isa.md) | FCR/FSR are **URs** (not SRs); 5 round modes `RNE=0/RTZ=1/+inf=2/−inf=3/away=4`; UR ids `0xe8`/`0xe9` |
| the SP FMA group | [`isa/ref/b17-spfma.md`](../isa/ref/b17-spfma.md) | result @13, FSR flags @14, `ImpreciseErr` @15; `mov $0xa/$0xd/$0xe` stage immediates |
| the HP FMA group (27 mnemonics) | [`isa/ref/b18-hp-fma.md`](../isa/ref/b18-hp-fma.md) | 27 members, one datapath, three spellings; FCR `RoundMode` read, FSR flags emitted |
| SP convert (`i_imm5`) | [`isa/ref/b13-sp-cvt.md`](../isa/ref/b13-sp-cvt.md) | the 5-bit convert scale; no FTZ/DAZ |
| HP convert (`i_imm4`) | [`isa/ref/b20-hp-cvt.md`](../isa/ref/b20-hp-cvt.md) | the 4-bit convert scale (the HP/SP imm-width split) |
| pipeline stages | [`uarch/pipeline-timing.md`](pipeline-timing.md) | FMA result @13, `TRUNC` @12, the scoreboard model |
| FLIX co-issue legality | [`uarch/co-issue-matrix.md`](co-issue-matrix.md) | FP-FMA is **not** `S3_ALU`-exclusive; bound is slot-count `1×S2 + 1×S3` |
| the soft-float oracle | [`validation/fp-soft-float.md`](../validation/fp-soft-float.md) | the bit-exact differential proof (forward link, Part 15) |
| the seed/refine family | [`validation/transcendental-seed.md`](../validation/transcendental-seed.md) | recip0/rsqrt0 + Newton; the DIVN/recip loop's seed (forward link, Part 15) |

**Divergences noticed (none material; carried for the Part-4 reconcile):**

1. `isa/ref/b17-spfma.md` line 340 reports `ADD_S` makes **11** state samples; my `ADDN_2XF32T`
   `@14` DEF count is **3**. These are consistent — b17's "11 state samples" is the **total**
   USE+DEF count (operand reads @10 + RoundMode USE + the @14 flag DEFs), while my figure isolates
   only the stage-14 FSR-flag DEFs (`mov $0xe,%esi`). Both agree ADD omits Underflow. No conflict;
   note the different denominators.
2. The `'N'`-class flag behaviour splits: the **scalar** `MADDN_S/_H` suppress *all* flags
   (`@14` count = 0), but the **SP** `IVP_DIVNN_2XF32T` still posts 3 (Overflow/Underflow/Inexact,
   no Invalid). Prior prose grouped "the 'N' forms" as flag-suppressing and "DIVN" as
   flag-posting; this page pins both behaviours per-mnemonic from the `_issue` bodies. Recorded
   for the Part-3/Part-4 reconcile (the running B04/B05 signed-flag-count agenda).
