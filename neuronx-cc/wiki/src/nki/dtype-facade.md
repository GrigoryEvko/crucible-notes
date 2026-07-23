# nki/dtype Façade over support.dtype

> *All addresses, offsets, and symbols on this page apply to neuronx_cc 2.24.5133.0+58f8de22, cp310 wheel (`neuronxcc/nki/dtype.cpython-310-x86_64-linux-gnu.so` and `neuronxcc/starfish/support/dtype.cpython-310-x86_64-linux-gnu.so`). The cp311/cp312 wheels carry identical module-exec logic at shifted addresses.*

## Abstract

`neuronxcc.nki.dtype` is the dtype surface a kernel author touches when they write `nki.bfloat16`, `nki.float8_e5m2`, or `nki.int32`. It is **not** where dtypes are defined. The compiled module (`nki/dtype.cpython-310...so`) contains no class bodies and no function bodies of its own — its entire `PyInit_dtype` does two `from ... import (...)` statements and copies the imported objects into its own module dict. It is a re-export façade, and a thin one: the module-exec function `__pyx_pymod_exec_dtype` at `0x2ff1` is the whole module.

The dtype objects, the byte-size predicate `sizeinbytes`, the BIR-name bridge `dtype2str`/`str2dtype`, the classification predicates `is_float_type`/`is_int_type`/`is_x4_dtype`, and the x4-unpacking helper `get_x1_from_x4` all live one layer down, in `neuronxcc.starfish.support.dtype`. That module is *itself* mostly a re-export shim: its `__pyx_pymod_exec_dtype` at `0x379a` imports those same names via `_Pyx_ImportFrom` from a still-lower numeric implementation (`custom_ml_dtypes` + `neuron_dtypes._impl`, surfaced through `neuronxcc.support.dtype_impl`). So the kernel-author API sits at the top of a three-level chain: `nki.dtype` → `starfish.support.dtype` → `dtype_impl` (`custom_ml_dtypes` / `neuron_dtypes._impl`).

This page documents the *façade* — exactly which names cross each re-export boundary, the singleton table with byte widths, and the contract of the five helpers a reimplementer must reproduce (`sizeinbytes`, `dtype2str`/`str2dtype`, `is_float_type`/`is_int_type`/`is_x4_dtype`, `get_x1_from_x4`). The deep numeric models (the FP8/FP4 bit layouts, the `static_cast_*` conversions) belong to the dtype catalog (Part 9, forward) and the BIR `Dtype` enum (Part 7, forward); here the concern is the API and the bridge token, not the bit-twiddling.

For reimplementation, the contract is:

- The exact re-export set `nki.dtype` publishes, and the surprising fact that the *standard* dtypes come from **numpy**, not from `starfish.support.dtype`.
- The singleton inventory with `sizeinbytes` widths, including the sub-byte FP8/FP4 formats and the `_x4` packed quartets.
- `dtype2str(dtype)` as the authoritative BIR-`Dtype` key, and `str2dtype(name)` as its inverse.
- The x4 packing model: `is_x4_dtype` / `get_x1_from_x4` and what "four sub-byte elements per 32-bit word" means for the size contract.

| | |
|---|---|
| **Façade module** | `neuronxcc.nki.dtype` → `nki/dtype.cpython-310...so` |
| **Module exec** | `__pyx_pymod_exec_dtype` @ `0x2ff1` (only function with logic) |
| **Re-export source A** | `neuronxcc.starfish.support.dtype` (5 custom dtypes) |
| **Re-export source B** | `numpy` (9 standard dtypes) |
| **Backing module** | `starfish/support/dtype.cpython-310...so`, exec @ `0x379a` |
| **Numeric impl** | `custom_ml_dtypes` + `neuron_dtypes._impl` via `support.dtype_impl` |
| **BIR bridge** | `dtype2str` / `str2dtype` (canonical name token = BIR `Dtype` key) |
| **Copyright** | `Copyright (c) 2024, Amazon.com` (`nki/dtype.py`) |

---

## The Re-Export Façade (`nki.dtype`)

### Purpose

