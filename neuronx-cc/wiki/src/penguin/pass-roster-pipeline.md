# Penguin Pass Roster and Pipeline Driver

> *All symbols, counts, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/starfish/penguin/`; cp311/cp312 are layout-identical, only the Cython hash suffix differs). Treat every count as version-pinned — re-run the `ls`/`strings`/`nm` recipes below against any other wheel.*

## Abstract

The Penguin middle-end is **not** a monolithic optimizer. It is a *catalog* of compiled pass modules — one Cython `.so` per pass — plus a small set of **driver generators** that sequence a subset of them into an ordered pipeline at run time. There is no `addPass(...); addPass(...)` registry hard-coded in one function the way LLVM's `PassBuilder` works. Instead, each per-target `CodeGenFlow` driver is a Cython *generator* that `yield`s pass *specifications* (`PassType` objects, each a `(pass_class, kwargs, required|optional)` triple) into a `PassConstructorBuilder`, which folds them via the Python `+`/`+=` operators into a list a `PassManager` then runs. This page is the index for that whole machine: the roster (which passes exist), the driver (which order they run in), the runner (`DotTransform`/`PassManager`/`IterativePassManager`), and the gate (`OptLevel`).

The roster splits cleanly by target-dependence. `penguin/transforms/` holds **71** target-independent IR passes (DCE, value numbering, LICM, the simplifier family, delinearization, predicate resolution). `penguin/targets/transforms/` holds **131** target-oriented passes shared across the Sunda and Tonga backends (allocation/spill, layout, tiling, collective lowering, DMA, type legalization, Tritium fusion). Two thin per-target packages — `targets/sunda/passes/` (**6**) and `targets/tonga/passes/` (**40**) — add the backend-specific ISel/tiling/scheduler passes. Counts are `ls`-verified below and are exact module counts excluding `__init__`.

The driver core lives in one unstripped, fully-decompiled file: `targets/sunda/SharedCodeGenFlow.{py→.so}`, banner `"Driver functions for Sunda CodeGen"`. It exposes exactly four generator functions — `peephole_optimizations`, `legalize_pe_insts`, `nki_codegen`, and the centerpiece `codegen_optimization_post_tritium_fusion` — and both the Sunda and Tonga `CodeGenFlow` drivers import and call them. The reimplementation frame the rest of this page builds on: **roster (catalog) → `PassType` specs → `PassConstructorBuilder` (assemble) → `PassManager`/`DotTransform` (run, with `run_or_rollback` safety) → `OptLevel` (skip whole stages at lower `-O`).**

For reimplementation, the contract is:

- **The roster taxonomy** — the four module directories, their exact counts, and how to tell a real `Pass` class from a helper/util module that ships in the same directory.
- **The spec/build/run separation** — `PassType` (a spec, not a pass), `PassConstructorBuilder` (folds specs via `nb_add`/`nb_inplace_add`), `PassManager`/`IterativePassManager` (the runners), all subclassing the `DotTransform` visitor base.
- **The driver sequencing** — the four `SharedCodeGenFlow` generators, with `codegen_optimization_post_tritium_fusion` given as annotated pseudocode in source-order, and the per-pass `required=`/`optional=` qualifier on each.
- **The gating model** — `OptLevel` as an inverted staged-skip enum (`skip_middle_end` → `skip_loop_level_transformations` → `skip_allocators`), applied at the *flow* level, plus per-pass `Options.clOptBool('<flag>')` gates.

| | |
|---|---|
| **Driver file** | `targets/sunda/SharedCodeGenFlow.cpython-310-…so` — banner `"Driver functions for Sunda CodeGen"` |
| **Driver generators** | `peephole_optimizations`, `legalize_pe_insts`, `nki_codegen`, `codegen_optimization_post_tritium_fusion` |
| **Spec builder** | `PassConstructor.cpython-310-…so` — `PassConstructorBuilder`, `PassType`, `build_pass_constructor`, `build_pass_list` |
| **Runner base** | `DotTransform.cpython-310-…so` — `DotTransform` (visitor base) + `PassManager` / `IterativePassManager` / `SuperPass` / `BaseAnalysis` |
| **Opt-level gate** | `ir/OptLevel.cpython-310-…so` — `@unique @total_ordering` enum, custom `__gt__` |
| **Target-independent passes** | `penguin/transforms/` — **71** modules (excl. `__init__`) |
| **Target-oriented passes** | `penguin/targets/transforms/` — **131** modules (excl. `__init__`, excl. `autotune/`) |
| **Sunda / Tonga pass packs** | `targets/sunda/passes/` **6** · `targets/tonga/passes/` **40** |
| **IR level in / out** | tensoriser-IR (op level) → ISel → `<Target>ISAInst`-level tensoriser-IR → BIR ([5.18 BirCodeGenLoop](ir-mlir-bir-mapping.md)) |

---

## The Roster — what passes exist

The roster is enumerable by `ls`. Every pass is its own Cython module; there is no shipped `.py`. The four directories below partition the catalog. Counts exclude `__init__.cpython-310-….so` (the package marker) and, for `targets/transforms/`, the `autotune/` subpackage which is counted separately.

> **GOTCHA — module count is not pass count.** Several modules in `transforms/` and `targets/transforms/` are *helper* libraries (`DataflowUtil`, `IPUtils`, `LoopTransformUtils`, `TensorOpUtils`, `InstTransformUtils`, `TensorContractUtils`, `InstBuilder`, `IntrinsicBuilder`, `DataflowUtils`) that export no `Pass`/`DotTransform` subclass. A reimplementer enumerating "passes" by directory listing over-counts by ~8–10. To get the real pass set, filter to modules whose top-level class subclasses `DotTransform` or `BaseAnalysis` (every real pass does — see [the runner section](#the-runner--dottransform-passmanager-iterativepassmanager)). The directory counts below are *module* counts, given exact because they are the verifiable anchor; the pass-class subset is a few smaller.

### Verification recipe

```bash
# from <wheel>/neuronxcc/starfish/penguin/
ls transforms/*.so          | grep -v __init__ | wc -l    # -> 71
ls targets/transforms/*.so  | grep -v __init__ | wc -l    # -> 131  (autotune/ is a subdir, not counted)
ls targets/sunda/passes/*.so| grep -v __init__ | wc -l    # -> 6
ls targets/tonga/passes/*.so| grep -v __init__ | wc -l    # -> 40
```

> **GOTCHA — count the three target directories separately.** `targets/transforms/` holds **131** *shared* target transforms; the per-target packs add **40** (tonga) and **6** (sunda), for 177 target-oriented modules across all three. A single "~140 target passes" figure is what you get by half-merging them. Each count already excludes `__init__` — the tonga pack is 40, not 41.

### `transforms/` — target-independent (71 modules)

These operate on op-level tensoriser-IR and never name a target. Grouped by role; names are the literal module stems (`<Name>.cpython-310-….so`).

| Group | Modules | Confidence |
|---|---|---|
| Alias / dependency | `AliasDependencyElimination` `AliasDependencyInduction` `AliasDependencyReset` `AliasDependencyVerificationPass` `AliasDependencyVerifier` `TransformMustAlias` `LegalizeOpLevelAlias` `OptimizeAliasedCopyChain` | CERTAIN |
| DCE / DSE / VN | `DeadCodeElimination` `DeadStoreElimination` `ValueNumbering` `MemcpyElimination` `ZeroSizeTensorElimination` | CERTAIN |
| Op-elimination | `CastOpElimination` `ConcatOpElimination` `ReshapeOpElimination` `SliceOpElimination` `TransposeOpElimination` `PadElimination` | CERTAIN |
| Simplify | `Simplifier` `SuperSimplifier` `SimplifySlice` `TensorOpSimplifier` `GenericAccessSimplifier` `IPSimplifier` `SimplifyPredicates` | CERTAIN |
| Loop / affine | `LICM` `LoopFusion` `FlattenLoop` `PerfectLoopNest` `EliminateDivs` `ModDivDelinear` `Delinearization` `DelinearizationBase` `ConcatDelinearizer` `AffinePredicateResolution` | CERTAIN |
| Predicate | `ResolvePredicates` `ResolveComplicatePredicates` `PredicateAffineSelect` `MaskPropagation` `InsertConflictResolutionOps` | CERTAIN |
| Tensor-op | `LowerTensorOp` `LateLowerTensorOp` `TensorOpTransform` `RecognizeOpIdiom` `InferIntrinsic` `MutateDataType` | CERTAIN |
| Analysis | `RangeAnalysis` `LoadStoreDependencyAnalysis` `IPLiveInterval` `IPSubgraphTensorAnalysis` `AnalyzeKernel` `AxesGroup` `DynamicInstEstimator` `DetailDynInst` | CERTAIN |
| Codegen / IR / debug | `NkiCodegenPass` `SimulatorPass` `PrintFunction` `DataflowViewer` `LayoutViewer` `DoNothing` `Region` `PatternMatch` `CanonicalizeIR` `HoistAllGather` | CERTAIN |
| Helper (no Pass class) | `DataflowUtil` `InstTransformUtils` `IPUtils` `LoopTransformUtils` `TensorContractUtils` `TensorOpUtils` | HIGH |

> **NOTE —** `CanonicalizeIR` and `HoistAllGather` live in `transforms/`, not under `targets/`. Several names that read as target-independent are not `transforms/` modules in this wheel at all: the `Infer*` passes (`InferInitValue`, `InferSharedMemLoc`, `InferNonlocalTensors`) live in `targets/transforms/`, and the Isl/IntegerSet machinery (`IntegerSetAnalysis`, `IslSimplifier`) is folded into [the AffineExpr algebra](affine-expr-algebra.md) rather than shipped as a standalone pass module.

### `targets/transforms/` — target-oriented (131 modules)

Shared across Sunda and Tonga. The high-population groups (counts are within-group, the 131 total is `ls`-verified):

| Group | Representative modules | Confidence |
|---|---|---|
| Alloc / spill | `AllocateBlocks` `AllocationDecision` `AllocatorState` `BufferState` `GlobalSpillCtx` `MaxLiveSpiller` `FastSpillGeneration` `SpillPSum` `SpillFreeKernel` `StackAllocator` `ModuloAllocation` `IntervalMinCut` `AnnotateNoSpill` `NeuronStackLiveInterval` `CachedLiveInterval` `TensorLifetimeAnalysis` `TensorLiveRange` | CERTAIN |
| Layout | `LayoutPreprocessing` `LayoutRequirementAnalysis` `LayoutTilingPipeline` `LocalLayoutOpt` `CycleBasedLayoutCostModel` `NKIKernelLayout` `PAGLayoutAnalysis` `PAGLayoutGreedySearchAlgorithm` `PAGLayoutHelpers` `OperatorLayoutVisualizer` | CERTAIN |
| Tiling / vectorize | `FlattenAxesForTiling` `FactorizeBlkDims` `FactorizeFreeDims` `LoopSplitting` `PartitionVectorization` `PackParDim` `PartialLoopFusion` `PartialSimdFusion` `VectorizeLoop` `VectorizeMatMult` `Vectorizer` `SFKVectorizer` `PGAnalysis` `PGAnalysisForTiling` `PGAnalysisHelpers` `PGTilingHelpers` `BFComputeCutting` `PComputeCutting` | CERTAIN |
| Collective (SPMD/CC) | `AGOrderingAnalysis` `BucketizeCCOp` `CCOpFusion` `CCOpAxesGroupAnalysis` `CCOpAxesGroupDelinearizer` `CoalesceCCOp` `FineGrainedCCOpFusion` `LowerCCOpBlockAxis` `TileCCOps` `LegalizeCCOpLayout` `LowerToSendRecv` `SimpleAllReduceTiling` `HoistFSDPCollectives` `RewriteReplicationMatmul` `SPMDCodeGen` `SpmdDCE` `TensorParallel` `ShardingUtils` `LowerShardAxis` `InferShardAxis` `InsertImplicitShardAxis` `ReinsertShardAxis` `RemoveShardedPartitionAxes` | CERTAIN |
| DMA | `DMALegalizer` `DMALocalityOpt` `AssignDMAQoSLabels` `DataStreaming` `DataLocalityOpt` `DramToDramTranspose` `InsertOffloadedTransposes` | CERTAIN |
| Lowering / legalize | `TargetLowering` `LowerIntrinsics` `LowerBroadcast` `LowerTranspose` `LegalizeType` `LegalizeSundaAccess` `LegalizeTongaAccess` `LocalLegalizeType` `RelaxPredicates` `TransformConvOp` `MacroGeneration` `SoftwarePipelineCodeGen` | CERTAIN |
| Tonga-flavored | `TongaCpyElim` `TongaDelinear` `TongaInstComb` `TongaIslSimplifier` `TongaLICM` `TongaLiveInterval` `TongaLoopFusion` `TongaLoopInterchange` `TongaSimplifier` `TongaSimplifyPredicates` | CERTAIN |
| Tritium fusion | `TritiumFusion` `TritiumFusionBase` (the "post_tritium_fusion" pre-stage; autotuner host — see [§ autotuner](#where-the-autotuner-hooks-in)) | CERTAIN |
| Type / cast | `DTypeMutator` `EnforceAluDTAcc` `DemoteLargeTensors` `ExpandBatchNorm` | CERTAIN |
| Verify / sim / latency | `NeuronVerifier` `UnshardBIRKernelForSimulation` `InstructionLatencyModel` `KernelLatencyInfo` | CERTAIN |
| Infer | `InferInitValue` `InferNonlocalTensors` `InferPSumTensor` `InferSharedMemLoc` | CERTAIN |
| Misc | `DeConcat` `MatMultCombine` `Rematerialization` `RewriteWeights` `RoundtripTranspose` `SimplifyMacroPredicates` `SimplifyTensor` `SoftmaxDivisionDelay` `StaticTransposeLocalTensor` `SplitAPUnionSets` `FlattenAPIndices` `TensorContract` `TensorInitialization` `InlineNKIKernels` `InsertCoreBarrier` `CanonicalizeDAG` | CERTAIN |

### Per-target packs

```text
targets/sunda/passes/  (6, excl __init__):
    InferIntrinsicOnCC  LateLegalizeInst  LegalizePartitionReduce
    LegalizeSundaMacro  SundaISel  SundaSizeTiling

targets/tonga/passes/  (40, excl __init__):
    AutoCastFP32  BroadcastWeights  ColoringAllocator  CommuteConcat  DMAProfiler
    ExpandISAMacro  FlattenMacroLoop  GlobalLayoutOpt  InferTongaTensor
    InsertIOTransposes  InsertLocalTransposes  IPGlobalLayoutOpt  LegalizePartitionTile
    LegalizeTongaMacro  LinearizeFreeDim  LowerPartitionTile  ParAxesAnnotation
    ParLayoutOpt  PartitionLocalityOpt  Recompute  ReshapeWeights  ResolveAccessConflict
    Scheduler  SimplifyTongaTensor  SplitAccGrp  SplitFreeDim  StaticProfiler
    TilingProfiler  TongaAccessAnalysis  TongaBufferUsageAnalysis  TongaEliminateCasts
    TongaISel  TongaLayoutAnalysis  TongaPerfectLoopNest  TongaSizeTiling
    TongaValueNumbering  TransformLayout  UnrollReduceMacro  VectorizeDMA  WeightCoalescing
```

> **NOTE —** `targets/cayman/passes/` and `targets/core_v4/passes/` ship only `__init__` — no per-pass modules. Those backends reuse the Sunda/Tonga packs; their own packs are placeholders in this build.

The Penguin→BIR / →NKI emit drivers live in `targets/codegen/`: `BirCodeGenLoop` (banner `"BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level."`), `NkiCodegen`, `CodeGenBase`, `DCE`, `DumpGraphAndMetadata`. The autotune engine is `targets/transforms/autotune/`: `Autotuner`, `_Compiler`, `_PerformanceMetric`, `_Search`, `_TreeSearch`, `_TreeSearchVisualizer`.

---

## The Runner — `DotTransform`, `PassManager`, `IterativePassManager`

Every pass — target-independent or not — subclasses one base, `DotTransform`, defined in `DotTransform.cpython-310-….so`. This is the shared *visitor*: the runner walks the comp-unit (Module → Function → Block/BasicBlock → Stmt → Macro/While/ScopeRegion) and the pass overrides the per-node hooks. It is the same base [`BirCodeGenLoop`](ir-mlir-bir-mapping.md) subclasses to emit BIR — so "lower to BIR" is structurally just another `DotTransform` walk.

### Purpose

`DotTransform` provides the drive loop, the IR walk, per-statement hooks, the **rollback-on-failure** safety net, and the `changed` bookkeeping that `IterativePassManager` uses for fixpoint. Analyses use a sibling base, `BaseAnalysis`, with the same drive/rollback harness but non-mutating (`analyzeModule`/`analyzeFunction` instead of `transform*`).

### Method Map

| Method group | Symbols (`DotTransform.<name>`) | Role |
|---|---|---|
| Drive | `runOnModule` `runOnFunction` `run_` `timed_run_` `runTransforms` | top-level entry; `timed_run_` wraps `run_` with timing |
| IR walk | `transformModule` `transformFunction` `transformBasicBlock` `transformBlock` `transformScopeRegion` `transformStmts` `transformWhile` `transformMacro` `transform` | recursive descent; `transform` is the leaf node hook |
| Per-stmt hooks | `beforeStmtTransform` `afterStmtTransform` | overridden by passes, and by `BirCodeGenLoop` across the binary boundary |
| Safety | `run_or_rollback` `always_rollback` `run_with_exception_handling` `rethrow_exception` `verify` | a pass that fails `verify` or raises `TransformError` is rolled back |
| Bookkeeping | `changed` `print_changed` `enter_pass_scope` `pass_name` `createPass` `should_skip_tensor` `_noop` | `changed` drives the fixpoint loop |
| Debug / dump | `dump_files` `dump_function` `dump_git` `set_cur_debug_location` `is_debug_mode` `LogContext` | per-pass dumps gated on debug mode |
| Flags | `clOptBool` `clOptString` | the CLI-option accessors passes self-gate on |

### The runners

```c
// DotTransform.PassManager — the top-level runner
class PassManager:
    transformModule(module):     // run each spec's pass over the whole comp-unit
        for spec in self.pass_list:        // spec is a PassType (see next section)
            if not spec.isenabled(): continue
            pass = spec.construct(target)   // createPass — instantiate class with kwargs
            pass.run_or_rollback(module)    // mutate, or revert if verify() fails / raises
    transformFunction(fn): ...              // per-function variant
    verify(module): ...                     // run the registered verifiers
    dumpJson(out) / fromJson(in): ...       // the whole manager is JSON-serialisable

// DotTransform.IterativePassManager — run a pass set to FIXED POINT
class IterativePassManager:
    transformFunction(fn):
        do:
            changed = False
            for spec in self.pass_list:
                pass = spec.construct(target)
                pass.run_or_rollback(fn)
                changed |= pass.changed       // wraps IterativeTransform.iterate / iterate_fn
        while changed                          // re-run until nothing mutates

// DotTransform.SuperPass — a composite that bundles a sub-pipeline AS ONE pass
class SuperPass(DotTransform):                 // used to group a phase (dump/print_changed)
    ...
```

> **QUIRK — `run_or_rollback` makes passes transactional.** Unlike an LLVM pass (which is assumed correct and whose failure aborts the pipeline), a Penguin pass runs inside a try/verify wrapper: if the pass raises or its post-`verify` fails, the IR is reverted to the pre-pass state and the pipeline continues. This is why `optional` perf passes can be aggressive — a miscompiled tile is rolled back, not fatal. A reimplementation that drops the rollback will turn recoverable optional-pass failures into hard compile errors.

---

## The Spec Builder — `PassConstructor` and `PassType`

A driver does not call `manager.addPass(SomePass())`. It `yield`s a **specification** object and lets a builder fold it in. This indirection is what lets the whole pipeline be `OptLevel`-gated and JSON-serialised before any pass instance exists.

### `PassType` — a spec, not a pass

A `PassType` wraps a pass **class** plus its `kwargs`, a `required=`/`optional=` qualifier, and an optional `target` requirement. It is enable/disable-toggleable (`isenabled`, `enable`, `disable`, `is_required`). The class is instantiated only when the `PassManager` actually runs the spec.

### `PassConstructorBuilder` — fold via `+` / `+=`

`PassConstructorBuilder` (in `PassConstructor.cpython-310-….so`) defines the numeric `nb_add` and `nb_inplace_add` slots, so a driver appends a spec with Python operator syntax:

```c
// inside a CodeGenFlow generator (Cython generator body)
builder = PassConstructorBuilder(target=target)        // .__init__ takes target
builder += PassType(LowerTranspose,  required=True)    // nb_inplace_add — guarded:
builder += PassType(LateNeuronInstComb, optional=True) //   "Pass must be a PassType"
...
// build_pass_constructor() finalizes; build_pass_list() -> inner pass_constructor closure
pipeline = builder.build_pass_constructor(...)
```

Two guard strings police this: `"Pass must be a PassType"` (rejects a bare class) and `"PassConstructorBuilder does not have target but %s requires target"` (a `target=`-tagged spec yielded into an untargeted builder fails loudly). The constructed pipeline is round-trippable: `PassConstructor.dumpJson` / `.fromJson` (`inputJson`/`outputJson`) serialise the entire spec list — so a pipeline can be cached, diffed, or introspected without instantiating a single pass.

> **NOTE —** the `+=` fold is why the driver functions are *generators*: each `yield PassType(...)` is consumed by a `for spec in gen: builder += spec` loop in the calling `CodeGenFlow`. The generator's *source order* therefore is the pipeline order — and because Cython interns each pass name as a `__pyx_n_s_<Name>` global fetched through a version-counter, sorting those lookups recovers the literal in-source order even from a stripped generator body.

---

## The Driver — `SharedCodeGenFlow`'s four generators

`targets/sunda/SharedCodeGenFlow.cpython-310-….so` is unstripped (full IDA decompile available) and defines exactly four module-level generator functions, named by their qualified-name and `__pyx_scope_struct` symbols:

```text
peephole_optimizations               -> __pyx_scope_struct_1_peephole_optimizations
legalize_pe_insts                    -> __pyx_scope_struct__legalize_pe_insts
nki_codegen                          -> __pyx_scope_struct_2_nki_codegen
codegen_optimization_post_tritium_fusion
                                     -> __pyx_scope_struct_3_codegen_optimization_post_tritium_fusion
```

Both `targets/sunda/CodeGenFlow.so` and `targets/tonga/CodeGenFlow.so` `from …targets.sunda import SharedCodeGenFlow` and call these four. The Sunda driver exposes `optimize` / `codegen_prepare` / `codegen_optimization` and their `_eager` variants (define-by-run single-shot mode); Tonga exposes the three staged ones only (no `_eager`).

### `peephole_optimizations()` — local cleanup, all optional

```c
function peephole_optimizations(builder):   // gen1; all yields optional=True
    yield PassType(FactorizeBlkDims,     optional=True)
    yield PassType(NeuronInstComb,       optional=True)   // instruction-combine
    yield PassType(NeuronValueNumbering, optional=True)
    yield PassType(NeuronInstComb,       optional=True)   // re-run after VN
```

The `InstComb → VN → InstComb` sandwich is the classic GVN-then-recombine shape: value numbering exposes redundancies that a second instruction-combine pass can fold. The pass-name tokens `FactorizeBlkDims`, `NeuronInstComb` and `NeuronValueNumbering` all sit in `SharedCodeGenFlow.so`'s `__pyx_n_s_` table.

### `legalize_pe_insts()` — PE-engine instruction legalization

```c
function legalize_pe_insts(builder):        // gen
    yield PassType(LowerTranspose,    required=True)   // transpose -> PE ops
    yield PassType(LowerBroadcast,    required=True)   // broadcast -> PE ops
    yield PassType(LateNeuronInstComb, optional=True)
    yield PassType(SplitAccGrp,       optional=True)   // split accumulation groups
    yield PassType(SpillPSum,         optional=True)   // PSUM-bank spill
```

The two `required` lowerings make every transpose/broadcast expressible on the PE (tensor) engine; the optional tail does late cleanup and PSUM spilling.

### `nki_codegen()` — emit NKI text

```c
function nki_codegen(builder, nki_dl, nki_debug_mode):  // gen2
    yield PassType(NkiCodegenPass, required=True,
                   nki_dl=nki_dl,                 // debug-location kwarg
                   nki_debug_mode=nki_debug_mode) // debug-mode kwarg
```

A single required pass — `NkiCodegenPass` from `penguin/transforms/` — with the debug-location (`nki_dl`) and debug-mode (`nki_debug_mode`) flags forwarded as kwargs. Both kwarg names appear as the unicode tokens `__pyx_n_u_nki_dl` / `__pyx_n_u_nki_debug_mode`.

### `codegen_optimization_post_tritium_fusion()` — the main middle-end list

This is the canonical post-fusion optimization sequence, run after Tritium fusion and before the engine-legalize / NKI / BIR tail. Recovered by the version-counter name-trace (source order). The whole generator is itself gated by `Options.clOptBool('enable_post_tritium_fusion_passes')` (interned as `__pyx_n_s_enable_post_tritium_fusion_passes`).

```c
function codegen_optimization_post_tritium_fusion(target, nki_dl, nki_debug_mode):  // gen3
    builder = PassConstructorBuilder(target=target)
    builder += nki_codegen(nki_dl, nki_debug_mode)          // chains 3c in

    builder += PassType(NeuronLICM,           optional=True)   // loop-invariant code motion
    builder += PassType(InferPSumTensor,      optional=True)   // mark PSUM-resident tensors
    if clOptBool('enable-local-legalize-type'):                // type legalization, local|global
        builder += PassType(LocalLegalizeType, required=True)
    else:
        builder += PassType(LegalizeType,      required=True)
    builder += PassType(WeightCoalescing,     optional=True)
    builder += PassType(LegalizeSundaAccess,  required=True)   // target=Sunda access legalize
    builder += PassType(RelaxPredicates,      required=True)
    builder += PassType(TensorInitialization, required=True)
    builder += PassType(NeuronSimplifyPredicates, optional=True)
    builder += PassType(ExpandISAMacro,       required=True)   // from tonga.passes
    builder += PassType(SimplifyNeuronTensor, optional=True)
    builder += PassType(DMALocalityOpt,       optional=True)
    builder += PassType(DataStreaming,        optional=True)
    builder += PassType(FastSpillGeneration,  optional=True)
    builder += PassType(SFKVectorizer,        optional=True)   // scalar/SIMD-free-kernel vectorize
    builder += PassType(FastSpillGeneration,  optional=True)   // re-run spill after vectorize
    if clOptBool('annotate-no-spill-hint'):
        builder += PassType(AnnotateNoSpill,  optional=True)
    builder += PassType(/* sunda.<pass> */,   required=True)   // attr on the `sunda` module; name not pinned
    builder += PassType(CoalesceCCOp,         required=True)   // collective-op coalescing
    if not clOptBool('disable-tiling-allreduce'):
        builder += PassType(SimpleAllReduceTiling, required=True)
    builder += PassType(InsertCoreBarrier,    optional=True)
    builder += PassType(DMAProfiler,          optional=True)   // from tonga.passes
    return builder.build_pass_constructor(...)
