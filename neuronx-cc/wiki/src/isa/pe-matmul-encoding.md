# PE Matmul Encoding — Dense / Sparse / MX & Quantize

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The encoders, look-up tables and the pybind assert roster live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331…`, md5 `1d93972b…`); the BIR enums (`NEURON_ISA_TPB_DTYPE`, `bir::MatmultPerfMode`) live in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of every instruction the **PE array** (the 128×128 systolic multiply grid, [1.08](../arch/pe-engine.md)) emits onto the wire. There are **nine** distinct 64-byte bundles, produced by three hardware generations of code generator:

- **CoreV2 (Sunda, arch 20)** — the dense baseline: `LoadStationary` (opcode 1) + `Matmul` (opcode 2).
- **CoreV3 (Cayman, arch 30)** — the dense overrides *plus* the gen3-only **structured-sparsity** path: `LoadTagsSparse` (opcode 6) + `MatmultSparse` (opcode 7).
- **CoreV4 (Mariana, arch 40)** — the **MX microscaling** path: `LdWeightMx` (opcode `0x1009`) + `MatmultMx` (opcode `0x100A`), and the DVE-engine companion `QuantizeMx` (opcode `0x10E3`).

Every bundle is a `std::array<unsigned char, 64>`: emplaced into a `SmallVector`, pxor-zeroed in full (so any byte the encoder does not write is a hard `0x00`), header-stamped, field-filled, validated by the ISA checker, and `fwrite(buf, 1, 0x40, bin)`-ed. The arch map is the one fixed in [1.05 codename taxonomy](../arch/codename-taxonomy.md): `CoreV2GenImpl`=20, `CoreV3GenImpl`=30, `CoreV4GenImpl`=40, dispatched from the `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` in `initCodegen`.

The bar for this page: a reader can **byte-encode any PE matmul instruction by hand**, knowing for each control byte its offset, width, semantic, the value source, the `<op>_info.so` / pybind assert string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence tag (CONFIRMED = exact store disassembled; STRONG = LUT/predicate cross-checked; INFERRED = zero-init implied, no direct store; SPECULATIVE). The control bands are recovered from the encoder bodies in `libwalrus.so`; bit positions are pinned against the literal shift/mask constants in the disassembly, never inferred. Where a bit has no recovered name, it is tagged INFERRED/unnamed — no field name is fabricated.

## At a glance

The whole family shares one skeleton — bytes `0x00..0x03` are the header, `0x10`/`0x30` carry the two access-pattern sub-bands ([2.1 the bundle](instruction-bundle.md), Family A), and `0x20..0x2F` is the instruction-specific **control band** that this page maps.

| Opcode | Generator (libwalrus) | Engine | Wire struct | Operand band(s) |
|---|---|---|---|---|
| `1` LoadStationary | `CoreV2/3GenImpl::generateLoadWeights` `@0x1258500` / `@0x1361ad0` | PE (3) | `…_S3_LW_STRUCT` (`s3_lw_*`) | weights AP @`+0x10` |
| `2` Matmul (dense) | `CoreV2/3GenImpl::generateMatMul` `@0x1248650` / `@0x13643d0` | PE (3) | `…_S3D3_MM_STRUCT` (`s3d3_mm_*`) | ifmap @`+0x10`, PSUM @`+0x30` |
| `6` LoadTagsSparse | `CoreV3GenImpl::generateLoadTagsSparse` `@0x135e960` | PE (3) | `s3_lw_*` (tags) | tags AP @`+0x10` |
| `7` MatmultSparse | `CoreV3GenImpl::generateMatMulSparse` `@0x135d150` | PE (3) | `s3d3_mm_*` (4-D ifmap) | ifmap `TENSOR4D` @`+0x10`, PSUM @`+0x30` |
| `0x1009` LdWeightMx | `CoreV4GenImpl::generateLdweightMx` `@0x143e350` | PE (3) | MX weight bundle | MXMEM_PATTERN1D @`+0x10` |
| `0x100A` MatmultMx | `CoreV4GenImpl::generateMatmultMx` `@0x143ebd0` | PE (3) | MX matmul bundle | MXMEM @`+0x10`, PSUM 3D @`+0x30` |
| `0x10E3` QuantizeMx | `CoreV4GenImpl::visitInstQuantizeMx` `@0x143dc60` | DVE (5) | `…_S3DMX1_QUANT_STRUCT` | src 3D @`+0x10`, MX out @`+0x30` |

**Header skeleton (all nine opcodes), from `setupHeader` (`@0x1172120`=V2, `@0x1369280`=V3, `@0x143f440`=V4; byte-identical bodies):**

```
 byte +0x00  opcode        (low byte of the opcode word)
 byte +0x01  inst_word_len = 0x10  (16 dwords = 64 bytes)   [QuantizeMx |= saturate bit0]
 byte +0x02..+0x03  reserved = 0x0000
