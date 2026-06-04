# The TPU Compiler

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

The TPU compiler is the half of `libtpu.so` that turns a serialized XLA program into the packed VLIW bundles a TensorCore executes. It is **not** one monolithic lowering. It is a five-phase, multi-dialect descent that begins as portable StableHLO bytecode handed across the PjRt boundary and ends as a per-generation bundle byte stream, threading through six successive IR levels and four kinds of optimization machinery on the way down. The whole descent is reachable from one class — `xla::jellyfish::DeepseaCompilerBase` — whose `RunHloPasses` (`0x109152a0`) and `RunBackend` family drive the HLO-level work, and from the phase registry `xla::TpuCompiler::RegisterAllPhases` (`0xf849ec0`) that wires the five `xla::CompilePhase*` entry points into the PjRt partial-program pipeline.

The reader who knows LLVM should hold a layered analogy. The first half of the pipeline is *XLA proper* — an HLO-on-`HloInstruction` optimizer with layout assignment, fusion, a memory-space allocator (MSA, XLA's analogue of register allocation but for HBM/VMEM tiers), and a latency-hiding scheduler — structurally the same machinery upstream XLA ships for GPU, retargeted with a TPU cost model and a TPU layout model. The second half is *MLIR* — the program crosses from `HloInstruction` into the MLIR world (StableHLO/CHLO/MHLO already live here on ingestion; the `tpu` dialect is the TPU-specific target dialect) and descends through dialect-conversion legalizers to the `jellyfish::Llo*` low-level IR, the TPU's equivalent of LLVM `MachineInstr`. From LLO, the [scheduling pipeline](../sched/overview.md) packs bundles and the per-generation encoder serializes bytes. A side door — the Mosaic kernel compiler — lets hand-written kernels enter the `tpu` dialect directly through `tpu_custom_call` HLOs, bypassing the HLO optimizer entirely.

This is the orientation page for Part V. It fixes the IR layer stack, names the five compile phases and the four pass families that run inside them, says where each lives in the binary, and maps the rest of Part V so a reader can jump to the page that owns each phase's detail. It does **not** reproduce any phase's algorithm — the HLO pass registry, layout assignment, MSA, fusion, the MLIR legalizers, the tpu→LLO ODS lowering, and Mosaic each have their own pages, cross-referenced below.

For reimplementation, the orientation contract is:

- **The IR layer stack and its crossing points.** Six levels — StableHLO/CHLO/VHLO bytecode → XLA HLO → MHLO/`tpu`-dialect MLIR (via TLP) → LLO → scheduled `Bundle`s → packed bytes — and the two boundaries that are *format* crossings, not mere passes: the StableHLO↔HLO legalization on ingestion, and the HLO→MLIR crossing inside Phase 2a.
- **The five-phase spine and what each consumes/produces.** `CompilePhase0StablehloToHlo` through `CompilePhase3Linking`, registered by `RegisterAllPhases`, each a `PjRtPartialProgramProto`-in / `PjRtPartialProgramProto`-out stage.
- **The four pass families and where they sit.** HLO optimization passes (Phase 1), the analyses that gate codegen (layout assignment, MSA), the lowering legalizers (Phase 2a/2b), and the schedule+pack back end (Part VIII, after LLO).
- **The Mosaic side channel.** Why a second IR producer exists and how its output rejoins the main descent at the `tpu` dialect.

| | |
|---|---|
| **Compiler class** | `xla::jellyfish::DeepseaCompilerBase` (Deepsea = internal TPU backend name) |
| **HLO pass driver** | public entry `xla::jellyfish::DeepseaCompilerBase::RunHloPasses(unique_ptr<HloModule>, StreamExecutor*, CompileOptions const&)` @ `0x109152a0` — the `xla::Compiler` virtual override, which delegates to the internal pipeline-running overload `RunHloPasses(Target const&, RunHloPassesConfig const&, …)` @ `0x1093a420` that actually builds and runs the `HloPassPipeline` (the C `TpuCompiler_RunHloPasses` export @ `0xeabcd80` is the PjRt-side wrapper). See [Compile Phases](compile-phases.md). |
| **Phase registry** | `xla::TpuCompiler::RegisterAllPhases` @ `0xf849ec0` |
| **Phase entry points** | `CompilePhase0StablehloToHlo` `0xf84de60` · `CompilePhase1HloOptimizations` `0xf84ee00` · `CompilePhase2aTlpLowering` `0xf850840` · `CompilePhase2bDedupedLowering` `0xf852180` · `CompilePhase3Linking` `0xf852f40` |
| **Phase I/O type** | `absl::Span<const xla::PjRtPartialProgramProto>` in / out (PjRt partial-program protocol) |
| **IR levels** | StableHLO/CHLO/VHLO → XLA HLO → TLP/MHLO + `tpu` dialect → LLO (`jellyfish::Llo*`) → `Bundle` → packed bytes |
| **MLIR dialects present** | `stablehlo`, `chlo`, `vhlo`, `mhlo`, `tpu` (verified in RTTI: `mlir::{mhlo,tpu,chlo,vhlo,stablehlo}::*`) |
| **tpu→LLO lowering** | `mlir::tpu::createLowerToLLOPass` @ `0x11203ba0` (`LowerToLLOPass`); `mlir::tpu::LowerPassBase` family |
| **LLO → bundles** | `PackBundles` @ `0x10a30a20` (back end; see Part VIII) |
| **Mosaic side door** | `tpu_custom_call` HLO → `tpu` dialect via `mlir::tpu::CanonicalizeMosaicPass` / `MosaicSerdePass`; LLO emission driven by `jellyfish::CustomCallEmitter::Emit` @ `0x111ef740` (`MosaicEmitter::EmitWindow` @ `0xfaadcc0` is its per-window helper) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The IR Layer Stack

A TPU program is not lowered through one IR but descends through a stack of six, two of which sit in upstream XLA's `HloInstruction` world and four of which sit in MLIR. The crossing between those two worlds is the single most important structural fact on this page: the front half is *XLA*, the back half is *MLIR*, and the program is reconstituted across the boundary, not merely walked.

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 0 — PORTABLE WIRE IR (MLIR bytecode, crosses the PjRt boundary)     │
│    StableHLO · CHLO (high-level "client" HLO) · VHLO (versioned)           │
│    mlir::{stablehlo,chlo,vhlo}::*   — what JAX/the bridge actually ships    │
└──────────────────────────────────────────────────────────────────────────┘
        │  Phase 0 — CompilePhase0StablehloToHlo  (format crossing, not a pass)
        │  ConvertStablehloToHlo / StablehloLegalizeToHlo / ChloLegalizeToHlo
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 1 — XLA HLO   (HloModule / HloInstruction, the HloOpcode enum)      │
│    the optimizer's home: HLO opt passes, layout assignment, fusion, MSA    │
│    DeepseaCompilerBase::RunHloPasses  0x109152a0                           │
└──────────────────────────────────────────────────────────────────────────┘
        │  Phase 2a — CompilePhase2aTlpLowering  (HLO → MLIR crossing)
        │  TlpLowering → TLPFunction (TPU-Level Program); enters MLIR
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 2 — MLIR HIGH/MID   (MHLO + TLP) → tpu target dialect               │
│    mlir::mhlo::* → mlir::tpu::*   ;  Mosaic kernels join HERE via          │
│    tpu_custom_call (CanonicalizeMosaicPass / MosaicSerdePass)             │
└──────────────────────────────────────────────────────────────────────────┘
        │  mlir::tpu::LowerPassBase family → createLowerToLLOPass 0x11203ba0
        │  (DialectConversion legalizers; ApplyVectorLayoutPass; LowerToMloPass DMA)
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 3 — LLO   (jellyfish::LloModule / LloInstruction / LloRegion)       │
│    the "MachineInstr" of the TPU: opcodes, allocations, dependency graph   │
│    LSRAv2 live-range alloc + MSA placement resolve here                    │
└──────────────────────────────────────────────────────────────────────────┘
        │  Part VIII — schedule + pack  (PackBundles 0x10a30a20)
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 4 — SCHEDULED BUNDLES   (vector<jellyfish::Bundle>)                 │
│    MXU/MRB assignment → bundle packing → modulo scheduling for loops       │
└──────────────────────────────────────────────────────────────────────────┘
        │  Phase 3 — CompilePhase3Linking ; per-gen Encoder*::EncodeBundle
        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  LEVEL 5 — PACKED PROGRAM   (TpuProgram, serialized byte stream)           │
└──────────────────────────────────────────────────────────────────────────┘
```

### The two worlds and the crossing

Levels 0 and 1 are MLIR-bytecode and XLA-HLO respectively; both predate the TPU target and are upstream XLA. StableHLO/CHLO/VHLO are the *portable* wire IRs that the framework bridge (JAX) actually serializes — they are MLIR dialects, but at this point they are payload, not yet under TPU control. Level 1 is classic XLA HLO: the `HloModule`/`HloInstruction` graph that XLA's whole optimizer was written against, with roughly 100 `HloOpcode`s. Phase 0 is the format crossing that imports the portable bytecode into HLO; it is *legalization between equivalent representations*, not an optimization, which is why it is a distinct phase rather than a pass inside Phase 1.

Levels 2 through 5 are TPU-specific. Phase 2a (`CompilePhase2aTlpLowering`, `0xf850840`) is the crossing from `HloInstruction` into MLIR via the TPU-Level Program (TLP) representation. The `tpu` dialect (covering MXU matmul, DMA enqueue/wait, semaphores, vector relayout, pack/unpack, PRNG, and trace) is the TPU's machine dialect. **The Level-2 `tpu` layer is reached by exactly one producer — imported Mosaic `tpu_custom_call` kernels — not by lowering general MHLO** (the binary has no MHLO→`tpu` pass); general HLO is emitted straight to LLO by the jellyfish `*Emitter` family without ever entering the `tpu` dialect. See the two-tree picture on [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) and [Mosaic Overview](mosaic-overview.md). The `tpu` dialect lowers through the `mlir::tpu::LowerPassBase` family / `createLowerToLLOPass` (`0x11203ba0`) into LLO, the `jellyfish::Llo*` family — `LloModule`, `LloInstruction`, `LloRegion`, `LloOpcode`, `LloAllocation`, `LloDependencyGraph` — which is the genuine "machine instruction" level: pre-bundle, post-MLIR, where live-range allocation (LSRAv2) and memory-space placement resolve. The [scheduling back end](../sched/overview.md) then packs LLO into `Bundle`s, and Phase 3 (`CompilePhase3Linking`, `0xf852f40`) finalizes and serializes the `TpuProgram`.

> **NOTE — "TLP" and "MHLO" are the same level, not two.** TLP (TPU-Level Program) names the Phase 2a import mechanism — its only direct anchors are the phase symbol `CompilePhase2aTlpLowering` (`0xf850840`) and the LLVM-backend predicate `llvm::TPU::isTLPFunction` (`0x13b67a60`); MHLO is the MLIR dialect the program is expressed in on arrival. They are not stacked layers — Level 2 is "the MLIR mid-level," and TLP names the import mechanism while MHLO/`tpu` name the dialects. A reimplementer should treat Phase 2a as "HLO → MLIR" and not search for a separate standalone TLP IR with its own op set; no `tlp.` op-model vtables or a distinct `TlpLowering`/`TLPFunction` type were isolable in the binary (LOW confidence on TLP having a distinct op enum).

> **QUIRK — the dialect op tables are deceptively large because the bytecode dialects ship in full.** RTTI confirms StableHLO, MHLO (including `mhlo.async_bundle`), CHLO (the high-level "client" HLO), and VHLO (the versioned dialect, with `_v1`/`_v2`-suffixed op names) are all present as complete op tables — each contributes thousands of `mlir::{stablehlo,mhlo,chlo,vhlo}::*` symbols. Only a fraction are reachable on the TPU path — VHLO in particular exists purely for *wire-format forward compatibility* (older runtimes ignore newer fields), and most VHLO/CHLO ops are legalized away in Phase 0. A reimplementation that builds handlers for every dialect op is building for the import surface, not the codegen surface; the codegen surface is the HLO opcodes plus the `tpu` dialect ops. (Exact per-dialect op-enum sizes were not byte-anchored here — string scans of the `<dialect>.` op-name prefixes are contaminated by truncated strings and by type/attribute mnemonics, so no precise op count is asserted.)

---

## The Five Compile Phases

The descent is structured as five phases registered by `xla::TpuCompiler::RegisterAllPhases` (`0xf849ec0`). Each is a free function in the `xla` namespace taking and returning `absl::Span<const PjRtPartialProgramProto>` — the PjRt *partial program* protocol, which lets the runtime stop after any phase (for AOT, caching, or staged compilation) and resume from the serialized intermediate. This phase boundary is why each stage is a distinct symbol rather than an inlined block: each is independently invocable and its output is serializable.

| Phase | Entry point | Address | Crosses | Owns |
|---|---|---|---|---|
| **0 — Stablehlo→Hlo** | `CompilePhase0StablehloToHlo` | `0xf84de60` | wire bytecode → XLA HLO | StableHLO/CHLO/VHLO legalization, HLO import |
| **1 — Hlo optimizations** | `CompilePhase1HloOptimizations` | `0xf84ee00` | HLO → HLO | the entire HLO pass pipeline (opt, layout, fusion, MSA, schedule) |
| **2a — Tlp lowering** | `CompilePhase2aTlpLowering` | `0xf850840` | HLO → MLIR (`tpu`) → LLO | HLO→MLIR crossing, dialect legalization, tpu→LLO |
| **2b — Deduped lowering** | `CompilePhase2bDedupedLowering` | `0xf852180` | LLO → LLO | cross-region deduplication of lowered output |
| **3 — Linking** | `CompilePhase3Linking` | `0xf852f40` | LLO/bundles → packed | bundle finalization, encode, `TpuProgram` serialization |

The single heaviest phase is Phase 1: it is where `DeepseaCompilerBase::RunHloPasses` (`0x109152a0`) runs the full HLO pass pipeline, and it is where the four optimization-pass families described in the next section live. Phases 2a and 2b are the lowering descent — 2a does the bulk crossing into MLIR and down to LLO, and 2b deduplicates: the `CompilePhase2bDedupedLowering` name and its LLO-in/LLO-out position indicate it collapses identical lowered regions so the back end packs each unique region once (the dedup algorithm itself was not reverse-engineered — see [Compile Phases](compile-phases.md); MEDIUM on the exact dedup key).

> **GOTCHA — Phase 1 is not "optimize then lower"; layout assignment changes the program shape that lowering depends on.** Inside Phase 1, layout assignment and MSA are *analyses that rewrite the HLO* (they insert copies, choose tiled layouts, and assign memory spaces), and fusion and the latency-hiding scheduler run against the post-layout program. A reimplementer who treats Phase 1 as a pure optimizer and defers all layout/memory decisions to the MLIR side will produce a `tpu`-dialect program whose tile shapes and memory spaces are unassigned, which the `tpu`→LLO legalizer cannot lower. The HLO half *commits* layout and memory space; the MLIR half *realizes* them.

---

## The Four Pass Families

Inside the five phases, the actual transformation work clusters into four families. Each family has its own detail pages; this section places them and says what they decide.

### Family 1 — HLO optimization passes (Phase 1)

The classic XLA pass pipeline: algebraic simplification, sharding propagation and SPMD partitioning, dynamic-shape handling, optimization barriers, custom-call lowering, and the long tail of canonicalizations. These run on `HloInstruction` under `RunHloPasses` and are registered/ordered by the HLO pass registry. They are *target-retargeted upstream XLA*: the same pass classes XLA ships for every backend, with TPU-specific enablement driven by the `xla_jf_*` flag family (Jellyfish; the dominant family, hundreds of flags), `xla_tpu_*` (pan-TPU codegen), and per-generation prefixes (`xla_sc_*` SparseCore, `xla_vf_*` Viperfish, `xla_gf_*` 6acc60406, `xla_pf_*` Pufferfish). Detail: [HLO Pass Registry](hlo-pass-registry.md), [HLO Pre-Passes](hlo-pre-passes.md), [Algebraic Simplifier](algebraic-simplifier.md), [Sharding Propagation](sharding-propagation.md), [Auto-Sharding / SPMD](auto-sharding-spmd.md), [Dynamic-Shape Support](dynamic-shape-support.md), [Optimization Barrier](optimization-barrier.md), [Custom-Call Lowering](custom-call-lowering.md).

### Family 2 — codegen-gating analyses: layout assignment + MSA (Phase 1)

Two analyses commit physical decisions the lowering depends on. **Layout assignment** (`LayoutAssignment`) chooses the tiled memory layout of every tensor — the TPU analogue of choosing data layout for a SIMD machine, except the tiling interacts with the MXU's 128×128 systolic geometry. **MSA** (`MemorySpaceAssignment`) is XLA's memory-space assignment: it decides which buffers live in fast VMEM versus HBM and when to prefetch/evict across the tiers, governed by the `xla_msa_*` flag family — it is the TPU's closest analogue to register allocation, but for the memory hierarchy rather than registers. Detail: [Layout Assignment](layout-assignment.md), [MSA Overview](msa-overview.md), [MSA AllocateSegment](msa-allocate-segment.md), [MSA Per-Version Defaults](msa-per-version-defaults.md), [MSA Reservation & HBM Policy](msa-reservation-hbm-policy.md).

### Family 3 — fusion (Phase 1)

Fusion clusters elementwise and reduction ops into single kernels to cut memory traffic, priced by a TPU cost model. `HloFusion` and the `FusionPipeline` drive it; the profitability decision reads the Part VII cost model. Detail: [Fusion Patterns](fusion-patterns.md), [Fusion Cost Model](fusion-cost-model.md), and the dot/conv → MXU lowering that fusion feeds: [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md), [RaggedDot → Convolution](raggeddot-convolution.md).

### Family 4 — MLIR lowering legalizers + schedule/pack back end (Phases 2a/2b/3)

The MLIR-side lowering is a chain of DialectConversion legalizers centered on `tpu` → LLO (`createLowerToLLOPass`, `0x11203ba0`), with the vector-layout realization pass (`ApplyVectorLayoutPass`) and the DMA bridge (`createLowerToMloPass`). **There is no MHLO → `tpu` legalizer in this binary** (0 `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu` symbols): the `tpu` dialect is *imported* from Mosaic `tpu_custom_call`s and lowered to LLO, while general (non-Mosaic) HLO is emitted straight to LLO by the jellyfish `*Emitter` family — it never becomes `tpu` ops. The "MHLO → XTile → `tpu`" reading of Level 2 is two disconnected trees; see the two-tree picture in [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md). After LLO, the schedule-and-pack back end (`PackBundles`, `0x10a30a20`) assigns MXU sequences, packs VLIW bundles, and modulo-schedules loops — this is the subject of Part VIII. Detail on the MLIR side: [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md), [The tpu MLIR Dialect](tpu-dialect-and-ops.md), [tpu → LLO Lowering](tpu-to-llo-ods.md), [DialectConversion Legalizer](dialect-conversion-legalizer.md), [ConversionPatternRewriter](conversion-pattern-rewriter.md), [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), [MLIR Op-Model Contract](mlir-op-model-contract.md). The scheduling/packing back end lives in [Part VIII — Scheduling](../sched/overview.md); its pricing lives in [Part VII — Cost Model](../cost/overview.md).

---

## The Mosaic Side Channel

Not every `tpu`-dialect program comes from the HLO optimizer. **Mosaic** is a separate kernel compiler that lets hand-written kernels (Pallas/JAX-side) be expressed directly in the `tpu` dialect and embedded into the HLO graph as `tpu_custom_call` ops. On the way down, `mlir::tpu::CanonicalizeMosaicPass` normalizes the embedded MLIR and `mlir::tpu::MosaicSerdePass` handles its serialization, so the kernel body rejoins the main descent at the `tpu` dialect (Level 2). The LLO-level driver that imports the embedded module, validates its ABI, runs the Mosaic pass pipeline, and stitches the result into the parent region is `jellyfish::CustomCallEmitter::Emit` (`0x111ef740`); the smaller `jellyfish::MosaicEmitter` (`MosaicEmitter::EmitWindow` @ `0xfaadcc0`, taking an `LloRegionBuilder`) is the per-window emit helper used inside it, not the driver. See [Mosaic Overview](mosaic-overview.md).

The structural point for a reimplementer is that Mosaic is a *second IR producer feeding the same target dialect*, not a parallel back end. A Mosaic kernel skips the entire HLO optimizer (Family 1–3) — it arrives already as `tpu` ops — but it converges with the optimized path at the `tpu` dialect and shares the identical tpu→LLO→bundle descent from there. Mosaic carries its own layout machinery (`mlir::tpu::VectorLayout`, `TiledLayoutAttr`) because its kernels declare their own tiling rather than inheriting layout assignment's choices. `MosaicFusion` lets the surrounding HLO fuse around a custom call, and `IsMosaicVerificationEnabled` (`0x14514d40`) gates an extra verifier. Detail: [Mosaic Overview](mosaic-overview.md), [Mosaic Layout Inference](mosaic-layout-inference.md), [Mosaic VectorLayout](mosaic-vectorlayout.md).

> **NOTE — the Mosaic boundary is a real IR seam, and it is where SparseCore also enters.** The SparseCore path (`mosaic_sc::MosaicSCDialect`, `LowerToSparseCoreLlvmPass`) reuses the same custom-call entry and dialect-conversion machinery to target the SparseCore engines rather than the TensorCore MXU. Its type lowering is an ordinary `LLVMTypeConverter` augmented with one SC address-space conversion lambda — there is no distinct `SCTypeConverter` symbol, the name is a documentary handle for that augmented converter. A reimplementer building only the TensorCore descent will see `tpu_custom_call`s it cannot lower; the custom-call registry routes each call to its emitter by target. See [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) and [SCTypeConverter](sc-type-converter.md), and the [SparseCore Part](../sparsecore/overview.md).

---

## Map of Part V

The remaining Part V pages own the detail this overview only places. Grouped by IR level:

| Level / family | Pages |
|---|---|
| **Front-end / phases** | [HLO Ingestion](hlo-ingestion.md), [Compile Phases 0–3](compile-phases.md), [Compilation Cache](compilation-cache.md) |
| **HLO opt passes** | [HLO Pass Registry](hlo-pass-registry.md), [HLO Pre-Passes](hlo-pre-passes.md), [Algebraic Simplifier](algebraic-simplifier.md), [Sharding Propagation](sharding-propagation.md), [Auto-Sharding / SPMD](auto-sharding-spmd.md), [Dynamic-Shape Support](dynamic-shape-support.md), [Optimization Barrier](optimization-barrier.md), [Custom-Call Lowering](custom-call-lowering.md) |
| **Layout / memory** | [Layout Assignment](layout-assignment.md), [MSA Overview](msa-overview.md), [MSA AllocateSegment](msa-allocate-segment.md), [MSA Per-Version Defaults](msa-per-version-defaults.md), [MSA Reservation & HBM Policy](msa-reservation-hbm-policy.md) |
| **Fusion / MXU** | [Fusion Patterns](fusion-patterns.md), [Fusion Cost Model](fusion-cost-model.md), [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md), [RaggedDot → Convolution](raggeddot-convolution.md), [Loop Tiling & Unrolling](loop-tiling-unrolling.md) |
| **MLIR lowering** | [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md), [The tpu MLIR Dialect](tpu-dialect-and-ops.md), [tpu → LLO Lowering](tpu-to-llo-ods.md), [DialectConversion Legalizer](dialect-conversion-legalizer.md), [ConversionPatternRewriter](conversion-pattern-rewriter.md), [LowerToMlo DMA Bridge](lower-to-mlo-dma-bridge.md), [MLIR Op-Model Contract](mlir-op-model-contract.md), [LlvmTpu Intrinsic Catalog](llvmtpu-intrinsic-catalog.md) |
| **Mosaic / SparseCore lowering** | [Mosaic Overview](mosaic-overview.md), [Mosaic Layout Inference](mosaic-layout-inference.md), [Mosaic VectorLayout](mosaic-vectorlayout.md), [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md), [SCTypeConverter](sc-type-converter.md) |
| **Output** | [TpuProgram Serialization](tpu-program-serialization.md) |
| **Adjacent parts** | [Part VIII — Scheduling](../sched/overview.md), [Part VII — Cost Model](../cost/overview.md), [Part — SparseCore](../sparsecore/overview.md) |

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Five compile phases named `CompilePhase0..3` (with 2a/2b split) | symbols `CompilePhase0StablehloToHlo` `0xf84de60`, `…1HloOptimizations` `0xf84ee00`, `…2aTlpLowering` `0xf850840`, `…2bDedupedLowering` `0xf852180`, `…3Linking` `0xf852f40` | CONFIRMED |
| Phases registered by one registry, take/return `PjRtPartialProgramProto` spans | `TpuCompiler::RegisterAllPhases` `0xf849ec0`; phase signatures `absl::Span<const PjRtPartialProgramProto>` | CONFIRMED |
| HLO pass pipeline driven by `DeepseaCompilerBase::RunHloPasses` | `RunHloPasses` `0x109152a0`; C wrapper `TpuCompiler_RunHloPasses` `0xeabcd80`; `RunBackendOnModuleInternal`, `LowerHloModuleImpl` (seen in `XLA_Timer*` guard variables) | CONFIRMED |
| IR stack: StableHLO/CHLO/VHLO → HLO → MHLO/`tpu` → LLO → bundles | RTTI `mlir::{stablehlo,chlo,vhlo,mhlo,tpu}::*`; `jellyfish::Llo{Module,Instruction,Region,Opcode}` | CONFIRMED |
| Phase 0 is a legalization crossing | `ConvertStablehloToHlo`, `StablehloLegalizeToHlo`, `ChloLegalizeToHlo`, `HloLegalizeToStablehlo`, `VhloToVersion` | CONFIRMED |
| `tpu` dialect lowers to LLO via `LowerToLLOPass`/`createLowerToLLOPass` | `mlir::tpu::createLowerToLLOPass` `0x11203ba0`; `mlir::tpu::LowerPassBase`, `ApplyVectorLayoutPass`, `createLowerToMloPass` `0x1322adc0` | CONFIRMED |
| Layout assignment, MSA, fusion are the HLO-side codegen families | `LayoutAssignment`, `MemorySpaceAssignment`, `HloFusion`, `FusionPipeline` symbols present | CONFIRMED |
| LLO is packed to bundles by the back end | `PackBundles` `0x10a30a20`; `jellyfish::BundlePacker`, `GlobalBundlePacker` | CONFIRMED |
| Mosaic enters the `tpu` dialect via `tpu_custom_call` | `tpu_custom_call` strings, `CanonicalizeMosaicPass`, `CustomCallEmitter::Emit` (driver), `MosaicEmitter::EmitWindow` (helper), `MosaicSerdePass`, `MosaicFusion` | CONFIRMED |
| TLP is the HLO→MLIR import vehicle, same level as MHLO (no separate op enum) | phase symbol `CompilePhase2aTlpLowering` `0xf850840`, `llvm::TPU::isTLPFunction` `0x13b67a60`; no isolable `tlp.` op-model vtables or standalone `TlpLowering`/`TLPFunction` type | LOW |
| Phase 2b key is cross-region lowered-output deduplication | name `CompilePhase2bDedupedLowering` + LLO-in/LLO-out position; dedup key not traced | MEDIUM |

---

## Cross-References

- [HLO Ingestion](hlo-ingestion.md) — Phase 0: how StableHLO/CHLO/VHLO bytecode is legalized and imported into XLA HLO.
- [Compile Phases 0–3](compile-phases.md) — the per-phase detail this page only places: each phase's body, the `RegisterAllPhases` wiring, and the Phase 2b dedup mechanism.
- [Compilation Cache](compilation-cache.md) — how the `PjRtPartialProgramProto` boundary is reused for caching and AOT.
- [HLO Pass Registry](hlo-pass-registry.md) — Family 1: the ordered HLO pass pipeline and its `xla_*` flag-gated enablement.
- [Layout Assignment](layout-assignment.md) — Family 2: tiled-layout commitment that the MLIR lowering realizes.
- [MSA Overview](msa-overview.md) — Family 2: memory-space assignment across the VMEM/HBM tiers.
- [Fusion Patterns](fusion-patterns.md) — Family 3: kernel clustering and its cost-model profitability gate.
- [MHLO → XTile → tpu](mhlo-xtile-tpu-lowering.md) — Family 4: the HLO→MLIR mid-level lowering inside Phase 2a.
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the TPU target dialect, the convergence point of the optimizer and Mosaic paths.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — Family 4: the `createLowerToLLOPass` descent into the LLO machine IR.
- [Mosaic Overview](mosaic-overview.md) — the side-channel kernel compiler that feeds the `tpu` dialect directly via `tpu_custom_call`.
- [TpuProgram Serialization](tpu-program-serialization.md) — Phase 3: how the packed bundle stream serializes into a `TpuProgram`.
- [Part VIII — Scheduling](../sched/overview.md) — the LLO→bundle schedule-and-pack back end this page hands off to.
- [Part VII — Cost Model](../cost/overview.md) — the cycle/latency model that prices fusion, layout, MSA, and scheduling decisions.
- [SparseCore Overview](../sparsecore/overview.md) — the second target the custom-call/Mosaic seam routes to.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / Front-end and pipeline — [back to index](../index.md)
