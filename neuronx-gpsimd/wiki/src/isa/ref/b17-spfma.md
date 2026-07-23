# ISA Batch 17 — fp32 Fused Multiply-Add (spfma)

This batch is the **single-precision (binary32) fused-multiply-add datapath** of the Vision-Q7 *Cairo*
(`ncore2gp`) vector ISA: the family that computes `d = round_RM(±(a·b) ± c)` over **16 lanes of fp32**
(`N_2XF32` = 16 × IEEE-754 1-8-23) with **exactly one rounding**. It owns the MUL / ADD / SUB / MADD /
MSUB / MADDN / MSUBN / MULSONE / DIVN forms in two mnemonic spellings — **18 `ivp_*n_2xf32` vector
opcodes** (9 producing/accumulate roots × a `t` predicated twin) plus **9 short `.S` scalar aliases** —
that all bind the **same** multiplexed fp32 FMA tree. It is the fp32 peer of [B18](b18-hp-fma.md)
(the fp16 `NXF16` FMA) and consumes the reciprocal/rsqrt seeds of [B15](b15-sp-lookup.md); it sits in
the floating-point sub-ISA whose control/status model is documented in
[the FCR/FSR view](../core/fp-sub-isa.md) and whose microarchitecture is the
[VFPU / IEEE-754 pipe](../../uarch/vfpu-ieee.md). The preceding committed boundary is
[B16 — Vector Replicate / Extract](b16-vec-rep.md).

