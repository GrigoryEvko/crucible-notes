# hlo2penguin MLIR Pipeline Order & Entry Flow

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22` (cp310), binary `neuronxcc/starfish/bin/hlo2penguin`. Offsets: `.rodata = VA − 0x200000`, `.text = VA − 0x201000`. Other versions will differ.*

## Abstract

This is the chapter opener for the **C-strand**: the MLIR pass pipeline that turns an *already-optimized* `xla::HloModule` into a textual Penguin Python module. Everything in Part 4.1–4.32 is the **B-strand** — passes that run on the `xla::HloModule` (the "112 HLO passes", driven through the `--passes` registry of [4.1](pass-registry.md)). The C-strand is what happens *after* that: XLA's optimized HLO is imported into the `mhlo` MLIR dialect, then a fixed sequence of ~32 MLIR passes rewrites, schedules, and legalizes it before a terminal pass walks the IR and emits Python. The output of this pipeline is `penguin.py`, the input to [Part 5](../penguin/) — there is **no** Penguin C++ dialect; "Penguin IR" is the textual Python the final pass prints.

The single most important finding here corrects a long-standing gap. Earlier analysis (S2-03) concluded the MLIR pass order "is built from a textual pipeline string parsed at runtime via `parsePassPipeline`" and "not recoverable from this binary alone." **That is false.** The pipeline is built **imperatively in C++**: a flat sequence of `OpPassManager::addPass(...)` / `addNestedPass<func::FuncOp>(...)` calls inside two functions — `hilo::registerMHLOPasses` @0x1ee12f0 and `hilo::registerStableHLOPasses` @0x1ee2120. There is no pipeline-spec literal, no comma-joined token list, and `mlir::parsePassPipeline` (statically linked) is never called on the Neuron compile path. Earlier analysis stopped one indirect tail-call short — at the 370-byte dispatcher `registerMLIRCompilePasses` @0x1ef12a0, which only logs and branches — and so never saw the `addPass` spine.

The pipeline forks into two near-identical paths — MHLO and StableHLO — selected by a single CompileConfig byte. Each pass in the MHLO path has a StableHLO twin at the same ordinal and gate; the StableHLO path inserts five extra bridge/legalize passes (`HloLegalizeToStablehlo`, `ConvertCustomCallToAllReduce`, `FusionToComposite`, `LegalizeSRA`, `LegalizeScatter`). The per-pass enable knobs are CompileConfig bytes populated from `cl::opt` globals; the *order* is hard-coded.

For reimplementation, the contract is:

- **The entry flow** `HiloCompileInternal → ConvertHloToMLIR → MLIRCompile → register{MHLO,StableHLO}Passes → PassManager::run`, including where HLO optimization vs. MLIR conversion vs. MLIR optimization happen.
- **The exact ordered `addPass` sequence** of the 32-step MHLO pipeline and the 5 StableHLO-only insertions, each with its CompileConfig gate byte.
- **The MHLO↔StableHLO fork** mechanism (CompileConfig `[+0x1CA]`) and the twin-pass naming convention.
- **The five misc leaf passes** owned by this page (`LegalizeAlias`, `LegalizeScatter`, `LegalizeSRA`, `ConvertFSPatternToCustomCalls`, `FoldIota`) — their match/action and exact factory/runOnOperation addresses.
- **The namespace/class census** of the Neuron MLIR passes — which is *not* one namespace but three.

| | |
|---|---|
| **Compile body** | `hilo::HiloCompileInternal` @0x1efc4d0 |
| **HLO→MHLO import** | `hilo::ConvertHloToMLIR` @0x1efc1c0 (627 B) |
| **MLIR PM driver** | `hilo::MLIRCompile` @0x1ef1430 (317 B) |
| **Pipeline dispatcher** | `hilo::registerMLIRCompilePasses` @0x1ef12a0 (370 B) |
| **MHLO ordered spine** | `hilo::registerMHLOPasses` @0x1ee12f0 (3096 B) — 32 passes |
| **StableHLO ordered spine** | `hilo::registerStableHLOPasses` @0x1ee2120 (3142 B) — 37 passes |
| **Path selector** | CompileConfig byte `[+0x1CA]` (≠0 ⇒ StableHLO) |
| **IR levels** | `xla::HloModule` → `mhlo`/`stablehlo` dialect ModuleOp → Penguin `.py` text |
| **Eager dialects** | exactly 3, via `hilo::initializeMLIRContext` @0x1ee0500: `mhlo` → `stablehlo` → `func` |
| **Terminal pass** | `mlir::hlo::createMhloToPyPenguinPass` @0x20aa940 / `…StableHLOToPyPenguinPass` @0x214a340 |

---

## The B-strand → C-strand Handoff

The B-strand and C-strand share the same `hilo/` source tree but live in **two different C++ namespaces operating on two different IRs**. B-strand passes are `xla::HloModulePass` subclasses in `xla::hilo::` running on the `xla::HloModule`; C-strand passes are `mlir::PassWrapper<…>` subclasses (in three namespaces — see [§Namespace Census](#hlo2penguin-namespace--class-census)) running on an MLIR ModuleOp. The hinge between them is `ConvertHloToMLIR`.

```text
xla::HloModule  ──(B-strand: hilo::RunHloPasses, the 112 HLO passes, §4.1 registry)──►  optimized xla::HloModule
       │
       │  hilo::ConvertHloToMLIR @0x1efc1c0
       ▼
  xla::ConvertHloToMlirHlo  ──────────────────────────────────────────────────────►  mhlo dialect ModuleOp
       │
       │  hilo::MLIRCompile @0x1ef1430  →  registerMHLO/StableHLOPasses (THIS PAGE)
       ▼
  ~32-step MLIR addPass spine  ──(terminal Mhlo/StableHLOToPyPenguin)──►  penguin.py  (Part 5 input)
