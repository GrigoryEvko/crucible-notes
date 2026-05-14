# nv_tileas Folds and Memory Consistency

## Abstract

`nv_tileas` canonicalization is deliberately split. Pure tile-structure rewrites simplify `alloc_tensor`, `insert_slice`, `extract_slice`, `view`, and structured control-flow scaffolding. Memory-ordering operations sit behind `MemoryConsistencyOpInterface` — pure canonicalizations must not reorder, duplicate, or erase them.

The rewrite shapes, the legality conditions that gate them, and the separation rule that keeps folding apart from ordering-sensitive transformations appear in the sections below.

## Folding Model

Most useful TileAS simplification lives in rewrite patterns rather than per-operation constant folds. The interesting cases are structural — typically an `scf.for` or `scf.if` plus tile slice operations — not a single operation with constant operands. The canonicalize driver runs all seven patterns to fixed point against the entire module; the recursive expression simplifier handles deeper boolean and integer cleanup elsewhere.

Pipeline-related lowering may still invoke ordinary MLIR folding during one-to-N conversion. Treat those folds as local simplifications only. Larger layout-chain removal belongs to the layout-conversion removal pass, not to a hidden `convert_layout` fold.

## Canonicalization Patterns

The dialect installs seven canonicalization patterns. Each is documented below as an input/output pair plus the matching legality condition.

| Pattern | Root | Summary |
|---|---|---|
| simplify extract slice | `nv_tileas.extract_slice` | Constant offsets/sizes/strides collapse into a static-shape view. |
| decompose loop iter args | `scf.for` | Sinks `alloc_tensor` into the loop body; removes redundant iter args. |
| decompose if by insert slice | `scf.if` | Duplicates allocation/insertion chains into each branch. |
| decompose if by extract slice | `scf.if` | Sinks extraction into each branch. |
| swap view and extract slice | `nv_tileas.extract_slice` | Rewrites `extract_slice(view(x))` into `view(extract_slice(x))`. |
| coalesce perfectly nested loops | `scf.for` | Flattens compatible nested loops. |
| simplify extract from insert | `nv_tileas.extract_slice` | Replaces exact extract-after-insert with the inserted source. |

### Simplify Extract Slice

The pattern collapses a slice operation whose offset, size, and stride operands are all `arith.constant` values into a view whose result type bakes those values into the static shape.

Input IR:

```mlir
%c0 = arith.constant 0 : index
%c64 = arith.constant 64 : index
%c1 = arith.constant 1 : index
%slice = nv_tileas.extract_slice %src[%c0, %c0][%c64, %c64][%c1, %c1]
    : tensor<128x128xf32> to tensor<?x?xf32>
```

Output IR:

```mlir
%slice = nv_tileas.extract_slice %src[0, 0][64, 64][1, 1]
    : tensor<128x128xf32> to tensor<64x64xf32>
```

Legality: every offset, size, and stride operand must resolve to a non-negative integer constant. The rewrite preserves the slice's memory ordering attributes (there are none on the pure slice op) and uses fold-aware constant indexing the canonicalizer already trusts.

### Decompose Loop Iter Args

The pattern recognizes a loop iter-arg whose init traces back to `alloc_tensor` through a chain of `insert_slice` operations, and whose yielded value traces back to the same allocation through a parallel chain. It sinks the allocation into the loop body, re-emits the insertion chain inside the body, and drops the iter-arg from the loop's signature.

Input IR:

```mlir
%init = nv_tileas.alloc_tensor : tensor<128x128xf32>
%init_v = nv_tileas.insert_slice %seed into %init[%i0, %j0][%m, %n][1, 1]
    : tensor<?x?xf32> into tensor<128x128xf32>

%out = scf.for %k = %k0 to %k1 step %k_step
    iter_args(%buf = %init_v) -> tensor<128x128xf32> {
    %step_v = "produce_tile"(%buf, %k) : (tensor<128x128xf32>, index) -> tensor<?x?xf32>
    %next = nv_tileas.insert_slice %step_v into %buf[%i_k, %j_k][%m, %n][1, 1]
        : tensor<?x?xf32> into tensor<128x128xf32>
    scf.yield %next : tensor<128x128xf32>
}
```

Output IR:

```mlir
%out = scf.for %k = %k0 to %k1 step %k_step iter_args() {
    %buf = nv_tileas.alloc_tensor : tensor<128x128xf32>
    %step_v = "produce_tile"(%buf, %k) : (tensor<128x128xf32>, index) -> tensor<?x?xf32>
    nv_tileas.insert_slice %step_v into %buf[%i_k, %j_k][%m, %n][1, 1]
        : tensor<?x?xf32> into tensor<128x128xf32>
    scf.yield
}
```

Legality (all must hold):

1. The iter-arg init traces through a chain of pure tile-structure ops to a single `alloc_tensor`.
2. The yielded value traces through a parallel chain to the same allocation.
3. No operation in either chain implements `MemoryConsistencyOpInterface`.
4. No use of the iter-arg outside the loop body depends on the loop-carried value (the rewrite eliminates that result).

The rewrite is safe because `alloc_tensor` is a pure tile constructor: re-emitting it inside the loop body produces a value with the same SSA semantics for each iteration, and the loop's signature contracts by exactly one iter-arg.

### Decompose If by Insert / Extract Slice

The two branch-decomposition patterns rewrite `scf.if` results whose chains involve `insert_slice` or `extract_slice` so that each branch performs its own allocation and slice work rather than yielding a shared mutable tile.

Input IR:

```mlir
%init = nv_tileas.alloc_tensor : tensor<64x64xf32>
%init_v = nv_tileas.insert_slice %seed into %init[0, 0][%m, %n][1, 1]
    : tensor<?x?xf32> into tensor<64x64xf32>
%r = scf.if %cond -> tensor<64x64xf32> {
    %v = nv_tileas.insert_slice %a into %init_v[%i, %j][%m, %n][1, 1]
        : tensor<?x?xf32> into tensor<64x64xf32>
    scf.yield %v : tensor<64x64xf32>
} else {
    %v = nv_tileas.insert_slice %b into %init_v[%i, %j][%m, %n][1, 1]
        : tensor<?x?xf32> into tensor<64x64xf32>
    scf.yield %v : tensor<64x64xf32>
}
```

Output IR:

```mlir
%r = scf.if %cond -> tensor<64x64xf32> {
    %ta = nv_tileas.alloc_tensor : tensor<64x64xf32>
    %ta_v = nv_tileas.insert_slice %seed into %ta[0, 0][%m, %n][1, 1] : ...
    %v = nv_tileas.insert_slice %a into %ta_v[%i, %j][%m, %n][1, 1] : ...
    scf.yield %v : tensor<64x64xf32>
} else {
    %tb = nv_tileas.alloc_tensor : tensor<64x64xf32>
    %tb_v = nv_tileas.insert_slice %seed into %tb[0, 0][%m, %n][1, 1] : ...
    %v = nv_tileas.insert_slice %b into %tb_v[%i, %j][%m, %n][1, 1] : ...
    scf.yield %v : tensor<64x64xf32>
}
```

Legality: both branches' yielded values trace back to the same allocation through pure tile-structure chains, no chain crosses a memory-consistency op, and the allocation has no live use outside the `scf.if`. Duplicating the allocation per branch is what makes the rewrite safe — the rewrite never creates a shared mutable tile across the two branches.

### Swap View and Extract Slice

The pattern rewrites `extract_slice(view(x))` into `view(extract_slice(x))` when the slice can be performed on the underlying storage at the same offset/stride and then re-viewed. The legality condition is that the view's layout transformation commutes with the slice operation — that is, applying the slice to the underlying tensor and then taking the view produces the same SSA value as applying the view to the underlying tensor and then taking the slice.

This rewrite typically fires after coalesce-perfectly-nested-loops produces fresh `extract_slice` ops over a view-shaped source.

### Coalesce Perfectly Nested Loops

The pattern flattens an outer `scf.for` and an inner `scf.for` when the inner loop is the only operation in the outer loop's body, neither loop has live iter-args, and the inner loop's bounds and step are constant. The merged loop carries the product of the two iteration ranges and re-derives the original induction variables inside the body via `arith.divsi`/`arith.remsi`.

Legality: the outer body must contain only the inner loop plus a terminator. Any other operation in the outer body forbids coalescing because it would have to run a different number of times after the merge.

### Simplify Extract from Insert

