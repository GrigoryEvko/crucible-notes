# The _INTERNAL_KERNEL_REGISTRY Mechanism

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 canonical). The registry lives in `BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so`, under `neuronxcc/starfish/penguin/targets/codegen/` ("Generate Backend IR from tensoriser IR at the TongaISAInst level"). The second (top-k) registry lives in `neuronxcc/nki/_private_kernels/topk/topk_method_mapping.cpython-310-x86_64-linux-gnu.so`. Cython-compiled, `-O3 -fwrapv -fPIC -g`, UNSTRIPPED with `debug_info`. Addresses are version-pinned; cp311/cp312 share the `__pyx` method roster but were not byte-diffed. Provenance: D-P18 (builder/table closer), D-O30 (production-kernel inventory).*

## Abstract

`_INTERNAL_KERNEL_REGISTRY` is the dispatch table that replaces a long `if name == "..." elif name == "..."` chain inside `BirCodeGenLoop`. It is a module-level global `dict[str, InternalKernelConfig]` mapping a macro-op **name string** — the `inst.func_name` of a Penguin `InternalNativeNkiKernel` op — to a record that bundles four things: the compiled NKI kernel leaf to trace, how many operands to marshal, a per-kernel argument-marshalling lambda, and a multi-core grid flag. When the lowering driver `codegenInternalNativeNkiKernel` meets one of these macro nodes, it looks the name up here, prepares the kernel's arguments, and re-traces the matching `_private_kernels` / `private_nkl` leaf through the new NKI frontend.

This is the dispatch surface of exactly **one** of the three kernel-node sinks. The three-sink model (`InstBIRKernel` / `InstNKIKernel` / `InstNKIKLIRKernel`) routes most library macros — MLP, QKV, attention, RMSNorm — by *name-inlining* an already-compiled library node; those never touch this registry. Only the `InstNKIKLIRKernel` sink **re-traces** a compiled leaf from NKI source, and the registry is the table that path dispatches through. Its membership is therefore not "all NKI kernels" but precisely the set whose BIR must be *regenerated* from NKI: convolutions, resize, select-and-scatter, blockwise matmul, and two transposes (see [the three-sink kernel-node model](three-sink-kernel-model.md)).

The registry is **lazy**. The global initializes to `None`; the first lookup builds the table once via `_build_internal_kernel_registry` and caches it. The page documents: the lazy-build mechanism; the complete 13-entry dispatch table with each key→leaf mapping and its config; the `InternalKernelConfig` schema; the query/miss path; and the structurally-separate second registry, `SUPPORTED_TOPK_METHOD_MAPPING`, a kernel-local top-k strategy picker keyed by an `Enum`.

For reimplementation, the contract is:

- The lazy-build / cache protocol (`None`-init global → first-use build → cached return).
- The exact 13 keys, their import modules, their leaf functions, and the per-key config triple (`operand_count`, `additional_args_builder`, `requires_multicore_grid`).
- The `InternalKernelConfig` 5-field schema and its three live arg-builder lambdas.
- The `.get(func_name)` query and the hard-raise miss message.
- The independent top-k mapping and its `Enum` keys.

| | |
|---|---|
| **Registry global** | `_INTERNAL_KERNEL_REGISTRY` — module-level `dict[str, InternalKernelConfig]`, `None`-init |
| **Builder** | `_build_internal_kernel_registry` — mdef #15, `@0xbd4b0`, `BirCodeGenLoop.py:207` |
| **Getter (lazy)** | `get_internal_kernel_registry` — mdef #17, `@0x87d70`, `BirCodeGenLoop.py:341` |
| **Config record** | `InternalKernelConfig.__init__` — mdef #20, `@0x99ef0`, `BirCodeGenLoop.py:192` |
| **Query** | `BirCodeGenLoop._resolve_kernel_config` — mdef #235, `@0xa0ca0`, `BirCodeGenLoop.py:2884` |
| **Consumer** | `codegenInternalNativeNkiKernel` — mdef #243, `@0x8d630` (IT56 `InstNKIKLIRKernel` driver) |
| **Entry count** | **13** (confirmed: 13 `__pyx_n_u_*` key loads, 13 `InternalKernelConfig` constructions) |
| **Second registry** | `SUPPORTED_TOPK_METHOD_MAPPING` — `dict[SupportedTopkMethods, callable]`, 3 entries, own `.so` |

---

## The Lazy-Build / Cache Protocol

### Purpose

