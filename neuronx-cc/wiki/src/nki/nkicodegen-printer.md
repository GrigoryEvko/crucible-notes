# 6.5.9 — `NkiCodegen`: the penguin.ir → NKI-text RE-EMIT Printer

> **Direction matters.** Pages 6.5.1–6.5.8 document `NeuronCodegen` /
> `GeneratedNeuronCodegen` — the *forward* trace-time builder that turns
> `nl.*`/`nisa.*` Python calls **into** a Penguin/BIR graph. This page documents
> the exact **inverse**: a separate Cython class, `NkiCodegen`, whose
> `codegen<Op>(inst)` methods walk an already-built Penguin graph and **print a
> line of NKI Python source** — a `nisa.<primitive>(...)` or `nl.<primitive>(...)`
> call — back out as text. It is a round-tripper (BIR → NKI source), not a lowering
> path. Everything below is grounded on the cp310 shared object
> `NkiCodegen.cpython-310-x86_64-linux-gnu.so` and its recovered symbols/strings.

---

## 0. Where it lives — and where it does *not*

> **CORRECTION (binary attribution).** The harness task and an early note placed the
> re-emit printer in `…/targets/sunda/NKICodeGenFlow.cpython-310-…so`. That is
> **wrong** and is corrected here in place. `NKICodeGenFlow.so` carries **no**
> `codegen<Op>` printer and **no** `NkiCodegen` class. Its only public symbols
> (`nm -C`) are pass-orchestration entry points:
> `optimize_nki_kernel`, `optimize_native_nki_kernel`, `construct_nki_opt_passes`,
> `codegen_nki_opt`, `codegen_nki_opt_allocated` — i.e. it *constructs and runs the
> NKI optimisation-pass pipeline*, it does not emit NKI text. The verbatim module
> qualnames recovered from it are
> `neuronxcc.starfish.penguin.targets.sunda.NKICodeGenFlow.{optimize_nki_kernel,
> construct_nki_opt_passes,codegen_nki_opt,…}` — no `codegen<Op>`/`opcode`/
> `reduce_cmd`/`write_line` strings are present. [CONFIRMED — `nm`/`strings` on the
> `.so`]
>
> The actual printer is in a **different** shared object:
> `…/starfish/penguin/targets/codegen/NkiCodegen.cpython-310-x86_64-linux-gnu.so`
> (4,891,928 B), class
> `neuronxcc.starfish.penguin.targets.codegen.NkiCodegen.NkiCodegen`. The
> `targets/codegen/` directory is the home of the per-op text codegen; siblings in
> the same dir are `CodeGenBase.so`, `BirCodeGenLoop.so` (the macro/loop half of the
> P-strand), and the package `__init__`. [CONFIRMED — directory listing +
> recovered qualnames]

**Why two classes named almost the same?** `NeuronCodegen` (the forward builder, in
`nki/compiler/backends/neuron/KernelBuilder.so`) and `NkiCodegen` (this printer, in
`targets/codegen/NkiCodegen.so`) are *inverse halves of a round-trip*. Prior pages
6.0.1 and 6.5.1 correctly reported **no printer class inside `KernelBuilder.so`** —
that is expected; the printer was never there. It is here, in its own `.so`.

| Property | Forward builder (6.5.1) | This page — re-emit printer |
|---|---|---|
| Class | `NeuronCodegen` / `GeneratedNeuronCodegen` | `NkiCodegen` |
| Module home | `nki/compiler/backends/neuron/KernelBuilder.so` | `starfish/penguin/targets/codegen/NkiCodegen.so` |
| Direction | NKI Python → Penguin/BIR | Penguin/BIR → NKI Python **text** |
| Per-op method shape | builds `bir::Inst*` via `self.builder` | `printf`-style fills a `nisa.*`/`nl.*` template, calls `write_line` |
| Consumes | `nl.affine_range`/`nisa.activation` calls | already-built `<Op>Inst` nodes |
| Produces | IR nodes | a `@trace`-decorated `def {name}(): …` source module |

