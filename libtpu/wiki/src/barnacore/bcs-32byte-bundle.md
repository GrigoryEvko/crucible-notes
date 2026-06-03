# BCS 32-Byte Bundle

> *All addresses, offsets, and bit positions on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The ELF is **not** stripped; full C++ symbols are present. `.text` VMA equals file offset (`0xe63c000`); all addresses are analysis VMAs. Other versions will differ.*

## Abstract

BarnaCore is the legacy TPU embedding accelerator (chip generations Jellyfish through Pufferfish). Its Pufferfish-generation (`pxc`/`pfc`) instruction word is a **32-byte / 256-bit VLIW bundle**, encoded by the same bit-packing machinery the SparseCore codec uses — a single `BitCopy(dst, dst_bitoff, src, src_bitoff, nbits)` primitive (`@0x1fa0a900`) writing fields at **absolute bundle-bit positions**. There are two bundle personalities sharing the same 32-byte word width: the **Sequencer bundle** (`BarnaCoreSequencerBundle`), a 2-wide dual-scalar control/memory word, and the **Channel bundle** (`BarnaCoreChannelBundle`), a 6-slot vector-datapath word that performs one fully-pipelined embedding-row transform per cycle. Both are produced by `pufferfish::isa::EncoderPfBarnaCore{Sequencer,Channel}`, whose `BundleSizeBytes()` accessors both `return 32` (`@0x1d229220`, `@0x1d22bb00` — confirmed in decompile).

The reverse-engineering technique mirrors the SparseCore SCS/TEC work: the per-personality `BarnaCore{Sequencer,Channel}CodecBase::Encode` dispatcher holds the output buffer `Span` (pointer + length) in fixed registers and passes it **unchanged** to every per-slot `Encoder::Encode`, so each slot writes at bundle-relative — i.e. absolute — bit offsets. Each field's `(dst_bitoff, nbits)` pair comes from the `mov esi,IMM` / `mov r8d,IMM` immediates before its `BitCopy` call; the per-op 6-bit hardware opcode **value** comes from the `mov QWORD PTR[rbp-0x18],N` constant written into the opcode field. The codec template name is the structural ground truth: `BarnaCoreChannelCodecBase<BarnaCoreChannelBundle, BarnacoreChannelPredication, …VectorExtendedResult{Decoder,Encoder}, …VectorStore…, …VectorLoad…, …VectorAlu0…, …VectorAlu1…, …Scalar…>::Encode` (`@0x1d22c560`) enumerates the six slots in dispatch order.

