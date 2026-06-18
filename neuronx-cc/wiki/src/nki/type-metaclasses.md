# Type Metaclasses & the Abstract Type Lattice

> *All addresses, line numbers, and symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 are structurally identical). The metaclass layer lives in `neuronxcc/nki/compiler/backends/neuron/metaclasses.cpython-310-x86_64-linux-gnu.so` — a Cython 3.0.10 extension. For `.text`/`.rodata` the virtual address equals the file offset. Every method line number is the `__FILE__`/lineno pair recovered from the function's `_Pyx_TraceSetupAndCall` argument (`"neuronxcc/nki/compiler/backends/neuron/metaclasses.py", N`); treat both as version-pinned.*

## Abstract

NKI's user-facing tile types — `tensor`, `tile`, `tile_index`, `mask`, `scalar` ([6.2.1](tile-tensor-model.md)) — are ordinary Python classes, but every one of them is built by a **metaclass** out of `metaclasses.so`. The metaclass is what makes `scalar[np.int32]` and `tensor[dtype, shape, addrspace]` legal subscript syntax, what makes `isinstance(x, tensor[bfloat16, (128,512), 'psum|sbuf'])` a *structural* check on dtype/shape/address-space instead of a nominal one, and what attaches the `Mutable`/`Immutable`/`ParDim` modifiers and the `Allocation`/`AutoAllocation` placement records to a parametrized type. This page recovers that metaclass layer: the base metaclass `nki_type`, its two specializations `scalar_type` and `tensor_type`, the *quantified* forms `modified_scalar_type` / `modified_tensor_type` that `T[...]` produces, the `StaticConstProperty` metaclass, the `nki_dtype` group metaclass, and the `Allocation` lattice that drives address-space compatibility.

The central mechanism is a two-stage `__instancecheck__`. `nki_type.__instancecheck__` (line 176) first asks the class for a **dynamic** verdict via `_dynamic_instancecheck`; only if that returns `None` does it fall through to the normal nominal `super().__instancecheck__`. For a *quantified* tensor type, `modified_tensor_type.__instancecheck__` (line 279) overrides this with a full structural comparison: the candidate must match on `tensor_dtype`, `tensor_shape`, `tensor_addrspace`, and `tensor_ir_class`, with address-space/IR-class compatibility resolved through `Allocation.is_superclass` (line 118). The result is a type system where a *type object* carries concrete dtype/shape/placement constraints and `isinstance` enforces them — the abstract tile-type lattice that the front-end tracer ([6.3.1](type-system.md)) and the `nisa`/`nl` validators consult on every traced value.

The bar for this page: a reimplementer can rebuild the metaclass hierarchy, the `T[...]` subscript → `modified_*_type` construction path, the structural `__instancecheck__`, the `Modifiers` set, and the `Allocation` address-space lattice from the page alone.

| | |
|---|---|
| **Module** | `neuronxcc/nki/compiler/backends/neuron/metaclasses.so` (Cython 3.0.10) |
| **Copyright string** | `"Copyright (c) 2023, Amazon.com. All Rights Reserved"` (CONFIRMED) |
| **Base metaclass** | `nki_type(type)` — `Base metaclass for NKI types (tensor and scalar)` |
| **Specializations** | `scalar_type(nki_type)`, `tensor_type(nki_type)` |
| **Quantified forms** | `modified_scalar_type(scalar_type)`, `modified_tensor_type(tensor_type)` |
| **Const-property MC** | `StaticConstProperty(type)` |
| **dtype group MC** | `nki_dtype(type)` — `isinstance` = membership in `cls.values` |
| **Modifiers** | `{Mutable, Immutable, ParDim}` (CONFIRMED strings; enum name `Modifiers`) |
| **Placement lattice** | `@dataclass(frozen=True) Allocation` + `AutoAllocation(Allocation)` |
| **Addr-space tags** | `psum\|sbuf`, `private_hbm`, `shared_hbm`, `tiled_hbm` (CONFIRMED) |
| **Two-stage check** | `nki_type.__instancecheck__` @176 → `_dynamic_instancecheck` → `super()` |
| **Structural check** | `modified_tensor_type.__instancecheck__` @279 (dtype/shape/addrspace/ir_class) |