Everything below is grounded in the shipped binaries: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte for the selector
`CONST`), the **operand/state signature** from the per-op `Iclass_<MNEM>_args` / `_stateArgs` tables
(read directly out of `.data.rel.ro`), the **timing** from the `*_inst_*_issue` bodies in
`libcas-core.so` (`mov $stage,%esi` operand-port stamps), the **single-rounding value semantics** by
*executing* the `module__xdref_madd_*` leaf in `libfiss-base.so` live in-process via ctypes, and a
byte-exact **encode/decode oracle** from the device-native `xtensa-elf-as`/`xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). The page default is `[HIGH/OBSERVED]` (read-from-byte /
proven-by-execution); claims that depart from that default carry an explicit tag per
[the Confidence & Walls model](../../reference/confidence-model.md) — `[MED/INFERRED]` = reasoned
over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic.** `libisa-core.so` (sha256
> `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`, 9 690 712 B, ET_DYN x86-64, not
> stripped). Per `readelf -SW`: `.text` (`0x312c10`) and `.rodata` (`0x3b6e40`) are
> **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) and `.data` (VMA `0x764040`
> ↔ file `0x564040`) carry the per-binary delta **`0x200000`** — **not** libtpu's `0x400000`. The
> `Iclass_*_args` / `_stateArgs` operand tables live in `.data.rel.ro` and must be read at
> `VMA − 0x200000`; the encode thunks live in `.text` (VMA == file). `libfiss-base.so` and
> `libcas-core.so` keep `.text`/`.rodata` at VMA == file-offset. All three are in `extracted/`
> (gitignored; reach with `fd --no-ignore` or an absolute path).

---

## 0. Scope boundary — what this batch owns, and the reclamation

The unifying property this batch owns is the **fused-multiply-add value shape at fp32 width**: every
op computes `±(a·b)±c` (where `c` is absent for the producing MUL/ADD/SUB and present for the
accumulate/divide forms) **rounded once** to binary32, over the 16-lane `N_2XF32` vector. Membership is
proven not by mnemonic spelling but by the **operand-semantic** carried in the iclass: every member's
data operand is `opnd_ivp_sem_spfma_{vt,vs,vr}` (§3). Four walls keep the 30 batches from
double-counting.

* **fp32 ALU min / max / compare / classify / convert (`ivp_minn_2xf32`, `ivp_oeqn_2xf32`,
  `ivp_cvtf16n_2xf32`, …) → [B02](b02-vec-alu-fp.md) / [B13](b13-sp-cvt.md), NOT here.** These spell
  `*n_2xf32` and ride the same FLIX slots, but their iclass operand is `opnd_ivp_sem_vec_alu_*`
  (min/max/compare) or the `sp_cvt` semantic — **not** `ivp_sem_spfma`. Read live:
  `IVP_MINN_2XF32` → `opnd_ivp_sem_vec_alu_vt`, `IVP_OEQN_2XF32` → `opnd_ivp_sem_vec_alu_vbt`,
  `IVP_CVTF16N_2XF32_0` → the sp_cvt convert core. Only the **9 FMA roots** (`muln`, `addn`, `subn`,
  `mulan`, `mulann`, `mulsn`, `mulsnn`, `mulsonen`, `divnn`) carry `ivp_sem_spfma`.
* **fp32 transcendental seeds (`ivp_recip0n_2xf32`, `ivp_rsqrt0n_2xf32`, `ivp_nexp01n_2xf32`,
  `ivp_div0n_2xf32`, `ivp_mksadjn_2xf32`) → [B15](b15-sp-lookup.md), NOT here.** Those are the
  table-lookup *seed* ops the `divnn`/`mulann` Newton chain *consumes*; they live in the
  `ivpep_sem_sp_lookup` semantic, not spfma. The `DIVN` here is the one-shot divide that *chains*
  those seeds, not the seed itself.
* **fp16 FMA (`ivp_mulnxf16`, `madd.h`, …) → [B18](b18-hp-fma.md), NOT here.** The 32-lane `NXF16` FMA
  is the half-precision peer; it shares the s0/s1/s2 tree skeleton at a narrower significand but is a
  distinct semantic (`fp_sem_hp_fma`). B17 owns **only** the 16-lane fp32 `N_2XF32` forms.
* **Integer MAC (`ivp_mulan_2x32`, `ivp_mulanx16`, the complex `_2x32c`) → [B04](b04-mac-integer.md) /
  [B05](b05-mac-mixed.md), NOT here.** Those accumulate into the **wide `wvec`** integer accumulator
  (24/48/96-bit) with no rounding; the fp32 FMA accumulates into a **`vec`** lane with IEEE rounding.
  See the CORRECTION below.

> **CORRECTION — the `*_2xf32` MUL / MADD forms are floating-point FMA, NOT integer MAC.** The
> sibling pages [B04](b04-mac-integer.md) and [B05](b05-mac-mixed.md) noted that a coarse "integer-MAC"
> partition over-counts **24 FP-typed MAC forms** (`mulnxf16`, `mulan_2xf32`, `mulsonenxf16`, …) as if
> they belonged to the integer batches. **This page authoritatively reclaims the fp32 half.** The exact
> fp32 forms reclaimed (`nm libisa-core.so | rg -i '2xf32.*(mul|add|sub|madd|msub|divn|mulsone)'`,
> filtered to the FMA roots) are the **18 vector mnemonics**
> `ivp_{muln,mulan,mulann,mulsn,mulsnn,mulsonen,addn,subn,divnn}_2xf32{,t}` plus the **9 `.S` scalar
> aliases** `{add,sub,mul,madd,maddn,msub,msubn,mulsone,divn}.s` — **27 in total**. They are FP because
> (a) every one carries the `opnd_ivp_sem_spfma_*` operand semantic, (b) their iclass `stateArgs` carry
> `RoundMode` + the IEEE `*Enable` / `*Flag` fields (an integer MAC carries none), and (c) the live
> `madd` value leaf is a single-rounded IEEE-754 fused multiply-add (§4). A reimplementation that routes
> `ivp_mulan_2xf32` through an integer `wvec` accumulator is wrong: it accumulates a **rounded fp32
> sum into a `vec` lane**, not an exact integer product into a wide accumulator.

---

## 1. Batch key facts

| Fact | Value | Binary source |
|---|---|---|
| Semantic group | **`ivp_sem_spfma`** (one shared multiplexed fp32 FMA tree) | `opnd_ivp_sem_spfma_*` in every member's `Iclass_*_args` |
| Lanes × width | **16 × fp32** (`N_2XF32`, IEEE-754 1-8-23) over the 512-bit `vec` | the `_2xf32` dtype tag; `vec` idx2 |
| Mnemonics this batch | **27** = 18 vector (`ivp_*n_2xf32{,t}`) + 9 scalar (`.S`) | §2; `nm libisa-core.so` distinct `Opcode_…_Slot` roots |
| Producing roots (write `vt`) | MUL · ADD · SUB · MULSONE (+ `.S`, + `t`) | `vt` dir = **`o`** (output) in `_args` (§3) |
| Accumulate roots (read-modify `vt`) | MADD/MULA · MSUB/MULS · MADDN/MULANN · MSUBN/MULSNN (+`.S`,+`t`) | `vt` dir = **`m`** (modify) in `_args` (§3) |
| Divide root | DIVN / `divnn_2xf32` (+`.S`,+`t`) | `vt` dir = **`m`**; no `InvalidEnable` (§3) |
| Data regfile | **`vec`** (idx2, 512b×32) for `vr`/`vs`/`vt`; **`vbool`** (idx3) for the `t` predicate | [register-files](../core/register-files.md) |
| Control state | **FCR** `RoundMode` + per-op `{Invalid,Overflow,Underflow,Inexact}Enable`; **FSR** the matching sticky `*Flag` | `Iclass_*_stateArgs` (§3, §5) |
| Encode-thunk ABI | `C7 07 imm32 · C7 47 04 00000000 · C3` — `imm32`(word0) = selector `<<15`, **word1 == 0** (S3_ALU) | §2.2; [flix-encoding §6.1](../core/flix-encoding.md) |
| Result latency | operands read **@stage 10**, rounded result `vt` DEF **@stage 13** (3-cycle), FSR flags **@14**, ImpreciseErr **@15** | `libcas-core.so` `*_issue` `mov $0xa/$0xd/$0xe,%esi` (§5) |
| Rounding | **single** (one round on the exact `a·b+c`), `RoundMode`-driven, RNE default; `N` forms force RNE | live `madd` leaf (§4); [VFPU/IEEE](../../uarch/vfpu-ieee.md) |
| Oracle | `xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp` | **16 forms round-trip byte-exact** (§6) |

> **The batch is one fully-multiplexed fp32 FMA tree, selector-indexed.** Every op is a route through
> *one* hardware datapath: a 2×2 partial-product array of the 24-bit significands → an align-and-fused-
> add on the **exact** (un-pre-rounded) product → a single normalize-and-round under the FCR
> `RoundMode`. The opcode-selector `CONST` lights exactly one op-class mux (`add_only` / `mul` / `madd` /
> `op_negadd` / `op_maddn` / `op_mulsone` / `op_divn`); with operands held fixed, the XOR of any two
> sibling bundles is confined to the selector field (§6). A reimplementation builds one parameterised
> FMA unit and decodes `(op-class, negate-product, negate-addend, force-RNE, predicate-enable)` from the
> selector. `[HIGH/OBSERVED]`

---

## 2. Batch roster — 27 fp32 FMA opcodes

The 27 split into **18 vector** mnemonics (`ivp_*n_2xf32`, each with a `t` predicated twin) and **9
scalar `.S` aliases**. The two spellings bind the *same* compute — the `.S` names are the toolchain's
aliases for the same 16-lane fp32 datapath, **not** AR-scalar fp (their operands are `vec`, §3, §6). The
`opc-sel` column is the F0/S3 selector field `bits[34:15]` (= the thunk's `word0 >> 15`); `placements`
is the per-mnemonic `Opcode_…_Slot_…_encode` count. Every row of §2.1–§2.3 is `[HIGH/OBSERVED]` except
the flagged `mulsn` polarity cell.

### 2.1 Producing forms — write `vt` (no accumulator read)

`vt = round_RM(a ⊙ b)`, `a = vr`, `b = vs`. The `t` twin adds a `vbool` mask and merges per lane.

| mnemonic | form | opc-sel (F0/S3) | placements | semantics |
|---|---|---|---|---|
| `ivp_muln_2xf32` / `mul.s` | MUL | `0x1015c` / `0x1019d` | 9 / 9 | `vt[k] = round_RM(a·b)` |
| `ivp_addn_2xf32` / `add.s` | ADD | `0x10d4f` / `0x10d4e` | 9 / 9 | `vt[k] = round_RM(a+b)` |
| `ivp_subn_2xf32` / `sub.s` | SUB | `0x101dc` / `0x101bd` | 9 / 9 | `vt[k] = round_RM(a−b)` |
| `ivp_mulsonen_2xf32` / `mulsone.s` | MUL (one-path) | `0x101bc` / `0x1017d` | 9 / 9 | `vt[k] = round_RM(a·b)`, `op_mulsone` partial-product path (recip-residual seed) |
| `ivp_muln_2xf32t` / `ivp_addn_2xf32t` / `ivp_subn_2xf32t` / `ivp_mulsonen_2xf32t` | `t` twins | (selector narrows; vbr field set) | 9 each | `vt[k] = pred[k] ? <result> : vt[k]` |

### 2.2 Accumulate forms — read-modify `vt` (the fused MADD/MSUB family)

`vt` is an **in-place accumulator** (`_args` dir = `m`): it is both read (the addend `c`) and written.
The assembler still requires `vt` spelled; a 2-operand form errors "too few operands" (§6).

| mnemonic | form | opc-sel (F0/S3) | placements | semantics |
|---|---|---|---|---|
| `ivp_mulan_2xf32` / `madd.s` | MADD | `0x1013c` / `0x1011d` | 7 / 7 | `vt[k] = round_RM(a·b + c)`  (single round) |
| `ivp_mulsn_2xf32` / `msub.s` | MSUB | `0x1019c` / `0x1015d` | 7 / 7 | `vt[k] = round_RM(c − a·b)`  (`negate_axb_M` sign path); form `[HIGH/OBSERVED]`, exact `c−a·b` vs `a·b−c` polarity `[MED]` |
| `ivp_mulann_2xf32` / `maddn.s` | MADDN ('N') | `0x1011c` / `0x101fc` | 7 / 7 | `vt[k] = round_RNE(a·b + c)`, **RNE-forced**, no flags (Newton step) |
| `ivp_mulsnn_2xf32` / `msubn.s` | MSUBN ('N') | `0x1017c` / `0x1013d` | 7 / 7 | `vt[k] = round_RNE(c − a·b)`, **RNE-forced**, no flags |
| `ivp_mulan_2xf32t` / `ivp_mulsn_2xf32t` / `ivp_mulann_2xf32t` / `ivp_mulsnn_2xf32t` | `t` twins | (vbr field set) | 7 each | predicated read-modify merge |

> **NOTE — `mulan`/`mulsn` (vector) and `madd`/`msub` (`.S`) are the same form, different spelling.**
> The vector `mulan` = "multiply-add", the `.S` `madd` = "multiply-add"; both are `round_RM(a·b+c)` with
> a read-modify `vt`. Likewise `mulsn`/`msub`, `mulann`/`maddn`, `mulsnn`/`msubn`. The selector const is
> the *only* byte difference (§6).

### 2.3 Divide form — one-shot fp32 divide

| mnemonic | form | opc-sel (F0/S3) | placements | semantics |
|---|---|---|---|---|
| `ivp_divnn_2xf32` / `divn.s` | DIVN | `0x10d6f` / `0x10d6e` | 7 / 7 | `vt[k] = round_RM(a / b)` — seeded by recip ([B15](b15-sp-lookup.md)) + refined by the MADDN/MULSONE chain |
| `ivp_divnn_2xf32t` | `t` twin | (vbr field set) | 7 | predicated divide |

> **GOTCHA — DIVN drops `InvalidEnable` and rides `vt` as read-modify, even though it has no addend.**
> The divide's iclass `stateArgs` carry `Overflow/Underflow/InexactEnable` but **no `InvalidEnable`**
> (the 0/0 and ∞/∞ invalid cases are handled by the `op_divn` mux + sticky flag, not the enable edge
> the mul/add forms carry). Its `vt` operand dir is `m` (read-modify) — `vt` is the quotient/result
> register the iterative refinement writes through. A reimplementation must not give DIVN an
> `InvalidEnable` input nor treat `vt` as pure-output — read from the `Iclass_DIVN_S` /
> `Iclass_IVP_DIVNN_2XF32` `stateArgs` (§3).

### 2.4 Placement summary

Per-mnemonic placement count (`nm libisa-core.so | rg -c 'Opcode_<mn>_Slot_'`): the
**producing** roots (MUL/ADD/SUB/MULSONE and their `.S`) carry **9** placements each; the
**accumulate/divide** roots (MADD/MSUB/MADDN/MSUBN/DIVN and their `.S`) carry **7** each — the denser
accumulate ops drop the two narrowest selector slots (`f1_s2_mul` and `f3_s3_alu`). The `t` twins match
their base count. Roll-up: `4×9 + 5×7 = 71` placements for the producing/accumulate/divide split per
spelling, doubled across vector+scalar and the `t` twins — all roll into the **1065-op vector axis**
plus the scalar `.S` set; **0** added to the integer-MAC batches (the reclamation, §0). `[HIGH/OBSERVED]`

---

## 3. Operand & state signature — read from the iclass tables

The authoritative per-op signature is the `Iclass_<MNEM>_args` (data operands) + `Iclass_<MNEM>_stateArgs`
(FCR/FSR/CPENABLE state) tables in `.data.rel.ro` (read at `VMA − 0x200000`). Each entry is a
`{char* name, uint64 dir}` pair; `dir` is the ASCII byte `i`=input, `o`=output, `m`=read-modify. The
data operands resolve to `opnd_ivp_sem_spfma_{vt,vs,vr}` (the group's own operand semantic), with
`vr = a` (multiplicand/minuend), `vs = b` (multiplier/subtrahend), `vt = c`/result.

**Data operands (`_args`):**

| op | `vt` | `vs` | `vr` | meaning |
|---|---|---|---|---|
| `IVP_MULN_2XF32` / `MUL_S` | `o` | `i` | `i` | producing: `vt = a·b` |
| `IVP_ADDN_2XF32` / `ADD_S` | `o` | `i` | `i` | producing: `vt = a+b` |
| `IVP_MULAN_2XF32` / `MADD_S` | **`m`** | `i` | `i` | accumulate: `vt += a·b` |
| `IVP_MULANN_2XF32` / `MADDN_S` | **`m`** | `i` | `i` | Newton accumulate (RNE) |
| `IVP_DIVNN_2XF32` / `DIVN_S` | **`m`** | `i` | `i` | divide (quotient in `vt`) |

**Control/status state (`_stateArgs`) — the per-op IEEE enable subset:**

| op | `RoundMode` | flags `m` (FSR) / enables `i` (FCR) | `CPENABLE` |
|---|---|---|---|
| `MUL_S` / `IVP_MULN_2XF32` | `i` | Invalid · Overflow · Underflow · Inexact | `i` |
| `ADD_S` / `SUB_S` / `IVP_ADDN_2XF32` | `i` | Invalid · Overflow · Inexact  *(no Underflow)* | `i` |
| `MADD_S` / `IVP_MULAN_2XF32` | `i` | Invalid · Overflow · Underflow · Inexact | `i` |
| `DIVN_S` / `IVP_DIVNN_2XF32` | `i` | Overflow · Underflow · Inexact  *(no Invalid)* | `i` |
| `MADDN_S` / `IVP_MULANN_2XF32` | **—** | **none** — only `CPENABLE` + operands | `i` |

> **QUIRK — the `N` (Newton) forms carry NO `RoundMode`, NO enables, NO flags.** Read straight from
> `Iclass_MADDN_S_stateArgs` / `Iclass_IVP_MULANN_2XF32_args`: the entire state list is
> **`CPENABLE/i`** plus the three `vt/m, vs/i, vr/i` operands. The `N` forms (`MADDN`/`MSUBN`/`MULANN`/
> `MULSNN`) are the **un-flagged, RNE-forced fast path** threaded through the DIVN/recip Newton loop:
> they neither read the dynamic `RoundMode` (they force round-to-nearest-even) nor post the IEEE sticky
> flags nor the imprecise-error edge. This matches the round core's gate `roundm = RoundMode ∧ ¬(maddn ∨
> msubn)` (the FMA round forces RNE for the negated-multiply forms). A reimplementation must **suppress
> all exception machinery** for the `N` ops — they are intermediate steps whose flags would be
> meaningless.

The IEEE exceptions are **imprecise**: every member additionally DEFs `VectorPipeImpreciseErr` (the
deferred vector-pipe fp-exception edge) at the late fp stage; the only **precise** exception is
`Coprocessor1Exception` (the `cp1`/`CPENABLE` gate sampled @stage 3, which squashes the op before any
datapath effect). See [the VFPU/IEEE exception model](../../uarch/vfpu-ieee.md) for the two-tier
precise/imprecise split. `[HIGH/OBSERVED]` structure; `[MED]` exact stage integers (§5).

---

## 4. Single-rounding value semantics — proven by live execution

The fp value math is **integer-only soft-float** (the host model cracks the IEEE fields and rounds in
integer ALU ops — it cannot trust the host x86's binary64 rounding to reproduce the device's binary32
tie-break). The `module__xdref_*` leaves in `libfiss-base.so` are the per-lane reference compute,
**callable in-process via ctypes with no license**. The decisive property — that the FMA rounds the
product `a·b` together with the addend `c` **exactly once** (not `round(round(a·b)+c)`) — was proven by
*executing* the real shipped `madd` leaf.

### 4.1 The single-rounding certificate (executed live)

The fp16 `madd` leaf `module__xdref_madd_1_1_1_1_16f_16f_16f_16f_2` @ `0x51dde0` is the self-contained
form (the fp32 `madd` leaf routes the wide multiply through a `mulpp` host callback that requires a live
`xstate`, so the fp16 leaf is the cleanly-drivable proof; both share the **identical** `fp_spfma_round`
single-round GRS core — the fp32 product is kept at `sig[26:0]`, wider than the 23-bit mantissa, exactly
the un-pre-rounded full-width product). The leaf computes the form `a + b·c`. ABI resolved by live probe:
`f(xstate, a, b, c, roundC, *out0…*out4)`, result in `out[4]`, `RoundMode = roundC & 3` (`0=RNE 1=RTZ
2=RU 3=RD`).

Driving it on cases where **single-round ≠ double-round** (a product `b·c` inexact in fp16, so rounding
it before the add diverges from rounding once on the exact sum):

```
a       b       c       live   single  double   live==single  live==double
0x3c00  0x3801  0x3801  0x3d01  0x3d01  0x3d00   True          False
0x3c01  0x3c01  0x3bff  0x4001  0x4001  0x4000   True          False
0x3c01  0x3c01  0x2e66  0x3c67  0x3c67  0x3c68   True          False
0x3c01  0x3801  0x4248  0x4125  0x4125  0x4126   True          False
0x3c01  0x3bff  0x3c01  0x4001  0x4001  0x4000   True          False
   (8 of 8 divergent cases: live == single-round, never double-round)
