# hlo2penguin Entry & the Native cl::opt Surface

> *All addresses on this page apply to the `hlo2penguin` ELF shipped in `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, `neuronxcc/starfish/bin/hlo2penguin`, `EXEC`, x86-64, 231,644,968 bytes, entry `0x1ec0d00`). Other versions and other ABI wheels (cp311/cp312) will differ in address but not in structure.*

## Abstract

`hlo2penguin` is the AWS-Neuron **front-half** compiler tool — the native binary that turns an XLA `HloModule` into Python "Penguin" IR (`penguin.py`). It is one of the five standalone executables under `starfish/bin/`. Architecturally it is a thin LLVM-`cl`-driven wrapper over the `hilo` ("HIgh-Level-Optimizer") C++ library: a `main` that parses a command line, folds every flag into a `hilo::CompileConfig`, loads the input HLO, and dispatches to exactly one of two compile entry points — `hilo::HiloFlatCompile` (non-partitioned) or `hilo::HiloPrePartitionCompile` (partitioned). The source file is `hilo/hlo2penguin/main.cc` (string `0x282e52`); the `cl::ParseCommandLineOptions` overview banner is the bare token `"hlo2penguin\n"` (`0x27ab49`).

The entire `--flag` vocabulary of this binary is built once, at static-init time, by a single 15,650-byte constructor function (`__static_initialization_and_destruction_0.constprop.0`, `0x1ed2130`). That function constructs an `llvm::cl::OptionCategory` named `"hlo2penguin options"` (`_ZL19Hlo2PenguinCategory`, `0x9c6a720`) and **74 `llvm::cl` option objects** that each `.addCategory(Hlo2PenguinCategory)` — so `main`'s `cl::HideUnrelatedOptions(Hlo2PenguinCategory)` call strips the entire inherited XLA/LLVM `--xla_*`/`cl` corpus and leaves precisely these 74 flags in `--help`. The 74-object count is not a transcription of the help text: it is the number of distinct `cl::opt`/`cl::list` destructor registrations the static-init emits, counted directly in the disassembly (§4).

The page also nails down a framing correction that matters for anyone tracing the driver→native handoff. The tokens `--internal-hlo2tensorizer-options`, `--tensorizer-options`, and `--targets` are **not** `cl::opt` flags of this binary. A full `strings` scan of the ELF finds **zero** occurrences of `internal-hlo2tensorizer-options` and `tensorizer-options`, and `targets` only once (as an MLIR pass-arg fragment). Those three are *driver-side* option names; the native binary is fed the **expanded** per-flag tokens (`--target-instance=`, `--logical-nc-config=`, `--out-dir`, …) by the Python job `HLOToTensorizer.so`, with the user's free-form `internal_hlo2tensorizer_options` string appended verbatim onto the native `argv` (§6).

For reimplementation, the contract is:

- **The `main` skeleton** — instrumentation/MLIR-context init → category hiding → `cl::ParseCommandLineOptions` → `GetHiloCompileConfig` (fold flags into config) → logger init → input load → IR-signature log → flat-vs-partition dispatch → emit.
- **The 74-flag surface** — argStr, storage global, type, default, and `OptionHidden` status, grounded in the static-init.
- **The two compile entry points** and the single byte-flag (`partitionGraph`) that selects between them.
- **The handoff contract** — which tokens are native `cl::opt`s, which are driver-side, and which belong to the partitioner sub-grammar consumed *inside* `HiloPrePartitionCompile`.

| | |
|---|---|
| **ELF** | `neuronxcc/starfish/bin/hlo2penguin` (`EXEC`, x86-64, 231,644,968 B) |
| **ELF entry** | `0x1ec0d00` (`_start`, 38 B) → `__libc_start_main(main, …)` |
| **`main`** | `0x1edc880`, 5,522 B, 212 blocks, 209 callees |
| **Option registration** | `__static_initialization_and_destruction_0.constprop.0` @ `0x1ed2130`, 15,650 B |
| **Option category** | `_ZL19Hlo2PenguinCategory` @ `0x9c6a720` — `"hlo2penguin options"` (`0x24a283`) |
| **Option count** | **74** `cl::opt`/`cl::list` objects (55 bool, 10 string, 3 int, 2 uint, 1 long, 2 enum, 1 list\<float\>) |
| **Source string** | `hilo/hlo2penguin/main.cc` (`0x282e52`); banner `"hlo2penguin\n"` (`0x27ab49`) |
| **Compile dispatch** | `HiloFlatCompile` @ `0x1efc7d0` \| `HiloPrePartitionCompile` @ `0x1efcb70` |
| **Output IR** | Python Penguin source (`penguin.py`) — see Part 5 |

> **NOTE —** `hlo2penguin` was decompile-skipped during the IDA pass (`NVOPEN_IDA_SKIP_DECOMPILE`), so every `decompiled/*.c` for this ELF is a stub. Every native claim on this page is therefore anchored to one of: the per-function disassembly (`disasm/*.asm`), the `functions.json` callee/string-reference tables, or the `strings.json` value/address records — never to a Hex-Rays pseudo-C body.

---

## 1. Process Position & Entry

### Purpose

`hlo2penguin` sits at the boundary between the XLA/HLO world and the Neuron-internal "Penguin" middle-end. Upstream, the Python driver has already produced an `HloModule` (proto or MLIR text); downstream consumers read `penguin.py`. The tool's job is the Neuron MLIR front pipeline: optionally lower StableHLO→HLO, run the `hilo` HLO-level optimizations and partitioning, and emit Penguin IR source. It is launched as a *subprocess* by the driver job `HLOToTensorizer.so` (`runHlo2Tensorizer`, docstring "Run hlo2penguin to produce penguin.py. Previously, this was part of the Frontend job.").

### Entry Point

```text
_start (0x1ec0d00, 38 B)
  └─ lea rdi, main          ; 0x1ec0d18, RIP-relative → 0x1edc880
  └─ __libc_start_main(main, argc, argv, …)
       └─ main (0x1edc880)
            ├─ new hilo::HiloInstrumentation()        ; 0x1edc8ae operator new + ctor
            ├─ xla::hilo::InitHloPassOptions()         ; 0x1edc8c5
            ├─ mlir::registerPassManagerCLOptions()    ; 0x1edc8ca
            ├─ MLIRContext ctx; hilo::initializeMLIRContext(ctx)  ; 0x1edc8db / 0x1edc8e7
            ├─ cl::HideUnrelatedOptions(Hlo2PenguinCategory, …)   ; 0x1edc8f9
            ├─ cl::ParseCommandLineOptions(argc, argv, "hlo2penguin\n", …)  ; 0x1edc91a
            ├─ GetHiloCompileConfig(move(inst)) → cfg  ; 0x1edc948
            ├─ NeuronLogger init / verbosity wiring     ; 0x1edc959…0x1edca5c
            ├─ <input load>                             ; 0x1edceaf (proto) / parse path
            ├─ NEURON_LOG("IR signature: " + sha256(m)) ; 0x1edcf2f
            └─ HiloFlatCompile | HiloPrePartitionCompile ; 0x1edd16f / 0x1edd6d8
```

`_start` is the textbook glibc stub: `lea rdi, main` (`0x1ec0d18`, resolving RIP-relative to `0x1edc880`) followed by the tail call into `__libc_start_main`. The ELF header `Entry point address` is `0x1ec0d00` (`readelf -h`), which matches `_start` exactly.

---

## 2. Function Map

| Function | Addr | Size | Role | Conf |
|---|---|---|---|---|
| `_start` | `0x1ec0d00` | 38 | ELF entry; `lea rdi,main`→`__libc_start_main` | CERTAIN |
| `main` | `0x1edc880` | 5522 | `hilo/hlo2penguin/main.cc` driver (§3) | CERTAIN |
| `__static_initialization_and_destruction_0.constprop.0` | `0x1ed2130` | 15650 | **cl::opt registration** — builds category + 74 option objects (§4) | CERTAIN |
| `GetHiloCompileConfig(unique_ptr<HiloInstrumentation>)` | `0x1ed7bd0` | 1454 | Folds every cl::opt global → `hilo::CompileConfig`; refs `"tonga"` | HIGH |
| `xla::hilo::InitHloPassOptions()` | `0x1f92fe0` | 57 | Init HLO pass-option globals | HIGH |
| `hilo::initializeMLIRContext(MLIRContext&)` | `0x1ee0500` | 295 | Loads/registers dialects (stablehlo, mhlo, …) | HIGH |
| `xla::hilo::SetNativeKernelCastType(string const&)` | `0x1f93060` | — | Applies `native-kernel-auto-cast` value | HIGH |
| `xla::hilo::SetDynamicDMASbufBytes(uint const&)` | `0x1f930d0` | — | Applies `dynamic-dma-scratch-size-per-partition` | HIGH |
| `hilo::parseHloProtoInput(string const&)` | `0x21bebe0` | 1232 | Loads input HLO proto → `StatusOr<unique_ptr<HloModule>>` | HIGH |
| `hilo::CreateHloModuleFromProto(HloModuleProto&)` | `0x21be0a0` | 614 | Build HloModule from proto (MLIR-text path) | HIGH |
| `hilo::runStableHLOFrontendPipeline(ModuleOp,MLIRContext&,CompileConfig&)` | `0x1ee0ca0` | 585 | StableHLO front pipeline (when `stablehlo-lowering`) | HIGH |
| `xla::ConvertStablehloToHlo(ModuleOp)` | `0x2d2e8c0` | — | StableHLO→HLO bridge before flat-compile | HIGH |
| `hilo::HiloFlatCompile(unique_ptr<HloModule>&,MLIRContext&,CompileConfig&,string const&)` | `0x1efc7d0` | 387 | Non-partitioned compile → penguin.py | HIGH |
| `hilo::HiloPrePartitionCompile(unique_ptr<HloModule>&,MLIRContext&,CompileConfig&)` | `0x1efcb70` | 4872 | Partitioned compile path | HIGH |
| `hilo::HiloInstrumentation::HiloInstrumentation()` | `0x1f609e0` | — | Pass-manager instrumentation (printer/timing/verifier) | HIGH |
| `cl::ParseCommandLineOptions(...)` | (import) | — | LLVM cl parse; called @ `0x1edc91a` | CERTAIN |
| `cl::HideUnrelatedOptions(OptionCategory&,SubCommand&)` | (import) | — | Hides all but `Hlo2PenguinCategory`; @ `0x1edc8f9` | CERTAIN |
| `hilo::sha256(string const&)` | `0x21c6520` | — | "IR signature: " (`0x2520bb`) digest of loaded module | HIGH |

The full demangled signatures are verbatim from `functions.json`, e.g. `main` is plain `main`; the registration function is `_Z41__static_initialization_and_destruction_0ii.constprop.0`; the two compile entries are `_ZN4hilo15HiloFlatCompile…` (4 args incl. the output-path `string const&`) and `_ZN4hilo23HiloPrePartitionCompile…` (3 args, **no** output-path argument — the partitioned path manages its own outputs).

Data globals: `_ZL19Hlo2PenguinCategory` (`0x9c6a720`), `_ZL16verbosityMapping` (`map<verboseEnum,LogLevel>`, `0x9c66960`), and a `neuronToABSLLogLevelMapping` (`unordered_map<LogLevel,absl::LogSeverity>`), all constructed in the same static-init.

---

## 3. `main` — Reconstructed Control Flow

### Algorithm

Anchored to the `main` disassembly (`disasm/main_0x1edc880.asm`); call targets are the demangled symbols IDA attached to each `call` site.

```c
int main(int argc /*edi*/, char **argv /*rsi*/) {
    // --- instrumentation + MLIR context init -------------------------------
    auto *inst = new hilo::HiloInstrumentation();        // 0x1edc8ae new + ctor
    xla::hilo::InitHloPassOptions();                     // 0x1edc8c5
    mlir::registerPassManagerCLOptions();                // 0x1edc8ca
    mlir::MLIRContext ctx(/*threading=*/1);              // 0x1edc8db
    hilo::initializeMLIRContext(ctx);                    // 0x1edc8e7  (registers dialects)

    // --- command line ------------------------------------------------------
    cl::HideUnrelatedOptions(Hlo2PenguinCategory,        // 0x1edc8f9
                             cl::SubCommand::getTopLevel());
    cl::ParseCommandLineOptions(argc, argv,              // 0x1edc91a
                                /*Overview=*/"hlo2penguin\n",
                                /*Errs=*/0, /*EnvVar=*/0, /*LongFlags=*/false);

    // --- fold every cl::opt global into a hilo::CompileConfig ---------------
    hilo::CompileConfig cfg = GetHiloCompileConfig(move(inst));   // 0x1edc948

    // --- logger + verbosity ------------------------------------------------
    NeuronLogger::getInstance().init(logFile);           // 0x1edc959/0x1edc966
    setConsoleLogLevel(verbosityMapping[verbose]);       // 0x1edc98a
    setFileLogLevel  (verbosityMapping[logFileVerbose]); // 0x1edc9ae
    xla::hilo::SetNativeKernelCastType(nativeKernelAutoCast);        // 0x1edc9b8
    xla::hilo::SetDynamicDMASbufBytes(nativeKernelDynamicDMABytes);  // 0x1edc9d0
    NeuronLogger::getInstance().init(logFile);           // 0x1edc9e2 (re-init)
    // second verbose/logfile-verbose apply guarded by _Rb_tree::find  // 0x1edc9ff..0x1edca5c
    switch (verbose) {                                   // cmp eax,4/1/0 @ 0x1edca61
        case 4: setenv("TF_CPP_MIN_LOG_LEVEL", …);  break;  // 0x246310
        case 1: setenv("TF_CPP_MAX_VLOG_LEVEL", …); break;  // 0x26e316
        case 0: …;                                  break;
    }

    // --- modular / directory input branch ----------------------------------
    if (stat(input)==0 && S_ISDIR(input)) {              // 0x1edca96
        // read <input>/metadata.json (nlohmann::json)
        //   "model_files" (0x2324fe) + "/metadata.json" (0x24a2e6)
        // malformed → NEURON_LOG "NCC_MOD016" (0x23a4f3)
    }

    // --- load the HLO module -----------------------------------------------
    std::unique_ptr<xla::HloModule> m;
    if (/* proto-typed input */) {
        m = hilo::parseHloProtoInput(input);             // 0x1edceaf
    } else {                                             // MLIR-text path
        Block block; mlir::parseSourceFile(input, &block, parserCfg);
        ModuleOp mod = constructContainerOpForParserIfNecessary<ModuleOp>(&block, &ctx, loc); // 0x1edd6b2
        if (stableHLOLowering)                           // default true (§4)
            hilo::runStableHLOFrontendPipeline(mod, ctx, cfg);   // 0x1edd461
        xla::ConvertStablehloToHlo(mod);                 // 0x1edd486
        m = hilo::CreateHloModuleFromProto(mod.ToProto());
    }

    // --- IR signature log --------------------------------------------------
    NEURON_LOG(INFO, "IR signature: " + hilo::sha256(m->ToString()));  // 0x1edcf2f

    // --- compile dispatch (the single decision) ----------------------------
    if (partitionGraph)                                  // byte flag, §4
        HiloPrePartitionCompile(m, ctx, cfg);            // 0x1edd6d8
    else
        HiloFlatCompile(m, ctx, cfg, output);            // 0x1edd16f
    // compile error → NEURON_LOG "[ERROR] [" NCC_EMOD018 (0x282e6b) / NCC_EMOD019 (0x28b08e)
    return rc;
}
```

> **NOTE —** `cl::HideUnrelatedOptions` is what makes the 74-flag surface *the* surface. Without it, `--help` would also dump the gigantic inherited `--xla_*` and core-LLVM `cl` corpus that this binary statically links. Because every `Hlo2PenguinCategory`-registered option carries that category, the hide call collapses the visible vocabulary to exactly §4.

### The flat-vs-partition decision

The dispatch is a single byte test. In the parse-path tail (`0x1edd12f`):

```asm
0x1edd12f: cmp     byte ptr [rbp+var_7D0], 0   ; partitionGraph flag
0x1edd136: jz      loc_1EDD6BC                 ; → partitioned path
0x1edd16f: call    hilo::HiloFlatCompile(…, output)
…
0x1edd6d8: call    hilo::HiloPrePartitionCompile(…)
```

`HiloFlatCompile` (`0x1edd16f`) takes the output-path `string const&` as a fourth argument; `HiloPrePartitionCompile` (`0x1edd6d8`) takes only `(module, ctx, cfg)` — it derives output locations from `out-dir`/`partitioner-opts` itself. This asymmetry is visible in the demangled signatures and is the clearest structural tell of the two flows.

### Error codes & env vars

| Code / token | Addr | Where | Meaning |
|---|---|---|---|
| `NCC_MOD016` | `0x23a4f3` | metadata.json branch | malformed `<input>/metadata.json` |
| `NCC_EMOD018` | `0x282e6b` | compile-error path | front-half compile failure |
| `NCC_EMOD019` | `0x28b08e` | compile-error path | front-half compile failure (second variant) |
| `[ERROR] [` | `0x27ec7f` | `NEURON_LOG` prefix | diagnostic emit prefix |
| `IR signature: ` | `0x2520bb` | post-load | `sha256(module.ToString())` log |
| `TF_CPP_MIN_LOG_LEVEL` | `0x246310` | verbose switch | `setenv` XLA/TF log gate |
| `TF_CPP_MAX_VLOG_LEVEL` | `0x26e316` | verbose switch | `setenv` XLA/TF vlog gate |

These two `TF_CPP_*` are the **only** environment variables `main` touches, and both are *written* (via `setenv`) as a function of `--verbose`, not read — they wire the cl-level verbosity down into the XLA/TF logging substrate.

---

## 4. The cl::opt Surface (static-init `0x1ed2130`)

### How the count is grounded

The option count is not inferred from `--help` text. It is the number of distinct `cl::opt`/`cl::list` **destructor registrations** the static-init emits — one `__cxa_atexit(&~opt, &storage, __dso_handle)` per option object. Counting the demangled `…D2Ev` destructor symbols in `disasm/Z41__static_initialization_…0x1ed2130.asm` by template type gives:

| `cl` template | Destructor symbol (suffix) | Count |
|---|---|---|
| `cl::opt<bool>` | `optIbLb0…D2Ev` | 55 |
| `cl::opt<string>` | `optINSt7__cxx1112basic_string…D2Ev` | 10 |
| `cl::opt<int>` | `optIiLb0…D2Ev` | 3 |
| `cl::opt<uint>` | `optIjLb0…D2Ev` | 2 |
| `cl::opt<long>` | `optIlLb0…D2Ev` | 1 |
| `cl::opt<verboseEnum>` | `optI11verboseEnum…D2Ev` | 2 |
| `cl::list<float>` | `listIfbNS0_6parserIfEEED2Ev` | 1 |
| **Total option objects** | | **74** |

The static-init makes 47 `cl::Option::Option(…)` base ctors + 47 `addArgument()` + 46 `setArgStr()` + 36 `addCategory()` calls; the remaining options use the all-in-one templated `cl::opt<…>::opt(args…)` ctor (which inlines the base ctor and arg-add) plus the 8 `cl::apply<opt<bool>,initializer<bool>,…>` helpers. The total `__cxa_atexit` count is 77 = **74 options + 1 `Hlo2PenguinCategory` + 2 log-level maps**, a consistency check that the 74 figure closes exactly.

> **CORRECTION —** an earlier driver-strand note (D-A01) reported "73 `cl::opt` objects." The binary truth is **74**: the destructor-registration count is unambiguous, and the catalog below enumerates 74 distinct argStrings (no duplicates), every one of which is present verbatim in the ELF `strings`. The off-by-one in the prose count did not affect that report's catalog, which already listed 74 rows. STRONG.

### The build pattern

Per option the static-init emits the canonical LLVM-`cl` sequence:

```text
mov  esi, <argStr literal>          ; "input" / "target-instance" / …
mov  edi, _ZL<storage>              ; the cl::opt<T> global
[ stack: desc{ptr,len}, value_desc{ptr,len}, initializer<…>, OptionHidden ]
call cl::opt<T>::opt(argStr, [Hidden,] desc, [value_desc,] [init,] cat=Hlo2PenguinCategory)
   ; enum/cb opts instead: setArgStr ; addCategory ; cl::apply<…>(…, values(…)) ; addArgument
__cxa_atexit(&cl::opt<T>::~opt, &_ZL<storage>, __dso_handle)
```

`Hlo2PenguinCategory` is constructed first (name `"hlo2penguin options"` @ `0x24a283`, `registerCategory()` @ `0x1ed2242`), so every subsequent `.addCategory(Hlo2PenguinCategory)` attaches to it.

### Catalog

Every argStr below was confirmed present verbatim in the ELF (`strings -n 3` exact match, 74/74), and 72 of the 74 are additionally listed in the static-init's `referenced_by_functions` string-xref set (the two exceptions — `spmd`, `stub` — are short strings IDA did not attribute to the function-level xref table, but both exist as exact ELF strings *and* carry their distinctive help strings, so they are confirmed by help-text). `H` = `OptionHidden`. Bool default is `false` unless an `initializer<bool>` ctor was emitted; rows tagged `=true` were either byte-confirmed (post-ctor `byte cs:…,1` write) or follow the same `initializer<bool>` pattern.

| Flag | Storage `_ZL…` | Type | Default | Help / values | Conf |
|---|---|---|---|---|---|
| `input` | `input` | string | `-` | `<input hlo file>` (`value_desc=filename`) | CERTAIN |
| `output` | `output` | string | `/dev/null` | `<output python file>` (`0x24a29d`) | CERTAIN |
| `out-dir` | `outputDir` | string +cb | `""` | `<output directory>` (`value_desc=dirname`) | CERTAIN |
| `framework-dbg-fn` | `frameworkDbgFilename` | string | `debug_info_pttf.dbg` | Override framework-ops dump filename | HIGH |
| `hlo-dbg-fn` | `hloDbgFilename` | string | `debug_info_hlo.dbg` | Override HLO-ops dump filename | HIGH |
| `no-dbg` | `turnOffDebugDump` | bool | false | Do not produce IR debug files | HIGH |
| `tolerance` | `tolerance` | **list\<float\>** | — | Relative & absolute tolerance of simulation (`rtol atol`, multi_val) | HIGH |
| `remove-passthrough-hack` | `removePassthroughHack` | bool | false | (DEBUG) Remove passthrough outputs | HIGH |
| `lower-to-nki-kernels` | `lowerToNKIKernelCC` | bool | false | Lower patterns to NKI Kernels | HIGH |
| `partition` | `partitionGraph` | bool | false | Enable hilo partitioning | HIGH |
| `partitioner-opts` | `partitionerOpts` | string, H | `""` | Internal options to control the partitioner (`options_string`) | HIGH |
| `disable-load-fusion` | `disableLoadFusion` | bool | false | Disable load fusion | MED |
| `disable-memcast-motion` | `disableMemcastMotion` | bool | false | Disable memcast motion | MED |
| `neuron-op-fusion` | `neuronOpFusion` | bool | **=true** | Run neuron op-fusion | HIGH |
| `emit-split-points` | `emitSplitPoints` | bool | false | Produce Penguin hints to split the graph | HIGH |
| `hoist-compute` | `hoistCompute` | bool | **=true** | Hoist compute closer to its producer | HIGH |
| `native-to-custom-softmax` | `nativeToCustomSoftmax` | bool | false | Pattern-match native softmax → custom | HIGH |
| `upcast-all-to-fp32` | `upcastAllToFP32` | bool | false | Upcast all ops to FP32 | HIGH |
| `spmd` | `spmd` | bool | false | Run additional optimizations only applied to SPMD models | HIGH |
| `disable-early-opt-barrier-removal` | `disableEarlyOptBarrierRemoval` | bool | false | Disable early opt-barrier-removal pass | HIGH |
| `fuseCCSlice` | `fuseCCSlice` | bool | false | Fuse CC op followed by slices if possible | HIGH |
| `remat` | `remat` | bool | false | Run rematerialization on HLO Graph | HIGH |
| `remove-opt-barriers` | `removeOptBarriers` | bool | **=true** | Remove Optimization Barriers from input HLO | HIGH |
| `coalesce-collectives` | `coalesceCollectives` | bool | false | Run coalescing of collectives on HLO Graphs | HIGH |
| `print-module-stats` | `printModuleStats` | bool | false | Print module statistics incl. instruction histogram | HIGH |
| `fold-iota` | `foldIota` | bool | **=true** | Fold iotaOp+powOp combo into constant | HIGH |
| `mlir-canonicalizer` | `mlirCanonicalizer` | bool | false | Enable `mlir::Canonicalizer` pass | HIGH |
| `lower-complex` | `lowerComplex` | bool | **=true** | Convert complex ops into real-value ops | HIGH |
| `lower-complex-extra` | `lowerComplexExtra` | bool | false | Additional complex-lowering patterns | HIGH |
| `verify-supported-ops` | `verifySupportedOps` | bool | **=true** | Verify ops supported at MHLO/StableHLO level | HIGH |
| `stub` | `stub` | bool | false | Gut graph contents, return constant for all outputs (debug) | HIGH |
| `eager-mode` | `eagerMode` | bool | false | Enable eager mode | HIGH |
| `force-eager-mode` | `forceEagerMode` | bool | false | Force full eager mode (debug) | HIGH |
| `schedule` | `schedule` | bool | false | Enable scheduler | HIGH |
| `engine-placement-mode` | `enginePlacementMode` | string | `""` | Force compute-engine placement (single-op HLO only) | HIGH |
| `emit-tensor-level-ops` | `emitTensorLevelOps` | bool | **=true** | Tensor-level ops for penguin copy-elim | HIGH |
| `emit-experimental-tensor-level-ops` | `emitExperimentalTensorLevelOps` | bool | **=true** | Tensor-level ops for penguin copy-elim | HIGH |
| `disable-emit-tensor-ops-memcpy` | `disableEmitTensorOpsMemcpy` | bool | false | Override: disable offloaded memcpy | HIGH |
| `disable-emit-tensor-ops-cast` | `disableEmitTensorOpsCast` | bool, H | false | Override: disable offloaded memcast | HIGH |
| `disable-emit-tensor-ops-transpose` | `disableEmitTensorOpsTranspose` | bool | false | Override: disable offloaded transpose | HIGH |
| `disable-emit-tensor-ops-slice` | `disableEmitTensorOpsSlice` | bool | false | Override: disable offloaded slice | HIGH |
| `disable-emit-tensor-ops-concat` | `disableEmitTensorOpsConcat` | bool, H | false | Override: disable offloaded concat | HIGH |
| `emit-tensor-level-dropout-ops` | `emitTensorLevelDropoutOps` | bool | false | Tensor-level dropout operators | HIGH |
| `expand-batch-norm-training` | `expandBatchNormTraining` | bool | false | Expand batch-norm-training via `xla::BatchNormExpander` | HIGH |
| `expand-batch-norm-training-and-grad` | `expandBatchNormTrainingAndGrad` | bool | false | Expand batch-norm-training-and-grad | HIGH |
| `enable-experimental-cc-control-deps` | `enableExperimentalCCControlDeps` | bool | false | Tokens → control-dep between CCOps | HIGH |
| `canonicalize-conv` | `canonicalizeConvolutions` | bool | false | Canonicalize small-channel 2×2-stride convs | HIGH |
| `enable-native-kernel` | `enableNativeKernel` | bool | false | Enable lowering to native kernel | HIGH |
| `batch-norm-training-upcast` | `batchNormTrainingUpcast` | bool | false | Upcast BF16 batch-norm-training to F32 | HIGH |
| `native-kernel-auto-cast` | `nativeKernelAutoCast` | string | `matmult-to-bf16` | Auto-cast type for native-kernel intermediates | HIGH |
| `dynamic-dma-scratch-size-per-partition` | `nativeKernelDynamicDMABytes` | uint | (read by `SetDynamicDMASbufBytes`) | SB scratch size per partition for Dynamic DMA | HIGH |
| `decompose-attention` | `decomposeAttention` | bool | false | Decompose masked self-attention | HIGH |
| `analyze-schedule` | `analyzeSchedule` | bool | false | Schedule analysis | MED |
| `schedule-fusion` | `scheduleFusion` | bool | false | Enable schedule fusion | HIGH |
| `dump-partitioned-hlo` | `dumpPartitionedHlo` | bool | false | Dump partitioned HLO for modular infergoldens | HIGH |
| `inject-numerical-errors` | `injectNumericalErrors` | bool | false | Inject numerical errors (`--numerical-error-targets`) | HIGH |
| `fuzz-aliases` | `fuzzAliases` | int | 0 | Alias-fuzzing mode 0–5 (none / remove-all / add-must / add-may / flip-must→may / flip-may→must) | HIGH |
| `verify-aliasing` | `verifyAliasing` | bool | **=true** | Verify copy-insertion preserves aliasing semantics | HIGH |
| `operand-downcaster` | `operandDowncaster` | bool | false | Apply operand-downcast on mixed-precision ops | HIGH |
| `convert-fs-patterns-to-cc` | `convertFSPatternToCC` | bool | false | Match unique patterns found in FS models | HIGH |
| `drop-collective-tokens` | `dropCollectiveTokens` | bool | false | Drop tokens from CCOps | HIGH |
| `pass-fixpt-iter-limit` | `passFixPtIterLimit` | int | (limit) | Fixpoint-pass iteration count | HIGH |
| `stablehlo-lowering` | `stableHLOLowering` | bool | **=true** | Use stableHLO | CERTAIN |
| `target-instance` | `targetInstance` | string, H | `trn1` | `{inf1, trn1, inf2, gen3, core_v4, trn1n, trn2, trn2n, trn3}` | CERTAIN |
| `logical-nc-config` | `logicalNCConfig` | int, H | `1` | `{1, 2}` — logical NeuronCore config | CERTAIN |
| `verbose` | `verbose` | verboseEnum | `error` | `{error=4, warning=2, info=1, debug=0}` | CERTAIN |
| `tiled-inst-limit` | `tiledInstLimit` | int | (limit) | Tiled-instruction-count budget per lnc | HIGH |
| `logfile` | `logFile` | string, H | `log-neuron-cc.txt` | Logfile name | CERTAIN |
| `logfile-verbose` | `logFileVerbose` | verboseEnum, H | `debug` | `{error, warning, info, debug}` | CERTAIN |
| `preserve-control-deps` | `preserveControlDeps` | bool | false | Preserve control-deps and pass to Tensorizer | HIGH |
| `inject-prints-for-send-to-host` | `injectPrintsForSendToHost` | bool | false | Insert `DevicePrint` for host-transfer send operands | HIGH |
| `ml-dtypes-version` | `mlDtypesVersion` | string, H | `unknown` | Version for `ml_dtypes` | CERTAIN |
| `run-pre-hilo-passes` | `runPJRTPasses` | bool | false | Run PJRT passes | HIGH |
| `constant-decomposition-threshold` | `constantDecompositionThreshold` | long | (threshold) | Decompose constants larger than N bytes into scalar+broadcast | HIGH |

> **GOTCHA —** `tolerance` is the **only** `cl::list` in the binary (`cl::list<float>` with `multi_val`), accepting two floats (`rtol atol`). It is *not* a `cl::opt<string>`. The single `listIfbNS0_6parserIfEEED2Ev` destructor is what brings the bool/string/int/uint/long ctor counts to a total of 74. Treating it as a scalar string option is the easiest way to mis-count the surface.

> **QUIRK —** seven options are `OptionHidden`: `partitioner-opts`, `target-instance`, `logical-nc-config`, `logfile`, `ml-dtypes-version`, `disable-emit-tensor-ops-cast`, `disable-emit-tensor-ops-concat`. The hidden ones include the two flags the driver *always* sets (`target-instance`, `logical-nc-config`) — they are deliberately kept out of `--help` because they are machine-emitted, not user-facing.

### `logical-nc-config` and the target-instance set

`--logical-nc-config` (`0x27670c`, hidden, `cl::opt<int>`, default `1`, values `{1,2}`) selects the logical NeuronCore configuration — how many physical NeuronCores the front-half treats as one logical unit (`1` = one core, `2` = the dual-core "lnc2" grouping). The driver passes it as `--logical-nc-config=<n>` from the config key `logical_nc_config`. All nine `--target-instance` family tokens are present verbatim in the ELF (`inf1 trn1 inf2 gen3 core_v4 trn1n trn2 trn2n trn3`); the default is `trn1`. `GetHiloCompileConfig` additionally references the backend-dialect token `"tonga"` (`0x27ab43`), threaded into the config even though the Tonga backend dialect itself is not loaded in this front-half.

> **CORRECTION —** the native `--verbose` default is `error` and `--logfile-verbose` default is `debug` (binary, this section). These differ from the Python `CompileCommand` argparser defaults (`--verbose=user`, `--logfile-verbose=info`). Both are correct for their respective layers — the Python defaults belong to a *different* parser (Part 3.2, two-parser architecture) and must not be conflated with the native cl-level defaults here.

---

## 5. `GetHiloCompileConfig` — Flags → CompileConfig

### Purpose

`GetHiloCompileConfig` (`0x1ed7bd0`, 1,454 B) is the inverse of the static-init: it reads each `_ZL<storage>` cl::opt global and writes the corresponding field of a `hilo::CompileConfig` that `HiloFlatCompile`/`HiloPrePartitionCompile` consume. It takes ownership of the `HiloInstrumentation` (its sole argument, moved from `main`), so the config carries the pass-manager instrumentation hooks (printer/timing/verifier) alongside the flag-derived fields.

A few flag effects are applied *outside* the config object, directly in `main`, by dedicated setters: `SetNativeKernelCastType(nativeKernelAutoCast)` (`0x1edc9b8`) installs the `native-kernel-auto-cast` policy globally, and `SetDynamicDMASbufBytes(nativeKernelDynamicDMABytes)` (`0x1edc9d0`) installs the Dynamic-DMA scratch size. The verbosity enums are routed through `verbosityMapping` (`0x9c66960`) into console/file log levels rather than into the config.

> **GAP —** the precise per-flag → `CompileConfig`-field offset mapping inside `GetHiloCompileConfig` was **not** byte-traced (the function is decompile-skipped; only its disassembly exists). Flag→behavior on this page is established from the help strings and the explicit setter wiring in `main`; the exact struct layout of `hilo::CompileConfig` is a follow-up. MED.

---

## 6. Driver → Native Handoff

Two distinct "penguin" paths exist in the Python driver; **only the second** spawns this native ELF.

**(a) `Frontend.so` — in-process penguin (`runPenguin`).** This path drives the *Python* module `neuronxcc.starfish.penguin.Penguin` in-process and emits `--targets`, `--tensorizer-options`, `--internal-hlo2tensorizer-options`, `--enable-penguin-mac-count` into that in-process arg vector. These three dash-tokens live here and as user options in `CompileCommand.so` — **not** in the native binary.

**(b) `HLOToTensorizer.so` — native `hlo2penguin` (`runHlo2Tensorizer`).** This job reads the driver option `internal_hlo2tensorizer_options` (free-form extra-flags string) plus config keys (`logical_nc_config`, `target`/`target_instance`, `out_dir`, `penguin_files`, `hlo_netlist.json`), and **expands** them into the native `--` tokens, building the subprocess `argv`:

```text
neuronx-cc (CompileCommand.so)
  │   user opts: internal_hlo2tensorizer_options, internal_tensorizer_opt_level,
  │              internal_hlo_remat, internal_hlo_analyze_schedule, tensorizer_options
  ├─ collectFrontendPipeline → Frontend.so::runPenguin   (IN-PROCESS python Penguin)
  │        emits --targets / --tensorizer-options / --internal-hlo2tensorizer-options
  └─ HLOToTensorizer.so::runHlo2Tensorizer  →  subprocess  starfish/bin/hlo2penguin  (NATIVE)
         argv = [ --input <hlo> --output <penguin.py> --out-dir <d>
                  --target-instance=<fam> --logical-nc-config=<n>
                  --verbose=<lvl> --logfile=<f> --logfile-verbose=<lvl>
                  (--partition --spmd --remat --analyze-schedule …)
                  <split internal_hlo2tensorizer_options tokens> ]
```

Native `--flag` tokens emitted by `HLOToTensorizer.so` (verbatim from its Cython `__pyx_*` const strings) that map onto §4 cl::opts:

| Token (verbatim) | Native cl::opt (§4) |
|---|---|
| `--input` / `--output` / `--out-dir` | `input` / `output` / `out-dir` |
| `--target-instance=` | `target-instance` (hidden) |
| `--logical-nc-config=` | `logical-nc-config` (hidden) |
| `--partition` / `--spmd` / `--remat` | `partition` / `spmd` / `remat` |
| `--analyze-schedule` / `--engine-placement-mode` | `analyze-schedule` / `engine-placement-mode` |
| `--dump-partitioned-hlo` / `--coalesce-collectives` | `dump-partitioned-hlo` / `coalesce-collectives` |
| `--disable-load-fusion` / `--emit-tensor-level-dropout-ops` | same |
| `--ml-dtypes-version=` / `--logfile=` / `--logfile-verbose=` / `--verbose=` | same (last three hidden incl. `logfile`) |

> **CORRECTION —** the task framing that `--internal-hlo2tensorizer-options` is parsed by this native tool is wrong. A `strings` scan of the ELF returns **zero** matches for `internal-hlo2tensorizer-options` and `tensorizer-options`, and a single match for `targets` (an MLIR pass-arg fragment). The user's `internal_hlo2tensorizer_options` string is split on whitespace by `HLOToTensorizer.so` and appended **verbatim** onto the native `argv` — so any §4 flag *can* be injected through it, but the token `--internal-hlo2tensorizer-options` never reaches the native cl parser. STRONG (negative grep + driver string evidence).

> **GOTCHA —** `HLOToTensorizer.so` also emits tokens that are **not** in §4: `--modular-flow-mac-target=200000000000`, `--layers-per-module=`, `--num-neuroncores-per-sengine=`, `--max-{all-gathers,all-reduce,reduce-scatter}-buffer-size=`, `--coalesce-{all-gathers,all-reduces,reduce-scatters}[=false]`, `--build-with-users`, `--disable-layer-det`, `--mmf`, `--native-int64`, `--disable-verifier=`. These are **not** rejected — they are consumed by the partitioner sub-grammar (`xla::partition::*`) reached *inside* `HiloPrePartitionCompile` (`0x1efcb70`) and/or routed through the `partitioner-opts` `options_string`. Do not expect them in `--help`; they are a separate grammar layered under `--partition`.

---

## 7. Reimplementation Notes & Gaps

A faithful reimplementation reproduces: (1) the `main` ordering above — instrumentation/context init *before* `ParseCommandLineOptions`, category-hide *before* parse, config-fold *after* parse; (2) the 74-option category with the seven hidden flags and the bool defaults; (3) the single `partitionGraph` byte test selecting `HiloFlatCompile(…, output)` vs `HiloPrePartitionCompile(…)`; and (4) the StableHLO front-pipeline + `ConvertStablehloToHlo` bridge on the MLIR-text input path, gated by the default-true `stablehlo-lowering`.

- **GAP — config layout.** `hilo::CompileConfig` field offsets are not traced (§5). Treat the flag→field mapping as behavioral, not structural.
- **GAP — driver bytecode.** `HLOToTensorizer.so` / `Frontend.so` / `CompileCommand.so` have no decompile dump; the token-emission order inside `runHlo2Tensorizer` is reconstructed from `__pyx_*` string tables, not control flow. MED.
- **GAP — partitioner sub-grammar.** The non-§4 tokens consumed inside `HiloPrePartitionCompile` (`xla::partition::*`) are a separate deep-dive (Part 4 / partition strand).

---

## Cross-References

- [CompileCommand Pipeline & the Canonical Job Order](compilecommand-pipeline.md) — the driver (3.3) that assembles the `runHlo2Tensorizer` job and the `internal_hlo2tensorizer_options` user option.
- [The Two-Parser Architecture](two-parser-architecture.md) — why the Python `--verbose`/`--logfile-verbose` defaults differ from the native cl defaults in §4.
- [Sub-Tool argv Construction & Replay](subtool-argv.md) — how `HLOToTensorizer.so` builds the native `argv` token list of §6.
- **Part 4 — hlo-opt + hlo2penguin** — the HLO-level optimization passes invoked inside `HiloFlatCompile`/`HiloPrePartitionCompile`.
- **Part 5 — Penguin IR & Middle-End** — the `penguin.py` IR this tool emits and the lowering that consumes it.
