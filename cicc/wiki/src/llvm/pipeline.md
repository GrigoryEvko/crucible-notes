# Pipeline & Pass Ordering

> **Mixed provenance.** The PassBuilder, pipeline parser, and `StringMap`-driven registry are stock LLVM 20.0.0 infrastructure; the **35 NVIDIA custom pass classes** (`nvvm-reflect`, `nvvm-intrinsic-lowering`, `process-restrict`, `lower-struct-args`, `lower-aggr-copies`, `nv-memory-space-opt`, the seven `nvopt<...>` pipeline shorthands, etc.) and the per-tier driver (`sub_12E54A0`) are NVIDIA additions injected via the standard extension-callback table at `[PassBuilder+2208]`. The **33 entries** counted later in this page are `StringMap` *registration rows* in `sub_2342890` — two NVIDIA classes register under multiple keys (parameterized variants), so the row count is lower than the class count. See [NVIDIA Custom Passes Overview](../passes/index.md) for the canonical definition and full inventory.
>
> **Upstream source:** `llvm/lib/Passes/PassBuilder.cpp`, `PassRegistry.def`, `PassBuilderPipelines.cpp`, `llvm/include/llvm/Passes/PassBuilder.h` (LLVM 20.0.0). NVPTX target-specific pipeline hooks: `llvm/lib/Target/NVPTX/NVPTXTargetMachine.cpp::registerPassBuilderCallbacks`.