```

The live binary matched the **single-rounded** reference (round once on the exact `a + b·c`) in *every*
case and the **double-rounded** reference in *none*. This is the bit-exact certificate that the FMA is
genuinely fused — the product is **not** rounded before the add; there is exactly one rounding at the
FMA output. A two-step `c + round(a·b)` reimplementation would diverge here. `[HIGH/OBSERVED by execution]`

### 4.2 Round-mode and special values (executed live, fp32 add/sub)

The fp32 `add`/`sub` leaves (`module__xdref_add_1_1_1_32f_32f_32f_2` @ `0x871790`,
`…sub…_32f…` @ `0x872020`) are self-contained and were driven live — they exercise the same
`RoundMode` and IEEE special-value machinery the whole spfma group shares:

```
add32(1.0, 2.0)        = 0x40400000  (= 3.0)            basic fp32 datapath
add32(0.1, 0.2) RNE    = 0x3e99999a  (matches numpy fp32 RNE)
add32(+inf, -inf)      = 0x7fc00000  (qNaN — INVALID; +inf + -inf is invalid)
sub32(3.0, 1.0)        = 0x40000000  (= 2.0)
add32(MAX, MAX) RNE    = 0x7f800000  (overflow -> +inf)
add32(MAX, MAX) RTZ    = 0x7f7fffff  (same sum -> max-finite — overflow is ROUND-MODE-dependent)
```

The last pair is decisive: the **same** overflowing sum `MAX+MAX` resolves to `+inf` under RNE but to
the largest finite `0x7f7fffff` under RTZ — overflow direction is governed by the round mode (the
directed-round overflow: round-to-max-finite when the mode rounds *away* from the infinity). The
`+inf + -inf → qNaN` invalid case fires the invalid path. Every result is bit-exact from the shipped
binary.

### 4.3 The C reference model (annotated, naming the real symbols)

```c
// The shared fp32 FMA value function — one rounding on the exact product+addend.
// Producing forms pass c=0 (and never read vt); accumulate forms read vt as c.
// Names: the SEMANTIC is ivp_sem_spfma; the round core is fp_spfma_round (sig[26:0]).
// RM = RoundMode from the FCR; for the 'N' forms (MULANN/MULSNN) RM is forced to RNE.

