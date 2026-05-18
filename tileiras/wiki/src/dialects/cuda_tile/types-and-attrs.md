# cuda_tile Types and Attributes

## Abstract

`cuda_tile` types are the public shape and memory vocabulary of TileIR: tile
values, typed pointers, tensor views, partitioned views, and ordering tokens.
Attributes layer on the numeric, memory, target, padding, assumption,
optimization, and debug facts that make lowering deterministic. This page lays
out those contracts in the terms a frontend or reimplementation needs — which
values may be constructed, which attributes are semantic, and which facts are
verified before the module enters private lowering.

## Concrete Types

| Type | Meaning | Contract |
|---|---|---|
| `cuda_tile.tile` | Static shaped value with an element type. | Rank and dimensions are part of the type; dimensions are positive powers of two; total element count is bounded. |
| `cuda_tile.ptr` | Typed pointer to a numeric scalar element. | Pointee is integer or floating; pointer-to-pointer is rejected. |
| `cuda_tile.tensor_view` | Global-memory view with element type, shape, and stride. | Shape and stride ranks match; static dimensions and strides are positive. |
| `cuda_tile.partition_view` | Tile partition over a tensor view. | Tile shape, tensor view, dimension map, and optional padding describe one legal tiled access pattern. |
| `cuda_tile.token` | Memory-ordering edge. | Carries dependency ordering between token-ordered memory effects and has no user-visible payload. |
| `cuda_tile.string` | Implementation-specific string handle. | Treat as nonportable unless the target contract explicitly accepts it. |

The first five types form the stable public surface. `cuda_tile.string` is
useful for reading this build's dumps; portable producers must not depend on
it.

## TypeStorage and TypeID Singletons