```

The header is written either by a virtual call to `setupHeader`, or — in every CoreV4 generate arm — by an inlined devirtualised `mov WORD[base], <opcode>` after the body checks that `*(*this+0x48)` still points at the concrete `setupHeader` (identical wire result). So the 16-bit word at `bundle[0:2]` is `(0x10<<8) | opcode`, little-endian: `0x1002` (dense MM), `0x1009`/`0x100A`/`0x10E3` (MX). CONFIRMED — `setupHeader @0x143f440` disassembled below.

```asm
143f440:  movzx eax, BYTE [rdx]      ; al = src[0] = opcode byte
143f443:  mov   BYTE [rsi+1], 0x10   ; inst_word_len
143f447:  mov   BYTE [rsi],   al     ; opcode
143f449:  xor   eax, eax
143f44b:  mov   WORD [rsi+2], ax     ; reserved
143f44f:  ret
```

> **NOTE — `inst_word_len = 0x10` is hard-pinned for the whole TPB compute ISA.** Sixteen dwords is exactly 64 bytes; this is why every `visitInst*` emplaces a `std::array<uchar,64>` and `fwrite`s `0x40`. The byte is not a length the encoder computes — it is a constant store.

---

## The bundle lifecycle (shared by all nine)

Every generator runs the same five-step cycle; the `CodeGenMode` at `this+0x270` selects the sink:

1. **Emplace + zero.** `SmallVectorImpl<std::array<u8,64>>::emplace_back`, then four `movups xmm` zero-stores blanket `[base..base+0x40]`. *Consequence: every reserved/unwritten byte reads `0x00`.* (CoreV2 `@0x12487d7`; CoreV4 `@…` 4× `movups xmm1`.)
2. **Header.** `setupHeader` (or the inlined word store).
3. **Access patterns.** `assignAccess<TENSOR3D>` / `<TENSOR4D>` into `+0x10` (source/ifmap) and `+0x30` (dest/PSUM); MX uses `assignAccessForMX<MXMEM_PATTERN1D>` ([2.6 MX operand addressing](mxmem-pattern1d.md)).
4. **Control band.** the per-opcode field fills mapped below.
5. **ISA check + emit.** `runISACheck*` (the `d3_mm_*`/`mxmem1d_valid` asserts) `@0x1249678`, then `fwrite(buf,1,0x40,findBin(I))`.

`CodeGenMode` arms (CONFIRMED for CoreV4 `@0x143e070/848/f01b`): **1 = GENERATE_ISACODE** (fill + `fwrite`); **2 = RUN_ISA_CHECKS** (build the *byte-identical* bundle on a stack frame, feed the checker, no `fwrite`); **0 = COLLECT_OPCODES** (push the opcode int into a `DenseMap` census, emit nothing). The field map below is shared between modes 1 and 2 — the only difference is the destination.

---

## Dense — `LoadStationary` (opcode 1) encoding

Stages the stationary weight tile into the PE weight latches. Wire struct `NEURON_ISA_TPB_S3_LW_STRUCT` (`s3_lw_*` field family). CoreV2 `generateLoadWeights @0x1258500` (bundle base in `r13`); CoreV3 override `@0x1361ad0`. Field offsets are identical to dense `Matmul`; only the opcode and a transpose marker differ.

| Off | W | Field | pybind / `s3_lw_*` name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `1` | `s3_lw_opcode` | setupHeader src=1 | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hdr constant | hdr | CONFIRMED |
| `+0x0C` | 1 | transpose/xbus marker | (`s3_lw_m`/transpose) | **V3 only**: `=2` (arch≥40 bg-transpose) | V3 `+0x0C` | CONFIRMED |
| `+0x10` | 16 | weights AP (`TENSOR3D`) | `s3_lw_valid_src_partition` | `assignAccess<TENSOR3D>`; `+0x16=0`, `+0x1c=0x00010001` | `0x12593f7`/`0x12588a8` | CONFIRMED |
| `+0x13` | 1 | transpose flag = `0x80` | (V2 transpose) | **V2 only**: `[+0x13]=0x80` | `0x125936c` | CONFIRMED |
| `+0x20` | 1 | in-dtype (wire tag) | `lw_dtype` | LUT `byte_1DF5760[dtype]` (V2) / `sub_1348870` (V3) | `0x12586b9` | CONFIRMED |
| `+0x20` | 2 | fp32r WORD override `0x020A` | `lw_dtype`+marker | when `dtype==float32r`: `WORD[+0x20]=0x020A` | `0x1258f16`/`0x1361ca4` | CONFIRMED |
| `+0x21` | 1 | perf/opcode-variant byte | (`s3_lw` perf) | `0`/`2`(load-weights perf)/`bl`; V2 fp32 = `0x80` | `0x12586e2`/`0x125913a`/`0x125952b` | CONFIRMED |
| `+0x23` | 1 | perf-opt dtype | `lw_perf_opt_dtype` | `perfModeToDstDtype(perfMode)` `@0x1203630` | `0x1258a05` | STRONG |
| `+0x24` | 1 | replication lo (tonga) | (`tonga_replication`) | **V2 only**: `[+0x24]=al` | `0x1258a05`/V2 | CONFIRMED |
| `+0x25` | 1 | replication hi (tonga) | (`tonga_replication`) | **V2 only**: `[+0x25]=al` | `0x1258a18` | CONFIRMED |
| `+0x26` | 1 | num_active_rows / wt base | `lw_valid_num_rows` | `*(*(groups[+0x50])+0x8)` = base partition | `0x1258921` | CONFIRMED |
| `+0x27` | 1 | num_active_cols | `lw_valid_num_active_cols` | `getNumActiveCols(srcAP)`; V3 via `runSingleISACheck @0x1352590` | `0x12586f4`/V3 | CONFIRMED |
| `+0x2C` | 1 | row group | `lw_valid_row_group` | `translateToRowGroup(pos, max(rowExt,+0x26))` | `0x125898a` | CONFIRMED |
| `+0x2D` | 1 | col group | `lw_valid_col_group_active_column` | `translateToColGroup(pos, max(colExt,+0x27), force)` | `0x1258973` | CONFIRMED |
| `+0x2E` | 1 | normalized/xbus group | `lw_valid_xbus` | **V2 only**: `=0x10` (collapse switch) | `0x12589c2` | CONFIRMED |
| `+0x22`,`+0x28`..`+0x2A`,`+0x2F` | — | reserved-zero | `lw_reserved_zero` | pxor zero-init; asserted `==0` | — | INFERRED |

> **CORRECTION (supersedes a "row-only at +0x2C" reading).** The CoreV3 LoadStationary writes **both** the row group at `+0x2C` *and* the col group at `+0x2D` (via `translateToColGroup` with a `force` bool), the same split as CoreV2 — it only **drops** the `+0x2E` normalized byte that CoreV2 keeps. Disassembly of the body confirms two separate group stores.

**Annotated encode (CoreV2 `generateLoadWeights`, the load-weights opcode of the shared struct):**

```c
// libwalrus CoreV2GenImpl::generateLoadWeights @0x1258500  (bundle base = r13)
u8 *b = emplace_zeroed_array64();        // pxor-zeroed
setupHeader(b, &opcode_1);               // b[0]=1, b[1]=0x10, b[2:4]=0
assignAccess_TENSOR3D(&b[0x10], weightsAP, /*dst*/false); // src partition base @ +0x10
b[0x13] = 0x80;                          // CoreV2 transpose flag (V3: instead b[0x0C]=2)
b[0x20] = byte_1DF5760[weightsAP.dtype]; // in-dtype wire tag (LUT @0x1df5760)
if (weightsAP.dtype == float32r)         // fp32r two-pass marker
    *(u16*)&b[0x20] = 0x020A;            //   b[0x20]=0x0A(float32), b[0x21]=0x02