The pattern recognizes `extract_slice(insert_slice(src, dst, offsets), offsets) → src` when the extract and insert use the exact same offsets, sizes, and strides. The fold returns the inserted source directly, bypassing the storage round-trip.

Input IR:

```mlir
%t = nv_tileas.insert_slice %x into %dst[%i, %j][%m, %n][1, 1]
    : tensor<?x?xf32> into tensor<64x64xf32>
%y = nv_tileas.extract_slice %t[%i, %j][%m, %n][1, 1]
    : tensor<64x64xf32> to tensor<?x?xf32>
```

Output IR:

```mlir
%y = %x
```

Legality: the offsets, sizes, and strides must be equal as SSA values (or as constants after fold-aware comparison); no other operation may insert or extract a slice into the same storage region between the matched pair.

## Memory Consistency Interface

`MemoryConsistencyOpInterface` marks operations whose ordering matters. Canonicalization may inspect them, but pure tile rewrites must not move across them or erase them.

| Operation group | Why it participates |
|---|---|
| async load/store/copy/dot | has visible async memory ordering |
| async waits | observes completion of async work |
| async TMA load/store/reduction/gather/scatter | consumes descriptor and memory-ordering semantics |
| synchronous `copy` | may observe or publish data relevant to async regions |
| `make_tiled_tma_desc` | descriptor result is consumed by TMA operations |
| `reduce` and `scan` | region bodies may carry ordering-sensitive operations |

Pure tile-shaping operations are intentionally excluded:

- `alloc_tensor`
- `insert_slice`
- `extract_slice`
- `view`
- `async.future_wait` (ordering rides on the future token itself)
- async pipeline region plumbing (ordering rides on producer/consumer interfaces and tokens)

## Safe-Rewrite Predicate

A canonicalization pattern is safe when every operation it moves, duplicates, or erases lies outside the memory-consistency set and is reached only through SSA chains of pure tile-structure operations. The match driver walks each chain from its root toward the defining op of the rewrite source, rejecting the match the moment it encounters a memory-consistency op or any op that is neither pure tile structure nor a constant.

The walk terminates at a block boundary, at the first non-pure operation, or at a fixed-point sink (an `alloc_tensor` for the iter-arg decomposition, an `insert_slice` for the extract-from-insert fold). A chain that hits a memory-consistency op aborts immediately so the rewrite never even considers reshuffling ordered ops.

## Layout Conversion Folding

The identity `convert_layout(convert_layout(x))` belongs to the layout-conversion removal pass, not to a local `convert_layout` fold. The legality of commuting or deleting a layout conversion depends on whether the value lives in register space, shared memory, tensor memory, or crosses a pipeline boundary — and only the pass has that context.

The pass-level rewrite trims a chain when the composition reduces to identity in the target's atom catalog. Two factors decide whether the composition reduces:

1. The inner conversion's source layout and the outer conversion's destination layout must lie in compatible storage classes. A register-to-shared conversion followed by a shared-to-register conversion is identity only when the register layout on both sides agrees on lane assignment and vector width.
2. The atom catalog must contain a direct atom from the inner source to the outer destination. If it does not, the pass keeps the chain because materializing the intermediate layout is what makes the round-trip legal at all.

When both conditions hold, the pass replaces the outer conversion's result with the inner conversion's source, leaving the inner conversion as dead code that ordinary DCE picks up.

Keeping this in a pass rather than a fold lets the compiler consult target atom plans and memory-space rules.

## Ordering Invariants

- Canonicalization roots may be pure tile ops or structured control-flow ops.
- Match chains may include `alloc_tensor`, `insert_slice`, `extract_slice`, `view`, and constants.
- Match chains must reject `copy`, async memory operations, TMA operations, reductions, scans, and descriptor builders.
- Rewrites must not alter memory semantic, memory scope, in-bounds, padding, or RMW attributes.
- Branch decomposition duplicates allocations per branch rather than sharing a mutable tile across arms.
- Layout-chain removal belongs to the layout-conversion pass, where target layout plans are available.

## Cross-References

[Operation Roster and Builders](op-roster-and-builders.md#operation-families) catalogues the operations these rewrites target. [Verifiers](verifiers.md#async-pipeline-verification) describes the legality contracts that survive the rewrites. [Types](types.md#pipeline-types) describes the iterator and async-token types that anchor the memory-consistency interface.
