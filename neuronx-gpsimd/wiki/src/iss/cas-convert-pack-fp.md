# cas/fiss Convert / Pack / FP Semantics

This page documents the **datatype-conversion op family** as the cycle-accurate ISS sees it: the
`int↔fp` converts (`float`/`ufloat` = int→fp; `trunc`/`utrunc` = fp→int), the **FP-width** converts
(`fp16↔fp32` widen/narrow), the **PACK / UNPACK** family (`packl` = wrap, `pack`/`packvr` = round,
`packv*`/`sats` = saturate), the **rounding modes** (FSR-driven, RNE default), **saturation/clamp**
on narrowing, and **special-value** (inf/nan/denorm) handling. It is the seventh page of
[Part 14 — the ISS as Executable Oracle](./cas-core-surface.md) and threads the keystone that
governs *every* page in this Part:

> **Keystone — cas decodes, fiss values.** The cycle-accurate ISS `libcas-core.so`
> (`BASE = libcas-Xtensa.so`, `VERS_1.1`, GCC 4.9.4, modeled core = Cayman / Vision-Q7 IVP VLIW)
> computes **DECODE + TIMING + REGISTER-HAZARD**, but it does **NOT** compute element **values**.
> Every per-lane conversion — the exponent rebias, the round, the saturate, the special-value
> branch — is delegated to `libfiss-base.so`, the 864-leaf soft-float oracle of
> `module__xdref_*` value functions, **drivable live via `ctypes`**
> ([fiss Datapath, the value oracle](./fiss-datapath-oracle.md)). This page covers **both halves**:
> the **fiss VALUE** (what bits come out, byte-anchored *and* live-driven) and the **cas TIMING**
> (which slot, what latency), reconciled with the firmware
> [Cast](../firmware/kernels/cast-copy.md) and [MX dequant](../firmware/kernels/mx-dequant.md)
> kernels end to end. The ISA encodings live at
> [B13 sp-cvt](../isa/ref/b13-sp-cvt.md), [B20 hp-cvt](../isa/ref/b20-hp-cvt.md),
> [B10 wvec-pack](../isa/ref/b10-wvec-pack.md); the FSR / FP sub-ISA at
> [fp-sub-isa](../isa/core/fp-sub-isa.md); the formal semantics at
> [group-semantics-ii](../isa/semantics/group-semantics-ii.md).

