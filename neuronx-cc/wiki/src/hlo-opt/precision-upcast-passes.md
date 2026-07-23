# Precision & Upcast Passes

> *All addresses on this page apply to `hlo-opt` from neuronx_cc 2.24.5133.0+58f8de22 (cp310). Other builds will differ. File-offset caveat for raw reads: `.rodata` file-offset = VA − 0x200000; the IDA JSON sidecars give VA directly; the local `_rodata.bin` dump starts at VA 0x20c940 (subtract that to index it).*

## Abstract

Three HLO-level passes in `hlo-opt` change element types rather than topology, all registered by the Neuron hilo pass registry and all built from the same two primitives — `xla::ShapeUtil::ChangeElementType(Shape, PrimitiveType)` and `HloInstruction::CreateConvert`. They share a single magic number: the F32 `PrimitiveType` code **`0x0B`**, passed as the `edx` immediate to every `ChangeElementType` call in all three bodies. Beyond that they are unrelated — they sit at different pipeline positions, key off different opcodes, and exist for different reasons. This page documents `NeuronIntMatmulDowncast`, `BatchNormTrainingUpcast`, and `UpcastAllToFP32`.

The reference frame is stock XLA. A reader who knows OpenXLA's `OpExpanderPass`/`HloModulePass` split, the `xla::match` pattern combinators, and the `PrimitiveType` enum will recognise every callee here; the Neuron-specific content is *which* dtype each pass targets, *why*, and one counter-intuitive name. The headline trap is the word "downcast": `NeuronIntMatmulDowncast` does not reduce precision — it **up-casts** an integer `dot` to F32 and runs it through the float datapath, then re-narrows the result. The PE array has no native integer matmul, so "downcast" means *route the integer op down onto the float PE path*, not *shrink the bit width*.

The two "upcast" passes are also not what their generic names suggest. `BatchNormTrainingUpcast` is narrow and surgical — it touches only `batch-norm-training` ops whose operands are BF16, widens the statistics computation to F32, then narrows the three tuple results back to BF16 so the external type signature is unchanged. `UpcastAllToFP32` is a blanket legalization that promotes **every floating-point element type in the module** (F16, F64, BF16, and the whole FP8/FP4 block) to F32 — but explicitly **leaves true integers untouched**, which corrects the intuition that a pass named "upcast all" would also lift integers into floats.

For reimplementation, the contract is:

- The three gates: `kDot` with an integer output; `kBatchNormTraining` with a BF16 operand; every value/constant shape whose element type is a non-F32 float.
- The rewrite each pass emits, as an explicit `CreateConvert` / `ChangeElementType` sequence, including the F32 round-trip.
- The dtype-selection policy of `UpcastShapeToFP32`, including the 15-entry FP8/FP4 selector table and the 0x20/0x21 special case.
- The `auto_cast=none` frontend-attribute lock that `NeuronIntMatmulDowncast` stamps to stop downstream re-casting.

| | |
|---|---|
| **Pass 1** | `xla::hilo::NeuronIntMatmulDowncast` — `OpExpander`, key `neuron-int-matmul-downcast` |
| **Pass 2** | `xla::BatchNormTrainingUpcast` — `HloModulePass`, key `batch-norm-training-upcast` |
| **Pass 3** | `xla::UpcastAllToFP32` — `HloModulePass`, key `upcast-all-to-fp32` |
| **Shared primitive** | `ShapeUtil::ChangeElementType(shape, 0x0B)` + `HloInstruction::CreateConvert` |
| **F32 PrimitiveType code** | `0x0B` (BF16 = `0x10`, F16 = `0x0A`, F64 = `0x0C`, TUPLE = `0x0D`) |
| **IR level** | HLO (post-ingestion, pre-Penguin), module-wide single sweep each |

> **GOTCHA — "downcast" in `NeuronIntMatmulDowncast` is about routing, not precision.**
> The pass up-casts integer operands to **F32** (`ChangeElementType(…, 0x0B)`), computes
> the dot in F32, and converts the result back to the original integer type. Nothing is
> ever narrowed to a smaller integer.

