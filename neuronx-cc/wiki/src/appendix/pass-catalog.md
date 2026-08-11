# The Pass Catalog

> *All counts, names, and symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 carry the same registries — only Cython hash suffixes and binary VAs drift). The four stages live in four binaries: `neuronxcc/starfish/bin/hlo-opt` (BuildID `93dd8bd9bd4c697b`, not stripped), the MLIR pipeline inside that same `hlo-opt`, the Cython pass modules under `neuronxcc/starfish/penguin/`, and `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`). Treat every count as version-pinned — the re-derivation recipes in [§5](#5-how-the-counts-are-grounded) re-run against any wheel.*

## Abstract

This appendix is the single consolidated index of **every compiler pass** in neuronx-cc, across all four optimization stages that a graph traverses on its way from XLA HLO to a packaged NEFF. There is no one place in the binaries where all four registries meet — each stage owns its own name→pass mechanism, in its own binary, with its own selection rule — so this page is the meeting point. It exists to answer one question for a reimplementer or a debugger: *given a pass name, which stage owns it, what class does it construct, where in the pipeline does it run, and which wiki page documents it.*

The four stages and their headline counts, each re-derived from the binary in [§5](#5-how-the-counts-are-grounded):

1. **hlo-opt B-strand** — the `--passes` registry of HLO-level rewrites, **112** registered names → 112 classes, keyed by `pass->name()`. The source of truth is [4.1](../hlo-opt/pass-registry.md).
2. **hlo2penguin C-strand (MLIR)** — the imperatively-built `mhlo`/`stablehlo` pass pipeline that lowers optimized HLO to `penguin.py` text. **32** MHLO steps; the StableHLO fork is the same spine with **5** insertions = **37** steps. The source of truth is [4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md).
3. **Penguin middle-end** — a *catalog* of Cython pass modules (no flat registry), sequenced at run time by `SharedCodeGenFlow` generators. **248** total pass modules across four directories (71 + 131 + 6 + 40), of which ~8–10 in the two `transforms/` dirs are helper libraries, not `Pass` classes. The source of truth is [5.7](../penguin/pass-roster-pipeline.md).
4. **walrus backend** — the `GeneratorRegistration` name→factory registry, **150** registered names collapsing to **121** distinct C++ classes (10 classes multi-registered), scheduled into ~180 pipeline steps. The source of truth is [8.1](../walrus/backendpass-registry.md).

Each stage uses a structurally different name→pass mechanism, and that difference is itself the most useful thing this catalog records — a reimplementer who assumes one registry for the whole compiler will get all four wrong. [§0](#0-the-four-registry-shapes) lays the four mechanisms side by side. [§1](#1-stage-1--hlo-opt-b-strand-112-passes)–[§4](#4-stage-4--walrus-backend-150-names--121-classes) index each stage's passes by family, with the documenting page per family. The full row-level tables are **not** reproduced here — they live in the four normative pages — because pasting 112 + 37 + ~240 + 150 rows would be a dump, not an index; this page is the map *to* those tables, with the cross-stage counts reconciled in one place.

This is a reference catalog, not an algorithm walkthrough; its value is **completeness and correct cross-stage ordering**. Confidence is per-row, and reconciled in [§5](#5-how-the-counts-are-grounded): the four name counts (112 / 32+5 / 248 / 150) come off the binary; the 121-class walrus total is derived from them; the Penguin run *order* is **[INFERRED]** from the version-counter source-order trace rather than read off a table.

| | |
|---|---|
| **Stage 1 — hlo-opt** | `xla::hilo::RegisterHiloHloPasses` @ `0x1e72270` → `llvm::StringMap`; **112** names ([4.1](../hlo-opt/pass-registry.md)) |
| **Stage 2 — MLIR** | `hilo::registerMHLOPasses` @ `0x1ee12f0` (**32**) / `registerStableHLOPasses` @ `0x1ee2120` (**37**) ([4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md)) |
| **Stage 3 — Penguin** | `SharedCodeGenFlow` generators → `PassConstructorBuilder`; **248** modules ([5.7](../penguin/pass-roster-pipeline.md)) |
| **Stage 4 — walrus** | `GeneratorRegistration::registerGenerator` → `_Hashtable`; **150** names → **121** classes ([8.1](../walrus/backendpass-registry.md)) |
| **Grand pass-name total** | 112 + 37 + ~240 + 150 ≈ **~539** named pass positions across the four stages |
| **Selection** | Stage 1: `--passes`/`--skip-pass` (XLA `DebugOptions`) · Stage 2: per-pass CompileConfig gate byte · Stage 3: `OptLevel` + `required`/`optional` · Stage 4: `--pass` (registry) + optlevel builder |

---

## 0. The Four Registry Shapes

The single most important cross-stage fact is that **each stage binds a pass name to a pass object by a different mechanism**. They are not four instances of one design; they are four designs. A reimplementer must reproduce each one separately.

| Stage | Binary | Name→pass mechanism | Key derivation | Selection / ordering |
|---|---|---|---|---|
| 1 hlo-opt | `hlo-opt` (C++) | `llvm::StringMap<std::function<unique_ptr<HloPass>()>>` filled by **112** `RegisterHloPass` calls | key = `pass->name()` read back at registration (vtable vptr+0x10) — **not** a literal in the registrar | XLA `DebugOptions` whitelist/blacklist; run-list ordered by the driver outside the registrar |
| 2 MLIR | `hlo-opt` (C++) | **no registry** — imperative `pm.addPass(...)` / `addNestedPass<func::FuncOp>(...)` spine | none; passes are constructed inline by `create<Name>Pass()` factories | ordering is the literal `addPass` source order; each gated on a CompileConfig byte |
| 3 Penguin | Cython `.so` per pass | **no registry** — `yield PassType(Class, kwargs, required\|optional)` into a `PassConstructorBuilder` folded via `+=` | none; the `PassType` carries the class object directly | `OptLevel` staged-skip at flow level + per-spec `required`/`optional` + `clOptBool` toggles |
| 4 walrus | `libwalrus.so` (C++) | `_Hashtable<string, function<unique_ptr<BackendPass>(PassOptions)>>` filled by **150** `register_generator_*__` static initializers | key = the literal string passed to `registerGenerator("<name>", …)` | `--pass <name>` via `getGenerator`; optlevel pipeline builders call `addPass("<name>")`; granularity per call-site |

Two stages have a real string-keyed registry (1, 4); two build the pipeline imperatively with no name table at all (2, 3). The two registries even differ in *where the key comes from*: hlo-opt reads the key back off `name()` at registration (so the registrar source never shows it), while walrus passes the literal name straight into `registerGenerator`. That asymmetry is why recovering the 112 hlo-opt keys required walking each factory to its vtable, whereas the 150 walrus names `grep` straight out of the symbol table.

> **GOTCHA — "pass count" means four different things.** Stage 1's 112 is *registered names* = classes (1:1). Stage 4's 150 is *registered names*, which is **not** the class count (121) — 10 classes answer to several names. Stage 3's 248 is *modules*, which is **not** the pass-class count (~8–10 are helper libs) and **not** the run-list length (a flow runs a subset, some passes twice). Stage 2's 32/37 is *pipeline steps*, every one gated, so the *executed* count depends on the CompileConfig. A single "total passes" number for the compiler is meaningless; always say which of name/class/module/step you mean.

---

## 1. Stage 1 — hlo-opt B-strand (112 passes)

`xla::hilo::RegisterHiloHloPasses` registers **112** passes into an `llvm::StringMap` keyed by each pass's `name()`. An objdump of the registrar body (`0x1e72270`–`0x1e744af`) shows exactly 112 `call` instructions targeting `RegisterHloPass` (`0x1ebc3f0`). Of the 112, **87** override their own `Run` (vtable+0x18) and **25** are `OpExpander` passes sharing `xla::OpExpanderPass::Run` @ `0x29f0bb0` and overriding `InstructionMatchesPattern` (vtable+0x28). Of provenance, **3** are stock XLA classes reused unchanged (`tuple-simplifier`, `call-inliner`, `dce`); the rest are Neuron-authored.

The 112-row name→class→vtable→entry table is **not** duplicated here — it is the canonical table in [4.1 §2](../hlo-opt/pass-registry.md#2-the-112-row-master-pass-index). Below is the index *of that table by family*, pointing each family at the 4.x page that documents it.

| Family (registration-order span) | Representative names | Documenting page | Conf |
|---|---|---|---|
| Stock XLA (reused) | `tuple-simplifier` `call-inliner` `dce` | [4.1 §4](../hlo-opt/pass-registry.md#4-stock-vs-neuron-split) | CERTAIN |
| Legalize / intrinsics / compare | `legalize-intrinsics` `legalize-compare` | [intrinsics-compare-legalize](../hlo-opt/intrinsics-compare-legalize.md) | CERTAIN |
| Softmax / TopK / ArgMax legalize | `legalize-softmax` `legalize-topk` `legalize-aws-neuron-arg-max` `lower-argminmax-custom-call` | [softmax-legalize](../hlo-opt/softmax-legalize.md), [topk-legalize](../hlo-opt/topk-legalize.md), [argmax-argmin-legalize](../hlo-opt/argmax-argmin-legalize.md) | CERTAIN |
| Quantize / scaled-matmul | `legalize-quantize-mx` `legalize-scaled-matmul` | [mx-fp8-legalization](../hlo-opt/mx-fp8-legalization.md), [int8-quantize-legalization](../hlo-opt/int8-quantize-legalization.md) | CERTAIN |
| Collectives → custom-call | `convert-collectives-to-custom-call` `convert-fs-patterns-to-cc` | [collectives-to-customcall](../hlo-opt/collectives-to-customcall.md) | CERTAIN |
| CC-op decompose / legalize | `decompose-cc-ops` `legalize-cpu-cc-ops` `legalize-ccops-for-tensorizer` `trivial-cc-removal` | [ccops-decompose-legalize](../hlo-opt/ccops-decompose-legalize.md) | CERTAIN |
| Collective combiners | `all-gather-combiner` `reduce-scatter-combiner` `all-reduce-combiner` | [collective-combiners](../hlo-opt/collective-combiners.md) | CERTAIN |
| Stream-ID / channel-ID | `collective-stream-id-checker` `aws_neuron_collective_stream_id_injector` `neuron_unique_channel_id_enforcer` | [collective-stream-channel-id](../hlo-opt/collective-stream-channel-id.md) | CERTAIN |
| Flip-collective OpExpanders | `aws_neuron_flip_all_gather_*` `aws_neuron_flip_reduce_*` | [flip-collective-opexpander](../hlo-opt/flip-collective-opexpander.md) | CERTAIN |
| AllReduce→ReduceScatter / DynSlice | `aws_neuron_rewrite_all_reduce_dynamic_slice[_multiple_groups]` | [allreduce-dynslice-rewrites](../hlo-opt/allreduce-dynslice-rewrites.md) | CERTAIN |
| CollectivePermute → AllGather | `collective-permute-to-all-gather` `aws_neuron_rewrite_collective_permute` | [collectivepermute-to-allgather](../hlo-opt/collectivepermute-to-allgather.md) | CERTAIN |
| Looped-einsum | `neuron_looped_einsum_replacer` `neuron_looped_einsum_token_replacer` | [looped-einsum-collective-matmul](../hlo-opt/looped-einsum-collective-matmul.md) | CERTAIN |
| Int / scalar-reduce decompose | `aws_neuron_decompose_int_all_reduce` `aws_neuron_decompose_scalar_reduce` | [int-scalar-reduce-decomposition](../hlo-opt/int-scalar-reduce-decomposition.md) | CERTAIN |
| While-loop unroll / trip-count | `while_loop_unroller` `unroll-while-loop` `aws_neuron_rewrite_all_gather_trip_count` | [whileloop-unroll-tripcount](../hlo-opt/whileloop-unroll-tripcount.md) | CERTAIN |
| While-loop collective code-motion | `neuron-while-loop-all-reduce-code-motion` `neuron_move_*_while_loop` | [whileloop-collective-codemotion](../hlo-opt/whileloop-collective-codemotion.md) | CERTAIN |
| Boundary markers / layer-cut | `canonicalize-boundary-marker` `boundary-marker-removal` | [boundary-markers-layer-cut](../hlo-opt/boundary-markers-layer-cut.md) | CERTAIN |
| Control-dep reification | `preserve-control-deps` | [control-dep-reification](../hlo-opt/control-dep-reification.md) | CERTAIN |
| Layout passes | `io-layout-normalization` `aws_neuron_ensure_descending_layout_in_root` | [layout-passes](../hlo-opt/layout-passes.md) | CERTAIN |
| I/O-alias family | `remove-aliases` `add-must-aliases` `add-may-aliases` `flip-must-aliases` `flip-may-aliases` `aws_neuron_alias_to_must_alias` `aws_neuron_buffer_donation_to_alias` | [io-alias-family](../hlo-opt/io-alias-family.md) | CERTAIN |
| Concat optimizations | `simplify-concat` `slice-of-concat-optimizer` `neuron_repeated_dus_to_concat` | [concat-optimizations](../hlo-opt/concat-optimizations.md) | CERTAIN |
| DUS/DS simplifier / mover | `neuron-dus-ds-index-simplifier` `dynamic-slice-mover` `dynamic-slice-transpose` | [dus-ds-simplifier](../hlo-opt/dus-ds-simplifier.md) | CERTAIN |
| Dedup (param / kernel) | `neuron-preprocess-kernel-duplicate-remover` `neuron_all_gather_duplicate_remover` | [dedup-passes](../hlo-opt/dedup-passes.md) | CERTAIN |
| InstCombine peephole | `neuron-hlo-inst-comb` `aws_neuron_common_instruction_elimination` `eliminate-redundant-compare` `aws_neuron_resolve_self_comparison` | [instcombine-peephole](../hlo-opt/instcombine-peephole.md) | CERTAIN |
| Precision / upcast | `upcast-all-to-fp32` `batch-norm-training-upcast` `rewrite-module-dtype` `neuron-int-matmul-downcast` | [precision-upcast-passes](../hlo-opt/precision-upcast-passes.md) | CERTAIN |
| Calibration / scale | (scale/zero-point flow) | [calibration-scale-flow](../hlo-opt/calibration-scale-flow.md) | HIGH |
| NKI / native lowering | `lower-to-nki-kernels` `lower-to-custom-native-kernel` | [hlo-to-native-kernel-lowering](../hlo-opt/hlo-to-native-kernel-lowering.md) | CERTAIN |
| Misc / cleanup sweep | `stub` `metadata-naming` `aws_neuron_default_metadata` `opt-barrier-removal` `inline-weights` `replace-rng` `inject-prints` `inject-numerical-errors` | [hlo-misc-cleanup-sweep](../hlo-opt/hlo-misc-cleanup-sweep.md) | CERTAIN |

> **NOTE —** the registration *order* (1..112) in [4.1 §2](../hlo-opt/pass-registry.md#2-the-112-row-master-pass-index) is the registrar's emission order and serves as the **default-pipeline proxy** — it is **not** the run order. The executed run-list is a driver-supplied ordered name subset assembled outside `hlo-opt`'s registrar, then filtered by `--passes` (whitelist) / `--skip-pass` (blacklist). The family table above is sorted by topic, not by registration index; cross-reference [4.1 §2](../hlo-opt/pass-registry.md#2-the-112-row-master-pass-index) for the exact `#`.

---

## 2. Stage 2 — hlo2penguin C-strand MLIR pipeline (32 + 5 = 37 steps)

After the 112 B-strand passes run, optimized HLO is imported into `mhlo` and a **fixed, imperatively-built** MLIR pipeline rewrites it down to `penguin.py` text. There is no registry: `hilo::registerMHLOPasses` @ `0x1ee12f0` is a flat `pm.addPass(...)` spine, and the linear source order *is* the pipeline order. The StableHLO fork (`hilo::registerStableHLOPasses` @ `0x1ee2120`, selected by CompileConfig byte `[+0x1CA]≠0`) is the **same** spine with 5 extra insertions — 32 → 37 steps. Every step except the terminal emitter is gated on a CompileConfig byte (default-off unless a `cl::opt` flips it; the lone default-on is step 6 `LowerComplex`, set by a static initializer).

The full ordered MHLO spine is the canonical listing in [4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md#the-ordered-mhlo-pipeline). Index of the 32 steps by family:

| # | MHLO step (`create…Pass`) | Family / documenting page | Gate | Conf |
|---|---|---|---|---|
| 1 | `Canonicalizer` | upstream MLIR canonicalize | `cfg[0x119]` | CERTAIN |
| 2 | `NeuronControlDepTupleSimplifier` | [controldep-tuple-flatten-mlir](../hlo-opt/controldep-tuple-flatten-mlir.md) | `cfg[0x200]` | CERTAIN |
| 3 | `StableHLOCollectiveBroadcastToAllGather` | collective bcast→all_gather | uncond | CERTAIN |
| 4 | `VerifySupportedOps` | op-legality verifier | `cfg[0x136]` | CERTAIN |
| 5 | `CanonicalizeConv` | [conv-canonicalization](../hlo-opt/conv-canonicalization.md) | `cfg[0x12F]` | CERTAIN |
| 6–7 | `LowerComplex` / `LowerComplexExtra` | complex→real | `cfg[0x11A]`(on) / `[0x11B]` | CERTAIN |
| 8 | `RemoveOptimizationBarriers` | strip `optimization_barrier` | `cfg[0x11C]` | CERTAIN |
| 9–10 | `IdentifyCrossPassTensors` / `Rematerialize` | cross-pass tag + recompute | `cfg[0x116]` | CERTAIN |
| 11 | `CoalesceCollectiveOps` | MLIR collective coalescing | `cfg[0x117]` | CERTAIN |
| 12 | `ConvertFSPatternToCustomCalls` | FS subgraph → `AwsFSPattern5` | `cfg[0x11F]` | CERTAIN |
| 13 | `RemoveDeadValues` | upstream MLIR DCE | uncond | CERTAIN |
| 14 | `NeuronInstCombine` | [neuron-instcombine-mlir](../hlo-opt/neuron-instcombine-mlir.md) | uncond | CERTAIN |
| 15 | `NeuronOpFusion` (6 sub-passes) | [op-fusion-dot-elementwise](../hlo-opt/op-fusion-dot-elementwise.md), [rmsnorm-fusion-cluster-codegen](../hlo-opt/rmsnorm-fusion-cluster-codegen.md) | `cfg[0x120]` | CERTAIN |
| 16 | `FoldIota` | pow(iota)→const | `cfg[0x118]` | CERTAIN |
| 17–18 | `SchedulePostorder` / `AnalyzeSchedule` | [schedule-fusion-composite](../hlo-opt/schedule-fusion-composite.md) | `cfg[0x130]`/`[0x134]` | CERTAIN |
| 19 | `ReplaceTokenTypeWithU8` | `!mhlo.token`→`tensor<ui8>` | uncond | CERTAIN |
| 20 | `ScatterMotion` | scatter code-motion | uncond | CERTAIN |
| 21 | `ScheduleFusion` | [schedule-fusion-composite](../hlo-opt/schedule-fusion-composite.md) | `cfg[0x135]` | CERTAIN |
| 22–23 | `HoistCompute` / `MemcastMotion` | hoist / late-cast motion | `cfg[0xE9]`/`[0xE5]` | CERTAIN |
| 24 | `PruneFunctions` | dead-function elim | uncond | CERTAIN |
| 25 | `SixtyFourHack` (**prepended** before 26) | i64→i32 emulation | `qword_9C71338==0` | HIGH |
| 26 | `PenguinizeIO` | [stablehlo-printer-penguinize](../hlo-opt/stablehlo-printer-penguinize.md) | uncond | CERTAIN |
| 27 | `CanonicalizeForTensorizer` (8 sub-rewriters) | [canonicalize-for-tensorizer](../hlo-opt/canonicalize-for-tensorizer.md) | uncond | CERTAIN |
| 28 | `TensorizerLegalization` | [tensorizer-legalization](../hlo-opt/tensorizer-legalization.md) | uncond | CERTAIN |
| 29–30 | `LegalizeAlias` / `VerifyAliasing` | insert AliasingCopy + RAW check | uncond / `cfg[0x13C]` | CERTAIN |
| 31 | `MLIRInstructionHistogram` | op-count diagnostics | uncond | CERTAIN |
| 32 | `MhloToPyPenguin` (**terminal**) | [mhlo-to-python-printer-driver](../hlo-opt/mhlo-to-python-printer-driver.md), [-heavy](../hlo-opt/mhlo-to-python-printer-heavy.md) | uncond | CERTAIN |

### The 5 StableHLO-only insertions (32 → 37)

The StableHLO spine twins every MHLO step and adds 5 of its own ([4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md#the-five-stablehlo-only-insertions)):

| After step | StableHLO-only insertion | Role | Conf |
|---|---|---|---|
| 1 | `HloLegalizeToStablehlo` | `mhlo` → `stablehlo` bridge | CERTAIN |
| 1 | `ConvertCustomCallToAllReduce` | `custom_call("mhlo.all_reduce")` → `all_reduce` | CERTAIN |
| 14 | `FusionToComposite` | `mhlo.fusion` → `stablehlo.composite` | CERTAIN |
| 26 | `LegalizeSRA` | `shift_right_arithmetic` signedness | CERTAIN |
| 26 | `LegalizeScatter` | `stablehlo.scatter` → F32 + canonical body | CERTAIN |

> **QUIRK — step 25 is prepended, not appended.** `SixtyFourHack`'s gate (`jz`→altblk @ `0x1ee19a0`, on global `qword_9C71338==0`) inserts it *before* step 26 `PenguinizeIO`, so it runs ahead of IO packaging despite appearing 25th in the source. A reimplementer who `addPass`-es it in textual position sequences it wrong. The ordering is read from the disassembled control flow only — there is no registration table that states it.

---

## 3. Stage 3 — Penguin middle-end (248 modules, run-list ~varies)

The Penguin middle-end is **not** a registry and **not** a flat pipeline — it is a *catalog* of Cython pass modules sequenced at run time by generator functions. Four directories partition the catalog; counts are `ls`-verified (excluding `__init__`):

| Directory | Modules | Scope | Documenting page |
|---|---:|---|---|
| `penguin/transforms/` | **71** | target-independent IR passes (DCE, VN, LICM, simplify, delinearize, predicate) | [5.7](../penguin/pass-roster-pipeline.md#transforms--target-independent-71-modules) |
| `penguin/targets/transforms/` | **131** | target-oriented, shared Sunda+Tonga (alloc/spill, layout, tiling, collective, DMA, type-legalize, Tritium fusion) | [5.7](../penguin/pass-roster-pipeline.md#targetstransforms--target-oriented-131-modules) |
| `penguin/targets/sunda/passes/` | **6** | Sunda-specific ISel / tiling | [5.7](../penguin/pass-roster-pipeline.md#per-target-packs) |
| `penguin/targets/tonga/passes/` | **40** | Tonga-specific ISel / tiling / scheduler | [5.7](../penguin/pass-roster-pipeline.md#per-target-packs) |
| **Total** | **248** | (`targets/cayman/passes/`, `targets/core_v4/passes/` ship only `__init__` — reuse Sunda/Tonga) | |

The `targets/transforms/autotune/` subpackage (6 modules: `Autotuner` `_Compiler` `_PerformanceMetric` `_Search` `_TreeSearch` `_TreeSearchVisualizer`) is counted separately, not in the 131.

> **GOTCHA — module count ≠ pass count.** ~8–10 modules in the two `transforms/` dirs export no `Pass`/`DotTransform` subclass — they are helper libraries (`DataflowUtil` `InstTransformUtils` `IPUtils` `LoopTransformUtils` `TensorContractUtils` `TensorOpUtils` `InstBuilder` `IntrinsicBuilder` `DataflowUtils`). The real pass-class set is the subset whose top-level class subclasses `DotTransform` or `BaseAnalysis`. The 248 is the verifiable *module* anchor; the pass-class subset is a few smaller — **[INFERRED]**, since the by-class filtering was spot-checked rather than exhaustively enumerated.

### Run order — the driver, not the directory

There is no static order in the directory listing. The order is set by the four `SharedCodeGenFlow` generators (`peephole_optimizations`, `legalize_pe_insts`, `nki_codegen`, `codegen_optimization_post_tritium_fusion`), each `yield`ing `PassType` specs that a `PassConstructorBuilder` folds. The canonical post-fusion list (with its `required`/`optional` qualifiers and the spill-vectorize-respill double-`FastSpillGeneration`) is in [5.7](../penguin/pass-roster-pipeline.md#the-driver--sharedcodegenflows-four-generators). Index of the high-population families:

| Family | Representative passes | Documenting page | Conf |
|---|---|---|---|
| Alloc / spill | `AllocateBlocks` `MaxLiveSpiller` `FastSpillGeneration` `StackAllocator` `SpillPSum` | [scheduling-minreg](../penguin/scheduling-minreg.md), [tensor-buffer-node](../penguin/tensor-buffer-node.md) | CERTAIN |
| Layout | `LayoutTilingPipeline` `PAGLayout*` `LayoutPreprocessing` `NKIKernelLayout` | [layout-tiling-pipeline](../penguin/layout-tiling-pipeline.md), [layout-middle-end](../penguin/layout-middle-end.md) | CERTAIN |
| Tiling / vectorize | `FlattenAxesForTiling` `VectorizeLoop` `SFKVectorizer` `LoopSplitting` `PG*Analysis` | [loop-transform-clients](../penguin/loop-transform-clients.md) | CERTAIN |
| Loop transforms | `LICM` `LoopFusion` `PerfectLoopNest` `FlattenLoop` `Delinearization` | [loop-transform-clients](../penguin/loop-transform-clients.md) | CERTAIN |
| ISL / affine | `IslSimplifier`-family, `ModDivDelinear`, `AffinePredicateResolution` | [isl-simplifier](../penguin/isl-simplifier.md), [isl-codegen](../penguin/isl-codegen.md), [affine-expr-algebra](../penguin/affine-expr-algebra.md) | HIGH |
| Collective (SPMD/CC) | `CCOpFusion` `CoalesceCCOp` `TileCCOps` `SPMDCodeGen` `SimpleAllReduceTiling` | [data-movement-fusion](../penguin/data-movement-fusion.md) | CERTAIN |
| DMA | `DMALegalizer` `DMALocalityOpt` `DataStreaming` `DramToDramTranspose` | [dge-level-dynamic-dma](../penguin/dge-level-dynamic-dma.md), [tensorcopy-dynamic-generators](../penguin/tensorcopy-dynamic-generators.md) | CERTAIN |
| Software pipelining | `SoftwarePipelineCodeGen` | [software-pipelining](../penguin/software-pipelining.md) | CERTAIN |
| Tritium fusion (+autotune) | `TritiumFusion` `TritiumFusionBase`, `autotune/Autotuner` | [5.7 autotuner](../penguin/pass-roster-pipeline.md#where-the-autotuner-hooks-in) | CERTAIN |
| ISel (per-target) | `SundaISel` (sunda), `TongaISel` (tonga) | [5.7](../penguin/pass-roster-pipeline.md#per-target-packs) | CERTAIN |
| Codegen / emit | `NkiCodegenPass`, `BirCodeGenLoop`, `targets/codegen/*` | [ir-mlir-bir-mapping](../penguin/ir-mlir-bir-mapping.md) | CERTAIN |

> **QUIRK — a pass can appear twice in the run-list.** `FastSpillGeneration` runs before *and* after `SFKVectorizer` (vectorization creates new live ranges to spill); `nki_codegen` is chained into the post-fusion list. A `set`-based reimplementation that de-dups specs by class silently drops the second spill. The Penguin run-list length is therefore *not* a function of the module count — it is the generator's yield sequence, with repeats. Membership is exact; the *order* is **[INFERRED]** from the version-counter source-order trace.

---

## 4. Stage 4 — walrus backend (150 names → 121 classes)

The walrus backend is a `GeneratorRegistration` registry: **150** `register_generator_<name>__` static initializers each install a `name → factory` entry, but they construct only **121** distinct C++ pass classes — **10** classes are registered under multiple names, parameterized at construction by a memory space, optimization level, or scheduling phase. The pipeline schedules these into **~180** steps (some names at several positions, plus interposed `!isRealPass` check/dump/sim passes).

- **150**, straight off the binary: `nm -DC libwalrus.so | rg -o 'register_generator_[a-z0-9_]+' | sort -u | wc -l` = 150.
- **121**, derived: the count of distinct `_M_invoke` ctor targets over the 150 bodies. The 10 multi-registered groups contribute 29 surplus names (`9+7+3+2+2+2+1+1+1+1`); `121 + 29 = 150`.

The full 150-name → class → granularity → evidence table is the canonical listing in [8.1 §"The full 150-name table"](../walrus/backendpass-registry.md#the-full-150-name--class--granularity-table). The 150→121 collapse must be represented, not flattened — here are the **10 multi-registered classes** that produce the gap:

| Class | Names | Surplus | Discriminator | Conf |
|---|---:|---:|---|---|
| `MemoryAnalysis` | 10 | 9 | which prior pass the snapshot follows (post-rotation/coloring/dma/sched/unroll) | CERTAIN |
| `ColoringAllocator` | 8 | 7 | `Type ∈ {SB,PSUM,REG,DRAM,DRAM-shared}` + `post_lnk`/`debug` | CERTAIN |
| `AddressRotation` | 4 | 3 | space `dram`/`psum`/`sb` + `post_schedule` | CERTAIN |
| `DMAOptimization` | 3 | 2 | `psum`/`sb`/`input-coalescing` | CERTAIN |
| `FullUnroll` | 3 | 2 | `unrollLevel ∈ {all,some}` + `memloc-generation` | CERTAIN |
| `SeparateLoadAndCompute` | 3 | 2 | base / `with_memset` / `post_ada` | CERTAIN |
| `AntiDependencyAnalyzer` | 2 | 1 | base / `post_shared_dram` | CERTAIN |
| `DeadCodeElim` | 2 | 1 | optlevel `o0` / `o1` | CERTAIN |
| `PerfSimPass` | 2 | 1 | `perf_sim` / `perf_sim_at_end` | CERTAIN |
| `PrefetchScheudling` *(sic — misspelled in binary)* | 2 | 1 | `before_sched` / `after_sched` | CERTAIN |

The other **111** names are 1:1 with their class. Index of the 150 names by family, pointing each at the documenting 8.x page:

| Family | Representative names | Documenting page | Conf |
|---|---|---|---|
| Pass hierarchy / registry | (the `BackendPass` base, forks, `GeneratorRegistration`) | [8.1](../walrus/backendpass-registry.md) | CERTAIN |
| Optlevel pipeline | (the optlevel builders that call `addPass("<name>")`) | [pass-pipeline-optlevels](../walrus/pass-pipeline-optlevels.md) | CERTAIN |
| NKI-AST→BIR / unroll | `translate_nki_ast_to_bir` `unroll` `full_unroll_*` `heuristic_unroll` `flatten_small_loops` | [translate-nki-unroll](../walrus/translate-nki-unroll.md) | CERTAIN |
| DMA legalization | `legalize_strided_dma` `legalize_cce_dma` `lower_generic_indirect` `lower_dynamic_dma` | [dma-legalization](../walrus/dma-legalization.md) | CERTAIN |
| DMA materialization / queues | `dynamic_dma_setup` `dynamic_dma_scan` `alloc_queues` `insert_dma_switch_queue_instance` `optimize_queue_switch` | [dma-materialization](../walrus/dma-materialization.md), [dma-queues](../walrus/dma-queues.md) | CERTAIN |
| DMA engine binding | `assign_hwdge_engine` `assign_trigger_engine` `infer_stream_ids` `label_dma_qos` | [dma-engine-binding](../walrus/dma-engine-binding.md) | CERTAIN |
| DMA metrics / profiler | `dma_metrics` `dma_optimization_psum` `dma_optimization_sb` `input_dma_coalescing` | [dmametrics-profiler](../walrus/dmametrics-profiler.md), [alt-allocators-dma-opt](../walrus/alt-allocators-dma-opt.md) | CERTAIN |
| lower_select / control / branch | `lower_select` `lower_control` `lower_branch` | [lower-select-control-branch](../walrus/lower-select-control-branch.md) | CERTAIN |
| Engine-lowering set | `lower_sync` `lower_act` `lower_dve` `lower_ap` `lower_ac` `lower_dma` `lower_symbolic_inst` `lower_local_collectives` | [engine-lowering-set](../walrus/engine-lowering-set.md) | CERTAIN |
| Coloring / DRAM / PSUM alloc | `coloring_allocator_*` (8) `coloring_allocator_with_loop` `linear_scan_allocator` `psum_legalization` | [allocator-drivers](../walrus/allocator-drivers.md), [dram-allocator](../walrus/dram-allocator.md), [psum-allocator](../walrus/psum-allocator.md) | CERTAIN |
| Address rotation | `address_rotation_{dram,psum,sb}` `address_rotation_psum_post_schedule` | [address-rotation](../walrus/address-rotation.md) | CERTAIN |
| Memory analysis (10 snapshots) | `memory_analysis_after_*` | [allocator-drivers](../walrus/allocator-drivers.md) | CERTAIN |
| Scheduling | `pre_sched` `post_sched` `prefetch_scheduling_{before,after}_sched` `instruction_reorder` | [post-sched-schedulers](../walrus/post-sched-schedulers.md) | CERTAIN |
| Dependence graph / dep-opt | `build_fdeps` `dep_opt` `dep_reduction` `order_constraints` `anti_dependency_analyzer*` | [dependence-graph](../walrus/dependence-graph.md), [dep-opt-reduction](../walrus/dep-opt-reduction.md) | CERTAIN |
| Peephole / constprop / remat | `peephole_opts` `early_peephole_opts` `constant_propagate` `remat_optimization` `remove_redundancies` `seq_inst_opt` | [peephole-constprop-remat](../walrus/peephole-constprop-remat.md) | CERTAIN |
| Matmul ordering / acc-groups | `order_column_tiled_mms` `legalize_mm_accumulation_groups` | [matmul-ordering-accgroups](../walrus/matmul-ordering-accgroups.md) | CERTAIN |
| Loop-opt (backend) | `loop_optimization` | [loopopt-licm-fusion](../walrus/loopopt-licm-fusion.md), [loopopt-transforms](../walrus/loopopt-transforms.md) | CERTAIN |
| Value numbering / mem2reg | `value_numbering` `vn_splitter` `mem2reg` `non_ssa_legalization` | [legality-dispatch](../walrus/legality-dispatch.md) | HIGH |
| LNC splitter / barrier | `lnc_splitter` `lnc_barriercheck` `lnc_verifier` `shared_mem_cb_insertion` | [lnc-splitter](../walrus/lnc-splitter.md), [barriercheck](../walrus/barriercheck.md) | CERTAIN |
| Local collectives | `lower_local_collectives` `coalesce_multichannel_cc_ops` `sync_before_global_cc` | [local-collectives](../walrus/local-collectives.md) | CERTAIN |
| BIR linker / kernel inline | `bir_linker` `inline_bir_kernel` `inline_nki_kernel` `memreserve` `runtime_memory_reservation` | [bir-linker](../walrus/bir-linker.md), [memreserve-kernel-inline](../walrus/memreserve-kernel-inline.md) | CERTAIN |
| Codegen / bin emission | `codegen` `neff_packager` | [codegen-driver](../walrus/codegen-driver.md), [bin-emission](../walrus/bin-emission.md) | CERTAIN |
| Verifiers / serdes | `birverifier` `bir_racecheck` `verify_bir_serdes` `oom_checker` | [birverifier-per-op](../walrus/birverifier-per-op.md) | CERTAIN |
| Perf-sim / metrics (`!isRealPass`) | `perf_sim` `perf_sim_at_end` `perf_sim_package_pass` `report_stats` `dumper` `hasher` | [perfsim-cost-model](../walrus/perfsim-cost-model.md), [perf-sim-wiring](../walrus/perf-sim-wiring.md), [metricstore](../walrus/metricstore.md), [parserdumper](../walrus/parserdumper.md) | CERTAIN |

> **GOTCHA — `serial*` is not "always serial".** In [8.1's full table](../walrus/backendpass-registry.md#the-full-150-name--class--granularity-table) the granularity column shows a Fork tag *only* where an explicit `add*ParallelPass<Class>` template instance exists; `serial*` means "added by name (default serial child)" — but the *same* class may be fork-wrapped at a different call-site, because granularity is a property of the call-site, not the class. Do not read `serial*` as a guarantee.

---

## 5. How the counts are grounded

Each cross-stage count this catalog turns on, and what pins it in the cp310 binaries:

1. **Stage 1 = 112.** `objdump -d --start-address=0x1e72270 --stop-address=0x1e744b0 hlo-opt | rg -c 'call.*1ebc3f0'` → **112** calls to `RegisterHloPass`. One name per call, key = `pass->name()`.
2. **Stage 2 = 32 MHLO + 5 = 37 StableHLO.** `registerMHLOPasses` @ `0x1ee12f0` (3096 B) is a flat 32-step `addPass` spine; `registerStableHLOPasses` @ `0x1ee2120` (3142 B) is the twin plus 5 insertions. The five inserts (`HloLegalizeToStablehlo`, `ConvertCustomCallToAllReduce`, `FusionToComposite`, `LegalizeSRA`, `LegalizeScatter`) are enumerated in [4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md#the-five-stablehlo-only-insertions). The step count is exact; the `SixtyFourHack` prepend at step 25 rests on disassembled control flow alone.
3. **Stage 3 = 248 modules (71/131/6/40).** `ls <dir>/*.so | grep -v __init__ | wc -l` in each of the four directories → **71, 131, 6, 40**, summing to 248. `targets/transforms/autotune/` accounts for the 6. `targets/cayman/passes/` and `targets/core_v4/passes/` contain only `__init__`. That is the *module* count; the pass-class subset is slightly smaller because of helper libs, flagged in [§3](#3-stage-3--penguin-middle-end-248-modules-run-list-varies).
4. **Stage 4 = 150 names → 121 classes.** `nm -DC libwalrus.so | rg -o 'register_generator_[a-z0-9_]+' | sort -u | wc -l` → **150**. The 10 multi-registered groups (`MemoryAnalysis`=10, `ColoringAllocator`=8, `AddressRotation`=4, `DMAOptimization`/`FullUnroll`/`SeparateLoadAndCompute`=3 each, four more ×2) contribute 29 surplus names, and `121 + 29 = 150`. The 150 is grep-able; the 121 is a distinct-ctor-target count derived over the [8.1](../walrus/backendpass-registry.md) table.
5. **The four name→pass mechanisms really are different.** Stage 1 = `llvm::StringMap` keyed by `name()` read-back; Stage 2 = imperative `addPass` with no name table; Stage 3 = `PassType` specs folded by `PassConstructorBuilder`, again no name table; Stage 4 = `_Hashtable` keyed by the literal `registerGenerator` string. Each is symbol-anchored in its normative page ([4.1](../hlo-opt/pass-registry.md), [4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md), [5.7](../penguin/pass-roster-pipeline.md), [8.1](../walrus/backendpass-registry.md)).

No pass name on this page is fabricated: every name is the verbatim string carried by its normative source page, which in turn read it from the binary — a `name()` literal, a `create…Pass` factory symbol, a `__pyx_n_s_` interned token, or a `register_generator_*__` symbol. Where this catalog points a family at a documenting wiki page, the slug is present on disk; families with no dedicated detail page point at their stage's normative registry page. The libBIR and libwalrus binaries and their IDA databases are present in-corpus, so the backend class addresses cited via [8.1](../walrus/backendpass-registry.md) are read, not inferred.

> **NOTE — what this page is *not*.** It is an index, not a replacement for the four normative tables. The per-row name→class→vtable→entry detail for Stage 1 lives in [4.1 §2](../hlo-opt/pass-registry.md#2-the-112-row-master-pass-index); the per-step MLIR spine in [4.32](../hlo-opt/hlo2penguin-mlir-pipeline.md#the-ordered-mhlo-pipeline); the Penguin driver order in [5.7](../penguin/pass-roster-pipeline.md#the-driver--sharedcodegenflows-four-generators); and the full 150-name → class → granularity table in [8.1](../walrus/backendpass-registry.md#the-full-150-name--class--granularity-table). This catalog reconciles their counts and gives one map across all four.

---

## Related Components

| Stage page | Relationship |
|---|---|
| [4.1 — hlo-opt Pass Registry](../hlo-opt/pass-registry.md) | The 112-name `--passes` registry; source of [§1](#1-stage-1--hlo-opt-b-strand-112-passes) |
| [4.32 — hlo2penguin MLIR Pipeline](../hlo-opt/hlo2penguin-mlir-pipeline.md) | The 32+5-step MLIR spine; source of [§2](#2-stage-2--hlo2penguin-c-strand-mlir-pipeline-32--5--37-steps) |
| [5.7 — Penguin Pass Roster & Pipeline Driver](../penguin/pass-roster-pipeline.md) | The 248-module catalog + `SharedCodeGenFlow` order; source of [§3](#3-stage-3--penguin-middle-end-248-modules-run-list-varies) |
| [8.1 — BackendPass Hierarchy & 150→121 Registry](../walrus/backendpass-registry.md) | The 150-name walrus registry; source of [§4](#4-stage-4--walrus-backend-150-names--121-classes) |

## Cross-References

- [The hlo-opt Pass Registry (the `--passes` Table)](../hlo-opt/pass-registry.md) — Stage 1, the 112-row canonical table this catalog indexes
- [hlo2penguin MLIR Pipeline Order & Entry Flow](../hlo-opt/hlo2penguin-mlir-pipeline.md) — Stage 2, the imperative `addPass` MLIR spine
- [Penguin Pass Roster & Pipeline Driver](../penguin/pass-roster-pipeline.md) — Stage 3, the Cython module catalog and `OptLevel` gate
- [BackendPass Hierarchy & the 150-Name→121-Class Registry](../walrus/backendpass-registry.md) — Stage 4, the walrus `GeneratorRegistration` registry
- [The walrus Pass Pipeline & Optlevel Planes](../walrus/pass-pipeline-optlevels.md) — how the 150 walrus names are scheduled per `-O` level
- [The Opt-Level Planes — -O0..-O3](../frontend/opt-level-planes.md) — the `--optlevel` dial that gates Stage 3 and Stage 4 selection
- [walrus_driver Backend CLI & Pass Vocabulary](../frontend/walrus-driver-cli.md) — the `--pass` surface that drives the Stage 4 registry
- [Master Opcode Reference](master-opcode-table.md) · [Master Dtype / Enum Reference](master-enum-dtype-table.md) — the sibling Part-14 reference catalogs
