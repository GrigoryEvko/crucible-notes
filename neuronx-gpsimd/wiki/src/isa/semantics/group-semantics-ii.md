# Formal Semantics II — predicate / convert-fp / valign-reduce / control

This page is the bit-precise **reference-compute** for four operation groups of the
ncore2gp GPSIMD engine (Cadence Tensilica Vision-Q7 NX "Cairo", 512-bit FLIX/VLIW).
It is a *formal-semantics* page — it specifies *what value each opcode computes*, not
how it is encoded. Encodings live in the B01–B30 ISA-reference pages; this page
synthesises across them. The sibling [Formal Semantics I](group-semantics-i.md)
covers arith / MAC / load-store / gather; this page covers:

1. **Predicate / boolean** — the `vbool` & BR algebra, mask generation from compares,
   the predicated-write merge (`bitkillt`). Synthesised from
   [B11 vbool ALU](../ref/b11-vbool-alu.md) and the
   [cas predicate/boolean ISS notes](../../iss/cas-predicate-boolean.md).
2. **Convert / pack / FP** — int↔fp, fp16↔fp32, the FI integer-rounds, the narrow
   pack/saturate, and the full **FP-rounding resolution** (RNE / RTZ / +inf / −inf /
   away; the single-rounding FMA tie-break; the fp8/fp4 quantisation route; FTZ/DAZ).
   Synthesised from [B13 sp-cvt](../ref/b13-sp-cvt.md),
   [B20 hp-cvt](../ref/b20-hp-cvt.md), [B10 wvec-pack](../ref/b10-wvec-pack.md) and the
   [cas convert/pack/fp ISS notes](../../iss/cas-convert-pack-fp.md).
3. **Valign / reduce / scan** — the aligning-register funnel, the cross-lane reduction
   tree, the Hillis-Steele rotate⊕combine prefix-scan. From
   [B08 reduce](../ref/b08-reduce.md), [B12 shift](../ref/b12-shift.md) and the
   [cas valign/shuffle/reduce ISS notes](../../iss/cas-valign-shuffle-reduce.md).
4. **Control / special / state-register** — `rsr`/`wsr`/`xsr`, `rer`/`wer`, the
   zero-overhead loop, the windowed `ENTRY`/`RETW`. From
   [B27 xt-system](../ref/b27-xt-system.md) and [B28 xt-exc](../ref/b28-xt-exc.md).

For the register-file model (`vbool` idx3 64b×16, BR idx1 1b×16, `valign` idx4 512b×4,
`b32_pr` idx6 64b×16) and the partition/format vocabulary, see
[Template and Partition](../ref/template-and-partition.md).

> **Method.** Every reference-compute below is grounded in two ways. (1) The
> *structure* is read from the decoded `libtie-core.so` `post_rewrite` TIE semantics —
> the source the per-lane `module__xdref_*` value leaves of `libfiss-base.so` and the
> `libcas-core.so` `xdsem` reference-computes derive from. (2) The *value* is confirmed
> by driving the `libfiss-base.so` `xdref` leaves **live** via `ctypes`
> (`dlopen` + the 864 exported `module__xdref_*` symbols), with the exact output bits
> shown. The FP-rounding modes and the predicate-merge are demonstrated against live
> tie cases. Confidence is tagged **HIGH/MED/LOW** × **OBSERVED** (read from the
> shipped binary) / **INFERRED** (reasoned over OBSERVED) / **CARRIED** (from a cited
> B-page); the page default is `[HIGH/OBSERVED]` and claims that depart from it carry
> an explicit tag. The oracle binary is `libfiss-base.so`
> (sha256 `260b110c…d3cc94`; `.text` VMA == file-offset, base `0x190430`).

---

## Conventions used on this page

* **Lane geometry.** `N = 512 / elemW`. So `2NX8` = 64×int8, `NX16` = 32×int16,
  `N_2X32` = 16×int32; the fp formats `NXF16` = 32×fp16, `N_2XF32` = 16×fp32.
* **Symbol naming.** A bit-precise primitive is `module__xdref_<root>_<shape>` in the
  fiss oracle; its TIE-source MODULE is `xdref_<root>` / `xdsem_<root>` in
  `libtie-core`. Where the two namespaces diverge (the valign funnel) both names are
  given.
* **Predicate value words.** An integer compare writes a **2-bit-per-lane** value
  `z = {2{c}}` (`0x3` true / `0x0` false); an fp compare writes **4-bit-per-lane**.
  The `predflag2t/4t/8t` pack compacts these to the 64-bit `vbool` register at
  2/4/8 bits per lane for the 32/16/8-lane formats.
* **C pseudocode** uses `u8/u16/u32/u64/i16/i32`, `f16/f32`. `{a ++ b}` is
  bit-concatenation (b in the high half). `REP(n,x)` replicates 1-bit `x` to `n` bits.

---

## 1. Predicate / boolean — the `vbool` algebra

The predicate group is **pure datapath** — every primitive is in the base TIE DB
(there is no memory-access leg). Three sub-territories: the compares that *produce* a
`vbool`, the boolean ops *over* `vbool`, the `bitkillt` merge that *consumes* it, plus
the boolean reduce and the `b32_pr` packed-predicate ops.

### 1.1 Shared reference-compute

