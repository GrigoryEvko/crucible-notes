# `nki.language` (`nl.*`) Op Dispatch Surface

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/nki/language/` and `neuronxcc/starfish/penguin/native_maths`). Other wheels differ; treat every symbol as version-pinned. Mangled names below are read directly from the per-function decompiled C and the `__pyx_n_s_*` interned-name table inside each `__pyx_pw_*` body — there is no IDA project for these five Cython modules, so grounding is decompile + symbol-table + `.rodata` string.*

## Abstract

`nl.*` — the `nki.language` namespace — is the **high-level op surface** a kernel author writes: `nl.exp`, `nl.add`, `nl.gelu`, `nl.sum`, `nl.softmax`, `nl.matmul`, `nl.zeros`, `nl.load`. It is *not* where any hardware instruction is decided. Every `nl` op is a Cython `def` that compiles to one native wrapper `__pyx_pw_9neuronxcc_3nki_8language_8math_ops_<N><name>`, whose semantic tail is a tiny sequence: fetch a **module-global dispatcher**, fetch an **op-token** (a NumPy ufunc, a `native_maths.<fn>` callable, or a SciPy `expit`), merge all args into one kwargs dict, and `tp_call`. The dtype/engine *legality* — which engine runs it, which dtypes are legal, what the partition/free limits are — lives one layer down in `nki.isa` (the `nisa` intrinsics, [6.4.1](./isa-compute-intrinsics.md) / [6.4.2](./isa-reduce-dve-dma.md)) and `sema`. `nl` is a **thin op-name + op-token veneer over `nisa`**.

The whole of `math_ops` reduces to **four module-global dispatchers** plus a handful of direct `nisa` passthroughs and two traced composites:

| Family | Dispatcher (module-global) | Op-token kind | Lowers to (nisa) |
|---|---|---|---|
| **A · unary** | `unary_op(x, op, dtype, mask)` | NumPy ufunc (`np.exp`, …) | `tensor_scalar` / `activation` |
| **B · binary** | `binary_op(x, y, op, …)` / `binop` / `bitwise_op` | NumPy ufunc (`np.add`, …) | `tensor_tensor` |
| **C · activation** | `activation(op, data, dtype, mask)` | `native_maths.<fn>` / `expit` | `activation` (Scalar engine ACT_FN) |
| **D · reduce** | `tensor_reduce(x, op, axis, …)` | NumPy reduce ufunc (`np.add`, …) | `tensor_reduce` (Vector, free-axis) |

Everything else is either a **1:1 `nisa` passthrough** (`reciprocal`→`nisa.reciprocal`, `transpose`→`nisa.nc_transpose`, `matmul`→`nc_transpose`+`nc_matmul`, `dropout`→`nisa.n_dropout`), a **`nki_ctx` build-context call** (creation/memory ops mint memref/DMA nodes, never compute), or a **traced composite** (`softmax`, `rms_norm`, `var`) that expands into several `nl` ops.

Because every dispatch is a Python attribute/global lookup resolved at runtime, **the native call-graph shows no `nl`→`nisa` edges** — they are all indirect through `tp_getattro`/`tp_call`. The mapping on this page is recovered from the interned `__pyx_n_s_*` names referenced in each wrapper, which is CONFIRMED-grade for *which* global/attr is fetched.

| | |
|---|---|
| **Module** | `neuronxcc/nki/language/math_ops.cpython-310-*.so` (~2.0 MB, 74 `pw` bodies) |
| **Sibling modules** | `creation_ops` · `memory_ops` · `shape_manipulation_ops` · `indexing_ops` |
| **Reference math lib** | `neuronxcc/starfish/penguin/native_maths.cpython-310-*.so` (activation + composite decompositions) |
| **Dispatcher binding** | `unary_op`/`binary_op`/`binop`/`activation`/`tensor_reduce` bound at import from `compiler.backends.neuron.{tensor,nki_ctx}` + `nki.isa` |
| **Wrapper mangling** | `__pyx_pw_9neuronxcc_3nki_8language_8math_ops_<N><name>` (`<N>` = Cython def-ordinal, odd = visible API) |

---

## 1 · The Cython wrapper shape (how to read a body)

Each public `nl` op compiles to exactly one native wrapper. The def-ordinal `<N>` is the Cython visibility key: **odd ordinals are visible API functions; even ordinals are the `__defaults__` argument tuples** Cython emits alongside. The body is ~90% boilerplate — `PyDict_New`, `__Pyx_GetKwValue_FASTCALL`, `__Pyx_ParseOptionalKeywords` unpacking the call's `*args`/`**kwargs` against the signature. The **semantic tail** is a short, recognizable sequence of three primitives:

```c
// Cython lowering pattern for every nl math op (schematic, from the pw decompile)
PyObject *g  = __Pyx__GetModuleGlobalName(__pyx_n_s_<dispatcher>); // unary_op / activation / tensor_reduce / nisa / nki_ctx
PyObject *op = __Pyx_PyObject_GetAttrStr(np_module, __pyx_n_s_<token>); // np.exp  OR  native_maths.gelu  OR  nisa.nc_matmul
// ... build one merged kwargs dict {x, op, dtype, mask, ...} ...
PyObject *r  = __Pyx_PyObject_Call(g, empty_args, kwargs);          // tp_call the dispatcher
return r;
```

> **NOTE — the no-underscore workhorse.** The interned name fetched by `nl.exp` is **`unary_op_2`** (`__pyx_n_s_unary_op_2`), not `unary_op`. The leading-underscore `_unary_op` (`pw` ordinal 3) is the back-compat *public* shim; the no-underscore `unary_op` global (interned as `unary_op_2` to disambiguate from the local `_unary_op` symbol) is the real `compiler.backends.neuron.tensor` elementwise builder. Same for `binary_op_2` in the binary family. Verified: `exp`'s body fetches `np`, `exp`, `unary_op_2`, `dtype`, `mask`, `x` and nothing else.

The dispatchers themselves (`unary_op`, `binary_op`, `binop`, `activation`, `tensor_reduce`) have **no `pw` symbol in `math_ops`** — they are module-global slots bound at import. They resolve into the `compiler.backends.neuron.tensor` builder and `nki.isa.neuron_isa`, and ultimately call the wave-1 `nisa` intrinsics: `activation`→`sema.activation`, `tensor_tensor`/`tensor_scalar`→`sema.tensorvectorscalar`/`sema.tensortensor`, `tensor_reduce`→`sema.tensorreduce`, `nc_matmul`→`sema.matmult`.

---

## 2 · The complete def-ordinal map (`math_ops`)

Every one of these 74 `pw` symbols was confirmed present in the decompile (`__pyx_pw_9neuronxcc_3nki_8language_8math_ops_<N><name>`). Ordinals are exact:

| N | name | N | name | N | name | N | name |
|---|---|---|---|---|---|---|---|
| 1 | `_binary_op` | 39 | `abs` | 77 | `gelu_apprx_tanh` | 115 | `var` |
| 3 | `_unary_op` | 41 | `negative` | 79 | `gelu_apprx_sigmoid` | 117 | `all` |
| 5 | `_is_integer` | 43 | `sign` | 81 | `gelu_apprx_sigmoid_dx` | 119 | `all_reduce` |
| 7 | `_bitwise_op` | 45 | `trunc` | 83 | `gelu_dx` | 121 | `loop_reduce` |
| 9 | `add` | 47 | `floor` | 85 | `silu` | 123 | `equal` |
| 11 | `subtract` | 49 | `ceil` | 87 | `silu_dx` | 125 | `not_equal` |
| 13 | `multiply` | 51 | `exp` | 89 | `erf` | 127 | `greater` |
| 15 | `divide` | 53 | `log` | 91 | `erf_dx` | 129 | `greater_equal` |
| 17 | `maximum` | 55 | `cos` | 93 | `softplus` | 131 | `less` |
| 19 | `minimum` | 57 | `sin` | 95 | `mish` | 133 | `less_equal` |
| 21 | `power` | 59 | `tan` | 97 | `square` | 135 | `logical_and` |
| 23 | `fmod` | 61 | `tanh` | 99 | `softmax` | 137 | `logical_or` |
| 25 | `mod` | 63 | `arctan` | 101 | `rms_norm` | 139 | `logical_xor` |
| 27 | `bitwise_and` | 65 | `sqrt` | 103 | `_rms_norm_matmul` | 141 | `logical_not` |
| 29 | `bitwise_or` | 67 | `rsqrt` | 105 | `max` | 143 | `dropout` |
| 31 | `bitwise_xor` | 69 | `reciprocal` | 107 | `min` | 145 | `matmul` |
| 33 | `invert` | 71 | `sigmoid` | 109 | `sum` | 147 | `transpose` |
| 35 | `left_shift` | 73 | `relu` | 111 | `prod` | | |
| 37 | `right_shift` | 75 | `gelu` | 113 | `mean` | | |

---

## 3 · Family A — elementwise UNARY → `unary_op(x, op=np.<ufunc>)`

Each unary op body fetches the NumPy ufunc by name and forwards it as the `op` token to the `unary_op` builder. The op-token **is** the ufunc object; the builder's op-table keys off ufunc identity to decide whether the op maps to a Scalar-engine ACT_FN (e.g. `exp`, `tanh`) or a Vector `tensor_scalar` (e.g. `abs`, `floor`, `ceil`, `trunc`).

```c
// e.g. exp (ordinal 51) — verified n_s: {np, exp, unary_op_2, x, dtype, mask}
PyObject *op_fn = getattr(np, "exp");
return unary_op(x, op=op_fn, dtype=dtype, mask=mask);
```

**CONFIRMED op→ufunc** (verbatim `__pyx_n_s_<name>` in each body): `exp`→`np.exp`, `log`→`np.log`, `cos`→`np.cos`, `sin`→`np.sin`, `tan`→`np.tan`, `tanh`→`np.tanh`, `sqrt`→`np.sqrt`, `abs`→`np.abs`, `negative`→`np.negative`, `floor`→`np.floor`, `ceil`→`np.ceil`, `trunc`→`np.trunc`. `arctan`/`sign` follow the same path. `logical_not`(141) and `invert`(33) also route through `unary_op` with `op=np.logical_not`/`np.invert`.

> **QUIRK — `square` is *not* unary_op.** `square`(97) is a hybrid: its body fetches **both** `np.square` *and* the `activation` global (`__pyx_n_s_activation` + `__pyx_n_s_np` + `__pyx_n_s_square`). It lowers to `activation(op=square, data=x)` — the Scalar-engine **square activation** — **not** `tensor_tensor(x, x)` and not `tensor_scalar`. This keeps `x²` on the ACT unit (one instruction) rather than spending a Vector multiply. CONFIRMED.

> **STRONG — the op-table is not here.** The exact ufunc→(Scalar-activation vs Vector-tensor_scalar) routing lives in `compiler.backends.neuron.tensor` (the `Tile` dunder-op machinery), only partially read. The op-*token* fetched by each `nl` body is CONFIRMED; the builder's internal routing is STRONG.

---

## 4 · Family B — elementwise BINARY → `binary_op(x, y, op=np.<ufunc>)`

```c
// e.g. add (ordinal 9) / equal (ordinal 123) — body fetches binary_op_2 + np + <name>
PyObject *op_fn = getattr(np, "add");
return binary_op(x, y, op=op_fn, dtype=dtype, mask=mask);   // → nisa.tensor_tensor
```

**Arithmetic** (CONFIRMED `binary_op_2` + `np` + `<name>`): `add`→`np.add`, `subtract`→`np.subtract`, `multiply`→`np.multiply`, `maximum`→`np.maximum`, `minimum`→`np.minimum`, `power`→`np.power`, `fmod`→`np.fmod`, `mod`→`np.mod`. All lower to `nisa.tensor_tensor(data1=x, data2=y, op=op_fn)` on the **Vector** engine (`power`/int-arith may route to GpSimd per the [6.4.1](./isa-compute-intrinsics.md) engine table).

**Compare / logical** (`equal`…`less_equal` 123–133, `logical_and`…`logical_xor` 135–139): same `binary_op` path with `op=np.<cmp>`; verified `equal` fetches `binary_op_2` + `np` + `equal`. Output dtype defaults to `bool`.

**Bitwise** (`bitwise_and`/`or`/`xor` 27–31, `left`/`right_shift` 35–37): route through the dedicated `bitwise_op` global. Verified `bitwise_and`'s body fetches `bitwise_op` + `np` + `bitwise_and`.

> **CORRECTION — the integer-dtype assert is *inside* `bitwise_op`, not the wrapper.** The backing report (§2.2) states the bitwise wrappers "assert integer dtype via the shared `_is_integer`(5) predicate (`n_s_is_integer` in the bitwise bodies)." This is wrong at the wrapper level: `bitwise_and`(27)'s interned-name set is exactly `{bitwise_op, np, bitwise_and, x, y, dtype, mask}` — **no `is_integer`**. The integer-dtype gate (→ `err_bitvec_operand_must_be_integer`, cf. [6.4.1](./isa-compute-intrinsics.md) `_check_tensor_scalar_bitvec_dtype`) is enforced one layer down inside the `bitwise_op` global / `tensor_tensor` legality, not in the per-op `pw` body.

> **GOTCHA — `divide` carries an extra tile-validity assert.** `divide`(15) is special: its body references `binop` + `np` + `divide` + `sema` + `nki_assert` + `tile` (CONFIRMED). It calls `binop(x, y, op=np.divide, …)` (a `tensor_tensor` variant) **and** runs a `sema.nki_assert` tile-legality check on the operands. It is a Vector `tensor_tensor` divide with a guard — **not** a HW reciprocal-multiply.

---

## 5 · Family C — ACTIVATION unit → `activation(op=native_maths.<fn>, data=x)`

These ops are **not** NumPy ufuncs. The op-token is fetched from the `native_maths` module-global (= `neuronxcc.starfish.penguin.native_maths`) and passed to the `activation` dispatcher, which emits a single **Scalar-engine activation** instruction (`nisa.activation`→`sema.activation`). The `native_maths.<fn>` callable is *both* the HW-ACT_FN identity token *and* a NumPy reference impl used for golden/sim and as the multi-instruction fallback on arches lacking the HW unit.

```c
// e.g. gelu (ordinal 75) — verified n_s: {activation, native_maths, gelu, x, dtype, mask}
PyObject *af = getattr(native_maths, "gelu");
return activation(op=af, data=x, dtype=dtype, mask=mask);
```

**CONFIRMED** (`activation` + `native_maths` + `<name>` in each body): `gelu`→`native_maths.gelu`, `silu`→`native_maths.silu`, `relu`→`native_maths.relu`, `erf`→`native_maths.erf`, `softplus`→`native_maths.softplus`, `mish`→`native_maths.mish`, `rsqrt`→`native_maths.rsqrt`. The approximation/derivative variants `gelu_apprx_tanh`(77), `gelu_apprx_sigmoid`(79), `gelu_apprx_sigmoid_dx`(81), `gelu_dx`(83), `silu_dx`(87), `erf_dx`(91) all take `native_maths.<that exact name>` — all six are present as `native_maths` `pw` exports.

> **QUIRK — `sigmoid` uses SciPy `expit`, not `native_maths.sigmoid`.** `sigmoid`(71)'s body fetches `__pyx_n_s_expit` (verified: interned set `{activation, expit, x, dtype, mask}` — no `native_maths`). It calls `activation(op=expit, data=x)`, mapping the SciPy `expit` identity to the HW sigmoid ACT_FN. There is no `native_maths.sigmoid` token in the path.

> **NOTE — `reciprocal` skips this family.** `reciprocal`(69) is a **direct `nisa` passthrough**: body = `{nisa, reciprocal, x, dtype, mask}` → `nisa.reciprocal(x)`, the dedicated Vector reciprocal intrinsic. It does **not** go through `activation` or `native_maths`.

**Activation scale/bias defaults** (from the `nisa.activation` layer, [6.4.1](./isa-compute-intrinsics.md) `_check_activation_*`): `activation(op, data, bias=None, scale=1.0)`; `scale` dtype must be FP32, `bias` dtype ∈ {FP32, FP16, BF16}.

### `native_maths` reference decompositions (the op-token bodies)

Each `native_maths.<fn>` is a NumPy/NKI reference that (a) keys HW ACT_FN selection and (b) is the multi-op fallback when no HW unit exists. Bodies confirmed from the interned-name table of each `native_maths` `pw`:

| `native_maths.<fn>` | ordinal | decomposition | interned names confirmed |
|---|---|---|---|
| `gelu` | 3 | `0.5·x·(1 + erf(x/√2))` | `erf, sqrt, np, x` |
| `silu` | 19 | `x · expit(x)` (swish) | (from §4 family; `expit`) |
| `relu` | 1 | `where(x>0, x, 0)` | (matches NkiCodegen relu emit helper) |
| `softplus` | 27 | `log1p(exp(x))` | `exp, log1p` |
| `mish` | 29 | `x · tanh(softplus(x))` | `tanh, exp, log1p` |
| `rsqrt` | 23 | `1 / sqrt(x)` | `sqrt` |

`native_maths` also exports `softmax`(39), `softmax_exp`(41), `softmax_rsum`(43), `softmax_dx`(45), `rms_norm`(47), `rms_norm_multidim`(49), and the full shift/conv/attention reference family — these back the composites in §8.

> **NOTE — `dropout` is its own `nisa` intrinsic.** `dropout`(143)'s body = `{n_dropout, rate, x, dtype, mask}` → `nisa.n_dropout(x, rate=…)`, the HW PRNG drop intrinsic. It is **not** a `native_maths` decomposition. (The backing report's claim that `dropout` references `n_s_numpy`/`n_s_add` does not hold — its interned set is exactly the four names above plus `x`.)

---

## 6 · Family D — REDUCTIONS → `tensor_reduce(x, op=np.<reduce>, axis, keepdims)`

```c
// e.g. sum (ordinal 109) — verified n_s: {tensor_reduce, np, add, axis, keepdims, x, dtype, mask}
PyObject *rf = getattr(np, "add");
return tensor_reduce(x, op=rf, axis=axis, keepdims=keepdims, dtype=dtype, mask=mask);
```

`tensor_reduce` is the module-global → `nisa.tensor_reduce` (IR node `tensorreduce`, [6.4.2](./isa-reduce-dve-dma.md)) on the **Vector** engine, reducing over the **free axis only** — partition dim 0 cannot be reduced, and the axis must be the last contiguous dim(s).

**CONFIRMED op→ufunc**: `max`→`np.max`, `min`→`np.min`, `sum`→`np.add`, `prod`→`np.multiply`, `mean`→`np.mean`, `all`→`np.all` (logical reduce). `keepdims` (default False) is forwarded in every body.

> **CORRECTION — only `mean` promotes integer accumulators in its own body.** The backing report (§2.4) states "sum/prod/mean call `_is_integer`(5) … and, if the input dtype is integer, FORCE the accumulator to `np.float32`." Direct check of the three `pw` bodies disproves this for two of them:
>
> | op | `is_integer` in body? | `float32` in body? |
> |---|---|---|
> | `sum`(109) | **no** | no |
> | `prod`(111) | **no** | no |
> | `mean`(113) | **yes** | **yes** |
>
> Only **`mean`(113)** carries the explicit `is_integer`→`np.float32` accumulator promotion in its wrapper (its interned set includes both `is_integer` and `float32`). `sum`/`prod` forward the raw ufunc with no fp32 forcing at the `nl` layer; any integer-overflow accumulation policy for them is decided inside the `tensor_reduce` builder / the reduce instruction, not the `nl` wrapper. `mean`'s divide-by-N is the reduce instruction's **mean reduce-command**, not a separate `nl.divide`.

> **GOTCHA — `var` is a four-op composite.** `var`(115)'s body fetches `mean` + `subtract` + `power` (CONFIRMED — no single HW variance instruction). It expands to:
> ```python
> m   = mean(x, axis)        # tensor_reduce(np.mean)
> d   = subtract(x, m)       # binary_op(np.subtract), broadcast
> d2  = power(d, 2)          # binary_op(np.power)
> var = mean(d2, axis)       # tensor_reduce(np.mean) again
> # = mean((x - mean(x))**2)
> ```

### Cross-SPMD / loop reductions

```c
// all_reduce (119) — verified n_s: {nki_ctx, loopreduce, op, program_axes, parallel_reduce, asynchronous, x, dtype, mask}
return nki_ctx.loopreduce(x, op, program_reduce_axes=program_axes,
                          parallel_reduce=parallel_reduce, asynchronous=asynchronous, ...);

