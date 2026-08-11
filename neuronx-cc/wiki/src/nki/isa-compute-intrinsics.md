# nki.isa COMPUTE Intrinsics and Validators

> *All symbols, offsets, and rules on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310/311/312 wheels). The `@0x…` addresses are file offsets into `neuronxcc/nki/isa/neuron_isa.cpython-310-x86_64-linux-gnu.so`; signatures and docstring rules are cross-checked against `neuronxcc-stubs/nki/isa/__init__.pyi` (2051 lines), which ships in the wheel and is therefore binary-derived ground truth. Other versions will differ.*

## Abstract

`nki.isa` (`nisa.*`) is the instruction-level layer of the NKI programming model: each public function is a single hardware instruction the programmer places by hand, in contrast to the tile-level `nki.language` (`nl.*`) ops that the compiler may fuse or re-engine. The module is a **Cython-compiled extension** (`neuron_isa.cpython-3XX.so`, ~451 native function bodies), and this page covers its *compute* half — the eleven intrinsics that build PE-array, Scalar-Engine, and Vector/GpSimd-Engine compute nodes: `nc_matmul`, `nc_transpose`, `activation`, `activation_reduce`, `tensor_tensor`, `tensor_scalar`, `tensor_scalar_reduce`, `tensor_scalar_cumulative`, `scalar_tensor_tensor`, `tensor_tensor_scan`, and `reciprocal`. The reduce/DVE/DMA half (`reduce`, `range_select`, `select`, `iota`, copies, DMA) is documented separately in [6.4.2](isa-reduce-dve-dma.md).

The single most important architectural fact is that **`neuron_isa` is a thin legality-plus-lowering veneer over `sema`**, the IR builder (the `nki_ctx.sema` object, owned by the forward-builder layer in Part 6.5.1). Each intrinsic does three things and no more: it unpacks Python kwargs, runs a battery of `_check_*` validators that enforce *combinations* and *engine selection* and emit the user-facing error strings, then calls one method on `sema` named after the hardware op (`sema.matmult`, `sema.activation`, `sema.tensortensor`, `sema.tensorvectorscalar`, `sema.tensorscalarcumulative`). The op-class predicates (`is_arith_ops`, `is_bitvec_ops`, `is_int_type`), the per-engine supported-op *sets*, `promote_type`, and the low-level matmul shape check all live on `sema`, not here. So a faithful reimplementation of this module reproduces the validator catalog and the args→`sema.<method>` mapping; the legality *sets* it gates against are owned one layer down.

> **NOTE —** Because every call inside a Cython `pw` body is a Python-level attribute or module-global lookup (`GetModuleGlobalName`, `PyObject_GetAttr`, `PyObject_Call`), the *native* callgraph shows **no** intrinsic→validator edges — every edge is indirect through the interned-name table (`__pyx_n_s_*`). The dispatch described here is recovered from those interned names inside each `pw` body — a reliable reading, even though the static callgraph looks empty.

For reimplementation, the contract is:

- The **Cython veneer model**: one `__pyx_pw_…_<N><name>` per intrinsic, semantic body = a sequence of global-fetch / attr-get / call against interned names.
- The **per-intrinsic args→IR-node table**: which `sema.<method>` each builds and how Python kwargs are renamed into it (`is_stationary_onezero`→`is_lhs_onezero`, etc.).
- The **complete `_check_*` validator catalog**: the rule each enforces, the error string it raises, and the NeuronCore-version gate where one exists.

| | |
|---|---|
| **Module** | `neuronxcc/nki/isa/neuron_isa.cpython-3XX-x86_64-linux-gnu.so` (Cython) |
| **API ground truth** | `neuronxcc-stubs/nki/isa/__init__.pyi` — 2051 lines, full docstrings |
| **IR builder handle** | module-global `nki_ctx`; `sema = nki_ctx.sema` |
| **Compute IR methods** | `sema.matmult` · `sema.activation` · `sema.tensortensor` · `sema.tensorvectorscalar` · `sema.tensorscalarcumulative` |
| **Engine enum** | `tensor=1 vector=5 scalar=2 gpsimd=3 dma=4 sync=6 unknown=0` (`__init__.pyi` L28–47) |
| **reduce_cmd enum** | `idle=0 reset=1 reduce=2 reset_reduce=3 load_reduce=4` (`__init__.pyi` L88–100) |
| **Compute intrinsics** | 11 |
| **Compute-set validators** | 24 (`_check_*`) + 2 `_is_*` predicates + 1 nested `.check_dtype` closure |

---

## Shared Machinery

### The `nki_ctx` / `sema` handle

Every compute intrinsic obtains the IR builder through the module-global `nki_ctx` and then calls a method on its `sema` attribute named after the hardware op:

```c
function <intrinsic>(args...):                 // __pyx_pw_…_<N><name>
    ctx  = ModuleGlobal("nki_ctx")             // GetModuleGlobalName __pyx_n_s_nki_ctx
    sema = GetAttr(ctx, "sema")                // GetAttr __pyx_n_s_sema
    // ... arg unpacking + the _check_* battery ...
    node = sema.<hw_method>(forwarded_args, target=ctx.target)
    return node
```

The IR-method targets, recovered from the interned method names in each body:

| `sema` method | Built by | penguin.ir node |
|---|---|---|
| `matmult` | `nc_matmul`, `nc_transpose` (Tensor path) | Matmul |
| `activation` | `activation`, `activation_reduce` | Activation |
| `tensortensor` | `tensor_tensor`, `tensor_tensor_scan` | TensorTensor |
| `tensorvectorscalar` | `tensor_scalar`, `tensor_scalar_reduce`, `scalar_tensor_tensor` | TensorScalar / VectorScalar |
| `tensorscalarcumulative` | `tensor_scalar_cumulative` | TensorScalarCumulative |

