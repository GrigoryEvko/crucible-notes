# Embedding Minibatching Decomposition

> *Every address, opcode, operand index, custom-call name, and source-line tag on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; not stripped — `nm -C` resolves every method). `.text` VMA equals its file offset (`0xe63c000`); `.rodata` at `0x84a0000`; `.data.rel.ro` is `VMA − 0x200000` (the operand-name tables, filled by `R_X86_64_RELATIVE` at load). Addresses apply to this build; other versions differ.*

## Abstract

This page is the **HLO-layer decomposition** that sits *above* the SC scan lowering: how the front-end's monolithic sparse-embedding-matmul custom-call (`SparseDenseMatmulWithMinibatchingOp`) is split into one inner `SparseDenseMatmulOp` **per minibatch per physical SparseCore core**, how each split's per-core CSR window is addressed, and how the result feeds the SC-dialect op set that the [SampleCombiner emitter](sample-combiner-emitter.md) and the [valency loop](emit-valency-loop.md) consume downstream. It is owned by three sibling HLO/MLIR passes in `xla::tpu::sparse_core`: **`MinibatchingDecomposition`** (the per-minibatch CSR-slice arithmetic + the forward/backward pass roster), **`EmbeddingDataFormattingDecomposer`** (the dense-per-table ↔ SC-packed-stacked-table activations/gradients adapter), and **`PackedOperandsLowering`** (the MLIR full-conversion pass that re-creates the dialect `sparse_core::SegmentedScanOp` on packed-width operands).

The single decisive structural fact — and the one that most often gets mis-read — is that the op named `DynamicSliceCsr` is **not** an HLO `kDynamicSlice` (`0x36`). `CreateDynamicSliceCsr` (`0x13489ea0`) emits **no** dynamic-slice opcode at all. It builds a `{sliced-csr, base-offset, padded-count}` 3-tuple out of a `GetCoreIndex` custom-call, a `GetPaddedRowCount` granule clamp, and a pure integer **multiply/add chain** (three `kMultiply`=`0x4b` plus one `kAdd`=`0x3`). The actual per-minibatch CSR row-pointer *window* is sliced by the inner `SparseDenseMatmulOp` custom-call the forward pass emits next, parameterised by that tuple. `DynamicSliceCsr`/`GetCoreIndex` are `SparseCoreOperationType` custom-call **names** (op-types `0x10` / `0xc`), not HLO opcodes.

The second decisive fact is the **division of labour across the three passes**. `MinibatchingDecomposition` is the *only* one that touches the CSR row-pointers — it is the segment-id provenance the SegmentedScan ultimately reduces over. `EmbeddingDataFormattingDecomposer` reformats *activations and gradients* between the dense per-table XLA layout and the SC packed stacked-table layout; it is **not** the CSR/id/gain operand reformatter. `PackedOperandsLowering` is the dialect-level op-packing pass that re-creates `SegmentedScanOp` (and ~40 other AluEp ops) on the target packed width via an unpack→create→pack rewrite. A reimplementer must keep these three concerns separate.

The page is three units plus the downstream binding: the **minibatching decomposition pipeline** (op recognition, `CreateDynamicSliceCsr`, the granule clamp, the forward/backward roster, while-fusion vs no-while), the **operand partition** (the per-core CSR base-offset arithmetic + the forward/backward operand-name tables), the **packed-operands lowering** (the `SegmentedScanOp` re-builder), and the **binding** into the gather→sort→uniquify→reduce→scatter SC Stream-op DAG the [DotCombiner emitter](sample-combiner-emitter.md) produces.