CICC v13.0 implements the LLVM New Pass Manager pipeline infrastructure, with NVIDIA injecting 35 custom pass classes (33 distinct `StringMap` registration rows, with two classes registered under multiple keys) into the registration table alongside approximately 493 standard LLVM passes. The master registration function at `sub_2342890` populates a `StringMap<PassInfo>` hash table with every known pass name at startup, and a text-based pipeline parser allows the full pass ordering to be specified as a parenthesized string (e.g., `module(function(instcombine,dse))`). This page documents the complete pass inventory, the registration mechanism, the NVIDIA-specific additions, and — critically — the **runtime pass execution order** for each optimization level including the tier system and pass factory addresses. The 35-class canonical count and its definition live in [NVIDIA Custom Passes](../passes/index.md#canonical-count).

| | |
|---|---|
| **Master registration** | `sub_2342890` (`0x2342890`, ~2,816 lines) |
| **Hash table insert** | `sub_E41FB0` (`0xE41FB0`) — open-addressing, 48-byte entries |
| **String equality** | `sub_9691B0` (`0x9691B0`) — `len==len && memcmp==0` |
| **AA name resolver** | `sub_233BD40` (`0x233BD40`) — chain of string comparisons |
| **AA pipeline parser** | `sub_233C0C0` (`0x233C0C0`) — splits on `,`, special-cases `"default"` |
| **Extension callback** | `sub_233C300` (`0x233C300`) — iterates `[PassBuilder+2208]`, stride 32 |
| **Option parser** | `sub_233A120` (`0x233A120`) — splits on `;`, validates tokens |
| **Help/listing** | `sub_233C410` (`0x233C410`) — `--print-pipeline-passes` handler |
| **Pipeline assembler** | `sub_12E54A0` (`0x12E54A0`, 49.8KB, 1,553 lines) |
| **AddPass** | `sub_12DE0B0` (`0x12DE0B0`, hash-based pass insertion) |
| **Tier 0 sub-pipeline** | `sub_12DE330` (`0x12DE330`, ~40 passes) |
| **Tier 1/2/3 sub-pipeline** | `sub_12DE8F0` (`0x12DE8F0`, phase-conditional) |
| **Codegen dispatch** | `sub_12DFE00` (`0x12DFE00`, 20.7KB) |
| **Total passes** | ~526 unique registrations |
| **NVIDIA additions** | 35 pass classes (33 `StringMap` rows: 12 module, 20 function, 1 loop) + 2 analyses |

## Registration Architecture

The pipeline infrastructure follows the standard LLVM New Pass Manager design. At startup, `sub_2342890` is called once and inserts every known pass into a `StringMap` living at `[PassBuilder+8]`. The insertion function `sub_E41FB0` uses open-addressing with linear probing; each entry occupies 48 bytes containing the key pointer, key length, value pointer, value length, and 16 bytes of inline storage for short class names.

Pass lookup during pipeline parsing uses the hash function at `sub_C94890` (likely DJB/FNV-family). Parameterized passes are detected by the presence of `<...>` angle brackets after the pass name; the parameter string is extracted and forwarded to a pass-specific callback. The generic parameter validator `sub_233A120` splits option strings on semicolons and compares each token to expected values, emitting `"invalid {PassName} pass parameter '{token}'"` on mismatch.

The alias analysis pipeline has its own parser at `sub_233C0C0`. It special-cases the string `"default"` (which calls `sub_23A1380` then `sub_23038C0` to build the default AA stack), and otherwise splits on commas, resolving each name through `sub_233BD40`:

| AA Name | Constructor |
|---|---|
| `globals-aa` | `sub_2396EC0` |
| `basic-aa` | `sub_2361CE0` |
| `objc-arc-aa` | `sub_2361F60` |
| `scev-aa` | `sub_2362040` |
| `scoped-noalias-aa` | `sub_2362120` |
| `tbaa` | `sub_2362200` |

Extension callbacks for target-specific pipeline customization are stored at `[PassBuilder+2208]` with a count at `[PassBuilder+2216]`. Each entry is 32 bytes with a guard at offset +16 (must be non-null) and the callback function pointer at offset +24. The string `"all"` in extension context triggers `invalidate<all>`.

## Pipeline Text Parser

The pipeline text parser accepts a nesting grammar where each level specifies the pass manager scope:

```text
module(
  function(
    instcombine<max-iterations=1>,
    dse,
    loop(indvars, loop-deletion)
  ),
  globalopt
)
```

The parser splits on commas and parentheses, recognizing `module(...)`, `cgscc(...)`, `function(...)`, and `loop(...)` as scope wrappers. Bare names are looked up in the `StringMap` built by `sub_2342890`. For parameterized passes, the `<...>` suffix is extracted and dispatched to per-pass option parsers. Several NVIDIA-specific parameter parsers are thin wrappers around `sub_233A120`:

| Parser | Pass | Recognized Options |
|---|---|---|
| `sub_233A330` | `process-restrict` | `propagate-only` |
| `sub_233A370` | `lower-struct-args` | `opt-byval` |
| `sub_233A3B0` | `lower-aggr-copies` | `lower-aggr-func-args` |

More complex passes (GVN, SimplifyCFG, InstCombine) use chained `sub_9691B0` string comparisons for multi-option parsing.

The pipeline name strings recognized by the `nvopt<>` dispatch table are:

| Pipeline Name | CLI Source | Pass Count |
|---|---|---|
| `nvopt<O0>` | (no -O flag, no -Ofc) | ~5–8 |
| `nvopt<O1>` | `-O1` | ~35 |
| `nvopt<O2>` | `-O2` | ~35+ |
| `nvopt<O3>` | `-O3` | ~35+ |
| `nvopt<Ofcmax>` | `-Ofast-compile=max` / `-Ofc=max` | ~12–15 |
| `nvopt<Ofcmid>` | `-Ofast-compile=mid` / `-Ofc=mid` | ~25–30 |
| `nvopt<Ofcmin>` | `-Ofast-compile=min` / `-Ofc=min` | ~30–35 |

Key addresses for pipeline name dispatch: `sub_226C400` selects the pipeline name string, which is passed to `sub_2277440` (pipeline text parser). The `nvopt` prefix is registered in `sub_225D540` (new PM) and `sub_12C35D0` (legacy PM), both calling into a pipeline builder class at vtable `unk_4A08350`.

**Mutual exclusion**: combining `-O#` with `--passes=` is an error: `"Cannot specify -O#/-Ofast-compile=<min,mid,max> and --passes=/--foo-pass, use -passes='default<O#>,other-pass' or -passes='default<Ofcmax>,other-pass'"`.

## Complete Pass Inventory

The following tables list every pass in exact registration order within `sub_2342890`. NVIDIA-specific passes are marked with bold names. Registration line numbers are from the decompiled output.

### Module Analyses (18)

| # | Pass Name | LLVM Class | Reg. Line |
|---|---|---|---|
| 1 | `callgraph` | `CallGraphAnalysis` | 514 |
| 2 | `collector-metadata` | `CollectorMetadataAnalysis` | — |
| 3 | `ctx-prof-analysis` | `CtxProfAnalysis` | — |
| 4 | `dxil-metadata` | `DXILMetadataAnalysis` | — |
| 5 | `dxil-resource-binding` | `DXILResourceBindingAnalysis` | — |
| 6 | `dxil-resource-type` | `DXILResourceTypeAnalysis` | — |
| 7 | `inline-advisor` | `InlineAdvisorAnalysis` | — |
| 8 | `ir-similarity` | `IRSimilarityAnalysis` | — |
| 9 | `last-run-tracking` | via `sub_2342820` | — |
| 10 | `lcg` | `LazyCallGraphAnalysis` | — |
| 11 | `module-summary` | `ModuleSummaryIndexAnalysis` | — |
| 12 | `no-op-module` | `NoOpModuleAnalysis` | — |
| 13 | `pass-instrumentation` | via `sub_2342830` | — |
| 14 | `profile-summary` | `ProfileSummaryAnalysis` | — |
| 15 | `reg-usage` | `PhysicalRegisterUsageAnalysis` | — |
| 16 | `stack-safety` | `StackSafetyGlobalAnalysis` | — |
| 17 | `verify` | via `sub_2342840` | 596 |
| 18 | `globals-aa` | `GlobalsAA` | — |

### Module Passes (131)

Registration lines 599–1153 in `sub_2342890`. The first 121 entries are standard LLVM; the final 12 are NVIDIA custom passes registered at lines 1096–1153.

#### Standard LLVM Module Passes (entries 19–131)

| # | Pass Name | LLVM Class |
|---|---|---|
| 19 | `always-inline` | `AlwaysInlinerPass` |
| 20 | `annotation2metadata` | `Annotation2MetadataPass` |
| 21 | `assign-guid` | `AssignGUIDPass` |
| 22 | `attributor` | `AttributorPass` |
| 23 | `attributor-light` | `AttributorLightPass` |
| 24 | `called-value-propagation` | `CalledValuePropagationPass` |
| 25 | `canonicalize-aliases` | `CanonicalizeAliasesPass` |
| 26 | `check-debugify` | `NewPMCheckDebugifyPass` |
| 27 | `constmerge` | `ConstantMergePass` |
| 28 | `coro-cleanup` | `CoroCleanupPass` |
| 29 | `coro-early` | `CoroEarlyPass` |
| 30 | `cross-dso-cfi` | `CrossDSOCFIPass` |
| 31 | `ctx-instr-gen` | `PGOInstrumentationGen` |
| 32 | `ctx-prof-flatten` | `PGOCtxProfFlatteningPass` |
| 33 | `noinline-nonprevailing` | `NoinlineNonPrevailing` |
| 34 | `deadargelim` | `DeadArgumentEliminationPass` |
| 35 | `debugify` | `NewPMDebugifyPass` |
| 36 | `dfsan` | `DataFlowSanitizerPass` |
| 37 | `dot-callgraph` | `CallGraphDOTPrinterPass` |
| 38 | `dxil-upgrade` | `DXILUpgradePass` |
| 39 | `elim-avail-extern` | `EliminateAvailableExternallyPass` |
| 40 | `extract-blocks` | `BlockExtractorPass` |
| 41 | `expand-variadics` | `ExpandVariadicsPass` |
| 42 | `forceattrs` | `ForceFunctionAttrsPass` |
| 43 | `function-import` | `FunctionImportPass` |
| 44 | `global-merge-func` | `GlobalMergeFuncPass` |
| 45 | `globalopt` | `GlobalOptPass` |
| 46 | `globalsplit` | `GlobalSplitPass` |
| 47 | `hotcoldsplit` | `HotColdSplittingPass` |
| 48 | `inferattrs` | `InferFunctionAttrsPass` |
| 49 | `inliner-ml-advisor-release` | via `sub_2342850` (InlinerWrapper) |
| 50 | `inliner-wrapper` | via `sub_2342850` (InlinerWrapper) |
| 51 | `inliner-wrapper-no-mandatory-first` | via `sub_2342850` |
| 52 | `insert-gcov-profiling` | `GCOVProfilerPass` |
| 53 | `instrorderfile` | `InstrOrderFilePass` |
| 54 | `instrprof` | `InstrProfilingLoweringPass` |
| 55 | `ctx-instr-lower` | `PGOCtxProfLoweringPass` |
| 56 | `print<ctx-prof-analysis>` | `CtxProfAnalysisPrinterPass` |
| 57 | `invalidate<all>` | via `sub_2342860` |
| 58 | `iroutliner` | `IROutlinerPass` |
| 59 | `jmc-instrumenter` | `JMCInstrumenterPass` |
| 60 | `lower-emutls` | `LowerEmuTLSPass` |
| 61 | `lower-global-dtors` | `LowerGlobalDtorsPass` |
| 62 | `lower-ifunc` | `LowerIFuncPass` |
| 63 | `lowertypetests` | `LowerTypeTestsPass` |
| 64 | `fatlto-cleanup` | `FatLtoCleanup` |
| 65 | `pgo-force-function-attrs` | `PGOForceFunctionAttrsPass` |
| 66 | `memprof-context-disambiguation` | `MemProfContextDisambiguation` |
| 67 | `memprof-module` | `ModuleMemProfilerPass` |
| 68 | `mergefunc` | `MergeFunctionsPass` |
| 69 | `metarenamer` | `MetaRenamerPass` |
| 70 | `module-inline` | `ModuleInlinerPass` |
| 71 | `name-anon-globals` | `NameAnonGlobalPass` |
| 72 | `no-op-module` | `NoOpModulePass` |
| 73 | `nsan` | `NumericalStabilitySanitizerPass` |
| 74 | `objc-arc-apelim` | `ObjCARCAPElimPass` |
| 75 | `openmp-opt` | `OpenMPOptPass` |
| 76 | `openmp-opt-postlink` | `OpenMPOptPass` |
| 77 | `partial-inliner` | `PartialInlinerPass` |
| 78 | `pgo-icall-prom` | `PGOIndirectCallPromotion` |
| 79 | `pgo-instr-gen` | `PGOInstrumentationGen` |
| 80 | `pgo-instr-use` | `PGOInstrumentationUse` |
| 81 | `pre-isel-intrinsic-lowering` | `PreISelIntrinsicLoweringPass` |
| 82 | `print` | `PrintModulePass` |
| 83 | `print-callgraph` | `CallGraphPrinterPass` |
| 84 | `print-callgraph-sccs` | `CallGraphSCCsPrinterPass` |
| 85 | `print-ir-similarity` | `IRSimilarityAnalysisPrinterPass` |
| 86 | `print-lcg` | `LazyCallGraphPrinterPass` |
| 87 | `print-lcg-dot` | `LazyCallGraphDOTPrinterPass` |
| 88 | `print-must-be-executed-contexts` | `MustBeExecutedContextPrinterPass` |
| 89 | `print-profile-summary` | `ProfileSummaryPrinterPass` |
| 90 | `print-stack-safety` | `StackSafetyGlobalPrinterPass` |
| 91 | `print<dxil-metadata>` | `DXILMetadataAnalysisPrinterPass` |
| 92 | `print<dxil-resource-binding>` | `DXILResourceBindingPrinterPass` |
| 93 | `print<inline-advisor>` | `InlineAdvisorAnalysisPrinterPass` |
| 94 | `print<module-debuginfo>` | `ModuleDebugInfoPrinterPass` |
| 95 | `print<reg-usage>` | `PhysicalRegisterUsageInfoPrinterPass` |
| 96 | `pseudo-probe` | `SampleProfileProbePass` |
| 97 | `pseudo-probe-update` | `PseudoProbeUpdatePass` |
| 98 | `recompute-globalsaa` | `RecomputeGlobalsAAPass` |
| 99 | `rel-lookup-table-converter` | `RelLookupTableConverterPass` |
| 100 | `rewrite-statepoints-for-gc` | `RewriteStatepointsForGC` |
| 101 | `rewrite-symbols` | `RewriteSymbolPass` |
| 102 | `rpo-function-attrs` | `ReversePostOrderFunctionAttrsPass` |
| 103 | `rtsan` | `RealtimeSanitizerPass` |
| 104 | `sample-profile` | `SampleProfileLoaderPass` |
| 105 | `sancov-module` | `SanitizerCoveragePass` |
| 106 | `sanmd-module` | `SanitizerBinaryMetadataPass` |
| 107 | `scc-oz-module-inliner` | via `sub_2342850` (InlinerWrapper) |
| 108 | `shadow-stack-gc-lowering` | `ShadowStackGCLoweringPass` |
| 109 | `strip` | `StripSymbolsPass` |
| 110 | `strip-dead-debug-info` | `StripDeadDebugInfoPass` |
| 111 | `strip-dead-prototypes` | `StripDeadPrototypesPass` |
| 112 | `strip-debug-declare` | `StripDebugDeclarePass` |
| 113 | `strip-nondebug` | `StripNonDebugSymbolsPass` |
| 114 | `strip-nonlinetable-debuginfo` | `StripNonLineTableDebugInfoPass` |
| 115 | `trigger-crash-module` | `TriggerCrashModulePass` |
| 116 | `trigger-verifier-error` | `TriggerVerifierErrorPass` |
| 117 | `tsan-module` | `ModuleThreadSanitizerPass` |
| 118 | `tysan` | `TypeSanitizerPass` |
| 119 | `verify` | via `sub_2342870` |
| 120 | `view-callgraph` | `CallGraphViewerPass` |
| 121 | `wholeprogramdevirt` | `WholeProgramDevirtPass` |

#### NVIDIA Module Passes (entries 122–131)

| # | Pass Name | LLVM Class | Reg. Line | Purpose |
|---|---|---|---|---|
| 122 | **`check-gep-index`** | `CheckGepIndexPass` | 1096 | Validates GEP index bounds |
| 123 | **`check-kernel-functions`** | `NVPTXSetFunctionLinkagesPass` | 1101 | Enforces kernel linkage |
| 124 | **`cnp-launch-check`** | `CNPLaunchCheckPass` | 1106 | Cooperative launch validation |
| 125 | **`ipmsp`** | `IPMSPPass` | 1111 | Inter-procedural memory space propagation |
| 126 | **`nv-early-inliner`** | via `sub_2342850` | 1114 | NVIDIA early inlining heuristic |
| 127 | **`nv-inline-must`** | `InlineMustPass` | 1119 | Force-inlines `__forceinline__` functions |
| 128 | **`nvvm-pretreat`** | `PretreatPass` | 1124 | IR canonicalization before optimization |
| 129 | **`nvvm-verify`** | `NVVMIRVerifierPass` | 1129 | NVVM IR constraint validation |
| 130 | **`printf-lowering`** | `PrintfLoweringPass` | 1134 | Lowers printf to vprintf ABI |
| 131 | **`select-kernels`** | `SelectKernelsPass` | 1139 | Selects kernels for compilation |

#### Parameterized Module Passes (entries 132–145)

| # | Pass Name | Class | Parameters |
|---|---|---|---|
| 132 | `asan` | `AddressSanitizerPass` | `kernel` |
| 133 | `cg-profile` | `CGProfilePass` | `in-lto-post-link` |
| 134 | `global-merge` | `GlobalMergePass` | `group-by-use;ignore-single-use;max-offset=N` |
| 135 | `embed-bitcode` | `EmbedBitcodePass` | `thinlto;emit-summary` |
| 136 | `globaldce` | `GlobalDCEPass` | `in-lto-post-link` |
| 137 | `hwasan` | `HWAddressSanitizerPass` | `kernel;recover` |
| 138 | `internalize` | `InternalizePass` | `preserve-gv=GV` |
| 139 | `ipsccp` | `IPSCCPPass` | `no-func-spec;func-spec` |
| 140 | `loop-extract` | `LoopExtractorPass` | `single` |
| 141 | `memprof-use` | `MemProfUsePass` | `profile-filename=S` |
| 142 | `msan` | `MemorySanitizerPass` | `recover;kernel;eager-checks;track-origins=N` |
| 143 | `print<structural-hash>` | `StructuralHashPrinterPass` | `detailed;call-target-ignored` |
| 144 | **`lower-ops`** | `LowerOpsPass` | `enable-optimization` |
| 145 | **`set-global-array-alignment`** | `SetGlobalArrayAlignmentPass` | `modify-shared-mem;skip-shared-mem;modify-global-mem;skip-global-mem` |

### CGSCC Analyses and Passes (entries 146–158)

| # | Pass Name | LLVM Class | Level |
|---|---|---|---|
| 146 | `no-op-cgscc` | `NoOpCGSCCAnalysis` | Analysis |
| 147 | `fam-proxy` | `FunctionAnalysisManagerCGSCCProxy` | Analysis |
| 148 | `pass-instrumentation` | via `sub_2342830` | Analysis |
| 149 | `argpromotion` | `ArgumentPromotionPass` | Pass |
| 150 | `attributor-cgscc` | `AttributorCGSCCPass` | Pass |
| 151 | `attributor-light-cgscc` | `AttributorLightCGSCCPass` | Pass |
| 152 | `invalidate<all>` | via `sub_2342860` | Pass |
| 153 | `no-op-cgscc` | `NoOpCGSCCPass` | Pass |
| 154 | `openmp-opt-cgscc` | `OpenMPOptCGSCCPass` | Pass |
| 155 | `coro-annotation-elide` | `CoroAnnotationElidePass` | Pass |
| 156 | `coro-split` | `CoroSplitPass` | Param: `reuse-storage` |
| 157 | `function-attrs` | `PostOrderFunctionAttrsPass` | Param: `skip-non-recursive-function-attrs` |
| 158 | `inline` | `InlinerPass` | Param: `only-mandatory` |

### Function Analyses (entries 159–201)

Registration lines 1208–1415 in `sub_2342890`.

| # | Pass Name | LLVM Class |
|---|---|---|
| 159 | `aa` | `AAManager` |
| 160 | `access-info` | `LoopAccessAnalysis` |
| 161 | `assumptions` | `AssumptionAnalysis` |
| 162 | `bb-sections-profile-reader` | `BasicBlockSectionsProfileReaderAnalysis` |
| 163 | `block-freq` | `BlockFrequencyAnalysis` |
| 164 | `branch-prob` | `BranchProbabilityAnalysis` |
| 165 | `cycles` | `CycleAnalysis` |
| 166 | `da` | `DependenceAnalysis` |
| 167 | `debug-ata` | `DebugAssignmentTrackingAnalysis` |
| 168 | `demanded-bits` | `DemandedBitsAnalysis` |
| 169 | `domfrontier` | `DominanceFrontierAnalysis` |
| 170 | `domtree` | `DominatorTreeAnalysis` |
| 171 | `func-properties` | `FunctionPropertiesAnalysis` |
| 172 | `machine-function-info` | `MachineFunctionAnalysis` |
| 173 | `gc-function` | `GCFunctionAnalysis` |
| 174 | `inliner-size-estimator` | `InlineSizeEstimatorAnalysis` |
| 175 | `last-run-tracking` | via `sub_2342820` |
| 176 | `lazy-value-info` | `LazyValueAnalysis` |
| 177 | `loops` | `LoopAnalysis` |
| 178 | `memdep` | `MemoryDependenceAnalysis` |
| 179 | `memoryssa` | `MemorySSAAnalysis` |
| 180 | `no-op-function` | `NoOpFunctionAnalysis` |
| 181 | `opt-remark-emit` | `OptimizationRemarkEmitterAnalysis` |
| 182 | `pass-instrumentation` | via `sub_2342830` |
| 183 | `phi-values` | `PhiValuesAnalysis` |
| 184 | `postdomtree` | `PostDominatorTreeAnalysis` |
| 185 | `regions` | `RegionInfoAnalysis` |
| 186 | `scalar-evolution` | `ScalarEvolutionAnalysis` |
| 187 | `should-not-run-function-passes` | `ShouldNotRunFunctionPassesAnalysis` |
| 188 | `should-run-extra-vector-passes` | `ShouldRunExtraVectorPasses` |
| 189 | `ssp-layout` | `SSPLayoutAnalysis` |
| 190 | `stack-safety-local` | `StackSafetyAnalysis` |
| 191 | `target-ir` | `TargetIRAnalysis` |
| 192 | `target-lib-info` | `TargetLibraryAnalysis` |
| 193 | `uniformity` | `UniformityInfoAnalysis` |
| 194 | `verify` | via `sub_2342840` |
| 195 | **`rpa`** | `RegisterPressureAnalysis` |
| 196 | **`merge-sets`** | `MergeSetsAnalysis` |

#### Function AA Analyses (entries 197–201)

| # | Pass Name | LLVM Class |
|---|---|---|
| 197 | `basic-aa` | `BasicAA` |
| 198 | `objc-arc-aa` | `objcarc::ObjCARCAA` |
| 199 | `scev-aa` | `SCEVAA` |
| 200 | `scoped-noalias-aa` | `ScopedNoAliasAA` |
| 201 | `tbaa` | `TypeBasedAA` |

### Function Passes (entries 202–419)

Registration lines 1420–2319 in `sub_2342890`. The first 173 entries (202–374) are standard LLVM; entries 376–392 are NVIDIA-specific; entries 393–419 are parameterized passes (both standard and NVIDIA).

#### Standard LLVM Function Passes (entries 202–375)

| # | Pass Name | LLVM Class |
|---|---|---|
| 202 | `aa-eval` | `AAEvaluator` |
| 203 | `adce` | `ADCEPass` |
| 204 | `add-discriminators` | `AddDiscriminatorsPass` |
| 205 | `aggressive-instcombine` | `AggressiveInstCombinePass` |
| 206 | `alignment-from-assumptions` | `AlignmentFromAssumptionsPass` |
| 207 | `annotation-remarks` | `AnnotationRemarksPass` |
| 208 | `assume-builder` | `AssumeBuilderPass` |
| 209 | `assume-simplify` | `AssumeSimplifyPass` |
| 210 | `atomic-expand` | `AtomicExpandPass` |
| 211 | `bdce` | `BDCEPass` |
| 212 | `break-crit-edges` | `BreakCriticalEdgesPass` |
| 213 | `callbr-prepare` | `CallBrPreparePass` |
| 214 | `callsite-splitting` | `CallSiteSplittingPass` |
| 215 | `chr` | `ControlHeightReductionPass` |
| 216 | `codegenprepare` | `CodeGenPreparePass` |
| 217 | `complex-deinterleaving` | `ComplexDeinterleavingPass` |
| 218 | `consthoist` | `ConstantHoistingPass` |
| 219 | `constraint-elimination` | `ConstraintEliminationPass` |
| 220 | `coro-elide` | `CoroElidePass` |
| 221 | `correlated-propagation` | `CorrelatedValuePropagationPass` |
| 222 | `count-visits` | `CountVisitsPass` |
| 223 | `dce` | `DCEPass` |
| 224 | `declare-to-assign` | `AssignmentTrackingPass` |
| 225 | `dfa-jump-threading` | `DFAJumpThreadingPass` |
| 226 | `div-rem-pairs` | `DivRemPairsPass` |
| 227 | `dot-cfg` | `CFGPrinterPass` |
| 228 | `dot-cfg-only` | `CFGOnlyPrinterPass` |
| 229 | `dot-dom` | `DOTGraphTraitsPrinter<DominatorTree, false>` |
| 230 | `dot-dom-only` | `DOTGraphTraitsPrinter<DominatorTree, true>` |
| 231 | `dot-post-dom` | `DOTGraphTraitsPrinter<PostDominatorTree, false>` |
| 232 | `dot-post-dom-only` | `DOTGraphTraitsPrinter<PostDominatorTree, true>` |
| 233 | `dse` | `DSEPass` |
| 234 | `dwarf-eh-prepare` | `DwarfEHPreparePass` |
| 235 | `expand-large-div-rem` | `ExpandLargeDivRemPass` |
| 236 | `expand-large-fp-convert` | `ExpandLargeFpConvertPass` |
| 237 | `expand-memcmp` | `ExpandMemCmpPass` |
| 238 | `extra-vector-passes` | `ExtraFunctionPassManager<ShouldRunExtraVectorPasses>` |
| 239 | `fix-irreducible` | `FixIrreduciblePass` |
| 240 | `flatten-cfg` | `FlattenCFGPass` |
| 241 | `float2int` | `Float2IntPass` |
| 242 | `gc-lowering` | `GCLoweringPass` |
| 243 | `guard-widening` | via `sub_2342880` |
| 244 | `gvn-hoist` | `GVNHoistPass` |
| 245 | `gvn-sink` | `GVNSinkPass` |
| 246 | `helloworld` | `HelloWorldPass` |
| 247 | `indirectbr-expand` | `IndirectBrExpandPass` |
| 248 | `infer-address-spaces` | `InferAddressSpacesPass` |
| 249 | `infer-alignment` | `InferAlignmentPass` |
| 250 | `inject-tli-mappings` | `InjectTLIMappings` |
| 251 | `instcount` | `InstCountPass` |
| 252 | `instnamer` | `InstructionNamerPass` |
| 253 | `instsimplify` | `InstSimplifyPass` |
| 254 | `interleaved-access` | `InterleavedAccessPass` |
| 255 | `interleaved-load-combine` | `InterleavedLoadCombinePass` |
| 256 | `invalidate<all>` | via `sub_2342860` |
| 257 | `irce` | `IRCEPass` |
| 258 | `jump-threading` | `JumpThreadingPass` |
| 259 | `jump-table-to-switch` | `JumpTableToSwitchPass` |
| 260 | `kcfi` | `KCFIPass` |
| 261 | `kernel-info` | `KernelInfoPrinter` |
| 262 | `lcssa` | `LCSSAPass` |
| 263 | `libcalls-shrinkwrap` | `LibCallsShrinkWrapPass` |
| 264 | `lint` | `LintPass` |
| 265 | `load-store-vectorizer` | `LoadStoreVectorizerPass` |
| 266 | `loop-data-prefetch` | `LoopDataPrefetchPass` |
| 267 | `loop-distribute` | `LoopDistributePass` |
| 268 | `loop-fusion` | `LoopFusePass` |
| 269 | `loop-load-elim` | `LoopLoadEliminationPass` |
| 270 | `loop-simplify` | `LoopSimplifyPass` |
| 271 | `loop-sink` | `LoopSinkPass` |
| 272 | `loop-versioning` | `LoopVersioningPass` |
| 273 | `lower-atomic` | `LowerAtomicPass` |
| 274 | `lower-constant-intrinsics` | `LowerConstantIntrinsicsPass` |
| 275 | `lower-expect` | `LowerExpectIntrinsicPass` |
| 276 | `lower-guard-intrinsic` | `LowerGuardIntrinsicPass` |
| 277 | `lower-invoke` | `LowerInvokePass` |
| 278 | `lower-widenable-condition` | `LowerWidenableConditionPass` |
| 279 | `make-guards-explicit` | `MakeGuardsExplicitPass` |
| 280 | `mem2reg` | `PromotePass` |
| 281 | `memcpyopt` | `MemCpyOptPass` |
| 282 | `memprof` | `MemProfilerPass` |
| 283 | `mergeicmps` | `MergeICmpsPass` |
| 284 | `mergereturn` | `UnifyFunctionExitNodesPass` |
| 285 | `move-auto-init` | `MoveAutoInitPass` |
| 286 | `nary-reassociate` | `NaryReassociatePass` |
| 287 | `newgvn` | `NewGVNPass` |
| 288 | `no-op-function` | `NoOpFunctionPass` |
| 289 | `normalize` | `IRNormalizerPass` |
| 290 | `objc-arc` | `ObjCARCOptPass` |
| 291 | `objc-arc-contract` | `ObjCARCContractPass` |
| 292 | `objc-arc-expand` | `ObjCARCExpandPass` |
| 293 | `pa-eval` | `PAEvalPass` |
| 294 | `partially-inline-libcalls` | `PartiallyInlineLibCallsPass` |
| 295 | `pgo-memop-opt` | `PGOMemOPSizeOpt` |
| 296 | `place-safepoints` | `PlaceSafepointsPass` |
| 297 | `print` | `PrintFunctionPass` |
| 298–338 | `print<access-info>` ... `print-predicateinfo` | (41 printer passes) |
| 339 | `reassociate` | `ReassociatePass` |
| 340 | `redundant-dbg-inst-elim` | `RedundantDbgInstEliminationPass` |
| 341 | `reg2mem` | `RegToMemPass` |
| 342 | `safe-stack` | `SafeStackPass` |
| 343 | `sandbox-vectorizer` | `SandboxVectorizerPass` |
| 344 | `scalarize-masked-mem-intrin` | `ScalarizeMaskedMemIntrinPass` |
| 345 | `sccp` | `SCCPPass` |
| 346 | `select-optimize` | `SelectOptimizePass` |
| 347 | `separate-const-offset-from-gep` | `SeparateConstOffsetFromGEPPass` |
| 348 | `sink` | `SinkingPass` |
| 349 | `sjlj-eh-prepare` | `SjLjEHPreparePass` |
| 350 | `slp-vectorizer` | `SLPVectorizerPass` |
| 351 | `slsr` | `StraightLineStrengthReducePass` |
| 352 | `stack-protector` | `StackProtectorPass` |
| 353 | `strip-gc-relocates` | `StripGCRelocates` |
| 354 | `tailcallelim` | `TailCallElimPass` |
| 355 | `transform-warning` | `WarnMissedTransformationsPass` |
| 356 | `trigger-crash-function` | `TriggerCrashFunctionPass` |
| 357 | `trigger-verifier-error` | `TriggerVerifierErrorPass` |
| 358 | `tsan` | `ThreadSanitizerPass` |
| 359 | `unify-loop-exits` | `UnifyLoopExitsPass` |
| 360 | `vector-combine` | `VectorCombinePass` |
| 361 | `verify` | via `sub_2342870` |
| 362–368 | `verify<cycles>` ... `verify<scalar-evolution>` | (7 verifiers) |
| 369–374 | `view-cfg` ... `view-post-dom-only` | (6 viewers) |
| 375 | `wasm-eh-prepare` | `WasmEHPreparePass` |

#### NVIDIA Function Passes (entries 376–392)

Registered at lines 2212–2292 of `sub_2342890`.

| # | Pass Name | LLVM Class | Reg. Line | Purpose |
|---|---|---|---|---|
| 376 | **`basic-dbe`** | `BasicDeadBarrierEliminationPass` | 2212 | Removes dead `bar.sync` instructions |
| 377 | **`branch-dist`** | `BranchDistPass` | 2217 | Branch distribution for divergence control |
| 378 | **`byval-mem2reg`** | `ByValMem2RegPass` | 2222 | Promotes byval arguments to registers |
| 379 | **`bypass-slow-division`** | `BypassSlowDivisionPass` | 2227 | Fast-path for small-operand division |
| 380 | **`normalize-gep`** | `NormalizeGepPass` | 2232 | GEP canonicalization for address arithmetic |
| 381 | **`nvvm-reflect-pp`** | `SimplifyConstantConditionalsPass` | 2237 | Folds `__nvvm_reflect` results (post-processing) |
| 382 | **`nvvm-peephole-optimizer`** | `NVVMPeepholeOptimizerPass` | 2242 | NVVM-specific peephole rewrites |
| 383 | **`old-load-store-vectorizer`** | `OldLoadStoreVectorizerPass` | 2247 | Legacy load/store vectorization |
| 384 | **`print<merge-sets>`** | `MergeSetsAnalysisPrinterPass` | 2252 | Printer for merge-sets analysis |
| 385 | **`remat`** | `RematerializationPass` | 2257 | Register-pressure-aware rematerialization |
| 386 | **`print<rpa>`** | `RegisterPressurePrinterPass` | 2262 | Printer for register pressure analysis |
| 387 | **`propagate-alignment`** | `PropagateAlignmentPass` | 2267 | Propagates alignment through pointer chains |
| 388 | **`reuse-local-memory`** | `ReuseLocalMemoryPass` | 2272 | Shares local memory across kernels |
| 389 | **`set-local-array-alignment`** | `SetLocalArrayAlignmentPass` | 2277 | Aligns stack arrays for coalescing |
| 390 | **`sinking2`** | `Sinking2Pass` | 2282 | Enhanced instruction sinking |
| 391 | **`d2ir-scalarizer`** | `ScalarizerPass` (NVIDIA alias) | 2287 | NVIDIA-branded scalarization |
| 392 | **`sink<rp-aware>`** | `SinkingPass` (variant) | 2292 | Register-pressure-aware sinking |

#### Parameterized Function Passes (entries 393–419)

| # | Pass Name | Class | Parameters |
|---|---|---|---|
| 393 | `cfguard` | `CFGuardPass` | `check;dispatch` |
| 394 | `early-cse` | `EarlyCSEPass` | `memssa` |
| 395 | `ee-instrument` | `EntryExitInstrumenterPass` | `post-inline` |
| 396 | `function-simplification` | (byte_3F871B3) | `O1;O2;O3;Os;Oz` |
| 397 | `gvn` | `GVNPass` | `no-pre;pre;no-load-pre;load-pre;...` |
| 398 | `instcombine` | `InstCombinePass` | `no-aggressive-aggregate-splitting;...;max-iterations=N` |
| 399 | `loop-unroll` | `LoopUnrollPass` | `O0;O1;O2;O3;full-unroll-max=N;...` |
| 400 | `loop-vectorize` | `LoopVectorizePass` | `no-interleave-forced-only;...` |
| 401 | `lower-allow-check` | `LowerAllowCheckPass` | (empty) |
| 402 | `lower-matrix-intrinsics` | `LowerMatrixIntrinsicsPass` | `minimal` |
| 403 | `lower-switch` | `LowerSwitchPass` | `enable-jump-table` |
| 404 | `mldst-motion` | `MergedLoadStoreMotionPass` | `no-split-footer-bb;split-footer-bb` |
| 405 | `print<da>` | `DependenceAnalysisPrinterPass` | `normalized-results` |
| 406 | `print<memoryssa>` | `MemorySSAPrinterPass` | `no-ensure-optimized-uses` |
| 407 | `print<stack-lifetime>` | `StackLifetimePrinterPass` | `may;must` |
| 408 | `scalarizer` | `ScalarizerPass` | `load-store;no-load-store;variable-insert-extract;...` |
| 409 | `separate-const-offset-from-gep` | `SeparateConstOffsetFromGEPPass` | `lower-gep` |
| 410 | `simplifycfg` | `SimplifyCFGPass` | `simplify-unreachable;...;bonus-inst-threshold=N` |
| 411 | `speculative-execution` | `SpeculativeExecutionPass` | `only-if-divergent-target` |
| 412 | `sroa` | `SROAPass` | `preserve-cfg;modify-cfg` |
| 413 | `structurizecfg` | `StructurizeCFG` | `skip-uniform-regions` |
| 414 | `win-eh-prepare` | `WinEHPreparePass` | `demote-catchswitch-only` |
| 415 | **`bounds-checking`** | `BoundsCheckingPass` (modified) | `trap` |
| 416 | **`memory-space-opt`** | `MemorySpaceOptPass` | `first-time;second-time;no-warnings;warnings` |
| 417 | **`lower-aggr-copies`** | `LowerAggrCopiesPass` | `lower-aggr-func-args` |
| 418 | **`lower-struct-args`** | `LowerStructArgsPass` | `opt-byval` |
| 419 | **`process-restrict`** | `ProcessRestrictPass` | `propagate-only` |

### LoopNest Passes (entries 420–423)

| # | Pass Name | LLVM Class |
|---|---|---|
| 420 | `loop-flatten` | `LoopFlattenPass` |
| 421 | `loop-interchange` | `LoopInterchangePass` |
| 422 | `loop-unroll-and-jam` | `LoopUnrollAndJamPass` |
| 423 | `no-op-loopnest` | `NoOpLoopNestPass` |

### Loop Analyses (entries 424–428)

| # | Pass Name | LLVM Class |
|---|---|---|
| 424 | `ddg` | `DDGAnalysis` |
| 425 | `iv-users` | `IVUsersAnalysis` |
| 426 | `no-op-loop` | `NoOpLoopAnalysis` |
| 427 | `pass-instrumentation` | via `sub_2342830` |
| 428 | `should-run-extra-simple-loop-unswitch` | `ShouldRunExtraSimpleLoopUnswitch` |

### Loop Passes (entries 429–455)

| # | Pass Name | LLVM Class |
|---|---|---|
| 429 | `canon-freeze` | `CanonicalizeFreezeInLoopsPass` |
| 430 | `dot-ddg` | `DDGDotPrinterPass` |
| 431 | `guard-widening` | via `sub_2342880` |
| 432 | `extra-simple-loop-unswitch-passes` | `ExtraLoopPassManager<...>` |
| 433 | `indvars` | `IndVarSimplifyPass` |
| 434 | `invalidate<all>` | via `sub_2342860` |
| 435 | `loop-bound-split` | `LoopBoundSplitPass` |
| 436 | `loop-deletion` | `LoopDeletionPass` |
| 437 | `loop-idiom` | `LoopIdiomRecognizePass` |
| 438 | `loop-idiom-vectorize` | `LoopIdiomVectorizePass` |
| 439 | `loop-instsimplify` | `LoopInstSimplifyPass` |
| 440 | `loop-predication` | `LoopPredicationPass` |
| 441 | `loop-reduce` | `LoopStrengthReducePass` |
| 442 | `loop-term-fold` | `LoopTermFoldPass` |
| 443 | `loop-simplifycfg` | `LoopSimplifyCFGPass` |
| 444 | `loop-unroll-full` | `LoopFullUnrollPass` |
| 445 | `loop-versioning-licm` | `LoopVersioningLICMPass` |
| 446 | `no-op-loop` | `NoOpLoopPass` |
| 447 | `print` | `PrintLoopPass` |
| 448–450 | `print<ddg>`, `print<iv-users>`, `print<loop-cache-cost>`, `print<loopnest>` | (printers) |
| 451 | **`loop-index-split`** | `LoopIndexSplitPass` | NVIDIA |

#### Parameterized Loop Passes (entries 452–455)

| # | Pass Name | Class | Parameters |
|---|---|---|---|
| 452 | `licm` | `LICMPass` | `allowspeculation;conservative-calls` |
| 453 | `lnicm` | `LNICMPass` | `allowspeculation` |
| 454 | `loop-rotate` | `LoopRotatePass` | `no-header-duplication;header-duplication;...` |
| 455 | `simple-loop-unswitch` | `SimpleLoopUnswitchPass` | `nontrivial;no-nontrivial;trivial;no-trivial` |

### Machine Function Analyses (entries 456–475)

| # | Pass Name | LLVM Class |
|---|---|---|
| 456 | `edge-bundles` | `EdgeBundlesAnalysis` |
| 457 | `livedebugvars` | `LiveDebugVariablesAnalysis` |
| 458 | `live-intervals` | `LiveIntervalsAnalysis` |
| 459 | `live-reg-matrix` | `LiveRegMatrixAnalysis` |
| 460 | `live-stacks` | `LiveStacksAnalysis` |
| 461 | `live-vars` | `LiveVariablesAnalysis` |
| 462 | `machine-block-freq` | `MachineBlockFrequencyAnalysis` |
| 463 | `machine-branch-prob` | `MachineBranchProbabilityAnalysis` |
| 464 | `machine-cycles` | `MachineCycleAnalysis` |
| 465 | `machine-dom-tree` | `MachineDominatorTreeAnalysis` |
| 466 | `machine-loops` | `MachineLoopAnalysis` |
| 467 | `machine-opt-remark-emitter` | `MachineOptimizationRemarkEmitterAnalysis` |
| 468 | `machine-post-dom-tree` | `MachinePostDominatorTreeAnalysis` |
| 469 | `machine-trace-metrics` | `MachineTraceMetricsAnalysis` |
| 470 | `pass-instrumentation` | via `sub_2342830` |
| 471 | `regalloc-evict` | `RegAllocEvictionAdvisorAnalysis` |
| 472 | `regalloc-priority` | `RegAllocPriorityAdvisorAnalysis` |
| 473 | `slot-indexes` | `SlotIndexesAnalysis` |
| 474 | `spill-code-placement` | `SpillPlacementAnalysis` |
| 475 | `virtregmap` | `VirtRegMapAnalysis` |

### Machine Function Passes (entries 476–526)

| # | Pass Name | LLVM Class |
|---|---|---|
| 476 | `dead-mi-elimination` | `DeadMachineInstructionElimPass` |
| 477 | `detect-dead-lanes` | `DetectDeadLanesPass` |
| 478 | `early-ifcvt` | `EarlyIfConverterPass` |
| 479 | `early-machinelicm` | `EarlyMachineLICMPass` |
| 480 | `early-tailduplication` | `EarlyTailDuplicatePass` |
| 481 | `finalize-isel` | `FinalizeISelPass` |
| 482 | `fixup-statepoint-caller-saved` | `FixupStatepointCallerSavedPass` |
| 483 | `localstackalloc` | `LocalStackSlotAllocationPass` |
| 484 | `machine-cp` | `MachineCopyPropagationPass` |
| 485 | `machine-cse` | `MachineCSEPass` |
| 486 | `machine-latecleanup` | `MachineLateInstrsCleanupPass` |
| 487 | `machine-scheduler` | `MachineSchedulerPass` |
| 488 | `machinelicm` | `MachineLICMPass` |
| 489 | `no-op-machine-function` | `NoOpMachineFunctionPass` |
| 490 | `opt-phis` | `OptimizePHIsPass` |
| 491 | `patchable-function` | `PatchableFunctionPass` |
| 492 | `peephole-opt` | `PeepholeOptimizerPass` |
| 493 | `phi-node-elimination` | `PHIEliminationPass` |
| 494 | `post-RA-sched` | `PostRASchedulerPass` |
| 495 | `postmisched` | `PostMachineSchedulerPass` |
| 496 | `post-ra-pseudos` | `ExpandPostRAPseudosPass` |
| 497 | `print` | `PrintMIRPass` |
| 498–510 | `print<livedebugvars>` ... `print<virtregmap>` | (13 MF printers) |
| 511 | `reg-usage-collector` | `RegUsageInfoCollectorPass` |
| 512 | `reg-usage-propagation` | `RegUsageInfoPropagationPass` |
| 513 | `register-coalescer` | `RegisterCoalescerPass` |
| 514 | `rename-independent-subregs` | `RenameIndependentSubregsPass` |
| 515 | `remove-redundant-debug-values` | `RemoveRedundantDebugValuesPass` |
| 516 | `require-all-machine-function-properties` | `RequireAllMachineFunctionPropertiesPass` |
| 517 | `stack-coloring` | `StackColoringPass` |
| 518 | `stack-slot-coloring` | `StackSlotColoringPass` |
| 519 | `tailduplication` | `TailDuplicatePass` |
| 520 | `trigger-verifier-error` | `TriggerVerifierErrorPass` |
| 521 | `two-address-instruction` | `TwoAddressInstructionPass` |
| 522 | `verify` | `MachineVerifierPass` |
| 523 | `verify<machine-trace-metrics>` | `MachineTraceMetricsVerifierPass` |
| 524 | `machine-sink` | `MachineSinkingPass` (parameterized) |
| 525 | `regallocfast` | `RegAllocFastPass` (parameterized) |
| 526 | `greedy` | `RAGreedyPass` (parameterized, LAST registered) |

No NVIDIA-specific machine function passes were identified in the registration table; NVIDIA's machine-level customizations are implemented through target hooks in the NVPTX backend rather than as separately registered passes.

## Runtime Pass Execution Order

Registration order (above) describes **what is known to the pipeline parser**. Runtime execution order is determined by `sub_12E54A0` (the pipeline assembler) and controlled by the tier system. The execution order varies dramatically depending on: (1) optimization level, (2) fast-compile mode, (3) language string, and (4) individual pass enable/disable flags in NVVMPassOptions.

### The AddPass Mechanism — `sub_12DE0B0`

All runtime pass insertion uses `sub_12DE0B0` (`0x12DE0B0`), a hash-table-based function that:

1. Hashes the pass pointer: `(pass >> 9) ^ (pass >> 4)`
2. Probes an open-addressed hash table at `passMgr+80`
3. Stores the pass pointer and a flags byte (`flags | 2` if barrier set)
4. Appends the pass pointer to a dynamic array at `passMgr[0]`
5. Increments the counter at `passMgr+8`

The third parameter encodes pass type: `0` = ModulePass/AnalysisPass, `1` = FunctionPass. The fourth parameter is a scheduling barrier hint.

### Tier System Architecture

The tier system is NVIDIA's mechanism for interleaving custom passes with standard LLVM passes at precise points. The main optimization loop in `sub_12E54A0` iterates over a plugin/extension pass array at `opts[4488..4496]` (16-byte stride: vtable + phase_id), and fires tier sub-pipelines when the accumulated phase counter exceeds their thresholds:

```c
// Pseudocode from sub_12E54A0, lines 481-553
for (entry = opts[4488]; entry < opts[4496]; entry += 16) {
    phase_id = entry[8];

    if (opts[4224] && phase_id > opts[4228]) {   // Tier 0
        sub_12DE330(PM, opts);                    // Full optimization
        opts[4224] = 0;                           // Fire once
    }
    if (opts[3528] && phase_id > opts[3532]) {    // Tier 1
        sub_12DE8F0(PM, 1, opts);
        opts[3528] = 0;
    }
    if (opts[3568] && phase_id > opts[3572]) {    // Tier 2
        sub_12DE8F0(PM, 2, opts);
        opts[3568] = 0;
    }
    if (opts[3608] && phase_id > opts[3612]) {    // Tier 3
        sub_12DE8F0(PM, 3, opts);
        opts[3608] = 0;
    }

    pass = entry->vtable[72]();                   // Plugin pass factory call
    sub_12DE0B0(PM, pass, 1, 0);                  // Insert plugin pass

    if (opts[3904])                               // Debug mode
        insert_verifier_after_each();
}
// Remaining unfired tiers fire unconditionally after loop
```

The tier control fields in the NVVMPassOptions struct:

| Offset | Type | Field |
|---|---|---|
| `+3528` | bool | Tier 1 enable |
| `+3532` | int | Tier 1 phase threshold |
| `+3568` | bool | Tier 2 enable |
| `+3572` | int | Tier 2 phase threshold |
| `+3608` | bool | Tier 3 enable |
| `+3612` | int | Tier 3 phase threshold |
| `+4224` | bool | Tier 0 (full optimization) enable |
| `+4228` | int | Tier 0 phase threshold |

### Infrastructure Setup (Always Runs)

These five passes are always inserted first, regardless of optimization level:

| Pos | Factory | Identity | AddPass Flags |
|---|---|---|---|
| 1 | `sub_149CCE0` (alloc 368B) | `TargetLibraryInfoWrapperPass` | `(PM, TLI, 0, 0)` Module |
| 2 | `sub_1BFB520` (alloc 208B) | `TargetTransformInfoWrapperPass` | `(PM, TTI, 1, 0)` Function |
| 3 | `sub_14A7550` | `VerifierPass` / `BasicAliasAnalysis` | `(PM, _, 0, 0)` Module |
| 4 | `sub_1361950` | `AssumptionCacheTracker` | `(PM, _, 0, 0)` Module |
| 5 | `sub_1CB0F50` | `ProfileSummaryInfoWrapperPass` | `(PM, _, 1, 0)` Function |

### Tier 0 — Full Optimization (`sub_12DE330`)

Called when `opts[4224]` (optimization enabled) and the phase threshold is exceeded. This is the primary optimization sub-pipeline for O1/O2/O3, adding ~40 passes. Address: `0x12DE330`.

> **Confidence note:** Pass identifications are based on diagnostic strings, factory-function signatures, and pipeline ordering. Most identifications are HIGH confidence (confirmed by unique string literals). Entries marked `[MEDIUM confidence]` are inferred from code structure, argument patterns, or address proximity rather than direct string evidence.

| Pos | Factory Address | Likely Pass | Guard Condition |
|---|---|---|---|
| 1 | `sub_1654860(1)` | BreakCriticalEdges | always |
| 2 | `sub_1A62BF0(1,0,0,1,0,0,1)` | LLVM standard pipeline #1 | always |
| 3 | `sub_1B26330` | MemCpyOpt | always |
| 4 | `sub_185D600` | IPConstantPropagation | always |
| 5 | `sub_1C6E800` | GVN | always |
| 6 | `sub_1C6E560` | NewGVN/GVNHoist `[MEDIUM confidence]` | always |
| 7 | `sub_1857160` | NVVMReflect | always |
| 8 | `sub_1842BC0` | SCCP | always |
| 9 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160]` |
| 10 | `sub_12D4560` | NVVMVerifier | always |
| 11 | `sub_18A3090` | NVVMPredicateOpt | always |
| 12 | `sub_184CD60` | ConstantMerge | always |
| 13 | `sub_1869C50(1,0,1)` | Sink/MemSSA `[MEDIUM confidence]` — three-arg factory matches Sink with MemSSA parameters, but could also be a custom sinking variant | `!opts[1040]` |
| 14 | `sub_1833EB0(3)` | TailCallElim/JumpThreading `[MEDIUM confidence]` — integer arg=3 could be JumpThreading threshold or TailCallElim mode; no disambiguating string | always |
| 15 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160]` |
| 16 | `sub_1952F90(-1)` | LoopIndexSplit | always |
| 17 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | always |
| 18 | `sub_1A223D0` | NVVMIRVerification | always |
| 19 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160]` |
| 20 | `sub_1A7A9F0` | InstructionSimplify | always |
| 21 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | always |
| 22 | `sub_1A02540` | GenericToNVVM | always |
| 23 | `sub_198DF00(-1)` | LoopSimplify | always |
| 24 | `sub_1C76260` | ADCE | `!opts[1320]` |
| 25 | `sub_195E880(0)` | LICM | `opts[2880]` |
| 26 | `sub_19C1680(0,1)` | LoopUnroll | `!opts[1360]` |
| 27 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160]` |
| 28 | `sub_19401A0` | InstCombine | always |
| 29 | `sub_1968390` | SROA | always |
| 30 | `sub_196A2B0` | EarlyCSE | always |
| 31 | `sub_19B73C0(2,-1,-1,-1,-1,-1,-1)` | LoopUnswitch | always |
| 32 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160]` |
| 33 | `sub_190BB10(0,0)` | SimplifyCFG | always |
| 34 | `sub_1A13320` | NVVMRematerialization | always |
| 35 | `sub_17060B0(1,1)` | PrintModulePass (verified) | `opts[3160]` |
| 36 | `sub_18F5480` | DSE | always |
| 37 | `sub_18DEFF0` | DCE | always |
| 38 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | always |
| 39 | `sub_18B1DE0` | NVVMLoopPass/BarrierOpt `[MEDIUM confidence]` — address is in NVVM pass range, but dual name reflects ambiguity between loop optimization and barrier optimization roles | always |
| 40 | `sub_1841180` | FunctionAttrs | always |

### Tier 1/2/3 — Phase-Specific Sub-pipeline (`sub_12DE8F0`)

Called with tier number (1, 2, or 3). Address: `0x12DE8F0`. Stores the tier value to `qword_4FBB410`. When tier==3, sets `qword_4FBB370` = 6 if BYTE4 was 0 (enables advanced barrier and memory space optimization features).

The pass sequence is significantly longer than Tier 0 and varies by tier. The following shows the **superset** of all passes that can be inserted; tier-based guards are annotated.

> **Confidence note:** Same methodology as Tier 0 table above. Most identifications confirmed by diagnostic strings or NVVMPassOptions slot cross-references.

| Pos | Factory Address | Likely Pass | Guard |
|---|---|---|---|
| 1 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` |
| 2 | `sub_1A223D0` | NVVMIRVerification | `!opts[2600]` |
| 3 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering (barrier) | `!opts[2000]` |
| 4 | `sub_18E4A00` | NVVMBarrierAnalysis | `opts[3488]` |
| 5 | `sub_1C98160(0)` | NVVMLowerBarriers | `opts[3488]` |
| 6 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` |
| 7 | `sub_12D4560` | NVVMVerifier | `!opts[600]` |
| 8 | `sub_185D600` | IPConstPropagation | `opts[3200] && !opts[920]` |
| 9 | `sub_1857160` | NVVMReflect | `opts[3200] && !opts[880]` |
| 10 | `sub_18A3430` | NVVMPredicateOpt | `opts[3200] && !opts[1120]` |
| 11 | `sub_1842BC0` | SCCP | `opts[3200] && !opts[720]` |
| 12 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` |
| 13 | `sub_12D4560` | NVVMVerifier | `!opts[600]` |
| 14 | `sub_18A3090` | NVVMPredicateOpt variant | `opts[3200] && !opts[2160]` |
| 15 | `sub_184CD60` | ConstantMerge | `opts[3200] && !opts[1960]` |
| 16 | `sub_190BB10(1,0)` | SimplifyCFG | tier!=1 && `!opts[1040] && !opts[1200]` |
| 17 | `sub_1952F90(-1)` | LoopIndexSplit | (same guard) && `!opts[1160]` |
| 18 | `sub_12D4560` | NVVMVerifier | (same guard) && `!opts[600]` |
| 19 | `sub_17060B0(1,0)` | PrintModulePass | (same guard) && `!opts[1080]` |
| 20 | `sub_195E880(0)` | LICM | `opts[3704] && opts[2880] && !opts[1240]` |
| 21 | `sub_1C8A4D0(v)` | EarlyCSE | `v=1 if opts[3704]` |
| 22 | `sub_1869C50(1,0,1)` | Sink | tier!=1 && `!opts[1040]` |
| 23 | `sub_1833EB0(3)` | TailCallElim | tier==3 && `!opts[320]` |
| 24 | `sub_1CC3990` | NVVMUnreachableBlockElim | `!opts[2360]` |
| 25 | `sub_18EEA90` | CorrelatedValuePropagation | `opts[3040]` |
| 26 | `sub_12D4560` | NVVMVerifier | `!opts[600]` |
| 27 | `sub_1A223D0` | NVVMIRVerification | `!opts[2600]` |
| 28 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` |
| 29 | `sub_1C4B6F0` | Inliner | `!opts[440] && !opts[480]` |
| 30 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` |
| 31 | `sub_1A7A9F0` | InstructionSimplify | `!opts[2720]` |
| 32 | `sub_12D4560` | NVVMVerifier | `!opts[600]` |
| 33 | `sub_1A02540` | GenericToNVVM | `!opts[2200]` |
| 34 | `sub_198DF00(-1)` | LoopSimplify | `!opts[1520]` |
| 35 | `sub_1C76260` | ADCE | `!opts[1320] && !opts[1480]` |
| 36 | `sub_17060B0(1,0)` | PrintModulePass | (same guard) |
| 37 | `sub_12D4560` | NVVMVerifier | (same guard) |
| 38 | `sub_195E880(0)` | LICM | `opts[2880] && !opts[1240]` |
| 39 | `sub_1C98160(0/1)` | NVVMLowerBarriers | `opts[3488]` |
| 40 | `sub_19C1680(0,1)` | LoopUnroll | `!opts[1360]` |
| 41 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` |
| 42 | `sub_19401A0` | InstCombine | `!opts[1000]` |
| 43 | `sub_196A2B0` | EarlyCSE | `!opts[1440]` |
| 44 | `sub_1968390` | SROA | `!opts[1400]` |
| 45 | `sub_19B73C0(tier,...)` | LoopUnswitch | tier!=1, SM-arch-dependent params |
| 46 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` |
| 47 | `sub_19B73C0(tier,...)` | LoopUnswitch (2nd) | `!opts[2760]` |
| 48 | `sub_1A62BF0(1,...)` | LLVM standard pipeline | `!opts[600]` |
| 49 | `sub_1A223D0` | NVVMIRVerification | `!opts[2600]` |
| 50 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` |
| 51 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` |
| 52 | `sub_190BB10(0,0)` | SimplifyCFG | `!opts[960]` |
| 53 | `sub_1922F90` | NVIDIA loop pass | `opts[3080]` |
| 54 | `sub_195E880(0)` | LICM | `opts[2880] && !opts[1240]` |
| 55 | `sub_1A13320` | NVVMRematerialization | `!opts[2320]` |
| 56 | `sub_1968390` | SROA | `!opts[1400]` |
| 57 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` |
| 58 | `sub_18EEA90` | CorrelatedValuePropagation | `opts[3040]` |
| 59 | `sub_18F5480` | DSE | `!opts[760]` |
| 60 | `sub_18DEFF0` | DCE | `!opts[280]` |
| 61 | `sub_1A62BF0(1,...)` | LLVM standard pipeline | `!opts[600]` |
| 62 | `sub_1AAC510` | NVIDIA-specific pass | `!opts[520] && !opts[560]` |
| 63 | `sub_1A223D0` | NVVMIRVerification | `!opts[2600]` |
| 64 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` |
| 65 | `sub_1C8E680` | MemorySpaceOpt | `!opts[2680]`, param from `opts[3120]` |
| 66 | `sub_1A223D0` | NVVMIRVerification | `opts[3120] && !opts[2600]` |
| 67 | `sub_17060B0(1,0)` | PrintModulePass (barrier) | `!opts[1080]` |
| 68 | `sub_1CC71E0` | NVVMGenericAddrOpt | `!opts[2560]` |
| 69 | `sub_1C98270(1,opts[2920])` | NVVMLowerBarriers variant | `opts[3488]` |
| 70 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` |
| 71 | `sub_1C6FCA0` | ADCE | `opts[2840] && !opts[1840]` |
| 72 | `sub_18B1DE0` | LoopOpt/BarrierOpt | `opts[3200] && !opts[2640]` |
| 73 | `sub_1857160` | NVVMReflect | `opts[3200] && tier==3 && !opts[880]` |
| 74 | `sub_1841180` | FunctionAttrs | `opts[3200] && !opts[680]` |
| 75 | `sub_1C46000` | NVVMLateOpt | tier==3 && `!opts[360]` |
| 76 | `sub_1841180` | FunctionAttrs (2nd) | `opts[3200] && !opts[680]` |
| 77 | `sub_1CBC480` | NVVMLowerAlloca | `!opts[2240] && !opts[2280]` |
| 78 | `sub_1CB73C0` | NVVMBranchDist | `!opts[2080] && !opts[2120]` |
| 79 | `sub_1C7F370(1)` | NVVMWarpShuffle | `opts[3328] && !opts[1640]` |
| 80 | `sub_1CC5E00` | NVVMReduction | `opts[3328] && !opts[2400]` |
| 81 | `sub_1CC60B0` | NVVMSinking2 | `opts[3328] && !opts[2440]` |
| 82 | `sub_1CB73C0` | NVVMBranchDist (2nd) | `opts[3328] && !opts[2080] && !opts[2120]` |
| 83 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3328] && !opts[1080]` |
| 84 | `sub_1B7FDF0(3)` | Reassociate | `opts[3328] && !opts[1280]` |
| 85 | `sub_17060B0(1,0)` | PrintModulePass (final) | `opts[3160] && !opts[1080]` |