`sema` is also the home of the op-class predicates the validators delegate to — `sema.is_arith_ops(op)`, `sema.is_bitvec_ops(op)`, `sema.is_int_type(dtype)`, the `sema.bitvec_opcodes` set, `promote_type(a,b)` for output dtype, and `check_matmul_low_level_shape` for PE geometry. Argument-presence guards use the module helpers `assert_not_none(...)` and `nki_assert(...)`.

> **GOTCHA —** the literal membership *sets* behind `_check_*_supported_ops` are **not** readable constants inside `neuron_isa`; they are attributes on `sema` consulted at runtime (`sema.tensor_tensor_supported_ops`, etc.). What `neuron_isa` owns is the *combination* logic and the error strings. A reimplementer who only reads this module sees the gate but not the set; the sets belong to the `sema` builder layer.

### Engine legality per compute op

The `engine=` kwarg defaults to `unknown` (the compiler picks by shape) on the intrinsics that expose it; the `*_engine_supported_ops` validators enforce the matrix:

| Intrinsic | Legal engine(s) | Note |
|---|---|---|
| `nc_matmul` / `nc_transpose` (tensor) | Tensor (`matmult`) | SBUF in, PSUM out, FP32 accumulate |
| `nc_transpose` (vector) | Vector | ≤ 32×32 input |
| `activation` / `activation_reduce` | Scalar **only** | math always FP32 |
| `tensor_tensor` | Vector (any binary op) \| GpSimd (`power`, integer add/mul/sub) | GpSimd cannot touch PSUM |
| `tensor_scalar` | Vector \| Scalar \| GpSimd (`rsqrt` only) \| Unknown | bitvec ⇒ Vector only |
| `tensor_scalar_reduce` | Vector | — |
| `tensor_scalar_cumulative` | Vector | accumulator scan |
| `scalar_tensor_tensor` | Vector | — |
| `tensor_tensor_scan` | Vector | — |
| `reciprocal` | Vector | 1/x |

### Datatype and ALU-op vocabulary

The dtype names present in the module string table: `float8_e4m3, float8_e5m2, bfloat16, float16, tfloat32, float32, int8, int16, int32, uint8, uint16, uint32, bool_`.

ALU ops split into two families that the `_check_*_both_arith_or_bitvec` gate keeps from mixing:

```text
arithmetic : add subtract multiply divide maximum minimum power rsqrt
             abs(absolute) mod is_equal/equal greater greater_equal
             less less_equal not_equal negate
bitvec     : bitwise_and bitwise_or bitwise_xor bitwise_not
             logical_and logical_or logical_xor logical_not
             left_shift right_shift              (== sema.bitvec_opcodes)
```

### The reduce_cmd legality gates

`reduce_cmd` drives the engine accumulator registers. The two compute intrinsics that gate it accept different subsets:

| Intrinsic | Legal `reduce_cmd` | Banned |
|---|---|---|
| `activation` | `{idle, reset, reduce, reset_reduce}` | `load_reduce` |
| `tensor_scalar_cumulative` | `{reset_reduce (default), reduce, load_reduce}` | `idle`, `reset` |

`reset_reduce` initialises the register by the `op1`-identity (`0` for add/sub, `1` for mult, `-inf` for max, `+inf` for min); `load_reduce` initialises it to `imm1`; `reduce` keeps the running value (`__init__.pyi` cumulative pseudocode L1773–1790).

### The `_check_*` family contract

Three recurring shapes account for every validator:

```c
// (a) per-API op-membership gate
function _check_<api>_supported_ops(inst_name, param_name, op):     // sub_…
    if op not in sema.<api>_supported_ops:
        raise err_instruction_unsupported_op(inst_name, param_name, op)

// (b) per-engine op gate — selects/validates engine, may consult promote_type
function _check_<api>_<engine>_engine_supported_ops(inst_name, param, op, data1, data2, engine):
    if op not in sema.<api>_<engine>_supported_ops:
        raise err_instruction_unsupported_op(...)

// (c) reduce-cmd gate — delegates to the generic helper
function _check_<api>_supported_reduce_cmd(inst_name, cmd):
    _check_supported_reduce_cmd(inst_name, cmd, {idle, reset, reduce, reset_reduce})
```

The verbatim diagnostic interned-names found in `.rodata` (the rule, encoded as its error message):

```text
err_instruction_unsupported_op             op not in this engine's supported set
err_instruction_unsupported_dtypes         input dtype not legal for this op
err_instruction_supported_dtypes           companion: the legal-dtype list message
err_instruction_engine_unsupported_op_comb op0/op1 combination illegal on chosen engine
err_bitvec_operand_must_be_integer         bitvec op with non-integer operand dtype
err_activation_scale_invalid_type          scale dtype != float32
err_activation_bias_invalid_type           bias dtype not in {f32,f16,bf16}
err_reduce_unsupported_negate              negate not allowed as a reduce op
err_par_reduce_unsupported_op
err_par_reduce_bitvec_op_invalid_dtype
err_reduce_bitvec_op_invalid_dtype
err_min_arch_support_for_hwdge             HW-DGE needs a min NeuronCore arch
```

---

## Compute Intrinsics

Each section gives the verbatim signature (from `__init__.pyi`), the args→`sema` IR node, and the validators dispatched in-body (recovered from interned `__pyx_n_s_*` names).

