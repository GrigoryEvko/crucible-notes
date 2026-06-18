# nki.isa REDUCE / SELECT / DVE / MEMORY / DMA Intrinsics

> *All symbols, line numbers, enum values, and error strings on this page apply to neuronx_cc 2.24.5133.0+58f8de22, module `neuronxcc.nki.isa.neuron_isa` (Cython-compiled from `neuronxcc/nki/isa/neuron_isa.py`). Other versions will differ.*

## Abstract

`nki.isa` (the user-facing alias `nisa`) is a single Cython module, `neuron_isa`, that exposes the Neuron ISA as Python callables. Each `nisa.*` intrinsic is a thin Python `def` whose compiled `__pyx_pw_*` wrapper does three things in order: parse the keyword-only argument table, run a handful of local `_check_*` validators plus `sema.assert_*` helpers, then delegate IR-node construction to a builder method on `self.nki_ctx`. The intrinsic itself owns **legality**, not lowering — the penguin IR `Operator` node is built downstream in the IRBuilder (`nki.compiler.backends.neuron.nki_ctx`). This page is the data-movement and reduction half of that surface; the compute half (`tensor_scalar`, `tensor_tensor`, `activation`, `nc_matmul`, the scan/cumulative ops) is documented in [6.4.1 isa-compute-intrinsics](isa-compute-intrinsics.md).

The reader who has internalised LLVM intrinsics should think of these as *legality-checked builder calls*: the analogue of `IRBuilder::CreateCall` plus a verifier, except the verifier runs at trace time in Python and the "intrinsic" is a hardware-engine micro-op (Vector / GpSimd / DMA). The interesting content is not the lowering — it is the per-op legality: which `reduce_cmd` accumulator commands are legal for which select op, which dtypes a bitvec reduce accepts, the full Hardware-DGE legality table for `dma_transpose`, and a cluster of arch-gated rules (`get_nc_version()`) that silently differ across Trn1 / Trn2 / Trn3. Three subtle off-by-ones in those legality sets are surfaced as CORRECTION/GOTCHA callouts.

For reimplementation, the contract is:

- **The 22-intrinsic catalog**: each op's signature → validators → IR node, with the real builder/validator symbol named.
- **The `reduce_cmd` validator family** (`_check_supported_reduce_cmd` and its three per-op wrappers) and the *exact* legal set per select op — these differ and the differences are the trap.
- **The `dma_transpose` HW-DGE / SW-DGE legality table**, reproduced verbatim from the binary's error strings, plus the `get_nc_version() >= gen3` HWDGE gate shared with `dma_copy`.
- **The dtype legality sets** for bitvec reduce (`tensor_reduce` vs `tensor_partition_reduce`), `select_reduce`, `tensor_copy_predicated`, and the dynamic-copy Trn1-Scalar prohibition.

| | |
|---|---|
| **Module** | `neuronxcc.nki.isa.neuron_isa` (Cython `.cpython-31x-x86_64-linux-gnu.so`) |
| **Source file** | `neuronxcc/nki/isa/neuron_isa.py` (line numbers from `_TraceSetupAndCall` args) |
| **Signatures** | CONFIRMED — `neuronxcc-stubs/nki/isa/__init__.pyi` |
| **Builder receiver** | `self.nki_ctx` (IRBuilder), `self.sema` (error/assert helper) |
| **IR target** | `neuronxcc.starfish.penguin.ir.Operator` nodes (built in `nki_ctx`) |
| **Arch gate** | `get_nc_version()` → `nc_version` IntEnum: `gen2=2` (Trn1/Inf2), `gen3=3` (Trn2), `gen4=4` (Trn3) |

---

## The Common Wrapper Skeleton

### Purpose

Every `nisa.*` body is generated from the same Cython template, so learning it once removes 90% of the per-op noise. The skeleton is the same regardless of whether the op is a one-line forward (`max8`, `iota`) or the 254 KB inline-validated monster (`select_reduce`).

### Algorithm

```c
function nisa_op(args..., *, mask=None, dtype=None):   // __pyx_pw_* wrapper
    parse_kwargs(args)                  // matches stub: kw-only after '*', defaults from __pyx_pf_*__defaults_*
    self     = bound module context
    nki_ctx  = self.nki_ctx             // IRBuilder — constructs penguin IR nodes
    sema     = self.sema                // owns every err_* / assert_* helper
    nki_ctx.cur_api      = op           // stamp user-facing name into context for
    nki_ctx.cur_api_name = "<op>"       //   error messages + IR provenance

    // --- VALIDATORS RUN FIRST ---
    sema.assert_not_none(<required tiles>)
    _check_*(...)                       // local dtype/engine/reduce_cmd legality
    // --- THEN BUILD ---
    node = nki_ctx.<builder>(args, mask=mask, dtype=dtype)   // penguin IR Operator
    return node                         // a tile handle
```

### Shared Helpers

`mask` is a **compile-time predicate**, threaded into the IR node as the instruction predicate (`nki_mask`); it is not a runtime branch. `dtype` is the output cast; default is the input dtype. The validators draw from a fixed pool of `sema` helpers — names below are CONFIRMED rodata identifiers:

| Family | Helpers (identifiers in `.so` rodata) |
|---|---|
| Asserts | `assert_not_none`, `assert_same_shape`, `assert_max_dimensions`, `assert_tile_shapes`, `assert_tile_has_expected_shape`, `assert_constant_value`, `assert_dma_input_length`, `nki_assert` |
| Shape/dtype util | `extract_buffer_shape`, `normalize_dim`, `promote_type`, `is_int_type`, `is_number`, `dtype2str`, `sizeinbytes` |
| Errors (`err_*` — each formats + raises a NKI compile error) | `err_instruction_supported_dtypes`, `err_instruction_unsupported_dtypes`, `err_instruction_unsupported_op`, `err_instruction_engine_unsupported_op_comb`, `err_reduce_unsupported_negate`, `err_reduce_bitvec_op_invalid_dtype`, `err_par_reduce_unsupported_op`, `err_par_reduce_bitvec_op_invalid_dtype`, `err_min_arch_support_for_hwdge` |

> **NOTE —** the numeric thresholds in the docstrings (`local_gather` ≤ 4096 indices, `bn_stats` ≤ 512 free elems, `max8` 8..16384 elems) are CONFIRMED from docstrings but the *runtime check* lives in `nki_ctx`, not in this module's `_check_*`. Treat them as IRBuilder-enforced (INFERRED location), legal-value CONFIRMED.

---

## Free-Dim Reductions

### tensor_reduce

```text
tensor_reduce(op, data, axis, *, mask=None, dtype=None, negate=False, keepdims=False)
  signature: CONFIRMED stub L1614 | source: neuron_isa.py:487 | engine: Vector
  builds: nki_ctx.tensorreduce(...)   (node name "tensorreduce" CONFIRMED n_s ref @ body 0x4c440)
```

Free-dimension reduction; the partition axis is preserved. The reduction *axis set* is normalised and validated by `_check_and_translate_free_axis` (`neuron_isa.py:2826`), which delegates to `_check_free_axis` (`:2810`). The axis rule (CONFIRMED docstring + the `Reduction on partition axes is not supported` rodata string):

> **GOTCHA —** axes must be **free** dims (axis 0 = partition is illegal), **consecutive**, and start at the most-minor free axis 1. Legal: `[1]`, `[1,2]`, `[1,2,3]`, `[1,2,3,4]` (≤ 4 axes). Illegal: `[1,3,4]` (gap), `[2,3]` (does not start at 1), `[0,...]` (partition). Empty axis → `"Empty axis not supported"`.

```c
function tensor_reduce(op, data, axis, ...):           // neuron_isa.py:487
    sema.assert_not_none(data)
    axis = _check_and_translate_free_axis(axis, data)  // :2826 -> :2810; gives reduce_dims
    _check_tensor_reduce_supported_op(op)              // op legality
    _check_tensor_reduce_bitvec_types(op, dtype)       // :3130 — bitvec dtype gate, below
    if negate and is_bitvec(op):
        sema.err_reduce_unsupported_negate(...)        // negate is ARITHMETIC-only
    return nki_ctx.tensorreduce(op, reduce_dims=axis, negate=negate,
                                keepdims=keepdims, mask=mask, dtype=dtype)
```

**Two op classes** (CONFIRMED docstring + `bitvec_opcodes` set):

- **bitvec ops** (`bitwise_and/or/xor`, `logical_and/or/xor`): NO casting; elements treated as bit patterns; input/output must be integer.
- **arithmetic ops** (`add`/`subtract`/`multiply`/`max`/`min`/…): engine casts to fp32, reduces in fp32, casts result to `dtype`. `negate` multiplies the fp32 result by `-1.0` at no extra cost — and is legal **only** here.

`_check_tensor_reduce_bitvec_types` (`neuron_isa.py:3130`, body `0x30490`) — CONFIRMED control flow:

```c
function _check_tensor_reduce_bitvec_types(op, dtype):  // :3130
    if op in bitvec_opcodes and dtype not in {int32, uint8, uint16, uint32}:
        sema.err_reduce_bitvec_op_invalid_dtype(op, dtype, name, ...)
```

### tensor_partition_reduce

```text
tensor_partition_reduce(op, data, *, mask=None, dtype=None)
  signature: CONFIRMED stub L1595 | source: neuron_isa.py:583 | engine: GpSimd
  builds: nki_ctx.tensor_partition_reduce(...)
```

The cross-partition counterpart to `tensor_reduce`: it reduces **across** partitions on the GpSimd engine. Body `0x76320` literally probes `np.add`, `np.sum`, `np.max`, `np.maximum`, `np.bitwise_and`, `np.bitwise_or` attributes in that order (n_s refs lines 778–994), so the supported set is exactly `{add, max, bitwise_and, bitwise_or}`.

```c
function tensor_partition_reduce(op, data, ...):       // neuron_isa.py:583
    if op not in {add, max, bitwise_and, bitwise_or}:
        sema.err_par_reduce_unsupported_op(op, ...)
    if op in {bitwise_and, bitwise_or} and dtype not in {uint8, uint16, uint32}:
        sema.err_par_reduce_bitvec_op_invalid_dtype(op, dtype, ...)
    return nki_ctx.tensor_partition_reduce(op, data, mask=mask, dtype=dtype)
```

> **GOTCHA — partition-reduce bitvec is uint-only.** `tensor_reduce` allows `int32` for bitvec ops; `tensor_partition_reduce` does **not** — its bitvec dtype set is `{uint8, uint16, uint32}` with no `int32`. A reimplementation that reuses one dtype set for both reduce flavours is wrong. (STRONG — body checks only the three uint widths.)

