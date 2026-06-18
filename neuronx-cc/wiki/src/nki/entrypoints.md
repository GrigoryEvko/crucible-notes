# NKI Entrypoints: `jit` / `baremetal` / `benchmark` / `profile` / `simulate_kernel`

> *All symbols, strings, and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython extension modules under `neuronxcc/nki/`). Other wheels differ; every name is version-pinned, every string verbatim from the `.so` pools unless tagged otherwise.*

## Abstract

NKI ships **five** user-facing front doors off the `neuronxcc.nki` package. One of them — `nki.jit` — is framework-agnostic and **dispatches at call time on the runtime argument types** to one of five concrete kernel classes. The other four (`baremetal`, `benchmark`, `profile`, `simulate_kernel`) name a fixed execution mode directly. This page is the *entrypoint and dispatch* map: what each callable is, which `.so` defines it, which kernel class it lands on, and — for `jit` — the exact argument-type → class decision. The **trace → compile → cache lifecycle** that all five share (the `TraceKernel`/`FrameworkKernel`/`NumpyKernel` class stack, `dump_config`, the base64 `backend_config`, the specialization cache) is [page 6.0.3](./framework-kernel-lifecycle.md); this page stops at the door and the dispatcher.

The single fact that organizes everything: NKI has two execution families. `jax`/`torchxla` modes lower the kernel to an **XLA `custom-call` HLO instruction** and never run anything locally — the framework's own compiler/runtime executes it later. `baremetal`/`benchmark`/`profile`/`simulation` modes **compile and/or run the kernel locally on `numpy.ndarray` I/O**, either shelling out to `neuronx-cc` and running the NEFF on a real NeuronCore, or running the in-process CPU simulator. `jit` is the only entrypoint that can land in either family; the other four are pinned to the local family.

| | |
|---|---|
| **Module split** | `nki/__init__.so` defines `jit`, `baremetal`, `benchmark`, `profile`, `_jax_jit`, `_torchxla_jit`; `nki/compile.so` defines `compute_mode`, `GenericKernel`, `simulate_kernel` (re-exported by `__init__`) |
| **Dispatcher** | `neuronxcc.nki.compile.compute_mode` (`_native_mode_map` + arg-type detection) |
| **Mode set** | `"jax"`, `"torchxla"`, `"baremetal"`, `"benchmark"`, `"simulation"`, `"auto"` (verbatim docstring) |
| **JAX target** | HLO `custom_call_target = "AwsNeuronCustomNativeKernel"` (`_jax.so`) |
| **torch-xla target** | HLO `custom_call_target = "AwsNeuronNkiKernel"` (`_torch_xla.so`) |
| **Local exec base** | `BaremetalKernel` (`NumpyKernel.so`) → `neuronx-cc compile … penguin.py` → NEFF |

> **CORRECTION (D-W12 §1.1 → binary).** W12 states that "`nki.jit`, the dispatcher, `GenericKernel` and `simulate_kernel` actually live in `nki/compile.so`." Only **partly** true. The `compute_mode` dispatcher, `_native_mode_map`, and the `GenericKernel` class **are** in `compile.so` (symbols `__pyx_pf_…compile_4compute_mode`, `__pyx_k_native_mode_map`, `neuronxcc.nki.compile.GenericKernel.__call__`). But the **`jit` decorator wrapper itself**, the `_jit_dispatch` helper, the `jit` docstring, *and* the dispatch error string `' passed to nki.jit'` live in **`nki/__init__.so`** — symbol `__pyx_pw_9neuronxcc_3nki_9jit` (= `neuronxcc.nki.jit`, note `_3nki_9jit`, not `_7compile_`). `__init__.so` imports `GenericKernel`/`simulate_kernel` from `neuronxcc.nki.compile` (string `neuronxcc.nki.compile` present in `__init__.so`). So: **`jit` and its dispatch glue are in `__init__.so`; the `compute_mode` algorithm and `GenericKernel` are in `compile.so`.** A reimplementer placing the whole of `jit` in `compile.so` will not match the symbol table.

## The five entrypoints

Every public name is a Cython `CyFunction` wrapper. For the four mode-pinned doors, the wrapper forwards to a **module-global singleton kernel object's `.trace`**; for `jit` it forwards into the `compute_mode` dispatcher. The table is the authoritative surface — each entry verified by its `__pyx_pw_*` wrapper symbol and (where present) the verbatim docstring/string in the named `.so`.

