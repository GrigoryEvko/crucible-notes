# cute Tile and Divide Operations

## Abstract

Tile and divide ops are the layout-partitioning toolkit `cute` exposes before any hardware atom is selected. They build shapes, coordinates, layouts, and views; simplify layouts via coalesce, filter, and complement; split layouts into tile and rest modes; form Cartesian products; and compose layouts into new coordinate maps. None of them lower straight to PTX. They shape the layout algebra that later `cute_nvgpu`, NVGPU, and TileAS passes consume.

## Builder Operations

| Operation | Contract |
|---|---|
| `cute.make_shape` | Build a shape or integer tuple from integer leaves. |
| `cute.make_coord` | Build a coordinate tuple from integer leaves. |
| `cute.make_layout` | Build a layout from shape and optional stride. |
| `cute.make_identity_layout` | Build a unit-stride identity layout for a shape. |
| `cute.make_identity_tensor` | Build an identity coordinate tensor for a shape. |
| `cute.make_ordered_layout` | Build a layout with stride order determined by an order tuple. |
| `cute.make_tuple` | General tuple constructor used by textual and desugared builders. |
| `cute.make_view` | Bind a pointer or iterator to a layout-backed view. |

Builder verification is mostly kind checking. Shapes must be shape-like, coords coord-like, layouts must carry compatible shape and stride structure, and views must bind a valid layout to an addressable object.

```c
LogicalResult verify_make_layout(MakeLayoutOp op) {
    require(is_shape_like(op.shape));

    if (op.stride.has_value) {
        require(is_stride_like(op.stride.value));
        require(weakly_congruent(op.shape.type, op.stride.value.type));
    }

    return success();
}
```

## Canonicalizers

`coalesce`, `filter_zeros`, and `complement` normalize layouts before divide
and product operations consume them.

| Operation | Contract |
|---|---|
| `cute.coalesce` | Merge contiguous modes into the smallest equivalent rank. |
| `cute.filter_zeros` | Collapse zero-stride broadcast dimensions to shape-one modes. |
| `cute.complement` | Compute the layout that covers the target domain not covered by the input. |

```c
Layout filter_zeros(Layout input, Optional<Profile> target_profile) {
    Layout result = input;

    for (Mode mode : result.modes) {
        if (mode.stride == 0) {
            mode.shape = 1;
        }
    }

    if (target_profile.has_value) {
        require(profile_matches(result, target_profile.value));
    }

    return normalize_layout(result);
}
```

## Divide Variants

Divide operations split an input layout `A` by a tiler `T`. Each divided mode
produces a tile component and a rest component. The variants differ only in how
they regroup those components.

| Operation | Regrouping |
|---|---|
| `cute.logical_divide` | Each divided mode becomes `(tile_i, rest_i)` in place. |
| `cute.tiled_divide` | The first mode is the tuple of all tile modes; rest modes follow. |
| `cute.flat_divide` | Tile modes, rest modes, and untouched outer modes are flattened. |
| `cute.zipped_divide` | Tile modes and rest modes are grouped into sibling tuples. |
| `cute.stencil_divide` | Sliding-window divide with window, stride, dilation, and padding-like bounds. |

```c
DividedLayout divide_layout(Layout input, Layout tiler, DivideMode mode) {
    require(rank(tiler) <= rank(input));

    SmallVector<Mode> tile_modes;
    SmallVector<Mode> rest_modes;
    SmallVector<Mode> untouched_modes;

    for (int axis = 0; axis < rank(input); ++axis) {
        if (axis < rank(tiler)) {
            Division part = divide_mode(input.mode(axis), tiler.mode(axis));
            tile_modes.push(part.tile);
            rest_modes.push(part.rest);
        } else {
            untouched_modes.push(input.mode(axis));
        }
    }

    return regroup_division(tile_modes, rest_modes, untouched_modes, mode);
}
```

Inner and outer divide are one partition viewed from opposite ends of the mode tree. The cleanest implementation normalises outer divide by reversing the relevant modes, running inner divide, then reversing the regrouped result.

```c
DividedLayout outer_divide(Layout input, Layout tiler, DivideMode mode) {
    Layout flipped_input = reverse_modes(input);
    Layout flipped_tiler = reverse_modes(tiler);
    DividedLayout divided = divide_layout(flipped_input, flipped_tiler, mode);
    return reverse_modes(divided);
}
```

## Stencil Divide

`stencil_divide` is the convolution and sliding-window form. For each selected dimension it counts the output positions a window produces:

```c
int64_t stencil_output_len(int64_t input,
                           int64_t window,
                           int64_t stride,
                           int64_t dilation) {
    require(input > 0);
    require(window > 0);
    require(stride > 0);
    require(dilation > 0);

    int64_t effective_window = (window - 1) * dilation + 1;
    require(input >= effective_window);
    return 1 + (input - effective_window) / stride;
}
```

The result mode carries both the window coordinate and the output coordinate. Lowering then maps the window coordinate to per-lane fetches and the output coordinate to the destination tile.

## Product Variants

Product operations compute a Cartesian product of layouts and regroup the
result. They are the symmetric counterpart of divide.

| Operation | Regrouping |
|---|---|
| `cute.logical_product` | Pair corresponding modes from the two operands. |
| `cute.tiled_product` | Gather the tiler modes into a leading tuple. |
| `cute.flat_product` | Flatten input and tiler modes into one mode list. |
| `cute.zipped_product` | Group input modes and tiler modes as sibling tuples. |
| `cute.raked_product` | Interleave modes for raked replication patterns. |
| `cute.blocked_product` | Replicate blocks as tile-of-tile structure. |

