# RankAndPermute and the Radix-Sort Ordering

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`RankAndPermute` is the per-digit body of the SparseCore embedding radix sort: the pass that turns a column of sorted embedding ids into a *gather permutation index vector* — a map from each original id to the slot of its deduplicated representative. It is emitted by `RadixSortEmitterInternal` (the MLIR-level emitter that the SparseCore dialect [`SortOp`](vectorextended-vex.md) lowers to), and it is the SSA-emission counterpart of the bundle-level `Sort`/`Uniquify`/`DuplicateCount` opcodes documented on the [VectorExtended](vectorextended-vex.md) page. This page owns three things: the **`RankAndPermuteComputeFunction` SSA shape** (the `UniqueOp` / `UniqueWithLaneIdsOp` dedup plus the inverse permutation), the **`SparseMapRow` sort+reduce-by-row** (op-type `0x4`), and how the **radix digit drives the ordering** before the gather.

The structural idea is classic LSD radix sort, lowered to MLIR ops instead of a scalar loop. Each digit pass extracts a digit `(key >> shift) & mask` from the sorted key, deduplicates on the `(key, digit)` pair, builds a *rank* for each unique value with an indexed scatter-add, then permutes the values into rank order. The compute function is the per-chunk callback; the `RankAndPermute` wrapper tiles it across the id stream with two `CreateChunkIteratorLoop` passes. The whole structure is keyed on the SparseCore `Unique`/`Permute`/`VectorLoadStoreIdxAdd`/`VectorStoreIdx` dialect ops, which lower to TEC [VEX](vectorextended-vex.md) reduction bundles.

For a reimplementer, the contract is:

- **The dedup key is the `(sorted_key, radix_digit)` pair.** `radix_digit = (sorted_key >> shift) & mask` is computed by `GetDigits` from the `RadixSortEmitterInternal` members; both `UniqueOp` and `UniqueWithLaneIdsOp` take the original sorted-key Value and the digit as their two operands.
- **`UniqueOp` yields 3 results; `UniqueWithLaneIdsOp` yields 5.** A capability/flag query selects the with-lane-ids form, whose two extra results are the lane-id / per-unique multiplicity that weights the permute.
- **The rank is an indexed scatter-add, the permute is the inverse.** `VectorLoadStoreIdxAddOp` builds the prefix-position rank of each unique id; `PermuteOp(rank, lane_id)` re-orders the values; `VectorStoreIdxOp` writes the resulting permutation index vector, keyed by the original sorted key.
- **`SparseMapRow` (op `0x4`) is a descending sort plus a scan.** It sorts a row's keys with the `"dscd"` direction StringAttr so duplicate token-ids become adjacent, then a `"max"` scan sizes the per-row output window; the carried `ReduceDuplicates` reduce-fn collapses the now-adjacent duplicates into one ELL row.

| | |
|---|---|
| **Compute fn** | `RadixSortEmitterInternal::RankAndPermuteComputeFunction` `0x134039c0` (0x1240 B) |
| **Wrapper** | `RadixSortEmitterInternal::RankAndPermute` `0x13404dc0` (0xE40 B) |
| **IR level** | MLIR — SparseCore (`sc_tpu.*`) dialect ops, pre-LLVM-lowering |
| **Dedup primitives** | `UniqueOp::create` `0x14622400` (3 results) · `UniqueWithLaneIdsOp::create` `0x146231a0` (5 results) |
| **Rank / permute** | `VectorLoadStoreIdxAddOp::create` `0x14634fc0` · `PermuteOp::create` `0x145f3920` |
| **Output store** | `VectorStoreIdxOp::create` `0x14638460` (`sc_tpu.vector_store_idx`) |
| **Digit extract** | `GetDigits` `0x133fe480` · `GetMappedKeysForRadixSort` `0x133fe8c0` |
| **Caller chain** | `EmitSort` `0x131c5fa0` → `SingleDigitRadixSort` `0x1340c580` / `…WithSoftwareCoalescing` `0x1340c740` → `RankAndPermute` |
| **SparseMapRow window** | `FusionEmitter::SetOutputWindowBoundsForSparseMapRow` `0x13890d40` (0x9c0 B) |
| **Source file** | `platforms/xla/sparse_core/lowering/internal/radix_sort_emitter_internal.cc` (`0x8783fe2`) |
| **Confidence** | CONFIRMED (decompile + mangled-symbol anchored) unless a row or callout says otherwise |

---

## RankAndPermute — The Per-Digit Rank+Permute Pass

### Purpose

`RankAndPermuteComputeFunction` is the compute callback for one chunk of one radix digit pass. Given a chunk of sorted keys and the values that travel with them, it (1) extracts the digit of each key, (2) deduplicates on `(key, digit)`, (3) computes the rank — the prefix position — of each unique value, and (4) emits the permutation that re-orders the original values into that rank order, storing it as an index vector. Repeated across all digits, the per-chunk permutations compose into the full sorted+deduplicated ordering that the [embedding gather](stream-gather-scatter.md) and [sample combiner](sample-combiner-emitter.md) consume.

### Signature and ABI

The const member function is, from the demangled symbol (`0x134039c0`):

