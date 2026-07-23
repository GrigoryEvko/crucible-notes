# Framework Bindings — JAX, PyTorch-XLA & Custom-Call Targets

> *All symbols, strings, and offsets on this page apply to `neuronx_cc` **2.24.5133.0+58f8de22** (cp310/311/312; analysis on cp310 unless noted). The NKI binding layer ships as Cython-compiled `.so` modules under `neuronxcc/nki/`; signatures are cross-checked against the authoritative `neuronx_cc_stubs` `.pyi` files. Other versions will differ.*

## Abstract

NKI (the Neuron Kernel Interface) is a Python-embedded kernel language whose programs must eventually run on a Trainium/Inferentia NeuronCore. There are two ways a kernel reaches the device: it is either **traced, compiled to a NEFF, and run locally** (the numpy "baremetal" family), or it is **embedded into a host ML program's XLA graph as a single opaque HLO `custom-call`** that the Neuron compiler backend expands at graph-compile time. This page documents the second path in full and the dispatch that chooses between them — the surface that JAX and PyTorch-XLA actually touch.

The mechanism is deliberately framework-agnostic at its core. A user decorates a Python function with `@nki.jit`; on each call, `compute_mode` (`nki/compile.so`) inspects the *runtime argument types* and picks a concrete kernel class — `JAXKernel`, `PyTorchXLAKernel`, or `BaremetalKernel`. The two framework classes share a base, `FrameworkKernel`, whose job is to **trace the kernel once per (shape, dtype, grid) specialization and serialize the traced Neuron IR into a base64 JSON blob**. That blob becomes the `backend_config` of an XLA `custom-call`. The only thing that differs between the JAX and the PyTorch-XLA bridge is (a) which HLO builder emits the instruction (`jaxlib` stablehlo vs the torch-xla `scribe` builder) and (b) the literal `custom_call_target` string: JAX emits `"AwsNeuronCustomNativeKernel"`, PyTorch-XLA emits `"AwsNeuronNkiKernel"`. The same `backend_config` payload feeds both.

If you know XLA, the model is exactly the familiar one: NKI is a custom op registered with a backend-config-carrying call target, and the buffer-donation / output-aliasing machinery is XLA's standard operand-output aliasing. The novelty is *what travels in the backend_config* — not a small struct of attributes but the **entire traced kernel program**, serialized as BIR/penguin IR text inside a base64 JSON string. The host framework never sees the kernel body; it ships the IR to the Neuron compiler, which re-reads it from the custom-call config. This page reconstructs the dispatch, the trace→config lifecycle, the two bridges, the alloc decorators, and the collectives surface, and draws the out-of-wheel boundary precisely.

For reimplementation, the contract is:

- The **argument-type compute-mode dispatch**: how `@nki.jit` picks `JAXKernel` / `PyTorchXLAKernel` / `BaremetalKernel` per call.
- The **`FrameworkKernel` lifecycle**: `dump_config` → trace → `encode_backend_config` → base64(JSON), including the full config-key set and the operand-output aliasing.
- The **two custom-call contracts**: target name `AwsNeuronCustomNativeKernel` (JAX, via stablehlo) vs `AwsNeuronNkiKernel` (PyTorch-XLA, via scribe), both carrying the same base64-BIR `backend_config`.
- The **numpy execution family** (`baremetal`/`benchmark`/`profile`/`simulate`) and its `neuronx-cc compile … penguin.py` shell-out.
- The **alloc decorators** and the **distributed/nccl collectives** surface that flips `has_collectives` in the config.

| | |
|---|---|
| **Dispatch module** | `neuronxcc.nki.compile` (`nki/compile.cpython-310-*.so`) — `compute_mode`, `GenericKernel`, `_native_mode_map` |
| **HLO emitter base** | `neuronxcc.nki.compiler.backends.neuron.FrameworkKernel` (`FrameworkKernel.so`) |
| **Trace engine base** | `neuronxcc.nki.compiler.backends.neuron.TraceKernel` (`TraceKernel.so`) |
| **JAX bridge** | `neuronxcc.nki._jax` (`_jax.so`) — `JAXKernel`, `nki_call_eval`, `nki_call_lowering_rule` |
| **PyTorch-XLA bridge** | `neuronxcc.nki._torch_xla` (`_torch_xla.so`) — `PyTorchXLAKernel`, `nki_jit` |
| **JAX call target** | `"AwsNeuronCustomNativeKernel"` (stablehlo `custom_call`) |
| **PyTorch-XLA call target** | `"AwsNeuronNkiKernel"` (torch-xla `scribe` `CustomCall`) |
| **backend_config encoding** | `base64.b64encode(json.dumps(cfg).encode()).decode()` — carries serialized BIR |
| **Numpy exec family** | `neuronxcc.nki.compiler.backends.neuron.NumpyKernel` (`NumpyKernel.so`) |
| **Local compile cmd** | `neuronx-cc compile --framework XLA penguin.py --internal-tensorizer-opt-level=nki --pipeline compile SaveTemps --target <arch>` |
| **Out of wheel** | `jax` / `jaxlib` / `torch` / `torch_xla` / `torch_neuronx` packages and the **PjRt Neuron plugin** |

> **NOTE —** the binding layer is split across two binaries by historical accident, and the names do not match the modules. `nki/compile.so` (module `neuronxcc.nki.compile`) defines `jit`, `compute_mode`, `GenericKernel`, and `simulate_kernel`; `nki/__init__.so` defines `baremetal`, `benchmark`, `profile`, and the internal `_jax_jit` / `_torchxla_jit` helpers, and re-exports the `compile.so` symbols as `nki.jit` / `nki.simulate_kernel`. When this page says "in `compile.so`" it means the module, not the file you `import`.

