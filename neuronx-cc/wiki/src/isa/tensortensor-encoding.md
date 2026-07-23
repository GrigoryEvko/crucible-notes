# TensorTensor / Copy / Cast / Select / Memset / MoveShape Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The encoders, the dtype look-up tables and the pybind assert roster live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`); the BIR enums (`NEURON_ISA_TPB_DTYPE`, `bir::AluOpType`, `bir::EngineAccumulationType`) live in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **Family-B "tensor-tensor" wire family** — the cluster of opcodes that build their control band *before* the descriptor slots, in the `+0x0C..+0x0F` nibble band, and lay three 16-byte access-pattern slots across `+0x10..+0x3F` ([2.1, Family B](instruction-bundle.md)). It also covers the two members that break out of the strict 3-slot shape but share the same engine, lifecycle and helpers — the unconditional `TensorCopy` (a Family-C 4D copy), `Memset`, and the `MoveShape` shape-register loader.

The headline result of this page is that the tensor-tensor family is **not** one opcode per `visitInst*` body. **Thirteen distinct wire opcodes are emitted by five `CoreV2GenImpl` visitor bodies.** One body in particular — `visitInstCopyPredicated` — emits **four** opcodes chosen by operand shape, which means the elementwise `Select` / masked-copy / masked-cast machine realisation all flows through a single C++ visitor.

The thirteen opcodes:

- **`InstTensorTensor`** → **`0x41`** (65, plain), **`0x51`** (81, bit-vector), **`0x8A`/`0x8E`** (138/142, GPSIMD int32 band) — the canonical `s3d3_tt` dtype-pair op (add/mul/sub/min/max/compare across two tensors).
- **`InstCopyPredicated`** → **`0xEA`** (234, `SelectReduce`), **`0x72`** (114, `CopyPredicated`), **`0x99`** (153, `CastPredicated`), **`0xE8`** (232, `CopyPredicatedScalar`) — four opcodes, **one body**, selected by operand shape.
- **`InstTensorCopy`** → **`0x46`** (70, plain copy), **`0x47`** (71, casting copy) — unconditional on-engine copy, cast = opcode `+1`.
- **`InstMemset`** → **`0x49`** (73, Const), **`0x4D`** (77, Random/Xorwow) — broadcast-fill.
- **`generateMoveShape`** → **`0xB2`** (178) — DVE shape-register loader, called from the `InstDMA` path.

Two BIR nodes that *look* like members of this family but have **no leaf bundle emitter** are mapped as negative results: `InstSelect` (IT51) is split into `GenericCopy + CopyPredicated` by the `lower_select` pass before codegen, and `InstTensorDequantize` (opcode `0x7B` in the ISA table) is lowered to a scale-multiply chain upstream — neither reaches a `CoreV2GenImpl::visitInst*` body.

