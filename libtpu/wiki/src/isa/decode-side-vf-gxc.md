# Decode-Side: VF / GXC

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (`libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols; `.text` and `.rodata` mapped 1:1, VA == file offset). Other wheel versions differ.*

## Abstract

This page documents the **disassembler inverse** of the three V5+ TensorCore MXU encoders: Viperfish (TPU v5, namespace `vxc`), Ghostlite (TPU v6 lite / cloud v6e, `gxc::glc`), and the `6acc60406` family (cloud TPU7x, `gxc::gfc`). It is the V5+ counterpart of the [JF / PF decode-side](decode-side-jf-pf.md) page: where Jellyfish decodes with a two-level opcode `switch` and Pufferfish introduces the staged-copy `Opcode::Matches` sweep, all three V5+ generations use that same sweep with no other decode path. The page proves every encode-side bit position from the decode side, names the two previously-inferred Viperfish latch control fields, and corrects the cross-generation MXU1 twin geometry.

The decode mechanism is uniform and inverse to the [V5+ `BitCopy` encode model](v5plus-emitx-bit-positions.md). `TensorCoreVectorExtended0Decoder::Decode` stages up to 9 bundle bytes into a scratch struct (a size-tag `1` at offset 0, bundle bytes at offset 8 — `v10 = 9; if (len < 9) v10 = len`), reads the predication field first via `…PredicationField::GetConcatenatedValue`, then sweeps a fixed sequence of per-opcode `Opcode::Matches` predicates. Each `Matches` is a mask/value test over the staged quadword; each operand `…Field::GetConcatenatedValue` is a `shift & mask` accessor. Because the bundle bytes sit at staged offset 8, an accessor that reads the staged quadword at struct offset 8 and shifts right by `S` is reading absolute bundle bit `S` directly — which is why the decode-side shifts equal the encode-side `BitCopy dst_bit` arguments one-for-one.

The single most important V5+ structural fact is the **−N MXU1 twin**: the two `VectorExtended` slots' control regions are a fixed bit-offset apart, and the decode side proves the offset and corrects it. Viperfish's MXU1 is **−20** vs MXU0; Ghostlite's is **−21**; and the `6acc60406` family's is **−25** — not −21 as an earlier cross-generation table carried over from Ghostlite. The reason is that the `6acc60406` MXU0 control region drifts +4 bits higher than Ghostlite's while MXU1 stays anchored at the same absolute bit 39 in both GXC generations, so the inter-slot delta grows by 4. The decode side also resolves the two Viperfish latch control bits the encode-side analysis left inferred: abs 57 is the `Transpose` field and abs 58 is the `Target` (matrix-staging-register select) field — both named directly by the decoder's `…TransposeField` / `…TargetField` accessors and traced to their MLIR producers.

For reimplementation, the contract is:

- The **V5+ decode mechanism**: `Decoder::Decode` → staged copy (`stage[0]=1`, bundle bytes at `stage+8`) → `GetConcatenatedValue` predication read → linear `Opcode::Matches` sweep, the byte-exact inverse of the encode-side `BitCopy`.
- The **Viperfish per-field bit map** and its **−20 twin**: every control field and the predication field shift exactly −20 from MXU0 to MXU1; the eight register-operand selectors do not move (shared pool).
- The **`Transpose` (abs 57) / `Target` (abs 58)** latch fields and their producer→encoder→decoder chain.
- The **GXC dtype-class decode**: the glc unified-8-bit-opcode latch discriminator, the float/int dtype-class matrices, the float-only `6acc60406` (gfc) set, and the corrected **−21 (glc) / −25 (gfc)** twins.

| | |
|---|---|
| **VF decoder entry** | `TensorCoreVectorExtended0Decoder::Decode` @ `0x1ef6e4c0` (MXU0), `…Extended1Decoder::Decode` @ `0x1efb9760` (MXU1) |
| **VF stage** | `*(qword)stage = 1`; `memcpy(stage+8, span, min(len,9))`; predication read first, bound `< 0x10` |
| **VF latch opcode** | abs 59 w5 (`Pushmatrix*`, value 14 for all dtypes), data-format abs 51 w4 |
| **VF matmul opcode** | abs 57 w7 (`MatrixMultiply*`, `0x2` Msra / `0x3` Msrb), `MatmulDataFormat` abs 51 w4 |
| **VF Transpose / Target** | abs 57 / abs 58 (`…PushmatrixBf16TransposeField` `0x1ef9db00` / `…TargetField` `0x1ef9db20`) |
| **VF twin** | −20 (control fields + predication; the 8 operand selectors are delta-0) |
| **glc decoder** | `…Extended0Decoder::Decode` @ `0x1f2f69a0`; opcode abs 60 w6, dtype-class abs 54 w2 |
| **gfc decoder** | `…Extended0Decoder::Decode` @ `0x1f96d020` (`gxc::gfc` = `6acc60406` / cloud TPU7x); opcode abs 64 w6, dtype-class abs 59 w2, bit-62 guard |
| **GXC twins** | glc −21, gfc −25 (MXU0 drifts +4 glc→gfc; MXU1 anchors at abs 39 in both) |
| **MSR count** | JF 1, PF 1, VF 2, glc 2 (`xla::jellyfish::*Target::MatrixStagingRegisterCount` `0x1d490340`/`…4949e0`/`…49ace0`/`…497ae0`) |
| **Encode-side check** | GXC encoder `CreateEncoderGlGf` @ `0x1e831020` (GL/GF share); VF/GL/GF bundle = 64B, runtime `field[32]/field[31]` via `GetTileInstructionBundleSizeInBytes` @ `0x1395b8e0` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Viperfish (TPU v5) — The Per-Field Decode and the −20 Twin

### Purpose

`TensorCoreVectorExtended0Decoder::Decode` (`0x1ef6e4c0`) and `…Extended1Decoder::Decode` (`0x1efb9760`) reconstruct the `TensorCoreVectorExtended0`/`1` proto from a Viperfish bundle. They are the byte-exact inverse of the `BitCopy`-packed Viperfish MXU slot ([Viperfish bundle](bundle-vf-64b.md#mxu-slots--vectorextended0--vectorextended1)); reading the same bits the encoder wrote independently confirms every position.

### Entry Point

```text
TensorCoreVectorExtended0Decoder::Decode @0x1ef6e4c0     ── MXU0 (control region abs 48..64)
  ├─ TensorCoreVectorExtended0PredicationField::GetConcatenatedValue  (read FIRST, bound < 0x10)
  ├─ …NoopOpcode::Matches
  ├─ …MatrixMultiply<fmt>Lgmr{Msra,Msrb}Opcode::Matches    @0x1ef98580..  (mask 0xFE78…)
  ├─ …Pushmatrix<fmt>[Masked]Opcode::Matches               @0x1ef98b80..  (mask 0xF878…)
  └─ … (98 Extended0 op families)
TensorCoreVectorExtended1Decoder::Decode @0x1efb9760     ── MXU1 (control region abs 28..44, −20)
```

### Algorithm — Staged Copy and the Field Accessors

```c
// TensorCoreVectorExtended0Decoder::Decode @ 0x1ef6e4c0 (decompiled, verified)
function Decode(span):
    ve = arena.DefaultConstruct<TensorCoreVectorExtended0>()
    *(qword*)stage = 1                                      // size-tag at offset 0
    n = min(span.len, 9)                                    // v10 = 9; if (a5 < 9) v10 = a5
    memcpy(stage + 8, span.data, n)                         // bundle bytes at stage+8 (abs 0..)
    pred = TensorCoreVectorExtended0PredicationField::GetConcatenatedValue(stage)
    if (pred >= 0x10) return Error                          // 4-bit predicate bound on VF
    ve.predication = pred
    if (MatrixMultiplyBf16LgmrMsra::Matches(stage)) { decode matmul operands; return Ok }
    …
    if (PushmatrixBf16::Matches(stage))             { decode latch operands; return Ok }
    …
```

Each operand accessor is a `shift & mask` over the staged quadword at offset 8 (= abs bit 0). Two accessors pin the latch control fields directly:

```c
// the two latch control fields (verified)
TensorCoreVectorExtended0PushmatrixBf16TransposeField::GetConcatenatedValue:  // 0x1ef9db00
    return (stage.qword[1] >> 57) & 1;     // abs 57
TensorCoreVectorExtended0PushmatrixBf16TargetField::GetConcatenatedValue:     // 0x1ef9db20
    return (stage.qword[1] >> 58) & 1;     // abs 58
```

`stage.qword[1]` is the staged quadword at offset 8, so `>> 57` reads absolute bundle bit 57 and `>> 58` reads abs 58 — exactly the bits the encoder's `BitCopy(buf, 57, …, 1)` / `BitCopy(buf, 58, …, 1)` wrote (verified in `EncodeTensorCoreVectorExtended0PushmatrixBf16` @ `0x1efaf820`).

### The Latch Opcode/Mnemonic Decode Table

The latch (`Pushmatrix<fmt>`) `Opcode::Matches` predicates read the staged quadword with mask `0xF878000000000000` (bits 51..54 = data-format, bits 59..63 = opcode-high). The plain variants all carry opcode-high 14; the data-format field at abs 51 distinguishes the dtype. The masked variants drop the format and carry the dtype in a distinct opcode-high value (`>> 59 ==`):

| Mnemonic | opcode-high (abs 59 w5) | format (abs 51 w4) | Matches | Confidence |
|---|---|---|---|---|
| `PushmatrixRounded` (`0x1ef98b80`) | 14 | 0 | `& 0xF878… == 0x7000000000000000` | CONFIRMED |
| `PushmatrixBf16` (`0x1ef98c00`) | 14 | 3 | `== 0x7018000000000000` | CONFIRMED |
| `PushmatrixBf8` (`0x1ef98c40`) | 14 | 4 | `== 0x7020000000000000` | CONFIRMED |
| `PushmatrixU8` (`0x1ef98c80`) | 14 | 5 | `== 0x7028000000000000` | CONFIRMED |
| `PushmatrixU8Masked` (`0x1ef98ca0`) | 20 | — | `>> 59 == 20` | CONFIRMED |
| `PushmatrixS4` / `…Masked` | 14 / 23 | 8 / — | per fmt | CONFIRMED |

The full plain set is `{Rounded 0, PackedIf8Conv 2, Bf16 3, Bf8 4, U8 5, S8 6, U4 7, S4 8}` at format abs 51 (opcode-high always 14); the masked set carries `{Rounded 15, PackedIf8Conv 17, Bf16 18, Bf8 19, U8 20, S8 21, U4 22, S4 23}` directly in opcode-high abs 59. There is no opcode-high 16 — `RoundedMasked` is 15, then `PackedIf8ConvMasked` is 17 (format 1 has no masked variant).

The matmul (`MatrixMultiply<fmt>`) predicates use mask `0xFE78000000000000` (bits 51..54 format + bits 57..63 opcode); the `MatrixMultiplyBf16LgmrMsra` body (`0x1ef98580`) is `(stage.qword[1] & 0xFE78000000000000) == 0x0408000000000000`, decoding to opcode `0x2` (Msra) at abs 57 and `MatmulDataFormat 1` (Bf16) at abs 51. The `MatmulDataFormat` enum (`Bf16 1, U8 2, S8 3, U4 4, S4 5, Bf8 6`) is a *distinct* ordinal space from the latch Pushmatrix format (Bf16 = 3) even though both occupy abs 51 w4.

> **QUIRK — the opcode field changes position and width by op family at one slot.** `MatrixMultiply<fmt>` is a 7-bit opcode at abs 57; `Pushmatrix<fmt>` is a 5-bit opcode-high at abs 59. On the latch/push path bits 57 and 58 are *repurposed* as the 1-bit `Transpose` and `Target` fields — the same physical bits that on the matmul path are the two high bits of the 7-bit opcode. The decoder disambiguates by opcode-high value (push/latch 14 vs matmul `0x1`-family). A decoder that masks every op as a 7-bit @57 opcode will read a latch's Transpose/Target bits as opcode bits. See [Viperfish bundle](bundle-vf-64b.md#mxu-slots--vectorextended0--vectorextended1).

### The −20 Twin

`Extended1Decoder::Decode` is the same sweep over a control region 20 bits lower. Each MXU1 field accessor reads the matching MXU0 bit minus 20; the eight register-operand selectors read the *same* absolute bits in both slots (the shared systolic-feed pool):

| Field | MXU0 abs | MXU1 abs | Δ | Confidence |
|---|---|---|---|---|
| latch opcode-high | 59 | 39 | −20 | CONFIRMED |
| matmul opcode | 57 | 37 | −20 | CONFIRMED |
| data-format | 51 | 31 | −20 | CONFIRMED |
| Transpose | 57 | 37 | −20 | CONFIRMED |
| Target | 58 | 38 | −20 | CONFIRMED |
| predication | 64 | 44 | −20 | CONFIRMED |
| 8 register-operand selectors | 157/180/214/225/248/259/282/293 | (same) | 0 | CONFIRMED |

The MXU1 accessors confirm the offset byte-for-byte: `…Extended1PushmatrixBf16TransposeField` (`0x1efe8bc0`) reads `>> 37 & 1` and `…TargetField` (`0x1efe8be0`) reads `>> 38 & 1`; the MXU1 matmul predicate (`0x1efe3700`) masks `0xFE780000000` (opcode abs 37, format abs 31). The predication field is part of the twin too: MXU0 predication is read via `Extended0PredicationField` (bound `< 0x10`, abs 64) and MXU1 via `Extended1PredicationField` (abs 44) — a −20 detail the encode-side analysis did not isolate, since predication is written by a separate template.

> **NOTE —** two `VectorExtended` issue slots is confirmed on the decode side too: the binary has 98 `Extended0` and 98 `Extended1` per-op `Opcode::Matches` decode helpers (`vxc::isa::TensorCoreVectorExtended{0,1}*Opcode::Matches`) and no `Extended2`/`Extended3` `Decoder::Decode`. The physical `mxu_count` (4 on VF) is the systolic-array count addressed by the unit_id quadrant, not extra bundle slots — see [MXU Slot](slot-mxu.md#viperfish-v5p--named-opcode-families-and-the-latch-control-bits).

---

## The Transpose and Target Latch Fields — Resolved

The two 1-bit Viperfish latch fields the decode side names trace through the full producer → encoder → decoder → cost-model chain. Both were left semantically inferred by the encode-side analysis; the decoder's field names plus the MLIR producer resolve them.

### Transpose (abs 57)

`Transpose` is the MLIR `tpu.matmul_push_rhs` op's `transpose` attribute — *latch the RHS weight matrix into the systolic array transposed* (matmul-with-transposed-weights at latch time). The chain:

```text
mlir::tpu::MatmulPushRhsOp                        attrs {mxu_index, staging_register, transpose}
  └─ getStagingRegister @0x14b2e6a0
  → OpConversion → mlir::llo::VectorLatchIOp       getMsr @0x13fbbea0, getMxuId @0x13fbbf00
  → ViperfishTensorCoreEmitter::EmitVectorLatchCommon
       (sig …, MatpushTarget); Pushmatrix lambda @0x14204700:
         stage[a2+0x20] = transpose_bool   (proto +0x20)     // [rsi+0x20] = dl
         stage[a2+0x24] = MatpushTarget    (proto +0x24)     // [rsi+0x24] = ecx
  → EncodeTensorCoreVectorExtended0PushmatrixBf16 @0x1efaf820:
         BitCopy(buf, 57, &proto[0x20], 0, 1)   // Transpose @ abs 57
         BitCopy(buf, 58, &proto[0x24], 0, 1)   // Target    @ abs 58
  → decoder TransposeField (>>57&1) / TargetField (>>58&1)
```

The producer lambda (`0x14204700`) writes the `bool` transpose argument to proto `+0x20` (`*(_BYTE*)(a2+0x20) = a3`) and the `MatpushTarget` enum to proto `+0x24` (`*(_DWORD*)(a2+0x24)`), confirmed in the decompile. The encoder then `BitCopy`s `+0x20` to abs 57 and `+0x24` to abs 58. The decoder reads them back at 57/58. So Transpose @ abs 57 controls weight-transpose-at-latch, distinct from the standalone `EmitVectorTranspose` op family (which routes data through the XLU).

### Target (abs 58)

`Target` is the `staging_register` attribute = the **MatrixStagingRegister (MSR)** bank select. The per-generation `*Target::MatrixStagingRegisterCount` confirms why this field exists only from Viperfish (TPU v5) onward:

| Generation | `MatrixStagingRegisterCount` | @ addr | Target field? |
|---|---|---|---|
| Jellyfish (TPU v2) | 1 | `0x1d490340` | none (1 MSR, nothing to select) |
| Pufferfish (TPU v4) | 1 | `0x1d4949e0` | none |
| Viperfish (TPU v5) | 2 | `0x1d49ace0` | abs 58 (picks MSR0 / MSR1) |
| Ghostlite (TPU v6e) | 2 | `0x1d497ae0` | (GXC unified-opcode scheme, below) |

All four are `xla::jellyfish::*Target::MatrixStagingRegisterCount`. Viperfish has two MSR banks, so the 1-bit Target field at abs 58 selects one. JF/PF have a single MSR and therefore carry no Target bit, consistent with the [JF](bundle-jf-41b.md) / [PF](bundle-pf-51b.md) slot maps having no such field.

> **GOTCHA — the latch `transpose` bit is not the standalone transpose op.** Transpose @ abs 57 transposes the weight matrix as it is pushed into the array (a Pushmatrix-latch attribute). A separate `EmitVectorTranspose<viperfish>` (`0x141c3f00`) family emits `TransposeStart`/`Continue`/`End`/`Packed`/`Segmented` opcodes that route data through the XLU/transpose unit. They are two different transpose mechanisms — the latch bit is a free repurpose of the matmul-opcode-LSB bit on the latch path, not the transpose-op opcode. A reimplementer must not fold them. See [MXU Slot](slot-mxu.md#the-transpose-and-target-latch-fields).

> **NOTE —** the decoded Transpose / Target bits feed the cost model `SetReservations<MatpushModifier>` (`0x1c8abde0`, building a `flat_hash_map<MatpushModifier, array<int,19>>` — the `MatpushModifier` key encodes `{dtype × transpose}`, the 19-wide value is the per-`MxuResource` reservation vector; the body bounds-checks `resource_index < MxuResource::kNumMxuResources` = 19, from `mxu_latency_table_vf.cc`) and the transpose-load latency `LatencyTableViperfish::XposeXLUReservationLatency` (`0x1c8a4f00`). The exact `MatpushTarget` ordinal → physical MSR-bank label is not isolated (value 0 = MSR0 / 1 = MSR1 is the natural reading; LOW confidence on the per-value hardware-bank mapping). The `transpose` field's downstream systolic transpose-load is the modeled latency; the bit itself is CONFIRMED.

---

## GXC (Ghostlite v6e / `6acc60406` TPU7x) — Wider Opcodes and the Corrected Twins

The GXC generations keep the V5+ codec shape — staged copy, linear `Opcode::Matches` — but widen the opcode field 7→8 bits, fold the latch discriminator into the opcode low bits, and shift the dtype set.

### Ghostlite (v6e / glc)

The glc decoder (`…Extended0Decoder::Decode` @ `0x1f2f69a0`) reads a **unified 8-bit opcode at abs 58**: the matmul uses the full 8-bit value (`0x2` Msra / `0x3` Msrb, MSR-select = the opcode LSB), and the latch's opcode-high sits at abs 60 (w6) with the low two bits at abs 58/59 acting as the latch-class discriminator. The dtype-class is a 2-bit field at abs 54.

```c
// glc PushMatrixBf16Opcode::Matches @ 0x1f326500 (verified)
v1 = stage.qword[1];                                  // abs 0..63
result = (~v1 & 0xC00000000000000) != 0               // bits 58,59 not both set (latch-class)
      && ((stage.oword >> 60) & 0x3F) == 0xE          // opcode @ abs 60 w6 == 14 (float)
      && (v1 & 0xC0000000000000) == 0x80000000000000; // dtype-class @ abs 54 w2 == 2 (Bf16)

// glc MatrixMultiplyBf16LgmrMsraOpcode::Matches @ 0x1f325b60 (verified)
v1 = stage.oword;
return (((v1 >> 58) & 0xFF) == 2)                     // opcode @ abs 58 w8 == 0x2 (Msra)
    && ((v1 & 0xF0000000000000) == 0x10000000000000); // format @ abs 52 w4 == 1 (Bf16)
```

The dtype-class field at abs 54 carries the sub-ordinal within the 4-element class selected by the opcode (14 = float, 15 = int): float `{F32 0, If8 1, Bf16 2, Bf8 3}`, int `{U8 0, S8 1, U4 2, S4 3}`. `GhostliteTarget::MatrixStagingRegisterCount` (`0x1d497ae0`) returns 2.

The MXU1 twin is **−21**, confirmed from the MXU1 predicates: `Extended1PushMatrixBf16Opcode` (`0x1f37ac60`) reads opcode @ abs 39 (mask `0x1F8000000000`, value `>> 39 = 14`), dtype-class @ abs 33, guard @ abs 37/38; `Extended1MatrixMultiplyBf16LgmrMsra` (`0x1f37a5a0`) masks `0x1FE780000000` (opcode abs 37 = `0x2`, format abs 31). Every field is exactly −21 vs MXU0 (op 60→39, class 54→33, matmul op 58→37, matmul fmt 52→31).

| Field | MXU0 abs (glc) | MXU1 abs (glc) | Δ | Confidence |
|---|---|---|---|---|
| latch opcode | 60 | 39 | −21 | CONFIRMED |
| dtype-class | 54 | 33 | −21 | CONFIRMED |
| matmul opcode (w8) | 58 | 37 | −21 | CONFIRMED |
| matmul format | 52 | 31 | −21 | CONFIRMED |

### `6acc60406` (cloud TPU7x / gfc)

The `6acc60406` family is **float-only**: it drops the integer matmul group and supports four dtypes `{F32, E4m3, Bf16, E5m2}` — the two FP8 formats named explicitly (vs Ghostlite's `If8`/`Bf8`). The gfc decoder (`…Extended0Decoder::Decode` @ `0x1f96d020`) reads the opcode-high at abs 64 (w6), the dtype-class at abs 59 (w2), and a single-bit latch valid-guard at abs 62.

```c
// gfc PushMatrixBf16Opcode::Matches @ 0x1f98fd20 (verified)
result = ((stage.dword[4] & 0x3F) == 0xE)             // opcode @ abs 64 w6 == 14 (all four float)
      && ((stage.qword[1] & 0x4000000000000000) == 0) // bit 62 clear (latch valid-guard)
      && ((stage.qword[1] & 0x1800000000000000) == 0x1000000000000000); // dtype-class @ abs 59 == 2 (Bf16)

// gfc MatrixMultiplyBf16LgmrMsraOpcode::Matches @ 0x1f98f740 (verified)
v1 = stage.oword;
return (((v1 >> 62) & 0xFF) == 2)                     // opcode @ abs 62 w8 == 0x2 (Msra)
    && ((v1 & 0x1E00000000000000) == 0x200000000000000); // format @ abs 57 w4 == 1 (Bf16)
```

The dtype-class at abs 59 is `{F32 0, E4m3 1, Bf16 2, E5m2 3}`. The MXU1 twin is **−25**, confirmed from `Extended1PushMatrixBf16Opcode` (`0x1f9ccd60`, opcode @ abs 39, value 14) and `Extended1MatrixMultiplyBf16LgmrMsra` (`0x1f9cc9a0`, mask `0x1FEF00000000`, opcode @ abs 37, format @ abs 32): op 64→39, matmul op 62→37, format 57→32 — all exactly −25.

| Field | MXU0 abs (gfc) | MXU1 abs (gfc) | Δ | Confidence |
|---|---|---|---|---|
| latch opcode | 64 | 39 | −25 | CONFIRMED |
| dtype-class | 59 | 34 | −25 | CONFIRMED |
| matmul opcode (w8) | 62 | 37 | −25 | CONFIRMED |
| matmul format | 57 | 32 | −25 | CONFIRMED |

> **CORRECTION (DEC-GXC-1) —** an earlier cross-generation table listed the `6acc60406` (gfc) inter-MXU twin as **−21** (carried over from Ghostlite). Decode-side disassembly of the gfc `MatrixMultiplyBf16Lgmr*` and `PushMatrix*Opcode::Matches` shows the twin is **−25**: MXU0 opcode @ abs 64, MXU1 @ abs 39; matmul @ 62 vs 37; format @ 57 vs 32. The +4 MXU0 drift (glc 60 → gfc 64) compounds the offset because MXU1 anchors at abs 39 across both GXC generations.

> **NOTE —** the glc latch valid-guard is read here as a 2-bit field at abs 58/59 in the cross-generation analysis, but the decompiled `glc PushMatrixBf16` body tests `(~v1 & bits{58,59}) != 0` (at least one of the two clear) together with the dtype-class at abs 54 — the exact guard polarity and the gfc 1-bit `bt 62` equivalent width are read as confirmed *single-bit/two-bit tests* but their full latch-vs-matmul boundary semantics are the [MXU Slot](slot-mxu.md#ghostlite-v6e--glc) page's territory (LOW confidence on the unified-opcode boundary value). The opcode, dtype-class, and matmul-format positions above are all CONFIRMED byte-exact.

---

## Per-Generation Decode Summary

The V5+ decode reference (this page) completing the [JF / PF](decode-side-jf-pf.md) older-gen counterpart:

| TpuVersion | Codename (binary) | Cloud | Decoder @ | MXU0 opcode | MXU1 opcode | Twin | dtype set | Confidence |
|---|---|---|---|---|---|---|---|---|
| 3 | viperfish (`vxc`) | TPU v5 (v5e/v5p) | `0x1ef6e4c0` | matmul abs 57 / push abs 59 | abs 37 / 39 | −20 | 8 (int + float) | CONFIRMED |
| 4 | ghostlite (`gxc::glc`) | TPU v6e | `0x1f2f69a0` | unified abs 58 (w8) | abs 37 | −21 | 8 (int + float) | CONFIRMED |
| 5 | `6acc60406` (`gxc::gfc`) | TPU7x | `0x1f96d020` | unified abs 62 (w8) | abs 37 | −25 | 4 (float only) | CONFIRMED |

All three use the staged-copy + linear `Opcode::Matches` codec; the per-generation deltas are the opcode widening (7→8 bits), the dtype-set shift (`6acc60406` drops integers, names FP8 explicitly), and the inter-MXU twin (−20 → −21 → −25). The encode side ([V5+ EmitX](v5plus-emitx-bit-positions.md), [Viperfish bundle](bundle-vf-64b.md)) wrote each of these bits with a `BitCopy(buf, abs_bit, …)`, and the decode `Opcode::Matches` masks recover them at the same abs_bit — the round-trip is closed for all three generations.

---

## Related Components

| Component | Relationship |
|---|---|
| `TensorCoreVectorExtended0Decoder::Decode` `0x1ef6e4c0` | VF MXU0 decoder (staged copy + `Opcode::Matches`) |
| `TensorCoreVectorExtended1Decoder::Decode` `0x1efb9760` | VF MXU1 decoder (the −20 twin) |
| `…PushmatrixBf16TransposeField` `0x1ef9db00` / `…TargetField` `0x1ef9db20` | VF abs 57 / abs 58 latch field accessors |
| `ViperfishTensorCoreEmitter::EmitVectorLatchCommon` lambda `0x14204700` | the Transpose/Target producer (proto +0x20/+0x24) |
| `gxc::glc::…Extended0Decoder::Decode` `0x1f2f69a0` | Ghostlite (v6e) decoder (unified 8-bit opcode, −21 twin) |
| `gxc::gfc::…Extended0Decoder::Decode` `0x1f96d020` | `6acc60406` (TPU7x) decoder (float-only, bit-62 guard, −25 twin) |
| `xla::jellyfish::*Target::MatrixStagingRegisterCount` `0x1d490340`..`497ae0` | per-gen MSR count (1/1/2/2) — why Target exists only from Viperfish (TPU v5) onward |

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle, codec-metadata width dispatch, and `kNeverExecute` convention the MXU slot lives inside.
- [Decode-Side: JF / PF](decode-side-jf-pf.md) — the older-gen counterpart: the JF two-level opcode `switch` and the PF staged-copy `Opcode::Matches` origin of this codec.
- [Viperfish 64B Bundle](bundle-vf-64b.md) — the v5p MXU control region, the Transpose/Target latch bits, and the named opcode families this page's decode confirms.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the v4 dual-MXU −20 twin that originates the V5+ twin geometry.
- [MXU Slot](slot-mxu.md) — the cross-generation MXU op family, opcode roster, dtype sets, and the Transpose/Target semantics.
- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the `EmitX` → `BitCopy` encode chain whose decode inverse this page documents; the consolidated per-(slot, gen) bit table.
- [MC Emitter](mc-emitter.md) — the parallel LLVM-MC encoding path for the same vmatmul/vmatprep MachineInstrs.
- [Record Format](record-format.md) — the on-disk record framing around the encoded bundle bytes.
