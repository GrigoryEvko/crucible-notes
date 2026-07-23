# Calibration and Scale/Zero-Point Flow

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/bin/hlo-opt` (cp310; cp311/cp312 share the layout). BuildID xxHash `93dd8bd9bd4c697b`. Other versions will differ.*

## Abstract

This page traces what happens to a *calibration range* and to *integer scale/zero-point* metadata as they pass through `hlo-opt`, and answers one sharp question: does any int8 per-tensor/per-channel scale or zero-point reach **Neuron device codegen**? The answer, grounded in the binary three independent ways, is **no**. Calibration and the entire int8 scale/zero-point machinery are a **golden / CPU-reference** concern. The only scale that reaches the device is the MX (OCP microscaling) per-block `E8M0` exponent scale, which carries **no zero-point** and is **not** calibration-derived.

The page is organized around the three actors. The first is `mlir::quant::CalibratedQuantizedType` — stock upstream MLIR. It carries a `(min, max)` range and **derives no scale**: the standard MLIR utilities that turn a calibrated range into a `UniformQuantizedType` (`fakeQuantAttrsToType`, `getUniformQuantizedTypeForMinMax`) are **entirely absent** from this binary (`nm` count = 0). The second is the Intel oneDNN v3.7.3 graph backend, statically linked: `convert_runtime_zero_points`, `fuse_src_zero_points`, `fuse_dst_zero_points`, and the rest of the `lower.cpp` int8-fusion pipeline. This is the XLA:CPU golden reference int8 path; it never emits device code. The third is the Neuron device quantizer, whose *only* quant passes are `LegalizeQuantizeMX` and `LegalizeScaledMatmul` — MX-FP8 microscaling, owned by the [MX-FP8 microscaling](mx-fp8-legalization.md) page.

The reader should leave with a precise three-bucket ledger — **stock MLIR**, **golden oneDNN**, **device MX** — and the ability to predict, for any quantized HLO, which bucket each piece of metadata lands in. Throughout, every actor is tagged `[STOCK]`, `[GOLDEN]`, or `[DEVICE]` so the boundary is never ambiguous.

For reimplementation, the contract is:

- The storage layout and accessor surface of `CalibratedQuantizedType` and the three uniform-quant types — and the crucial negative: **no min/max → scale derivation exists in this binary**.
- The oneDNN zero-point fusion algorithm: which op-kinds (`dnnl_sub_zps` = `0x1238`, `dnnl_add_zps` = `0x1237`) the passes match, how static zps fold into the primitive attr versus how runtime (tensor) zps become primitive inputs.
- The device-vs-golden split and *why* it falls where it does: the import gate on the `"mhlo."` custom-call prefix, and the single device scale type `builtin.f8E8M0FNU`.

| | |
|---|---|
| **Calibrated type** | `mlir::quant::CalibratedQuantizedType` — `{Type expressed, double min, double max}` |
| **`::get`** | `0x809aaf0` — only caller is the MLIR bytecode reader (`readType` `0x8095630`) |
| **Calibration→scale derivation** | **ABSENT** (`fakeQuantAttrsToType` / `getUniformQuantizedTypeForMinMax` count = 0) |
| **oneDNN version** | v3.7.3 (`.rodata` `dnnl_version::ver` `0x954fc0` = `03 00 00 00 07 00 00 00 03 00 00 00`) |
| **Golden int8 GEMM** | `ref_matmul_int8_t::execute_ref` `0x3fb9090` (gate env `DISABLE_DNNL_BACKEND`) |
| **Device quant passes** | `LegalizeQuantizeMX::Run` `0x1efc4f0`, `LegalizeScaledMatmul::Run` `0x1efe1a0` |
| **Only device scale** | `builtin.f8E8M0FNU` (MX per-block, **no zero-point**, runtime EMAX) |

---

## `CalibratedQuantizedType` — the Range Record `[STOCK MLIR quant]`

### Purpose

`mlir::quant::CalibratedQuantizedType` is the stock upstream MLIR `quant`-dialect type that records a calibration range on a value *before* it is resolved to a concrete quantized type. Textually it is `!quant.calibrated<f32<MIN:MAX>>`: a float expressed-type plus a `(min, max)` pair. It carries **no scale and no zero-point** — it is the pre-quantization range record produced by a calibration pass upstream, exactly the upstream semantics.

### Storage and Accessors

The storage is the standard three-tuple, confirmed from the `StorageUniquer` instantiation symbol (`detail::CalibratedQuantizedTypeStorage` constructed from one `mlir::Type&` and two `double&`):

```c
struct CalibratedQuantizedTypeStorage {   // detail::CalibratedQuantizedTypeStorage
    Type   expressedType;                 // e.g. f32
    double min;                           // calibration low
    double max;                           // calibration high
};
```

The full API surface is present and 100% stock (cp310 addresses):

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CalibratedQuantizedType::get(Type, double min, double max)` | `0x809aaf0` | construct/unique | CERTAIN |
| `CalibratedQuantizedType::getChecked(diagFn, Type, double, double)` | `0x809ac40` | checked construct | CERTAIN |
| `CalibratedQuantizedType::verifyInvariants(diagFn, Type, double, double)` | `0x8099f90` | invariant check | CERTAIN |
| `CalibratedQuantizedType::getMin() const → double` | `0x809a870` | accessor | CERTAIN |
| `CalibratedQuantizedType::getMax() const → double` | `0x809a880` | accessor | CERTAIN |
| `Dialect::addType<CalibratedQuantizedType>` | `0x8090f70` | registered in `QuantDialect::initialize` | CERTAIN |

