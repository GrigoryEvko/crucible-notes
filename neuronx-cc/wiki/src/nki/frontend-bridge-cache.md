# NKI-Frontend Bridge and Binary Cache

> *All addresses on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, cp310 wheel, `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (BuildID `cb19fae04f37f9a7ef7acc610b6d32d36ac816f1`). This `.so` is an **unstripped** Cython extension with DWARF; every offset below is verifiable with `nm -C`, `addr2line`, and the per-method IDA ctree decompile. cp311/cp312 ship the same method roster at different addresses.*

## Abstract

When the beta3 BIR codegen loop walks a Penguin macro graph and hits an **internal NKI kernel** op — a compiled `_private_kernels` / `private_nkl` leaf such as `blockwise_mm`, `conv2d_pbp_*`, `resize_nearest_fixed_dma_kernel`, or `tiled_dve_transpose_10` — it cannot lower the kernel directly. It must *re-trace the whole kernel through the "new NKI frontend"* at codegen time, turning a Python `@nki` function back into a serialized binary, and then wrap that binary in a single BIR `NKIKLIRKernel` instruction. This page documents the bridge that performs that re-trace and the in-memory cache that stops it from happening twice for an identical kernel.

The bridge is a thin dispatcher, `_trace_internal_kernel_to_new_nki_frontend` (`@0x5cd10`), wrapped by an orchestrator op-codegen, `codegenInternalNativeNkiKernel` (`@0x8d630`). The codename "new NKI frontend" hides a two-way fork — **beta2** traces the kernel to KLIR (`TraceKernel.trace`, format `'klir'`) and **beta3** compiles it to BIR (`ParserFrontend.compile_to_bir`, format `'bir'`). The compiled cp310 dispatcher does **not** branch: it hard-calls the beta3 leaf, and the beta2 leaf — though present as a fully compiled method — has no live caller in this binary. The dual-frontend the docstrings advertise is, for the internal-kernel path, **beta3-only**.

Around the expensive re-trace sits a per-compilation, in-memory memo. The key is a **SHA-256 of the sorted key-component items** — kernel name, operand/result shapes and dtypes, op attrs, target class name, and neuroncore count — sorted so that dict insertion order cannot perturb the hash. A hit short-circuits the trace entirely and re-attaches the cached binary to a fresh BIR instruction; a miss runs the beta3 compile and stores the result under three parallel maps. The cache lives on the `BirCodeGenLoop` instance, dies with the compilation pass, and is never serialized.

For reimplementation, the contract is:

- **The bridge**: how a registry-resolved internal kernel is re-traced into BIR via `ParserFrontend.compile_to_bir(format='bir')`, and why the beta2/KLIR arm is retained but dormant.
- **The cache key**: the exact construction of the order-insensitive SHA-256 — which components feed it, how `target`/`num_neuroncores` are folded in, and where the `sort()` sits.
- **The hit/miss short-circuit**: the control flow in the orchestrator, the two instance counters, and the three store maps.
- **The metrics**: how cache statistics and per-kernel-name usage are surfaced to `KernelMetricsCollector` and the debug log.

Every claim below is grounded in the cp310 decompile and disassembly.

| | |
|---|---|
| **Binary** | `BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (11,139,256 B, unstripped Cython) |
| **Orchestrator** | `codegenInternalNativeNkiKernel` (`_243` @`0x8d630`, py4689, 27,664 B) |
| **Bridge dispatcher** | `_trace_internal_kernel_to_new_nki_frontend` (`_241` @`0x5cd10`, py3012, 2,768 B) |
| **Live trace leaf** | `_trace_kernel_beta3` (`_239` @`0x1ad940`) — `ParserFrontend.compile_to_bir` |
| **Dormant trace leaf** | `_trace_kernel_beta2` (`_237` @`0x18ebc0`) — `TraceKernel.trace` (no live caller) |
| **Key builder** | `_compute_cache_key_from_components` (`_5` @`0x7eb10`, py391) |
| **Shape/dtype extractor** | `_extract_tensor_info` (`_3` @`0x5fb00`, py379) |
| **Cache lookup / store** | `_get_cached_new_nki_frontend_binary` (`_7` @`0x154e50`) / `_cache_new_nki_frontend_binary` (`_9` @`0xbc6b0`) |
| **Usage counter** | `_record_cache_usage` (`_11` @`0xcad60`, py263) |
| **Metrics / stats** | `_export_new_nki_fe_metrics` (`_315` @`0x111bd0`) / `_print_new_nki_frontend_cache_statistics` (`_316`/`_317` @`0x1833d0`/`0x18cd70`) |
| **Cache state** | per-`BirCodeGenLoop`-instance dicts + two int counters, init in `__init__` (`_1` @`0xb4bc0`) |

---

## The Bridge

### Purpose