```

> **NOTE —** HLO optimization is already finished before any MLIR exists. `ConvertHloToMLIR` first calls `hilo::RunHloPasses` @0x1efa160 (the 8248-byte B-strand driver), *then* imports the result. The MLIR pipeline on this page never re-runs HLO passes; it operates exclusively on the imported `mhlo`/`stablehlo` IR. The "two collective coalescers" question (HLO-side combiners in [4.5](collective-combiners.md) vs. MLIR-side `CoalesceCollectiveOps` at step 11 here) resolves this way: they are *two distinct stages*, the second running on the dialect IR after import, dominance-scheduling based rather than key-bucketing. See [§CoalesceCollectiveOps note](#coalescecollectiveops-step-11).

---

## Entry Flow

### Purpose

Three functions wrap the pipeline: `ConvertHloToMLIR` produces the IR, `MLIRCompile` builds and runs the PassManager, and `registerMLIRCompilePasses` selects which of the two ordered spines to install. None of these three *contains* the order — they delegate to `registerMHLOPasses`/`registerStableHLOPasses`.

### Entry Point

```text
hilo::HiloCompileInternal @0x1efc4d0          ── non-partitioned MLIR compile body (FrontendDriver.cc)
  ├─ hilo::ConvertHloToMLIR @0x1efc1c0  (627 B) ── make the mhlo ModuleOp
  │    ├─ hilo::RunHloPasses @0x1efa160 (8248 B) ── the 112 B-strand HLO passes (HLO opt happens HERE)
  │    ├─ mlir::UnknownLoc::get + mlir::ModuleOp::create ── empty destination ModuleOp
  │    └─ xla::ConvertHloToMlirHlo(module, hlo, b1, b2, b3) ── UPSTREAM XLA importer: HloModule → mhlo
  │         └─ on failure: NEURON_LOG(ERROR) NCC_DRV001, "The conversion from HLO to MHLO failed, status: " @0x2f6400
  └─ hilo::MLIRCompile @0x1ef1430  (317 B)      ── build + run the PassManager
       ├─ mlir::PassManager::PassManager(ctx, anchor, nesting)
       ├─ mlir::applyPassManagerCLOptions(pm)                       ── --mlir-* CL options
       ├─ hilo::HiloInstrumentation::applyMLIRInstrumentationOptions ── printer/timing/verifier (14 lambdas)
       ├─ hilo::registerMLIRCompilePasses @0x1ef12a0 (370 B)        ── BUILD THE PIPELINE  ◄── order source
       │    └─ branch on CompileConfig [+0x1CA]:  registerMHLOPasses | registerStableHLOPasses
       └─ mlir::PassManager::run(op)
```

### Algorithm

```c
// hilo::ConvertHloToMLIR @0x1efc1c0
function ConvertHloToMLIR(hlo_module, ctx, cfg, modulePaths, outFile):
    RunHloPasses(hlo_module, cfg, outFile);            // @0x1efa160 — B-strand 112 HLO passes
    module = ModuleOp::create(UnknownLoc::get(ctx));   // empty mhlo destination
    status = xla::ConvertHloToMlirHlo(module, hlo_module, b1, b2, b3);  // upstream importer → mhlo
    if (!status.ok()):
        NEURON_LOG(ERROR, "NCC_DRV001",                // @0x1efc3df
                   "The conversion from HLO to MHLO failed, status: ");  // string @0x2f6400
        return error;
    return module;                                     // an mhlo-dialect ModuleOp; NO MLIR passes added here

// hilo::registerMLIRCompilePasses @0x1ef12a0 — the 370-byte DISPATCHER (adds NO passes)
function registerMLIRCompilePasses(pm, module, modulePaths, cfg, outFile):
    pm.enableVerifier(false);                          // esi = 0
    if (cfg[0x1CA] != 0):                              // "use StableHLO lowering" flag
        log("[INFO] StableHLO Lowering Enabled");      // string @0x2b1878
        return registerStableHLOPasses(pm, module, cfg, modulePaths, outFile);  // TAIL-CALL @0x1ee2120
    else:
        log("[INFO] MHLO Lowering Enabled");           // string @0x255d80
        return registerMHLOPasses(pm, module, cfg, modulePaths, outFile);       // TAIL-CALL @0x1ee12f0
