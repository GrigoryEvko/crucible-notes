# EmitValencyLoop

> *Every address, op-create order, `scf::ForOp` bound, stack-slot identity, and `::create` signature on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). `.text` VA equals file offset at `0xe63c000`; `.rodata` at `0x84a0000`; both identity-mapped. The binary is not stripped — `nm -C` resolves every method. `EmitValencyLoop` is the **gfc/Trillium** namespace instance; the addresses on this page are from that build. Other versions differ.*

## Abstract

`SparseDenseMatmulDotCombinerEmitter::EmitValencyLoop` (`@0x1332cee0`, `0x12e0` B) is the **innermost embedding loop** of the per-sample scalar combiner lowering. For one sample (one CSR row), it loads the sample's **valency** — the runtime count of ids in that sample's CSR segment — at HBM-load time, builds an `scf::ForOp` whose trip count *is* that valency, and inside the loop gathers one embedding row per id (a synchronous indirect stream) and FMAs `emb·gain` into an SPMEM accumulator via [`EmitVectorizedLoop`](sample-combiner-emitter.md). It is the scalar, per-id form of the embedding sum-lookup.

The page documents three things, all confirmed against the function's decompiled body: (1) the **three-level loop structure** it generates — per-sample setup, the per-id `scf::ForOp`, and the per-feature-chunk vectorized FMA — and the inner SPMEM accumulator it scopes with a `memref::AllocaScopeOp` region; (2) the **valency mechanism** — the runtime `UnalignedLoadScalarFromHbm` → `IndexCastOp` that drives the `for` upper bound, and the deliberate **absence** of any runtime valency division (mean / sqrtn is folded into the per-id gain at the front end); and (3) the **sort / uniquify / dedup wiring** this loop sits beside — the HLO `SparseDenseMatmulOpDecomposer` dedup datapath (`SortAndPartition` → `ReduceDuplicates` CSR→ELL → `SparseGather` over unique ids → segmented reduce) and the SC-dialect `SortOp` / `UniqueOp` / `UniqueWithLaneIdsOp` / `SegmentedScanOp` / `DuplicateCount` op set those lower to. The DotCombiner per-id scalar loop and the Sort/Unique dedup path are two **alternative** lowerings of the same embedding sum-lookup; this page anchors how the valency loop is wired and how the dedup operands thread.

For reimplementation, the contract is:

- **The valency is a runtime scalar, not a compile-time constant.** `EmitValencyLoop` emits `UnalignedLoadScalarFromHbm` against the CSR/id operand, `IndexCastOp`s the result to `index`, and uses it as the `scf::ForOp` upper bound. The generated loop is `for (i = 0; i < valency; ++i)`.
- **There is no runtime division anywhere in the loop.** Exhaustively: no `arith::DivFOp`, `math::RsqrtOp`, reciprocal, `pow`, or any `divsd`/`vdiv`/`vsqrt` instruction. The mean (`1/n`) and sqrtn (`1/√n`) combiner divisors are pre-folded into the per-id gain by the TF/JAX front end. The valency drives *only* the integer trip count.
- **Per id: gather, then FMA.** The body loads the per-id gain (`UnalignedLoadScalarFromHbm` → `arith::BitcastOp` i32→f32), computes the per-id token offset (`feature_width × 4` byte-scaled, `MulIOp` + `AddIOp`), issues the synchronous indirect gather (`InitiateSynchronousStreamOperation`), and calls `EmitVectorizedLoop(gather_buf, gain, outer_accumulator)`.
- **The accumulator lives in an `AllocaScopeOp` region.** A `memref::AllocaScopeOp` opens an inner region; `AllocateScopedMemory` allocates the f32 SPMEM accumulator inside it; `memref::AllocaScopeReturnOp` yields the result out at loop end.
- **The dedup path is a different, dedup-optimised lowering.** The Sort/Unique/`ReduceDuplicates`/`SparseGather` datapath belongs to `SparseDenseMatmulOpDecomposer` (HLO custom-call decomposition), not to this emitter. This emitter is the per-id scalar gather-FMA form.

