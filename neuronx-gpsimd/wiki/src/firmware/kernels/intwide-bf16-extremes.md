# Gen-Bracket Extremes — MAVERICK `INT_WIDE` + SUNDA BF16 — `0xf3` · `0xf4` · `0x8a` · `0x8b` · `0x8c` · `0x8d` · `0x8f`

This page decodes the **two opposite ends of the GPSIMD opcode roster**: what the **newest**
generation (MAVERICK / NC‑v5) *added* at the very top of the opcode space, and what the **oldest**
generation (SUNDA / NC‑v2) *had and retired*.

- **The NEW end — MAVERICK `INT_WIDE` (`0xf3` / `0xf4`).** Two dedicated wide‑integer opcodes that
  promote a 32‑bit integer operation to a 64‑bit result, materialising it as an **explicit
  `(low32, high32)` destination pair**. `0xf3 TENSOR_TENSOR_INT_WIDE` is the tensor‑tensor form;
  `0xf4 TENSOR_SCALAR_INT_WIDE` is the tensor‑scalar form. They are the **last two real opcodes
  before `INVALID 0xff`** and exist **only** in the MAVERICK enum.
- **The OLD end — the SUNDA BF16 cluster (`0x8a` / `0x8b` / `0x8c` / `0x8d` / `0x8f`).** Five
  dtype‑specialised, 2×‑throughput packed‑BF16 fast‑path opcodes (TensorTensor add/mult/sub +
  TensorReduce add/max) that **only SUNDA carries** and that were **hard‑removed** at CAYMAN (NC‑v3)
  once the generic dtype dispatch could run BF16 directly.

The descriptor- and engine-level *views* of the surrounding machinery are elsewhere; this page is the
**instruction-word front door** for these seven opcodes — names, structs, dispatch surface, semantics,
dtype gates, per-generation presence, and the **add / retire lineage**.

Cross-links: the committed [Opcode Catalog Ledger](./opcode-catalog-ledger.md) (the 140-opcode roster),
the [DTYPE Model](./dtype-model.md) (the dtype enum these gate on), the
[Tensor-Tensor 64-bit Path](./tensor-tensor-64bit.md) (the native-`INT64`/`UINT64` dispatch that
`INT_WIDE` is *related to but distinct from*), the
[Batch-Norm Param Load](./batchnorm-paramload.md) (the `0x8e` op that sits *inside* the BF16 byte span
but is **not** a BF16 op), the [Correction Ledger](../../reference/correction-ledger.md), and the
forward-planned [Master Per-Generation Capability Matrix](../../generations/master-capability-matrix.md).

> **NOTE — provenance.** Every primary fact derives from the shipped customop-lib package
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`: the four per-gen arch-isa C headers under
> `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/` (the authoritative
> opcode / operand-struct / validator contract — `aws_neuron_isa_tpb_common.h` plus the four operand
> `*.h` files), the per-gen `instruction_mapping.json` (`struct2opcode` binding), a `gcc`
> `sizeof`/`offsetof` compile-verify against those headers, and the shipped device firmware container
> `…/c10/lib/libnrtucode_internal.so` (gen-image build-variant markers + the `S:` self-name dispatch
> tags). `v2..v4` (SUNDA / CAYMAN / MARIANA, and the v4+ sub-step MARIANA_PLUS) are **byte-grounded**;
> **`v5` MAVERICK is header-OBSERVED only** — the ISA contract is byte-exact, but the MAVERICK device
> interiors are not re-disassembled, so **every MAVERICK interior claim is flagged INFERRED**.

> **WALL — the `0xf3` body-depth wall (no MAVERICK POOL DEBUG image).** `libnrtucode_internal.so`
> ships **no `MAVERICK_NX_POOL_DEBUG` image**. MAVERICK POOL ships only `PERF`/`PROF`/`TEST` variants;
> MAVERICK is the **single (engine, generation) cell** whose POOL family is missing the `DEBUG`
> variant while every other generation keeps it (MAVERICK *DVE* `DEBUG` is present). The `DEBUG` image
> is the one that carries the kernel-name table, so a POOL `S:`-tag self-name for `0xf3` **cannot
> appear in this blob even if the kernel exists** — its absence here is a build-config artifact, **not**
> evidence the kernel is unimplemented. The MAVERICK `INT_WIDE` device body is therefore **not
> carved** on this page; the header-level contract is reproduced and the interior is marked
> **INFERRED**. `[HIGH/OBSERVED that no MAVERICK_NX_POOL_DEBUG marker exists; the implication MED/INFERRED]`

Confidence convention: `[HIGH/OBSERVED]` = read directly from a byte / header / `struct2opcode` /
compile-verify / firmware marker this pass; `[MED/INFERRED]` = reasoned over an OBSERVED fact;
`[…/CARRIED]` = re-used from a sibling firmware decode at its stated confidence without re-tracing the
artifact here.

---

## 1. The seven names + per-generation presence `[HIGH/OBSERVED]`

The opcode bytes and the per-gen `// Y` maintained-flag are read **byte-exact** from each generation's
`NEURON_ISA_TPB_OPCODE` enum in `aws_neuron_isa_tpb_common.h`. The enum identifier is the authoritative
mnemonic — *not* inferred from the opcode number.

| op     | enum identifier (`…OPCODE_…`)   | SUNDA v2 | CAYMAN v3 | MARIANA v4 | MAVERICK v5 | first gen | enum line     |
| ------ | ------------------------------- | :------: | :-------: | :--------: | :---------: | --------- | ------------- |
| `0x8a` | `TENSOR_TENSOR_ADD_BF16`        | **Y**    | —         | —          | —           | SUNDA     | `sunda:223`   |
| `0x8b` | `TENSOR_TENSOR_MULT_BF16`       | **Y**    | —         | —          | —           | SUNDA     | `sunda:224`   |
| `0x8c` | `TENSOR_REDUCE_ADD_BF16`        | **Y**    | —         | —          | —           | SUNDA     | `sunda:225`   |
| `0x8d` | `TENSOR_REDUCE_MAX_BF16`        | **Y**    | —         | —          | —           | SUNDA     | `sunda:226`   |
| `0x8f` | `TENSOR_TENSOR_SUB_BF16`        | **Y**    | —         | —          | —           | SUNDA     | `sunda:228`   |
| `0xf3` | `TENSOR_TENSOR_INT_WIDE`        | —        | —         | —          | **Y**       | MAVERICK  | `maverick:320`|
| `0xf4` | `TENSOR_SCALAR_INT_WIDE`        | —        | —         | —          | **Y**       | MAVERICK  | `maverick:321`|

