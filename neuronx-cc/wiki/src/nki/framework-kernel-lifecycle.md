# 6.0.3 FrameworkKernel / TraceKernel Lifecycle & the `backend_config`

> **Scope.** This page follows one NKI kernel from `@nki.jit` through tracing,
> specialization, the per-instance specialization **cache**, and the assembly of the
> base64-JSON **`backend_config`** that ships a serialized BIR program *inside* the HLO
> custom-call. It owns the **trace → compile → cache lifecycle** and the
> **`backend_config` wire format**.
>
> The five entrypoints (`jit` / `baremetal` / `benchmark` / `profile` /
> `simulate_kernel`) and the argument-type dispatch are [6.0.2 NKI Entrypoints &
> Argument Dispatch](./entrypoints.md) — cross-referenced, not duplicated. The
> framework-side registration of the custom-call is [3.13 Framework
> Bindings](../frontend/framework-bindings.md); re-trace/cache reuse downstream is
> [6.6.3](../nki/retrace-cache.md); the BIR wire that `serialize_ir_string` carries is
> [Part 7 BIR](../bir/inst-hierarchy.md).

**Binaries.** `neuronxcc/nki/compiler/backends/neuron/FrameworkKernel.cpython-310-*.so`
(1,226,584 B, Cython, partly decompiled by IDA) and
`.../TraceKernel.cpython-310-*.so` (1,958,168 B, BuildID
`23b090983c13cbdd33920903008fd22b664b31eb`, **"with debug_info, NOT stripped"** —
full DWARF). cp311/cp312 copies are byte-equivalent in source; cp310 used throughout.

> **How the two binaries were read.** Every `backend_config` key, every pipeline method
> name, the cache-attribute name, the custom-call target, and the worked HLO example
> below come out of `FrameworkKernel.so`'s `.rodata` string pool. The `TraceKernel.so`
> method **bodies** — `specialize_and_call`, `expand_kernel_with_ctx`, `call_impl` — were
> **DWARF-reconstructed**: IDA's batch only processed the PLT/GOT thunk band
> `0x608e8–0x60e80`, so the real pyx bodies at `0x1c000–0x4e000` were disassembled
> directly and cross-keyed to the 33,623-row DWARF line table. Claims resting on that
> reconstruction carry a **[DWARF]** tag below.

---

## 1. The class stack and where the lifecycle lives

Both the framework path (JAX / torch-xla, emit HLO) and the local path (baremetal /
benchmark / profile / simulate, compile & run on numpy) inherit a single trace engine.
Bottom-up, from the symbol tables of both `.so` files:

```
Kernel                  (TraceKernel.so)  — arg-binding + grid base
 └ TraceKernel(Kernel)  (TraceKernel.so)  — trace/specialize engine  ───────┐ §2
    ├ FrameworkKernel   (FrameworkKernel.so) — HLO/backend_config emitter ──┘ §3+
    │   ├ UnifiedKernel            (newer unified path)
    │   ├ LegacyFrameworkKernel    (legacy dump_config-compat path)
    │   ├ JAXKernel                (_jax.so;       target "AwsNeuronCustomNativeKernel")
    │   └ PyTorchXLAKernel         (_torch_xla.so; target "AwsNeuronNkiKernel")
    └ NumpyKernel       (NumpyKernel.so)  — numpy-tensor exec base (→ 6.0.2)
        ├ BaremetalKernel ├ BenchmarkKernel ├ ProfileKernel └ SimulateKernel
```

The split is the whole point of the design: **`TraceKernel` re-executes the kernel
body to build the IR; `FrameworkKernel` turns that IR into a serialized
`backend_config` and never runs anything locally; `NumpyKernel` shells out to
`neuronx-cc` and runs a NEFF.** This page is the left spine —
`TraceKernel → FrameworkKernel`. The `NumpyKernel` exec path is 6.0.2.

