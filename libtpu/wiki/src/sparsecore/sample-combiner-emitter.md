# SampleCombiner Emitter

> *Every emitter address, op-creation sequence, source-line tag, and operand-identity claim on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`) — from the decompiled bodies of `SparseDenseMatmulDotCombinerEmitter::{Emit, EmitSampleCombiner, EmitValencyLoop, EmitVectorizedLoop}` and the `GetCurrentLocation` source-line strings embedded in each. The `.text` VMA equals its file offset (`0xe63c000`). Addresses apply to this build; other versions differ.*

## Abstract

The **SampleCombiner emitter** is the inner-loop body of `SparseDenseMatmulDotCombinerEmitter` — the lowering that turns one embedding-lookup sample (a variable-length list of table-row ids with per-id gains) into one dense output vector. It is the *reduce* stage of the embedding datapath: where [stream gather/scatter](stream-gather-scatter.md) moves rows between HBM and `TILE_SPMEM` and [VEX](vectorextended-vex.md) is the cross-lane scan engine, this emitter is the per-sample gather-multiply-accumulate (GMA) loop that combines a sample's rows into its activation row. The decisive structural fact is that the three emitters form a **strict three-level loop nest** — `EmitSampleCombiner` ⊃ `EmitValencyLoop` ⊃ `EmitVectorizedLoop` — not a set of alternatives chosen at run time. There is no branch between them; each unconditionally calls the next.

The second decisive fact is the **combiner roster collapses to one code path**. The reduction this emitter performs is *always* a weighted sum: for each id in the sample's CSR segment, gather its embedding row, broadcast the id's gain to a feature-width vector, and fuse-multiply-add `row · gain` into a zeroed SPMEM accumulator. The three named pooling combiners — `sum`, `mean`, `sqrtn` — are **not** distinct code paths and **not** a backend-config field. They differ only in the per-id gain value the front-end (the TF/JAX TPU-embedding layer above the XLA custom-call) folds into the `sorted_gains` operand: `sum` leaves the gain unchanged, `mean` pre-scales it by `1/valency`, `sqrtn` by `1/sqrt(valency)`. The emitter applies whatever gain it is handed, verbatim, as a multiplicative scale. The general (non-pooling) combiner lives in a separate `CustomCombiner` family that inlines a user reduce computation in place of the fixed FMA.

This page owns three things: the **combiner reduction roster** (the fixed weighted-sum FMA, the gain-as-divisor convention, and the `DotCombiner` vs `CustomCombiner` vs non-minibatch `GatherMulScatter` family split), the **per-combiner weight application** (the `UnalignedLoadScalarFromHbm` → `BitcastOp` i32→f32 gain load, the `BroadcastScalarToVector` fan-out, and the `MulFOp`/`AddFOp` FMA), and the **inner-loop emission** (the `Emit` sample-tile loop with its bounds-guard, the `EmitSampleCombiner` accumulator scope, and the `EmitValencyLoop` per-id loop). The CSR multiplicity / valency it consumes is owned by [Dedup Multiplicity](dedup-multiplicity.md) and [EmitValencyLoop](emit-valency-loop.md); the synchronous gather it issues is owned by [stream gather/scatter](stream-gather-scatter.md). They are linked, not repeated.

For reimplementation, the contract is:

- **The combiner is a fixed weighted-sum FMA.** `acc_chunk += emb_chunk · broadcast(gain)`, `FastMathFlags = none`. `sum`/`mean`/`sqrtn` are a *gain scale*, not a code path, and not a config field. A reimplementer emits one FMA loop and trusts the front-end to have pre-scaled the gain.
- **The three emitters are a strict nest, not a dispatch.** `EmitSampleCombiner` (per-sample accumulator) → `EmitValencyLoop` (per-id loop) → `EmitVectorizedLoop` (per-chunk FMA). No run-time selection between a "valency loop" and a "vectorized loop"; the vectorized loop is the valency loop's body.
- **The gain is loaded as raw i32 bits and bit-cast to f32.** The per-id gain is fetched by `UnalignedLoadScalarFromHbm` (returning an integer scalar) and reinterpreted via `arith::BitcastOp` to `f32`. It is never an `arith::SIToFPOp`-style numeric convert — the HBM word *is* the float's bit pattern.
- **The accumulator is a zeroed SPMEM tile, scoped by a `memref::AllocaScopeOp`.** `EmitSampleCombiner` allocates an f32 scoped buffer, zeroes it via `InitializeTileSpmemBuffer` (`ZeroMemOp`), runs the valency loop with the buffer as `iter_arg`, then drains it to HBM with a synchronous stream op.
- **The `Emit` `scf::IfOp` is a tile bounds guard, not a minibatch dispatch.** Its `then` region runs `EmitSampleCombiner`; its `else` region is empty. It skips the out-of-range lanes of the last partial sample tile — it is *not* a `num_minibatches == 1` branch.

| | |
|---|---|
| **Emitter class** | `xla::tpu::sparse_core::SparseDenseMatmulDotCombinerEmitter` |
| **Source file** | `platforms/xla/sparse_core/sparse_dense_matmul_dot_combiner_emitter.cc` (from `GetCurrentLocation` strings) |
| **Entry / nest** | `Emit` `0x1332bda0` ⊃ `EmitSampleCombiner` `0x1332c640` ⊃ `EmitValencyLoop` `0x1332cee0` ⊃ `EmitVectorizedLoop` `0x1332e1c0` |
| **Dispatcher** | `LoweringEmitter::EmitSparseDenseMatmulDotCombiner` `0x131a7ca0` (builds 3 `FoldAllDimensions` values → ctor → `Emit`) |
| **Combiner reduction** | fixed weighted-sum FMA `acc += emb · gain`; `MulFOp` + `AddFOp`, `FastMathFlags = none` |
| **Combiner roster** | `sum` / `mean` / `sqrtn` = one path, distinguished by the front-end gain scale (NOT a code path, NOT a config field) |
| **Weight source** | `sorted_gains` operand; per-id scalar via `UnalignedLoadScalarFromHbm` → `arith::BitcastOp` i32→f32 |
| **Accumulator** | f32 SPMEM tile, `memref::AllocaScopeOp` + `AllocateScopedMemory` + `InitializeTileSpmemBuffer` (`ZeroMemOp`) |
| **Gather** | `InitiateSynchronousStreamOperation` (= `LinearStreamStartOp` + `StreamWait` + `SetSyncFlag`), keyed by per-id token offset |
| **Confidence** | CONFIRMED (decompile op-sequence & source-line anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the combiner reduction, the weight application, and the inner-loop op sequence.** The CSR multiplicity / valency this loop iterates is owned by [Dedup Multiplicity](dedup-multiplicity.md); the per-id loop structure in isolation by [EmitValencyLoop](emit-valency-loop.md); the synchronous indirect gather / scatter-add primitive by [stream gather/scatter](stream-gather-scatter.md); the HLO minibatching decomposition above this lowering by [Embedding Minibatching](embedding-minibatching.md). They are linked, not repeated.

---

## The Combiner Reduction Roster

### One reduction, three names

The `DotCombiner` emitter performs a single arithmetic reduction: a **gain-weighted sum** of the gathered embedding rows. In the decompiled `EmitVectorizedLoop` body the core is exactly two arith ops, in order:

```text
EmitVectorizedLoop core (0x1332e1c0, src lines 250 / 252 / 254)
  mul = MulFOp(emb_chunk, broadcast_gain)   ; src ln 250 — emb · gain
  acc = AddFOp(mul, accumulator_chunk)       ; src ln 252 — + running accumulator
        StoreChunk(accumulator, acc)         ; src ln 254 — write back to acc buffer