### Optimization Level Summary

| Pipeline | Sub-pipeline called | lsa-opt | mem-space-opt | Approx. passes |
|---|---|---|---|---|
| `nvopt<O0>` | (minimal, `sub_1C8A4D0(0)` only) | off | off | ~5–8 |
| `nvopt<Ofcmax>` | Sinking2 + common tail only | forced 0 | forced 0 | ~12–15 |
| `nvopt<Ofcmid>` | mid-level pipeline | normal | enabled | ~25–30 |
| `nvopt<Ofcmin>` | close to full pipeline | normal | enabled | ~30–35 |
| `nvopt<O1>` | `sub_12DE330` (Tier 0) | normal | enabled | ~35 |
| `nvopt<O2>` | `sub_12DE330` + Tier 1/2 | normal | enabled | ~35+ |
| `nvopt<O3>` | `sub_12DE330` + Tier 1/2/3 | normal | enabled | ~35+ |

O1/O2/O3 all route through the same `sub_12DE330` (Tier 0). The difference manifests through the tiered pass inserter `sub_12DE8F0`: O1 only fires Tier 1, O2 fires Tiers 1–2, O3 fires all three tiers. Within the tiers, passes additionally vary by: loop unroll factor (parameter to `sub_1833EB0`), vectorizer width (parameters to `sub_19B73C0`), CGSCC iteration count (first parameter to `sub_1A62BF0`), and the SM-architecture-dependent late passes gated by `opts[3328]`.