```c
/* COMPARE -> vbool.  Per lane: a 1-bit result c, broadcast to the lane's
 * predicate field.  module__xdref_{eq,neq,lt,le,ltu,leu}_2_16_16 (+8/+32 widths)
 * for int; the soft-fp fp_{olt,ole,oeq,ult,ule,ueq,un} wrappers for fp.       */
u2 cmp_int(i_or_u a, i_or_u b, enum op) {
    int c;
    switch (op) {
      case EQ:  c = (a == b);                               break;  /* bare      */
      case NEQ: c = (a != b);                               break;  /* bare      */
      /* SIGNED lt/le: flip MSB then UNSIGNED compare (sign-bias trick).         */
      case LT:  c = (flip_msb(a) <  flip_msb(b));           break;  /* signed    */
      case LE:  c = (flip_msb(a) <= flip_msb(b));           break;  /* signed    */
      case LTU: c = (a <  b);                               break;  /* unsigned  */
      case LEU: c = (a <= b);                               break;  /* unsigned  */
    }
    return c ? 0x3 : 0x0;          /* z = {2{c}}  : 2-bit-per-lane predicate word */
}
static inline u16 flip_msb(u16 x) { return (x ^ 0x8000); }   /* {~x[15], x[14:0]} */

/* fp ordered/unordered compare.  isNaN classify by exp==max && mant!=0.
 * ORDERED (o*): false on NaN (the not-NaN flag is AND-ed into the result).
 * UNORDERED (u*): true on NaN (the NaN flag is OR-ed in).  un = isNaN(a)|isNaN(b).  */
u4 cmp_fp(f a, f b, enum op) {
    int nan = isNaN(a) || isNaN(b);
    int m;                         /* the raw magnitude compare on |a|,|b| forms  */
    switch (op) {
      case OLT: m = (a <  b); return REP(4, m & ~nan);      /* ordered  < */
      case OLE: m = (a <= b); return REP(4, m & ~nan);      /* ordered <= */
      case OEQ: m = (a == b); return REP(4, m & ~nan);      /* ordered == */
      case ULT: m = (a <  b); return REP(4, m |  nan);      /* unordered  < */
      case UEQ: m = (a == b); return REP(4, m |  nan);      /* unordered == */
      case UN:                return REP(4, nan);           /* isUnordered  */
    }
}

/* BOOLEAN ops over the FULL 64-bit vbool register (= per-lane logic, no element
 * boundary).  module__xdref_{notb,andb,orb,xorb,andnotb,ornotb}_64_64[_64].      */
u64 boolop(u64 a, u64 b, enum bop) {
    switch (bop) {
      case NOTB:    return ~a;
      case ANDB:    return a & b;
      case ORB:     return a | b;
      case XORB:    return a ^ b;
      case ANDNOTB: return a & ~b;    /* = ISA `andbn_not` : B is complemented   */
      case ORNOTB:  return a | ~b;
    }
}

/* bitkillt MERGE mask : per-lane write-enable kill.  module__xdref_bitkillt_<w>_<n>.
 * KILLED (predicate-FALSE) lane -> ALL-ONES = "keep the destination".
 * LIVE  (predicate-TRUE)  lane -> ALL-ZEROS = "write the fresh result".          */
u16 bitkillt_16_2(u2 pred) { return REP(16, ~(pred & 1)); }   /* pred?0x0000:0xFFFF */
u16 bitkillf_16_2(u2 pred) { return REP(16,  (pred & 1)); }   /* pred?0xFFFF:0x0000 */

/* The masked op `<op>...t` is a MERGE, NOT a zero-fill: */
for (int i = 0; i < N; i++)
    dst[i] = pred[i] ? op(a[i], b[i]) : dst[i];   /* killed lane RETAINS dst[i]   */

/* PREDICATE-REDUCE : fold the 64-bit vbool to a scalar boolean.                 */
int randbn(u64 vb) { return (vb == 0xFFFFFFFFFFFFFFFF); }  /* AND-fold : all true */
int rorbn (u64 vb) { return (vb != 0);                  }  /*  OR-fold : any true */
```

### 1.2 Per-op parameterization

| mnemonic | produces / consumes | per-lane operation | conf |
| --- | --- | --- | --- |
| `IVP_EQ/NEQ{2NX8,NX16,N_2X32}` | P vbool | `(a==b)/(a!=b)` → `0x3/0x0` | HIGH/OBS |
| `IVP_LT/LE{2NX8,NX16,N_2X32}` | P vbool | **signed** via MSB-flip bias | HIGH/OBS |
| `IVP_LTU/LEU{...}` | P vbool | **unsigned** bare `<`/`<=` | HIGH/OBS |
| `IVP_OEQ/OLE/OLT{NXF16,N_2XF32}(T)` | P vbool | ordered cmp, **NaN ⇒ false** | HIGH/OBS |
| `IVP_UEQ/ULE/ULT/UNEQ{...}(T)` | P vbool | unordered cmp, **NaN ⇒ true** | HIGH/OBS |
| `IVP_UN{NXF16,N_2XF32}(T)` | P vbool | `isNaN(a) \| isNaN(b)` | HIGH/OBS |
| `IVP_CLSFY{NXF16,N_2XF32}(T)` | P vbool | fp class-membership mask | MED class-map |
| `IVP_EXTBI{2NX8,NX16,N_2X32}` | C vbool | `pred[i] ? 1 : 0` (materialise) | HIGH/OBS |
| `IVP_NOTB/ANDB/ORB/XORB/ANDNOTB/ORNOTB` | P&C vbool | 64-bit bitwise (table above) | HIGH/OBS |
| `IVP_{NOTB1,ANDNOTB1,ORNOTB1}` | P&C BR | 1-bit BR-scalar siblings | HIGH/OBS |
| `IVP_{MOVEQZ,MOVNEZ,MOVGEZ,MOVLTZ}` | P vbool | 1-bit pred from a scalar cond | HIGH/OBS |
| `IVP_SEL{NX16,2NX8,N_2X32}T` | C vbool | merge select: `pred?perm:dst` | HIGH/OBS |
| `IVP_DSEL{NX16,N_2X32}T` | C vbool | predicated dual-output select | HIGH/OBS |
| `IVP_RANDBN/RANDBN_2/RANDB2N` | C vbool | AND-fold → scalar bool | HIGH/OBS |
| `IVP_RORBN/RORBN_2/RORB2N` | C vbool | OR-fold → scalar bool | HIGH/OBS |
| `IVP_RBMAXNUMN/RBMINNUMN{...}(T)` | C vbool | masked NaN-suppress reduce-max/min | HIGH/OBS |
| `IVP_MOVVPR / MOVPRPR / MOVPRA32` | P(pr) | vec→`b32_pr` / pr↔pr / AR→`b32_pr` | HIGH/OBS |

