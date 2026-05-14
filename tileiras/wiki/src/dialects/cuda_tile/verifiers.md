# cuda_tile Verifiers

## Abstract

`cuda_tile` verification is the public gate before TileIR enters private lowering. The verifier rejects malformed tile shapes, illegal numeric policies, broken memory-ordering pairs, structurally invalid control flow, view construction errors, MMA shape mismatches, malformed assumption predicates, and inconsistent optimization hints. Generic ODS-style checks for operand counts, result counts, region counts, attribute presence, and trait-driven type constraints run first; domain-specific verifiers run only against operations that already pass generic checks.

The diagnostic strings emitted by the verifier are part of the public contract. Frontends and tests key off the wording to distinguish "I emitted illegal IR" from "I hit a compiler bug." This page enumerates the verbatim diagnostics by category and shows the branch logic that decides which message a given operation receives.

## Verification Pipeline

```c
LogicalResult verify_cuda_tile_operation(Operation op, Target target) {
    if (failed(verify_generic_traits(op))) {
        return failure();
    }

    switch (op.family) {
    case FAMILY_FLOAT_ARITH:    return verify_float_arith(op);
    case FAMILY_CONVERSION:     return verify_conversion(op);
    case FAMILY_TOKEN_MEMORY:   return verify_token_memory(op);
    case FAMILY_CONTROL_FLOW:   return verify_control_flow(op);
    case FAMILY_VIEW_AND_SHAPE: return verify_view_and_shape(op);
    case FAMILY_AGGREGATE:      return verify_aggregate(op);
    case FAMILY_MMA:            return verify_mma(op, target);
    case FAMILY_ASSUMPTION:     return verify_assume(op);
    default:                    return success();
    }
}
```

Generic verification covers operand count, result count, region count, required attribute presence, and trait-driven type constraints. The first failure short-circuits the rest of the pipeline so a malformed operand list never reaches a domain verifier that would dereference it.

## Block-Structure Diagnostics

The strongest invariant covers region structure. Region-bearing operations require a non-empty entry block, block argument types that obey the operation's region contract, and a terminator whose operand list lines up with both the block-argument list and the enclosing op's result list.

```c
LogicalResult verify_region_structure(Operation op) {
    for (Region region : op.regions) {
        Block entry = region.entry_block;

        if (entry.operations.empty()) {
            return op.emit_error("expect non-empty block");
        }

        for (size_t i = 0; i < entry.arguments.size(); ++i) {
            Type arg_type = entry.arguments[i].type;
            if (!isa<TileType>(arg_type)) {
                return op.emit_error("expected TileType for block arguments but got types: ")
                    .append_type_list(entry.argument_types());
            }
            if (cast<TileType>(arg_type).rank != 0) {
                return op.emit_error("expect 0-rank tile type at index: ").append(i);
            }
        }

        Operation terminator = entry.terminator;
        if (terminator.operands.size() != op.expected_terminator_arity()) {
            return op.emit_error("expect number of terminators operands (")
                .append(terminator.operands.size())
                .append(") to match expected (")
                .append(op.expected_terminator_arity())
                .append(")");
        }

        for (size_t i = 0; i < terminator.operands.size(); ++i) {
            Type tt = terminator.operands[i].type;
            Type bt = entry.arguments[i].type;
            if (tt != bt) {
                return op.emit_error("expected TileType for operand and terminator types but got: ")
                    .append(tt).append(" vs ").append(bt);
            }
        }
    }
    return success();
}
```

The diagnostics emitted by this code path are:

| Diagnostic | Cause |
|---|---|
| `"expect non-empty block"` | A region's entry block contains no operations (no terminator either). |
| `"expect 0-rank tile type at index: "` followed by the index | A reduction or scan body block argument is not the required rank-zero tile. |
| `"expected TileType for block arguments but got types: "` followed by the argument-type list | A body block argument carries a non-tile type. |
| `"expect number of terminators operands ("` followed by actual-vs-expected | The terminator's operand list does not match the parent's result-type list. |
| `"expected TileType for operand and terminator types but got: "` followed by both types | A terminator operand's element type does not match the corresponding block-argument element type. |
| `"only pure operations allowed"` | A reduction or scan body contains an operation with memory effects. |
| `"invalid op: "` followed by the operation name | A body region contains an operation the family rejects (for example, a side-effecting op inside `reduce`). |

These messages name the failing slot in the contract, not the implementation function that produced them. A producer reading them can locate the operand or argument by index without consulting compiler internals.

## Type-Compatibility Diagnostics

Floating arithmetic preserves the producer's numeric choices. The verifier rejects illegal rounding modes, illegal flush-to-zero applications, and mismatched element types in the same arithmetic op.

