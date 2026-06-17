# Worked Example B — flash-attention end-to-end

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 — the Cython `.so` modules under `neuronxcc/nki/`, `neuronxcc/starfish/penguin/`, and the native `libBIR.so`/`libwalrus.so`). Other wheels differ; treat every address as version-pinned. The driving binaries here are unstripped Cython goldmines: `KernelBuilder.cpython-310.so` (14,588,400 B, BuildID `9eb1020e…`, "with debug_info, not stripped") and `BirCodeGenLoop.cpython-310.so` (11,139,256 B, BuildID `cb19fae0…`, debug_info) carry full DWARF mapping methods back to `KernelBuilder.py` / `BirCodeGenLoop.py` source lines.*

## Abstract

This page follows **one program** — a single `@nki.jit` flash-attention context/prefill kernel (`attention_cte`) — from the Python kernel body a user (or `nkilib`) hands to the NKI frontend down to the gzip-wrapped NEFF the runtime loads. It is the companion to [Worked Example A — a matmul end-to-end](worked-example-matmul.md), and it deliberately exercises the part of the compiler the matmul walkthrough does not: the **NKI Python-kernel path**. The matmul enters as XLA HLO and is lowered op-by-op; the flash-attention kernel enters as a *traced Python function*, is **abstract-interpreted** into Penguin IR, and is carried through BIR as a kernel node, not as a graph of primitive ops.

The descent is **not** the textbook "parse → optimize → lower". A traced NKI kernel never becomes an `HloInstruction`; it is run once under a tracing context that records each `nl.*`/`nisa.*` intrinsic as a `penguin.ir.<Op>` node. The path is `@nki.jit` → **NKI trace** (`TraceContext` abstract interpretation under the `nki_ctx` singleton) → **forward build** (`KernelBuilder.NeuronCodegen` constructs `penguin.ir` nodes) → **graph insertion** (`insert_nki_kernel` wraps the traced body into a kernel graph node) → **beta3 codegen** (`BirCodeGenLoop` re-traces/lowers the node straight to `bir::Instruction`) → **BIR** (the online-softmax algorithm: running negated row-max, the `α = exp(m_old − m_new)` rescale, the causal `affine_select` mask) → **walrus** (`walrus_driver` → `libwalrus.so`, ~18 backend passes) → **NEFF**. Two facts shape the whole picture and are easy to get wrong, marked at each stage below.

The reference frame is the matmul walkthrough and [pipeline.md](pipeline.md). If you know that page's six-level IR descent (HLO → Penguin → BIR → per-engine ISA → NEFF) and its **beta3-live / beta2-dormant** split, you already know the spine; this page replaces the HLO front half with the NKI trace front half and shows where the two rejoin (at Penguin IR — pipeline.md's "NKI `@nki.jit` traces in here directly"). The genuinely NKI-specific descent is the first three stages: the abstract-interpreter, the forward builder, and the kernel-graph-node insertion. Everything from BIR down is shared with the matmul.

For reimplementation, the orientation contract is:

- The **six stages** and the single driving symbol that owns each: `TraceKernel.trace` (the `@nki.jit` decorator) → `TraceContext.call` (trace-vs-concrete dispatch) → `NeuronCodegen.<method>` + `self.insert` (`@0x165f00`, → `IRBuilder.add_named_instruction`) → `insert_nki_kernel` (graph node) → `BirCodeGenLoop` `codegenExternalNativeNkiKernel`/`runOnModule` (beta3) → `bir::InstNKIKLIRKernel` (IT56) → `TranslateNKIASTToBIR::run` / `InlineNKIKernel` → NEFF.
- The **online-softmax algorithm in BIR**: the negated running max, the `min`-of-negated-maxes running-max update, the `α = exp(m_old − m_new)` rescale of the accumulator, and the causal `affine_select` iota-triangle mask — all CONFIRMED from `nkilib/core/attention/attention_cte.py`.
- The **three kernel sinks** (`InstBIRKernel` IT54 / `InstNKIKernel` IT55 / `InstNKIKLIRKernel` IT56) and which one a traced NKI kernel lands in versus which one a pre-compiled *library macro* attention lands in — the macro-vs-NKI contrast a reader most often confuses.

