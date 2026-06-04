# Bias-Add & Quant/Dequant Helpers

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA minus `0x200000` equals its file offset. Other libtpu builds will differ.*

## Abstract

These are the *numerics helpers* that bracket the systolic-array matmul/conv on the TensorCore — the layer between the raw int32/f32 accumulator coming out of the MXU and the value finally stored. Concretely: the bias-add, the activation clamp, and the quantize/dequantize sequences (scale application, narrow-float and integer converts, the saturating clamp that closes the round-trip). A reimplementer hunting for an `EmitBias` or a `QuantizeOp` will not find one, and that absence is the first finding.

There is **no dedicated bias op**. A "matmul + bias" HLO is an *output fusion* whose root is the conv and whose body is an ordinary elementwise `add(conv, broadcast(bias))`, emitted by the generic fusion vectorizer per output chunk inside `MatrixMultiplyAccumulateFunctor::DoOutputFusion`. The bias is broadcast to the tile shape (`HandleBroadcast`), added in f32 (`HandleAdd` → `VaddF32`), the activation runs (`HandleMaximum`/`HandleClamp`), and the result is requantized (`HandleConvert`) and stored. The accumulator dtype is dtype-keyed: int8 matmuls accumulate in int32 (`VaddS32`) and are dequantized to f32 *before* the float epilogue; bf16/fp32 matmuls accumulate in f32 (`VaddF32`), with two dedicated mixed-precision adds (`0x11e`/`0x11f`) folding bf16 high/low passes into the f32 accumulator.

The quant/dequant leaves are single LLO vector ops built by `LloRegionBuilder::Vcvt*`/`Vpack*`/`Vunpack*`. Round-to-nearest narrow-float quantize goes through the EXMY converts (`0x6e`/`0x6f`) or the bf16→fp8 packs; stochastic rounding goes through four `VcvtSrF32To*` binops (`0x70`-`0x73`) whose second operand is the per-lane random dither; int8 quantize is `round → VclampSymmetric → VpackcB8`. On the device fast path, quantization is **symmetric** (zero-point implicitly 0); the asymmetric / zero-point model lives only in the host-side MLIR quant dialect. The cleanest in-binary reference implementation of symmetric absmax 8-bit quantization is the collective `pincer_utils` family, which is documented here op-by-op.

For reimplementation, the contract is:

- **Bias-add is fusion, not an op:** the `AccumulateOutputChunk → DoOutputFusion` spine and the `VectorizingEmitter` `Handle*` dispatch (`HandleBroadcast` = bias splat, `HandleAdd` = bias add).
- **The accumulator dtype split:** int32 `VaddS32` (int8 matmul) vs f32 `VaddF32` (bf16/fp32), plus the bf16-in-f32 mixed adds `0x11e`/`0x11f`.
- **The convert leaves:** EXMY narrow-float (`0x6e`/`0x6f`), the four stochastic-round binops (`0x70`-`0x73`), `VcvtF32ToS32` round-direction, and the lane pack/unpack helpers.
- **The saturate layer:** `VclampSymmetric*` (native + min/max fallback `0x15f`) vs `VclampAsymmetric*` (binop `0x4c` + min/max), and `VclampGez*` (ReLU).
- **The symmetric absmax quantizer (`pincer_utils`):** absmax → scale (`qmax/absmax`) → quantize → 8-bit wire → f32 requant-reduce → dequant, with the three per-dtype max constants.

