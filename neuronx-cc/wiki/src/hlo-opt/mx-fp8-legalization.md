# MX-FP8 Microscaling Legalization

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310), binary `neuronxcc/starfish/bin/hlo-opt` (xxHash BuildID `93dd8bd9bd4c697b`). The cp311/cp312 wheels share this layout; offsets inside an inlined arm can drift by a few bytes between wheels. This binary was tagged `NVOPEN_IDA_SKIP_DECOMPILE`, so every claim below is grounded in `objdump`/`strings` on the live ELF plus the IDA sidecar tables, not Hex-Rays output.*

## Abstract

Two HLO legalization passes in `hlo-opt` — `legalize-quantize-mx` (registry order 9, `xla::LegalizeQuantizeMX`) and `legalize-scaled-matmul` (order 10, `xla::LegalizeScaledMatmul`) — are the **only** quantization path in the whole compiler that reaches genuine low-precision Neuron device code. Everything else that *looks* like quantization in `hlo-opt` is golden/CPU-reference plumbing: `mhlo`/`stablehlo.uniform_quantize` is stock upstream MLIR, `mhlo.dequantize`/`MIN_COMBINED` is legacy TF, `convert_dynamic_quantize_ops`/`lift_up_quantize` are Intel oneDNN, and integer dots that survive to HLO are *F32-emulated* by `NeuronIntMatmulDowncast`. The device's actual hardware-quantized GEMM is **OCP Microscaling FP8** (MX-FP8): `float8_e5m2`/`float8_e4m3fn` data in `block_size=32` groups, each group carrying one `float8_e8m0fnu` (E8M0) shared-exponent scale, with the FP8 elements x4-packed into `U32` lane words. See [§7 of the int8 page](int8-quantize-legalization.md) for the device-vs-golden ledger that this page is the device half of.

Both passes are pure match-and-rewrite over the HLO post-order. `LegalizeQuantizeMX` matches a **frontend-emitted `QuantizeMX` `kCustomCall`** — never `uniform_quantize` — and rewrites it into a dtype-tagged target (`QuantizeMX_{f16|bf16}_{e5m2|e4m3fn}`). `LegalizeScaledMatmul` matches the JAX/StableHLO composite-decomposition target **`__op$block_scaled_dot`** (lhs, rhs, lhs_scale, rhs_scale) and rewrites it into `ScaledMatmul_{e4m3fn_e4m3fn|e5m2_e5m2}_{f32|bf16}`. Neither pass *validates* the MX contract — the rich `block_size==32`/dtype/operand-count/tuple-2/rank/batch error catalog lives upstream in the structured-error machinery (`hilo::lookup_cause`/`lookup_resolution`/`formatErrorMessage`) fired at custom-call construction time, not in these `Run` bodies.

The dtype-tagged custom-calls these passes emit are the HLO entry points of the MX path that the downstream layers — NKI `quantize_mx()`/`nc_matmul_mx()`, MLIR `nisa.quantize_mx`/`nisa.matmul_mx`, BIR `InstQuantizeMx`/`InstMatmultMx`, and the CoreV4 wire ops `LdWeightMx`/`MatmultMx`/`QuantizeMx` — trace down to silicon. The whole MX path is **CoreV4-only** (Trainium3 / gen4); the MX memory descriptors (`MXMEM_PATTERN1D`) that carry the x4-packed data + E8M0 scale exist only on `core_v4` targets.

### What a reimplementer must reproduce

- The two post-order matchers: opcode `== kCustomCall` (byte `0x2B`) plus a `std::string::compare` against `"QuantizeMX"` / `"__op$block_scaled_dot"` at `HloInstruction+0x220`.
- The `backend_config` JSON schemas (QuantizeMX: `dtype`/`dim`/`block_size`/`scale_method`; ScaledMatmul: nested under `scaled_dot_backend_config`), field-by-field, with the OCP-MX constraints.
- The negative-axis normalization of `dim` (`dim < 0 → dim + rank`), the default `block_size = 32`, and the metadata-constant build (`PopulateR1<long>` shape list + `CreateR0<long>` scalars → `CreateConstant`).
- The byte-exact emitted target-string construction (inline `movabs` literal heads + dtype-suffix `_M_append` chains) and the four-way / eight-way target enumerations.
- The operand binding: QuantizeMX reuses the original 2-tuple result shape verbatim; ScaledMatmul binds scales **positionally** (operand 2 = lhs_scale, operand 3 = rhs_scale) and appends a dims-metadata constant.
- The crosswalk invariants (block_size=32 = 8 partitions × 4 elems, E8M0 scale, U32 lane = `float8_*_x4`) that must line up bit-for-bit with the BIR/CoreV4 encoders.

