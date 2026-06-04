# Dedup Multiplicity

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

A SparseCore embedding lookup gathers many ids per sample, and the same id usually appears more than once in a sample's window — a DLRM categorical feature is a *multiset*, not a set. Before the [scatter-add](vectorload-slot.md#the-fetch-and-add-return-path) writes a gradient back into an embedding row, the pipeline collapses each repeated id to a single *unique* row and records how many input lanes collapsed onto it: the **multiplicity**. This page owns that arithmetic — how the `DuplicateCount` op and the HLO CSR row-pointers produce a per-unique count, how the `UniqueWithLaneIds` inverse permutation routes the deduplicated result back to every original lane, the in-vector fetch-and-add commit ordering (and why dedup makes it moot), and how the multiplicity binds into the forward combiner (`sum`/`mean`/`sqrtn`) and the backward gradient scale.

The mechanism lives at two altitudes that produce the same number by different means. On the **TEC** (the SparseCore vector engine), `DuplicateCount` is a single `VectorExtended` scan op (opcode 24/25) that emits, per lane, the count of lanes in the vector sharing that lane's id — an in-vector multiplicity. At the **HLO** level the `SparseDenseMatmulGradOpDecomposer` lowers the gradient apply into a Sort → COO→CSR → Cumsum-exclusive → Add-combiner scatter chain where the multiplicity is the run length of a unique row in the CSR `row_pointers` and the count× accumulation falls out of the Add-combiner summing every duplicate into one row. Both reduce to the same MLIR dedup ops (`sc_tpu.unique`, `sc_tpu.duplicate_count`, with/without lane ids), which lower through one templated pattern, `DuplicateCountUniqueOpLowering<OpT>`, to a HW intrinsic (`tpu_dupcnt{i,f}` / `tpu_uniq{i,f}`) returning an LLVM struct that the lowering field-extracts.

The decisive structural facts, all byte-confirmed: the HW dedup intrinsic *always* produces three outputs (two `vector<N×i32>` maps plus the input-dtype value); the MLIR op chooses whether to surface two or three of them via the `ReplaceOpWithExtracts` index count (`2` plain, `3` WithLaneIds). The struct field order is `{i32-vec, i32-vec, input-dtype}` and the extract index array re-orders it back to the op-result convention — `{2,1}` plain, `{2,1,0}` WithLaneIds. The surfaced third result is the lane→unique-row **inverse permutation** consumed by a `PermuteOp` immediately before the scatter. The fetch-and-add op carries **no atomic-ordering trait** — only LLVM alias-scope — so the intra-vector same-address commit order is silicon; dedup sidesteps it entirely by creating its scatter with `unique_indices=TRUE`, so no two lanes of a vector ever hit the same address.

For reimplementation, the contract is:

- **The dedup intrinsic emits three results; the op surfaces 2 or 3.** `inferReturnTypes` builds `result[0]=input dtype`, `result[1]=vector<N×i32>`, and (WithLaneIds only) `result[2]=vector<N×i32>`. The lowering builds a three-field struct `{i32-vec, i32-vec, input-dtype}` (the op-result order reversed) and the `ReplaceOpWithExtracts` index array (`{2,1}` or `{2,1,0}`) selects which struct fields become op results, re-ordering them back.
- **The multiplicity is a count, computed two ways that agree.** TEC: the `DuplicateCount` scan op's i32 result = per-lane in-vector id count. HLO: `csr_row_pointers[i+1] − csr_row_pointers[i]` = run length of unique row `i`, produced by `CumsumExclusive` (`Pad` + `ReduceWindow`-Add).
- **The count× accumulation is the Add-combiner of a `unique_indices=TRUE` scatter.** `DeduplicateGradientVectorsToApply` scatter-ADDs each input's gradient into its unique row with `CreateAddComputation` as the update computation; summing all duplicates into one row *is* the count× weighting. No explicit multiply is emitted on the backward path.
- **The forward combiner divisor is the per-sample `gains`.** `sum → 1`, `mean → 1/valency`, `sqrtn → 1/sqrt(valency)`, where `valency` = the per-sample id count = the multiplicity; realized by the SC `ReciprocalF32` / `ReciprocalSqrtF32` VectorAlu ops in `EmitValencyLoop`.
- **The lane_ids 3rd result routes the deduplicated result back to every input lane.** It is the inverse permutation (input-lane domain → unique-row codomain), consumed by `PermuteOp` (→ `tpu_sc_permute` / `llvm.tpu.vperm.sublane`) just before the `VectorStoreIdx` / fetch-and-add scatter.
- **In-vector commit order is silicon, made moot by `unique_indices`.** The fetch-and-add (`tpu_vst_msk_idx_ret_add_np`) has `AccessGroup` + `AliasAnalysis` traits and *no* `AtomicOrdering`; the dedup scatter is created `unique_indices=TRUE` so the per-lane order can never affect the result.