| Entrypoint | Defining `.so` | Wrapper symbol | Positional | Lands on | Family |
|---|---|---|---|---|---|
| `jit(func=None, mode="auto", **kwargs)` | `nki/__init__.so` | `__pyx_pw_9neuronxcc_3nki_9jit` | `func` | `GenericKernel` → (dispatched) | either |
| `baremetal(kernel=None, **kwargs)` | `nki/__init__.so` | `nki_*baremetal` (`@0x9ef0`) | `kernel` | `BaremetalKernel` | local |
| `benchmark(kernel=None, **kwargs)` | `nki/__init__.so` | `nki_*benchmark` (`@0xa7e0`) | `kernel` | `BenchmarkKernel` | local |
| `profile(func=None, **kwargs)` | `nki/__init__.so` | `nki_*profile` (`@0x9600`) | `func` | `ProfileKernel` | local |
| `simulate_kernel(kernel, *args, **kwargs)` | `nki/compile.so` | `compile_*simulate_kernel` | `kernel` | `SimulateKernel` | local (CPU) |
| `_jax_jit(func, grid=…)` | `nki/__init__.so` | `nki_*_jax_jit` (`@0xd040`) | `func` | `JAXKernel` | XLA (internal) |
| `_torchxla_jit(func, grid=…)` | `nki/__init__.so` | `nki_*_torchxla_jit` (`@0xc2a0`) | `func` | `PyTorchXLAKernel` | XLA (internal) |

The four mode-pinned wrappers are **structurally identical** — the only differences are the positional argument name (`kernel` vs `func`), the singleton they forward to (`_baremetal_kernel` / `_benchmark_kernel` / `_profile_kernel`), and the kernel class that singleton is an instance of. `__init__.so`'s string pool carries `_baremetal_kernel`, `_benchmark_kernel`, and the docstring lines that pin every default. CONFIRMED.

```c
// nki/__init__.so — the mode-pinned wrapper pattern (profile shown; baremetal/benchmark identical)
// __pyx_pw_…_profile @0x9600 ; module singleton _profile_kernel : ProfileKernel
static PyObject *nki_profile(PyObject *func /*=None*/, PyObject *kwargs) {
    // forwards the decorated fn + splatted kwargs straight to the singleton's bound .trace
    return ProfileKernel_trace(_profile_kernel, func, kwargs);   // TraceKernel.trace decorator factory
}
// func is None when used as `@nki.profile(working_directory=…)`  -> trace returns a partial;
// func is the kernel when used as bare `@nki.profile`.
```

### `nki.jit` — the framework-agnostic door

`jit` is the only entrypoint that auto-detects. Its docstring (verbatim, `__init__.so`):

> *"This decorator compiles a function to run on NeuronDevices. This decorator tries to automatically detect the current framework and compile the function as a custom operator of the current framework. To bypass the framework detection logic, you may specify the `mode` parameter explicitly."*
> `:param mode:` *"The compilation mode, possible values: `"jax"`, `"torchxla"`, … `"baremetal"`, `"benchmark"`, `"simulation"` and `"auto"`"*

`jit` supports both forms: bare `@nki.jit` (`func` given) and parametrized `@nki.jit(mode=…)` (`func=None` → returns a partial). Either way it returns a **`GenericKernel`** (defined in `compile.so`). `GenericKernel` is **subscriptable** — its type object installs `mp_subscript` (confirmed in `compile.so`), implementing `__class_getitem__` so `nki.jit[...]` type-parameter syntax is legal. CONFIRMED.

`GenericKernel.__call__` does **not** compile eagerly. On each call it consults `compute_mode` to pick the concrete kernel class for the live argument types, then memoizes it: the `__call__` references `compute_mode`, `build_cache_kernel`, and `_cached_kernel` (all CONFIRMED symbols in `compile.so`). So one `@nki.jit` object can resolve to different backend classes across calls if you feed it numpy on one call and a torch tensor on the next — and it caches the resolved kernel per resolution.

