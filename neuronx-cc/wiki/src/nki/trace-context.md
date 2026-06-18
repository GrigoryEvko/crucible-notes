# TraceContext: the Abstract-Interpretation Engine

> *All symbols, offsets, and interned strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, `neuronxcc/nki/compiler/backends/neuron/TraceContext.cpython-310-x86_64-linux-gnu.so` (cp311/cp312 carry identical Cython). Other versions will differ. Addresses are file offsets into the `.so` `.text`; `.py` line numbers come from the embedded `_Pyx_AddTraceback` strings.*

## Abstract

NKI ("Neuron Kernel Interface") kernels are written as ordinary Python functions, but they never *run* as Python. When a kernel is launched, `TraceContext` **abstract-interprets** the function body once: it walks the Python call/branch/loop/return structure and, at every decision point, chooses between **tracing** an operation into the Penguin IR (via a semantic builder `ctx.sema`) and **executing** it concretely in the host CPython interpreter. The result is a Penguin IR graph; the Python source is the program that builds that graph, not the program that runs on the device. This is the same shape as a tracing JIT (think `torch.jit.trace`, JAX's `jaxpr` tracer, or a staged metaprogramming evaluator): concrete Python control flow is executed and specialized away, while data-carrying values become symbolic IR nodes.

The central organ is `TraceContext.call` — a dispatcher invoked for *every* call site in the kernel body that decides trace-vs-concrete. Four gates draw the boundary: a callee marked as an `@nki` op (`nki_func`) is routed to the builder; a function decorated `@dont_trace` (`__nki_dont_trace__`) is run live; a nested `TraceKernel` recurses; and any function whose *defining module* is in the exclusion set (`neuronxcc.nki`, `neuronxcc.starfish.penguin`, `numpy`, `math`) is library code and runs concretely. Everything else — the user's own helper functions — is *inlined and traced*. Around this sit the static control-flow evaluators (`eval_if_cnd`, `if_scope`, `while_scope`): Python `if`/`while` are evaluated *during* the trace on concrete conditions, so a branch is taken (and unrolled) rather than emitted; a branch condition that depends on device data (an `nki_tensor` or a `ScalarPredicate`) is **rejected** — NKI has no dynamic control flow at the Python level.

The whole machine hangs off one process-singleton, `TraceContext.global_ctx`, installed by the `new_ctx` classmethod at trace start and cleared at teardown. That single mutable cell is the ambient context every language intrinsic reaches through [`nki_ctx`](nki-ctx.md); its `cur_scope` is the active emission scope and its `sema` *is* the definition of "we are inside a traced kernel." This page documents the engine itself; the accessor module, range/loop semantics, and the type system are split into the cross-referenced pages below.

For reimplementation, the contract is:

- **The trace boundary** — the exact four-gate test in `call` that decides whether a callee is traced into IR or executed in Python (`nki_func` → `__nki_dont_trace__` → `TraceKernel` → `_exclude_modules`), plus the `is_nki_data` predicate that classifies a value as symbolic-or-concrete.
- **Static control-flow evaluation** — how `if`/`while` are concretely evaluated during the trace, and the precise rejection rule for data-dependent branches (`err_dynamic_control_flow_not_supported`).
- **The singleton lifecycle** — `new_ctx` installs `global_ctx`; the scope chain (`KernelScope`/`FunctionScope`/`StmtScope`) it mutates; teardown that re-raises `err_nki_api_outside_of_nki_kernel` afterward.

| | |
|---|---|
| **Module** | `neuronxcc/nki/compiler/backends/neuron/TraceContext.py` (Cython `.so`) |
| **Module docstring** | "This file define the TraceContext class, it define the interface for tracing a nki function" |
| **Dispatcher** | `TraceContext.call` — `__pyx_pw_…12TraceContext_10call` @ `0x2b380` (~546 basic blocks) |
| **Static-if evaluator** | `TraceContext.eval_if_cnd` @ `0x28070` |
| **Trace boundary set** | `TraceContext._exclude_modules` @ `0x193f0` → `(neuronxcc.nki, neuronxcc.starfish.penguin, numpy, math)` |
| **Singleton** | `TraceContext.global_ctx` — installed by `new_ctx` (classmethod) @ `0x24340` |
| **NKI-data predicate** | `TraceContext.is_nki_data` @ `0x16be0` → `isinstance(v, (nki_tensor, mask))` |
| **Ambient accessor** | `nki_ctx.get_cur_scope` / `nki_ctx` / `nki_ctx_or_none` (sibling `.so`) → `TraceContext.global_ctx` |

---

## The Trace Boundary

### Purpose

The "trace boundary" is the line that separates *user kernel code* (which becomes IR) from *library code* (which runs as Python and produces concrete values that the trace folds in). Drawing this line correctly is the whole point of `call`: trace too much and you try to symbolically execute `numpy`; trace too little and the user's own helper functions never reach the device. The boundary is defined declaratively by three markers on the callee and one set of excluded modules.

### The four markers

| Marker | Where checked | Meaning | Disposition |
|---|---|---|---|
| `nki_func` attribute | `call` gate 1 (`__pyx_n_u_nki_func`) | callee is an `@nki` op (a `NKIFunc`) | route to `self.sema` (emit IR) |
| `__nki_dont_trace__ == True` | `call` gate 2 (`dont_trace_attr`) | explicitly opted out via `@dont_trace` | execute concretely |
| `isinstance(func, TraceKernel)` | `call` gate 3 | a nested traced kernel | recurse into the kernel |
| defining module ∈ `_exclude_modules()` | `call` gate 4 (`inline_function` path) | library code | execute concretely |

`is_nki_data` (`0x16be0`) is the orthogonal *value*-side classifier, distinct from the callee gates above:

```c
// TraceContext.is_nki_data(v)        __pyx_pw_…3is_nki_data @ 0x16be0
//   string-table symbols: __pyx_n_s_v, __pyx_n_s_nki_tensor, __pyx_n_s_mask
bool is_nki_data(v):
    return PyObject_IsInstance(v, nki_tensor)   // module global `nki_tensor`
        || PyObject_IsInstance(v, mask);        // module global `mask` (EQTileMask family)
```

A value is "nki data" iff it is an IR-symbolic tensor (`nki_tensor`) or a tile-mask (`mask` → `EQTileMask`). This predicate is what `call` uses for its return-consistency check, and it is conceptually why a [`DynamicScalar`](type-system.md) comparison cannot be a branch condition: those produce predicate objects, not `bool`.

### `_exclude_modules`

```c
// TraceContext._exclude_modules()    __pyx_pw_…1_exclude_modules @ 0x193f0   (staticmethod)
//   string-table: __pyx_n_s_neuronxcc_nki, __pyx_n_s_neuronxcc_starfish_penguin,
//                 __pyx_n_s_numpy, __pyx_n_s_math
tuple _exclude_modules():
    import neuronxcc.nki                  // .py line 45
    import neuronxcc.starfish.penguin     // .py line 46
    return (neuronxcc.nki,                // the NKI op surface — already traced ops
            neuronxcc.starfish.penguin,   // the IR target itself
            numpy, math);                 // host numeric libraries
```

A callee whose `inspect.getmodule(func).__name__` (split on `"."`, see `call` gate 4) is *that module or a submodule of it* is treated as a leaf: it runs in Python and its return value is folded into the trace as a concrete constant. This is why `math.ceil(...)`, `numpy.array(...)`, and the internal NKI/Penguin machinery are never re-traced.

> **NOTE —** the exclusion is by *defining module*, not by name. A user function named `ceil` in the kernel's own module is still traced; `math.ceil` is not. The split-on-`"."` (`__pyx_kp_u__11` is the `"."` separator in `call`) does prefix matching against the four exclude entries' dotted names.

### `dont_trace` — the explicit opt-out

```c
// dont_trace(func=None)              module function, wrapper @ 0x1ea60
//   dont_trace_attr = "__nki_dont_trace__"   (CONFIRMED interned constant)
//   docstring: "Decorator to mark a function as not being traced by NKI."
decorator dont_trace(func=None):
    def wrapper(func_):
        PyObject_SetAttr(func_, "__nki_dont_trace__", True);   // .py lines 338-339
        return func_;
    // bare-decorator (@dont_trace) vs parameterized (@dont_trace()) both handled
    // via the func is None / func is not None split.
```

`@dont_trace` writes a sentinel attribute; `call` later reads it with `getattr(func, "__nki_dont_trace__", False)`. It is the user's escape hatch to force a piece of in-module Python to run concretely (e.g. a pure-Python shape computation that should not become IR).

### Function Map

| Function | Offset | Role | Confidence |
|---|---|---|---|
| `TraceContext._exclude_modules` | `0x193f0` | builds the 4-module exclusion tuple | CERTAIN |
| `TraceContext.is_nki_data` | `0x16be0` | `isinstance(v, (nki_tensor, mask))` value classifier | CERTAIN |
| `dont_trace` (+ `.<locals>.wrapper`) | `0x1ea60` (wrapper) | set `__nki_dont_trace__` sentinel | CERTAIN |

---

## `TraceContext.call` — the Dispatcher

### Purpose

`call` is invoked for every call expression in the traced body. It is the heart of the engine: a single linear cascade of the four gates above, ending in either an IR emission, a concrete Python call, a recursion, or an inlined trace — followed by a return-type consistency check. The Cython body is large (~546 basic blocks) because each gate carries the full attribute-fetch and error-path machinery; the *logic* is a short if-cascade.

### Algorithm

```c
// TraceContext.call(self, func, *args, **kwargs)
//   __pyx_pw_…12TraceContext_10call @ 0x2b380
//   string-table symbol stream (dispatch order, verified verbatim):
//     self, func, nki_func, sema, NKIFunc, dont_trace_attr, TraceKernel,
//     inline_function, inspect, isfunction, isbuiltin, module, "." ,
//     exclude_modules, genexpr, is_nki_data, self_2, has_nki_data,
//     function_without_nki_data_as_inp, opts
result call(self, func, *args, **kwargs):

    // ---- Gate 1: an @nki op -> emit into IR via the semantic builder ----
    if hasattr(func, "nki_func"):                 // NKIFunc marker
        return self.sema.<dispatch>(func, *args, **kwargs);

    // ---- Gate 2: explicit @dont_trace -> run concretely ----
    if getattr(func, dont_trace_attr, False):     // "__nki_dont_trace__"
        return func(*args, **kwargs);

    // ---- Gate 3: a nested traced kernel -> recurse ----
    if isinstance(func, TraceKernel):             // the .TraceKernel import (D-P20)
        return func.<trace_entry>(*args, **kwargs);

    // ---- Gate 4: library module gate -> concrete; else inline+trace ----
    mod = inspect.getmodule(func);
    if (inspect.isfunction(func) || inspect.isbuiltin(func))
            && module_dotted_name(mod).split(".")          // "." == __pyx_kp_u__11
               matches_prefix self._exclude_modules():
        return func(*args, **kwargs);             // library code -> concrete

    result = inline_function(func, *args, **kwargs);   // trace the body (new FunctionScope)

    // ---- Return-consistency check ----
    //   has_nki_data(args) = any(is_nki_data(a) for a in args)
    //   (TraceContext.call.<locals>.genexpr + .<locals>.has_nki_data closures)
    if (not has_nki_data(args)) and is_nki_data(result):
        raise self.opts.<error>(
            "function without nki data as input should not return nki data");
    return result;
```

The two nested closures are real and compiled separately:

- `TraceContext.call.<locals>.genexpr` — the `is_nki_data(a) for a in args` generator.
- `TraceContext.call.<locals>.has_nki_data` (`__pyx_pf_…4call_has_nk` @ `0x1af80`, wrapper @ `0x1b770`) — the `any(...)` over that generator.

> **GOTCHA —** the order of the gates is the contract, not an accident. `nki_func` is checked *before* `__nki_dont_trace__`: an `@nki` op is always traced even if something also marked it `dont_trace`. And the module-exclusion gate (gate 4) is guarded by `isfunction(func) or isbuiltin(func)` — a *callable object* that is neither a plain function nor a builtin (e.g. a class instance with `__call__`) skips the exclusion test entirely and falls through to `inline_function`. A reimplementation that exempts excluded modules unconditionally will mis-trace callable objects, and one that checks `dont_trace` first will fail to trace a `dont_trace`-tagged `@nki` op.

> **QUIRK — the return-consistency rule.** "A function with *no* nki-data inputs must not *return* nki-data" (`function_without_nki_data_as_inp…`, interned). The rationale: a traced helper that takes only Python scalars but conjures an IR tensor out of nowhere is almost always a bug (a tensor allocated against the wrong scope, or an `nl.*` op called where a pure computation was intended). The check is a one-directional taint guard: nki-data may only *flow through* a traced helper, never *originate* in one that received none.

### Function Map

| Function | Offset | Role | Confidence |
|---|---|---|---|
| `TraceContext.call` | `0x2b380` | the trace-vs-concrete dispatcher | CERTAIN |
| `TraceContext.call.<locals>.genexpr` | `0x16570` (generator body) | `is_nki_data(a) for a in args` | CERTAIN |
| `TraceContext.call.<locals>.has_nki_data` | `0x1af80` / `0x1b770` | `any(is_nki_data(a) for a in args)` | CERTAIN |
| `inline_function` | (`call`-internal) | trace a user function body under a new `FunctionScope` | HIGH |

---

## Static Control-Flow Evaluation

### Purpose

NKI source uses ordinary Python `if`/`while`. During tracing these are evaluated *concretely* — the trace takes the live branch and replicates the body, exactly as a partial evaluator would. There is no IR node for "Python if." The only constraint is that the *condition* must be a real Python `bool`: a condition that depends on device data (a tensor element, a launch-grid index comparison) is not knowable at trace time, so it is rejected outright. This is the single most surprising property of the model for a reimplementer coming from a framework with `tf.cond`/`lax.cond`.

### Algorithm

```c
// TraceContext.eval_if_cnd(self, cnd)
//   __pyx_pw_…12TraceContext_26eval_if_cnd @ 0x28070
//   string-table symbols (verified): self, cnd, nki_tensor, ScalarPredicate,
//     if_cnd_generator, err_dynamic_control_flow_not_sup, sema
bool eval_if_cnd(self, cnd):
    // A symbolic condition cannot be resolved at trace time:
    if isinstance(cnd, nki_tensor)
            || isinstance(cnd, ScalarPredicate):       // data-dependent
        raise err_dynamic_control_flow_not_supported(...);
        // interned: "err_dynamic_control_flow_not_supported"
        // related diagnostic: "Condition should be bool"

    // Otherwise it is a concrete Python value -> drive the branch selection now:
    return bool(cnd);            // feeds if_cnd_generator (the taken-branch trace)
```

```text
   Python `if`/`while` in a kernel body
                 │
                 ▼
        TraceContext.eval_if_cnd(cnd)
                 │
   ┌─────────────┴──────────────┐
   │ cnd is nki_tensor /         │ cnd is a concrete bool
   │ ScalarPredicate (symbolic)  │
   ▼                             ▼
 raise                    bool(cnd) -> if_cnd_generator
 err_dynamic_control_     selects the taken branch;
 flow_not_supported       that branch's body is TRACED
                          (the other branch is dropped)
```

`if_scope` (`0x175f0`, routes through `.predicates`), `while_scope` (`0x17c20`, takes a `cnd`), and the generator `if_cnd_generator` are the surrounding scaffolding. The semantic outcome:

- A `while` loop whose condition is concrete is **unrolled** by the trace (each concrete iteration emits its body once), exactly like `static_range` — see [range semantics](range-semantics.md).
- A data-dependent loop or branch must instead be expressed with dedicated tensor/predicate ops (masking, `select`, predicated stores), not Python control flow.

> **CORRECTION (W11-CF) —** the backing report names the rejection symbol `err_dynamic_control_flow_not_sup`. That is the truncated Cython `__pyx_n_s_` identifier; the *full interned string* in the module string table is `err_dynamic_control_flow_not_supported`. Cite the complete name when reasoning about the error catalog.

> **GOTCHA —** the rejection key is **the type of the condition value**, not whether the variable "looks dynamic." A [`DynamicScalar`](type-system.md) comparison such as `nl.program_id(0) < 4` does not return a Python `bool`; `__lt__` returns a `ScalarPredicate` (see the type-system page). So `if nl.program_id(0) < 4:` is rejected *because the comparison already produced a `ScalarPredicate`*, which `eval_if_cnd` then refuses. A reimplementation that overloads comparison to return `bool` would silently admit broken kernels.

### Function Map

| Function | Offset | Role | Confidence |
|---|---|---|---|
| `TraceContext.eval_if_cnd` | `0x28070` | reject symbolic / evaluate concrete condition | CERTAIN |
| `TraceContext.if_scope` | `0x175f0` | enter a static-if scope (via `.predicates`) | HIGH |
| `TraceContext.while_scope` | `0x17c20` | enter a static-while scope | HIGH |
| `TraceContext.if_cnd_generator` | (generator) | yield the taken-branch body trace | HIGH |

---

## Scopes and the Singleton Lifecycle

### Purpose

A trace is stateful: it needs to know which kernel, which function, and which statement it is currently emitting into, so that tensors allocate against the right scope and nested-function returns wire back to the right caller. `TraceContext` holds this as a chain of scope objects rooted at one `KernelScope`, and the whole context is reached through the process-singleton `global_ctx`. The singleton is what lets a deeply nested `nl.*` intrinsic call reach the active builder without threading the context through every signature.

### `__init__` — the context state

```c
// TraceContext.__init__(self, opts)     @ 0x18980 (n_s setattro list)
void __init__(self, opts):
    self.opts                 = opts;              // CompileOpts
    self.type_system          = NkiTypeSystem(...);
    self.cur_scope            = <KernelScope/None>; // THE ambient emission scope
    self.kernel_scope         = <KernelScope>;      // top-level kernel scope
    self.function_scope       = <FunctionScope>;    // current function scope
    self.cur_api              = None;               // which @nki op is executing
    self.has_collectives      = False;              // set when a CC op is traced
    self.in_exception_block   = False;              // exception-handling guard
    self.multi_physical_cores = <bool>;             // grid spans >1 NeuronCore (LNC)
```

`cur_scope` is the cell that [`get_cur_scope`](nki-ctx.md) returns; `cur_api` is set/restored by the `nki_api_scope` context manager (`@contextmanager` generator @ `0x15fc0`) so diagnostics can attribute an error to the NKI op currently in flight. `multi_physical_cores` is set from [`grid_has_multi_physical_cores`](spmd-dimensions.md) — it records whether this is a Logical-NeuronCore (LNC) multi-core launch.

### The singleton

```c
// TraceContext.new_ctx(cls, opts)       @ 0x24340  (CLASSMETHOD)
classmethod new_ctx(cls, opts):
    ctx = cls(opts);                  // a fresh TraceContext
    TraceContext.global_ctx = ctx;    // install the process singleton
    return ctx;
```

```text
new_ctx(opts) ──installs──▶ TraceContext.global_ctx ◀── nki_ctx() reads it
                                     │
                              ┌──────┴────────┐
                              ▼               ▼
                          .sema           .cur_scope
                       (IR builder)   (active KernelScope/
                                       FunctionScope/StmtScope)
   ── trace_kernel(name, frame) walks the body under a KernelScope
   ── finalize_kernel / finalize tear down, clearing global_ctx -> None
   ── post-trace nl.* ops then raise err_nki_api_outside_of_nki_kernel
```

`trace_kernel` (`0x1c630`) uses `global_ctx` and delegates the statement-by-statement walk to `trace_kernel_impl` (`0x23910`, a generator). The scope helpers mutate the chain: `new_function_scope(name, stack_frame)` (`0x1d780`) pushes a `FunctionScope`; `cur_function_scope` (`0x1f450`) reads it; `finalize` (`0x1d240`) and `finalize_kernel` (`0x16fb0`) tear down. After teardown, `global_ctx` is `None`, so any stray `nl.*` op correctly raises `err_nki_api_outside_of_nki_kernel` — the same string the [`nki_ctx`](nki-ctx.md) guard checks.

> **NOTE —** `global_ctx` is a *single mutable class attribute*, not thread-local on `TraceContext` itself. The thread-locality lives in the [`nki_ctx`](nki-ctx.md) accessor layer (whose docstring is explicitly "the (thread local) global context of IR construction during NKI tracing"). One trace is in flight per interpreter at a time; the report notes a `main_interpreter_id` static guard enforcing single-interpreter loading (INFERRED — the guard's exact mechanism was not individually traced).

### Return handling

```c
// TraceContext.check_return(self, return_value)              @ 0x1b780
//   -> transfer_return_value_to_caller(return_value)
// TraceContext.transfer_return_value_to_caller(self, ...)    @ 0x19740
//   symbols: cur_function_scope, KernelScope, KernelTensor, DataTile,
//            add_kernel_return_values, return_from_function, kernel_return, opts
void transfer_return_value_to_caller(self, rv):
    if self.cur_function_scope() is the top KernelScope:
        // a top-level kernel may NOT return a value to Python:
        raise <"Returning value from top level nki kernel is not supported">;
        // (KernelTensor results are registered via add_kernel_return_values /
        //  kernel_return as kernel OUTPUTS, not Python return values)
    else:
        return_from_function(rv);   // hoist KernelTensor/DataTile up to caller scope
```

> **QUIRK —** a top-level NKI kernel cannot `return` a value (`"Returning value from top level nki kernel is not supported"`, interned). Kernel results are *outputs*, registered through `add_kernel_return_values`/`kernel_return`, not Python return values. A nested traced helper *may* return a `KernelTensor`/`DataTile`; `return_from_function` hoists it into the caller's scope. The asymmetry is the IR boundary: the outermost frame's results are device outputs with a fixed ABI; an inner frame's results are just IR values flowing up.

### Object shadowing

`warn_object_shadowing` (`0x29180`) tracks variable rebinding across scopes (`is_out_of_scope` / `is_the_same_scope` / `parent_scope` / `f_locals`). When a name rebinds a tensor of a different dtype/shape, it emits a `SyntaxWarning` assembled from the interned fragments `"shadowing "`, `" with a new object, use '"`, and `"[...] =' if you want to update the existing object"` — steering the user toward in-place tile update (`x[...] = ...`) instead of accidentally allocating a new tile. `annotate_object` (`0x24ad0`) validates a Python type annotation's shape against the object (mismatch → `err_annotation_shape_mismatch`) and then routes through the same shadowing check.

### Function Map

| Function | Offset | Role | Confidence |
|---|---|---|---|
| `TraceContext.__init__` | `0x18980` | initialize the trace-context state | CERTAIN |
| `TraceContext.new_ctx` | `0x24340` | classmethod: construct + install `global_ctx` | CERTAIN |
| `TraceContext.trace_kernel` | `0x1c630` | drive the kernel-body walk via `global_ctx` | HIGH |
| `TraceContext.trace_kernel_impl` | `0x23910` | generator: statement-by-statement trace | HIGH |
| `TraceContext.nki_api_scope` | `0x15fc0` | `@contextmanager` set/restore `cur_api` | CERTAIN |
| `TraceContext.new_function_scope` | `0x1d780` | push a `FunctionScope` | CERTAIN |
| `TraceContext.cur_function_scope` | `0x1f450` | read the active `FunctionScope` | CERTAIN |
| `TraceContext.finalize` | `0x1d240` | tear down `cur_scope` | CERTAIN |
| `TraceContext.finalize_kernel` | `0x16fb0` | tear down `kernel_scope` | CERTAIN |
| `TraceContext.check_return` | `0x1b780` | route a return through `transfer_…` | CERTAIN |
| `TraceContext.transfer_return_value_to_caller` | `0x19740` | wire nested return / reject top-level return | CERTAIN |
| `TraceContext.warn_object_shadowing` | `0x29180` | `SyntaxWarning` on dtype/shape rebind | CERTAIN |
| `TraceContext.annotate_object` | `0x24ad0` | annotation shape check + shadowing | CERTAIN |
| `TraceContext.annotate_iter` | `0x1e0f0` | annotate a loop-iterator value | HIGH |
| `TraceContext.builtin_range` | `0x1c020` | traced replacement for builtin `range` | HIGH |
| `TraceContext.no_active_exception` | `0x1a5d0` | `sys.exc_info()[0] is None` | CERTAIN |

---

## Adversarial Self-Verification

The five strongest claims on this page, each re-challenged against the binary:

1. **The `.call` dispatch order is `nki_func` → `__nki_dont_trace__` → `TraceKernel` → exclude-modules → inline.** Re-checked: the `__pyx_n_s_`/`__pyx_n_u_` symbol stream in the `call` body file (`…12TraceContext_10call_…0x2b380`) appears *in that exact textual order*: `nki_func`, `sema`, `NKIFunc`, `dont_trace_attr`, `TraceKernel`, `inline_function`, `inspect`, `isfunction`, `isbuiltin`, `module`, `exclude_modules`, then `genexpr`/`is_nki_data`/`has_nki_data`/`function_without_nki_data_as_inp`. **CONFIRMED.**

2. **Data-dependent branches are rejected for being `nki_tensor`/`ScalarPredicate`.** Re-checked the `eval_if_cnd` body (`…26eval_if_cn_…0x28070`): symbols `cnd`, `nki_tensor`, `ScalarPredicate`, `if_cnd_generator`, `err_dynamic_control_flow_not_sup`, `sema` — confirming the type test and the raise. **CONFIRMED.** Issued an in-place CORRECTION on the truncated error name (full string `err_dynamic_control_flow_not_supported`, verified in the string table).

3. **`_exclude_modules` is exactly `(neuronxcc.nki, neuronxcc.starfish.penguin, numpy, math)`.** Re-checked the staticmethod body (`…1_exclude_modules_…0x193f0`): symbols `neuronxcc_nki`, `neuronxcc_starfish_penguin`, `numpy`, `math` — four entries, no more. **CONFIRMED.**

4. **`is_nki_data(v)` is `isinstance(v, (nki_tensor, mask))`.** Re-checked the body (`…3is_nki_data_…0x16be0`): symbols `v`, `nki_tensor`, `mask` only. **CONFIRMED.** (`mask` resolves to the `EQTileMask` family per the string table.)

5. **A top-level kernel cannot return a value.** Re-checked: the string `"Returning value from top level nki kernel is not supported"` is present in the module string table, alongside `add_kernel_return_values`/`return_from_function`/`kernel_return` referenced by `transfer_return_value_to_caller` (`0x19740`). **CONFIRMED.**

Items left at INFERRED and *not* upgraded: the `main_interpreter_id` single-interpreter guard mechanism (the static was not individually traced — it did not appear in the grepped string table for this module), and the precise internal of `inline_function` (it is a `call`-internal branch, not a separately exported method). Nothing on this page asserts a fabricated address, default, or field meaning.

---

## Related Components

| Name | Relationship |
|---|---|
| `nki_ctx` (`nki_ctx.so`) | the ambient accessor that reads `TraceContext.global_ctx`; how every `nl.*` op reaches `.sema`/`.cur_scope` |
| `iterators` (`iterators.so`) | `static_range` unrolls (like a concrete `while`), `affine_range`/`sequential_range` defer to a loop axis — the loop counterpart of `eval_if_cnd` |
| `programming_model` (`programming_model.so`) | `program_id`/`num_programs` produce the `ProgramId` scalars whose comparisons are `ScalarPredicate`s `eval_if_cnd` rejects |
| `scalars` (`scalars.so`) | `DynamicScalar`/`ProgramId`/`LoopVar` — the symbolic values `is_nki_data` and `eval_if_cnd` classify |
| `TraceKernel` (D-P20) | the kernel entry; `call` gate 3 recurses into nested `TraceKernel` instances |

## Cross-References

- [nki_ctx — the Ambient Trace-Scope Accessor](nki-ctx.md) — `get_cur_scope`/`nki_ctx` reading `global_ctx`; the `.sema`/`.cur_scope` an op fetches
- [Range Semantics](range-semantics.md) — `static_range` vs `affine_range`/`sequential_range`; the loop analogue of static-if evaluation
- [The NKI Type System](type-system.md) — `DynamicScalar`/`ScalarPredicate`/`nki_tensor`/`EQTileMask`: why comparisons return predicates, not `bool`
- [SPMD Dimension Model](spmd-dimensions.md) — `grid_has_multi_physical_cores` feeding `multi_physical_cores`; launch-grid → Penguin Axis mapping
- [NKI Architecture Overview](architecture-overview.md) — where the tracing JIT sits in the NKI front-end
