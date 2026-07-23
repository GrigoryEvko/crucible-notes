# Conv Device-Lowering and Variant Selection

> *All symbols, addresses, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The selector lives in `neuronxcc/starfish/penguin/targets/transforms/TransformConvOp.cpython-310-x86_64-linux-gnu.so` (1.58 MB, Cython, `-g`, UNSTRIPPED with `debug_info`; class `TransformConvOp`, pass title "Match certain convolutions and lower them to NKI kernels"). Method-index numbers are the `__pyx_mdef_…_NN` slots; addresses are the cp310 `__pyx_pw_*` bodies. cp311 (`…_d73596a3051909e3`) and cp312 (`…_bdd23fff2ef905f0`) share the `__pyx` method roster and string table but were not byte-diffed. Names are cross-checked against the binary string table and `private_nkl/conv.py`.*

## Abstract

`TransformConvOp` is the penguin device-**lowering** pass that decides *which* compiled NKI convolution kernel a 2-D conv will run on, and rewrites the conv into a macro-op carrying that kernel's **name string**. It is the missing selector that the conv kernel pages (depthwise, dense-NHWC, column-packing) deferred to "the backend selects by name based on tensor layout": the backend selector *is this pass*, and the name it writes is the only thing carried forward to codegen.

The pass is structured like an LLVM `SelectionDAG` instruction-selection table, but for whole convolutions instead of nodes. It holds two ordered `list[KernelInfo]` registries — `FUNCTIONAL_KERNEL_REGISTRY` (always tried) and `EXPERIMENTAL_KERNEL_REGISTRY` (tried only under an Option). Each `KernelInfo` pairs a `match_*` predicate over the conv's attributes (feature-group count, batch-group count, strides, dilations, channel counts read through layout permutations) with a kernel **name** and a target layout. `match_and_replace_kernel` walks a registry **in order** and the **first** predicate that returns truthy wins — so *registry order is selection priority*, exactly as a matcher table's row order is. On a win it optionally inserts layout-bridging transposes, marshals the conv attributes into a dict, and rewrites the `ConvTensorOp` to a `NativeKernel` macro whose `func_name` is the matched name. Downstream, `BirCodeGenLoop` looks that name up in `_INTERNAL_KERNEL_REGISTRY` and re-traces the matching `private_nkl/conv.py` leaf — closing the loop documented on [the internal-kernel registry page](internal-kernel-registry.md).

This page documents: the top-level gate (`transformConvTensorOp`), the dispatcher (`match_and_replace_kernel`) and its first-match-wins loop, the `FUNCTIONAL`-vs-`EXPERIMENTAL` split and the build-ordered functional registry, every `match_*` predicate and the attribute test it performs, the `KernelInfo` record schema, the layout-bridging transpose cluster, the `get_kernel_attr` marshaller, the `_lower_to_conv_kernel` rewrite (the name handoff), and the pass-epilogue statistics.

For reimplementation, the contract is:

- The two-registry, first-match-wins dispatch model and why `FUNCTIONAL` is tried before `EXPERIMENTAL`.
- The build-ordered functional registry — its priority order and the exact predicate each entry uses.
- The `KernelInfo` schema, especially that `match` is a `(fn, default_attrs)` **tuple**, not a bare callable.
- The attribute-marshalling dict (key set + insertion order) and the `func_name` name handoff to codegen.
- The layout-permutation algebra that bridges the op's layout to the kernel's expected canonical layout.

| | |
|---|---|
| **Pass class** | `TransformConvOp` (base `TensorOpTransform`) |
| **Top-level gate** | `transformConvTensorOp` — mdef #37, `@0x48b80`, py 735 |
| **Dispatcher** | `match_and_replace_kernel` — mdef #31, `@0x3c420`, py 664 |
| **Rewriter (name handoff)** | `_lower_to_conv_kernel` — mdef #33, `@0x3f700`, py 695 |
| **Attr marshaller** | `get_kernel_attr` — mdef #27, `@0x2a910`, py 513 |
| **Epilogue / stats** | `afterStmtTransform` — mdef #35, `@0x250e0`, py 718 |
| **Registries** | `FUNCTIONAL_KERNEL_REGISTRY` (always) · `EXPERIMENTAL_KERNEL_REGISTRY` (Option-gated) — both `list[KernelInfo]` |
| **Registry value** | `TransformConvOp.KernelInfo` — `@dataclass`; `match` is a `tuple[Callable[[ConvTensorOp, dict], bool], dict]` |
| **Gate** | `op.spatial_dims == 2` only |
| **IR level** | Penguin / tensorizer IR — `ConvTensorOp` → `NativeKernel` (`InstNKIKLIRKernel`, IT56) |
| **Emittable names** | `Conv1d_depthwise_bf01_oi01_bf01` · `conv2d_depthwise_f01b_o01i_bf01` · `Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` · `conv2d_column_packing{,_1,_io10}` · two `Conv2d_pbp_*_experimental_1` |

---

## How a conv finds its kernel — one picture

