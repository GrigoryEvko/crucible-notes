# The NKI Frontend + Reference Simulator

The previous page — [compiler-map.md](compiler-map.md) — owns the **lowering seam**: the
62 `emit_*` IR-builder entry points and the numeric TPB opcode each one produces. This
page owns the **frontend surface that sits on top of it** and the **value oracle that
sits beside it**: the public `nki.isa.<name>` op set a kernel author writes against, the
per-op SEMANTICS, and the pure-numpy **reference simulator** the validation (VAL) lane
runs as the bit-exact ground truth for every op. Where compiler-map answers *"what
opcode does `emit_iota` emit?"*, this page answers *"what does `nki.isa.iota(...)`
actually compute, and what numpy does the simulator run to prove it?"*

Everything below is read from the **shipped plain-python NKI frontend** and the **cc-stubs
`.pyi`** — these `.py`/`.pyi` files are binary-derived shipped artifacts and are citeable
verbatim. The compiled residual under the python surface is the `_nki_irbuilder.*.so`
pybind (and its `_mlir_libs` siblings); §6 isolates exactly what it hides.

**Anchors.** Every claim is tagged `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` and bound to
a file/line/symbol. The two corpora:

```
NKI = neuronx-misc/extracted/nki-0.3.0+23928721754.g18aa1271-cp311-cp311-linux_x86_64/nki/
PYI = neuronx-cc/extracted/neuronx_cc_stubs-2.24.5133.0+58f8de22-py3-none-any/neuronxcc-stubs/nki/isa/__init__.pyi
```

`cp310`/`cp311`/`cp312` wheels are byte-identical in the Python layer; the cp311 copy is
cited. `nki.__version__ == "0.3.0+23928721754.g18aa1271"` (`NKI/_version.py`,
[HIGH/OBSERVED]) — the **op-library** version, an axis fully distinct from any firmware
version (see §7).

> **NOTE — scope boundary with [compiler-map.md](compiler-map.md).** That page is the
> authoritative `emit_*`→opcode table; this page does **not** re-tabulate it. The
> opcode numerics quoted here are CARRIED from it and from
> [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md);
> this page's deliverables are the **`nki.isa.<name>` public spellings**, the **per-op
> semantics**, and the **numpy reference simulator**.

---

## 1. The three-altitude surface — counts, byte-pinned

The NKI frontend straddles three altitudes; all three are in-hand as shipped python/pyi.
The counts below are programmatic, not eyeballed.

| Altitude | Artifact | Count | Evidence |
|---|---|---|---|
| (1) Public API `nki.isa.<op>` | `NKI/isa/*.py`, `@_public_api`-decorated defs | **50** ops across **19** modules | `rg -c '@_public_api' NKI/isa/*.py` → 50; `fd -e py NKI/isa/` → 19 [HIGH/OBSERVED] |
| (1b) Documented surface | `PYI` `def <op>(…)` | **35** documented defs | `rg -c '^def ' PYI` → 35 [HIGH/OBSERVED] |
| (2) MLIR emitters | `NKI/backends/mlir_tracer/isa_emit.py` `emit_*` | **62** emitters | `rg -c '^def emit_' isa_emit.py` → 62 (the compiler-map deliverable) [HIGH/OBSERVED] |
| (2b) Numpy reference sim | `NKI/backends/simulator/*.py` | per-op pure-numpy | the VAL oracle, §4–5 [HIGH/OBSERVED] |
| (3) IR builder | `_nki_irbuilder.*.so` | **69** mnemonics | `strings … \| rg -c '^NKI IR builder for'` → 69 [HIGH/OBSERVED] |

> **CORRECTION (vs GX-REF-03 §1, which CARRIED "40 documented ops" in the `.pyi`).** The
> shipped `.pyi` documents **35** `def`s, not 40 (`rg -c '^def ' PYI` = 35; full list in
> §3). The 50/35/62/69 funnel is the accurate one: **50** `@_public_api` ops → **35** of
> them documented in the cc-stubs → **62** `emit_*` mnemonics in `isa_emit.py` (one NKI op
> can fan to several emit names, e.g. `tensor_partition_reduce` →
> `emit_cross_lane_reduce_arith` + `_bitvec`) → **69** irbuilder docstrings (the 62 +
> 7 not emitted from `isa_emit.py`, §6). [HIGH/OBSERVED — all four counts re-grepped this
> pass.]

The 19 `isa/*.py` modules and the ops each exports (`NKI/isa/__init__.py`, OBSERVED):

| Module | `@_public_api` ops |
|---|---|
| `activation.py` | `activation`, `activation_reduce` |
| `advanced.py` | `dma_compute`, `tensor_partition_reduce`, `scalar_tensor_tensor`, `select_reduce`, `quantize_mx`, `range_select`, … (14 defs) |
| `copy.py` | `dma_copy`, `memset`, `tensor_copy` |
| `gather.py` | `local_gather`, `nc_n_gather` |
| `iota.py` | `iota`, `affine_select` |
| `lnc.py` | `core_barrier`, `sendrecv` |
| `matmul.py` | `nc_matmul` (+ `nc_matmul_mx` re-export) |
| `misc.py` | `max8`, `nonzero_with_count`, `reciprocal`, `sequence_bounds`, `exponential`, `tensor_copy_dynamic_*`, … (9 defs) |
| `rng.py` | `dropout`, `rand2`, `rand_get_state`, `rand_set_state`, `rng`, `set_rng_seed` |
| `scan.py` | `tensor_tensor_scan` |
| `shuffle.py` | `nc_stream_shuffle` |
| `stats.py` | `bn_aggr`, `bn_stats` |
| `tensor_ops.py` | `tensor_tensor`, `tensor_scalar`, `tensor_reduce` |
| `transpose.py` | `dma_transpose`, `nc_transpose` |
| `constants.py`, `enums.py`, `validation.py`, `virtual_register.py` | enums / helpers / `VirtualRegister` |

