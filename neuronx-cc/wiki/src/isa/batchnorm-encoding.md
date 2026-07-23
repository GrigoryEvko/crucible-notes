# BatchNorm-Family Encoding — Stats / Aggregate / Gradients / Backprop

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core logic — see the parity note in [Build & Version Provenance](../reference/versions.md)). The five encoder bodies, the dtype LUT, the IMM_SRC scalar encoder and the pybind assert roster live in `libwalrus.so` (build-id `92b4d331…`, VA == file offset); the `bir::Inst*` ordinals and the `NEURON_ISA_TPB_*_BN_STRUCT` shapes come from `libBIR.so` (`a9b1ea38`) and the `neuron_isa_tpb_pybind.cpython-310-*.so` assert table. Treat every address as version-pinned.*

## Abstract

This page is the byte-for-byte field map of the five batch-norm instructions the **Pool engine** emits onto the wire, produced by `neuronxcc::backend::CoreV2GenImpl`: `visitInstBNStats`, `visitInstBNStatsAggregate`, `visitInstBNGradients`, `visitInstBNBackprop`, and `visitInstBNBackprop2`. Between them they encode the full Welford / two-pass batch-norm statistics datapath — the forward partial-stats reduction, the cross-tile merge, the backward gradient reduction, and the `dx` apply — onto the 64-byte bundle the rest of the ISA shares ([2.1 the bundle](instruction-bundle.md)).

Three things make this family distinct from the elementwise ops. First, **`BNStats` is mixed-width**: the input access pattern is a `TENSOR4D` (the reduced ofmap) landing at `+0x0C`, but the output is a `TENSOR2D` — six fp32 per partition, two Welford triples — landing at `+0x30` (Family-C anchors, [2.1 BNStats mixed-width note](instruction-bundle.md)). Second, the **two backprop ops emit *two* bundles each**: a control / param-load bundle that carries the coefficients, `N` and `Mean`, followed by a data / back-prop bundle that carries the `x / dy / dx` tensors — and the param-load bundle is written *first* on the wire. Third, the **`BNGradients` op forks by arch level**: a pre-v5 AP/scalar-coefficient form (opcode `0x63`) and a `core_v5`+ register-or-AP-coefficient form (opcode `0x94`), with the older form FATAL-asserting on `core_v5`.

The bar for this page: a reader can **byte-encode any BN-family instruction by hand**, knowing for each control byte its offset, width, semantic, value source, the pybind assert string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence cell: CERTAIN means the exact store is disassembled and its offset cited, HIGH means the predicate / LUT / opcode was cross-checked, MEDIUM means the value is implied by the bulk `movups` zero-fill with no direct store. The Welford math itself is the silicon's; the encoder only **seeds** the per-lane counts and chooses which operand lands in which descriptor slot. No field name is fabricated; where the binary exposes only a 16-char-truncated assert slot, the reconstructed tail is shown in brackets.

## At a glance

Every BN bundle is a `std::array<unsigned char, 64>`: `emplace_back` into the engine's `SmallVector`, four `movups xmm0` to zero all 64 bytes, header-stamped by `setupHeader` (vtable slot `+0x48`), field-filled, ISA-checked, and `fwrite(buf, 1, 0x40, bin)`-ed — the common skeleton of [2.1](instruction-bundle.md#the-emission-skeleton). The header word is `(0x10<<8) | opcode`, little-endian.

| BIR inst / bundle role | Generator (libwalrus) | Opcode byte / word | ISA mnemonic | Wire struct |
|---|---|---|---|---|
| `BNStats` (no transpose) | `CoreV2GenImpl::visitInstBNStats` `@0x123ab00` | `0x61` / `0x1061` | `BatchNormStats2` | `S4D2_BN` |
| `BNStats` (transpose) | (same body; opcode toggled) | `0x82` / `0x1082` | `TransposeBatchNormStats2` | `S4D2_BN` |
| `BNStatsAggregate` | `CoreV2GenImpl::visitInstBNStatsAggregate` `@0x123c6a0` | `0x62` / `0x1062` | `BatchNormAggregate` | `S4D2_BN` |
| `BNGradients` (AP/scalar coeff) | `CoreV2GenImpl::visitInstBNGradients` `@0x1241b10` | `0x63` / `0x1063` | `BatchNormGradAccumulate` | `S3S3D1_BN` |
| `BNGradients` (register coeff) | (same body; arch-gated) | `0x94` / `0x1094` | `BatchNormGradAccumulate2` | `S3S3D1_BN2` |
| `BNBackprop` ctrl | `CoreV2GenImpl::visitInstBNBackprop` `@0x1235e80` | `0x64` / `0x1064` | `BatchNormParamLoad` | `S2_BN` |
| `BNBackprop2` ctrl | `CoreV2GenImpl::visitInstBNBackprop2` `@0x12368e0` | `0x8E` / `0x108E` | `BatchNormParamLoad2` | `S2_BNPL2` |
| `BNBackprop` / `BNBackprop2` data | (both backprop bodies) | `0x65` / `0x1065` | `BatchNormBackProp` | `S3S3D3_TT` |

