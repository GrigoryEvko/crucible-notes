# EUP Correction Coefficients

> *Every offset, `u32`, and float on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA minus `0x200000` equals its file offset. Every coefficient `u32` below was confirmed by `struct.unpack` on the file at the cited `.rodata` offset. Other libtpu builds will differ.*

## Abstract

The [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) page establishes the central structural fact: on an EUP-capable generation the nine `V*Decomposed` builders emit a **bare hardware push + bare pop and nothing else** — the raw hardware transcendental is the answer, with no inline correction polynomial. This page is the *numeric* complement to that encoding view. It catalogs, byte for byte, the fp32 `.rodata` magic numbers that the **software** transcendental code loads, in the two places coefficients actually live:

1. **The shared Newton / rational-minimax refinement helpers** that a few accuracy-sensitive functions wrap around the raw push, or use as the software path: `EmitRecpNrIteration`, `EmitRsqrtNrIteration`, `EmitTanhPolyApproximation`, `EmitAtan2Approximation`. These are the `recip`/`rsqrt` Newton steps and the `tanh`/`atan2` rationals.
2. **The `*NoEupF32` software fallbacks** that implement a transcendental entirely in VALU when a generation or datatype lacks the hardware EUP: `VexpNoEupF32`, `Vexpm1NoEupF32`, `VlnNoEupF32`, `Vln1pNoEupF32`, `Vlog2NoEup`, `VtanhNoEupF32`. These exist because there is no hardware EUP `exp` or `ln` — only `pow2` and `log2` push — so `exp`/`expm1`/`ln`/`ln1p` are built either by scaling the hardware `pow2`/`log2` push (the `*Eup` variants) or, where no EUP exists, by a full Cody-Waite / mantissa-extract polynomial.

This is the reimplementation specification for the TPU's transcendental accuracy: the exact rational `P(x²)/Q(x²)` for `tanh`, the Cody-Waite `ln2` split and 5-term Taylor for `exp`, the 9-coefficient mantissa-extract Horner for `log2`, and the Newton iteration constants. It also pins the one EUP-result-FIFO quantity that *is* a libtpu literal — the per-`TpuVersion` `kErf` FIFO depth from `ResultFifoEntryCount` — and the push→pop latency edge those coefficients are software-pipelined against.

Every constant pool entry is shared aggressively: `0x84a2444` (`1.0`) and `0x84a2dc8` (`log2e`) appear in five functions each. The offsets below are therefore the authoritative identity of each constant; the same physical word is consumed by different VALU ops in different functions.

For reimplementation, the contract is:

- The `recip` Newton step `y·(2 − x·y)` (one constant) and the `rsqrt` step `y·(1.5 − 0.5·x·y²)` (two constants).
- The `tanh` rational `x·P(x²)/Q(x²)`: clamp window, small-`|x|` linear threshold, the 7-coefficient numerator `P`, the 4-coefficient denominator `Q`, the `±1` saturation — all in `.rodata` load order.
- The `atan2` 8-coefficient odd Horner plus the four quadrant constants (`π/2`, `π`, `π/4`, `3π/4`) and the NaN/inf sentinels.
- The `exp`/`expm1`/`ln`/`ln1p`/`log2` no-EUP polynomials with their full coefficient lists and offsets, plus the `*Eup` variants' scale/correction constants.
- The `kErf` EUP-result-FIFO depth per generation, and the push→pop latency edge the correction window hides.

| | |
|---|---|
| **Refinement emitters** | `jellyfish::Emit{Recp,Rsqrt}NrIteration`, `EmitTanhPolyApproximation`, `EmitAtan2Approximation` |
| **No-EUP fallbacks** | `LloRegionBuilder::V{exp,expm1,ln,ln1p,tanh}NoEupF32`, `(anon)::Vlog2NoEup` |
| **EUP-using variants** | `LloRegionBuilder::V{exp,expm1,ln1p}Eup` — scale/correct over the HW `pow2`/`log2` push |
| **Constant pool** | `.rodata` `0x84a2400`..`0x84a3040`; VMA == file offset; fp32 (`vmovss` RIP-relative) |
| **`tanh` rational** | `x·P(x²)/Q(x²)`, deg `P` = 6 (7 coeffs), deg `Q` = 3 (4 coeffs) |
| **`exp` method** | `2^(x·log2e)`, Cody-Waite `ln2` split, 5-term Taylor, `VextractExponent`/`VcomposeF32` |
| **`log2` method** | mantissa `AND 0x7fffff` / `OR 0x3f800000` → `[1,2)`, sqrt2 split, 9-coeff Horner |
| **EUP result FIFO depth** | `ResultFifoEntryCount(kErf, ver)` = `{4, 4, 32, 16, 16, 16}` per `TpuVersion` 0..5 |
| **Push→pop latency edge** | JF/DF 4, PF 7, VF 6, GL 13/14 — the depth the correction software-pipeline fills |

