# xla_infergoldens — the Reference Evaluator

> *All addresses on this page apply to the `xla_infergoldens` standalone ELF shipped in neuronx_cc **2.24.5133.0+58f8de22** (cp310 wheel, `neuronxcc/starfish/bin/xla_infergoldens`). The binary is not stripped; VA == file offset for `.text`/`.rodata`. Other versions will differ.*

## Abstract

`xla_infergoldens` is the Neuron compiler's **CPU reference oracle**. Given an HLO module and a set of input tensors, it runs the module on the host CPU and writes the resulting output tensors back out as NumPy `.npy` "goldens". Those goldens are the numerical ground truth the rest of the toolchain checks device-equivalent output against: the BIR simulator inside `walrus_driver` (`birsim`) re-executes the scheduled BIR and auto-compares its per-tensor results to these goldens within a tolerance. Golden inference is the oracle; BIR-sim is the candidate; the tolerance compare is the accept/reject signal that guards scheduling, codegen, and the autotuner search.

The tool is a thin **`hilo::`** driver shell wrapped around two statically-linked XLA engines. The shell (`main@0x1fb4fa0`) does option parsing, module/literal I/O via NumPy codecs, an in-process "JIT Legalization" `HloPassManager`, a native-kernel simulation shim, and a multihost-runner wrapper. The execution core is `xla::FunctionalHloRunner::CompileAndRun` on a **PjRt Host-CPU client**, which compiles through the statically-linked `xla::cpu::*` Thunk runtime and falls back to the `xla::HloEvaluator` reference interpreter for ops the Thunk path defers. The numeric heart of that interpreter is the `xla::HloEvaluatorTypedVisitor<NativeT, AccumT>` family — a **44-op × 30-dtype = 1320** matrix of per-element `Handle*` methods, every cell of which is present in the binary symbol table (directly counted, not inferred).

