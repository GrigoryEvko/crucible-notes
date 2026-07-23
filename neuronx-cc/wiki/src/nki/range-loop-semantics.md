# Range & Loop Semantics: static / affine / sequential range

> *All symbols, offsets, and source lines on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules). The public iterators live in `neuronxcc/nki/language/iterators.cpython-310-*.so` (BuildID `203a02d9…`); the lowering lives in `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-*.so`, class `NeuronCodegen` (BuildID `9eb1020e…`). cp311/cp312 ship byte-identical Cython twins at different addresses. Treat every address as version-pinned.*

## Abstract

A Python `for` loop inside a NKI kernel does not survive as a loop by default. During **trace time** — the abstract-interpretation pass where `NeuronCodegen` walks the kernel body op-by-op and emits Penguin IR — every loop is resolved into one of exactly two outcomes: it is either **fully unrolled** (the body is replayed once per iteration, emitting `tripcount` copies of every instruction into the IR), or it is **deferred** as a single Penguin loop axis (`AffineAxis` / `DynamicAxis`) carrying one symbolic induction variable (`LoopVar`) that the backend later schedules and tiles. Which outcome you get is decided entirely by *which iterator you wrote*, not by any dependence analysis.

There are three loop iterators — `nl.static_range`, `nl.affine_range`, `nl.sequential_range` — plus the implicit fourth case, a bare Python `range(...)`. `static_range` unrolls at trace time and emits no axis. `affine_range` and `sequential_range` both emit one deferred axis through the *same* node factory, `create_affine_axis_block`; they differ only by an **`AxisType` attribute** carried on that node — `Affine` (reorderable / vectorizable) versus `Sequential` (ordered / no-reorder). A bare `range(...)` is rewritten to `sequential_range` — the conservative, dependency-safe default. This page reconstructs that decision tree from the binary: the trampoline that routes each iterator to `NeuronCodegen`, the `affine_range` generator that is the real workhorse, the `Affine`-vs-`Sequential` attribute split, the `Axis`-vs-`DynamicAxis` static-vs-runtime IR-type split, and the `create_affine_axis_block` choke point where a Python range becomes a Penguin loop-axis node.

The single most important thing to internalize: **the affine/sequential distinction is a scheduler hint encoded as an attribute on one node class, while the static/deferred distinction is a structural fork between "emit N copies" and "emit one axis."** Confusing the two is the most common way to misread this subsystem.

| | |
|---|---|
| **Public iterators** | `iterators.affine_range` (`iterators.py:46`), `sequential_range` (`:111`), `static_range` (`:23`), `sync_program` (`:17`) |
| **Trampoline** | each is a 3-line shim → `nki_ctx().<name>(*args, **kwds)` (`affine_range` pw @ `0x12230`) |
| **Real lowering** | `NeuronCodegen.affine_range` generator @ `0x240260` — all four ranges funnel here |
| **Sequential =** | `NeuronCodegen.sequential_range` @ `0xa7c30` (py 697) → `affine_range(..., axis_type=AxisType.Sequential)` |
| **Static =** | `NeuronCodegen.static_range` @ `0x72b40` (py 694) → trace-time full unroll, **no axis** |
| **Bare range =** | `NeuronCodegen.builtin_range` @ `0x81900` → `opts.trace_time_unroll_builtin_range ? unroll : sequential_range` |
| **AxisType enum** | KernelBuilder `.rodata`: members map to strings `AffineAxis`, `Sequential`, `DynamicAxis` (+ `Default`); enum name `AxisType` |
| **Node factory** | `NeuronCodegen.create_affine_axis_block` @ `0x881f0` — kwargs `{loop_name_id, lb, ub, stride, it, parent, insert_before, axis_type, attrs}` |
| **IR target (static)** | `bir::LoopAxis` via `InstLoop::setAxis` (E16); **(runtime)** `bir::DynamicForLoopAxis` via `InstDynamicForLoop::setAxis` |
| **Cross-ref** | [5.3 penguin/axis-loop-model](../penguin/axis-loop-model.md) · [5.26 penguin/dynamic-for-loop](../penguin/dynamic-for-loop.md) · §6.5.5 range emitters |