| | |
|---|---|
| **Engine** | Pool — read-only `EngineInfo` at `Inst+0x90` for the per-engine census; **not** chosen by the encoder |
| **Bundle size** | exactly 64 bytes (`std::array<u8,64>` + `fwrite(…,0x40,…)`) |
| **Header** | `+0x00` opcode, `+0x01` `inst_word_len`=`0x10`, `+0x02..03` reserved=0 |
| **Slot family** | Family C (`BNStats`/`Aggregate`, 4D-in at the `+0x0C` low anchor; **2D-out at the `+0x30` *high* anchor — the A/B 3D-out slot, NOT the Family-C `+0x2C` 4D-out anchor**, see note); Family B (`Gradients`/data, 3×3D `+0x10`/`+0x20`/`+0x30`) |
| **dtype LUT** | `sub_120E650` → `byte_1DF5760` (BIR dtype ordinal → ISA wire tag) |
| **IMM_SRC encoder** | `sub_12051E0` (`setupImmediate<NEURON_ISA_TPB_IMM_SRC>`) — `Mean` / split-coeff scalar imm |
| **CodeGenMode** | `1` GENERATE (wire), `2` RUN_ISA_CHECKS (stack scratch, same layout), `0` COLLECT_OPCODES (census) |

> **GOTCHA — Family-C does not imply a `+0x30` destination.** Family C's canonical destination anchor is `+0x2C`, the 20-byte 4D-out slot. `BNStats`/`Aggregate` take their input on the Family-C low anchor `+0x0C` as a `TENSOR4D`, but write their narrow 2-D Welford output to **`+0x30`** — the high anchor Families A and B use for 3D output ([2.1 the bundle](instruction-bundle.md)). This is the mixed-width case: the anchor *position* is fixed per slot, but the descriptor *width* is per-operand, so a `TENSOR2D` stat pattern lands in the `+0x30` high slot (`lea 0x30(%r13)` + `assignAccess<TENSOR2D>` @ `0x123adf6`). The per-op field maps below pin `+0x30` directly.

---

## The common frame

All five bodies share the [2.1](instruction-bundle.md) prologue verbatim: emplace a 64-byte array, zero all 64 bytes (`4×movups xmm0`), then a virtual call through `vtbl+0x48` to `setupHeader`, which writes only `bundle[0]=opcode`, `bundle[1]=0x10` (`inst_word_len` = 16 dwords = 64 B), `bundle[2:4]=0`. Every byte the body does not explicitly write is a hard `0x00` on the wire.

`CodeGenMode` (`this+156` dword) selects three arms in every body, exactly as for the matmul encoders:

```c
// shared mode dispatch at the top of every visitInstBN* body
switch (self->codeGenMode) {            // *(int*)(self+156)
  case 1:  /* GENERATE_ISACODE   */     // emplace 64-B array, fill, ISA-check, fwrite 0x40
  case 2:  /* RUN_ISA_CHECKS     */     // fill a STACK temp (rbp-relative), ISA-check, no fwrite
  case 0:  /* COLLECT_OPCODES    */     // census only: insert opcode into set<u32>, no struct
  default: FATAL("Wrong CodeGenMode. It must be one of "
                 "GENERATE_ISACODE, RUN_ISA_CHECKS, or COLLECT_OPCODES");
}
```

The RUN_ISA_CHECKS arm builds the *same* field layout into an `rbp`-relative stack temp before running the ISA checker — which is itself a second, independent confirmation of every offset, since the two arms write the identical fields to two different bases. The COLLECT_OPCODES arm only inserts the opcode integer; its literal imm is the opcode-table proof.

> **GOTCHA —** disassembling a `visitInstBN*` body you will see *two* descriptor writes for the same field: an `[rbp+OFF]` write (the RUN_ISA_CHECKS scratch) and an `[r12/r13/r14/rbx+OFF]` write (the live wire slot in GENERATE mode). Only the latter is the 64-byte bundle. Reading the `rbp` form as the wire layout is the classic mis-pinned-offset trap — though here the two layouts happen to agree, which is what makes the RUN arm a useful cross-check.

> **NOTE — engine.** Every body reads `EngineInfo` at `Inst+0x90` to key a per-engine census `DenseMap`; the physical engine is **not** chosen by the encoder and is **not** a wire field. The BN opcodes occupy the Pool-engine block of the `_OPCODE` table (grouped with `Pool` / `Max8` / `FindIndex8`), so engine = POOL follows from opcode grouping and partition-reduce semantics rather than from a hard encoder assert.

---

## `visitInstBNStats` — `BatchNormStats2` (op `0x61` / `0x82`, `S4D2_BN`)

### Purpose

`BNStats` is the forward partial-statistics reduction: it folds the free dimension of the reduced ofmap into **two per-lane Welford triples** — `[n_A, mean_A, M2_A, n_B, mean_B, M2_B]`, six fp32 per partition. Lane A accumulates the even-indexed free elements (`ceil(N/2)` of them), lane B the odd (`floor(N/2)`); the silicon merges them at run time. The encoder's only numeric job is to **seed the two counts**; the mean/M2 words are produced by the hardware accumulator. `M2` is the raw sum-of-squared-deviations — normalisation to population variance happens only in `Aggregate`.

### Pre-guards and opcode select

```c
// CoreV2GenImpl::visitInstBNStats @0x123ab00
function visitInstBNStats(self, Inst):
    if *(u8*)(Inst+0x110) != 0:                  // apply_transpose MaybeAffine<bool> variant TAG
        throw bad_variant_access;                 //   @0x123ab2e — the bool must be a constant
    // opcode from apply_transpose (Inst+0xF0): 0 -> 0x61, 1 -> 0x82
    opcode = (Inst[0xF0] == 1) ? 0x82 : 0x61;     // sbb/and 0xFFFFFFDF/sub 0x7E  @0x123ab5f
    ...
    // POST-fill: reject an indirect/symbolic input AP
    if isTensorIndirect(arg0_AP):                 // (vtbl+0x80)(arg0)
        reportError("TODO: support BATCH_NORM_STATS2 with TensorIndirect AP "
                    "once ISA supports it");      //   @0x123ad8e / 0x123b013
```

