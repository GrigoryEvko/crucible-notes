# HLO Pre-Passes

> *Symbol names, VAs, and the build-id below apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

The **HLO pre-passes** are the ordered set of `xla::HloPassInterface` passes that run on `libtpu`'s incoming XLA program *before* it leaves the `HloInstruction` world for MLIR. They scrub free-form frontend graphs into a normalized shape the rest of the compiler can assume: scheduling annotations are legalized, linear-algebra and RNG custom-calls are decomposed into primitive arithmetic, dynamic shapes are statically padded, sub-byte and high-precision dtypes are bracketed, sharding is propagated and the program is SPMD-partitioned per device, layout is assigned, and a final pass materializes TPU `frontend_attribute` strings into typed `backend_config` so the MLIR import sees nothing TPU-private. Every pass here consumes HLO and emits HLO; the handoff to MLIR is the next phase, owned by [overview.md](overview.md).

This page is the **full ordered enumeration** of that pre-pass set — the table that [compile-phases.md](compile-phases.md) delegates to. It fixes, for every pass: its C++ class, the builder function that adds it, the pipeline-builder phase it sits in (1–6, matching the [compile-phases.md](compile-phases.md) numbering), what HLO invariant it consumes, what HLO invariant it guarantees on output, whether it is TPU-specific or open-source XLA, and its byte-anchored entry symbol where one was recovered. The five builder functions live in the `(anonymous namespace)` of `deepsea_compiler_hlo_passes.cc` and are reached from `DeepseaCompilerBase::RunHloPasses` (`0x1093a420`), the body of the public C-ABI export `TpuCompiler_RunHloPasses` and of the separate-compilation entry `xla::CompilePhase1HloOptimizations` (`0xf84ee00`).

This is the *enumeration* page. It does **not** re-derive the top-level phase ordering or each phase's entry symbol — that spine lives on [compile-phases.md](compile-phases.md) and is linked, not duplicated. It does **not** re-derive per-pass transformation *algorithms* (the actual graph rewrites) — those have their own pages: [algebraic-simplifier.md](algebraic-simplifier.md) for `TpuAlgebraicSimplifier`, [sharding-propagation.md](sharding-propagation.md) / [auto-sharding-spmd.md](auto-sharding-spmd.md) for the sharding flows. The 372-entry `HloPassInterface` RTTI catalog with `name()` strings lives on [hlo-pass-registry.md](hlo-pass-registry.md).

For reimplementation, the pre-pass contract is:

- **One container type.** `xla::HloPassPipeline` is the only HLO pass container — there is no TPU-private pipeline class. Every TPU pass derives directly from `xla::HloPassInterface` (so `RunImpl(HloModule*, execution_threads)` returns `absl::StatusOr<bool>`) and is added with `AddPass<T>(...)`.
- **Six phases, five builders.** The ordering decomposes into six pipeline-builder phases. Phases 1–5 are HLO→HLO; phase 6 is the final pre-MLIR HLO domain. Five decompiled builder functions add the passes: `PreOptimizationPipeline`, `AddAutoShardingAndRelatedPasses`, `AddTpuPartitioningPasses`, `HloOptimizeThroughLayoutAssignment`, `PostOptimizationPipeline`.
- **The pre-passes legitimately *introduce* ops.** Expanders emit new ops; the partitioner emits collectives. The acceptance gate (`TpuHloSupportChecker`) therefore runs in phase 4 *after* expansion and partitioning, not at the front — checking earlier would reject programs that are actually compilable.
- **Fixed-point loops.** Several passes are wrapped in `xla::HloPassFix<P>` and re-run to convergence; `HloDCE` and `HloCSE` are re-run between most stages whether or not wrapped.
- **Invariant checkers re-run after every pass.** `MaybeAddInvariantCheckers` (`0x10944600`) adds `HloVerifier`, `LegalizeSchedulingAnnotations` (as checker), and `HloCycleDetection` via the separate `AddInvariantChecker<T>` API at the head of every nested pipeline — they re-validate after *each* pass, not once.