---

## 1. The two-layer split: iterator surface vs codegen lowering

The NKI loop machinery is physically two ELF modules with a thin trampoline between them.

**Layer A — the language surface.** `iterators.cpython-310-*.so` exports the user-visible names: `affine_range`, `sequential_range`, `static_range`, plus the index helpers `arange`/`ds` and the SPMD barrier `sync_program`. Every one of these is a *pure dispatch shim*. The decompiled `affine_range` public wrapper (`__pyx_pw_…_iterators_5affine_range` @ `0x12230`) is three operations:

```c
// iterators.affine_range(*args, **kwds)  —  iterators.py:46
static PyObject *iterators_affine_range(PyObject *args, PyObject *kwds) {
    PyObject *ctx = GetModuleGlobalName("nki_ctx")();   // the active codegen context
    PyObject *m   = PyObject_GetAttr(ctx, "affine_range"); // bind ctx.affine_range
    return PyObject_Call(m, args, kwds);                  // ctx.affine_range(*args, **kwds)
}
```

The identical pattern holds for `sequential_range` (`"sequential_range"`, `iterators.py:111`), `static_range` (`"static_range"`, `iterators.py:23`), and `sync_program` (`"sync_program"`, `iterators.py:17`). `nki_ctx()` returns the process-singleton trace context (the `NeuronCodegen` instance); see [6.1.2 nki_ctx scopes](./nki-ctx-scopes.md). So **`nl.affine_range(n)` is exactly `nki_ctx().affine_range(n)`** — all semantics live in `NeuronCodegen.<name>`; `iterators.py` carries none.

> **NOTE —** The "active codegen context" the trampoline fetches is the same `NeuronCodegen` referenced throughout Part 5/6 as the trace-time IR builder (it owns `self.builder`, the pelican/BIR `IRBuilder` C++ binding, and `self.curstmt`, the insertion anchor).

**Layer B — the lowering.** `NeuronCodegen` (in `KernelBuilder.so`) holds the four range methods. The `nm` roster of the control/scope methods places them adjacent: `builtin_range` (`_61`), `affine_range` (`_71`), `static_range` (`_89`), `sequential_range` (`_91`), with `create_affine_axis_block` (`_55`) as the shared node factory.

---

## 2. The decision tree (the one diagram to keep)

```text
              Python source inside @nki kernel
              ┌──────────────┬──────────────┬───────────────┬──────────────┐
       nl.static_range   nl.affine_range  nl.sequential_range   range(...)        ← what you wrote
              │              │              │                   │
              │              │              │            builtin_range(@0x81900)
              │              │              │             opts.trace_time_unroll_
              │              │              │             builtin_range ?
              │              │              │             ┌────┴─────┐
              │              │              │           true       false
              │              │              │             │          │
              ▼              ▼              ▼             ▼          ▼
   ┌─────────────────┐   axis_type=     axis_type=   (unroll)   sequential_range
   │  FULL UNROLL    │    Affine        Sequential                   │
   │  no Penguin axis│      │              │                         │
   │  body × tripcnt │      └──────┬───────┴─────────────────────────┘
   │  emitted now    │             ▼
   └─────────────────┘    NeuronCodegen.affine_range  (gen @0x240260)
                                   │  normalize_range_args → lb,ub,stride,tripcount
                                   │  extract_loop_directives → attrs (pragmas)
                                   │  has_runtime_value(ub)?
                                   ┌───────────┴────────────┐
                                  no                       yes
                                   │                        │
                            ir_type = Axis           ir_type = DynamicAxis
                                   │                        │
                                   └──────────┬─────────────┘
                                              ▼
                          create_affine_axis_block(loop_name_id, lb, ub,
                              stride, it, parent, insert_before,
                              axis_type, attrs)            (@0x881f0)
                                              ▼
                          self.builder → bir InstLoop+LoopAxis   (static)
                                       | bir InstDynamicForLoop+DynamicForLoopAxis (runtime)
```

Two orthogonal forks decide the fate of a loop:

1. **Unroll vs defer** (the *structural* fork): `static_range` (and `range` under `trace_time_unroll_builtin_range`) take the left branch — the loop body is materialized `tripcount` times at trace time and **no axis node is produced**. `affine_range`/`sequential_range`/`range` take the right branch — one axis node, one symbolic `LoopVar`.
2. **Affine vs Sequential** (the *attribute* fork, only on the deferred branch): the `axis_type` attribute carried on the *same* node tells the backend scheduler whether iterations may be reordered/vectorized (`Affine`) or must run in program order (`Sequential`).

A third, independent fork — **static vs runtime tripcount** (`Axis` vs `DynamicAxis`) — is decided inside `affine_range` by `has_runtime_value(ub)` and is *not* tied to the affine/sequential choice; see §5.

---

## 3. `static_range` — full unroll at trace time

```c
// NeuronCodegen.static_range(*args, **kwargs)  —  KernelBuilder.py:694, pw @0x72b40
//   thin wrapper, only `self` operand; forwards to the unroll path
def static_range(self, *args, **kwargs):
    # No create_affine_axis_block call, no LoopVar, no AxisType.
    # The range object drives a *concrete* Python iteration during the trace:
    # for each i in range(...): replay the loop body, emitting that iteration's
    # instructions directly into the current scope. tripcount copies result.
    ...
```

The docstring, interned verbatim in the iterators `.so` string pool, states the contract precisely:

> *"Create a sequence of numbers for use as loop iterators in NKI, resulting in a fully unrolled loop. Unlike `affine_range` or `sequential_range`, Neuron compiler will fully unroll the loop during NKI kernel tracing. […] Due to loop unrolling, compilation time may go up significantly […]. On-chip memory (SBUF) usage may also go up significantly […]. No loop-level optimizations will be performed in the compiler. `static_range` should only be used as a fall-back option for debugging purposes when `affine_range` or `sequential_range` is giving functionally incorrect results or undesirable performance characteristics."*

Consequences, stated plainly for a reimplementer:

- **No `LoopAxis` is emitted.** Downstream passes (K01 `ColoringAllocatorWithLoop`, K18 SW-pipeline ring rotation) never see a loop here — they see a straight-line block of `tripcount × |body|` instructions. The induction variable is a *concrete Python `int`*, one distinct value per unrolled copy, not a symbolic `LoopVar`.
- **SBUF pressure is multiplicative.** Each unrolled iteration that allocates a tile allocates a *distinct* tile (no buffer reuse across iterations is implied by an axis), which is why the docstring warns about on-chip memory blowup.
- **It is a debugging escape hatch**, not a performance tool — the binary's own docstring says so.

> **GOTCHA —** Because `static_range` materializes concrete ints, you *can* legally use the loop index in places that demand a compile-time constant (e.g. as a `static_range`-indexed Python list lookup of tensors). The same code under `affine_range` would hand you a symbolic `LoopVar` and fail. This is the only legitimate reason to reach for `static_range` outside of debugging.

---

## 4. `affine_range` and `sequential_range` — one node, two attributes

Both deferring iterators funnel through **one** generator. `sequential_range` is literally `affine_range` with a different `axis_type`:

```c
// NeuronCodegen.sequential_range(*args, **kwargs)  —  KernelBuilder.py:697, pw @0xa7c30
//   disasm: GetAttr(self,"affine_range"); GetModuleGlobalName("AxisType"); GetAttr(.,"Sequential")
def sequential_range(self, *args, **kwargs):
    return self.affine_range(*args, name=…, axis_type=AxisType.Sequential)
```

The disassembly shows the exact operand sequence: bind `self.affine_range`, fetch the module global `AxisType`, get its `.Sequential` member, and call `affine_range` with it as `axis_type`. `affine_range` itself supplies the *default* `axis_type` — the `Affine` (parallel/reorderable) case — when called directly.

### 4.1 The `AxisType` attribute and what it means to the scheduler

The `AxisType` enum is interned in KernelBuilder `.rodata`. Its members resolve to the strings `AffineAxis`, `Sequential`, and `DynamicAxis` (the enum name `AxisType` and the member `Default` are also interned). The affine/sequential split is **not** two different node classes — it is this one attribute on the *same* `LoopAxis` node:

| `axis_type` | Scheduler contract | From docstring |
|---|---|---|
| `Affine` (default) | iterations **independent** → may be reordered, vectorized, software-pipelined across compute engines within one NeuronCore | "parallel loop iterators … default … when there is **no** loop carried dependency … allows … additional loop-level optimizations, such as loop vectorization" |
| `Sequential` | inter-iteration **dependency respected** → conservative, no reorder/vectorize | "sequential loop iterators … should be used when there is a loop carried dependency … informs Neuron compiler to respect inter-loop dependency and perform much more conservative loop-level optimizations compared to `affine_range`" |

The `affine_range` docstring also pins down two subtleties a reimplementer will get wrong:

- **Associative reductions are not loop-carried dependencies** in this model. Multiple `nl.matmul` / `nisa.nc_matmul` calls accumulating into one PSUM buffer defined *outside* the loop are still safe under `affine_range` — the docstring explicitly carves this out with a worked example.
- `affine_range` does **not** parallelize across NeuronCores. "Since each kernel instance only runs on a single NeuronCore, `affine_range` does **not** parallelize different loop iterations across multiple NeuronCores. However, different iterations could be parallelized/pipelined on different compute engines within a NeuronCore." Cross-NeuronCore parallelism is the SPMD launch grid's job (`nc()` sharding), not the loop iterator's — see [6.1.4 SPMD programming model](./spmd-programming-model.md).

> **QUIRK —** Using `affine_range` where a loop-carried dependency genuinely exists is *unsafe* and, in the docstring's words, "could lead to numerical errors". The compiler does not verify the no-dependency claim — `affine_range` is an assertion *by the kernel author*, and `Sequential` is the safe default that a bare `range` falls back to (§4.3). The whole point of the attribute is to let the author *grant* reordering permission the compiler cannot prove on its own.

### 4.2 The `affine_range` generator — the workhorse

`affine_range` is a `@contextmanager` (a Cython generator, `__pyx_gb_…_NeuronCodegen_72generator12` @ `0x240260`). The ordered call sequence below is reconstructed from the generator's symbol references (operands: `normalize_range_args`, `extract_loop_directives`, `has_runtime_value`, `Axis`, `DynamicAxis`, `create_affine_axis_block`, `loop_scope`, `allocation_scope`, `LoopVar`, `iv`, `pred_lt`, `wrap_expr`, `opt_level`, `nullcontext`):

```c
// NeuronCodegen.affine_range  —  generator @0x240260
@contextmanager
def affine_range(self, *args, name=…, axis_type=AxisType.Affine):
    lb, ub, stride, tripcount = self.normalize_range_args(*args)   # parse range-like bounds
    attrs = self.extract_loop_directives()                         // §4.4 loop pragmas

    # ── static-vs-runtime IR-type fork (independent of affine/sequential) ──
    if has_runtime_value(ub):          # data-dependent upper bound?
        ir_type = DynamicAxis          # → bir DynamicForLoopAxis
    else:
        ir_type = Axis                 # → bir LoopAxis (the canonical static case)

    block = self.create_affine_axis_block(                         // §5 — the choke point
        loop_name_id, lb, ub, stride, it,
        parent, insert_before,
        axis_type,                     # Affine | Sequential | (Dynamic via ir_type)
        attrs)                         # the loop directives from extract_loop_directives

    # ── scope the body under the induction var, inside a fresh allocation region ──
    iv = wrap_expr(it)                 # the symbolic induction value
    with self.loop_scope(predicates=[pred_lt(iv, ub)]):            // §4.3 — iv < ub guard
        with self.allocation_scope():                              // SBUF/PSUM lifetime region
            yield LoopVar(iv)          # ← the value the kernel `for` binds
    # (on __exit__ the generator resumes past the yield: region closed)
```

Notice the body is yielded **once**: the kernel's `for i in nl.affine_range(n):` runs its body a single time during the trace, binding `i` to one symbolic `LoopVar`. Every instruction emitted in that single pass is parameterized by `iv` and lands inside the `LoopScope` region. The backend, not the trace, replays it `tripcount` times — which is exactly why `affine_range` keeps compile time and SBUF bounded relative to `static_range` (the docstrings cite "better compilation time compared to the fully unrolled iterator `static_range`").