[HIGH/OBSERVED — `@_public_api` decorator counted per module: gather 2, transpose 2,
tensor_ops 3, copy 3, stats 2, shuffle 1, advanced 14, activation 2, scan 1, rng 6,
matmul 1, iota 2, lnc 2, misc 9 = 50.]

---

## 2. The `nki.isa.<name>` → `emit_<mnemonic>` → engine → opcode join (GPSIMD slice)

The full 62-row `emit_*`→opcode table is in [compiler-map.md](compiler-map.md) §3 — do
not duplicate it. The table below is the **frontend view**: the public `nki.isa.<name>` a
user types, the `emit_` it dispatches to, the per-op cost formula from the `.pyi`, and the
device opcode (numeric, CARRIED from compiler-map §3 / the ledger). Only the **GPSIMD-
resident** slice is shown; the names agree field-for-field with compiler-map.

| `nki.isa.<name>` | `emit_<mnemonic>` | engine | cost (.pyi) | TPB opcode | sim ref (§4–5) |
|---|---|---|---|---|---|
| `iota` | `emit_iota` | GpSimd | `150 + N` | `IOTA 0x7e` | `simulator/iota.py:iota` |
| `affine_select` | `emit_tensor_scalar_affine_select` | GpSimd | `GPSIMD_START + N` | `TENSOR_SCALAR_AFFINE_SELECT 0x92` | `simulator/iota.py:affine_select` |
| `tensor_partition_reduce` (`op=add/max`) | `emit_cross_lane_reduce_arith` | GpSimd | `N` | `CROSS_LANE_REDUCE_ARITH 0x7c` | `advanced.py:tensor_partition_reduce_arith` |
| `tensor_partition_reduce` (`op=bitwise_or/and`) | `emit_cross_lane_reduce_bitvec` | GpSimd | `N` | `CROSS_LANE_REDUCE_BITVEC 0x7d` | `…_reduce_bitvec` |
| `local_gather` | `emit_indirect_copy` | GpSimd | `150 + (n_idx·elem)/C` | `INDIRECT_COPY 0xe7` | `gather.py:local_gather` |
| `nc_n_gather` | `emit_gather` | GpSimd | — | `GATHER 0x68` | `gather.py:nc_n_gather` |
| `nonzero_with_count` | `emit_nonzero_with_count` | GpSimd | — | `NONZERO_WITH_COUNT 0xf2` | `misc.py:nonzero_with_count` |
| `quantize_mx` | `emit_quantize_mx` | Vector ◆ | — | `TENSOR_DEQUANTIZE 0x7b` (fwd pack) | `advanced.py:quantize_mx` |
| `sequence_bounds` | `emit_sequence_bounds` | GpSIMD | — | `GET_SEQUENCE_BOUNDS 0xbe` | (`isa/misc.py`) |
| `tensor_tensor` (arith) | `emit_tensor_tensor_arith` | Vector **or** GpSimd (route) | `max(MIN_II,2N)` | `TENSOR_TENSOR_ARITH 0x41` | `tensor_ops.py:tensor_tensor_arith` |
| `tensor_scalar` (arith) | `emit_tensor_scalar_arith` | Vector/Scalar **or** GpSimd (rsqrt only) | `max(MIN_II,N)` | `TENSOR_SCALAR_ARITH 0x43` | `tensor_ops.py:tensor_scalar_arith` |
| `tensor_copy` (copy/cast) | `emit_tensor_copy` | Vector/Scalar/GpSimd | — | `COPY 0x46` / `CAST 0x47` | `copy.py:tensor_copy` |
| `memset` | `emit_memset` | Vector **or** GpSimd | — | `MEMSET 0x49` | `copy.py:memset` |

◆ `quantize_mx` declares **Vector** in the irbuilder `.so` docstring (compiler-map §4.2
CORRECTION); the device decode is the *inverse* MX dequant on POOL. The opcode numerics
match [compiler-map.md](compiler-map.md) §3 and
[../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md).
[opcode CARRIED; `nki.isa` names + cost formulas HIGH/OBSERVED.]

### 2.1 The engine enum — numeric, byte-pinned

`NKI/isa/enums.py` (`class engine(Enum)`, OBSERVED verbatim ordinals):

```python
class engine(Enum):
    tensor  = 1
    vector  = 5
    scalar  = 2
    gpsimd  = 3
    dma     = 4
    sync    = 6
    unknown = 0   # default — let the compiler pick the engine
```

This is the authoritative NKI-side engine id set. The numbering is **not** dense and
**not** source-order: `vector=5` is declared second in the file but carries ordinal 5.
[HIGH/OBSERVED — `NKI/isa/enums.py:8-29`.] A reimplementation copying these ordinals must
take the *values*, not the declaration order.

> **GOTCHA — `gpsimd` is the engine; `gpsimd_dma` is a different selector.** `enums.py`
> also defines `class dma_engine: dma=1, gpsimd_dma=2` — the host-side selector for the
> low-latency SB-to-SB swap path (`InstGPSIMDSB2SB`), orthogonal to the compute-engine
> `engine` enum above. Do not conflate `engine.gpsimd=3` (compute datapath) with
> `dma_engine.gpsimd_dma=2` (DMA trigger). [HIGH/OBSERVED.]

