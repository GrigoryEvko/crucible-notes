# BCS Scalar0/Scalar1 ISA

> *Every op-class name, proto-oneof ordinal, pipe binding, and LLO opcode byte on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the `asic_sw::deepsea::pxc::pfc::isa::BarnaCoreSequencerScalar{0,1}_<Op>` vtable/typeinfo set, the proto `TcParseTableBase` oneof-aux arrays, the `FindFreeScalarSlot<Scalar0_X,Scalar1_X>` template instantiations, and the `LloInstruction::CreateBarnaCore*` ctor opcode constants. Other versions differ.*

## Abstract

BarnaCore is Google's legacy embedding accelerator (v2–v4: Jellyfish, Dragonfish, Pufferfish), the predecessor that SparseCore replaced. Its compute heart in the Pufferfish generation is the **BCS** (BarnaCore Sequencer): a **2-wide dual-scalar VLIW** whose bundle (`BarnaCoreSequencerBundle`) carries exactly two scalar slots, `scalar_0` and `scalar_1`. The two slots are *not* interchangeable — they implement an **asymmetric op split** of a shared scalar ISA, the direct structural ancestor of the SparseCore SCS dual scalar-ALU lanes documented in [SCS Scalar Opcode Enumeration](../sparsecore/scalar-opcode-enum.md). This page enumerates that ISA op-by-op in proto-oneof (wire opcode) order, then traces how the BCS scalar ALU plus a small DMA/sync primitive set absorbs the entire high-level embedding-op surface: the 20 LogFatal-priced gather/scatter/reduce ops lower, through `LloRegionBuilder::Bc<Op>` builders and `barna_core::BcsLloEmitter`, into 13 priced BCS primitives.

The closest familiar analog is a CISC ISA recovered not from a manual but from a protobuf schema: each scalar op is a oneof submessage of `BarnaCoreSequencerScalar0`/`Scalar1`, so the **opcode is the proto field number** and the opcode *ordering* is the oneof declaration order. There is no opcode-name string table; the roster is reconstructed by reloc-resolving each oneof submessage's `_table_` aux pointer and reverse-mapping it to a class name via `c++filt`. The pipe split is reconstructed independently from the `FindFreeScalarSlot<Scalar0_X, Scalar1_Y>` template instantiation set (a `void` second arg marks a pipe-only op). The embedding lowering is reconstructed from the disassembled `BcsLloEmitter` bodies and the `mov edi,0x1XX` opcode constant before each `LloInstruction::CreateBarnaCore<Op>` ctor.

For reimplementation, the contract is:

- **The op count.** The RTTI typeinfo count is **61 Scalar0 / 59 Scalar1** = 120 typeinfos. Subtracting the abstract base, the `ResourceUsageEntry_DoNotUse` proto-map entry, and the codec-template typeinfo from each pipe leaves **58 Scalar0 + 56 Scalar1 = 114 concrete ISA ops**. The 114 are enumerated in full below.
- **The shared header + shared ALU tail are identical across both pipes; the pipes diverge only in the middle band.** Ordinals 0..12 (Noop, Halt, HostInterrupt, Trace, the 6 `Sync*` waits at ords 4..9, SyncAdd, PopHmf, Delay) and the ALU/compare tail (Convert, IntAdd/Sub, bitwise, FloatMax/Min, shifts, Move, Clz, the 6 int compares, AddCarryOut, PredicateOr, the 6 float compares, IsInfOrNan) are present on both. The split is in the control/DMA/SMEM band.
- **The asymmetric split.** Scalar0 = **control + DMA + multiply** pipe (3 Branch + 3 Call + 3 Dma + FloatMul/UintMul); Scalar1 = **load-store + sync-completion + add/sub** pipe (LoadSmem/LoadSmemOffset/StoreSmemAbsolute + ReadDone/WriteDone/ReadPublicAccess/WritePublicAccess + FloatAdd/FloatSub). **47 ops are present in both pipes** (dual-issuable by RTTI), of which **38 route through `FindFreeScalarSlot<S0_X, S1_X>`** and 9 more (the 5 non-equal float compares, plus Noop/Trace/HostInterrupt/SetTracemarkRegister) are dual by virtue of owning a vtable in each pipe without a `FindFreeScalarSlot` pair.
- **The 20→13 embedding lowering.** The 20 high-level `kBarnaCore*` LLO ops are never directly priced (they LogFatal in the latency classifier); each has a `Bc<Op>` builder → `CreateBarnaCore<Op>` (LLO opcode `0x1bb..0x1c7`) → `BcsLloEmitter` expansion into the 13 priced primitives = scalar address math on the shared BCS ALU + a BarnaCore DMA (`BcDma`/`DmaGeneral`) + sync (`BcSwait*`/`BcSsyncAdd`/`BcSdoneWrite`).