### nc_matmul — `@0x61d50`

```python
def nc_matmul(stationary, moving, *, is_stationary_onezero=False,
              is_moving_onezero=False, is_transpose=False,
              tile_position=(), tile_size=(), mask=None, **kwargs)
```

`PSUM_out = stationary.T @ moving` on the Tensor Engine (128×128 PE systolic array). Both inputs read from SBUF; result and accumulation are **always FP32**. The contraction dim is the partition axis of *both* inputs (equal, ≤ 128); free axes are stationary ≤ 128, moving ≤ 512; output layout is `(stationary.free, moving.free)`.

**IR node:**

```c
sema.matmult(stationary, moving,
             is_lhs_onezero = is_stationary_onezero,   // renamed in-body
             is_rhs_onezero = is_moving_onezero,        // renamed in-body
             is_transpose, tile_position, tile_size, mask,
             target = nki_ctx.target)
```

**Validators dispatched:** `_check_nc_matmul_supported_dtypes`, `_check_nc_matmul_pe_tiling_position_size_supported_shape`, `_check_nc_matmul_pe_tiling_position_size_supported_dtypes`.

**Inline guards** (verbatim `.rodata`, each a `nki_assert`):

```text
"nc_matmul tile_position and tile_size must be both set or not set."
"nc_matmul tile_position must be a 2D tuple (row_tile_position, col_tile_position) or ()."
"nc_matmul tile_size must be a 2D tuple (row_tile_size, col_tile_size) or ()."
"nc_matmul tile_position only supports int or affine expression."
"nc_matmul tile_size only supports int or affine expression."
"- nc_matmul: missing tiling mode and perf mode"
```

The numeric shape constraints (values multiple of 32, `tile_size >= accessed stationary`, both row & col of `tile_size` cannot be 32 on NeuronCore-v2) are delegated to `sema.check_matmul_low_level_shape` — quoted verbatim from `__init__.pyi` L976–982:

```text
3. Values in tile_position and tile_size must be multiple of 32.
4. tile_size must be larger than or equal to accessed stationary tile size.
5. Both the row and column sizes in tile_size cannot be 32 for NeuronCore-v2.
```

> **QUIRK —** `is_stationary_onezero` / `is_moving_onezero` are **pure perf hints with no legality effect**; the docstring (L990) states they give "2x better performance if stationary tile is in float32" and "no impact for non-float32" inputs. They are renamed to `is_lhs_onezero`/`is_rhs_onezero` when forwarded to `sema.matmult`, so a reimplementer tracing the IR node will not find the original kwarg names.

### nc_transpose — `@0x3b540`

```python
def nc_transpose(data, *, mask=None, dtype=None, engine=engine.unknown, **kwargs)
```

2D partition↔free transpose. The Tensor path is `matmul(data, identity)` into PSUM (≤ 128×128); the Vector path handles ≤ 32×32; `engine=unknown` lets the compiler pick by shape. A `dtype` mismatch inserts an extra `nki.isa.cast` after the transpose.

**IR node:** Tensor path → `sema.matmult(data, identity, is_transpose=True, …)`; Vector path → a vector transpose builder. Routed by `engine`.

**Validators dispatched:** **none** — no `_check_*` interned name appears anywhere in this body; geometry legality is reused indirectly through the matmul low-level shape path inside `sema`.

> **GOTCHA —** on NeuronCore-v2 the matmul-transpose path is **not bit-accurate for NaN/Inf inputs** (stub note). A reimplementation that routes a v2 transpose through the PE array and expects exact NaN/Inf propagation will diverge.

### activation — `@0x65cb0`

```python
def activation(op, data, *, bias=None, scale=1.0, reduce_op=None, reduce_res=None,
               reduce_cmd=reduce_cmd.idle, mask=None, dtype=None, **kwargs)
```

Scalar Engine: `out = f_op(data*scale + bias)`. The engine always casts inputs to FP32 and does the math in FP32 (`__init__.pyi` L162). `scale` must be FP32 (scalar const broadcast, or a `(P,1)` vector); `bias` may be FP32/FP16/BF16 `(P,1)`. An optional free-axis reduction into the 128 scalar-engine accumulator registers is controlled by `reduce_cmd`; `reduce_res` reads the registers out. Output dtype = `dtype` or input dtype.

**IR node:** `sema.activation(op, data, bias, scale, reduce_op, reduce_res, reduce_cmd, mask, dtype, target=…)`.

**Validators dispatched (all 6 named in body):**

```text
_check_activation_supported_ops                op is a legal activation function
_check_activation_scale_type                   scale dtype == FP32      -> err_activation_scale_invalid_type
_check_activation_bias_type                    bias dtype in {f32,f16,bf16} -> err_activation_bias_invalid_type
_check_activation_reciprocal_bias              reciprocal must have bias=None on gen2
_check_activation_reduce_supported_reduce_ops  reduce_op legal; negate banned -> err_reduce_unsupported_negate
_check_activiation_supported_reduce_cmd        reduce_cmd in {idle,reset,reduce,reset_reduce}  [sic spelling]
```

### activation_reduce — `@0x5ff40`

```python
def activation_reduce(op, data, *, reduce_op, reduce_res, bias=None, scale=1.0,
                      mask=None, dtype=None, **kwargs)
```

Equivalent to `activation` with `reduce_cmd=reset_reduce` and a mandatory `reduce_res`; kept for back-compat (the stub recommends plain `activation`). On NeuronCore-v2 `reduce_op` must be `add`.

**IR node:** `sema.activation(…, reduce_cmd=reset_reduce, reduce_op, reduce_res, …)`.