// loop_reduce (121) — verified n_s: {nki_ctx, loopreduce, op, loop_indices, x, dtype, mask}
return nki_ctx.loopreduce(x, op, loop_indices=loop_indices, dtype=dtype, mask=mask);
```

`all_reduce`(119) reduces a tile across multiple SPMD program instances (the `program_axes`); `loop_reduce`(121) accumulates a reduction across loop-carried iterations (e.g. `nl.loop_reduce(a, op=np.add, loop_indices=[k_i])` for a perf reduce-over-k). Both bind the **same** `nki_ctx.loopreduce` build-context method, differing only in whether the reduce axis is a program axis (SPMD, gated by `parallel_reduce` default True / `asynchronous` default False) or a Python loop index (sequential). CONFIRMED.

---

## 7 · Matmul / transpose — direct Tensor-Engine passthrough

```c
// matmul (145) — verified n_s: {nisa, nc_transpose, nc_matmul, vector, engine, transpose_x, x, y, mask}
def matmul(x, y, *, transpose_x=False, mask=None):
    if not transpose_x:
        xT = nisa.nc_transpose(x, engine=nisa.engine.vector)  # insert transpose on the VECTOR engine
    else:
        xT = x                                                # x already x.T (perf path)
    return nisa.nc_matmul(xT, y, mask=mask)                   # x.T @ y on the PE array (SBUF in, PSUM out)