| | |
|---|---|
| **Decomposition pass** | `MinibatchingDecomposition::RunImpl` (`0x1348f940`) — scan ops by custom-call name, build ArgSpecs, dispatch forward/backward |
| **Op recogniser** | `minibatching_decomposer_util::IsSparseDenseMatmulWithMinibatchingOp` (`0x13c86da0`) — `IsCustomCall("…WithMinibatchingOp", 35)` OR `IsCustomCall("…GradOptimizerUpdateWithMinibatchingOp", 54)` |
| **Per-minibatch slice** | `MinibatchingDecomposition::CreateDynamicSliceCsr` (`0x13489ea0`, `0x8c0` B) → `{sliced-csr, base, padded}` 3-tuple. **No HLO `kDynamicSlice`.** |
| **Padded count** | `sparse_dense_matmul_decomposer_util::GetPaddedRowCount` (`0x13c90280`) = `max( max(GranuleBytes/4, num), cfg[+0x948]→[+0x94] )`, gated by `SupportsSparseCore()` |
| **Custom-call op-types** | `GetCoreIndex` = `0xc`, `DynamicSliceCsr` = `0x10` (`SparseCoreOperationTypeToString` `0x14b7f480`) |
| **HLO opcodes emitted** | `kMultiply` = `0x4b` (×3), `kAdd` = `0x3` (×1) — verified against `StringToHloOpcode` init (`0x1e5ef040`) |
| **Decomposed inner op** | `GetSparseDenseMatmulOpCustomCallTarget` (`0x13c86e60`) → `"SparseDenseMatmulOp"` (19 B) |
| **Data-format adapter** | `EmbeddingDataFormattingDecomposer::RunImpl` (`0x1368b4a0`) — op-types `0x1a..0x1d`; Sc/Tc gated by `EnableEmbeddingDataFormattingOffload` |
| **Packed-op lowering** | `ScanOpLowering<SegmentedScanOp>::matchAndRewrite` (`0x135f3000`) — unpack→`getReductionOp`→`SegmentedScanOp::create`→pack |
| **Downstream emitter** | `SparseDenseMatmulDotCombinerEmitter::Emit` (`0x1332bda0`) → `EmitValencyLoop` / `EmitVectorizedLoop` / `EmitSampleCombiner` |
| **Confidence** | CONFIRMED (decompile + binary-byte anchored) unless a row or callout says otherwise |

---

## Unit 1 — The Minibatching Decomposition Pipeline

### Why minibatching exists

A TPU-embedding lookup produces, per training step, a concatenated CSR (compressed-sparse-row) structure describing which embedding-table rows each sample touches. When the global lookup is too large to process in one pass, the front end tags the custom-call as a *minibatching* op: the lookup is to be processed in several sub-batches, each handled independently, with the results recombined. `MinibatchingDecomposition` is the HLO pass that turns that one tagged custom-call into one concrete inner `SparseDenseMatmulOp` **per (minibatch, physical SC core)** pair — and that per-pair op is the unit the SC scan datapath actually emits a sequencer program for.

The structurally important point is that the decomposition is purely an HLO graph rewrite. It emits no SC dialect, no SPMEM traffic, no sequencer instructions — it produces a tree of HLO custom-calls and arithmetic whose leaves are the per-minibatch inner ops. The SC-dialect lowering happens *later*, when each inner `SparseDenseMatmulOp` is itself lowered (Unit 4).

### Op recognition — by custom-call name

`RunImpl` (`0x1348f940`) walks the module and recognises its target op by **custom-call target string**, not by opcode or attribute. The recogniser `IsSparseDenseMatmulWithMinibatchingOp` (`0x13c86da0`) is a two-name disjunction, byte-confirmed from the decompiled body:

```c
// IsSparseDenseMatmulWithMinibatchingOp (0x13c86da0) — decompile-exact
bool IsSparseDenseMatmulWithMinibatchingOp(const HloInstruction *op) {
    if (op->IsCustomCall("SparseDenseMatmulWithMinibatchingOp", 35))           // forward
        return true;
    return op->IsCustomCall("SparseDenseMatmulGradOptimizerUpdateWithMinibatchingOp", 54);  // backward
}
```

Once matched, `GetArgSpec` (`0x13c87040`) selects the per-op operand spec — `ForwardPassArgSpec` (vtable `0x21937cc8`) vs `BackwardPassArgSpec` (vtable `0x219382c0`) — and `GetSparseDenseMatmulOpCustomCallTarget` (`0x13c86e60`) maps the matched *minibatching* op onto the **decomposed inner op name**. The mapping, read from the decompiled string-assignment arms:

| Matched custom-call (length B) | Decomposed inner op |
|---|---|
| `SparseDenseMatmulWithMinibatchingOp` (35) | `SparseDenseMatmulOp` (19) |
| `SparseDenseMatmulGradOptimizerUpdateWithMinibatchingOp` (54) | grad-with-optimizer-update inner (`kSparseDenseMatmulGradOpWithOptimizerUpdate`) |
| `SparseDenseMatmulCustomCombinerMegachipOp` (41) | `SparseDenseMatmulCustomCombinerOp` |
| `…CustomCombinerTcCombinerMegachipOp` | `…CustomCombinerTcCombinerOp` |

> **NOTE — recognition is string-keyed, so the op vocabulary is a closed set of `.rodata` strings.** The match is `HloInstruction::IsCustomCall(target_string, length)` against literal byte strings, not an enum compare. A reimplementer reproduces this by string-matching the custom-call target; the megachip/custom-combiner variants are additional literal strings in the same recogniser family.