| | |
|---|---|
| **Bias-add path** | `MatrixMultiplyAccumulateFunctor::DoOutputFusion` (`0x13124360`); `HandleBroadcast` (`0x136f1d60`) + `HandleAdd` (`0x13e22e80`) |
| **Accumulator** | int8 → int32 `VaddS32` (`0x1d51e9c0`); bf16/fp32 → f32 `VaddF32` (`0x1d525160`); mixed `0x11e`/`0x11f` |
| **RNE narrow-float** | `VcvtEXMYToE4M3`/`E5M2` (`0x6e`/`0x6f`); `VpackCBf16ToE5M2`/`E4M3Fn` (`0x126`) |
| **Stochastic round** | `VcvtSrF32To{E5M2,E4M3,If8,Bf16}` (`0x70`-`0x73`), binop, rng-bits 2nd operand |
| **f32→int32** | `VcvtF32ToS32` (`0x1d524760`): `RoundNearest`→`VroundF32`, else `VtruncF32`, then `0x5e` |
| **Clamp** | `VclampSymmetricF32`/`Bf16` (native + `0x15f` fallback); `VclampAsymmetric*` (`0x4c`); `VclampGez*` (ReLU) |
| **Collective quantizer** | `pincer_utils`: `UpdateScale` (`0x137b75c0`), `GetUnpackFunction` (`0x137b8720`); types `{S8, F8E5M2, F8E4M3B11FNUZ}` |

---

## Bias-Add Is the Output-Fusion Epilogue

### Purpose

A reimplementer needs to know that bias, activation, and output-requantize are *not* separate passes over a materialized matmul output. They are emitted inline per output chunk, reading the matmul result directly out of the MXU result registers — the whole point of "output fusion".

### Algorithm

The post-canonicalization "matmul + bias + activation" HLO is an output fusion:

```text
fusion {
  conv  = convolution(activations, weights)   // MXU
  bcast = broadcast(bias)                      // bias to output tile shape
  add   = add(conv, bcast)                     // bias add
  [opt] = maximum(add, 0) / clamp(...) / tanh  // activation
  [opt] = convert(...) -> out_dtype            // (re)quantize
}
```

lowered by the per-chunk MXU emission spine:

```c
MatrixMultiplyAccumulateFunctor::operator()                       // 0x1310cd80
  -> AccumOutputChunkAndPossiblyDoOutputFusion                    // 0x13124100
       -> AccumulateOutputChunk                                   // 0x13123a80 : matres -> accumulator
       -> RegisterAccumOutput                                     // 0x131196a0
       -> PossiblyDoOutputFusion                                  // 0x13122d80
            -> DoOutputFusion                                     // 0x13124360
```

`DoOutputFusion` (`0x13124360`) branches on `ShapeUtil::ElementIsIntegral`/`ElementIsFloating(out)`, dequantizes an int32 accumulator with `VcvtS32ToF32` *before* the float epilogue (so bias-add and activation run in f32 even for int8 matmuls), then walks the fusion body op-by-op through the `VectorizingEmitter` `Handle*` dispatch, and finally `VreducePrecision` (bf16 rounding, unless `AllowExcessPrecision`) + `Vst`.

The elementwise op chain is JIT-emitted by the handlers:

| Handler | Address | Role |
|---|---|---|
| `HandleBroadcast` | `0x136f1d60` | Bias broadcast (splat to tile) |
| `HandleAdd` | `0x13e22e80` | Bias add (`VaddF32`/`VaddS32`, dtype-keyed) |
| `HandleConvert` | `0x13e22040` | In-fusion (re)quant/dequant |
| `HandleMaximum` | `0x13e21f80` | ReLU / clip-low |
| `HandleClamp` | `0x13e21f20` | Clamp both sides |
| `HandleMultiply` | `0x13e228c0` | Scale multiply |
| `HandleReducePrecision` | `0x13e23a40` | bf16 round-to-nearest-even |
| `HandleStochasticConvert` | `0x13e22520` | HLO `kStochasticConvert` |

`HandleBroadcast` reads the broadcast operand, splats the bias across the output tile (`PackOrMakeTuple`), and materializes it via `OpaqueCopy`. The bias operand enters through `FusionEmitter::AddInput` (`0x136d5f20`).

> **NOTE —** there is no `BiasAddEmitter` / `EmitBias`. Bias-add is the generic fusion vectorizer emitting `Add(value, Broadcast(bias))`. Treat "bias" as an ordinary fused input edge, not a special op.

---

## Accumulator Dtype and bf16-in-f32 Accumulation

### Purpose

The accumulate op is selected by the output element type, and the high-accuracy bf16 matmul modes need dedicated mixed-precision adds. A reimplementer must pick the right accumulate op per dtype or silently lose precision.