`mhlo.convolution` is canonicalized to a Penguin `ConvTensorOp` by the [conv-canonicalization pass](../hlo-opt/conv-canonicalization.md). That op carries the layout permutations `_in_perm` / `_kern_perm` / `_out_perm` (framework→canonical axis maps), `feature_group_count`, `batch_group_count`, `stride`, `lhs_dilation`, `rhs_dilation`, `padding`, and the input/kernel/result shapes. `TransformConvOp` consumes that op:

```text
   ConvTensorOp  (← mhlo.convolution, conv-canonicalization)
        │  TensorOpTransform.visit → transformConvTensorOp
        ▼
   transformConvTensorOp(op):  spatial_dims == 2 ?
        │  match_and_replace_kernel(op, FUNCTIONAL_KERNEL_REGISTRY)
        │      for ki in registry:                       # build order = priority
        │          if ki.match[0](op, **ki.match[1]):     # first truthy wins
        │              bridge layout  (ki.{in,kern,out}_perm) ── OffloadedTranspose
        │              attrs = ki.get_kernel_attr(op)        ── stride/pad/perms/groups
        │              _lower_to_conv_kernel(op, ki.name, attrs)
        │  else if self.convolution_kernel_match:        # Option-gated fallback
        │      match_and_replace_kernel(op, EXPERIMENTAL_KERNEL_REGISTRY)
        ▼
   NativeKernel  func_name = "conv2d_column_packing" | "Conv1d_depthwise_…" | …
        │  (codegen) BirCodeGenLoop._resolve_kernel_config(func_name)
        ▼  _INTERNAL_KERNEL_REGISTRY[func_name]
   neuronxcc.private_nkl.conv.<leaf>   (re-traced through the NKI frontend)
```

Two passes, one string. `TransformConvOp` **writes** `func_name` (the selection); `BirCodeGenLoop` **reads** `func_name` and re-traces the leaf (the execution). The name strings written here are byte-for-byte the keys of `_INTERNAL_KERNEL_REGISTRY` and the `def` names in `private_nkl/conv.py`; all eight appear in the `TransformConvOp` table, the `BirCodeGenLoop` table, and as `conv.py` defs.

---

## The top-level gate — `transformConvTensorOp`

### Purpose

The base `TensorOpTransform` visitor routes each `ConvTensorOp` node to the method named `transform<OpType>` — here `transformConvTensorOp`. The method is the gate that admits only 2-D convolutions into the matcher, runs the two registries in the right order, and keeps the module-global match statistics.

### Algorithm

```c
function transformConvTensorOp(self, op):                    // mdef #37, @0x48b80, py:735
    if op.spatial_dims != 2:                                 // py:736 — only 2-D convs are matched here
        return False;
    TransformConvOp.total_conv_ops += 1;                     // py:740 — class-level (module-global) counter

    if match_and_replace_kernel(op, self.FUNCTIONAL_KERNEL_REGISTRY):   // py:743 — always tried first
        TransformConvOp.matched_conv_ops += 1;               // py:744
        return True;

    if not self.convolution_kernel_match:                    // py:747 — the experimental-match Option
        return False;                                        // experimental registry NOT tried

    if match_and_replace_kernel(op, self.EXPERIMENTAL_KERNEL_REGISTRY): // py:750–752 — Option-gated fallback
        TransformConvOp.matched_conv_ops += 1;               // py:753
        return True;
    else:
        print_debug("DIDN'T MATCH KERNEL FOR: " + str(op));  // py:755–756
    return False;
```

`spatial_dims`, `FUNCTIONAL_KERNEL_REGISTRY`, `EXPERIMENTAL_KERNEL_REGISTRY`, `convolution_kernel_match`, `match_and_replace_kernel`, `total_conv_ops`, `matched_conv_ops`, and the `"DIDN'T MATCH KERNEL FOR: "` literal are all present in the binary string table. The two `match_and_replace_kernel` calls read `FUNCTIONAL_KERNEL_REGISTRY` first, then — only past the `convolution_kernel_match` truth-test — `EXPERIMENTAL_KERNEL_REGISTRY`.

> **QUIRK — `FUNCTIONAL` always; `EXPERIMENTAL` only under an Option.** The two registries are not a single fallthrough list. The functional registry is walked unconditionally; the experimental registry is reached only when `self.convolution_kernel_match` (set from the `experimental-convolution-kernel-match` Option) is truthy — it is an *enabling* predicate, not merely a "functional already matched" short-circuit: when false, the body returns `Py_False` before the experimental registry is ever loaded. A reimplementer who concatenates the two registries into one list will mis-select: the experimental `pbp` kernels would become reachable by attribute alone, which the real pass forbids. Keep them separate and Option-gate the second.

> **GOTCHA — the counters live on the class, not the instance.** `total_conv_ops` / `matched_conv_ops` are set on `TransformConvOp` (a module-global), accumulated across every op the pass visits, and reset in `afterStmtTransform`. They are statistics, not per-op state; do not read them to decide anything mid-pass.

---

## The dispatcher — `match_and_replace_kernel`

### Purpose

`match_and_replace_kernel` is the matcher-table walker. Given an op and a registry, it tries each `KernelInfo` predicate in order, and on the first truthy one it bridges layout, marshals attributes, and rewrites the op. It is the single point where *registry order becomes selection priority*.