**Ofcmax critical behavior**: when fast-compile level == 2 (max), the libnvvm pipeline builder **forces** `-lsa-opt=0` and `-memory-space-opt=0` even if the user explicitly enables them. This is confirmed in both `sub_9624D0` (line 1358) and `sub_12CC750` (line 2025).

### Codegen Dispatch — `sub_12DFE00`

After all optimization tiers complete, `sub_12DFE00` (`0x12DFE00`) performs codegen pass scheduling. This is NOT a simple pass adder — it performs a full dependency graph construction:

1. Reads optimization level from `opts[200]` (0 = minimal, >1 = enable dependency tracking)
2. Iterates all passes already in the pass manager
3. For each pass, calls `vtable+112` (`isCodeGenOnly()`) to filter
4. Calls `vtable+16` (`getAnalysisUsage()`) to extract dependencies
5. Builds a secondary hash table of ordering constraints
6. Dispatches each pass to the codegen subsystem in topological order via the subtarget hook at `vtable+16`

## Pass Classification Statistics

| Category | Count |
|---|---|
| Module analyses | 18 |
| Module passes | ~131 |
| CGSCC analyses | 3 |
| CGSCC passes | ~10 |
| Function analyses | ~39 |
| Function AA analyses | 5 |
| Function passes | ~219 |
| LoopNest passes | 4 |
| Loop analyses | 5 |
| Loop passes | ~26 |
| MachineFunction analyses | 20 |
| MachineFunction passes | ~50 |
| **Total** | **~526** |
| NVIDIA additions (`StringMap` rows) | 33 |
| NVIDIA additions (unique pass classes) | 35 |
| Standard LLVM | ~493 |

