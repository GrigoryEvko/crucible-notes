# Math Pass Pipeline + Crosswalk

## Abstract

This page anchors the end-to-end translation of MLIR `math.*` and `arith.*` floating-point operations through three name-binding layers: MLIR `OpToFuncCallLowering` patterns emit `llvm.call @__nv_<op>[f]`, the embedded libdevice bitcode supplies the `__nv_*` bodies, and the post-libdevice LLVM constant folder matches surviving call sites by `Intrinsic::ID` or by callee name.

It also corrects an earlier misidentification: the 8-phase LLVM pass near the libdevice consumers is not the math-to-libdevice rewriter. Math lowering happens in MLIR before libdevice is linked. The 8-phase pass is a later per-function cleanup over LLVM IR.

## Post-libdevice cleanup, not math-to-libdevice

The 8-phase pass was originally hypothesized as an in-`tileiras` "math to libdevice" pass. That is not its role. It allocates no `RewritePatternSet`, configures no `ConversionTarget`, references no `math.*` mnemonic strings, references no `__nv_*` symbols, and walks LLVM `Function` ranges rather than MLIR operation graphs. Its filters match memory-reading and memory-writing LLVM instruction classes: loads, stores, calls, atomics, fences, and memset-like operations. The pass is therefore an LLVM new-PM `FunctionPass` running after MLIR lowering and libdevice linking, well after the `__nv_*` calls have already been materialized and inlined.

What the pass shares with NVVMReflect is the underlying LLVM ADT vocabulary, not the algorithm. NVVMReflect is module-global and string-keyed; this pass is per-function and pointer-keyed. NVVMReflect folds `__nvvm_reflect(...)` calls, then `nvvm-reflect-pp` removes the now-constant branches in libdevice bodies. The cleanup pass runs downstream on the simplified IR and never introduces a new `__nv_*` call.

## Full math-op crosswalk

For every `math.*` / `arith.*` op lowered here, the important public artifacts are the libdevice entry point and, where applicable, the post-link intrinsic or name recognized by the constant folder after libdevice inlining and `nvvm-reflect-pp` cleanup.