---

## Newton-Raphson Refinements

Two shared emitters produce the Newton refinement of a coarse `recip`/`rsqrt` seed. They are not transcendental implementations on their own — they refine an existing estimate `y` of `1/x` or `1/sqrt(x)` toward full fp32 accuracy, and are called by `VrecpNr` (division / high-accuracy reciprocal) and the rsqrt accuracy modes. Both take the argument `x` and the current estimate `y`; both return one improved estimate.

```c
function EmitRecpNrIteration(x, y):       // jellyfish::EmitRecpNrIteration @0x1d5a9ec0
    c = VimmF32(1.0)                       //  0x84a2444
    t = VmulF32(x, y)                      //  x·y
    t = VsubF32(c, t)                      //  1 − x·y
    t = VmulF32(y, t)                      //  y·(1 − x·y)
    return VaddF32(y, t)                   //  y + y·(1 − x·y) = y·(2 − x·y)

function EmitRsqrtNrIteration(x, y):      // jellyfish::EmitRsqrtNrIteration @0x1d5a9e20
    h = VimmF32(0.5)                        //  0x84a27e8
    t = VmulF32(x, y)                       //  x·y
    t = VmulF32(t, y)                       //  x·y²
    t = VmulF32(h, t)                       //  0.5·x·y²
    one_half = VimmF32(1.5)                 //  0x84a2680
    t = VsubF32(one_half, t)                //  1.5 − 0.5·x·y²
    return VmulF32(y, t)                    //  y·(1.5 − 0.5·x·y²)
```

| role | function | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|---|
| `1.0` | `EmitRecpNrIteration` | `0x3f800000` | 1.0 | `0x84a2444` | CERTAIN |
| `0.5` | `EmitRsqrtNrIteration` | `0x3f000000` | 0.5 | `0x84a27e8` | CERTAIN |
| `1.5` | `EmitRsqrtNrIteration` | `0x3fc00000` | 1.5 | `0x84a2680` | CERTAIN |

`VrecpNr` (`@0x1d5580c0`) wraps `EmitRecpNrIteration` with special-value saturation: it abs-masks with the integer constant `0x7fffffff` (a `VectorU32Constant`, not a `.rodata` fp32) and clamps overflow/underflow to `2^126` (`0x7e800000` @ `0x84a3004`) and `2^−126` (`0x00800000` @ `0x84a2f64`), selecting on NaN/inf through `SimplifyVweird`.

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| `2^126` saturation hi | `0x7e800000` | 8.507059e+37 | `0x84a3004` | CERTAIN |
| `2^−126` saturation lo | `0x00800000` | 1.175494e−38 | `0x84a2f64` | CERTAIN |

---

## Tanh Rational — `tanh(x) ≈ x·P(x²)/Q(x²)`

`EmitTanhPolyApproximation` (`@0x1d5a9f40`) is the no-EUP software `tanh`; `VtanhNoEupF32` (`@0x1d535b20`) is a thin thunk into it (byte-confirmed: it loads no constants of its own and tail-calls the emitter). The structure, read directly from the decompiled `VimmF32`/`VmulAddF32`/`VdivF32` chain:

```c
function EmitTanhPolyApproximation(x):    // @0x1d5a9f40
    t   = VclampFloat(x, -9.0, +9.0)       //  0x84a283c / 0x84a2a84
    small = VltF32(VabsF32(t), 4e-4)       //  0x84a2758  (linear-region predicate)
    x2  = VmulF32(t, t)

    // NUMERATOR: 7-coefficient Horner P(x²), low→high, seed c0 then 6 FMAs, then ×x
    acc = VimmF32(c0)
    for ck in [c1, c2, c3, c4, c5, c6]:
        acc = VmulAddF32(x2, acc, ck)       //  acc = acc·x² + ck
    num = VmulF32(t, acc)                    //  x·P(x²)   — the odd factor

    // DENOMINATOR: 4-coefficient Horner Q(x²), low→high, seed d0 then 3 FMAs
    den = VimmF32(d0)
    for dk in [d1, d2, d3]:
        den = VmulAddF32(x2, den, dk)        //  den = den·x² + dk

    q = VdivF32(num, den)                    //  x·P(x²) / Q(x²)
    r = Vselect(small, t, q)                 //  return x in the linear region
    return VclampFloat(r, -1.0, +1.0)        //  0x84a26cc / 0x84a2444
```

