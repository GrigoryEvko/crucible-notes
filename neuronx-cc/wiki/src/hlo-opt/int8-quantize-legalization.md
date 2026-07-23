# INT8 Uniform-Quantize / Dequantize Legalization

> *All addresses on this page are virtual addresses (VMA) for `hlo-opt` from neuronx_cc 2.24.5133.0+58f8de22 (cp310 build, BuildID xxHash `93dd8bd9bd4c697b`); resolve them via `objdump --start-address` or the VMA-keyed `disasm/` sidecars. VA ≠ raw file offset here: `.text` file_off = VA − 0x201000, `.rodata` file_off = VA − 0x200000. The cp311/cp312 builds share the same layout. Other versions will differ.*

## Abstract

The neuronx_cc `hlo-opt` driver is the MLIR/XLA front of the Neuron compiler — a 229 MB, **not-stripped** binary that legalizes incoming HLO/StableHLO before handing a lowered graph to the tensorizer. It carries a complete INT8 quantization apparatus: the stock `mhlo`/`stablehlo` `uniform_quantize`/`uniform_dequantize` ops, the legacy `mhlo.dequantize` `DequantizeMode`, the MLIR `quant` type zoo (per-tensor / per-axis / sub-channel), and a Neuron-authored dictionary deserializer `xla::getQuantizedType` that rebuilds a `!quant.uniform` element type from an HLO custom-call attribute dictionary. To anyone reading the symbol table, it looks like INT8 quantization is a first-class device feature.

It is not. The decisive finding of this page is a **negative result**, proven from the binary: **no int8 scale or zero-point ever reaches Neuron device code.** Every int8 quantize path in `hlo-opt` terminates on the XLA:CPU golden-reference side — either the upstream MLIR `quant`-dialect arithmetic lowering or Intel oneDNN's graph backend (`dnnl::impl::graph::dnnl_impl::`), both of which compute reference outputs on the host and never emit a Neuron PE instruction. The *only* genuinely low-precision quantize that targets the device is a **separate, frontend-authored** path: `QuantizeMX` + `ScaledMatmul`, which is **FP8 microscaling** (OCP MXFP8, `float8_e5m2`/`float8_e4m3fn`, `block_size=32`), not int8 at all. And the one Neuron pass that touches integer dots — `NeuronIntMatmulDowncast` — proves the device has *no* native int8 GEMM on this route: it converts the dot to F32, computes it in F32, and casts back.

This page tags every code path explicitly as **[STOCK]** (upstream MLIR/XLA/oneDNN), **[GOLDEN]** (XLA:CPU reference, never on device), or **[DEVICE]** (emits Neuron accelerator code). The structure walks the four-plus-one path taxonomy, anchors `xla::getQuantizedType` and its per-tensor/per-axis sentinel branch in disassembly, and closes with the device-vs-golden ledger that backs the negative result.

For reimplementation, the contract is:

- The `uniform_quantize`/`uniform_dequantize` op semantics and how `scale`/`zero_point` live *inside* the quantized result element type, not as operands.
- `xla::getQuantizedType`: the seven dictionary keys, the array-vs-scalar reads, and the `quantization_dimension == -1` sentinel that selects per-tensor vs per-axis.
- The four-plus-one quantize path taxonomy and which single pair (`QuantizeMX`/`ScaledMatmul`) emits device code — and that it is FP8, not int8.
- The int8 arithmetic kernel (scale-reciprocal-mul, zero-point add, round-to-nearest-even, clamp, convert) as a **golden-only** lowering.

| | |
|---|---|
| **Binary** | `neuronxcc/starfish/bin/hlo-opt` (229 MB, not stripped) |
| **Neuron deserializer** | `xla::getQuantizedType(mlir::DictionaryAttr&)` @ `0x75d65e0` |
| **Per-tensor/per-axis branch** | `cmp $-1, quantDim; je` @ `0x75d6889` |
| **Legacy mode symbolizer** | `mlir::mhlo::symbolizeDequantizeMode` @ `0x76dece0` (only `MIN_COMBINED`) |
| **Device quantize pass** | `xla::LegalizeQuantizeMX::Run` @ `0x1efc4f0` (matches `kCustomCall` "QuantizeMX") |
| **Device matmul pass** | `xla::LegalizeScaledMatmul::Run` @ `0x1efe1a0` |
| **Int-dot F32 emulation** | `xla::hilo::NeuronIntMatmulDowncast` @ `0x1fa9f10` / `0x1faa9b0` |
| **Golden CPU backend** | `dnnl::impl::graph::dnnl_impl::` (oneDNN), e.g. `lift_up_quantize` @ `0x5548ee0` |
| **Decisive finding** | INT8 uniform_quantize is GOLDEN/CPU-only; the device quantize is FP8 microscaling |