This page documents the end-to-end golden-generation flow (`main` → `hilo::XLAInferGoldens` → JIT-legalize → `FunctionalHloRunner` → `HloEvaluatorTypedVisitor` per-op dispatch → `.npy` golden), the op×dtype dispatch matrix and its emit machinery, the native-kernel simulation splice (where a Neuron custom-call's golden value *is* the Penguin simulator's output), and the downstream BIR-sim auto-compare gate. A correction up front: `--birsim-output-tolerance` is **not** a flag of this binary — it lives in the `birsim` `cl::opt` belt on the *consumer* side (see [§7](#7-golden--bir-sim-auto-compare-the-tolerance-gate)).

For reimplementation, the contract is:

- The CLI grammar (`getopt_long` string `m:t:i:o:I:v:esO:SuhUF`) and the module/input/output staging it drives.
- The NumPy ↔ `xla::Literal` codec, including the `.meta` sidecar for `ml_dtypes` NumPy cannot natively express.
- The `HloEvaluatorTypedVisitor<T,T>` dispatch shape: 44 ops, 30 dtypes, the accumulator-type rule, and how each cell reduces to a scalar lambda fed to `PopulateLinear*`.
- The native-kernel sim splice and the golden → BIR-sim auto-compare contract.

| | |
|---|---|
| **Entry point** | `main` @ `0x1fb4fa0` (12329 bytes, 523 bb) |
| **Golden engine** | `hilo::XLAInferGoldens` @ `0x1fc7f70` |
| **Execution core** | `xla::FunctionalHloRunner::CompileAndRun` (PjRt Host-CPU) |
| **Reference interpreter** | `xla::HloEvaluator::Evaluate` @ `0x85c3870` |
| **Numeric core** | `HloEvaluatorTypedVisitor<NativeT,AccumT>` — **1320** `Handle*` bodies (44×30) |
| **getopt string** | `m:t:i:o:I:v:esO:SuhUF` (@ `0x243574`) |
| **Tolerance gate** | `--birsim-output-tolerance` — in `birsim` (`walrus_driver`/`nki_klr_sim`), **not** here |
| **Driver job** | `driver/jobs/XLAInferGoldens.cpython-310.so` (Cython) |

---

## 1. The End-to-End Golden Flow

### Purpose

`xla_infergoldens` turns an HLO module plus inputs into reference output tensors. It is invoked as a subprocess by the Python `XLAInferGoldens` driver job, one invocation per HLO module (or per modular-flow partition). It does *not* schedule or codegen for the device; its only job is to compute what the *correct* answer is, on a CPU, with enough dtype fidelity (the full float8 / MXFP4 / packed-int zoo) to be a meaningful reference for a quantized Neuron graph.

### Entry Point

```text
main (0x1fb4fa0, 523 bb)                          ── CLI parse, I/O, orchestration
  ├─ getopt_long("m:t:i:o:I:v:esO:SuhUF")          ── fill hilo::XlaInfergoldensOptions
  ├─ readCommandLineInputs (0x1fb48a0)             ── validate ("incorrect input type")
  ├─ <parse module: proto | text | snapshot>       ── convertProtoForTF2_11 (0x23330c0)
  ├─ hilo::npy2literal (0x78caa00)  × N inputs      ── NumPy → xla::Literal
  ├─ modifyInputOutputAliasing (0x1fb1650)         ── strip aliasing → all outputs materialize
  ├─ ProbeHloOutputs / probeAllIntermediates       ── (0x1fbce70 / 0x1fb0ff0) expose roots
  ├─ getQuantizeMXDtypeOverrides (0x1fae930)       ── MX/quant dtype overrides
  ├─ hilo::XLAInferGoldens (0x1fc7f70)             ── *** golden engine ***
  │    ├─ addToXlaFlags (0x1fbe1b0)                ── XLA_FLAGS env
  │    ├─ runJitLegalizationPipeline (0x1fc44c0)   ── HloPassManager "JIT Legalization"
  │    └─ runUsingMultihostHloRunner (0x1fc6680)   ── FunctionalHloRunner::CompileAndRun
  └─ hilo::literal2npy (0x78cc560) × results        ── Literal → value_output<i>.npy
```

### Algorithm

```c
function main(argc, argv):                              // 0x1fb4fa0
    XlaInfergoldensOptions opt;
    // ---- option parse: getopt_long, string @0x243574 ----
    while ((c = getopt_long(argc, argv, "m:t:i:o:I:v:esO:SuhUF", ...)) != -1):
        switch (c):                                     // -m module, -t type, -i input(s),
            ...                                          // -o output(s), -I in-type, -O out-dir,
                                                         // -v verbose, -s save-intermediates,
                                                         // -e emulate-devices, -S spmd,
                                                         // -u unsafe-cast, -U upcast-fp32, -F fp8
    readCommandLineInputs(opt);                          // 0x1fb48a0 — "incorrect input type"
    echo_banner(opt);                                    // "Module: ", "Module Type: ", ...

    // ---- module parse (3 source formats by -t) ----
    switch (opt.moduleType):
        proto:    ifstream >> HloModuleProto;            // "Failed to parse proto: "
        text:     ParseAndReturnUnverifiedModule(text);
        snapshot: HloSnapshot::ParseFromIstream;         // inputs come from snapshot args
                                                         // "Failed to parse snapshot: "
    convertProtoForTF2_11(&proto);                       // 0x23330c0 — TF 2.11 proto compat
    module = HloModule::CreateFromProto(proto);

    // ---- input literals (numpy), unless snapshot ----
    for path in opt.inputs:
        inputs.push_back(npy2literal(path, opt.unsafeDtypeCast));   // 0x78caa00

    // ---- expose every output as a distinct, materialized literal ----
    modifyInputOutputAliasing(module);                   // 0x1fb1650 — kill I/O aliasing
    if opt.saveIntermediates:
        probeAllIntermediates(module, names);            // every node becomes a root
    ProbeHloOutputs(module, chosen_roots);               // 0x1fbce70 — rebuild entry root

    getQuantizeMXDtypeOverrides(module);                 // 0x1fae930 — MX/quant overrides

    // ---- run the golden engine ----
    XLAInferGoldens(module, inputs, &instrumentation, opt);   // 0x1fc7f70

    // ---- write goldens ----
    for (i, result) in results:
        literal2npy(result, outDir + "/value_output" + i + ".npy", ...);  // 0x78cc560
        // + value_intermediate<i>.npy when -s; + <name>.npy.meta for ml_dtypes
    return 0;
```

> **NOTE —** `modifyInputOutputAliasing` (`0x1fb1650`) is run *before* evaluation precisely so that input/output buffer aliasing does not let an output silently share storage with an input. A golden must be an independent literal the simulator can diff against; an aliased output would not materialize as a separate tensor.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `main` | `0x1fb4fa0` | 12329 | Entry: getopt, parse, alias-fix, probe, run, write npy | CERTAIN |
| `(anon)::readCommandLineInputs` | `0x1fb48a0` | 325 | Validate parsed CLI (`"incorrect input type"`) | HIGH |
| `(anon)::printUsage` | `0x1fae080` | 319 | `Usage:` help, full flag table | HIGH |
| `(anon)::modifyInputOutputAliasing` | `0x1fb1650` | 1649 | Remove I/O aliasing so outputs materialize | CERTAIN |
| `ProbeHloOutputs` | `0x1fbce70` | 1586 | Rebuild entry root to expose chosen outputs | CERTAIN |
| `(anon)::probeAllIntermediates(…)::lambda` | `0x1fb0ff0` | 665 | Collect every node as a probe target (`-s`) | HIGH |
| `(anon)::getQuantizeMXDtypeOverrides(…)::lambda` | `0x1fae930` | 350 | MX/quant dtype override map | MEDIUM |
| `hilo::convertProtoForTF2_11` | `0x23330c0` | 163 | HloModuleProto TF-2.11 compat shim | CERTAIN |

---

## 2. The Golden Engine — `hilo::XLAInferGoldens`

### Purpose

`hilo::XLAInferGoldens` (`0x1fc7f70`) is the driver between a parsed module and a host-CPU run. It sets XLA env flags, runs the in-process legalization pipeline, then dispatches to the multihost HLO runner. It is the single function `main` calls to "compute the goldens"; everything numeric happens beneath it.

### Entry Point

```text
hilo::XLAInferGoldens (0x1fc7f70, 1296 bytes, 54 bb)
  ├─ addToXlaFlags("xla_allow_excess_precision", …)            (0x1fbe1b0)
  ├─ addToXlaFlags("xla_force_host_platform_device_count", N)  ── N = emulated device count
  ├─ getParticipatingNumDevices (0x1fc5dc0)                    ── SPMD/collective device count
  ├─ runJitLegalizationPipeline (0x1fc44c0)                    ── HloPassManager "JIT Legalization"
  └─ runUsingMultihostHloRunner (0x1fc6680)                    ── FunctionalHloRunner::CompileAndRun
```

### Algorithm

```c
function XLAInferGoldens(module, inputs, instr, opt):          // 0x1fc7f70
    // ---- emulated device count for -e / -S ----
    numDevices = 1;
    if opt.emulateMultipleDevices || opt.spmd:
        numDevices = getParticipatingNumDevices(module);       // 0x1fc5dc0
    addToXlaFlags("xla_allow_excess_precision", "true");        // 0x1fbe1b0 → XLA_FLAGS env
    addToXlaFlags("xla_force_host_platform_device_count", numDevices);

    // ---- in-process "JIT Legalization" HloPassManager ----
    runJitLegalizationPipeline(module, instr,                  // 0x1fc44c0
                               opt.upcastAllToFP32,
                               opt.fp8e4m3fnAsFp8e4m3,
                               opt.unsafeDtypeCast);
        // TupleSimplifier + LegalizeTypesForInfergoldens
        //   + LegalizeCCOpsForTensorizer + InjectNumericalErrors
        //   + PreprocessKernelsForSimulation  (registry passes 49–53; MED on exact set)

    // ---- host-CPU run ----
    runUsingMultihostHloRunner(module, inputs,                 // 0x1fc6680
                               opt.spmd, opt.emulateMultipleDevices, numDevices);
```

> **QUIRK —** despite a prior pass labeling `xla_infergoldens` as holding "zero HLO optimizer passes", it *does* run a real `HloPassManager` — "JIT Legalization" (`runJitLegalizationPipeline@0x1fc44c0`, 4663 bytes, 286 bb). The distinction is that these are *legalization/simulation-prep* passes (type legalization, custom-call lowering for the tensorizer, optional numerical-error injection, kernel preprocessing), not the device optimizer. The "no optimizer passes" claim should be read against this legalization shell, not as "no passes at all". Only `TupleSimplifier` is a directly-named ctor callee; the rest are constructed via inlined `make_unique` factories and inferred from the `PreprocessKernelsForSimulation` string family (MEDIUM on the exact list).

### Related Knobs

| Flag (XLA_FLAGS) | Set by | Meaning |
|---|---|---|
| `xla_allow_excess_precision` | `addToXlaFlags` (`0x1fbe1b0`) | Permit wider intermediate precision (the fp32-accumulator path) |
| `xla_force_host_platform_device_count` | `addToXlaFlags` | Emulated device count for `-e`/`-S` SPMD on one CPU |

---

## 3. The HloEvaluatorTypedVisitor Matrix — 44 × 30

### Purpose

`xla::HloEvaluator` is the structural/control interpreter; it walks the computation, handles control flow and structural ops itself, and delegates every **element-wise** opcode to a per-dtype template instantiation `xla::HloEvaluatorTypedVisitor<NativeT, AccumT>`. This template is the numeric core of golden inference. Its dispatch space is a clean Cartesian product, and the full product is directly observable in the symbol table — every cell is a named symbol, so nothing here is reconstructed.

### The dispatch dimensions

The visitor's `Handle*` surface is best described by its axes, not by dumping 1320 method bodies. (This is the dispatch-dimension table the style guide prescribes for a large mechanical space.)

| Axis | Cardinality | Values | Source |
|---|---|---|---|
| Op | **44** | the `Handle*` set in §3.1 | demangled, directly counted |
| Dtype (`NativeT`) | **30** | §3.2 table | demangled, directly counted |
| Accumulator (`AccumT`) | — | function of `NativeT` (the rule in §3.2) | demangled |
| Product | **1320** | one `Handle<Op>(HloInstruction const*)` body per (op, dtype) | counted = 44 × 30 |

The count is exact. Selecting all symbols of the form
`_ZN3xla24HloEvaluatorTypedVisitorI<dtype>E<n>Handle<Op>EPKNS_14HloInstructionE`
from the binary's function table yields **1320** distinct top-level methods, **44** distinct op names, and **30** distinct `<NativeT,AccumT>` instantiations — a uniform grid with no missing cells.

> **QUIRK —** the grid is *complete*: the emitter generates every op for every dtype, even type-incompatible pairs (e.g. a float-only transcendental for `bool`, or a bit-shift for `complex<float>`). Those nonsensical cells are not elided at compile time; each resolves at runtime to an `Unimplemented`/error stub. A reimplementer who prunes the grid by op/dtype compatibility will produce a *different binary shape* than the original — the original trades binary size (1320 bodies) for a uniform template instantiation that needs no per-pair `enable_if`.

### 3.1 Op axis (44 ops, demangled verbatim)

```text
Unary math:      Abs Ceil Floor Round RoundNearestEven Sign Negate Not Exp Log Logistic
                 Sqrt Rsqrt Cbrt Sin Cos Tan Tanh Erf Clz PopulationCount ReducePrecision Iota
Binary arith:    Add Subtract Multiply Divide Power Remainder Maximum Minimum
Binary logic:    And Or Xor ShiftLeft ShiftRightArithmetic ShiftRightLogical
Ternary/struct:  Select Clamp Pad
Heavy numeric:   Dot DotSlowPath Convolution
RNG:             Rng
```

(`HandleExpm`/`HandleAtan`/`HandleDotSlowPathWithLiterals`/`HandleConvolutionWithLiterals` appear in the broader symbol sweep with extra inlined-lambda multiplicity, but exactly **44** distinct names are top-level `Handle*` methods on the visitor; the list above is the directly-counted set.)

### 3.2 Dtype axis (30 instantiations, demangled verbatim)

The template is `<NativeT, AccumT>`; `AccumT` is the wider type used for compute/accumulation. The rule is mechanical: integers promote to `long`/`unsigned long`; all low-precision floats promote to `float`; `double` and `complex` are self-accumulating.

| `NativeT` (HLO element type) | `AccumT` | Promotion class |
|---|---|---|
| `float` | `float` | self |
| `double` | `double` | self |
| `Eigen::half` (f16) | `float` | narrow-float → fp32 |
| `Eigen::bfloat16` | `float` | narrow-float → fp32 |
| `std::complex<float>` | `std::complex<float>` | self |
| `std::complex<double>` | `std::complex<double>` | self |
| `bool` | `bool` | self |
| `signed char` (s8) | `long` | int → s64 |
| `short` (s16) | `long` | int → s64 |
| `int` (s32) | `long` | int → s64 |
| `long` (s64) | `long` | self |
| `unsigned char` (u8) | `unsigned long` | uint → u64 |
| `unsigned short` (u16) | `unsigned long` | uint → u64 |
| `unsigned int` (u32) | `unsigned long` | uint → u64 |
| `unsigned long` (u64) | `unsigned long` | self |
| `ml_dtypes::intN<1,signed char>` | `long` | packed-int → s64 |
| `ml_dtypes::intN<2,signed char>` | `long` | packed-int → s64 |
| `ml_dtypes::intN<4,signed char>` (s4) | `long` | packed-int → s64 |
| `ml_dtypes::intN<1,unsigned char>` | `unsigned long` | packed-uint → u64 |
| `ml_dtypes::intN<2,unsigned char>` | `unsigned long` | packed-uint → u64 |
| `ml_dtypes::intN<4,unsigned char>` (u4) | `unsigned long` | packed-uint → u64 |
| `ml_dtypes::float8_internal::float8_e3m4` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e4m3` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e4m3b11fnuz` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e4m3fn` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e4m3fnuz` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e5m2` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e5m2fnuz` | `float` | fp8 → fp32 |
| `ml_dtypes::float8_internal::float8_e8m0fnu` (MX scale) | `float` | fp8 → fp32 |
| `ml_dtypes::mxfloat_internal::float4_e2m1fn` (MXFP4) | `float` | fp4 → fp32 |

> **NOTE —** the presence of the full float8 zoo (`e3m4`, `e4m3{,b11fnuz,fn,fnuz}`, `e5m2{,fnuz}`, `e8m0fnu`) plus MXFP4 (`float4_e2m1fn`) and the packed `intN<{1,2,4}>` types confirms golden inference supports the Neuron quantization dtypes end-to-end. The `e8m0fnu` "scale" type and `float4_e2m1fn` together are the MX (microscaling) pair; the evaluator can therefore produce a *quantization-accurate* golden for an MXFP4 graph, not just an fp32 approximation.

### 3.3 Op-body emit machinery (HIGH — demangled symbol chains)

Each cell is small. The body builds a scalar C++ lambda (the per-element kernel), hands it to an `ElementWise{Unary,Binary}Op` / `ElementwiseTernaryOp<…>` helper, which calls a populate routine on the destination literal:

```c
// HandleAdd<float,float> @0x87eaea0
function HandleAdd(inst):
    auto kernel = [](float a, float b) -> float { return a + b; };
    return ElementWiseBinaryOp(inst, kernel);
        // → MutableLiteralBase::PopulateLinearInternal<float,...>(AnyInvocable)   [serial]
        //   or PopulateLinearParallel<float,...>(...)                            [large literals]

// HandleSelect<int,long> @0x8995430
function HandleSelect(inst):
    auto kernel = [](bool p, int t, int f) -> int { return p ? t : f; };
    return ElementwiseTernaryOp<bool,T,T>(inst, kernel);   // → PopulateLinear*
```

The scalar kernel is wrapped in `absl::AnyInvocable` (`functional_internal::InvokeObject`/`VoidPtr`) and dispatched as a `{lambda(long,int)#1}` populate callback — the `(linear_index, thread_id)` pair the parallel populate path passes. Anchored to the full demangled chains at `0x87d33f0`, `0x87d6a60`, `0x87d6bd0`, `0x897c900`, `0x8984560`. The parallel path (`PopulateLinearParallel`) is the thread-fan-out for large tensors; the serial `PopulateLinearInternal` handles small ones.

### 3.4 Representative `HandleAdd` row

Each dtype's 44 `Handle*` methods are emitted contiguously, so the whole grid occupies the `~0x876xxxx`–`0x8ac7xxx` band. The `HandleAdd` column is a good landmark:

| dtype | `HandleAdd` | dtype | `HandleAdd` |
|---|---|---|---|
| `float,float` | `0x87eaea0` | `int,long` (s32) | `0x8995430` (HandleSelect anchor band) |
| `double,double` | `0x87cdac0` | `long,long` (s64) | `0x89f1640` |
| `Eigen::half,float` | `0x88dbfe0` | `signed char,long` (s8) | `0x8a10bd0` |
| `Eigen::bfloat16,float` | `0x876f950` | `short,long` (s16) | `0x8938de0` |
| `bool,bool` | `0x878bda0` | `unsigned char,ulong` (u8) | `0x8ac78c0` |
| `complex<float>` | `0x87b3120` | `unsigned short,ulong` (u16) | `0x8a6ad70` |
| `complex<double>` | `0x87a0090` | `unsigned int,ulong` (u32) | `0x8a89a20` |
| `float8_e4m3,float` | `0x88a2ac0` | `unsigned long,ulong` (u64) | `0x8aa92f0` |
| `float8_e5m2,float` | `0x88a3920` | `float8_e8m0fnu,float` | `0x8a473d0` |
| `float8_e4m3fn,float` | `0x88a37b0` | `float4_e2m1fn,float` (MXFP4) | `0x8a47820` |

### 3.5 Ops handled by base `HloEvaluator` (not templated, HIGH)

The non-element-wise / structural / control ops are virtual methods on `xla::HloEvaluator` itself (one implementation each, dtype-dispatched internally), *not* part of the 1320-cell grid:

```text
Sort SelectAndScatter ReduceWindow Reduce Scatter Map While Fusion Tuple
GetTupleElement Transpose Reshape Reverse Slice StochasticConvert
SetDimensionSize GetDimensionSize Parameter IsFinite Infeed Fft
```

Plus routing helpers: `EvaluateElementwiseBinaryOp` (`0x8752efe`), `MatmulArray`, `MakeDimMultipliers`, `Preprocess`/`Postprocess`, `TryEvaluate`, `EvaluateParameterFromCallerArgument`. The top-level entries are `xla::HloEvaluator::Evaluate` (`0x85c3870`) and the recursive per-instruction `EvaluateInternal` (`0x86bbb00`).

---

## 4. The Execution Backend — FunctionalHloRunner on PjRt Host-CPU

### Purpose

`hilo::runUsingMultihostHloRunner` (`0x1fc6680`, 6230 bytes, 310 bb) wraps `xla::FunctionalHloRunner::CompileAndRun` on a **PjRt Host-CPU client**. This is the real execution engine: the binary statically links the full `xla::cpu::*` Thunk compiler/runtime, and `HloEvaluator` is the reference/fallback interpreter for ops the Thunk JIT defers. The `FunctionalHloRunner` symbols are present and demangled in the binary (e.g. `FunctionalHloRunner::Run`, `RunInternal`, `FetchAndLogOutput`, `CopyArgumentsToDevice`, `ExecuteOnDevices`).

### Entry Point

```text
runUsingMultihostHloRunner (0x1fc6680)
  └─ xla::FunctionalHloRunner::CompileAndRun(client, module, args, RunningOptions)
       ├─ Compile → PjRtLoadedExecutable (xla::cpu Thunk)   "compilation started." / "compile succeeded."
       ├─ CopyArgumentsToDevice                              ── host literals → PjRt buffers
       ├─ ExecuteOnDevices(repeat = N)                       "ExecuteOnDevices started/succeeded (repeat = "
       └─ FetchAndLogOutput → vector<Literal>                ── device buffers → host literals
```

### Algorithm

```c
function runUsingMultihostHloRunner(module, inputs, spmd, emulate, numDevices):  // 0x1fc6680
    client   = CreateHostCpuPjRtClient(numDevices);       // emulated device count
    options  = BuildRunningOptions(spmd, repeat=1, ...);
    // log lines (verbatim): "FunctionalHloRunner: compilation started."
    executable = FunctionalHloRunner::Compile(client, module, options);
    // "FunctionalHloRunner: compile succeeded."
    // "FunctionalHloRunner: local_executable count = " <N>
    arg_buffers = CopyArgumentsToDevice(client, executable, inputs, options);
    // "FunctionalHloRunner: ExecuteOnDevices started (repeat = " <R>
    out_buffers = FunctionalHloRunner::Run(client, executable, arg_buffers, options, rng);
    //   rng = std::linear_congruential_engine<…, 16807, 0, 2147483647>  (minstd, seeds Rng ops)
    // "FunctionalHloRunner: ExecuteOnDevices succeeded (repeat = " <R>
    return FetchAndLogOutput(client, out_buffers, ModuleOutputMode);   // → vector<Literal>
```

> **NOTE —** the embedded `std::linear_congruential_engine<unsigned long, 16807, 0, 2147483647>` in the `Run` signature is the classic `minstd` PRNG. It is what seeds `HandleRng` so golden inference is deterministic across runs — a reference oracle for a graph containing dropout/random ops must be reproducible, or the BIR-sim compare would be meaningless.

---

## 5. CLI Surface and Literal I/O

### 5.1 The getopt grammar

`getopt_long` option string `m:t:i:o:I:v:esO:SuhUF` (@ `0x243574`), echoed by `printUsage` (`0x1fae080`):

| Short | Long | Arg | Default | Meaning |
|---|---|---|---|---|
| `-m` | `--module <file>` | file | (required) | HLO module file |
| `-t` | `--module-type <type>` | proto\|text\|snapshot | **proto** | input module format |
| `-i` | `--input <file>` | file(s) | — | module input(s) (`.npy`) |
| `-o` | `--output <name>` | name(s) | — | module output name(s) |
| `-I` | `--input-type <type>` | numpy | **numpy** | input file format |
| `-O` | `--out-dir <dir>` | dir | **`.`** | output directory |
| `-v` | `--verbose <level>` | int | — | verbose level |
| `-s` | `--save-intermediates` | — | **off** | dump all intermediate tensors |
| `-e` | `--emulate-multiple-devices` | — | **off** | emulate N devices on one CPU |
| `-S` | `--spmd` | — | **off** | SPMD mode (#inputs = #rank × #inputs) |
| `-u` | `--unsafe-dtype-cast` | — | **off** | load `.npy` via `.meta` in the real XLA dtype |
| `-U` | `--upcast-all-to-fp32` | — | **off** | upcast all FP ops to FP32 |
| `-F` | `--experimental-unsafe-fp8e4m3fn-as-fp8e4m3` | — | **off** | load `float8_e4m3` as `float8_e4m3fn` |
| `-h` | `--help` | — | — | help |

Banner echoes (verbatim): `Module: `, `Module Type: `, `Input Type: `, `Input Files (`, `Output Files (`, `Output Directory: `, `Verbose Level: `, `Save Intermediates: `, `Upcast All To FP32: `, `FP8 E4M3FN as E4M3: `, `Unsafe Dtype Cast: `, `Emulate Multiple Devices: `, `SPMD: `. Errors: `Unknown error parsing options`, `incorrect input type`, `Failed to parse proto: `, `Failed to parse snapshot: `, `Could not open: `, `Unexpected literal data type found: `, `[ERROR] [NCC_INFG001] Failed to run custom kernel: `, `[ERROR] [NCC_INFG002] `.

### 5.2 The NumPy ↔ Literal codec

```c
// npy2literal (read) — 0x78caa00
function npy2literal(path, unsafe):
    hdr = parse_npy_header(path);   // {'descr': D, 'fortran_order': False, 'shape': (...)}
    dtype = descr_to_xla(D);        // <f4 <f8 <f2 <i1 <i2 <i4 <i8 <u1 <u2 <u4 <u8 |b1
    if D in {|V1, |V2, |V4}:        // void-typed: ml_dtype NumPy can't express
        dtype = read_meta(path + ".meta");   // JSON {"ml_dtype": "float8_e4m3", ...}
    return Literal(dtype, shape, raw_bytes);

// literal2npy (write) — 0x78cc560
function literal2npy(lit, path, ..., metaPath):
    if lit.is_tuple(): decompose("It's a tuple"); recurse per element;
    write_magic("\x93NUMPY"); write_descr_and_shape(lit);
    write_raw(lit.data());
    if lit.dtype is ml_dtype: write_json(metaPath, {"ml_dtype": name});  // .meta sidecar
```

> **GOTCHA —** NumPy has no native dtype for the float8 / MXFP4 / packed-int types, so these are stored on disk as `|V1`/`|V2`/`|V4` *void* arrays and the true Neuron dtype is carried in a sibling `<file>.npy.meta` JSON (`{"ml_dtype": …}`). A reimplementer who reads only the `.npy` header will silently treat an `e4m3` golden as an opaque byte blob and any byte-level compare against a *differently-encoded* candidate will appear to "match" or "mismatch" for the wrong reason. The `.meta` sidecar is mandatory for the low-precision dtypes; `-u`/`--unsafe-dtype-cast` is the flag that opts into honoring it on read.

Output names: `value_output<i>.npy` (results), `value_intermediate<i>.npy` (with `-s`), `value_input*-000.npy` (staged inputs) — produced via the `/value_`, `/value_output`, `/value_output0` path roots in `main`.

---

## 6. Native-Kernel Simulation Splice

### Purpose

When the legalized module contains an `AwsNeuronCustomNativeKernel_Sim` custom-call (a Neuron NKI kernel), the host-CPU run cannot evaluate it numerically on its own — there is no XLA op for it. Instead, `xla_infergoldens` registers an XLA custom-call callback that **shells out to the Penguin/NKI simulator** and splices the simulator's output back into the HLO evaluation. So the *golden value of a Neuron native kernel is the Penguin simulator's output*, evaluated inline as part of computing the module's golden.

### Algorithm

```c
// jitSimulateNativeKernel — 0x2349b70  (XLA custom-call callback ABI: void(void* out, const void** ins))
function jitSimulateNativeKernel(out, ins):
    info = parseNativeKernelInfo(inst);            // 0x2347420 — backend-config → NativeKernelInfo
                                                   //   {kernel_name, dst_shapes, kernel_sim}
    results = simulateNativeKernel(info, input_literals);   // 0x2347f30
    copy_into(out, results);

// simulateNativeKernel — 0x2347f30
function simulateNativeKernel(info, inputs):
    dir = mkdtemp("/ksim_…");
    saveKernelSimConfig(info, dir);                // ksim_config.json {kernel_name, dst_shapes, kernel_sim}
    for (i, lit) in inputs: write_npy(dir + "/input" + i + ".npy", lit);
    run("cd '" + dir + "' && Running … penguin.py simulator.py --nki …");   // _xla_kexec_, func_literal
    if failed: raise "[NCC_INFG001] Failed to run custom kernel: ";
    return read_npy(dir + "/output0.npy");
```

Markers (verbatim): `/pgsim_value_`, `/penguin.py`, `/ksim_config.json`, `kernel_sim`, `kernel_name`, `dst_shapes`, `_xla_kexec_`, `func_literal`, `output0.npy`. Sim bookkeeping globals: `hilo::allKernels` (`0x9dd1ff0`), `hilo::kernelsPreprocessedForSimulation` (`0x9dd2008`), `simulateNativeKernel(...)::exec_counter` (`0x9dd1fec`).

> **QUIRK —** the golden generator is not a pure in-process computation: for Neuron native kernels it spawns a *separate* Penguin simulator subprocess per custom-call, writes inputs as `.npy`, and reads `output0.npy` back. The reference value of a kernel is therefore whatever the bit-accurate NKI simulator says it is — the golden inherits the simulator's fidelity. A `[NCC_INFG001]` failure here aborts golden generation for the whole module.

---

## 7. Golden → BIR-Sim Auto-Compare (the tolerance gate)

### The consumer side

The `value_output*.npy` goldens (pickled as `gold-000.p`, staged through the `XLAInferGoldens` job's state map) are consumed by the `WalrusDriver` job's BIR simulator, `birsim`. `birsim` re-executes the scheduled BIR, writes its per-tensor results to `*-birsim.npy`, and **auto-compares them against the goldens within a tolerance**, raising a mismatch on divergence. That comparison is the autotuner/correctness gate: golden = oracle, BIR-sim = candidate, tolerance compare = accept/reject.

```text
XLAInferGoldens job → xla_infergoldens -m <hlo> -i <in.npy> -o … -O <dir>
   → value_output*.npy / gold-000.p   (the goldens)
        → state map
             → WalrusDriver job → birsim runs scheduled BIR → *-birsim.npy
                  → compare(|golden − birsim| ≤ --birsim-output-tolerance)
                       → auto_compare_mismatch on divergence  (accept/reject)
```

The BIR-sim placement and compare toggles live on the **`birsim` side**, in the `walrus_driver` pass-flag vocabulary (see [walrus_driver Backend CLI](walrus-driver-cli.md)): `--enable-birsim` / `--enable-birsim-after-all` / `--enable-birsim-at-begin` / `--enable-birsim-at-end` / `--enable-birsim-sync-only`, `--enable-birsim-with-kernel-inline`, plus the `birsim` `cl::opt` belt flags `--birsim-output-tolerance` and `--birsim-output-flatten`.

> **GOTCHA — `--birsim-output-tolerance` is not a flag of this binary.** The comparison tolerance is the natural companion to golden production, so it is easy to look for here. A full strings sweep of the `xla_infergoldens` ELF finds no `birsim`-prefixed flag and no `auto_compare_mismatch`. The flag belongs to the `birsim` `cl::opt` belt on the consumer side — it appears in the `nki_klr_sim` binary alongside `birsim-output-flatten` and `enable-birsim-eigen-openmp`, and is registered in the `walrus_driver` pass infrastructure. `xla_infergoldens` *produces* goldens; `birsim` *applies* the tolerance.

---

## 8. Driver Job — `XLAInferGoldens.cpython-310.so`

Shipped only as a compiled Cython `.so` (no `.py` source in the wheel); symbols expose the full class/method graph. It derives from `neuronxcc.driver.Job` (state machine; `in_states`, `getFilesToStateMap`).

| Class | Key methods (verbatim qualnames) |
|---|---|
| `XLAInferGoldens(Job)` | `run`, `runSingleInput`, `getHloModule`, `getHloModuleInputDict`, `createHloModuleMappings`, `findOutputsHlo`, `writeNpys`/`writeNpy`, `checkNan`, `spmd`/`spmdRanks`/`getSpmdNpyNames`, `getFilesToStateMap`; analysis helpers `findConnectedScatterGather`, `findParameterFanIn`, `findInfMaskParam`, `findSelectPredParams` |
| `InferGoldenInstance` | **`getIgCmd`** (assembles the `xla_infergoldens` argv), `getInputArgs`, `getOutputArgs`, `getOutputDtypes`, `getSpmdArgs`, `getEmulateMultipleDevicesArgs`, `getUpcastAllToFP32Args`, `getFp8e4m3fnAsfp8e4m3Args`, `legalizeIntermediateDtype` |
| `ModularNet` | `getTopoSortedInstanceWrappers`, `getIntermediateIOs` (modular-flow ordering) |

CLI flags the job passes to the binary (verbatim): `--module`, `--module-type proto`, `--input`/`--input-type numpy`, `--output`, `--spmd`, `--emulate-multiple-devices`, `--upcast-all-to-fp32`, `--experimental-unsafe-fp8e4m3fn-as-fp8e4m3`.

**Input synthesis heuristics** (when no inputs are supplied — `getInputSpecFromHloModule`): a parameter is classified by shape/role and given synthetic data — bias vector, weight matrix/kernel, mask (binary random), select-predicate (all-1), `float8_e8m0fnu` scale (powers of 2), float-like (random in range), signed int (`[-10..10]`), unsigned int (`[0..10]`); with a NaN guard (`… has NaN in --images; make sure this is intended.`).

**Artifacts / naming:** input pickles `inp-000.p`; golden pickles `gold-000.p` (presence short-circuits a re-run: `Detected gold-000.p, dumping goldens and skipping XLAInferGoldens`); `value_output(\d+)\.npy$`, `value_intermediate(\d+)\.npy$`; modular-flow dirs `nc*/sg*/`, `hlo_netlist.json`, `metadata.json`. Progress: `Executing golden inference`, `Executing modular golden inference for partition `.

---

## 9. Reconstructed Signatures

```cpp
namespace hilo {
  struct XlaInfergoldensOptions {        // dtor @0x1faec00; field offsets not recovered [INFERRED]
    std::string module;        // -m
    int         moduleType;    // -t  {proto, text, snapshot}  (enum order MED)
    std::vector<std::string> inputs, outputs;   // -i, -o (repeatable)
    int         inputType;     // -I  {numpy}
    std::string outDir;        // -O  (default ".")
    int         verbose;       // -v
    bool        saveIntermediates, emulateMultipleDevices, spmd,
                unsafeDtypeCast, upcastAllToFP32, fp8e4m3fnAsFp8e4m3;
  };

  // golden engine (every address below resolves in the symbol table)
  void XLAInferGoldens(xla::HloModule*, std::vector<xla::Literal>&,
                       HiloInstrumentation*, const XlaInfergoldensOptions&);   // 0x1fc7f70
  bool runJitLegalizationPipeline(xla::HloModule*, HiloInstrumentation*, bool, bool, bool); // 0x1fc44c0
  void runUsingMultihostHloRunner(xla::HloModule*, std::vector<xla::Literal>&,
                                  bool spmd, bool emulate, int numDevices);    // 0x1fc6680
  int  getParticipatingNumDevices(xla::HloModule*);                           // 0x1fc5dc0
  void addToXlaFlags(const std::string& name, const std::string& value);      // 0x1fbe1b0

  // literal codecs
  xla::Literal npy2literal(const std::string& path, bool unsafe);             // 0x78caa00
  void literal2npy(const xla::Literal&, const std::string& path, bool, const std::string& meta); // 0x78cc560

  // native-kernel sim
  NativeKernelInfo parseNativeKernelInfo(const xla::HloInstruction*);          // 0x2347420
  std::vector<xla::Literal> simulateNativeKernel(const NativeKernelInfo&,
                                  absl::Span<const xla::Literal*>);            // 0x2347f30
  void jitSimulateNativeKernel(void* out, const void** ins);                   // 0x2349b70
}

// Reference interpreter (XLA upstream, statically linked):
template<class NativeT, class AccumT>
class xla::HloEvaluatorTypedVisitor : public xla::ConstDfsHloVisitorWithDefault {
  Status Handle<Op>(const HloInstruction*);   // 44 ops × 30 (NativeT,AccumT) = 1320 bodies
  // body → ElementWise{Unary,Binary}Op / ElementwiseTernaryOp<…>(lambda)
  //      → MutableLiteralBase::PopulateLinearInternal / PopulateLinearParallel<T,…>(AnyInvocable)
};
```

---

## 10. Evidence summary

The five strongest claims, re-challenged against the binary:

- **`main@0x1fb4fa0`** — `function_addresses.json` maps `0x1fb4fa0 → main`.
- **44 ops × 30 dtypes = 1320 `Handle*` bodies** — counting symbols matching `_ZN3xla24HloEvaluatorTypedVisitorI…E…Handle…EPKNS_14HloInstructionE` yields exactly **1320** methods, **44** distinct op names, and **30** distinct `<NativeT,AccumT>` instantiations. The grid is complete; there are no missing cells.
- **30 dtypes with the accumulator rule** — demangling the `HandleAdd` column yields all 30 `<NativeT,AccumT>` pairs in §3.2, including the float8 zoo, MXFP4, packed-int, and the int→`long` / float→`float` promotion rule.
- **Execution via `FunctionalHloRunner` on PjRt Host-CPU** — `RunInternal`, `Run`, `FetchAndLogOutput`, `CopyArgumentsToDevice`, and `ExecuteOnDevices` all demangle in the symbol table, and the log strings `FunctionalHloRunner: compilation started./compile succeeded./ExecuteOnDevices …` are present.
- **The compare gate lives in `birsim`, not here** — a strings sweep of the `xla_infergoldens` ELF finds no `birsim` flag, while `nki_klr_sim` carries it and the `walrus_driver` belt registers it.

## Limits of this reading

- The exact "JIT Legalization" pass list is **[INFERRED]**: only `TupleSimplifier` appears as a named ctor callee, so passes 49–53 are deduced from context.
- `XlaInfergoldensOptions` field offsets and enum order are **[INFERRED]**.
- The contents of the `getQuantizeMXDtypeOverrides` map are **[UNRESOLVED]**.
- The Cython job's `getIgCmd` argv ordering is string-derived rather than byte-traced.

---

## Related Components

| Name | Relationship |
|---|---|
| `walrus_driver` / `birsim` | Consumer of the goldens; applies `--birsim-output-tolerance` auto-compare |
| `XLAInferGoldens.cpython-310.so` | The driver job that builds the argv and invokes this tool |
| Penguin/NKI simulator (`penguin.py`/`simulator.py --nki`) | Computes the golden value of `AwsNeuronCustomNativeKernel_Sim` custom-calls |
| `xla::cpu::*` Thunk runtime | The primary host-CPU execution engine; `HloEvaluator` is the reference fallback |

## Cross-References

- [CompileCommand Pipeline & the Canonical Job Order](compilecommand-pipeline.md) — where the InferGoldens job sits in the job order
- [walrus_driver Backend CLI & Pass Vocabulary](walrus-driver-cli.md) — the `--enable-birsim*` / `--birsim-output-tolerance` flags on the consumer side
- [The Compile Pipeline at a Glance](../front/pipeline.md) — overall driver→backend flow
- [Snapshot / Decomposed Input Format](snapshot-input-format.md) — the `-t snapshot` module/input format this tool consumes