**Validators dispatched (4 named — note: NO `reciprocal_bias`, NO `reduce_cmd` gate, since the cmd is fixed):** `_check_activation_supported_ops`, `_check_activation_scale_type`, `_check_activation_bias_type`, `_check_activation_reduce_supported_reduce_ops`.

### tensor_tensor — `@0x5e4e0`

```python
def tensor_tensor(data1, data2, op, *, dtype=None, mask=None, engine=engine.unknown, **kwargs)
```

Elementwise `data1 <op> data2`. **Vector Engine**: any binary NKI op (bitvec needs integer in/out, treated as bit patterns with no cast). **GpSimd Engine**: only `power` or integer `add`/`multiply`/`subtract`. Arithmetic ops auto-cast to FP32. Both inputs must share P and elems/part; both cannot be in PSUM (3 legal SBUF/PSUM placements); GpSimd cannot touch PSUM at all. Output dtype = `dtype` or `promote_type(data1,data2)`.

**IR node:** `sema.tensortensor(data1, data2, op, dtype, mask, engine, target=…)`.

**Validators dispatched:** `_check_tensor_tensor_supported_ops`, `_check_tensor_tensor_vector_engine_supported_ops`, `_check_tensor_tensor_gpsimd_engine_supported_ops`, `_check_tensor_scalar_bitvec_dtype` (shared bitvec int-dtype guard).

### tensor_scalar — `@0x6fd00`

```python
def tensor_scalar(data, op0, operand0, reverse0=False, op1=None, operand1=None,
                  reverse1=False, *, dtype=None, mask=None, engine=engine.unknown, **kwargs)
```

`(data <op0> operand0) <op1> operand1`, operands broadcast over the free dim. `operand0/1` are scalar consts or `(P,1)` vectors and **must be FP32**. `op0`/`op1` must be the same family (both bitvec or both arith). Bitvec ⇒ Vector Engine + integer in/out only. Arith ⇒ Vector \| Scalar \| GpSimd. The Scalar Engine on trn2 supports **only** the combos `(multiply, add)`, `(multiply, None)`, `(add, None)` (`__init__.pyi` L1716–1718). GpSimd is `rsqrt`-only. `op1`/`operand1` are both-or-neither; `reverse0/1` swap operand/data order.

**IR node:** `sema.tensorvectorscalar(data, op0, operand0, reverse0, op1, operand1, reverse1, dtype, mask, engine, target=…)`.

**Validators dispatched (8 named):**

```text
_check_tensor_scalar_supported_ops
_check_tensor_scalar_ptr_supported_ops
_check_tensor_scalar_ptr_gpsimd_engine_supported_ops
_check_tensor_scalar_ptr_scalar_engine_supported_ops
_check_tensor_scalar_ptr_scalar_engine_supported_op_comb   the 3 trn2 combos
_check_tensor_scalar_both_arith_or_bitvec                  "must be both bitvec or both arithmetic operators."
_check_tensor_scalar_bitvec_dtype                          -> err_bitvec_operand_must_be_integer
_check_tensor_scalar_abs                                   "'abs' engine must be Vector or Unknown."
_check_tensor_scalar_rsqrt                                 "'rsqrt' engine must be GpSimd or Unknown."
```

**Inline guards** (`.rodata`):

```text
"Engine for tensor_scalar can only be Scalar or Vector or GpSIMD or Unknown."
"operand1 and op1 input arguments must be both provided or both None."
```

### tensor_scalar_reduce — `@0x46790`

```python
def tensor_scalar_reduce(*, data, op0, operand0, reduce_op, reduce_res, reverse0=False,
                         dtype=None, mask=None, **kwargs)
```

Vector Engine: `result = data <op0> operand0`; `reduce_res = reduce_op(result, free)`. Only one op; `op0` must be **arithmetic** (bitvec banned); `reduce_op ∈ {add, subtract, multiply, max, min}`; `reduce_res` is a `(P,1)` SBUF/PSUM tile. `reverse0` is documented but **not yet supported**.

**IR node:** `sema.tensorvectorscalar(data, op0, operand0, reverse0, reduce_op=…, reduce_res=…)`.

**Validators dispatched (3 named):** `_check_tensor_scalar_reduce_supported_ops` (op0 arithmetic), `_check_tensor_scalar_reduce_supported_reduce_ops` (the 5-op reduce set), `_check_tensor_scalar_abs`.

### tensor_scalar_cumulative — `@0x55c50`

```python
def tensor_scalar_cumulative(*, src, dst, op0, op1, imm0, imm1=None,
                             reduce_cmd=reduce_cmd.reset_reduce, mask=None, **kwargs)
```

Vector Engine cumulative scan: per element `reg = op1(op0(src[i], imm0), reg)`; `dst[i] = reg`. `op0` is an arith scalar op; `op1 ∈ {add, subtract, mult, max, min}`. `reduce_cmd` controls register init (see the reduce_cmd table above). `imm0` (and `imm1` under `load_reduce`) **must be FP32** (`__init__.pyi` L1814–1816). In/out dtypes are restricted to `{BF16, FP16, FP32, FP8, UINT8, UINT16, INT8, INT16}` — **INT32/UINT32 banned (ISA limitation)**, stated verbatim in `__init__.pyi` L1799–1800.

**IR node:** `sema.tensorscalarcumulative(src, dst, op0, op1, imm0, imm1, reduce_cmd, mask, …)` — the interned name `__pyx_n_s_tensorscalarcumulative` is present in the body; backed by `neuronxcc.include.isa.tensor_scalar_cumulative_info`.

