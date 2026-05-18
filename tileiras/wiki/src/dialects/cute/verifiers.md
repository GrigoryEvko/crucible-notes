# cute Verifiers

## Abstract

The `cute` verifier surface guards layout algebra. It checks that shapes, coordinates, layouts, composed layouts, views, tuples, divide/product operands, memrefs, atom fragments, and tuple arithmetic stay compatible before lowering picks a target instruction. The mental model is short: verifiers guard kind, rank, congruence, staticness, and algebraic validity.

## Verification Categories

| Category | Examples | Verbatim diagnostic prefix |
|---|---|---|
| Layout builders | `make_layout`, `make_shape`, `make_stride`, `make_composed_layout` | structural — checked by the type-kind discriminator |
| Layout queries | `get_shape`, `get_stride`, `get_layout` | `"expects \`input\` to be a layout or a view, got "` |
| Algebra | `composition`, `right_inverse`, `coalesce`, `filter_zeros`, `recast_layout` | `"expects an input of type layout or composed layout, but got "` |
| Divide / product | `logical_divide`, `tiled_divide`, `flat_divide`, `stencil_divide` | `"invalid tiler type, got"` / `"expects rank(tiler) <= rank(input), but got input="` |
| Tile and mode | `local_tile`, `local_partition`, `group_modes`, `select`, `size`, `cosize` | `"unexpected tiler type, got "` |
| Coordinates | `crd2idx`, `make_fragment_like`, `make_view` | `"unexpected coordinate type, got "` / `"expected a coordinate of rank "` |
| Tile-to-shape | `tile_to_shape` | `"invalid input types for tile_to_shape, got "` |
| Atom call sites | `copy_atom_call`, `mma_atom_call` | covered by the atom-interface verifier |

## Verbatim Diagnostics

Every `cute` op emits diagnostics through `Op::emitOpError(<verbatim string>)`. The strings below are the user-visible contract; tests match diagnostics by string and a reimplementation must preserve them byte-for-byte. Grouped by op family:

### `cute.local_tile`