| | |
|---|---|
| **Function** | `SparseDenseMatmulDotCombinerEmitter::EmitValencyLoop(OpBuilder, Value v1, Value v2)` `@0x1332cee0` (gfc; `0x12e0` B) |
| **Caller** | `…::EmitSampleCombiner` `@0x1332c640` (call site `@0x1332ca82`) |
| **`v1`** | the CSR/embedding-id operand `Value` — the `UnalignedLoadScalarFromHbm` source (valency + gain) |
| **`v2`** | the SPMEM outer accumulator `Value` (the tile just zeroed by `InitializeTileSpmemBuffer`) |
| **Loop primitive** | `scf::ForOp::create` `@0x17866d60` — `(lower=ConstIdx(0), upper=IndexCast(valency), step=ConstIdx(1), iter_args={acc})` |
| **Valency load** | `lowering_util::UnalignedLoadScalarFromHbm` `@0x13da4580` (ComputeType=1, operand=`v1`) → `IndexCastOp` `@0x1cb0ce80` |
| **Per-id gather** | `lowering_util::InitiateSynchronousStreamOperation` `@0x13d896a0` (edx=1, r8d=1, `StreamOptions` by value) |
| **Inner FMA** | `EmitVectorizedLoop` `@0x1332e1c0` `(v1=gather_buf, v2=gain, v3=outer_acc, v4=unset)` |
| **Accumulator scope** | `memref::AllocaScopeOp::create` `@0x18304960` + `AllocaScopeReturnOp::create` `@0x18304e40` |
| **Division ops** | **none** — no `DivFOp`/`RsqrtOp`/reciprocal/`sqrt`/`divsd`/`vdiv` in the body |
| **Source file** | `platforms/xla/sparse_core/sparse_dense_matmul_dot_combiner_emitter.cc` (`@0x87610c0`) |
| **Confidence** | CONFIRMED (op-create order and `scf::ForOp` bounds read from the function body) unless a row says otherwise |

---

## The Loop Structure

### Purpose

`EmitValencyLoop` builds the **per-sample → per-id → per-feature-chunk** nest that turns one CSR row of `(id, gain)` pairs into one accumulated embedding-sum vector. It does not itself iterate at compile time; it *emits* MLIR ops — `scf::ForOp`, `memref::AllocaScopeOp`, arith ops, and two nested emitter calls — that the SC backend later lowers to a sequencer program. The structure below is the static op-create order of that emission.

### Signature and ABI

`EmitValencyLoop(OpBuilder, Value v1, Value v2)` — `this` in `rdi`, `OpBuilder` passed by value at `[rbp+0x10]`. The caller `EmitSampleCombiner` (call site `@0x1332ca82`) passes:

| Entity | Reg / slot (entry) | Identity |
|---|---|---|
| `this` | `rdi` → `rbx`, `[rbp-0x30]` | `SparseDenseMatmulDotCombinerEmitter*` (`+0x8` = builder/context state, `+0x10` = HLO, `+0x38` = `Target&`) |
| `v1` | `rsi` → `r13` | the CSR/embedding-id operand `Value` (`EmitSampleCombiner` `[rbp-0xc8]`) — the `UnalignedLoad` source |
| `v2` | `rdx` → `r14`, `[rbp-0x130]` | the SPMEM **outer accumulator** `Value` (the zeroed tile; threaded into `EmitVectorizedLoop` as `v3`) |
| `OpBuilder` (by value) | `[rbp+0x10]` (`r12`) | the inserting builder; ymm-copied into the stream / loop call frames |

> **NOTE — `v2` is pre-zeroed by the caller.** `EmitSampleCombiner` calls `InitializeTileSpmemBuffer` (`@0x13d93440`, call site `@0x1332ca53`) on the tile before passing it as `v2`; `EmitValencyLoop` accumulates into it, it does not zero it.

### Op-Create Sequence (byte-exact)

The function emits exactly this op sequence. Each row is a single `…::create` call at the listed VA; the `src ln` is the `.cc` line number baked into the `DebugInfoTracker::GetCurrentLocation` call that precedes each op.