b[0x21] = perf_variant;                  // 0 default / 2 load-weights perf / 0x80 fp32
b[0x23] = perfModeToDstDtype(perfMode);  // perf-opt dtype  (helper @0x1203630)
b[0x24] = b[0x25] = replication_code;    // tonga replication pair (V2 only)
b[0x26] = weight_base_partition;         // *(*(I.groups[+0x50])+8)
b[0x27] = getNumActiveCols(weightsAP);   // num active columns
b[0x2C] = translateToRowGroup(numRows, base);   // {1,2,4,8|3,12,32,64|0xF}
b[0x2D] = translateToColGroup(numCols, base, force);
b[0x2E] = 0x10;                          // xbus / fp32r restriction (V2 only)
runISACheck(b); fwrite(b, 1, 0x40, bin);
```

---

## Dense — `Matmul` (opcode 2) encoding

The dense multiply. Wire struct `NEURON_ISA_TPB_S3D3_MM_STRUCT` (`s3d3_mm_*`). CoreV2 `generateMatMul @0x1248650` (base `r15`); CoreV3 override `@0x13643d0` (base `r13`). The struct band layout: header `+0x00`, source/ifmap `TENSOR3D` `+0x10`, control `+0x20`, dest/PSUM `TENSOR3D` `+0x30`.

| Off | W | Field | pybind / `s3d3_mm_*` name | Value source | Store-site (V2) | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | mm_opcode = `2` | `s_s3d3_mm_opcode` | setupHeader src=2 | hdr | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | constant | hdr | CONFIRMED |
| `+0x10` | 16 | ifmap (moving) AP `TENSOR3D` | `d3_mm_valid_src_partition` | `assignAccess<TENSOR3D>(&b[0x10], srcAP)` | `0x1248c39` | CONFIRMED |
| `+0x20` | 1 | in-dtype (ifmap+weights) | `d3_mm_dtype` (in arm) | `byte_1DF5760[srcAP.dtype]` (V2) / `sub_1348870` (V3) | `0x1248872` | CONFIRMED |
| `+0x20` | 2 | fp32r WORD override `0x020A` | `d3_mm_dtype`+marker | when `dtype==float32r`: `WORD[+0x20]=0x020A` | `0x124972d` | CONFIRMED |
| `+0x21` | 1 | opcode-variant / perf-select | `s_s3d3_mm_opcode` + perf | `0`=dense, `2`=load-weights, `3`=perf/transpose | `0x12488a2`/`0x124a6fa`/`0x124a3ea` | CONFIRMED |
| `+0x22` | 1 | reserved-zero | `d3_mm_reserved_z*` | pxor zero-init | — | INFERRED |
| `+0x23` | 1 | dst-dtype (PSUM accumulator) | `d3_mm_dtype` (dst arm) | `perfModeToDstDtype(perfMode)` `@0x1203630` | `0x1248882` | CONFIRMED |
| `+0x24` | 1 | perf-opt dtype | `d3_mm_perf_opt_dtype` | `InstMatmult[+0x2d8]` (perf-opt config byte) | `0x1248c92` | CONFIRMED |
| `+0x25` | 1 | perf-opt src / replication | `d3_mm_perf_opt_src` | same `al` as `+0x24`; `Inst[+0x2f8]` asserted `==0` | `0x1248caa` | CONFIRMED |
| `+0x26` | 1 | weight/PE-row base | `d3_mm_valid_row_*` | `*(*(groups[+0x50])+0x8)` | `0x1248923` | CONFIRMED |
| `+0x27` | 1 | num-cols / num_elements | `d3_mm_num_elements` | `getNumElementsPerPartition(srcAP)/(1+isDoublePixel)` | `0x12488dd` | CONFIRMED |
| `+0x2B` | 1 | accumulate / calc flags | `mm_psum_flags` | init 0; `or 0x1`=CalcStart, `or 0x2`=CalcStop | `0x12493b3`/`0x124963d`/`0x1249664` | CONFIRMED |
| `+0x2C` | 1 | row-group code | `d3_mm_valid_row_*` | `translateToRowGroup(numRows, base)` | `0x124898b` | CONFIRMED |
| `+0x2D` | 1 | col-group code | `d3_mm_valid_col_group_active_col` | `translateToColGroup(numCols, base, bool)` | `0x1248975` | CONFIRMED |
| `+0x2E` | 1 | xbus / fp32r restriction | `d3_mm_valid_xbus` / `d3_mm_fp32r_restrictions` | constant `0x10` (V2); **V3 drops it** | `0x1248c25` | CONFIRMED |
| `+0x30` | 16 | PSUM dst AP `TENSOR3D` | `d3_mm_valid_dst_*` | `assignAccess<TENSOR3D>(&b[0x30], dstAP)` | `0x1248c4e` | CONFIRMED |
| `+0x28`,`+0x29`,`+0x2A`,`+0x2F` | — | reserved-zero | `d3_mm_reserved_z*` / `reserved2_tonga_replication` | pxor zero-init; asserted `==0` | — | INFERRED |

> **CORRECTION — the dst-dtype lives at `+0x23`, not `+0x28`.** No `+0x28` store exists in either CoreV2 encoder; `+0x28` is a stale offset from an older struct revision. The on-this-build dst-dtype is `byte +0x23` (`mov [r15+0x23],al @0x1248882`). On `MatmultMx` (`+0x28`, below) the PSUM-dst-dtype genuinely *is* at `+0x28` — different struct.

### The accumulate byte `+0x2B` — the PSUM start/stop/accu contract

The calc bits encode how the matmul interacts with the PSUM accumulation bank. CoreV2 writes them inline; CoreV3 factors them into the template `generateMatMulAccumulateFlag<S3D3_MM> @0x1428630` and gates on `module[+0xAC] <= 40` (arch>40=CoreV5 skips them).

```
 bit0 (0x01) = START      = zero-then-write the PSUM bank   (getCalcStart)
 bit1 (0x02) = STOP       = drain/read the bank             (getCalcStop)
 bit2 (0x04) = ACCUMULATE = add into the bank               (getCalcAccu)
