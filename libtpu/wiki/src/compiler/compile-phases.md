# Compile Phases

> *Addresses, build-id, and symbol names apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

A TPU program is compiled by running an **ordered phase sequence** end to end: the front HLO pre-passes scrub and canonicalize the incoming graph, then sharding/SPMD partitioning specializes it per device, then layout assignment fixes physical tensor layouts, then post-layout HLO refinement and fusion shape the graph, then the HLO is handed to MLIR for the dialect descent (StableHLO → MHLO → TLP → `tpu` dialect → LLO), then memory-space assignment and scheduling price and order the LLO, then the bundle packer fits ops into VLIW words, and finally the program is serialized into a `TpuExecutable`. This page owns the **top-level ordered phase list and each phase's entry symbol** — the spine that the compiler walks once per module.

There are two views of that spine, and they are not contradictory. The **multi-phase separate-compilation** view exposes five public C++ phase functions — `xla::CompilePhase0StablehloToHlo` through `xla::CompilePhase3Linking` — each consuming and producing a serialized `PjRtPartialProgramProto` span so the caller can checkpoint between phases. The **monolithic** view runs the same work inside one `DeepseaCompilerBase::RunHloPasses` call, which builds and runs the HLO `xla::HloPassPipeline` containers directly. The phase numbers in the separate-compilation API (0/1/2a/2b/3) are coarser than the internal pipeline-builder stages (PreOptimization → sharding → SPMD-prep → through-layout → LayoutAssignment → PostOptimization → MLIR). This page reconciles both and gives the entry symbol for each.

This is the *ordering* page. It fixes the sequence and the entry point of every phase; it does not re-derive the IR-layer stack (HLO/MHLO/StableHLO/CHLO/VHLO/TLP/`tpu`/LLO op counts and conversion passes) — that concept lives on [overview.md](overview.md) and is linked, not duplicated. The full enumerated HLO pre-pass table lives on [hlo-pre-passes.md](hlo-pre-passes.md) and the per-class pass catalog on [hlo-pass-registry.md](hlo-pass-registry.md); this page documents only the *front-of-pipeline* pre-pass set as the head of the ordering.

For reimplementation, the phase-sequence contract is:

- **The phase order is total and each phase consumes the previous phase's output.** Phases 1–5 (in pipeline-builder numbering) are HLO→HLO; the handoff to MLIR is phase 6. There is no phase that runs out of order, and no phase that is skipped wholesale — individual *passes* inside a phase are flag-gated, but the phase scaffolding always runs.
- **`xla::HloPassPipeline` is the only HLO container type.** There is no TPU-private pipeline class; every TPU pass derives from `xla::HloPassInterface` and is added with `AddPass<T>`. Invariant checkers are added with the separate `AddInvariantChecker<T>` API and re-validate after *every* pass in a pipeline, not once.
- **The HLO pre-passes are the front of the ordering.** `PreOptimizationPipeline` runs first (input scrub: scheduling-annotation legalization, custom-call expanders, dynamic-shape padding, precision rewriters), before any sharding, layout, or fusion decision.
- **The MLIR/LLO/MSA/scheduling/bundle-packing phases run downstream of all HLO work.** Layout assignment, MSA, and the two schedulers all require a fully-pre-passed, sharded, fused HLO module as a precondition; the bundle packer requires lowered LLO.
- **Serialization closes the sequence.** The compiled program is emitted as a `TpuExecutable` via the `TpuExecutable_Serialize` C-ABI export; the separate-compilation flow re-serializes each phase boundary into a `PjRtPartialProgramProto`.