The `VdivF32` operand trace settles the parity: the accumulator that is multiplied by the clamped `x` (forming the *odd* function `x·P`) is the numerator; the bare *even* Horner is the denominator. This makes `tanh` odd by construction.

Numerator `P(x²)` — 7 coefficients, Horner low→high in `x²`, whole `P` then `×x`:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| c0 | `0xa59f25c0` | -2.7607683663038313e-16 | `0x84a2654` | CERTAIN |
| c1 | `0x2a61337e` | 2.0001879384549948e-13 | `0x84a2428` | CERTAIN |
| c2 | `0xaebd37ff` | -8.604671836165423e-11 | `0x84a2ef4` | CERTAIN |
| c3 | `0x335c0041` | 5.122297253024044e-08 | `0x84a27d8` | CERTAIN |
| c4 | `0x3779434a` | 1.4857223504805006e-05 | `0x84a24f0` | CERTAIN |
| c5 | `0x3a270ded` | 0.0006372619536705315 | `0x84a242c` | CERTAIN |
| c6 | `0x3ba059dc` | 0.004893524572253227 | `0x84a24a0` | CERTAIN |

Denominator `Q(x²)` — 4 coefficients, Horner low→high in `x²`:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| d0 | `0x35a0d3d8` | 1.1982583600911312e-06 | `0x84a2e8c` | CERTAIN |
| d1 | `0x38f895d6` | 0.00011853470641653985 | `0x84a2658` | CERTAIN |
| d2 | `0x3b14aa05` | 0.0022684347350150347 | `0x84a2564` | CERTAIN |
| d3 | `0x3ba059dd` | 0.0048935250379145145 | `0x84a27dc` | CERTAIN |

Brackets and saturation:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| clamp lo | `0xc1100000` | -9.0 | `0x84a283c` | CERTAIN |
| clamp hi | `0x41100000` | +9.0 | `0x84a2a84` | CERTAIN |
| small thresh | `0x39d1b717` | 0.00039999998989515007 | `0x84a2758` | CERTAIN |
| saturate −1 | `0xbf800000` | -1.0 | `0x84a26cc` | CERTAIN |
| saturate +1 | `0x3f800000` | 1.0 | `0x84a2444` | CERTAIN |

> **NOTE —** c6 (`0x3ba059dc`) and d3 (`0x3ba059dd`) differ in only the LSB. The leading numerator and denominator coefficients are intentionally near-equal so `x·P/Q → ±1` as `|x| → 9` (the saturation limit); this is the rational's high-order asymptote and is the reason the outer `[−1, +1]` clamp is exact at the boundary. They are byte-confirmed *distinct* constants, not a transcription artifact.

---

## Atan2 — 8-Coefficient Odd Horner + Quadrant Constants

`EmitAtan2Approximation` (`@0x1d5aa1c0`) computes `atan2(y, x)` from the ratio `r = min(|x|,|y|) / max(|x|,|y|)`, an 8-coefficient Horner in `r²` multiplied by `r`, then a quadrant/sign correction. The Horner is read from the alternating `VimmF32` / `VmulF32` + `VaddF32` chain (the emitter uses explicit mul+add rather than FMA here).

```c
function EmitAtan2Approximation(y, x):    // @0x1d5aa1c0
    r2  = VmulF32(r, r)                     //  r = min/max
    acc = VimmF32(p0)
    for pk in [p1, p2, p3, p4, p5, p6, p7]:
        acc = VaddF32(VmulF32(acc, r2), pk) //  Horner in r²  (8 coeffs total)
    core = VaddF32(VmulF32(VmulF32(acc, r2), r), r)   //  r·(1 + r²·Horner)
    // quadrant fix-ups: subtract from π/2 if |x|<|y|, reflect through π / π/4 / 3π/4,
    // NaN/inf sentinels, final Vcopysign(core, y)
```

