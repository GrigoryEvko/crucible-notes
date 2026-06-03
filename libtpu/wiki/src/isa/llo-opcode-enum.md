# LloOpcode Enum

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`xla::jellyfish::LloOpcode` is the in-memory opcode enum of LLO (Low-Level Optimizer IR), the TPU-specific late compiler IR that sits below MHLO/TLP and above the per-generation TensorCore bundle encoders. It is a **dense, zero-based** enumeration: the binary spells exactly **461 enumerators**, values `0x000`..`0x1CC` (0..460), with no gaps. Every value carries a `k`-prefixed name in a relocated `char*` table; the names cleanly sort into functional families — sequencer/sync, scalar SPU arithmetic, vector VPU arithmetic, EUP transcendentals, cross-lane/reduction/XLU, the MXU matmul/latch/transpose/result group, load/store, a 40-opcode DMA block, predicate/mask, pseudo nodes, and a 33-opcode BarnaCore (SparseCore) block.

`LloOpcode` is distinct from three sibling index spaces a reimplementer must not conflate. It is **not** the `LloOpcodeProto` wire enum (1-based, max value 499, 38 reserved gaps — see [LloOpcode↔Proto](llo-opcode-to-proto.md)). It is **not** the `GhPerf::Instruction` cost-grid enum (a denser 0..0x1DB space where 19 rows are shared across opcodes). And it is **not** the MC `MCInst` opcode space the [MC-Emitter](mc-emitter.md) dispatches on (offset by +499, gating `opcode <= 0x1F2` and indexing `4*opc-1996`). `LloOpcode` is the one all the others map *from*.

This page is a structured reference catalog, not an algorithm trace. It groups the 461 values by family, gives each family its value range and representative members, names the per-family classifier the binary uses, and flags the per-generation additions (F8 conversions, stochastic rounding, S4/U4 matmul, dual matrix staging). The exhaustive 461-row dump lives in [the appendix](../appendix/llo-opcode-table.md); here the goal is the *shape* of the space.

| | |
|---|---|
| **Enum** | `xla::jellyfish::LloOpcode` — 461 enumerators, dense, `0x000`..`0x1CC` |
| **Name accessor** | `LloOpcodeName(LloOpcode)` @ `0x1d631280` — bound `>= 0x1CD` → `ud1` |
| **Name table** | `opcode_name` @ `0x21ccfef0` (`.data.rel.ro`, 461 × `char*`, `R_X86_64_RELATIVE`) |
| **Property word** | `opcode_info` @ `0x223a1320` (461 × `uint16`) — Push/Pop/Remat/Fold/Cse + reg-file class |
| **Descriptor** | `opcode_info_big` @ `0x227b5570` (461 × 28 B) — result-FIFO + arch-register lists |
| **Family classifiers** | `LloOpcodeIsVector` @ `0x1d60c1c0`, `LloOpcodeIsScalar` @ `0x1d60c7e0` (= `!IsVector`), `LloOpcodeIsVectorUnop/Binop/Load/Store`, … |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## How The Enum Is Stored And Named

`LloOpcode` is an ordinary C++ scoped enum; nothing in the binary stores it as a class. The single piece of reflection is `LloOpcodeName`, which converts a value to its string:

```c
// xla::jellyfish::LloOpcodeName @ 0x1d631280 (decompiled, exact)
std::string LloOpcodeName(uint32_t opcode) {
    if (opcode >= 0x1CD)                              // bound = 461 enumerators
        __builtin_trap();                             // ud1 — out of enum range
    const char *s = opcode_name[(uint16_t)opcode];    // opcode_name @ 0x21ccfef0
    return std::string(s, strlen(s));                 // SSO or heap copy
}
```

The bound `0x1CD` is the contract: the valid domain is `0x000`..`0x1CC` inclusive, 461 values. `opcode_name` is a `char*` table in `.data.rel.ro`; each slot is stored as `0` in the file and filled by an `R_X86_64_RELATIVE` relocation pointing into `.rodata`. All 461 slots are relocated and all 461 strings are non-empty. This same 461-bound appears verbatim in every consumer of the two per-opcode metadata tables (`cmp opcode, 0x1CE; jae <fatal>` — the `< 0x1CE` form), and the metadata tables (`opcode_info`, `opcode_info_big`) are each sized `461 × stride`.