### At a glance

| | |
|---|---|
| **Pass 9** | `legalize-quantize-mx` — `xla::LegalizeQuantizeMX` |
| `name()` | `0x1ef5090` → `"legalize-quantize-mx"` (len `0x14`=20) |
| `Run` | `0x1efc4f0` (≈3117 B) |
| vtable / RTTI | `_ZTVN3xla18LegalizeQuantizeMXE` `@0x40e168`; `_ZTIN3xla18LegalizeQuantizeMXE` |
| source | `hilo/hlo_passes/LegalizeQuantizeMX.cc` |
| matches | `kCustomCall` target `== "QuantizeMX"` |
| emits | `QuantizeMX_{f16\|bf16}_{e5m2\|e4m3fn}` |
| **Pass 10** | `legalize-scaled-matmul` — `xla::LegalizeScaledMatmul` |
| `name()` | `0x1efd120` → `"legalize-scaled-matmul"` (len `0x16`=22) |
| `Run` | `0x1efe1a0` (≈7285 B) |
| vtable / RTTI | `_ZTVN3xla20LegalizeScaledMatmulE` `@0x40e248`; `_ZTIN3xla20LegalizeScaledMatmulE` |
| source | `hilo/hlo_passes/LegalizeScaledMatmul.cc` |
| matches | `kCustomCall` target `== "__op$block_scaled_dot"` |
| emits | `ScaledMatmul_{e4m3fn_e4m3fn\|e5m2_e5m2}_{f32\|bf16}` |
| **Namespace** | `xla::` (NOT `xla::hilo`) — `N3xla…E` RTTI, source under `hilo/hlo_passes/` |
| **MX block** | `block_size = 32` (OCP MXFP), E8M0 = `float8_e8m0fnu` per-block scale, FP8 x4-packed → `U32` |
| **Hardware** | CoreV4 / gen4 only (`MXMEM_PATTERN1D`) |

---

## 1. Why this is the only real device quantize

The int8 page ([int8-quantize-legalization.md](int8-quantize-legalization.md)) enumerates four quantize code paths in `hlo-opt` and finds that three of them are golden/CPU-reference: stock `uniform_quantize`/`uniform_dequantize` with `!quant.uniform` element types, the legacy `mhlo.dequantize` `MIN_COMBINED` mode, and the bundled Intel oneDNN graph (`Quantize`/`Dequantize`/`DynamicQuantize` + `convert_dynamic_quantize_ops`/`lift_up_quantize`). A fifth path, `NeuronIntMatmulDowncast`, is a device pass but proves the *opposite* of an int8 GEMM: it converts an integer `kDot` to F32, does the matmul in F32, and converts the result back — the PE array has no native integer matmul on that route.

The MX-FP8 pair documented here is **PATH D**: the genuinely-low-precision device GEMM. The contrast is sharp and worth stating plainly because it is a common misreading of the binary:

> **GOTCHA —** the Neuron device quantize is not int8-uniform. `uniform_quantize` is a frontend/golden construct that on the device only rides `bir::CastToNewDType` for the numeric cast; there is no dedicated int8 device quantize op. The hardware quantized matmul is MX-FP8, and `QuantizeMX` is matched as a frontend-emitted `kCustomCall`, never derived from `uniform_quantize`. Looking for "the int8 device path" turns up F32 emulation, not a GEMM.

> **NOTE —** "MX" here is OCP Microscaling, not a Neuron coinage. `block_size=32` is the OCP-MXFP group size, the E8M0 shared exponent is `float8_e8m0fnu`, and the supported element formats are exactly the OCP set `float8_e5m2` / `float8_e4m3fn`. The binary even cites the spec URL in an error-resolution string (`https://www.opencompute.org/documents/ocp-microscaling-formats-mx-v1-0-spec-final-pdf`).