| | |
|---|---|
| **Top-level orchestrator (monolithic)** | `xla::jellyfish::DeepseaCompilerBase::RunHloPasses` @ `0x1093a420` |
| **Separate-compilation phase fns** | `Phase0` @ `0xf84de60` · `Phase1` @ `0xf84ee00` · `Phase2a` @ `0xf850840` · `Phase2b` @ `0xf852180` · `Phase3` @ `0xf852f40` |
| **Pipeline builders** | `CreateHloPipeline` @ `0x1093efe0` · `PreOptimizationPipeline` (in `0x10948840` AddPass thunk) · `HloOptimizeThroughLayoutAssignment` @ `0x1094ad80` · `PostOptimizationPipeline` @ `0x1093fd40` · `MaybeAddInvariantCheckers` @ `0x10944600` |
| **Sharding / SPMD** | `AddAutoShardingAndRelatedPasses` @ `0x10939c40` · `AddTpuPartitioningPasses` @ `0x1278a440` · `TpuSpmdPartitioner` (AddPass `0x1278b8a0`) |
| **Acceptance test** | `xla::TpuHloSupportChecker::RunImpl` @ `0x11071480` (`name()` @ `0x11071780`, returns `"tpu-hlo-support-checker"`) |
| **Layout** | `Phase2PreLayoutAssignment` @ `0x1094e0e0` · `TpuLayoutAssignment` (subclass of `xla::LayoutAssignment`) |
| **MLIR handoff** | `mlir::mhlo::HloLegalizeToStablehloPass::runOnOperation` @ `0x16adcea0`; final pre-MLIR HLO pass `ConvertFrontendAttributesToBackendConfig::RunImpl` @ `0x10a148a0` |
| **MSA** | `xla::jellyfish::RunMemorySpaceAssignment` @ `0x12fc3080` |
| **Scheduling** | `(anon)::RunHloScheduler` @ `0x1096fac0` |
| **Bundle packing** | `GlobalBundlePacker::PackInstruction` @ `0x10a875a0` (see [LLO bundle packing](../sched/llo-bundle-packing.md)) |
| **Serialization** | `TpuExecutable_Serialize` @ `0xeabea80` · `TpuExecutableSerialize_WriteToArray` @ `0xeabeba0` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Two Numbering Schemes

libtpu exposes the compile pipeline through two distinct surfaces, and a reimplementation must keep them straight because the word "phase" means different things in each.

**Separate-compilation phases (public C++ API).** Recovered from RTTI, five free functions each take `(CompileOptions, absl::Span<const PjRtPartialProgramProto>, const PjRtTopologyDescription&)` and return a `StatusOr` of partial programs. They are the checkpointable boundaries of the PJRT separate-compilation flow — a caller can run Phase 0, persist the intermediate proto, and resume at Phase 1 later. The `Phase1HloOptimizations` body confirms this shape: it works at the `HloModuleProto` / `PjRtPartialProgramProto` granularity, deserializing an HLO module proto, running the HLO-optimization stage, and re-serializing.

| Sep-comp phase | Symbol | VA | What it spans |
|---|---|---|---|
| 0 | `xla::CompilePhase0StablehloToHlo` | `0xf84de60` | StableHLO/MHLO frontend import → HLO |
| 1 | `xla::CompilePhase1HloOptimizations` | `0xf84ee00` | the entire HLO pass pipeline (pre-passes → sharding → layout → post-opt) — same work as `RunHloPasses` |
| 2a | `xla::CompilePhase2aTlpLowering` | `0xf850840` | HLO → MHLO → TLP MLIR lowering |
| 2b | `xla::CompilePhase2bDedupedLowering` | `0xf852180` | deduplicated lowering (shared-subgraph dedup before/within `tpu` dialect emission) |
| 3 | `xla::CompilePhase3Linking` | `0xf852f40` | LLO link, bundle pack, serialize to `TpuExecutable` |

**Pipeline-builder stages (internal).** Inside `Phase1`/`RunHloPasses`, the HLO work is decomposed into finer numbered stages by the pipeline-builder functions. These are the stages the [hlo-pre-passes.md](hlo-pre-passes.md) ordering table uses (its phases 1–6). The mapping is: sep-comp Phase 0 ≈ builder stage 0 (frontend import); sep-comp Phase 1 = builder stages 1–5 (all HLO→HLO); sep-comp Phase 2a/2b = builder stage 6 (MLIR descent) plus the LLO-level MSA/schedule passes; sep-comp Phase 3 = bundle pack + link + serialize.