---

## 1. The metaclass hierarchy

All NKI tile classes are *pure-Python* classes (no Cython `cdef` extension structs — confirmed absent in [6.2.1](tile-tensor-model.md)), created with `class X(Base, metaclass=tensor_type)`. The metaclass tree is shallow and entirely about *parametrized types*:

```text
type
 ├── StaticConstProperty            (read-only class-level @property)
 ├── nki_dtype                      (dtype-group membership metaclass)
 └── nki_type                       "Base metaclass for NKI types (tensor and scalar)"
      ├── scalar_type               → produces modified_scalar_type  on  scalar[dtype]
      │     └── modified_scalar_type
      └── tensor_type               → produces modified_tensor_type  on  tensor[dtype,shape,...]
            └── modified_tensor_type
```

`scalar` and `tensor` (the classes in `tensor.py`, [6.2.1](tile-tensor-model.md)) are *instances* of `scalar_type` / `tensor_type`. The bare class `tensor` is an unparametrized type; `tensor[bfloat16, (128,512)]` is a *new class*, an instance of `modified_tensor_type`, manufactured on the fly by `__getitem__`. This is the abstract type lattice referred to as W06's `tensor_type` — the metaclass *is* the lattice constructor.

> **NOTE —** The docstrings are crisp and CONFIRMED verbatim: `scalar_type` is `"Metaclass for scalar types that allows parameterized types. This enables syntax like: scalar: Basic scalar register value / scalar[np.int32]: Typed scalar register with specific dtype …"`; `tensor_type` is `"This is the metaclass for the tensor class."` and `modified_tensor_type` is `"This is the metaclass for the quantified tensor class."` ("quantified" = the dtype/shape-parametrized form).

Module imports (CONFIRMED from the string/import tables): `inspect` (Signature/Parameter/bind/apply_defaults), `dataclasses` (`@dataclass(frozen=...)`), `numpy as np`, `typing`, the canonical dtype singletons from `neuronxcc.starfish.support.dtype` (`bfloat16`, `float32r`, `tfloat32`, `float8_e4m3fn_x4`, `float4_e2m1fn_x4`, …), the IR tensor classes `NeuronSBTensor` / `NeuronPSUMTensor` / `NeuronLocalTensor` / `DRAM3DBlockTensor`, and `CoreV4`.

> **CORRECTION —** The backing report (D-W09 §4.1) lists `import os` and `from enum import Enum`. Neither `os`, `enum`, nor `IntEnum` appears in this module's string/import pool. `Modifiers` is referenced (CONFIRMED string `"Modifiers"` @ `0x3e9f0`) with members `Mutable`/`Immutable`/`ParDim`, but its *base* (Enum vs. plain class) and *definition site* are not visible in this module — it is most likely imported from a sibling. Treat "`Modifiers` is an `enum.Enum`" as **INFERRED**, not confirmed.

---

## 2. `nki_type` — the base metaclass

`nki_type(type)` implements the parametrization protocol shared by scalars and tensors. Its seven methods (line numbers CONFIRMED):

| Method | Line | Addr | Role |
|---|---|---|---|
| `_dynamic_instancecheck(cls, instance)` | — | `0x26540` | overridable dynamic-verdict hook (returns bool or `None`) |
| `__instancecheck__(cls, instance)` | 176 | `0x30c50` | two-stage check (dynamic → nominal) |
| `extract_items(self, items)` | — | `0x25e90` | normalize the `[...]` subscript payload |
| `_create_modified_type(self, attrs)` | — | `0x25860` | build the quantified class (overridden) |
| `_get_call_signature(self)` | — | `0x255d0` | the `inspect.Signature` for `cls(...)` |
| `__getitem__(self, items)` | 185 | `0x2e160` | `T[...]` → `_create_modified_type(extract_items(items))` |
| `__call__(cls, *a, **kw)` | 198 | `0x38a30` | bind args to signature, `create_instance(**bound)` |