```c
// nki/compile.so — GenericKernel.__call__ (annotated; symbols CONFIRMED, control flow STRONG)
PyObject *GenericKernel___call__(GenericKernel *self, PyObject *args, PyObject *kwargs) {
    KernelClass cls = compute_mode(self->mode, args, kwargs);   // resolve concrete class
    Kernel *k = self->_cached_kernel.get(cls);                  // memoized per resolved class
    if (!k) {
        k = self->build_cache_kernel(cls, ...);                 // construct + store
        self->_cached_kernel[cls] = k;
    }
    return k->__call__(args, kwargs);                           // delegate to the concrete kernel
}
```

## The dispatch algorithm — `compute_mode`

`compute_mode` (`neuronxcc.nki.compile.compute_mode`, `compile.so`) is the heart of NKI's "front door" behavior. It has two paths: an **explicit-mode lookup** and an **argument-type auto-detection**. The class table is `_native_mode_map` (CONFIRMED symbol `__pyx_k_native_mode_map`). The auto path's detection strings — `jax`, `get_backend`, `platform`, `neuron`, `Array`, `ArrayImpl`, `Tracer`, `torch` — are all present verbatim in `compile.so`, and the platform guard error `nki only support on neuron, current target: ` is the verbatim string emitted when JAX is detected but its backend platform is not `"neuron"`.

```c
// nki/compile.so — compute_mode (symbols + detection strings all CONFIRMED; precise branch order STRONG)
KernelClass compute_mode(const char *mode, PyObject *args, PyObject *kwargs) {

    // ── PATH (a): explicit mode string ───────────────────────────────────────
    if (mode != "auto") {
        KernelClass cls = _native_mode_map.get(mode);   // dict: name -> class object
        //   "jax"        -> JAXKernel             (XLA custom-call; never runs locally)   §_jax.so
        //   "torchxla"   -> PyTorchXLAKernel      (XLA custom-call)                        §_torch_xla.so
        //   "baremetal"  -> BaremetalKernel       (NumpyKernel family; compile+run NEFF)
        //   "benchmark"  -> BenchmarkKernel
        //   "simulation" -> SimulateKernel        (CPU simulator, no NEFF)
        if (cls == NULL)
            raise(... + mode + "' passed to nki.jit");  // error string lives in __init__.so wrapper
        return cls;
    }

    // ── PATH (b): mode == "auto" — inspect the RUNTIME ARGUMENT TYPES ─────────
    // (compute_mode has four generator sub-closures __pyx_gb_…compute_mode_{,1,2,3} — the
    //  per-argument type-test scans over args/kwargs.)
    for (each arg a in args, kwargs) {
        if (is_jax_tensor(a)) {                     // jax Array / ArrayImpl / Tracer
            if (jax.get_backend().platform != "neuron")
                raise("nki only support on neuron, current target: " + platform);  // CONFIRMED string
            return JAXKernel;
        }
        if (is_torch_tensor(a))                      // torch.is_tensor(a)
            return PyTorchXLAKernel;
    }
    return BaremetalKernel;                           // default: numpy / no framework tensor
}
```

> **QUIRK — auto-detection is argument-driven, not import-driven.** `compute_mode` does **not** ask "is JAX installed?"; it asks "did this *call* pass a JAX `Array`/`ArrayImpl`/`Tracer`, *and* is JAX's active backend the `neuron` platform?". Only then does it pick `JAXKernel`. A torch tensor in any argument selects `PyTorchXLAKernel`. Anything else (numpy arrays, or no framework tensor at all) falls through to `BaremetalKernel`. This is why a single `@nki.jit` kernel can compile to an HLO custom-call under JAX and run a local NEFF under numpy *with no code change* — the decision is per call, per argument.

> **GOTCHA — the JAX path requires the `neuron` platform, the torch path does not check.** The `jax` branch is guarded by `jax.get_backend().platform == "neuron"` and raises `nki only support on neuron, current target: <platform>` otherwise. The torch branch has no analogous platform guard in `compute_mode` — `PyTorchXLAKernel.__init__` instead resolves the platform target later from `torch_neuronx` (see 6.0.3). So a JAX user on a non-Neuron backend gets an immediate, explicit failure here; a torch-xla user's target resolution is deferred into the kernel constructor.

### `_native_mode_map` — the explicit table