---

## 3. The documented surface — the 35 `.pyi` ops

The cc-stubs `.pyi` is the *documented* subset (the surface a kernel author sees in an
IDE). The 35 `def`s, verbatim names (`rg '^def ' PYI`):

```
activation            activation_reduce     affine_select         bn_aggr
bn_stats              builtin_custom_op     dma_copy              dma_transpose
dropout               get_nc_version        iota                  local_gather
max8                  memset                nc_find_index8        nc_match_replace8
nc_matmul             nc_stream_shuffle     nc_transpose          range_select
reciprocal            scalar_tensor_tensor  select_reduce         sequence_bounds
tensor_copy           tensor_copy_dynamic_dst   tensor_copy_dynamic_src
tensor_copy_predicated    tensor_partition_reduce   tensor_reduce
tensor_scalar         tensor_scalar_cumulative  tensor_scalar_reduce
tensor_tensor         tensor_tensor_scan
```

[HIGH/OBSERVED — `PYI` line 116…1975.] `builtin_custom_op` is here — the host op that
names a pre-built ucode lib and emits the `(ulib_to_ucode_version, ulib_to_isa_version)`
version handshake (§7). `nc_n_gather`, `quantize_mx`, `nonzero_with_count` are **absent**
from the `.pyi` though present as `@_public_api` in `isa/*.py` — the documented surface is
narrower than the live surface, which is the 50/35 gap from §1.

### 3.1 The cost model — GPSIMD_START vs MIN_II

The `.pyi` per-op *"Estimated instruction cost"* blocks carry two named constants, both
byte-pinned this pass:

| Constant | Value | Definition (verbatim) | `.pyi` line |
|---|---|---|---|
| `GPSIMD_START` | ≈ **150** GpSimd cycles | *"the instruction startup overhead on GpSimdE"* | 300–301 |
| `MIN_II` | ≈ **64** engine cycles | *"the minimum instruction initiation interval for small input tiles"* | 180–181, 249–250 |

The GpSimd ops pay a **fixed 150-cycle startup floor + N**; the Vector/Scalar ops pay a
**`max(MIN_II≈64, N)` floor**. The per-op formulas (verbatim, `.pyi`):

- **`iota`** — `150 + N` GpSimd cycles, `N` = elements/partition of the output tile; the
  int→dtype cast is *"at no additional performance cost"* (`.pyi:612`). Note `iota` uses
  the literal `150`, while `affine_select` uses the named `GPSIMD_START` — same constant.
- **`affine_select`** — `GPSIMD_START + N` GpSimd cycles, `N` = elements/partition of
  `on_true_tile` (`.pyi:300`).
- **`local_gather`** — `150 + (num_valid_indices * num_elem_per_idx) / C` GpSimd cycles,
  where `C = ((28 + t·num_elem_per_idx)/(t·num_elem_per_idx)) / min(4/dtype_size,
  num_elem_per_idx)`, `t` a constant **4**, `dtype_size` = `src_buffer.dtype` bytes
  (`.pyi:679`).
- **`tensor_tensor`** — `max(MIN_II, N)` when one input is in PSUM / when both SBUF and
  all-bf16 with op ∈ {add,multiply,subtract} and free-contiguous; else `max(MIN_II, 2N)`
  (`.pyi:1928`).
- **`tensor_scalar`/`activation`** — `max(MIN_II, N)` Vector/Scalar cycles
  (`.pyi:1725`, `:175`); **`tensor_reduce`** — `N` (or `N/2` for bf16 add/max,
  `.pyi:1646`); **`reciprocal`** — `max(MIN_II, 13·(N/3))` (`.pyi:346`).

> **NOTE — the 150-vs-64 startup contrast is the architectural reading.** `GPSIMD_START`
> (150) ≈ 2.3× the Vector/Scalar `MIN_II` (64). The Q7 POOL cluster pays a markedly
> higher per-instruction launch cost than the hardwired Vector/Scalar datapaths —
> consistent with GPSIMD being a software-dispatched Xtensa cluster (kernel_info_table
> scan + trampoline + windowed-ABI entry) vs a fixed-function pipe. This is *why* the
> compiler keeps small tiles on Vector and routes to GpSimd only the ops Vector cannot do
> (cross-partition reduce, gather, iota, affine_select, MX, int tensor_tensor). The two
> cost bases are HIGH/OBSERVED; the architectural reading is INFERRED-HIGH. The scheduler
> consumes this model — see `neuronx-cc/wiki/src/.../scheduler-cost.md`.

---

## 4. The numpy reference simulator — the value oracle

`NKI/backends/simulator/*.py` is the **pure-numpy ground truth** the VAL lane runs to
prove a device kernel computes the right value. It is the semantic specification of every
op: a reimplementation's GPSIMD kernel is *correct* iff it matches this numpy bit-for-bit
(modulo the dtype mapping in §4.1). The Part-15 validation replay drives kernels against
this oracle — see `neuronx-gpsimd/wiki/src/validation/` (cited as a path; the per-op
replay stubs may not exist yet).

The dispatch is keyed by **opcode sentinels**, not strings. The op-name→numpy-callable
registry is `_NUMPY_FUNC_MAP` (`NKI/language/operators.py:442`, **56** entries), consumed
through three accessors in `NKI/language/ops.py`:

- `get_numpy_func(op)` — `_NUMPY_FUNC_MAP[op]` (the 56-entry master, `operators.py:534`).
- `get_numpy_binary_op(op)` — asserts `op ∈ BINARY_OPS`, then `get_numpy_func` (`ops.py:162`).
- `get_numpy_reduce_op(op)` — a separate **6-entry** `_REDUCE_OP_MAP` (`ops.py:170`):
  `{add→np.sum, multiply→np.prod, maximum→np.max, minimum→np.min,
  bitwise_and→bitwise_and.reduce, bitwise_or→bitwise_or.reduce}`.

`_NUMPY_FUNC_MAP` (`operators.py:442–502`, OBSERVED — representative entries):

```python
_NUMPY_FUNC_MAP = {
    opcode.add: np.add,          opcode.subtract: np.subtract,
    opcode.multiply: np.multiply, opcode.divide: np.divide,
    opcode.maximum: np.maximum,  opcode.minimum: np.minimum,
    opcode.power: np.power,      opcode.bitwise_and: np.bitwise_and,
    opcode.rsqrt:   lambda x: 1.0 / np.sqrt(x),
    opcode.sigmoid: lambda x: 1.0 / (1.0 + np.exp(-x)),
    opcode.relu:    lambda x: np.maximum(0, x),
    opcode.silu:    lambda x: x / (1.0 + np.exp(-x)),
    opcode.fmod: np.fmod, opcode.mod: np.mod,  # ... 56 entries total
}
```

[HIGH/OBSERVED — `operators.py:442`, `rg -c 'opcode\.\w+:' operators.py` = 56.] Note
`fmod→np.fmod` (C-style truncated remainder, sign of dividend) is distinct from
`mod→np.mod` (Python floored, sign of divisor): a reimplementation's POOL ALU leaves must
honor the same two remainder conventions. The transcendental seeds (`gelu`, `erf`,
`silu_dx`, …) are closed-form numpy lambdas here — the oracle does **not** model the
device PWL/seed-table; a kernel matching the table to ~1 ULP is "correct" against this
fp32 reference only to the reference's own tolerance.

### 4.1 `_NKI_TO_NUMPY_DTYPE` — the dtype map (the precision trap)

`NKI/backends/simulator/dtypes.py:34` (OBSERVED verbatim). This is the single most
precision-critical map for the simulator — it decides what numpy *precision* the oracle
runs.

```python
_NKI_TO_NUMPY_DTYPE = {
    bool_:   np.bool_,
    int8:    np.int8,    int16:  np.int16,   int32:  np.int32,
    uint8:   np.uint8,   uint16: np.uint16,  uint32: np.uint32,
    float16: np.float16, float32: np.float32,
    bfloat16:        np.float32,   # ← upcast to fp32 by default
    tfloat32:        np.float32,   # ← upcast
    float8_e4m3:     np.float32,   # ← upcast
    float8_e5m2:     np.float32,   # ← upcast
    float8_e4m3fn:   np.float32,   # ← upcast
    float8_e5m2_x4:    np.uint32,  # 4×fp8 packed in a uint32 word
    float8_e4m3fn_x4:  np.uint32,
    float4_e2m1fn_x4:  np.uint8,
}
```

> **GOTCHA — the default sim runs low-precision FP as fp32.** By DEFAULT, `bfloat16`,
> `tfloat32`, and the unpacked `float8_e*` map to `np.float32` — the oracle does **not**
> model their reduced mantissa. A `precise_fp` context flag (`to_numpy_dtype`,
> `dtypes.py:74`) swaps in `_get_precise_dtype_map()`, which imports **`ml_dtypes`** and
> remaps `bfloat16→mld.bfloat16`, `float8_e4m3→mld.float8_e4m3fn`,
> `float8_e5m2→mld.float8_e5m2` (`dtypes.py:58–71`). A reimplementation validating a bf16
> kernel against this oracle MUST set `precise_fp`, or it compares fp32 numpy against bf16
> silicon and the rounding will diverge. The packed `*_x4` dtypes are stored as their
> *container* integer (`uint32`/`uint8`) and unpacked through `unpack_x4_data`
> (`dtypes.py:127`) — a uint32 word is a `.view()` of 4 fp8 lanes. [HIGH/OBSERVED.]

### 4.2 The `tensor_tensor_arith` engine split — the GpSimd vs Vector value divergence

This is the one place the high-level **engine choice** changes the **computed value**, and
the simulator makes it visible. `NKI/backends/simulator/tensor_ops.py:41–59` (verbatim):

```python
def tensor_tensor_arith(dst, data1, data2, op, engine, name):
    np_op = get_numpy_binary_op(op)
    def compute(d1, d2):
        if engine == engine_enum.gpsimd:
            return np_op(d1, d2)                      # NATIVE int — 32-bit wrap, no fp cast
        d1_fp32 = d1 if d1.dtype == np.float32 else d1.astype(np.float32)
        d2_fp32 = d2 if d2.dtype == np.float32 else d2.astype(np.float32)
        return np_op(d1_fp32, d2_fp32)                # Vector path — fp32-cast first
    _tensor_tensor_impl(dst, data1, data2, compute)
```

The GpSimd branch runs the op **in the native integer lane width** (int32/uint32, 32-bit
wrap, no saturation); the Vector branch casts both operands to fp32 first. The two branches
compute **different bit patterns** for the same `int32` inputs — the GpSimd path is exact
integer arithmetic, the Vector path rounds through fp32's 24-bit mantissa for values
beyond 2²⁴.

