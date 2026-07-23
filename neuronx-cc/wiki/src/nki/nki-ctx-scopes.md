# nki_ctx, Scopes & the sema Handle

> *All addresses, line numbers, and interned identifiers on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, cp310 wheel (`nki_ctx.cpython-310-x86_64-linux-gnu.so`). cp311/cp312 ship byte-identical Cython logic at shifted addresses.*

## Abstract

Every NKI language op (`nl.add`, `nl.load`, `nl.program_id`, `nl.affine_range`, …) is a thin Python surface that, before it can emit a single IR node, must answer two questions: *which IR-builder am I emitting into?* and *which scope region do my instructions land in?* The whole answer is one tiny Cython module — `neuronxcc/nki/compiler/backends/neuron/nki_ctx.py` — compiled to a 197-line `.so` with exactly **three functions** and **no instance state of its own**. It is the ambient-accessor layer: it reads a process-global cell, validates that a kernel trace is actually in progress, and hands back the active codegen context.

The cell is `TraceContext.global_ctx`. The object it holds is the active **codegen context** — the `NeuronCodegen` instance from `KernelBuilder.so` that owns `.builder` (the pelican/BIR `IRBuilder` C++ binding) and the `.cur_scope` chain. `nki_ctx()` returns that object or raises; `get_cur_scope()` returns `nki_ctx().cur_scope`; `nki_ctx_or_none()` returns the raw cell unchecked. The scope chain itself is a stack of `@contextmanager` region emitters on `NeuronCodegen` — `kernel_scope`, `loop_scope`, `if_scope`, `while_scope`, `allocation_scope`, `stage_scope` — each opening a Penguin `ScopeRegion` of a tagged kind. The guard that fires when no trace is active, `err_nki_api_outside_of_nki_kernel`, does not live here at all; it is a function in the sibling `sema` module, imported by name.

This page documents the fetch chain bottom-up: the three accessors and their exact decompiled control flow; the `is-None` guard and the error path; the two distinct scope vocabularies (TraceContext's abstract-interp scope *classes* vs NeuronCodegen's IR region *kinds*) and how `cur_scope` bridges them; and the ScopeRegion-kind set every traced op selects into.

For reimplementation, the contract is:

- The **singleton cell**: `TraceContext.global_ctx`, set once per kernel trace, cleared on teardown; the only mutable state the accessors read.
- The **three accessors** and their exact guard semantics: `nki_ctx()` (None-check + raise), `nki_ctx_or_none()` (raw read), `get_cur_scope()` (truthy-check + `.cur_scope`).
- The **outside-kernel guard**: `sema.err_nki_api_outside_of_nki_kernel()`, fired on `global_ctx is None`.
- The **ScopeRegion-kind set** an op's `cur_scope` can be — `KernelScope / FunctionScope / LoopScope / IfScope / WhileScope / AllocationScope` (+ `ScopeRegion` generic) — and the `@contextmanager` emitters that push them.

| | |
|---|---|
| **Module** | `neuronxcc/nki/compiler/backends/neuron/nki_ctx.py` → `nki_ctx.cpython-310-*.so` (197 lines) |
| **Public functions** | `nki_ctx` (`0x7700`, py:14), `nki_ctx_or_none` (`0x7440`, py:23), `get_cur_scope` (`0x6f60`, py:29) |
| **Singleton cell** | `TraceContext.global_ctx` (class attr of `TraceContext`) |
| **Returned object** | the active `NeuronCodegen` codegen context (`KernelBuilder.so`) — owns `.builder`, `.cur_scope` |
| **Outside-kernel guard** | `neuronxcc.nki.compiler.backends.neuron.sema.err_nki_api_outside_of_nki_kernel` |
| **Guard message** | `"calling NKI API outside of NKI kernels is not supported."` |
| **Interned identifiers (whole module)** | `TraceContext, global_ctx, cur_scope, sema, err_nki_api_outside_of_nki_kerne[l], nki_ctx, nki_ctx_or_none, get_cur_scope` |