| `mode` string | Class | Family | Defining `.so` |
|---|---|---|---|
| `"jax"` | `JAXKernel` | XLA custom-call | `_jax.so` |
| `"torchxla"` | `PyTorchXLAKernel` | XLA custom-call | `_torch_xla.so` |
| `"baremetal"` | `BaremetalKernel` | local NEFF | `NumpyKernel.so` |
| `"benchmark"` | `BenchmarkKernel` | local NEFF + latency | `NumpyKernel.so` |
| `"simulation"` | `SimulateKernel` | CPU simulator | `NumpyKernel.so` |
| `"auto"` | *(arg-type detection above)* | — | — |

Unknown mode → the wrapper raises with the verbatim fragment `' passed to nki.jit'`. A *second* error string `Unknown framework '` is also present in `__init__.so` — the framework-detection failure message distinct from the mode-string failure. CONFIRMED both strings; exact wording of the surrounding text is split across rodata (STRONG).

## The two XLA custom-call targets

When `jit` resolves to `JAXKernel` or `PyTorchXLAKernel`, no kernel runs. The kernel is *traced* (to learn output shapes/dtypes and to serialize its IR), and an XLA `custom-call` HLO instruction is emitted carrying the traced program in its `backend_config`. The two bridges emit **different `custom_call_target` names** — this is the single most reimplementation-critical divergence between them:

| Bridge | `.so` | `custom_call_target` | HLO builder | Hooks |
|---|---|---|---|---|
| JAX | `_jax.so` (`JAXKernel`) | **`AwsNeuronCustomNativeKernel`** | `stablehlo.custom_call` (via `jaxlib.mlir`) | `is_framework_tensor`, `map_framework_tensor`, `translate_to_neuron_dtype` |
| torch-xla | `_torch_xla.so` (`PyTorchXLAKernel`) | **`AwsNeuronNkiKernel`** | `scribe` `CustomCall` + `OutputOperandAliasing` | same three hooks + `build_xla_type` |

Both target strings are CONFIRMED at their definition sites: `_jax.so` carries `AwsNeuronCustomNativeKernel` (symbols `__pyx_n_u_AwsNeuronCustomNativeKernel`); `_torch_xla.so` carries `AwsNeuronNkiKernel` (`__pyx_n_s_AwsNeuronNkiKernel`). The `FrameworkKernel` base-class docstring itself shows the JAX form in its worked HLO example: `custom_call_target="AwsNeuronCustomNativeKernel"` (verbatim in `FrameworkKernel.so`).

> **GOTCHA — the two targets are NOT interchangeable.** JAX emits `AwsNeuronCustomNativeKernel`; torch-xla emits `AwsNeuronNkiKernel`. They carry the *same* base64-JSON `backend_config` payload (both produced by the shared `dump_config` — see 6.0.3) and the same operand-output aliasing intent, but the call-target string the downstream XLA/runtime matches on is different per framework. A reimplementer wiring the runtime side must register **both** custom-call names. The W12 in-text note flagging this divergence is correct and confirmed at both binaries.

### The JAX `nki_call` primitive

The JAX bridge registers NKI as a proper **JAX primitive**, `nki_call_p`, with the two-rule structure JAX requires (`apply_primitive` / `def_abstract_eval` both present in `_jax.so`). CONFIRMED symbols:

- **`nki_call_eval`** — the abstract-eval rule. Traces the kernel (via `dump_config`) only to learn the output `return_types`, and returns JAX `ShapedArray` avals — **no real compute**. The trace result is wrapped hashable so JAX can cache the lowering.
- **`nki_call_lowering_rule`** — the MLIR lowering rule. Emits the `stablehlo.custom_call` with `call_target_name = "AwsNeuronCustomNativeKernel"`, the base64 `backend_config`, the operand-output aliasing, and `mhlo.frontend_attributes`.

> **QUIRK — a JAX-only trace-cache escape hatch.** `_jax.so` carries the verbatim message `NKI trace is not properly cached, use ``export NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING=TRUE`` to workaround the issue` plus the env-name `NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING` and the internal flag `_dont_cache_trace_for_jax_lowering`. Setting that env var to `TRUE` bypasses a trace-caching defect on the JAX lowering path specifically. There is no torch-xla analogue. CONFIRMED.