The bridge converts a *registry-resolved internal kernel* — a Python callable plus its marshalled call arguments — into the uniform trace-result dict `{'binary', 'input_names', 'output_names', 'format'}` that the orchestrator can wrap into a BIR `NKIKLIRKernel` instruction. It is the per-kernel mechanization of the project's dual-frontend split: beta3 compiles to BIR in-process; beta2 traces to KLIR for the C++ `KlirToBirCodegen` consumer. In this cp310 build only the beta3 arm is reachable.

The bridge's own docstring (verbatim `.rodata` @`0x1ed040`, the orchestrator's, and @`0x1ed140`, the shared trace-dispatcher's) describes the fork:

```text
Codegen for internal NKI kernels using new NKI frontend path.
Traces the kernel to new NKI frontend at codegen time and creates BIR NKIKLIRKernel instruction.
Uses caching to avoid redundant tracing of identical kernels.

Trace an internal NKI kernel using beta2 (KLIR) or beta3 (BIR) path.
  Environment variable NKI_FRONTEND controls the path:
  - "beta2": Use beta2 KLIR tracing (default)
  - "beta3": Use beta3 BIR compilation
  Returns a dict with keys: 'binary', 'input_names', 'output_names', 'format' ('klir' or 'bir').
```

> **GOTCHA —** the docstring is *stale*. It claims a `NKI_FRONTEND`-gated two-way branch defaulting to beta2. The compiled cp310 dispatcher does no such branch — see [Algorithm](#algorithm) below. Trusting the docstring would lead a reimplementer to wire a beta2 default that this binary never exercises.

### Entry Point

```text
codegenInternalNativeNkiKernel  (_243 @0x8d630)         ── ORCHESTRATOR (op codegen)
  ├─ _extract_tensor_info        (_3  @0x5fb00)          ── operand/result shapes+dtypes
  ├─ _compute_cache_key_from_components (_5 @0x7eb10)    ── SHA-256 order-insensitive key
  ├─ _get_cached_new_nki_frontend_binary (_7 @0x154e50) ── cache probe
  │     ╲ HIT → _record_cache_usage (_11 @0xcad60) → emit from cached blob
  └─ MISS:
        _trace_internal_kernel_to_new_nki_frontend (_241 @0x5cd10)  ── THE BRIDGE
          └─ _resolve_kernel_config (_235 @0xa0ca0)     ── registry.get(func_name)
          └─ _trace_kernel_beta3    (_239 @0x1ad940)    ── ParserFrontend.compile_to_bir  [LIVE]
             └─ (_trace_kernel_beta2 _237 @0x18ebc0     ── TraceKernel.trace  [DORMANT, no caller])
        _cache_new_nki_frontend_binary (_9 @0xbc6b0)    ── store under 3 maps
        → build BIR NKIKLIRKernel → bb.addInstruction
```

### Algorithm

The dispatcher body is small. Its only `trace_kernel_*` attribute access is to `beta3`; it builds a 2-tuple `(inst, cache_key)` and calls the leaf. There is no `os.environ` read, no conditional, and no reference to `beta2` anywhere in the function body.

```c
function _trace_internal_kernel_to_new_nki_frontend(self, inst, cache_key):   // _241 @0x5cd10
    // pyargnames = [self, inst, cache_key]; nargs must == 3   (@0x5cd10+0x88..0x97)
    leaf = getattr(self, "_trace_kernel_beta3")    // n_s_trace_kernel_beta3, ONLY trace attr (L168)
    args = (inst, cache_key)                       // PyTuple_New + Pack inst, cache_key (L177-186)
    return leaf(*args)                             // tp_call → {'binary',...,'format':'bir'}
    // NO branch, NO n_s_trace_kernel_beta2 load, NO os.environ read in this body.
```

The bridge takes **three** positional arguments, not two: the cp310 prologue (`@0x5cd10`) installs `pyargnames = [self, inst, cache_key]` and rejects any call with `nargs != 3`. It then invokes the beta3 leaf with the 2-tuple `(inst, cache_key)`. So the bridge *threads* the precomputed key through to the leaf; it never recomputes it.

#### Proving beta3-only, beta2-dormant

The dispatch is hard-wired, and the evidence is a one-line xref asymmetry across every decompiled body in the module:

```text
n_s_trace_kernel_beta3  appears in 2 decompiled bodies:
    Pyx_CreateStringTabAndInitStrings_0xa205.c   (the interned-string table init)
    ..._241_trace_internal_kernel_to_new_nki_frontend_0x5cd10.c   (the LIVE caller, L168)

n_s_trace_kernel_beta2  appears in 1 decompiled body:
    Pyx_CreateStringTabAndInitStrings_0xa205.c   (string-table init ONLY)
```

`_trace_kernel_beta2` (`_237` @`0x18ebc0`) is a fully compiled method, but its name is interned and never loaded by any runtime body. It is **dead by xref**. `_trace_kernel_beta3` is loaded exactly once — by the dispatcher. The internal-kernel re-trace is therefore **beta3-only** in this build.

> **QUIRK —** the `NKI_FRONTEND` env read does still happen, but in the *orchestrator*, not the dispatcher. `codegenInternalNativeNkiKernel` calls `os.environ.get(__pyx_tuple__195)` at py3039 (`@0x8d8d5`–`@0x8d944`) and the result is threaded into tensor-info extraction — it does **not** select the trace path. The selection the docstring describes was constant-folded to beta3 (or simplified out) at Cython-compile time; the env value survives as data, not as a branch predicate. *(That the compiled dispatcher does not branch on it is read directly; which of const-fold vs. source-simplification produced that is **[INFERRED]**.)*

### The beta3 leaf — `ParserFrontend.compile_to_bir`

The live leaf, `_trace_kernel_beta3` (`@0x1ad940`), imports and calls the new NKI compiler frontend directly. Its body performs `_Pyx_ImportFrom(..., n_s_compile_to_bir)` and binds `n_s_ParserFrontend` (both confirmed in the decompile, L995–L1042), then assembles a `CompileOptions` from the target before compiling.

```c
function _trace_kernel_beta3(self, inst, cache_key):     // _239 @0x1ad940
    (cfg, kernel_func, additional_args, call_args) = self._resolve_kernel_config(inst)   // _235

    target  = get_platform_target()                      // n_s_get_platform_target
    ncores  = target.num_neuroncores_per_sengine
    opts    = CompileOptions(...)                         // set_pipeline_options, disable_backend_optimizations
    opts.set_pipeline_options(...)                        //   @0x1f3a80 'set_pipeline_options'
    specs   = introspect(kernel_func.signature)          // input_specs / output_specs from parameters

    json_path = write_kernel_json(tempfile.mkdtemp(), ...) // kernel_json_path under a temp dir

    binary  = ParserFrontend(...).compile_to_bir(...)    // @0x1f5d70 'compile_to_bir', @0x1f5e50 'ParserFrontend'
                                                         //   → emits BIR directly (the "new NKI frontend")
    return { 'binary': binary,                           // raw BIR bytes
             'input_names':  specs.input_names,
             'output_names': specs.output_names,
             'format': 'bir' }                            // n_u_bir, the interned format token
```

The format value `'bir'` is not a guess from the docstring: it is the interned PyUnicode `__pyx_n_u_bir`, and it is the *same* token the external-KLIR codegen `_233` compares against in its format gate (`if inst.kernel_format == "bir"`, decompile L705/L723–724). So `'bir'` provably exists as a runtime string in this binary.

> **NOTE —** the dormant beta2 leaf, `_trace_kernel_beta2` (`@0x18ebc0`), uses the *other* frontend: a `with TraceKernel(target=..., num_neuroncores_per_sengine=...) as tk:` context that does `tk.bind_arguments(call_args)`, `tk.specialize_and_call(kernel_func, additional_args)`, `klir = tk.trace()`, returning `{...,'format':'klir'}`. Its `klir_binary` feeds the C++ `KlirToBirCodegen` strand. The machinery is intact; only the wiring into the internal-kernel path is severed.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `codegenInternalNativeNkiKernel` (`_243`) | `0x8d630` | Orchestrator: cache + dispatch + BIR emit | CERTAIN |
| `_trace_internal_kernel_to_new_nki_frontend` (`_241`) | `0x5cd10` | Thin dispatcher, hard-calls beta3 | CERTAIN |
| `_trace_kernel_beta3` (`_239`) | `0x1ad940` | `ParserFrontend.compile_to_bir` → format `'bir'` (LIVE) | CERTAIN |
| `_trace_kernel_beta2` (`_237`) | `0x18ebc0` | `TraceKernel.trace` → format `'klir'` (DORMANT) | CERTAIN |
| `_resolve_kernel_config` (`_235`) | `0xa0ca0` | `registry.get(func_name)` → `(cfg, func, args, call_args)` | HIGH |

> **NOTE — the cache-stats method has two addresses, and both are real.** `_pf_..._316_print_new_nki_frontend_cache_statistics` @`0x1833d0` is the Cython implementation core; `_pw_..._317_print_new_nki_frontend_cache_statistics` @`0x18cd70` is its argument-clinic wrapper. Cite `_316`/`@0x1833d0` for the body and `_317`/`@0x18cd70` for the entry. This is the normal Cython `pf`/`pw` split, not two distinct methods.

---

## The Cache Key

### Purpose

The key uniquely identifies a *compiled variant* of an internal kernel so that two structurally identical re-traces collapse to one. "Identical" means: same kernel name, same operand and result shapes and dtypes, same op attrs, same target architecture, and same neuroncore-per-sengine count. The key is a SHA-256 hex digest, computed so that the *order* in which the components were inserted into the dict cannot change the digest.

### Algorithm

`_compute_cache_key_from_components` (`_5` @`0x7eb10`, py391) takes a partially-built `key_components` dict, folds in the target identity, stringifies the **sorted** items, and SHA-256s the UTF-8 encoding. Every step below is read directly from the decompiled C (line anchors are decompile lines in the per-method ctree).

```c
function _compute_cache_key_from_components(self, key_components):   // _5 @0x7eb10
    // (1) fold in the architecture identity  (py394-395)
    key_components['target'] =                                 // PyObject_SetItem, L410
        self.target.__class__.__name__                         // n_s_target → n_s_class → n_s_name (L372/382/396)
    key_components['num_neuroncores'] =                        // PyObject_SetItem, L443
        self.target.num_neuroncores_per_sengine                // n_s_num_neuroncores_per_sengine (L421/431)

    // (2) build a deterministic string of the components  (py398)
    items = key_components.items()                             // n_s_items (L453)
    lst   = list(items)                                        // PySequence_List (L533)
    lst.sort()                                                 // PyList_Sort (L539)   ⭐ ORDER-INSENSITIVITY
    key_str = str(lst)                                         // PyObject_Str if not already unicode (L548)

    // (3) hash  (py401)
    h = hashlib.sha256(key_str.encode())                       // n_s_hashlib→n_s_sha256 (L585), n_s_encode (L600)
    return h.hexdigest()                                       // n_s_hexdigest (L628)
```

The faithful Python equivalent — every token in it read from the decompile:

```c
def _compute_cache_key_from_components(self, key_components):
    key_components['target']          = self.target.__class__.__name__
    key_components['num_neuroncores'] = self.target.num_neuroncores_per_sengine
    key_str = str(sorted(key_components.items()))   // sorted() == the L539 PyList_Sort
    return hashlib.sha256(key_str.encode()).hexdigest()
```

> **QUIRK —** the order-insensitivity is *not* a `sorted(dict)` over keys; it is `PyList_Sort` over the `list(dict.items())` — a sort of `(key, value)` tuples. Two `key_components` dicts that were populated in different insertion orders (e.g. `operands` before `results` vs. the reverse) produce *the same* sorted item list and hence *the same* SHA-256. A reimplementer who hashes `str(dict)` directly — without the intervening `sorted()` — gets an insertion-order-dependent key and will miss cache hits that this binary catches. The `PyList_Sort` call at decompile L539, sitting between `PySequence_List` (L533) and the `sha256` GetAttr (L585), is the single instruction that makes the cache order-insensitive.

#### Proving the order-insensitive SHA-256 against the binary

The proof is the *program-order* of three calls inside `_5`'s body, all in the same straight-line region of the decompile:

```text
L533:  v32 = PySequence_List();      //  list(key_components.items())
L539:  if ( PyList_Sort() != -1 )    //  lst.sort()   ← ORDER-INSENSITIVITY
L585:  ... getattr(hashlib, "sha256")
L600:  ... getattr(key_str, "encode")
L628:  ... getattr(<digest>, "hexdigest")
```

`PyList_Sort` (L539) executes unconditionally on the items list before any hashing occurs; `hashlib.sha256(...).encode().hexdigest()` (L585→L600→L628) consumes the sorted result. There is no code path that hashes the unsorted list. This is a direct, decompile-level confirmation — not an inference from the docstring (the docstring only says *"Compute a cache key from key components"*, `.rodata` @`0x1ed840`, with no mention of sorting).

### The key inputs — `_extract_tensor_info`

The bulk of `key_components` is built by the orchestrator (see [Hit/Miss](#the-hitmiss-short-circuit)) before the key builder folds in `target`/`num_neuroncores`. The shape/dtype portions come from `_extract_tensor_info` (`_3` @`0x5fb00`, py379), docstring *"Helper to extract shape and dtype info from tensors for cache key."* (`.rodata` @`0x1ed880`).

```c
function _extract_tensor_info(self, tensors):           // _3 @0x5fb00
    result = {}
    result['shapes'] = []                               // n_u_shapes, PyList_New + SetItem
    result['dtypes'] = []                               // n_u_dtypes, PyList_New + SetItem
    for t in tensors:
        try:    shp = t.access_shape                    // n_s_access_shape (L521), PRIMARY
        except: PyErr_Clear(); shp = t.shape            // PyErr_Clear (L525) → n_s_shape (L557), FALLBACK
        result['shapes'].append(shp)                    // GetItem n_u_shapes → n_s_append (L577/L620)
        result['dtypes'].append(t.dtype)                // n_s_dtype (L676) → append (L732)
    return result
```

> **NOTE —** the shape preference is `access_shape` *then* `.shape`. `access_shape` is the BIR access-pattern shape (the strided/tiled view the kernel actually sees); `.shape` is the raw tensor shape used only when `access_shape` raises. A reimplementer that keys on `.shape` unconditionally will conflate two kernels that differ only in their access pattern — a correctness-affecting cache collision. The `PyErr_Clear` at L525 is the exact point where the primary attribute's `AttributeError` is swallowed before the fallback.

### Full key-component map

| Component | Source | Built by | Confidence |
|---|---|---|---|
| `kernel_name` | `inst.kernel_name` / `func_name` | orchestrator prologue | HIGH |
| `operands` | `_extract_tensor_info(inst.operands)` → `{shapes, dtypes}` | `_3`, orchestrator L1004 | CERTAIN |
| `results` | `_extract_tensor_info(inst.results)` → `{shapes, dtypes}` | `_3`, orchestrator L1088 | CERTAIN |
| `attrs` | `inst.get_attrs_dict()` | orchestrator L1159 | CERTAIN |
| `out_shape` | `inst.results[..].access_shape` *(ResizeNearest only)* | orchestrator L1258 gate | HIGH |
| `target` | `self.target.__class__.__name__` | `_5` L410 | CERTAIN |
| `num_neuroncores` | `self.target.num_neuroncores_per_sengine` | `_5` L443 | CERTAIN |

The `out_shape` entry is conditional: the orchestrator compares the op against the interned `n_u_ResizeNearest` token (`_Pyx_PyUnicode_Equals`, decompile L1258) and only adds `key_components['out_shape']` for the resize family, whose output geometry is not otherwise captured by the operand shapes.

---

## The Hit/Miss Short-Circuit

### Purpose

The cache is wired entirely inside the orchestrator `codegenInternalNativeNkiKernel` (`_243` @`0x8d630`, py4689). The lookup/store helpers are trivial dict operations; the *decision* — probe, branch, count, emit — lives here. This keeps the helpers reusable and the control flow in one place.

### Algorithm

The orchestrator's cache spine, with every name and counter operation pinned to its decompile line in `_243`:

```c
function codegenInternalNativeNkiKernel(self, inst, bb):   // _243 @0x8d630, py4689
    func_name   = inst.func_name
    kernel_name = inst.kernel_name
    _ = os.environ.get(nki_frontend_key)         // L849 os, L884 environ, L901 get; key load @0x8d99b (see below)

    // build key_components (order here does NOT matter — the key sorts):
    key_components = {}
    key_components['operands'] = self._extract_tensor_info(inst.operands)    // L1004
    key_components['results']  = self._extract_tensor_info(inst.results)     // L1088
    key_components['attrs']    = inst.get_attrs_dict()                       // L1159
    if _Pyx_PyUnicode_Equals(op, "ResizeNearest"):                          // L1258
        key_components['out_shape'] = inst.results[..].access_shape

    cache_key = self._compute_cache_key_from_components(key_components)      // L1399
    cached    = self._get_cached_new_nki_frontend_binary(cache_key)         // L1457

    if cached is not None:                        // ⭐ HIT
        self.cache_hits += 1                      // GetAttr cache_hits L1510 → SetAttr L1563
        print_info("Cache hit for " + func_name + " (cache key: " + cache_key[:N] ...)   // @0x1f6418
        self._record_cache_usage(cache_key)       // L1587
        result = cached
    else:                                         // ⭐ MISS
        self.cache_misses += 1                    // GetAttr cache_misses L1868 → SetAttr L1921
        print_info("Cache miss for " + func_name + " (cache key: " + cache_key[:N] ...)  // @0x1f5f20
        result = self._trace_internal_kernel_to_new_nki_frontend(inst, cache_key)   // L2219  ⭐ EXPENSIVE
        self._cache_new_nki_frontend_binary(cache_key, result, key_components)       // L2300  store

    // common tail — emit the BIR kernel inst from `result`:
    binary = result['binary']; fmt = result['format']        // 'bir'
    k = NKIKLIRKernel(...)                                    // L2577
    k.set_klir_binary(binary)
    k.set_kernel_format(fmt)
    k.set_nki_binary_version_identifier(...)
    k.set_func_args(result['input_names']); k.set_func_outs(result['output_names'])
    bb.addInstruction(k)
```

Two details of that flow are easy to get wrong. The key is the SHA-256 of `sorted(key_components.items())` (§[The Cache Key](#the-cache-key)), and the store takes **three** positional arguments — `(cache_key, result, key_components)` (L2300), not two. The third, `key_components`, is retained for the stats/metrics export.

### The env key — `nki_frontend`, lowercase

The orchestrator's `os.environ.get(...)` call passes `__pyx_tuple__195` as its `(key, default)` tuple (`tp_call(get, __pyx_tuple__195, 0)`, decompile L941). IDA elides the `PyTuple_Pack` varargs in module-init, so the tuple's literal contents are not directly recoverable from the decompile. The disassembly, however, resolves the key: immediately after the `environ`/`get` loads, `@0x8d99b` loads the interned PyUnicode `__pyx_n_u_nki_frontend` — the **lowercase** `'nki_frontend'` — which the env result is compared against.

```asm
0x8d8d5:  mov  rsi, cs:__pyx_n_s_environ
0x8d90d:  mov  rsi, cs:__pyx_n_s_get
0x8d944:  mov  rsi, cs:__pyx_tuple__195            ; the (key, default) tuple — varargs elided in decompile
0x8d99b:  mov  rsi, cs:__pyx_n_u_nki_frontend      ; lowercase 'nki_frontend' — the compared key
```

> **GOTCHA — the internal-kernel env key is lowercase `nki_frontend`, not `NKI_FRONTEND`.** The docstring advertises the uppercase name with default `"beta2"`, but the orchestrator's runtime comparison loads the **lowercase** `__pyx_n_u_nki_frontend` (`@0x8d99b`). Both spellings are interned in the string table; only the lowercase one appears in the internal-kernel codegen body, while the uppercase token is referenced exclusively from the external-kernel siblings and `Pyx_CreateStringTabAndInitStrings`. *(The `__pyx_tuple__195` literal default value is **[INFERRED]** from the docstring — the tuple contents were not byte-recoverable.)*

### The store and lookup helpers

`_get_cached_new_nki_frontend_binary` (`_7` @`0x154e50`) is a single `dict.get`, no side effects, returns `None` on miss:

```c
function _get_cached_new_nki_frontend_binary(self, cache_key):   // _7 @0x154e50
    return self._new_nki_frontend_kernel_cache.get(cache_key)    // GetAttr cache L336 → n_s_get L346
```

`_cache_new_nki_frontend_binary` (`_9` @`0xbc6b0`) writes **three** parallel maps under the same key, in program order:

```c
function _cache_new_nki_frontend_binary(self, cache_key, binary, key_components):   // _9 @0xbc6b0
    self._new_nki_frontend_kernel_cache[cache_key] = binary           // SetItem L641/L649
    self._cache_key_components[cache_key]          = key_components    // SetItem L659/L667
    self._cache_usage_counts[cache_key]            = 1                 // SetItem L681/L686 (value __pyx_int_1)
```

`_record_cache_usage` (`_11` @`0xcad60`, py263) bumps the usage tally on every hit:

```c
function _record_cache_usage(self, cache_key):                  // _11 @0xcad60
    if cache_key in self._cache_usage_counts:                   // PySequence_Contains L331
        self._cache_usage_counts[cache_key] += 1                // PyInt_AddObjC int_1 L358 → SetItem L364
```

> **QUIRK —** the usage counter starts at **1** on the store (the miss-and-compile counts as the first use) and is incremented by `_record_cache_usage` only on subsequent *hits*. So `usage_count` for a variant = (1 from the initial compile) + (number of cache hits). It is the total invocation count of that exact variant, not the hit count alone. `cache_hits`/`cache_misses` are the *global* instance tallies; `_cache_usage_counts[key]` is the *per-variant* tally — three distinct counters with three distinct meanings.

### Cache state and lifetime

All cache state is initialized in `BirCodeGenLoop.__init__` (`_1` @`0xb4bc0`) as five per-instance attributes, in program order:

```c
function __init__(self, ...):                       // _1 @0xb4bc0
    self._new_nki_frontend_kernel_cache = {}        // PyDict_New + SetAttr L814/L822  (key → binary dict)
    self._cache_key_components          = {}        // PyDict_New + SetAttr L831/L839  (key → key_components)
    self._cache_usage_counts            = {}        // PyDict_New + SetAttr L848/L856  (key → int)
    self.cache_hits                     = 0         // __pyx_int_0 + SetAttr L861
    self.cache_misses                   = 0         // __pyx_int_0 + SetAttr L867
```

> **NOTE —** there is **no on-disk persistence** anywhere in these methods. No `open()`, `json.dump`, `pickle`, or `os.path` touches the cache dicts. A fresh `BirCodeGenLoop` is constructed per compilation pass, so the cache is per-compilation and in-memory; it dies with the pass. This is *distinct* from the `FrameworkKernel`/`TraceKernel` `backend_config` cache documented in [framework-kernel-lifecycle](framework-kernel-lifecycle.md), which persists across the kernel lifecycle. Do not conflate the two: the cache here memoizes the *beta3 trace result within a single codegen run*; the lifecycle cache memoizes *kernel-build configuration across runs*.

### Net effect

On a repeated identical internal-kernel invocation — same `kernel_name`, same `target`/`num_neuroncores`, same operand/result shapes and dtypes, same attrs — the SHA-256 matches, `_get_cached` returns the prior `{'binary', 'input_names', 'output_names', 'format':'bir'}`, and the entire beta3 trace + `ParserFrontend.compile_to_bir` compile is **skipped**. The cached BIR blob is re-attached to a fresh `NKIKLIRKernel` instruction via the common tail. This is the *"avoid redundant tracing of identical kernels"* of the orchestrator docstring, mechanized.

---

## Metrics and Statistics

### Purpose

Two epilogue methods surface cache behavior: a human-readable debug report and a structured export to the global kernel-metrics collector. Both are fired from `afterStmtTransform` at per-function teardown.

### `_print_new_nki_frontend_cache_statistics`

`_316` (@`0x1833d0`, py5441; wrapper `_317` @`0x18cd70`) emits a debug report via `self.print_debug` (the BIR-codegen logger, not stdout). It short-circuits if nothing was processed, then prints the count-based statistics:

```c
function _print_new_nki_frontend_cache_statistics(self):    // _316 @0x1833d0
    total = self.cache_hits + self.cache_misses
    if not self._new_nki_frontend_kernel_cache and total == 0:
        return                                              // IsTrue short-circuit
    print_debug("="*80)                                     // banner
    print_debug("New NKI Frontend Kernel Cache Statistics")  // @0x1ef500
    print_debug("Total kernels processed: " + str(total))    // @0x1f3240
    print_debug("  Cache hits:   " + format(hit_rate, '1f'))
    print_debug("  Cache misses: " + ...)
    print_debug("Unique cached variants: " + str(len(cache)))     // @0x1f3600  (distinct cache keys)
    print_debug("Distinct kernel types: " + str(...))             // @0x1f39e0  (distinct kernel_name)
    for name, info in sorted(types.items(), key=lambda x: ..., reverse=True):  // lambda33 @0x64a30
        print_debug("Kernel: " + name)
        print_debug("  Variants: " + str(...))
        print_debug("  Total usage: " + str(sum(...)))
        print_debug("    Operand shapes: " + str(kc['operands']['shapes']))
        print_debug("    Result shapes:  " + str(kc['results']['shapes']))
```

> **NOTE —** the report is purely **count-based**: hits, misses, hit rate, unique variants, distinct kernel types, per-kernel usage and shapes. There is **no** wall-clock "time saved" figure — no `time`/`perf_counter` vocabulary appears in the body. A reimplementer should not expect a latency-savings number; the cache's value is measured in trace-calls avoided, not seconds.

### `_export_new_nki_fe_metrics`

`_315` (@`0x111bd0`, py5270), docstring *"Export new NKI FE kernel metrics to the collector."* (`.rodata` @`0x1ed000`), aggregates per kernel name and records into the singleton `KernelMetricsCollector`:

```c
function _export_new_nki_fe_metrics(self):                  // _315 @0x111bd0
    if not self._cache_key_components: return               // IsTrue short-circuit
    collector = KernelMetricsCollector.get_instance()       // L447/L473 (penguin.KernelMetrics singleton)
    agg = defaultdict(lambda: {'call_count': 0, 'variances': 0})  // defaultdict L524, factory lambda31 @0x52ca0
    for cache_key, kc in self._cache_key_components.items():
        agg[kc['kernel_name']]['call_count'] += self._cache_usage_counts.get(cache_key)
        agg[kc['kernel_name']]['variances']  += 1           // one variant per cache_key
    for kernel_name, stats in agg.items():
        collector.record_kernel(                            // n_s_record_kernel L1427
            kernel_name = kernel_name,
            category    = KernelCategory.NEW_NKI_FE,         // GetAttr NEW_NKI_FE off KernelCategory L1473/L1586
            call_count  = stats['call_count'],
            number_of_variances = stats['variances'])
```

> **GOTCHA — `NEW_NKI_FE` and `NKI_BETA3_FE` are not environment variables.** They are **enum members of `KernelCategory`**, accessed via `GetAttr` off the `KernelCategory` object (L1473/L1586) — metric *buckets*, never compared as strings, never read from the environment. The only environment variable in the internal-kernel path is the lowercase `nki_frontend` ([above](#the-env-key--nki_frontend-lowercase)). `NKI_FRONTEND` and `NKI_BETA3_FE` gate the *external*-kernel siblings, a parallel path documented in [three-sink-kernel-model](three-sink-kernel-model.md).

Each distinct internal-kernel *name* is recorded once under the `KernelCategory.NEW_NKI_FE` bucket, with `call_count` = sum of per-variant usage counts and `number_of_variances` = number of distinct cached variants (cache keys) that name produced.

---

## How a Registry Hit Is Re-Traced and Cached

The end-to-end flow, tying the registry resolution ([internal-kernel-registry](internal-kernel-registry.md)) to this bridge and cache:

```text
Penguin macro op: InternalNkiKernel  (the IT56-class sink — see three-sink-kernel-model)
  │  dispatch_codegen routes by type(op).__name__  →  codegenInternalNativeNkiKernel (_243)
  │
  ├─ extract operand/result tensor-info  (_extract_tensor_info ×2)      ── shapes(access_shape|shape)+dtypes
  ├─ get_attrs_dict + ResizeNearest out_shape special-case
  ├─ compute_cache_key  (_5)  =  sha256( str(sorted(key_components.items())) ).hexdigest()
  ├─ probe  (_get_cached _7)
  │     ╲ HIT  → cache_hits++ ; record_cache_usage (_11) ; result = cached
  │     ╲ MISS → cache_misses++ ;
  │              _trace_internal_kernel_to_new_nki_frontend (_241)       ── THE BRIDGE
  │                 └─ _resolve_kernel_config (_235) ← get_internal_kernel_registry().get(func_name)
  │                 └─ _trace_kernel_beta3 (_239): ParserFrontend.compile_to_bir → {'binary',...,'format':'bir'}
  │              _cache_new_nki_frontend_binary (_9): store under 3 maps, usage=1
  │
  └─ COMMON TAIL: NKIKLIRKernel(binary, format, version_id, func_args/outs) → bb.addInstruction
        │  (the same BIR carrier the external-KLIR codegen _233 builds)
        └─ downstream: lowerKLIRToNKI consumes the NKIKLIRKernel inst on the C++ side
```

The registry maps a macro `func_name` → `InternalKernelConfig` (the *which kernel and how to marshal its args* question); the cache maps a SHA-256 key → compiled BIR binary (the *have we already compiled this exact variant* question). They are complementary and orthogonal: a registry hit still pays the beta3 compile on the first cache miss, and a cache hit still needs the registry only insofar as the orchestrator already resolved the kernel name before keying. The re-emit printer that turns a Penguin macro back into NKI text is a *separate* component — see [nkicodegen-printer](nkicodegen-printer.md); the bridge here re-traces to *binary*, not to text.

---

## Evidence summary

Directly disassembled or decompiled:

- All 13 method symbols, indices, and addresses (`function_addresses.json` + `nm -C`).
- The order-insensitive SHA-256 key: `PySequence_List` (L533) → `PyList_Sort` (L539) → `hashlib.sha256` (L585) → `.encode` (L600) → `.hexdigest` (L628) in `_5`'s decompile.
- The beta3-only dispatch: `n_s_trace_kernel_beta3` loaded once (dispatcher L168), `n_s_trace_kernel_beta2` referenced only from the string-table init.
- `ParserFrontend.compile_to_bir` present in the beta3 leaf (`_Pyx_ImportFrom n_s_compile_to_bir`, L1042); `'bir'` is the interned `n_u_bir` (reused in `_233`'s format gate).
- The orchestrator cache spine, every counter op (`cache_hits`/`cache_misses` GetAttr→AddObjC→SetAttr) and helper call at its decompile line.
- The three-map store (`_9` L641–L686), the `dict.get` lookup (`_7`), the contains-and-increment usage counter (`_11`), and the five-attribute `__init__` init (`_1` L814–L867).
- The lowercase `nki_frontend` env key (disasm `@0x8d99b`); `NEW_NKI_FE` as a `KernelCategory` GetAttr (not a string/env).
- All cache-banner and docstring `.rodata` strings at their cited addresses.

Open questions:

- **`__pyx_tuple__195` contents** (the `os.environ.get` `(key, default)` tuple): IDA elided the `PyTuple_Pack` varargs in module-init, so the literal *default* value — the docstring says `"beta2"` — is **[INFERRED]**, not byte-recovered. The *key* is resolved from the adjacent `n_u_nki_frontend` load.
- **Which of {compile-time const-fold, source simplification}** produced the beta3-only dispatch is **[UNRESOLVED]**; only the fact that the compiled dispatcher does not branch is read directly.
- **The exact `NKIKLIRKernel` attr-binding order in `_243`'s tail** was not byte-ordered here; it is structurally identical to the external-KLIR codegen `_233`, which was traced.
- **cp311/cp312 parity**: the method roster is present in all three wheels (`native_exports.json`), but the beta2-dormancy xref was only re-confirmed on cp310. Whether another build re-enables the beta2 arm is untested.

---

## Related Components

| Name | Relationship |
|---|---|
| `codegenExternalNativeNkiKlirKernel` (`_233` @`0x137c40`) | Builds the *same* `NKIKLIRKernel` carrier for pre-traced external `@nki` kernels; its `kernel_format == "bir"` gate confirms the `'bir'` format token |
| `_resolve_kernel_config` (`_235` @`0xa0ca0`) | Registry resolution feeding both trace leaves; maps `func_name` → `InternalKernelConfig` |
| `KernelMetricsCollector` (`penguin.KernelMetrics`) | Singleton sink for `_export_new_nki_fe_metrics`; `KernelCategory.NEW_NKI_FE` bucket |
| `afterStmtTransform` (`_313` @`0x5e970`) | Per-function epilogue that fires both stats/metrics methods |

## Cross-References

- [The Three-Sink Kernel Model](three-sink-kernel-model.md) — `InstNKIKLIRKernel` (IT56) as the registry-traced sink this bridge serves; the external-kernel parallel path and its `NKI_FRONTEND` gate
- [The Internal Kernel Registry](internal-kernel-registry.md) — `_INTERNAL_KERNEL_REGISTRY` and `_resolve_kernel_config`; the `func_name → InternalKernelConfig` resolution that precedes this re-trace
- [NkiCodegen Re-Emit Printer](nkicodegen-printer.md) — the `penguin.ir → NKI-text` re-emit path; distinct from this bridge's re-trace-to-*binary*
- [Framework Kernel Lifecycle](framework-kernel-lifecycle.md) — the `FrameworkKernel`/`TraceKernel` lifecycle and its `backend_config` cache; contrast with this *per-compilation in-memory* cache
- [BirCodeGenLoop](bircodegenloop.md) — the host beta3 codegen loop these bridge/cache methods live in
