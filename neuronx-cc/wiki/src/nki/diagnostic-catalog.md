# NKI Diagnostic Catalog: the `err_`/`check_`/`assert_` Funnel

> *All addresses on this page apply to `neuronxcc/nki/compiler/backends/neuron/sema.cpython-310-x86_64-linux-gnu.so` from neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel, md5 `755c15de65e6cc00083fab0e5838e4a0`, BuildID `688cef0a3cfa701d6f5799767be408075bec1498`). The module is **not stripped** and ships `debug_info`; all symbol names below are taken verbatim from its symbol table. cp311/cp312 differ only in Python-ABI tags.*

## Abstract

`sema` is the NKI (Neuron Kernel Interface) front-end **semantic-analysis** layer. It runs during kernel *tracing* — when a `@nki.jit` / `@nki.trace` body executes symbolically to build the IR — and validates every `nl.*` / `nisa.*` call's operands (shape, dtype, address space, engine, op-legality, architecture limits) *before* the kernel is lowered to `penguin.ir`. When a check fails, `sema` raises a user-facing exception whose message and documentation link are exactly what the kernel author sees in their traceback. This page is the complete catalog of that diagnostic surface.

The surface is a **three-tier funnel**, mirroring how a SelectionDAG legalizer separates a per-operand predicate from the diagnostic it emits. The bottom tier — the **`err_*` diagnostic builders** — are the leaves: each constructs one formatted message, appends the canonical docs URL via the `sema_err_url` decorator, and the caller raises it. Above them, the **`assert_*` engine** evaluates a single boolean predicate and, on failure, looks up an `err_*` global and calls it (or builds an inline message and raises through the shared `nki_assert` primitive). At the top, the **`check_*` validators** — bound to the `NKIFunc` class — validate a whole API parameter set and dispatch to `assert_*`/`err_*`. The reader who has met the compiler-driver error catalog in [3.20 frontend/diagnostic-error-catalog](../frontend/diagnostic-error-catalog.md) should read this as the layer *below* it: `sema` raises; the driver catches and surfaces.

This is a **catalog / reference page**, so it omits a reimplementation contract and instead front-loads the funnel mechanism (one annotated example), then gives the full `err_*` table grouped by concern, then the `assert_*` and `check_*` layers and the `sema_err_url` mechanism. Every message template is re-grounded against the shipped binary's rodata; the docstrings (`__doc__`) reproduced here are the verbatim source of the rendered `nki.errors.html` page.

| | |
|---|---|
| **Module** | `neuronxcc.nki.compiler.backends.neuron.sema` (Cython extension) |
| **Source module** | `sema.py` (per Cython `AddTraceback` line tags) |
| **`err_*` builders** | **70** module-level (`__pyx_mdef … err_*`); **77** distinct `err_*` rodata names (see *Counting the leaves*) |
| **`assert_*` helpers** | 24 (the assert engine) |
| **`check_*` validators** | `NKIFunc` methods; ~32 named, 63 `check_*` symbols total |
| **`warn_*`** | 1 — `warn_block_dimension_is_deprecated` (the lone non-fatal diagnostic) |
| **Raise primitive** | `nki_assert` @ `0xbb9a0` (`sema.py:1233`) |
| **Docs decorator** | `sema_err_url` @ `0x9aed0`; `.wrapper` @ `0xf9180` |
| **Docs URL appended** | `https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/api/nki.errors.html` |
| **Default exception** | `NKISyntaxError` (`__init__` @ `0xf6c20`, defined in this module) |

### Counting the leaves

Three different numbers circulate for "how many `err_*` are there", and all three are defensible because they count different things:

| Count | What it measures | How to reproduce |
|---|---|---|
| **70** | module-level `err_*` builders — the functions actually wrapped by `sema_err_url` | `nm` over `__pyx_mdef_…_<idx>err_*` |
| **77** | distinct `err_*` *names*, adding seven class-method and Cython-collision-suffixed forms (e.g. `err_param_shape_incompatible_wit_2`) plus the format helper `err_tensor_access_out_of_bound_1range_string` | `strings` over rodata, deduplicated |
| **78** | the 77 names with the `_1range_string` helper counted as a leaf in its own right | as above, plus one |

This page tabulates the full named surface and labels the format helper explicitly, so either reconciliation works. `err_msg` also appears in rodata, but it is a *field* name rather than a builder and is excluded from every count.

---

## The Three-Tier Funnel

### Purpose

NKI separates *what is illegal* from *how the illegality is reported*, so that one diagnostic builder serves many predicates and one predicate can be reused across many APIs. The `check_*` layer knows the API contract (a `matmul` needs compatible contraction dims); the `assert_*` layer knows the primitive predicate (`par_dim <= max_p`); the `err_*` layer knows the prose. A reimplementer who collapses these into per-API inline `raise`s will produce a working but unmaintainable surface — the NKI design is deliberately three-tier, and the `nki.errors.html` docs page is generated from the `err_*` docstrings, so the split is also the docs-generation boundary.

### Pipeline

```text
NKI API call (nl.* / nisa.*) during tracing
           │
           ▼
NKIFunc.<api>  →  check_<concern>(params)        ── tier C: VALIDATORS  (NKIFunc methods)
           │       validates a whole parameter set
           ▼
assert_<predicate>(cond, …, api_name=…)          ── tier B: ASSERT ENGINE  (24 helpers)
           │       if not cond:  f = <err_* global>;  return f(…)
           ▼
err_<diagnostic>(<interpolated fields>)          ── tier A: DIAGNOSTIC LEAVES  (70 builders)
           │       build message  →  sema_err_url.wrapper appends docs URL
           ▼
raise  NKISyntaxError / ValueError / TypeError / IndexError / RuntimeError
```

### Algorithm — the assert→err hop

The canonical funnel is `assert_num_partition` (`sema.py:2877`, wrapper `0x96c80`) dispatching to `err_num_partition_exceed_arch_limit` (`sema.py:2420`, `0x99e90`). Both symbols and the docs-rendered message are confirmed in the binary.

```c
function assert_num_partition(par_dim, shapes, max_p, api_name):     // 0x96c80
    cond = PyObject_RichCompare(par_dim, max_p, Py_LE);              // par_dim <= max_p (128)
    if (PyObject_IsTrue(cond))                                       // predicate holds
        return None;
    f = GetModuleGlobalName("err_num_partition_exceed_arch_limit");  // cached global slot, 0x99e90
    return f(shapes, max_p, api_name);                              // tp_call → builds + raises
```