```

Both `MulFOp::create` and `AddFOp::create` are emitted with `FastMathFlags = none` (the flags byte register is xor-zeroed before the call). There is no division, no square-root, and no conditional in this body — the reduction is unconditionally `acc += emb · gain`.

The three pooling combiners a TPU-embedding feature can request map onto this single FMA as follows. The combiner divisor is **folded into the per-id gain** before it reaches the XLA custom-call; the emitter never sees the combiner name:

| Combiner | Per-id gain the front-end supplies | What the emitter does | Confidence |
|---|---|---|---|
| `sum` | `gain` unchanged | `acc += emb · gain` | HIGH (gain applied verbatim CONFIRMED; divisor convention INFERRED) |
| `mean` | `gain · (1 / valency)` | `acc += emb · gain` (same op) | HIGH |
| `sqrtn` | `gain · (1 / sqrt(valency))` | `acc += emb · gain` (same op) | HIGH |

> **NOTE — `sum`/`mean`/`sqrtn` is a gain scale, not a code path.** There is no `combiner_type` field in the `SparseDenseMatmulConfig` backend-config, and the emitter contains no branch on a combiner enum. The divisor (`1/n`, `1/sqrt(n)`) is applied above the XLA layer, in the TF/JAX TPU-embedding API, by pre-scaling each id's gain. The front-end op that computes `gain = 1/n` or `1/sqrt(n)` is **not** present in `libtpu.so` (it lives in the layer above the custom-call). What is CONFIRMED in the binary is that the emitter multiplies the gain in verbatim; that this gain *carries* the combiner divisor is INFERRED from the absence of any combiner field and the API convention.

### The combiner emitter family

The `DotCombiner` is one of three embedding-combine lowerings the binary carries. Each is a distinct emitter / decomposer:

| Emitter / decomposer | Address | Reduction form | Confidence |
|---|---|---|---|
| `SparseDenseMatmulDotCombinerEmitter::Emit` | `0x1332bda0` | fixed weighted-sum FMA (this page) | CONFIRMED |
| `LoweringEmitter::EmitSparseDenseMatmulDotCombiner` | `0x131a7ca0` | dispatcher: 3 `FoldAllDimensions` operands → ctor → `Emit` | CONFIRMED |
| `XlaSparseDenseMatmulCustomCombinerOnTc{,Grad}WithCsrInputOp` (+ `…GradWith{Sgd,Adagrad,AdagradMomentum,…}AndCsrInputOp`) | `0xe653640` (Compile) ff. | user reduce computation inlined in place of the FMA | CONFIRMED (symbol-anchored) |
| `GatherMulScatterSparseDenseMatmulOpDecomposer` (`AddPass`) | `0x1306d740` | non-minibatch HLO decomposition (no CSR-window decomposition) | CONFIRMED (pass + symbol) |
| `SparseGatherEmitter::Emit` | `0x133f9120` | standalone SC gather (the gather half the non-minibatch path lowers through) | CONFIRMED (symbol) |

The `CustomCombiner` family is the generalization: instead of the fixed `acc += emb · gain`, it inlines a user-supplied `HloComputation` reduce over the gathered rows (it carries `BuildCombinerVjpComputation` and `EmitSparseCoreComputations` members for the forward/backward reduce). The `GatherMulScatter` decomposer is the simpler counterpart the embedding lowers to when the CSR minibatching decomposition is *not* applied — a different lowering of the same sum-lookup, named in this binary but not body-decoded here.

> **WARNING — the `DotCombiner` path does not use the Sort/Unique/SegmentedScan dialect DAG.** A reasonable assumption is that the embedding sum-lookup always lowers through the cross-lane [scan datapath](scan-datapath.md) (`SegmentedScanOp` etc.). This emitter does **not**: it uses a scalar/streaming combiner — per-id `UnalignedLoadScalarFromHbm` + a synchronous `LinearStream` gather + an `arith` FMA into an SPMEM accumulator. The segmented-scan dialect path is a *different* lowering of the embedding reduce; the `DotCombiner` is the gather-FMA form.

---

## Per-Combiner Weight Application

### Loading the gain

The combiner weight is the `sorted_gains` operand of the `SparseDenseMatmul` op — one f32 gain per (sample, id) pair, laid out parallel to the sorted-id list the CSR segment indexes. In `EmitValencyLoop` the per-id gain is fetched and converted in two ops:

```text
EmitValencyLoop gain load (0x1332cee0, src lines 181 / 195)
  g_i32 = UnalignedLoadScalarFromHbm(gains_base, token_offset)   ; src ln 181 — raw 32-bit word
  g_f32 = arith::BitcastOp(f32, g_i32)                           ; getF32Type + BitcastOp — reinterpret bits