```text
#   op / call                                   @VA          src ln  role
--  ------------------------------------------   ----------   ------  --------------------------------------------
0   HloInstruction::operand(1)  ×2               1332cf06 /   151     read CSR/id operand-1 Shape twice; dims[0],
                                                 1332cf4b             dims[1]; layout CHECK (fail @1332e13d ln843)
1   arith::ConstantIndexOp(0)                    1332d038     151     ForOp LOWER  → [rbp-0x118]
2   arith::ConstantIndexOp(1)                    1332d0f4     152     ForOp STEP   → [rbp-0xf0]
3   UnalignedLoadScalarFromHbm (CSR valency)     1332d185     160     load per-sample valency; operand = v1
4   arith::IndexCastOp (valency → index)         1332d247     —       → ForOp UPPER [rbp-0xe8]
5   ConstantIndexOp + arith::MulIOp #1 (hi dim)  1332d3b2 /   164/165 CSR row addr from operand1.dim0 (hi half)
                                                 1332d3c7
6   ConstantIndexOp + arith::MulIOp #2 (lo dim)  1332d515 /   164/165 CSR row base → [rbp-0x148]
                                                 1332d52a
7   TypeRange + memref::AllocaScopeOp::create    1332d621     71      open inner-accumulator region (lowering_util_alloc.h)
8   getF32Type + AllocateScopedMemory            1332d792 /   —       f32 SPMEM inner accumulator → r12
                                                 1332d7eb
9   scf::ForOp::create  (VALENCY LOOP)           1332d8d4     —       (Const0, IndexCast(valency), Const1, {acc})
--- loop body (per id) ---------------------------------------------------------------------------------------
10  arith::AddIOp #1 (id index)                  1332da10     —       loop_iv + CSR base from MulIOp [rbp-0x148]
11  UnalignedLoadScalarFromHbm (per-id gain)     1332da93     195     load per-id sorted_gains scalar; operand = v1
12  getF32Type + arith::BitcastOp                1332db43 /   198     i32 bits → F32 gain → [rbp-0xe8]
                                                 1332db54
13  (feat «×4») + ConstantIndexOp + MulIOp #3    1332dcb9 /   —       per-id token offset
                                                 1332dd82
14  arith::AddIOp #2 (token offset + base)       1332dd9b     —       final embedding-row offset
15  InitiateSynchronousStreamOperation           1332defb     —       indirect gather (edx=1, r8d=1, StreamOptions)
16  EmitVectorizedLoop call                      1332df2e     —       FMA acc += emb·gain
--- end loop body --------------------------------------------------------------------------------------------
17  memref::AllocaScopeReturnOp::create          1332e08d     —       yield accumulator out of inner scope
18  ret                                          1332e13c     —       StatusOr<>; CHECK-fail path @1332e13d ln843
```

> **GOTCHA — the operand(1) Shape is read with a 3-vs-5 storage discriminant.** Both `operand(1)` reads (`@0x1332cf06`, `@0x1332cf4b`) load the operand's `Shape*` from `[hlo+0x58]`, then branch on the byte at `[shape+0x138]` (offset 312): `== 3` → dims are inline; `== 5` → dims are heap, dereference `[shape]` first; anything else → `LogMessageFatal("buffer != nullptr", shape.h:843)`. A reimplementer reading the CSR/id operand's dims must reproduce that inline/heap small-buffer discriminant or the layout read is wrong. `dims[0]` (`[…+0x8]`) → `r15` is the per-id stride; `dims[1]` (`[…+0x10]`) → `[rbp-0xe0]` is the feature width.

### Three-Level Nesting

```text
region                         ops emitted in it                                  role
-----------------------------  ------------------------------------------------   --------------------------
EmitValencyLoop body           operand(1) shape; Const0/Const1; CSR-valency       set up loop bounds, the CSR row
  (outer, per sample)          load + IndexCast; CSR-addr MulIOp×2; AllocaScopeOp; base, and the inner accumulator
                               AllocateScopedMemory (inner acc); scf::ForOp
scf::ForOp body                AddIOp(id index); gain load + Bitcast(i32→f32);     per-id gather + FMA
  (per id; iter_arg = acc)     token-offset MulIOp/AddIOp; Initiate-              (one embedding row per id)
                               SynchronousStreamOperation (gather);
                               EmitVectorizedLoop (FMA into acc)
EmitVectorizedLoop body        BroadcastScalarToVector(gain); LoadChunk×2;         per-feature-chunk FMA
  (per feature chunk)          MulFOp(emb·gain); AddFOp(+acc); StoreChunk(→acc)
```

The outer region is the function body itself. The middle region is the `scf::ForOp` whose single `iter_arg` is the SPMEM accumulator. The innermost region is generated by [`EmitVectorizedLoop`](sample-combiner-emitter.md), which broadcasts the scalar gain to a vector, multiplies the gathered embedding row by it, and adds into the accumulator chunk by chunk.

---

## The Valency Mechanism

### Purpose