```c
function err_num_partition_exceed_arch_limit(shapes, max_p, api_name):   // 0x99e90
    // __doc__ (rodata): "Number of partitions exceeds architecture limitation … of 128"
    msg = "number of partitions "        // frag 0x10ac70
        + str(par_dim)
        + " exceed architecture limitation of "   // frag 0x106ae0
        + str(max_p);
    exc = ValueError(msg);                // ValueError recovered as a literal in-wrapper global
    return sema_err_url_wrapper(exc);     // appends docs URL, attaches long __doc__
```

The verbatim docstring example confirms the chain end-to-end (rodata): `x = nl.zeros(shape=[256, 1024], dtype=np.float32, buffer=nl.sbuf) # Error: number of partitions 256 exceed architecture limitation of 128.`

> **NOTE —** the `err_*` global is reached through a **Cython-cached module-global slot**, not a direct call relocation. That is why many catalog rows below read *CERTAIN (trigger MEDIUM)*: the builder and its message template are read straight out of the binary, but the predicate that funnels into a given `err_*` is sometimes resolvable only by name and semantics, not by a literal call-site instruction. Fan-in is also many-to-one — `assert_num_partition`, `assert_par_dim_sbuf`, and `assert_par_dim_psum` all funnel to the *same* `err_num_partition_exceed_arch_limit` leaf.

### The `nki_assert` raise primitive

Asserts that do **not** route to a named `err_*` build their own inline message string and raise through one shared primitive:

```c
function nki_assert(exception, condition, msg):       // 0xbb9a0  ·  sema.py:1233
    if (not PyObject_IsTrue(condition)):
        raise exception(msg);                         // exception is a passed-in class object
    return None;
```

`nki_assert` is the W04 "assert engine" core: every `assert_*` helper either funnels to an `err_*` builder *or* calls `nki_assert(<ExcClass>, cond, <inline msg>)` directly. The exception class is an argument, so the same primitive raises `ValueError`, `TypeError`, or `NKISyntaxError` depending on caller intent.

### The `sema_err_url` decorator

Every module-level `err_*` builder is wrapped by `sema_err_url` (`0x9aed0`; closure body `sema_err_url.<locals>.wrapper` @ `0xf9180`). The wrapper takes the builder's raw message and **appends the canonical documentation link**:

```c
function sema_err_url_wrapper(exc):                   // 0xf9180  (closure over wrapped err_*)
    long_doc  = wrapped.__doc__;                      // the RST docstring (→ nki.errors.html)
    short_msg = exc.args[0];                          // the runtime f-string
    url       = "https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/api/nki.errors.html";
    exc.args  = (short_msg + "\n" + url,);            // user sees msg + link
    return exc;
```

Both halves are confirmed: the URL string is a single rodata literal, and the `sema_err_url` / `sema_err_url.<locals>.wrapper` symbols are present. The long RST docstrings reproduced in the catalog below are exactly the per-error sections that `nki.errors.html` renders; the short f-strings are what lands in the raised exception's `args`.

> **QUIRK —** the decorator means there is no such thing as a "bare" NKI diagnostic. Every leaf the user sees carries the same trailing URL, and the docs site is *not* hand-written — it is the `__doc__` corpus of these 70 builders. A reimplementer who wants byte-identical diagnostics must reproduce both the short message *and* the docstring, because the docstring is published.

### Exception types

`sema` raises six exception classes plus one warning. The default is `NKISyntaxError` (defined in this module, `__init__` @ `0xf6c20`); where a concrete builtin was recovered as a literal in-wrapper global it is shown explicitly in the catalog. All are confirmed as rodata literals.

| Exception | Used for | Example builders |
|---|---|---|
| `NKISyntaxError` | NKI-specific compile-time syntax/semantic error (default) | most `err_*` (tagged `NKISyntaxError*`) |
| `ValueError` | numeric / shape / arch-limit / mask violations | `err_num_partition_*`, `err_param_shape_*`, `err_unsupported_expression_in_mask` |
| `TypeError` | dtype / operand-type violations | `err_supported_operand_dtype`, `err_unexpected_type_of_operand`, `err_unsupported_args` |
| `IndexError` | out-of-bounds axis / index | `err_tensor_access_out_of_bound`, `err_program_id_axis_out_of_bounds`, `err_indices_shape_mismatch` |
| `RuntimeError` | engine / arch / context dispatch failures | `err_nki_api_outside_of_nki_kernel`, `err_stack_overflow_sbuf`, `err_shared_hbm_must_in_kernel_level` |
| `NotImplementedError` | "not supported" feature gates | rodata literal `0x10b150` |
| `DeprecationWarning` | the single `warn_*` | `warn_block_dimension_is_deprecated` |

Concrete builtins were recovered as in-wrapper literals for ~23 of the leaves (`ValueError ×12`, `TypeError ×8`, `RuntimeError ×4`, `IndexError ×3` — overlapping); the rest construct via a Cython-cached class slot and default to `NKISyntaxError`, tagged `NKISyntaxError*` below.

---

## `err_*` Catalog — Group A: Architecture Limits