> **GOTCHA — the trace is re-execution, not a stored AST.** `TraceKernel.call_impl`
> *runs the user Python function again* under a tracing context; the NKI intrinsics in
> the body emit `penguin.ir` ops into the live `GeneratedNeuronCodegen` builder. There
> is no persisted KLIR AST in this file. (The KLIR-AST / beta2 re-trace path lives
> downstream in `BirCodeGenLoop`, not here — see §2.4.) [DWARF; the base
> `Kernel.call_impl` at `0x30090` literally raises with
> `"call_impl not implemented for base kernel"`, and `TraceKernel.call_impl` at
> `0x398b0` is the real dispatcher.]

---

## 2. `@nki.jit` → trace → `specialize_and_call` → re-execute body

### 2.1 The decorator hands back a wrapper

`TraceKernel.trace` (`TraceKernel.py` L554, pyx-wrapper `@0x24300`) is a **decorator
factory**, not a tracer. Its body is a CyFunction builder: it parses the positional
args, loads `__pyx_mdef_…_TraceKernel_5trace_1decorating_function`, calls
`__Pyx_CyFunction_New` (`@0x24583`), and returns the closure
`decorating_function`. [DWARF; the `decorating_function` k-string lives at `.rodata
0x56070`, qualname literal `"…TraceKernel.TraceKernel.trace"` at `0x52c68`.]

`Kernel.__init__` (L87, `@0x45830`) captures the kernel object's fields: it runs
`inspect.signature(func)` (`inspect` k-string `0x56c20`, `signature` `0x56970`) and
stores `func`, `func_name`/`kernel_name` (defaulting to `func.__name__`, falling back to
`'unnamed'`), `_grid`/`grid`, the `inspect.Signature`, `opts` (`Optional[CompileOpts]`),
`debug_kernel`, and a `parent_ctx` (`TraceContext`). [DWARF — field vocabulary.]

```c
// trace(self, func) — the @nki.jit entry; a decorator factory, not a tracer.
PyObject* TraceKernel_trace(TraceKernel *self, PyObject *func) {
    // capture func + inspect.signature(func) into the Kernel object (Kernel.__init__)
    return CyFunction_New(decorating_function, /*closure=*/ self);   // 0x24583
}

// trace.<locals>.decorating_function(*args, **kwargs) — the per-CALL wrapper [DWARF]
PyObject* decorating_function(PyObject *args, PyObject *kwargs) {    // pf @0x1def0
    boundargs = translate_args_kwargs(self.signature, args, kwargs); // inlines Kernel.translate (0x1fb10)
    return self.specialize_and_call(boundargs);                      // dispatch into the trace
}
```

### 2.2 `specialize` / `specialize_and_call` — the cache-keyed trace

`specialize_and_call` (L386, `@0x406e0`) is the **largest** method in the `TraceKernel`
class (runs to `0x42c20`) and carries a generator
(`__pyx_gb_…specialize_and_call_2generator2 @0x1ea50`). It is the cache-keyed trace
entry: build (or reuse) a specialized `TraceKernel` for the concrete arg shapes/dtypes,
re-execute the body under the trace context, and return the traced result plus the
`has_collectives` flag. [DWARF; the method's k-string vocabulary in `.rodata`
includes `addBasicBlock`, `boundargs`, `expand_kernel_with_ctx`, `Function`,
`GeneratedNeuronCodegen`, `grid`, `grid_has_multi_physical_cores`, `has_collectives`,
`opt_level`, `opts`, `recompiled`, `new_ctx`.]

```c
// specialize_and_call(self, boundargs) -> (kernel_return, has_collectives, ...)   [DWARF]
result specialize_and_call(TraceKernel *self, BoundArguments boundargs) {
    fn  = Function_new(self.func_name);            // penguin.ir.Function — seed an IR function
    cg  = GeneratedNeuronCodegen(fn, self.opts);   // the forward builder (nl -> penguin.ir; P01)
    fn.addBasicBlock();                            // entry block
    new_ctx = self.enter_trace_context(cg);        // contextlib scope

    // ⭐ run the user kernel body under the trace context (the RE-EXECUTION)
    kernel_return, has_collectives =
        self.expand_kernel_with_ctx(new_ctx, boundargs);  // §2.3

    return (kernel_return, has_collectives, fn);
}
```