| | |
|---|---|
| **Pipeline driver** | `xla::jellyfish::DeepseaCompilerBase::RunHloPasses` @ `0x1093a420` |
| **Sep-comp entry** | `xla::CompilePhase1HloOptimizations` @ `0xf84ee00` |
| **Builder — phase 1** | `(anon)::PreOptimizationPipeline(Target const&, unique_ptr<HloModule>, long, CompilationStats*)` |
| **Builder — phase 2** | `xla::jellyfish::AddAutoShardingAndRelatedPasses(...)` @ `0x10939c40` |
| **Builder — phase 3** | `xla::jellyfish::AddTpuPartitioningPasses(HloModule*, AliasInfo const*, PartitioningPipelineConfig)` @ `0x1278a440` |
| **Builder — phase 4** | `(anon)::HloOptimizeThroughLayoutAssignment(Target const&, RunHloPassesConfig const&, long, HloModule*, ComputationLayout*, CompilationStats*)` — body in `…::$_0::operator()` @ `0x1094ada0` (std::function trampoline `InvokeObject<…$_0>` @ `0x1094ad80`) |
| **Builder — phase 5/6** | `(anon)::PostOptimizationPipeline(Target const&, AliasInfo const*, HloModule*, long, CompilationStats*, bool, bool)` @ `0x1093fd40` |
| **Pipeline opener** | `(anon)::CreateHloPipeline(...)` @ `0x1093efe0`; nested variants `CreateNestedHloPipeline` @ `0x1093a180`, `CreateNestedHloPipelineFix` @ `0x10953e20` |
| **Invariant checkers** | `(anon)::MaybeAddInvariantCheckers(...)` @ `0x10944600` |
| **Gather/scatter helper** | `(anon)::AddGatherScatterExpanderPasses(Target const&, HloModule*, HloPassPipeline&)` @ `0x1095a040` |
| **Acceptance gate** | `xla::TpuHloSupportChecker::RunImpl` @ `0x11071480` (vtable `_ZTVN3xla20TpuHloSupportCheckerE`) |
| **Last HLO-domain pass** | `xla::jellyfish::ConvertFrontendAttributesToBackendConfig` |
| **Source unit** | `platforms/xla/service/jellyfish/deepsea_compiler_hlo_passes.cc` (string-anchored) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## Pipeline-Level Architecture

`RunHloPasses` (`0x1093a420`) is the monolithic driver. It builds and runs a sequence of `xla::HloPassPipeline` containers; the finer numbered stages map onto the five builder functions confirmed in the symbol table by their exact signatures:

```text
RunHloPasses (0x1093a420)  ==  CompilePhase1HloOptimizations (0xf84ee00)
  ├─ PreOptimizationPipeline ........................ PHASE 1  input scrub
  │
  ├─ CreateHloPipeline (0x1093efe0)  opens the main pipeline
  │    ├─ AddAutoShardingAndRelatedPasses (0x10939c40) PHASE 2  sharding
  │    ├─ AddTpuPartitioningPasses (0x1278a440) ...... PHASE 3  SPMD prep
  │    ├─ HloOptimizeThroughLayoutAssignment (0x1094ada0) PHASE 4 pre-layout
  │    │    └─ LayoutAssignment  ........... (see layout-assignment.md)
  │    └─ PostOptimizationPipeline (0x1093fd40) ...... PHASE 5  post-layout
  │                                                    PHASE 6  pre-MLIR HLO
  └─ MLIR conversion  ..................... (see overview.md)
```

Two structural facts shape the whole set:

- **`xla::HloPassFix<P>` wraps a pass in a fixed-point loop.** Vtable evidence in the binary shows it instantiated for `HloPassPipeline`, `HloDCE`, `AllReduceReassociate`, `ReduceWindowRewriter`, `ReduceScatterReassociate`, `WhileLoopConstantSinking`, `AllReduceReduceScatterReorder`, `jellyfish::TpuReduceWindowRewriter`, and `jellyfish::MosaicFusion`. The fixed-point mechanism is CONFIRMED. Its crash-on-non-convergence behaviour is reportedly gated by `--xla_tpu_crash_if_hlo_pass_fix_did_not_converge`; that flag string was **not** located in the sampled strings table. [Confidence: LOW on the crash flag; CONFIRMED on `HloPassFix`.]

- **Invariant checkers re-add at every nested pipeline head.** `MaybeAddInvariantCheckers` (`0x10944600`) uses `AddInvariantChecker<T>` (not `AddPass<T>`), so each checker re-runs after every pass:

| Checker class | Purpose |
|---|---|
| `xla::HloVerifier` + `xla::jellyfish::TpuVerifierMetadata` | full HLO structural / shape verification (shape-size via an `HloVerifierOpts::shape_size` member-fn-ptr wrapped in `std::function<int64_t(Shape const&)>`) |
| `xla::LegalizeSchedulingAnnotations` (as checker) | scheduling-annotation graph well-formedness |
| `xla::HloCycleDetection` | reject HLO graphs with circular dependencies |

> **NOTE — decompile cross-check.** All five builder signatures were confirmed verbatim in `*_functions.json`: `PreOptimizationPipeline` (lambdas `$_0..$_8`, predicate `$_7` confirmed as the `HloDomainIsolator` argument), `PostOptimizationPipeline` (lambdas `$_0..$_12`), `HloOptimizeThroughLayoutAssignment`, `AddAutoShardingAndRelatedPasses`, `AddTpuPartitioningPasses` (takes `PartitioningPipelineConfig`), plus `AddGatherScatterExpanderPasses`, `MaybeAddInvariantCheckers`, and `CreateHloPipeline`. The four TPU vtables (`TpuHloSupportChecker`, `jellyfish::TpuCallInliner`, `jellyfish::TpuInt2AutoUpDownCaster`, `jellyfish::TpuRngBitGeneratorTupleDecomposer`) are present in `*_names.json`. [Confidence: CONFIRMED.]

---

## The Ordered Pre-Pass Table

The master table below is the discovery-order enumeration recovered by walking each builder's `AddPass<T>` instantiations. The **Phase** column matches the pipeline-builder numbering on [compile-phases.md](compile-phases.md): phase 1 = `PreOptimizationPipeline`, phase 2 = sharding, phase 3 = SPMD prep, phase 4 = `HloOptimizeThroughLayoutAssignment` (through layout assignment), phases 5–6 = `PostOptimizationPipeline` (post-layout refinement, then the final pre-MLIR HLO domain). Numbering is *discovery* order, not source contiguity; loop/fixed-point passes appear once but re-run.

The **Src** column: `T` = TPU-specific (`xla::jellyfish::`, `xla::tpu::`, or `xla::Tpu*` prefix); `O` = open-source XLA pass (some called with a TPU `Target`).

### Phase 1 — PreOptimizationPipeline (input scrub)