The registry is not assembled at module import. Building it eagerly would force-import 13 compiled NKI leaf modules — heavyweight Cython `.so` files like `conv.so` (6.0 MB) — into every process that imports `BirCodeGenLoop`, even compilations that never lower a single registered kernel. Instead the table is built on first use and memoized.

### Algorithm

```c
// module-level, BirCodeGenLoop.so
_INTERNAL_KERNEL_REGISTRY = None;             // global, init None

function get_internal_kernel_registry():       // mdef #17, @0x87d70, py:341
    // docstring: "Get the internal NKI kernel registry."
    reg = GetModuleGlobalName(_INTERNAL_KERNEL_REGISTRY);
    if (reg != Py_None)                         // disasm: reg-vs-None test
        return reg;                             // cache hit — the common path
    reg = _build_internal_kernel_registry();    // mdef #15, @0xbd4b0 — first use only
    SetModuleGlobal(_INTERNAL_KERNEL_REGISTRY, reg);
    return reg;
```

The getter is the **only** caller of the builder: `_build_internal_kernel_registry` is referenced exactly once, through `__pyx_n_s_build_internal_kernel_registry`, from inside the getter. There is no eager-build site at module-exec time. `[CONFIRMED — getter body @0x87d70; single build reference.]`

> **NOTE — first-use is keyed to the first *registered* lowering, not module import.** `get_internal_kernel_registry` is called from `_resolve_kernel_config`, which runs only when `codegenInternalNativeNkiKernel` lowers an `InstNKIKLIRKernel` macro node. A compilation that emits only `InstBIRKernel` library macros (MLP/QKV/attention name-inlined) never builds the registry. The 13 leaf-module imports are paid for once, on demand.

### Considerations

The cache is a single module global with no lock. Cython codegen executes Python-level `dict` builds under the GIL, so the build is atomic with respect to other threads in the same interpreter; a torn build is not a concern in the single-interpreter compiler process. The cached `dict` is never invalidated within a process — the kernel set is static per build.

---

## The 13-Entry Dispatch Table

### Purpose

Each entry maps one macro-op name (the `inst.func_name`) to an `InternalKernelConfig`. The key is the lookup string; the value carries the leaf kernel function plus the marshalling policy. The table below is the full membership, in registry **insertion (program) order** — the same order in which the 13 `__pyx_n_u_<KEY>` constants are loaded and `_PyDict_SetItem`'d in the builder disassembly at `0xbd4b0`.

### Algorithm

```c
function _build_internal_kernel_registry():        // mdef #15, @0xbd4b0, py:207
    // docstring: "Build the registry of all internal NKI kernels that can be
    //   traced to new NKI frontend. ... maps kernel names to their configuration,
    //   eliminating the need for long if-elif chains ..."

    // ---- PHASE A: import the 13 compiled leaf kernel functions ----------------
    from neuronxcc.private_nkl.resize            import resize_nearest_fixed_dma_kernel
    from neuronxcc.private_nkl.select_and_scatter import select_and_scatter_kernel
    from neuronxcc.private_nkl.conv import (conv1d_depthwise_bf01_oi01_bf01,
                                            conv2d_depthwise_f01b_o01i_bf01,
                                            conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh,
                                            conv2d_column_packing,
                                            conv2d_column_packing_io10,
                                            conv2d_column_packing_1)
    from neuronxcc.nki._private_kernels.conv import (
                conv2d_pbp_0f1b_0i1o_01fb_experimental_1,
                conv2d_pbp_fb01_io01_01bf_kernel_experimental_1)   // note: _kernel_
    from neuronxcc.nki._private_kernels.blockwise_mm import blockwise_mm
    from neuronxcc.private_nkl.transpose import (tiled_dve_transpose_10,
                                                 tiled_pf_transpose)

    // ---- PHASE B: build the arg-marshalling lambdas (see schema section) ------
    _get_attrs                     = lambda inst: inst.get_attrs_dict()       // py:240
    _get_conv_attrs                = lambda inst: ...   // BUILT, write-only, UNUSED (see GOTCHA)
    _get_conv_attrs_with_out_shape = lambda inst: {**inst.get_attrs_dict(),
                                                   'out_shape': ...dsts_shapes...}  // py:259
    _get_resize_args               = lambda inst: ...(inst.results, .access_shape, out_shape) // py:266

    // ---- PHASE C: construct 13 configs, insert into a presized dict -----------
    registry = {};                                  // __PyDict_NewPresized
    registry['SelectAndScatter']                         = InternalKernelConfig(select_and_scatter_kernel, operand_count=2);
    registry['ResizeNearest']                            = InternalKernelConfig(resize_nearest_fixed_dma_kernel, operand_count=1, additional_args_builder=_get_resize_args);
    registry['Conv2d_pbp_0f1b_0i1o_01fb_experimental_1'] = InternalKernelConfig(conv2d_pbp_0f1b_0i1o_01fb_experimental_1, additional_args_builder=_get_attrs);
    registry['Conv2d_pbp_fb01_io01_01bf_experimental_1'] = InternalKernelConfig(conv2d_pbp_fb01_io01_01bf_kernel_experimental_1, additional_args_builder=_get_attrs);
    registry['Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh']  = InternalKernelConfig(conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['Conv1d_depthwise_bf01_oi01_bf01']          = InternalKernelConfig(conv1d_depthwise_bf01_oi01_bf01, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['conv2d_column_packing']                    = InternalKernelConfig(conv2d_column_packing, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['conv2d_column_packing_io10']               = InternalKernelConfig(conv2d_column_packing_io10, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['conv2d_column_packing_1']                  = InternalKernelConfig(conv2d_column_packing_1, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['blockwise_mm']                             = InternalKernelConfig(blockwise_mm, additional_args_builder=_get_attrs);
    registry['conv2d_depthwise_f01b_o01i_bf01']          = InternalKernelConfig(conv2d_depthwise_f01b_o01i_bf01, operand_count=2, additional_args_builder=_get_conv_attrs_with_out_shape);
    registry['tiled_pf_transpose']                       = InternalKernelConfig(tiled_pf_transpose, additional_args_builder=_get_attrs, requires_multicore_grid=True);
    registry['tiled_dve_transpose_10']                   = InternalKernelConfig(tiled_dve_transpose_10, additional_args_builder=_get_attrs, requires_multicore_grid=True);
    return registry;
```