### The Crucial Negative — No Calibration→Uniform Conversion Here

> **GOTCHA —** the calibration arithmetic that everyone *expects* to find — `scale = (max - min) / (qmax - qmin)`, `zero_point = qmin - round(min / scale)` — is **not in this binary**. The standard MLIR-quant utilities that perform it (`fakeQuantAttrsToType`, `getUniformQuantizedTypeForMinMax`, `UniformQuantizedValueConverter`, `ExpressedToQuantizedConverter`) all have an `nm` symbol count of **0**. A reimplementer who assumes `hlo-opt` resolves a calibrated range to a scale will look for a function that does not exist.

The only caller of `CalibratedQuantizedType::get` in the whole 229 MB binary is the MLIR bytecode reader:

```c
function readType(MLIRContext*, DialectBytecodeReader&):   // (anon) 0x8095630
    ...
    readDoubleAPFloat(reader, &min);   // 0x8094ec0 — reads min (double)
    readDoubleAPFloat(reader, &max);   //          — reads max (double)
    return CalibratedQuantizedType::get(expressedType, min, max);   // 0x809aaf0
```

The type is therefore only ever **deserialized** from a serialized MLIR module — never constructed from a live calibration computation, and never consumed to derive a scale inside `hlo-opt`. `verifyInvariants` (`0x8099f90`) emits the stock `"... calibrated ..."` verifier diagnostic (the `"calibrated"` token sits in the `.rodata` error-string pool alongside the other quant verifier fragments); that is an MLIR structural check, **not** a device check.

> **NOTE —** the canonical upstream formula is given here only as reference for what `CalibratedQuantizedType` feeds *when converted elsewhere* (in the framework). It is **not** proven from this binary:
> ```text
> scale      = (max - min) / (qmax - qmin)        // qmax,qmin from the storage type
> zero_point = qmin - round(min / scale)          // asymmetric; clamped to [qmin, qmax]
> ```
> Symmetric quantization sets `min = -max`, `zero_point = 0`. `hlo-opt` performs none of this.

**Verdict.** `CalibratedQuantizedType` is a stock pass-through type. `hlo-opt` can parse and carry a calibrated range in its IR, but the range→scale+zp derivation lives **upstream** of `hlo-opt` (the Python/framework calibration step that emits an already-resolved `UniformQuantized(PerAxis)Type`). The formula is not recoverable here because it is not present — an honest negative.

---

