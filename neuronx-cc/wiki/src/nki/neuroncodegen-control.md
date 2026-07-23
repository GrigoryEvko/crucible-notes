# NeuronCodegen Control / Scope / Predicate Emitters

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The lowering class lives in `KernelBuilder.cpython-310-x86_64-linux-gnu.so` (Build ID `9eb1020e…`, unstripped, ELF `debug_info`) under `neuronxcc/nki/compiler/backends/neuron/`; the `nl.*` range builtins live in `iterators.cpython-310-x86_64-linux-gnu.so` (Build ID `203a02d9…`) under `neuronxcc/nki/language/`. cp311/cp312 are twins with the same symbol names. Treat every address as version- and ABI-pinned.*

## Abstract

NKI control flow — `for i in nl.affine_range(n)`, `with nl.sequential_range(...)`, `if`/`while` regions, masked writes via `affine_select`, software-pipeline stage markers — is **not** lowered by an MLIR pass or a SASS emitter. It is lowered *at Python trace time*, inside a Cython class called **`NeuronCodegen`**, while the kernel's Python source executes once. Every NKI control construct is a method on that class that calls `self.builder` (the pelican/BIR `IRBuilder` C++ binding) to splice a **Penguin region** or a **LoopAxis** node into the IR under construction, then either yields (for `@contextmanager` scopes) or returns (for ranges and selects).

This page enumerates every control/scope/predicate emitter on `NeuronCodegen`: the four range builtins and the single `affine_range` constructor they all funnel into, the six `ScopeRegion` `@contextmanager`s, the `affine_select` masked write, the three-method predicate-lowering chain, the `pipeline_stage` marker, and the three block-construction primitives the scopes call. The recurring shape is **trampoline → `NeuronCodegen.<method>` → `self.builder.<ctor>` → Penguin region / BIR node**, with the affine-vs-sequential distinction carried as an *attribute* (`AxisType`) on one shared loop node rather than as a separate node class.

> **GOTCHA — `NkiCodegen` runs the other direction.** The name reads like the NKI→Penguin lowering, but `NkiCodegen.so` (under `starfish/penguin/targets/codegen/`) is a **BIR→NKI-source-text printer** — `write_line`, `quote`, `codegenBlock`, `begin_loop`, `simple_predicates`, driven by `ir_to_nki` / `nki_to_nki`. It is a debug round-tripper. The live trace-time lowering is `KernelBuilder.NeuronCodegen.*`, documented here.
>
> A second name collision sits underneath it: `KernelBuilder` ships a generated twin at `neuronxcc/generated/…/KernelBuilder.so` exporting `GeneratedNeuronCodegen`. Every address on this page is from the *non-generated* `nki/compiler/backends/neuron/KernelBuilder.so`, Build ID `9eb1020e…`.