`specialize` (`@0x37690`) produces the per-shape specialized kernel via
`copy_kernel`/`replace`/`pop_compile_opts`, and the `recompiled` /
`previous_code` (L432, `@0x29540`) vocabulary is the **recompilation guard**: an
unchanged body is not re-traced. [DWARF.]

### 2.3 `expand_kernel_with_ctx` — SPMD scope + run the body

`expand_kernel_with_ctx` (L401, `@0x29ab0`, runs to `0x2d0f0`) is the SPMD /
grid-expansion entry. It destructures a `(grid, ctx)`-style tuple (the
`"too many values to unpack"` / `"need more than %zd value…to unpack"` guards at
`0x2c650` / `0x2bccf`), enters the `spmd_kernel_scope` contextmanager
(`GetModuleGlobalName @0x2c61f`, k-string `0x561b0`), maps args→IR parameters, runs the
body (`trace_kernel_impl`, k `0x56190`), flushes deferred SBUF/PSUM allocs
(`allocate_pending_tensors`, k `0x55d30`), validates the return, and finalizes.
`NKIFinalizeException` is the recompile/finalize control signal. [DWARF]

```c
// expand_kernel_with_ctx(self, ctx, boundargs)  [DWARF]
result expand_kernel_with_ctx(...) {                      // 0x29ab0
    self.check_grid(self.grid);                           // §2.5 — platform-target SPMD guard
    with spmd_kernel_scope(self.grid):                    // 0x2c61f -> contextlib enter
        inputs = self.translate_args_kwargs(boundargs);   // python args -> IR TensorRefs
        kernel_return = self.trace_kernel_impl(inputs);   // ⭐ run user NKI body -> emits penguin.ir
        self.allocate_pending_tensors();                  // 0x55d30 — flush deferred SBUF/PSUM
        self.check_return(kernel_return);                 // NKIFinalizeException = finalize signal
    return (kernel_return, has_collectives);
}
```

`has_collectives` becomes `True` here iff the body issued any
`nki.distributed`/`nki.nccl` collective (which lower through `nki_ctx.*_cc` IR builders);
the flag is later folded straight into `backend_config` (§4). `inline_function` (L347,
`@0x2d0f0`) splices a *sub-kernel* into the caller's builder instead of emitting a
separate node — and a nested kernel may **not** carry its own SPMD grid
(`err_nested_kernel_with_spmd_grid`, k `0x554c0`). [DWARF.]

### 2.4 What the trace produces — node insertion

After re-execution, the module-level `insert_nki_kernel` (L583, `@0x42c20`) wraps the
finished `penguin.ir` kernel into a graph node, binding each operand as a parameter.
Two node classes are selected:

- **`ExternalNativeNkiKernel`** (k `0x55df0`) — a user `@nki.jit` kernel traced **fresh**
  on this call; carries the just-built IR (`serialize_ir_string` / `kernel_ir_str`).
- **`InternalMaterializedNkiKernel`** (k `0x55bd0`) — a registry / `_private_kernels`
  kernel that is **already compiled+materialized** (arrives with `weights_dir` /
  `save_weights` / `private_hbm`); only operand binding happens here.

Each operand must live in `shared_hbm` address space — `insert_nki_kernel` validates
`tensor_addrspace` with the literal guard
`"The input and output tensor of a NKI kernel must be in shared_hbm address space"`
(k `0x557c0`). [DWARF.]

> **GOTCHA — the beta2/beta3 dual-frontend re-trace is not in this file.** It lives in
> `BirCodeGenLoop` (`codegenInternalNativeNkiKernel`). `TraceKernel.trace` is the classic
> re-exec front end; the `External`/`Internal` node it emits is what the downstream bridge
> either codegens directly (External) or re-traces (Internal). Do not attribute KLIR
> re-tracing here — the only two non-`BIRKernel` kernel-node class names in the pool are
> `External…` and `Internal…`.

