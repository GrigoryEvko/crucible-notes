# LLO Opcode Master Table

> *Every opcode value, mnemonic, slot name, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID `89edbbe81c5b328a958fe628a9f2207d`, not stripped). Other versions differ.*

## Abstract

This appendix is the single consolidated reference table for the LLO opcode space — the one table a reimplementer keeps open while building a TPU back end. LLO (Low-Level Optimizer IR) is the TPU-specific late compiler IR below MHLO/TLP and above the per-generation TensorCore bundle encoders; its in-memory opcode enum `xla::jellyfish::LloOpcode` is a **dense, zero-based, 461-value** numbering (`0x000`..`0x1CC`), and every value is named in the relocated `opcode_name` table at `0x21ccfef0`. The authoritative per-family facts live on the isa/ deep pages; this page aggregates them into one master opcode→mnemonic→slot→semantics→per-gen table, grouped by the bundle slot (functional unit) each opcode is encoded into.

The table is organized by **slot family**, not by numeric run, because the value space interleaves: `LloOpcodeIsVector` is a dense switch, not a `>= threshold` test, and (for example) `kVectorReadIar` (1) is vector while `kLog` (5) is not. Each slot section below enumerates the opcodes that the per-slot encoder serializes into that slot, with the numeric `LloOpcode` value, a one-line semantics, and per-generation availability where it varies (the five codec lineages: **jxc** Jellyfish/Dragonfish v2/v3, **pxc** Pufferfish v4, **vfc/vxc** Viperfish v5p, **glc** Ghostlite v6e, **gfc** 6acc60406 TPU7x). The bit-level field encodings — where each opcode's operands land in the 41/51/64-byte bundle — are *not* repeated here; they live on the per-slot deep pages, which every section cross-links.