**Validators dispatched:** `_check_tensor_scalar_cumulative_immediate_values`, which contains a nested `.check_dtype` closure (`__pyx_pf_…_48_…_check_dtype @0x28690`) enforcing each immediate's `dtype == float32`.

> **NOTE —** the INT32/UINT32 ban is enforced in the `sema.tensorscalarcumulative` lowering (an ISA limit), **not** by a named `_check_*` in `neuron_isa`. The only in-module validator for this intrinsic is the FP32 immediate check; the ban itself is read from stub L1799–1800, since no matching validator string exists locally.

### scalar_tensor_tensor — `@0x81200`

```python
def scalar_tensor_tensor(*, data, op0, operand0, op1, operand1, reverse0=False,
                         reverse1=False, dtype=None, mask=None, **kwargs)
```

Vector Engine: `(data <op0> operand0) <op1> operand1`. `operand0` is scalar/`(P,1)`; `operand1` is a full `(P,F)` tile matching `data`. `operand1` and `data` cannot both be PSUM. Fast path (N cycles) when `data` & `operand1` are bf16, `op0=subtract`, `op1=multiply`, N even.

**IR node:** `sema.tensorvectorscalar(data, op0, operand0, op1, operand1, reverse0, reverse1, dtype, mask, target=…)`.

**Validators dispatched (4 named):** `_check_tensor_scalar_supported_ops`, `_check_tensor_scalar_both_arith_or_bitvec`, `_check_tensor_scalar_bitvec_dtype`, `_check_tensor_scalar_abs`.

> **NOTE —** the two-tensor form is distinguished from plain `tensor_scalar` inside `sema` by a builder-flag, **not** by an `is_scalar_tensor_tensor` kwarg literal in this `pw` body (0 occurrences of the interned name at `0x81200`). The flag string exists in the module, but which builder entry `sema` takes is what sets it.

### tensor_tensor_scan — `@0x4e300`

```python
def tensor_tensor_scan(data0, data1, initial, op0, op1, reverse0=False, reverse1=False,
                       *, dtype=None, mask=None, **kwargs)
```

Vector Engine: `result[:,0] = op1(op0(data0[:,0], initial), data1[:,0])`; then `result[:,i] = op1(op0(data0[:,i], result[:,i-1]), data1[:,i])`. `op0`,`op1` are any arith binary op; `initial` is an FP32 scalar const (P-broadcast) or `(P,1)` tile; `data0`,`data1` cannot both be PSUM; auto-cast to FP32, out dtype = `dtype` or higher-precision input.

**IR node:** `sema.tensortensor(data0, data1, op0, op1, initial, reverse0, reverse1, …, scan-flag)`.

**Validators dispatched:** `_check_tensor_scalar_supported_ops` (shared arith-op legality gate for `op0`/`op1`).

### reciprocal — `@0x3a980`

```python
def reciprocal(data, *, dtype=None, mask=None, **kwargs)
```

Vector Engine elementwise `1/x`. Cost ≈ `max(MIN_II, 8*N)` vector cycles.

**IR node:** a Vector reciprocal builder on `sema` (`tensorvectorscalar`/divide-class).

**Validators dispatched:** **none** named in body.

> **NOTE —** the diagnostic `"reciprocal must have bias=None for trn1."` belongs to `_check_activation_reciprocal_bias`, where `reciprocal` appears *as an activation op*, not to this standalone `reciprocal` intrinsic. The two are distinct: `nisa.reciprocal` is a Vector op; `reciprocal`-as-activation is a Scalar op with a bias slot.

---

## Validator Catalog

Twenty-four `_check_*` validators, two `_is_*` predicates, and one nested closure make up the compute-set legality layer. Each entry: native symbol + file offset, recovered rule, error raised, grounding. The op-membership *sets* the `_check_*_supported_ops` gates against live on `sema`; the rules below are what `neuron_isa` itself enforces.

The 24 break down as 3 matmul + 7 activation + 3 tensor_tensor + 8 tensor_scalar + 2 tensor_scalar_reduce + 1 cumulative. Two further `_is_*` *predicates* — `_is_tensor_tensor_gpsimd_engine_supported_op` and `_is_tensor_scalar_ptr_scalar_engine_supported_op_comb` — are wrapped by `_check_*` entries and appear below under their wrappers. `_check_tensor_reduce_bitvec_types` and the `free_axis` / `tensor_copy_dynamic` checks are cross-API helpers belonging to the reduce/DVE page ([6.4.2](isa-reduce-dve-dma.md)), and are counted there, not here.

### nc_matmul group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_nc_matmul_supported_dtypes` | `0x841f0` | input dtype ∈ `{float8_e4m3, float8_e5m2, bfloat16, float16, tfloat32, float32}`; result always FP32 | `err_instruction_unsupported_dtypes` / `_supported_dtypes` | CERTAIN (set = stub L923 verbatim) |
| `_check_nc_matmul_pe_tiling_position_size_supported_shape` | `0x2f660` | PE-tiling geometry; delegates to `sema.check_matmul_low_level_shape` (2D tuple/`()`, multiple-of-32, `tile_size ≥ stationary`, v2 32×32 ban) | `nki_assert` (tile messages) | CERTAIN (rules = stub L976–982) |
| `_check_nc_matmul_pe_tiling_position_size_supported_dtypes` | `0x2a840` | PE-tiling only legal for a low-precision dtype subset (FP8 tiling path) | `err_instruction_unsupported_dtypes` | HIGH |