| | |
|---|---|
| **Dedup ops** | `sc_tpu.unique` · `…unique_with_lane_ids` · `…duplicate_count` · `…duplicate_count_with_lane_ids` |
| **Lowering template** | `DuplicateCountUniqueOpLowering<OpT>::matchAndRewrite` — 4 instantiations |
| **HW intrinsics** | `llvm.tpu.dupcnt{i,f}` · `llvm.tpu.uniq{i,f}` (3-field struct, `NOperands<2>` `OneResult`) |
| **TEC opcodes** | `DuplicateCount{Integer=24,Float=25}` · `Uniquify{Integer=26,Float=27}` @ word `0x28` bits 16..21 |
| **Result types** | `[0]`=input dtype · `[1]`=`vector<N×i32>` · `[2]`=`vector<N×i32>` (WithLaneIds) |
| **HLO decomposer** | `SparseDenseMatmulGradOpDecomposer` (`Deduplicate{RowIds,GradientVectors}ToApply`, `CumsumExclusive`, `LocalReduction`) |
| **Fetch-and-add** | `tpu_vst_msk_idx_ret_add_np` — `NOperands<4>`, `OneResult`, `AccessGroup`+`AliasAnalysis`, **no AtomicOrdering** |
| **Dedup flag** | `xla_tpu_enable_sparse_core_computation_deduplication` (`0x223c5f10`) |

> **NOTE — this page owns the count→multiplicity arithmetic, the `WithLaneIds` inverse permutation, and the in-vector commit ordering.** The bundle-level `Sort`/`Uniquify`/`DuplicateCount` opcodes live in [VectorExtended](vectorextended-vex.md); the SSA emission sequence (Unique → IdxAdd → Permute → StoreIdx) lives in [RankAndPermute](rank-and-permute-radixsort.md); the fetch-and-add return path and `SourceOne` seed live in [VectorLoad Slot](vectorload-slot.md); the per-sample valency loop lives in [Emit Valency Loop](emit-valency-loop.md). They are linked, not repeated.

---

## The Dedup Op → Intrinsic → Result Map

### Purpose

Four MLIR `sc_tpu.*` ops express the dedup primitive. `unique` and `duplicate_count` differ only in what `result[0]` means (unique values vs per-position counts); the `…_with_lane_ids` variants add a third result, the inverse permutation. All four lower through *one* templated pattern to *one* HW intrinsic that always produces a three-field struct — the op merely chooses how many fields to extract.

### The Four Ops

| MLIR op (`sc_tpu.*`) | operands | results | intrinsic (int / float) | extract idx |
|---|---:|---:|---|---|
| `unique` | 2 | 2 | `uniqi` / `uniqf` | `{0,1}` |
| `unique_with_lane_ids` | 2 | 3 | `uniqi` / `uniqf` | `{0,1,2}` |
| `duplicate_count` | 2 | 2 | `dupcnti` / `dupcntf` | `{0,1}` |
| `duplicate_count_with_lane_ids` | 2 | 3 | `dupcnti` / `dupcntf` | `{0,1,2}` |