### `CreateDynamicSliceCsr` — the per-minibatch slice descriptor (NOT a dynamic-slice)

`CreateDynamicSliceCsr` (`0x13489ea0`) is the heart of the partition. It is named for the *SC custom-call* it produces, not for any HLO dynamic-slice opcode. Its job: for one minibatch, build a `{sliced-csr, base-offset, padded-count}` 3-tuple that the inner `SparseDenseMatmulOp` will use to read exactly its slice of the concatenated CSR row-pointers. The decompiled HLO-builder call sequence, in emit order:

```text
CreateDynamicSliceCsr (0x13489ea0)  — decompile-confirmed op DAG, in order
  guard  RetCheckFail "max_ids_per_partition > 0"   (minibatching_decomposer.cc:154) if num <= 0
  0  padded = GetPaddedRowCount(target, num)         // 0x13c90280  — granule clamp (see below)
  1  C_pad  = CreateConstant( LiteralUtil::CreateR0<int>(padded) )   // s32 scalar
  2  GCI    = CreateCustomCall(s32[1], "GetCoreIndex", {})           // op-type 0xc; runtime per-core index
  3  mul1   = CreateBinary(s32[1], MULTIPLY/*0x4b*/, a6,    C_pad)   // a6 * padded
  4  mul2   = CreateBinary(s32[1], MULTIPLY/*0x4b*/, GCI,   mul1)    // GCI * a6 * padded
  5  mul3   = CreateBinary(s32[1], MULTIPLY/*0x4b*/, C_pad, a7)      // padded * a7
  6  base   = CreateBinary(s32[1], ADD/*0x3*/,       mul2,  mul3)    // per-core CSR base offset = padded·(GCI·a6 + a7)
  7  dsc    = CreateCustomCall(…, "DynamicSliceCsr", {csr, base, C_pad})  // op-type 0x10; 3 operands
  8  gte    = CreateGetTupleElement(shape, dsc, 0)
  9  tuple  = CreateTuple({ gte, base, C_pad })       // StatusOr<HloInstruction*> 3-tuple
```

The three multiplies are opcode `0x4b` (MULTIPLY) and the single add is `0x3` (ADD), both read against the alphabetical `StringToHloOpcode` init table (`0x1e5ef040`), where `add`=`0x3`, `multiply`=`0x4b`, and `dynamic-slice`=`0x36`. **`0x36` is never emitted here** — there is no HLO dynamic-slice anywhere in this function. The shapes are built with `ShapeUtil::MakeValidatedShape(S32 /*PrimitiveType 4*/, …)`.

> **CORRECTION — "DynamicSlice + Binary + CustomCall" is structurally wrong.** An earlier structural read described this as an HLO dynamic-slice plus a binary plus a custom-call. The decompiled body emits **only** `kMultiply` (×3) and `kAdd` (×1) as HLO opcodes, plus `Constant`/`CustomCall`/`GetTupleElement`/`Tuple`. The `"DynamicSliceCsr"` string is a `SparseCoreOperationType` custom-call name (op-type `0x10`), not `HloOpcode::kDynamicSlice`. The per-minibatch CSR row-pointer window is sliced *inside* the inner `SparseDenseMatmulOp`, parameterised by the `{base, padded}` this tuple carries.

> **NOTE — the early-return guard string.** The `num <= 0` guard `RetCheck`s with the message `"max_ids_per_partition > 0"` (`platforms/xla/sparse_core/hlo/minibatching_decomposer.cc:154`), and a second `RetCheck` on the inner-call result checks `"concatenated_csr_tuple.size() > kCsrTupleRowPtrIndex"` (line 177). Both strings are read directly from the decompiled body. (An earlier note phrased the first guard as `"num<=0"`; the real `.rodata` string is `max_ids_per_partition > 0` — corrected here.)

### `GetPaddedRowCount` — the granule clamp

`GetPaddedRowCount` (`0x13c90280`) computes the per-segment fixed stride the SegmentedScan operates over: it rounds the minibatch row count up to a granule of `int32` words and floors it at a per-target minimum. The decompiled body is short enough to be exact:

```c
// GetPaddedRowCount (0x13c90280) — decompile-exact
int GetPaddedRowCount(const Target &t, int num) {
    uint64_t granule = t.GranuleBytes();                 // vtable[+0x5c0] (0x1d617f80)
    if (!t.SupportsSparseCore())                         // vtable[+0x260]
        LOG(FATAL) << "SparseCore is not supported by this target";  // target.h:1709
    uint64_t r = granule >> 2;                           // GranuleBytes / sizeof(int32) = i32 words/granule
    if (num > (int)r) r = num;                           // r = max(granule/4, num)
    int cfg = *(int*)( *((long*)&t + 297) + 148 );       // t[+0x948] -> [+0x94]  (per-target min-row config)
    if ((int)r <= cfg) r = cfg;                          // r = max(r, cfg)   <-- floor, not cap
    return (int)r;
}
```