### 2.1 The two-stage `__instancecheck__`

```c
// nki_type.__instancecheck__  @0x30c50  (metaclasses.py:176)
PyObject *nki_type___instancecheck__(cls, instance) {
    // Stage 1: ask the class for a DYNAMIC verdict.
    PyObject *r = GetAttr(cls, "_dynamic_instancecheck")(instance);   // 0x26540
    // Recovered control flow compares r against the three singletons:
    if (r != Py_None)                  // r is True / False (or any non-None)
        return r;                      // dynamic verdict wins
    // Stage 2: fall through to the normal nominal check.
    return super(nki_type, cls).__instancecheck__(instance);   // _pyx_builtin_super
}
```

The decompile confirms the shape: it fetches `super` (`_pyx_builtin_super`) and the attribute `__instancecheck__`, *and* fetches `_dynamic_instancecheck`, *and* tests the candidate result against `Py_TrueStruct`, `Py_FalseStruct`, and `Py_NoneStruct`. The base `_dynamic_instancecheck` is the extension point: a plain `tensor`/`scalar` returns `None` (no dynamic constraint → nominal `isinstance`), while a quantified type overrides it (or overrides `__instancecheck__` outright, §4) to perform a structural comparison.

> **GOTCHA —** This is a *metaclass* `__instancecheck__`, so it fires for `isinstance(x, tensor)` — i.e. the second argument `tensor` is the *instance of the metaclass* whose `__instancecheck__` runs. A reimplementer who puts the structural logic on the class rather than the metaclass will silently get nominal-only checks.

### 2.2 `__getitem__` — the `T[...]` constructor

```c
// nki_type.__getitem__  @0x2e160  (metaclasses.py:185)
PyObject *nki_type___getitem__(self, items) {
    PyObject *attrs = GetAttr(self, "extract_items")(items);   // normalize subscript
    return GetAttr(self, "_create_modified_type")(attrs);      // build quantified class
}
```

`extract_items` (overridden per metaclass) parses the heterogeneous subscript tuple `[dtype, shape, Modifiers, Allocation, ...]` into a normalized attribute dict; `_create_modified_type` (overridden) calls `modified_scalar_type(...)` / `modified_tensor_type(...)` to mint the new class. So `scalar[np.int32]` and `tensor[bfloat16, (128,512), 'psum|sbuf']` are both `self._create_modified_type(self.extract_items(...))`.

### 2.3 `__call__` — signature-bound instantiation

```c
// nki_type.__call__  @0x38a30  (metaclasses.py:198)
PyObject *nki_type___call__(cls, *args, **kwargs) {
    PyObject *sig   = GetAttr(cls, "_get_call_signature")();          // inspect.Signature
    PyObject *bound = GetAttr(sig, "bind")(*args, **kwargs);          // BoundArguments
    GetAttr(bound, "apply_defaults")();                              // fill omitted params
    return GetAttr(cls, "create_instance")(**GetAttr(bound, "arguments"));
}
```

Instantiation routes through an `inspect.Signature` (built by `_get_call_signature`, using `inspect.Parameter` with `POSITIONAL_OR_KEYWORD` / `KEYWORD_ONLY` kinds — both strings CONFIRMED) so that positional and keyword arguments are normalized to a single `arguments` dict before `create_instance` runs. `scalar_type._get_call_signature` (`0x29610`) declares the `dtype` parameter; `tensor_type._get_call_signature` (`0x28b90`) declares `shape` then `dtype`.

---

## 3. `scalar_type` / `tensor_type` and their quantified forms