| `math.<op>` / `arith.<op>` | f32 symbol | f64 symbol | Constant-folder name(s) | by-name fold |
|---|---|---|---|---|
| `arith.remf` | `__nv_fmodf` | `__nv_fmod` | `_Z4fmodff` / `_Z4fmoddd` | yes |
| `arith.minnumf` | `__nv_fminf` | `__nv_fmin` | LLVM `MinNum` node | no |
| `arith.maxnumf` | `__nv_fmaxf` | `__nv_fmax` | LLVM `MaxNum` node | no |
| `math.absi` | `__nv_abs` | n/a | n/a | no |
| `math.absf` | `__nv_fabsf` | `__nv_fabs` | `fabsd` / `llvm.nvvm.fabs.f` | no |
| `math.acosh` | `__nv_acoshf` | `__nv_acosh` | libdevice-only | yes |
| `math.asin` | `__nv_asinf` | `__nv_asin` | `asind` | yes |
| `math.atan` | `__nv_atanf` | `__nv_atan` | `atand` | yes |
| `math.acos` | `__nv_acosf` | `__nv_acos` | `acosd` | yes |
| `math.atan2` | `__nv_atan2f` | `__nv_atan2` | `_Z5atan2ff` / `_Z5atan2dd` | yes |
| `math.asinh` | `__nv_asinhf` | `__nv_asinh` | libdevice-only | yes |
| `math.atanh` | `__nv_atanhf` | `__nv_atanh` | libdevice-only | yes |
| `math.cbrt` | `__nv_cbrtf` | `__nv_cbrt` | libdevice-only | yes |
| `math.ceil` | `__nv_ceilf` | `__nv_ceil` | `ceild` | no |
| `math.copysign` | `__nv_copysignf` | `__nv_copysign` | `llvm.copysign.*` | no |
| `math.cos` | `__nv_cosf` | `__nv_cos` | `cosd` / `cosf` | yes |
| `math.cosh` | `__nv_coshf` | `__nv_cosh` | `coshd` | yes |
| `math.erf` | `__nv_erff` | `__nv_erf` | libdevice-only | yes |
| `math.erfc` | `__nv_erfcf` | `__nv_erfc` | libdevice-only | yes |
| `math.exp2` | `__nv_exp2f` | `__nv_exp2` | `exp2d` | yes |
| `math.exp` | `__nv_expf` | `__nv_exp` | `expd` / `expf` | yes |
| `math.expm1` | `__nv_expm1f` | `__nv_expm1` | libdevice-only | yes |
| `math.floor` | `__nv_floorf` | `__nv_floor` | `floord` | yes |
| `math.fma` | `__nv_fmaf` | `__nv_fma` | `llvm.nvvm.fma.rn.{f,d}` | no |
| `math.fpowi` | `__nv_powif` | `__nv_powi` | libdevice-only | no |
| `math.isfinite` | `__nv_finitef` | `__nv_isfinited` | bit arithmetic | no |
| `math.isinf` | `__nv_isinff` | `__nv_isinfd` | bit arithmetic | no |
| `math.isnan` | `__nv_isnanf` | `__nv_isnand` | bit arithmetic | no |
| `math.log10` | `__nv_log10f` | `__nv_log10` | `log10d` | yes |
| `math.log1p` | `__nv_log1pf` | `__nv_log1p` | libdevice-only | yes |
| `math.log2` | `__nv_log2f` | `__nv_log2` | `log2f` | yes |
| `math.log` | `__nv_logf` | `__nv_log` | `logd` / `logf` | yes |
| `math.powf` | `__nv_powf` | `__nv_pow` | `powff` / `powdd` | yes |
| `math.roundeven` | `__nv_rintf` | `__nv_rint` | `llvm.rint.f64` | no |
| `math.round` | `__nv_roundf` | `__nv_round` | libdevice-only | no |
| `math.rsqrt` | `__nv_rsqrtf` | `__nv_rsqrt` | `nvvm.rsqrt.approx.{f,d}` | no |
| `math.sinh` | `__nv_sinhf` | `__nv_sinh` | `sinhd` | yes |
| `math.sin` | `__nv_sinf` | `__nv_sin` | `sind` / `sinf` | yes |
| `math.sqrt` | `__nv_sqrtf` | `__nv_sqrt` | `sqrtd` | yes |
| `math.tanh` | `__nv_tanhf` | `__nv_tanh` | `tanhd` | yes |
| `math.tan` | `__nv_tanf` | `__nv_tan` | libdevice-only | yes |

Entries marked "libdevice-only" have no dedicated NVPTX backend intrinsic. After libdevice inline plus NVVMReflect cleanup, the body decays into a sequence of more primitive `__nvvm_*` intrinsics whose IDs the constant folder may recognize. The by-name folder runs against compile-time-constant inputs only: it reads `Function::getName()`, matches the recognized libdevice or finite-math spelling, evaluates the operation with host math routines, and constructs a `ConstantFP` result. The libdevice body is not invoked for IR-time constant folding.

## FP32 vs FP64 — four axes of divergence

| Axis              | f32                                                | f64                                                |
| ----------------- | -------------------------------------------------- | -------------------------------------------------- |
| Symbol pair       | `__nv_Xf` (47 entries)                              | `__nv_X` / `__nv_Xd` (47 entries)                   |
| Libdevice body    | Separate `__nv_sinf` body (Payne–Hanek f32 reduction, single-precision polynomial coefficients) | Separate `__nv_sin` body (Payne–Hanek f64 reduction, double-precision polynomial coefficients) |
| Backend intrinsic | TableGen suffix `f` — `sinf`, `cosf`, `expf`, `logf`, `sqrtf`, `powff` (`pow.f.f`), `_Z5atan2ff` (`atan2(float,float)`), `_Z4fmodff` | TableGen suffix `d` — `sind`, `cosd`, `expd`, `logd`, `sqrtd`, `powdd` (`pow.d.d`), `_Z5atan2dd`, `_Z4fmoddd` |
| HW asymmetry      | `nvptx-prec-divf32`, `nvptx-prec-sqrtf32`, `nvptx-approx-log2f32`, `nvptx-rsqrt-approx-opt` — all PTX-ISA-level f32 selectors with no f64 counterpart | f64 div is always `div.rn.f64`; f64 sqrt is always `sqrt.rn.f64` or a libdevice fallback when HW lacks it on the target SM |