The transpose flag is **folded into the opcode** (`0x61` vs `0x82` = `TransposeBatchNormStats2`); it is not a separate bundle field. The encoder computes it branchlessly: `cmpb $0x1,0xf0(%rsi); sbb %r14d,%r14d; and $0xffffffdf,%r14d; sub $0x7e,%r14d`, giving `0x61` for `transpose=0` and `0x82` for `transpose=1`.

### Control-band field map (GENERATE path, base = `r13`)

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x61`/`0x82` | `s_batchnorm_stats_opcode` | `setupHeader(apply_transpose)` | hdr | CERTAIN |
| `+0x01` | 1 | `inst_word_len`=16 | (header) | `setupHeader` b1=`0x10` | hdr | CERTAIN |
| `+0x02` | 2 | format/reserved=0 | (header) | `setupHeader` | hdr | CERTAIN |
| `+0x0C` | 16 | **IN AP `TENSOR4D`** | `s_bnstats2_src_element_cnt` | `assignAccess<TENSOR4D>(arg0)` | `lea 0xc(%r13)` `@0x123ade5` | CERTAIN |
| `+0x20` | 1 | IN dtype (ISA tag) | `bnstats2`/`s2_bn` dtype | `sub_120E650([arg0+0x30])` | `mov %al,0x20(%r13)` `@0x123acec` | CERTAIN |
| `+0x21` | 1 | OUT dtype (ISA tag) | `d2_bn_stats_out_[ty]` | `sub_120E650([out+0x30])` | `mov %al,0x21(%r13)` `@0x123ad07` | CERTAIN |
| `+0x22` | 1 | BASE partition | `s_bnstats2` src/base | `*(*(arg0+0x50)+8)` (APPair0) | `mov %al,0x22(%r13)` `@0x123ad29` | CERTAIN |
| `+0x28` | 4 | **EVEN count `n_A`** (f32) | `s even_count must be finite` / `d2_bn_zero_counts` | `(float)((InNEPP+1)>>1)` = `ceil(N/2)` | `movss %xmm1,0x28(%r13)` `@0x123ad45` | CERTAIN |
| `+0x2C` | 4 | **ODD count `n_B`** (f32) | `s odd_count must be finite` / `d2_bn_zero_counts` | `(float)(InNEPP>>1)` = `floor(N/2)` | `movss %xmm1,0x2c(%r13)` `@0x123ad75` | CERTAIN |
| `+0x30` | 16 | **OUT AP `TENSOR2D`** | `s_bnstats2_dst_e[lement_cnt]` | `assignAccess<TENSOR2D>(out)` | `lea 0x30(%r13)` `@0x123adf6` | CERTAIN |
| `+0x04..0B`, `+0x23..27` | — | RESERVED-0 | `d2_bn_reserved_z[ero]` | bulk `movups` zero-init | — | MEDIUM |

`InNEPP = getNumElementsPerPartition(arg0)`. The count seeds are computed by `add $1,%rax; shr $1,%rax; cvtsi2ss` (ceil) and `shr $1,%rax; cvtsi2ss` (floor), reusing the same `%rax`, across `0x123ad39`–`0x123ad75`. The output AP is `assignAccess<TENSOR2D>`: 12 bytes encoding the 6-fp32 destination, so the OUT operand uses the **`+0x30` high anchor** (Family-C) with a narrow 2-D pattern, not a 4-D one — this is the mixed-width case [2.1](instruction-bundle.md) calls out. The verifier asserts out-NEPP == 6.

> **NOTE — base partition.** `+0x22` reads `*(*(arg0+0x50)+8)`, the partition of the input AP's first `APPair`, guarded by `[arg0+0x58]!=0` (else assert `"idx < size()"` `@0x123b0b0`) — the AP must carry at least one pair. The same `(*(AP+0x50)+8)` access recurs for the base byte of every BN op.

### Welford seeding

```c
// the ONLY numeric values the encoder writes into the 6-elt output's frame
n_A = ceil(N/2);   // +0x28, lane A = even-indexed free elements
n_B = floor(N/2);  // +0x2C, lane B = odd-indexed free elements
// mean_A, M2_A, mean_B, M2_B are produced by the silicon Welford accumulator at run time.
// The two "*_count must be finite" asserts validate the f32 seeds;
// d2_bn_zero_counts is the N==0 -> counts-must-be-zero guard.
```

---

## `visitInstBNStatsAggregate` — `BatchNormAggregate` (op `0x62`, `S4D2_BN`)

### Purpose

`Aggregate` is the cross-tile merge: it takes `K` partial Welford triples (`K·3` fp32, the concatenated outputs of `K` `BNStats` runs) and reduces them to `(mean, population-variance)` — exactly **two** fp32 per partition. The Chan / parallel-variance merge (`n_a+n_b`, pooled mean, `M2_a+M2_b + n_a·n_b/(n_a+n_b)·δ²`, then the divide-by-N to turn `M2` into population variance) is the engine's; the encoder reads the counts *from the packed input triples*, not from the bundle.

It is the simplest of the family — a strict field **subset** of `BNStats`, minus the two count seeds (`+0x28`/`+0x2C`) and minus the transpose toggle (opcode is the constant `0x62`).

### Control-band field map (GENERATE path, base = `r12`)

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x62` (const) | `s_batchnorm_aggregate_opcode` | `setupHeader(98)` | `movb $0x62` `@0x123c98e` | CERTAIN |
| `+0x0C` | 16 | IN AP `TENSOR4D` | `s_bnstats_agg_src_element_cnt` | `assignAccess<TENSOR4D>(arg0)` | `lea 0xc(%r12)` `@0x123c825` | CERTAIN |
| `+0x20` | 1 | IN dtype | `d2_bn_agg_in_typ[e]` | `sub_120E650([arg0+0x30])` | `mov %al,0x20(%r12)` `@0x123c85f` | CERTAIN |
| `+0x21` | 1 | OUT dtype | `d2_bn_agg_out_ty[pe]` | `sub_120E650([out+0x30])` | `mov %al,0x21(%r12)` `@0x123c88d` | CERTAIN |
| `+0x22` | 1 | BASE partition | `s_bnstats_agg_dst` / base | `*(*(arg0+0x50)+8)` | `mov %al,0x22(%r12)` `@0x123c84d` | CERTAIN |
| `+0x30` | 16 | OUT AP `TENSOR2D` | `s_bnstats_agg_dst_element_cnt` | `assignAccess<TENSOR2D>(out)` | `lea 0x30(%r12)` `@0x123c86b` | CERTAIN |
| `+0x28`/`+0x2C`, `+0x04..0B`, `+0x23..27` | — | RESERVED-0 (no count seeds) | `d2_bn_reserved_z[ero]` | zero-init | — | CERTAIN (absence of stores) |

