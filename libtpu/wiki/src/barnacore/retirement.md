# Retirement Evidence

> *Every status-stub body, error string, source-line tag, per-generation symbol count, embedding-op name, and `GetNumberOfBarnaCores` return value on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`) — from the demangled C++ symbol table, the embedded proto/op-name strings, and the decompiled bodies of the per-family `TpuCore<Fam>DriverImpl` BarnaCore vfuncs and the `<fam>::HardwareAttributes::GetNumberOfBarnaCores` accessors. Addresses and counts apply to this build; other versions differ.*

## Abstract

**BarnaCore is the TPU's legacy embedding / sparse-lookup coprocessor — the engine SparseCore replaced.** It is live silicon on the three oldest generations this binary knows (Jellyfish, Dragonfish, Pufferfish) and is *retired* on Viperfish and later, where SparseCore does the same job. This page is the *binary proof of the retirement* — not the BarnaCore architecture itself ([overview](overview.md), [BCS scalar ISA](bcs-scalar-isa.md), [merged-ALU layout](merged-alu.md), [per-gen perf grids](per-gen-perf-grids.md) own that), and not the SparseCore replacement engine ([SparseCore overview](../sparsecore/overview.md) owns that). What this page owns is the *evidence that the engine is dead on v5+*, drawn from three independent readings of the binary that all agree.

The first reading is the **runtime-driver stub bodies**. The `tpu::TpuCore` driver vtable carries a fourteen-vfunc BarnaCore control plane (`SetBarnaCoreRowLength`, `EnableBarnaCore`, `SetBarnaCoreAddressHandlerProgram`, …) on *every* generation's driver impl — the ABI is gen-invariant. But on the Viperfish driver (`TpuCoreVxcDriverImpl`, the v3/v4/v5 family driver) every one of those twelve override bodies is a one-line error stub returning `"Not implemented in Viperfish."` (status code `kUnimplemented` = 12). The corresponding Jxc/Pxc bodies are real register-programming code. The ABI is preserved; the implementation is deleted.

The second reading is the **per-generation symbol presence proof**. BarnaCore encoder / codec / ISA symbols exist in bulk under the legacy family namespaces — 563 under `jxc`/`jellyfish`, 34 under `pxc`/`pfc` (`EncoderBcsDf`, `EncoderPfBarnaCoreSequencer`, `EncoderPfBarnaCoreChannel`) — and number **exactly zero** under `vxc`/`vfc`/`gxc`/`glc`/`gfc`. The hardware-count accessor confirms it numerically: `pfc::HardwareAttributes::GetNumberOfBarnaCores()` returns `4`, while `vxc::` and `gxc::HardwareAttributes::GetNumberOfBarnaCores()` both return `0`.

The third reading is the **lowered-away embedding ops**. The device-path TPUEmbedding op family that drove the BarnaCore pipeline (the `Enqueue*` / `Recv*Activations` / `Send*Gradients` lookup ops) is, on v3+, lowered through the SparseCore `XlaSparseDenseMatmul*` primitive family and the `SparseDenseMatmulDotCombinerEmitter` instead of the BarnaCore `barna_core::BcsLloEmitter`. The eleven-optimizer BarnaCore-era parameter API collapses to a five-optimizer SparseCore set, because the optimizer math moved onto the SparseCore TEC engine.

For reimplementation, the contract is:

- **The BarnaCore control-plane ABI is gen-invariant; its v3+ implementation is a stub.** A reimplementer must carry the fourteen `*BarnaCore*` driver vfuncs in the vtable for *all* generations (callers expect the slots), but the v5+ bodies must fail closed — `kUnimplemented`, not silent no-op. Calling any of them on a Viperfish-family target is a hard error by design.
- **Per-gen presence is a symbol-namespace readout.** Live BarnaCore = a non-empty `EncoderBcsDf` / `EncoderPf*BarnaCore*` codec roster scoped under `jxc` (v0/v1) or `pxc.pfc` (v2). v3+ has *no* BarnaCore codec, encoder, ISA table, or subtarget. The discriminator is the family namespace plus `GetNumberOfBarnaCores() != 0`.
- **The embedding op set retires onto SparseCore primitives, not onto BarnaCore.** On v3+ the device-path embedding ops lower to `XlaSparseDenseMatmul{,GradWith<Opt>}` and the `SparseDenseMatmulDotCombinerEmitter`. The BarnaCore `BcsLloEmitter` gather/scatter/infeed path is never reached.

| | |
|---|---|
| **What this page proves** | BarnaCore is a *retired* engine on Viperfish/Ghostlite/Trillium — present as ABI/enum vestige, absent as silicon |
| **Live on** | Jellyfish (v0) · Dragonfish (v1) · Pufferfish (v2) — `GetNumberOfBarnaCores()` ∈ {≥1, 4} |
| **Retired on** | Viperfish (v3) · Ghostlite (v4) · Trillium (v5) — `GetNumberOfBarnaCores()` = `0`; zero BC codecs |
| **Stub evidence** | 12× `TpuCoreVxcDriverImpl::*BarnaCore*` vfuncs → `"Not implemented in Viperfish."` (`MakeErrorImpl<12>`) |
| **Symbol-count proof** | BC encoder/codec syms: `jxc` 563 · `pxc/pfc` 34 · `vfc/glc/gfc` **0** |
| **Replacement** | SparseCore (`XlaSparseDenseMatmul*` + `SparseDenseMatmulDotCombinerEmitter`) — see [SparseCore overview](../sparsecore/overview.md) |
| **Confidence** | CONFIRMED (decompiled stub bodies + symbol counts + return-value constants) unless a row or callout says otherwise |

> **NOTE — this page owns the retirement *evidence*, not the engines.** The BarnaCore architecture is owned by [overview](overview.md) and its ISA siblings; the SparseCore replacement engine by [SparseCore overview](../sparsecore/overview.md); the SparseCore combiner that took over the embedding reduce by [SampleCombiner Emitter](../sparsecore/sample-combiner-emitter.md). They are linked, not repeated.

---

## Evidence 1 — The Viperfish Driver Stub Bodies

The `tpu::TpuCore` runtime driver exposes a BarnaCore control plane as a block of consecutive vtable vfuncs (the per-family override region documented under [overview](overview.md)). The vfunc *names* are BarnaCore-specific and the *slots* are present in every family's driver impl — `TpuCoreJxcDriverImpl` (v0/v1), `TpuCorePxcDriverImpl` (v2), and `TpuCoreVxcDriverImpl` (v3/v4/v5). The ABI does not change across the retirement.

What changes is the body. On the Viperfish-family driver, every BarnaCore vfunc is a single-line error return. Decompiled verbatim:

```c
// _ZN3tpu20TpuCoreVxcDriverImpl15EnableBarnaCoreEv  @ 0x1d118cc0
__int64 tpu::TpuCoreVxcDriverImpl::EnableBarnaCore(tpu::TpuCoreVxcDriverImpl *this)
{
  return absl::status_internal::MakeErrorImpl<12>(
      "Not implemented in Viperfish.", 29, 185,
      "learning/45eac/tpu/runtime/hal/internal/vxc/tpu_core_vxc_driver_impl.cc");
}