> **GOTCHA — "Phase 2b deduped lowering" is not reverse-engineered, only named.** Its dedup *algorithm* (which identical subgraphs it collapses, and at which IR level) is not recovered. Treat the row above as the phase boundary only. [Confidence: CONFIRMED that the phase exists @ `0xf852180`; LOW on its internal mechanism.]

---

## The Ordered Phase List

The compiler walks the following sequence exactly once per module. The diagram below is the canonical ordering recovered from `RunHloPasses` (`0x1093a420`) and its callees; each box is a phase with its entry symbol on the right. Phases 1–5 are pure HLO→HLO; phase 6 hands HLO to MLIR; phases 7–10 operate post-lowering.

```text
StableHLO / MHLO module from PJRT frontend
   │  CompilePhase0StablehloToHlo                                   0xf84de60
   ▼
HLO module
   │
   ▼  (all of the below = CompilePhase1HloOptimizations 0xf84ee00 / RunHloPasses 0x1093a420)
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 1 — HLO PRE-PASSES (input scrub)        PreOptimizationPipeline        │
│   front pass: HloDomainIsolator (sharding-domain bracketing)                 │
│   custom-call expanders: Tpu{Cholesky,Qr,Eigh,TriangularSolve}Expander,      │
│     FftExpander, LuDecompositionExpander                                     │
│   dynamic shape: DynamicIndexSplitter, DynamicDimensionSimplifier,           │
│     DynamicPadder                                                            │
│   precision/dtype: TpuInt2AutoUpDownCaster, XPrecisionRewriter (x6/x9/x128), │
│     TpuHloPrecisionTracer, BitcastDtypesExpander                            │
│   misc: BatchNormExpander, TpuCallInliner(MustFuse), HloDCE                  │
└───────────────────────────────────────────────────────────────────────────┘
   │  (CreateHloPipeline 0x1093efe0 opens the main pipeline)
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 2 — SHARDING                            AddAutoShardingAndRelatedPasses│   0x10939c40
│   manual flow : ShardingPropagation                                          │
│   auto  flow  : TpuAutoSharding → ShardingPropagation                        │
│   Shardy flow : sdy::ShardyXLA (when JAX/Shardy frontend detected)           │
│   prep        : FlattenCallGraph, TpuCallInliner, TupleSimplifier            │
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 3 — SPMD PARTITIONING PREP              AddTpuPartitioningPasses        │   0x1278a440
│   SpmdPrepare, HloConstantSplitter, TpuPartitionAssignment,                  │
│   TpuSpmdConcatRewriter, TpuSpmdPartitioner (per-partition HLO + collectives)│
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 4 — THROUGH-LAYOUT HLO OPT     HloOptimizeThroughLayoutAssignment       │   0x1094ad80
│   acceptance : TpuHloSupportChecker  (the canonical TPU HLO filter)          │   0x11071480
│   simplify   : TpuAlgebraicSimplifier, HloCSE, HloConstantFolding            │
│   collective : AllReduceSimplifier, TpuAllGatherSimplifier, AllToAllDecomposer│
│   shape/conv : ConvolutionFolding, TransposeFolding, Tpu*Rewriter family     │
│   gather/scatter: TpuGatherScatterFlattener / TpuGather/ScatterExpander       │
│   ─────────────────────────────────────────────────────────────────────    │
│   LAYOUT ASSIGNMENT  Phase2PreLayoutAssignment / TpuLayoutAssignment          │   0x1094e0e0
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 5 — POST-LAYOUT HLO REFINEMENT          PostOptimizationPipeline        │   0x1093fd40
│   HloCSE (post-layout), AddOriginalValue (provenance metadata),              │
│   FUSION (main fusion runs here — see fusion-patterns.md),                   │
│   final pre-MLIR pass: ConvertFrontendAttributesToBackendConfig             │   0x10a148a0
└───────────────────────────────────────────────────────────────────────────┘
   │  CompilePhase2aTlpLowering 0xf850840 / CompilePhase2bDedupedLowering 0xf852180
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 6 — MLIR DIALECT DESCENT                                               │
│   HloLegalizeToStablehloPass  (HLO → StableHLO bytecode)                     │   0x16adcea0
│   StableHLO → MHLO → TLP → tpu MLIR dialect → LLO                            │
│   (IR-layer stack & op counts: see overview.md)                             │
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 7 — MEMORY-SPACE ASSIGNMENT             RunMemorySpaceAssignment        │   0x12fc3080
│   (HBM/VMEM/SMEM placement; LSRAv2 live-range alloc — see msa-overview.md)   │
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 8 — SCHEDULING                          RunHloScheduler                 │   0x1096fac0
│   final_scheduler (base order) → async_scheduling (LatencyHidingScheduler)   │
│   + LLO-level MXU/MRB assignment (see sched/overview.md)                     │
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 9 — LLO BUNDLE PACKING                  GlobalBundlePacker::PackInstr.  │   0x10a875a0
│   forward greedy earliest-legal-bundle VLIW packing                          │
└───────────────────────────────────────────────────────────────────────────┘
   │  CompilePhase3Linking 0xf852f40
   ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ PHASE 10 — SERIALIZATION / LINK               TpuExecutable_Serialize         │   0xeabea80
│   link LLO program → TpuExecutable → WriteToArray                            │   0xeabeba0
└───────────────────────────────────────────────────────────────────────────┘
   │
   ▼
TpuExecutable bytes
```