### Algorithm

`AccumulateOutputChunk` (`0x13123a80`) selects:

- int8 / int4 matmul → int32 accumulator → `VaddS32` (`0x1d51e9c0`)
- bf16 / fp32 matmul → f32 accumulator → `VaddF32` (`0x1d525160`)

The bf16-in-f32 multi-pass accumulation (the `bf16x2`/`bf16x3` high-accuracy modes) uses two dedicated mixed adds that fold a bf16 result half into an f32 accumulator:

| Builder method | Address | LLO opcode | Role |
|---|---|---|---|
| `VaddF32MixedBF16HighInst` | `0x1d55dd40` | `0x11e` | Accumulate bf16 HIGH pass into f32 |
| `VaddF32MixedBF16LowInst` | `0x1d55dd80` | `0x11f` | Accumulate bf16 LOW pass into f32 |
| `VaddF32` | `0x1d525160` | (binop) | Plain f32 accumulate |
| `VaddS32` | `0x1d51e9c0` | (binop) | Plain int32 accumulate |

The decompile confirms `VaddF32MixedBF16HighInst` calling `CreateVectorBinop(0x11E, …)`. The accumulate is always f32-wide (the systolic array multiplies bf16 but partial products accumulate in an f32 register file), which is why fp32 and bf16 share the same per-matmul MXU latency.

---

## The Convert Leaves: RNE and Stochastic Rounding

### Purpose

Every fp8/int8 quantize and the inverse dequant is a single LLO op. A reimplementer needs the opcode and the creator for each so the conversion is one instruction, not an emulated sequence (on capable targets).

### Encoding — round-to-nearest narrow float

```c
function VcvtEXMYToE4M3(in, exp_bits):                  // 0x1d52b440
    assert target().SupportsFloat8EXMY()
    inst = CreateVectorEXMYConversion(110 /*0x6e*/, …)
    return region->AppendInstruction(inst)
```

| Method | Address | LLO opcode | Creator |
|---|---|---|---|
| `VcvtEXMYToE4M3` | `0x1d52b440` | `0x6e` | `CreateVectorEXMYConversion` (gate `SupportsFloat8EXMY`) |
| `VcvtEXMYToE5M2` | `0x1d52b520` | `0x6f` | `CreateVectorEXMYConversion` |
| `VpackCBf16ToE5M2` | `0x1d567a40` | `0x126` | `CreateVectorPack` (VpackFormat) |
| `VpackCBf16ToE4M3Fn` | `0x1d567b20` | `0x126` | `CreateVectorPack` (VpackFormat) |
| `VcvtF32ToNarrowFloat` | `0x1d560960` | (composite) | `VimmIf`+`VabsF32`+`SimplifyPackF16` (software RNE) |

### Encoding — stochastic rounding

Each Sr convert is one binop whose second operand is the per-lane random dither, so the convert rounds up with probability equal to the fractional part:

```c
function VcvtSrF32ToE5M2(src_f32, rng_bits):            // 0x1d52b600
    inst = CreateVectorBinop(0x70, src_f32, rng_bits, region)
    return region->AppendInstruction(inst)
```

| Method | Address | LLO opcode |
|---|---|---|
| `VcvtSrF32ToE5M2` | `0x1d52b600` | `0x70` |
| `VcvtSrF32ToE4M3` | `0x1d52b640` | `0x71` |
| `VcvtSrF32ToIf8` | `0x1d52b680` | `0x72` |
| `VcvtSrF32ToBf16` | `0x1d52b6c0` | `0x73` |
| `VcvtF32ToS32Stochastic` | `0x1d52ade0` | `0x5e` (fast) / `0x15c` (sw fallback) |

The decompile confirms `VcvtSrF32ToE5M2` → `CreateVectorBinop(0x70u, …)` and `VcvtSrF32ToE4M3` → `CreateVectorBinop(0x71u, …)`. There is no software emulation for the fp8/bf16 Sr converts on capable targets — they are one op each. The HLO `kStochasticConvert` (`HandleStochasticConvert` `0x13e22520`) dispatches to this family for fp8/bf16 outputs and to `VcvtF32ToS32Stochastic` for integer outputs.