```

`nc_matmul` computes `x.T @ y` on the Tensor (PE) Engine. The crucial detail confirmed by the interned set is that the **default inserted transpose runs on the VECTOR engine** (`__pyx_n_s_vector` + `__pyx_n_s_engine` in the body), **not** the PE-array identity-matmul transpose — so a plain `nl.matmul(x, y)` spends one Vector transpose plus one PE matmul. `transpose_x=True` elides the transpose. Shape limits (from `nisa` layer): `x` par≤128 / free≤128; `y` par≤128 / free≤512 (PSUM free-dim limit).

```c
// transpose (147) — verified n_s: {nc_transpose} only
return nisa.nc_transpose(x, dtype, mask);
```

`transpose`(147) is a bare 1:1 `nc_transpose` (default PE-engine transpose via the identity matmul) — the standalone tile par↔free swap, and the **only** shape op that emits a real instruction (data movement is unavoidable for a partition/free transpose).

---

## 8 · Composite traced kernels — `softmax`, `rms_norm`

These two `nl` ops are *not* dispatchers; their `pw` bodies do a legality check and then call a `@trace` sub-kernel bound at import from the `native_maths` family.

```c
// softmax (99) — verified n_s: {check_free_axis, softmax_kernel, axis, compute_dtype, x, dtype, mask}
def softmax(x, axis, *, dtype=None, compute_dtype=None, mask=None):
    check_free_axis(axis)                      # axis must be free, last-contiguous
    return softmax_kernel(x, axis, dtype=dtype, compute_dtype=compute_dtype, mask=mask)

