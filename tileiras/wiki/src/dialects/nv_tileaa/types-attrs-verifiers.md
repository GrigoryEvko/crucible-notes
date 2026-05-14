# nv_tileaa Types, Attributes, Verifiers

## Abstract

`nv_tileaa` carries just enough type and attribute structure to make alias,
memory, layout, and target facts explicit between `cuda_tile` and `nv_tileas`.
The type system covers pointer-like values, queues, memrefs, tiled views,
program identifiers, and memory tokens. The attribute system covers target
capability, memory policy, atomic mode, arithmetic rounding, convolution
layout, and assumption predicates. Verification is deliberately concentrated
in the few places where a wrong fact would make later scheduling unsound.

The public contract below is what a reimplementation must preserve. It does
not lean on any one binary's registration addresses or decompiler names.

## Type Surface

| Type | Purpose | Reimplementation contract |
|---|---|---|
| `nv_tileaa.ptr` | Pointer value with element type and memory space. | Preserve address space and provenance through casts and `addptr`. |
| `nv_tileaa.program_id` | Grid program index value. | Treat as an opaque index returned by grid-query operations. |
| `nv_tileaa.queue` | Typed queue handle for producer and consumer regions. | Carry result types and queue isolation flags until TileAS pipeline lowering. |
| `nv_tileaa.memref` | Strided memory reference. | Store base pointer, offset, sizes, strides, element type, memory space, and alias scope. |
| `nv_tileaa.tiled_view` | Tile-shaped view over a value or memory object. | Preserve shape, layout, and element type without implying a memory effect. |
| `nv_tileaa.mem_token` | Memory-order token. | Represent ordering only; no user-visible payload is attached to the token. |

`memref` and `tiled_view` are the structural types that matter most. `memref`
answers "where is this data and how is it strided?"; `tiled_view` answers
"how should tile-level computation interpret this value?" Keeping those two
questions separate lets layout assignment swap a view without rewriting the
underlying pointer provenance.

```c
typedef struct {
    Pointer base;
    Index offset;
    Shape sizes;
    Strides strides;
    ElementType element_type;
    MemorySpace memory_space;
    AliasScope alias_scope;
} TileAAMemRef;

typedef struct {
    Value source;
    Shape shape;
    Layout layout;
    ElementType element_type;
} TileAATiledView;
```

### TypeStorage Layouts

Every `nv_tileaa` type is a normal MLIR `Type` subclass backed by its own
`TypeStorage` derivative, routed through the context `StorageUniquer`
documented in [Storage Uniquer and Context Impl](../../mlir-infra/storage-uniquer-and-context-impl.md).
The storage layouts below are the byte-exact records used for hashing,
equality, and round-trip — all part of the reimplementation contract.

| Type | TypeID singleton | Storage size | Uniquer key |
|---|---|---:|---|
| `nv_tileaa.ptr` | dialect TypeID slot | 0x20 | `(pointee, address_space)` |
| `nv_tileaa.program_id` | dialect TypeID slot | 0x18 | parameterless |
| `nv_tileaa.queue` | dialect TypeID slot | 0x28 | `(result_types ArrayRef<Type>, isolated_flag)` |
| `nv_tileaa.memref` | dialect TypeID slot | 0x48 | `(element, shape, stride, addrspace, alias_scope)` |
| `nv_tileaa.tiled_view` | dialect TypeID slot | 0x30 | `(source_type, shape, layout)` |
| `nv_tileaa.mem_token` | `&unk_5B46F78` | 0x18 | parameterless |

```c
typedef struct PtrStorage {
    /*+0x00*/ BaseStorage    base;             // vtable, ctx, hash bucket
    /*+0x18*/ Type           pointee_type;
    /*+0x20*/ uint32_t       address_space;
} PtrStorage;

typedef struct MemRefStorage {
    /*+0x00*/ BaseStorage    base;
    /*+0x18*/ Type           element_type;
    /*+0x20*/ const int64_t *shape_begin;
    /*+0x28*/ uint64_t       shape_size;
    /*+0x30*/ const int64_t *stride_begin;
    /*+0x38*/ uint64_t       stride_size;
    /*+0x40*/ uint32_t       address_space;
    /*+0x44*/ uint32_t       alias_scope_id;
} MemRefStorage;

typedef struct TiledViewStorage {
    /*+0x00*/ BaseStorage    base;
    /*+0x18*/ Type           source_type;
    /*+0x20*/ const int64_t *shape_begin;
    /*+0x28*/ uint64_t       shape_size;
    /*+0x30*/ Attribute      layout;           // LayoutAttr handle, interned
} TiledViewStorage;

typedef struct QueueStorage {
    /*+0x00*/ BaseStorage    base;
    /*+0x18*/ const Type    *result_types_begin;
    /*+0x20*/ uint64_t       result_types_size;
    /*+0x28*/ bool           isolated;
} QueueStorage;

typedef struct MemTokenStorage {
    /*+0x00*/ BaseStorage    base;             // vtable from &unk_5B46F78
} MemTokenStorage;
```