Partition counts, dimension ranks, dimension sizes, SBUF/PSUM capacity. The hardware invariant behind this whole group is the 128-partition limit and the fixed SBUF/PSUM budgets — these are the legalization gates that map directly onto the [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md).

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_num_partition_exceed_arch_limit` `0x99e90 · 2420 · ValueError` | `number of partitions <par_dim> exceed architecture limitation of <max_p>` (frags `0x10ac70`+`0x106ae0`) | `par_dim > 128` | tile creation (`nl.zeros`/`ndarray`), `nl.transpose`, `nisa.*` | CERTAIN |
| `err_num_partition_mismatch` `0x807f0 · 2398 · ValueError` | `number of partitions … mismatch in parameters (<shapes>)` (frag `0x106500`) | partition dims differ across operands | `nisa.tensor_tensor` & multi-operand isa | CERTAIN |
| `err_size_of_dimension_exceed_arch_limit` `0x77500 · 2366 · ValueError` | `size of dimension <dim> in '<name><shape>' of '<api>' exceed architecture limitation of <max_size>` (frag `0x109a00`) | a free/contract dim > arch cap | `nl.transpose`, `nl.matmul`/`nisa.nc_matmul` | CERTAIN |
| `err_exceed_max_supported_dimension` `0xbf3a0 · 2343 · ValueError` | `'<name><shape>' … exceed max supported number of dimension <max_rank>` (frag `0x1099c0`) | `rank(param) > max_rank` | `nl.transpose`, rank-restricted isa | CERTAIN |
| `err_stack_overflow_sbuf` `0x813a0 · 1763 · RuntimeError` | `stack overflow: required sbuf size <sbuf_size> exceeds available sbuf size <target_sbuf_size>` (frag `0x109f20`) | cumulative SBUF alloc > device budget | tile alloc on `nl.sbuf` | CERTAIN (trigger MEDIUM) |
| `err_stack_overflow_psum` `0x81f60 · 1756 · RuntimeError` | `stack overflow: required psum banks <n> exceeds available psum banks <target>` (frag `0x109da0`) | PSUM bank demand > 8 | PSUM alloc / matmul accumulation | CERTAIN (trigger MEDIUM) |
| `err_valid_size` `0x63bd0 · 2867 · NKISyntaxError*` | `<name> size must be in [ … ]` / ` size must be ` (frags `0x10bac0`+`0x10c178`) | size operand outside valid set | size-constrained isa (raised inline via `nki_assert`) | HIGH |

---

## `err_*` Catalog — Group B: Shape / Annotation / Broadcast / Matmul

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_tile_shape_mismatch` `0x4ba30 · 1330 · NKISyntaxError*` | `tile shape mismatch, expected '<expected_shape>' …` (frag `0x109d60`) | actual tile shape ≠ expected | fixed-shape operand contracts | CERTAIN |
| `err_annotation_shape_mismatch` `0x4cd30 · 2441 · TypeError` | `shape of \`<name><shape>\` does not match the expected shape of <annotation_shape>` (frags `0x10cd68`+`0x109260`) | `value.shape != annotation` | typed assignment `data: nt.tensor[…] = …` | CERTAIN |
| `err_param_shape_mismatch` `0x90430 · 360 · ValueError` | `Parameter shapes (<shapes>) … has mismatched shapes` (frags `0x10ba50`+`0x10aa90`) | operand shapes incompatible (no-broadcast) | elementwise isa requiring identical shapes | CERTAIN |
| `err_param_shape_incompatible_with_numpy` `0x8f6d0 · 354 · ValueError` | `Parameter shapes (<shapes>) … could not be broadcast together with shapes` (frag `0x109320`) | shapes fail NumPy broadcast | broadcast-following APIs (`nl.add`/`mul`) | CERTAIN |
| `err_param_shape_incompatible_with_matmul` `0xee450 · 343 · ValueError` | matmul contraction-shape incompatibility (`<transpose_x>`, `<shapes>`) | lhs/rhs contraction dims incompatible | `nl.matmul`, `nisa.nc_matmul` | CERTAIN |
| `err_store_dst_shape_smaller_than_other_shape` `0xbe290 · 1958 · ValueError` | `Illegal assignment destination shape in '<expr>': shape … '<dst_name>' is smaller than other parameter shape <shapes>` (frags `0x1087c0`+`0x108f80`) | dst broadcast-smaller than src | tile assign `dst[…]=src`, `nl.store` | CERTAIN |
| `err_indices_shape_mismatch` `0x705c0 · 1985 · IndexError` | `shape mismatch: indices indexing tensor …` (frag `0x108c60`) | advanced-index tensor shape mismatch | indirect/advanced indexing | CERTAIN (trigger MEDIUM) |
| `err_leading_dimension_of_tensor_must_be_partition` `0x7f070 · 2519 · NKISyntaxError*` | `The leading dimension of SBUF/PSUM tensors must be the partition dimension. The tensor has shape <tensor_shape>` (frag `0x107c00`) | leading dim of SBUF/PSUM tile ≠ partition dim | tile creation/use where dim 0 ≠ par_dim | CERTAIN |

> **NOTE —** `err_param_shape_incompatible_with_matmul` is reached from `check_matmul_high_level_shape` via the Cython-collision slot `err_param_shape_incompatible_wit_2` (the `_2` suffix disambiguates it from `..._with_numpy`, which truncates to `..._wit`). Both truncations are visible as cached-slot names; the builders are distinct.

---

## `err_*` Catalog — Group C: Dtype / Operand-Type

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_supported_operand_dtype` `0xce320 · 332 · TypeError` | `'<name>' …, expected one of the following dtypes: <expected_dtypes>` (frag `0x1090c0`) | operand dtype ∉ supported set | dtype-restricted isa | CERTAIN |
| `err_unsupported_dtype_value` `0xd3ec0 · 327 · ValueError` | `Unsupported dtype '<dtype_value>' …, expected one of the following dtypes: <expected_dtypes>` (frag `0x10b130`) | `dtype=` value not a legal NKI dtype | any API taking `dtype=` | CERTAIN |
| `err_unsupported_args` `0x628b0 · 313 · TypeError` | `'<api>' with unsupported arguments on nki tensor: <e>` (frag `0x108ac0`) | tensor dunder/op with bad arg signature | overloaded tensor operators | CERTAIN |
| `err_activation_bias_invalid_type` `0x7fc30 · 1573 · TypeError` | `'bias' param of '<instr_name>' must be a vector of type float32, float16, or bfloat16, got '<dtype>'` (frags `0x10c0e0`+`0x108ea0`) | `bias.dtype ∉ {fp32,fp16,bf16}` | `nisa.activation`/`activation_reduce` (`bias=`) | CERTAIN (trigger MEDIUM) |
| `err_activation_scale_invalid_type` `0x84330 · 1558 · TypeError` | `'scale' param of '<instr_name>' must be a scalar or vector of type float32, got '<dtype>'` (frags `0x10bcf0`+`0x108ee0`) | `scale.dtype != float32` | `nisa.activation`/`activation_reduce` (`scale=`) | CERTAIN (trigger MEDIUM) |
| `err_src_dst_same_dtype` `0x50c70 · 2861 · NKISyntaxError*` | `<api> src and dst must have same data type but got <src> / <dst>` (frag `0x108c20`) | `src.dtype != dst.dtype` on copy/transpose | `nisa.dma_copy`/`tensor_copy`/transpose | CERTAIN |
| `err_tile_index_add_non_integer_scalar` `0xa73e0 · 1752 · TypeError` | `tile_index can only add with integer scalar, got scalar with dtype <dtype>` (frag `0x1061e0`) | `tile_index + non-integer scalar` | index arithmetic on `tile_index`/mgrid | CERTAIN (trigger MEDIUM) |
| `err_unexpected_type_of_operand` `0x8a800 · 1339 · TypeError` | operand type ∉ `<expected_types>` (`'… but got 'np.…'` family, frag `0x10c940`) | operand python/IR type ∉ expected | any class-restricted operand | CERTAIN |
| `err_operand_cannot_be_none` `0x941e0 · 323 · ValueError` | `'<name>' cannot be None.` (frag `0x10be30`) | required operand is `None` | any API with a mandatory operand | CERTAIN |
| `err_incorrect_target_type` `0xd5170 · 1481 · NKISyntaxError*` | `<target_type> … is not supported in '<name>'` (frag `0x10af70`) | wrong NeuronCore target for instruction | arch-gated isa (from the two arch-support errs) | CERTAIN |

---

