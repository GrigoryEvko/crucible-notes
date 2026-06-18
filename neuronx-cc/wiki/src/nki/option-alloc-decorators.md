# NKI Compiler-Option & Allocation Decorators

> *All symbols, offsets, and strings on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, the `cp310` wheel (`neuronxcc/nki/compiler/backends/neuron/{decorators,allocator,CompileOpts}.cpython-310-x86_64-linux-gnu.so`). The `cp311`/`cp312` wheels ship an identical surface; only the Cython mangling hashes differ. Provenance reports: **D-P21** (decorators), **D-W12** (framework-binding surface). Re-verified directly against the shipped `.so` string/symbol tables and disassembly.*

## Abstract

A NKI kernel is *configured* by two disjoint families of decorators, and this page documents both. The first family — six **compiler-option decorators** in `decorators.so` (`update_compile_opts`, `force_auto_alloc`, `skip_middle_end_transformations`, `override_platform_target_for_test`, `update_allocator`, `enable_stack_allocator`) — composes *on top of* an already-`@nki.jit`-wrapped `Kernel` object and mutates its immutable `CompileOpts` dataclass. The second family — the **PSUM/SBUF allocation decorators** in `allocator.so` (`nki.compiler.psum`/`nki.compiler.sbuf` × `{alloc, mod_alloc, auto_alloc}`) — is assigned to a tensor's `buffer=` field and decides the physical address of each tile, guarded by the `verify_allocation` ISA-constraint validator.

The two families meet at one field. A user-space allocator (selected by `enable_stack_allocator`) is only legal once `skip_middle_end_transformations` has fired, because the backend's coloring allocator — the thing being replaced — lives inside the middle end. So the option decorators are not independent knobs; they form a small dependency lattice enforced by three runtime guard clauses in `update_allocator`. The whole mechanism is **purely functional**: every decorator clones the `Kernel` via `copy_kernel(opts=dataclasses.replace(...))` and returns a *new* `Kernel`; the original is never mutated.

> **CORRECTION (vs the task title and earlier drafts) —** `@nki.jit` is **not** defined in `decorators.so`. It lives in `nki/compile.so` (D-W12 §1.1) and merely *produces* the `Kernel` instance these six decorators operate on. Inside `decorators.so` the literal `@nki.jit` appears **only** in the `enable_stack_allocator` docstring as a stacking example — confirmed: it is the single `@nki.jit` string in the module. The `@baremetal`/`@benchmark`/`@profile`/`@simulate` *mode* decorators are likewise elsewhere (`nki/__init__.so`, `nki/compile.so`; D-W12 §1).

### What a reimplementer must reproduce

- The **`CompileOpts` field map**: which decorator writes which dataclass field, with what value, and where that field is read downstream.
- The **`update_compile_opts` primitive** — `isinstance(Kernel)` assert → `copy_kernel(opts=replace_fields(func.opts, **kwargs))` — and the four thin decorators as `functools.partial` of it (or of `update_allocator`).
- The **three guard clauses** in `update_allocator` (one-allocator, not-`force_auto_alloc`, requires-skip-middle-end) and the symmetric guard in `force_auto_alloc`.
- The **`verify_allocation` ISA constraints**: the PSUM `fdim_size ≤ 2 KiB`/bank cap, the three SBUF `start_partition` partition tiers, the `[0, 192 KiB−16 KiB)` SBUF `byte_addr` range, and the `mod_alloc` tile-count rules.
- The **dual `@deco`/`@deco()` form** shared by all four thin decorators.

### At a glance

| | |
|---|---|
| **Option module** | `neuronxcc/nki/compiler/backends/neuron/decorators.py` → `decorators.cpython-310-…so` (410,728 B) |
| **Alloc module** | `neuronxcc/nki/compiler/backends/neuron/allocator.py` → `allocator.cpython-310-…so` (1,471,808 B) |
| **Opts dataclass** | `neuronxcc/nki/compiler/backends/neuron/CompileOpts.py` → `CompileOpts.cpython-310-…so` (335,408 B) |
| **Config object** | `Kernel.opts : CompileOpts` (a `@dataclass`; mutated via `dataclasses.replace`) |
| **Option decorators** | `update_compile_opts`, `force_auto_alloc`, `skip_middle_end_transformations`, `override_platform_target_for_test`, `update_allocator`, `enable_stack_allocator` |
| **Public re-exports** | `nki.compiler.{force_auto_alloc, enable_stack_allocator, skip_middle_end_transformations, allocation_scope, multi_buffer, no_reorder, psum, sbuf}` (`compiler/__init__.so`) |
| **Alloc decorators** | `nki.compiler.{psum,sbuf}.{alloc, mod_alloc, auto_alloc}` (`PSUMAllocation`/`SBUFAllocation`) |
| **ISA validator** | `AllocFunc.verify_allocation` @ `0x265c0`; `PSUMAllocFuncBase.verify_allocation` @ `0x38940`; `SBUFAllocFuncBase.verify_allocation` @ `0x346a0` |
| **Module docstring** | *"Define the decorators to modify the compile options of nki kernel"* (verbatim) |

---

## 1. The configuration object: `Kernel.opts`

Everything the option decorators do is mediated by one object: the kernel's `opts`, an instance of the `CompileOpts` `@dataclass`. The `Kernel` class is imported from `TraceKernel` (D-W12 §2.0); the option decorators import it purely so they can `isinstance`-check their argument and call its `copy_kernel`. The import list at module-init (program order, from the `decorators.so` import table) is:

```python
import logging                                                      # feeds enable_stack_allocator log_level default only
from functools import partial                                       # thin decorators = partial(...)
from neuronxcc.nki.compiler.backends.neuron.CompileOpts import AllocatorType
from neuronxcc.starfish.penguin.ir import OptLevel                  # backend opt-level enum
from neuronxcc.nki.compiler.backends.neuron.TraceKernel import Kernel
from dataclasses import replace as replace_fields                   # the immutable-update primitive
```

The two `backends.neuron.<Cls>` strings present in the binary are exactly `CompileOpts` and `TraceKernel` — confirming the resolution above (no other `backends.neuron.*` class string exists in `decorators.so`). [CONFIRMED — string table]

### 1.1 The fields the decorators touch

`CompileOpts.so`'s string table exposes the relevant dataclass fields. Two are subtly different and a reimplementer must keep them apart:

| Field | Type | Written by | Read by |
|---|---|---|---|
| `allocator_type` | `Optional[AllocatorType]` | `update_allocator` / `enable_stack_allocator` | backend allocator selector |
| `allocator_class` | property (derived) | — (read-only guard) | `force_auto_alloc` mutual-exclusion guard |
| `skip_allocators` | enum/flag | `update_allocator` / `enable_stack_allocator` | backend K-strand (skip coloring alloc) |
| `skip_middle_end` | enum/flag | `skip_middle_end_transformations` | Penguin middle-end driver (bypass legalize/fuse/layout) |
| `opt_level` | `OptLevel` | (set indirectly via `skip_middle_end`) | `update_allocator` hard gate |
| `platform_target` | target enum / str | `override_platform_target_for_test` | target resolution (cross-ref `_get_platform_target`) |
| `force_auto_alloc` | bool | `force_auto_alloc` | backend auto-allocator trigger |

[CONFIRMED — `CompileOpts.so` exposes `allocator_type`, `allocator_class` (with an `Optional[AllocatorType]` annotation and a `<locals>.strip_partial` helper), `force_auto_alloc`, `platform_target`, `opt_level`, plus the `AllocatorType` and `OptLevel` enum names.]

> **GOTCHA —** `allocator_type` and `allocator_class` are *both* real attributes. `update_allocator` reads/writes `allocator_type` (the enum); `force_auto_alloc` reads `allocator_class` (a derived property). They are kept consistent inside `CompileOpts`, but a reimplementation that conflates them will mis-fire the mutual-exclusion guards in §4–§5. The `AllocatorType` annotation is `Optional[...]`, so the unset state is `None`, which the §4 guard tests against (`is not None`).

---

## 2. `update_compile_opts(**kwargs)` — the primitive

Every option decorator ultimately funnels through this one factory. Outer wrapper `update_compile_opts` @ `0xd290`; inner closure `update_compile_opts.<locals>.decorating_function` @ `0xce00`. [CONFIRMED — symbols `__pyx_pw_…decorators_1update_compile_opts` @ `0xd290` and `…_19update_compile_opts_1decorating_function` @ `0xce00`.]

```python
def update_compile_opts(**kwargs):                          # decorators.py:13  (outer)
    def decorating_function(func_):                         # decorators.py:18  (inner closure)
        assert isinstance(func_, Kernel), \
            "Can only update compile options of nki kernel!"           # decorators.py:19
        return func_.copy_kernel(                                       # decorators.py:20-24
            opts=replace_fields(func_.opts, **kwargs))                  # dataclasses.replace
    return decorating_function
```

Decompiled mechanism, branch-by-branch:

1. The outer call packs `**kwargs` into `__pyx_scope_struct__update_compile_opts` (a Cython closure-capture struct, confirmed present) and returns the bound `decorating_function` CyFunction. [CONFIRMED]
2. `decorating_function(func_)` does `PyObject_IsInstance(func_, Kernel)`; on failure it raises `AssertionError("Can only update compile options of nki kernel!")` — the assert string is in the table. [CONFIRMED]
3. It fetches `func_.opts`, calls `dataclasses.replace(func_.opts, **kwargs)` (the `replace_fields` alias) to build a *new* `CompileOpts` with the requested fields overridden, then `PyDict_SetItem(d, "opts", <new opts>)` and `func_.copy_kernel(**d)` → a **new** `Kernel`. [CONFIRMED — `copy_kernel`, `opts`, `replace_fields` getattr/dict keys present.]

> **NOTE —** the update is immutable in two layers: `dataclasses.replace` builds a fresh `CompileOpts`, and `copy_kernel` builds a fresh `Kernel`. The caller's original `Kernel` is untouched. This is *why* decorator stacking is well-defined (§6): each layer sees the previous layer's `opts` and emits a new object. The `**kwargs` keys must be real `CompileOpts` field names — `dataclasses.replace` raises if you pass an unknown field, which is the only validation on `update_compile_opts` itself.

---

## 3. The thin decorators: `partial` over the primitive

Three of the six decorators are one-liners: a `functools.partial` of `update_compile_opts` with a single keyword pre-bound, wrapped in the **dual `@deco`/`@deco()`** idiom (`return deco if func is None else deco(func)`). All three accept `func=None`. [CONFIRMED — each has `func` as `pyargnames[0]` with default `&Py_NoneStruct`.]

### 3.1 `skip_middle_end_transformations(func=None)`