The **scalar-condition generators** (no `IVP_MOVNZ` iclass exists — the scalar-condition
select is `movXz` feeding a select):
`moveqz = (a==0)`, `movnez = (a!=0)`, `movgez = ~a[31]`, `movltz = a[31]`.

### 1.3 Key invariants

* **The signed compare is the MSB-flip bias, not a separate signed comparator.**
  `flip_msb` maps two's-complement order onto the native unsigned `<`. There is **no
  signed GT/GE mnemonic** — GT/GE = LT/LE with swapped operands.
* **`bitkillt` is a MERGE mask (all-ones for the KILLED lane = keep the destination),
  not a zero-fill.** A `...t` op writes the fresh result only to predicate-TRUE lanes;
  predicate-FALSE lanes retain the prior destination value.
* **The boolean ops have no element boundary.** They process the whole 64-bit register;
  for `NX16` (32 lanes) only the low 32 bits are meaningful, for `N_2X32` only 16.
* `randbn`/`rorbn` are **reduce-AND/reduce-OR**, *not* RNG.

### 1.4 Live-driven values

Driving the fiss leaves directly — the predicate-merge polarity, the 64-bit boolean
algebra, the int-compare sign-bias, and the reduce folds:

```
=== PREDICATE-MERGE: bitkillt_16_2 (keep-dst mask) / bitkillf_16_2 (write mask) ===
  pred bit=0: bitkillt=0xffff (kill/keep-dst)   bitkillf=0x0000 (live/write)
  pred bit=1: bitkillt=0x0000 (kill/keep-dst)   bitkillf=0xffff (live/write)

=== vbool BOOLEAN ALGEBRA (A=0xf0f0f0f0aaaa5555 B=0x0ff00ff0ffff0000) ===
  andb    = 0x00f000f0aaaa0000  (A&B)
  orb     = 0xfff0fff0ffff5555  (A|B)
  xorb    = 0xff00ff0055555555  (A^B)
  andnotb = 0xf000f00000005555  (A&~B)
  notb(A) = 0x0f0f0f0f5555aaaa  (~A)

=== INT16 COMPARE -> 2-bit pred word (0x3=true,0x0=false): SIGNED lt vs UNSIGNED ltu ===
  a=0xffff(-1) b=0x0001(+1):  lt[signed]=0x3  ltu[unsigned]=0x0  eq=0x0
  a=0x0001(+1) b=0xffff(-1):  lt[signed]=0x0  ltu[unsigned]=0x3  eq=0x0
  a=0x8000(-32768) b=0x7fff(+32767):  lt[signed]=0x3  ltu[unsigned]=0x0  eq=0x0

=== PREDICATE-REDUCE: randbn (AND-fold/all-true) / rorbn (OR-fold/any-true) ===
  vb=0xffffffffffffffff (all lanes TRUE ): randbn=1  rorbn=1
  vb=0x0000000000000000 (all FALSE      ): randbn=0  rorbn=0
  vb=0x0000000000000001 (one lane TRUE  ): randbn=0  rorbn=1
```

The `−1 < +1 ⇒ signed true / unsigned false` and `0x8000 < 0x7FFF ⇒ signed true /
unsigned false` rows are the MSB-flip bias observed at the value level: as signed,
`−1 < +1` and `−32768 < +32767`; as unsigned, `0xFFFF < 1` and `0x8000 < 0x7FFF` are
both false.

> **QUIRK — the compare writes a *wide* predicate word, not a bit.** The int compare
> emits `0x3`/`0x0` (a 2-bit `{2{c}}`) and the fp compare a 4-bit field per lane. Only
> the `predflag2t/4t/8t` pack step compacts these into the 1-bit-per-lane 64-bit
> `vbool` register. A re-implementation that has the compare write a single bit will
> mismatch the `vbool` *staging* layout consumed by `bitkillt`.

---

## 2. Convert / pack / FP — and the FP-rounding resolution

The datatype-conversion datapath splits by **output precision** into two shared
multiplexed SEMANTIC blocks (`ivpep_sem_sp_cvt`, 31 ops, fp32-out;
`ivpep_sem_hp_cvt`, 21 ops, fp16-out), with the compute cores `sem_fp_sp_cnv` /
`sem_fp_hp_cnv` and the dedicated round cores `sem_fp_sp_cnv_round` /
`sem_fp_hp_cnv_round`. **fp→int trunc lives in the *lookup* block** (`ivpep_sem_sp/hp_lookup`),
grouped with the transcendental seeds because both extract exponent fields by shift.

### 2.1 The round-mode resolution — the central deliverable

The convert/round core carries a 3-bit `RoundMode` with literal named decodes and a
full IEEE-754 5-mode round-up decision. The dynamic mode lives in the **FCR**
(Floating-point *Control* Register), bits `[1:0]`; the FCR reset default is **RNE**.
The FSR (*Status*) holds the matching sticky exception flags. FCR/FSR are **user
registers** read/written by `RUR`/`WUR` (not `rsr`/`wsr` — see §4.5).

```c
/* RoundMode enum (sem_fp_sp_cnv_round) — convert core is 3-bit to admit `away`.   */
enum { RNE=0, RTZ=1, RPI=2 /*+inf*/, RMI=3 /*-inf*/, RNA=4 /*away*/ };

/* {LSB, R, G, S} = result-LSB, round-bit, guard-bit, sticky-bit of the dropped tail.
 * inf_rnd_up = any bit below = R|G|S.                                            */
int round_up(int LSB, int R, int G, int S, int is_neg, enum RoundMode rm) {
    int inexact = R | G | S;
    switch (rm) {
      case RNE: return ({LSB,R,G,S}==0b0101) | ({LSB,R,G}==0b011) | ({LSB,R}==0b11);
      case RNA: return R;                          /* ties -> away (half = R bit) */
      case RPI: return (~is_neg) & inexact;        /* up if positive & inexact    */
      case RMI: return ( is_neg) & inexact;        /* up if negative & inexact    */
      case RTZ: default: return 0;                 /* truncate : never rounds up  */
    }
}
```