The ordering is forced by data dependence, not convention. Sharding (Phase 2) must precede SPMD partitioning (Phase 3) because the partitioner consumes sharding annotations on every op. Layout assignment (within Phase 4) must precede fusion (Phase 5) because fusion legality depends on physical tile layouts. The MLIR descent (Phase 6) must follow all HLO work because the `tpu` dialect has no notion of an un-pre-passed HLO graph. MSA (Phase 7) must precede scheduling (Phase 8) because the scheduler prices spill/refill in cycles against assigned memory spaces. Bundle packing (Phase 9) is last among optimization phases because slot legality is a per-VLIW-word micro decision that only exists once the program is LLO.

---

## Phase 1 — The HLO Pre-Pass Front

The head of the ordering is the **`PreOptimizationPipeline`** — an `xla::HloPassPipeline` built inside the `(anonymous namespace)` of `deepsea_compiler_hlo_passes.cc`. Every pass here consumes HLO and emits HLO; nothing has yet been sharded, laid out, or lowered. This is the *only* place where free-form frontend graphs are normalized into a shape the rest of the compiler can assume.

The recovered front-of-pipeline pass set (the `name()`-anchored, pipeline-confirmed subset — the full enumeration with HLO-input/output invariants is on [hlo-pre-passes.md](hlo-pre-passes.md)):

| Order | Pass class | Role | Confidence |
|---|---|---|---|
| front | `xla::HloDomainIsolator` (predicate `$_7`) | wrap sharding regions in explicit `kDomain` ops (only if `EnableDomainPasses()`) | CONFIRMED |
| early | `xla::DynamicIndexSplitter` | split multi-dim dynamic indices into scalars | CONFIRMED |
| early | `xla::BatchNormExpander(true,true,true)` | lower batch-norm to primitive arithmetic | CONFIRMED |
| expanders | `xla::TpuCholeskyExpander` / `TpuQrExpander` / `TpuEighExpander` / `TpuTriangularSolveExpander` | decompose linalg custom-calls to dot/triangular-solve graphs | CONFIRMED (TPU subclasses) |
| expanders | `xla::FftExpander(Target const&)` / `xla::LuDecompositionExpander` | FFT (TPU-aware radix-2) / LU decomposition | CONFIRMED |
| rng | `xla::jellyfish::TpuRngBitGeneratorExpander` (+ `TupleDecomposer`) | Philox/ThreeFry; un-tuple `(state,output)` | CONFIRMED |
| dtype | `xla::jellyfish::TpuInt2AutoUpDownCaster` | bracket int2 arith with `Convert`↔int8 (MXU wire min is int8) | CONFIRMED |
| inline | `xla::jellyfish::TpuCallInliner(MustFuseInlineMode)` | inline `must_fuse`-marked callees early | CONFIRMED |
| fusion-prep | `xla::jellyfish::UserGuidedFusionIdAssigner` | turn `frontend_attribute: fusion_id` strings into integer backend-config | CONFIRMED |
| dynamic | `xla::DynamicDimensionSimplifier` → `xla::DynamicPadder(...)` | fold then statically pad dynamic shapes | CONFIRMED |
| precision | `xla::jellyfish::XPrecisionRewriter(kX128Precision)` then `XPrecisionRewriter()` | x128 → 8-step, x6/x9 → 2/3-step dot accumulation chains | CONFIRMED |
| precision | `xla::jellyfish::TpuHloPrecisionTracer` / `xla::BitcastDtypesExpander` | fill missing `precision_config`; expand dtype-only bitcasts | CONFIRMED |
| cleanup | `xla::HloDCE` (re-run between most stages) | drop dead instructions/computations | CONFIRMED |
| boundary | `xla::HloDomainRemover("sharding", ...)` | strip `kDomain`, keep sharding as attribute — always runs before Phase 2 | CONFIRMED |

