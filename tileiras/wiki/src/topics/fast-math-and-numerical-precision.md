# Fast-Math and Numerical Precision

## Abstract

Tileiras lets the user trade floating-point correctness for performance along five orthogonal axes: per-op fast-math flags on arithmetic operations, flush-to-zero control on division and transcendentals, approximate-versus-exact intrinsic selection in libdevice, narrow-precision FP8/FP4 arithmetic with explicit cast semantics, and block-scaled formats that share one exponent across a value group. Each axis is controlled by an attribute or reflect key that travels through the lowering pipeline; the final PTX modifier or hardware intrinsic is chosen at NVVM-to-PTX emission time.

The axes compose, but they do not commute. A function-level FTZ promise interacts with per-op `arcp`; an approximate intrinsic selected by `afn` is still subject to FTZ at the instruction modifier; FP8 casts must round under a rounding mode that may differ from the surrounding fast-math context. This page documents the legal compositions and the data flow that produces them.

## The Fast-Math Flags

Tileiras carries the standard LLVM fast-math flag set as an MLIR attribute on each arithmetic op. The flag bits map one-to-one onto LLVM's `FastMathFlags`:

| Flag | Assumption | Optimization unlocked |
|---|---|---|
| `nnan` | result is not NaN | folds such as `fcmp ord x, x → true` |
| `ninf` | result is not infinity | infinity-arm removal in select chains |
| `nsz` | sign of zero is irrelevant | `x - x → 0`, `0 - x → -x` without sign care |
| `arcp` | `a/b` may become `a * (1/b)` | reciprocal substitution and CSE on the reciprocal |
| `contract` | FMA fusion is permitted | `a*b + c → fma(a,b,c)` across a basic block |
| `afn` | approximate intrinsics are allowed | `__nv_sqrt` may resolve to `sqrt.approx.f32` |
| `reassoc` | algebraic reassociation is permitted | reduction-tree rebalancing, horner reordering |

The aggregate flag `fast` is the bitwise OR of all seven. Tileiras frontends emit individual flags rather than the aggregate, which lets later passes turn one bit off without losing the others.

The flag bits are not advisory. Each downstream consumer reads the exact bit that authorises its rewrite: the FMA former reads `contract`, the reciprocal pass reads `arcp`, the libdevice resolver reads `afn`. A flag missing from the op blocks the rewrite even when the surrounding context is `fast`.

```c
bool can_fuse_to_fma(Operation *mul, Operation *add) {
    if (!single_use_chain(mul, add)) return false;
    return has_fastmath_flag(mul, FMF_CONTRACT)
        && has_fastmath_flag(add, FMF_CONTRACT);
}

bool can_use_approx_sqrt(Operation *call) {
    return has_fastmath_flag(call, FMF_AFN);
}
```

The lattice across the pipeline is monotone: passes may drop bits when they cannot prove the assumption is preserved (typically across a control-flow merge that joins fast and slow operands), but they do not add bits. The frontend is the sole producer of fast-math flags.

## FTZ — Flush to Zero

FTZ treats subnormal inputs and results as signed zero. On every NVIDIA GPU since Maxwell, FTZ is a per-instruction modifier rather than a global mode; the PTX form is `.ftz` appended to the mnemonic of `mul`, `div`, `sqrt`, `rsqrt`, and the transcendental family. The hardware path for FTZ-enabled instructions is one cycle faster on the f32 path on most architectures because it skips the subnormal handler.

FTZ has two control surfaces in tileiras:

| Surface | Scope | Source |
|---|---|---|
| function attribute `denormal-fp-math` | every op in the function | LLVM IR attribute populated from CLI `--use-fast-math` / `-ftz=true` |
| reflect key `__CUDA_FTZ` | libdevice bodies before linking | NVVMReflect var-map from CLI plus module metadata |

The function attribute has three legal values: `ieee` (no FTZ), `preserve-sign` (subnormals become signed zero), `positive-zero` (subnormals become +0 regardless of sign). NVPTX only emits `.ftz` PTX modifiers when the function-level attribute is `preserve-sign` and the op family has an FTZ form.

The reflect key drives a separate decision earlier in the pipeline. When NVVMReflect runs on a libdevice body whose control flow is gated on `__nvvm_reflect("__CUDA_FTZ")`, the fold collapses the FTZ arm or the non-FTZ arm before the body inlines into the caller. The two surfaces must agree at compile time: `__CUDA_FTZ=1` with `denormal-fp-math=ieee` is rejected by the backend because the libdevice body selected the FTZ-aware intrinsic but the NVPTX emitter refuses to add `.ftz` to it.