```c
// RadixSortEmitterInternal::RankAndPermuteComputeFunction(...) const
StatusOr<SmallVector<Value,6>>
RankAndPermuteComputeFunction(
    Target const&        target,           // rsi → [rbp-0x88]
    OpBuilder            builder,          // by value [rbp+0x10] = r12
    int                  chunk_start,      // edx → [rbp-0x44]  (per-chunk loop var)
    Value                key,              // rcx → [rbp-0xc0]  (the to-be-digitised key)
    Value                v5,               // r8  → [rbp-0x108] (threaded to SliceBuffer)
    KeyValueRangeData const& range,        // r9               (drives GetMappedKeys/GetDigits)
    Value                sorted_key,       // [rbp+0x68]  — a19: the original sorted key column
    ArrayRef<Value>      values_in,        // [rbp+0x38]/+0x40 (gather-index prologue)
    ArrayRef<Value>      values_out,       // [rbp+0x48..]     (the VectorStoreIdx loop)
    Value, Value, Value,                   // alias-scope / loc helpers
    AliasScopeAssignment* assignment,      // a20
    AliasScope*          scope) const;
```

> **QUIRK —** the `this` pointer (`rdi → [rbp-0x30]`) is the `RadixSortEmitterInternal`, and the radix parameters live in *its* members, not in `KeyValueRangeData`: `+0x28` = num-buckets exponent (the digit `mask`), `+0x34` = the digit `shift`, `+0x38` = `num_keys` per chunk (the loop bound). `KeyValueRangeData const&` (the `r9` arg) carries the key/value range bounds that scale the bucket mapping; its field layout was not decoded (LOW).

### Algorithm

```c
function RankAndPermuteComputeFunction(...):              // sub_134039c0
    // --- PROLOGUE: 2 SmallVector<u32> (sized [this+0x38]=num_keys) + a gather-index chain
    grow_pod+memset ×2                                    // ~line 0x13403ab1
    for v in values_in: AddIOp(base, stride)              // per-value offsets into values buffer

    // --- KEY DERIVATION: digit = (key >> shift) & mask ----------------
    et   = operand(0).Shape::element_type()               // 0x134040ad / b6 — key element type
    mk   = GetMappedKeysForRadixSort(et, …)               // 0x133fe8c0 — bucket-map the key
    digit = GetDigits([this+0x28], [this+0x34],            // 0x133fe480 → [rbp-0xa8]
                      key, mk)                             //   the two i32 scalars are the only
    //   ↑ decompile line 493: GetDigits(this, *(u32*)(this+0x28), *(u32*)(this+0x34), key, mk)
    //   GetDigits body: ConstantIntOp + MulIOp + BroadcastScalarToVector + ShRUIOp + AndIOp ( + opt SubIOp )
    vecTy = VectorType::get(getI32Type(), …)              // 0x1340411f/35 → [rbp-0xa0]

    // --- DEDUP: select Unique vs UniqueWithLaneIds by a capability query
    if  target.caps[+0x88]()                               // 0x13404... line 497
        and target.SupportsSparseCore()                    //   line 499 (vtable slot 76)
        and target.caps[+0x80]() :                         //   line 501 → with-lane-ids path
        u = UniqueWithLaneIdsOp::create(builder, loc,       // 0x146231a0
                                        sorted_key, digit)  //   (a19, digit) — SAME two operands
        unique_vals = u.getNextResultAtOffset(0)            // result0 → [rbp-0x98]
        unique_idx  = u.getNextResultAtOffset(1)            // result1 → rbx
        lane_id     = u.getNextResultAtOffset(2)            // result2 → [rbp-0x90] (multiplicity)
    else:
        u = UniqueOp::create(builder, loc, sorted_key, digit) // 0x14622400, op "sc_tpu.unique"
        unique_vals = u.getNextResultAtOffset(0)            // result0 → [rbp-0x98]
        unique_idx  = u.getNextResultAtOffset(1)            // result1 → rbx
        lane_id     = NULL                                  // [rbp-0x90] = 0 → no PermuteOp

    // --- RANK: indexed scatter-add over a sliced values window --------
    BroadcastI32ToVector + SubIOp                          // 0x134043aa / 1340444b — rank base
    win = SliceBuffer(values, …)                           // 0x13404c00 — memref::SubViewOp window
    rank = VectorLoadStoreIdxAddOp::create(builder, loc,    // 0x14634fc0 → [rbp-0xb8]
              vecTy, unique_idx, win, unique_vals, {digit}) //   "sc_tpu.vector_load_store_idx_add"
    assignment.AddToNewScope(rank.getDefiningOp())          // CHECK: "base_offsets" / "input_chunk"

    // --- PERMUTE: re-order the values by rank × multiplicity ----------
    if lane_id != NULL:
        rank = PermuteOp::create(builder, loc, vecTy,        // 0x145f3920, op "sc_tpu.permute"
                                 rank, lane_id)              //   (rank vector, lane-id weight)

    // --- STORE: write the permutation index vector per element --------
    for i in 0 .. [this+0x38]:                              // cmp [rbp-0x44] < num_keys
        s = VectorStoreIdxOp::create(builder, loc, /*bool*/0, // 0x14638460
              permute_result, dest_slot, /*key=*/sorted_key,  //   "sc_tpu.vector_store_idx"
              {gather_index})                                 //   keyed by the sorted key (a19)
        assignment.AddToScope(s, *scope)                     // CHECK: "output_scatter"
    return ok                                                // eax=1; free the SmallVectors
```

