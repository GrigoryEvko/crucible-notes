# ISA Numeric Enum-Ordinal Tables

> *All symbols, addresses, ordinals, and LUT bytes on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 rebuild the libraries per ABI — re-confirm any raw address against the target wheel). Every wire ordinal below is read from the L3 silicon encoders / `.rodata` look-up tables / name-decode switches in `neuronxcc/starfish/lib/libwalrus.so` (GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`); the member **names** and the L1 in-memory ordinals come from the `bir::*2string` bodies in `libBIR.so` (build-id `a9b1ea38…`). For `.text`/`.rodata`, virtual address equals file offset (`.text` base `0x62d660`, `.rodata` base `0x1c72000`); `.data` carries a `+0x400000` VMA≠file-offset delta, but **every LUT cited here is `.rodata`-resident**, so `xxd`/`objdump` at the cited VA reads the right bytes directly. Recovered enum/LUT values are binary-derived and citeable. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This is the **numeric-key page** for [Part 2](../). Every encoding page in this part — [PE matmul](pe-matmul-encoding.md) (2.10) through [collective / CustomOp](collective-customop-encoding.md) (2.22) — pins instruction *fields* to byte offsets but defers the **value of each enum field** to one place. This is that place: the 21 ISA enums of the `NEURON_ISA_TPB_*` family, each pinned to its integer **wire ordinal** — the byte the encoder actually stamps, or the `.rodata` LUT byte it reads — alongside the dtype→wire-tag and dtype→stride look-up tables that every operand carries. The bar is concrete: with this page open, a reader can fill in the value of any enum field of any instruction bundle without re-deriving the mapping.

The central fact, and the reason this page must exist as a separate reference, is that **an ISA enum has up to three forms and they frequently disagree**. The libBIR in-memory ordinal (L1, what `bir::*2string` decodes), the BIR-JSON spelling (L2, the textual round-trip name), and the libwalrus silicon byte (L3, the wire ordinal the encoder writes) are *not* the same integer for several families. The `Dtype` wire tag is a full remap ordered by container-size class — practically no dtype equals its L1 ordinal. The `AluOpType` comparison family is reordered on the wire so an operand swap needs no re-encode. `CollectiveKind` carries one out-of-sequence member (`AllToAllV`). `EngineAccumulationType` reorders two members and drops one. Every such L1≠L3 mismatch is flagged ✗ in the tables below; **the binary is authoritative throughout**, and where a sibling encoding page and the binary disagreed this pass, the disagreement is reconciled with an in-place CORRECTION naming the page.

The ordinals are grounded from three classes of in-binary evidence, every one re-walked this pass: **(E1)** name-decode switches — `enum_variant_string_<enum>(int,char*,int)` or `klr::to_string(<enum>&)`, a dense `case N → strncpy("Name")` switch where `case N == ordinal N` and the literal is the wire mnemonic; **(E2)** BIR-enum→wire-byte `convert<…>` encoders, each tagged by its `"Invalid enum variant for enum <Name>"` error string, whose per-case `mov eax,IMM` or `movzx eax, byte[LUT+idx]` is the wire value; **(E3)** the `.rodata` wire LUTs themselves (`byte_1DFBAD0` dtype, `unk_1DF5790` collective, `byte_1DF5780` branch-compare, `byte_1DF577A` wait-mode). The spine — the dtype LUT, the AluOp ordinals, the comp_op set, the CollectiveKind LUT, the EngineType map — is CONFIRMED (encoder writes / LUT bytes read byte-exact this pass, and cross-confirmed against the sibling encoding pages that consume them).

## At a glance — the 21-enum roster

| # | Enum (`NEURON_ISA_TPB_*` / `bir::` / `klr::`) | Cardinality | Form | L1=L3? | Consumed by | Conf |
|---|---|---|---|---|---|---|
| 1 | `_OPCODE` (`bir::InstructionType`) | 161 live (0..255) | name-decode (E1) | n/a | every encoder | CONFIRMED |
| 2 | `_ALU_OP` (`bir::AluOpType`) | 30 (V2/3) / 33 (V4) | convert (E2) | [2.14](tensorscalar-encoding.md), [2.12](pool-reduce-encoding.md) | ✗ remap | CONFIRMED |
| 3 | `_DTYPE` (`bir::Dtype`) | 20 | LUT (E3) | all operands | ✗ full remap | CONFIRMED |
| 4 | `_ACCUM_CMD` (`klr::AccumCmd`) | 5 + reserved | name-decode (E1) | PE / Act | ✗ (wire reorder) | CONFIRMED |
| 5 | `_DGE_COMPUTE_OP` (`klr::DgeComputeOp`) | 2 + reserved | name-decode (E1) | DMA/DGE | = | CONFIRMED |
| 6 | `_CCE_OP` (= `AluOpType` field) | subset `{0,4}` | field identity (E2) | collective copy | = (uses #2) | CONFIRMED |
| 7 | `_PERF_OPT_MODE` (`klr::MatmulPerfMode`) | 4 | identity convert (E2) | PE matmul | ✗ (klr≠BIR) | CONFIRMED |
| 8 | `_TENSOR_SUBDIM` (`klr::TensorSubDim`) | 4 + reserved | name-decode (E1) | reduce/bcast | (shifted +1) | CONFIRMED |
| 9 | `_DROPOUT_THRESHOLD_TYPE` (`klr::`) | 2 + reserved | name-decode (E1) | Dropout | — | CONFIRMED |
| 10 | `_TENS_SCALAR_REV_OPS` (`klr::`) | 4 + reserved | name-decode (E1) | TensorScalar | — | CONFIRMED |
| 11 | `_INDIRECT_DIM` (`bir::IndirectDim`) | 4 | 2string + emit name | gather/indirect | = | STRONG |
| 12 | `_COLLECTIVE_TYPE` (`bir::CollectiveKind`) | 11 (2..10 wired) | LUT (E3) | [2.22](collective-customop-encoding.md) | ✗ reorder | CONFIRMED |
| 13 | `_WAIT_MODE` (`bir::sync::Wait`) | 3 | LUT (E3) | [2.20](sp-sync-encoding.md) | ✗ remap | CONFIRMED |
| 14 | `_UPDATE_MODE` (`bir::sync::Update`) | 2+ | identity write (E2) | [2.20](sp-sync-encoding.md) | = (raw) | CONFIRMED |
| 15 | `_PE_FP32MODE` (PE precision sig.) | `{0,2}` | byte pair (E2) | [2.10](pe-matmul-encoding.md) | n/a | STRONG |
| 16 | `_RAND_ALGORITHM` (`bir::RandomAlgorithmKind`) | 3 | 2string, identity wire | [2.18](rng-encoding.md) | = | STRONG |
| 17 | `_MATMUL_PSUM_ACCUMULATE_FLAGS` | 3-bit field | bit setters (E2) | [2.10](pe-matmul-encoding.md) | bits | CONFIRMED |
| 18 | `_MATMUL_ZERO_REGION` (MX window) | range field | field role (E2) | [2.10](pe-matmul-encoding.md) | n/a | CONFIRMED |
| 19 | `_DGE_OPCODE` | — (no standalone enum) | GAP | DMA-family | INFERRED | INFERRED |
| 20 | `_BRANCH_COMPARE_OP` (`bir::BranchCompareOp`) | 12 (0..11 wired) | LUT (E3) | [2.20](sp-sync-encoding.md) | ✗ reorder | CONFIRMED |
| 21 | `_IMM_SRC` (`NEURON_ISA_TPB_IMM_SRC`) | 3 `{0,1,2}` | validator only (E2) | scalar imm ops | INFERRED names | CONFIRMED |

The two operand-carried LUTs — `_DTYPE` wire-tag (`byte_1DFBAD0` / `byte_1DF5760`) and the parallel dtype→stride table (`qword_1DFC040`) — are tabulated in [§ENUM 3](#enum-3--_dtype). The three engine/sync support enums reached only as a name (`DGEType`, `RandomDistributionKind`, `IndexMissBehavior`) are listed in [§Supplementary](#supplementary--enums-reached-only-as-a-name-or-codepath).

> **NOTE — what "wire ordinal" means here.** Three layers carry an enum: L1 (libBIR in-memory `int`), L2 (BIR-JSON name), L3 (the libwalrus silicon byte the encoder writes / the LUT indexes). **This page is L3.** Where L3 = L1 the row is marked `=`; where it differs the row is marked ✗ with the delta, and the L1 ordinal is given so a reimplementer holding a BIR `int` can find the wire byte. The L1 catalog and the JSON spellings live in [Part 7 BIR](../bir/); the consolidated master enum table is [Part 14](../appendix/).

---

## ENUM 1 — `_OPCODE` (`NEURON_ISA_TPB_OPCODE` / `bir::InstructionType`)

**Grounding (E1):** `neuronxcc::core_v4::enum_variant_string_opcode(int,char*,int)` `@0x143fd80` — a 256-case switch (`cmp edi,0xFF; ja default`) over `jpt @0x1DFD684`; each live case does `lea rsi,"<Mnemonic>"; strncpy(dest,…)`. The case index *is* the ordinal *is* the low byte of the 16-bit `setupHeader` opcode word. core_v3 `@0x1369a40` and core_v2 `@0x127aea0` hold the same switch shape (the MX/Pseudo high band is core_v4-rich). **161 live ordinals**; every other index in `0..255` falls to the default no-op (RESERVED), and `255` is the explicit `"Invalid"` sentinel. Tag: **CONFIRMED**.

The full live ordinal → wire mnemonic map (decimal = hex low byte of the `0x10__` `setupHeader` word):

```text
  1 (0x01) Ldweights              2 (0x02) Matmul               3 (0x03) PeRegWrite
  4 (0x04) WeightMask             5 (0x05) WeightShift          6 (0x06) Ldtags
  7 (0x07) MatmulSparse           8 (0x08) PeManageSeed         9 (0x09) LdweightsMX
 10 (0x0A) MatmulMX
 — gap 11..32 RESERVED —
 33 (0x21) Activate              34 (0x22) ActivateQuantize     35 (0x23) ActivationTableLoad
 36 (0x24) ActivationReadAccumulator                            37 (0x25) Activate2
 — gap 38..47 RESERVED —
 48 (0x30) Exponential
 — gap 49..64 RESERVED —
 65 (0x41) TensorTensorArithOp   66 (0x42) TensorReduceArithOp  67 (0x43) TensorScalarArithOp
 68 (0x44) TensorScalarPtrArithOp                               69 (0x45) Pool
 70 (0x46) Copy                  71 (0x47) Cast                 72 (0x48) Reciprocal
 73 (0x49) Memset                74 (0x4A) RegLoad              75 (0x4B) RegStore
 76 (0x4C) RegShuffle            77 (0x4D) Rng                  78 (0x4E) TensorCumulativeArithOp
 79 (0x4F) TensorScalarPtrMultiArith                            80 (0x50) [RESERVED]
 81 (0x51) TensorTensorBitvecOp  82 (0x52) TensorReduceBitvecOp 83 (0x53) TensorScalarBitvecOp
 84 (0x54) TensorScalarPtrBitvecOp                              85..87 [RESERVED]
 88 (0x58) MaxPoolSelect
 — gap 89..93 RESERVED —
 94 (0x5E) TensorCumulativeBitvecOp                             95 (0x5F) TensorScalarPtrMultiBitvec
 96 (0x60) BatchNormStats        97 (0x61) BatchNormStats2      98 (0x62) BatchNormAggregate
 99 (0x63) BatchNormGradAccumulate                             100 (0x64) BatchNormParamLoad
101 (0x65) BatchNormBackProp    102 (0x66) LoadParameterRam    103 (0x67) PoolBufferLoad
104 (0x68) Gather               105 (0x69) LoadMaskSelect      106 (0x6A) StreamShuffle
107 (0x6B) StreamTranspose      108 (0x6C) Max8                109 (0x6D) MatchValueLoad
110 (0x6E) FindIndex8           111 (0x6F) MatchReplace8       112 (0x70) TensorScalarImmLdArith
113 (0x71) TensorScalarImmLdBitvec                            114 (0x72) CopyPredicated
115 (0x73) RoiAlign             116 (0x74) TensorScalarAddr    117 (0x75) [RESERVED]
118 (0x76) Rand                 119 (0x77) RandGetState        120 (0x78) RandSetState
121 (0x79) EmbeddingUpdate      122 (0x7A) LoadPoolArgument    123 (0x7B) TensorDequantize
124 (0x7C) CrossLaneReduceArith 125 (0x7D) CrossLaneReduceBitvec               126 (0x7E) Iota
127 (0x7F) Dropout              128 (0x80) [RESERVED]          129 (0x81) JpegDecode
130 (0x82) TransposeBatchNormStats2                           131 (0x83) TransposeTensorReduceArithOp
132 (0x84) TransposeTensorReduceBitvecOp                      133 (0x85) CustomOpHeader
134 (0x86) CustomOpPayload      135..141 [RESERVED]           142 (0x8E) BatchNormParamLoad2
143..145 [RESERVED]            146 (0x92) TensorScalarAffineSelect
147 (0x93) TransposeTensorScalarArithOp                       148 (0x94) BatchNormGradAccumulate2
149 (0x95) ModifyPoolConfig    150 (0x96) Sort                151 (0x97) [RESERVED]
152 (0x98) TensorScalarSelect  153 (0x99) CastPredicated      154 (0x9A) TensorScalarCacheReduce
155 (0x9B) DveReadAccumulator  156 (0x9C) TensorReduceRangeCheck               157 (0x9D) ScalarTensorTensorArith
158 (0x9E) ScalarTensorTensorBitvec                           159 (0x9F) EngineNop
160 (0xA0) EventSemaphore      161 (0xA1) Halt                162 (0xA2) Drain
163 (0xA3) InstructionFlush    164 (0xA4) Nop                 165 (0xA5) Write   [PeRegWrite alias]
166 (0xA6) Notify              167 (0xA7) Move    [RegisterMove alias]         168 (0xA8) AluOp
169 (0xA9) CompareBranch       170 (0xAA) TensorLoad          171 (0xAB) TensorStore
172..175 [RESERVED]           176 (0xB0) EventSemaphoreRangeClear              177 (0xB1) SetOrderingMode
178 (0xB2) MoveShape           179 (0xB3) PollSem             180 (0xB4) TestEventSem
181 (0xB5) BranchPrefetchHint  182 [RESERVED]                183 (0xB7) UcodeConfig
184 (0xB8) DMAMemcpy           185,186 [RESERVED]            187 (0xBB) DmaIndirect
188 (0xBC) RangeSelect         189 (0xBD) DmaTranspose        190 (0xBE) GetSequenceBounds
191 (0xBF) Sb2sbCollective     192 [RESERVED]                193 (0xC1) PseudoDMATrigger
194 (0xC2) PseudoDMARearm      195 (0xC3) PseudoDMABarrier    196 (0xC4) PseudoDMAMemcpyFullInd
197 (0xC5) PseudoSemaphoreSet  198 (0xC6) PseudoLoadActFuncSet 199 (0xC7) PseudoTriggerAllReduce
200 (0xC8) PseudoTriggerCollective                            201 (0xC9) PseudoReadVarAddr
202 (0xCA) PseudoEmbeddingUpdate                              203 (0xCB) PseudoSendRecv
204 (0xCC) PseudoBranchLabel   205 (0xCD) PseudoTensorStore   206 (0xCE) PseudoTensorLoad
207 (0xCF) PseudoDMASwapQueueSet                              208 (0xD0) PseudoSetRngSeed
209 (0xD1) PseudoFunctionBegin 210 (0xD2) PseudoFunctionReturn 211 (0xD3) PseudoFunctionCall
212 (0xD4) PseudoDmaDirect2d   213 (0xD5) PseudoSyncBarrier   214 (0xD6) PseudoRangeCheck
215 (0xD7) PseudoJpegDecode    216 (0xD8) PseudoCoreBarrier   217 (0xD9) PseudoTriggerCollective2
218 (0xDA) PseudoExtension     219 (0xDB) PseudoCurProcessingRankID            220 (0xDC) PseudoGidLoad
221 (0xDD) PseudoBranchPrefetchHint                           222 (0xDE) PseudoTensorCompletion
223 (0xDF) PseudoInst          224 (0xE0) SparsityCompress    225 (0xE1) SparsityCompressTag
226 (0xE2) Rand2               227 (0xE3) QuantizeMX          228 (0xE4) ConvLutLoad
229 (0xE5) TensorTensorScanArith                              230 (0xE6) TensorScalarCacheCumulative
231 (0xE7) IndirectCopy        232 (0xE8) CopyPredicatedScalar 233 (0xE9) DveReadIndices
234 (0xEA) SelectReduce        235 (0xEB) Hint   [BranchOutcomeHint-suffix placeholder]
 — gap 236..239 RESERVED —
240 (0xF0) ExtendedInst        241 (0xF1) DmaGatherTranspose  242 (0xF2) NonzeroWithCount
 — gap 243..254 RESERVED —
255 (0xFF) Invalid             [explicit sentinel: strncpy("Invalid")]
```

> **NOTE — the MX trio and the arch-specific Activate slot.** `LdweightsMX`/`MatmulMX`/`QuantizeMX` (9/10/227) are the low bytes of the `setupHeader` words `0x1009`/`0x100A`/`0x10E3`; the `0x10__` prefix is the universal MX-class nibble and the low byte is *this* ordinal — consistent, no remap. Ordinals 33 (`Activate`) and 37 (`Activate2`) are the **same** public BIR Activation op at different arch levels: CoreV2/V3 emit `0x21`, CoreV4 emits `0x25` (`mov BYTE[rbp-0xf0],0x25 @0x143c25f`), selected at emit time by the constraint layer — not two distinct ops. Cases 165 `"Write"` / 167 `"Move"` are suffix-truncated labels for `PeRegWrite` / `RegisterMove` (the binary literally `strncpy`s those shorter strings).

---

## ENUM 2 — `_ALU_OP` (`bir::AluOpType`)

**Grounding (E2):** two encoders stamp the *same* wire byte. (a) the CoreV2/V3 `convert` `sub_12039C0 @0x12039C0` (`cmp edi,0x1D; ja default`, jump-table base `0x1DF4BB8`, 30 cases `0..29`); (b) the core_v4 `convert` `sub_142E030 @0x142E030` (`cmp edi,0x20`, base `0x1DFB5E8`, 33 cases `0..32`, extends (a) with `abs_max`/`abs_min`/`mod_int`). Both carry the error string `"Invalid enum variant for enum AluOpType"`. Member **names** from `bir::AluOpType` 2string (`libBIR @0x400600`), joined 1:1 by ordinal. Tag: **CONFIRMED** (byte) / **STRONG** (name).

| L1 ord | name | L3 `_ALU_OP` byte | src | Δ vs L1 |
|---|---|---|---|---|
| 0 | `bypass` | `0x00` | `@0x142e220` | = |
| 1 | `bitwise_not` | `0x01` | `@0x142e230` | = |
| 2 | `arith_shift_left` | `0x02` | `@0x142e228` | = |
| 3 | `arith_shift_right` | `0x03` | `@0x142e210` | = |
| 4 | `add` | `0x04` | `@0x142e200` | = |
| 5 | `subtract` | `0x05` | `@0x142e1f0` | = |
| 6 | `mult` | `0x06` | `@0x142e1e0` | = (spelled `mult`) |
| 7 | `divide` | `0x07` | `@0x142e1d0` | = |
| 8 | `max` | `0x08` | `@0x142e1c0` | = |
| 9 | `min` | `0x09` | `@0x142e1b0` | = |
| 10 | `bitwise_and` | `0x0A` | `@0x142e1a0` | = |
| 11 | `bitwise_or` | `0x0B` | `@0x142e190` | = |
| 12 | `bitwise_xor` | `0x0C` | `@0x142e180` | = |
| 13 | `logical_and` | `0x0D` | `@0x142e170` | = |
| 14 | `logical_or` | `0x0E` | `@0x142e160` | = |
| 15 | `logical_xor` | `0x0F` | `@0x142e150` | = |
| 16 | `logical_shift_left` | `0x10` | `@0x142e140` | = |
| 17 | `logical_shift_right` | `0x11` | `@0x142e130` | = |
| 18 | `is_equal` | `0x12` | `@0x142e120` | = |
| 19 | `not_equal` | `0x18` (24) | `@0x142e110` | ✗ **REORDER** |
| 20 | `is_gt` | `0x13` (19) | `@0x142e100` | ✗ **REORDER** |
| 21 | `is_ge` | `0x14` (20) | `@0x142e0f0` | ✗ **REORDER** |
| 22 | `is_lt` | `0x16` (22) | `@0x142e0e0` | ✗ **REORDER** |
| 23 | `is_le` | `0x15` (21) | `@0x142e0d0` | ✗ **REORDER** |
| 24 | `average` | ERR (no byte) | default (dedicated op) | ✗ |
| 25 | `elemwise_mul` | ERR (no byte) | default (dedicated op) | ✗ |
| 26 | `pow` | `0x1A` | `@0x142e0c0` | = |
| 27 | `mod` | `0x1B` | `@0x142e0b0` | = |
| 28 | `rsqrt` | `0x1D` (29) | `@0x142e0a0` | ✗ **REORDER** |
| 29 | `abs` | `0x19` (25) | `@0x142e090` | ✗ **REORDER** |
| 30 | `abs_max` | `0x20` (32) | `@0x142e080` (core_v4) | ✗ |
| 31 | `abs_min` | `0x21` (33) | `@0x142e070` (core_v4) | ✗ |
| 32 | `mod_int` | `0xC8` (200) | `@0x142e068` (core_v4) | ✗ |

> **QUIRK — the comparison family is reordered because the predicates are anti-symmetric.** An operand swap flips `gt↔lt`, `ge↔le`; the wire band groups them so the swapped form needs no re-encode. Order-sensitive ops must therefore never be operand-canon-swapped on the wire. `mod_int(32)` emits the literal `0xFFFFFFC8` (the int32-both-operand extended band `0xC4..0xE1`), masked to the 1-byte ALU field → `0xC8`. The COLLECT-mode encoder (a) tops out at case 29; `abs_max`/`abs_min`/`mod_int` (30..32) are core_v4-only.
>
> **CORRECTION — CoreV2 and CoreV4 share one ALU-op band** ([2.14](tensorscalar-encoding.md) already carries this). An earlier brief claimed CoreV2 used a "compressed" comparison scheme distinct from CoreV4. Disassembly shows `sub_12039C0` emits the *same* bytes as the CoreV4 band for ordinals `0..29` (`not_equal=19→0x18`, `is_gt=20→0x13`, `is_ge=21→0x14`, `is_lt=22→0x16`, `is_le=23→0x15`, `rsqrt=28→0x1D`, `abs=29→0x19`). One band; CoreV4 only adds `30/31/32` and lacks nothing CoreV2 has except the dedicated `24/25` slots both share.

`_CCE_OP` (ENUM 6) is *this* field: `InstDMACopy::getCceOp()` returns an `AluOpType`, encoded by the same converter. The shipped collective-copy legalization admits only the subset `{bypass=0x00, add=0x04}` (`@0x1D25BA0` / `@0x1D32258` asserts; `legalize_cce_dma @0x1CE2270`). For CCE ordinals, use this table.

---

## ENUM 3 — `_DTYPE` (`bir::Dtype` → wire tag + stride)

**Grounding (E3):** two `.rodata` LUTs, both indexed by the `bir::Dtype` L1 ordinal (`0..19`), the byte read being the `NEURON_ISA_TPB_DTYPE` wire tag. `byte_1DFBAD0` is the core_v4 path (read by `generateLdweightMx`); `byte_1DF5760` the older core_v2/v3 path (consumer `sub_120E650`). Both are 20-entry — confirmed by the parallel dtype→stride table `qword_1DFC040`, which runs exactly 20 qwords (`0x1DFC040..0x1DFC0DF`) before the rodata strings begin. The two LUT byte sequences (xxd this pass), and confirmed against the [2.10 PE-matmul](pe-matmul-encoding.md) in-dtype LUT (`byte_1DF5760`):

```text
byte_1DFBAD0 (core_v4): 03 02 10 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c
byte_1DF5760 (pre-v4):  03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c
                              ^^idx2: v4=0x10(16) vs pre-v4=0x05(5)
```

Member **names** from `bir::Dtype` 2string (`libBIR @0x2641e0`). Tag: **CONFIRMED** (byte) / **STRONG** (name). This is a **full remap** — practically no entry equals its L1 ordinal.

| L1 ord | name | L3 wire tag (v4 / pre-v4) | stride | Δ vs L1 |
|---|---|---|---|---|
| 0 | `uint8` | `0x03` / `0x03` | 1 | ✗ |
| 1 | `int8` | `0x02` / `0x02` | 1 | ✗ |
| 2 | `float4_e2m1fn_x4` | `0x10` (16) / `0x05` (5) | 2 | ✗ **LUT-DIFF** |
| 3 | `float8e3` | `0x0D` / `0x0D` | 1 | ✗ |
| 4 | `float8e4` | `0x0E` / `0x0E` | 1 | ✗ |
| 5 | `float8_e4m3fn` | `0x0E` / `0x0E` | 1 | ✗ (shares e4m3 tag with #4) |
| 6 | `float8_e8m0fnu` | `0x03` / `0x03` | 1 | ✗ (E8M0 scale; uint8 class) |
| 7 | `float8e5` | `0x0F` / `0x0F` | 1 | ✗ |
| 8 | `float8_e4m3fn_x4` | `0x0E` / `0x0E` | 4 | ✗ (u32 container; shares tag 14) |
| 9 | `float8_e5m2_x4` | `0x0F` / `0x0F` | 4 | ✗ (shares tag 15) |
| 10 | `uint16` | `0x05` / `0x05` | 2 | ✗ |
| 11 | `int16` | `0x04` / `0x04` | 2 | ✗ |
| 12 | `bfloat16` | `0x06` / `0x06` | 2 | ✗ |
| 13 | `float16` | `0x07` / `0x07` | 2 | ✗ |
| 14 | `uint32` | `0x09` / `0x09` | 4 | ✗ |
| 15 | `int32` | `0x08` / `0x08` | 4 | ✗ |
| 16 | `float32` | `0x0A` / `0x0A` | 4 | ✗ |
| 17 | `float32r` | `0x0B` / `0x0B` | 4 | ✗ (fp32r; own tag + gate) |
| 18 | `uint64` | `0x01` / `0x01` | 8 | ✗ |
| 19 | `int64` | `0x0C` / `0x0C` | 8 | ✗ |

> **QUIRK — the wire tag is ordered by container-size class, not BIR ordinal.** That is why almost no `Dtype` int equals its tag. The distinct emitted tag set is `{1..16}`. The sole LUT difference is index 2 (`float4_e2m1fn_x4`): core_v4 gives FP4-packed its own wire tag `16`, the pre-v4 path carries it as the `uint16` tag `5`.
>
> **GOTCHA — the trailing LUT bytes are a different table.** Bytes after the 20-entry block (`byte_1DFBAD0[20..25] = 11 15 17 13 14 19`) are a *separate* adjacent extended/quantized tag sub-table (MX-scale / x4 lanes), **not** Dtype ordinals 20+. `Dtype` has exactly 20 members (stride-table length). A third, *different* 20-entry container-tag LUT exists for the `QuantizeMx` ADDR4 byte[34/35] path (`u32→9`); that is **not** `byte_1DFBAD0`.
>
> **NOTE — CoreV3 gates the ×4-packed dtypes.** [2.10](pe-matmul-encoding.md) records that CoreV3 `sub_1348870` *rejects* `{2=fp4_e2m1fn_x4, 8=fp8_e4m3fn_x4, 9=fp8_e5m2_x4}` (matmul does not consume ×4-packed operands directly — that is the gen4 MX path), while CoreV2's flat LUT silently maps them to `{0x05,0x0E,0x0F}`. The encoding bytes are shared for the other 17 tags; only the legality gate diverges.

---

## ENUM 4 — `_ACCUM_CMD` (`klr::AccumCmd`) and `EngineAccumulationType` wire

**Grounding (E1):** `klr::to_string(klr::AccumCmd&) @0xf7f630` — a 6-case switch (`cmp [rsi],5; ja default`, jump-table base `0x1DEF348`). This is the **KLIR text-IR** enum; the **libBIR wire** form is a *separate* space (`EngineAccumulationType`, see the wire row below). Tag: **CONFIRMED**.

| klr ord | name | grounding |
|---|---|---|
| 0 | (reserved) | default → `"UNABLE TO PRINT"` |
| 1 | `Idle` | `@0xf7f668` |
| 2 | `Zero` | `@0xf7f680` |
| 3 | `Accumulate` | `@0xf7f698` |
| 4 | `ZeroAccumulate` | `@0xf7f6b0` |
| 5 | `LoadAccumulate` | `@0xf7f650` |

> **QUIRK — the wire byte reorders the BIR `EngineAccumulationType`.** The CoreV2/3 wire encoder `convert sub_12038D0 @0x12038D0` (error `"Invalid enum variant for enum EngineAccumulationType"`, 6-case base `0x1DF4BA0`) maps L1 `{0 Idle, 1 Zero, 2 Accumulate, 3 ZeroAccumulate, 4 AddAccumulate, 5 LoadAccumulate}` to wire bytes `{0→0, 1→1, 2→ERR, 3→3, 4→2, 5→4}`. So the wire `_ACCUM_CMD` = `{Idle:0, Zero:1, ZeroAccumulate:3, AddAccumulate:2, LoadAccumulate:4}`; `Accumulate(2)` has *no* wire byte (selects a codepath). The `klr::AccumCmd` ordinals above are the text-IR space and do not carry `AddAccumulate`. **CONFIRMED.**

---

## ENUM 5 — `_DGE_COMPUTE_OP` (`klr::DgeComputeOp`) · ENUM 6 — `_CCE_OP`

**ENUM 5 grounding (E1):** `klr::to_string(klr::DgeComputeOp&) @0xf7f9e0`, the inverse `DgeComputeOp_des @0xf4a680`, and `backend::AluOpType2DGEComputeOp @0x120b9e0` (maps `AluOp::bypass→none`, `add→add`). Tag: **CONFIRMED**.

| ord | name | grounding |
|---|---|---|
| 0 | (reserved) | default → `"UNABLE TO PRINT"` |
| 1 | `none` | inline `"none"` (`mov dword 0x656E6F6E`) `@0xf7fa0c`; `AluOp::bypass` maps here |
| 2 | `add` | `"add"` `@0xf7fa30`; `AluOp::add` maps here |

The SW-DGE descriptor either copies (`none`) or accumulates (`add`); the full `AluOp` space is **not** available on the DGE path (`are_valid_memcpy_dtypes @0x29378e` gates it).

**ENUM 6 (`_CCE_OP`):** not a distinct enum — it is the `AluOpType` returned by `InstDMACopy::getCceOp()`, encoded by the ENUM 2 converter. Shipped legalization admits only `{bypass=0x00 (plain copy), add=0x04 (reduce-on-copy)}`. **Use the ENUM 2 table.** Tag: **CONFIRMED** (field identity) / **STRONG** (value set).

---

## ENUM 7 — `_PERF_OPT_MODE` (`klr::MatmulPerfMode`)

**Grounding (E1+E2):** `klr::to_string(klr::MatmulPerfMode&) @0xf7f030` (`cmp eax,1/2/3`); the CoreV2/3 wire encoder `convert sub_1203630 @0x1203630` (`"Invalid enum variant for enum MatmultPerfMode"`) is **identity** `mov eax,edi` for `edi∈{0,1,2,3}` (`edi>3 → ERR`). Tag: **CONFIRMED**.

| klr ord | name | wire byte (`sub_1203630`) |
|---|---|---|
| 0 | (default → UNABLE) | `0` (identity) |
| 1 | `None` | `1` |
| 2 | `DoubleRow` | `2` |
| 3 | `DoubleRowSwInterleave` | `3` |
| >3 | — | ERR (no wire form) |

> **QUIRK — three different ordinal spaces.** The libBIR `MatmultPerfMode` is `{0:None, 1:DoubleRow, 2:DoubleColumn, 3:DoublePixel, 4:DoubleRowSwInterleave}`; the KLIR `klr::MatmulPerfMode` is the *smaller* `{1:None, 2:DoubleRow, 3:DoubleRowSwInterleave}`. The wire encoder `sub_1203630` is identity over `0..3`, so on the wire the BIR `{None=0, DoubleRow=1, DoubleColumn=2, DoublePixel=3}` ride identically and BIR `DoubleRowSwInterleave(4)` ERRs (not wire-encodable on this path). The klr text-IR ordinal ≠ the BIR ordinal ≠ always the wire byte — the binary is authoritative for each layer. The `+0x21`/`+0x23` perf-opt dtype bytes ([2.10](pe-matmul-encoding.md), via `perfModeToDstDtype @0x1203630`) consume this mode.

---

## ENUM 8 — `_TENSOR_SUBDIM` (`klr::TensorSubDim`)

**Grounding (E1):** `klr::to_string(klr::TensorSubDim&) @0xf7fe30`. Tag: **CONFIRMED**.

| ord | name | grounding |
|---|---|---|
| 0 | (reserved) | default → `"UNABLE TO PRINT"` |
| 1 | `X` | `loc_F7FEB0` |
| 2 | `XY` | `@0xf7fe48` |
| 3 | `XYZ` | `loc_F7FE80` |
| 4 | `XYZW` | `@0xf7fe65` |

These are cumulative axis prefixes; `klr::TensorSubDim` shifts the libBIR `AxisListType {0:X, 1:XY, 2:XYZ, 3:XYZW}` up by 1 (0 reserved).

---

## ENUM 9 — `_DROPOUT_THRESHOLD_TYPE` (`klr::DropoutThresholdType`)

**Grounding (E1):** `klr::to_string(klr::DropoutThresholdType&) @0xf7f5b0`. Tag: **CONFIRMED**.

| ord | name | grounding |
|---|---|---|
| 0 | (reserved) | default → `"UNABLE TO PRINT"` |
| 1 | `DropRate` | `@0xf7f600` (threshold = P(drop)) |
| 2 | `KeepRate` | `@0xf7f618` (threshold = P(keep)) |

Used on the `Dropout` op (opcode 127, ENUM 1).

---

## ENUM 10 — `_TENS_SCALAR_REV_OPS` (`klr::TensorScalarReverseOps`)

**Grounding (E1):** `klr::to_string(klr::TensorScalarReverseOps&) @0xf7fd90`. Tag: **CONFIRMED**.

| ord | name | meaning |
|---|---|---|
| 0 | (reserved) | default → `"UNABLE TO PRINT"` |
| 1 | `none` (`loc_F7FE10`) | neither operand reversed |
| 2 | `first` (`@0xf7fda8`) | reverse op0 (scalar − tensor) |
| 3 | `second` (`loc_F7FDE0`) | reverse op1 |
| 4 | `both` (`@0xf7fdc5`) | reverse both ops |

Controls operand-order reversal for the non-commutative ALU ops (subtract/divide/shift) in a `TensorScalar` instruction.

---

## ENUM 11 — `_INDIRECT_DIM` (`bir::IndirectDim`)

**Grounding:** `bir::IndirectDim` 2string (`libBIR @0x4021a0`); libwalrus emits the dim **name** via `convertIndirectDim sub_1204220 @0x1204220` and gates legality via `valid_indirect_dim_by_mode` (`@0x1DD4B70`). The dim ordinal *is* the BIR ordinal (no remap). Tag: **STRONG**.

| ord | name |
|---|---|
| 0 | `W` |
| 1 | `Z` |
| 2 | `Y` |
| 3 | `X` |

Related — `klr::IndexMissBehavior @0xf82fc0` (the indirect-copy miss handler, *not* the dim): `0`=reserved/UNABLE, `1`=`Imm` (fill with immediate), `2`=`Skip` (skip the miss) — `@0xf83010`/`@0xf83020`, **CONFIRMED**.

---

## ENUM 12 — `_COLLECTIVE_TYPE` (`bir::CollectiveKind`)

**Grounding (E3+E2):** the CoreV2/3 wire encoder `convert sub_1203770 @0x1203770` (`"Invalid enum variant for enum CollectiveKind"`) does `sub edi,2; cmp edi,8; movzx eax, byte[unk_1DF5790 + (kind-2)]`. The 9-byte LUT `unk_1DF5790` (xxd this pass, and `objdump`-verified byte-identical in [2.22](collective-customop-encoding.md)):

```text
unk_1DF5790 = 01 02 03 04 09 05 06 07 08      (index = kind − 2; only kinds 2..10 wire-encodable)
```

Member **names** from `bir::CollectiveKind` 2string (`libBIR @0x4016c0`). Tag: **CONFIRMED** (byte) / **STRONG** (name).

| L1 kind | name | LUT idx (kind−2) | wire byte | Δ vs L1 |
|---|---|---|---|---|
| 0 | `SendRecv` | — | ERR (not wire-encodable here) | · |
| 1 | `SendRecvCCE` | — | ERR | · |
| 2 | `AllReduce` | 0 | `0x01` | ✗ |
| 3 | `ReduceScatter` | 1 | `0x02` | ✗ |
| 4 | `AllGather` | 2 | `0x03` | ✗ |
| 5 | `AllToAll` | 3 | `0x04` | ✗ |
| 6 | `AllToAllV` | 4 | `0x09` **(out of sequence)** | ✗ |
| 7 | `Permute` | 5 | `0x05` | ✗ |
| 8 | `PermuteReduce` | 6 | `0x06` | ✗ |
| 9 | `PermuteImplicit` | 7 | `0x07` | ✗ |
| 10 | `PermuteReduceImplicit` | 8 | `0x08` | ✗ |

> **QUIRK — `AllToAllV` (kind 6) maps to wire `0x09`, out of sequence.** Every other kind is a `−1` shift from its ordinal; `AllToAllV` jumps to `0x09` and the `Permute` family slides down to fill `0x05..0x08` contiguously. A naive `wire = kind − 1` reimplementation mis-encodes `AllToAllV` and the entire permute family. Use the LUT verbatim. `SendRecv(0)`/`SendRecvCCE(1)` are **not** wire-encodable via this LUT — they use the dedicated `PseudoSendRecv` / `Sb2sbCollective` opcodes (ENUM 1, #203/#191).

---

## ENUM 13 — `_WAIT_MODE` (`bir::sync::Wait::WaitMode`)

**Grounding (E3):** CoreV2 `visitInstEventSemaphore @0x1217df0` computes the wait byte as `sub_120BE70(Wait::getMode())` and stores it at `bundle+0x04` (`mov [r14+4],al @0x1218245`). `sub_120BE70 @0x120BE70`: `cmp edi,2; ja default; movzx eax, byte[unk_1DF577A + mode]`. The 3-byte LUT `unk_1DF577A` (xxd this pass, byte-identical in [2.20](sp-sync-encoding.md)): `07 05 85`. Tag: **CONFIRMED**.

| mode | name | wire byte | meaning |
|---|---|---|---|
| 0 | (reserved) | `0x07` | not a supported wait mode → error |
| 1 | `SEM_GE_IMM` | `0x05` | wait until semaphore ≥ immediate |
| 2 | `SEM_GE_REG` | `0x85` (= `0x05 \| 0x80`) | wait until semaphore ≥ register (bit `0x80` = compare-against-register) |

The encoder rejects `mode∉{1,2}` with `"only two wait modes supported now: SEM_GE_IMM and SEM_GE_REG"` (`@0x1d66598`). Only the two GE forms are wired on `EventSemaphore` in this build.

---

## ENUM 14 — `_UPDATE_MODE` (`bir::sync::Update::UpdateMode`)

**Grounding (E2):** same encoder (`visitInstEventSemaphore @0x1217df0`). Unlike wait-mode, the update mode is written **raw / identity, no LUT** (`call Update::getMode @0x12180ed`; `mov [r14+0x22],al @0x12180f9` — update byte at `bundle+0x22`). The mode ordinal *is* the wire byte. Tag: **CONFIRMED**.

| mode | name | wire byte | meaning |
|---|---|---|---|
| 0 | replace / set | `0` (raw) | overwrite the semaphore value |
| 1 | increment | `1` (raw) | atomic add (`"instr.sem_increment"`) |

> **GOTCHA — wait/update asymmetry.** `_WAIT_MODE` is LUT-remapped (`07/05/85`); `_UPDATE_MODE` is written identity. Do **not** apply the wait LUT to update_mode or vice-versa. The richer runtime update-subtype set `{0x11 evt-set, 0x13 sem-inc, 0x14 sem-dec, 0x15 add-imm, 0x17 sub-imm, 0x19 wr-imm}` documented in [2.20](sp-sync-encoding.md) / [1.14](../arch/execution-sync-model.md) is the runtime record's lane-2 subtype byte (`byte_1DF5774`), not this compile-side `EventSemaphore` update ordinal — the two are placed by different encoders.

---

## ENUM 15 — `_PE_FP32MODE` (matmul FP32 precision signature)

**Grounding (E2):** FP32 matmul uses a two-pass 2-FMA lowering; the precision mode rides `bundle+0x20`/`+0x21`. For `fp32r` (Dtype 17) the encoder writes the 16-bit signature `*(u16)(bundle+0x20) = 0x020A` ([2.10](pe-matmul-encoding.md) `@0x1258f16`/`@0x124972d`): byte `+0x20 = 0x0A` (the `float32` wire dtype tag, ENUM 3) and byte `+0x21 = 0x02` (the FP32MODE selector). Plain fp32 (Dtype 16) uses the standard 2-FMA without the mode byte. Tag: **STRONG** (the discrete member set is not name-decoded; the wire bytes are CONFIRMED).

| fp32 mode | byte `+0x21` | dtype `+0x20` | grounding |
|---|---|---|---|
| standard fp32 (Dtype 16) | `0x00` | `0x0A` | no mode byte |
| fp32r 2-FMA round (Dtype 17) | `0x02` | `0x0A` | `0x020A` signature, CONFIRMED |

⇒ `_PE_FP32MODE ∈ {0 = default/full, 2 = fp32r round 2-FMA}`. The perf-mode `+0x21` byte (ENUM 7, `{0/2/3}`) overlaps this byte position — a shared field.

---

## ENUM 16 — `_RAND_ALGORITHM` (`bir::RandomAlgorithmKind`)

**Grounding:** `bir::RandomAlgorithmKind2string @0x401de0` (libBIR) + inverse `string2 @0x40ef50`, round-trip verified; the simulator's `visitInstRand @0x1d6290` dispatches on this ordinal (`mov esi,[rdi+0xF0]`). The ISA `random_algorithm` field on `Rng` (opcode 118) carries the ordinal **identity** (no remap on this path). Tag: **STRONG** (ordinal CERTAIN from 2string; wire = identity). [2.18](rng-encoding.md) carries the same map.

| ord | name | grounding |
|---|---|---|
| 0 | `LFSR` | 2string default tail; 32-bit Fibonacci LFSR |
| 1 | `PCG32` | 2string `a2==1`; XSH-RR |
| 2 | `PHILOX_1` | 2string `a2==2`; Philox-4×32-10 |

(default → FATAL `"Unknown RandomAlgorithmKind"`.)

> **CORRECTION — `LFSR=0, PCG32=1, PHILOX_1=2`.** An earlier note implied `PCG32=0`. The binary is authoritative: the 2string `@0x401de0` / `string2 @0x40ef50` decode in order `LFSR / PCG32 / PHILOX_1`. The companion `RandomDistributionKind` (the `Rng` `distribution` field, identity wire) = `{0:Raw, 1:Uniform, 2:Normal, 3:Binomial}`. The gen-2 `Rand2` path runs a fixed `XORWOW` over a *separate* state vector and namespace — do not conflate the two ([2.18](rng-encoding.md)).

---

## ENUM 17 — `_MATMUL_PSUM_ACCUMULATE_FLAGS` (bundle `+0x2B`, a 3-bit field)

**Grounding (E2):** not a scalar enum — a 3-bit flag byte at `bundle+0x2B`, OR-ed by the calc-chain setters (CoreV2 `generateMatMul` inline `@0x1248650`; CoreV3 `generateMatMulAccumulateFlag @0x1428630`). Init `0x00` (`mov byte[r15+0x2b],0 @0x12493b3`). Tag: **CONFIRMED** (masks) / **STRONG** (valid-set).

| bit | mask | name | setter site | meaning |
|---|---|---|---|---|
| 0 | `0x01` | START | `or [r15+0x2b],0x1 @0x124963d` | zero-then-write the PSUM bank |
| 1 | `0x02` | STOP | `or [r15+0x2b],0x2 @0x1249664` | drain / read the bank out |
| 2 | `0x04` | ACCUMULATE | `or [rbx+0x2b],0x4 @0x14288b5` | add into the bank (interior MM) |

Valid byte combinations (`checkAccumulationFlag @0x150db50`): `{0,1,2,3,4,6}` — any combo **except** `5` (START+ACCU) and `7` (all three). `0`=passthrough, `3`=START+STOP (single-MM group), `6`=STOP+ACCU (last interior+drain). CoreV3 skips the byte when module arch `> 40` (`cmp [rax+0xac],0x28; jg @0x142865d`); CoreV2 always writes it; CoreV4 MX copies the pre-packed byte raw from `inst+0xF0` to `bundle+0x2B` (`@0x143ef1d`). Same silicon bit contract across gens.

---

## ENUM 18 — `_MATMUL_ZERO_REGION` (MX PSUM zero-window, bundle `+0x2F`)

**Grounding (E2):** on CoreV4 MX, `generateMatmultMx` writes a PSUM zero-region descriptor at `bundle+0x2F` copied from `inst+0x118` (present-tag `inst+0x138`). It is the pow2-aligned PSUM **bank range** the START matmul clears (`get_psum_zero_region_bank_range`), not a small value-enum. Tag: **CONFIRMED** (field location + role).

| field | bundle off | source | semantics |
|---|---|---|---|
| `zero_region` | `+0x2F` | `inst+0x118` (MX only) | PSUM bank window the START-MM zeroes |

⇒ `_MATMUL_ZERO_REGION` has **no enumerated value→name set**; the "flag" is the START bit of ENUM 17 plus this packed bank-range window. Its overlap handling (`find_overlapped_psum_zero_region_groups @0x16dee50`) is a legalization concern.

---

## ENUM 19 — `_DGE_OPCODE` (no standalone enum — GAP)

A `.dynsym`/`.rodata` search for `DGE_OPCODE` / `DgeOpcode` finds **no** standalone klr/ISA enum string or name-decode in `libwalrus.so` this build (unlike `_DGE_COMPUTE_OP`, ENUM 5). The SW-DGE/HW-DGE descriptor opcodes are a **subset of the main `_OPCODE` table** (ENUM 1): the DMA/descriptor ops (`DMAMemcpy` 184, `DmaIndirect` 187, `DmaTranspose` 189, `DmaGatherTranspose` 241, the `PseudoDMA*` band 193–196/207/212) carry the descriptor; the DGE compute sub-op is ENUM 5. `DGEType {0:None, 1:SWDGE, 2:HWDGE, 3:Unassigned}` (libBIR) selects *which engine*, not an opcode. Tag: **INFERRED** (no separate enum found). ⇒ For `_DGE_OPCODE`, use the DMA-family rows of ENUM 1 + `DGEType` + ENUM 5. **GAP** — no independent value→name table to bit-pin.

---

## ENUM 20 — `_BRANCH_COMPARE_OP` (`bir::BranchCompareOp` / `klr::BrCmpOp`)

**Grounding (E3+E1):** (a) the CoreV2/3 wire encoder `convert sub_1203580 @0x1203580` (`"Invalid enum variant for enum BranchCompareOp"`): `cmp edi,0x0B; ja default; movzx eax, byte[unk_1DF5780 + comp_op]`. The 12-byte LUT `unk_1DF5780` (xxd this pass, byte-identical in [2.20](sp-sync-encoding.md)) is indexed by the **BIR** ordinal. (b) `klr::to_string(klr::BrCmpOp&) @0xf80db0` supplies the klr-shifted names. Tag: **CONFIRMED** (byte) / **CONFIRMED** (klr names).

```text
unk_1DF5780 = 01 02 03 04 05 06 09 0a 0b 0c 0d 0e      (index = BIR comp_op 0..11)
```

| BIR ord | name | klr name | LUT byte | Δ |
|---|---|---|---|---|
| 0 | `IS_LTIMM` | `LtImm` | `0x01` | ✗ |
| 1 | `IS_LEIMM` | `LeImm` | `0x02` | ✗ |
| 2 | `IS_EQIMM` | `EqImm` | `0x03` | ✗ |
| 3 | `IS_NEIMM` | `NeImm` | `0x04` | ✗ |
| 4 | `IS_GEIMM` | `GeImm` | `0x05` | ✗ |
| 5 | `IS_GTIMM` | `GtImm` | `0x06` | ✗ |
| 6 | `IS_LTREG` | `LtReg` | `0x09` **(reorder)** | ✗ |
| 7 | `IS_LEREG` | `LeReg` | `0x0A` | ✗ |
| 8 | `IS_EQREG` | `EqReg` | `0x0B` | ✗ |
| 9 | `IS_NEREG` | `NeReg` | `0x0C` | ✗ |
| 10 | `IS_GEREG` | `GeReg` | `0x0D` | ✗ |
| 11 | `IS_GTREG` | `GtReg` | `0x0E` | ✗ |
| 12 | `Unsupported` | — | ERR (`>11` → default) | · |

> **QUIRK — IMM band `0..5 → 0x01..0x06` (`ord+1`); REG band `6..11 → 0x09..0x0E` (`ord+3`).** A `+2` jump at the IMM→REG boundary leaves wire `0x07`/`0x08` unused, mirroring the wait-mode `0x80`-reg pattern. The klr space additionally carries `Always` (klr ordinal 1) — the unconditional-jump predicate, which BIR `BranchCompareOp` has no member for (it is the absence of a compare). The full klr ordinal table (`@0xf80db0`): `1:Always 2:LtImm 3:LeImm 4:EqImm 5:NeImm 6:GeImm 7:GtImm 8:LtReg 9:LeReg 10:EqReg 11:NeReg 12:GeReg 13:GtReg` (0 → reserved/UNABLE). The branch encoder also negates the predicate via `dword_1DF5720` when the taken edge is the fall-through ([2.20](sp-sync-encoding.md)).

---

## ENUM 21 — `_IMM_SRC` (`NEURON_ISA_TPB_IMM_SRC`)

**Grounding (E2, validation only):** `NEURON_ISA_TPB_IMM_SRC` appears only as a validator argument (`has_valid_immediates @0x127ff90`, `ts_addr_immediate_check @0x12c34c0`). No name-decode / serializer exists. The validator branches on `src∈{0,1,2}`: `test sil,sil` (0), `cmp sil,1`, `cmp sil,2` (`@0x127ff9f`/`@0x127ffa7`). Tag: **CONFIRMED** (3-value cardinality) / **INFERRED** (member names — no name table).

| ord | inferred meaning | grounding |
|---|---|---|
| 0 | immediate (inline field) | `test sil,sil @0x127ff9a` |
| 1 | register | `cmp sil,1 @0x127ff9f` |
| 2 | pointer (imm ptr) | `cmp sil,2 @0x127ffa7` (`"ISA immediate pointer should only be SB/PSUM"`) |

⇒ exactly 3 ordinals `{0,1,2}`; the names are **not** name-decoded in this binary (**GAP**). Companion `NEURON_ISA_TPB_IMM_REG` / `IMM_VAL_INST_FIELD` / `IMM64` are field-typedefs, not value enums.

---

## Supplementary — enums reached only as a name or codepath

These are in the `NEURON_ISA` enum family but emit no independent silicon ordinal here (ordinals are the libBIR L1, which equals the wire form where any exists):

- **`DGEType`** (`libBIR @0x400dc0`): `0:None 1:SWDGE 2:HWDGE 3:Unassigned` — selects the descriptor-gen engine, not an opcode. Identity, no remap.
- **`RandomDistributionKind`** (`libBIR @0x401ee0`): `0:Raw 1:Uniform 2:Normal 3:Binomial` — `Rng`/`Rand2` `distribution` field, identity wire.
- **`IndexMissBehavior`** (`klr @0xf82fc0`): `1:Imm 2:Skip` — the indirect-copy miss handler (see ENUM 11).
- **`_DTYPE_PAIR`**: a *pair* of ENUM 3 wire tags (`{dtype_hi, dtype_lo}` = in0/in1), not its own ordinal set.
- **`_EVENTS` / per-N `_IMM_SRC`**: the events struct (`wait_idx`/`wait_mode`/`update_idx`/`update_mode` — ENUMs 13/14) and the per-N immediate-source vector; structural, no scalar ordinal.
- **`klr::CollectiveOp` printer** (`@0xf95120`): the `op=` field reuses ENUM 12; the struct carries `dsts`/`srcs`/`replicaGroup`/`channel_id` fields (printed, not enum-ordinals).

### The `EngineType` ordinal — the key every engine field binds to

Not a wire field of these instruction bundles, but the discriminant every per-engine map keys on, and the one ordinal a reimplementer needs alongside the opcode. Byte-pinned from `bir::EngineType2string` (`libBIR @0x47fa80`) + `bir::EngineType2ExternalName` (`@0x47fca0`), cross-confirmed by `getEngineCount` (`@0x47d820`) and `getValidEngines` ([1.01 the arch object model](../arch/arch-object-model.md)). Tag: **CONFIRMED**.

| ord | internal | external | role |
|---|---|---|---|
| 0 | `Unassigned` | `Unassigned` | pseudo / ctor default |
| 1 | `Pool` | `GPSIMD` | pooling / reduce leg |
| 2 | `Activation` | `Scalar` | scalar / activation (PWP LUT) |
| 3 | `PE` | `Tensor` | systolic matmul array |
| 4 | `DMA` | `SyncDMA` | DMA engine |
| 5 | `DVE` | `Vector` | data-movement / vector |
| 6 | `SP` | `Sync` | sync / control sequencer |
| 7 | `ALL` | `All` | pseudo / all-engine barrier |

The datapath mask `bir::isDataPathEngine = 0x6E = {1,2,3,5,6}` excludes `0`/`4`/`7`. The `Pool→GPSIMD` external alias is why GPSIMD is not a separate engine ([1.13](../arch/gpsimd-engine.md)).

---

## Adversarial self-verification (this pass)

Five strongest claims, each re-derived from the binary-evidence chain and cross-checked against the sibling encoding page that consumes it:

1. **Dtype wire-tag LUT.** `byte_1DF5760 = 03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c` matches the in-dtype LUT cited byte-for-byte in [2.10](pe-matmul-encoding.md) (`b[0x20] = byte_1DF5760[dtype]`, fp32r→`0x0B`, fp8×4→`0x0E`/`0x0F`). The sole core_v4 divergence at index 2 (`0x10` vs `0x05`) is corroborated by [2.10]'s note that CoreV3 *rejects* the ×4-packed dtypes. **PASS.**
2. **AluOpType ordinals.** `19→0x18, 20→0x13, 21→0x14, 22→0x16, 23→0x15, 28→0x1D, 29→0x19` and the v4 extensions `30→0x20, 31→0x21, 32→0xC8` match the jump-table arms quoted verbatim in [2.14](tensorscalar-encoding.md) §`sub_12039C0`, including the explicit CORRECTION that CoreV2/V4 share one band. **PASS.**
3. **comp_op set.** `unk_1DF5780 = 01 02 03 04 05 06 09 0a 0b 0c 0d 0e` matches [2.20](sp-sync-encoding.md) (`byte_1DF5780`, IMM `1..6` / REG `9..14`, gap at wire 7/8). **PASS.**
4. **CollectiveKind LUT.** `unk_1DF5790 = 01 02 03 04 09 05 06 07 08`, `AllToAllV=0x09` out of sequence — matches [2.22](collective-customop-encoding.md) byte-for-byte (objdump-verified there, `kind∈{2..10}`). **PASS.**
5. **EngineType map.** `PE=3, Pool=1, Activation=2, DVE=5, SP=6, DMA=4`, `Unassigned=0`, `ALL=7` — matches [1.01](../arch/arch-object-model.md)'s three-way cross-confirmed table (`EngineType2string`, `getEngineCount`, `getValidEngines`). **PASS.**

No claim required revision against the binary; every L1≠L3 mismatch is flagged ✗ in place. Unpinnable items are tagged INFERRED (`_DGE_OPCODE` has no enum; `_IMM_SRC` names are validator-only).

## Cross-References

- **The encoders that consume these ordinals** — [PE matmul](pe-matmul-encoding.md) (2.10) through [collective / GPSIMD / CustomOp](collective-customop-encoding.md) (2.22): every Part-2 encoding page pins fields to offsets and defers the field *values* here. Notably [TensorScalar](tensorscalar-encoding.md) (2.14, AluOp), [Pool / TensorReduce](pool-reduce-encoding.md) (2.12, PoolFunc / reduce-cmd), [RNG](rng-encoding.md) (2.18, RandomAlgorithm), [SP sync / branch](sp-sync-encoding.md) (2.20, comp_op / wait / update), [collective](collective-customop-encoding.md) (2.22, CollectiveKind).
- **The struct capstone** — [NEURON_ISA_TPB Struct-Family Capstone](neuron-isa-tpb-capstone.md) (2.9): the byte-layout `.h` these enums are fields of.
- **The hardware key** — [The Arch Object Model](../arch/arch-object-model.md) (1.01): the confirmed `EngineType` map; [SP engine](../arch/sp-engine.md) (1.12) and [Pool engine](../arch/pool-engine.md) (1.09) for the AluOp / PoolFunc datapath semantics; [execution & sync model](../arch/execution-sync-model.md) (1.14) for the runtime semaphore record.
- **The L1 / L2 forms** — [Part 7 BIR](../bir/): the libBIR in-memory ordinals and the BIR-JSON spellings these L3 bytes derive from.
- **The master catalog** — [Part 14 appendix](../appendix/): the consolidated cross-component enum table.