The torch-xla bridge has no JAX-style primitive; `PyTorchXLAKernel.__call__` builds the `CustomCall` directly through the torch-xla `scribe` HLO builder, attaching `OutputOperandAliasing` for in-place donation and an `excluded_output_tensors` set (string CONFIRMED in `_torch_xla.so`) for outputs that need not be materialized. The module also exposes a top-level `nki_jit` (symbol `neuronxcc.nki._torch_xla.nki_jit`).

## The local-execution doors

`baremetal`, `benchmark`, `profile`, and `simulate_kernel` all land in the `NumpyKernel` family (`NumpyKernel.so`) and take **`numpy.ndarray` I/O**. Three of them (`baremetal`/`benchmark`/`profile`) share one spine — compile the traced IR to a NEFF via a `neuronx-cc` shell-out, then run it on a real NeuronCore — differing only in *how* they run it. `simulate_kernel` skips the device entirely. The class lineage and what each overrides is the subject of 6.0.3; here is the entrypoint-level contract for each.

### `nki.baremetal` — compile once, run once

Docstring (verbatim, `__init__.so`): *"Compile and run a NKI kernel on NeuronDevice without involving ML frameworks such as PyTorch and JAX."* It compiles the kernel into a NEFF and runs it once on the local device; expects `numpy.ndarray` in/out, returns `None` (in-place outputs). kwargs: `save_neff_name`, `save_trace_name`, `additional_compile_opt`, `artifacts_dir`. The compile is a literal shell-out — the verbatim command template lives in `NumpyKernel.so`:

```
neuronx-cc compile --framework XLA penguin.py --internal-tensorizer-opt-level=nki --pipeline compile SaveTemps --target <arch>
```

The `penguin.py` file is the serialized **Penguin IR** that `write_tensorizer_ir` emits into the temp dir before the shell-out. `additional_compile_opt` is appended to this command; the target arch is filled in from the resolved platform target.

> **CORRECTION — `write_tensorizer_ir` emits Penguin IR, not BIR.** The label was loosely "Penguin/BIR"; the decompiled body of `NumpyKernel.write_tensorizer_ir` (`NumpyKernel.py:43–45`, decompile + disasm) pins it down: it does `open(os.path.join(dir, "penguin.py"), "w")` — the filename constant is literally `penguin.py` — and writes via `IRWriter.run(...)`, where `IRWriter` resolves to `neuronxcc.starfish.penguin.ir.IRWriter` (the **Penguin** IR text writer, under `penguin/ir/`). So the artifact this function writes is the **Penguin tensorizer-IR** dump. It is **distinct from** the `<fn>.TensorizerBIR.json` artifact: that BIR (`bir::Module`) dump is produced *later*, *inside* the `neuronx-cc compile … penguin.py` shell-out, by `BirCodeGenLoop.runOnFunction` (cf. [bircodegenloop](./bircodegenloop.md) §; the Penguin→BIR lowering). The wheel-side `write_tensorizer_ir` never touches BIR. CONFIRMED (decompile of `sub_1AE60` + the `penguin.py`/`IRWriter`/`os.path.join`/`open` constants at `0x1b08d`/`0x1c206`).

> **GOTCHA — `save_trace_name` forces `save_neff_name`.** The `baremetal` docstring carries the verbatim *"Known issue: if `save_trace_name` is specified, `save_neff_name` must be set to "file.neff"."* If you ask `baremetal` to keep the NTFF execution trace, you must also leave the NEFF name at its default. CONFIRMED string.

### `nki.benchmark` — same spine, plus latency

Docstring: *"Benchmark a NKI kernel on a NeuronDevice … uses the same underlying mechanism as `nki.baremetal` but additionally collects latency statistics …"*. It additionally invokes `neuron-bench` (`NeuronBench`, from `neuronxcc.kra.profile_lib`) and runs `warmup` + `iters` executions for timing. kwargs: `warmup` (default 10), `iters` (default 100), `save_neff_name`, `save_trace_name`, `additional_compile_opt` — the docstring shows `@benchmark(warmup=10, iters=100, …)`.