`skip_middle_end_transformations` @ `0xabe0`. [CONFIRMED — `__pyx_pw_…decorators_5skip_middle_end_transformations` @ `0xabe0`.] Docstring: *"Skip all middle end transformations on the kernel"*.

```python
def skip_middle_end_transformations(func=None):             # decorators.py:48
    deco = partial(update_compile_opts,
                   skip_middle_end=OptLevel.skip_middle_end) # decorators.py:50-51
    return deco if func is None else deco(func)              # decorators.py:52/56
```

Mechanism: `GetBuiltin(OptLevel)` → `getattr(OptLevel, "skip_middle_end")` → `partial(update_compile_opts, skip_middle_end=<that>)`; then the dual-form return. [CONFIRMED — `partial`, `update_compile_opts`, `OptLevel`, `skip_middle_end` refs + the `if func is Py_None` branch.]

**Effect:** sets `opts.skip_middle_end = OptLevel.skip_middle_end`. The Penguin middle-end driver reads this flag and **bypasses** the HLO→kernel legalize/fuse/layout passes — the kernel goes (near-)directly from NKI trace to the BIR backend. This is also a **hard prerequisite** for any user-space allocator (§4 guard 3).

### 3.2 `override_platform_target_for_test(func=None, platform_target=…)`

`override_platform_target_for_test` @ `0x9d80`. [CONFIRMED.] Docstring: *"Override platform target on the kernel"*.

```python
def override_platform_target_for_test(func=None, platform_target=<dflt>):  # decorators.py:59
    deco = partial(update_compile_opts, platform_target=platform_target)    # decorators.py:65-67
    return deco if func is None else deco(func)                             # decorators.py:69
```

Two arg names are parsed (`func`, `platform_target`); the body builds `partial(update_compile_opts, platform_target=<arg>)` and returns the dual form. [CONFIRMED — both arg names + `partial`/`update_compile_opts`/`platform_target` refs.]

**Effect:** sets `opts.platform_target = <arg>`, overriding the auto-detected hardware target (trn1/trn2/…/inf2). The `_for_test` suffix and its **absence from the public `nki.compiler` namespace** (§7) mark it as a test/debug hook, not a user-facing API. [The `platform_target` default is a runtime object in `__defaults__`, not a plain string in the `.so`; its concrete value (a default target enum or `None`) is INFERRED.]

### 3.3 `force_auto_alloc(func=None)`

Outer `force_auto_alloc` @ `0xe350`; inner `force_auto_alloc.<locals>.decorating_function` @ `0xe980`. Docstring: *"Force automatic allocation to be turned on in the kernel.\n  This will ignore any direct allocation inside the kernel"*. [CONFIRMED.]

> **CORRECTION (vs D-P21 §8) —** D-P21 tags the outer as `pw_3 @0xd9e0` and the inner as `pf_16`. The shipped `cp310` binary places the **outer** `force_auto_alloc` (`__pyx_pw_…decorators_3force_auto_alloc`) at `0xe350` and its **inner** `decorating_function` (`__pyx_pw_…decorators_16force_auto_alloc_1decorating_function`) at `0xe980`. The two-VA-frame artifact (nm body frame vs sidecar internal frame) explains the `0xd9e0` figure; the body-frame addresses above are the ones in this wheel's symbol table. The *structure* D-P21 describes is otherwise correct.

Unlike §3.1–3.2, this one has a real inner closure because it must *read* a field before delegating:

```python
def force_auto_alloc(func=None):                            # decorators.py:33
    def decorating_function(func_):                         # decorators.py:34
        assert isinstance(func_, Kernel), \
            "Can only update compile options of nki kernel!"           # decorators.py:35
        allocator_class = func_.opts.allocator_class                   # decorators.py:36
        if allocator_class is not None:                                # decorators.py:37
            raise RuntimeError(
                f"`@force_auto_alloc` is not compatible with "
                f"`Allocator '{allocator_class.name}'`!")               # decorators.py:38
        return update_compile_opts(force_auto_alloc=True)(func_)       # decorators.py:40
    return decorating_function if func is None else decorating_function(func)
```

Mechanism: `isinstance(Kernel)` assert → read `func_.opts.allocator_class` → if not `None`, raise the f-string `RuntimeError` (joined from the two halves `` "`@force_auto_alloc` is not compatible with `Allocator '" `` + `allocator_class.name` + `` "'`!" `` via `_Pyx_PyUnicode_Join`) → otherwise route through `update_compile_opts(force_auto_alloc=True)(func_)`. [CONFIRMED — both error-string halves + `allocator_class`/`name`/`update_compile_opts`/`force_auto_alloc` refs.]

**Effect:** sets `opts.force_auto_alloc = True`, telling the backend to run its **own** auto-allocator and **ignore any explicit in-kernel placement**. It is the symmetric opposite of `update_allocator`: this asserts *no* explicit allocator is set; `update_allocator` (§4 guard 2) asserts `force_auto_alloc` is *off*.

---

## 4. `update_allocator(allocator_type)` — the allocator factory

This is the generic "pick a user-space allocator" factory and the only option decorator with non-trivial validation. Outer `update_allocator` @ `0x9740`; inner `update_allocator.<locals>.decorating_function` @ `0xedf0`. [CONFIRMED — `__pyx_pw_…decorators_9update_allocator` @ `0x9740`, `…_16update_allocator_1decorating_function` @ `0xedf0`.] The outer packs `allocator_type` into `__pyx_scope_struct_1_update_allocator` (confirmed present) and returns the bound inner.