### Algorithm

```c
function match_and_replace_kernel(self, op, kernel_registry) -> bool:   // mdef #31, @0x3c420, py:664
    for ki in kernel_registry:                              // py:667 — GetIter loop, IN ORDER
        match_fn      = ki.match[0];                         // py:668 — subscript [0] of the match tuple
        default_attrs = ki.match[1];                         // py:669 — subscript [1] (the kwargs dict)
        op.is_pglt    = self.is_pglt;                        // py:670 — propagate partition-group-local flag onto op

        if match_fn(op, **default_attrs):                    // py:671 — the predicate call
            target_in_perm   = ki.in_perm(op);               // py:673 — KernelInfo perm callables
            target_kern_perm = ki.kern_perm(op);             // py:674
            target_out_perm  = ki.out_perm(op);              // py:675

            if not ki.insert_transpose:                      // py:678 — "only bridge if layout differs"
                already_canonical = (op.in_perm   == target_in_perm  and   // py:686 — 3 RichCompares
                                     op.kern_perm == target_kern_perm and
                                     op.out_perm  == target_out_perm);
                if not already_canonical:
                    transpose_conv_input(op, target_in_perm);    // §layout bridging
                    transpose_conv_kern (op, target_kern_perm);
                    transpose_conv_out  (op, target_out_perm);
            else:
                /* ki.insert_transpose True ⇒ always bridge */
                transpose_conv_input(op, target_in_perm);
                transpose_conv_kern (op, target_kern_perm);
                transpose_conv_out  (op, target_out_perm);

            kernel_attrs = ki.get_kernel_attr(op);           // py:690 — marshal the conv attrs (a KernelInfo-bound builder)
            self._lower_to_conv_kernel(op, ki.name, kernel_attrs);  // py:691 — rewrite; ki.name is the kernel NAME
            return True;                                     // FIRST match wins — return immediately
    return False;
```

`PyObject_GetIter` over `kernel_registry`; `ki.match` subscripted `[0]` and `[1]` (the `match` field is a `(fn, dict)` tuple — see the schema); `is_pglt` SetItem; the `in_perm` / `kern_perm` / `out_perm` callables read and *called*; `insert_transpose` read; the three-RichCompare identity guard; `get_kernel_attr`, `name`, `_lower_to_conv_kernel`, and the `transpose_conv_{input,kern,out}` references are all present in the 13021-byte body at `@0x3c420`.

> **QUIRK — first-match-wins, so registry order is priority.** The loop `return True`s on the first truthy predicate; nothing later in the list is consulted. The predicate-truthy branch reads `_lower_to_conv_kernel`, sets the return slot to `Py_True`, and jumps straight to the function return rather than advancing the iterator. The functional registry's build order (below) is therefore the variant-selection priority order. This is the same contract as an `iselDAG` matcher table — earlier patterns shadow later ones — and the same reimplementation hazard: get the order wrong and you select a lower-priority kernel for a conv a higher-priority kernel should have claimed.

> **NOTE — the identity-skip is an optimization, not a correctness gate.** When `insert_transpose` is false and the op's layout already equals the target perms, the bridging transposes are skipped (py:686). When the layout differs, `OffloadedTranspose` ops are inserted so the kernel always sees its canonical layout. A reimplementer can always insert the bridge and rely on a later transpose-elimination pass; the skip just avoids emitting identity transposes here.

---

## The two registries and the variant decision tree

### Purpose

The decision tree is not an `if/elif` ladder — it is the ordered list of `KernelInfo` entries, each carrying a `match_*` predicate. Walking the functional registry in build order *is* the decision tree. This section gives the build order (= priority) and the predicate each entry uses.

### The functional registry, in build (priority) order

Both registries are constructed once in module-exec as `list[KernelInfo]`. The build order is the order the `match_*` method globals are loaded and `KernelInfo`s constructed. The `__pyx_mdef` method-index numbers below come from the binary symbol table and fix each predicate's identity; the kernel **name** column matches the string table, the `BirCodeGenLoop` registry, and `conv.py` byte-for-byte.