## `err_*` Catalog — Group D: Address Space / Memory

HBM / SBUF / PSUM placement. NKI enforces per-API memory-residency requirements; these leaves are the placement legalizer.

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_unsupported_memory` `0xda290 · 1810 · TypeError` | `Expected operand '<name>' of '<api>' to be in address space '<expected_addr_space>', but got a <tile> instead.` (frags `0x10ba70`+`0x10a830`+`0x10cc68`) | operand buffer ∉ required address spaces | `nl.load` (src=hbm), compute (psum\|sbuf) | CERTAIN |
| `err_shared_hbm_must_in_kernel_level` `0xeb420 · 2080 · RuntimeError` | `shared_hbm buffer can only be created top level kernel scope <scope>` | `nl.ndarray(buffer=nl.shared_hbm)` outside top-level scope | `nl.ndarray`/`nl.full` with `buffer=nl.shared_hbm` | CERTAIN |
| `err_hbm_tensor_with_init_value_not_supported` `0x44850 · 1499 · NKISyntaxError*` | `Creating HBM tensor with init value is not supported.` (frag `0x108940`) | init/fill value on an HBM tensor | `nl.full`/`nl.zeros(…, buffer=hbm)` | CERTAIN |
| `err_tensor_creation_on_scratchpad_with_init_value_not_supported` `0x44a20 · 1485 · NKISyntaxError*` | `Creating SBUF/PSUM tensor with init value is not supported in allocated kernels.` (frag `0x1088e0`) | init value on SBUF/PSUM tile in *allocated* kernel | `nl.full(buffer=ncc.sbuf…)` in allocated kernels | CERTAIN |
| `err_bias_tensor_must_be_specified_in_allocation` `0x44680 · 1521 · NKISyntaxError*` | `Bias tensor for activation op must be specified in allocated kernel!` (frag `0x108a60`) | `nisa.activation` without explicit bias in allocated kernel | `nisa.activation` (allocated) | CERTAIN |
| `err_transpose_on_tensor_engine_not_allowed_in_allocated_kernel` `0x444b0 · 1590 · NKISyntaxError*` | feature-gate string (no interpolation) | `nc_transpose` on TensorEngine / matmul w/o `transpose_x` in allocated kernel | `nisa.nc_transpose`, `nl.matmul` (allocated) | CERTAIN |

---

## `err_*` Catalog — Group E: Engine / Op-Legality / Arch-Support

Instruction selection. These leaves gate which op runs on which engine, which dtypes a given instruction supports per target, and the minimum NeuronCore generation an instruction requires.

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_instruction_unsupported_op` `0x5d880 · 1399 · NKISyntaxError*` | `<op_name> is not supported in '<inst_name>'` (frags `0x10af70`+`0x10b910`) | op ∉ instruction's legal op set | `nisa.tensor_reduce`/`tensor_tensor`/`tensor_scalar` (`op=`) | CERTAIN |
| `err_instruction_engine_unsupported_op_comb` `0xba320 · 1409 · NKISyntaxError*` | unsupported `(op0, op1)` pair on engine | op-pair illegal on chosen engine | fused two-op isa (e.g. `tensor_tensor_scan`) | CERTAIN (trigger MEDIUM) |
| `err_instruction_supported_dtypes` `0x98ac0 · 1420 · NKISyntaxError*` | `<inst>.<param> on <target> dtype <dtype>, supported: <supported_dtypes>` | param dtype ∉ per-target positive-list | dtype-gated isa (positive form) | CERTAIN (trigger MEDIUM) |
| `err_instruction_unsupported_dtypes` `0x975f0 · 1430 · NKISyntaxError*` | dtype `<dtype>` in *unsupported* set for inst/param/target | param dtype ∈ per-target blacklist | dtype-gated isa (negative form) | CERTAIN (trigger MEDIUM) |
| `err_tensor_int32_add_multiply_supported_engine` `0xb2490 · 2898 · NKISyntaxError*` | int32 add/multiply only on `<multiple_engines>`; op `<op>` dtype `<dtype>` engine `<engine>` | int32 tensor add/mul on engine lacking int32 ALU (→ `err_instruction_unsupported_op`) | `nisa.tensor_tensor`/`tensor_scalar` (int32 add\|mult) | CERTAIN |
| `err_par_reduce_unsupported_op` `0xeace0 · 1454 · NKISyntaxError*` | `par_reduce does not support operator=<op_name>` (frag `0x1064c0`) | unsupported op in partition reduction | `nisa.*_par_reduce` | CERTAIN (trigger MEDIUM) |
| `err_reduce_unsupported_negate` `0x76650 · 1458 · NKISyntaxError*` | `negate option can only be used with arithmetic ops, unsupported op=<op_name>` (frag `0x1067a0`) | `negate=True` on non-arithmetic reduce | `nisa.tensor_reduce(negate=True)` | CERTAIN (trigger MEDIUM) |
| `err_reduce_bitvec_op_invalid_dtype` `0x85c20 · 1440 · NKISyntaxError*` | bitvec reduce requires integer dtype, got `<dtype>` | bitwise reduce on float dtype | `nisa.tensor_reduce(op=bitwise_*)` | CERTAIN (trigger MEDIUM) |
| `err_par_reduce_bitvec_op_invalid_dtype` `0x84ee0 · 1447 · NKISyntaxError*` | partition bitvec reduce requires integer dtype, got `<dtype>` | bitwise partition-reduce on float dtype | nisa partition bitwise reductions | CERTAIN (trigger MEDIUM) |
| `err_bitvec_operand_must_be_integer` `0xbc170 · 1462 · NKISyntaxError*` | bitvec op operand `<operand_name>` dtype `<dtype>` must be integer (frag `0x10bb10`) | bitwise op on non-integer operand | nisa bitwise tensor ops (and/or/xor/shift) | CERTAIN (trigger MEDIUM) |
| `err_mask_not_equal_not_supported` `0x44bf0 · 1468 · NKISyntaxError*` | `'not equal' mask is not supported` (frag `0x108e20`) | `!=` used to build a mask predicate | `mask=` with not-equal comparison | CERTAIN |
| `err_logic_or_not_supported` `0x94f50 · 339 · ValueError` | `'logical or' is not supported for the \`<name>\`` (frag `0x108f20`) | python `or` on a tensor/mask | tensor expressions using `or` (use `\|`) | CERTAIN |
| `err_exact_arch_support_for_inst` `0xbd240 · 2910 · NKISyntaxError*` | inst `<inst_name>' is available on <available_targets>` (frag `0x10bb40`; via `ncversion2str`) | instruction used on a target where it does not exist (→ `err_incorrect_target_type`) | arch-exclusive isa | CERTAIN |
| `err_min_arch_support_for_inst` `0xe3620 · 2919 · NKISyntaxError*` | inst `<inst_name>` requires min target `<min_target>` (via `get_nc_version`/`ncversion2str`) | instruction below its minimum NeuronCore gen (→ `err_incorrect_target_type`) | gen-gated isa | CERTAIN |
| `err_min_arch_support_for_hwdge` `0x69130 · 2927 · NKISyntaxError*` | HW DGE mode `<dge_mode>` for inst `<inst_name>` requires min target `<min_target>` | HW descriptor-gen-engine mode below min arch (→ `err_min_arch_support_for_inst`) | DMA/DGE-accelerated isa on too-old a target | CERTAIN |
| `err_multi_cores_spmd_not_supported` `0x7d0a0 · 2064 · RuntimeError` | `SPMD grid with multi neuron cores is not supported on <target>` (frag `0x107c80`) | multi-core SPMD grid on a target lacking it | `kernel[grid]` launch on unsupported target | CERTAIN (trigger MEDIUM) |