The valency is the number of embedding ids contributing to one sample's lookup — variable per sample, known only at runtime. This unit documents how `EmitValencyLoop` reads it, how it becomes the loop bound, and — the key negative result — that no division is ever applied to it.

### Runtime Valency Load → `scf::ForOp` Bound

The per-sample valency is loaded from the CSR row buffer at runtime, not baked at compile time:

```c
// emitted by EmitValencyLoop (op-create #3, #4, #9)
ScalarFromHbm = UnalignedLoadScalarFromHbm(/*Target&*/, /*ComputeType=*/1,
                                           /*OpBuilder*/, /*SmallVector<Value,6>=*/{v1});  // @0x1332d185
valency_idx   = arith::IndexCastOp(loc, IndexType, ScalarFromHbm);                          // @0x1332d247
loop = scf::ForOp::create(builder,
                          /*lower=*/ConstantIndexOp(0),        // [rbp-0x118]
                          /*upper=*/valency_idx,               // [rbp-0xe8]  ← the runtime valency
                          /*step =*/ConstantIndexOp(1),        // [rbp-0xf0]
                          /*iter_args=*/ValueRange{accumulator});   // @0x1332d8d4
```

The `SmallVector<Value,6>` argument is constructed inline with header word `0x600000001` (capacity 6, size 1) holding `{v1}` — confirmed at both load sites. `ComputeType` is passed as `esi = 1` at both. The generated loop is therefore `for (i = 0; i < valency; ++i)` with the SPMEM accumulator threaded as the loop-carried value.

> **NOTE — the valency is the CSR segment length, read from HBM.** `v1` (the CSR/embedding-id operand) is the load source; the loaded scalar is the per-sample row pointer / id-count, `IndexCast`'d to drive the trip count. It is not a constant — different samples iterate different numbers of times.

### The Per-Id Gain Load (Not a Division)

Inside the loop, the per-id gain is loaded as raw i32 bits and **bitcast** to f32 — it is never computed by a division:

```c
// op-create #11, #12 — inside the scf::ForOp body
gain_bits = UnalignedLoadScalarFromHbm(/*ComputeType=*/1, /*SmallVector=*/{v1});  // @0x1332da93 (src ln 195)
gain_f32  = arith::BitcastOp(loc, getF32Type(), gain_bits);                       // @0x1332db54 (src ln 198)
```

`gain_f32` is passed to `EmitVectorizedLoop` as `v2`, where it becomes `BroadcastScalarToVector(gain) → MulFOp(emb·gain) → AddFOp(+acc)`. The combiner is therefore a weighted-sum FMA; the divisor lives *inside* the supplied gain.

> **QUIRK — both `UnalignedLoadScalarFromHbm` calls share operand `v1` and `ComputeType=1`; the offsets distinguish valency from gain.** The CSR-valency load (`@0x1332d185`) and the per-id gain load (`@0x1332da93`) pass the identical `SmallVector<Value,6>={v1}` and `ComputeType=1`; only the computed token offset differs. This is consistent with `v1` being a single packed/strided HBM buffer carrying *both* the CSR row pointers and the sorted gains, keyed by the differing offsets — **INFERRED** (the shared operand and ComputeType are CONFIRMED; the single-buffer multiplexing is not bit-proven, the gain could alternatively be a distinct buffer reached through the same `Value` at a different base).

### No Runtime Valency Division — Exhaustive

The complete set of `…::create` calls in `EmitValencyLoop` is: `ConstantIndexOp` ×5, `MulIOp` ×3, `AddIOp` ×2, `IndexCastOp` ×1, `BitcastOp` ×1 (i32→f32 gain reinterpret), `AllocaScopeOp` / `AllocaScopeReturnOp`, the valency `scf::ForOp`, `UnalignedLoadScalarFromHbm` ×2, `InitiateSynchronousStreamOperation` ×1, and `EmitVectorizedLoop` ×1. There is **no** `arith::DivFOp`, `math::RsqrtOp`, reciprocal, `PowOp`, or any `sqrt`/`divsd`/`vdiv`/`vsqrt`/`vrsqrt` x86 instruction in the body.

> **QUIRK — the mean / sqrtn divisor is a front-end gain-scale, not a runtime op.** For a `mean` combiner the divisor is `1/n`; for `sqrtn` it is `1/√n`. Neither is computed here: the front end (TF/JAX) folds the divisor into the per-id gain before the gain reaches HBM, so `EmitValencyLoop` applies the gain verbatim (load → bitcast → multiply in the FMA). The valency is used *only* to size the integer `scf::ForOp`. A reimplementer must replicate the gain-side folding; expecting a runtime divide here will not find one.