`scalar_type` and `tensor_type` each override `extract_items`, `_create_modified_type`, and `_get_call_signature`. `tensor_type.extract_items` (`0x36d70`) is the richer parser — it consumes `[dtype, shape, Modifiers, Allocation, ...]`, validates the dtype operand against the `nki_dtype` groups and `_is_np_dtype` (`0x2a580`), and uses `any_as` (alias resolution) and the `Modifiers`/`Allocation` operands. The output attribute dict carries the four concrete constraint fields that the quantified class stores: **`tensor_dtype`**, **`tensor_shape`**, **`tensor_addrspace`**, **`tensor_ir_class`** (all four CONFIRMED as `tensor_*` strings).

`modified_tensor_type.__new__` (`0x2fb60`) assembles those into the class's `type_attrs` and calls `type.__new__`. An over-supplied subscript raises `"unexpected attr in modified tensor type."` (CONFIRMED verbatim — note the message says "modified tensor type", **not** "modified_tensor_type"; the scalar twin is `"unexpected attr in modified scalar type."`).

> **CORRECTION —** D-W09 §4.4/§4.5 give the error strings as `"unexpected attr in modified_scalar_type"` / `"...modified_tensor_type"` (underscored). The binary strings are space-separated: `"unexpected attr in modified scalar type."` and `"unexpected attr in modified tensor type."`.

---

## 4. The structural `__instancecheck__` (the heart of the lattice)

A *quantified* tensor type carries concrete `tensor_dtype` / `tensor_shape` / `tensor_addrspace` / `tensor_ir_class`. Its `__instancecheck__` performs a full structural comparison against the candidate:

```c
// modified_tensor_type.__instancecheck__  @0x32520  (metaclasses.py:279)
bool modified_tensor_type___instancecheck__(cls, instance) {
    // The candidate must first be (an instance of) a tensor_type at all.
    if (!isinstance(instance, tensor_type))                    // n_s_tensor_type
        return False;
    // Match the four concrete constraints. (Fields referenced in the body:
    //   instance.dtype / instance.shape  vs  cls.tensor_dtype / cls.tensor_shape;
    //   address-space + IR-class via Allocation.is_superclass.)
    if (GetAttr(instance, "dtype")  != cls.tensor_dtype)  return False;   // n_s_dtype
    if (GetAttr(instance, "shape")  != cls.tensor_shape)  return False;   // n_s_shape
    // addr-space / IR-class compatibility is a SUPERCLASS relation, not equality:
    return cls.tensor_addrspace.is_superclass(<instance addr-space/ir_class>);
                                                            // n_s_is_superclass
}
```

The decompile of `0x32520` references exactly: `n_s_tensor_type`, `n_s_dtype`, `n_s_shape`, `n_s_tensor_dtype`, `n_s_tensor_shape` (twice), `n_s_tensor_addrspace`, `n_s_tensor_ir_class`, `n_s_is_superclass`. So the check is: *nominal* on dtype and shape, but *subtype* on address-space/IR-class — a `psum|sbuf`-allocated tile is accepted where an SBUF type is required only if `is_superclass` says the allocation classes are compatible (§6). `modified_scalar_type.__instancecheck__` (`0x20ed0`) is the scalar analogue, matching `instance.dtype == required_dtype`.

> **QUIRK —** dtype and shape use `==`, but placement uses `is_superclass`. Address-space is the one *covariant* axis of the lattice: a more specific allocation (e.g. a concrete SBUF placement) satisfies a more general requirement, while dtype and shape must match exactly. A reimplementer who makes all four axes equality-checks will reject legal tiles.

### 4.1 `is_has_mutable_modifier` — reading the `Modifiers` set

```c
// modified_tensor_type.is_has_mutable_modifier  @0x23d90  (metaclasses.py:270)
bool modified_tensor_type_is_has_mutable_modifier(cls) {
    return Modifiers.Mutable in cls.modifiers;   // n_s_Modifiers, n_s_Mutable
}
```

The body references `n_s_Modifiers` and `n_s_Mutable` only (CONFIRMED), confirming the `Modifiers` enum has at least `Mutable`, and that a quantified type carries a `modifiers` collection. The full member set `{Mutable, Immutable, ParDim}` is CONFIRMED from the three standalone strings; their semantics — `Mutable`/`Immutable` gate whether a tile can be written, `ParDim` marks the partition dimension — are INFERRED from naming and the partition-dim contract in [6.2.1](tile-tensor-model.md).

