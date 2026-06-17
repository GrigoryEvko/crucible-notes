# MLIR Dynamic-Shape Front-End and the "Bucketing" Non-Mechanism

> *All addresses, symbols, and strings on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical — names, offsets, and constants are stable, virtual addresses are cp310-specific). The dynamic-shape machinery lives in `neuronxcc/starfish/bin/hlo2penguin` (BuildID `4ea91b0c9770f006`, **not stripped**) and identically in `neuronxcc/starfish/bin/hlo-opt` (BuildID `93dd8bd9bd4c697b`). Addresses below are IDA virtual addresses from the hlo2penguin sidecars; for a raw on-disk `xxd`/`objdump` of `hlo-opt`, subtract the `.text` delta `0x201000` / `.rodata` delta `0x200000` documented in [hlo-ingestion-boundary §5](hlo-ingestion-boundary.md). Other versions will differ.*

## Abstract

This page documents the HLO/MLIR front-end's handling of **dynamic tensor dimensions**, and it leads with a **negative result** that is the single most important fact on the page: **neuronx-cc 2.24 has no "compile-N-static-buckets" mechanism inside the compiler.** There is no pass, no class, no flag, and no string in `hlo2penguin` or `hlo-opt` that takes one dynamic-shaped program and emits a finite set of static-shape compilations. Every `bucket*` token in either binary belongs to one of four *unrelated* subsystems — a TensorFlow `HistogramProto`, the XLA `literal_comparison::ErrorBuckets` numerical-diff helper, LLVM's `StringMap`/`FoldingSet` hash-table internals, or the collective-combiner *byte-size* thresholds (`--internal-ccop-*-bucketing`). The "N static buckets" pattern a Neuron user sees (dynamic batch, KV-cache sequence length) is produced **above** this compiler: the framework traces the model once per shape and hands neuronx-cc one *fully static* HLO module per bucket; the runtime later dispatches to the smallest fitting NEFF. Inside the compiler, the only support that requires is "compile a static shape" — which is trivial and carries no bucketing code.

What the front end **does** implement is the stock XLA *bounded-dynamic-shape* path: a single executable whose dynamic dims are **bounded** (each carries a compile-time upper limit `N`), **padded** to that bound, and **masked at runtime** with a per-dimension size scalar. This is upstream code, statically linked: `DynamicDimensionInference` builds the `(instruction, ShapeIndex, dim) → S32-size-scalar` side table; `DynamicPadder` rewrites every shape-dynamic op into static HLO plus `PadToStatic`/`SliceToDynamic` boundary custom-calls; the StableHLO/MHLO `*NotActuallyDynamic` canonicalizations fold away dynamism that is provably static. The Neuron-authored delta is **thin**: one MLIR cleanup pass, `CanonicalizeForTensorizer`, whose only dynamic-relevant rewrite is `replaceGetDimensionSize` — a constant-fold of `GetDimensionSize` on statically-known dims — plus the handoff that leaves a surviving runtime-offset `DynamicSlice` for the backend's indirect-DMA path.

The reader should leave with a sharp stock-vs-Neuron map. The propagation, padding, and compatibility machinery is **all** upstream XLA/MHLO/StableHLO (tagged `[STOCK]` below). The Neuron contribution is exactly three things (tagged `[NEURON]`): the `replaceGetDimensionSize` constant-fold, the `Input tensor ${0} has dynamic shape` rejection guard at the Tensorizer boundary, and the DMA-scratch sizing that surviving runtime-offset slices feed. Everything labeled "bucketing" is a red herring this page exists to retire.

For reimplementation, the contract is:

- **The bucketing non-mechanism** — the complete enumeration of `bucket*` tokens and the proof that each is unrelated, so a reimplementer writes *zero* shape-bucketing logic.
- **The bounded-dynamic representation** — the two-channel encoding (Shape `is_dynamic_dimension[]` bits + the `DynamicDimensionInference` size-scalar side table) and where the size scalar originates (`SetDimensionSize`/`DynamicParameterBinding`).
- **The dynamic→static lowering** — `DynamicPadder`'s `PadWithScalar` + per-opcode rewrites + `PadToStatic`/`SliceToDynamic`, and the StableHLO `*NotActuallyDynamic` folds that precede it.
- **The Neuron handoff** — `CanonicalizeForTensorizer::replaceGetDimensionSize` (static-dim const-fold), the still-dynamic rejection guard, and how a runtime-offset slice survives into Penguin for the backend dynamic path.