`nki.dtype` exists to give kernel authors a stable, namespaced handle on the dtype singletons without exposing the `starfish` package layout or the numpy dependency. Writing `nki.bfloat16` is the documented surface; the object it resolves to is the same singleton the rest of the compiler uses. The module is a Cython extension whose *source* (`neuronxcc/nki/dtype.py`, per the `__file__` string and the `neuronxcc/nki/dtype.c` translation-unit name) is a short list of `from ... import` lines plus the module docstring `"dtype.py - this file defines nki data types"` — a docstring that overstates its job, since this file *defines* nothing.

### Algorithm

The entire module body is the import sequence below. Cython lowers each `from X import (a, b, ...)` into one `_Pyx_Import(X, name_tuple)` followed by one `_Pyx_ImportFrom` + `PyDict_SetItem` per name. There are exactly **two** `_Pyx_Import` calls in `__pyx_pymod_exec_dtype` (verified: `rg -c '_Pyx_Import('` over the decompile yields the two sites at lines 588 and 698).

```c
function pymod_exec_dtype(module_dict):              // __pyx_pymod_exec_dtype @ 0x2ff1
    // --- group A: the 5 custom Neuron dtypes ---
    nameA = ("bfloat16", "float32r", "float8e4",     // tuple built @ lines 572-587
             "float8_e5m2", "float8_e8m0fnu")
    srcA = _Pyx_Import("neuronxcc.starfish.support.dtype", nameA)   // @ 0x..588
    for n in nameA:                                  // lines 598-657
        module_dict[n] = _Pyx_ImportFrom(srcA, n)    // _Pyx_ImportFrom + PyDict_SetItem

    // --- group B: the 9 standard dtypes, FROM NUMPY ---
    nameB = ("float32", "float16", "int32", "uint32", // tuple built @ lines 670-697
             "int16", "uint16", "int8", "uint8", "bool")
    srcB = _Pyx_Import("numpy", nameB)               // @ line 698  <-- numpy, not starfish
    for n in nameB:                                  // lines 704-760
        module_dict[n] = _Pyx_ImportFrom(srcB, n)
    return 0
```

The published set is exactly those **14** names — five custom, nine from numpy — and nothing else. Two details in it routinely trip people up.

The boolean is re-exported under the numpy attribute name **`bool`**, so the user-visible handle is `nki.dtype.bool`. The token `bool_` also sits in the string pool, but it is a separate interned constant and is never bound into the module dict.

> **GOTCHA — `tfloat32` and `float8_e4m3` are in the string pool but are *not* re-exported.**
> Both tokens are interned by `Pyx_CreateStringTabAndInitStrings` @ `0x2850`, so a `strings`
> sweep of `nki/dtype.so` finds them and suggests a 16-name façade. Neither appears in any
> `_Pyx_ImportFrom`/`PyDict_SetItem` site in the module-exec body — only the variable
> declarations exist. `tfloat32` *is* a real singleton one layer down in
> `support.dtype`; it simply is not skinned into `nki.dtype`. Interned name constants are
> not evidence of a binding.

### Function Map

The module has no Python-level functions; it is one module-exec body. The relevant native symbols:

| Symbol | Addr | Role | Confidence |
|---|---|---|---|
| `__pyx_pymod_exec_dtype` | `0x2ff1` | The whole module: two `from..import` re-exports | CERTAIN |
| `Pyx_CreateStringTabAndInitStrings` | `0x2850` | Interns all name constants incl. the two unused ones | CERTAIN |
| `PyInit_dtype` | `0x4166` | C extension init entry; calls module-exec | CERTAIN |
| `_Pyx_Import` | (thunk) | One call per `from`-group (2 total) | CERTAIN |
| `_Pyx_ImportFrom` | (thunk) | One call per imported name (14 total) | CERTAIN |

> **GOTCHA —** a reimplementer who treats `nki.dtype` as the *definition* site will look for `sizeinbytes`/`dtype2str` here and find nothing. Those are attributes of `starfish.support.dtype`, never re-bound into `nki.dtype`. The kernel-author API is *only* the dtype singletons; the predicates and the BIR bridge live one module down and are imported by the compiler internals, not by user kernels through the `nki.dtype` namespace.

---

## The Backing Module (`starfish.support.dtype`)

### Purpose