```

The byte is validated by `checkAccumulationFlag` (CoreV2 `@0x1178f30`). The `{START, Continue, Stop, Accu, Auto}` set of `InstMatmultBase::setCalc*` collapses onto these three bits; the two-pass FP32 split selects START on pass-1 and STOP/ACCU on pass-2. CONFIRMED — the three `or` bit literals (`0x1`,`0x2`,`0x4`) and `getCalc{Start,Stop,Accu}` callees `@0x5f20a0/0x5fb350/0x5f9f40`.

### CoreV2 → CoreV3 control-band deltas

The Cayman PE array is **not wider** (still 128×128, four 32-lane quadrants; the row/col LUTs are byte-identical — see below). The override is purely encoding/legality:

- **dtype legality:** CoreV3 `sub_1348870` (SWITCH, jump-table `@0x1df9574`) **rejects** the three ×4-packed dtypes `{2=fp4_e2m1fn_x4, 8=fp8_e4m3fn_x4, 9=fp8_e5m2_x4}` (falls through `cmp edi,0x13; ja → NeuronAssertion`). CoreV2's flat LUT silently maps them to `{0x05,0x0e,0x0f}`. The other 17 tags are byte-identical, so the wire **encoding** is shared; only the **legality gate** diverges (Cayman PE matmul does not consume ×4-packed operands directly — that is the gen4 MX path). CONFIRMED — full jump-table decode + per-target `mov eax,imm; ret`.
- **row/col group packing:** CoreV2 writes two separate bytes (`+0x2C` row, `+0x2D` col); CoreV3 packs them into **one WORD at `+0x2C`** = `(colGroup<<8 | rowGroup)` via `getCoreV3MatmulRowColGroup @0x1096110`, but only when module arch-order ≤ 40 — on arch>40 the boxed setter is NULLed and the group bytes are skipped. STRONG (the packed value path is via an arch-order boxed setter).
- **`+0x2E` dropped:** CoreV3 takes the raw `{1,2,4,8|3,12,32,64|15}` codes; the V2 normalized-collapse byte is gone.
- **`+0x24/+0x25` replication pair:** written on V2, **not** on V3 (Cayman routes replication legality through a gate; the bytes stay 0).
- **signature:** V2 `generateMatMul` takes an explicit `bir::MatmultPerfMode`; V3 takes a plain `bool` and reads perf mode from `Inst[+180]`.

---

## Dense look-up tables (shared V2/V3)

**Row/col group geometry** — `translateToRowGroup` (V2 free fn `@0x6043e0`; V3 member `@0x1346100`), `translateToColGroup` (`@0x6264f0` / `@0x13460b0`). Both read two `.rodata` LUTs, xxd-verified this pass:

```
LUT32 @0x1DD33F0 = 01 00 00 00  02 00 00 00  04 00 00 00  08 00 00 00   → {1,2,4,8}
LUT64 @0x1DBEDA0 = 03 00 00 00  0c 00 00 00  20 00 00 00  40 00 00 00   → {3,12,32,64}

  if (size <= 0x20):  rg = LUT32[(idx>>5)&7]   → {1,2,4,8}
  elif (size <= 0x40): rg = LUT64[(idx>>6)&3]  → {3,12,32,64}
  else:                rg = 0x0F               (full 128-row/col)
```

`translateToColGroup` adds a `force` bool that returns `0x0F` on transpose / fast-FP32 / background-transpose. The thresholds `0x20`/`0x40` are the 32/64-row tile boundaries. CONFIRMED — both LUTs xxd-verified at the addresses above.

**dtype → ISA wire tag** — CoreV2 flat LUT `byte_1DF5760` (xxd-verified `03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 …`); CoreV3 `sub_1348870` SWITCH (same tags except the three ×4 rejects). Indexed by BIR `Dtype` ordinal `0..19`. The fp32r path additionally writes `WORD[+0x20]=0x020A` (`=522`): `byte+0x20=0x0A` (float32 tag) + `byte+0x21=0x02` (two-pass marker), requiring `weights.Dtype==float32r` too.

| BIR ord | dtype | V2 `byte_1DF5760` | V3 `sub_1348870` | V4 `byte_1DFBAD0` |
|---|---|---|---|---|
| 0 | uint8 | `0x03` | `0x03` | `0x03` |
| 1 | int8 | `0x02` | `0x02` | `0x02` |
| **2** | **float4_e2m1fn_x4** | `0x05` | ⛔ REJECT | **`0x10`** |
| 3 | float8e3 | `0x0d` | `0x0d` | `0x0d` |
| 4 / 5 | float8e4 / e4m3fn | `0x0e` | `0x0e` | `0x0e` |
| 6 | float8_e8m0fnu (E8M0) | `0x03` | `0x03` | `0x03` |
| 7 | float8e5 | `0x0f` | `0x0f` | `0x0f` |
| **8** | **float8_e4m3fn_x4** | `0x0e` | ⛔ REJECT | `0x0e` |
| **9** | **float8_e5m2_x4** | `0x0f` | ⛔ REJECT | `0x0f` |
| 10 / 11 | uint16 / int16 | `0x05` / `0x04` | same | same |
| 12 / 13 | bfloat16 / float16 | `0x06` / `0x07` | same | same |
| 14 / 15 | uint32 / int32 | `0x09` / `0x08` | same | same |
| 16 / 17 | float32 / float32r | `0x0a` / `0x0b` | same | same |
| 18 / 19 | uint64 / int64 | `0x01` / `0x0c` | same | same |

> **QUIRK — the fp4 wire tag moved on CoreV4.** `byte_1DFBAD0[2] = 0x10` on Mariana vs `byte_1DF5760[2] = 0x05` on Sunda. The MX decoder `mxmem1d_valid` keys on this: a DATA ADDR4 validates as 4-byte DTYPE 9, or as 2-byte DTYPE 5 *iff* the descriptor's DTYPE arg `== 0x10` (the FP4-x4 2-byte container). Both bytes verified by xxd this pass.

---

## Sparse — `LoadTagsSparse` (opcode 6) encoding

CoreV3 gen3-NEW: there is **no** CoreV2/CoreV4 sparse symbol. `visitInstMatmultSparse @0x1363b40` is an orchestrator (emits no bundle of its own); it binds `getArgument(0)`=weights, `getArgument(1)`=ifmap, **`getArgument(2)`=the int16 sparsity-tag/index tensor**, `getOutput(0)`=PSUM, then dispatches:

- **not-FP32** (single pass): `padIfMapForMMSparse` → `generateLoadTagsSparse(0,0)` → `generateLoadWeights(0,0)` (shared dense LW) → `generateMatMulSparse(0,0)`.
- **FP32** (double pass): tags + two `{LoadWeights, MatMulSparse}` passes carrying the low/high fp32-half flags `(dl,hi)`.

`generateLoadTagsSparse @0x135e960` (base `r13`) emits the **tags/index bundle** — the per-row column-selection indices.

| Off | W | Field | name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `6` | (LoadTags) | setupHeader src=6 | `0x135eac3` | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | constant | hdr | CONFIRMED |
| `+0x10` | 4 | tags DWORD addr/bank | (`s3_lw` src) | `vtable+0x20(this,…)` → `[+0x10]` | `0x135eb4c` | CONFIRMED |
| `+0x14` | 4 | = `0x00000001` (single col) | const | const | `0x135eb50` | CONFIRMED |
| `+0x18` | 2 | = `0` | const | const | `0x135eb36` | CONFIRMED |
| `+0x1A` | 2 | num-elements | (tags NEPP) | `getNumElementsPerPartition(tags)` | `0x135eb2d` | CONFIRMED |
| `+0x1C` | 4 | = `0x00010001` (stride pair) | const | const | `0x135eb58` | CONFIRMED |
| `+0x20` | 1 | tags dtype (= `0x04`, int16) | `s3_lw_dtype` | `sub_1348870(tagsAP.dtype)` | `0x135eaf9` | CONFIRMED |
| `+0x21` | 1 | col_grp (LUT-remapped) | `col_grp` | jump-table `@0x1df97c0` from row-group | `0x135ebf9` | CONFIRMED |
| `+0x26` | 1 | tile-size | (tile) | `max(getLegalTileSize, [rbx+0x50]+8)` | `0x135eb8d` | CONFIRMED |
| `+0x2C` | 1 | row group (sparsity ratio) | `s3_lw_valid_row_group` | `translateToRowGroup(pos, max(tile,+0x26))` | `0x135ebcd` | CONFIRMED |
| `+0x2D` | 1 | ISA-check aux | `col_grp` (0xF bound) | `runSingleISACheck @0x1350810` | — | STRONG |

### The row-group → `col_grp` remap (`+0x21` jump-table `@0x1df97c0`)

After `[+0x2C]=rowGroup`, a `cmp al,0xF; ja → llvm_unreachable` guards a `movsxd rax,DWORD[0x1df97c0 + rg*4]; jmp rax` dispatch that sets `[+0x21]`:

```
 row-group  1, 3, 15 → col_grp +0x21 = 0
 row-group  2        → col_grp +0x21 = 4
 row-group  4, 12    → col_grp +0x21 = 8
 row-group  8        → col_grp +0x21 = 0xC
 row-group  0,5,6,7,9,10,11,13,14 → llvm_unreachable (invalid structured-sparsity rg)