### 2.5 `check_grid` / `_get_platform_target` — the fallback flag

`check_grid` (L302, `@0x47410`) validates a multi-physical-core SPMD grid against the
resolved platform target: a grid spanning multiple physical cores is legal only on
`trn2`/`trn2n`/`trn3`/`core_v4`/`gen3`, else `err_multi_cores_spmd_not_supported`
(k `0x55500`). `_get_platform_target` (L54, `@0x34da0`) resolves the target from
environment (`NEURON_PLATFORM_TARGET_OVERRIDE`) / DMI
(`/sys/devices/virtual/dmi/id/product_name`); on miss it warns and sets a fallback flag.
[DWARF.]

> **QUIRK — the fallback target is `trn1` and it is folded into the cache key.** The
> verbatim warning is two adjacent literals:
> `"Unable to read MLA target. Assuming cross compile to trn1."` When this fires, `platform_target_is_fallback = True`,
> and that boolean is **part of the specialization cache key (§5) and of the
> `backend_config` (§4)** — so a kernel traced under a guessed target is cached and
> serialized *distinctly* from one traced with a real target. A host that later runs the
> NEFF on a different chip will not silently reuse a `trn1`-fallback specialization.

---

## 3. `FrameworkKernel` and the public `dump_config` pipeline

`FrameworkKernel` is the HLO custom-call emitter. Its class docstring, verbatim from
`.rodata`, shows the exact mapping a framework integrator gets:

```
NKI kernels are represeted as XLA CustomCall instructions in HLO. This class
facilitates the HLO generation for NKI kernels.
  def example_kernel(in1, in2, out):  # reads in1,in2; writes out (pass-by-ref)
should be mapped to:
  %custom-call.2 = f32[16,8,128,512]{3,2,1,0} custom-call(
    f32[16,8,128,512]{3,2,1,0} %p2.2, f32[16,8,128,512]{3,2,1,0} %p1.2),
    custom_call_target="AwsNeuronCustomNativeKernel",
    api_version=API_VERSION_UNSPECIFIED,
    metadata={op_type="xla___op_NkiKernelCallImpl" op_name="xla___op_NkiKernelCallImpl"},
    backend_config= # ...omitted
```

Two seams worth pinning, both verbatim from that docstring:

- The **HLO instruction returns the output tensor** even though Python NKI is
  pass-by-reference ("the corresponding HLO instruction returns the output tensor").
  The reconciliation between the two semantics is the `operand_output_aliases` donation
  table (§4.2).
- `op_type`/`op_name` default to `"xla___op_NkiKernelCallImpl"` and surface in
  `neuron-profiler`; `api_version` is `API_VERSION_UNSPECIFIED`; the
  `custom_call_target` "should always be `AwsNeuronCustomNativeKernel`".

Framework integrators subclass `FrameworkKernel` and implement exactly three abstract
hooks, each with its own docstring and symbol:

| hook | contract |
|---|---|
| `is_framework_tensor(t) -> bool` | `True` ⇒ treat `t` as a tensor; `False` ⇒ compile-time constant |
| `map_framework_tensor(t) -> (shape, dtype)` | `"map_framework_tensor must return a tuple of shape and type"` is the enforced error |
| `translate_to_neuron_dtype(dt)` | framework dtype → neuron/numpy dtype |

`__init__` kwargs: `func`, `func_name` (default `func.__name__`), `grid` (optional SPMD
grid), `enable_cache` (default `True` — `"Default True. Whether to cache the
specializations"`). It allocates the per-instance specialization cache attribute
**`__neuron_kernel_interface_kernel_cache__`**. Subclasses present in the binary:
`UnifiedKernel`, `LegacyFrameworkKernel`.

### 3.1 `dump_config` — the public generator

`dump_config(*args, **kwargs)` is the public config generator; its docstring, verbatim:

```
Returns the `backend_config`, the list of input names and the list of the output name,
based on given arguments. If `self.enable_cache` is True, `dump_config` will try to
retrieve the results from the cache using `args`, `kwargs` and the spmd launch grid and
other kernel attributes as key to identify the identical backend_config. Otherwise,
`dump_config` will always generate new backend_config.
# NOTE: THis is still used by legacy framework code, dont change the signature
```

`dump_config` (decompiled `@0x18860`) just binds and delegates:

```c
// dump_config(self, *args, **kwargs)  — refs: bind_arguments, dump_config_with_boundargs
tuple dump_config(FrameworkKernel *self, PyObject *args, PyObject *kwargs) {  // 0x18860
    boundargs = self.bind_arguments(args, kwargs);          // inspect.BoundArguments
    return self.dump_config_with_boundargs(boundargs);
}
```

### 3.2 `dump_config_with_boundargs` — the ordered pipeline

This is the core of the page. The method's call vocabulary sits in the string pool
(`bind/apply`, `map_args`, `specialize_and_call`, `translate_return_types`,
`generate_operand_output_aliases`, `_generate_hash_key` + cache get,
`encode_backend_config`, `assemble_result`, `has_collectives`, `enable_cache`,
`Sequence`). The ordered pipeline:

```c
// dump_config_with_boundargs(self, boundargs) -> (backend_config, input_names, output_names, ...)
tuple dump_config_with_boundargs(FrameworkKernel *self, BoundArguments b) {
    // (0) cache short-circuit: build the lookup key and try the OrderedDict first  (§5)
    if (self.enable_cache) {
        key = self._arg_hash_key(b);                  // bind -> _map_args -> _generate_hash_key
        if (key in self.__neuron_kernel_interface_kernel_cache__)
            return self.__neuron_kernel_interface_kernel_cache__[key];  // full cached result

    }
    // (1) reduce framework tensors to DeclTensor placeholders; constants pass through
    inputs = self._map_args(b);                       // §4.1

    // (2) TRACE: re-execute the body, learn the IR + collective flag
    kernel_return, has_collectives = self.specialize_and_call(b);  // §2.2

    // (3) translate returned nki_tensors to (shape,dtype); a Sequence -> result_is_sequence
    outputs, result_is_sequence = self.translate_return_types(kernel_return);

    // (4) compute in-place donation pairs (operand_index -> output_index)
    aliases = self.generate_operand_output_aliases(inputs, outputs);  // §4.2

    // (5) serialize the dict to base64(JSON)
    cfg = self.encode_backend_config(has_collectives, kernel_return, ...); // §4

    // (6) package + store under the cache key
    res = self.assemble_result(cfg, input_names, output_names, aliases,
                               constant_values, outputs, has_collectives,
                               key, kernel_return, result_is_sequence);
    if (self.enable_cache) self.__neuron_kernel_interface_kernel_cache__[key] = res;
    return res;
}
```

> **NOTE — what gets cached is the *whole result*, not just the config.** `assemble_result`
> packages `(backend_config, input_names, output_names, operand_output_aliases,
> constant_values, return_types, has_collectives, cache_key, kernel_return,
> result_is_sequence)` and *that whole tuple* is stored under `cache_key`. A cache hit
> therefore reuses the alias table and the return-type metadata too — not merely the
> serialized string.

---

## 4. The `backend_config` payload

### 4.1 `_map_args` — the only framework-specific surface

`_map_args(boundargs)` iterates the bound arguments and calls
`_map_to_decltensor_or_passthrough` on each:

```c
// _map_to_decltensor_or_passthrough(self, t)
PyObject* _map_to_decltensor_or_passthrough(FrameworkKernel *self, PyObject *t) {
    if (self.is_framework_tensor(t)) {                  // subclass hook
        shape, dt = self.map_framework_tensor(t);       // subclass hook -> (shape,dtype)
        ndt       = self.translate_to_neuron_dtype(dt); // subclass hook -> neuron dtype
        return DeclTensor(shape, ndt);                  // a tracing placeholder (nki_tensor)
    }
    return t;                                            // compile-time constant operand
}
```