---

## 2. `LegalizeQuantizeMX::Run` @ `0x1efc4f0`

### 2.1 Match

Walk the entry computation in post-order (`HloComputation::MakeInstructionPostOrder` `@0x1efc555`). For each instruction, two gates:

```c
// 0x1efc594:  cmpb $0x2b, 0x14(%rax)      ; opcode byte == kCustomCall (0x2B = '+')
if (inst->opcode() != kCustomCall) continue;
// 0x1efc5a1:  lea  0x220(%rax), %rdi      ; &inst->custom_call_target  (std::string @+0x220)
if (inst->custom_call_target().compare("QuantizeMX") != 0) continue;
```

The opcode test is byte-exact in the disasm (`80 78 14 2b` at `0x1efc594`), and the target string lives at `HloInstruction+0x220` (`lea 0x220(%rax),%rdi` at `0x1efc5a1`, feeding `std::string::compare`). These two gates are what make the device quantize a *separate, frontend-authored* MX custom-call, disjoint from stock `uniform_quantize`.

### 2.2 Read the `backend_config`

The raw config string is fetched under a mutex (`BackendConfigWrapper::GetRawStringWithoutMutex`, Lock/Unlock `@0x1efc5ef`/`0x1efc602`) and parsed by `nlohmann::json::parse` (`@0x1efc61c`, abi v3.11.3). Three typed reads via `nlohmann::…::value<T>`:

| key | type | default | use |
|---|---|---|---|
| `"dtype"` | `std::string` | `""` | output FP8 element dtype of the quantized data |
| `"dim"` | `int` | `0` | the microscaling (grouping) axis → `r14` |
| `"block_size"` | `int` | `0x20`=**32** | elements per MX group |

The `block_size` default is preloaded `mov dword[rbp-0x880], 0x20` at `0x1efc688` **before** the `value<>` read — so an absent `block_size` defaults to 32, the OCP-MXFP block. `scale_method` is a *required* field per the error catalog but is never branched on in `Run` — `EMAX` is the only allowed value, so it is invariant; see §6.

### 2.3 Normalize `dim`, build the metadata constant

`dim` gets the standard negative-index fold against the operand-0 rank:

```c
long rank = inst->operand(0)->shape().rank();   // rank = (array_state >> 1)
// 0x1efc6df:  cmovs r14, [r14 + rank]          ; dim < 0  ->  dim + rank
if ((long)dim < 0) dim += rank;                 // -1 -> last, -2 -> second-to-last
```

This matches the validator string *"Only last dim (%d or -1) or second-to-last dim (%d or -2) are supported."* The pass then reads the operand-0 shape dims into a `std::vector<long>` and materializes a small **metadata tuple-literal**:

```c
// 1-D S64 literal of the dim list:
Shape s = ShapeUtil::MakeValidatedShape(/*PrimitiveType=*/S64 (5), dims_span);  // 0x1efc7f8
Literal shape_lit = ...; shape_lit.PopulateR1<long>(dims_span);                 // 0x1efc86c
// scalar S64 literals for dim, block_size, and a 0:
Literal a = LiteralUtil::CreateR0<long>(dim);          // 0x1efc6ea / 0x1efc87b
Literal b = LiteralUtil::CreateR0<long>(block_size);   // 0x1efc88e
Literal z = LiteralUtil::CreateR0<long>(0);            // 0x1efc89c
HloInstruction* meta = comp->AddInstruction(           // 0x1efca7c
    HloInstruction::CreateConstant(tuple{shape_lit, a, b, z}));  // 0x1efca41
```

The `{shape, dim, block_size}` metadata rides into the new op as a **constant operand** rather than being re-serialized into a `backend_config` string (the output config string passed to `CreateCustomCall` is `""` for QuantizeMX). The literal-build call sites are direct; the exact tuple-slot ordering is only HIGH confidence, read from parse/create order rather than traced into the constant's shape.

### 2.4 Build the new target string

The emitted target is assembled from an inline literal head plus two dtype-driven `_M_append` chains:

```c
// head: "QuantizeMX_" (len 0xB = 11)
// 0x1efcbd4:  movabs $0x657a69746e617551, %rax   ; "Quantize"  (LE bytes)
// 0x1efcbcf:  mov    $0x584d, %ecx               ; "MX"
//             + '_'  (0x5F)
std::string tgt = "QuantizeMX_";

// input-dtype prefix from operand(0) element type [rbp-0x948]:
// 0x1efcbde:  cmpl $0x10, -0x948(%rbp)           ; 0x10 = 16 = BF16
if (operand0_etype == BF16) tgt += "bf16_";       // 0x1efcf85
else                        tgt += "f16_";         // 0x1efcc1e  (fall-through = F16)

// scale/output-dtype suffix from backend_config "dtype":
if (cfg_dtype.compare("float8_e5m2") == 0) tgt += "e5m2";    // 0x1efcc2f / 0x1efcc68
else                                       tgt += "e4m3fn";  // 0x1efcf46
```

→ emitted target ∈ { `QuantizeMX_f16_e5m2`, `QuantizeMX_f16_e4m3fn`, `QuantizeMX_bf16_e5m2`, `QuantizeMX_bf16_e4m3fn` }. The literal-head bytes are decoded byte-exact from the disasm (`movabs $0x657a69746e617551` at `0x1efcbd4`, `mov $0x584d` at `0x1efcbcf`).

> **GOTCHA —** BF16 (`0x10`=16) is the *only* explicitly-tested input element type; F16 is the fall-through. The validator string *"only BF16 and F16 are supported"* is the upstream guard — by the time `Run` sees the op, anything else has already been rejected, so the two-way branch is safe.

### 2.5 Emit and replace

```c
HloInstruction* nw = comp->AddInstruction(                    // 0x1efcd5d
    HloInstruction::CreateCustomCall(
        inst->shape(),                       // reused VERBATIM — already the 2-tuple
        Span{meta, orig_operand...},          // metadata constant + original input
        tgt, /*opaque=*/"", /*api_version=*/CustomCallApiVersion::API_VERSION_ORIGINAL));  // push 1, 0x1efcd0c
TF_CHECK_OK(comp->ReplaceInstruction(inst, nw));              // 0x1efcdb2
```

`inst->shape()` is reused without modification because the original `QuantizeMX` already carries the **2-tuple result {quantized_data (x4-packed FP8), scale (E8M0)}** that the validator demands (*"must return a tuple with exactly 2 outputs (quantized data and scale)"*). `api_version=1` is `API_VERSION_ORIGINAL` (`push 1` at the `CreateCustomCall` site).

---

## 3. `LegalizeScaledMatmul::Run` @ `0x1efe1a0`

### 3.1 Match

```c
// 0x1efe256:  cmpb $0x2b, 0x14(%r13)             ; kCustomCall
if (inst->opcode() != kCustomCall) continue;
// 0x1efe25d:  lea  0x220(%r13), %rdi             ; &custom_call_target
if (inst->custom_call_target().compare("__op$block_scaled_dot") != 0) continue;
```

> **GOTCHA —** the matched *input* target is `__op$block_scaled_dot` — the JAX/StableHLO block-scaled-dot composite-decomposition target — not the literal `"ScaledMatmul"`, which appears only as the *output* target prefix and inside error strings. The pass's flag name invites the wrong reading; the compare site is `0x1efe264`.

### 3.2 Read 4 operands and the nested config

The pass reads `operand(0..3)` and each `->shape()` (`0x1efe287…0x1efe2db`): **lhs, rhs, lhs_scale, rhs_scale** — the 4-operand contract (*"must have exactly 4 operands (lhs, rhs, lhs_scale, rhs_scale)"*). The lhs/rhs ranks are stashed (`shr rax,1`) for dim bookkeeping.

The `backend_config` is fetched mutex-guarded and `json::parse`d (`@0x1eff0dc`), then descended one level into the **`"scaled_dot_backend_config"`** sub-object (a default-`"None"` guard at `0x1efe3f2` routes the no-config case into the sub-object parse at `loc_1EFF098`). Five sub-keys:

| sub-key | type | meaning | sites |
|---|---|---|---|
| `"lhs_batch_dimensions"` | `int[]` | LHS batch dims | `0x1eff522` / `0x1effba7` |
| `"rhs_batch_dimensions"` | `int[]` | RHS batch dims | `0x1eff5b5` / `0x1effaaf` |
| `"lhs_contracting_dimensions"` | `int[]` | LHS contract dims | `0x1eff648` / `0x1eff8bf` |
| `"rhs_contracting_dimensions"` | `int[]` | RHS contract dims | `0x1eff6db` / `0x1eff9b7` |
| `"element_dtype"` | `std::string` | the FP8 *input* dtype | `0x1eff772` / `0x1eff7b4` |

The four int-array dim lists are the dot dimension numbers; they are packed into a metadata constant (S64 `CreateR0<long>` ×7 at `0x1efe712…0x1efe77e`, `CreateConstant` `@0x1efea50`, `AddInstruction` `@0x1efea8b`) appended as an operand to the rewritten op. The key reads and literal builds are direct; the mapping of the 7 scalars to batch/contract lists is only HIGH confidence, read from parse order rather than traced into the constant's shape.

### 3.3 Build the new target string

```c
// head: "ScaledMatmul_" (len 0xD = 13)
// 0x1efebd8:  movabs $0x614d64656c616353, %rax   ; "ScaledMa"
// 0x1efebee:  movl   $0x6c756d74, -0xb28(%rbp)   ; "tmul"
//             + '_'  (0x5F)
std::string tgt = "ScaledMatmul_";

// input dtype (doubled, lhs==rhs) from "element_dtype":
if (element_dtype.compare("float8_e4m3fn") == 0) tgt += "e4m3fn_e4m3fn_";  // 0x1efec53
else                                             tgt += "e5m2_e5m2_";      // 0x1efef66

// output dtype suffix from the result element type [rbp-0xCA0]:
// 0x1efec64:  cmpl $0xb,  -0xca0(%rbp)           ; 0xB = 11 = F32
// 0x1efec78:  cmpl $0x10, -0xca0(%rbp)           ; 0x10 = 16 = BF16
if (out_etype == F32)  tgt += "f32";   // F32 arm
else /* BF16 */        tgt += "bf16";  // BF16 arm
```

→ emitted target ∈ { `ScaledMatmul_e4m3fn_e4m3fn_f32`, `ScaledMatmul_e4m3fn_e4m3fn_bf16`, `ScaledMatmul_e5m2_e5m2_f32`, `ScaledMatmul_e5m2_e5m2_bf16` }. The literal head `movabs $0x614d64656c616353` ("ScaledMa") + `movl $0x6c756d74` ("tmul") and the length `0xD`=13 are byte-exact in the disasm at `0x1efebd8`/`0x1efebee`/`0x1efec14`.

> **QUIRK —** The output-dtype branch tests both `0xB` (F32=11) and `0x10` (BF16=16) explicitly in the cp310 wheel (`0x1efec64`/`0x1efec78`); a prior trace cited a single `cmp $0xB` at `0x1efef77`, which is the equivalent test inside a different inlined arm. The *result* enumeration ({F32→`f32`, BF16→`bf16`}, matching *"only F32 and BF16 are supported"*) is identical either way. The F32 numeric `0xB`=11 and BF16 `0x10`=16 are the same `PrimitiveType` codes used by QuantizeMX's input check, internally consistent.

### 3.4 Emit and replace

```c
HloInstruction* nw = comp->AddInstruction(                    // 0x1efed5e
    HloInstruction::CreateCustomCall(
        inst->shape(),                                        // single F32/BF16 dot output, verbatim
        Span{lhs, rhs, lhs_scale, rhs_scale, dims_meta},      // scales bound POSITIONALLY
        tgt, /*opaque=*/backend_cfg_str, /*api_version=*/1)); // push 1, 0x1efed0d
TF_CHECK_OK(comp->ReplaceInstruction(inst, nw));              // 0x1efedb1
```

The scale operands are bound **by position**: operand index 2 = `lhs_scale`, index 3 = `rhs_scale` — i.e. the U32 packed-MX LHS data plus its E8M0 scale, and the RHS packed data plus its E8M0 scale. The result shape (the single F32/BF16 dot output) is reused verbatim.

---

## 4. The MX `backend_config` schemas

### 4.1 `QuantizeMX` (flat JSON)

Required fields, verbatim from the resolution string *"Ensure the backend_config is valid JSON with required fields: dtype, dim, block_size, scale_method."*

