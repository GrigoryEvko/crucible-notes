# ISA Batch 02 — Vector ALU (fp16 / fp32 slice)

This is the per-instruction reference for the **floating-point Vector-ALU slice** of the
Vision-Q7 *Cairo* (`ncore2gp`) ISA: the binary-arithmetic, comparison, and sign-manipulation
opcodes that operate on the `vec` 512-bit data file in fp16 (`NXF16`, 32 lanes) and fp32
(`N_2XF32`, 16 lanes). It owns **52 shipped mnemonics** — `abs`/`neg`, `add`/`sub`/`mul`,
`addexp`/`addexpm`, `min`/`max`/`minnum`/`maxnum`, the ten ordered/unordered compares
(`oeq`/`ole`/`olt`/`ueq`/`ule`/`ult`/`uleq`/`ultq`/`uneq`/`un`), and `clsfy` (classify) — in both
precisions, summing to **572 placements** of the certified-perfect `12569` cover
([coverage tally](../core/coverage-tally.md)).

This page is the **value-semantics-and-encoding** companion to the control-view
[fp sub-ISA page](../core/fp-sub-isa.md): that page owns the FCR/FSR round-mode/exception model
(the two-level RNE/RZ rounding, the `SHARED_OR` sticky flags, the CLSFY 8-bit class mask); this
page owns *what each fp-ALU opcode computes, bit-exact, and how each one is encoded into a slot*.
The fused multiply-add families (`mula*`/`muls*`/`madd*`/`mulsone`/`maddn`) are **not** here —
they live in [B17 fp32 FMA](b17-spfma.md) / [B18 fp16 FMA](b18-hp-fma.md); the tree-reduction
min/max forms (`rmaxnum`/`rminnum`/`rbmaxnum`/`rbminnum`) are **not** here either — they reduce a
whole vector and belong to [B08 reduce](b08-reduce.md); the round/convert family
(`firound`/`ficeil`/`fifloor`/`fitrunc`/`firint`/`trunc`) is [B13](b13-sp-cvt.md)/B20; and the
transcendental seeds (`recip0`/`rsqrt0`/`sqrt0`/`nexp0`/`div0`/`divn`) are
[B14](b14-hp-lookup.md)/[B15](b15-sp-lookup.md)/B23. The §"Batch boundary" table pins every
near-neighbor.

Everything below is grounded two ways: the **opcode-selector immediates** are read straight from
the `Opcode_<mnem>_Slot_<slot>_encode` thunk bodies in the non-stripped `libisa-core.so`
(`movl $imm,(%rdi); ret`); the **value semantics** are *proven by execution* — every fp claim on
this page was computed by calling the matching `module__xdref_` leaf in `libfiss-base.so` live via
`ctypes` (license-free), the binary acting as the arbiter of its own arithmetic. `[HIGH/OBSERVED]`
throughout (by-execution where a value fact is stated), per the
[confidence model](../../reference/confidence-model.md).

---

## 1. Scope, datapath, and key facts