```

`col_grp(+0x21)` is the active-column-group code **derived** from the row-group density ratio — a log2-style `{pow-of-2 rg → 0/4/8/12}` remap. The jump table is 8 rel32 entries, xxd-verified `@0x1df97c0` this pass (`c0 56 56 ff  30 54 56 ff  20 57 56 ff  30 54 56 ff …`). CONFIRMED.

---

## Sparse — `MatmultSparse` (opcode 7) encoding

`generateMatMulSparse @0x135d150` (base `r14`). The structured-sparse multiply. The wire delta vs dense `Matmul` is three artifacts, not a standalone "sparse mode" bit: (1) the separate opcode-6 tags bundle above; (2) the ifmap bound as **`TENSOR4D`** (4-D, the 4th dim carries the sparse-column substructure, added by `padIfMapForMMSparse @0x1345d30`); (3) the density row-group `+0x2C` + derived `col_grp +0x21`.

| Off | W | Field | name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | mm_opcode = `7` | `s_s3d3_mm_opcode` | setupHeader src=7 | `0x135d2d6` | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | constant | hdr | CONFIRMED |
| `+0x10` | 16 | ifmap AP **`TENSOR4D`** | `d3_mm_valid_src_partition` | `assignAccess<TENSOR4D>` | `0x135d40b` | CONFIRMED |
| `+0x20` | 1 | in-dtype | `s3d3_mm_dtype` | `sub_1348870(ifmapAP.dtype)`; fp32r → `0x020A` | `0x135d337`/`0x135d6f8` | CONFIRMED |
| `+0x21` | 1 | perf byte = `3·(dtype==FP32)` | (perf) | `cmp eax,0x10; sete al; lea eax,[rax+rax*2]` → `3` if FP32 else `0` | `0x135d350` | CONFIRMED |
| `+0x26` | 1 | tile-size | (tile) | `[rbx+0x50]+8` | `0x135d3ac` | CONFIRMED |
| `+0x27` | 1 | num_active_cols | `s3d3_mm` `num_active_cols` | `getNumElementsPerPartition(weights)`; `runSingleISACheck @0x134ff80` | `0x135d36a` | CONFIRMED |
| `+0x2B` | 1 | control (fp32-pass bits) | (pass flags) | init 0; `or 0x1`(low), `or 0x2`(high) | `0x135d472`/`0x135e1b0`/`0x135e1a0` | CONFIRMED |
| `+0x2C` | 1 | row group (density ratio) | `s3d3_mm_valid_row` | `translateToRowGroup(pos, max(tile,+0x26))` | `0x135d404` | CONFIRMED |
| `+0x2D` | 1 | ISA-check aux | `col_grp` (0xF bound) | `runSingleISACheck @0x1350810` | — | STRONG |
| `+0x30` | 16 | PSUM dst AP `TENSOR3D` | `d3_mm_valid_dst_*` | `assignAccess<TENSOR3D>` | `0x135d420` | CONFIRMED |

> **GOTCHA — sparse `+0x2B` is NOT the calc/accumulate byte.** Dense `Matmul` uses `+0x2B` for `{START,STOP,ACCU}` (computed). Sparse `MatmultSparse` reuses the *same offset* but stamps the **FP32 low/high-pass flags** (`or 0x1`/`or 0x2`) — there is no `generateMatMulAccumulateFlag` call here. The structured-sparsity matmul is always a fresh-accumulate single drain. Two FP32 passes ⇒ two `fwrite` sites (`@0x135dc00` / `@0x135e1df`).

The `compress_ratio` (BIR `InstMatmultSparse+0x328`, a `variant<int,QuasiAffineExpr>`) is consumed **upstream** into `tileSize → translateToRowGroup` — it is *not* a standalone bundle field. The on-wire density is the `+0x2C` row-group code.

---

## MX — `LdWeightMx` (opcode `0x1009`) encoding

CoreV4 gen4-only. `visitInstMatmultMx @0x143f410` is a naked 12-instruction dispatcher: it calls `generateLdweightMx` then tail-jumps `generateMatmultMx`, so **one** `InstMatmultMx` always produces the wire stream `[LdWeightMx][MatmultMx]` — no cache, no two-pass, no perf-mode branch. `generateLdweightMx @0x143e350` (base `r14`) stages the ×4-packed weight DATA + per-block E8M0 SCALE.

Operands: `arg1 = getArgument(1)` = weights ×4 DATA; `arg3 = getArgument(3)` = weights E8M0 SCALE.

| Off | W | Field | name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x09` | (LdWeightMx) | `WORD[r14]=0x1009` | `0x143e4d1` | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hi byte of `0x1009` | hdr | CONFIRMED |
| `+0x10` | 16 | MX operand (MXMEM_PATTERN1D) | — | `assignAccessForMX` (weights DATA+SCALE) | `0x143e623` | CONFIRMED |
| `+0x20` | 1 | weights dtype (wire tag) | `d3_mm_dtype` | `byte_1DFBAD0[arg1.Dtype]` via `@0x14347c0` | `0x143e529` | CONFIRMED |
| `+0x26` | 1 | num_partitions (K rows) | `d3_mm_valid_num` | `arg1.Pattern[0].num` | `0x143e5e8` | CONFIRMED |
| `+0x27` | 1 | num_active | `num_active_cols` | `(*arg1.vtbl+0x90)(arg1)` low | (WORD@26) | CONFIRMED |
| `+0x2C` | 1 | row group (PE-row tile) | `d3_mm_valid_row` | `translateToRowGroup(pos, +0x26)` | `0x143e611` | CONFIRMED |
| `+0x2D` | 1 | col group = `0x0F` (full) | `d3_mm_valid_col_group_active_col` | forced full 128-wide column | `0x143e606` | CONFIRMED |
| `+0x21`..`+0x25`,`+0x28`..`+0x2B`,`+0x2E`,`+0x2F` | — | reserved-zero | `d3_mm_reserved_z*` | pxor zero-init | — | CONFIRMED |