```

The load returns an *integer* scalar; the bit pattern is reinterpreted as `f32` with `arith::BitcastOp` (`mlir::Builder::getF32Type` immediately precedes the `BitcastOp::create` in the decompile). This is a **bit reinterpretation, not a numeric conversion** — the HBM word holds the IEEE-754 bits of the gain directly. A reimplementer who emits an integer-to-float numeric convert here produces wrong activations.

### Broadcasting and applying the gain

The scalar gain is broadcast to a feature-width vector once per id, *outside* the chunk loop, then multiplied into every chunk:

```text
EmitVectorizedLoop weight application (0x1332e1c0)
  bcast = BroadcastScalarToVector(lane_width, g_f32)   ; src ln 234 — gain → vector, hoisted out of the loop
  for chunk in [0, feature_width):                     ; scf::ForOp, iter_arg = accumulator chunk
    emb   = LoadChunk(gathered_row,  chunk)            ; the gathered embedding chunk (operand v3 base)
    a_cur = LoadChunk(accumulator,   chunk)            ; the running accumulator chunk
    mul   = MulFOp(emb, bcast)                         ; src ln 250
    a_new = AddFOp(mul, a_cur)                         ; src ln 252
            StoreChunk(accumulator, chunk, a_new)      ; src ln 254
```

`BroadcastScalarToVector` is invoked once with the per-id gain (`lowering_util::BroadcastScalarToVector`, `0x13d94460`); the resulting vector is the second `MulFOp` operand for every chunk. This is the entire per-combiner weight application: a single broadcast of the (already combiner-scaled) gain, fused-multiply-added into the accumulator chunk by chunk. The `mean`/`sqrtn` divisor, if present, was baked into `g_f32` before it ever reached HBM — this loop is combiner-agnostic.

| Op | `lowering_util` symbol | Role | Confidence |
|---|---|---|---|
| `UnalignedLoadScalarFromHbm` | (per-id scalar load) | load the raw 32-bit gain word from `sorted_gains` | CONFIRMED |
| `arith::BitcastOp` | `getF32Type` + `BitcastOp::create` | reinterpret the i32 word as the f32 gain | CONFIRMED |
| `BroadcastScalarToVector` | `0x13d94460` | fan the scalar gain to a lane-width vector (hoisted) | CONFIRMED |
| `arith::MulFOp` | `MulFOp::create`, `FastMathFlags=none` | `emb_chunk · gain` | CONFIRMED |
| `arith::AddFOp` | `AddFOp::create`, `FastMathFlags=none` | `+ accumulator_chunk` | CONFIRMED |
| `LoadChunk` / `StoreChunk` | `0x13d97620` / `0x13d99960` | read the embedding/accumulator chunk; write back | CONFIRMED |

---

## Inner-Loop Emission

The emission is a three-level nest. Top to bottom: a per-sample-tile `scf::ForOp` (in `Emit`), a per-id `scf::ForOp` over the sample's CSR segment (in `EmitValencyLoop`), and a per-chunk `scf::ForOp` over the feature dimension (in `EmitVectorizedLoop`). Each level's body unconditionally invokes the next.

### Level 0 — `Emit`: the sample-tile loop and bounds guard

`Emit` (`0x1332bda0`) builds the outer loop over sample tiles and a guard that drops the out-of-range lanes of the last partial tile:

```text
Emit outer loop (0x1332bda0)
  if !Target::SupportsSparseCore(): FATAL("SparseCore is not supported by this target")  ; target.h:1709
  lane_cfg = Target[0x948]->[0x94]                       ; per-target max-row / lane config
  n_samples = operand(0).dim                             ; src ln 69
  (operand(1).dim idiv lane_cfg) RetCheck                ; alignment check (remainder != 0 → fail)
  lo   = ConstantIndexOp(0)                              ; src ln 73
  hi   = ConstantIndexOp(n_samples)                      ; src ln 75
  step = ConstantIndexOp(this[+0x60] = ctor-arg n)       ; src ln 78 — tile count (ForOp STEP)
  scf::ForOp(lo, hi, step):                              ; src ln 78 — per-sample-tile loop
    tid     = TileIdOp()                                 ; src ln 86 — SC physical tile id
    s_index = AddIOp(loop_iv, tid-derived)               ; src ln 88 — global sample index
    in_range= CmpIOp(ult, s_index, n_samples)            ; src ln 90 — predicate 6 (ult)
    scf::IfOp(in_range, then = $_0, else = ∅):           ; src ln 103 — bounds guard
      then: EmitSampleCombiner(...) ; YieldOp            ; src ln 98 (lambda $_0 @0x1332ea80)
  SfenceOp("all", 3) ; InsertTileBarrier                 ; inter-tile synchronization