### The Table

Registry KEY (= `inst.func_name`, the `__pyx_n_u_` unicode dict key, in insertion order) → import module → leaf function → config triple. All key strings, module paths, and function names are verbatim `strings.json` values from `BirCodeGenLoop.so`.

| # | KEY (macro-op name) | Import module | `kernel_func` (leaf) | `operand_count` | `additional_args_builder` | `grid` |
|---|---|---|---|---|---|---|
| 1 | `SelectAndScatter` | `neuronxcc.private_nkl.select_and_scatter` | `select_and_scatter_kernel` | `2` | — | — |
| 2 | `ResizeNearest` | `neuronxcc.private_nkl.resize` | `resize_nearest_fixed_dma_kernel` | `1` | `_get_resize_args` | — |
| 3 | `Conv2d_pbp_0f1b_0i1o_01fb_experimental_1` | `neuronxcc.nki._private_kernels.conv` | `conv2d_pbp_0f1b_0i1o_01fb_experimental_1` | `None` | `_get_attrs` | — |
| 4 | `Conv2d_pbp_fb01_io01_01bf_experimental_1` | `neuronxcc.nki._private_kernels.conv` | `conv2d_pbp_fb01_io01_01bf_kernel_experimental_1` | `None` | `_get_attrs` | — |
| 5 | `Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` | `neuronxcc.private_nkl.conv` | `conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 6 | `Conv1d_depthwise_bf01_oi01_bf01` | `neuronxcc.private_nkl.conv` | `conv1d_depthwise_bf01_oi01_bf01` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 7 | `conv2d_column_packing` | `neuronxcc.private_nkl.conv` | `conv2d_column_packing` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 8 | `conv2d_column_packing_io10` | `neuronxcc.private_nkl.conv` | `conv2d_column_packing_io10` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 9 | `conv2d_column_packing_1` | `neuronxcc.private_nkl.conv` | `conv2d_column_packing_1` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 10 | `blockwise_mm` | `neuronxcc.nki._private_kernels.blockwise_mm` | `blockwise_mm` | `None` | `_get_attrs` | — |
| 11 | `conv2d_depthwise_f01b_o01i_bf01` | `neuronxcc.private_nkl.conv` | `conv2d_depthwise_f01b_o01i_bf01` | `2` | `_get_conv_attrs_with_out_shape` | — |
| 12 | `tiled_pf_transpose` | `neuronxcc.private_nkl.transpose` | `tiled_pf_transpose` | `None` | `_get_attrs` | `True` |
| 13 | `tiled_dve_transpose_10` | `neuronxcc.private_nkl.transpose` | `tiled_dve_transpose_10` | `None` | `_get_attrs` | `True` |

`[CONFIRMED]` — every key, module, and leaf name is a verbatim string; the per-entry config triple is read byte-for-byte from the kwarg `_PyDict_SetItem` stream that precedes each `__pyx_n_u_<KEY>` registry-set in the builder disassembly (`0xbd4b0`). The interleaving is:

```text
disasm @0xbd4b0 — kwarg / key load order (line numbers within the .asm body):
  n_s_kernel_func, int_2, n_s_operand_count                 → n_u_SelectAndScatter
  n_s_kernel_func, int_1, n_s_operand_count, n_s_args_builder→ n_u_ResizeNearest          (slot var_70 = _get_resize_args)
  n_s_kernel_func, n_s_args_builder                          → n_u_Conv2d_pbp_0f1b…       (slot var_78 = _get_attrs)
  n_s_kernel_func, n_s_args_builder                          → n_u_Conv2d_pbp_fb01…       (slot var_78 = _get_attrs)
  n_s_kernel_func, int_2, n_s_operand_count, n_s_args_builder→ n_u_Conv2d_dw…             (slot var_A8 = _get_conv_attrs_with_out_shape)
  n_s_kernel_func, int_2, n_s_operand_count, n_s_args_builder→ n_u_Conv1d_depthwise…      (var_A8)
  n_s_kernel_func, int_2, n_s_operand_count, n_s_args_builder→ n_u_conv2d_column_packing  (var_A8)
  n_s_kernel_func, int_2, n_s_operand_count, n_s_args_builder→ n_u_conv2d_column_packing_io10 (var_A8)
  n_s_kernel_func, n_s_args_builder                          → n_u_conv2d_column_packing_1
  n_s_kernel_func, int_2, n_s_operand_count, n_s_args_builder→ n_u_blockwise_mm  /  conv2d_depthwise (interleaved)
  n_s_kernel_func, n_s_args_builder, Py_True, n_s_requires_multicore_grid → n_u_tiled_pf_transpose
  n_s_kernel_func, n_s_args_builder, Py_True, n_s_requires_multicore_grid → n_u_tiled_dve_transpose_10