Each of the six concrete types is a normal MLIR `Type` subclass backed by its
own `TypeStorage` derivative. Construction flows through the MLIRContext's
`StorageUniquer` gateway documented in
[Storage Uniquer and Context Impl — getOrCreate Gateway](../../mlir-infra/storage-uniquer-and-context-impl.md#the-getorcreate-gateway):
the dialect's self-registration ctor hands a `TypeID` singleton and a build
hook to the uniquer, which hashes the storage payload, looks up an existing
instance, and either returns the cached pointer or allocates a fresh storage
block in the context arena. Every public `Type` handle in the IR is a
24-bit-tagged pointer into that arena.

All six types share a 24-byte `BaseStorage` header (vtable, context pointer,
hash-bucket pointer) and then append a type-specific payload. Storage sizes
are byte-exact across the build and stable under bytecode round-trip.

| Type | TypeID singleton | Storage size | Self-ctor |
|---|---|---:|---|
| `cuda_tile.tile` | `&unk_5B38BC0` | 0x30 | `sub_6C5870` |
| `cuda_tile.tensor_view` | `&unk_5B38BB8` | 0x40 | `sub_6C5C40` |
| `cuda_tile.partition_view` | `&unk_5B38BB0` | 0x40 | `sub_6C5E80` |
| `cuda_tile.ptr` | `&unk_5B38BC8` | 0x20 | `sub_6C5630` |
| `cuda_tile.token` | `off_5A2E208` slot | 0x18 | `sub_6C6240` |
| `cuda_tile.string` | `off_5A2DB38` slot | 0x18 | `sub_6C6500` |

The PointerType singleton `&unk_5B38BC8` is the exact value the
`TileElementType` predicate (`sub_6C4E20`) tests against in its final arm when
it accepts a typed pointer as a tile element — the registration ctor's TypeID
slot is observably the same one driving tile-element verification.

### TileType storage

`cuda_tile.tile` carries a static shape and an element type. The shape is held
as an `ArrayRef<int64_t>` (begin pointer plus size, 16 bytes); the element
type is a single tagged pointer.

```c
typedef struct TileTypeStorage {
    /*+0x00*/ BaseStorage    base;          // vtable=&unk_5B38BC0, ctx, hash bucket
    /*+0x18*/ const int64_t *shape_begin;   // pointer into context-owned int64 array
    /*+0x20*/ uint64_t       shape_size;    // dimension count
    /*+0x28*/ Type           element_type;  // scalar element or cuda_tile.ptr
} TileTypeStorage;
```

The shape array is interned alongside the storage block, and copies returned
to callers re-use that pointer. The element type field accepts the
`TileElementType` palette — f16, bf16, f32, tf32, f64, f8E4M3FN, f8E5M2, i1,
i8, i16, i32, i64, and `cuda_tile.ptr`. Total storage 0x30 bytes.

### TensorViewType storage

`cuda_tile.tensor_view` carries an element type, a shape, and a stride. Both
shape and stride are full `ArrayRef<int64_t>` records, and both accept the
shared MLIR `kDynamic = INT64_MIN` sentinel independently per dimension.

```c
typedef struct TensorViewTypeStorage {
    /*+0x00*/ BaseStorage    base;          // vtable=&unk_5B38BB8
    /*+0x18*/ Type           element_type;
    /*+0x20*/ const int64_t *shape_begin;
    /*+0x28*/ uint64_t       shape_size;
    /*+0x30*/ const int64_t *stride_begin;
    /*+0x38*/ uint64_t       stride_size;
} TensorViewTypeStorage;
```

Element type must satisfy the bare-`Number` predicate (`sub_6C2A10`): the
tile-element palette minus the `cuda_tile.ptr` arm. Total storage 0x40 bytes.

### PartitionViewType storage

`cuda_tile.partition_view` overlays a power-of-two tile grid on a tensor view
and optionally selects a padding value for out-of-bounds reads. The tile shape
is held as a `DenseI32ArrayAttr` attribute pointer (interned independently by
the attribute uniquer); the dimension map is a raw `ArrayRef<int32_t>`; the
padding value is a nullable attribute slot.

```c
typedef struct PartitionViewTypeStorage {
    /*+0x00*/ BaseStorage         base;           // vtable=&unk_5B38BB0
    /*+0x18*/ DenseI32ArrayAttr   tile_shape;     // interned attr
    /*+0x20*/ TensorViewType      tensor_view;
    /*+0x28*/ const int32_t      *dim_map_begin;
    /*+0x30*/ uint64_t            dim_map_size;
    /*+0x38*/ PaddingValueAttr    padding_value;  // nullable, attr pointer
} PartitionViewTypeStorage;
```

At registration time, the self-registration ctor also wires the `TileView`
interface concept-model pointer and method table into the `TypeStorage+0x88`
slot. Op verifiers such as `verifyViewLoadStoreCommon` reach the partition
view through that interface to read `getViewIndexRank()` and
`getViewTileType()`, so the interface vtable is consulted on every view load
and store. Total storage 0x40 bytes.

### PointerType storage

`cuda_tile.ptr` is a typed pointer to a single scalar element. This build
carries no explicit address-space field: the pointer is always a global-memory
typed reference, and any address-space variation rides on the `tensor_view` or
partitioned access it flows through, not on the pointer type itself.

```c
typedef struct PointerTypeStorage {
    /*+0x00*/ BaseStorage    base;          // vtable=&unk_5B38BC8
    /*+0x18*/ Type           pointee_type;  // bare-Number palette only
} PointerTypeStorage;
```

The pointee type must satisfy the bare-`Number` predicate
(`sub_6C2840`); pointer-to-pointer is forbidden by that arm. Total storage
0x20 bytes.

### TokenType storage

`cuda_tile.token` is parameter-free. It carries no payload beyond the shared
`BaseStorage` header — its only job is to thread ordering edges between
token-producing and token-consuming memory operations.

```c
typedef struct TokenTypeStorage {
    /*+0x00*/ BaseStorage    base;          // vtable from off_5A2E208 slot
} TokenTypeStorage;
```

Total storage 0x18 bytes. Two `cuda_tile.token` SSA values produced by
different ops are unequal IR values, but their storage instance is unique —
every `!cuda_tile.token` type in a context resolves to the same `TypeStorage`.

### StringType storage

`cuda_tile.string` is also parameter-free at the storage layer in this build.
It is an internal handle used by debug and diagnostic plumbing; public
producers should treat it as nonportable.

```c
typedef struct StringTypeStorage {
    /*+0x00*/ BaseStorage    base;          // vtable from off_5A2DB38 slot
} StringTypeStorage;
```

Total storage 0x18 bytes. Like `cuda_tile.token`, the type resolves to a
single canonical storage instance per context.

### Element-type dispatch and the 11-arm predicate

The TileType self-registration ctor at `sub_6C5870` wires the
`TileElementType` predicate at `sub_6C4E20` into the parameter-trait verifier
emitted by TableGen. That predicate is an unrolled `AnyTypeOf<>` switch over
the element-type singleton table — each arm a direct vtable-pointer test or
a width-keyed `isInteger(N)` call. The accepted set has thirteen arms in the
binary (f16, bf16, f32, tf32, f64, f8E4M3FN, f8E5M2, i1, i8, i16, i32, i64,
and `cuda_tile.ptr`) and emits the verbatim failure string
`failed to verify 'elementType': f16 or bf16 or f32 or tf32 or f64 or
f8E4M3FN or f8E5M2 or i1 or i8 or i16 or i32 or i64 or Pointer type` on
mismatch. The bare-`Number` predicate at `sub_6C2A10` is the same dispatch
table minus the final `cuda_tile.ptr` arm — which is how the verifier forces
`tensor_view` element types to be scalar while still letting pointer elements
live inside tiles.

### Dynamic-dimension sentinel and tile cap

Both `tensor_view` shape/stride and the inner check in
`PartitionViewType::verify` use the MLIR-wide `kDynamic = INT64_MIN` sentinel
to mark an unknown-at-IR-build-time dimension; the parser accepts `?` and
stores `INT64_MIN`. The tile element-count cap is `0x1000000 = 16777216`
elements, enforced by the overflow-safe `numElems > kMaxElems / dim` check in
`verifyTileSize` before each multiplication. Both constants are part of the
storage-level contract: a reimplementation that picks a different sentinel
collides with the positivity check in shape verification, and a looser tile
cap admits tiles that exceed shared-memory capacity on Blackwell.

## Tile Type

Tiles are shaped SSA values. Shape is static; element type is one of the
accepted integer, floating, or pointer element types. The verifier is simple
by design — every dimension must be a positive power of two, and the product
must stay under the compiler's tile-size ceiling.

```c
LogicalResult verify_tile_type(Shape shape, ElementType element) {
    require(is_tile_element_type(element));

    int64_t elements = 1;
    int64_t max_elements = 16 * 1024 * 1024;

    for (int64_t dim : shape) {
        require(dim > 0);
        require(is_power_of_two(dim));
        require(elements <= max_elements / dim);
        elements *= dim;
    }

    return success();
}
```

That strong shape rule pays off downstream. Tile lowerings routinely assume
powers of two when picking warp lanes, vector widths, and layout factors, and
the verifier guarantees those assumptions never fail.

## Pointer and View Types

`cuda_tile.ptr` is a typed pointer to a numeric element. The pointer itself is
not a tensor; tensor structure is introduced by `tensor_view` and
`partition_view`.

```c
LogicalResult verify_pointer_type(ElementType pointee) {
    require(is_integer_type(pointee) || is_float_type(pointee));
    require(!is_pointer_type(pointee));
    return success();
}
```

`tensor_view` stores element type, rank, shape, and stride. Dynamic dimensions
and strides are allowed, but the rank is fixed.

```c
LogicalResult verify_tensor_view(Type element, Shape shape, Strides strides) {
    require(is_numeric_type(element));
    require(shape.rank == strides.rank);

    for (int axis = 0; axis < shape.rank; ++axis) {
        require(shape[axis] == dynamic_dim() || shape[axis] > 0);
        require(strides[axis] == dynamic_stride() || strides[axis] > 0);
    }

    return success();
}
```

`partition_view` describes how a tile-shaped access maps onto a tensor view. It
is where padding legality and dimension mapping are checked.

```c
LogicalResult verify_partition_view(PartitionViewType view) {
    TensorViewType tensor = view.tensor;
    Shape tile_shape = view.tile_shape;

    require(tile_shape.rank == tensor.rank);
    require(view.dim_map.length == tile_shape.rank);

    BitSet used_tensor_dims(tensor.rank);
    for (int tile_axis = 0; tile_axis < tile_shape.rank; ++tile_axis) {
        int tensor_axis = view.dim_map[tile_axis];

        require(tile_shape[tile_axis] > 0);
        require(is_power_of_two(tile_shape[tile_axis]));
        require(0 <= tensor_axis && tensor_axis < tensor.rank);
        require(!used_tensor_dims.contains(tensor_axis));

        used_tensor_dims.insert(tensor_axis);
    }

    if (view.padding.has_value && view.padding.value.requires_float()) {
        require(is_float_type(tensor.element_type));
    }

    return success();
}
```

## Element-Type Palette

The public element palette includes the integer widths `i1`, `i8`, `i16`,
`i32`, and `i64`; the floating types `f16`, `bf16`, `f32`, `tf32`, and `f64`;
and the FP8 formats used by current tile operations. Lower-precision FP4, FP6,
and block-scale helper formats are introduced in lower internal dialects rather
than as general `cuda_tile` element types.

| Predicate family | Accepted types | Typical users |
|---|---|---|
| Any integer | `i1`, `i8`, `i16`, `i32`, `i64` | Integer arithmetic, logic, indices, predicates. |
| Any float | `f16`, `bf16`, `f32`, `tf32`, `f64`, FP8 formats | Floating arithmetic, MMA, conversion, padding. |
| Numeric | Integers or floats | Pointers, tensor views, constants. |
| Tile element | Numeric or pointer | Tile values and pointer tiles. |
| Pointer tile | Tile whose element is `cuda_tile.ptr` | Gather, scatter, and pointer-tile memory forms. |

## Attribute Families

| Family | Attributes | Contract |
|---|---|---|
| Integer mode | `signedness`, `overflow` | Select signed or unsigned interpretation and optional overflow assumptions. |
| Floating mode | `rounding`, `comparison_ordering`, `comparison_predicate` | Preserve rounding and ordered or unordered comparison semantics. |
| Atomic and memory model | `atomic_rmw_mode`, `memory_scope`, `memory_ordering_semantics` | Define legal atomic operation, visibility scope, and ordering semantics. |
| Padding | `padding_value` | Select the fill value for out-of-bounds partitioned view reads. |
| Assumption predicates | `div_by`, `same_elements`, `bounded` | Attach verifier-checked facts to `cuda_tile.assume`. |
| Optimization hints | `optimization_hints` | Carry optional architecture-keyed tuning hints for entries and memory ops. |
| Debug and location | `di_loc`, `di_compile_unit`, `di_file`, `di_lexical_block`, `di_subprogram` | Preserve source provenance when debug info is enabled. |

Parse enum-like attributes as closed sets. Validate data attributes' payload
shape at parse time and, where needed, again in the consuming operation's
verifier.

## Assumption Predicates

`div_by`, `bounded`, and `same_elements` implement the assumption-predicate
contract. They mean anything only when attached to `cuda_tile.assume`. Later
passes can lean on them for simplification — but only because the verifier
type-checks the constrained value first.

```c
LogicalResult verify_assume_predicates(AssumeOp op) {
    Type type = op.value.type;

    for (Attribute attr : op.predicates) {
        switch (attr.kind) {
        case DIV_BY:
            require(attr.divisor > 0);
            require(is_power_of_two(attr.divisor));
            require(is_integer_like(type) || is_pointer_like(type));
            require(optional_pair_is_complete(attr.every, attr.along));
            break;
        case BOUNDED:
            require(is_integer_like(type));
            require_bounds_fit_integer_width(attr.lower, attr.upper, type);
            require_lower_not_greater_than_upper(attr.lower, attr.upper);
            break;
        case SAME_ELEMENTS:
            require(attr.values.length == ranked_shape(type).rank);
            require_each_value_fits_axis(attr.values, ranked_shape(type));
            break;
        default:
            break;
        }
    }

    return success();
}
```

The dispatch above is the public contract. The implementation lives in three per-attribute verifier bodies
that the bytecode reader reaches through a small fan-out of trampolines. The next sections document those
bodies as they appear in the binary — together they cover the only `cuda_tile` attributes that carry a
non-trivial verifier. The remaining attributes in the family table are simple key-value records that the
generic attribute parser accepts without a dedicated `verify` slot.

## DivByAttr Verifier

`DivByAttr` is the divisibility assumption used on `cuda_tile.assume`. Its verifier lives at `sub_15107A0` —
the largest attribute-verifier body in the binary at roughly 1 467 lines of decompiled C, almost all of it
type-universe dispatch and overflow bookkeeping. The symbol-table name reads `DivByAttr::verifyWithAssumeOp`,
and the diagnostics it emits sometimes spell the attribute as `nv_tileaa.div_by` rather than
`cuda_tile.div_by` — the dialect was renamed mid-binary and the diagnostic strings were never refreshed.
Treat both spellings as the same attribute when matching error output.

The verifier opens by checking that the divisor is positive. A non-positive divisor is rejected
immediately; the verifier emits a diagnostic suffixed with the verbatim `"' divisor must be a power of 2"`
phrase (the leading `'` closes the quoted attribute-name prefix the diagnostic prints first). It then
bound-checks the magnitude against `2^62`. The ceiling is chosen so the divisor can be multiplied by a
signed 64-bit residue without overflow during downstream simplification — the primary reason a divisibility
fact gets consulted.

After the magnitude check, the verifier walks the constrained value's type universe. Four branches, all
structural rather than nominal: the dispatch keys on the value's `TypeKind`, not on the printed type name,
so retypings during canonicalization do not change which branch runs.

| Branch | Type-class | Verifier action |
|---|---|---|
| 0 | Integer (any width) | Bound-check divisor against `2^62`; accept any positive integer divisor. |
| 1 | Float (`f16`/`bf16`/`f32`/`tf32`/`f64` and FP8) | Reject — divisibility is not defined for floating point and the attribute is refused with a diagnostic. |
| 2 | Pointer | Bound-check divisor against the pointee element size in bytes; alignment must be a multiple of `sizeof(pointee)`. |
| 3 | Aggregate (`cuda_tile.tile`, `cuda_tile.tensor_view`) | Recurse into the element type; the same dispatch then runs against the element. |

The aggregate branch is what lets `div_by` apply to a tile uniformly: the verifier descends through the tile
type and rechecks the leaf element. A pointer-of-pointer or tile-of-tile terminates in the rejection arm
because each recursion is guarded by the same dispatch.

`DivByAttr` carries two optional covariant fields, `every` and `along`. `every` asserts the fact for every
dimension of a multi-dim divisor; `along` restricts the assertion to a single axis. The two fields obey a
joint-presence contract policed by three verbatim binary diagnostics — `"' 'every'/'along' must be used in combination"`,
`"' 'every'/'along' cannot be used if the constrained value is a tensor_view"`, and
`"' 'every'/'along' cannot be used if the constrained value is a 0D tile"` (each with the leading `'`
closing the quoted attribute-name prefix). When `every` is present on a multi-dim divisor the verifier
requires every dim of the divisor to divide cleanly into the corresponding tile extent; when `along` is
present it checks divisibility only along the named axis and leaves the other axes unconstrained.

## BoundedAttr Verifier

`BoundedAttr` is the integer-range assumption verified at `sub_150EB90`. It runs much shorter than the
divisibility verifier because there is no type-universe walk — bounds only apply to integer-typed values,
and the verifier rejects everything else up front. The primary check is the consistency relation
`lo <= hi`, emitted as a diagnostic when it fails. The verifier also checks that both bounds fit in the
integer width of the constrained value; an out-of-range bound is reported with the offending width and
value.

Three optional fields tune the relation. `min` provides the minimum permitted value and defaults to
`INT64_MIN`; `max` provides the maximum and defaults to `INT64_MAX`; `strict`, when true, switches the
relation from inclusive (`lo <= v <= hi`) to strict (`lo < v < hi`) on both ends. The `strict` flag changes
only the predicate emitted to downstream passes; the verifier itself enforces the same `lo <= hi`
consistency regardless of the flag.

## SameElementsAttr Verifier

`SameElementsAttr` is the splat-form assumption verified at `sub_150D3F0`. It applies to attributes shaped
like `DenseElementsAttr` and asserts that every element of the dense payload equals one canonical value. The
verifier confirms the underlying `DenseElementsAttr` really is splat-form — its dense storage collapses to a
single value — and then stores only that canonical value rather than the full payload. The optimizer reads
the stored canonical value to fold splat-multiply-x patterns into element-multiply-x, which is the main
reason the attribute exists.

A non-splat payload is rejected outright. There is no per-element scan in the verifier itself; the splat
check is a constant-time query on the dense attribute's internal layout.

## Verifier Trampolines

The bytecode reader does not call the three verifier bodies directly. Each one sits behind a 64-byte
trampoline that the reader installs as the attribute kind's `verify` slot. The trampolines at `sub_1517B70`,
`sub_1517B90`, and `sub_1517BB0` are byte-identical apart from the inner call target — they dispatch to
`DivByAttr::verifyWithAssumeOp` at `sub_15107A0`, `BoundedAttr` at `sub_150EB90`, and `SameElementsAttr` at
`sub_150D3F0` respectively. The thunks exist because the bytecode reader stores a uniform function pointer
in each attribute's vtable slot, and each trampoline adapts the generic call signature to its verifier's
specific argument layout.

## Optimization Hints

`optimization_hints` is a dictionary keyed by architecture name, then by
operation-specific hint name. The contents are advisory but still verified:
unknown architectures and unknown keys must be rejected so producers never
think a hint was honored when it was actually ignored.

```c
LogicalResult verify_optimization_hints(Operation op, DictAttr hints) {
    for (NamedDict arch_entry : hints.entries) {
        require(is_allowed_architecture_key(arch_entry.name));

        for (NamedAttribute hint : arch_entry.value.entries) {
            require(is_allowed_hint_for_operation(op.name, hint.name));
            require(hint_value_has_expected_type(op.name, hint.name, hint.value));
        }
    }

    return success();
}
```

Common hint concepts include occupancy, CTA clustering, latency, and whether
TMA is allowed for a view load or store. A missing hint means the compiler is
free to choose.

## Invariants

- Tile dimensions are static, positive powers of two.
- Pointer pointees are numeric, never pointers.
- Tensor views have matching shape and stride ranks.
- Partition views map tile dimensions injectively into tensor dimensions.
- Special padding values such as NaN or infinity require floating-point element
  types.
- Tokens are ordering values, not runtime data visible to the program.
- Assumption predicates are verifier-checked before they can justify a rewrite.
- Optimization hints must be explicit and known to the verifier.