The compiler picks the branch via the routing rule in `NKI/isa/tensor_ops.py:104–119`
(verbatim):

```python
_INT32_DTYPES   = (int32, uint32)
_GPSIMD_ARITH_OPS = (add, subtract, multiply)
resolved_engine = engine
if (resolved_engine == _engine_enum.unknown
        and op in _GPSIMD_ARITH_OPS
        and dst.dtype  in _INT32_DTYPES
        and data1.dtype in _INT32_DTYPES
        and data2.dtype in _INT32_DTYPES
        and not is_psum(dst.buffer)
        and not is_psum(data1.buffer)
        and not is_psum(data2.buffer)):
    resolved_engine = _engine_enum.gpsimd   # -> TENSOR_TENSOR_ARITH 0x41 on POOL
```

So `int32`/`uint32` `add`/`subtract`/`multiply` with no PSUM operand and `engine=unknown`
routes to GpSimd (`0x41` on POOL), where the device runs the native-int kernel — and the
simulator's `gpsimd` branch is its exact oracle. Everything else (fp, or any PSUM operand,
or a non-{add/sub/mul} op) stays on Vector with the fp32-cast semantics. [HIGH/OBSERVED —
both the routing rule and the sim branch; the kernel-body identity is INFERRED-HIGH (the
POOL int-tt kernel is not byte-disassembled here, but its decode and the libfiss xdref-int
leaf both describe 32-bit-wrap add/sub/mul).] This same split is documented from the
lowering side in [compiler-map.md](compiler-map.md) §4.4.

The companion `tensor_tensor_power` (`tensor_ops.py:71`) has **no** engine kwarg — it
always casts to fp32 and calls `np.power`, i.e. there is no native-int power path.
`tensor_tensor_bitvec` (`:62`) never casts (bit patterns preserved). The shared
`_tensor_tensor_impl` (`:6`) reshapes equal-size-but-mismatched operands, runs the branch,
truncates/reshapes to `dst.shape`, then casts to `to_numpy_dtype(dst.dtype)`.

---

## 5. Per-op semantics — the GPSIMD kernels, as numpy

Each subsection reproduces the simulator body as annotated pseudocode. These are the
**reimplementation specs**: a Vision-Q7 POOL kernel is correct iff it reproduces this.

### 5.1 `iota` — the integer ramp

`NKI/isa/iota.py:23` (public) docstring pseudocode and `simulator/iota.py:36` (oracle)
agree exactly. The public docstring's loop nest (verbatim):

```python
for channel_id in range(num_partitions):
  for w in range(num_w):
   for z in range(num_z):
    for y in range(num_y):
     for x in range(num_x):
       value = offset + (channel_id * channel_multiplier) \
               + (w*step_w) + (z*step_z) + (y*step_y) + (x*step_x)
       dst[channel_id, w, z, y, x] = value
```

The simulator computes the same in vectorized int64 then casts (`simulator/iota.py:55–79`):

```python
channel_ids = np.arange(num_partitions, dtype=np.int64)
result = offset + channel_ids.reshape(-1,1) * channel_multiplier   # per-channel additive stride
# coeffs/shape = pattern with trivial [0,1] dims stripped (_extract_coeffs_shape)
grids = np.meshgrid(*[np.arange(n, dtype=np.int64) for n in shape], indexing="ij")
dim_contribution = sum(grids[i] * coeffs[i] for i in range(len(coeffs)))   # int64
result = result + dim_contribution.reshape(1, -1)
dst.set_data(result.reshape(dst.shape).astype(dst_np_dtype))       # int ramp THEN cast
```

The semantics, byte-pinned: (1) values computed in **integer** math (the docstring says
*"32-bit integer arithmetic"*, `.pyi:609`; the sim widens to int64 to avoid intermediate
overflow then casts down); (2) `offset` is the int32 base added to every element;
(3) `channel_multiplier × channel_id` is the per-channel additive stride; (4) the 4D
`pattern` is left-padded with trivial `[0,1]` dims when < 4D (`isa/iota.py:30`), which
`_extract_coeffs_shape` strips (`simulator/iota.py:24`); (5) the int→`dtype` cast is the
tail step, *"at no additional performance cost"*. [HIGH/OBSERVED.]

`affine_select` (`simulator/iota.py:82`) builds the identical affine grid, then
`predicate = cmp_fn(affine_values, 0); result = np.where(predicate, on_true_tile,
on_false_value)` — the affine value is compared against **0** (not against a tensor), and
the comparison op is one of the `_NUMPY_FUNC_MAP` binary comparators. It routes to
`TENSOR_SCALAR_AFFINE_SELECT 0x92` — a DISTINCT opcode from `TENSOR_SCALAR_SELECT 0x98`;
matching on the substring "select" mis-binds (compiler-map §4.3 S2).

### 5.2 `local_gather` vs `nc_n_gather` — the 2:1 split (do-not-repeat)

These two NKI names map to **different POOL kernels** and must not be conflated — the
do-not-repeat item flagged in
[../reference/correction-ledger.md](../reference/correction-ledger.md):

| NKI name | `emit_` → opcode | model | indices | sim |
|---|---|---|---|---|
| `local_gather` | `emit_indirect_copy` → `INDIRECT_COPY 0xe7` | 8-core / 16-partition | **uint16** | `gather.py:local_gather` |
| `nc_n_gather` | `emit_gather` → `GATHER 0x68` | within-partition flat | **int/uint32** | `gather.py:nc_n_gather` |