```

> **CORRECTION (S2-03) —** `registerMLIRCompilePasses` does **not** build the order. Its 5 callees are all logging/verifier; it only sets `enableVerifier(false)`, logs `MHLO`/`StableHLO`, and tail-calls the real builder. Earlier analysis inspected only this stub and inferred a runtime-textual `parsePassPipeline` mechanism. The `addPass` spine is one indirect call deeper. The two `…Lowering Enabled` strings (@0x255d80 / @0x2b1878) are the on-disk fingerprint of the fork.

> **QUIRK —** the trailing `ArrayRef<std::string> modulePaths` and `std::string outFile` threaded through the dispatcher are **not** a pipeline spec. They are the import-module list and output `.py` path consumed only by the *terminal* `createMhloToPyPenguinPass(StringRef dir, StringRef file, ArrayRef<string> modules, MhloToPyPenguinPassConfig const&, bool)` (signature CONFIRMED at the mangled symbol of @0x20aa940). A reimplementer who treats them as a pass list will misread the whole entry flow.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `hilo::HiloCompileInternal` | 0x1efc4d0 | — | non-partitioned MLIR compile body | CERTAIN |
| `hilo::ConvertHloToMLIR` | 0x1efc1c0 | 627 | RunHloPasses + xla::ConvertHloToMlirHlo; NCC_DRV001 on fail | CERTAIN |
| `hilo::RunHloPasses` | 0x1efa160 | 8248 | the 112 B-strand HLO passes | HIGH |
| `xla::ConvertHloToMlirHlo` | (upstream) | — | XLA HLO→mhlo importer | CERTAIN |
| `hilo::MLIRCompile` | 0x1ef1430 | 317 | build PM, applyCLOptions, instrumentation, register…, pm.run | CERTAIN |
| `hilo::registerMLIRCompilePasses` | 0x1ef12a0 | 370 | log + branch on cfg[0x1CA]; tail-call the builder | CERTAIN |
| `hilo::registerMHLOPasses` | 0x1ee12f0 | 3096 | the MHLO ordered pipeline | CERTAIN |
| `hilo::registerStableHLOPasses` | 0x1ee2120 | 3142 | the StableHLO ordered pipeline | CERTAIN |
| `hilo::initializeMLIRContext` | 0x1ee0500 | — | eager-load mhlo→stablehlo→func | CONFIRMED |
| `GetHiloCompileConfig` | 0x1ed7bd0 | 1454 | cl::opt globals → CompileConfig gate bytes | HIGH |

All entry-flow symbols verified by demangled signature in `names.json` (e.g. `_ZN4hilo16ConvertHloToMLIRE…`, `_ZN4hilo23registerStableHLOPassesE…`).

---

## The Ordered MHLO Pipeline

### Purpose

`registerMHLOPasses` @0x1ee12f0 is the canonical order. It is a flat `addPass`/`addNestedPass` spine: most passes fall through inline; the conditionally-enabled ones are laid out by IDA as out-of-line blocks that `jmp` back to the next gate, so the *true* linear order interleaves them at their gate positions. The reconstruction below restores that linear order.

### Gate convention

Every gate is `cmp byte ptr [cfg+OFF], 0` on a CompileConfig boolean. Both gate forms — `jnz loc_FAR` (add-pass-in-altblock) and `jz loc_SKIP` (skip-inline-pass) — mean the same thing: **cfg byte NONZERO ⇒ pass ENABLED**. Gate bytes are populated by `GetHiloCompileConfig` @0x1ed7bd0, which copies ~73 `cl::opt` boolean globals (`movzx eax, cs:<global>; mov [cfg+OFF], al`) into the config; the order is independent of these bytes.

### Algorithm — the 32-step spine

```c
// hilo::registerMHLOPasses @0x1ee12f0 — linear order (gate ⇒ enabled-when-cfg-byte-nonzero)
function registerMHLOPasses(pm, module, cfg, modulePaths, outFile):
    if cfg[0x119]: pm.addPass(createCanonicalizerPass(greedyCfg, disabledPatterns));   // 1  transpose/bool-splat cleanup
    if cfg[0x200]: pm.addPass(createNeuronControlDepTupleSimplifierPass());            // 2  flatten AwsNeuronControlDep tuples
                   pm.addPass(createStableHLOCollectiveBroadcastToAllGatherPass());    // 3  collective_broadcast → all_gather
    if cfg[0x136]: pm.addPass(createVerifySupportedOps());                             // 4  op legality
    if cfg[0x12F]: pm.nest<func>().addPass(createCanonicalizeConvPass());              // 5  conv pad/stride normalize
    if cfg[0x11A]: pm.nest<func>().addPass(mhlo::createLowerComplexPass());            // 6  complex → real (default-on, see note)
    if cfg[0x11B]: pm.nest<func>().addPass(mhlo::createLowerComplexExtraPass());       // 7  extra complex lowering
    if cfg[0x11C]: pm.addPass(createRemoveOptimizationBarriersPass());                 // 8  strip optimization_barrier
    if cfg[0x116]: pm.addPass(createIdentifyCrossPassTensorsPass());                   // 9  tag neuron.CrossPassTensor
                   pm.addPass(createRematerializePass());                              // 10 recompute-to-save-memory (same gate as 9)
    if cfg[0x117]: pm.addPass(createCoalesceCollectiveOpsPass());                      // 11 MLIR-level collective coalescing
    if cfg[0x11F]: pm.nest<func>().addPass(createConvertFSPatternToCustomCallsPass()); // 12 FS subgraph → AwsFSPattern5 custom-call
                   pm.addPass(createRemoveDeadValuesPass());                           // 13 upstream MLIR DCE
                   pm.addPass(createNeuronInstCombinePass());                          // 14 reduce-slice-reduce peephole
    if cfg[0x120]: pm.nest<func>().addPass(createNeuronOpFusionPass());                // 15 6 fusion sub-passes
    if cfg[0x118]: pm.nest<func>().addPass(createFoldIotaPass());                      // 16 pow(iota·scale) → constant
    if cfg[0x130]: pm.nest<func>().addPass(createSchedulePostorderPass());             // 17 postorder serialize + realize
    if cfg[0x134]: pm.nest<func>().addPass(createAnalyzeSchedulePass());               // 18 live-range analysis
                   pm.addPass(createReplaceTokenTypeWithU8Pass());                     // 19 !mhlo.token → tensor<ui8>
                   pm.nest<func>().addPass(createScatterMotionPass());                 // 20 scatter code motion
    if cfg[0x135]: pm.nest<func>().addPass(createScheduleFusionPass());                // 21 comm/compute overlap
    if cfg[0xE9]:  pm.nest<func>().addPass(createHoistComputePass());                  // 22 hoist compute
    if cfg[0xE5]:  pm.nest("func.func").addPass(createMemcastMotionPass());            // 23 late dtype-cast motion
                   pm.addPass(createPruneFunctionsPass());                             // 24 dead-function elimination
    if (qword_9C71338 == 0): prepend createSixtyFourHackPass();                        // 25 i64→i32 emulation (prepended before 26)
                   pm.addPass(createPenguinizeIOPass());                               // 26 Penguin IO-boundary packaging
                   pm.addPass(createCanonicalizeForTensorizerPass());                  // 27 8 sub-rewriters
                   pm.addPass(createTensorizerLegalizationPass());                     // 28 output no-aliasing
                   pm.addPass(createLegalizeAliasPass());                              // 29 insert AliasingCopy
    if cfg[0x13C]: pm.addPass(createVerifyAliasingPass());                             // 30 RAW alias check
                   pm.addPass(createMLIRInstructionHistogram());                       // 31 op-count diagnostics
                   pm.addPass(mlir::hlo::createMhloToPyPenguinPass(dir,file,modules,   // 32 TERMINAL Python emitter
                                                                   penguinCfg, b5));