| | |
|---|---|
| **Binary** | `neuronxcc/starfish/bin/hlo2penguin` (mirrored in `hlo-opt`) |
| **Dynamic lowering (stock)** | `xla::DynamicPadder::Run` @`0x2b7aff0` |
| **Pad primitive (stock)** | `(anon)::PadWithScalar` @`0x2b6bd00` |
| **Compatibility predicate (stock)** | `xla::ShapeUtil::DynamicShapeIsCompatible` @`0x94d4f90` (196 B, 10 bb) |
| **Neuron const-fold** | `CanonicalizeForTensorizer::replaceGetDimensionSize` @`0x2080240` |
| **Neuron DMA-scratch sizing** | `xla::hilo::SetDynamicDMASbufBytes` @`0x1f930d0` |
| **Tensorizer legalize** | `TensorizerLegalizationPass::runOnOperation` @`0x2197940` |
| **Bucketing inside compiler** | **none** — see [§ The Bucketing Non-Mechanism](#the-bucketing-non-mechanism) |
| **IR level** | HLO (`xla::`/`xla::hilo::`) + MHLO/StableHLO, pre-Penguin |

---

## The Bucketing Non-Mechanism

### Purpose

This is the headline result and the reason the page exists. A widespread mental model holds that a "dynamic shapes" compiler picks a `bucket_limit`-style list of sizes and compiles each into a static variant — that the *compiler* is what turns one dynamic batch into, say, batch-{1,2,4,8} executables. **That mechanism is not present in neuronx-cc 2.24.** This section enumerates every `bucket*` token in the compiler binaries and proves, token by token, that none of them is shape compilation. Backing the negative hard matters: a reimplementer who believes the compiler buckets will waste effort building a pass that does not exist, and will misattribute a runtime/framework concern to the wrong layer.

### The complete `bucket*` enumeration

A deduplicated grep of every `bucket`-containing string in `hlo2penguin` (`rg -i -o '"[^"]*[Bb]ucket[^"]*"'` on the strings sidecar) returns exactly the set below. There are no others; `hlo-opt` matches.

| Token (verbatim) | Origin subsystem | Shape-related? |
|---|---|---|
| `"bucket"`, `"bucket_limit"` | `tensorflow::HistogramProto` (`summary_go_proto`) | **No** — metrics proto |
| `"Bucket "`, `"Hash in Bucket "`, `"Offset in Bucket "`, `"String in Bucket "`, `"Header Bucket Count"`, `"Header: bucket count"` | LLVM DWARF `AccelTableWriter` (`emitAppleAccelTableImpl`/`emitHashes`) | **No** — debug-info accel-table diagnostics |
| `_ZN3xla18literal_comparison…ErrorBuckets…` (in `EqualHelper`/`Equal`) | `xla::literal_comparison::ErrorBuckets` | **No** — numerical diff |
| `_ZN4llvm13StringMapImpl15LookupBucketForE…` | `llvm::StringMapImpl::LookupBucketFor` | **No** — LLVM hashtable |
| `_ZN4llvm14FoldingSetBase15GrowBucketCountE…` | `llvm::FoldingSetBase::GrowBucketCount` | **No** — LLVM hashtable |
| `--internal-ccop-*-bucketing` (driver-side, `Frontend.so`) | collective combiner byte thresholds | **No** — see [§ ccop](#ccop-bucketing-is-byte-size-not-shape) |

> **GOTCHA —** the only token that *looks* shape-relevant is `bucket_limit`, and it is the most innocent of all. It is a field of `tensorflow::HistogramProto` — confirmed: the string `"HistogramProto"`, the mangled type `N10tensorflow14HistogramProtoE`, and the descriptor path `…/summary_go_proto` are all co-located in the same `.rodata` region as `bucket_limit`/`bucket`. `HistogramProto` is `{min, max, num, sum, sum_squares, bucket_limit[], bucket[]}`, an XLA/TSL metrics message for plotting value distributions. It has nothing to do with tensor shapes. A reimplementer who finds `bucket_limit` and concludes "here is the shape-bucket list" has misread a histogram.

### The proven absences

Equally important is what is **not** present. Every token a shape-bucketing system would carry returns **zero** matches across `hlo2penguin` and `hlo-opt`:

| Searched token | Hits | Interpretation |
|---|---|---|
| `candidate_shapes` | 0 | no list of static shape variants |
| `num_bucket` | 0 | no bucket count |
| `bucketize` | 0 | no bucketing verb |
| `shape-bucket` / `auto-bucket` | 0 | no shape-bucket pass name |
| `dynamic_batch` | 0 | no dynamic-batch knob |
| `compile.*bucket` (regex) | 0 | no "compile N buckets" path |
| `max_batch` | 1 (**not** bucketing) | the one hit is `max_batch_partitions == conv_output_batch_partitions` — a convolution SPMD-partitioning assert |

> **NOTE —** the single `max_batch` hit is a deliberate false-positive to flag: it is a CHECK string inside the stock convolution partitioner about *batch partition counts* under SPMD sharding, not a maximum batch *bucket*. Re-grepping it in context resolves it immediately; it is recorded here so the next auditor does not re-discover it as a "lead". **(CONFIRMED — string `"max_batch_partitions == conv_output_batch_partitions"`.)**

The conclusion is structural, not merely lexical: there is also **no runtime dispatch loop** over shape variants anywhere in these binaries (they are AOT compile-time tools), and **no class** that holds a set of shapes to be compiled. The compiler produces **one executable per invocation**; if that executable has dynamic dims, they are bounded and padded (next sections). The bucketing the user sees is produced elsewhere.

### Where the "static buckets" actually come from `[outside this binary]`

```text
   user model with dynamic batch / seq-len
          │
          ▼  FRAMEWORK (torch-neuronx / torch_xla / transformers-neuronx)   [NOT neuronx-cc]
   trace the model once PER desired shape  → one FULLY-STATIC HLO per bucket
          │
          ▼  neuronx-cc compiles EACH static HLO independently
   NEFF(bs=1)   NEFF(bs=2)   NEFF(bs=4) …      (compiler sees only static shapes)
          │
          ▼  RUNTIME (libnrt / neuron runtime dispatch)                      [NOT neuronx-cc]
   pick smallest bucket whose shape ≥ actual input, pad input up to it
```

The framework integration traces the graph at each batch size / sequence length in the user's bucket list, producing one static HLO module per bucket. neuronx-cc compiles each to its own NEFF, seeing only static shapes — the bucketing is invisible to it. The runtime holds the NEFF set and selects the smallest fitting bucket at inference time. **(INFERRED-STRONG from architecture: confirmed by the *absence* of any shape-variant set, bucket-selection symbol, or inference-time dispatch string in the compiler binaries; the positive mechanism of the framework/runtime layers is owned outside this wheel's compiler binaries.)**

### ccop "bucketing" is byte-size, not shape `[NEURON, collective]`

The only *Neuron-authored* thing called "bucketing" is **collective-communication** bucketing, exposed as driver flags on `neuronxcc/driver/jobs/Frontend.so`:

```text
--internal-ccop-bucketing
--internal-ccop-all-gather-bucketing
--internal-ccop-all-reduce-bucketing
--internal-ccop-reduce-scatter-bucketing
--internal-ccop-bucketing-allgather-size-in-bytes
--internal-ccop-bucketing-allreduce-size-in-bytes
--internal-ccop-bucketing-reducescatter-size-in-bytes
```

These set **byte-size thresholds** for *combining* many small collective ops (all-gather / all-reduce / reduce-scatter) into fewer larger ones — a "bucket" here is "group collectives until their cumulative byte size fills a threshold". This is the same mechanism as the [collective combiners](collective-combiners.md) (`all-reduce-combiner`), a performance fusion for distributed SPMD, and it is **orthogonal to dynamic shapes**. **(CONFIRMED — all seven flags present verbatim in `Frontend.so`; and crucially these are the *only* `bucketing` strings anywhere in the inspected binaries. The word `bucketing` does not appear in `hlo2penguin` or `hlo-opt` at all — the compiler binaries carry no bucketing string of any kind. Not to be conflated with shape bucketing.)**

> **QUIRK —** the byte-size suffix is the tell. A *shape* bucket would be sized in elements or named by a dimension; these are sized `-size-in-bytes` and scoped per *collective opcode* (`allgather`/`allreduce`/`reducescatter`). The word "bucket" is overloaded across two completely separate Neuron concerns and only one of them — the collective combiner — exists in this compiler at all.

---

## Dynamic-Dimension Propagation `[STOCK XLA]`

### Purpose

Before anything can be padded, the compiler must know *which* dims are dynamic and *what scalar* gives each one's runtime size. This is stock XLA `DynamicDimensionInference`. The entire representation and inference walk are upstream; the Neuron front end imports them unmodified.

### Representation — two channels

A dynamic dim is encoded two ways, exactly as in upstream XLA:

```c
// Channel (a): static, in the Shape proto — per dim i
shape.dimensions(i)            // the BOUND (upper limit), an int64
shape.is_dynamic_dimension(i)  // bool: is dim i dynamic?
//   BOUNDED dynamic : dimensions(i) = N,             is_dynamic = true  (real size <= N)
//   UNBOUNDED dynamic: dimensions(i) = kUnboundedSize sentinel,         (size unknown)

// Channel (b): dataflow, a side table held by DynamicDimensionInference
//   (HloInstruction* inst, ShapeIndex idx, int64 dim) -> HloInstruction* size
//   where `size` is a SCALAR S32 HLO evaluating to the actual (<= bound) extent.
```

Grounding — these CHECK strings are present verbatim in `hlo2penguin` `.rodata`:

```text
"Malformed shape proto: number of is_dynamic_dimension fields ("
"dynamic dimension must have size == kUnboundedSize or >= 0."
"Invalid dimension size %d, is_dynamic=%s"
"dimensions.size() == dynamic_dimensions.size()"
```

> **NOTE —** Neuron compiles **bounded** dynamic — it needs a finite buffer bound. Unbounded forms (`kUnboundedSize`) must be bounded earlier or are rejected by the validation surface in [§ Compatibility](#compatibility-and-validation-stock-xla). This is why "dynamic batch" reaches HLO not as N buckets but as a single bounded parameter plus a size scalar.

### Where the size scalar originates

```c
// Producer: kSetDimensionSize(operand, size_scalar, dimension=d)   [MHLO SetDimensionSizeOp]
//   returns the SAME tensor, dim d now marked dynamic, `size_scalar` registered.
// Consumer: kGetDimensionSize(operand, dimension=d) -> S32 scalar   [MHLO GetDimensionSizeOp]
//   yields the current runtime size of dim d.
```

For **entry parameters**, the size of a dynamic dim is itself passed as another scalar parameter, mapped by `xla::DynamicParameterBinding::Bind(DynamicSizeParameter, DynamicDimension)` — "param `p2` supplies the size for (param `p0`, index{}, dim d)". `DynamicDimensionInference` seeds its side table from this binding at module entry. **(CONFIRMED — `DynamicParameterBinding` (12 hits), `GetDimensionSizeOp`/`SetDimensionSizeOp` symbols, `set_dynamic_dimension_inference` evaluator-dependency string all present.)**

### The inference walk

`DynamicDimensionInferenceVisitor` is a `DfsHloVisitor` that, per opcode, computes which output dims are dynamic and composes the operands' size scalars into the output's. `HandleReshape` is the hard case (split/combine dimension groups, many lambdas); other handlers are `HandleConditional`, `HandleWhile`, `HandleParameter`, `HandleTuple`, `HandleInfeed`, `HandleAsyncStart`/`Done`, plus `DefaultAction`. After this pass, **every** HLO carrying a dynamic dim has a known S32 size scalar in the side table for `DynamicPadder` to consume. **(CONFIRMED — `DynamicDimensionInference` symbols; assert strings cite `xla/service/dynamic_dimension_inference.cc`.)** All `[STOCK]`.

---

## The Dynamic→Static Lowering `[STOCK XLA / MHLO]`

### Purpose

This is the pass that "turns dynamic shapes into compilable static shapes" inside the compiler — but **per execution, not by producing N buckets**. There are two upstream layers: an MHLO/StableHLO canonicalization layer that eliminates *not-actually-dynamic* ops, and the HLO `DynamicPadder` that makes the rest static-plus-masked.

### Layer A — StableHLO/MHLO `*NotActuallyDynamic` folds `[STOCK]`

`hlo2penguin`/`hlo-opt` link the full StableHLO + MHLO op universe, including the dynamic ops (`Dynamic{BroadcastInDim,Conv,Gather,Iota,Pad,Reshape,Slice,UpdateSlice}Op`, `RealDynamicSliceOp`, `Get/SetDimensionSizeOp` — all present as `HloToStablehloOpConverter<Op>` instantiations). The stock canonicalizations that *delete* dynamism that is provably static run via the symbolic-shape engine `stablehlo_ext::ShapeComponentAnalysis`:

```c
// confirmed canonicalization symbols (hlo2penguin):
DynamicReshapeOpNotActuallyDynamic        // static output shape  -> ReshapeOp
RemoveRedundantDynamicReshape             // fold chained / shape-of feeders
ShapeOfDynamicReshape                     // shape_of(dyn_reshape) -> its shape operand
DynamicBroadcastInDimOpNotActuallyDynamic // static result        -> BroadcastInDimOp
ChainedDynamicBroadcastInDimCanonicalization
DynamicBroadcastToOwnShape_{1,2,3,4}
materializeReshapeAsExpandAndCollapse(ShapeComponentAnalysis, ...)  // stablehlo_ext
```

**(CONFIRMED — `DynamicReshapeOpNotActuallyDynamic` (21 hits), `DynamicBroadcastInDimOp` (561), `ChainedDynamicBroadcastInDim` (23), `RealDynamicSliceOp` (468).)** None of these are Neuron-authored.

> **QUIRK —** the `*NotActuallyDynamic` naming is upstream XLA's, and it captures the whole philosophy: a great deal of apparent dynamism is, after symbolic-shape reasoning, statically resolvable, and the cheapest dynamic op is the one folded away before `DynamicPadder` ever runs. A reimplementer should run this layer *first*; what survives it is genuinely dynamic.

### Layer B — `xla::DynamicPadder::Run` `[STOCK]`

```c
function DynamicPadder_Run(module, exec_threads):   // xla::DynamicPadder::Run @0x2b7aff0
    // For each shape-dynamic op, make its output static while threading the
    // size scalars so the VALID region is preserved and the PADDED region is harmless.
    for inst in module:
        switch inst.opcode:
            case kReshape:  RewriteDynamicReshape(inst)       // @0x2b759a0 (+SingleGroup @0x2b73bb0,
                                                              //  CombineInput @0x2b720b0, SplitInput)
            case kConcat:   RewriteDynamicConcat(inst)        // @0x2b6c2c0
            case kSort:     RewriteDynamicSort(inst)          // @0x2b701c0
            case binary_op: RewriteDynamicBinaryOp(inst)      // @0x2b6d650
            ...
        if RequiresPadToStatic(inst, shape_index):            // boundary / entry-exit
            InsertPadToStaticOnInstruction(inst)              // kCustomCall "PadToStatic"

// the pad primitive every rewrite calls:
function PadWithScalar(inst, dim, dynamic_size, padding_scalar):  // (anon) @0x2b6bd00
    assert inst && dynamic_size && padding_scalar             // verbatim assert string present
    // write `padding_scalar` (0, or -inf for sort, etc.) into [dynamic_size : bound) of `dim`
    // via an iota>=dynamic_size mask + select; the live prefix is untouched.
```

`PadToStatic` (kCustomCall) splits a dynamic tensor into `(static-padded-data, size-scalar-0, size-scalar-1, …)`; `SliceToDynamic` is the inverse, reconstructing a dynamic-shaped tensor from static data + runtime sizes at outputs/boundaries. The on-device handshake is the **dynamic-shape metadata prefix**: a dynamic buffer is laid out `[metadata prefix: per-dim runtime sizes][padded data]`, with `PadToStatic` reading the prefix and `SliceToDynamic` writing it. **(CONFIRMED — `PadToStatic` (39), `SliceToDynamic` (11), `PadWithScalar` (4), `RewriteDynamicSort` (19); `RewriteDynamicConcat` is anon-namespace and inlines into `DynamicPadder` so it does not surface as an independent string — tagged STRONG, address from the appendix.)**

After `DynamicPadder`, `DynamicDimensionSimplifier::Run`, `DynamicIndexSplitter::Run` (scalarize dynamic indices), and `DynamicShapeRemovingVisitor` (drop dynamism where the consumer is a static-only backend op) run, all `[STOCK]`.

---

## The Neuron Handoff `[NEURON]`

### Purpose

This is the entire Neuron-authored delta in the dynamic path: one MLIR cleanup rewrite, a rejection guard, and the DMA-scratch sizing that the surviving runtime-offset slice feeds. Everything above is upstream; this section is the thin band AWS actually wrote.

### `CanonicalizeForTensorizer::replaceGetDimensionSize` `[NEURON]`

`CanonicalizeForTensorizer` (factory `mlir::createCanonicalizeForTensorizerPass`, StableHLO variant `createStableHLOCanonicalizeForTensorizerPass`) is the Neuron MHLO cleanup pass run before the Tensorizer. Its `runOnOperation` invokes, in order: `fuseIotaSort`, `canonicalizeTupleOp`, `replaceGetDimensionSize`, `replaceConvertsWithConstants`, `replaceInvalidBackendConfigs`, `replaceBroadcastInDimWithConstants`, `replaceBroadcastsWithBroadcastInDim`. Only `replaceGetDimensionSize` is dynamic-relevant:

```c
function replaceGetDimensionSize(func):              // CanonicalizeForTensorizer::replaceGetDimensionSize @0x2080240
    for op in func.walk(mhlo::GetDimensionSizeOp):   // mlir::detail::walk + ForwardIterator
        d     = op.getDimension()                    // GetDimensionSizeOp::getDimension
        shape = op.getOperand().getType()
                  .cast<RankedTensorType>().getShape()
        if shape[d] is STATIC:                        // not ShapedType::kDynamic
            c = mhlo::ConstantOp(DenseElementsAttr<S32>(shape[d]))  // ::build + getRawIntOrFloat
            op.getResult().replaceAllUsesWith(c)      // ResultRange::replaceAllUsesWith
            op.erase()                                // Operation::erase
        // else: a still-dynamic dim — KEEP the op (real runtime size scalar)
```

This **constant-folds** `GetDimensionSize` for statically-known dims before the Tensorizer, so the backend only ever sees a true runtime size scalar for dims that are genuinely dynamic. **(INFERRED-STRONG — the 26-callee set is exactly the MLIR API for "walk op, read dim, if static replace with constant, erase"; `replaceGetDimensionSize` (12 hits) and both pass factories confirmed.)**

> **GOTCHA —** the `else` branch is the trap. A reimplementer who folds *all* `GetDimensionSize` to a constant — including dims marked dynamic — will erase the runtime size scalar the backend's dynamic path depends on, silently producing a compiler that drops dynamic behavior. The fold is gated **strictly** on `shape[d]` being static; the dynamic case must fall through unmodified. The callee set includes a `report_fatal_error` path for `GetDimensionSize` on a non-tensor, so malformed input fails loudly rather than folding.

### The Tensorizer rejection guard `[NEURON]`

By the time the program reaches the Neuron `xla::hilo` HLO→Penguin bridge, **no shape-dynamic op may remain**. `DynamicPadder` has already removed every one; in `hilo` the handlers for `Get/SetDimensionSize`, `DynamicReshape`, `DynamicSlice`, `DynamicUpdateSlice` are wrapped in `NativeDfsHloVisitorWrapper<ShapeVerifier>` — **verification only, no lowering emitter**. A tensor that still carries a dynamic dim into the Tensorizer trips the guard:

```text
"Input tensor ${0} has dynamic shape"
```

**(CONFIRMED — verbatim string present in `hlo2penguin`; STRONG-Neuron, a Tensorizer-side rejection.)** The `dynamic-index-splitter` pass guarantees indices are scalar first — its diagnostic is present verbatim: `"…Make sure that 'dynamic-index-splitter' pass was exectuted to canonicalize the indices:"` (the upstream typo "exectuted" is reproduced, a fingerprint).

### What survives into Penguin, and the DMA handoff `[NEURON]`

```text
Penguin only ever receives:
  (a) fully-static tensors (bounded dims padded to bound), and
  (b) DynamicSlice / DynamicUpdateSlice whose SHAPE is static but whose OFFSET is a
      runtime scalar  — NOT shape-dynamic; slice size is a compile-time constant.
```

The surviving runtime-offset `DynamicSlice`/`DynamicUpdateSlice` (e.g. a KV-cache write at a runtime index) is the real "dynamic" thing Neuron materializes on device: a copy whose base address depends on a runtime register — the indirect-DMA / DGE path. Its SBUF scratch is sized by `xla::hilo::SetDynamicDMASbufBytes` @`0x1f930d0` (demangled symbol `_ZN3xla4hilo22SetDynamicDMASbufBytesERKj`) and the driver flags `dynamic-dma-sbuf-bytes` / `dynamic-dma-scratch-size-per-partition` (both confirmed verbatim). **(CONFIRMED — the function at `0x1f930d0` resolves to the demangled `xla::hilo::SetDynamicDMASbufBytes(unsigned int const&)`; the exact DGE level selection is the backend's subject.)** The runtime *size* scalar (for a bounded dynamic dim) and the runtime *offset* scalar (for a slice) are both handed to the backend as plain S32-valued HLO/Penguin values; the front end allocates no registers.

---

## End-to-End Pipeline (Dynamic Path)

```text
 HLO proto (bounded dynamic dims; DynamicParameterBinding gives size params)
   │  [STOCK] HloFunctionImporter::ImportInstructionImpl(…, DynamicShapeHandlingMode)
   ▼  -> MHLO (mhlo.set/get_dimension_size carry dynamism)
 [STOCK] DynamicDimensionInference (DfsHloVisitor): build (inst,idx,dim)->S32 size map
   ▼
 [STOCK] DynamicPadder::Run: PadWithScalar / RewriteDynamic{Reshape,Concat,Sort,BinaryOp};
   │      InsertPadToStaticOnInstruction (kCustomCall PadToStatic); SliceToDynamic
   ▼  -> static HLO + size scalars + PadToStatic/SliceToDynamic at boundaries
 [STOCK] DynamicDimensionSimplifier, DynamicIndexSplitter (scalarize dyn indices),
   │      DynamicShapeRemovingVisitor (ConvertToStatic/ConvertToDynamic at edges)
   ▼
 [STOCK] MHLO/StableHLO canonicalize-dynamism (*NotActuallyDynamic, etc.)
   ▼
 [NEURON] CanonicalizeForTensorizer: replaceGetDimensionSize (static-dim const-fold) + IO canon
   ▼
 [NEURON] TensorizerLegalizationPass -> xla::hilo HLO->Penguin
   │       (shape-dynamic ops already GONE; only static tensors + runtime-OFFSET slices remain)
   ▼
 Penguin IR:
   - shape-dynamic  => nothing left (everything bounded+padded)
   - runtime-offset => DynamicSlice/DUS -> DGE / indirect-DMA  (SetDynamicDMASbufBytes)
   - runtime trip   => backend dynamic for-loop (BirDynamicForAxis / InstDynamicForLoop)
   - scalar->reg    => backend symbolic-AP register materialization
```

### Reimplementation checklist (front end only)

```text
To reimplement neuronx-cc 2.24's dynamic front end you write NO bucketing.
Wire the upstream XLA passes in this order on a BOUNDED-dynamic HLO module:
  1. DynamicParameterBinding from entry signature (size params).        [STOCK]
  2. DynamicDimensionInference.                                          [STOCK]
  3. DynamicPadder (PadToStatic on entry/exit).                          [STOCK]
  4. DynamicDimensionSimplifier + DynamicIndexSplitter.                  [STOCK]
  5. DynamicShapeRemovingVisitor where backend is static-only.           [STOCK]
  6. (MHLO) stablehlo canonicalize-dynamism (*NotActuallyDynamic).       [STOCK]
  7. CanonicalizeForTensorizer.replaceGetDimensionSize: walk            [NEURON]
       GetDimensionSizeOp; if dim static -> mhlo.constant(S32, size), erase;
       else KEEP (real runtime size). (Do not fold dynamic dims — GOTCHA above.)
  8. TensorizerLegalizationPass + xla::hilo to Penguin; size SBUF        [NEURON]
       scratch with SetDynamicDMASbufBytes for surviving runtime-offset slices.
Validation: assert ShapeUtil::DynamicShapeIsCompatible(actual, bound) at boundaries;
  reject any tensor still carrying a dynamic dim ("Input tensor ${0} has dynamic shape").
```

### Explicit non-findings (do not claim these exist)

- No `bucket_limit` / `Bucket` class controlling shape compilation — only `summary.proto`.
- No "compile N static buckets from one dynamic shape" pass.
- No runtime bucket-selection / smallest-bucket-fit logic in the compiler binaries.
- "ccop bucketing" = collective combiner byte thresholds, **not** shapes.
- `num_dynamic_loop_bounds_` = XLA *CPU* backend parallel-loop bounds (low/high pairs, JIT fallback only) — **not** the Neuron device dynamic loop.

---

## Compatibility and Validation `[STOCK XLA]`

### `xla::ShapeUtil::DynamicShapeIsCompatible`

```c
function DynamicShapeIsCompatible(actual, bound) -> bool:   // @0x94d4f90 (196 B, 10 bb)
    // ForEachSubshape over both shapes; OK iff for every leaf subshape and dim i:
    //   element types equal, rank equal, AND
    //   if bound.is_dynamic_dimension(i): actual.dimensions(i) <= bound.dimensions(i)
    //   else (static):                    actual.dimensions(i) == bound.dimensions(i)
    // i.e. "the concrete shape FITS within the bounded/compact shape".
```

**(INFERRED-STRONG from the standard XLA impl; `ForEachSubshape` structure + per-dim predicate confirmed; symbol at `0x94d4f90`.)** Callers are runtime/transfer-boundary code (`TransferManager::ReadDynamicShapes`, `MutableLiteralBase::CopyFrom`, `cpu::CpuExecutable::ExecuteAsyncOnStream`), **not** the Neuron device-lowering path. The validation surface is a family of stock CHECK strings (e.g. `"Check failed: ShapeUtil::DynamicShapeIsCompatible(compact_shape, bound_shape)"`, `"AllToAll does not support bounded dynamic shapes"`, `"bitcast-convert is not valid for dynamic shape %s->%s"`, `"dynamic_reshape->operand(i)->shape().element_type() == S32"`). All are stock XLA except the Neuron `Input tensor ${0} has dynamic shape` guard documented above.

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary (the bucketing negative first):

1. **No compile-N-buckets mechanism exists (the headline negative).** Re-challenge: could a bucketing pass exist under a non-`bucket` name? Checked the *absences* directly — `candidate_shapes`, `num_bucket`, `bucketize`, `dynamic_batch`, `shape-bucket`, `auto-bucket`, and the regex `compile.*bucket` all return **0** in `hlo2penguin` (and `hlo-opt`). The one `max_batch` hit resolves to a convolution-partitioning assert, **not** bucketing. The positive path (`DynamicPadder` bounded-dynamic, single executable) is present and complete, leaving no room for a parallel bucket path. **Verdict: CONFIRMED.** A reimplementer writes zero shape-bucketing code.
2. **Every `bucket*` token is unrelated.** Re-challenge: enumerate, do not sample. The deduplicated grep returns exactly the [table set](#the-complete-bucket-enumeration); `bucket`/`bucket_limit` co-locate with `HistogramProto`/`summary_go_proto`; the mangled `ErrorBuckets`, `StringMapImpl::LookupBucketFor`, `FoldingSetBase::GrowBucketCount` are diff-tooling and LLVM hashtables; the ccop flags are byte-size collective thresholds. **Verdict: CONFIRMED.** No `bucket*` token is shape compilation.
3. **The dynamic→static lowering is `DynamicPadder`, stock.** `DynamicPadder` (44 hits), `PadToStatic` (39), `SliceToDynamic` (11), `PadWithScalar` (4) confirmed; the per-opcode rewrite addresses are in the appendix; `RewriteDynamicConcat` is anon-namespace/inlined (softened to STRONG). **Verdict: CONFIRMED (STOCK).**
4. **`replaceGetDimensionSize` is the Neuron delta and folds only static dims.** Symbol (12 hits) + both pass factories confirmed; the static-only gate is INFERRED-STRONG from the callee API set, and the `else`-keep behavior is flagged as the critical GOTCHA. **Verdict: STRONG (the static-only gate is inferred, not byte-traced).**
5. **The Tensorizer rejects still-dynamic input.** `"Input tensor ${0} has dynamic shape"` confirmed verbatim; `dynamic-dma-sbuf-bytes` / `dynamic-dma-scratch-size-per-partition` confirmed; the function at `0x1f930d0` resolves to the demangled `xla::hilo::SetDynamicDMASbufBytes(unsigned int const&)` in the function-address table (it is a hidden symbol absent from the strings sidecar, which is why a string grep alone misses it). **Verdict: CONFIRMED.**

No claim was fabricated; inferred items (`DynamicShapeIsCompatible` body, the `replaceGetDimensionSize` static-only gate) carry INFERRED-STRONG and are anchored to addresses and callee sets.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::DynamicPadder` `[STOCK]` | the dynamic→static lowering; the real "compile a dynamic shape" pass |
| `xla::DynamicDimensionInference` `[STOCK]` | builds the size-scalar side table feeding `DynamicPadder` |
| `CanonicalizeForTensorizer` `[NEURON]` | hosts `replaceGetDimensionSize`; the only Neuron dynamic-relevant MHLO rewrite |
| Collective combiners `[NEURON]` | own the `--internal-ccop-*-bucketing` *byte-size* flags — not shapes |
| `xla::hilo::SetDynamicDMASbufBytes` `[NEURON]` | sizes SBUF scratch for surviving runtime-offset slices |

## Cross-References

- [HLO / mhlo / stablehlo Ingestion & the Stock-vs-Neuron Boundary](hlo-ingestion-boundary.md) — the four classification markers and the `.text`/`.rodata` offset deltas these addresses resolve against
- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — what "ccop bucketing" actually is (byte-size collective coalescing)
- [DUS/DS Simplifier & DynamicSlice Mover](dus-ds-simplifier.md) — sibling treatment of runtime-index slices in the HLO passes
- [Pass Registry (the `--passes` table)](pass-registry.md) — where `DynamicPadder`, `DynamicIndexSplitter`, and `CanonicalizeForTensorizer` are registered
- Part 5 (Penguin IR & Middle-End) — the backend dynamic path: DGE / indirect-DMA for runtime-offset slices, `BirDynamicForAxis` / `InstDynamicForLoop` for runtime trip counts, and the symbolic-AP register materialization that turns a front-end S32 scalar into a hardware register