The fp Vector-ALU ops all read their sources from the `vec` file `@10`, run the
**1-cycle ALU latency class** (writeback to `vec` `@11` for the arithmetic forms), and — for the
compares — write a boolean lane into `vbool` (idx 3) or an AR-bit, per the
[register-file model](../core/register-files.md). They honor the FCR round mode where rounding can
occur (`add`/`sub`/`mul`) and accumulate the five `SHARED_OR` FSR sticky flags where an IEEE
exception can fire ([fp sub-ISA §5](../core/fp-sub-isa.md#5-the-ieee-754-conformance-model)).

| Fact | Value | Source |
|---|---|---|
| Data file | `vec` idx 2, **512 b × 32**; fp16 = 32 lanes, fp32 = 16 lanes | [register-files §3](../core/register-files.md) |
| Compare result file | `vbool` idx 3 (64 b × 16) — one mask bit per sub-lane | [register-files §3](../core/register-files.md) |
| Precision tokens | `nxf16` (fp16 1-5-10 bias 15) · `n_2xf32` (fp32 1-8-23 bias 127) | mnemonic suffix |
| Mnemonics this batch | **52** (26 fp16 + 26 fp32) | `nm libisa-core.so \| rg -c` |
| Placements this batch | **572** | per-mnemonic census (§7) |
| Round-honoring ops | `add`/`sub`/`mul` (FCR `RoundMode[1:0]`) | leaf takes round as a parameter (§3) |
| Non-rounding ops | `abs`/`neg`/`min`/`max`/`*num`/compares/`addexp`/`clsfy` | no round arg in leaf ABI |
| FSR-raising ops | `add`/`sub`/`mul` (Invalid/Overflow/Inexact); signaling compares (Invalid) | live flag-field probe (§4–§5) |
| Value oracle | `libfiss-base.so` `module__xdref_*` leaves, ctypes | [confidence §6.1](../../reference/confidence-model.md) |
| Canonical encode slot | `F1_S3_ALU` (every fp-ALU op is placed there) | §2 |

> **GOTCHA — there is NO native `copysign` opcode in this slice.** A search of the full 1065-op
> `ivp_*` roster for `copysign`/`csign`/`signinj` returns **zero** (`rg -i copysign /tmp/all_mnem`
> = 0). Sign manipulation is exactly the `abs` (clear bit 15) / `neg` (toggle bit 15) primitives
> (§4.1) plus the `vbool`/bitwise-int ops in [B01](b01-vec-alu-int.md)/[B11](b11-vbool-alu.md): a
> reimplementer synthesizes `copysign(a,b)` as `abs(a)` OR-ed with `(b & 0x8000)` (an int-domain
> bit op, *not* an fp-ALU op). Do not invent a `copysign` mnemonic from a higher-level lowering —
> it is named-but-never-shipped at the ISA level. `[HIGH/OBSERVED]` (member-LIST negative control,
> mirrors the [fp32-hub negative control](../core/fp-sub-isa.md#6-the-fp-datatype-set)).

---

## 2. The roster

Every fp-ALU mnemonic this batch owns, with its encoding read at the **canonical `F1_S3_ALU`
slot** (the one slot that hosts the whole family). The `opcode-sel imm` is the `movl` immediate in
the `Opcode_ivp_<mnem>_Slot_f1_s3_alu_encode` thunk; the `byte-size` is the FLIX format length the
op is placed into (16 B for the wide formats `F0/F1/F2/F3/F7`, 8 B for the narrow `N0/N1/N2`). The
`vec field positions` are the per-slot operand placements deposited by `field_set` *after* the
selector template — see [FLIX encoding §6](../core/flix-encoding.md); they are slot-local, so this
column states the operand **arity/role**, not absolute bit offsets (which differ per slot).

> **GOTCHA — the opcode-selector immediate is SLOT-LOCAL, not a global opcode number.** The same
> mnemonic carries a *completely different* selector in each slot it is legal in. `addnxf16` is
> `0x25d10000` in `F1_S3_ALU` but `0x86bf0000` in `F0_S3_ALU`, `0x03900004` in `F2_S2_Mul`,
> `0x01970000` in `F7_S2_Mul`, and `0x00db8000` in `N1_S2_Mul` (all byte-exact this pass). There is
> **no** slot-invariant "fp-add opcode"; the iclass is invariant, the per-slot packing is not. This
> page tabulates one canonical slot so the selectors are comparable; a reimplementer reads the
> placement it actually targets from `Opcode_<mnem>_Slot_<that-slot>_encode`. `[HIGH/OBSERVED]`

### 2.1 fp16 (`nxf16`) — 32-lane

| mnemonic | FLIX fmt·slot | opcode-sel imm (F1_S3) | vec field roles | byte-size | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_absnxf16` | F1·S3_ALU (+12 slots) | `0x2ba80306` | `a=vec(o), b=vec(i)` | 16/8 | `|x|` — clear sign bit, NaN/Inf preserved | H/OBS·exec |
| `ivp_negnxf16` | F1·S3_ALU (+12) | `0x25ba0202` | `a=vec(o), b=vec(i)` | 16/8 | `−x` — toggle sign bit (incl. ±0, NaN) | H/OBS·exec |
| `ivp_addnxf16` | F1·S3_ALU (+8) | `0x25d10000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | `b + c`, RNE/FCR rounded | H/OBS·exec |
| `ivp_subnxf16` | F1·S3_ALU (+8) | `0x26210000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | `b − c`, FCR rounded | H/OBS·exec |
| `ivp_mulnxf16` | F1·S3_ALU (+8) | `0x25f10000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | `b × c`, single-round (GRS) | H/OBS·exec |
| `ivp_addexpnxf16` | F1·S3_ALU (+5) | `0x25820202` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | scale: `exp_a += (exp_c − bias)` | H/OBS·exec |
| `ivp_addexpmnxf16` | F1·S3_ALU (+5) | `0x2be80306` | `a=vec(o), b=vec(i)` | 16/8 | `addexp` minus-variant (sign/scale) | H/OBS |
| `ivp_maxnxf16` | F1·S3_ALU (+11) | `0x24eb0000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | NaN-**asymmetric** max (b-NaN wins) | H/OBS·exec |
| `ivp_minnxf16` | F1·S3_ALU (+11) | `0x2a0b0000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | NaN-asymmetric min | H/OBS·exec |
| `ivp_maxnumnxf16` | F1·S3_ALU (+11) | `0x24e98000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | IEEE-754 `maxNum` (number wins) | H/OBS·exec |
| `ivp_minnumnxf16` | F1·S3_ALU (+11) | `0x24ee8000` | `a=vec(o), b=vec(i), c=vec(i)` | 16/8 | IEEE-754 `minNum` (number wins) | H/OBS·exec |
| `ivp_oeqnxf16` | F1·S3_ALU (+13) | `0x27054800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | ordered `==` (quiet on qNaN) | H/OBS·exec |
| `ivp_olenxf16` | F1·S3_ALU (+13) | `0x27064800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | ordered `<=` (**signaling**) | H/OBS·exec |
| `ivp_oltnxf16` | F1·S3_ALU (+13) | `0x27074800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | ordered `<` (**signaling**) | H/OBS·exec |
| `ivp_ueqnxf16` | F1·S3_ALU (+13) | `0x27044c00` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered `==` (quiet) | H/OBS·exec |
| `ivp_ulenxf16` | F1·S3_ALU (+13) | `0x27054c00` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered `<=` (quiet) | H/OBS·exec |
| `ivp_ultnxf16` | F1·S3_ALU (+13) | `0x27074c00` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered `<` (quiet) | H/OBS·exec |
| `ivp_uleqnxf16` | F1·S3_ALU (+13) | `0x27064c00` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered `<=` (**signaling** `q`) | H/OBS·exec |
| `ivp_ultqnxf16` | F1·S3_ALU (+13) | `0x27804800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered `<` (**signaling** `q`) | H/OBS·exec |
| `ivp_uneqnxf16` | F1·S3_ALU (+13) | `0x27814800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | `!=` or unordered (quiet) | H/OBS·exec |
| `ivp_unnxf16` | F1·S3_ALU (+13) | `0x27824800` | `a=vbool(o), b=vec(i), c=vec(i)` | 16/8 | unordered (either is NaN, quiet) | H/OBS·exec |
| `ivp_clsfynxf16` | F1·S3_ALU (+8) | `0x25920202` | `a=vec(o), b=vec(i)` | 16/8 | 8-bit class mask (see fp sub-ISA §5.4) | H/OBS·exec |

### 2.2 fp32 (`n_2xf32`) — 16-lane

The fp32 forms are the **same opcodes at the same slot**, distinguished by a slot-local precision
bit (§3.3). Selectors at `F1_S3_ALU` (byte-exact this pass):

| mnemonic | opcode-sel imm (F1_S3) | precision-bit Δ vs fp16 | conf |
|---|---|---|---|
| `ivp_absn_2xf32` | `0x2bc80306` | `0x2ba8`→`0x2bc8`: byte2 bit 5 (`+0x20`) | H/OBS·exec |
| `ivp_negn_2xf32` | `0x25ca0202` | `0x25ba`→`0x25ca`: byte2 bit 4 (`+0x10`) | H/OBS·exec |
| `ivp_addn_2xf32` | `0x25d90000` | `0x25d1`→`0x25d9`: byte2 bit 3 (`+0x08`) | H/OBS·exec |
| `ivp_subn_2xf32` | `0x26290000` | `0x2621`→`0x2629`: byte2 bit 3 (`+0x08`) | H/OBS·exec |
| `ivp_muln_2xf32` | `0x25f90000` | `0x25f1`→`0x25f9`: byte2 bit 3 (`+0x08`) | H/OBS·exec |
| `ivp_addexpn_2xf32` | `0x258a0202` | `0x2582`→`0x258a`: bit 3 | H/OBS |
| `ivp_maxn_2xf32` | `0x24ec0000` | `0x24eb`→`0x24ec` (group remap) | H/OBS·exec |
| `ivp_minn_2xf32` | `0x2a1b0000` | `0x2a0b`→`0x2a1b`: byte2 bit 4 | H/OBS·exec |
| `ivp_maxnumn_2xf32` | `0x24ea0000` | `0x24e98000`→`0x24ea0000` | H/OBS·exec |
| `ivp_minnumn_2xf32` | `0x24ef0000` | `0x24ee8000`→`0x24ef0000` | H/OBS·exec |
| `ivp_oeqn_2xf32` | `0x2705c800` | `0x4800`→`0xc800`: byte2 bit 7 (`+0x80`) | H/OBS·exec |
| `ivp_olen_2xf32` | `0x2706c800` | byte2 bit 7 | H/OBS·exec |
| `ivp_oltn_2xf32` | `0x2707c800` | byte2 bit 7 | H/OBS·exec |
| `ivp_ueqn_2xf32` | `0x2704cc00` | byte2 bit 7 | H/OBS·exec |
| `ivp_ulen_2xf32` | `0x2705cc00` | byte2 bit 7 | H/OBS·exec |
| `ivp_ultn_2xf32` | `0x2707cc00` | byte2 bit 7 | H/OBS·exec |
| `ivp_uleqn_2xf32` | `0x2706cc00` | byte2 bit 7 | H/OBS·exec |
| `ivp_ultqn_2xf32` | `0x2780c800` | byte2 bit 7 | H/OBS·exec |
| `ivp_uneqn_2xf32` | `0x2781c800` | byte2 bit 7 | H/OBS·exec |
| `ivp_unn_2xf32` | `0x2782c800` | byte2 bit 7 | H/OBS·exec |
| `ivp_clsfyn_2xf32` | `0x259a0202` | `0x2592`→`0x259a`: bit 3 | H/OBS·exec |

`ivp_addexpmn_2xf32` = `0x2bf80306`. The `oeqn_2xf32`=`0x2705c800` value matches the
[FLIX encoding §6.2](../core/flix-encoding.md) spot-check byte-for-byte (independent re-read).

> **GOTCHA — the fp16/fp32 precision bit is NOT a single global bit; its position varies by
> opcode group.** Across the compare group it is **byte2 bit 7** (`0x48`→`0xc8`, `+0x80`), a clean
> roster-wide step; but for `add`/`sub`/`mul` it is **byte2 bit 3** (`+0x08`), for `neg` byte2 bit
> 4, for `abs` byte2 bit 5, and the min/max group is a small selector remap rather than a single
> bit-flip. This is the same finding as the FLIX page's
> [refuted "u-bit/lane/fp-pred global triad"](../core/flix-encoding.md#62-the-two-tier-selector-model):
> precision is a *format-local enumerated field*, realized as a distinct opcode per precision, not
> one bit you can OR in. A reimplementer's assembler must select the precision *opcode*, never
> flip a fixed bit. `[HIGH/OBSERVED]`

---

## 3. The arithmetic ops — `add` / `sub` / `mul` (the round-honoring core)

These are the only fp-ALU ops that round, so they are the only ones that take the FCR `RoundMode`
as a parameter and the only ones that can raise Overflow/Underflow/Inexact. Their value leaves
(`module__xdref_add_1_1_1_16f_16f_16f_2` @ `0x51c640`, `sub_…` @ `0x51ce70`,
`mul_1_1_1_1_16f_16f_16f_2` @ `0x51d640`) are the same integer-only soft-float bodies the
[fp sub-ISA page](../core/fp-sub-isa.md#8-the-soft-float--hardware-fp-reconciliation) proved carry
zero hardware-FP x86 instructions: they crack the binary16 fields with `0x7c00`/`0x3ff`/`0x1f`
masks and round in the integer ALU with the round mode as the **4th argument** (`%ecx`).

### 3.1 The value-leaf calling convention (recovered by disassembly + execution)

The fp value leaves do **not** return a scalar; they take a leading scratch arg, the input
value(s), the round mode (where applicable), and *one output pointer per status/result field*. The
register/stack layout was read from each leaf's prologue and **confirmed by execution** — the fp
result lands in the **last** pointer argument:

```c
// add / sub  (8 args; result in arg-8):
//   void add(long /*scratch, unused*/, uint a, uint b, uint round_mode,
//            uint *flagfield0, uint *flagfield1, uint *flagfield2, uint *result);
// mul         (9 args; one extra status field; result in arg-9):
//   void mul(long, uint a, uint b, uint round_mode,
//            uint *f0, uint *f1, uint *f2, uint *f3, uint *result);
```

The disassembly of `add` shows the prologue saving `%r8`→`-0x28(%rsp)`, `%r9`→`-0x20(%rsp)`, the
round mode `%ecx`→`-0x3c(%rsp)`, and two further stack-passed pointers reloaded from `0x38(%rsp)`
/ `0x40(%rsp)`; the final four stores write the four output fields, the fp **result** going to the
last (`mov %r15d,(%rax)` where `%rax = 0x40(%rsp)`). `mul`'s prologue is identical but reads a
*third* incoming stack pointer at `0x48(%rsp)`, so `mul` is 9 args with the result last. Passing
fewer pointers null-derefs (segfault) — this is the ABI a reimplementer must honor to drive the
oracle. `[HIGH/OBSERVED]` — prologue read + ABI confirmed by `add(2.0,3.0)=5.0`,
`mul(2.0,3.0)=6.0` returning the correct bits in the last pointer.

### 3.2 `add` / `sub` — annotated value model

```c
// ivp_addnxf16 :  out = round_FCR(a + b)
// IEEE-754 binary16 add, single-rounded, round mode = FCR.RoundMode (or leaf arg).
// Proven by execution against module__xdref_add_1_1_1_16f_16f_16f_2 (@0x51c640).
uint16_t add_f16(uint16_t a, uint16_t b, int rm /*0=RNE 1=RZ 2=RU 3=RD*/) {
    // exact-rational a+b, then round once to binary16 under rm:
    //   RNE: ties to even   RZ: toward 0   RU: toward +inf   RD: toward -inf
    // signed-zero:  +0 + -0 = +0 under RNE/RZ/RU,  -0 under RD; -0 + -0 = -0
    // exact cancellation (x + (-x)) = +0 under RNE,  -0 under RD
    // inf + (-inf) = qNaN(0x7e00) + Invalid;  inf + inf = inf
    // NaN propagation: a-NaN dominates (first-operand payload wins, see §6)
}
// ivp_subnxf16 :  out = round_FCR(a - b)  — identical algebra; sub(inf,inf)=qNaN+Invalid.
```

**Rounding-tie certificate (proven by execution).** Driving `add` at the exact half-ULP tie point
`1024.0 + 0.5` (ULP at 1024.0 is 1.0, so this is an exact tie):

```
add(1024.0=0x6400, 0.5=0x3800, RNE) -> 0x6400 = 1024.0   (1024 even -> tie DOWN to even)
add(1025.0=0x6401, 0.5=0x3800, RNE) -> 0x6402 = 1026.0   (1025 odd  -> tie UP to even)
add(1024.0,        0.5,        RU ) -> 0x6401 = 1025.0   (ceil)
add(1024.0,        0.5,        RZ ) -> 0x6400 = 1024.0   (truncate)
add(1024.0,        0.5,        RD ) -> 0x6400 = 1024.0   (floor)
```

This is textbook round-half-to-even (RNE), and the directed modes round the half toward their
respective bound. It re-confirms the
[fp sub-ISA §3.2](../core/fp-sub-isa.md#32-the-encoding--proven-by-execution) round-mode encoding
`{0:RNE, 1:RZ, 2:RU, 3:RD}` on a *different* leaf (add, not the 3000-pair sweep), via the
**architecturally-default RNE** branch — distinct from the un-parameterized-call RZ default the
[confidence model §6.1](../../reference/confidence-model.md) carries (the two-level RNE/RZ split is
*not re-litigated* here; this page always threads the mode explicitly). `[HIGH/OBSERVED·exec]`

**Signed-zero certificate.**

```
add(+0, -0, RNE) -> 0x0000 (+0)      add(+0, -0, RD) -> 0x8000 (-0)
add(-0, -0, RNE) -> 0x8000 (-0)      add(2.0, -2.0, RNE) -> 0x0000 (+0; exact cancel)
                                     add(2.0, -2.0, RD)  -> 0x8000 (-0; RD signs the zero)
```

The sign of an exact zero result is `+` under all modes except `RD`, which signs it `−` — the
IEEE-754 rule the [fp sub-ISA](../core/fp-sub-isa.md) round core implements. fp32
(`add_1_1_1_32f_32f_32f_2`) reproduces every one of these bit-for-bit at the `2^24` tie boundary
(§6.3). `[HIGH/OBSERVED·exec]`

### 3.3 `mul` — GRS / sticky single-rounding

```c
// ivp_mulnxf16 :  out = round_FCR(a * b)
// Full-width product (22-bit significand for fp16), then ONE rounding to 11 bits using
// guard/round/sticky (GRS).  sign(out) = sign(a) XOR sign(b)  (always, incl. zeros).
// Proven by execution against module__xdref_mul_1_1_1_1_16f_16f_16f_2 (@0x51d640).
```

**GRS / overflow certificate (proven by execution).**

```
mul(1+2^-10, 1+2^-10, RNE) -> 0x3c02   mul(.., RU) -> 0x3c03   mul(.., RZ/RD) -> 0x3c02
  (the exact product needs >11 sig bits; RU bumps the last bit, RZ/RD/RNE keep it)
mul(60000, 60000, RNE) -> 0x7c00 = +inf            (overflow rounds to inf under RNE)
mul(60000, 60000, RZ ) -> 0x7bff = max-finite      (RZ overflow clamps to largest finite)
```

The RZ-overflow→max-finite vs RNE-overflow→inf split is the
[directed-round-overflow rule](../core/fp-sub-isa.md#51-what-is-full-ieee-754): overflow under a
mode that rounds *away* from the infinity yields `±0x7bff` (fp16) / `±0x7f7fffff` (fp32), under
RNE/toward-the-infinity it yields `±inf`. `[HIGH/OBSERVED·exec]`

**Signed-zero + special-value certificate.**

```
mul(+0, +2.0) -> 0x0000 (+0)     mul(-0, +2.0) -> 0x8000 (-0)     mul(-0, -2.0) -> 0x0000 (+0)
mul(-3.0, +0) -> 0x8000 (-0)     mul(0, inf)   -> 0x7e00 (qNaN) + Invalid(0x1)
mul(inf, 2.0) -> 0x7c00 (+inf)   mul(inf, -2.0)-> 0xfc00 (-inf)
```

`0 × inf = qNaN + Invalid` is the IEEE invalid-operation case; the result sign is the **XOR of the
input signs** for every finite product, including the signed zeros. `[HIGH/OBSERVED·exec]`

### 3.4 The Invalid sticky flag — observed live

The `add`/`mul` leaves write a dedicated **Invalid** field (one of the status output pointers,
`%r9d`→ field at `0x38(%rsp)` for add): it reads `0x1` exactly when the op is an
invalid-operation (`inf − inf`, `0 × inf`, or an sNaN input) and `0x0` otherwise (an ordinary
inexact add does **not** set it — `add(1024.0,0.5)` Invalid field = `0x0`). This is the per-lane
input to the `SHARED_OR` `InvalidFlag`
([fp sub-ISA §5.2](../core/fp-sub-isa.md#52-the-shared_or-flag-semantics--the-deviation-a-reimplementer-must-model)).
`[HIGH/OBSERVED·exec]`

---

## 4. `abs` / `neg` / `addexp` — the sign and exponent primitives

### 4.1 `abs` / `neg` — fully decoded *and* execution-confirmed

These two are tiny enough to read end-to-end. `module__xdref_abs_16f_16f` (@ `0x51c630`) is
exactly nine bytes:

```asm
51c630:  and  $0x7fff, %esi      ; clear bit 15 (the sign)
51c636:  mov  %esi, (%rdx)       ; out
51c638:  ret
```

`module__xdref_neg_16f_16f` (@ `0x521d50`) toggles bit 15 unconditionally (extract sign, `sete`,
re-OR the complemented bit). So:

```c
uint16_t abs_f16(uint16_t x) { return x & 0x7FFF; }   // ABI: (scratch, x, out_ptr)
uint16_t neg_f16(uint16_t x) { return x ^ 0x8000; }   // ABI: (scratch, x, out_ptr)
```

Both are **bit operations on the sign**: they do *not* round, do *not* canonicalize NaN, do *not*
raise any flag (no flag-field pointer in the ABI). Confirmed by execution:

```
abs(-2.0=0xC000) -> 0x4000 (+2.0)    abs(-inf=0xFC00) -> 0x7c00 (+inf)   abs(NaN) keeps payload
neg(+2.0=0x4000) -> 0xC000 (-2.0)    neg(+0=0x0000)   -> 0x8000 (-0)     neg(NaN) toggles sign only
```

A reimplementer models `abs`/`neg` as pure sign-bit ops in the integer domain; they are the
**only** sign manipulators in the slice (no `copysign`, §1 GOTCHA). `[HIGH/OBSERVED·exec]`

### 4.2 `addexp` — the exponent-add range-reduction primitive

`module__xdref_addexp_16f_16f_16f` (@ `0x5218d0`) is a 46-byte body read end to end: it extracts
both biased exponents (`shr $0xa; and $0x1f`), adds them plus the constant `0x11` (= 17), masks to
5 bits, and re-assembles with **a's mantissa** and **`sign_a XOR sign_b`**:

```c
// ivp_addexpnxf16 :  scale a by a power of two derived from b's exponent.
//   out.exp      = (a.exp + b.exp + 0x11) & 0x1F     // 0x11 = 17 = bias-bias correction
//   out.mantissa = a.mantissa
//   out.sign     = a.sign ^ b.sign
// ABI: (scratch, a, b, out_ptr)  — out in %rcx (4th arg), no round, no flag.
```

Execution confirms it acts as `a · 2^(unbias(b.exp))` with a's significand carried through:

```
addexp(1.0=0x3c00, 2.0=0x4000) -> 0x4000 = 2.0      addexp(3.0, 1.0) -> 0x4200 = 3.0
addexp(1.5=0x3e00, 1.0=0x3c00) -> 0x3e00 = 1.5      addexp(-1.0, 2.0)-> 0xc000 = -2.0 (sign=a^b)
```

This is the standard fast scale-by-power-of-two used inside the transcendental
range-reduction kernels (the exponent of `b` selects the binade; `a`'s significand is the
unit-interval value). `addexpm` is the minus/sign variant (selector `0x2be80306`); its exact
mantissa-mask differs but the family is the same exponent-domain ALU op — encoding pinned,
the `m`-variant value detail `[HIGH/OBSERVED]` on structure. `[HIGH/OBSERVED·exec]` for `addexp`.

---

## 5. `min` / `max` / `minnum` / `maxnum` — and the NaN-asymmetry QUIRK

There are two distinct min/max families, and a reimplementer that collapses them is wrong on every
NaN input. The value leaves are `module__xdref_{min,max}_1_16f_16f_16f` (@ `0x523630`/`0x523720`)
and `module__xdref_{minnum,maxnum}_1_16f_16f_16f` (@ `0x524bc0`/`0x524cc0`); all four take
`(scratch, a, b, status_ptr, result_ptr)` — **no round mode** (min/max select, they do not round)
— with the fp **result in the *last* pointer** (`%r8`, arg-5) and an Invalid status in arg-4
(`%rcx`). `[HIGH/OBSERVED]` — ABI read from the two store sites and confirmed by `max(2.0,3.0)=3.0`.

### 5.1 The numeric behavior

```c
// maxnum / minnum  (IEEE-754-2008 maxNum / minNum):  if exactly one operand is NaN,
//   return the *other* (the number); if both NaN, return a NaN.  Quiet, NaN-suppressing.
// max / min        (the plain forms):  NaN-ASYMMETRIC — the SECOND operand's NaN dominates,
//   the FIRST operand's NaN is suppressed (treated as "take operand b").
```

### 5.2 The NaN-asymmetry — proven by execution

Driving all four leaves with a number `3.0` and distinct-payload qNaNs (`NaN_a`=`0x7e11`,
`NaN_b`=`0x7e55`):

| call | `max` | `maxnum` | `min` | `minnum` |
|---|---|---|---|---|
| `(NaN_a, 3.0)` | **`0x4200` (3.0)** | `0x4200` (3.0) | `0x4200` (3.0) | `0x4200` (3.0) |
| `(3.0, NaN_b)` | **`0x7e55` (NaN_b)** | `0x4200` (3.0) | `0x7e55` (NaN_b) | `0x4200` (3.0) |
| `(NaN_a, NaN_b)` | `0x7e55` | `0x7e55` | — | — |

> **QUIRK — plain `max`/`min` are NaN-asymmetric: the *second* operand's NaN wins, the *first*
> operand's NaN is suppressed.** `max(NaN_a, 3.0)` returns the *number* `3.0`, but
> `max(3.0, NaN_b)` returns the *NaN* `0x7e55` — the result depends on **which operand** is the
> NaN. This is *not* IEEE-754 `maxNum` (which suppresses NaN symmetrically) and *not* symmetric
> NaN-propagation (which would propagate either). The shipped `max`/`min` behave like "if `a` is
> NaN, take `b`; else compare" — equivalently, they return operand `b` on the
> `a`-is-NaN-or-equal path, so **operand `b` wins both the NaN case and the tie case** (§5.3). A
> reimplementer must use `maxnum`/`minnum` for IEEE `maxNum`/`minNum` and reserve `max`/`min` for
> the asymmetric "select" semantics the compiler emits where operand order encodes intent.
> `[HIGH/OBSERVED·exec]` — fp16 above, fp32 identical (§6.3).

### 5.3 Signed-zero and tie behavior

```
max(+0, -0) -> 0x8000 (-0)     max(-0, +0) -> 0x0000 (+0)      // returns the SECOND operand on a tie
min(+0, -0) -> 0x8000 (-0)     min(-0, +0) -> 0x0000 (+0)
```

On the `+0`/`−0` tie (which compare-equal), `max`/`min` return the **second** operand — the same
"operand `b` wins on equal" rule that drives the NaN asymmetry. (IEEE leaves the signed-zero
min/max tie implementation-defined; this core picks operand `b`.) `[HIGH/OBSERVED·exec]`

### 5.4 sNaN handling

All four forms raise **Invalid** (`0x1` in the status field) on an sNaN input and return the
*number* (`maxnum(sNaN, 3.0) = 3.0 + Invalid`; `max(sNaN, 3.0) = 3.0 + Invalid`). sNaN always
signals; only the *quiet*-NaN behavior differs between the plain and `*num` forms. `[HIGH/OBSERVED·exec]`

---

## 6. The compares — ordered/unordered × quiet/signaling (the compare-split GOTCHA)

The ten compares write a per-lane boolean to `vbool` (the `a=vbool(o)` roster role). The value
leaves are `module__xdref_<pred>_1_1_16f_16f` — ABI `(scratch, a, b, status_ptr, bool_ptr)`, the
**boolean result in the *last* pointer** (`%r8`) and the **Invalid status in arg-4** (`%rcx`).
`[HIGH/OBSERVED]` — ABI confirmed by `oeq(2,2)=1`, `olt(2,3)=1`.

### 6.1 The truth table (proven by execution)

| pred | `2<3` | `3<2` | `2==2` | meaning |
|---|:--:|:--:|:--:|---|
| `oeq` | 0 | 0 | 1 | ordered equal |
| `ole` | 1 | 0 | 1 | ordered `<=` |
| `olt` | 1 | 0 | 0 | ordered `<` |
| `ueq` | 0 | 0 | 1 | unordered equal (`== OR either-NaN`) |
| `ule` | 1 | 0 | 1 | unordered `<=` |
| `ult` | 1 | 0 | 0 | unordered `<` |
| `uleq` | 1 | 0 | 1 | unordered `<=` (signaling `q`-form) |
| `ultq` | 1 | 0 | 0 | unordered `<` (signaling `q`-form) |
| `uneq` | 1 | 1 | 0 | not-equal-or-unordered |
| `un` | 0 | 0 | 0 | unordered (true iff some NaN) |

The **ordered** predicates (`o*`) are *false* when either operand is NaN; the **unordered**
predicates (`u*`) are *true* when either operand is NaN (so `ult(2, qNaN)=1`, `un(2, qNaN)=1`).
`[HIGH/OBSERVED·exec]`

### 6.2 The quiet/signaling split — proven by the Invalid status field

The decisive reimplementation fact is **which compares raise Invalid on a quiet-NaN input**. The
status field (`%rcx`, arg-4), probed live with `(2.0, qNaN)` and `(2.0, sNaN)`:

| pred | Invalid on **qNaN** | Invalid on **sNaN** | class |
|---|:--:|:--:|---|
| `oeq` | `0x0` | `0x1` | **quiet** |
| `ole` | `0x1` | `0x1` | **signaling** |
| `olt` | `0x1` | `0x1` | **signaling** |
| `ueq` | `0x0` | `0x1` | **quiet** |
| `ule` | `0x0` | `0x1` | **quiet** |
| `ult` | `0x0` | `0x1` | **quiet** |
| `uleq` | `0x1` | `0x1` | **signaling** (`q`) |
| `ultq` | `0x1` | `0x1` | **signaling** (`q`) |
| `uneq` | `0x0` | `0x1` | **quiet** |
| `un` | `0x0` | `0x1` | **quiet** |

> **GOTCHA — the `q` suffix means *signaling*, and the ordered relationals signal on qNaN; an
> sNaN always signals.** The split is *not* ordered-vs-unordered. It is: the **relational ordered
> predicates `olt`/`ole`** and the two **`q`-suffixed unordered predicates `uleq`/`ultq`** raise
> **Invalid even on a quiet NaN** (they are IEEE *signaling* compares); the **equality predicates
> (`oeq`/`ueq`/`uneq`)** and the **plain unordered relationals (`ult`/`ule`/`un`)** are **quiet** —
> they return a boolean with *no* Invalid on qNaN. Every compare raises Invalid on an sNaN. A
> reimplementer who maps the `q`-suffix to "quiet" inverts the flag behavior on exactly the
> predicates a sort/clamp kernel uses most. The selector encoding mirrors this: ordered
> (`o*`)=byte2 `0x48`, unordered (`u*`)=`0x4c` (bit `0x04`), and the `q`/`uneq`/`un` sub-group
> sits at byte1 `0x80`/`0x81`/`0x82` over `0x48` — three enumerated sub-bands, not one bit (§2).
> `[HIGH/OBSERVED·exec]`

The `vbool` boolean these write feeds the predicated `_T`-throttle select family
([B11](b11-vbool-alu.md), [B21](b21-select-shuffle.md)) and the predicate-validation lane.

### 6.3 fp32 precision-invariance — the same behaviors re-proven

The fp32 leaves (`add_1_1_1_32f_32f_32f_2`, `mul_1_1_1_1_32f_32f_32f_2`, `max_1_32f_32f_32f`,
`maxnum_1_32f_32f_32f`, `oeq_1_1_32f_32f`, `olt_1_1_32f_32f`) were driven over the same edge cases.
Every behavior is **bit-identical to fp16** at the fp32 scale:

```
add(2^24=0x4b800000, 0.5, RNE) -> 16777216.0 (even, tie down)   add(2^24+1, 0.5, RNE) -> 16777218.0 (odd, tie up)
add(2^24, 0.5, RU) -> 16777218.0 (ceil)        add(2^24, 0.5, RZ) -> 16777216.0 (trunc)
max(NaN_a, 3.0) -> 0x40400000 (3.0)            max(3.0, NaN_b) -> 0x7fc00055 (NaN_b)  // asymmetry holds
maxnum(NaN_a,3.0)=3.0   maxnum(3.0,NaN_b)=3.0  // number always wins
oeq(2.0, qNaN) Invalid=0x0 (quiet)             olt(2.0, qNaN) Invalid=0x1 (signaling)
mul(-0, 2.0) -> 0x80000000 (-0)                // sign = a^b
```

This grounds the fp sub-ISA's
[gen-invariant two-precision claim](../core/fp-sub-isa.md#6-the-fp-datatype-set): the fp16 and
fp32 datapaths are one round/compare core multiplexed across the two widths, producing the
identical rounding, NaN-asymmetry, signed-zero, and quiet/signaling algebra. `[HIGH/OBSERVED·exec]`

---

## 7. Batch tally — every fp-ALU mnemonic vs `nm`

The 52 mnemonics and their placement counts, re-counted this pass with
`nm libisa-core.so | rg -c 'Opcode_ivp_<mnem>_Slot_.*_encode'` (the only legitimate count method,
per [coverage tally](../core/coverage-tally.md)):

| group | fp16 mnemonics (placements) | fp32 mnemonics (placements) |
|---|---|---|
| sign | `abs`(13) `neg`(13) | `abs`(13) `neg`(13) |
| arith | `add`(9) `sub`(9) `mul`(9) | `add`(9) `sub`(9) `mul`(9) |
| exponent | `addexp`(6) `addexpm`(6) | `addexp`(6) `addexpm`(6) |
| min/max | `max`(12) `min`(12) `maxnum`(12) `minnum`(12) | `max`(12) `min`(12) `maxnum`(12) `minnum`(12) |
| compare | `oeq`/`ole`/`olt`/`ueq`/`ule`/`ult`/`uleq`/`ultq`/`uneq`/`un` (14 each) | same 10 × (14 each) |
| classify | `clsfy`(9) | `clsfy`(9) |

```
fp16 subtotal : 26 mnemonics,  286 placements
fp32 subtotal : 26 mnemonics,  286 placements
BATCH TOTAL   : 52 mnemonics,  572 placements   (of the 12569-placement certified cover)
```

**Value-leaf grounding.** Every arithmetic/compare/min-max/sign mnemonic resolves to a
`module__xdref_` value leaf that was **driven live this pass**: `add`/`sub`/`mul`,
`max`/`min`/`maxnum`/`minnum`, `oeq`/`ole`/`olt`/`ueq`/`ule`/`ult`/`uleq`/`ultq`/`uneq`/`un`,
`abs`/`neg`, `addexp` — fp16 fully, fp32 spot-confirmed for the edge classes (§6.3). `clsfy`'s mask
is execution-validated on the
[fp sub-ISA page](../core/fp-sub-isa.md#54-the-classify-mask--proven-by-execution); this page
states its encoding only. `[HIGH/OBSERVED·exec]`

> **NOTE — ungrounded / deferred items (honestly flagged).**
> (1) `addexpm`'s exact mantissa-mask value semantics are read structurally from the body but were
> not execution-swept like `addexp` — its **encoding** is `[HIGH/OBSERVED]`, its precise value rule
> `[MED/OBSERVED]` pending a live sweep. (2) The `r*`/`rb*` reduce-min/max forms
> (`rmaxnum`/`rminnum`/`rbmaxnum`/`rbminnum`, selectors `0x2d0000b1`/`0x27902080`…) are
> **tree-reductions** over the whole vector (their leaf `rbmaxnum_nx16f_1_64_16f_512f` takes a
> 512-bit input) and are **deferred to [B08 reduce](b08-reduce.md)** — *not* tallied in this
> batch's 52/572. (3) Per-instruction **cycle latency** (the 1-cycle ALU write-stage `@11`) is
> from the [register-file schedule model](../core/register-files.md#5-readwrite-semantics-per-file);
> the cycle oracle (`libcas-core.so`) is license-gated, so latency is `[HIGH/OBSERVED]` from the
> schedule table, not from a retirement run.

---

## 8. Batch boundary — what this page does NOT own

| neighbor op(s) | owner page | why not here |
|---|---|---|
| `mula`/`mulan`/`muls`/`mulsn`/`madd`/`msub`/`maddn`/`mulsone` | [B17 fp32 FMA](b17-spfma.md) · [B18 fp16 FMA](b18-hp-fma.md) | fused multiply-add (3-operand, `wvec`-class accumulate or single-round FMA) |
| `rmaxnum`/`rminnum`/`rbmaxnum`/`rbminnum` | [B08 reduce](b08-reduce.md) | full-vector tree reduction, not a per-lane ALU op |
| `firound`/`ficeil`/`fifloor`/`fitrunc`/`firint`/`trunc16` | [B13 sp-cvt](b13-sp-cvt.md) · B20 | round-to-integral / convert family (FCR + the 3-bit convert-core round, `away` mode) |
| `recip0`/`rsqrt0`/`sqrt0`/`nexp0`/`nexp01`/`div0`/`divn` | [B14](b14-hp-lookup.md)/[B15](b15-sp-lookup.md)/B23 | transcendental seeds + Newton/QLI iterate |
| `const`/`mkdadj`/`mksadj` | B16/B24 | constant materialize / adjust-by-magnitude |
| `clsfy` value mask | [fp sub-ISA §5.4](../core/fp-sub-isa.md#54-the-classify-mask--proven-by-execution) | classify *mask* is owned there; B02 owns only its *encoding* |
| `rur/wur fcr/fsr`, `movscfv` | [fp sub-ISA §4·§7](../core/fp-sub-isa.md) | FCR/FSR control/status access, not arithmetic |

---

## 9. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim of this page re-tested against the binary this pass; a claim survives only if a
*second* independent witness agrees.

1. **`add` rounds RNE-ties-to-even, RU ceils.** *Challenge:* could the leaf be ignoring the mode
   arg (the un-parameterized RZ-default)? *Re-test:* `add(1024.0,0.5,rm)` returns `0x6400` (RNE,
   even-down), `0x6401` (RU), `0x6400` (RZ); `add(1025.0,0.5,RNE)`→`0x6402` (odd → up). Four
   *distinct* mode outcomes on the same inputs ⇒ the mode arg is honored and RNE is genuine ties-to-even.
   Cross-witness: matches the [fp sub-ISA 3000-pair sweep](../core/fp-sub-isa.md#32-the-encoding--proven-by-execution).
   **Survives.** `[HIGH/OBSERVED·exec]`

2. **`max`/`min` are NaN-asymmetric (operand `b` wins).** *Challenge:* could the apparent asymmetry
   be a swapped result/status pointer in my harness? *Re-test:* with the pointers fixed by the
   `max(2.0,3.0)=3.0` sanity, `max(NaN_a,3.0)=3.0` but `max(3.0,NaN_b)=NaN_b` — and the
   `+0`/`−0` tie *also* returns operand `b` (`max(+0,−0)=−0`, `max(−0,+0)=+0`), an independent
   confirmation of the same "operand-b-wins" rule on a non-NaN tie. fp32 reproduces it. **Survives.**
   `[HIGH/OBSERVED·exec]`

3. **The `q` suffix is *signaling*, not quiet; `olt`/`ole` signal on qNaN.** *Challenge:* maybe the
   Invalid field is just always set for NaN inputs? *Re-test:* on the *same* `(2.0, qNaN)` input,
   `oeq`/`ueq`/`ult`/`un` report Invalid=`0x0` while `olt`/`ole`/`uleq`/`ultq` report `0x1` — the
   flag *discriminates* by predicate, so it is genuine signaling behavior, not a blanket NaN flag.
   sNaN sets it for all ten (the always-signals baseline). **Survives.** `[HIGH/OBSERVED·exec]`

4. **The opcode-selector imm is slot-local.** *Challenge:* maybe `F1_S3_ALU` selectors are
   representative and the rest are derived by an offset? *Re-test:* `addnxf16` across five slots
   yields `0x25d10000`/`0x86bf0000`/`0x03900004`/`0x01970000`/`0x00db8000` — no additive or
   bit-pattern relation; the F0 form even differs in the high byte (`0x86` vs `0x25`). The selector
   is genuinely per-`(opcode,slot)`, exactly as the
   [FLIX encode-thunk model](../core/flix-encoding.md#61-the-universal-encode-thunk-abi) states.
   **Survives — and is the reason §2 fixes one canonical slot.** `[HIGH/OBSERVED]`

5. **No native `copysign`; `abs`=`x & 0x7fff`, `neg`=`x ^ 0x8000`.** *Challenge:* could a
   copysign-class op hide under a different name (e.g. a `mk*adj`)? *Re-test:* `rg -i
   copysign|csign|signinj` over the 1065-op roster = 0; and the `abs`/`neg` bodies are nine and
   twenty-seven bytes, both pure sign-bit integer ops with no flag pointer in their ABI, confirmed
   by `abs(-inf)=+inf`, `neg(+0)=-0`. The `mkdadj`/`mksadj` ops are *magnitude-adjust* (a different
   B16/B24 family), not copysign. **Survives.** `[HIGH/OBSERVED·exec]`

No claim on this page rests on a raw dump, an unnamed symbol, or a single uncorroborated witness;
every fp value fact carries a differential-execution certificate against the shipped leaf.

---

## 10. Confidence ledger

**HIGH / OBSERVED (by execution)** — driven live against `libfiss-base.so` this pass:

* `add`/`sub`/`mul` rounding (RNE ties-to-even, RU/RD/RZ directed; the `1024.0+0.5` and `2^24+0.5`
  tie certificates), signed-zero algebra (`+0+−0`, exact cancellation under RD), GRS single-round
  for `mul`, RZ-overflow→max-finite vs RNE-overflow→inf, `inf−inf`/`0×inf`→qNaN+Invalid.
* The NaN-asymmetry of plain `max`/`min` (operand `b` wins both the NaN and the tie case) vs the
  symmetric NaN-suppression of `maxnum`/`minnum`; sNaN-always-signals for all four.
* The ten-predicate compare truth table and the quiet/signaling Invalid split (`olt`/`ole`/`uleq`/
  `ultq` signal on qNaN; `oeq`/`ueq`/`ult`/`ule`/`uneq`/`un` are quiet; all signal on sNaN).
* `abs` = `x & 0x7fff`, `neg` = `x ^ 0x8000` (bodies read + execution-confirmed); `addexp` =
  exponent-add with `a`'s mantissa and `sign_a^sign_b`.
* The NaN-payload precedence (add: first-operand payload; mul: second-operand payload — §6 of fp
  sub-ISA cross-reference; observed here directly).
* fp32 precision-invariance of every edge behavior above.

**HIGH / OBSERVED** — read from `libisa-core.so` immediates / disassembly this pass:

* The 52-mnemonic roster, the `F1_S3_ALU` opcode-selector immediates (fp16 + fp32), the slot-local
  variance of the selector, the fp16/fp32 precision-bit positions, the 572-placement census.
* The value-leaf ABI (arg counts, result-in-last-pointer) from each leaf's prologue.

**MED / OBSERVED**

* `addexpm`'s exact mantissa rule (body read; not execution-swept).

**Deferred (not in this batch's denominator)**

* `r*`/`rb*` reduce-min/max → [B08](b08-reduce.md); FMA `mul*`/`madd*` → [B17](b17-spfma.md)/
  [B18](b18-hp-fma.md); round/convert → [B13](b13-sp-cvt.md)/B20; transcendental seeds →
  [B14](b14-hp-lookup.md)/[B15](b15-sp-lookup.md).

All facts read as derived from shipped-artifact static analysis and license-free in-process
execution of the binary's own value leaves (lawful interoperability RE).

---

## Cross-references

* [The Floating-Point Sub-ISA (FCR/FSR view)](../core/fp-sub-isa.md) — the round-mode encoding, the
  five `SHARED_OR` sticky flags this slice raises, the two-level RNE/RZ model, and the CLSFY 8-bit
  class mask (the value semantics this page's `clsfy` encoding points to).
* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid, the `F1_S3_ALU`
  slot these selectors target, the encode-thunk ABI, and the two-tier (refuted-global-bit) selector
  model the precision-bit GOTCHA builds on.
* [The Eight Register Files](../core/register-files.md) — the `vec` (idx 2) datapath, the `vbool`
  (idx 3) compare-result file, and the stage-`@11` ALU write the arithmetic ops use.
* [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the `12569`-placement
  certified-perfect denominator this batch's `572` is a slice of; the count-with-`nm` discipline.
* [B01 vector ALU (int slice)](b01-vec-alu-int.md) — the integer ALU/bitwise ops a `copysign` is
  synthesized from; [B11 vbool ALU](b11-vbool-alu.md) / [B21 select/shuffle](b21-select-shuffle.md)
  — the consumers of the `vbool` masks these compares produce.
* [B17 fp32 FMA](b17-spfma.md) · [B18 fp16 FMA](b18-hp-fma.md) — the fused `mul*`/`madd*` family
  excluded here; [B08 reduce](b08-reduce.md) — the `r*`/`rb*` reduce-min/max; [B13](b13-sp-cvt.md) —
  the round/convert family.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, and the
  proven-by-execution value lane (`libfiss-base.so` via ctypes) that grounds every fp claim here.