> **NOTE — fixed-point loops.** Several passes are wrapped in `xla::HloPassFix<P>` and re-run to convergence. The `RunToFixPoint` method is present in the binary for `HloPassFix<ReduceWindowRewriter>` and `HloPassFix<HloDCE>` (vtable-confirmed). Non-convergence is reportedly gated by a `--xla_tpu_crash_if_hlo_pass_fix_did_not_converge` flag; the flag string was **not** located in the sampled strings table, so the crash-behavior claim is [Confidence: LOW] while the `HloPassFix` fixed-point mechanism itself is CONFIRMED.

> **GOTCHA — `TpuHloSupportChecker` is the acceptance gate, and it runs in Phase 4, not Phase 1.** A reimplementation that wants to reject unsupported ops early will be tempted to put the support check at the front. libtpu does not: `TpuHloSupportChecker::RunImpl` (`0x11071480`) runs inside `HloOptimizeThroughLayoutAssignment` *after* sharding and SPMD prep, because the pre-passes legitimately *introduce* ops (expander outputs, partitioned collectives) that must themselves pass the check. Checking before expansion would reject programs that are actually compilable. The checker never mutates the module — it walks every `HloComputation` and validates each result `Shape` with `ShapeUtil::ValidateShapeWithOptionalLayout`, returning an error `Status` on the first unsupported shape.

---

## Phases 2–3 — Sharding and SPMD Partitioning

Two builder functions own this region. `AddAutoShardingAndRelatedPasses` (`0x10939c40`) decides *how* the program is sharded; `AddTpuPartitioningPasses` (`0x1278a440`) *applies* the sharding by rewriting the single-device graph into a per-partition graph with collectives.

Three sharding flows coexist, selected by flags and frontend-attribute detection:

- **Manual** — pre-existing sharding annotations are propagated by `xla::ShardingPropagation` until every op has a sharding (or `Replicated`).
- **Auto** — `xla::TpuAutoSharding` (wrapping the open-source `xla::AutoSharding`) synthesizes annotations, then `ShardingPropagation` fills the rest. Gated by the `xla_tpu_auto_sharding_enabled` flag family.
- **Shardy** — `xla::sdy::ShardyXLA` runs when a JAX/Shardy frontend is detected (its `AddPass` thunks are present at `0x10959e80`, `0x1278b3a0`, `0x14bbc200`), converting Shardy-format sharding ops into HLO sharding annotations.

The partitioner — `xla::jellyfish::TpuSpmdPartitioner` (AddPass thunk `0x1278b8a0`), a TPU-specific subclass of the open-source `SpmdPartitioner` — then produces per-partition HLO with explicit collectives, assisted by `TpuPartitionAssignment` (device-id metadata) and `TpuSpmdConcatRewriter`.

