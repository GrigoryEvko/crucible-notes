# Conv Canonicalization

> *All addresses on this page apply to neuronx_cc `2.24.5133.0+58f8de22`, binary `hlo2penguin` (cp310). `.rodata` VA−0x200000, `.text` VA−0x201000. Other versions will differ.*

## Abstract

`CanonicalizeConv` is one of the hlo2penguin MLIR conv-canonicalization passes that
normalizes `mhlo.convolution` before the conv device-lowering selects a kernel
([6.8.1](../nki/conv-device-lowering.md)). It is a **func-level**
`OperationPass<func::FuncOp>` (RTTI-confirmed via the `getName`/`clonePass`
wrapper) registered under the pass argument `canonicalize-conv`. A byte-identical
twin, `StableHLOCanonicalizeConv` (`stablehlo-canonicalize-conv`), runs the same
algorithm over the StableHLO dialect.

The pass does exactly **two** rewrites — it makes stride and padding *explicit* so
the backend only ever sees **zero-padding, unit-stride** convolutions:

- **`explicitlyPad`** folds the conv's window `padding` into a standalone `mhlo.pad`
  on the LHS input, then rebuilds the conv with `padding = 0`.
- **`explicitlyStride`** folds each non-unit `window_stride` into a strided
  `mhlo.slice` of the LHS input, then rebuilds the conv with `window_strides = 1`.

