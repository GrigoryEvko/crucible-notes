# VAL — Convert / Pack / Cast Family

This page is the **differential-validation certificate** for the width-changing datapath of the
Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD engine: the **convert / pack / cast** kernel family. It
applies the [4-oracle bit-exact method](four-oracle-method.md) — **GX-SEM** (an x86-body model
lifted this pass from the disassembly), **GX-FLIX** (the device-assembled + device-decoded FLIX
bundle), **nki-0.3.0** (the shipped numpy reference simulator), and **libfiss-base** driven
**LIVE** via `ctypes` — to *prove* every sign-fill, zero-fill, width-boundary, saturation, and
round-vs-truncate behaviour of the family **bit-exact** against the shipped vendor binary, on an
identical width-boundary / sign-fill / saturation / round-vs-truncate edge + fuzz corpus.

The arbiter is **leg (d)**: the real shipped `libfiss-base.so` is `dlopen`'d and each
`module__xdref_*` value leaf is **called on every input** (the binary computes the result; the
SEM model is checked against it, never the reverse). Every value claim below carries
`[HIGH/OBSERVED·exec]` where it was computed by the binary in-process this pass, per the
[confidence model](../reference/confidence-model.md).

This is the **value-and-edge** companion to four encoding/semantics pages, each of which it
cross-validates:

* [B10 wvec-pack](../isa/ref/b10-wvec-pack.md) — the integer wide-accumulator repack
  (`pack*`, `cvt32s24`, `cvt48snx32`) encoding/slot semantics;
* [B13 sp-cvt](../isa/ref/b13-sp-cvt.md) — fp32 convert + the five fp32 round-to-integral ops;
* [B20 hp-cvt](../isa/ref/b20-hp-cvt.md) — the fp16-typed convert/round side;
* and the control/ISS view [cas convert/pack/fp](../iss/cas-convert-pack-fp.md) plus the
  firmware [cast-copy kernel](../firmware/kernels/cast-copy.md) that *calls* these leaves.

> **NOTE — the binary and the address arithmetic this page uses.** `libfiss-base.so`
> (`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`, gitignored — reach it with an
> absolute path or `fd --no-ignore`): ELF64 x86-64, **not stripped**, **12,330,016 B**, sha256
> `260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94` (re-hashed this pass). Its
> `module__xdref_*` value leaves are in `.text`, **VMA == file-offset** (so every address below
> is both an objdump start and a symbol address — `nm` re-read this pass). The device toolchain
> (`xtensa-elf-as`/`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`, GNU binutils 2.34.20200201)
> assembles + decodes the FLIX bundle (§6, the GX-FLIX leg); device mnemonics come from
> `libisa-core.so` strings. `[HIGH/OBSERVED]`

> **GOTCHA — these are x86-64 host leaves, not device code.** `libfiss-base.so` is the *host
> ISS value oracle*; its bodies are x86-64 (`movsbw`, `cmova`, `shr %cl`). The device IVP32
> mnemonics (`ivp_packpnx48`, `ivp_cvtf32f16`) live in the FLIX leg (§6). Leg (d) computes the
> *value*; leg (b) binds the *device encoding* to that value. Do not disassemble `libfiss-base.so`
> expecting Xtensa. `[HIGH/OBSERVED]`

---

## 1. The family, the leaf census, and the validated subset

The convert/pack/cast family is the **width-changing** part of the datapath: it moves a value
between the 8/16/24/32/48-bit integer containers (and fp16↔fp32) with one of three *fill*
disciplines (zero-fill, sign-fill, saturating clamp) or rounds an accumulator down with one of two
*round* disciplines (round-half-up, truncate). `nm` grounds the census against the **864**-leaf
`module__xdref_*` oracle (`nm libfiss-base.so | rg -c module__xdref_` = **864**, this pass):

| Sub-family | Symbols (representative) | `nm -c` count | Discipline |
|---|---|---|---|
| `sext_*` (sign-fill widen) | `sext_16_8`, `sext_32_16`, `sext_32_8`, `sext_2_1`, `sext_4_1` | **5** | replicate sign bit |
| `zeroext_*` (zero-fill widen) | `zeroext_16_8`, `zeroext_32_16`, `zeroext_48_32` | **3** | high bits → 0 |
| `cvt[0-9]*` (narrow/widen) | `cvt16{s,u}`, `cvt24{s,u}`, `cvt32{s,u}`, `cvt48{s,u}` | **34** | sign-ext / mask / sats-wrap |
| `sats_*` (saturating narrow) | `sats_8_16`, `sats_16_24`, `sats_16_32`, `sats_32_48` | **4** | CLAMP to {MIN,MAX} |
| `pack*` (accumulator repack) | `pack`, `packp/packq/packl/packm`, `packvr/packvrnr/packvru` | **56** | round-half-up / truncate |
| fp round-to-integral | `fitrunc/ficeil/fifloor/firound/firint` (×{16f,32f}) | **10** | 5 modes × 2 widths |
| fp↔int / fp↔fp convert | `cvtf32_1_32f_16f`, `cvtf16_…_16f_32f_2`, `trunc_1_1_32_32f_32` | — | widen/narrow/round |