So the padded row count is `max( max(GranuleBytes/4, num), cfg )` — a granule-aligned floor on the per-minibatch row count.

> **CORRECTION — the config bound is a floor (`max`), not a cap (`min`).** An earlier reading described the final step as `min(r, cfg-max)`, i.e. the config field as an upper cap. The decompiled comparison is `if ((int)r <= cfg) r = cfg`, which raises `r` *up* to `cfg` when `r` is below it — a **lower** bound. The padded count is therefore `max(max(GranuleBytes/4, num), cfg)`. The `SupportsSparseCore()` gate and the `target.h:1709` `LOG(FATAL)` are confirmed unchanged. The semantic identity of the `cfg` field (`[Target+0x948]→[+0x94]`) is read structurally — it is a per-target row-count config, but its proto field *name* was not decoded (INFERRED).

### The forward / backward pass roster

For each matched op, `DecomposeForwardPass` (`0x1348a9c0`) — and its grad twin `DecomposeBackwardPass` (`0x1348b600`) — emits the per-minibatch inner ops. Per minibatch the forward pass: reads core counts via `GetNumSparseCores` (`0x13c9eba0`) + `GetCores` (`0x14b79900`); reads the division-level field from the `SparseDenseMatmulConfig` globals (`0x223a95b8`, field `+0x30`); calls `CreateDynamicSliceCsr`; emits the inner `SparseDenseMatmulOp` via `CreateCustomCall`; and pulls the per-minibatch result with `CreateGetTupleElement`. Two emission modes exist, dispatched by the top-level driver:

| Pass | Address | Role |
|---|---|---|
| `RunImpl` | `0x1348f940` | scan ops, build ArgSpecs, dispatch |
| `IsSparseDenseMatmulWithMinibatchingOp` | `0x13c86da0` | `IsCustomCall(name)` recogniser |
| `GetArgSpec` | `0x13c87040` | → `ForwardPassArgSpec` / `BackwardPassArgSpec` |
| `GetSparseDenseMatmulOpCustomCallTarget` | `0x13c86e60` | minibatching → decomposed inner op name |
| `CreateDynamicSliceCsr` | `0x13489ea0` | per-minibatch `{base, padded}` tuple |
| `GetPaddedRowCount` | `0x13c90280` | granule clamp |
| `CreateAddComputation` | `0x13c90320` | builds the `"add"` reduction HLO computation |
| `DecomposeForwardPass` | `0x1348a9c0` | per-minibatch forward emit |
| `DecomposeBackwardPass` | `0x1348b600` | per-minibatch grad emit |
| `DecomposeForwardPassesWithNoWhileLoop` | `0x1348c720` | **inline** forward (minibatches unrolled) |
| `DecomposeForwardPassesWithWhileFusion` | `0x1348cd60` | **while-loop** forward |
| `DecomposeBackwardPassesWithNoWhileLoop` / `…WhileFusion` | `0x1348ca40` / `0x1348e260` | backward analogues |
| `DecomposeSparseDenseMatmulWithMinibatchingWithWhileFusion` | `0x1348f200` | top-level while-fusion driver |
| `CombineParamsIntoTupleAndUpdateOutputShape` | `0x13c87260` | while-carry tuple (`CreateTuple`) |
| `CreateInitialWhileLoopInductionVar` | `0x1348a760` | while induction var |

> **GOTCHA — two emission modes, same descriptor.** The **no-while** mode (`0x1348c720`) emits each minibatch's inner op inline (unrolled); the **while-fusion** mode (`0x1348cd60`) wraps them in an HLO `while` loop whose carry tuple is assembled by `CombineParamsIntoTupleAndUpdateOutputShape` (`0x13c87260`) and whose induction var is seeded by `CreateInitialWhileLoopInductionVar` (`0x1348a760`). Both consume the *same* `CreateDynamicSliceCsr` `{base, padded}` descriptor — the mode only changes how the per-minibatch bodies are laid out in the graph, not the per-minibatch slice arithmetic. A reimplementer must produce both forms or accept that a fixed-trip-count `while` is the canonical large-batch shape.

---

## Unit 2 — The Operand Partition

### The per-core CSR base offset