```

Exactly **13** `__pyx_n_u_*` key constants load (`SelectAndScatter`, `ResizeNearest`, `Conv2d_pbp_0f1b_0i1o_01fb_experi…`, `Conv2d_pbp_fb01_io01_01bf_experi…`, `Conv2d_dw_fb01_io01_01bf_rep_nhw…`, `Conv1d_depthwise_bf01_oi01_bf01`, `conv2d_column_packing`, `conv2d_column_packing_io10`, `conv2d_column_packing_1`, `blockwise_mm`, `conv2d_depthwise_f01b_o01i_bf01`, `tiled_pf_transpose`, `tiled_dve_transpose_10`), matched by exactly 13 `InternalKernelConfig` construction sites (26 `__pyx_n_s_InternalKernelConfig` references = 13 × {`GetBuiltinName`, `GetModuleGlobalName` fallback}). No 14th `_PyDict_SetItem` keyed by an `n_u_` constant exists in the body. `[CONFIRMED — disasm enumeration @0xbd4b0.]`

### Two import namespaces

The 13 leaves split across two sibling packages — a finding worth internalizing, because it is the source-protection gradient made concrete:

- **`neuronxcc.private_nkl.{resize, select_and_scatter, conv, transpose}`** — the **production** leaves: select-and-scatter (#1), resize (#2), the six conv variants (#5–9, #11), and the two transposes (#12, #13).
- **`neuronxcc.nki._private_kernels.{conv, blockwise_mm}`** — only the two `conv2d_pbp_*experimental_1` (#3, #4) and `blockwise_mm` (#10).

So `private_nkl` ships the production NKI leaves; `nki._private_kernels` supplies the experimental "pbp" convolutions and the blockwise MoE matmul.

### Key↔function asymmetries

Two transcription traps that a reimplementer copying the keys verbatim will hit:

> **GOTCHA — entry #4: the KEY has no `_kernel`, the FUNCTION does.** The registry KEY is `Conv2d_pbp_fb01_io01_01bf_experimental_1` (no `_kernel`), but the bound `kernel_func` is `conv2d_pbp_fb01_io01_01bf_kernel_experimental_1` — the leaf name carries an extra `_kernel_` segment. Both strings are present and distinct in `strings.json` (`@0x1eef00` is the function). The KEY for #3 *is* identical to its function (`conv2d_pbp_0f1b_0i1o_01fb_experimental_1`); only #4 diverges. Bind the leaf by its true name, not by the key.

> **QUIRK — keys #3–6 are Capitalised, their functions lowercase.** `Conv2d_…` / `Conv1d_…` keys dispatch to `conv2d_…` / `conv1d_…` functions. The capitalised form is the Penguin **macro-op name** (`inst.func_name`); the lowercase form is the Python function symbol. Keys #7–9 (`conv2d_column_packing*`) and #11 (`conv2d_depthwise_…`) are lowercase and function-identical. The case is not a typo — it tracks where the string came from (macro-op enum vs. import symbol).

### Corrections to the inventory pass (D-O30 §3)

> **CORRECTION (D-O30 §3 #1) — there is NO `mlp` / `fused_mlp_isa_kernel` registry entry.** O30 read the registry as mapping `mlp → fused_mlp_isa_kernel`. The binary disproves this two ways: (1) the string `neuronxcc.nki._private_kernels.mlp` IS interned in `BirCodeGenLoop.so` (`@0x1eed00`), but it is referenced only for the `TKG_BS_SEQLEN_THRESHOLD` constant (`@0x1f3280`), used by `codegenMLPKernel` — not for a registry registration; (2) the string `fused_mlp_isa_kernel` is **absent** from `BirCodeGenLoop.so` entirely, and there is no `__pyx_n_u_MLP` / `__pyx_n_u_mlp` registry key. MLP, QKV, attention, RMSNorm, and backward-attention are `InstBIRKernel` library nodes *name-inlined* downstream by their own `codegen<Name>` twins — they are not `InstNKIKLIRKernel` registry-traced carriers (see [the three-sink kernel-node model](three-sink-kernel-model.md) and [NeuronCodegen macro-kernel emitters](neuroncodegen-macro.md)). The complete `__pyx_n_u_*` key enumeration is exactly the 13 above.

> **CORRECTION (D-O30 §3 #2) — resize is `private_nkl.resize.resize_nearest_fixed_dma_kernel`, not `_internal.resize_nearest_kernel`.** O30 charted the registry as `{blockwise_mm, mlp, conv, _internal}` and placed resize under `(_internal)`. The registry actually imports `resize_nearest_fixed_dma_kernel` from `neuronxcc.private_nkl.resize` (`@0x1f22a0`). The `_internal.so` module hosts a *different* function, `resize_nearest_kernel` (no `_fixed_dma`), which the registry does not reference. O30 also omitted `SelectAndScatter` (#1) and the two transpose entries (#12, #13).

---

## The InternalKernelConfig Schema

### Purpose

`InternalKernelConfig` is the value type of the registry: a small immutable-by-convention record holding everything `_resolve_kernel_config` needs to marshal a macro node's operands and re-trace its leaf. It is a plain class with a hand-written `__init__` (not a `@dataclass`), so its field set is the `__init__` parameter list, not a decorator.

### Algorithm

```c
class InternalKernelConfig:                       // __init__ mdef #20, @0x99ef0, py:192
    // docstring (verbatim):
    //   "Configuration for an internal NKI kernel that will be traced to new NKI
    //    frontend.
    //    Attributes:
    //      kernel_func: The actual NKI kernel function to trace
    //      operand_count: Optional number of operands to use (None = use all operands)
    //      additional_args_builder: Callable that builds additional kwargs from inst
    //      requires_multicore_grid: Whether kernel needs grid=(VNC(2),) for multi-core"
    def __init__(self, kernel_func,
                 operand_count=None,              // default Py_None
                 additional_args_builder=None,    // default Py_None
                 requires_multicore_grid=False,   // default Py_False
                 output_is_parameter=False):      // default Py_False — NOT in docstring (see NOTE)
        self.kernel_func              = kernel_func
        self.operand_count            = operand_count
        self.additional_args_builder  = additional_args_builder
        self.requires_multicore_grid  = requires_multicore_grid
        self.output_is_parameter      = output_is_parameter   // 5 plain setattrs