---

## NeuronIntMatmulDowncast — integer `kDot` through the F32 PE datapath

### Purpose

The Tonga PE array (the systolic matmul engine) has no native integer-matmul mode; only the floating-point datapath exists. An HLO `dot` whose result type is an integer therefore cannot be lowered directly. This `OpExpanderPass` rewrites such a dot into `convert(int→F32) · convert(int→F32)` feeding an F32 `dot`, then `convert(F32→int)` on the result. The integer matmul is *emulated* on the float hardware. This is the same F32-emulation strategy used for int8 quantize lowering ([§4.28](int8-quantize-legalization.md)); the two passes are siblings in intent.

### Entry Point

```text
NeuronIntMatmulDowncast (OpExpanderPass, 8-byte stateless object)
  ├─ InstructionMatchesPattern  0x1fa9f10 (37 B)   ── gate: kDot + integer output
  └─ ExpandInstruction          0x1faa9b0 (1010 B) ── the int→F32→int rewrite
        └─ add_auto_cast_none   0x1faa1a0 (1900 B) ── stamp auto_cast=none on each new op
  factory: _M_invoke            0x1e71670 (73 B)   ── operator new(0x10) + inline-vtable ctor
```

### Gate — `InstructionMatchesPattern` @ 0x1fa9f10 (CERTAIN)

The matcher is 37 bytes and reads the opcode field at `HloInstruction+0x14`, then tail-calls the output-shape integer test:

```c
bool InstructionMatchesPattern(HloInstruction *inst):   // sub_1FA9F10
    if inst->opcode() != kDot:                          // cmp byte [rsi+0x14], 0x2E ; 0x2E == kDot
        return false                                    // xor eax,eax; ret
    return inst->shape().AreAllLeavesIntegers()         // tail-call sub_97D09E0 @ 0x1fa9f30
```