float spfma_lane(float a /*=vr*/, float b /*=vs*/, float c /*=vt prior*/,
                 enum opclass op, int RM /*FCR RoundMode; 0=RNE 1=RTZ 2=RU 3=RD*/) {
    // 1) signs: op_negadd negates the PRODUCT leg for MSUB/MSUBN (the negate_axb_M path).
    //    SUB negates the ADDEND leg (b for ADD/SUB; here the 2nd operand).
    // 2) significand multiply: a 2x2 partial-product grid of the 24-bit significands
    //    (m00/m01/m10/m11), summed into the FULL product — NOT rounded here.
    extended product = significand_mul(a, b);          // s0: kept exact, sig[26:0]
    // 3) align the addend c to the product exponent and do ONE fused add (s1).
    extended sum = align_add(product, c);              // exact accumulation
    // 4) normalize + round ONCE under RM; emit the 4 IEEE sticky flags (s2 -> stage13).
    return round_once(sum, RM);                        // fp_spfma_round: GRS tie-to-even
    // 'N' forms force RM=RNE and suppress all flags (roundm = RM & ~(maddn|msubn)).
}

// Per opclass (the op_* mux the selector lights):
//   MUL  / mul.s        : op=mul,    c ignored,        d = round_RM(a*b)
//   ADD  / add.s        : op=add,    a ignored,        d = round_RM(a+b)   (a,b are the addends)
//   SUB  / sub.s        : op=add+negadd_addend,        d = round_RM(a-b)
//   MULSONE / mulsone.s : op=mul (op_mulsone path),    d = round_RM(a*b)   (recip-residual seed)
//   MADD / mulan / madd.s : op=madd,                   d = round_RM(a*b + c=vt)
//   MSUB / mulsn / msub.s : op=madd+negate_axb_M,      d = round_RM(c=vt - a*b)
//   MADDN/MSUBN ('N')     : as MADD/MSUB but RM=RNE, no flags (Newton step)
//   DIVN / divnn / divn.s : op=op_divn,                d = round_RM(a / b)  (seed+refine chain)
// 'T' twins: d[k] = pred[k] ? <above> : vt_prev[k]  (vbool per-lane writeback kill).
```

`[HIGH/OBSERVED]` the single round, RM-driven rounding, special-value algebra (proven by execution §4.1,
§4.2); `[HIGH/CARRIED]` the s0/s1/s2 tree decomposition and the `op_*` mux names (from the shared
INSTR_SCHEDULE DEF list, cross-referenced in [the VFPU/IEEE microarch](../../uarch/vfpu-ieee.md));
`[MED]` the exact MSUB `c−a·b` vs `a·b−c` polarity (the `negate_axb_M` toggle emits both directions;
the per-mnemonic polarity is the selector's job).

---

## 5. Timing — the 3-cycle FMA pipe

The per-op issue body in `libcas-core.so` stamps each operand/state access with its pipeline stage in
`%esi` before the access call. Read from `F0_F0_S3_ALU_36_inst_MUL_S_issue` and its peers,
the stage stamps are unambiguous: **`$0xa` (10)** = `vec` operand reads, **`$0xd` (13)** = `vec` result
write, **`$0xe` (14)** = FCR/FSR state-register samples.

```
MUL_S issue stamps:  8× mov $0xa,%esi  (operand reads @10)
                     3× mov $0xd,%esi  (result write @13)
                     8× mov $0xe,%esi  (FCR/FSR state @14)