---

## The Path Taxonomy at a Glance

`hlo-opt` contains four distinct quantization code paths plus one integer-dot fallback. Exactly one pair emits device code, and it is not int8. Read this table first; the rest of the page expands each row.

| Path | What it is | Granularity source | Tag |
|---|---|---|---|
| **A** | `mhlo`/`stablehlo.uniform_quantize`/`dequantize` + `!quant.uniform` types + quant-dialect int8 legalization | result element type | **[STOCK / GOLDEN]** |
| **B** | legacy `mhlo.dequantize` + `DequantizeMode=MIN_COMBINED` (tf.Dequantize range-based) | `min`/`max` range table | **[STOCK / GOLDEN]** |
| **C** | oneDNN graph `Quantize`/`Dequantize`/`Dynamic*` + `convert_dynamic_quantize_ops` + `lift_up_quantize` + fusions | op attribute hashtable | **[STOCK oneDNN / GOLDEN]** |
| **D** | `xla::hilo` `QuantizeMX` + `ScaledMatmul` — **FP8 microscaling** | `backend_config` JSON | **[NEURON / DEVICE]** |
| **E** | `xla::hilo::NeuronIntMatmulDowncast` — integer dot → **F32-emulated** | shape leaf prim-type | **[NEURON / DEVICE]** |