---

## The Per-Id Gather and FMA Wiring

### Purpose

Each loop iteration gathers exactly one embedding row from HBM into SPMEM and FMAs it into the accumulator. This unit pins the token-offset arithmetic that keys the gather and the call-argument identities into the gather and the FMA.

### Token-Offset Arithmetic

The per-id embedding-row offset is built from the feature width (operand1 `dims[1]`) and the loop index:

```c
// op-create #10, #13, #14 — the gather index
id_index  = arith::AddIOp(loop_iv, csr_base);          // @0x1332da10  (csr_base from MulIOp #2 [rbp-0x148])
feat_x4   = feature_width << 2;                         // @0x1332dcb9  (×4 = i32 bytes per word)
tok_mul   = arith::MulIOp(ConstantIndexOp(feat_x4), …); // @0x1332dd82  (MulIOp #3)
token_off = arith::AddIOp(tok_mul, base);              // @0x1332dd9b  → the embedding-row offset
```

The `feature_width << 2` (`v223 = 4 * v223` in the decompile, `@0x1332dcb9`) converts the feature count to a byte stride for 4-byte (i32) words.

### Synchronous Indirect Gather

```c
// op-create #15
InitiateSynchronousStreamOperation(/*Target&*/, /*Array=*/token_off_operands,
                                   /*edx=*/1, /*r8d=*/1, /*StreamOptions by value*/);  // @0x1332defb → @0x13d896a0
```

This issues the indirect HBM→SPMEM gather of this id's embedding row, keyed by `token_off`. The two leading scalar args are both `1` (`edx=1`, `r8d=1`); these are the `StreamOptions` discriminant bits that select an **indirect id-keyed load** rather than a linear DMA. The per-id token offset is confirmed to be the gather index; the precise meaning of the two `StreamOptions` bools is **LOW** (not bit-decoded — same open item carried from the broader DotCombiner survey). The producer/consumer side of this stream is the `LinearStreamStartOp` / `IndirectStreamStartOp` family on the [Stream Gather/Scatter](stream-gather-scatter.md) page.

### `EmitVectorizedLoop` Call Wiring

```text
arg     reg / slot           identity                          confirmed via
------  ------------------   -------------------------------   ------------------------------------------
this    rdi = rbx            the emitter                        member layout
builder [rbp-0x110]          inner-region OpBuilder (in loop)   ymm copy to [rsp]
v1      rdx = [rbp-0xf0]     the gathered embedding / inner buf set from [rbp-0xf0] at 1332dde6
v2      rcx = [rbp-0xe8]-0x10 the GAIN (Bitcast result)         BroadcastScalarToVector(v2) = gain
v3      r8  = [rbp-0x130]    the OUTER accumulator (= v2 saved) LoadChunk / StoreChunk(v3) = acc
v4      (r9 unset)           unused / null Value                4th Value left unset
```

The call (`@0x1332df2e`) targets `EmitVectorizedLoop @0x1332e1c0`. Its mangled name carries four `Value` parameters (`…N4mlir9OpBuilderENS3_5ValueES5_S5_S5_`), confirming a 4-`Value` signature; the 4th (`r9`) is not loaded, so `v4` is passed as the default/null `Value`. After both fallible calls a `StatusOr` ok-check (`cmp rax, 1`) gates the success path; the failure path builds an `absl::Status` at `.cc` lines 208 / 210.

### Drain

`memref::AllocaScopeReturnOp::create` (`@0x1332e08d` → `@0x18304e40`) yields the accumulator results out of the `AllocaScopeOp` region; `mlir::ValueRange::ValueRange` packs the op's results, and the function returns `StatusOr<…>` with `*(_QWORD*)ptr = 1` on success.

---

## The Sort / Uniquify / Dedup Wiring

### Purpose

`EmitValencyLoop` is the per-id **scalar** form of the embedding sum-lookup. A *different*, dedup-optimised lowering exists: the HLO `SparseDenseMatmulOpDecomposer` decomposition that sorts the ids, collapses duplicates (CSR→ELL), gathers over the unique window, and segment-reduces. This unit documents that wiring — the layer this loop sits beside — and the SC-dialect op operand/result threading it lowers to. It corrects any implication that the DotCombiner emitter itself runs the sort/unique DAG: it does not; the dedup path is the decomposer's.