---

## 5. `nki_dtype` — the dtype-group metaclass

Independent of the tile lattice, `nki_dtype(type)` is the metaclass for *dtype groups* (`nki_float_dtype`, `nki_int_dtype`, `nki_scalar_dtype`, …). Its `isinstance` is pure set membership:

```c
// nki_dtype.isinstance  @0x27240  (metaclasses.py:69)
bool nki_dtype_isinstance(cls, instance) {
    return Contains(cls.values, instance);   // instance in cls.values
}
```

The body references `n_s_cls`, `n_s_instance`, `n_s_values` and the `Contains` (PySequence/PyMapping membership) op (CONFIRMED). So `nki_float_dtype.isinstance(bfloat16)` is `bfloat16 in nki_float_dtype.values`. `tensor_type.extract_items` consults these groups to validate the dtype operand of a parametrized tensor type. `_is_np_dtype(t)` (`0x2a580`) is the numpy-side companion (`isinstance(t, np.dtype)` / np scalar type).

---

## 6. `Allocation` / `AutoAllocation` — the address-space lattice

Address-space compatibility (the covariant axis of §4) is resolved by a small frozen dataclass lattice:

```c
// Allocation.is_superclass  @0x2b700  (metaclasses.py:118)
bool Allocation_is_superclass(self, other) {
    // 'other' is acceptable iff every member of other's tensor-class set is in
    // self's tensor_class_type, minus an 'excludes' set. Realized as all(genexpr).
    return all(c in self._tensor_class_type        // n_s_tensor_class_type
               for c in <other's classes>
               if c not in self.excludes);          // n_s_excludes
}
```