| | |
|---|---|
| **Engine** | BarnaCore Sequencer (BCS), Pufferfish gen (`pxc::pfc`) |
| **Bundle** | `BarnaCoreSequencerBundle` — 2 scalar slots: `scalar_0`, `scalar_1` (`_table_` @ `0x21e868d8`) |
| **Opcode model** | proto oneof field number; order = oneof declaration order = wire tag order |
| **Op-class namespace** | `asic_sw::deepsea::pxc::pfc::isa::BarnaCoreSequencerScalar{0,1}_<Op>` |
| **Concrete op count** | **58 Scalar0 + 56 Scalar1 = 114** (RTTI typeinfo count 61+59=120) |
| **Pipe binding source** | `FindFreeScalarSlot<Scalar0_X, Scalar1_Y>` instantiations (`void` 2nd arg = pipe-only) |
| **Dual-issue / S0-only / S1-only** | 47 in both pipes (38 via `FindFreeScalarSlot`, 9 RTTI-only) · 11 S0-only (FloatMul + UintMul via `<S0,void>`, + 9 structural control/DMA) · 9 S1-only (6 via `<void,S1>`, + 3 structural SMEM) |
| **Embedding primitives** | 13 priced (scalar ALU + `BcDma`/`DmaGeneral` + `BcSwait*`/`BcSsyncAdd`/`BcSdoneWrite`) |
| **High-level embedding ops** | 20 `kBarnaCore*` LLO ops (opcode `0x1bb..0x1c7`), all LogFatal-priced → lower to the 13 |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page is the BCS *scalar* ISA + the embedding-op lowering. The 6-slot BarnaCore *Channel* VLIW (VectorExtendedResult / VectorLoad / VectorStore / VectorAlu0 / VectorAlu1 / ChannelScalar), the 54-shared-op vector ALU, and the EUP transcendental block are the channel datapath, summarized here only where the scalar pipe and embedding lowering touch it.** The 32-byte bundle byte-encoding lives in [BCS 32-Byte Bundle](bcs-32byte-bundle.md); the per-gen retirement rationale in [Retirement](retirement.md); the merged-ALU lineage in [Merged ALU](merged-alu.md).

---

## The Opcode-Proto Model

### Why the opcode is a proto field number

The BCS scalar ISA carries no opcode-name array. Each op is a distinct oneof submessage type `BarnaCoreSequencerScalar0_<Op>` (or `Scalar1_<Op>`) in the `asic_sw::deepsea::pxc::pfc::isa` namespace, and the bundle proto `BarnaCoreSequencerScalar0` holds a single oneof whose members are those op submessages. The **wire opcode tag is the oneof field number**, and the field numbers are assigned in declaration order, so the *order* in which the op submessages appear in the `TcParseTableBase` aux array *is* the opcode ordering.

The roster is recoverable because the proto runtime emits, per op, a full set of methods (`Clear`, `ByteSizeLong`, `_InternalSerialize`, `MergeImpl`, the ctor/dtor) and a per-op `_table_` parse table. Reloc-resolving the parent oneof's aux-pointer array (the entries are `R_X86_64_RELATIVE`; the file holds zeros, addends from `readelf -r`) yields each member's sub-`_table_` symbol, which `c++filt` maps back to the op class name. This is the proto analog of the SCS `Matches()` predicate sweep on the [SCS side](../sparsecore/scalar-opcode-enum.md): there the opcode is a `cmp` immediate; here it is a oneof field number.

```c
// BarnaCoreSequencerBundle::_table_ @ 0x21e868d8 — exactly 2 submessage aux slots
field[0] = scalar_0  -> BarnaCoreSequencerScalar0::_table_ @ 0x21e8b7f0  (max_field_number=62)
field[1] = scalar_1  -> BarnaCoreSequencerScalar1::_table_ @ 0x21e90b98  (max_field_number=60)
// each Scalar{0,1} _table_ holds a oneof aux array of submessage _table_ pointers,
// reloc-resolved (R_X86_64_RELATIVE) -> the op-class _table_ symbols -> c++filt.
```

The decompiled directory confirms the namespace and roster: 1,259 files match `BarnaCoreSequencerScalar0`, 1,191 match `Scalar1` (per-op proto-method explosion), and the trimmed op-class set matches the enumeration below op-for-op, including the proto-map noise type `ResourceUsageEntry_DoNotUse`.

### Why the opcode is a *field number*, not an encoded bit field