Polynomial `P(r²)` — 8 coefficients, Horner low→high:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| p0 | `0x3b369013` | 0.0027856871020048857 | `0x84a2594` | CERTAIN |
| p1 | `0xbc81f96a` | -0.015866000205278397 | `0x84a2e38` | CERTAIN |
| p2 | `0x3d2df75a` | 0.042472220957279205 | `0x84a2520` | CERTAIN |
| p3 | `0xbd998ca7` | -0.07497530430555344 | `0x84a278c` | CERTAIN |
| p4 | `0x3dda01d4` | 0.10644879937171936 | `0x84a2d24` | CERTAIN |
| p5 | `0xbe117ae1` | -0.14207030832767487 | `0x84a300c` | CERTAIN |
| p6 | `0x3e4cbba4` | 0.19993454217910767 | `0x84a2790` | CERTAIN |
| p7 | `0xbeaaaa6c` | -0.33333146572113037 | `0x84a26dc` | CERTAIN |

Quadrant constants and sentinels:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| `π/2` | `0x3fc90fdb` | 1.5707964 | `0x84a29d0` | CERTAIN |
| `π` | `0x40490fdb` | 3.1415927 | `0x84a2e3c` | CERTAIN |
| `π/4` | `0x3f490fdb` | 0.78539819 | `0x84a25f8` | CERTAIN |
| `3π/4` | `0x4016cbe4` | 2.3561945 | `0x84a28f8` | CERTAIN |
| NaN | `0x7fc00000` | nan | `0x84a2ff8` | CERTAIN |
| inf | `0x7f800000` | inf | `0x84a2a34` | CERTAIN |

> **NOTE —** `π/4` is loaded from `0x84a25f8`, which is offset `+0xC` into the 16-byte aligned constant pair at `0x84a25ec`. The word at `+0x8` of that pair (`0x84a25f4`, `0x3f317218` = `ln2` = 0.6931472) is the `ln2` multiplier used by `VlnNoEupF32` and `Vln1pEup` (below). The pair packs `π/4` and `ln2` adjacently — a constant-pool layout detail worth reproducing only if a reimplementer mirrors the exact RIP-relative addressing.

---

## No-EUP Software Fallbacks

When a generation or datatype has no hardware EUP transcendental, the `*NoEupF32` family implements the function in pure VALU. There is **no** hardware EUP `exp` or `ln` push at all (only `pow2`/`log2`), so these are the only `exp`/`expm1`/`ln`/`ln1p` paths; `tanh`/`log2` have both a hardware push and a no-EUP fallback.

### `exp` — `VexpNoEupF32` (`@0x1d533820`)

`exp(x) = 2^(x · log2e)`, range-clamped, with a Cody-Waite `ln2` split to keep the fractional reduction accurate, a 5-term Taylor polynomial for `2^f` on the reduced fraction `f ∈ [−0.5, 0.5]`, and exponent reconstruction through `VextractExponent` / `VcvtF32ToS32` / `VcomposeF32`. Overflow saturates to the clamp-hi `inf`-producing path; underflow is selected away by the clamp-lo comparison.

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| `1.0` | `0x3f800000` | 1.0 | `0x84a2444` | CERTAIN |
| `0.5` | `0x3f000000` | 0.5 | `0x84a27e8` | CERTAIN |
| clamp hi | `0x42b1722d` | 88.7229995727539 | `0x84a2734` | CERTAIN |
| clamp lo | `0xc2aeac4f` | -87.33654022216797 | `0x84a2878` | CERTAIN |
| log2e | `0x3fb8aa3b` | 1.4426950216293335 | `0x84a2dc8` | CERTAIN |
| `ln2`-hi (Cody-Waite) | `0xbf318000` | -0.693359375 | `0x84a2d28` | CERTAIN |
| `ln2`-lo (Cody-Waite) | `0x395e8083` | 0.00021219444170128554 | `0x84a2d7c` | CERTAIN |
| Taylor t0 | `0x3efffffc` | 0.49999988079071045 | `0x84a2ebc` | CERTAIN |
| Taylor t1 | `0x3e2aaa47` | 0.166665181517601 | `0x84a2794` | CERTAIN |
| Taylor t2 | `0x3d2aadcc` | 0.04166965186595917 | `0x84a2b18` | CERTAIN |
| Taylor t3 | `0x3c091de6` | 0.008368944749236107 | `0x84a26e4` | CERTAIN |
| Taylor t4 | `0x3ab42872` | 0.001374496379867196 | `0x84a2ca4` | CERTAIN |

### `expm1` — `Vexpm1NoEupF32` (`@0x1d533ec0`)

