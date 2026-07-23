# Dtype-Promotion Lattice (`promote_type`)

> *All addresses, offsets, and symbols on this page apply to neuronx_cc 2.24.5133.0+58f8de22, cp310 wheel. The promotion lattice lives in `neuronxcc/starfish/penguin/dtypes.cpython-310-x86_64-linux-gnu.so` (BuildID `sha1:01c37fd6d3190601e1104533925fcb08b5d4a066`, 694 088 bytes, not stripped, Cython 3.0.10 / GNU C17 `-O3 -fwrapv -fPIC`, source `neuronxcc/starfish/penguin/dtypes.py`). The cp311/cp312 wheels carry the same logic at shifted addresses. All function bodies below were read from the binary directly; the `static_cast_*` conversion math is **not** in this wheel — see [§ The Façade Gap](#the-faade-gap-supportdtypeso).*

## Abstract

`promote_type(t0, t1)` answers one question the IR auto-cast policy asks on every binary op whose two operands disagree on dtype: *which single dtype should both be cast to before the op runs?* It is the implicit-promotion rule — the Neuron analogue of NumPy's type-promotion lattice — and it is hand-written, not table-driven. Where NumPy materialises a 2-D promotion table, neuronx-cc compiles a four-function decision tree: a dispatcher splits the (t0, t1) pair into one of three regimes (both-int, both-float, mixed), and a dedicated sub-rule resolves each regime.

The page's subject is that decision tree as it exists in `penguin/dtypes.so`: the dispatcher `promote_type` (`__pyx_pw_..._5promote_type` @ `0x1a660`, source line 41) and its three closure-captured sub-rules `_promote_floats` (`0x17370`, line 46), `_promote_ints` (`0x12400`, line 86), `_promote_float_int` (`0xd5d0`, line 118), plus the shared `_is_floating` (`0xcfa0`, line 115). Two rules dominate the float regime — **wider-itemsize-wins**, and, on a tie, a fixed eight-rung **precedence ladder** — and one format, the MX scale type `float8_e8m0fnu`, is special-cased *out* of that ladder because it is not a numeric value type at all.

This is a reimplementation reference. From the page a reader must be able to compute the promotion of *any* dtype pair, reproduce each of the four functions as pseudocode, and know which rungs of the ladder are certain (read off the binary) versus inferred. A companion concern — the actual byte-level `static_cast` conversions — lives one module over in `starfish/support/dtype.so`, which is a re-export façade over an **external** package not bundled in this wheel; that gap is stated plainly below.

| | |
|---|---|
| **Lattice module** | `penguin/dtypes.cpython-310...so` (real bodies, compiled C) |
| **Dispatcher** | `promote_type` @ `0x1a660` (wrapper), closure builder inside; source line 41 |
| **Float rule** | `_promote_floats` @ `0x17370` (size `0x2b7c`); line 46 |
| **Int rule** | `_promote_ints.constprop.0` @ `0x12400` (size `0x29bf`); line 86 |
| **Mixed rule** | `_promote_float_int` @ `0xd5d0` (size `0x1e8`); line 118 |
| **Float predicate** | `_is_floating.constprop.0` @ `0xcfa0`; line 115 |
| **Regime split** | `_is_floating` on each operand (2×2 truth table); `np.issubdtype(t, np.integer)` guards the error path |
| **Tie-break ladder** | `float64 > float32r > float32 > bfloat16 > float16 > e4m3 > e4m3fn > e5m2` |
| **e8m0 status** | excluded from the float widen ladder — it is an MX *scale*, not a value |
| **No-path error** | `"No available implicit dtype promotion path … Use .astype(dtype) explicitly."` @ rodata `0x1e1e0` |
| **Conversion math** | `static_cast_*` in `support/dtype.so` → external `neuron_dtypes` (NOT in wheel) |

---

## 1. The Reimplementation Contract

To reproduce the lattice a reader needs four things, all on this page:

- **The regime split.** `promote_type` calls `_is_floating` on both operands and uses the 2×2 result to pick a sub-rule. Both-float → `_promote_floats`; neither-float → `_promote_ints`; exactly-one-float → `_promote_float_int`. A separate `np.issubdtype(_, np.integer)` guard distinguishes a genuine int from an unrecognised dtype, which reaches the `"unexpected dtype: "` raise.
- **The float rule, in three steps.** An `e8m0fnu` guard at the top; then wider-itemsize-wins; then, on equal itemsize, a fixed precedence ladder. The result is wrapped as `np.dtype(name)`.
- **The int rule.** Both-unsigned and both-signed each reduce to wider-itemsize-wins; the mixed-sign case walks a width ladder over `uint{64,32,16,8}` / `int{32,16}` and escapes to `float32` when no integer type can hold both.
- **The mixed (float×int) rule.** The float side always wins outright — no widening of the float to cover the int's range.

The single counter-intuitive fact a reimplementer must not miss: `float8_e8m0fnu` is a *scale exponent*, not a numeric float, so it is removed from the float widen path before either the itemsize or the ladder rule runs (see [§3.1](#31-the-e8m0-guard-why-a-scale-is-not-a-value) and the cross-reference to the [MX-microscaling page](mx-microscaling.md), §9.8).

---

## 2. The Dispatcher — `promote_type`

### Structure: a closure factory

`promote_type` is not a flat function. Its body (the wrapper `__pyx_pw_..._5promote_type` @ `0x1a660`, source line 41) **defines four nested functions** and then dispatches to them. The nested functions are captured as cell variables in the closure struct `__pyx_scope_struct__promote_type` (freelist symbols at `0x22440`/`0x22460`), and each is instantiated from its method-def descriptor before the dispatch runs:

```text
__pyx_mdef_..._promote_type_1_promote_floats      @ 0x21c20   (-> _promote_floats body 0x17370)
__pyx_mdef_..._promote_type_3_promote_ints        @ 0x21c00   (-> _promote_ints  body 0x12400)
__pyx_mdef_..._promote_type_5_is_floating         @ 0x21be0   (-> _is_floating   body 0xcfa0)
__pyx_mdef_..._promote_type_7_promote_float_int   @ 0x21bc0   (-> _promote_float_int 0xd5d0)
```

The decompiled wrapper builds all three result rules (`_promote_floats` at C-line 439, `_promote_ints` at 448, `_promote_float_int` at 468) and stores the no-path error string into the closure cell `__pyx_v_no_implicit_promotion_error_message` (C-lines 431/437) so the sub-rules can raise it through the captured cell.

*Anchors: the closure cell plus four mdef instantiations inside `pw_5promote_type`.*

### The 2×2 regime split

After the closures exist, the dispatcher keys the regime on **float-ness**, calling the captured `_is_floating` once per operand, and branches on the pair of booleans. A separate `np.issubdtype(_, np.integer)` test then distinguishes a genuine integer (which routes to `_promote_ints`) from a dtype that is neither float nor int (the error path):

```c
// promote_type(t0, t1)  — penguin/dtypes.py:41, wrapper @ 0x1a660
PyObject *promote_type(PyObject *t0, PyObject *t1) {
    int t0_f = _is_floating(t0);   // call @ 0x1b5db, IsTrue @ 0x1b610
    int t1_f = _is_floating(t1);   // call @ 0x1b733

    if (t0_f && t1_f)          return _promote_floats(t0, t1);     // call @ 0x1b63a
    if (t0_f ^ t1_f)           return _promote_float_int(t0, t1);  // call @ 0x1b793 (exactly one float)
    // neither float: confirm both are integers, else raise
    if (np.issubdtype(t0, np.integer) && np.issubdtype(t1, np.integer))
        return _promote_ints(t0, t1);                              // call @ 0x1bae4
    raise "unexpected dtype: " + repr(dtype);                      // __pyx_kp_u_unexpected_dtype @ 0x1bea5
}
```

`_is_floating` (defined below) already folds in the `custom_dtypes` membership test, so the float regimes capture the Neuron custom floats that NumPy's `issubdtype(_, np.floating)` would miss. The `issubdtype(_, np.integer)` guard is used only on the both-non-float branch: `np.issubdtype(t0, np.integer)` at `0x1bbd6`/`integer`@`0x1bc29`, and `np.issubdtype(t1, np.integer)` at `0x1bd4d`/`0x1bda5`; if either is non-integral the formatter at `0x1bea5` raises.

### Error surface

| String | rodata offset | Raised when |
|---|---|---|
| `"No available implicit dtype promotion path for input dtypes {t0} and {t1}. Use .astype(dtype) explicitly."` | `0x1e1e0` | a sub-rule finds no path (held in the closure cell, C-line 437) |
| `"unexpected dtype: "` | `0x1e3c0` | an operand is neither a recognised int nor float |
| `"Cannot merge type!"` | `0x1e3a0` | the stricter `merge_type` sibling (see [§5](#5-sibling-predicates)) |

All three strings are present verbatim in the binary string pool at the offsets above.

---

## 3. The Float Rule — `_promote_floats`

`_promote_floats` (`__pyx_pf_..._promote_type__promote_floats` @ `0x17370`, size `0x2b7c`, source line 46) is the largest and most interesting sub-rule. It runs three phases in order: an e8m0 guard, the itemsize rule, and the precedence ladder.

### 3.1 The e8m0 guard — why a scale is not a value

The very first thing `_promote_floats` does is compare both operands against `float8_e8m0fnu`:

```c
// _promote_floats top — disasm 0x173d9 loads mstate+0x110 = __pyx_n_s_float8_e8m0fnu
//   getattr at decompiled C-line 192; two Py_EQ RichCompares (op=2) at 0x1741f and 0x1750e
if (t0 == float8_e8m0fnu) { ... }          // 0x17414: mov $0x2,%edx ; 0x1741f call RichCompare
if (t1 == float8_e8m0fnu) { ... }          // 0x17503: mov $0x2,%edx ; 0x1750e call RichCompare
```

`float8_e8m0fnu` (E8M0: 8-bit, exponent-only, no sign, no mantissa, bias 127, `0xFF`=NaN) is the OCP-MX block-scale format. It carries the *exponent* of a per-block scale factor, not a value drawn from a numeric range, so it is meaningless to "widen" it against another float — there is no mantissa to preserve and no monotone position on a value lattice. The guard removes it from the widen path before the itemsize or ladder rules can mistakenly select it. The deeper justification — that the device-side `bir::CastToNewDType` decode of e8m0 is a *raw byte* load (no `2^(e−127)` scaling), with the actual power-of-two applied only in the MX matmul/dequant path — belongs to the [MX-microscaling page](mx-microscaling.md) (§9.8).

*Anchors: two Py_EQ compares against `float8_e8m0fnu` at function entry; mstate field `+0x110`.*

> **QUIRK —** e8m0 is the one float-typed dtype that `_promote_floats` refuses to treat as a float. A reimplementer who folds it into the precedence ladder will produce wrong promotions for any op that mixes an MX-scale tensor with a real value tensor. Guard it first.

### 3.2 Wider-itemsize-wins

With e8m0 handled, the rule reads `.itemsize` from each operand and compares the two widths. The binary uses two ordered `RichCompare` calls with explicit op-codes:

```c
// itemsize getattr at C-lines 303/313 (and again 357/367); compares below
if (t0.itemsize < t1.itemsize) return t1;   // 0x175e4 xor %edx,%edx (Py_LT=0) ; 0x175ec RichCompare
if (t0.itemsize > t1.itemsize) return t0;   // 0x176e9 mov $0x4,%edx  (Py_GT=4) ; 0x176f4 RichCompare
// equal itemsize -> fall through to the precedence ladder (§3.3)
```

The op-codes are unambiguous: `Py_LT`=0 (zeroed `%edx` at `0x175e4`) and `Py_GT`=4 (`mov $0x4,%edx` at `0x176e9`). The wider type wins; ties fall through.

### 3.3 The precedence ladder (equal itemsize)

When the two floats have equal `.itemsize`, the rule walks a fixed precedence order and returns the first member that equals either operand, wrapped as `np.dtype(name)` (the `np.dtype` getattr appears repeatedly in the construct tail, e.g. C-lines 483/1451/1645). The ladder is the unrolled equivalent of an in-order `if/elif` scan: for each rung `d` (high → low), test `t0 == d or t1 == d`.

The order was read off the binary from the IDA-resolved `.asm` listing of `_promote_floats`: the *first* (lowest-address) `RichCompare` against each dtype global establishes that dtype's rank. The same dtype recurs at higher addresses because the rule materialises the tail ladder twice — once for the `itemsize < `-branch and once for the `itemsize > `-branch — and tests both operands (`t0 == d` then `t1 == d`); only the first-occurrence address determines precedence.

| Rank | Dtype | First compare @ (`.asm`) | Notes |
|---:|---|---|---|
| 1 | `float64` | `0x17786` | 8-byte FP |
| 2 | `float32r` | `0x178d1` | reduced/tf32-like 4-byte FP — **ranks above plain float32** |
| 3 | `float32` | `0x17a1e` | IEEE binary32 |
| 4 | `bfloat16` | `0x17b69` | 2-byte, 8-bit exponent |
| 5 | `float16` | `0x17dcc` | 2-byte, IEEE binary16 |
| 6 | `float8_e4m3` | `0x17eff` | 1-byte FP8 (1-4-3) |
| 7 | `float8_e4m3fn` | `0x18034` | 1-byte FP8 (1-4-3, finite) |
| 8 | `float8_e5m2` | `0x19a6d` | 1-byte FP8 (1-5-2) |

> **GOTCHA — the Hex-Rays listing gives the wrong FP8 order.** The decompiler interleaves the two-operand symmetric branches, so its *textual* line order reads `e4m3 > e5m2 > e4m3fn`. That is an artifact, not the evaluation order. The disassembly's first-compare addresses (`e4m3`@`0x17eff`, `e4m3fn`@`0x18034`, `e5m2`@`0x19a6d`) give the true ladder **`… e4m3 > e4m3fn > e5m2`**. *(The practical impact is narrow: all three FP8 formats are 1-byte, so these rungs only decide between two equal-itemsize FP8 operands.)*

Reconstructed:

```c
// _promote_floats — penguin/dtypes.py:46, body @ 0x17370
// PRECEDENCE order verified against the IDA disassembly (.asm first-compare addresses):
static const dtype _FLOAT_PRECEDENCE[8] = {
    float64, float32r, float32, bfloat16, float16,
    float8_e4m3, float8_e4m3fn, float8_e5m2
};

PyObject *_promote_floats(PyObject *t0, PyObject *t1) {
    // §3.1: e8m0fnu is a scale, not a widen target — handled out of band
    if (t0 == float8_e8m0fnu || t1 == float8_e8m0fnu) { /* special-cased */ }

    // §3.2: wider itemsize wins
    if (t0.itemsize < t1.itemsize) return t1;       // Py_LT @ 0x175ec
    if (t0.itemsize > t1.itemsize) return t0;       // Py_GT @ 0x176f4

    // §3.3: equal itemsize -> first precedence member matching either operand
    for (int i = 0; i < 8; i++) {                   // unrolled if/elif in the binary
        dtype d = _FLOAT_PRECEDENCE[i];
        if (t0 == d || t1 == d) return np.dtype(d); // np.dtype getattr in construct tail
    }
    return NULL;   // dispatcher then raises the no-path error
}
```

The ladder order and the itemsize comparisons are read from the binary, as is the `np.dtype` wrap; which construct path each individual rung takes to build its result is [INFERRED].

---

## 4. The Integer Rule — `_promote_ints`

`_promote_ints` (`__pyx_pf_..._2_promote_ints.constprop.0` @ `0x12400`, size `0x29bf`, source line 86) handles the both-integer regime. It tests signedness with `np.issubdtype` and resolves three cases.

```c
// _promote_ints — penguin/dtypes.py:86, body @ 0x12400
PyObject *_promote_ints(PyObject *t0, PyObject *t1) {
    // both unsigned -> wider uint wins
    // issubdtype(_, np.unsignedinteger) getattr at C-lines 232 (t0), 1821 (t1)
    if (np.issubdtype(t0, np.unsignedinteger) && np.issubdtype(t1, np.unsignedinteger))
        return (t0.itemsize >= t1.itemsize) ? t0 : t1;   // itemsize compare C-lines 513/524

    // both signed -> wider int wins
    // issubdtype(_, np.signedinteger) getattr at C-lines 339/458/637
    if (np.issubdtype(t0, np.signedinteger) && np.issubdtype(t1, np.signedinteger))
        return (t0.itemsize >= t1.itemsize) ? t0 : t1;   // itemsize compare C-lines 768/778

    // mixed sign: walk a width ladder over uint{64,32,...} / int{32,16},
    //   constructing the smallest signed type that holds both; when none can
    //   (e.g. uint64 x a signed int), escape to float32.
    //   uint64 C-line 837, uint32 C-line 932, int32 C-lines 1132/1331,
    //   float32 ONLY on this branch, C-lines 1478/1568.
    ...
}
```

Two facts are firmly grounded: the both-unsigned and both-signed cases each reduce to wider-itemsize-wins (with `>=`, so the *first* operand wins an exact tie), and the `float32` global is referenced **only** on the mixed-signedness branch — confirming the NumPy-style rule where a signed×unsigned pair that no integer type can represent exactly escapes to a float. The exact `(uintN, intM) → dtype` cells of the mixed ladder are [INFERRED] from the getattr targets (`uint64`/`uint32`/`int32`/`int16`) rather than each individually traced.

---

## 5. The Mixed Rule — `_promote_float_int`

`_promote_float_int` (`__pyx_pf_..._6_promote_float_int` @ `0xd5d0`, size `0x1e8`, source line 118) is the smallest sub-rule and the simplest to reproduce: it asks `_is_floating(t0)` and returns the float side.

```c
// _promote_float_int — penguin/dtypes.py:118, body @ 0xd5d0
PyObject *_promote_float_int(PyObject *t0, PyObject *t1) {
    if (_is_floating(t0))   return t0;   // C-line 54 call; true -> return v3 (=t0), C-line 43
    else                    return t1;   // false -> return t1, C-line 91
}
```

The body has exactly one call (to the `_is_floating` constprop) and a two-way arg passthrough: the float side always wins, with **no widening of the float to cover the int's range**. (Promoting `float16 × int64` yields `float16`, not a 64-bit float — a reimplementer who "widens to hold the int" will diverge here.)

### `_is_floating`

The shared predicate (`__pyx_pf_..._4_is_floating.constprop.0` @ `0xcfa0`, source line 115) is a two-clause OR. The explicit `custom_dtypes` membership test comes *first* because NumPy's `issubdtype(_, np.floating)` does **not** recognise the Neuron custom float dtypes (bf16, the FP8 family, FP4, e8m0):

```c
// _is_floating — penguin/dtypes.py:115, body @ 0xcfa0
int _is_floating(PyObject *dt) {
    // custom_dtypes getattr C-line 84; PySequence_Contains C-line 93
    if (PySequence_Contains(custom_dtypes, dt)) return 1;     // short-circuit
    // else fall to numpy: issubdtype getattr C-line 147, floating C-line 198
    return np.issubdtype(dt, np.floating);
}
```

The call order — `PySequence_Contains(custom_dtypes, dt)` before `np.issubdtype(dt, np.floating)` — is read from the body; the set-membership semantics of `custom_dtypes` follow from the name plus the `Contains` call.

---

## 6. Sibling Predicates

`penguin/dtypes.so` exports a family of related type predicates the legalization and inference passes call. These are grounded by name-ref sets, call shapes, and source line numbers read from the binary; the argument semantics are reconstructed from those unless noted.

| Symbol | Addr | Src line | Behaviour |
|---|---|---:|---|
| `merge_type(t0, t1)` | `0x155b0` | 16 | Stricter sibling of `promote_type` for type-inference/fusion. Merges two tensor dtypes by itemsize + signedness with `float32r`/`bfloat16` special handling (C-lines 578/657). On an incompatible pair it raises — **AssertionError**, not ValueError — with `"Cannot merge type!"` (C-line 453). |
| `can_merge_type(t0, t1)` | `0xe3a0` | 12 | Boolean guard for `merge_type`; checks both operands are compatible numeric kinds (`numbers.Integral`). |
| `is_int_bitcast(t0, t1)` | `0xf040` | 157 | True iff both are `numbers.Integral` **and** share `.itemsize` (C-lines 377/451 + 479/489) — a width-equal int reinterpret (`int32↔uint32`) is legal. |
| `scalar_dtype(scalar)` | `0xfda0` | 137 | Maps a Python/NumPy scalar to its Neuron dtype: `bool`→`uint8` (C-lines 361/410); `np.floating`/`float`→`float64` (566/598/794); `np.integer`/`int`→`int64` with an `int32` fast-path (676/838/903). Returned as `np.dtype(...)`. |

> **GOTCHA — `merge_type` raises `AssertionError`, not `ValueError`.** The message `"Cannot merge type!"` reads like a value error, but the decompiled body calls `_Pyx_Raise(_pyx_builtin_AssertionError, __pyx_kp_u_Cannot_merge_type, …)` at C-line 453. Code that catches `ValueError` around a merge will not catch this.

The integer-width predicate family (`is_int32_dtypes`/`is_int64_dtypes` and their `has_*` collection forms) is documented with the [dtype catalog](dtype-catalog.md) (§9.1) since it concerns the dtype *set*, not the promotion lattice.

---

## 7. Consumers — Where the Lattice Is Called

`promote_type` is imported (as `from neuronxcc.starfish.penguin.dtypes import promote_type`) by the modules that auto-cast operands during IR construction and legalization. Grepping the symbol across the cp310 wheel:

| Module (under `neuronxcc/`) | Role |
|---|---|
| `nki/compiler/backends/neuron/scalars.so` | scalar-op result-type inference |
| `nki/compiler/backends/neuron/KernelBuilder.so` | the NKI kernel builder (`static_cast` path) |
| `nki/isa/neuron_isa.so` | ISA-level op typing |
| `starfish/penguin/targets/transforms/InstBuilder.so` | IR instruction builder |
| `starfish/penguin/targets/transforms/LegalizeType.so` | type legalization |
| `starfish/penguin/targets/transforms/LocalLegalizeType.so` | local type legalization |
| `starfish/penguin/targets/transforms/LowerTranspose.so` | transpose lowering |

`merge_type` / `can_merge_type` are imported by a broader set — `InferPSumTensor`, `LegalizeType`, `LocalLegalizeType`, `LowerTensorOp`, `MatMultCombine`, `TongaCpyElim`, `Tonga`, `TongaInstComb`, `LowerTranspose`, `KernelBuilder` — the type-inference and fusion passes.

*Anchors: a binary-safe `grep -la` of each symbol across every `*.so` in the wheel.*

Data flow for a binary op with operands `(a, b)`:

```text
binary op (dtype a, dtype b)
  └─ InstBuilder / scalars call promote_type(a, b)
       └─ result dtype R, or raise "No available implicit dtype promotion path …"
  └─ both operands auto-cast to R via dt.static_cast(...)   [support/dtype -> neuron_dtypes]
       └─ at BIR level this lowers to bir::CastToNewDType
```

An `*_x4` packed input is first laundered (`launder_x4_dtype`, see below) to its scalar element dtype so promotion reasons about the element, not the 4-lane container, then the `_x4` tag is restored on the result. This data flow is assembled from the import list plus the façade's exported helper names rather than traced end-to-end.

---

## The Façade Gap (`support/dtype.so`)

The task scope names two modules. Only one — `penguin/dtypes.so` — contains the lattice. The second, `neuronxcc/starfish/support/dtype.cpython-310...so` (BuildID `sha1:6391eb2e…`, 107 336 bytes), is the home of the *conversion* surface (`static_cast_*`, the `_x4` helpers), and it is **a thin re-export façade, not an implementation**.

Its module-init `__pyx_pymod_exec_dtype` @ `0x379a` performs one `from neuron_dtypes import (...)` and a run of `__Pyx_ImportFrom` (symbol @ `0x247d`) + dict re-exports. There are **zero** `CyFunction_New` / function-body definitions — every conversion name is `from neuron_dtypes import NAME`. The strings prove the surface (`static_cast_fp32_to_bfloat16`, `static_cast_fp32_to_float8_e8m0fnu`, `static_cast_fp32_to_float8_e4m3`, `static_cast_fp32_to_float4_e2m1fn_x4`, `launder_x4_dtype`, `get_x1_from_x4`, `is_x4_dtype`, …) but the module that *contains* the byte-twiddling — `neuron_dtypes`, the external AWS-Neuron custom-dtype C extension — **is not bundled in this wheel** (a grep of all 768 `*.so` for the module path `neuron_dtypes` resolves only to the two façades).

Consequences for a reimplementer:

- The promotion **lattice** (this page) is fully recoverable from `penguin/dtypes.so`.
- The scalar **cast math** (RNE bf16, FP8 saturate constants, FP4 pack, e8m0 exponent bookkeeping) is **not** in this wheel. The in-wheel authoritative equivalent for byte-exact device behaviour is the BIR primitive `bir::CastToNewDType` (libBIR.so; documented separately) — the device-side twin of these host-side `static_cast_*` reference casts. Treat the `support/dtype.so` names as a *signature catalog*, and read `bir::CastToNewDType` for the actual bits.
- `launder_x4_dtype` / `get_x1_from_x4` strip an `_x4` container down to its scalar element dtype precisely so the lattice above can reason about element type; this is the mechanism by which packed MX lane formats enter `promote_type` (see the NKI dtype façade page, [`nki/dtype-facade.md`](../nki/dtype-facade.md), for the kernel-author surface over this same `support.dtype`).

---

## 8. Evidence Anchors and Limits

**Read from the disassembly or from an exact in-binary string:**

- The four-closure structure of `promote_type` and its 2×2 regime split keyed on `_is_floating(t0)`/`_is_floating(t1)` (calls @ `0x1b5db`/`0x1b733`; sub-rule call sites @ `0x1b63a`/`0x1b793`/`0x1bae4`), with `np.issubdtype(_, np.integer)` (@ `0x1bbd6`/`0x1bd4d`) guarding the both-non-float branch and the `unexpected_dtype` raise (@ `0x1bea5`); dispatcher source line 41.
- `_promote_floats`: the e8m0 guard (two Py_EQ vs `float8_e8m0fnu` at function entry, mstate +0x110), wider-itemsize-wins (Py_LT @ `0x175ec`, Py_GT @ `0x176f4`), and the precedence ladder **`float64 > float32r > float32 > bfloat16 > float16 > e4m3 > e4m3fn > e5m2`** — read from the IDA disassembly first-compare addresses (`e4m3`@`0x17eff`, `e4m3fn`@`0x18034`, `e5m2`@`0x19a6d`).
- `_promote_ints`: both-unsigned/both-signed wider-itemsize; `float32` referenced only on the mixed-sign branch.
- `_promote_float_int`: single `_is_floating(t0)` call, float side wins, no widening.
- `_is_floating = (dt in custom_dtypes) or np.issubdtype(dt, np.floating)`, membership tested first.
- Error strings `"No available implicit dtype promotion path …"` (`0x1e1e0`), `"unexpected dtype: "` (`0x1e3c0`), `"Cannot merge type!"` (`0x1e3a0`).
- `merge_type` raises `AssertionError`, not `ValueError`.
- `support/dtype.so` is a pure `from neuron_dtypes import (…)` façade; `neuron_dtypes` is absent from the wheel.
- The `promote_type` / `merge_type` consumer import lists.

**Grounded by name-set and call shape, with the exact body obscured by Cython refcount IR:**

- `custom_dtypes` set-membership semantics.
- The `_promote_ints` mixed-sign result-width table. The `float32` fallback is read from the binary; the individual `(uintN, intM) → dtype` cells are reasoned from the ladder's getattr targets.
- The `merge_type` / `can_merge_type` / `is_int_bitcast` / `scalar_dtype` reconstructions.
- `launder_x4_dtype` / `get_x1_from_x4` element-dtype semantics.

**Not recoverable from this wheel — the body lives in the external `neuron_dtypes`:**

- The exact `static_cast_*` conversion bit-twiddling. Use `bir::CastToNewDType` for byte-exact device behaviour.
- The OCP-MX e8m0 host encode/decode (`2^(e−127)`); the device path materialises e8m0 as a raw byte, and the power-of-two is applied in the MX matmul/dequant path.

The single remaining ceiling inside the lattice itself is that mixed-sign cell table: the branch *targets* (`uint64`/`uint32`/`int32`/`int16`/`float32`) are all read from the binary, but which specific signed type each `(uintN, intM)` pair lands on is reasoned from the ladder shape rather than traced pair by pair.
