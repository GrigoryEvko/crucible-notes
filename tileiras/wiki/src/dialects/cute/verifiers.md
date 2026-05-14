# cute Verifiers

## Abstract

The `cute` verifier surface guards layout algebra. It checks that shapes, coordinates, layouts, composed layouts, views, tuples, divide/product operands, memrefs, atom fragments, and tuple arithmetic stay compatible before lowering picks a target instruction. The mental model is short: verifiers guard kind, rank, congruence, staticness, and algebraic validity.

## Verification Categories

| Category | Examples | Main checks |
|---|---|---|
| Layout builders | `make_layout`, `make_shape`, `make_stride`, `make_composed_layout` | Operand kind, shape/stride congruence, composed layout validity. |
| Layout queries | `get_shape`, `get_stride`, `get_layout`, composed-layout getters | Input implements the required layout, tile, view, or composed-layout interface. |
| Algebra | `composition`, `complement`, `right_inverse`, `left_inverse`, `coalesce`, `filter` | Staticness where required, valid domains, successful algebraic construction. |
| Divide/product | `logical_divide`, `tiled_divide`, `flat_divide`, products | Layout-like operands, legal tiler, rank relation, successful regrouping. |
| Tuple arithmetic | `tuple_div`, `tuple_mod`, `tuple_mul`, `tuple_sub`, `tuple.product` | Same tuple kind, supported arithmetic leaves, no divide or modulo by zero. |
| Coordinates and slicing | `crd2idx`, `dice`, `slice`, `local_tile`, `local_partition` | Coordinate kind, weak congruence, valid mode range, valid target profile. |
| Memory and descriptors | `memref.load`, `load_scaled_index`, descriptor iterators | Supported element type, bit width, address space, coordinate/layout congruence. |
| Atoms and fragments | `make_tiled_copy`, `make_tiled_mma`, `mma.make_fragment` | Atom type, operand role, vector mode, profile compatibility, inferred result type. |

## Layout Builder Checks

```c
LogicalResult verify_make_composed_layout(MakeComposedLayoutOp op) {
    require(implements_layout_interface(op.outer));
    require(is_int_tuple(op.offset));
    require(implements_layout_interface(op.inner) || is_swizzle(op.inner));

    Optional<Layout> layout = compose_layout(op.outer, op.inner);
    require(layout.has_value);
    require(offset_is_valid_for_layout(op.offset, layout.value));
    return success();
}
```

`make_layout` and `make_ordered_layout` check congruence rather than computing a full composed layout. `make_identity_layout` and `make_identity_tensor` accept shape-like operands and reject anything that cannot produce a valid identity coordinate map.

## Mode and Rank Checks

Many `cute` operations accept Python-style mode ranges. Negative modes are
normalized relative to rank, then checked.

```c
LogicalResult verify_mode_range(int begin, int end, int rank) {
    int normalized_begin = begin < 0 ? begin + rank : begin;
    int normalized_end = end < 0 ? end + rank : end;

    require(0 <= normalized_begin && normalized_begin < rank);
    require(0 <= normalized_end && normalized_end <= rank);
    require(normalized_begin < normalized_end);
    return success();
}
```

`select` and similar mode-list operations also reject out-of-range modes and
duplicates.

```c
LogicalResult verify_mode_list(ArrayRef<int32_t> modes, int rank) {
    BitSet seen(rank);

    for (int32_t mode : modes) {
        require(0 <= mode && mode < rank);
        require(!seen.contains(mode));
        seen.insert(mode);
    }

    return success();
}
```

## Divide and Product Checks

Divide requires a layout-like input and a tile-like tiler, with tiler rank at most input rank. Product requires layout-like operands. Both families actually run the algebraic operation during verification so an invalid regrouping fails early instead of slipping into lowering.

```c
LogicalResult verify_divide(DivideOp op) {
    require(is_layout_like(op.input) || is_tile_like(op.input));
    require(is_tile_like(op.tiler) || is_layout_like(op.tiler));
    require(rank(op.tiler) <= rank(op.input));

    Optional<Layout> result = try_divide_and_regroup(op.input, op.tiler, op.mode);
    require(result.has_value);
    require(result.value.type == op.result.type);
    return success();
}

LogicalResult verify_product(ProductOp op) {
    require(is_layout_like(op.lhs));
    require(is_layout_like(op.rhs));

    Optional<Layout> result = try_product_and_regroup(op.lhs, op.rhs, op.mode);
    require(result.has_value);
    require(result.value.type == op.result.type);
    return success();
}
```

## Tuple Arithmetic Checks

Tuple arithmetic is structural. The operands must have the same tuple kind, and
each leaf operation must be defined. Division and modulo reject zero divisors.

```c
LogicalResult verify_tuple_arithmetic(TupleArithOp op) {
    require(implements_value_type_interface(op.lhs));
    require(implements_value_type_interface(op.rhs));
    require(same_tuple_kind(op.lhs.type, op.rhs.type));

    for (LeafPair leaf : zip_leaves(op.lhs, op.rhs)) {
        require(arithmetic_supported_for_leaf(leaf.lhs, leaf.rhs));

        if (op.kind == TUPLE_DIV || op.kind == TUPLE_MOD) {
            require(!is_zero(leaf.rhs));
        }
    }

    return success();
}
```

`to_int_tuple` rejects scaled bases, underscores, error leaves, and non-tuple sources. The conversion is strict because later LLVM lowering expects a plain integer tuple.

## Coordinates, Local Tiles, and Slices

Coordinate-based operations check weak congruence: the coordinate profile must fit the layout or view profile, but it may be less specific where the input has dynamic structure.