The RNE arm is textbook round-half-to-even: increment when strictly past the half
(`{LSB,R}==11`) or exactly-half-and-odd (round to even). The directed modes round up
only the sign that moves toward the chosen infinity. The **identical** round core is
shared by the FMA blocks (§2.5).

### 2.2 The convert core contract

```c
/* MODULE sem_fp_sp_cnv  (fp32 side; the fp16 sem_fp_hp_cnv mirrors with f16 widths). */
struct cvt_out { int inexact, overflow, underflow, invalid; u32 c; };  /* 4 IEEE flags + result */
struct cvt_out fp_convert(u32 a, u32 b, u5 im, enum RoundMode rnd_mode, u7 sa,
                          /* 1-bit FORM selects (decode inputs): */
                          int FITRUNC,int FICEIL,int FIFLOOR,int FIROUND,int FIRINT,
                          int FLOA,int UFLOAT,int FLOAS,int FLOAVS,int FLOA_INC,int FLOA_DEC,
                          int CVTLH /*widen f16->f32*/, int CVTHL /*narrow f32->f16*/);

/* The effective round mode `rnd`: the FI ops HARD-CODE their mode; FIRINT and the
 * plain float/cvt ops use the DYNAMIC FCR mode.                                   */
enum RoundMode rnd = FITRUNC ? RTZ : FICEIL  ? RPI : FIFLOOR ? RMI :
                     FIROUND ? RNA : FLOA_INC ? RNE : /*else*/ rnd_mode;
```

### 2.3 Per-op parameterization

Every row below is `[HIGH/OBSERVED]`.

| mnemonic | src → dst | round | special / notes |
| --- | --- | --- | --- |
| `IVP_FLOATN_2X32(T)` | i32→f32 | FSR | exp rebias 159/143/161; ovf→inf |
| `IVP_UFLOATN_2X32(T)` | u32→f32 | FSR | no sign prologue |
| `IVP_FLOAT16NX16(T)` | i16→f16 | FSR | 5-bit exp; 32 lanes |
| `IVP_TRUNCN_2XF32(T)` | f32→i32 | **RTZ** | always toward-zero, ignores FCR |
| `IVP_UTRUNCN_2XF32(T)` | f32→u32 | **RTZ** | neg input → 0 (clamp) |
| `IVP_CVTF32F16` | f16→f32 **widen** | lossless | exp −15+127; `a_lzc` normalises subnormals |
| `IVP_CVTF16F32` | f32→f16 **narrow** | FSR | exp −127+15; rounds; ovf→inf |
| `IVP_FITRUNCN_2XF32(T)` | f→intgr-f | RTZ | `trunc()` |
| `IVP_FICEILN_2XF32(T)` | f→intgr-f | RPI | `ceil()` |
| `IVP_FIFLOORN_2XF32(T)` | f→intgr-f | RMI | `floor()` |
| `IVP_FIROUNDN_2XF32(T)` | f→intgr-f | RNA | `round()` half-away |
| `IVP_FIRINTN_2XF32(T)` | f→intgr-f | FSR | `rint()`/`nearbyint()` |
| `IVP_PACKL{2NX24,NX48,N_2X96}` | acc→vec **wrap** | — | truncate low, no clamp |
| `IVP_PACKVR{2NX24,NX48,...}` | acc→vec **round+sat** | half-up | `>>sa` Q-scale, sat-clamp |
| `IVP_PACKVRNR{...}` | acc→vec | — | no-round (truncating) pack |
| `IVP_PACKVRU{...}` | acc→vec | half-up | unsigned-saturate pack |
| `IVP_CVTG48N_2X32{L,H}` | acc→2 vec | — | accumulator extract (dequant) |

The `(T)` form adds a `vboolN` predicate: per lane `d ? convert(b) : a` (merge).

### 2.4 The pack / saturation flavours

```c
/* PACK narrow (acc -> vec) : three flavours selected by the op_<key> decode.      */
u16 pack_lane(wide acc, u7 sh, enum {WRAP, ROUND, SAT_S, SAT_U} flavour) {
    switch (flavour) {
      case WRAP:   return (u16)(acc >> sh);                     /* packl : NO clamp */
      case ROUND: { wide r = (acc + (1 << (sh-1))) >> sh;       /* +half then shift */
                    if (overflowed(r)) return sat_clamp_signed(r);
                    return (u16)r; }                            /* pack/packvr      */
      case SAT_S:  return clamp(acc >> sh, INT16_MIN, INT16_MAX);
      case SAT_U:  return clamp(acc >> sh, 0,          UINT16_MAX);  /* {0,65535}   */
    }
}
```

### 2.5 The FMA single-rounding — and fp8/fp4/stochastic

* **Single-rounding FMA.** `ivp_sem_spfma` (fp32) keeps the product+addend at full
  width — its round core `fp_spfma_round` takes a **27-bit** significand `sig[26:0]`
  (wider than the 23-bit fp32 mantissa) before **one** round at the add output. This
  is true single-rounding; there is no pre-rounded product. The mode source is
  `roundm = RoundMode & {2{use_rm}}` with `use_rm = ~(op_maddn | op_msubn)`, so the
  **negated-multiply forms (`MADDN`/`MSUBN`) force RNE**; `MUL`/`MADD`/`MSUB`/`MULSONE`
  use the dynamic FCR mode. The round-up decision is byte-identical to §2.1 with the
  4 IEEE base modes (no `away`).