---

## bn_stats / bn_aggr — BatchNorm Statistics

### bn_stats

```text
bn_stats(data, *, mask=None, dtype=None)
  signature: CONFIRMED stub L356 | source: neuron_isa.py:2501 | engine: Vector
  builds: sema.bn_stats_inst(data, mask, dtype)   (node "bn_stats_inst" CONFIRMED n_s @ body 0x376e0)
```

Per-partition mean/variance accumulator, computed in fp32. The output is **6 elements per partition** — two `(count, mean, variance*count)` triples, one for the *even* input elements and one for the *odd* (CONFIRMED docstring L361–368):

```text
out[par] = [ count_even, mean_even, var_even*count_even,
             count_odd,  mean_odd,  var_odd*count_odd ]
```

> **NOTE —** the even/odd split is a hardware artifact: the Vector engine accumulates two interleaved streams. `bn_aggr` is what folds them (and any further tiles) into a single mean/variance. Free-dim size of `data` must be ≤ 512 (`nl.tile_size.bn_stats_fmax`) — to BatchNorm > 512 free elements, issue multiple `bn_stats` and aggregate.

### bn_aggr

```text
bn_aggr(data, *, mask=None, dtype=None)
  signature: CONFIRMED stub L320 | source: neuron_isa.py:2558 | engine: Vector
  builds: sema.bn_aggr_inst(data, mask, dtype)   (node "bn_aggr_inst" CONFIRMED n_s @ body 0x36be0)
```

Aggregates one or more `bn_stats` outputs into **2 elements per partition** — `(mean, variance)`. Because each `bn_stats` triple is `(count, mean, variance*count)`, the input's free dimension must be a **multiple of 3** (CONFIRMED docstring). fp32 compute, cast out to `dtype` (default = input dtype).

---

## DVE Search Ops — max8 / find_index8 / match_replace8

These three are the Vector engine's "search" micro-ops, sharing the per-partition top-8 / find-8 hardware. Their wire encoding is documented in [2.16 dve-search-encoding](../isa/dve-search-encoding.md); this section gives the legality the trace layer enforces. All three are thin wrappers (the heavy work is in `nki_ctx`), but their shape/dtype envelopes differ.

### max8

```text
max8(*, src, mask=None, dtype=None)
  signature: CONFIRMED stub L704 | source: neuron_isa.py:2124 | engine: Vector
  builds: nki_ctx.max8(src, mask=mask, dtype=dtype)   (thin wrapper, body 0x73330)
```

Reads `src`, converts to fp32 internally, outputs the **8 largest values in DESCENDING order** per partition. `src` up to 5-D; output always 2-D `[par_dim, 8]`; 8..16384 elements per partition; `src` and output share partition-dim size; default out dtype = `src` dtype.

### nc_find_index8

```text
nc_find_index8(*, data, vals, mask=None, dtype=None)
  signature: CONFIRMED stub L776 | source: neuron_isa.py:2162 | engine: Vector
  builds: nki_ctx.nc_find_index8(...)   (thin wrapper)
```

For each partition, finds the index (from 0) of the **first occurrence** of each of the 8 `vals` in `data`. `data` up to 5-D, 8..16384 elem/part; `vals` up to 3-D, **exactly 8** elem/part; output `[par_dim, 8]` with dtype `uint16` OR `uint32` (default `uint32`). `mask` applies to `data` only.

### nc_match_replace8

```text
nc_match_replace8(*, data, vals, imm, dst_idx=None, mask=None, dtype=None)
  signature: CONFIRMED stub L816 | source: neuron_isa.py:2207 | engine: Vector
  builds: nki_ctx.nc_match_replace8(...)   (body 0x6e7a0)
```

Replaces the first occurrence of each `vals` element in `data` with the immediate `imm`; returns the replaced tensor. If `dst_idx` is given, the matched indices are written there. Validation: `assert_same_shape(...)` over the `data`/`vals` partition dims; free dims are flattened for matching but preserved in the outputs. `data` up to 5-D, ≤ 16384 elem/part; `vals` up to 3-D, exactly 8 elem/part; replaced output same shape as `data`.

> **NOTE —** the `8` is not a parameter — it is fixed silicon width across all three ops. `find_index8`/`match_replace8` *require* exactly 8 `vals`; passing any other count is a shape error, not a degenerate case.

---

## The Range / Select Reduce Family

`range_select` and `select_reduce` are the two DVE select-with-reduce ops. Both run on the Vector engine, both can chain into the engine's accumulator registers via `reduce_cmd`, and both share the same accumulator hardware — which is exactly why their `reduce_cmd` legality sets differ (the trap below). Their DVE datamove encoding is in [2.17 dve-datamove-encoding](../isa/dve-datamove-encoding.md).

### range_select

```text
range_select(*, on_true_tile, comp_op0, comp_op1, bound0, bound1,
             reduce_cmd=reduce_cmd.idle, reduce_res=None, reduce_op=np.max,
             range_start=0, on_false_value=fp32.min, mask=None, dtype=None)
  signature: CONFIRMED stub L1133 | source: neuron_isa.py:1456 | engine: Vector (FORCED)
  availability: NeuronCore-v3+ ONLY (CONFIRMED docstring L1140)
  builds: range_select node + range_select_info descriptor
          (import neuronxcc.include.isa.range_select_info — CONFIRMED)
```