> **GOTCHA —** the presence of `mlir::quant::UniformQuantizedType`, the `uniform_quantize` op, and `xla::getQuantizedType` in the symbol table does **not** imply an int8 device path. Those symbols belong to Paths A/B/C, all of which terminate on the host golden-reference side. A reimplementer who wires `uniform_quantize` straight to a PE-array int8 GEMM is building a path the binary does not have. The device int8 "GEMM" is Path E (F32 emulation); the device low-precision GEMM is Path D (FP8). See [§7](#7-device-vs-golden-the-ledger).

---

## Path A — The Stock Ops and Type Zoo  [STOCK]

### Purpose

`mhlo.uniform_quantize`/`uniform_dequantize` and their `stablehlo` twins are the canonical XLA quantization ops. They are 100% upstream — full op definitions, verifier traits, and bidirectional `mhlo`↔`stablehlo` converters are present, and Neuron adds no custom op here. The quantize *arithmetic* is not in the op def; it lives in the quant-dialect legalization ([§4](#4-the-int8-arithmetic-golden)).

### The ops

`uniform_quantize` takes a float (expressed) tensor and produces a `RankedTensor` whose **element type** is a `mlir::quant::UniformQuantized(*)Type` — the `scale`/`zero_point` ride *inside* the result type, not as operands (`real -> !quant.uniform<i8:f32, scale:zp>`). `uniform_dequantize` inverts it: quantized input, float output, with the output float type inferred from the quantized input's expressed type.

Both ops are emitted into the giant 1:1 stock op-converter template. The `RewritePatternSet::add<...>` symbol enumerates ~130 ops alphabetically, and `UniformDequantizeOp` / `UniformQuantizeOp` appear verbatim in both directions:

```text
mlir::stablehlo::HloToStablehloOpConverter<UniformDequantizeOp>   (mhlo  -> stablehlo)
mlir::stablehlo::HloToStablehloOpConverter<UniformQuantizeOp>
mlir::stablehlo::StablehloToHloOpConverter<UniformDequantizeOp>   (stablehlo -> mhlo)
mlir::stablehlo::StablehloToHloOpConverter<UniformQuantizeOp>
```

> **NOTE —** these are the generic stock converters, part of the ~130-op `RewritePatternSet::add` template (e.g. `0x761a840` for the Hlo→Stablehlo direction). There is no Neuron-custom quantize converter: the demangled symbol carries the full op list, with `UniformQuantize`/`UniformDequantize` sitting between `UnaryEinsumOp` and `WhileOp`.

### The quantized type zoo

Five MLIR `quant` element types are present (all stock):

| Type | Granularity | Parameters |
|---|---|---|
| `quant::UniformQuantizedType` | per-tensor | single `scale` + single `zero_point` |
| `quant::UniformQuantizedPerAxisType` | per-axis | `scale[]` + `zp[]` + `quantizedDimension` |
| `quant::UniformQuantizedSubChannelType` | sub-channel | block-wise scale grid |
| `quant::CalibratedQuantizedType` | calib range | `(min, max)` pre-quant — see [4.30](#cross-references) |
| `quant::AnyQuantizedType` | storage-only | no params |

The `::get` signatures, recovered from the call sites in `xla::getQuantizedType`:

```c
// flags bit0 = signed storage, from IntegerType::getSignedness(storage_type)
UniformQuantizedType::get(uint flags, Type storageType, Type expressedType,
                          double scale, long zp, long storageMin, long storageMax);
                          // @0x809d370 — per-tensor

UniformQuantizedPerAxisType::get(uint flags, Type storageType, Type expressedType,
                          ArrayRef<double> scales, ArrayRef<long> zps,
                          int quantizedDimension, long storageMin, long storageMax);
                          // @0x809bed0 — per-axis
```

The stock `stablehlo` quantized-op verifier strings are present in the pool, confirming these are the upstream quantized-`DotGeneral`/`Convolution` verifiers, not Neuron re-authored:

```text
"expect same quantization dimension size for operand and result"
"expect same quantization scale and zero_point but got"
"illegal quantized dimension:"
"expects all operands and results to be either quantized or non-quantized"
```

> **NOTE —** sub-channel exists in the binary (`UniformQuantizedSubChannelType`, `CalibratedQuantizedTypeStorage` @ `0x8096690`) but is **not** reached through the Neuron dict deserializer in [§3](#3-xlagetquantizedtype-the-neuron-deserializer) — it has no `block_size` read there. Sub-channel arrives only via the stock `stablehlo` quant axis machinery.

---

## Path B — Legacy DequantizeMode  [STOCK / GOLDEN]

### Purpose

`mlir::mhlo::DequantizeModeAttr` is the mode attribute on the **old** `mhlo.dequantize` op — the TF-style int8 dequant with a `min`/`max` range table, distinct from `uniform_dequantize`. It is a stock, CPU/reference-only, near-dead path with exactly one recognized mode.

### Algorithm

`symbolizeDequantizeMode` @ `0x76dece0` recognizes a single string. The disassembly is unambiguous:

```c
function symbolizeDequantizeMode(StringRef s):     // 0x76dece0
    if s.size() == 0xC                              // cmp $0xc,%rsi  (len 12)
       and s[0..8] == "MIN_COMB"                    // movabs $0x424d4f435f4e494d
       and s[8..12] == "INED":                      // cmpl $0x44454e49,0x8(%rdi)
        return 1                                     // mov $0x1,%eax  -> MIN_COMBINED
    return <default = 0>                             // anything else
```

So `DequantizeMode = { <default> = 0, MIN_COMBINED = 1 }`. Pure stock `tf.Dequantize` legacy semantics; never device-bound. The `movabs $0x424d4f435f4e494d` literal decodes ASCII-LE to `MIN_COMB` and the `cmpl $0x44454e49` to `INED`.

> **QUIRK —** the `DequantizeMode` attribute and the `uniform_dequantize` op are easily conflated because both say "dequantize". They are different ops with different lowerings: `DequantizeMode` is the legacy range-table path (Path B), `uniform_dequantize` is the quant-dialect path (Path A). Neither targets the device.

---

## §3. `xla::getQuantizedType` — the Neuron Deserializer  [NEURON]

### Purpose

This is the one genuinely Neuron-authored function in the int8 type machinery (namespace `xla::`, no `mlir::`/`dnnl::` prefix), at `0x75d65e0`. It is the bridge that reads a Neuron HLO custom-call / frontend dictionary attribute and reconstructs the MLIR `quant` element type — translating a serialized quantized-tensor description back into a `!quant.uniform` type. It builds Path-A types, but is itself Neuron code.

### Algorithm

The function reads seven dictionary keys in order and branches on the quantization dimension. Disassembly-confirmed (key reads via `DictionaryAttr::get(StringRef)` @ `0x9429d60`, with the value extracted by the matching accessor):

```c
function getQuantizedType(DictionaryAttr& dict):                    // 0x75d65e0
    // --- scale: ArrayAttr<FloatAttr> -> std::vector<double> ---
    a = dict.get("scale")                                          // 0x75d663c
    for each FloatAttr f in a.getValue():                          // ArrayAttr::getValue 0x9426060
        scales.push_back(f.getValueAsDouble())                     // FloatAttr 0x9426bb0

    // --- zero_point: ArrayAttr<IntegerAttr> -> std::vector<long> ---
    a = dict.get("zero_point")                                     // 0x75d66fb
    for each IntegerAttr i in a.getValue():
        zps.push_back(i.getInt())                                  // IntegerAttr::getInt 0x9426c60

    quantDim    = dict.get("quantization_dimension").getInt()      // 0x75d678d, scalar long
    storageMin  = dict.get("storage_min").getInt()                // 0x75d67bb
    storageMax  = dict.get("storage_max").getInt()                // 0x75d67ed
    storageTy   = dict.get("storage_type").getValue()             // TypeAttr::getValue 0x9426230
    expressedTy = dict.get("expressed_type").getValue()           // 0x75d684c
    flags       = signednessBit(storageTy.getSignedness())        // IntegerType 0x94593c0

    // ----- the decisive per-tensor vs per-axis branch -----
    if quantDim == -1:                                             // cmp $0xffffffffffffffff,%rbx  @0x75d6889
        return UniformQuantizedType::get(                         // je 0x75d6900 -> 0x809d370
                   flags, storageTy, expressedTy,
                   scales[0], zps[0], storageMin, storageMax)     // per-tensor: only [0] used
    else:
        return UniformQuantizedPerAxisType::get(                  // fall-through 0x75d68c4 -> 0x809bed0
                   flags, storageTy, expressedTy,
                   scales, zps, quantDim, storageMin, storageMax) // per-axis: full arrays
```

> **QUIRK —** `scale` and `zero_point` are **always** read as arrays, even for per-tensor. The per-tensor branch simply uses `scales[0]`/`zps[0]` and discards the rest. A reimplementer who serializes a scalar `scale` for per-tensor will produce a dictionary this deserializer rejects (it calls `ArrayAttr::getValue`, not `FloatAttr::getValueAsDouble`, on the top-level `scale` attr). Both `ArrayAttr::getValue` calls, at `0x75d665e` and `0x75d6669`, precede the per-element loop.

The sentinel is the central fact: **`quantization_dimension == -1` ⟺ per-tensor; `>= 0` ⟺ per-axis.** The `cmp $0xffffffffffffffff,%rbx` at `0x75d6889` is the literal `-1` compare; the `je` at `0x75d688d` jumps to the per-tensor builder, and the fall-through at `0x75d68c4` calls the per-axis builder. All seven key strings are present in the string pool at the expected lengths (`scale` len 5, `zero_point` len 10, `quantization_dimension` len 22, `storage_min`/`storage_max` len 11, `storage_type` len 12, `expressed_type` len 14).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xla::getQuantizedType(DictionaryAttr&)` | `0x75d65e0` | Neuron dict → `!quant.uniform` type | CERTAIN |
| `UniformQuantizedType::get` | `0x809d370` | per-tensor builder (sentinel `-1`) | CERTAIN |
| `UniformQuantizedPerAxisType::get` | `0x809bed0` | per-axis builder (`quantDim >= 0`) | CERTAIN |
| `DictionaryAttr::get(StringRef)` | `0x9429d60` | key lookup (7 calls) | CERTAIN |
| `IntegerType::getSignedness` | `0x94593c0` | signed-storage flag bit | HIGH |

---

## §4. The INT8 Arithmetic  [STOCK / GOLDEN]

### Purpose

The scale-mul / zero-point-add / round / clamp / convert sequence that turns a `!quant.uniform` element type into integer math is **not** a Neuron pass. It is the upstream MLIR `quant`-dialect + `stablehlo` quantized-op legalization, lowering on the XLA:CPU reference path. Neuron does not re-author int8 quantize math.

### Algorithm

```c
// uniform_quantize  (real f32 -> int8)            [STOCK, matches MLIR/SHLO]
q = clamp( round_to_nearest_even( real * (1.0 / scale) ) + zero_point,
           storage_min, storage_max )
result_storage = convert<i8>(q)
    // per-tensor : scalar scale,zp broadcast over the whole tensor
    // per-axis   : scale[c],zp[c] broadcast along quantizedDimension c

// uniform_dequantize  (int8 -> real f32)
real = (convert<f32>(q) - zero_point) * scale
```

`round_to_nearest_even` is the MLIR `quant` default (banker's rounding). The CPU emitter side implements the same: the binary carries xnnpack reference ukernels `unary_ukernel_quantized<xnnpack::quantized<signed char,...>, ConvertOp<float,float>>` (e.g. `0x61aa9a0`), which round-to-nearest on the host. `storage_min`/`storage_max` come straight from the storage type's bit-width and signedness (`i8 -> [-128,127]`, `u8 -> [0,255]`, plus the narrower 2/4-bit and `i16`/`i32` forms the verifier strings enumerate).

> **NOTE —** the exact round-even/clamp formula is the documented MLIR `quant` semantics, corroborated by the xnnpack `RoundToNearest`-class quantized ukernels present in the binary; the precise instruction sequence was not single-stepped. On the device, the numeric `convert<i8>` of operands rides `bir::CastToNewDType` at the tensorizer level (see [4.x — CastToNewDType](#cross-references)); the quantize *semantics* are not a dedicated device op. The `MIN_COMBINED` path (Path B) is a third, legacy, CPU-only lowering, unused by the device.

---

## §5. Path D — QuantizeMX + ScaledMatmul  [NEURON / DEVICE]

### Purpose

This is the honesty crux. The Neuron device does **not** consume `uniform_quantize`/int8 directly. The one device quantize path is **microscaling FP8** (OCP MXFP), implemented as a pair of `xla::hilo` HLO passes that rewrite a quantized cluster into packed device custom calls. `hilo` is Neuron's HLO-IR layer; both passes are `HloModulePass` over `xla::HloModule`. Source paths leak in the string pool: `hilo/hlo_passes/LegalizeQuantizeMX.cc`, `hilo/hlo_passes/LegalizeScaledMatmul.cc`. Pass flags: `legalize-quantize-mx`.

### Entry Point

```text
xla::LegalizeQuantizeMX::Run   @0x1efc4f0   ── lowers an existing "QuantizeMX" custom call
xla::LegalizeScaledMatmul::Run @0x1efe1a0   ── lowers the quantized-matmul cluster
  registered via RegisterLegalizeQuantizeMX / RegisterLegalizeScaledMatmul
```

### Algorithm

The decisive proof that Path D is **disjoint** from int8 `uniform_quantize` is the input matcher. `LegalizeQuantizeMX::Run` does **not** match `uniform_quantize`; it matches a `kCustomCall` whose target string is literally `"QuantizeMX"`:

```c
function LegalizeQuantizeMX_Run(HloModule* m, ...):                  // 0x1efc4f0
    for instr in computation->MakeInstructionPostOrder():
        if instr->opcode() != kCustomCall:                          // cmpb $0x2b,0x14(%rax) @0x1efc594
            continue                                                 // 0x2b == 43 == kCustomCall
        if instr->custom_call_target().compare("QuantizeMX") != 0:   // string::compare @0x1efc5ad
            continue
        raw = instr->backend_config().GetRawStringWithoutMutex()    // read under a Mutex
        cfg = nlohmann::json::parse(raw)                            // json_abi_v3.11.3
        dtype        = cfg.value<string>("dtype", "")
        dim          = cfg.value<int>("dim")
        block_size   = cfg.value<int>("block_size")
        scale_method = cfg.value<string>("scale_method")            // "EMAX" only
        // build x4-packed U32 shape + per-block scale shape
        new = HloInstruction::CreateCustomCall(tuple_shape, {input},
                  "QuantizeMX", backend_config_str, api_version)
        computation->ReplaceInstruction(instr, new)
```

> **QUIRK —** `LegalizeQuantizeMX` LOWERS an *already-present* `QuantizeMX` custom call (emitted upstream by the Neuron frontend) into its packed device form. It is matched by `opcode == kCustomCall (0x2b)` + a `string::compare` against `"QuantizeMX"`, with **no reference to `uniform_quantize` anywhere**. This is the cleanest proof that the device quantize is a separate, frontend-authored MX custom call, fully disjoint from the stock int8 op — `cmpb $0x2b,0x14(%rax)` at `0x1efc594`, `string::compare` at `0x1efc5ad`.

### The QuantizeMX custom-call contract

Recovered from the verifier error strings:

| Field | Constraint | Evidence string |
|---|---|---|
| operands | exactly 1 (input tensor) | (op verifier) |
| input element type | `BF16` or `F16` only | `"only BF16 and F16 are supported"` |
| output dtype | `float8_e5m2` or `float8_e4m3fn` | `"only 'float8_e5m2' and 'float8_e4m3fn' are supported"` |
| `block_size` | `== 32` (hard) | `"Use block_size=32 per OCP MXFP standard: …ocp-microscaling-formats-mx-v1-0…"` |
| `dim` | last (`-1`) or second-to-last (`-2`) | (op verifier) |
| packing | input dim along packing divisible by 4 | `"must be divisible by 4 for x4 packing"` |
| `scale_method` | `"EMAX"` only | `"Use scale_method='EMAX'."` |
| output | tuple of exactly 2: (U32-packed data, per-block scale) | `"must return a tuple with exactly 2 outputs"` |
| `backend_config` | JSON with `{dtype, dim, block_size, scale_method}` | `"Ensure the backend_config is valid JSON with required fields: dtype, dim, block_size, scale_method."` |

> **GOTCHA —** the device quantize target is **FP8, not int8.** `QuantizeMX` outputs `float8_e5m2`/`float8_e4m3fn`, packs four FP8 values into one U32 word, and emits the per-block (E8M0) scale as a *separate second tuple output* — OCP-MX style. There is no int8 storage type anywhere in this contract. An int8 model that reaches the frontend as a `uniform_quantize` does **not** become a `QuantizeMX`. All eight contract strings are present in the pool.

### ScaledMatmul — the consumer

`LegalizeScaledMatmul::Run` @ `0x1efe1a0` rewrites a quantized-matmul cluster into a `"ScaledMatmul"` custom call (`CreateCustomCall` → `ReplaceInstruction`). Contract from verifier strings:

- exactly 4 operands: `(lhs, rhs, lhs_scale, rhs_scale)`
- LHS input type `U32` ("packed MX format from QuantizeMX"); LHS/RHS rank ≥ 2 (`"RHS tensor must have rank >= 2"`)
- output type `F32` or `BF16` (`"only F32 and BF16 are supported"`)
- batch dims must match; contracting dims must match in size and not overlap batch dims (`"contracting dimension … that overlaps with batch dimension"`); batch index bounds-checked vs rank (`"out of bounds for tensor of rank"`)
- `side_input_scale` — additional scale operand/attr (fused residual / side input)

`ScaledMatmul` consumes the U32-packed MX tensor + per-block scale produced by `QuantizeMX` and performs the microscaled matmul on the Neuron PE array. The `(QuantizeMX -> ScaledMatmul)` graph fuses naturally, so no separate quantize hoist is needed on the device side — the `lift_up_quantize` hoist is the oneDNN/CPU one ([§6](#6-path-c--onednn-cpu-golden-stock-golden)).

> **GOTCHA —** a string-pool fragment near these passes reads `…replace_quant_dag…` and looks like a Neuron pass name. It is not: it is the oneDNN function `replace_quant_data_with_binary_post_op` (`dnnl::impl::graph::dnnl_impl::`) spliced against an adjacent string. There is no `replace_quant_dag` pass.

---

## Path E — NeuronIntMatmulDowncast  [NEURON / DEVICE]

### Purpose

`xla::hilo::NeuronIntMatmulDowncast` is an HLO `OpExpanderPass` (`neuron_int_matmul_downcast.cc`, flag `neuron-int-matmul-downcast`). It is the closest thing to an int8 device path, and the answer is blunt: **the device does not execute integer matmuls natively — it emulates them in F32.** The string `" Downcast for int support "` names the intent.

### Algorithm

```c
function InstructionMatchesPattern(HloInstruction* h):              // 0x1fa9f10
    if h->opcode() != kDot:                                         // cmpb $0x2e,0x14(%rsi) @0x1fa9f10
        return false                                                 // 0x2e == 46 == kDot
    return Shape::AreAllLeavesIntegers(h->shape())                  // all-integer dot

function ExpandInstruction(HloInstruction* dot):                   // 0x1faa9b0
    assert all operands are integer-typed
    lhs_f = CreateConvert(ChangeElementType(lhs.shape, F32), lhs)   // int -> F32  (F32 == 0xb)
    rhs_f = CreateConvert(ChangeElementType(rhs.shape, F32), rhs)
    dot_f = CreateDot(ChangeElementType(out.shape, F32), lhs_f, rhs_f,
                      dot_dimension_numbers, precision_config)       // F32 matmul
    out   = CreateConvert(out.shape /* int */, dot_f)               // F32 -> int
    add_auto_cast_none(...)   // 0x1faa1a0 — tags converts with auto_cast / auto_cast_type
                              // so the tensorizer must not re-cast them
```

The matcher at `0x1fa9f10` is `cmpb $0x2e,0x14(%rsi)` — opcode field at offset `0x14` of the `HloInstruction`, compared to `0x2e` (kDot). The expander tags its inserted converts with the `auto_cast`/`auto_cast_type` attributes (both strings present), marking them as compiler-inserted so the downstream tensorizer leaves them alone. Error: `"HloInstruction '%s' is of type '%s' and cannot be downcasted to '%s.'"`.

> **QUIRK —** Path E proves the device has **no native int8 GEMM** on this route. An int8-quantized model that reaches HLO as an integer dot runs in **F32** on the PE array (convert up, F32 dot, convert down), not as int8. The genuinely low-precision device GEMM is the MX-FP8 `ScaledMatmul` (Path D). The int8 numeric cast of the *operands* rides `bir::CastToNewDType` at the tensorizer.

---

## §6. Path C — oneDNN CPU Golden  [STOCK / GOLDEN]

### Purpose

Both names the task line cites — `convert_dynamic_quantize_ops` and `lift_up_quantize` — are **stock Intel oneDNN graph** functions (namespace `dnnl::impl::graph::dnnl_impl::`), bundled as the XLA:CPU INT8 reference backend. They are **not** Neuron device passes and emit no device code.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `convert_dynamic_quantize_ops(subgraph&)` | `0x55586a0` | lower `DynamicQuantize`/`DynamicDequantize` (runtime scale/zp tensors) into dnnl primitives | CERTAIN |
| `lift_up_quantize(subgraph&)` | `0x5548ee0` | hoist `Quantize` ops earlier so a following op consumes pre-quantized data (enables `dequant→op→quant` fusion) | CERTAIN |
| `static_dequant_handler` | `0x553aba0` | fold constant scale/zp | CERTAIN |
| `dynamic_dequant_handler` | `0x553cad0` | read qtype/axis from op-attr hashtable; decompose `Dequantize` into `reorder + mul_scales (+ sub_zps)` | CERTAIN |
| `pattern::register_quantize_fusion` | `0x56df790` | register `dequant→matmul→quant` and quantized-add CPU INT8 fusions | CERTAIN |

The supporting oneDNN op-kind family (`Quantize`, `Dequantize`, `DynamicQuantize`, `DynamicDequantize`, `TypeCast`, `Reorder`, `StaticReshape`) and the execution kernel `quantize_dequantize_t` are all present.

> **GOTCHA —** this is the **golden/CPU reference** INT8 path — used by XLA:CPU to compute reference outputs for numeric verification of the Neuron compile. It never touches the Neuron PE/accelerator. The depth of the oneDNN INT8 matmul scale/zp flow is owned by the calibration sibling (see [4.30](#cross-references)); this section just establishes provenance: the `dnnl::impl::graph::dnnl_impl::` namespace prefix is the tell, and all five functions carry it at the listed addresses.

---

## §7. Device vs Golden — the Ledger

The negative result, stated as a ledger. Of the five paths, only D and E emit Neuron device code, and **neither carries int8 scale/zero-point onto the accelerator**:

| | Path A | Path B | Path C | Path D | Path E |
|---|---|---|---|---|---|
| **Op** | `uniform_quantize`/`dequantize` | `mhlo.dequantize` | oneDNN `Quantize`/`Dequantize` | `QuantizeMX`/`ScaledMatmul` | int `kDot` |
| **Numeric format** | int8 (storage) | int8 (range table) | int8 | **FP8 (e5m2/e4m3fn)** | **F32 (emulated)** |
| **Author** | STOCK MLIR/SHLO | STOCK (tf legacy) | STOCK oneDNN | NEURON | NEURON |
| **Emits device code?** | no | no | no | **yes** | **yes** |
| **int8 reaches device?** | no | no | no | **no (FP8)** | **no (F32)** |

The chain of evidence backing **"no int8 scale/zero-point reaches device code"**:

1. The int8 quantize *arithmetic* (Path A, [§4](#4-the-int8-arithmetic-golden)) is the stock MLIR `quant`-dialect lowering; its on-host reference implementation is the xnnpack `unary_ukernel_quantized<signed char,…>` family — host, not device.
2. The two int8 *graph* backends (Path B legacy, Path C oneDNN) live in `mlir::mhlo::` and `dnnl::impl::graph::dnnl_impl::` — host reference code by namespace.
3. The one Neuron *quantize* pass (Path D) matches a `kCustomCall` named `"QuantizeMX"` — **never** `uniform_quantize` — and its verifier rejects every non-FP8 dtype (`"only 'float8_e5m2' and 'float8_e4m3fn' are supported"`). It is FP8 microscaling.
4. The one Neuron pass that touches an integer dot (Path E) **converts it to F32** and casts back. No int8 GEMM is emitted.

Therefore the int8 `uniform_quantize` op is a frontend / golden-reference construct; the hardware quantized GEMM is MX-FP8; integer dots that survive to the device are F32-emulated; and int8 numeric casts of operands ride `bir::CastToNewDType` at the tensorizer. **No int8 scale/zero-point ever crosses into Neuron device code.**

### Evidence summary

| Claim | Anchor | Confidence |
|---|---|---|
| **No int8 scale/zp reaches device (golden-only)** | namespaces + Path D FP8 verifier + Path E F32 expander | CERTAIN |
| `getQuantizedType` keys + `-1` per-tensor/per-axis branch | disasm `0x75d6889` `cmp $-1` | CERTAIN |
| `DequantizeMode = {0, MIN_COMBINED=1}` | disasm `0x76dece0` | CERTAIN |
| `QuantizeMX` matches `kCustomCall`, FP8 contract | `0x1efc594` + 8 verifier strings | CERTAIN |
| `NeuronIntMatmulDowncast` int-dot → F32 | matcher `0x1fa9f10` + expander `0x1faa9b0` | CERTAIN |
| oneDNN provenance (Path C) | `dnnl::…::` symbols at listed addrs | CERTAIN |
| Exact round-even/clamp formula (§4) | MLIR quant semantics + xnnpack ukernels; not single-stepped | HIGH |
| int8 weights reach device via `bir::CastToNewDType` | no int8-uniform device op exists, so the cast is the only remaining route | MEDIUM |

---

## Key Addresses

| Address | Symbol | Tag |
|---|---|---|
| `0x75d65e0` | `xla::getQuantizedType(mlir::DictionaryAttr&)` | NEURON deserializer |
| `0x75d6889` | per-tensor/per-axis `cmp $-1` branch | NEURON |
| `0x809d370` | `UniformQuantizedType::get` | STOCK |
| `0x809bed0` | `UniformQuantizedPerAxisType::get` | STOCK |
| `0x76dece0` | `mhlo::symbolizeDequantizeMode` (only `MIN_COMBINED`) | STOCK |
| `0x1efc4f0` | `xla::LegalizeQuantizeMX::Run` (matches `kCustomCall` "QuantizeMX") | NEURON / DEVICE |
| `0x1efe1a0` | `xla::LegalizeScaledMatmul::Run` | NEURON / DEVICE |
| `0x1fa9f10` | `NeuronIntMatmulDowncast::InstructionMatchesPattern` (int `kDot`) | NEURON / DEVICE |
| `0x1faa9b0` | `NeuronIntMatmulDowncast::ExpandInstruction` (int dot → F32) | NEURON / DEVICE |
| `0x1faa1a0` | `add_auto_cast_none` (`auto_cast` tag) | NEURON |
| `0x5548ee0` | `dnnl::…::lift_up_quantize` | STOCK oneDNN / GOLDEN |
| `0x55586a0` | `dnnl::…::convert_dynamic_quantize_ops` | STOCK oneDNN / GOLDEN |
| `0x553aba0` | `dnnl::…::static_dequant_handler` | STOCK oneDNN / GOLDEN |
| `0x553cad0` | `dnnl::…::dynamic_dequant_handler` | STOCK oneDNN / GOLDEN |
| `0x56df790` | `dnnl::…::pattern::register_quantize_fusion` | STOCK oneDNN / GOLDEN |

Custom-call targets: `"QuantizeMX"`, `"ScaledMatmul"`. Pass flags: `legalize-quantize-mx`, `neuron-int-matmul-downcast`. QuantizeMX `backend_config` JSON: `{ dtype, dim, block_size, scale_method }`.

---

## Cross-References

- [4.29 — MX-FP8 Microscaling Legalization](mx-fp8-legalization.md) — the real device quantized GEMM (Path D), the U32-packed microscaling consumer
- [4.30 — Calibration and scale/zero-point flow](calibration-scale-flow.md) — `CalibratedQuantizedType`, the oneDNN INT8 matmul scale/zp flow (Path C in depth)
- Part 9 — Numerics — round-to-nearest-even, clamp, FP8/MXFP8 numeric semantics, golden-vs-device numeric verification
- [bir::CastToNewDType](../numerics/cast-to-new-dtype.md) — the low-level numeric int cast that quantize operands ultimately ride on the device