---

## The Singleton Cell — `TraceContext.global_ctx`

### Purpose

The entire ambient-context mechanism is one class attribute: `TraceContext.global_ctx`. It is `None` between kernel traces and holds the active codegen context during a trace. `TraceContext.new_ctx(opts)` (`TraceContext.so`, classmethod) constructs the context and installs it; trace teardown clears it back to `None`. The accessors in `nki_ctx.py` never own a context — they only read this cell.

The object stored in that cell is **not** a `TraceContext` instance, despite the attribute living on `TraceContext`. `TraceContext.so` interns `cur_scope`, `global_ctx`, `new_ctx`, and `nki_func`, but neither `sema` nor `builder` as instance attributes. What `nki_ctx()` fetches — and what callers drive with `.affine_range(...)` / `.if_scope(...)` / `.cur_scope` — is the `NeuronCodegen` codegen context from `KernelBuilder.so`, which does intern `builder`, `cur_scope`, and `affine_range`.

> **GOTCHA — `sema` is a module, not a context attribute.** `nki_ctx()` guards with a single `is None` test on `global_ctx`; there is no `ctx.sema` check anywhere ([§Algorithm](#algorithm)). The `sema` that appears in the disassembly is the imported *module* off which the error helper is fetched, so reading it as a field of the context leads to a guard condition that does not exist.

### Considerations

`global_ctx` is a single mutable cell, so NKI tracing is single-active-trace per interpreter — there is no per-thread context stack at this layer despite the module docstring's "(thread local) global context" wording (the thread-locality is enforced upstream by `TraceContext`'s `main_interpreter_id` guard, not by `nki_ctx.py`). Nested traced functions reuse the *same* `global_ctx`; what changes per nested call is `cur_scope` (a new `FunctionScope` pushed on the chain), not the cell.

---

## `nki_ctx()` — the guarded accessor

### Purpose

The function nearly every NKI intrinsic calls first. Returns the active codegen context, or raises if no kernel trace is in progress. It is a `METH_NOARGS` wrapper — **it takes no arguments**.

> **GOTCHA — there is no `allow_none` keyword.** The raise-vs-return-`None` choice looks like it should be a parameter, but the wrapper at `0x7700` is `METH_NOARGS` (`PyObject *__pyx_self, PyObject *unused`) and no `allow_none` identifier exists in the string or name tables. The non-raising behaviour is a *separate* function, `nki_ctx_or_none()`.

### Entry Point

```text
nl.<op>(...)                          ── any W01/W02 NKI intrinsic
  └─ nki_ctx()                        ── __pyx_pw_..._nki_ctx_1nki_ctx @0x7700
       ├─ from .TraceContext import TraceContext     (ImportModuleLevelObject + ImportFrom)
       ├─ GetAttr(TraceContext, "global_ctx")        (tp_getattro, py:17)
       └─ if None → sema.err_nki_api_outside_of_nki_kernel()   (py:19)
```

### Algorithm

```c
// __pyx_pw_..._nki_ctx_1nki_ctx @0x7700   src nki_ctx.py:14
PyObject *nki_ctx(void):
    TraceContext = ImportFrom(import ".TraceContext", "TraceContext")   // py:15
    ctx = getattr(TraceContext, "global_ctx")        // py:17  (tp_getattro, n_s global_ctx)
    if ctx != Py_None:
        return ctx                                   // common path: trace in progress
    // ctx is None → not inside a traced kernel:
    err = GetBuiltinName("sema").err_nki_api_outside_of_nki_kernel   // py:19
    return err()                                     // raises; never returns a value
```

The decompiled body makes the None-branch unambiguous: after `getattr(ctx, "global_ctx")` it tests `if ( Attr != &Py_NoneStruct ) goto LABEL_18` (return `ctx`). Only when `Attr == Py_None` does it resolve `sema` (via `_Pyx_GetBuiltinName(__pyx_n_s_sema)`), fetch its `err_nki_api_outside_of_nki_kerne[l]` attribute through the tp_call slot at `+144`, and call it with the empty tuple. The error helper unconditionally raises, so `nki_ctx()` has exactly two outcomes: **return the live context**, or **raise** `"calling NKI API outside of NKI kernels is not supported."`.

> **NOTE —** `sema` is resolved as a *builtin/module global name*, not as `ctx.sema`. nki_ctx.py does `from neuronxcc.nki.compiler.backends.neuron import sema` (and `from .TraceContext import TraceContext`); the import-from for `sema` appears in `__pyx_pymod_exec_nki_ctx`. The error helper is `sema.err_nki_api_outside_of_nki_kernel`, confirmed present in `sema.cpython-310-*.so` with the message string. This is why a stale/None `global_ctx` after trace teardown still produces a clean, attributable error rather than an `AttributeError`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `__pyx_pw_..._nki_ctx_1nki_ctx` | `0x7700` | guarded accessor; `METH_NOARGS` | CERTAIN |
| `sema.err_nki_api_outside_of_nki_kernel` | (in `sema.so`) | raises the outside-kernel error | CERTAIN |

---

## `nki_ctx_or_none()` — the soft accessor

### Purpose

Returns `TraceContext.global_ctx` raw — `None` if no trace is active, the context otherwise. No guard, no error. This is the accessor for code paths that legitimately run both inside and outside a trace (e.g. helpers that branch on "am I tracing right now?").

### Algorithm

```c
// __pyx_pw_..._nki_ctx_3nki_ctx_or_none @0x7440   src nki_ctx.py:23
PyObject *nki_ctx_or_none(void):
    TraceContext = ImportFrom(import ".TraceContext", "TraceContext")   // py:24
    return getattr(TraceContext, "global_ctx")       // py:26  — may be None, no check
```

The decompiled body is a strict prefix of `nki_ctx()` minus the None-branch: import `TraceContext`, `getattr(..., "global_ctx")`, return it. There is no `Py_None` comparison and no `sema` resolution — the function has a single return site for the attribute. This is the "allow none" behaviour in full: a separate function, not a keyword.

---

## `get_cur_scope()` — the scope accessor

### Purpose

The function the op emitters call to learn *which region to emit into*. Returns `nki_ctx().cur_scope` — the active scope on the codegen context's scope chain. Because it routes through `nki_ctx()` (not `nki_ctx_or_none()`), calling it outside a kernel raises the same outside-kernel error.

### Algorithm

```c
// __pyx_pw_..._nki_ctx_5get_cur_scope @0x6f60   src nki_ctx.py:29
PyObject *get_cur_scope(void):
    f = GetBuiltinName("nki_ctx")                    // py:30  module-global nki_ctx
    ctx = f()                                        // calls nki_ctx() → raises if outside
    if PyObject_IsTrue(ctx):                          // py:31  truthy guard before getattr
        return getattr(ctx, "cur_scope")             // py:31  (n_s cur_scope)
    return ctx                                        // ctx falsy → return it as-is
```

The body resolves the module global `nki_ctx` (not a direct internal call), invokes it, then runs `PyObject_IsTrue` on the result. For a real context this is true and it does `getattr(ctx, "cur_scope")`; the `False`/`None`/`True` fast-paths short-circuit the truthiness check. In practice `nki_ctx()` either raises or returns a truthy context object, so the falsy return is a defensive tail.

> **QUIRK —** `get_cur_scope()` calls `nki_ctx` *by name through the module dict* (`_Pyx_GetBuiltinName(__pyx_n_s_nki_ctx)`), not as a private C call. A reimplementation that inlines the context fetch would diverge if user code monkey-patched the module-level `nki_ctx`; the binary preserves the indirection, so the public `nki_ctx` is the single choke-point even for the in-module caller.

---

## The Scope Chain — what `cur_scope` points at

`cur_scope` is the active node of a scope stack, but **two different objects wear the name "scope"** in this subsystem, and the page that conflates them will mis-model the IR. They live in different binaries and serve different layers.

### TraceContext scope *classes* (abstract-interpretation side)

`TraceContext.so` defines the Python-level scope objects that the abstract interpreter walks the kernel body under. Confirmed interned class names: **`KernelScope`, `FunctionScope`, `StmtScope`**. These track the *Python* nesting (which kernel, which traced function, which statement) and carry `parent_scope`, `name_and_loc`, `stack_frame`. `cur_scope` on the context is set to one of these as the interpreter enters/leaves functions and statements.

### NeuronCodegen scope *region kinds* (IR-builder side)

`KernelBuilder.so`'s `NeuronCodegen` is the codegen context — the object `nki_ctx()` actually returns. Its scopes are `@contextmanager` *emitters* that open a Penguin **`ScopeRegion`** of a tagged **kind**. Confirmed kind strings in `.rodata`:

| Region kind | Opened by (`NeuronCodegen` method) | Meaning | Confidence |
|---|---|---|---|
| `KernelScope` | `kernel_scope` / `spmd_kernel_scope` | top-level NeuronCore (or SPMD-grid) kernel region | CERTAIN |
| `FunctionScope` | `new_function_scope` | a traced nested-function region | CERTAIN |
| `LoopScope` | `loop_scope` | body of an affine/sequential loop axis | CERTAIN |
| `IfScope` | `if_scope` | predicated `if` region (entry guard = lowered predicate) | CERTAIN |
| `WhileScope` | `while_scope` | `while`-loop region (yields `CompileTimeWhileContext`) | CERTAIN |
| `AllocationScope` | `allocation_scope` / `allocation_region_scope` | SBUF/PSUM tensor-lifetime boundary | CERTAIN |
| `ScopeRegion` (generic) | `region_scope` / `stage_scope` / `no_reorder` | plain region (e.g. no-reorder barrier, pipeline-stage container) | CERTAIN |

The axis kinds `AffineAxis` / `DynamicAxis` are *not* scope kinds — they tag the `LoopAxis` node a `LoopScope` is opened around. The task brief's "loop / if / while / kernel / allocation / stage" kinds correspond to `loop_scope`/`if_scope`/`while_scope`/`kernel_scope`/`allocation_scope`/`stage_scope` here; `stage_scope` opens a generic `ScopeRegion` (the pipeline-stage *container*), with the per-stage marker carried by `pipeline_stage(stage_id, execution_order)`.

> **NOTE —** there is **no `StmtScope` region kind** in `NeuronCodegen`. `StmtScope` exists only as a TraceContext class (statement-level abstract-interp bookkeeping); it does not produce an IR `ScopeRegion`. A reimplementer mapping `cur_scope` → IR region must skip `StmtScope` nodes — only `Kernel/Function/Loop/If/While/Allocation` scopes back a Penguin region.

### Algorithm — `new_scope`, the generic region opener

```c
// NeuronCodegen.new_scope  (KernelBuilder.so) — @contextmanager generator
contextmanager new_scope(self, kind, **payload):
    block = self.create_scope_region_block(self.curstmt, parent, kind, attrs)  // builder.makeScopeRegion
    prev = self.cur_scope
    self.cur_scope = block            // push: ops emitted now land in `block`
    try:
        yield block
    finally:
        self.cur_scope = prev         // pop on __exit__
```

Each named scope (`if_scope`, `loop_scope`, `allocation_scope`, …) is `create_*_block` (the node factory) + `new_scope(kind=…)` (the push/pop). `cur_scope` is the mutated cell: it is what `get_cur_scope()` reads, so an op emitted inside a `with self.if_scope(pred):` sees the `IfScope` block as its `cur_scope` and the op is inserted `in_cur_scope`. (Region-construction internals — `create_scope_region_block`, `create_affine_axis_block`, `create_while_block` — are documented in [6.5.5 scope emitters](neuroncodegen-control.md); predicate lowering for `if_scope`/`while_scope` in [6.4.4 sema legality engine](sema-legality.md).)

---

## End-to-End — how one traced op reaches its builder and scope

```text
nl.add(a, b)                                   (W01/W02 intrinsic surface)
 ├─ ctx   = nki_ctx()                           @0x7700  → active NeuronCodegen (or raise)
 ├─ scope = get_cur_scope()  (= nki_ctx().cur_scope)  @0x6f60  → active ScopeRegion
 ├─ builder = ctx.builder                        (pelican/BIR IRBuilder binding)
 └─ builder.<emit>(... , scope=scope)            → appends a Penguin IR node into `scope`
```

The single mutable cell is `TraceContext.global_ctx`, installed by `TraceContext.new_ctx(opts)` at trace start and cleared on `finalize_kernel`. Between traces it is `None`, so a stray `nl.*` call resolves `nki_ctx()` → `global_ctx is None` → `sema.err_nki_api_outside_of_nki_kernel()` → `"calling NKI API outside of NKI kernels is not supported."`. The `cur_scope` the op selects into is whatever region is on top of the `NeuronCodegen` scope stack at emit time — the kernel region by default, narrowed by every active `with loop_scope()/if_scope()/allocation_scope()` the trace has entered.

---

## Evidence summary

- **Exactly three public functions.** The `nki_ctx.so` `__pyx_pw_*` set is `{nki_ctx@0x7700, nki_ctx_or_none@0x7440, get_cur_scope@0x6f60}`; there are zero `__pyx_gb_*` symbols, so no generator, and the name table contains no `allow_none`.
- **The guard tests `global_ctx is None`.** Decompiled `nki_ctx` does `getattr(ctx,"global_ctx")` then `if (Attr != &Py_NoneStruct) goto return`; only the `None` branch resolves `sema`.
- **The error helper lives in `sema`.** `sema` is `_Pyx_GetBuiltinName`-resolved, and `err_nki_api_outside_of_nki_kernel` is present in `sema.cpython-310-*.so` carrying `"calling NKI API outside of NKI kernels is not supported."`.
- **`nki_ctx()` returns the `NeuronCodegen` codegen context.** The iterators trampoline does `nki_ctx().affine_range(...)`, and `NeuronCodegen` (`KernelBuilder.so`) interns `affine_range`, `cur_scope`, and `builder`.
- **The ScopeRegion kind set** is Kernel / Function / Loop / If / While / Allocation plus the generic `ScopeRegion`: `KernelBuilder.so` `.rodata` carries all seven names, while `StmtScope` appears only in `TraceContext.so` and is therefore not a region.

## Limits of this reading

Whether `global_ctx` is literally typed `NeuronCodegen`, or a thin trace-context wrapper that forwards to it, is **[UNRESOLVED]** — the type is not byte-pinned. What is pinned is the surface: `.affine_range`, `.cur_scope`, and `.builder` all resolve on `NeuronCodegen`.

---

## Cross-References

- [TraceContext](trace-context.md) — 6.1.1, the abstract-interpreter that owns `global_ctx`, `new_ctx`, and the scope *classes* (`KernelScope`/`FunctionScope`/`StmtScope`)
- [NeuronCodegen Control / Scope / Predicate Emitters](neuroncodegen-control.md) — 6.5.5, `NeuronCodegen` `@contextmanager` region emitters and `create_*_block` node factories
- [sema legality engine](sema-legality.md) — 6.4.4, the `sema` module that hosts `err_nki_api_outside_of_nki_kernel` and predicate lowering for `if_scope`/`while_scope`
