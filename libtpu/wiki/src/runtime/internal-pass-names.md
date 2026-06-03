# Internal Pass-Name Catalog

> *All addresses and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (`libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, **not** stripped, clang/LLVM trunk). The image carries its full C++ symbol table, so the pass names below are demangled `name()`/`getArgument()` symbols and verbatim `.rodata` strings, not inferred labels. Other wheels will differ.*

## Abstract

This page is the **name index** of every internal compiler pass libtpu registers — the verbatim identifier each pass returns from `xla::HloPassInterface::name()`, the `-flag` argument an `mlir::Pass` registers with the MLIR `PassRegistry`, and the capitalized stage string an `xla::HloPassPipeline` carries in its constructor. It is a reference catalog, not an algorithm page: its value is the grouped, deduplicated table of *names* cross-linked to the Part-V compiler pages that document each pass *body*. Where [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md) explains how passes are added and ordered, this page answers "what is this string I just saw in a `VLOG`, an `xla_dump` directory, or a `--pass-pipeline=` spec, and which page documents it?"

Because the image is unstripped, the names are recovered with near-1:1 fidelity by two complementary symbol enumerations rather than by disassembly. The HLO surface comes from the `_ZNK3xla*4nameEv` symbol set — every XLA HLO pass overrides `HloPassInterface::name()`, so demangling those symbols enumerates the registered HLO passes directly; **322 of them are real passes** across three provenance tiers. The MLIR surface comes from the `create*Pass` / `Create*Pass` factory symbols plus the `getArgument()` pass-flag strings; it splits cleanly by dialect (StableHLO/CHLO/VHLO, Shardy, XTile, `tpu`, `mosaic_sc`, `sparse_core`, `llo`) and is capped at the bottom by the **242 `tpu-*` LLVM-backend MachineFunction pass flags**. A handful of capitalized `HloPassPipeline` stage strings and a `phaseN_*` separate-compilation metric taxonomy round out the inventory.

The two `name()` spellings a reader will meet differ by convention. An HLO pass's runtime name is the **kebab-case of its class** (`ShardingPropagation` → `sharding-propagation`); ~25 of these are byte-confirmed as literal strings, the rest are built at runtime and are inferred from the class name (marked `INFER` below). An MLIR pass's name is the literal it hands to `mlir::PassRegistration` and is byte-confirmed wherever its `getArgument()` symbol carries the string. The page groups names by **pipeline phase** — HLO-opt, sharding/SPMD, layout, fusion, collective, scheduling, MSA, host-offload, SparseCore, then the MLIR lowering cascade and the LLVM-backend codegen tail — and gives, per group, the documenting wiki page. Status/error templates are on [`error-templates.md`](error-templates.md); advisory hint strings on [`hint-strings.md`](hint-strings.md); this page owns only the *pass-name index and the name→page cross-link*.

The catalog contract:

- **The four name families** and how each is recovered (`name()` RTTI · `create*Pass` factory · `getArgument()` flag · pipeline stage string).
- **Per phase:** the verbatim registered name, its provenance tier (`T` TPU-private / `O` open-source XLA), and a confidence tag (byte-confirmed string vs convention-inferred).
- **The name → documenting-page cross-link** for every group that has an owning Part-V page.
- **The honest gap:** ~300 HLO `name()` strings are convention-inferred, the per-pass *add order* is owned by the registry pages, and the flag→pass gating is not on this page.

| Phase group | Family | Count | Owning page |
|---|---|---|---|
| HLO-opt (expanders / canonicalizers / simplifiers) | HLO `name()` | ~95 | [`hlo-pre-passes.md`](../compiler/hlo-pre-passes.md), [`algebraic-simplifier.md`](../compiler/algebraic-simplifier.md) |
| Sharding / SPMD / partition | HLO `name()` | ~16 | [`sharding-propagation.md`](../compiler/sharding-propagation.md), [`auto-sharding-spmd.md`](../compiler/auto-sharding-spmd.md) |
| Layout / tiling / mem-space | HLO `name()` | ~12 | [`layout-assignment.md`](../compiler/layout-assignment.md) |
| Fusion | HLO `name()` | ~13 | [`fusion-patterns.md`](../compiler/fusion-patterns.md), [`fusion-cost-model.md`](../compiler/fusion-cost-model.md) |
| Collective / async | HLO `name()` | ~40 | — (collective rewriting; not individually paged) |
| Scheduling | HLO `name()` | ~14 | [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md) |
| MSA (memory-space assignment) | symbol | ~8 | [`msa-overview.md`](../compiler/msa-overview.md) |
| Host-offload | HLO `name()` | ~17 | — |
| While-loop / pipelining / misc | HLO `name()` | ~25 | [`loop-tiling-unrolling.md`](../compiler/loop-tiling-unrolling.md), [`optimization-barrier.md`](../compiler/optimization-barrier.md) |
| SparseCore-offload (HLO-level) | HLO `name()` | ~10 | — |
| SparseCore tile-task (MLIR) | `Create*Pass` | 41 | [`lower-to-sparsecore-llvm.md`](../compiler/lower-to-sparsecore-llvm.md) |
| StableHLO / CHLO / VHLO legalizers | `create*Pass`/flag | ~40 | [`mhlo-xtile-tpu-lowering.md`](../compiler/mhlo-xtile-tpu-lowering.md) |
| Shardy (sdy) round-trip | `create*Pass`/flag | ~35 | [`sharding-propagation.md`](../compiler/sharding-propagation.md) |
| XTile | `create*Pass`/flag | ~13 | [`mhlo-xtile-tpu-lowering.md`](../compiler/mhlo-xtile-tpu-lowering.md) |
| `tpu` dialect (TensorCore vector IR) | `create*Pass`/flag | 13 | [`tpu-dialect-and-ops.md`](../compiler/tpu-dialect-and-ops.md), [`tpu-to-llo-ods.md`](../compiler/tpu-to-llo-ods.md) |
| `mosaic_sc` dialect | `create*Pass` | 3 | [`mosaic-overview.md`](../compiler/mosaic-overview.md) |
| `llo` / LLO bridge | `create*Pass`/flag | ~5 | [`lower-to-mlo-dma-bridge.md`](../compiler/lower-to-mlo-dma-bridge.md), [`llvmtpu-intrinsic-catalog.md`](../compiler/llvmtpu-intrinsic-catalog.md) |
| TPU LLVM-backend MachineFunction flags | `cl::opt` flag | 242 | — (codegen tail; flags only) |
| Pipeline-stage strings (monolithic) | `HloPassPipeline` ctor | ~15 | [`compile-phases.md`](../compiler/compile-phases.md) |
| Separate-compilation phase metrics | `.rodata` token | 11 | [`compile-phases.md`](../compiler/compile-phases.md) |

> **NOTE —** the counts are name-complete for the symbol families (HLO `name()`, `create*Pass`, `getArgument()`) and string-confirmed for the pipeline stages. The add *order* of any pass inside any pipeline is **not** on this page — it is owned by [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md) and the per-phase registry. This is the name index; the order index is the registry.

---

## How the Names Are Recovered

Four name families, four extraction surfaces. The provenance matters because it sets the confidence: a demangled symbol is `CERTAIN` for the *class*, while the runtime *string* it returns is only `CERTAIN` when the literal is also present in `.rodata`.

```text
HLO pass name      ── _ZNK3xla*4nameEv symbol  ── kebab(ClassName)         ── string CERTAIN iff literal in .rodata, else INFER
MLIR pass flag     ── create*Pass / Create*Pass factory symbol             ── string CERTAIN where getArgument() carries it
MLIR pass flag     ── PassIN…getArgumentEv symbol body                     ── byte-confirmed literal
pipeline stage     ── HloPassPipeline ctor std::string at a .rodata offset ── byte-confirmed literal
phaseN_* metric    ── .rodata token referenced from RegisterAllPhases      ── byte-confirmed literal
```

The confidence convention used in every table:

| Tag | Meaning |
|---|---|
| `CERTAIN` | The registered name string is a literal in the binary (a `name()` `.rodata` constant, a `getArgument()` literal, or a stage string) — spot-confirmed below. |
| `HIGH` | The pass *class* symbol is present and demangled; the runtime name follows XLA's `CamelCase`→`kebab` convention but the literal was not located (the `name()` body builds it at runtime). |
| `INFER` | Same as `HIGH`, used for the bulk OSS rows where neither the literal nor a per-pass disassembly was done; trustworthy by convention, not byte-anchored. |

Tier column: **`T`** = TPU-private (`xla::jellyfish::*`, `xla::tpu::sparse_core::*`, `xla::Tpu*`, `xla::megascale::*`) — the Deepsea/Jellyfish backend's private surface; **`O`** = open-source XLA (`xla::*`), shared with the CPU/GPU backends.

> **QUIRK —** an HLO pass's registered name is the *kebab-case of the class, not of the file or the factory*. `xla::jellyfish::TpuInt2AutoUpDownCaster` registers `tpu-int2-auto-up-down-caster` — the `Tpu` prefix survives, the namespace does not. A reimplementer who derives the name from the source filename (`tpu_int2_caster.cc`) or the namespace path will mismatch every `VLOG`/`xla_dump` filter.

---

## HLO Pass Names

The HLO surface is **322 passes** across three provenance tiers: 137 `xla::jellyfish::*` (TPU-private), 24 `xla::tpu::sparse_core::*` (SparseCore-HLO), 161 open-source `xla::*`, plus ~6 `megascale`/`sdy`/`spmd`/`conditional_opt`. Grouped below by the pipeline phase that adds them. Names are the kebab-case the class returns from `name()`; the string is `CERTAIN` only where a literal was located, else `INFER`.

### HLO-Opt — expanders / canonicalizers / simplifiers (Phase 1)

The input-scrub phase: lower high-level ops to primitive arithmetic, canonicalize shapes and conditionals, fold constants, and run the algebraic simplifier. Documented on [`hlo-pre-passes.md`](../compiler/hlo-pre-passes.md); the simplifier itself on [`algebraic-simplifier.md`](../compiler/algebraic-simplifier.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `tpu-int2-auto-up-down-caster` | `jellyfish::TpuInt2AutoUpDownCaster` | T | CERTAIN |
| `algebraic-simplifier` / TPU superset | `jellyfish::TpuAlgebraicSimplifier`, `AlgebraicSimplifier` | T/O | INFER |
| `batch-norm-expander` | `BatchNormExpander` | O | INFER |
| `cholesky-expander` | `TpuCholeskyExpander`, `CholeskyExpander` | T/O | INFER |
| `qr-expander` / `eigh-expander` | `TpuQrExpander`/`TpuEighExpander` (+ OSS) | T/O | INFER |
| `triangular-solve-expander` | `TpuTriangularSolveExpander`, `TriangularSolveExpander` | T/O | INFER |
| `fft-expander`, `lu-decomposition-expander` | `FftExpander`, `LuDecompositionExpander` | O | INFER |
| `dynamic-padder`, `dynamic-index-splitter`, `dynamic-dimension-simplifier` | `DynamicPadder` etc. | O | INFER |
| `scatter-expander` / `gather-expander` | `TpuScatterExpander`/`TpuGatherExpander` (+ OSS) | T/O | INFER |
| `comparison-expander`, `logistic-expander`, `optimization-barrier-expander` | `ComparisonExpander` etc. | O | INFER |
| `dot-decomposer`, `dot-canonicalizer`, `dot-algorithm-normalizer` | `DotDecomposer`, `jellyfish::Dot*` | O/T | INFER |
| `x-precision-rewriter` | `jellyfish::XPrecisionRewriter` (x6/x9/x128 dot) | T | INFER |
| `tpu-hlo-precision-tracer`, `float-type-correction` | `jellyfish::TpuHloPrecisionTracer`, `FloatTypeCorrection` | T | INFER |
| `tpu-broadcast-rewriter`, `tpu-degenerate-dimension-rewriter` | `jellyfish::TpuBroadcastRewriter` etc. | T | INFER |
| `tpu-reduce-rewriter`, `tpu-reduce-window-{rewriter,normalizer}` | `jellyfish::TpuReduce*` | T | INFER |
| `tpu-convolution-rewriter`, `convolution-folding` | `jellyfish::TpuConvolutionRewriter`, `ConvolutionFolding` | T | INFER |
| `tpu-call-inliner` (`-must-fuse`/`-inner-must-fuse`/`-non-must-fuse`) | `jellyfish::TpuCallInliner` | T | INFER |
| `tpu-hlo-support-checker` | `TpuHloSupportChecker` (canonical acceptance test) | T | HIGH |
| `hlo-dce` / `hlo-module-dce` / `hlo-cse` | `HloDCE`/`HloModuleDCE`/`HloCSE` (re-run between stages) | O | INFER |
| `flatten-call-graph`, `call-inliner`, `map-inliner` | `FlattenCallGraph` etc. | O | INFER |
| `float-normalization`, `bfloat16-propagation` | `FloatNormalization`, `BFloat16Propagation` | O | INFER |
| `gmm`, `ragged-dot-expander`, `flash-attention` | `jellyfish::Gmm`/`RaggedDotExpander`/`FlashAttention` | T | INFER |

> **NOTE —** the `jellyfish::Tpu*` rewriters are *supersets* of the OSS pass of the same root name, not replacements; `TpuAlgebraicSimplifier` re-runs the OSS `AlgebraicSimplifier` rule set plus TPU-only rules. Both classes have a distinct `name()` symbol, so a `VLOG` filter sees two strings.

### Sharding / SPMD / Partition (Phase 2-3)

Sharding propagation and SPMD partitioning. `sharding-propagation` is byte-confirmed (literal appears three times — once per registration in the propagation, dump, and verifier paths). Documented on [`sharding-propagation.md`](../compiler/sharding-propagation.md) and [`auto-sharding-spmd.md`](../compiler/auto-sharding-spmd.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `sharding-propagation` | `ShardingPropagation` (×3 registration) | O | CERTAIN |
| `auto-sharding` | `AutoSharding`, `TpuAutoSharding` | O/T | INFER |
| `tpu-spmd-partitioning` | `jellyfish::TpuSpmdPartitioner` | T | INFER |
| `tpu-partition-assignment` | `jellyfish::TpuPartitionAssignment` | T | CERTAIN |
| `tpu-spmd-concat-rewriter` | `jellyfish::TpuSpmdConcatRewriter` | T | INFER |
| `tpu-sparse-core-hierarchical-spmd-partitioning` | `sparse_core::SparseCoreHierarchicalSpmdPartitioner` | T | INFER |
| `whole-graph-manual-pass` | `WholeGraphManualPass` | O | INFER |
| `spmd-prepare`, `spmd-partitioner` | `spmd::SpmdPrepare`/`SpmdPartitioner` | O | INFER |
| `shardy-xla` | `sdy::ShardyXLA` (Shardy frontend import) | O | INFER |
| `hlo-domain-isolator` / `hlo-domain-remover` | sharding-domain brackets | O | INFER |
| `canonicalize-all-gather-for-cse` | `CanonicalizeAllGatherForCSE` | O | INFER |

### Layout / Tiling / Memory-Space (Phase 4-5)

Layout assignment, TPU tiling propagation, and memory-space propagation. Documented on [`layout-assignment.md`](../compiler/layout-assignment.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `layout-assignment` | `LayoutAssignment` | O | INFER |
| `tpu-tiling-propagation`, `tpu-tiling-rewriter` | `jellyfish::TpuTilingAssignment`/`TpuTilingRewriter` | T | INFER |
| `tpu-peephole-optimize-layouts` | `jellyfish::TpuPeepholeOptimizeLayouts` | T | INFER |
| `window-config-assignment` (`-via-service`) | `jellyfish::WindowConfigAssignment[ViaService]` | T | INFER |
| `memory-space-propagation` | `MemorySpacePropagation` | O | INFER |
| `copy-insertion`, `tpu-copy-normalizer` | `CopyInsertion`, `jellyfish::TpuCopyNormalizer` | O/T | INFER |
| `convert-memory-placement-to-internal-annotations` | `ConvertMemoryPlacementToInternalAnnotations` | O | INFER |
| `optimize-input-output-buffer-alias` | `OptimizeInputOutputBufferAlias` | O | INFER |

### Fusion (Phases "Pre main fusion" / "Main fusion" / "Post main fusion")

The fusion bucket. `tpu-instruction-fusion` (the main pass, using `TpuPriorityFusionQueue`) builds its name at runtime — the class symbol `xla::jellyfish::TpuInstructionFusion::name()` is present at `0x12e037a0` but carries no `.rodata` literal, so the name is `HIGH`-confidence by convention. Documented on [`fusion-patterns.md`](../compiler/fusion-patterns.md) and [`fusion-cost-model.md`](../compiler/fusion-cost-model.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `tpu-instruction-fusion` | `jellyfish::TpuInstructionFusion` (`name()` @ `0x12e037a0`) | T | HIGH |
| `tpu-multi-output-fusion` | `jellyfish::TpuMultiOutputFusion` | T | INFER |
| `instruction-fusion` / `multi-output-fusion` / `megacore-fusion` | OSS `InstructionFusion` etc. | O | INFER |
| `tpu-loop-fusion-enhancer` | `jellyfish::TpuLoopFusionEnhancer` | T | INFER |
| `mosaic-fusion` | `jellyfish::MosaicFusion` (Pallas/Mosaic, `HloPassFix`) | T | INFER |
| `tpu-subgraph-simplifier` | `jellyfish::PostFusionTpuSubgraphSimplifier` | T | INFER |
| `user-guided-fusion-id-assigner` | `jellyfish::UserGuidedFusionIdAssigner` | T | INFER |
| `wrap-fusion-output-for-debug` | `jellyfish::WrapFusionOutputForDebug` | T | INFER |
| `fusion-tuple-deduplicator`, `masked-fusion-iteration-skipper` | `jellyfish::FusionTupleDeduplicator` etc. | T | INFER |
| `tpu-trivial-fusion-remover` / `tpu-trivial-instruction-unfuser` | `jellyfish::TpuTrivial*` | T | INFER |

### Collective / Async Rewriting (Phase 4 onward)

All-reduce / all-gather / reduce-scatter / all-to-all combiners, legalizers, and async-collective creators, including the MegaScale cross-slice family. Not individually paged; this is the name index.

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `tpu-all-reduce-legalizer`, `tpu-all-reduce-scatter-fusion` | `jellyfish::TpuAllReduceLegalizer` etc. | T | INFER |
| `tpu-all-gather-combiner`, `tpu-all-gather-normalizer` | `jellyfish::TpuAllGather*` | T | INFER |
| `all-reduce-combiner` / `all-reduce-reassociate` / `all-reduce-simplifier` | OSS `AllReduce*` | O | INFER |
| `all-gather-combiner` / `all-gather-decomposer` / `all-gather-simplifier` | OSS `AllGather*` | O | INFER |
| `tpu-async-collective-creator` | `jellyfish::TpuAsyncCollectiveCreator` | T | INFER |
| `async-collective-merger` / `async-collective-fusion` / `async-op-thread-renamer` | `jellyfish::Async*` | T | INFER |
| `collective-pipeliner`, `collective-permute-combiner`/`-cse`/`-motion` | OSS `Collective*` | O | INFER |
| `reduce-scatter-decomposer` / `reduce-scatter-legalizer` | OSS + `jellyfish::ReduceScatterLegalizer` | O/T | INFER |
| `ragged-all-to-all-expander`, `tpu-all-to-all-compaction` | `jellyfish::Ragged*`/`TpuAllToAllCompaction` | T | INFER |
| `tpu-quantized-all-reduce-backend-config-setter` | `jellyfish::TpuQuantizedAllReduceBackendConfigSetter` | T | INFER |
| `barrier-assignment`, `async-barrier-creator` | `jellyfish::BarrierAssignment` etc. | T | INFER |
| MegaScale: `tpu-all-reduce-merger`, `cross-slice-legalizer`/`-rewrites` | `megascale::compiler::*` | T | INFER |

### Scheduling (Phase 7: "final_scheduler" / "async_scheduling")

The latency-hiding scheduler family. Three scheduler `name()` strings are byte-confirmed literals (one hit each). The scheduler placement is owned by [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `latency-hiding-scheduler` | `LatencyHidingScheduler` | O | CERTAIN |
| `latency-hiding-layer-scheduler` | `jellyfish::LatencyHidingLayerScheduler` (`name()` @ `0x10b85160`) | T | CERTAIN |
| `sparsecore-latency-hiding-scheduler` | `sparse_core::SparseCoreLatencyHidingScheduler` (`name()` @ `0x13077ea0`) | T | CERTAIN |
| `legalize-scheduling-annotations` | `LegalizeSchedulingAnnotations` (`name()` @ `0x12e9a060`) | O | CERTAIN |
| `check-no-data-dependency-in-scheduling-annotations` | `CheckNoDataDependencyInSchedulingAnnotations` | O | INFER |
| `annotate-traceable-loops` | `jellyfish::TpuAnnotateTraceableLoops` | T | INFER |
| `while-loop-all-reduce-code-motion` | `WhileLoopAllReduceCodeMotion` | O | INFER |
| `hlo-memory-scheduler`, `hlo-memory-scheduler-with-brkga-fallback`, `hlo-descheduler` | OSS schedulers | O | INFER |
| `async-op-scheduler`, `barna-core-feed-scheduler`, `slack-in-trough`, `constant-deferring` | `jellyfish::*` / OSS | T/O | INFER |

> **QUIRK —** `final_scheduler` and `async_scheduling` are **pipeline *stage* names**, not pass names — they are the `HloPassPipeline` constructor strings for the two scheduler pipelines, and they live in the [pipeline-stage section](#pipeline-stage-names) below. The passes *inside* those pipelines are the `*-scheduler` rows above. Do not confuse the pipeline label with the pass it contains.

### MSA — Memory-Space Assignment (after scheduling)

MSA is one engine, not a `HloPass` with a `name()`; it is driven by `xla::jellyfish::RunMemorySpaceAssignment`. Documented on [`msa-overview.md`](../compiler/msa-overview.md).

| Symbol / name | Tier | Confidence |
|---|---|---|
| `memory_space_assignment::MemorySpaceAssignment` (engine) | O | HIGH |
| `MemorySpaceAssignmentBestFitRepacker` (telamalloc-class repacker) | O | HIGH |
| `jellyfish::RunMemorySpaceAssignment` (driver fn) | T | HIGH |
| `jellyfish::ScopedMemoryAssignment` (`scoped-vmem` anchor) | T | HIGH |
| `sparse_core::SparseCoreMemorySpaceAssignment` | T | HIGH |
| `tpu-memory-precolor` (`jellyfish::TpuMemoryPrecolor`) | T | INFER |
| `jellyfish::PlumbVmemInfo`, `PinPrecoloring`, `PinUserAnnotatedInstructions` | T | HIGH |

### Host-Offload (Phase 5-6)

Host-offload analysis and asyncification. Not individually paged.

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `host-offloader` / `host-offload-legalize` / `host-offloading-prepare` | OSS `HostOffload*` | O | INFER |
| `host-offloading-layout-analysis`, `annotate-host-compute-offload` | OSS | O | INFER |
| `host-compute-asyncifier`, `host-memory-transfer-asyncifier` | OSS | O | INFER |
| `add-random-host-offloading` | `jellyfish::AddRandomHostOffloading` (debug) | T | INFER |
| `host-offloading-lookahead`, `host-offload-normalization` | `jellyfish::*` | T | INFER |
| `host-collective-rewriter`, `host-transfer-channel-id-assigner` | `jellyfish::*` | T | INFER |
| `infeed-decomposer` / `infeed-flattener` / `outfeed-decomposer` | `jellyfish::*` | T | INFER |
| `infeed-token-propagation` | `InfeedTokenPropagation` | O | INFER |

### While-Loop / Pipelining / Misc

While-loop simplification, unrolling, rematerialization, and assorted TPU rewrites. Loop transforms documented on [`loop-tiling-unrolling.md`](../compiler/loop-tiling-unrolling.md); opt-barrier handling on [`optimization-barrier.md`](../compiler/optimization-barrier.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `while-loop-simplifier` / `while-loop-constant-sinking` / `while-loop-invariant-code-motion` | OSS `WhileLoop*` | O | INFER |
| `while-loop-unroller` / `while-loop-pipeline-unroller` / `while-loop-trip-count-annotator` | OSS | O | INFER |
| `while-loop-concat-code-motion` / `-expensive-invariant-code-motion` / `-fusible-sinking` | OSS | O | INFER |
| `hlo-rematerialization` / `hlo-rematerialization-with-stats` | OSS | O | INFER |
| `add-original-value` | `AddOriginalValue` (provenance metadata) | O | INFER |
| `tpu-initial-weight-broadcaster` | `jellyfish::TpuInitialWeightBroadcaster` | T | INFER |
| `tpu-indexed-instruction-analyzer` | `jellyfish::TpuIndexedInstructionAnalyzer` | T | INFER |
| `convert-frontend-attributes-to-backend-config` | `jellyfish::ConvertFrontendAttributesToBackendConfig` (last in HLO domain) | T | INFER |
| `cross-slice-code-motion`, `pred-packing`, `operand-span-exposure` | `jellyfish::*` | T | INFER |
| `always-crash` | `jellyfish::AlwaysCrash` (testing, gated by `--xla_tpu_always_crash`) | T | INFER |
| `mock-while-loop-bounding` | `jellyfish::MockWhileLoopBounding` (testing) | T | INFER |
| `conditional-code-motion` | `conditional_opt::ConditionalCodeMotion` | O | INFER |

### SparseCore-Offload (HLO-level)

The HLO-level SparseCore offloaders that run *before* the SparseCore MLIR lowering. The 24 `xla::tpu::sparse_core::*` HLO passes (embedding / sparse-dense matmul decomposers) are the SparseCore-HLO tier; the MLIR tile-task passes are in the [SparseCore MLIR section](#sparsecore-tile-task-passes). Documented on [`lower-to-sparsecore-llvm.md`](../compiler/lower-to-sparsecore-llvm.md).

| Registered name | Class | Tier | Confidence |
|---|---|---|---|
| `sparse-core-compute-offloader` | `jellyfish::SparseCoreComputeOffloader` | T | INFER |
| `sparse-core-collective-offload` (+ `-remover`/`-aggregator`) | `jellyfish::SparseCoreCollective*` | T | INFER |
| `offload-gather-to-sparse-core` / `offload-scatter-to-sparse-core` / `offload-sort-to-sparse-core` | `jellyfish::Offload*ToSparseCore` | T | INFER |
| `sparse-core-queue-assignment` / `sparse-core-queue-legalizer` | `jellyfish::SparseCoreQueue*` | T | INFER |
| `sparse-core-mismatch-detector`, `aggregate-data-dependent-sc-ops` | `jellyfish::*` | T | INFER |
| 24 `sparse_core::*` HLO passes (embedding/SDM decomposers) | `tpu::sparse_core::*` | T | INFER |

---

## MLIR Pass Names

The MLIR surface is recovered from `create*Pass`/`Create*Pass` factory symbols and `getArgument()` pass-flag literals. It splits by dialect and forms a 4-stage lowering cascade. Names below are the `getArgument()` literal (byte-confirmed where a `getArgument()` symbol carries it).

```text
StableHLO ── createStablehloLowerToXtilePass ──> XTile
XTile     ── vector-layout / tiling passes     ──> tpu (TensorCore vector IR)
tpu       ── createLowerToLLOPass              ──> LLO ── tpu-bundle-packer / tpu-encode-mcinst-bundles ──> ISA bundles
tpu       ── (SparseCore fork) sc 41 passes    ──> Mlo ── createLowerToSparseCoreLlvmPass ──> LLVM-TPU IR
```

### StableHLO / CHLO / VHLO Legalizers

Input wire-format legalization. Documented on [`mhlo-xtile-tpu-lowering.md`](../compiler/mhlo-xtile-tpu-lowering.md).

| Pass-arg flag | Factory | Confidence |
|---|---|---|
| `chlo-legalize-to-stablehlo` / `-hlo` / `-high-level-mhlo` | `createChloLegalize*Pass` | HIGH |
| `stablehlo-canonicalize-dynamism` | `createStablehloCanonicalizeDynamismPass` | HIGH |
| `stablehlo-complex-math-expander` | `createStablehloComplexMathExpanderPass` | HIGH |
| `stablehlo-convert-to-signless` | `createStablehloConvertToSignlessPass` | HIGH |
| `stablehlo-legalize-quant-to-math` | `createStablehloLegalizeQuantToMathPass` | HIGH |
| `stablehlo-legalize-to-{hlo,linalg,vhlo}` | `createStablehloLegalizeTo*Pass` | HIGH |
| `stablehlo-refine-{arguments,shapes}` | `createStablehloRefine*Pass` | HIGH |
| `vhlo-to-version`, `vhlo-legalize-to-stablehlo` | `createVhlo*Pass` | HIGH |
| `legalize-tf`, `legalize-tf-{collective,communication}` | `createLegalizeTFPass` | HIGH |
| `stablehlo-ext-*` (8 variants: canonicalize-dynamism, refine-shapes, sink-constants-to-control-flow, …) | extension passes | HIGH |

### Shardy (sdy) Round-Trip Sharding

The Shardy import/export round-trip. Documented on [`sharding-propagation.md`](../compiler/sharding-propagation.md).

| Pass-arg flag | Factory | Confidence |
|---|---|---|
| `sdy-import-shardings` / `-import-constants` / `-import-func-calls` | `xla::sdy::createImport*Pass` | HIGH |
| `sdy-export-ops` / `-export-named-computations` | `createExportOpsPass` etc. | HIGH |
| `sdy-round-trip-{dedup-meshes,export-ops}` | `createSdyRoundTrip*Pass` | HIGH |
| `sdy-apply-sharding-constraints`, `-close-shardings`, `-collective` | sdy passes | HIGH |
| `sdy-insert-explicit-reshards`, `-reshard`, `-reshard-to-collectives` | sdy passes | HIGH |
| `sdy-flatten-call-graph`, `-inline-meshes`, `-lift-inlined-meshes` | sdy passes | HIGH |
| ~35 `sdy-*` flags total (constant-or-scalar-{merger,splitter}, remove-*, manual-axes-cleanup, …) | sdy passes | HIGH |

### XTile

The StableHLO → XTile lowering layer, plus the XTile-CPU bufferization/vectorization passes. Documented on [`mhlo-xtile-tpu-lowering.md`](../compiler/mhlo-xtile-tpu-lowering.md).

| Pass-arg flag | Factory | Confidence |
|---|---|---|
| `stablehlo-lower-to-xtile` | `xla::xtile::createStablehloLowerToXtilePass` | HIGH |
| `stablehlo-lower-to-arith` | `createStablehloLowerToArithPass` | HIGH |
| `xtile-verify-legal-ops` | `createVerifyLegalXTileOpsPass` | HIGH |
| `xtile-cpu-*` (bufferization, fuse-elementwise, linalg-elementwise-to-vector, lower-xtile-entry, shlo-to-vector, vector-to-scalar, unpack-sub-byte-vector-write, …) | XTile-CPU passes | HIGH |

### `tpu` Dialect — TensorCore Vector IR (13 `create*Pass`)

The TensorCore vector-layout and lowering passes. `lower-to-llo` and `lower-to-mlo` are byte-confirmed (`getArgument()` literals located). Documented on [`tpu-dialect-and-ops.md`](../compiler/tpu-dialect-and-ops.md) and [`tpu-to-llo-ods.md`](../compiler/tpu-to-llo-ods.md).

| Pass-arg flag | Factory | Confidence |
|---|---|---|
| `lower-to-llo` | `mlir::tpu::createLowerToLLOPass` (`getArgument()` @ `0x112041c0`) | CERTAIN |
| `lower-to-mlo` | `mlir::tpu::createLowerToMloPass` | CERTAIN |
| `tpu-tiling-propagation` / `tiling-propagation` | `createTilingPropagationPass` (`getArgument()` @ `0x132e0d80`) | CERTAIN |
| `tpu-apply-vector-layout` | `createApplyVectorLayoutPass` | HIGH |
| `tpu-infer-vector-layout` / `infer-vector-layout` | `createInferVectorLayoutPass` | HIGH |
| `tpu-infer-memref-layout` | `createInferMemRefLayoutPass` | HIGH |
| `tpu-relayout-insertion` / `relayout-insertion` | `createRelayoutInsertionPass` | HIGH |
| `tpu-canonicalize-{memory-space,mosaic,operations}` | `createCanonicalize*Pass` | HIGH |
| (no flag) `createConvertIntegerMemrefsPass`, `createDebugAssertInsertionPass`, `createLogicalToPhysicalDeviceIdPass`, `createPreCanonicalizationOptimizationPass` | tpu passes | HIGH |

### `mosaic_sc` Dialect — Mosaic-for-SparseCore (3 `create*Pass`)

Documented on [`mosaic-overview.md`](../compiler/mosaic-overview.md).

| Factory | Confidence |
|---|---|
| `mlir::mosaic_sc::createApplyVectorLayoutPass` | HIGH |
| `mlir::mosaic_sc::createInferVectorLayoutPass` | HIGH |
| `mlir::mosaic_sc::createInsertRelayoutPass` | HIGH |

### SparseCore Tile-Task Passes (41 `Create*Pass`)

The `xla::tpu::sparse_core::Create*Pass` family — the SparseCore tile-task lowering chain (bounds-checks, tile outlining, allocation, prefetch scheduling, LLVM lowering). Documented on [`lower-to-sparsecore-llvm.md`](../compiler/lower-to-sparsecore-llvm.md). Named by *function* rather than dumped flat:

| Sub-group | Representative `Create*Pass` factories |
|---|---|
| Bounds / safety checks | `AddStreamBoundChecks`, `IndirectStreamBoundsCheck`, `LoadStoreBoundsCheck`, `InsertRunIdCheck`, `LlvmIntToPtrSafety`, `SyncFlagVerifier` |
| Tile outlining / structure | `TileTaskOutlining`, `SinkOpsIntoTileTask`, `ExpandTiledMemRefs`, `TileTaskArgumentsSpill`, `InsertClearTileIbuf` |
| Allocation | `AllocationAssignment`, `GlobalAllocationAssignment`, `TileOverlayAllocation`, `LegalizeHbmGlobals`, `PrepareHbmSpill` |
| Scheduling / prefetch | `ScheduleTileTaskPrefetch`, `LowerPrefetchTileTask`, `DelaysInsertion`, `TileBarrierInsertion` |
| Lowering | `LowerToSparseCoreLlvm`, `LowerDynamicDimensionSize`, `LowerSequencerFunctions`, `PackedOperandsLowering`, `AlgebraicDecomposer` |
| Misc | `HardwareTracing`, `SflagWaitInstrumentation`, `EmitTileOverlayHandler`, `ScsOverlaysTrampoline`, `MemrefAliasScopeAnnotation`, `PackUnpackCleaner`, `VectorUnroll`, `LoopUnroll`, `SparseCoreCanonicalizer`, `TileHostTransfer`, `LegalizeMegaChipModule` |
| Pass-arg flags | `legalize-hbm-globals`, `legalize-megachip-module`, `lower-to-mlo`, `lower-to-sc`, `lower-to-sparse-core-llvm` |

### `llo` / LLO Bridge

The LLO-extension elimination pass that closes the MLIR cascade before bundle packing. Documented on [`lower-to-mlo-dma-bridge.md`](../compiler/lower-to-mlo-dma-bridge.md) and [`llvmtpu-intrinsic-catalog.md`](../compiler/llvmtpu-intrinsic-catalog.md).

| Factory / class | Confidence |
|---|---|
| `mlir::llo::createEliminateLLOExtensionsPass` / `EliminateLLOExtensionsPass` | HIGH |

---

## TPU LLVM-Backend MachineFunction Pass Flags (242)

The bottom of the codegen stack, below LLO: the TPU LLVM target's machine-level passes, exposed as 242 `tpu-*` `cl::opt` flag strings. Only the flag strings are recovered — the `llvm::Pass` subclass symbols behind them are not on this page (codegen tail). Grouped by function rather than dumped flat:

| Function | Representative flags |
|---|---|
| Scheduling | `tpu-machine-scheduler[-fast]`, `tpu-misched[-dcpl]`, `tpu-fifo-scheduler`, `tpu-use-{fifo-sched,swing-modulo-sched}`, `tpu-pipeliner[-analysis/-info/-strategy/-verbose]`, `tpu-pipeline-dovetailing`, `tpu-slack-lazy-backtrack` |
| Super-passes | `tpu-ssa-super-pass`, `tpu-enable-pipeliner-super-pass`, `tpu-enable-overlayer-passes` |
| Bundle packing / encoding | `tpu-bundle-packer[-maintain-order/-maintain-topologial-order]`, `tpu-encode-mcinst-bundles`, `tpu-post-bundle-lower`, `tpu-print-{mcinst,sc-mcinst}-encodings` |
| Register alloc / spill | `tpu-register-prepare`, `tpu-round-robin-registers`, `tpu-pre-spill[-per-fill]`, `tpu-spill-to-dreg`, `tpu-opt-spill-to-dreg`, `tpu-crosscall-spill-packer`, `tpu-fixed-{vregs,maskregs}`, `tpu-liveness-*`, `tpu-remat-pass` |
| ISel / lowering | `tpu-isel`, `tpu-early-post-isel`, `tpu-opt-prepare`, `tpu-codegen-prep[are]`, `tpu-frame-lowering`, `tpu-gep-lowering`, `tpu-rotate-lowering`, `tpu-memop-intrinsics`, `tpu-vliw-prepare`, `tpu-mem-alloc` |
| Verifiers | `tpu-verifier`, `tpu-machine-memoperands-verifier`, `tpu-validate-{inputs,session-inputs,function-sizes}`, `tpu-fatal-{function-size-assertions,mem-alloc-error,verifier-error}` |
| V1-island / TF-bridge | `tpu-v1-island-{coarsening,inlining,outlining}`, `tpu-cluster-formation`, `tpu-device-propagation`, `tpu-merge-variables-with-execute`, `tpu-colocate-{composite-resource-ops,splits}` |

> **GOTCHA —** several `tpu-*` strings are **dual-surface**: `tpu-call-inliner`, `tpu-instruction-fusion`, `tpu-int2-auto-up-down-caster`, `tpu-tiling-propagation`, `tpu-partition-assignment` appear both as HLO `name()` strings (above) *and* as `tpu-*` flag args here. They are the same logical pass exposed at two registry levels. Counting the `tpu-*` flags and the HLO `name()` strings as disjoint sets double-counts these; treat the HLO-level pass as canonical and the flag as its CLI alias.

---

## Pipeline-Stage Names

Two phase taxonomies coexist. The **monolithic compile** labels its `HloPassPipeline` stages with capitalized strings (the `std::string` passed to the pipeline constructor); the **separate compile** (`tpu_phase_functions.cc`) labels its phases with `phaseN_*` metric tokens. They are *not* interchangeable. Both documented on [`compile-phases.md`](../compiler/compile-phases.md).

### Monolithic `HloPassPipeline` Stage Strings

| Stage string | File-off | Role | Confidence |
|---|---|---|---|
| `pre-optimization` | `0x85eaa00` | PreOptimizationPipeline (Phase 1) | CERTAIN |
| `post-optimization` | `0x85ea85e` | PostOptimizationPipeline | CERTAIN |
| `Pre layout assignment` | `0x84ef7bf` | pre-layout HLO | CERTAIN |
| `Layout assignment` / `XLA::JF Layout Assignment` | `0x84ef7d5` / `0x84efbdf` | LayoutAssignment | CERTAIN |
| `Post layout assignment` | `0x84ef6db` | post-layout HLO | CERTAIN |
| `HLO passes through layout assignment` | `0x84ef757` | HloOptimizeThroughLayoutAssignment | CERTAIN |
| `HLO passes after layout assignment` | `0x84ef6f2` | HloOptimizeAfterLayoutAssignment | CERTAIN |
| `Pre main fusion` | `0x10963b16` | PostLayoutPreFusionHloOptimize | CERTAIN |
| `Main fusion` / `XLA::TPU main fusion pass` | — / `0x8522e3e` | TpuInstructionFusion bucket | CERTAIN |
| `Post main fusion` / `JF after main fusion` | `0x85f7605` / `0x85f7616` | PostMainFusionHloOptimize | CERTAIN |
| `final_scheduler` | (SSO `strcpy`) | base memory-schedule pipeline (Phase 7) | HIGH |
| `async_scheduling` | `0x865dcab` | LHS + async-overlap pipeline (Phase 7) | CERTAIN |
| `final_scheduled` | `0x8713d6a` | post-scheduling marker | CERTAIN |
| `simplification after layout assignment` | `0x84ef715` | post-layout simplify | CERTAIN |

### Separate-Compilation Phase Metric Tokens

The multi-phase compile names its phases with `phaseN_*` tokens (the metric keys in the separate-compilation timing table). `phase1_hlo_opts` and `phase3_linking` are byte-confirmed as literals referenced from `xla::TpuCompiler::RegisterAllPhases` @ `0xf849ec0`. "TLP" = TPU-Level-Program.

| Token | Role | Confidence |
|---|---|---|
| `phase0_stablehlo_to_hlo` | StableHLO → HLO import | HIGH |
| `phase1_hlo_opts` | HLO optimization (the HLO passes above) | CERTAIN |
| `phase2a_tlp_lowering` | HLO → MLIR-`tpu` (TPU-Level-Program) lowering | HIGH |
| `phase2b_deduped_lowering` | dedup'd per-chunk lowering | HIGH |
| `phase3_linking` (+ `_test_only`) | HLO-module linking | CERTAIN |
| `phase2_compilation_{status,time}` | phase-2 status/timing metric fields | HIGH |
| `phase0_{transfer_dim,shard_size_bytes}`, `phase1_transfer_dim` | per-phase transfer/shard metric fields | HIGH |

> **NOTE —** the `phaseN_*` tokens are **metric keys**, not pass names — they appear in timing/transfer tables, never as a pass a reader can run. A `VLOG` or `xla_dump` filter will match the *stage* strings above and the *pass* names earlier; it will not match `phaseN_*` unless reading the separate-compilation timing path. The `RegisterAllPhases` symbol that anchors `phase1_hlo_opts`/`phase3_linking` is the registration entry, not a per-phase pass body.

---

## Not Confirmed / Not on This Page

Honest scope limits, so a reimplementer knows what to re-derive:

- **~300 HLO `name()` strings are convention-inferred (`INFER`), not byte-confirmed.** The `name()` body builds the string at runtime (a static const or a string-builder); only ~25 dashed names are present as literals in `.rodata` (the `CERTAIN` rows). The class symbol is certain; the exact returned string follows the `CamelCase`→`kebab` convention but was not located per pass.
- **The add *order* of each pass is not here.** This is the name index; the order is owned by [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md) (Phase-1..6 sequence), the scheduler placement, and the fusion-bucket order.
- **The flag → pass gating is not here.** Which `xla_tpu_*` flag enables which `AddPass<T>` is a `.text` branch in `RunHloPasses` (`0x1093a420`), not a string this page recovers.
- **The 242 `tpu-*` flags' `llvm::Pass` class symbols are not recovered** — only the `cl::opt` flag strings.
- **The per-TpuVersion (jxc/pxc/vxc/glc/gfc) pass-set delta is not isolated** — the HAL modules are named per version, but which passes are skipped for v3 vs v6e is a `Target::tpu_version()` branch not traced here.

---

## Cross-References

- [`hlo-pass-registry.md`](../compiler/hlo-pass-registry.md) — the pass-add registry; this page is its name index, that page is its order index
- [`compile-phases.md`](../compiler/compile-phases.md) — the monolithic-vs-separate phase taxonomy that the stage strings label
- [`algebraic-simplifier.md`](../compiler/algebraic-simplifier.md) — body of the `algebraic-simplifier` / `TpuAlgebraicSimplifier` rows
- [`sharding-propagation.md`](../compiler/sharding-propagation.md) — body of `sharding-propagation` (byte-confirmed ×3) and the sdy round-trip passes
- [`auto-sharding-spmd.md`](../compiler/auto-sharding-spmd.md) — body of the `auto-sharding` / `tpu-spmd-partitioning` rows
- [`layout-assignment.md`](../compiler/layout-assignment.md) — body of the `layout-assignment` / `tpu-tiling-propagation` rows
- [`fusion-patterns.md`](../compiler/fusion-patterns.md) — body of `tpu-instruction-fusion` and the fusion bucket
- [`loop-tiling-unrolling.md`](../compiler/loop-tiling-unrolling.md) — body of the while-loop unroll / pipeline rows
- [`optimization-barrier.md`](../compiler/optimization-barrier.md) — body of the `optimization-barrier-expander` row
- [`msa-overview.md`](../compiler/msa-overview.md) — the MSA engine behind the memory-space rows
- [`mhlo-xtile-tpu-lowering.md`](../compiler/mhlo-xtile-tpu-lowering.md) — the StableHLO/XTile/`tpu` MLIR lowering cascade
- [`lower-to-sparsecore-llvm.md`](../compiler/lower-to-sparsecore-llvm.md) — the 41 SparseCore `Create*Pass` lowering chain
- [`tpu-to-llo-ods.md`](../compiler/tpu-to-llo-ods.md) — the `lower-to-llo` / `lower-to-mlo` bodies
- [`error-templates.md`](error-templates.md) — the error/status template catalog (contrast: this page owns pass names, not error text)
- [`hint-strings.md`](hint-strings.md) — the advisory-hint catalog (contrast: this page owns pass names, not remedies)
- [`overview.md`](overview.md) — Part XI runtime section map
