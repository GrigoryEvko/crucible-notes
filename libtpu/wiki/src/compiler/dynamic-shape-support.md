# Dynamic-Shape Support

> *Symbol names, VAs, and the build-id below apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ; treat every VA as version-pinned.*

## Abstract

TPU does **not** allocate buffers whose byte-size depends on a runtime value. Its entire dynamic-shape strategy is **static pad-to-bound with the real runtime size carried in a fixed-width metadata prefix**. A dimension marked dynamic carries a *static upper bound* (the extent stored in the `xla::Shape`) plus a per-dimension `is_dynamic` bit; the runtime value lives in `[0, bound]`. The compiler sizes every buffer at the bound, and the `xla::DynamicPadder` pass — running twice in the [HLO pre-pass](hlo-pre-passes.md) set — rewrites the module so that after it runs, layout assignment, tiling, MSA, and LLO lowering all see a **fully static module**. Dynamic-ness survives only as (a) ordinary S32 scalar SSA values that compute each dimension's runtime size, and (b) two boundary custom-calls, `PadToStatic` and `SliceToDynamic`, that read and write a **1024-byte metadata prefix** prepended to every dynamic buffer.

This page documents the three layers a reimplementation must get right: the **bounded-dynamic Shape model** and the size-operand threading (`DynamicParameterBinding`, `DynamicDimensionInference`, `SetDimensionSize`/`GetDimensionSize`); the **`DynamicPadder` pass** and its companions (`DynamicIndexSplitter`, `DynamicDimensionSimplifier`) — what they rewrite and where they insert the boundary custom-calls; and the **pad-to-static-tile policy** — how the prefix is sized, how `Target::ShapeWithMetadataSizeBytes` computes a buffer's physical bytes, and how the two boundary emitters lower the prefix to LLO. The window/conv same-padding bound arithmetic and the SparseCore MLIR dynamic-dim subsystem are out of scope and noted where they branch off.

The contract a reimplementation must honor:

- **Static pad-to-bound, never runtime-sized allocation.** `Target::ShapeWithMetadataSizeBytes(shape)` is `ShapeSizeBytesRaw(shape) + prefix` for a dynamic shape and `ShapeSizeBytesRaw(shape)` for a static one. `ShapeSizeBytesRaw` uses the static (upper-bound) extents. No code path makes a buffer's physical size a function of a runtime scalar.
- **The metadata prefix is a compile-time constant: 1024 bytes.** `Target::DynamicShapeMetadataPrefixBytes()` returns `1024` on every TPU generation, asserted divisible by `sizeof(int32_t)` (256 int32 dim-size slots) and `<= ChunkSizeBytes()`.
- **`DynamicPadder` is the master lowering pass; it runs *before* layout.** It sits at step 21 of the PreOptimization phase (after `DynamicDimensionSimplifier` at step 20, with `DynamicIndexSplitter` earlier at step 4 — see [hlo-pre-passes.md](hlo-pre-passes.md)). After it, only `Set/GetDimensionSize` S32 scalars and the two boundary custom-calls remain.
- **Dimension sizes are S32 scalars threaded through the HLO graph.** Entry-parameter dynamic dims bind to another S32 entry parameter via `DynamicParameterBinding`; intra-graph the `DynamicDimensionInference` map carries each dim's size SSA value; a 38-handler forward visitor propagates them.
- **TPU rejects *unbounded* dynamism.** Only bounded-dynamic dims (a dynamic dim with a static upper bound) compile; `unbounded dynamism is not supported` is the gate.