---

## The Boundary: What Is and Is Not in This Wheel

Stating the boundary first prevents the most common reimplementation error.

```text
  IN THIS WHEEL (the NKI side — documented here)
  ─────────────────────────────────────────────
  neuronxcc/nki/_jax.so          JAXKernel(FrameworkKernel)        → emits stablehlo custom_call
  neuronxcc/nki/_torch_xla.so    PyTorchXLAKernel(FrameworkKernel) → emits scribe CustomCall
  neuronxcc/nki/compile.so       compute_mode / jit / GenericKernel
  …/neuron/FrameworkKernel.so    dump_config / encode_backend_config (the backend_config producer)
  …/neuron/TraceKernel.so        trace / specialize_and_call (the trace engine)
  …/neuron/NumpyKernel.so        baremetal/benchmark/profile/simulate local exec
  neuronxcc/starfish/bin/…       the native compiler backend that RE-READS the backend_config

  OUT OF WHEEL (third-party — imported lazily at call time, never claimed here)
  ─────────────────────────────────────────────────────────────────────────────
  jax / jaxlib                   the JAX runtime + stablehlo MLIR dialect
  torch / torch_xla              the PyTorch + XLA bridge + `scribe` HLO builder
  torch_neuronx                  resolves platform_target; provides the deprecated `nki_jit` shim
  the PjRt Neuron plugin         what actually hands the compiled HLO module to the Neuron runtime
```

The `_jax.so` and `_torch_xla.so` modules **import jax/torch lazily, at use** — the `__pyx_k_jaxlib_mlir_dialects_stablehlo` and `__pyx_k_torch_neuronx_pyhlo` string constants are the import targets, resolved only when a JAX/torch kernel is actually lowered. Nothing about jax or torch internals is in this wheel or claimed here.

> **GOTCHA —** the **PjRt plugin is the missing link and it is not in this wheel.** When XLA finishes graph compilation it must hand the HLO module (with its `AwsNeuron*` custom-calls) to *something* that drives the Neuron runtime. That something is the PjRt Neuron plugin, a separate artifact. This wheel produces the custom-call and re-reads its `backend_config` inside the compiler backend, but the end-to-end "JAX program → device" path also requires the out-of-wheel plugin. A reimplementer wiring NKI into a framework must register the plugin themselves; NKI only provides the HLO-emission side.

> **NOTE —** the call-target string is not write-only on the framework side: `"AwsNeuronCustomNativeKernel"` appears in the native string table of the `neuronxcc/starfish/bin/…/xla_infergoldens` binary (3 occurrences). The compiler backend that ingests XLA HLO recognizes the JAX call target and knows to crack open its `backend_config`. This is the round-trip that makes "ship the BIR inside the HLO" work.

---

## Dispatch — `nki.jit` and `compute_mode`

### Purpose

`@nki.jit` is the single framework-agnostic front door. Its job is to defer the choice of execution backend until it can see the *actual argument types* of a call, then construct (and cache) the concrete kernel object that knows how to handle that framework.

### Entry Point

```text
nki.jit(func=None, mode="auto", **kwargs)            ── compile.so, jit @ 0xdde0
  └─ returns GenericKernel  (subscriptable: nki.jit[...] via __class_getitem__)
       └─ GenericKernel.__call__(*args, **kwargs)
            ├─ compute_mode(args, kwargs, mode)       ── picks the kernel class
            ├─ build_cache_kernel(...)                ── instantiate + memoize
            └─ cached_kernel(*args, **kwargs)         ── dispatch the call
```

### Algorithm

```c
// compute_mode  (neuronxcc.nki.compile.compute_mode)
// Refs CONFIRMED: jax, get_backend, platform, neuron, Array, ArrayImpl, Tracer,
//                 JAXKernel, torch, PyTorchXLAKernel, BaremetalKernel, _native_mode_map
function compute_mode(args, kwargs, mode):
    if mode != "auto":
        // (a) explicit mode → table lookup
        kls = _native_mode_map[mode]          // "jax"→JAXKernel, "torchxla"→PyTorchXLAKernel,
                                              // "baremetal"→BaremetalKernel, "benchmark"→BenchmarkKernel,
                                              // "simulation"→SimulateKernel
        if kls is None:
            raise Error("'" + mode + "' passed to nki.jit")   // unknown mode string
        return kls

    // (b) mode == "auto" → inspect RUNTIME ARGUMENT TYPES (not just which imports exist)
    for a in flatten(args, kwargs):
        if isinstance(a, (jax.Array, jaxlib.ArrayImpl, jax.core.Tracer)):
            if jax.get_backend().platform == "neuron":   // only on the neuron PjRt backend
                return JAXKernel
        if torch.is_tensor(a):
            return PyTorchXLAKernel
    return BaremetalKernel                                 // numpy / everything else
```

The dispatch is **argument-type based, not import-based.** Having `jax` installed does not force the JAX path; a numpy-only call still resolves to `BaremetalKernel`. The JAX path additionally gates on `jax.get_backend().platform == "neuron"` — a JAX kernel only lowers to a Neuron custom-call when JAX is actually running on the Neuron PjRt backend.

`GenericKernel.__call__` memoizes the chosen concrete kernel (`build_cache_kernel` → `cached_kernel`), so the type-inspection cost is paid once per process for a given dispatch.

### Function Map

