# Pipeline & Pass Ordering

CICC v13.0 implements the LLVM New Pass Manager pipeline infrastructure, with NVIDIA injecting 33 custom passes into the registration table alongside approximately 493 standard LLVM passes. The master registration function at `sub_2342890` populates a `StringMap<PassInfo>` hash table with every known pass name at startup, and a text-based pipeline parser allows the full pass ordering to be specified as a parenthesized string (e.g., `module(function(instcombine,dse))`). This page documents the complete pass inventory, the registration mechanism, and the NVIDIA-specific additions.

| | |
|---|---|
| **Master registration** | `sub_2342890` (`0x2342890`, ~2,816 lines) |
| **Hash table insert** | `sub_E41FB0` (`0xE41FB0`) -- open-addressing, 48-byte entries |
| **String equality** | `sub_9691B0` (`0x9691B0`) -- `len==len && memcmp==0` |
| **AA name resolver** | `sub_233BD40` (`0x233BD40`) -- chain of string comparisons |
| **AA pipeline parser** | `sub_233C0C0` (`0x233C0C0`) -- splits on `,`, special-cases `"default"` |
| **Extension callback** | `sub_233C300` (`0x233C300`) -- iterates `[PassBuilder+2208]`, stride 32 |
| **Option parser** | `sub_233A120` (`0x233A120`) -- splits on `;`, validates tokens |
| **Help/listing** | `sub_233C410` (`0x233C410`) -- `--print-pipeline-passes` handler |
| **Total passes** | ~526 unique registrations |
| **NVIDIA additions** | 33 passes (12 module, 20 function, 1 loop) |

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