Verification (`rg -n 'INT_WIDE\|BF16' …/aws_neuron_isa_tpb_common.h`, all four gens this pass):

- `TENSOR_TENSOR_INT_WIDE = 0xf3` and `TENSOR_SCALAR_INT_WIDE = 0xf4` appear **only** in the MAVERICK
  enum (`maverick:320`/`:321`); **zero** opcode hits in SUNDA / CAYMAN / MARIANA — *and* zero in the
  `arch-headers/mariana_plus/` (v4+) header set. The two are the last named opcodes before
  `NEURON_ISA_TPB_OPCODE_INVALID = 0xff` (`maverick:322`); `0xf0..0xf4` is
  `EXTENDED_INST 0xf0 · DMA_GATHER_TRANSPOSE 0xf1 · NONZERO_WITH_COUNT 0xf2 · TENSOR_TENSOR_INT_WIDE 0xf3 · TENSOR_SCALAR_INT_WIDE 0xf4`.
  `[HIGH/OBSERVED]`
- The five BF16 opcode names appear **only** in the SUNDA enum (`sunda:223`–`228`, all `// Y`); **zero**
  opcode hits in CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK. (The only "BF16" strings in later headers
  are SBUF-stride and output-alignment *comments*, never an opcode enumerator.) `[HIGH/OBSERVED]`

> **CORRECTION — the `0x8e` hole inside the BF16 byte span.** The task scopes the cluster as
> "`0x8a`–`0x8f`", but that is the byte **span**, not the op set. The byte `0x8e` in that span is
> **`BATCH_NORM_PARAM_LOAD2`**, *not* a BF16 op — and it is `// Y` **maintained in all four
> generations** (`sunda:227`, `cayman:225`, `mariana:230`, `maverick:233`), decoded on its own
> [Batch-Norm Param Load](./batchnorm-paramload.md) page. The actual SUNDA-only BF16 cluster is the
> **five** ops `{0x8a, 0x8b, 0x8c, 0x8d, 0x8f}`, not six. `[HIGH/OBSERVED]`

> **QUIRK — clean byte removal, no reuse.** After SUNDA, the bytes `0x8a/0x8b/0x8c/0x8d/0x8f` are
> simply **unassigned** in every later enum (the `0x8x` band in CAYMAN/MARIANA/MAVERICK contains only
> `0x8e BATCH_NORM_PARAM_LOAD2`). The bytes are **not** recycled for a different instruction, so the
> per-gen byte→mnemonic map stays globally consistent. `[HIGH/OBSERVED — `rg '= 0x8[a-f],'` on the
> three later enums returns only `0x8e`]`

---

## 2. The struct binding — `struct2opcode` + compile-verify (all 64 B) `[HIGH/OBSERVED]`

`instruction_mapping.json`'s `struct2opcode` table is the authoritative opcode→operand-struct binding.
The two `INT_WIDE` ops each live on a **dedicated, sole-member** MAVERICK struct that did not exist on
earlier gens; the five BF16 ops live on **two shared base structs** alongside their generic siblings.

| op           | operand struct (`NEURON_ISA_TPB_…`) | `struct2opcode` membership                     | `sizeof` |
| ------------ | ----------------------------------- | ---------------------------------------------- | :------: |
| `0xf3`       | `S2S2D2D2_TT_STRUCT`                | MAVERICK: sole member `TENSOR_TENSOR_INT_WIDE` | 64       |
| `0xf4`       | `S2D2D2_TS_WIDE_STRUCT`            | MAVERICK: sole member `TENSOR_SCALAR_INT_WIDE` | 64       |
| `0x8a/b/f`   | `S3S3D3_TT_STRUCT` (shared)        | SUNDA member; dropped CAYMAN+                   | 64       |
| `0x8c/d`     | `S4D4_TR_STRUCT` (shared)          | SUNDA member; dropped CAYMAN+                   | 64       |

**The shared-struct membership delta is the retirement, made measurable** (`jq` on all three mappings
this pass):

- `S3S3D3_TT_STRUCT`: **SUNDA = 8 members** → **CAYMAN / MAVERICK = 5**. The SUNDA member list is
  `[TENSOR_TENSOR_ARITH_OP, TENSOR_TENSOR_BITVEC_OP, COPY_PREDICATED, CAST_PREDICATED,
  BATCH_NORM_BACK_PROP, TENSOR_TENSOR_ADD_BF16, TENSOR_TENSOR_MULT_BF16, TENSOR_TENSOR_SUB_BF16]`; the
  CAYMAN list is exactly the **first five** — the **three dropped members are precisely the TT BF16
  trio** `{0x8a, 0x8b, 0x8f}`. `[HIGH/OBSERVED]`
- `S4D4_TR_STRUCT`: **SUNDA = 13 members** → **CAYMAN / MAVERICK = 11**. The two dropped members are
  exactly the reduce BF16 pair `TENSOR_REDUCE_ADD_BF16` (`0x8c`) and `TENSOR_REDUCE_MAX_BF16` (`0x8d`).
  `[HIGH/OBSERVED]`
- `S2S2D2D2_TT_STRUCT` and `S2D2D2_TS_WIDE_STRUCT` are **absent (`null`) from the SUNDA mapping** — the
  struct *and* its opcode were added together at MAVERICK. `[HIGH/OBSERVED]`

> **NOTE — this is the "8-vs-5" `S3S3D3_TT` delta, itemised.** The
> [Opcode Catalog Ledger](./opcode-catalog-ledger.md) and the prior `S3S3D3_TT` decode reported "SUNDA
> lists 8 opcodes on `S3S3D3_TT`, CAYMAN+ list 5". This page names the three dropped members: the TT
> BF16 trio. The companion reduce pair lives on a *different* base struct (`S4D4_TR`), so its 13→11
> drop is a separate (consistent) fact. `[HIGH/OBSERVED]`

Compile-verify this pass (`gcc -std=c11`, `offsetof`/`sizeof`; all four structs are exactly 64 B). The
sub-pattern types resolve to `MEM_PATTERN2D = 12 B`, `TENSOR3D = 16 B`, `TENSOR4D = 20 B`,
`DTYPE_PAIR = 1 B`, `IMM_VAL_INST_FIELD = 4 B`:

```
# MAVERICK (-I …/neuron_maverick_arch_isa/tpb)
NEURON_ISA_TPB_S2S2D2D2_TT_STRUCT     sizeof=64
   in0_in1_dtype +12   out0_out1_dtype +13   op +14   num_active_channels +15
   src0_mem_pattern +16 (2D,12B)   src1_mem_pattern +28   dst0_mem_pattern +40   dst1_mem_pattern +52
NEURON_ISA_TPB_S2D2D2_TS_WIDE_STRUCT  sizeof=64
   in_imm_dtype +12    out0_out1_dtype +13   op +14   num_active_channels +15
   src_mem_pattern +16 (2D,12B)    dst0_mem_pattern +28   dst1_mem_pattern +40
   imm_src +52         imm +56 (IMM_VAL_INST_FIELD,4B)

# SUNDA (-I …/neuron_sunda_arch_isa/tpb)
NEURON_ISA_TPB_S3S3D3_TT_STRUCT       sizeof=64
   in0_in1_dtype +12   out_dtype +13   op +14   num_active_channels +15
   src0_mem_pattern +16 (3D,16B)   src1_mem_pattern +32   dst_mem_pattern +48
NEURON_ISA_TPB_S4D4_TR_STRUCT         sizeof=64
   src_mem_pattern +12 (4D,20B)    in_dtype +32   out_dtype +33   num_active_channels +34
   negated +35   op +36   op_dim +37   mask_enable +38   reserved1[5] +39   dst_mem_pattern +44 (4D,20B)
```

> **GOTCHA — the `INT_WIDE` "tell" is the 2-D pattern, and it is forced.** `S3S3D3_TT` uses three
> **3-D** patterns (`TENSOR3D`, 16 B each = 48 B) and one destination. `S2S2D2D2_TT` instead uses
> **four 2-D** patterns (`MEM_PATTERN2D`, 12 B each = 48 B) to fit a **second destination** into the
> same 64-byte instruction word: `4 × 12 B = 48 B` + a 16-byte header/dtype/op preamble = 64 B exactly.
> The header states it verbatim: *"Uses 2D memory patterns (12 bytes each) to fit in 64-byte
> instruction. Does NOT support indirect addressing."* The second destination is the entire point of
> the op; the 2-D demotion is the budget cost of carrying it. `[HIGH/OBSERVED —
> `s2s2d2d2_tt.h:23,35-38` + compile-verify]`

---

## 3. The two MAVERICK `INT_WIDE` ops — newest-gen additions

### 3.1 `0xf3 TENSOR_TENSOR_INT_WIDE` — POOL; dual-dest 64-bit int TT

**Semantics (header `s2s2d2d2_tt.h:13-25`, verbatim contract).** An element-wise **integer** operation
of two source tensors producing a 64-bit output **split into two 32-bit destination tensors**:

> *"dst0: low 32 bits (result for add/subtract/multiply, or low bits for shifts); dst1: high 32 bits
> (carry for add, borrow for subtract, high bits for multiply/shifts). Supports 2x perf mode."*

Per output element `e`, the datapath computes (annotated C; symbols are the real validator/header names):

```c
/* 0xf3 TENSOR_TENSOR_INT_WIDE — S2S2D2D2_TT.
 * op ∈ is_valid_tt_int_wide_op (s2s2d2d2_tt.h:81-90):
 *   { AddInt, SubtractInt, MultInt, MultUint,
 *     ArithShiftLeft, ArithShiftRight, LogicalShiftLeft, LogicalShiftRight }   */
for (e = 0; e < same_element_count_m2d(src0, src1, dst0, dst1); ++e) {
    int64_t full = widen64(src0[e]) <op> widen64(src1[e]);  /* 32b ints widened to 64b */
    dst0[e] = (uint32_t)( full        & 0xFFFFFFFFu);        /* low 32  : result / low bits  */
    dst1[e] = (uint32_t)((full >> 32) & 0xFFFFFFFFu);        /* high 32 : carry/borrow/high  */
}
```

The defining new capability vs the plain `TensorTensorArithOp` (`0x41`, on `S3S3D3_TT`): the **high
half / carry / borrow / high product is captured explicitly in a second destination**, rather than
discarded by 32-bit wrap or folded internally by a native-`INT64` dtype (see §5). `[HIGH/OBSERVED]`

**AluOp byte values** (MAVERICK `aws_neuron_isa_tpb_common.h`, read directly this pass) —
`is_valid_tt_int_wide_op` admits exactly these eight, no others:

| AluOp            | enum (`…ALU_OP_…`)    | byte   | note                                              |
| ---------------- | --------------------- | ------ | ------------------------------------------------- |
| `AddInt`         | `ADD_INT`             | `0xC4` | *"bit 7:6 == 0x3 to indicate integer engine compute dtype"* / shared signed+unsigned |
| `MultInt`        | `MULT_INT`            | `0xC5` | signed int                                        |
| `SubtractInt`    | `SUBTRACT_INT`        | `0xC6` | shared signed+unsigned                            |
| `MultUint`       | `MULT_UINT`           | `0xDB` | unsigned int                                      |
| `ArithShiftLeft` | `ARITH_SHIFT_LEFT`    | `0x02` |                                                   |
| `ArithShiftRight`| `ARITH_SHIFT_RIGHT`   | `0x03` |                                                   |
| `LogicalShiftLeft`| `LOGICAL_SHIFT_LEFT` | `0x10` |                                                   |
| `LogicalShiftRight`| `LOGICAL_SHIFT_RIGHT`| `0x11`|                                                   |

> **NOTE — the `0xCx` opcodes are the integer-datapath marker.** The three int-ALU ops carry the
> header comment *"bit 7:6 == 0x3 to indicate integer engine compute dtype"* (`0xC4 = 0b11000100`).
> This is the integer-engine routing bit the ALU-op matrix uses across the corpus, not a quirk of this
> op. `[HIGH/OBSERVED]`

**Per-op signedness constraints** (`tt_int_wide_op_dtype_constraints`, `s2s2d2d2_tt.h:92-117`):