| Symbol | Module | Role | Confidence |
|---|---|---|---|
| `jit` | `compile.so` | front-door decorator; bare & parametrized forms; `__class_getitem__` makes `nki.jit[...]` subscriptable | CERTAIN |
| `GenericKernel` | `compile.so` | per-call dispatcher; `__call__` / `build_cache_kernel` | CERTAIN |
| `compute_mode` | `compile.so` | mode resolution (explicit table or arg-type auto) | CERTAIN |
| `_native_mode_map` | `compile.so` | `{mode-string → kernel-class}` table | CERTAIN (symbol) |
| `_jax_jit(func, grid=…)` | `__init__.so` | internal `func,grid → JAXKernel(...).trace` helper | CERTAIN |
| `_torchxla_jit(func, grid=…)` | `__init__.so` | internal helper for the torch path | CERTAIN |

### Considerations

The mode strings are exactly `{"jax","torchxla","baremetal","benchmark","simulation","auto"}` (per the `jit` docstring and the `__pyx_k_simulation` / `__pyx_k_torchxla` string constants). Note `"torchxla"` and `"simulation"` are the mode spellings; the classes are `PyTorchXLAKernel` and `SimulateKernel`. An unknown mode raises with the fragment `"' passed to nki.jit"` — useful as a reimplementation oracle for the exact error wording.

---

## `FrameworkKernel` — The Custom-Call Emitter

### Purpose

`FrameworkKernel` is the shared base of `JAXKernel` and `PyTorchXLAKernel`. Its single responsibility, per its own docstring, is HLO generation: *"NKI kernels are represented as XLA CustomCall instructions in HLO. This class facilitates the HLO generation for NKI kernels."* It owns `dump_config`, which traces the kernel and produces the `backend_config`; the two subclasses contribute only the framework-specific tensor probing and the actual HLO-builder call.

### The Three Abstract Hooks

A framework integrator subclasses `FrameworkKernel` and implements exactly three methods (the docstring enumerates them; the `.pyi` gives signatures):

| Hook | Contract | JAX impl | torch impl |
|---|---|---|---|
| `is_framework_tensor(t) -> bool` | `True` ⇒ treat as a runtime tensor; `False` ⇒ it is a compile-time constant that *specializes* the kernel | jax/jnp tensor test | `torch.is_tensor(t)` |
| `map_framework_tensor(t) -> (shape, dtype)` | reduce a framework tensor to `(shape, dtype)` | `(t.shape, t.dtype)` | `(t.shape, t.dtype)` |
| `translate_to_neuron_dtype(dt)` | framework dtype → neuron/numpy dtype | jax dtype map | full torch dtype map (see §torch) |

These three hooks are the **only** framework-specific surface in the entire trace→config pipeline. Everything else — tracing, IR serialization, aliasing, caching — is framework-agnostic and lives in `FrameworkKernel` / `TraceKernel`.

> **QUIRK —** non-tensor arguments are not ignored; they are *specialization keys*. A Python `int`, a tuple, a string passed as a kernel arg returns `False` from `is_framework_tensor` and is folded into the cache key as a compile-time constant. This is why the spec cache is keyed on `(shapes, dtypes, grid, opts, const-args)` and why non-tensor args must be `Hashable` — passing a different constant traces and compiles a *new* kernel. An unhashable non-tensor arg raises `err_nki_param_not_hashable`.

### `__init__` and the spec cache

`FrameworkKernel.__init__` takes `func`, `func_name` (defaults to `func.__name__`), an optional SPMD `grid`, and `enable_cache=True`. It allocates a per-instance `OrderedDict` specialization cache on the attribute literally named `__neuron_kernel_interface_kernel_cache__`. One traced+encoded specialization is stored per distinct `(shape/dtype-or-hashable, grid, opts, platform_target_is_fallback)` key.

Two concrete subclasses live in `FrameworkKernel.so` alongside the bridge classes: `UnifiedKernel` (the newer unified path) and `LegacyFrameworkKernel` (the legacy `dump_config`-compatible path). The `dump_config` docstring carries the note *"This is still used by legacy framework code, dont change the signature"* — the public `dump_config(*args, **kwargs)` signature is frozen for backward compatibility.

---

## `dump_config` — Trace → backend_config Pipeline

### Purpose

`dump_config(*args, **kwargs)` is the public config generator: *"Returns the `backend_config`, the list of input names and the list of the output name, based on given arguments."* It is the load it the JAX/torch lowering rules call to produce the custom-call payload.

### Algorithm

```c
// FrameworkKernel.dump_config(*args, **kwargs)  @ 0x18860
function dump_config(args, kwargs):
    boundargs = bind_arguments(self.func, args, kwargs)     // bind to signature
    return dump_config_with_boundargs(boundargs)            // delegate

// FrameworkKernel.dump_config_with_boundargs(boundargs)
function dump_config_with_boundargs(boundargs):
    inputs = _map_args(boundargs)                           // (1) framework tensors → DeclTensors
    if self.enable_cache:                                   // (5) cache probe FIRST
        key = _generate_hash_key(boundargs)
        if key in __neuron_kernel_interface_kernel_cache__:
            return cached_result(key)                       // reuse encoded backend_config

    (kernel_return, has_collectives) =                      // (2) trace the body
        specialize_and_call(boundargs)                      //     (TraceKernel; see §Trace)
    outputs = translate_return_types(kernel_return)         // (3) per-output (shape, dtype)
    aliases = generate_operand_output_aliases(inputs, outputs)  // (4) in-place donation

    backend_config = encode_backend_config(                 // (6) base64(JSON), §encode
        func_name, grid, has_collectives, matmul_mac_count,
        platform_target, platform_target_is_fallback,
        opts, serialize_dims(...), serialize_ir_string(...))

    result = assemble_result(backend_config, input_names,   // (7) package + cache-store
        output_names, aliases, constant_values, return_types,
        has_collectives, kernel_return, result_is_sequence)
    __neuron_kernel_interface_kernel_cache__[key] = result
    return result
```