```

The `scf::IfOp` `then` region runs `EmitSampleCombiner`; the `else` region is empty (the `IfOp` is created with a null `else` builder). This makes the `IfOp` a **tile bounds guard**, not a two-way dispatch: it executes the combiner for in-range samples and does nothing for the padding lanes of the last partial tile. After the loop, `SfenceOp::create(…, "all", 3)` and `lowering_util::InsertTileBarrier` provide the inter-tile synchronization.

> **NOTE — the bounds-guard `IfOp` is not a minibatch dispatch.** An earlier survey ([Embedding Minibatching](embedding-minibatching.md)) left open whether this `IfOp` selected between a minibatch and non-minibatch combiner. It does not: it gates `EmitSampleCombiner` on `s_index < n_samples` only. The non-minibatch combine path is the *separate* `GatherMulScatterSparseDenseMatmulOpDecomposer` HLO decomposition, not an else-branch here.

The `CmpIOp` predicate value (`6` = unsigned-less-than) is emitted as an inlined register argument and is not visible as a literal in the decompiled C; it is read from the disassembly. The predicate identity is HIGH; the `CmpIOp` op and its placement are CONFIRMED.

### Level 1 — `EmitSampleCombiner`: the per-sample accumulator scope

`EmitSampleCombiner` (`0x1332c640`) allocates a scoped f32 SPMEM accumulator, zeroes it, runs the valency loop, then drains the result to the activation output:

```text
EmitSampleCombiner (0x1332c640)
  AllocaScopeOp::create                                 ; scope the accumulator region
  F32 = getF32Type
  acc = AllocateScopedMemory(F32, memspace=2)           ; the SPMEM accumulator tile
  InitializeTileSpmemBuffer(acc, 0)                      ; → ZeroMemOp — zero the accumulator
  EmitValencyLoop(builder, acc, ...)                    ; accumulate over the sample's ids
  InitiateSynchronousStreamOperation(...)               ; drain/scatter acc → HBM activation row
  AllocaScopeReturnOp::create                           ; yield from the scope