A **double-bound range test** on the free-dimension coordinate: for each element, compare `range_start + free_index` against `bound0` and `bound1` (in fp32), and where both comparisons hold, copy `on_true_tile`, else write `on_false_value`. The body forces `nisa_engine.vector` (CONFIRMED n_s `{nisa_engine, vector}`).

```c
function range_select(*, on_true_tile, comp_op0, comp_op1, bound0, bound1, ...):  // :1456
    cur_api_name = "range_select"
    _check_range_select_tiles(on_true_tile, bound0, bound1, ...)   // :2980
        // partition-dim sizes must match across on_true_tile/bound0/bound1
        // bound0,bound1: exactly ONE element per partition (per-partition scalar)
        // on_true_tile: FP dtype; bound0,bound1: FP32
    _check_range_select_supported_reduce_cmd(reduce_cmd)           // :2995
        // LEGAL = {idle, reduce, reset, reset_reduce}  (NO load_reduce)
    return build_range_select(on_true_tile, comp_op0, comp_op1, bound0, bound1,
        reduce_cmd, reduce_res, reduce_op, range_start, on_false_value,
        engine=vector, mask=mask, dtype=dtype)
```

The numpy equivalent (CONFIRMED docstring L1192):

```python
indices = range_start + np.arange(on_true_tile[0].size)        # computed in fp32
mask    = comp_op0(indices, bound0) & comp_op1(indices, bound1)
out     = np.where(mask, on_true_tile, on_false_value)
reduce_tile = reduce_op(out, axis=1, keepdims=True)            # if reduce_cmd != idle
```

Legal `comp_op0/1` (CONFIRMED): `{np.equal, np.less, np.less_equal, np.greater, np.greater_equal}`. `reduce_op`: only `np.max`.

> **GOTCHA — `on_false_value` MUST be the FP32_MIN bit pattern (`fp32.min`).** This is a hardware constraint, hence the default. The docstring spells out the numerical-stability reason: in the attention pattern `range_select → reduce_res → activation`, a full row of fill produces `FP32_MIN - FP32_MIN = 0` (stable) only if `reduce_res` is FP32. If `dtype` is FP16/BF16/FP8 the fill downcasts to `-INF`, and you must keep `reduce_res` FP32 so `activation` (which upcasts to fp32) sees `-INF - FP32_MIN = -INF`, stable. `range_start + free_index` must stay within `2^24` (fp32 exact-integer range).

### select_reduce

```text
select_reduce(*, dst, predicate, on_true, on_false, reduce_res=None,
              reduce_cmd=reduce_cmd.idle, reduce_op=np.max, reverse_pred=False,
              mask=None, dtype=None)
  signature: CONFIRMED stub L1298 | source: neuron_isa.py:1591 | engine: Vector
  builds: nki_ctx.<select_reduce builder>(...)   (largest body 0x78670, ~254KB decompiled)
```

`select_reduce` is the most heavily inline-validated op in the module — its predicate select is `where(reverse_pred ? ~predicate : predicate, on_true, on_false)`, with an optional `reduce_res = max(result, axis=1, keepdims=True)`. It shares the Vector accumulator registers with `range_select`.

```c
function select_reduce(*, dst, predicate, on_true, on_false, ...):   // :1591
    cur_api_name = "select_reduce"
    sema.assert_not_none(dst, predicate, on_true, on_false)

    // 1. on_true dtype: may be ANY supported dtype EXCEPT {tfloat32, int32, uint32}
    if on_true.dtype in {tfloat32, int32, uint32}:
        sema.err_instruction_unsupported_dtypes(...)

    // 2. predicate must be an INTEGER tile
    if predicate.dtype not in {int8, uint8, int16, uint16, int32, uint32}:
        sema.err_instruction_unsupported_dtypes(...)

    // 3. on_false: scalar => float32, else vector (on_true.shape[0], 1)
    if is_number(on_false):
        if on_false.dtype != float32:
            raise "on_false must be float32 when provided as a scalar, got ..."
    else:
        if on_false.shape[1] != 1:
            raise "on_false's free dimension should be 1 (a vector), got ..."

    // 4. shape match: on_true, dst, predicate identical
    assert_tile_has_expected_shape(...); assert_max_dimensions(...)

    // 5. dst dtype must equal the instruction's computed output dtype
    if dst.dtype != out_dtype:  raise "dst dtype must match instruction ..."

    // 6. reduce_res (if given): vector (on_true.shape[0],1), FLOAT dtype; reduce_op=max only
    // 7. reduce_cmd legality
    _check_select_reduce_supported_reduce_cmd(reduce_cmd)   // :3000
        // LEGAL = {idle, reduce, reset_reduce}  (NO reset, NO load_reduce)

    return build_select_reduce(dst, predicate, on_true, on_false, reduce_res,
        reduce_cmd, reduce_op, reverse_pred, mask=mask, dtype=dtype)
```

Every `raise` string above is a CONFIRMED literal in body `0x78670` (`pyx_kp_u_on_false_must_be_float32_when_p`, `pyx_kp_u_on_false_s_free_dimension_should`, `pyx_kp_u_dst_dtype_must_match_instructio`). MEMORY (CONFIRMED docstring): `on_true` and `predicate` may both be SBUF; at most one of them in PSUM; `dst` in SBUF or PSUM.

