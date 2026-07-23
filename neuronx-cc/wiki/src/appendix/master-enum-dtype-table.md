# Appendix — Master Dtype / Enum Reference

> *All ordinals, wire tags, and addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310). Enum ordinals and the L1↔L3 arithmetic are ABI-stable across cp310/cp311/cp312; the byte addresses are cp310-pinned (cp311/cp312 VAs drift).*

## Abstract

This is the single consolidated lookup for every BIR/ISA enum in the Neuron
compiler, with its value→name mapping and — where it differs — the three distinct
representations a value can carry. A BIR enum is not one number: it is up to three —
the **three-layer model**:

- **L1** — the `libBIR` C++ enum ordinal stored in the `bir::Inst*` object in memory; the integer the `<Enum>2string` switch indexes.
- **L2** — the BIR-JSON wire **name**; every enum except `MemoryType` serializes as its `2string` spelling (string-keyed), so `L2 == L1 spelling`, 1:1.
- **L3** — the `libwalrus` silicon **byte** stamped into the 64-byte TPB instruction bundle by the `CoreV{2,3,4}` `visitInst<Op>` encoder. Exists only for the few enums that map onto a silicon opcode/field.

The trap this table exists to prevent: for several
families **L1 ≠ L3** — `Dtype` is a full remap, `AluOpType`'s comparison family is
reordered, `DMAQoSClass` is `class−1`, `InstructionType`→opcode is an arbitrary ISA
word, and `Collective`/`BranchCompareOp` carry reorder jumps in their wire LUTs.
Reading an L1 ordinal where an L3 byte is expected (or vice-versa) is wrong; the **L2
name is the only representation stable across all three layers**. Every L1≠L3 cell here
is flagged `✗`.