## The Quant Type Zoo — Scale/Zero-Point Carriers `[STOCK MLIR quant]`

These are the types a calibrated range is converted **into** (upstream), and that the Neuron import bridge (`xla::getQuantizedType`, below) reconstructs from a dictionary attribute. In MLIR the scale and zero-point live **inside the element-type**, not as op operands.

| Type | `::get` | Scale/ZP carried | Accessors |
|---|---|---|---|
| `UniformQuantizedType` (per-tensor) | `0x809d370` | 1 scale, 1 zp | `getScale()` `0x8098a70`, `getZeroPoint()` `0x8098a80` |
| `UniformQuantizedPerAxisType` (per-channel) | `0x809bed0` | `scales[]`, `zps[]`, `quantizedDimension` | `getScales()` `0x8099da0`, `getZeroPoints()` `0x8099db0`, `getQuantizedDimension()` `0x8099dc0` |
| `UniformQuantizedSubChannelType` (block grid) | `0x809e620` | `DenseElementsAttr scales/zeroPoints`, `quantizedDimensions[]`, `blockSizes[]` | `getScales()` `0x8099dd0`, `getZeroPoints()` `0x8099de0`, `getBlockSizes()` `0x8099e00` |
| `AnyQuantizedType` (storage only) | `0x809e090` | none | — |

Verifiers present (stock, anon-namespace): `verifyPerAxisQuantization` (`0x808d800`), `verifySubChannelQuantization` (`0x80921f0`). Cast ops `QuantizeCastOp`/`DequantizeCastOp`/`StorageCastOp` expose `getQuantizedType`/`getStorageType`.

> **NOTE —** the per-tensor vs per-axis distinction is the `quantizedDimension == -1` sentinel: `-1` ⇒ `UniformQuantizedType` (one scale, one zp); `>= 0` ⇒ `PerAxis` (scale[], zp[] along that axis); sub-channel adds a `blockSizes[]` grid on top. This sentinel logic is decoded by `xla::getQuantizedType` on the import side.

---

## The oneDNN Int8 Backend — Provenance and Gate `[GOLDEN — Intel oneDNN]`

All of the zero-point and int8-scale flow named in this page lives in Intel oneDNN (DNNL), statically linked. This is the **XLA:CPU golden** reference int8 backend; it never emits Neuron device code.

| Anchor | Evidence | Confidence |
|---|---|---|
| **Version** = oneDNN **v3.7.3** | `.rodata` `dnnl_version::ver` `0x954fc0` = `03 00 00 00 07 00 00 00 03 00 00 00` (major 3, minor 7, patch 3) | CERTAIN |
| **Graph namespace** | `dnnl::impl::graph::dnnl_impl::` (the oneDNN *graph compiler* backend) | CERTAIN |
| **Primitive namespace** | `dnnl::impl::cpu::matmul::` (the int8 reference kernels) | CERTAIN |
| **Gate** | env string `DISABLE_DNNL_BACKEND` toggles the whole `dnnl_backend` | CERTAIN |
| **C API breadth** | 2300+ `dnnl_*` symbols incl. `dnnl_primitive_attr_set_zero_points` | CERTAIN |

> **NOTE —** oneDNN's int8 GEMM microkernels are `s8u8s32` (signed-×-unsigned → s32 accumulate). This is *why* a `u8` matmul operand must be shifted to `s8` before the GEMM (`insert_u8_to_s8_for_matmul`, below) — a quirk of the kernel ISA, not of the model.

### `dnnl_primitive_attr_set_zero_points` — Programming the GEMM

This stock oneDNN v3.x C API is how the graph backend tells a matmul/conv primitive its zero-points. The graph-fusion passes ultimately call it.

```c
// 0x3c03050
dnnl_status_t dnnl_primitive_attr_set_zero_points(
        dnnl_primitive_attr_t attr,
        int             arg,     // DNNL_ARG_SRC / DNNL_ARG_WEIGHTS / DNNL_ARG_DST
        int             mask,    // 0 = per-tensor; (1<<axis) = per-channel
        int             ndims,
        const dim_t    *groups,
        dnnl_data_type_t dt);
    // delegates to dnnl::impl::zero_points_t::set(arg, mask, ndims, groups, dt) @0x3c01fa0
```