> **QUIRK — the enum has 461 members, not 462.** The "462" figure the [ISA overview](overview.md) quotes is the *nominal* member count of the `LloOpcodeProto` *wire* enum: 1-based with a value-0 sentinel, so 461 live (mappable) wire values + 1 sentinel = 462 nominal. Its addressable range is wider still (max 499, with 38 reserved gaps; `499 − 38 = 461` live — see [LloOpcode↔Proto](llo-opcode-to-proto.md)). The in-memory `LloOpcode` a reimplementer's compiler manipulates is exactly 461 dense values (`LloOpcodeName` bound `0x1CD`, verified at `0x1d631280`). Drive a switch off 462 and the last index reads past the table; off the proto's 499 and you index garbage.

### Two metadata tables ride alongside the enum

Every opcode is indexed in lock-step into two parallel per-opcode tables the LLO scheduler and optimizer read:

| Table | Address | Stride | Carries | Confidence |
|---|---|---|---|---|
| `opcode_info` | `0x223a1320` | 2 B (`uint16`) | LOW byte = property bitfield (bit0 Push / bit1 Pop / bit4 Remat / bit5 Fold-const / bit6 Cse / bit7 pred-mask tag); HIGH byte = register-file class (0 none/pred, 1 scalar/mask, 2 vector) | CONFIRMED |
| `opcode_info_big` | `0x227b5570` | 28 B | `int8 result_fifos[8]` @+0x00 (neg-terminated, ResultFifo 0..0xF), 12-B reserved gap @+0x08, `int8 arch_registers_written[8]` @+0x14 (neg-terminated, ArchRegister 1..0x32) | CONFIRMED |

Both are indexed by the raw `LloOpcode` value with the same `< 0x1CE` bound. They are documented in their own pages; this page references them only to anchor each family's scheduler behavior (which opcodes push/pop result FIFOs, which are CSE-able, which write registers).

---

## Family Taxonomy

The binary does not store a family tag per opcode; family membership is recovered from (a) the `k`-prefix of each name, (b) the dense classifier switches (`LloOpcodeIsVector`, `LloOpcodeIsVectorUnop`, `LloOpcodeIsVectorLoad`, …), and (c) the `opcode_info` register-file-class byte. The table below is the at-a-glance map of the eleven families; the per-family sections that follow give ranges and representatives.

| Family | Value range (mostly contiguous) | Count | Reg-file (typical) | Primary classifier |
|---|---|---:|---|---|
| Sequencer / sync / FIFO transfer | `0x000`..`0x030` (interleaved) | ~50 | scalar/none | — |
| Scalar SPU arithmetic & control | scattered, dense tail `0x16B`..`0x1AA` | 63 | scalar | `!LloOpcodeIsVector` |
| Vector VPU arithmetic & logic | dense `0x11B`..`0x1A2` core | (subset of 295) | vector | `LloOpcodeIsVectorUnop/Binop` |
| Vector convert / pack / unpack | `0x05B`..`0x076`, `0x107`..`0x10F`, `0x126`..`0x127` | ~45 | vector | (convert-prefix) |
| EUP transcendentals | `0x128`..`0x14D` | 38 | vector (EUP FIFO) | (Tanh/Pow2/Recip/Log2/Rsqrt/Sig/Sin/Cos/Erf × F32/Bf16 × {,AndPop}) |
| Cross-lane / reduce / XLU result | `0x0F5`..`0x101`, `0x14E`..`0x155` | ~28 | vector | (Reduce/Result prefix) |
| MXU matmul / latch / matprep / matres | `0x08D`..`0x0AB`, `0x152`..`0x153` | ~33 | vector | `LloOpcodeIsVectorMatprep*`, matmul band `0x8D`..`0xA5` |
| Load / store / IAR / RNG | `0x001`..`0x004`, `0x030`..`0x046`, `0x077`..`0x078` | ~30 | mixed | `LloOpcodeIsVectorLoad` @ `0x14024900`, `…IsVectorStore` @ `0x14024920` |
| DMA | `0x0B3`..`0x0DA` (contiguous) | 40 | none | (Dma prefix) |
| Predicate / mask | `0x0E1`..`0x0F1`, `0x193`..`0x199` | ~18 | predicate/mask | (Predicate/Mask prefix) |
| Constants / pseudo / call | `0x0DB`..`0x0F4`, `0x02C`..`0x02E`, `0x17C` | ~25 | mixed | (Constant/Phi/Pseudo/Call prefix) |
| BarnaCore (SparseCore) | `0x1AC`..`0x1CC` (contiguous) | 33 | mixed | (BarnaCore prefix) |