```c
Layout product_layout(Layout lhs, Layout rhs, ProductMode mode) {
    require(is_layout_like(lhs));
    require(is_layout_like(rhs));

    SmallVector<Mode> lhs_modes = modes(lhs);
    SmallVector<Mode> rhs_modes = modes(rhs);
    return regroup_product(lhs_modes, rhs_modes, mode);
}
```

## If You Know CUTLASS (open source) — cross-walk

The divide and product family maps almost one-to-one onto the open-source `cute/` C++ headers:

| CUTLASS C++ (`cute::`) | tileiras `cute.*` op |
|---|---|
| `logical_divide(layout, tiler)` | `cute.logical_divide` |
| `zipped_divide(layout, tiler)` | `cute.zipped_divide` |
| `tiled_divide(layout, tiler)` | `cute.tiled_divide` |
| `flat_divide(layout, tiler)` | `cute.flat_divide` |
| `local_tile(tensor, tiler, coord, mode)` | `cute.local_tile` |
| `local_partition(tensor, tiler, coord, mode)` | `cute.local_partition` |
| `logical_product(A, B)` | `cute.logical_product` |
| `zipped_product`, `tiled_product`, `flat_product` | same names under `cute.*` |
| `blocked_product`, `raked_product` | same names under `cute.*` |
| `composition(A, B)` | `cute.composition` |
| `coalesce(A)` | `cute.coalesce` |
| `filter(A)` (zero-stride filter) | `cute.filter_zeros` |
| `complement(A, total_size)` | `cute.complement` |

Each op's algebraic semantics match the open-source library: ranks, modes, tile shapes, and result mode-tree structure are preserved. The differences are representational — hierarchy lives in nested `(shape, stride)` trees rather than C++ template parameter packs, and verification happens through an MLIR verifier rather than a `static_assert` chain.

## Composition

`cute.composition` is the binary layout-function composition primitive.

```c
Optional<Layout> verify_and_compose(Layout lhs, Layout rhs) {
    require(is_layout_like(lhs));
    require(is_layout_like(rhs));

    if (cosize(lhs) > size(rhs)) {
        return none();
    }

    return compose_layout(lhs, rhs);
}
```

Composition underlies most divide and product rewrites. Divide uses the tiler's inverse and complement to split the input; product uses composition with a regrouping permutation.

## Invariants

- `rank(tiler) <= rank(input)` for divide operations.
- Divide does not change the covered coordinate set; it only exposes tile and
  rest coordinates.
- Product expands the coordinate set as a Cartesian product.
- Coalesce, filter, and complement preserve layout meaning while changing
  representation.
- Stencil divide requires positive window, stride, and dilation values.
- Composition is legal only when the inner image fits the outer domain.

## Tiled partition verifier

`sub_196AFF0` is the shared verifier for `cute.copy`, `cute.tiled_partition`, `cute.tiled_divide`, and the other partition-emitting ops in this family. One routine, 13 349 bytes, 27 distinct diagnostic strings — and despite the size it walks a single linear pipeline. The verifier never selects an atom and never inspects target-specific state; it only checks that operand shapes, the predicate operand, and the residual atom-v-rank line up with the op's declared partitioning contract.

Phase one is the rank cross-check. For `cute.copy(A, C)` and its tiled-partition siblings, source and destination ranks satisfy a small relation rather than strict equality, because partition ops legally drop or fold one rank between input and output:

| `rank(A)` | Legal `rank(C)` |
|---|---|
| `1` | `1` or `2` |
| `2` | `2` or `3` |
| `3` | `3` |

When the pair falls outside this table the verifier emits, verbatim, `"expects same size in rank N but got srcShape:{...}, dstShape:{...}"`, substituting `N` for the rank that disagreed and filling the curly braces with the printed shape tuples. The diagnostic keys on the disagreeing rank, not the operand pair, so a rank-3-to-rank-1 failure reports the first rank that cannot be reconciled rather than the overall pair.

Phase two runs only when the op carries the optional `pred` operand. The predicate is a tile-shaped mask that suppresses out-of-bounds lanes inside a partitioned copy, and it must share the same memref-shaped envelope as the data tiles. Concretely: `pred` must be a `CuteMemRefType`, its memory space must be one of `rmem`, `smem`, `gmem`, or `generic`, and its layout's swizzle component must be the identity. Bit-reversal swizzles are rejected here because a swizzled predicate would reorder mask bits relative to the data lanes they gate, breaking the per-lane correspondence the lowering relies on. On failure the verifier emits the matching diagnostic verbatim: `"pred must be a CuteMemRefType"`, `"pred memory space invalid"`, or the swizzle-identity message.

Phase three handles `restAtomVRank` retiling. When the op replicates an atom multiple times across the tile, the residual atom-v-rank is the set of dimensions the atom's natural shape does not consume. The verifier walks each residual dimension and checks that it tiles cleanly into the corresponding operand layout extent — that is, the operand extent is a multiple of the atom extent along that axis. This is the same divisibility check `cute.tiled_divide` enforces on its tiler argument, lifted into the partition verifier so copy and partition ops share one feasibility predicate.

The ordering is deliberate: phase one rejects rank-shape mismatches before phase two looks at predicate type, and both run before phase three touches the atom-v-rank walk. A reimplementation should keep that ordering. It lets the diagnostics name the first thing that went wrong rather than the deepest layer, and it lets the residual-rank walk assume rank and predicate have already been normalised.