```

The accumulator is scoped by a `memref::AllocaScopeOp` so its lifetime is exactly the one sample; `AllocateScopedMemory` requests it in memory space `2` (the SPMEM tile space) as `f32`; `InitializeTileSpmemBuffer` zeroes it (it lowers to `ZeroMemOp` + a chunk-iterator loop). After the valency loop fills the accumulator, a final `InitiateSynchronousStreamOperation` scatters it back to the dense activation output in HBM, and `AllocaScopeReturnOp` closes the scope.

### Level 2 — `EmitValencyLoop`: the per-id loop

`EmitValencyLoop` (`0x1332cee0`, the largest of the four at ~4.8 KB) loads the sample's CSR valency, then loops over the ids in that CSR segment, gathering and FMA-ing each:

```text
EmitValencyLoop (0x1332cee0)
  valency = UnalignedLoadScalarFromHbm(csr_row_ptr)     ; src ln 151 — segment length
  v_idx   = IndexCastOp(valency)                         ; src ln 160 — to index type
  base    = MulIOp(...); MulIOp(...)                     ; src ln 161/164/165 — per-id base offset
  inner   = AllocaScopeOp + getF32Type + AllocateScopedMemory   ; lowering_util_alloc.h:71
  scf::ForOp(0, v_idx, 1) iter_arg = acc:                ; loop over ids in the CSR segment
    id      = AddIOp(loop_iv, ...)                       ; advance the id index
    g_i32   = UnalignedLoadScalarFromHbm(gains_base, id) ; src ln 181 — per-id gain word
    g_f32   = BitcastOp(f32, g_i32)                      ; reinterpret as float
    tok_off = ConstantIndexOp; MulIOp; AddIOp            ; per-id embedding token offset
    InitiateSynchronousStreamOperation(tok_off)          ; GATHER this id's embedding row
    EmitVectorizedLoop(acc, g_f32, gathered_row)         ; FMA: acc += row · gain
  AllocaScopeReturnOp::create