> **GOTCHA — `local_gather` is a TRAP: it does NOT emit `GATHER 0x68`.** The name
> contains "gather" but `NKI/backends/mlir_tracer/isa.py:853-854` is explicit:
> *"Emit MLIR local_gather operation (maps to IndirectCopy in NISA dialect)"* — it routes
> to `emit_indirect_copy` (`isa.py:858`) → `INDIRECT_COPY 0xe7`. Only `nc_n_gather`
> (`isa.py:1238`) routes to `emit_gather` (`isa.py:1243`) → `GATHER 0x68`. [HIGH/OBSERVED
> — verbatim comment + the two emit dispatches.]

**`local_gather` semantics** (`simulator/gather.py:8`, the 16-partition-per-core model,
verbatim structure):

```python
num_cores = num_partitions // 16                    # GpSimd cores, 16 partitions each
for core in range(num_cores):
    start_p = core * 16
    core_idx = idx_data[start_p:start_p+16]
    indices_1d = core_idx.flatten(order="F")[:num_valid_indices]   # COLUMN-MAJOR (partition-first)
    core_src = src_flat[start_p:start_p+16]
    gathered = np.take(core_src, indices_1d, axis=1)               # gather along free axis
    # num_elem_per_idx>1: copy `num_elem_per_idx` contiguous src elements per index
```

Byte-pinned constraints (`isa/gather.py:16` + `.pyi:670`): indices are **uint16**
(*"The indices in `index` tile must be uint16 types"*, `gather.py:56`); `num_elem_per_idx
∈ {1,2,4,8,16,32}`; **≤ 4096 indices per core**; `src_buffer.shape[0] % 16 == 0`. The
`flatten(order="F")` is the **partition-dimension-first** flatten — the index stream walks
the 16 partitions of a core before advancing along the free axis. Output dtype = source
dtype (*"no data type casting is allowed"*, `gather.py:55`). [HIGH/OBSERVED.]

> **NOTE — the loop bound is `num_partitions // 16`, not a literal `range(8)`.** The sim
> drives `num_cores = num_partitions // 16` cores (`gather.py:9`); on a full 128-partition
> tile that is exactly 8, matching the eight-GpSimd-core SUNDA POOL geometry, but the sim
> scales the loop to the tile's partition count rather than hardcoding 8. (Contrast
> `nonzero_with_count` §5.4, which *does* hardcode `range(8)`.) [HIGH/OBSERVED.]

**`nc_n_gather` semantics** (`simulator/gather.py:88`, within-partition flat gather):

```python
idx_clipped = np.clip(idx_flat, 0, data_flat.shape[1] - 1)         # clip to valid range
result = data_flat[np.arange(num_partitions)[:,None], idx_clipped] # advanced index per partition
out_of_bounds = (idx_flat < 0) | (idx_flat >= data_flat.shape[1])
result = np.where(out_of_bounds, 0, result)                        # OOB lanes -> 0
```

Each partition gathers from its own row independently; an OOB index is **clipped then
zero-filled** — the host model of the firmware's `IMMEDIATE_WRITE(0)` index-miss behavior.
The `n = ceil(elems_per_partition / 512)` decomposition in the public docstring
(`isa/gather.py:121`) is the **ISA-decomposition** count (how many sub-instructions the
ISA splits the gather into); the simulator does **not** compute it — it is documentation,
not value semantics. [HIGH/OBSERVED — `gather.py:88-117`.]

### 5.3 `tensor_partition_reduce` — the cross-lane (partition-axis) reduce

`simulator/advanced.py:374` (verbatim):

```python
def tensor_partition_reduce_arith(dst, op, data, name):
    result = np_reduce(data.get_data().astype(np.float32), axis=0, keepdims=True)  # FP32, partition axis
    dst.set_data(np.broadcast_to(result, dst.shape).astype(dst_np_dtype))
def tensor_partition_reduce_bitvec(dst, op, data, name):
    result = np_reduce(data.get_data(), axis=0, keepdims=True)                     # NO fp32 cast
    dst.set_data(np.broadcast_to(result, dst.shape).astype(dst_np_dtype))
```

The reduce is over **`axis=0` — the PARTITION axis** (the cross-lane reduce, what POOL
does that Vector cannot), `keepdims=True`, then broadcast back to `dst.shape`. The
**arith** path casts to fp32 first; the **bitvec** path reduces raw bits. The two map to
`CROSS_LANE_REDUCE_ARITH 0x7c` / `_BITVEC 0x7d`. [HIGH/OBSERVED.]

> **CORRECTION (R1, vs the firmware `REDUCE_OP` enum).** The NKI surface exposes only
> `{add, max, bitwise_or, bitwise_and}` (`.pyi:1599` `tensor_partition_reduce` param doc,
> verbatim) — a SUBSET of the firmware `REDUCE_OP {ADD, AVERAGE, MAX, OR, AND, XOR}`. No
> `average`/`xor` at the NKI surface. This is a frontend *policy* restriction, not a
> hardware limit — the POOL kernel supports all six. [HIGH/OBSERVED NKI / CARRIED firmware.]

Contrast `tensor_reduce` (free/within-partition axis, `tensor_ops.py:200`,
`numpy_axis = range(ndim-num_r_dim, ndim)` — the **last** dims, never axis 0) → Vector
engine. The two reduces are byte-distinct: `tensor_partition_reduce` always `axis=0`;
`tensor_reduce` never touches axis 0.