```

### Field semantics

| Field | Offset role | Default | Meaning | Conf |
|---|---|---|---|---|
| `kernel_func` | required arg 1 | — | The imported compiled NKI leaf function to re-trace. Required; every entry sets it. | CONFIRMED |
| `operand_count` | kwarg | `None` | Number of *input* operands to marshal into ndarray proxies. `None` ⇒ use **all** operands. Table split: `2` for conv/select/depthwise/column-packing, `1` for resize, `None` for pbp-conv/blockwise/transpose. | CONFIRMED |
| `additional_args_builder` | kwarg | `None` | `inst → kwargs` marshaller; one of three live lambdas (below). `None` ⇒ no extra kwargs. | CONFIRMED |
| `requires_multicore_grid` | kwarg | `False` | `True` ⇒ trace the leaf with `grid=(VNC(2),)` for multi-core. Set only by the two transposes (#12, #13). | CONFIRMED |
| `output_is_parameter` | kwarg | `False` | Whether the output tensor is a kernel **parameter** vs. an internally-allocated buffer; gates dst-proxy marshalling in `_resolve_kernel_config`. **No registry entry sets it** ⇒ all 13 default `False`. | CONFIRMED |

> **NOTE — the schema has 5 data fields, but the docstring lists only 4.** The class docstring enumerates `kernel_func`, `operand_count`, `additional_args_builder`, `requires_multicore_grid`. `__init__` takes a 5th data parameter, `output_is_parameter` (the 6th positional including `self`), with default `Py_False`, and `_resolve_kernel_config` reads `cfg.output_is_parameter` to gate output-proxy construction. The field is real and consumed; the docstring is stale. Treat the schema as 5 fields. `[CONFIRMED — __init__ pyargnames vector @0x99ef0; read site in _resolve_kernel_config.]`

### The three live arg-builder lambdas

All four lambdas are constructed in Phase B of the builder, but only three are ever bound into a config:

| Lambda | mdef / addr | py | Body | Bound by |
|---|---|---|---|---|
| `_get_attrs` | #1, `@0xad3e0` | 240 | `lambda inst: inst.get_attrs_dict()` — the generic attribute bag | #3, #4, #10, #12, #13 |
| `_get_conv_attrs_with_out_shape` | #5, `@0x111020` | 259 | `get_attrs_dict()` augmented with an explicit `out_shape` derived from `inst.dsts_shapes` | #5–9, #11 |
| `_get_resize_args` | #7, `@0x957e0` | 266 | builds from `inst.results` / `.access_shape` / `out_shape` (resize-specific) | #2 |

> **GOTCHA — `_get_conv_attrs` (mdef #3, `@0x111760`) is built but UNUSED.** The builder constructs a fourth lambda, `_get_conv_attrs`, and writes it into the closure scope-struct field `__pyx_v__get_conv_attrs`. But that scope field is only **written**, never **read** inside the builder body — no `additional_args_builder` kwarg slot dereferences it. Every conv entry that needs conv attributes uses `_get_conv_attrs_with_out_shape` (mdef #5) instead. `_get_conv_attrs` is a refactor remnant: a near-identity / plain `get_attrs_dict` passthrough that no registry entry binds. A reimplementer can omit it without behavioral change. `[STRONG — write-only scope field; zero read sites in the builder.]`

---

## Query and Miss Path

### Purpose

`_resolve_kernel_config` is the single read path into the registry. It takes a Penguin macro-op `inst`, looks up its `func_name`, and returns everything the tracer needs: the resolved config, the leaf function, the extra kwargs, and the proxy operand list.

### Algorithm

```c
function _resolve_kernel_config(self, inst):       // mdef #235, @0xa0ca0, py:2884
    // docstring: "Look up kernel config from registry and prepare common arguments.
    //   Returns (kernel_config, internal_kernel, additional_args, call_args)."
    func_name = inst.func_name                       // n_s_func_name
    cfg = get_internal_kernel_registry().get(func_name)   // lazy-build on first call

    if (cfg is None):                                // MISS — hard raise, no fallback
        raise <Error>("Internal NKI kernel '" + func_name + "' is not registered. "
                      "Available kernels: " + str(registry.keys()))   // both literals verbatim

    kf       = cfg.kernel_func
    builder  = cfg.additional_args_builder
    n_in     = cfg.operand_count                     // None ⇒ all operands
    out_is_p = cfg.output_is_parameter

    // build np.ndarray operand PROXIES (n_s_ndarray ×2):
    //   for n_in inputs (None ⇒ all): ndarray(access_shape/shape, dtype) proxies
    //   for the dst(s): likewise, gated by out_is_p (parameter vs internal buffer)
    call_args = make_input_proxies(inst, n_in) + make_output_proxies(inst, out_is_p)

    additional_args = builder(inst) if builder is not None else {}   // calls the per-kernel lambda

    return (cfg, kf, additional_args, call_args)