Every enum here is derived from the binary: `libBIR` `<Enum>2string` switch bodies give
the L1 names and ordinals, and `libwalrus` wire LUTs / convert encoders / name-decode
switches give the L3 bytes. The raw LUT bytes for the five most error-prone enums
(`Dtype` wire-tags, `AluOpType`, `Collective`, `BranchCompareOp`, wait-mode) are listed
in [Evidence anchors and limits](#evidence-anchors-and-limits). Both `libBIR.so` and
`libwalrus.so` are present in the corpus (decompile sidecars + `ida/` DBs); all enum
bodies and LUTs are byte-citeable.

| | |
|---|---|
| **L1 source** | `libBIR.so` `<Enum>2string` switches (md5 `12bb979f…`) |
| **L2 form** | BIR-JSON name (`toJson`→`<Enum>2string`); `MemoryType` is the one numeric exception |
| **L3 source** | `libwalrus.so` `CoreV*` `visitInst<Op>` + convert encoders + `.rodata` LUTs (BuildID `92b4d331…`) |
| **VA==fileoff** | `.text`/`.rodata` of both ELFs (`.data` has a +0x400000 delta — not used here) |
| **L1≠L3 families** | `InstructionType`, `AluOpType`, `Dtype`, `DMAQoSClass`, `Collective`, `BranchCompareOp`, `EngineAccumulationType`, wait-mode |

---

## How To Read These Tables

```text
L1     = libBIR ordinal (the in-memory enum int; byte-exact off 2string)
L2     = BIR-JSON wire name == the L1 2string spelling (shown implicitly as the row label)
L3     = libwalrus silicon byte/word, or:
           —          enum never reaches L3 (no silicon byte)
           codepath   enum selects an emitter branch / birsim mode (no standalone byte)
           ERR        value falls to the encoder's default (no wire form)
Δ      = ✗  L1 ≠ L3 (DANGER: do not conflate)
         =  L1 == L3 (identity-encoded)
         ·  no L3 form
         name  L2 is a name (no numeric wire form)
         num   L2 is a number (MemoryType only)
Conf   = CERTAIN (byte read straight off a switch/LUT) | HIGH (byte certain, name joined
         1:1 from BIR 2string) | MEDIUM (reconstructed, not disassembled case-by-case)
```

> **GOTCHA —** the L2 name is the join key, not the integer. `InstructionType::QuantizeMx`
> is L1 `96`, L2 `"QuantizeMx"`, L3 opcode byte `0xE3` (227). `96 ≠ 0xE3`. Any consumer
> that hard-codes `96` as an opcode, or `0xE3` as a BIR class id, is broken. The only
> stable cross-layer key is the string.

---

## Dtype  (20 members, L1 0..19)

`bir::Dtype` — `2string @0x2641e0` (libBIR), wire-tag `byte_1DFBAD0` (libwalrus core_v4,
read by `sub_14347C0`). **Full remap** ordered by container-size class — practically no
Dtype ordinal equals its wire tag. The enum is exactly 20 members and closed: the
`2string` default is a `NeuronAssertion` at `Dtype.cpp:39`, so no case ≥20 exists.

| L2 name | L1 | Wire-tag (core_v4 / pre-v4) | Stride | Align | x4 | Δ | Conf |
|---|---|---|---|---|---|---|---|
| `uint8` | 0 | 0x03 (3) | 1 | 1 | — | ✗ | CERTAIN |
| `int8` | 1 | 0x02 (2) | 1 | 1 | — | ✗ | CERTAIN |
| `float4_e2m1fn_x4` | 2 | **0x10 (16) / 0x05 (5)** | 2 | (CoreV4-only) | 4 | ✗ | CERTAIN |
| `float8e3` | 3 | 0x0d (13) | 1 | 1 | — | ✗ | CERTAIN |
| `float8e4` | 4 | 0x0e (14) | 1 | 1 | — | ✗ | CERTAIN |
| `float8_e4m3fn` | 5 | 0x0e (14) | 1 | 1 | — | ✗ | CERTAIN |
| `float8_e8m0fnu` | 6 | 0x03 (3) | 1 | 1 | — | ✗ | CERTAIN |
| `float8e5` | 7 | 0x0f (15) | 1 | 1 | — | ✗ | CERTAIN |
| `float8_e4m3fn_x4` | 8 | 0x0e (14) | 4 | 1 | 4 | ✗ | CERTAIN |
| `float8_e5m2_x4` | 9 | 0x0f (15) | 4 | 1 | 4 | ✗ | CERTAIN |
| `uint16` | 10 | 0x05 (5) | 2 | 2 | — | ✗ | CERTAIN |
| `int16` | 11 | 0x04 (4) | 2 | 2 | — | ✗ | CERTAIN |
| `bfloat16` | 12 | 0x06 (6) | 2 | 2 | — | ✗ | CERTAIN |
| `float16` | 13 | 0x07 (7) | 2 | 2 | — | ✗ | CERTAIN |
| `uint32` | 14 | 0x09 (9) | 4 | 4 | — | ✗ | CERTAIN |
| `int32` | 15 | 0x08 (8) | 4 | 4 | — | ✗ | CERTAIN |
| `float32` | 16 | 0x0a (10) | 4 | 4 | — | ✗ | CERTAIN |
| `float32r` | 17 | 0x0b (11) | 4 | 4 | — | ✗ | CERTAIN |
| `uint64` | 18 | 0x01 (1) | 8 | 8 | — | ✗ | CERTAIN |
| `int64` | 19 | 0x0c (12) | 8 | 8 | — | ✗ | CERTAIN |

**Dtype reading notes:**

- **Stride** = `qword_1DFC040[L1]` (libwalrus) — the **container** byte size, not the logical element size. `float4_e2m1fn_x4` (idx 2) stride = 2 (two-byte container holding 4 FP4 nibbles); `float8_*_x4` (idx 8/9) stride = 4 (u32 container holding 4×FP8). For standard widths stride == byte-width and == alignment.
- **Align** = byte-address alignment from `core_v3::addr_aligned_dtype @0x136ed10`, keyed on the **wire-tag** (not the BIR ordinal): tags `{2,3,13,14,15}`→none (1), `{4,5,6,7}`→`&1` (2), `{8,9,10,11}`→`&3` (4), `{1,12}`→`&7` (8).
- **x4** = lane-container expand factor 4 for the MX packed types `{2,8,9}`; the on-wire container holds 4 logical FP4/FP8 elements and `num_elem_per_partition` is ×4 on the MX matmul path.
- **Wire-tag sharing** (NOT bugs): `float8e4`(4) and `float8_e4m3fn`(5)/`float8_e4m3fn_x4`(8) all share tag 14 (all e4m3); `float8e5`(7) and `float8_e5m2_x4`(9) share tag 15. The x4-ness rides on the access-pattern (×4 num + stride), not a distinct tag. `float8_e8m0fnu`(6) is the OCP-MXFP shared-exponent scale (8-bit, uint8 class, tag 3). `float32r`(17) is a first-class type (own tag 11, own gate `NEURON_ISA_TPB_DTYPE_ALLOW_FP32R`) sized/aligned identically to `float32`.

> **QUIRK —** `float4_e2m1fn_x4` (idx 2) is the **only** entry that differs between the two
> wire LUTs: `byte_1DFBAD0` (core_v4) gives it tag **16**, but the pre-v4 LUT
> `byte_1DF5760` (consumer `sub_120E650`) carries it as tag **5** (the uint16 class).
> So FP4-packed gets its own wire tag only on CoreV4; on the older path it is smuggled
> through the uint16 slot. `core_v3::addr_aligned_dtype` does not even handle tag 16 (no
> defined alignment) — FP4-x4 is a CoreV4-only type; the core_v4 copy `@0x1446420` widens
> the always-aligned set to include tag 16 (align 1).

> **NOTE (Dtype container-tag caveat) —** the QuantizeMx `ADDR4 byte[34/35]` path reads a
> **different** 20-entry LUT that tags the packed datum as a `u32` container (tag 9). That
> is the *container* tag, not the *element* wire-tag from `byte_1DFBAD0` (where idx 8/9 →
> 14/15). Both are true and not in conflict — see [§9.1 dtype-catalog](../numerics/dtype-catalog.md).

> **GOTCHA —** FP4 alignment is often quoted as a flat "align 1", but that holds only on
> core_v4 (`@0x1446420`); `core_v3::addr_aligned_dtype @0x136ed10` returns never-aligned for
> wire-tag 16, so FP4-x4 has no defined core_v3 alignment. Hence the Align column marks
> idx 2 "CoreV4-only" rather than "1".

---

## AluOpType  (33 members, L1 0..32)

`bir::AluOpType` — `2string @0x400600` (libBIR), L3 via `CoreV4` convert `sub_142E030`
(jpt base `0x1DFB5E8`) and the `CoreV2/3` COLLECT-mode converter `sub_12039C0` (jpt base
`0x1DF4BB8`). Identity for 0..18, then the **comparison family reorders** and an int32
extended band pushes `mod_int` to `0xC8`. L2 keys:
`op`/`op0`/`op1`/`reduce_op`/`compare_op0`/`compare_op1` (15 consuming instructions).

| L2 name | L1 | L3 ALU_OP byte | Δ | Conf |
|---|---|---|---|---|
| `bypass` | 0 | 0x00 (0) | = | CERTAIN |
| `bitwise_not` | 1 | 0x01 (1) | = | CERTAIN |
| `arith_shift_left` | 2 | 0x02 (2) | = | CERTAIN |
| `arith_shift_right` | 3 | 0x03 (3) | = | CERTAIN |
| `add` | 4 | 0x04 (4) | = | CERTAIN |
| `subtract` | 5 | 0x05 (5) | = | CERTAIN |
| `mult` | 6 | 0x06 (6) | = | CERTAIN |
| `divide` | 7 | 0x07 (7) | = | CERTAIN |
| `max` | 8 | 0x08 (8) | = | CERTAIN |
| `min` | 9 | 0x09 (9) | = | CERTAIN |
| `bitwise_and` | 10 | 0x0a (10) | = | CERTAIN |
| `bitwise_or` | 11 | 0x0b (11) | = | CERTAIN |
| `bitwise_xor` | 12 | 0x0c (12) | = | CERTAIN |
| `logical_and` | 13 | 0x0d (13) | = | CERTAIN |
| `logical_or` | 14 | 0x0e (14) | = | CERTAIN |
| `logical_xor` | 15 | 0x0f (15) | = | CERTAIN |
| `logical_shift_left` | 16 | 0x10 (16) | = | CERTAIN |
| `logical_shift_right` | 17 | 0x11 (17) | = | CERTAIN |
| `is_equal` | 18 | 0x12 (18) | = | CERTAIN |
| `not_equal` | 19 | **0x18 (24)** ◄REORDER | ✗ | CERTAIN |
| `is_gt` | 20 | **0x13 (19)** ◄REORDER | ✗ | CERTAIN |
| `is_ge` | 21 | **0x14 (20)** ◄REORDER | ✗ | CERTAIN |
| `is_lt` | 22 | **0x16 (22)** ◄REORDER | ✗ | CERTAIN |
| `is_le` | 23 | **0x15 (21)** ◄REORDER | ✗ | CERTAIN |
| `average` | 24 | ERR (no byte) | ✗ | CERTAIN |
| `elemwise_mul` | 25 | ERR (no byte) | ✗ | CERTAIN |
| `pow` | 26 | 0x1a (26) | = | CERTAIN |
| `mod` | 27 | 0x1b (27) | = | CERTAIN |
| `rsqrt` | 28 | **0x1d (29)** ◄REORDER | ✗ | CERTAIN |
| `abs` | 29 | **0x19 (25)** ◄REORDER | ✗ | CERTAIN |
| `abs_max` | 30 | 0x20 (32) | ✗ | CERTAIN |
| `abs_min` | 31 | 0x21 (33) | ✗ | CERTAIN |
| `mod_int` | 32 | **0xC8 (200)** ◄int32 ext band | ✗ | CERTAIN |

> **QUIRK —** the comparison family (`not_equal`/`is_gt`/`is_ge`/`is_lt`/`is_le`, L1 19..23)
> is **deliberately reordered** on the wire because the predicates are anti-symmetric
> (operand swap flips gt↔lt, ge↔le). The wire band groups them so codegen can pick the
> swapped form without re-encoding. Order-sensitive ops (`subtract`/`divide`/shifts/`pow`/
> `mod`/`mod_int` + the four gt/ge/lt/le) **must not** be operand-canon-swapped.
> `mod`(27)→`0x1b` is *not* a reorder (0x1b == 27); only the comparison + abs/rsqrt + the
> int32 band move. `average`(24)/`elemwise_mul`(25) are dedicated CoreV4 ops with no ALU
> byte (fall to default → ERR). `mod_int`(32) emits literal `0FFFFFFC8h` masked to one byte
> = `0xC8` (the int32-both-operand band 0xC4..0xE1; full band membership beyond `mod_int`
> is **[INFERRED]**, not exhaustively disassembled). `abs_max`/`abs_min`
> (30/31) are core_v4-only (the COLLECT encoder tops out at case 29).

The `_CCE_OP` field (the collective-copy compute op, `InstDMACopy::getCceOp()`) is **not a
distinct enum** — it is an `AluOpType` field encoded by the same converter; only the
subset `{bypass=0x00 (plain copy), add=0x04 (reduce-on-copy)}` is legal.
Likewise `_DGE_COMPUTE_OP` is an `AluOpType` reduced to `{none, add}`: `bypass`→`none`,
`add`→`add` via `backend::AluOpType2DGEComputeOp @0x120b9e0`.

---

## Dtype / AluOp Layer Summary

The L1≠L3 cells above are the "do-not-confuse" core. The complete set of families where
the silicon byte is not the BIR integer:

| Family | L1→L3 rule | Source |
|---|---|---|
| `InstructionType` | arbitrary ISA opcode word (per-op `setupHeader`) | `CoreV*` `visitInst<Op>` |
| `AluOpType` | identity 0..18, comparison reorder, int32 band 0xC8 | `sub_142E030` jpt `0x1DFB5E8` |
| `Dtype` | full remap by container-size class | `byte_1DFBAD0` |
| `DMAQoSClass` | `class−1` for P-classes; 0 for Unassigned/Default | encode `@0x3128c0` |
| `Collective` | LUT `unk_1DF5790`, kind−2 index, idx-4 reorder | `sub_1203770` |
| `BranchCompareOp` | LUT `unk_1DF5780`, +2 jump at IMM→REG | `sub_1203580` |
| `EngineAccumulationType` | reorder `{Idle:0,Zero:1,ZeroAcc:3,AddAcc:2,LoadAcc:4}`; `Accumulate`→ERR | `sub_12038D0` |

> **NOTE —** `MemoryType` is not a mismatch but a different *kind* of wire form: its L2 is
> the **numeric** bit-flag (`MemoryLocation::toJson` writes the integer, not the name). It
> is the only enum whose L2 is a number — see [Bit-Flag & Engine Enums](#bit-flag--engine-enums).

---

## Instruction Opcode Ordinals  (`_OPCODE`, 161 live of 0..255)

`NEURON_ISA_TPB_OPCODE` — the low byte of the 16-bit `setupHeader` opcode word, pinned
from `core_v4::enum_variant_string_opcode @0x143fd80` (256-case `strncpy` switch, jpt
`0x1DFD684`; identical core_v3 `@0x1369a40`, core_v2 `@0x127aea0`). 161 live cases; all
other indices fall to a no-op default (RESERVED). Ordinal 0 = default no-op (no name),
255 = explicit `"Invalid"` sentinel. This is the L3 form of `InstructionType` — the BIR `InstructionType` L1 ordinal differs
from this opcode for almost every op; the full 110-row L1→opcode join lives in the
libwalrus opcode master table, so only the MX trio + override roster are L1-pinned here.

```text
PE / matmul band (0x01..0x0A)
  1  Ldweights        2  Matmul          3  PeRegWrite       4  WeightMask
  5  WeightShift      6  Ldtags          7  MatmulSparse     8  PeManageSeed
  9  LdweightsMX     10  MatmulMX
  --- 11..32 RESERVED ---
Activation band (0x21..0x30)
  33 Activate        34 ActivateQuantize 35 ActivationTableLoad
  36 ActivationReadAccumulator           37 Activate2        48 Exponential
  --- 38..47, 49..64 RESERVED ---
DVE / tensor band (0x41..0x5F)
  65 TensorTensorArithOp   66 TensorReduceArithOp   67 TensorScalarArithOp
  68 TensorScalarPtrArithOp 69 Pool   70 Copy   71 Cast   72 Reciprocal
  73 Memset  74 RegLoad  75 RegStore  76 RegShuffle  77 Rng
  78 TensorCumulativeArithOp  79 TensorScalarPtrMultiArith
  81 TensorTensorBitvecOp  82 TensorReduceBitvecOp  83 TensorScalarBitvecOp
  84 TensorScalarPtrBitvecOp  88 MaxPoolSelect
  94 TensorCumulativeBitvecOp  95 TensorScalarPtrMultiBitvec
BatchNorm / gather band (0x60..0x86)
  96 BatchNormStats   97 BatchNormStats2  98 BatchNormAggregate
  99 BatchNormGradAccumulate  100 BatchNormParamLoad  101 BatchNormBackProp
 102 LoadParameterRam 103 PoolBufferLoad 104 Gather  105 LoadMaskSelect
 106 StreamShuffle    107 StreamTranspose 108 Max8   109 MatchValueLoad
 110 FindIndex8       111 MatchReplace8   112 TensorScalarImmLdArith
 113 TensorScalarImmLdBitvec 114 CopyPredicated 115 RoiAlign
 116 TensorScalarAddr 118 Rand  119 RandGetState  120 RandSetState
 121 EmbeddingUpdate  122 LoadPoolArgument 123 TensorDequantize
 124 CrossLaneReduceArith 125 CrossLaneReduceBitvec 126 Iota  127 Dropout
 129 JpegDecode  130 TransposeBatchNormStats2  131 TransposeTensorReduceArithOp
 132 TransposeTensorReduceBitvecOp  133 CustomOpHeader  134 CustomOpPayload
Extended band (0x8E..0xA1)
 142 BatchNormParamLoad2  146 TensorScalarAffineSelect
 147 TransposeTensorScalarArithOp  148 BatchNormGradAccumulate2
 149 ModifyPoolConfig  150 Sort  152 TensorScalarSelect  153 CastPredicated
 154 TensorScalarCacheReduce  155 DveReadAccumulator  156 TensorReduceRangeCheck
 157 ScalarTensorTensorArith  158 ScalarTensorTensorBitvec  159 EngineNop
 160 EventSemaphore  161 Halt
Control / SP band (0xA2..0xB7)
 162 Drain  163 InstructionFlush  164 Nop  165 Write[=PeRegWrite]  166 Notify
 167 Move[=RegisterMove]  168 AluOp  169 CompareBranch  170 TensorLoad
 171 TensorStore  176 EventSemaphoreRangeClear  177 SetOrderingMode
 178 MoveShape  179 PollSem  180 TestEventSem  181 BranchPrefetchHint
 183 UcodeConfig
DMA band (0xB8..0xC3)
 184 DMAMemcpy  187 DmaIndirect  188 RangeSelect  189 DmaTranspose
 190 GetSequenceBounds  191 Sb2sbCollective  193 PseudoDMATrigger
Pseudo band (0xC2..0xEB)
 194 PseudoDMARearm  195 PseudoDMABarrier  196 PseudoDMAMemcpyFullInd
 197 PseudoSemaphoreSet  198 PseudoLoadActFuncSet  199 PseudoTriggerAllReduce
 200 PseudoTriggerCollective  201 PseudoReadVarAddr  202 PseudoEmbeddingUpdate
 203 PseudoSendRecv  204 PseudoBranchLabel  205 PseudoTensorStore
 206 PseudoTensorLoad  207 PseudoDMASwapQueueSet  208 PseudoSetRngSeed
 209 PseudoFunctionBegin  210 PseudoFunctionReturn  211 PseudoFunctionCall
 212 PseudoDmaDirect2d  213 PseudoSyncBarrier  214 PseudoRangeCheck
 215 PseudoJpegDecode  216 PseudoCoreBarrier  217 PseudoTriggerCollective2
 218 PseudoExtension  219 PseudoCurProcessingRankID  220 PseudoGidLoad
 221 PseudoBranchPrefetchHint  222 PseudoTensorCompletion  223 PseudoInst
 224 SparsityCompress  225 SparsityCompressTag  226 Rand2  227 QuantizeMX
 228 ConvLutLoad  229 TensorTensorScanArith  230 TensorScalarCacheCumulative
 231 IndirectCopy  232 CopyPredicatedScalar  233 DveReadIndices
 234 SelectReduce  235 Hint
High band (0xF0..0xFF)
 240 ExtendedInst  241 DmaGatherTranspose  242 NonzeroWithCount  255 Invalid
```

> **NOTE —** the MX trio `LdweightsMX=9 (0x09)`, `MatmulMX=10 (0x0A)`, `QuantizeMX=227
> (0xE3)` are the low bytes of the `0x10__` MX-class `setupHeader` words `0x1009 / 0x100A /
> 0x10E3`. The MX class prefix `0x10` is the high byte; the
> `_OPCODE` ordinal is the low byte. Ordinals 165 `"Write"` / 167 `"Move"` are
> suffix-truncated labels (`PeRegWrite`/`RegisterMove` reused via +offset); the binary
> literally `strncpy`s the short string. The BIR `Activation` op (IT4) emits opcode `0x21`
> (33, `Activate`) on CoreV2/V3 but `0x25` (37, `Activate2`) on CoreV4 (`mov BYTE
> [rbp-0xf0],0x25 @0x143c25f`) — ordinals 33 and 37 are the **same public op at different
> arch levels**, not two distinct ops.

---

## Collective Kind  (11 members, L1 0..10)

`bir::CollectiveKind` — `2string @0x4016c0` (libBIR), wire via `CoreV2/3` convert
`sub_1203770` (`sub edi,2; cmp edi,8; movzx eax, byte[unk_1DF5790 + (kind-2)]`). The
9-byte LUT covers only kinds 2..10; kinds 0/1 (`SendRecv`/`SendRecvCCE`) fall to the
default (ERR) and use the dedicated `PseudoSendRecv`(203)/`Sb2sbCollective`(191) opcodes
instead. L2 key `cc_channel.kind`.

| L2 name | L1 kind | LUT idx (kind−2) | wire byte | Δ | Conf |
|---|---|---|---|---|---|
| `SendRecv` | 0 | — | ERR (dedicated opcode 203) | · | CERTAIN |
| `SendRecvCCE` | 1 | — | ERR | · | CERTAIN |
| `AllReduce` | 2 | 0 | 0x01 (1) | ✗ | CERTAIN |
| `ReduceScatter` | 3 | 1 | 0x02 (2) | ✗ | CERTAIN |
| `AllGather` | 4 | 2 | 0x03 (3) | ✗ | CERTAIN |
| `AllToAll` | 5 | 3 | 0x04 (4) | ✗ | CERTAIN |
| `AllToAllV` | 6 | 4 | **0x09 (9)** ◄REORDER | ✗ | CERTAIN |
| `Permute` | 7 | 5 | 0x05 (5) | ✗ | CERTAIN |
| `PermuteReduce` | 8 | 6 | 0x06 (6) | ✗ | CERTAIN |
| `PermuteImplicit` | 9 | 7 | 0x07 (7) | ✗ | CERTAIN |
| `PermuteReduceImplicit` | 10 | 8 | 0x08 (8) | ✗ | CERTAIN |

> **QUIRK —** `AllToAllV` (kind 6) maps to wire `0x09` **out of sequence** — the rest are a
> `−1` shift from kind, but the LUT (`01 02 03 04 09 05 06 07 08`) jumps to 0x09 at index 4
> so the `Permute` family stays contiguous at `0x05..0x08`. Companion enums:
> `CollectiveDimension` `{0:Partition,1:Free}` (`cc_dim`), `CollectiveComputeTypeHint`
> `{0:TP,1:FSDP,2:None}` (`cc_type_hint`, None suppressed on emit) — both name-only, no L3
> byte.

---

## Branch / Sync / Semaphore Ordinals

`bir::BranchCompareOp` — `2string` names + `klr::BrCmpOp` `to_string @0xf80db0`; wire via
`CoreV2/3` convert `sub_1203580` (`cmp edi,0Bh; movzx eax, byte[unk_1DF5780 + comp_op]`).
The 12-byte LUT is indexed by the BIR ordinal. The `klr` name space is shifted +1
(`klr` 0 = reserved; `klr` 1 = `Always`, which BIR lacks — `Always` is the absence of a
compare).

| BIR name | L1 | klr name | LUT byte | Δ | Conf |
|---|---|---|---|---|---|
| `IS_LTIMM` | 0 | `LtImm` | 0x01 (1) | ✗ | CERTAIN |
| `IS_LEIMM` | 1 | `LeImm` | 0x02 (2) | ✗ | CERTAIN |
| `IS_EQIMM` | 2 | `EqImm` | 0x03 (3) | ✗ | CERTAIN |
| `IS_NEIMM` | 3 | `NeImm` | 0x04 (4) | ✗ | CERTAIN |
| `IS_GEIMM` | 4 | `GeImm` | 0x05 (5) | ✗ | CERTAIN |
| `IS_GTIMM` | 5 | `GtImm` | 0x06 (6) | ✗ | CERTAIN |
| `IS_LTREG` | 6 | `LtReg` | **0x09 (9)** ◄REORDER | ✗ | CERTAIN |
| `IS_LEREG` | 7 | `LeReg` | 0x0a (10) | ✗ | CERTAIN |
| `IS_EQREG` | 8 | `EqReg` | 0x0b (11) | ✗ | CERTAIN |
| `IS_NEREG` | 9 | `NeReg` | 0x0c (12) | ✗ | CERTAIN |
| `IS_GEREG` | 10 | `GeReg` | 0x0d (13) | ✗ | CERTAIN |
| `IS_GTREG` | 11 | `GtReg` | 0x0e (14) | ✗ | CERTAIN |
| `Unsupported` | 12 | — | ERR (>11 → default) | · | CERTAIN |

The IMM band (0..5) → `0x01..0x06` (`ord+1`); the REG band (6..11) → `0x09..0x0E`
(`ord+3`, a +2 jump at the IMM→REG boundary). The semaphore enums:

| Enum | members | wire | Δ | Conf |
|---|---|---|---|---|
| `WaitMode` | 0:(reserved) 1:`SEM_GE_IMM` 2:`SEM_GE_REG` | LUT `unk_1DF577A` = `07 05 85` | ✗ | CERTAIN |
| `UpdateMode` | 0:replace/set 1:increment | identity raw write (no LUT) | = | CERTAIN |
| `BranchOutcomeHint` | 0:`LikelyTaken` 1:`LikelyNotTaken` | name (`hint`) | name | CERTAIN |

> **GOTCHA —** WaitMode is LUT-remapped (`07 05 85` — `SEM_GE_REG`'s `0x85` = `0x05 | 0x80`,
> the 0x80 bit selecting the register operand) but **UpdateMode is written identity (raw
> ordinal)** by the same `visitInstEventSemaphore @0x1217df0`. Do not apply the wait LUT to
> update_mode, or vice-versa. Only the two GE wait forms are wired; LT/GT/EQ waits are not
> emitted on this build.

---

## Engine, Arch & Accumulation Enums

Structural enums consumed by passes/verifier; L2 = the name, no L3 silicon byte unless
noted.

| Enum | `2string@` | members (L1:name) | L3 |
|---|---|---|---|
| `EngineType` | `0x47fa80` | 0:Unassigned 1:Pool 2:Activation 3:PE 4:DMA 5:DVE 6:SP 7:ALL | name |
| `ArchLevel` | `0x479490` | 10:inferentia 20:sunda 30:gen3 40:core_v4 50:core_v5 | name (= gen×10) |
| `ArchRevision` | `0x479860` | 0:v1 1:v2 | name |
| `EngineAccumulationType` | `0x400990` | 0:Idle 1:Zero 2:Accumulate 3:ZeroAccumulate 4:AddAccumulate 5:LoadAccumulate | codepath (PSUM RMW) |
| `klr::AccumCmd` | `to_string @0xf7f630` | 1:Idle 2:Zero 3:Accumulate 4:ZeroAccumulate 5:LoadAccumulate (0=reserved) | KLIR text-IR |

> **QUIRK (EngineAccumulationType wire reorder) —** the `CoreV2/3` wire encoder `sub_12038D0`
> reorders: L1 `{Idle:0, Zero:1, Accumulate:2, ZeroAccumulate:3, AddAccumulate:4,
> LoadAccumulate:5}` → wire `{0→0, 1→1, 2→ERR, 3→3, 4→2, 5→4}`. `Accumulate`(2) has **no
> wire byte** (default → codepath). The `klr::AccumCmd` text-IR ordinals (1..5) are a
> **separate space** and do **not** carry `AddAccumulate`. `EngineType2ExternalName
> @0x47fca0` provides a second L2 spelling for the PROFILE layer (Pool→GPSIMD,
> Activation→Scalar, PE→Tensor, DMA→SyncDMA, DVE→Vector, SP→Sync, ALL→All) — an alias, not
> an L3 byte.

---

## Axis, Indirect & Transpose Enums

| Enum | `2string@` | members | L3 |
|---|---|---|---|
| `AxisListType` | `0x4011b0` | 0:X 1:XY 2:XYZ 3:XYZW 4:XYZWC 5:C | name (reduce-count = val+1) |
| `klr::TensorSubDim` | `to_string @0xf7fe30` | 1:X 2:XY 3:XYZ 4:XYZW (0=reserved) | KLIR text (AxisListType +1) |
| `IndirectDim` | `0x4021a0` | 0:W 1:Z 2:Y 3:X | name (`indirect_dim`); identity wire |
| `klr::IndexMissBehavior` | `@0xf82fc0` | 1:Imm 2:Skip (0=reserved) | KLIR text |
| `TransposeOps` | `0x401500` | 0:None 1:WZXY … 12:XYZW 13:XYWZ (14 vals) | codepath (DMA-transpose) |
| `klr::TensorScalarReverseOps` | `@0xf7fd90` | 1:none 2:first 3:second 4:both (0=reserved) | KLIR text |

> **NOTE —** `IndirectDim` axis order is `W,Z,Y,X` (the **reverse** of the natural
> `X,Y,Z,W`); the dim ordinal is the BIR ordinal (no remap), emitted as a name via
> `convertIndirectDim @0x1204220`. `klr::TensorSubDim` shifts the cumulative-prefix names up
> by 1 (0 = reserved/UNABLE).

---

## RNG, DGE & Perf-Mode Enums

| Enum | `2string@` | members | L3 |
|---|---|---|---|
| `RandomAlgorithmKind` | `0x401de0` | 0:LFSR 1:PCG32 2:PHILOX_1 | name (`random_algorithm`); identity wire |
| `RandomDistributionKind` | `0x401ee0` | 0:Raw 1:Uniform 2:Normal 3:Binomial | name (`distribution`); identity wire |
| `klr::DropoutThresholdType` | `@0xf7f5b0` | 1:DropRate 2:KeepRate (0=reserved) | KLIR text |
| `DGEType` | `0x400dc0` | 0:None 1:SWDGE 2:HWDGE 3:Unassigned | codepath (`@InstDMA+0xF8`); identity |
| `klr::DgeComputeOp` | `to_string @0xf7f9e0` | 1:none 2:add (0=reserved) | from `AluOp` bypass/add |
| `MatmultPerfMode` | `0x400be0` | 0:None 1:DoubleRow 2:DoubleColumn 3:DoublePixel 4:DoubleRowSwInterleave | codepath / wire identity 0..3 |
| `klr::MatmulPerfMode` | `to_string @0xf7f030` | 1:None 2:DoubleRow 3:DoubleRowSwInterleave (0=reserved) | KLIR text |

> **GOTCHA —** `RandomAlgorithmKind` starts at `LFSR=0`, not `PCG32=0`: the order is
> `LFSR=0, PCG32=1, PHILOX_1=2` in both `2string @0x401de0` and `string2 @0x40ef50`.

> **GOTCHA (MatmultPerfMode three-way split) —** the **BIR** `MatmultPerfMode` (5 members,
> `0:None..4:DoubleRowSwInterleave`), the **KLIR** `klr::MatmulPerfMode` (3 members), and the
> **wire** byte are three *different* ordinal spaces. The `CoreV2/3` wire encoder
> `sub_1203630` is identity for BIR `0..3` (so `None/DoubleRow/DoubleColumn/DoublePixel`
> ride identically) but BIR `DoubleRowSwInterleave`(4) **ERRs** (not wire-encodable on this
> path). The KLIR text-IR names `{1:None, 2:DoubleRow, 3:DoubleRowSwInterleave}` are smaller
> and renumbered. The binary is authoritative for each layer — do not assume the names line
> up across them.

---

## ActivationFunctionType  (31 members, L1 0..30)

`bir::ActivationFunctionType` — `2string @0x4002a0` (libBIR). L2 key `func`; emitted as
a name. The activation func reaches silicon only indirectly: the chosen
func-id is baked into the `InstActivation`(IT4) opcode + the PWP table, and the **PWP
`neuron_id` is a SEPARATE id space** from this ordinal.

```text
 0 Identity      1 Sigmoid       2 Exp          3 Tanh
 4 Rsqrt         5 Sqrt          6 Reciprocal   7 Gelu
 8 Relu          9 Softplus     10 Mish        11 Square
12 Abs          13 Sign         14 Erf         15 Sin
16 Bitwise_not  17 Ln           18 Sin_h       19 Cos
20 Geluapprox   21 Selu         22 Elu         23 Softsign
24 Floor        25 Ceil         26 Gelu_apprx_tanh  27 Silu
28 Derivative_silu  29 Is_finite  30 Unknown
```

> **NOTE —** the member roster above is the L1 ordinal list (31 values, 0..30). The 31st
> slot `Unknown`(30) is the sentinel. `PoolFunctionType` (`@0x4010e0`) is the companion
> 2-member enum `{0:Max, 1:Avg}` (`func` key). See
> [§7.x activation catalog](../activation/act-function-catalog.md) for the func→PWP id-space
> join (the part that is **not** this ordinal).

---

## Bit-Flag & Engine Enums

`MemoryType` — `2string @0x3ca040` (libBIR). The **one** enum whose L2 is **numeric**:
`MemoryLocation::toJson` writes the bit-flag integer to the `type` key, not the name
No L3 silicon byte (it gates allocation/addressing).

| name | L1 = L2 numeric | bit | Δ | Conf |
|---|---|---|---|---|
| `Unallocated` | 1 | 0 | num | CERTAIN |
| `Input` | 2 | 1 | num | CERTAIN |
| `Output` | 4 | 2 | num | CERTAIN |
| `DRAM` | 8 | 3 | num | CERTAIN |
| `SB` | 16 | 4 | num | CERTAIN |
| `PSUM` | 32 | 5 | num | CERTAIN |
| `RNGSTATE` | 64 | 6 | num | CERTAIN |
| `REG` | 128 | 7 | num | CERTAIN |

`DMAQoSClass` — `2string @0x400ed0` (libBIR), encode `@0x3128c0` → descriptor `byte[12]
bits[0:2]` (only when `supportsDMAQoSOnISA`, arch>29). L3 = `class−1` for P-classes; 0 for
Unassigned/Default.

| name | L1 | L3 byte | Δ |
|---|---|---|---|
| `Unassigned` | 0 | 0 | = |
| `Default` | 1 | 0 | ✗ |
| `P0`…`P14` | 2…16 | `L1−1` (1…15) | ✗ |

> **GOTCHA —** `Default`(L1 1) encodes to byte 0, the same as `Unassigned`(0) — so the wire
> byte cannot distinguish them. `P7..P14` (wire 8..15) exceed the 3-bit descriptor field
> `byte[12] bits[0:2]`; how core_v5's runtime-raw path carries them is **[UNRESOLVED]**.

---

## Field-Typedef "Enums" (no value→name set)

These appear in the `NEURON_ISA` family but have no enumerated ordinal roster — they are
bitfields or packed windows:

| name | location | form | Conf |
|---|---|---|---|
| `_MATMUL_PSUM_ACCUMULATE_FLAGS` | bundle `+0x2B` | 3-bit flag: START 0x01 / STOP 0x02 / ACCUMULATE 0x04; valid set `{0,1,2,3,4,6}` (not 5/7) | CERTAIN |
| `_MATMUL_ZERO_REGION` | bundle `+0x2F` (MX) | packed PSUM bank-range window (from `inst+0x118`); no ordinals | CERTAIN |
| `_PE_FP32MODE` | bundle `+0x20`/`+0x21` | `{0: standard fp32, 2: fp32r 2-FMA round}`; fp32r signature `*(u16)(bundle+0x20)=0x020A` | HIGH |
| `_IMM_SRC` | validator only | 3 ordinals `{0:immediate, 1:register, 2:pointer}`; names not name-decoded (GAP) | CERTAIN (cardinality) / MEDIUM (names) |

---

## Evidence anchors and limits

The five highest-risk enums — the ones with layer reorders or wire-tag subtleties — are
grounded on these raw `libwalrus.so` LUT bytes:

```text
dtype wire-tag  byte_1DFBAD0 @0x1DFBAD0 (core_v4, 20B):
  03 02 10 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c   ✓ exact
dtype wire-tag  byte_1DF5760 @0x1DF5760 (pre-v4, 20B):
  03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c   ✓ idx2=05 (vs core_v4 10)
dtype stride    qword_1DFC040 @0x1DFC040 (20×u64):
  1 1 2 1 1 1 1 1 4 4 2 2 2 2 4 4 4 4 8 8                       ✓ (fp4_x4=2, fp8_x4=4 container)
collective LUT  unk_1DF5790 @0x1DF5790 (9B):
  01 02 03 04 09 05 06 07 08                                    ✓ idx4=09 (AllToAllV reorder)
branch-cmp LUT  unk_1DF5780 @0x1DF5780 (12B):
  01 02 03 04 05 06 09 0a 0b 0c 0d 0e                           ✓ +2 jump at IMM→REG boundary
wait-mode LUT   unk_1DF577A @0x1DF577A (3B):
  07 05 85                                                       ✓ SEM_GE_IMM/REG (0x85=0x05|0x80)
```

The `AluOpType` comparison reorder is corroborated by the per-case `mov eax,IMM` sites
(e.g. `case19 not_equal→0x18 @0x142e110`), and the `float32r`, `AllToAllV` and
`QuantizeMX` mnemonics are present as standalone `.rodata` strings in `libwalrus.so`.

> **NOTE —** the pybind `isa_tpb` python `__init__` module is a thin stub (560-byte enum
> dump = `PySendResult`/`PyGILState_STATE` only); the `enum_mapping` module exposes
> `map_dtype` + dtype name strings (`float32r`, `bfloat16`/`BFLOAT16`, `is_gt`) but not the
> raw wire-tag LUTs. The authoritative wire tables are the `libBIR`/`libwalrus` `.rodata`
> LUTs verified above — the pybind layer is a name surface, not the byte source.

---

## Gaps

- **G1** — `DMAQueueAttribute` (`2string @0x25b2f0`) has no static members (FATAL stub); int↔name unrecoverable from this build.
- **G2** — `NEURON_ISA_TPB_DTYPE` member **names** (e.g. `DTYPE_FP8_E4M3 = 14`) are not recovered as a symbol table; the wire-tag integers are certain but their symbolic names are **[INFERRED]** from grouping.
- **G3** — the full 110-row `InstructionType`→`_OPCODE` join: only the MX trio + override roster (`Activation`/`Exponential`/`Rand2`) are L1-pinned here; the rest of the per-op `setupHeader` words live in the libwalrus opcode master table.
- **G4** — the `AluOpType` int32 extended band (`0xC4..0xE1`) membership beyond `mod_int`(0xC8) is **[INFERRED]**; the reordered comparison + abs/rsqrt + mod_int cases are read directly.
- **G5** — `_DGE_OPCODE` has no standalone enum in `libwalrus.so`; the DGE descriptor opcodes are a subset of `_OPCODE` (the DMA family) selected by `DGEType`. `_IMM_SRC` member names are not name-decoded.
- **G6** — no NEFF/BIR-JSON round-trip fixture byte-diff was performed; the L2 name↔L1 int binding comes from the (de)serializer bodies and the L3 byte from emitter constants, so the L2→L3 join is emitter-certain rather than wire-observed.

---

## Cross-References

- [The BIR Dtype Enum and its Wire-Tag / Stride / Alignment LUTs](../bir/dtype-tables.md) — §7.6, full dtype LUT derivation
- [The KLR→BIR Codegen Enum-Translation Crosswalk](../bir/codegen-enum-crosswalk.md) — §7.7, the AluOpType L1→L3 reorder narrative
- [Op-Family Enums and the L1/L2/L3 Crosswalk](../bir/op-family-enums.md) — §7.8, InstructionType / Collective / Activation families
- [Structural Enums — Engine, Arch, Accumulation, Axis, Indirect, Transpose](../bir/structural-enums.md) — §7.9, the structural/metadata enums
- [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) — §2.23, the bit-pinned L3 silicon ordinals
- [Neuron Dtype Catalog and x4-Packing](../numerics/dtype-catalog.md) — §9.1, dtype semantics + the x4 container-tag caveat
- [Activation Function Catalog & Function-Sets](../activation/act-function-catalog.md) — the ActivationFunctionType → PWP neuron-id space
