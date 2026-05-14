# cuda_tile Verifiers

## Abstract

`cuda_tile` verification is the public gate before TileIR enters private
lowering. It checks tile shapes, numeric policies, memory ordering, structured
control flow, view construction, MMA legality, assumption predicates, and
optimization hints. Many checks are ordinary ODS-style constraints — operand
counts and type equality. The hand-written checks that matter are the domain
rules that keep later alias analysis, scheduling, and code generation honest.

This page presents those checks as algorithms and invariants rather than as a
map of implementation functions.

## Verification Pipeline

Every operation runs generic structural verification first, then any
domain-specific verifier. The order matters: it lets the custom verifier read
operands, regions, and attributes without defending against missing pieces at
every step.

```c
LogicalResult verify_cuda_tile_operation(Operation op, Target target) {
    require_operand_count(op);
    require_result_count(op);
    require_region_count(op);
    require_required_attributes(op);
    require_trait_type_constraints(op);

    switch (op.kind) {
    case FLOAT_ARITH:
        return verify_float_arith(op);
    case INT_CONVERSION:
        return verify_integer_conversion(op);
    case TOKEN_MEMORY:
        return verify_token_memory_op(op);
    case CONTROL_FLOW:
        return verify_control_flow_op(op);
    case REDUCE:
    case SCAN:
        return verify_aggregate_op(op);
    case MMAF:
    case MMAI:
        return verify_mma_op(op, target);
    case ASSUME:
        return verify_assume_predicates(op);
    default:
        return success();
    }
}
```

## Arithmetic and Conversion Rules

Floating arithmetic preserves the producer's numeric choices. `addf`, `subf`,
`mulf`, and `fma` accept the IEEE rounding modes. `divf` also accepts the
division-specific approximate and full modes; both are meaningful only for
`f32`. Flush-to-zero is likewise restricted to `f32`.

```c
LogicalResult verify_float_arith(Operation op) {
    require_same_operand_and_result_types(op);
    require_same_shapes(op);

    if (op.has_flush_to_zero) {
        require(op.element_type == f32_type());
    }

    if (op.kind == DIVF) {
        require(rounding_is_valid_for_division(op.rounding));
        if (op.rounding == APPROX || op.rounding == FULL) {
            require(op.element_type == f32_type());
        }
    } else {
        require(rounding_is_ieee_basic(op.rounding));
    }

    return success();
}
```

Integer conversions check width direction. `exti` widens, `trunci` narrows,
and the signedness attribute controls interpretation rather than changing bit
width. `ftof` rejects identity conversions outright so producers cannot drag
no-op casts into lowering.

```c
LogicalResult verify_conversion(Operation op) {
    int from_width = bit_width(op.input.element_type);
    int to_width = bit_width(op.result.element_type);

    switch (op.kind) {
    case EXTI:
        require(from_width < to_width);
        break;
    case TRUNCI:
        require(from_width > to_width);
        break;
    case FTOF:
        require(from_width != to_width);
        require(op.rounding == NEAREST_EVEN);
        break;
    case ITOF:
        require(op.rounding == NEAREST_EVEN);
        break;
    case FTOI:
        require(op.rounding == NEAREST_INT_TO_ZERO);
        break;
    case BITCAST:
        require(from_width == to_width);
        break;
    }

    return success();
}
```

## Memory Model Rules

Token-ordered memory operations combine three independent checks:

- the pointer or view type matches the loaded, stored, or atomic value type;
- mask and value shapes match the memory access shape;
- memory ordering and memory scope form a legal pair.

Loads accept weak, relaxed, and acquire-like orderings. Stores accept weak,
relaxed, and release-like orderings. Weak memory operations have no scope;
non-weak operations require one.

```c
LogicalResult verify_memory_ordering(MemoryKind kind,
                                     Ordering ordering,
                                     Optional<Scope> scope) {
    if (kind == MEMORY_LOAD) {
        require(ordering == WEAK || ordering == RELAXED || ordering == ACQUIRE);
    }

    if (kind == MEMORY_STORE) {
        require(ordering == WEAK || ordering == RELAXED || ordering == RELEASE);
    }

    if (ordering == WEAK) {
        require(!scope.has_value);
    } else {
        require(scope.has_value);
    }

    return success();
}
```

Atomic RMW also checks mode-specific element types. Bitwise modes are
integer-only, floating add is floating-only, and exchange or compare-and-swap
is restricted to atomic widths the target can update directly.

```c
LogicalResult verify_atomic_rmw(AtomicRmwOp op) {
    require(op.pointer.pointee == op.value.element_type);
    require_same_shape(op.pointer_tile, op.value, op.result);
    require_mask_shape_matches(op.mask, op.result.shape);
    require_atomic_ordering(op.ordering, op.scope);

    switch (op.mode) {
    case AND:
    case OR:
    case XOR:
    case ADD:
    case MAX:
    case MIN:
    case UMAX:
    case UMIN:
        require(op.value.element_type == i32_type() || op.value.element_type == i64_type());
        break;
    case ADDF:
        require(is_float_type(op.value.element_type));
        require(is_supported_atomic_float_width(op.value.element_type));
        break;
    case XCHG:
        require(is_supported_atomic_exchange_type(op.value.element_type));
        break;
    }

    return success();
}
```

