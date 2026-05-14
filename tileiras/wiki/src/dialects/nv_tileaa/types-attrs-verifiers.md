# nv_tileaa Types, Attributes, Verifiers

## Abstract

`nv_tileaa` carries just enough type and attribute structure to make alias, memory, layout, and target facts explicit between `cuda_tile` and `nv_tileas`. The type system covers pointer-like values, queues, memrefs, tiled views, program identifiers, and memory tokens. The attribute system covers target capability, memory policy, atomic mode, arithmetic rounding, convolution layout, and assumption predicates. Verification is concentrated in the few places where a wrong fact would make later scheduling unsound.

The diagnostic strings emitted by the dialect's verifier are part of the producer contract. They are reproduced here verbatim so a reimplementation matches the exact text the binary emits.

## Type Surface

| Type | Purpose |
|---|---|
| `nv_tileaa.ptr` | Pointer value with element type and memory space. |
| `nv_tileaa.program_id` | Grid program index value. |
| `nv_tileaa.queue` | Typed queue handle for producer and consumer regions. |
| `nv_tileaa.memref` | Strided memory reference with base, offset, sizes, strides, element type, memory space, and alias scope. |
| `nv_tileaa.tiled_view` | Tile-shaped view over a value or memory object. |
| `nv_tileaa.mem_token` | Memory-order token; ordering only, no payload. |

`memref` and `tiled_view` are the structural types that matter most. `memref` answers "where is this data and how is it strided?"; `tiled_view` answers "how should tile-level computation interpret this value?" Keeping those two questions separate lets layout assignment swap a view without rewriting the underlying pointer provenance.

```c
typedef struct {
    Pointer       base;
    Index         offset;
    Shape         sizes;
    Strides       strides;
    ElementType   element_type;
    MemorySpace   memory_space;
    AliasScope    alias_scope;
} TileAAMemRef;

typedef struct {
    Value         source;
    Shape         shape;
    Layout        layout;
    ElementType   element_type;
} TileAATiledView;
```

## Type Storage and Uniquing

Every `nv_tileaa` type is a normal MLIR `Type` subclass backed by its own `TypeStorage` derivative routed through the context `StorageUniquer` documented in [Storage Uniquer and Context Impl](../../mlir-infra/storage-uniquer-and-context-impl.md). The uniquer key for each type names the fields the hasher consumes and the equality check compares.

| Type | Uniquer key |
|---|---|
| `nv_tileaa.ptr` | `(pointee_type, address_space)` |
| `nv_tileaa.program_id` | parameterless (single canonical storage per context) |
| `nv_tileaa.queue` | `(result_types: ArrayRef<Type>, isolated_flag: bool)` |
| `nv_tileaa.memref` | `(element_type, shape: ArrayRef<int64_t>, stride: ArrayRef<int64_t>, address_space, alias_scope_id)` |
| `nv_tileaa.tiled_view` | `(source_type, shape, layout_attr)` |
| `nv_tileaa.mem_token` | parameterless |

Shape, stride, and result-type arrays are interned alongside the storage block; copies returned to callers reuse those pointers. Pointer identity on the resulting `Type*` is the dispatch key every walker and type converter in the cascade consumes, so a reimplementation must intern through one `StorageUniquer` per context rather than allocating fresh storage per call site.

## Attribute Surface

The dialect has eighteen logical attributes plus a legacy spelling of `compute_capability` retained for compatibility with older text and bytecode producers.

| Group | Attributes | Meaning |
|---|---|---|
| Target and kernel configuration | `compute_capability`, `compute-capability`, `target_spec`, `kernel_spec` | Select architectural features, launch shape, and kernel-level policy. |
| Memory policy | `cache_modifier`, `eviction_policy`, `mem_semantic`, `mem_scope` | Annotate loads, stores, and atomics with cache, eviction, ordering, and scope facts. |
| Atomic and arithmetic modes | `rmw_mode`, `rounding_mode`, `propagate_nan`, `signedness` | Select atomic operation, floating rounding, NaN behavior, and integer MMA signedness. |
| Convolution and layout | `padding_value`, `activation_layout`, `conv_params` | Preserve convolution padding, activation order, and structured convolution parameters. |
| Assumption predicates | `div_by`, `bounded`, `same_elements` | Attach verifier-checked facts to `assume` so later passes can simplify safely. |

Most attributes are enum-like or data containers. Parsing validates their spelling and payload; the consuming op's verifier runs a second pass when it matters. The three assumption predicates implement a runtime verification interface against the value constrained by `nv_tileaa.assume`.

## Dot and Block-Scaled MMA Diagnostics