The returned wrapper exposes `.benchmark_result.nc_latency` — a `BenchmarkResult` whose `nc_latency` is an `NCLatency` (or `None` if `iters` is too low). `NCLatency.get_latency_percentile(p)` returns the p-th percentile in **microseconds**. The percentile set is documented verbatim in `NCLatency.get_latency_percentile`'s docstring: *"result_dicts must be in form of {x: <Latency in us>} where x is the percentile. For example, p0, p1, p10, p25 …"* — i.e. `p ∈ {0,1,10,25,50,90,99,100}`. `NCLatency.__str__` prints a five-number summary. All symbols (`benchmark_result`, `NCLatency`, `get_latency_percentile`, `result_dicts`, `NeuronBench`) CONFIRMED in `NumpyKernel.so`.

> **NOTE — single NeuronCore, shapes-only.** `benchmark` runs on a single NeuronCore (NKI's collective compute is not supported on the benchmark path) and, like all three device doors, **does not use the real input values** when running the NEFF — only the shapes matter for timing. The docstring states *"`nki.benchmark` does not use the actual inputs passed into the benchmarked function when running the [NEFF]"* (verbatim). Outputs are undefined under benchmark. CONFIRMED.

### `nki.profile` — full Neuron Profile capture (`.ntff` producer)

`profile` is the richest local door. kwargs: `working_directory` (**REQUIRED** — `ProfileKernel.__init__` raises `ValueError` *"Please specify working directory"* if `None`), `save_neff_name` (default `file.neff`), `save_trace_name` (default `profile.ntff`), `additional_compile_opt`, `overwrite` (default `False`), `profile_nth`, plus an internal `num_execs`. It writes `file.neff`, `profile.ntff`, and JSON profile-summary files into `working_directory`.

The producer boundary is the key fact and is owned by D-AD06: **the wheel triggers the capture; the runtime/firmware writes the `.ntff` bytes.** `ProfileKernel.execute_neff` (CONFIRMED decompiled body, `NumpyKernel.py:333`) acquires the device lock, constructs `neuronxcc.kra.profiler.Profiler(ntff_name, work_dir, num_execs, profile_nth, overwrite)`, then calls `generate_profile()` (which builds and runs an external `neuron-profile capture --num-exec --output-file …` command line — the on-device NRT `modelExecute` writes the `.ntff`) and `extract_ntff_json_from_profile()` (re-invokes `neuron-profile` to dump the per-NTFF JSON summaries). It then defers to `super().execute_neff` for the base device run.

```c
// NumpyKernel.so — ProfileKernel.execute_neff (py333; CONFIRMED decompile, see D-AD06)
void ProfileKernel_execute_neff(self, neff, ir, boundargs) {
    super_try_lock_device(self);                         // acquire NeuronDevice lock
    Profiler *p = Profiler(/*ntff_name=*/ self->save_trace_name ?: "profile.ntff",
                           /*work_dir=*/   self->work_dir,
                           /*num_execs=*/  self->num_execs,
                           /*profile_nth=*/self->profile_nth,
                           /*overwrite=*/  self->overwrite);
    p->generate_profile();                  // *** triggers external `neuron-profile capture` -> .ntff ***
    p->extract_ntff_json_from_profile();    // *** JSON summaries (re-invoke neuron-profile) ***
    self->save_neff_name  = false;          // consume-once: base must not re-copy
    self->save_trace_name = false;
    print("Profile results are written to " + self->work_dir);
    return super_execute_neff(self, neff, ir, boundargs);   // base NRT modelLoad/modelExecute
}
```

> **NOTE — `profile_nth` is exec-index selection, not a Python timer.** `num_execs` is the number of on-device runs the runtime performs; `profile_nth` selects **which** of those N runs to keep the profile for (skip warm-up / pick steady-state). The selection happens inside `Profiler` (`_get_specific_ntff` for a specific `profile_nth`, else `_get_all_ntffs`), not in a Python sampling loop. The `.ntff` **binary layout** and the Go `neuron-profile` *consumer* are out-of-wheel (`aws-neuronx-tools`) and deliberately unspecified — see [Part 8 perf-sim](../walrus/) and D-AD06. The wheel owns the *request* and the NEFF; the runtime owns the `.ntff` *bytes*.

### `nki.simulate_kernel` — CPU simulator, no device

`simulate_kernel` is **not a decorator** — it is a direct function call, `simulate_kernel(kernel, *args, **kwargs)`, defined in `compile.so` (symbol `neuronxcc.nki.compile.simulate_kernel`) and re-exported as `nki.simulate_kernel`. Docstring (verbatim, `compile.so`):

> *"Simulate a nki kernel on CPU using a built-in simulator in Neuron Compiler. This simulation mode is especially useful for inspecting intermediate tensor values using `nki.language.device_print`. … All input and output tensors to the kernel must be `numpy.ndarray` when using this `simulate_kernel` API."*

It is backed by `SimulateKernel` (`NumpyKernel.so`), which **overrides `post_process_call`** (not `execute_neff`) — it compiles nothing and touches no device. Instead it builds an `IRSimulator` (the in-process BIR interpreter), runs the traced IR on CPU, streams `device_print` output to a `dump_file`, and copies simulated results back into the numpy arrays. CONFIRMED symbols: `SimulateKernel.__init__`, `SimulateKernel.post_process_call`, `IRSimulator`, `dump_file`, `_dump_file`. This is the only door that needs no hardware and the only one where intermediate tensor values are inspectable.

## Where this connects

- The **trace → compile → cache lifecycle**, `dump_config`/`dump_config_with_boundargs`, the base64 `backend_config` payload, operand-output aliasing, and the per-instance specialization cache (`__neuron_kernel_interface_kernel_cache__`) — all shared by every entrypoint above — are [6.0.3 NKI Framework-Kernel Lifecycle](./framework-kernel-lifecycle.md).
- The broader XLA front door (`libneuronpjrt`, how the framework consumes the emitted custom-call) is [3.13 frontend/framework-bindings](../frontend/framework-bindings.md).
- The NKI → **Penguin IR** codegen that runs *inside* the trace (`GeneratedNeuronCodegen`, `KernelBuilder`, `write_tensorizer_ir`/`IRWriter`) is the Part-6 codegen pages. (The Penguin→BIR lowering happens later, in the `neuronx-cc compile … penguin.py` shell-out via `BirCodeGenLoop` — see the CORRECTION above.)
- The `neuron-profile` `.ntff` consumer and the `walrus`/perf-sim trace artifacts are [Part 8 perf-sim](../walrus/).

## Adversarial self-verification

The five strongest claims on this page, re-challenged against the binary:

1. **`jit`'s wrapper + dispatch error are in `__init__.so`, not `compile.so`.** Re-checked: `__pyx_pw_9neuronxcc_3nki_9jit`, `_jit_dispatch`, `' passed to nki.jit'`, `Unknown framework '`, and the `jit` docstring are all in `__init__.so`; `compute_mode`/`_native_mode_map`/`GenericKernel` are in `compile.so`. Correction issued in-place. **CONFIRMED.**
2. **The two custom-call targets differ: JAX=`AwsNeuronCustomNativeKernel`, torch-xla=`AwsNeuronNkiKernel`.** Re-checked at definition sites: `_jax.so` → `AwsNeuronCustomNativeKernel` (also in `FrameworkKernel.so` docstring example); `_torch_xla.so` → `AwsNeuronNkiKernel`. **CONFIRMED.**
3. **Auto-dispatch is argument-type based: jax Array/ArrayImpl/Tracer→JAXKernel, torch→PyTorchXLAKernel, else→BaremetalKernel.** Re-checked: `Array`, `ArrayImpl`, `Tracer`, `get_backend`, `platform`, `neuron`, `torch`, `JAXKernel`, `PyTorchXLAKernel`, `BaremetalKernel` all in `compile.so`; the JAX-platform guard string `nki only support on neuron, current target: ` is verbatim. Branch *order* (jax-before-torch) is STRONG (from the four `compute_mode` generator closures), not byte-exact. **CONFIRMED (order STRONG).**
4. **`baremetal`'s compile is `neuronx-cc compile --framework XLA penguin.py …`.** Re-checked: full command template `neuronx-cc compile --framework XLA penguin.py --internal-tensorizer-opt-level=nki --pipeline compile SaveTemps --target ` is one verbatim string in `NumpyKernel.so`. **CONFIRMED.**
5. **`profile` triggers but does not write the `.ntff`; the runtime does.** Re-checked: `ProfileKernel.execute_neff` calls `Profiler(...).generate_profile()`/`extract_ntff_json_from_profile()`; the `.ntff` bytes are produced by the external `neuron-profile capture` → NRT `modelExecute`, out-of-wheel (D-AD06). `simulate_kernel` overrides `post_process_call` with `IRSimulator`, never compiling. **CONFIRMED.**