## View and Shape Rules

View construction is verified before memory lowering. `make_tensor_view`
checks that the base pointer pointee matches the view element type and that
dynamic shape or stride operands match the number of dynamic slots.
`make_partition_view` checks that the operand tensor view matches the tensor
view embedded in the partition type.

Shape operations enforce element and rank contracts:

- `reshape` preserves total element count;
- `extract` keeps the element type and divides each source dimension by the
  result dimension;
- `cat` preserves rank and element type and changes only the concatenation
  dimension;
- `permute` uses a valid dense permutation of the input rank;
- `iota` produces a one-dimensional integer tile with enough integer range for
  every lane;
- tensor and index-space shape query ops return one value per queried dimension.

```c
LogicalResult verify_reshape(Tile source, Tile result) {
    require(source.element_type == result.element_type);
    require(num_elements(source.shape) == num_elements(result.shape));
    return success();
}
```

## Structured Control Flow

Region verifiers keep the public dialect structured. They deliberately reject
view types as carried loop or branch results, since those views would outlive
the region semantics that created them.

```c
LogicalResult verify_if(IfOp op) {
    require(op.then_region.exists);

    if (op.results.empty()) {
        return success();
    }

    require(op.else_region.exists);
    verify_region_yields(op.then_region, op.result_types);
    verify_region_yields(op.else_region, op.result_types);
    reject_view_typed_results(op.result_types);
    return success();
}

LogicalResult verify_for(ForOp op) {
    require(op.induction_var.type == op.lower_bound.type);
    require(op.lower_bound.type == op.upper_bound.type);
    require(op.lower_bound.type == op.step.type);
    require(op.init_values.types == op.region_iter_arg_types);
    require(op.result_types == op.region_iter_arg_types);
    reject_view_typed_results(op.result_types);
    return success();
}
```

`break` and `continue` walk outward through nested `if` regions until they
find a compatible `loop` or `for`. The verifier rejects any early-exit op that
jumps out through an unrelated parent.

```c
LogicalResult verify_early_exit(Operation op, Set<OpKind> allowed) {
    Operation parent = op.parent;

    while (parent.kind == IF) {
        parent = parent.parent;
    }

    require(allowed.contains(parent.kind));
    require(op.operands.types == expected_exit_types(parent, op.kind));
    return success();
}
```

## Reductions and Scans

`reduce` and `scan` require a pure body region. The body receives pairs of
rank-zero tile arguments and yields one value per input. Identity attributes, if
present, must match the input element types.

```c
LogicalResult verify_aggregate_op(AggregateOp op) {
    require(!op.inputs.empty());
    require(op.inputs.length == op.results.length);
    require_valid_reduction_dimension(op.dim, op.inputs[0].rank);
    require_identities_match_inputs(op.identities, op.inputs);

    Block body = op.body.entry_block;
    require(body.arguments.length == 2 * op.inputs.length);

    for (Operation nested : op.body.operations) {
        require(nested.is_memory_effect_free());
    }

    verify_region_yields(op.body, op.result_types);
    return success();
}
```

## MMA Rules

Floating and integer MMA share their shape rules but diverge on element type.
Operands are two-dimensional or batched three-dimensional tiles. Contracting
dimensions must agree, accumulator/result shape equals the output shape, and
integer MMA carries explicit signedness attributes.

```c
LogicalResult verify_mma(MmaOp op, Target target) {
    require(op.lhs.rank == 2 || op.lhs.rank == 3);
    require_all_ranks_equal(op.lhs, op.rhs, op.acc, op.result);
    require_mma_dimensions_match(op.lhs, op.rhs, op.acc, op.result);

    if (op.is_integer) {
        require(op.has_signedness_lhs);
        require(op.has_signedness_rhs);
        require(op.acc.element_type == i32_type());
        require(op.result.element_type == i32_type());
        require(is_legal_integer_mma_input(op.lhs.element_type));
        require(op.lhs.element_type == op.rhs.element_type);
    } else {
        require(is_legal_float_mma_tuple(op.lhs.element_type, op.acc.element_type));
        require(op.acc.type == op.result.type);
    }

    return success();
}
```

## Diagnostic Stability

Verifier diagnostics are part of the practical producer contract. Frontends
and tests key off wording to decide whether they emitted illegal IR or hit a
compiler bug. Keep diagnostics specific — name the attribute, operand,
region, or type relation that failed, and include the offending type or value
where possible.

## Invariants

- Generic ODS-style checks run before domain-specific verifiers.
- Floating rounding and flush-to-zero policies are target- and type-checked.
- Conversion operations are not allowed to hide no-op casts.
- Weak memory ordering has no scope; non-weak memory ordering requires scope.
- Token-ordered operations preserve token inputs and outputs.
- Structured control-flow results cannot smuggle view lifetimes across regions.
- Aggregate bodies are pure and yield the expected result types.
- MMA verifiers check both shape and element-type legality before lowering.

