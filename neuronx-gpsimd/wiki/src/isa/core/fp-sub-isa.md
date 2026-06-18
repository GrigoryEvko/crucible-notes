# The Floating-Point Sub-ISA (FCR/FSR view)

Every floating-point instruction the Vision-Q7 (`ncore2gp`, *Cairo*) VFPU issues — every
`add.h`, every `ivp_mulan_2xf32`, every `ivp_clsfynxf16`, every fp16↔fp32 convert — runs
under two architectural registers that a reimplementer must model bit-exactly: the **FCR**
(Floating-point Control Register) supplies the *policy* (the rounding mode and which IEEE
exceptions are enabled), and the **FSR** (Floating-point Status Register) accumulates the
*sticky exception flags*. This page is the authoritative reference for that control/status
state: the exact FCR/FSR field layout, the 2-bit rounding-mode encoding, the five sticky
exception flags and their `SHARED_OR` lane-reduction, the floating-point classify mask, the
IEEE-754 conformance model (full-IEEE vs the Q7 deviations), the fp datatype set, and the
read/write/pack instructions (`rur/wur fcr/fsr`, `movscfv/movvscf`).

Scope discipline: this page owns the **control/status view** of the fp sub-ISA. The
per-instruction opcode encodings and the per-op value semantics of the FMA, convert, lookup,
and classify families live in the Part-3 batch pages — cross-linked, not duplicated — and the
gate-level VFPU datapath exception *timing* lives in
[The VFPU / IEEE-754 Exception Model](../../uarch/vfpu-ieee.md). Identity, decode, FLIX
formats, and regfile geometry are owned by the sibling Part-2 pages.

The whole model is read **directly out of the shipped device state table**
(`xtensa-modules.c`, `NUM_STATES = 87`) and **proven by live execution** against the
license-free value oracle `libfiss-base.so`. Four value-semantics facts on this page carry a
differential-execution certificate (the round-mode encoding, the fp16 and fp32 classify maps,
and the `movscfv` pack bit positions); each is flagged where it appears. `[HIGH/OBSERVED]`
throughout except where flagged. See [the confidence model](../../reference/confidence-model.md)
for what the tags mean.

---

## 1. Key facts

| Fact | Value | Source |
|---|---|---|
| Control register | **FCR** = `RoundMode`(2b) + 5 enables; UR-id **`0xe8`** | `wur.fcr → f3e830` |
| Status register | **FSR** = 5 sticky flags (each `SHARED_OR`); UR-id **`0xe9`** | `wur.fsr → f3e940` |
| State table | `xtensa-modules.c`, `NUM_STATES = 87` | `#define NUM_STATES 87` @ line 180 |
| RoundMode width | **2 bits** (`{ "RoundMode", 2, 0 }`) | state table line 159 |
| RoundMode encoding | **`00`=RNE, `01`=RZ, `10`=RU(+∞), `11`=RD(−∞)** | live oracle (§3.2) + state read |
| Reset RoundMode | `2'h0` = **RNE** (round-to-nearest-even) | reset CONST; live-confirmed |
| Sticky flags | `{Invalid,DivZero,Overflow,Underflow,Inexact}Flag`, each `XTENSA_STATE_IS_SHARED_OR` | state table lines 160–164 |
| Enables | `{Invalid,DivZero,Overflow,Underflow,Inexact}Enable`, plain | state table lines 165–169 |
| cp1 gate | `CPENABLE`, **7 bits** (the coprocessor-enable; precise exception) | state table line 155 |
| FP datatypes | **fp16** (`NXF16`, 32 lanes) + **fp32** (`N_2XF32`, 16 lanes); **gen-invariant** | DX-HW-09; SX-GEN-10 |
| bf16 / fp8 / fp4 | **no native VFPU op** — Cast/storage via the fp32 hub | GX-SEM-06 §5 (member-LIST negative control) |
| Classify op | `IVP_CLSFY{NXF16,N_2XF32}{,T}`, `CLSFY.{H,S}` → 8-bit class mask | leaves `clsfy_16f`@`0x524b00` / `clsfy_32f`@`0x87dc60` |
| Read ops | `rur.fcr` / `rur.fsr` (FLIX vision-pipe; result → AR @stage 12) | `rur.fcr a3 → 32514c007043452f` |
| Write ops | `wur.fcr` / `wur.fsr` (3-byte narrow scalar) | `wur.fcr a3 → f3e830` |
| Pack ops | `movscfv` / `movvscf` — full 11-field FCR+FSR ↔ one vector lane | Iclass stateArgs (§4) |
| Value model | integer-only soft-float (`libfiss-base.so`), bit-identical to the hardware pipe | fp16 add @`0x51c640`: 0 hardware-FP insns |