```

Every pass-name token above is present in `SharedCodeGenFlow.so`'s interned-name table (`__pyx_n_s_NeuronLICM` … `__pyx_n_s_DMAProfiler`). The `required`/`optional` qualifier on each is the FINE gate of [the opt-level model](#the-gate--optlevel): `required` passes are correctness legalizations that always run; `optional` passes are the perf passes dropped at lower `-O`. The intra-list ordering comes from the version-counter trace (source order) rather than a direct read; the two `FastSpillGeneration` instances bracketing `SFKVectorizer` are the spill-vectorize-respill pattern, mirroring the InstComb sandwich in `peephole_optimizations`.

> **QUIRK — the list re-runs `nki_codegen` and `FastSpillGeneration`.** A pass appearing twice in the spec list is not a bug: `FastSpillGeneration` runs once before vectorization and again after (vectorization creates new live ranges that may need spilling), and the leading `nki_codegen` sub-flow is chained in so the post-fusion list can itself drive NKI emission on the NKI path. A `set`-based reimplementation that de-dups specs by class will silently drop the second spill and miscompile spill-heavy kernels.

---

## The Gate — `OptLevel`

`ir/OptLevel.cpython-310-….so` defines the optimization-level enum. It is `@unique` and `@total_ordering` with a custom `__gt__` (`__pyx_n_s_unique` / `__pyx_n_s_total_ordering` / `OptLevel.__gt__`). Docstring, verbatim: **"The optimization level of an penguin function."** (the *less-optimization-toward-the-end* ordering note is in the source comment).

This is an **inverted** enum: members later in the list run *fewer* optimizations. The member tokens, consumed by `NKICodeGenFlow.so`:

```text
default_level   — full optimization (the implicit top)
skip_middle_end
skip_loop_level_transformations
skip_allocators
prepare         — prepare-only (the bottom; least optimization)
                  (+ allow_none — sentinel for "no level set")