| # | Pass class | HLO-input invariant | HLO-output invariant | Src | Notes |
|---|---|---|---|---|---|
| 1 | `xla::LegalizeSchedulingAnnotations` | sched. annotations may be textual | annotations normalized to backend-config form | O | gated by `non_mitigatable_gap_checking` config |
| 2 | `xla::CheckNoDataDependencyInSchedulingAnnotations` | sched. annotations on instructions | unchanged (verifier); error on dep cycle | O | invariant checker |
| 3 | `xla::HloDomainIsolator` (predicate `$_7`) | sharding domains may be implicit | explicit `kDomain` ops wrap sharding regions | O | only if `EnableDomainPasses()` |
| 4 | `xla::DynamicIndexSplitter` | dynamic-index ops, multi-dim index | dynamic-index ops, split scalar indices | O | |
| 5 | `xla::BatchNormExpander(true,true,true)` | `BatchNorm{Training,Inference,Grad}` | only primitive arithmetic ops | O | rewrites to log/sqrt/divide |
| 6 | `xla::TpuCholeskyExpander` | `Cholesky` custom-call | tile-recursive dots + triangular-solves | T | subclass of `xla::CholeskyExpander` |
| 7 | `xla::TpuQrExpander` | `QrDecomposition` | Givens-rotation HLO graph | T | subclass of `xla::QrExpander` |
| 8 | `xla::LuDecompositionExpander` | `LuDecomposition` | scatter/gather decomposition | O | |
| 9 | `xla::TpuEighExpander` | `Eigh` | Jacobi-rotation HLO graph | T | subclass of `xla::EighExpander` |
| 10 | `xla::FftExpander(Target const&)` | `Fft` op | Cooley–Tukey radix-2, TPU tile sizes | O* | TPU-aware via `Target` arg |
| 11 | `xla::TpuTriangularSolveExpander` | `TriangularSolve` | recursive blocked solve | T | subclass of `xla::TriangularSolveExpander` |
| 12 | `xla::jellyfish::TpuRngBitGeneratorExpander` | `RngBitGenerator` | Philox / ThreeFry HLO sequence | T | when impure-RNG enabled |
| 13 | `xla::RngBitGeneratorExpander` | `RngBitGenerator` | open-source default algorithm | O | alternate path when 12 not selected |
| 14 | `xla::jellyfish::TpuRngBitGeneratorTupleDecomposer` | tupled-output `RngBitGenerator` | separate `GetTupleElement` per output | T | |
| 15 | `xla::HloDCE` | any | dead instr/computations removed | O | re-run between most stages |
| 16 | `xla::jellyfish::TpuInt2AutoUpDownCaster` | mixed-precision arith with int2 | int2 ops bracketed by `Convert` ↔ int8 | T | MXU wire min is int8 |
| 17 | `xla::jellyfish::TpuCallInliner(MustFuseInlineMode)` | `must_fuse`-marked call sites | callees inlined into caller | T | gated by `xla_tpu_impure_inline_must_fuse_early` |
| 18 | `xla::jellyfish::UserGuidedFusionIdAssigner` | `frontend_attribute: fusion_id` strings | fusion_id as integer backend-config | T | feeds later fusion passes |
| 19 | `xla::ConditionalCanonicalizer` | `Conditional` with arbitrary nesting | canonical form (single branch root) | O | |
| 20 | `xla::DynamicDimensionSimplifier` | dynamic-dim ops | redundant dynamic-dim ops folded | O | |
| 21 | `xla::DynamicPadder(DynamicPadderOptions)` | dynamic shapes | static shapes + padding masks | O | runs dynamic-shape lowering (2× `AddPass`) |
| 22 | `xla::jellyfish::PreX64RewriterOptimizations(Target const&)` | any | sub-ops pre-rewritten for x64 host codegen | T | |
| 23 | `xla::ScatterExpander(ScatterExpander::Mode)` | `Scatter` op | `While` loop over indices | O | mode = slow-path vs simplified |
| 24 | `xla::MapInliner` | `Map` op | callee inlined as elementwise sequence | O | |
| 25 | `xla::HloDomainRemover("sharding", ApplyDomainSharding)` | sharding `kDomain` brackets | brackets removed, sharding stays as attribute | O | always before phase 2 |
| 26 | `xla::jellyfish::TpuHloPrecisionTracer` | instruction precisions | `precision_config` filled where missing | T | |
| 27 | `xla::BitcastDtypesExpander` | dtype-only `Bitcast` | reinterpret-cast HLO graph | O | |
| 28 | `xla::jellyfish::XPrecisionRewriter(kX128Precision)` | matmul with `x128` precision | 8-step accumulation chain | T | high-precision dot decomposition |
| 29 | `xla::jellyfish::XPrecisionRewriter()` | matmul with x6 / x9 precision | 2-step / 3-step accumulation chain | T | run twice (cumulative; 2× `AddPass` confirmed) |
| 30 | `xla::ComparisonExpander({{S64,S32}})` | s64 comparisons | s32 comparisons on hi/lo halves | O | configured for S64→S32 |

`O*` = open-source class, TPU-specialized through a `Target` argument.

### Phase 2 — AddAutoShardingAndRelatedPasses (sharding)

| # | Pass class | HLO-input invariant | HLO-output invariant | Src | Notes |
|---|---|---|---|---|---|
| 31 | `xla::FlattenCallGraph` | nested calls | single-level call graph | O | re-run before every sharding pass |
| 32 | `xla::jellyfish::TpuCallInliner` | small reusable computations | inlined into caller for sharding | T | |
| 33 | `xla::HloDCE` | any | dead instr/computations removed | O | |
| 34 | `xla::ShardingPropagation` | partial sharding annotations | every op has a sharding (or `Replicated`) | O | manual-sharding flow (4× `AddPass`) |
| 35 | `xla::TpuAutoSharding(AutoShardingOption, Target*, AliasInfo*)` | unannotated HLO | sharding on every op | T | auto-sharding flow; `auto_sharding` flag family |
| 36 | `xla::sdy::ShardyXLA(PropagationOptions, ...)` | Shardy-format sharding ops | HLO sharding annotations | O | JAX/Shardy frontend detected |
| 37 | `xla::jellyfish::TpuRngBitGeneratorTupleDecomposer(nullptr, bool)` | tupled RNG output post-sharding | un-tupled | T | re-run after sharding |
| 38 | `xla::TupleSimplifier` | redundant tuple/GTE chains | simplified | O | |

### Phase 3 — AddTpuPartitioningPasses (SPMD prep)