| | |
|---|---|
| **Master pass** | `xla::DynamicPadder::RunImpl(HloModule*, exec_threads)` @ `0x16998ca0` (~5,918 decompiled lines) |
| **Companion — step 4** | `xla::DynamicIndexSplitter::RunImpl` @ `0x164ae740` |
| **Companion — step 20** | `xla::DynamicDimensionSimplifier::RunImpl` @ `0x164d0020` |
| **Dim-size analysis** | `xla::DynamicDimensionInference::Run` @ `0x1e39ad20`; visitor `DynamicDimensionInferenceVisitor::Run` @ `0x1e3984c0` |
| **Boundary insert helpers** | `DynamicDimensionInferenceVisitor::RequiresPadToStatic` @ `0x1e39a7e0`; `::InsertPadToStaticOnInstruction` @ `0x1e390920` |
| **Un-pad rewriter** | `(anon)::DynamicShapeRemovingVisitor::ConvertToDynamic` @ `0x169a7bc0` |
| **Boundary emitters (LLO)** | `jellyfish::PadToStaticEmitter::Emit` @ `0x10c9ad40`; `jellyfish::SliceToDynamicEmitter::Emit` @ `0x10c9c6c0` |
| **Sizing** | `Target::ShapeWithMetadataSizeBytes` @ `0x1d619f20` → `TransferSizeUtil::ShapeWithMetadataSizeBytes` @ `0x1d6aea00`; `Target::DynamicShapeMetadataPrefixBytes` @ `0x1d61c4e0`; `Target::DynamicShapeMetadataPrefixShape` @ `0x1d61c500` |
| **Source unit** | `third_party/tensorflow/compiler/xla/service/dynamic_padder.cc` (string-anchored) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Bounded-Dynamic Shape Model

The model is the standard XLA per-dimension dynamic bit paired with the static extent serving as the upper bound. Each `xla::Shape` carries, per dimension `i`, a static extent `D[i]` and a boolean `is_dynamic[i]`. When `is_dynamic[i]` is true, `D[i]` is the **upper bound** and the runtime value lies in `[0, D[i]]`. The relevant accessors are byte-anchored:

| Method | VA | Role | Confidence |
|---|---|---|---|
| `xla::Shape::is_dynamic_dimension(int)` | `0x1e52c9e0` | per-dim dynamic bit query | CONFIRMED |
| `xla::Shape::set_dynamic_dimension(int, bool)` | `0x20cd8c60` | set per-dim dynamic bit | CONFIRMED |
| `xla::Shape::is_static()` | `0x20cd8f80` | true iff no dim is dynamic | CONFIRMED |

A dynamic dimension's runtime size is always an **S32 scalar**. The shape-inference helpers for the two dimension-size ops fix this:

- `xla::ShapeInference::InferSetDimensionSizeShape(Shape, Shape, long)` @ `0x1e541d20`
- `xla::ShapeInference::InferGetDimensionSizeShape(Shape, long)` @ `0x1e541960`

with the diagnostic `SetDimensionSize's value has to be S32 scalar, got %s` enforcing the operand dtype. The HloOpcode bytes for the dynamic-dim op family (authoritative map cross-referenced from the opcode catalog):

```text
0x35 dynamic-reshape       0x36 dynamic-slice        0x37 dynamic-update-slice
0x3f get-dimension-size    0x70 set-dimension-size   0x51 pad
```

> **NOTE — decompile cross-check.** `DynamicPadder::RunImpl` (`0x16998ca0`) discriminates these by raw opcode: at line ~1077 it tests `v25 == 63 || v25 == 112` (decimal `0x3f`/`0x70` = get-/set-dimension-size) and special-cases the `SetBound` custom-call before deciding an instruction is "already-static". [Confidence: CONFIRMED — read directly from the decompiled switch.]

Compile-time invariants on the bound are guarded by verbatim diagnostics (the rewriters never silently truncate):

- `Dimension size has to be less-equal than upper bound %lld for dimension %lld in shape %s`
- `dynamic size must be less than or equal to static size`
- `Shape size has to be less than %d in dynamic shape bounded by %s`
- `Shape size has to be greater or equal than 0 in dynamic shape bounded by %s`
- `Non-positive constant for dynamic size`
- `requires 'shape' to have at most one dynamic dimension, but got multiple dynamic dimensions at indices {0} and {1}` — some ops cap at a single dynamic dim.

The jellyfish layer additionally models a per-dimension tiling/parallel extent as `std::variant<long, jellyfish::DynamicBound>` — a tiling dim is either a concrete `long` extent or a `DynamicBound` (a bounded-dynamic extent). This surfaces in `VerifyParallelAttributes(..., absl::Span<variant<long, DynamicBound> const>, ...)` (`0x14516f40`) and `LiteralBase::Piece::CopyElementsWithDynamicBound<T>` for literal evaluation under a bound. [Confidence: HIGH — symbol-anchored, body not fully traced.]

---

## Dimension-Size Threading

Dynamic dim sizes are threaded as ordinary S32 scalar SSA values through three layers.