> **NOTE — vector-vs-scalar is a per-opcode partition, not a range split.** `LloOpcodeIsScalar` is literally `LloOpcodeIsVector(op) ^ 1` (@ `0x1d60c7e0`), and `LloOpcodeIsVector` (@ `0x1d60c1c0`) is a dense `switch` that returns `1` for vector opcodes and `0` for the rest — but the two sets interleave across the value space. For example `kEvent` (0), `kLog` (5), `kHloStart`/`kHloEnd` (6/7) are non-vector; `kVectorReadIar` (1) is vector; the BarnaCore vector load/store ops (457..460) are vector while the BarnaCore scalar ops (428..456) are not. A reimplementer must port the switch, not a `>= threshold` test.

---

## Sequencer, Sync, and FIFO-Transfer Family (`0x000`..`0x030`)

The low opcodes are the control-and-handshake layer: program boundaries, scheduling barriers, fences, and the scalar/vector/sync-flag result-FIFO push/pop primitives that move values between the SPU, VPU, and the cross-FIFO (CCF) staging registers.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x000` | `kEvent` | program/trace event marker | CONFIRMED |
| `0x006` / `0x007` | `kHloStart` / `kHloEnd` | source-HLO span markers (debug-info) | CONFIRMED |
| `0x008` | `kSchedulingBarrier` | hard reorder barrier (vetoes CSE across it) | CONFIRMED |
| `0x009` | `kScalarToVector` | scalar→vector broadcast push | CONFIRMED |
| `0x00A`..`0x012` | `kVectorToScalarPush` … `kDrfPop` | V→S / sync-flag→scalar FIFO push/pop quartet | CONFIRMED |
| `0x013`..`0x014` | `kScalarFence` / `kVectorStoreFence` | ordering fences | CONFIRMED |
| `0x015`..`0x01C` | `kScalarCcfPush` … `kVectorCcfPopAsymmetrical` | cross-core FIFO (CCF) push/pop, symmetric + asymmetric | CONFIRMED |
| `0x01D` | `kMegacoreSwapCoresPseudo` | megacore core-swap pseudo | CONFIRMED |
| `0x01E` | `kCmemFence` | CMEM ordering fence (Pufferfish+) | CONFIRMED |
| `0x01F`..`0x024` | `kScalarReadCycleStart` … `kScalarReadCycleLow` | cycle-counter reads / 64-bit splits | CONFIRMED |
| `0x025`..`0x027` | `kScalarHalt` / `…YieldConditional` / `…OnError` | sequencer halt variants | CONFIRMED |
| `0x029` | `kProgramLaunchSc` | SparseCore program launch | CONFIRMED |
| `0x02B` | `kVectorInterrupt` | vector-side interrupt | CONFIRMED |

The push/pop bits (`opcode_info` bit0/bit1) are set on exactly the FIFO-transfer members: e.g. `kVectorToScalarPush` (0x0A) and the `*Push` group carry bit0; `kSfrfPop`/`kDrfPop` (0x11/0x12) and the `*Pop` group carry bit1. `kScalarHalt`/`kScalarHaltOnError` share `GhPerf` cost row 0x000.

---

## Scalar SPU Family

Scalar opcodes are the complement of the vector set. They cluster in two places: a few address/compose/branch/select members in the `0x085`..`0x089` band, and a dense arithmetic-and-shift tail `0x16B`..`0x1AA`. The register-file-class byte for these is `1` (scalar/mask).

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x085` | `kScalarComposeU64` | pack two `u32` into a `u64` | CONFIRMED |
| `0x086` | `kScalarAddressCalculation` | address arithmetic (shares `GhPerf` 0x00C with `kScalarAddS32`) | CONFIRMED |
| `0x087`/`0x088` | `kScalarBranchRel` / `kScalarBranchInd` | relative / indirect branch | CONFIRMED |
| `0x089` | `kScalarSelect` | scalar conditional select | CONFIRMED |
| `0x16B`..`0x16C` | `kScalarCompare` / `kScalarAddCarryU32` | compare + add-with-carry | CONFIRMED |
| `0x16D`..`0x172` | `kScalarMultiplyWordAddr` … `kScalarAddS32` | multiplies + adds (`u24`/`u32`/`f32`/`s32`) | CONFIRMED |
| `0x173`..`0x177` | `kScalarSubtractS32` … `kScalarBitwiseXor` | sub + bitwise and/or/xor | CONFIRMED |
| `0x178`..`0x17A` | `kScalarDivRemU32` / `kScalarDivU32AndPop` / `kScalarRemU32AndPop` | divide/remainder (data-format-special-cased in FIFO analyses) | CONFIRMED |
| `0x17B` | `kScalarMove` | scalar copy — **Move-exclusion** opcode (never CSE'd/rematted) | CONFIRMED |
| `0x17D`..`0x17F` | `kScalarFloorF32` / `kScalarCeilF32` / `kScalarCountLeadingZeros` | scalar rounding + CLZ | CONFIRMED |
| `0x1A3`..`0x1A6` | `kScalarShrl` … `kScalarShllOnes` | logical/arith shifts | CONFIRMED |
| `0x1A7`..`0x1AA` | `kScalarMinimumF32` … `kScalarMaximumU32` | min/max (`f32`/`u32`) | CONFIRMED |

---

## Vector VPU Arithmetic & Logic Family

The vector ALU is the largest family. Its arithmetic core is the contiguous `0x11B`..`0x1A2` block; clamp/accumulate helpers sit earlier at `0x048`..`0x05A`. `LloOpcodeIsVectorUnop` (@ `0x1d60c200`) and `LloOpcodeIsVectorBinop` (@ `0x1d60c680`) partition unary vs binary forms. The register-file byte is `2` (vector); the foldable/CSE-able bits (`opcode_info` bit5/bit6) are set on the pure-functional members.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x048`..`0x050` | `kVectorClampGezF32` … `kVectorRemapBf16` | clamp / remap (gez, symmetric, asymmetric; F32/Bf16/S4) | CONFIRMED |
| `0x051`..`0x05A` | `kVectorMultiplyAccumulate` … `kVectorMoveEvenAccLow` | MAC + accumulator moves (FOLD bit set on MAC family) | CONFIRMED |
| `0x11B`..`0x124` | `kVectorAddS32` … `kVectorSubtractS16` | add/subtract (`s32`/`f32`/`bf16`/`s16`, plus Bf16-high/low) | CONFIRMED |
| `0x156`..`0x15B` | `kVectorPowF32` … `kVectorMultiplyBf16` | pow + multiply (F32/U32/U16/Bf16) | CONFIRMED |
| `0x15C`..`0x15F` | `kVectorAndU32` … `kVectorXorU32` | bitwise and / and-negated / or / xor | CONFIRMED |
| `0x162`..`0x166` | `kVectorMultiplyComposeU64` … `kVectorExtractHigh32` | 64-bit multiply compose + word extracts | CONFIRMED |
| `0x180`..`0x184` | `kVectorCountLeadingZeros` … `kVectorExtractSignificand` | CLZ / move / popcount / FP field extracts | CONFIRMED |
| `0x19A`..`0x19C` | `kVectorShiftRightLogical` … `kVectorShiftLeftLogical` | vector shifts | CONFIRMED |
| `0x19D`..`0x1A2` | `kVectorMaximumF32` … `kVectorMinimumU32` | min/max (F32/Bf16/U32) | CONFIRMED |

`kVectorMove` (0x181) carries the CSE/remat bits but is an unconditional **Move-exclusion** opcode like `kScalarMove` — the LLO CSE/remat passes never fire on it.

---

## Convert / Pack / Unpack Family

Type conversions are spread across three bands: the `f32→{s32,f8,hf16}` and `{s8,s4,u8,u4}↔bf16` block at `0x05B`..`0x076`, the unpack/round/truncate block at `0x107`..`0x11A`, and pack/compose at `0x125`..`0x127`. These are the opcodes that gained the most across generations.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x05B`..`0x060` | `kScalarConvertF32ToS32WithProbRounding` … `kVectorConvertF32ToS32TowardsZeroPseudo` | F32→S32 (prob-round / towards-zero) | CONFIRMED |
| `0x061`..`0x064` | `kVectorConvertF32ToF8E5M2` / `…E4M3Fn` / `…E4M3B11` / `…ToHf16` | F32→F8 / Hf16 (**F8 added Pufferfish+**) | CONFIRMED |
| `0x066`..`0x06D` | `kVectorConvertS8ToBf16` … `kVectorConvertBf16ToU4` | int↔Bf16 (`s8`/`s4`/`u8`/`u4`; **S4/U4 Pufferfish+**) | CONFIRMED |
| `0x06E`..`0x06F` | `kVectorConvertEXMYToE4M3` / `…ToE5M2` | generic FP8 reformat | CONFIRMED |
| `0x070`..`0x074` | `kVectorConvertF32ToE5M2Stochastic` … `kVectorConvertF32ToHf16Stochastic` | **stochastic-rounding converts (Viperfish+)** | CONFIRMED |
| `0x109`..`0x10F` | `kVectorUnpack` … `kVectorDynamicUnpack` | unpack + B2→B4 / B4→B8 join + EXMY/dynamic | CONFIRMED |
| `0x110`..`0x11A` | `kVectorCeilF32` … `kVectorTruncateBf16` | round-to-int / RTNA / RTNE / truncate | CONFIRMED |
| `0x125`..`0x127` | `kVectorComposeF32` / `kVectorPack` / `kVectorPackEXMY` | compose + pack | CONFIRMED |

---

## EUP Transcendental Family (`0x128`..`0x14D`)

The Extended Unit Pipeline computes transcendentals as a deferred-result pipeline: nine functions (Tanh, Pow2, Reciprocal, Log2, Rsqrt, SigShft, Sinq, Cosq, Erf, plus the standalone `PushErf`) each appear in four forms — `F32`, `Bf16`, `F32AndPop`, `Bf16AndPop`. The `*AndPop` variants both issue the transcendental and pop its result from the EUP FIFO in one opcode; the bare forms only issue (bit0 Push set), with the result collected later by `kVectorEupResult` (0x14E).

| Value band | Members | Form | Confidence |
|---|---|---|---|
| `0x128`..`0x131` | Tanh/Pow2/Reciprocal/Log2/Rsqrt/SigShft/Sinq/Cosq/Erf + `kVectorPushErf` | F32, issue-only | CONFIRMED |
| `0x132`..`0x13A` | same set | Bf16, issue-only | CONFIRMED |
| `0x13B`..`0x144` | same set + `kVectorPushErfAndPop` | F32, issue+pop | CONFIRMED |
| `0x145`..`0x14D` | same set | Bf16, issue+pop | CONFIRMED |

> **NOTE — the EUP family is the cleanest illustration of the Push/Pop property bits.** Every issue-only EUP opcode sets `opcode_info` bit0 (Push); `kVectorEupResult` (0x14E) sets bit1 (Pop, `opcode_info[0x14E] == 0x0202`). The `*AndPop` opcodes fuse both into one instruction. A scheduler that models the EUP FIFO drives directly off these bits.

---

## Cross-Lane, Reduction, and XLU-Result Family

Reductions (`0x0F5`..`0x101`) compute min/max/add/argmin/argmax across lanes or segments in F32 and Bf16; the result-collection opcodes (`0x14E`..`0x155`) pop the various deferred-result FIFOs (EUP, cross-lane, permute, CMEM, transpose). The cross-lane permute/rotate/broadcast primitives live earlier at `0x036`..`0x03B`.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x0F5`..`0x0FC` | `kVectorMinReduceF32` … `kVectorAddSegmentReduceF32` | F32 reductions (whole + segmented + index) | CONFIRMED |
| `0x0FD`..`0x101` | `kVectorMinReduceBf16` … `kVectorMinIndexReduceBf16` | Bf16 reductions | CONFIRMED |
| `0x102`..`0x106` | `kVectorSublaneId` … `kVectorLaneSequenceInterleavedB16` | lane/sublane identity sequences (remat-able constants) | CONFIRMED |
| `0x14E` | `kVectorEupResult` | pop EUP result FIFO (Pop bit) | CONFIRMED |
| `0x14F`..`0x151` | `kVectorXlaneResult` / `kVectorPermuteResult` / `kVectorCmemResult` | pop XLU / permute / CMEM result FIFOs | CONFIRMED |
| `0x152`..`0x153` | `kVectorMatres` / `kVectorMatresAdd` | pop MXU result (plain / accumulate) | CONFIRMED |
| `0x154`..`0x155` | `kVectorTransposeResult` / `kVectorTransposeClear` | pop transpose result / clear transpose FIFO | CONFIRMED |

`kVectorXlaneResult`/`kVectorPermuteResult`/`kVectorTransposeResult` share `GhPerf` cost row 0x1C7. `kVectorTransposeClear` (0x155) is the single opcode whose `opcode_info` word is `0x000E` (Pop + a transpose-clear sub-tag in bits 2/3).

---

## MXU Family — Matmul, Latch, Matprep, Result

The matrix unit pipeline stages a stationary operand (latch), prepares the moving operand (matprep), issues the matmul, and collects the result (matres). The latch/matmul opcodes (`0x08D`..`0x0A5`) are the data-format-dependent band that the FIFO and cost analyses special-case: their result-FIFO behavior and `GhPerf` cost row are computed from the runtime `matmul_data_format` / `latch_mode`, not from a static table read.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x08D`..`0x096` | `kVectorLatchLsf` … `kVectorLatch3Msk` | stationary-operand latch (Lsf / 0..3, masked variants) | CONFIRMED |
| `0x097`..`0x09A` | `kVectorMatprepSubr` … `kVectorMatprepMubrMsk` | moving-operand prep (single/multi broadcast, masked) | CONFIRMED |
| `0x09B`..`0x0A5` | `kVectorMatmul` … `kVectorMatmulLmr` | matmul (Mubr / High / Low / Msk / Packed / Lmr) | CONFIRMED |
| `0x0A6`..`0x0A7` | `kVectorTranspose` / `kVectorTransposeBinary` | XLU transpose (vxpose-mode dispatched) | CONFIRMED |
| `0x0A8`..`0x0AB` | `kVectorDoneWithGains` … `kVectorLoadLmrWithBf16Conversion` | gain handshake + GMR/LMR loads | CONFIRMED |
| `0x152`..`0x153` | `kVectorMatres` / `kVectorMatresAdd` | matmul result collection (also in result family) | CONFIRMED |

> **GOTCHA — the matmul band is special-cased *before* the property-word table read.** `LloInstructionPushesToResultFifo` tests the matmul band (`0x8D`..`0xA5`) via a bitmask and routes to a `matmul_data_format` vtable call; only opcodes outside the band fall through to `opcode_info[op] & 1`. A reimplementer who reads the static Push bit for a matmul opcode gets the wrong FIFO behavior — the matmul's push depends on its data format, decided at the instruction instance, not the opcode.

---

## Load / Store / IAR / RNG Family

Memory access and the per-lane index-address-register (IAR) setup. `LloOpcodeIsVectorLoad` (@ `0x14024900`) and `LloOpcodeIsVectorStore` (@ `0x14024920`) classify the vector forms; the RNG opcodes (`0x03C`..`0x03E`) seed and read the per-lane PRNG used by stochastic conversions.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x001`..`0x004` | `kVectorReadIar` … `kVectorSetIarSublane` | read / set index-address register | CONFIRMED |
| `0x030`..`0x035` | `kVectorLoadSublaneShuffle` … `kVectorCmemLoadAndPop` | vector + CMEM loads | CONFIRMED |
| `0x036`..`0x03B` | `kVectorPermute` … `kVectorBroadcastLane` | cross-lane permute / rotate / combine / broadcast | CONFIRMED |
| `0x03C`..`0x03E` | `kVectorPrng` / `kVectorSetRngSeed` / `kVectorGetRngSeed` | per-lane PRNG | CONFIRMED |
| `0x03F`..`0x046` | `kVectorStore` … `kVectorStoreEvenOddSublanes` | vector + CMEM stores (indexed, masked, shuffle) | CONFIRMED |
| `0x077`..`0x078` | `kScalarLoad` / `kScalarStore` | scalar memory access | CONFIRMED |
| `0x047` | `kVectorNop` | vector no-op (shares `GhPerf` 0x1B4 with `kVectorMaskMove`) | CONFIRMED |

---

## DMA Family (`0x0B3`..`0x0DA`)

The 40 contiguous DMA opcodes are the largest single contiguous block. They enumerate direction (HBM/VMEM/SMEM/CMEM/HIB/IMEM/Host, plus IOVA-addressed host) crossed with the source/destination-register variants (`Vsrc`/`Vdst`/`VsrcVdst`) and a `WithHibUpdate` family. The two terminators `kDmaDone`/`kDmaDoneWait` close a DMA.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x0B3`..`0x0B4` | `kDmaGeneral` / `kDma` | generic DMA forms | CONFIRMED |
| `0x0B5`..`0x0CC` | `kDmaHbmToVmem` … `kDmaSmemToVmem` | direction matrix (HBM/VMEM/SMEM/CMEM/HIB/IMEM) | CONFIRMED |
| `0x0CD`..`0x0D0` | `kDmaHbmToVmemWithHibUpdate` … `…VdstWithHibUpdate` | HBM→VMEM with HIB update | CONFIRMED |
| `0x0D1`..`0x0D8` | `kDmaHbmToHost` … `kDmaSmemToHostIova` | host DMA (direct + IOVA) | CONFIRMED |
| `0x0D9`..`0x0DA` | `kDmaDone` / `kDmaDoneWait` | DMA completion / wait | CONFIRMED |

> **NOTE — the cost model prices DMA by direction-class, not per-opcode.** The 14 HBM/Host-DMA opcodes all collapse to `GhPerf` cost row 0x40 and the 14 VMEM/SMEM-DMA opcodes to row 0x43. The 40 distinct `LloOpcode` values are real and must all exist, but the latency model treats each direction family as one. None of the DMA opcodes set a result-FIFO Push/Pop bit (`opcode_info` LOW byte `0x00`); they are side-effecting via memory, not via the FIFOs.

---

## Predicate / Mask Family

Predicate (single-bit, P-register) and vector-mask (lane-mask, VM-register) ops. The register-file class is `0`/`1` (none/predicate or scalar/mask). The CSE-able + predicate-tag bits (`opcode_info` `0xC0`/`0xD0`/`0xE0`) mark this family.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x0E1` | `kPredicateConstant` | remat-able predicate constant (`opcode_info` `0x00D0`) | CONFIRMED |
| `0x0E5`..`0x0E8` | `kPredicateNegate` … `kPredicateOr` | predicate logic (share `GhPerf` row 0x032) | CONFIRMED |
| `0x0E6` | `kPredicateMove` | predicate copy — **Move-exclusion** opcode | CONFIRMED |
| `0x0E2`..`0x0E3` | `kVectorMaskConstant` / `…Packed` | mask constants | CONFIRMED |
| `0x167`..`0x16A` | `kVectorCompare` … `kVectorAddCarryU16` | mask-producing compares + add-carry | CONFIRMED |
| `0x193`..`0x199` | `kVectorMaskXor` … `kVectorMaskMove` | mask logic / pack-compressed / negate / move | CONFIRMED |
| `0x199` | `kVectorMaskMove` | mask copy — **Move-exclusion** opcode | CONFIRMED |

The four Move-exclusion opcodes — `kScalarMove` (0x17B), `kVectorMove` (0x181), `kVectorMaskMove` (0x199), `kPredicateMove` (0xE6) — form the bitmask `0x40000041` (over base 0x17B) ∪ `{0xE6}` that the CSE/remat/fold passes use to skip moves. See [opcode property word](instbits-master-db.md) for the gate detail.

---

## Constants, Pseudo, and Call Family

Constants (`0x0DB`..`0x0E4`), the Phi/pseudo SSA nodes (`0x0E9`..`0x0F4`), and the call/tuple group. Constants set the remat bit (`opcode_info` `0x50`); Phi nodes (`0x0E9`..`0x0EC`) short-circuit to cost 0 in the latency model.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x02C`..`0x02E` | `kAllocationAddress` / `kParameterAddress` / `kIntToPtr` | address materialization (remat-able) | CONFIRMED |
| `0x0DB`..`0x0E0` | `kScalarConstantU32` … `kVectorConstantF32` | scalar/vector constants (U32/PackedBf16/F32) | CONFIRMED |
| `0x0E4` | `kVectorConstantU64` | 64-bit vector constant | CONFIRMED |
| `0x0E9`..`0x0EC` | `kPredicatePhi` / `kScalarPhi` / `kVectorPhi` / `kVectorMaskPhi` | SSA phi nodes (cost 0) | CONFIRMED |
| `0x0ED`..`0x0F0` | `kTuple` / `kInlinedCall` / `kCall` / `kInlinedCallOperand` | call / tuple structure | CONFIRMED |
| `0x0F1`..`0x0F4` | `kPredicatePseudo` … `kVectorMaskPseudo` | pseudo placeholders per register class | CONFIRMED |
| `0x17C` | `kRelocatableConstant` | link-time relocated constant | CONFIRMED |

---

## BarnaCore (SparseCore) Family (`0x1AC`..`0x1CC`)

The 33 highest opcodes are the BarnaCore (SparseCore) instruction set — embedding scatter/gather, sparse reduce, remote scalar writes, and the BarnaCore-local vector load/store/move. These are the opcodes the [MC-Emitter](mc-emitter.md) actually encodes with real `insertBits` sequences (its populated `InstBits_BarnaCorePxcHwMode` table covers this range), in contrast to the TensorCore opcodes which it returns all-zero.

| Value | Name | Role | Confidence |
|---|---|---|---|
| `0x1AC`..`0x1B3` | `kBarnaCoreScalarWaitDone` … `kBarnaCoreScalarWaitNe` | scalar wait/sync primitives | CONFIRMED |
| `0x1B4`..`0x1B7` | `kBarnaCoreScalarSyncDoneRead` … `…SyncPublicAccessWrite` | sync-flag read/write | CONFIRMED |
| `0x1B8`..`0x1BB` | `kBarnaCoreScalarPop` … `kBarnaCoreScalarFence` | pop / FSM issue / fence | CONFIRMED |
| `0x1BC`..`0x1C8` | `kBarnaCoreRemoteScalarWrite` … `kBarnaCorePfLocalScatterGradients` | remote write / scatter-gather / sparse-reduce (Pf = prefetch variants) | CONFIRMED |
| `0x1C9`..`0x1CC` | `kBarnaCoreVectorLoad` … `kBarnaCoreVectorStore` | BarnaCore vector load/store/move | CONFIRMED |

> **QUIRK — the BarnaCore vector load/store opcodes (457..460) test as *vector* in `LloOpcodeIsVector`, while the BarnaCore scalar/sync opcodes (428..456) test as non-vector.** This is the only place the BarnaCore block straddles the vector/scalar partition, and it matters for register-class selection: `kBarnaCoreVectorLoad`/`…ImmediateOffset`/`…MoveScalarReg`/`…VectorStore` get the vector register file even though their family prefix is "BarnaCore", not "Vector".

---

## Per-Generation Additions

`LloOpcode` is gen-invariant in its *numbering* — the same enum value means the same opcode on every TPU generation — but the *valid subset* grows with each silicon. The compaction encoders' vtable slot counts track this growth (`vxc` Viperfish ≈ 403, `gxc::glc` Ghostlite ≈ 623, `gxc::gfc` ≈ 674 slots), reflecting more legal (opcode × data-format) combinations on later gens. The codenames below are the binary's own internal strings (`Jellyfish`, `Pufferfish`, `Viperfish`, `Ghostlite` all appear verbatim in `libtpu.so`); the newest generation is named only by its hashed family tag `6acc60406` — the marketing names "Trillium"/"Ironwood" have **zero** byte occurrences in this build.

| Generation | Codename | LloOpcode additions | Confidence |
|---|---|---|---|
| TPU v3 | Jellyfish | base set (proto-direct encoding, no Compact encoder) | HIGH |
| TPU v4 | Pufferfish | F8 converts (`0x061`..`0x063`), S4/U4 int↔Bf16 (`0x067`/`0x069`/`0x06B`/`0x06D`), `kCmemFence` (`0x01E`), CMEM DMA/load opcodes | HIGH |
| TPU v5e | Viperfish | stochastic-rounding converts (`0x070`..`0x074`) | HIGH |
| TPU v5p | Ghostlite | `vector_misc` slot ops | MEDIUM |
| TPU7x | `6acc60406` | dual matrix staging (MATPUSH target MSRA/MSRB); newest-gen-only opcodes `kVectorToScalarPush` (0x0A) / `kSyncFlagToScalarPush` (0x0B) map to the highest `GhPerf` rows (0x1DA) only valid on the 476-row grid | MEDIUM |

> **NOTE — the enum is append-and-insert, not append-only, which is why proto and in-memory numbering diverge.** New opcodes are inserted into the in-memory `LloOpcode` at their family's natural position (keeping families contiguous), but appended to the *end* of the `LloOpcodeProto` wire enum (to preserve wire compatibility). The result is the non-monotonic tail of the [LloOpcode↔Proto](llo-opcode-to-proto.md) map: proto value 499 (the newest wire slot) maps to in-memory `0x197` (`kVectorMaskPackCompressedEven`), and proto value 498 maps to `0x084` (`kVectorTraceArg`).

---

## Cross-References

- [LloOpcode↔Proto](llo-opcode-to-proto.md) — the `LloOpcodeToProto` / `ProtoToLloOpcode` wire-value map and the 38 reserved proto gaps.
- [MC-Emitter](mc-emitter.md) — the `getBinaryCodeForInstr` dispatch over the MC opcode space (`LloOpcode` + 499), which encodes only the BarnaCore subset and returns the TensorCore subset all-zero.
- [InstBits DB](instbits-master-db.md) — the `opcode_info` property word, `opcode_info_big` descriptor, and the Move-exclusion / CSE / remat gates keyed on these opcodes.
- [LLO Opcode Table (appendix)](../appendix/llo-opcode-table.md) — the exhaustive 461-row value↔name dump.