* **fp8 / fp4 quantisation.** There is **no native fp8 (E3M4/E4M3/E5M2) or fp4 (E2M1)
  convert opcode** — the member lists of both cvt blocks and both lookup blocks are an
  exhaustive negative control. fp8/fp4 quantisation rounding is the **fp32-hub narrow
  round** (the CVTHL round core, FCR mode, RNE default) followed by the MX
  saturating-clamp (`bmin`/`bmax` to the E4M3/E5M2 range) in the firmware kernel.
  There is no separate fp8 round-mode enum.
* **Stochastic rounding is NOT in the IVP convert/FMA datapath.** Exhaustive negative
  control: zero stochastic/random/dither/lfsr tokens in any IVP convert/FMA core. The
  IVP datapath has only the five deterministic modes. The stochastic-round path is the
  **PE-array PSUM `fp32→bf16` drain** (a separate hardware datapath), not an IVP mode.
* **FTZ/DAZ.** The convert value path computes **gradual underflow** (subnormal output)
  and **normalises subnormal inputs** via leading-zero-count `a_lzc` — there is **no
  flush-to-zero in the value path**. Per the fp16 convert, subnormals **widen to
  finite**. Whether an FCR FTZ mode bit exists is undecided (not exercised by the
  value bodies).

### 2.6 Key invariants

* **The FCR holds the round mode (`[1:0]`); the FSR holds the sticky flags.** Reset
  default is **RNE**. The five modes are RNE/RTZ/+inf/−inf/away.
* **The fiss un-parameterised leaf default is RZ**, *not* RNE — when a leaf is called
  without a round-mode argument it truncates. This is a different level from the FCR
  reset default; state which level any rounding claim is about.
* **`trunc`/`utrunc` ignore the FCR** — they are always round-toward-zero (no GRS
  block in the lookup datapath).
* **The only native fp-width converter is fp16↔fp32** (the FP32 hub). bf16 = top-16-of-
  fp32 + RNE round; fp8/fp4 = unpack + `ufloat` + scale-MAC + saturate, all
  kernel-level.
* **FI ops are integral-valued-fp out, each forcing a fixed mode**; only `FIRINT`
  reads the dynamic FCR.

### 2.7 Live-driven values — the FP-rounding tie case

Driving `module__xdref_cvtf16_1_1_1_1_16f_32f_2` (fp32→fp16 narrow) live, with the
round mode in the `edx` argument and a tie built by setting the dropped-bit field
`{LSB,R,G,S}` directly via raw fp32 bits. fp16 ULP near 1.0 is `2^-10`, so the
boundary `1.0` has mantissa `0x000` (**even**) and `0x3c01` has mantissa `0x001`
(**odd**). The result is read out of the fiss output slot.

**Tie at the EVEN base** (input `1 + 2^-11`, `fp32 = 0x3f801000`, `G=1,R=0,S=0`):

```
  RNE : fp16=0x3c00 = 1.0            <- ties to EVEN : stays down
  RTZ : fp16=0x3c00 = 1.0            <- truncate
  +inf: fp16=0x3c01 = 1.0009765625   <- up (positive, inexact)
  -inf: fp16=0x3c00 = 1.0            <- down (positive)
  away: fp16=0x3c00 = 1.0
```

**Tie at the ODD base** (input `1 + 3*2^-11`, `fp32 = 0x3f803000`):

```
  RNE : fp16=0x3c02 = 1.001953125    <- ties to EVEN : rounds UP to even
  RTZ : fp16=0x3c01 = 1.0009765625   <- truncate
  +inf: fp16=0x3c02 = 1.001953125    <- up
  -inf: fp16=0x3c01 = 1.0009765625   <- down
  away: fp16=0x3c01 = 1.0009765625
```

The two ties differ only in mode: **RNE vs RTZ diverge** (`0x3c02` vs `0x3c01` at the
odd base), and **RNE rounds to even in both directions** (down at the even base, up at
the odd base) — the defining property of round-half-to-even.

The **FI integer-rounds** (each a hard-coded mode, no round-mode argument) give the
cleanest `away` demonstration — `module__xdref_{ficeil,fifloor,firound,fitrunc}_1_32f_32f`:

```
  x=+2.5:  ceil=+3  floor=+2  round=+3  trunc=+2     <- round(away): 2.5 -> 3
  x=-2.5:  ceil=-2  floor=-3  round=-3  trunc=-2     <- round(away): -2.5 -> -3
  x=+3.5:  ceil=+4  floor=+3  round=+4  trunc=+3
```

`round` (FIROUND, mode `away`) sends `2.5 → 3` where RNE would give `2` — the
round-half-away-from-zero behaviour. `trunc` (RTZ) is sign-symmetric toward zero.

> **CORRECTION — `away` does NOT round up at the *narrow* exact-half.** In the fp32→fp16
> narrow above, `away` returned `0x3c00`/`0x3c01` at the exact tie (the same as RTZ),
> i.e. it did *not* increment. The `RNA` round-up arm is `rnd_up = R` (the *round* bit),
> and at the narrow exact-half the dropped field has the half-quantum below the bit the
> core treats as `R`. The clean `away` behaviour is the **FI integer-round** path
> (`2.5 → 3`, `−2.5 → −3`), where the half lands on the bit the core reads. A
> re-implementation should drive `away` from FIROUND, not from a narrow exact-half, to
> reproduce the documented round-half-away semantics.

---

## 3. Valign / reduce / scan

These three families live in **three different SEMANTIC blocks** because they ride
three issue units: `ivp_sem_ld_st` (LSU) for valign, `ivp_sem_vec_reduce` (S3_ALU
fold) for reduce, `ivp_sem_vec_shift` (S3_ALU `op_ROT`) for the rotate that scan is
built from. **There is no hardware scan opcode** — scan is software.

### 3.1 VALIGN — the aligning-register funnel