> **NOTE — `LdWeightMx` has no accumulate byte, no PSUM-dst, no ifmap.** It *only* stages the weight tile. The exhaustive write list is: `WORD[+0x00]`, `BYTE[+0x20]`, `WORD[+0x26]` (`{num_partitions, num_active}`), `BYTE[+0x2C]`, `BYTE[+0x2D]=0xF`, + MXMEM `@+0x10`. Nothing else is stored.

---

## MX — `MatmultMx` (opcode `0x100A`) encoding

`generateMatmultMx @0x143ebd0` (base `r13`). Feeds the ×4-packed moving/ifmap + its E8M0 scale into the PE against the pre-loaded stationary weights, draining to PSUM.

Operands: `arg0`=ifmap ×4 DATA; `arg1`=weights ×4 DATA (K-rows/num_active only); `arg2`=ifmap E8M0 SCALE; `out0`=PSUM dst.

| Off | W | Field | name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x0A` | (MatmultMx) | `WORD[r13]=0x100A` | `0x143ed5a` | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | — | hi byte of `0x100A` | hdr | CONFIRMED |
| `+0x10` | 16 | MX operand (MXMEM_PATTERN1D) | — | `assignAccessForMX` (ifmap DATA+SCALE) | `0x143eede` | CONFIRMED |
| `+0x20` | 1 | ifmap dtype (wire tag) | `d3_mm_dtype` | `byte_1DFBAD0[arg0.Dtype]` | `0x143edca` | CONFIRMED |
| `+0x26` | 1 | num_partitions (K rows) | `d3_mm_valid_num` | `arg1(weights).Pattern[0].num` | `0x143ee80` | CONFIRMED |
| `+0x27` | 1 | num_active | `num_active_cols` | `(*arg1.vtbl+0x90)(arg1)` low | `0x143eea1` | CONFIRMED |
| `+0x28` | 1 | PSUM-dst dtype (wire tag) | `d3_mm_dtype` (dst) | `byte_1DFBAD0[out0.Dtype]` | `0x143eded` | CONFIRMED |
| `+0x2B` | 1 | accumulate flag | `mm_psum_flags` | **RAW** `Inst+0xF0` (pre-packed MaybeAffine) | `0x143ef1d` | CONFIRMED |
| `+0x2C` | 1 | row group (PE-row tile) | `d3_mm_valid_row` | `translateToRowGroup(pos, +0x26)` | `0x143eecf` | CONFIRMED |
| `+0x2D` | 1 | col group = `0x0F` (full) | `d3_mm_valid_col_group_active_col` | forced full 128-wide column | `0x143eec4` | CONFIRMED |
| `+0x2F` | 1 | psum_zero_region | (psum_zero) | **RAW** `Inst+0x118` (MaybeAffine<u8>) | `0x143ef3b` | CONFIRMED |
| `+0x30` | 16 | PSUM dst AP `MEM_PATTERN3D` | `d3_mm_valid_dst_*` | `assignAccess<3D>(out0)` | `0x143eef3` | CONFIRMED |
| `+0x21`..`+0x25`,`+0x29`,`+0x2A`,`+0x2E` | — | reserved-zero | `d3_mm_reserved_z*` | pxor zero-init | — | CONFIRMED |

The two `MaybeAffine<u8>` fields are emitted with a present-tag guard — a non-affine (immediate) value asserts its present-tag (`Inst+0x110`/`Inst+0x138`) is 0 (else `std::out_of_range` `@0x75927e`), then copies the already-bit-packed byte straight to the bundle:

```asm
143ef14:  movzx eax, BYTE [r12+0xf0]   ; the accumulate byte (raw, pre-packed)
143ef1d:  mov   BYTE [r13+0x2b], al
143ef32:  movzx eax, BYTE [r12+0x118]  ; psum_zero_region
143ef3b:  mov   BYTE [r13+0x2f], al
```

> **CORRECTION — CoreV4 does NOT recompute the accumulate byte.** CoreV2 (inline) and CoreV3 (`generateMatMulAccumulateFlag @0x1428630`) *compute* `+0x2B` from `getCalc{Start,Stop,Accu}` + a two-pass split. CoreV4 has **no** `getCalc*` calls — the bit-packing was done **upstream** by `legalize_mm_accumulation_groups` into `Inst+0xF0`, and the encoder copies it verbatim. The silicon contract is the same: `bit0`=START, `bit1`=STOP, `bit2`=ACCU. There is no arch>40 gate (MX matmul is always arch 40, always writes the byte).

The byte is validated by `checkAccumulationFlag @0x150db50`, which lazily builds (via `__cxa_guard`) a hash-set seeded from `DWORD 0x03020100 + WORD 0x0604` — i.e. the valid set `{0, 1, 2, 3, 4, 6}`:

```asm
150dbea:  mov DWORD [rsp+0x10], 0x03020100   ; {0x00,0x01,0x02,0x03}
150dbd4:  mov eax, 0x604                     ; {0x04,0x06}
```

> **QUIRK — accumulate values 5 and 7 are illegal.** `{0,1,2,3,4,6}` admits START(1), STOP(2), START|STOP(3), ACCU(4), STOP|ACCU(6), and 0 (no-op). The combos `5 = START|ACCU` and `7 = START|STOP|ACCU` are **rejected** — START (zero-the-bank) cannot co-occur with ACCU (add-into-the-bank) in one pass. The DWORD+WORD seed was disassembled this pass.

> **NOTE — MatmultMx has NO perf-mode byte.** The CoreV2 DoubleRow AP-halving and CoreV3 AP dim-swap are absent; `+0x21..+0x25` are all reserved-zero. MX has a single fixed PE-feed mode — the "perf mode" is implicit in the ×4 block geometry.

---

## MX — `QuantizeMx` (opcode `0x10E3`) encoding

`visitInstQuantizeMx @0x143dc60` (base `r13`). Reads a bf16/fp16 source tensor and narrows it to an ×4-packed fp8/fp4 DATA output + a per-block E8M0 SCALE output — the OCP-MXFP microscaling quantize. **Engine = DVE (5)**, not PE; wire struct `NEURON_ISA_TPB_S3DMX1_QUANT`. Note the **role inversion**: the SRC *input* is the `MEM_PATTERN3D` (the thing read) and the `{DATA,SCALE}` *output* pair is the `MXMEM_PATTERN1D` (the thing produced) — the opposite roles to `MatmultMx`.

Operands: `ins[0]`=SRC (bf16/fp16); `outs[0]`=DATA (fp8/fp4-x4); `outs[1]`=SCALE (E8M0 per-block).

| Off | W | Field | name | Value source | Store-site | Tag |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xE3` | (QuantizeMx, =227) | `WORD[r13]=0x10E3` | `0x143ddea` | CONFIRMED |
| `+0x01` | 1 | inst_word_len `0x10` \| sat bit0 | (saturate) | `0x10`; `or 0x1` when `!FP8ConvConfig[0]` | `0x143df3f` | CONFIRMED |
| `+0x04` | 1 | sync WAIT mode | (sync) | `Wait::getMode` via `setupSyncWait @0x143cbe0` | sync | CONFIRMED |
| `+0x05` | 1 | sync WAIT sem-id | (sync) | `SyncRef::getId(wait)` | sync | CONFIRMED |
| `+0x06` | 1 | sync UPDATE mode | (sync) | `Update::getMode` via `setupSyncUpdate @0x143c950` | sync | CONFIRMED |
| `+0x07` | 1 | sync UPDATE sem-id | (sync) | `SyncRef::getId(update)` | sync | CONFIRMED |
| `+0x08` | 4 | sync value/reg | (sync) | GE_IMM value or GE_REG regId | sync | CONFIRMED |
| `+0x10` | 16 | SRC input AP `MEM_PATTERN3D` | — | `assignAccess<3D>(srcAP)` | `0x143df18` | CONFIRMED |
| `+0x21` | 1 | SRC partition count | (src part) | `srcAP.Pattern[0].num` | `0x143df14` | CONFIRMED |
| `+0x22` | 1 | SRC dtype (wire tag) | `d3_mm_dtype` (src) | `byte_1DFBAD0[srcAP.Dtype]` (bf16→`0x06`, fp16→`0x07`) | `0x143de53` | CONFIRMED |
| `+0x23` | 1 | DATA-out dtype (wire tag) | `d3_mm_dtype` (data) | `byte_1DFBAD0[dataAP.Dtype]` (e4m3-x4→`0x0E`, e5m2-x4→`0x0F`, fp4-x4→`0x10`) | `0x143de71` | CONFIRMED |
| `+0x30` | 16 | MX output (MXMEM_PATTERN1D) | — | `assignAccessForMX` ({DATA,SCALE}) | `0x143df2e` | CONFIRMED |
| `+0x0C`..`+0x0F`,`+0x24`..`+0x2F` | — | reserved-zero | — | pxor zero-init | — | INFERRED |