### `_map_args` — the framework-tensor reduction

```c
// _map_to_decltensor_or_passthrough(t)
function _map_to_decltensor_or_passthrough(t):
    if is_framework_tensor(t):                       // subclass hook
        (shape, dt) = map_framework_tensor(t)        // subclass hook
        ndt = translate_to_neuron_dtype(dt)          // subclass hook
        return DeclTensor(shape, ndt)                // a tracing placeholder (nki_tensor)
    else:
        return t                                     // pass through as compile-time constant
```

This is the *single point* where the framework escapes into the framework-agnostic core: a torch/jax/numpy tensor is collapsed to `(shape, neuron-dtype)` and replaced by a `DeclTensor`; everything downstream operates on Neuron IR, not framework objects.

### `generate_operand_output_aliases` — buffer donation

```c
// generate_operand_output_aliases(inputs, outputs)
// Refs CONFIRMED: findAliasTensor, hasAliasedTensor, get_alias_output_index
function generate_operand_output_aliases(inputs, outputs):
    aliases = []
    for out in outputs:
        if hasAliasedTensor(out):                    // python "write into the last arg"
            op_idx  = findAliasTensor(out, inputs)
            out_idx = get_alias_output_index(out)
            aliases.append((op_idx, out_idx))        // XLA donates input buffer to output
    return aliases
```

Python NKI uses pass-by-reference output semantics (you write into the buffer arg), but the HLO custom-call *returns* its outputs. Aliasing is how the two are reconciled: each output that is actually an input buffer becomes an `(operand_index → output_index)` alias pair, so XLA donates the input buffer instead of copying. The `AliasType` / `must_alias` / `aliased_tensor` strings are present in the binary.

### The specialization cache key

`_generate_hash_key` builds the key from `(per-arg shape/dtype-or-hashable, grid, opts, platform_target_is_fallback)` via `_return_shape_dtype_or_hashable`: framework tensors contribute `(shape, dtype)`; other args must be `Hashable` (else `err_nki_param_not_hashable`). The cache is the per-instance `OrderedDict`; a hit returns the already-encoded `backend_config` without re-tracing.

> **GOTCHA —** the cache is probed *before* tracing but the key is computed from bound args, and `platform_target_is_fallback` is part of the key. A kernel traced on a machine where the platform target fell back to `trn1` (see §Trace) caches under a *different* key than the same kernel on a correctly-detected target. Do not assume the cache is keyed on shapes/dtypes alone.

---

## `encode_backend_config` — The Base64-BIR Payload

### Purpose

This is the centerpiece of the whole binding. `encode_backend_config` builds a Python dict describing the kernel — including **the serialized Neuron IR program itself** — and encodes it as a base64 string suitable for the `backend_config` field of an XLA `custom-call`.

### Algorithm

```c
// FrameworkKernel.encode_backend_config(...)  @ 0x296d0
// Refs CONFIRMED: json, dumps, base64, b64encode, encode, decode,
//                 serialize_dims, serialize_ir_string, KERNEL_VERSION
function encode_backend_config(...):
    cfg = {
        "kernel_version":               KERNEL_VERSION,        // module const
        "func_name":                    func_name,
        "grid":                         serialize(grid),       // SPMD launch grid
        "has_collectives":              has_collectives,       // bool, from trace (§collectives)
        "matmul_mac_count":             matmul_mac_count,      // perf/cost annotation (§Trace)
        "platform_target":              platform_target,       // trn1/trn2/.../inf2
        "platform_target_is_fallback":  platform_target_is_fallback,
        "opts":                         serialize(opts),       // CompileOpts
        "serialize_dims":               serialize_dims(...),   // tensor dim metadata
        "serialize_ir_string":          serialize_ir_string(...),  // ← THE BIR/penguin IR program
        "tiled":                        tiled,                 // tiling marker
        "constant_values":              constant_values,       // non-tensor const args
        "kernel_return":                kernel_return,
        "result_is_sequence":           result_is_sequence,
    }
    return base64.b64encode(json.dumps(cfg).encode()).decode()  // CONFIRMED b64encode + json.dumps
```

### The config-key set

These dict keys are all verbatim string constants in `FrameworkKernel.so`: `kernel_version`, `func_name`, `grid`, `has_collectives`, `matmul_mac_count`, `platform_target`, `platform_target_is_fallback`, `opts`, `serialize_dims`, `serialize_ir_string`, `tiled`, `constant_values`, `kernel_return`, `result_is_sequence`.

> **QUIRK —** `serialize_ir_string` is the entire traced kernel program — the BIR/penguin IR text — riding inside the HLO `backend_config`. The host framework (JAX, torch-xla) never compiles, inspects, or understands the kernel body; it transports an opaque base64 blob to the Neuron compiler backend, which base64-decodes it, parses the JSON, and reads `serialize_ir_string` to rebuild the kernel. The custom-call is, in effect, a self-describing parcel: target name says "this is an NKI kernel," and the config *is* the kernel. This is why the same `backend_config` works for both the JAX and torch bridges unchanged — it is framework-independent IR, not framework attributes.

> **NOTE —** the encoding is plain `base64(json.dumps(...))` — not protobuf, not a binary struct. A reimplementer can decode any NKI custom-call's `backend_config` with `json.loads(base64.b64decode(s))` and read the kernel metadata (and the serialized IR) directly. There is no encryption or compression at this layer.