---

## `err_*` Catalog — Group F: Indexing / Indirect / Out-of-Bound / Atomic RMW

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_tensor_access_out_of_bound` `0x5ae50 · 1840 · IndexError` | `Out-of-bound access for tensor \`<tensor_name>\` on dimension … index range <oob_range> exceed dimension size of <shape>` (frags `0x10c550`+`0x10aab0`) | computed index range exceeds a tensor dim | `nl.load`/`nl.store` (fix via `mask=`) | CERTAIN (trigger MEDIUM) |
| `err_tensor_access_out_of_bound_1range_string` `0xa1500 · 1885 · NKISyntaxError*` | format **helper** — formats `<range_tuple>` for the above | (helper, not a top-level leaf) | — | CERTAIN |
| `err_tensor_index_not_supported` `0x9cda0 · 2124 · NKISyntaxError*` | tensor `<tensor_name>` indexed with unsupported index `<index>` (type `<type_name>`) | unsupported index object/type | tile subscript with illegal index kind | CERTAIN (trigger MEDIUM) |
| `err_cannot_assign_to_index` `0x46510 · 2008 · TypeError` | `'index' tensor does not support item assignment <index_or_mask>` | item assignment into index/mask tensor | assignment to `nl.mgrid`/arange-derived index | CERTAIN |
| `err_unsupported_mixing_basic_advanced_tensor_indexing` `0x43d70 · 1934 · NKISyntaxError*` | `Mixing basic tensor indexing and advanced tensor indexing is not supported.` (frag `0x108660`) | subscript mixes slice and index-tensor across axes | any tile subscript combining basic+advanced | CERTAIN |
| `err_indirect_indices_free_dim` `0x439d0 · 2133 · NKISyntaxError*` | `Indirect indexing on free dimension not supported, must be partition dimension or block dimension.` (frag `0x108740`) | indirect index applied to a free dimension | `nl.load`/`nl.store` with index tensor on free axis | CERTAIN |
| `err_indirect_indices_sbuf` `0x43800 · 2158 · NKISyntaxError*` | `Indirect indices tile must be on SBUF.` (frag `0x108700`) | index tile not in SBUF | indirect-indexed `nl.load`/`nl.store` | CERTAIN |
| `err_indirect_indices_are_not_supported_with_nki_api` `0xaafb0 · 2459 · NKISyntaxError*` | `cannot use indirect indexing access with the current NKI API for <indirect_operands>` (frag `0x107820`) | indirect operand passed to an API that forbids it | `nl.load_transpose2d`, `nisa.dma_transpose` | CERTAIN |
| `err_copy_dynamic_indirect_indices_not_natively_supported` `0xebba0 · 2484 · NKISyntaxError*` | `cannot copy a tensor with indirect memory reference access to \`<lhs>\`` (frag `0x107880`) | copy with runtime-dynamic indirect source | use `nisa.tensor_copy_dynamic_src` | CERTAIN |
| `err_atomic_rmw_add_only` `0x46b10 · 2163 · NKISyntaxError*` | `atomic_rmw \`op\` param only supports 'add' operation currently. An unsupported op=<op>` (frag `0x107960`) | `atomic_rmw` with `op != add` | `nl`/`nisa` `atomic_rmw` | CERTAIN |
| `err_atomic_rmw_dynamic_index` `0x43630 · 2170 · NKISyntaxError*` | `atomic_rmw \`dst\` only supports indirect dynamic indexing currently. Use a tile to index.` (frag `0x1079c0`) | `atomic_rmw` dst without indirect dynamic index | `atomic_rmw` dst addressing | CERTAIN |
| `err_1d_arange_not_supported` `0xa2240 · 1716 · NKISyntaxError*` | `<tensor_name> with 1d arange is not supported.` (frag `0x108b00`) | tile subscripted with a bare 1-D `nl.arange` | tile indexing (use `[:,None]`) | CERTAIN |
| `err_unexpected_output_dependencies` `0xec2e0 · 1895 · NKISyntaxError*` | `Unexpected output dependencies <missing_indices>` | same memory written across parallel iterations | writes inside `nl.affine_range` / spmd grid | CERTAIN |

---