### The HLO Dedup Datapath (`SparseDenseMatmulOpDecomposer`)

```text
stage              function                                       @VA          role
-----------------  --------------------------------------------   -----------  --------------------------
sort + partition   SortByPartitionIdsOrFallthrough                1366ea60     sort (sample,token) ids
                   SortAndPartitionSparseInput                    1366f9c0       "
                   PartitionCsr / PartitionCsrByLogicalReplicas   13670100 /   per-physical-core CSR window
                                                                  1366fae0
reverse index      CalculateReverseIndex                          13670f00     un-permute index
DEDUP + reduce     ReduceDuplicates                               136722e0     CSR→ELL duplicate collapse
gather (unique)    DistributedGatherUniqueVectors                 13674080     gather over unique ids
                   GatherAndQuantizeEmbeddingVectors              13674680       "  + dequant
                   GatherEmbeddingVectors                         13674d60       "
reduce             ReduceVectors / FixedWindowReduceVectors       13676680 /   per-sample reduce (× dup count)
                                                                  13675d20
activations        ComputeActivations                             13675840     final embedding-sum output
```

The chain mirrors the broader [embedding-minibatching decomposition](embedding-minibatching.md)'s gather→sort→uniquify→scan→scatter DAG; the addresses above are the forward-embedding decomposition entry points.

### `ReduceDuplicates` — the CSR→ELL Dedup Op

`ReduceDuplicates` (`@0x136722e0`) is the duplicate-collapse + segment-reduce. Its shape contract is enforced by `SparseCoreShapeVerifier::HandleReduceDuplicates` (`@0x136538a0`): `reduce->operands().size() == 2` ("ReduceDuplicates expects two inputs" — the sorted-CSR input + the init value), `reduce->called_computations().size() == 1` ("expects a reduce function" — the combiner, e.g. add), and "Csr input and Ell output must have identical number of rows" (the CSR→ELL collapse). Its body emits:

```text
#   op / custom-call                              @VA          notes
--  -------------------------------------------   ----------   -----------------------------------------
0   CreateGetTupleElement ×2 + CreateTuple(2)      136722.. /   unpack {csr_pointers, values}, re-tuple
                                                   13672573
1   CustomCall "SparseMapRow"  (op-type 0x4)       1367260b     5-arg form, WITH the reduce HloComputation
2   CreateGetTupleElement ×2                        13672897 /   extract SparseMapRow results
                                                   13672930
3   reduce computation: CreateParameter ×3          13672a70     lhs/rhs value params + init; nested SparseMapRow
4   CustomCall "DynamicBoundedSlice" (op-type 0x11) 13672e56     bound the unique window to max_unique_ids
5   CreateTuple + CustomCall "SparseMapRow" (0x4)   13672fe8     final map-row
6   CreateGetTupleElement + AddInstruction          13673588     package
```

The op-type names are byte-decoded from the `SparseCoreOperationTypeToString` jump table (`@0x14b7f480`, table `@0xaf36d70`): entry `0x4` = `"SparseMapRow"`, entry `0x11` = `"DynamicBoundedSlice"`. The duplicate multiplicity is realised by `SparseMapRow`'s per-ELL-row reduce collapsing the sorted duplicate token-ids; `DynamicBoundedSlice` caps the deduped window at the config `max_unique_ids_per_partition`.

> **NOTE — the dedup shrinks the gather directly.** `DistributedGatherUniqueVectors` (`@0x13674080`) lowers to the HLO `SparseGather` op, whose contract (`SparseCoreShapeVerifier::HandleSparseGather @0x1365b880`) is: "SparseGather expects 2 operands" (embedding table + the unique-id gather indices), "require a CSR Config", and "Gathered Output dimension-0 size must match input CSR's max-non-zeros". The output dim-0 is the *unique-id* window — so deduplicating the ids directly reduces the number of embedding-table HBM gathers. This is the redundant-gather elimination, in contrast to `EmitValencyLoop`'s one-gather-per-id scalar form.

### SC-Dialect Op Signatures (byte-confirmed from `::create`)

The HLO ops above lower to the SC dialect. The `::create` signatures are confirmed from the (non-stripped) symbol mangling and producer call sites:

| Op | `::create` @VA | Signature | Producer call site |
|---|---|---|---|
| `SortOp` | `0x14604600` (Type form) / `0x14604800` (TypeRange) | `(OpBuilder, Location, Type, Type, Type, Value, Value, Value, StringAttr)` — **3 result types, 3 key inputs, sort-dir attr** | `FusionEmitter::SetOutputWindowBoundsForSparseMapRow` (`@0x1389136a`) → `SortOpLowering::matchAndRewrite @0x13597700` → `RadixSortEmitter` |
| `UniqueOp` | `0x14622400` (also `0x14622200`) | `(OpBuilder, Location, Value, Value)` — **2 sorted-key inputs** → unique values + index/marker | `RankAndPermuteComputeFunction` (`@0x134042a9`) — `rdx=key0`, `rcx=key1` |
| `UniqueWithLaneIdsOp` | `0x146231a0` | `(OpBuilder, Location, Value, Value)` — same 2 inputs, **+ per-lane (multiplicity) outputs** (`getNextResultAtOffset` ×2) | `RankAndPermute` (`@0x13404303`) |
| `SegmentedScanOp` | `0x145fd5a0` | `(OpBuilder, Location, Type, Value data, Value seg-id, StringAttr reduction_op)` | `ScanOpLowering @0x135f348d`; `ComputeElementScatter @0x13d2a766` |
| `DuplicateCountOp` / `…WithLaneIds` | (inline build) | lowered via `DuplicateCountUniqueOpLowering<T>` → LLVM struct `{i32 count, f32 …}` → `ReplaceOpWithExtracts` | `matchAndRewrite @0x13599d40` / `@0x1359a7e0` |

The `SortOp` carries a 4-char sort-direction `StringAttr` `"dscd"` (`@0x8720761`, built via `StringAttr::get`); the 1-input HLO `SortLexicographic` ("SortLexicographic expects one input", `HandleSortLexicographic @0x13653540`) lowers through `VariableWindowPipelineEmitter` → dialect `SortOp` → `SortOpLowering` → `RadixSortEmitter`. `UniqueOp` and `UniqueWithLaneIdsOp` are called over the *sorted* `(token, sample)` key columns in `RadixSortEmitterInternal::RankAndPermuteComputeFunction` (`@0x134039c0`).

### Multiplicity → Segmented Reduce

`DuplicateCountOp`, `DuplicateCountWithLaneIdsOp`, `UniqueOp`, and `UniqueWithLaneIdsOp` all share **one** LLVM lowering template — `DuplicateCountUniqueOpLowering<T>`, instantiated at `0x13599d40` / `0x1359a7e0` / `0x1359b280` / `0x1359bd20`, all registered in one `RewritePatternSet::add` (`@0x13572820`) alongside `SortOpLowering`. The lowering builds an LLVM struct `{i32, f32, …}` and extracts results via `ReplaceOpWithExtracts`; the `i32` is the **count of how many sorted entries collapsed into each unique id** — the per-unique valency weight.

That multiplicity scales the segmented reduce. `SegmentedScanOp(resultType, data, seg-id, reduction_op)` (the `reduction_op` read via `getReductionOp`) resets at each CSR boundary; because the duplicate-collapse pre-weights each unique entry's row population, the `SegmentedScan` reduces over the unique window with the multiplicity already baked in. See [dedup multiplicity](dedup-multiplicity.md) for the `DuplicateCount`→multiplicity and `Uniquify` inverse-permutation detail, and [segmented add scan](segmented-add-scan.md) / [scan datapath](scan-datapath.md) for the reduce.

> **NOTE — two lowerings, one semantics.** The DotCombiner per-id scalar loop (this page) and the Sort/Unique/`ReduceDuplicates`/`SparseGather` dedup datapath are two alternative lowerings of the same embedding sum-lookup. The scalar loop is simpler (one gather per id, gain applied verbatim); the dedup path eliminates redundant gathers but threads the sort/unique/segmented-scan op set. Which lowering a given embedding op takes is the decomposer's decision, not `EmitValencyLoop`'s.

---

## Per-Generation Presence

| Mechanism | VF (vfc, v5e) | GL (glc, v5p) | GF (gfc, v6e) |
|---|:---:|:---:|:---:|
| `SparseDenseMatmulDotCombinerEmitter::EmitValencyLoop` | yes | yes | yes (decoded here) |
| Runtime valency load (`UnalignedLoadScalarFromHbm` → `IndexCast`) | yes | yes | yes |
| No-runtime-division (front-end gain-scale) | yes | yes | yes (byte-confirmed) |
| Synchronous indirect gather (`InitiateSynchronousStreamOperation`) | yes | yes | yes |
| HLO `ReduceDuplicates` (CSR→ELL) dedup | yes | yes | yes |
| SC-dialect `SortOp` / `UniqueOp` / `UniqueWithLaneIdsOp` / `SegmentedScanOp` | yes | yes | yes |
| `DuplicateCountUniqueOpLowering<T>` (shared multiplicity template) | yes | yes | yes |