> **QUIRK — the family is 110 leaves but the prefixes do not partition it.** Two leaves the
> family needs are *not* under an obvious prefix: the fp→int `module__xdref_trunc_1_1_32_32f_32`
> (`@0x87b240`, a `trunc_*` name, fp32→int32) and the fp16→fp32 widen
> `module__xdref_cvtf32_1_32f_16f` (`@0x5b77f0`, a `cvtf*` name). Counting `sext`+`zeroext`+`cvt[0-9]`
> +`sats`+`pack` gives **102**; adding the 10 fp round-to-integral + the fp-convert leaves brings the
> validated family to **110**. A reimplementer who greps only `cvt`/`pack` *misses the fp boundary
> leaves*. `[HIGH/OBSERVED·nm]`

---

## 2. The recovered semantics (leg a — lifted from this-pass disassembly)

Each leaf body below was re-disassembled this pass with plain `objdump -d` at the `nm` address.
The pseudocode names the real symbol and is what leg (a) computes; leg (d) (the live binary) is the
authority it is checked against.

### 2.1 SEXT / ZEROEXT — the sign-fill vs zero-fill axis `[HIGH/OBSERVED]`

```
module__xdref_sext_16_8   @0x870bc0 : movsbw %sil,%si ; movzwl %si,%esi ; mov %esi,(%rdx)
        // s8 -> s16: sign-extend the byte, then hold in a 16-bit lane.
module__xdref_sext_32_16  @0x858680 : movswl %si,%esi                    // s16 -> s32
module__xdref_sext_32_8   @0x815b10 : movsbl %sil,%esi                   // s8  -> s32
module__xdref_sext_2_1    @0x8573e0 : shl 31 ; sar 31 ; and 0x3          // bit0 -> 2-bit field
module__xdref_sext_4_1    @0x8579e0 : shl 31 ; sar 31 ; and 0xf          // bit0 -> 4-bit field
module__xdref_zeroext_16_8@0x870c30 : mov %esi,(%rdx)                    // bare copy (caller pre-bounds the byte)
module__xdref_zeroext_48_32@0x85a950: low = value ; high word(s) = 0
```

The disassembly of `sext_16_8`'s tail (`mov %esi,(%rdx); ret`) was re-read this pass; the entry
`movsbw`/`movzwl` precede it. The `sext_2_1`/`sext_4_1` "replicate-bit0" idiom (`shl 31; sar 31`)
broadcasts bit0 to all bits, then masks to the destination field width — a sub-byte sign fill.

### 2.2 CVT narrow / widen — the three narrow disciplines `[HIGH/OBSERVED]`

```
module__xdref_cvt32s_32_24 @0x5ba860 : shl $8 ; sar $8 ; mov %esi,(%rdx)
        // SIGN-EXTEND FROM bit23: in(0xff800001) <<8=0x80000100, >>8(arith)=0xff800001.
module__xdref_cvt24u_24_32 @0x5ba870 : and $0xffffff ; mov %esi,(%rdx)   // zero-extend, mask to 24
module__xdref_cvt24s_24_16 @0x5ba850 : movswl %si ; and 0xffffff         // SIGN-EXTEND low16 into 24-bit container
module__xdref_cvt24u_24_16 @0x5ba840 : bare mov                          // zero-extend
module__xdref_cvt16s_16_24{l,h}, cvt32s_32_48{l,h} : thin wrappers that CALL sats_16_24 / sats_32_48
        // => the SIGNED narrow-cvt IS the saturating narrow (§2.3).
```

> **CORRECTION — `cvt24s_24_16` is a sign-*extend*, not a clamp.** It narrows a 24-bit container
> *down to* 16 bits' worth of signed value but *expresses* it back in the 24-bit container by
> **sign-extending the low 16**: LIVE `cvt24s_24_16(0x008000) = 0xff8000` (low16 `0x8000` is s16
> −32768, sign-extended to 24-bit negative), `0x00ffff = 0xffffff` (low16 −1), `0x800000 = 0x0000`
> (low16 = 0). A reimplementer who *clamps* here diverges on every input whose low-16 high bit is
> set. `[HIGH/OBSERVED·exec]` (this is the cvt32 phantom of the convert family, extended.)