> **NOTE — `Unique` operand order is `(sorted_key, digit)`.** Both `Unique` create calls take operands in the order `create(builder, loc, sorted_key, digit)` — the original sorted-key Value first, the `GetDigits` radix digit second (decompile `0x134039c0` lines 529 / 569: `UniqueWithLaneIdsOp::create(&a8, loc, a19, Digits)` and `UniqueOp::create(&a8, loc, a19, Digits)`). The dedup is keyed on the `(sorted_key, digit)` pair. The same `sorted_key` Value (`a19`) is reused as the keying operand of the final `VectorStoreIdxOp` (line 878).

### The Unique SSA Shape

The two dedup primitives share the exact same two operands and differ only in result count. `getNextResultAtOffset(base, n)` (`0x1d8e9700`) returns the op's `n`-th result; the decompile reads offsets `0`, `1`, `2` — i.e. the first three results `result0`/`result1`/`result2`, contiguous indices, *not* every-other-result.

| Op | `::create` | Results | Extracted (this fn) | Lowers to |
|---|---|---:|---|---|
| `UniqueOp` | `0x14622400` | 3 | `result0` = unique values, `result1` = unique index/marker | `tpu_uniquei`/`tpu_uniquef` (3-field LLVM struct) |
| `UniqueWithLaneIdsOp` | `0x146231a0` | 5 | `result0` = unique values, `result1` = index, `result2` = lane-id / multiplicity | same 3-field struct + 2 `ReplaceOpWithExtracts` |

Both lower through the shared `DuplicateCountUniqueOpLowering<T>` template (`UniqueOp` at `0x1359b280`, `UniqueWithLaneIdsOp` at `0x1359bd20`), which builds a 3-field literal struct via `LLVMStructType::getLiteral(ctx, types, 3, 0)` (decompile `0x1359b280` line 46; the `0x600000003` word is the size-3/capacity-6 SmallVector header of the field-type list). The lane-id form does two extra extracts for its two added results. The per-field meaning of the struct (unique-values vs write-mask vs segment-marker) is inferred from the extract order, not from a field-name table (LOW). The `result2` lane-id is the per-unique *multiplicity* — the count of original ids that collapsed into each unique entry — and it is the third operand (the second Value) of `PermuteOp`.

> **GOTCHA —** the selection between `UniqueOp` (3 results) and `UniqueWithLaneIdsOp` (5 results) is a target capability/flag query (`builder.target.caps`, decompile line 497), not a config field with a named accessor (LOW). In the plain `UniqueOp` path, `lane_id` is set to `0` (line 573) and the `PermuteOp` step is skipped entirely — the rank from `VectorLoadStoreIdxAddOp` is stored directly. A reimplementation that always emits `PermuteOp` will crash on a null operand in the no-lane-ids path.

### The Rank and the Inverse Permutation

The rank build is the heart of the pass and the place a reimplementer most often gets the data flow wrong.

```c
// VectorLoadStoreIdxAddOp::create(builder, loc, vecTy, unique_idx, win, unique_vals, {digit})
//   0x1340456c — decompile: create(&a8, loc, v192, v83, v104, NextResultAtOffset, {Digits})
//     vecTy        = i32 VectorType            [rbp-0xa0]
//     unique_idx   = Unique result1  (v83)     — where each id writes
//     win          = SliceBuffer SubViewOp     — the values gather window
//     unique_vals  = Unique result0            — the values being ranked
//     {digit}      = ValueRange{digit}         — the ranked-by digit
//   ⇒ result [rbp-0xb8] = the RANK: prefix position of each unique id in the deduped window.
//   Registered as "base_offsets" in the alias scope (CHECK string @ line 685).
```

`VectorLoadStoreIdxAddOp` is an indexed scatter-add: it reads the sliced values window at the unique indices, adds, and produces the prefix position (rank) of each id. `PermuteOp(rank, lane_id)` then re-orders the values into rank order, with the lane-id multiplicity weighting how many slots each unique entry occupies. The result is the inverse permutation — for every original id, the slot of its deduplicated representative. `VectorStoreIdxOp` writes that vector out per element, keyed by the original `sorted_key`, into the destination buffer (`output_scatter`).

### Function Map

| Function | Address | Role |
|---|---|---|
| `RankAndPermuteComputeFunction` | `0x134039c0` | per-digit per-chunk rank+permute body |
| `RankAndPermute` (wrapper) | `0x13404dc0` | tiles the compute fn over chunks (two passes) |
| `GetDigits` | `0x133fe480` | `digit = (key>>shift)&mask` (`ShRUIOp`+`AndIOp`+…) |
| `GetMappedKeysForRadixSort` | `0x133fe8c0` | bucket-map the key before digit extract |
| `SliceBuffer` (anon ns) | `0x13404c00` | `memref::SubViewOp` window of the values buffer |
| `$_0` callback | `0x1341b7e0` | trampoline → `RankAndPermuteComputeFunction` (pass 1) |
| `$_1` callback | `0x1341b9e0` | companion `vector::BroadcastOp` twin (pass 2) |
| `DuplicateCountUniqueOpLowering<UniqueOp>` | `0x1359b280` | LLVM lowering → 3-field struct + extracts |

---

## The RankAndPermute Wrapper and the Radix-Sort Chain

### Purpose