The addresses on this page are the gfc/Trillium instances; the vfc/glc siblings carry the same emitter and op set at different addresses. No `SparseDenseMatmul*` emitter appears under the `jxc` (Jellyfish) or `pxc` (Pufferfish) namespaces — those generations have no SparseCore.

---

## Limits and Open Items

| Item | Status | Confidence |
|---|---|---|
| `EmitValencyLoop` full op-create order + `scf::ForOp` bounds (Const0, IndexCast(valency), Const1, {acc}) | read from the function body | CONFIRMED |
| Runtime valency load → `IndexCast` → loop upper bound | both load sites + IndexCast read | CONFIRMED |
| Absence of any runtime division / sqrt / reciprocal in the loop | exhaustive op-create + x86 instruction sweep | CONFIRMED |
| Per-id gain load + i32→f32 `BitcastOp` | accessor + `BitcastOp::create` read | CONFIRMED |
| Token-offset arithmetic (`feat << 2`, MulIOp + AddIOp) → gather index | read from body | CONFIRMED |
| `EmitVectorizedLoop` call args (`v1=gather_buf, v2=gain, v3=outer_acc, v4=unset`) | reg/slot + 4-Value mangling | CONFIRMED |
| `ReduceDuplicates` contract (2 inputs + 1 reduce-fn, CSR→ELL) + `SparseMapRow`(0x4)/`DynamicBoundedSlice`(0x11) | verifier strings + jump table | CONFIRMED |
| SC-dialect `Sort`/`Unique`/`UniqueWithLaneIds`/`SegmentedScan` `::create` signatures | symbol mangling + producer sites | CONFIRMED |
| `DuplicateCountUniqueOpLowering<T>` → struct-extract multiplicity | 4 template instantiations + pattern-set | CONFIRMED |
| Whether `v1` is a single packed CSR+gain buffer vs. two buffers via one `Value` | shared operand + ComputeType confirmed; multiplexing not bit-proven | INFERRED |
| `StreamOptions` bits (edx=1/r8d=1) selecting indirect vs. linear DMA | per-id offset confirmed as gather index; the bools not decoded | LOW |
| `SortOp` `"dscd"` per-char semantics (direction / stability per key column) | read as a 4-char attr; per-char meaning not cross-decoded | LOW |
| Exact `ReduceDuplicates`→`SparseMapRow`→(`SortOp`+`ScanOp`) inlining chain | `SparseMapRow` carries the reduce fn + builds Sort/Scan window (strong); body not fully decoded | INFERRED |
| The precise emitter that creates the dialect `DuplicateCountOp` (no standalone `::create`; built inline) | LLVM lowering + `UniqueWithLaneIds` lane outputs confirmed; dialect-create producer not isolated | LOW |

---

## Cross-References

- [SampleCombiner Emitter](sample-combiner-emitter.md) — the `EmitSampleCombiner` caller and the `EmitVectorizedLoop` inner FMA this loop calls.
- [RankAndPermute / RadixSort](rank-and-permute-radixsort.md) — the sort/permute compute function where `UniqueOp` / `UniqueWithLaneIdsOp` consume the sorted keys.
- [Dedup Multiplicity](dedup-multiplicity.md) — the `DuplicateCount` → multiplicity and `Uniquify` inverse-permutation that weight the reduce.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the indirect-stream slot the per-id `InitiateSynchronousStreamOperation` issues into.
- [VectorExtended (VEX)](vectorextended-vex.md) — the vector datapath the inner per-feature-chunk FMA runs on.
- [Embedding Minibatching Decomposition](embedding-minibatching.md) — the HLO layer above, with the full gather→sort→uniquify→scan→scatter DAG.
- [Segmented Add Scan](segmented-add-scan.md) / [Scan Datapath](scan-datapath.md) — the `SegmentedScanOp` per-segment reduce the dedup feeds.
- [SparseCore Overview](overview.md) — the host-table → HBM → SC embedding datapath this loop is the per-id form of.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