The verifier asserts in-NEPP `% 3 == 0` (`K` triples) and out-NEPP `== 2`. The input is a `TENSOR4D` (`assignAccess<TENSOR4D>`, byte-verified) — the descriptor *type* is 4-D even though the data is a flat `K·3` stat vector.

---

## `visitInstBNGradients` — `BatchNormGradAccumulate` (op `0x63` / `0x94`)

### Purpose

`BNGradients` is the backward reduction: it folds `x` and `dy` against the forward statistics into a **3-element** gradient vector — `[Σdy, Σ(x−m)·dy·vf, Σ(x−m)·dy·vf²]` — used to assemble the `dx` coefficients. One 64-byte bundle, but **two encoding variants** chosen by an arch-feature gate plus the operand kind: an AP/scalar-coefficient form (`0x63`, struct `S3S3D1_BN`) and a register-or-AP form (`0x94`, struct `S3S3D1_BN2`).

### Operand and arch gate

```c
// CoreV2GenImpl::visitInstBNGradients @0x1241b10
function visitInstBNGradients(self, Inst):
    in2 = getMeanAccessor(Inst);                  // sub_1240360 — "in2 must be AP" gate
    in3 = getArgument(Inst, 3);                    // Varfactor
    out = getOutput<AccessPattern>(Inst, 0);       // Gradients (3-elt)
    assert(isAP(in2) && isAP(in3),  "in2 and in3 must be AP argument type");         // line 1562
    assert(isLocationSB(in2) || isLocationPSUM(in2),
           "ISA immediate pointer should only be SB/PSUM accesses");                  // 1564/1566
    assert(in2.dtype == in3.dtype,  "in2.getType() == in3.getType()");               // NeuronAssertion 956

    if *(int*)(self+0x258) <= 0x31:                // arch level <= 49  (pre-v5)   @0x1242149
        if ModuleArchLevel >= ArchLevel::core_v5:
            FATAL("BatchNormGradAccumulate not supported in core_v5");                // NeuronAssertion 2101
        opcode = 0x63;  struct = S3S3D1_BN;        // VARIANT A — AP/scalar coeffs
    else:                                          // arch level > 49
        opcode = 0x94;  struct = S3S3D1_BN2;       // VARIANT B — register-or-AP coeffs
```

The gate is the single compare `cmpl $0x31,0x258(%r12)` @ `0x1242149`. The `core_v5` FATAL (`NeuronAssertion 2101`, "`BatchNormGradAccumulate not supported in core_v5`") makes the `0x63` AP-coeff form **illegal on `core_v5`**; the register-coeff `0x94` form — `BatchNormGradAccumulate2` — is its replacement there.

### Shared AP-slot template (`generateBNGradients<…>` `@0x133e550` / `@0x133e630`)

Both variants share a byte-identical AP layout, filled by the template helper before the variant-specific body. `arg0 = Ofmap(x)`, `arg1 = OfmapGrad(dy)`, `out = Gradients(3-elt)`. `S3S3D1` = Src-3D, Src-3D, Dst-1D:

| Off | W | Field | Value / source | Store-site | Conf |
|---|---|---|---|---|---|
| `+0x3C` | 1 | packed dtype: low nibble = `sub_120E650(Ofmap.D)`, high nibble = `16*sub_120E650(OfmapGrad.D)` | RMW `&0xF0` then `&0x0F` | `mov %al,0x3c(%rbx)` ×2 `@0x133e5a3`/`@0x133e5bd` | CERTAIN |
| `+0x3F` | 1 | BASE partition | `*(*(Ofmap+0x50)+8)` (guard `[+0x58]!=0`) | `mov %al,0x3f(%rbx)` `@0x133e5de` | CERTAIN |
| `+0x10` | 16 | Ofmap AP `TENSOR3D` | `assignAccess<TENSOR3D>(arg0)` | `lea 0x10(%rbx)` `@0x133e5ce` | CERTAIN |
| `+0x20` | 16 | OfmapGrad AP `TENSOR3D` | `assignAccess<TENSOR3D>(arg1)` | `lea 0x20(%rbx)` `@0x133e5e6` | CERTAIN |
| `+0x30` | 16 | Gradients AP `TENSOR1D` | `assignAccess<TENSOR1D>(out)` | `lea 0x30(%rbx)` `@0x133e5f7` | CERTAIN |

### Variant A — op `0x63` (`S3S3D1_BN`, AP/scalar coefficients)