## Complete Pass Factory Address Map

Every unique pass factory address observed in `sub_12E54A0`, `sub_12DE330`, and `sub_12DE8F0`:

| Function | Address | Size | Role |
|---|---|---|---|
| NVVMVerifier | `sub_12D4560` | many (tiers) | many (tiers) |
| AssumptionCacheTracker | `sub_1361950` | 1 | 1 |
| TargetLibraryInfoWrapperPass | `sub_149CCE0` | 1 | 1 |
| VerifierPass/BasicAA | `sub_14A7550` | 1 | 1 |
| BreakCriticalEdges | `sub_1654860` | 2 | 2 |
| PrintModulePass (debug dump) | `sub_17060B0` | ~30+ | ~30+ |
| InstructionCombining | `sub_1832270` | 2 | 2 |
| TailCallElim/JumpThreading | `sub_1833EB0` | 3 | 3 |
| FunctionAttrs | `sub_1841180` | 3 | 3 |
| SCCP | `sub_1842BC0` | 2 | 2 |
| NVVMReflect | `sub_1857160` | ~8 | ~8 |
| IPConstantPropagation | `sub_185D600` | 3 | 3 |
| Sink (MemorySSA-based) | `sub_1869C50` | 3 | 3 |
| NVVMPredicateOpt | `sub_18A3090` | 2 | 2 |
| AggressiveInstCombine | `sub_18A3430` | 2 | 2 |
| NVVMLoopOpt/BarrierOpt | `sub_18B1DE0` | 3 | 3 |
| Sinking2Pass (fast-mode) | `sub_18B3080` | 1 | 1 |
| DCE | `sub_18DEFF0` | 4 | 4 |
| NVVMBarrierAnalysis | `sub_18E4A00` | 1 | 1 |
| CorrelatedValuePropagation | `sub_18EEA90` | 3 | 3 |
| DSE | `sub_18F5480` | 2 | 2 |
| DeadArgElimination | `sub_18FD350` | 5 | 5 |
| SimplifyCFG | `sub_190BB10` | 4 | 4 |
| NVIDIA loop pass | `sub_1922F90` | 1 | 1 |
| LoopIndexSplit | `sub_1952F90` | 3 | 3 |
| LICM | `sub_195E880` | 4 | 4 |
| SROA | `sub_1968390` | 2 | 2 |
| EarlyCSE | `sub_196A2B0` | 2 | 2 |
| LoopUnroll/Vectorize | `sub_197E720` | 1 | 1 |
| LoopSimplify/IndVarSimplify | `sub_198DF00` | 3 | 3 |
| CorrelatedValuePropagation | `sub_198E2A0` | 1 | 1 |
| InstCombine | `sub_19401A0` | 2 | 2 |
| LoopUnswitch | `sub_19B73C0` | 3 | 3 |
| LoopUnroll | `sub_19C1680` | 2 | 2 |
| NVIDIA pass (unknown) | `sub_19CE990` | 1 | 1 |
| GenericToNVVM | `sub_1A02540` | 1 | 1 |
| NVVMRematerialization | `sub_1A13320` | 3 | 3 |
| NVVMIRVerification | `sub_1A223D0` | 5+ | 5+ |
| LLVM StandardPassPipeline | `sub_1A62BF0` | ~9 | ~9 |
| LoopIdiomRecognize | `sub_1A68E70` | 1 | 1 |
| InstructionSimplify | `sub_1A7A9F0` | 3 | 3 |
| NVIDIA-specific pass | `sub_1AAC510` | 1 | 1 |
| MemCpyOpt | `sub_1B26330` | 4 | 4 |
| Reassociate/Sinking | `sub_1B7FDF0` | 3 | 3 |
| TTIWrapperPass | `sub_1BFB520` | 1 | 1 |
| NVVMLateOpt | `sub_1C46000` | 1 | 1 |
| Inliner/AlwaysInline | `sub_1C4B6F0` | 2 | 2 |
| NewGVN/GVNHoist | `sub_1C6E560` | 1 | 1 |
| GVN | `sub_1C6E800` | 2 | 2 |
| ADCE (AggressiveDCE) | `sub_1C6FCA0` | 2 | 2 |
| ADCE variant | `sub_1C76260` | 2 | 2 |
| NVVMWarpShuffle | `sub_1C7F370` | 1 | 1 |
| EarlyCSE/GVN variant | `sub_1C8A4D0` | 3 | 3 |
| MemorySpaceOpt | `sub_1C8E680` | 4 | 4 |
| NVVMLowerBarriers | `sub_1C98160` | 4 | 4 |
| NVVMLowerBarriers variant | `sub_1C98270` | 1 | 1 |
| ProfileSummaryInfo | `sub_1CB0F50` | 1 | 1 |
| NVVMIntrinsicLowering | `sub_1CB4E40` | ~10 | ~10 |
| NVVMBranchDist | `sub_1CB73C0` | 3 | 3 |
| NVVMLowerAlloca | `sub_1CBC480` | 1 | 1 |
| NVVMUnreachableBlockElim | `sub_1CC3990` | 1 | 1 |
| NVVMReduction | `sub_1CC5E00` | 1 | 1 |
| NVVMSinking2 | `sub_1CC60B0` | 3 | 3 |
| NVVMGenericAddrOpt | `sub_1CC71E0` | 1 | 1 |
| NVVMFinalLowering | `sub_1CEBD10` | 1 | 1 |
| NVVMPeephole | `sub_1CEF8F0` | 2 | 2 |
| NVVMAnnotationsProcessor | `sub_215D9D0` | 2 | 2 |