| field | type | semantics | constraint (validator strings) |
|---|---|---|---|
| `dtype` | string | output FP8 element dtype of the quantized data | only `float8_e5m2` / `float8_e4m3fn` |
| `dim` | int | the microscaling axis (group along this dim) | only last (`-1`) or second-to-last (`-2`) |
| `block_size` | int | elements per MX group | **must be 32** ("Use block_size=32 per OCP MXFP standard") |
| `scale_method` | string | how the per-block scale is computed | only `EMAX` ("Use scale_method='EMAX'.") |

- **Input element type:** only **BF16** (`0x10`) and **F16**. (*"only BF16 and F16 are supported"*.)
- **Output:** a **2-tuple {quantized_data, scale}**. The data is the x4-packed FP8 tensor (the free dim along packing *"must be divisible by 4 for x4 packing"*); the scale is **E8M0** = `float8_e8m0fnu` (also `builtin.f8E8M0FNU` / `tsl::float8_e8m0fnu`, all present as strings in the binary). One E8M0 scale per 32-element block.

### 4.2 `ScaledMatmul` (nested JSON)

Top-level key **`scaled_dot_backend_config`** holding the dot dimension numbers:

| sub-field | type | semantics |
|---|---|---|
| `lhs_batch_dimensions` | int[] | LHS batch dims |
| `rhs_batch_dimensions` | int[] | RHS batch dims |
| `lhs_contracting_dimensions` | int[] | LHS contraction dims |
| `rhs_contracting_dimensions` | int[] | RHS contraction dims |
| `element_dtype` | string | FP8 input dtype (`float8_e4m3fn` / `float8_e5m2`) |

- **Operand contract:** exactly 4 — `lhs, rhs, lhs_scale, rhs_scale`.
- **LHS input type:** must be **`U32`** — *"only U32 (packed MX format from QuantizeMX) is supported"* — i.e. the 4×FP8-packed-into-`u32` lane container produced by `QuantizeMX`. LHS/RHS rank ≥ 2.
- **Output:** **F32 or BF16** only.
- **Cross-operand validators** (upstream, error-catalog): batch-dim count match, contract dim/size match (*"same contract dim and size for lhs and rhs"*), contract∩batch non-overlap, batch dim in-bounds.

### 4.3 The pipeline coupling

`QuantizeMX` **produces** the U32-packed MX data + E8M0 scale; `ScaledMatmul` **consumes** them. The canonical HLO MX-GEMM graph is therefore:

```text
QuantizeMX(BF16/F16 act)  ->  {u32 data, e8m0 scale}     (x2: one for lhs, one for rhs)
                                       │
                                       ▼
   __op$block_scaled_dot(lhs_u32, rhs_u32, lhs_scale, rhs_scale)
```

which order-9 then order-10 legalize into the dtype-tagged `QuantizeMX_*` and `ScaledMatmul_*` custom-calls. Because `QuantizeMX → ScaledMatmul` fuses naturally by dataflow, no separate quantize-hoist is needed on the device side (the `lift_up_quantize` hoist is the unrelated oneDNN/CPU one) — the operand types already line up by construction.

---

## 5. Crosswalk to BIR `InstQuantizeMx` / `InstMatmultMx`

The dtype-tagged custom-calls are the HLO heads of the MX path that the tensorizer carries through NKI → MLIR `nisa` → BIR → CoreV4 wire. The invariants line up exactly across layers — the same 32-element block, the same E8M0 scale, the same x4 lane packing:

| HLO (this page) | NKI | MLIR `nisa` | BIR | CoreV4 wire |
|---|---|---|---|---|
| `QuantizeMX_*`; `block_size=32`, `scale_method=EMAX`, out `float8_e5m2/e4m3fn`; 2-tuple {u32 data, e8m0 scale} | `quantize_mx()`; src 32-multiple par, free 4-multiple | `nisa.quantize_mx` (src_ap, dst_ap, dst_scale_ap) | `InstQuantizeMx` (BIR class id **96**) | `QuantizeMx` opcode `0x10E3` (`visitInstQuantizeMx @0x143dc60`, DVE engine), `…_S3DMX1_QUANT_STRUCT` |
| `ScaledMatmul_*`; LHS = `U32` packed-MX; 4 ops (lhs, rhs, lhs_scale, rhs_scale); out F32/BF16 | `nc_matmul_mx()`; K = partitionDim×4; par 32-multiple ≤128 | `nisa.matmul_mx` (stationary/moving + their scale aps, dst) | `InstMatmultMx` (BIR class id **95**) | 2-op bundle `LdWeightMx` `0x1009` + `MatmultMx` `0x100A` (`visitInstMatmultMx @0x143f410` → `generateLdweightMx @0x143e350` then `generateMatmultMx @0x143ebd0`) |