This is the **single** point at which a torch/jax/numpy tensor is reduced to
`(shape, dtype)` + neuron dtype. Everything framework-specific is concentrated in the
three hooks; the rest of the pipeline is framework-agnostic.

### 4.2 `generate_operand_output_aliases` — pass-by-ref → HLO donation

For each output that aliases an input buffer (the Python "write into the last arg"
outputs), emit an `(operand_index → output_index)` pair so XLA *donates* the input
buffer to the output and skips the copy. The referenced helpers `findAliasTensor`,
`hasAliasedTensor` and `get_alias_output_index` are all real symbols, the last as the
nested closure `generate_operand_output_aliases.get_alias_output_index`.

```c
// generate_operand_output_aliases(inputs, outputs) -> [(operand_idx, output_idx), ...]
list generate_operand_output_aliases(...) {
    for (out in outputs)
        if (hasAliasedTensor(out)) {
            in_idx  = findAliasTensor(out, inputs);   // which input this output writes into
            out_idx = get_alias_output_index(out);    // closure: which result slot
            emit (in_idx, out_idx);
        }
}
```

This is exactly the HLO `output_operand_aliasing` (torch-xla
`OutputOperandAliasing`) / `operand aliasing` (JAX `operand_output_aliases`) field on the
custom-call. The two bridges differ only in builder mechanics; the alias *table* is
produced once here.

### 4.3 `encode_backend_config` — the base64-JSON dict

`encode_backend_config` (decompiled `@0x296d0`) builds a Python dict then
`base64.b64encode(json.dumps(cfg).encode()).decode()` (the referenced names `json`,
`dumps`, `base64`, `b64encode`, `encode`, `decode` are all in the pool). The **complete
key schema** — every key below appears as an interned `__pyx_n_u_*` string in
`FrameworkKernel.so`'s `.rodata`:

```jsonc
// backend_config (decoded from base64; this dict is json.dumps'd then b64encoded)
{
  "kernel_version":               <KERNEL_VERSION>,   // module const (see QUIRK below)
  "func_name":                    "example_kernel",   // func.__name__ or the override
  "grid":                         <serialized SPMD grid>,
  "has_collectives":              false,              // from the trace (§2.3); true if nccl/distributed
  "matmul_mac_count":             <int>,              // TraceKernel.matmul_mac_count sum over IR insts
  "platform_target":             "trn2",              // trn1/trn2/trn2n/trn3/inf2/...
  "platform_target_is_fallback":  false,              // true => target was GUESSED as trn1 (§2.5)
  "opts":                         <serialized CompileOpts>,
  "serialize_dims":               <tensor dim metadata>,
  "serialize_ir_string":          "<the serialized penguin/BIR program>",  // ⭐ the kernel itself
  "tiled":                        <tiling marker>,
  "constant_values":              <non-tensor const operands>,
  "kernel_return":                <return descriptor>,
  "result_is_sequence":           false               // multiple returns -> a Sequence
}
```

> **⭐ The kernel program ships *inside* the HLO custom-call.** `serialize_ir_string`
> is the serialized `penguin`/BIR program — the actual traced kernel body. NKI does not
> hand XLA a symbol to compile later; it base64-embeds the *whole IR* in the
> `backend_config`. The Neuron compiler backend that consumes the HLO re-reads this
> string to rebuild the kernel. (Wire details of that IR: [Part 7
> BIR](../bir/inst-hierarchy.md).) `serialize_ir_string` is both an interned key string
> and a `TraceKernel` insert-path vocabulary word, at `0x56030`.