- `"unexpected tiler type, got "` — tiler is not a `LayoutType` or `TileType`
- `"unexpected coordinate type, got "` — coord is not a `CoordType`
- `"expected a coordinate of rank "` (followed by `<n>` and `" but got "` and the coord's type) — coord rank does not match the selected mode
- `"Failed to dice "` (followed by the tile and the coord) — `dice_view` returned a malformed view
- `"failed to construct a valid coordinate from "` (followed by the coord print)
- `"expected a view as an input but got "` (followed by the input type)

### `cute.local_partition`

- `"expects LayoutType tiler, but got "`
- `"expects LayoutType tiler with static shape, but got "`
- `` "expects `input` to be a layout or a view, got " ``
- `` "expects `target_profile` be CoordType, but got " ``
- `"unable to coalesce `input` of type "`
- `"unable to construct a coordinate for local_partition"`

### `cute.make_fragment_like`

- `` "expects `input` is CuteMemRefType or CuteLayoutTypeInterface, but got " ``
- `` "expects `src` is LayoutTypeInterface or CuteMemRefType, but got " ``
- `` "expects `src.layout` is LayoutType or ComposedLayoutType, but got " ``
- `"unable to make fragment-like layout"`
- `"unable to make fragment-like layout from "`

### `cute.group_modes`

- `"expects begin in the range of [-rank , rank-1], but got begin [{0}] and rank [{1}]"`
- `"expects end in the range of [-rank+1 , rank], but got end [{0}] and rank [{1}]"`
- `"expects begin < end, but got begin [{0}] ([{1}]) and end [{2}] ([{3}])"`
- `"expects view or layout type, but got "`
- `"unable to infer return type with inputs "`

### `cute.size` and `cute.cosize`

- `"input type [{0}] has invalid values."`
- `"mode [{0}] has invalid values for input type {1}"`
- `"unable to compute size for input {0} and mode [{1}]"`
- `"can't derive meaningful cosize of composed layout when inner is affine: "`
- `"mode [{0}] is invalid for input type {1}"`
- `"unable to compute cosize for input {0} and mode [{1}]"`

### `cute.select`

- `"Invalid results for select(). Modes: ["`

Out-of-range and duplicate modes are reported through the shared mode-list helper. The helper formats the offending mode and the rank into a `"vector::_M_range_check: __n (which is %zu) >= this->size() (which is %zu)"` failure when the underlying small-vector lookup throws.

### `cute.tile_to_shape`

- `"invalid input types for tile_to_shape, got "`
- `"Target and kernel shapes must be congruent."`
- `"Lower padding shape must be congruent with target shape"`
- `"Upper padding shape must be congruent with target shape"`
- `"Traversal stride must be congruent with target shape"`
- `"expects target shape and order operands have same rank, but got "`
- `"expects only static order modes, but got "`

### `cute.logical_divide` / `tiled_divide` / `flat_divide` / `stencil_divide`

- `"invalid tiler type, got"` (followed by the tiler type print)
- `"invalid input type, got "`
- `"expects rank(tiler) <= rank(input), but got input="` (followed by both ranks)
- `"failed to perform a valid division of "` (followed by `<input>` and `<tiler>`)

### `cute.recast_layout`

- `` "expects `src` is LayoutTypeInterface, but got " ``
- `"unable to recast layout "`

### `cute.right_inverse`

- `"expects an input of type layout or composed layout, but got "`
- `"expects an input with static shape, but got "`
- `"unable to compute a right inverse for input "`

### `cute.filter_zeros`

- `"expects a ShapeType for the target profile, but got "`
- `"Expects target_profile has the same profile with the src, but src_profile is: "`
- `"and target_profile is: "`
- `"unable to filter zeros for input "`

## Layout Builder Checks

`make_layout` accepts a shape and a stride, checks rank congruence, and produces a `LayoutType`. `make_composed_layout` accepts an outer layout, an inner layout or swizzle, and an integer-tuple offset, then runs the full `compose(outer, inner)` algebra to confirm the result is a well-formed layout.

```c
LogicalResult verify_make_composed_layout(MakeComposedLayoutOp op) {
    if (!implements_layout_interface(op.outer))
        return op.emitOpError("expects an input of type layout or composed layout, but got ") << op.outer.getType();
    if (!is_int_tuple(op.offset))
        return op.emitOpError("expects `target_profile` be CoordType, but got ") << op.offset.getType();
    if (!implements_layout_interface(op.inner) && !is_swizzle(op.inner))
        return op.emitOpError("expects `input` to be a layout or a view, got ") << op.inner.getType();

    Optional<Layout> layout = compose_layout(op.outer, op.inner);
    if (!layout)
        return op.emitOpError("failed to perform a valid division of ") << op.outer << " " << op.inner;
    if (!offset_is_valid_for_layout(op.offset, *layout))
        return op.emitOpError("unable to construct a coordinate for local_partition");
    return success();
}
```

## Mode and Rank Checks

`cute` mode-range operations accept Python-style ranges. Negative modes are normalised relative to rank before the range check, and the three boundary checks emit distinct diagnostics so users can see which side failed.

```c
LogicalResult verify_mode_range(int begin, int end, int rank, Op op) {
    int nb = begin < 0 ? begin + rank : begin;
    int ne = end   < 0 ? end   + rank : end;

    if (nb < 0 || nb >= rank)
        return op.emitOpError(
            "expects begin in the range of [-rank , rank-1], but got begin [")
            << begin << "] and rank [" << rank << "]";
    if (ne < 0 || ne > rank)
        return op.emitOpError(
            "expects end in the range of [-rank+1 , rank], but got end [")
            << end << "] and rank [" << rank << "]";
    if (nb >= ne)
        return op.emitOpError(
            "expects begin < end, but got begin [")
            << begin << "] ([" << nb << "]) and end [" << end << "] ([" << ne << "])";
    return success();
}
```

The `select`-family mode list rejects out-of-range modes and duplicates by formatting the offending set into the `"Invalid results for select(). Modes: ["` prefix.

```c
LogicalResult verify_mode_list(ArrayRef<int32_t> modes, int rank, Op op) {
    BitSet seen(rank);
    for (int32_t m : modes) {
        if (m < 0 || m >= rank || seen.contains(m))
            return op.emitOpError("Invalid results for select(). Modes: [")
                << format_mode_list(modes) << "]";
        seen.insert(m);
    }
    return success();
}
```

## Divide and Product Checks

Divide requires a layout-like input and a tile-like tiler, with tiler rank at most input rank. The verifier *actually runs* the algebra during verification so an invalid regrouping fails inside `verify` rather than producing an ill-formed result type. The algorithm has three gates and one algebraic step:

```c
LogicalResult verify_logical_divide(LogicalDivideOp op) {
    Type tiler = op.tiler.getType();
    if (!implements_tile_like(tiler) && !implements_layout_like(tiler))
        return op.emitOpError("invalid tiler type, got") << tiler;

    Type input = op.input.getType();
    if (!implements_layout_like(input) && !implements_view_like(input))
        return op.emitOpError("invalid input type, got ") << input;

    if (rank(tiler) > rank(input))
        return op.emitOpError("expects rank(tiler) <= rank(input), but got input=")
            << rank(input) << " and tiler=" << rank(tiler);

    Layout in_layout = layout_of(op.input);
    Optional<Layout> result = try_logical_divide(in_layout, op.tiler);
    if (!result || result->getType() != op.result.getType())
        return op.emitOpError("failed to perform a valid division of ")
            << in_layout << " by " << op.tiler;
    return success();
}
```

`tiled_divide`, `flat_divide`, and `stencil_divide` follow the same skeleton with a different kind impl in the algebraic step. Product variants share the rank gate but build through the layout-product algebra instead.

## Tuple Arithmetic Checks

Tuple arithmetic is structural. The operands must have the same tuple kind, and each leaf operation must be defined. Division and modulo reject zero divisors before any leaf walk runs because a zero divisor would propagate a hard error through every later layout fold.

```c
LogicalResult verify_tuple_arithmetic(TupleArithOp op) {
    if (!same_tuple_kind(op.lhs.getType(), op.rhs.getType()))
        return op.emitOpError("input type [") << op.lhs.getType() << "] has invalid values.";

    for (LeafPair leaf : zip_leaves(op.lhs, op.rhs)) {
        if ((op.kind == TUPLE_DIV || op.kind == TUPLE_MOD) && is_zero(leaf.rhs))
            return op.emitOpError("mode [") << index_of(leaf) << "] has invalid values for input type "
                                            << op.lhs.getType();
        if (!arithmetic_supported_for_leaf(leaf))
            return op.emitOpError("unable to compute size for input ")
                << op.lhs.getType() << " and mode [" << index_of(leaf) << "]";
    }
    return success();
}
```

`to_int_tuple` rejects scaled bases, underscores, error leaves, and non-tuple sources because the LLVM lowering downstream expects a plain integer tuple.

## Coordinates, Local Tiles, and Slices

Coordinate-based operations check **weak congruence**: the coordinate profile must fit the layout or view profile but may be less specific wherever the input has dynamic structure. The `local_tile` verifier runs five gates in fixed order:

```c
LogicalResult verify_local_tile(LocalTileOp op) {
    if (!is_tile_like(op.tiler) && !is_shape_like(op.tiler))
        return op.emitOpError("unexpected tiler type, got ") << op.tiler.getType();
    if (!is_coord(op.coord))
        return op.emitOpError("unexpected coordinate type, got ") << op.coord.getType();
    if (op.mode.length < rank(op.coord))
        return op.emitOpError("expected a coordinate of rank ")
            << op.mode.length << " but got " << op.coord.getType();
    if (!is_view(op.input) && !is_layout_like(op.input))
        return op.emitOpError("expected a view as an input but got ") << op.input.getType();

    Optional<View> v = dice_view(op.input, op.tiler, op.coord, op.mode);
    if (!v)
        return op.emitOpError("Failed to dice ") << op.tiler << " with " << op.coord;
    return success();
}
```

`local_partition` shares the input-kind gate but applies a stricter tiler check (`LayoutType` with static shape) and asks for a `CoordType` target profile.

## Worked Example: `crd2idx` Weak Congruence Violation

A coordinate that does not satisfy weak congruence against the layout's shape fails at the rank gate. Consider a rank-3 layout indexed by a rank-2 coordinate:

```mlir
%shape  = cute.make_int_tuple %c4, %c8, %c16 : !cute.shape
%stride = cute.make_int_tuple %c128, %c16, %c1 : !cute.stride
%layout = cute.make_layout_raw %shape, %stride : !cute.layout

%coord  = cute.make_int_tuple %ci, %cj : !cute.coord<rank=2>

%idx = cute.crd2idx %coord, %layout : !cute.coord, !cute.layout -> index
```

`cute.crd2idx` desugars into a `local_tile`-style walk for verification — the coord profile must be weakly congruent with the layout's shape profile. The rank gate fires first because the coord type stores rank 2 while the layout's shape stores rank 3:

```text
error: expected a coordinate of rank 3 but got !cute.coord<rank=2>
```

The diagnostic uses the verbatim `"expected a coordinate of rank "` prefix from the `local_tile` ladder; `crd2idx` shares the same helper so the message is identical. The mode list (`%mode = []` here) has length 0, so the rank check reduces to `mode.length(0) < rank(coord)(2)`, which fails and selects this diagnostic.

A second variant of the same bug — a same-rank coord whose shape does not weakly fit — fails at the dicing step instead:

```mlir
%coord = cute.make_int_tuple %ci, %cj, %ck : !cute.coord<rank=3>
// %ck has static value 32, but layout's shape[2] = 16

%idx = cute.crd2idx %coord, %layout : !cute.coord, !cute.layout -> index
```

```text
error: Failed to dice !cute.layout<((4,8,16),(128,16,1))> with !cute.coord<(0,0,32)>
```

The rank gate passes (`mode.length == rank(coord) == 3`), but `dice_view` rejects the coord because its third component (32) is out of bounds for the layout's third shape element (16). The diagnostic uses the verbatim `"Failed to dice "` prefix and prints both the tile and the offending coord.

## Memref and Scaled-Index Checks

`cute.memref.load` and related pointer helpers validate element type, bit width, address space, and coordinate congruence. Boolean element loads are accepted only in the memory space where the implementation can represent them safely.

```c
LogicalResult verify_memref_load(MemrefLoadOp op) {
    MemrefType memref = op.memref.type;

    require(is_supported_element_type(memref.element_type));
    require(is_power_of_two(bit_width(memref.element_type)));
    require(is_supported_address_space(memref.address_space));
    require(is_coord(op.coord));
    require(weakly_congruent(profile(op.coord), profile(memref.layout)));

    if (memref.element_type == i1_type()) {
        require(memref.address_space == register_memory_space());
    }

    return success();
}
```

`load_scaled_index` adds two requirements: a cute pointer type and an integer-tuple stride. Non-power-of-two element widths are rejected because scaled-index math would otherwise need a slow path the lowering does not provide.

## Atom and Fragment Checks

Tiled copy and tiled MMA builders confirm that the result atom type matches the operand atom type. `cute.mma.make_fragment` is stricter — it checks operand role, atom type, input profile, vector-mode staticness, and the inferred result type.

```c
LogicalResult verify_mma_fragment(MmaFragmentOp op, Target target) {
    require(is_mma_operand_id(op.operand_id));
    require(op.atom.type.implements_mma_atom());
    require(is_memref_like(op.source) || is_shape_like(op.source));

    Profile profile = infer_profile(op.source);
    require(profile.rank >= 3);
    require(vector_mode(profile).is_static);
    require(vector_mode(op.atom.type.profile).is_static);
    require(vector_modes_compatible(profile, op.atom.type.profile));

    Type inferred = infer_fragment_type(op.atom, op.source, op.operand_id, target);
    require(inferred == op.result.type);
    return success();
}
```

The fragment verifier reaches the target only through the atom interface. The generic `cute` dialect must not hard-code every SM instruction variant.

## LayoutTypeInterface Kind Discriminator

Every `cute` Type carries a kind-discriminator slot inside its `TypeStorage` block, separate from the upstream-MLIR `TypeID` at the head of the storage. The slot is one of seven static sentinels; sentinel identity, not content, drives dispatch. Walkers, verifiers, builders, parsers, and folders all dispatch on this slot by pointer-identity against the seven-entry table, exactly the same way upstream MLIR dispatches on `TypeID` at the type header. The duplicated slot exists because the upstream `TypeID` carries the `LayoutTypeInterface` interface-id, and the cute dialect needs a separate, denser tag for the seven-kind switch.

| Ordinal | Kind             | Meaning                                                                |
|---:|------------------|------------------------------------------------------------------------|
| 0 | `ComposedLayout` | `compose(L1, L2)` — a layout formed by composing two sub-layouts        |
| 1 | `Layout`         | Plain `(Shape, Stride)` pair                                            |
| 2 | `Swizzle`        | `swizzle<B, M, S>` — bit-reversal swizzle layout                        |
| 3 | `Tile`           | Tile-shape descriptor (shape only, no stride)                           |
| 4 | `Shape`          | Pure shape tuple (no stride)                                            |
| 5 | `Coord`          | Coordinate tuple (no stride)                                            |
| 6 | `IntTuple`       | Pure-integer tuple                                                      |

Four parallel function-pointer tables index by the kind ordinal — the row position in the seven-entry table — and each holds one handler per kind. Together they cover the lifecycle of every `cute` Type: verification, asm printing, bytecode parsing, and folding.

| Table | Role |
|---|---|
| `verify`   | per-kind verifier callback |
| `print`    | per-kind asm printer |
| `parse`    | per-kind bytecode reader |
| `fold`     | per-kind canonicalisation |

A separate nine-entry operand-kind table records the expected kind discriminator for each operand slot of multi-operand ops. The arity of nine covers the widest cute op: `cute.partition` consumes up to nine sub-layouts (input view, tiler, coord, mode list, and up to five auxiliary atom-binding slots). Narrower ops such as `cute.compose` (two operands) and `cute.zipped_divide` (three) leave the trailing entries unused but read the same table layout, which keeps the verifier-side per-operand checks index-uniform. The partition op page documents the consumer side of this slot table — see [Tile and Divide Ops — Tiled partition verifier](tile-and-divide-ops.md#tiled-partition-verifier).

Dispatch is a linear scan over the seven sentinels followed by an indexed call into the appropriate table. Pointer-identity comparison keeps the inner loop to a single compare per kind; falling off the end is a hard error because every well-formed `cute` Type must carry one of the seven sentinels.

```c
void *dispatch_by_kind(const CuteType *t, const Handler *table) {
    for (int i = 0; i < 7; ++i) {
        if (t->kind == kSentinels[i]) {
            return table[i];
        }
    }
    abort();   /* unknown kind — should be unreachable */
}
```

A reimplementation should keep the kind ordering and the four-table convention. Reordering the sentinels silently mis-routes verify and fold to the wrong handler because every table is indexed by the same ordinal.

## Side Effects

Most layout algebra is pure. Copy atoms, local partitions, fragments, and view construction may allocate or read/write resources through MLIR side-effect interfaces. Model effects explicitly — otherwise canonicalizers will reorder memory-meaningful operations past each other.

## Invariants

- Kind checks are interface-based where possible, not string-based.
- Shape and stride operands are weakly congruent when paired.
- Divide and product run the algebra enough to prove their result type.
- Tuple division and modulo reject zero divisors.
- Coordinates are weakly congruent with the layout or view they index.
- Memref operations reject unsupported element widths and address spaces.
- Atom fragments verify through atom interfaces and target profiles.
- Pure layout algebra remains movable; effectful atom and view operations do
  not.

## Cross-References

[TypeID Sentinels and Anchors — Idiom 1: Static Pointer-Identity Sentinel](../../mlir-infra/typeid-sentinels-and-anchors.md#idiom-1-static-pointer-identity-sentinel) documents the upstream-MLIR sentinel idiom that the cute kind discriminator mirrors at the dialect level. [Layout Algebra and Descriptor Grammar — Layout Primitives](layout-algebra-and-descriptor-grammar.md#layout-primitives) covers the layout primitives whose Types carry these sentinels. [Atom Builders and Desugar — Per-Atom Desugar Rewrites](atom-builders-and-desugar.md#per-atom-desugar-rewrites) covers the desugar pass whose rewrites the verifiers run against. [Tile and Divide Ops — Tiled partition verifier](tile-and-divide-ops.md#tiled-partition-verifier) documents the higher-level tile partitioning ops whose verifiers reuse the rank, mode, and coordinate gates listed here.