```c
PtxModifier select_ftz_modifier(Operation *op, FunctionAttrs attrs) {
    if (!op_family_supports_ftz(op->kind)) return MODIFIER_NONE;
    if (attrs.denormal_fp_math != DENORMAL_PRESERVE_SIGN) return MODIFIER_NONE;
    return MODIFIER_FTZ;
}
```

The 2x2 case matrix below shows the four legal combinations on f32 single-precision sqrt. The fifth and sixth cells — `ieee` function with `__CUDA_FTZ=1` libdevice resolution, or `preserve-sign` function with `__CUDA_FTZ=0` libdevice resolution — are rejected at NVVMIRVerifier with a "function FTZ disagrees with libdevice FTZ" diagnostic.

| function | reflect | resolved op |
|---|---|---|
| `ieee` | `__CUDA_FTZ=0` | `nvvm.sqrt.rn.f` (no `.ftz`) |
| `ieee` | (FTZ not consulted; `__CUDA_PREC_SQRT=0`) | `nvvm.sqrt.approx.f` (no `.ftz`) |
| `preserve-sign` | `__CUDA_FTZ=1`, `__CUDA_PREC_SQRT=1` | `nvvm.sqrt.rn.ftz.f` |
| `preserve-sign` | `__CUDA_FTZ=1`, `__CUDA_PREC_SQRT=0` | `nvvm.sqrt.approx.ftz.f` |

f64 has no FTZ form on any current architecture: the `.ftz` modifier is rejected by the assembler on f64 mnemonics. Tileiras silently drops the modifier on the f64 path even when the function attribute is `preserve-sign`.

## Approximate Transcendentals

The NVIDIA SFU has dedicated hardware for five transcendentals: `sin.approx`, `cos.approx`, `rsqrt.approx`, `lg2.approx`, `ex2.approx`. Each is approximately 22 bits of accuracy on f32 input (vs ~24 for fully IEEE single) and runs at one result per cycle per SFU lane. The IEEE path through libdevice is roughly an order of magnitude slower.

Tileiras selects the `.approx` variant on three independent triggers:

1. The op carries the `afn` fast-math bit. This is the frontend-driven path: a math function called with `nvfuser::fastmath` or compiled under `--ffast-math` arrives at the math pass with `afn` set, and the libdevice resolver rewrites `math.sin %x : f32` into `nvvm.sin.approx.f` directly without going through `__nv_sinf`.

2. The libdevice symbol that was called is itself one of the explicit `__nv_fast_*` aliases (`__nv_fast_sinf`, `__nv_fast_cosf`, `__nv_fast_logf`, etc.). These bodies are a single approximate intrinsic with no reflect-guarded fallback. The resolver folds the symbol regardless of the surrounding fast-math context.

3. The `__nv_*` body is reflect-gated and `__CUDA_PREC_*` selected the approximate arm. `__nv_sqrtf` for example has `__nvvm_reflect("__CUDA_PREC_SQRT") ? sqrt.rn : sqrt.approx`; with `__CUDA_PREC_SQRT=0` the reflect fold leaves only the `sqrt.approx.f` arm.

The five SFU operations have FTZ and non-FTZ forms; the four-way matrix (approx × FTZ) is enumerated in the NVVM-Reflect crosswalk for each op. Operations outside the SFU family — `tanh`, `erf`, `atan` — have no `.approx` PTX form; the only fast path is the libdevice `__nv_fast_*` alias, which inlines a polynomial approximation in software.

```c
Intrinsic resolve_transcendental(MathOp op, Target t, ReflectMap r) {
    if (op_has_fastmath_flag(op, FMF_AFN)) return approx_intrinsic_for(op, t);
    if (call_target_is_fast_alias(op)) return approx_intrinsic_for(op, t);
    if (reflect_says_approx(op.kind, r)) return approx_intrinsic_for(op, t);
    return exact_intrinsic_for(op, t);
}
```

The decision happens in the math pass, before the NVPTX backend sees the call. By the time the LLVM IR reaches the backend the call has been replaced by an `nvvm.*.approx.*` intrinsic, and the only remaining choice is FTZ.

## Libdevice Gating: Bit-Exact vs Fast

Libdevice ships two callable variants for almost every transcendental, distinguished by symbol name:

| Symbol | Behaviour | Reflect-gated |
|---|---|---|
| `__nv_sqrt` | IEEE-correct, FTZ-agnostic | yes, on `__CUDA_PREC_SQRT` and `__CUDA_FTZ` |
| `__nv_fast_sqrt` | approximate, FTZ-aware | no, single arm |
| `__nv_sinf` | reflect-gated approx-or-exact | yes, on `__CUDA_FTZ` |
| `__nv_fast_sinf` | approximate, FTZ-aware | no, single arm |
| `__nv_sin` (f64) | IEEE-correct | no f64 fast variant |

Frontend code that wants the fast variant must call the `__nv_fast_*` symbol explicitly or compile under a fast-math context that the math pass can use to rewrite the call. There is no CLI option that globally swaps `__nv_sqrt` for `__nv_fast_sqrt`; the dispatch is symbol-name-based, not flag-based.

The two reflect keys interact: `__CUDA_PREC_DIV`, `__CUDA_PREC_SQRT`, `__CUDA_PREC_RSQRT`, and `__CUDA_PREC_LOG` each select the bit-exact arm of one transcendental family, while `__CUDA_FTZ` selects the FTZ variant of whichever arm survived. Setting `__CUDA_PREC_SQRT=0` with `__CUDA_FTZ=1` resolves `__nv_sqrtf` to `sqrt.approx.ftz.f`; setting both to zero resolves to `sqrt.approx.f`; setting `__CUDA_PREC_SQRT=1` with `__CUDA_FTZ=0` resolves to `sqrt.rn.f`.

[NVVMReflect Mechanism](../libdevice/nvvm-reflect-mechanism.md) documents the var-map source order and the constant-conditional cleanup that follows the substitution. [Math Pass Pipeline and Crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) carries the per-op crosswalk between `math.*` ops, `__nv_*` libdevice symbols, and the final `nvvm.*` intrinsic for each fast-math configuration.

## FP8 — E4M3 and E5M2

Two FP8 formats are first-class types in tileiras:

| Type | Layout | Range | Special values | Typical use |
|---|---|---|---|---|
| `f8E4M3FN` | 1 sign + 4 exp + 3 mantissa | ±448 | no inf, one NaN encoding | forward activations and weights |
| `f8E4M3FNUZ` | same layout, unsigned-zero variant | ±448 | no inf, no negative zero | some training recipes |
| `f8E5M2` | 1 sign + 5 exp + 2 mantissa | ±57344 | inf and NaN encoded | backward gradients |
| `f8E5M2FNUZ` | same layout, unsigned-zero variant | ±57344 | no inf, no negative zero | some training recipes |

The `FN` suffix means "finite": no infinity encoding, only NaN; the `UZ` suffix means "unsigned zero": no negative zero. The four element types are MLIR built-ins and round-trip through the bytecode.

Cast semantics matter because FP8's narrow range makes overflow common. Tileiras supports four rounding modes on f16→f8 and f32→f8 casts: round-to-nearest-even (default), round-to-nearest-tied-away-from-zero, round-toward-zero, and round-toward-positive-infinity. Saturation is independent: a `satf` modifier clamps overflowing values to ±max-finite instead of producing NaN or infinity (the latter is impossible on `FN` types).

```mlir
nv_tileaa.cast %x : tile<128x128 x f32> to tile<128x128 x f8E4M3FN>
    {rounding = #rne, satf = true}
```

FP8 MMA is available on SM89 (Ada) for the small-tile WMMA family and on SM90 (Hopper) for WGMMA. On SM89 the FP8 inputs are accumulated into f32; on SM90 the WGMMA accumulator is also f32 or f16. The MMA atom builder rejects `f8 × f8 → f8` and `f8 × f8 → bf16` shapes because the hardware refuses to issue them.

A worked dot product mixing precisions:

```mlir
%a : tile<128x64 x f8E4M3FN>
%b : tile<64x128 x f8E4M3FN>
%c : tile<128x128 x f32>

%d = nv_tileaa.dot %a, %b, %c
    : tile<128x64 x f8E4M3FN>, tile<64x128 x f8E4M3FN>, tile<128x128 x f32>
        -> tile<128x128 x f32>

%out = nv_tileaa.cast %d : tile<128x128 x f32> to tile<128x128 x f8E4M3FN>
    {rounding = #rne, satf = true}
```

The dot lowers to one or more WGMMA atoms on SM90 or one WGMMA-equivalent UMMA group on SM100. The cast lowers to a `cvt.rn.satfinite.e4m3x2.f32` pair-packed conversion on hardware that supports the packed form.