> **NOTE — name disambiguation.** A *third* nearby module is
> `starfish/penguin/transforms/NkiCodegenPass.so` (the pass *wrapper* that schedules
> this printer inside the transform pipeline). It is neither the printer class nor
> the flow module. Three distinct artifacts, near-identical names — keep them apart:
> `NkiCodegen` (this printer), `NkiCodegenPass` (transform wrapper),
> `NKICodeGenFlow` (opt-pass orchestration).

---

## 1. The shape of the output — a `@trace` kernel module

`NkiCodegen` does not emit a free-floating instruction list. It emits a **complete,
re-traceable NKI Python kernel**: a fixed import preamble, then a `@trace`-decorated
function whose body is the printed instruction stream. This is what makes it a
*round-tripper* — the text it prints is valid NKI that the frontend (6.6.3) can feed
straight back through the trace path to rebuild the graph.

`codegenImports` emits the preamble verbatim (every line is a recovered string
literal in the `.so`): [CONFIRMED]

```python
import neuronxcc.nki as nki
import neuronxcc.nki.isa as nisa
import neuronxcc.nki.language as nl
import neuronxcc.nki.typing as nt
import numpy as np
from neuronxcc.nki import trace
from neuronxcc.nki.language import par_dim
```

Then `codegenFunctionBegin` opens the function. The two template literals
`@trace` and `def {name}():` are present verbatim, so the emitted skeleton is:

```python
@trace
def {name}():
    # ... one printed line per instruction ...
```

> **GOTCHA — the "`_trace_internal_kernel`" name is not in the binary.** The
> conceptual "re-emit / trace-internal-kernel path" is realised by
> `codegenFunctionBegin` wrapping the body in `@trace` + `def {name}():` plus the
> `from neuronxcc.nki import trace` import — there is **no symbol or string literal
> spelled `_trace_internal_kernel`** in `NkiCodegen.so`. Treat that label as a
> conceptual handle for the `@trace`-wrapped re-emit, not a recovered symbol.
> [INFERRED — name; CONFIRMED — the `@trace`/`def {name}():`/`trace` mechanism]

### 1.1 `write_line` — the single output primitive

Every emitter ultimately funnels its filled template through one method,
`NkiCodegen.write_line` (qualname + Cython wrapper
`__pyx_pw_…_10NkiCodegen_3write_line` both recovered). [CONFIRMED] This is the
`printf` of the printer: an emitter assembles a Python source string (LHS binding +
`nisa.*`/`nl.*` call + tail kwargs) and pushes it as one indented line. The master
walk is:

```c
// __pyx_pf_…_NkiCodegen_codegen           — master per-inst dispatch
// __pyx_pf_…_NkiCodegen_codegenBasicBlock — walks a BB, calls codegen() per inst
// __pyx_pf_…_NkiCodegen_codegenFunctionBegin / _codegenFunctionEnd — @trace wrapper
PyObject *codegen(self, inst) {
    // dispatch by inst type → the matching codegen<Op>(self, inst)
    // each codegen<Op> calls self.write_line(<filled nisa/nl template string>)
}
```

The qualnames `NkiCodegen.codegen`, `NkiCodegen.codegenBasicBlock`,
`NkiCodegen.codegenFunctionBegin`, `NkiCodegen.codegenFunctionEnd`,
`NkiCodegen.write_line` are all confirmed present. [CONFIRMED]

### 1.2 Shared sub-emitters (the operand/result/index plumbing)

Before any op template can be filled, three helpers turn Penguin objects into source
fragments — all confirmed as recovered qualnames:

- **`codegenNeuronOperand`** — a Penguin operand → its NKI source expression (tensor
  handle, scalar, immediate, or an `nl.<dtype>` literal). Every op emitter calls it
  per operand.
- **`codegenNeuronInstResult`** — the LHS `dst = …` binding text.
- **`codegenNeuronAP`** — an AccessPattern → the `[i,j,…]` slice/index text.
- Buffer declarations: `codegenNeuronSBTensor`, `codegenNeuronWeightTensor`,
  `codegenIdentityWeightTensor` → `nl.ndarray(shape=…, dtype=…)` (the literals
  `nl.ndarray(shape=` and `nl.par_dim(` are present). [CONFIRMED]