// _ZN3tpu20TpuCoreVxcDriverImpl21SetBarnaCoreRowLengthEi  @ 0x1d118be0
__int64 tpu::TpuCoreVxcDriverImpl::SetBarnaCoreRowLength(tpu::TpuCoreVxcDriverImpl *this)
{
  return absl::status_internal::MakeErrorImpl<12>(
      "Not implemented in Viperfish.", 29, 146,
      "learning/45eac/tpu/runtime/hal/internal/vxc/tpu_core_vxc_driver_impl.cc");
}
```

`MakeErrorImpl<12>` constructs an `absl::Status` with code `12` — `absl::StatusCode::kUnimplemented`. The `29` is the message length (`"Not implemented in Viperfish."`), and the trailing integer is the source line in `tpu_core_vxc_driver_impl.cc`. All twelve BarnaCore vfunc overrides on the Vxc driver are byte-identical in shape, differing only by source line:

| Vxc vfunc | `.text` | Source line | Body |
|---|---|---:|---|
| `SetBarnaCoreRowLength` | `0x1d118be0` | 146 | `kUnimplemented "Not implemented in Viperfish."` |
| `SetBarnaCoreAddressHandlerProgram` | `0x1d118c00` | 152 | `kUnimplemented` |
| `SetBarnaCoreFeatureInfo` | `0x1d118c20` | 160 | `kUnimplemented` |
| `SetBarnaCoreFeatureAddress` | `0x1d118c40` | 165 | `kUnimplemented` |
| `SetBarnaCoreFeaturePCs` | `0x1d118c60` | 171 | `kUnimplemented` |
| `SetBarnaCorePseudoChannelBit` | `0x1d118c80` | 176 | `kUnimplemented` |
| `SetBarnaCoreErrorMask` | `0x1d118ca0` | 181 | `kUnimplemented` |
| `EnableBarnaCore` | `0x1d118cc0` | 185 | `kUnimplemented` |
| `EnableBarnaCoreHbmMuxWorkaround` | `0x1d118ce0` | 189 | `kUnimplemented` |
| `SetBarnaCoreHbmMuxNodeFabricModeTimer` | `0x1d118d00` | 194 | `kUnimplemented` |
| `SetBarnaCoreHbmMuxBfifoModeTimer` | `0x1d118d20` | 199 | `kUnimplemented` |
| `GetBarnaCoreLastIssuedInstructionTag` | `0x1d118d40` | 204 | `kUnimplemented` |

The sequential source-line range (146 → 204, monotone with `.text` address) shows these are not scattered ad-hoc stubs but a *contiguous block of deliberate "this engine is gone" stubs* written in one place in the Viperfish driver source — the runtime-side fingerprint of the retirement.

### Contrast — the live Jellyfish body

The same vfunc on the Jellyfish driver is real register-programming code, not a stub:

```c
// _ZN3tpu20TpuCoreJxcDriverImpl15EnableBarnaCoreEv  @ 0xe735540
__int64 tpu::TpuCoreJxcDriverImpl::EnableBarnaCore(tpu::TpuCoreJxcDriverImpl *this)
{
  if ( **(_DWORD **)(**((_QWORD **)this + 1) + 8LL) != 1 )      // gate on chip state
    return (*(... **)(**((_QWORD **)this + 101) + 296LL))(*((_QWORD *)this + 101), 2);
  v1 = (*(... **)(**((_QWORD **)this + 101) + 352LL))(*((_QWORD *)this + 101), 0);  // HW write
  if ( v1 == 1 )
    return (*(... **)(**((_QWORD **)this + 101) + 296LL))(*((_QWORD *)this + 101), 2);
  inited = util::StatusBuilder::InitRepImpl(v1);
  return util::StatusBuilder::CreateStatusAndConditionallyLog(
      257, "learning/45eac/tpu/runtime/hal/internal/jxc/tpu_core_jxc_driver_impl.cc", inited);
}
```

The Jxc body dispatches through the chip-register interface (the `+296` / `+352` vtable indirections into the register backend at `this+101`) to actually enable the BarnaCore — exactly the kind of body a *live* engine's `Enable` has. The Pxc (Pufferfish) driver likewise carries real bodies. The retirement boundary is therefore visible as *stub-body vs real-body* on the identically-named vfunc across the Vxc / Jxc-Pxc drivers.

> **CONFIRMED — the BarnaCore control-plane ABI survives the retirement; only the body dies.** A reimplementer must reproduce the vfunc *slots* on the v5+ driver (callers in the gen-agnostic HAL layer reference them by vtable index), but the bodies must return `kUnimplemented`. This is the canonical "preserve the ABI, delete the implementation" retirement pattern, and it is byte-confirmed: same demangled vfunc name, real body on `jxc`/`pxc`, `"Not implemented in Viperfish."` on `vxc`.

---

## Evidence 2 — Per-Generation Symbol Presence Proof

The second, independent proof is a census of BarnaCore *engine* symbols (encoders, codecs, ISA classes) per generation family namespace. A live engine ships a codec/encoder roster; a retired one ships none. Counting decompiled BarnaCore-named files that are encoder / codec / ISA classes, bucketed by family namespace:

| Family ns | Gen(s) | BC encoder/codec/ISA syms | `GetNumberOfBarnaCores()` | Verdict |
|---|---|---:|:---:|---|
| `jxc` / `jellyfish` | Jellyfish (v0), Dragonfish (v1) | **563** | (≥1, JF/DF) | **LIVE** |
| `pxc` / `pfc` | Pufferfish (v2) | **34** | **`4`** (`pfc`) | **LIVE** |
| `vxc` / `vfc` | Viperfish (v3) | **0** | **`0`** (`vxc`) | **RETIRED** |
| `gxc` / `glc` | Ghostlite (v4) | **0** | **`0`** (`gxc`) | **RETIRED** |
| `gxc` / `gfc` | Trillium (v5) | **0** | — | **RETIRED** |

The split is razor-sharp at the v2→v3 boundary. The legacy families carry hundreds of BarnaCore symbols; the SparseCore-era families carry exactly zero BarnaCore encoder/codec/ISA symbols. There is *no* `EncoderVfBarnaCore*`, no `vfc::isa::BarnaCore*Codec`, no v3+ BarnaCore subtarget.

### The live Pufferfish BarnaCore codec roster

The 34 `pxc`/`pfc` symbols are the high-water mark of BarnaCore — a fully independent VLIW embedding sequencer. The encoder leaves that survive in `pxc.pfc`:

```text
EncoderBcsDf                 ; v0/v1 BarnaCore (address-handler personality, 16-byte bundle)
EncoderPfBarnaCoreSequencer  ; v2 BarnaCore Sequencer (BCS, seq=1, 32-byte VLIW bundle)
EncoderPfBarnaCoreChannel    ; v2 BarnaCore Channel
```

These feed the TPU MC code emitter (`llvm::TPUMCCodeEmitter::getBinaryCodeForInstr` @ `0x13c74da0`), which carries a *second-personality* instruction-encoding table, `InstBits_BarnaCorePxcHwMode`, selected by a subtarget HwMode bit — the only HwMode-gated InstBits table in the whole TPU backend. No `InstBits_BarnaCore*HwMode` exists for any v3+ family; the retirement of BarnaCore also retired the embedding-coprocessor HwMode mechanism. (The ISA-table detail is owned by [BCS scalar ISA](bcs-scalar-isa.md) and [merged-ALU layout](merged-alu.md); here it serves only as presence evidence.)

### The numeric count accessor

The cleanest single datum is the hardware-attributes accessor that reports how many BarnaCores a chip has. The bodies are trivial constants:

```c
// pxc::pfc::HardwareAttributesCommon<...>::GetNumberOfBarnaCores()  @ 0x1fbac480
__int64 ...::GetNumberOfBarnaCores() { return 4; }     // Pufferfish: 4 BarnaCores