`arg` selects which operand the zp applies to; `mask` selects per-tensor (`0`) vs per-channel (`1<<axis`). The legacy `dnnl_primitive_attr_set_zero_points_mask(attr, arg, mask)` (`0x3c02db0`) is the two-arg form. Per-primitive validators (`zero_points_ok()` on matmul/conv/deconv/brgemm) reject unsupported configurations with verbose strings such as `"primitive,create,matmul,...,unsupported zero-point configuration"`.

---

## The Zero-Point Flow — `fuse_src/dst` and `convert_runtime` `[GOLDEN — oneDNN graph]`

### Purpose

Three oneDNN graph-backend subgraph passes fold int8 zero-points into the GEMM/conv primitive. They are **registered and live**, not dead code.

| Pass | Address | Op-kind matched | Rewrite |
|---|---|---|---|
| `fuse_src_zero_points(subgraph&)` | `0x55650c0` | `dnnl_sub_zps` = `0x1238` | `fuse_op_to_successor` (absorb before GEMM) |
| `fuse_dst_zero_points(subgraph&)` | `0x5564810` | `dnnl_add_zps` = `0x1237` | `fuse_op_to_predecessor` (absorb after GEMM) |
| `convert_runtime_zero_points(subgraph&)` | `0x555a300` | int8 band `≥ 0x1237` (jump table) | rewrite zp into a runtime primitive input |

### Op-Kind Values — Directly Confirmed

These are `dnnl::impl::op_kind` *internal* enum values (not the public `dnnl_graph` op-kinds), confirmed by `cmpl` in the disassembly at the exact cited offsets:

```asm
; fuse_src_zero_points @ 0x5565194  — match dnnl_sub_zps
5565194:  81 78 18 38 12 00 00   cmpl   $0x1238,0x18(%rax)

; fuse_dst_zero_points @ 0x55648e0  — match dnnl_add_zps
55648e0:  81 78 18 37 12 00 00   cmpl   $0x1237,0x18(%rax)

; convert_runtime_zero_points @ 0x555a3a4  — dispatch on the int8 op-kind band
555a3a4:  2d 37 12 00 00         sub    $0x1237,%eax     ; followed by `ja default`
```

The op-kind is read from offset `+0x18` of the op record. `dnnl_matmul` = `0x1258` (matched by `insert_runtime_u8_to_s8_for_matmul`). `convert_runtime_zero_points`'s `sub $0x1237; ja` is a jump-table base anchored at the `0x1237` band — only int8 zp op-kinds are handled.

### Registration Order

`larger_partition_kernel_t::setup_pipeline_stage1` (`0x551c820`) builds the `pass_pipeline_t` by pushing each pass function-pointer onto a vector; `fuse_src_zero_points`'s address `0x55650c0` is written into the pipeline at `0x551d537` (`movq $0x55650c0, -0x60(%rbp)`). Resolving every function-pointer literal in stage1 against the symbol table recovers the int8-relevant slice of the pipeline:

```text
... fuse_typecast_to_matmul_or_conv, remove_quant_data_with_no_effect,
    combine_binary_post_op_scales,
STATIC SRC SIDE:
    convert_to_runtime_src_scales      → fuse_src_scales
    convert_to_runtime_src_zero_points → fuse_src_zero_points      (0x55650c0)
    insert_runtime_u8_to_s8_for_matmul (0x55c1420)
    ... lift_up_post_add_for_matmul, fuse_post_ops, fold_mul_scales,
STATIC DST SIDE:
    convert_to_runtime_dst_scales      → fuse_dst_scales
    convert_to_runtime_dst_zero_points → fuse_dst_zero_points      (0x5564810)
    defer_src_zps_for_pool, fold_sub_zps_add_zps (0x5556bb0),
DYNAMIC (runtime scale/zp TENSORS):
    convert_runtime_mul_scales, convert_runtime_zero_points        (0x555a300)
    fuse_dynamic_mul_scales_add_zps, fuse_dynamic_sub_zps_mul_scales,
    convert_dynamic_quantize_ops,
LAYOUT:
    insert_u8_to_s8_for_matmul, insert_permute_for_matmul, ... reorder_canonicalization.
```