```c
LogicalResult verify_float_arith(Operation op) {
    if (!operand_and_result_types_equal(op)) {
        return op.emit_error("expected matching operand and result element types");
    }

    if (op.has_flush_to_zero && op.element_type != f32_type()) {
        return op.emit_error("flush-to-zero is only legal for f32 element type");
    }

    if (op.kind == DIVF) {
        if (!rounding_is_valid_for_division(op.rounding)) {
            return op.emit_error("invalid rounding mode for divf");
        }
        if ((op.rounding == APPROX || op.rounding == FULL)
            && op.element_type != f32_type()) {
            return op.emit_error("approx and full rounding modes require f32");
        }
    } else if (!rounding_is_ieee_basic(op.rounding)) {
        return op.emit_error("rounding mode not allowed outside divf");
    }
    return success();
}
```

Integer conversions check width direction. `exti` widens, `trunci` narrows, `ftof` rejects identity conversions, and `bitcast` requires equal widths.

```c
LogicalResult verify_conversion(Operation op) {
    int from = bit_width(op.input.element_type);
    int to   = bit_width(op.result.element_type);

    switch (op.kind) {
    case EXTI:
        if (from >= to) {
            return op.emit_error("exti requires the result width to be strictly greater than the input width");
        }
        break;
    case TRUNCI:
        if (from <= to) {
            return op.emit_error("trunci requires the result width to be strictly less than the input width");
        }
        break;
    case FTOF:
        if (from == to) {
            return op.emit_error("ftof rejects identity conversions");
        }
        if (op.rounding != NEAREST_EVEN) {
            return op.emit_error("ftof requires nearest-even rounding");
        }
        break;
    case ITOF:
        if (op.rounding != NEAREST_EVEN) {
            return op.emit_error("itof requires nearest-even rounding");
        }
        break;
    case FTOI:
        if (op.rounding != NEAREST_INT_TO_ZERO) {
            return op.emit_error("ftoi requires truncation toward zero");
        }
        break;
    case BITCAST:
        if (from != to) {
            return op.emit_error("bitcast requires equal-width source and destination element types");
        }
        break;
    }
    return success();
}
```

## Operand-Shape Diagnostics

Token-ordered memory operations check three independent contracts: the pointer or view's pointee/element type matches the value type, the mask shape matches the tile shape, and the memory ordering and scope form a legal pair.

```c
LogicalResult verify_token_memory(Operation op) {
    if (op.pointer.pointee != op.value.element_type) {
        return op.emit_error("pointer pointee element type must match value element type");
    }
    if (!shapes_compatible(op.pointer_tile, op.value)) {
        return op.emit_error("pointer tile shape must match value shape");
    }
    if (op.has_mask && op.mask.shape != op.value.shape) {
        return op.emit_error("mask shape must match value shape");
    }

    if (op.kind == LOAD) {
        if (op.ordering != WEAK && op.ordering != RELAXED && op.ordering != ACQUIRE) {
            return op.emit_error("load ordering must be weak, relaxed, or acquire");
        }
    } else if (op.kind == STORE) {
        if (op.ordering != WEAK && op.ordering != RELAXED && op.ordering != RELEASE) {
            return op.emit_error("store ordering must be weak, relaxed, or release");
        }
    }

    if (op.ordering == WEAK && op.scope.has_value) {
        return op.emit_error("weak memory ordering must not carry a scope");
    }
    if (op.ordering != WEAK && !op.scope.has_value) {
        return op.emit_error("non-weak memory ordering requires an explicit scope");
    }
    return success();
}
```

Atomic RMW further restricts the element type by mode. Bitwise and integer-arithmetic modes accept only `i32` or `i64`; floating add requires a floating type the target can atomically update; exchange and compare-and-swap require an element width the hardware supports atomically.

```c
LogicalResult verify_atomic_rmw(AtomicRmwOp op) {
    if (failed(verify_token_memory(op))) {
        return failure();
    }

    switch (op.mode) {
    case AND: case OR: case XOR:
    case ADD: case MAX: case MIN: case UMAX: case UMIN:
        if (op.value.element_type != i32_type() && op.value.element_type != i64_type()) {
            return op.emit_error("integer rmw mode requires i32 or i64 element type");
        }
        break;
    case ADDF:
        if (!is_supported_atomic_float(op.value.element_type)) {
            return op.emit_error("floating-add rmw requires a target-supported floating width");
        }
        break;
    case XCHG:
        if (!is_supported_atomic_xchg(op.value.element_type)) {
            return op.emit_error("xchg rmw requires a target-supported atomic width");
        }
        break;
    }
    return success();
}
```

## View and Shape Diagnostics

View construction is verified before memory lowering. `make_tensor_view` checks that the base-pointer pointee matches the view's element type and that the dynamic shape/stride operand count matches the type's dynamic-slot count. `make_partition_view` checks that the operand tensor view matches the tensor view embedded in the partition type.

Shape operations carry tight element-count and rank rules:

```c
LogicalResult verify_reshape(ReshapeOp op) {
    if (op.source.element_type != op.result.element_type) {
        return op.emit_error("reshape requires matching element types");
    }
    int64_t in  = num_elements(op.source.shape);
    int64_t out = num_elements(op.result.shape);
    if (in != out) {
        return op.emit_error("reshape element-count mismatch: source has ")
            .append(in).append(" elements, result has ").append(out);
    }
    return success();
}
```

A concrete reshape failure walk illustrates the contract. Consider the input

```mlir
%dst = cuda_tile.reshape %src : tile<8x8xf32> -> tile<7x9xf32>
```

The verifier computes 8·8 = 64 elements in and 7·9 = 63 elements out, the equality check fails, and the diagnostic identifies both counts so the producer knows whether to fix the source rank/shape, the result rank/shape, or both. The op never reaches lowering, so no downstream pass needs to defend against a partially-typed reshape.

`extract`, `cat`, `permute`, and `iota` carry parallel contracts. `extract` divides every source dimension by the corresponding result dimension and rejects non-integer ratios. `cat` accepts only inputs that agree on rank and element type and differ on the concatenation axis. `permute` requires a dense permutation of the input rank with no repeated indices. `iota` produces a one-dimensional integer tile whose extent fits in the chosen integer width.

## Structured Control Flow

Region verifiers reject view-typed loop-carried values and view-typed branch results. Views describe memory that may outlive the region that constructs them; allowing them as iter-args or yields would let a frontend smuggle aliasing past the verifier and into later passes that assume views never escape a region.

```c
LogicalResult verify_if(IfOp op) {
    if (!op.then_region.exists()) {
        return op.emit_error("if requires a then-region");
    }
    if (op.results.empty()) {
        return success();
    }
    if (!op.else_region.exists()) {
        return op.emit_error("if with results requires an else-region");
    }
    if (failed(verify_region_yields(op.then_region, op.result_types))) {
        return failure();
    }
    if (failed(verify_region_yields(op.else_region, op.result_types))) {
        return failure();
    }
    for (Type t : op.result_types) {
        if (is_view_type(t)) {
            return op.emit_error("view-typed if results are not permitted");
        }
    }
    return success();
}

LogicalResult verify_for(ForOp op) {
    if (op.induction_var.type != op.lower_bound.type
        || op.lower_bound.type != op.upper_bound.type
        || op.lower_bound.type != op.step.type) {
        return op.emit_error("for induction, lower, upper, and step must share an integer type");
    }
    if (op.init_values.types != op.region_iter_arg_types) {
        return op.emit_error("for init values must match region iter-arg types");
    }
    if (op.result_types != op.region_iter_arg_types) {
        return op.emit_error("for result types must match region iter-arg types");
    }
    for (Type t : op.result_types) {
        if (is_view_type(t)) {
            return op.emit_error("view-typed for results are not permitted");
        }
    }
    return success();
}
```

`break` and `continue` walk outward through nested `if` regions until they hit a compatible `loop` or `for`. The verifier rejects an early-exit op that jumps out through an unrelated parent.

```c
LogicalResult verify_early_exit(Operation op, Set<OpKind> allowed_parents) {
    Operation parent = op.parent;
    while (parent.kind == IF) {
        parent = parent.parent;
    }
    if (!allowed_parents.contains(parent.kind)) {
        return op.emit_error("early-exit must be enclosed by a compatible loop or for");
    }
    if (op.operands.types != expected_exit_types(parent, op.kind)) {
        return op.emit_error("early-exit operand types must match the enclosing region contract");
    }
    return success();
}
```

## Special-Op Diagnostics

Reductions and scans share a verifier. The body region must be pure, must consume pairs of rank-zero tile arguments, and must yield one value per input.

```c
LogicalResult verify_aggregate(AggregateOp op) {
    if (op.inputs.empty()) {
        return op.emit_error("reduce/scan requires at least one input");
    }
    if (op.inputs.length != op.results.length) {
        return op.emit_error("reduce/scan must produce one result per input");
    }
    if (op.dim < 0 || op.dim >= op.inputs[0].rank) {
        return op.emit_error("reduction dimension is out of range");
    }
    for (size_t i = 0; i < op.identities.length; ++i) {
        if (op.identities[i].type != op.inputs[i].element_type) {
            return op.emit_error("identity element type must match input element type");
        }
    }

    Block body = op.body.entry_block;
    if (body.arguments.length != 2 * op.inputs.length) {
        return op.emit_error("reduce/scan body must take two rank-zero arguments per input");
    }
    for (size_t i = 0; i < body.arguments.length; ++i) {
        TileType arg = cast<TileType>(body.arguments[i].type);
        if (arg.rank != 0) {
            return op.emit_error("expect 0-rank tile type at index: ").append(i);
        }
    }

    for (Operation nested : op.body.operations) {
        if (!nested.is_memory_effect_free()) {
            return op.emit_error("only pure operations allowed");
        }
        if (!nested.kind_legal_in_aggregate_body()) {
            return op.emit_error("invalid op: ").append(nested.name);
        }
    }
    return verify_region_yields(op.body, op.result_types);
}
```