## `err_*` Catalog — Group G: Control-Flow / Tracing / SPMD / Kernel Structure

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_dynamic_control_flow_not_supported` `0x442e0 · 1620 · NKISyntaxError*` | `dynamic control-flow depending on tensor value is not supported.` (frag `0x107640`) | `if cnd:` where `cnd` is a runtime tensor value | python if/while on a loaded tensor | CERTAIN |
| `err_control_flow_condition_depending_on_arange` `0x43f40 · 1688 · NKISyntaxError*` | `Control-flow depending on \`nl.arange\` or \`nl.mgrid\` is not supported.` (frag `0x108980`) | branch condition derives from arange/mgrid | if-conditions over arange/mgrid (use `mask=`) | CERTAIN |
| `err_unsupported_expression_in_mask` `0x44110 · 1635 · ValueError` | `NKI mask expressions must be affine expressions of static loop indices. Dynamic values, tensor elements, or non-affine operations are not supported in mask predicates.` (frag `0x107d80`) | `mask=` contains non-affine/runtime term | load/store/compute `mask=` predicates | CERTAIN |
| `err_while_loop_requires_unconditional_entry` `0x43290 · 2584 · ValueError` | `Traditional while loops are not supported in NKI. Use the do-while pattern instead: start with 'cond = scalar(True)' …` (frag `0x1095a0`) | `while cond:` evaluated before first entry | python while loops in a kernel | CERTAIN |
| `err_ambiguous_tensor_truth_value` `0x43460 · 2533 · ValueError` | f-string from the DOC text; no interpolation (NumPy-style ambiguity) | `bool()` of a multi-element tensor | logical operators / truthiness on tensors | CERTAIN |
| `err_nki_api_outside_of_nki_kernel` `0x43ba0 · 2027 · RuntimeError` | `calling NKI API outside of NKI kernels is not supported.` (frag `0x1078c0`) | `nl.*`/`nisa.*` with no active trace context | any NKI API outside `@nki.jit`/`@nki.trace` | CERTAIN |
| `err_nested_kernel_with_spmd_grid` `0xb4270 · 2038 · NKISyntaxError*` | `<func_name>[<grid>]) inside another kernel is not supported.` (frag `0x109040`) | invoking `kernelN[grid](…)` inside another traced kernel | nested SPMD-grid kernel calls | CERTAIN |
| `err_program_id_axis_out_of_bounds` `0x83730 · 1323 · IndexError` | `axis in \`program_id\` is out-of-bound(s) of the spmd launch grid (axis=<axis> … rank <rank>)` (frag `0x107900`) | `nl.program_id(axis)` with `axis >= grid rank` | `nl.program_id(axis)` | CERTAIN |
| `err_local_variable_used_out_of_scope` `0xb4f00 · 1257 · NKISyntaxError*` | `Local variable '<name>' is referenced outside of its parent scope <parent_scope>` | tensor defined in an if/for block used after it | cross-scope tensor reuse (hoist the `nl.ndarray`) | CERTAIN |
| `err_tensor_output_not_written_to` `0xeca20 · 2187 · ValueError` | `<tensor_name> … never written to` | output tensor with no store reaching it | kernel output contract / `nl.store` coverage | CERTAIN |
| `err_cannot_update_immutable_parameter` `0x45f10 · 2287 · TypeError` | `Cannot update immutable parameter \`<tensor_name>\`` (frag `0x1089e0`) | `nl.store` into un-annotated (immutable) param | `nl.store(in_tensor, …)` on immutable param | CERTAIN |
| `err_mutable_parameter_not_returned` `0xaa470 · 2242 · NKISyntaxError*` | `<tensor_names> … mutable kernel parameter not returned` | `nt.mutable_tensor` param written but not returned | kernel return list | CERTAIN |
| `err_failed_to_infer_tile_from_local_tensor` `0x64f90 · 1770 · TypeError` | `Failed to infer tile from tensor '…': the first dimension of the tile is not the partition dimension of the tensor.` (frags `0x108860`+`0x108b40`) | compute API given a tensor whose dim 0 ≠ partition dim | any compute API (`nl.add`/`exp`) on a non-tile-shaped tensor | CERTAIN |

---

## `err_*` Catalog — Group H: Activation-Shape / Tiled-Offload / Constant / Hashable

| `err_*` (addr · line · exc) | Message template (rodata) | Trigger / predicate | Raising op | Conf |
|---|---|---|---|---|
| `err_activation_scale_scalar_or_vector` `0x4c6a0 · 1540 · NKISyntaxError*` | `'scale' param of 'activation' can only be a scalar or a vector in partition dimension, scale.shape=<shape>` (frag `0x108ca0`) | scale tensor neither scalar nor partition-dim 1-D vector | `nisa.activation(scale=)` | CERTAIN |
| `err_tiled_offloaded_memcpy_same_shape` `0x4aec0 · 2175 · NKISyntaxError*` | `src and dst must have the same shape, src.shape=<src> dst.shape=<dst>` (frag `0x106300`+`0x10c5b0`) | tiled/offloaded memcpy with mismatched src/dst | tiled offloaded `nisa.dma_copy`/memcpy | CERTAIN (trigger MEDIUM) |
| `err_tiled_offloaded_fma_same_length` `0x95cc0 · 2181 · NKISyntaxError*` | `srcs and scales must have the same length, len(srcs)=<srcs>, len(scales)=<scales>` (frags `0x10bb60`+`0x10c840`) | tiled FMA where `len(srcs) != len(scales)` | tiled offloaded fused multiply-add | CERTAIN (trigger MEDIUM) |
| `err_expected_constant_value` `0x7c4a0 · 1472 · NKISyntaxError*` | expected compile-time constant `<expected>`, got `<value>` | must-be-constant argument got a runtime value | APIs with compile-time-constant operands (shapes/axes/dtypes) | CERTAIN |
| `err_nki_param_not_hashable` `0x82b20 · 2071 · ValueError` | `'<name>' to be hashable, but got type <ty>` (frag `0x109e60`) | hashable-expected kernel param is unhashable | kernel parameter binding / memoization | CERTAIN (trigger MEDIUM) |

---

## `warn_*` — the lone non-fatal diagnostic

| `warn_*` (addr · line · severity) | Message template | Trigger | Conf |
|---|---|---|---|
| `warn_block_dimension_is_deprecated` `0xd5ea0 · 2512 · DeprecationWarning` | `Block dimension is deprecated. The leading dimension of <tensor>…` (frag `0x108a20`); shares DOC `0xfebc0` with `err_leading_dimension_of_tensor_must_be_partition` | a tile with a block dim in front of the partition dim | CERTAIN |

> **NOTE —** `warn_block_dimension_is_deprecated` is the only diagnostic that does *not* abort tracing. It emits through `warnings.warn` (`DeprecationWarning`, rodata `0x10b470`) and the kernel continues — block dim is still accepted but points the author at the migration guide. Its sibling hard error `err_leading_dimension_of_tensor_must_be_partition` fires only once block dim is fully removed for a given op. The two share a docstring, so the docs page renders the warning and the error from one source.

---

## The `assert_*` Engine (24)

The `assert_*` helpers are the W04 layer: each validates one predicate and, on failure, either funnels to a named `err_*` or builds an inline message and raises through `nki_assert` (`0xbb9a0`). Five funnel to named leaves; the rest raise inline.

### Named-err asserts (funnel to an `err_*` leaf)

| `assert_*` (addr · line) | Args | Funnels to |
|---|---|---|
| `assert_tile_has_expected_shape` `0xb1b10 · 1334` | `name, tile, expected_shape, shape` | `err_tile_shape_mismatch` |
| `assert_is_tile` `0x92890 · 1356` | `api_name, x, name, tensor, tile` | `err_failed_to_infer_tile_from_local_tensor` \| `err_unexpected_type_of_operand` |
| `assert_constant_value` `0x9dc10 · 1476` | `value, expected` | `err_expected_constant_value` |
| `assert_tensor_in_valid_addr_spaces` `0xcd3d0 · 2705` | `valid_addr_spaces, x, name, api_name` | `err_unsupported_memory` |
| `assert_num_partition` `0x96c80 · 2877` | `par_dim, shapes, max_p, api_name` | `err_num_partition_exceed_arch_limit` |
| `assert_par_dim_psum` `0x57bd0 · 2710` | `par_dim, target, psum_num_partitions` | `err_num_partition_exceed_arch_limit` |
| `assert_par_dim_sbuf` `0x57250 · 2716` | `par_dim, target, statebuf_num_partitions` | `err_num_partition_exceed_arch_limit` |