After the template, the body writes the scalar coefficient *values* and their dtypes:

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x0C` | 4 | `Mean` (in2) scalar VALUE | `ga_imm_check` / `ga_valid_immedia[te]` | `(self[76]→vtbl+0x20)(in2, 0)` (4-byte wire imm) | `mov %eax,0xc(%r13)` `@0x12422ab` | CERTAIN |
| `+0x38` | 4 | `Varfactor` (in3) scalar VALUE | `ga_imm_check` | `(self[76]→vtbl+0x20)(in3, 0)` | `mov %eax,0x38(%r13)` `@0x12422e2` | CERTAIN |
| `+0x3D` | 1 | in2 (`Mean`) dtype | `bnga_valid_input_t[ype]` | `sub_120E650(in2.D)` | `mov %al,0x3d(%r13)` `@0x12422fd` | CERTAIN |
| `+0x3E` | 1 | out (`Gradients`) dtype | `ga_valid_output_[type]` | `sub_120E650(out.D)` | `mov %al,0x3e(%r13)` `@0x1242299` | CERTAIN |

Full operands: AP0 `Ofmap`, AP1 `OfmapGrad`, in2 `Mean`(scalar), in3 `Varfactor`(scalar), OUT `Gradients`(3-elt).

### Variant B — op `0x94` (`S3S3D1_BN2`, register-or-AP coefficients)

The dynamic / HWDGE form: `Mean` and `Varfactor` may each be a physical AP scalar **or** a register id, tagged by a 2-bit mode nibble at `+0x3E`:

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x3E` | 1 | coeff MODE nibbles: low (in2) `\|0x1`=AP / `\|0x2`=reg; high (in3) `\|0x10`=AP / `\|0x20`=reg | `ga2_valid_output` | RMW (`&0xF0`/`&0x0F` preserve) | `mov %al,0x3e(%r13)` `@0x124282d` (reg) | CERTAIN |
| `+0x0C` | 4 or 1 | in2 value: AP → `(self[76]→vtbl+0x20)` [4B] OR reg → `getRegId` [1B] | `ga2_imm_check` | mode-dependent | `mov [0xc(%r13)]` `@0x1242bb7` (AP) / `@0x1242844` (reg) | CERTAIN |
| `+0x38` | 4 or 1 | in3 value: AP-scalar [4B] OR reg `getRegId` [1B] | `ga2_imm_check` | mode-dependent | `mov [0x38(%r13)]` `@0x1242bdb` / `@0x124291b` | CERTAIN |
| `+0x3D` | 1 | packed dtype: low = `sub_120E650(in2.D)`, high = `16*sub_120E650(out.D)` | `ga2_valid_input_` | RMW | `mov %al,0x3d(%r13)` `@0x1242305` | CERTAIN |

A register coefficient with an offset register attached is rejected: `"offset register cannot be applied to register immediate in ISA"` (line 1584); a wrong kind throws `"contains wrong argument type"`. `0x94` = `BatchNormGradAccumulate2`.

> **NOTE — `self[76]`.** The scalar value encoder is the object at `self+0x260` (`= *((u64*)self+76)`); its `vtbl+0x20` slot encodes an AP-scalar immediate to a 4-byte wire value (the same IMM_SRC family the backprop `setupImmediate` uses). The register path bypasses it and calls `bir::Register::getRegId` for a 1-byte id.

---

## `visitInstBNBackprop` — `dx` apply, packed coeffs (op `0x64` + `0x65`, two bundles)

### Purpose

`BNBackprop` computes `dx = (dy·N − ((x−m)·Bc + A)) · scale` and emits **two 64-byte bundles per BIR instruction, in wire order**:

1. **bundle #1** = opcode `0x64` `BatchNormParamLoad` — the *control* bundle: the packed coefficient AP, `total_elements` (`N`), the coeff dtype, the base, and `Mean` as an IMM_SRC. Struct `S2_BN`.
2. **bundle #2** = opcode `0x65` `BatchNormBackProp` — the *data* bundle: the `Ofmap`(x) / `OfmapGrad`(dy) / `Dst`(dx) tensors. Struct `S3S3D3_TT`.

Order is pinned by the two header stores: in GENERATE mode the body stamps the `0x64` control header (`movb $0x64,-0x112(%rbp)` @ `0x12362b1`) before the `0x65` data header (`movb $0x65,-0x110(%rbp)` @ `0x12360cd`). Param-load **first**, back-prop **second**.

The naming follows the semantics, not the emission order: `0x64` `BatchNormParamLoad` *loads* the coefficients, `N` and `Mean` (its asserts are the `s_bn_param_load_*` group, alongside `s2_bn_dtype` and `s_valid_bn_param_load_imm0`), and `0x65` `BatchNormBackProp` *applies* `dx` over x/dy/dx (assert `s_batchnorm_backprop_opcode`, grouped with `s3d3_tt_*`).

### Bundle #1 — op `0x64` `BatchNormParamLoad` (`S2_BN`, base = `r14`)