// rms_norm (101) — verified n_s: {check_free_axis, rmsnorm_kernel, w, epsilon, compute_dtype, x, axis, n, dtype, mask}
def rms_norm(x, w, axis, n, epsilon=1e-06, *, dtype=None, compute_dtype=None, mask=None):
    check_free_axis(axis)
    return rmsnorm_kernel(x, w, axis, n, epsilon, dtype=..., compute_dtype=..., mask=...)
```

`softmax_kernel` / `rmsnorm_kernel` are module-level `__pyx_v_*` slots, bound at import to the `native_maths` softmax/rms_norm reference impls — **not** inlined into the `pw` body. `_rms_norm_matmul`(103) is a private fused rms_norm+matmul variant (omitted from the public stubs).

### What they actually lower to (from the `native_maths` reference bodies)

```python
# native_maths.softmax (39) — verified n_s: {amax, maximum, exp, reduce, add, reciprocal,
#                                            zero_max_mode, return_max_reduce, mixed_precision, float32, astype}
m   = amax(x, dim)                     # reduce-MAX over axis    → nisa tensor_reduce(max)
xs  = x - m  (or maximum(x - m, 0))    # numerical-stability shift; zero_max_mode clamps the shift
e   = exp(xs)                          # Scalar-engine exp activation
s   = reduce(add)(e, dim)              # reduce-SUM              → nisa tensor_reduce(add)
r   = reciprocal(s)                    # nisa.reciprocal
out = e * r                            # tensor_tensor multiply (broadcast)
```

`softmax` options confirmed in the body: `zero_max_mode` (clamp the shift), `return_max_reduce` (also return the max), `mixed_precision`/`float32` (accumulate the exp-sum in fp32, via `astype`). Split helpers `softmax_exp`(41) = max+exp leg and `softmax_rsum`(43) = exp+reduce-sum leg exist for tiled/online softmax. This `reduce-max → exp → reduce-sum → reciprocal·` chain mirrors the HLO-side softmax legalization ([4.27 softmax-legalize](../hlo-opt/softmax-legalize.md)).

```python
# native_maths.rms_norm (47) — verified n_s: {power, mean, epsilon, sqrt, expand_dims, w, shape, x, axis, np}
ss  = power(x, 2)                      # square
ms  = mean(ss, axis) + epsilon         # mean-square + eps (tensor_reduce mean + scalar add)
rms = sqrt(ms)                         # sqrt activation
inv = reciprocal(rms)                  # 1 / rms
out = x * expand_dims(inv) * w         # normalize, then scale by weight w
# = x / sqrt(mean(x**2) + eps) * w
```

`rms_norm_multidim`(49) handles >1 reduce axis.

> **CORRECTION — `native_maths.rms_norm`'s body keys on `axis`, not a free-standing `n`.** The backing report (§2.6) cites the `native_maths.rms_norm` interned set as including `n_s_n`. The actual `pw`(47) interned set is `{power, mean, epsilon, sqrt, expand_dims, w, shape, x, axis, np}` — it has **`axis`**, not a separate `n`. (The `n` argument is on the `nl.rms_norm`(101) *wrapper* signature, where it pairs with `axis`; it does not appear as an interned op-name inside the `native_maths` reference body, which derives the reduce extent from `shape`/`axis`.)

---

## 9 · Creation ops (`creation_ops`)

`pw` symbols confirmed: `ndarray`(1), `full`(3), `zeros`(5), `ones`(7), `zeros_like`(9), `empty_like`(11), `rand`(13), `random_seed`(15), `shared_constant`(17), `shared_identity_matrix`(19). There is **one** base allocator (`ndarray`) reached through `nki_ctx.cur_scope.buffer(...)`; every other creation op is a thin wrapper. Allocation produces a `Tile` bound to a buffer (SBUF default) — **not** an IR compute instruction; any fill is a separate memset/iota the lowering inserts.

```c
// ndarray (1) — verified n_s: {nki_ctx, cur_scope, buffer, shape, dtype, name}
scope = nki_ctx.cur_scope;
return scope.buffer(shape, dtype, name=name);          // uninitialised Tile, default buffer=SBUF