```

### Two-level gating

**Coarse (flow-level).** The `CodeGenFlow` / `NKICodeGenFlow` drivers branch on `if opt_level > OptLevel.skip_middle_end` / `… skip_loop_level_transformations` / `… skip_allocators` and skip whole phases. `OptLevel.__gt__` plus these three member tokens are consumed *only* by `NKICodeGenFlow.so` — opt-level decisions are made at the flow level, not buried per-pass. `skip_allocators` is also threaded as a kwarg through `NKICodeGenFlow`.

```c
// flow-level gating shape — exact thresholds INFERRED
function codegen_optimization(fn, opt_level):
    if opt_level > OptLevel.skip_middle_end:                  // run the middle-end at all?
        run TritiumFusion, fusion, simplify, …
    if opt_level > OptLevel.skip_loop_level_transformations:  // loop/tiling/layout phase?
        run LoopFusion, LayoutTilingPipeline, Vectorize*, ISel
        run SharedCodeGenFlow.codegen_optimization_post_tritium_fusion(...)
    if opt_level > OptLevel.skip_allocators:                  // register/buffer allocation?
        run StackAllocator | ColoringAllocator, AllocateBlocks
    run SharedCodeGenFlow.legalize_pe_insts(...)              // always (correctness)
    run SharedCodeGenFlow.peephole_optimizations(...)         // final cleanup