The MX-group geometry is a single source of truth across all layers: **32 elements = 8 partitions × 4 elems/partition**, one **E8M0 (uint8)** scale per 32-element group. The HLO `block_size=32` *is* the OCP-MXFP block, the E8M0 scale *is* the `float8_e8m0fnu` shared exponent, and "x4 packing" / "U32 LHS" *is* the `float8_*_x4` lane container the PE engine unpacks (×4 → effective contraction K). On the wire the data + E8M0 scale ride a `MXMEM_PATTERN1D` descriptor at `bundle[+0x10]`, with the PSUM output as a 3D pattern at `+0x30`. See [`isa/pe-matmul-encoding.md`](../isa/pe-matmul-encoding.md) for the byte-level `LdWeightMx`/`MatmultMx`/`QuantizeMx` bundle layout and [`isa/mxmem-pattern1d.md`](../isa/mxmem-pattern1d.md) for the descriptor.

> **NOTE —** The CoreV4 header word is the little-endian `(0x10<<8) | opcode`, so the `QuantizeMx` op shows as `0x10E3` at the bundle head while the low opcode byte is `0xE3`=227. The MX descriptors (`MXMEM_PATTERN1D`, `assignAccessForMX @0x150e2f0`) are gen4-only — the entire MX path activates **only** on `core_v4` targets (Trainium3 / Mariana). On older cores there is no hardware MX, and these passes simply find no `QuantizeMX`/`__op$block_scaled_dot` custom-calls to rewrite.

---

## 6. Reconstructed signatures

```cpp
// xla:: (NOT xla::hilo) — hilo/hlo_passes/{LegalizeQuantizeMX,LegalizeScaledMatmul}.cc
class LegalizeQuantizeMX : public HloPassInterface {              // _ZTV @0x40e168
  absl::string_view name() const override;                       // 0x1ef5090 -> "legalize-quantize-mx"
  StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>&) override;  // 0x1efc4f0
};
class LegalizeScaledMatmul : public HloPassInterface {            // _ZTV @0x40e248
  absl::string_view name() const override;                       // 0x1efd120 -> "legalize-scaled-matmul"
  StatusOr<bool> Run(HloModule*, const flat_hash_set<string_view>&) override;  // 0x1efe1a0
};

// QuantizeMX backend_config (flat):
struct QuantizeMXConfig {
  std::string dtype;           // float8_e5m2 | float8_e4m3fn
  int         dim          = 0;   // last (-1) | second-to-last (-2)
  int         block_size   = 32;  // OCP-MXFP block, default 32
  std::string scale_method;    // "EMAX" only (read by validator, not branched in Run)
};

// ScaledMatmul backend_config (nested under "scaled_dot_backend_config"):
struct ScaledDotConfig {
  std::vector<long> lhs_batch_dimensions, rhs_batch_dimensions,
                    lhs_contracting_dimensions, rhs_contracting_dimensions;
  std::string element_dtype;   // float8_e4m3fn | float8_e5m2
};

// Emitted custom-call targets (verbatim):
//   QuantizeMX_{f16|bf16}_{e5m2|e4m3fn}
//   ScaledMatmul_{e4m3fn_e4m3fn|e5m2_e5m2}_{f32|bf16}

// Shared structured-error catalog (validators fire UPSTREAM, not in Run):
std::string hilo::lookup_cause(hilo::ErrorCode);                 // body @0x759d7d0
std::string hilo::lookup_resolution(hilo::ErrorCode);
std::string hilo::formatErrorMessage<…>(hilo::ErrorCode, …);
```

