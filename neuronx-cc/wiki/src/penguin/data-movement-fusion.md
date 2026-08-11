# Penguin Data-Movement: Fusion + Copy-Elimination

> *All symbols, sizes, and strings on this page apply to neuronx-cc `2.24.5133.0+58f8de22` (cp310 wheel). The cp311/cp312 wheels are byte-equivalent in symbol content. Evidence is recovered from the Cython-compiled extension modules under `neuronxcc/starfish/penguin/{transforms,ir}/*.cpython-310-x86_64-linux-gnu.so` — Cython retains fully-qualified `Class.method` names, module/method docstrings, per-literal string constants, and statistics-counter phrases in `.rodata`, but **not** the `.py` line text. So every algorithm below is reconstructed from the *set* of recovered method names + identifier literals + docstrings + KPI counters; predicate bodies are inferred from those, never read line-for-line.*

## Abstract

The Penguin middle-end removes logical copies and shape-shuffles **by value-level fusion before allocation** — it rewrites producer stores, consumer loads, and affine load addresses so that a copy, concat, slice, transpose, reshape, or cast simply never materializes. This is the Penguin analogue of LLVM copy-coalescing and producer/consumer loop fusion, but expressed over affine loop-nests and offloaded data-movement primitives rather than SSA virtual registers. Three other layers attack the same logical copy at other altitudes — the MLIR `NeuronOpFusion` op-clusterer runs *before* Penguin primitives exist, the Tritium autotuner *decides which* fusions to attempt, and the Walrus backend `tensor_copy_elim` coalesces *physical slots after* allocation. This page is the Penguin tier in the middle of that stack: where the chosen data-movement fusions are actually **applied**.

There are two distinct copy-elimination machines. **`MemcpyElimination`** (1.63 MB) is a polyhedral pass: it builds an ISL access map from a producing store-nest to a consuming load-nest and folds the offloaded `memcpy` away by rewriting the consumer's load address into the producer's index space — inserting a zero-cost reinterpret-cast (or a real cast on dtype mismatch) when layouts differ, and respecting schedule legality across barriers. The second machine is the **geometric `{Cast,Concat,Slice,Transpose,Reshape}OpElimination` family**: five ~318 KB thin drivers that each delegate to a corresponding `*OpOptimizer` class inside the 5.46 MB `TensorOpUtils` engine, removing an offloaded shape-op by `fuse_src`/`fuse_dst` (rewrite every consumer load or every producer store to target the opposite tensor) at kernel boundaries. Surrounding them is the operator-fusion family — the `FusionOp` IR container, the `LoopFusion` affine load↔store engine, experimental `OperatorFusion` mega-op clustering, and target-level `CCOpFusion` — and the two-stage lowering (`LowerTensorOp` lowers compute ops but deliberately leaves the offloaded mem primitives for copy-elim to fold; `LateLowerTensorOp` materializes whatever survives).

> **NOTE —** the numeric cast-elimination (the `OffloadedMemCast` dtype-conversion folding *as a numeric transform*, precision rules, canonicalization to a single representative dtype) is documented in **[Numerics 9.7](../numerics/cast-elimination.md)**. This page covers only the *geometric/structural* side of cast handling — how a memcast tensor is fused into a neighbor loop-nest to make the standalone op vanish, not what dtype conversion it performs.

For reimplementation, the contract is:

- **The offloaded-primitive IR** (`OffloadedMemCpy/MemCast/Bitcast/Concat/Slice/Transpose/Broadcast/...`, base `OffloadedMemIntr`) and the `verify()` invariants each carries — these are the legality envelope every fold must preserve.
- **`MemcpyElimination`'s polyhedral fold**: the `can_fold_memcpy` legality predicate (must-alias + whole-tensor-cover + single-read + barrier-respecting schedule), the trivial fast path, the collective-dst special case, and the affine address-rewrite (`rewriteLoadAddress` / `newaddrs` / `DelinearIndices`).
- **The `fuse_src`/`fuse_dst` shape**: how every `*OpOptimizer` removes its op by redirecting one tensor's defs/uses, the per-op addr-range / sub-tensor-access rewrites, and the kernel-boundary single-use IO rule.
- **The fusion engines** (`FusionOp` container shape, `LoopFusion`'s named op-pair patterns and its full rejection-reason set, `OperatorFusion`, `CCOpFusion`) and the **two-stage lowering** that materializes the survivors.

| | |
|---|---|
| **IR level** | Penguin IR — affine loop-nests + offloaded data-movement primitives, **pre-allocation** |
| **Polyhedral copy-elim** | `MemcpyElimination.cpython-310-….so` (1,630,208 B) — ISL/`islpy` access maps |
| **Geometric fold engine** | `TensorOpUtils.cpython-310-….so` (5,455,456 B) — the `*OpOptimizer` classes |
| **Thin op-elim drivers** | `{Cast,Concat,Slice,Transpose}OpElimination` (~318 KB ea) + `ReshapeOpElimination` (307,680 B) |
| **Fusion container** | `ir/FusionOp.cpython-310-….so` (2,043,232 B) — `ElementWise`/`Reduce`/`TensorContract` subclasses |
| **Loop fusion engine** | `LoopFusion.cpython-310-….so` (4,622,360 B) — affine load↔store fusion |
| **Lowering (compute / mem)** | `LowerTensorOp` (10,250,552 B) / `LateLowerTensorOp` (1,817,200 B) |
| **Pipeline placement** | `codegen_prepare` (copy-elim) then `codegen_optimization` (loop-fusion, late-lower) |

---

## The Offloaded-Primitive IR

### Purpose

Penguin represents every data-movement operation as an *offloaded* intrinsic before deciding how to realize it. These are the operands the copy-elimination family folds and the lowering family materializes. The set is recovered verbatim from `ir/Intrinsics.cpython-310-….so` as `__builtin_offloaded_*` strings plus the matching IR class names.

### Encoding

| `__builtin` name | IR class | Category | Key `verify()` invariant |
|---|---|---|---|
| `__builtin_offloaded_memcpy` | `OffloadedMemCpy` | pure copy / relabel | rhs_str `dst[0] = memcpy(srcs[0])`; same dtype + same `#elements` **[INFERRED]** — op semantics, not a binary literal |
| `__builtin_offloaded_memcast` | `OffloadedMemCast` | dtype convert | rhs_str `dst[0] = cast(srcs[0])`; different dtype + same `#elements` **[INFERRED]** |
| `__builtin_offloaded_bitcast` | `OffloadedBitcast` | reinterpret, no data move | — |
| `__builtin_offloaded_concat` | `OffloadedConcat` | N-source join along one dim | `every src tensor for concat must be FullTensorAccess`; `dst tensor for concat must be FullTensorAccess`; `Invalid src shape for concat: must only differ along concat dimension` |
| `__builtin_offloaded_slice` | `OffloadedSlice` | sub-tensor extract | `dst for OffloadedSlice must be of type FullTensorAccess`; `src … must be of type SubTensorAccess` |
| `__builtin_offloaded_transpose` | `OffloadedTranspose` | permutation (reshape+transpose+reshape) | rhs_str `dst = memcpy(src, src_shape, permutation)`; `input_dimensions[permutation[i]] = output_dimensions[i]` |
| `__builtin_offloaded_broadcast` | `OffloadedBroadcast` | replicate along `broadcast_dims` | `squeeze()` drops size-1 dims |
| `__builtin_offloaded_memset` | `OffloadedMemSet` | fill | — |
| `__builtin_offloaded_fma` | `OffloadedFMA` | fused multiply-add (tiled-capable) | `scales`, `is_tiled`, `num_tiles` |
| `__builtin_offloaded_rng` | `OffloadedRNG` | random | — |

`OffloadedMemIntr` is the shared base for the memory-movement ops (it carries `.is`/`.verify`), with `OffloadedMemCpy` and `OffloadedMemCast` deriving from it, per the `Intrinsics` class symbols. The tiled variants of memcpy and FMA carry `is_tiled`/`num_tiles` and route to generated per-target instruction wrappers `TiledOffloadedMemCpyGen` / `TiledOffloadedFMAGen` (each exposing `canVectorize`/`canWriteToPsum`/`isBarrier`/`loadTensor`/`serialize`/`verify`) — so a large, non-foldable offloaded memcpy becomes a **tiled offloaded-memcpy instruction** rather than a loop-nest.

> **GOTCHA —** the memcpy-vs-memcast distinction is the *only* thing that separates a fold from a miscompile. `OffloadedMemCpy`'s rhs is `dst[0] = memcpy(srcs[0])` — same dtype, same element count, a **pure relabel**, always foldable if aliasing is legal. `OffloadedMemCast`'s rhs is `dst[0] = cast(srcs[0])` — a dtype conversion, **real compute**. "Eliminating" a memcast therefore never deletes it; it fuses the conversion into a neighbor's loop-nest (`_find_candidate_tensor_memcast_fusion`). A reimplementation that drops a memcast the way it drops a memcpy silently loses the dtype conversion.

> **NOTE — the memcpy/memcast dtype and element-count rules are semantics, not `verify()` strings.** `Intrinsics.so` carries no "dtype of src and dst … must be the same / must be DIFFERENT" or "#elements between the src and dst … must be the same" literal. What it does carry is the rhs-emission forms (`dst[0] = memcpy(srcs[0])`, `dst[0] = cast(srcs[0])`, `dst = memcpy(src, src_shape, permutation)`) plus the concat/slice `FullTensorAccess` and `must only differ along concat dimension` asserts. The dtype and element-count rules hold, but they are the ops' meaning rather than a checked message.

---

## MemcpyElimination — the Polyhedral Copy-Folder

### Purpose

The headline pass. Its module docstring is unambiguous:

```text
MemCpyElimination - A more advanced loop fusion to eliminate memcpy. This pass
applies ISL (i.e. polyhedral model).
```

It is **not** a peephole copy-dropper. It treats an offloaded memcpy as the boundary between a producing store loop-nest and a consuming load loop-nest, builds an ISL (`islpy` / `IslSimplifier`) access map relating the two index spaces, and — when legal — rewrites the consumer's loads to read directly from the producer's storage, then relocates the producer DAG to the use site so the copy disappears. This is "copy elimination by coalescing" done with affine access maps rather than value-numbering.

### Entry Point

```text
MemcpyElimination.afterStmtTransform          ── per-statement post-order hook
  ├─ tryEliminateTrivialAliasedCpy            ── cheap must-alias fast path (pre-ISL)
  ├─ fuse_with_collective_op_dst              ── CollectiveComputeOp special case
  └─ (general ISL path)
       ├─ calculateMemcpyMapping              ── build src→dst access map (ISL)
       ├─ can_fold_memcpy                     ── LEGALITY predicate
       ├─ transformAffineStore               ── rewrite producing AffineStore
       ├─ rewriteLoadAddress / newaddrs       ── redirect consumer load addrs to src
       │    └─ DelinearIndices                ── delinearize a flattened access
       └─ moveDAG                             ── relocate producer DAG to use site
```

### Algorithm

```c
// MemcpyElimination.afterStmtTransform — fired per offloaded memcpy, post-order
function eliminate_memcpy(memcpy /* src -> dst */):
    // --- 1. trivial fast path: src and dst MUST-alias outright -----------------
    if tryEliminateTrivialAliasedCpy(memcpy):       // whole-tensor identical access
        count("Number of memcopy eliminated"); return

    // --- 2. collective special case -------------------------------------------
    // a memcpy feeding/fed by a CollectiveComputeOp is fused into the
    // collective's destination so the collective writes the final buffer
    if fuse_with_collective_op_dst(memcpy):         // identifier: CollectiveComputeOp
        return

    // --- 3. general polyhedral fold -------------------------------------------
    amap = calculateMemcpyMapping(memcpy)           // ISL src->dst access map
    if not can_fold_memcpy(memcpy, amap):           // legality (see below)
        count("Number of failure in access folding"); return   // failed_access_folding

    // 3a. relabel vs cast: if layouts differ but bytes compatible, no real copy
    if layouts_compatible_no_dtype_change(memcpy):
        loadWithReinterpretCast(dst_users)          // reinterpret / reinterpret_inplace
        set_reinterpret_to_all_users(dst)
        count("Number of reinterpret cast inserted")
    else if dtype_mismatch(memcpy):
        insert_cast(...)                            // count("Number of cast inserted")

    // 3b. rewrite every consumer load addr into producer index space
    for user in dst_loads_not_load_different_tensor(memcpy):
        new = newaddrs()                            // quasi-affine builder
        for factor in producer_affine_factors:
            new.add_factor(factor)                  // compose affine factors
        if needs_delinearize and not _dont_delinearize_tensor(t):
            new = DelinearIndices(new)              // delinearize flattened access
        rewriteLoadAddress(user, new)
        count("Number of quasi affine expr generated during addr rewrite")

    transformAffineStore(memcpy.producing_store)    // retarget producer store
    moveDAG(producer_dag, use_site)                 // count("Number of simple dag moved")
    count("Number of memcopy eliminated")
    count("Number of bytes eliminated", memcpy.nbytes)
```

### The `can_fold_memcpy` Legality Predicate

Reconstructed from the identifier literals that drive the fold decision inside `MemcpyElimination`. Every identifier below is present in `.rodata`; the boolean *body* is compiled-opaque, so the shape of the conjunction is **[INFERRED]** from the naming rather than read line-for-line:

```c
// can_fold_memcpy(memcpy, amap) — all conditions must hold
function can_fold_memcpy(memcpy, amap):
    // (a) coverage: dst access COVERS the src tensor, OR access map composes cleanly
    if not (is_whole_tensor_copy(memcpy) && cover_src_tensor(memcpy)):
        if failed_access_folding(amap): return false
    // (b) single-read: folding must not duplicate work / break a multi-consumer
    if read_more_than_once(src) && would_materialize(src): return false
    // (c) expressibility: consumers must be affine-addressable
    for u in dst_users:
        if used_by_generic_access(u) || load_different_tensor(u): return false
        if not can_move_to_load(u): return false
    // (d) aliasing provable via the DAG alias map (buildAliasTensorMap / AliasType)
    if not (is_must_alias(src, dst) || must_alias(src, dst)): return false
    // aliased output stores are honored in order (min_aliased_output_store tracks it)
    respect(aliased_output_stores, min_aliased_output_store)
    // (e) schedule legality: consumer load must come after producer top stmt,
    //     and must not cross a barrier (moveDAG must preserve the schedule)
    if max_dst_load_schedule(dst) < schedule_of_top_stmt(producer): return false
    if crosses(barrier_schedule, producer, consumer): return false
    return true
```

> **QUIRK —** when the source and destination tensors have *compatible bytes but different layout*, the pass does not insert a copy — it inserts a **reinterpret-cast** (`loadWithReinterpretCast` / `reinterpret_inplace` / `set_reinterpret_to_all_users`) and counts it as `"Number of reinterpret cast inserted"`. The memcpy folds into a zero-cost relabel of the consumer's load. Only a *dtype* mismatch forces a real inserted cast (`"Number of cast inserted"`). A reimplementation that always materializes a cast loses this zero-cost path.

> **NOTE —** rematerialized tensors are deliberately **not** folded — the counter `"Number of remated tensors skipped"` records the skips. Folding a remat tensor would defeat the rematerialization that re-created it to save memory.

### Statistics Counters (the pass's KPIs)

These verbatim `.rodata` strings are the pass's whole accounting — and the accounting is **in bytes of copy removed**, which confirms the purpose is data-movement reduction, not generic DCE:

```text
Number of memcopy eliminated
Number of bytes eliminated
Number of bytes copied by memcpy            (the baseline it measures against)
Number of cast inserted
Number of reinterpret cast inserted
Number of quasi affine expr generated during addr rewrite
Number of failure in access folding         (legality-failure counter)
Number of simple dag moved                  (moveDAG successes)
Number of remated tensors skipped           (remat tensors are not folded)
```

### Considerations

This polyhedral pass is the complement of, not a duplicate of, the backend `tensor_copy_elim` (Walrus, runs twice at orders 34 & 77). `MemcpyElimination` works on Penguin loop-nests via ISL access maps **before** allocation and folds the copy into the consumer's affine load. The backend pass removes copies by physical-slot / AP memloc aliasing **after** allocation. Two complementary layers: polyhedral-fusion vs physical-slot-coalescing.

---

## The Geometric Op-Elimination Family

### Purpose

Five passes remove the offloaded *shape* ops — cast, concat, slice, transpose, and reshape-as-memcpy. Each is a near-empty thin driver (`__init__` + `afterStmtTransform` + one `transformOffloaded*` method) that delegates to a corresponding `*OpOptimizer` class living in the 5.46 MB `TensorOpUtils` engine. The single strongest structural clue in the tree is that the four `{Cast,Concat,Slice,Transpose}OpElimination` siblings are byte-near-identical in size (~318 KB) — they are template-parallel drivers over a shared optimizer base.

### Function Map

| Pass (.so size) | Driver method | Engine class | Docstring (verbatim) | Confidence |
|---|---|---|---|---|
| `CastOpElimination` (318,320) | `transformOffloadedMemCast` | `CastOpOptimizer` | "Transform pass to eliminate unnecessary offloaded memory casts" | CERTAIN |
| `ConcatOpElimination` (318,424) | `transformOffloadedMemConcat` | `ConcatOpOptimizer` | "…unnecessary offloaded memory Concats" | CERTAIN |
| `SliceOpElimination` (318,368) | `transformOffloadedMemSlice` | `SliceOpOptimizer` | "…unnecessary offloaded memory Slices" | CERTAIN |
| `TransposeOpElimination` (318,608) | `transformOffloadedTranspose` | `TransposeOpOptimizer` | "…unnecessary offloaded memory Transposes" | CERTAIN |
| `ReshapeOpElimination` (307,680) | `transformOffloadedMemCpy` | (reshape == memcpy fold) | — | CERTAIN |

> **QUIRK —** `ReshapeOpElimination`'s transform method is literally named `transformOffloadedMemCpy`, not `transformOffloadedReshape`. At the Penguin level a reshape is represented as an `OffloadedMemCpy` whose src and dst differ *only in shape* (same `#elements`, same dtype — the memcpy verify invariant). So "reshape elimination" is exactly "memcpy fold": there is no distinct reshape primitive to eliminate.

### The Common Fusion Shape — `fuse_src` vs `fuse_dst`

Every `*OpOptimizer.fuse` picks a direction to make the op vanish. The verbatim docstring (from `TensorOpUtils`):

```text
fuse_dst = True, implies eliminate memcast by eliminating dst tensor
fuse_src = True, implies eliminate memcast by eliminating src tensor
```

Generalized for an op `X = op(Y)`:

```c
// the universal fold direction in every *OpOptimizer
if fuse_dst:                  // eliminate the op by eliminating the DST tensor
    // all definitions of Y write directly to X; all loads of Y read X
    replace_store_dst(defs_of_Y, X)
    replace_op_dst_tensor_and_erase(op)
else if fuse_src:             // eliminate the op by eliminating the SRC tensor
    // all loads of X read Y directly
    replace_load_src(loads_of_X, Y)
    replace_op_src_tensor_and_erase(op)
```

The apply-side primitives — `replace_op_src_tensor_and_erase`, `replace_op_dst_tensor_and_erase`, `replace_load_src`, `replace_store_dst` — are all `TensorOpUtils` symbols. So copy-elimination here *is* producer/consumer fusion: the op is removed by rewriting every producer store of one tensor or every consumer load of the other to target the opposite tensor.

### Kernel-Boundary Legality

The core "is it safe to drop" rule — verbatim from `eliminate_offloaded_memcpy` / `match_memcpy_src_dst_shape_kernel_boundary`:

```text
Eliminate memcpy at kernel boundaries when:
  1. the only load of the dst tensor is a kernel input and no other stores to the dst tensor
  2. the only store to the src tensor is a kernel output and no other loads of the src tensor
```

Reinforced by the assertion strings `"Either src/dst must be IO"` and `"Dst tensor on memcpy with ID {} must be output"`. The copy must be between a kernel-IO tensor and an internal tensor, single-use on the IO side, so redirecting the IO tensor's single def/use is provably value-preserving.

### Per-Op Algorithms

#### Concat — `ConcatOpOptimizer`

A concat `A = concat(B, C)` is eliminated by mapping each consumer access of `A` back to whichever source owns that address range along the concat dim. The verbatim worked example from the binary:

```text
A = concat (B, C)
shape(A) = [2, 4, 2, 8, 3], shape(B) = [2, 4, 7, 3], shape(C) = [2, 4, 9, 3]
If A is accessed as A[x,y,1,m,n] where m has tripcount 8, the affineExpr for the
concat dim is (8 + m), addr_range [8,15] -> falls into C's addr space alone.
```

Legality: a consumer access must fall **entirely within one source's slab** along the concat dim (`"it access addr space of only a single consumer of concat"`). Its load is then rewritten to read that source directly via `replace_dst_load_with_src_from_concat` / `transform_dst_addrs_to_src_addrs_for_concat_fusion` / `get_src_idx_from_addr_range_for_concat_fusion`. Sub-fusions handle the loop-nest split (`_fuse_concat_dst_to_loopnests`, `_compute_dst_bases_for_concat_fusion`) and reshape cleanup (`_check_src_reshape_eligibility_for_concat_fusion` + `eliminate_io_reshapes`).

#### Slice — `SliceOpOptimizer`

A slice is a sub-tensor access (`Flattened-` or `NDimSubTensorAccess`). Two symmetric directions, each in two access flavors:

```c
// fuse_dst: producer writes the slab directly into the parent
_fuse_dst_into_src_flattened_subtensor_access
_fuse_dst_into_src_ndim_subtensor_access
// fuse_src: consumer reads the slab directly from the parent at the slice offset
_fuse_src_flattened_subtensor_access_into_dst_load_user
_fuse_src_ndim_subtensor_access_into_dst_load_user   // transform_src_addrs_for_ndim_slice
```

> **GOTCHA —** `_skip_matmul_loads_from_slice_fusion`: a slice feeding a **matmul** load is *not* fused. The systolic-array loader needs the materialized slice layout, so slice-fusion is blocked when the consumer is a contraction. The constraint string `"Only NDimSubTensorAccess follows the merged/split loopnest lowering pattern for OffloadedSlice op"` further restricts which access shape can be fused.

#### Transpose — `TransposeOpOptimizer`

Verbatim docstring:

```text
A transpose consists of a reshape + transpose + reshape. This function separates
out the reshapes into memcpy's such that the transpose no longer [has reshapes].
We only split out the reshape if it is a delinearized reshape. [...]
If we cannot fuse away the reshape, we prefer to not modify the transpose and
hand it off to D2DTranspose optimizations.
```

The two fold directions are `fuse_dst_into_src_offloaded_transpose` / `fuse_src_into_dst_offloaded_transpose` (with `_replace_{dst,src}_use_for_offloaded_transpose_fuse`); `splitOffloadedTranspose` peels the bracketing reshapes into memcpys so the core permutation can be fused or handed off. Two guards:

- **Identity:** `"offloadedTranspose with identity permutation must be lowered to a lower tensor op"` — an identity transpose is degraded to a plain memcpy (then memcpy-folded).
- **DRAM-to-DRAM preservation:** a transpose sandwiched between DRAM load/store is *deliberately not fused* — `mark_dram_to_dram_transpose_non_local` marks src/dst `non_local` so it flows to the `DramToDramTranspose` backend path instead.

#### Cast — `CastOpOptimizer`

`fuse()` + `_find_candidate_tensor_memcast_fusion` find a neighbor tensor op into whose loop-nest the dtype conversion can be absorbed, eliminating the standalone memcast tensor. As noted, the memcast is *fused*, never deleted — see [Numerics 9.7](../numerics/cast-elimination.md) for the numeric side.

### Shared Delinearization / Reshape Legality

Fusion frequently needs to delinearize/reshape a tensor so src & dst shapes line up (`match_memcpy_src_dst_shape`, `reshape_tensor`, `delinearize_and_tile_accesses`). The verbatim legality (from `can_delinearize_tensor`):

```text
We can delinearize a tensor anytime unless
  1. there exists a generic access on `tensor` on the dimension undergoing a reshape
  2. there exists a tensor op / opaque op usage of `tensor`
  3. the tensor does not have any affine accesses
  4. affine accesses on delinearized dims are not fully accessed
```

IO-reshape legality is gated through `can_fold_io_input_load_intrinsic` / `can_fold_io_output_store_intrinsic` / `can_reshape_input_tensor` / `can_reshape_output_tensor` / `is_compatible_io_reshape`, controlled by the option `"disable TensorOpTransform to perform reshape on IO tensors"`. An **anti-degradation guard** (`is_degraded_fusion`) declines a fusion that would produce a worse loop-nest, with the refusal string `"lower_op_to_merged_or_split_loopnest: refusing to lower memcpy using big 1d tensor (use_merge …)"`.

---

## Penguin Operator Fusion

There are three fusion engines at the Penguin level, each a distinct granularity, plus a target-level collective fuser.

### FusionOp — the Fused-Op IR Container

`ir/FusionOp.cpython-310-….so` (2,043,232 B) defines the IR node that *holds* a fused cluster. Verbatim textual form:

```text
FusionOp - dsts = fuse[fusion_local_tensors, embbedded_tensor_ops](srcs)
   <fusion_label> fusion {
     locals = [list of local tensors]
     [tensorop insts]
   }
```

A `FusionOp` wraps a cluster of `TensorOp` instructions into one op with `formal_inputs`/`formal_outputs`, `local_tensors` (intermediates that never escape) and the body insts. Three subclasses exist: `ElementWiseFusionOp` ("FusionOp with only elementwise tensorop insts"), `ReduceFusionOp` (terminal is a `reduce_inst`), and `TensorContractFusionOp` (built around a matmul `tc_inst`). Well-formedness invariants, from the verify strings:

```text
internal inst(s) must be TensorOp        cannot load from a dst tensor inside the fusion op
local inst must be tensor op             cannot store to a src tensor inside the fusion op
Function in TensorOp must have a single user
local tensor shapes are not compatible with op.shape
fusion op is empty
```

A `FusionOp` is therefore a **single-block, single-user SSA region**: locals are private; you may only read formal srcs and write formal dsts. `skip_lower_to_loopnest` lets a `FusionOp` pass through to codegen un-expanded — emitted as a fused kernel.

### LoopFusion — the Affine Load↔Store Engine

`LoopFusion.cpython-310-….so` (4,622,360 B) is the workhorse that fuses a producing store loop-nest with a consuming load loop-nest and, as a special case, eliminates pure copy loops:

```text
eliminate copy loops of the form:  $1 = load A ;  B = store $1
where load and store have the exact same access pattern          (fuseTrivialCopy)
```

Two fuse directions (`fuseLoadToStore` / `fuseStoreToLoad`, with `fuseImpl`, `tryFuseLoadStore`, `propagateCopy`, `simplify_fused_axis`). Candidate detection: `findFusionCandidateForLoad`, `isLoadFusionCandidate` / `isStoreFusionCandidate` / `isLoadStoreCompatibleFusionCandidate`, `has_unique_load_parent`. An instruction-count cap prevents mega-loops (verbatim): *"We do not want loop fusion to produce loops with thousands of instructions because then … subsequent passes have compile time problems due to long def-use chains within a loop."* The cap's numeric default is compiled-opaque.

**Named op-pair pattern fusions** (method names — the specific fusions applied):

| Pattern | Method | What fuses |
|---|---|---|
| matmul → softmax | `check_matmult_softmax_pattern` | attention |
| matmul → rmsnorm | `check_matmult_rmsnorm_pattern` | normalization after contraction |
| batch-norm gradient | `check_bn_grad` | BN-grad cluster |
| reduce chains | `check_reduces` / `has_interleave_reduce_axes` | reduce fusion |
| elementwise chains | `isFusionWithPseudoElementWiseOp` / `has_pseudo_elementwise_op` | pseudo-elementwise chains |

The legality is that the candidate load and the producer store must have **matching access maps**, the producer must be a perfect nested loop, single-def/single-use, with no broadcast that breaks a matmul free dim. The exact illegality conditions are recoverable as the pass's verbatim rejection-reason strings (this is the precise legality envelope, expressed as its complement):

```text
: Accesses do not match           : access not match
: bad producer consumer addr mapping   : complex access pattern
: complex loop mapping            : Definition not a perfect nested loop
: broadcast                       : broadcast load           : has broadcast on load
: broadcast becomes free dim in matmul
: Cannot fuse with pseudo_elementwise_op
: more than 1 stores              : too many loads
: single_def_single_use           : has_unique_load_parent
: loading constant or input (or undef)
: overlapping with                : padded access
: No candidate found              : unexpected parent
```

Guards: `cannot_fuse_with_inst`, `cannot_eliminate_store`, `has_overwritten_store`, `can_move_to_load` / `can_move_to_store`. Base class `LoopFusionBase` is shared (with `PartialLoopFusion`, in `targets/transforms/`).

> **NOTE —** the rejection-reason list *is* the reimplementation spec for `LoopFusion`'s legality. The boolean body of `isLoadStoreAccessMatchForFusion` is compiled-opaque, but a reimplementer can drive a fusion checker that emits exactly these reasons and reject on any of them: access-map mismatch, non-perfect producer nest, multi-store/multi-load, broadcast-into-matmul-free-dim, overlapping/padded accesses, loading a constant/input.

### OperatorFusion — Coarse Mega-Operator Fusion

`transforms/experimental/OperatorFusion.cpython-310-….so`. Verbatim docstring: *"OperatorFusion - Operator Fusion tries to fuse key operators like Matmult, Conv … into a mega operator"* ("OperatorFusion only supports single function modules"). It clusters anchor ops (`MegaMatmult` / `MegaOperator`) and **sinks** surrounding ops into the cluster: `create_mega_op`, `fuse_and_create_mega_op`, `outline_mega_ops` (`OutlineCloner`), `sink_insts_to_mega_op`, `sink_mem_ops`, `sink_single_def_single_use_from_input`, `hoist_slice_op`, `find_mem_ops`, `is_candidate_for_fusion`, `trivial_pre_schedule`. The CLI knob is `operator-fution-split-ratio` (sic — typo in the binary), described as *"ratio of opertors to be fused to previous fusion candidate in range of [0.0, 1.0], by default all operators goes to previous fusion candidate"* (`opertors` typo is verbatim). This experimental ("modular optimization") path is the Penguin-side coarse fusion that produces the `FusionOp` regions.

### CCOpFusion — Collective-Compute-Op Fusion

`targets/transforms/CCOpFusion.cpython-310-….so` fuses/localizes tensors around collective-compute ops ("CCop" in this binary's strings — e.g. `Delinearize CCop src and dst if the tensor is used in ccop`): `canLocalize`, `localizeCCOpTensor`, `localizeRankedCCOpTensors` / `localizeNonRankedCCOpTensors`, `localizeOffloadedFMA`, `CCOpFusionCandidateGenerator`, `delinearize_inst_ap`. It ties to `MemcpyElimination.fuse_with_collective_op_dst` so collectives get their data-movement neighbors folded in and read/write final buffers directly. Target-level, distinct from the graph-level fusion above.

> **NOTE —** the op-class identifier `CollectiveComputeOp` appears in `MemcpyElimination.so` (driving `fuse_with_collective_op_dst`), but `CCOpFusion.so` itself uses the abbreviated `CCop`/`ccop` spelling throughout — same logical op class, two naming conventions in two binaries.

### Which Op Pairs Fuse — Consolidated

| Fusion | Owner |
|---|---|
| load ↔ store with matching affine access | `LoopFusion` (generic) |
| trivial copy-loop (load then identical store) | `LoopFusion.fuseTrivialCopy` |
| matmul → softmax | `LoopFusion.check_matmult_softmax_pattern` |
| matmul → rmsnorm | `LoopFusion.check_matmult_rmsnorm_pattern` |
| batchnorm-grad cluster | `LoopFusion.check_bn_grad` |
| reduce chains / interleaved reduce axes | `LoopFusion.check_reduces` |
| elementwise (pseudo-elementwise) chains | `LoopFusion.*pseudo_elementwise*` |
| matmul/conv + neighbors → mega-operator | `OperatorFusion` (experimental) |
| collective + its copy/FMA neighbors | `CCOpFusion` / `MemcpyElimination.fuse_with_collective_op_dst` |
| concat/slice/transpose/cast/reshape into a neighbor | the geometric §2 folds (each is itself a fusion) |

---

## Two-Stage Lowering

### Stage A — LowerTensorOp (compute, excluding mem-instrs)

`LowerTensorOp.cpython-310-….so` (10,250,552 B — the biggest pass in the tree). Docstring (verbatim): *"LowerTensorOp - Lower tensor ops into loopnests, excluding OffloadedMemInstr"*. It lowers **compute** TensorOps (conv, matmul/`TensorContract`, reduce, softmax, batchnorm, gather/scatter, pad, iota, dynamic-slice/update-slice, resize, quantize-MX, ternary/binary/unary, fusion-op via `transformFusionOp`) into affine loop-nests, but **deliberately leaves the offloaded memory primitives un-lowered** so the copy-elimination family gets first crack at folding them away. It *does* lower the few offloaded ops that are really compute: `transformOffloadedBroadcast`, `transformOffloadedRmsNorm(Backward)`, `transformOffloadedDropoutV1` (+ `_materializeOffloadedRmsNorm` / `_lowerToRmsNormIntrinsic`). The emit helpers `emit_memcast_load` / `emit_transpose_load` / `emit_reshape_load` / `emit_broadcast_load` turn an offloaded shape op into a load-with-index-map when consumed.

> **QUIRK —** the lowering order is the whole point of the design. `LowerTensorOp` runs **first** but *skips* the offloaded mem primitives on purpose — they are left un-materialized so `MemcpyElimination` and the geometric folds can rewrite them out of existence. Only the survivors reach the second stage.

### Stage B — LateLowerTensorOp (the surviving mem-instrs)

`LateLowerTensorOp.cpython-310-….so` (1,817,200 B). Docstring (verbatim, note lowercase): *"LateLowerTensorOp - Lower eligible OffloadedMemInstr tensor ops into loopnests"*. Every offloaded memory primitive that copy-elimination could not fold gets materialized here: `lower_offloaded_concat_to_loopnest`, `lower_offloaded_transpose_to_loopnest`, `lower_offloaded_cast_op_to_loopnest`, `lower_special_offloaded_memcast_to_loopnest`, plus `transformOffloadedSlice/Concat/MemCast/MemSet/Bitcast/Transpose`; the nested class `LateLowerReshapeOp.transformOffloadedMemCpy` lowers a leftover reshape-as-memcpy. Transpose materialization is the delicate one (`get_offloaded_transpose_shape_bases`, `should_split_offloaded_transpose` / `split_offloaded_transpose`, `lower_transpose_with_common_reshape_basis_src_and_dst`), with verbatim constraints:

```text
OffloadedTranspose - src and dst must have same number of elements
Only IO buffer could transpose with same src/dst
Tranpose src and dst reshape must have common reshape basis at this point
```

### Related Knobs

| Knob (option string) | Gates | Attr |
|---|---|---|
| `--big-tensor-threshold-one-d=<number>` | "suppress fusion optimizations for large 1d tensors originating in offloaded transpose lowering" | `_big_tensor_threshold_one_d` |
| `--big-tensor-threshold-one-d-memcpy=<number>` | same idea, memcpy-specific | `_big_tensor_threshold_one_d_memcpy` |
| `--disable-degraded-fusion` | the `is_degraded_fusion`-gated "refusing to lower memcpy using big 1d tensor" path | `_disable_degraded_fusion` |
| `--disable-tensor-op-io-reshape` | "disable TensorOpTransform to perform reshape on IO tensors" | `_disable_tensor_op_io_reshape` |
| `--disable-non-compatible-tensor-op-io-reshape` | same, restricted to incompatible new shapes ("an experimental option used by modular optimization") | `_disable_non_compatible_tensor_op_io_reshape` |

The default numeric thresholds are baked into compiled code, not `.rodata`, so they are **not recoverable** from static analysis. The upstream `disable-emit-offloaded-{memcpy,memcast,concat,slice,transpose}` family lives in hlo2penguin emission (cross-ref the MLIR strand) — when emission is disabled for a primitive, the op never appears as `Offloaded*`, so the elimination and late-lowering for it have nothing to fold/materialize.

---

## The Rest of the Copy-Elim Family

### OptimizeAliasedCopyChain — IO-Alias Chain Coalescing

`OptimizeAliasedCopyChain.cpython-310-….so` (746,424 B), the newest pass (copyright 2024). Verbatim docstring:

```text
OptimizeAliasedCopyChain - find a chain of instructions of the form A = op(B, ...) where
we allow memory sharing between A and B. If the ends of this chain are two aliased IO tensors,
then the entire of chain of tensors can share memory
```

(Quoted verbatim, including the "entire **of** chain" wording in the binary.)

When an input is aliased to an output (e.g. a weight updated in place), the whole producer chain is made to share that buffer, removing all intermediate copies. Methods: `identify_copy_chain`, `is_memory_sharing_op_candidate`, `find_memory_share_candidate_tensor`, `optimize_copy_chain`, `replace_intermediate_tensors`, and `REINTERPRET_SUPPORTED_OPS` — a whitelist of ops through which memory-sharing/reinterpret is legal (including `ExternalNativeNkiKernel`).

### PadElimination

`PadElimination.cpython-310-….so` (598,848 B). Docstring: *"PadElimination - A simplification pass that can eliminate padding writes."* Methods `find_and_eliminate_pad`, `single_value_array`; counter `"Number of padding eliminated"`. Drops redundant pad stores (writing a known constant into a region never read, or re-padding an already-padded region).

### TensorOpTransform — Canonicalize + Driver

`TensorOpTransform.cpython-310-….so` (996,304 B) runs the `OpOptimizer`s and canonicalizes offloaded ops into the form the elimination passes expect: `_canonicalizeOffloaded{Concat,MemCast,Slice,Transpose}`, `_canonicalize{Flattened,NDim}SubTensorAccess`, `_linearizeOffloadedSlice`, `_splitAndTransformOffloadedMemCast`, `_replaceOpWithSubTensorOp`, `transformOffloaded{MemCpy,MemCast,Concat,Slice,Transpose}`. It also does its own dead-copy cleanup ("Delete dead offloadedMemCpy"). This is the pass the IO-reshape option gates.

### TensorOpSimplifier

`TensorOpSimplifier.cpython-310-….so` (1,705,768 B), sibling to `TensorOpTransform` in the `codegen_prepare` Simplifier cluster. Performs algebraic identities on tensor ops (e.g. canceling inverse shape ops) before fusion. Distinct from the `OpOptimizer`s in `TensorOpUtils`. Its role is read from the name and its cluster placement, not from the body.

---

## Pipeline Placement

```text
codegen_prepare:
  Delinearization / DelinearIndices / ConcatDelinearizer -> DeConcat / CommuteConcat
  -> alias setup (AliasDependencyInduction/Reset/VerificationPass,
                  ConvertMustAliasToIOBuffer, LegalizeOpLevelAlias)
  -> Dead{Code,Store}Elimination
  -> Simplifier cluster incl. TensorOpSimplifier / SimplifySlice
  -> OptimizeAliasedCopyChain          (IO-alias chain coalescing)
  -> MemcpyElimination                 (ISL polyhedral memcpy fold)
  -> ZeroSizeTensorElimination
  -> Cast/Concat/Reshape/Slice/Transpose OpElimination + PadElimination
                                       (offloaded-op folds; each -> its *OpOptimizer)
codegen_optimization:
  -> TritiumFusion (autotuner hooks) -> LoopFusion / PartialLoopFusion
  -> ... layout / tiling / vectorize ... -> CCOpFusion -> ISel
  -> LateLowerTensorOp / LateLowerReshapeOp
                                       (materialize the offloaded mem primitives
                                        that survived copy-elimination)
```

Net dataflow: hlo2penguin emits `Offloaded*` primitives (gated by `disable-emit-offloaded-*`) → `TensorOpTransform` canonicalizes → `MemcpyElimination` + the geometric folds try to fold each away into a producer/consumer → `LoopFusion`/`OperatorFusion`/`CCOpFusion` fuse surviving compute loop-nests → `LateLowerTensorOp` lowers any leftover offloaded mem op to a real loop-nest (or a tiled offloaded instruction). The pass ordering comes from the sibling pass-roster trace rather than being re-derived here.

---

## The Boundary Matrix

The single most important orientation fact: **four non-overlapping layers can kill the same logical copy**, at four altitudes.

| Layer | IR level | What it removes / fuses |
|---|---|---|
| MLIR `NeuronOpFusion` | MHLO/StableHLO (pre-Penguin) | elementwise op clustering into MLIR fusion regions, **before** any `Offloaded*` primitive exists |
| Tritium autotuner fusion | Penguin IR (search/plan) | **decides** which loop fusions to attempt (cost/beam/MCTS); does not itself rewrite |
| **— this page (Penguin application) —** | | |
| `OptimizeAliasedCopyChain` | Penguin IR (pre-fusion) | coalesces IO-aliased copy chains to share buffers |
| `MemcpyElimination` | Penguin loop-nests / ISL | folds offloaded memcpy by polyhedral loop fusion + affine addr rewrite / reinterpret-cast |
| `*OpElimination` + `*OpOptimizer` | Penguin offloaded-op level | folds concat/slice/transpose/cast/reshape into producer/consumer via addr-range / sub-tensor rewrite at kernel boundaries |
| `LoopFusion` / `OperatorFusion` / `CCOpFusion` | Penguin loop-nest / graph | producer-consumer loop fusion; named op-pair fusions; mega-op / collective fusion |
| `LateLowerTensorOp` | Penguin IR → loop-nest | materializes the offloaded mem ops that could **not** be folded |
| **— downstream (other strands) —** | | |
| Backend `tensor_copy_elim` | Walrus backend (post-alloc) | physical-slot / AP memloc copy coalescing; runs **twice** (orders 34 & 77) |

> **NOTE —** the key distinction. The Penguin level (this page) removes copies by **value-level fusion** — rewriting producer stores, consumer loads, and affine addresses — **before** allocation. The Walrus backend removes copies by **address-level aliasing** — two values get the same physical slot — **after** allocation. The MLIR strand fuses at op-cluster level before Penguin primitives even exist. Three non-overlapping opportunities; a copy that survives one is caught by the next.

---

## Evidence summary

| Claim | Grounding | Confidence |
|---|---|---|
| `MemcpyElimination` is a polyhedral/ISL pass, not a peephole | the module docstring reads *"A more advanced loop fusion to eliminate memcpy. This pass applies ISL (i.e. polyhedral model)"*, and `islpy` / `IslSimplifier` / `isl_Map` identifiers appear in the same `.so` | CERTAIN |
| Each `*OpElimination` thin pass delegates to a `*OpOptimizer` in `TensorOpUtils` | every `transformOffloaded*` method name and its matching `*OpOptimizer` class string is present in both the driver `.so` and `TensorOpUtils.so` (5,455,456 B) | CERTAIN |
| Reshape elimination is the memcpy fold | `ReshapeOpElimination`'s transform is literally the symbol `transformOffloadedMemCpy`, consistent with `OffloadedMemCpy`'s same-dtype / same-`#elements` invariant | CERTAIN |
| The `.so` sizes quoted throughout | re-listed from disk: MemcpyElimination 1,630,208; LoopFusion 4,622,360; LowerTensorOp 10,250,552; FusionOp 2,043,232; TensorOpUtils 5,455,456; etc. | CERTAIN |
| The `LoopFusion` rejection-reason set is the legality complement | every reject string (`: Accesses do not match`, `: broadcast becomes free dim in matmul`, `: Definition not a perfect nested loop`, …) is present in `LoopFusion.so`; the *predicate body* is compiled-opaque | CERTAIN (strings), MEDIUM (bodies) |

Three things stay out of reach because they are compiled-opaque, and are marked as such where they appear: the exact boolean bodies of `can_fold_memcpy` / `is_degraded_fusion` / `isLoadStoreAccessMatchForFusion`; the numeric defaults of the `--big-tensor-threshold-one-d*` knobs and the `LoopFusion` max-inst cap; and the literal source-order of passes, which is taken from the sibling pass-roster trace.

---

## Cross-References

- [Numerics — Cast Elimination](../numerics/cast-elimination.md) — § 9.7, the *numeric* memcast-folding (dtype-conversion rules, precision, canonicalization); this page covers only the geometric side.
- [Penguin AffineExpr Algebra over pelican::Expr](affine-expr-algebra.md) — the quasi-affine address expressions `rewriteLoadAddress` / `newaddrs.add_factor` build.
- [Penguin High-Level Operator (TensorOp) Family](tensor-op-family.md) — the compute TensorOps `LowerTensorOp` materializes and `FusionOp` wraps.
- [Penguin Pass Roster & Pipeline Driver](pass-roster-pipeline.md) — the `codegen_prepare` / `codegen_optimization` ordering this page places passes within.
- [Penguin Dependency Model — DependencyEdge & EdgeKind](dependency-model.md) — the def-use / alias edges the fold-legality predicates query.