```python
def update_allocator(allocator_type):                       # decorators.py:72
    def decorating_function(func_):                         # decorators.py:73
        # GUARD 1 — one allocator only
        if func_.opts.allocator_type is not None \           # decorators.py:76
           and func_.opts.allocator_type != allocator_type:
            raise RuntimeError(
                f"Allocator already set on nki kernel: '{...}'")
        # GUARD 2 — mutually exclusive with @force_auto_alloc
        if func_.opts.force_auto_alloc:                      # decorators.py:79
            raise RuntimeError(
                f"Allocator '{allocator_type.name}' is not compatible "
                f"with `@force_auto_alloc`!")                # decorators.py:80-81
        # GUARD 3 — HARD GATE: must already be in skip-middle-end mode
        if func_.opts.opt_level != OptLevel.skip_middle_end: # decorators.py:82
            raise RuntimeError("Cannot set allocator for kernel "
                "without `@skip_middle_end_transformations`!")          # decorators.py:83
        assert isinstance(func_, Kernel), \
            "Can only update compile options of nki kernel!"            # decorators.py:85
        return func_.copy_kernel(opts=replace_fields(        # decorators.py:86-91
            func_.opts,
            allocator_type=allocator_type,
            skip_allocators=OptLevel.skip_allocators))
    return decorating_function
```

Each guard is verified in the disassembly:

- **Guard 1** — `func_.opts.allocator_type`, `PyObject_RichCompare` vs the captured `allocator_type`, error half `` "Allocator already set on nki kernel: '" ``. [CONFIRMED]
- **Guard 2** — `func_.opts.force_auto_alloc`, `PyObject_IsTrue`, error halves `` "Allocator '" `` + `` "' is not compatible with `@force_auto_alloc`!" `` (f-string joined over `allocator_type.name`). [CONFIRMED]
- **Guard 3** — `func_.opts.opt_level`, `getattr(OptLevel, "skip_middle_end")`, `RichCompare`; on mismatch `RuntimeError("Cannot set allocator for kernel without `@skip_middle_end_transformations`!")`. [CONFIRMED]
- **Commit** — `copy_kernel(opts=replace_fields(func_.opts, allocator_type=<captured>, skip_allocators=OptLevel.skip_allocators))` — two `PyDict_SetItem` keys `allocator_type` + `skip_allocators`. [CONFIRMED]

> **GOTCHA — Guard 3 reads `opt_level`, not `skip_middle_end`.** `skip_middle_end_transformations` *writes* the `skip_middle_end` field, but the gate here tests `opts.opt_level == OptLevel.skip_middle_end`. `OptLevel.skip_middle_end` is therefore both an *opt-level value* and the name the writer uses — the two stay coupled inside `CompileOpts`. A reimplementation must make `skip_middle_end_transformations` drive `opt_level` to `OptLevel.skip_middle_end`, or this guard never passes and no custom allocator can be installed. The ordering constraint follows: `@skip_middle_end_transformations` must be applied *below* (before, at runtime) any allocator decorator.

**Effect:** writes **two** fields — `opts.allocator_type = <chosen>` and `opts.skip_allocators = OptLevel.skip_allocators`. The second is the flag that tells the BIR backend's K-strand to **skip its own coloring allocator** for SBUF/PSUM and honor the addresses the user-space allocator produced. (See Part 8 §8.16–8.21 — `ColoringAllocatorWithLoop`, the SBUF/PSUM/DRAM allocators, and the `enable_stack_allocator` REG/LinearScan alternatives — and 6.7.1 for the nkilib `StackAllocator`.)

---

## 5. `enable_stack_allocator(func=None, log_level=…)`

The user-facing convenience name for `update_allocator(AllocatorType.stack)`. `enable_stack_allocator` @ `0xb7e0`. [CONFIRMED.]

```python
def enable_stack_allocator(func=None, log_level=<dflt>):    # decorators.py:93
    deco = update_allocator(AllocatorType.stack)            # decorators.py:110
    return deco if func is None else deco(func)             # decorators.py:114
```

Two arg names (`func`, `log_level`); the body does `getattr(AllocatorType, "stack")` → `update_allocator(AllocatorType.stack)` → dual-form return. [CONFIRMED — `update_allocator`, `AllocatorType`, `stack`, `log_level` refs + the `Py_None` branch.] The public stub default is `log_level=50` (D-W12 §4.1, i.e. `logging.WARNING`).

> **NOTE —** `log_level` is currently inert in the decorator *body* — it is parsed but not threaded into the `update_allocator` call. It exists to plumb verbosity into the stack allocator itself (the only reason `logging` is imported into this module). A reimplementation can accept and ignore it without behavioral difference at this layer.

Because it delegates straight to `update_allocator`, **all three guards of §4 apply** — in particular it raises `"Cannot set allocator for kernel without `@skip_middle_end_transformations`!"` unless skip-middle-end already fired. Its own docstring says exactly this:

```text
  Use stack allocator to allocate the psum and sbuf tensors in the kernel.
  Must use together with skip_middle_end_transformations.

  .. code-block:: python

    from neuronxcc import nki

    @nki.compiler.enable_stack_allocator
    @nki.compiler.skip_middle_end_transformations
    @nki.jit
    def kernel(...):
        ...
```

(This docstring is the *sole* appearance of `@nki.jit` in `decorators.so`.) The effect chain is:

```text
enable_stack_allocator
  → update_allocator(AllocatorType.stack)
  → opts.allocator_type  = AllocatorType.stack
    opts.skip_allocators = OptLevel.skip_allocators
```

i.e. select the user-space `StackAllocator` (the pure address bookkeeper, 6.7.1 / `private_nkl/utils/StackAllocator.py`) and disable the backend coloring allocator for SBUF+PSUM.

> **QUIRK — only `stack` is observable as an `AllocatorType` member here.** The `decorators.so` string table contains exactly one `AllocatorType` member name: `stack`. The full enum is defined in `CompileOpts.so`, whose IDA decompile is recorded as missing in this corpus, so the other members (e.g. a coloring / reg / linear-scan family matching the backend allocators) are **INFERRED**, not confirmed. What is CONFIRMED: `AllocatorType` is `Optional`-typed and `enable_stack_allocator` selects `.stack`.

---

## 6. Stacking order and the P20 boundary

These six decorators operate **on** an already-built `Kernel`; they create no trace and trigger no compile. The dependency is one-way:

```text
decorators.so  ──imports──▶  TraceKernel.Kernel, CompileOpts.AllocatorType,
                              penguin.ir.OptLevel, functools.partial, dataclasses.replace
```

`@nki.jit` (in `nki/compile.so`, D-W12 §1.1) first wraps the python function into a `Kernel` that owns `.opts: CompileOpts` and `.copy_kernel()`. The trace/compile trigger (lazy first call → `TraceKernel` → `GeneratedNeuronCodegen` → compile pipeline) is **entirely** in TraceKernel/NumpyKernel. The option decorators just emit a new `Kernel` with a `dataclasses.replace`-updated `.opts` *before* any trace happens. The canonical stack (per the §5 docstring) is therefore:

```text
@nki.compiler.<option>      # outermost; clones Kernel with updated opts
@nki.compiler.<option>
@nki.jit                    # innermost; produces the Kernel
def kernel(...): ...
```

Because each layer does `copy_kernel()`, stacking is associative and each layer sees the previous layer's `opts`. This is the structural reason Guard 3 of §4 works: `@skip_middle_end_transformations` must sit *below* (closer to `@nki.jit`) the allocator decorator so its `opt_level` mutation is visible when the allocator guard runs. No caching or trace logic lives in `decorators.so`. [STRONG]

---

## 7. The public `nki.compiler` namespace

`compiler/__init__.so` re-exports a curated subset (verified against its string table):

```text
force_auto_alloc          enable_stack_allocator     skip_middle_end_transformations
allocation_scope          multi_buffer               no_reorder
allocated_psum            allocated_sbuf             psum  sbuf   (via allocator.so)
```

[CONFIRMED — these names are present in `compiler/__init__.so`.]

> **NOTE — three option decorators are deliberately *not* public.** `update_compile_opts`, `update_allocator`, and `override_platform_target_for_test` do **not** appear in the `compiler/__init__` re-export set. `update_compile_opts`/`update_allocator` are the internal primitives the public thin decorators are built from; `override_platform_target_for_test` is a test hook (its `_for_test` name). A user reaches the allocator only through `enable_stack_allocator`.

`allocation_scope()` (→ `AllocationScope`), `multi_buffer(factor=2)` (→ `MultiBufferDirective`), and `no_reorder()` (→ `OperationOrderGuard`) are lexical-scope directives defined in `LexicalScopeDirective.so` and used as `with` context managers (`with nki.compiler.multi_buffer(2): ...`), not as kernel decorators. [CONFIRMED — D-W12 §4.1; out of scope for this page beyond the namespace listing.]

---

## 8. The PSUM/SBUF allocation decorators

The second family is assigned to a tensor's `buffer=` field, not stacked on the kernel. Two parallel classes in `allocator.so` implement the surface: `PSUMAllocation` and `SBUFAllocation`, each with `.alloc(func)`, `.mod_alloc(...)`, and `.auto_alloc()`. These *are* the `nki.compiler.psum` / `nki.compiler.sbuf` namespaces (written `ncc.psum` / `ncc.sbuf` in docstrings).

```python
nl.ndarray(shape, dtype=nl.float32,
           buffer=ncc.psum.mod_alloc(base_bank=0, num_bank_tiles=(2,)))
```

A NKI tensor has up to three dimension kinds, **in order**: logical-block `B`, partition `P`, free `F`. `P` and `F` map directly to the hardware SBUF/PSUM dimensions; `P` must be one-dimensional, `B`/`F` may be multi-dimensional. [CONFIRMED — verbatim: *"Both B and F can be multi-dimensional, while P must be one-dimensional per Neuron ISA constraints."*]

### 8.1 `psum.alloc(func)` — custom PSUM allocation