Operands read: `getArgument(0)=Ofmap` (base only), `getArgument(2)=Gradients` (3-elt packed coeff `[scale, A, Bc]`), `getArgument(3)=Mean`.

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x64` | `s_bn_param_load_[opcode]` | `setupHeader(100)` | `movb $0x64` `@0x12362b1` | CERTAIN |
| `+0x10` | 16 | Gradients coeff AP `TENSOR2D` | `s2_bn_src_element_cnt` | `assignAccess<TENSOR2D>(arg2)` | `lea 0x10(%r14)` `@0x1236441` | CERTAIN |
| `+0x1C` | 4 | `total_elements` `N` (f32) | `s_valid_bn_param_load_imm0` | `*(float*)(Inst+0xF0)` | `movss %xmm0,0x1c(%r14)` `@0x1236364` | CERTAIN |
| `+0x20` | 1 | coeff (arg2) dtype | `s2_bn_dtype` | `sub_120E650([arg2+0x30])` | `mov %al,0x20(%r14)` `@0x1236316` | CERTAIN |
| `+0x22` | 1 | BASE partition | `s2_bn` / base | `*(*(arg0+0x50)+8)` | `mov %al,0x22(%r14)` `@0x123634f` | CERTAIN |
| `+0x30` | 16 | `Mean` (IMM_SRC) | `s_valid_bn_param_load_imm0` | `setupImmediate(arg3)` (`sub_12051E0`) | `lea 0x30(%r14)` `@0x12363e4` | CERTAIN |
| `+0x21`, `+0x23..2F`, `+0x02..0B` | — | RESERVED-0 | `s2_bn_reserved_zer[o]` | zero-init | — | MEDIUM |

`total_elements` at `Inst+0xF0` is the `dx` `N`-multiplier; it is the BIR-JSON `total_elements` float. `Mean` is encoded by `setupImmediate<NEURON_ISA_TPB_IMM_SRC>` — a scalar immediate or an AP source, the same 4-function IMM_SRC encoder the matmul scalar path uses.

### Bundle #2 — op `0x65` `BatchNormBackProp` (`S3S3D3_TT`, base = `r14`)

Operands: `getArgument(0)=Ofmap`(x), `getArgument(1)=OfmapGrad`(dy), `getOutput(0)=Dst`(dx). `S3S3D3_TT` = three rank-3 SBUF tensors laid out like a `TensorTensor` op.

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x65` | `s_batchnorm_backprop_opcode` | `setupHeader(101)` | `movb $0x65` `@0x12360cd` | CERTAIN |
| `+0x0C` | 1 | packed dtype: low nibble = `sub_120E650(Ofmap.D)`, high = `16*sub_120E650(OfmapGrad.D)` | `s3d3_tt_dtype` | RMW `&0xF0`/`&0x0F` | `mov %al,0xc(%r14)` `@0x1236124..0x1236149` | CERTAIN |
| `+0x0D` | 1 | OUTPUT (`Dst`) dtype | `s3d3_tt_dtype` | `sub_120E650(out.D)` | `mov %al,0xd(%r14)` `@0x123615d` | CERTAIN |
| `+0x0F` | 1 | BASE partition | `s3d3_tt` / base | `*(*(arg0+0x50)+8)` (guard `[+0x58]!=0`) | `mov %al,0xf(%r14)` `@0x1236181` | CERTAIN |
| `+0x10` | 16 | Ofmap AP `TENSOR3D` | `s3d3_tt_valid_op[s]` | `assignAccess<TENSOR3D>(arg0)` | `lea 0x10(%r14)` `@0x1236179` | CERTAIN |
| `+0x20` | 16 | OfmapGrad AP `TENSOR3D` | `s3d3_tt_src_dst_[count]` | `assignAccess<TENSOR3D>(arg1)` | `lea 0x20(%r14)` `@0x123618f` | CERTAIN |
| `+0x30` | 16 | `Dst` (dx) AP `TENSOR3D` | `s3d3_tt_dst_elem[ent_cnt]` | `assignAccess<TENSOR3D>(out)` | `lea 0x30(%r14)` `@0x12361a7` | CERTAIN |

`s3d3_tt_is_zero_[guard]` is the empty / Welford-empty guard. Full operands across both bundles (the `dx` apply): AP0 `Ofmap`, AP1 `OfmapGrad`, AP2 `Gradients`(3-elt packed coeff), AP3 `Mean`, OUT `Dst`. The packed coeffs ride the param-load bundle's AP2; `total_elements` is its `+0x1C` float; `Mean` its `+0x30` IMM_SRC.

---

## `visitInstBNBackprop2` — `dx` apply, split coeffs (op `0x8E` + `0x65`, two bundles)

### Purpose

`BNBackprop2` is structurally identical to `BNBackprop` (two bundles, control first), differing **only in the control bundle**. The `dx` numerics are identical; the difference is purely operand packing — the scalar `scale` is split into its own 1-element tensor and `(A, Bc)` into a 2-element tensor, encoded via **two** `setupImmediate` calls instead of one packed 3-element AP.

Order is pinned the same way: control header `movb $0x8E,-0x112(%rbp)` @ `0x1236d11`, then data header `movb $0x65,-0x110(%rbp)` @ `0x1236b2d`.

> **GOTCHA — the two param-load bundles do not share an opcode.** `BNBackprop2`'s control bundle is `0x8E` (`BatchNormParamLoad2`), a distinct opcode from `BNBackprop`'s `0x64` (`BatchNormParamLoad`) — only the *data* bundle `0x65` is shared between the two.

### Bundle #1 — op `0x8E` `BatchNormParamLoad2` (`S2_BNPL2`, base = `r14`)

Five operands: `getArgument(0)=Ofmap`(base), `getArgument(2)=GradientsA` (1-elt `[scale]`), `getArgument(3)=GradientsBc` (2-elt `[A, Bc]`), `getArgument(4)=Mean`.

| Off | W | Field | pybind assert | Value / source | Store-site | Conf |
|---|---|---|---|---|---|---|
| `+0x00` | 1 | opcode `0x8E` | `s_s2_bnpl2_opcode` | `setupHeader(142)` | `movb $0x8e` `@0x1236d11` | CERTAIN |
| `+0x10` | 16 | GradientsA coeff AP `TENSOR2D` | `s_valid_s2_bnpl2_imm_ptr` | `assignAccess<TENSOR2D>(arg2)` | `lea 0x10(%r14)` (`@0x123703f`) | CERTAIN |
| `+0x1C` | 4 | `total_elements` `N` (f32) | `s_bnpl2_src_elem[ent]` | `*(float*)(Inst+0xF0)` | `movss %xmm0,0x1c(%r14)` `@0x1236db9` | CERTAIN |
| `+0x20` | 1 | GradientsA dtype | `s2_bnpl2` dtype | `sub_120E650([arg2+0x30])` | `mov %al,0x20(%r14)` `@0x1236d7b` | CERTAIN |
| `+0x22` | 1 | BASE partition | `s2_bnpl2` / base | `*(*(arg0+0x50)+8)` | `mov %al,0x22(%r14)` `@0x1236da4` | CERTAIN |
| `+0x30` | 16 | split coeffs + `Mean` | `s_valid_s2_bnpl2_imm_ptr` | `setupImmediate(GradientsBc)` THEN `setupImmediate(Mean)` — two `sub_12051E0` | `call 12051e0` `@0x1236eda` + `@0x1236fc8` | CERTAIN |