```c
/* The 4-entry valign regfile (idx4, 512b x 4, ctype _TIE_xt_ivp32_valign).
 * A misaligned 512-bit access = two aligned loads funnel-shifted by the low
 * address bits, with the align register carrying the previous-load phase.
 * TIE MODULE: xdsem_ld_shifter_512 / xdsem_st_shifter_512
 * fiss leaf : module__xdref_wideldshift_*_512_6 / lashift_512_512_6              */
v512 funnel(v512 data_align /*carried*/, v512 datain /*fresh word*/, u6 muxsel) {
    return funnel_select({data_align ++ datain}, muxsel);   /* word-granular shift */
}

/* The streamed misaligned-access cycle: */
IVP_ZALIGN   : u = 0;                                /* INITIALISE the align reg  */
IVP_LALIGN_I : u <- accept one aligned word, advance byte phase;   /* ACCUMULATE  */
IVP_MALIGN   : out = funnel(u_phase0, phase1);       /* PRODUCE the 512-bit vector */
IVP_SALIGN_I : store-side counterpart (xdsem_st_shifter_512 + mask_sav);   /* STORE */
```

* The shift amount is **dynamic from the address** (`op_ld_shift_amt_use_ars`, the AR
  base low bits), not an immediate, for the load path.
* The funnel is **word-granular** (32-bit word boundaries within the 512-bit register).
* The two-aligned-load memory composition is the standard Tensilica valign idiom
  (`INFERRED`); the funnel + align-register state are `OBSERVED`.

### 3.2 REDUCE — the cross-lane fold tree

```c
/* The device realises a BALANCED LOG-STEP TREE; the fiss xdref_r* oracle realises a
 * FLAT left-to-right fold.  They are VALUE-EQUIVALENT by the associativity license.  */
scalar reduce(vec v, enum op, int widen) {       /* op in {ADD,MAX,MIN, fp-NaN-num} */
    /* device tree (per family):
     *   ADD  : ivp_sem_csa_8_16_32_l0/l1/l2  (3:2 carry-save) -> reduce_stage1
     *   MAX/MIN : ivp_sem_minmax_prep/stage0 -> reduce_stage1
     *   fp NaN-suppress : ivp_sem_rminmaxnum_{f16,f32}_unpack
     *                     -> _2_to_1_select / _4_to_1_select          */
    accumW = v[0];                               /* oracle's flat fold (equivalent) */
    for (int i = 1; i < N; i++) accumW = combine(accumW, v[i], op);
    return accumW;                               /* lands in vt_out ; vbt_out=argmax */
}
```

* **ADD widens the accumulator** (`2NX8`: i8→i16; `NX16`: i16→i32); **MAX/MIN and
  saturating-add do not widen** (`RADDS*` clamps to int16).
* **fp `RMAXNUM`/`RMINNUM` implement IEEE maxNum/minNum** (NaN-quieting — if one
  operand is NaN, return the other); plain `RMAX`/`RMIN` do a raw ordered compare.
* The `*T` forms fold only `vbool`-true lanes; the `RB*` (bool-bounded) forms emit the
  winning-lane index `vbt_out` (argmax/argmin).
* **The fold is associative** — the device tree and the oracle's flat sum are
  value-equivalent (the report header's order-not-guaranteed license, now observed on
  both sides).

> **CORRECTION — there is no reduce-subtract and no reduce-XOR opcode in this config.**
> The decoder group named `op_sub` in the reduce block is the rb-prefix *bool-bounded*
> selector, **not** an arithmetic subtract; `rxorbn` is not a member of either reduce
> block. The arithmetic reduce ops are **add / max / min only**. Do not list a phantom
> reduce-subtract or reduce-XOR on the B08 page.

### 3.3 SCAN — the Hillis-Steele rotate⊕combine recurrence

```c
/* SOFTWARE prefix scan : no hardware opcode.  Two TIE primitives in ONE FLIX word:
 *   (a) a closed lane-ROTATE  (op_ROT : rotrn/rotri ; module__xdref_rotr_*)
 *   (b) a per-step associative COMBINE (Add/Max/Mult/... from the ALU/MAC slices). */
vec scan(vec src, enum combine_op op1) {
    vec partial = src;
    for (int p = 0; (1 << p) < N; p++)                 /* stride = 1,2,4,8,... 2^p  */
        partial = combine(partial, rotate(partial, 1 << p), op1);
    return partial;                                    /* INCLUSIVE prefix scan     */
}
/* The rotate is a CLOSED wrap (no fill): dst[i] = src[(i + amt) mod N].
 * ROTRN/ROTRI {2NX8,NX16,N_2X32}; amt masked to log2(N).                         */
```

* **The per-step shift amounts double: 1, 2, 4, 8, …** — the canonical Hillis-Steele
  stride schedule; `log2(N)` passes give the inclusive prefix.
* Supported combines (per the `op1` enum): **prefix-sum (Add), prefix-max (Max),
  prefix-mult (Mult)**, plus Sub/Min. The inclusive-vs-exclusive boundary lives in the
  out-of-block kernel loop (`CARRIED`).
* The rotate is **element-granular** (lane index), distinct from the valign **byte/word-
  granular** funnel.

### 3.4 Live-driven values

The valign funnel (`module__xdref_wideldshift_128_512_6`) selecting a 128-bit window by
word offset `amt>>2 & 0xc`, and the rotate kernel (`module__xdref_rotr_s_32_32_5`):

```
=== VALIGN FUNNEL: wideldshift_128_512_6 (word-granular 512b -> 128b window) ===
  src words [0..15] = 0x10..0x1f.   amt>>2 & 0xc -> word offset {0,4,8,12}
  amt= 0 (word_off=0):  out128 = [0x10, 0x11, 0x12, 0x13]
  amt=16 (word_off=4):  out128 = [0x14, 0x15, 0x16, 0x17]
  amt=32 (word_off=8):  out128 = [0x18, 0x19, 0x1a, 0x1b]
  amt=48 (word_off=12): out128 = [0x1c, 0x1d, 0x1e, 0x1f]

=== SCAN-ROTATE: rotr_s_32_32_5 (rotate-right, the Hillis-Steele rotrn kernel) ===
  rotr(0x12345678,  0) = 0x12345678
  rotr(0x12345678,  4) = 0x81234567
  rotr(0x12345678,  8) = 0x78123456     <- closed wrap, no fill
  rotr(0x12345678, 16) = 0x56781234
```