Per-gen stochastic support: `Target::SupportsVectorConvertF32Stochastic::kSupportedTypes` = `{10, 16, 19, 23}` = `{F16, BF16, F8E5M2, F8E4M3B11FNUZ}` on both Ghostlite (`0xb530678`) and Viperfish (`0xb530784`). `F8E4M3Fn` (20) is **not** in the Sr-supported set — only the B11FNUZ fp8 variant has native f32→fp8 stochastic rounding.

### Encoding — f32 → int32 round direction

`VcvtF32ToS32(value, RoundDirection)` (`0x1d524760`): `RoundNearest` (1) prepends `VroundF32`, otherwise (TowardsZero) prepends `VtruncF32`, then emits opcode `0x5e`. Named pseudo-variants: `VcvtF32ToS32TiesToEven` (`0x1d52ab00`), `...TowardsZeroPseudo` (`0x1d52ad20`), `...KnownIntegral` (`0x1d52ad80`), `...Stochastic` (`0x1d52ade0`).

---

## The Saturating Clamp

### Purpose

The quantize round-trip and the activation close with a saturating clamp into the destination range. The symmetric vs asymmetric split is the device-symmetric vs host-zero-point distinction surfaced as two builder families.

### Encoding

```c
function VclampSymmetricF32(value, bound):              // 0x1d527fc0
    if target supports native clamp:
        inst = CreateVectorClampSymmetric(value, bound, …)
    else:                                                // fallback
        v = CreateVectorMinimumF32(value, +bound)        // opcode 0x15f path
        inst = CreateVectorMaximumF32(v, -bound)
    return region->AppendInstruction(inst)
```

| Method | Address | Body |
|---|---|---|
| `VclampSymmetricF32` | `0x1d527fc0` | `CreateVectorClampSymmetric` + min/max (`0x15f`) fallback |
| `VclampSymmetricBf16` | `0x1d5283a0` | same |
| `VclampAsymmetricF32` | `0x1d528480` | binop `0x4c` + `Minimum`/`Maximum` (lo ≠ −hi) |
| `VclampAsymmetricBF16` | `0x1d528740` | same |
| `VclampGezF32` / `Bf16` | `0x1d527ea0` / `0x1d527ee0` | clamp ≥ 0 (ReLU) |
| `VclampS32` / `U32` / `Float` | `0x1d528820` / `0x1d528c40` / `0x1d528fe0` | integer / generic clamp |

The decompile of `VclampSymmetricF32` shows both the native `CreateVectorClampSymmetric` arm and the `CreateVectorMinimumF32`/`CreateVectorMaximumF32` fallback with opcode `0x15f`. **Symmetric** saturates to `[-bound, +bound]` (zero-point 0) — the int8/fp8 device saturate. **Asymmetric** saturates to `[lo, hi]` with `lo ≠ -hi` — the with-zero-point / unsigned host path.

`VreducePrecision` (`0x1d5697c0`, called from `DoOutputFusion` and `HandleReducePrecision`) is the bit-manipulation round-to-nearest-even that truncates an f32 mantissa to bf16: add the rounding bias to the mantissa, mask the low bits, fix the tie-to-even and the NaN/Inf exponent. The LLO op is `llo.vround.nearest_even.f32` (`VroundF32` `0x1d525480`).

---

## The Collective Symmetric Quantizer (`pincer_utils`)

### Purpose

The quantized all-reduce quantizes each shard to 8 bits before the wire write, reduces in 8-bit-on-wire with a running scale, and dequantizes at the end. These `xla::jellyfish::pincer_utils` helpers are the cleanest in-binary reference for symmetric absmax 8-bit quantization, and a reimplementer can lift the scale formula directly.

### Algorithm