Shape, stride, and result-type arrays are interned alongside the storage
block; copies returned to callers re-use that pointer. The shared
`BaseStorage` header is 24 bytes (vtable, MLIRContext pointer, hash bucket
pointer); each derivative appends only its semantic payload. Pointer identity
on the resulting `Type*` is the dispatch key every walker and type converter
in the cascade consumes, so a reimplementation must intern through one
`StorageUniquer` per context rather than allocating fresh storage per call
site.

## Attribute Surface

The dialect has eighteen logical attributes plus a legacy spelling of
`compute_capability` that exists for compatibility with older text and bytecode
producers. For a user or reimplementer, the useful grouping is:

| Group | Attributes | Meaning |
|---|---|---|
| Target and kernel configuration | `compute_capability`, `compute-capability`, `target_spec`, `kernel_spec` | Select architectural features, launch shape, and kernel-level policy. |
| Memory policy | `cache_modifier`, `eviction_policy`, `mem_semantic`, `mem_scope` | Annotate loads, stores, and atomics with cache, eviction, ordering, and scope facts. |
| Atomic and arithmetic modes | `rmw_mode`, `rounding_mode`, `propagate_nan`, `signedness` | Select atomic operation, floating rounding, NaN behavior, and integer MMA signedness. |
| Convolution and layout | `padding_value`, `activation_layout`, `conv_params` | Preserve convolution padding, activation order, and structured convolution parameters. |
| Assumption predicates | `div_by`, `bounded`, `same_elements` | Attach verifier-checked facts to `assume` so later passes can simplify safely. |

Most attributes are enum-like or data containers. Parsing validates their
spelling and payload; the consuming op's verifier runs a second pass when it
matters. The three assumption predicates are the exception — they implement a
runtime verification contract against the value constrained by
`nv_tileaa.assume`.

## Assumption Predicate Verification

`nv_tileaa.assume` accepts a value and a list of predicate attributes. During
verification, each predicate that implements the assumption interface checks
the value's type and its own parameters. The first failing predicate emits
the diagnostic; later predicates never run.

```c
LogicalResult verify_assume(AssumeOp op) {
    Type constrained_type = op.value.type;

    for (Attribute predicate : op.predicates) {
        AssumePredicate verifier = dyn_cast_assume_predicate(predicate);
        if (verifier == NULL) {
            continue;
        }

        if (failed(verifier.verify_with_assume_op(predicate, constrained_type, op))) {
            return failure();
        }
    }

    return success();
}
```

### `div_by`

`div_by` states that every constrained element is divisible by a positive
power-of-two divisor. Optional `every` and `along` fields refine the statement
to a periodic subset of an axis; they must appear together.

```c
LogicalResult verify_div_by(DivByAttr attr, Type type) {
    require(is_integer_like(type) || is_pointer_like(type) || is_memref_like(type));
    require(attr.divisor > 0);
    require(is_power_of_two(attr.divisor));

    bool has_every = attr.every.has_value;
    bool has_along = attr.along.has_value;
    require(has_every == has_along);

    if (has_every) {
        require(attr.every.value > 0);
        require(axis_is_valid(type, attr.along.value));
    }

    return success();
}
```

### `bounded`

`bounded` states that the constrained integer-like value falls within an
inclusive range. Bounds are interpreted using the constrained element width, so
the verifier must check both the order and the representable range.