The proto field number is the *logical* opcode (the codec's selector). The *physical* bit allocation inside the 32-byte bundle is a separate concern owned by `InstBits_BarnaCorePxcHwMode` (`@0x33931f0`, 181,344 B) and `EncoderPfBarnaCoreSequencer::EncodeBundleInternal`; that byte-encoding is documented in [BCS 32-Byte Bundle](bcs-32byte-bundle.md) and is **not** the oneof ordinal. A reimplementer must keep the two distinct: the oneof ordinal selects which op; the HwMode bit table places its operands. This page pins the logical roster; the bundle page pins the encoding.

---

## The Asymmetric Pipe Split

The two slots implement one ISA with three membership classes, pinned by the `FindFreeScalarSlot<Scalar0_X, Scalar1_Y>` template instantiation set (`@0x140efa80..`). When both template args are concrete op classes, the op is **dual-issue** — the emitter picks whichever lane is free that cycle. When the second arg is `void` (mangled `EvE`), the op is **Scalar0-pipe-only**; `<void, Scalar1_X>` marks **Scalar1-pipe-only**. A second, structural class of pipe-only ops never routes through `FindFreeScalarSlot` at all — they simply own a vtable in one pipe and not the other (the control/DMA ops on Scalar0, the SMEM ops on Scalar1).

| Membership | Count | Ops |
|---|---:|---|
| **Dual-issue, `FindFreeScalarSlot<S0_X, S1_X>` pair** | 38 | the int ALU (IntAdd/Sub/IntAddCarryOut, the 6 int compares, And/Or/Xor), `ScalarFloatEqual`, FloatMax/Min, shifts, Convert, Move, the 6 `Sync*` waits (SyncDone/EqualTo/NotEqualTo/GreaterThan/GreaterOrEqualTo/LessThan), SyncAdd, Fence, IssueFsm, PopHmf, Delay, SetTagRegister, ReadRegisters, PredicateOr, Clz, IsInfOrNan, Halt |
| **Dual-issue, RTTI-only (no `FindFreeScalarSlot` pair)** | 9 | `Noop`, `Trace`, `HostInterrupt`, `ScalarSetTracemarkRegister`, and the 5 non-equal float compares `ScalarFloat{Greater,GreaterEqual,Less,LessEqual,NotEqual}` |
| **Scalar0-only** `<S0_X, void>` | 2 | `ScalarFloatMul`, `ScalarUintMul` |
| **Scalar0-only structural** | 9 | `ScalarBranch{Absolute,Relative,Reg}`, `ScalarCall{Absolute,Relative,Reg}`, `ScalarDma{Simple,SingleStrided}`, `ScalarGeneralDma` |
| **Scalar1-only** `<void, S1_X>` | 6 | `ReadDone`, `WriteDone`, `ReadPublicAccess`, `WritePublicAccess`, `ScalarFloatAdd`, `ScalarFloatSub` |
| **Scalar1-only structural** | 3 | `ScalarLoadSmem`, `ScalarLoadSmemOffset`, `ScalarStoreSmemAbsolute` |

The five rows sum to the 67 distinct op classes across the union (38 + 9 + 2 + 9 + 6 + 3); the 47 dual-issuable ops (38 + 9) appear in both pipes' RTTI, giving Scalar0 = 47 + 2 + 9 = 58 and Scalar1 = 47 + 6 + 3 = 56.

The interpretation is a deliberate port-pressure split for the embedding sequencer: **multiply** on pipe0 vs **add/sub** on pipe1 (a float-port split), the **SMEM load/store** datapath on pipe1 so a load can co-issue with a control/DMA on pipe0, and the four **sync-completion-register accessors** (`Read/Write` × `Done/PublicAccess`) on pipe1 — sync-done bookkeeping rides the load-store pipe while pipe0 drives control flow and DMA.

> **GOTCHA — `ScalarHalt` is in BOTH pipes.** The decompile shows both `BarnaCoreSequencerScalar0_ScalarHalt` and `BarnaCoreSequencerScalar1_ScalarHalt` op-class symbols, and the dual-issue `FindFreeScalarSlot<Scalar0_ScalarHalt, Scalar1_ScalarHalt>` instantiation (`@0x140e79e0`). A scheduler that forces Halt onto pipe0 will fail to dual-issue it with a pipe0 control op when pipe1 is free. `ScalarFence` and `IssueFsm` are likewise dual through a `FindFreeScalarSlot` pair; `Noop`, `Trace`, `HostInterrupt`, and `ScalarSetTracemarkRegister` are dual by owning a vtable in each pipe even though no `FindFreeScalarSlot` pair is emitted for them.

> **QUIRK — the `void` second template arg is the pipe-only marker, not an encoding artifact.** `FindFreeScalarSlot<...ScalarFloatMulEvEE>` (the trailing `EvE` = `<…, void>`) is what proves `ScalarFloatMul` cannot take pipe1; there is no `BarnaCoreSequencerScalar1_ScalarFloatMul` op-class symbol. A reimplementer reads the pipe binding from the template arg list, not from the bundle bits.

---

## TABLE E0 — The 58 Scalar0 Ops in Proto-Oneof (Opcode) Order

`BarnaCoreSequencerScalar0::_table_` @ `0x21e8b7f0`; `max_field_number=62`; 58 oneof submessage aux pointers reloc-resolved from `@0x21e8bb68..`. `ord` = oneof declaration index = wire opcode tag. `[S0]` = Scalar0-pipe-only.

| ord | op class | role |
|---:|---|---|
| 0 | `Noop` | no-op slot filler |
| 1 | `ScalarHalt` | halt sequencer (also Scalar1) |
| 2 | `HostInterrupt` | raise host interrupt |
| 3 | `Trace` | trace marker emit |
| 4 | `SyncDone` | wait sync-flag DONE (= `kBarnaCoreScalarWaitDone` `0x1ac`) |
| 5 | `SyncEqualTo` | wait sync == operand (= `kBarnaCoreScalarWaitEq` `0x1b2`) |
| 6 | `SyncNotEqualTo` | wait sync != operand (= `kBarnaCoreScalarWaitNe` `0x1b3`) |
| 7 | `SyncGreaterThan` | wait sync > operand (= `kBarnaCoreScalarWaitGt` `0x1b1`) |
| 8 | `SyncGreaterOrEqualTo` | wait sync >= operand (= `kBarnaCoreScalarWaitGe` `0x1b0`) |
| 9 | `SyncLessThan` | wait sync < operand (= `kBarnaCoreScalarWaitLt` `0x1af`) |
| 10 | `SyncAdd` | atomic add to sync flag (= `kBarnaCoreScalarSyncAdd` `0x1ad`) |
| 11 | `ScalarPopHmf` | pop host-message FIFO (= `kBarnaCoreScalarPop` `0x1b8`) |
| 12 | `ScalarDelay` | fixed-cycle delay |
| 13 | `ScalarSetTagRegister` | set instruction tag register |
| 14 | `ScalarSetTracemarkRegister` | set tracemark register |
| 15 | `ScalarBranchAbsolute` `[S0]` | branch to absolute target |
| 16 | `ScalarBranchRelative` `[S0]` | branch by relative offset |
| 17 | `ScalarBranchReg` `[S0]` | branch to register target |
| 18 | `ScalarCallAbsolute` `[S0]` | call absolute target |
| 19 | `ScalarCallRelative` `[S0]` | call relative offset |
| 20 | `ScalarCallReg` `[S0]` | call register target |
| 21 | `ScalarFence` | scalar memory fence |
| 22 | `IssueFsm` | issue address-handler FSM program |
| 23 | `ScalarDmaSimple` `[S0]` | simple DMA issue |
| 24 | `ScalarDmaSingleStrided` `[S0]` | single-strided DMA |
| 25 | `ScalarGeneralDma` `[S0]` | general (descriptor) DMA |
| 26 | `ScalarReadRegisters` | read register file |
| 27 | `ScalarConvertIntToFloat` | int→float convert |
| 28 | `ScalarConvertFloatToInt` | float→int convert |
| 29 | `ScalarIntAdd` | integer add |
| 30 | `ScalarIntSub` | integer sub |
| 31 | `ScalarAnd` | bitwise and |
| 32 | `ScalarOr` | bitwise or |
| 33 | `ScalarXor` | bitwise xor |
| 34 | `ScalarFloatMul` `[S0]` | float multiply |
| 35 | `ScalarUintMul` `[S0]` | uint multiply |
| 36 | `ScalarFloatMax` | float max |
| 37 | `ScalarFloatMin` | float min |
| 38 | `ScalarLogicalShiftLeft` | logical shl |
| 39 | `ScalarLogicalShiftRight` | logical shr |
| 40 | `ScalarArithmeticShiftRight` | arithmetic shr |
| 41 | `ScalarMove` | register move |
| 42 | `ScalarCountLeadingZeros` | clz |
| 43 | `ScalarIntEqual` | int == |
| 44 | `ScalarIntNotEqual` | int != |
| 45 | `ScalarIntGreater` | int > |
| 46 | `ScalarIntGreaterEqual` | int >= |
| 47 | `ScalarIntLess` | int < |
| 48 | `ScalarIntLessEqual` | int <= |
| 49 | `ScalarIntAddCarryOut` | int add with carry-out |
| 50 | `ScalarPredicateOr` | predicate or |
| 51 | `ScalarFloatEqual` | float == |
| 52 | `ScalarFloatNotEqual` | float != |
| 53 | `ScalarFloatGreater` | float > |
| 54 | `ScalarFloatGreaterEqual` | float >= |
| 55 | `ScalarFloatLess` | float < |
| 56 | `ScalarFloatLessEqual` | float <= |
| 57 | `ScalarIsInfOrNan` | classify inf/nan |

58 concrete ops. Plus the abstract base `BarnaCoreSequencerScalar0` (`_ZTV` @ `0x21e87840`) + `ResourceUsageEntry_DoNotUse` (proto-map entry) + the `BarnaCoreSequencerScalar0Decoder`/`Encoder` codec-template typeinfo = the 61 Scalar0 typeinfos.

---

## TABLE E1 — The 56 Scalar1 Ops in Proto-Oneof (Opcode) Order

`BarnaCoreSequencerScalar1::_table_` @ `0x21e90b98`; `max_field_number=60`; 56 oneof aux pointers reloc-resolved from `@0x21e90ef8..`. `[S1]` = Scalar1-pipe-only.

| ord | op class | role |
|---:|---|---|
| 0 | `Noop` | no-op slot filler |
| 1 | `ScalarHalt` | halt sequencer (also Scalar0) |
| 2 | `HostInterrupt` | raise host interrupt |
| 3 | `Trace` | trace marker emit |
| 4 | `SyncDone` | wait sync-flag DONE |
| 5 | `SyncEqualTo` | wait sync == operand |
| 6 | `SyncNotEqualTo` | wait sync != operand |
| 7 | `SyncGreaterThan` | wait sync > operand |
| 8 | `SyncGreaterOrEqualTo` | wait sync >= operand |
| 9 | `SyncLessThan` | wait sync < operand |
| 10 | `SyncAdd` | atomic add to sync flag |
| 11 | `ScalarPopHmf` | pop host-message FIFO |
| 12 | `ScalarDelay` | fixed-cycle delay |
| 13 | `ScalarLoadSmem` `[S1]` | load from SMEM |
| 14 | `ScalarLoadSmemOffset` `[S1]` | load SMEM with offset |
| 15 | `ScalarStoreSmemAbsolute` `[S1]` | store SMEM absolute |
| 16 | `ScalarSetTagRegister` | set instruction tag register |
| 17 | `ScalarSetTracemarkRegister` | set tracemark register |
| 18 | `ScalarFence` | scalar memory fence |
| 19 | `ScalarReadRegisters` | read register file |
| 20 | `IssueFsm` | issue address-handler FSM program |
| 21 | `ReadDone` `[S1]` | read sync-DONE completion reg (= `kBarnaCoreScalarSyncDoneRead` `0x1b4`) |
| 22 | `WriteDone` `[S1]` | write sync-DONE completion reg (= `kBarnaCoreScalarSyncDoneWrite` `0x1b5`) |
| 23 | `ReadPublicAccess` `[S1]` | read public-access sync reg (= `…PublicAccessRead` `0x1b6`) |
| 24 | `WritePublicAccess` `[S1]` | write public-access sync reg (= `…PublicAccessWrite` `0x1b7`) |
| 25 | `ScalarConvertIntToFloat` | int→float convert |
| 26 | `ScalarConvertFloatToInt` | float→int convert |
| 27 | `ScalarIntAdd` | integer add |
| 28 | `ScalarIntSub` | integer sub |
| 29 | `ScalarAnd` | bitwise and |
| 30 | `ScalarOr` | bitwise or |
| 31 | `ScalarXor` | bitwise xor |
| 32 | `ScalarFloatAdd` `[S1]` | float add |
| 33 | `ScalarFloatSub` `[S1]` | float sub |
| 34 | `ScalarFloatMax` | float max |
| 35 | `ScalarFloatMin` | float min |
| 36 | `ScalarLogicalShiftLeft` | logical shl |
| 37 | `ScalarLogicalShiftRight` | logical shr |
| 38 | `ScalarArithmeticShiftRight` | arithmetic shr |
| 39 | `ScalarMove` | register move |
| 40 | `ScalarCountLeadingZeros` | clz |
| 41 | `ScalarIntEqual` | int == |
| 42 | `ScalarIntNotEqual` | int != |
| 43 | `ScalarIntGreater` | int > |
| 44 | `ScalarIntGreaterEqual` | int >= |
| 45 | `ScalarIntLess` | int < |
| 46 | `ScalarIntLessEqual` | int <= |
| 47 | `ScalarIntAddCarryOut` | int add with carry-out |
| 48 | `ScalarPredicateOr` | predicate or |
| 49 | `ScalarFloatEqual` | float == |
| 50 | `ScalarFloatNotEqual` | float != |
| 51 | `ScalarFloatGreater` | float > |
| 52 | `ScalarFloatGreaterEqual` | float >= |
| 53 | `ScalarFloatLess` | float < |
| 54 | `ScalarFloatLessEqual` | float <= |
| 55 | `ScalarIsInfOrNan` | classify inf/nan |

56 concrete ops. Plus abstract base + `ResourceUsageEntry_DoNotUse` + codec-template typeinfo = 59 Scalar1 typeinfos.

> **NOTE — the divergence is bounded to ordinals 13..24 (Scalar0) / 13..24 (Scalar1) plus the FloatMul/UintMul vs FloatAdd/FloatSub pair.** The shared header (0..12) and the shared ALU/compare tail are byte-identical in oneof ordering across both pipes. Scalar0 spends ordinals 15..25 on Branch×3/Call×3/Dma×3 and 34..35 on Mul; Scalar1 spends 13..15 on SMEM, 21..24 on the four sync-completion regs, and 32..33 on FloatAdd/Sub. A reimplementer can decode the common region with one table and branch on pipe id only for the divergent band.

---

## The 13 Priced BCS Embedding Primitives

The BCS does not have a wide embedding datapath. Everything an embedding op needs — address arithmetic, the DMA that moves a row, and the sync that orders it — is expressed in the scalar ISA above plus a handful of LLO-level DMA/sync builders. These are the **13 priced primitives**: the operations the BarnaCore latency model (`PufferfishBarnaCorePerformance`) actually costs. They fall in three families.

| Family | Primitive (`LloRegionBuilder::Bc*` / scalar op) | Role | Confidence |
|---|---|---|---|
| **Scalar address math** | the shared BCS scalar ALU (`ScalarIntAdd/Sub`, `ScalarAnd`, `ScalarUintMul`, `ScalarIntLess`, `ScalarMove`, `ScalarPredicateOr`, plus the emitter helpers `SimmS32`/`SneS32`/`SsubS32`/`SandU32`/`SltS32`/`Sselect`/`Pand`/`SdivU32`/`SmulU32`/`SmodU32`) | compute the bmem/HBM word address, granule align, modulo-partition columns across cores | CONFIRMED |
| **DMA** | `BcDma` (→ `kBarnaCoreDma` `0x1bb`, 19-arg), `barna_core::DmaGeneral`, `EnqueueDmaLocalInGranules` | move an embedding row HBM↔bmem↔VMEM; cross-core remote DMA | CONFIRMED |
| **Sync** | `BcSwaitInfeedSV` (`0x1ae`), `BcSwaitEqSV` (`0x1b2`), `BcSwaitGeSV` (`0x1b0`), `BcSwaitGtSV` (`0x1b1`), `BcSwaitNeSV` (`0x1b3`), `BcSwaitDone` (`0x1ac`), `BcSsyncAdd` (`0x1ad`), `BcSdoneWrite` (`0x1b5`), `BcSpop` (`0x1b8`) | wait/signal sync flags; gather/scatter completion ordering | CONFIRMED |

The `BcSwait*` family is the LLO-level surface of the Scalar0/Scalar1 `Sync*` ops (ords 4..10) — `BcSwaitEqSV` emits a `SyncEqualTo`, `BcSwaitGeSV` a `SyncGreaterOrEqualTo`, `BcSsyncAdd` a `SyncAdd`, `BcSdoneWrite` a `WriteDone`. The decompile shows all of `BcSwait{Done,EqSV,GeSV,GtSV,NeSV,InfeedSV}`, `BcSsyncAdd`, `BcSpop`, `BcSdoneWrite`, `BcSfence` as `LloRegionBuilder` methods, alongside the address helpers `BcBmemAddrScaled` and the channel moves `BcVectorLoad`/`BcVectorLoadImmediateOffset`/`BcVectorStore`.

---

## The 20 → 13 Embedding-Op Lowering

The high-level embedding surface is a set of 20 `kBarnaCore*` LLO ops — local/global gather, gradient scatter, sparse-reduce, remote-scalar-write, remote-buffer FSM allocation. They are **never directly priced**: in the BarnaCore latency classifier they LogFatal, exactly as the SparseCore stream ops are absent from the SC scalar cost table. Their cost is the **sum of the primitives they expand into**. The expansion is a three-tier datapath.

```text
  TIER 1 (builder)        TIER 2 (LLO op)                       TIER 3 (expansion)
  ---------------------   -----------------------------------   ---------------------------
  LloRegionBuilder        LloInstruction::CreateBarnaCore<Op>   barna_core::BcsLloEmitter::*
   ::Bc<Op>               (mov edi,0x1XX; LloInstruction::New)  -> scalar ALU + BcDma + sync
```

### Tier 1 → Tier 2: builders and LLO opcodes

`LloRegionBuilder::Bc<Op>` @ `0x1d57d560..` dispatches a legacy arm and a Pufferfish (`Pf`) arm; the `Pf` arm takes more `LloValue*` operands (the extra remote-buffer / multi-core addressing). Each emits `LloInstruction::CreateBarnaCore<Op>`, whose opcode constant is byte-pinned from the `mov edi,0x1XX` before the `LloInstruction::New` call. The decompiled ctors confirm the constants exactly: `CreateBarnaCoreDma` calls `LloInstruction::New(443, ...)` = `0x1bb`; `CreateBarnaCoreLocalGather` passes `449` = `0x1c1`; `CreateBarnaCoreSparseReduce` passes `451` = `0x1c3`.

| high-level op | `Bc*` builder | `Create*` ctor | LLO opcode | Confidence |
|---|---|---|---:|---|
| `kBarnaCoreDma` | `BcDma` (19-arg) | `CreateBarnaCoreDma` (`New(443,…)`) | `0x1bb` | CONFIRMED |
| `kBarnaCoreRemoteScalarWrite` | `BcRemoteScalarWrite` / `BcPf…` | `CreateBarnaCoreRemoteScalarWrite` / `Pf` | `0x1bc` / `0x1bd` | CONFIRMED |
| `kBarnaCoreGlobalScatterIds` | `BcGlobalScatterIds` / `BcPf…` (7 args) | `CreateBarnaCoreGlobalScatterIds` / `Pf` | `0x1bf` / `0x1c0` | CONFIRMED |
| `kBarnaCoreLocalGather` | `BcLocalGather` (6-arg) / `BcPf…` (10 args) | `CreateBarnaCoreLocalGather` (`New(449,…)`) / `Pf` | `0x1c1` / `0x1c2` | CONFIRMED |
| `kBarnaCoreSparseReduce` | `BcSparseReduce` (4-arg) / `BcPf…` (7 args) | `CreateBarnaCoreSparseReduce` (`New(451,…)`) / `Pf` | `0x1c3` / `0x1c4` | CONFIRMED |
| `kBarnaCoreGlobalScatterGradients` | `BcGlobalScatterGradients` (8 args) / `BcPf…` | `CreateBarnaCoreGlobalScatterGradients` / `Pf` | `0x1c6` / `0x1c5` | CONFIRMED |
| `kBarnaCoreLocalScatterGradients` | `BcLocalScatterGradients` / `BcPf…` | `CreateBarnaCoreLocalScatterGradients` / `Pf` | `0x1c7` / `0x1c8` | CONFIRMED |
| `kBarnaCoreIssueFsm` | `BcIssueFsm` | (emits `IssueFsm`) | `0x1b9` | CONFIRMED |
| `kBarnaCoreScalarFence` | `BcSfence` | (emits `ScalarFence`) | `0x1ba` | CONFIRMED |
| `kBarnaCoreScalarWaitInfeed` | `BcSwaitInfeedSV` | (emits a `Sync*` wait) | `0x1ae` | CONFIRMED |
| `kBarnaCoreMoveScalarReg` | `EmitBarnaCoreMoveScalarReg` (`@0x140c9400`) | — | `0x1cb` | CONFIRMED |

The 20-op count is the F-routed block (the gather/scatter/reduce/FSM/remote-buffer ops) counted across the legacy + `Pf` arms; the seven core ops above (`Dma`, `RemoteScalarWrite`, `GlobalScatterIds`, `LocalGather`, `SparseReduce`, `GlobalScatterGradients`, `LocalScatterGradients`) plus their `Pf` variants and the `IssueFsm`/`Fence`/`WaitInfeed`/`MoveScalarReg` adjuncts make up the surface.

### Tier 3: `BcsLloEmitter` expansion into the 13 primitives

`platforms_deepsea::jellyfish::barna_core::BcsLloEmitter` (`@0xf9d7700..0xf9d87a0`, disassembled byte-exact) is the embedding datapath. Each high-level op expands into scalar address math + a DMA + sync. The descriptor fetch goes through `BcsMetadataAccessor` (`LoadBmemWordAddressFromMetadata @0xf9d9140`, `LoadPassHeaderMetadata @0xf9d8d40`, `LoadPayloadLocationMetadata @0xf9d8da0`, `LoadPartitionColumn @0xf9d92e0`, `LoadBarnaCoreLocation`, `LoadRemoteBufferOffset`, `GetBarnaCoreAbsoluteHbmAddress`, `LoadFsmTransferSizeMemUnit`) — the BarnaCore equivalent of the SparseCore TAC descriptor table.

| high-level op | `BcsLloEmitter` path | expansion shape | Confidence |
|---|---|---|---|
| `kBarnaCoreLocalGather` | `IssueDmaInfeedToVmem` (`@0xf9d77e0`) | metadata loads → ~10 scalar ALU (`SimmS32`/`SneS32`/`SandU32`/`SsubS32`/`SltS32`/`Sselect`/`Pand`/`SdivU32`) → `SmemWordAddress`/`BcBmemAddrScaled` → `EnqueueDmaLocalInGranules` (×2, predicated) → `BcDma` + `BcSwait` sync | CONFIRMED |
| `kBarnaCoreGlobalScatterGradients` | `IssueDmaScatter` (`@0xf9d8400`) → `IssueDmaScatterOne` (`@0xf9d8560`) | `TpuCoreLocation::Id` → `SimpleLoop` over columns → per-column `SmulU32`×2/`SaddS32`/`SmodU32` (modulo-partition) → `LoadPartitionColumn` → `LoadBarnaCoreLocation`/`LoadRemoteBufferOffset`/`GetBarnaCoreAbsoluteHbmAddress`/`LoadFsmTransferSizeMemUnit` → `barna_core::DmaGeneral` (cross-core remote) | CONFIRMED |
| gather sync (`WaitForInfeedOfHostIds` `@0xf9d7700`, `WaitForInfeedToVmemDma` `@0xf9d7bc0`) | `WaitOnInfeedSyncFlag` (`@0xf9d9e00`) / `WaitOnValueAndClearSyncFlag` (`@0xf9d9d40`) | `BcSwaitInfeedSV` + `BcSwaitGeSV` + `BcSsyncAdd`; `BcSwaitEqSV`/`BcSwaitGeSV` + `BcSdoneWrite` | CONFIRMED |
| `kBarnaCoreAllocateRemoteBuffers` | `AllocateRemoteBuffers` (`@0xf9d7ca0`) | → `LloRegionBuilder::BcIssueFsm` (programs the address-handler FSM); `AllocateRemoteBufferForPadding @0xf9d8340` the padding variant | CONFIRMED |
| `kBarnaCoreSparseReduce` | `BcSparseReduce @0x1d57d920` → `CreateBarnaCoreSparseReduce` (`451`) | realized via the **address-handler FSM + DMA**, not an on-engine wide reduce (see callout) | CONFIRMED |

A `LocalGather` therefore costs `{≈10 scalar ALU ops + 2 BcDma + sync}`; a `GlobalScatterGradients` costs `{loop × [scalar partition math + DmaGeneral remote]}`. Because the cost is the sum of primitive latencies (scalar = 1, etc.) over a runtime-dynamic loop trip count (feature length / partition count), the high-level op cannot be priced as a single constant — which is precisely why the 20 ops LogFatal in the direct classifier.

> **NOTE — `SparseReduce` has NO native channel reduce; the LogFatal is structural evidence.** `PufferfishBarnaCoreChannelEmitter::EmitVectorSegmentedReduce` (`@0x140cf3a0`) and `EmitVectorCrossLaneReduce` (`@0x140cf360`) are all-LogFatal: the body is `LogMessageFatal("…pufferfish_barnacore_channel_emitter.h", 436) << "Not implemented"`. BarnaCore's `SparseReduce` runs as a strided/segmented DMA accumulate through the address-handler FSM, not a wide on-engine reduce. This is the concrete ISA datum behind BarnaCore's retirement: where SparseCore's TEC adds three native vector ALUs with scan/sort/uniquify, BarnaCore's embedding reduction bounces through the FSM + DMA. See [Retirement](retirement.md) and [SCS Scalar Opcode Enumeration](../sparsecore/scalar-opcode-enum.md) for the SC-side contrast.

---

## Contrast: BCS vs the SparseCore SCS Successor

The BCS dual-scalar pipe is the structural ancestor of the SCS three-scalar-slot model. The convergence is close enough to be retirement evidence: the Pufferfish BC sequencer scalar ISA mirrors the SC scalar ALU op-for-op in the arithmetic/compare/sync block.

| Aspect | BCS (BarnaCore, this page) | SCS ([successor](../sparsecore/scalar-opcode-enum.md)) |
|---|---|---|
| Scalar slots per bundle | 2 (`scalar_0`, `scalar_1`) | 3 (`ScsScalarMisc`, `ScalarAlu1`, `ScalarAlu0`) |
| Opcode model | proto oneof field number (declaration order) | 6-bit primary + class escapes; opcode = `Matches()` immediate |
| Concrete scalar ops | 58 + 56 = 114 | ~78 (Alu0) / ~82 (Alu1) / ~82 (Misc) op-forms |
| Multiply / add-sub split | FloatMul on S0, FloatAdd/Sub on S1 | FloatMul/Mul/Div on Alu0, FloatAdd/Sub on Alu1 |
| SMEM load/store | Scalar1-only | `ScalarAlu1`-only |
| Sync/atomic | `Sync*` ops on both pipes; `Read/WriteDone`+`PublicAccess` on S1 | dedicated `ScsScalarMisc` composite sync/atomic slot |
| Embedding reduce | FSM + DMA (channel reduce LogFatal) | TEC native vector reduce (scan/sort/uniquify) |
| Embedding gather/scatter | `BcsLloEmitter` → scalar ALU + `BcDma`/`DmaGeneral` + sync | TAC stream-gather / TEC scan pipeline (`STREAM_OPCODE_*`) |

The mapping is direct: BarnaCore `LocalGather` ≈ SparseCore `STREAM_OPCODE_GATHER`; `GlobalScatterGradients` ≈ `STREAM_OPCODE_SCATTER_FLOAT_ADD`; `SparseReduce` ≈ the TEC segmented/cross-lane reduce — except BarnaCore lacks the native vector reduce and falls back to FSM + DMA, the headline functional gap that motivated retiring BarnaCore in favor of SparseCore.

---

## Function Map

All addresses are Pufferfish (`pxc::pfc`) BCS; the proto-oneof ordinal is the opcode, the `mov edi,0x1XX` constant is the LLO opcode.

| Symbol | Address | Evidence | Confidence |
|---|---|---|---|
| `BarnaCoreSequencerBundle::_table_` | `0x21e868d8` | 2 submessage aux slots {Scalar0, Scalar1} | CONFIRMED |
| `BarnaCoreSequencerScalar0::_table_` | `0x21e8b7f0` | `max_field_number=62`; 58 oneof aux @ `0x21e8bb68` | CONFIRMED |
| `BarnaCoreSequencerScalar1::_table_` | `0x21e90b98` | `max_field_number=60`; 56 oneof aux @ `0x21e90ef8` | CONFIRMED |
| `BarnaCoreSequencerScalar0` abstract base `_ZTV` | `0x21e87840` | vtable for typeinfo reconciliation | CONFIRMED |
| `FindFreeScalarSlot<…SyncAdd, …SyncAdd>` | `0x140efa80` | dual-issue binding (38 such pairs) | CONFIRMED |
| `FindFreeScalarSlot<…ScalarFloatMul, void>` | `0x140ee020` | Scalar0-only binding (`EvE` marker) | CONFIRMED |
| `FindFreeScalarSlot<…ScalarHalt, …ScalarHalt>` | `0x140e79e0` | Halt is dual-issuable | CONFIRMED |
| `LloInstruction::CreateBarnaCoreDma` | `0x1d4e1c20` | `LloInstruction::New(443,…)` = `0x1bb` | CONFIRMED |
| `LloInstruction::CreateBarnaCoreLocalGather` | `0x1d4e2040` | `New(449,…)` = `0x1c1` | CONFIRMED |
| `LloInstruction::CreateBarnaCoreSparseReduce` | `0x1d4e2120` | `New(451,…)` = `0x1c3` | CONFIRMED |
| `LloRegionBuilder::BcDma` | `0x1d57d5a0` | 19-arg `kBarnaCoreDma` builder | CONFIRMED |
| `LloRegionBuilder::BcLocalGather` | `0x1d57d880` | 6-arg local-gather builder | CONFIRMED |
| `LloRegionBuilder::BcSparseReduce` | `0x1d57d920` | 4-arg sparse-reduce builder | CONFIRMED |
| `barna_core::BcsLloEmitter::IssueDmaInfeedToVmem` | `0xf9d77e0` | local-gather expansion | CONFIRMED |
| `barna_core::BcsLloEmitter::IssueDmaScatter` | `0xf9d8400` | gradient-scatter column loop | CONFIRMED |
| `barna_core::BcsLloEmitter::WaitForInfeedOfHostIds` | `0xf9d7700` | gather sync → `BcSwait*`/`BcSsyncAdd` | CONFIRMED |
| `barna_core::BcsLloEmitter::AllocateRemoteBuffers` | `0xf9d7ca0` | remote-buffer FSM via `BcIssueFsm` | CONFIRMED |
| `BcsMetadataAccessor::LoadBmemWordAddressFromMetadata` | `0xf9d9140` | embedding descriptor fetch | CONFIRMED |
| `PufferfishBarnaCoreChannelEmitter::EmitVectorSegmentedReduce` | `0x140cf3a0` | LogFatal "Not implemented" (no native reduce) | CONFIRMED |
| `InstBits_BarnaCorePxcHwMode` | `0x33931f0` | 181,344 B bit-encoding table (bundle page) | HIGH |

---

## Considerations

- **The opcode is the oneof field number, not a bit field.** The roster above is the *logical* opcode space (the codec selector). The physical 32-byte bundle bit allocation is `InstBits_BarnaCorePxcHwMode`'s concern — see [BCS 32-Byte Bundle](bcs-32byte-bundle.md). Do not read the oneof ordinal as a bit position.
- **The typeinfo count is not the op count.** Counting RTTI typeinfos gives 61 Scalar0 + 59 Scalar1 = 120; the concrete ISA op count is 58 + 56 = 114 after subtracting the abstract base, the proto-map entry, and the codec-template typeinfo from each pipe. A reimplementer needs 114 op encoders.
- **Pipe binding is read from template args, not bundle bits.** `<S0_X, void>` / `<void, S1_X>` mark the FindFreeScalarSlot pipe-only ops; structural pipe-only ops have a vtable in only one pipe. The 38 dual-issue / 2 S0-only / 6 S1-only split plus the structural control-DMA/SMEM ops is the complete picture.
- **The 20 high-level ops carry NO standalone latency.** They LogFatal in the direct classifier; their cost is the runtime-dynamic sum of the 13 primitives they expand into. Any cost model must walk the `BcsLloEmitter` expansion, not look up the high-level op.
- **`SparseReduce`/segmented-reduce being LogFatal is a feature absence, not a stub.** BarnaCore genuinely has no native vector reduce; the embedding reduction is an FSM + DMA accumulate. A reimplementation that emits a channel reduce op will hit the same `Not implemented` fatal.
- **All BarnaCore LLO-opcode integers are pinned two independent ways.** The seven core gather/scatter/reduce ctors carry a byte-exact `mov edi, 0x1XX` immediate before `LloInstruction::New` (`0x1bb`/`0x1bc`/`0x1bf`/`0x1c1`/`0x1c3`/`0x1c6`/`0x1c7`, plus the `Pf` variants `0x1bd`/`0x1c0`/`0x1c2`/`0x1c4`/`0x1c5`/`0x1c8`). The full opcode→name binding for the entire `0x1ac..0x1cc` BarnaCore block — including `WaitInfeed` `0x1ae`, `IssueFsm` `0x1b9`, `ScalarFence` `0x1ba`, and `MoveScalarReg` `0x1cb` — is independently fixed by the relocated `LloOpcodeName::opcode_name` pointer table (`@0x21ccfef0`), so those four are CONFIRMED, not merely inferred from call order.

---

## Cross-References

- [BarnaCore Overview](overview.md) — the legacy embedding accelerator, the three generations (Jellyfish/Dragonfish/Pufferfish), and where the BCS sits in the pipeline.
- [BCS 32-Byte Bundle](bcs-32byte-bundle.md) — the physical bundle byte-encoding (`InstBits_BarnaCorePxcHwMode`); this page's opcodes are the logical oneof ordinals that the bundle places.
- [Merged ALU](merged-alu.md) — the shared scalar/vector ALU lineage the BCS arithmetic/compare block belongs to.
- [Retirement](retirement.md) — why SparseCore replaced BarnaCore; the `SparseReduce`-via-FSM LogFatal datum on this page is one of the five evidence lines.
- [SCS Scalar Opcode Enumeration](../sparsecore/scalar-opcode-enum.md) — the SparseCore SCS scalar ISA, the successor whose three-slot dual-ALU model the BCS dual-scalar split foreshadows; the convergence is the retirement argument.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4) — [back to index](../index.md)