---

## The Two Bridges

The two bridges are nearly identical: each implements the three `FrameworkKernel` hooks, then emits one HLO custom-call carrying the `dump_config` payload. The deltas are the HLO builder and the call-target string.

### JAX bridge — `JAXKernel` (`_jax.so`)

`JAXKernel(FrameworkKernel)` registers NKI as a **JAX primitive** with an abstract-eval rule and an MLIR lowering rule. Imports (lazy): `jax.core`, `jax.extend.core`, `jax.interpreters.mlir`, `jax.numpy`, `jaxlib.mlir`, `jaxlib.mlir.dialects.stablehlo`, `jaxlib.hlo_helpers`.

```c
// nki_call_eval(...)  — the ABSTRACT-EVAL rule (def_abstract_eval)
// Refs CONFIRMED: dump_config, trace, return_types, ShapedArray, jnp
function nki_call_eval(*avals, kernel):
    cfg = kernel.dump_config(...)              // trace to learn output return_types
    return [ShapedArray(rt.shape, rt.dtype)    // jax avals; NO real compute
            for rt in cfg.return_types]
    // wrapped in JaxTraceResult (hashable: __hash__) so JAX caches the lowering

// nki_call_lowering_rule(ctx, *args)  — the MLIR LOWERING rule
// Refs CONFIRMED: avals_in, avals_out, aval_to_ir_type, custom_call,
//                 dump_config, encode, has_collectives, operand_output_aliases
function nki_call_lowering_rule(ctx, *args):
    cfg = kernel.dump_config(...)              // (cached via JaxTraceResult)
    return stablehlo.custom_call(
        call_target_name = "AwsNeuronCustomNativeKernel",   // CONFIRMED literal
        operands         = args,
        result_types     = [aval_to_ir_type(a) for a in ctx.avals_out],
        backend_config   = cfg.dumped_config,               // base64 JSON, §encode
        operand_output_aliases = cfg.operand_output_aliases,
        // mhlo.frontend_attributes carried as DictAttr/StringAttr
        frontend_attributes = DictAttr({...}),
    )
```

The abstract-eval rule traces once to learn the output `ShapedArray` avals (no compute); the lowering rule emits the `stablehlo.custom_call`. The trace result is wrapped in `JaxTraceResult` (with `__hash__`) so JAX's lowering cache works.

> **GOTCHA — a known trace-caching bug, guarded by an env var.** The verbatim message is: *"NKI trace is not properly cached, use `export NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING=TRUE` to workaround the issue."* Setting `NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING=TRUE` forces a re-trace at lowering time rather than reusing the cached abstract-eval trace. A reimplementer mirroring the JAX cache must replicate this escape hatch.

The verbatim HLO shape this produces (from the `FrameworkKernel` docstring) is:

```text
%custom-call.2 = f32[16,8,128,512]{3,2,1,0} custom-call(
    f32[16,8,128,512]{3,2,1,0} %p2.2, f32[16,8,128,512]{3,2,1,0} %p1.2),
    custom_call_target="AwsNeuronCustomNativeKernel",
    api_version=API_VERSION_UNSPECIFIED,
    metadata={op_type="xla___op_NkiKernelCallImpl" op_name="xla___op_NkiKernelCallImpl"},
    backend_config= # ...omitted (base64 JSON)
```