```
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

## Complete Pass Inventory

The following tables list every pass in exact registration order within `sub_2342890`. NVIDIA-specific passes are marked with bold names.

### Module Analyses (18)

| # | Pass Name | LLVM Class |
|---|---|---|
| 1 | `callgraph` | `CallGraphAnalysis` |
| 2 | `collector-metadata` | `CollectorMetadataAnalysis` |
| 3 | `ctx-prof-analysis` | `CtxProfAnalysis` |
| 4 | `dxil-metadata` | `DXILMetadataAnalysis` |
| 5 | `dxil-resource-binding` | `DXILResourceBindingAnalysis` |
| 6 | `dxil-resource-type` | `DXILResourceTypeAnalysis` |
| 7 | `inline-advisor` | `InlineAdvisorAnalysis` |
| 8 | `ir-similarity` | `IRSimilarityAnalysis` |
| 9 | `last-run-tracking` | via `sub_2342820` |
| 10 | `lcg` | `LazyCallGraphAnalysis` |
| 11 | `module-summary` | `ModuleSummaryIndexAnalysis` |
| 12 | `no-op-module` | `NoOpModuleAnalysis` |
| 13 | `pass-instrumentation` | via `sub_2342830` |
| 14 | `profile-summary` | `ProfileSummaryAnalysis` |
| 15 | `reg-usage` | `PhysicalRegisterUsageAnalysis` |
| 16 | `stack-safety` | `StackSafetyGlobalAnalysis` |
| 17 | `verify` | via `sub_2342840` |
| 18 | `globals-aa` | `GlobalsAA` |

### Module Passes (selected, ~131 total)

The full module pass list includes 121 standard LLVM passes (always-inline, attributor, constmerge, deadargelim, globalopt, inliner-wrapper, openmp-opt, sample-profile, strip, wholeprogramdevirt, etc.) plus 12 NVIDIA-specific passes:

| Pass Name | LLVM Class | Purpose |
|---|---|---|
| **`check-gep-index`** | `CheckGepIndexPass` | Validates GEP index bounds |
| **`check-kernel-functions`** | `NVPTXSetFunctionLinkagesPass` | Enforces kernel linkage |
| **`cnp-launch-check`** | `CNPLaunchCheckPass` | Cooperative launch validation |
| **`ipmsp`** | `IPMSPPass` | Inter-procedural memory space propagation |
| **`nv-early-inliner`** | via `sub_2342850` | NVIDIA early inlining heuristic |
| **`nv-inline-must`** | `InlineMustPass` | Force-inlines `__forceinline__` functions |
| **`nvvm-pretreat`** | `PretreatPass` | IR canonicalization before optimization |
| **`nvvm-verify`** | `NVVMIRVerifierPass` | NVVM IR constraint validation |
| **`printf-lowering`** | `PrintfLoweringPass` | Lowers printf to vprintf ABI |
| **`select-kernels`** | `SelectKernelsPass` | Selects kernels for compilation |
| **`lower-ops`** | `LowerOpsPass` | Lowers operations (param: `enable-optimization`) |
| **`set-global-array-alignment`** | `SetGlobalArrayAlignmentPass` | Alignment control (param: `modify-shared-mem`) |

### Function Analyses (39)

Standard LLVM function analyses include `aa`, `assumptions`, `block-freq`, `branch-prob`, `domtree`, `loops`, `memdep`, `memoryssa`, `scalar-evolution`, `target-ir`, `target-lib-info`, `uniformity`, plus 26 others. NVIDIA adds two:

| Pass Name | LLVM Class |
|---|---|
| **`rpa`** | `RegisterPressureAnalysis` |
| **`merge-sets`** | `MergeSetsAnalysis` |

### Function Passes (selected, ~219 total)

The function pass inventory is the largest category. Standard LLVM passes include adce, bdce, dce, dse, early-cse, gvn, instcombine, jump-threading, licm, loop-vectorize, mem2reg, memcpyopt, reassociate, sccp, simplifycfg, slp-vectorizer, sroa, tailcallelim, and approximately 170 others including all printers and verifiers.

NVIDIA adds 20 function-level passes:

| Pass Name | LLVM Class | Purpose |
|---|---|---|
| **`basic-dbe`** | `BasicDeadBarrierEliminationPass` | Removes dead `bar.sync` instructions |
| **`branch-dist`** | `BranchDistPass` | Branch distribution for divergence control |
| **`byval-mem2reg`** | `ByValMem2RegPass` | Promotes byval arguments to registers |
| **`bypass-slow-division`** | `BypassSlowDivisionPass` | Fast-path for small-operand division |
| **`normalize-gep`** | `NormalizeGepPass` | GEP canonicalization for address arithmetic |
| **`nvvm-reflect-pp`** | `SimplifyConstantConditionalsPass` | Folds `__nvvm_reflect` results |
| **`nvvm-peephole-optimizer`** | `NVVMPeepholeOptimizerPass` | NVVM-specific peephole rewrites |
| **`old-load-store-vectorizer`** | `OldLoadStoreVectorizerPass` | Legacy load/store vectorization |
| **`remat`** | `RematerializationPass` | Register-pressure-aware rematerialization |
| **`propagate-alignment`** | `PropagateAlignmentPass` | Propagates alignment through pointer chains |
| **`reuse-local-memory`** | `ReuseLocalMemoryPass` | Shares local memory across kernels |
| **`set-local-array-alignment`** | `SetLocalArrayAlignmentPass` | Aligns stack arrays for coalescing |
| **`sinking2`** | `Sinking2Pass` | Enhanced instruction sinking |
| **`d2ir-scalarizer`** | `ScalarizerPass` (NVIDIA alias) | NVIDIA-branded scalarization |
| **`sink<rp-aware>`** | `SinkingPass` (variant) | Register-pressure-aware sinking |
| **`memory-space-opt`** | `MemorySpaceOptPass` | Generic-to-specific address space promotion |
| **`lower-aggr-copies`** | `LowerAggrCopiesPass` | Lowers aggregate copies to loops |
| **`lower-struct-args`** | `LowerStructArgsPass` | Lowers struct-typed arguments |
| **`process-restrict`** | `ProcessRestrictPass` | `__restrict__` pointer analysis |
| **`bounds-checking`** | `BoundsCheckingPass` (modified) | NVIDIA-modified bounds checker |

### Loop Passes (selected, ~26 total)

Standard loop passes include canon-freeze, indvars, licm, loop-deletion, loop-idiom, loop-reduce, loop-rotate, loop-unroll-full, simple-loop-unswitch, and others. NVIDIA adds one:

| Pass Name | LLVM Class |
|---|---|
| **`loop-index-split`** | `LoopIndexSplitPass` |

### Machine Function Passes (~50 total)

Machine-level passes are entirely standard LLVM: dead-mi-elimination, machine-cp, machine-cse, machine-scheduler, machinelicm, peephole-opt, phi-node-elimination, post-RA-sched, register-coalescer, stack-coloring, two-address-instruction, and approximately 40 others. The last two registered passes are `regallocfast` and `greedy` (the greedy register allocator).

No NVIDIA-specific machine function passes were identified in the registration table; NVIDIA's machine-level customizations are implemented through target hooks in the NVPTX backend rather than as separately registered passes.

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
| NVIDIA additions | 33 |
| Standard LLVM | ~493 |

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
| `0x2342820` | `sub_2342820` | `LastRunTrackingAnalysis` factory |
| `0x2342830` | `sub_2342830` | `PassInstrumentationAnalysis` factory |
| `0x2342840` | `sub_2342840` | `VerifierAnalysis` factory |
| `0x2342850` | `sub_2342850` | `InlinerWrapper` factory (shared by 4 inliner variants) |
| `0x2342860` | `sub_2342860` | `InvalidateAllAnalysesPass` factory |
| `0x2342870` | `sub_2342870` | `VerifierPass` factory |
| `0x2342880` | `sub_2342880` | `GuardWideningPass` factory |
| `0x2339850` | `sub_2339850` | `PassBuilder` destructor |
| `0x233B610` | `sub_233B610` | `PassBuilder::~PassBuilder` cleanup |