---

## reduce_cmd — The Accumulator Legality Gate

### Purpose

`reduce_cmd` selects what the op does with the Vector engine's accumulator registers. Its five values (CONFIRMED stub L85, `reduce_cmd(IntEnum)`) are the same enum across the whole DVE select family, but **each op accepts a different subset**. The gate is a single membership test against a per-op `supported` set.

```text
reduce_cmd: idle=0, reset=1, reduce=2, reset_reduce=3, load_reduce=4
  idle         = accumulator registers unused
  reset        = reset accumulators to initial state
  reduce       = keep accumulating over current accumulator value
  reset_reduce = reset, then immediately accumulate this instruction's result
  load_reduce  = load a value into accumulators, then accumulate this instruction
```

### Algorithm

```c
function _check_supported_reduce_cmd(inst_name, reduce_cmd, supported):  // :3005, body 0x27fc0
    if reduce_cmd not in supported:          // PySequence_Contains(supported, reduce_cmd)
        sema.err_instruction_unsupported_op(name=inst_name)
```

Each caller just builds its `supported` tuple and forwards to the core. The three per-op wrappers (each CONFIRMED from its body's n_s set):

| Validator | Source | Legal `reduce_cmd` set | Excludes |
|---|---|---|---|
| `_check_range_select_supported_reduce_cmd` | `:2995`, body `0x31530` | `{idle, reduce, reset, reset_reduce}` | `load_reduce` |
| `_check_select_reduce_supported_reduce_cmd` | `:3000`, body `0x2b8c0` | `{idle, reduce, reset_reduce}` | `reset`, `load_reduce` |
| `_check_activiation_supported_reduce_cmd` *(sic)* | `:2990`, body `0x32360` | `{idle, reduce, reset, reset_reduce}` | `load_reduce` (used by `activation*` — sibling [6.4.1](isa-compute-intrinsics.md)) |

> **GOTCHA — `select_reduce` is strictly narrower than `range_select`.** `range_select` allows a bare `reset` (set accumulators without immediately reducing); `select_reduce` does **not** — its set is `{idle, reduce, reset_reduce}` only. The reason mirrors the hardware: `select_reduce` always wants a *defined* reset-or-continue (`reset_reduce` fuses both), never a bare `reset` that leaves the accumulator in an indeterminate "reset but not yet reduced" state. A reimplementation that shares one `reduce_cmd` whitelist across both ops will incorrectly accept `range_select(reduce_cmd=reset)` for `select_reduce`.

> **CORRECTION (REDUCE-1) —** `load_reduce` (=4) is never legal for any of these three DVE select validators. It is reserved for the `tensor_scalar_cumulative` path (where `reset_reduce` is the default), which is sibling [6.4.1](isa-compute-intrinsics.md) territory. Any draft that lists `load_reduce` as a legal `range_select`/`select_reduce` command is wrong — the binary's `supported` tuples contain only the four/three values above.

> **CORRECTION (REDUCE-2) —** the validator name `_check_activiation_supported_reduce_cmd` is misspelled in the source ("activiation"). This is a genuine identifier in the binary's `_TraceSetupAndCall` table at `neuron_isa.py:2990`, not a transcription error. A reimplementer grepping for `_check_activation_supported_reduce_cmd` will miss it.

---

## Affine / Predicated Copy Ops

### affine_select

```text
affine_select(pred, on_true_tile, on_false_value, *, mask=None, dtype=None)
  signature: CONFIRMED stub L268 | source: neuron_isa.py:1389 | engine: GpSimd
  builds: nki_ctx.affine_select(pred, on_true_tile, on_false_value, mask, dtype)  (thin, body 0x49510)
```

`out[x] = (pred[x] > 0) ? on_true_tile[x] : on_false_value`. The body is a pure forward — n_s set is exactly `{pred, on_true_tile, on_false_value, mask, dtype, nki_ctx, affine_select}`, no local `_check_*`. The predicate is lowered in `neuronxcc.nki.compiler.backends.neuron.predicates`.

> **GOTCHA — `pred` is a single AFFINE expression, evaluated per-element on the GpSimd engine, not a materialized boolean tile.** It is built from `nl.arange()` / index algebra; it must be compile-time resolvable (no runtime vars) and **cannot** be a boolean combination (`&`/`|`) of masks. For richer predicates the docs route users to `nl.where`. DTYPE: `on_true_tile`/output may be any float or 8/16-bit int, but `on_false_value` MUST be float32 regardless of the tile dtypes (CONFIRMED docstring + the `on_false` fp32 error string).

### tensor_copy_predicated

```text
tensor_copy_predicated(*, src, dst, predicate, reverse_pred=False, mask=None, dtype=None)
  signature: CONFIRMED stub L1542 | source: neuron_isa.py:1909 | engine: Vector
  builds: nki_ctx.tensor_copy_predicated(...)  (body 0x7e1f0)
```

Conditional copy: where `predicate` is True copy `src → dst` (or a scalar `src`); where False, `dst` is unchanged. Five inline error strings (all CONFIRMED literals in body `0x7e1f0`):

```c
function tensor_copy_predicated(*, src, dst, predicate, reverse_pred=False, ...):  // :1909
    sema.assert_not_none(src, dst, predicate)
    if predicate.dtype not in {uint8, uint16, uint32}:
        raise "`predicate` must be a boolean or integer tile"
    if is_tensor(src) and src.dtype != dst.dtype:
        raise "`src` and `dst` must have the same dtype"
    if is_number(src) and is_float(dst) and not is_float(src):
        raise "Scalar `src` must be float if `dst` is float"
    if reverse_pred and not is_number(src):
        raise "`reverse_pred` only supported if `src` is scalar input."
    if dst.dtype != out_dtype:
        raise "dst dtype must match instruction ..."
    return nki_ctx.tensor_copy_predicated(src, dst, predicate, reverse_pred, mask, dtype)
```

MEMORY: `src` and `predicate` both SBUF is fine; at most one in PSUM (CONFIRMED docstring). `reverse_pred` being scalar-only is the trap — it is *not* a general "invert the predicate" knob.

---

## Memory / Constant Generation

### memset

```text
memset(shape, value, dtype, *, mask=None, engine=engine.unknown)
  signature: CONFIRMED stub L740 | source: neuron_isa.py:1817 | engine: Vector or GpSimd
  builds: nki_ctx.memset(shape, value, dtype, mask, engine)  (thin, body 0x4b520)
```

Fills a tile with a **compile-time constant** `value`. Ignores `nl.par_dim()`; `shape[0]` is always the partition axis. `engine` may be `vector_engine | gpsimd_engine | unknown_engine` (default — compiler picks). Supports all valid NKI dtypes; `value=0` with bf16/fp16 output is the cheap path.

### iota

```text
iota(expr, dtype, *, mask=None)
  signature: CONFIRMED stub L602 | source: neuron_isa.py:1281 | engine: GpSimd
  builds: index_value_inst   (CONFIRMED n_s @ body 0x39dd0)
```

Materializes a constant index pattern in SBUF from an affine `nl.arange()`-based `AccessPattern` `expr`, computed in int32, then cast to `dtype` for free (GpSimd cast). No local `_check_*` — `expr` legality is enforced by the `AccessPattern` machinery in `nki_ctx`. n_s set: `{expr, dtype, index_value_inst, mask, nki_ctx}`.

### dropout

```text
dropout(data, prob, *, mask=None, dtype=None)
  signature: CONFIRMED stub L548 | source: neuron_isa.py:1312 | engine: Vector
  builds: nki_ctx.dropout(data, prob, mask, dtype)  (thin)
```

Per-element Bernoulli zeroing with drop-probability `prob`. `prob` may be a scalar const OR a tile of shape `(data.shape[0], 1)` (per-partition prob). Dtype rules (CONFIRMED docstring, enforced in ctx): if `data` is **integer**, `prob` must be float32 **and** the output dtype must equal `data` dtype; if `data` is **float**, `prob` may be any float type and the output may be any valid type.

### sequence_bounds

```text
sequence_bounds(*, segment_ids, dtype=None)   [NOTE: no mask param]
  signature: CONFIRMED stub L1384 | source: neuron_isa.py:1775 | engine: GpSimd
  builds: nki_ctx.sequence_bounds(segment_ids, dtype)  (thin, body 0x29010)
```

For each element returns `[start_index, end_index]` of its segment. Padding (`segment_id == 0`) → `start = n` (len of `segment_ids`), `end = -1`. Partition dim MUST be 1; input `(1, N)` → output `(1, 2, N)`. `segment_ids` dtype float32 or int32; out dtype float32 or int32 (default = input dtype).

> **GOTCHA —** `sequence_bounds` is the *only* op in this catalog with no `mask` parameter. Its signature is `(*, segment_ids, dtype=None)` — do not add `mask` by analogy.

---

## On-Chip Copy Ops

### tensor_copy and dynamic variants

```text
tensor_copy(src, *, mask=None, dtype=None, engine=engine.unknown)              # :1857, stub L1424
tensor_copy_dynamic_src(src, *, mask=None, dtype=None, engine=engine.unknown)  # :2328, stub L1499
tensor_copy_dynamic_dst(*, dst, src, mask=None, dtype=None, engine=...)        # :2383, stub L1467
```

`tensor_copy` is an on-chip SRAM copy via Vector / Scalar / GpSimd — all three compute engines can copy (their cast behaviour differs). For SBUF→SBUF prefer this over `dma_copy`. The dynamic variants copy with `src` (resp. `dst`) at a **dynamic per-partition offset** (a scalar offset in SBUF), and both gate the engine through `_check_tensor_copy_dynamic_supported_engine`:

```c
function _check_tensor_copy_dynamic_supported_engine(engine, inst_name):  // :3191, body 0x2e6e0
    if get_nc_version() == gen2 and engine == nisa_engine.scalar:          // gen2 = Trn1
        nki_assert(False, "<inst> is not supported on trn1 Scalar Engine.")
```

> **GOTCHA — Scalar-engine dynamic copy is ILLEGAL on Trn1 (gen2).** The check is exact: `get_nc_version() == gen2` *and* `engine == scalar`. It is legal on gen3+ (Trn2/Trn3); Vector and GpSimd are legal everywhere. The error string `pyx_kp_u_is_not_supported_on_trn1_Scalar` is a CONFIRMED literal. Note this is an *equality* on `gen2`, not a `< gen3` range — but they coincide because gen2 is the minimum.

---

## DMA Ops

Both DMA ops gate Hardware DGE on `get_nc_version() >= gen3` through the *same* helper, `sema.err_min_arch_support_for_hwdge`. The interesting part is `dma_transpose`'s full legality table.

### dma_copy

```text
dma_copy(*, dst, src, mask=None, dst_rmw_op=None, oob_mode=oob_mode.error, dge_mode=dge_mode.unknown)
  signature: CONFIRMED stub L414 | source: neuron_isa.py:2428 | engine: DMA
  builds: nki_ctx DMA-copy node  (body 0x3c080)
```

HBM↔SBUF copy. Cast happens automatically if src/dst dtypes differ.

```c
function dma_copy(*, dst, src, dst_rmw_op=None, oob_mode=error, dge_mode=unknown):  // :2428
    if dge_mode == hwdge and get_nc_version() < gen3:        // gen2/Trn1
        sema.err_min_arch_support_for_hwdge(...)             // HWDGE is NeuronCore-v3+ only
    // dst_rmw_op: read-modify-write at destination; ONLY np.add supported (None = overwrite)
    //   if dst_rmw_op set, ONLY oob_mode.error is allowed
    return build_dma_copy(src, dst, mask, dst_rmw_op, oob_mode, dge_mode)
```

`oob_mode` (`error=0` raise on OOB, `skip=1` silently skip) applies to indirect gather/scatter. When `dst_rmw_op` is set, only `oob_mode.error` is legal.

### dma_transpose

```text
dma_transpose(src, *, axes=None, dge_mode=dge_mode.unknown, mask=None, dtype=None, oob_mode=oob_mode.error)
  signature: CONFIRMED stub L477 | source: neuron_isa.py:2003 | engine: DMA
  builds: (indirect path) IndirectMemrefTileND tile  (CONFIRMED n_s)
```

Transpose via the DMA engine. Permutations (CONFIRMED docstring): 2-D → `[1,0]`; 3-D → `[2,1,0]`; 4-D → `[3,1,2,0]`. Legal `axes`: `(1,0)`, `(2,1,0)`, `(3,1,2,0)`. The HWDGE gate is the same `>= gen3` rule as `dma_copy`.

The legality splits into a **non-indirect** path (DGE ∈ `{unknown, hwdge}`) and an **indirect/gather** path (DGE ∈ `{unknown, swdge}`). Every row below is a CONFIRMED literal error string in body `0x3e8e0` (cross-checked against the docstring constraints at stub L487–504):

| Path | dge_mode | Rule | Error string / check |
|---|---|---|---|
| Non-indirect | `swdge` rejected | — | `"SWDGE is not a valid dge_mode for non-indirect dma_transpose"` |
| Non-indirect + HWDGE | `hwdge` | `src.shape[0] == 16` | `"Hardware DGE transpose src tile must have 16 partitions, but it has <n>"` |
| Non-indirect + HWDGE | `hwdge` | `src.shape[-1] % 128 == 0` | `"Hardware DGE transpose src tile must have <=128 columns, but it has <n>"` |
| Non-indirect + HWDGE | `hwdge` | `sizeinbytes(dtype) == 2` | `"Hardware DGE DMA transpose can only be used for 2 byte data types, but the type is <dt>"` |
| Indirect | `hwdge` rejected | — | `"Indirect DMA transpose operations must use SWDGE mode"` |
| Indirect + SWDGE | `swdge` | `sizeinbytes(src.dtype) == 2` | `"Source tensor for DMA gather transpose must have 2B dtype"` |
| Indirect + SWDGE | `swdge` | `src.shape[-1] <= 128` (docstring: `== 128`) | `"Fast dimension of source tensor for DMA gather transpose must be <=128"` |
| Indirect + SWDGE | `swdge` | `indices.shape[1] == 1` | `"Indices tensor for DMA gather transpose must have shape Nx1"` |
| Indirect + SWDGE | `swdge` | `indices.dtype == np.uint32` | `"Indices tensor for DMA gather transpose must have uint32 dtype"` |
| Indirect + SWDGE | `swdge` | `indices.shape[0] % 16 == 0` | `"Size of indices tensor for DMA gather transpose must be multiple of 16"` |

> **CORRECTION (DMA-1) —** the HWDGE column constraint is **`src.shape[-1] % 128 == 0`** (a multiple of 128), per the docstring rule at stub L492, *not* `src.shape[-1] <= 128`. The error string says "must have <=128 columns" but the actual predicate that fires it is the modulo check — a draft that implements a literal `<= 128` upper bound will accept e.g. a 64-column tile that the binary rejects and reject a 256-column tile that the binary accepts. Implement the modulo, present the `<=128` text only as the diagnostic.

> **GOTCHA — the `dge_mode` whitelists are disjoint by path.** `hwdge` is legal *only* for non-indirect transpose; `swdge` is legal *only* for indirect/gather transpose. `unknown` is legal for both (compiler decides). Passing `swdge` to a plain transpose or `hwdge` to a gather transpose are the two single-string rejections at the top of each path's table.

---

## GpSimd Gather

### local_gather

```text
local_gather(src_buffer, index, num_elem_per_idx=1, num_valid_indices=None, *, mask=None)
  signature: CONFIRMED stub L631 | source: neuron_isa.py:2596 | engine: GpSimd
  builds: nki_ctx.local_gather(...)  (body 0x64f80, uses check_local_gather_shape)
```

The 8 GpSimd cores each gather independently from their 16 connected SBUF partitions. Constraints (CONFIRMED docstring):

- `src_buffer.shape[0] == index.shape[0]` **and** divisible by 16.
- `num_elem_per_idx ∈ {1, 2, 4, 8, 16, 32}` (contiguous elements gathered per index, for throughput).
- ≤ 4096 indices per core.
- `index` dtype must be **uint16**; NO dtype cast — output preserves `src_buffer` dtype.
- `num_valid_indices` default = `index.size / (index.shape[0] / 16)`; must not exceed that.

> **NOTE —** `local_gather` is the only op here with NO `dtype` parameter at all (its signature ends `..., *, mask=None`). The output dtype is forced to `src_buffer`'s — casting is structurally impossible. The 16-partition divisibility is the per-core SBUF wiring (8 cores × 16 partitions = 128), not an arbitrary alignment.

### builtin_custom_op

```text
builtin_custom_op(function_name, lib_file_name, ulib_to_ucode_version,
                  ulib_to_isa_version, srcs, dsts, **kwargs)
  signature: CONFIRMED stub L411 | source: neuron_isa.py:2798
  builds: nki_ctx.builtin_custom_op(...)  (thin, body 0x3d2a0)
```

Registers a call to a precompiled custom-op library (the `libbuiltincustomop_cpuN.so` family) with `src`/`dst` tile lists and ulib version pins (`ucode` + `isa`). No local dtype/engine validation at this layer — it is a pure registration forward.

---

## IR Node / Builder Inventory

The intrinsic layer never constructs a penguin `Operator` directly; it calls a builder on `nki_ctx` (or a `sema.*_inst` constructor). The node-kind names below are CONFIRMED `__pyx_n_s_*` identifiers; the rest forward to the same-named `nki_ctx.<op>` method, where the `Operator` is built (see the IRBuilder, sibling D-P22).

| Intrinsic | IR node / builder | Confidence |
|---|---|---|
| `tensor_reduce` | `nki_ctx.tensorreduce` (penguin reduce node) | CONFIRMED |
| `tensor_partition_reduce` | `nki_ctx.tensor_partition_reduce` | STRONG |
| `iota` | `index_value_inst` | CONFIRMED |
| `bn_stats` | `sema.bn_stats_inst` | CONFIRMED |
| `bn_aggr` | `sema.bn_aggr_inst` | CONFIRMED |
| `range_select` | `range_select` node + `range_select_info` descriptor | CONFIRMED |
| `dma_transpose` (indirect) | `IndirectMemrefTileND` tile | CONFIRMED |
| `affine_select` / `dropout` / `memset` / `tensor_copy{,_dynamic_*,_predicated}` / `max8` / `nc_find_index8` / `nc_match_replace8` / `select_reduce` / `local_gather` / `sequence_bounds` / `dma_copy` / `builtin_custom_op` | same-named `nki_ctx.<op>(...)` | STRONG |

---

## Confidence Ledger

| Claim class | Confidence | Evidence |
|---|---|---|
| All 22 call signatures / defaults / kw-only boundaries | CONFIRMED | stub `__init__.pyi` (verified L268–1614) |
| All enum values (`engine`/`dge_mode`/`oob_mode`/`reduce_cmd`/`nc_version`) | CONFIRMED | stub L5–100 |
| Per-op source line numbers | CONFIRMED | `_TraceSetupAndCall` args |
| `dma_transpose` legality table (10 strings/rules) | CONFIRMED | literal error strings body `0x3e8e0` + docstring L487–504 |
| `reduce_cmd` legal sets per op | CONFIRMED | each validator body's n_s set |
| bitvec dtype sets (`tensor_reduce` {int32,uint8/16/32} vs `partition_reduce` {uint8/16/32}) | CONFIRMED / STRONG | `:3130` body; partition-reduce body checks |
| `select_reduce` / `tensor_copy_predicated` error strings | CONFIRMED | `pyx_kp_u_*` literals |
| IR node names | CONFIRMED | `__pyx_n_s_*` identifiers |
| Per-op validator call **order** | STRONG | decompile call-graph + n_s ordering |
| AccessPattern / predicate construction inside `nki_ctx` | INFERRED | lives in IRBuilder/predicates modules (D-P22), not in this `.so` |
| Numeric thresholds (4096, 512, 16384) — *enforcement site* | INFERRED | values CONFIRMED docstring; runtime check in `nki_ctx` |

---

## Cross-References

- [6.4.1 nki.isa Compute Intrinsics](isa-compute-intrinsics.md) — the compute half: `tensor_scalar`, `tensor_tensor`, `activation`, `nc_matmul`, scan/cumulative; owns `load_reduce` (the `reduce_cmd` value never legal here)
- [2.16 DVE Search Encoding](../isa/dve-search-encoding.md) — wire encoding for `max8` / `find_index8` / `match_replace8`
- [2.17 DVE Datamove Encoding](../isa/dve-datamove-encoding.md) — wire encoding for `range_select` / `select_reduce` datamove path
- [6.5.3 Memory Builders](isa-memory-builders.md) — the `nki_ctx` IRBuilder side that materializes the `Operator` nodes these intrinsics request