```

**Fine (per-pass).** Inside `codegen_optimization_post_tritium_fusion`, each spec's `required=`/`optional=` qualifier is the per-pass gate, plus the `Options.clOptBool('<flag>')` toggles. The flag strings are `enable-local-legalize-type`, `annotate-no-spill-hint`, `disable-tiling-allreduce`, `enable-tritium-loopfusion`, and the generator-level `enable_post_tritium_fusion_passes`.

> **GOTCHA — the level→pass cutoff table is not statically recoverable.** The `>` comparisons live in `CodeGenFlow.so` (no IDA decompile for that module in this build). The *staged-skip taxonomy* above is firm — member names, `__gt__`, and the consuming flow are all directly readable — but the exact numeric `OptLevel` value each `-O` maps to, and which optional passes survive at each level, is **INFERRED**. The driver-CLI `--optlevel "0".."3"` (default `"2"`, [3.10](../frontend/opt-level-planes.md)) maps to these enum members upstream; that map is not in this binary's recoverable text. Do not invent a cutoff table — gate on the *named stage* boundary, not a guessed integer.

---

## Where the Autotuner Hooks In

The L-strand autotuner is **not** a pipeline stage. It is embedded in one middle-end pass: `targets/transforms/TritiumFusion.cpython-310-….so` with 145 `autotun*` string hits, the only pass binary with heavy autotune integration. Mechanism:

- `AutotuneFusionPlanGenerator` enumerates candidate matmul fusion plans: `build`, `best_fusion_plan`, `best_fusion_plan_search`, `generate_fusion_plans_for_search_more_trip`, `force_tripcount`, `get_axes_info`.
- Plans are submitted via `add_plans` to `Autotuner` (`autotune/Autotuner.so`, docstring verbatim: *"Autotuner -- Automatically benchmark and tune decisions within penguin during compilation."*), which benchmark-drives through `_Compiler` / `_Search` / `_TreeSearch` / `_PerformanceMetric` and returns the winner via `apply_plan`.
- Gating: `_autotune_enabled` / `_autotune_config`; when disabled, a `_BypassedAutotuner` / `add_no_change_plan` short-circuits to a deterministic heuristic.

So at default opt the fusion decision is a deterministic heuristic; with `-autotune` it becomes a benchmarked search over a *re-run* of the same middle-end (each plan = a re-tiling). The autotuner sits **inside** `codegen_optimization`, at the `TritiumFusion` pass, *before* ISel — downstream passes are deterministic given the chosen plan.

---

## Placement — entry and exit

```text
HLO ──hlo2penguin (C-strand)──▶ Penguin Module / Function (tensoriser-IR, op level)
   │  Driver/Job layer (collectBackendPipeline) calls per-target CodeGenFlow as a backend Job
   ▼