### activation group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_activation_supported_ops` | `0x695d0` | `op` ∈ `sema` activation-function set (`absolute, arctan, expit, erf_dx, gelu_dx, gelu_apprx_sigmoid(_dx), gelu_apprx_tanh, silu_dx, square, rsqrt, reciprocal, …`) | `err_instruction_unsupported_op` | CERTAIN |
| `_check_activation_scale_type` | `0x2d7a0` | `scale` dtype **must be float32** | `err_activation_scale_invalid_type` | CERTAIN (stub L169) |
| `_check_activation_bias_type` | `0x86060` | `bias` dtype ∈ `{float32, float16, bfloat16}` | `err_activation_bias_invalid_type` | CERTAIN (stub L169) |
| `_check_activation_reciprocal_bias` | `0x5d0f0` | if `op == reciprocal` and `get_nc_version() == gen2` → `bias` must be `None` | `nki_assert("…reciprocal must have bias=None for trn1.")` | CERTAIN |
| `_check_activation_reduce_supported_reduce_ops` | `0x73f10` | `reduce_op` legal scalar-engine reduction; `negate` banned; v2 → `add` only | `err_instruction_unsupported_op` / `err_reduce_unsupported_negate` | CERTAIN |
| `_check_activiation_supported_reduce_cmd` *(sic)* | `0x32360` | delegates to `_check_supported_reduce_cmd(cmd, {idle, reset, reduce, reset_reduce})`; `load_reduce` illegal | `err_instruction_unsupported_op` | CERTAIN |
| `_check_supported_reduce_cmd` | `0x27fc0` | generic helper: `cmd ∉ supported` → raise (also used by the reduce set) | `err_instruction_unsupported_op` | CERTAIN |

> **GOTCHA —** `_check_activiation_supported_reduce_cmd` is **misspelled in the binary** ("activiation"). The interned name carries the typo; a reimplementation that probes the module namespace by the corrected spelling will not find it.

### tensor_tensor group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_tensor_tensor_supported_ops` | `0x67a30` | `op` ∈ vector-engine binary-op superset | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_tensor_vector_engine_supported_ops` | `0x58f30` | on Vector, `op` ∈ vector set; uses `promote_type(data1,data2)` and `is_tensor_tensor_gpsimd_engine` to split paths | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_tensor_gpsimd_engine_supported_ops` | `0x5a320` | on GpSimd, `op` must satisfy `_is_tensor_tensor_gpsimd_engine_supported_op` | `err_instruction_unsupported_op` | CERTAIN |
| `_is_tensor_tensor_gpsimd_engine_supported_op` *(predicate)* | `0x2c530` | `True` iff `op == power`, or `op ∈ {add, multiply, subtract}` with integer dtype | — | CERTAIN |

```c
// _is_tensor_tensor_gpsimd_engine_supported_op @0x2c530  — the GpSimd op rule
function is_gpsimd_op(op, dtype) -> bool:
    if op.name == "power":
        return True
    if op.name in {"add", "multiply", "subtract"}:
        return np.issubdtype(dtype, np.integer)   // checks dtype vs int32 / np.integer
    return False
```

### tensor_scalar group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_tensor_scalar_supported_ops` | `0x68870` | `op` ∈ `sema.tensor_scalar` op set | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_scalar_ptr_supported_ops` | `0x6afa0` | same, for the "ptr" (vector-operand) variant | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_scalar_ptr_gpsimd_engine_supported_ops` | `0x52660` | GpSimd-path op gate (`rsqrt`-only) | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_scalar_ptr_scalar_engine_supported_ops` | `0x74d10` | Scalar-path single-op gate | `err_instruction_unsupported_op` | CERTAIN |
| `_is_tensor_scalar_ptr_scalar_engine_supported_op_comb` *(predicate)* | `0x48b90` | `True` iff `(op0.name, op1.name)` ∈ `{(multiply,add), (multiply,None), (add,None)}` | — | CERTAIN |
| `_check_tensor_scalar_ptr_scalar_engine_supported_op_comb` | `0x5b710` | wraps the predicate above | `err_instruction_engine_unsupported_op_comb` | CERTAIN |
| `_check_tensor_scalar_both_arith_or_bitvec` | `0x4fc00` | `sema.is_arith_ops`/`is_bitvec_ops` on `op0` & `op1` must agree | `nki_assert("…must be both bitvec or both arithmetic operators.")` | CERTAIN |
| `_check_tensor_scalar_bitvec_dtype` | `0x53590` | if `is_bitvec_ops(op)`: `np.issubdtype(operand_dtype, integer)` (bool ok) | `err_bitvec_operand_must_be_integer` | CERTAIN |
| `_check_tensor_scalar_abs` | `0x41fb0` | if `op == abs`: engine ∈ `{vector, unknown}`; and `abs` unsupported on trn1 (target Sunda/penguin gate) | `nki_assert` (two abs messages) | CERTAIN |
| `_check_tensor_scalar_rsqrt` | `0x63140` | if `op == rsqrt`: engine ∈ `{gpsimd, unknown}`; plus an `op1` shape constraint | `nki_assert` (rsqrt messages) | CERTAIN |

```c
// _check_tensor_scalar_ptr_scalar_engine_supported_op_comb @0x5b710
//   wraps _is_…_op_comb @0x48b90 — the trn2 Scalar-Engine restriction
function check_scalar_engine_comb(inst_name, op0, op1):
    supported_comb = {("multiply","add"), ("multiply",None), ("add",None)}
    if (op0.name, op1.name) not in supported_comb:
        raise err_instruction_engine_unsupported_op_comb(inst_name, op0, op1)

// _check_tensor_scalar_both_arith_or_bitvec @0x4fc00
function check_both_same_family(inst_name, op0, op1):
    a0, a1 = sema.is_arith_ops(op0), sema.is_arith_ops(op1)
    b0, b1 = sema.is_bitvec_ops(op0), sema.is_bitvec_ops(op1)
    nki_assert((a0 and a1) or (b0 and b1),
               inst_name + " op0/op1 must be both bitvec or both arithmetic operators.")
```