`func(idx, pdim_size, fdim_size)` returns `(bank_id, start_partition, byte_addr)`, where `idx` is the logical-block index tuple (length == #`B` dims), `pdim_size` == partition count, `fdim_size` == per-partition bytes of the `(P,F)` tile. Verbatim from the binary docstring:

```text
The func returns a tuple of three integers (bank_id, start_partition, byte_addr) indicating
the physical tile location for the input logical block index.
  bank_id        — the PSUM bank ID of the physical tile.
  start_partition — the lowest partition the physical tile allocation starts from.
  byte_addr      — the byte offset into each PSUM bank per partition the physical tile starts from.
```

**ISA constraints (all CONFIRMED verbatim in `allocator.so`):**

- *"In current release, `fdim_size` cannot exceed 2KiB, which is the size of a single PSUM bank per partition. Therefore, a physical PSUM tile cannot span multiple PSUM banks."*
- *"In current release, `start_partition` and `byte_addr` must both be 0."*

> **PROOF — the 2 KiB-per-bank PSUM cap is grounded, not asserted.** The docstring carries its own derivation: a worked example states *"For the above nki_tensor, `pdim_size` should be 128, and `fdim_size` should be `512*sizeof(nl.float32) = 2048` bytes."* `512 × 4 = 2048 = 2 KiB`, exactly the per-partition size of one PSUM bank. The constant is not an inline immediate (the disassembled `verify_allocation` compares via `PyObject_RichCompare` against interned `PyLong`s, not `cmp $0x800` — see §8.6), so the docstring's `2048` *is* the authoritative recovered value. This matches the PSUM bank geometry in Part 1 (1.05 SBUF/PSUM geometry): the `psum_bank_map` carries two bank addresses, `psum_addr_0` and `psum_addr_1` (confirmed symbols `psum_bank_map.psum_addr_0` / `psum_addr_1`), and a single physical tile is confined to one of them.

Failure paths (verbatim runtime error strings, distinct from the docstrings):

- `"Cannot find bank_id for PSUM allocation: "` — `func` returned a bank the map cannot place.
- `", expect tuple of (bank_id, start_partition, byte_addr)"` — return shape check.

### 8.2 `sbuf.alloc(func)` — custom SBUF allocation

`func(idx, pdim_size, fdim_size)` returns `(start_partition, byte_addr)` (two-tuple — no bank, since SBUF is flat-partitioned). The `start_partition` rules are the central SBUF ISA constraint; verbatim:

```text
- If 64 < pdim_size <= 128, start_partition must be 0
- If 32 < pdim_size <= 64,  start_partition must be 0 or 64
- If 0  < pdim_size <= 32,  start_partition must be one of 0/32/64/96
```

> **PROOF — the SBUF `start_partition` tiers are corroborated by the disassembly.** Beyond the verbatim docstring, the `SBUFAllocFuncBase.verify_allocation` body (@ `0x346a0`, region `0x346a0..0x38940`) contains exactly **six** `PyObject_RichCompare`/`…RichCompareBool` callsites — structurally the two-sided boundary tests of three partition tiers (`64 < p ≤ 128`, `32 < p ≤ 64`, `0 < p ≤ 32`). The tier constants `128/96/64/32` themselves are interned `PyLong`s in the Cython constant pool (only `__pyx_int_0`/`__pyx_int_1` are named globals; everything else is `lea`'d from `__pyx_mstate`), so the docstring numerals are the authoritative recovered values and the six compares confirm the *shape* of the check. [PROOF strength: numerals CONFIRMED via docstring; six-compare structure STRONG via disassembly.]

The `byte_addr` range and alignment (verbatim):

```text
On NeuronCore-v2, a valid byte_addr can be any integer values from 0 (inclusive) to
192KiB-16KiB=(192-16)*1024 (exclusive). 192KiB is the physical size of a SBUF partition
and 16KiB is allocated for compiler internal usage.
In addition, the base_addr must be aligned to nki.language.constants.sbuf_min_align.
```

> **PROOF — the SBUF `[0, 192 KiB − 16 KiB)` window.** The docstring spells the arithmetic out as `(192-16)*1024` = `180 KiB` = `184320` bytes. `192 KiB` is the physical SBUF partition size (Part 1, 1.05) and the top `16 KiB` is reserved for the compiler — consistent with the bank geometry page. The alignment floor is `nki.language.constants.sbuf_min_align` (a named symbol, not an inline number). [Numerals CONFIRMED verbatim; the alignment value itself is a reference into `nki.language.constants` — INFERRED-resolved, not re-disassembled.]

Failure paths (verbatim):

- `"Cannot find start_partition for SBUF allocation: "`
- `", expect tuple of (start_partition, byte_addr)"`
- `" does not satisfy minimum alignment requirement "`

### 8.3 `psum.mod_alloc(...)` / `sbuf.mod_alloc(...)` — modulo (double-buffered) allocation

Modulo allocation is `alloc()` with an auto-generated allocation function that cycles a tile through a small set of physical tiles by index modulo, enabling double/multi-buffering. Stub signatures (D-W12 §4.2, from the authoritative `.pyi`):

```python
psum.mod_alloc(*, base_bank, base_addr=0, base_partition=0,
               num_bank_tiles=(), num_par_tiles=(), num_free_tiles=())
sbuf.mod_alloc(*, base_addr, base_partition=0,
               num_par_tiles=(), num_free_tiles=())
```

PSUM: tile `i` → bank `base_bank + (i % len(num_bank_tiles-cycle))`. SBUF: `byte_addr = base_addr + (i % num_free_tiles)*tile_bytes`. The `mod_alloc.genexpr` symbol confirms a generator-comprehension builds the per-tile mapping. [CONFIRMED — `PSUMAllocation.mod_alloc.genexpr`, `SBUFAllocation.mod_alloc` symbols.]

**Constraints (verbatim runtime/doc strings):**

- PSUM `base_addr` / `base_partition` must be `0` *"in the current version"*.
- `"num_par_tiles in PSUM must be () or all 1"` and `"num_free_tiles in PSUM must be () or all 1"` (runtime error strings — i.e. PSUM cannot multi-tile along P or F, only along banks).
- For both, each `num_*_tiles` tuple: *"The length of the tuple must be empty or equal to the length of block dimension for the tensor"*; PSUM additionally: *"Currently must be an empty tuple or (1, 1, ...)."*
- SBUF `base_partition` must be `0` *"in the current version"*.

[All CONFIRMED — strings present in `allocator.so`.]

### 8.4 `auto_alloc()` — the default marker

`psum.auto_alloc()` / `sbuf.auto_alloc()` is a marker telling the compiler to auto-place the tensor; `nl.psum` and `nl.sbuf` are exactly aliases for them. Verbatim: *"Initialize a tensor with `buffer=nl.psum` is equivalent to `buffer=ncc.psum.auto_alloc()`."* (and the SBUF twin). [CONFIRMED.]

> **GOTCHA — no mixing within a kernel.** Verbatim: *"All PSUM tensors in a kernel must either all be marked as `auto_alloc()`, or all be allocated with `alloc` or `mod_alloc`."* (and the SBUF twin). You cannot mix auto and direct allocation for the same memory space in one kernel. This is the per-kernel uniformity rule a reimplementation must enforce at trace finalize. [CONFIRMED.]

### 8.5 The `AllocFunc` machinery and `verify_allocation`

The three decorators are backed by an `AllocFunc` hierarchy (confirmed symbols):

```text
AllocFunc.{func, create_buffer_allocation, create_buffer_allocation_on_blocks, verify_allocation}
PSUMAllocFuncBase / SBUFAllocFuncBase  →  .verify_allocation  (the per-tile ISA validator)
concrete: PSUMAllocFunc / PSUMModuloAllocFunc / SBUFAllocFunc / SBUFModuloAllocFunc
```

`verify_allocation` is the single choke point that enforces §8.1–§8.3 *per logical block, before lowering*. The base `AllocFunc.verify_allocation` (@ `0x265c0`) dispatches to the space-specific override (`PSUMAllocFuncBase.verify_allocation` @ `0x38940`, `SBUFAllocFuncBase.verify_allocation` @ `0x346a0`). [CONFIRMED — all three `…verify_allocation` symbols + mdefs at the listed addresses.]

Reconstructed validator shape (annotated; constants per §8.1–8.2):

```c
// PSUMAllocFuncBase.verify_allocation @ 0x38940  (per (bank_id, start_partition, byte_addr) tuple)
verify_psum(tuple ret, int pdim_size, int fdim_size):
    require len(ret) == 3            // else "... expect tuple of (bank_id, start_partition, byte_addr)"
    require fdim_size <= 2048        // 2 KiB per PSUM bank; tile cannot span banks
    require start_partition == 0     // "start_partition and byte_addr must both be 0"
    require byte_addr      == 0
    if bank_id not in psum_bank_map: raise "Cannot find bank_id for PSUM allocation: "

// SBUFAllocFuncBase.verify_allocation @ 0x346a0  (per (start_partition, byte_addr) tuple)
verify_sbuf(tuple ret, int pdim_size, int fdim_size):
    require len(ret) == 2            // else "... expect tuple of (start_partition, byte_addr)"
    // three partition tiers — six RichCompare boundary tests confirmed in disasm:
    if   64 < pdim_size <= 128: require start_partition == 0
    elif 32 < pdim_size <=  64: require start_partition in {0, 64}
    elif  0 < pdim_size <=  32: require start_partition in {0, 32, 64, 96}
    require 0 <= byte_addr < (192-16)*1024            // 180 KiB window on NeuronCore-v2
    require byte_addr % sbuf_min_align == 0           // else "does not satisfy minimum alignment requirement"
    if start_partition unplaceable: raise "Cannot find start_partition for SBUF allocation: "
```

### 8.6 The auto allocator and its TraceKernel hook

When tensors are `auto_alloc`, the `Allocator` class (the auto path) runs at `TraceKernel.allocate_pending_tensors` (D-W12 §2.0 `expand_kernel_with_ctx` → `allocate_pending_tensors`). Its method surface (confirmed symbols): `allocate`, `allocate_psum_tensor`, `allocate_sbuf_tensor`, `allocate_pending_inst_tile`, `allocate_pending_tensors_in_scope`, `assign_allocation`, `enter_scope`/`exit_scope`/`scope`, `get_identity_tensor`, `get_activation_bias_tensor`. It tracks live allocations via `psum_bank_map` (`psum_addr_0`/`psum_addr_1`) and an SBUF address map. [CONFIRMED — `Allocator.allocate_psum_tensor` / `allocate_sbuf_tensor` / `allocate_pending_inst_tile` / `allocate_pending_tensors_in_scope` / `get_identity_tensor` symbols.]

> **NOTE — re-verification ceiling.** The numeric ISA constants (`2048`, `128`, `96`, `64`, `32`, `192`, `16`) are **not** inline x86 immediates in `verify_allocation`; they are interned `PyLong` objects in the Cython constant pool (`lea`'d from `__pyx_mstate_global_static`, with only `__pyx_int_0`/`__pyx_int_1` exposed as named globals). I therefore ground each on the **verbatim docstring numeral** (CONFIRMED) plus the **structural** disassembly evidence (e.g. six `RichCompare` sites in `SBUFAllocFuncBase.verify_allocation` matching three two-sided tiers — STRONG). I could not re-disassemble the constants as raw immediates because Cython does not emit them as such. The `decorators.so` and `CompileOpts.so` IDA decompiles are recorded as *missing* in this corpus, so closure-internal control flow there is reconstructed from the body disassembly + string table rather than a per-symbol `.c` sidecar.

---

## 9. How options reach the compile pipeline

The threading mechanism is the `CompileOpts` dataclass riding inside the `Kernel`. Write side (this page) and read side (downstream strands):

| Field | Set by | Value | Read by (downstream) |
|---|---|---|---|
| `opts.skip_middle_end` | `skip_middle_end_transformations` | `OptLevel.skip_middle_end` | Penguin middle-end driver — bypass HLO→kernel legalize/fuse/layout (H/U strands) |
| `opts.opt_level` | (coupled to above) | `OptLevel.skip_middle_end` | `update_allocator` Guard 3 |
| `opts.platform_target` | `override_platform_target_for_test` | `<arg>` | target resolution / `CompileCommand` (`--target <arch>`) |
| `opts.allocator_type` | `update_allocator` / `enable_stack_allocator` | `AllocatorType.stack` | backend allocator selector |
| `opts.skip_allocators` | `update_allocator` / `enable_stack_allocator` | `OptLevel.skip_allocators` | BIR K-strand — skip `ColoringAllocatorWithLoop` for SBUF/PSUM, honor user addresses |
| `opts.force_auto_alloc` | `force_auto_alloc` | `True` | backend auto-allocator; ignore explicit in-kernel placement |
| *(any field)* | `update_compile_opts` | `**kwargs` | as above |

At compile time these `opts` ride the `Kernel` into the trace/compile path. For the `baremetal`/`benchmark`/`simulate` numpy kernels, `opts` is serialized into the `neuronx-cc compile` shell-out (D-W12 §3.2 `_compile` reads `OptLevel` / `skip_allocators`); for the `jax`/`torchxla` framework kernels, `opts` is embedded in the base64-JSON `backend_config` of the XLA `CustomCall` (D-W12 §2.4 `encode_backend_config` serializes the `opts` key). Either way the same `CompileOpts` object the decorators on this page mutated is the object the backend re-reads. [STRONG — cross-ref D-W12 §2.4 / §3.2; the read sides are downstream strands, not in `decorators.so`.]

---

## 10. Adversarial self-verification

The five strongest claims on this page, re-challenged against the binary:

1. **`@nki.jit` is not in `decorators.so`.** Challenge: maybe it's a hidden symbol. Result: the only `@nki.jit` string is inside the `enable_stack_allocator` docstring; no `jit` pyx wrapper symbol exists in this module. **Holds (CONFIRMED).**
2. **`update_allocator` Guard 3 tests `opt_level`, not `skip_middle_end`.** Challenge: the writer sets `skip_middle_end`, so maybe the gate does too. Result: both strings are in the table; D-P21 §6 L82 and the field map show the gate reads `opt_level == OptLevel.skip_middle_end` while the writer sets the `skip_middle_end` field — kept coupled in `CompileOpts`. Flagged as a GOTCHA. **Holds.**
3. **PSUM `fdim_size ≤ 2 KiB` per bank.** Challenge: is `2048` real or a guess? Result: docstring carries the derivation `512*sizeof(nl.float32) = 2048` and `psum_bank_map` exposes `psum_addr_0`/`psum_addr_1` (two banks). Not an inline immediate; grounded on verbatim docstring + bank-map symbols. **Holds (CONFIRMED numeral, with the ceiling noted).**
4. **SBUF three-tier `start_partition` rule.** Challenge: are the tiers fabricated? Result: verbatim docstring lists all three tiers; `SBUFAllocFuncBase.verify_allocation` @ `0x346a0` has six `RichCompare` callsites = three two-sided tests. **Holds (CONFIRMED numerals + STRONG structure).**
5. **`force_auto_alloc` outer @ `0xe350`, inner @ `0xe980`.** Challenge: D-P21 said `0xd9e0`. Result: the wheel's symbol table places them at `0xe350`/`0xe980`; the `0xd9e0` figure is the two-VA-frame artifact. Issued an in-place CORRECTION. **Corrected.**

---

## Cross-references

- **Part 1 / 1.05 — SBUF/PSUM geometry, `EngineInfo`** — the 2 KiB PSUM bank and 192 KiB SBUF partition this page's constraints are grounded in.
- **Part 8 / 8.16–8.21 — backend allocators** — `ColoringAllocatorWithLoop`, the SBUF/PSUM/DRAM allocators, and the REG/LinearScan alternatives that `opts.skip_allocators` switches off.
- **6.7.1 — nkilib `StackAllocator`** — the user-space allocator `enable_stack_allocator` selects (`private_nkl/utils/StackAllocator.py`).
- **D-W12 §1–§3 — entrypoints & FrameworkKernel/NumpyKernel** — where `@nki.jit` lives and how `opts` is serialized into the compile shell-out / `backend_config`.
- **D-W11 / D-P strands — TraceKernel & NeuronCodegen** — `allocate_pending_tensors`, the trace lifecycle that runs the auto `Allocator`.

*Provenance: this page is a re-verified synthesis of **D-P21** (NKI compiler-option decorators) and **D-W12** (NKI framework-binding + alloc surface), re-grounded against `decorators.so`, `allocator.so`, and `CompileOpts.so` from the `cp310` wheel.*