The two IMM_SRC calls are the defining difference: `call 12051e0` appears twice in the control body (`@0x1236eda` for `GradientsBc`, `@0x1236fc8` for `Mean`), versus the single call in `BNBackprop`. Full operands: AP0 `Ofmap`, AP1 `OfmapGrad`, AP2 `GradientsA`(1-elt), AP3 `GradientsBc`(2-elt), AP4 `Mean`, OUT `Dst`.

> **NOTE — split rationale.** The `dx` math is identical to `BNBackprop`; splitting `scale` into a 1-element tensor and `(A,Bc)` into a 2-element tensor with two IMM_SRC encodings (rather than one packed 3-element AP) lets the two coefficient groups be sourced separately. The operand-packing difference is exact; the "so separate engines can produce them" rationale is [INFERRED].

### Bundle #2 — op `0x65` `BatchNormBackProp`

Byte-identical to `BNBackprop`'s data bundle: `arg0`/`arg1`/`out` as 3×`TENSOR3D` at `+0x10`/`+0x20`/`+0x30`; `+0x0C` dtype nibbles (Ofmap low / OfmapGrad high); `+0x0D` out dtype; `+0x0F` base.

*Anchors: `mov %al,0xc(%r14)` @ `0x1236b8e` / `0x1236ba9`; `[0xd(%r14)]` @ `0x1236bbd`; `[0xf(%r14)]` @ `0x1236be1`; AP slots `lea 0x10(%r14)` @ `0x1236bd9` and `lea 0x30(%r14)` @ `0x1236c07`.*

---

## The pybind assert roster → struct-offset join

The pybind module `neuron_isa_tpb_pybind.cpython-310-*.so` exposes each wire struct as a raw byte array; its `.rodata` assert table names the fields (16-char-truncated in the binding strings — full tails in brackets). Grouped by struct, every name below is confirmed present via `strings` on the module:

```text
S4D2_BN  (BNStats / Aggregate) ──────────────────────────────────────────────
  s_batchnorm_stats_opcode        -> +0x00 opcode (Stats 0x61/0x82)
  s_batchnorm_aggregate_opcode    -> +0x00 opcode (Aggregate 0x62)
  s_bnstats2_src_element_cnt      -> +0x0C IN AP (TENSOR4D) element count
  s_bnstats2_dst_e[lement_cnt]    -> +0x30 OUT AP (== 6)
  s even_count must be finite     -> +0x28 f32 even-count (n_A)
  s odd_count must be finite      -> +0x2C f32 odd-count  (n_B)
  d2_bn_zero_counts               -> +0x28/+0x2C zero-when-N==0 guard
  d2_bn_stats_out_[ty]            -> +0x21 out dtype (Stats)
  d2_bn_agg_in_typ[e]             -> +0x20 in  dtype (Aggregate)
  d2_bn_agg_out_ty[pe]            -> +0x21 out dtype (Aggregate)
  s_bnstats_agg_src_element_cnt   -> in-AP NEPP (% 3 == 0)
  s_bnstats_agg_dst_element_cnt   -> out-AP NEPP (== 2)
  d2_bn_reserved_z[ero]           -> reserved-zero band

S3S3D1_BN / _BN2  (BNGradients) ─────────────────────────────────────────────
  s_s3s3d1_bn_opco[de]            -> +0x00 opcode 0x63
  s_s3s3d1_bn2_opc[ode]           -> +0x00 opcode 0x94
  s3d1_bn_channels                -> partition/channel count (+0x3F base + AP)
  bnga_valid_input_t[ype]         -> +0x3D in2(Mean) dtype (low nibble)
  ga_valid_output_[type]          -> +0x3E out dtype (A) / mode (B)
  ga_imm_check / ga_valid_immedia -> +0x0C in2 value / +0x38 in3 value
  ga_src_element_cnt_check        -> source (Ofmap/OfmapGrad) AP element count
  ga_dst_element_c[nt]            -> out Gradients AP NEPP (== 3, TENSOR1D)
  ga2_* (src_element_cnt_check, valid_output, valid_input_, imm_check)
                                  -> the op0x94 (BN2) parallels of ga_*

S2_BN / S2_BNPL2  (BNBackprop/2 control = param-load) ───────────────────────
  s_bn_param_load_[opcode]        -> +0x00 opcode 0x64 (S2_BN)
  s_s2_bnpl2_opcode               -> +0x00 opcode 0x8E (S2_BNPL2)
  s2_bn_dtype                     -> +0x20 coeff dtype
  s2_bn_reserved_zer[o]           -> +0x21/+0x23.. reserved band
  s_bnpl2_src_elem[ent]           -> +0x1C total_elements (BNPL2)
  s_valid_bn_param_load_imm0      -> +0x30 Mean IMM_SRC validity (BP1)
  s_valid_s2_bnpl2_imm_ptr        -> +0x30 split-coeff/Mean IMM_SRC validity (BP2)

S3S3D3_TT  (BNBackprop/2 data = back-prop) ──────────────────────────────────
  s_batchnorm_backprop_opcode     -> +0x00 opcode 0x65
  s3d3_tt_valid_op[s]             -> +0x10 Ofmap AP validity
  s3d3_tt_dtype                   -> +0x0C dtype-nibbles + +0x0D out dtype
  s3d3_tt_src_dst_[count]         -> +0x20 OfmapGrad / +0x30 Dst shape match
  s3d3_tt_src_element_cnt_check   -> source AP element count
  s3d3_tt_dst_elem[ent_cnt]       -> Dst AP element count
  s3d3_tt_is_zero_[guard]         -> empty/zero-count guard
```