> **NOTE — selector logic not recovered.** Which of the three flows runs is decided by flags plus frontend-attribute detection deeper inside the builders; the exact selector branch was not isolated. [Confidence: CONFIRMED that all three flows exist and are added by these builders; LOW on the precise dispatch condition.] See [auto-sharding-spmd.md](auto-sharding-spmd.md) and [sharding-propagation.md](sharding-propagation.md).

---

## Phases 4–5 — Layout Assignment and Post-Layout Refinement

`HloOptimizeThroughLayoutAssignment` (`0x1094ad80`) runs the bulk of HLO optimization — algebraic simplification (`TpuAlgebraicSimplifier`, a superset of the open-source `AlgebraicSimplifier`), CSE, constant folding, collective simplification, convolution folding, and the TPU shape-rewriter family (`TpuBroadcastRewriter`, `TpuDegenerateDimensionRewriter`, `TpuReduceRewriter`, `HloPassFix<TpuReduceWindowRewriter>`) — and then hands off to **layout assignment** (`Phase2PreLayoutAssignment` @ `0x1094e0e0` driving `TpuLayoutAssignment`, a subclass of `xla::LayoutAssignment`). See [layout-assignment.md](layout-assignment.md).

`PostOptimizationPipeline` (`0x1093fd40`) then refines the now-laid-out graph: post-layout `HloCSE(false)`, provenance tagging via `xla::AddOriginalValue`, the **main fusion** (documented on [fusion-patterns.md](fusion-patterns.md) and [fusion-cost-model.md](fusion-cost-model.md)), and finally `xla::jellyfish::ConvertFrontendAttributesToBackendConfig::RunImpl` (`0x10a148a0`) — the **last HLO-domain pass**. It parses known TPU `frontend_attribute` keys (`tpu.precision`, `tpu.fusion_id`, `tpu.collective_group_id`, `tpu.memory_space`, `tpu.scheduling_group`) into typed `backend_config` protobuf, leaving unknown keys untouched so the downstream MLIR import sees nothing TPU-private.

> **NOTE — invariant checkers re-run continuously.** `MaybeAddInvariantCheckers` (`0x10944600`) re-adds `HloVerifier` (+ `TpuVerifierMetadata`), `LegalizeSchedulingAnnotations` (as checker), and `HloCycleDetection` at the head of every nested pipeline via `AddInvariantChecker<T>`, so they re-validate after each pass rather than once. A reimplementation that runs verification only at phase boundaries will accept malformed intermediates that libtpu rejects mid-phase. [Confidence: CONFIRMED.]

---

## Phase 6 — MLIR Dialect Descent

Once `ConvertFrontendAttributesToBackendConfig` has run, the HLO module is handed to MLIR. The first MLIR pass is `mlir::mhlo::HloLegalizeToStablehloPass` (`runOnOperation` @ `0x16adcea0`), which converts HLO into StableHLO bytecode; from there the descent continues StableHLO → MHLO → TLP → `tpu` dialect → LLO. This page does **not** enumerate the dialect op counts, the per-dialect conversion passes, or the legalizer internals — that is the IR-layer-stack material on [overview.md](overview.md), with the lowering legalizers on [tpu-to-llo-ods.md](tpu-to-llo-ods.md), [dialect-conversion-legalizer.md](dialect-conversion-legalizer.md), and the Mosaic/custom-call paths on [mosaic-overview.md](mosaic-overview.md) and [custom-call-lowering.md](custom-call-lowering.md). In the separate-compilation numbering this descent is `CompilePhase2aTlpLowering` (`0xf850840`) followed by `CompilePhase2bDedupedLowering` (`0xf852180`).

---

## Phases 7–10 — MSA, Scheduling, Bundle Packing, Serialization

These phases operate post-lowering and are owned by other pages; this page fixes only their position in the ordering and their entry symbols.