`api_version` and `metadata` are optional (`metadata`'s `op_type`/`op_name` surface in `neuron-profiler`); the call target *should always be* `"AwsNeuronCustomNativeKernel"` (docstring, emphasis original).

### PyTorch-XLA bridge — `PyTorchXLAKernel` (`_torch_xla.so`)

`PyTorchXLAKernel(FrameworkKernel)` builds the same payload but emits the custom-call through the torch-xla `scribe` HLO builder rather than stablehlo. Imports (lazy): `torch`, `numpy`, the torch-xla `scribe` builder, `torch_neuronx.pyhlo.xla_data_pb2`, and `torch_neuronx` for the platform target. It exposes a module-level `nki_jit`.

```c
// PyTorchXLAKernel.__call__ → call_impl
// Refs CONFIRMED: AwsNeuronNkiKernel, CustomCall, build_xla_type, scribe,
//                 dumped_config, output_operand_aliasing, OutputOperandAliasing,
//                 excluded_output_tensors, xla_data_pb2
function call_impl(boundargs):
    cfg = dump_config_with_boundargs(boundargs)        // same base64 payload, §encode
    operands = [load_tensor_from_arguments(a) for a in inputs]
    result_xla_types = [build_xla_type(o) for o in outputs]   // neuron dtype+shape → XLA Shape

    return scribe.CustomCall(
        custom_call_target = "AwsNeuronNkiKernel",     // CONFIRMED literal (≠ JAX's target!)
        operands           = operands,
        shape              = result_xla_types,
        opaque             = cfg.dumped_config,         // base64 JSON backend_config
        // mhlo frontend_attributes
        output_operand_aliasing = [
            OutputOperandAliasing(output_shape_index, operand_index, operand_shape_index)
            for alias in cfg.operand_output_aliases
        ],
        // excluded_output_tensors: outputs not materialized
    )
```

```c
// translate_to_neuron_dtype(dt) — the torch dtype map (build_xla_type uses nki_dtype_to_xla_type_map)
// covers: float64/32/16, bfloat16, float8_e4m3 / float8_e5m2,
//         int/uint 8/16/32/64, bool, complex64/128.  Honors an env override table.
```

`__init__` resolves the platform target from `torch_neuronx` via `_use_existing_target_or_retrieve_from_torch_neuronx` and folds it into `opts` (using `dataclasses.replace`).

> **QUIRK —** the two bridges use *different* call-target strings: JAX emits `"AwsNeuronCustomNativeKernel"`, PyTorch-XLA emits `"AwsNeuronNkiKernel"`. The `backend_config` payload is byte-for-byte the same `dump_config` output. A reimplementer must register **both** target names with the Neuron backend (or whichever the framework in use emits) — they are not interchangeable, and only `AwsNeuronCustomNativeKernel` is confirmed in the native `xla_infergoldens` string table.

> **NOTE —** `torch_neuronx.nki_jit` is deprecated — the `_torch_xla.so` binary carries a `DeprecationWarning` and the string `"torch_neuronx.nki_jit is deprecated…"`. The supported entry is `@nki.jit` with auto/`"torchxla"` mode; the standalone `nki_jit` shim remains for compatibility.

### Bridge Function Map

| Symbol | Module | Role | Confidence |
|---|---|---|---|
| `JAXKernel` | `_jax.so` | JAX `FrameworkKernel` subclass | CERTAIN |
| `nki_call_eval` | `_jax.so` | abstract-eval rule (output avals) | CERTAIN |
| `nki_call_lowering_rule` | `_jax.so` | stablehlo `custom_call` emitter, target `AwsNeuronCustomNativeKernel` | CERTAIN |
| `JaxTraceResult.__hash__` | `_jax.so` | hashable trace wrapper for JAX lowering cache | CERTAIN |
| `PyTorchXLAKernel` | `_torch_xla.so` | torch `FrameworkKernel` subclass | CERTAIN |
| `PyTorchXLAKernel.build_xla_type` | `_torch_xla.so` | neuron dtype+shape → XLA Shape (`nki_dtype_to_xla_type_map`) | CERTAIN |
| `nki_jit` | `_torch_xla.so` | deprecated module-level torch entry | CERTAIN |

---

## The Numpy Execution Family

The non-framework modes (`baremetal`, `benchmark`, `profile`, `simulate_kernel`) do not emit HLO — they trace, then either compile a NEFF and run it locally or run a CPU simulator. They are documented here for the dispatch's sake; the runtime/profiler internals belong to other strands.

### Class stack

```text
TraceKernel (TraceKernel.so)            ── trace / specialize_and_call engine
 ├─ FrameworkKernel (FrameworkKernel.so) ── HLO/backend_config emitter
 │   ├─ UnifiedKernel / LegacyFrameworkKernel
 │   ├─ JAXKernel        (_jax.so)        ── emit XLA custom_call, never runs locally
 │   └─ PyTorchXLAKernel (_torch_xla.so)  ── emit XLA CustomCall, never runs locally
 └─ NumpyKernel (NumpyKernel.so)          ── numpy-tensor local exec base
     ├─ BaremetalKernel                   ── compile NEFF + run on device
     ├─ BenchmarkKernel (←Baremetal)      ── neuron-bench latency percentiles
     ├─ ProfileKernel   (←Baremetal)      ── kra Profiler → .ntff + json
     └─ SimulateKernel  (←Numpy)          ── CPU IRSimulator, no NEFF
```

Both branches inherit `TraceKernel.trace` / `specialize_and_call` — the single tracing entry. The split is: framework modes serialize the trace into a custom-call; numpy modes write the trace to `penguin.py` and shell out to the compiler.

### The local compile shell-out

`BaremetalKernel._compile` shells out to the Neuron compiler with this verbatim command template, a single string constant in `NumpyKernel.so`:

```bash
neuronx-cc compile --framework XLA penguin.py \
    --internal-tensorizer-opt-level=nki \
    --pipeline compile SaveTemps --target <arch>
```

`<arch>` is the resolved platform target; `additional_compile_opt` is appended; stdout goes to `DEVNULL`; failure raises `CalledProcessError`. The traced IR is first serialized to `penguin.py` via `write_tensorizer_ir` (an `IRWriter`), which is the file the `penguin.py` argument names. `execute_neff` then uses `NrtClient` (from `neuronxcc.kra.kralib`): `try_lock_device` → `modelLoad(neff)` → `modelExecute(io_maps)` → `update_outputs` (`np.frombuffer` → `reshape` → `np.copyto` into the caller's numpy arrays — realizing the pass-by-reference output semantics).

### Mode summary

| Entry | Class | Mechanism | Notes |
|---|---|---|---|
| `nki.baremetal(kernel=None, **kw)` | `BaremetalKernel` | compile NEFF, run once, copy outputs back | `save_neff_name`/`save_trace_name`/`additional_compile_opt`/`artifacts_dir`; returns `None` (in-place) |
| `nki.benchmark(kernel=None, **kw)` | `BenchmarkKernel` | `neuron-bench` (`NeuronBench` from `kra.profile_lib`), `warmup`/`iters` | `.benchmark_result.nc_latency.get_latency_percentile(p)`, `p∈[0,1,10,25,50,90,99,100]` µs; single-core only; does **not** use real input values |
| `nki.profile(func=None, **kw)` | `ProfileKernel` | `kra` `Profiler` → `.ntff` + json into `working_directory` | `working_directory` REQUIRED absolute; `save_neff_name=file.neff`, `save_trace_name=profile.ntff`, `overwrite`, `profile_nth` — full detail elsewhere |
| `nki.simulate_kernel(kernel, *a, **kw)` | `SimulateKernel` | CPU `IRSimulator` (`starfish.penguin.simulation`); no NEFF | makes `nki.language.device_print` values inspectable; not a decorator — a direct call |

> **NOTE —** `benchmark` and `profile` run the NEFF on *shapes only* — they do not feed the real input values, so their outputs are numerically meaningless (the docstrings state this explicitly). Only `baremetal` and `simulate_kernel` produce usable output values.

---

## Alloc Decorators — `nki.compiler` psum/sbuf

The `nki.compiler` namespace controls where SBUF/PSUM tensors land and which compiler transforms run. It is the surface a framework-bound kernel uses to pin allocations; it is part of the binding because these decorators wrap the same `Kernel` object the dispatch constructs.

### The two allocation classes

`allocator.so` defines two parallel classes, `PSUMAllocation` and `SBUFAllocation`, each exposing `.alloc(func)`, `.mod_alloc(...)`, `.auto_alloc()`. These *are* the `nki.compiler.psum` / `nki.compiler.sbuf` namespaces (`ncc.psum` / `ncc.sbuf` in docs). They are assigned to a tensor's `buffer=` field:

```c
// custom allocation: func(idx, pdim_size, fdim_size) returns physical location
nki_tensor = nl.ndarray((2,4,par_dim(128),512), dtype=float32,
                        buffer=ncc.psum.alloc(my_alloc_fn))
//   psum: my_alloc_fn → (bank_id, start_partition, byte_addr)
//   sbuf: my_alloc_fn → (start_partition, byte_addr)
```

The ISA constraints (verbatim from the `.pyi`): PSUM `fdim_size` ≤ 2 KiB (one bank/partition); `start_partition`/`byte_addr` must be 0 in this release. SBUF `start_partition` follows the partition-quantization rule (`64<pdim≤128`⇒0; `32<pdim≤64`⇒{0,64}; `0<pdim≤32`⇒{0,32,64,96}); `byte_addr ∈ [0, 192KiB−16KiB)` on NeuronCore-v2 (16 KiB reserved for the compiler), aligned to `sbuf_min_align`. `mod_alloc` is `alloc` with a generated modulo function for double-buffering.

> **GOTCHA —** within one kernel, *all* PSUM tensors (and *all* SBUF tensors) must be uniformly auto-allocated **or** uniformly direct-allocated — mixing `auto_alloc()` with `alloc()`/`mod_alloc()` in the same kernel is rejected. The `auto_alloc()` marker is the default (`nl.psum`/`nl.sbuf` == `ncc.psum/sbuf.auto_alloc()`), consumed by the `Allocator` class in `allocator.so` (the auto path, driven by `TraceKernel.allocate_pending_tensors`).

### Transform decorators (`decorators.so`)

| Decorator | Effect | Confidence |
|---|---|---|
| `force_auto_alloc(func=None)` | switch to auto `Allocator`; ignore any direct alloc in the kernel | CERTAIN |
| `enable_stack_allocator(func=None, log_level=50)` | `AllocatorType.stack`; **must** combine with `skip_middle_end_transformations` (stub note) | CERTAIN |
| `skip_middle_end_transformations(func=None)` | `OptLevel` skips all middle-end transforms | CERTAIN |
| `multi_buffer(factor=2)` | `MultiBufferDirective` lexical scope (`with nki.compiler.multi_buffer(2): …`) | CERTAIN |
| `no_reorder()` | `OperationOrderGuard` lexical scope — prevents op reordering | CERTAIN |
| `allocation_scope()` | `AllocationScope` context manager | CERTAIN |

All decorators re-wrap the `Kernel` via `copy_kernel` + `replace_fields` (immutable-style override): `update_compile_opts` mutates `CompileOpts`; `update_allocator` mutates the allocator type / `OptLevel`. The lexical-scope directives (`multi_buffer`, `no_reorder`, `allocation_scope`) live in `LexicalScopeDirective.so` and wrap an `nki_ctx` context manager.

---

## Collectives — `nki.distributed` and `nki.nccl`

Collective ops are the last piece of the binding surface: when a kernel issues a collective, `TraceKernel` sets `has_collectives=True`, which flows into the `backend_config` (§encode) and tells the SPMD/collective backend the kernel participates in cross-core communication.

### Two layers

| Layer | Module | Ops | Lowers via |
|---|---|---|---|
| `nki.distributed` | `distributed/collectives.so` | `all_reduce`, `all_gather`, `reduce_scatter`, `all_to_all`, `broadcast`, `collective_permute` | `nki_ctx.*_cc` builders |
| `nki.nccl` | `nccl/collectives.so` | the six above **plus** `send`, `recv`, `collective_permute_implicit`, `collective_permute_reduce_implicit` | `nki_ctx.*_cc_raw` builders |

`nki.distributed` is the group-aware convenience layer (operates across the "tensor parallel group"); `nki.nccl` is the thin primitive layer exposing raw `replica_groups`, point-to-point `send`/`recv` with `peer_id`, and NCCL `channel_id`/`num_channels`. Both emit collective instructions into the kernel's BIR and both flip `has_collectives`.

```c
// nki.distributed (group-aware)        → nki_ctx.*_cc
all_reduce(srcs, dsts, op, ...)              → nki_ctx.all_reduce_cc
all_gather(srcs, dsts, op, all_gather_dim)   → nki_ctx.all_gather_cc
reduce_scatter(srcs, dsts, op, dim)          → nki_ctx.reduce_scatter_cc
all_to_all(srcs, dsts, op, split_dim, concat_dim) → nki_ctx.all_to_all_cc
broadcast(srcs, dsts, op, broadcast_sizes)   → nki_ctx.broadcast_cc
collective_permute(srcs, dsts, ...)          → nki_ctx.collective_permute_cc

// nki.nccl (primitive)                  → nki_ctx.*_cc_raw + tiled implicit variants
all_reduce(srcs, dsts, op, replica_groups)   → nki_ctx.all_reduce_cc_raw
send(srcs, op, peer_id)                       → nki_ctx.send_cc_raw
recv(dsts, op, peer_id, replica_groups)       → nki_ctx.recv_cc_raw
collective_permute_implicit(src, dst, replica_groups, channel_id, num_channels, mask)
                                              → nki_ctx.tiled_collective_permute_implicit
collective_permute_reduce_implicit(...)       → nki_ctx.tiled_collective_permute_reduce_implicit
```

`srcs`/`dsts` are *lists* of HBM (or SB) tensors, per their docstrings. The actual collective-compute realization (the hardware backend the `*_cc` / `*_cc_raw` builders feed) is owned by the collectives strand — see Part 13.

> **NOTE —** `nki.benchmark` cannot run a kernel with collectives — *"NKI not yet supports collective compute"* for the benchmark path (single-NeuronCore only). Collective kernels must reach the device through the framework (custom-call) path or `baremetal`.

---

## Trace Engine — `TraceKernel` (surface only)

The trace engine underneath both branches is `TraceKernel` (`TraceKernel.so`); its SPMD/codegen internals are owned by other strands, but two seams matter to the binding.

**Platform-target resolution.** `_get_platform_target` reads the target from the environment (`NEURON_PLATFORM_TARGET_OVERRIDE` and instance detection). On a miss it warns *"Unable to read MLA target. Assuming cross compile to trn1."* (verbatim) and sets `platform_target_is_fallback=True`, which is folded into both the cache key and the `backend_config`. A grid spanning multiple physical cores is only legal on `trn2`/`trn2n`/`trn3`/`core_v4`/`gen3` (else `err_multi_cores_spmd_not_supported`).

**`matmul_mac_count`.** After tracing, `matmul_mac_count` walks the IR instructions and sums matmul MAC counts; this scalar is surfaced into `backend_config` as a perf/cost annotation for the runtime/profiler.

`specialize_and_call` is the single tracing entry both branches call: it builds a penguin `Function`, seeds a `GeneratedNeuronCodegen` context, runs the user kernel body under the SPMD trace scope (`expand_kernel_with_ctx`), allocates pending SBUF/PSUM tensors, and returns `(kernel_return, has_collectives)`. The forward NKI→BIR codegen detail is Part 6.

---

## Evidence summary

Each of this page's central claims rests on verbatim strings.

- **The JAX target `"AwsNeuronCustomNativeKernel"`** is triple-anchored: in `_jax.so` as `__pyx_n_u_AwsNeuronCustomNativeKernel`, in the `FrameworkKernel` docstring as `custom_call_target="AwsNeuronCustomNativeKernel"`, and in the native `xla_infergoldens` string table (3 hits).
- **The PyTorch-XLA target `"AwsNeuronNkiKernel"`** appears in `_torch_xla.so` as `__pyx_n_s_AwsNeuronNkiKernel`, adjacent to `CustomCall`, `scribe`, and `OutputOperandAliasing`. The two targets are genuinely distinct strings, not variants of one.
- **The base64-JSON `backend_config` mechanism** is pinned by `b64encode`, `encode_backend_config`, `json`/`dumps`, and `serialize_ir_string`, all in `FrameworkKernel.so`, with the full config-key pool string-verified.
- **Argument-type compute-mode dispatch** rests on `compute_mode`, `_native_mode_map`, `JAXKernel` / `PyTorchXLAKernel` / `BaremetalKernel`, and `get_backend` / `ArrayImpl` / `Tracer` / `is_tensor`, all in `compile.so`.
- **The local compile command** `neuronx-cc compile --framework XLA penguin.py …` is one verbatim string in `NumpyKernel.so`.

### Limits of this reading

Two claims reach past what the framework-side binaries show. That the Neuron backend *parses* `serialize_ir_string` to rebuild the kernel — the "BIR rides inside the config" semantics — is supported by the native target string and the round-trip framing, but the backend-side parser was not traced. And the `platform == "neuron"` gate is read from the `neuron` / `platform` / `get_backend` references rather than from a traced comparison.

---

## Related Components

| Name | Relationship |
|---|---|
| `TraceKernel` | trace/specialize engine both branches inherit; SPMD scope mechanics |
| `GeneratedNeuronCodegen` / `KernelBuilder` | the NKI→BIR forward codegen that `specialize_and_call` drives |
| `NumpyKernel` | local compile+exec family (`NrtClient` / `NeuronBench` / `IRSimulator`) |
| `nki_ctx` | the `*_cc` / `*_cc_raw` collective IR builders |
| PjRt Neuron plugin | OUT OF WHEEL — drives the compiled HLO module to the device |

## Cross-References

- [Command Dispatcher](command-dispatcher.md) — 3.1, the `neuronx-cc` CLI that `BaremetalKernel._compile` shells out to
- [HLO → Native / NKI Kernel Lowering](../hlo-opt/hlo-to-native-kernel-lowering.md) — how the compiler backend cracks the `AwsNeuronCustomNativeKernel` custom-call and re-reads the serialized IR
- [NKI Architecture Overview](../nki/architecture-overview.md) — Part 6, the NKI→BIR codegen behind `specialize_and_call`
- [NeuronCodegen Collective Forward Builders](../nki/neuroncodegen-collectives.md) — the SPMD/collective backend the `*_cc` builders feed (the standalone collectives/distribution Part 13 page is pending)
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the bank/partition layout the alloc decorators target
