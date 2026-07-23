# 6.5.9 — `NkiCodegen`: the penguin.ir → NKI-text RE-EMIT Printer

> **Direction matters.** Pages 6.5.1–6.5.8 document `NeuronCodegen` /
> `GeneratedNeuronCodegen` — the *forward* trace-time builder that turns
> `nl.*`/`nisa.*` Python calls **into** a Penguin IR graph. This page documents
> the exact **inverse**: a separate Cython class, `NkiCodegen`, whose
> `codegen<Op>(inst)` methods walk an already-built Penguin IR graph and **print a
> line of NKI Python source** — a `nisa.<primitive>(...)` or `nl.<primitive>(...)`
> call — back out as text. It is a round-tripper (Penguin IR → NKI source), not a lowering
> path. Everything below is grounded on the cp310 shared object
> `NkiCodegen.cpython-310-x86_64-linux-gnu.so` and its recovered symbols/strings.

The IR on both ends of this printer is **Penguin IR** — it never touches BIR. The
`.so` imports its IR types exclusively from `neuronxcc.starfish.penguin.ir`
(`AffineExpr`, `Operator`, `TileAccess`) and walks `NeuronInst` nodes via
`codegenNeuronOperand` / `codegenNeuronInstResult`; a `strings` sweep finds **zero**
`bir`/`birpy` tokens. The contrast with its sibling is sharp: `BirCodeGenLoop.so` is
full of `birpy.Instruction` / `Opcodes` / `MemoryLocation`, because *that* is the
module which actually produces BIR.

> **GOTCHA — Penguin IR and BIR are different IR levels.** Penguin IR → BIR is its own
> separate codegen crossing (see the [architecture overview](architecture-overview.md)).
> The forward builder `NeuronCodegen` also produces Penguin IR, not BIR, so both halves
> of this round-trip sit *above* the BIR boundary. Reading "codegen" here as "lowering
> to BIR" mis-places the whole page by one IR level.

---

## 0. Where it lives — and where it does *not*

The printer is
`…/starfish/penguin/targets/codegen/NkiCodegen.cpython-310-x86_64-linux-gnu.so`
(4,891,928 B), class
`neuronxcc.starfish.penguin.targets.codegen.NkiCodegen.NkiCodegen`. The
`targets/codegen/` directory is the home of the per-op text codegen; its siblings are
`CodeGenBase.so`, `BirCodeGenLoop.so` (the macro/loop half of the P-strand), and the
package `__init__`.

> **GOTCHA — the printer is *not* in `NKICodeGenFlow.so`.** The name invites the
> mistake, but `…/targets/sunda/NKICodeGenFlow.cpython-310-…so` carries no
> `codegen<Op>` method and no `NkiCodegen` class. Its entire public surface is
> pass orchestration — `optimize_nki_kernel`, `optimize_native_nki_kernel`,
> `construct_nki_opt_passes`, `codegen_nki_opt`, `codegen_nki_opt_allocated` — and
> none of the `codegen<Op>` / `opcode` / `reduce_cmd` / `write_line` strings appear
> in it. It builds and runs the NKI optimisation-pass pipeline; it emits no text.

**Why two classes named almost the same?** `NeuronCodegen` (the forward builder, in
`nki/compiler/backends/neuron/KernelBuilder.so`) and `NkiCodegen` (this printer, in
`targets/codegen/NkiCodegen.so`) are *inverse halves of a round-trip*. There is no
printer class inside `KernelBuilder.so` at all — the forward half is genuinely
build-only, and the print half lives here in its own `.so`.

| Property | Forward builder (6.5.1) | This page — re-emit printer |
|---|---|---|
| Class | `NeuronCodegen` / `GeneratedNeuronCodegen` | `NkiCodegen` |
| Module home | `nki/compiler/backends/neuron/KernelBuilder.so` | `starfish/penguin/targets/codegen/NkiCodegen.so` |
| Direction | NKI Python → Penguin IR | Penguin IR → NKI Python **text** |
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
literal in the `.so`):

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
> conceptual handle for the `@trace`-wrapped re-emit, not a recovered symbol. The
> mechanism is pinned; only the [INFERRED] *name* is not.