`RankAndPermute` (`0x13404dc0`) drives the compute function across the id stream. It computes the chunk count, loads the sorted keys, and runs the per-chunk callback inside a `CreateChunkIteratorLoop`. It is invoked once per digit by `SingleDigitRadixSort`, which `RadixSortEmitter::EmitSort` (`0x131c5fa0`) calls per digit to realise the multi-digit LSD radix sort that the SparseCore `SortOp` lowers to.

### Entry Point

```text
RadixSortEmitter::EmitSort                         0x131c5fa0   the SortOp lowering target
  └─ SingleDigitRadixSort                          0x1340c580   one digit (call 0x1340c6cf)
  │   └─ RankAndPermute                            0x13404dc0   ── this wrapper
  └─ SingleDigitRadixSortWithSoftwareCoalescing    0x1340c740   one digit, coalesced tails (call 0x1340c954)
      └─ RankAndPermute                            0x13404dc0
```

### Algorithm

```c
function RankAndPermute(target, builder, …, range, values_in, values_out):  // sub_13404dc0
    assignment.NewScope()                           // 0x13404e72 (line 181)
    n_chunks = ceil(n / chunk_size)                 // RemUIOp 0x134050ab + SubIOp + DivUIOp 0x134052b4
    keys = lowering_util::LoadChunk(sorted_keys)    // 0x1340544c (line 439) — load the sorted keys

    // PASS 1 — the rank+permute itself
    CreateChunkIteratorLoop($_0)                    // 0x1340565d (line 513); $_0 0x1341b7e0
    assignment.Materialize()                        // 0x134056e1 (line 554) — emit the alias scopes

    // PASS 2 — the companion BroadcastOp pass
    assignment.NewScope()                           // line 602
    CreateChunkIteratorLoop($_1)                    // 0x13405a28 (line 662); $_1 0x1341b9e0
    assignment.Materialize()                        // line 703
    return
```

> **NOTE —** `$_0` and `$_1` are the two `std::function` trampolines for the two chunk-iterator passes. The mangled names confirm both are `RankAndPermute`'s lambda closures (`…RankAndPermute…$_0` / `…$_1` in `_functions.json`); `$_0` invokes `RankAndPermuteComputeFunction`, `$_1` is the `vector::BroadcastOp` companion. The wrapper opens a fresh `NewScope` per pass and `Materialize`s after each — the alias scoping the two passes share is the `input_chunk` / `base_offsets` / `output_scatter` triad the compute function registers.

---

## SparseMapRow — Sort-and-Reduce by Row

### Purpose

`SparseMapRow` (SparseCore op-type `0x4`, confirmed by `IsSparseMapRowHlo` `0x13d7efe0` testing `SparseCoreOperationTypeFromString(...) == 4`) is the dedup-and-collapse primitive the `reduce_duplicates` custom-call emits — `SparseDenseMatmulOpDecomposer::ReduceDuplicates` `0x136722e0` builds it alongside a `DynamicBoundedSlice` (op `0x11` = 17). Structurally it is a descending lexicographic sort of a row's `(token, sample)` keys followed by a scan: sorting brings duplicate token-ids adjacent, and the carried reduce-fn (e.g. `add`) collapses each adjacent run into one ELL row — the CSR→ELL row collapse that realises the duplicate multiplicity. See [Dedup Multiplicity](dedup-multiplicity.md) for the multiplicity-weighting side and [Scan Datapath](scan-datapath.md) for the `ScanOp` / `SegmentedScanOp` lowering.

### Algorithm — The Window Emitter

`FusionEmitter::SetOutputWindowBoundsForSparseMapRow` (`0x13890d40`) is the half of the lowering that sizes the per-row output window. It recognises the op, builds the per-row segment state, then emits a descending `SortOp` and a `"max"` scan:

```c
function SetOutputWindowBoundsForSparseMapRow(target, hlo, builder, state, …):  // sub_13890d40
    if not IsSparseMapRowHlo(GetRootInstruction(hlo)): return   // 0x13890d71 / d81
    row = operand(0)                                            // 0x13890da3 — the row input
    BroadcastBoolToVector / BroadcastI32ToVector + GetCurrentState  // per-row segment/window state
    bound = ConstantIndexOp + AddIOp + AddIOp                  // window-bound arithmetic

    dir = StringAttr::get(builder.ctx, "dscd")                 // 0x1389133d; "dscd" @0x8720761
    s   = SortOp::create(builder, loc, Ty, Ty, Ty,             // 0x1389136a — 3 result types,
                         row, key1, key2, dir)                 //   3 key Values, sort-dir "dscd"
    col = s.getNextResultAtOffset(1).getNextResultAtOffset(0)  // 0x138913a6 — sorted column
    ExtractVectorElement(col, 0)                               // pull the scalar window bound

    redop = getStringAttr(builder, "max")                      // 0x138914a4; "max" @0x84c6977
    sc    = ScanOp::create(builder, loc, Ty, data, segid, redop) // 0x138914bf — window-extent scan
    ExtractVectorElement(sc, …) ; IndexCastOp                  // 0x1389150d / a5 — finalise the bound
```

The decompile (`0x13890d40`) confirms the literal `"dscd"` at line 468 (with the Twine kind word `259` = `0x103`, a cstring-ptr+len), the `SortOp::create` with three result types (`v80, v81, v81`) and three key Values at line 474, and the `getStringAttr("max")` + `ScanOp::create` pair at lines 311/312.