### The saturate bit `+0x01.bit0`

```asm
143df33:  mov rax, [FP8ConvConfig]   ; GOT → libBIR global
143df3a:  cmp BYTE [rax], 0x0
143df3d:  jne skip
143df3f:  or  BYTE [r13+0x1], 0x1    ; SET the saturate bit
```

`bundle[1].bit0` is forced ON when `FP8ConvConfig[0]==0` (saturation not already arranged by config) — the wire counterpart of the sim's unconditional `saturate=1` (fp8 narrow with no Inf/NaN out). There is **no round-mode field** — rounding is hard RNE in silicon (the fp4 e2m1 packer and the libBIR e4m3fn/e5m2 narrows are all RNE). CONFIRMED — the `FP8ConvConfig` compare and `or` literal disassembled.

> **GOTCHA — the ×4-pack codegen guard.** Between the dtype stamps and the MX assign, the encoder FATALs unless the DATA output is an ×4-packed dtype: `mov eax,[rbx+0x30]; cmp eax,2; je ok; sub eax,8; cmp eax,1; setbe r8b` then `reportError("MX dtype must be packed with 4 elements into 1")` — duplicating the verifier EC211 gate. The `{2,8,9}` predicate `dt==2 || (dt-8)<=1` is byte-identical across encoder, verifier, simulator and `QuantizeMx`. String xxd-verified `@0x1d71c10`.

**Block geometry** is *not* a scalar field. The 32-element OCP-MXFP block (8-partition × 4-element) is implicit in the operand APs: `+0x38` K-extent = (out free-dim)×4 (the inner FP8/FP4 packing); `+0x21` src partition count `∈{32,64,96,128}`; the `+0x30/+0x34` ADDR4 pair + `+0x3B` scalePart keep data & E8M0 in the same 32-partition quadrant. The amax-reduce + EMAX-biased E8M0 derivation + round-to-grid live in silicon (the DVE microscaling unit); the verifier `checkQuantizeMxInstruction @0x1009a60` enforces `block_size=32`.

---

## The MX operand descriptor — `MXMEM_PATTERN1D`

All four MX opcodes share **one** operand encoder: `assignAccessForMX<MXMEM_PATTERN1D> @0x150e2f0`. The type is **16 bytes**; only **12 are written**; `+0xC..+0xF` are reserved (the type is passed by value to `mxmem1d_valid` as one `xmm` register, and the encoder stores only `+0x00..+0x0B`). The structural divergence from the dense `MEM_PATTERN`/`TENSORnD` family: **two** ADDR4 words (data + co-located E8M0 scale) in one descriptor, not one address.

| Off (in slot) | W | Field | Value source | Store-site | Tag |
|---|---|---|---|---|---|
| `+0` | 4 | DATA ADDR4 (×4-packed data base) | `assignStartAddr<ADDR4>(B+0, dataAP, isOut=0)` → `CoreV4Hwm::getStartAddress` (SB\|PSUM) | `0x150e54f` | CONFIRMED |
| `+4` | 4 | SCALE ADDR4 (E8M0 base) | `assignStartAddr<ADDR4>(B+4, scaleAP, isOut=1)` → `getStartAddressForMXScale` (**SBUF-only**) | `0x150e563` | CONFIRMED |
| `+8` | 2 | K-extent (×4-unpacked u16) | `NEPP(dataAP)`, ×4 iff `Dtype∈{2,8,9}` | `0x150e61c` | CONFIRMED |
| `+0xA` | 1 | step-direction sign | `(Pattern[last].step>>63)\|1` ∈ `{0x01, 0xFF}` (`sar rax,0x3f; or 1`) | `0x150e5e0` | CONFIRMED |
| `+0xB` | 1 | SCALE base-partition %PE_count | `scaleAP.getBasePartition() % 128` | `0x150e57c` | CONFIRMED |
| `+0xC` | 4 | reserved = 0 | (no encoder write) | — | CONFIRMED |