### Algorithm — `fuse_src_zero_points`

```c
function fuse_src_zero_points(subgraph):                       // 0x55650c0
    for op in subgraph.ops:
        if op.kind != 0x1238:        continue                 // dnnl_sub_zps  @0x5565194
        consumer = op.successor                               // matmul / conv
        if not has_int8_support(consumer.kind): continue      // 0x5564600
        // fold the source-side zp INTO the consumer:
        subgraph_rewriter.fuse_op_to_successor(op)            // 0x5507500 — remove sub_zps node
        zps = op.get_attr<vector<long>>("zps")               // per-tensor or per-channel + axis
        fusion_info.set_zero_points(consumer, /*is_runtime=*/false, idx)   // 0x5549880
        // make_dnnl_primitive_attr later emits:
        //   dnnl_primitive_attr_set_zero_points(DNNL_ARG_SRC, mask, ...)  (0x3c03050)
    // numeric effect: acc = Σ_k (x_k − zp_src)·w_k  — folded so no standalone sub node remains.
```

### Algorithm — `fuse_dst_zero_points`

The mirror image on the output side: an `dnnl_add_zps` op (`0x1237`) *consuming* a matmul output is folded **backward** into the producer.

```c
function fuse_dst_zero_points(subgraph):                       // 0x5564810
    for op in subgraph.ops:
        if op.kind != 0x1237:        continue                 // dnnl_add_zps  @0x55648e0
        producer = op.predecessor                             // matmul / conv
        if not has_int8_support(producer.kind): continue
        subgraph_rewriter.fuse_op_to_predecessor(op, idx)    // 0x5507d80
        zp = op.get_attr<long>(...)                          // scalar dst zp onto fusion_info
        // make_dnnl_primitive_attr emits:
        //   dnnl_primitive_attr_set_zero_points(DNNL_ARG_DST, ...)
    // numeric effect: out_q = round(acc · dst_scale) + zp_dst  — the +zp_dst is folded in.
```

### Algorithm — `convert_runtime_zero_points` (the dynamic path)

When zps arrive as runtime **tensors** (dynamic / per-token quant) rather than static attributes:

```c
function convert_runtime_zero_points(subgraph):               // 0x555a300
    for op in subgraph.ops:
        case = op.kind − 0x1237                               // dispatch @0x555a3a4 (sub; ja)
        if case out of int8 band: continue                   // default branch
        is_runtime = op.get_attr<bool>("runtime")
        // rewrite so the zp is consumed as a runtime primitive INPUT:
        new = build_dnnl_op_with_zp_tensor_operand(op)       // set_attr<vector<long>>, connect_input
        fusion_info.with_runtime_zps(new, /*flag=*/true, idx) // 0x550def0
        erase_static_qtype_attr(new); rewire_consumers(new)
    // paired with convert_to_runtime_{src,dst}_zero_points so static & tensor zps
    // converge on ONE runtime form the oneDNN primitive can take.
```

> **NOTE —** the runtime-vs-static distinction: **static** zps (compile-time constants) are folded into the primitive **attr** (`fuse_src/dst_zero_points`); **runtime** zps (tensors) become primitive **inputs** (`convert_runtime_zero_points` + `fuse_dynamic_*`). Either way the final form is a oneDNN primitive attr/arg consumed by the matmul/conv kernel — entirely on the CPU golden path.

### `insert_u8_to_s8_for_matmul` — the s8u8s32 shift

```c
function insert_runtime_u8_to_s8_for_matmul(subgraph):        // 0x55c1420
    for op where op.kind == 0x1258 and operand.dtype == u8:   // dnnl_matmul, u8 input @0x55c14fc
        // oneDNN GEMM is s8u8s32 → map u8[0,255] → s8[-128,127]:
        insert sub_zps(128) with a compensating zp adjustment  // the 128 literal was not isolated
        thread with_runtime_scales / with_runtime_zps flags
```