### 1.1 `write_line` — the single output primitive

Every emitter ultimately funnels its filled template through one method,
`NkiCodegen.write_line` (qualname + Cython wrapper
`__pyx_pw_…_10NkiCodegen_3write_line` both recovered). This is the
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
`NkiCodegen.write_line` are all confirmed present.

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
  `nl.ndarray(shape=` and `nl.par_dim(` are present).
- **`codegenScalarValue`** — marshals an immediate/scalar payload (used e.g. by
  `memset`'s `value=`).

Common tail kwargs shared by nearly every emitter: `dtype=`, `mask=` (a predicate
list), and where legal `engine=nki.isa.engine.<E>`, `perf_mode=`,
`oob_mode=nki.isa.oob_mode.<m>` (the prefixes `nki.isa.engine.`,
`nki.isa.oob_mode.`, `nki.isa.reduce_cmd.` are all recovered string literals).

---

## 2. The enum-marshalling core — `opcode()` and `reduce_cmd()`

This is the heart of why the printer is the *inverse* of the build-side remaps. The
forward builder renumbers Python enum members into integer opcodes on the way down to
BIR; the printer **name-maps the Penguin IR enum members
(`ALUOpcode`/`ActivationFunctionType`/`EngineAccumulationType`, recovered verbatim
from the `.so`) back to `np.*`/`nl.*`/`scipy.special.*` Python callables**. It never
renumbers — it rewrites a fixed set of member *names*. (The enums it reads are
Penguin-level — `neuronxcc.starfish.penguin.targets.Opcodes` — not `bir::` opcodes;
the printer never sees BIR.)

### 2.1 `opcode(self, op)` — ALUOpcode / ActivationFunctionType → Python callable

Body @ `0xbe630` (symbol `…NkiCodegen_10NkiCodegen_221opcode`, `NkiCodegen.py:1261` per
DWARF `addr2line`). It
tests the enum member name and rewrites a fixed allow-list; everything else passes
through as the bare member name. The recovered marker strings (`expit`, `erf`,
`act_identity`, `abs`, `max`, `min`, `copy`, `sigmoid`, `scipy.special`,
`np.multiply`, `np.sum`) pin the table. The address and the string set are read off
the binary; the exact *branch ordering* below is HIGH, reconstructed from string
proximity rather than a decompiled control-flow dump.

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

Body @ `0x75170` (symbol `…NkiCodegen_10NkiCodegen_45reduce_cmd`). It iterates the
`EngineAccumulationType` enum `members.items()` keyed by `accum_type`/`value` and
emits the member name after the prefix `nki.isa.reduce_cmd.` (the prefix + `accum_type`,
`members`, `items`, `value` are recovered strings). The address and the prefix are
read off the binary; the iteration body itself is HIGH.
The result feeds `reduce_cmd=nki.isa.reduce_cmd.<m>` into `activation_reduce`,
`tensor_scalar_reduce`, `tensor_scalar_cumulative`, `select_reduce`, and
`range_select`.

### 2.3 `dtype()` / `np_dtype()` — type marshalling

`NkiCodegen.dtype` and `NkiCodegen.np_dtype`
(wrapper `__pyx_pw_…_10NkiCodegen_181np_dtype`) emit `np.dtype(...)`/`np.float16`/
`np.float32`/`np.void` and the NKI-extended dtypes `nl.float8_e4m3`,
`nl.float8_e5m2`, `nl.tfloat32` (all recovered literals).

---

## 3. The per-op emitter roster

~33 `codegen<Op>` methods, one per Penguin op family. Every `nisa.<x>` / `nl.<x>`
template below is a **verbatim string literal recovered from the `.so`** (the full
set was dumped with `strings | rg '^(nisa|nl)\.'` and is reproduced faithfully).
Addresses in parentheses resolve to symbols recovered directly from the
cp310 `NkiCodegen.so` with `nm`/`strings` — see §5.

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
selects pointer-scale vs immediate-scale.
- `codegenReciprocalOp` → `dst = nisa.reciprocal(data=<src>, dtype=…, mask=…)`.
 

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
recovered literals. `codegenTensorScalarGEPOp` **delegates** to
`codegenTensorScalarPtrOp` (a GEP-resolved operand reuses the same emit path).

- `codegenTensorScalarCacheReduce` → `nisa.tensor_scalar_reduce(data=…, op0=nl.<o>,
  operand0=…, reduce_op=nl.<r>, reduce_res=…, reverse0=…)`.
- `codegenTensorScalarCacheCumulative` → `nisa.tensor_scalar_cumulative(src=…,
  op0=…, op1=…, imm0=…, imm1=…, reduce_cmd=nki.isa.reduce_cmd.<m>)` — the scan/
  cumulative form.

### 3.3 Reduce family

```python
dst = nisa.tensor_reduce(nl.<reduce-op>, data=<src>, axis=<reduce_indices>,
                         negate=<bool>, dtype=…, mask=…)
dst = nisa.tensor_partition_reduce(nl.<reduce-op>, data=<src>, dtype=…, mask=…)
```

`codegenTensorReduceOp` and `codegenPartitionReduceOp` both take the reduce-op as the
first **positional** `nl.<fn>` arg via `opcode()`. The literals `nisa.tensor_reduce(nl.`,
`nisa.tensor_partition_reduce(nl.`, `axis=`, `negate` are present.
`codegenNeuronReduceMacro` expands cross-tile reductions to `nl.loop_reduce(…)` /
`nl.all_reduce(…)` (literals `nl.loop_reduce(`, `nl.all_reduce(`).

### 3.4 Select / range-select family

- `codegenTensorSelect` → `dst = nl.where(pred, on_true, on_false)`
  (literal `nl.where(`).
- `codegenTensorCopyPredicated` → `nisa.tensor_copy_predicated(src=…)` with a
  `simple_predicates` genexpr building the `[AffinePredicate(...)]` mask list (the
  type hint string `Iterable[AffinePredicate]` is recovered).
- `codegenAffSelTensorScalarOp` → `nisa.affine_select(…)` with `cmp_str`,
  `index_expr`, `fill_value` (all recovered) — an **affine** index predicate, not a
  runtime tensor mask.
- `codegenRangeSelect` → `nisa.range_select(on_true_tile=…, comp_op0=np.<c0>,
  bound0=…, comp_op1=np.<c1>, bound1=…, range_start=…, on_false_value=…,
  fill_value=…)`, plus a `RangeSelectReduce` variant adding `reduce_op`/`reduce_cmd`/
  `reduce_res`. Two compare ops + two bounds form an interval predicate.
- `codegenSelectReduce` → `nisa.select_reduce(dst=…, …, reverse_pred=…, reduce_op=…,
  reduce_cmd=…, reduce_res=…)`.

### 3.5 Top-K / index primitives (DVE)

```python
dst = nisa.max8(src=…, dtype=…, mask=…)                 # codegenSundaMax8
nisa.nc_find_index8(data=…, vals=<max_vals>, …)         # codegenSundaMaxIndex8
nisa.nc_match_replace8(data=…, vals=…, imm=<fill>, …)   # codegenSundaMatchReplace8
nisa.nc_match_replace8(dst_idx=…, vals=…, imm=…)        # codegenMaxIndexAndMatchReplace (fused)
```

The two `nc_match_replace8(...` literals (`data=` and the fused `dst_idx=`) are both
present — the fused index+match-replace variant emits the `dst_idx` kwarg.

### 3.6 BN (Welford) + dropout

```python
dst = nisa.bn_stats(data=<src>, dtype=…, mask=…)   # codegenSundaBNStats
dst = nisa.bn_aggr(data=<src>, dtype=…, mask=…)    # codegenSundaBNAggr
dst = nisa.dropout(data=<src>, prob=<p>, …)        # codegenDropoutMaskInst
```

*Anchors: literals `nisa.bn_stats(data=`, `nisa.bn_aggr(data=`, `nisa.dropout(data=`.*

### 3.7 Data-move / memset / iota / misc

- `codegenTensorCopyOp` → `nisa.tensor_copy(…)` **or** `nl.copy(…)`, chosen by
  engine.
- `codegenDMACopyOp` → `nisa.dma_copy(dst=…/src=…)`; `codegenDMATransposeCopy` →
  `nisa.dma_transpose(…)`.
- `codegenTensorCopyDynamicSrc` / `…DynamicDst` →
  `nisa.tensor_copy_dynamic_src(src=)` / `…_dynamic_dst(dst=)`.
- `codegenBroadcastPartition` → `np.broadcast_to(…)`;
  `codegenStreamShuffleInst` → `nisa.nc_stream_shuffle(src=)`;
  `codegenPoolGather` → `nl.gather_flattened(data=)`;
  `codegenGetSequenceBounds` → `nisa.sequence_bounds(segment_ids=)`.
- `codegenMemsetOp` → `nisa.memset(shape=…, value=<codegenScalarValue>)` —
  random fill can route to `nl.rand`.
- `codegenIndexValueInst` → `nisa.iota(…)`.
- Matmul/transpose (`nisa.nc_matmul(`, `nisa.nc_transpose(`) are present as the
  `codegenMatMulOp` / `codegenTransposeOp` emitters.

> **GOTCHA — FATAL guards on a None destination.** Three emit guards are recovered
> verbatim and will abort code generation rather than print a malformed line:
> `"destination of MemSetOp cannot be None for code generation"`,
> `"destination of DMATransposeStore cannot be None for code generation"`,
> `"destination of SBAtomStore cannot be None for code generation"`. The printer
> refuses to emit a `dst = …` line for an instruction whose result slot was never
> bound.

> **NOTE — `quantize_mx` is *not* here.** The MX-quantize macro emit lives in the
> sibling `BirCodeGenLoop.so` (the macro/loop half of the P-strand), not in this
> per-op printer — the symbol is absent from this `.so` entirely.

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

## 5. Grounding & limits

The cp310 `.so` retains `.debug_info`/`.debug_line`, so the two enum-marshalling
addresses come with source lines. `opcode @0xbe630` is symbol
`…NkiCodegen_10NkiCodegen_221opcode`, which `addr2line` maps to `NkiCodegen.py:1261`
— the `expit` / `act_identity` / `sigmoid` allow-list strings sit in that body.
`reduce_cmd @0x75170` is `…NkiCodegen_10NkiCodegen_45reduce_cmd`, mapping to
`NkiCodegen.py:309`, with the `accum_type` / `members` / `items` iteration strings.
Both are the Cython `__pyx_pw_` entry symbols; no separate `__pyx_pf_` body symbol is
emitted for either.

Every string, qualname, and class-name claim on this page comes off the `.so` itself.
Two things do not reach that standard:

- the **branch ordering** inside `opcode()` — the allow-list membership is pinned by
  the string set, but the order of the tests is reconstructed from string proximity;
- the per-method `codegen<Op>` **body offsets**, which are carried over from an IDA
  pass rather than re-derived here.

> **NOTE — no IDA sidecar DB exists for `NkiCodegen.so`.** IDA exported
> `BirCodeGenLoop`, `CodeGenBase`, and `DumpGraphAndMetadata` from `targets/codegen/`
> but not this one, so the binary itself is the grounding artifact throughout. All
> three wheels do ship the `.so`: cp310 at 4,891,928 B (the artifact the prose is
> keyed to), cp311 at 5,818,104 B, cp312 at 5,885,376 B — the twins are available for
> cross-checking.

---

## See also

- [6.5.1 — NeuronCodegen forward builder](./neuroncodegen-forward-builder.md) — the
  NKI→Penguin builder this printer inverts.
- 6.6.3 — the re-trace path that consumes the printed `@trace` kernel text.
- `BirCodeGenLoop.so` — the macro/loop half of the P-strand (`quantize_mx` lives
  there, not here).