> **GOTCHA —** the `"max"` scan here is the *window-extent* scan — it computes the per-row output window size, not the duplicate-value collapse. The duplicate-value reduce uses the `HloComputation` that the `ReduceDuplicates` `SparseMapRow` custom-call carries (e.g. `add`), inlined by a *separate* FusionEmitter value-reduce path. The two `ScanOp` creators are distinct; conflating them produces a row collapse that sums indices instead of values (the value-collapse emitter was not body-decoded here — MEDIUM).

### The `"dscd"` / `"ascd"` Sort Direction

The 4-character sort-direction StringAttr is byte-confirmed against the intrinsic split in `SortOpLowering::matchAndRewrite` (`0x13597700`), which reads both strings from the `.rodata` pool `"dscd\0ascd\0…"` (`0x8720761` / `0x8720766`) and selects one of three comparator lambdas. The direction × element-type product maps to four distinct intrinsics:

| Direction | StringAttr `.rodata` | Element type | Intrinsic `::create` | Meaning |
|---|---|---|---|---|
| `"ascd"` | `0x8720766` | integer | `tpu_sort_ascdi` `0x14739520` | ASCENDING int keys |
| `"ascd"` | `0x8720766` | float | `tpu_sort_ascdf` `0x14738d00` | ASCENDING float keys |
| `"dscd"` | `0x8720761` | integer | `tpu_sort_dscdi` `0x1473a560` | DESCENDING int keys |
| `"dscd"` | `0x8720761` | float | `tpu_sort_dscdf` `0x14739d40` | DESCENDING float keys |