// full (3) — verified n_s: {nki_ctx, cur_scope, buffer, fill_value, shape, dtype, name}
scope = nki_ctx.cur_scope;
return scope.buffer(shape, dtype, init=fill_value, ...); // lowering emits memset/broadcast-fill
```

- `zeros`/`ones`/`zeros_like` are 1-line wrappers over the `full` global (`zeros` = `full(shape, 0, …)`, `ones` = `full(shape, 1, …)`, `zeros_like` = `full(a.shape, 0, dtype or a.dtype, …)`).
- `empty_like` = `ndarray(a.shape, dtype or a.dtype, …)` (no fill).
- `shared_constant`(17) → `nki_ctx.shared_constant(constant, dtype)`: materialises a compile-time NumPy array as a read-only DRAM/HBM constant shared across program instances. The data **is** the payload, not a scalar fill.
- `shared_identity_matrix`(19) → `nki_ctx.shared_identity_matrix(n, dtype=np.uint8)`: an N×N identity in a shared buffer, used as the stationary operand for `nc_transpose`-via-matmul and permutation/gather setups.
- `rand`(13) → `nisa.random(shape, dtype)` (HW PRNG fill); `random_seed`(15) → `nki_ctx.random_seed(seed, mask)` seeds the engine PRNG (must precede `rand`/`dropout` for reproducibility).

**Buffer defaults:** all alloc ops default `buffer=SBUF` (valid: `sbuf` / `psum` / `hbm`(dram)); `name` defaults `""`; `dtype` is required positionally for `zeros`/`ones`/`full`/`ndarray`, inherited for `*_like`. `sema` partition/free-dim asserts (par≤128, per-partition byte budget) fire at allocation.

> **NOTE — scope correction vs the report.** The backing report's creation-ops list includes `iota`. There is **no** `iota` `pw` in `creation_ops` — `iota` is `native_maths.iota`(51). Creation_ops' ten public `pw` symbols are exactly those above.

---

## 10 · Memory ops (`memory_ops`)

`pw` symbols confirmed under the `10memory_ops` mangling: `load`(1), `store`(3), `atomic_rmw`(5), `load_transpose2d`(7), `copy`(9), `broadcast_to`(11). **Every** memory op dispatches to a method on the `nki_ctx` build context (`__pyx_n_s_nki_ctx` appears in each body); the `nki_ctx` method mints the DMA/copy IR node and, for `load`/`load_transpose2d`, allocates the destination SBUF tile. All addr-space / dtype / DMA-transpose legality lives in `nki_ctx`+`sema`, not the `nl` wrapper.

```c
// load (1) — n_s: {nki_ctx, load, src, dtype, mask}
return nki_ctx.load(src, mask, dtype);            // DMA: HBM/DRAM → SBUF; nki_ctx allocates dst tile