```c
function UpdateScale(dtype, builder, max_abs_addr, scale_addr):   // 0x137b75c0
    switch dtype:
      case S8(0x2):           dtype_max = 127.0        // dword_84A2A28
      case F8E4M3B11FNUZ(0x17): dtype_max = 30.0       // dword_84A27FC
      case F8E5M2(0x13):      dtype_max = 57344.0      // dword_84A2530
      default: LogFatal("quantized_type_bound != nullptr") + PrimitiveType_Name(dtype)
    m       = VimmF32(dtype_max)
    max_abs = Vld(max_abs_addr)                         // expected in VMEM (checked)
    scale   = VdivF32(m, max_abs)                       // scale_factor = qmax / absmax
    Vst(scale_addr, scale)
```

The decompile fixes the dtype switch and the three `.rodata` floats exactly, and — importantly — fixes the division operand order as `VdivF32(dtype_max, max_abs)`, i.e. the stored scale factor is `qmax / absmax` (the **quantize** multiplier). Quantize is then `q = round(x * scale_factor)`; dequantize is `x = q / scale_factor`. The per-dtype max constants:

| `.rodata` address | Value | dtype | PrimitiveType |
|---|---|---|---|
| `0x84a2a28` | `127.0` | int8 max | `0x2` |
| `0x84a27fc` | `30.0` | F8E4M3B11FNUZ max finite | `0x17` |
| `0x84a2530` | `57344.0` | F8E5M2 max finite | `0x13` |

The five-stage round trip:

| Helper | Address | Role |
|---|---|---|
| `UpdateMaxLocalChunk` | `0x137b73a0` | Running absmax: `Vld` → `VunpackCF32` (if bf16) → `VabsF32` → `VmaxF32` → `Vst` |
| `UpdateScale` | `0x137b75c0` | absmax → scale (`qmax/absmax`); 3 dtype-max consts |
| `SymmetricallyQuantizeShardInPlaceTo8Bits` | `0x137b7740` | `round(x*scale)` → `VpackcB16` → `VpackcB8` (×4 unrolled) |
| `SymmetricallyDequantizeShardInPlace8Bit` | `0x137b7fc0` | `unpack` → `VcvtS32ToF32` → `/scale` → `VpackCBf16` |
| `ReduceSymmetricallyQuantized8BitShardInPlace` | `0x137b8880` | f32 requant-reduce (dequant → merge → re-track absmax) |
| `GetUnpackFunction` | `0x137b8720` | dtype → unpack fn pointer |

`GetUnpackFunction` (`0x137b8720`) maps `S8(0x2)→VunpackCS8`, `F8E5M2(0x13)→VunpackCF8E5M2`, `F8E4M3B11FNUZ(0x17)→VunpackCF8E4M3B11`, else a `StatusOr` error carrying the PrimitiveType name. The ring reduction (`ReduceSymmetricallyQuantized8BitShardInPlace`) is done in **f32** — dequant → merge functor → re-track absmax — so the 8-bit format is only the *wire* representation, not the arithmetic. `RotatedPincerQuantizedEmitter::kSupportedQuantizationTypes` (`0xae5b90c`) = `{2, 19, 23}` = `{S8, F8E5M2, F8E4M3B11FNUZ}`.

> **NOTE —** there is no zero-point in the device-side symmetric quantizers (`pincer_utils` + `Vcvt*`): the on-device fast paths are symmetric (`zero_point == 0`). Asymmetric / zero-point quantization exists only at the host-side MLIR quant-dialect layer.

---

## Per-Tensor vs Per-Channel vs Sub-Channel Scale (Host)

On the device codegen path quantization is symmetric and the granularity is the shard / vector tile (one scale, implicit zero-point 0). The richer scale + zero-point model lives in the host-side MLIR quant dialect, fully linked in `libtpu`:

| MLIR type | Scale | Zero-point | Extra fields | Granularity |
|---|---|---|---|---|
| `UniformQuantizedType` | `getScale()` | `getZeroPoint()` | — | Per-tensor |
| `UniformQuantizedPerAxisType` | `getScales()[]` | `getZeroPoints()[]` | `getQuantizedDimension()` | Per-channel |
| `UniformQuantizedSubChannelType` | `getScales()[]` | `getZeroPoints()[]` | `getBlockSizes()[]`, `getQuantizedDimensions()[]` | Block-wise |
| `CalibratedQuantizedType` | `getMin()`/`getMax()` | — | (no scale yet) | Calibration |