```c
LogicalResult verify_local_tile(LocalTileOp op) {
    require(is_tile_like(op.tiler) || is_shape_like(op.tiler));
    require(is_coord(op.coord));
    require(implements_view_type_interface(op.input));
    require(op.mode.length >= rank(op.coord));
    require(weakly_congruent(profile(op.coord), selected_profile(op.input, op.mode)));

    Optional<View> result = dice_view(op.input, op.tiler, op.coord, op.mode);
    require(result.has_value);
    return success();
}
```

`local_partition`, `coalesce`, and `slice` share the same input/profile checks. The input must be a layout or view; any target profile must be a coordinate type compatible with that input.

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

Every `cute` Type carries a kind-discriminator pointer at offset `0x88` of its `TypeStorage` block. The pointer is one of seven static sentinels in `.data.rel.ro`; the sentinel's address — not its contents — is the identity. Walkers, verifiers, builders, parsers, and folders all dispatch on this slot by pointer-identity against the seven-entry table, exactly the same way upstream MLIR dispatches on `TypeID` at `Type+0x00`. The kind slot is repeated at `+0x88` because the upstream slot still carries the `LayoutTypeInterface` interface-id (a Meyers qword), and the cute dialect needs a separate, denser tag for the seven-kind switch.

| Address       | Kind             | Meaning                                                                |
|---------------|------------------|------------------------------------------------------------------------|
| `0x5B49AD8`   | `ComposedLayout` | `compose(L1, L2)` — a layout formed by composing two sub-layouts        |
| `0x5B49AE0`   | `Layout`         | Plain `(Shape, Stride)` pair                                            |
| `0x5B49AE8`   | `Swizzle`        | `swizzle<B, M, S>` — bit-reversal swizzle layout                        |
| `0x5B49AF0`   | `Tile`           | Tile-shape descriptor (shape only, no stride)                           |
| `0x5B49AF8`   | `Shape`          | Pure shape tuple (no stride)                                            |
| `0x5B49B00`   | `Coord`          | Coordinate tuple (no stride)                                            |
| `0x5B49B10`   | `IntTuple`       | Pure-integer tuple                                                      |

The addresses lie inside the `0x5B49AD8..0x5B49B10` band; the gap between `Coord` and `IntTuple` is a 16-byte spacer, not an eighth kind. The bands belong to the `cute` dialect's concrete-type sentinel strand documented in [TypeID Sentinels and Anchors](../../mlir-infra/typeid-sentinels-and-anchors.md), so reimplementations should keep these seven slots dense and aligned.

The `CuteType` storage layout is fixed regardless of which kind a given instance carries. The discriminator slot is always at `+0x88`; everything before it is the upstream-MLIR header and the per-kind payload, and everything after it is per-kind trailing data.

```c
typedef struct CuteType {
    /*+0x00*/ TypeStorage   base;                  /* upstream MLIR TypeStorage (16-B header)        */
    /*+0x18*/ /* per-kind payload */
    /*+0x88*/ const void   *kind_ptr;              /* one of the 7 sentinels above (pointer-identity) */
    /*+0x90*/ /* trailing per-kind data */
} CuteType;
```

Four parallel `.rodata` function-pointer tables index by the kind ordinal — the row position in the seven-entry sentinel list — and each holds one handler per kind. Together they cover the lifecycle of every `cute` Type: verification, asm printing, bytecode parsing, and folding.

| Table          | Address     | Role                                  |
|----------------|-------------|---------------------------------------|
| `funcs_7293D0` | `0x7293D0`  | `verify` — per-kind verifier callback |
| `funcs_74E87D` | `0x74E87D`  | `print` — per-kind asm printer        |
| `funcs_794E2C` | `0x794E2C`  | `parse` — per-kind bytecode reader    |
| `funcs_748D93` | `0x748D93`  | `fold`  — per-kind canonicalization   |

A separate nine-entry operand-kind table at `Type+32` records the expected kind discriminator for each operand slot of multi-operand ops. The arity of nine covers the widest cute op: `cute.partition` consumes up to nine sub-layouts. Narrower ops such as `cute.compose` (two operands) and `cute.zipped_divide` (three) leave the trailing entries unused but read the same table layout, which keeps the verifier-side per-operand checks index-uniform.

Dispatch itself is a linear scan over the seven sentinels followed by an indexed call into the appropriate table. Pointer-identity comparison keeps the inner loop to a `MOV`/`CMP` pair per kind; falling off the end is a hard error because every well-formed `cute` Type must carry one of the seven sentinels.

```c
void *dispatch_by_kind(const CuteType *t, const void **table) {
    static const void *kSentinels[7] = {
        (const void *)0x5B49AD8, /* ComposedLayout */
        (const void *)0x5B49AE0, /* Layout         */
        (const void *)0x5B49AE8, /* Swizzle        */
        (const void *)0x5B49AF0, /* Tile           */
        (const void *)0x5B49AF8, /* Shape          */
        (const void *)0x5B49B00, /* Coord          */
        (const void *)0x5B49B10, /* IntTuple       */
    };
    for (int i = 0; i < 7; ++i) {
        if (t->kind_ptr == kSentinels[i]) {
            return table[i];
        }
    }
    abort();                                       /* unknown kind — should be unreachable */
}
```

A reimplementation should keep the slot at `+0x88`, the kind ordering, and the four-table convention. Moving the slot breaks every dispatcher; reordering the sentinels silently mis-routes verify and fold to the wrong handler because every table is indexed by the same ordinal.

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

[TypeID Sentinels and Anchors](../../mlir-infra/typeid-sentinels-and-anchors.md) documents the upstream-MLIR sentinel idiom that the `Type+0x88` kind discriminator mirrors at the cute level. [cuTe Layout Algebra and Descriptor Grammar](layout-algebra-and-descriptor-grammar.md) covers the layout primitives whose Types carry these sentinels.