### tensor_scalar_reduce group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_tensor_scalar_reduce_supported_ops` | `0x6bde0` | `op0` must be arithmetic (bitvec banned in this API) | `err_instruction_unsupported_op` | CERTAIN |
| `_check_tensor_scalar_reduce_supported_reduce_ops` | `0x6a280` | `reduce_op` ∈ `{add, subtract, multiply, max, min}` | `err_instruction_unsupported_op` | CERTAIN |

### tensor_scalar_cumulative group

| Validator | Offset | Rule | Raises | Confidence |
|---|---|---|---|---|
| `_check_tensor_scalar_cumulative_immediate_values` | `0x43bb0` | `imm0` (and `imm1` under `load_reduce`) must be FP32; nested `.check_dtype` closure does the per-immediate test | `nki_assert` (FP32 message) | CERTAIN |
| └ `…_immediate_values_check_dtype` *(closure)* | `0x28690` | per-immediate `dtype == float32` — the only separately-decompiled `__pyx_pf_…` body in the module | `nki_assert` | CERTAIN |

### Shared cross-API helper

`_check_tensor_reduce_bitvec_types` `@0x30490` — rule reconstructed from its error names rather than a traced body — requires an integer dtype for a bitvec reduce op, raising `err_reduce_bitvec_op_invalid_dtype` / `err_par_reduce_bitvec_op_invalid_dtype`. Used by the reduce-class lowering — owned by [6.4.2](isa-reduce-dve-dma.md), listed here only because the compute set references the same bitvec-dtype error family.

---

## Validator → Intrinsic Dispatch Matrix

Recovered from the interned `__pyx_n_s_*` names referenced in each `pw` body:

| Validator | Called by |
|---|---|
| `_check_nc_matmul_supported_dtypes` | `nc_matmul` |
| `_check_nc_matmul_pe_tiling_position_size_*_shape` | `nc_matmul` |
| `_check_nc_matmul_pe_tiling_position_size_*_dtypes` | `nc_matmul` |
| `_check_activation_supported_ops` | `activation`, `activation_reduce` |
| `_check_activation_scale_type` | `activation`, `activation_reduce` |
| `_check_activation_bias_type` | `activation`, `activation_reduce` |
| `_check_activation_reciprocal_bias` | `activation` |
| `_check_activation_reduce_supported_reduce_ops` | `activation`, `activation_reduce` |
| `_check_activiation_supported_reduce_cmd` | `activation` |
| `_check_tensor_tensor_supported_ops` | `tensor_tensor` |
| `_check_tensor_tensor_vector_engine_supported_ops` | `tensor_tensor` |
| `_check_tensor_tensor_gpsimd_engine_supported_ops` | `tensor_tensor` |
| `_check_tensor_scalar_bitvec_dtype` | `tensor_tensor`, `tensor_scalar`, `scalar_tensor_tensor` |
| `_check_tensor_scalar_supported_ops` | `tensor_scalar`, `scalar_tensor_tensor`, `tensor_tensor_scan` |
| `_check_tensor_scalar_ptr_supported_ops` | `tensor_scalar` |
| `_check_tensor_scalar_ptr_gpsimd_engine_supported_ops` | `tensor_scalar` |
| `_check_tensor_scalar_ptr_scalar_engine_supported_ops` | `tensor_scalar` |
| `_check_tensor_scalar_ptr_scalar_engine_supported_op_comb` | `tensor_scalar` |
| `_check_tensor_scalar_both_arith_or_bitvec` | `tensor_scalar`, `scalar_tensor_tensor` |
| `_check_tensor_scalar_abs` | `tensor_scalar`, `tensor_scalar_reduce`, `scalar_tensor_tensor` |
| `_check_tensor_scalar_rsqrt` | `tensor_scalar` |
| `_check_tensor_scalar_reduce_supported_ops` | `tensor_scalar_reduce` |
| `_check_tensor_scalar_reduce_supported_reduce_ops` | `tensor_scalar_reduce` |
| `_check_tensor_scalar_cumulative_immediate_values` | `tensor_scalar_cumulative` |

`nc_transpose` and `reciprocal` dispatch no `_check_*` in-body — their legality is enforced in `sema` lowering.

> **GOTCHA —** the `_check_tensor_scalar_*` validators are **shared** across `tensor_scalar`, `scalar_tensor_tensor`, `tensor_scalar_reduce`, and `tensor_tensor_scan` — the `tensor_scalar` name prefix names the *validator family*, not the intrinsic. `tensor_tensor_scan` reuses `_check_tensor_scalar_supported_ops` as its arith-op gate despite building a `tensortensor` node, not a `tensorvectorscalar` node.

---

## NeuronCore-Version Gating

Version-conditional rules in the compute validators, recovered via `get_nc_version()` / `nc_version` usage:

```text
gen2 = 2  (Trn1/Inf2)    gen3 = 3  (Trn2)    gen4 = 4  (Trn3)
```