> **NOTE — the `trivial_loop` fast path.** The KernelBuilder string pool interns `"trivial loop "` and the generator references `OptLevel`/`nullcontext`. When `tripcount == 1` the generator takes a degenerate path that uses a `nullcontext` instead of opening a real `LoopScope` — a one-iteration loop needs no axis, no `iv < ub` predicate, and no allocation region of its own. This avoids emitting a `LoopAxis` whose range is `[0,1)`. The `"trivial loop "` string and the `nullcontext` / `opt_level` operands are all present; the exact guard condition is **[INFERRED]**.

### 4.3 The `iv < ub` predicate and `loop_scope`

The `loop_scope` context manager (`NeuronCodegen_46`, generator @ `0x780d0`, kind string `"LoopScope"`) is the region opened *around* the axis body. It carries `predicates=[pred_lt(iv, ub)]` — the induction-variable bound guard, built by `pred_lt` (a "less-than" predicate). This is the trace-time form of the loop's continuation condition; it is *lowered* by the predicate machinery (`lower_predicates` → `lower_simple_predicate` → `opcode_for_predicate`) into a Penguin compare expression over the axis induction variable. The `Affine`-vs-`Sequential` attribute and this `iv < ub` predicate are independent: the predicate bounds the iteration domain; the attribute grants/denies reordering.

### 4.4 Loop directives (pragmas → axis attrs)

`extract_loop_directives` (module function, `pf @0x80330`) parses kernel loop pragmas into the `attrs` dict passed to `create_affine_axis_block`. The directive taxonomy is read from KernelBuilder `.rodata`:

| Directive | Purpose | Error sentinel |
|---|---|---|
| `AutoPipelineDirective` | request SW-pipelining of the axis | "Multiple auto_pipeline directives are not supported!" |
| `MultiBufferDirective` | buffer-ring depth (multi-buffering) | "Multiple multi_buffer directives are not supported!" |
| `PipelineStageDirective` | per-region `stage_id` / `execution_order` marker | — |
| `LexicalScopeDirective` | lexical scoping hint | — |
| (opt fields) | `opt_level`, `skip_allocators`, `accumulated_tripcount`, `tripcount_expr`, `num_stages`, `trace_time_unroll_builtin_range` | "Unsupported directive" (catch-all) |