A separate `expm1(x) = exp(x) − 1` implementation with its own coefficient set (sharing `log2e`, the Cody-Waite `ln2` words, and the `88.723` clamp-hi by constant-pool reuse) plus a tighter small-`x` bound `−17.32868` and `{−1, 0.5, −0.5, 2}` selector constants for the `expm1`-specific result correction.

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| clamp lo | `0xc18aa123` | -17.328680038452148 | `0x84a3038` | CERTAIN |
| clamp hi | `0x42b17218` | 88.72283935546875 | `0x84a2ce8` | CERTAIN |
| log2e | `0x3fb8aa3b` | 1.4426950216293335 | `0x84a2dc8` | CERTAIN |
| `ln2`-hi | `0x3f317200` | 0.693145751953125 | `0x84a2f44` | CERTAIN |
| `ln2`-lo | `0x35bfbe8e` | 1.428606765330187e-06 | `0x84a282c` | CERTAIN |
| Taylor (t·1) | `0x3e2aaa6f` | 0.16666577756404877 | `0x84a26ac` | CERTAIN |
| Taylor (t·2) | `0x3d2aaab6` | 0.041666708886623383 | `0x84a2754` | CERTAIN |
| Taylor (t·3) | `0x3c09055f` | 0.008363096974790096 | `0x84a2fe0` | CERTAIN |
| Taylor (t·4) | `0x3ab654c9` | 0.0013910765992477536 | `0x84a2c54` | CERTAIN |
| selector −1 | `0xbf800000` | -1.0 | `0x84a26cc` | CERTAIN |
| selector +0.5 | `0x3f000000` | 0.5 | `0x84a27e8` | CERTAIN |
| selector −0.5 | `0xbf000000` | -0.5 | `0x84a28f4` | CERTAIN |
| selector 2.0 | `0x40000000` | 2.0 | `0x84a2850` | CERTAIN |
| inf | `0x7f800000` | inf | `0x84a2a34` | CERTAIN |

### `log2` — `Vlog2NoEup` (`@0x1d556a60`)

The base-2 logarithm. The mantissa is extracted by integer masking (`AND 0x7fffff` clears the exponent, `OR 0x3f800000` forces it to `[1, 2)`), a sqrt2 split (`1.4142135` @ `0x84a2524`) recenters the significand to `[1/√2, √2)` and adjusts the integer exponent by ±1, then a 9-coefficient Horner in `(m − 1)` produces the fractional `log2`, and the integer exponent (cvt `S32→F32`) is added back. Exceptions: `x == 0 → −inf`, `x < 0 → NaN`, handled via `SimplifyVweird` + `VcmpHelper` selects.

```c
function Vlog2NoEup(x):                    // @0x1d556a60
    m   = (x AND 0x7fffff) OR 0x3f800000   //  significand in [1, 2)
    e   = SplitF32_exponent(x)             //  integer exponent
    // sqrt2 recentering: if m > 1.4142135 (0x84a2524) then m·=0.5, e+=1
    f   = m - 1.0                          //  0x84a2444
    acc = VimmF32(p0)
    for pk in [p1..p8]:
        acc = VmulAddF32(f, acc, pk)        //  9-coeff Horner in (m−1)
    return cvtS32ToF32(e) + f·acc           //  log2(x)
```

9-coefficient Horner in `(m − 1)`, low→high:

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| p0 | `0x3e013d7b` | 0.12621109187602997 | `0x84a2c14` | CERTAIN |
| p1 | `0x3e5c9fc9` | 0.21545328199863434 | `0x84a2598` | CERTAIN |
| p2 | `0xbe540971` | -0.20706726610660553 | `0x84a28fc` | CERTAIN |
| p3 | `0xbe74b2ad` | -0.23896284401416779 | `0x84a29d4` | CERTAIN |
| p4 | `0x3e936e69` | 0.28795173764228821 | `0x84a26e0` | CERTAIN |
| p5 | `0xbeb8ae28` | -0.36070370674133301 | `0x84a2874` | CERTAIN |
| p6 | `0x3ef639b7` | 0.4809090793132782 | `0x84a2684` | CERTAIN |
| p7 | `0xbf38aa38` | -0.72134733200073242 | `0x84a2a3c` | CERTAIN |
| p8 | `0x3fb8aa3b` | 1.4426950216293335 | `0x84a2dc8` | CERTAIN |

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| sqrt2 split | `0x3fb504f3` | 1.4142135381698608 | `0x84a2524` | CERTAIN |
| `−inf` (x==0) | `0xff800000` | -inf | `0x84a2578` | CERTAIN |
| NaN (x<0) | `0x7fc00000` | nan | `0x84a2ff8` | CERTAIN |