| Rule | Gate | Validator |
|---|---|---|
| `reciprocal` (as activation op): `bias` must be `None` on gen2 | `get_nc_version() == gen2` | `_check_activation_reciprocal_bias` |
| `tensor_scalar` `abs`: not supported on trn1; engine ∈ Vector/Unknown | target Sunda/penguin gate | `_check_tensor_scalar_abs` |
| `tensor_scalar` `rsqrt`: engine ∈ GpSimd/Unknown | engine check | `_check_tensor_scalar_rsqrt` |
| `activation_reduce` `reduce_op`: `add`-only on v2 | v2 gate | `_check_activation_reduce_supported_reduce_ops` |
| `tensor_scalar` Scalar-Engine op combos: trn2 subset only | trn2 gate | `_check_tensor_scalar_ptr_scalar_engine_supported_op_comb` |
| `nc_matmul` PE-tiling: row & col `tile_size` both 32 banned on v2 | v2 gate | `sema.check_matmul_low_level_shape` |

> **QUIRK —** the `.rodata` error messages say **"trn1"** but the runtime gate compares `get_nc_version() == gen2`. These agree: in this codebase `gen2` *is* the Trn1/Inf2 generation. A reimplementer who maps "trn1" to a hypothetical `gen1` and gates on that will mis-fire every version-conditional rule.

---

## Evidence summary

Two independent artifacts back this page, and they agree.

The **offsets** come from `nm` on `neuronxcc/nki/isa/neuron_isa.cpython-310-x86_64-linux-gnu.so` (3,303,784 bytes), with an IDA database alongside it. Spot checks across the whole address range hold: `nc_matmul@0x61d50`, `nc_transpose@0x3b540`, `reciprocal@0x3a980`, `activation@0x65cb0`, `activation_reduce@0x5ff40`, `tensor_tensor@0x5e4e0`, `scalar_tensor_tensor@0x81200`, `_check_nc_matmul_supported_dtypes@0x841f0`, `_check_nc_matmul_pe_tiling_position_size_supported_shape@0x2f660`, `_check_activation_scale_type@0x2d7a0`, the misspelled `_check_activiation_supported_reduce_cmd@0x32360`, and `_check_tensor_scalar_abs@0x41fb0`. The Cython index labels (`_5activation`, `_79_check_nc_matmul_supported_dtypes`, `_99_check_activiation_supported_reduce_cmd`, …) are read straight from the `__pyx_pw_*` symbol table.

The **rules, signatures, enums, and dtype sets** come from the shipped `neuronxcc-stubs/nki/isa/__init__.pyi`, which is generated from the same source and is therefore ground truth for the API surface:

- All 11 compute signatures appear verbatim at L116, 206, 915, 1069, 1232, 1255, 1686, 1762, 1848, 1895, 1975.
- L28–47 gives the `engine` ordinals `tensor=1 vector=5 scalar=2 gpsimd=3 dma=4 sync=6 unknown=0`; L88–100 gives `reduce_cmd` as `idle=0 reset=1 reduce=2 reset_reduce=3 load_reduce=4`. `reduce=2` / `reset_reduce=3` are declared out of numeric order in the stub, but the values are as listed.
- L1716–1718 lists the trn2 Scalar-Engine combos `(multiply,add)`, `(multiply,None)`, `(add,None)` — exactly the `_is_…_op_comb` set.
- L923 enumerates the matmul dtype set; L976–982 lists the five tiling requirements, including "multiple of 32" and "both row & col cannot be 32 for NeuronCore-v2".
- L1799–1800 states the cumulative `{BF16,FP16,FP32,FP8,UINT8,UINT16,INT8,INT16}` restriction and "INT32/UINT32 are not supported (ISA limitation)"; L1814–1816 require `imm0`/`imm1` to be FP32.

---

## What Cython Obscured

- The literal membership *sets* behind `_check_*_supported_ops` are `sema` attributes, not in-module constants; the op-name vocabulary on this page is the full string-table vocabulary, but the precise per-engine partition lives in the `sema` layer.
- `pw` bodies are dominated by CPython call machinery; exact positional-vs-keyword forwarding into `sema.<method>` is inferred from interned-name *order*, which is dependable for the argument names themselves and weaker for their exact order.
- `"local variable allocation has failed, the output may be wrong!"` heads several decompiled bodies (`nc_matmul`, `tensor_tensor` vector ops). Structural facts — which globals are fetched, which `sema` method is called, which error strings are referenced — survive that warning intact; fine-grained intra-validator control flow does not, and is reconstructed.

---

## Related Components

| Name | Relationship |
|---|---|
| `sema` / IRBuilder (Part 6.5.1 forward builder) | owns `matmult`/`activation`/`tensortensor`/`tensorvectorscalar`/`tensorscalarcumulative` builders, the op-class predicates, the supported-op sets, `promote_type`, `check_matmul_low_level_shape` |
| penguin.ir wire encoding | the Matmul/TensorTensor/TensorScalar/Activation/TensorScalarCumulative nodes these build lower to BIR/wire opcodes |
| dtype/memtype enums | the dtype vocabulary and SBUF/PSUM placement constraints surfaced as docstring rules |

## Cross-References

- [nki.isa reduce / DVE / DMA Intrinsics](isa-reduce-dve-dma.md) — the sibling half: `reduce`, `select`, `range_select`, `iota`, copies, DMA, and the shared `_check_supported_reduce_cmd` / `_check_tensor_reduce_bitvec_types` helpers
- NKI Forward Builder (Part 6.5.1) — the `nki_ctx`/`sema` IR-builder layer these intrinsics call into
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the SBUF-in/PSUM-out placement rules the compute ops obey
- TPB Engines and LNC (Part 1, arch/) — the Tensor/Scalar/Vector/GpSimd engine model behind the engine-legality matrix