### Layer 1 — entry binding: `DynamicParameterBinding`

For an entry parameter whose dimension is dynamic, the runtime size is supplied as *another entry parameter* (an S32 scalar). The binding is a `map<DynamicDimension{param, index, dim} -> DynamicSizeParameter{param, index}>`. The verbatim diagnostic anchors the mechanism:

```text
-- Input param number %lld at %s has dim %lld as dynamic dimension,
   which is represented by param number %lld at %s
```

`DynamicParameterBinding` is the analysis seed: `DynamicDimensionInferenceVisitor::Run` consumes it, and conditionals carry a per-branch binding (`dynamic_parameter_binding for conditional branch`).

### Layer 2 — intra-graph map: `DynamicDimensionInference`

This analysis maintains `map<DynamicDimension{HloInstruction*, ShapeIndex, dim} -> HloInstruction*>` — the S32 SSA value that holds each dim's runtime size. It is backed by two ordered-tree containers (one keyed by `HloInstruction*`, one by the `DynamicDimension` struct via `operator<` @ `0x1e3a8520`). Public API (all byte-anchored):

| Method | VA |
|---|---|
| `Run(module, op_dynamism_support_fn, custom_call_handler, ShapeCheckMode, assertion_generator, exec_threads)` | `0x1e39ad20` |
| `SetDynamicSize(inst, ShapeIndex, dim, size_inst)` | `0x1e38f1e0` |
| `GetDynamicSize(inst, ShapeIndex, dim) -> HloInstruction*` | (const variant) `0x1e39bbe0` |
| `GetDynamicSizes(inst, ShapeIndex)` | `0x1e39bc00` |
| `GetDynamicShape(inst)` | `0x1e39b980` |
| `ForwardDynamicSize(inst, new_inst, ShapeIndex)` | `0x1e39b580` |
| `ReplaceAllDynamicDimensionUsesWith(a, b)` | `0x1e3927a0` |
| `CopyMapping(from, to, replacement_map)` | `0x1e3982c0` |
| `AnalyzeDynamicDimensions()` | `0x1e39b0a0` |

> **NOTE — decompile cross-check on `Run` signature.** The mangled symbol at `0x1e39ad20` demangles to `Run(HloModule*, std::function<OpDynamismSupport(HloInstruction*)>, std::function<bool(HloInstruction*, OpDynamismSupport)>, ShapeCheckMode, std::function<void(HloInstruction*)> const&, absl::flat_hash_set<string_view> const&)`. The first callback is the per-op dynamism-support query, the third is the `assertion_generator`, the last is the execution-thread set. This matches the call from `DynamicPadder::RunImpl` (line ~1236) exactly. [Confidence: CONFIRMED.]

`Run` builds a `DynamicParameterBinding` from the entry layout, runs `DynamicDimensionInferenceVisitor` to propagate dim-size SSA values forward across every instruction, then calls `AnalyzeDynamicDimensions`.

### Layer 3 — propagation: `DynamicDimensionInferenceVisitor` (38 handlers)

The forward visitor (`Run` @ `0x1e3984c0`) computes each output dim-size SSA from operand dim-sizes. Each handler receives a callback of signature `(HloInstruction* inst, ShapeIndex, long dynamic_dim, long operand_dim, HloInstruction* dynamic_size)`. Selected handlers (full set is 38; representative subset with byte anchors):

| Handler | VA | Propagation rule |
|---|---|---|
| `HandleParameter` | `0x1e39a6c0` | seeds from `DynamicParameterBinding` |
| `HandleSetDimensionSize` | `0x1e3929e0` | binds dim → operand-1 (the S32 size) |
| `HandleGetDimensionSize` | `0x1e3923a0` | materializes the stored size SSA |
| `HandleBroadcast` | `0x1e38fae0` | maps via broadcast dims; `Broadcast input and output dynamism mismatch` |
| `HandleConstant` | `0x1e38fba0` | clears dynamism (constants are static) |
| `HandleConcatenate` | `0x1e391c20` | sum of operand dyn-sizes on the concat dim |
| `HandleReshape` | `0x1e394980` | factorizes dim groups; threads sizes per group |
| `HandleDynamicReshape` | `0x1e3947e0` | uses the explicit dim-size operands |
| `HandlePad` | `0x1e391360` | size combined with pad config |
| `HandleDot` / `HandleConvolution` | `0x1e3918a0` / `0x1e391b60` | propagate batch/spatial dyn dims |
| `HandleDynamicConvolutionForward` | `0x1e392d00` | `GetWindowedOutputSize` bound |
| `HandleDynamicWindowSamePadding` | `0x1e393060` | same-padding bound |
| `HandleWhile` | `0x1e398900` | threads dyn-size through the loop-carried tuple |
| `HandleConditional` | `0x1e395d00` | per-branch binding |
| `HandleCustomCall` | `0x1e390580` | `Dynamic inferencing on custom call %s is not supported` unless a handler is registered |
| `PassThroughDynamicDimension` | `0x1e3935e0` | generic forward helper (elementwise, select, clamp, transpose, slice…) |