> **GOTCHA —** The MX validation errors (`block_size==32`, dtype support, operand count, tuple-2, rank≥2, batch/contract consistency, x4-divisibility) are **not** emitted from either `Run` body. They are `hilo::formatErrorMessage(ErrorCode, …)` calls fired at custom-call *construction* time (frontend / HLOToTensorizer), upstream of these passes. The error strings are xref'd only from `lookup_cause`/`lookup_resolution`, never from the `Run` code. A reimplementer who only ports these two passes will silently accept a malformed `QuantizeMX` op — the contract enforcement lives elsewhere.

---

## 7. Evidence anchors and limits

The five structural claims on this page and the bytes behind each:

1. **`QuantizeMX::Run @0x1efc4f0` matches `kCustomCall` + `"QuantizeMX"` at `+0x220`.** `cmpb $0x2b,0x14(%rax)` at `0x1efc594` and `lea 0x220(%rax),%rdi` at `0x1efc5a1` feed the compare; symbol `_ZN3xla18LegalizeQuantizeMX3RunE…` starts at `0x1efc4f0`.

2. **Emitted heads are byte-exact `"QuantizeMX_"` and `"ScaledMatmul_"`.** `movabs $0x657a69746e617551`("Quantize") + `mov $0x584d`("MX") at `0x1efcbd4`/`0x1efcbcf`; `movabs $0x614d64656c616353`("ScaledMa") + `movl $0x6c756d74`("tmul") at `0x1efebd8`/`0x1efebee`; lengths `0xB`/`0xD` materialized at `0x1efcc01`/`0x1efec14`.

3. **The MX contract is FP8 microscaling, not int8.** The string table carries *"only 'float8_e5m2' and 'float8_e4m3fn' are supported"*, *"block_size must be 32"*, the OCP spec URL, *"divisible by 4 for x4 packing"*, *"tuple with exactly 2 outputs"*, and the E8M0 type `float8_e8m0fnu`/`builtin.f8E8M0FNU`. No int8 device-quantize op exists.

4. **ScaledMatmul matches `__op$block_scaled_dot`, not `"ScaledMatmul"`.** The literal `__op$block_scaled_dot` is in the string table; the compare site is `0x1efe264` with `lea 0x220(%r13)` at `0x1efe25d`. `"ScaledMatmul"` is the *output* prefix only.

5. **Crosswalk: HLO `QuantizeMX_*`/`ScaledMatmul_*` → BIR `InstQuantizeMx`(95-class wire `0x10E3`)/`InstMatmultMx`(`0x1009`+`0x100A`).** The opcodes and CoreV4 generator addresses come from `isa/pe-matmul-encoding.md`: `LdWeightMx 0x1009`, `MatmultMx 0x100A`, `QuantizeMx 0x10E3`. The BIR *class ids* 95/96 come from the BIR-class enumeration rather than this ELF, so the numeric ids are **[INFERRED]** here.

**Limits.**

- **Metadata-constant tuple-slot ordering** (which `CreateR0` maps to which dim list / scalar) — HIGH, read from parse/create order rather than traced into the constant shape.
- **BIR class ids 95/96** — **[INFERRED]**; this ELF pins the symbols and opcodes, not the numeric class id.
- **`scale_method` re-emission** — the QuantizeMX output `backend_config` is `""`; whether `scale_method` survives anywhere is MEDIUM. It is invariant (`EMAX` only), so it does not affect the rewrite.
- **Output-type cmp site for ScaledMatmul** — the branch *structure* is certain (`cmpl $0xb`/`$0x10`), but the precise offset shifts a few bytes between inlined arms and wheels.

---

## See also

- [§7 device-vs-golden ledger — int8 quantize legalization](int8-quantize-legalization.md) — the **golden-only** int8 path this page contrasts with (PATHS A/B/C are stock; PATH E is F32 emulation).
- [`isa/pe-matmul-encoding.md`](../isa/pe-matmul-encoding.md) — the byte-level CoreV4 `LdWeightMx`/`MatmultMx`/`QuantizeMx` MX bundle encoding.
- [`isa/mxmem-pattern1d.md`](../isa/mxmem-pattern1d.md) — the `MXMEM_PATTERN1D` descriptor that carries the x4-packed data + E8M0 scale.
- [`hlo-opt/pass-registry.md`](pass-registry.md) — where passes 9 and 10 sit in the `--passes` table.