```

The valency loop bound comes from `UnalignedLoadScalarFromHbm` of the CSR row pointer (the sample's segment length), index-cast and used as the `scf::ForOp` upper bound. The accumulator threads through as the loop `iter_arg`. Inside the body, each id loads its gain (the bit-cast f32), computes its embedding token offset, issues the synchronous gather (keyed by that offset — the `InitiateSynchronousStreamOperation` is the producer of the `LinearStreamStartOp` the [stream gather/scatter](stream-gather-scatter.md) MRB/FIFO placement consumes), and calls `EmitVectorizedLoop` to FMA the gathered row into the accumulator.

> **NOTE — the multiplicity / valency this loop consumes is the CSR segment length.** The per-sample number of ids (the *valency*) is the CSR row-pointer delta the loop bound is taken from; duplicate-id folding (the `count × gradient` multiplicity) is a pre-processing step owned by [Dedup Multiplicity](dedup-multiplicity.md), not this emitter. Here the emitter simply iterates `valency` ids, each with its own pre-computed gain.

---

## Considerations

- **Emit one FMA loop; trust the gain.** Do not special-case `sum`/`mean`/`sqrtn`. The reduction is always `acc += emb · gain`; the divisor was folded into the gain upstream. A combiner-enum branch is a modeling error — the binary has none.
- **The gain is a bit-reinterpret, not a numeric convert.** `UnalignedLoadScalarFromHbm` returns an integer; `arith::BitcastOp` (not `SIToFPOp`) recovers the f32. The HBM word is the float's bit pattern. An integer-to-float convert here corrupts every gain.
- **The accumulator lives in SPMEM, scoped per sample, and must be zeroed.** `AllocateScopedMemory(f32, memspace=2)` + `InitializeTileSpmemBuffer` (`ZeroMemOp`). Skipping the zero-init leaks the previous sample's partial sum into the next.
- **Broadcast the gain once, not per chunk.** `BroadcastScalarToVector` is hoisted out of the chunk loop; the same vector multiplies every chunk. Re-broadcasting per chunk is wasted work the emitter avoids.
- **The outer `IfOp` is a bounds guard.** `then` = combiner, `else` = empty. It drops the padding lanes of the last partial sample tile; it is not a minibatch dispatch and not a combiner selector.
- **Unmapped (HIGH / LOW / INFERRED).** The `CmpIOp` predicate value `6`=ult (HIGH — inlined register arg, read from disassembly, not literal in decompiled C). The combiner divisor folding `mean→1/n` / `sqrtn→1/sqrt(n)` into `sorted_gains` (HIGH — the emitter applies the gain verbatim CONFIRMED; the front-end op that computes the divisor is not in `libtpu.so`, INFERRED). The `StreamOptions` discriminant bits that make the gather an indirect id-keyed DMA vs a linear DMA (LOW — the per-id token offset feeding the stream is CONFIRMED; the struct was not bit-decoded — see [stream gather/scatter](stream-gather-scatter.md)). Whether the `EmitSampleCombiner` outer accumulator and the `EmitValencyLoop` inner `AllocateScopedMemory` are the same re-scoped SPMEM buffer or two allocations (LOW — both go through a `memref::AllocaScopeOp`; the aliasing was not traced). The `GatherMulScatterSparseDenseMatmulOpDecomposer` body (the non-minibatch combine) (LOW — pass + op name pinned; HLO it builds not decoded).

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseDenseMatmulDotCombinerEmitter::Emit` (`0x1332bda0`) | the sample-tile loop + bounds-guard `IfOp` (level 0) |
| `::EmitSampleCombiner` (`0x1332c640`) | the per-sample SPMEM accumulator scope + zero-init + drain (level 1) |
| `::EmitValencyLoop` (`0x1332cee0`) | the per-id CSR loop: gain load, gather, FMA call (level 2) |
| `::EmitVectorizedLoop` (`0x1332e1c0`) | the per-chunk FMA core: `BroadcastScalarToVector` + `MulFOp` + `AddFOp` + `StoreChunk` |
| `LoweringEmitter::EmitSparseDenseMatmulDotCombiner` (`0x131a7ca0`) | the dispatcher that builds the `FoldAllDimensions` operands and runs `Emit` |
| `lowering_util::BroadcastScalarToVector` (`0x13d94460`) | fans the per-id gain to a lane-width vector |
| `lowering_util::{LoadChunk,StoreChunk}` (`0x13d97620` / `0x13d99960`) | the SPMEM chunk read/write the FMA fuses across |
| `lowering_util::InitializeTileSpmemBuffer` (`0x13d93440`) | zeroes the accumulator (`ZeroMemOp` + chunk-iterator loop) |
| `lowering_util::InitiateSynchronousStreamOperation` (`0x13d896a0`) | the per-id gather + the per-sample drain (`LinearStreamStartOp` + `StreamWait` + `SetSyncFlag`) |
| `GatherMulScatterSparseDenseMatmulOpDecomposer` (`0x1306d740`) | the non-minibatch combine counterpart |
| `XlaSparseDenseMatmulCustomCombinerOnTcWithCsrInputOp` (`0xe653640`) | the user-reduce combiner family (replaces the fixed FMA) |

## Cross-References

- [EmitValencyLoop](emit-valency-loop.md) — the per-id loop in isolation; the CSR-segment iteration this page's level 2 builds.
- [Dedup Multiplicity](dedup-multiplicity.md) — the duplicate-count / uniquify pre-processing that produces the valency this combiner iterates.
- [Embedding Minibatching](embedding-minibatching.md) — the HLO minibatching decomposition above this lowering; where the sample-tile count comes from.
- [Stream Gather / Scatter](stream-gather-scatter.md) — the synchronous indirect gather this emitter issues per id and the scatter that drains the accumulator.
- [VectorExtended (VEX)](vectorextended-vex.md) — the alternative cross-lane scan/reduce datapath the embedding *can* lower through; the `DotCombiner` is the gather-FMA form, not this.
- [Scan Datapath](scan-datapath.md) — the `SegmentedScanOp` reduce path the `DotCombiner` does *not* use.
- [SparseCore Overview](overview.md) — the three SC engine classes and where the embedding gather-reduce-scatter datapath sits.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