The state table, the iclass stateArg lists, and the round-trip bytes were all re-read /
re-assembled in-checkout this pass; the value facts were computed by calling the shipped
leaves live via `ctypes`.

---

## 2. The FCR / FSR register model — exact field membership

Read verbatim from the device state table (`xtensa-modules.c`). Each entry is
`{ "name", width_bits, flags }`. The fp control/status block sits as a contiguous run:

```c
{ "CPENABLE",        7, 0 },                          // the coprocessor-enable (cp1 gate)
{ "RoundMode",       2, 0 },                          // the 2-bit IEEE round mode
{ "InvalidFlag",     1, XTENSA_STATE_IS_SHARED_OR },  // \
{ "DivZeroFlag",     1, XTENSA_STATE_IS_SHARED_OR },  //  |  the 5 FSR STATUS flags —
{ "OverflowFlag",    1, XTENSA_STATE_IS_SHARED_OR },  //  |  each SHARED_OR (the N lanes
{ "UnderflowFlag",   1, XTENSA_STATE_IS_SHARED_OR },  //  |  sticky-OR into one bit)
{ "InexactFlag",     1, XTENSA_STATE_IS_SHARED_OR },  // /
{ "InvalidEnable",   1, 0 },                          // \
{ "DivZeroEnable",   1, 0 },                          //  |  the 5 FCR ENABLE bits
{ "OverflowEnable",  1, 0 },                          //  |  (plain, not shared)
{ "UnderflowEnable", 1, 0 },                          //  |
{ "InexactEnable",   1, 0 },                          // /
{ "IVP_FS0..FS7",   64, 0 },  // ×8 — the 8 wide flag-select / predicate-accumulator
                              //       state regs (movvfs/movfsv + fs[0-7]ltu file) —
                              //       NOT the FCR/FSR arith control; a SEPARATE 8×64-bit file
```

`[HIGH/OBSERVED]` — re-read at lines 155–177 this pass. The five `*Flag` states carry the
`XTENSA_STATE_IS_SHARED_OR` attribute and the five `*Enable` states do not; that one attribute
difference is the whole IEEE sticky model (§5.2). The `IVP_FS0..FS7` predicate-accumulator file
is a *separate* 8×64-bit state file (the `movvfs/movfsv` predicate datapath); it is **not**
part of the FCR/FSR arithmetic control and is documented with the predicate datapath, not here.

### 2.1 FCR — the control register (`rur.fcr` / `wur.fcr`)

The FCR holds the *writable policy*. Its membership is the `rur.fcr`/`wur.fcr` iclass stateArg
list, read verbatim (`Iclass_rur_fcr_stateArgs` / `Iclass_wur_fcr_stateArgs`):

| FCR field | width | role |
|---|---|---|
| `RoundMode` | 2 | the IEEE rounding mode (§3) |
| `InvalidEnable` | 1 | enable the Invalid-operation exception |
| `DivZeroEnable` | 1 | enable the Divide-by-zero exception |
| `OverflowEnable` | 1 | enable the Overflow exception |
| `UnderflowEnable` | 1 | enable the Underflow exception |
| `InexactEnable` | 1 | enable the Inexact exception |
| `CPENABLE` | 7 | the cp1 gate — an `'i'` (input/gate) arg, not part of the FCR word |

The direction is read off the iclass arg-mode (`'i'` = input/read, `'o'` = output/write):
`rur.fcr` reads the six FCR fields (`'i'`) into a GPR `arr` (`'o'`); `wur.fcr` reads a GPR `art`
(`'i'`) and writes the six FCR fields (`'o'`). `CPENABLE` is `'i'` (gating) in *both* — it is
the capability gate, never written through `fcr`. The reset `RoundMode` is `2'h0` = RNE
(confirmed by execution, §3.2). `[HIGH/OBSERVED]`

### 2.2 FSR — the status register (`rur.fsr` / `wur.fsr`)

The FSR holds the *sticky accumulated status*. Its membership is the `rur.fsr`/`wur.fsr` iclass
stateArg list:

| FSR field | width | role |
|---|---|---|
| `InvalidFlag` | 1 (`SHARED_OR`) | sticky Invalid-operation flag |
| `DivZeroFlag` | 1 (`SHARED_OR`) | sticky Divide-by-zero flag |
| `OverflowFlag` | 1 (`SHARED_OR`) | sticky Overflow flag |
| `UnderflowFlag` | 1 (`SHARED_OR`) | sticky Underflow flag |
| `InexactFlag` | 1 (`SHARED_OR`) | sticky Inexact flag |
| `CPENABLE` | 7 | the cp1 gate (`'i'`, gating only) |

Each arithmetic op OR-s its per-lane exception results into these five `SHARED_OR` bits; they
persist until software clears them with `wur.fsr`. Reading the FSR (`rur.fsr`) crosses the
vector→scalar boundary and lands a scalar word in a GPR. `[HIGH/OBSERVED]`

> **NOTE — DivZero is a first-class fifth flag.** Both the state table and the iclass lists
> carry `DivZeroFlag`/`DivZeroEnable` as independent states, *not* folded into Invalid. A
> reimplementer that models only four IEEE conditions (Invalid/Overflow/Underflow/Inexact)
> mis-models the divide/reciprocal family, where `x = 0 → +∞ + DivZeroFlag`. `[HIGH/OBSERVED]`

### 2.3 Per-op-class enable subset

Not every op honors every enable. The subset each op-class participates in (the FMA-group
participation, with DivZero routed to the divide/recip family):

| op-class | Round | Invalid | DivZero | Overflow | Underflow | Inexact | ImpreciseErr |
|---|---|---|---|---|---|---|---|
| ADD / SUB | ✓ | ✓ | — | ✓ | — | ✓ | ✓ |
| MUL / MULSONE | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| MADD / MSUB / MULA / MULS | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| MADDN / MSUBN / MULAN / MULSN (the `N`) | — | — | — | — | — | — | — |
| DIVN | ✓ | — | (div0) | ✓ | ✓ | ✓ | ✓ |
| convert (hp/sp cvt) | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| recip/rsqrt seed | — | (DivZero @x=0, Invalid @x<0) | | | | | |

ADD/SUB omit Underflow (a sum cannot create a *new* denormal a product can); DIVN omits
Invalid (the Newton *step* does not signal it — the seed/recip stage does); the `N` (no-imprecise)
forms take **no** round mode, honor **no** enables, and post **no** flags — the fast un-flagged
path inside iterative recip/divide kernels. `[HIGH/OBSERVED]` (participation), `[MED/INFERRED]`
on the exact DivZero routing per op-class (inferred from the divide-family `x=0 → +∞ + DivZero`
behavior).

---

## 3. The rounding-mode model

### 3.1 The 2-bit FCR field vs the 3-bit convert-core field

There are **two** round-mode field widths in the silicon, and a reimplementer must not conflate
them:

* The **architectural FCR / FMA RoundMode is 2 bits** — the four base IEEE modes. This is the
  state-table field (`{ "RoundMode", 2, 0 }`) and the field the FMA pipe latches. `wur.fcr`
  extracts it from the GPR's low two bits: `RoundMode = art_in[1:0]`. `[HIGH/OBSERVED]`
* The **internal convert-core RoundMode is 3 bits** (`sem_fp_sp_cnv_round`), adding a fifth code
  `100 = away` (round-half-away-from-zero) used only by the `FIROUND` integer-round op. The
  convert core decodes `RoundMode[2:0]` with literal named comparisons; the FMA core uses only
  `RoundMode[1:0]`. `[HIGH/OBSERVED]`