> **GOTCHA — `visitInstCopyPredicated` is not a single-opcode encoder, and its four branches do not share one band.** `0xEA` is the `SelectReduce` form (fused masked-copy-with-reduce) and rides a **2-D** AP band at `+0x18`/`+0x30` with the ALU bytes at `+0x24`/`+0x25`. The other three — `CopyPredicated` (`0x72`), `CastPredicated` (`0x99`), `CopyPredicatedScalar` (`0xE8`) — use 3-D bands. Reading the whole body as "CopyPredicated = `0xEA`" gets both the mnemonic and the field offsets wrong. See [§3](#3-visitinstcopypredicated-0x125eb20--four-opcodes-one-body-the-select-family).

The bar for this page: a reimplementer can byte-encode any of the thirteen instructions by hand, knowing for each control byte its offset, width, semantic, value source, the pybind assert string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence tag: CERTAIN = an exact store or compare read byte-exact from the disassembly; HIGH = validator / LUT / pybind-roster cross-reference; MEDIUM = zero-init implied with no direct store. Bit positions are pinned against literal store constants in the disassembly, never inferred. Where a byte has no recovered name, it is tagged reserved — no field name is fabricated.

## At a glance — 13 opcodes, 5 bodies, one family

| Opcode | Mnemonic (ordinal) | Generator (`CoreV2GenImpl`, libwalrus) | Wire struct | Band shape | Operand slots |
|---|---|---|---|---|---|
| `0x41` / `0x51` / `0x8A`/`0x8E` | TensorTensor (65/81/138/142) | `visitInstTensorTensor @0x12356d0` (1965 B) | `s3d3_tt` | 3-D, `+0x0C..0x0F` | dst/in0/in1 `TENSOR3D` `+0x10`/`+0x20`/`+0x30` |
| `0xEA` | SelectReduce (234) | `visitInstCopyPredicated @0x125eb20` (5994 B) | select-reduce | 2-D, `+0x24..0x2C` | dst/mask/src `TENSOR2D` `+0x18`/`+0x30`/(packed) |
| `0x72` | CopyPredicated (114) | `visitInstCopyPredicated @0x125eb20` | `s3d3_tt` | 3-D, `+0x0C..0x0F` | dst/src/mask `TENSOR3D` `+0x10`/`+0x20`/`+0x30` |
| `0x99` | CastPredicated (153) | `visitInstCopyPredicated @0x125eb20` | `s3d3_tt` | 3-D, `+0x0C..0x0F` | dst/src/mask `TENSOR3D` `+0x10`/`+0x20`/`+0x30` |
| `0xE8` | CopyPredicatedScalar (232) | `visitInstCopyPredicated @0x125eb20` | `d3_cp` | 3-D, `+0x20..0x2B` | dst/mask `TENSOR3D` `+0x10`/`+0x30`, scalar `+0x2B` |
| `0x46` / `0x47` | TensorCopy (70/71) | `visitInstTensorCopy @0x1237f50` (1089 B) | `s_copy`/`s3d3_copy` | 4-D Family-C, `+0x20..0x22` | src/dst `TENSOR4D` `+0x0C`/`+0x2C` |
| `0x49` / `0x4D` | Memset (73/77) | `visitInstMemset @0x125b320` (1295 B) | `d3_memset` | 4-D Family-C, `+0x1C..0x28` | dst `TENSOR4D` `+0x2C` |
| `0xB2` | MoveShape (178) | `generateMoveShape @0x1213d00` (3426 B) | `s_move`/`move` | shape-reg, `+0x0C..0x30` | step/num blocks `+0x20`/`+0x30` |

`CoreV3GenImpl::visitInstTensorTensor @0x135acb0` (1839 B) is the same `s3d3_tt` field map with an extended GPSIMD int32 band (see [§2](#corev3-parity)); `CopyPredicated`, `Memset` and `MoveShape` are **CoreV2-only** bodies — CoreV3/V4 route through them and have no own symbol (`nm` sweep zero).

**Header skeleton (every opcode), from `setupHeader @0x1172120` (vtable slot, `call *0x48(%rax)`):**

```
 byte +0x00  opcode byte    (per-op, table above)
 byte +0x01  inst_word_len = 0x10  (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
 byte +0x04..+0x0B  sync band  (wait/update mode+idx, value DWORD — §1)
```

## The bundle lifecycle (shared by all five bodies)

Every body has the identical skeleton (re-confirmed across all five decompiles). `CodeGenMode = *((int*)this + 156)` (`this+0x270`) is a three-way branch:

1. **`GENERATE_ISACODE` (1).** `emplace_back<array<u8,64>>` into the bundle vector at `this+120`; the whole 64 bytes are `movups xmm0`-zeroed first (so any byte the encoder does not write is a hard `0x00`); `setupHeader` stamps `+0x00..+0x03`; the per-op sync writers fill `+0x04..+0x0B`; the control band and `assignAccess<TENSOR{2,3,4}D>` slots are filled; `sub_12095A0` (`this+64`) registers the node; `fwrite(bundle,1,0x40,findBin(inst))`. If `this+0x2B9` (`+697`) is set: a second `createInstBin` + `fwrite` for the census stream.
2. **`RUN_ISA_CHECKS` (2).** The *same* field layout into an on-stack 64-byte scratch buffer, then the L2 silicon validator vcall `(**this)(this,inst,&buf)` (vtable slot 0, `call *(%rax)`); **no** `fwrite`.
3. **`COLLECT_OPCODES` (0).** Inserts the opcode word into a `DenseMap<Inst,set<u32>>` at `this+32`, updates the branch-hint target PC, returns. This is the cleanest opcode witness — a bare `movl $<opcode>,...` then an `_Rb_tree` insert.
4. **else** → `reportError "Wrong CodeGenMode. It must be one of GENERATE_ISACODE, RUN_ISA_CHECKS, or COLLECT_OPCODES"`.

> **GOTCHA — every body has a near-duplicate field-store block.** The mode-2 stack-scratch block mirrors the mode-1 bundle block field-for-field. Do not double-count it as a second emitted bundle: each body emits exactly one bundle. (`CopyPredicated` in particular has *both* a `DWORD`-into-stack opcode write `@0x125ee0e/0x125ee56` and the `BYTE`-into-bundle writes `@0x125ef1d/0x125efd6/...` for the same opcode.)

### Shared field-build helpers

| Helper | Address | Role | Output → |
|---|---|---|---|
| `sub_120E650` | `@0x120E650` | `bir::Dtype → NEURON_ISA_TPB_DTYPE` wire byte = `byte_1DF5760[dtype]` (assert `≤0x13`, `EC946`) | dtype bytes |
| `sub_12039C0` | `@0x12039C0` | `bir::AluOpType → ISA ALU_OP` byte; `0..18` identity, comparison family reorder (`19→24,20→19,21→20,23→21,28→29,29→25`); int32 band `0xC4..` separate | TT `+0x0E`, SelectReduce `+0x24`/`+0x25` |
| `sub_12038D0` | `@0x12038D0` | `bir::EngineAccumulationType → ISA byte` (`0→0,1→1,3→3,4→2,5→4`) | SelectReduce `+0x2C` |
| `qword_1DF59E0` | `@0x1DF59E0` | `bir::Dtype → element BYTE SIZE` (20×8 B, `[1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]`) | MoveShape default step |

The **dtype wire LUT** `byte_1DF5760` (20 bytes, idx = `bir::Dtype`), dumped byte-exact from `.rodata`:

```
03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c
```

At `0x1df5760` the `.rodata` bytes read `0302050d 0e0e030f 0e0f0504 06070908 / 0a0b010c …`. The `qword_1DF59E0` size LUT reads `01000000.. 01000000.. 02000000.. … 04000000` (uint8 = 1, a 16-bit dtype = 2, fp32 = 4, a 64-bit dtype = 8), all 20 entries present.

---

## 2. `visitInstTensorTensor @0x12356d0` (IT31, DVE) — the canonical `s3d3_tt` band

The 2-input elementwise op (add/mul/sub/min/max/compare, etc.). Operands: `out = getOutput(0)`, `in0 = getArgument(0)`, `in1 = getArgument(1)`.

**Opcode byte:** the base is `0x41`; `bir::isBitVecInstruction(inst)` (call to `@0x5ffe20`, witnessed at `0x1235715`) selects `0x51` (`add r13d,0x51 @0x1235742`). The **GPSIMD int32 override** fires when `this+0x258 == 20` (the GPSIMD arch gate) and both inputs are int32 (`dtype == 12`): engine `@inst+0x90 == 4` → `0x8A` (138), engine `== 6` → ALU byte `0xC5`, plus the `+4` variant `0x8E` (142). The override byte is written into `+0x0E`: `mov BYTE PTR [r15+0xe],0xc5 @0x1235c1a` and `…,0xc4 @0x1235e28`, with opcode `mov ebx,0x8a @0x1235e3e`.

> **GOTCHA — PSUM guard.** If `inst+0x90 (engine) == 1` and either input `isLocationPSUM`, the body raises `reportError "GPSIMD engine cannot access PSUM"`. The GPSIMD engine cannot read the PSUM bank.

**64-byte bundle — Family-B control band** (bundle base register `r15`):

| Offset | W | Field | BIR source | pybind name | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x41`/`0x51`/`0x8A`/`0x8E` | `setupHeader(opByte)` | `tensor_tensor_opcode` | CERTAIN |
| `+0x0C` (lo nibble) | 4b | in0 dtype | `sub_120E650(in0.Dtype)` | `s3d3_tt_dtype` | CERTAIN (`movzx [r15+0xc]` … `mov [r15+0xc] @0x1235a40`) |
| `+0x0C` (hi nibble) | 4b | in1 dtype | `sub_120E650(in1.Dtype)` (`16*`) | `s3d3_tt_dtype` | CERTAIN (`mov [r15+0xc] @0x1235a62`) |
| `+0x0D` | 1 | out dtype | `sub_120E650(out.Dtype)` | `s3d3_tt_dtype` | CERTAIN (`mov [r15+0xd] @0x1235a75`) |
| `+0x0E` | 1 | ALU-op wire byte | `sub_12039C0(*(int*)(inst+0xF0))` | `s3d3_tt_valid_op` | CERTAIN (`mov [r15+0xe] @0x1235a85`) |
| `+0x0E` | 1 | int32 override `0xC4`(eng4)/`0xC5`(eng6) | when int32×3 & `inst+0x90==1` | — | CERTAIN (`@0x1235c1a`/`@0x1235e28`) |
| `+0x0F` | 1 | `num_active_partitions` | `in0.firstAPPair.num` (`*(*(in0+80)+8)`) | `s3d3_tt_src_element_cnt_check` | HIGH (`mov [r15+0xf] @0x1235abe`) |
| `+0x10` | 16 | dst access pattern | `assignAccess<TENSOR3D>(out)` | — | CERTAIN |
| `+0x20` | 16 | in0 access pattern | `assignAccess<TENSOR3D>(in0)` | — | CERTAIN |
| `+0x30` | 16 | in1 access pattern | `assignAccess<TENSOR3D>(in1)` | — | CERTAIN |

ASSERT: `in0+88 == 0` (no `APPairs`) → `__assert_fail "idx<size()"` `SmallVector.h:0x128`. ENGINE: relayed from `inst+0x90` (DVE = 5 canonical), no recompute. **BROADCAST**: a stride-0 `APPair` inside `in0`/`in1` is passed verbatim by `assignAccess` — there is no broadcast flag in the band.

> **NOTE — the `s3d3_tt` struct is shared, not exclusive.** The pybind roster carries `s3d3_tt_is_zero_s_copy_cast_predicated_opcode` and `s3d3_tt_dst_elems_batchnorm_backprop_opcode`: `CopyPredicated`/`CastPredicated` (§3.2) and a BatchNorm-backprop opcode all ride the same `s3d3_tt` control band — only the opcode byte differs. The `+0x0C` dtype-pair / `+0x0D` out-dtype / `+0x0F` num triple is byte-identical across them.

#### C pseudocode — TensorTensor encode

```c
// out = in0 (alu) in1, three TENSOR3D operands, s3d3_tt band.
u8 bundle[64] = {0};
u8 op = bir_isBitVecInstruction(inst) ? 0x51 : 0x41;       // 0x1235742
if (gpsimd_int32_gate(this) && all_int32(in0,in1,out)) {   // this+0x258==20, dtype==12
    op = (engine == 4) ? 0x8A : 0x8E;                      // 0x1235e3e
    // ALU byte forced to int32 band:
    bundle[0x0E] = (engine == 6) ? 0xC5 : 0xC4;            // 0x1235c1a / 0x1235e28
}
setupHeader(bundle, op);                                   // +0x00..+0x03, sync +0x04..+0x0B
bundle[0x0C]  = wire_dtype(in0.dtype)        & 0x0F;       // sub_120E650
bundle[0x0C] |= wire_dtype(in1.dtype) << 4;
bundle[0x0D]  = wire_dtype(out.dtype);
if (!gpsimd_int32)
    bundle[0x0E] = wire_alu(*(int*)(inst+0xF0));           // sub_12039C0(AluOpType)
bundle[0x0F]  = in0.pairs[0].num;                          // num_active_partitions
assignAccess_TENSOR3D(&bundle[0x10], out);
assignAccess_TENSOR3D(&bundle[0x20], in0);
assignAccess_TENSOR3D(&bundle[0x30], in1);
```

### CoreV3 parity

`CoreV3GenImpl::visitInstTensorTensor @0x135acb0` is the same `s3d3_tt` 3-D field map (dtype-pair `+0x0C`, out `+0x0D`, ALU `+0x0E`, num `+0x0F`, three `TENSOR3D` APs at `+0x10`/`+0x20`/`+0x30`), with its own dtype mapper `sub_1348870` (== `sub_120E650`) and ALU mapper `sub_1343E40` (== `sub_12039C0`), and an **extended** GPSIMD int32 band `{eng4→0xC4, eng5→0xC6, eng6→0xC5, eng7→0xC7, mod_int(32)→0xC8}`. The band map is the one part read from the validator rather than a direct store.

---

## 3. `visitInstCopyPredicated @0x125eb20` — four opcodes, one body (the Select family)

This single body is the machine realisation of elementwise `Select` / `Where` / masked-cast-copy. Operands: `mask = getArgument<AP>(0)` (per-lane 0/1 predicate), `src = sub_123DF20(inst)` (true-lane data: TENSOR **or** SCALAR), `out = getOutput<AP>(0)` (dst = onFalse seed), `fill = sub_1240360(inst)` (optional false-fill, read only by the `0xEA` branch).

**Top-level selector** (`v5 = *(int*)(inst+0x140)`, the predicate/reduce ALU-op DWORD at `+0x140`):

| Branch condition | Opcode | Mnemonic | AP band |
|---|---|---|---|
| `inst+0x140 != 0` (a reduce ALU present) | `0xEA` (234) | SelectReduce | 2-D `+0x18`/`+0x30` |
| `inst+0x140 == 0` & `src` is TENSOR & `out.dtype == src.dtype` | `0x72` (114) | CopyPredicated | 3-D `s3d3_tt` |
| `inst+0x140 == 0` & `src` is TENSOR & `out.dtype != src.dtype` | `0x99` (153) | CastPredicated | 3-D `s3d3_tt` |
| `inst+0x140 == 0` & `src.isScalar()` | `0xE8` (232) | CopyPredicatedScalar | 3-D `d3_cp` |

All four opcode immediates are in the body: `mov edx,0x72 @0x125ecb7`, `mov eax,0x99 @0x125ecc3`, `mov DWORD…,0xe8 @0x125ee0e`, `mov DWORD…,0xea @0x125ee56` (mode-2 stack writes), plus the `BYTE`-into-bundle stores `…,0xea @0x125ef1d` / `…,0xe8 @0x125fca8`.

### 3.1 SelectReduce (`0xEA` = 234) — 2-D AP band

The fused predicated-copy + reduce form (`inst+0x140 != 0`). It is a **2-D** band — a masked per-lane op is a 2-D operation.

| Offset | W | Field | Source | pybind check | Conf |
|---|---|---|---|---|---|
| `+0x18` | 16 | dst access pattern | `assignAccess<TENSOR2D>(out)` | — | CERTAIN (`lea [r15+0x18] @0x125f944`) |
| `+0x30` | 16 | mask access pattern | `assignAccess<TENSOR2D>(arg0)` | — | CERTAIN (`lea [r15+0x30] @0x125f97b`) |
| (packed) | 16 | src access pattern | `assignAccess<TENSOR2D>(src)` | — | HIGH (3rd 2-D AP; pointer register-packed, not a direct `lea`) |
| `+0x24` | 1 | copy/bypass ALU byte | `sub_12039C0(0) = 0x00` | `s_select_reduce_op` | CERTAIN (`[r15+0x24] @0x125f995`) |
| `+0x25` | 1 | predicate/reduce ALU byte | `sub_12039C0(*(int*)(inst+0x140))` | `s_valid_select_reduce_op` | CERTAIN (`[r15+0x25] @0x125f9b5`) |
| `+0x26` | 1 | `useFalseFill` flag | `*(u8)(inst+0x118)` | — | CERTAIN (`[r15+0x26] @0x125f9f3`) |
| `+0x27` | 1+ | false-fill immediate | `sub_12051E0` `std::visit(fill)` | — | CERTAIN (`lea [r15+0x27] @0x125f9d7`, 3-arm variant writer) |
| `+0x28` (lo) | 4b | mask dtype | `sub_120E650(mask.Dtype)` | `s3d3_copy_pred_src0_dtype` | CERTAIN (`[r15+0x28] @0x125fb0a`) |
| `+0x28` (hi) | 4b | src dtype | `sub_120E650(src.Dtype)` (`16*`) | `s3d3_copy_cast_pred_src1_dst_dty` | CERTAIN (`[r15+0x28] @0x125fb2f`) |
| `+0x29` | 1 | out dtype | `sub_120E650(out.Dtype)` | `s3d3_copy_cast_pred_src1_dst_dty` | CERTAIN (`[r15+0x29] @0x125fb4e`) |
| `+0x2A` | 1 | `num_active_partitions` | `mask.firstAPPair.num` | `s3d3_copy..src_element` | HIGH (`[r15+0x2a] @0x125fb75`) |
| `+0x2B` | 1 | false-fill dtype | `sub_120E650(fill.Dtype)` | — | CERTAIN (`[r15+0x2b] @0x125fb42`) |
| `+0x2C` | 1 | EngineAccumulationType byte | `sub_12038D0(*(int*)(inst+0x144))` | `s_zero_accum_cmd` | CERTAIN (`[r15+0x2c] @0x125fb8d`) |

VARIANT GUARD: `inst+0x138 == 0` else `__throw_bad_variant_access`. The `std::visit` (`sub_12051E0`) routes the false-fill `Argument` variant `{PhysicalAP / RegisterAP / ImmediateValue}` to one of three writer lambdas, each storing the scalar/addr into `+0x27`. ENGINE DVE(5). COLLECT inserts opcode word 234.

### 3.2 CopyPredicated (`0x72`) / CastPredicated (`0x99`) — 3-D `s3d3_tt` band

Guard: `inst+0x140 == 0` and `src` is a TENSOR. **Opcode byte:** `(out.dtype == src.dtype) ? 0x72 : 0x99` — the same cast-bit trick as TensorCopy `0x46`/`0x47` (§4), lifted to the predicated family. `mov eax,0x72 @0x125ecd2` / `mov eax,0x99 @0x125ecc3`. This branch reuses the **TensorTensor** `s3d3_tt` control band byte-for-byte, with the mask AP in the in1 slot:

| Offset | W | Field | Source | Conf |
|---|---|---|---|---|
| `+0x0C` (lo) | 4b | mask dtype | `sub_120E650(mask.Dtype)` | CERTAIN (`[r15+0xc] @0x125f3c6`) |
| `+0x0C` (hi) | 4b | src dtype | `sub_120E650(src.Dtype)` (`16*`) | CERTAIN (`[r15+0xc] @0x125f3e2`) |
| `+0x0D` | 1 | out dtype | `sub_120E650(out.Dtype)` | CERTAIN (`[r15+0xd] @0x125f3fc`) |
| `+0x0F` | 1 | `num_active_partitions` | `mask.firstAPPair.num` | HIGH (`[r15+0xf] @0x125f41c`) |
| `+0x10` | 16 | dst access pattern | `assignAccess<TENSOR3D>(out)` | CERTAIN (`lea [r15+0x10] @0x125f40f`) |
| `+0x20` | 16 | src/true access pattern | `assignAccess<TENSOR3D>(src)` | CERTAIN (`lea [r15+0x20] @0x125f437`) |
| `+0x30` | 16 | mask access pattern | `assignAccess<TENSOR3D>(arg0)` | CERTAIN (`lea [r15+0x30] @0x125f448`) |

There is **no** `+0x0E` ALU byte and **no** false-fill immediate in this branch: the predicated copy/cast is a pure masked move — false lanes keep the `GenericCopy`-seeded onFalse value. A `CopyPredicated`/`CastPredicated` bundle is therefore a *TensorTensor control band with a copy opcode and a mask AP in the in1 slot*. VARIANT GUARD: `inst+0x138 == 0`; pre-emit runs `bir::checkImmValueTypeCompatible` (`EC1099 NeuronAssertion` if a scalar fill's type is incompatible). ASSERT `mask+88 == 0`. The pybind `s3d3_tt_is_zero_s_copy_cast_predicated_opcode` names exactly this shared-struct relationship.

### 3.3 CopyPredicatedScalar (`0xE8` = 232) — 3-D `d3_cp` band

Guard: `inst+0x140 == 0` and `src.isScalar()` (the true-lane is a scalar immediate). Opcode `0xE8`. dst & mask are the two `TENSOR3D` APs; there is **no src AP** — the scalar rides `+0x2B`.

| Offset | W | Field | Source | pybind check | Conf |
|---|---|---|---|---|---|
| `+0x10` | 16 | dst access pattern | `assignAccess<TENSOR3D>(out)` | — | CERTAIN (`lea [r15+0x10] @0x125fe53`) |
| `+0x30` | 16 | mask access pattern | `assignAccess<TENSOR3D>(arg0)` | — | CERTAIN (`lea [r15+0x30] @0x125fe61`) |
| `+0x20` | 1 | mask dtype | `sub_120E650(mask.Dtype)` | `d3_cp_pred_scalar..` | CERTAIN (`[r15+0x20] @0x125fd4e`) |
| `+0x21` | 1 | `useFalseFill` flag | `*(u8)(inst+0x118)` | — | CERTAIN (`[r15+0x21] @0x125fcf3`) |
| `+0x22` | 1 | `num_active_partitions` | `out.firstAPPair.num` | — | HIGH (`[r15+0x22] @0x125fd0a`) |
| `+0x29` | 1 | src/scalar dtype | `sub_120E650(src.Dtype)` | `d3_cp_pred_scalar..` | CERTAIN (`[r15+0x29] @0x125fd17`) |
| `+0x2A` | 1 | out dtype | `sub_120E650(out.Dtype)` | `d3_cp_pred_scalar..` | CERTAIN (`[r15+0x2a] @0x125fd2a`) |
| `+0x2B` | 1+ | scalar true-value immediate | `sub_12051E0` `std::visit(src)` | — | CERTAIN (`lea [r15+0x2b] @0x125fd3d`) |

VARIANT GUARD: `inst+0x138 == 0`; ASSERT `out+88 == 0`. `d3_cp_pred_scalar_reserved_zero` is the L2 reserved-zero check on this struct. ENGINE DVE(5).

> **NOTE — why one body, four opcodes.** The `lower_select` pass splits `Select(IT51)` into an unconditional `GenericCopy` (onFalse seed) + an `InstCopyPredicated` (IT52). The IT52 node's operand shape — scalar vs tensor src, dtype match, presence of a reduce op at `+0x140` — selects which of the four wire opcodes the single visitor emits. `visitInstCopyPredicated` is the universal masked-write encoder; the opcode is chosen per operand profile. That is why one visitor address serves four mnemonics.

---

## 4. `visitInstTensorCopy @0x1237f50` (IT23, DVE) — `0x46`/`0x47`, the unconditional copy

The plain on-engine copy/cast — **not** predicated. Operands: `arg0 = getArgument(0)` (SRC), `out = getOutput(0)` (DST).

**Opcode byte** (the cast-bit trick): `opcode = (src.Dtype@+0x30 != dst.Dtype@+0x30) + 0x46`, i.e. `0x46` (same-dtype plain copy) / `0x47` (casting copy). Disassembled: `cmp eax,[r13+0x30] @0x1237f92`; `setne r15b @0x1237f96`; `add r15d,0x46 @0x1237f9a`.

This is the **Family-C** 4-D copy band (bundle base register `rbx`):

| Offset | W | Field | Source | pybind | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x46`/`0x47` | `(srcDty != dstDty) + 0x46` | `s_copy_opcode` | CERTAIN |
| `+0x0C` | 20 | SRC mem-pattern | `assignAccess<TENSOR4D>(arg0)` | — | CERTAIN (`lea [rbx+0xC]`) |
| `+0x20` | 1 | SRC dtype (wire tag) | `sub_120E650(getReinterpretedCopyDtype(src.Dtype))` | `s3d3_copy..` | CERTAIN (`[rbx+0x20]`) |
| `+0x21` | 1 | DST dtype (wire tag) | `sub_120E650(getReinterpretedCopyDtype(dst.Dtype))` | `s3d3_copy..` | CERTAIN (`[rbx+0x21]`) |
| `+0x22` | 1 | num channels / partition count | `arg0.MemLoc.partn_count` (`*(*(arg0+0x50)+8)`) | `copy_s_..active_channels` | CERTAIN (`[rbx+0x22]`, guarded `arg0+0x58 != 0`) |
| `+0x25` | 1 | reserved/flag = 0 | const 0 | — | CERTAIN (`[rbx+0x25]`) |
| `+0x2C` | 20 | DST mem-pattern | `assignAccess<TENSOR4D>(out)` | — | CERTAIN (`lea [rbx+0x2C]`) |

> **QUIRK — the cast bit reads RAW dtype, the wire tag is REINTERPRETED.** The `0x46`→`0x47` decision uses the **raw** src/dst dtype, but the bytes at `+0x20`/`+0x21` carry the **reinterpreted** dtype (`getReinterpretedCopyDtype`, libBIR extern `@0x5ec620`). So a same-bitwidth reinterpret keeps opcode `0x46` yet can write a different wire-dtype tag.

DYNAMIC SRC/DST: no own visitor; `ExpandInstLate rewriteTensorCopyDynamicSrcDst @0xcd5c60` lowers to TensorCopy + RegisterAP; the offset reg is encoded by `assignStartAddr<ADDR4> @0x1172e10` (RegId in the addr-word low byte + bit-31 at byte+3). ASSERT `arg0+0x58 == 0`. ENGINE DVE.

> **NOTE — three "copy-cast" forms, do not conflate.** TensorCopy (`0x46`/`0x47`, §4) = unconditional copy, cast via `+1`, on the `s_copy`/`s3d3_copy` **TENSOR4D** struct with src/dst dtype at `+0x20`/`+0x21`. CastPredicated (`0x99`, §3.2) = masked cast copy. CopyPredicated (`0x72`, §3.2) = masked same-dtype copy. The two predicated forms ride the `s3d3_tt` **TENSOR3D** struct with the `+0x0C` dtype-pair — different band, same cast-bit philosophy.

---

## 5. `visitInstMemset @0x125b320` (IT10, DVE) — `0x49`/`0x4D`, `d3_memset`

Single operand `out = getOutput(0)`. `MemsetMode` at `inst+0xF0` (DWORD): `0 = Const → 0x49`, `1 = Random → 0x4D` (Xorwow RNG), `≥ 2 → reportError "has unimplemented Memset Mode"`. Opcode immediates: `mov ebx,0x4d @0x125b379`, `mov ebx,0x49 @0x125b530`.

| Offset | W | Field | Source | pybind | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x49`/`0x4D` | `mode@inst+0xF0 → setupHeader` | `memset_opcode` | CERTAIN |
| `+0x1C` | 4 | `dst_element_count` (DWORD) | `getNumElementsPerPartition(out)` (`sub_1250E50`) | `instr.dst_element_count` | CERTAIN (`lea [r13+0x1c] @0x125b45e`) |
| `+0x20` | 1 | dtype (wire tag) | `sub_120E650(getReinterpretedCopyDtype())` | — | CERTAIN (`[r13+0x20] @0x125b41d`) |
| `+0x22` | 1 | `num_active_partitions` | `out.firstAPPair.num` | — | HIGH (`[r13+0x22] @0x125b435`) |
| `+0x28` | 4 | FILL VALUE (DWORD immediate) | `*(u32)(inst+0xF8)` | `memset_set_value` | CERTAIN (`DWORD [r13+0x28] @0x125b455`) |
| `+0x2C` | 20 | output access pattern | `assignAccess<TENSOR4D>(out)` | — | CERTAIN (`lea [r13+0x2c] @0x125b47d`) |

VARIANT GUARD: `inst+0x118` (fill `MA<uint>` tag) must be 0 else `__throw_bad_variant_access` — the fill must be a resolved scalar immediate. ASSERT `out+88 == 0`. The fill is a 32-bit immediate broadcast across every element of the dst AP; Random-mode RNG state rides the engine context, not a bundle field. ENGINE DVE(5).

---

## 6. `generateMoveShape @0x1213d00` (InstDMA helper, DVE) — `0xB2`, `s_move`

`CoreV2GenImpl::generateMoveShape(bir::InstDMA&, bir::AccessPattern const*)` is a **DMA-family helper**, not a `visitInst*` method and not a tensor-tensor op. It is called from the `InstDMA` codegen path with `a2 = InstDMA` and `a3 = the AccessPattern` whose shape is loaded, and it programs the DVE "shape registers" with a per-dim `(step, num)` pattern. Opcode `0xB2` (178).

GUARD up front: `reportError "Hardware Restriction: Shape registers doesn't work with TensorIndirect AP"` — MoveShape rejects indirect APs. Pattern build: `rank = AP+88` (the `APPair` count); ASSERT `rank ≤ 5` else `EC1124 NeuronAssertion "ImmPattern.size() <= SHAPE_REGISTER_SIZE + 1"`. For each dim `step[i] = DescGenHelper::getAPStepOnHW(...)`, `num[i] = AP.pairs[rank-i].num`; the remaining dims up to 4 pad out with `step = qword_1DF59E0[out.dtype]` (the default contiguous step = element byte size) and `num = 1`.

| Offset | W | Field | Source | pybind | Conf |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0xB2` | `setupHeader` | `move_opcode` | CERTAIN |
| `+0x0C` | 2 | const `0x0100` (256) | literal | — | CERTAIN |
| `+0x0E` | 1 | flag = 1 | literal | — | CERTAIN (`mov BYTE [r14+0xe],0x1 @0x1213f64`) |
| `+0x10` | 1 | mode/replication byte | `*(u8)(a3+44)` | — | CERTAIN |
| `+0x20` | 16 | 4× step DWORD (per-dim HW step) | `getAPStepOnHW` / dtype-size default | — | CERTAIN (`movups [rax+0x20] @0x1213f48`) |
| `+0x30` | 16 | 4× num WORD (per-dim count) | AP pair count, default 1 | — | CERTAIN (`movups [rax+0x30] @0x1213f4c`) |

The const `0x0100` at `+0x0C` (`mov r9d,0x100 @0x1213f5e`) and flag `1` at `+0x0E` are fixed for every MoveShape bundle. ENGINE DVE. (The `+0x10` byte = `AP+44` = a per-AP mode/replication flag carried into the shape register.)

---

## 7. Select (IT51) + TensorDequantize — no machine emitter (negative results)

**Select (IT51)** has **no** `CoreV{2,3,4}GenImpl::visitInstSelect` — the symbol exists in none of the three. `out = preds ? onTrue : onFalse` is realised by `lower_select`: `Select(IT51) → InstGenericCopy(out←onFalse seed) + InstCopyPredicated(IT52)`. The IT52 leaf is §3's visitor, whose operand profile picks the wire opcode: scalar onTrue → `0xE8`; tensor same-dtype → `0x72`; tensor cast → `0x99`; reduce-op present → `0xEA`. A `Select` never reaches a bundle — it would be a hard codegen error the schedule prevents. (The only `visitInstSelect` bodies are in `birverifier`, `birsim`, `SimplifyMemset`, the `SplitSelectVisitor` lower pass, and `ConstantPropagate` — none are codegen emitters.)

**TensorDequantize** (ISA opcode `0x7B` = 123) likewise has no `CoreV*` encoder symbol. The dequant is lowered to a scale-multiply / `TensorScalar` chain before CoreV2 codegen, so there is no leaf bundle emitter; that lowering rationale is **INFERRED** from the absence plus the surviving scale-multiply path.

---

## Evidence ledger

Direct store, compare or opcode read byte-exact from the disassembly:
- All 13 opcode bytes pinned: TT `add 0x51 @0x1235742`, GPSIMD `0x8A @0x1235e3e`/`0xC4`/`0xC5`; CopyPred `0x72 @0x125ecb7`, `0x99 @0x125ecc3`, `0xE8 @0x125fca8`, `0xEA @0x125ef1d`; TensorCopy `setne;add 0x46 @0x1237f9a`; Memset `0x49 @0x125b530`/`0x4D @0x125b379`; MoveShape `0xB2` + flag `0x1 @0x1213f64` + const `0x100 @0x1213f5e`.
- TT field stores `+0x0C`/`+0x0D`/`+0x0E`/`+0x0F` (`@0x1235a40..0x1235abe`), three `TENSOR3D` lea slots.
- SelectReduce `+0x18`/`+0x30` lea, `+0x24`/`+0x25`/`+0x26`/`+0x27`/`+0x28`/`+0x29`/`+0x2B`/`+0x2C` (`@0x125f944..0x125fb8d`); CopyPred/CastPred `+0x0C`/`+0x0D`/`+0x0F` + lea `+0x10`/`+0x20`/`+0x30` (`@0x125f3bc..0x125f448`); CopyPredScalar `+0x10`/`+0x20`/`+0x21`/`+0x22`/`+0x29`/`+0x2A`/`+0x2B`/`+0x30` (`@0x125fcf3..0x125fe61`).
- Memset `+0x1C`/`+0x20`/`+0x22`/`+0x28`/`+0x2C`; TensorCopy `+0x20`/`+0x21`/`+0x22` (decompile-cross-checked).
- Negative results: zero `CoreV2` `visitInstSelect`/`CastPredicated`/`SelectReduce`/`TensorDequantize` symbols (`nm` sweep).
- LUTs: `byte_1DF5760` and `qword_1DF59E0` dumped byte-exact from `.rodata`.

Established by validator / pybind cross-reference: the wire struct tags `s3d3_tt`, `s3d3_copy`, `d3_cp`, `d3_memset`, `s_move` and the check names `s3d3_tt_dtype`/`s3d3_tt_valid_op`/`s3d3_tt_src_element_cnt_check`/`s3d3_tt_is_zero_s_copy_cast_predicated_opcode`/`s3d3_copy_pred_src0_dtype`/`s3d3_copy_cast_pred_src1_dst_dty`/`d3_cp_pred_scalar_reserved_zero`/`s_valid_select_reduce_dtypes`/`s_zero_accum_cmd`/`memset_opcode`/`move_opcode` are all present in the `.so` string table and name-matched to offsets; the `num_active_partitions` slot (TT `+0x0F`, predicated `+0x0F`/`+0x2A`/`+0x22`, Memset `+0x22`) = `firstAPPair.num` via `AP+80[8]`; the CoreV3 extended int32 band; the TensorDequantize/Select upstream-lowering rationale.

**[INFERRED]** — the `0xEA` SelectReduce `+0x24` bypass-vs-`+0x25` reduce-op semantic (the reduction-axis selection rides the AP, not a band field); the per-field pybind *member* names (vs the check-label names dumped) — they abut in the string table and are joined via the encoder's own device-keys + check-label prefixes, not a clean `def_readwrite` dump. The src AP slot in the SelectReduce 2-D band is register-packed (no direct `lea`); inferred as the third 2-D `assignAccess`.

**OPEN / cross-ref:** sub-bit field widths *within* the `TENSORxD` descriptor interiors (handled by `assignAccess` / `setupSync*`, N-strand common code — see [2.4 TENSOR4D / MEM_PATTERN4D](tensor4d-mempattern4d.md)); the exact `TensorDequantize` (`0x7B`) emitter location upstream.

## Cross-References

- [2.1 — The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — Family B (the 3-slot `+0x10..+0x3F` shape, control band squeezed into `+0x0C..+0x0F` *before* the slots); the shared lifecycle and `setupHeader` convention.
- [2.10 — PE Matmul Encoding](pe-matmul-encoding.md) — the sibling Family-A encoding page; same lifecycle, same `setupHeader`/`assignAccess`/ISA-check skeleton.
- [2.12 — Pool / TensorReduce / Reciprocal / Iota Encoding](pool-reduce-encoding.md) — the Family-C `+0x20..+0x2F` control band (TensorCopy and Memset share that 4-D band shape).
- [9.3 — CastToNewDType](../numerics/) — the dtype reinterpret/cast semantics behind the `0x46`/`0x47` and `0x72`/`0x99` cast-bit split and `getReinterpretedCopyDtype`.
- Part 7 (`../bir/`) — codegen tensor-tensor (I06): the front-end pass that fixes `AluOpType` (`inst+0xF0`), the predicate/reduce op (`inst+0x140`) and the engine (`inst+0x90`) long before these encoders read them; and `lower_select`, which produces the `GenericCopy + CopyPredicated` pair.
- [2.23 — ISA Enum Ordinals](isa-enum-ordinals.md) (planned) — the opcode-ordinal table that pins 65/81/138/142/234/114/153/232/70/71/73/77/178.