### 5.4 `nonzero_with_count` — predicate compaction

`simulator/misc.py:302` (verbatim):

```python
for core in range(8):
    p = core * 16
    if p >= num_partitions: break
    nonzero_indices = np.nonzero(src_data[p])[0]      # operate on each core's 0th partition only
    count = len(nonzero_indices)
    output = np.full(dst_free, padding_val, dtype=np.int32)
    output[:count] = nonzero_indices + index_offset   # found positions, offset by iota base
    output[-1] = count                                # COUNT goes in the LAST element
    dst_data[p] = output
```

The 8-core model (`range(8)` hardcoded here) operates on **each core's 0th partition only**
(`p = core*16`); the count is written to `output[-1]` (the **last** element of the
destination free dim) **after** the indices are written — so if `dst_free` is small, the
count overwrites the last index slot. Found positions are offset by `index_offset` (the
iota base). Routes to `NONZERO_WITH_COUNT 0xf2`. [HIGH/OBSERVED — `misc.py:302-332`.]

### 5.5 `quantize_mx` — MX block pack (forward direction)

`simulator/advanced.py:136` (verbatim core):

```python
exp = (src_data.view(np.uint32) >> 23) & 0xFF        # extract fp32 biased exponent
exp_reshaped = exp.reshape(SP, 8, SF, 4)             # 8×4 = 32-element blocks
mx_scale = np.max(exp_reshaped, axis=(1,3)).astype(np.uint8) - max_exp   # per-block max exp
scale_exp = mx_scale.astype(np.int32) - 127          # float32_exp_bias
scale_factors = 2.0 ** scale_exp
quantized = np.clip(src_data / scale_expanded, -max_val, max_val)        # clip to fp8 max
packed = quantized.astype(target_dtype).view(np.uint32)  # pack 4×fp8 -> uint32 word
```

Per-`(8×4)`-block max-exponent scaling; `max_exp`/`max_val` are dtype-specific (OBSERVED
`advanced.py:154-165`): `float8_e5m2_x4` → `max_exp=15, max_val=57344.0`;
`float8_e4m3fn_x4` → `max_exp=8, max_val=448.0`. The per-block uint8 scales are written
into `dst_scale` with an 8-row-stride compaction (`scale_out[32q:32q+4] = mx_scale[4q:4q+4]`,
`advanced.py:188-189`). This is the **forward pack** (declared Vector); the firmware
`TENSOR_DEQUANTIZE 0x7b` / `proc_4bit_mx_8` is the inverse unpack on POOL. [HIGH/OBSERVED
sim / CARRIED firmware.]

> **NOTE — `e4m3` `max_exp` is 8, not 15.** The simulator uses `max_exp=8, max_val=448.0`
> for `float8_e4m3fn_x4` and `max_exp=15, max_val=57344.0` for `e5m2` (and the
> `else`-fallthrough). 448 = e4m3fn finite max; 57344 = e5m2 finite max. A reimplementation
> must key the block-scale subtraction constant off the *target* fp8 format. [HIGH/OBSERVED
> — `advanced.py:154-165`.]

### 5.6 `sequence_bounds`, `dma_compute`, and the restrictions

- **`sequence_bounds`** — per-segment `[start, end)` bounds from `segment_ids`; padding
  (`id==0`) → `[n, -1]`. Routes to `GET_SEQUENCE_BOUNDS 0xbe`. [OBSERVED `isa/misc.py`
  `@_public_api`.]
- **`dma_compute`** — `dst = Σ src[i]*scale[i]` in fp32, **`reduce_op=add` only** (R2: the
  SDMA CCE/DGE firmware enum carries `{Add, Max, Min, Mult, …}`; NKI restricts to add).
  `dma_compute` is one of the **7 not emitted from `isa_emit.py`** — it is emitted from
  the *other* tracer file `mlir_tracer/isa.py:590` and has a sim impl
  (`simulator/advanced.py:28`); see §6. [HIGH/OBSERVED NKI / CARRIED firmware.]
- **(R3) `tensor_scalar` GpSimd is rsqrt-only.** `NKI/isa/tensor_ops.py:242` (verbatim):
  *"`nki.isa.engine.gpsimd` (only allowed for rsqrt)"*. The GpSimd route for
  `tensor_scalar` is far narrower than for `tensor_tensor` (which allows int add/sub/mul).
  A related fact (`tensor_ops.py:216-218`): the **Scalar Engine on trn2** supports only
  `op0=multiply,op1=add` / `op0=multiply,op1=None` / `op0=add,op1=None`. [HIGH/OBSERVED.]

None of R1–R3 contradict the firmware; each is the frontend exposing *less* than the
hardware.

---

## 6. The compiled residual — what is NOT recoverable from the python surface

The op-level surface above (names, signatures, semantics, costs, routing) is all plain
python; the IR emission underneath is a pybind. The lone compiled `.so` the **emit layer**
imports is `_nki_irbuilder.cpython-311-x86_64-linux-gnu.so` (`isa_emit.py:17`,
`from … _mlir_libs import _nki_irbuilder`). It is not literally the only `.so` in
`_mlir_libs/` — that directory ships nine compiled objects (`_frontend`, `_mlir`,
`_nki`, `_nki_irbuilder`, `_nki_simulator`, `_nisaDialectsNanobind`, …) — but
`_nki_irbuilder` is the one the `emit_*` chain calls. Each `emit_<m>` calls exactly one
`_nki_irbuilder.<m>()`. What the pybind hides — and what a reimplementation must recover by
other means — is bounded precisely:

| Recoverable from python | NOT recoverable (compiled-only in `_nki_irbuilder.so`) |
|---|---|
| op names, signatures, docstrings (`isa/*.py`, `.pyi`) | the MLIR NISA-dialect op construction (`_nki_irbuilder.<m>` C++ body) |
| per-op numpy semantics (`simulator/*.py`) | the serialization of the NISA op to the 64-byte TPB struct |
| cost formulas (`.pyi`) | the operand-pattern → `start_addr/num/step` descriptor packing |
| engine routing rule (`tensor_ops.py`) | the per-engine ISel refinement (lives in `SundaISel`, a *different* `.so`) |

> **GOTCHA — the irbuilder is a SUPERSET of the 62 `isa_emit.py` emit_*.** `_nki_irbuilder.so`
> declares **69** `"NKI IR builder for …"` docstrings: the 62 emitted from `isa_emit.py`
> **plus 7 not in `isa_emit.py`**, byte-confirmed this pass by set-difference:
> `{dma_compute, rand, rand2, rand_get_state, rand_set_state, rng, set_rng_seed}`.
> [HIGH/OBSERVED — `strings _nki_irbuilder.so | rg -c '^NKI IR builder for'` = 69;
> `comm -23` against the 62 emit names yields exactly those 7.]
>
> **CORRECTION — "irbuilder-only/unreachable" is too strong for 6 of those 7.** A direct
> trace shows `dma_compute`, `rand2`, `rng`, `rand_get_state`, `rand_set_state`,
> `set_rng_seed` ARE emitted — from the *other* tracer file `mlir_tracer/isa.py`
> (`dma_compute@590`, `rng@1540`, `rand2@1563`, `rand_get_state@1596`,
> `rand_set_state@1620`, `set_rng_seed@1645`), and ARE implemented in the simulator
> (`simulator/rng.py` + `advanced.py`), and ARE public in `isa/__init__.py`. They are
> simply **not in `isa_emit.py`** and **not in the `.pyi`** — the jinja-generated `emit_*`
> roster (compiler-map's deliverable) covers 62 of the 69; the RNG/`dma_compute` family is
> reached through `isa.py` directly. The lone genuinely-vestigial one is bare **`rand`**:
> it has a `.so` docstring but no `emit_rand`/`def rand`/sim function in this build (the
> realized ops are `rand2`/`rng`). So: do not assume "irbuilder docstring ⇒ `emit_*` entry
> point" (62 of 69), nor "not in `isa_emit.py` ⇒ unreachable" (6 of the 7 are reachable
> via `isa.py`). [HIGH/OBSERVED — the `isa.py` line numbers + the `rng.py` sim impls.]

The simulator gives an escape hatch for compiled-only behavior: the generic
`extended_inst(opcode, engine, latency_ns, mem_reads, mem_writes, scalars, …,
simulate=None)` (`NKI/compiler/kernel_builder/isa.py`) carries a caller-supplied
`simulate` callback — the numpy ground-truth for an op the sim does not natively know,
e.g. a hand-assembled `EXTENDED_INST 0xf0` kernel. So even the raw-byte custom lane has a
python-supplied oracle, but the *device* op body it models is compiled/hand-written, not in
the python surface. [HIGH/OBSERVED.]

---

## 7. Version axes — the NKI op-set is not a firmware version

Five distinct version strings, all OBSERVED, joined only at the `builtin_custom_op`
handshake:

| Axis | Value | Source | Pins |
|---|---|---|---|
| NKI python op-set | `0.3.0+23928721754.g18aa1271` | `NKI/_version.py` | the `emit_*`/`isa`/simulator frontend |
| neuronx-cc wheel | `2.24.5133.0+58f8de22` | cc-stubs `.pyi` package | the `isa_tpb` tables, `SundaISel`, the documented surface |
| customop-lib device pkg | `0.21.2.0` | `aws-neuronx-gpsimd-customop-lib` | the arch-isa headers + runtime |
| ucode image | `1.21.1.0` | `builtin_custom_op(ulib_to_ucode_version=…)` | the device ucode semantic version |
| ISA table | `1.0.2520.0` | `builtin_custom_op(ulib_to_isa_version=…)` | the ISA-table version |

`builtin_custom_op(function_name, lib_file_name, ulib_to_ucode_version,
ulib_to_isa_version, srcs, dsts)` (`.pyi:411`) is the host op that pins the device
contract. The `ulib_to_ucode_version` it carries (`1.21.1.0`) is the same value the
build_custom_op codegen and the shipped device ucode JSON agree on — the complete
emit→ABI version key. So a NKI 0.3.0 program targets a device whose ucode-lib is 1.21.1.0;
**there is no single "NKI == firmware" version** — the two are independent axes bridged by
that handshake. [HIGH/OBSERVED — five distinct strings; the join point is `ulib_to_*`.]

---

## Cross-references

- [compiler-map.md](compiler-map.md) — the authoritative 62-row `emit_*`→opcode table (the lowering seam this page sits on).
- [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md) — the device opcode roster the `nki.isa` names bind against.
- [../reference/correction-ledger.md](../reference/correction-ledger.md) — the do-not-repeat ledger (the `local_gather`/`nc_n_gather` 2:1 split lives there).
- Part-15 validation replay: `neuronx-gpsimd/wiki/src/validation/` — the lane that runs the §4–5 numpy simulator as the bit-exact kernel oracle (cited as a path; per-op replay stubs may not exist yet).