Confidence tags follow [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte or proven by live `ctypes` execution, `[MED/INFERRED]` = reasoned
over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence. fiss offsets are into the
shipped `extracted/.../ncore2gp/config/libfiss-base.so` (12 330 016 B; ELF64 x86-64, **not
stripped**, `sha256 260b110c…`); cas offsets into `libcas-core.so` (45 878 080 B, `sha256 7f1d86da…`).

> **GOTCHA — VMA vs file offset.** In `libfiss-base.so`, `.text` (VMA `0x190430`) and `.rodata`
> (`0x88ff00`) are **VMA == file offset** — every `objdump`/`xdref` body below is read at raw VMA.
> The writable sections carry the **`0x200000` delta** (`.data.rel.ro` VMA `0xc17e80` → file
> `0xa17e80`; `.data` VMA `0xc8eb68` → file `0xa8eb68`; `readelf -SW`). All the convert/pack value
> bodies are `.text`, so the delta never bites the disassembly here.
---

## 1. Executive finding — the convert/pack machine, decoded *and* driven

The Vision-Q7 / Cayman conversion subsystem splits the usual way: cas schedules, fiss computes. The
decoded shape, with every value claim either byte-read or **live-confirmed** through `ctypes`:

| family | what it does | cas LAT (vec) | fiss primitive | §|
|---|---|---:|---|---|
| INT → FP | `ivp_float*` (signed) / `ufloat*` (unsigned); rounds per FSR | 13 | `float`/`ufloat` `_16f`/`_32f` | §2 |
| FP → INT | `ivp_trunc*` (signed) / `utrunc*`; **round-toward-zero** | 12 | `trunc`/`utrunc` `_16`/`_32` | §3 |
| FP-width **widen** | `ivp_cvtf32f16` (fp16 → fp32); lossless | 13 | `cvtf32_1_32f_16f` | §4 |
| FP-width **narrow** | `ivp_cvtf16f32` (fp32 → fp16); rounds per FSR | 13 | `cvtf16_…16f_32f` | §4 |
| **bf16 / fp8** | **NO NATIVE CONVERT OP** — via FP32 hub / unpack | (n/a) | (none) | §5 |
| INT-width cvt | `cvt{16,24,32,48,64,96}{s,u}` (sext/zeroext/sat-narrow) | 10–12 | `cvt*`/`sext`/`zeroext`/`sats` | §6 |
| PACK (wrap) | `packl` = take low, **no clamp** | 12 | `packl`\[`_nosat`\] | §7 |
| PACK (round) | `pack` / `packvr` = `1<<(sh-1)` bias + shift | 12 | `pack`/`packvr` | §7 |
| PACK (saturate) | `packv*`/`packs` / `sats` = sign-extend + clamp | 12 | `packv*`/`sats` | §7 |
| PACK accumulator | `packvr` (48b acc → 16b vec, round + sat) | 12 (S1\_Ld) | `packvr`/`packvrnr` | §7 |
| ROUNDING mode | FSR special-register (UR `0xe9`); RNE default | +0 | `r8`/`r9` ctx → `%edx[1:0]` | §8 |

The five claims this page stakes — **(1)** bf16/fp8 have no native convert (FP32-hub only), **(2)**
`packl` wraps while `packvr` rounds and `sats` saturates, **(3)** `trunc` is round-toward-zero,
**(4)** the default fp rounding is RNE, **(5)** the fiss soft-float values are exact IEEE-754 — are
each verified against the binary *and*, for (2)/(3)/(5), against **live `ctypes` execution** (§13).

---

## 2. INT → FP converts — `ivp_float` (signed) / `ivp_ufloat` (unsigned)

### 2.1 The signed body — `module__xdref_float_1_32f_32_32_2` @ `0x87aec0`

The ABI of these leaf `module__xdref_*` value helpers is register-based:
`fn(edi = lane/aux, esi = input, ecx = round-mode word [the "B" operand], …)`, returning the
result bits in `%eax` (the `_2` register-return forms) or storing through a pointer arg (the
`cvtf*`/`pack*` forms; §4, §7). The signed int32→fp32 body, byte-exact:

```c
// module__xdref_float_1_32f_32_32_2 @ 0x87aec0 — INT32 -> FP32, signed
// %esi = int32 input, %ecx = round-mode word (FSR copy), result in %eax
int32_t mag  = input;
mag          = -mag;             // 87aec6: neg  %edi   <- SIGN PROLOGUE
bool   neg   = (input < 0);      // 87aec8: test %esi,%esi ; 87aed0: js  <neg path>
uint32_t rm  = round_mode;       // 87aece: mov  %ecx,%ebx   <- the B operand = ROUND MODE
// ... normalize the magnitude (shl %cl) ; collect guard/round/sticky:
bool gr_sticky = (mag & 0x7f) != 0;   // 87aeec: test $0x7f,%dil ; setne
// 87af01: test $3,%al    -- low 2 bits = round decision
bool rne_tie  = (rm == 3);            // 87af07: cmp  $0x3,%ebx ; sete  <- RNE TIE-BREAK
// EXPONENT REBIAS to the fp32 0x7f bias:
exp = 0xa0 + (0x1fdf-region) + ...;   // 87aede: mov $0x1fdf,%r10d ; 87af7f: lea 0xa0(%r10,%rdx,1)
if (exp > 0xfe) return INF_or_clamp;  // 87afac: cmp $0xfe,%edx ; jbe <finite>   <- OVERFLOW->inf
return pack_fp32(neg, exp, mantissa); // assemble sign|exp|mantissa, value in %eax
```

The **sign** is the leading `neg %edi ; test %esi,%esi ; js` triple. The magnitude is normalized,
the exponent rebiased to the fp32 bias, the mantissa rounded per the FSR mode (the `cmp $0x3 ; sete`
is the round-to-nearest-**even** tie-break), overflow clamps to inf. It is **integer-only soft-float
— zero hardware FP** (no `cvtsi2ss`, no SSE).
### 2.2 Signed vs unsigned is the prologue — `ufloat_1_32f_32_32_2` @ `0x87b0d0`

The unsigned counterpart is **identical except it omits the sign prologue**. Disassembled head:

```c
// module__xdref_ufloat_1_32f_32_32_2 @ 0x87b0d0 — UINT32 -> FP32
// 87b0d6: push %rbx ; 87b0d7: jne <nonzero>   <- NO neg, NO js: operand is a magnitude
exp = 0x1fdf-region ...;          // 87b0dd: mov $0x1fdf,%r11d   (SAME exponent rebias as float)
mag = input << cl;                // 87b0e5: mov %esi,%edi ; 87b0e9: shl %cl,%edi
bool gr = (mag & 0x7f) != 0;      // 87b0eb: test $0x7f,%dil ; setne   (SAME round collect)
// ... same round path, no sign bit ever set.
```

So **signedness = presence/absence of the `neg/test/js` prologue** — exactly the operand-prologue
discriminator the multiply path uses.
> **NOTE — narrower forms.** `float16_1_16f_16_32_2` @ `0x522070` (INT16 → FP16) is the same
> shape with the 5-bit-exp / 10-bit-mant geometry (`lea 0x20(…)` fp16 `0xf` rebias, `0x7c00`-inf
> overflow). The scalar leaf is invoked **32 times** by the NX16 vector caller
> (`opcode__ivp_float16nx16__stage_5` @ `0x7fd910` — one call per lane). The named int→fp forms
> cover `{INT8/16/32, UINT8/16/32} → {FP16, FP32}`. There is **no** int→bf16 and **no** int→fp8
> leaf (§5).
---

## 3. FP → INT converts — `ivp_trunc` / `utrunc` (round-toward-zero)

### 3.1 The truncation body — `trunc_1_1_32_32f_32` @ `0x87b240`

```c
// module__xdref_trunc_1_1_32_32f_32 @ 0x87b240 — FP32 -> INT32, ROUND-TOWARD-ZERO
uint32_t exp = input >> 23;           // 87b24d: shr $0x17,%r12d   -> 8-bit exponent
bool special = (exp == 0);            // 87b257: test %r12b ; sete  (zero / Inf-NaN classify)
int sh = 0x34 - ((exp - bias) ...);   // 87b251: mov $0x34,%edi (=52) ; 87b27e: sub %eax,%edi
//   sh = mantissa-width + bias - exp  -> the right-shift amount
mantissa = (1 << 23) | (input & 0x7fffff);
int32_t  result = mantissa >> sh;     // SHIFT RIGHT; the fractional bits are DISCARDED == RTZ
//   NO `cmp $0x3` round tie-break anywhere in this body (grep == 0 — confirmed §13)
if (exp == 0xff) result = special;    // 87b2.. cmp $0xff (NaN/Inf) ; overflow -> clamp/INT_MIN
return result;
```

The exponent is extracted, the right-shift amount computed as `(mantissa-width + bias − exp)`, the
implied-`1.mantissa` shifted right, and **the fractional bits are dropped unconditionally** — that
is round-toward-zero, the C-cast / `numpy.astype(int)` semantics. A byte sweep of the entire body
(`0x87b240`–`0x87b510`) finds **zero** `cmp $0x3` RNE tie-breaks — the contrast with `float`/narrow
(which carry the tie-break) is decisive.
### 3.2 fp16 and unsigned forms

`trunc16_1_1_16_16f_32` @ `0x522610` is the fp16 form (`shr $0xa ; and $0x1f` 5-bit exp,
`cmp $0x1f` Inf/NaN, same shift-and-truncate). The unsigned `utrunc*` (`utrunc16` @ `0x5227d0`,
`utrunc_…32u_32f_32` @ `0x87b510`, `utrunc_…32u_16f_32` @ `0x523370`) drop the sign handling;
negative input clamps to `0`. `[HIGH/OBSERVED — present; the negative-clamp path is MED/INFERRED
from the unsigned target.]`

> **CORRECTION — `trunc` ignores the FSR mode.** A natural-but-wrong assumption is that the FSR
> rounding mode (§8) governs fp→int as it governs int→fp. The bytes refute it: `trunc`/`utrunc`
> carry **no** round-mode operand and **no** `cmp $0x3` tie-break — they are **always**
> round-toward-zero, regardless of FSR. The FSR mode steers **only** the rounding converts
> (int→fp and fp32→fp16-narrow and the rounding packs).
---

## 4. FP-width converts — `fp16 ↔ fp32` (the only native fp-width pair)

### 4.1 Direction convention (resolved by tracing cas stage-5 → fiss xdref)

The cas mnemonic reads **`CVT<DST><SRC>`**: `CVTF32F16` = "produce **F32** from **F16**" = **widen**;
`CVTF16F32` = "produce **F16** from **F32**" = **narrow**. Confirmed by the stage-5 dispatch:

```
opcode__ivp_cvtf32f16__stage_5    @0x250460 -> call xdref_cvtf32_1_32f_16f       (WIDEN  fp16->fp32)
opcode__ivp_cvtf16f32__stage_5    @0x250750 -> call xdref_cvtf16_…16f_32f         (NARROW fp32->fp16)
opcode__ivp_cvtf16n_2xf32__stage_5@0x251560 -> call xdref_cvtf16_…32f_32f_0       (2x narrow pack)
opcode__ivp_cvtf32nxf16_0__stage_5@0x250b80 -> call xdref_cvtf32_1_32f_32f_0      (lo half widen)
```

The `_0`/`_1` suffixes are the **low / high** half of the source pair (the `2NX` format packs two
fp16 per fp32 lane).

### 4.2 The WIDEN — `cvtf32_1_32f_16f` @ `0x5b77f0` (fp16 → fp32, lossless)

ABI: `fn(edi = aux, esi = fp16_in, rdx = aux/flag out, rcx = fp32 result out)` — the body writes
**both** `(%rdx)` (a class/flag word) and `(%r11 = rcx)` (the fp32 word):

```c
// module__xdref_cvtf32_1_32f_16f @ 0x5b77f0 — FP16 -> FP32 WIDEN
uint32_t e5  = (h >> 10) & 0x1f;            // 5b77f7: shr $0xa ; 5b7805: and $0x1f
bool inf_nan = (e5 == 0x1f);                // 5b7809: cmp $0x1f ; sete
bool mant_nz = (h & 0x1ff) != 0;            // 5b7811: test $0x1ff ; setne   (NaN vs Inf)
uint32_t qbit= ((h >> 9) ^ 1) & mant_nz;    // 5b781a: shr $0x9 ; 5b7823: xor $1  (qNaN detect)
uint32_t m10 = h & 0x3ff;                   // 5b781d: and $0x3ff   10-bit mantissa
uint32_t m23 = (m10 << 13) & 0x7fe000;      // 5b7848: shl $0xd ; and $0x7fe000  (10->23, shift 13)
if (inf_nan) result = 0x7f800000 | …;       // 5b78.. mov $0x7f800000   <- fp32 INF/NaN encode
// denormal path @0x5b78a8: bsr -> NORMALIZE the subnormal ; lea 0x71(…) rebias 5->8 bit exp
uint32_t sign = (uint32_t)(h & 0x8000) << 16;   // fp16 sign -> fp32 sign bit
*rdx = class_word; *rcx = sign | exp8 | m23;    // 5b7884: mov %edi,(%rdx) ; 5b789c: mov %eax,(%r11)
```

Widening is **lossless** (fp32 strictly contains fp16, so no rounding is needed); NaN/Inf propagate
with the qNaN bit preserved; **denormal fp16 inputs are normalized** (`bsr`), not flushed.
### 4.3 The NARROW — `cvtf16_1_1_1_1_16f_32f_2` @ `0x5b78f0` (fp32 → fp16, must round)

This is the largest convert body (~0x600 B) precisely because narrowing **rounds**:

```c
// module__xdref_cvtf16_1_1_1_1_16f_32f_2 @ 0x5b78f0 — FP32 -> FP16 NARROW
uint32_t e8  = f >> 23;                      // 5b78f6: shr $0x17
bool inf_nan = (e8 & 0xff) == 0xff;          // 5b78fd: cmp $0xff
bool mant_nz = (f & 0x7fffff) != 0;          // 5b7911: test $0x7fffff   (NaN vs Inf)
int16_t e5   = (e8 + 0x1f90) & 0x1fff;       // 5b792d: add $0x1f90 ; and $0x1fff  <- 8->5 REBIAS
// ROUND BLOCK @0x5b79df.. (round-mode arrives in %edx[1:0]):
//   and $1,r ; and $2,r ; cmp $0x3 ; sete   <- mode==3 selector + RNE tie-break  (5b79e7, 5b79f0)
//   test $0x7,cl ; (guard|round|sticky)
uint16_t sign = (f >> 16) & 0x8000;          // and $0x8000
// overflow / inf tail:
//   or $0x7c,%dh -> 0x7c00 (fp16 INF) ; or $0x78,%dh -> round-up-to-max / overflow path
// subnormal-output path present (gradual underflow to fp16 denormal, NOT flush-to-zero)
return sign | (e5 << 10) | mant10;
```

Two `cmp $0x3` tie-breaks (vs **zero** in `trunc`) confirm the narrow rounds. Overflow → fp16 inf
`0x7c00`; underflow → fp16 **denormal** (gradual, not FTZ); NaN/Inf propagate. The round mode is the
`%edx[1:0]` operand (§8).
### 4.4 The cas ENCODE (libisa-core, F1\_S3\_ALU, byte-exact)

```
Opcode_ivp_cvtf16f32_Slot_f1_s3_alu_encode @0x353680 : movl $0x27b050f0   (narrow)
Opcode_ivp_cvtf32f16_Slot_f1_s3_alu_encode @0x353620 : movl $0x27d050f0   (widen)
```

The widen/narrow direction differs in **byte[2]** (`0xb0` vs `0xd0` = a `+0x20000` enumerated
direction field); both share the `0x…050f0` cvt opcode base.

---

## 5. bf16 / fp8 — **NO NATIVE CONVERT OP** (the negative finding)

### 5.1 Exhaustive negative control (both binaries)

* **fiss:** an `nm -D` sweep over the **20 368** exported `T` symbols of `libfiss-base.so` for any of
  `{bf16, bfloat, _8f_, fp8, e4m3, e5m2}` returns **0 hits**. The only fp-width convert leaves are
  `cvtf16_…{16f,32f}` and `cvtf32_…{16f,32f}` — **binary16 (1-5-10) and binary32 (1-8-23) ONLY**.
* **cas:** no `IVP_*BF16*` / `IVP_*FP8*` / `IVP_*E4M3*` / `IVP_*E5M2*` convert issue function exists
  (apparent "bf16" string hits were hex addresses `0xbf16xx`, not mnemonics).

> **QUIRK — there is no hardware bf16 or fp8 convert.** The native fp-width converter is
> **fp16 ↔ fp32 only**. Every bf16 and every fp8 (E5M2 / E4M3) conversion the firmware appears to
> do is realized **in the kernel through the FP32 hub**, never by a dedicated ISS op.

### 5.2 How bf16 / fp8 are actually converted

* **The Cast kernel** (POOL `Cast`, op `0x47`) converts any dtype pair **through an FP32
  intermediate**: `in_dtype → FP32 → out_dtype`, `numpy.astype` / RNE. For a bf16 or fp8 endpoint the
  firmware does the bitfield work in the kernel — bf16 = the **top 16 bits of fp32** (so bf16↔fp32 is
  a shift + RNE round); fp8 E4M3/E5M2 = exponent rebias + mantissa truncate/round + saturate — using
  the §2/§3/§4 ISS primitives only for the fp16↔fp32 + int↔fp legs. The FP32 hub is exactly *why*
  the ISS needs only those two native convert families. `[HIGH — from the Cast two-mode contract +
  the absence here.]`
* **The MX dequant kernel** produces fp8 output (E5M2/E4M3) by
  `unpack(nibble) → ufloat(int→fp) → scale-multiply → SATURATING-CLAMP (bmin/bmax to the E5M2/E4M3
  range) → cvtg48 accumulator-extract`. The intermediate compute is the fp16/fp32 soft-float
  datapath; the final fp8 packing rides the `cvtg48` extract (§7.4); the E2M1→E5M2 rebias is a pure
  kernel bitfield op (exp rebias + mantissa zero-extend). `[HIGH — from MX §7.]`

End to end: the ISS exposes `fp16↔fp32 + int↔fp + saturating narrow/pack`; bf16 and the fp8 forms
ride those primitives via the FP32 hub (Cast) or the unpack+scale+clamp (MX).

---

## 6. INT-width converts — sext / zeroext / sats-narrow / cvt

### 6.1 Exact (lossless) width converts

```c
// module__xdref_sext_32_16 @0x858680 :  movswl %si,%esi ; mov %esi,(%rdx) ; ret  (sign-extend 16->32)
// module__xdref_zeroext_48_32 @0x85a950: mov %esi,(%rdx) ; movl $0,4(%rdx) ; ret  (zero-extend 32->48)
// module__xdref_cvt48s_48_32 @0x5ba8e0 : mov %esi,(%rdx) ; sar $0x1f ; and $0xffff ; …  (48b acc sext)
```

These are trivial leaves — the value in, sign/zero-extended, value out via the `%rdx` pointer.
`sext_32_16` and `zeroext_48_32` are **live-confirmed** in §13.
### 6.2 Saturating narrow — `sats_16_32` @ `0x5baaa0`

```c
// module__xdref_sats_16_32 @0x5baaa0 — SATURATING signed 32->16 narrow
int32_t lo  = (in << 16) >> 16;          // shl $0x10 ; sar $0x1f-region  (sign-extend the low 16)
if (lo == in) result = in & 0xffff;      // no overflow -> pass the low 16
else result = (in < 0) ? 0x8000 : 0x7fff;// CLAMP by sign to INT16_MIN / INT16_MAX
```

`sats_8_16` @ `0x8711a0` is the same at 8-bit (clamp `0x80`/`0x7f`). The canonical
"sign-extend-low + compare-to-high + clamp" saturating narrow; `cvt16s*` (signed narrow) calls
`sats`, `cvt16u*` (unsigned narrow) clamps to the unsigned range.
The int cvt/sext ops issue in `S3_ALU` at vec LAT 10–12 (exact sext/zeroext at 10, narrow cvt at
12).

---

## 7. The PACK / UNPACK family

### 7.1 Three narrow-pack flavours — byte-confirmed, the suffix is the discriminator

**(a) TRUNCATING (wrap) — `packl_16_48` @ `0x5e95e0`.** The whole body is three instructions:

```c
// module__xdref_packl_16_48 @0x5e95e0
//   0f b7 06   movzwl (%rsi),%eax    <- take the low 16 bits, NO clamp
//   89 02      mov    %eax,(%rdx)
//   c3         ret
```

`packl_16_48_nosat` @ `0x85a940` is **byte-identical** (`movzwl (%rsi),%eax ; mov %eax,(%rdx) ; ret`)
— proving `packl` **never** saturates; the `_nosat` is a confirming alias, not a different op. This is
the WRAP form, and §13 drives it live: input `0x8001` → `0x8001` (a saturating pack would give
`0x7fff`), input `0x10000` → `0x0000` (wraps). `[HIGH/OBSERVED + live]`

**(b) ROUNDING — `pack` @ `0x82cd10`.**

```c
// module__xdref_pack @0x82cd10 — generic ROUNDING narrow
int sh   = %edx;                         // shift amount
int bias = 1 << (sh - 1);                // lea -1(%rdx) ; and $0xf ; shl   <- round-half bias
val      = ((load + bias) & 0x7fffffff) >> sh;   // add bias ; shr %cl
return val & 0xffff;                     // round-to-nearest narrowing
```

**(c) SATURATING — `packv*` / `packs` / `sats`.** Sign-extend the low + overflow-detect + clamp to
`0x7fff`/`0x8000` (the §6.2 `sats` body). `packv*` = vector pack with saturation, `packvu` =
unsigned-saturate, `packvnr` = no-round saturating.
So **`packl` = WRAP, `pack`/`packvr` = ROUND, `packv*`/`sats` = SATURATE** — three distinct
primitives, not a single op with a flag.

> **CORRECTION — `PACKL` is the wrap form, not the clamp form.** An earlier MED-inferred binding
> read the MAC-drain `PACKL` post-op as a *saturating* narrow. The byte truth is the opposite:
> `packl` is `movzwl`-low with **no clamp** (and `_nosat` is byte-identical). The **saturating**
> drain is `packv*`/`packs`/`sats`; the **rounding** drain is `pack`/`packvr`.

### 7.2 The accumulator pack (the MAC drain) — `packvr_16_48_32` @ `0x5e98b0`

```c
// module__xdref_packvr_16_48_32 @0x5e98b0 — 48-bit accumulator -> 16-bit vec, ROUND + SATURATE
int sh = %edx & 0x1f;                     // 5e98d3: and $0x1f   (shift / Q-format scale)
acc >>= 5;  ...                           // 5e98cf: shr $0x5,%r8d  (the "_32" 32-bit shift operand)
// build a 1<<(sh-1) ROUND bias, add it, arithmetic-shift the 48-bit acc right by sh
shifted = (acc + round_bias) >> sh;
result  = SATURATE_s16(shifted);          // shl $0xf ; sar $0xf  -> clamp to signed 16-bit
// the lo/hi 16-bit halves are packed two-per-32-bit output word
```

The `packvrnr_16_48_32` sibling **omits** the `+1` round bias (truncating accumulator pack);
`packvru` is unsigned-saturate. The `8_24_32` / `32_48_32` / `32_96_32` variants drain the 24/48/96-bit
accumulators. This is the canonical MAC-accumulator → vec drain:
**arithmetic-right-shift (the scale) → round-half-up → saturating-clamp to the lane width** — and it
is the same family as the MX `cvtg48` accumulator→lane extract.
### 7.3 cas DECODE/TIMING — `IVP_PACKVR2NX24_issue` @ `0x71e4c0` (S1\_Ld, the pack unit)

```
reads  opnd_sem_wvec_addr (the wide accumulator)  at LAT 10
writes opnd_sem_vec_addr  (the result)            at LAT 12   (mov $0xc,%esi @0x71e514)
reads  opnd_sem_AR_addr   (the shift/scale amount) at LAT 3
```

The accumulator pack issues in `S1_Ld`, posts the vec result at **LAT 12**, and takes the shift
amount from an address register.
### 7.4 The widen / unpack side

The `cvt*_l` / `cvt*_h` half-extracts (`cvt48u_48_64l/h`, `cvt24u_24_32l/h`) and
`cvtg48_48_32_16_l/h` (@ `0x855b00`/`0x855b10`) are the accumulator **unpack** — the inverse of pack,
widening one wide value to two vec halves. The MX 4-bit nibble-unpack is the `ivp_sel2nx8i_s4`
sub-byte select (a lane-reorg, not a fiss arithmetic op).

---

## 8. The rounding modes (FSR-driven; RNE default)

### 8.1 The round-mode operand (fiss)

Every **rounding** soft-float convert (`float`/`ufloat`/`cvtf16`-narrow/`pack`) carries a round-mode
word in the `%edx`/`%ecx` "B" operand. The decode pattern (in `float` @ `0x87af07`, `cvtf16` @
`0x5b79e7`/`0x5b79f0`, `float16` @ `0x52212e`):

```c
and $0x1,r ; and $0x2,r ; cmp $0x3 ; sete   // mode==3 selector
test $0x7  ; cmp $0x3                        // guard | round | sticky
```

Bits `[1:0]` of the round-mode word select the behavior; the default (mode 0) is
round-to-nearest-even (the `cmp $0x3 ; sete` resolves the half-way tie to even). `[HIGH for the mode
word + RNE tie-break/OBSERVED; MED for the full 4-mode enum (RNE/RTZ/RPI/RMI) — bit positions read,
the full mode→behavior table not exhaustively enumerated.]`

### 8.2 The FSR special-register source (cas)

The round mode is the **FSR** (Floating-point Status Register, UR-id **`0xe9`**; `wur.fsr` =
`0x00f3e900`). The cas `RUR_FSR` op (@ `0x150c450`, `ivp_sem_rur_fcr_fsr_semantic`) reads it;
`wur.fsr` writes it. The stage-5 vector caller materializes the FSR copy from the per-op state
(`0xc0`/`0xd4(%rdi)`) into the xdref's round-mode argument:

```
opcode__ivp_float16nx16__stage_5 @0x7fd952:  mov 0xd4(%rdi),%eax ; … ; mov %eax,0xc(%rsp)
                                              -> staged into the per-lane xdref's %edx
```

Rounding is a software-visible mode in the FSR SR, plumbed per-op into the soft-float body — the
RoundMode-SR finding, now **pinned to the FSR (UR `0xe9`)** and OBSERVED in the convert path.

### 8.3 Scope

`trunc`/`utrunc` **ignore** the FSR mode (always RTZ, §3.2). The widen needs no rounding (lossless).
So the FSR round mode affects **only** the rounding converts: int→fp and fp32→fp16-narrow (and the
rounding pack). The Cast kernel's "RNE for fp targets" is exactly FSR default **mode 0**.

---

## 9. Special-value handling (inf / nan / denorm)

### 9.1 Classification (the IEEE field logic, every fp body)

| class | test (fp16 / fp32) |
|---|---|
| NaN | `exp == max (0x1f / 0xff)` **and** `mant != 0` |
| qNaN | mantissa MSB (`shr $0x9` fp16 / `shr $0x16` fp32) |
| Inf | `exp == max` **and** `mant == 0` (`0x7c00` / `0x7f800000`) |
| Zero | `exp == 0` **and** `mant == 0` |
| Denorm | `exp == 0` **and** `mant != 0` |

The dedicated `clsfy_16f_16f` @ `0x524b00` / `clsfy_32f_32f` @ `0x87dc60` emit the full multi-bit
IEEE class field (`is_nan/is_inf/is_zero/is_normal/is_subnormal/sign`).
### 9.2 Propagation on convert (byte-read *and* live-confirmed in §13)

* **Widen** (fp16→fp32): NaN → NaN (`0x1f → 0xff` exp, qNaN bit preserved); Inf → Inf
  (`mov $0x7f800000`); denormal fp16 → **normalized** fp32 (`bsr`).
* **Narrow** (fp32→fp16): NaN → fp16 NaN (`or $0x7c,%dh` = `0x7c00` + mantissa); Inf → `0x7c00`;
  **overflow** (exp too large for fp16) → fp16 Inf `0x7c00`; **underflow** → fp16 **denormal**
  (gradual, **not** flush-to-zero — explicit subnormal-output path).
* fp8 special encodings (E5M2 has Inf/NaN, E4M3 is NaN-only no-Inf) are handled by the MX saturating
  clamp + kernel rebias, **not** an ISS op (§5).

### 9.3 Denormal policy

The soft-float **handles denormals fully** — `bsr`-normalize on widen, gradual-underflow subnormal
output on narrow. **There is no flush-to-zero in the value path.** Whether a hardware FTZ bit exists
in the FSR is **not decided** here — the soft-float always computes full denormals; an FSR FTZ bit,
if present, is a separate mode not exercised by these bodies. `[HIGH for full-denormal soft-float;
the existence of an FTZ bit = LOW/NOT-DECIDED.]`

---

## 10. The cas TIMING (convert LAT, round-mode cost, structural ports)

Read by the issue-fn `mov $LAT,%esi` method (the value before each scoreboard `call`):

| op | issue unit | vec RESULT-LAT | src reads | struct ports (horizon `$0xe`=14) |
|---|---|---:|---|---|
| `FLOAT16NX16T` (i16→f16) | S3\_ALU | **13** (`mov $0xd`) | vec 10 | 1 |
| `UFLOAT16NX16T` (u16→f16) | S3\_ALU | 13 | vec 10 | 2 (extra port) |
| `FLOATN_2X32T` (i32→f32) | S3\_ALU | 13 | vec 10 | 1 |
| `TRUNCN_2XF32T` (f32→i32) | S3\_ALU | **12** (`mov $0xc`) | vec 10 | 2 |
| `TRUNC16NXF16T` (f16→i16) | S3\_ALU | 12 (`mov $0xc` @ `0x14b6a81`) | vec 10 | 2 |
| `CVTF16F32` (f32→f16) | S3\_ALU | 13 | vec 10 | **4** (2:1 width) |
| `CVTF16N_2XF32T` (f32→f16) | S3\_ALU | 13 | vec 10 | 4 |
| `PACKVR2NX24` (acc→vec) | S1\_Ld | 12 | wvec 10 | + AR 3 (shift) |

The `FLOAT16NX16T` issue fn @ `0x14b6860` carries `mov $0xa` (src LAT 10), `mov $0xd` (result LAT
13), `mov $0xe` (structural horizon 14).

Interpretation:

* INT→FP and FP-width converts post the vec result at **LAT 13** (one deeper than the LAT-10 vector
  ALU — the extra exp-rebias/normalize/round stages). FP→INT `trunc` posts at **LAT 12** (truncation
  is shallower than the rounding converts). The accumulator pack posts at **LAT 12**.
* The **fp-width converts use FOUR structural WB ports** — the fp32↔fp16 **2:1 register-width
  mismatch** occupies multiple writeback slots (an fp32 result is 2× the fp16 register footprint).
  The **unsigned** int→fp (`ufloat`) uses an **extra** structural port vs the signed.
* **The round mode has NO extra cas latency cost** — rounding is internal to the fiss body; the
  scoreboard sees the same LAT 13 regardless of FSR mode. cas timing is **mode-agnostic**; only the
  VALUE differs.
* As always, the cas scoreboard **never inspects the converted value** — it schedules reg#/LAT slots;
  the exp-rebias / round / saturate / special-value is the fiss xdref's job (§2–9).

---

## 11. Reconciliation with the Cast and MX kernels — end to end

**Cast** (POOL `0x47`): "convert through an FP32 intermediate, `numpy.astype` / RNE, narrowing
saturation". Reconciled leg by leg:

| Cast leg | ISS primitive |
|---|---|
| `in → FP32` | the widen / int→fp leaves: `cvtf32_1_32f_16f` (§4.2), `float`/`ufloat` (§2), + kernel bf16/fp8→fp32 rebias (§5) |
| `FP32 → out` (fp) | the narrow `cvtf16_…16f_32f` (§4.3) with **RNE** = FSR mode 0 (§8) |
| `FP32 → out` (int) | `trunc` (fp32→int, **round-toward-zero** = Cast's "C-truncation for int targets", §3) |
| narrowing sat | fp16 overflow→inf `0x7c00` (§9.2); fp8 → MX `bmin/bmax` clamp |

The FP32 universal hub is **exactly why** the ISS exposes only `fp16↔fp32 + int↔fp`: every other
dtype pair is two of these legs through fp32. `[HIGH/OBSERVED + Cast contract.]`

**MX dequant**: fp8 OUTPUT = `unpack(nibble) → ufloat(int→fp) → scale-multiply →
SATURATING-CLAMP (bmin/bmax to E5M2/E4M3) → cvtg48 accumulator-extract`. The `ufloat` is the §2
unsigned int→fp; `cvtg48` is the §7.4 extract; the saturate is a min/max (not a convert op); the
E2M1→E5M2 rebias is a kernel bitfield op. No dedicated fp8 convert op — confirming §5.
`[HIGH/OBSERVED + MX §7.]`

The dtype superset (BF16, FP16, FP32, FP8\_E3/E4/E5, FP4) is the Cast/MX matrix; the ISS
native-convert subset is `{INT8/16/32, UINT8/16/32, FP16, FP32}` — the rest via the hub.

---

## 12. Reconciliation ledger

* **vs the fiss value oracle** — confirms the `float`/`ufloat`/`trunc`/`cvtf16`/`cvtf32`/`sats`/`pack`
  roster; **refines** "rounding-mode source = `r8`/`r9` ctx [inferred RoundMode SR]" → **pinned to
  the FSR (UR `0xe9`)**, OBSERVED in the stage-5 materialization + the `RUR_FSR` op; confirms the
  integer-only soft-float (zero hw-FP) for every body read.
* **vs the MAC/pack sibling** — confirms the `packvr` accumulator→vec pack (round + sat, shift from
  AR); **refines** "`PACKL` = saturating-pack post" → the byte truth is `packl` = **WRAP** (`movzwl`
  low, `_nosat` byte-identical); the saturating post is `packv*`/`packs`/`sats`, the rounding post is
  `pack`/`packvr`.
* **vs the timing model** — confirms "some 11/12/13 for … CVT" with byte-exact LATs (int→fp /
  fp-width = 13, fp→int = 12, packvr = 12); adds the **4-structural-port** cost of the fp-width
  converts and the round-mode-agnostic cas timing.
* **vs the ISA encode roster** — confirms s-vs-u = distinct iclass (`float16nx16` `0x2f000401` vs
  `ufloat16nx16` `0x2f100400`, the `+0x100000` unsigned bit20), the `cvtf16f32`/`cvtf32f16`
  `+0x20000` direction field, the FSR UR-id `0xe9`.
* **vs Cast / MX** — the FP32-hub Cast and the MX fp8 path are **composed** from the §2/§3/§4 ISS
  primitives; the absence of bf16/fp8 ISS ops explains why both must route through fp32.

---

## 13. Live ISS-as-oracle — driving the fiss soft-float through `ctypes`

The value claims above are not inferred from disassembly alone — `libfiss-base.so` loads standalone
(`DT_NEEDED = libc.so.6` only) and the leaf `module__xdref_*` value functions are **called directly**.
This is the executable-oracle method that names this Part: the binary *is* the spec, and it is run.

### 13.1 A convert — `cvtf32_1_32f_16f` (fp16 → fp32 widen)

```python
import ctypes, struct
lib = ctypes.CDLL(".../libfiss-base.so")
f = lib.module__xdref_cvtf32_1_32f_16f          # ABI: (edi, esi=fp16_in, rdx=aux*, rcx=fp32_out*)
f.restype = None
f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
aux, res = ctypes.c_uint32(0), ctypes.c_uint32(0)
fp16 = lambda x: struct.unpack('<H', struct.pack('<e', x))[0]
for v in [1.0, -2.5, 65504.0, float('inf'), float('nan'), 2.0**-24]:
    f(0, fp16(v), ctypes.byref(aux), ctypes.byref(res))
```

Live output (each fp16 input encoded with Python's reference `<e`):

| fp16 in | input | fp32 out (live) | decoded | check |
|---|---|---|---|---|
| `0x3c00` | `1.0` | `0x3f800000` | `1.0` | exact |
| `0xc100` | `-2.5` | `0xc0200000` | `-2.5` | exact |
| `0x7bff` | `65504.0` (fp16 max) | `0x477fe000` | `65504.0` | exact |
| `0x7c00` | `inf` | `0x7f800000` | `inf` | Inf propagates |
| `0x7e00` | `nan` | `0x7fc00000` | `nan` | **qNaN bit set** |
| `0x0001` | smallest subnormal | `0x33800000` | `2^-24` | **denormal normalized** (`bsr`), not flushed |

The narrow's `mov $0x7f800000` Inf encode and the `bsr` subnormal-normalize path of §4.2 are
confirmed **at runtime**, not just from the bytes. `[HIGH/OBSERVED — live]`

### 13.2 A pack — `packl_16_48` (wrap) vs the saturate it is **not**

```python
packl = lib.module__xdref_packl_16_48           # ABI: (edi, rsi=in*, rdx=out*)
packl.restype = None
packl.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
i, o = ctypes.c_uint64(0), ctypes.c_uint32(0)
```

| 48-bit in | `packl` out (live) | low16 | a *saturate* would give |
|---|---|---|---|
| `0x…1234` | `0x00001234` | `0x1234` | `0x1234` |
| `0x…8001` | `0x00008001` | `0x8001` | `0x7fff` ← differs |
| `0x…ABCD` | `0x0000abcd` | `0xabcd` | `0x7fff`/clamp |
| `0x010000` | `0x00000000` | `0x0000` | `0x7fff` ← differs (wraps) |

`packl` returns the **low 16 with no clamp** — `0x8001` stays `0x8001`, `0x10000` wraps to `0x0000`.
`packl_16_48_nosat` (byte-identical) gives the same answers. This is decisive: **`packl` is the
WRAP form**, live-confirmed against the saturate it is sometimes mistaken for (§7.1 correction).
`[HIGH/OBSERVED — live]`

### 13.3 An int-width cvt — `sext_32_16`

```python
sext = lib.module__xdref_sext_32_16             # movswl %si,%esi ; mov %esi,(%rdx)
```

Live: `0x7fff → 32767`, `0x8000 → -32768`, `0xffff → -1`, `0x0001 → 1` — exact 16→32 sign-extension,
confirming the `movswl` leaf. `[HIGH/OBSERVED — live]`

---

## 14. Honesty / uncertainty ledger

**HIGH / OBSERVED (disassembled bytes + live `ctypes`):**

* int→fp = `float` (signed, `neg/test/js` prologue) / `ufloat` (no prologue); fp→int =
  `trunc`/`utrunc`, **round-toward-zero** (fractional bits discarded; **zero** `cmp $0x3` tie-breaks
  in the body). Integer-only soft-float.
* fp-width = fp16↔fp32 **only**; `CVTF32F16` = widen (`bsr`-normalize denorm, `shl $0xd` mantissa,
  `0x7f800000` inf), `CVTF16F32` = narrow (`add $0x1f90` rebias, RNE round block, `0x7c00`
  overflow→inf, gradual-underflow denorm). **Both live-driven.**
* **no bf16 / fp8 native convert** (negative control: 0 of 20 368 fiss exports carry a bf16/fp8
  token; 0 cas BF16/FP8 issue fns); bf16/fp8 via the FP32 hub (Cast) / unpack+scale (MX).
* rounding = FSR-driven (UR `0xe9`; `RUR_FSR` @ `0x150c450`; stage-5 materializes the `0xc0`/`0xd4`
  state into the xdref `%edx` arg); RNE default (`cmp $0x3 ; sete` tie-break); `trunc` ignores the mode.
* PACK: `packl` = WRAP (`movzwl`, `_nosat` byte-identical, live `0x8001→0x8001`), `pack`/`packvr` =
  ROUND (`1<<(sh-1)` bias + shift), `packv*`/`sats` = SATURATE.
* special values: full IEEE classify; NaN/Inf propagate; overflow-narrow → inf `0x7c00`;
  underflow-narrow → fp16 denormal (no FTZ in the value path).
* cas TIMING: int→fp & fp-width LAT 13, fp→int LAT 12, packvr LAT 12; src LAT 10; fp-width 4
  structural ports; `ufloat` extra port; round-mode-agnostic cas LAT. ENCODE: `cvtf16f32`
  `0x27b050f0` / `cvtf32f16` `0x27d050f0`; `float16nx16` `0x2f000401` / `ufloat16nx16` `0x2f100400`.

**MED / INFERRED:** the full FSR round-mode enum (RNE/RTZ/RPI/RMI → bits `[1:0]`) — the mode word and
the RNE tie-break are OBSERVED, only RNE (mode 0) traced; `utrunc` negative-input → clamp-to-0
(inferred from the unsigned target, not byte-traced); the bf16/fp8 kernel rebias being "trivial
bitfield" (inferred from format geometry + the absence of an ISS op).

**LOW / NOT DECIDED:** whether an FSR flush-to-zero bit exists (the soft-float always computes full
denormals; an FTZ bit, if any, is not exercised by these bodies); the exact full bit-layout of the
FSR beyond bits `[1:0]` = round-mode.

> **NOTE — validation coverage.** The Part-15 validation pages (named VAL-04, VAL-17, VAL-18 in the
> survey) will replay these converts and packs across the full dtype matrix against the firmware
> Cast/MX golden vectors; this page provides the per-op value/timing contract they check against.

---

*Method, for downstream reproduction:* fiss VALUE entries — `float_1_32f` @ `0x87aec0`, `ufloat` @
`0x87b0d0`, `float16` @ `0x522070`; `trunc_…32f_32` @ `0x87b240`, `trunc16` @ `0x522610`;
`cvtf32_1_32f_16f` @ `0x5b77f0` (widen), `cvtf16_…16f_32f` @ `0x5b78f0` (narrow); `clsfy_16f` @
`0x524b00`, `clsfy_32f` @ `0x87dc60`; `sext_32_16` @ `0x858680`, `zeroext_48_32` @ `0x85a950`;
`sats_16_32` @ `0x5baaa0`, `sats_8_16` @ `0x8711a0`; `packl_16_48` @ `0x5e95e0` (wrap),
`packl_16_48_nosat` @ `0x85a940`, `pack` @ `0x82cd10` (round), `packvr_16_48_32` @ `0x5e98b0` (acc
round+sat), `cvtg48_48_32_16_l/h` @ `0x855b00`/`0x855b10`. cas TIMING issue fns —
`FLOAT16NX16T` @ `0x14b6860`, `TRUNC16NXF16T` @ `0x14b69d0`, `CVTF16F32` @ `0x14b47b0`,
`PACKVR2NX24` @ `0x71e4c0`, `RUR_FSR` @ `0x150c450`. ENCODE thunks (libisa-core) — `cvtf16f32` @
`0x353680`, `cvtf32f16` @ `0x353620`, `float16nx16` @ `0x350bc0`, `ufloat16nx16` @ `0x350c20`.
Direction convention: cas `CVT<DST><SRC>`; round mode = FSR (UR `0xe9`), bits `[1:0]`, default RNE;
`trunc` = always round-to-zero. All from static RE of the shipped `libcas-core.so` +
`libfiss-base.so` + `libisa-core.so` plus live `ctypes` execution (DMCA 1201(f) lawful
interoperability).
