# SelectAndScatter & ReduceWindow Forward Lowering

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310/311/312 wheels; the three are byte-identical for the C++ core and the Python kernels are textually identical across them). The MLIR printer / verifier symbols live in `hlo2penguin` (IDA sidecars under `ida/…hlo2penguin/`); the NKI kernels are readable `.py` under `extracted/…/neuronxcc/nki/kernels/vision.py` and `…/neuronxcc/private_nkl/select_and_scatter.py`. The BIR `PoolFunctionType` enum lives in `libBIR.so`. Treat every address as version-pinned.*

## Abstract

Windowed reductions in `neuronx_cc` cover two HLO ops that look unrelated but share one machinery: **`mhlo.reduce_window`** (the forward pool — max-pool, avg-pool, and the long tail of other commutative reductions) and **`mhlo.select_and_scatter`** (its backward twin — the gradient of max-pooling, which routes each window's upstream gradient back to the position that won that window's max). This page is the reimplementation reference for both: how the front-end dispatches each, where they bottom out, and the exact NKI kernel that implements the one hard-wired select-and-scatter shape.

The two ops bottom out very differently. `reduce_window` is never traced to a hand-written op-sequence inside `hlo2penguin`; `MhloToPythonPrinter::print<ReduceWindowOp>` re-emits it verbatim as one NKI/penguin tensor op carrying `(window_shape, stride, padding, reduce_function=<numpy name>)`, and the choice of Pool engine (`InstPool` Max/Avg, opcode `0x45`) versus reduce engine (`InstTensorReduce`) happens downstream in the NKI back-end. `select_and_scatter`, by contrast, has a *dedicated hand-written NKI kernel* — `vision.select_and_scatter_kernel` — gated behind a native-kernel flag, plus an XLA stock `SelectAndScatterExpander` fallback. The kernel is the interesting artifact: because the Pool engine's `InstPool(Max)` returns only the max *value* and discards the argmax *position* (`PoolFunctionType = {Max, Avg}` — there is no "argmax" pool function), the kernel must **recompute the argmax** with a reduce-max → equal-mask → iota-select → reduce-min chain, then **scatter-accumulate** because overlapping windows share output pixels.

Two facts shape everything and are easy to get wrong. First, there is **no `Sum` `PoolFunctionType`** — avg-pool is `add`-reduction followed by a separate `÷N` (folded to a `1/N` reciprocal at BIR-encode time), and a *pure* sum-window falls to the reduce engine, not the Pool engine. Second, the hand-written kernel implements **exactly one window shape** — `(3,3)` window, `(2,2)` stride, `(1,1)` pad, `C=64`, `select=GT`, `scatter=add` — and asserts every one of those; any other shape falls back to the generic expander. This page treats `reduce_window` and `select_and_scatter` as the general case of which C09's `ResizeNearestGrad` average-pool lowering ([4.39 canonicalize-for-tensorizer](../hlo-opt/canonicalize-for-tensorizer.md)) is the narrowest instance.

For reimplementation, the contract is:

- **The dispatch fork** — when `reduce_window`/`select_and_scatter` becomes a native NKI custom-call vs. a generic printer emit vs. an XLA stock expander, and the flags that gate it.
- **The generic `reduce_window` emit** — the five window attributes read off the op, and the reduction-body → numpy-name decode table.
- **The argmax-recompute algorithm** — the reduce/equal/iota/min chain that reproduces XLA's "first max wins" tie-break, and *why the Pool engine cannot do it*.
- **The overlap scatter-accumulate** — why `stride < window` forces a per-offset masked-multiply + 9-way reduce-add, and how the lowered form keeps it in SBUF.
- **The dilation legality gate** — `NeuronHloVerifier` rejects base/window dilation; only undilated windows survive.

| | |
|---|---|
| **Generic `reduce_window` printer** | `MhloToPythonPrinter::print<ReduceWindowOp>` @ `0x20d5ff0` (3642 B, 134 bb) |
| **Reduce-body decode** | `extractReduceFunction` @ `0x20afca0` → `extractReduceFunctionBlock` @ `0x20afbe0` → `getReduceOpStrFromOperation` |
| **`select_and_scatter` printer** | `MhloToPythonPrinter::printSelectAndScatter` @ `0x20de190` |
| **Dilation verifier** | `xla::hilo::isValidReduceWindowOp` @ `0x2022dc0` (1386 B, 61 bb) |
| **Native-kernel pass** | `xla::LowerToCustomNativeKernel::Run` @ `0x1ffe750` (2007 B, 71 bb) |
| **Custom-call target** | `"AwsNeuronCustomNativeKernel"` @ `0x2521b1`; kernel_config `{"kernel_name":"SelectAndScatter"}` |
| **Public NKI kernel** | `vision.select_and_scatter_kernel` (`nki/kernels/vision.py:17`) |
| **Lowered NKI kernel** | `private_nkl/select_and_scatter.py:7` (beta3 trace form) |
| **Pool enum** | `libBIR` `PoolFunctionType2string` @ `0x4010e0` — exactly `{0 Max, 1 Avg}` (no Sum) |
| **HW argmax route (unused)** | `InstMaxIndex` (IT89) → DVE — returns up to 8 indices ([1.09 pool-engine](../arch/pool-engine.md)) |