```

So the fp32 FMA reads its `vec` operands **@stage 10**, lands the rounded result **@stage 13** (a
3-cycle result latency — 2 cycles deeper than the 1-cycle integer vec-ALU at stage 11), and the FSR
sticky flags / `*Enable` state ride **@stage 14**; the deferred `VectorPipeImpreciseErr` posts **@stage
15** (the 16-stage `*_inst_stage0..15` symbol set confirms the ~15-deep vector pipe). The per-op state-
sample count tracks the enable subset: `ADD_S` makes 11 state samples (no Underflow, but more FSR/flag
edges), `MUL_S` 8, `MADD_S`/`DIVN_S` 7, and the `N` form `MADDN_S` only 5 — consistent with the `N`
forms suppressing the flag machinery (§3). `[HIGH/OBSERVED]` the stage integers and the per-op stamp
counts; `[MED/CARRIED]` the @14/@15 flag/error stages (inherited from the shared schedule, cross-ref
[VFPU/IEEE §4](../../uarch/vfpu-ieee.md)).

> **NOTE — only the schedule-distinct ops carry a `*_issue` body; siblings alias it.** `MUL_S`,
> `ADD_S`, `MADD_S`, `MADDN_S`, `DIVN_S` and the `t`-vector forms have their own
> `F0_F0_S3_ALU_36_inst_<op>_issue`; `SUB_S`, `MSUB_S`, `MSUBN_S`, `MULSONE_S` have **no** separate
> issue body (each shares the issue/timing routine of the form with an identical schedule — `SUB_S`
> aliases `ADD_S`, `MSUB` aliases `MADD`, etc.) but **does** carry its own `<op>_stage_functions` table
> and `<op>_inst_stage0..15` compute bodies. Timing is shared; the per-stage value compute is
> distinct (from `nm libcas-core.so`).

---

## 6. Device-assembler oracle — byte-exact round-trip

Feeding the device-native `xtensa-elf-as` (`XTENSA_SYSTEM=…/ncore2gp/config`, `XTENSA_CORE=ncore2gp`,
GNU binutils 2.34.20200201 / Xtensa Tools 14.09) each op, then disassembling back. **16 forms** assemble
`rc=0` and round-trip to the same lowercase mnemonic with the correct `vec` operands and arity. The
assembler packs the minimal **N1** 8-byte bundle (the op rides `N1/S2/Mul`, with two `nop` fills in
S0/S1). Verbatim bundle words (the objdump 64-bit hex):

| assembled | bundle word | disasm | proves |
|---|---|---|---|
| `ivp_muln_2xf32 v7,v6,v5` | `00a54e098846352f` | `{ nop; nop; ivp_muln_2xf32 v7,v6,v5 }` | producing, 3 `vec` operands |
| `ivp_addn_2xf32 v7,v6,v5` | `00a54e08ea46352f` | `ivp_addn_2xf32 v7,v6,v5` | ADD |
| `ivp_subn_2xf32 v7,v6,v5` | `00a54e098c46352f` | `ivp_subn_2xf32 v7,v6,v5` | SUB |
| `ivp_mulan_2xf32 v7,v6,v5` | `00a54e090846352f` | `ivp_mulan_2xf32 v7,v6,v5` | accumulate (MADD), `vt` spelled |
| `ivp_mulann_2xf32 v7,v6,v5` | `00a54e08ee46352f` | `ivp_mulann_2xf32 v7,v6,v5` | MADDN ('N') |
| `ivp_mulsn_2xf32 v7,v6,v5` | `00a54e098a46352f` | `ivp_mulsn_2xf32 v7,v6,v5` | MSUB |
| `ivp_mulsonen_2xf32 v7,v6,v5` | `00a54e090c46352f` | `ivp_mulsonen_2xf32 v7,v6,v5` | MULSONE |
| `ivp_divnn_2xf32 v4,v6,v5` | `00a54808ec46352f` | `ivp_divnn_2xf32 v4,v6,v5` | DIVN |
| `ivp_addn_2xf32t v7,v6,v5,vb2` | `00254e080c46352f` | `ivp_addn_2xf32t v7,v6,v5,vb2` | **`t` twin, 4th operand `vb2` (`vbool`)** |
| `mul.s v7,v6,v5` | `00a54e09aa46352f` | `mul.s v7,v6,v5` | `.S` alias, **`vec` operands** |
| `add.s v7,v6,v5` | `00a54e08ce46352f` | `add.s v7,v6,v5` | `.S` ADD |
| `madd.s v7,v6,v5` | `00a54e098e46352f` | `madd.s v7,v6,v5` | `.S` MADD (`vt` spelled) |
| `maddn.s v7,v6,v5` | `00a54e090e46352f` | `maddn.s v7,v6,v5` | `.S` MADDN |
| `msub.s v7,v6,v5` | `00a54e09a846352f` | `msub.s v7,v6,v5` | `.S` MSUB |
| `mulsone.s v7,v6,v5` | `00a54e092a46352f` | `mulsone.s v7,v6,v5` | `.S` MULSONE |
| `divn.s v4,v6,v5` | `00a54808e846352f` | `divn.s v4,v6,v5` | `.S` DIVN |

Three structural facts the oracle pins:

* **The `.S` aliases take `vec` operands, not AR-scalar.** `mul.s v7,v6,v5` round-trips with `vec`
  registers — the `.S` spelling is a toolchain alias for the same 16-lane fp32 datapath, confirming §0.
* **The `t` twin's fourth operand is a `vbool`.** `ivp_addn_2xf32t v7,v6,v5,vb2` round-trips with the
  mask register spelled; the bundle's byte1 = `0x25` (vbr field set) vs the non-`t` `0xa5` — the
  encoding witness for the per-lane predicated writeback.
* **Sibling discrimination is purely the selector.** With operands held at `v7,v6,v5`, the XOR of each
  bundle against `ivp_muln_2xf32` lands **only** in the S2 selector field — `addn ^ muln = 0x…62000000`,
  `subn ^ muln = 0x…04000000`, `mulan ^ muln = 0x…80000000`, `divnn ^ muln = 0x0600…64000000`, and the
  vec/.S flip `muln ^ mul.s = 0x…22000000`. The `addn_2xf32t` XOR additionally sets byte1 (`0x00800000…`,
  the vbr field). This is the encoding-level proof of the single fully-multiplexed datapath +
  selector-indexed mux (§1). `[HIGH/OBSERVED]`

### 6.1 Worked F0/S3 bit pattern (the wide-bundle slot word)

To seat an op in a wide F0 bundle alongside three companions, the slot-local 35-bit word is built from
the F0/S3 field map (`sel[34:15]`, `vs[14:10]`, `vt[9:8]++[3:1]`, `vr[7:4]++[0]`). For `vt=v7, vs=v5,
vr=v6` (reconstructed and **verified bit-exact** against the thunk's `word0`):

```
ivp_muln_2xf32  (sel 0x1015c):  word0 = sel<<15 | vs<<10 | vt-split | vr-split = 0x80ae143e
ivp_addn_2xf32  (sel 0x10d4f):                                                  = 0x86a7943e
add.s           (sel 0x10d4e):                                                  = 0x86a7143e
   (addn ^ add.s = 0x00000800 — the selector bit0 vec/.S flip)