The integer masks `0x7fffff` (mantissa) and `0x3f800000` (set-exponent-1) are `LloModule::VectorU32Constant` immediates (`esi`-carried), not `.rodata` fp32 loads; `p8` (`0x84a2dc8`) is the same physical word as the `exp` `log2e`.

### `ln` — `VlnNoEupF32` (`@0x1d534740`)

`ln(x) = Vlog2NoEup(x) · ln2`. The function is a two-line wrapper: it calls `Vlog2NoEup` then multiplies by `ln2`.

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| `ln2` | `0x3f317218` | 0.6931471824645996 | `0x84a25f4` | CERTAIN |

### `log1p` — `Vln1pNoEupF32` (`@0x1d534a20`)

`log1p(x) = ln(1 + x)` with an 8-coefficient Horner specialized for the near-1 argument (plus a 9th coefficient `−0.5` consumed in the `x²` recombination), and a Cody-Waite `ln2` split applied through a `VpairFloatAddF32` double-word reconstruction. The mantissa/exponent path mirrors `Vlog2NoEup`; the difference is the input is `1 + x` and the polynomial is in the recentered significand.

8-coefficient Horner (low→high):

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| q0 | `0xbd43a4d3` | -0.0477646104991436 | `0x84a2560` | CERTAIN |
| q1 | `0x3dda59bb` | 0.10661645978689194 | `0x84a2940` | CERTAIN |
| q2 | `0xbe066c58` | -0.13127267360687256 | `0x84a2e00` | CERTAIN |
| q3 | `0x3e13d018` | 0.14434850215911865 | `0x84a27d4` | CERTAIN |
| q4 | `0xbe2a7741` | -0.16647054255008698 | `0x84a2498` | CERTAIN |
| q5 | `0x3e4cbc51` | 0.1999371200799942 | `0x84a2fe4` | CERTAIN |
| q6 | `0xbe800036` | -0.25000160932540894 | `0x84a2838` | CERTAIN |
| q7 | `0x3eaaaabf` | 0.33333393931388855 | `0x84a2b50` | CERTAIN |

| role | u32 | f32 | .rodata off | Confidence |
|---|---|---|---|---|
| recombine `−0.5` | `0xbf000000` | -0.5 | `0x84a28f4` | CERTAIN |
| `ln2`-hi | `0x3f317200` | 0.693145751953125 | `0x84a2f44` | CERTAIN |
| `ln2`-lo | `0x35bfbe8e` | 1.428606765330187e-06 | `0x84a282c` | CERTAIN |
| `−inf` (1+x==0) | `0xff800000` | -inf | `0x84a2578` | CERTAIN |
| NaN (1+x<0) | `0x7fc00000` | nan | `0x84a2ff8` | CERTAIN |

### Function Map

| Function | Address | Method | Confidence |
|---|---|---|---|
| `EmitRecpNrIteration` | `0x1d5a9ec0` | recip Newton `y·(2−xy)` | CERTAIN |
| `EmitRsqrtNrIteration` | `0x1d5a9e20` | rsqrt Newton `y·(1.5−0.5xy²)` | CERTAIN |
| `EmitTanhPolyApproximation` | `0x1d5a9f40` | tanh rational `x·P(x²)/Q(x²)` | CERTAIN |
| `EmitAtan2Approximation` | `0x1d5aa1c0` | atan2 8-coeff odd Horner + quadrant | CERTAIN |
| `VexpNoEupF32` | `0x1d533820` | `2^(x·log2e)` Cody-Waite + 5-term Taylor | CERTAIN |
| `Vexpm1NoEupF32` | `0x1d533ec0` | expm1 own 4-coeff Taylor + selectors | CERTAIN |
| `Vlog2NoEup` | `0x1d556a60` | mantissa extract + 9-coeff Horner | CERTAIN |
| `VlnNoEupF32` | `0x1d534740` | `log2 · ln2` | CERTAIN |
| `Vln1pNoEupF32` | `0x1d534a20` | log1p 8-coeff Horner + Cody-Waite | CERTAIN |
| `VtanhNoEupF32` | `0x1d535b20` | thunk → `EmitTanhPolyApproximation` | CERTAIN |
| `VrecpNr` | `0x1d5580c0` | recip Newton + saturation wrapper | CERTAIN |

---

## EUP-Using Variants — Scale / Correct Over the Hardware Push