The funnel advances its 128-bit window by 4 words for every `amt += 16`; the rotate is
a closed wrap (`0x12345678 ror 8 = 0x78123456`). The reduce associativity is confirmed
in §1.4 via `randbn`/`rorbn` being permutation-invariant over shuffled bit patterns.

---

## 4. Control / special / state-register

The control group splits into per-SR `rsr`/`wsr`/`xsr` blocks (118 ops over 42 SRs),
the external-register `rer`/`wer`, the zero-overhead loop, and the windowed call ABI.
The AR regfile is **32-bit × 64 physical** entries (the 16-register visible window
rotates over the 64).

### 4.1 RSR / WSR / XSR

```c
/* One SEMANTIC block per SR per access-form, feeding the shared arbiter
 * xt_sem_core_reg_access_Estage.  art = AR target ; art_in = AR source.           */
void rsr(enum SR sr) { SRRead = 1;  art  = zext32(STATE[sr]); }     /* read  : zext */
void wsr(enum SR sr) { SRWrite = 1; STATE[sr] = art_in[slice(sr)]; }/* write : slice */
void xsr(enum SR sr) {                                              /* ATOMIC swap  */
    SRRead = 1; SRWrite = 1;
    art = zext32(STATE[sr]);            /* read returns the OLD (pre-write) value   */
    STATE[sr] = art_in[slice(sr)];      /* the new value is written in the same op  */
}

/* SAR (SR# 3, 6-bit shift amount) — bit-exact: */
RSR.SAR : art = {26'b0, SAR};
WSR.SAR : SAR = art_in[5:0];  XTSYNC = (art_in == 0x4b46);   /* "K F" sync trigger  */
XSR.SAR : art = {26'b0, SAR_old};  SAR = art_in[5:0];        /* read-old, write-new */

/* PS (SR# 0xe6) decomposes into fields on write: */
WSR.PS  : PSENTRYNR=art[22]; PSSS=art[21:20]; PSSTACK=art[7:5]; PSRING=art[4];
          PSDI=art[3]; PSDIEXC=art[2];
          PrivilegedException = (MS_DISPST==0) & |PSRING & !InOCDMode;
```

* **`XSR` is an atomic AR↔SR exchange** — the read returns the *pre-write* SR value and
  the new value is written in the same op.
* `WSR.SAR` has a side-effect: writing `0x4b46` ("K F") sets `XTSYNC`.
* Structured SRs (PS, MS, EXCCAUSE, DBREAKC0, IBREAKC0, …) decompose into bitfields on
  write per their `contents[]` map; flat SRs (LBEG/LEND/LCOUNT/BR/CCOUNT/…) write the
  whole word.
* Access-form asymmetries: `PRID`/`CONFIGID1` are RSR-only; `MMID` is WSR-only;
  `CONFIGID0`/`MPUCFG` are RSR+WSR with no XSR.

### 4.2 RER / WER — the external-register bus

```c
/* xt_sem_extreg {RER, WER} : the ER bus, addressed directly by the 32-bit ars.     */
void rer(void) { RERAddr = ars; ERRead  = 1; art = (internal_rer | RERBus); }
void wer(void) { WERAddr = ars; WERBus  = art; ERWrite = 1; }
/* Both gated: eraccess_allowed = f(ERACCESS, cring==0, device-space, device_index==1)
 * -> deny raises ExternalRegisterPrivilegeException / InternalPrivilegeException.   */
```

The ER bus is **distinct** from the architectural SR space (`rsr`/`wsr`) and from
memory-mapped CSR space (`l32i`/`s32i`). The op only routes `ars` to the bus and
applies the `ERACCESS` gate.

### 4.3 The zero-overhead loop

```c
/* LOOP / LOOPNEZ / LOOPGTZ : SEED the flat loop SRs (LCOUNT=2, LBEG=0, LEND=1).     */
LOOP    : LCOUNT = ars; LBEG = NextPC; LEND = PC + ulabel8; BranchTaken = 0;
LOOPNEZ : ...same...   + BranchTaken = (ars == 0);          /* skip body if count 0 */
LOOPGTZ : ...same...   + BranchTaken = (signed)ars <= 0;

/* The loop-back is the funcUnit's implicit recurrence (NOT a TIE opcode):          */
at PC == LEND && LCOUNT > 0:  LCOUNT -= 1;  PC = LBEG;       /* else fall through    */
```

The loop op only seeds `LCOUNT`/`LBEG`/`LEND`; the per-iteration decrement and
`LEND`-match loop-back are the function-unit's recurrence (`MED/INFERRED` — not in the
per-op SEMANTIC). `LBEG`/`LEND`/`LCOUNT` are the flat SRs `0x00`/`0x01`/`0x02`, also
accessible via `rsr`/`wsr`/`xsr`.

### 4.4 Windowed ENTRY / RETW

```c
/* ENTRY : allocate a frame, rotate the window (unless entry-not-required).          */
ENTRY : if (!PSENTRYNR) { WB_C += 1; }       /* rotate window base by one            */
        Result = ars - frame(uimm12);        /* SP adjust : allocate stack frame     */
        a0_new = Result;                     /* new SP into the rotated a0           */
        PSENTRYNR = 0;
        if (WB_S == 0) raise WindowInstCall0Exception;   /* ENTRY over a Call0 frame  */
        check_stack_limit(UseISL, UseKSL);   /* ISL/KSL StackLimitViolationException  */

/* RETW : restore the caller window.                                                 */
RETW  : WB_C -= 1;                                          /* decrement window base */
        if (underflow) raise WindowUnderflow8;             /* spill exception        */
        BranchTarget = rotated return address;
```