That is the whole transform. The most important thing a reimplementer must know is
what it does **not** do: there is no dimension-number permute, no `mhlo.transpose`
insertion, no conv→dot rewrite, and no conv→im2col-matmul rewrite anywhere in this
pass (see [§7](#7-what-this-pass-does-not-do)). Dimension-numbers,
`feature_group_count`, and `batch_group_count` are read only to *locate* the
spatial axes, and are copied verbatim onto the rebuilt conv. The "canonical form"
this pass produces is therefore: **same dim-numbers, same layout, same group
counts — but padding and stride hoisted out of the conv into preceding `pad`/`slice`
ops.** Layout normalization to a fixed order (NHWC etc.) happens elsewhere, not here.

For reimplementation, the contract is:

- The func-level two-phase driver: collect all eligible `ConvolutionOp`s, then
  rewrite each (pad-first, stride-second).
- The eligibility gate `canCanonicalize`: rank-4 (2-D spatial) convs with small
  spatial parameters only; everything else passes through untouched.
- The two rewrites' exact rebuilt-conv signature, including the verbatim
  copy-through of dim-numbers and both group counts.
- The MHLO ↔ StableHLO split: same algorithm, different dialect ops, **different
  source file** and a different `PadOp::build` signature.

| | |
|---|---|
| **MHLO factory** | `mlir::createCanonicalizeConvPass` @0x2077c60 |
| **StableHLO factory** | `mlir::createStableHLOCanonicalizeConvPass` @0x2119950 |
| **Pass args** | `canonicalize-conv` / `stablehlo-canonicalize-conv` |
| **Pass kind** | `PassWrapper<…, OperationPass<func::FuncOp>>` (func-level) |
| **MHLO driver** | `CanonicalizeConv::runOnOperation` @0x207c270 (653 B) |
| **StableHLO driver** | `StableHLOCanonicalizeConv::runOnOperation` @0x211dcd0 |
| **Source files** | `hilo/MLIRPasses/Transforms/CanonicalizeConv.cc` @0x2f66a0 · `…/StableHLOCanonicalizeConv.cc` @0x348ed8 |
| **IR level** | MHLO / StableHLO (pre-Penguin) |
| **Error code** | `NCC_EUET001` @0x226305 (pad-path unsupported element type) |

---

## 1. Driver — `runOnOperation`

### Purpose

Walk the function, collect every eligible convolution, then rewrite each.
Collect-then-rewrite avoids mutating the IR while the walk is in flight.

### Entry Point

```text
CanonicalizeConv::runOnOperation @0x207c270 (653 B)
  ├─ mlir::detail::walk(funcOp, callback=0x2077710, PreOrder)   ── collect
  │    └─ canCanonicalize @0x20774d0                            ── gate
  ├─ explicitlyPad   @0x207a7a0                                 ── phase 2: pad
  ├─ explicitlyStride @0x2079890                               ── phase 2: stride
  └─ isBackwardConv  @0x20777c0                                ── phase 2b (VLOG-gated)
```

### Algorithm

```c
function runOnOperation(this):                 // sub_207C270
    funcOp = this->[+0x28] & ~7                 // the func::FuncOp under processing
    SmallVector<Operation*,6> convs             // inline storage, cap 6

    // PHASE 1 — walk + collect
    walk(funcOp, callback=0x2077710, PreOrder)  // collect lambda below

    // PHASE 2 — rewrite each collected conv, PAD FIRST then STRIDE
    for op in convs:
        newOp = explicitlyPad(op)               // @0x207c393 — returns rebuilt conv
        explicitlyStride(newOp)                 // @0x207c39b

    // PHASE 2b — diagnostic re-pad, gated by VLOG flag qword_9C714B8
    if qword_9C714B8:                           // byte read just before the loop
        re-walk; for each remaining ConvolutionOp whose producer chain
        hits another ConvolutionOp:
            if isBackwardConv(op): explicitlyPad(op)   // @0x207c446

function collect_callback(payload, Operation* op):     // sub_2077710 (169 B)
    if op->[+0x30]->[+0x10] == TypeID<void>:               return   // term/null
    if op->[+0x30]->[+0x10] != TypeID<mhlo::ConvolutionOp>: return   // not a conv
    if !canCanonicalize(op):                                return
    (*payload).push_back(op)                                          // SmallVector
```

> **QUIRK —** the order is fixed: `explicitlyPad` runs **before** `explicitlyStride`.
> The stride rewrite's slice math (§4b) assumes the conv it rebuilds already has
> explicit, zeroed padding. Reordering the two silently produces wrong slice
> bounds when the input padding is non-zero.

> **NOTE —** phase 2b is purely diagnostic — it is gated on the BSS VLOG flag
> `qword_9C714B8` and uses `isBackwardConv` only to decide whether to emit a
> re-pad for logging. It does **not** select a different lowering and has no effect
> in a normal (non-verbose) build.

---

## 2. Eligibility Gate — `canCanonicalize`

### Purpose

Accept only convolutions that are worth materializing explicitly: rank-4 (2-D
spatial) convs with small spatial parameters. Everything else returns to the
backend untouched.

### Algorithm

```c
function canCanonicalize(conv):                     // sub_20774D0 (576 B)
    if conv op-state byte [op+0x2E] >= 0: ud2        // sanity: op must be a valid conv
    cp = buildMhloConvParams(conv, conv.operand0, conv.operand1)   // @0x2077510 → §5
    accept = false
    if cp.rank_lhs == 4 && cp.rank_rhs == 4:         // var_248 / var_208 — both rank-4
        for v in cp.<window dims>: if v != 2: reject  // 2-D spatial expected
        d0 = cp.dims[ inputSpatialDim0 ]
        if d0 <= 1:      reject
        elif d0 <= 3:    accept = qword_9C71578       // small-kernel feature flag
        d1 = cp.dims[ inputSpatialDim1 ]
        if d1 <= 1:      reject
        elif d1 <= 3:    accept = qword_9C71578
        accept = ( cp.<stride/pad>[idx] <= 31 )       // upper bound 31 on one component
    free cp (ConvParams dtor inline)
    return accept
```

Plain-language gate: **rank-4 convolutions only**, with spatial extents `> 1`, a
feature-flag escape (`qword_9C71578`) for size-2/3 kernels, and an upper bound of
31 on one stride/pad component. Confidence is HIGH on the rank-4 + small-spatial
intent; MEDIUM on the exact `var_*` field → semantic mapping, because `ConvParams`
is a large struct whose vectors are witnessed only by destructor order (§5).

> **GOTCHA —** the gate builds a full `ConvParams` (every conv attribute + both
> operand shapes) on *every* candidate, just to read a handful of fields. A naive
> reimplementation that skips the build and reads attributes directly is faster but
> must reproduce the rank-4 and small-spatial cutoffs exactly, or it will canonicalize
> convs the device-lowering expects to receive un-normalized.

---

## 3. Rewrite — `explicitlyPad`

### Purpose

Hoist the conv's window `padding` out into a standalone `mhlo.pad` on the LHS
input, then rebuild the conv with zeroed padding.

### Algorithm

```c
function explicitlyPad(convOp) -> Operation*:        // sub_207A7A0 (6858 B)
    dn  = convOp.getDimensionNumbers()                // @0x8f77bc0
    fgc = convOp.getFeatureGroupCount()               // @0x8f77be0 — copied through
    bgc = convOp.getBatchGroupCount()                 // @0x8f77cf0 — copied through
    elemTy = input.getElementType()
    // zero pad-value constant:
    zero = OpBuilder::create<mhlo::ConstantOp>(loc, scalarTy(elemTy),
               DenseElementsAttr(0))                   // @0x2077e00
    // explicit pad of the LHS input with the conv's spatial padding:
    padded = mhlo::PadOp::build(b, st, paddedTy, input, zero,           // @0x8f7f470
               edge_padding_low  = DenseIntElementsAttr,  // conv pad-low  on spatial dims
               edge_padding_high = DenseIntElementsAttr,  // conv pad-high on spatial dims
               interior_padding  = DenseIntElementsAttr(0))  // always 0 — no dilation here
    // rebuild conv over the padded input with ZERO padding:
    newConv = OpBuilder::create<mhlo::ConvolutionOp>(loc, convOp.getType(),  // @0x2078070
               /*lhs*/ padded, /*rhs*/ kernel,
               /*window_strides*/ sameAttr,
               /*padding*/        DenseIntElementsAttr(0),    // << zeroed
               /*lhs_dilation*/   sameAttr,
               /*rhs_dilation*/   sameAttr,
               /*window_reversal*/ DenseElementsAttr,
               dn, fgc, bgc, precision_config)               // dim-numbers + groups VERBATIM
    convOp.erase()                                            // @0x9b59040
    return newConv
```

`OpBuilder::create<mhlo::ConvolutionOp>` (@0x2078070, demangle-confirmed) carries:
`RankedTensorType`, 2× `TypedValue<RankedTensorType>`, **4× `DenseIntElementsAttr`**
(strides, padding, lhs_dilation, rhs_dilation), `DenseElementsAttr` (window_reversal),
`ConvDimensionNumbersAttr`, **2× `unsigned long`** (feature_group_count,
batch_group_count), and `ArrayAttr` (precision_config). The two group counts are
plain integers copied straight through — they are never restructured.

> **NOTE —** the error path runs `hilo::formatErrorMessage<>(ErrorCode)` (@0x1ec6fc0)
> → emits **`NCC_EUET001`** (@0x226305) when the input element type is unsupported
> on the pad path. `llvm::report_fatal_error` guards one `RegisteredOperationName`
> lookup.

---

## 4. Rewrite — `explicitlyStride`

### Purpose

For each non-unit window stride, fold the stride into a strided `mhlo.slice` of the
(already-padded) LHS input, then rebuild the conv at unit stride.

### Algorithm

```c
function explicitlyStride(convOp) -> void:           // sub_2079890 (3675 B)
    ctx = attr.getContext()
    inShape = cast<RankedTensorType>(input).getShape()
    dn      = convOp.getDimensionNumbers()
    i64     = IntegerType::get(ctx, 64, Signless)     // stride-attr element type

    emit = lambda(spatialDim, strideVal):             // sub_20782A0 (5604 B), per spatial dim
        sliced = mhlo::SliceOp::build(b, st, slicedTy, curInput,        // @0x8f83dd0
                   start_indices = DenseIntElementsAttr(0…),
                   limit_indices = DenseIntElementsAttr(full extent),
                   strides       = DenseIntElementsAttr(stride on dim, 1 elsewhere))
        newConv = OpBuilder::create<mhlo::ConvolutionOp>(loc, …,        // @0x2078070
                   /*lhs*/ sliced, /*rhs*/ kernel,
                   /*window_strides*/ DenseIntElementsAttr(1),   // << unit stride
                   /*padding*/        0,                         // already zero from explicitlyPad
                   lhs_dil, rhs_dil,
                   /*window_reversal*/ BoolAttr::get(ctx, b),    // @0x9b4ddb0
                   dn, fgc, bgc, precision_config)
        set inherent attrs; erase old; return newConv

    // iterate window_strides (DenseElementsAttr::getRawIntOrFloat @0x9afddd0 / isSplat);
    // for any stride != 1, invoke emit(dim, stride).
    // mhlo::AddOp::build (@0x8f704b0) composes multi-dim slice-bound index arithmetic.
```

Net algebra:

```text
conv(x, w, window_strides=s, padding=0)
  →  conv( slice(x, strides=s), w, window_strides=1, padding=0 )
```

The stride is pushed out of the conv into the slice — the classic explicit-stride
canonicalization. The rebuilt conv reuses the original result type, so the output
shape is preserved.

`mhlo::SliceOp::build` (@0x8f83dd0) takes **3× `DenseIntElementsAttr`** =
start / limit / **stride**; the third is where the folded window stride lands.

---

## 5. `hilo::ConvParams` and `buildMhloConvParams`

`buildMhloConvParams(ConvolutionOp&, lhs, rhs)` (@0x21cd050, 5455 B) is the single
shared attribute/shape extractor — its only caller is `canCanonicalize`. It reads
every conv attribute and both operand shapes into a `hilo::ConvParams` (~0x200 bytes).

Accessors called (all demangle-confirmed in the callee graph):

| Group | Symbols |
|---|---|
| Dim-numbers | `getDimensionNumbers` (@0x8f77bc0) → all ten dim accessors: `getInput{Batch,Feature,Spatial}Dimension(s)`, `getKernel{Input,Output}FeatureDimension`, `getKernelSpatialDimensions`, `getOutput{Batch,Feature,Spatial}Dimension(s)` |
| Window attrs | `getWindowStrides` (@0x8f77a30), `getPadding` (@0x8f77a80), `getLhsDilation` (@0x8f77ad0), `getRhsDilation` (@0x8f77b20), `getWindowReversal` (@0x8f77b70) |
| Group counts | `getFeatureGroupCount` (@0x8f77be0), `getBatchGroupCount` (@0x8f77cf0) |
| Shapes | `ShapedType::getShape` (@0x9b21890) — both operands |

Variadic attrs are materialized into vectors via
`llvm::append_range<vector<long>, DenseElementsAttr::ElementIterator<long>>`
(@0x21cc870, strides/pad/dilation), the `unsigned long` and `bool` variants
(@0x21cc640, @0x21ccaa0), and padding pairs into `vector<pair<long,long>>`
(`_M_realloc_insert` @0x1f30f50). A `hilo::Permutation(SmallVector<long,6>)`
(@0x21e4f70) holding the canonical spatial-dim order is stored in `ConvParams`.

> **QUIRK —** the `hilo::Permutation` is the closest this pass comes to a layout
> transform — but it is **only read by the gate** (§2). It is never lowered to an
> `mhlo.transpose`. No dimension-number reordering reaches the IR.

### Struct layout (witnessed by `~ConvParams` @0x21cc510)

The destructor frees 11 owned regions in order; the layout is inferred from that
order plus accessor call sites (field → semantic mapping MEDIUM):

| Offset | Kind | Inferred role |
|---|---|---|
| 0x000 / 0x040 / 0x080 | string / SmallVector<long> | name + first dim vectors |
| 0x0C8 – 0x128 | 5× `vector<T>` (ptr,size,cap) | stride / pad / dilation / window-reversal arrays |
| 0x170 / 0x1B0 / 0x1F0 | 3× `SmallVector<long>` | input / kernel / output shape + spatial-dim vectors + Permutation |

Returned ranks land in stack slots `var_248` / `var_208` (the `== 4` gate, §2).

---

## 6. MHLO ↔ StableHLO Twin

`StableHLOCanonicalizeConv::runOnOperation` @0x211dcd0 is the same control flow as
the MHLO driver — same `SmallVector` collect, same pad-first/stride-second loop,
same `qword_9C714B8` VLOG gate, same `isBackwardConv` diagnostic branch. The deltas
are dialect substitutions:

| Axis | MHLO | StableHLO |
|---|---|---|
| TypeID filter | `mhlo::ConvolutionOp` | `stablehlo::ConvolutionOp` |
| Rewrites emit | `mhlo.{pad,slice,add,constant,convolution}` | `stablehlo.{…}` |
| Param extractor | `buildMhloConvParams` @0x21cd050 | `buildStableHLOConvParams` @0x21cee00 |
| Dim accessors | `mhlo::ConvDimensionNumbersAttr` | `stablehlo::ConvDimensionNumbersAttr` |
| Source file | `CanonicalizeConv.cc` @0x2f66a0 | `StableHLOCanonicalizeConv.cc` @0x348ed8 |
| `PadOp::build` sig | 3× `DenseIntElementsAttr` (@0x8f7f470) | 3× `llvm::ArrayRef<long>` (@0x9159ef0) |

> **CORRECTION (D-C11) —** the backing analysis stated the StableHLO twin shares the
> *same* source file `CanonicalizeConv.cc`. The binary disagrees: the StableHLO pass
> references its own file `hilo/MLIRPasses/Transforms/StableHLOCanonicalizeConv.cc`
> @0x348ed8, and `stablehlo::PadOp::build` takes `llvm::ArrayRef<long>` (@0x9159ef0)
> rather than `DenseIntElementsAttr`. Two distinct translation units, same algorithm.
> The error code `NCC_EUET001` @0x226305 and the gate logic are identical.

StableHLO member set, all demangle-confirmed: `canCanonicalize` @0x21191c0,
`isBackwardConv` @0x21194b0, `explicitlyPad` @0x211c270 (6744 B),
`explicitlyStride` @0x211b600 (2998 B), stride lambda @0x211a140.

---

## 7. What This Pass Does *Not* Do

This is the key correction to the task premise. A reimplementer must not expect any
of the following from conv canonicalization:

- **No conv→dot and no conv→im2col-matmul rewrite.** The four bodies
  (`runOnOperation`, `explicitlyPad`, `explicitlyStride`, stride lambda) reference
  zero `im2col` / `DotGeneral` / `TransposeOp` / `matmul` builders (`rg -ic` over
  all four decompiled bodies = 0). The only ops built are `{pad, slice, add,
  constant, convolution}`. The actual conv-as-matrix lowering for Trainium lives
  **downstream** in the Penguin middle-end / Tensorizer backend, out of scope for
  hlo2penguin.
- **No dimension-number permute / `mhlo.transpose` insertion.** Dim-numbers are read
  only to locate spatial axes and copied verbatim onto the rebuilt conv (§3, §4).
  The `hilo::Permutation` is gate-internal (§5).
- **No layout normalization to a fixed order (NHWC etc.).** The rebuilt conv keeps
  the original layout. Any fixed-layout requirement is enforced by the conv
  device-lowering ([6.8.1](../nki/conv-device-lowering.md)), not here.

> **NOTE —** the `tensor.g2s.im2col.*` and `tensor.g2s.tile.*` strings present in the
> binary are **not** Neuron extension ops emitted by this pass. They are upstream
> LLVM NVPTX intrinsics (`llvm.nvvm.cp.async.bulk.tensor.g2s.{tile,im2col}.*`, Hopper
> TMA) and NVGPU-dialect verifier strings, statically linked from the XLA-GPU
> dependency and GPU-targeted — never produced by any hlo2penguin pass.

---

## Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `CanonicalizeConv::runOnOperation` | 0x207c270 | 653 B | two-phase driver | CERTAIN |
| collect callback (lambda) | 0x2077710 | 169 B | filter `mhlo::ConvolutionOp` + gate → push | CERTAIN |
| `CanonicalizeConv::canCanonicalize` | 0x20774d0 | 576 B | eligibility gate | HIGH |
| `CanonicalizeConv::isBackwardConv` | 0x20777c0 | 1147 B | predicate (VLOG diag only) | HIGH |
| `CanonicalizeConv::explicitlyPad` | 0x207a7a0 | 6858 B | pad-fold → `mhlo.pad` + rebuilt conv | HIGH |
| `CanonicalizeConv::explicitlyStride` | 0x2079890 | 3675 B | stride-fold driver | HIGH |
| explicitlyStride lambda | 0x20782a0 | 5604 B | per-dim `mhlo.slice` + rebuilt conv | HIGH |
| `hilo::buildMhloConvParams` | 0x21cd050 | 5455 B | parse all conv attrs+shapes → `ConvParams` | HIGH |
| `hilo::ConvParams::~ConvParams` | 0x21cc510 | 294 B | struct-layout witness (11 owned buffers) | HIGH |
| `mlir::createCanonicalizeConvPass` | 0x2077c60 | — | MHLO factory | CERTAIN |
| `StableHLOCanonicalizeConv::runOnOperation` | 0x211dcd0 | ~653 B | byte-identical StableHLO driver | CERTAIN |
| `StableHLOCanonicalizeConv::explicitlyPad` | 0x211c270 | 6744 B | → `stablehlo.pad` | HIGH |
| `StableHLOCanonicalizeConv::explicitlyStride` | 0x211b600 | 2998 B | StableHLO stride driver | HIGH |
| `hilo::buildStableHLOConvParams` | 0x21cee00 | — | StableHLO param parse | HIGH |
| `mlir::createStableHLOCanonicalizeConvPass` | 0x2119950 | — | StableHLO factory | CERTAIN |

### BSS flag globals

| Symbol | Role | Confidence |
|---|---|---|
| `qword_9C714B8` | gates phase-2b `isBackwardConv` diagnostic re-pad | CERTAIN |
| `qword_9C71578` | gates acceptance of size-2/3 spatial dims in the gate | HIGH |

Both are unnamed BSS bytes (likely `cl::opt`/VLOG statics); their option-arg names
and defaults were not recovered from this binary (MEDIUM).

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

1. **"explicitlyPad emits `mhlo.pad` + zeroed-padding conv; group counts copied as
   2× `unsigned long`."** — CONFIRMED. The `explicitlyPad` @0x207a7a0 callee graph
   lists `mhlo::PadOp::build` (@0x8f7f470, 3× `DenseIntElementsAttr`) and
   `OpBuilder::create<mhlo::ConvolutionOp>` (@0x2078070) whose demangled signature
   ends `…, unsigned long, unsigned long, mlir::ArrayAttr&&` — the two group counts
   + precision_config.
2. **"explicitlyStride folds stride into the third `DenseIntElementsAttr` of
   `mhlo.slice`."** — CONFIRMED. `mhlo::SliceOp::build` (@0x8f83dd0) demangles to
   `(…, Type, Value, DenseIntElementsAttr, DenseIntElementsAttr, DenseIntElementsAttr)`
   = start/limit/stride; `BoolAttr::get` (@0x9b4ddb0) and `AddOp::build` (@0x8f704b0)
   appear in the stride callee graph.
3. **"Func-level pass; source `CanonicalizeConv.cc` @0x2f66a0; error `NCC_EUET001`
   @0x226305."** — CONFIRMED. Both string literals appear in the `explicitlyPad`
   context refs; the `OperationPass<func::FuncOp>` wrapper is RTTI-confirmed.
4. **"No im2col/dot/transpose anywhere in the four bodies."** — CONFIRMED.
   `rg -ic 'im2col|DotGeneral|TransposeOp|matmul'` over the four decompiled bodies
   returned 0 for each.
5. **"StableHLO twin shares the *same* source file `CanonicalizeConv.cc`."** —
   **FAILED → corrected.** The StableHLO `explicitlyPad` @0x211c270 refs
   `StableHLOCanonicalizeConv.cc` @0x348ed8 (a distinct TU) and `stablehlo::PadOp::build`
   @0x9159ef0 takes `llvm::ArrayRef<long>` (not `DenseIntElementsAttr`). Corrected in
   §6.

INFERRED / not pinned: the per-vector `ConvParams` field → semantic mapping (§5,
witnessed by dtor order only); the `cl::opt` names/defaults of the two BSS flags;
the full human-readable text of `NCC_EUET001` (formatted at runtime).

---

## Related Components

| Name | Relationship |
|---|---|
| Conv device-lowering selection ([6.8.1](../nki/conv-device-lowering.md)) | consumes the zero-pad/unit-stride canonical form this pass produces; selects the kernel |
| Conv kernels ([6.8.2 Depthwise](../nki/depthwise-conv.md) / [6.8.3 Dense Conv2D](../nki/dense-conv2d.md) / [6.8.4 conv2d_pbp](../nki/conv2d-pbp.md)) | the actual `private_nkl` conv lowerings the device-lowering dispatches to |

## Cross-References

- [Conv Device-Lowering Selection](../nki/conv-device-lowering.md) — 6.8.1, the next stage; expects zero-padding, unit-stride convs
- [private_nkl Depthwise Conv Kernels](../nki/depthwise-conv.md) — 6.8.2, depthwise conv kernel implementations dispatched after lowering selection
- [private_nkl Dense Conv2D (NHWC-Replication & Column-Packing)](../nki/dense-conv2d.md) — 6.8.3, dense conv2d kernel implementation
- [Experimental conv2d_pbp Kernels](../nki/conv2d-pbp.md) — 6.8.4, additional (experimental) conv kernel variants