The diagnostic `"only pure operations allowed"` fires the moment the body walk encounters an op with memory effects; `"invalid op: "` fires for an op kind the body region rejects categorically (for example, a `cuda_tile.return` inside a reduce body).

## MMA Diagnostics

Floating and integer MMA share their shape rules but diverge on element type. Operands are rank-2 or rank-3 (the rank-3 form is batched). Contracting dimensions must agree, accumulator and result shapes must match, and integer MMA must carry explicit signedness attributes.

```c
LogicalResult verify_mma(MmaOp op, Target target) {
    if (op.lhs.rank != 2 && op.lhs.rank != 3) {
        return op.emit_error("mma operand A must be rank-2 or rank-3");
    }
    if (op.rhs.rank != op.lhs.rank
        || op.acc.rank != op.lhs.rank
        || op.result.rank != op.lhs.rank) {
        return op.emit_error("mma operands must share rank");
    }
    if (op.lhs.k_dim != op.rhs.k_dim) {
        return op.emit_error("mma contracting dimension mismatch");
    }
    if (op.lhs.m_dim != op.acc.m_dim || op.rhs.n_dim != op.acc.n_dim) {
        return op.emit_error("mma accumulator must agree with A and B on M and N");
    }
    if (op.acc.shape != op.result.shape) {
        return op.emit_error("mma accumulator and result shapes must match");
    }

    if (op.is_integer) {
        if (!op.has_signedness_lhs || !op.has_signedness_rhs) {
            return op.emit_error("expect signedness attribute for operand A");
        }
        if (op.acc.element_type != i32_type() || op.result.element_type != i32_type()) {
            return op.emit_error("integer mma accumulator and result must be i32");
        }
        if (!is_legal_integer_mma_input(op.lhs.element_type)
            || op.lhs.element_type != op.rhs.element_type) {
            return op.emit_error("integer mma inputs must share a legal integer element type");
        }
    } else {
        if (!is_legal_float_mma_tuple(op.lhs.element_type, op.acc.element_type)) {
            return op.emit_error("floating mma input/accumulator pair is not supported on the target");
        }
        if (op.acc.type != op.result.type) {
            return op.emit_error("floating mma accumulator and result must share type");
        }
    }
    return success();
}
```

## Assumption Predicates

`assume` accepts a value and a list of predicate attributes. Each predicate runs against the constrained value's type and its own parameters; the first failure short-circuits the rest.

```c
LogicalResult verify_assume(AssumeOp op) {
    Type t = op.value.type;
    for (Attribute pred : op.predicates) {
        if (auto verifier = dyn_cast_assume_predicate(pred)) {
            if (failed(verifier.verify(pred, t, op))) {
                return failure();
            }
        }
    }
    return success();
}
```

`div_by`, `bounded`, and `same_elements` each implement that interface. Their per-predicate diagnostics are documented alongside the attribute surface for the dialect they share with `nv_tileaa`.

## Diagnostic Stability

Verifier diagnostics are a producer-facing contract. Keep them specific — name the failing slot, the offending type, or the integer index of the bad argument. Avoid generic phrases that force the producer to read compiler source to interpret the failure. The verbatim strings above (and the typo-preserving variants from later TileAS verification) survive across builds because external infrastructure has been matching them; silently rewording a diagnostic breaks downstream log capture.

## Invariants

- Generic ODS-style checks run before any domain-specific verifier.
- Floating rounding and flush-to-zero policies are target- and type-checked.
- Conversions are not allowed to hide no-op casts.
- Weak memory ordering rejects scope; non-weak memory ordering requires it.
- Token-ordered operations preserve token inputs and outputs.
- Structured control-flow results cannot smuggle view lifetimes across regions.
- Aggregate bodies are pure and yield the expected result types per input.
- MMA verifiers check shape and element-type legality before lowering.
- Diagnostic wording is stable; reimplementations must reproduce it byte-for-byte.

## Cross-References

[Operation Roster](op-roster.md#operation-families) catalogues the operations these verifiers run against. [Types and Attributes](types-and-attrs.md#concrete-types) describes the underlying tile, view, pointer, and token types and their structural contracts. [Canonicalizers and Folds](canonicalizers-and-folds.md#fold-surface) describes the rewrites that run after verification succeeds. The `nv_tileaa` verifier set in [nv_tileaa Types, Attributes, Verifiers](../nv_tileaa/types-attrs-verifiers.md#assumption-predicate-verification) reuses the assumption-predicate contract documented here.