> **QUIRK —** this 128-shift exists purely because the JIT kernel `jit_avx512_core_gemm_s8u8s32_kern` cannot take two unsigned operands. It is stock oneDNN graph behavior, CPU-only, and has nothing to do with the device.

---

## The Golden Int8 GEMM — `linalg.quantized_matmul` and `ref_matmul_int8` `[GOLDEN]`

There are two representations of the *same* CPU golden int8 matmul.

### (A) MLIR level — `linalg::QuantizedMatmulOp` `[STOCK MLIR linalg]`

`QuantizedMatmulOp` (`regionBuilder` `0x7c6c7d0`) and `QuantizedBatchMatmulOp` (`0x7c6c7c0`) are upstream MLIR linalg **named ops**, registered through the generic `addNamedOpBuilders<...,QuantizedBatchMatmulOp,QuantizedMatmulOp,...>` template (the giant StringMap builder, alongside every other linalg op) — **not** Neuron-authored. They take 4 inputs `(lhs, rhs, lhs_zeropoint, rhs_zeropoint)` + 1 init (destination-passing style). The region is the textbook quantized contraction:

```c
function QuantizedMatmulOp::regionBuilder(builder, block, attrs):   // 0x7c6c7d0
    // acc(i,j) += ( ext_i32(lhs(i,k)) − ext_i32(lhs_zp) )
    //           * ( ext_i32(rhs(k,j)) − ext_i32(rhs_zp) )
    // zero-points SUBTRACTED from each operand before the i32 MAC; NO scale here
    // (scales applied by a following requant).
```

> **NOTE —** the 4-input shape comes from the stock upstream op definition, not from this binary. The `.rodata` pool does contain a `"quantized_matmul"` token, but the nearby `"Expected four"` fragment is a merged/spliced string and does not stand on its own as evidence.

### (B) Primitive level — `ref_matmul_int8_t` `[STOCK oneDNN]`

This is the actual reference int8 GEMM the CPU executes — the golden output the Neuron device result is numerically checked against.

```c
function ref_matmul_int8_t::execute_ref(ctx):                  // 0x3fb9090
    src = ctx.arg(0x01);  wei = ctx.arg(0x21);  dst = ctx.arg(0x11)   // SRC/WEIGHTS/DST ids
    for (m, n):
        acc = 0
        for k: acc += ( src(m,k) − src_zp ) * ( wei(k,n) − wei_zp )   // s32 accumulate
        out  = saturate_cast<dst_dt>(
                  round( acc * src_scale * wei_scale * dst_scale_inv ) + dst_zp )  // requant
    // per-tensor OR per-channel scales/zps (mask-selected); fastpath uses JIT s8u8s32 kernels.
```

`ref_matmul_int8_t::pd_t::init` (`0x3f8fc30`) gates on `attr_zero_points_ok` (`0x3f86970`) / `attr_scales_ok`. The faster int8 conv variant `gemm_x8s8s32x_convolution_fwd_t` (`0x3f753a0`) carries the same `s8u8s32` semantics with `apply_src_zero_point<u8/s8>`.

> **NOTE —** (B) is the "reference int8 matmul the device output is checked against": the XLA:CPU `CpuCompiler` (`xla::cpu::CpuCompiler::RunBackend` present in this binary) compiles int8 HLO to oneDNN matmul/conv primitives and runs them on the host. 100% Intel oneDNN; no Neuron device code.

---

## The Device-vs-Golden Split — the Honesty Crux

The sharpest question: does the Neuron **device** path use calibration or any int8 per-tensor/per-channel scale, or is calibration purely a golden/accuracy concern?

**Answer, grounded three independent ways: for int8, calibration and the scale+zero-point flow are purely a GOLDEN / frontend-IR concern. No int8 scale and no int8 zero-point reach Neuron device codegen.**

The grounding chain:

1. **Calibration never resolves to a scale here.** `CalibratedQuantizedType` is only deserialized (bytecode reader); the conversion utilities are absent (count 0). `hlo-opt` derives no scale from a calibrated range.

2. **Int8 scale/zp land in STOCK stablehlo/MHLO IR, on the golden path.** The MLIR quant types and their scale+zp are reconstructed by `xla::getQuantizedType` (`0x75d65e0`), but its **only** caller is `xla::ImportCustomCallAsOp` (`0x75d6ac0`) — the HLO→MLIR *importer*. `IsOpEncodedCustomCall` (`0x75d6410`) gates it on a custom-call target beginning `"mhlo."`, confirmed in disassembly:

   ```asm
   75d6423:  81 3a 6d 68 6c 6f      cmpl   $0x6f6c686d,(%rdx)   ; "mhlo"
   75d6442:  80 7a 04 2e            cmpb   $0x2e,0x4(%rdx)       ; '.'
   ```

   So int8 scale/zp rebuild `mhlo.uniform_quantize` / `mhlo.uniform_dequantize` ops for the reference/legalization (golden) path — **not** a device tensorizer.

3. **The entire int8 flow is oneDNN inside XLA:CPU.** `linalg.quantized_matmul`, `ref_matmul_int8`, `fuse_src/dst_zero_points`, `convert_runtime_zero_points`, `dnnl_primitive_attr_set_zero_points`, the whole `lower.cpp` pipeline — all Intel oneDNN v3.7.3, gated by `DISABLE_DNNL_BACKEND`. CPU/golden only.

4. **No device tensorizer reads a quant scale/zp.** No `bir::`/`penguin::` symbol reads a quant `scale` or `zero_point` attribute (`nm` empty). (The string `"rescale"` is `xla::IndexingMap::RescaleSymbols`, an affine concept, unrelated to quant.)

5. **The only device quant passes are MX.** `LegalizeQuantizeMX` (`0x1efc4f0`) + `LegalizeScaledMatmul` (`0x1efe1a0`) — MX-FP8 microscaling, not int8, not calibration. Integer dots that reach HLO are F32-emulated by `NeuronIntMatmulDowncast` (`0x1faa9b0`).

### The Only Device Scale — MX `E8M0`, No Zero-Point

```text
DEVICE quant   = MX-FP8 per-block scale, NO zero-point, NO calibration (runtime EMAX)
GOLDEN/CPU quant = int8 per-tensor/per-channel scale + zp, oneDNN, optionally fed by a
                   calibrated range resolved UPSTREAM of hlo-opt
calibration (min/max → scale+zp) = NOT performed by this binary at all
```

The only scale that reaches the device is the MX per-block scale (OCP MXFP8): scale type `builtin.f8E8M0FNU` (string confirmed in `.rodata`), produced by `QuantizeMX` as a second tuple output and consumed by `ScaledMatmul` (`lhs_scale`, `rhs_scale`, `side_input_scale` — all three strings confirmed).

> **QUIRK —** the MX scale is per-32-element **block**, not per-tensor/per-channel, and carries **no zero-point** (MX is symmetric, `zp = 0` implicitly). There is no calibration step feeding it: the per-block scale is computed *at runtime* by `QuantizeMX` from the block's own values (`scale_method = "EMAX"`), not from a calibrated min/max range. A reimplementer who models the device scale as a calibration-derived per-tensor int8 scale will be wrong on every axis — granularity, derivation time, and the presence of a zero-point.

---

## Stock vs Golden vs Device Ledger

