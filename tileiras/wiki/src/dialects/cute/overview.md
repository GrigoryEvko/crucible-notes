# cute Dialect Overview

## Abstract

`cute` is tileiras's MLIR form of CUTLASS cuTe layout algebra. It encodes shapes, strides, layouts, swizzles, coordinates, tiles, pointer views, copy atoms, and MMA atoms — together with the operations that compose, divide, complement, coalesce, and filter them — and stops short of binding any of it to NVIDIA hardware. That binding is the job of `cute_nvgpu`. Every later GPU-specific dialect (`cute_nvgpu`, `nvgpu`, `nvvm`) reads layout values produced here.

`cute` is not a code-generation dialect. Its values describe structure: how a logical tile maps to physical coordinates, how coordinates become offsets, how one layout composes with another, how a tiled copy or tiled MMA partitions work across lanes, warps, and memory spaces. That makes it the common language shared by CUTLASS pipeline modeling, TileAS layout assignment, TMA descriptor construction, and MMA lowering.

## Role in the Cascade

```text
cuda_tile / nv_tileaa / nv_tileas
    |
    | choose tile shapes, views, and partitioning
    v
cute
    |
    | attach target-specific atoms and SM-tier constraints
    v
cute_nvgpu
    |
    | normalize to nvgpu and nvvm
    v
PTX
```

Think of `cute` as a compact typed form of the same algebra that CUTLASS C++ expresses with templates. The templates become values and attributes that passes inspect, compose, verify, and lower.

## Core Concepts

| Concept | Meaning | Typical use |
| --- | --- | --- |
| Shape | Extents of a logical tile or nested coordinate tuple | Describes the iteration space of a tile. |
| Stride | Offset step for each coordinate dimension | Converts coordinates into linear offsets. |
| Layout | Shape plus stride, optionally decorated with swizzle | Maps logical coordinates to storage locations. |
| Tile | A grouped shape/layout fragment | Represents a fragment moved or computed as a unit. |
| Coord | A point in a shape or tile | Indexes layouts, views, and partitioned fragments. |
| Swizzle | Bit permutation applied to low address bits | Avoids bank conflicts or matches hardware layout rules. |
| View | Pointer or memref plus layout metadata | Describes an addressed object without losing its layout. |
| Tiled copy / MMA | Layout plus atom-level partitioning | Feeds target-specific copy or matrix-multiply lowering. |

The key invariant is that `cute` values remain algebraic. A layout should be composable and queryable without knowing whether it will eventually become a TMA descriptor, an ldmatrix load, a WGMMA operand, or a Blackwell tensor-memory operation.

## Layout Semantics

A layout maps a coordinate to an offset. The simplest model is a shape/stride pair; the real dialect supports nested tuples, composition, constraints, and swizzles, but the core rule is still coordinate-to-offset evaluation.

```c
int64_t layout_offset(Layout layout, Coord coord) {
    require(rank(layout.shape) == rank(layout.stride));
    require(rank(coord) == rank(layout.shape));

    int64_t offset = 0;

    for (int dim = 0; dim < rank(coord); ++dim) {
        require(0 <= coord[dim] && coord[dim] < layout.shape[dim]);
        offset += coord[dim] * layout.stride[dim];
    }

    return apply_swizzle(layout.swizzle, offset);
}
```

Composition substitutes one coordinate mapping into another. This is what lets a high-level tile layout be refined into per-warp, per-lane, or per-atom layouts without flattening the whole model into ad hoc integer arithmetic.

```c
Layout compose(Layout outer, Layout inner) {
    require(result_shape(inner) == domain_shape(outer));

    Layout result;
    result.shape = domain_shape(inner);
    result.stride = transform_stride(inner.stride, outer.stride);
    result.swizzle = compose_swizzles(inner.swizzle, outer.swizzle);
    return canonicalize_layout(result);
}
```

For a reimplementation, the storage class the original compiler picks does not matter. What does matter: equivalent layouts canonicalize consistently, nested tuple layouts preserve rank and dimension identity, and swizzle composition stays explicit until a target-specific lowering consumes it.