`nv_tileaa.dot` is the densest verifier in the dialect. It runs five phase checks against the A/B/C element-type tuple, the optional `sfa`/`sfb` scale-factor operands, the integer signedness attributes, the operand ranks, and the result shape. Each phase emits a specific diagnostic.

```c
LogicalResult verify_dot(DotOp op, Target target) {
    bool all_int   = is_integer(op.a.elem_t) && is_integer(op.b.elem_t)
                     && is_integer(op.c.elem_t) && is_integer(op.d.elem_t);
    bool all_float = is_float(op.a.elem_t) && is_float(op.b.elem_t)
                     && is_float(op.c.elem_t) && is_float(op.d.elem_t);
    if (!all_int && !all_float) {
        return op.emit_error("expected the element types of A, B, C, and D to be either all integers or all floats.");
    }

    if (all_int) {
        if (bit_width(op.a.elem_t) != bit_width(op.b.elem_t)) {
            return op.emit_error("expects #A and #B have same bit width but got ")
                .append(bit_width(op.a.elem_t)).append(" vs ").append(bit_width(op.b.elem_t));
        }
        if (!op.has_signedness_a()) {
            return op.emit_error("expect signedness attribute for operand A");
        }
    }

    if (all_float) {
        if (is_fp4(op.a.elem_t)) {
            if (op.c.elem_t != f32_type() && op.c.elem_t != f16_type()) {
                return op.emit_error("expects #C element type to be either f32 or f16, but got ")
                    .append(op.c.elem_t);
            }
        } else if (op.c.elem_t != f32_type()) {
            return op.emit_error("expects #C element type to be f32, but got ").append(op.c.elem_t);
        }
    }

    int rank = op.d.rank;
    if (rank != 2 && rank != 3) {
        return op.emit_error("expects rank-2 or rank-3 tensor for result, but got (")
            .append(rank).append(")");
    }
    if (!shapes_compatible_for_mma(op.a, op.b)) {
        return op.emit_error("expects the shape of operand #A and #B to be compatible");
    }
    if (!shapes_compatible_for_acc(op.a, op.b, op.c)) {
        return op.emit_error("expects the shape of operand #C is compatible with operands #A and #B");
    }

    if (op.has_sfa || op.has_sfb) {
        if (!op.has_sfa || !op.has_sfb) {
            return op.emit_error("expects both SFA and SFB to be present");
        }
    }
    return success();
}
```

The diagnostics this routine emits:

| Diagnostic | Cause |
|---|---|
| `"expected the element types of A, B, C, and D to be either all integers or all floats."` | A mixed integer/floating tuple was supplied. |
| `"expects #A and #B have same bit width but got "` followed by both widths | Integer MMA inputs at unequal widths. |
| `"expect signedness attribute for operand A"` | Integer MMA without a `signedness_a` attribute. |
| `"expects #C element type to be f32, but got "` followed by the actual type | Floating accumulator is not f32. |
| `"expects #C element type to be either f32 or f16, but got "` followed by the actual type | FP4 inputs with an accumulator that is neither f32 nor f16. |
| `"expects rank-2 or rank-3 tensor for result, but got ("` followed by the actual rank | Result rank outside the legal range. |
| `"expects the shape of operand #A and #B to be compatible"` | Contracting dimensions disagree. |
| `"expects the shape of operand #C is compatible with operands #A and #B"` | Accumulator/result shape disagrees with the M/N derived from A/B. |
| `"expects both SFA and SFB to be present"` | Block-scaled MMA with only one of `sfa`/`sfb`. |

## Convolution and Tile Blocking

`nv_tileaa.block_tile` and `nv_tileaa.conv_tile` preserve convolution structure until the memory layout pass can pick producer and consumer layouts. Their verifier rejects malformed `tileSizes`, `dimGroups`, filter sizes, and convolution parameter tuples.

```c
LogicalResult verify_block_tile(BlockTileOp op) {
    if (op.tile_sizes.empty()) {
        return op.emit_error("expects non-empty tileSizes");
    }
    if (op.dim_groups.empty()) {
        return op.emit_error("expects non-empty dimGroups");
    }
    if (op.tile_sizes.size() != op.dim_groups.size()) {
        return op.emit_error("expects rank of tileSizes be equal to rank of dimGroups, but got ")
            .append(op.tile_sizes.size()).append(" vs ").append(op.dim_groups.size());
    }
    for (int64_t s : op.tile_sizes) {
        if (s <= 0) {
            return op.emit_error("expects all tile size bigger than zero");
        }
    }
    Set<int> seen;
    for (DimGroup g : op.dim_groups) {
        for (int axis : g.dims) {
            if (!seen.insert(axis)) {
                return op.emit_error("expects dim is being tiled only one time, but got ").append(axis);
            }
        }
    }
    return success();
}

LogicalResult verify_conv_tile(ConvTileOp op) {
    for (int s : op.filter_sizes) {
        if (s < 1 || s > 3) {
            return op.emit_error("expects filter size must be in range 1 to 3");
        }
    }
    if (!conv_params_consistent(op.conv_params)) {
        return op.emit_error("expects conv params size matched with each other");
    }
    if (!any_group_contains_channel(op.dim_groups, op.channel_axis)) {
        return op.emit_error("expects channel must be a dimGroup");
    }
    if (!channel_group_is_singleton(op.dim_groups, op.channel_axis)) {
        return op.emit_error("expects channel dimGroup should contain only channel");
    }
    return success();
}
```