| | |
|---|---|
| **Front-door input** | a `@nki.jit`-decorated Python kernel (`attention_cte`, `nkilib/core/attention/attention_cte.py:168`) |
| **The kernel we trace** | prefill flash-attention `O = softmax(scale · Q·Kᵀ) · V`, KV in 8K sections, Q in 128-groups, causal |
| **Trace engine** | `TraceContext.call` (`TraceContext.cpython-310.so` @ `0x2b380`, 546 BBs) under the `TraceContext.global_ctx` singleton |
| **Forward builder** | `KernelBuilder.NeuronCodegen.<method>` → `self.insert` @ `0x165f00` → `penguin.ir.IRBuilder.add_named_instruction` |
| **Graph insertion** | `insert_nki_kernel` (`TraceKernel.cpython-310.so` @ `0x42c20`) → `BIRKernel` / `ExternalNativeNkiKernel` node |
| **BIR codegen (beta3)** | `BirCodeGenLoop.runOnModule` @ `0x975f0` / `codegenExternalNativeNkiKernel` (`_231`) @ `0x1a4e60` |
| **Kernel carrier node** | `bir::InstNKIKLIRKernel` IT56 (ctor `0x40a500`) → `bir::InstNKIKernel` IT55 (ctor `0x40a490`) |
| **Backend** | `WalrusDriver` job → `walrus_driver` → `libwalrus.so`; `TranslateNKIASTToBIR::run` @ `0xf0dbc0` |
| **Wire output** | NEFF — a gzip-wrapped PAX tar of JSON + per-engine `.bin` members |

## Stage map at a glance

The six stages the flash-attention kernel passes through, the IR/form each speaks, the driving symbol there, and the page that owns the full detail. **Live?** marks whether the stage is on the live path of this cp310 build (beta3) or a retained-but-dormant alternative (beta2 KLIR).

| # | Stage | IR / form | Driving symbol (addr) | Live? | Owning deep page |
|---|-------|-----------|------------------------|-------|------------------|
| 0 | `@nki.jit` front door | Python callable → `Kernel` obj | `TraceKernel.trace` (`0x24300`) → `insert_nki_kernel` (`0x42c20`) | ✓ | [6.0.x entrypoints](../nki/) |
| 1 | NKI trace | abstract interpretation | `TraceContext.call` (`0x2b380`); `nki_ctx()` (`0x7700`) | ✓ | [6.1.x TraceContext](../nki/) |
| 2 | Forward build | `penguin.ir.<Op>` nodes | `NeuronCodegen.<method>` + `self.insert` (`0x165f00`) | ✓ | [6.5.x NeuronCodegen](../nki/) |
| 3 | BIR codegen | `bir::Instruction` (TongaISAInst level) | `BirCodeGenLoop.runOnModule` (`0x975f0`); `_231`/`_243` | beta3 ✓ / beta2 ✕ | [6.5.10 BirCodeGenLoop](../nki/) |
| 4 | Kernel carrier + algorithm | `InstNKIKLIRKernel` IT56 → IT55 | ctor `0x40a500`; `TranslateNKIASTToBIR::run` (`0xf0dbc0`) | ✓ | [Part 7 BIR](../bir/) · [6.7.6 attention-CTE](../nki/) |
| 5 | Walrus → NEFF | per-engine ISA → NEFF | `WalrusDriver` → `libwalrus.so` | ✓ | [Part 8 walrus](../walrus/) · [Part 12 formats](../formats/) |