---

## The dispatch — three sinks, in priority order

A windowed reduction does **not** have a single lowering. There are three distinct sinks, tried in order; which one fires depends on a native-kernel flag, the op's reduce body, and (for `select_and_scatter`) whether the window matches the one hard-wired shape.

```text
  mhlo.reduce_window / mhlo.select_and_scatter
        │
        ├─(1) native NKI custom-kernel pattern-match ───► custom_call
        │      flag enable-native-kernel-{select-and-scatter,resize-nearest,attention}
        │      target="AwsNeuronCustomNativeKernel" → NKI select_and_scatter_kernel (§ The kernel)
        │
        ├─(2) MhloToPythonPrinter generic emit ─────────► NeuronTensorOp(window_shape, stride,
        │      print<ReduceWindowOp> @0x20d5ff0           padding, reduce_function=<numpy name>)
        │
        └─(3) XLA stock expander (no native kernel) ────► primitive HLO
               SelectAndScatterExpander / ReduceWindowRewriter
               select_and_scatter → ReduceWindow + Scatter + Select
```

### Native-kernel flags

The native-kernel route is gated by `cl::opt` globals, each with help text in `.rodata` (CONFIRMED):

| Flag | Help / role | Source anchor |
|---|---|---|
| `enable-native-kernel-select-and-scatter` | "Enable hlo pattern match for select-and-scatter native kernel" | string `@0x3a9b80`, flag `@0x361258` |
| `enable-native-kernel-resize-nearest` | resize-nearest native kernel match | `@0x2f6888` |
| `enable-native-kernel-attention[-bf16]` | attention pattern match | `@0x348ca0` / `@0x33c208` |
| `enable-native-kernel` | master toggle | `@0x259fb3` |
| `native-kernel-cast-type` / `native-kernel-auto-cast` | dtype-cast controls (`SetNativeKernelCastType` @ `0x1f93060`) | — |

When a pattern matches **and** its flag is on, the op is replaced by an HLO `custom_call` with target `"AwsNeuronCustomNativeKernel"` (`@0x2521b1`); `hlo_utils::isCustomCallWithNativeKernel` @ `0x1f986f0` detects it downstream. `xla::LowerToCustomNativeKernel::Run` @ `0x1ffe750` walks `entry_computation->MakeInstructionPostOrder()` and delegates to the per-pattern lowerer. If the pass is on but no kernel flag is set it logs `"LowerToCustomNativeKernel pass is enabled but none of the kernels is enabled."` (`@0x2d4b60`).