CodeGenFlow.optimize():
   codegen_prepare       (canonicalize, infer, alias setup, DCE/DSE, simplify, dtype)
   codegen_optimization  (TritiumFusion[+autotune] → fusion → loop/tiling/layout →
                          vectorize → collective lower → ISel  ◀── op→ISAInst boundary
                          → SharedCodeGenFlow.codegen_optimization_post_tritium_fusion (§ driver)
                          → legalize_pe_insts → late lower → alloc → peephole → nki_codegen)
   ▼  IR now at <Target>ISAInst level (same Python object, mutated in place)
BirCodeGenLoop (P-strand) — a DotTransform subclass — walks it, emits BIR   [5.18]
   (NKI path: SharedCodeGenFlow.nki_codegen / targets/codegen/NkiCodegen emits NKI text instead)
```

There is **no serialization between passes** — it is one live Python object graph, mutated in place, with `changed` tracking and `run_or_rollback` reverting failed passes. ISel (`SundaISel` / `TongaISel`) is the IR-form boundary: op-level tensoriser-IR before, `<Target>ISAInst`-level after. The hand-off to `BirCodeGenLoop` is just-another-`DotTransform`-walk that happens to emit BIR rather than mutate.

---

## Evidence Summary

The five strongest claims and their anchors:

1. **Pass counts: transforms/ = 71, targets/transforms/ = 131, sunda = 6, tonga = 40.** Directory enumeration, exact. The 131 excludes the `autotune/` subdir (6 modules) and `__init__`. **CERTAIN** as a module count; the pass-*class* subset is a few smaller because ~8–10 modules are helper libraries, flagged inline.
2. **The four `SharedCodeGenFlow` driver generators exist and are generators.** `__pyx_scope_struct_{1,2,3,_}` for `peephole_optimizations`/`nki_codegen`/`codegen_optimization_post_tritium_fusion`/`legalize_pe_insts`, plus `__pyx_mdef_…` method-defs and the banner `"Driver functions for Sunda CodeGen"`. **CERTAIN.**
3. **`codegen_optimization_post_tritium_fusion` runs the §-driver list in that order.** Every pass-name token (`NeuronLICM`…`DMAProfiler`) is in the interned `__pyx_n_s_` table, and the qualifiers with them. The *order* comes from the version-counter name-trace on the unstripped decompile. **Membership CERTAIN, order HIGH.**
4. **`OptLevel` is an inverted staged-skip enum with members `prepare`/`skip_middle_end`/`skip_loop_level_transformations`/`skip_allocators`/`default_level`/`allow_none`, `@unique @total_ordering`, custom `__gt__`.** All tokens and the docstring are in `ir/OptLevel.so`, and the flow-level vs per-pass split follows from the members being consumed only by `NKICodeGenFlow`. The exact `-O`→member→pass cutoff table is **INFERRED** — the `>` comparisons live in `CodeGenFlow.so`, which has no decompile here. **Enum CERTAIN, thresholds INFERRED.**
5. **`PassConstructorBuilder` folds `PassType` specs via `nb_add`/`nb_inplace_add`; `DotTransform`/`PassManager`/`IterativePassManager` run them with `run_or_rollback`.** `nm -D` shows `nb_add`/`nb_inplace_add`; the guard strings `"Pass must be a PassType"` and `"PassConstructorBuilder does not have target but %s requires target"`; `PassManager.transformModule/transformFunction/verify/dumpJson/fromJson` + `IterativePassManager.transformFunction` + `run_or_rollback`/`always_rollback`/`verify` are all present in `DotTransform.so`. **CERTAIN.**

Remaining gaps: the `codegen_prepare`/`codegen_optimization` *intra-phase ordering* in [§ placement](#placement--entry-and-exit) rests on the pass set plus the SharedCodeGen internal order rather than a direct read; the `sunda.<pass>` spec at position 18 of the driver list resolves to an attribute on the `sunda` module whose name is not pinned; and the `Job → CodeGenFlow` call site is **INFERRED** from [the pipeline page](../front/pipeline.md), since that driver is compiled-opaque.

---

## Related Components

| Component | Relationship |
|---|---|
| `DotTransform` | Shared pass-visitor base for every Penguin pass *and* `BirCodeGenLoop` |
| `PassConstructor` | Spec builder — turns driver `yield`s into a runnable, serialisable pipeline |
| `OptLevel` | Staged-skip enum gating whole phases at the flow level |
| `TritiumFusion` | The autotuner host; the fusion stage the post-tritium list runs after |
| `BirCodeGenLoop` | The exit driver — a `DotTransform` subclass that emits BIR |

## Cross-References

- [Opt Levels](../frontend/opt-level-planes.md) — the driver-CLI `--optlevel "0".."3"` that maps to `OptLevel` enum members
- [Penguin IR Node Model](ir-node-model.md) — the Module/Function/Block/Stmt graph the `DotTransform` walk traverses
- [Penguin AffineExpr Algebra](affine-expr-algebra.md) — owns the Isl/IntegerSet machinery the delinearization passes use
- [Tensor / Buffer Node](tensor-buffer-node.md) — the PSUM/SBUF placement the alloc/spill passes target
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where the backend Job dispatches into `CodeGenFlow`
- Per-pass deep dives this page indexes (Part 5.x siblings): [Scheduling — MinReg](scheduling-minreg.md), [Software Pipelining](software-pipelining.md), [Data-Movement Fusion](data-movement-fusion.md), [Layout-Tiling Pipeline](layout-tiling-pipeline.md), [Loop-Transform Clients](loop-transform-clients.md)