> **GOTCHA — sub-byte widths are not pinned.** The encoder writes whole bytes; the dtype tags at `+0x20`/`+0x21`/`+0x0C`/`+0x3D` may internally be 5-bit-tag-plus-3-reserved, but the binary stores the whole byte and the pybind exposes the structs as raw byte arrays with no per-field bit metadata. Only the byte field map and the explicit `&0xF`/`&0xF0` nibble splits (the dtype-nibble and mode-nibble stores) are pinned below byte granularity. Do not invent a bit layout below the byte.

---

## Confidence ledger

| Claim | Confidence | Evidence |
|---|---|---|
| Five `CoreV2GenImpl::visitInstBN*` bodies at the cited addresses | CERTAIN | `nm -DC libwalrus.so` (`0x123ab00`/`0x123c6a0`/`0x1241b10`/`0x1235e80`/`0x12368e0`) |
| `BNStats` mixed-width 4D-in `+0x0C` / 2D-out `+0x30` | CERTAIN | `lea 0xc(%r13)` `@0x123ade5` + `lea 0x30(%r13)`+`assignAccess<TENSOR2D>` `@0x123adf6` |
| `BNStats` count seeds `ceil/floor(N/2)` @ `+0x28`/`+0x2C` | CERTAIN | `add$1;shr$1;cvtsi2ss;movss 0x28` / `shr$1;…0x2c` `@0x123ad39..75` |
| Opcode `0x61`/`0x82` folds transpose | CERTAIN | `cmpb$1,0xf0;sbb;and$0xffffffdf;sub$0x7e` `@0x123ab5f` |
| `Aggregate` = `BNStats` minus count seeds (op `0x62`) | CERTAIN | `movb $0x62` `@0x123c98e`; `+0x20/0x21/0x22/lea 0xc/0x30` on `r12` |
| `Gradients` arch gate `<=49` → `0x63`, else `0x94`; `core_v5` FATAL on `0x63` | CERTAIN | `cmpl $0x31,0x258(%r12)` `@0x1242149`; `NeuronAssertion 2101` |
| Gradients template `+0x3C` dtype-nibble / `+0x3F` base / 3 AP slots | CERTAIN | `mov 0x3c(%rbx)`×2 / `mov 0x3f(%rbx)` / `lea 0x10/0x20/0x30(%rbx)` `@0x133e59a..f7` |
| `BNBackprop` two bundles `0x64`→`0x65`, control first | CERTAIN | `movb $0x64` `@0x12362b1` before `movb $0x65` `@0x12360cd` |
| `BNBackprop2` control opcode `0x8E` (not `0x64`) | CERTAIN | `movb $0x8e,-0x112(%rbp)` `@0x1236d11` |
| `BNBackprop2` split coeffs = two `setupImmediate` | CERTAIN | `call 12051e0` `@0x1236eda` + `@0x1236fc8` |
| `S3S3D3_TT` data bundle byte-identical across BP1/BP2 | CERTAIN | `+0x0C/0x0D/0x0F` + 3×`TENSOR3D` stores in both bodies |
| Every pybind assert name (`d2_bn_*`/`s2_bn*`/`s3d3_tt_*`/`ga*`/`bnstats2_*`) | HIGH | `strings neuron_isa_tpb_pybind.cpython-310-*.so` |
| dtype LUT `sub_120E650`→`byte_1DF5760` | HIGH | shared with the matmul dtype path; not re-traced this pass |
| Reserved-zero bands | MEDIUM | bulk `movups` zero-fill + `*_reserved_zero` asserts; no direct store |
| Engine = Pool | HIGH | opcode-table grouping + partition-reduce semantics; `Inst+0x90` is the scheduler's field |

---

## Cross-References

- [The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the shared frame, `setupHeader`, and the Family-C mixed-width note (`BNStats` 4D-in / 2D-out) this page fills in (2.1).
- [PE Matmul Encoding](pe-matmul-encoding.md) — the sibling encoding page; the matmul scalar IMM_SRC path the BN coeff encoder reuses (2.10).
- [ADDR4 — the 32-Bit Address Word](addr4.md) — what rides at byte 0 of every descriptor slot (2.2).
- [TENSOR4D / MEM_PATTERN4D — the Spill Descriptor](tensor4d-mempattern4d.md) — the 20-byte `TENSOR4D` slot at `BNStats`/`Aggregate` `+0x0C` (2.4).
- [TENSOR1D / 2D / 3D Descriptors](tensor-descriptors.md) — the 4+4N layout of the 2D coeff / 3D x·dy·dx / 1D Gradients slots (2.3).
- [Access-Pattern Encoder Dispatch](ap-encoder-dispatch.md) — the `assignAccess<TENSORnD>` leaf bodies that pack each slot (2.8).
- BIR simulation — the `BNStats` two-Welford-triple numerical model and the `BNBackprop` `dx` datapath the encoded bundles drive (Part 7) *(planned)*.
- NKI norm kernels — the front-end `nl`/`nc` norm primitives that lower to this family (6.7.5) *(planned)*.