```c
LogicalResult verify_bounded(BoundedAttr attr, Type type) {
    ElementType element = integer_element_type(type);
    require(element.is_integer);

    IntegerRange range = signed_integer_range(element.bit_width);

    if (attr.lower.has_value) {
        require(range.contains(attr.lower.value));
    }

    if (attr.upper.has_value) {
        require(range.contains(attr.upper.value));
    }

    if (attr.lower.has_value && attr.upper.has_value) {
        require(attr.lower.value <= attr.upper.value);
    }

    return success();
}
```

### `same_elements`

`same_elements` records a shape fact: each listed axis must have exactly the
specified extent. The attribute earns its keep after rank-changing
canonicalization, when a later pass needs to prove that two views still cover
the same logical tile.

```c
LogicalResult verify_same_elements(SameElementsAttr attr, Type type) {
    Shape shape = ranked_shape(type);
    require(attr.values.length == shape.rank);

    for (int axis = 0; axis < shape.rank; ++axis) {
        require(attr.values[axis] >= 0);
        require(attr.values[axis] <= shape.dim(axis));
    }

    return success();
}
```

## Operation-Level Verifiers

Most operations rely on generic trait checks — operand counts, result counts,
region count, type equality, terminator shape. These operations need
domain-specific verification on top:

| Operation area | Required checks |
|---|---|
| `dot` and block-scaled MMA | Operand element-type tuple, accumulator type, signedness attributes, scale-factor operands, target capability. |
| `conv_dot`, `conv_tile`, `block_tile` | Convolution rank, padding shape, activation layout, tile blocking, result rank. |
| `fp_to_fp` | Source and destination are supported floating formats; block-scaled auxiliary formats appear only in legal contexts. |
| `func`, `call`, `return` | Function type, symbol name, call result types, argument and result attribute dictionaries. |
| `yield` and `queue.yield` | Terminator operands match the parent region's expected yielded values. |
| `execute` and `plugin` | Resource requirements, warp or agent counts, symbol references, and layout metadata. |
| `load`, `store`, tiled memory ops | Segment sizes, mask and fallback value shape, token result presence, bounds attributes. |
| `assume` | Predicate attributes satisfy the interface contracts above. |

```c
LogicalResult verify_tileaa_operation(Operation op, Target target) {
    require_generic_mlir_traits(op);

    switch (op.kind) {
    case DOT:
        return verify_dot(cast_dot(op), target);
    case FP_TO_FP:
        return verify_float_conversion(cast_fp_to_fp(op), target);
    case FUNC:
        return verify_function_contract(cast_func(op));
    case EXECUTE:
        return verify_execute_contract(cast_execute(op), target);
    case YIELD:
    case QUEUE_YIELD:
        return verify_parent_yield_contract(op);
    case ASSUME:
        return verify_assume(cast_assume(op));
    default:
        return success();
    }
}
```

## Element-Type Contract

The dialect reuses the ordinary MLIR integer and floating families and adds
the low-precision formats needed by FP8, FP4, and block-scaled MMA. Model
these as a finite legality table, not as ad hoc string tests. The exact
storage class doesn't matter; the table below is the behavioral contract.

| Element family | Typical use |
|---|---|
| `f16`, `bf16`, `tf32`, `f32` | Standard MMA input and accumulator paths. |
| FP8 E4M3 and E5M2 formats | Low-precision MMA inputs and conversion targets. |
| E8M0 scale factors | Block-scaled MMA scale-factor operands. |
| FP4 and NVIDIA FP4 variants | Blackwell-era block-scaled MMA input paths. |
| Integer widths | Integer MMA, pointer arithmetic, predicates, and indices. |

```c
LogicalResult verify_float_conversion(FpToFpOp op, Target target) {
    require(is_supported_float_element(op.source.element_type));
    require(is_supported_float_element(op.result.element_type));

    if (uses_block_scaled_format(op.source) || uses_block_scaled_format(op.result)) {
        require(target.supports_block_scaled_mma);
    }

    return success();
}
```

## Invariants

- `compute-capability` and `compute_capability` should parse to one logical
  target-capability concept; emit the canonical underscore spelling in new IR.
- Enum-like attributes are validated by parser tables and by consuming ops.
- `div_by`, `bounded`, and `same_elements` are meaningful only through
  `nv_tileaa.assume`.
- Memory-policy attributes do not create ordering by themselves; tokens and
  memory effects do.
- Low-precision element formats are target-gated where the hardware requires
  it.
- Function, plugin, and queue attributes must remain structured until their
  symbols and resource requirements have been resolved.