| # | Pass class | HLO-input invariant | HLO-output invariant | Src | Notes |
|---|---|---|---|---|---|
| 39 | `xla::spmd::SpmdPrepare` | HLO with sharding | normalized for SPMD partitioning | O | |
| 40 | `xla::ConvOperandSwapper` | conv with swapped operand layout | canonical operand order | O | |
| 41 | `xla::jellyfish::TpuSpmdConcatRewriter(Target const&)` | sharded `Concatenate` | replicated concatenate + slice | T | |
| 42 | `xla::HloConstantSplitter(bool)` | shared constants across shardings | per-sharding constant copies | O | |
| 43 | `xla::jellyfish::TpuPartitionAssignment(Target const&, long)` | sharded HLO | partition/device-id metadata attached | T | `name()` → `"tpu-partition-assignment"` |
| 44 | `xla::jellyfish::ConvolutionFolding(Target const&, bool)` | conv with foldable bias/activation | fused conv-bias-activation | T | run twice (pre-sharding, in-layout) |
| 45 | `xla::jellyfish::TpuSpmdPartitioner(...)` | sharded HLO | per-partition HLO with collectives | T | TPU subclass of `SpmdPartitioner` |
| 46 | `xla::RecognizeReduceWindow` | `ReduceWindow`-shaped graphs | `ReduceWindow` op explicit | O | |
| 47 | `xla::CollectivePermuteCSE` | duplicate `CollectivePermute` | deduplicated | O | |
| 48 | `xla::WholeGraphManualPass` | `manual` sharding on whole graph | passthrough sharding annotation | O | |

### Phase 4 — HloOptimizeThroughLayoutAssignment (through layout)