- `AddInt` / `SubtractInt`: **both inputs must be unsigned int** (header: *"signed overflow is
  undefined"*).
- `MultInt`: both inputs **signed**. `MultUint`: both inputs **unsigned**.
- **shift ops**: the shift-amount input (`dtype_hi`) must be **unsigned int**.
- **non-shift ops**: the low destination (`out0_out1_dtype.dtype_lo`) must be a valid 32-bit int dtype
  (`is_valid_32b_int_dtype` = `{INT32, UINT32}`), and the high destination's signedness must **match
  the source signedness** (`tt_int_wide_dst1_signedness_matches_src`). `[HIGH/OBSERVED]`

**Dtype matrix** (`is_valid_int_dtype_datapath`, `common.h:3309-3316`): both inputs and both outputs
are gated to **exactly `{INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5, INT32 0x8, UINT32 0x9}`** —
8/16/32-bit ints only. **Not `INT64`/`UINT64`.** The 64-bit-ness lives in the **output width** (the
dual 32-bit destination), never in the input dtype. `[HIGH/OBSERVED]`

**Dispatch surface = POOL** `[validator constant HIGH/OBSERVED; engine attribution MED/INFERRED]`. The
validator `is_valid_tensor_tensor_int_wide` (`s2s2d2d2_tt.h:50-79`) gates the channel range against
`POOLING_NUM_CHANNELS` (`has_valid_active_channel_range_with_tile(..., POOLING_NUM_CHANNELS, ...)`),
versus `DVE_NUM_CHANNELS` for `0xf4`. All four patterns are `SBUF`-only / `PSUM`-forbidden
(`mem2d_valid(..., AllowedInPSUM::False, AllowedInSBUF::True)`), with `same_element_count_m2d` enforced
across `src0 ≡ src1 ≡ dst0 ≡ dst1`. The validator decomposition is the dispatch chain:

```
opcode 0xf3 (header)
  └─ instruction_mapping.json: S2S2D2D2_TT  (maverick, sole member)
       └─ is_valid_tensor_tensor_int_wide:
            int-datapath dtype on both src + both dst (is_valid_int_dtype_datapath)
            AluOp ∈ the 8-op int/shift set (is_valid_tt_int_wide_op)
            per-op signedness (tt_int_wide_op_dtype_constraints)
            4× check_m2d_active_channels + 4× mem2d_valid (SBUF only, NOT PSUM)
            same_element_count_m2d across all four patterns
            POOLING_NUM_CHANNELS channel range  ⇒ POOL
            └─ MAVERICK POOL TensorTensor int-wide body  [INFERRED — not carved; see WALL]
```

> **WALL — `0xf3` interior is INFERRED.** No `S: …IntWide` self-name exists anywhere in
> `libnrtucode_internal.so` (zero `IntWide` hits across the whole blob; `2428` `S:` dispatch tags
> total, so the namespace is well-populated and the absence is meaningful — not a parse failure). But
> the blob ships **no `MAVERICK_NX_POOL_DEBUG` image**, so a POOL self-name for `0xf3` **could not
> surface here even if it exists**. The MAVERICK POOL funcVA and Xtensa body are therefore **not
> resolved**; the header-level contract above is the load-bearing record and the device interior is
> flagged INFERRED. `[HIGH/OBSERVED — marker-absent, exhaustive `rg`; the interior MED/INFERRED]`

### 3.2 `0xf4 TENSOR_SCALAR_INT_WIDE` — DVE; dual-dest 64-bit int TS

The scalar sibling of `0xf3`: the second operand is a **scalar** (immediate or pointer), not a tensor.
Same dual-dest `(low32, high32)` model (header `s2d2d2_ts_wide.h:13-24`, *"Specialized … producing
64-bit output split into two 32-bit destination tensors"*).

```c
/* 0xf4 TENSOR_SCALAR_INT_WIDE — S2D2D2_TS_WIDE.
 * AluOp set = identical 8 ops as 0xf3 (is_valid_ts_int_wide_op, s2d2d2_ts_wide.h:82-91).
 * scalar = imm resolved per imm_src (is_valid_ts_wide_immediate):
 *   InstructionImmediate (inline 4B) | PointerImmediate (SBUF ptr, dtype-aligned to dtype_hi)
 *   | RegPtrImmediate (register-held ptr).                                                 */
for (e = 0; e < same_element_count_m2d(src, dst0, dst1); ++e) {
    int64_t full = widen64(src[e]) <op> widen64(scalar);
    dst0[e] = (uint32_t)( full        & 0xFFFFFFFFu);
    dst1[e] = (uint32_t)((full >> 32) & 0xFFFFFFFFu);
}
```

**Constraints** (`ts_int_wide_op_dtype_constraints`, `s2d2d2_ts_wide.h:93-103`): the tensor input and
the immediate must have the **same signedness** (`input_imm_same_signedness`); `AddInt`/`SubtractInt`/
`MultUint` require **both unsigned** (`ts_int_wide_is_unsigned_only_op`); `MultInt` requires **both
signed**; for shift ops the shift-amount (`dtype_hi`) is unsigned, and for an inline shift the
immediate value must fit **0–31** — `ts_wide_shift_imm_value_check`:
`(imm.imm_bitvec_uint32 & 0xFFFFFFE0) == 0` (the 5-bit shift-range mask). `[HIGH/OBSERVED]`

**Dtype matrix**: input + immediate (`in_imm_dtype.dtype_lo`/`dtype_hi`) and both outputs are each
`is_valid_int_dtype_datapath` (8/16/32 int), same `INT64`-exclusion as `0xf3`. `[HIGH/OBSERVED]`

**Dispatch surface = DVE** `[validator constant HIGH/OBSERVED; engine attribution MED/INFERRED]`.
`is_valid_tensor_scalar_int_wide` (`s2d2d2_ts_wide.h:53-80`) gates the channel range against
`DVE_NUM_CHANNELS` (versus `POOLING_NUM_CHANNELS` for `0xf3`); three `mem2d_valid` checks (1 src read +
2 dst writes, `SBUF`-only / `PSUM`-forbidden); `same_element_count_m2d(src,dst0)` &
`(src,dst1)`; the immediate-source check (`is_valid_ts_wide_immediate`); `ts_wide_reserved_zero` over
the two reserved gaps (`reserved0[3]` at +53, `reserved1[4]` at +60).

```
opcode 0xf4 ── S2D2D2_TS_WIDE ── is_valid_tensor_scalar_int_wide
   [int-datapath dtype on src+imm+both dst; AluOp ∈ 8-op set; same-signedness;
    is_valid_ts_wide_immediate (Inst | Pointer | RegPtr); ts_wide_shift_imm_check (0-31);
    3× mem2d_valid SBUF-only; same_element_count(src,dst0)&(src,dst1); DVE_NUM_CHANNELS]
   ⇒ MAVERICK DVE TensorScalar int-wide body  [INFERRED]
```

> **GOTCHA — `0xf4`'s absence is *informative*, unlike `0xf3`'s.** The `MAVERICK_NX_DVE_DEBUG` image
> **is** present (full `DRAM_/EXTRAM_/IRAM_/SRAM_` region set) and **does** carry other DVE compute
> self-names — `FindIndex8`, `DveReadAccumulator`, `DveReadIndices` each appear **4×**. Yet there is
> **no `S: …IntWide`** name in it. Two readings cannot be disambiguated from this blob alone: (a) the
> kernel is folded inline into the generic TensorScalar machinery with no dedicated self-name (the same
> shape as the `0x93` Transpose-TensorScalar fold), or (b) the body is not yet present in *this* ucode
> revision (header-defined, body pending — consistent with it being the very newest op). The
> disambiguation is **LOW / NOT CLAIMED**; the `DEBUG`-image-present-but-name-absent fact is
> `[HIGH/OBSERVED]`.

---

## 4. The five SUNDA BF16 ops — oldest-gen / retired

**The unifying semantic (all five): a dtype-specialised, 2×-throughput packed-BF16 fast path for a
generic op.** Both base-struct headers state it verbatim:

- `s3s3d3_tt.h:20-21`: *"(NC_v2 only) TensorTensorAddBf16 / TensorTensorMultBf16 / TensorTensorSubBf16
  … element-wise operation of two source tensors with bf16 operand (2x faster than regular
  TensorTensorArithOp)."*
- `s4d4_tr.h:17-18`: *"TensorReduceAddBf16 — same as TensorReduceOp, but at 2x speed, only for BF16,
  only addition."* / *"TensorReduceMaxBf16 — … 2x speed, only for BF16, only maximum."*

The 2× comes from **packing two BF16 values into each 32-bit lane**: the validators read the source
patterns as `Dtype::UINT32` (a packed pair) and **require the inner dimension to be even and
unit-stride**. `[HIGH/OBSERVED]`

### 4.1 The TT trio — `0x8a ADD_BF16` / `0x8b MULT_BF16` / `0x8f SUB_BF16` (`S3S3D3_TT`)

```c
/* 0x8a/0x8b/0x8f on S3S3D3_TT — is_valid_tensor_tensor_bf16 (s3s3d3_tt.h:116-134).
 * The OPCODE encodes the op; the AluOp field is PINNED to match it
 * (has_valid_tensor_tensor_bf16_op, s3s3d3_tt.h:180-186):                           */
for (e = 0; e < element_count; ++e) {              /* two BF16 per 32b lane → 2× lanes */
    bf16 a = src0_bf16[e], b = src1_bf16[e];       /* src patterns read as Dtype::UINT32 */
    switch (opcode) {
        case TensorTensorAddBf16:  dst[e] = a + b;  break; /* op == AluOp::Add  (0x04) */
        case TensorTensorMultBf16: dst[e] = a * b;  break; /* op == AluOp::Mult (0x06) */
        case TensorTensorSubBf16:  dst[e] = a - b;  break; /* op == AluOp::Subtract (0x05) */
    }
}
```

Each is the corresponding `TensorTensorArithOp` (`0x41`) **restricted to BF16 inputs**, run at 2×
throughput. Unlike the generic `0x41` (where the `AluOp` field *selects* the op), here the **opcode
encodes the op**, so the `AluOp` field is constrained to match (`AddBf16↔Add`, `MultBf16↔Mult`,
`SubBf16↔Subtract`). `[HIGH/OBSERVED]`

**Validators** (`s3s3d3_tt.h:116-134`, `:155-186`, `:214-247`):

- `has_valid_tensor_tensor_bf16_in_dtype` (`:244-247`): **both** inputs hard-pinned to
  `Dtype::BFLOAT16` (`0x6`).
- `is_valid_dtype(out_dtype, DtypeAllowFP32R::True)` — the output may be **any valid dtype, including
  `FP32R`** (a BF16 op commonly casts up on write).
- `tensor_tensor_bf16_access_pattern_check` (`:214-226`): `src{0,1}.num_elem[0] % 2 == 0` and
  `step_elem[0] == 1` (the packed-pair constraint — *"We read a pair of BF16 values per src port at
  once → num elems should be even"*), with higher-dim steps even.
- `tensor3d_valid(src{0,1}, Dtype::UINT32, …)` and `tensor3d_valid(dst, out_dtype, …)` — the src is
  validated as `UINT32` because two BF16 are packed per 4-byte word. `[HIGH/OBSERVED]`

**Dispatch surface = POOL** `[MED/INFERRED]` — by analogy to the base `TensorTensor` op (`0x41` is POOL
across the corpus) on the same `S3S3D3_TT` surface; no device self-name reachable (see §6).

> **QUIRK — BF16 TT permits PSUM; `INT_WIDE` forbids it.** The BF16 TT patterns are validated
> `AllowedInPSUM::True` (`tensor3d_valid(..., AllowedInPSUM::True, AllowedInSBUF::True)`), so the BF16
> fast path can read/write PSUM. The MAVERICK `INT_WIDE` ops are the opposite — strictly `SBUF`-only
> (`AllowedInPSUM::False`). A subtle but real surface difference between the two ends of the roster.
> `[HIGH/OBSERVED]`

### 4.2 The reduce pair — `0x8c REDUCE_ADD_BF16` / `0x8d REDUCE_MAX_BF16` (`S4D4_TR`)

```c
/* 0x8c/0x8d on S4D4_TR — is_valid_tensor_reduce_bf16 (s4d4_tr.h:79-86).
 * Opcode encodes the reduce op; op_dim (TENSOR_SUBDIM) selects the axis (X/XY/XYZ/XYZW). */
for each output position p along the kept axes:
    if (opcode == TensorReduceAddBf16)  dst[p] = Σ over op_dim of src_bf16[...];   /* reduce-SUM */
    if (opcode == TensorReduceMaxBf16)  dst[p] = max over op_dim of src_bf16[...]; /* reduce-MAX */
    /* negated (S4D4_TR +35) optionally negates — these are classed tensor_reduce_arith */
```

Each is the corresponding `TensorReduceArithOp` (`0x42`) restricted to BF16, at 2× throughput.
**Validators** (`s4d4_tr.h:79-86`, `:129-134`, `:167-179`):

- `is_valid_tensor_reduce_common` — `s4d4_tr_reserved_zero`, `mask_enable_zero`,
  `is_valid_dtype(in_dtype, DtypeAllowFP32R::False)`, `is_valid_dtype(out_dtype,
  DtypeAllowFP32R::True)`, `POOLING_NUM_CHANNELS` channel range.
- `has_valid_tensor_reduce_bf16_in_dtype` (`:172-174`): `in_dtype == Dtype::BFLOAT16`.
- `tensor_reduce_bf16_access_pattern_check` (`:176-179`): 4-byte-aligned src, `num_elem[0] % 2 == 0`,
  `step_elem[0] == 1` (*"pairs of bf16 in 4 bytes"*).
- Both ops are listed in `tensor_reduce_arith` (`:129-134`) — so the `negated` field applies.
  `[HIGH/OBSERVED]`

**Dtype matrix**: `in = BFLOAT16` (`0x6`) hard-pinned; `out` is any valid dtype incl `FP32R` (a BF16
reduce commonly accumulates to FP32). **Dispatch surface = POOL** (the `S4D4_TR TensorReduce` surface,
`POOLING_NUM_CHANNELS`) `[POOL constant HIGH/OBSERVED; engine attribution MED/INFERRED]`.

---

## 5. `INT_WIDE` vs the native-`INT64` 64-bit path (§ relation) `[HIGH/OBSERVED — mechanism]`

The natural question — *is `INT_WIDE` just the MAVERICK spelling of the 64-bit integer path?* —
resolves to **related in goal, distinct in mechanism**. Two different ways to get a 64-bit integer
result coexist:

| axis            | native-`INT64`/`UINT64` path ([Tensor-Tensor 64-bit](./tensor-tensor-64bit.md)) | MAVERICK `INT_WIDE` (`0xf3`/`0xf4`) |
| --------------- | ------------------------------------------------- | ------------------------------------ |
| host opcode     | the regular `0x41 TENSOR_TENSOR_ARITH_OP` (a sub-case) | dedicated `0xf3` / `0xf4`        |
| host struct     | `S3S3D3_TT` (single 3-D dest)                     | `S2S2D2D2_TT` / `S2D2D2_TS_WIDE` (dual 2-D dest) |
| input dtype     | native `INT64 0xC` / `UINT64 0x1` (`is_valid_64b_int_dtype`) | 8/16/32-bit ints (`is_valid_int_dtype_datapath`) — **never 64-bit** |
| 64-bit output   | one wide element emulated as a lo32+hi32 **even-register pair** on **one** dest | result **split across two separate 32-bit dest tensors** (`dst0` low / `dst1` high) |
| high half       | folded away internally (the dtype *is* 64-bit)    | **exposed as a first-class output** (carry/borrow/high-product) |
| per-gen         | gen-wide (sub-case of `0x41`)                     | MAVERICK-only                        |

The gates make the distinction byte-exact: `is_valid_int_dtype_datapath` (`common.h:3309-3316`) admits
**only** `{INT8, UINT8, INT16, UINT16, INT32, UINT32}`, while `is_valid_64b_int_dtype`
(`common.h:3345-3347`) is the disjoint `{INT64, UINT64}`. `INT_WIDE` inputs are 32-bit; the 64-bit-ness
is **output width**, not input dtype. `[HIGH/OBSERVED]`

> **NOTE — the inferred use-case.** Exposing the high half / carry / borrow / wide product as an
> explicit output is exactly what a kernel needs for **32×32→64 widening multiply**, **explicit
> carry/borrow add/sub chains**, and **shifts that cross a 64-bit boundary** — without committing
> operands to the heavier native-`INT64` even-register-pair read/write model. The capability is
> `[HIGH/OBSERVED]` from the two structs' dtype gates and the `dst0`/`dst1` semantics; the specific
> use-case attribution is `[MED/INFERRED]`. The `s3s3d3_tt.h` comment *"64-bit int allowed on Cayman+
> POOL"* corroborates that 64-bit integer compute existed before MAVERICK — so `INT_WIDE` is an
> **additional** way to surface width, not the first one. `[HIGH/OBSERVED for the comment]`

---

## 6. Dispatch-surface discriminator — applied, honestly `[engine attributions MED/INFERRED]`

The standard discriminator (POOL `kernel_info_table` membership ⇒ POOL software kernel; otherwise the
`S:` self-name multiplicity selects the engine) **cannot run cleanly on any of these seven**, because
the relevant device images are not in the blob:

| op     | in a POOL `kernel_info_table`? | self-name in blob                          | validator channel constant | ⇒ engine        |
| ------ | ------------------------------ | ------------------------------------------ | -------------------------- | --------------- |
| `0xf3` | no (maverick-only)             | none — **no MAVERICK_NX_POOL_DEBUG image**  | `POOLING_NUM_CHANNELS`     | POOL `[MED]`    |
| `0xf4` | no (maverick-only)             | none — DVE DEBUG present, no `IntWide` name | `DVE_NUM_CHANNELS`         | DVE  `[MED]`    |
| `0x8a` | no (SUNDA RELEASE)             | none — SUNDA RELEASE string-stripped        | (`S3S3D3_TT` TT surface)   | POOL `[MED]`    |
| `0x8b` | no                             | none                                        | (`S3S3D3_TT`)              | POOL `[MED]`    |
| `0x8c` | no                             | none                                        | `POOLING_NUM_CHANNELS`     | POOL `[MED]`    |
| `0x8d` | no                             | none                                        | `POOLING_NUM_CHANNELS`     | POOL `[MED]`    |
| `0x8f` | no                             | none                                        | (`S3S3D3_TT`)              | POOL `[MED]`    |

> **GOTCHA — `POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128`.** Both channel constants are `128U`
> (`common.h:36`/`:37`), so the constant's **value** does not distinguish POOL from DVE. The
> discriminating signal is the validator's **choice of constant name** — `0xf3`/`0x8c`/`0x8d` name
> `POOLING_NUM_CHANNELS`, `0xf4` names `DVE_NUM_CHANNELS`. The split TT→POOL / TS→DVE also mirrors the
> corpus-wide "TensorTensor is POOL, TensorScalar can be DVE" convention. This is why every engine
> attribution here is `[MED/INFERRED]`, not `[HIGH]`. `[constants HIGH/OBSERVED; attribution MED/INFERRED]`

The three independent reasons the self-name surface is unavailable: (1) SUNDA ships only **RELEASE**
images (`SUNDA_NX_POOL_RELEASE` / `SUNDA_NX_DVE_RELEASE`), which are string-stripped — zero `Bf16`
self-names anywhere; (2) the **missing `MAVERICK_NX_POOL_DEBUG`** image blanks any `0xf3` POOL name;
(3) the present `MAVERICK_NX_DVE_DEBUG` image has a genuine no-dedicated-name for `0xf4`. The marker set
read this pass (`rg -a -o "(SUNDA|CAYMAN|MARIANA|MAVERICK)_NX_(POOL|DVE)_[A-Z]+" | sort -u`) confirms
the asymmetry:

```
SUNDA    : POOL_RELEASE  DVE_RELEASE                              (RELEASE only)
CAYMAN   : POOL_DEBUG POOL_PERF POOL_PROF POOL_TEST  DVE_{DEBUG,PERF,PROF,TEST}
MARIANA  : POOL_DEBUG POOL_PERF POOL_PROF POOL_TEST  DVE_{DEBUG,PERF,PROF,TEST}
MAVERICK :           POOL_PERF POOL_PROF POOL_TEST  DVE_{DEBUG,PERF,PROF,TEST}   ← no POOL_DEBUG
```

> **NOTE — build-variant taxonomy.** Each dev generation actually ships a full
> `{DEBUG, PERF, PROF, TEST}` build-variant matrix across five engine families (`ACT/DVE/PE/POOL/SP`),
> each split into memory-region suffixes; SUNDA ships `RELEASE` only. The precise, defensible fact for
> the `0xf3` wall is **not** "DEBUG vs RELEASE" but: **`MAVERICK_NX_POOL_DEBUG` is the single
> (engine, generation) cell missing the DEBUG variant** while MAVERICK POOL keeps `PERF`/`PROF`/`TEST`
> and MAVERICK DVE keeps `DEBUG`. `[HIGH/OBSERVED — exhaustive marker sweep; firmware blob 10,276,288 B]`

---

## 7. The add / retire lineage — the gen-bracket synthesis

### 7.1 The retire — SUNDA BF16, dropped at CAYMAN (NC-v3)

- **WHAT:** five dedicated BF16 opcodes (TT add/mult/sub on `S3S3D3_TT`; reduce add/max on `S4D4_TR`),
  each a 2×-throughput packed-BF16 fast path for a generic arith/reduce op. `[HIGH/OBSERVED]`
- **HOW:** the opcodes were **removed from the CAYMAN+ enums**, and the shared host structs shed exactly
  those members (`S3S3D3_TT` 8→5, `S4D4_TR` 13→11), leaving the **generic** `TensorTensor` /
  `TensorReduce` ops to handle BF16 via the ordinary dtype dispatch. The removal is **clean**: zero
  `BF16` opcode strings in any CAYMAN/MARIANA/MAVERICK enum *and* zero `BF16` keys in their
  `struct2opcode` — enum and mapping both purged, with no byte reuse. `[HIGH/OBSERVED]`
- **WHY (MED/INFERRED):** the generalised dtype enum + dtype-dispatch makes BF16 (`0x6`) just another
  value the generic op accepts (the [DTYPE Model](./dtype-model.md) lists BF16 as fully accepted across
  the generic TensorTensor/TensorReduce ops, and the generic ALU path already uses dtype-parameterised
  kernels including a 2×-packed half-precision kernel). A per-op BF16 opcode therefore became
  **redundant**. `[retire FACTS HIGH/OBSERVED; the "folded into the generic dtype dispatch" WHY MED/INFERRED]`

> **CONTRAST — a *used* fast path the arch outgrew, not a never-finished stub.** This is the same
> retirement *shape* as the SUNDA-only DUAL-ptr twins `0x87`/`0x88` (gone CAYMAN+), but with a sharper
> edge: the DUAL twins were `// n` (dormant — ucode existed, not maintained) on SUNDA, whereas the five
> BF16 ops were `// Y` (**maintained**) on SUNDA. They were a real, used 2× fast path the architecture
> generalised past — and removed **cleanly** (the DUAL twins, by contrast, left a stale `S4D4_TSM`
> JSON binding behind). `[HIGH/OBSERVED for the `// Y` flags + the clean-removal grep]`

### 7.2 The add — MAVERICK `INT_WIDE`, new at NC-v5

- **WHAT:** two dedicated wide-integer opcodes (`0xf3 TENSOR_TENSOR_INT_WIDE` → POOL;
  `0xf4 TENSOR_SCALAR_INT_WIDE` → DVE), each promoting a 32-bit int op to a 64-bit result captured as
  **two 32-bit dest tensors** (low/high). `[HIGH/OBSERVED]`
- **HOW:** appended at the **top** of the MAVERICK opcode enum (`0xf3`/`0xf4`, the last two before
  `INVALID 0xff`), each on a **new dedicated struct** (`S2S2D2D2_TT` / `S2D2D2_TS_WIDE`) absent on
  earlier gens. The new structs use **2-D** (not 3-D) memory patterns specifically to fit the **second
  destination** into the 64-byte frame. `[HIGH/OBSERVED]`
- **WHY (MED/INFERRED):** exposes the high half / carry / borrow / wide product of integer ops as a
  first-class output — a capability the pre-existing native-`INT64`/`UINT64` dtype path (single dest,
  even-register-pair) did **not** surface. It lets a kernel do 32×32→64 widening multiply, explicit
  carry/borrow chains, and 64-bit-boundary shifts without promoting operands to the heavier native-`INT64`
  model. `[capability HIGH/OBSERVED from the dtype gates; use-case MED/INFERRED]`

### 7.3 The arc

The opcode roster's two ends capture **opposite design moves**:

- **The OLD end (SUNDA)** solved a *throughput* problem with **dtype-specialised** opcodes, then the ISA
  **generalised** dtype handling and **retired** them — *specialisation → generalisation*, removed
  cleanly from enum and mapping.
- **The NEW end (MAVERICK)** solved a *precision/width* problem by **adding** a dedicated capability the
  generic ops and the existing native-`INT64` path could not express — *generalisation → targeted
  extension*, grafted on top.

The old end pruned a dtype hack the general dispatch **absorbed**; the new end grafted a width capability
the general dispatch **lacked**. `[structural facts HIGH/OBSERVED; the design-intent narrative MED/INFERRED]`

---

## 8. Per-opcode summary table — name → struct → per-gen presence `[HIGH/OBSERVED]`

| op     | enum name (`…OPCODE_…`)    | struct (`NEURON_ISA_TPB_…`) | sizeof | SUNDA | CAYMAN | MARIANA | MAVERICK | engine        | class                                  | enum anchor   |
| ------ | ------------------------- | --------------------------- | :----: | :---: | :----: | :-----: | :------: | ------------- | -------------------------------------- | ------------- |
| `0xf3` | `TENSOR_TENSOR_INT_WIDE`  | `S2S2D2D2_TT` (sole)        | 64     | —     | —      | —       | **Y**    | POOL `[MED]`  | 64-bit-result int TT (dual 32-b dst)   | `maverick:320`|
| `0xf4` | `TENSOR_SCALAR_INT_WIDE`  | `S2D2D2_TS_WIDE` (sole)     | 64     | —     | —      | —       | **Y**    | DVE  `[MED]`  | 64-bit-result int TS (dual 32-b dst)   | `maverick:321`|
| `0x8a` | `TENSOR_TENSOR_ADD_BF16`  | `S3S3D3_TT` (shared)        | 64     | **Y** | —      | —       | —        | POOL `[MED]`  | 2×-throughput packed-BF16 TT add       | `sunda:223`   |
| `0x8b` | `TENSOR_TENSOR_MULT_BF16` | `S3S3D3_TT` (shared)        | 64     | **Y** | —      | —       | —        | POOL `[MED]`  | 2×-throughput packed-BF16 TT mult      | `sunda:224`   |
| `0x8c` | `TENSOR_REDUCE_ADD_BF16`  | `S4D4_TR` (shared)          | 64     | **Y** | —      | —       | —        | POOL `[MED]`  | 2×-throughput packed-BF16 reduce-sum   | `sunda:225`   |
| `0x8d` | `TENSOR_REDUCE_MAX_BF16`  | `S4D4_TR` (shared)          | 64     | **Y** | —      | —       | —        | POOL `[MED]`  | 2×-throughput packed-BF16 reduce-max   | `sunda:226`   |
| `0x8f` | `TENSOR_TENSOR_SUB_BF16`  | `S3S3D3_TT` (shared)        | 64     | **Y** | —      | —       | —        | POOL `[MED]`  | 2×-throughput packed-BF16 TT sub       | `sunda:228`   |
| `0x8e` | `BATCH_NORM_PARAM_LOAD2`  | (see [paramload](./batchnorm-paramload.md)) | 64 | **Y** | **Y** | **Y** | **Y**    | —             | **NOT BF16** — maintained all gens     | `sunda:227`   |

(`0x8e` is included to make the byte-span correction explicit: it sits *inside* `0x8a`–`0x8f` but is a
different, all-gen-maintained instruction.)

---

## 9. Honesty ledger

**HIGH / OBSERVED (read directly this pass):**

- All seven opcode names + per-gen `// Y` flags, byte-exact from the four `aws_neuron_isa_tpb_common.h`
  enums (SUNDA `223`–`228`; MAVERICK `320`/`321`). `INT_WIDE` names appear in MAVERICK only and zero in
  SUNDA/CAYMAN/MARIANA/MARIANA_PLUS; BF16 names in SUNDA only and zero later.
- The `0x8e = BATCH_NORM_PARAM_LOAD2` hole (YYYY-maintained, not BF16); the clean byte removal (no reuse).
- All struct bindings via `instruction_mapping.json`: `0xf3→S2S2D2D2_TT`, `0xf4→S2D2D2_TS_WIDE`
  (MAVERICK, sole members, `null` in SUNDA); `0x8a/0x8b/0x8f→S3S3D3_TT`, `0x8c/0x8d→S4D4_TR` (SUNDA).
  The `S3S3D3_TT` 8→5 and `S4D4_TR` 13→11 membership deltas (the retirement, itemised).
- All four structs compile-verified 64 B with the exact offsets in §2; `MEM_PATTERN2D = 12 B`
  (the `4 × 12 = 48 B` dual-dest packing rationale).
- Semantics from verbatim header pseudocode: the `dst0=low`/`dst1=high(carry/borrow/high)` `INT_WIDE`
  model, its 8-op AluOp set, per-op signedness, the shift-imm 0–31 mask; the BF16 "2× faster"
  packed-pair fast path, the `BFLOAT16`-pinned inputs, the even/unit-stride access pattern, the
  opcode-encodes-the-op rule.
- AluOp byte values (AddInt `0xC4`, MultInt `0xC5`, SubtractInt `0xC6`, MultUint `0xDB`, shifts
  `0x02/0x03/0x10/0x11`; BF16 Add `0x04` / Mult `0x06` / Subtract `0x05`).
- `is_valid_int_dtype_datapath` = `{I/U 8/16/32}` (not 64-bit) ⇒ `INT_WIDE` inputs are 32-bit, the
  64-bit-ness is output width — disjoint from `is_valid_64b_int_dtype = {INT64, UINT64}`.
- Channel constants `POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128`.
- Firmware gen-image markers: SUNDA RELEASE-only; CAYMAN/MARIANA full POOL+DVE matrix; MAVERICK DVE
  `DEBUG` present but **no `MAVERICK_NX_POOL_DEBUG`**; zero `IntWide`/`Bf16` self-names (`2428` `S:`
  tags total); `FindIndex8`/`DveReadAccumulator`/`DveReadIndices` = 4× each in MAVERICK DVE; blob
  size 10,276,288 B.

**MED / INFERRED (attributed this pass):**

- Engine attributions (`0xf3` + the five BF16 → POOL; `0xf4` → DVE) — from the validator channel
  constant + shared-struct analogy, **not** a self-name multiplicity (unavailable here).
- The "folded into the generalised dtype dispatch" mechanism for the BF16 retirement.
- The `INT_WIDE` use-cases (widening multiply / explicit carry-borrow / 64-bit-boundary shift).
- **The MAVERICK device bodies of all seven** — not carved (the `0xf3` body-depth wall; SUNDA RELEASE
  string-stripping; the `0xf4` no-dedicated-name). All MAVERICK interiors are header-OBSERVED only.

**LOW / NOT CLAIMED:**

- For `0xf4`: whether the no-self-name in the present MAVERICK DVE `DEBUG` means "folded inline (no
  dedicated name)" vs "body not yet in this ucode revision" — not disambiguable from this blob.
- Whether `0xf3` has a POOL self-name on real MAVERICK silicon (no MAVERICK POOL `DEBUG` image to check).
- The exact MAVERICK micro-schedule / SUNDA BF16 packing micro-op sequence (not disassembled).