Total unique pass factories: ~65.

## NVVMPassOptions Offset-to-Pass Guard Map

The NVVMPassOptions struct (4,512 bytes, 221 slots) controls which passes execute. The pipeline assembler reads boolean flags at specific offsets to gate pass insertion. See [NVVMPassOptions](../config/nvvm-pass-options.md) for the full slot layout. Key offset-to-pass mappings:

| Offset | Slot | Type | Controls |
|---|---|---|---|
| +200 | 9 | int | Optimization level (0/1/2/3) |
| +280 | 15 | bool | DCE disable |
| +320 | 17 | bool | TailCallElim/JumpThreading disable |
| +360 | 19 | bool (default=1) | NVVMLateOpt disable |
| +600 | 31 | bool | NVVMVerifier disable |
| +720 | 37 | bool | SCCP disable |
| +760 | 39 | bool | DSE disable |
| +880 | 45 | bool | NVVMReflect disable |
| +920 | 47 | bool | IPConstantPropagation disable |
| +960 | 49 | bool | SimplifyCFG disable |
| +1000 | 51 | bool | InstCombine disable |
| +1040 | 53 | bool | Sink/MemSSA disable |
| +1080 | 55 | bool | PrintModulePass disable |
| +1160 | 59 | bool | LoopIndexSplit disable |
| +1240 | 63 | bool | LICM disable |
| +1280 | 65 | bool | Reassociate disable |
| +1320 | 67 | bool | ADCE disable |
| +1360 | 69 | bool | LoopUnroll disable |
| +1400 | 71 | bool | SROA disable |
| +1440 | 73 | bool | EarlyCSE disable |
| +1760 | 89 | bool | MemorySpaceOpt disable |
| +2000 | 101 | bool | NVVMIntrinsicLowering disable |
| +2320 | 117 | bool (default=1) | NVVMRematerialization disable |
| +2440 | 123 | bool | NVVMSinking2 disable |
| +2600 | 131 | bool | NVVMIRVerification disable |
| +2840 | 141 | bool (default=1) | ADCE enable (reversed logic) |
| +2880 | 143 | bool (default=1) | LICM enable (reversed logic) |
| +3120 | 155 | bool (default=1) | MemorySpaceOpt (2nd pass) enable |
| +3160 | 157 | bool (default=1) | PrintModulePass/debug dump enable |
| +3200 | 159 | bool (default=1) | Advanced NVIDIA passes group enable |
| +3328 | 165 | bool (default=1) | SM-specific late passes enable |
| +3488 | 175 | bool | Barrier optimization enable |
| +3648 | 181 | ptr | Language string (`"ptx"`/`"mid"`/`"idn"`) |
| +3656 | — | int | Language string length |
| +3704 | 185 | bool | Late optimization / address-space flag |
| +4064 | 201 | bool | Concurrent compilation enable |
| +4104 | 203 | int (default=-1) | Thread count |
| +4224 | 211 | bool (default=1) | Master optimization enable |
| +4304 | 213 | bool | Device-code / separate-compilation flag |
| +4384 | 217 | bool | Fast-compile bypass (skip LLVM pipeline) |
| +4464 | 219 | bool (default=1) | Late CFG cleanup guard |