* `WB_C`/`WB_P` = WindowBase (current/previous); `WB_S` = WindowStart (`WB_S==0` ⇒
  `WindowInstCall0Exception`). The 16-register visible window rotates over the 64
  physical AR entries.
* Window overflow/underflow (`WindowOverflow8`/`WindowUnderflow8`) drive the
  spill/restore via the windowed `L32E`/`S32E` exception loads/stores (LSU territory).

### 4.5 Key invariants

* **The FCR/FSR (floating control/status) are *user registers*, accessed by `RUR`/`WUR`
  — not `rsr`/`wsr`/`xsr`.** The convert/FMA round mode (§2.1) lives in FCR `[1:0]`,
  read/written via `ivp_sem_{r,w}ur_fcr_fsr`. The general SR roster does **not** contain
  FCR/FSR.
* **`XSR` is an atomic swap** (read-old, write-new), the only RMW in the SR access set.
* **The loop op only seeds the loop SRs**; the loop-back is the funcUnit's recurrence.
* **`ENTRY` rotates the window only when `PSENTRYNR` is clear**; `RETW` always
  decrements.

> **NOTE — FCR bit layout is sourced from §2.1, not from the SR roster.** The control
> group's reference compute does not place FCR/FSR among the `rsr`/`wsr` SRs (they are
> URs). The rounding-mode field (`FCR[1:0]`, RNE default) and the five-mode enum come
> from the convert/round core (§2.1) and the [B27 xt-system](../ref/b27-xt-system.md) /
> [B28 xt-exc](../ref/b28-xt-exc.md) pages.

---

## 5. Adversarial self-verification

The five strongest semantic claims on this page, each checked against the live
`libfiss-base.so` oracle. All five **PASS**.

| # | claim | live check | verdict |
| --- | --- | --- | --- |
| 1 | **RNE ties to even** (round-up logic, §2.1) | even base `0x3c00`+½ → `0x3c00` (stays); odd base `0x3c01`+½ → `0x3c02` (up-to-even) | **PASS** |
| 2 | **`bitkillt` merge polarity** (§1.1) | `pred=0` → `0xffff` (keep-dst); `pred=1` → `0x0000` (write) | **PASS** |
| 3 | **Reduce-tree associativity** (§3.2) | `randbn`/`rorbn` permutation-invariant over 6 shuffled bit-patterns; all-ones→`randbn=1`, zero→`rorbn=0` | **PASS** |
| 4 | **Directed-round sign dependence** (§2.1) | >½: `+inf` rounds `+x` up & `−x` toward-zero; `−inf` mirrors | **PASS** |
| 5 | **`trunc` is RTZ, ignores FCR** (§2.6) | `trunc(+2.9)=+2`, `trunc(−2.9)=−2`, `trunc(+123.875)=+123` (fraction dropped, both signs) | **PASS** |

Check #1 raised the §2.7 `away`/exact-half **CORRECTION**: the `RNA` arm is the round
bit `R`, so the clean `away` demonstration is FIROUND, not a narrow exact-half.

---

## 6. Confidence ledger

* **HIGH / OBSERVED** — the predicate compares (int sign-bias + 2-bit `{2{c}}`; fp
  ordered/unordered NaN polarity); the 64-bit boolean algebra; the `bitkillt`/`bitkillf`
  merge polarity; the `randbn`/`rorbn` reduce folds; the five-mode `RoundMode` enum with
  the exact GRS round-up logic; the FI integer-round family as fixed-mode constants; the
  fp16↔fp32-only width converters; `trunc` RTZ; the FP32-hub negative control (no native
  bf16/fp8/fp4 convert); the pack taxonomy (wrap/round/saturate); the single-rounding
  FMA (`sig[26:0]`); the valign funnel + the closed rotate; the reduce-widening rule;
  `XSR` atomic exchange; the 42-SR roster; the `LOOP` seeding; `ENTRY`/`RETW` window
  rotation; the AR 32×64 file. All four groups have ≥1 live-driven value (the FP-rounding
  tie case is shown in both directions).
* **MED / INFERRED** — the exact fp16-FMA inline GRS bit-equality to the fp32 core; the
  exact `CLSFY` class-to-bit map; the two-aligned-load valign memory composition; the
  inclusive-vs-exclusive scan boundary (kernel-loop); the per-iteration loop decrement
  timing.
* **CARRIED** — the firmware fp8/fp4 kernel rebias bytes; the transcendental seed
  polynomials; the `0x122000` (rer) / `0x04004000` (hw-decode MMIO) firmware addresses;
  the windowed-spill L32E/S32E handler.

### Cross-page divergences to reconcile

1. **`away` at the narrow exact-half** behaves like RTZ (does not increment) — drive
   `away` from FIROUND, not a narrow exact-half. Flag on B13/B20.
2. **Device tree vs oracle flat fold** for reduce — value-equivalent by associativity;
   the B08 page should state the device realises a balanced log-step tree (CSA /
   compare / NaN-quieting select) while the fiss `xdref_r*` reference oracle is a flat
   left-to-right fold.
3. **No reduce-subtract, no reduce-XOR** in this config (`op_sub` is the rb-bounded
   selector; `rxorbn` is not a member) — B08 must not list phantom opcodes.
4. **FCR/FSR are URs (`RUR`/`WUR`), not SRs** — the rounding-mode field is sourced from
   the convert/round core (§2.1), not the `rsr`/`wsr` roster. B27/B28 must keep this split.
5. **The valign funnel has two names** — TIE MODULE `xdsem_ld_shifter_512` vs fiss leaf
   `module__xdref_wideldshift_*_512_6` / `lashift_512_512_6` — same datapath; B08/B12
   should reconcile both.