> **NOTE — native-kernel selection is split across two passes.** The HLO-level `LowerToCustomNativeKernel` is one site; the MLIR-level `CanonicalizeForTensorizer` is the other (C09's `lowerResizeNearestGrad` lives there — [4.39](../hlo-opt/canonicalize-for-tensorizer.md)). Both end in either a `custom_call` or a generic `mhlo.reduce_window` the printer can emit. The select-and-scatter / resize matcher *bodies* inside `LowerToCustomNativeKernel.cc` are not individually decompiled (`Run @0x1ffe750` is decompile-skipped; only the attention callee `lowerMMSoftmaxMMToAttentionKernel @0x1ffe0d0` is directly visible). The flag-gated dispatch and the `custom_call` target are CONFIRMED from strings; the exact match predicate is INFERRED from the kernel's own asserts (below). MEDIUM.

### The generic XLA expanders (fallback)

When no native kernel matches (flag off, or window/shape unsupported), the stock XLA expanders run pre-printer:

- **`SelectAndScatterExpander::ExpandInstruction`** (string `"select_and_scatter_expander"` @ `0x2567ad`, `".reduce_window"` @ `0x2269cd`) decomposes `select_and_scatter` into primitive HLO — a `ReduceWindow` (to recompute the per-window select), plus `Scatter` and `Select`. This is *the same math* as the NKI kernel below, but as stock ops; used whenever the hard-wired `(3,3)/(2,2)/(1,1)`, `C=64`, `select=GT`, `scatter=add` shape is not matched.
- **`ReduceWindowRewriter::ReplaceReduceWindowWithReshape`** (flag `"reduce-window-rewriter"`, run-to-fixpoint via `HloPassFix<…,25>`; strings `"Converting R1 reduce window: "` / `"R2 resulting reduce window: "`) canonicalises rank-1 reduce-windows and reshapes R2 windows. Standard XLA, not Neuron-specific.
- **`RewriteDynamicSelectAndScatterSamePadding`** handles the dynamic-shape SAME-padding case.

---

## The generic `reduce_window` emit

When `reduce_window` is not matched to a native kernel, `MhloToPythonPrinter::print<ReduceWindowOp>` @ `0x20d5ff0` re-emits it as one line of NKI/penguin Python. It reads the **five** window attributes directly off the op, each an `llvm::DenseElementsAttr` of `i64` copied into a `SmallVector<long,6>`:

| Attribute getter | Becomes kwarg | Notes |
|---|---|---|
| `ReduceWindowOp::getWindowDimensions()` | `window_shape =` | window extent per spatial dim |
| `ReduceWindowOp::getWindowStrides()` | `stride =` | window step |
| `ReduceWindowOp::getPadding()` | `padding =` | `Nx2` (low,high) pairs |
| `ReduceWindowOp::getBaseDilations()` | (read; verifier-checked) | must be 1 — see dilation gate |
| `ReduceWindowOp::getWindowDilations()` | (read; verifier-checked) | must be 1 — see dilation gate |

The op materialises as a `NeuronTensorOp` / `xla_op` Python call (strings `"NeuronTensorOp"`, `"xla_op"`, `"numpy"` all referenced) with an extra `use_init_operand=<bool>` kwarg (whether to thread the HLO init scalar). There is no special `kernel_config`; it is a plain windowed-reduce tensor op.

### The reduce body → numpy name

The reduce-window region (an `mhlo` block with one binary op + a return) is decoded by `extractReduceFunction(Operation*)` @ `0x20afca0` → `extractReduceFunctionBlock(Block&)` @ `0x20afbe0` → `getReduceOpStrFromOperation`, which maps the inner op to a numpy reduce-op name:

| `mhlo` inner op | numpy `reduce_function` | Pool meaning |
|---|---|---|
| `mhlo.maximum` | `maximum` | **max-pool** |
| `mhlo.minimum` | `minimum` | min-pool (reduce engine) |
| `mhlo.multiply` | `multiply` | product-pool (reduce engine) |
| `mhlo.add` | `add` | **sum**; avg = `add` then `÷N` |
| `mhlo.subtract` / `divide` | `subtract` / `divide` | rare |
| `mhlo.{bitwise,logical}_{and,or,xor}` | same names | bit/logical pool |

```c
// extractReduceFunction (@0x20afca0) → getReduceOpStrFromOperation — CONFIRMED (string set)
// The reduce-window body is exactly one binary op over the two block args; this
// returns its numpy name. The printer emits it as reduce_function=<name>.
str getReduceOpStrFromOperation(Operation *innerOp):
    switch (innerOp.kind):
      case mhlo::MaximumOp:  return "maximum";   // → max-pool
      case mhlo::MinimumOp:  return "minimum";
      case mhlo::MultiplyOp: return "multiply";
      case mhlo::SubtractOp: return "subtract";
      case mhlo::DivideOp:   return "divide";
      case mhlo::AndOp:      return "bitwise_and";  // logical_and for i1
      ...                                            // or/xor likewise
      default /* AddOp */:   return "add";        // ADD is the fall-through → sum-pool
```

> **QUIRK — avg-pool is not a single op; it is `add` + `÷N`.** There is no `avgpool` reduction body in HLO and **no `Sum` `PoolFunctionType`** in BIR (`PoolFunctionType2string` @ `0x4010e0` is exactly `{0 Max, 1 Avg}` — [1.09 pool-engine](../arch/pool-engine.md) confirms it independently). Avg is always `reduce_window{body=add}` followed by a *separate* `Div` (or `Mul` by `1/N`); the back-end recognises that `add`+scale pattern and folds the reciprocal `1/N` into the `InstPool(Avg)` encoding at `bundle+0x28` ([2.5 pool-reduce-encoding](../isa/pool-reduce-encoding.md)). A *pure* `add`-window with no following scale has no Pool function — it lowers to `InstTensorReduce(add)` on the reduce engine. Exactly the structure C09 builds for `ResizeNearestGrad`: `Div(ReduceWindow_sum(grad), Π scale)` ([4.39](../hlo-opt/canonicalize-for-tensorizer.md)).

> **CORRECTION — where the body→engine choice actually lives.** The printer does *not* pick Pool vs. reduce engine; it only stamps `reduce_function=<numpy name>`. The mapping `body=maximum, undilated, 2-innermost-window → InstPool(Max=0)` and `body=add+×(1/N) → InstPool(Avg=1)` vs. `body∈{min,multiply,pure-add,>2 spatial dims} → InstTensorReduce` happens downstream in the NKI/penguin compiler + KLIR→BIR codegen ([2.5 pool-reduce-encoding](../isa/pool-reduce-encoding.md)). The precise FE heuristic that decides Pool-Avg (when a `Div`/`Mul`-by-`1/N` is fusable) vs. reduce-engine was not traced past `hlo2penguin`. MEDIUM.

### The dilation legality gate

`NeuronHloVerifier` calls `xla::hilo::isValidReduceWindowOp(HloInstruction*)` @ `0x2022dc0`, which invokes `xla::window_util::HasBaseDilation(Window&)` @ `0x93935e0` and `HasWindowDilation(Window&)` @ `0x9393630` and emits `NeuronLogger` errors `NCC_EVRF017` / `NCC_EVRF018` / `NCC_EVRF019` (`@0x2727f5` / `@0x26e4ab` / `@0x255fc3`) when the window carries dilation. The Neuron pooling path supports **only undilated windows** (`base_dilation==1`, `window_dilation==1` per dim). Window dimensions, strides, and padding are fully parameterised. Corroborating `.rodata` strings: `"w.window_dilation() == 1"`, `"valid_num_dims(base_dilation)"`, `"valid_num_dims(window_dilation)"`, `"Window %s has a non-positive window dilation factor."` (CONFIRMED).

> **NOTE — re-verification ceiling.** The two dilation callees `HasBaseDilation` / `HasWindowDilation` and all five `ReduceWindowOp` attribute getters (`getWindowDimensions`/`getWindowStrides`/`getPadding`/`getBaseDilations`/`getWindowDilations`) are CONFIRMED as real exported symbols in the `hlo2penguin` export table. `isValidReduceWindowOp` itself is an internal (non-exported) symbol resolved through the IDA function-address index, not the export table — its address `0x2022dc0` and the `NCC_EVRF0*` string bindings are CONFIRMED at the report's grounding level but were not independently re-isolated from the export json here. STRONG.

---

## `select_and_scatter` — the printer emit & legality

`MhloToPythonPrinter::printSelectAndScatter` @ `0x20de190` (source `hilo/MLIRPasses/Transforms/MhloToPythonPrinter.cc`) emits a `NeuronTensorOp` carrying `kernel_config = '{"kernel_name":"SelectAndScatter"}'` plus the kwargs that fully describe the op:

| kwarg | Meaning |
|---|---|
| `window_shape`, `window_strides`, `window_size`, `padding` | the window geometry (NO dilations — `select_and_scatter` has only these 3 geometric attrs in mhlo) |
| `operand_shape`, `src_shape`, `mask_shape` | the three tensor shapes |
| `select_reduce_name` | the select reduction's numpy name — `"maximum"` when `select=GT`, `"minimum"` when `select=LT` |
| `is_select_first` | the tie-break: pick the **first** matching position (= the iota+min "first argmax" below) |
| `binary_op_name` | the scatter body op (e.g. numpy `add`) |
| `scatter_op_type`, `scatter_ident` | scatter computation kind + identity (`0` for add) |
| `init_val` | reduce-window init for the select (e.g. `-inf` / `0`) |

```c
// printSelectAndScatter (@0x20de190) legality — CONFIRMED (NCC_PYP029..035 strings)
// The select region MUST be a single mhlo.compare over two BlockArguments;
// its ComparisonDirection maps to (select_reduce_name, is_select_first).
void printSelectAndScatter(Operation *op):
    selectRegion = op.getSelect();
    cmp = single_op_of(selectRegion);
    if (!isa<mhlo::CompareOp>(cmp))
        error("SelectAndScatter: requires select to be a compare, but got {0}", cmp);
    if (!isBlockArg(cmp.lhs) || !isBlockArg(cmp.rhs))
        error("expected BlockArgument for cmpOp's lhs and rhs ...");
    // ComparisonDirection → (select_reduce_name, is_select_first). GT→"maximum",
    // LT→"minimum" are CONFIRMED; the exact GE/LE→is_select_first booleans are
    // not isolated in the disasm (INFERRED: the strict directions set is_select_first).
    switch (cmp.getComparisonDirection()):
      case GT /*GE*/: select_reduce_name = "maximum"; is_select_first = true;
      case LT /*LE*/: select_reduce_name = "minimum"; is_select_first = true;
    // padding<window check (printer-side, NCC_PYP):
    //   "Ensure padding values are less than corresponding window dimensions.
    //    Check that padding[%d] = (%d,%d) < window_size[%d] = %d."
    emit NeuronTensorOp(kernel_config='{"kernel_name":"SelectAndScatter"}', ...kwargs);
```

This printer path produces the call that, in the FE, dispatches to the NKI `select_and_scatter_kernel` — i.e. the kernel below is the *body* of this named kernel.

---

## The kernel — `select_and_scatter_kernel` (maxpool backward)

The hand-written NKI kernel is the backward pass of 2-D max-pooling. Given the forward-pool **input** `operand_tensor (N,C,H,W)` and the upstream **gradient** `source_tensor (N,C,sH,sW)` (one value per pooled window), it produces the input-space gradient `out_tensor (N,C,H,W)` — each window's gradient routed to the position that held that window's maximum. It exists in two textually-identical-algorithm forms:

- **`vision.select_and_scatter_kernel`** (`nki/kernels/vision.py:17`) — the readable `@nki.jit` source, `nl.*` high-level ops.
- **`private_nkl/select_and_scatter.py:7`** — the beta3-FE traced/lowered form, `nisa.*` intrinsics, double-buffered, engine-balanced. Same math.

### The hard-wired window and its asserts

The kernel implements **exactly one shape** and asserts every parameter (CONFIRMED, `vision.py:42–54`):

```c
sw_h, sw_w   = (3, 3)       // window dimensions
stride_h, _w = (2, 2)       // window strides  (stride 2 < window 3 → OVERLAP)
padding      = (1, 1)       // zero-pad border
init_value   = 0;  select = greater-than;  scatter = add
assert (padded_h - sw_h)//stride_h + 1 == src_h    // window count matches
assert (padded_w - sw_w)//stride_w + 1 == src_w
assert H == W and src_h == src_w
assert C == 64 and N % 2 == 0    // two batches packed into P=128 partitions
```

> **GOTCHA — the kernel is single-shape; everything else falls back.** Any `reduce_window`-shaped `select_and_scatter` whose window ≠ `(3,3)`, stride ≠ `(2,2)`, pad ≠ `(1,1)`, `C ≠ 64`, `select ≠ GT`, or `scatter ≠ add` is *not* handled by this kernel — it falls to `SelectAndScatterExpander` (stock HLO `ReduceWindow + Scatter + Select`). The native-kernel matcher's accept predicate before delegating to this kernel is INFERRED from these asserts, not read from the matcher disasm. MEDIUM.

### Partition / tile structure

`p = 128` partitions. The outer loop `ib` walks `N//2` batch-**pairs**; two `(n,c)` planes (each `C==64`) are packed into partitions `[0:64]` and `[64:128]`, so one 128-partition tile holds two batches. `operand_local` is a padded SBUF tensor `(p, padded_h, padded_w)` pre-filled with `finfo(dtype).min` (the `-inf` init for max; the lowered form uses the literal `-3.4028235e38` = fp32 `-max`), with real data loaded into the interior `[1:H+1, 1:W+1]` — the `(1,1)` pad border stays at `-inf` so it can never win a max.

### The six-step algorithm

```c
// vision.select_and_scatter_kernel (vision.py:60–163) — CONFIRMED (full read)
for ib in affine_range(N // 2):                         // batch-pair
  operand_local = full((128, padded_h, padded_w), finfo(dtype).min)   // -inf init
  load operand interior [1:H+1, 1:W+1] for both batches into [0:64],[64:128]

  for ik in affine_range(src_h):                        // one OUTPUT row of windows
    // Step 0 — sliding-window gather (mgrid): stride-2 windows
    operand_local_sw[p, src_w, sw_h, sw_w] =
        copy(operand_local[p, 2*ik + win.y, 2*win.x + win.z])

    // Step 1 — per-window max (the "select" reduction VALUE; positions discarded)
    max_val[p, src_w, 1, 1] = max(operand_local_sw, axis=[2,3], keepdims=True)

    // Step 2 — equal-mask: 1 at every position whose value == window max (ties → >1)
    _win_idx_mask[p, src_w, sw_h, sw_w] = equal(max_val, operand_local_sw)   // int8

    // Step 3 — tie-break to the FIRST argmax (reproduces XLA row-major "first wins")
    linear_indices = iota(3*win.y + win.z)              // 0..8 row-major within window
    selecting     = where(_win_idx_mask, linear_indices, 9 /*=sw_h*sw_w, sentinel*/)
    selected      = min(selecting, axis=[2,3], keepdims=True)   // smallest idx = first max

    // Step 4 — one-hot mask: exactly ONE 1 per window, at the chosen argmax slot
    win_idx_mask_local[:, ik, :, :, :] = equal(selected, linear_indices)     // uint8

  load source_local (the gradient) packed 2-batch like operand

  // Step 5 — SCATTER: 9 separate masked-multiply passes, one per (iq,ih) intra-window offset
  vals_nonlocal = ndarray((sw_h, sw_w, p, H, W), buffer=private_hbm)
  for iq in sw_h: for ih in sw_w:
      vals_local[p, 2*tx+iq, 2*ty+ih] = source_local * win_idx_mask_local[...,iq,ih]
      store vals_nonlocal[iq, ih] = vals_local interior     // each pass writes a
                                                            // NON-overlapping stride-2 sublattice
  // Step 6 — scatter-ADD across overlapping windows: 9-way reduce-add
  out_local = 0
  for iq in sw_h: for ih in sw_w:
      out_local += load(vals_nonlocal[iq, ih])
  store out_tensor interior = out_local
```

### Why the argmax must be recomputed

This is the crux, and it is forced by the hardware. The Pool engine's `InstPool(Max)` returns only the maximum **value**, not its position — `PoolFunctionType` has no argmax member ([1.09 pool-engine](../arch/pool-engine.md), `PoolFunctionType2string` @ `0x4010e0` = `{0 Max, 1 Avg}`). Max-pool backward needs the *position* that won each window, so the kernel cannot pool; it reconstructs the argmax on the reduce/ALU/DVE engines:

> **QUIRK — the argmax is recomputed because `InstPool(Max)` discards positions.** The four-op chain `{reduce-max → equal-mask → iota-select → reduce-min}` (Steps 1–3) re-derives, for each window, the linear index of the *first* position equal to the max. Step 1's reduce-max gives the value; Step 2's `equal` re-finds which positions held it (possibly several, on ties); Step 3 maps those positions to their row-major linear index via `iota`, sends non-max positions to a large sentinel, and `min`-reduces to the smallest surviving index — i.e. XLA's "first in row-major wins" tie-break. There is no shortcut on the Pool engine; the argmax is a *reduce-engine recompute* of information the forward pool threw away. (The `iota`/`min` dance exists solely to reproduce the tie-break exactly.)

> **NOTE (INFERRED) — there is a HW-native argmax route this kernel does not take.** The Pool-alias DVE op `InstMaxIndex` (IT89, `max8`) can return up to 8 argmax indices ([1.09 pool-engine](../arch/pool-engine.md)). The HW-native route would be `InstMaxIndex` directly; this kernel instead uses the reduce-engine `{max,equal,iota,min}` recompute. The most likely reason is engine balancing — keeping the reduce/ALU/DVE engines busy on the argmax while the Pool engine is free — and that `max8`'s 8-index cap does not cleanly cover a 9-element window's tie semantics. The recompute is the engine-balanced *software* route; `InstMaxIndex` would be the *hardware* route. INFERRED.

### Why the scatter must accumulate (overlap)

Window `(3,3)` with stride `(2,2)` means adjacent windows **overlap by 1**: one input pixel can be the argmax of up to 4 windows, so its gradient is the **sum** of those windows' source values (`scatter computation = add`). A naïve scatter-write would lose all but the last contribution. The kernel realises the accumulate by splitting the scatter into 9 passes, one per intra-window offset `(iq,ih)`:

> **GOTCHA — overlap forces 9 non-overlapping passes + a 9-way reduce-add.** Each `(iq,ih)` pass writes a *stride-2 sublattice* of the output that, within that pass, never self-overlaps (every window contributes its `(iq,ih)` slot to a distinct output pixel). The masked multiply `source_local × win_idx_mask_local[...,iq,ih]` zeroes every non-argmax slot, so the pass deposits each window's gradient at exactly the position whose `(iq,ih)` slot won. Summing all 9 passes (Step 6) accumulates the up-to-4 windows that share any one output pixel. This overlap handling — `stride < window` → scatter-add — is the genuinely new machinery vs. C09's `ResizeNearestGrad`, whose `stride == dim` makes windows non-overlapping and needs no accumulate ([4.39](../hlo-opt/canonicalize-for-tensorizer.md)).

---

## The lowered form — `private_nkl/select_and_scatter.py`

The beta3 FE traces to the same algorithm emitted with low-level `nisa.*` intrinsics, double-buffering, and explicit engine assignment — the form the front-end actually traces to penguin.ir. Signature `select_and_scatter_kernel(a_ptr, b_ptr)` (`a`=operand=fwd input, `b`=source=upstream grad). The intrinsic-to-step mapping (CONFIRMED, full read of `select_and_scatter.py`):

| `nisa.*` intrinsic | BIR op / engine | Step / role |
|---|---|---|
| `nisa.memset` | `InstMemset` | init `op_local` to `-3.4028235e38` (`-inf`); the `(1,1)` pad border stays `-inf` |
| `nisa.dma_copy` | DMA | load operand interior, load source, store result |
| `nisa.activation(copy)` | `InstActivation` (**Act** engine) | Step 0 sliding-window gather — *"Use Activation for tensor copy for better engine balancing"* (line 81) keeps the reduce engine free |
| `nisa.tensor_reduce(max)` | `InstTensorReduce`, free-axis `[2,3]` | Step 1 per-window max |
| `nisa.tensor_tensor(eq)` | `InstTensorTensor` ALU `op=equal` | Steps 2 + 4 (the masks) |
| `nisa.select_reduce` | DVE select (Vector engine) | Step 3 fused `where(mask, iota, 255)` — used as element-wise SELECT, no reduction here |
| `nisa.tensor_reduce(min)` | `InstTensorReduce`, free-axis `[2,3]` | Step 3 finish — first argmax |
| `nisa.tensor_tensor(mul)` | `InstTensorTensor` `op=multiply` | Step 5 grad × one-hot |
| `nisa.tensor_copy` | copy | Step 5 spill prep (padded interior → compact) |
| `nisa.tensor_tensor(add)` | `InstTensorTensor` `op=add` | Step 6 accumulate overlapping-window contributions |

```c
// private_nkl/select_and_scatter.py — LICM hoist (lines 47–52) — CONFIRMED
_win_indices = iota(pattern=[[0,src_w],[sw_w,sw_h],[1,sw_w]], offset=0)   // uint8 0..8,
              // col axis broadcast over src_w; = vision.py's `3*y+z`, precomputed once
_win_indices_max = memset(255)        // sentinel tensor (see QUIRK — dead in this body)
```

> **QUIRK — the materialized `255` sentinel tensor is dead; the literal won.** `private_nkl` LICM-hoists `_win_indices_max = memset(255)` out of all loops (lines 51–52), but the Step-3 `select_reduce` actually passes the **immediate** `on_false=255.0` (line 137), never reading `_win_indices_max`. The hoisted tensor is unused — an artifact of the lowering preferring an inline literal to a materialized broadcast for the "lose the min" sentinel. The public `vision.py` Step 3 uses `sw_h*sw_w = 9` as its sentinel (`where(..., 9)`); the lowered form uses `255` (uint8 max) for the same "non-max position can never win the `min`" role. Same math, different sentinel magnitude. CONFIRMED.

> **NOTE — the lowered scatter stays in SBUF.** Public `vision.py` spills the 9 scatter passes to `private_hbm` (`vals_nonlocal`) between Step 5 and Step 6, then loads and adds. The lowered `private_nkl` form keeps `vals_interm` in SBUF and does `out_local += vals_interm` in place (lines 195–206) — same arithmetic, one fewer HBM round-trip. CONFIRMED.

### Access-pattern encoding

The `.ap(pattern=[[step,num],…], offset=…)` is a list of `[stride, count]` pairs, innermost **last**, first pair = the partition axis (`P=128`). The Step-1 reduce reads `op_local_sw` with `pattern=[[2*src_w*sw_w*sw_h, P], [sw_w*sw_h, src_w], [sw_w, sw_h], [1, sw_w]]` and `axis=[2,3]` — four nested loops (partition, output-col, window-row, window-col), folding the inner two into the windowed max. This is exactly the windowed AP a Pool op would carry implicitly, but materialised on the **reduce** engine because the position (argmax) is needed downstream and `InstPool(Max)` discards it.

The double-buffer (the leading length-`2` axis indexed at `offset … *(ik%2)`) lets iteration `ik` and `ik+1` use disjoint SBUF so the scheduler can pipeline the reduce chain across output rows; it is a perf-lowering artifact absent from the public source.

---

## How this generalises C09's `ResizeNearestGrad`

C09 reversed `CanonicalizeForTensorizer::lowerResizeNearestGrad` @ `0x207df40` ([4.39](../hlo-opt/canonicalize-for-tensorizer.md)), which handles only `custom_call{target="ResizeNearestGrad"}`. It is the **narrowest** instance of the machinery on this page:

| C09 narrow (`ResizeNearestGrad`) | AA06 general (`reduce_window` / `select_and_scatter`) |
|---|---|
| `window_dimensions = scale[]` (upsample factor) | arbitrary window (printer reads `getWindowDimensions`) |
| `window_strides = scale[]` (== dims; **non-overlapping**) | arbitrary stride (kernel uses `2 < 3` → **overlapping**, needs scatter-add) |
| `padding = 0` | arbitrary (kernel uses `(1,1)`) |
| base/window dilation = 1 | must be 1 (`isValidReduceWindowOp` rejects dilation) |
| reduction body = `add` (hard-wired) | any numpy body: `maximum`/`minimum`/`add`/… |
| then `÷ Π scale[i]` (avg-pool) | avg = `add`+`Div`; max = pure max-pool; etc. |
| `mhlo::ReduceWindowOp::build` @ `0x8f98cb0`, 2-arg Add region | **same builder**; AA06 is the general op C09 emits |
| forward only (grad given) | `reduce_window` = FWD pool; `select_and_scatter` = BWD scatter |

Key points: (1) C09 is a *canonicalizer* — it recognises the resize-grad custom-call and rewrites it to a generic `mhlo.reduce_window(Add) + Div`; it never touches the Pool engine itself. (2) AA06 is the rest of the chain — that general `reduce_window` is then either native-matched (§ dispatch) or generic-printed (§ generic emit). (3) `ResizeNearestGrad`'s `stride==dim` makes windows non-overlapping, so it never needs the scatter-accumulate that `select_and_scatter`'s `stride<dim` requires — *that overlap handling is the new machinery here*. (4) Both bottom out the same way: a windowed reduction whose body name + window geometry drives the downstream choice of `InstPool(Max/Avg)` vs. `InstTensorReduce`; **there is no Sum-pool function type; avg = sum + `1/N` folded at BIR encode.**

---

## Path summary

| HLO op | Sink | Engine ([1.09](../arch/pool-engine.md)) |
|---|---|---|
| `reduce_window{max}`, undilated | printer `NeuronTensorOp(window…)` | `InstPool(Max)` `0x45` |
| `reduce_window{add}` + `÷N` | printer + `Div` (avg-pool) | `InstPool(Avg)` `0x45`, `1/N`@`+0x28` |
| `reduce_window{add}` pure (no scale) | printer `NeuronTensorOp` | `InstTensorReduce` (no Sum pool) |
| `reduce_window{min/mul/…}` | printer `NeuronTensorOp` | `InstTensorReduce` |
| `reduce_window` w/ dilation | **REJECTED** (`NCC_EVRF017/018/019`) | n/a |
| `select_and_scatter` (matched shape) | native kernel `custom_call` | NKI kernel (reduce+TT+select, §§ The kernel) |
| `select_and_scatter` (other) | `SelectAndScatterExpander` (HLO) | `ReduceWindow + Scatter + Select` |
| `ResizeNearestGrad` | C09 → `reduce_window{add}`+`Div` | avg-pool ([4.39](../hlo-opt/canonicalize-for-tensorizer.md)) |

## Cross-References

- [Conv Device-Lowering & Variant Selection](conv-device-lowering.md) — the sibling 6.8.1 selector; conv and pooling share the windowed-AP and the native-kernel dispatch surface.
- [Resize Kernels](resize-kernels.md) *(planned, 6.8.6)* — `ResizeNearestGrad`'s host path emits the `reduce_window(add)+Div` avg-pool this page documents as the narrow case.
- [Pool Engine — Windowed Pooling and the Reduce Leg](../arch/pool-engine.md) — the `PoolFunctionType = {Max, Avg}` enum (no Sum), `InstMaxIndex` argmax route, and the Pool-vs-reduce engine fork the back-end applies.
- [Pool / TensorReduce / Reciprocal / Iota Encoding](../isa/pool-reduce-encoding.md) — `InstPool` opcode `0x45`, the `1/N`-reciprocal avg trick, and the `iota` encoding Step 3 relies on.
- [CanonicalizeForTensorizer Rewriters](../hlo-opt/canonicalize-for-tensorizer.md) — `lowerResizeNearestGrad`, the narrowest instance of this machinery.
- [MhloToPythonPrinter — Heavy, Collective & Fusion/Reduce Emitters](../hlo-opt/mhlo-to-python-printer-heavy.md) — the printer driver that owns `print<ReduceWindowOp>` and `printSelectAndScatter`.
- [nki.isa REDUCE / SELECT / DVE / MEMORY / DMA Intrinsics](isa-reduce-dve-dma.md) — `select_reduce`, `tensor_reduce`, and `iota`, the intrinsics the lowered kernel uses.