```asm
150e57c:  mov  BYTE [rbx+0xb], dl   ; scalePart % PE_count
150e5e0:  mov  BYTE [rbx+0xa], al   ; step-dir sign
150e61c:  mov  WORD [rcx+0x8], ax   ; K-extent (×4-unpacked)
150e5d9:  sar  rax, 0x3f            ; (step>>63) for the sign byte
```

The **×4-unpack**: `n = isTensorIndirect ? getNumIndirectIndices() : getNumElementsPerPartition(); if (Dtype==2 || (Dtype-8)<=1) n *= 4; MXMEM.K = n`. The dtypes `{2=fp4_e2m1fn_x4, 8=fp8_e4m3fn_x4, 9=fp8_e5m2_x4}` pack 4 logical elements per physical container; the K-extent counts **unpacked** logical elements while the ADDR4 leaf writes the start in **packed** container units (stride `qword_1DFC040[dt]`: fp4→2, fp8→4, both xxd-verified). This predicate is byte-identical across encoder, verifier EC211, simulator deinterleave-4, and the `QuantizeMx` ×4-pack assert.

**Indirect variant — `MXINDIRECT16B @0x150de90`.** If `dataAP->vtbl[+0x80]()` (`isTensorIndirectDynamicAP`) returns true, `assignAccessForMX` tail-jumps to the indirect encoder, which packs the same 16 bytes as a **three-ADDR4 gather triple**, every field +4-shifted: `+0` INDICES, `+0x03.bit5` indirect-mode marker (`or [B+3],0x20`), `+0x04` DATA, `+0x08` SCALE, `+0x0C` K-extent (u16), `+0x0F` scalePart. This is the only MX-indirect form. Full algorithm: [2.6](mxmem-pattern1d.md).

> **NOTE — the E8M0 scale is SBUF-only.** `isOut=1` routes the SCALE address through `CoreV4Hwm::getStartAddressForMXScale @0x185fbe0`, which asserts `"MX Scale Tensor can only be in SB"` (`@0x1da07b0`, string verified) and computes `addr = (basePart<<18 & 0xFF800000) + inPartByteAddr` with **no PSUM term**. The DATA address (`isOut=0`) routes through the ordinary `getStartAddress` (SB or PSUM).

The wire validator `mxmem1d_valid @0x1447190` agrees byte-for-byte with the encoder: DATA ADDR4 valid as DTYPE 9 (or DTYPE 5 iff desc-DTYPE arg `==0x10`), SCALE ADDR4 valid as DTYPE 3 (1-byte E8M0), K-extent ≠ 0, step ∈ `{+1,-1}` (`(step_byte+1)&0xFD == 0`), scalePart low-2 bits == 0 and ≤ `0x0F`. The static path **never reads** `+0xC..+0xF`.

---

## PE-tile row/col group — shared geometry

All matmul/weight bundles (V2/V3/V4) compute the PE row group identically: `row_group(+0x2C) = translateToRowGroup(tilePos.lo, num_partitions[+0x26])` (CoreV4 calls the CoreV3 member **by symbol** — direct inheritance proof), with `tilePos` from `getLegalTilePosition @0x1094330` (shared all three gens). For the MX bundles the **col group is hard-forced `0x0F`** (full 128-wide column) — MX is a new *data-format* path (×4 + E8M0 block scale), not a wider array; the 128×128 / four-quadrant PE geometry is unchanged from gen2/3 ([1.08](../arch/pe-engine.md)). MX writes row `@+0x2C` + const col `@+0x2D` as two separate bytes (vs CoreV3's packed `+0x2C` WORD). `QuantizeMx` has no row/col group (DVE engine, not PE).

---

## Field-naming census

Across the nine bundles, **every** semantic control byte is named to a recovered pybind/`<op>_info.so` string or a const-purpose: dense `Matmul` (`d3_mm_dtype`, `s_s3d3_mm_opcode`, `d3_mm_perf_opt_dtype`/`_src`, `d3_mm_num_elements`, `mm_psum_flags`, `d3_mm_valid_row_*`/`_col_group_active_col`, `d3_mm_valid_xbus`, `d3_mm_valid_src_partition`/`dst_*`); `LoadStationary` (`s3_lw_opcode`, `lw_dtype`, `lw_valid_num_rows`/`_active_cols`, `lw_valid_row_group`, `lw_valid_col_group_active_column`, `lw_valid_xbus`, `lw_perf_opt_src`/`_dtype`); sparse (`col_grp`, `num_active_cols`); MX (`d3_mm_dtype`, `d3_mm_valid_row`/`_col_group_active_col`, `mm_psum_flags`). The recovered strings were grepped live from `libwalrus.so` this pass (`d3_mm_dtype`, `d3_mm_valid_src_partition`, `d3_mm_valid_xbus`, `s3_lw_opcode`, `col_grp`, `num_active_cols`, `psum_flags`, `perf_opt_s`, … all present, suffix-interned).

The **only** bytes left as INFERRED/unnamed are the pxor-zeroed reserved padding (`d3_mm_reserved_z*` / `lw_reserved_zero` — named as a family but with no per-byte distinguishing member) and the sparse `+0x2D` ISA-check aux (the value path is STRONG via `runSingleISACheck`, the field NAME is the generic `col_grp` 0xF-bound). No field name is fabricated.

---

## Cross-References

- [1.08 The PE Engine](../arch/pe-engine.md) — the 128×128 systolic array these bundles drive; the engine this page encodes.
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — Family A, the `setupHeader` skeleton shared by all nine opcodes.
- [2.2–2.5 ADDR4 / Tensor descriptors / MEM_PATTERN](addr4.md) — the `+0x10`/`+0x30` access-pattern sub-bands and the ADDR4 atom every operand begins with.
- [2.6 MXMEM_PATTERN1D — MX Operand Addressing](mxmem-pattern1d.md) — the full `assignAccessForMX` algorithm, the indirect `MXINDIRECT16B` triple, and `mxmem1d_valid`.
- [1.05 Codename Taxonomy](../arch/codename-taxonomy.md) — the CoreV2=20 / V3=30 / V4=40 arch map and the `initCodegen` `std::variant` dispatch.
- [6.8.7 NKI Sparse Format](../nki/) — the structured-sparsity tag tensor as the NKI frontend produces it.
- [Part 7 — BIR Codegen Matmul (I02/I03)](../bir/) — the upstream `InstMatmult`/`InstMatmultSparse`/`InstMatmultMx` that feed these encoders, and `legalize_mm_accumulation_groups` (which pre-packs the CoreV4 accumulate byte).
- [Build & Version Provenance](../reference/versions.md) — the single pinned build all addresses are read from.