`starfish.support.dtype` is the module the rest of neuronx_cc imports when it needs to ask *"how many bytes is this dtype"* or *"what BIR name does this dtype map to"*. It owns the canonical singletons (or, more precisely, re-publishes them from the impl layer) and the predicate/bridge helper surface. Like `nki.dtype` it is largely a re-export shim — its module-exec `__pyx_pymod_exec_dtype` @ `0x379a` resolves every helper and singleton through `_Pyx_ImportFrom`, pulling them from the lower numeric implementation. The names it republishes (verified from the `_Pyx_ImportFrom` target list in the exec body) are the full superset, including the `_x4` packed formats and `int64`/`uint64` that `nki.dtype` does not surface.

### The Singleton Inventory

The canonical dtype objects and their `sizeinbytes` widths. The custom (non-numpy) formats carry explicit name-string constants in the `.rodata` of `support/dtype.so` (verified present: `bfloat16`, `float32r`, `float4_e2m1fn_x4`, `float8`, `float8_e3m4`, `float8e4`, `float8_e4m3`, `float8_e4m3fn`, `float8_e4m3fn_x4`, `float8_e5m2`, `float8_e5m2_x4`, `float8_e8m0fnu`); the standard formats are numpy dtype objects and have no string in this module's pool.

| Singleton | `sizeinbytes` | Class | Source | Confidence |
|---|---|---|---|---|
| `float32` | 4 | IEEE binary32 | numpy | CERTAIN |
| `float32r` | 4 | FP32 "round"/reduced-precision (TF32-class) | custom | HIGH |
| `tfloat32` | 4 (19-bit significant) | TensorFloat-32 | custom | HIGH |
| `bfloat16` | 2 | brain-float16 | custom (`custom_ml_dtypes`) | CERTAIN |
| `float16` | 2 | IEEE binary16 | numpy | CERTAIN |
| `float8` / `float8e4` | 1 | FP8 (default = E4M3 alias) | custom | HIGH |
| `float8_e4m3` / `float8_e4m3fn` | 1 | FP8 E4M3 (and finite-only `fn`) | custom | CERTAIN |
| `float8_e5m2` | 1 | FP8 E5M2 | custom | CERTAIN |
| `float8_e8m0fnu` | 1 | FP8 E8M0 (unsigned scale exponent) | custom | CERTAIN |
| `float8_e3m4` | 1 | FP8 E3M4 | custom | HIGH |
| `float8_e4m3fn_x4` | 4 (packed: 4×1B) | FP8 E4M3 quartet, 4 elems / 32-bit word | custom `_x4` | CERTAIN |
| `float8_e5m2_x4` | 4 (packed: 4×1B) | FP8 E5M2 quartet | custom `_x4` | CERTAIN |
| `float4_e2m1fn_x4` | 2 (packed: 4×4-bit) | FP4 E2M1 quartet, 4 nibbles / 16-bit word | custom `_x4` | CERTAIN |
| `int8` / `uint8` | 1 | signed / unsigned byte | numpy | CERTAIN |
| `int16` / `uint16` | 2 | | numpy | CERTAIN |
| `int32` / `uint32` | 4 | | numpy | CERTAIN |
| `int64` / `uint64` | 8 | (not surfaced in `nki.dtype`) | numpy | CERTAIN |
| `bool` | 1 | | numpy | CERTAIN |

> **NOTE —** the `_x4` "packed" formats are the reason `sizeinbytes` and the element width diverge. An `_x4` dtype is a logical vector of four sub-byte scalars stored contiguously in one machine word: `float8_*_x4` packs 4×8-bit into a 32-bit word (4 bytes), `float4_e2m1fn_x4` packs 4×4-bit nibbles into a 16-bit word (2 bytes). `sizeinbytes` reports the *packed word* width, not the scalar element width. To recover the scalar element you call `get_x1_from_x4` (below). Group-level membership of the float set is published as `float_dtypes` (string constant present).

### The BIR-Dtype Bridge: `dtype2str` / `str2dtype`

The bridge from a Python dtype object to the BIR `Dtype` enum (Part 7) is the `dtype2str` / `str2dtype` pair. `dtype2str(dtype)` returns the canonical name token — `"bfloat16"`, `"float8_e4m3"`, `"float4_e2m1fn_x4"`, etc. — and that token is *the same string* the BIR `Dtype` enum keys on. A reimplementer should treat `dtype2str(dtype)` as the authoritative serialization key when emitting BIR and `str2dtype(name)` as the inverse when parsing it; the token, not the Python object identity, is what crosses the BIR boundary.