The visitor also decides **where** the static boundary goes, via `RequiresPadToStatic(inst, ShapeIndex)` (`0x1e39a7e0`) and `InsertPadToStaticOnInstruction(inst)` (`0x1e390920`): when a downstream op cannot consume a dynamic operand, a `PadToStatic` custom-call is inserted. The `ShapeCheckMode` enum selects whether dynamic-dim consistency is verified at compile time or deferred to a runtime assertion emitted by the `assertion_generator` callback; the runtime-mode anchor is `dynamic dimensions size %d did not match number of dimensions %d`.

---

## The DynamicPadder Pass

`xla::DynamicPadder::RunImpl(HloModule*, exec_threads)` @ `0x16998ca0` is the master dynamic-shape lowering pass — the open-source XLA `DynamicPadder`, configured by a `DynamicPadderOptions` and added with `AddPass<DynamicPadder>(DynamicPadderOptions)`. It sits at PreOptimization **step 21**, immediately after `DynamicDimensionSimplifier` (step 20) and `ConditionalCanonicalizer`, with `DynamicIndexSplitter` earlier at step 4 (see [hlo-pre-passes.md](hlo-pre-passes.md), where it is recorded as a 2× `AddPass`).

`RunImpl` flow, recovered from the decompiled call sequence:

1. **Build dynamism info.** Call `DynamicDimensionInference::Run(module, op_dynamism_support_fn, custom_call_handler, shape_check_mode, assertion_generator, exec_threads)` (line ~1236). The `op_dynamism_support_fn` returns an `OpDynamismSupport` per op; the anchor `op_support != OpDynamismSupport::kNoSupport` decides whether an op keeps its dynamic shape or must be padded. The TPU `tpu_compile_op_support.cc` supplies the per-op support table.