The whole point of the multiply/add chain in `CreateDynamicSliceCsr` is to give each physical SparseCore a **contiguous, padded window** into the single concatenated `concatenated_csr_pointers` operand. The window's start is the `base` value; its length is `padded`. The register-identity dataflow, byte-confirmed:

| symbol | expression | provenance |
|---|---|---|
| `padded` | `max( max(GranuleBytes/4, num), cfg )` | `GetPaddedRowCount`; `num` = arg int |
| `C_pad` | const `s32` = `padded` | `CreateConstant` |
| `GCI` | custom-call `"GetCoreIndex"` → `s32[1]` | runtime per-physical-SC index (op-type `0xc`) |
| `mul1` | `b * C_pad` | `b` = `HloInstruction*` arg (≈ minibatch index, **INFERRED**) |
| `mul2` | `GCI * mul1` = `GCI · b · padded` | |
| `mul3` | `C_pad * GCI` = `padded · GCI` | |
| `base` | `mul2 + mul3` = `GCI·b·padded + padded·GCI` = `padded · GCI · (b + 1)` | the per-core CSR window base offset |

So each physical SparseCore reads a contiguous `padded`-length window of the concatenated row-pointers starting at `padded · GCI · (b + 1)`. The `csr` operand (an `HloInstruction*` arg) is consumed by the `"DynamicSliceCsr"` custom-call as its first of three operands when present, and the `{sliced-csr, base, padded}` is wrapped into the returned 3-tuple.

> **NOTE — operand identity of `b` is structural.** The two `HloInstruction*` args to `CreateDynamicSliceCsr` (call them `a` and `b`) were read from the `DecomposeForwardPass` call-site register convention (`r8`=`a`, `r9`=`b`), not from a named accessor. `b` is inferred to be the minibatch index operand and `a` the csr operand. The register-identity dataflow inside `CreateDynamicSliceCsr` is CONFIRMED byte-exact; which SSA value is the per-table vs per-minibatch index is **INFERRED**. The `SparseDenseMatmulConfig` field `[+0x30]` that the forward pass reads (≈ `FLAGS_xla_sparse_core_minibatch_max_division_level`, `0x222bd280`) is a CONFIRMED byte read whose proto field *name* is INFERRED by flag adjacency.

### The decomposed operand-name tables

The operand layout the decomposition produces is fixed by two `.data.rel.ro` name tables, reloc-resolved. The forward pass has 7 operands, the backward pass 9 (entry 8 repeats the csr pointers). Operand 0, `concatenated_csr_pointers`, is the segment-id source that `CreateDynamicSliceCsr` slices per minibatch and that the SegmentedScan ultimately reduces over:

```text
ForwardPassArgSpec::kForwardPassOperandNames (0x21937d80, 7 entries):
  0  concatenated_csr_pointers          <- segment-id source (per-minibatch sliced by CreateDynamicSliceCsr)
  1  concatenated_embedding_ids         (gather indices = sorted_token_ids)
  2  concatenated_sample_ids            (output rows)
  3  concatenated_gains                 (combiner weights — the per-id gain the DotCombiner applies)
  4  num_mini_batches_per_sparse_core   (scalar)
  5  embedding_table
  6  activations_init

BackwardPassArgSpec::kBackwardPassOperandNames (0x21938320, 9 entries):
  0..4  same as forward
  5     tables
  6     gradients
  7     hyperparameters                 (SGD / Adam / Ftrl / Adagrad / AdagradMomentum families)
  8     concatenated_csr_pointers        (repeated)
```