These attrs ride on the `LoopAxis` and are read by the backend SW-pipeline: `MultiBufferDirective` → buffer-ring size; `AutoPipeline`/`PipelineStage` → stage count (the denominator in K18's Euclidean `ModuloExpr` = number of physical buffers = stage count); `num_stages`/`accumulated_tripcount` feed the unroll/rotation period. (Detailed in §6.5.5, the backend range emitters / pass catalog.)

### 4.5 Bare `range(...)` → `sequential_range` (the conservative default)

A Python `range(...)` used as a loop iterator inside a kernel is intercepted by `builtin_range`:

```c
// NeuronCodegen.builtin_range  —  pw @0x81900
def builtin_range(self, *args, **kwargs):
    if self.opts.trace_time_unroll_builtin_range:   # GetAttr(self,"opts"),
        return <unroll now, like static_range>      #   GetAttr(opts,"trace_time_unroll_builtin_range")
    else:
        return self.sequential_range(*args, **kwargs)   # GetAttr(self,"sequential_range")
```

The decompiled body reads `self.opts.trace_time_unroll_builtin_range`; on the false branch it fetches and calls `self.sequential_range`. This grounds the `sequential_range` docstring verbatim: *"Inside a NKI kernel, any use of Python `range(...)` will be replaced with `sequential_range(...)` by Neuron compiler."*

> **QUIRK —** The bare-range default is `Sequential`, **not** `Affine`. A reimplementer's instinct might be that an un-annotated loop is "probably parallel" — the binary does the opposite. The conservative choice is correct: a loop the author never thought about must not be silently granted reorder permission it may not have, which would risk the very numerical errors §4.1 warns about. To get reorder/vectorization you must *opt in* with `affine_range`; `range` only ever gives you `Sequential` (or a full unroll if `trace_time_unroll_builtin_range` is set globally).

---

## 5. `create_affine_axis_block` — where a range becomes a Penguin loop-axis node

This is the single choke point where the deferred branch turns a Python range into a Penguin loop-axis node. It is a generator (`NeuronCodegen_55`, `__pyx_gb_…_NeuronCodegen_56generator8` @ `0x881f0`) that builds a kwargs dict and calls into `self.builder` (the pelican/BIR `IRBuilder`):

```c
// NeuronCodegen.create_affine_axis_block  —  generator @0x881f0
@contextmanager
def create_affine_axis_block(self, loop_name_id, lb, ub, stride, it,
                             parent, insert_before, axis_type, attrs):
    kwargs = {                          # exact key set from .rodata + disasm
        "loop_name_id": loop_name_id,   # the axis/loop name
        "lb": lb, "ub": ub, "stride": stride,   # iteration domain [lb, ub) step stride
        "it": it,                       # induction-variable handle
        "parent": parent,               # enclosing axis/region (loop-nest parent)
        "insert_before": insert_before, # Axis/Instruction anchor (must be None here —
                                        #   sentinel: "unexpected case in create affine
                                        #   axis block, insert before should be None")
        "axis_type": axis_type,         # Affine | Sequential | Dynamic
        "attrs": attrs,                 # loop directives (§4.4)
    }
    block = self.builder.<makeAffineAxisBlock>(**kwargs)   # GetAttr(self,"builder"); call
    yield block
```

The kwargs key set is exactly `{loop_name_id, lb, ub, stride, it, parent, insert_before, axis_type, attrs}` (read from the string pool + the wrapper's kwargs construction). The error sentinel `"unexpected case in create affine axis block, insert before should be None"` is interned verbatim and guards the `insert_before` anchor.

### 5.1 Mapping to the BIR LoopAxis family (the static/runtime fork realized)

The `axis_type` + `ir_type` selection from §4.2 lands on two distinct BIR node families (cross-ref [E16 / 5.3 axis-loop-model](../penguin/axis-loop-model.md), [5.26 dynamic-for-loop](../penguin/dynamic-for-loop.md)):

| trace-time choice | BIR instruction | BIR axis node | `setAxis` | key fields |
|---|---|---|---|---|
| static `ub` (`ir_type=Axis`), `axis_type` ∈ {`Affine`,`Sequential`} | `InstLoop` | `bir::LoopAxis` | `InstLoop::setAxis(string, l, l, l)` @ `0x3237f0` | `name@+0x48`, `lb@+0x20`, `ub@+0x28`, `stride@+0x30`; toJson key `"LoopAxis"` |
| runtime `ub` (`ir_type=DynamicAxis`) | `InstDynamicForLoop` | `bir::DynamicForLoopAxis` | `InstDynamicForLoop::setAxis` @ `0x3265a0` | `hasRuntimeValue=1`; 3× `QuasiAffineExpr`; toJson key `"DynamicForLoopAxis"` |

The pivotal observation for a reimplementer: **`Affine` and `Sequential` produce the *same* node class** (`InstLoop`+`LoopAxis`) — the distinction is the `axis_type` attribute carried on the node, which the scheduler (K01 `ColoringAllocatorWithLoop`) reads to decide whether the trip iterations may be reordered. Only the **static-vs-runtime tripcount** distinction (`has_runtime_value`) changes the node *class* (`LoopAxis` vs `DynamicForLoopAxis`). Three trace-time concepts, two node classes, one shared factory.

---

## 6. Evidence summary

| Claim | Grounding | Confidence |
|---|---|---|
| `sequential_range` = `affine_range` with `axis_type=AxisType.Sequential` | the `sequential_range` body `@0xa7c30` does `GetAttr(self,"affine_range")`, then resolves the module global `AxisType` and `GetAttr(., "Sequential")`, and calls `affine_range` with it; the traceback string pins `KernelBuilder.py:697`. It creates no node class — it delegates | CERTAIN |
| Bare `range(...)` defaults to `sequential_range`, gated by `trace_time_unroll_builtin_range` | `builtin_range` `@0x81900` reads `self.opts.trace_time_unroll_builtin_range` (both `GetAttr`s present) and on the false branch fetches `self.sequential_range`; matches the verbatim `sequential_range` docstring | CERTAIN |
| `static_range` emits no axis — it fully unrolls at trace time | the iterators `.so` docstring says "fully unroll the loop during NKI kernel tracing"; the `static_range` wrapper `@0x72b40` is thin (only `self` operand, no `create_affine_axis_block` / `LoopVar` / `AxisType` references) | CERTAIN (docstring), HIGH (exact unroll mechanism) |
| The `Axis`-vs-`DynamicAxis` fork is decided by `has_runtime_value` inside `affine_range`, independent of `axis_type` | the `affine_range` generator `@0x240260` (lines 1196–1304) looks up the global `Axis`, does `GetAttrStr(., "has_runtime_value")`, and on the runtime branch looks up `DynamicAxis` instead, before feeding the chosen type into `create_affine_axis_block`; `axis_type` flows separately from `sequential_range`'s call site | CERTAIN |
| `create_affine_axis_block` is the single choke point, with kwargs `{loop_name_id, lb, ub, stride, it, parent, insert_before, axis_type, attrs}` | all nine keys are interned in KernelBuilder `.rodata` alongside the method name and its `__pyx_scope_struct_14_create_affine_axis_block` scope; the `insert_before` sentinel string pins that parameter's handling; the `affine_range` generator references the factory | CERTAIN |

> **GOTCHA — the lowering is in `KernelBuilder.NeuronCodegen`, not `NkiCodegen`.** The file name invites the wrong guess: `starfish/penguin/targets/codegen/NkiCodegen.so` runs the **reverse** direction — it is a BIR→NKI-Python source-text printer (`write_line` / `begin_loop` / `ir_to_nki`), a debug round-tripper. Trace-time NKI→Penguin loop lowering is `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.so`, class `NeuronCodegen`. (KernelBuilder also has a generated twin under `neuronxcc/generated/…/KernelBuilder` — `GeneratedNeuronCodegen` — with identical method names.)

> **NOTE — two `AxisType` enums at two layers; keep them straight.** This page's `AxisType` is the **trace-time NKI-codegen** enum (KernelBuilder), whose members map to `AffineAxis` / `Sequential` / `DynamicAxis` (+ `Default`). The **penguin/IR** `AxisType` documented in [5.3 axis-loop-model](../penguin/axis-loop-model.md) is a *different, richer* enum — `{Default, Parallel, Sequential, Shard, Thread, Block, CCRank, Interleave, SPMD}` — on the `Axis` class hierarchy (`AffineAxis`/`DynamicAxis`/`SequentialAxis` subclasses). The trace-time `Affine` corresponds to the penguin `Parallel`/`AffineAxis` (reorderable static loop); the trace-time `Sequential` corresponds to the penguin `Sequential`; the trace-time `Dynamic` corresponds to the penguin `DynamicAxis`. The extra penguin members (`Shard`/`Thread`/`Block`/`CCRank`/`SPMD`) are SPMD launch-grid roles that come from the dimension model ([6.1.4](./spmd-programming-model.md)), **not** from the three loop iterators on this page. Do not conflate the loop-iterator enum with the grid-axis enum.

### Open questions

- The exact pelican C++ `IRBuilder` method name behind `self.builder.<makeAffineAxisBlock>` — the second `GetAttr` is loaded dynamically — is **[UNRESOLVED]**; the semantics are pinned by the `InstLoop` / `InstDynamicForLoop` mapping above regardless.
- The precise `tripcount == 1` guard for the `"trivial loop"` fast path is **[INFERRED]**: the string and the `nullcontext` / `opt_level` operands are present, but the comparison is not isolated to a single instruction.
- The internal unroll loop of `static_range` (concrete Python iteration replaying the body) is **[INFERRED]** from the thin wrapper plus the docstring; per-iteration emission is not traced instruction-by-instruction.
