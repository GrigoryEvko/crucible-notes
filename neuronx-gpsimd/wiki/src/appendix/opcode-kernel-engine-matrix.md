# The Opcode ↔ Kernel ↔ Engine Matrix

This is the **master lookup appendix** for the GPSIMD (Cadence Tensilica Vision-Q7
*Cairo* `ncore2gp`) instruction set: the single consolidated table a reimplementer scans
to resolve *any* opcode to its **kernel name, executing engine, operand struct, dtype
set, per-generation presence, and the firmware/kernels page that decodes its body.** It
is the join of three independently-grounded sources, all read from shipped binaries:

* the [Opcode-Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the
  authoritative **140-real-opcode** roster (`172 − 31 PSEUDO − 1 INVALID`), every opcode
  named from the four shipped `aws_neuron_isa_tpb_common.h` `NEURON_ISA_TPB_OPCODE` enums
  and struct-decoded through the firmware;
* the [Cross-Gen `kernel_info_table` / Opcode Matrix](../images/cross-gen-kernel-info-matrix.md)
  — the image-grounded per-generation presence/engine carve from the embedded Q7 POOL
  firmware in `libnrtucode_internal.so`;
* the [POOL Extended-Opcode (`0xF0`) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the
  byte-13 spec sub-table for the single `0xF0` escape opcode.

This appendix is a **terse reference**, not a narrative: the per-opcode *body* lives on
its dedicated [firmware/kernels](../firmware/kernels/opcode-catalog-ledger.md) page (the
**Page** column links it); the *struct field layout* lives on the
[`kernel_info_table` layout](../firmware/pool/kernel-info-table.md) and
[device-firmware globals](struct-device-firmware-globals.md) pages; the *dtype codes*
live in the [Unified Datatype Model](../firmware/kernels/dtype-model.md). This page never
re-derives them — it indexes them.

> **Scope contract.** Where this page and its three sources could diverge, the **ledger
> ([#688](../firmware/kernels/opcode-catalog-ledger.md)) is authoritative for opcode →
> name → engine → presence**, the **`0xF0` page
> ([#678](../firmware/pool/pool-ext-0xf0.md)) is authoritative for the spec sub-table**,
> and the **dtype model ([#690](../firmware/kernels/dtype-model.md)) is authoritative for
> dtype codes**. Every divergence found this pass is reconciled in-place with a
> **CORRECTION** callout and noted for the Part-16 reconcile. This page MUST NOT
> contradict #688/#775; it does not.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string read from a shipped image this session; `INFERRED` = reasoned
over OBSERVED facts (usually across a FLIX/literal-pool desync); `CARRIED` = consolidated
from a cited cross-page anchor at its original confidence. The recovered `common.h` enum
lines, firmware log strings, `.xt.prop` mangled names, and ELF section bytes **are**
binary-derived facts and are cited as such. Generation byte-confidence: **SUNDA (v2) /
CAYMAN (v3) / MARIANA (v4) / MARIANA_PLUS (v4+)** are byte-grounded `[HIGH/OBSERVED]`;
**MAVERICK (v5)** is header-OBSERVED for its enum names/bytes but device-body MED for
several late ops (FLIX-desynced interiors). **TONGA (V1)** presence is `[INFERRED]` — it
is decoded on the [cross-gen opcode diff](../generations/cross-gen-opcode-diff.md) page,
not here; this page carries the v2–v5 columns and flags the TONGA-retired ops.

---

## 0. How to read this table

The presence column reads **`[SU·CA·MA·MV]`** = `[SUNDA v2 · CAYMAN v3 · MARIANA v4 ·
MAVERICK v5]`. **MARIANA_PLUS (v4+) is folded into the MARIANA column** — its
`NEURON_ISA_TPB_OPCODE` enum is byte-identical to MARIANA (`kernel_info_table` key sha
`9f2ce049`), so it adds **zero opcode-space delta** (the v4+ change is the DGE fast-path,
not the roster). Cell values:

| Glyph | Meaning |
| --- | --- |
| `Y` | present **and** maintained in that gen's enum (`// Y` flag) |
| `n` | present but **dormant** (`// n` — "not maintained/used") |
| `.` | present, **unflagged** (no `// Y` / `// n`) |
| `-` | **absent** from that gen's enum |

Engine column: **PE** (systolic array) · **ACT** (activation) · **DVE** (vector engine) ·
**POOL** (Q7 compute core) · **NX** (SEQ control core) · **NX/DMA** (descriptor-backed
DMA). `POOL/DVE` = decoded on the Q7 POOL surface but executes on DVE lanes;
`DVE/POOL` = inverse. Struct column families are defined on the per-kernel pages and the
[struct census](struct-census-overview.md); `—` = body not separately decoded.

> **GOTCHA — three counts that differ by construction.** The roster is **140** (the
> cross-gen enum union); the Q7 POOL `kernel_info_table` is **17** (one image's compute
> back-end, five of them the same `0xF0`); the SEQ ASCII dispatch hub is **178 slots → 55
> real handlers**. These are three *different surfaces* and were never meant to match —
> see the [ledger §3](../firmware/kernels/opcode-catalog-ledger.md#3-the-three-source-crossing-why-140--17--55).
> This page enumerates the **140**.

---

## 1. The `140` reconciliation (byte-grounded)

Re-derived byte-exact this pass from the four shipped headers under
`extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../custom_op/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
(`rg -c 'NEURON_ISA_TPB_OPCODE_\w+\s*=\s*0x'` per header; Python union over the four
parsed dicts):

```
   145 / 150 / 159 / 165   opcode enum lines (SUNDA / CAYMAN / MARIANA / MAVERICK)
   172   distinct opcode VALUES in the union of the four enums   [HIGH/OBSERVED]
 −  31   PSEUDO 0xC1–0xDF (NRT-lowered, never device-decoded)    [HIGH/OBSERVED]
 −   1   INVALID 0xFF sentinel                                   [HIGH/OBSERVED]
 ───────
   140   REAL HW opcodes — the rows of §2/§3 below
```

`[HIGH/OBSERVED — every count re-grounded against the four headers this pass: 145/150/159/165
line counts, 172-union, 31-PSEUDO band, 1-INVALID; matches the ledger §1.4 byte-for-byte.]`

> **NOTE — this matrix's row total IS 140.** §2 (PE 10 · ACT 6 · DVE-vector 8 · POOL band
> 56 · batchnorm 10 · `0x81` 1) + §3 (NX/SEQ+DMA 49) sum to **140**, the same partition
> the ledger closes against. Every real HW opcode appears **exactly once** below.

---

## 2. The compute engines (PE · ACT · DVE · POOL · batchnorm)

### 2.1 PE — systolic array (`0x01–0x0A`)

`[HIGH/OBSERVED — names + bytes re-read from the maverick header this pass.]`

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x01` | LDWEIGHTS | PE | S3_LW | weight load | `YYYY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x02` | MATMUL | PE | S3_MM | MAC→PSUM (FP32 accum) | `YYYY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x03` | PE_REG_WRITE | PE | — | reg write | `YYYY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x04` | WEIGHT_MASK | PE | — | — | `nnnn` | NONE (dormant; tonga-retired) |
| `0x05` | WEIGHT_SHIFT | PE | — | — | `nnnn` | NONE (dormant; tonga-retired) |
| `0x06` | LDTAGS | PE | S3_LT | tag load | `-YYY` | [sparsity-compress-tag](../firmware/kernels/sparsity-compress-tag.md) |
| `0x07` | MATMUL_SPARSE | PE | S4D3_MM | 2/3/4 fmap | `-YYY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x08` | PE_MANAGE_SEED | PE | — | PE seed state | `--YY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x09` | LDWEIGHTS_MX | PE | S3_LW (MX) | MX weights | `--YY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |
| `0x0A` | MATMUL_MX | PE | S3_MM (MX) | MX matmul | `--YY` | [pe-matmul](../firmware/kernels/pe-matmul.md) |

> **CORRECTION — `0x02 = MATMUL`, `0x05 = WEIGHT_SHIFT` (not "Matmul").** The shipped
> maverick header pins `0x02 = MATMUL // Y` and `0x05 = WEIGHT_SHIFT // n, tonga stuff,
> deprecated` (and `0x04 = WEIGHT_MASK // n, tonga stuff, deprecated`). The cross-gen
> matrix ([#775 §4h](../images/cross-gen-kernel-info-matrix.md#4h-the-pe-matmul-opcodes-surface-a-systolic-array))
> once labelled `0x05 = Matmul (MultiplyMoving)` and omitted a `0x02` row — that is a
> wrong label conflating the NKI "MultiplyMoving" *name* with the wrong opcode byte. The
> ledger ([#688 §2.1](../firmware/kernels/opcode-catalog-ledger.md#21-pe--systolic-array-0x010x0a))
> already carries this CORRECTION; recorded here authoritatively. **Noted for Part-16
> reconcile: fix #775 §4h's `0x05`/`0x02` rows.** `[HIGH/OBSERVED — header bytes read this pass.]`

> **NOTE — `(†)` MX-fold on MAVERICK.** `LDWEIGHTS_MX`/`MATMUL_MX` (`0x09`/`0x0A`) persist
> as opcodes on v5 but their named handlers **fold into Matmul** via the `MXTensorV2`
> path (still PE); see [#775 §4h note](../images/cross-gen-kernel-info-matrix.md#4h-the-pe-matmul-opcodes-surface-a-systolic-array).
> `0x08`/`0x09`/`0x0A` first ship at MARIANA (CAYMAN = absent). `[HIGH/OBSERVED]`

### 2.2 ACT — activation (`0x21–0x26`)

`[HIGH/OBSERVED — all six bytes re-read this pass; `0x26` body MED on v5.]`

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x21` | ACTIVATE | ACT | S3D3_AC | PWL / affine | `YYYY` | [activate-pwl](../firmware/kernels/activate-pwl.md) |
| `0x22` | ACTIVATE_QUANTIZE | ACT | S3D3_AQ | requantize | `YYYY` | [activate-pwl](../firmware/kernels/activate-pwl.md) |
| `0x23` | ACTIVATION_TABLE_LOAD | ACT | CTRL_NO | LUT DMA stage | `YYYY` | [activate-pwl](../firmware/kernels/activate-pwl.md) |
| `0x24` | ACTIVATION_READ_ACCUMULATOR | ACT | D1_RD | accumulator | `YYYY` | [activate-pwl](../firmware/kernels/activate-pwl.md) |
| `0x25` | ACTIVATE2 | ACT | — | 2nd activate | `--YY` | NONE — maintained gap **`†MED`** |
| `0x26` | ACTIVATE_MULTIPASS | ACT | — | multipass | `---Y` | NONE — MAVERICK + **`†MED`** |

### 2.3 DVE — vector engine: Exponential / RNG / sparsity / MX (`0x30`, `0x76–0x78`, `0xE0–0xE3`)

`[HIGH/OBSERVED — bytes read this pass; `0xE0–0xE3` are MARIANA additions.]`

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x30` | EXPONENTIAL | DVE | S3D3_TS | fp | `....` | [exponential](../firmware/kernels/exponential.md) |
| `0x76` | RAND | DVE | — | XORWOW | `nnnn` | [rng-lfsr-dispatch](../firmware/kernels/rng-lfsr-dispatch.md) (dormant; covered) |
| `0x77` | RAND_GET_STATE | DVE | — | seed state | `YYYY` | [rng-seed-state-ops](../firmware/kernels/rng-seed-state-ops.md) |
| `0x78` | RAND_SET_STATE | DVE | — | seed state | `YYYY` | [rng-seed-state-ops](../firmware/kernels/rng-seed-state-ops.md) |
| `0xE0` | SPARSITY_COMPRESS | DVE | S3D3_SC | fp8 / bf16 / fp16 | `--YY` | [sparsity-compress-tag](../firmware/kernels/sparsity-compress-tag.md) |
| `0xE1` | SPARSITY_COMPRESS_TAG | DVE | S2D2D2_SC | u16 tag | `--YY` | [sparsity-compress-tag](../firmware/kernels/sparsity-compress-tag.md) |
| `0xE2` | RAND2 | DVE | — | XORWOW uniform | `--YY` | [rand2](../firmware/kernels/rand2.md) |
| `0xE3` | QUANTIZE_MX | DVE | MXTensorV2 | MX block quant (E8M0 scale) | `--YY` | [mx-dequant](../firmware/kernels/mx-dequant.md) |

> **QUIRK — `0xE3 QUANTIZE_MX` is a DVE opcode, NOT a POOL `kernel_info_table` row.**
> Re-carving the MAVERICK `EXTISA_0` POOL KIT (17 entries) confirmed `0xE3` is **absent**;
> it is armed only in the MAVERICK DVE PROF CAM, and its named handler is even dropped from
> the v5 DVE roster (60→59). **POOL's only MX surface is `0x7B TENSOR_DEQUANTIZE`** (KIT
> idx16, §2.4). `[HIGH/OBSERVED — #775 §4g re-carve.]`

> **NOTE — `0x30 EXPONENTIAL` presence `....`** The header carries it present-but-unflagged
> in all four gens; its DVE compute *body* is a MARIANA realization (per #775 §4g). The
> `....` reflects the header roster; the DVE attribution reflects the runtime. `[HIGH/OBSERVED]`

### 2.4 POOL — Q7 compute core (the bulk of the kernel lane)

The largest band. Rows in numeric order; the `0x60–0x66` batchnorm sub-cluster is broken
out in §2.5 for readability, and the transpose/`2` batchnorm variants `0x82`/`0x8E`/`0x94`
are listed here in their numeric positions. **`†MED`** = body in the per-gen
device-depth tail (FLIX-desynced interior, out-of-corpus, or header-OBSERVED-only on
MAVERICK): named + dispatch-placed HIGH, operand-layout body MED.

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x41` | TENSOR_TENSOR_ARITH_OP | POOL | S3S3D3_TT | DTYPE_PAIR arith | `YYYY` | [tensor-tensor](../firmware/kernels/tensor-tensor.md) |
| `0x42` | TENSOR_REDUCE_ARITH_OP | POOL | S3D3 reduce | arith | `YYYY` | [tensor-reduce](../firmware/kernels/tensor-reduce.md) |
| `0x43` | TENSOR_SCALAR_ARITH_OP | POOL | S3D3_TS | arith | `YYYY` | [tensor-scalar](../firmware/kernels/tensor-scalar.md) |
| `0x44` | TENSOR_SCALAR_PTR_ARITH_OP | POOL | S3D3_TS | arith (ptr) | `nnnn` | [tensor-scalar](../firmware/kernels/tensor-scalar.md) (dormant; covered) |
| `0x45` | POOL | POOL | S3D3 pool | avg / max | `YYYY` | [avg-max-pool](../firmware/kernels/avg-max-pool.md) |
| `0x46` | COPY | POOL | S3D3 copy | bit-accurate | `YYYY` | [cast-copy](../firmware/kernels/cast-copy.md) |
| `0x47` | CAST | POOL | S3D3 cast | in→FP32→out | `YYYY` | [cast-copy](../firmware/kernels/cast-copy.md) |
| `0x48` | RECIPROCAL | POOL | — | fp | `YYYY` | NONE — maintained gap |
| `0x49` | MEMSET | POOL | S3D3 | — | `YYYY` | NONE — maintained gap |
| `0x4A` | REG_LOAD | POOL | — | — | `nnnn` | NONE (dormant) |
| `0x4B` | REG_STORE | POOL | — | — | `nnnn` | NONE (dormant) |
| `0x4C` | REG_SHUFFLE | POOL | — | — | `nnnn` | NONE (dormant) |
| `0x4D` | RNG | POOL | — | XORWOW | `YYYY` | [rng-lfsr-dispatch](../firmware/kernels/rng-lfsr-dispatch.md) |
| `0x4E` | TENSOR_CUMULATIVE_ARITH_OP | POOL | S4D4_TR | arith | `YYYY` | [tensorcumulative](../firmware/kernels/tensorcumulative.md) |
| `0x4F` | TENSOR_SCALAR_PTR_MULTI_ARITH | POOL | S3D3_TS | arith | `nnnn` | [ts-ptrmulti](../firmware/kernels/ts-ptrmulti.md) (dormant; covered) |
| `0x51` | TENSOR_TENSOR_BITVEC_OP | POOL | S3S3D3_TT | bitvec | `YYYY` | [tensor-tensor](../firmware/kernels/tensor-tensor.md) |
| `0x52` | TENSOR_REDUCE_BITVEC_OP | POOL | S3D3 reduce | bitvec | `YYYY` | [tensor-reduce](../firmware/kernels/tensor-reduce.md) |
| `0x53` | TENSOR_SCALAR_BITVEC_OP | POOL | S3D3_TS | bitvec | `YYYY` | [tensor-scalar](../firmware/kernels/tensor-scalar.md) |
| `0x54` | TENSOR_SCALAR_PTR_BITVEC_OP | POOL | S3D3_TS | bitvec (ptr) | `nnnn` | [tensor-scalar](../firmware/kernels/tensor-scalar.md) (dormant; covered) |
| `0x58` | MAX_POOL_SELECT | POOL | — | pool-select | `YYYY` | [avg-max-pool](../firmware/kernels/avg-max-pool.md) |
| `0x5E` | TENSOR_CUMULATIVE_BITVEC_OP | POOL | S4D4_TR | bitvec | `YYYY` | [tensorcumulative](../firmware/kernels/tensorcumulative.md) |
| `0x5F` | TENSOR_SCALAR_PTR_MULTI_BITVEC | POOL | S3D3_TS | bitvec | `nnnn` | [ts-ptrmulti](../firmware/kernels/ts-ptrmulti.md) (dormant; covered) |
| `0x67` | POOL_BUFFER_LOAD | POOL | PSEUDO | — | `YYYY` | NONE — maintained gap |
| `0x68` | GATHER | POOL | S4D4_GT | idx u8/16/32 | `YYYY` | [indirection-gather](../firmware/kernels/indirection-gather.md) |
| `0x69` | LOAD_MASK_SELECT | POOL | — | — | `YYYY` | NONE — maintained gap |
| `0x6A` | STREAM_SHUFFLE | POOL | — | — | `YYYY` | NONE — maintained gap |
| `0x6B` | STREAM_TRANSPOSE | POOL | S4D4_TR | 32×32 transpose | `YYYY` | [stream-transpose](../firmware/kernels/stream-transpose.md) |
| `0x6C` | MAX8 | DVE | S4D2_BN | search | `YYYY` | [search-cluster](../firmware/kernels/search-cluster.md) |
| `0x6D` | MATCH_VALUE_LOAD | DVE | S4D2_BN | search | `YYYY` | [search-cluster](../firmware/kernels/search-cluster.md) |
| `0x6E` | FIND_INDEX8 | DVE | S4D2_BN | search | `YYYY` | [search-cluster](../firmware/kernels/search-cluster.md) |
| `0x6F` | MATCH_REPLACE8 | DVE | S4D2_BN | search | `YYYY` | [search-cluster](../firmware/kernels/search-cluster.md) |
| `0x70` | TENSOR_SCALAR_IMM_LD_ARITH | POOL | S3D3_TS | arith (imm) | `nnnn` | [ts-immld](../firmware/kernels/ts-immld.md) (dormant; covered) |
| `0x71` | TENSOR_SCALAR_IMM_LD_BITVEC | POOL | S3D3_TS | bitvec (imm) | `nnnn` | [ts-immld](../firmware/kernels/ts-immld.md) (dormant; covered) |
| `0x72` | COPY_PREDICATED | POOL | — | predicated copy | `YYYY` | NONE — maintained gap |
| `0x73` | ROI_ALIGN | POOL | — | — | `nnnn` | NONE (dormant) |
| `0x74` | TENSOR_SCALAR_ADDR | POOL | S3D3_TS addr | arith (addr) | `YYYY` | NONE — maintained gap |
| `0x79` | EMBEDDING_UPDATE | POOL | PSEUDO | scatter-reduce | `YYYY` | [indirection-gather](../firmware/kernels/indirection-gather.md) |
| `0x7A` | LOAD_POOL_ARGUMENT | POOL | PSEUDO | — | `YYYY` | NONE — maintained gap |
| `0x7B` | TENSOR_DEQUANTIZE | POOL | S3D3_TENS_DEQUANT | MX 4-bit (`dequant_fmt`) | `YYYY` | [tensor-dequantize](../firmware/kernels/tensor-dequantize.md) |
| `0x7C` | CROSS_LANE_REDUCE_ARITH | POOL | S3D3 reduce | arith | `YYYY` | [cross-lane-reduce](../firmware/kernels/cross-lane-reduce.md) |
| `0x7D` | CROSS_LANE_REDUCE_BITVEC | POOL | S3D3 reduce | bitvec | `YYYY` | [cross-lane-reduce](../firmware/kernels/cross-lane-reduce.md) |
| `0x7E` | IOTA | POOL | PSEUDO/iota | INT index ramp | `YYYY` | [iota](../firmware/kernels/iota.md) |
| `0x7F` | DROPOUT | POOL/DVE | — | mask | `YYYY` | [dropout](../firmware/kernels/dropout.md) |
| `0x81` | JPEG_DECODE | POOL | — | — | `-nnn` | NONE (dormant) |
| `0x82` | TRANSPOSE_BATCH_NORM_STATS2 | DVE | — | bn stats (T) | `YYYY` | [batchnorm-forward](../firmware/kernels/batchnorm-forward.md) |
| `0x83` | TRANSPOSE_TENSOR_REDUCE_ARITH_OP | POOL | S3D3 reduce | arith | `YYYY` | [tensor-reduce](../firmware/kernels/tensor-reduce.md) |
| `0x84` | TRANSPOSE_TENSOR_REDUCE_BITVEC_OP | POOL | S3D3 reduce | bitvec | `YYYY` | [tensor-reduce](../firmware/kernels/tensor-reduce.md) |
| `0x85` | CUSTOM_OP_HEADER | POOL | — | marshalling | `YYYY` | NONE — maintained gap |
| `0x86` | CUSTOM_OP_PAYLOAD | POOL | — | marshalling | `YYYY` | NONE — maintained gap |
| `0x87` | TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH | POOL | S3D3_TS | arith | `n---` | [sunda-dual-tensorscalarptr](../firmware/kernels/sunda-dual-tensorscalarptr.md) (SUNDA-only, dormant) **`†MED`** |
| `0x88` | TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC | POOL | S3D3_TS | bitvec | `n---` | [sunda-dual-tensorscalarptr](../firmware/kernels/sunda-dual-tensorscalarptr.md) (SUNDA-only, dormant) **`†MED`** |
| `0x8A` | TENSOR_TENSOR_ADD_BF16 | POOL | — | bf16 | `Y---` | NONE — SUNDA-only **`†MED`** |
| `0x8B` | TENSOR_TENSOR_MULT_BF16 | POOL | — | bf16 | `Y---` | NONE — SUNDA-only **`†MED`** |
| `0x8C` | TENSOR_REDUCE_ADD_BF16 | POOL | — | bf16 | `Y---` | NONE — SUNDA-only **`†MED`** |
| `0x8D` | TENSOR_REDUCE_MAX_BF16 | POOL | — | bf16 | `Y---` | NONE — SUNDA-only **`†MED`** |
| `0x8E` | BATCH_NORM_PARAM_LOAD2 | DVE | — | bn param | `YYYY` | [batchnorm-paramload](../firmware/kernels/batchnorm-paramload.md) |
| `0x8F` | TENSOR_TENSOR_SUB_BF16 | POOL | — | bf16 | `Y---` | NONE — SUNDA-only **`†MED`** |
| `0x92` | TENSOR_SCALAR_AFFINE_SELECT | POOL | S3D3_TS | select | `YYYY` | [affineselect](../firmware/kernels/affineselect.md) |
| `0x93` | TRANSPOSE_TENSOR_SCALAR_ARITH_OP | POOL | S3D3_TS | arith | `YYYY` | NONE — maintained gap |
| `0x94` | BATCH_NORM_GRAD_ACCUMULATE2 | DVE | — | bn grad | `YYYY` | [batchnorm-gradaccum](../firmware/kernels/batchnorm-gradaccum.md) |
| `0x95` | MODIFY_POOL_CONFIG | POOL | — | config | `YYYY` | NONE — maintained gap (SEQ-named; body PENDING) |
| `0x96` | SORT | POOL | — | sort | `YYYY` | [sort](../firmware/kernels/sort.md) |
| `0x98` | TENSOR_SCALAR_SELECT | POOL | S3D3_TS | select | `YYYY` | [ts-select](../firmware/kernels/ts-select.md) |
| `0x99` | CAST_PREDICATED | POOL | — | predicated cast | `YYYY` | [castpredicated](../firmware/kernels/castpredicated.md) |
| `0x9A` | TENSOR_SCALAR_CACHE_REDUCE | POOL | — | cache-reduce | `YYYY` | [ts-cache-reduce](../firmware/kernels/ts-cache-reduce.md) |
| `0x9B` | DVE_READ_ACCUMULATOR | POOL/DVE | — | accumulator | `YYYY` | [dve-read-state](../firmware/kernels/dve-read-state.md) |
| `0x9C` | TENSOR_REDUCE_RANGE_CHECK | POOL | — | — | `nnnn` | NONE (dormant) |
| `0x9D` | SCALAR_TENSOR_TENSOR_ARITH | POOL | — | arith | `YYYY` | [scalar-tensor-tensor](../firmware/kernels/scalar-tensor-tensor.md) |
| `0x9E` | SCALAR_TENSOR_TENSOR_BITVEC | POOL | — | bitvec | `YYYY` | [scalar-tensor-tensor](../firmware/kernels/scalar-tensor-tensor.md) |
| `0xE4` | CONV_LUT_LOAD | POOL | S2_CONVLUT | cptc codec (`cptc_decode<1-6>`) | `-YYY` | [convlutload](../firmware/kernels/convlutload.md) · [cptc-codec](../firmware/kernels/cptc-codec.md) |
| `0xE5` | TENSOR_TENSOR_SCAN_ARITH | POOL | — | scan | `YYYY` | [tensor-tensor-scan](../firmware/kernels/tensor-tensor-scan.md) |
| `0xE6` | TENSOR_SCALAR_CACHE_CUMULATIVE | POOL | — | cumulative | `YYYY` | [ts-cache-cumulative](../firmware/kernels/ts-cache-cumulative.md) |
| `0xE7` | INDIRECT_COPY | POOL | S4D4_IC | indexed copy | `YYYY` | [indirection-gather](../firmware/kernels/indirection-gather.md) |
| `0xE8` | COPY_PREDICATED_SCALAR | POOL | S3D3_CP_PRED_SCALAR | predicated copy | `YYYY` | [copypredicatedscalar](../firmware/kernels/copypredicatedscalar.md) |
| `0xE9` | DVE_READ_INDICES | POOL/DVE | — | indices | `-YYY` | [dve-read-state](../firmware/kernels/dve-read-state.md) |
| `0xEA` | SELECT_REDUCE | POOL | — | select-reduce | `YYYY` | [copypredicatedreduce](../firmware/kernels/copypredicatedreduce.md) |
| `0xF0` | EXTENDED_INST | POOL | (escape) | per-spec (see §4) | `YYYY` | [pool-ext-0xf0](../firmware/pool/pool-ext-0xf0.md) |
| `0xF1` | DMA_GATHER_TRANSPOSE | POOL | — | gather-transpose | `-YYY` | NONE — SEQ FLIX-inline; body PENDING **`†MED`** |
| `0xF2` | NONZERO_WITH_COUNT | POOL | S3D3_NONZERO_WC | float / int | `-YYY` | [nonzero-with-count](../firmware/kernels/nonzero-with-count.md) |
| `0xF3` | TENSOR_TENSOR_INT_WIDE | DVE/POOL | — | wide int | `---Y` | [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) **`†MED`** |
| `0xF4` | TENSOR_SCALAR_INT_WIDE | DVE/POOL | — | wide int | `---Y` | [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) **`†MED`** |

> **CORRECTION — the MAVERICK `0x26`/`0xF3`/`0xF4` bytes ARE header-pinned (byte = HIGH;
> only the device *body* is MED).** The cross-gen matrix
> ([#775 §9](../images/cross-gen-kernel-info-matrix.md#9-reconciliation-of-the-maverick-6-new-opcodes-premise))
> wrote that `0x26`/`0xF3`/`0xF4` "are NOT the pinned bytes" and "not asserted", and the
> ledger flagged `INT_WIDE` as "not byte-pinned". Re-reading the shipped maverick header
> this pass refutes the *byte* uncertainty: it pins `ACTIVATE_MULTIPASS = 0x26 // Y`,
> `TENSOR_TENSOR_INT_WIDE = 0xf3 // Y`, `TENSOR_SCALAR_INT_WIDE = 0xf4 // Y` directly — the
> opcode **bytes are `[HIGH/OBSERVED]`**. What remains MED is only the **device dispatch
> trampoline / operand body** (FLIX-desynced, no clean v5 POOL DEBUG image) — hence the
> `†MED` flag stays on the body, but the byte assignment is *not* inferred. **Noted for
> Part-16 reconcile: #775 §9 + ledger §5 should narrow the MED scope to the device body,
> not the byte.** `[HIGH/OBSERVED — maverick header lines read this pass: `0x26`, `0xf3`,
> `0xf4` all assigned + `// Y`.]`

> **NOTE — `0x81 JPEG_DECODE` is real-but-dormant** (`-nnn`: absent on SUNDA, dormant
> CAYMAN+). It is a *different* opcode from the pseudo `0xD7 PSEUDO_JPEG_DECODE`
> ([ledger §1.2](../firmware/kernels/opcode-catalog-ledger.md#12-the-31-pseudo-opcodes-subtracted-0xc10xdf)).
> Listed once, in its numeric position above. `[HIGH/OBSERVED]`

### 2.5 DVE — batchnorm family (`0x60–0x66`)

`[HIGH/OBSERVED — names + flags read this pass.]`

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x60` | BATCH_NORM_STATS | DVE | — | bn stats | `nnnn` | [batchnorm-forward](../firmware/kernels/batchnorm-forward.md) (dormant; covered) |
| `0x61` | BATCH_NORM_STATS2 | DVE | — | bn stats | `YYYY` | [batchnorm-forward](../firmware/kernels/batchnorm-forward.md) |
| `0x62` | BATCH_NORM_AGGREGATE | DVE | — | bn aggregate | `YYYY` | [batchnorm-forward](../firmware/kernels/batchnorm-forward.md) |
| `0x63` | BATCH_NORM_GRAD_ACCUMULATE | DVE | — | bn grad | `nnnn` | [batchnorm-gradaccum](../firmware/kernels/batchnorm-gradaccum.md) (dormant; covered) |
| `0x64` | BATCH_NORM_PARAM_LOAD | DVE | — | bn param | `nnnn` | [batchnorm-paramload](../firmware/kernels/batchnorm-paramload.md) (dormant; covered) |
| `0x65` | BATCH_NORM_BACK_PROP | DVE | — | bn backprop | `YYYY` | [batchnorm-backprop](../firmware/kernels/batchnorm-backprop.md) |
| `0x66` | LOAD_PARAMETER_RAM | DVE | — | 256-recip RAM | `YYYY` | [batchnorm-forward](../firmware/kernels/batchnorm-forward.md) |

---

## 3. The control engine (NX SEQ + DMA, `0x9F–0xBF`)

The NX SEQ control core's spine plus the descriptor-backed DMA opcodes. Most of the
`CTRL` rows are **SEQ control-spine handlers** named in the
[dispatch hub](../firmware/seq/dispatch-hub.md) with no dedicated decode body.

| Op | Kernel name | Eng | Struct | Dtype(s) | `[SU·CA·MA·MV]` | Page |
| --- | --- | --- | --- | --- | --- | --- |
| `0x9F` | ENGINE_NOP | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA0` | EVENT_SEMAPHORE | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA1` | HALT | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA2` | DRAIN | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA3` | INSTRUCTION_FLUSH | NX | CTRL | — | `YYYY` | SEQ spine (`INS_FL`, [dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA4` | NOP | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA5` | WRITE | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA6` | NOTIFY | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xA7` | MOVE | NX | move (full reg) | u32 / i32 / fp32 | `YYYY` | [move-dtype](../firmware/kernels/move-dtype.md) |
| `0xA8` | ALU_OP | NX | — | scalar ALU | `YYYY` | [alu-op-matrix](../firmware/kernels/alu-op-matrix.md) |
| `0xA9` | COMPARE_BRANCH | NX | — | branch | `YYYY` | [branch-prefetch](../firmware/seq/branch-prefetch.md) |
| `0xAA` | TENSOR_LOAD | NX | — | load | `YYYY` | [tensorload](../firmware/kernels/tensorload.md) |
| `0xAB` | TENSOR_STORE | NX | — | store | `YYYY` | [tensorstore](../firmware/kernels/tensorstore.md) |
| `0xB0` | EVENT_SEMAPHORE_RANGE_CLEAR | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xB1` | SET_ORDERING_MODE | NX | CTRL | — | `YYYY` | SEQ spine (`SET_OM`, [dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xB2` | MOVE_SHAPE | NX | CTRL | — | `YYYY` | SEQ spine (`MoveShape`, [dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xB3` | POLL_SEM | NX | CTRL | — | `YYYY` | SEQ spine ([dispatch-hub](../firmware/seq/dispatch-hub.md)) |
| `0xB4` | TEST_EVENT_SEM | NX | CTRL | — | `--YY` | NONE — maintained gap (MARIANA +; no SEQ slot) |
| `0xB5` | BRANCH_PREFETCH_HINT | NX | CTRL | — | `YYYY` | SEQ spine ([branch-prefetch](../firmware/seq/branch-prefetch.md)) |
| `0xB6` | COMPACT_CONTROL_INST | NX | — | control | `---Y` | NONE — MAVERICK + **`†MED`** |
| `0xB8` | DMAMEMCPY | NX/DMA | (DMA) | — | `YYYY` | NONE — DGE-backed; per-opcode pending |
| `0xB9` | DMA_MEMCPY2 | NX/DMA | (DMA) | — | `---Y` | NONE — MAVERICK +; DGE-backed **`†MED`** |
| `0xBA` | DMA_IMMEDIATE | NX/DMA | (DMA) | imm | `---Y` | NONE — MAVERICK +; DGE-backed **`†MED`** |
| `0xBB` | DMA_INDIRECT | NX/DMA | DMA_INDIRECT1D | by-index | `YYYY` | [indirection-gather](../firmware/kernels/indirection-gather.md) |
| `0xBC` | RANGE_SELECT | NX/DMA | — | range | `-YYY` | [rangeselect](../firmware/kernels/rangeselect.md) |
| `0xBD` | DMA_TRANSPOSE | NX/DMA | — | transpose | `-YYY` | [dma-transpose-opcode-cluster](../firmware/kernels/dma-transpose-opcode-cluster.md) **`†MED`** |
| `0xBE` | GET_SEQUENCE_BOUNDS | POOL | S3D3_SEQ_BOUNDS | dtype-keyed | `-YYY` | [get-sequence-bounds](../firmware/kernels/get-sequence-bounds.md) |
| `0xBF` | SB2SB_COLLECTIVE | POOL | — | collective | `-YYY` | [sb2sb-remote-copy](../firmware/kernels/sb2sb-remote-copy.md) |

> **CORRECTION — `0xBE`/`0xF2` are distinct opcodes (`GET_SEQUENCE_BOUNDS` /
> `NONZERO_WITH_COUNT`), not one.** The maverick enum pins `GET_SEQUENCE_BOUNDS = 0xbe`
> and `NONZERO_WITH_COUNT = 0xf2`; the `0xf2` trampoline routes through a *shared*
> sequence-bounds/dequant region, hence a historical "`0xf2 = GetSequenceBounds`" loose
> conflation. The authoritative split is `0xBE = GetSequenceBounds` (POOL, KIT idx14),
> `0xF2 = NonzeroWithCount` (POOL, KIT idx15) — both already correct in the tables above.
> `[HIGH/OBSERVED — #775 §2 CORRECTION, enum-pinned.]`

> **NOTE — `0xBE`/`0xBF` are POOL-engine, not NX.** Numerically they sit in the NX
> `0x9F–0xBF` band, but `GET_SEQUENCE_BOUNDS` and `SB2SB_COLLECTIVE` execute on the **Q7
> POOL** core (`0xBE` is `kernel_info_table` idx14). They are listed in their numeric
> position here with the correct POOL engine tag. `[HIGH/OBSERVED]`

---

## 4. The `0xF0` EXTENDED_INST spec sub-table

`0xF0 EXTENDED_INST` is the **single escape opcode**, POOL-exclusive (absent on the
ACT/DVE/PE/SP SEQ tables). There is **no in-loop `if (opcode==0xF0)` branch**: the
"two-level" dispatch is realized **entirely by the `kernel_info_table`** — opcode `0xF0`
is registered **five times** with five distinct **spec bytes** (the spec is the third
byte of the packed key `opcode<<24 | spec<<16`, i.e. key offset `+0x02`), plus once more
(spec 7) in the cptc sub-image `EXTISA_3`. One linear key-scan lands a `(0xF0, spec)`
instruction on exactly one row. `[HIGH/OBSERVED — #678 §1, re-carved byte-exact.]`

| Spec (byte+13) | KIT idx | funcVA (CAYMAN) | Sub-op / handler | Resolved kernel | Image | Conf |
| --- | --- | --- | --- | --- | --- | --- |
| `0` | 6 | `0x01003370` | ExtendedInstEngineNop | no-op (`entry; movi a2,0; retw.n`) | `EXTISA_0` | HIGH |
| `1` | 7 | `0x01003380` | ExtendedInstCopy | `pool_extended_inst_copy()` | `EXTISA_0` | HIGH `[.xt.prop EXACT]` |
| `2` | 8 | `0x01003484` | ExtendedInstTensorTensorArith | `decode_extended_inst_tensor_tensor_arith(bool,uint)` → `0x010034b0` | `EXTISA_0` | HIGH |
| `4` | 9 | `0x010037a8` | Rand/Cptc band (state `0x0200046c`) | → `decode_pool@0x01000b90` (PROBABLE `RandSetState`) | `EXTISA_0` | MED |
| `3` | 10 | `0x01003a60` | Rand/Cptc band (state `0x02000470`) | → `decode_pool@0x01000b90` (PROBABLE `RandGetState`) | `EXTISA_0` | MED |
| `7` | 8 (E3) | `0x01003b64` | ExtendedInst spec7 → cptc extended path | cptc decode (dtype-selected `cptc_decode_impl<1-6>`) | `EXTISA_3` | MED |

`[HIGH for the spec→funcVA rows (specs 0/1/2 EXACT, 4/3/7 routed); the spec3=RandGetState /
spec4=RandSetState pairing is PROBABLE at MED — the table stores specs in registration
order `0,1,2,4,3` (not sorted), and the PERF trampolines carry no name string. See
#678 §6.]`

> **QUIRK — registration order is `0,1,2,4,3`, not sorted; spec 4 precedes spec 3.** A
> linear key-scan is order-independent so this is harmless to dispatch, but a
> reimplementation that **binary-searches** a `(opcode,spec)`-sorted table would mis-locate
> specs 3/4. Scan linearly, or sort first. `[HIGH/OBSERVED — #678 §1.]`

> **GOTCHA — the spec `1..6` of `cptc_decode_impl<N>` is a DTYPE selector, not the POOL
> spec byte.** Do **not** map POOL spec `0/3/4/7` one-to-one onto `cptc_decode_impl<1..6>`.
> The `<N>` template arg is an `(unsigned char)` `in_dtype`/`out_dtype` selector chosen
> *inside* the cptc handler (reached via `0xE4` or `0xF0`-spec7), proven by the demangled
> signature and the `"unsupported in_dtype/out_dtype for cptc_decode"` error strings.
> `[HIGH/OBSERVED — #678 §5.]`

The two miss paths (DEBUG-string-anchored; PERF strips the strings, behavior identical):
`0xF0` + an unregistered spec → `"P%i: UNKNOWN EXTENDED OPCODE=%d"` (**decimal** spec);
any top-level scan miss → `"P%i: UNKNOWN OPCODE=0x%x"` (**hex** opcode).

---

## 5. The do-not-repeat rows (explicit walls)

These rows are **NOT live HW opcodes a reimplementer should decode as such.** They are
called out so a future author does not re-list them as real kernels.

### 5.1 The SortMerge PHANTOM — do not document as an opcode

> **CORRECTION — SortMerge is a PHANTOM; there is no SortMerge HW opcode in any shipped
> enum.** A task plan once named "SortMerge" as a hardware opcode to decode. It survives
> **only** as a comment on the `0x98` line of the maverick header, read verbatim this pass:
> `NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT = 0x98, // SortMerge wip 0x97 // Y`. The byte
> `0x97` is **never assigned to any opcode** in any gen (it appears only as the unrelated
> `NEURON_ISA_TPB_UPDATE_MODE_SEM_SUB_REG_COMPLETE = 0x97` in a *different* enum), and
> `0x98` is the real `TENSOR_SCALAR_SELECT` ([ts-select](../firmware/kernels/ts-select.md)).
> **SortMerge is named-but-never-shipped — it is NOT one of the 140.** `[HIGH/OBSERVED — maverick header
> `0x98` line + the absence of any `OPCODE_* = 0x97` re-verified this pass.]`

### 5.2 The MAVERICK (v5) / Maverick-only adds — flagged on the byte, MED on the body

The six genuine MAVERICK `159→165` enum additions (all `// Y` in the maverick header):

| Op | Name | Engine | Byte | Body |
| --- | --- | --- | --- | --- |
| `0x26` | ACTIVATE_MULTIPASS | ACT | `[HIGH/OBSERVED]` | `†MED` (no v5 ACT DEBUG image) |
| `0xB6` | COMPACT_CONTROL_INST | NX/ctrl | `[HIGH/OBSERVED]` | `†MED` |
| `0xB9` | DMA_MEMCPY2 | NX/DMA | `[HIGH/OBSERVED]` | `†MED` (DGE-backed) |
| `0xBA` | DMA_IMMEDIATE | NX/DMA | `[HIGH/OBSERVED]` | `†MED` (DGE-backed) |
| `0xF3` | TENSOR_TENSOR_INT_WIDE | DVE/POOL | `[HIGH/OBSERVED]` | `†MED` (FLIX-desync) |
| `0xF4` | TENSOR_SCALAR_INT_WIDE | DVE/POOL | `[HIGH/OBSERVED]` | `†MED` (FLIX-desync) |

> **NOTE — the INT_WIDE / MULTIPASS bytes are NOT inferred (see §2.4 CORRECTION).** Earlier
> syntheses flagged `0x26`/`0xF3`/`0xF4` as ordinal-not-byte-pinned; the maverick header
> assigns them directly. Only the **device dispatch body** is INFERRED/MED. `[HIGH byte /
> MED body.]`

> **GOTCHA — the `search-cluster` ops `0x6C–0x6F` are NOT MAVERICK additions.** `MAX8`,
> `MATCH_VALUE_LOAD`, `FIND_INDEX8`, `MATCH_REPLACE8` (all `YYYY`) are **pre-existing
> MARIANA DVE opcodes**; at MAVERICK they are merely *PROF-armed* onto DVE (a profile-table
> change, not opcode growth). Do not double-count them as v5-new. `[HIGH/OBSERVED — #775 §9.]`

### 5.3 The SUNDA-only retired ops — do not carry forward to CAYMAN+

SUNDA (v2) is the only key-set outlier: it ships ops **no later gen carries.** Re-read
from the SUNDA header this pass:

* **The BF16 cluster `0x8A/0x8B/0x8C/0x8D/0x8F`** (`TENSOR_TENSOR_{ADD,MULT,SUB}_BF16`,
  `TENSOR_REDUCE_{ADD,MAX}_BF16`) — all `// Y` on SUNDA, **absent** from CAYMAN/MARIANA/
  MAVERICK (`Y---`). Five ops, **not six** — `0x8E` is *not* in the cluster (it is
  `BATCH_NORM_PARAM_LOAD2`, DVE, `YYYY`). Their operand layouts are out-of-corpus (SUNDA
  EXTISA in `neuronx-runtime`), so the bodies are `†MED`/CARRIED.
* **The deprecated dual-ptr pair `0x87`/`0x88`** (`TENSOR_SCALAR_PTR_MULTI_DUAL_{ARITH,
  BITVEC}`) — both `// n, ucode/kaenadve exists, not maintained/used` on SUNDA, **absent**
  from CAYMAN+ (`n---`). See [sunda-dual-tensorscalarptr](../firmware/kernels/sunda-dual-tensorscalarptr.md).

> **QUIRK — do not confuse SUNDA-retired BF16 with the dtype `BFLOAT16(0x6)`.** The
> SUNDA BF16 ops `0x8A–0x8F` are *dedicated bf16 arithmetic opcodes* the later gens
> dropped in favor of the generic `TENSOR_TENSOR`/`TENSOR_REDUCE` ops routing bf16 through
> the [FP32 convert hub](../firmware/kernels/dtype-model.md#23-the-fp32-convert-hub). The
> dtype code `BFLOAT16 = 0x6` is present in **all** gens; only the *opcodes* were retired.
> `[HIGH/OBSERVED — SUNDA header + dtype model §1.]`

### 5.4 TONGA (V1) — flagged absent, decoded elsewhere

TONGA (V1) predates the v2–v5 band this matrix carries. Its opcode roster is the
**floor** of the superset chain and is decoded on the
[cross-gen opcode diff](../generations/cross-gen-opcode-diff.md) page, not here. The
`0x04 WEIGHT_MASK` / `0x05 WEIGHT_SHIFT` ops carry the header comment *"tonga stuff,
deprecated"* — they are the TONGA-origin ops the modern gens retired (`nnnn`). TONGA
`arch_id` provenance is `[INFERRED]` (carry wall). `[INFERRED — TONGA not re-decoded this page.]`

---

## 6. Coverage tally (over the 140 real HW opcodes)

Carried from the [ledger §6](../firmware/kernels/opcode-catalog-ledger.md#6-coverage-tally-over-the-140-real-hw-opcodes),
re-confirmed against §2/§3 above:

| Bucket | Count | % |
| --- | --- | --- |
| Body-decoded (per-kernel page or `dormant; covered`) | 78 | 55.7% |
| Planned (per-kernel page authored separately) | 11 | 7.9% |
| NONE (no body decode) | 51 | 36.4% |
| **Total** | **140** | 100% |

The 51 `NONE` split **43 maintained (`// Y`) + 8 dormant (`// n`)**; the 43 maintained-`NONE`
further split **14 SEQ control-spine** (named in the
[dispatch hub](../firmware/seq/dispatch-hub.md), no dedicated body) + **29 genuine
compute/DMA/ACT gap** (the true remaining kernel-lane decode debt). The **20 dormant**
(`// n`) opcodes: 8 are `NONE` (`0x04`, `0x05`, `0x4A`, `0x4B`, `0x4C`, `0x73`, `0x81`,
`0x9C`) + 12 are `dormant; covered` (`0x44`, `0x4F`, `0x54`, `0x5F`, `0x60`, `0x63`,
`0x64`, `0x76`, `0x87`, `0x88` + `0x70`, `0x71`). `[HIGH/OBSERVED — ledger §6.]`

---

## 7. Carry walls (explicit)

| Wall | Status | Scope |
| --- | --- | --- |
| **SortMerge phantom** | `[HIGH/OBSERVED]` | NOT an opcode (§5.1); `0x97` never assigned; `0x98 = TENSOR_SCALAR_SELECT` |
| **MAVERICK (v5) device bodies** | byte `[HIGH/OBSERVED]`, body `[INFERRED/MED]` | `0x26`/`0xB6`/`0xB9`/`0xBA`/`0xF3`/`0xF4` — names+bytes pinned, dispatch interiors FLIX-desynced (§5.2) |
| **SUNDA-only retired ops** | `[HIGH/OBSERVED]` enum, `[CARRIED]` body | BF16 `0x8A–0x8F` (5 ops) + dual-ptr `0x87`/`0x88` — out-of-corpus EXTISA bodies (§5.3) |
| **TONGA `arch_id` / V1 roster** | `[INFERRED]` | decoded on [cross-gen opcode diff](../generations/cross-gen-opcode-diff.md), not here (§5.4) |
| **FLIX-desync device interiors** | opcode/slot/`funcVA` `[HIGH]`, interior `[MED]` | `0x45` Pool inner reduce, `0xBD`/`0xF1` DMA-transpose, v5 late ops — corpus-wide MED ceiling, not per-kernel defects |

---

## See also

* [Opcode-Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the
  authoritative 140-opcode roster + the `172 − 31 − 1` derivation (the source of §1/§2/§3).
* [Cross-Gen `kernel_info_table` / Opcode Matrix](../images/cross-gen-kernel-info-matrix.md)
  — the image-grounded per-gen presence/engine carve (the source of the presence columns).
* [POOL Extended-Opcode (`0xF0`) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the spec
  sub-table (the source of §4).
* [`kernel_info_table` Binary Layout](../firmware/pool/kernel-info-table.md) — the 8-byte
  `(key, funcVA)` record format the Struct column references.
* [Device-Firmware Global Structs](struct-device-firmware-globals.md) — the operand-struct
  field layouts.
* [The Unified Datatype Model](../firmware/kernels/dtype-model.md) — the dtype codes the
  Dtype column names.
* [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) — Surface A, the 178-slot
  fetch table the `CTRL`/SEQ-spine rows reach.
* [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md) —
  the per-gen step-diff + the TONGA V1 deep-dive.
* [The Confidence & Walls Model](../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging.