`exp`, `expm1`, and `ln1p` also have *EUP-using* variants that lean on the hardware `pow2`/`log2` push and apply only a small fp32 scale or correction around it. These are used on EUP-capable gens where the hardware transcendental is available but the desired function (`exp`/`ln1p`) is not a direct hardware op.

```c
function VexpEup(x):                        // @0x1d556080
    if ShouldUseBf16FloatOps(x):
        s = VectorU32Constant(0x3fb93fb9)    //  packed bf16 log2e pair (two bf16 values)
        t = VmulBf16(x, s)                   //  op 0x15b kVectorMultiplyBf16
        return CreateVectorUnop(0x146, t)    //  kVectorPow2Bf16AndPop (fused HW pow2)
    else:
        t = VmulF32(x, log2e)                //  0x84a2dc8 = 1.4426950
        return Vpow2(t)                      //  HW pow2 push + pop
```

| function | address | HW push consumed | scale / correction constant(s) | Confidence |
|---|---|---|---|---|
| `VexpEup` | `0x1d556080` | `0x146` `kVectorPow2Bf16AndPop` (bf16) / `Vpow2` (f32) | f32 log2e `0x3fb8aa3b` @ `0x84a2dc8`; bf16-packed log2e `0x3fb93fb9` (`VectorU32Constant`) | CERTAIN |
| `Vexpm1Eup` | `0x1d5561a0` | `Vexp` (which uses HW pow2) | `1.0` @ `0x84a2444`, `0.5` @ `0x84a27e8`, small-x thresh `0x3c4c7ad2` = 0.012480455 @ `0x84a2fbc` | CERTAIN |
| `Vln1pEup` | `0x1d557340` | `Vlog2` (HW log2 push) | `1.0` @ `0x84a2444`, `−0.5` @ `0x84a28f4`, `ln2` `0x3f317218` @ `0x84a25f4`, correction `0x39e81ecb` = 0.00044273 @ `0x84a3008` | CERTAIN |

`Vexpm1Eup` computes `exp(x) − 1` via the EUP `exp` then a small-argument correction; for tiny `|x|` (below the `0.012480455` threshold) it falls back to a `tanh`-style series rather than subtracting 1 from a near-1 value (catastrophic cancellation guard). `Vln1pEup` does `ln1p(x) = log2(1+x)·ln2 + correction·x` with the EUP `log2` push.

> **NOTE —** `VexpEup`'s bf16 path loads `0x3fb93fb9` through `VectorU32Constant`, not a `.rodata` fp32 — it is two bf16 `log2e` values (`0x3fb9`) packed into one 32-bit lane, the lane-width-2 form of the f32 `log2e` `0x3fb8aa3b`. A reimplementer targeting bf16 must pack the bf16 constant, not load the f32 one twice. See [EUP Lane-Width / Unpack](eup-lane-width-unpack.md) for the sub-lane packing model.

---

## The EUP Result FIFO Depth and the Latency Edge

The coefficients above are software-pipelined against the EUP push→pop latency: a `V*Decomposed` push that the late decomposer separates from its pop leaves a window of bundles that the scheduler must fill with the Newton/poly correction VALU work (or unrelated independent work). Two libtpu literals bound that window — a **per-generation FIFO depth** and a **per-generation latency edge**.

### `kErf` FIFO Depth — `ResultFifoEntryCount(kErf, ver)`

Unlike the runtime `EupResultFifoEntry` proto list (a HW-state *snapshot*, not a sizing constant — see [ResultFifo / ArchRegister Enums](../isa/resultfifo-archregister.md)), the physical EUP-result-FIFO depth **is** a libtpu literal. `ResultFifoEntryCount` (`@0x1d631520`, the 25-arm switch over `ResultFifo`) resolves the `kErf` ordinal (`0x12`) through a per-`TpuVersion` `int[]` table at `.rodata` `0xb53e270`, gated on `TpuVersion < 6`:

```c
case kErf:                                  // ResultFifo ordinal 0x12
    if (version >= 6) Fatal("invalid platform type")
    return (uint32_t[6]){4, 4, 32, 16, 16, 16}[version]   // .rodata @0xb53e270
```

| `TpuVersion` | generation | `kErf` depth | Confidence |
|---|---|---|---|
| 0 | Jellyfish | 4 | CERTAIN (value); HIGH (gen binding) |
| 1 | Dragonfish | 4 | CERTAIN; HIGH |
| 2 | Pufferfish | 32 | CERTAIN; HIGH |
| 3 | Viperfish | 16 | CERTAIN; HIGH |
| 4 | Ghostlite (V5e/V6e-class) | 16 | CERTAIN; HIGH |
| 5 | `6acc60406` (TPU7x-class) | 16 | CERTAIN (value); HIGH (gen binding) |