> **QUIRK — `KERNEL_VERSION` is a module global, not a literal in `.rodata`.** Grepping
> the binary finds the *key name* `kernel_version`/`KERNEL_VERSION` (both an interned
> Unicode key `__pyx_n_u_kernel_version` and the module-global reference
> `__pyx_n_s_KERNEL_VERSION`) but **no version literal**. It is computed/imported at
> module init and emitted by value — i.e. the schema *version* of the `backend_config`
> contract, distinct from the wheel version `2.24.5133.0+58f8de22`. [INFERRED] Reading it
> as a config-schema version constant is the best available interpretation; the value
> itself is not statically recoverable from `FrameworkKernel.so` alone.

> **NOTE — `matmul_mac_count` is a cost annotation, not a correctness field.**
> `TraceKernel.matmul_mac_count` (L436, `@0x28a20`) walks the emitted instructions
> (`MatMulOp` / `MatMulSparseOp` in the pool) and sums MAC counts
> (`total_arithmetic_ops`, `flops`, `total` vocabulary). It is surfaced for the
> runtime/profiler's roofline, not consumed for scheduling. [DWARF.]

---

## 5. The specialization cache

The cache is a per-instance `OrderedDict` named `__neuron_kernel_interface_kernel_cache__`
holding **one traced+compiled specialization per distinct call signature**. The lookup
key is built by `_arg_hash_key` → `_generate_hash_key`:

```c
// _arg_hash_key(args, kwargs)  -> cache key for one call
key _arg_hash_key(self, args, kwargs) {
    b = self.bind_arguments(args, kwargs);
    inputs = self._map_args(b);                  // same reduction as the trace path
    return self._generate_hash_key(b);
}

// _generate_hash_key(b) -> (per-arg key tuple, grid, opts, platform_target_is_fallback)
key _generate_hash_key(self, b) {
    per_arg = [ self._return_shape_dtype_or_hashable(a) for a in b ];
    return (per_arg, self.grid, self.opts, self.platform_target_is_fallback);
}

// _return_shape_dtype_or_hashable(t)
key _return_shape_dtype_or_hashable(self, t) {
    if (self.is_framework_tensor(t)) return (shape, dtype);   // tensors keyed by shape+dtype only
    if (isinstance(t, Hashable))     return t;                // non-tensor args ARE the key
    raise err_nki_param_not_hashable;                         // else: hard error
}
```

> **GOTCHA — non-tensor kernel arguments must be hashable compile-time constants, and
> they specialize the kernel.** A framework tensor contributes only `(shape, dtype)` to
> the key (its *values* are runtime). But a plain Python argument (a block size, a flag,
> a tuple of strides) is hashed *by value* and stored in the key — so each distinct value
> produces a **separate traced specialization**, and a non-hashable arg (e.g. a `list`)
> raises `err_nki_param_not_hashable` rather than being silently ignored.
> (`_return_shape_dtype_or_hashable` is a real symbol carrying a `.genexpr` scope struct.)

**Key components, and why each is in the key:**

| component | reason |
|---|---|
| per-arg `(shape, dtype)` or hashable value | different shapes/dtypes/consts → different IR |
| `grid` | the SPMD launch grid changes the expansion (§2.3) |
| `opts` (`CompileOpts`) | compile options change the produced program |
| `platform_target_is_fallback` | a guessed-`trn1` trace must not be reused for a real target (§2.5) |

> **NOTE — there is no eviction.** It is a plain `OrderedDict` with no size cap or TTL
> in the binary. For a workload that calls one kernel with thousands of distinct
> *hashable-but-varying* constant arguments, the cache grows unbounded for the kernel
> object's lifetime. (This is also why the JAX bridge ships the env escape hatch below.)