Three index spaces must not be conflated with `LloOpcode`, and a reimplementer who confuses them mis-decodes every program: the **`LloOpcodeProto`** wire enum (1-based, max 499, 38 reserved gaps, see [LloOpcode↔Proto](../isa/llo-opcode-to-proto.md)); the **MC `MCInst`** opcode space the LLVM-MC emitter dispatches on (`LloOpcode + 499`, gated `<= 0x1F2`); and the **`GhPerf::Instruction`** cost-grid enum. `LloOpcode` is the one all the others map *from*. A representative sample of the values in this table was re-verified against the binary classifiers and converters (see [Verification](#verification-provenance)); where a deep page and the binary agreed they were taken as CONFIRMED, and no disagreements were found.

| | |
|---|---|
| **Enum** | `xla::jellyfish::LloOpcode` — 461 enumerators, dense `0x000`..`0x1CC` (0..460) |
| **Name accessor** | `LloOpcodeName(LloOpcode)` @ `0x1d631280` — bound `>= 0x1CD` → `ud1` |
| **Name table** | `opcode_name` @ `0x21ccfef0` (461 × `char*`, `R_X86_64_RELATIVE`) |
| **Property word** | `opcode_info` @ `0x223a1320` (461 × `uint16`: Push/Pop/Remat/Fold/Cse + reg-file class) |
| **Descriptor** | `opcode_info_big` @ `0x227b5570` (461 × 28 B: ResultFifo + ArchRegister lists) |
| **Family classifier** | `LloOpcodeIsVector` @ `0x1d60c1c0`; `LloOpcodeIsScalar` = `!IsVector` @ `0x1d60c7e0` |
| **Wire converter** | `LloOpcodeToProto` @ `0x14420020` (table @ `0x344cb4c`); `ProtoToLloOpcode` @ `0x14420040` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

For reimplementation, the contract this table supports is:

- The **opcode numbering is gen-invariant** — the same `LloOpcode` value means the same opcode on every generation; what changes per gen is the *valid subset* (sparsity is v5+, cmem-load is PF-only) and the *bundle encoding*, not the number.
- The **slot a value occupies** (which bundle field the per-gen encoder serializes it into) is recovered from the per-slot classifier switches and encoder dispatch, listed per section.
- The **per-gen availability** column flags opcodes that exist only from a given generation (F8 converts PF+, stochastic-round VF+, S4/U4 matmul PF+, BarnaCore vs SparseCore split).
- The **bit encodings are deferred** to the per-slot deep pages; this table is value↔mnemonic↔slot↔semantics only.

---

## Slot Families at a Glance

Eleven functional-unit families partition the 461 opcodes (plus a 33-opcode BarnaCore/SparseCore block). The count column is the number of distinct `LloOpcode` values the family owns; the owning page is the deep page that documents that slot's bit encoding. Values are mostly-contiguous bands; the exact members are in the per-family sections below.

| Slot family | Value band(s) | Opcodes | Owning deep page | Confidence |
|---|---|---:|---|---|
| Sequencer / sync / FIFO transfer | `0x000`..`0x030` (interleaved) | ~50 | [Sequencer Slot](../isa/slot-sequencer.md) · [Seq-Ops-Per-Gen](../isa/sequencer-ops-per-gen.md) | CONFIRMED |
| SPU / scalar ALU & control | `0x085`..`0x089`, `0x16B`..`0x1AA` | 63 | [SPU / Scalar Slot](../isa/slot-spu-scalar.md) | CONFIRMED |
| VPU / vector ALU & logic | `0x048`..`0x05A`, `0x11B`..`0x1A2` | (subset of 295 vector) | [VPU Slot](../isa/slot-vpu.md) | CONFIRMED |
| Convert / pack / unpack | `0x05B`..`0x076`, `0x107`..`0x11A`, `0x125`..`0x127` | ~45 | [VPU Slot](../isa/slot-vpu.md) · [EUP](../isa/slot-eup-transcendental.md) | CONFIRMED |
| EUP transcendental | `0x128`..`0x14D` | 38 | [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) | CONFIRMED |
| Cross-lane / reduce / XLU result | `0x036`..`0x03B`, `0x0F5`..`0x101`, `0x14E`..`0x155` | ~28 | [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md) | CONFIRMED |
| MXU matmul / latch / matprep / matres | `0x08D`..`0x0AB`, `0x152`..`0x153` | ~33 | [MXU Slot](../isa/slot-mxu.md) · [Matprep/IAR/Latch](../isa/slot-matprep-iar-latch.md) | CONFIRMED |
| Memory load / store / IAR / RNG | `0x001`..`0x004`, `0x030`..`0x046`, `0x077`..`0x078` | ~30 | [Memory-Load](../isa/slot-memory-load.md) · [Memory-Store](../isa/slot-memory-store.md) · [CMEM-Load](../isa/slot-cmem-load-pf.md) | CONFIRMED |
| DMA | `0x0B3`..`0x0DA` (contiguous) | 40 | [Sequencer Slot](../isa/slot-sequencer.md) (DMA-issue) | CONFIRMED |
| Predicate / mask | `0x0E1`..`0x0F1`, `0x167`..`0x16A`, `0x193`..`0x199` | ~18 | [Predicate Slot](../isa/slot-predicate.md) · [vcreate_mask](../isa/slot-vcreate-mask-mregister.md) | CONFIRMED |
| Constants / pseudo / call | `0x02C`..`0x02E`, `0x0DB`..`0x0F4`, `0x17C` | ~25 | [Immediate Slot](../isa/slot-immediate.md) | CONFIRMED |
| BarnaCore (SparseCore) | `0x1AC`..`0x1CC` (contiguous) | 33 | [LLO Opcode Enum](../isa/llo-opcode-enum.md) | CONFIRMED |
| Sparsity (v5+) structured-sparse | — | (open) | [Sparsity Slot (v5+)](../isa/slot-sparsity-v5plus.md) | OPEN |

> **NOTE — the sparsity slot is not yet recovered.** The structured-sparsity slot ([Sparsity Slot (v5+)](../isa/slot-sparsity-v5plus.md)) is a v5+/Viperfish-and-later feature whose backing analysis is still open; no distinct `LloOpcode` values for it have been pinned. It is listed for completeness but contributes no rows to the tables below. A reimplementer targeting only v3/v4 can ignore it entirely.

> **QUIRK — 461, not 462.** The "462" figure that floats around LLO documentation is the `LloOpcodeProto` *wire* enum (1-based, value-0 sentinel, max 499, 38 gaps). The in-memory `LloOpcode` this table indexes is exactly **461 dense values** (`0x000`..`0x1CC`); `LloOpcodeName` traps on `>= 0x1CD`. Drive a switch off 462 and the last index reads past the metadata tables. See [LloOpcode↔Proto](../isa/llo-opcode-to-proto.md).

---

## Sequencer, Sync & FIFO-Transfer Slot (`0x000`..`0x030`)

The control-and-handshake layer: program boundaries, scheduling barriers, fences, cycle-counter reads, halt/yield, and the scalar/vector/sync-flag result-FIFO push/pop primitives that move values between the SPU, VPU, and the cross-FIFO staging registers. PC mutation (branch/call/halt) is lane-0-only; sync ops live in a separate `ScalarMisc` lane on v5+. The per-(gen × sequencer-type) control-flow roster is on [Seq-Ops-Per-Gen](../isa/sequencer-ops-per-gen.md).

| `LloOpcode` | Mnemonic | Slot / lane | Semantics | Per-gen | Confidence |
|---|---|---|---|---|---|
| `0x000` | `kEvent` | sequencer | program / trace event marker | all | CONFIRMED |
| `0x005` | `kLog` | sequencer | debug-log marker (non-vector) | all | CONFIRMED |
| `0x006`/`0x007` | `kHloStart` / `kHloEnd` | sequencer | source-HLO span markers (debug-info) | all | CONFIRMED |
| `0x008` | `kSchedulingBarrier` | sequencer | hard reorder barrier (vetoes CSE across it) | all | CONFIRMED |
| `0x009` | `kScalarToVector` | V↔S bridge | scalar→vector broadcast push | all | CONFIRMED |
| `0x00A` | `kVectorToScalarPush` | V→S FIFO | vector→scalar FIFO push (Push bit) | all (v6e GhPerf 0x1DA) | CONFIRMED |
| `0x00B` | `kSyncFlagToScalarPush` | sync→S FIFO | sync-flag→scalar FIFO push | all | CONFIRMED |
| `0x00A`..`0x012` | `kVectorToScalarPush` … `kDrfPop` | V↔S / sync FIFO | V→S / sync-flag→scalar push/pop quartet | all | CONFIRMED |
| `0x011`/`0x012` | `kSfrfPop` / `kDrfPop` | FIFO pop | sync-flag-result / divrem-result FIFO pop (Pop bit) | all | CONFIRMED |
| `0x013`/`0x014` | `kScalarFence` / `kVectorStoreFence` | sequencer / misc | ordering fences (store-fence → misc slot on gfc) | all | CONFIRMED |
| `0x015`..`0x01C` | `kScalarCcfPush` … `kVectorCcfPopAsymmetrical` | CCF FIFO | cross-core FIFO push/pop (symmetric + asymmetric) | all | CONFIRMED |
| `0x01D` | `kMegacoreSwapCoresPseudo` | sequencer | megacore core-swap pseudo | all | CONFIRMED |
| `0x01E` | `kCmemFence` | sequencer | CMEM ordering fence | **PF+** | CONFIRMED |
| `0x01F`..`0x024` | `kScalarReadCycleStart` … `kScalarReadCycleLow` | sequencer | cycle-counter reads / 64-bit lo/hi splits | all | CONFIRMED |
| `0x025`..`0x027` | `kScalarHalt` / `…YieldConditional` / `…OnError` | sequencer (lane 0) | sequencer halt variants (GhPerf row 0x000) | all | CONFIRMED |
| `0x029` | `kProgramLaunchSc` | sequencer | SparseCore program launch | all | CONFIRMED |
| `0x02B` | `kVectorInterrupt` | sequencer | vector-side interrupt | all | CONFIRMED |

> **NOTE — Push/Pop are property bits, not opcode ranges.** `opcode_info` bit0 (Push) is set on the `*Push` members, bit1 (Pop) on the `*Pop` members. A scheduler models the V2S / sync FIFOs by reading these bits, not by a numeric range test. `kScalarHalt`/`kScalarHaltOnError` share `GhPerf` cost row 0x000.

---

## SPU / Scalar-ALU Slot

The scalar control CPU lane — 32-entry SREG file, address arithmetic, branch/call/select, the dense arithmetic-and-shift tail, and the V↔S FIFO pops. The register-file-class byte (`opcode_info` high byte) is `1` (scalar/mask). Lane 0 owns the full set incl. branch/call; lane 1 is an ALU+halt mirror. Bit layout on [SPU / Scalar Slot](../isa/slot-spu-scalar.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Confidence |
|---|---|---|---|---|
| `0x077`/`0x078` | `kScalarLoad` / `kScalarStore` | scalar lane | SMEM↔SREG memory access (lane-1 bound on JF/PF) | CONFIRMED |
| `0x085` | `kScalarComposeU64` | scalar | pack two `u32` into a `u64` | CONFIRMED |
| `0x086` | `kScalarAddressCalculation` | scalar | address arithmetic (shares GhPerf 0x00C with kScalarAddS32) | CONFIRMED |
| `0x087`/`0x088` | `kScalarBranchRel` / `kScalarBranchInd` | sequencer (lane 0) | relative / indirect branch | CONFIRMED |
| `0x089` | `kScalarSelect` | scalar | scalar conditional select | CONFIRMED |
| `0x16B`/`0x16C` | `kScalarCompare` / `kScalarAddCarryU32` | scalar | compare + add-with-carry | CONFIRMED |
| `0x16D`..`0x172` | `kScalarMultiplyWordAddr` … `kScalarAddS32` | scalar | multiplies + adds (`u24`/`u32`/`f32`/`s32`) | CONFIRMED |
| `0x173`..`0x177` | `kScalarSubtractS32` … `kScalarBitwiseXor` | scalar | subtract + bitwise and/or/xor | CONFIRMED |
| `0x178`..`0x17A` | `kScalarDivRemU32` / `…U32AndPop` / `…RemU32AndPop` | scalar | divide/remainder (data-format special-cased) | CONFIRMED |
| `0x17B` | `kScalarMove` | scalar | scalar copy — **Move-exclusion** (never CSE'd/rematted) | CONFIRMED |
| `0x17D`..`0x17F` | `kScalarFloorF32` / `kScalarCeilF32` / `kScalarCountLeadingZeros` | scalar | scalar rounding + CLZ | CONFIRMED |
| `0x1A3`..`0x1A6` | `kScalarShrl` … `kScalarShllOnes` | scalar | logical / arithmetic shifts | CONFIRMED |
| `0x1A7`..`0x1AA` | `kScalarMinimumF32` … `kScalarMaximumU32` | scalar | min / max (`f32`/`u32`) | CONFIRMED |

The SPU is the complement of the vector set (`LloOpcodeIsScalar = !LloOpcodeIsVector`), 63 opcodes total; `EncodingToScalarRegister` (`0x1e871e40`) bounds the SREG selector at `> 0x1F` (32 registers, 5-bit field) — verified byte-exact. The per-gen jump-table size *is* the per-gen scalar ISA size (JF 0..0x3E, PF ≤0x33, VF ≤0x4C, GL/GF ≤0x49).

---

## VPU / Vector-ALU Slot

The per-lane vector ALU over the 8 sublane × 128 lane = 1024-element vreg. Register-file-class byte `2` (vector); the foldable/CSE bits (`opcode_info` bit5/bit6) are set on the pure-functional members. `LloOpcodeIsVectorUnop` (`0x1d60c200`) / `LloOpcodeIsVectorBinop` (`0x1d60c680`) split unary vs binary. Bit layout (6/7/8-bit opcode, 5/4/2-bit predicate per gen) on [VPU Slot](../isa/slot-vpu.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Confidence |
|---|---|---|---|---|
| `0x048`..`0x050` | `kVectorClampGezF32` … `kVectorRemapBf16` | VALU | clamp / remap (gez, symmetric, asymmetric; F32/Bf16/S4) | CONFIRMED |
| `0x051`..`0x05A` | `kVectorMultiplyAccumulate` … `kVectorMoveEvenAccLow` | VALU | MAC + accumulator moves (FOLD bit on MAC family) | CONFIRMED |
| `0x11B`..`0x124` | `kVectorAddS32` … `kVectorSubtractS16` | VALU | add / subtract (`s32`/`f32`/`bf16`/`s16`, Bf16-hi/lo) | CONFIRMED |
| `0x156`..`0x15B` | `kVectorPowF32` … `kVectorMultiplyBf16` | VALU | pow + multiply (F32/U32/U16/Bf16) | CONFIRMED |
| `0x15C`..`0x15F` | `kVectorAndU32` … `kVectorXorU32` | VALU | bitwise and / and-negated / or / xor | CONFIRMED |
| `0x162`..`0x166` | `kVectorMultiplyComposeU64` … `kVectorExtractHigh32` | VALU | 64-bit multiply compose + word extracts | CONFIRMED |
| `0x180`..`0x184` | `kVectorCountLeadingZeros` … `kVectorExtractSignificand` | VALU | CLZ / move / popcount / FP field extracts | CONFIRMED |
| `0x181` | `kVectorMove` | VALU | vector copy — **Move-exclusion** (never CSE'd/rematted) | CONFIRMED |
| `0x19A`..`0x19C` | `kVectorShiftRightLogical` … `kVectorShiftLeftLogical` | VALU | vector shifts | CONFIRMED |
| `0x19D`..`0x1A2` | `kVectorMaximumF32` … `kVectorMinimumU32` | VALU | min / max (F32/Bf16/U32) | CONFIRMED |

The VALU is the largest family; only the representative bands are shown. `vmul.u32.u64` is a PF+ slot-pair wide multiply. The `VectorAluYEncoding` (0..31) source-B selector — vreg / VS0-2 ports / hardwired float constants / immediate slots — is documented on [VPU Slot §Y-operand](../isa/slot-vpu.md#the-y-operand-source-b-selector).

---

## Convert / Pack / Unpack

Type conversions across three bands: `f32→{s32,f8,hf16}` and `{s8,s4,u8,u4}↔bf16`, the unpack/round/truncate block, and pack/compose. These gained the most across generations.

| `LloOpcode` | Mnemonic | Slot | Semantics | Per-gen | Confidence |
|---|---|---|---|---|---|
| `0x05B`..`0x060` | `kScalarConvertF32ToS32WithProbRounding` … `…TowardsZeroPseudo` | VALU/scalar | F32→S32 (prob-round / towards-zero) | all | CONFIRMED |
| `0x061`..`0x063` | `kVectorConvertF32ToF8E5M2` / `…E4M3Fn` / `…E4M3B11` | VALU | F32→F8 | **PF+** | CONFIRMED |
| `0x064` | `kVectorConvertF32ToHf16` | VALU | F32→Hf16 | all | CONFIRMED |
| `0x066`..`0x06D` | `kVectorConvertS8ToBf16` … `kVectorConvertBf16ToU4` | VALU | int↔Bf16 (`s8`/`s4`/`u8`/`u4`) | **S4/U4 = PF+** | CONFIRMED |
| `0x06E`/`0x06F` | `kVectorConvertEXMYToE4M3` / `…ToE5M2` | VALU | generic FP8 reformat | PF+ | CONFIRMED |
| `0x070`..`0x074` | `kVectorConvertF32ToE5M2Stochastic` … `…ToHf16Stochastic` | VALU | stochastic-rounding converts | **VF+** | CONFIRMED |
| `0x107`..`0x10F` | `kScalarConvertS32ToF32` … `kVectorDynamicUnpack` | VALU/scalar | S32→F32 convert + unpack (`kVectorUnpack` `0x109`) + B2→B4 / B4→B8 join + EXMY/dynamic | all | CONFIRMED |
| `0x110`..`0x11A` | `kVectorCeilF32` … `kVectorTruncateBf16` | VALU | round-to-int / RTNA / RTNE / truncate | all | CONFIRMED |
| `0x125`..`0x127` | `kVectorComposeF32` / `kVectorPack` / `kVectorPackEXMY` | VALU | compose + pack | all | CONFIRMED |

---

## EUP / Transcendental Slot (`0x128`..`0x14D`)

The Extended Unary Pipeline computes nine transcendentals (`tanh`, `pow2`, `reciprocal`, `log2`, `rsqrt`, `shifted-sigmoid`, `sinq`, `cosq`, `erf`) as a deferred-result pipeline: a *push* (VALU slot 3 only on v5+) and a later *pop* from the `VectorResult` slot. Each function appears in four LLO forms — F32 issue-only, Bf16 issue-only, F32 issue+pop (`AndPop`), Bf16 issue+pop. Selector map and latency on [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md).

| Band | `LloOpcode` | Members | Form | Confidence |
|---|---|---|---|---|
| Bare push F32 | `0x128`..`0x131` | Tanh/Pow2/Recip/Log2/Rsqrt/SigShft/Sinq/Cosq/Erf + `kVectorPushErf` | F32 issue-only (Push bit) | CONFIRMED |
| Bare push Bf16 | `0x132`..`0x13A` | same set | Bf16 issue-only | CONFIRMED |
| Fused F32 | `0x13B`..`0x144` | same set + `kVectorPushErfAndPop` (`0x144`) | F32 issue+pop (`*AndPop`) | CONFIRMED |
| Fused Bf16 | `0x145`..`0x14D` | same set | Bf16 issue+pop | CONFIRMED |
| Deferred pop | `0x14E` | `kVectorEupResult` | pop EUP FIFO (Pop bit, `opcode_info`=`0x0202`) | CONFIRMED |

Representative function-selector values (the 5-bit field, gfc/glc @ bit 183): F32 `tanh`=`0x13`, `rsqrt`=`0x10`, `pow2`=`0x11`, `log2`=`0x12`, `sigmoid`=`0x14`, `reciprocal`=`0x15`, `sin`=`0x17`, `cos`=`0x18`, `erf`=`0x0e`; Bf16 selectors differ (e.g. `tanh`=`0x1b`). The 18 `*AndPop` pseudo-ops (excluding `0x144`) are split by `LloLateDecomposer` into bare push + deferred pop; `LloOpcodeIsPseudoEupInstruction` (`0x1d60c880`) classifies them via the `0x7FDFF` mask — verified byte-exact.

> **NOTE — the EUP family is the cleanest Push/Pop illustration.** Every issue-only EUP opcode sets `opcode_info` bit0 (Push); `kVectorEupResult` (`0x14E`) sets bit1 (Pop). `CreateVectorEupResult` (`0x1d4d9820`) asserts the push operand is in `[0x128, 0x13A]` before building the `0x14E` pop — verified byte-exact.

---

## Cross-Lane, Reduce & XLU-Result

Cross-lane permute/rotate/broadcast, whole/segmented reductions (min/max/add/argmin/argmax in F32 and Bf16), and the deferred-result pops (EUP, cross-lane, permute, CMEM, transpose, matmul). The 21 XLU opcodes are walked by `ComputeXluOperations` (`0x126d9780`) and emit `{TransposeTile, RpuOperation, XluControlOperation}` variants — see [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Confidence |
|---|---|---|---|---|
| `0x036`..`0x03B` | `kVectorPermute` … `kVectorBroadcastLane` | XLU (load slot) | cross-lane permute / rotate / combine / broadcast | CONFIRMED |
| `0x08B`/`0x08C` | `kVectorSetPermutePattern` / `kVectorSetSegmentPattern` | XLU control | permute / segment pattern setup | CONFIRMED |
| `0x0F5`..`0x0FC` | `kVectorMinReduceF32` … `kVectorAddSegmentReduceF32` | XLU (reduce) | F32 reductions (whole + segmented + index) | CONFIRMED |
| `0x0FD`..`0x101` | `kVectorMinReduceBf16` … `kVectorMinIndexReduceBf16` | XLU (reduce) | Bf16 reductions | CONFIRMED |
| `0x102`..`0x106` | `kVectorSublaneId` … `kVectorLaneSequenceInterleavedB16` | VALU | lane/sublane identity sequences (remat-able) | CONFIRMED |
| `0x14E` | `kVectorEupResult` | VectorResult | pop EUP result FIFO | CONFIRMED |
| `0x14F`..`0x151` | `kVectorXlaneResult` / `kVectorPermuteResult` / `kVectorCmemResult` | VectorResult | pop XLU / permute / CMEM result FIFOs | CONFIRMED |
| `0x152`/`0x153` | `kVectorMatres` / `kVectorMatresAdd` | VectorResult | pop MXU result (plain / accumulate) | CONFIRMED |
| `0x154`/`0x155` | `kVectorTransposeResult` / `kVectorTransposeClear` | VectorResult | pop transpose result / clear transpose FIFO | CONFIRMED |

`kVectorXlaneResult`/`kVectorPermuteResult`/`kVectorTransposeResult` share GhPerf cost row 0x1C7. `kVectorTransposeClear` (`0x155`) is the single opcode whose `opcode_info` word is `0x000E`.

---

## MXU — Matmul, Latch, Matprep, Result

The systolic-array op family: latch the stationary weights, matprep the moving operand, issue the matmul, drain via matres. Latch/matprep/matmul share the `VectorExtended` slot; matres uses `VectorResult`. The matmul band (`0x8D`..`0xA5`) is data-format special-cased *before* the property-word read. Field encoding (per-gen 6→7→8-bit opcode, the −20/−21/−25 inter-MXU twin) on [MXU Slot](../isa/slot-mxu.md); latch/matprep field maps on [Matprep/IAR/Latch](../isa/slot-matprep-iar-latch.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Builder / classifier | Confidence |
|---|---|---|---|---|---|
| `0x08D` | `kVectorLatchLsf` | VectorExtended | latch stationary weights (load-stationary-from-FIFO) | `CreateVectorLatchLsf` @ `0x1d4d7aa0` → `New(141)` | CONFIRMED |
| `0x08E` | `kVectorLatchLsfMsk` | VectorExtended | masked LSF latch | — | CONFIRMED |
| `0x08F`..`0x096` | `kVectorLatch` … `kVectorLatch3Msk` | VectorExtended | gain-matrix latch (plain / 1..3 sub-bank, masked) | `CreateVectorLatchHelper` | CONFIRMED |
| `0x097`/`0x098` | `kVectorMatprepSubr` (`+Msk`) | VectorExtended | push moving operand, sub-row form | `(op-151)<2` @ `0x1d60c400` | CONFIRMED |
| `0x099`/`0x09A` | `kVectorMatprepMubr` (`+Msk`) | VectorExtended | push moving operand, block-row form | `(op-153)<2` @ `0x1d60c3e0` | CONFIRMED |
| `0x09B` | `kVectorMatmul` | VectorExtended | one systolic step | `EmitVectorMatmul` @ `0x140b92c0` | CONFIRMED |
| `0x09C`/`0x0A0` | `kVectorMatmulMubr` (`+Msk`) | VectorExtended | conv block-row matmul | `((op-156)&0xFFFB)==0` @ `0x1d60c3c0` | CONFIRMED |
| `0x09D`/`0x09E` | `kVectorMatmulHigh` / `kVectorMatmulLow` | VectorExtended | high-half / low-half accumulator step | `EmitVectorMatmul` | CONFIRMED |
| `0x09F`..`0x0A5` | `kVectorMatmulMsk` … `kVectorMatmulLmr` | VectorExtended | masked / packed (`kVectorMatmulPacked` `0x0A3`) / LMR-fused matmul | — | CONFIRMED |
| `0x0A6`/`0x0A7` | `kVectorTranspose` / `kVectorTransposeBinary` | VectorExtended | XLU transpose (vxpose-mode dispatched) | — | CONFIRMED |
| `0x0A8`..`0x0AB` | `kVectorDoneWithGains` … `kVectorLoadLmrWithBf16Conversion` | VectorExtended | gain handshake + GMR/LMR loads | — | CONFIRMED |
| `0x152`/`0x153` | `kVectorMatres` / `kVectorMatresAdd` | VectorResult | matmul result collection (plain / accumulate) | `a3 != 338` @ EmitVectorMatres | CONFIRMED |

Per-gen dtype set: PF/VF/GL carry 8 formats `{F32, If8, Bf16, Bf8}` × float + `{U8, S8, U4, S4}` × int; gfc (v6e) is **float-only** 4-format `{F32, E4m3, Bf16, E5m2}`. MXUs per TensorCore: 1 (JF) / 4 (PF, VF) / 2 (GL, gfc). MSR banks: 1 (JF/PF) / 2 (VF/GL/gfc).

> **GOTCHA — the matmul push bit is data-format-dependent, not static.** `LloInstructionPushesToResultFifo` tests the matmul band (`0x8D`..`0xA5`) via a bitmask and routes to a `matmul_data_format` vtable call; reading the static Push bit from `opcode_info[op]` for a matmul opcode gives the wrong FIFO behavior. The push depends on the data format decided at the instruction instance.

---

## Memory Load / Store / IAR / RNG

Per-lane index-address-register (IAR) setup, the per-lane PRNG, and the load/store slots. The tier (VMEM/SMEM/CMEM/SPMEM) is selected by *which slot* the op occupies, not a tier bit. CMEM-load is a dedicated PF-only slot. Bit layout on [Memory-Load](../isa/slot-memory-load.md), [Memory-Store](../isa/slot-memory-store.md), [CMEM-Load (PF)](../isa/slot-cmem-load-pf.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Per-gen | Confidence |
|---|---|---|---|---|---|
| `0x001` | `kVectorReadIar` | load slot | read index-address register into a vreg | all | CONFIRMED |
| `0x002`..`0x004` | `kVectorSetIarLane` / `…Raw` / `…Sublane` | store slot | set IAR (lane/raw/sublane; LLO≠slot opcode) | all | CONFIRMED |
| `0x030`..`0x035` | `kVectorLoadSublaneShuffle` … `kVectorCmemLoadAndPop` | load slot | vector + CMEM loads | all (CMEM = PF) | CONFIRMED |
| `0x03C`..`0x03E` | `kVectorPrng` / `kVectorSetRngSeed` / `kVectorGetRngSeed` | VALU/store | per-lane PRNG (seeds stochastic converts) | all | CONFIRMED |
| `0x03F`..`0x046` | `kVectorStore` … `kVectorStoreEvenOddSublanes` | store slot | vector + CMEM stores (indexed/masked/shuffle/even-odd) | all | CONFIRMED |
| `0x047` | `kVectorNop` | VALU | vector no-op (shares GhPerf 0x1B4 with kVectorMaskMove) | all | CONFIRMED |
| `0x077`/`0x078` | `kScalarLoad` / `kScalarStore` | scalar lane | SMEM scalar access | all | CONFIRMED |

The store window classified by `LloOpcodeIsVectorStore` (`0x14024920`) is internal `{63,64,65,68,69,70}` ∪ `{460}` — verified byte-exact (`(op-63)<=7 && _bittest(0xE7, op-63) || op==460`). The load window `LloOpcodeIsVectorLoad` (`0x14024900`) is `{48..51}` ∪ `{457,458}` — verified byte-exact. `IarsPerTensorCore` = 2 on every gen.

The Ghostlite perf classifier (`GetGhostliteInstruction` @ `0x1c8b1740`) keys the indexed-memory ops on an IAR-present sentinel; the seven IAR-class opcodes are byte-exact (used as the IAR op-id ground truth):

| `LloOpcode` | Mnemonic | Role | Confidence |
|---|---|---|---|
| `0x001` | `kVectorReadIar` | drain IAR into a vreg | CONFIRMED |
| `0x002` | `kVectorSetIarLane` | set IAR (lane mode) | CONFIRMED |
| `0x003` | `kVectorSetIarRaw` | set IAR (raw mode) | CONFIRMED |
| `0x004` | `kVectorSetIarSublane` | set IAR (sublane mode) | CONFIRMED |
| `0x032` | `kVectorLoadIndexed` | gather VMEM[base + IAR] | CONFIRMED |
| `0x040` | `kVectorStoreIndexed` | scatter to VMEM[base + IAR] | CONFIRMED |
| `0x044` | `kVectorStoreIndexedMasked` | masked scatter | CONFIRMED |

> **QUIRK — the LLO IAR opcode number and the bundle-slot IAR opcode do not line up.** LLO `kVectorSetIarRaw` (`0x03`) maps to ISA slot-opcode 4, and LLO `kVectorSetIarSublane` (`0x04`) maps to slot-opcode 3 — the Sublane/Raw encodings cross. A reimplementation that assumes LLO-opcode == slot-opcode swaps them. See [Matprep/IAR/Latch §IAR](../isa/slot-matprep-iar-latch.md#the-iar--index-address-register).

---

## DMA (`0x0B3`..`0x0DA`)

The 40 contiguous DMA opcodes — the largest single contiguous block — enumerate direction (HBM/VMEM/SMEM/CMEM/HIB/IMEM/Host + IOVA host) × source/destination-register variants + a `WithHibUpdate` family + two terminators. DMA is issued from the sequencer/scalar lane via descriptors; none set a result-FIFO Push/Pop bit (`opcode_info` low byte `0x00`).

| `LloOpcode` | Mnemonic | Semantics | Confidence |
|---|---|---|---|
| `0x0B3`/`0x0B4` | `kDmaGeneral` / `kDma` | generic DMA forms | CONFIRMED |
| `0x0B5`..`0x0CC` | `kDmaHbmToVmem` … `kDmaSmemToVmem` | direction matrix (HBM/VMEM/SMEM/CMEM/HIB/IMEM) | CONFIRMED |
| `0x0CD`..`0x0D0` | `kDmaHbmToVmemWithHibUpdate` … `…VdstWithHibUpdate` | HBM→VMEM with HIB update | CONFIRMED |
| `0x0D1`..`0x0D8` | `kDmaHbmToHost` … `kDmaSmemToHostIova` | host DMA (direct + IOVA) | CONFIRMED |
| `0x0D9`/`0x0DA` | `kDmaDone` / `kDmaDoneWait` | DMA completion / wait | CONFIRMED |

> **NOTE — the cost model prices DMA by direction-class.** The 14 HBM/Host-DMA opcodes collapse to GhPerf row 0x40 and the 14 VMEM/SMEM-DMA opcodes to row 0x43; the 40 distinct `LloOpcode` values all exist but the latency model treats each direction family as one.

---

## Predicate / Mask Slot

Scalar predicate (1-bit `Preg`, P0..P14/15) and per-lane vector mask (`Vmreg`, M0..M31) ops — two physically distinct register files. Compare ops produce a mask; select consumes one. There is **no native predicate-AND** (synthesized via De Morgan). Bit layout on [Predicate Slot](../isa/slot-predicate.md); the M-register rectangle on [vcreate_mask](../isa/slot-vcreate-mask-mregister.md).

| `LloOpcode` | Mnemonic | Slot | Semantics | Confidence |
|---|---|---|---|---|
| `0x0E1` | `kPredicateConstant` | predicate | remat-able predicate constant (`opcode_info`=`0x00D0`) | CONFIRMED |
| `0x0E2`/`0x0E3` | `kVectorMaskConstant` / `…Packed` | mask | mask constants | CONFIRMED |
| `0x0E5`..`0x0E8` | `kPredicateNegate` … `kPredicateOr` | predicate | predicate logic (share GhPerf row 0x032) | CONFIRMED |
| `0x0E6` | `kPredicateMove` | predicate | predicate copy — **Move-exclusion** | CONFIRMED |
| `0x167`..`0x16A` | `kVectorCompare` … `kVectorAddCarryU16` | mask | mask-producing compares + add-carry | CONFIRMED |
| `0x193`..`0x199` | `kVectorMaskXor` … `kVectorMaskMove` | mask | mask logic / pack-compressed / negate / move | CONFIRMED |
| `0x195` | `kVectorMaskAnd` | mask | mask AND (synthesized-rectangle combine) | CONFIRMED |
| `0x198` | `kVectorMaskNegate` | mask | mask complement | CONFIRMED |
| `0x199` | `kVectorMaskMove` | mask | mask copy — **Move-exclusion** | CONFIRMED |

The four Move-exclusion opcodes — `kScalarMove` (`0x17B`), `kVectorMove` (`0x181`), `kVectorMaskMove` (`0x199`), `kPredicateMove` (`0xE6`) — are skipped by the CSE/remat/fold passes. Native `vcreate_mask` exists only on VF/GL (synthesized via iota-compare on JF/DF/PF).

---

## Constants, Pseudo & Call

Constants, Phi/pseudo SSA nodes, and the call/tuple group. Constants set the remat bit (`opcode_info` `0x50`); Phi nodes short-circuit to cost 0. Address materialization is remat-able. Literals route through immediate slots — see [Immediate Slot](../isa/slot-immediate.md).

| `LloOpcode` | Mnemonic | Semantics | Confidence |
|---|---|---|---|
| `0x02C`..`0x02E` | `kAllocationAddress` / `kParameterAddress` / `kIntToPtr` | address materialization (remat-able) | CONFIRMED |
| `0x0DB`..`0x0E0` | `kScalarConstantU32` … `kVectorConstantF32` | scalar / vector constants (U32/PackedBf16/F32) | CONFIRMED |
| `0x0E4` | `kVectorConstantU64` | 64-bit vector constant | CONFIRMED |
| `0x0E9`..`0x0EC` | `kPredicatePhi` / `kScalarPhi` / `kVectorPhi` / `kVectorMaskPhi` | SSA phi nodes (cost 0) | CONFIRMED |
| `0x0ED`..`0x0F0` | `kTuple` / `kInlinedCall` / `kCall` / `kInlinedCallOperand` | call / tuple structure | CONFIRMED |
| `0x0F1`..`0x0F4` | `kPredicatePseudo` … `kVectorMaskPseudo` | pseudo placeholders per register class | CONFIRMED |
| `0x17C` | `kRelocatableConstant` | link-time relocated constant | CONFIRMED |

---

## BarnaCore (SparseCore) Block (`0x1AC`..`0x1CC`)

The 33 highest opcodes are the BarnaCore (SparseCore) instruction set — embedding scatter/gather, sparse reduce, remote scalar writes, and the BarnaCore-local vector load/store/move. These are the opcodes the LLVM-MC emitter actually encodes (its populated `InstBits_BarnaCorePxcHwMode` table covers this range), unlike the TensorCore opcodes which it returns all-zero. The SparseCore scalar/vector opcode *enums* (the per-engine SCS/TAC/TEC scalar-ALU and vector-ALU rosters) are separate engine-family enums with their own deep pages — see [SPU / Scalar Slot §SparseCore](../isa/slot-spu-scalar.md#sparsecore-scalar-slot) and [VPU Slot](../isa/slot-vpu.md).

| `LloOpcode` | Mnemonic | Semantics | Confidence |
|---|---|---|---|
| `0x1AC`..`0x1B3` | `kBarnaCoreScalarWaitDone` … `kBarnaCoreScalarWaitNe` | scalar wait / sync primitives | CONFIRMED |
| `0x1B4`..`0x1B7` | `kBarnaCoreScalarSyncDoneRead` … `…SyncPublicAccessWrite` | sync-flag read / write | CONFIRMED |
| `0x1B8`..`0x1BB` | `kBarnaCoreScalarPop` … `kBarnaCoreDma` | pop / FSM issue / fence (`kBarnaCoreScalarFence` `0x1BA`) / DMA | CONFIRMED |
| `0x1BC`..`0x1C8` | `kBarnaCoreRemoteScalarWrite` … `kBarnaCorePfLocalScatterGradients` | remote write / scatter-gather / sparse-reduce | CONFIRMED |
| `0x1C9`..`0x1CC` | `kBarnaCoreVectorLoad` … `kBarnaCoreVectorStore` | BarnaCore vector load / store / move | CONFIRMED |

> **QUIRK — the BarnaCore vector load/store opcodes (457..460) test as *vector*; the scalar/sync ones (428..456) test as non-vector.** This is the only place the BarnaCore block straddles the vector/scalar partition, and it matters for register-class selection. The TpuSequencerType enum that selects the SparseCore engine codec is `kTensorCoreSequencer`(0)..`kSparseCoreTileExecuteSequencer`(5); `TpuSequencerTypeFromProto` (`0x20b36300`) maps proto 1..6 → internal 0..5 — verified byte-exact. BarnaCore is a pre-v5 construct (JF address-handler, PF sequencer); from v5 onward SparseCore (SCS/TAC/TEC) replaces it, and 6acc60406 (v7x) drops the TAC engine.

---

## Per-Generation Availability Summary

`LloOpcode` numbering is gen-invariant; the *valid subset* grows per silicon. The codename → namespace map: jxc = Jellyfish (`jellyfish::isa`) + Dragonfish (alias); pxc = Pufferfish; vxc/vfc = Viperfish; glc = Ghostlite; gfc = 6acc60406.

| Generation | Codename | LloOpcode additions over prior gen | Confidence |
|---|---|---|---|
| TPU v2 / v3 | Jellyfish / Dragonfish | base set (proto-direct encoding, no Compact encoder) | HIGH |
| TPU v4 | Pufferfish | F8 converts (`0x061`..`0x063`), S4/U4 int↔Bf16 (`0x067`/`0x069`/`0x06B`/`0x06D`), `kCmemFence` (`0x01E`), CMEM DMA/load/store | HIGH |
| TPU v5e | Viperfish | stochastic-rounding converts (`0x070`..`0x074`); SparseCore SCS/TAC/TEC engines; native `vcreate_mask` | HIGH |
| TPU v6e | Ghostlite | `vector_misc` slot ops; consolidated `VectorSelect`; 2 vector-store write ports | MEDIUM |
| TPU7x | 6acc60406 | dual matrix staging (MSRA/MSRB); float-only FP8 matmul remap; dual-predicate slot; drops TAC engine, yield machinery | MEDIUM |

> **NOTE — append-and-insert, not append-only.** New opcodes are *inserted* into the in-memory `LloOpcode` at their family's natural position (keeping families contiguous) but *appended* to the end of the `LloOpcodeProto` wire enum (preserving wire compatibility). This is why proto 498/499 map to low in-memory opcodes (`0x084`/`0x197`) — verified byte-exact. Port the actual `LloOpcodeToProto` table (`0x344cb4c`), not a formula. See [LloOpcode↔Proto](../isa/llo-opcode-to-proto.md).

---

## Verification Provenance

A representative sample of this table's value↔mnemonic↔slot bindings is grounded directly in the binary (classifiers, builders, converters). Probes (all CONFIRMED byte-exact):

| Probe | Binary fact | Confirms |
|---|---|---|
| `LloOpcodeName` @ `0x1d631280` | bound `>= 0x1CD` → `ud1`; indexes `opcode_name` @ `0x21ccfef0` | 461 dense enumerators |
| `opcode_info` / `opcode_info_big` symbols | `0x223a1320` / `0x227b5570` resolved by name | metadata table addresses |
| `LloOpcodeIsVectorStore` @ `0x14024920` | `(op-63)<=7 && _bittest(0xE7,op-63) \|\| op==460` | store window `{63,64,65,68,69,70,460}` |
| `LloOpcodeIsVectorLoad` @ `0x14024900` | `(op-48)<4 \|\| (op-457)<2` | load window `{48..51,457,458}` |
| `LloOpcodeIsVectorMatprepSubr/Mubr/MatmulMubr` | `(op-151)<2` / `(op-153)<2` / `((op-156)&0xFFFB)==0` | matprep `0x97/0x98`, `0x99/0x9a`; matmul-mubr `0x9c/0xa0` |
| `EmitVectorMatres` | `a3 != 338` guard | `kVectorMatres` = `0x152` (338) |
| `CreateVectorLatchLsf` @ `0x1d4d7aa0` | `New(141, …)` | `kVectorLatchLsf` = `0x8D` (141) |
| `LloOpcodeIsPseudoEupInstruction` @ `0x1d60c880` | `(op-315)<0x13 & (0x7FDFF >> (op-59))` | fused-EUP band `0x13B`..`0x14D` minus `0x144` |
| `CreateVectorEupResult` @ `0x1d4d9820` | asserts push opcode `(op-296) < 0x13` | EUP push range `[0x128,0x13A]` → pop `0x14E` |
| `EncodeTensorCoreVectorAlu3F32Tanh` (gfc) | op=0 @194/w8, sel=19 @183/w5, src @188/w6 | F32 Tanh selector `0x13` |
| `EncodingToScalarRegister` @ `0x1e871e40` | bound `> 0x1F` → error | 32-deep SREG file (5-bit) |
| `TpuSequencerTypeFromProto` @ `0x20b36300` | proto 1..6 → internal 0..5 switch | 6-value sequencer-type enum |
| `LloOpcodeToProto` @ `0x14420020` | GOT-relative table @ `0x344cb4c` | forward map is a flat lookup |
| `ProtoToLloOpcode` @ `0x14420040` | 499-arm switch; proto 497→460, 498→132, 499→407; 38 `__debugbreak` arms | reverse map + 38 reserved gaps |

---

## Cross-References

- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the master family taxonomy, the `opcode_info` / `opcode_info_big` metadata tables, and the per-family value ranges this table consolidates.
- [Sequencer Ops Per Gen](../isa/sequencer-ops-per-gen.md) — the per-(generation × sequencer-type) control-flow op inventory and the `TpuSequencerType` enum.
- [InstBits Master DB](../isa/instbits-master-db.md) — the LLVM-MC base-bits table (`opcode − 499` index) that encodes only the BarnaCore subset.
- [LloOpcode↔Proto](../isa/llo-opcode-to-proto.md) — the wire-value converters and the 38 reserved proto gaps.
- [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md) — the FIFO and physical-register enums the XLU/result-pop opcodes reference.
- [MXU Slot](../isa/slot-mxu.md) · [Matprep/IAR/Latch](../isa/slot-matprep-iar-latch.md) — matmul/latch/matprep/matres bit encoding and the IAR.
- [VPU Slot](../isa/slot-vpu.md) · [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — vector-ALU and transcendental push/pop encoding.
- [SPU / Scalar Slot](../isa/slot-spu-scalar.md) · [Sequencer Slot](../isa/slot-sequencer.md) — scalar-ALU and PC-mutation encoding.
- [Memory-Load](../isa/slot-memory-load.md) · [Memory-Store](../isa/slot-memory-store.md) · [CMEM-Load (PF)](../isa/slot-cmem-load-pf.md) — the load/store/IAR slot encodings and the tier-by-slot model.
- [Predicate Slot](../isa/slot-predicate.md) · [vcreate_mask / M-Register](../isa/slot-vcreate-mask-mregister.md) — the scalar-predicate and vector-mask register files.
- [Immediate Slot](../isa/slot-immediate.md) · [Loop / LCC](../isa/slot-loop.md) · [Sparsity Slot (v5+)](../isa/slot-sparsity-v5plus.md) — immediate pool, loop counter, and the (open) sparsity slot.
- [LLVM-TPU Intrinsic Table](llvmtpu-intrinsic-table.md) — the sibling appendix of `llvm.tpu.*` intrinsics that lower onto these opcodes.
- [Per-Gen Comparison Matrix](per-gen-comparison-matrix.md) — the sibling appendix consolidating per-generation ISA deltas.