### 2.3 SATS_* saturating narrow — the CLAMP `[HIGH/OBSERVED]`

All four share the **redundant-sign-bits** overflow test: broadcast the destination sign bit and
compare it against the bits being dropped; if those are pure sign-extension the value is in range,
else clamp to {pos_max, neg_min} with the **source sign bit preserved at top**. The
`sats_8_16` body (`@0x8711a0`), re-disassembled this pass:

```asm
sats_8_16 @0x8711a0:
    movzbl %sil,%eax        ; candidate low byte
    shl    $0x18,%ecx       ; signbyte = (x<<24)
    shr    $0x8,%edi        ; hi = x>>8
    sar    $0x1f,%ecx       ;          >>31 (arith)  -> all-ones/zero from bit7
    movzbl %cl,%ecx
    cmp    %ecx,%edi        ; in-range iff hi == signbyte
    je     8711d0           ; -> passthrough x&0xff
    shr    $0xf,%esi        ; else: test bit15 for sign
    xor    %ecx,%ecx ; mov %edi,%eax ; test %esi,%esi ; sete %cl
    and    $0x80,%eax       ; top = hi & 0x80  (source sign bit)
    neg    %ecx ; and $0x7f,%ecx     ; pos ? 0x7f : 0x00
    or     %ecx,%eax        ; result = top | (pos?0x7f:0)
    mov    %eax,(%rdx) ; ret
```

```
sats_16_24 @0x5ba6e0 : signbyte=(x<<16)sar31 ; hi=x>>16 ; in-range iff hi==signbyte
                       else result = ((x>>8)&0x8000) | (((x>>23)==0)?0x7fff:0)
sats_16_32 @0x5baaa0 : signword=(x<<16)sar31 ; hi=x>>16 ; in-range iff hi==signword
                       else result = ((~x sar31)&0x7fff) | (hi&0x8000)
sats_32_48 @0x5ba760 : ptr-ABI {lo@*rsi, hi16@*(rsi+4)} ; signword=lo sar31 ;
                       in-range iff hi==signword else clamp {0x7fffffff,0x80000000}
```

### 2.4 PACK core — the round-half-up engine `[HIGH/OBSERVED]`

`module__xdref_pack @0x82cd10`, re-disassembled this pass (the canonical round-half-up repack):

```asm
pack @0x82cd10:                         ; (rsi=&acc, rdx=shift, rcx=out)
    mov  %rcx,%r8
    je   82cd25                         ; if shift==0 -> rb=0 (skip bias)
    lea  -0x1(%rdx),%edi ; mov $1,%al
    and  $0xf,%edi ; mov %edi,%ecx
    shl  %cl,%eax                       ; rb = 1 << ((shift-1)&0xf)   <-- round-half-up bias
82cd25:
    add  (%rsi),%eax                    ; acc_word0 + rb
    mov  %edx,%ecx
    and  $0x7fffffff,%eax               ; mask to 31 bits
    shr  %cl,%eax                       ; >> shift   (LOGICAL: 31-bit value)
    and  $0xffff,%eax                   ; narrow to 16
    mov  %eax,(%r8) ; ret
```

So `pack(acc,shift) = (((acc_word0 + rb) & 0x7fffffff) >> shift) & 0xffff` with
`rb = (shift==0) ? 0 : 1<<((shift-1)&0xf)`. Wrappers:

```
packp_16_48_nosat @0x82cd40 : calls pack with fixed shift = 0x0a (10)
packq_16_48_nosat @0x82cdb0 : calls pack with fixed shift = 0x0f (15)
packp_16_48 @0x82cd80, packq_16_48 @0x82cdf0 : wrap _nosat with a final (xstate-gated) saturate
packl_16_48 @0x5e95e0 : movzwl (%rsi) -> low-16 TRUNCATE (no bias) ; packm = mid-word
```

### 2.5 PACKVR / PACKVRNR / PACKVRU — variable-shift, the round-vs-truncate distinction `[HIGH/OBSERVED]`

```
packvr_16_24_32   @0x5e9610 : sh = min(shift,0x17) ; v = sign_extend24(acc) ;
                              rb = (1<<sh)>>1 ;                  // ROUND-HALF-UP bias
                              r  = arith_shift_right(v + rb, sh) ;
                              clamp r to int16: >0x7fff->0x7fff ; <-0x8000->0x8000 ; else r&0xffff
packvrnr_16_24_32 @0x5e97d0 : IDENTICAL pipeline, rb OMITTED (pure truncate / floor) ;
                              distinct wide-shift (>0x17) saturate-to-sign branch
packvru_16_24_32  @0x5e9700 : round bias as packvr, UNSIGNED clamp (overflow -> 0xffff, neg -> 0)
```