```c
// Contract (bodies live in the impl layer; surfaced via support.dtype re-export):
function dtype2str(dtype) -> str:        // n_s_dtype2str, ImportFrom @ support.dtype exec
    // canonical name token; identical to the BIR Dtype enum spelling.
    // e.g. bfloat16 -> "bfloat16",  float4_e2m1fn_x4 -> "float4_e2m1fn_x4"
    return canonical_name[dtype]

function str2dtype(name) -> dtype:       // n_s_str2dtype
    // inverse lookup: BIR token -> the singleton object
    return singleton_by_name[name]
```

> **QUIRK —** both `dtype2str` and `str2dtype` are *imported* into `starfish.support.dtype`, not defined there — `__pyx_pymod_exec_dtype` @ `0x379a` resolves them through `_Pyx_ImportFrom` alongside the singletons and the `static_cast_*` conversions (the full ImportFrom target list includes `n_s_dtype2str`, `n_s_str2dtype`, `n_s_sizeinbytes`, `n_s_is_x4_dtype`, `n_s_get_x1_from_x4`, `n_s_static_cast`, and ~16 `static_cast_*_to_fp32` / `static_cast_fp32_to_*` entries). The numeric truth lives in `custom_ml_dtypes` and `neuron_dtypes._impl`; `support.dtype` is the curation/namespacing layer, and `nki.dtype` is the user-facing skin over *that*.

### Classification Predicates and x4 Unpacking

The kernel-author-relevant predicates, all re-exported through `support.dtype` (names verified in the string pool and the exec ImportFrom list):

```c
function sizeinbytes(dtype) -> int:      // n_s_sizeinbytes
    // packed-word byte width per the singleton table above.
    // sub-byte scalars (fp8/fp4) report 1 (fp8) or are only reachable
    // packed as _x4 (fp4 has no unpacked singleton in the inventory).

function is_float_type(dtype) -> bool:   // n_s_is_float_type
    // True for the float_dtypes group (bf16/fp16/fp32/fp32r/tf32/fp8.../fp4_x4)

function is_int_type(dtype) -> bool:     // n_s_is_int_type
    // True for int8/16/32/64 and uint8/16/32/64

function is_number(x) -> bool:           // n_s_is_number
    // host-side scalar number test (used by the tracer's category dispatch)

function is_x4_dtype(dtype) -> bool:     // n_s_is_x4_dtype
    // True iff dtype is one of the *_x4 packed quartet formats
    // (float8_e4m3fn_x4, float8_e5m2_x4, float4_e2m1fn_x4)

function get_x1_from_x4(dtype) -> dtype: // n_s_get_x1_from_x4   (alias: x4_to_x1)
    // map a packed x4 dtype to its scalar (x1) element dtype:
    //   float8_e4m3fn_x4 -> float8_e4m3fn   (1 byte scalar)
    //   float8_e5m2_x4   -> float8_e5m2     (1 byte scalar)
    //   float4_e2m1fn_x4 -> <fp4 e2m1 scalar nibble>
    // (launder_x4_dtype is the related normalizer in the same family)
```

> **GOTCHA —** `sizeinbytes` of an `_x4` dtype is the *packed word* size, so naive `count * sizeinbytes(dtype)` buffer math counts 4× the logical scalars correctly only if `count` is the number of packed words, not the number of scalar elements. A reimplementer iterating scalar elements must first call `get_x1_from_x4` to get the per-element dtype (and the per-element bit width — 8 bits for fp8 quartets, 4 bits for the fp4 quartet) before sizing. `is_x4_dtype` is the guard that tells you whether that unpacking step is required.

### Function Map

| Helper | Native form | Defined in | Confidence |
|---|---|---|---|
| `sizeinbytes` | re-exported into `support.dtype` | impl layer | CERTAIN (name) |
| `dtype2str` | re-exported (BIR key producer) | impl layer | CERTAIN (name) |
| `str2dtype` | re-exported (BIR key inverse) | impl layer | CERTAIN (name) |
| `is_float_type` / `is_int_type` / `is_number` | re-exported predicates | impl layer | CERTAIN (name) |
| `is_x4_dtype` | re-exported packing predicate | impl layer | CERTAIN (name) |
| `get_x1_from_x4` / `x4_to_x1` / `launder_x4_dtype` | re-exported x4 helpers | impl layer | CERTAIN (name) |
| `as_native_type` / `finfo` | numpy-backing / float-info | impl layer | HIGH |
| `static_cast` + ~16 `static_cast_*` | host numeric conversions | impl layer | CERTAIN (names) |