- **`codegenScalarValue`** — marshals an immediate/scalar payload (used e.g. by
  `memset`'s `value=`).

Common tail kwargs shared by nearly every emitter: `dtype=`, `mask=` (a predicate
list), and where legal `engine=nki.isa.engine.<E>`, `perf_mode=`,
`oob_mode=nki.isa.oob_mode.<m>` (the prefixes `nki.isa.engine.`,
`nki.isa.oob_mode.`, `nki.isa.reduce_cmd.` are all recovered string literals).
[CONFIRMED]

---

## 2. The enum-marshalling core — `opcode()` and `reduce_cmd()`

This is the heart of why the printer is the *inverse* of the build-side remaps. The
forward builder renumbers Python enum members into BIR integer opcodes; the printer
**name-maps BIR enum members back to `np.*`/`nl.*`/`scipy.special.*` Python
callables**. It never renumbers — it rewrites a fixed set of member *names*.

### 2.1 `opcode(self, op)` — ALUOpcode / ActivationFunctionType → Python callable

Body @ `0xbe630` (per the D-P02 IDA pass). It tests the enum member name and rewrites
a fixed allow-list; everything else passes through as the bare member name. The
recovered marker strings (`expit`, `erf`, `act_identity`, `abs`, `max`, `min`,
`copy`, `sigmoid`, `scipy.special`, `np.multiply`, `np.sum`) pin the table:
[CONFIRMED — strings; STRONG — exact branch mapping per D-P02]

| `op.name` | emitted expression | source |
|---|---|---|
| `abs` | `np.abs` | numpy |
| `max` | `np.max` | numpy |
| `min` | `np.min` | numpy |
| `copy` | `copy` (identity move; `nl.copy` at the call site) | — |
| `sigmoid` | `expit` | `from scipy.special import expit` |
| `erf` | `erf` | `from scipy.special import erf` |
| `act_identity` | `act_identity` | imported callable |
| *default* | `op.name` / `op.__qualname__` | e.g. `add`→`np.add`, `mult`→`np.multiply`, and the NKI act-func names `gelu`/`silu`/`exp`/`tanh` passed straight through |

So activation/ALU ops surface in the printed text as ordinary Python math:
`np.add` / `np.multiply` / `np.max` / `np.abs`, `scipy.special.expit`/`erf`, and the
NKI act-func names by their enum name. This is the print-side inverse of the
build-side `codegenAluOp` / act-func remap tables (cross-ref the I-strand
`KlirToBirCodegen` numbering — not repeated here).

### 2.2 `reduce_cmd(self, …)` — accumulate command → `nki.isa.reduce_cmd.<m>`

Body @ `0x75170`. It iterates the `EngineAccumulationType` enum `members.items()`
keyed by `accum_type`/`value` and emits the member name after the prefix
`nki.isa.reduce_cmd.` (the prefix + `accum_type`, `members`, `items`, `value` are
recovered strings). [CONFIRMED — prefix string; STRONG — iteration body per D-P02]
The result feeds `reduce_cmd=nki.isa.reduce_cmd.<m>` into `activation_reduce`,
`tensor_scalar_reduce`, `tensor_scalar_cumulative`, `select_reduce`, and
`range_select`.

### 2.3 `dtype()` / `np_dtype()` — type marshalling

`NkiCodegen.dtype` and `NkiCodegen.np_dtype`
(wrapper `__pyx_pw_…_10NkiCodegen_181np_dtype`) emit `np.dtype(...)`/`np.float16`/
`np.float32`/`np.void` and the NKI-extended dtypes `nl.float8_e4m3`,
`nl.float8_e5m2`, `nl.tfloat32` (all recovered literals). [CONFIRMED]

---

## 3. The per-op emitter roster

~33 `codegen<Op>` methods, one per Penguin op family. Every `nisa.<x>` / `nl.<x>`
template below is a **verbatim string literal recovered from the `.so`** (the full
set was dumped with `strings | rg '^(nisa|nl)\.'` and is reproduced faithfully).
[CONFIRMED] Addresses in parentheses are from the D-P02 IDA pass [report-sourced].

### 3.1 Activation family

`codegenActivationOp` branches on `isinstance(inst, ActivationAccumulationOp)`
(the class name `ActivationAccumulationOp` is a recovered string):

```python
# plain
dst = nisa.activation(op=<opcode(func)>, data=<src>, bias=<bias>,
                      scale=<scale | scale_ptr>, dtype=…, mask=…)
# accumulate
dst = nisa.activation_reduce(op=<opcode(func)>, data=<src>, bias=…, scale=…,
                             reduce_op=np.sum, reduce_res=<accum-dst>, dtype=…, mask=…)
```

The literals `nisa.activation(op=`, `nisa.activation_reduce(op=`, `bias=`,
`reduce_op` (with `np.sum`), `reduce_res`, and `ActivationAccumulationOp` are
present. The `scale` slot has two forms — a second `isinstance` test on `scale_ptr`
selects pointer-scale vs immediate-scale. [CONFIRMED strings]

- `codegenReciprocalOp` → `dst = nisa.reciprocal(data=<src>, dtype=…, mask=…)`.
  [CONFIRMED]

### 3.2 Tensor-scalar / tensor-tensor family

`codegenTensorScalarPtrOp` is the workhorse. It emits one of two primitives, chosen
by the recovered flag `is_scalar_tensor_tensor`:

```python
nisa.tensor_scalar(data=<src>, op0=nl.<o0>, operand0=<s0>, op1=nl.<o1>, operand1=<s1>,
                   reverse0=<b>, reverse1=<b>, engine=…, dtype=…, mask=…)
# OR, when is_scalar_tensor_tensor:
nisa.scalar_tensor_tensor(data=<src>, op0=…, operand0=…, op1=…, operand1=…, …)
```

Two ALU ops (`op0`/`op1`, each via `opcode()`→`nl.<fn>`), two operands
(`operand0`/`operand1`), and the operand-swap flags `reverse0`/`reverse1` are all
recovered literals. [CONFIRMED] `codegenTensorScalarGEPOp` **delegates** to
`codegenTensorScalarPtrOp` (a GEP-resolved operand reuses the same emit path).
[CONFIRMED]

- `codegenTensorScalarCacheReduce` → `nisa.tensor_scalar_reduce(data=…, op0=nl.<o>,
  operand0=…, reduce_op=nl.<r>, reduce_res=…, reverse0=…)`. [CONFIRMED]
- `codegenTensorScalarCacheCumulative` → `nisa.tensor_scalar_cumulative(src=…,
  op0=…, op1=…, imm0=…, imm1=…, reduce_cmd=nki.isa.reduce_cmd.<m>)` — the scan/
  cumulative form. [CONFIRMED]

### 3.3 Reduce family

```python
dst = nisa.tensor_reduce(nl.<reduce-op>, data=<src>, axis=<reduce_indices>,
                         negate=<bool>, dtype=…, mask=…)
dst = nisa.tensor_partition_reduce(nl.<reduce-op>, data=<src>, dtype=…, mask=…)
```

`codegenTensorReduceOp` and `codegenPartitionReduceOp` both take the reduce-op as the
first **positional** `nl.<fn>` arg via `opcode()`. The literals `nisa.tensor_reduce(nl.`,
`nisa.tensor_partition_reduce(nl.`, `axis=`, `negate` are present. [CONFIRMED]
`codegenNeuronReduceMacro` expands cross-tile reductions to `nl.loop_reduce(…)` /
`nl.all_reduce(…)` (literals `nl.loop_reduce(`, `nl.all_reduce(`). [CONFIRMED]

### 3.4 Select / range-select family

- `codegenTensorSelect` → `dst = nl.where(pred, on_true, on_false)`
  (literal `nl.where(`). [CONFIRMED]
- `codegenTensorCopyPredicated` → `nisa.tensor_copy_predicated(src=…)` with a
  `simple_predicates` genexpr building the `[AffinePredicate(...)]` mask list (the
  type hint string `Iterable[AffinePredicate]` is recovered). [CONFIRMED]
- `codegenAffSelTensorScalarOp` → `nisa.affine_select(…)` with `cmp_str`,
  `index_expr`, `fill_value` (all recovered) — an **affine** index predicate, not a
  runtime tensor mask. [CONFIRMED]
- `codegenRangeSelect` → `nisa.range_select(on_true_tile=…, comp_op0=np.<c0>,
  bound0=…, comp_op1=np.<c1>, bound1=…, range_start=…, on_false_value=…,
  fill_value=…)`, plus a `RangeSelectReduce` variant adding `reduce_op`/`reduce_cmd`/
  `reduce_res`. Two compare ops + two bounds form an interval predicate. [CONFIRMED]
- `codegenSelectReduce` → `nisa.select_reduce(dst=…, …, reverse_pred=…, reduce_op=…,
  reduce_cmd=…, reduce_res=…)`. [CONFIRMED]

### 3.5 Top-K / index primitives (DVE)

```python
dst = nisa.max8(src=…, dtype=…, mask=…)                 # codegenSundaMax8
nisa.nc_find_index8(data=…, vals=<max_vals>, …)         # codegenSundaMaxIndex8
nisa.nc_match_replace8(data=…, vals=…, imm=<fill>, …)   # codegenSundaMatchReplace8
nisa.nc_match_replace8(dst_idx=…, vals=…, imm=…)        # codegenMaxIndexAndMatchReplace (fused)
```

The two `nc_match_replace8(...` literals (`data=` and the fused `dst_idx=`) are both
present — the fused index+match-replace variant emits the `dst_idx` kwarg.
[CONFIRMED]

### 3.6 BN (Welford) + dropout

```python
dst = nisa.bn_stats(data=<src>, dtype=…, mask=…)   # codegenSundaBNStats
dst = nisa.bn_aggr(data=<src>, dtype=…, mask=…)    # codegenSundaBNAggr
dst = nisa.dropout(data=<src>, prob=<p>, …)        # codegenDropoutMaskInst
```

[CONFIRMED — literals `nisa.bn_stats(data=`, `nisa.bn_aggr(data=`,
`nisa.dropout(data=`.]

### 3.7 Data-move / memset / iota / misc

- `codegenTensorCopyOp` → `nisa.tensor_copy(…)` **or** `nl.copy(…)`, chosen by
  engine. [CONFIRMED]
- `codegenDMACopyOp` → `nisa.dma_copy(dst=…/src=…)`; `codegenDMATransposeCopy` →
  `nisa.dma_transpose(…)`. [CONFIRMED]
- `codegenTensorCopyDynamicSrc` / `…DynamicDst` →
  `nisa.tensor_copy_dynamic_src(src=)` / `…_dynamic_dst(dst=)`. [CONFIRMED]
- `codegenBroadcastPartition` → `np.broadcast_to(…)`;
  `codegenStreamShuffleInst` → `nisa.nc_stream_shuffle(src=)`;
  `codegenPoolGather` → `nl.gather_flattened(data=)`;
  `codegenGetSequenceBounds` → `nisa.sequence_bounds(segment_ids=)`. [CONFIRMED]
- `codegenMemsetOp` → `nisa.memset(shape=…, value=<codegenScalarValue>)` —
  random fill can route to `nl.rand`. [CONFIRMED]
- `codegenIndexValueInst` → `nisa.iota(…)`. [CONFIRMED]
- Matmul/transpose (`nisa.nc_matmul(`, `nisa.nc_transpose(`) are present as the
  `codegenMatMulOp` / `codegenTransposeOp` emitters. [CONFIRMED]

> **GOTCHA — FATAL guards on a None destination.** Three emit guards are recovered
> verbatim and will abort code generation rather than print a malformed line:
> `"destination of MemSetOp cannot be None for code generation"`,
> `"destination of DMATransposeStore cannot be None for code generation"`,
> `"destination of SBAtomStore cannot be None for code generation"`. The printer
> refuses to emit a `dst = …` line for an instruction whose result slot was never
> bound. [CONFIRMED strings]

> **NOTE — `quantize_mx` is *not* here.** The MX-quantize macro emit lives in the
> sibling `BirCodeGenLoop.so` (the macro/loop half of the P-strand), not in this
> per-op printer. [CONFIRMED — by directory + absence of the symbol in this `.so`]

---

## 4. Why a distinct class at all?

The two directions cannot share one method body because the **enum flow runs
opposite ways**. The forward builder must *parse* Python and *number* BIR enums
(`add` → BIR opcode N); the printer must *un-number* and *re-name* (BIR enum member
→ `np.add`). The two collapse points that the build side merges — e.g. klr
`ScalarTensorTensor` and `TensorScalar` both folding into one BIR `InstTensorScalarPtr`
— are **re-split** by the printer back into two distinct `nisa` names
(`scalar_tensor_tensor` vs `tensor_scalar`) using the carried `is_scalar_tensor_tensor`
flag. A single bidirectional class would have to encode both the merge and the split;
the toolchain instead keeps them as inverse siblings in different shared objects. The
printer's whole reason to exist is **debuggable round-tripping**: dump a Penguin
graph as a runnable `@trace` NKI kernel that 6.6.3 can re-trace.

---

## 5. Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **"The printer class is `NkiCodegen` in `targets/codegen/NkiCodegen.so`, not
   `NKICodeGenFlow`."** — Re-checked both `.so`s. `NKICodeGenFlow.so` qualnames are
   only `optimize_nki_kernel`/`construct_nki_opt_passes`/`codegen_nki_opt` (no
   `codegen<Op>`, no `opcode`, no `write_line`). `NkiCodegen.so` carries the full
   `NkiCodegen.NkiCodegen.codegen<Op>` roster + `opcode`/`reduce_cmd`/`write_line`.
   **HOLDS — task's binary attribution corrected.** [CONFIRMED]
2. **"It prints `nisa.*`/`nl.*` source via a `printf`-style `write_line`."** —
   `NkiCodegen.write_line` qualname + wrapper recovered; the full `nisa.*`/`nl.*`
   template-literal set dumped directly. **HOLDS.** [CONFIRMED]
3. **"Output is a `@trace`-decorated kernel module with a fixed import preamble."**
   — `@trace`, `def {name}():`, the seven import lines, and
   `from neuronxcc.nki import trace` are all recovered. **HOLDS.** [CONFIRMED]
4. **"`opcode()` name-maps `sigmoid`→`expit`, `erf`→`erf`, `abs`→`np.abs`, else the
   member name."** — `expit`, `erf`, `act_identity`, `abs`/`max`/`min`, `copy`,
   `sigmoid`, `scipy.special`, `np.multiply` strings present; exact branch mapping
   per D-P02 IDA body. **HOLDS** (string-confirmed; branch order STRONG).
5. **"There is no `_trace_internal_kernel` symbol."** — `rg` over strings finds
   none. **HOLDS — tagged INFERRED** as a conceptual handle for the `@trace`
   wrapping, not a recovered name.

Anything not byte-pinned in this checkout is tagged: the per-method **addresses**
(`opcode @0xbe630`, `reduce_cmd @0x75170`, the `codegen<Op>` body offsets) are
sourced from the D-P02 IDA pass [report-sourced], since this repo checkout ships the
`NkiCodegen.so` binary and its string/symbol tables but not the decompiled chunk
directory for it; every **string/qualname/class-name** claim was independently
re-verified here against the `.so`. The cp311/cp312 twins exist as IDA exports but
the corresponding `targets/codegen/NkiCodegen.cpython-31{1,2}.so` are not extracted
in this checkout — cp310 is the grounded artifact.

---

## See also

- [6.5.1 — NeuronCodegen forward builder](./neuroncodegen-forward-builder.md) — the
  NKI→Penguin builder this printer inverts.
- 6.6.3 — the re-trace path that consumes the printed `@trace` kernel text.
- `BirCodeGenLoop.so` — the macro/loop half of the P-strand (`quantize_mx` lives
  there, not here).