### Inline-message asserts (raise via `nki_assert` directly)

These 17 build their own message string and raise through `nki_assert`. Where an inline fragment was recovered from rodata it anchors the row:

| `assert_*` (addr · line) | Args | Inline message fragment (rodata) |
|---|---|---|
| `assert_dma_input_length` `0xb0a00 · 2635` | `srcs` | — |
| `assert_type` `0x5a020 · 2687` | `name, value, type_2` | — |
| `assert_dtype` `0x59070 · 2694` | `name, tile, type_2, …` | — |
| `assert_not_none` `0x58550 · 2701` | `value, name` | — |
| `assert_free_dim_psum` `0x889c0 · 2722` | `target, free_shape, dtype, …` | — |
| `assert_free_dim_sbuf` `0x797a0 · 2732` | `free_shape, dtype, target, …` | — |
| `assert_tile_shapes` `0x565a0 · 2742` | `target, tile_shape, dtype` | — |
| `assert_dtype_psum` `0x55940 · 2749` | `dtype, target, float32, int32` | `PSUM tensor for writes must be either 4-byte (FP32/int32) or BF16` `0x107cc0`; `PSUM tensor for non-transpose writes must be a 4-byte dtype (FP32/int32)` `0x107d20` |
| `assert_local_tensor_shapes` `0xc92a0 · 2760` | `buffer, tile_shape, dtype, …` | ` could not be broadcast together with shapes ` `0x109320` |
| `assert_gen3_or_newer_mm_dtype` `0x54460 · 2775` | `inst, dtype, ctx, target, is_transpose` | `Transpose mode on or after Gen3 requires input and output dtypes to be the same` `0x109540` |
| `assert_elements_per_partition` `0xc6c90 · 2801` | `elements_per_partition, min_size, max_size, name` | ` elements per partition, but must be between ` `0x1091e0` |
| `assert_max_dimensions` `0x53670 · 2817` | `name, shape, max_dims` | — |
| `assert_min_dimensions` `0x52880 · 2828` | `name, shape, min_dims` | — |
| `assert_exact_elements` `0x682e0 · 2839` | `name, elements_2, required` | ` elements per partition, but must have exactly ` `0x105f60` |
| `assert_dtype_in` `0x51a50 · 2853` | `name, dtype, supported_dtypes, …` | — |
| `assert_shape_valid` `0x4fb20 · 2882` | `shape, name` | `, which must be all positive, non-zero values.` `0x109860` |
| `assert_same_shape` `0xe6bb0 · 2888` | `shape1, shape2, arg2, api_name, arg1` | — |

---

## The `check_*` Validators (NKIFunc methods)

The `check_*` validators are `NKIFunc` methods that validate a whole API parameter set and dispatch to `assert_*`/`err_*`. The binary has 63 `check_*` symbols; the named, diagnostic-relevant ones with a recovered dispatch target:

| `check_*` (addr · line) | Dispatches to |
|---|---|
| `check_tensor` `0x61bb0 · 367` | `err_unexpected_type_of_operand` |
| `check_tile` `0x61010 · 377` | `assert_is_tile` |
| `check_dtype` `0xc0510 · 381` | `err_operand_cannot_be_none`, `err_unsupported_dtype_value` |
| `check_tensor_dtype` `0xcf5c0 · 562` | `err_supported_operand_dtype` |
| `check_tensor_addr_space` `0xd2c60 · 596` | `err_unsupported_memory` |
| `check_param_type` `0xcaae0 · 692` | `err_operand_cannot_be_none`, `err_unexpected_type_of_operand` |
| `check_parameter_shapes_np_broadcast` `0x789a0 · 744` | `err_param_shape_incompatible_with_numpy` (slot `…_wit`) |
| `check_parameter_shapes_without_broadcast` `0xabeb0 · 752` | `err_param_shape_mismatch` |
| `check_par_dim_sizes` `0x6f060 · 805` | `err_num_partition_exceed_arch_limit` |
| `check_par_dim_size_match` `0x8c0e0 · 819` | `err_num_partition_mismatch` |
| `check_free_dim_size_sbuf` `0x712e0 · 831` | `assert_free_dim_sbuf` |
| `check_matmul_high_level_shape` `0xf2a30 · 848` | `err_param_shape_incompatible_with_matmul` (slot `…_wit_2`) |
| `check_matmul_f_dim_sizes` `0xe7f90 · 869` | `err_size_of_dimension_exceed_arch_limit` |
| `check_transpose_shape` `0x73e60 · 903` | `err_exceed_max_supported_dimension`, `err_size_of_dimension_exceed_arch_limit` |
| `check_store_shape` `0xa5ac0 · 923` | `err_store_dst_shape_smaller_than_other_shape` |
| `check_tensor_reduce_supported_ops` `0x5ec40 · 1375` | `err_instruction_unsupported_op` |

### Validators with their own inline messages

Several `check_*` raise inline messages with no `err_*` symbol fronting them — they belong to the same diagnostic surface:

```text
check_quantize_mx_shape (1025):
  "quantize_mx src must have the last free dimension size to be multiple of 4."          0x1063c0
  "quantize_mx src must have 32-multiple partition dimension size."                       0x106420
  "quantize_mx must have same access shape for src and dst except the last free dimension." 0x106460
  "quantize_mx dst must access F // 4 elements per partition for src with F elements …"  0x108d20
check_matmul_mx_low_level_shape (0xe1310) / check_matmul_low_level_shape:
  "nc_matmul_mx stationary must have an even number of packed (x4) elements per partition" 0x106800
  "nc_matmul_mx stationary/moving must access 32/64/128 partitions."                       0x106860
  "nc_matmul_mx moving_scale/stationary_scale must access same elements per partition …"  0x1068c0
check_local_gather_shape (946):
  "<…> can only have input data of type uint32/uint16/uint8, unsupported dtype="          0x105fa0
  "<…> can only have input data of type int32/uint32/uint16/uint8, unsupported dtype="    0x1093c0
```

---

## Raising-Op Index

The inverse map: which user-facing NKI API can trip which diagnostics. A kernel author who hits a class of errors can scan here for the full set their API surface can raise.