> **GOTCHA — the framework "attention" custom-call is upstream of this kernel, not part of it.** When a framework hands the compiler an HLO attention subgraph, `LowerToCustomNativeKernel` (`hlo-opt` pass #37, `Run` @ `0x1f3eeb0`) recognises the matmul→softmax→matmul idiom and emits an HLO `CustomCall` whose **target name** is the attention variant string (`AttentionMMSoftmaxMM` / `AttentionMMSoftmaxMMWithoutSwap` / `CausalAttentionMMSoftmaxMMWithoutSwap`, all CONFIRMED verbatim in `hlo-opt`'s string table) carried by the `AwsNeuronCustomNativeKernel` class, with a `{psum_shape,sb_shape,auto_cast,auto_cast_type,kernel_name}` `backend_config`. The sibling `DecomposeAttention` (#38, `Run` @ `0x1e9d160`) does the inverse, expanding a native/fused attention back to `dot → −max → exp → ÷Σ → dot` math. **The literal `AwsNeuronNkiKernel` is not a verbatim string in this binary** — the MLP-to-NKI router (#56, `LowerToNKIKernelCC`) emits `AwsNeuronMLPNKI`, and the attention router emits the `Attention*` names above; treat "`AwsNeuronNkiKernel`" as the conceptual *kernel-name slot in `backend_config`*, not a recovered token. That custom-call is how a *framework* program selects this kernel; this page traces the kernel itself, which is a plain `@nki.jit` function.

---

## Stage 0 — Front door: `@nki.jit` → a traced `Kernel`

The flash-attention kernel does **not** enter as XLA HLO. It enters as a **Python callable** decorated `@nki.jit` — `attention_cte` at `nkilib/core/attention/attention_cte.py:168`, a `@nki.jit` kernel (CONFIRMED entry literal). The first act is not optimization; it is wrapping the callable into a kernel object.

`@nki.jit` resolves to `TraceKernel.trace` (`TraceKernel.cpython-310.so` @ `0x24300`, DWARF line `TraceKernel.py:554`). `trace` is a **decorator factory**: its body builds and returns a closure `decorating_function` (`__Pyx_CyFunction_New` @ `0x24583`) — it emits no Penguin IR itself. The user receives that wrapper. `Kernel.__init__` (`0x45830`, py87) captures the callable, its `inspect.signature` (`signature = inspect.signature(func)`), the grid, the `CompileOpts`, and a parent `TraceContext` — so the kernel object carries the py-callable + its `Signature` + grid + opts. (CONFIRMED surface.)

> **NOTE — `@nki.jit` is in `TraceKernel`, not in the `decorators` module.** The compiler-option decorators (`skip_middle_end_transformations`, `enable_stack_allocator`, `update_allocator`, …) live in `decorators.cpython-310.so` and only *clone* a `Kernel` with a `dataclasses.replace`-updated `CompileOpts`; `@nki.jit` appears there solely inside a docstring. Stacking is `@nki.compiler.<option>` (outer) over `@nki.jit` (inner, produces the `Kernel`).

When the wrapper is called, `decorating_function` (`0x1def0`) binds the user `*args/**kwargs` to the kernel signature (it inlines `Kernel.translate_args_kwargs.<locals>.translate` @ `0x1fb10`), then routes through `specialize_and_call` (`0x406e0`, the largest `TraceKernel` method, cache-keyed on concrete shapes/dtypes) into `call_impl` (`0x398b0`) — the real entry that drives the trace. `call_impl` is a compact dispatcher (copies args, enters the trace context, invokes the user `py_func`); the Penguin IR construction is delegated to `NeuronCodegen`/the `IRBuilder`, **not** open-coded here. The trace mechanism is **re-execution under a tracing context**, not a stored AST.

→ [6.0.x NKI entrypoints](../nki/) · companion [Worked Example A](worked-example-matmul.md) · [pipeline.md](pipeline.md) front doors

---

## Stage 1 — NKI trace: abstract interpretation by `TraceContext`

This is the stage with no analogue in the matmul walkthrough. `call_impl` re-runs the kernel body, and every Python call, branch, loop, and return is intercepted by `TraceContext` (`TraceContext.cpython-310.so`; module docstring "the interface for tracing a nki function"). The central dispatcher is `TraceContext.call` (`0x2b380`, 546 basic blocks): for one call site it decides **trace** (record into Penguin IR via the semantic builder `self.sema`) versus **concrete-execute** (run live in Python). Reconstructed dispatch order (CONFIRMED control flow):

```c
// TraceContext.call(self, func, *args, **kwargs)  @0x2b380
function call(self, func, *args, **kwargs):
    if is_nki_func(func):                       // @nki-marked op (nl.*/nisa.*)
        return self.sema.<dispatch>(func, ...)  // → emit a penguin.ir Op
    if getattr(func, "__nki_dont_trace__", 0):  // dont_trace marker (TraceContext.py:336)
        return func(*args, **kwargs)            // concrete
    if isinstance(func, TraceKernel):
        return func.trace(...)                  // nested kernel (sub-kernel inline)
    mod = inspect.getmodule(func)
    if module_in(mod, self._exclude_modules()): // {neuronxcc.nki, ...penguin, numpy, math}
        return func(*args, **kwargs)            // library code → concrete
    result = inline_function(func, *args, ...)  // trace the body (recurse, new fn scope)
    if not has_nki_data(args) and is_nki_data(result):
        raise <"function without nki data as input should not return nki data">
    return result
```

The trace boundary is drawn by `_exclude_modules()` (`TraceContext.py:44`), returning `(neuronxcc.nki, neuronxcc.starfish.penguin, numpy, math)`: callees defined in those run live; user/kernel code is traced. `is_nki_data(v)` (`py328`) — `isinstance(v, (nki_tensor, mask))` — is the predicate separating abstract IR-tensor values from plain Python values.

Two semantic rules matter for flash-attention specifically. **(1) Control flow is statically resolved.** `eval_if_cnd` (`0x28070`) evaluates Python `if`/`while` *at trace time*; the section/group loop structure of `attention_cte` (8K KV sections, 128-Q groups) is unrolled or emitted as loop axes by the iterators (`affine_range`/`sequential_range` emit a deferred loop axis with a `LoopVar`; `static_range` unrolls concretely). A branch whose condition is a symbolic `nki_tensor`/`ScalarPredicate` is rejected (`err_dynamic_control_flow_not_sup`) — which is exactly why the kernel's *causal* masking is expressed as tensor predicate ops (`affine_select`/`range_select`), not as Python `if`. **(2) The ambient builder is a singleton.** Every `nl.*` intrinsic in the body calls `nki_ctx()` (`nki_ctx.cpython-310.so` @ `0x7700`) to fetch `TraceContext.global_ctx.sema` (the semantic builder) and `get_cur_scope()` (`0x6f60`) for the active emission scope. `global_ctx` is installed once per trace by `TraceContext.new_ctx` (`0x24340`) and cleared on teardown — so a post-trace `nl.*` correctly raises `err_nki_api_outside_of_nki_kernel`.

> **QUIRK — Cython obscures the exact `sema.<dispatch>` per intrinsic.** `TraceContext.call`'s control flow and the `_exclude_modules` tuple are CONFIRMED from decompiled control flow + interned strings, but the precise `self.sema.<method>` reached for a given `nl.*` op is routed through the module-state struct and is recovered at the *next* layer (Stage 2: which `NeuronCodegen` method, which `penguin.ir.<Op>`), not inside `call` itself. The trace *decision* is CONFIRMED; the per-op *target* is read in Stage 2.

→ [6.1.x TraceContext](../nki/) · the SPMD grid model (`program_id`/`num_programs`, `ProgramId`) and the `affine_range`/`sequential_range`/`static_range` unroll-vs-axis distinction.

---

## Stage 2 — Forward build: `NeuronCodegen` constructs `penguin.ir` nodes

The semantic builder `sema` is `KernelBuilder.NeuronCodegen` — the canonical **forward builder** (`KernelBuilder.cpython-310.so`, unstripped with debug_info; single public class `NeuronCodegen`, 194 method-defs). Each traced intrinsic lands on a `NeuronCodegen` method that (a) parses kwargs, (b) validates, (c) normalizes operand tiles via `self.combine_tiles` (`0x16ad90`), (d) reads the `AccessPattern` off the tile (`tile.canonical_par_indices` / `canonical_free_indices`), (e) constructs **one** `penguin.ir.<Op>` Python node, and (f) appends it via `self.insert` (`0x165f00`):

```c
// the NeuronCodegen builder pattern (every tensor-op method) — self.insert @0x165f00
function insert(self, Op, buffer, name, deps, sema, mask, ...):
    builder.add_named_instruction(inst)   // penguin.ir.IRBuilder.add_named_instruction
    add_predicates(...)                   // the `mask` predicate list
    process_dep_edges(...)                // the `deps` dependency edges
    process_new_neuroninst(...)           // NeuronCodegen bookkeeping
    update_debugloc(...)                  // stamp source location
```

So the "`IRBuilder.add_named_instruction → penguin.ir.<Op>`" the descent needs is the ctor-then-insert pair: `add_named_instruction` is reached *through* `self.insert`. The flash-attention body maps onto a small, CONFIRMED set of these methods (each builds the named `penguin.ir.<Op>`):

| Kernel step (attention_cte) | `NeuronCodegen` method | `penguin.ir.<Op>` | Anchor |
|---|---|---|---|
| `Q·Kᵀ` (MM1) / `P·V` (MM2) | `matmult` / `nc_matmul` (P01) | `MatMulOp` | P01 §2.4 |
| section row-max (negated) | `tensorreduce` (op=max, **`negate=True`**) | `TensorReduceOp` | `0x239de0`, py2098 |
| causal diagonal mask | `affine_select` | `AffSelTensorScalarOp` | `0x18cf70`, py2312 |
| dynamic CP/SWA/prefix mask | `range_select` | `RangeSelect`/`RangeSelectReduce` | `0x1544c0`, py2414 |
| `exp(S − max)` + running sum | `activation` (op=`exp`, **`bias=−max`**, `reduce_op=add`) | `ActivationOp` | `0x1968c0`, py2882 |
| running-sum `α·l_old + l_section` | `tensorscalar` / `tensorvectorscalar` | `TensorScalarPtrOp` | `0x140530`, py3066 |
| `O_acc = α·O_prev + O_section` | `tensorvectorscalar` (scalar-tensor-tensor) | `TensorScalarPtrOp` | `0x293fd0`, py2733 |
| `1 / l` reciprocal | `reciprocal` (→ `simple_unary`) | `ReciprocalOp` | `0xe9270`, py3269 |
| K/V/exp DMA + transpose | `dma_copy`/`dma_transpose` (P03) | `DMACopy`/`TransposeOps` | P03 |

The three op-selector enums (`ALUOpcode`/`np.ufunc`, `TSOpcode`, `EngineAccumulationType`) are stored **verbatim** on the Op node — the builder does **not** renumber them. The `.rodata` type-hints pin it exactly: ALU op = `Union[np.ufunc, ALUOpcode]`, tensor-scalar op = `TSOpcode`, accumulate cmd = `Optional[EngineAccumulationType]` (default `Idle`, reset member `ResetReduce`). Numeric ISA renumbering is deferred one layer down (Stage 3, `BirCodeGenLoop` `codegenAluOp`/`codegenAccumCmd`). This is the key to reading the negated-max trick at the IR level: `tensorreduce(op=max, negate=True)` produces a `TensorReduceOp` whose `negate` flag is the hardware free-axis "reduce-then-negate", and `activation(op=exp, bias=mm1_running_max)` folds the subtraction into the activation's additive bias — both are single Penguin nodes, faithfully carrying the algorithm of Stage 4.

When the trace finishes, the just-built `penguin.ir` kernel is stitched into the host graph by `insert_nki_kernel` (`TraceKernel.cpython-310.so` @ `0x42c20`). It builds a `BIRKernel` graph node (the producer form of the IT54 carrier; class from `penguin.ir.ir`), binds each shared-`hbm` input/output via `add_parameter` (operand types `TensorRef`/`MemrefTileND`/`ProgramId`/`DynamicScalar`), seals the body via `addBasicBlock`/`finalize_exec`, and validates every operand is in `shared_hbm` address space (CONFIRMED guard string "The input and output tensor of a NKI kernel must be in shared_hbm address space"). It selects **`ExternalNativeNkiKernel`** for a fresh user trace vs **`InternalMaterializedNkiKernel`** for a registry/`_private_kernels` leaf with cached weights — the producer-side twins of the Stage-3 consumers.

> **NOTE — this is where the NKI path rejoins the matmul path.** After Stage 2 the kernel is `penguin.ir` — exactly [pipeline.md](pipeline.md)'s "NKI `@nki.jit` traces in here directly" arrow into Penguin IR. From here down the IR levels (Penguin → BIR → per-engine ISA → NEFF) are shared with the matmul; the NKI-specific machinery is the kernel-node wrapping, which keeps the whole traced body as *one node* rather than dissolving it into the host op graph.

→ [6.5.x NeuronCodegen forward build](../nki/) · the `self.insert`/`combine_tiles`/AccessPattern surface · the IT54 producer node (`insert_nki_kernel`).

---

## Stage 3 — BIR codegen: `BirCodeGenLoop` (beta3) lowers the kernel node

The kernel node is consumed by `BirCodeGenLoop.cpython-310.so` — the **beta3** Penguin-direct-to-BIR codegen (banner: "BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level"). The module driver is `runOnModule` (`_319` @ `0x975f0`, `BirCodeGenLoop.py:4010`), a thin fan-out `for f in cu.subgraph_functions: f.runOnFunction()`; `runOnFunction` (`_321` @ `0xadb40`) delegates the body-walk to `run_with_exception_handling` (the upstream visitor), which fires `beforeStmtTransform` (per-function prologue: `curModule.addFunction` creates the `bir::Function`, opens the `"Block1"` entry BB, wires IO via `ensureDstOnFunction`, installs the identity-matmul weight) → `transformAxis`/`codegenLoop` (the loop-nest walk: `codegenLoop` emits `BirAxis`+`InstLoop` and opens the `"block0"` body BB; `transformAxis` loops `axis.stmts` calling `stmt.transform(self)`) → `transformInstruction` (`_295` @ `0xd5c40`, the per-ISA-inst finalizer: `addInstToBir` + id/loopnest/engine/predicate stamping + dependency-edge wiring, `EdgeKind ∈ {Flow, Output, Anti, Ordered}`). Each Penguin op maps to a small fixed set of `bir::Instruction`s **at the TongaISAInst granularity** (one BIR inst per Trainium "Tonga" ISA instruction).

The NKI kernel node specifically is handled by the **NKI frontend bridge** inside `BirCodeGenLoop` — the cluster of methods that take a *whole* NKI kernel and re-trace/wrap it:

- **External** (a `@nki.jit` user kernel already traced): `codegenExternalNativeNkiKernel` (`_231` @ `0x1a4e60`) builds a BIR `NKIKernel` (binds `set_func`/`set_func_args`/`set_func_outs`, `set_sb_buf_shape`/`set_psum_buf_shape`, grid, `addSeqAccess`); the KLIR-carrier variant `codegenExternalNativeNkiKlirKernel` (`_233` @ `0x137c40`) builds a BIR `NKIKLIRKernel` and gates on `kernel_format == "bir"` (`set_klir_binary`/`set_kernel_format`/`set_nki_binary_version_identifier`).
- **Internal** (a registry/`_private_kernels` leaf — e.g. when the framework custom-call from Stage-map's GOTCHA selected an `nkilib` attention kernel by `kernel_name`): `codegenInternalNativeNkiKernel` (`_243` @ `0x08d630`, the largest bridge method, 27,664 B) orchestrates: `_resolve_kernel_config` (registry lookup by `func_name`) → cache lookup (`_7`) → `_trace_internal_kernel_to_new_nki_frontend` (`_241`) → cache store (`_9`) → build the BIR `NKIKLIRKernel` inst.

> **GOTCHA — the dispatcher is beta3-only in this build, despite the docstring.** `_trace_internal_kernel_to_new_nki_frontend`'s docstring describes a two-way env branch (`NKI_FRONTEND ∈ {"beta2","beta3"}`, default beta2). The **compiled cp310 body does not branch** — it hard-calls `self._trace_kernel_beta3` (decompiled line 168; `__pyx_n_s_trace_kernel_beta2` is referenced only in the StringTab init, never loaded by any runtime body). So **beta3 = `ParserFrontend.compile_to_bir` → format `'bir'` is the LIVE path**, and **beta2 = `TraceKernel.trace` → format `'klir'` → `KlirToBirCodegen` is retained but DORMANT** for the internal-kernel path — exactly [pipeline.md](pipeline.md)'s beta3-live/beta2-dormant split, mechanized at the per-kernel level. The `NKI_FRONTEND` env read still happens (in `_243`) but no longer selects the trace path. (`NKI_BETA3_FE`/`NEW_NKI_FE` are `KernelCategory` *metric enum members*, not env vars.)

Both bridge paths converge on a BIR `NKIKLIRKernel` instruction. That inst, plus the algorithm it carries, is Stage 4.

→ [6.5.10 BirCodeGenLoop driver](../nki/) · [6.6.1 the three-sink model](../nki/) · the dual-frontend keystone (beta2/beta3).

---

## Stage 4 — BIR: the kernel carrier node and the online-softmax algorithm

### 4a — The carrier node and its lowering to a call-site

The `NKIKLIRKernel` BIR inst is `bir::InstNKIKLIRKernel` — **IT56**, the frontend **carrier** (`libBIR.so`, ctor `0x40a500`, `readFieldsFromJson` `0x434870`, `toJson` `0x43a990`, all transcribed). It carries the kernel in one of two on-disk forms selected by its `kernel_format` field (`@+0x110`): a serialized KLR-AST binary (FORM A, `kernel_format != "bir"`) or an embedded BIR-JSON blob (FORM B, `== "bir"`), with the file path at `klir_binary` (`@+0xF0`), the name-keyed argument maps `func_args`/`func_outs` (`@+0x178`/`+0x1A8`, `std::map<string,string>`), the scratch hints `sb_buf_shape`/`psum_buf_shape`, the version (`nki_binary_version_identifier` `@+0x130`), and the address-rotation scope. It is fully round-trippable; its `sameInst` is `__assert_fail "Not Implemented"` (IT56 nodes are never CSE'd).

In the backend, `TranslateNKIASTToBIR::run` (`libwalrus.so` @ `0xf0dbc0`, walrus pre-codegen pass order 2) walks every BasicBlock, finds each IT56 node, and via `lowerKernelInst` (`0xf0b610`) dispatches on `kernel_format`: FORM A deserializes the KLR file (`klr::*_des`) and `KlirToBirCodegen::codegenLncKernel` (`0xf34140`) walks the KLR AST into a fresh NKI `bir::Function`, then `lowerKLIRToNKI` mints an `InstNKIKernel` call-site with `KernelSource = Beta2`; FORM B runs the two-pass BIR-JSON loader (`Function::createFromJson`) and `lowerFromBirJson` mints the call-site with `KernelSource = Beta3`. Either way the result is a separate NKI `bir::Function` + a `bir::InstNKIKernel` (**IT55**, ctor `0x40a490`) call-site (`getFunc` `@+0xF0`, the two resolved `FunctionArgumentMap`s `@+0xF8`/`+0x120`, `kernel_source_val` `@+0x160` ∈ `{Beta1=0,Beta2=1,Beta3=2}`), and the IT56 placeholder is removed. IT55 is later inline-expanded by `InlineNKIKernel` (splice the NKI Fn body into the caller, rebind operands through the two arg-maps, remove the IT55 + the NKI Fn), leaving the caller filled with the leaf BIR opcodes.

> **GOTCHA — the macro-attention path is a DIFFERENT sink (IT54).** A *pre-compiled BIR library kernel* — a hand-written/library attention/quant/rmsnorm op that arrives **already as BIR** — is `bir::InstBIRKernel` (**IT54**, ctor `0x409ee0`, `toJson` `0x439ed0`, with a **real** structural `sameInst` `0x2f66a0`). It is self-describing: `kernel_name`, `srcs_shape`/`dsts_shape`, an opaque `kernel_attrs` JSON, plus `auto_cast`/`quant_kernel`/`norm_type`/`fused_rmsnorm`/**`is_causal`**/`lnc_size`/`output_layout`/`store_add`/`lower_bound`. It is expanded by `InlineBIRKernel` via `BIRKernelWrapper` — **no JSON round-trip, no KLR walk** — because its body is already BIR. **Use the IT to tell the two attentions apart:** a traced `@nki.jit` flash-attention rides IT56→IT55 (carrier → call-site, re-traced/lowered); a library/macro attention rides IT54 (a finished BIR node, just inlined). The version triplet `ulib_to_*_version` is on neither of these — it belongs to `InstCustomOp` (IT53); IT54's kernel-version surrogate is `lnc_size` + the shape/attr fields, and IT56 carries `nki_binary_version_identifier`.

### 4b — The flash-attention algorithm inside the BIR body

The IT55 NKI `bir::Function` body is the online-softmax algorithm, CONFIRMED from `nkilib/core/attention/attention_cte.py` (2920 lines, readable NKI Python). The mathematical target is `O = softmax(scale · Q·Kᵀ) · V` for prefill, with the KV axis processed in **sections** (8K-token flash-attn outer blocks, threshold `10·1024`) and running per-row statistics carried across sections:

```text
per Q-group g (128-wide = SBUF partition dim), per KV section (512/2048-col tiles):
  MM1     : nc_matmul(mm1_psum, stat=q_sb[d,q], mov=k_sb[d,k])     (Q·Kᵀ; d on partitions)
  MASK    : affine_select (causal) | range_select (CP/SWA/prefix)  → upper-tri → _FLOAT32_MIN
  SCALE+MAX: tensor_scalar_reduce(×scale, reduce=maximum)          → mm1_partial_max
  SECT MAX: tensor_reduce(maximum, negate=True)                    → mm1_section_max = −max(S)
  RUN MAX : running = min(running, section)                        // min of NEGATED maxes
            α = exp(prev_running + running) = exp(max_old − max_new) // flash rescale
  EXP+SUM : activation(exp, data=S, bias=mm1_running_max, reduce=add→exp_partial_sum) → exp_sb
  TRANSPOSE: dma_transpose exp_sb → exp_tp_sb [128,4,128]          // KV onto partitions
  MM2     : nc_matmul(mm2_psum, stat=exp_tp_sb[kv,q], mov=v_sb[kv,d])  (P·V)
  RUN SUM : exp_running_sum = α·prev + exp_section_sum             // l_new = α·l_old + l_section
  X-SECT  : scalar_tensor_tensor(O_acc = α·O_prev + O_section)     // rescale accumulator
  NORMALIZE: reciprocal(exp_running_sum); activation(copy, scale=1/l) → final O
```

The three subtleties a reimplementer must reproduce, all CONFIRMED:

1. **The negated-max representation.** Row-max is stored *negated* throughout: `tensor_reduce(..., negate=True)` on the partial maxes (line 2135) yields `mm1_section_max = −max(S)`. This lets two things fall out of cheap ops: (a) the running-max update becomes `min` of two *negated* maxes — `min(−max_old, −max_new) = −max(max_old, max_new) = −max_new` (line 2156) — and (b) the exp subtraction folds into the activation's additive **bias**: `exp(S − max) ≡ activation(exp, data=S, bias=(−max))`, where the bias operand *is* the stored negated running max (line 2236, `bias=mm1_running_max`). No separate subtract instruction is emitted.

2. **The `α = exp(m_old − m_new)` rescale.** When a new section lowers the max, the correction factor is `flash_attn_correction_factor[g] = exp(prev_mm1_running_max + mm1_running_max)` = `exp((+max_old) + (−max_new)) = exp(max_old − max_new)` (line 2162). It rescales both the running denominator (`l_new = α·l_old + l_section`, line 2402, via `tensor_scalar` op0=multiply/op1=add) and the partial output accumulator (`O_acc = α·O_prev + O_section`, line 2488, via `scalar_tensor_tensor`). The partial `O` is round-tripped through HBM between sections (the SBUF-budget trade-off; running max and sum stay resident, `O` does not).

3. **The causal `affine_select` iota-triangle mask.** Compute-skipping drops strictly-upper-triangle tiles entirely (`_has_any_compute_causal`: skip a KV tile when the largest Q index in the group is below the smallest K index — ~½ the QKᵀ/PV work eliminated). The single *diagonal* tile that straddles the boundary is partially masked: `affine_select(out, pattern=[[-1,num_f]], offset=qkmax_grp·128 − k_start_pos, channel_multiplier=1, cmp_op=greater_equal, on_true=scores, on_false=_FLOAT32_MIN)` (lines 2687–2699). The predicate `channel·1 + free·(−1) + offset ≥ 0` resolves to `q_pos ≥ k_pos`; the upper triangle is set to `_FLOAT32_MIN = −3.4e38` so `exp → 0`. Sliding-window adds a *second* `affine_select` with the lower-diagonal predicate (one `affine_select` cannot express the AND of two triangular masks); CP/sequence-packing/prefix-prior use the dynamic `range_select` with fp32 bounds (which forces `scale = 1.0`, so those callers pre-scale Q).

→ [Part 7 — BIR](../bir/) (the `bir::Instruction` model, the IT54/55/56 carrier structs) · [6.7.6 the attention-CTE kernel](../nki/) (the full `attention_cte.py` algorithm, tiling, masking, GQA) · the `MatMulOp`/`ActivationOp`/`TensorReduceOp` Penguin→BIR codegens.

---

## Stage 5 — Walrus → NEFF: the shared backend tail

From here the flash-attention kernel's BIR is identical in kind to the matmul's BIR, and the descent is the one [pipeline.md](pipeline.md) and [Worked Example A](worked-example-matmul.md) own. The `WalrusDriver` job (a **single** Job, per pipeline.md's GOTCHA) forks `walrus_driver` → `libwalrus.so` and forwards its ~18 backend stages as `--walrus-passes`: `TranslateNKIASTToBIR` (pass order 2, the IT56→IT55 lowering of Stage 4a) runs early, followed by the lowering/legalization/scheduling/allocation chain (`lsa`, `coloring`, `unroll`, `lower_ac`, `pre_sched`, `partitioner`, `bir_splitter`, the SBUF/PSUM coloring allocator or the user-space stack allocator, `dma_optimization`, anti-dependency analysis, `vn_splitter`, `post_sched`, `tensorcopy_accel`, `dep_reduction`, `print_metrics`, and finally `birverifier`/`bir_sim`). The `InlineNKIKernel` expansion (Stage 4a) splices the NKI `bir::Function` body into the caller so the flash-attention loop nest becomes ordinary BIR the allocator and scheduler can place onto the PE / Pool / Activation / DVE / SP engines as 64-byte bundles. The NEFF packager (in `libwalrus`, with `Kelper`/`NeffWrapper` on the relevant flows) writes the gzip-wrapped PAX tar of JSON + per-engine `.bin` members.

> **NOTE — the engine choices for the flash-attention atoms are made here, not in the trace.** `nc_matmul` (MM1/MM2) lands on the PE array; the masked-softmax atoms (`affine_select`/`range_select`/`tensor_reduce`/`activation`-exp/`reciprocal`) land on the Pool/Activation/DVE engines; the K/V/exp DMA + transposes on the SP/DMA path. The trace and the Penguin/BIR layers fixed the *algorithm and access patterns*; walrus fixes *which engine and which cycle*. The negated-max bias-folded-exp keeps the exp+sum a single Activation-engine pass — the algorithmic choice from Stage 4b is what lets the backend pack it tightly.

→ [Part 8 — walrus](../walrus/) (the `--walrus-passes` pipeline, allocation, scheduling) · [Part 12 — formats](../formats/) (NEFF / KELF wire format) · [pipeline.md](pipeline.md) (the `WalrusDriver` single-job model).

---

## Reading the rest of the book from here

| If you want… | Start at |
|---|---|
| The `@nki.jit`/`TraceKernel` entry and `insert_nki_kernel` graph insertion | [6.0.x NKI entrypoints](../nki/) |
| The `TraceContext` abstract interpreter (trace-vs-concrete, control flow, SPMD grid) | [6.1.x TraceContext](../nki/) |
| The `NeuronCodegen` forward builder and the full method→`penguin.ir.<Op>` map | [6.5.x NeuronCodegen](../nki/) |
| The beta3 `BirCodeGenLoop` driver and the beta2/beta3 dispatch keystone | [6.5.10 BirCodeGenLoop](../nki/) · [6.6.1 three-sink model](../nki/) |
| The `attention_cte` flash-attention algorithm in full (online softmax, tiling, masking, GQA) | [6.7.6 attention-CTE kernel](../nki/) |
| The IT54/55/56 kernel-carrier node structs and their serializers | [Part 7 — BIR](../bir/) |
| The walrus backend passes, allocation, and scheduling | [Part 8 — walrus](../walrus/) |
| The NEFF / KELF wire format | [Part 12 — formats](../formats/) |
| The matmul (HLO-front) version of this same descent | [Worked Example A](worked-example-matmul.md) |
| The job schedule and IR-descent overview both worked examples sit under | [pipeline.md](pipeline.md) |

## Cross-References

- [Worked Example A — a matmul end-to-end](worked-example-matmul.md) — the companion that traces the XLA-HLO front-door path; this page is its NKI-Python-front-door twin, sharing the BIR→walrus→NEFF tail.
- [pipeline.md — The Compile Pipeline at a Glance](pipeline.md) — the driver job order and the six-level IR descent; the beta3-live/beta2-dormant split this page mechanizes per-kernel.
- [6.0.x NKI entrypoints](../nki/) — `@nki.jit`/`TraceKernel.trace`, `insert_nki_kernel`, the `External`/`Internal` graph-node split (Stage 0/2).
- [6.1.x TraceContext](../nki/) — the abstract-interpretation engine `TraceContext.call`, `nki_ctx`, the SPMD grid model (Stage 1).
- [6.5.x NeuronCodegen](../nki/) — the canonical forward builder, `self.insert`→`add_named_instruction`, the method→`penguin.ir.<Op>` map (Stage 2).
- [6.5.10 BirCodeGenLoop](../nki/) — the beta3 Penguin→BIR driver and the NKI frontend bridge (Stage 3).
- [6.6.1 the three-sink model](../nki/) — `InstBIRKernel` (IT54) vs `InstNKIKernel` (IT55) vs `InstNKIKLIRKernel` (IT56), the macro-vs-NKI distinction (Stage 4a).
- [6.7.6 the attention-CTE kernel](../nki/) — `attention_cte.py`, the online-softmax algorithm, tiling, masking, GQA (Stage 4b).
- [Part 7 — BIR](../bir/) — the `bir::Instruction` data model and the kernel-carrier node structs/serializers.
- [Part 8 — walrus](../walrus/) — the `walrus_driver` backend passes, `TranslateNKIASTToBIR`, allocation, scheduling (Stage 5).
- [Part 12 — formats](../formats/) — the NEFF / KELF wire format the descent terminates at.