---

## The Impl Layer (`support.dtype_impl`)

The bottom of the chain. `neuronxcc.support.dtype_impl.__init__` is itself a shim: its string pool contains exactly `custom_ml_dtypes` and `neuron_dtypes._impl` as import targets (verified — those two tokens plus the standard `cannot import name %S` / re-init guard strings are the only meaningful entries). `custom_ml_dtypes` is the ml-dtypes-style library carrying the FP8/FP4/bf16 numeric models (the bit layouts, rounding, the `static_cast_*` conversions); `neuron_dtypes._impl` adds the Neuron-specific formats (`float32r`, the `_x4` packings, `float8e4`). `support.dtype` re-publishes the union with curated names; `nki.dtype` skins the user-visible slice.

```text
nki.dtype                    (5 custom + 9 numpy re-exports — user API)
   └─ starfish.support.dtype (singletons + dtype2str/str2dtype + predicates)
        └─ support.dtype_impl (__init__ shim)
             ├─ custom_ml_dtypes        (FP8/FP4/bf16 numeric models + casts)
             └─ neuron_dtypes._impl     (float32r, _x4 packings, neuron formats)
```

> **NOTE —** three layers of re-export is deliberate, not accident. The impl layer can be swapped (e.g. a newer `custom_ml_dtypes`) without touching `support.dtype`'s curated namespace; `support.dtype` can reorganize without breaking the `nki.*` kernel-author contract. The cost is that *no single module file* holds the dtype definitions — a reimplementer must follow the chain to `custom_ml_dtypes` / `neuron_dtypes._impl` to find the actual bit math, which is the subject of the Part 9 dtype catalog.

---

## Evidence anchors and limits

Read directly from the two `.so` files:

- **`nki.dtype` carries no bodies of its own.** The only logic-bearing function in the module is `__pyx_pymod_exec_dtype` @ `0x2ff1`, and its body is two `_Pyx_Import` calls plus 14 `_Pyx_ImportFrom`/`PyDict_SetItem` pairs — nothing else.
- **The standard dtypes come from numpy.** The second `_Pyx_Import` target is `__pyx_n_s_numpy`, and the nine names `float32/float16/int32/uint32/int16/uint16/int8/uint8/bool` are pulled with `_Pyx_ImportFrom(numpy, …)`.
- **`tfloat32` and `float8_e4m3` are interned but never bound** — they exist only in `Pyx_CreateStringTabAndInitStrings` @ `0x2850`, with zero `ImportFrom`/`SetItem` uses.
- **`dtype2str` / `str2dtype` are imported, not defined, by `support.dtype`** — `n_s_dtype2str` and `n_s_str2dtype` both appear in the `_Pyx_ImportFrom` target list of that module's exec @ `0x379a`.
- **The `_x4` family exists as named singletons**: `float8_e4m3fn_x4`, `float8_e5m2_x4`, `float4_e2m1fn_x4`, together with the `is_x4_dtype` / `get_x1_from_x4` / `x4_to_x1` helpers.

Two things on this page are reconstructed rather than read. First, the claim that the `dtype2str` token is byte-identical to the BIR `Dtype` enum spelling rests on the shared canonical name strings (`bfloat16`, `float8_e4m3`, `float4_e2m1fn_x4`) appearing in both layers, not on a per-token comparison. Second, the `_x4` widths (fp8_x4 = 4 bytes, fp4_x4 = 2 bytes) and the "reports the packed word, not the scalar" behaviour of `sizeinbytes` follow from the packing arithmetic — 4 elements × element bits — since the `sizeinbytes` body lives in the impl layer and was not decompiled here.

---

## Cross-References

- [nki/type-system](type-system.md) — 6.3.1, the tracer that consumes these dtypes via `is_number` and category dispatch
- [dtype catalog](../catalog/dtype-catalog.md) — Part 9 (forward), the FP8/FP4/bf16 bit layouts and `static_cast_*` numeric models behind the impl layer
- BIR `Dtype` enum — Part 7 (forward), the consumer of the `dtype2str` token across the BIR boundary