The f16 and bf16 slots of these lowerings are empty: no `__nv_*` half-precision libdevice symbol is used. The MLIR pipeline promotes f16/bf16 to f32 via `arith.extf` before the libdevice call and demotes via `arith.truncf` after. The fp128 family is independent and softfloat-emulated; it is not driven by these `OpToFuncCallLowering` patterns.

## Cases that skip libdevice entirely

A subset of `math.*` ops have libdevice bodies whose control flow is mostly `__nvvm_reflect("__CUDA_PREC_*")` or `__nvvm_reflect("__CUDA_FTZ")` tests guarding `Intrinsic::nvvm_*` arms. After NVVMReflect folds the reflect calls and `nvvm-reflect-pp` removes constant branches, the body can reduce to a single hardware intrinsic and the `__nv_*` call symbol disappears.

Examples:

- `math.sqrt %x : f32` with `__CUDA_PREC_SQRT=0` reduces to `nvvm.sqrt.approx.f`; with `__CUDA_PREC_SQRT=1` it reduces to `nvvm.sqrt.rn.f`.
- `math.rsqrt %x : f32` reduces to `nvvm.rsqrt.approx.f`.
- `math.sin` / `math.cos` on f32 reduce to FTZ or non-FTZ approximate intrinsics depending on `__CUDA_FTZ`.
- `math.exp %x : f32` rewrites to `exp2.approx.f` composed with a multiply.
- `math.log2 %x : f32` rewrites to `nvvm.lg2.approx.f` when the approximate-log2 option is enabled.
- `math.absi` inlines as `(x ^ (x >> 31)) - (x >> 31)`.
- `math.{isnan,isinf,isfinite}` reduce to bit arithmetic on the raw FP encoding.

Conversely, `acosh`, `asinh`, `atanh`, `cbrt`, `erf`, `erfc`, `expm1`, `log1p`, `sinh`, `cosh`, `tanh`, `atan`, `atan2`, `asin`, `acos`, `tan`, generic `pow`, `remainder`, `fmod`, and `powi` retain the libdevice body unless the input is a compile-time constant.

## Reimplementation Notes

```text
lower_math_op(op):
    if op.type is f16 or bf16:
        x = extf(op.input, f32)
        y = call_libdevice(f32_symbol(op.name), x)
        return truncf(y, op.type)

    if op.type is f32:
        return call_libdevice(f32_symbol(op.name), op.operands)

    if op.type is f64:
        return call_libdevice(f64_symbol(op.name), op.operands)
```

Constant folding is a separate LLVM-tier concern. Do not execute libdevice IR to fold constants; classify the call, evaluate the recognized math operation directly, and replace the call with a constant.

## Cross-references

The four-pass integration sequence that materializes the `__nv_*` bodies this page lowers into is documented in [libdevice Overview — Pipeline](overview.md#pipeline) and [libdevice Overview — Link, inline, simplify](overview.md#link-inline-simplify). The `__nvvm_reflect("__CUDA_FTZ")` / `__CUDA_PREC_*` mechanism whose folding collapses the per-arch arms is documented in [NVVMReflect Mechanism — Three var-map sources](nvvm-reflect-mechanism.md#three-var-map-sources). The constant-folder classifier that recognizes the post-libdevice call sites by `Intrinsic::ID` or by name is documented in [Intrinsic ID Switch and Name Table — libdevice suffix name table](intrinsic-id-switch-and-name-table.md#libdevice-suffix-name-table). The NVPTX bring-up path that pulls libdevice into the LLVM module is documented in [NVPTX Bring-up and Target Init](../codegen/nvptx-bring-up-and-target-init.md).