## Block-Scaled FP — MX-FP and NV-FP4

Block-scaled formats pack `N` narrow values together with a single shared scale factor. The effective dynamic range of the block is the value precision times the scale precision; the per-value cost is the narrow-value width plus `1/N` of the scale width.

On Blackwell SM100+ tileiras supports four block-scaled formats:

| Format | Value type | Block size | Scale type | OpCode group |
|---|---|---|---|---|
| MX-FP8 | `f8E4M3FN` or `f8E5M2` | 32 | `e8m0` | `kind::f8f6f4` |
| MX-FP6 | `f6E2M3FN` or `f6E3M2FN` | 32 | `e8m0` | `kind::f8f6f4` |
| MX-FP4 | `f4E2M1FN` | 32 | `e8m0` | `kind::mxf4` |
| NV-FP4 | `f4E2M1FN` | 16 or 32 | `e4m3` | `kind::mxf4nvf4` |

The scale factor lives in its own MLIR operand (`sf_a`, `sf_b` on the MMA op) and rides a dedicated TMEM region allocated alongside the value operands. The MMA hardware multiplies the value-product by the scale-product per block before adding into the accumulator.

The two `kind::mxf4` variants differ only in scale type: OCP-standard MX-FP4 uses `e4m3` scales (4-bit exponent, 3-bit mantissa, finite-only), and NVIDIA-defined NV-FP4 uses `e8m0` scales (8-bit exponent, no mantissa). The dispatcher reads the scale element type to pick the opcode group; mismatched scale types across `sf_a` and `sf_b` are a verifier error. The MMA atom registry in [tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) enumerates the legal `(atom_K, vecSize)` triples per variant.

```mlir
%d = nv_tileaa.dot %a, %b, %c
    sfa(%sa) sfb(%sb)
    : tile<M x K x f4E2M1FN>, tile<K x N x f4E2M1FN>, tile<M x N x f32>
        sfa: tile<M x (K/32) x e4m3>, sfb: tile<(K/32) x N x e4m3>
        -> tile<M x N x f32>
```

The scale factor operands consume their own TMEM region and have their own staging pipeline. The mainloop must keep the scale and value operands aligned across the K loop or the MMA produces silently wrong results — the hardware does not check operand correspondence.

## Recommended Precision Recipes

The four common configurations across the production matrix:

| Scenario | Input | Accumulator | Output | FTZ | Fast-math |
|---|---|---|---|---|---|
| inference / serving | `f8E4M3FN` | `f32` | `f8E4M3FN` or `bf16` | on | `afn`, `contract` |
| training forward | `bf16` | `f32` | `bf16` | off | `contract` only |
| training backward | `f8E5M2` | `f32` | `bf16` | off | `contract` only |
| bit-exact reference | `f32` | `f32` | `f32` | off | none |

The asymmetry between forward and backward in training reflects the different dynamic-range requirements: forward activations are tightly bounded around the activation function output, while gradients span many orders of magnitude across layers. `f8E4M3FN` has more mantissa precision and a narrower range; `f8E5M2` has wider range and less precision. The forward path tolerates the narrow range because activations are bounded; the backward path requires the wider range because gradient magnitudes are not.

Block-scaled formats (MX-FP4, NV-FP4) are usable on the forward path on SM100+ but require quantisation-aware training to converge. They do not currently compose with bit-exact reference recipes.

## Cross-References

[NVVMReflect Mechanism](../libdevice/nvvm-reflect-mechanism.md) documents the var-map source order, the merge rules between metadata and CLI overrides, and the constant-conditional cleanup that follows reflect substitution.
[Math Pass Pipeline and Crosswalk](../libdevice/math-pass-pipeline-and-crosswalk.md) carries the per-op crosswalk from `math.*` through `__nv_*` to `nvvm.*` for every fast-math configuration on f32 and f64.
[Intrinsic ID Switch and Name Table](../libdevice/intrinsic-id-switch-and-name-table.md) documents how the constant folder recognises post-libdevice call sites and which `nvvm.*` intrinsics it folds.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) carries the block-scaled MMA opcode table and the scale-factor TMEM allocation rules.
[nv_tileaa Op Roster](../dialects/nv_tileaa/op-roster.md) documents the `dot` operand shape and the scale-factor verifier diagnostics.
[Matmul Progression by SM](matmul-progression-by-sm.md) places FP8 on SM89, FP8 WGMMA on SM90, and block-scaled MX-FP / NV-FP4 on SM100 within the broader hardware lineage.