| NKI API family | Diagnostics it can raise |
|---|---|
| tile creation (`nl.zeros`/`ndarray`/`full`) | `num_partition_exceed_arch_limit`, `hbm_tensor_with_init_value_*`, `tensor_creation_on_scratchpad_with_init_value_*`, `shared_hbm_must_in_kernel_level`, `leading_dimension_of_tensor_must_be_partition`, `stack_overflow_sbuf`, `stack_overflow_psum`, `warn_block_dimension_is_deprecated` |
| `nl.load` / `nl.store` | `unsupported_memory`, `tensor_access_out_of_bound`, `indirect_indices_free_dim`, `indirect_indices_sbuf`, `indices_shape_mismatch`, `cannot_update_immutable_parameter`, `tensor_output_not_written_to`, `store_dst_shape_smaller_than_other_shape`, `unexpected_output_dependencies` |
| `nl.transpose` / `nl.matmul` / `nc_matmul` | `size_of_dimension_exceed_arch_limit`, `exceed_max_supported_dimension`, `param_shape_incompatible_with_matmul`, `transpose_on_tensor_engine_not_allowed_in_allocated_kernel`, `src_dst_same_dtype` |
| `nisa.activation` / `activation_reduce` | `activation_bias_invalid_type`, `activation_scale_invalid_type`, `activation_scale_scalar_or_vector`, `bias_tensor_must_be_specified_in_allocation` |
| `nisa.tensor_tensor` / `tensor_scalar` | `num_partition_mismatch`, `param_shape_mismatch`, `param_shape_incompatible_with_numpy`, `instruction_unsupported_op`, `instruction_engine_unsupported_op_comb`, `instruction_supported_dtypes`, `instruction_unsupported_dtypes`, `tensor_int32_add_multiply_supported_engine`, `supported_operand_dtype`, `unsupported_dtype_value`, `operand_cannot_be_none` |
| `nisa.tensor_reduce` / `par_reduce` | `par_reduce_unsupported_op`, `reduce_unsupported_negate`, `reduce_bitvec_op_invalid_dtype`, `par_reduce_bitvec_op_invalid_dtype`, `bitvec_operand_must_be_integer` |
| `atomic_rmw` | `atomic_rmw_add_only`, `atomic_rmw_dynamic_index` |
| indexing / `mgrid` / `arange` | `1d_arange_not_supported`, `cannot_assign_to_index`, `tensor_index_not_supported`, `unsupported_mixing_basic_advanced_tensor_indexing`, `indirect_indices_are_not_supported_with_nki_api`, `copy_dynamic_indirect_indices_not_natively_supported`, `tile_index_add_non_integer_scalar` |
| control flow (`if`/`while`/`mask`) | `dynamic_control_flow_not_supported`, `control_flow_condition_depending_on_arange`, `unsupported_expression_in_mask`, `while_loop_requires_unconditional_entry`, `ambiguous_tensor_truth_value`, `mask_not_equal_not_supported`, `logic_or_not_supported` |
| kernel structure / SPMD | `nki_api_outside_of_nki_kernel`, `nested_kernel_with_spmd_grid`, `multi_cores_spmd_not_supported`, `program_id_axis_out_of_bounds`, `mutable_parameter_not_returned`, `local_variable_used_out_of_scope`, `nki_param_not_hashable` |
| arch-gated isa | `exact_arch_support_for_inst`, `min_arch_support_for_inst`, `min_arch_support_for_hwdge`, `incorrect_target_type` |
| typed assignment (`nt.tensor[…]`) | `annotation_shape_mismatch`, `tile_shape_mismatch` |
| tiled offload / MX | `tiled_offloaded_memcpy_same_shape`, `tiled_offloaded_fma_same_length` (+ `check_quantize_mx_shape`, `check_matmul_mx_low_level_shape` inline) |
| unsupported op args | `unsupported_args`, `unexpected_type_of_operand`, `failed_to_infer_tile_from_local_tensor`, `expected_constant_value`, `valid_size` |

---

## Evidence Anchors

Every symbol and string cited on this page is reproducible from the wheel with `nm` and `strings`; the chain-level anchors are:

- **The `assert` → `err` hop is a real chain, not a reconstruction.** `assert_num_partition` (`0x96c80`) and `err_num_partition_exceed_arch_limit` (`0x99e90`) are both present as symbols, and the docstring example `# Error: number of partitions 256 exceed architecture limitation of 128.` is a verbatim rodata literal — predicate, builder, and rendered message all resolve in one chain.
- **`sema_err_url` appends exactly one URL.** Both `sema_err_url` and `sema_err_url.<locals>.wrapper` are symbols, and `https://awsdocs-neuron.readthedocs-hosted.com/en/latest/nki/api/nki.errors.html` is a single rodata literal.
- **`nki_assert` is the shared raise primitive.** `__pyx_pw_…_23nki_assert` sits at `0xbb9a0`, and `NKISyntaxError.__init__` is present in the module, which is what makes the default exception class module-local.
- **Message templates are verbatim, not paraphrased.** `Traditional while loops are not supported`, `number of partitions mismatch in parameters (`, `stack overflow: required sbuf size `, and the `tensor_tensor` docstring example all match rodata exactly. Rows whose template had to be reassembled from interpolation fragments carry those fragment addresses inline.

> **GOTCHA —** the IDA sidecar for `sema.so` is absent in this corpus (only `missing_addresses/*` index files ship for it). All grounding on this page therefore comes from the **shipped `sema.cpython-310-…so` binary itself** (`nm`, `strings`, DWARF `debug_info` — the module is not stripped), not from a decompilation database. The addresses are file VAs from the symbol table and are directly checkable with `nm`; the per-fragment rodata offsets are from `strings -t x`. A reimplementer can reproduce every cited symbol and string from the wheel with two commands.

---

## Related Components

| Component | Relationship |
|---|---|
| `assert_*` engine (W04) | the predicate layer that funnels into these leaves via `nki_assert` |
| `NKIFunc.check_*` validators | the per-API parameter-set validators that drive the asserts |
| `sema_err_url` decorator | wraps every leaf; appends the docs URL and the published `__doc__` |
| nki.jit / nki.trace driver | catches the raised exception and surfaces it to the user |
| `neuronxlogger` (A08) | formats/records the raised diagnostic |

## Cross-References

- [NKI Semantic Legality](sema-legality.md) — the `check_*`/`assert_*` legality layer that raises these `err_*` leaves
- [Frontend Diagnostic & Error Catalog](../frontend/diagnostic-error-catalog.md) — the compiler-driver error catalog that sits *above* this layer and surfaces what `sema` raises
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition limit and SBUF/PSUM budgets enforced by Group A
- [Range/Loop Semantics](range-loop-semantics.md) — `affine_range`/`sequential_range`, source of `err_unexpected_output_dependencies`
- [SPMD Programming Model](spmd-programming-model.md) — `program_id` and grid launch, source of the SPMD-structure diagnostics