The opcode immediate `0x2E` is **case 46 in `xla::HloOpcodeString`** = `"dot"` (CERTAIN — read from the binary's opcode-name switch table). The gate is therefore: **`kDot` whose output shape is all-integer**. No `cl::opt` flag, no backend-config, no cost model — a stateless trigger on op shape alone.

> **NOTE —** `AreAllLeavesIntegers` is called on the *output* shape, not the operands. The expander re-checks each operand's shape independently (next section), so a dot with one integer and one float operand still routes the integer side through a convert.

### Algorithm — `ExpandInstruction` @ 0x1faa9b0 (CERTAIN)

```c
HloInstruction *ExpandInstruction(HloInstruction *dot):     // sub_1FAA9B0
    op0 = dot->mutable_operand(0)                            // 0x1faa9e2
    op1 = dot->mutable_operand(1)                            // 0x1faa9f2

    // --- operand 0: int → F32 (only if integer) ---
    if op0->shape().AreAllLeavesIntegers():                 // 0x1faaa09
        s0   = ShapeUtil::ChangeElementType(op0->shape(), F32=0x0B)   // 0x1faaa2c edx=0Bh ; 0x1faaa37
        cvt0 = AddInstruction(CreateConvert(s0, op0), "")    // 0x1faaa45
    else:
        cvt0 = op0

    // --- operand 1: symmetric ---
    if op1->shape().AreAllLeavesIntegers():                 // 0x1faaaad
        s1   = ShapeUtil::ChangeElementType(op1->shape(), F32=0x0B)   // 0x1faaad0 edx=0Bh ; 0x1faaadb
        cvt1 = AddInstruction(CreateConvert(s1, op1), "")    // 0x1faaae9
    else:
        cvt1 = op1

    // --- the dot itself, computed in F32 ---
    sD     = ShapeUtil::ChangeElementType(dot->shape(), F32=0x0B)     // 0x1faab90 edx=0Bh ; 0x1faab98
    newdot = AddInstruction(CreateDot(sD, cvt0, cvt1,       // 0x1faabcc
                  dot->dot_dimension_numbers(),             // preserved verbatim
                  dot->precision_config(),                  // preserved verbatim
                  /*sparsity=*/{}, /*operands span=*/{}), "")

    // --- narrow the F32 result back to the original integer type ---
    result = AddInstruction(CreateConvert(dot->shape(), newdot), "")  // 0x1faac8c (orig int shape)

    // --- lock all four new ops against downstream re-casting ---
    add_auto_cast_none(cvt0); add_auto_cast_none(cvt1)      // 0x1faacda / 0x1faace6
    add_auto_cast_none(newdot); add_auto_cast_none(result)  // 0x1faacf2 / 0x1faacfa
    return result                                           // replaces the original dot
```

The `CreateDot` callee carries the full stock-XLA signature — `(Shape, lhs, rhs, DotDimensionNumbers, PrecisionConfig, vector<SparsityDescriptor>, Span<HloInstruction* const>)` — with an empty sparsity vector and empty operand span. The dimension numbers and precision config are passed through unchanged, so the contraction structure is identical; only the element type moves to F32.

### `add_auto_cast_none` — the cast lock @ 0x1faa1a0 (CERTAIN)

The sole caller is `ExpandInstruction`. For each freshly-created op it:

1. Copies the original dot's `OpMetadata` into the new op (`OpMetadata::OpMetadata(const&)` @ 0x1faa1db, `OpMetadata::CopyFrom` @ 0x1faa28c).
2. Appends the literal **` Downcast for int support `** (rodata @ 0x224b86) to that metadata.
3. Inserts the frontend-attribute map entry **`"auto_cast"` → `"none"`** via the protobuf string map: key `kAuto_cast` ("auto_cast", rodata @ 0x22c785) through `google::protobuf::Map<string,string>::InnerMap::TryEmplaceInternal<char const(&)[10]>` @ 0x1faa2db.

```c
void add_auto_cast_none(HloInstruction *op):                // sub_1FAA1A0
    md = OpMetadata(orig_dot->metadata())                   // 0x1faa1be / 0x1faa1db
    md.append(" Downcast for int support ")                 // aDowncastForIntSupport @0x224b86
    op->metadata().CopyFrom(md)                             // 0x1faa28c
    op->frontend_attributes()["auto_cast"] = "none"         // kAuto_cast @0x22c785 ; value "none"
```

> **QUIRK —** the `auto_cast=none` attribute is a *policy* lock, not a numeric guard. The downstream Tensorizer auto-cast machinery (see the CC-op legalize family, [§4.x](ccops-decompose-legalize.md)) keys off this frontend attribute; `none` pins these F32 ops so they are not re-promoted or re-demoted, which would otherwise undo the int→F32 routing the pass just performed.

### Saturation / range handling (CERTAIN — absence verified)

**None explicit.** The 50-basic-block body contains only `CreateConvert` and `ChangeElementType` callees — there are no `Clamp`, `Min`, `Max`, or rounding helpers. Range correctness is delegated entirely to stock XLA `kConvert` semantics: int→F32 is exact for any value inside the 24-bit mantissa and the matmul accumulates in F32; F32→int truncates per XLA's convert rule. The pass implicitly assumes the integer product fits the F32 mantissa.

> **GOTCHA —** there is no overflow guard. A reimplementation that needs bit-exact integer matmul for products exceeding 2²⁴ cannot rely on this emulation path; the F32 round-trip will silently lose low bits. The pass is correct only under the assumption that the integer result is mantissa-representable.

---

## BatchNormTrainingUpcast — BF16 batch-norm-training widened to F32

### Purpose

Computing batch-norm-training statistics (mean and variance) in BF16 is numerically fragile — BF16's 8-bit mantissa loses precision in the running sums. This `HloModulePass` finds `batch-norm-training` ops with BF16 operands, recomputes them in F32, and converts the three tuple results back to BF16 so the surrounding graph sees an unchanged BF16 type signature. F32 batch-norm-training is left alone; inference (`kBatchNormInference`, 0x11) and grad (`kBatchNormGrad`, 0xF) are not touched.

### Entry Point

```text
BatchNormTrainingUpcast::Run  0x1e898e0 (2881 B, 127 bb)
  ├─ AllOfPattern::Match       0x1e891d0 (579 B)  ── opcode==kBatchNormTraining ∧ predicate
  └─ predicate lambda#1        0x1e89440 (211 B)  ── any operand shape.element_type()==BF16
```

### Gate — `Run` sweep + `AllOfPattern` (CERTAIN)

`Run` walks `computation->MakeInstructionPostOrder()` and matches each instruction against an `xla::match::detail::AllOfPattern<BaseImpl, OpcodeImpl, PredicateImpl>`:

- **OpcodeImpl** = `mov eax, 0x12` @ 0x1e899a8 stored into the pattern → opcode **`0x12` = `kBatchNormTraining`** (CERTAIN — case 18 in `HloOpcodeString` = `"batch-norm-training"`).
- **PredicateImpl** = lambda#1 @ 0x1e89440: iterates the op's operand vector, reads each operand's `shape()`, and tests `cmp dword ptr [rax], 0x10` → **element type == BF16**; returns true if a BF16 operand is present.

```c
bool predicate(const HloInstruction *bnt):                  // sub_1E89440
    for op in bnt->operands():
        if op->shape().element_type() == BF16(0x10):        // shape() @0x1e8948f; cmp [rax],0x10 @0x1e89494
            return true
    return false
```

So the gate = **`kBatchNormTraining` with at least one BF16 operand**. The matched variable is even named `bf16Bnt` in the binary's assertion string (below).

### Algorithm — `Run` rewrite (CERTAIN)

For each matched `bf16Bnt` (operands: input `op0`, scale `op1`, offset `op2`; attributes `epsilon`, `feature_index`):

```c
void rewrite(HloComputation *comp, HloInstruction *bf16Bnt):   // within sub_1E898E0
    // upcast each BF16 operand to F32
    for op in {op0, op1, op2}:
        sF  = ShapeUtil::ChangeElementType(op->shape(), F32=0x0B)   // 0x1e89bbd edx=0Bh ; 0x1e89bc5
        cvt = AddInstruction(CreateConvert(sF, op), "")            // 0x1e89beb

    eps  = bf16Bnt->epsilon()                                     // 0x1e89f0e
    fidx = bf16Bnt->feature_index()                               // 0x1e89f22
    outShapeF32 = ShapeUtil::MakeValidatedTupleShape(             // 0x1e89fa7
                      { F32 output, F32 mean, F32 var })
    f32Bnt = AddInstruction(CreateBatchNormTraining(              // 0x1e8a01c
                  outShapeF32, cvt0, cvt1, cvt2, eps, fidx), "")

    // narrow each of the 3 tuple results back to BF16, then re-tuple
    for i in {0, 1, 2}:
        gte_i = GetTupleElement(<bf16 elem shape>, f32Bnt, i)     // CreateGetTupleElement @0x1e8a180/0x1e8a19f
        cvt_i = CreateConvert(<orig bf16 elem shape>, gte_i)      // @0x1e8a20c / 0x1e8a21e
        AddInstruction(cvt_i, "")
    newInstTuple = AddInstruction(CreateTuple({cvt_0,cvt_1,cvt_2}), "")   // 0x1e89ced
    CHECK_OK(comp->ReplaceInstruction(bf16Bnt, newInstTuple))     // 0x1e89d5a
```

The post-rewrite assertion string `computation->ReplaceInstruction(bf16Bnt, newInstTuple)` (rodata @ 0x31c8a8, referenced @ 0x1e89d69) names both the BF16-matched op and the re-tupled BF16 result — direct variable-named evidence that the external signature stays BF16 while the statistics are computed in F32. A trailing `CHECK` on `nullptr != entry_computation_` (rodata @ 0x2108c9) guards the module.

### Saturation / range handling (CERTAIN)

No clamp or saturate. Pure `CreateConvert`: BF16→F32 on the three inputs is a lossless widen; F32→BF16 on the three outputs rounds per XLA convert semantics. There are no `Clamp`/`Min`/`Max` callees in the 127-bb body.

> **NOTE —** the F32→BF16 narrowing on the *outputs* means the precision win is confined to the internal sum-of-squares: the externally visible mean/variance are still delivered as BF16. The pass buys numerical stability in the accumulation, not in the stored result.

---

## UpcastAllToFP32 — every non-F32 float in the module → F32

### Purpose

A blanket legalization that normalises **every floating-point element type in the module to F32**: F16, F64, BF16, and the entire FP8/FP4 block. It is the heavy hammer used to force a whole graph onto the F32 datapath (a debug / golden-reference-grade legalization, run mid-pipeline). The per-shape decision lives in the anonymous-namespace helper `UpcastShapeToFP32`; `Run` applies it to instruction result shapes and to constant literals.

### Entry Point

```text
UpcastAllToFP32::Run        0x1f74640 (1986 B, 112 bb)
  └─ UpcastShapeToFP32      0x1f74020 (1057 B, 49 bb)  ── the dtype dispatch (anon namespace)
        recurse on TUPLE element shapes
```

### `Run` — shape replacement vs constant rebuild

`Run` sweeps the module; for each candidate it computes `newShape = UpcastShapeToFP32(shape)` and skips when `Shape::Equal(shape, newShape)` (already F32). For a **constant**, the literal is rebuilt: `literal()` → `LiteralBase::ConvertToShape(newShape)` → `CreateConstant` → `ReplaceAllUsesWith` → `RemoveInstruction`. For a **non-constant instruction**, the result shape is replaced in place (HloModuleConfig copied). The tuple-recurse reserve path is anchored by the `vector::reserve` string (rodata @ 0x24b6e2).

### Algorithm — `UpcastShapeToFP32` dtype dispatch @ 0x1f74020 (CERTAIN)

The helper reads `et = shape.element_type()` (`[Shape+0]`) and dispatches. The exact branch immediates are disasm-verified:

```c
Shape UpcastShapeToFP32(const Shape &shape):                // sub_1F74020
    et = shape.element_type()                               // mov eax,[rsi] @0x1f74047

    // float family F16(0xA)/F32(0xB)/F64(0xC), and BF16(0x10)
    if (et - 0xA) <= 2  or  et == 0x10:                     // lea edx,[rax-0Ah]; cmp edx,2 @0x1f7404c ; cmp eax,0x10 @0x1f74053
        if et == F32(0xB):                                  // cmp eax,0Bh @0x1f7405e
            return shape                                    // already F32, copy unchanged @0x1f74067
        return ShapeUtil::ChangeElementType(shape, F32=0x0B)   // edx=0Bh @0x1f74291

    // FP8/FP4 + S4/U4 block: codes 0x13..0x21
    if (et - 0x13) <= 0xE:                                  // lea edx,[rax-13h]; cmp edx,0Eh; jbe @0x1f740a6
        if CSWTCH_446[et - 0x13] != 0:                      // table lookup @0x1f74288
            return ShapeUtil::ChangeElementType(shape, F32=0x0B)
        if (et - 0x20) <= 1:                                // et in {0x20,0x21}: sub eax,0x20; cmp eax,1; jbe @0x1f742b0
            return ShapeUtil::ChangeElementType(shape, F32=0x0B)
        return shape                                        // integer/other: unchanged @0x1f74067

    // tuple: recurse element-wise
    if et == TUPLE(0xD):                                    // cmp eax,0Dh @0x1f740ac
        elems = [ UpcastShapeToFP32(e) for e in shape.tuple_shapes() ]   // loop @0x1f740b1..
        return ShapeUtil::MakeValidatedTupleShape(elems)

    return shape                                            // PRED/S8..U64, tokens, etc.: unchanged
```

### Data Tables — `CSWTCH_446`, the FP8/FP4 selector @ VA 0x410830 (CERTAIN bytes)

Raw read from `_rodata.bin` (VA 0x410830 → dump offset 0x203ef0): `01 01 00 00 01 01 01 00 00 01 01 00 00 00 01 00`. Indexed by `et − 0x13`. A `1` byte means "convert this code to F32". The numeric code→F32 decision is read straight out of the table. The FP8/FP4 *labels* in the next column are not: they are assigned from this XLA vintage's dtype ordering. The binary does carry the name strings — f8E5M2, f8E4M3FN, f8E4M3B11FNUZ, f8E5M2FNUZ, f8E4M3FNUZ, f8E3M4, f8E8M0FNU, f4E2M1FN — but the per-index code↔name binding was never byte-pinned.

| idx | et | byte | → F32? | dtype (reconstructed label) |
|---:|---:|:---:|:---:|---|
| 0 | 0x13 | 01 | yes | F8E5M2 |
| 1 | 0x14 | 01 | yes | F8E4M3FN |
| 2 | 0x15 | 00 | no  | S4 (integer) |
| 3 | 0x16 | 00 | no  | U4 (integer) |
| 4 | 0x17 | 01 | yes | F8E4M3B11FNUZ |
| 5 | 0x18 | 01 | yes | F8E5M2FNUZ |
| 6 | 0x19 | 01 | yes | F8E4M3FNUZ |
| 7 | 0x1A | 00 | no  | (int / other) |
| 8 | 0x1B | 00 | no  | (int / other) |
| 9 | 0x1C | 01 | yes | F8E3M4-family |
| 10 | 0x1D | 01 | yes | F8 float |
| 11 | 0x1E | 00 | no  | (int / other) |
| 12 | 0x1F | 00 | no  | (int / other) |
| 13 | 0x20 | 00 | yes* | F4E2M1FN |
| 14 | 0x21 | 01 | yes | F8E8M0FNU |

> **GOTCHA —** code `0x20` has `CSWTCH_446[13] == 0` yet is still converted to F32. The table lookup returns "no", but the fallthrough special case `sub eax,0x20; cmp eax,1; jbe` (@ 0x1f742b0) catches `0x20` and `0x21` and routes both to F32 anyway. A reimplementation that drives purely off the 15-byte table will wrongly leave `0x20` unchanged. The table covers 14 of the 15 codes; the 0x20/0x21 pair has a second guard that is redundant for 0x21 but is the only path that converts 0x20.

### Net policy

**Upcast to F32:** F16 (0xA), F64 (0xC), BF16 (0x10), and the FP8/FP4 floats {0x13, 0x14, 0x17, 0x18, 0x19, 0x1C, 0x1D, 0x20, 0x21}. **Left unchanged:** F32 (0xB) itself, all true integers (PRED..U64, S4=0x15, U4=0x16), tokens, and the non-float codes 0x1A/0x1B/0x1E/0x1F. **TUPLE (0xD):** recursed element-wise.

> **GOTCHA — `UpcastAllToFP32` lifts *floats* to F32, not everything.** The generic name
> invites reading it as "lift all element types, integers included." It does not: true
> integers, S4 (0x15) and U4 (0x16) among them, are explicitly left unchanged (CSWTCH byte
> 0), and only floating-point element types are touched. A pass built on the broader
> reading diverges on every integer tensor in the module.

---

## Per-pass dtype-conversion policy

| Pass | Touched op / operand | From | To | Condition | Conf |
|---|---|---|---|---|---|
| IntMatmulDowncast | dot operand 0/1 | any integer | F32 (0x0B) | gate matched ∧ operand `AreAllLeavesIntegers` | CERTAIN |
| IntMatmulDowncast | the `kDot` (0x2E) compute | integer-result | F32 (0x0B) | dot output `AreAllLeavesIntegers` | CERTAIN |
| IntMatmulDowncast | dot result convert | F32 | original int type | always (re-narrow) | CERTAIN |
| IntMatmulDowncast | all 4 new ops | — | — | stamp `auto_cast=none` + ` Downcast for int support ` | CERTAIN |
| BatchNormTrainingUpcast | BNT operands (input/scale/offset) | BF16 (0x10) | F32 (0x0B) | opcode `kBatchNormTraining` (0x12) ∧ BF16 operand | CERTAIN |
| BatchNormTrainingUpcast | each of 3 tuple results | F32 | BF16 (0x10) | re-narrow via GTE + Convert + Tuple | CERTAIN |
| UpcastAllToFP32 | every value/constant shape | F16/F64/BF16 | F32 (0x0B) | element type is one of these | CERTAIN |
| UpcastAllToFP32 | every value/constant shape | FP8/FP4 {0x13,0x14,0x17,0x18,0x19,0x1C,0x1D,0x20,0x21} | F32 (0x0B) | CSWTCH_446 / 0x20-0x21 special case | CERTAIN (codes) / MEDIUM (labels) |
| UpcastAllToFP32 | F32, integers, tokens | (self) | (unchanged) | — | CERTAIN |
| UpcastAllToFP32 | TUPLE (0xD) | per-element | recurse | — | CERTAIN |

---

## Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `NeuronIntMatmulDowncast::InstructionMatchesPattern` | 0x1fa9f10 | 37 | int-dot gate | CERTAIN |
| `NeuronIntMatmulDowncast::ExpandInstruction` | 0x1faa9b0 | 1010 | int→F32→int rewrite | CERTAIN |
| `xla::hilo::add_auto_cast_none` | 0x1faa1a0 | 1900 | stamp `auto_cast=none` + metadata | CERTAIN |
| `RegisterNeuronIntMatmulDowncast` factory `_M_invoke` | 0x1e71670 | 73 | `new(0x10)` + inline vtable | CERTAIN |
| `BatchNormTrainingUpcast::Run` | 0x1e898e0 | 2881 | BF16 BNT → F32 → BF16 | CERTAIN |
| `AllOfPattern<…>::Match` | 0x1e891d0 | 579 | opcode + predicate match | CERTAIN |
| BNT predicate lambda#1 `_M_invoke` | 0x1e89440 | 211 | operand BF16 test | CERTAIN |
| `UpcastAllToFP32::Run` | 0x1f74640 | 1986 | module-wide all-float→F32 | CERTAIN |
| `(anon)::UpcastShapeToFP32` | 0x1f74020 | 1057 | per-shape dtype dispatch | CERTAIN |

---

## Considerations

- **Run order vs registration order.** In registration these are #51 (BNT) < #60 (UpcastAllToFP32) < #68 (IntMatmulDowncast). BNT upcast runs early; UpcastAllToFP32 mid-pipeline; IntMatmulDowncast late. The actually-executed subset is driver-supplied (the Python/HLOToTensorizer layer selects passes), which was not traced — treat the placement as registration-order, not a confirmed run schedule.
- **No Hex-Rays for these bodies.** All reconstructions are from full disasm walk + callee names + verbatim rodata strings + the CSWTCH bytes. Exact control-flow edges are MEDIUM confidence; the dtype policy, op sets, and immediates are CERTAIN (they are individual instructions, verified byte-for-byte above).
- **The shared `0x0B` is the cross-check.** If a reimplementation of any of these passes emits a `ChangeElementType` with anything other than the F32 code, it has diverged. All five `ChangeElementType` call sites across the three passes use `edx = 0x0B`.

---

## Related Components

| Name | Relationship |
|---|---|
| `int8-quantize` (4.28) | Same F32-emulation strategy for integer ops; sibling in intent |
| `int-scalar-reduce-decomposition` (4.10) | Other integer-op legalization at the HLO level (reduce, not matmul) |
| `ccops-decompose-legalize` | Owns the downstream Tensorizer auto-cast machinery that `auto_cast=none` locks against |

## Cross-References

- [INT8 Uniform-Quantize / Dequantize Legalization](int8-quantize-legalization.md) — §4.28, the F32-emulation theme applied to int8 matmul lowering
- [Integer All-Reduce & Scalar-Reduce Decomposition](int-scalar-reduce-decomposition.md) — §4.10, integer-op decomposition at the HLO level
- [CC-Op Decompose & Legalize Family](ccops-decompose-legalize.md) — the Tensorizer auto-cast machinery keyed by the `auto_cast` frontend attribute
- [Numerics](../numerics/precision-model.md) — Part 9, the F32/BF16/FP8 datapath precision model these passes feed into