```

The miss is a **hard raise** — there is no fallback to a readable `.py` leaf or a default kernel. The message is assembled from two verbatim string fragments: the prefix `Internal NKI kernel '` (`@0x1f43c0`) and the suffix `' is not registered. Available kernels: ` (`@0x1f17e0`), with `str(registry.keys())` appended (the `keys` attribute is read for the message). `[CONFIRMED — both fragments in strings.json; n_s_keys read site.]`

### Consumers

`_resolve_kernel_config` is called by the two tracers `_trace_kernel_beta2` (#237, KLIR — the `NKI_FRONTEND` default) and `_trace_kernel_beta3` (#239, BIR), under the thin router `_trace_internal_kernel_to_new_nki_frontend` (#241, `@0x5cd10`), which is driven by `codegenInternalNativeNkiKernel` (#243, `@0x8d630`). That driver is the **only** codegen that emits an IT56 `InstNKIKLIRKernel` carrier and re-traces a compiled leaf through the new NKI frontend. The registry's membership therefore equals exactly the kernels whose BIR must be regenerated from NKI source — convolution, resize, select-and-scatter, blockwise matmul, transpose — and excludes every library macro that is already compiled and name-inlined (see [NeuronCodegen macro-kernel emitters](neuroncodegen-macro.md), [the three-sink kernel-node model](three-sink-kernel-model.md)).

---

## The Second Registry — SUPPORTED_TOPK_METHOD_MAPPING

### Purpose

A structurally separate, much smaller registry lives in its own compiled module, `neuronxcc/nki/_private_kernels/topk/topk_method_mapping.cpython-310-…so`. It is a kernel-local strategy picker: given a chosen top-k method, it returns the implementation function. Module docstring (verbatim): *"Mapping of topk method name to implementation for autodispatching topk kernel"*. The module header carries *"Copyright (c) 2025, Amazon.com … Do not redistribute/publish/uncythonize without authorization."*

### Algorithm

```c
// module-exec (pymod_exec dict-build), program order
from neuronxcc.nki._private_kernels.topk.cascaded_2_stage_topk import cascaded_2_stage_topk
from neuronxcc.nki._private_kernels.topk.naive_scanning_topk  import naive_scanning_topk
from neuronxcc.nki._private_kernels.topk.rotational_topk      import rotational_topk