| # | Pass class | HLO-input invariant | HLO-output invariant | Src | Notes |
|---|---|---|---|---|---|
| 49 | `xla::ZeroSizedHloElimination` | ops with zero-size operand/output | eliminated / empty constant | O | |
| 50 | `xla::TpuHloSupportChecker` | any | unchanged (validator); error on unsupported Shape | T | **the canonical TPU HLO acceptance test** |
| 51 | `xla::ConvertMemoryPlacementToInternalAnnotations` | `frontend_attribute: memory_space` | backend-config memory_space integer | O | |
| 52 | `xla::HloModuleDCE` | module-level dead computations | removed | O | |
| 53 | `xla::ConvolutionTypeCanonicalizer` | mixed-type conv | unified-type conv with explicit converts | O | |
| 54 | `xla::ConvolutionPrecisionNormalizer` | conv with operand-only precision | normalized precision_config | O | |
| 55 | `xla::BroadcastCanonicalizer` | broadcast, non-canonical dim order | canonical broadcast | O | |
| 56 | `xla::TransposeFolding` | transpose absorbable into dot/conv | dot/conv with permuted operands | O | |
| 57 | `xla::ConvertOperandFolding` | `Convert` absorbable into dot/conv | dot/conv with mixed-precision operand | O | |
| 58 | `xla::HloCSE(bool)` | duplicate pure ops | shared single op | O | re-run between most stages |
| 59 | `xla::HloPassFix<xla::jellyfish::TpuReduceWindowRewriter>` | `ReduceWindow` with non-trivial window | repeated rewrites until canonical | T | fixed-point |
| 60 | `xla::jellyfish::TpuAlgebraicSimplifier(Target, AlgSimpOptions)` | any | algebraic-simplified, TPU-aware | T | superset of `xla::AlgebraicSimplifier` — see [algebraic-simplifier.md](algebraic-simplifier.md) |
| 61 | `xla::GatherOptimizer(Target const&)` | `Gather` op | TPU-friendly Gather (split/decomposed) | O* | |
| 62 | `xla::AllReduceSimplifier` | `AllReduce`, degenerate replica groups | simplified | O | |
| 63 | `xla::jellyfish::TpuAllGatherSimplifier(Target const&)` | `AllGather`, degenerate replica groups | simplified | T | |
| 64 | `xla::AllToAllDecomposer(bool, int)` | `AllToAll` with split-dim | per-partition slice + AllToAll | O | |
| 65 | `xla::jellyfish::RaggedAllToAllExpander(long)` | `RaggedAllToAll` custom-call | dense AllToAll + scatter | T | |
| 66 | `xla::SortSimplifier` | redundant Sort operands | simplified Sort | O | |
| 67 | `xla::jellyfish::TpuReduceRewriter(bool)` | `Reduce` with multiple outputs | per-output reduces | T | |
| 68 | `xla::jellyfish::TpuDegenerateDimensionRewriter` | ops with size-1 batch dim | size-1 dim eliminated via reshape | T | |
| 69 | `xla::jellyfish::TpuBroadcastRewriter` | broadcast, unfavorable target dim | reshape + broadcast to TPU-favored dim | T | |
| 70 | `xla::jellyfish::TpuReduceRewriter` | as 67, no-flag variant | per-output reduces | T | |
| 71 | `xla::ReduceWindowResizer` | `ReduceWindow`, non-pow-2 window | resized window via padding | O | `name()` → `"reduce-window-resizer"` |
| 72 | `xla::WhileLoopConstantSinking(bool)` | While carrying constants | constants sunk into body | O | |
| 73 | `xla::WhileLoopSimplifier(bool)` | While with constant trip | unrolled / simplified | O | |
| 74 | `xla::WhileLoopConcatCodeMotion(long)` | While, `Concatenate` of invariants | concatenate hoisted out | O | |
| 75 | `xla::HloConstantFolding` | foldable constant ops | folded into literal | O | |
| 76 | `xla::jellyfish::TpuConditionalSimplifier(Target const&)` | `Conditional` with TPU patterns | simplified or rewritten | T | |
| 77 | `xla::DeadDynamicUpdateSliceElimination` | DUS chains with dead targets | dead DUS dropped | O | |
| 78 | `xla::conditional_opt::ConditionalCodeMotion(...)` | code identical in cond. branches | code hoisted before / sunk after | O | |
| 79 | `xla::jellyfish::SortMerger` | adjacent `Sort` over compatible keys | merged into single Sort | T | |
| 80 | `xla::ScanExpander` | `Scan` HLO | While loop of partial reduces | O | |
| 81 | `xla::StableSortExpander` | `Sort` requesting stable | augmented-key Sort + post-strip | O | |
| 82 | `xla::InfeedTokenPropagation` | `Infeed` without explicit token edges | token edges threaded for ordering | O | |
| 83 | `xla::jellyfish::InfeedDecomposer` | `Infeed` op | DMA + token sequence (TPU host-transfer) | T | |
| 84 | `xla::jellyfish::OutfeedDecomposer` | `Outfeed` op | DMA + token sequence | T | |
| 85 | `xla::megascale::compiler::TpuAllReduceMerger(Target, mapper)` | per-slice AllReduce | cross-slice AllReduce merged | T | MegaScale path |
| 86 | `xla::megascale::compiler::CrossSliceLegalizer(Target const&)` | cross-slice ops | legalized for MegaScale topology | T | MegaScale path |
| 87 | `xla::TpuGatherScatterFlattener(Target, long)` | high-rank gather/scatter | rank-flattened gather/scatter | T | in `AddGatherScatterExpanderPasses` |
| 88 | `xla::TpuGatherExpander(Target const&)` | `Gather` that can be expanded | `While`-loop of slices | T | |
| 89 | `xla::TpuScatterExpander(Target const&)` | `Scatter` that can be expanded | `While`-loop of DUS | T | |

*Layout assignment (`Phase2PreLayoutAssignment` / `TpuLayoutAssignment`) runs at the tail of phase 4 and is owned by [layout-assignment.md](layout-assignment.md).*

### Phases 5–6 — PostOptimizationPipeline (post-layout refinement, then pre-MLIR)

| # | Pass class | Phase | HLO-input invariant | HLO-output invariant | Src | Notes |
|---|---|---|---|---|---|---|
| 90 | `xla::HloDomainIsolator` (predicate `$_7`) | 5 | re-add domain brackets if `EnableDomainPasses()` | `kDomain` brackets re-added | O | inverse of step 25 |
| 91 | `xla::HloCSE(bool=false)` | 5 | duplicate pure ops | shared single op | O | post-layout CSE |
| 92 | `xla::jellyfish::WrapFusionOutputForDebug` | 5 | fused HLO | fusion outputs wrapped with `kCopy` for debug capture | T | conditional |
| 93 | `xla::jellyfish::AlwaysCrash` | 5 | any | **intentionally fails** (testing pass) | T | gated by `xla_tpu_always_crash` |
| 94 | `xla::AddOriginalValue` | 5 | HLO post-layout | each instruction tagged with provenance metadata | O | `name()` → `"add-original-value"` |
| 95 | `xla::jellyfish::AddRandomHostOffloading(double)` | 6 | any | random instr wrapped with host-offload custom-calls | T | debug pass, flag-gated |
| 96 | `xla::jellyfish::ConvertFrontendAttributesToBackendConfig` | 6 | `frontend_attribute` strings | parsed into typed backend_config protobuf | T | **runs last in HLO domain** |
| 97 | `xla::HloHostDeviceTypeCallWrapper(Options)` | 6 | host_compute call sites | wrapped with type-call markers for MLIR import | O | final pre-MLIR pass |