// vxc::HardwareAttributes::GetNumberOfBarnaCores()  @ 0x1fbad0e0
__int64 ...::GetNumberOfBarnaCores(... *this) { return 0; }   // Viperfish: none

// gxc::HardwareAttributes::GetNumberOfBarnaCores()
__int64 ...::GetNumberOfBarnaCores(... *this) { return 0; }   // Ghostlite/Trillium: none
```

Pufferfish reports `4` — consistent with the `DMA_CORE_ID_BARNA_CORE_0..3` reservation in the SparseCore DMA fabric's `DmaCoreId` enum (four BarnaCore core IDs). Viperfish and the Ghostlite/Trillium family both report `0`. (The `pxc::plc` variant also returns `0`, a per-config `plc` attributes object rather than the `pfc` chip attributes; the live count is the `pfc` `return 4`.)

> **GOTCHA — `vxc`/`gxc` still contain *some* BarnaCore symbols, but only the ABI vestige.** A grep for `BarnaCore` under `vxc` returns 12 hits — but every one is a `TpuCoreVxcDriverImpl::*BarnaCore*` vfunc *stub* (Evidence 1) or the `vxc::HardwareAttributes::GetNumberOfBarnaCores() { return 0; }` accessor. None is an encoder, codec, ISA class, or subtarget. "Zero live BarnaCore engine symbols on v3+" means zero codec/encoder/ISA symbols; the runtime-ABI stubs are exactly the vestige Evidence 1 documents.

---

## Evidence 3 — The Lowered-Away Embedding Ops

The third proof is the embedding *operation* set. BarnaCore existed to execute the device-side embedding lookup/gather/scatter that a recommender model's graph emits. On v3+ those same model-graph ops are lowered through SparseCore primitives instead — the BarnaCore embedding emitter is never reached.

### The device-path embedding op family

The TPUEmbedding op family in this binary splits into a host-setup band (`Configure*` / `Connect*` / `Collate*`), an optimizer-parameter-staging band (`Load*` / `Retrieve*`, eleven optimizers), and a **device-path lookup band** — the ops a training step actually emits per minibatch. The device-path band, by exact symbol count:

| # | Op | Role |
|---:|---|---|
| 1 | `EnqueueTPUEmbeddingBatchOp` | enqueue a dense-batch lookup |
| 2 | `EnqueueTPUEmbeddingIntegerBatchOp` | enqueue an integer-id batch |
| 3 | `EnqueueTPUEmbeddingSparseBatchOp` | enqueue a sparse (indices/values) batch |
| 4 | `EnqueueTPUEmbeddingSparseTensorBatchOp` | enqueue a sparse-tensor batch |
| 5 | `EnqueueTPUEmbeddingRaggedTensorBatchOp` | enqueue a ragged-tensor batch |
| 6 | `EnqueueTPUEmbeddingTensorBatchOp` | enqueue a dense-tensor batch |
| 7 | `EnqueueTPUEmbeddingArbitraryTensorBatchOp` | enqueue an arbitrary-tensor batch |
| 8 | `CreateEnqueueTPUEmbeddingArbitraryTensorBatchOp` | fixed-state enqueue constructor |
| 9 | `DynamicEnqueueTPUEmbeddingArbitraryTensorBatchOp` | dynamic-shape enqueue |
| 10 | `RecvTPUEmbeddingActivationsOp` | receive forward activations |
| 11 | `RecvTPUEmbeddingDeduplicationDataOp` | receive dedup metadata |
| 12 | `SendTPUEmbeddingGradientsOp` | send backward gradients |
| 13 | `LowerRecvTPUEmbeddingActivationsOp` | lowering of (10) |
| 14 | `LowerRecvTPUEmbeddingDeduplicationDataOp` | lowering of (11) |
| 15 | `LowerSendTPUEmbeddingGradientsOp` | lowering of (12) |
| 16 | `XlaRecvTPUEmbeddingActivationsOp` | XLA forward-activations op |
| 17 | `XlaRecvTPUEmbeddingDeduplicationDataOp` | XLA dedup-data op |
| 18 | `XlaSendTPUEmbeddingGradientsOp` | XLA gradient op |

That is the ~20-op device-path embedding surface (18 distinct symbols by exact count; the round figure includes the `Recv`/`Send` activation/gradient pair counted with their dedup-data companions). All of them are the *front* of the embedding pipeline that, on a BarnaCore chip, terminates in the `barna_core::BcsLloEmitter`, and on a SparseCore chip terminates in the SparseCore combiner.

### Where they used to lower — and where they lower now

Both emitters ship in this one binary; which one runs is the retirement:

| Stage | BarnaCore path (v0–v2) | SparseCore path (v3–v5) |
|---|---|---|
| Embedding gather | `barna_core::BcsLloEmitter::IssueDmaInfeedToVmem` (`@ 0xf9d77e0`) | SparseCore stream-gather → `SparseDenseMatmulDotCombinerEmitter` ([combiner](../sparsecore/sample-combiner-emitter.md)) |
| Per-sample reduce | folded into BCS DMA-infeed | `SparseDenseMatmulDotCombinerEmitter::EmitSampleCombiner` (`@ 0x1332c640`) |
| Per-id loop | BCS sequencer FSM | `::EmitValencyLoop` (`@ 0x1332cee0`) |
| Gradient scatter-add | `BcsLloEmitter::IssueDmaScatter` (`@ 0xf9d8400`) | `STREAM_OPCODE_SCATTER_FLOAT_ADD` (atomic, in-HBM) |
| Lookup primitive | (BarnaCore lookup program) | `XlaSparseDenseMatmul{,WithCsrInput,WithStaticBufferSize}Op` |

The `barna_core::BcsLloEmitter` (`IssueDmaScatter`, `IssueDmaScatterOne`, `IssueDmaInfeedToVmem`, `WaitForInfeedToVmemDma`) is the legacy lookup emitter; the `SparseDenseMatmulDotCombinerEmitter` (`Emit` / `EmitSampleCombiner` / `EmitValencyLoop` / `EmitVectorizedLoop`) is its replacement. Both are present — the binary supports all six generations — but the v3+ codegen reaches only the SparseCore one.

### The optimizer set shrinks onto the TEC

The most legible "lowered away" signal is the embedding-optimizer set. The BarnaCore-era TF parameter API supports **eleven** optimizer algorithms (each with `Load*`/`Retrieve*` parameter ops, some with a `GradAccumDebug` variant):

```text
Adadelta · Adagrad · ADAM · CenteredRMSProp · FTRL · MDLAdagradLight ·
Momentum · ProximalAdagrad · ProximalYogi · RMSProp · StochasticGradientDescent
```

The SparseCore-era op family carries a smaller, modern subset, because the optimizer update now runs *on the SparseCore TEC vector engine* as a fused gradient op rather than as a host-staged parameter table:

```text
XlaSparseCore:               Adagrad · AdagradMomentum · Adam · Ftrl · Sgd  (+ GraphConvolution)
XlaSparseDenseMatmulGradWith: Adagrad · AdagradMomentum · Adam · Ftrl · Sgd
```

The eleven host-staged BarnaCore optimizers collapse to five on-engine SparseCore optimizers (`XlaSparseDenseMatmulGradWith{Adagrad,AdagradMomentum,Adam,Ftrl,Sgd}AndCsrInputOp` and their `StaticBufferSize` variants). The legacy `Load/RetrieveTPUEmbedding<Opt>Parameters` ops remain in the binary for back-compat with older programs, but the v3+ embedding update does not route through them.

> **NOTE — both emitter families ship; the *reached* one is the retirement.** `libtpu.so` is a single fat artifact serving all six generations, so it necessarily contains both `barna_core::BcsLloEmitter` and `SparseDenseMatmulDotCombinerEmitter`. The retirement is not "the BarnaCore emitter was deleted from the binary" — it is "no v3+ target's lowering ever selects it." The selection happens upstream, by target generation: a Pufferfish target lowers embeddings to the BCS emitter; a Viperfish+ target lowers them to the SparseCore combiner. This page documents the *that-it-happens*; the SparseCore combiner mechanics are owned by [SampleCombiner Emitter](../sparsecore/sample-combiner-emitter.md).

---

## Putting It Together — The Retirement Boundary

The three readings agree on a single, sharp cut at the v2→v3 (Pufferfish→Viperfish) boundary:

| Gen | TpuVer | Family ns | Driver BC vfuncs | BC codec syms | `#BarnaCores` | Embedding emitter | Status |
|---|---|---|---|---:|:---:|---|---|
| Jellyfish | 0 | `jxc` | real bodies (`jxc` driver) | 563 (jxc/jf) | ≥1 | `BcsLloEmitter` | **LIVE** |
| Dragonfish | 1 | `jxc` | real bodies (`jxc` driver) | (shares jxc) | ≥1 | `BcsLloEmitter` | **LIVE** |
| Pufferfish | 2 | `pxc.pfc` | real bodies (`pxc` driver) | 34 | **4** | `BcsLloEmitter` | **LIVE (peak)** |
| Viperfish | 3 | `vxc.vfc` | `"Not implemented in Viperfish."` | **0** | **0** | `SparseDenseMatmulDotCombinerEmitter` | **RETIRED** |
| Ghostlite | 4 | `gxc.glc` | (shares vxc stub family) | **0** | **0** | `SparseDenseMatmulDotCombinerEmitter` | **RETIRED** |
| Trillium | 5 | `gxc.gfc` | (shares vxc stub family) | **0** | — | `SparseDenseMatmulDotCombinerEmitter` | **RETIRED** |