```

> **NOTE —** step 6 (`LowerComplexPass`) is gated by `cfg[0x11A]` whose source global `qword_9C690D8` is *also set to 1* by a static initializer @0x1ed3606 — i.e. complex lowering is default-on. Most other gates default off (the `cl::opt` flag must be passed to flip them).

> **GOTCHA —** step 25 `SixtyFourHack` is **prepended**, not appended. Its gate (`jz`→altblk @0x1ee19a0) checks the global `qword_9C71338`; when that global is **zero** the pass is inserted *before* step 26 (`PenguinizeIO`), so it runs ahead of IO packaging despite appearing late in the source. A reimplementer who simply `addPass`-es it in textual position will sequence it wrong.

### CompileConfig gate-byte map (the ~17 pipeline gates)

`GetHiloCompileConfig` @0x1ed7bd0 supplies these gate bytes (CERTAIN load→store pairs). The human-readable `--flag` spelling per global is a Strand-U follow-up; the config offset and source global are CONFIRMED.

| cfg byte | source `cl::opt` global | gates step(s) |
|---|---|---|
| `+0xE5` | `qword_9C69C18` | 23 MemcastMotion |
| `+0xE9` | `qword_9C699D8` | 22 HoistCompute |
| `+0x116` | `qword_9C69558` | 9–10 IdentifyCrossPassTensors + Rematerialize |
| `+0x117` | `qword_9C693D8` | 11 CoalesceCollectiveOps |
| `+0x118` | `byte_9C69258` | 16 FoldIota |
| `+0x119` | `byte_9C69198` | 1 Canonicalizer (transpose) |
| `+0x11A` | `qword_9C690D8` | 6 LowerComplex (default-on @0x1ed3606) |
| `+0x11B` | `byte_9C69018` | 7 LowerComplexExtra |
| `+0x11C` | `byte_9C69498` | 8 RemoveOptimizationBarriers |
| `+0x11F` | `qword_9C67898` | 12 ConvertFSPatternToCustomCalls |
| `+0x120` | `qword_9C69B58` | 15 NeuronOpFusion |
| `+0x12F` / `+0x130` | (read @0x1ed7df9) | 5 CanonicalizeConv / 17 SchedulePostorder |
| `+0x134` | (config bool) | 18 AnalyzeSchedule |
| `+0x135` | (config bool) | 21 ScheduleFusion |
| `+0x136` | (config bool) | 4 VerifySupportedOps |
| `+0x13C` | (loaded @0x1ed754d) | 30 VerifyAliasing |
| `+0x200` | (config bool) | 2 NeuronControlDepTupleSimplifier |
| `+0x1CA` | (config bool) | MHLO-vs-StableHLO selector (in dispatcher) |
| global `qword_9C71338` | (cl::opt byte) | 25 SixtyFourHack prepend (==0 ⇒ enabled) |

### Terminal-pass argument assembly

The terminal `MhloToPyPenguin` pass is the only one that consumes `modulePaths`/`outFile`. Its arguments are packed @0x1ee152e–0x1ee18b0: `dir`/`file` strings are assembled from CompileConfig fields (`[cfg+0x20]`/`[cfg+0x28]` and `[cfg]`/`[cfg+8]`) joined with `"."` (@0x282e77); the `modulePaths` ArrayRef is passed in `r14`; a trailing `bool` = `!cfg[0xE4]`; and a `MhloToPyPenguinPassConfig` is packed from `cfg[0x174]/0x178/0x180/0x1C9` plus a vtable call `[cfg+0x1D0]→+0x30`. The pass signature `(StringRef, StringRef, ArrayRef<string>, MhloToPyPenguinPassConfig const&, bool)` is CONFIRMED against the mangled symbol at @0x20aa940. Penguin emission internals are owned by [4.43–4.45](../penguin/) (the printer / PyPenguin family).

---

## The StableHLO Pipeline & the Fork

### Purpose

`registerStableHLOPasses` @0x1ee2120 is the same skeleton with StableHLO-prefixed twin passes at every ordinal and **five structural insertions**. Gate config-bytes are identical to MHLO except where noted. The fork is chosen once, at the dispatcher, by `cfg[0x1CA]`.

### The five StableHLO-only insertions

The import always arrives as `mhlo` (the XLA importer produces `mhlo`), so the StableHLO path opens by bridging to `stablehlo`, and closes with two extra legalizers absent from MHLO:

```text
 1   createCanonicalizerPass (same 3 disabledPatterns)         [+0x119]   (twin)
 1a  mhlo::createHloLegalizeToStablehloPass(...)               uncond     ◄── STABLEHLO-ONLY: mhlo → stablehlo bridge
 1b  createConvertCustomCallToAllReducePass()                  uncond     ◄── STABLEHLO-ONLY: custom_call("mhlo.all_reduce") → all_reduce
 2   createStableHLONeuronControlDepTupleSimplifierPass()      [+0x200]   (twin)
 …   (steps 3–14 are StableHLO twins at the same gates as MHLO) …
 14  createNeuronInstCombinePass()                             uncond     (shared symbol)
 14a createFusionToCompositePass()                             uncond     ◄── STABLEHLO-ONLY: mhlo.fusion → stablehlo.composite
 …   (steps 15–26 are StableHLO twins) …
 26  createStableHLOPenguinizeIOPass()                         uncond     (twin)
 26a createLegalizeSRAPass()                                   uncond     ◄── STABLEHLO-ONLY: shift_right_arithmetic signedness
 26b createLegalizeScatterPass()                               uncond     ◄── STABLEHLO-ONLY: stablehlo.scatter → F32 + canonical body
 27  createStableHLOCanonicalizeForTensorizerPass()            uncond     (twin, "canonicalize-for-penguin")
 28  createStableHLOTensorizerLegalizationPass(long)           uncond     (twin — extra `long` ctor arg, strictly richer)
 29  createStableHLOLegalizeAliasPass()                        uncond     (twin)
 30  createStableHLOVerifyAliasingPass()                       [+0x13C]   (twin)
 31  createMLIRInstructionHistogram()                          uncond     (shared symbol)
 32  mlir::hlo::createStableHLOToPyPenguinPass(...)            uncond     TERMINAL