This page documents four artifacts a reimplementer must reproduce: (1) the **Sequencer bundle bit-layout** — two 27-bit scalar slots plus four 16-bit immediates; (2) the **Channel bundle bit-layout** — six slots plus four immediates; (3) the **`BcsMetadataAccessor` descriptor format** — a 14-type flat SMEM-word table indexed by `BcsProgramMetadataType`; and (4) the **`MigrateInstruction` ordinal map** — the `Alu0`↔`Alu1` vector-lane-swap set that pins which 54 ops are lane-agnostic and which 7 are lane-locked. The per-op opcode rosters in proto-oneof order, the embedding-lowering datapath, and the SparseCore bundle this mirrors are owned by sibling pages (see [Cross-References](#cross-references)).

For reimplementation, the contract is:

- **The bit-packing model**: every field is `BitCopy(buf, absolute_bitoff, src, src_bitoff, nbits)`; the dispatcher reuses one `Span` across all slots, so bit offsets are bundle-absolute, not slot-relative.
- **The two 32-byte bundle layouts**: Sequencer (bits 3..132 used) and Channel (bits 12..238 used), each with its slot/opcode/predication/immediate placement.
- **The 27-bit scalar-slot template** shared by both Sequencer pipes (and bit-identical to SparseCore SCS): `operand-y / operand-x / dest / OPCODE / predication`.
- **The `BcsMetadataAccessor`**: per-type SMEM-word base/count arrays + per-field offset enums; a field read is `Sld(SmemWordAddress(base(type) + offset_enum))`.
- **The `MigrateInstruction` map**: 54 ops × 2 directions = 108 templates that serialize/deserialize an op between ALU lanes; 7 ops have no template (lane-locked).

| | |
|---|---|
| **Bundle size** | 32 bytes / 256 bits (both personalities) |
| **Size accessors** | `EncoderPfBarnaCoreSequencer::BundleSizeBytes` `@0x1d229220` → 32; `…Channel::BundleSizeBytes` `@0x1d22bb00` → 32 |
| **Bit packer** | `BitCopy(void*, int dst_bitoff, void const*, int src_bitoff, int nbits)` `@0x1fa0a900` |
| **Sequencer codec** | `BarnaCoreSequencerCodecBase<…>::Encode` `@0x1d229780` (2 scalar slots) |
| **Channel codec** | `BarnaCoreChannelCodecBase<…>::Encode` `@0x1d22c560` (6 slots) |
| **Scalar slot encoders** | Scalar0 `@0x1ee51ec0` (base bit 106); Scalar1 `@0x1ee69000` (base bit 79) |
| **Channel slot encoders** | ChannelScalar `@0x1e87e6a0`, VectorAlu0 `@0x1e8927c0`, VectorAlu1 `@0x1e8b4ec0`, VectorStore `@0x1e8c5640`, VectorLoad `@0x1e8c4900`, VectorExtendedResult `@0x1e8c3c60` |
| **Metadata accessor** | `BcsMetadataAccessor` ctor `@0xf9d8b80`; `MetadataSmemWordBase` `@0xf9d8ba0`; `MetadataSmemWordCount` `@0xf9d8c40` |
| **Migrate map** | `PufferfishBarnaCoreChannelEmitter::MigrateInstruction<Alu0_X, Alu1_X>` — 108 instances (54 ops × 2) `@0x140d17c0..0x140d6c00` |
| **DMA buffer width** | `TpuTypedHostDmaBuffer<unsigned char, 32>` (the `Li32E` template arg on the channel program encoder) — 32-byte alignment |

---

## 1. The Bundle Encoding Model

### Purpose

Both BarnaCore bundles are flat 256-bit words whose fields are written by a shared bit-copy primitive at absolute positions. Understanding the encoding model is the prerequisite for reading every offset table below: a reimplementer who treats the per-slot bit offsets as slot-relative will mis-place every field, because the dispatcher does **not** rebase the buffer per slot.

### Entry Point

```text
EncoderPfBarnaCoreChannel::EncodeBundle (0x1d22b780)
  └─ EncodeBundleInternal (0x1e87d580)
       └─ BarnaCoreChannelCodecBase<…>::Encode (0x1d22c560)   ── buf.ptr in %r14, buf.len in %rbx
            ├─ VectorExtendedResultEncoder::Encode (0x1e8c3c60)   ── same Span
            ├─ VectorStoreEncoder::Encode          (0x1e8c5640)   ── same Span
            ├─ VectorLoadEncoder::Encode           (0x1e8c4900)   ── same Span
            ├─ VectorAlu0Encoder::Encode           (0x1e8927c0)   ── same Span (called per oneof form)
            ├─ VectorAlu1Encoder::Encode           (0x1e8b4ec0)   ── same Span
            └─ ChannelScalarEncoder::Encode        (0x1e87e6a0)   ── same Span
                 └─ (each field) BitCopy (0x1fa0a900)
```

The Sequencer path is identical in shape: `EncoderPfBarnaCoreSequencer::EncodeBundle (0x1d228ea0)` → `BarnaCoreSequencerCodecBase<…>::Encode (0x1d229780)` → `{Scalar0,Scalar1}Encoder::Encode`.

### Algorithm

```c
// Models BarnaCoreChannelCodecBase<…>::Encode @0x1d22c560.
// a3 = buf.ptr, a4 = buf.len  (absl::Span<unsigned char>).
function ChannelCodec_Encode(bundle, buf_ptr, buf_len):       // %r14 = buf_ptr, %rbx = buf_len
    // The Span is NEVER rebased: every slot encoder receives (buf_ptr, buf_len)
    // unchanged, so each writes at bundle-absolute bit offsets.
    EncodeSlot(bundle.extended_result, buf_ptr, buf_len)       // 0x1e8c3c60
    EncodeSlot(bundle.store,           buf_ptr, buf_len)       // 0x1e8c5640
    EncodeSlot(bundle.load,            buf_ptr, buf_len)       // 0x1e8c4900
    EncodeSlot(bundle.alu0,            buf_ptr, buf_len)       // 0x1e8927c0
    EncodeSlot(bundle.alu1,            buf_ptr, buf_len)       // 0x1e8b4ec0
    EncodeSlot(bundle.scalar,          buf_ptr, buf_len)       // 0x1e87e6a0
    // tail: validation logging only — no separate predication / check-byte write;
    // predication is emitted inline by each slot encoder.

function EncodeField(buf, op):                                 // per-op helper
    BitCopy(buf, DST_BITOFF, &src, SRC_BITOFF, NBITS)          // 0x1fa0a900
    //          ^esi imm      ^      ^ecx imm   ^r8d imm
    // OPCODE value comes from `mov QWORD PTR[rbp-0x18], N` before the opcode BitCopy.
```

> **QUIRK —** the bit offsets in every table below are **bundle-absolute**, recovered from the `mov esi,IMM` immediate feeding `BitCopy`. They are *not* slot-relative. The slot-relative template (operand-y@+0 … OPCODE@+16 … predication@+22) is the same 27-bit shape for both scalar pipes; the absolute placement differs only by the slot base (Scalar1 at 79, Scalar0 at 106).

> **NOTE —** the codec dispatcher's tail is pure `absl::log_internal` validation, not a framing-byte write. `PufferfishCodecMetadata::BundleCheckByte`/`BundleSizeBytes` (the TC-style `0x55` check byte / `0x33`=51 framing) belong to `TpuSequencerType=0`; the BarnaCore encoders route through `TpuSequencerType=1` and their own `BundleSizeBytes()` returns 32 directly with no check byte.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `BitCopy` | `0x1fa0a900` | Bit-granular field writer; `(dst, dst_bitoff, src, src_bitoff, nbits)` | CERTAIN |
| `BarnaCoreSequencerCodecBase<…>::Encode` | `0x1d229780` | Sequencer dispatcher; reuses one `Span` across both scalar slots | CERTAIN |
| `BarnaCoreChannelCodecBase<…>::Encode` | `0x1d22c560` | Channel dispatcher; reuses one `Span` across 6 slots | CERTAIN |
| `EncoderPfBarnaCoreSequencer::BundleSizeBytes` | `0x1d229220` | `return 32` | CERTAIN |
| `EncoderPfBarnaCoreChannel::BundleSizeBytes` | `0x1d22bb00` | `return 32` | CERTAIN |

---

## 2. The Sequencer Bundle (`InstBits_BarnaCorePxcHwMode`)

### Purpose

The Sequencer bundle is a 2-wide dual-scalar VLIW carrying the control-flow and SMEM-access ISA: branches, calls, fences, sync/done, DMA descriptors, SMEM load/store, and the scalar integer/float ALU. Two 27-bit scalar slots (`Scalar0`, `Scalar1`) stack 27 bits apart and share a region of four 16-bit immediate slots in the bundle's low bits. The symbol `InstBits_BarnaCorePxcHwMode` (`@0x33931f0`, a static `.rodata` table inside the LLVM backend's `TPUMCCodeEmitter::getBinaryCodeForInstr` — the per-instruction bit-pattern table for this hardware-mode bundle) is the independent compiler-side ground truth for the layout decoded below from the proto encoders.

### Encoding

The bundle uses **only bits 3..132** of its 256 bits (the lower ~17 bytes). Bits 0..2 and 133..255 are reserved/unwritten — confirmed by sweeping every scalar op encoder and both dispatcher heads; the maximum written bit is 132. The upper 16 bytes are padding, present so the Sequencer and Channel bundles share the same physical word width (and therefore the same 32-byte DMA transfer unit), even though the Sequencer fills only the lower half.

Each scalar slot is the **27-bit template** (slot-relative): `operand-y @+0/w5`, `operand-x @+5/w6` (a `ScalarY`-style 6-bit selector that names either a scalar register or one of the four immediate slots), `dest @+11/w5`, `OPCODE @+16/w6`, `predication @+22/w5`. This is bit-identical to the SparseCore SCS scalar template; the only delta is that BarnaCore predication is a single 5-bit field (`BarnaCoreSequencerScalar{0,1}PredicationField`, both confirmed in symbols) rather than the SCS multi-field quad.

```text
Sequencer bundle (32 B / 256 bits; only bits 3..132 written)

bit:  15      31      47      63        79 ............ 105   106 ........... 132   133 .......... 255
     +-------+-------+-------+-------+   +------------------+  +------------------+  +---------------+
     | Imm0  | Imm1  | Imm2  | Imm3  |   |   Scalar1 slot   |  |   Scalar0 slot   |  |   RESERVED    |
     | w16   | w16   | w16   | w16   |   |   27 bits @79    |  |   27 bits @106   |  |  (unwritten)  |
     +-------+-------+-------+-------+   +------------------+  +------------------+  +---------------+
                                          opcode@95  pred@101    opcode@122 pred@128
```

#### Scalar0 slot (base 106; opcode value = §4 Table)

| Field | Base bit | Width | Slot offset | Role |
|---|---|---|---|---|
| `SyField` (operand y) | 106 | 5 | +0 | scalar-reg selector |
| `SxField` (operand x) | 111 | 6 | +5 | scalar-reg OR immediate-slot (`ScalarY`-style) |
| `DestField` | 117 | 5 | +11 | result scalar-reg selector |
| `OPCODE` | 122 | 6 | +16 | 6-bit hardware opcode |
| `PredicationField` | 128 | 5 | +22 | `BarnaCoreSequencerScalar0PredicationField` |

#### Scalar1 slot (base 79; opcode value = §4 Table)

| Field | Base bit | Width | Slot offset | Role |
|---|---|---|---|---|
| operand y | 79 | 5 | +0 | scalar-reg selector |
| operand x | 84 | 6 | +5 | scalar-reg OR immediate (`ScalarY`-style) |
| dest | 90 | 5 | +11 | result scalar-reg selector |
| `OPCODE` | 95 | 6 | +16 | 6-bit hardware opcode |
| `PredicationField` | 101 | 5 | +22 | `BarnaCoreSequencerScalar1PredicationField` |

#### Shared immediate slots (both scalar pipes)

| Field | Base bit | Width |
|---|---|---|
| `Imm0Field` | 15 | 16 |
| `Imm1Field` | 31 | 16 |
| `Imm2Field` | 47 | 16 |
| `Imm3Field` | 63 | 16 |

> **GOTCHA —** DMA ops are a **oneof-of-lane**: `ScalarDmaSimple`/`ScalarSingleStridedDma`/`ScalarGeneralDma` do not fit a single scalar slot. Their opcode rides the Scalar0 opcode field (@122), but the descriptor (source/dest address, core id, memory id, length, dest sync flag, dma type — names from the `ScalarDmaSimple{Source,Dest}{Address,CoreId,MemoryId}/Length/DestSyncFlag/DmaType0` field accessors) spills through the Scalar1 region, the four immediate slots, and extra low bits. `ScalarGeneralDma` (`@0x1ee55120`) is the widest, spanning bits 0..127 (the entire lower 16 bytes). A reimplementer must treat a DMA bundle as consuming the entire dual-scalar capacity, not one lane.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `BarnaCoreSequencerScalar0Encoder::Encode` | `0x1ee51ec0` | Scalar0 slot encoder; base 106, jump table `@0xb84559c` (bound ≤0x3e) | CERTAIN |
| `BarnaCoreSequencerScalar1Encoder::Encode` | `0x1ee69000` | Scalar1 slot encoder; base 79, jump table `@0xb845698` (bound ≤0x3c) | CERTAIN |
| `ScalarIntAdd` (Scalar0) | `0x1ee55ee0` | op=0x20; Sy@106/Sx@111/Dest@117 | HIGH |
| `ScalarGeneralDma` (Scalar0) | `0x1ee55120` | widest op; spans bits 0..127 | HIGH |
| `ScalarStoreSmemAbsolute` (Scalar1) | `0x1ee6ace0` | op=0x6 | HIGH |

---

## 3. The Channel Bundle

### Purpose

The Channel bundle is the 6-slot vector datapath. In one cycle it performs a fully pipelined embedding-row transform: advance the feature-length loop, run two vector-ALU ops on the two lanes, store the previous row, load the next row, and drain an EUP (transcendental unit) result. The six slots are placed at ascending absolute bit positions; the codec template (`@0x1d22c560`) lists them in the order ExtendedResult / Store / Load / Alu0 / Alu1 / Scalar.

### Encoding

The bundle fills **bits 12..238** of its 256-bit frame; bits 0..11 and 239..255 are reserved (max written bit = 238, where the fourth immediate ends). The two vector-ALU lanes are 33 bits apart (a 31-bit slot plus a 2-bit inter-lane gap). Every `VectorAlu` encoder also writes a shared 2-bit field group at bits 35/37/39 — the vector lane/mask header at the vector-region base (role inferred from the `VxField`/`YSrc` accessor neighborhood; bit positions exact).

```text
Channel bundle (32 B / 256 bits; bits 12..238 written)

12        35       62 67    92 95 100    125  126 128   147 149  167 172  175    191    207    223  238
+----------+--------+--------+----+--------+----+--------+----+------+------+----+-------+------+------+------+
| Channel  | (2-bit |VectorAlu0| | VectorAlu1| |VectorStore| VectorLoad |VExtRes| Imm0 | Imm1 | Imm2 | Imm3 |
| Scalar   | lane   | opcode@67 | | opcode@100| | pred@128  | pred@149   |pred@167| w16 | w16  | w16  | w16  |
| loop ctl | hdr@35 | pred@62   | | pred@95   | | form@126  | form@147   |       |
+----------+--------+----------+-+-----------+-+-----------+------------+--------+------+------+------+------+
```

| Slot | Encoder | Bit extent | Opcode / pred bits | Role |
|---|---|---|---|---|
| `ChannelScalar` | `0x1e87e6a0` | 12 .. 59 | type@12/w2; count@16/w8 | feature-length loop control (`LoopStart`/`NormalOp`/`OpBranch`) |
| `VectorAlu0` | `0x1e8927c0` | 35 .. 92 | opcode@67/w6; pred@62/w5 | vector ALU lane 0 (carries `VectorFloatMul`; 54 shared ops) |
| `VectorAlu1` | `0x1e8b4ec0` | 35 .. 125 | opcode@100/w6; pred@95/w5 | vector ALU lane 1 (carries `VectorFloatAdd`/`Sub` + 4 shifts) |
| `VectorStore` | `0x1e8c5640` | 126 .. 147 | form@126/w2; pred@128/w5 | embedding-row store (`Base`/`Source`/`FeatureLen`/`Stride`) |
| `VectorLoad` | `0x1e8c4900` | 147 .. 167 | form@147/w2; pred@149/w5 | embedding-row load (`Base`/`Dest`/`FeatureLen`) |
| `VectorExtendedResult` | `0x1e8c3c60` | 167 .. 174 | pred@167/w5; @172/w1, @173/w2 | EUP-result drain |
| `ScalarImmediates` | (in op enc.) | 175 .. 238 | 4 × 16-bit @175/191/207/223 | shared literals (`Imm0..Imm3`) |

#### The VectorAlu template

Slot-relative to the opcode field: `OPCODE w6` at the base, four 5-bit VREG operand selectors above it (`Dest`/`Vx`/`YSrc`/`YSrcVreg` — names from the `VectorFloatMul{Dest,Vx,YSrc,YSrcVreg}Field` accessors, all confirmed in symbols), and the 5-bit predication field below it. `VectorFloatMul` also declares `Imm0..Imm5Field` (confirmed).

#### The ChannelScalar loop controller

The `ChannelScalar` slot is the per-cycle feature-length loop sequencer. Its fields (all confirmed as `BarnaCoreChannelScalar*Field` symbols): `LoopStartLoopInstructionCount`, `LoopStartPipelineDepth`, `OpBranch{BranchTargetPc,BranchType,BranchPred}`, `OpShift{,PipelineStage}`, `OpPush`, `PredFeatureId`, `ProgEnd`, and a family of `AddLoopIndexTo{ShiftAmount,VldDst,VstSrc,V0Dst,V0X,V0YReg,V0VselectMask,V1Dst,V1X,V1YReg,V1VselectMask}` selectors that add the loop index to per-lane operand registers.

> **NOTE —** the decoder declares six channel immediate fields (`Imm0..Imm5Field`, e.g. `VectorFloatMulImm4Field`/`Imm5Field` confirmed in symbols), but every sampled op encoder writes only the four at 175/191/207/223 (4 × 16-bit fills bits 175..238, exactly the bundle's written maximum). `Imm4`/`Imm5` are **declared-but-unwritten** in v0.0.40 — reserved or op-form-specific (LOW confidence on their placement, since the encode side never sets them).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `BarnaCoreChannelVectorAlu0Encoder::Encode` | `0x1e8927c0` | Alu0 slot; opcode@67/w6, pred@62/w5; jump table `@0xb834e8c` (bound ≤0x2e) | CERTAIN |
| `BarnaCoreChannelVectorAlu1Encoder::Encode` | `0x1e8b4ec0` | Alu1 slot; opcode@100/w6, pred@95/w5 | CERTAIN |
| `BarnaCoreChannelScalarEncoder::Encode` | `0x1e87e6a0` | loop controller; type@12/w2, count@16/w8 | CERTAIN |
| `VectorFloatMul` (Alu0) | `0x1e894420` | opcode value 0x7 | HIGH |
| `VectorTanh` (Alu0) | `0x1e89edc0` | opcode value 0x33 | HIGH |
| `VectorReciprocal` (Alu0) | `0x1e89ee40` | opcode value 0x34 | HIGH |

---

## 4. Hardware Opcode Values (6-bit field)

The 6-bit opcode field is written with a literal value (`mov QWORD PTR[rbp-0x18],N`) per op. These are the **hardware** opcode values, a separate ordering from the proto-oneof ordinals; the two orderings are independent and both now pinned.

### Scalar0 / Scalar1 dispatch dimensions

The scalar pipes share a common ALU tail at identical values and split on pipe-only ops in the gaps:

| Axis | Values | Source |
|---|---|---|
| Shared header | `0x00..0x03` = Noop/Sync(0x1)/Pop(0x2)/Delay(0x3) | both jump tables |
| Shared ALU tail | IntAdd=0x20, IntSub=0x21, And=0x22, Or=0x23, Xor=0x24, Move=0x2e, IntEqual=0x30 — **same value in both pipes** | per-op `mov QWORD` |
| Scalar0-only | Branch{Abs,Rel,Reg}=0x8/0x9/0xa, Call=0xc.., Fence=0x10, Dma=0x12, IssueFsm=0x15, ReadRegs=0x1d, ConvI2F=0x1e, FloatMul=0x27, UintMul=0x28, FloatMax=0x29, IsInfOrNan=0x3e | Scalar0 encoders |
| Scalar1-only | LoadSmem=0x4, LoadSmemOffset=0x5, StoreSmemAbsolute=0x6, ReadDone=0x16, WriteDone=0x17, ReadPublicAccess=0x18, WritePublicAccess=0x19, FloatAdd=0x25, FloatSub=0x26 | Scalar1 encoders |

All seven `Sync*` ops share opcode `0x1`, distinguished by a sub-form field (≤0x3e fits the 6-bit field).

### Channel VectorAlu0

| Opcode | Op | Opcode | Op |
|---|---|---|---|
| 0x03 | `VectorOr` | 0x20 | `VectorIntEqual` |
| 0x04 | `VectorXor` | 0x27 | `CreateSublaneMask` |
| 0x07 | `VectorFloatMul` | 0x2f | `CreateLaneMask` |
| 0x08 | `VectorFloatMax` | **0x30** | **`VectorReciprocalSquareRoot`** |
| 0x09 | `VectorFloatMin` | **0x31** | **`VectorPow2`** |
| 0x18 | `VectorLaneId` | **0x32** | **`VectorLog2`** |
| 0x1e | `VectorRelux` | **0x33** | **`VectorTanh`** |
| 0x1f | `VectorMove` | **0x34** | **`VectorReciprocal`** |
| | | 0x35 | `MoveDataUnchanged` |

> **QUIRK —** the EUP transcendental block is **contiguous** in the hardware opcode space at `0x30..0x34` (rsqrt → pow2 → log2 → tanh → recip), exactly as it is contiguous in the proto oneof. This is independent confirmation — from the 6-bit opcode value, not the proto ordinal — of the within-block EUP order. `VectorRelux` (0x1e) sits in the early ALU range, *separate* from the transcendental run, mirroring its role as an embedding activation rather than a transcendental.

---

## 5. The `BcsMetadataAccessor` Descriptor Format

### Purpose

Per-op embedding metadata (the programming surface a gather/scatter op needs: where its rows live, how big the buffers are, which partition column, which HBM address) is not in the bundle — it lives in SMEM as a flat per-type word array. `BcsMetadataAccessor` is the runtime reader. This is the BarnaCore analog of the SparseCore TAC descriptor table: the descriptor lives in SMEM (programmed by the driver's `SetBarnaCoreFeature*` path), the emitter loads it into scalar registers, and the address math runs on the shared scalar ALU.

### Encoding

The accessor object holds two driver-populated `int32` arrays indexed by `BcsProgramMetadataType` (confirmed enum symbol): the SMEM-word **base** at `this[+0x10]` (length at `[+0x18]`) and the word **count** at `this[+0x28]` (length at `[+0x30]`). `MetadataSmemWordBase` (`@0xf9d8ba0`) and `MetadataSmemWordCount` (`@0xf9d8c40`) are the bounds-checked lookups. A descriptor field read is:

```c
// field(type, offset_enum) =
value = Sld( SmemWordAddress( MetadataSmemWordBase(type) + offset_enum ) );
//            SmemWordAddress @0xf9d9720 ; LloRegionBuilder::Sld @0x1d516a20
```

The type number for each accessor comes from the `mov esi,N; call MetadataSmemWordBase` site inside the accessor body. The 14 types (mangled accessor symbols all confirmed in `…_names.json`):

| Type | `BcsProgramMetadataType` | Accessor method | Address | Offset enum |
|---|---|---|---|---|
| 0x1 | DedupTransfer | `LoadDedupTransferMetadata` | `0xf9d90e0` | `BcsDedupTransferMetadataOffset` |
| 0x2 | PassHeader | `LoadPassHeaderMetadata` | `0xf9d8d40` | `BcsPassHeaderMetadataOffset` {2,3,4,5,6,..} |
| 0x3 | PayloadLocation | `LoadPayloadLocationMetadata` | `0xf9d8da0` | `BcsPayloadLocationMetadataOffset` (per-id) |
| 0x4 | LocalBuffer | `LoadLocalBufferSize` | `0xf9d8e40` | — |
| 0x5 | RemoteBufferSize | `LoadRemoteBufferSize` | `0xf9d8f80` | — |
| 0x6 | BmemWordAddress | `LoadBmemWordAddressFromMetadata` | `0xf9d9140` | — |
| 0x7 | RemoteBufferOffset | `LoadRemoteBufferOffset` | `0xf9d8fe0` | — |
| 0x8 | PartitionColumn | `LoadPartitionColumn` | `0xf9d92e0` | (base+0, no offset) |
| 0x9 | ScatterGroup | `LoadScatterGroup` | `0xf9d9340` | — |
| 0xa | BarnaCoreLocation | `LoadBarnaCoreLocation` | `0xf9d93a0` | — |
| 0xb | TensorCoreLocation | `LoadTensorCoreLocation` | `0xf9d9460` | — |
| 0xc | AbsoluteHbm | `GetBarnaCoreAbsoluteHbmAddress` | `0xf9d9520` | — |
| 0xd | TensorCoreDmaAddr | `GetBarnaCoreAbsoluteHbmAddressForTensorCoreDma` | `0xf9d95c0` | — |
| 0xe | BackwardPassSlotSelector | `Load/StoreBackwardPassSlotSelector` | `0xf9d9660` / `0xf9d96c0` | — |

`LoadCommandMetadata` (`@0xf9d8ce0`) takes the type as a runtime arg (`BcsCommandMetadataOffset` enum, confirmed). The constructor is `BcsMetadataAccessor(TpuTopology const*, int, int, absl::Span<int const>)` (`@0xf9d8b80`).

> **NOTE —** the symbol table also carries `LoadTensorCoreBufferSelector` and `LoadTpuBatchCount` accessors not enumerated above — additional metadata readers on the same SMEM-word model (their type numbers were not separately decoded; HIGH confidence they follow the same `base(type)+offset` pattern, LOW on their specific type indices).

---

## 6. The `MigrateInstruction` Ordinal Map

### Purpose

`PufferfishBarnaCoreChannelEmitter::MigrateInstruction<Alu0_X, Alu1_X>` (and the reverse `<Alu1_X, Alu0_X>`) is the vector-ALU **lane-swap** mechanism. The scheduler uses it to move a vector op between the two ALU lanes when one is free. The map is the runtime, message-level proof of the Alu0/Alu1 lane asymmetry: a `Migrate` template exists exactly for the lane-agnostic ops, and is absent for the lane-locked ops.

### Encoding

Each `MigrateInstruction` body is a proto round-trip: `SerializeToString` (`@0x210580c0`) the source-lane message, then `ParseFromString` (`@0x21057460`) into the destination-lane message type. The two lane types share their field layout, so the round-trip is lossless. The op message types live in `asic_sw::deepsea::pxc::pfc::isa::BarnaCoreChannelVectorAlu{0,1}_<Op>`.

There are **108 instances = 54 ops × 2 directions** (`@0x140d1400..0x140d6c00`; `<Alu0_Noop,Alu1_Noop>` `@0x140d17c0`, `<Alu0_VectorTanh,Alu1_VectorTanh>` `@0x140d4c80`). The 54 migratable ops (all confirmed as `BarnaCoreChannelVectorAlu0_<Op>` ↔ `Alu1_<Op>` template pairs in symbols):

```text
Noop, VectorIntAdd, VectorIntSub, VectorAnd, VectorOr, VectorXor,
VectorFloatMax, VectorFloatMin, VectorLaneId, VectorSublaneCircularRotateDown,
VectorRelux, VectorMove, VectorClampSymmetric, VectorPopCount,
VectorCountLeadingZeros, VectorSelectVmsk0..7 (8), VectorPackAsHalfFloats{Interleaved,Compressed},
VectorUnpackHalfFloats{Upper,Lower}, VectorConvert{IntToFloat,FloatToInt},
VectorExtractExponent, VectorExtractSignificand, VectorComposeFloat,
VectorInt{Equal,NotEqual,Greater,GreaterEqual,Less,LessEqual}, VectorIntAddCarryOut,
CreateSublaneMask, VectorFloat{Equal,NotEqual,Greater,GreaterEqual,Less,LessEqual},
VectorFloatIsInfOrNan, CreateLaneMask, VectorReciprocalSquareRoot, VectorPow2,
VectorLog2, VectorTanh, VectorReciprocal, MoveDataUnchanged
```

The 7 lane-locked ops have **no** `Migrate` template — the scheduler cannot move them:

| Op | Locked to | Reason |
|---|---|---|
| `VectorFloatMul` | Alu0 | float-multiply lane |
| `VectorFloatAdd` | Alu1 | float-add lane |
| `VectorFloatSub` | Alu1 | float-add lane |
| `VectorLogicalShiftLeft` | Alu1 | shift unit |
| `VectorLogicalShiftRight` | Alu1 | shift unit |
| `VectorArithmeticShiftRight` | Alu1 | shift unit |
| `VectorRoundingArithmeticShiftRight` | Alu1 | shift unit |

> **QUIRK —** the asymmetry is real hardware structure: float-**mul** is Alu0-only, float-**add/sub** and all four shifts are Alu1-only. A scheduler that assumes the two lanes are interchangeable will mis-place these seven ops. The presence/absence of a `Migrate` template *is* the lane-capability table.

> **NOTE —** there are **zero** `Scalar0`↔`Scalar1` `Migrate` instances. Scalar dual-issue is resolved statically at schedule time by the `FindFreeScalarSlot<Scalar0_X, Scalar1_Y>` template instantiations, never by a runtime message migration. (The whole-binary `MigrateInstruction<…>` template-instance count is 222 — the 108 BarnaCore-channel pairs above plus 114 from `PufferfishTensorCoreEmitter`; the BarnaCore-channel set is the 108 in the address range above.)

---

## Related Components

| Name | Relationship |
|---|---|
| `BarnaCoreSequencerCodecBase` / `BarnaCoreChannelCodecBase` | The two per-personality bundle dispatchers; reuse one `Span` across slots |
| `EncoderPfBarnaCore{Sequencer,Channel}` | The `pufferfish::isa` encoders; `BundleSizeBytes() == 32` |
| `BitCopy` (`@0x1fa0a900`) | The shared SparseCore/BarnaCore bit packer |
| `PufferfishBarnaCoreChannelEmitter` | Owns `MigrateInstruction`; the lane-swap scheduler hook |

## Cross-References

- [Overview](overview.md) — BarnaCore, the legacy embedding accelerator: where this bundle sits in the pipeline
- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the per-op control+memory ISA rosters whose opcodes §4 gives the hardware values for
- [Merged-ALU Bit Layout](merged-alu.md) — `VectorResultDestination` / `BaseAddressEncoding`, the vector-ALU operand selectors referenced in §3
- [JF/DF 16-Byte Address-Handler Bundle](jf-df-address-handler-bundle.md) — the legacy (non-Pufferfish) BarnaCore bundle; a separate 16-byte word filled by direct struct write, not `BitCopy`
- [Index](../index.md) — Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4)