These lower through `mlir::quant::stablehlo::ConvertUniform{Quantize,Dequantize,Requantize,QuantizedDotGeneral,QuantizedConvolution,QuantizedAdd,QuantizedClipByValue,...}Op` (ten distinct `ConvertUniform*Op` families, exposed as 40 `QuantizedStablehloOpConversion::matchAndRewrite` instantiations) to integer dot/conv + affine requant `round((scale_in/scale_out)·(acc - zp_in)) + zp_out`. The guard string `"Cannot requantize while changing quantization_axis"` confirms a requantize cannot move the per-channel axis. A TPU dynamic per-column int8 quantizer (`convert_dynamic_quantize_ops` → `damax_output` → per-column scale) is flag-gated by `xla_tpu_experimental_enable_dynamic_int8_quantization`.

---

## Worked Example: int8 Matmul + Bias + ReLU → int8

```text
HLO (post DotCanonicalizer + output fusion):
  fusion {
    conv = convolution(int8 act, int8 weights) : s32   // MXU, int32 accumulate
    bc   = broadcast(f32 bias)
    deq  = convert(conv) : f32
    add  = add(deq, bc)
    relu = maximum(add, 0)
    req  = convert(relu) : s8
  }

Emission per output chunk (inside MatrixMultiplyAccumulateFunctor):
  AccumulateOutputChunk:  int32 accumulator (VaddS32 over K-tiles)
  DoOutputFusion:
    VcvtS32ToF32(acc)                       // int32 -> f32 dequant
    HandleBroadcast(bias) -> OpaqueCopy     // bias splat to tile
    HandleAdd            -> VaddF32         // + bias
    HandleMaximum        -> VclampGezF32    // ReLU
    HandleConvert        -> VmulF32(.,scale) + VcvtF32ToS32(RNE)
                            + VclampSymmetricF32(127) + VpackcB16/VpackcB8   // requant s8
    Vst                                     // store int8 output
```

---

## What Is Not Decoded

- The host-side affine-requant constants (`M0`, shift) inside `ConvertUniformRequantizeOp` / `ConvertUniformQuantizedDotOp`: the pattern symbols and the structural formula are recovered; the `matchAndRewrite` was not unwound op-by-op (upstream open-source legalization).
- The TPU `DynamicQuantize` custom-op emitter body (per-column damax → scale → int8) and the `xla::jellyfish::QuantizationConfig` proto field layout.
- The `VpackFormat` ordinals distinguishing E5M2 / E4M3Fn / E4M3B11 inside `0x126 CreateVectorPack` (the three bf16→fp8 pack methods are named; the enum values were not bound here — see [Pack/Unpack Precision](pack-unpack-precision.md)).
- The exact bit layout of `VcvtF32ToNarrowFloat`'s software RNE composite per fp8 format (exponent bias, subnormal flush, overflow-to-Inf vs saturate).
- The `VclampAsymmetric` lo/hi operand provenance (quant-type zero-point vs explicit fusion clip constants): confirmed asymmetric (`lo ≠ -hi`) but the operand origin not traced.

---

## Cross-References

- [Pack/Unpack Precision](pack-unpack-precision.md) — the `VpackFormat` enum, `set_pack_format_sublane`, and the bf16↔f32 lane pack/unpack the quant pack/unpack helpers share
- [VPU (Vector-ALU) Slot](slot-vpu.md) — the VALU slot carrying the `vcvt`/`vclamp`/`vadd` opcode immediates and the `.sr` stochastic-round convert family
- [EUP / Transcendental Slot](slot-eup-transcendental.md) — `SupportsBf16AluInstructions` and the lane-width model behind the bf16 accumulate and unpack
- [XLU Op Roster](xlu-op-roster.md) — the broader XLU op-to-factory table this convert/clamp family sits beside
- [Bundle Model](bundle-model-overview.md) — the VLIW bundle the convert/clamp/add ops schedule into per generation