`SparseMapRow` uses `"dscd"` (descending), so duplicate token-ids become adjacent for the carried reduce-fn to collapse. The dialect `SortOp` op name is `"sc_tpu.sort"` (`0x84de6a5`); its bundle-level opcodes and the `EmitVectorSort` emitter are owned by the [VectorExtended](vectorextended-vex.md#the-embedding-reduce-op-roster) page and are not repeated here.

### SparseMapRow Op-Create Sequence

| # | Op / call | Address | Role |
|---|---|---|---|
| 0 | `IsSparseMapRowHlo` (`0x13d7efe0`) + `GetRootInstruction` | `0x13890d71`/`d81` | recognise the op (`...FromString == 4`) |
| 1 | `operand(0)` | `0x13890da3` | the row input |
| 2 | `BroadcastBoolToVector` / `…I32…` + `GetCurrentState` | `0x13890ede`/`ef4` | per-row segment state |
| 3 | `ConstantIndexOp` + `AddIOp` ×2 | `0x13891136`+ | window-bound arithmetic |
| 4 | `StringAttr::get("dscd")` | `0x1389133d` | sort-dir attr (`0x8720761`) |
| 5 | `SortOp::create(Ty,Ty,Ty, V,V,V, "dscd")` | `0x1389136a` | 3 result tys, 3 keys |
| 6 | `getNextResultAtOffset` ×2 + `ExtractVectorElement` | `0x138913a6`+ | extract sorted column + scalar |
| 7 | `getStringAttr("max")` | `0x138914a4` | reduction_op (`0x84c6977`) |
| 8 | `ScanOp::create(Type, data, segid, "max")` | `0x138914bf` | window-extent scan |
| 9 | `ExtractVectorElement` + `IndexCastOp` | `0x1389150d`/`a5` | finalise the bound |

---

## How the Radix Digit Orders the Indices

The ordering is a multi-digit LSD radix sort lowered to MLIR ops. The full radix-sort emitter (`RadixSortEmitterInternal`) is the count/scan/scatter machinery whose rank pass is documented above; the count and bucket-scan halves — `HistogramKeysComputeFunction` `0x133feca0`, `ScanBucketsComputeFunction` `0x13400120`, `CalculateGatherIndices` `0x13400400` — are decoded in [The Histogram and Bucket-Scan Halves](#the-histogram-and-bucket-scan-halves) below. What this page pins for the rank+permute that turns a digit histogram into the per-digit permutation:

1. **Digit extraction.** `GetMappedKeysForRadixSort` (`0x133fe8c0`) bucket-maps the sorted key; `GetDigits` (`0x133fe480`) computes `digit = (mapped_key >> shift) & mask` with `ShRUIOp` + `AndIOp` + `ConstantIntOp` + `MulIOp` + `BroadcastI32ToVector`. The `shift` is `RadixSortEmitterInternal+0x34` and the `mask` follows from the num-buckets exponent at `+0x28`.
2. **Dedup on `(sorted_key, digit)`.** `UniqueOp`/`UniqueWithLaneIdsOp` collapse equal `(key, digit)` pairs, yielding the unique values, a unique index/marker, and (with lane ids) the per-unique multiplicity.
3. **Rank.** `VectorLoadStoreIdxAddOp` scatter-adds each unique value into its bucket position to produce the prefix-position rank — the deduped slot index of each id (the `base_offsets`).
4. **Permute.** `PermuteOp(rank, lane_id)` re-orders the values into rank order, multiplicity-weighted; `VectorStoreIdxOp` writes the permutation index vector keyed by the original key.

Composing the per-digit permutations across all digits yields the globally sorted+deduplicated id ordering. Because each digit pass deduplicates, the gather window after the final pass is the unique-id count, not the raw id count — the redundant-gather elimination the [stream gather/scatter](stream-gather-scatter.md) path relies on.

> **NOTE —** the digit `mask`/`shift` and the bucket count are driven jointly by the `RadixSortEmitterInternal` members (`+0x28`/`+0x34`/`+0x38`) and the `KeyValueRangeData` arg. The member offsets feeding the digit math are CONFIRMED; the `KeyValueRangeData` field map (key/value range bounds) is not decoded (LOW), so the exact bits-per-pass parameterisation is inferred from the member usage.

---

## The Histogram and Bucket-Scan Halves

The rank+permute above is the *third* phase of each digit pass. It is fed by two earlier phases — **histogram** (count keys per bucket) and **bucket-scan** (exclusive prefix-sum of the bucket counts) — plus a final **gather-index** arithmetic that turns a scanned bucket base into a per-element scatter offset. All three are per-chunk compute functions on `RadixSortEmitterInternal`, with bodies decoded here (CONFIRMED, decompile-anchored to `radix_sort_emitter_internal.cc` source lines).

### Histogram — `HistogramKeysComputeFunction` `0x133feca0`

The histogram phase counts, per digit value, how many keys map to each bucket. Per chunk it loads a masked window of keys, derives the digit (the same `GetMappedKeysForRadixSort` + `GetDigits` pair the rank pass uses), deduplicates on the `(key, digit)` pair, then scatters the result into the histogram buffer with a `VectorStoreIdxOp` — the same indexed-store primitive the rank pass uses for its output.

```c
function HistogramKeysComputeFunction(builder, …, key, …, range, target, opt_mask):  // sub_133feca0
    chunk = lowering_util::LoadChunkMasked(key, …)        // masked chunk load (CHECK status line 244)
    assignment.AddToNewScope(chunk.getDefiningOp())       // → "input_chunk" alias scope (line 246)

    if  opt_mask.has_value():                             // a14 optional<int> upper bit set
        m   = BroadcastI32ToVector(opt_mask)              // 0x133fef.. — the mask bound
        cmp = arith::CmpIOp::create(builder, …)           // line 253 — lane-active predicate
        v   = arith::AndIOp::create(builder, loc, chunk, cmp)  // line 255 — gate inactive lanes

    mk    = GetMappedKeysForRadixSort(et, chunk, …)       // 0x133fe8c0 — bucket-map the key
    digit = GetDigits([this+0x28],[this+0x34], chunk, mk) // 0x133fe480 — (mapped>>shift)&mask (line 269)
    u     = UniqueOp::create(builder, loc, chunk, digit)  // 0x14622400 — dedup (key,digit) (line 272)
    uidx  = u.getNextResultAtOffset(1).getNextResultAtOffset(0)  // unique index (the bucket slot)
    uval  = u.getNextResultAtOffset(0)                    // unique value (the count contribution)
    s     = VectorStoreIdxOp::create(builder, loc, /*bool*/1,    // 0x14638460 — scatter into histogram
              uidx, ctx, uval, {digit})                   //   keyed by the digit bucket
    assignment.AddToNewScope(s)                           // → "scatter" alias scope (CHECK line 274)
```

> **NOTE —** the histogram path uses the *plain* `UniqueOp` (3 results), never the lane-ids form — the count phase needs only the unique value and its bucket index, not the multiplicity weight that the rank phase's `PermuteOp` consumes. The optional mask argument (`std::optional<int>`, the `a14` upper-bit test at decompile line 198) is the software-coalescing tail mask: it gates the partial final chunk so out-of-range lanes contribute nothing to the histogram. When absent, the full chunk is counted.

### Bucket-Scan — `ScanBucketsComputeFunction` `0x13400120`

The bucket-scan converts the per-bucket histogram counts into the *scatter base offset* of each bucket — a classic exclusive prefix-sum. It is a single `ScanOp` with the `"sum"` reduction, followed by an inclusive→exclusive fix-up (`AddIOp` − `SubIOp`).

```c
function ScanBucketsComputeFunction(builder, histogram, …):  // sub_13400120
    seg  = lowering_util::BroadcastBoolToVector(…)         // 0x13400... — all-active scan segment
    sum  = ScanOp::create(builder, loc, vecTy, seg,        // 0x138... ScanOp, op "sc_tpu.scan"
              histogram, StringAttr "sum")                 //   "sum" @ ptr (Twine kind 259); line 411
    last = BroadcastVectorElementToVector(sum, n-1)        // line 420 — broadcast the inclusive total
    add  = arith::AddIOp::create(builder, …)               // line 420 — inclusive prefix
    arith::SubIOp::create(builder, …)                      // line 424 — subtract own count ⇒ exclusive
    return add                                             // the exclusive bucket base offsets
```

> **NOTE — the bucket prefix-scan is a `sparse_core::ScanOp` with the `"sum"` reduction.** The reduction StringAttr is `"sum"` (decompile `0x13400120`: the literal `"sum"` is stored as `ptr[0]="sum"` with Twine kind `259`/`0x103` immediately before `mlir::StringAttr::get`, then passed to `ScanOp::create`). This `ScanOp` lowers through `ScanOpLowering::matchAndRewrite` (`0x1358ab00`) exactly as documented on the [Scan Datapath](scan-datapath.md) — the radix bucket-scan is one *user* of that lowering, not a separate scan primitive. The inclusive scan is converted to the exclusive base offset by the trailing `AddIOp`/`SubIOp` pair (source lines 420/424). The histogram-side prefix is a vector `ScanOp`, *not* the `tpu_mprefix` `i1`-count path (that path is for boolean inputs; the histogram is an `i32` count vector).

### Gather-Index — `CalculateGatherIndices` `0x13400400`

The final arithmetic turns the exclusive bucket base (a per-bucket scalar) into a per-element destination index. It builds a lane sequence, adds the scanned base, then splits the per-tile geometry with a modulo/divide:

```c
function CalculateGatherIndices(builder, a2, a3, base):  // sub_13400400
    seq = VlaneseqOp::create(builder, loc, idxVecTy)       // 0x145... "sc_tpu.vlaneseq" (line 438)
    s32 = arith::IndexCastOp(seq)                          // index→i32 (line 441)
    b   = vector::BroadcastOp(IndexCast(base))             // broadcast the scanned bucket base
    pos = arith::AddIOp(s32, b)                            // line 448 — global position = lane + base
    w   = vector::BroadcastOp(ConstantIntOp a2)            // tile-width constant (line 453)
    lo  = arith::RemUIOp(pos, w)                           // line 455 — within-tile offset
    hi  = arith::DivUIOp(pos, w)                           // line 459 — tile index
    s   = vector::BroadcastOp(ConstantIntOp a3)            // tile-stride constant (line 463)
    return arith::AddIOp(arith::MulIOp(hi, s), lo)         // line 463/464 — tile*stride + offset
```

> **NOTE —** the `RemUIOp`/`DivUIOp` split (lines 455/459) decomposes the linear scanned position into a `(tile_index, within_tile_offset)` pair, then recomposes it as `tile_index*stride + offset` (lines 463/464) so the scatter lands in the correct VMEM tile for the multi-tile scan. The two `arith::ConstantIntOp` immediates (`a2`, `a3` — tile width and tile stride) are passed in by the caller; their derivation from the `RadixSortEmitterInternal` members was not traced (LOW). `CalculateGatherIndices` is the bridge between the bucket-scan base and the rank pass's `VectorLoadStoreIdxAddOp` window.

### The Three-Phase Chain

| Phase | Compute fn | Key op(s) | Alias scope / output |
|---|---|---|---|
| 1 — Histogram | `HistogramKeysComputeFunction` `0x133feca0` | `LoadChunkMasked` → `UniqueOp` → `VectorStoreIdxOp` | `input_chunk` / `scatter` (per-bucket counts) |
| 2 — Bucket-scan | `ScanBucketsComputeFunction` `0x13400120` | `ScanOp("sum")` → `AddIOp`/`SubIOp` | exclusive bucket base offsets |
| 3a — Gather-index | `CalculateGatherIndices` `0x13400400` | `VlaneseqOp` + `RemUIOp`/`DivUIOp` + `MulIOp`/`AddIOp` | per-element scatter destination |
| 3b — Rank+permute | `RankAndPermuteComputeFunction` `0x134039c0` | `UniqueWithLaneIdsOp` → `VectorLoadStoreIdxAddOp` → `PermuteOp` | the per-digit permutation |

The wrappers that tile these compute functions are `HistogramKeys` `0x133ff3a0`, `ScanLocalTileHistograms` `0x13400c60`, `ScanHistogram` `0x13401000`, `ScanBuckets` `0x13401540`, and `MultiTileScanBuckets` `0x13402340` (the multi-VMEM-tile variant) — each driving its compute function through a `CreateChunkIteratorLoop` the same way [`RankAndPermute`](#the-rankandpermute-wrapper-and-the-radix-sort-chain) drives the rank pass.

---

## PermuteOp ISA Lowering

The `PermuteOp` that the rank pass emits (`0x145f3920`, op name `sc_tpu.permute`) is itself lowered to an SC TEC VectorAlu instruction. The lowering is a thin one-pattern conversion; the heavy lifting is in the bundle encoder.

### `PermuteOpLowering::matchAndRewrite` `0x135a1640`

```c
char PermuteOpLowering::matchAndRewrite(PermuteOp op, PermuteOpAdaptor a, ConversionPatternRewriter& r):
    rty = a.getOperand(0).getType()                  // result type = type of operand[0]
    p   = mlir::sparse_core::tpu_sc_permute::create(  // 0x14735ac0 — the lowered op
            r, loc, TypeRange{rty}, ValueRange{op.operands})  // both PermuteOp operands flow through
    r.replaceOp(op, p)                                // (*vtable+8)(rewriter, op, p)
    return 1                                           // always matches
```

The decompile (`0x135a1640`) is just that: derive the result type from operand[0] (`ValueRange::dereference_iterator(…, 0)` masked with `0xF8`), build a 1-element `TypeRange`, call `tpu_sc_permute::create`, and `replaceOp`. It is registered in the SparseCore→LLVM `RewritePatternSet` alongside `UniqueOp`/`DuplicateCountOp`/`SortOp` lowerings (the `RewritePatternSet::add<…PermuteOpLowering…>` template at `0x13572820`).

### The `tpu_sc_permute` Op and Its Masked Twin

`tpu_sc_permute::build` (`0x147359a0`) confirms the op shape: it `addOperands` the two incoming Values (line 12) and one result type (lines 33–45) — a 2-operand, 1-result op (`NOperands<2>`, `OneTypedResult<Type>`, `MemoryEffectOpInterface`). There is a masked twin, `tpu_sc_mask_permute`, with the identical 2-operand/1-result shape — the predicated permute that gates inactive output lanes (the `tpu_sc_mask_permute` op name appears 37× in `.rodata` vs 39× for `tpu_sc_permute`).

| MLIR op | Op name | Operands | Result |
|---|---|---:|---|
| `mlir::sparse_core::PermuteOp` | `sc_tpu.permute` | 2 (data, index) | 1 (= type of operand[0]) |
| `tpu_sc_permute` (lowered) | `tpu_sc_permute` | 2 | 1 |
| `tpu_sc_mask_permute` (predicated) | `tpu_sc_mask_permute` | 2 | 1 |

### The TEC VectorAlu Permute Opcode

`tpu_sc_permute` is realised by the ISA emitter as a TEC VectorAlu `VectorPermute` instruction, dispatched per element width. The opcode lives in bits `13..20` of the VectorAlu op word (mask `0x1FE000`), byte-confirmed from the `…Opcode::Matches` predicates:

| ISA op | `Matches` raw | Opcode field `(word>>13)&0xFF` | Element width | Address |
|---|---|---:|---|---|
| `SparseCoreTecVectorAlu0VectorPermuteB32` | `(word & 0x1FE000) == 0x10A000` | `0x85` | 32-bit | `0x1ea9f620` |
| `SparseCoreTecVectorAlu0VectorPermuteB16` | `== 0x10C000` (`1097728`) | `0x86` | 16-bit | `0x1ea9f640` |
| `SparseCoreTecVectorAlu0VectorPermuteB8`  | `== 0x10E000` (`1105920`) | `0x87` | 8-bit | `0x1ea9f660` |

The radix-sort permute operates on `i32` indices, so it uses **`VectorPermuteB32`** (opcode field `0x85`). The bundle emit for it is the `EmitVectorBinop<…SparseCoreTecVectorAlu_VectorPermuteB32…>` / `EmitVectorY<…>` template family (gxc/glc `0x13a19ae0`/`0x13a21ec0`, gxc/gfc `0x13aae200`/`0x13aae340`), which routes the two operands through the `SparsecoreVregReadPort` set and writes the destination VREG. The viperfish (v5) generation has its own `SparseCoreTecVectorAlu_VectorPermute` emitter (`0x139ad760`) — the `B8`/`B16`/`B32` width split is a gxc-era (v6e/v7x) refinement.

> **NOTE —** the permute is a cross-lane gather *within a VREG*: operand[0] is the data vector (the ranked values), operand[1] is the per-lane source-index vector (the rank). `VectorPermuteB32` reads each lane's index, fetches that source lane's element, and writes it to the destination lane — i.e. `dst[i] = src[index[i]]`. In the radix pass this realises the inverse permutation: each original id reads from the rank slot of its deduplicated representative. The `tpu_sc_mask_permute` twin adds an M-register predicate so inactive output lanes are left untouched, matching the masked-scan datapath on [Scan Datapath](scan-datapath.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `RadixSortEmitter::EmitSort` `0x131c5fa0` | the `SortOp` lowering entry; calls `SingleDigitRadixSort` per digit |
| `HistogramKeysComputeFunction` `0x133feca0` | the count half of the radix sort ([decoded above](#histogram--histogramkeyscomputefunction-0x133feca0)) |
| `ScanBucketsComputeFunction` `0x13400120` | the bucket prefix-sum (`ScanOp("sum")`) that feeds the rank pass ([decoded above](#bucket-scan--scanbucketscomputefunction-0x13400120)) |
| `CalculateGatherIndices` `0x13400400` | turns the scanned bucket base into a per-element scatter index ([decoded above](#gather-index--calculategatherindices-0x13400400)) |
| `PermuteOpLowering::matchAndRewrite` `0x135a1640` | lowers `sc_tpu.permute` → `tpu_sc_permute` ([decoded above](#permuteoplowering-matchandrewrite-0x135a1640)) |
| `SparseCoreTecVectorAlu0VectorPermuteB32Opcode::Matches` `0x1ea9f620` | the TEC VectorAlu permute opcode (field `0x85`, mask `0x1FE000`) |
| `SparseDenseMatmulOpDecomposer::ReduceDuplicates` `0x136722e0` | the decomposer that emits the `reduce_duplicates` `SparseMapRow` + `DynamicBoundedSlice` (op `0x11`) custom-calls |

## Cross-References

- [VectorExtended (VEX)](vectorextended-vex.md) — owns the bundle-level `Sort`/`Uniquify`/`DuplicateCount` opcodes and the `EmitVectorSort` emitter these MLIR ops lower to
- [Scan Datapath](scan-datapath.md) — the `ScanOp` / `SegmentedScanOp` lowering used by the `SparseMapRow` window emitter
- [Dedup Multiplicity](dedup-multiplicity.md) — the lane-id / `DuplicateCount` multiplicity that weights `PermuteOp` and the ELL row collapse
- [Emit Valency Loop](emit-valency-loop.md) — the per-sample valency loop that consumes the deduped, gathered ids
- [Sample Combiner Emitter](sample-combiner-emitter.md) — the embedding combiner that drives the sort→dedup→gather→reduce datapath
- [Stream Gather / Scatter](stream-gather-scatter.md) — the gather over the deduped unique-id window the permutation produces
- [SparseCore Overview](overview.md) — where the radix-sort dedup sits in the embedding pipeline