| | |
|---|---|
| **Lowering class** | `NeuronCodegen` (Cython) — `KernelBuilder.so` `nki/compiler/backends/neuron/` |
| **Range builtins** | `iterators.so` — `affine_range`@`0x12230`, `sequential_range`@`0x11c10`, `static_range`@`0x12850`, `sync_program`@`0x12d30` (`pw` entries) |
| **Trampoline target** | global `nki_ctx` → `neuronxcc.nki.compiler.backends.neuron.nki_ctx` → the active `NeuronCodegen` |
| **Loop-axis choke-point** | `NeuronCodegen.create_affine_axis_block` (#55) → `self.builder` → BIR `InstLoop`+`LoopAxis` |
| **Region kinds** (`.rodata`) | `KernelScope` · `LoopScope` · `IfScope` · `WhileScope` · `AllocationScope` · `ScopeRegion` |
| **Axis types** (`.rodata`) | `AxisType.{Affine→AffineAxis, Sequential, Dynamic→DynamicAxis}` |
| **Select primitives** | `affine_select` (#183, `0x18cf70`) → `AffSelTensorScalarOp`; `range_select`/`select`/`tensor_copy_predicated`/`select_reduce` (siblings) |

All `pw` offsets above are read from the cp310 `nm` symbol table (`__pyx_pw_9neuronxcc_3nki_…`).

---

## 1. The trampoline: `nl.range` is `nki_ctx().<name>`

The `nl.affine_range`, `nl.sequential_range`, `nl.static_range`, `nl.sync_program` builtins are **pure dispatch**. Each is a three-line Cython shim in `iterators.so` that fetches the *active codegen context* from a module global and delegates the call. The decompiled body of `__pyx_pw_…_iterators_5affine_range` (`0x12230`):

```c
// neuronxcc/nki/language/iterators.py:46  (affine_range) 
static PyObject *affine_range(PyObject *args, PyObject *kwds) {
    ctx = GetModuleGlobalName("nki_ctx")();    // the active NeuronCodegen
                                               // (str "neuronxcc.nki.compiler.backends.neuron.nki_ctx")
    m   = PyObject_GetAttr(ctx, "affine_range");// bind ctx.affine_range
    return PyObject_Call(m, args, kwds);       // ctx.affine_range(*args, **kwds)
}
```

The identical pattern holds for `sequential_range` (`0x11c10`, delegates `"sequential_range"`, `iterators.py:111`), `static_range` (`0x12850`, `"static_range"`, `iterators.py:23`), and `sync_program` (`0x12d30`, `"sync_program"`, `iterators.py:17`). Two further builtins are *not* loops: `arange` (`0x11430`, an iota descriptor builder) and `ds` (`0x13b30`, the dynamic-slice descriptor); `MGridClass.__getitem__` supplies the SPMD launch grid.

**Consequence:** every loop/range semantic lives in `NeuronCodegen.<name>`, not in `iterators.py`. `nl.affine_range(n)` ≡ `nki_ctx().affine_range(n)`. The string `nki_ctx` and the fully-qualified `neuronxcc.nki.compiler.backends.neuron.nki_ctx` are both present in `iterators.so` `.rodata`, confirming the target module.

---

## 2. The range types → one `affine_range` constructor parameterised by `AxisType`

The four range builtins do **not** produce four kinds of loop. Three of them collapse onto `NeuronCodegen.affine_range`, differing only in the `axis_type` argument; the fourth (`static_range`) produces *no* loop node at all. The `AxisType` enum members are `.rodata` strings in `KernelBuilder.so`: `AffineAxis`, `DynamicAxis`, `Sequential`, and the enum name `AxisType` itself.

### 2.1 `affine_range` (`NeuronCodegen` #71, `pw` `0x8c570`) — the constructor

This is the parallel/reorderable default and the one constructor everything else funnels through. Its body (a `@contextmanager`) sequences, in order — call references from the disassembly:

```c
// NeuronCodegen.affine_range  (pw 0x8c570)
lb, ub, stride, tripcount = normalize_range_args(args)   // canonicalise bounds
attrs                     = extract_loop_directives()    // loop pragmas → axis attrs (§7)
axis_type                 = AxisType.Affine              // (Sequential when reached via §2.2)
if has_runtime_value(...): axis_kind = DynamicAxis       // runtime tripcount
else:                      axis_kind = <static Axis>
block = create_affine_axis_block(loop_name_id, lb, ub, stride, it,    // §6.1 — the choke-point
                                 parent, insert_before, axis_type, attrs)
with loop_scope(predicates=[pred_lt(iv, ub)]):           // §3.1 — LoopScope region
    with allocation_scope():                             // §3.6 — SBUF/PSUM lifetime
        yield LoopVar(iv)                                // the kernel body runs here
// tripcount == 1  ⇒  "trivial_loop" fast-path: no axis node emitted
```

The docstring (in `iterators.so`, verbatim) defines the semantics: *"for use as **parallel** loop iterators … should be the default … when there is **no** loop carried dependency … allows Neuron compiler to perform additional loop-level optimizations, such as loop vectorization."* It also warns the axis does **not** parallelize across NeuronCores — *"different iterations could be parallelized/pipelined on different compute engines."* So **Affine axis = reorderable / vectorizable.**

> **GOTCHA — generator-body offsets belong to the other twin.** Generator-body addresses for `affine_range` in the `0x240260` range (with a `pf` around `0x71…`) come from the IDA sidecar of the *generated* `KernelBuilder.so`, not the shipping one. In the non-generated binary (`9eb1020e…`) the `affine_range` Cython `pw` wrapper entry is `0x8c570`, and the `@contextmanager` generator code is reached through it. Every offset on this page is a `pw` offset from `nm` on the shipping binary.

### 2.2 `sequential_range` (#91, `pw` `0xa7c30`) — `affine_range` with `Sequential`

A thin forwarder. Disassembly name-operands are exactly `{AxisType, Sequential, axis_type, affine_range, name, self}`; the body is literally:

```c
// NeuronCodegen.sequential_range  (pw 0xa7c30)         
return self.affine_range(*args, name=…, axis_type=AxisType.Sequential);
```

Docstring (iterators.so): *"for use as **sequential** loop iterators … should be used when there is a loop carried dependency … informs Neuron compiler to respect inter-loop dependency and perform much more conservative loop-level optimizations compared to affine_range."* So **Sequential axis = ordered / no reorder / no vectorization.** Crucially this is the *same* `LoopAxis` node as the affine case — only the `axis_type` attribute differs; see §2.5.

### 2.3 `static_range` (#89, `pw` `0x72b40`) — full trace-time unroll, NO axis

The body carries only the `self` operand and forwards to the trace-time *fully-unrolled* path: the loop body is replicated `tripcount`× while the kernel traces, so **no Penguin loop region is produced**. Docstring (iterators.so): *"resulting in a fully unrolled loop … fully unroll the loop during NKI kernel tracing … compilation time may go up significantly … fall-back for debugging."* `static_range` materialises iterations rather than emitting a `LoopAxis`.

### 2.4 `builtin_range` (#61, `pw` `0x81900`) — Python `range(...)` inside a kernel

A bare `for i in range(n)` inside a `@nki.jit` kernel maps here. References: `{trace_time_unroll_builtin_range, sequential_range, opts, self}`. Logic:

```c
// NeuronCodegen.builtin_range  (pw 0x81900)            
if (opts.trace_time_unroll_builtin_range)               // opt flag set?
    return <unroll now>;                                //   ≈ static_range path
else
    return self.sequential_range(...);                  //   conservative default
```

This grounds the iterators.so docstring *"any use of Python `range(...)` will be replaced with `sequential_range(...)`"* — i.e. raw `range` is **conservative** (ordered) unless the unroll opt is explicitly enabled. The `trace_time_unroll_builtin_range` string is present in both binaries.

### 2.5 What these produce in BIR (cross-ref 5.14, E16)

`create_affine_axis_block` (§6.1) builds the kwargs `{loop_name_id, lb, ub, stride, it, parent, insert_before, axis_type, attrs}` and hands them to `self.builder`. The split on `has_runtime_value` selects the BIR node:

| `affine_range` outcome | BIR node | Setter | Axis fields |
|---|---|---|---|
| static tripcount, `axis_type ∈ {Affine, Sequential}` | `bir::LoopAxis` (`InstLoop`) | `InstLoop::setAxis(string,l,l,l)` | `name@+0x48`, `lb@+0x20`, `ub@+0x28`, `stride@+0x30`; `toJson` key `"LoopAxis"` |
| runtime tripcount (`has_runtime_value`, `DynamicAxis`) | `bir::DynamicForLoopAxis` (`InstDynamicForLoop`) | `InstDynamicForLoop::setAxis` | `hasRuntimeValue=1`; 3× `QuasiAffineExpr`; `toJson` key `"DynamicForLoopAxis"` |

> **QUIRK — affine vs sequential is an attribute, not a node class.** `affine_range` and `sequential_range` emit the *identical* `InstLoop`+`LoopAxis`. The only difference is the `axis_type` field (`AffineAxis` vs `Sequential`) carried on that node. The backend scheduler / `ColoringAllocatorWithLoop` reads that attribute to decide whether the trip iterations may be reordered — there is no `SequentialLoopAxis` class. Only the static-vs-*dynamic* split (`has_runtime_value`) changes the node class.

---

## 3. The six `ScopeRegion` `@contextmanager`s → Penguin regions

Every scope is a Python `@contextmanager` whose body calls `self.builder.<ctor>(parent, …)` relative to `self.curstmt` (the current insertion statement) to open a **`ScopeRegion`**, `yield`s, and on `__exit__` resumes past the `yield` to close it. The region *kind* is a `.rodata` tag string. `new_scope` (#23, `pw` `0x1e0500`) is the generic opener taking `(kind, payload)`; `new_function_scope` (#34) is its function-boundary variant.

> The five region-kind strings — `KernelScope`, `LoopScope`, `IfScope`, `WhileScope`, `AllocationScope` — plus the generic `ScopeRegion` are all present as exact-match strings in `KernelBuilder.so`. This set was independently confirmed by page 6.1.2; it is reconfirmed here directly via `strings`.

### 3.1 `loop_scope` (#46, `pw` `0xcb240`) — `LoopScope`

The region wrapped around an affine/sequential axis, entered by `affine_range` (§2.1) *after* `create_affine_axis_block`. Kind `"LoopScope"`; pairs enter/exit; carries `predicates` (the `iv < ub` guard built by `pred_lt`). All instructions emitted inside the `for` body land in this region, scoped under the `LoopVar`/`iv`.

### 3.2 `if_scope` (#49, `pw` `0x914b0`; src `KernelBuilder.py:516`) — `IfScope`

Signature `if_scope(self, predicates)`. References `{cur_scope, predicates, new_scope, IfScope}`:

```c
// NeuronCodegen.if_scope(self, predicates)  (pw 0x914b0) 
with self.new_scope(IfScope, predicates=lower_predicates(predicates)):  // §5
    yield
```

The entry condition is the **lowered Penguin predicate** (§5). Every instruction emitted inside the `with` lands in that predicated region. On the BIR side the region guard becomes a sequencer compare-branch — see §5.5.

### 3.3 `while_scope` (#68, `pw` `0x93a50`) — `WhileScope` + `CompileTimeWhileContext`

References `{WhileScope, new_scope, CompileTimeWhileContext, create_while_block, set_continue_condition, continue_loop, block}`:

```c
// NeuronCodegen.while_scope  (pw 0x93a50)                
block = self.create_while_block(...);            // §6.3 → bir While, guard=AlwaysTruePredicate
with self.new_scope(WhileScope, …):
    yield CompileTimeWhileContext(block)         // inner class; kernel sets the guard at trace time
```

The yielded `CompileTimeWhileContext` is a nested class (`NeuronCodegen.while_scope.<locals>.CompileTimeWhileContext`, confirmed by the mangled symbol). Its two methods — `__init__` and `set_continue_condition` (`__pyx_pw_…NeuronCodegen_11while_scope_23CompileTimeWhileContext_{1__init__,3set_continue_condition}`) — let the kernel install the loop-continue predicate at trace time. `set_continue_condition` replaces the block's default `AlwaysTruePredicate` (§6.3) with a real guard expression.

### 3.4 `region_scope` (#74, `pw` `0x91e10`) — public alias for `no_reorder`

References `{no_reorder, region_scope, self}`. The body is `return self.no_reorder(...)` — `region_scope` **is** a no-reorder region (a plain `ScopeRegion` program-order barrier; §4.2). The `__pyx_doc` for #73 region_scope confirms it.

### 3.5 `stage_scope` (#77, `pw` `0xc9dd0`) — pipeline-stage container

References `{builder, curstmt, attrs, parent, allocation_scope, name, block, ScopeRegion}`. Builds a `ScopeRegion` via `self.builder` (`attrs`+`parent`+`name`), **nested inside an `allocation_scope`**, and yields the block. Docstring (verbatim): *"Context manager that creates a scope of pipeline stages."* This is the *container* that holds the individual `pipeline_stage` children (§4.3) — cross-ref 5.14 software-pipelining.

### 3.6 `kernel_scope` (#26, `pw` `0x10dc40`) and `allocation_scope` (#52, `pw` `0x99400`)

`kernel_scope` opens the top-level `KernelScope` Penguin region for one NeuronCore kernel — the Module/Function root the P01–P04 op emitters fill. A `spmd_kernel_scope` (#29) + `post_process_spmd_grid` (#32) pair opens the per-grid-instance region for SPMD launches (strings `spmd_axis_type`, `CompositeSPMDDim`); the m-grid from `MGridClass`/`sync_program` supplies the program axes, and `post_process_spmd_grid` resolves the composite grid dims into partition/replica axes after the body is traced . The grid→axis math itself is **[UNRESOLVED]** here and is deferred to an SPMD-focused page.

`allocation_scope` references `{builder, curstmt, parent, ScopeRegion, name, block}` and tags its `ScopeRegion` as `AllocationScope` — a fresh **tensor-allocation lifetime boundary** fed to `K01 ColoringAllocatorWithLoop`. Docstring: *"Context manager that creates a new tensor allocation scope."* A narrower variant `allocation_region_scope` (#83) opens a sub-region inside it; it carries the `force_auto_alloc` / `skip_allocators` opt control, with the guard string *"opt_level skip_allocators is not compatible with @force_auto_alloc!"*.

---

## 4. `affine_select`, `no_reorder`, `annotate_iter`

### 4.1 `affine_select` (#183, `pw` `0x18cf70`) → `AffSelTensorScalarOp`

A masked/conditional **write** gated by a *single* affine predicate. References: `{AffSelTensorScalarOp, predicates, pred, mask, on_true, on_false, fill_value, is_ge, GE, index_expr, par_indices, free_indices, InstTile, sema, schedule, deps, in_cur_scope, insert, nki_assert, src}`. Lowering:

```c
// NeuronCodegen.affine_select  (pw 0x18cf70)           
nki_assert(len(predicates) == 1,                          // GOTCHA: exactly one predicate
           "'affine_select' only supports single predicate, was provided <N>");
opc = (pred.is_ge ? GE : …);                              // compare opcode from the predicate
op  = AffSelTensorScalarOp(                               // an InstTile (TensorScalar variant)
          src, on_true, on_false_or_fill_value,
          predicate=index_expr(par_indices, free_indices),// iota/index affine compare
          opcode=opc, sema=sema, schedule=schedule, deps=deps);
insert(op, in_cur_scope=True);                            // masked write: out = pred ? on_true(src) : on_false
```

> **GOTCHA — exactly one affine predicate.** `affine_select` `nki_assert`s on `len(predicates) == 1`; a multi-predicate mask raises *"'affine_select' only supports single predicate, was provided N"*. For compound masks the kernel must pre-fold via `lower_compound_predicates` (§5.2) or use a different primitive. The emitted node is an `AffSelTensorScalarOp` — an `InstTile`/TensorScalar whose write is masked by the affine compare; this is the element-level masked write used by the CTE attention mask (iota `q_pos ≥ k_pos` → `_FLOAT32_MIN`).

**Sibling select primitives (distinct, not interchangeable):** `range_select` (#193) is the fp32 dynamic-bounds band-select (forces `scale=1.0`; the SWA/CP path); `tensor_copy_predicated` (#197) is the lazy 0/1-mask predicated copy → BIR `InstCopyPredicated` (IT 52, via `codegenCopyPredicated`); `select` (#195) and `select_reduce` (#199) round out the family. These four are *datapath* selects (deferred to the tensor/memory emitter pages); they are listed here only to contrast with `affine_select`'s single-affine-predicate masked write.

### 4.2 `no_reorder` (#80, `pw` `0x75a00`) → `ScopeRegion` with `no_reorder` attr

A `@contextmanager` program-order barrier. Docstring: *"Context manager that creates a scope where reordering is disabled."* It opens a `ScopeRegion` whose `no_reorder` boolean attribute is set (field docstring: *"no_reorder: Boolean flag indicating if reordering is disabled"*). Inside the region the scheduler must preserve program order — it is the **flat-region twin of the `Sequential` axis_type** (§2.2), applied to an instruction region rather than a loop. `region_scope` (§3.4) is the public alias.

### 4.3 `annotate_iter` (#63, `pw` `0x12a060`) → `DynamicScalar` over loop axes

References `{it, value, name, axes, obj, DynamicScalar, self}`. Binds the loop iteration variable `it` to a **runtime** value: wraps `value` as a `DynamicScalar` over the loop `axes` and annotates the iterator `obj`. This is how a data-dependent index / runtime loop bound attaches to the `LoopVar` so the backend emits a `DynamicForLoopAxis` (§2.5) / `QuasiAffineExpr` instead of a constant. The `DynamicScalar` and `List[DynamicScalar]` strings are present.

### 4.4 `pipeline_stage` (#86, `pw` `0xca810`) — the SW-pipeline stage marker

Signature `pipeline_stage(self, stage_id, execution_order=…)`. References `{stage_id, execution_order}`. It tags the enclosed instructions with a **`PipelineStageDirective`** (§7) carrying `stage_id` (which ring slot / pipeline depth) and `execution_order` (ordering hint). Docstrings (verbatim): *"stage_id: Pipeline stage id for the enclosed instructions"*, *"execution_order: Optional hint for stage execution ordering"*.

> **NOTE — `stage_id` is the K18 ring index.** `pipeline_stage` is the marker the backend SW-pipeline (`K18` ModuloExpr) consumes: `stage_id` ↔ the N-slot buffer ring, where the stage count becomes the denominator in K18's Euclidean `ModuloExpr` (= number of physical buffers). `pipeline_stage` children live inside a `stage_scope` (§3.5) container. Cross-ref 5.14.

---

## 5. Predicate lowering: Python bool → Penguin predicate

A Python condition (or list of conditions) becomes an `int32` mask tile plus a Penguin compare expression, through a three-method chain. The same Python predicate object has **two** BIR realisations depending on whether it guards a *region* (a branch) or a *write* (a mask) — see §5.5.

### 5.1 `lower_predicates` (#191, `pw` `0x11e1b0`) — dispatcher

References `{tile, predicates, pred, mask, np, dtype, int32, lower_simple_predicate, lower_compound_predicates, self}`:

```c
// NeuronCodegen.lower_predicates  (pw 0x11e1b0)         
mask = np.<…>(dtype=int32);                                // allocate the int32 mask tile
if (len(predicates) == 1) lower_simple_predicate(tile, predicates[0]);
else                      lower_compound_predicates(tile, predicates);
return mask;                                               // consumed by if_scope/select
```

### 5.2 `lower_compound_predicates` (#189, `pw` `0xd3130`) — AND-fold

References `{mask, dtype, np, tile, lhs, rhs, op, predicates, pred, binop, logical_and, lower_simple_predicate, self}`. Folds the predicate list with **`np.logical_and`** (conjunction) via `self.binop`: each leaf is lowered by `lower_simple_predicate`, and the accumulator is `lhs = logical_and(lhs, rhs)`. A compound predicate is therefore the **conjunction** of simple predicates. (`logical_and` is the only logical ufunc name in `.rodata` — there is no OR-fold path.)

### 5.3 `lower_simple_predicate` (#187, `pw` `0xd1ac0`) — one comparison → TensorScalar mask

References `{pred, mask, TileIndex, tile, dtype, tensorscalar, tensor, scalar0, reverse0, op0, opcode_for_predicate, index_value_inst, expr, self}`:

```c
// NeuronCodegen.lower_simple_predicate  (pw 0xd1ac0)    
opc  = self.opcode_for_predicate(pred);                    // §5.4 → numpy ufunc opcode
expr = index_value_inst(<affine index over loop iv>);      // materialise iota / TileIndex
mask = self.tensorscalar(TileIndex(expr), scalar0,         // (iota/index)  <cmp>  scalar-bound
                         op0=opc, reverse0=…);              // → int32 boolean mask
```

It materialises the affine index expression (`TileIndex` / `index_value_inst` over the loop `iv`) and compares it to the predicate's scalar bound via a `TensorScalar` op carrying the compare opcode. The underlying expr nodes are the pelican `AffineExpr` family: the `.rodata` names `neuronxcc.starfish.penguin.ir.AffineExpr`, `AffineIV`, and `affine_predicate` are all present.

### 5.4 `opcode_for_predicate` (#185, `pw` `0x7c7d0`) — compare kind → numpy ufunc

References (ordered): `{pred, is_ge, greater_equal, pred, equal, np…}`:

```c
// NeuronCodegen.opcode_for_predicate  (pw 0x7c7d0)      
if (pred.is_ge) return np.greater_equal;                   // GE
…               return np.equal;                           // EQ (+ further lt/gt/ne kinds)
```

> **NOTE — only `greater_equal` and `equal` are exact-match `.rodata` strings.** An exact-match `strings` sweep over `KernelBuilder.so` returns `greater_equal`, `equal`, and `logical_and` as the only comparison/logic ufunc names, which pins the `is_ge → np.greater_equal` and default `→ np.equal` branches. Whether `np.less` / `np.greater` / `np.not_equal` branches exist for the remaining `lt` / `gt` / `ne` predicate kinds is **[INFERRED]**: those kinds exist in the predicate object, but their ufunc operands are not standalone strings in this binary. The two pinned opcodes cover the dominant GE-mask (attention-style) and EQ paths.

### 5.5 The dual realisation — region guard vs masked write

These numpy-ufunc opcodes are the **trace-time stand-ins** for the BIR compare codes. The *same* Python predicate object reaches BIR two ways:

* **Region guard** (`if_scope` / `while_scope`): the lowered predicate becomes a **sequencer compare-branch** — `codegenCmpBranch` / `codegenRegisterAluOp`, realising the BIR `BranchCompareOp` family (`IS_LTIMM`…`IS_GTREG`). `add_predicates` / `AlwaysTruePredicate` are the region-guard side.
* **Masked write** (`affine_select` / `tensor_copy_predicated`): the lowered predicate becomes a **datapath mask** — `CopyPredicated` (`InstCopyPredicated`, IT 52).

`lower_simple_predicate` is the trace-time producer that emits the `TensorScalar`/`CopyPredicated` mask; `opcode_for_predicate`'s ufunc codes are the trace-time stand-ins for the BIR `BranchCompareOp` / `AluOpType` codes.

---

## 6. Block-construction primitives — the node factories the scopes call

All three call `self.builder` (the pelican/BIR `IRBuilder`) relative to `self.curstmt`, build a node, and yield it. A *scope* (§3) = a `create_*_block` factory + `new_scope(region_kind)` enter/exit pair.

### 6.1 `create_affine_axis_block` (#55, `pw` `0x1c0140`) — the LoopAxis factory

The single choke-point where a Python range becomes a Penguin loop-axis node. Builds the kwargs dict (exact key order from disasm):

```c
// NeuronCodegen.create_affine_axis_block  (pw 0x1c0140) 
kwargs = { loop_name_id, lb, ub, stride, it,               // (all strings present in .rodata)
           parent, insert_before, axis_type, attrs };
return self.builder.<makeAffineAxisBlock>(**kwargs);       // → bir InstLoop+LoopAxis (§2.5)
// sentinel: "unexpected case in create affine axis block, insert before should be None"
```

`it` is the induction variable; `insert_before` the Axis/Instruction anchor; `axis_type` ∈ `{Affine, Sequential, Dynamic}`; `attrs` the loop directives (§7). Maps onto E16 `InstLoop::setAxis(string,l,l,l)` → `bir::LoopAxis` (fields `name@+0x48`, `lb@+0x20`, `ub@+0x28`, `stride@+0x30`).

### 6.2 `create_scope_region_block` (#58, `pw` `0x87b50`) — generic ScopeRegion

References `{builder, curstmt, parent, ScopeRegion}`. `self.builder.<makeScopeRegion>(parent, …)` → a generic Penguin `ScopeRegion` block. This is the backing region for `if_scope` / `stage_scope` / `allocation_scope` / `no_reorder` — all of which are `ScopeRegion`s distinguished by a kind tag + attrs.

### 6.3 `create_while_block` (#65, `pw` `0x92790`) — bir `While`, default `AlwaysTruePredicate`

References `{builder, curstmt, guard, parent, insert_before, wrap_expr, sema, AlwaysTruePredicate, While}`:

```c
// NeuronCodegen.create_while_block  (pw 0x92790)        
guard ??= AlwaysTruePredicate;                             // unconditional until set_continue_condition
return self.builder.<makeWhile>(guard, parent, insert_before, sema, …);  // → bir While block
// sentinel: "unexpected case in create while block, insert before should be None"
```

The `guard` defaults to `AlwaysTruePredicate` (the loop runs unconditionally until `set_continue_condition` (§3.3) installs a real guard). `wrap_expr` lifts a Python scalar into the BIR predicate-expr type.

> **NOTE — scopes vs primitives.** `loop_scope`/`if_scope`/`while_scope` are the *context managers*; `create_affine_axis_block`/`create_scope_region_block`/`create_while_block` are the *node factories* they call. A scope = `create_*_block` + `new_scope(region_kind)` enter/exit.

---

## 7. Loop directives: pragmas → axis attrs

`extract_loop_directives` (a module function, `KernelBuilder.extract_loop_directives`) parses kernel loop pragmas into the `attrs` passed to `create_affine_axis_block`. The directive taxonomy, read from the `.rodata` class names:

| Directive class | Error guard / role |
|---|---|
| `AutoPipelineDirective` | *"Multiple auto_pipeline directives are not supported!"* |
| `MultiBufferDirective` | *"Multiple multi_buffer directives are not supported!"* — buffer-ring size |
| `PipelineStageDirective` | the §4.4 `stage_id`/`execution_order` marker — stage count |
| `LexicalScopeDirective` | (`neuronxcc.nki.compiler.backends.neuron.LexicalScopeDirective`) |

Opt fields carried alongside: `OptLevel`/`opt_level`, `skip_allocators`, `accumulated_tripcount`, `tripcount_expr`, `num_stages`, `trace_time_unroll_builtin_range`. The catch-all error is *"Unsupported directive"*. These are precisely the knobs the backend SW-pipeline reads: `MultiBufferDirective` → buffer-ring size; `AutoPipeline`/`PipelineStage` → stage count (the denominator in K18's Euclidean `ModuloExpr` = number of physical buffers); `num_stages`/`accumulated_tripcount` feed the unroll/rotation period. Cross-ref 5.14, and `K01 ColoringAllocatorWithLoop` for the lifetime colouring.

---

## 8. End-to-end: one `affine_range` loop

```text
nl.affine_range(n)                                       (iterators.py:46, pw 0x12230)
 └─ nki_ctx().affine_range(n)                            (trampoline, §1)
     └─ NeuronCodegen.affine_range  (pw 0x8c570)
         1. normalize_range_args(n) → lb, ub, stride, tripcount
         2. attrs = extract_loop_directives()            (§7 pragmas)
         3. axis_type = Affine        (Sequential if reached via sequential_range)
         4. has_runtime_value? → static Axis | DynamicAxis
         5. block = create_affine_axis_block(loop_name_id, lb, ub, stride, it,
                       parent, insert_before, axis_type, attrs)   (§6.1)
                  → self.builder → bir InstLoop+LoopAxis           (or
                                    InstDynamicForLoop+DynamicForLoopAxis if dynamic)
         6. with loop_scope(predicates=[pred_lt(iv,ub)]):          (§3.1)
                with allocation_scope():  yield LoopVar(iv)        (§3.6)
         7. (tripcount==1 ⇒ "trivial_loop" fast-path — no axis node)
Backend: scheduler honours axis_type (Affine=reorderable/vectorizable,
 Sequential=ordered) → K01 ColoringAllocatorWithLoop colours the LoopAxis
 lifetimes → K18 rotates buffer addresses by ModuloExpr(denom = stage count, §7).
```

---

## Method roster (control / scope / predicate)

| # | Method | `pw` offset | Emits |
|---|---|---|---|
| 23 | `new_scope` | `0x1e0500` | generic ScopeRegion opener (kind + payload) |
| 26 | `kernel_scope` | `0x10dc40` | `KernelScope` region (kernel root) |
| 29 | `spmd_kernel_scope` | — | SPMD per-grid kernel region |
| 32 | `post_process_spmd_grid` | — | resolve `CompositeSPMDDim` → axes |
| 46 | `loop_scope` | `0xcb240` | `LoopScope` region |
| 49 | `if_scope` | `0x914b0` | `IfScope` region (lowered predicate guard) |
| 52 | `allocation_scope` | `0x99400` | `AllocationScope` region (tensor lifetime) |
| 55 | `create_affine_axis_block` | `0x1c0140` | `InstLoop`+`LoopAxis` (the choke-point) |
| 58 | `create_scope_region_block` | `0x87b50` | generic `ScopeRegion` |
| 61 | `builtin_range` | `0x81900` | `range(...)` → unroll \| `sequential_range` |
| 63 | `annotate_iter` | `0x12a060` | `DynamicScalar` over loop axes |
| 65 | `create_while_block` | `0x92790` | bir `While` (`AlwaysTruePredicate` default) |
| 68 | `while_scope` | `0x93a50` | `WhileScope` + `CompileTimeWhileContext` |
| 71 | `affine_range` | `0x8c570` | the range constructor (`AxisType.Affine`) |
| 74 | `region_scope` | `0x91e10` | alias of `no_reorder` |
| 77 | `stage_scope` | `0xc9dd0` | pipeline-stage container `ScopeRegion` |
| 80 | `no_reorder` | `0x75a00` | `ScopeRegion` + `no_reorder` attr (barrier) |
| 83 | `allocation_region_scope` | — | narrower `AllocationScope` sub-region |
| 86 | `pipeline_stage` | `0xca810` | `PipelineStageDirective` marker |
| 89 | `static_range` | `0x72b40` | full trace-time unroll (no axis) |
| 91 | `sequential_range` | `0xa7c30` | `affine_range(axis_type=Sequential)` |
| 183 | `affine_select` | `0x18cf70` | `AffSelTensorScalarOp` (`InstTile`) |
| 185 | `opcode_for_predicate` | `0x7c7d0` | numpy ufunc compare opcode |
| 187 | `lower_simple_predicate` | `0xd1ac0` | `TensorScalar` mask over index expr |
| 189 | `lower_compound_predicates` | `0xd3130` | `np.logical_and` AND-fold |
| 191 | `lower_predicates` | `0x11e1b0` | int32 mask dispatcher |

`pw` offsets are cp310 (`KernelBuilder.so`, Build ID `9eb1020e…`); `0x12230` etc. for the range builtins are cp310 `iterators.so` (`203a02d9…`). Offsets without a value (`#29/#32/#83`) are present as symbols but not pinned on this page.

---

## Evidence summary

- **`sequential_range` = `affine_range(axis_type=Sequential)`** — symbol `NeuronCodegen_91sequential_range`@`0xa7c30`, with disasm name-operands `{AxisType, Sequential, axis_type, affine_range}`; `AffineAxis` and `Sequential` are exact `.rodata` strings.
- **The five ScopeRegion kinds are `{Kernel,Loop,If,While,Allocation}Scope`** — all five plus `ScopeRegion` are exact-match strings, matching 6.1.2's set. No sixth kind surfaced.
- **`affine_select` takes exactly one predicate and emits `AffSelTensorScalarOp`** — the guard string *"'affine_select' only supports single predicate, was provided "* and `AffSelTensorScalarOp` are both present; symbol `NeuronCodegen_183affine_select`@`0x18cf70`.
- **The trampoline delegates via `nki_ctx`** — both `nki_ctx` and `neuronxcc.nki.compiler.backends.neuron.nki_ctx` are present in `iterators.so`, and the range builtin `pw` symbols sit at the cited offsets.
- **`opcode_for_predicate` maps `is_ge → np.greater_equal` and defaults to `np.equal`** — `greater_equal`, `equal`, and `logical_and` are the only matching ufunc strings in the binary.

## Limits of this reading

The `lt` / `gt` / `ne` ufunc branches of `opcode_for_predicate` are **[INFERRED]** — the predicate kinds exist, but no matching ufunc name appears as a standalone string (see the NOTE in §5.4).