| Component | Bucket | Owner |
|---|---|---|
| `CalibratedQuantizedType` + all quant carrier types; the quant bytecode reader | **STOCK** | upstream MLIR `quant` |
| `linalg.quantized_matmul` / `QuantizedBatchMatmul` named ops | **STOCK** | upstream MLIR `linalg` |
| `convert_runtime_zero_points`, `fuse_src/dst_zero_points`, the `lower.cpp` int8 pipeline, `dnnl_primitive_attr_set_zero_points`, `ref_matmul_int8` | **GOLDEN** | Intel oneDNN v3.7.3 (XLA:CPU) |
| `xla::getQuantizedType` + `ImportCustomCallAsOp` `"mhlo."` gate | **GOLDEN (Neuron-authored frontend)** | HLO→MLIR import — golden side, not device |
| MX-FP8 per-block `E8M0` scale (`QuantizeMX` / `ScaledMatmul`) | **DEVICE** | `LegalizeQuantizeMX` / `LegalizeScaledMatmul` |
| Calibration → scale+zp **derivation** (`(max−min)/(qmax−qmin)`, zp) | **NOT PRESENT** | upstream Python/framework |

---

## Evidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| The calibration→scale derivation is absent | `fakeQuantAttrsToType`, `getUniformQuantizedTypeForMinMax`, `UniformQuantizedValueConverter`, `ExpressedToQuantizedConverter` each have `nm` count **0** in `hlo-opt_function_addresses.json` | CERTAIN |
| `fuse_src_zero_points` matches `0x1238`, `fuse_dst` matches `0x1237` | `objdump`: `0x5565194: cmpl $0x1238,0x18(%rax)`; `0x55648e0: cmpl $0x1237,0x18(%rax)` | CERTAIN |
| oneDNN is v3.7.3 | `.rodata` `0x954fc0` = `03 00 00 00 07 00 00 00 03 00 00 00` | CERTAIN |
| Int8 scale/zp enter only through the `"mhlo."`-gated importer | `IsOpEncodedCustomCall` `0x75d6423: cmpl $0x6f6c686d` ("mhlo") and `0x75d6442: cmpb $0x2e` (".") — two separate instructions | CERTAIN |
| `ImportCustomCallAsOp` is the *only* caller of `getQuantizedType` | `getQuantizedType` (`0x75d65e0`) and `ImportCustomCallAsOp` (`0x75d6ac0`) are co-located in the import bridge; the sole-caller claim rests on an xref sweep | HIGH |
| The only device scale is MX `E8M0` | `f8E8M0FNU`, `lhs_scale`, `rhs_scale`, `side_input_scale` strings present; `LegalizeQuantizeMX::Run` `0x1efc4f0` and `LegalizeScaledMatmul::Run` `0x1efe1a0` present; no `bir::`/`penguin::` quant-scale reader | CERTAIN |
| MX carries no zero-point | an MX-format property; no explicit zp-absence assertion was single-stepped | HIGH |

### Limits of this reading

Three descriptions rest on stock oneDNN/MLIR semantics plus recovered node/fold structure rather than single-stepped execution: the `acc = Σ(src−zp)(wei−zp)` arithmetic, the 4-input linalg op shape, and the literal `128` in the u8→s8 shift. The claim that a calibrated range was resolved *upstream* of `hlo-opt` is [INFERRED] by elimination — the derivation code is absent from this binary and the type only ever arrives pre-serialized, but nothing here shows the upstream step directly.

---

## Related Components

| Name | Relationship |
|---|---|
| `LegalizeQuantizeMX` / `LegalizeScaledMatmul` | the only device quant passes — MX-FP8, the sole device scale |
| `NeuronIntMatmulDowncast` (`0x1faa9b0`) | integer dots reaching HLO are F32-emulated, not int8-quantized on device |
| `xla::cpu::CpuCompiler::RunBackend` | drives the oneDNN golden int8 reference |

## Cross-References

- [INT8 Uniform-Quantize / Dequantize Legalization (golden-only)](int8-quantize-legalization.md) — Part 4.28; `uniform_quantize`/`dequantize` legalization and the device-vs-golden taxonomy this page's int8 flow feeds
- [MX-FP8 Microscaling Legalization](mx-fp8-legalization.md) — Part 4.29; `QuantizeMX`/`ScaledMatmul`, the `E8M0` per-block scale, the only quant that reaches the device
- [Numeric Semantics](../numerics/numerics.md) — Part 9; the requant/round/saturate numerics shared by the golden int8 GEMM and the MX path