```

> **NOTE —** `RemoveOptimizationBarriers` (MHLO step 8) has **no** StableHLO twin: a single pass body handles *both* `mhlo.optimization_barrier` and `stablehlo.optimization_barrier` (two TypeID compares), then runs an inline nested CSE. So `--remove-opt-barriers` always implicitly CSEs the touched ops. This is the only one of the seven misc passes without a twin.

> **QUIRK —** `LegalizeSRA` and `LegalizeScatter` are un-prefixed (no `StableHLO…` in the name) yet they walk `mlir::stablehlo::` ops directly and appear **only** on the StableHLO path. They are single-dialect StableHLO-input passes despite the bare names — they are not the "MHLO variant" of a twin pair. The naming model "every pass has an MHLO + StableHLO twin" is false for these two (and for `RemoveOptimizationBarriers`, which is one pass for both).

### Confidence

Both ordered spines are CERTAIN (disasm-verified gate→altblock→jmp-back reconstruction). The terminal StableHLO emitter has several trailing `cmp …; jz` near @0x2749–0x27ce that select extra emit-options (a `disable-emit-offloaded-*` family); those option bits are MED and out of scope for the order.

---

## Misc Pass Reference

These five leaf passes are owned by this chapter opener (their pipeline placement is given above; the fusion/canonicalize/tensorizer passes are owned by [4.34–4.42](pass-registry.md)). All are `mlir::PassWrapper<hilo::X, OperationPass<…>>`; the factory lives in `mlir::` and the body in `hilo::` (verified by demangled symbols).

| Pass (pass-arg) | Factory @ | runOnOperation @ | Base | Match → Action | Conf |
|---|---|---|---|---|---|
| `LegalizeAlias` (`legalize-alias`) | `createLegalizeAliasPass` 0x209c380 | 0x209d530 | `<ModuleOp>` | reads func attr `"mhlo.input_output_alias"` (@0x27286a) → per aliased `{output_index, parameter_number, kind}` pair, `getOrCreateCopy` builds a memoized `mhlo.copy` stamped `"AliasingCopy"` (@0x26285a) on the returned value, breaking false in-place aliasing for Tensorizer | HIGH |
| `LegalizeScatter` (`legalize-scatter`) | `createLegalizeScatterPass` 0x209ee10 | 0x20a1330 | `<ModuleOp>` | walks `stablehlo.scatter` → up-cast operands/results to **F32** (`TensorType::cloneWith` + `getF32Type`), regenerate the update-computation region canonically (`Convert`→{Add,Max,Min,Mul}→`Return`), **pass `ScatterDimensionNumbersAttr` through verbatim** (no reconstruction), RAUW, erase original | HIGH |
| `LegalizeSRA` (`legalize-shift-right-arithmetic`) | `createLegalizeSRAPass` 0x209e600 | 0x209e7a0 | `<ModuleOp>` | walks `stablehlo.shift_right_arithmetic`; `isInt32`/`getSignedness` inspect element type; wrap operands/result in `stablehlo.convert` to a canonical **signed** integer type so the arithmetic (sign-extending) shift is correctly typed. Op preserved (no decomposition) | HIGH |
| `ConvertFSPatternToCustomCalls` (`convert-fs-patterns-to-cc`) | `createConvertFSPatternToCustomCallsPass` 0x2093900 | 0x2094ee0 | `<func::FuncOp>` | walks `mhlo.add`, descends ~20× via `getDefiningOp` casting to `mhlo.mul` with `hasOneUse` SSA guards (`convertPattern5` @0x2093df0) → builds ONE `mhlo.custom_call` target `"AwsFSPattern5"` (@0x26e52c): 4 results, 6 operands, attr `"call_target_name"` (@0x25a1fb); `moveAfter`, erase matched ops | HIGH (mechanics); FS expansion UNRESOLVED |
| `FoldIota` (`fold-iota`) | `createFoldIotaPass` 0x2097380 | 0x2098250 | `<func::FuncOp>` | walks `mhlo.power`; matcher `pow(broadcast(const_scalar), mul(iota, broadcast(const_scalar)))` → `foldIotaToPower(shape, iota_dim, base, scale)` @0x2097b90 precomputes a `DenseElementsAttr`, builds `mhlo.constant`, RAUW, `eraseOpIfNotUsed`. Logs `"Fold iota: The Pattern is found"` (@0x354b38) | HIGH |

> **CORRECTION (S2-03 §3) —** `FoldIota` is **not** generic `mhlo.iota` constant-folding. Its root op is `mhlo.power`; iota appears only as a multiplicand inside the exponent. It folds a `pow(iota·scale)` exponential/geometric ramp into a constant. The matcher's reject diagnostics are verbatim in the binary: `"powOp arg0 is not a BroadcastInDimOp"` (@0x391a10), `"powOp arg1 is not a MulOp"` (@0x287061), `"mulOp arg0 is not a IotaOp"` (@0x24a547).

> **NOTE —** `LegalizeScatter` walks `stablehlo.scatter`, not `mhlo.scatter`, and there is **no** StableHLO-twin pass — the un-prefixed name is the only variant. Its action is F32 up-cast + canonical body regeneration, **not** a dim-numbers normalization or a multi-op decomposition: the scatter is rebuilt in place with its `ScatterDimensionNumbersAttr` threaded through unchanged.

> **GOTCHA — `FS` is an unresolved opaque label.** The pass emits only `AwsFSPattern5`; there is no `AwsFSPattern1..4` and no source string expanding "FS". The only `FS`-expansion anywhere in the binary is `IsFlashAttentionKernel` inside an *unrelated* HLO pass ([4.20](dedup-passes.md), `NeuronPreprocessKernelDuplicateRemover`) — not evidence for this pass. Flash / Fully-Sharded / Fused-Scale all remain unprovable; treat the target as an opaque numbered placeholder.

### CoalesceCollectiveOps (step 11)

`CoalesceCollectiveOps` (runOnOperation @0x20931d0) is the second, MLIR-dialect-level collective coalescer, distinct from the HLO combiners of [4.5](collective-combiners.md). The HLO combiners run on `xla::HloModule` via the stock `CombineInstructionsByKey` engine (6-tuple key, fixed 256-count / 1-GiB thresholds). `CoalesceCollectiveOps` runs *after* import, keyed primarily by `replica_groups`, and is **dominance-scheduling based**: it physically `moveBefore`/`moveAfter`s same-kind collectives (each move guarded by `DominanceInfo`/`PostDominanceInfo`) into a coalescable window, bounded by independent per-kind `cl::opt` byte caps. It short-circuits if the input is already partially coalesced (`"Not Coalescing AllGathers as we have partially coalesced AllGathers in input."` @0x348e00). It is a *second stage*, not a duplicate of the HLO combiners.

---

## hlo2penguin Namespace & Class Census

### The three namespaces (not one)

The Neuron-authored MLIR passes are **not** a single `hilo::` namespace. They span three C++ namespaces by mangling, plus a separate HLO-level namespace. This matters for a reimplementer reading symbol tables: the task-level shorthand "`hilo` = the 25 Neuron MLIR passes" conflates them.

| Namespace | vtable typeinfos | Holds | Authored |
|---|---|---|---|
| `hilo::` | 25 | the genuine pass namespace — `NeuronOpFusion`, `NeuronInstCombine`, `ScheduleFusion`, `AnalyzeSchedule`, `FoldIota`, `SixtyFourHack`, `ReplaceTokenTypeWithU8`, `VerifyAliasing`, `LegalizeAlias`, `LegalizeScatter`, `LegalizeSRA`, `ConvertFSPatternToCustomCalls`, `ConvertCustomCallToAllReducePass`, `NeuronControlDepTupleSimplifier`, `FusionToComposite`, `CollectiveBroadcastToAllGather{Pattern,Pass}` (+ StableHLO twins) | AWS Neuron |
| GLOBAL / anon-ns | ~19 | the passes mangled as bare `_ZTI<len><Name>` (anon-ns of each TU) — `PenguinizeFunctions`, `CanonicalizeForTensorizer`, `TensorizerLegalizationPass`, `CanonicalizeConv`, `MemcastMotion`, `CoalesceCollectiveOps`, `RemoveOptimizationBarriers`, `SchedulePostorder`, `IdentifyCrossPassTensors`, `Rematerialize`, `MLIRInstructionHistogram`, `PruneFunctions`, `ScatterMotion` (+ StableHLO twins) | AWS Neuron |
| `mlir::` / `mlir::hlo::` | ~4 | the terminal emitters — `mlir::hlo::MhloToPyPenguin`, `mlir::hlo::StableHLOToPyPenguin` (passes); `mlir::MhloToPythonPrinter`, `mlir::StableHLOToPythonPrinter` (printer helpers, no vtable) | AWS Neuron (into upstream ns) |
| `xla::hilo::` | 28 | the **HLO-level** (B-strand) passes — verifiers, combiners, arch tags; run on `xla::HloModule`, *not* MLIR. See [4.1](pass-registry.md) | AWS Neuron |

> **CORRECTION (census) —** the passes a casual reading attributes to `hilo::` — `CanonicalizeForTensorizer`, `TensorizerLegalization`, `PenguinizeFunctions`/`PenguinizeIO`, `CanonicalizeConv` — are actually **GLOBAL/anonymous-namespace** classes (mangled `_ZTI19PenguinizeFunctions`, `_ZTI25CanonicalizeForTensorizer`, `_ZTI16CanonicalizeConv`). They live in `hilo/MLIRPasses/Transforms/*.cc` but are emitted in each TU's anonymous namespace, so they mangle global while the fusion/instcombine/controldep authors used an explicit `namespace hilo {…}`. This is a per-file authoring inconsistency, not a semantic boundary.

### Class hierarchy

```text
mlir::Pass
  └ mlir::OperationPass<OpT>                       (OpT = mlir::ModuleOp | mlir::func::FuncOp)
      └ mlir::PassWrapper<hilo::X, mlir::OperationPass<OpT>>   ← every hilo:: / global-ns pass
          └ X                                       (runOnOperation override; getArgument → CLI token)
mlir::RewritePattern
  └ mlir::OpRewritePattern<TargetOp>               (INFERRED base for the 2 patterns)
      └ hilo::SimplifyReduceSliceReducePattern      (on mhlo.ReduceOp)
      └ hilo::CollectiveBroadcastToAllGatherPattern (on {mhlo,stablehlo}.CollectiveBroadcastOp)
```

The "hilo" prefix = **HIgh-Level Optimizer** (src tree `hilo/`); it is a **pass** namespace, never a dialect. There is no `hilo::*Dialect`, `neuron::*Dialect`, `penguin::*Dialect` (CONFIRMED negative). `initializeMLIRContext` @0x1ee0500 eager-loads exactly **3** dialects in order: `mhlo` → `stablehlo` → `func`; everything else is transitive or the linked-but-dead XLA-GPU/CPU cluster. The de-facto Neuron op set is **data on upstream `custom_call` ops** — 26 `AwsNeuron*` call-target strings plus `FusionKind`/`CompositeKind` attrs and 8 `neuron.*` attrs — not C++ op classes.

> **NOTE —** the `OpRewritePattern<TargetOp>` template base of the two `hilo::` RewritePatterns is INFERRED — the pattern typeinfos exist but the exact `OpRewritePattern<>` instantiation was not pinned.

---

## Adversarial Self-Verification

Five strongest claims, re-challenged against the binary:

1. **"The order is imperative C++, not a `parsePassPipeline` string."** Re-checked: `registerMHLOPasses` @0x1ee12f0 (3096 B) and `registerStableHLOPasses` @0x1ee2120 (3142 B) exist with the demangled `register{MHLO,StableHLO}Passes` signatures in `names.json`; the two `…Lowering Enabled` strings (@0x255d80 / @0x2b1878) confirm the fork. `parsePassPipeline` overloads are linked but their callers are internal MLIR machinery. **CONFIRMED.**
2. **"Terminal pass signature is `(StringRef, StringRef, ArrayRef<string>, MhloToPyPenguinPassConfig const&, bool)`."** Re-checked against the mangled symbol at @0x20aa940 (`_ZN4mlir3hlo25createMhloToPyPenguinPassEN4llvm9StringRefES2_NS1_8ArrayRefI…EERKNS0_25MhloToPyPenguinPassConfigEb`). The five-argument shape, including the trailing `bool`, matches verbatim. **CONFIRMED.**
3. **"The five misc factories are in `mlir::` and the bodies in `hilo::`."** Re-checked: `_ZN4mlir23createLegalizeAliasPassEv` @0x209c380 vs `_ZN4hilo13LegalizeAlias14runOnOperationEv` @0x209d530; same split for Scatter/SRA/FS/Iota. **CONFIRMED.**
4. **"`SixtyFourHack` is prepended before `PenguinizeIO` when global `qword_9C71338`==0."** This rests on a single disasm `jz`→altblk @0x1ee19a0 chain (D-C22). The `createSixtyFourHackPass` @0x2114000 and `createPenguinizeIOPass` @0x2087080 factories are CONFIRMED; the *prepend semantics* are STRONG (disasm-only, control-flow MED per the NVOPEN_IDA_SKIP_DECOMPILE tag). Tagged **STRONG**, not CERTAIN.
5. **"Gate `cfg[0x11A]` (LowerComplex) is default-on via a static init @0x1ed3606."** Source global `qword_9C690D8` and the static-init store are from `GetHiloCompileConfig`/static-init disasm (HIGH); the human-readable flag name is *not* recovered. Tagged **HIGH** for the default-on behavior; the `--flag` spelling is an explicit **gap** (Strand-U).

Remaining gaps (stated, not papered over): per-`cl::opt`-global → `--flag` spelling for the ~17 gate bytes; the `MhloToPyPenguinPassConfig` field semantics; the partitioned path (`HiloPrePartitionCompile` @0x1efcb70) may register a different per-partition set; the `FS` expansion is unrecoverable from this build.

---

## Related Components

| Name | Relationship |
|---|---|
| `hilo::RunHloPasses` @0x1efa160 | the B-strand it consumes — runs the 112 HLO passes before import |
| `xla::ConvertHloToMlirHlo` | upstream importer that produces the `mhlo` ModuleOp this pipeline rewrites |
| `Mhlo/StableHLOToPyPenguin` @0x20aa940 / @0x214a340 | terminal emitters that produce `penguin.py`, the Part 5 input |
| `CoalesceCollectiveOps` @0x20931d0 | step 11 — the MLIR-level second coalescing stage vs. the HLO combiners |

## Cross-References

- [The hlo-opt Pass Registry (the `--passes` Table)](pass-registry.md) — 4.1, the B-strand registry whose output this pipeline imports; also hosts the `xla::hilo::` HLO-level passes
- [AllReduce/ReduceScatter/AllGather Combiners](collective-combiners.md) — 4.5, the HLO-side combiners contrasted with step-11 `CoalesceCollectiveOps`
- [CollectivePermute → AllGather Lowering](collectivepermute-to-allgather.md) — 4.8, the HLO analogue of step-3 `CollectiveBroadcastToAllGather`
- [Input/Output Alias Family](io-alias-family.md) — 4.17, the HLO alias config that step-29 `LegalizeAlias` materializes as `AliasingCopy`
- [Duplicate-Parameter & Kernel Dedup Passes](dedup-passes.md) — 4.20, the unrelated `IsFlashAttentionKernel` whose `FS` does *not* expand `AwsFSPattern`
- [Penguin IR & Middle-End](../penguin/) — Part 5, the `penguin.py` this pipeline emits and everything downstream of the terminal pass