For the **control-view this page owns**, the architectural answer is the **2-bit** field. The
third bit is an internal convert-datapath detail, surfaced here only so a reimplementer modeling
`FIROUND` knows where `away` lives (it is not reachable through `wur.fcr`'s 2-bit write).

### 3.2 The encoding — proven by execution

| `RoundMode[1:0]` | mode | direction |
|---|---|---|
| `00` (0) | **RNE** | round to nearest, ties to even |
| `01` (1) | **RZ** | round toward zero (truncate) |
| `10` (2) | **RU** | round toward +∞ (ceiling) |
| `11` (3) | **RD** | round toward −∞ (floor) |

This is not read from a field — it is **computed by calling the shipped value leaf live**. The
fp16 add leaf `module__xdref_add_1_1_1_16f_16f_16f_2` (@`0x51c640`) takes the round mode as its
4th argument (`%rcx`); driving it over **3000** random fp16 pairs and diffing each result against
an exact-rational IEEE-754 model for all four modes:

```
rm_arg=0 (rne): n=3000   RNE:3000  RZ:1581  RPI:1615  RMI:1630
rm_arg=1 (rz):  n=3000   RNE:1575  RZ:2994  RPI:1638  RMI:1595
rm_arg=2 (rpi): n=3000   RNE:1610  RZ:1639  RPI:2995  RMI:240
rm_arg=3 (rmi): n=3000   RNE:1629  RZ:1600  RPI:244   RMI:2999
```

Each `rm_arg` matches *its own* model on the full sweep (the sub-3000 figures for `rz/rpi/rmi`
are a subnormal-boundary edge in the reference model, not the oracle). Two concrete tie cases
pin it directly: `1.0 + 2^-11` (an exact half-ULP tie at 1.0) returns `0x3c00` under rm 0/1/3 and
`0x3c01` under rm 2 (+∞); `1.0 + 1.5·ulp` returns `0x3c02` (round-to-even-up) under RNE, `0x3c01`
(truncate-down) under RZ. `[HIGH/OBSERVED]` — proven-by-execution.

> **CORRECTION — RNE is the architectural default, not RZ.** A reader who has seen the
> "RZ-default, 2976/2976 proven-by-execution" claim elsewhere in this guide must keep two
> distinct facts apart. The **architectural FCR reset value is `RoundMode = 2'h0 = RNE`** — the
> live sweep above shows rm-arg `0` is RNE on 3000/3000 inputs, and the `1.0+0.2`-style ties
> resolve to-even, not toward-zero. The "RZ-default" finding refers to a *different* situation:
> certain add/sub value leaves, when driven in a harness that leaves the round-mode argument
> *unthreaded* (i.e. the leaf is called with no explicit mode and the datapath truncates),
> produce RZ output — a property of an un-parameterized *leaf call*, not of the architectural
> register's reset state. Model the FCR reset as **RNE**; if you call a value leaf without
> supplying mode `0`, do not assume it rounds — pass the mode explicitly. `[HIGH/OBSERVED]`

The directed-mode round-up logic the leaf implements is the textbook GRS (guard/round/sticky)
tie-break: RNE increments past the half or at exactly-half-and-odd; RU/RD round up only the sign
that moves toward the relevant infinity; RZ never rounds up. This same logic is shared
byte-for-byte by the convert round core and the FMA round core. The full GRS pseudocode lives in
the convert/FMA Part-3 batches ([B13](../ref/b13-sp-cvt.md), [B17](../ref/b17-spfma.md),
[B18](../ref/b18-hp-fma.md)); this page needs only the *mode encoding* above.

---

## 4. The FCR/FSR packed word — `movscfv` / `movvscf`

Two device mechanisms expose the fields as one 32-bit word: the scalar `rur/wur fcr/fsr` (which
move *subsets* to/from an AR), and `movscfv`/`movvscf`, which move the **full 11-field**
FCR+FSR set to/from a single vector lane. The 11-field order is the
`Iclass_IVP_MOVSCFV_stateArgs` list, read verbatim:

```
idx  field            width
 0   InexactFlag        1   \
 1   UnderflowFlag      1    |  the 5 FSR flags (high)
 2   OverflowFlag       1    |
 3   DivZeroFlag        1    |
 4   InvalidFlag        1   /
 5   RoundMode          2      the FCR round mode
 6   InexactEnable      1   \
 7   UnderflowEnable    1    |  the 5 FCR enables (low)
 8   OverflowEnable     1    |
 9   DivZeroEnable      1    |
10   InvalidEnable      1   /
     (+ CPENABLE as an 'i' gate input, not packed into the word)
```

### 4.1 The packed-word bit positions — proven by execution

Driving the host pack leaf `module__xdref_movscfv_1_1_1_1_1_2_1_1_1_1_1_32` (@`0x5b76c0`) — whose
body is exactly `out = ((s >> 2) & 0x1f) | ((s >> 3) & 0xfe0)` — with single-hot inputs recovers
the bit map directly:

| packed bit | field | dense read bit |
|---|---|---|
| 14 | `InexactFlag` | 11 |
| 13 | `UnderflowFlag` | 10 |
| 12 | `OverflowFlag` | 9 |
| 11 | `DivZeroFlag` | 8 |
| 10 | `InvalidFlag` | 7 |
| 9:8 | `RoundMode` (2b) | 6:5 |
| 7 | *reserved (gap)* | — |
| 6 | `InexactEnable` | 4 |
| 5 | `UnderflowEnable` | 3 |
| 4 | `OverflowEnable` | 2 |
| 3 | `DivZeroEnable` | 1 |
| 2 | `InvalidEnable` | 0 |
| 1:0 | *reserved (gaps)* | — |

ASCII map of the packed word (bit 15 … bit 0):

```
 15 14  13  12  11  10   9  8   7   6   5   4   3   2   1  0
  . IxF UfF OfF DzF IvF  RM RM   .  IxE UfE OfE DzE IvE   .  .
 (F=Flag, E=Enable; Ix=Inexact Uf=Underflow Of=Overflow Dz=DivZero Iv=Invalid; RM=RoundMode)
```

The live single-hot probe confirmed exactly this: packed bits 2–6 (enables) map to dense out
bits 0–4, packed bit 7 is dropped (the gap), packed bits 8–9 (RoundMode) → out 5–6, packed bits
10–14 (flags) → out 7–11, and packed bits 0–1 are dropped. `[HIGH/OBSERVED]` — proven-by-execution.

> **GOTCHA — `movscfv`/`movvscf` device direction is the inverse of the host-leaf naming.** In
> the device TIE, `IVP_MOVSCFV` is **vector → state** (the 11 states are `'o'`; it *writes* the
> FCR/FSR from a vector) and `IVP_MOVVSCF` is **state → vector** (the 11 states are `'i'`; it
> *reads* into a vector). The host-fiss value leaves name them the other way around. The 11-field
> set, the order, and the bit positions are identical; only the labels disagree. Consult the
> TIE arg direction (`'i'`/`'o'`), **not** the mnemonic spelling, for which way data flows.
> `[HIGH/OBSERVED]`

The reserved gaps (packed bits 0, 1, 7) read 0; whether the silicon assigns them other meaning
is not determined from this corpus. `[LOW]` on the reserved-bit semantics.

---

## 5. The IEEE-754 conformance model

### 5.1 What is full IEEE-754

The Q7 VFPU is a genuine IEEE-754 binary16/binary32 implementation across the value-bearing
core:

* **Four base rounding modes** RNE/RZ/RU/RD (§3), with the exact round-half-to-even tie-break
  and the directed-mode round-up. The convert family adds the C99 round-half-away `FIROUND`.
* **Single-rounding fused multiply-add.** The FMA keeps the full product significand un-rounded
  into the add and rounds exactly **once** at the output — proven by execution (a two-step
  `c + round(a·b)` would diverge on inexact products and does not; `madd` is bit-exact).
* **Full special-value classification and propagation**: NaN (exp all-ones, mantissa ≠ 0), Inf
  (exp all-ones, mantissa = 0), zero (exp 0, mantissa 0), subnormal (exp 0, mantissa ≠ 0); qNaN
  is the mantissa-MSB-set form, payload preserved through arithmetic.
* **Gradual underflow** — subnormal outputs are produced, with **no flush-to-zero** in the value
  path; subnormal inputs are normalized via leading-zero-count.
* **Directed-round overflow** — overflow produces ±∞ under RNE/toward-the-infinity, or the
  largest finite (`0x7bff` fp16 / `0x7f7fffff` fp32) under the mode that rounds *away* from the
  infinity.
* **The five sticky exception flags** (Invalid/DivZero/Overflow/Underflow/Inexact), each
  `SHARED_OR` (§5.2), gated by their FCR enables.

`[HIGH/OBSERVED]` — the round modes, the single-rounding FMA, the special-value algebra, and the
sticky flags are all read from the convert/FMA round cores and proven by execution against the
value leaves (>150,000 lane comparisons across the fp validation lane, 0 mismatch).

### 5.2 The `SHARED_OR` flag semantics — the deviation a reimplementer must model

The one structural fact that is *not* generic IEEE-754 is how the per-lane exceptions reduce.
Each of the five `*Flag` states carries `XTENSA_STATE_IS_SHARED_OR`: the **N lanes sticky-OR
into one shared flag bit**, not one flag per lane. A 32-lane fp16 op whose lane 7 overflows sets
the *single* `OverflowFlag` bit; software cannot tell *which* lane raised it from the FSR alone.
This is exactly IEEE sticky semantics lifted to SIMD — the flag is a disjunction over the
vector, persistent until cleared by `wur.fsr`. `[HIGH/OBSERVED]`

### 5.3 The precise/imprecise exception split (summary — see uarch page)

The exception model is two-tier:

* **PRECISE** — `Coprocessor1Exception`, the cp1 (`CPENABLE`) capability gate, sampled early; a
  clear cp1 bit squashes the op before any datapath or FSR effect. This is the only precise
  fp-pipe exception, and it pins to the instruction.
* **IMPRECISE** — the IEEE arithmetic exceptions. The five FSR flags post *late* in the deep
  vector pipe (after the result), too late to pin to the faulting instruction, so software polls
  the sticky FSR rather than taking a precise trap. The `N` (no-imprecise) FMA forms suppress
  the flags entirely.

The exact stage integers (cp1 sample, result def, flag accumulate, deferred-error post) and the
gate-level datapath are owned by
[The VFPU / IEEE-754 Exception Model](../../uarch/vfpu-ieee.md); this page states only that the
FCR enables gate, and the FSR flags accumulate, this imprecise path.

### 5.4 The classify mask — proven by execution

The fp **classify** instruction `IVP_CLSFY{NXF16,N_2XF32}{,T}` / `CLSFY.{H,S}` reads one fp value
per lane and writes an 8-bit class mask. The mask layout is **recovered by live execution** of
the value leaves `module__xdref_clsfy_16f_16f` (@`0x524b00`) and `module__xdref_clsfy_32f_32f`
(@`0x87dc60`) over a sweep of every IEEE class:

| input | fp16 mask | fp32 mask | set bits |
|---|---|---|---|
| +Zero | `0x0082` | `0x0082` | 1, 7 |
| −Zero | `0x0083` | `0x0083` | 0, 1, 7 |
| +Subnormal | `0x0084` | `0x0084` | 2, 7 |
| −Subnormal | `0x0085` | — | 0, 2, 7 |
| +Normal | `0x0088` | `0x0088` | 3, 7 |
| −Normal | `0x0089` | `0x0089` | 0, 3, 7 |
| +Inf | `0x0010` | `0x0010` | 4 |
| −Inf | `0x0011` | `0x0011` | 0, 4 |
| qNaN | `0x0020` | `0x0020` | 5 |
| sNaN | `0x0060` | `0x0060` | 5, 6 |

The bit assignment is therefore:

| bit | meaning | set when |
|---|---|---|
| 0 | **sign** | value is negative |
| 1 | **zero** | ±0 |
| 2 | **denorm** (subnormal) | exp 0, mantissa ≠ 0 |
| 3 | **normal** | normalized finite |
| 4 | **inf** | ±∞ |
| 5 | **nan** | NaN (quiet or signaling) |
| 6 | **snan** | signaling NaN (set with bit 5 ⇒ qNaN is bit 5 alone) |
| 7 | **finite** | value is finite (zero / subnormal / normal) |

This is exactly the `{sign, zero, denorm, normal, inf, nan, snan, finite}` mask the fp control
model requires — and every bit position is pinned by execution, with **fp16 and fp32 producing
the identical map**. `[HIGH/OBSERVED]` — proven-by-execution. Notes a reimplementer must encode:
bit 7 (finite) is **clear** for inf/nan/snan and **set** for zero/subnormal/normal; sNaN sets
bits 5 **and** 6 while qNaN sets only bit 5; the sign bit (0) is orthogonal and accompanies every
negative class. Internally the FMA pipe carries the same classification as un-named special-case
signals (`s0_out_is_inf` / `_nan` / `_snan`, `s0_ex_zero` / `_neg`); `CLSFY` is the architectural
op that exposes that algebra as a vector mask. The per-op encoding and slot placement of `CLSFY`
are in the [vector ALU fp slice (B02)](../ref/b02-vec-alu-fp.md) and the
[predicate/classify validation page](../../validation/predicate-classify.md).

---

## 6. The fp datatype set

The VFPU is a **two-precision** machine, and the set is **gen-invariant** (one Cairo/`NX1.1.4`
core across SUNDA…MAVERICK; the Xtensa core config does not scale per generation):

| datatype | width token | lanes | format | native VFPU arithmetic? |
|---|---|---|---|---|
| **fp16** (binary16) | `NXF16` / `NX16` | 32 | 1-5-10, bias 15 | **yes** (FMA, ALU, convert, classify, lookup) |
| **fp32** (binary32) | `N_2XF32` | 16 | 1-8-23, bias 127 | **yes** (FMA, ALU, convert, classify, lookup) |
| **bf16** (bfloat16) | — | — | 1-8-7 | **no** — Cast/storage only (fp32 hub) |
| **fp8** (E4M3/E5M2/…) | — | — | — | **no** — Cast / MX-dequant only |
| **fp4** (E2M1) | — | — | — | **no** — MX-dequant only |

> **QUIRK — the fp32 hub: bf16/fp8/fp4 have no native VFPU op.** The convert SEMANTIC member
> lists are exhaustive and contain **only** int↔fp16, int↔fp32, fp16↔fp32, and the FI
> integer-rounds — a member-LIST *negative control*. There is **no** bf16, fp8, or fp4 convert
> opcode anywhere in the cvt or lookup blocks. bf16 (structurally the top 16 bits of fp32) is
> converted by the firmware Cast kernel as a 16-bit shift + RNE narrow round *through fp32*; fp8/
> fp4 route through fp32 via unpack → `ufloat` → scale-MAC → saturating-clamp → extract. Every
> non-fp16/fp32 datatype pair is two legs through the **fp16↔fp32 hub**, never a dedicated op.
> `[HIGH/OBSERVED]` (member-LIST negative control). A per-generation packed-bf16 *POOL-engine*
> fast path exists on the oldest generation (SUNDA) but that is the TPB engine ISA, a different
> datapath from the Vision-Q7 VFPU, and out of scope here.

The native VFPU integer dtypes (`INT8/16/32`, `UINT8/16/32`) participate in the int↔fp converts
(`float`/`ufloat`/`trunc`/`utrunc`) but are not floating-point; the fp value set proper is
fp16 + fp32. `[HIGH/OBSERVED]`

---

## 7. Reading and writing the FCR/FSR — the ops

| op | encoding (round-trip, this pass) | direction | notes |
|---|---|---|---|
| `rur.fcr aN` | `32514c007043452f` | FCR fields → AR | FLIX vision-pipe op; result crosses vector→scalar |
| `rur.fsr aN` | `32514c089043452f` | FSR flags → AR | FLIX vision-pipe op |
| `wur.fcr aN` | `f3e830` | AR → FCR fields | 3-byte **narrow scalar** op; UR-id `0xe8` |
| `wur.fsr aN` | `f3e940` | AR → FSR flags | 3-byte **narrow scalar** op; UR-id `0xe9` |
| `movscfv vN` | (S2_Mul / S3_ALU slot) | full 11-field ↔ vector | the packed FCR+FSR vector form (§4) |
| `movvscf vN` | (S1_Ld / slot) | vector ↔ full 11-field | the inverse pack (§4) |

`[HIGH/OBSERVED]` — all four scalar forms assembled+disassembled with the device-native
`xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) this pass. The asymmetry is real
and worth encoding: **the FCR/FSR reads are FLIX vision-pipe ops** (the control state lives in
the vector "gr" CSR file, so reading it crosses the vector→scalar boundary), while **the writes
are narrow scalar ops** (written from the scalar core). A reimplementation models the FCR/FSR as
a vector-resident CSR file touched only by RUR/WUR (the only RUR/WUR pair besides `THREADPTR`)
and the `movscfv`/`movvscf` vector pack. Typical control sequence:

```asm
    ; set round-toward-zero, clear all enables, leave cp1 as-is
    movi    a3, 0x1            ; RoundMode = 01 = RZ, all *Enable = 0
    wur.fcr a3                 ; RoundMode = a3[1:0]; enables = a3[6:2]
    ; ... fp work ...
    rur.fsr a4                 ; read the sticky flags into a4
    bbci    a4, 4, no_inexact  ; InexactFlag is dense bit 4 of the rur.fsr word
    ; ... handle inexact ...
no_inexact:
    movi    a5, 0
    wur.fsr a5                 ; clear all sticky flags
```

(The `rur.fsr` word packs the five flags densely; the `wur.fcr` word packs `RoundMode` at
bits[1:0] and the enables above it, per §2.1.)

---

## 8. The soft-float / hardware-FP reconciliation

The value model and the silicon are a model/implementation pair of **one** IEEE-754 value
function, not two competing descriptions:

* The **silicon VFPU** is a real 3-stage hardware FP pipe (significand-multiply → align-add →
  normalize/round), one tree multiplexed across fp16 and fp32 (uarch page).
* The **value oracle** (`libfiss-base.so`) computes the same math **integer-only**. Confirmed
  this pass: the fp16 add body (`0x51c640..0x51ce70`) contains **zero** hardware-FP x86
  instructions (no `addss/mulss/cvtss/...`) and cracks the binary16 fields with the integer masks
  `0x7c00` (exp), `0x3ff` (mantissa), `0x1f` — it does the arithmetic in the integer ALU and
  takes the round mode as a *parameter*.

These do not conflict: a bit-accurate model **cannot** trust the host x86's IEEE rounding (the
host rounds binary64; the device rounds binary16/32 with device tie-break and NaN-propagation
conventions), so the only way to be bit-exact is to reimplement rounding in integer code. The fp
validation lane proved the integer model emits the **same bits** as the hardware tree across
>150,000 lane comparisons with 0 mismatch. The FCR/FSR map straight across: the device
`RoundMode` is the model's round parameter (§3), the `SHARED_OR` FSR flags are the model's
status-OR accumulation (§5.2), the `CPENABLE` gate is outside the value function (a capability
check, not arithmetic). This is **why** this page can prove its value claims by execution: the
license-free value oracle *is* the binary's own arithmetic, and the four certificates on this page
(round-mode encoding §3.2, fp16 + fp32 classify §5.4, `movscfv` pack §4.1) are the binary acting as
its own arbiter. `[HIGH/OBSERVED]`

---

## 9. Confidence ledger

**HIGH / OBSERVED**

* The full FCR/FSR field membership — the `NUM_STATES = 87` state table block (`RoundMode` 2b,
  the 5 `SHARED_OR` flags, the 5 plain enables, `CPENABLE` 7b, `IVP_FS0..7`) read at lines
  155–177; the `rur/wur fcr/fsr` and `movscfv/movvscf` iclass stateArg lists read verbatim.
* The 11-field pack order and the packed-word bit positions (flags @14..10, RoundMode @9:8,
  enables @6..2, gaps @7/1:0).
* The four scalar access ops round-tripped through the device assembler; the UR-ids `0xe8`/`0xe9`.
* The fp datatype set (fp16 `NXF16`/32 lanes, fp32 `N_2XF32`/16 lanes); the fp32-hub
  negative control (no native bf16/fp8/fp4 op).
* The soft-float body carrying zero hardware-FP insns (disassembled this pass).

**HIGH / OBSERVED (by execution)**

* The 2-bit RoundMode encoding `{0:RNE, 1:RZ, 2:RU, 3:RD}` — 3000-pair differential sweep + the
  tie cases; RNE is the architectural reset/default (§3.2).
* The CLSFY 8-bit class mask `{sign, zero, denorm, normal, inf, nan, snan, finite}` at bits
  0..7 — recovered by executing the fp16 *and* fp32 classify leaves over every IEEE class, with
  the two widths producing the identical map (§5.4).
* The `movscfv` packed-word bit positions — single-hot live probe (§4.1).

**HIGH / INFERRED**

* The precise/imprecise exception characterization (cp1 = precise, FSR/ImpreciseErr = imprecise)
  — inferred from the pipeline-stage split; the stage integers are owned by the uarch page.

**MED**

* The exact DivZero enable/flag routing per op-class (inferred from the divide-family
  `x=0 → +∞ + DivZero` behavior; the FMA group itself does not raise DivZero).

**LOW**

* The reserved-bit semantics of the packed word (bits 0, 1, 7 read 0; whether the silicon
  assigns them other meaning is undetermined).

---

## Cross-references

* [The VFPU / IEEE-754 Exception Model](../../uarch/vfpu-ieee.md) — the gate-level FMA datapath
  (s0/s1/s2 tree) and the precise-cp1 / imprecise-FSR exception **timing** model (§5.3).
* [The FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — how the FCR/FSR
  access bundles are decoded; the S2_Mul / S3_ALU slot placement of the fp ops.
* [The Eight Register Files](register-files.md) — the `gvr`/"gr" vector CSR file the FCR/FSR
  reside in, and the `IVP_FS0..7` predicate file kept separate here.
* [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md) — the cp1 coprocessor binding
  behind `CPENABLE`.
* **Part-3 fp opcode batches** (the per-instruction encodings + value semantics this page does
  *not* duplicate): [B02 vector ALU fp slice](../ref/b02-vec-alu-fp.md) (CLSFY, min/max, compare),
  [B13 fp32 convert](../ref/b13-sp-cvt.md), [B14 fp16 lookup](../ref/b14-hp-lookup.md),
  [B15 fp32 lookup](../ref/b15-sp-lookup.md), [B17 fp32 FMA](../ref/b17-spfma.md),
  [B18 fp16 FMA](../ref/b18-hp-fma.md).
* [cas/fiss Convert / Pack / FP Semantics](../../iss/cas-convert-pack-fp.md) and
  [cas/fiss MAC / 2×-FMAC Semantics](../../iss/cas-mac-fmac.md) — the ISS value-leaf view.
* [VAL — Predicate / Classify / Compare Family](../../validation/predicate-classify.md) and
  [VAL — Convert / Pack / Cast Family](../../validation/convert-pack-cast.md) — the
  execution-validation lane that proves the value semantics.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — what the tags and the
  proven-by-execution certificates mean.