- **Phase 7 — Memory-Space Assignment.** `xla::jellyfish::RunMemorySpaceAssignment` (`0x12fc3080`) places tensors across HBM / VMEM / SMEM and runs live-range allocation (LSRAv2). It precedes scheduling because the scheduler prices spill/refill against assigned spaces. See [msa-overview.md](msa-overview.md), [msa-allocate-segment.md](msa-allocate-segment.md), [msa-reservation-hbm-policy.md](msa-reservation-hbm-policy.md).
- **Phase 8 — Scheduling.** `(anon)::RunHloScheduler` (`0x1096fac0`) runs the two-pipeline HLO scheduler — `final_scheduler` (memory-minimizing base order) then `async_scheduling` (the `LatencyHidingScheduler` overlap rewrite) — followed downstream by LLO-level MXU-sequence/MRB assignment. The whole scheduling stack, including the second LLO-level scheduler and the cost model that prices it, is documented on [../sched/overview.md](../sched/overview.md).
- **Phase 9 — LLO Bundle Packing.** `GlobalBundlePacker::PackInstruction` (`0x10a875a0`) is the forward-greedy earliest-legal-bundle VLIW packer; inner hardware loops are software-pipelined by a separate modulo scheduler. See [../sched/llo-bundle-packing.md](../sched/llo-bundle-packing.md) and [../sched/bundle-modulo-scheduling.md](../sched/bundle-modulo-scheduling.md).
- **Phase 10 — Serialization / Link.** `CompilePhase3Linking` (`0xf852f40`) links the LLO program and emits a `TpuExecutable`; the C-ABI export `TpuExecutable_Serialize` (`0xeabea80`) and `TpuExecutableSerialize_WriteToArray` (`0xeabeba0`) write the bytes. The separate-compilation flow additionally re-serializes each phase boundary into a `PjRtPartialProgramProto`. See [tpu-program-serialization.md](tpu-program-serialization.md).

---

## What Is Not on This Page

- **The IR-layer stack** (HLO/StableHLO/MHLO/CHLO/VHLO/TLP/`tpu`/LLO op counts and dialect-to-dialect conversion passes) — see [overview.md](overview.md).
- **The full HLO pre-pass enumeration** (all 97 pipeline-mentioned passes with per-pass HLO invariants and the 372-entry RTTI pass catalog) — see [hlo-pre-passes.md](hlo-pre-passes.md) and [hlo-pass-registry.md](hlo-pass-registry.md).
- **Per-pass transformation algorithms** (what each rewriter actually does to the graph) — per-topic pages: [algebraic-simplifier.md](algebraic-simplifier.md), [layout-assignment.md](layout-assignment.md), [fusion-patterns.md](fusion-patterns.md), etc.
- **Per-`TpuVersion` pipeline divergence.** The decompilation reflects *one* pipeline that branches by `Target` / `TpuCompilationEnvironment` flags; which passes are skipped for, e.g., TPU v3 vs v6e was not isolated. [Confidence: LOW on per-gen differences.]

---

## Cross-References

- [overview.md](overview.md) — compiler front-end overview and the HLO → … → LLO IR-layer stack (concept owner).
- [hlo-pre-passes.md](hlo-pre-passes.md) — full ordered HLO pre-pass table with per-pass HLO-input/output invariants.
- [hlo-pass-registry.md](hlo-pass-registry.md) — the 372-entry `HloPassInterface` class catalog and `name()` strings.
- [layout-assignment.md](layout-assignment.md) — `TpuLayoutAssignment` (Phase 4 tail).
- [fusion-patterns.md](fusion-patterns.md) / [fusion-cost-model.md](fusion-cost-model.md) — main fusion (Phase 5).
- [auto-sharding-spmd.md](auto-sharding-spmd.md) / [sharding-propagation.md](sharding-propagation.md) — Phases 2–3 detail.
- [msa-overview.md](msa-overview.md) — Phase 7 memory-space assignment.
- [../sched/overview.md](../sched/overview.md) — Phase 8 scheduling stack (HLO LHS + LLO bundle packer + MXU/MRB).
- [tpu-to-llo-ods.md](tpu-to-llo-ods.md) — `tpu` dialect → LLO lowering (Phase 6 tail).
- [tpu-program-serialization.md](tpu-program-serialization.md) — Phase 10 `TpuExecutable` serialization.
- [back to index](../index.md)
