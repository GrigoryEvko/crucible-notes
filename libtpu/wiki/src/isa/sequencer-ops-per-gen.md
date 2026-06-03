# Sequencer Ops Per Gen × Type

> *Every op name, namespace, address, and enum value on this page was read byte-exactly from the symbol table and `.text` of `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped, 1,233,709 symbols). Other versions differ.*

## Abstract

The TPU sequencer slot ([Sequencer Slot](slot-sequencer.md)) does not expose the same control-flow op roster on every chip. Two axes vary it: the **silicon generation** (Jellyfish through `6acc60406`) and the **sequencer type** (`TpuSequencerType`) — the sub-core within a generation that the bundle is destined for (TensorCore, BarnaCore, or one of the SparseCore engines). The codec, the proto namespace, and the available ops are all keyed on the `(TpuVersion, TpuSequencerType)` pair, so the precise question "which control-flow ops exist" only has an answer once both coordinates are fixed.

This page is the gen × type inventory. The op presence is recovered from the per-(gen × type) protobuf message-type symbols in the binary: each control-flow op is a distinct C++ type such as `vxc::isa::TensorCoreScalarAlu_BranchRelative` or `gxc::gfc::isa::SparseCoreScalarAlu_CallSreg`, and a generation either has that symbol or it does not. The namespace prefix *is* the (gen × type) key — `vxc::isa` is Viperfish TensorCore, `vxc::vfc::isa` is Viperfish SparseCore, `gxc::glc::isa` is Ghostlite, `gxc::gfc::isa` is `6acc60406`, `pxc::isa` is Pufferfish TensorCore, and `platforms_deepsea::jellyfish::isa` is Jellyfish. (The `gxc` pairing inverts easily: Ghostlite is the load-core `glc`, `6acc60406` is the fetch-core `gfc` — see [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md).) Cross-referencing the presence of `…HaltYield`, `…ReadRegisterLcc{Low,High}`, `…BranchRelativeRotatingPreg`, and the SparseCore `…ScalarMisc_*` sync family against each namespace yields the matrix.

For reimplementation, the contract is:

- `TpuSequencerType` is a 6-value enum; the codec/emitter registry is keyed by `(TpuVersion, TpuSequencerType)`, and the *same* generation serves several sequencer types with different op rosters.
- The TensorCore sequencer is present on every generation; the SparseCore engines (SCS / TAC / TEC) appear only from Viperfish, and **`6acc60406` drops TAC**.
- The branch / call / halt / delay core is universal; the deltas are the *yield* family (introduced at Viperfish, narrowed at Ghostlite, dropped at `6acc60406`), the hardware-loop-counter read (Viperfish+ only), the dual-channel and rotating-predicate sync ops, and the BarnaCore sync family.
- Naming drifts across generations: Pufferfish uses a `Scalar`-prefix and `Reg` suffix (`ScalarBranchReg`); Viperfish+ drops the prefix and renames `Reg`→`Sreg` (`BranchSreg`).

| | |
|---|---|
| **Enum** | `tpu::TpuSequencerType`, 6 values (internal `0..5`) |
| **ToString** | `TpuSequencerTypeToString` @ `0x20b362e0` (indexes `off_22010DE0`) |
| **FromProto** | `TpuSequencerTypeFromProto` @ `0x20b36300` (proto `1..6` → internal `0..5`) |
| **Enum table** | `tpu::kTpuSequencerTypes` @ `0xb540778` (values `0,1,2,3,4,5`) |
| **Codec key** | `(TpuVersion, TpuSequencerType)`; SCS instantiated at `TpuSequencerType=3` |
| **TC namespaces** | JF `jellyfish::isa`; PF `pxc::isa`; VF `vxc::isa`; GL `gxc::glc::isa`; 6acc60406 `gxc::gfc::isa` |
| **SCS namespaces** | VF `vxc::vfc::isa`; GL `gxc::glc::isa`; 6acc60406 `gxc::gfc::isa` |
| **Universal core** | Branch{Abs,Rel,Sreg/Ind}, Call{Abs,Rel,Sreg/Ind}, Halt, Delay, Fence |
| **Viperfish+ additions** | `ReadRegisterLcc{Low,High}`, `HaltYield*`, `ScalarMisc` sync lane |
| **6acc60406 additions** | `BranchRelativeRotatingPreg`, `SetRotatingPredicateRegister`, `SetPOrTState` |
| **Confidence** | CONFIRMED (symbol-presence-anchored) unless a row says otherwise |

---

## The `TpuSequencerType` Enum

`TpuSequencerType` is the second key into the codec/emitter registry. Its six values are recovered byte-exactly: `TpuSequencerTypeFromProto` (`0x20b36300`) maps the protobuf enum (`1..6`) to the internal C++ enum (`0..5`), and `TpuSequencerTypeToString` (`0x20b362e0`) is a flat table index `off_22010DE0[value]`. The string-pointer length table at `0xbdf2878` gives each name's length (`19, 18, 23, 19, 33, 34`), which pins the six names uniquely:

| Internal value | Proto value | Name (`k…`) | Role | Codename / gen | Confidence |
|---:|---:|---|---|---|---|
| 0 | 1 | `kTensorCoreSequencer` | TC — the main TensorCore VLIW sequencer | all gens | CONFIRMED |
| 1 | 2 | `kBarnaCoreSequencer` | BCS — Pufferfish BarnaCore sequencer | Pufferfish | CONFIRMED |
| 2 | 3 | `kBarnaCoreAddressHandler` | BCAH — Jellyfish BarnaCore address handler | Jellyfish | CONFIRMED |
| 3 | 4 | `kSparseCoreSequencer` | SCS — SparseCore scalar sequencer | Viperfish+ | CONFIRMED |
| 4 | 5 | `kSparseCoreTileAccessSequencer` | TAC — SparseCore tile-access | Viperfish, Ghostlite | CONFIRMED |
| 5 | 6 | `kSparseCoreTileExecuteSequencer` | TEC — SparseCore tile-execute | Viperfish+ | CONFIRMED |

The mapping is the literal switch in the decompiled `TpuSequencerTypeFromProto`:

```c
// tpu::TpuSequencerTypeFromProto(TpuSequencerTypeProto)  @ 0x20b36300
switch (proto) {
    case 1: result = 0; break;   // kTensorCoreSequencer
    case 2: result = 1; break;   // kBarnaCoreSequencer
    case 3: result = 2; break;   // kBarnaCoreAddressHandler
    case 4: result = 3; break;   // kSparseCoreSequencer
    case 5: result = 4; break;   // kSparseCoreTileAccessSequencer
    case 6: result = 5; break;   // kSparseCoreTileExecuteSequencer
    default: return error("Invalid sequencer type: " + proto);
}
```

The codec instantiations confirm the keying: the SparseCore SCS codec template is instantiated at `(tpu::TpuSequencerType)3`, e.g. `EncoderBase<…SparseCoreScsCodecBase<…>…, (tpu::TpuSequencerType)3>::EncodeBundle`, matching `kSparseCoreSequencer = 3`. The TAC codec (`gxc::glc::isa::SparseCoreTacCodecBase`) is instantiated at `(tpu::TpuSequencerType)4`, matching `kSparseCoreTileAccessSequencer = 4`.

> **NOTE — the sequencer type is a codec key, not a chip property.** A single generation hosts several sequencer types simultaneously: Ghostlite has TensorCore, SCS, TAC, and TEC sequencers, each with its own bundle width and op roster. The `(TpuVersion, TpuSequencerType)` pair is what selects the codec ([Bundle Model](bundle-model-overview.md)); a reimplementation that treats "sequencer type" as derivable from the chip alone cannot encode a SparseCore bundle on a chip that also runs TensorCore bundles.

---

## Sub-Core Presence Per Generation

Before the op matrix, the coarser question: which sequencer types exist on which generation. This is decided by codec-class presence (`SparseCoreScsCodecBase`, `SparseCoreTacBundle`, `SparseCoreTecBundle`) in each gen's namespace.

| Gen | TC | BarnaCore | SCS | TAC | TEC | Source | Confidence |
|---|:--:|:--:|:--:|:--:|:--:|---|---|
| Jellyfish (v2) | ✓ | BCAH | — | — | — | `jellyfish::isa` + `barna_core` | CONFIRMED |
| Dragonfish (v3) | ✓ | BCAH | — | — | — | aliases Jellyfish codec | HIGH |
| Pufferfish (v4) | ✓ | BCS | — | — | — | `pxc::isa` + `pxc::pfc::isa` | CONFIRMED |
| Viperfish (v5p, +v5e lite) | ✓ | — | ✓ | ✓ | ✓ | `vxc::isa`, `vxc::vfc::isa` | CONFIRMED |
| Ghostlite (v6e) | ✓ | — | ✓ | ✓ | ✓ | `gxc::glc::isa` | CONFIRMED |
| `6acc60406` (v7) | ✓ | — | ✓ | — | ✓ | `gxc::gfc::isa` | CONFIRMED |

The `6acc60406` TAC drop is byte-anchored: `gxc::gfc::isa::SparseCoreTac{Bundle,CodecBase,Program}` is absent, while `gfc::isa::SparseCoreTec{Bundle,Program}` and `gfc::isa::SparseCoreScs{Bundle,CodecBase,Program}` are present (`nm -C`). Viperfish and Ghostlite each have all three SparseCore codecs. BarnaCore is a pre-Viperfish construct: Jellyfish's address handler (BCAH) and Pufferfish's sequencer (BCS) are distinct sequencer types, gone from Viperfish onward as SparseCore replaces it. The full sub-core taxonomy is on [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md).

---

## The Control-Flow Op Matrix (gen × sequencer-type)

The matrix below is the per-(gen × type) presence of each control-flow op family, anchored to the proto message-type symbols. `✓` = the proto message type exists in that gen's namespace; `—` = absent.

| Gen × Type | Branch Abs/Rel | Branch Indirect | Call Abs/Rel | Call Indirect | Return form | Halt | HaltYield | HaltYieldCond | Delay | Fence | LCC read |
|---|:--:|:--:|:--:|:--:|---|:--:|:--:|:--:|:--:|:--:|:--:|
| **JF TC** | ✓ | ✓ (BTR) | ✓ | ✓ | branch-to-BTR | ✓ | — | ✓ | ✓ | ✓ | — |
| **JF BCAH** | ✓ | ✓ | ✓ | ✓ | implicit / loop | ✓ | — | ✓ | ✓ | ✓ | — |
| **PF TC** | ✓ `ScalarBranch{Abs,Rel}` | ✓ `ScalarBranchReg` | ✓ `ScalarCall{Abs,Rel}` | ✓ `ScalarCallReg` | dest sreg | ✓ | — | — | ✓ | ✓ | — |
| **PF BCS** | ✓ `ScalarBranch{Abs,Rel}` | ✓ `ScalarBranchReg` | ✓ `ScalarCall{Abs,Rel}` | ✓ `ScalarCallReg` | dest sreg | ✓ | — | — | ✓ | ✓ | — |
| **VF TC** | ✓ | ✓ `BranchSreg` | ✓ | ✓ `CallSreg` | branch-to-dest | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **VF SCS** | ✓ + `BranchAbsoluteClearIbuf` | ✓ `BranchSreg` | ✓ (link #5) | ✓ `CallSreg` | branch-to-dest | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **GL TC** | ✓ | ✓ `BranchSreg` | ✓ | ✓ `CallSreg` | branch-to-dest | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| **GL SCS** | ✓ + `BranchAbsoluteClearIbuf` | ✓ `BranchSreg` | ✓ (link #5) | ✓ `CallSreg` | branch-to-dest | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| **GF TC** | ✓ | ✓ `BranchSreg` | ✓ | ✓ `CallSreg` | branch-to-dest | ✓ | — | — | ✓ | ✓ | ✓ |
| **GF SCS** | ✓ + `BranchAbsoluteClearIbuf` + `BranchRelativeRotatingPreg` | ✓ `BranchSreg` | ✓ (link #5) | ✓ `CallSreg` | branch-to-dest | ✓ | — | — | ✓ | ✓ | ✓ |

The matrix is recovered from these byte-anchored symbol facts:

- **`HaltYield` (unconditional)** exists only on `vxc::isa::TensorCoreScalarAlu_HaltYield` and `vxc::vfc::isa::SparseCoreScalarAlu_HaltYield` — **Viperfish only**.
- **`HaltYieldConditional`** exists on `vxc` (VF TC + SCS) and `gxc::glc::isa` (GL TC + SCS), and is **absent from `gxc::gfc::isa`** — so `6acc60406` drops yield entirely.
- **`ReadRegisterLccLow` / `ReadRegisterLccHigh`** exist on `vxc`, `glc`, `gfc` (TC and SCS) and are **absent from JF/PF** — the hardware loop counter is a Viperfish+ feature.
- **`BranchAbsoluteClearIbuf`** is an SCS-only branch that clears the instruction buffer; present on `vfc`/`glc`/`gfc` SCS namespaces, absent from any TC namespace.
- **`BranchRelativeRotatingPreg`** and **`SetRotatingPredicateRegister`** exist only on `gxc::gfc::isa::SparseCoreScalarAlu_*` — `6acc60406` SCS only.
- **`ReadRegisterYieldRequest`** is on `vxc` (VF) and `glc` (GL) TensorCore, absent from `gfc` — confirming `6acc60406` has no yield machinery at all.

TAC and TEC do not declare their own `Branch*`/`Call*` message types; their scalar-ALU sub-bundles (`TacScalarSubBundle`, `TecScalarSubBundle`) embed the shared `SparseCoreScalarAlu_*` op messages and reuse the SCS branch/call/halt set. Their distinct codecs (`SparseCoreTacCodecBase` keyed at type 4, `SparseCoreTecBundle`) differ in stream/DMA fields, not in the control-flow op roster, so the TAC/TEC control-flow rows mirror the SCS row of the same generation.

> **GOTCHA — naming drifts and must be normalized across generations.** The *same* op is `ScalarBranchAbsolute` on Pufferfish (`pxc::isa::TensorCoreScalar0_ScalarBranchAbsolute`), `BranchAbsolute` on Viperfish+ (`vxc::isa::TensorCoreScalarAlu_BranchAbsolute`), and a `ScalarOpcode` enum value `8..11` on Jellyfish. The indirect form is `ScalarBranchReg` on PF, `BranchSreg` on Viperfish+, and `ScalarBranchIndirect` on JF. A reimplementation keying the op table on the literal proto name will treat one logical op as four distinct ops; normalize to a generation-independent op id first.

---

## The Jellyfish `ScalarOpcode` Enum (the TC sequencer subset)

Jellyfish predates the per-op proto messages; its TensorCore sequencer ops are values of a flat 62-entry `ScalarOpcode` enum (`ScalarOpcode_descriptor()` @ `0x1fa1fc00`). The sequencer-relevant subset, anchored to the `ProtoUtils` classifiers and `.rodata` strings:

| Range | Classifier | Ops | Source | Confidence |
|---|---|---|---|---|
| 8..11 | `IsBranch` @ `0x1e876120` (`op & ~3 == 8`) | `ScalarBranch{Relative,Absolute,Indirect}` + 1 | byte-exact disasm | CONFIRMED (range); MEDIUM (per-value name) |
| 12..15 | `IsCall` @ `0x1e876140` (`op & ~3 == 12`) | `ScalarCall{Relative,Absolute,Indirect}` + 1 | byte-exact disasm | CONFIRMED (range); MEDIUM (per-value name) |
| — | — | `ScalarHalt`, `ScalarHaltOnError`, `ScalarHaltYieldConditional` | strings | CONFIRMED |
| — | — | `ScalarDelay`, `ScalarFence` | strings | CONFIRMED |
| — | — | `ScalarReadCycle{Start,End,Low,High}` | strings | CONFIRMED |

The branch range `8..11` and call range `12..15` are byte-exact from the classifier disassembly. The Jellyfish TensorCore emitter (`JellyfishEmitter`) confirms the full control-flow capability of the JF TC: it has `EmitScalarBranchWithDelay`, `EmitScalarUnconditionalCallWithDelay`, `EmitScalarIndirectBranchWithDelay`, `EmitScalarHalt`, `EmitScalarHaltYieldConditional`, and `EmitScalarDelay` — i.e. the Jellyfish TensorCore is *not* call-less, and it does have halt-yield-conditional. The Jellyfish BarnaCore address handler (`BarnaCoreAddressHandlerEmitter`) carries the same `EmitScalar*` set plus the software-loop builder `AddressHandlerProgramBuilder::BeginLoop` (`0xfa90d40`) / `EndLoop` (`0xfa91300`).

---

## Sync Ops and Where They Live

Sync-flag, barrier, and atomic ops are part of the sequencer's job, but the slot they occupy differs by generation, and they form their own per-(gen × type) inventory:

| Gen × Type | Sync slot | Sync op family | Source | Confidence |
|---|---|---|---|---|
| JF TC | vector path | `EmitVectorSyncFlag{Set,Add,SetRemote,AddRemote,PublicAccessSet}` | `JellyfishEmitter::*` | CONFIRMED |
| PF BCS | `Scalar0` **and** `Scalar1` | `Sync{Add,Done,EqualTo,GreaterOrEqualTo,GreaterThan,LessThan,NotEqualTo}` (7) | `BarnaCoreSequencerScalar{0,1}_*` | CONFIRMED |
| VF SCS / TAC / TEC | dedicated `ScalarMisc` lane | base sync family (`SetSyncFlag`, `SyncEqual`, `SyncGreaterOrEqual`, `SyncBarrier`, `AddSyncFlag`, `SmemFetchAndAdd`, atomics, `ReadSync*`) | `vfc::isa::SparseCoreScalarMisc_*` | CONFIRMED |
| GL SCS / TAC / TEC | dedicated `ScalarMisc` lane | base family **+ dual-channel** (`AddBothSyncFlag`, `SetBothSyncFlag`, `SetOtherSyncFlag`) **+ `YieldableSync*`** | `glc::isa::SparseCoreScalarMisc_*` | CONFIRMED |
| 6acc60406 SCS / TEC | dedicated `ScalarMisc` lane | base family **only** (dual-channel + Yieldable dropped) **+ `SetPOrTState`** | `gfc::isa::SparseCoreScalarMisc_*` | CONFIRMED |

The generation deltas in the sync family are byte-anchored: the dual-channel ops (`AddBothSyncFlag` / `SetBothSyncFlag` / `SetOtherSyncFlag`) and the `YieldableSync*` family (`YieldableSyncEqual`, `YieldableSyncGreater`, `YieldableSyncDone`, …) exist in `gxc::glc::isa` (Ghostlite) but are **absent from `gxc::gfc::isa`** (`6acc60406`); `6acc60406` adds the unique `SparseCoreScalarMisc_SetPOrTState`. The `ScalarMisc` lane is encoded by `EmitBarrierSync<Bundle, …ScalarMisc>` — instantiated for SCS (`0x13a5f100`), TAC (`0x139f1f80`), and TEC (`0x13a38600`) on Ghostlite — and the atomic add-return-old by `EmitFetchAndAddOp<…SmemFetchAndAdd>` (`0x13a60e00`). The barrier op sets the `ScalarMisc` present bit (`orb $0x4`) and writes the sflag id/threshold through a `ScalarY` operand whose immediate form reuses immediate slot 0.

> **NOTE — the `ScalarMisc` lane is distinct from the `ScalarAlu0` sequencer lane.** On Viperfish+, sync ops do not share the lane-0 sequencer slot; they occupy a separate `ScalarMisc` lane in the SparseCore bundle. The `ScalarAlu0` lane owns PC mutation (branch/call/halt); the `ScalarMisc` lane owns sync. A bundle can issue both a branch and a sync op in the same cycle because they are different lanes. Jellyfish is the exception — its sync work is in the vector path, not a scalar lane at all.

---

## Per-Generation Evolution Summary

| Gen | Sequencer evolution (byte-anchored) |
|---|---|
| **Jellyfish v2** | TensorCore has full Branch/Call (incl. indirect via BTR), Halt, HaltYieldConditional, Delay, Fence, ReadCycle. No LCC register; hardware loop is a software bundle-index backward branch (BCAH `BeginLoop`/`EndLoop`). Sync via the vector path. 5-bit predication (15 regs + always=15 + never=31). |
| **Pufferfish v4** | Renames to `Scalar`-prefix + `Reg` suffix (`ScalarBranchReg`, `ScalarCallReg`). BCS sequencer gains a 7-op sync family in both `Scalar0` and `Scalar1`. No LCC register; no HaltYield. |
| **Viperfish v5p (+v5e lite)** | Drops the `Scalar` prefix, renames `Reg`→`Sreg`, adds explicit `BranchSreg::x()` / `CallSreg::{x,dest}`. Introduces the hardware LCC read (`ReadRegisterLcc{Low,High}`) on TC + SCS, `HaltYield` + `HaltYieldConditional`, and the dedicated `ScalarMisc` sync lane on the SparseCore engines. `BranchAbsoluteClearIbuf` is SCS-only. |
| **Ghostlite v6e** | Drops unconditional `HaltYield` (keeps `HaltYieldConditional`). The SparseCore `ScalarMisc` family grows: dual-channel sync (`AddBothSyncFlag` / `SetBothSyncFlag` / `SetOtherSyncFlag`) + the `YieldableSync*` family. |
| **`6acc60406` v7** | Drops yieldable execution entirely (no `HaltYield`, no `HaltYieldConditional`, no `ReadRegisterYieldRequest`, no `YieldableSync*`). Drops the TAC sequencer and the dual-channel sync ops. Adds `gfc`-only SCS ops `BranchRelativeRotatingPreg` + `SetRotatingPredicateRegister` and `ScalarMisc_SetPOrTState`. Adds dual predication (a `6acc60406` conditional branch can be guarded by either of two per-bundle predicates). |

---

## Cross-References

- [Sequencer Slot](slot-sequencer.md) — the slot identity, three-layer encode model, and the branch/call/halt/delay field layout.
- [Bundle Model](bundle-model-overview.md) — the codec keyed by `(TpuVersion, TpuSequencerType)` and the per-gen bundle widths.
- [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md) — the full per-generation sub-core / sequencer-type inventory.