// store (3) — n_s: {nki_ctx, store, dst, value, mask}
return nki_ctx.store(dst, value, mask);           // DMA: SBUF → HBM/DRAM; dst is a pre-existing HBM tensor

// copy (9) — n_s: {nki_ctx, copy, src, dtype, mask}
return nki_ctx.copy(src, mask, dtype);            // ON-CHIP copy (SBUF/PSUM→SBUF), compute-engine, NOT DMA

// load_transpose2d (7) — n_s: {nki_ctx, dma_transpose, src, dtype, mask}
return nki_ctx.dma_transpose(src, mask, dtype);   // fused DMA-load + 2D transpose via HW-DGE
```

`load_transpose2d` fuses an HBM→SBUF load with a 2-D transpose via the descriptor-generation engine (legality: ≤16 parts, ≤128 cols, 2-byte dtype, N×1 uint32 indices, `nc_version≥gen3` else SWDGE fallback — see [6.4.2](./isa-reduce-dve-dma.md) `dma_transpose`), saving a separate `nc_transpose`.

```c
// atomic_rmw (5) — n_s: {nki_ctx, op, add, store, value, dst, mask,
//                        err_atomic_rmw_add_only, err_atomic_rmw_dynamic_index,
//                        IndirectMemrefTileND, numpy}
// validate: op MUST be np.add (else err_atomic_rmw_add_only)
// validate: dst MUST use indirect/dynamic indexing IndirectMemrefTileND (else err_atomic_rmw_dynamic_index)
return nki_ctx.<store-with-atomic-add-flag>(dst, value, op);   // scatter-add via indirect DMA
```

> **GOTCHA — `atomic_rmw` is add-only and index-restricted.** Verified verbatim in the body: `err_atomic_rmw_add_only` (op must be `np.add`) and `err_atomic_rmw_dynamic_index` (`dst` must be an `IndirectMemrefTileND` with dynamic index values). The RMW reuses the `store` path with an atomic-add flag. Any non-add op or a statically-indexed `dst` is rejected at trace time.

`broadcast_to`(11) → a view/broadcast op producing a tile of `shape` from `src` by 0-stride broadcasting (no data movement; sets up a broadcast access pattern for a subsequent compute op). It lives in `memory_ops` because it produces a memref view, not a compute result. (STRONG — the exact `nki_ctx` method is obscured; the `shape`-only broadcast is confirmed from the interned set.)

---

## 11 · Shape & select ops

```c
// expand_dims (shape_manipulation_ops, ordinal 1) — n_s: {data, axis, expand_dims} ONLY — self-contained
// inserts a size-1 axis at `axis`; PURE VIEW (rewrites tile access-pattern metadata, emits NO IR node)