> **GOTCHA — the acceptance gate (#50) is in phase 4, not phase 1.** A reimplementation that wants to reject unsupported ops early will be tempted to front-load `TpuHloSupportChecker`. libtpu does not: it runs *after* the expanders (phase 1) and the SPMD partitioner (phase 3) because those passes legitimately introduce new ops (expander outputs, partitioned collectives) that must themselves pass the check. Checking before expansion would reject compilable programs. The checker never mutates — it walks every `HloComputation` and validates each result `Shape` with `ShapeUtil::ValidateShapeWithOptionalLayout`, returning an error `Status` on the first unsupported shape. Entry `RunImpl` @ `0x11071480`. [Confidence: CONFIRMED on RunImpl VA + vtable; HIGH that `name()` returns `"tpu-hlo-support-checker"` (the literal did not surface in the sampled strings table).]

---

## TPU-Specific vs Open-Source Split

Of the 97 pipeline-mentioned passes, 33 carry an explicit TPU prefix (`xla::jellyfish::`, `xla::tpu::`, or `xla::Tpu*`); the remainder are open-source XLA passes, some parameterized with a TPU `Target`. The TPU-specific ones cluster by role:

- **Custom-call decomposition (expanders):** `TpuCholeskyExpander`, `TpuQrExpander`, `TpuEighExpander`, `TpuTriangularSolveExpander`, `TpuGatherExpander`, `TpuScatterExpander`, `jellyfish::TpuRngBitGeneratorExpander` (+ `TupleDecomposer`), `jellyfish::RaggedAllToAllExpander`, `jellyfish::Infeed/OutfeedDecomposer`.
- **Shape / canonicalization rewriters:** `jellyfish::TpuBroadcastRewriter`, `jellyfish::TpuDegenerateDimensionRewriter`, `jellyfish::TpuReduceRewriter` (flag + no-flag), `HloPassFix<jellyfish::TpuReduceWindowRewriter>`, `xla::TpuGatherScatterFlattener`, `xla::TpuGatherSplit`, `jellyfish::TpuConvolutionTypeCanonicalizer`.
- **Algebraic / simplifier:** `jellyfish::TpuAlgebraicSimplifier` (superset of the open-source simplifier — see [algebraic-simplifier.md](algebraic-simplifier.md)), `PostFusionTpuSubgraphSimplifier`, `TpuTrivialFusionRemover`, `TpuTrivialInstructionUnfuser`.
- **Call-graph / acceptance:** `jellyfish::TpuCallInliner` (must-fuse aware; `name()` → `"tpu-call-inliner"` / `-must-fuse` / `-inner-must-fuse` / `-non-must-fuse`), `TpuHloSupportChecker`.
- **Sharding / partitioning:** `TpuAutoSharding` (wraps `xla::AutoSharding`), `jellyfish::TpuSpmdPartitioner`, `TpuPartitionAssignment`, `TpuSpmdConcatRewriter`.
- **Precision / dtype:** `jellyfish::TpuInt2AutoUpDownCaster`, `XPrecisionRewriter` (x6/x9/x128 dot precision), `TpuHloPrecisionTracer`.
- **Custom-call / fusion prep:** `jellyfish::MosaicFusion` (Pallas/Mosaic kernels), `UserGuidedFusionIdAssigner`, `WrapFusionOutputForDebug`, `TpuCustomCallScopedVmemAdjuster` (confirmed `AddPass` in `PostOptimizationPipeline::$_12`).

A further set of TPU passes is present in the binary's RTTI (vtable-only) but was **not** observed inside any of the five decompiled builders — they are added by configuration-flag-gated branches deeper inside `RunHloPasses`. These include the SparseCore offload family (`SparseCoreComputeOffloader`, `OffloadGatherToSparseCore`, etc.), the async-collective family (`TpuAsyncCollectiveCreator`, `AsyncCollectiveMerger`, `AsyncOpScheduler`), the fusion family (`TpuInstructionFusion`, `TpuMultiOutputFusion`), `FlashAttention`, `Gmm` (group-matmul), and the int4 path `TpuInt4Rewriter`. [Confidence: CONFIRMED these classes exist (vtable RTTI); LOW on their pipeline position and gating.]

---

## Custom-Call Targets Consumed at the HLO Boundary

The pre-passes consume / produce HLO `kCustomCall` ops with these target strings (recovered from the string section). They form the surface between the JAX/PyTorch frontend and the TPU compiler:

| Target string | Consumed/produced by | Role |
|---|---|---|
| `Sharding` | `ShardingPropagation` | sharding boundary marker |
| `SPMDFullToShardShape` / `SPMDShardToFullShape` | SPMD lowering | sharding lowering helpers |
| `mhlo.sharding` / `_XlaSharding` (attrs) | `sdy::ShardyXLA` | Shardy/MHLO import sharding markers — carried as MLIR attributes, not as a `Sharding-mhlo` custom-call target |
| `RngBitGenerator` | `TpuRngBitGenerator*` | RNG, decomposed by the RNG expander family |
| `TopK` | — | kept opaque, lowered later |
| `tpu_custom_call` | `MosaicFusion` / Mosaic emit | the registered Pallas/Mosaic kernel custom-call target (`CustomCallRegistration::RegisterCompilationProperties("tpu_custom_call", …)`); generic TPU custom-call wrapper |
| `MoveToHost` / `MoveToDevice` | host-offload legalizer | host-offload markers |
| `Pin` | `jellyfish::PinPrecoloring` | precoloring marker |
| `inspect_sharding` | `jellyfish::RemoveInspectShardingCustomCall` | debug-only, removed |

`__cudnn$convForward` and similar GPU targets are **not** consumed by any TPU pass — they are rejected by `TpuHloSupportChecker` (#50).

---

## What Is Not Recovered

- **Exact ordering of the vtable-only TPU passes.** The SparseCore, async-collective, fusion, FlashAttention, and int4 families appear in RTTI but were not observed in the five decompiled builders; they live in flag-gated branches deeper inside `RunHloPasses` not walked in this pass. [Confidence: LOW on position.]
- **Per-`TpuVersion` divergence.** The decompilation reflects *one* pipeline that branches on `Target` / `TpuCompilationEnvironment` flags; which passes are skipped for, e.g., TPU v3 vs v6e was not isolated. [Confidence: LOW on per-gen differences.]
- **Sharding-flow selector logic.** Which of manual / auto / Shardy runs is decided by flags plus frontend-attribute detection inside the builders; the precise dispatch branch was not isolated (the `auto_sharding` flag family is CONFIRMED present in the string table, but the exact selector is LOW). See [auto-sharding-spmd.md](auto-sharding-spmd.md).
- **Per-pass rewrite algorithms.** This page recovers *names + order + I/O invariants*, not the internal graph rewrites. The simplifier algorithm has its own page ([algebraic-simplifier.md](algebraic-simplifier.md)); the rest are pending.
- **The `--xla_tpu_crash_if_hlo_pass_fix_did_not_converge` flag string** was not found in the sampled strings table. [Confidence: LOW.]

---

## Cross-References

- [compile-phases.md](compile-phases.md) — the top-level ordered phase list and each phase's entry symbol (this page is the table it delegates to).
- [overview.md](overview.md) — compiler orientation and the HLO → … → LLO IR-layer stack; owns the MLIR handoff that follows phase 6.
- [hlo-ingestion.md](hlo-ingestion.md) — how the StableHLO/MHLO frontend program becomes the HLO module these pre-passes consume.
- [hlo-pass-registry.md](hlo-pass-registry.md) — the 372-entry `HloPassInterface` class catalog and `name()` strings.
- [algebraic-simplifier.md](algebraic-simplifier.md) — the `TpuAlgebraicSimplifier` rewrite algorithm (pass #60).
- [sharding-propagation.md](sharding-propagation.md) / [auto-sharding-spmd.md](auto-sharding-spmd.md) — the phase-2/3 sharding and SPMD flows (passes #34–48).
- [optimization-barrier.md](optimization-barrier.md) — `OptimizationBarrier` handling across the pre-pass set.
- [layout-assignment.md](layout-assignment.md) — `TpuLayoutAssignment` at the tail of phase 4.
- [back to index](../index.md)