## In-Memory IR Tier

Treat `cute` as an in-memory compiler tier. It exists so passes can exchange rich layout objects without serializing every intermediate shape into the public input format. Textual rendering helps with debugging and documentation; production input normally enters through `cuda_tile`, `nv_tileaa`, `cutlass`, or another higher-level dialect, and the pipeline constructs `cute` objects internally.

Practical consequence: do not build tooling that depends on `cute` bytecode as a stable interchange format unless the serializer is explicitly provided. Textual dumps are for inspecting the compiler, not as a user-facing artifact.

## Verifier Invariants

A `cute` verifier should enforce algebraic consistency before target-specific lowering starts:

- shape and stride ranks agree,
- coordinates are in bounds for the shape they index,
- layout composition connects compatible domains and ranges,
- constrained integers satisfy their divisibility or range constraints,
- swizzle masks only touch valid low address bits,
- pointer and memref views preserve element type, address space, and bit layout,
- tiled copy and tiled MMA atoms agree with the value layouts they consume,
- tuple-valued shapes preserve dimension order during canonicalization.

These checks make later GPU lowering deterministic. If a malformed layout reaches `cute_nvgpu`, the target-specific verifier may only see an invalid atom shape, not the original algebra mistake that caused it.

## Reimplementation Checklist

A useful `cute` implementation should begin with a small, exact layout algebra: shape, stride, coord, layout, swizzle, tuple, composition, and canonicalization. Add pointer/memref views once offset calculation is stable. Add tiled-copy and tiled-MMA descriptors last, because those descriptors depend on the layout algebra being correct.

Required pieces:

- immutable or hash-consed layout values,
- rank and shape checking for every layout operation,
- coordinate-to-offset evaluation,
- layout composition and canonicalization,
- swizzle composition with explicit legality checks,
- view types that carry element type, memory space, and bit layout,
- tiled-copy and tiled-MMA descriptors that can be consumed by `cute_nvgpu`,
- textual printer support for inspecting layouts in dumps.

## If You Know CUTLASS (open source) — cross-walk

The open-source `cute/` C++ headers map almost directly onto this dialect:

| CUTLASS C++ (cute namespace) | tileiras `cute` IR |
|---|---|
| `cute::Shape<...>` and `cute::Stride<...>` | hierarchical `(shape, stride)` tuples in a `!cute.layout` |
| `cute::Layout<Shape, Stride>` | `!cute.layout` type (kind discriminator at offset `+0x88`) |
| `cute::Swizzle<B, M, S>` | `!cute.swizzle` value composed into a layout via `make_composed_layout` |
| `cute::make_tile`, `cute::make_layout` | `cute.make_tile`, `cute.make_layout` ops |
| `cute::Tensor<Engine, Layout>` | `cute.make_view` ties a pointer/memref to a layout |
| `composition`, `complement`, `logical_divide`, `logical_product` | identically-named `cute.*` ops |
| `cute::make_tiled_copy`, `cute::make_tiled_mma` | `cute.make_tiled_copy`, `cute.make_tiled_mma` (target binding deferred to `cute_nvgpu`) |
| Compile-time integer arithmetic in C++ templates | `cute.make_int_tuple` + `tuple_div/mod/mul/sub` ops |

The main difference is where the target boundary sits. The open-source `cute/` library compiles SM-specific `MMA_Atom` and `Copy_Atom` traits straight into the same headers; tileiras keeps the SM-neutral atoms in `cute` and pushes every target-specific atom into `cute_nvgpu`. A pass running inside `cute` should never need to ask which SM tier is in use. If it does, the layout choice belongs on the `cute_nvgpu` side.

## Cross-links

- [Layout Algebra and Descriptor Grammar](layout-algebra-and-descriptor-grammar.md) covers the concrete grammar and round-trip rules.
- [Tile and Divide Ops](tile-and-divide-ops.md) covers tile partitioning operations.
- [Atom Builders and Desugar](atom-builders-and-desugar.md) covers construction of copy and MMA atoms.
- [Verifiers](verifiers.md) covers layout and atom verifier behavior.
