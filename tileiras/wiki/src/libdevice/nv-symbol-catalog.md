# libdevice `__nv_*` Symbol Catalog

## Abstract

The libdevice bitcode shipped with CUDA exposes roughly 350 device-side math entry points behind the `__nv_` prefix. They are the implementation surface that MLIR `math.*` / `arith.*` lowering and CUDA-C front ends target by name; every call site appears in the LLVM module as `declare <type> @__nv_<name>(<args>)` until [`Linker::linkModules` pulls in the bitcode body](overview.md#link-inline-simplify) and the always-inliner folds it into the caller. This page catalogues those symbols by family, names the reflection keys their bodies query, identifies the NVPTX hardware intrinsic each body decays into after [`NVVMReflectPass`](nvvm-reflect-mechanism.md) folds the configuration constants, and pins down the rounding-mode and FTZ matrix the symbols collectively cover.

The [Intrinsic ID Switch and Name Table](intrinsic-id-switch-and-name-table.md) page documents how the LLVM constant folder classifies surviving call sites by name; the [Math Pass Pipeline and Crosswalk](math-pass-pipeline-and-crosswalk.md) page documents the MLIR-side rewrite from `math.<op>` to `__nv_<name>`. This page is the inventory in between — the names themselves, the bodies they unwrap to, and the reasons the body chooses one PTX form over another.

## Naming convention

Every libdevice symbol decomposes into prefix, base name, type suffix, and optional rounding-mode suffix:

```text
__nv_  <base>  [<rounding-mode>]  [<type-suffix>]
```

| Component | Form | Examples | Notes |
|---|---|---|---|
| Prefix | `__nv_` | every entry | identifies device math; trips libdevice linker pattern |
| Base name | C99 / IEEE-754 root | `sin`, `cos`, `exp`, `log`, `sqrt`, `fma`, `pow`, `rint` | shared with libm; semantics match unless reflection keys override |
| Rounding mode | `_rn`, `_rz`, `_ru`, `_rd` | `__nv_dadd_rn`, `__nv_fdiv_ru` | optional; absent forms imply round-to-nearest-even |
| Type suffix | `f`, `d` (or none) | `__nv_sinf`, `__nv_sin`, `__nv_fabs` (default f64) | `f` = `float`, `d` or bare = `double`, `h`/`bf16` absent |

The full grammar admits four orthogonal axes: input domain (`f32`/`f64`/`i32`/`i64`/`u32`/`u64`), rounding mode, FTZ behaviour, and approximation policy. A name like `__nv_dadd_rn` reads as "double add, round-to-nearest-even, full precision"; `__nv_fast_powf` reads as "float pow, fast path approximation, may flush denormals". Half-precision (`f16`, `bf16`) is intentionally absent — MLIR `OpToFuncCallLowering` promotes to `f32` before the libdevice call and demotes via `arith.truncf` after, so libdevice never sees the narrow type.

## Family inventory

The catalogue groups symbols by the IEEE-754 / C99 root family they belong to. Counts are the entries reachable from `Linker::Flags::OnlyNeeded` against a kernel that touches every published math intrinsic; bitcode versions with optional families may not ship a body for every entry in the table.

### Trigonometric — circular

| Symbol family | f32 | f64 | Fast path | Reflection key | Decay (when applicable) |
|---|---|---|---|---|---|
| Sine | `__nv_sinf` | `__nv_sin` | `__nv_fast_sinf` | `__CUDA_FTZ`, `__CUDA_ARCH` | `sin.approx.f32` (FTZ); Payne–Hanek otherwise |
| Cosine | `__nv_cosf` | `__nv_cos` | `__nv_fast_cosf` | `__CUDA_FTZ` | `cos.approx.f32` (FTZ); Payne–Hanek otherwise |
| Tangent | `__nv_tanf` | `__nv_tan` | `__nv_fast_tanf` | `__CUDA_FTZ` | `sin.approx`/`cos.approx` quotient on FTZ paths |
| Sine + cosine | `__nv_sincosf` | `__nv_sincos` | `__nv_fast_sincosf` | `__CUDA_FTZ` | fuses both PTX approximations; returns by pointer outs |
| Sine of π·x | `__nv_sinpif` | `__nv_sinpi` | — | — | scaled Payne–Hanek; argument is in half-cycles |
| Cosine of π·x | `__nv_cospif` | `__nv_cospi` | — | — | scaled Payne–Hanek; argument is in half-cycles |
| Arc sine | `__nv_asinf` | `__nv_asin` | — | — | libdevice-only; polynomial in `1 - x*x` |
| Arc cosine | `__nv_acosf` | `__nv_acos` | — | — | uses `asin` then subtracts from `π/2` |
| Arc tangent | `__nv_atanf` | `__nv_atan` | — | — | range-reduced rational approximation |
| Two-arg arc tan | `__nv_atan2f` | `__nv_atan2` | — | — | quadrant fixup on top of `atan`; matches C `atan2` |

The `__nv_fast_*` aliases bind directly to the PTX approximate intrinsic (`sin.approx.f32`, `cos.approx.f32`) and skip Payne–Hanek range reduction; they are reachable through the fast-math math path or by name, never through MLIR `math.*` lowering on default settings.

### Trigonometric — hyperbolic and inverse hyperbolic

| Symbol family | f32 | f64 | Reflection key | Decay |
|---|---|---|---|---|
| Hyperbolic sine | `__nv_sinhf` | `__nv_sinh` | — | `(__nv_exp(x) - __nv_exp(-x)) * 0.5` with overflow guard |
| Hyperbolic cosine | `__nv_coshf` | `__nv_cosh` | — | `(__nv_exp(x) + __nv_exp(-x)) * 0.5` with overflow guard |
| Hyperbolic tangent | `__nv_tanhf` | `__nv_tanh` | — | rational approximation; sm_75+ uses `tanh.approx.f32` when present |
| Inverse hyperbolic sine | `__nv_asinhf` | `__nv_asinh` | — | `log(x + sqrt(x*x + 1))` with cancellation fix-up |
| Inverse hyperbolic cosine | `__nv_acoshf` | `__nv_acosh` | — | `log(x + sqrt(x*x - 1))` |
| Inverse hyperbolic tangent | `__nv_atanhf` | `__nv_atanh` | — | `0.5 * log1p(2x/(1-x))` |

### Exponential family

| Symbol family | f32 | f64 | Fast path | Reflection key | Decay |
|---|---|---|---|---|---|
| Base-e exp | `__nv_expf` | `__nv_exp` | `__nv_fast_expf` | `__CUDA_FTZ` | `ex2.approx.f32` (`exp(x) = ex2(x * 1.4426950408)`) |
| Base-2 exp | `__nv_exp2f` | `__nv_exp2` | — | `__CUDA_FTZ` | `ex2.approx.f32` directly |
| Base-10 exp | `__nv_exp10f` | `__nv_exp10` | `__nv_fast_exp10f` | `__CUDA_FTZ` | `ex2.approx` after `* log2(10)` |
| `exp(x) - 1` | `__nv_expm1f` | `__nv_expm1` | — | — | libdevice-only; Estrin-form polynomial near 0 |
| Natural log | `__nv_logf` | `__nv_log` | `__nv_fast_logf` | `__CUDA_FTZ` | `lg2.approx.f32` then `* 0.6931471806` |
| Base-2 log | `__nv_log2f` | `__nv_log2` | — | `__CUDA_FTZ`, `nvptx-approx-log2f32` | `lg2.approx.f32` directly |
| Base-10 log | `__nv_log10f` | `__nv_log10` | `__nv_fast_log10f` | `__CUDA_FTZ` | `lg2.approx` then `* 0.30102999566` |
| `log(1 + x)` | `__nv_log1pf` | `__nv_log1p` | — | — | libdevice-only; minimax polynomial |
| Power | `__nv_powf` | `__nv_pow` | `__nv_fast_powf` | `__CUDA_FTZ` | `lg2.approx` + `ex2.approx` composition |
| Integer power | `__nv_powif` | `__nv_powi` | — | — | repeated-squaring; integer exponent |
| `pow(x, n)` for `int n` | `__nv_fast_powf` (alias) | — | — | — | uses `lg2`/`ex2` regardless of integer-ness |

The fast-path aliases are the entry points the [fast-math pragma](../topics/fast-math-and-numerical-precision.md) routes math ops through; they short-circuit the precision-checking guard arms and emit the bare `ex2.approx.f32` / `lg2.approx.f32` pair without finite-input cleanup.

### Power-of-2 and integer-shift helpers

| Symbol family | f32 | f64 | Notes |
|---|---|---|---|
| `ldexp(x, n)` | `__nv_ldexpf` | `__nv_ldexp` | integer scale `n` is `i32`; result is `x * 2^n` |
| `frexp(x, *n)` | `__nv_frexpf` | `__nv_frexp` | mantissa returned, exponent written through pointer |
| `scalbn(x, n)` | `__nv_scalbnf` | `__nv_scalbn` | identical to `ldexp` on IEEE-754 binary radix |
| `scalbln(x, l)` | `__nv_scalblnf` | `__nv_scalbln` | `long` exponent; libdevice clamps before scaling |
| `logb(x)` | `__nv_logbf` | `__nv_logb` | floor(log2(|x|)) as f32/f64; matches C99 |
| `ilogb(x)` | `__nv_ilogbf` | `__nv_ilogb` | `int` exponent; raises domain error inline |
| `nextafter(x, y)` | `__nv_nextafterf` | `__nv_nextafter` | bitwise next representable; respects denormal direction |

### Rounding and sign manipulation

| Symbol | Type | Decay |
|---|---|---|
| `__nv_floorf` / `__nv_floor` | round toward -∞ | `cvt.rmi.f32.f32` (f32); libdevice body (f64) |
| `__nv_ceilf` / `__nv_ceil` | round toward +∞ | `cvt.rpi.f32.f32` (f32); libdevice body (f64) |
| `__nv_truncf` / `__nv_trunc` | round toward 0 | `cvt.rzi.f32.f32` (f32); libdevice body (f64) |
| `__nv_roundf` / `__nv_round` | round half-away-from-zero | libdevice-only — PTX has no matching mode |
| `__nv_rintf` / `__nv_rint` | round to nearest (current rounding mode) | `cvt.rni.f32.f32` (default IEEE) |
| `__nv_nearbyintf` / `__nv_nearbyint` | rint without inexact flag | same as `rint`; libdevice flag handling differs |
| `__nv_lroundf` / `__nv_lround` | round to `long` | `cvt.rni.s32.f32` after range check |
| `__nv_llroundf` / `__nv_llround` | round to `long long` | `cvt.rni.s64.f64` after range check |
| `__nv_lrintf` / `__nv_lrint` | rint to `long` | `cvt.rni.s32.f32` |
| `__nv_llrintf` / `__nv_llrint` | rint to `long long` | `cvt.rni.s64.f64` |
| `__nv_copysignf` / `__nv_copysign` | sign transfer | bit op; folds to `llvm.copysign.*` |
| `__nv_fabsf` / `__nv_fabs` | absolute value | bit-AND mask; folds to `llvm.fabs.*` or `abs.f32` |
| `__nv_signbitf` / `__nv_signbitd` | sign-bit test | shift-right of bit pattern |

### Min/max and classification

| Symbol | Semantics | Decay |
|---|---|---|
| `__nv_fminf` / `__nv_fmin` | IEEE-754 minNum | `min.f32`/`min.f64` on sm_80+; libdevice body otherwise |
| `__nv_fmaxf` / `__nv_fmax` | IEEE-754 maxNum | `max.f32`/`max.f64` on sm_80+; libdevice body otherwise |
| `__nv_fminimumf` / `__nv_fminimum` | IEEE-754-2019 minimum (NaN-propagating) | bit ops + NaN check |
| `__nv_fmaximumf` / `__nv_fmaximum` | IEEE-754-2019 maximum (NaN-propagating) | bit ops + NaN check |
| `__nv_isfinitef` / `__nv_isfinited` | finite predicate | bit arithmetic on exponent field |
| `__nv_isinff` / `__nv_isinfd` | infinite predicate | bit arithmetic on exponent + mantissa |
| `__nv_isnanf` / `__nv_isnand` | NaN predicate | bit arithmetic; matches IEEE-754 quiet/sign-NaN definition |
| `__nv_finitef` / `__nv_finite` | legacy `isfinite` alias | aliased to `__nv_isfinitef`/`__nv_isfinited` |

The min/max divergence is the most observable one. `fmin`/`fmax` follow IEEE-754-2008's "minNum" rule that returns the non-NaN operand when exactly one operand is NaN; `fminimum`/`fmaximum` follow IEEE-754-2019's "minimum" rule that returns NaN whenever any operand is NaN. The MLIR `arith.minnumf` and `arith.maxnumf` ops route to `fmin`/`fmax`; there are no MLIR ops covering `fminimum`/`fmaximum`, only direct front-end calls.

### Roots, reciprocals, divides — the precision-keyed family

| Symbol | f32 | f64 | Reflection key | Decay at key=0 | Decay at key=1 |
|---|---|---|---|---|---|
| Square root | `__nv_sqrtf` | `__nv_sqrt` | `__CUDA_PREC_SQRT` | `sqrt.approx.f32` | `sqrt.rn.f32` |
| Reciprocal sqrt | `__nv_rsqrtf` | `__nv_rsqrt` | — | `rsqrt.approx.f32` | (same — no precise form) |
| Division | `__nv_fdividef` | `__nv_fdivide` | `__CUDA_PREC_DIV` | `div.approx.f32` | `div.rn.f32` |
| Reciprocal | `__nv_frcp_rn` etc. | `__nv_drcp_rn` etc. | — | `rcp.approx.f32` | `rcp.rn.f32` |
| Cube root | `__nv_cbrtf` | `__nv_cbrt` | — | libdevice-only — polynomial + Newton refinement | (same) |
| Reciprocal cbrt | `__nv_rcbrtf` | `__nv_rcbrt` | — | libdevice-only — `1 / cbrt(x)` with sign fix | (same) |
| Hypot | `__nv_hypotf` | `__nv_hypot` | — | `sqrt(x*x + y*y)` with overflow guard | (same) |
| Reciprocal hypot | `__nv_rhypotf` | `__nv_rhypot` | — | `1 / hypot(x, y)` | (same) |
| 3-argument hypot | `__nv_norm3df` | `__nv_norm3d` | — | `sqrt(x*x + y*y + z*z)` | (same) |
| 4-argument hypot | `__nv_norm4df` | `__nv_norm4d` | — | same with one more term | (same) |
| n-argument hypot | `__nv_normf` | `__nv_norm` | — | loop; pointer + length args | (same) |

`__CUDA_PREC_SQRT` and `__CUDA_PREC_DIV` are the two reflection keys with the most observable impact on libdevice output. Their `0` settings trip the approximate hardware path that the SASS engine schedules in a single cycle; their `1` settings replace the call with a software Newton-Raphson refinement on top of the approximate result, costing roughly five additional FMAs per call. The MLIR lowering path picks the key value from module-level `!nvvm.reflection` metadata seeded by [the driver CLI options](../driver/cli-options.md) — `tileiras` defaults to `__CUDA_PREC_DIV=1`, `__CUDA_PREC_SQRT=1` matching `nvcc`'s default of full IEEE precision.

### Integer arithmetic helpers

| Symbol family | Width | Decay |
|---|---|---|
| `__nv_abs` | `i32` → `i32` | `(x ^ (x >> 31)) - (x >> 31)` — fully inlined |
| `__nv_llabs` | `i64` → `i64` | same idiom on 64-bit shift |
| `__nv_min` / `__nv_max` | `i32` | `min.s32` / `max.s32` |
| `__nv_umin` / `__nv_umax` | `u32` | `min.u32` / `max.u32` |
| `__nv_llmin` / `__nv_llmax` | `i64` | `min.s64` / `max.s64` |
| `__nv_ullmin` / `__nv_ullmax` | `u64` | `min.u64` / `max.u64` |
| `__nv_mul24` | `i32 × i32 → i32` | `mul24.s32` (24-bit truncated multiply) |
| `__nv_umul24` | `u32 × u32 → u32` | `mul24.u32` |
| `__nv_mul64hi` | `i64 × i64 → i64` (hi half) | `mul.hi.s64` |
| `__nv_umul64hi` | `u64 × u64 → u64` (hi half) | `mul.hi.u64` |
| `__nv_mulhi` | `i32 × i32 → i32` (hi half) | `mul.hi.s32` |
| `__nv_umulhi` | `u32 × u32 → u32` (hi half) | `mul.hi.u32` |
| `__nv_popc` | `u32` → `i32` | `popc.b32` |
| `__nv_popcll` | `u64` → `i32` | `popc.b64` |
| `__nv_clz` / `__nv_clzll` | leading zeros | `clz.b32` / `clz.b64` |
| `__nv_ffs` / `__nv_ffsll` | bit position of LSB | `bfind` family |
| `__nv_brev` / `__nv_brevll` | bit reverse | `brev.b32` / `brev.b64` |
| `__nv_sad` / `__nv_usad` | sum of absolute differences | `sad.s32` / `sad.u32` |
| `__nv_byte_perm` | byte permutation | `prmt.b32` |
| `__nv_funnelshift_l`/`_lc`/`_r`/`_rc` | 64-bit funnel shifts | `shf.l/r.wrap/clamp.b32` |

The `mul24` family is the most architecture-dependent: pre-Volta hardware ran `mul24.s32` as a single-issue instruction; sm_70+ runs the full 32-bit `mul.lo.s32` at the same throughput, and the libdevice body simply forwards the call. Old CUDA-C code that explicitly calls `__mul24` therefore retains the API surface but loses the historical performance benefit.

### Mixed-mode conversions and float decoders

| Symbol family | Direction | Decay |
|---|---|---|
| `__nv_int2float_{rn,rz,ru,rd}` | `i32` → `f32` | `cvt.<rnd>.f32.s32` |
| `__nv_uint2float_{rn,rz,ru,rd}` | `u32` → `f32` | `cvt.<rnd>.f32.u32` |
| `__nv_ll2float_{rn,rz,ru,rd}` | `i64` → `f32` | `cvt.<rnd>.f32.s64` |
| `__nv_ull2float_{rn,rz,ru,rd}` | `u64` → `f32` | `cvt.<rnd>.f32.u64` |
| `__nv_int2double_rn` | `i32` → `f64` | `cvt.f64.s32` (only `rn` is exact) |
| `__nv_double2int_{rn,rz,ru,rd}` | `f64` → `i32` | `cvt.<rnd>.s32.f64` |
| `__nv_float2int_{rn,rz,ru,rd}` | `f32` → `i32` | `cvt.<rnd>.s32.f32` |
| `__nv_double2float_{rn,rz,ru,rd}` | `f64` → `f32` | `cvt.<rnd>.f32.f64` |
| `__nv_float2half_{rn,rz}` | `f32` → `f16` | `cvt.<rnd>.f16.f32` |
| `__nv_half2float` | `f16` → `f32` | `cvt.f32.f16` |
| `__nv_float_as_int` | bit reinterpret | `mov.b32` (lossless) |
| `__nv_int_as_float` | bit reinterpret | `mov.b32` (lossless) |
| `__nv_longlong_as_double` | bit reinterpret | `mov.b64` |
| `__nv_double_as_longlong` | bit reinterpret | `mov.b64` |
| `__nv_double2hiint` / `_loint` | f64 → upper/lower 32 bits | `cvt.u32.u64` after `mov.b64` |
| `__nv_hiloint2double` | reassemble f64 from two i32 | `mov.b64` of packed result |

The `*_as_*` family is intentionally a no-op at the LLVM level; libdevice ships a body anyway so that the symbol exists and the bitcode linker has something to resolve. The body is a single `bitcast` followed by `ret`, which the always-inliner reduces to a register rename in the caller.

### Error and gamma functions

| Symbol | f32 | f64 | Notes |
|---|---|---|---|
| Error function | `__nv_erff` | `__nv_erf` | libdevice-only; rational approximation, double-double internals |
| Complementary erf | `__nv_erfcf` | `__nv_erfc` | libdevice-only; scaled `exp(-x*x)` path for large `|x|` |
| Inverse erf | `__nv_erfinvf` | `__nv_erfinv` | libdevice-only; iterative |
| Inverse erfc | `__nv_erfcinvf` | `__nv_erfcinv` | libdevice-only; iterative |
| Scaled erfc | `__nv_erfcxf` | `__nv_erfcx` | `exp(x*x) * erfc(x)`; large-x stable form |
| Gamma | `__nv_tgammaf` | `__nv_tgamma` | Stirling for large `x`, reflection for small `x` |
| Log-gamma | `__nv_lgammaf` | `__nv_lgamma` | log of |Γ(x)|; signbit returned through TLS in the legacy API, by pointer in the modern API |
| Norm CDF | `__nv_normcdff` | `__nv_normcdf` | `0.5 * erfc(-x/sqrt(2))` |
| Inverse norm CDF | `__nv_normcdfinvf` | `__nv_normcdfinv` | iterative on `erfinv` |
| Bessel J0 / J1 | `__nv_j0f` / `__nv_j1f` | `__nv_j0` / `__nv_j1` | libdevice-only; minimax for small `x`, asymptotic for large |
| Bessel Y0 / Y1 | `__nv_y0f` / `__nv_y1f` | `__nv_y0` / `__nv_y1` | libdevice-only; same shape |
| Bessel Jn / Yn | `__nv_jnf` / `__nv_ynf` | `__nv_jn` / `__nv_yn` | recurrence on the J0/J1, Y0/Y1 pair |

### Rounding-mode-qualified arithmetic

These are the "primitive" forms the MLIR lowering does not use directly, but which front-end code can call to force a specific rounding mode on a single op:

| Op | f32 family | f64 family | Decay |
|---|---|---|---|
| Add | `__nv_fadd_rn` / `_rz` / `_ru` / `_rd` | `__nv_dadd_rn` / `_rz` / `_ru` / `_rd` | `add.<rnd>.f32` / `add.<rnd>.f64` |
| Subtract | `__nv_fsub_rn` etc. | `__nv_dsub_rn` etc. | `sub.<rnd>.f32` / `sub.<rnd>.f64` |
| Multiply | `__nv_fmul_rn` etc. | `__nv_dmul_rn` etc. | `mul.<rnd>.f32` / `mul.<rnd>.f64` |
| Divide | `__nv_fdiv_rn` etc. | `__nv_ddiv_rn` etc. | `div.<rnd>.f32` / `div.<rnd>.f64`; `_rn` is the only IEEE-correct form |
| FMA | `__nv_fmaf_rn` etc. | `__nv_fma_rn` etc. | `fma.<rnd>.f32` / `fma.<rnd>.f64` |
| Reciprocal | `__nv_frcp_rn` etc. | `__nv_drcp_rn` etc. | `rcp.<rnd>.f32` / `rcp.<rnd>.f64` |
| Square root | `__nv_fsqrt_rn` etc. | `__nv_dsqrt_rn` etc. | `sqrt.<rnd>.f32` / `sqrt.<rnd>.f64` |

The MLIR pipeline never emits these names directly; they are reachable only through CUDA-C intrinsic shims (`__fadd_rn` etc. without the `__nv_` prefix) and pass through the libdevice linker unchanged.

## Reflection-key cross-reference

The reflection keys consumed by libdevice bodies fall into four orthogonal axes:

| Key | Type | Values | Effect on bodies that read it |
|---|---|---|---|
| `__CUDA_FTZ` | bool | `0` (preserve), `1` (flush) | Selects FTZ vs non-FTZ approximate-intrinsic variant in `sin`, `cos`, `tan`, `exp`, `log`, `pow`, etc. Bodies typically have `if (__nvvm_reflect("__CUDA_FTZ"))` arms wrapping the `sin.approx.ftz.f32` / `sin.approx.f32` selection. |
| `__CUDA_PREC_DIV` | bool | `0` (approx), `1` (IEEE) | `__nv_fdividef` and `__nv_fdivide` choose `div.approx.f32` vs `div.rn.f32` + Newton refinement. `nvcc` default is `1`; `--use_fast_math` flips to `0`. |
| `__CUDA_PREC_SQRT` | bool | `0` (approx), `1` (IEEE) | `__nv_sqrtf` and `__nv_sqrt` choose `sqrt.approx.f32` vs `sqrt.rn.f32`. Default and flip behaviour mirror `__CUDA_PREC_DIV`. |
| `__CUDA_FAST_INT_DIV` | bool | `0`, `1` | Integer division and modulo libdevice helpers (`__nv_idiv`, `__nv_imod`, etc., if present in the bitcode) choose between the reference 32-bit algorithm and the truncated approximation. |
| `__CUDA_ARCH` | int | `700`, `750`, `800`, `860`, `890`, `900`, `1000`, `1030`, `1200`, … | Selects per-SM intrinsic availability inside bodies that fall back to legacy paths on older hardware. |

Bodies that do not query any reflection key are non-configurable; they emit the same NVPTX intrinsic regardless of target options. The libdevice [overview pipeline](overview.md#pipeline) folds the reflection keys before the always-inliner runs, so reflection-driven branches are dead by the time the inliner copies the body into the caller.

## SM-floor inventory

A handful of `__nv_*` symbols decay into instructions whose lowest PTX support level is later than the rest of libdevice. Calls to these symbols from a kernel compiled for an older SM produce libdevice fall-back bodies rather than the named instruction.

| Symbol family | Decay floor | Older-SM fallback |
|---|---|---|
| `__nv_fminf` / `__nv_fmaxf` | sm_80 `min.f32`/`max.f32` | branch-and-select bit logic |
| `__nv_fmin` / `__nv_fmax` | sm_80 `min.f64`/`max.f64` | branch-and-select |
| `__nv_tanhf` | sm_75 `tanh.approx.f32` | rational approximation in software |
| Block-scaled `__nv_cvt_*` (FP8 / FP4) | sm_89 / sm_100a `cvt.packfloat.*` | not provided — undefined behaviour on older SMs |
| `__nv_fma_relu_*` | sm_75 (f16) / sm_90a (f8) | not provided — softmax-style ReLU+FMA fused intrinsic is sm-gated |
| Tensor-memory casts | sm_100a tcgen05 path | not in libdevice — these live in [`nvvm`](../dialects/nvvm/tcgen05-ops.md) |

The libdevice "fall-back" body is the same body the reflection-folded reference path uses; the only difference is that the always-inliner cannot collapse the body into a single PTX instruction because the PTX form does not exist yet.

## Linker behaviour and dead-call elimination

Libdevice bitcode is linked with `Linker::Flags::OnlyNeeded`. The linker walks the user module's declaration set, copies in the matching definitions, and recursively pulls in any further `__nv_*` declarations the freshly-imported bodies reference. The `__CUDA_FTZ` / `__CUDA_PREC_*` reflection arms typically reference both the FTZ and the non-FTZ helper symbols, so a library body that ultimately resolves to a single arm still drags the unused arm's helpers into the user module. The post-inline `GlobalDCEPass` cleans them up:

```text
1. Linker pulls in __nv_sinf body, which references __nv_sin_kernel_ftz, __nv_sin_kernel_nonftz.
2. NVVMReflectPass folds the FTZ arm to the chosen path.
3. AlwaysInlinerPass inlines __nv_sinf into the caller.
4. SimplifyCFG + SCCP eliminate the dead arm and its helper call.
5. GlobalDCEPass removes the orphaned __nv_sin_kernel_<other> from the module.
```

Steps 4 and 5 are why the libdevice bitcode appears tiny in the final PTX even though the bitcode blob is several megabytes. The pre-DCE module size can be 5–10× the final size; the dead-arm elimination is the single largest IR shrink in the libdevice integration path.

## Verification invariants

Three invariants hold across libdevice integration. Violations are caught by [`NVVMIRVerifier`](../nvptx-passes/nvvm-ir-verifier.md) before the NVPTX backend runs.

- Every `__nv_*` declaration is resolved before code generation. A surviving declaration is a backend error.
- Every `__nvvm_reflect("KEY")` call is folded into a `ConstantInt` before always-inlining. A surviving reflect call is a configuration bug.
- No `__nv_*` body retains a `__nvvm_reflect` call after the four-pass integration; the post-link [`nvvm-reflect-pp` cleanup](nvvm-reflect-mechanism.md#post-reflect-cleanup-nvvm-reflect-pp) folds the constant branches and removes any dangling intrinsic call sites.

## QUIRK: Unknown reflection keys silently fold to zero

`NVVMReflectPass::populateVarMap` defaults missing keys to `0` and **records the zero in the resolved map** so that every later call site folds to the same value. A typo in `__nvvm_reflect("__CUDA_FFZ")` (with double-F) is therefore not a diagnostic — it is a silent reset to the FTZ-off behaviour, applied consistently. The only way to notice is to inspect the post-reflect IR and check that the key the body queries is the key the configuration set. Reimplementations that diverge from this — for example by warning on unknown keys, or by returning `-1` to indicate "unknown" — break libdevice bodies that rely on the recorded-zero behaviour for legacy options that the bitcode references but the current configuration system does not know about.

## QUIRK: `_rn` is the only IEEE-correct division and square root

`__nv_fdiv_rn` and `__nv_fsqrt_rn` decay to `div.rn.f32` and `sqrt.rn.f32` — the only PTX divide and square-root variants that the IEEE-754 standard certifies as correctly rounded. The `_rz`, `_ru`, and `_rd` variants are valid hardware instructions but do not satisfy IEEE-754 single-step correctness for division and square root: they round the approximate result rather than the mathematically exact one. Libdevice does not paper over this — code that calls `__nv_fdiv_ru(a, b)` gets the directed-rounded approximation, not a Newton-refined directed-rounded result. The MLIR `arith` dialect has no rounding-mode parameter on `arith.divf`, so this asymmetry is only reachable through CUDA-C intrinsics; MLIR-fronted code always sees the round-to-nearest path.

## QUIRK: `__nv_fast_*` are libdevice symbols, not preprocessor macros

`__nv_fast_sinf`, `__nv_fast_cosf`, `__nv_fast_powf`, etc. exist as separate bitcode symbols, not as `#define`-style rewrites of `__nv_sinf` and friends. They have distinct bodies — typically a single `sin.approx.ftz.f32` call — and their existence is what allows `--use_fast_math` to substitute the symbol name during MLIR `OpToFuncCallLowering` selection without recompiling the libdevice bitcode. A reimplementation that treats `__nv_fast_sinf` as a macro alias of `__nv_sinf` will lose the FTZ behaviour the fast-path body enforces unconditionally; the slow-path body is FTZ-conditional on `__CUDA_FTZ`, and a fast-math build with `__CUDA_FTZ=0` (the IEEE-clean default) would then silently preserve denormals where CUDA's bitcode would flush them.

## Cross-references

The four-pass integration sequence that turns these declarations into concrete bodies is documented in [libdevice Overview — Pipeline](overview.md#pipeline). The reflection keys that gate body selection are documented in [NVVMReflect Mechanism — Three var-map sources](nvvm-reflect-mechanism.md#three-var-map-sources). The MLIR-side rewriter that emits the `__nv_*` call sites these symbols define is documented in [Math Pass Pipeline and Crosswalk — Full math-op crosswalk](math-pass-pipeline-and-crosswalk.md#full-math-op-crosswalk). The LLVM constant folder that classifies any surviving by-name call sites is documented in [Intrinsic ID Switch and Name Table — libdevice suffix name table](intrinsic-id-switch-and-name-table.md#libdevice-suffix-name-table). The fast-math pragma that selects the `__nv_fast_*` family over the precision-keyed family is discussed in [Fast Math and Numerical Precision](../topics/fast-math-and-numerical-precision.md).