> **GOTCHA — the `*vr/*vrnr/*vru` accumulator is passed BY VALUE in `%esi`, not by pointer.**
> Unlike the wide `pack` core (which reads `(%rsi)`), these variable-shift leaves take a `u32`
> accumulator *by value* in `%esi`, the shift in `%edx`, the out pointer in `%rcx`. A driver that
> passes a pointer reads garbage (a found-and-fixed harness false-positive, §8). `[HIGH/OBSERVED]`

### 2.6 fp round-to-integral and fp→int (legs c/d — full IEEE-754 soft-float) `[HIGH/OBSERVED]`

```
fitrunc/ficeil/fifloor/firound/firint_1_32f_32f @0x87d8e0/0x87d3c0/0x87d5a0/0x87d760/0x87d9d0 :
        extract exponent (x>>23), build the integral mantissa, apply the named rounding rule,
        re-pack fp. Two pointer outputs (flags @ rdx, result @ rcx) for the fi{trunc,ceil,floor,round}.
firint_1_1_32f_32f_2 @0x87d9d0 : RNE; the ROUNDED VALUE is in the THIRD out-pointer (r9), not rcx.
trunc_1_1_32_32f_32 @0x87b240 (fp32->int) : THREE pointer outs (rcx,r8,r9). The int result is in
        slot r9 as a Q1 fixed-point (= int*2); int(r9/2) toward zero recovers the integer.
        Round rule = TRUNCATE-toward-zero (== numpy astype int32).
cvtf32_1_32f_16f @0x5b77f0 : fp16 -> fp32 widen (exact, no rounding).
```

> **NOTE — the bit-precise soft-float SEM (leg a) for the `fi*`/`cvtf*`/`trunc` leaves is
> deferred** to the [fp soft-float page](fp-soft-float.md); here legs c/d cross-validate the round
> modes by **execution + numpy reference**, which is sufficient to *separate* the modes and
> root-cause the divergences (§5). `[MED/INFERRED]`

---

## 3. Live oracle — exact `ctypes` binds (leg d)

The bodies above are *checked against* the live binary. The ABI signatures recovered this pass
(`restype = None`, all outputs via pointer):

| Leaf | Recovered C-ABI | Out slot |
|---|---|---|
| `pack` | `(void* xstate, u32* acc, u32 shift, u32* out)` | `*out` |
| `packvr/packvrnr/packvru_16_24_32` | `(void* xstate, u32 acc_BYVAL, u32 shift, u32* out)` | `*out` |
| `sats_8_16 / _16_24 / _16_32` | `(void* xstate, u32 val, u32* out)` | `*out` |
| `sats_32_48` | `(void* xstate, u32* {lo,hi16}, u32* out)` | `*out` |
| `cvtf32_1_32f_16f` | `(void* xstate, u32 fp16, u32* flags, u32* out)` | `*out` |
| `firound/fitrunc/ficeil/fifloor_1_32f_32f` | `(void* xstate, u32 fp32, u32* flags, u32* out)` | `*out` |
| `firint_1_1_32f_32f_2` | `(void* xstate, u32 fp32, u32, u32* o1, u32* o2, u32* o3)` | `*o3` (RNE value); `o1` = flag |
| `shift_amt_satu_32 / _23` | `(void* xstate, u32 amt, u32* out)` | `*out` |

`xstate` may be `NULL` for the pure-value leaves (the value path does not deref it; the `packp`
*saturation enable* is the documented exception, §4 / §7). `[HIGH/OBSERVED·exec]`

---

## 4. The four references — exactly how each computes a result

| Leg | Reference | What it computes |
|---|---|---|
| **(a) GX-SEM** | python from §2 (disassembly-lifted) | the recovered x86-body model |
| **(b) GX-FLIX** | device `xtensa-elf-as` assemble + `xtensa-elf-objdump` decode | raw-bytes → mnemonic identity (§6) |
| **(c) nki** | `result.astype(dst)` in the shipped `nki-0.3.0` simulator | numpy narrow = **truncate-wrap**; fp→int = truncate-toward-zero |
| **(d) libfiss LIVE** | `ctypes` dlopen of the real `libfiss-base.so` | the **arbiter** — the binary's own arithmetic |