The diagnostics:

| Diagnostic | Cause |
|---|---|
| `"expects non-empty tileSizes"` | `tileSizes` attribute is missing or empty. |
| `"expects non-empty dimGroups"` | `dimGroups` attribute is missing or empty. |
| `"expects dim is being tiled only one time, but got "` followed by the duplicated axis | A spatial axis appears in more than one dim group. |
| `"expects rank of tileSizes be equal to rank of dimGroups, but got "` followed by both ranks | Rank disagreement between the two attributes. |
| `"expects all tile size bigger than zero"` | A tile-size entry is zero or negative. |
| `"expects filter size must be in range 1 to 3"` | A convolution filter size falls outside the supported range. |
| `"expects conv params size matched with each other"` | The convolution-parameter tuples disagree on rank. |
| `"expects channel must be a dimGroup"` | The convolution's channel axis is not assigned to any dim group. |
| `"expects channel dimGroup should contain only channel"` | The channel dim group contains additional non-channel axes. |

## Region Terminator Diagnostics

Region operations route through a yield verifier. The terminator must be `nv_tileaa.yield` and operate inside a `nv_tileaa.func` parent.

```c
LogicalResult verify_region_terminator(Operation op) {
    Operation term = op.region(0).front().terminator;
    if (term.name != "nv_tileaa.yield") {
        return op.emit_error("expects regions to end with 'nv_tileaa.yield'");
    }
    return success();
}

LogicalResult verify_yield_parent(YieldOp op) {
    Operation parent = op.parent;
    if (parent.name != "nv_tileaa.func") {
        return op.emit_error("expects parent op 'nv_tileaa.func'");
    }
    return success();
}
```

The diagnostics:

| Diagnostic | Cause |
|---|---|
| `"expects regions to end with 'nv_tileaa.yield'"` | A region-bearing op's terminator is the wrong op kind. |
| `"expects parent op 'nv_tileaa.func'"` | A yield, return, or function-scoped op appears outside its required parent. |

## Assumption Predicate Verification

`nv_tileaa.assume` accepts a value and a list of predicate attributes. Each predicate that implements the assumption interface verifies the value's type and its own parameters. The first failing predicate emits the diagnostic; later predicates never run.