2. **Rewrite ops that cannot consume a dynamic operand.** All rewriters live in the `dynamic_padder.cc` anonymous namespace:
   - `PadWithScalar(inst, dim, dynamic_size, pad_value)` @ `0x169a2fe0` — pads the bounded region beyond the runtime size with an *identity* scalar (0 for add-reduce, −inf for max-reduce, …) so padded lanes do not perturb the result.
   - `GenerateBinaryMask(inst, dim, dims, dyn_sizes, iota, lt, is_lower)` @ `0x169a6360` — builds an `iota < dynamic_size` comparison mask (the "padding mask" of the pass's output invariant).
   - `RewriteDynamicReshape(inst, ddi)` @ `0x169a11c0` (+ `RewriteDynamicReshapeSingleGroup` @ `0x169a3600`) — splits the reshape into dim-groups and threads dynamic sizes.
   - `RewriteInputWithDynamicPadding(inst, operand, pad, dims, Window*, size_fn)` @ `0x169a6ec0` — pads a conv/window input, using `GetWindowedOutputSize` (`0x1e3a93e0`) / `GetWindowedInputGradSize` (`0x1e3a9b40`) for the bound. Driven by the three `DynamicConvolution{Forward, InputGrad, KernelGrad}` custom-call branches.
   - `RewriteDynamicBinaryOp` / `RewriteDynamicSort` (lambdas), and the reduce-window/select-and-scatter same-padding rewriters.

   The rewriters build HLO via `MakePadHlo` (`0x1e3e5560`), `CreateSlice` (`0x1e593160`), `CreateDynamicSlice` (`0x1e5947e0`), `CreateDynamicUpdateSlice` (`0x1e594860`), `CreateReshape` (`0x1e594de0`), and friends.

3. **Insert the two boundary custom-calls.**
   - `PadToStatic` — at every point a dynamic value must become a static-shaped value (entry to a static-only region). Inserted by `InsertPadToStaticOnInstruction` / `RequiresPadToStatic`.
   - `SliceToDynamic` — the inverse, at every point a static value must be re-annotated as dynamic (output boundary).

   Targets: `jellyfish::dynamic_padding_handler::kPadToStatic` / `kSliceToDynamic`, registered via `CustomCallRegistration::RegisterLoweringEmitter("SliceToDynamic", dynamic_padding_emit_helper)` from `custom_ops/dynamic_padding_handler.cc`.

4. **Un-pad ops that DO support dynamic natively.** `(anon)::DynamicShapeRemovingVisitor::ConvertToDynamic(inst)` @ `0x169a7bc0` (+ `ConvertOperandsToDynamic` @ `0x169a8ac0`) walks ops whose `OpDynamismSupport` is "supported" and strips the `PadToStatic` that `DynamicPadder` would otherwise have inserted, avoiding an unnecessary pad/slice round-trip. The mirror anchor is `Input to RemoveDynamicShapeMetadataIfPresent should be static`.

> **NOTE — decompile cross-check.** `DynamicPadder::RunImpl` (5,918 decompiled lines) was confirmed to call, in order, `Shape::is_static` (the already-static early-out at line ~1071), `DynamicDimensionInference::Run` (~1236), `RewriteDynamicReshape` (~1410), `GetDynamicSize`/`ForwardDynamicSize`/`GetDynamicSizes`/`HasDynamicDimension` throughout, `PadWithScalar` and `RewriteInputWithDynamicPadding` inside the three `DynamicConvolution*` custom-call branches (~3098/3172/3332/3381/3528), and `DynamicShapeRemovingVisitor::ConvertToDynamic` at the tail (~5178). A `ret_check` `kernel->shape().is_static()` (~3267) guards the conv-kernel path. [Confidence: CONFIRMED.]

### Companions

- `xla::DynamicIndexSplitter::RunImpl` @ `0x164ae740` (step 4) — splits multi-dim dynamic indices on `DynamicSlice`/`DynamicUpdateSlice` into per-dim scalar index operands, so each index is a single S32 SSA value.
- `xla::DynamicDimensionSimplifier::RunImpl` @ `0x164d0020` (step 20) — folds redundant `Get`/`SetDimensionSize` chains and `<= K` dynamic-dim ops before `DynamicPadder` runs.

---

## Pad-to-Static-Tile Policy and the Runtime Representation

After `DynamicPadder`, every buffer is sized at its static bound, and the runtime sizes live in a **1024-byte metadata prefix** prepended to each dynamic buffer.

### Buffer sizing

`Target::ShapeWithMetadataSizeBytes(shape)` (`0x1d619f20`) delegates to `TransferSizeUtil::ShapeWithMetadataSizeBytes` (`0x1d6aea00`). The decompiled body:

```c
// xla::jellyfish::TransferSizeUtil::ShapeWithMetadataSizeBytes  @0x1d6aea00
if (element_type == 13)            // TOKEN/opaque: no payload
    prefix = 0;
else if (Shape::is_static(shape))  // fully static: no prefix
    prefix = 0;
else {                             // dynamic: prefix from layout, default 1024
    prefix = shape.layout().dynamic_shape_metadata_prefix_bytes();
    if (prefix == 0) prefix = 1024;
}
return prefix + ShapeSizeBytesRaw(shape);   // ShapeSizeBytesRaw uses static extents
```

So the physical byte-size is `ShapeSizeBytesRaw(shape) + prefix`, where `ShapeSizeBytesRaw` is computed from the **static (upper-bound) extents** — never a runtime value. The prefix is 1024 by default and can be overridden per-buffer by the `xla::Layout::dynamic_shape_metadata_prefix_bytes()` field.

> **CORRECTION — the prefix is layout-overridable, defaulting to 1024.** Earlier notes described `ShapeWithMetadataSizeBytes` as a flat `ShapeSizeBytesRaw + 0x400`. The decompile shows the dynamic branch reads `layout().dynamic_shape_metadata_prefix_bytes()` and only falls back to `1024` when that field is zero, and that the `TOKEN` element type (13) takes neither prefix nor payload. The constant 1024 still comes from `Target::DynamicShapeMetadataPrefixBytes()` (below), which is what populates the layout field. [Confidence: CONFIRMED — read from `0x1d6aea00`.]

### The prefix constant

`Target::DynamicShapeMetadataPrefixBytes()` @ `0x1d61c4e0` is, in full:

```c
__int64 xla::jellyfish::Target::DynamicShapeMetadataPrefixBytes(Target *this) {
  return 1024;
}
```

1024 bytes, on every TPU generation. Asserted invariants (verbatim `CHECK` strings):

- `b.target().DynamicShapeMetadataPrefixBytes() % sizeof(int32_t) == 0` — 1024 = 256 four-byte int32 dim-size slots.
- `b.target().DynamicShapeMetadataPrefixBytes() <= b.target().ChunkSizeBytes()` — the prefix fits inside one HBM/VMEM chunk.
- `metadata_offset == target.DynamicShapeMetadataPrefixBytes()` — the data region begins exactly after the prefix.

`Target::DynamicShapeMetadataPrefixShape()` @ `0x1d61c500` builds a 1-D S32 shape of dim sizes — the logical type of the prefix.

### On-device buffer layout

```text
+------------------------------+--------------------------------------------+
| metadata prefix (1024 bytes) | data, padded to the static upper bound     |
| = DynamicShapeMetadataPrefix |  (= ShapeSizeBytesRaw(shape) bytes)         |
|   Bytes() = 0x400            |                                            |
| up to 256 int32 dim sizes    |  physical extent independent of runtime    |
+------------------------------+--------------------------------------------+
```

`SliceToDynamic` writes the prefix; `PadToStatic` reads it.

### Layout / tiling / MSA treatment

Because `DynamicPadder` runs before layout, all later passes operate on static array shapes (with the per-dim `is_dynamic` bit retained for bookkeeping):

- **Layout** ([layout-assignment.md](layout-assignment.md)): `TpuLayoutAssignment` chooses layouts from the static (upper-bound) dims via `ChooseCompactLayoutForShape`. The `dynamic_shape_metadata_prefix_bytes()` field rides on the `xla::Layout` (anchor: `input_shape.layout().dynamic_shape_metadata_prefix_bytes() is expected to be non-zero, where input_shape = `), recording that the buffer carries a prefix.
- **Tiling** ([loop-tiling-unrolling.md](loop-tiling-unrolling.md)): tiling uses the static extents. `lowering_util::DynamicShapeSizeCompactRaw` (`0x1c6ca220`) and `DynamicShapeSizeCompactForDmaRaw` (`0x1c6ca8a0`) compute the *actual* runtime transfer byte-size from the dim-size scalars at DMA time, so DMAs move only the live region, not the full pad-to-bound buffer.
- **MSA** (memory-space assignment): allocates `Target::ShapeWithMetadataSizeBytes(shape)` per buffer — pad-to-bound + prefix. Asserts `allocation->size() == target.ShapeWithMetadataSizeBytes(allocation->shape())`. MSA never sees a runtime-sized buffer.

The TF/host boundary uses `tensorflow::XlaTpuPaddedShapeFn(TpuTopology, Shape, Shape*)` @ `0xf7d1cc0` (→ `TransferSizeUtil::SetPaddedShape`) to compute the on-device padded shape for a dynamic XLA shape (pads each dynamic dim to its bound, walks tuples).

---

## DynamicSlice / DynamicUpdateSlice / Reshape Lowering

After `DynamicPadder`, the module is static plus the two boundary custom-calls. `dynamic-pad` and `dynamic-reshape` never survive to LLO — `DynamicPadder` fully rewrites them into `Pad` + `DynamicSlice`/`Reshape` + mask sequences at HLO time. The remaining first-class dynamic-index ops are lowered directly by the TPU HLO→LLO `LoweringEmitter`:

| Emitter | VA | Role |
|---|---|---|
| `jellyfish::LoweringEmitter::HandleDynamicSlice(hlo)` | `0x10c3b0c0` | static-extent indexed load with a runtime base offset |
| `jellyfish::LoweringEmitter::HandleDynamicUpdateSlice(hlo)` | `0x10c3b640` | static-extent indexed store |
| `jellyfish::DynamicUpdateSliceEmitter` | `0x10c66ec0` | DUS variant (`OpEmitter::Emit<>`) |
| `jellyfish::AsyncDynamicIndexEmitter` | `0x10c415e0` | pipelined form — index scalar computed in one bundle, the indexed load/store deferred to a later one |

Because the slice extents are static (the slice is into a pad-to-bound buffer), the lowering is a static-extent indexed copy with a runtime base offset computed from the S32 index scalars that `DynamicIndexSplitter` produced. The X128/precision and sharding visitors reuse the same handlers: `XPrecisionRewriteVisitor::HandleDynamicSlice/UpdateSlice` (`0x1115fb40`/`0x1115fd00`) and `DimLabelPropagation::HandleDynamicSlice/UpdateSlice` (`0x11197ca0`/`0x11198220`). A peephole `SliceToDynamicCopyMover` (`0x10fc0240`) pushes a `Copy` through `SliceToDynamic(Copy(x))` to eliminate a redundant copy at the dynamic boundary.

### The two boundary emitters

These are the heart of the runtime contract.

**`PadToStaticEmitter::Emit()` @ `0x10c9ad40`** takes a dynamic-shaped input buffer (prefix + pad-to-bound data) and produces a tuple of {static-shaped data array, S32 dim-size scalars}. Confirmed LLO sequence:

```text
DynamicShapeMetadataPrefixBytes()                 // locate the 1024-byte prefix
  -> CHECK prefix % sizeof(int32_t) == 0
LloRegionBuilder::Vld(...)                          // vector-load dim-size metadata
lowering_util::ScalarToSreg(...)                    // move size into a scalar reg
lowering_util::ComputeBoundsInChunks(sizes, b)      // dim sizes -> chunk counts
lowering_util::CalculateDynamicCompact2ndMinorRatio // re-derive compact tiling ratio
Target::ChunkCountsWithTmp(shape, tmp)              // pad-to-bound chunk counts
PipelineEmitter::SetDynamicIterationBounds(...)     // iteration count driven by runtime size
PipelineEmitter::Emit(...)                          // software-pipelined chunk transfer
```

> **NOTE — decompile cross-check.** All of the above were read directly from `0x10c9ad40`: the `DynamicShapeMetadataPrefixBytes() & 3` divisibility check with the matching `CHECK` string (line ~274/277), `Vld` (~291), `ScalarToSreg` (~446), `ComputeBoundsInChunks` (~499), `ChunkCountsWithTmp` (~270/271), `CalculateDynamicCompact2ndMinorRatio` (~705/841), and `PipelineEmitter::SetDynamicIterationBounds` (~630). [Confidence: CONFIRMED.]

**`SliceToDynamicEmitter::Emit()` @ `0x10c9c6c0`** is the inverse: it takes a static-shaped data array + S32 dim-size scalars, writes the prefix, and produces a dynamic-shaped buffer. Confirmed LLO sequence:

```text
DynamicShapeMetadataPrefixShape()                   // the 1-D S32 prefix shape
  + DynamicShapeMetadataPrefixBytes() (% 4 check)
LloRegionBuilder::Vlaneseq / VimmS32 / VeqS32 / Vselect   // build per-lane validity mask
lowering_util::BroadcastScalarToVreg(...)            // broadcast each dim size
deep_copy_util::MemsetInGranules(...)                // zero/init the prefix region
lowering_util::ComputeBoundsInChunks(...)            // sizes -> chunk counts
lowering_util::CalculateDynamicCompact2ndMinorRatio  // compact tiling ratio
... CopyArray after the prefix
```

> **NOTE — decompile cross-check.** Read from `0x10c9c6c0`: `Vlaneseq`/`VimmS32`/`VeqS32`/`Vselect` mask construction (lines ~291–317), `BroadcastScalarToVreg` (~314), the prefix `% sizeof(int32_t)` check (~324/327), `DynamicShapeMetadataPrefixShape` (~336), `ComputeBoundsInChunks` (~430), `MemsetInGranules` (~541), and `CalculateDynamicCompact2ndMinorRatio` (~613/744). [Confidence: CONFIRMED.]

---

## Unsupported and Rejected Cases

TPU explicitly rejects *unbounded* dynamism and several op-specific dynamic cases. Verbatim string anchors:

| Anchor | Meaning | Confidence |
|---|---|---|
| `unbounded dynamism is not supported` / `Unbounded dynamism is disabled for instruction: %s` | only bounded-dynamic dims compile | CONFIRMED (string) |
| `AllToAll does not support bounded dynamic shapes` / `AllToAllTuple does not support unbounded dynamic shapes` | collectives reject dynamism | CONFIRMED (string) |
| `CustomCall "%s" is not supported to have a dynamic dimension` | most custom-calls must be static-shaped | CONFIRMED (string) |
| `Dynamic inferencing on custom call %s is not supported` | no registered dynamism handler | CONFIRMED (string) |
| `bitcast-convert is not valid for dynamic shape %s->%s` | bitcast-convert rejects dynamism | CONFIRMED (string) |
| `The output of iota must not have dynamic dimensions` | iota output must be static | CONFIRMED (string) |
| `Dynamic shapes are not supported for host buffers` / `dynamic shapes not supported in allocations` | host/pinned allocs reject dynamism | CONFIRMED (string) |
| `MemRefType don't support dynamic shapes` | MLIR memref must be static | CONFIRMED (string) |

The StableHLO unbounded-`Dynamic*Op` family (`DynamicReshapeOp`, `DynamicBroadcastInDimOp`, `DynamicConvOp`, `DynamicGatherOp`, …) has verifiers present in the binary, but whether any are ever reachable on TPU or are purely a front-end import artifact rejected by `unbounded dynamism is not supported` was not established. [Confidence: LOW.]

---

## What Is Not Recovered

- **Exact internal byte/dim-ordering of the 1024-byte prefix.** Inferred to be a 1-D S32 array of dim sizes (`DynamicShapeMetadataPrefixShape`); whether the ordering is major-to-minor or minor-to-minor, and whether tuple sub-shapes share or each own a prefix, was not byte-dumped from a live buffer. [Confidence: MEDIUM.]
- **Full `DynamicPadderOptions` proto field set.** Confirmed via the `Run` signature + anchors: `shape_check_mode`, `op_support_from_compute` (the `OpDynamismSupport` fn), `assertion_generator`, `slice_dynamic_output`. The complete `dynamic_padding.proto` descriptor was not field-by-field decoded. [Confidence: MEDIUM.]
- **The per-op `OpDynamismSupport` table** (`tpu_compile_op_support.cc`) — which ops keep a dynamic operand vs require a `PadToStatic` boundary — was not enumerated. [Confidence: LOW.]
- **Per-handler arithmetic of each `DynamicDimensionInferenceVisitor::Handle*`.** The open-source propagation algorithm is known and the TPU binary follows it; handler bodies were sampled, not exhaustively traced. [Confidence: MEDIUM.]
- **The SparseCore dynamic-dim subsystem** (`LowerDynamicDimensionSizePass`, `sc_tpu.set/get_dynamic_dimension_size`, `DynamicBoundedSlicedInput`, `ConvertStaticToDynamicEmitter`) — a distinct MLIR-level path, out of this page's scope.
- **Conv/window same-padding bound arithmetic** (`GetWindowedOutputSize` / `RewriteDynamicConvolution*`) — anchored here but not traced to reimplementation accuracy.

---

## Cross-References

- [hlo-pre-passes.md](hlo-pre-passes.md) — the ordered pre-pass table where `DynamicIndexSplitter` (#4), `DynamicDimensionSimplifier` (#20), and `DynamicPadder` (#21, 2× `AddPass`) live; this page is the algorithm detail for those rows.
- [compile-phases.md](compile-phases.md) — the top-level phase ordering; `DynamicPadder` runs inside the PreOptimization phase, before layout/MLIR.
- [overview.md](overview.md) — compiler orientation and the HLO → … → LLO IR-layer stack; owns the MLIR handoff the boundary emitters feed into.
- [layout-assignment.md](layout-assignment.md) — `TpuLayoutAssignment` carries the `dynamic_shape_metadata_prefix_bytes()` layout field; it sees a static module after `DynamicPadder`.
- [loop-tiling-unrolling.md](loop-tiling-unrolling.md) — tiling uses static extents; `DynamicShapeSizeCompactForDmaRaw` sizes DMAs to the live region at runtime.
- [back to index](../index.md)