// where (indexing_ops) — n_s: {nki_ctx, select, condition, x, y, dtype, mask}
return nki_ctx.select(condition, x, y);   // HW predicated-select (masked tensor_tensor); elementwise ternary
```

`expand_dims` is a pure metadata rewrite (only supports expanding after the last index) used to set up broadcasting for `tensor_tensor`. `where` lowers to `nki_ctx.select` — the HW predicated select. `gather_flattened` (indexing_ops) → `nki_ctx.gather_flattened` mints the indirect-DMA / pool-gather node. **There is no `nl.reshape` / `nl.squeeze`** in this version's surface (absent from both the `.so` symbol tables and the stubs).

---

## 12 · Composite-vs-primitive classification (the deliverable)

**PRIMITIVE** — one `nl` op : one HW instruction:

| Class | ops | lowers to |
|---|---|---|
| unary | `exp, log, sin, cos, tan, tanh, sqrt, abs, negative, floor, ceil, trunc, arctan, sign` | 1 `tensor_scalar`/`activation` |
| binary | `add, subtract, multiply, maximum, minimum, power, fmod, mod, divide, bitwise_*, *_shift, equal/greater/less/…, logical_*` | 1 `tensor_tensor` |
| activation | `gelu, silu, relu, sigmoid, erf, softplus, mish, rsqrt, square, gelu_apprx_*, *_dx` | 1 Scalar ACT_FN |
| direct nisa | `reciprocal` (`nisa.reciprocal`), `transpose` (`nisa.nc_transpose`) | 1 instr |
| reduce | `max, min, sum, prod, mean, all` | 1 reduce instr (mean's /N is the reduce command) |
| alloc | `zeros, ones, full, ndarray, empty_like, *_like, shared_constant, shared_identity_matrix, rand, random_seed` | 1 memref node, no compute |
| memref | `load, store, copy, load_transpose2d, atomic_rmw, broadcast_to, expand_dims, where, gather_flattened` | 1 DMA/view/select node |

**COMPOSITE** — one `nl` op : N instructions:

| op | expansion | instrs |
|---|---|---|
| `softmax` | reduce_max → (shift) → exp → reduce_sum → reciprocal → multiply | ≈5–6 (`native_maths.softmax`; helpers `softmax_exp`/`softmax_rsum`) |
| `rms_norm` | square → reduce_mean → +eps → sqrt → reciprocal → ·x → ·w | ≈6–7 (`native_maths.rms_norm`; `rms_norm_multidim` for >1 axis) |
| `var` | mean → subtract → power(²) → mean | 4 `nl` ops |
| `matmul` | nc_transpose(x) → nc_matmul(x.T, y) | 2 (1 if `transpose_x=True`) |
| `all_reduce`/`loop_reduce` | `nki_ctx.loopreduce` over program/loop axes | 1 logical op, multiple HW reduces |
| `dropout` | `nisa.n_dropout` (PRNG gen + mask-multiply) | — |

> **CAVEAT — "1 nl : 1 instr" for the activation family assumes the arch HAS the HW ACT_FN.** On an arch lacking the unit, the `native_maths.<fn>` body supplies the multi-op fallback (§5 formulas) — then the op is composite. The `native_maths.<fn>` callable is the single token that selects between the two.

---

## Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **Four dispatchers + op-tokens.** ✅ Verified by interned-name sets: `exp`→`{unary_op_2, np, exp}`, `add`/`equal`→`{binary_op_2, np, …}`, `gelu`→`{activation, native_maths, gelu}`, `sum`→`{tensor_reduce, np, add}`. The dispatchers have no `pw` symbol in `math_ops` (confirmed by absence from the symbol enumeration).
2. **`sigmoid` uses `expit`, `square` uses `activation`, `divide` carries `nki_assert`.** ✅ All three interned sets verified verbatim (`expit`; `activation`+`np`+`square`; `binop`+`sema`+`nki_assert`+`tile`).
3. **softmax/rms_norm composite lowering.** ✅ `native_maths.softmax`(39) = `{amax, maximum, exp, reduce, add, reciprocal, zero_max_mode, return_max_reduce, mixed_precision, float32, astype}`; `native_maths.rms_norm`(47) = `{power, mean, epsilon, sqrt, expand_dims, w, shape, axis}`. Both match the documented chains.
4. **`matmul` inserts a VECTOR-engine transpose.** ✅ `matmul`(145) = `{nisa, nc_transpose, nc_matmul, vector, engine, transpose_x}` — the `vector`+`engine` pair confirms the default transpose is on the Vector engine, not the PE array.
5. **`atomic_rmw` is add-only / dynamic-index-only.** ✅ Body contains `err_atomic_rmw_add_only`, `err_atomic_rmw_dynamic_index`, `IndirectMemrefTileND` verbatim.

**Failures caught and corrected** (issued as in-place CORRECTION callouts): the report's claim that bitwise wrappers call `_is_integer` directly (§4 — it's inside `bitwise_op`); that `sum`/`prod` promote integer accumulators in-body (§6 — only `mean` does, table-verified); that `native_maths.rms_norm` interns `n` (§8 — it interns `axis`); that `dropout` references `numpy`/`add` (§5 — its set is `{n_dropout, rate}`); and that `iota` is a `creation_ops` op (§9 — it is `native_maths.iota`(51)).

**Tagged uncertain:** the ufunc→engine routing inside `compiler.backends.neuron.tensor` (STRONG, builder not fully read); the exact `@trace` tiling wrapper `softmax_kernel`/`rmsnorm_kernel` add over the `native_maths` reference (STRONG); positional-vs-keyword arg order into each dispatcher (Cython merges all named args into one kwargs dict before the call — CONFIRMED names, STRONG order).

## Cross-references

- [6.4.1 `nki.isa` compute intrinsics](./isa-compute-intrinsics.md) — `nc_matmul`/`nc_transpose`/`activation`/`tensor_tensor`/`tensor_scalar`/`reciprocal` signatures + `_check_*` validators that the `nl` dispatchers call.
- [6.4.2 `nki.isa` reduce / DVE / DMA](./isa-reduce-dve-dma.md) — `tensor_reduce` (`nl.max`/`sum`/`mean`/…), `dma_transpose` (`nl.load_transpose2d`), `n_dropout` (`nl.dropout`).
- [4.27 softmax legalization](../hlo-opt/softmax-legalize.md) — the HLO-side softmax legalization mirrors `native_maths.softmax`.
- Part 6.7 (kernels using `nl`) — real kernels exercising this surface.