```c
LogicalResult verify_assume(AssumeOp op) {
    Type constrained_type = op.value.type;
    for (Attribute predicate : op.predicates) {
        AssumePredicate verifier = dyn_cast_assume_predicate(predicate);
        if (verifier == nullptr) {
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

`div_by` states that every constrained element is divisible by a positive power-of-two divisor. Optional `every` and `along` fields refine the statement to a periodic subset of an axis; they must appear together.

```c
LogicalResult verify_div_by(DivByAttr attr, Type type) {
    if (!is_integer_like(type) && !is_pointer_like(type) && !is_memref_like(type)) {
        return emit_diag("div_by requires an integer-, pointer-, or memref-like value");
    }
    if (attr.divisor <= 0 || !is_power_of_two(attr.divisor)) {
        return emit_diag("div_by divisor must be a positive power of two");
    }

    bool has_every = attr.every.has_value;
    bool has_along = attr.along.has_value;
    if (has_every != has_along) {
        return emit_diag("div_by every and along must appear together");
    }
    if (has_every) {
        if (attr.every.value <= 0) {
            return emit_diag("div_by every must be positive");
        }
        if (!axis_is_valid(type, attr.along.value)) {
            return emit_diag("div_by along must reference a valid axis");
        }
    }
    return success();
}
```

### `bounded`

`bounded` states that the constrained integer-like value falls within an inclusive range. Bounds are interpreted using the constrained element width, so the verifier checks both order and representable range.

```c
LogicalResult verify_bounded(BoundedAttr attr, Type type) {
    ElementType element = integer_element_type(type);
    if (!element.is_integer) {
        return emit_diag("bounded requires an integer-like element type");
    }
    IntegerRange range = signed_integer_range(element.bit_width);

    if (attr.lower.has_value && !range.contains(attr.lower.value)) {
        return emit_diag("bounded lower exceeds the element's representable range");
    }
    if (attr.upper.has_value && !range.contains(attr.upper.value)) {
        return emit_diag("bounded upper exceeds the element's representable range");
    }
    if (attr.lower.has_value && attr.upper.has_value
        && attr.lower.value > attr.upper.value) {
        return emit_diag("bounded lower must not exceed upper");
    }
    return success();
}
```

### `same_elements`

`same_elements` records a shape fact: each listed axis must have exactly the specified extent. The attribute earns its keep after rank-changing canonicalization, when a later pass needs to prove that two views still cover the same logical tile.

```c
LogicalResult verify_same_elements(SameElementsAttr attr, Type type) {
    Shape shape = ranked_shape(type);
    if (attr.values.length != shape.rank) {
        return emit_diag("same_elements length must match the constrained value's rank");
    }
    for (int axis = 0; axis < shape.rank; ++axis) {
        if (attr.values[axis] < 0 || attr.values[axis] > shape.dim(axis)) {
            return emit_diag("same_elements axis bound is out of range");
        }
    }
    return success();
}
```

## Operation-Level Verifier Dispatch

Most operations rely on generic trait checks. The operations that need domain-specific verification on top route through this dispatch:

```c
LogicalResult verify_tileaa_operation(Operation op, Target target) {
    if (failed(verify_generic_traits(op))) {
        return failure();
    }
    switch (op.kind) {
    case DOT:           return verify_dot(cast<DotOp>(op), target);
    case BLOCK_TILE:    return verify_block_tile(cast<BlockTileOp>(op));
    case CONV_TILE:     return verify_conv_tile(cast<ConvTileOp>(op));
    case FP_TO_FP:      return verify_float_conversion(cast<FpToFpOp>(op), target);
    case FUNC:          return verify_function_contract(cast<FuncOp>(op));
    case EXECUTE:       return verify_execute_contract(cast<ExecuteOp>(op), target);
    case YIELD:
    case QUEUE_YIELD:   return verify_yield_parent(op);
    case ASSUME:        return verify_assume(cast<AssumeOp>(op));
    case TILED_LOAD:
    case TILED_STORE:
    case TILED_ATOMIC:  return verify_tiled_memop(op);
    default:            return success();
    }
}
```

## Element-Type Contract

The dialect reuses the ordinary MLIR integer and floating families and adds the low-precision formats needed by FP8, FP4, and block-scaled MMA. The legality is a finite table, not ad hoc string tests.

| Element family | Typical use |
|---|---|
| `f16`, `bf16`, `tf32`, `f32` | Standard MMA input and accumulator paths. |
| FP8 E4M3 and E5M2 | Low-precision MMA inputs and conversion targets. |
| E8M0 scale factors | Block-scaled MMA scale-factor operands. |
| FP4 (OCP MX-FP4 and NVFP4) | Blackwell-era block-scaled MMA input paths. |
| Integer widths | Integer MMA, pointer arithmetic, predicates, and indices. |

```c
LogicalResult verify_float_conversion(FpToFpOp op, Target target) {
    if (!is_supported_float_element(op.source.element_type)) {
        return op.emit_error("fp_to_fp source element type is not supported");
    }
    if (!is_supported_float_element(op.result.element_type)) {
        return op.emit_error("fp_to_fp result element type is not supported");
    }
    if ((uses_block_scaled_format(op.source) || uses_block_scaled_format(op.result))
        && !target.supports_block_scaled_mma) {
        return op.emit_error("fp_to_fp block-scaled format requires a target that supports block-scaled MMA");
    }
    return success();
}
```

## Invariants

- `compute-capability` and `compute_capability` parse to one logical target-capability concept; new IR emits the canonical underscore spelling.
- Enum-like attributes are validated by parser tables and by consuming ops.
- `div_by`, `bounded`, and `same_elements` are meaningful only through `nv_tileaa.assume`.
- Memory-policy attributes do not create ordering by themselves; tokens and memory effects do.
- Low-precision element formats are target-gated where the hardware requires it.
- Function, plugin, and queue attributes remain structured until their symbols and resource requirements have been resolved.
- Diagnostic strings are stable across builds. Reimplementations reproduce them verbatim.

## Cross-References

[op-roster.md](op-roster.md) catalogues the operations these verifiers run against and shows complete IR examples. [folds-canonicalizers-tokens.md](folds-canonicalizers-tokens.md) describes the rewrites that run after verification succeeds. The `nv_tileas` block-scaled MMA verifier in [../nv_tileas/verifiers.md](../nv_tileas/verifiers.md) extends the dot contract documented here with Blackwell-specific atom catalog checks.