The result types are set by `inferReturnTypes` (see [§The Result Types](#the-result-types)):

```text
result[0] = INPUT DATA TYPE   (unique: the unique-value vector;
                               duplicate_count: the per-position count vector)
result[1] = vector<N × i32>   (N = input shape; secondary i32 map)
result[2] = vector<N × i32>   (WithLaneIds only: lane→unique-row INVERSE permutation)
```

### The Lowering — One Template, Three Struct Fields

`DuplicateCountUniqueOpLowering<OpT>::matchAndRewrite` is instantiated once per op. Each instance reads the op's N result types, `convertType`s each, builds an `LLVMStructType::getLiteral` of those types, emits the HW intrinsic via two type-builder lambdas (lambda #1 → integer intrinsic, lambda #2 → float), and calls `ReplaceOpWithExtracts` with a 2- or 3-element index array:

```c
function DuplicateCountUniqueOpLowering<OpT>::matchAndRewrite(op, rewriter):  // e.g. 0x13599d40
    // read the op's result types (3 reads even for the plain op: add edx,0x3)
    t0 = convertType(op.getNextResultAtOffset(0))     // input dtype
    t1 = convertType(op.getNextResultAtOffset(1))     // vector<N x i32>
    t2 = convertType(op.getNextResultAtOffset(2))     // vector<N x i32>  (built regardless)
    structTy = LLVMStructType::getLiteral({t0,t1,t2}) // 0x17471ae0; header pack 0x600000003
    if  isIntegerElementType:
        v = tpu_dupcnti::create(rewriter, loc, structTy, ...)   // 0x146e23c0 (uniqi 0x149895c0)
    else:
        v = tpu_dupcntf::create(rewriter, loc, structTy, ...)   // 0x146e1bc0 (uniqf 0x14988dc0)
    indices = {0,1}      if plain                     // mov QWORD [rbp-0x60], 0x2
            = {0,1,2}    if WithLaneIds               // mov QWORD [rbp-0x60], 0x3
    ReplaceOpWithExtracts(rewriter, loc, op, structTy, v, indices)   // 0x135b82a0
```

`ReplaceOpWithExtracts` (@0x135b82a0) is byte-confirmed to loop over the index array — `do { ... } while (4*count != offset)`, stepping 4 bytes per `unsigned` index — emitting one `LLVM::ExtractValueOp::create` per index, then calling `replaceOp` through the rewriter vtable to substitute the extracted SSA values for the original op's results:

```c
// xla::tpu::sparse_core::ReplaceOpWithExtracts                          (0x135b82a0)
for (i = 0; i < count; ++i) {                          // count = ArrayRef length (2 or 3)
    extractIndex = indices[i];                         // v20[2] = *(uint*)(a7 + 4*i)
    ev = mlir::LLVM::ExtractValueOp::create(builder);  // one extract per surfaced field
    results.push_back(ev);
}
rewriter.replaceOp(op, ValueRange(results));           // (**v9)(...) vtable call
```

> **QUIRK — the HW intrinsic always emits three lanes of output; the MLIR op picks two or three.** Even the *plain* `unique` / `duplicate_count` lowering reads three result types and builds a three-field struct (`add edx, 0x3`; three `getNextResultAtOffset` reads), then extracts only `{0,1}`. The `lane_ids` third field is computed by the silicon either way and discarded by the plain op. A reimplementer who sizes the intrinsic struct to the *op's* result count (2) will mis-decode the WithLaneIds form, whose third field lives at the same struct offset the plain op leaves unread.

### Lowering / Intrinsic Address Map

| Symbol (gfc) | Address | Role |
|---|---|---|
| `DuplicateCountUniqueOpLowering<DuplicateCountOp>::matchAndRewrite` | `0x13599d40` | plain count lowering (extract `{0,1}`) |
| `…<DuplicateCountWithLaneIdsOp>::matchAndRewrite` | `0x1359a7e0` | count + lane_ids (extract `{0,1,2}`) |
| `…<UniqueOp>::matchAndRewrite` | `0x1359b280` | plain unique lowering (extract `{0,1}`) |
| `…<UniqueWithLaneIdsOp>::matchAndRewrite` | `0x1359bd20` | unique + lane_ids (extract `{0,1,2}`) |
| `ReplaceOpWithExtracts` | `0x135b82a0` | per-index `ExtractValueOp` + `replaceOp` |
| `tpu_dupcnti::create` / `tpu_dupcntf::create` | `0x146e23c0` / `0x146e1bc0` | integer / float count intrinsic |
| `tpu_uniquei::create` / `tpu_uniquef::create` | `0x149895c0` / `0x14988dc0` | integer / float unique intrinsic |

> **NOTE — the intrinsic op itself is `NOperands<2>`, `OneResult`, `OneTypedResult<Type>`.** The single result is the `LLVMStructType` literal; the *lowering* (not the intrinsic) splits it into 2 or 3 op results. The LLVM intrinsic names are `llvm.tpu.dupcnti` / `.dupcntf` / `.uniquei` / `.uniquef`. The value semantics of `result[0]` for `duplicate_count` (the per-position broadcast count) vs `unique` (the unique values) are read from the op name, not a per-field accessor; the field *type* is `= input dtype`.

---

## The Result Types

### Purpose

`inferReturnTypes` is the single source of truth for how many results each op has and what their types are. It is the function a reimplementer must reproduce to wire the dedup op into a type-checked IR. The two-result and three-result forms are byte-identical in structure except for one extra push.

### The WithLaneIds Build (3 results)

`UniqueWithLaneIdsOp::inferReturnTypes` (@0x145a0040) takes the op's `ValueRange` operands, reads the input data type from operand 0 (`& ~7` to strip the pointer tag bits), takes the shape from the *last* operand, builds a signless i32 `VectorType::get(shape, i32)`, and pushes **three** result types — the input type once, the `vector<N×i32>` **twice**:

```c
// mlir::sparse_core::UniqueWithLaneIdsOp::inferReturnTypes                  (0x145a0040)
inputTy = dereference_iterator(operands, 0)              // v11 → result[0]
shape   = VectorType::getShape(operands[last] & ~7)      // 0x1d895080
i32     = IntegerType::get(ctx, 32, /*signless*/0)       // 0x1d8c60c0
vecI32  = VectorType::get(shape, i32)                    // 0x1d894100
results.push_back(inputTy & ~7)                          // result[0] = input data type
results.push_back(vecI32)                                // result[1] = vector<N x i32>
results.push_back(vecI32)                                // result[2] = vector<N x i32>  (lane→unique map)
return 1
```

The three `grow_pod`-guarded stores into the `SmallVectorImpl<Type>` (a11) are byte-confirmed: `v17` (input type) once, `v16` (the `vector<N×i32>` value) twice. `DuplicateCountWithLaneIdsOp::inferReturnTypes` (@0x1459ff00) is structurally identical (three pushes).

### The Plain Build (2 results)

`UniqueOp::inferReturnTypes` (@0x1459fe00) and `DuplicateCountOp::inferReturnTypes` (@0x1459fd00) push **two** types — the input type then `vector<N×i32>` once:

```c
// mlir::sparse_core::UniqueOp::inferReturnTypes                            (0x1459fe00)
inputTy = dereference_iterator(operands, 0) & ~7         // v13
results.push_back(inputTy)                               // result[0] = input data type
shape   = VectorType::getShape(operands[last] & ~7)
vecI32  = VectorType::get(shape, IntegerType::get(ctx,32,0))
results.push_back(vecI32)                                // result[1] = vector<N x i32>
return 1
```

| inferReturnTypes | Address | Results pushed |
|---|---|---|
| `UniqueWithLaneIdsOp` | `0x145a0040` | input · vec<i32> · vec<i32> (3) |
| `DuplicateCountWithLaneIdsOp` | `0x1459ff00` | input · vec<i32> · vec<i32> (3) |
| `UniqueOp` | `0x1459fe00` | input · vec<i32> (2) |
| `DuplicateCountOp` | `0x1459fd00` | input · vec<i32> (2) |

> **GOTCHA — `result[1]` and `result[2]` are the *same type* `vector<N×i32>`, but not the same semantics.** They are built by one shared `VectorType::get` call and pushed twice, so a type-only check cannot tell them apart. `result[2]` is the `lane_ids` inverse permutation (consumed by `PermuteOp`); `result[1]` is a secondary i32 map (forward permutation / segment-start / count-index). The role split of `[1]` vs `[2]` is read from the op-name suffix and the `PermuteOp` consumer convention, not from a distinct per-field accessor.

---

## The Multiplicity Arithmetic — Two Levels

### Purpose

The "how many times does this unique id appear" count is produced at two granularities that the pipeline keeps as parallel mechanisms: the TEC in-vector primitive (per-tile, one vector at a time) and the HLO CSR row-pointers (cross-tile, the full minibatch window). Both yield an i32 count; the page documents each and marks the un-traced handoff.

### TEC In-Vector — the `DuplicateCount` Scan Op

On the TEC, `DuplicateCount` is a unary `VectorExtended` (EUP scan-family) op. `DuplicateCountInteger` is opcode **24** (`@0x1eca7b20`, `cmp 0x180000` of word `0x28` bits 16..21 >> 16); `DuplicateCountFloat` is 25. It is emitted as `EmitVectorResultUnop<…DuplicateCountInteger>` (@0x13aaf660): one input VREG (`GetVregno`), a `GetVectorMask`, a `FindAndEmitToUnusedPort` to claim a free V read port, and the count result drained through the `VectorResult` XRF→VRF path — the same drain model as the [scan ops](vectorextended-vex.md) it shares the slot with.

```c
// EmitVectorResultUnop<…DuplicateCountInteger>                            (0x13aaf660)
in    = GetVregno(input)                  // the id vector
mask  = GetVectorMask(...)
port  = FindAndEmitToUnusedPort(...)      // one free V read port
emit  DuplicateCountInteger(in, mask) → VectorResult (XRF)
// per lane: result[lane] = #{ j : id[j] == id[lane] }  — the in-vector multiplicity
```

The op gives, per lane, the multiplicity of that lane's value across the vector. This i32 count is the value the combiner uses as the `mean` divisor (forward) or the gradient scaler (backward).

### HLO Decomposition — CSR Row-Pointers and the Add-Combiner

The minibatch gradient apply is lowered by `SparseDenseMatmulGradOpDecomposer`. The multiplicity here is the run length of a unique id in the CSR `row_pointers`, and the count× accumulation is the Add-combiner of a `unique_indices=TRUE` scatter:

```text
SparseDenseMatmulGradOpDecomposer — backward dedup-reduce
  Sort (sample,id) pairs           SortVectorsAndGainsBySampleIds  0x1367c5a0
    → COO → CSR                     CooToSparseMatrixFormat         0x13412480
        csr_row_pointers[i+1] − csr_row_pointers[i] = multiplicity of unique row i
    → exclusive prefix sum          CumsumExclusive = Pad + ReduceWindow(Add)  0x134b3ec0
        = each input's destination unique-row index (the multiplicity INDEX)
    → DeduplicateRowIdsToApply      0x134b4560  Scatter(unique=TRUE)+Overwrite+MaxValue+Ternary
        collapses duplicate ids onto the unique slot
    → DeduplicateGradientVectorsToApply  0x134b4e40
        Scatter(update=AddComputation, unique=TRUE) + LiteralUtil::Zero
        scatter-ADDs each input gradient into its unique row → count× accumulation
    → LocalReduction                0x134b01e0  ReduceWindow + Scatter(Add) + Gather/Compare/Ternary
        windowed sum over the run of duplicate rows of one unique id
```

`DeduplicateGradientVectorsToApply` (@0x134b4e40) is byte-confirmed to: create two HLO parameters (`cumsum_exclusive`, `gradient_vectors`); zero-initialize the accumulator via `LiteralUtil::Zero` + `CreateBroadcast` (`accumulated_gradients_zero_init`); build the scatter's update computation with `CreateAddComputation`; and emit `CreateScatter(operand, scatter_indices, updates, AddComputation, dim_numbers, indices_are_sorted=0, unique_indices=…)`. The trailing scatter bool args are byte-confirmed: `push 0x0` = `indices_are_sorted=false`, `push 0x1` = `unique_indices=true`.

> **QUIRK — there is no explicit `× count` on the backward path.** The count× weighting is *implicit* in the Add-combiner: every one of the `count` duplicate ids contributes its gradient once, and the scatter's `AddComputation` sums them all into the single unique row, so the collapsed row accumulates exactly `count ×` the per-input gradient. A reimplementer looking for a `MulOp` by the multiplicity on the backward path will not find one — the multiply is the fold of the Add-combiner over the run of duplicates.

### Forward Combiner — the `gains` Divisor

The forward reduction is a segmented sum scaled by a per-id **gain**. The combiner is one of three, byte-confirmed from the backend-config attr strings `"sum"` / `"mean"` / `"sqrtn"`:

| Combiner | `gains` | realized by |
|---|---|---|
| `sum` | `1` | (no scale) |
| `mean` | `1 / valency` | `ReciprocalF32` (`EmitExtendedVectorVxUnop` `0x13a1dc00`) |
| `sqrtn` | `1 / sqrt(valency)` | `ReciprocalSqrtF32` (`0x13a1de00`) |

`valency` = the per-sample id count = the multiplicity. The gains are applied per-sample inside `SparseDenseMatmulDotCombinerEmitter::EmitValencyLoop` (@0x1332cee0); `SortVectorsAndGainsBySampleIds` (@0x1367c5a0) sorts the gathered rows *and* their gains by sample id before the segmented reduce. The config fields carrying this are `gains`, `csr_row_pointers`, `has_reciprocal`, `has_reciprocal_sqrt`, `divisor`, `max_valency`, `feature_length`, `valency`. Bf16 reciprocal variants (`0x13a1dd00` / `0x13a1df00`) exist for the half-precision path.

> **NOTE — whether the TEC in-vector `DuplicateCount` and the HLO CSR multiplicity are ever combined is not bit-traced.** Both produce a count, but the per-tile in-vector primitive and the cross-minibatch CSR row-pointer count are parallel mechanisms; which one feeds which scatter at which granularity is not resolved. The custom-combiner gains arithmetic (`SparseDenseMatmulCustomCombinerOpDecomposer` `0x1366cf80`) defines gains beyond `sum`/`mean`/`sqrtn` and is not traced.

---

## The Uniquify Inverse Permutation

### Purpose

The dedup loop computes once per *unique* row, but the embedding op must produce a result for every *input* lane. The third result of the `…_with_lane_ids` ops is the map that closes that loop: for each original input lane, the unique row it collapsed onto — an inverse permutation that a `PermuteOp` uses to fan the per-unique result back to every input position.

### The Map and Its Type

`result[2]` ("lane_ids") is `vector<N×i32>`, the same shape as the input (built by the same `VectorType::get(inputShape, i32)` in `inferReturnTypes` above). It is the **inverse** permutation: domain = input-lane index, codomain = unique-row index. Reading lane `k` gives the unique representative that input id `k` mapped to.

### The Consumer — `PermuteOp` Before the Scatter

The emission sequence in `RankAndPermuteComputeFunction` (@0x134039c0) — the SC radix-sort emitter — is byte-confirmed:

```text
UniqueOp                  create @0x134042a9   (sc_tpu.unique)
UniqueWithLaneIdsOp       create @0x13404303   (sc_tpu.unique_with_lane_ids → lane_ids = result[2])
VectorLoadStoreIdxAddOp   create @0x1340456c   (rank / fetch-and-add over the unique window)
PermuteOp                 create @0x13404771   (data, lane_ids) → fan result back to input lanes
VectorStoreIdxOp          create @0x134049cb   (write the permutation index vector)
```

`PermuteOp` is `NOperands<2>`, `OneResult` (`create` @0x145f3920; lowering @0x135a1640 → `tpu_sc_permute` / `llvm.tpu.vperm.sublane`). It takes `(data, lane_ids)` and applies the permutation, routing each per-unique-row result back to the original input lane positions, immediately before the `VectorStoreIdx` / fetch-and-add scatter writes the result. The second caller is `(anon)::TransferOperandSlices` (@0x13d202e0), within an `ElementScatterContext` — the scatter consumer that uses the lane-id map to route slices.

> **NOTE — the plain `unique` op has no `lane_ids` and skips the `PermuteOp`.** The emitter selects `UniqueWithLaneIds` (3 results) vs `Unique` (2 results) by a target capability query; in the plain path the `lane_id` Value is null and the `PermuteOp` step is omitted entirely (see [RankAndPermute](rank-and-permute-radixsort.md#the-unique-ssa-shape)). A reimplementation that always emits `PermuteOp` will fault on a null operand in the no-lane-ids path. The HW datapath of `tpu_sc_permute` (the sublane-permute network width / cycles) is silicon.

---

## The In-Vector Fetch-and-Add Commit Order

### Purpose

When two lanes of one vector scatter-add to the same address, the order in which their read-modify-adds commit determines the result of a *raw* (non-deduplicated) scatter. This section documents what the IR does and does not specify, and why the dedup path is immune.

### No Atomic Ordering — Only Alias Scope

The fetch-and-add op `tpu_vst_msk_idx_ret_add_np` carries this trait pack, byte-confirmed from its `getHasTraitFn` template instantiation (@0x14a6ac60):

```text
ZeroRegions · OneResult · OneTypedResult<Type> · ZeroSuccessors · NOperands<4u>
            · OpInvariants · BytecodeOpInterface::Trait
            · LLVM::AccessGroupOpInterface::Trait · LLVM::AliasAnalysisOpInterface::Trait
```

There is **no `AtomicOrdering` trait and no `MemoryEffectsOpInterface` ordering attribute**. The only memory interfaces are `AccessGroup` and `AliasAnalysis` — LLVM alias-scope metadata for the *scheduler*, not atomic ordering. The plain scatter-add `tpu_vst_msk_idx_add` is `NOperands<4>`, `ZeroResult` (no return). The fetch-and-add returns the **pre-add** value (it reads through the load-Dest mux — see [VectorLoad Slot](vectorload-slot.md#the-fetch-and-add-return-path)); its LLVM intrinsics are `llvm.tpu.vst.msk.idx.ret.add.{np,e4m3,e5m2}` (no `.cb` form).

The lowering `VectorLoadStoreIdxAddOpLowering::matchAndRewrite` (@0x135c3ba0) computes a strided element pointer (`MemRefDescriptor::stride` + `LLVM::MulOp` + `LLVM::AddOp` = `base + index*stride`), builds the index vector (`ShuffleVectorOp` / `InsertElementOp`), reads the mask (`getBoolVectorAttr`), and emits **one** `tpu_vst_msk_idx_ret_add_np::create`. There is no loop, no atomic construct, no per-lane serialization in the lowering — it is a single HW vector op.

```text
intra-vector same-address fetch-and-adds within ONE vector:
   IR says nothing about order  →  it is silicon  (a read-modify-add HAZARD)
cross-vector order:
   sequential program order of the scatter instructions (each is one ordered HW op)
```

The hardware *detects* same-address conflicts: the SC telemetry counter "Address match conflict count" (in the `.rodata` string pool next to "Stream wait count") and the conflict-stall strings ("must wait more that 16 cycles due to conflicting accesses by Vector Store instructions") establish that the HW serializes conflicting same-address accesses with stall cycles — the combine is an *ordered* read-modify-add, not a free parallel reduce.

> **GOTCHA — for a raw scatter the intra-vector commit order is undefined here.** The exact per-lane priority (low-lane-first / high-lane-wins / tree-combine) for concurrent same-address fetch-and-adds within one vector is not in the C++ — it is silicon. This matters only for a deduplication-*disabled* scatter (gated by `xla_tpu_enable_sparse_core_computation_deduplication` @0x223c5f10); for the dedup path it is moot.

### Why Dedup Makes It Moot

Both `DeduplicateRowIdsToApply` (@0x134b4560) and `DeduplicateGradientVectorsToApply` (@0x134b4e40) create their HLO `Scatter` with `unique_indices=TRUE` (byte-confirmed: `push 0x1` = arg `unique_indices`; `push 0x0` = arg `indices_are_sorted`). After Uniquify, each unique row is touched exactly once, so no two lanes of a vector ever fetch-and-add the same address — the per-lane commit order can never affect the result. The `AliasScopeAssignment` / `MemrefAliasScopeAnnotationPass` (@0x135d2060) attaches LLVM `!alias.scope` / `!noalias` metadata to the deduplicated scatter ops (the `AliasAnalysisOpInterface` trait), encoding the non-aliasing for the scheduler. The dedup *is* the correctness mechanism; the silicon in-vector order is only reachable on the raw path.

---

## The Complete Dedup-Reduce Pipeline

`DuplicateCount` and `Uniquify` sit in the middle of the SparseCore embedding dedup-reduce datapath. The full composition (the other stages owned by the linked pages):

```text
SparseCore embedding dedup-reduce — 8 stages
 1. GATHER   Stream IndirectStream         id list → HBM[base+id*stride] → TILE_SPMEM   (stream-gather-scatter)
 2. SORT     SortVectorsAndGainsBySampleIds → HLO Sort; TEC VectorExtended SortInteger 20/21
 3. COO→CSR  CooToSparseMatrixFormat        csr_row_pointers = run lengths = multiplicity
 4. UNIQUIFY UniqueWithLaneIds (op 26/27)   collapse duplicates + lane→unique inverse perm (result[2])
 5. COUNT    DuplicateCount (op 24/25)      per-unique multiplicity i32                  ◄── THIS PAGE
 6. REDUCE   forward: SegmentedAddScan sum × gains{1, 1/valency, 1/√valency}             ◄── THIS PAGE
             backward: scatter-ADD gradient into each unique row ONCE (unique_indices=TRUE,
                       Add-combiner = count× accumulation)                               ◄── THIS PAGE
 7. PERMUTE  PermuteOp + lane_ids → tpu_sc_permute   route per-unique result → input lanes  ◄── THIS PAGE
 8. SCATTER  VectorLoadStoreIdxAdd fetch-and-add / VectorStoreIdx; pre-add return;
             no atomic ordering; conflict-free by unique_indices + alias-scope           (vectorload-slot)
```

Stages 4–7 (this page) collapse the duplicates, count the multiplicity, fold it into the combiner, and route the result back. The radix-sort SSA emission that realizes stages 2–7 is owned by [RankAndPermute](rank-and-permute-radixsort.md); the per-sample valency loop that applies the gains is owned by [Emit Valency Loop](emit-valency-loop.md); the bundle-level opcodes 20/21/24/25/26/27 are owned by [VectorExtended](vectorextended-vex.md).

---

## Per-Generation Table

The dedup op set and the HLO decomposer are gen-stable: `SparseDenseMatmulGradOpDecomposer` is parameterised by `jellyfish::Target`, so it covers all SC generations, and the per-gen `VectorExtended` opcode field position is gen-stable (6-bit @ word `0x28` bits 16..21 across vfc/glc/gfc).

| Quantity | VF (vfc, v5e) | GL (glc, v5p) | GF (gfc, v6e) |
|---|---|---|---|
| `DuplicateCount{Integer,Float}` VEX op | yes | yes | yes |
| `Uniquify{Integer,Float}` VEX op | yes | yes | yes |
| `DuplicateCountInteger` opcode | 24 (`0x180000`) | 24 | 24 |
| `UniquifyInteger` opcode | 26 (`0x1a0000`) | 26 | 26 |
| `tpu_dupcnt{i,f}` / `tpu_uniq{i,f}` intrinsic | yes | yes | yes |
| `sc_tpu.*_with_lane_ids` (3-result) | yes | yes | yes |
| `lane_ids` 3rd result = `vector<N×i32>` | yes | yes | yes |
| `tpu_vst_msk_idx_ret_add_np` (fetch-and-add) | yes | yes | yes |
| AtomicOrdering on fetch-and-add | NO | NO | NO |
| dedup Scatter `unique_indices=TRUE` | yes | yes | yes |

Per-gen `Matches()` anchors for the TEC opcodes: gfc `DuplicateCountInteger` @0x1eca7b20 / `UniquifyInteger` @0x1eca7b60; vfc @0x1e9ae0c0 / @0x1e9ae100; glc @0x1eb2d320 / @0x1eb2d360.

---

## Function Map

| Symbol (gfc) | Address | Role |
|---|---|---|
| `DuplicateCountUniqueOpLowering<DuplicateCountOp>::matchAndRewrite` | `0x13599d40` | plain count lowering, extract `{0,1}` |
| `…<DuplicateCountWithLaneIdsOp>::matchAndRewrite` | `0x1359a7e0` | count + lane_ids, extract `{0,1,2}` |
| `…<UniqueOp>::matchAndRewrite` | `0x1359b280` | plain unique lowering |
| `…<UniqueWithLaneIdsOp>::matchAndRewrite` | `0x1359bd20` | unique + lane_ids |
| `ReplaceOpWithExtracts` | `0x135b82a0` | per-index `ExtractValueOp` + `replaceOp` |
| `UniqueWithLaneIdsOp::inferReturnTypes` | `0x145a0040` | 3 results (input · vec<i32> · vec<i32>) |
| `DuplicateCountWithLaneIdsOp::inferReturnTypes` | `0x1459ff00` | 3 results (identical structure) |
| `UniqueOp::inferReturnTypes` | `0x1459fe00` | 2 results (input · vec<i32>) |
| `DuplicateCountOp::inferReturnTypes` | `0x1459fd00` | 2 results |
| `EmitVectorResultUnop<…DuplicateCountInteger>` | `0x13aaf660` | TEC in-vector count emitter (op 24) |
| `DeduplicateGradientVectorsToApply` | `0x134b4e40` | Add-combiner scatter, `unique_indices=TRUE` |
| `DeduplicateRowIdsToApply` | `0x134b4560` | Overwrite scatter, collapse ids |
| `CumsumExclusive` | `0x134b3ec0` | `Pad` + `ReduceWindow(Add)` → CSR row index |
| `LocalReduction` | `0x134b01e0` | windowed sum over duplicate run |
| `CooToSparseMatrixFormat` | `0x13412480` | COO → CSR; row-pointer run lengths |
| `SortVectorsAndGainsBySampleIds` | `0x1367c5a0` | sort rows + gains by sample id |
| `EmitValencyLoop` | `0x1332cee0` | per-sample gains application |
| `ReciprocalF32` / `ReciprocalSqrtF32` emitters | `0x13a1dc00` / `0x13a1de00` | `mean` / `sqrtn` gain |
| `PermuteOp::create` / lowering | `0x145f3920` / `0x135a1640` | apply lane_ids inverse perm |
| `tpu_vst_msk_idx_ret_add_np` trait fn | `0x14a6ac60` | fetch-and-add traits (no AtomicOrdering) |
| `VectorLoadStoreIdxAddOpLowering::matchAndRewrite` | `0x135c3ba0` | one HW op, no serialization |
| `MemrefAliasScopeAnnotationPass::runOnOperation` | `0x135d2060` | `!noalias` on dedup scatters |

---

## Considerations

- **Reproduce `inferReturnTypes` exactly.** Build `result[0]=input dtype`, `result[1..]=vector<N×i32>` (twice for WithLaneIds). The two i32 vectors share a type; only the op-name suffix distinguishes the `lane_ids` map (`result[2]`) from the secondary map (`result[1]`).
- **Size the intrinsic struct to three fields, not the op's result count.** The HW intrinsic always returns `{value, vec<i32>, vec<i32>}`; the lowering surfaces 2 or 3 via the `ReplaceOpWithExtracts` index array. A 2-field struct mis-decodes the WithLaneIds form.
- **The backward count× is the Add-combiner, not a multiply.** Emit the gradient scatter with `unique_indices=TRUE` and `CreateAddComputation`; the count× accumulation falls out of summing duplicates. Do not add a `× multiplicity` op on the backward path.
- **The forward divisor is the gains, applied per-sample.** `sum → 1`, `mean → 1/valency`, `sqrtn → 1/sqrt(valency)` via the SC reciprocal ops in `EmitValencyLoop`; `valency` is the multiplicity.
- **Skip `PermuteOp` in the plain-unique path.** `lane_ids` exists only on the `…_with_lane_ids` ops; the plain path stores the rank directly and emits no `PermuteOp`.
- **In-vector ordering is silicon; dedup makes it moot.** The fetch-and-add has no atomic-ordering trait; rely on `unique_indices=TRUE` + alias-scope, not on a deterministic per-lane commit order.
- **Unmapped.** The raw (dedup-disabled) intra-vector commit priority; the `result[1]` vs `result[2]` role split (`[2]`=lane_ids, `[1]` secondary map); the `result[0]` value semantics for `duplicate_count` (count broadcast); the custom-combiner gains arithmetic; the TEC-in-vector vs HLO-CSR multiplicity handoff; the `tpu_sc_permute` HW datapath.

---

## Related Components

| Name | Relationship |
|---|---|
| `DuplicateCountUniqueOpLowering<OpT>` (`0x13599d40`+) | the one template that lowers all four dedup ops to the 3-field intrinsic |
| `ReplaceOpWithExtracts` (`0x135b82a0`) | selects 2 vs 3 surfaced results via the index-array length |
| `SparseDenseMatmulGradOpDecomposer` (`Deduplicate*` `0x134b4560`/`0x134b4e40`) | the HLO multiplicity decomposition (CSR + Add-combiner scatter) |
| `SparseDenseMatmulDotCombinerEmitter::EmitValencyLoop` (`0x1332cee0`) | applies the `sum`/`mean`/`sqrtn` gains (the forward multiplier) |
| `tpu_vst_msk_idx_ret_add_np` (trait fn `0x14a6ac60`) | the fetch-and-add the dedup feeds; no atomic ordering, alias-scope only |
| `RankAndPermuteComputeFunction` (`0x134039c0`) | the SSA emission sequence (Unique → IdxAdd → Permute → StoreIdx) |

## Cross-References

- [RankAndPermute and the Radix-Sort Ordering](rank-and-permute-radixsort.md) — the SSA emission sequence that creates the Unique / Permute / scatter ops documented here
- [VectorExtended (VEX)](vectorextended-vex.md) — the bundle-level `Sort`/`Uniquify`/`DuplicateCount` opcodes (20/21/24/25/26/27) and the scan-emitter model
- [VectorLoad Slot](vectorload-slot.md) — the fetch-and-add return path (pre-add through the load-Dest mux) and the `SourceOne` scan seed
- [Emit Valency Loop](emit-valency-loop.md) — the per-sample valency loop that consumes the multiplicity and applies the gains
- [Sample Combiner Emitter](sample-combiner-emitter.md) — the embedding combiner that drives the sort → dedup → gather → reduce datapath
- [SparseCore Overview](overview.md) — where the dedup-reduce sits among the SC engine classes
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