## Infrastructure Functions

| Address | Function | Role |
|---|---|---|
| `0x2342890` | `sub_2342890` | Master pass registration (~2,816 lines) |
| `0xE41FB0` | `sub_E41FB0` | `StringMap::insert` (48-byte entries, open-addressing) |
| `0xE41C70` | `sub_E41C70` | `StringMap::grow` (hash table resize) |
| `0xC94890` | `sub_C94890` | String hash function (DJB/FNV-family) |
| `0x9691B0` | `sub_9691B0` | String equality (`len + memcmp`) |
| `0xC931B0` | `sub_C931B0` | `StringRef::find_first_of` (delimiter search) |
| `0x95CB50` | `sub_95CB50` | `StringRef::consume_front` (strip `llvm::` prefix) |
| `0x233C410` | `sub_233C410` | Help listing (`--print-pipeline-passes`) |
| `0x233BD40` | `sub_233BD40` | AA name resolver (chain of comparisons) |
| `0x233C0C0` | `sub_233C0C0` | AA pipeline parser |
| `0x233C300` | `sub_233C300` | Extension callback dispatch |
| `0x233A120` | `sub_233A120` | Generic parameterized option parser |
| `0x12E54A0` | `sub_12E54A0` | Master pipeline assembler (49.8KB) |
| `0x12DE0B0` | `sub_12DE0B0` | AddPass (hash-table-based insertion) |
| `0x12DE330` | `sub_12DE330` | Tier 0 full optimization sub-pipeline |
| `0x12DE8F0` | `sub_12DE8F0` | Tier 1/2/3 phase-specific sub-pipeline |
| `0x12DFE00` | `sub_12DFE00` | Codegen dispatch (dependency-ordered) |
| `0x226C400` | `sub_226C400` | Pipeline name selector (nvopt\<O#\>) |
| `0x2277440` | `sub_2277440` | Pipeline text parser entry |
| `0x225D540` | `sub_225D540` | New PM nvopt registration |
| `0x12C35D0` | `sub_12C35D0` | Legacy PM pipeline orchestrator |
| `0x2342820` | `sub_2342820` | `LastRunTrackingAnalysis` factory |
| `0x2342830` | `sub_2342830` | `PassInstrumentationAnalysis` factory |
| `0x2342840` | `sub_2342840` | `VerifierAnalysis` factory |
| `0x2342850` | `sub_2342850` | `InlinerWrapper` factory (shared by 4 inliner variants) |
| `0x2342860` | `sub_2342860` | `InvalidateAllAnalysesPass` factory |
| `0x2342870` | `sub_2342870` | `VerifierPass` factory |
| `0x2342880` | `sub_2342880` | `GuardWideningPass` factory |
| `0x2339850` | `sub_2339850` | `PassBuilder` destructor |
| `0x233B610` | `sub_233B610` | `PassBuilder::~PassBuilder` cleanup |

## Cross-References

- [Optimizer](../pipeline/optimizer.md) — runtime pipeline assembly, two-phase model, concurrent compilation
- [NVVMPassOptions](../config/nvvm-pass-options.md) — 221-slot option struct controlling pass enablement
- [Optimization Levels](../config/optimization-levels.md) — O0/O1/O2/O3 and Ofcmin/Ofcmid/Ofcmax
- [Concurrent Compilation](../infra/concurrent-compilation.md) — Phase I/II, thread pool, GNU Jobserver