The four `concatenated_*` operands map directly onto the SC embedding sum-lookup roles: `csr_pointers` → segment boundaries (the SegmentedScan's reset points), `embedding_ids` → the gather indices, `sample_ids` → output rows, `gains` → the per-id multiplicative weight the [DotCombiner FMA](sample-combiner-emitter.md) applies verbatim.

| Table | Address | Entries | Confidence |
|---|---|---|---|
| `kForwardPassOperandNames` | `0x21937d80` | 7 | CONFIRMED (reloc addends resolved; strings present in binary) |
| `kBackwardPassOperandNames` | `0x21938320` | 9 | CONFIRMED |

---

## Unit 3 — Packed-Operands Lowering (the SegmentedScanOp re-builder)

Once the inner `SparseDenseMatmulOp` ops exist, the SC dialect `sparse_core::SegmentedScanOp` they lower to must be re-created on **packed-width** operands (the SC vector engines pack sub-byte / bf16 data). `PackedOperandsLowering` (`runOnOperation` `0x135d8520`, ctor `CreatePackedOperandsLoweringPass` `0x135d82a0`) is the MLIR full-conversion pass that does this. It builds a `ConversionTarget` with per-op dynamic-legality callbacks (`setLegalityCallback` `0x1c957e40` / `0x1c958640`) plus a `RewritePatternSet`, then runs `mlir::applyFullConversion` (`0x1c958ac0`). An op is **legal** iff its operands are already at the target packed width; otherwise the matching rewrite wraps it `Unpack → op → Pack`.

The `SegmentedScanOp` arm is registered by `AddDynamicallyLegalScanOps<SegmentedScanOp>` (legality lambda `0x135f3920`) → `ScanOpLowering<SegmentedScanOp,SegmentedScanOp>::matchAndRewrite` (`0x135f3000`). The byte-confirmed rewrite body:

```text
ScanOpLowering<SegmentedScanOp>::matchAndRewrite (0x135f3000)  — decompile-confirmed
  1   ReductionOp = SegmentedScanOp::getReductionOp()           // 0x145fd460 — read reduction_op StringAttr
  2   UnpackOperand<UnpackFOp>(data, …)                         // 0x1360fac0 — split bf16/sub-byte data (F path)
  2'  UnpackOperand<UnpackUIOp>(segment-id, …)                  // 0x136104e0 — split (unsigned-int path)
  3   newop = SegmentedScanOp::create(b, loc, T, data, seg, reduction_op)  // 0x145fd5a0 — re-create on packed ops
  4   PackResults<PackFOp>(…)                                   // 0x13610940 — re-pack F results
  4'  PackResults<PackUIOp>(…)                                  // 0x13610de0 — re-pack UI results
```

This is the dialect `SegmentedScanOp` **builder**: it produces the op that the SC scan lowering (the segmented-scan emission, [scan datapath](scan-datapath.md)) then turns into a sequencer intrinsic. The same pass legalizes ~40 other AluEp arith/math ops (`AddF/I`, `MulF/I`, `DivF`, `Max/Min`, `CmpF/I`, `Exp`, `Tanh`, `Rsqrt`, `Clamp`, …) each with its own `Unpack{F,SI,UI}` / `Pack{F,SI,UI}` pair — `SegmentedScan`/`Scan` are two members of that packing table. The plain (non-segmented) `ScanOp` arm is `0x135f2580` (legality lambda `0x135f2f60`).

> **NOTE — the partition feeds the scan, not the reverse.** The CSR partition (Unit 2) decides *which* row-pointers each minibatch's SegmentedScan reduces over; `PackedOperandsLowering` decides *what packed width* that SegmentedScan's operands carry. They are orthogonal: the partition is HLO-level index arithmetic, the packing is dialect-level operand-width legalization. A reimplementer runs the minibatching decomposition first (producing per-minibatch inner ops over CSR slices), then the packed-operands lowering (re-creating each SegmentedScan on packed operands).

### The activations / gradients layout adapter

`EmbeddingDataFormattingDecomposer` (`RunImpl` `0x1368b4a0`) is the third pass — and the one most easily confused with the operand partition. It is **not** the CSR/id/gain reformatter. It reformats the dense per-table *activations* (forward output) and *gradients* (backward input) between the dense XLA tensor layout and the SC packed stacked-table layout. It matches four `SparseCoreOperationType` custom-calls:

| op-type | name | direction |
|---|---|---|
| `0x1a` | `SparseActivationsUnstack` | SC packed → per-table dense (forward output) |
| `0x1b` | `SparseActivationsUnstackInterleaved` | as above, interleaved |
| `0x1c` | `SparseGradientsStack` | per-table dense → SC packed (backward input) |
| `0x1d` | `SparseGradientsStackInterleaved` | as above, interleaved |

Each dispatches on `EnableEmbeddingDataFormattingOffload(GetTpuCompEnv(op))` (`0x1d6b94a0` / `0x1d73de80`): `true` → the **Sc** (on-device SparseCore) variant; `false` → the **Tc** (host/TensorCore) variant. The Sc unstack (`DecomposeActivationsUnstackSc` `0x13682d40`) splits the packed stacked-table activation into per-table dense rows via `CreateSlice` + `CreateReshape` + `CreateConvert` + `CreateTuple`, driven by `StackedTableConfig::Extract` (`0x13681160`), sized by `ElementPackingFactor` (`0x1d6b03e0`) × `NumEmbeddingDevices` (`0x1d6b8a00`). The Sc stack (`DecomposeGradientsStackSc` `0x13684d40`) is the inverse: per-table dense grads → packed stacked grad via `CreateConcatenate` + `CreatePad` (pad to stacked extent) + `CreateSlice` + `CreateConvert` + `CreateUnary`.

> **CORRECTION — this pass does not feed the SegmentedScan operands.** It would be natural to assume the "data formatting decomposer" reformats the CSR/id/sample/gain operands that the SegmentedScan reads. It does **not**. The CSR→segment-id provenance is `MinibatchingDecomposition` (Unit 1). `EmbeddingDataFormattingDecomposer` only touches activations/gradients (the dense ↔ packed-stacked-table layout adapter). Keep the two passes' operand domains separate.

---

## Unit 4 — Binding: from inner op to the Stream-op DAG

Each per-minibatch inner `SparseDenseMatmulOp` the decomposition emits is itself lowered into an SC-dialect **gather → sort → uniquify → segmented-reduce → scatter** Stream-op DAG. That lowering is *not* done by any of the three passes above — it is the job of `SparseDenseMatmulDotCombinerEmitter::Emit` (`0x1332bda0`, via `LoweringEmitter::EmitSparseDenseMatmulDotCombiner` `0x131a7ca0`), which splits into `EmitValencyLoop` (`0x1332cee0`), `EmitVectorizedLoop` (`0x1332e1c0`), and `EmitSampleCombiner` (`0x1332c640`). The DAG it produces:

| stage | SC dialect op | role |
|---|---|---|
| gather | `IndirectStreamStartOp` / `IndirectVectorStreamStartOp` (build `0x145cf440`) | indirect HBM→SPMEM embedding-row load keyed by sorted token ids |
| sort | `SortOp` (build `0x14604480` / `0x146046c0`) | lexicographic sort of `(sample, token)` ids |
| uniquify | `UniqueOp` / `UniqueWithLaneIdsOp` + `DuplicateCountOp` / `…WithLaneIdsOp` | dedup token ids, count dups per lane |
| reduce | `SegmentedScanOp` (`reduction_op="sum"`) | per-segment sum-scan, reset at each CSR boundary |
| scatter | `IndirectStreamAddStartOp` / `IndirectVectorStreamAddStartOp` (build `0x145d4420`) | scatter-ADD to HBM (forward drain / backward grad accumulate) |

The chain is: **`MinibatchingDecomposition`** produces the per-minibatch inner ops over CSR slices → **`PackedOperandsLowering`** re-creates each `SegmentedScanOp` on packed operands → the **`DotCombiner` emitter** wires the gather/sort/uniquify/scatter around that scan → `LowerToSparseCoreLlvm` lowers the dialect to the sequencer program. The `concatenated_gains` operand (Unit 2 operand 3) is the per-id multiplicative weight the [DotCombiner FMA](sample-combiner-emitter.md) applies; the `concatenated_csr_pointers` (operand 0) is the segment boundary the [valency loop](emit-valency-loop.md) reads as its per-sample trip count.

A separate, non-minibatch path exists for the gather-mul-scatter form: `GatherMulScatterSparseDenseMatmulOpDecomposer` (`0x13c861e0`) with the `GatherEmitter::ScatterOperandSlicesToHbm{,ForSortedIndices,ForChunkGather,ForColumnWiseGather}` family (`0x138e6c80` …). That is the symmetric non-CSR lowering and is out of scope here.

> **NOTE — the deep emitter wiring is surveyed, not instruction-decoded here.** The per-instruction gather/scan/scatter wiring inside `EmitVectorizedLoop` / `EmitSampleCombiner` — which `IndirectStream` variant per dtype, SPMEM tile sizes, `Sfence`/`TileBarrier`/`SyncAdd` placement, and the `GetScheduleType` (`0x131d5300`) valency-vs-vectorized selection — is owned by [EmitValencyLoop](emit-valency-loop.md) and [SampleCombiner Emitter](sample-combiner-emitter.md). The DAG *shape* (the five stages above) is CONFIRMED by op `::build`/`::create` presence; the instruction sequence inside the loops is documented on those pages.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Op recognition is two-name `IsCustomCall` (`…WithMinibatchingOp` 35 B OR `…GradOptimizerUpdate…` 54 B) | `0x13c86da0` decompile (both string literals + lengths) | CONFIRMED |
| Decomposed inner op name = `"SparseDenseMatmulOp"` (19 B); custom-combiner family mapped | `0x13c86e60` decompile string-assignment arms | CONFIRMED |
| `CreateDynamicSliceCsr` emits NO HLO `kDynamicSlice`; only `kMultiply` (×3) + `kAdd` (×1) as opcodes | `0x13489ea0`: `CreateBinary(…,75,…)` ×3 + `CreateBinary(…,3,…)` ×1; no `0x36` | CONFIRMED |
| `GetCoreIndex` / `DynamicSliceCsr` are SC custom-call names (op-types `0xc` / `0x10`) | `CreateCustomCall` sites + `SparseCoreOperationTypeToString` `0x14b7f480` | CONFIRMED |
| Op DAG order: `GetPaddedRowCount → Constant → CustomCall(GCI) → 3×Mul → Add → CustomCall(dsc,3 ops) → GTE → Tuple` | `0x13489ea0` decompile call order | CONFIRMED |
| Early-return guard string = `"max_ids_per_partition > 0"` (line 154), inner check `"…size() > kCsrTupleRowPtrIndex"` (177) | `0x13489ea0` `RetCheckFailSlowPath` args | CONFIRMED |
| `GetPaddedRowCount` = `max(max(GranuleBytes/4, num), cfg[+0x948]→[+0x94])`, gated by `SupportsSparseCore()` | `0x13c90280` full body | CONFIRMED |
| `GetPaddedRowCount` config bound is a **floor** (`max`), not a cap (`min`) | `if ((int)r <= cfg) r = cfg` raises `r` up to `cfg` | CORRECTED (was read as `min`) |
| Granule term = `GranuleBytes >> 2` (= bytes / `sizeof(int32)`); `LOG(FATAL)` at `target.h:1709` | `0x13c90280`: `v2 >> 2`, message string | CONFIRMED |
| Per-core CSR base = `padded · GetCoreIndex · (b + 1)` | TABLE B register-identity dataflow | CONFIRMED (arithmetic); `b`=minibatch index INFERRED |
| Forward 7 / backward 9 operand-name tables; operand 0 = `concatenated_csr_pointers` | `0x21937d80` / `0x21938320` reloc-resolved; strings present in binary | CONFIRMED |
| `CreateDynamicSliceCsr` 6th-arg `cfg[+0x30]` ≈ `FLAGS_xla_sparse_core_minibatch_max_division_level` | byte read CONFIRMED (`[rcx+0x30]`); proto field name not decoded | INFERRED (name only) |
| Two emission modes: no-while (inline) vs while-fusion; both use the same slice descriptor | `0x1348c720` / `0x1348cd60`; `CombineParamsIntoTuple` `0x13c87260` + induction var `0x1348a760` | CONFIRMED |
| `PackedOperandsLowering` re-creates `SegmentedScanOp` via unpack→`getReductionOp`→`create`→pack | `0x135f3000` decompile (UnpackF/UnpackUI, getReductionOp, create, PackF/PackUI) | CONFIRMED |
| `applyFullConversion` driver + per-op dynamic legality; ~40 AluEp ops in the same packing table | `0x135d8520` (`applyFullConversion` `0x1c958ac0`, `setLegalityCallback`) | CONFIRMED |
| `EmbeddingDataFormattingDecomposer` reformats activations/gradients (NOT CSR/id/gain operands) | `0x1368b4a0` op-types `0x1a..0x1d`; Sc/Tc dispatch on `EnableEmbeddingDataFormattingOffload` | CONFIRMED |
| Inner op lowers to gather→sort→uniquify→reduce→scatter Stream DAG via `DotCombinerEmitter::Emit` | `0x1332bda0`; op `::build` presence (`0x145cf440` / `0x14604480` / `0x145d4420`) | CONFIRMED (DAG shape); per-instruction wiring surveyed only |

---

## Cross-References

- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the embedding data path this decomposition opens.
- [SparseCore Hardware Architecture](architecture.md) — engine roles, the 4:1 SC:TC ratio, and the physical-core geometry `GetCoreIndex` indexes.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the twelve-pass SC-MLO pipeline the dialect ops produced here are lowered through (and the MEGACORE barrier).
- [SC Core Selection](sc-core-selection.md) — the physical-core selection policy that decides which SC cores each collective occupies (the counterpart to this page's per-core `GetCoreIndex` partition).
- [SampleCombiner Emitter](sample-combiner-emitter.md) — the per-sample gather-multiply-accumulate emitter that lowers each inner `SparseDenseMatmulOp`; consumes the `concatenated_gains` operand this decomposition partitions.
- [EmitValencyLoop](emit-valency-loop.md) — the per-id scalar loop whose trip count is the per-sample CSR segment length the `concatenated_csr_pointers` operand defines.
- [SC Scan Datapath](scan-datapath.md) — the segmented-scan emission the re-created `SegmentedScanOp` lowers into downstream.
- [Stream Gather / Scatter](stream-gather-scatter.md) — the `IndirectStream*StartOp` gather/scatter-add primitives the inner-op lowering issues.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function the lowered bundles route through.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