`@dataclass(frozen=True) Allocation` (the `dataclass`/`frozen` strings are CONFIRMED) models a memory-class / address-space placement. `is_superclass(self, other)` (body references `n_s_Allocation`, `n_s_excludes`, `n_s_other`, `n_s_tensor_class_type`, and an `all(...)` generator) decides whether `self` (the required allocation) is a *superclass* of `other` (the candidate's) by checking that the candidate's IR tensor classes fall within `self`'s `_tensor_class_type` set, after removing an `excludes` set. `AutoAllocation(Allocation)` adds `auto_allocate` (`0x341d0`) — the compiler-chooses-placement variant (see 6.5.15, alloc decorators).

The IR-class → address-space mapping is `ir_tensor_class_to_addr_space`:

```c
// ir_tensor_class_to_addr_space  @0x26b30  (metaclasses.py:151)
PyObject *ir_tensor_class_to_addr_space(cls) {
    return _as_translations.get(cls);    // dict lookup, default None
}
```

`_as_translations` maps the IR tensor classes (`NeuronSBTensor`, `NeuronPSUMTensor`, `NeuronLocalTensor`, `DRAM3DBlockTensor` — all CONFIRMED) to address-space tags. The address-space tag *strings* present in the binary are **`psum|sbuf`, `private_hbm`, `shared_hbm`, `tiled_hbm`** (CONFIRMED).

> **CORRECTION —** D-W09 §4.7 lists the tags as `{sbuf, psum, psum_sbuf, local, hbm, private_hbm, shared_hbm, tiled_hbm}`. The standalone strings `sbuf`, `psum`, `psum_sbuf`, `local`, and bare `hbm` are **not** present in this module. The on-chip SBUF/PSUM tag is the single compound token `psum|sbuf`; the off-chip tags are `private_hbm` / `shared_hbm` / `tiled_hbm`. The per-IR-class pairing inside `_as_translations` is **INFERRED** from naming; only the tag string set is confirmed.

---

## 7. `StaticConstProperty` — read-only class attributes

`StaticConstProperty(type)` is a small metaclass giving classes *static* (class-level, instance-independent) read-only properties. Docstring CONFIRMED: `"This allows us to have static properties that aren't tied to an instance of a class. By default, python properties (decorated with @property) are tied to an instance. This metaclass allows us to have static properties."`

```c
// StaticConstProperty.__getattribute__  @0x33210  (metaclasses.py:402)
//   resolve a descriptor 'get' on the named attr, else fall to type.__getattribute__.
// StaticConstProperty.__setattr__       @0x20810  (metaclasses.py:409)
void StaticConstProperty___setattr__(cls, name, value) {
    raise Error("cannot assign to a static const property.");   // CONFIRMED verbatim
}
// getattr_for_stubgen  @0x27920  — stub-generation hook that bypasses the const guard
//   (returns type.__getattribute__(cls, name) directly).
```

Any attempt to *write* a static const property raises `"cannot assign to a static const property."` (CONFIRMED). `getattr_for_stubgen` is the escape hatch the stub generator uses to read declarations without tripping the const guard.

---

## 8. Cross-module flow

How the metaclass layer is consulted, end to end:

1. **Type declaration.** `tensor` / `scalar` are declared `class tensor(metaclass=tensor_type)` in `tensor.py` ([6.2.1](tile-tensor-model.md)) — they are instances of `tensor_type` / `scalar_type`.
2. **Parametrization.** `tensor[bfloat16, (128,512), 'psum|sbuf']` → `tensor_type.__getitem__` (185) → `extract_items` → `_create_modified_type` → a fresh `modified_tensor_type` class carrying `tensor_dtype`/`tensor_shape`/`tensor_addrspace`/`tensor_ir_class`/`modifiers`.
3. **Structural check.** `isinstance(traced_value, tensor[...])` → `modified_tensor_type.__instancecheck__` (279): nominal dtype+shape, covariant address-space via `Allocation.is_superclass` (118). This is what the front-end tracer ([6.3.1](type-system.md)) and the `nisa`/`nl` validators use to type-check traced tiles.
4. **Instantiation.** `tensor[...](shape=..., dtype=...)` → `nki_type.__call__` (198): bind to `inspect.Signature`, `create_instance`.
5. **dtype validation.** `tensor_type.extract_items` validates the dtype operand via `nki_dtype.isinstance` (69) = membership in the group's `values` set, plus `_is_np_dtype`.
6. **Placement.** `Allocation` / `AutoAllocation` carry the memory-class; `ir_tensor_class_to_addr_space` (151) maps the IR tensor class to its address-space tag for codegen.

---

## 9. Grounding summary

**CONFIRMED** (symbol + decompiled body + lineno):
- `nki_type.__instancecheck__` (@176/`0x30c50`), `__getitem__` (@185/`0x2e160`), `__call__` (@198/`0x38a30`).
- `modified_tensor_type.__instancecheck__` (@279/`0x32520`) field set {tensor_dtype, tensor_shape, tensor_addrspace, tensor_ir_class, is_superclass}.
- `is_has_mutable_modifier` (@270/`0x23d90`) → `Modifiers.Mutable in cls.modifiers`.
- `Allocation.is_superclass` (@118/`0x2b700`) → `_tensor_class_type` minus `excludes`.
- `nki_dtype.isinstance` (@69/`0x27240`) → membership in `cls.values`.
- `ir_tensor_class_to_addr_space` (@151/`0x26b30`) → `_as_translations.get`.
- `StaticConstProperty.__getattribute__`/`__setattr__` (@402/@409), the const-assign error string and docstring.
- All 9 metaclass/class names, `Modifiers` member strings `{Mutable, Immutable, ParDim}`, addr-space tags `{psum|sbuf, private_hbm, shared_hbm, tiled_hbm}`, the four IR tensor classes, all docstrings.

**INFERRED / SPECULATIVE:** `Modifiers` being an `enum.Enum` and its base/definition site (not in this module); the exact per-IR-class pairing inside `_as_translations`; the precise semantics of `Mutable`/`Immutable`/`ParDim`; `AutoAllocation.auto_allocate`'s placement algorithm (not traced here — see 6.5.15, alloc decorators).