The six depth values `{4, 4, 32, 16, 16, 16}` were read byte-exactly from `0xb53e270`. The `TpuVersion → generation` ordering is not labeled in this function; it is the same `TpuVersion` enum the rest of the cost model indexes (ordinal 0 = Jellyfish, per [ResultFifo / ArchRegister Enums](../isa/resultfifo-archregister.md)). A strong consistency signal supports the binding: the `TpuVersion 0`/`1` depth of `4` exactly equals the Jellyfish/Dragonfish push→pop latency clamp of `4` (below) — on the legacy gens the FIFO holds exactly one latency-window's worth of in-flight EUP results.

> **NOTE —** Two distinct EUP-FIFO quantities exist and must not be conflated. The runtime `EupResultFifoEntry` proto is a `repeated`-message *snapshot* of in-flight results (a hardware-state dump, no fixed size). `ResultFifoEntryCount(kErf, ver)` is the separate *compile-time depth* the scheduler enforces — and that one **is** a libtpu literal: the `int[6]` `{4, 4, 32, 16, 16, 16}` at `0xb53e270`.

### The Push→Pop Latency Edge

The latency edge is the depth the correction software-pipeline fills. The legacy `Jellyfish`/`Dragonfish` path clamps the edge to `Performance[+0x30] = 4` in `LatencyTableJellyfish::LatencyBetweenInternal` (`@0x1c8a0d60`); the newer gens route through a per-instruction heap array. The integers are owned by [EUP Per-Gen Latency Integers](eup-per-gen-integers.md) and summarized here only for the correction-window sizing:

| gen | push→pop latency edge | `kErf` FIFO depth | correction-window note |
|---|---|---|---|
| Jellyfish | 4 | 4 | one latency window of in-flight results |
| Dragonfish | 4 | 4 | inherits Jellyfish (`+0x30` unchanged) |
| Pufferfish | 7 | 32 | deep FIFO; half-rate EUP issue (reservation 2) |
| Viperfish | 6 | 16 | |
| Ghostlite | 13 (F32) / 14 (BF16) | 16 | deepest latency; correction window largest |

The number of correction VALU ops a software fallback emits (e.g. the 7-coefficient `tanh` Horner = ~11 FMA/mul ops) is sized to hide this latency: on Ghostlite the 13-cycle EUP latency leaves room for the full rational, while on Jellyfish the 4-cycle edge is filled by the shorter Newton step. The depth bounds how many independent transcendentals can be in flight simultaneously before the scheduler must stall a push waiting for a pop to drain.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ResultFifoEntryCount` | `0x1d631520` | per-`TpuVersion` FIFO depth; `kErf` arm → `0xb53e270` | CERTAIN |
| `LatencyTableJellyfish::LatencyBetweenInternal` | `0x1c8a0d60` | JF/DF push→pop clamp to `Performance[+0x30]`=4 | CERTAIN |
| `kErf` depth table | `.rodata 0xb53e270` | `int[6]` `{4,4,32,16,16,16}` | CERTAIN |

---

## Cross-References

- [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — the encoding view: bare push + pop, the `V*Decomposed` builders, the function-selector map, and the duality these coefficients complement
- [EUP Latency Overview](eup-latency-overview.md) — the push→pop software-pipelining cost model the correction window feeds
- [EUP Per-Gen Latency Integers](eup-per-gen-integers.md) — the byte-pinned PF/VF/GL push→pop latency integers (7 / 6 / 13–14) and the `Get<Gen>Instruction → GetLatency` mechanism
- [EUP Payne-Hanek Range Reduction](eup-paynehanek.md) — the integer `1/(2π)` reduction table that pairs with the `sin`/`cos` EUP push (the trig argument-reduction half not catalogued here)
- [EUP Lane-Width / Unpack](eup-lane-width-unpack.md) — the bf16 sub-lane packing behind `VexpEup`'s packed `0x3fb93fb9` log2e immediate
- [ResultFifo / ArchRegister Enums](../isa/resultfifo-archregister.md) — `kErf` (ResultFifo `0x12`), the `EupResultFifoEntry` runtime proto, and `ResultFifoEntryCount`'s full 25-arm depth model