Pufferfish is the high-water mark — a full BCS sequencer, four BarnaCores, a dedicated HwMode encoding table, and real driver bodies. Viperfish is the cut — zero codecs, zero BarnaCores, `kUnimplemented` driver stubs, and the embedding workload re-homed onto SparseCore. Everything that *remains* on v3+ is back-compat shell: the driver vfunc slots (stubbed), the `TpuSequencerType` BCS=1/BCAH=2 enum values (never selected), the `DMA_CORE_ID_BARNA_CORE_0..3` DMA-routing enum (never routed to), and the `GetNumberOfBarnaCores() == 0` accessor that tells the runtime there is nothing there.

> **WARNING — do not mistake the surviving ABI for a live engine.** A reimplementer or analyst who sees `*BarnaCore*` driver vfuncs in the Viperfish vtable, or `BARNA_CORE` values in the v5+ DMA enums, might conclude BarnaCore is still active on those chips. It is not. Every such surface is vestigial: the vfuncs return `kUnimplemented`, the DMA core IDs route to nothing (`GetNumberOfBarnaCores() == 0`), and no codec exists to encode a BarnaCore program. The presence of a name is not the presence of an engine.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| 12 `TpuCoreVxcDriverImpl::*BarnaCore*` vfuncs are `kUnimplemented` stubs | decompiled bodies `0x1d118be0`–`0x1d118d40`, all `MakeErrorImpl<12>("Not implemented in Viperfish.", …, tpu_core_vxc_driver_impl.cc)`, source lines 146–204 | CONFIRMED |
| The same vfunc has a real register-programming body on `jxc`/`pxc` | `TpuCoreJxcDriverImpl::EnableBarnaCore` `@ 0xe735540` dispatches through the register backend; Pxc likewise | CONFIRMED |
| BC encoder/codec/ISA syms: `jxc` 563, `pxc/pfc` 34, `vfc/glc/gfc` 0 | decompiled-file census bucketed by family namespace (`Encoder`/`Codec`/`isa` ∩ `BarnaCore`) | CONFIRMED |
| Pufferfish has 4 BarnaCores; Viperfish/Ghostlite have 0 | `pfc::…GetNumberOfBarnaCores() {return 4;}` `@ 0x1fbac480`; `vxc::` `@ 0x1fbad0e0` and `gxc::` both `{return 0;}` | CONFIRMED |
| Live Pufferfish BC encoders are `EncoderBcsDf`, `EncoderPfBarnaCoreSequencer`, `EncoderPfBarnaCoreChannel` | demangled symbol names under `pxc.pfc` | CONFIRMED |
| `InstBits_BarnaCorePxcHwMode` is the only HwMode BC encoding table; none for v3+ | `TPUMCCodeEmitter::getBinaryCodeForInstr` `@ 0x13c74da0`; no `InstBits_BarnaCore*HwMode` under v3+ families | HIGH |
| Device-path embedding op set is the ~20-op `Enqueue*`/`Recv*`/`Send*` family | 18 distinct `*(Enqueue\|Recv\|Send)TPUEmbedding*Op` symbols enumerated | CONFIRMED (count); "~20" framing HIGH |
| BarnaCore lookup emitter = `barna_core::BcsLloEmitter`; v3+ replacement = `SparseDenseMatmulDotCombinerEmitter` | both symbol families present; `BcsLloEmitter::{IssueDmaInfeedToVmem,IssueDmaScatter}` vs combiner `Emit*` | CONFIRMED (presence); per-target selection HIGH |
| 11 BarnaCore-era optimizers collapse to 5 on-engine SparseCore optimizers | 11 distinct `(Load\|Retrieve)TPUEmbedding<Opt>Parameters`; 5 distinct `XlaSparseDenseMatmulGradWith<Opt>` | CONFIRMED |
| The v3+ embedding lowering never reaches the BC emitter | inferred from zero BC codecs + `GetNumberOfBarnaCores()==0` + the SC combiner being the v3+ lowering | HIGH (target-selection not body-traced) |

---

## Cross-References

- [BarnaCore Overview](overview.md) — the legacy embedding accelerator this page proves is retired; the per-gen presence matrix in full.
- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the BarnaCore control+memory ISA whose encoders (`EncoderPfBarnaCoreSequencer`) are the live-engine evidence counted here.
- [Merged-ALU Bit Layout](merged-alu.md) — the BarnaCore VLIW bundle layout backing the `InstBits_BarnaCorePxcHwMode` table.
- [Per-Gen BarnaCore Perf Grids](per-gen-perf-grids.md) — the Pufferfish-only `BarnaCorePFSchedModel` scheduling tables (no v3+ counterpart).
- [SparseCore Overview](../sparsecore/overview.md) — the replacement engine, present from Viperfish onward; the SCS/TAC/TEC three-engine split.
- [SampleCombiner Emitter](../sparsecore/sample-combiner-emitter.md) — the SparseCore embedding combiner (`SparseDenseMatmulDotCombinerEmitter`) the device-path embedding ops lower to on v3+, replacing the BarnaCore `BcsLloEmitter`.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4) — [back to index](../index.md)