class SupportedTopkMethods(Enum):                   // 3 members
    SCANNING; CASCADED; ROTATIONAL

SUPPORTED_TOPK_METHOD_MAPPING = {                    // the registry dict
    SupportedTopkMethods.SCANNING:   naive_scanning_topk,
    SupportedTopkMethods.CASCADED:   cascaded_2_stage_topk,
    SupportedTopkMethods.ROTATIONAL: rotational_topk,
}
```

| Enum member (key) | `kernel_func` | Module |
|---|---|---|
| `SupportedTopkMethods.SCANNING` | `naive_scanning_topk` | `neuronxcc.nki._private_kernels.topk.naive_scanning_topk` |
| `SupportedTopkMethods.CASCADED` | `cascaded_2_stage_topk` | `neuronxcc.nki._private_kernels.topk.cascaded_2_stage_topk` |
| `SupportedTopkMethods.ROTATIONAL` | `rotational_topk` | `neuronxcc.nki._private_kernels.topk.rotational_topk` |

`[CONFIRMED]` — the enum members (`SCANNING` `@0x5730`, `CASCADED` `@0x5740`, `ROTATIONAL` `@0x56f0`), the dict variable (`SUPPORTED_TOPK_METHOD_MAPPING` `@0x5610`), the enum class (`SupportedTopkMethods` `@0x5650`), and the three function/module strings are all verbatim in the module's own `strings.json`.

> **CORRECTION (D-O30 §3 #3) — the first enum member is `SCANNING`, not `NAIVE_SCANNING`.** O30 named the key `NAIVE_SCANNING`. The binary enum member string is `SCANNING`; it is the *function* that is named `naive_scanning_topk`. No `NAIVE_SCANNING` string exists in the module — only `SCANNING`, `CASCADED`, `ROTATIONAL`. The dispatch key is the enum value, not a name string.

### How it differs from the main registry

The two registries are parallel in spirit but independent in every mechanical detail:

| | `_INTERNAL_KERNEL_REGISTRY` | `SUPPORTED_TOPK_METHOD_MAPPING` |
|---|---|---|
| Binary | `BirCodeGenLoop.so` | `topk_method_mapping.so` |
| Key type | `str` (macro-op name) | `SupportedTopkMethods` (`Enum`) |
| Value type | `InternalKernelConfig` (5-field record) | bare callable |
| Build | lazy, first-use, cached | eager at module-exec |
| Consumer | `BirCodeGenLoop` lowering (codegen time) | the top-k kernel itself (trace-time autodispatch) |
| Selection driver | macro-op name from the Penguin op | K-size / cost (small-K → scanning, mid → cascaded, K>8 → rotational) |

The top-k picker chooses a strategy *inside* a kernel at trace time; the internal-kernel registry chooses *which kernel leaf to re-trace* at codegen time. They never interact. `[CONFIRMED — distinct binaries, distinct key/value types.]` The top-k strategy semantics are documented in Part 6.7 (top-k algorithms); see the forward cross-reference below.

---

## Function / Symbol Map

| Symbol | mdef | VA | py-line | Role | Conf |
|---|---|---|---|---|---|
| `InternalKernelConfig.__init__` | 20 | `0x99ef0` | 192 | config record (5 data fields) | CONFIRMED |
| `_build_internal_kernel_registry` | 15 | `0xbd4b0` | 207 | builder (13 entries, lazy) | CONFIRMED |
| `…<locals>._get_attrs` | 1 | `0xad3e0` | 240 | `lambda inst: get_attrs_dict()` | CONFIRMED |
| `…<locals>._get_conv_attrs` | 3 | `0x111760` | ~250 | UNUSED remnant (write-only scope field) | STRONG |
| `…<locals>._get_conv_attrs_with_out_shape` | 5 | `0x111020` | 259 | attrs + `out_shape` (conv) | CONFIRMED |
| `…<locals>._get_resize_args` | 7 | `0x957e0` | 266 | `results` / `access_shape` / `out_shape` (resize) | CONFIRMED |
| `get_internal_kernel_registry` | 17 | `0x87d70` | 341 | lazy-build getter | CONFIRMED |
| `_resolve_kernel_config` | 235 | `0xa0ca0` | 2884 | registry `.get` + ndarray proxies | CONFIRMED |
| `_trace_kernel_beta2` | 237 | `0x18ebc0` | — | KLIR trace leaf (`NKI_FRONTEND` default) | CONFIRMED |
| `_trace_kernel_beta3` | 239 | `0x1ad940` | — | BIR compile leaf | CONFIRMED |
| `_trace_internal_kernel_to_new_nki_frontend` | 241 | `0x5cd10` | 3012 | thin trace router | CONFIRMED |
| `codegenInternalNativeNkiKernel` | 243 | `0x8d630` | 3028 | IT56 `InstNKIKLIRKernel` carrier driver | CONFIRMED |

mdef ordinals are encoded in the `__pyx_pw_…BirCodeGenLoop_<NNN><name>` symbol names (e.g. `_15_build_internal_kernel_registry`, `_17get_internal_kernel_registry`, `_20InternalKernelConfig`, `_235_resolve_kernel_config`); the VAs are the function entry addresses from `functions.json`.

---

## Related Components

| Name | Relationship |
|---|---|
| `codegenInternalNativeNkiKernel` (IT56 driver) | The only consumer; emits the macro node whose name this registry dispatches |
| `_trace_kernel_beta2` / `_trace_kernel_beta3` | The tracers that consume `_resolve_kernel_config`'s output under `NKI_FRONTEND` |
| `_private_kernels` / `private_nkl` packages | Hold the 13 compiled leaf functions the registry imports |
| `topk_method_mapping.so` | The independent second registry (top-k autodispatch) |

## Cross-References

- [The three-sink kernel-node model](three-sink-kernel-model.md) — IT54/IT55/IT56; why only the `InstNKIKLIRKernel` sink is registry-traced
- [NeuronCodegen macro-kernel emitters](neuroncodegen-macro.md) — the EMIT side that picks the macro-op NAME this registry keys on; the `codegen<Name>` twins for the name-inlined library macros
- [Conv / sparse kernel leaves](conv-sparse-kernels.md) — Part 6.8.x: the conv2d/resize/select-and-scatter leaves this registry dispatches to *(planned)*
- [Top-k kernel algorithms](topk-kernels.md) — Part 6.7.12: the scanning/cascaded/rotational top-k implementations and their K-size selection *(planned)*
- [Worked example: flash attention](../front/worked-example-flash-attention.md) — an end-to-end trace through the kernel-node sinks the traced leaves lower into