```

The encode thunk itself emits `word0 = sel<<15` with the operand fields left for the FLIX scatter:
`Opcode_ivp_muln_2xf32_Slot_f0_s3_alu_encode` is `movl $0x80ae0000,(%rdi)` (the selector `0x1015c<<15`)
+ `movl $0x0,0x4(%rdi)` (**word1 == 0** — the upper lane carries no selector bits for an S3_ALU op) +
`ret`. The full 128-bit bundle is produced by the FLIX slot-scatter get-fn.

---

## 7. Adversarial self-verification — the five strongest claims

1. **"The roster is exactly 27 — 18 vector (`ivp_*n_2xf32{,t}`) + 9 scalar (`.S`) — all `ivp_sem_spfma`."**
   The FMA-root encode thunks
   `Opcode_ivp_(muln|mulan|mulann|mulsn|mulsnn|mulsonen|addn|subn|divnn)_2xf32t?_Slot` = **18** distinct
   lowercase roots; the `.S` set `{add,sub,mul,madd,maddn,msub,msubn,mulsone,divn}_s` = **9**; total
   **27**. The `2xf32` min/max/compare/cvt ops carry `opnd_ivp_sem_vec_alu_*` / sp_cvt, **not** spfma —
   confirmed live (`IVP_MINN_2XF32` → `vec_alu`). **VERDICT: PASS** (matches the
   [partition table](template-and-partition.md)'s "B17 `ivp_sem_spfma` ~27" target).
   *Divergence noted for reconcile (§8).*

2. **"The F0/S3 selector for `ivp_muln_2xf32` is `0x1015c` (thunk `word0 = 0x80ae0000`)."**
   `34acd0: c7 07 00 00 ae 80  movl $0x80ae0000,(%rdi)`; `0x80ae0000 >> 15 = 0x1015c`. The full per-op
   table (§2) was read the same way; `addn 0x10d4f`, `mul.s 0x1019d`, `madd.s 0x1011d`, `maddn.s
   0x101fc`, `divnn 0x10d6f` all match. **VERDICT: PASS.**

3. **"The FMA is single-rounding — `round_RM(a·b+c)` once, not `round(round(a·b)+c)`."** Driven live:
   `madd16(a=1.0, b=1.0009766, c=1.0009766) = 0x3d01`; single-round ref `0x3d01`, double-round ref
   `0x3d00` — **live matches single** (8/8 divergent cases). **VERDICT: PASS** (proven by executing the
   shipped binary).

4. **"DIVN omits `InvalidEnable`; the `N` forms (MADDN/MSUBN) carry no RoundMode/enables/flags."** In the
   `stateArgs`: `DIVN_S` = `{Overflow,Underflow,Inexact}{Enable,Flag}` + RoundMode + CPENABLE,
   **no Invalid**; `MADDN_S` = **CPENABLE only** + the three `vt/m,vs/i,vr/i` operands. **VERDICT: PASS.**

5. **"Operands read @stage 10, result @stage 13 (3-cycle), state @stage 14."** In the disassembled
   `MUL_S_issue`: `8× mov $0xa,%esi` (reads @10), `3× mov $0xd,%esi` (write @13), `8× mov $0xe,%esi`
   (state @14). The 16-stage `*_inst_stage0..15` symbol set confirms the ~15-deep pipe. **VERDICT: PASS.**

---

## 8. Cross-page reconcile notes

* **Count divergence (resolved here authoritatively).** An earlier survey's headline prose said
  "19 `IVP_*N_2XF32` vector mnemonics … + 9 `.S`", which sums to 28, while its own roster list and
  ledger say **27**. The binary is unambiguous: **18** vector encode-thunk roots + **9** `.S` = **27**.
  This page pins **27 = 18 + 9**; the "19" is a transcription slip (it likely double-counted MULSONE
  or a `t` twin).
* **B04/B05 reclamation (resolved).** B05 §9 already correctly *defers* the "24 FP MAC forms" to
  B17/B18; this page reclaims the **fp32** half (the `*_2xf32` FMA roots) authoritatively as
  `ivp_sem_spfma`, with the `opnd_ivp_sem_spfma_*` + `RoundMode`/IEEE-state proof (§0 CORRECTION). The
  fp16 half is [B18](b18-hp-fma.md)'s. No member overlap with the integer-MAC batches.
* **Shared FMA tree with B18.** B17 (fp32, 16 lanes, sig[26:0]) and [B18](b18-hp-fma.md) (fp16, 32
  lanes) are the SP/HP peers of the **same** s0/s1/s2 VFPU tree at two significand widths
  ([VFPU/IEEE](../../uarch/vfpu-ieee.md)); the round core is bit-identical (the same `RoundMode`/GRS
  tie-to-even). The convert/round formal semantics are in
  [Formal Semantics I](../semantics/group-semantics-i.md); the cas/fiss FMAC modelling in
  [iss/cas-mac-fmac](../../iss/cas-mac-fmac.md); the value-differential proof in
  [validation/mac-multiply](../../validation/mac-multiply.md).
* **Partition routing.** The [template & partition classifier](template-and-partition.md) routes the
  fp32 FMA here by the `ivp_sem_spfma` semantic; the seed ops it chains are
  [B15](b15-sp-lookup.md)'s, the convert legs [B13](b13-sp-cvt.md)'s, the divide microcode
  [B23](b23-divide.md)'s.