> **GOTCHA — the nki narrow path has NO saturating-narrow primitive.** The simulator cast path is
> literally `result.astype(int8/int16)` = numpy **mod-2ⁿ truncate-wrap**. The hardware `sats_*` /
> signed `cvtNs` is a **CLAMP**. nki routes dtype-narrowing through the Vector/DVE engine, so leg
> (c) *legitimately* diverges on every overflow (§5.2) — a property of the nki API surface, not a
> validation weakness. `[HIGH/OBSERVED]`

---

## 5. Agreement tables — the result (SEM ≡ LIVE; divergences root-caused)

### 5.1 Integer convert/pack — SEM(a) vs LIVE(d), edge + ~4,000/op fuzz

| Leaf | Class | Agree | Mismatch |
|---|---|---|---|
| `sext_16_8` | sign-fill 8→16 | 4016/4016 | 0 |
| `sext_32_16` | sign-fill 16→32 | 4016/4016 | 0 |
| `sext_32_8` | sign-fill 8→32 | 4016/4016 | 0 |
| `cvt32s_32_24` | sext-from-bit23 | 4014/4014 | 0 |
| `cvt24s_24_16` | sext 16→24 | 4016/4016 | 0 |
| `cvt24u_24_16` | zext 16→24 | 4016/4016 | 0 |
| `sats_8_16` | CLAMP 16→8 | 4016/4016 | 0 |
| `sats_16_24` | CLAMP 24→16 | 4011/4011 | 0 |
| `sats_16_32` | CLAMP 32→16 | 4014/4014 | 0 |
| `packp` (sh 10) | round-half-up | 4007/4007 | 0 |
| `packq` (sh 15) | round-half-up | 4007/4007 | 0 |
| `packl_16_48` | truncate | 4007/4007 | 0 |
| `packvr_16_24_32` | var-shift ROUND+SAT | 8086/8086 | 0 |
| `packvrnr_16_24_32` | var-shift TRUNCATE | 8086/8086 | 0 |

**TOTAL SEM(a) vs LIVE(d): ~64,000 lane comparisons, 0 mismatch.** `[HIGH/OBSERVED·exec]`

### 5.2 SATURATING-NARROW — the 4-way differential (the nki divergence)

These are the **live** `sats_8_16` outputs reproduced this pass (cf. §2.3 body); nki's
`astype(int8)` wraps where the hardware clamps:

| input | SEM(a) | **LIVE(d)** | nki(c) | verdict |
|---|---|---|---|---|
| `0x0050` | 0x50 | **0x50** | 0x50 | AGREE (in range) |
| `0x007f` | 0x7f | **0x7f** | 0x7f | AGREE (+max) |
| `0x0080` (+128) | 0x7f | **0x7f** | 0x80 | a=d CLAMP \| c WRAP |
| `0x0100` (+256) | 0x7f | **0x7f** | 0x00 | a=d CLAMP \| c WRAP |
| `0x7fff` (+32767) | 0x7f | **0x7f** | 0xff | a=d CLAMP \| c WRAP |
| `0xff80` (−128) | 0x80 | **0x80** | 0x80 | AGREE |
| `0xff00` (−256) | 0x80 | **0x80** | 0x00 | a=d CLAMP \| c WRAP |
| `0xffff` (−1) | 0xff | **0xff** | 0xff | AGREE |
| `0x8000` (−32768) | 0x80 | **0x80** | 0x00 | a=d CLAMP \| c WRAP |

`sats_16_32` LIVE this pass: `0x00008000`→`0x7fff`, `0xffff8000`→`0x8000`, `0x0000ffff`→`0x7fff`.
**SEM and LIVE agree bit-exact on every case (the CLAMP); nki(c) diverges only on overflow** —
root cause: numpy astype = truncate-wrap, hardware = clamp. `[HIGH/OBSERVED·exec]`

### 5.3 PACK ROUND vs TRUNCATE — the central distinction (SEM ≡ LIVE on both)

The headline of this page. The `packvr` (round-half-up + signed-sat) and `packvrnr` (pure
truncate) columns are the **live** outputs reproduced this pass — they differ *precisely* when the
dropped fraction's MSB is set (`rb = 1<<(sh-1)`) or when rounding pushes across the SAT clamp:

| (acc, shift) | `packvr` (round+sat) | `packvrnr` (truncate) | round ≠ trunc? |
|---|---|---|---|
| `(0x7fffff, 2)` | **0x7fff** (clamp) | **0xffff** | YES |
| `(0x800000, 1)` | **0x8000** (−min) | **0x0000** | YES |
| `(0x000018, 5)` | **0x0001** | **0x0000** | YES (the `+rb` bit) |
| `(0x123456, 8)` | **0x1234** | **0x1234** | no (no dropped MSB-frac) |
| `(0xffffff, 3)` | **0x0000** | **0xffff** | YES |