| Prio | `match_*` predicate (mdef) | Selected kernel **name** | Confidence |
|---|---|---|---|
| 1 | `match_Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` (#9) | `Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` | CERTAIN (unique match↔name) |
| 2 | `match_conv_depthwise_backward` / `is_low_channel_conv_1` | `conv2d_column_packing` | HIGH (name↔shape) |
| 3 | `match_Conv1d_depthwise_bf01_oi01_bf01` (#13) | `Conv1d_depthwise_bf01_oi01_bf01` | CERTAIN (unique) |
| 4 | `match_conv_depthwise_backward` (2nd shape) | `conv2d_column_packing_1` / `_io10` | HIGH (trio split inferred) |
| 5 | `match_conv2d_depthwise_f01b_o01i_bf01` (#21) | `conv2d_depthwise_f01b_o01i_bf01` | CERTAIN (unique) |
| 6 | `match_conv_depthwise_forward` | (depthwise-forward, stride-3 variant) | HIGH |
| 9 | `match_replication_conv` | (K-replication path) | HIGH |
| 10 | `match_Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh2` (#19) | `Conv2d_dw_…_Pcinh` (alt-shape, lowest prio) | CERTAIN (predicate) |

The two experimental entries:

| `match_*` predicate (mdef) | Selected kernel **name** | Body |
|---|---|---|
| `match_Conv2d_pbp_0f1b_0i1o_01fb_experimental_1` (#23) | `Conv2d_pbp_0f1b_0i1o_01fb_experimental_1` | `return False` stub |
| `match_Conv2d_pbp_fb01_io01_01bf_experimental_1` (#25) | `Conv2d_pbp_fb01_io01_01bf_experimental_1` | `return False` stub |

> **GOTCHA — the column-packing trio's exact predicate↔name binding is INFERRED.** `conv2d_column_packing`, `_1`, and `_io10` all exist as names; `match_conv_depthwise_backward` is registered twice (two shapes) and `is_low_channel_conv_1` is the shared low-channel gate. The exact pairing of which of the three names binds to which registration is inferred from the three-name set plus the duplicated matcher — the unique-name matchers (#9 rep-nhwc, #13 conv1d-dw, #21 conv2d-dw, #23/#25 pbp) are certain by unique match↔name correspondence. A byte-exact fix would read the `KernelInfo` argument tuples literally from module-exec.

The eight per-kernel statistic strings (`"Number of times kernel <NAME> is matched"`) independently corroborate the exact eight names the pass can emit — one such string exists verbatim per kernel.

### The `match_*` predicates

Each predicate reads conv attributes and returns a bool. The tests, in priority order:

#### `match_Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh` — dense NHWC-replication (prio 1)

The DENSE (cross-channel) conv that uses the PE array via an NHWC partition-replication layout (`Pcinh` = partition dimension folded from `C_in × h`). The richest dense predicate (~15.4 KB body, py 333). It validates a real dense conv (feature-group count vs channel counts), the input/kernel layout reachable by the rep-NHWC transpose, dtype/`sizeinbytes` within the SBUF budget, the `allow_stride` flag for strided support, and the `is_pglt` (partition-group-local-tensor) gate. Registered prio 1 — a dense conv that fits this path takes it before any depthwise matcher even looks. The attribute set it reads is direct; reading the dtype/size arithmetic as an SBUF budget is an inference from its mirroring the dense-conv kernel's `MAX_F`/`dtype_size` logic.

#### `match_Conv1d_depthwise_bf01_oi01_bf01` — 1-D depthwise (prio 3)

The richest depthwise predicate (~15.9 KB body, py 402). Reads `feature_group_count`, `batch_group_count`, both dilations, `rhs_reversal`, `stride`, `padding`, all four perms, and all three shapes. Matches a 1-D depthwise (effective spatial extent 1 on one axis) with `feature_group_count == C_in`, no `rhs_reversal`, and stride/padding within the `conv1d_depthwise` dispatcher's accepted regime. Selects the `conv1d_depthwise_bf01_oi01_bf01` wrapper, which itself dispatches a default vs `f_packing` body. The full attribute set it touches is direct; reading the `rhs_reversal`/`padding`/`res_shape` tests as the conv1d kernel's shape-legality guards is an inference from that kernel's requirements.

#### `match_conv2d_depthwise_f01b_o01i_bf01` — direct 2-D depthwise (prio 5)

```c
function match_conv2d_depthwise_f01b_o01i_bf01(self, op):   // mdef #21, @0x38070, py:485
    if op.lhs_dilation != [1, 1]:                            // py:486
        return False;
    if op.batch_group_count != 1:                            // py:491
        return False;
    // depthwise signature: one channel per group ⇒ input channel axis == kernel grouped axis
    return op.in_shape[op.in_perm[1]] == op.kern_shape[op.kern_perm[0]];   // py:496/498
```

The perm-indexed channel-equality test (`in_perm[1]` = the input channel axis, `kern_perm[0]` = the kernel's output/grouped axis) is the pure-depthwise signature. Selects `conv2d_depthwise_f01b_o01i_bf01` — the Vector-engine direct multiply+reduce depthwise that does *not* use the PE array.

#### `match_conv_depthwise_forward` — stride-3 forward depthwise (prio 6)

```c
function match_conv_depthwise_forward(self, op):            // @0x36070, py:460
    if op.lhs_dilation != [1, 1]:                            // py:461 — builds [int_1,int_1]
        return False;
    ... feature_group_count / channel (perm-indexed) checks ...   // py:463–470
    return op.stride == [3, 3];                              // py:473 — builds [int_3,int_3]
```

> **NOTE — this matcher is stride-3-specific.** py:473 builds the list `[3, 3]` (two loads of `__pyx_int_3` at `@0x36bfe`/`@0x36c11`) and RichCompares `op.stride` against it, while the dilation guards use `[1, 1]`. So this is a *specialised stride-3* depthwise-forward variant, not the general unit-stride case — that goes to `match_conv2d_depthwise_f01b_o01i_bf01` above.

#### `match_conv_depthwise_backward` — backward / transpose-conv depthwise (prio 2/4)

The gradient (backward) depthwise. Reads `batch_group_count`, both dilations. In the backward form, stride is encoded as `rhs_dilation` and the grouping appears on `batch_group_count` (not `feature_group_count`), with the LHS/RHS dilation roles swapped relative to forward. This is the entry that routes to the `column_packing_{1,io10}` shapes.

#### `is_low_channel_conv_1` — the column-packing gate (module-level helper)

```c
function is_low_channel_conv_1(op):                         // @0x39550, MODULE-level fn (not a method), py:143
    C_in   = op.in_shape[op.in_perm[1]];
    C_out  = op.res_shape[op.out_perm[1]];
    // structural equalities, each an embedded AssertionError:
    assert op.res_shape[op.out_perm[0]] == op.in_shape[op.in_perm[0]],
           "Convolution output batch must match input batch";
    assert C_in  == op.kern_shape[op.kern_perm[?]],
           "Convolution input channels must match kernel input channels";
    assert C_out == op.kern_shape[op.kern_perm[?]],
           "Convolution output channels must match kernel output channels";
    return (chain of RichCompares — channel magnitude is "low") ;    // all must pass
```

The three assertion messages are verbatim in the binary. This is the predicate the `conv2d_column_packing{,_1,_io10}` entries use to claim low-`C_in` convs (the column-packing / diagonal-extract kernel family). The `_1` suffix on `is_low_channel_conv_1` mirrors the `conv2d_column_packing_1` name. The "low channel" threshold is shape-derived — a chain of comparisons against the kernel's own channel counts — not a single literal.

#### `match_Conv2d_dw_…_Pcinh2` — alt-shape layout fast-path (prio 10)

```c
function match_Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh2(self, op):  // mdef #19, @0x2b8b0, py:477
    // reads ONLY in_perm, in_shape, kern_perm, out_perm — a pure layout predicate
    return <perms admit the EXPERIMENTAL_KERNEL_ALT_SHAPE layout>;
```

A pure layout-permutation predicate that matches the *same* rep-NHWC-`Pcinh` kernel but for a second admissible permutation set (`EXPERIMENTAL_KERNEL_ALT_SHAPE`). Because it inspects only perms + `in_shape`, it is the "data is already (or trivially transposable) to the alt layout" fast-path, registered last so it is the lowest-priority rep-NHWC fallback. Its alt-shape role is fixed by the `EXPERIMENTAL_KERNEL_ALT_SHAPE` constant it gates on.

#### `match_replication_conv` — K-replication path (prio 9)

The legacy K-replication matcher. It inspects both the `input` and `kernel` tensors and their perms/shapes (reads `input`, `kernel`, `tensor`, `isInput`) to decide whether a stacked-channel replication lowering applies. Registered after the depthwise + dense entries — a lower-priority general path.

#### `match_Conv2d_pbp_*_experimental_1` — disabled stubs

```c
function match_Conv2d_pbp_0f1b_0i1o_01fb_experimental_1(self, op):  // mdef #23, @0x21db0, py:505
    return False;                                            // single return — NO attr reads
function match_Conv2d_pbp_fb01_io01_01bf_experimental_1(self, op):  // mdef #25, @0x22370, py:509
    return False;                                            // single return — NO attr reads
```

> **GOTCHA — the experimental `pbp` kernels never auto-match.** Both bodies are single-`return False` stubs with no attribute reads at all — the parsed `op` argument is never dereferenced, and both functions are byte-identical in size (1464 bytes / 323 instructions each, `@0x21db0` and `@0x22370`). So even when the experimental registry IS tried (Option on), nothing in it matches by attribute — `transformConvTensorOp` falls to its `print_debug("DIDN'T MATCH KERNEL FOR: …")` branch. The `pbp` ("per-batch-partition") experimental conv kernels are reachable only by an out-of-band path (explicit name / a different Option), not by this decision tree, and there is no `pbp` kernel `def` anywhere in `private_nkl/conv.py` (the capitalized `Conv2d_pbp_*` names exist only in this `.so`'s string pool). A reimplementer wiring `pbp` selection into the attribute matcher is wrong; in this build they are matcher-disabled.

---

## The `KernelInfo` record

### Purpose

`KernelInfo` is the registry value type — a `@dataclass` that bundles a kernel's selection predicate, its name, its target layout, and its transpose/stride policy.

### Schema

| Field | Type (from the binary annotation strings) | Role | Confidence |
|---|---|---|---|
| `name` | `str` | the macro-op `func_name` (= registry key) | CERTAIN |
| `match` | `tuple[Callable[[ConvTensorOp, dict], bool], dict]` | `(predicate, default_attrs)` | CERTAIN |
| `in_perm` | `Callable[[ConvTensorOp], dict]` | target input permutation (computed) | HIGH |
| `kern_perm` | `Callable[[ConvTensorOp], dict]` | target kernel permutation | HIGH |
| `out_perm` | `Callable[[ConvTensorOp], dict]` | target output permutation | HIGH |
| `insert_transpose` | `bool` | force layout-bridging transposes | HIGH |
| `is_pglt` | `bool` | partition-group-local-tensor flag | HIGH |
| `force_insert_transpose`, `allow_stride` | `bool` | transpose / stride policy knobs | HIGH |

> **QUIRK — `match` is a tuple, not a bare callable.** The dispatcher subscripts `ki.match[0]` for the predicate and `ki.match[1]` for a default-kwargs dict, then calls `match[0](op, **match[1])`. A reimplementer who stores a bare function in `match` will crash on the `[1]` subscript. The kwargs dict lets one `match_*` function serve several `KernelInfo` rows with different per-row defaults. The annotation strings `tuple[Callable[[ConvTensorOp, dict], bool], dict]`, `Callable[[ConvTensorOp], dict]`, and `list[KernelInfo]` are verbatim in the binary.

---

## Layout bridging — `transpose_conv_input` / `_kern` / `_out`

### Purpose

When the chosen `KernelInfo`'s target perm differs from the op's current perm (or `insert_transpose`/`force_insert_transpose` is set), the dispatcher inserts `OffloadedTranspose` ops to bridge the op's framework layout to the kernel's expected canonical layout. The math is a small permutation-algebra cluster of module-level helpers.

### The permutation algebra

```c
invert_perm(p)               // inverse permutation
compose_perm(p, q)           // composition:  apply_perm(T, r) == apply_perm(apply_perm(T, p), q)
apply_perm_to_dims(shape, p) // apply a perm to a shape tuple
get_bridging_perm(cur, tgt)  // r such that  apply_perm(apply_perm(T, cur), r) == apply_perm(T, tgt)  (input/kernel side)
get_bridging_output_perm(…)  // the analogous bridge on the output side
```

The convention, verbatim from the docstrings: *"apply_perm(input_tensor, _in_perm) == canonical_input_tensor"* and *"apply_perm(kernel_tensor, _kern_perm) == canonical_kernel_tensor"*. So the `ConvTensorOp`'s `_in_perm`/`_kern_perm`/`_out_perm` encode the framework→canonical axis mapping, the `KernelInfo` perms encode the kernel→canonical mapping, and the bridge `r = get_bridging_perm(op.in_perm, target_in_perm)` is the difference. `transpose_conv_input` (docstring "Transposes convolution input (i.e. image) so that it has the desired permutation."), `transpose_conv_kern`, and `transpose_conv_out` each compute that bridge and, unless `r` is identity (and not `force_insert_transpose`), insert one `OffloadedTranspose`. The py:686 identity-skip in the dispatcher is exactly "`r == identity` ⇒ no transpose".

---

## Attribute marshalling — `get_kernel_attr`

### Purpose

`get_kernel_attr` builds the dict that `_lower_to_conv_kernel` serializes (via `json.dumps`) into the `NativeKernel`'s `attrs`. It is the upstream half of the attribute pipeline; the downstream half is `BirCodeGenLoop`'s `_get_conv_attrs_with_out_shape`, which reads that JSON back and augments it with the output shape.

### Algorithm

```c
function get_kernel_attr(self, op):                         // mdef #27, @0x2a910, py:513
    d = {};                                                  // _PyDict_NewPresized
    d['stride']              = op.stride;                    // insertion order:
    d['padding']             = op.padding;
    d['rhs_dilation']        = op.rhs_dilation;
    d['lhs_dilation']        = op.lhs_dilation;
    d['in_perm']             = op.in_perm;
    d['kern_perm']           = op.kern_perm;
    d['out_perm']            = op.out_perm;
    d[<shape keys>]          = op.{input,kernel,dst}.shape;  // via shape/input/kernel/dst/tensor
    d['batch_group_count']   = op.batch_group_count;
    d['feature_group_count'] = op.feature_group_count;
    return d;
```

The `PyDict_SetItem` sequence with `getattr(op, …)` for each key runs in exactly that order in the body, so the dict's insertion order is fixed.

> **NOTE — a SAME-padding sibling marshaller exists.** `get_kernel_attr_same_hl_pad` (mdef #29, `@0x20330`, py 528) is a variant that normalizes padding/stride to the "same high/low pad" convention (reads only `padding`/`stride`). It is used by the kernels that require symmetric (SAME) padding semantics. A reimplementer needs both marshallers and must pick by the kernel's padding contract.

---

## The rewrite and name handoff — `_lower_to_conv_kernel`

### Purpose

This is where selection becomes a fact in the IR: the conv op is replaced by a `NativeKernel` macro whose `func_name` is the matched kernel **name** and whose `attrs` is the marshalled JSON. The original conv is erased.

### Algorithm

```c
function _lower_to_conv_kernel(self, op, kernel_name, kernel_attrs):    // mdef #33, @0x3f700, py:695
    builder = IRBuilder(op.function);            // IRBuilder over the parent function
    builder.insert(<before op>);                  // set insertion point
    new = NativeKernel(                           // the InstNKIKLIRKernel (IT56) macro carrier
              srcs      = op.srcs,                 // image, filter
              dsts      = op.dsts,                 // result
              tensor    = <src/dst>.tensor,
              func_name = kernel_name,             // ★ the matched ki.name — the registry key
              attrs     = json.dumps(kernel_attrs));   // JSON payload of get_kernel_attr's dict
    builder.add(new);
    op.eraseFromParent();                         // delete the original conv
```

`IRBuilder`, `NativeKernel`, `srcs`/`dsts`/`tensor`, `json`/`dumps`, `eraseFromParent`, and `add` are all referenced in the body; `kernel_name`/`kernel_attrs` are positional params 2/3.

> **QUIRK — the kernel is selected by a string, not a pointer.** `_lower_to_conv_kernel` writes a *name*, not a kernel reference. At codegen, `BirCodeGenLoop.codegenInternalNativeNkiKernel → _resolve_kernel_config(func_name)` does `get_internal_kernel_registry().get(func_name)` and re-traces the matching `private_nkl/conv.py` leaf. The decoupling is deliberate: selection (this pass) and execution (codegen) are two passes joined only by the string. That is why the conv kernel leaves have no in-wheel caller — *this* pass is the caller, by name. See [the internal-kernel registry](internal-kernel-registry.md) and [the three-sink kernel-node model](three-sink-kernel-model.md).

---

## Pass epilogue and statistics — `afterStmtTransform`

After the visitor has processed all statements, `afterStmtTransform` (mdef #35, `@0x250e0`, py 718) finalizes the pass:

- Computes `conv_kernel_match_percentage = 100.0 * matched_conv_ops / total_conv_ops` (the `"%.2f"` formatting).
- Emits `Statistic` counters (via `update`/`print_info`/`reset`): `"Total number of convolution operations processed"`, `"Number of convolution operations matched to kernels"`, `"Percentage of convolution operations matched to kernels"`, and one `"Number of times kernel <NAME> is matched"` per kernel — verbatim for all eight names.
- Resets the module-global `total_conv_ops` / `matched_conv_ops` counters for the next module.

The eight per-kernel counter strings independently corroborate the exact set of names the pass can emit.

---

## Pass configuration and Options

`TransformConvOp.__init__` (`@0x2dd10`, py 185) reads:

| Field | Source | Role | Confidence |
|---|---|---|---|
| `convolution_kernel_match` | Option `experimental-convolution-kernel-match` | gates whether `EXPERIMENTAL_KERNEL_REGISTRY` is tried | CERTAIN |
| `do_not_insert_convolution` | Option | suppresses the lowering / transpose insertion ("*Optionally* match") | CERTAIN |
| `is_pglt` | flag | partition-group-local-tensor flag propagated onto each op during matching | CERTAIN |
| `EXPERIMENTAL_KERNEL_ALT_SHAPE` | module constant | gates the `…Pcinh2` alt-shape matcher | CERTAIN |

Pass docstring, verbatim: *"TransformConvOp - Optionally match convolutions and lower them to NKI kernels."* Base class `TensorOpTransform`.

---

## End-to-end: selecting a kernel for a given conv

Given an `mhlo.convolution` canonicalized to a `ConvTensorOp`, `TransformConvOp` (2-D only) walks `FUNCTIONAL_KERNEL_REGISTRY` in build order; the first truthy `match_*` wins:

```text
TRY FUNCTIONAL_KERNEL_REGISTRY (priority order):
  1. dense rep-NHWC-Pcinh    → DENSE conv whose layout+dtype fit the NHWC-replication PE-array path
                                 → "Conv2d_dw_fb01_io01_01bf_rep_nhwc_Pcinh"
  2. column-packing          → low-channel / backward depthwise (is_low_channel_conv_1 +
                                 match_conv_depthwise_backward) → "conv2d_column_packing{,_1,_io10}"
  3. conv1d depthwise        → 1-D depthwise (feature_group_count == C_in, no rhs_reversal)
                                 → "Conv1d_depthwise_bf01_oi01_bf01"
  4. conv2d direct depthwise → feature_group_count==C_in, batch_group_count==1, unit dilation,
                                 in_shape[in_perm[1]]==kern_shape[kern_perm[0]]  (Vector-engine DW)
                                 → "conv2d_depthwise_f01b_o01i_bf01"
  5. stride-3 forward DW / replication → specialised / general fallbacks
  6. rep-NHWC-Pcinh2 (alt layout, lowest priority)  → "Conv2d_dw_…_Pcinh"
ELSE (only if convolution_kernel_match Option on):
  EXPERIMENTAL_KERNEL_REGISTRY → both pbp matchers are return-False stubs ⇒ NOTHING matches
                               ⇒ print_debug "DIDN'T MATCH KERNEL FOR: …"; conv left to a generic path.
```

Decision summary: **depthwise vs dense** is decided by `feature_group_count` (==1 dense vs ==`C_in` depthwise) and the perm-indexed channel-equality test; **column-packing** by the low-channel predicate; the **dense rep-NHWC dtype/SBUF-budget** gate matches the dense kernel's `MAX_F`/`dtype_size` heuristics. On the winning entry: bridge layout (only if perms differ), marshal attrs, rewrite to `NativeKernel(func_name=ki.name)`, erase the conv. At codegen, `BirCodeGenLoop` looks the name up and re-traces the leaf.

---

## Evidence summary

The five highest-risk claims and what pins each:

1. **"`FUNCTIONAL` is tried before `EXPERIMENTAL`, and `EXPERIMENTAL` is Option-gated."** `transformConvTensorOp` (`@0x48b80`) loads `match_and_replace_kernel` + `FUNCTIONAL_KERNEL_REGISTRY` for the first call, then reads `convolution_kernel_match`; when that test is false the body sets the result to `Py_False` and returns *before* the `EXPERIMENTAL_KERNEL_REGISTRY` global is ever loaded. Only on a true test does it load the second registry. A reimplementer must keep them as two registries with the Option gate between, not one concatenated list.

2. **"First-match-wins; registry order = priority."** The dispatcher (`@0x3c420`) iterates `kernel_registry` via `GetIter`; on the first truthy predicate it reads `_lower_to_conv_kernel`, sets the return slot to `Py_True`, and jumps to the function return rather than advancing the iterator. The functional registry's *build* order comes from the module-exec `match_*` reference sequence: the unique-name predicates' positions are certain, the column-packing trio's exact slots are **INFERRED** (flagged in the priority table).

3. **"The two `pbp` experimental matchers are `return False` stubs."** Both bodies (`@0x21db0`, `@0x22370`) return `Py_False` with no `GetAttr` on the `op` argument and are byte-identical in size (1464 B / 323 insns each). No `pbp` kernel `def` exists in `private_nkl/conv.py`, so "reachable only out-of-band" follows directly.

4. **"`match_conv_depthwise_forward` requires `stride == [3, 3]`."** The body builds a 2-element list with two `__pyx_int_3` stores (`@0x36bfe`/`@0x36c11`) and RichCompares `op.stride` against it, while the dilation guards use `[1, 1]`. This is the one numeric constant in the whole tree, and it makes this a stride-3 *specialised* matcher.

5. **"`get_kernel_attr` emits stride/padding/dilations/perms/group-counts."** The body (`@0x2a910`) does `_PyDict_NewPresized` then `PyDict_SetItem` in the order stride → padding → rhs_dilation → lhs_dilation → in_perm → kern_perm → out_perm → (a tensor-shape key) → batch_group_count → feature_group_count. Its symmetry with `BirCodeGenLoop._get_conv_attrs_with_out_shape` (which adds `out_shape`) is a strong correlation across the name handoff rather than a shared code path.

Every symbol, kernel name, predicate name, registry name, annotation string, assertion message, docstring, and statistic string on this page sits in the cp310 `.so` string table, and the name handoff resolves end-to-end against `BirCodeGenLoop` (`_INTERNAL_KERNEL_REGISTRY`, `_get_conv_attrs`/`_get_conv_attrs_with_out_shape`, the conv name keys) and `private_nkl/conv.py`, where `conv2d_column_packing{,_1,_io10}` are all present as leaf `def`s. All 16 function addresses match the IDA function map. The one remaining gap is the column-packing trio's exact predicate↔name binding — which of `conv2d_column_packing` / `_1` / `_io10` binds to which of the two `match_conv_depthwise_backward` registrations — which needs the `KernelInfo` argument tuples read literally from module-exec.

---

## Related Components

| Name | Relationship |
|---|---|
| [Conv Canonicalization](../hlo-opt/conv-canonicalization.md) | produces the `ConvTensorOp` (with `_in_perm`/`_kern_perm`/`_out_perm`) this pass consumes |
| [The `_INTERNAL_KERNEL_REGISTRY`](internal-kernel-registry.md) | downstream table that maps the `func_name` this pass writes → the `conv.py` leaf |
| [The Three-Sink Kernel-Node Model](three-sink-kernel-model.md) | the `NativeKernel` this pass emits is the IT56 `InstNKIKLIRKernel` sink (the only re-traced one) |
| `nki/conv-depthwise.md` *(planned, 6.8.2)* | the `Conv1d_depthwise_*` / `conv2d_depthwise_f01b_*` leaves selected at prio 3/5 |
| `nki/conv-dense.md` *(planned, 6.8.3)* | the `Conv2d_dw_…_rep_nhwc_Pcinh` dense leaf selected at prio 1 |
| `nki/conv-column-packing.md` *(planned, 6.8.4)* | the `conv2d_column_packing{,_1,_io10}` leaves selected at prio 2/4 |

## Cross-References

- [The Compile Pipeline at a Glance](../front/pipeline.md) — where penguin lowering sits between HLO→Penguin and BIR codegen
- [Conv Canonicalization](../hlo-opt/conv-canonicalization.md) — the Part-4 pass that builds the `ConvTensorOp`
- [The `_INTERNAL_KERNEL_REGISTRY` Mechanism](internal-kernel-registry.md) — the name→leaf dispatch table the handoff lands in
- [The Three-Sink Kernel-Node Model](three-sink-kernel-model.md) — IT54/IT55/IT56 sinks; this pass feeds IT56
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the partition/SBUF budget the dense rep-NHWC predicate gates against