> **GOTCHA — the JAX lowering can desync from this cache.** `_jax.so` carries the
> verbatim message *"NKI trace is not properly cached, use `export
> NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING=TRUE` to workaround the issue"* and reads
> `NKI_DONT_CACHE_TRACE_FOR_JAX_LOWERING`, both in `_jax.so`. Setting it forces a
> re-trace at lowering time instead of trusting the cached `JaxTraceResult`. This is the
> JAX-side seam onto the same cache (full bridge: [3.13](../frontend/framework-bindings.md)).

---

## 6. End-to-end: one `@nki.jit(mode="jax")` call

Tying the spine together for a single JAX-mode invocation
`y = my_kernel(in1, in2, out)`:

1. `@nki.jit` returned a `GenericKernel`; argument types resolve to `JAXKernel`
   (6.0.2). `JAXKernel.__call__` registers a JAX primitive and runs abstract-eval.
2. **Abstract-eval** calls `dump_config(in1, in2, out)`.
   `bind_arguments` → `BoundArguments`; `_arg_hash_key` builds the key
   `((shape,dtype)×3, grid, opts, fallback)`.
3. **Cache miss** ⇒ `_map_args` reduces each framework tensor to a `DeclTensor`;
   `specialize_and_call` enters `spmd_kernel_scope`, **re-executes `my_kernel`'s body**,
   emitting `penguin.ir` ops via `GeneratedNeuronCodegen`; `has_collectives` is set if the
   body called any collective.
4. `translate_return_types` yields the output `(shape,dtype)`;
   `generate_operand_output_aliases` discovers `out` aliases input #2 and emits
   `(2 → 0)`.
5. `encode_backend_config` serializes the 14-key dict (with the embedded
   `serialize_ir_string`) to base64-JSON; `assemble_result` packages everything and
   stores it under the cache key.
6. **Lowering** emits the stablehlo `custom_call(call_target="AwsNeuronCustomNativeKernel",
   backend_config=<base64>, operand_output_aliases=[(2,0)])`. The output HLO tensor is
   the donated `out` buffer.
7. On a later call with the **same** shapes/dtypes/grid/opts/consts, step 2 **hits the
   cache** and the whole assembled result is reused — no re-trace, no re-serialize.

The torch-xla path is identical through step 5; only the HLO builder (`scribe` vs
stablehlo) and the target name (`AwsNeuronNkiKernel` vs `AwsNeuronCustomNativeKernel`)
differ (6.0.2 / 3.13).

---

## 7. Limits of this reading

All fourteen `backend_config` keys are interned key strings in `FrameworkKernel.so`
(`kernel_version`, `func_name`, `grid`, `has_collectives`, `matmul_mac_count`,
`platform_target`, `opts`, `serialize_dims`, `serialize_ir_string`, `tiled`,
`constant_values`, `kernel_return`, `result_is_sequence`, plus
`platform_target_is_fallback`, which appears both as `__pyx_n_s_platform_target_is_fallback`
and as the `_platform_target_is_fallback` attribute — it is both a config key and a
cache-key component). The custom-call target `"AwsNeuronCustomNativeKernel"`, the
`"Unable to read MLA target. Assuming cross compile to trn1."` warning, and the cache
attribute name are likewise verbatim literals.

Three things are weaker:

- **The value of `KERNEL_VERSION`.** [INFERRED] Only the names are in the binary; there is
  no version literal (§4.3).
- **The statement *order* inside `dump_config_with_boundargs`.** The individual method
  names are real symbols, but the sequence is reconstructed from the call vocabulary plus
  data dependency — you cannot alias outputs before you have outputs, and you cannot
  encode before you have `has_collectives`.
- **Every `TraceKernel` method body** (`specialize_and_call`, `expand_kernel_with_ctx`,
  `call_impl`) is [DWARF]-reconstructed: disassembled and line-table-keyed rather than
  IDA-decompiled.

---

## Cross-references

- [6.0.2 NKI Entrypoints & Argument Dispatch](./entrypoints.md) — the five entrypoints,
  `compute_mode`, `GenericKernel`, the `NumpyKernel` exec path.
- [3.13 Framework Bindings](../frontend/framework-bindings.md) — JAX / torch-xla
  custom-call registration & lowering hooks (the consumer of `dump_config`).
- [6.6.3 Re-trace / Cache](../nki/retrace-cache.md) — downstream cache reuse semantics.
- [Part 7 — BIR Instruction Hierarchy](../bir/inst-hierarchy.md) — the wire format that
  `serialize_ir_string` carries inside the `backend_config`.