`(0x000018, 5)` is the cleanest case: `rb = 1<<4 = 0x10`; `(0x18+0x10)>>5 = 0x28>>5 = 1` (round)
vs `0x18>>5 = 0` (truncate). The **pack core** was also driven live: `pack(0x18,5)=0x0001`,
`pack(0x200,10)=0x0001`, `pack(0x123456,8)=0x1234` — all match the round-half-up arithmetic.
**SEM(a) ≡ LIVE(d) on BOTH columns for all 8,086 (acc,shift) cells.** `[HIGH/OBSERVED·exec]`

### 5.4 fp round-to-integral and fp→int — LIVE(d) vs numpy(c)

The **live** `firound` vs `firint` outputs reproduced this pass (the *silent rounding mismatch*
hunted in the report **"Silent Mismatches in Rounding / Saturation Edge Cases"**):

| x | `firound` (half-away) | `firint` (RNE / half-even) |
|---|---|---|
| +0.5 | **+1.0** | **+0.0** |
| +1.5 | **+2.0** | **+2.0** |
| +2.5 | **+3.0** | **+2.0** |
| +3.5 | **+4.0** | **+4.0** |
| +4.5 | **+5.0** | **+4.0** |
| −0.5 | **−1.0** | **−0.0** |
| −2.5 | **−3.0** | **−2.0** |

`fitrunc/ficeil/fifloor` agree bit-exact with numpy `trunc/ceil/floor` (e.g. LIVE
`ceil(+2.3)=+3, floor(+2.3)=+2, trunc(−2.3)=−2`). **The only divergences vs `np.round` are the
even-integer ties, and they are FIROUND** (round-half-away) vs numpy's round-half-even (= FIRINT's
rule). `fp32→int32` (`trunc_1_1_32_32f_32`) is bit-exact with numpy truncate-toward-zero
(2,013/2,013) once the leaf's **Q1 fixed-point scale** is removed (`int(r9/2)` toward zero).
`[HIGH/OBSERVED·exec]`

> **CORRECTION — FIROUND ≠ FIRINT; they are distinct `xdref` roots.** `firound_1_32f_32f`
> (`@0x87d760`) is **round-half-away-from-zero**; `firint_1_1_32f_32f_2` (`@0x87d9d0`) is
> **round-half-to-EVEN (RNE)**. A reimplementer who uses ONE round-to-integral routine for both is
> wrong on **every** `.5` tie whose integer part is even. Compounding it: `firint` returns the
> rounded value in its **THIRD** out-pointer (`r9`), `firound` in the first (`rcx`); copying the
> `firound` ABI onto `firint` reads a flag word, not the value. `[HIGH/OBSERVED·exec]`

### 5.5 The shift-amount clamp — the 32-flush trap

`module__xdref_shift_amt_satu_32` (`@0x80e7e0`, body `cmp $0x20,%esi ; mov $0x20,%eax ; cmova
%eax,%esi ; mov %esi,(%rdx)`) — `cmova` = conditional-move if **unsigned-above** — gates the shift
of every pack/narrow. LIVE this pass:

| amt | `0` | `31` | `32` | `33` | `0x7fffffff` | `0x80000000` | `0xffffffff` |
|---|---|---|---|---|---|---|---|
| out | 0 | 31 | **32** | **32** | **32** | **32** | **32** |

`shift_amt_satu_23` clamps to 23 identically (`24→23`, `0xffffffff→23`).

> **GOTCHA — `x >> 32` on x86/ARM is `x >> 0` (mask), in C it is undefined.** Firmware first
> **unsigned-clamps** `amt` to `[0,32]` (so any high-bit-set "negative" shift lands ABOVE 32 and
> clamps to the cap), *then* performs a real 32-position arithmetic shift (flush-to-sign). A
> reimplementer using a raw `>>` diverges on **every** shift ≥ 32 and on any amount with the high
> bit set. `[HIGH/OBSERVED·exec]`

---

## 6. The FLIX leg (b) — device-assembled + device-decoded

The pack/convert mnemonics come from `libisa-core.so` strings: `ivp_packlnx48`, `packmnx48`,
`packpnx48`, `packqnx48`, `packvnx48`, `packvrnx48`, `packvrnrnx48`, `packvru2nx24`, `cvt24s2nx16`,
`cvt32s24`, `cvt48snx32`, `cvtf16f32`, `cvtf32f16`, `sats*` (the device names of the §2 leaves).
The bundle (`flix.s`) assembled by `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) and decoded bit-exact
by `xtensa-elf-objdump` (the GX-FLIX oracle):

```
 0:  { nop; ivp_packlnx48      v2, wv0 }
10:  { nop; ivp_packpnx48      v2, wv0 }
18:  { nop; ivp_packqnx48      v2, wv0 }
30:  { nop; ivp_packvrnx48     v2, wv0, a3 }
38:  { nop; ivp_packvrnrnx48   v2, wv0, a3 }
40:  { nop; nop; nop; ivp_cvtf32f16  v2, v0 }
```

Raw `.text` bytes (LE): `2fc51438 2962a402` (packl), `2fc52038 2962a402` (packp), `2fc52438
2962a402` (packq), `2fc53838 29629402` (packvr), `2fc53838 28629402` (packvrnr), `2f4580e0
20175032` (cvtf32f16). raw-bytes → mnemonic reproduced bit-exact.

> **NOTE — the regfile + the one-nibble round selector.** The narrow pack reads its accumulator
> from the wide-vector regfile `wv` (the 48-bit accumulator) and writes the NX16 regfile `v`; the
> variable-shift packs take their shift from a scalar AR `a`. **`packvr` vs `packvrnr` differ in
> exactly ONE encoding nibble** — the 5th byte, `0x29` (round) vs `0x28` (no-round) — the
> round/no-round selector. This binds the §5.3 value distinction to the encoding. `[HIGH/OBSERVED]`

---

## 7. Divergence ledger + root cause

| # | Divergence | Legs | Root cause | Status |
|---|---|---|---|---|
| — | integer convert/pack/cast core | a vs d | none — bit-exact ~64,000 cmp | **AGREE** |
| 1 | nki saturating-narrow | c vs a/d | numpy `astype` = truncate-wrap; hw = CLAMP; nki has no sat-narrow primitive | ROOT-CAUSED |
| 2 | `FIROUND` vs `np.round` | c vs d | FIROUND = half-away; `np.round` = half-even (= FIRINT's rule); distinct ops | ROOT-CAUSED |
| 3 | `packp` "always saturates"? | model vs d | packp saturation is **xstate-gated** (NULL xstate ⇒ WRAP); packv* saturation is unconditional per-variant | ROOT-CAUSED |
| 4 | shift `x>>32` | model vs d | native `>>` masks to 5 bits; hw unsigned-clamps amt to [0,32] then flushes | ROOT-CAUSED |

> **QUIRK — `packp` saturation is state-bit, `packv*` saturation is leaf-variant.** With a NULL
> xstate, `module__xdref_packp_16_48` (the "sat" entry) == `packp_16_48_nosat` — both *wrap*
> (`33554432 → 0x8000`, not clamped to `0x7fff`); the clamp-enable is read from the xstate at
> runtime. By contrast `packvr`/`packvru` clamp **unconditionally** by leaf-variant
> (signed-sat / unsigned-sat), and `packvnr`/`packvrnr` never clamp (wrap). Conflating the two sat
> models silently diverges on every overflow input. `[MED/OBSERVED·exec]`

> **NOTE — no stochastic-rounding value leaf exists.** `nm` of the 864-leaf oracle shows NO leaf
> matching `round|rne|rtz|rint|stoch|rnd` beyond the four `firound`/`firint` roots, and none taking
> an RNG-seed input. The deterministic modes the value oracle exposes are exactly: `firound`
> (half-away), `firint` (RNE), `packvr` (half-up), `packv*/packp` (floor or wrap), and the
> saturating narrows. Any MX/quantize stochastic rounding lives **outside** this value oracle.
> `[MED/INFERRED]`

---

## 8. Harness-side false positives — found, fixed, arbitrated by the live binary

In the spirit of the method (the live binary is always the arbiter), four *driver* bugs surfaced
and were corrected this pass — none was a firmware divergence:

1. **`packvr/packvrnr/packvru` accumulator passed by value, not pointer.** Initial driver passed a
   pointer and read garbage; corrected to `%esi` by-value (§2.5) ⇒ 100% agreement.
2. **fp→int `trunc` leaf has THREE pointer outputs + a Q1-scaled result in `r9`.** A single-out
   call segfaulted; binding three outs and dividing `r9` by 2 ⇒ bit-exact.
3. **`sats` SEM lift initially took the clamp sign from the wrong bit** (bit7/bit15 instead of
   bit15/bit31); re-read against the asm (§2.3) ⇒ 0 mismatch.
4. **`sats_16_24` was fed out-of-domain 32-bit inputs** (it is a 24-bit-input narrow); restricting
   the corpus to the 24-bit domain removed 2 spurious mismatches.

`[HIGH/OBSERVED]`

---

## 9. Adversarial self-verify — the five strongest claims re-challenged

1. **"`pack` core is round-half-up `rb=1<<(sh-1)`."** Re-challenge: could the `>>` be arithmetic
   (sign-fill)? The body `and $0x7fffffff` *before* `shr %cl` clears bit31, so the shift operates
   on a 31-bit non-negative value — it is **logical**, and the bias is added before it. Confirmed
   against live `pack(0x18,5)=0x0001` (round); the same input is `0x0000` under truncate.
   `[HIGH/OBSERVED·exec]`
2. **"`packvr` rounds + sat, `packvrnr` truncates."** Re-challenge: is the difference real or a
   harness artifact? Live `(0x000018,5)`: `packvr=0x0001`, `packvrnr=0x0000` — they DIFFER on the
   same input, and the FLIX encoding differs in exactly the 5th nibble (`0x29` vs `0x28`). Both
   the value and the encoding separate them. `[HIGH/OBSERVED·exec]`
3. **"FIROUND = half-away, FIRINT = RNE."** Re-challenge: is FIRINT's `+0.0` on `+0.5` an ABI
   artifact (reading the wrong slot)? The value `+0.0` (RNE of 0.5) appears in `r9` and is the
   *correct RNE result*; `firound(+2.5)=+3` but `firint(+2.5)=+2` confirms half-even independently.
   `[HIGH/OBSERVED·exec]`
4. **"cvtf32 fp16→fp32 is exact: 1.0→0x3f800000, qNaN→0x7fc00000."** Re-challenge: is qNaN
   canonicalised or passed through? Live `cvtf32_1_32f_16f(0x7e00 fp16-qNaN) = 0x7fc00000`
   (canonical fp32 qNaN), `0x3c00→0x3f800000`, `0x7c00(+inf)→0x7f800000`, `0x3800(0.5)→0x3f000000`.
   Exact widen, qNaN canonicalised. `[HIGH/OBSERVED·exec]`
5. **"shift_amt_satu_32 unsigned-clamps to 32, not mask-mod-32."** Re-challenge: could `0xffffffff`
   wrap to a small value? Live `0xffffffff→32`, `0x80000000→32`, `0x21→32` — `cmova` (unsigned
   above) sends every high-bit-set or >32 value to exactly 32. A masked `>>` would give `0`.
   `[HIGH/OBSERVED·exec]`

All five survive re-challenge against the binary. The single most consequential **CORRECTION**
this page asserts is in §5.4: a reimplementer's natural "one round() routine" for fp
round-to-integral is **silently wrong on every even-integer `.5` tie** — `FIROUND` (half-away) and
`FIRINT` (round-half-even) are *distinct* shipped leaves, and `firint` additionally returns its
result in a *different* output pointer (`r9`, not `rcx`).

---

## 10. Verdict

`[HIGH/OBSERVED·exec]` The convert/pack/cast family is **bit-exact between the GX-SEM lift (a)
and the LIVE shipped `libfiss-base.so` (d)** over an edge + ~4,000-per-op random corpus —
**~64,000 lane comparisons, 0 mismatch** — covering sext/zeroext (sign vs zero fill),
`cvtNs`/`cvtNu` (sign-extend-from-bitN vs mask), `sats_*` (saturating CLAMP narrow with
source-sign preserved), the pack core round-half-up (`rb = 1<<(sh-1)`), `packl` truncate, and the
variable-shift `packvr` (round+SAT) vs `packvrnr` (truncate) — the round-vs-truncate distinction
**proven and separated op-for-op**, and bound to the device encoding by the one-nibble FLIX
selector (`0x29`/`0x28`). The fp boundary is driven live: `cvtf32` fp16→fp32 is exact
(`1.0→0x3f800000`, `qNaN→0x7fc00000`); `FIROUND` is round-half-away, `FIRINT` is RNE; `fp32→int32`
is truncate-toward-zero (2,013/2,013). Four reimplementer traps are root-caused — nki truncate-wrap
vs hardware CLAMP, FIROUND≠FIRINT, xstate-gated `packp` vs unconditional `packv*` saturation, and
the shift-amount unsigned-clamp-to-32 flush — and four harness-side false positives were arbitrated
by the live binary. **Zero firmware value defects**; every divergence is the reimplementer/model,
never the binary.
