# nv_tileaa Operation Roster

## Abstract

`nv_tileaa` is the alias-aware tile dialect between public `cuda_tile` IR and
the lower `nv_tileas` scheduling dialect. Its operations keep the mathematical
shape of the original program while making pointer provenance, memory
ordering, queue flow, and plugin boundaries explicit enough for later passes
to schedule and materialize. This page documents the operation surface as a
reimplementation contract — what each family represents, which operands and
attributes matter, and which invariants a verifier or lowering must preserve.

The roster groups operations by behavior, not by binary registration order. A
reimplementation should track the family contracts and textual mnemonics, not
the incidental internal layout used by one compiler build.

## Semantic Families

| Family | Operations | Contract |
|---|---|---|
| Floating and integer tile math | `addf`, `subf`, `mulf`, `divf`, `fma`, `sqrt`, `rsqrt`, `exp2`, `clampf`, `mulhiui` | Elementwise arithmetic over scalar or tile-shaped values. Floating ops accept rounding or NaN propagation attributes where applicable. |
| Dot, convolution, and collectives | `dot`, `conv_dot`, `reduce`, `scan`, `histogram` | Preserve high-level collective math until TileAS can choose MMA, tensor-memory, or reduction pipelines. |
| Shape and tile construction | `splat`, `broadcast`, `expand_dims`, `extract`, `extract_slice`, `view`, `cat`, `permute`, `make_range`, `generate`, `block_tile`, `conv_tile`, `get_dim_size` | Express rank changes, indexing, view reinterpretation, generated tiles, and convolution blocking without hiding shape dependencies. |
| Pointer, memref, and type conversion | `addptr`, `int_to_ptr`, `ptr_to_int`, `make_memref`, `bitcast`, `fp_to_fp`, `extern_elementwise`, `elementwise_inline_asm`, `call_elementwise_intrinsic` | Convert public pointer and element-type concepts into explicit addressable objects and per-element escape hatches. |
| Memory effects | `get_global`, `load`, `store`, `tiled_load`, `tiled_store`, `gather_load`, `scatter_store`, `atomic_cas`, `atomic_rmw`, `tiled_atomic_rmw` | Represent memory traffic with visible masks, volatility, TMA eligibility, bounds facts, and token dependencies. |
| Tokens, assumptions, and lifetime hints | `create_mem_token`, `join_mem_token`, `mark_for_reuse`, `assert`, `assume`, `optimization_barrier`, `pragma`, `message`, `print` | Carry ordering, alias, reuse, diagnostics, and optimizer constraints as SSA-visible IR. |
| Functions, plugins, and launch structure | `func`, `call`, `return`, `yield`, `global`, `launch_func`, `execute`, `plugin`, `inject_ir` | Provide the internal symbol, function, launch, and extension shell that survives until queue and plugin lowering. |
| Grid and queue flow | `get_program_id`, `get_num_programs`, `is_valid_program_id`, `cancel_next_program_id`, `create_queue`, `queue.get`, `queue.put`, `queue.yield` | Model program-grid queries and queue dataflow before they become TileAS async pipeline regions. |

## Core Operation Contracts

### Arithmetic

Elementwise arithmetic is shape-preserving. The verifier rejects operand sets
that cannot be broadcast or matched by the dialect's normal shape rules. For
floating operations, the result element type is the operand element type;
`fp_to_fp` is the explicit conversion boundary and must be the only operation
that silently changes floating width.

```c
TileValue verify_elementwise_arithmetic(Op op) {
    Shape result_shape = infer_common_shape(op.operands);
    ElementType type = infer_common_element_type(op.operands);

    require_all_operands_compatible(op.operands, result_shape, type);
    require_rounding_mode_if_needed(op);

    return TileValue(type, result_shape);
}
```

`mulhiui` is an unsigned high-half multiply: for each lane, multiply the
zero-extended operands at double width and return the upper half. Never model
it as ordinary signed multiplication followed by a shift.

```c
uintN mulhiui(uintN a, uintN b) {
    uint2N wide = zero_extend(a) * zero_extend(b);
    return truncate_to_N_bits(wide >> N);
}
```

### Dot and Convolution

#### `nv_tileaa.dot`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | A | `tile<M × K × elem_a>` | yes | rank-2 or rank-3 (batched) |
| operand 1 | B | `tile<K × N × elem_b>` | yes | K dimension agrees with A |
| operand 2 | C | `tile<M × N × elem_c>` | yes | accumulator |
| operand 3 | sfa | `tile<scale × E8M0>` | block-scaled only | scale factors for A |
| operand 4 | sfb | `tile<scale × E8M0>` | block-scaled only | scale factors for B |
| result 0 | D | `tile<M × N × elem_c>` | yes | same shape as C |
| attr `signedness_a` | enum | `signed\|unsigned` | integer MMA | |
| attr `signedness_b` | enum | `signed\|unsigned` | integer MMA | |
| attr `propagate_nan` | bool | | optional | floating-point only |
| attr `operandSegmentSizes` | dense i32 | length 5 | yes | `{A, B, C, sfa, sfb}` |

`dot` abstracts matrix multiply-accumulate. It accepts ordinary float and
integer MMA shapes and carries the scale-factor operands needed for
Blackwell-style block-scaled MMA. The verifier owns four properties:

- A and B use a legal paired element type.
- The accumulator/result type is legal for that pair.
- Integer operands agree on bit width and signedness rules.
- Block-scaled forms are gated on a target that supports them.

```c
LogicalResult verify_dot(DotOp op, Target target) {
    MmaShape shape = infer_mma_shape(op.a, op.b, op.c);
    require_compatible_contracting_dims(shape);

    if (op.has_scale_factors) {
        require(target.supports_block_scaled_mma);
        require_legal_scale_factor_types(op.sfa, op.sfb, shape);
    }

    require_legal_mma_element_tuple(op.a.type, op.b.type, op.c.type, op.result.type);
    require_signedness_attrs_for_integer_mma(op);
    return success();
}
```

`conv_dot`, `conv_tile`, and `block_tile` keep convolution lowering
structured. The key behavior is not a special address calculation — it is
preserving padding, activation layout, and tile-blocking facts until the
memory layout pass can pick the right producer and consumer layouts.

### Shape Operations

Shape operations stay cheap, explicit, and canonicalizable. `view` changes
interpretation without changing elements, `expand_dims` inserts size-one axes,
`broadcast` repeats values across larger axes, and `extract` or
`extract_slice` projects subshapes. `splat` is the scalar-to-tile constructor
and the canonical sink for many reshape folds.

```c
Shape infer_shape(Op op) {
    switch (op.kind) {
    case SPLAT:
        return op.result_shape;
    case EXPAND_DIMS:
        return insert_axes(op.input.shape, op.axes, size_one_axes());
    case BROADCAST:
        require_can_broadcast(op.input.shape, op.result_shape);
        return op.result_shape;
    case VIEW:
        require_same_element_count(op.input.shape, op.result_shape);
        return op.result_shape;
    case EXTRACT:
        return remove_indexed_axes(op.input.shape, op.indices);
    case EXTRACT_SLICE:
        return op.slice_shape;
    default:
        return infer_from_traits(op);
    }
}
```

### Pointer and Memref Construction

#### `nv_tileaa.addptr`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | base | `ptr` or `tile<ptr>` | yes | preserves address space |
| operand 1 | offset | `index` or `tile<index>` | yes | shape must broadcast against base |
| result 0 | ptr | same as base | yes | element type and address space inherited |

#### `nv_tileaa.make_memref`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | base | `ptr` | yes | base pointer of the memref |
| operand 1 | offset | `index` | optional | byte offset added to base |
| operand 2..R+1 | sizes | `index` | yes per-dynamic-dim | matches dynamic slots in result shape |
| operand R+2.. | strides | `index` | yes per-dynamic-stride | matches dynamic stride slots |
| result 0 | memref | `memref` | yes | element type, shape, stride packed into result type |
| attr `alias_scope` | scope id | u32 | optional | provenance tag consumed by alias analysis |
| attr `operandSegmentSizes` | dense i32 | length 4 | yes | `{base, offset, sizes, strides}` |

`addptr` is the primary pointer-arithmetic operation. It accepts scalar or
tile-shaped offsets and preserves the base pointer's address-space and
provenance. Canonicalization collapses chained additions into a single
addition with a folded offset expression.

```c
PointerValue addptr(PointerValue base, IndexValue offset, Layout layout) {
    ByteOffset bytes = scale_offset_by_element_size(offset, base.element_type);
    return PointerValue(base.address + bytes, base.element_type, base.space, layout.provenance);
}
```

`make_memref` packages a base pointer with offset, sizes, strides, element
type, memory space, and alias provenance. Later TMA descriptor generation
depends on this object being structurally complete — never hide strides or
bounds behind opaque pointer arithmetic.

### Memory Effects

Scalar memory ops and tiled memory ops share one discipline: every memory
effect consumes the incoming token and returns a token representing the
effect after the access. Loads return a value too; stores and atomics still
return the updated token even when their data result is unused.

#### `nv_tileaa.load` / `nv_tileaa.tiled_load`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | base | `ptr` or `memref` | yes | source address |
| operand 1..R | indices | `index` | yes (R = base rank) | per-axis index |
| operand R+1 | mask | `tile<S × i1>` | optional | per-lane predicate |
| operand R+2 | other | `tile<S × element>` | optional | fallback value when masked off |
| token slot | token | `mem_token` | optional | drives ordering |
| result 0 | value | `tile<S × element>` or scalar | yes | element type matches base pointee |
| result 1 | token | `mem_token` | optional | mirrors token operand |
| attr `cache_modifier` | enum | `none\|ca\|cg\|cs\|lu\|cv` | optional | |
| attr `eviction_policy` | enum | `none\|first\|last\|normal` | optional | |
| attr `mem_semantic` | enum | `weak\|relaxed\|acquire` | optional | |
| attr `mem_scope` | enum | `tl_blk\|cluster\|gpu\|sys` | required when semantic > weak | |
| attr `in_bounds` | dense bool | per-axis | optional | |
| attr `operandSegmentSizes` | dense i32 | length 4 | yes | `{base, indices, mask, other}` |

#### `nv_tileaa.store` / `nv_tileaa.tiled_store`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | base | `ptr` or `memref` | yes | destination |
| operand 1 | value | `tile<S × element>` or scalar | yes | element type matches base pointee |
| operand 2..R+1 | indices | `index` | yes | per-axis index |
| operand R+2 | mask | `tile<S × i1>` | optional | per-lane predicate |
| token slot | token | `mem_token` | optional | |
| result 0 | token | `mem_token` | optional | mirrors token operand |
| attr `mem_semantic` | enum | `weak\|relaxed\|release` | optional | acquire/acq_rel rejected |
| attr `mem_scope` | enum | as above | required when semantic > weak | |
| attr `cache_modifier` | enum | as above | optional | |
| attr `operandSegmentSizes` | dense i32 | length 4 | yes | `{base, value, indices, mask}` |

#### `nv_tileaa.atomic_cas` / `nv_tileaa.atomic_rmw` / `nv_tileaa.tiled_atomic_rmw`

| Slot | Kind | Type | Required | Notes |
|---|---|---|---|---|
| operand 0 | base | `ptr` or `memref` | yes | atomic target |
| operand 1 | value or compare | scalar/tile | yes | RMW operand or CAS compare |
| operand 2 | replacement | scalar/tile | CAS only | |
| operand 3.. | indices, mask | `index`, `tile<i1>` | optional | per-axis index; predicate |
| token slot | token | `mem_token` | optional | |
| result 0 | old value | scalar/tile | yes | |
| result 1 | token | `mem_token` | optional | |
| attr `rmw_mode` | enum | full set | RMW only | `add\|and\|or\|xor\|xchg\|min\|max\|umin\|umax\|addf` |
| attr `mem_semantic` | enum | full set | optional | |
| attr `mem_scope` | enum | full set | required when semantic > weak | |

```c
LogicalResult verify_memory_op_common(MemoryOp op) {
    require_operand_segments(op);
    require_indices_match_base_rank(op.base(), op.indices());
    require_mask_shape_matches_result(op.mask(), op.result(0).shape());
    require_token_arity_matches_segment(op);

    if (op.mem_semantic() != WEAK) {
        require(op.mem_scope().has_value(),
                "non-weak memory ordering requires explicit scope");
    } else {
        require(!op.mem_scope().has_value(),
                "weak memory ordering must not carry a scope");
    }
    return success();
}
```


```c
LoadResult lower_memory_read(MemRef ref, Indices indices, Token token, Mask mask) {
    require_indices_in_rank(ref, indices);

    if (mask.is_constant_false()) {
        return LoadResult(mask.other_value_or_undef(), token);
    }

    Value value = masked_or_unmasked_load(ref, indices, mask);
    Token next = sequence_after(token, value.memory_effect);
    return LoadResult(value, next);
}
```

`tiled_load`, `tiled_store`, and `tiled_atomic_rmw` are the TMA-aware forms.
Their attributes record whether TMA is allowed and whether each dimension is
known in bounds. The TileAA verifier validates the structural facts; the
TileAS lowering decides whether a concrete TMA instruction is profitable and
legal for the selected layout.

### Tokens and Lifetime

`create_mem_token` creates an initial memory-order value. `join_mem_token`
merges several order edges into one. `mark_for_reuse` tells buffer allocation
that a value's lifetime extends beyond naive SSA liveness. The token value
carries no user-visible data — it is an ordering edge that later lowering can
map to barrier phase state.

```c
Token join_mem_token(ArrayRef<Token> inputs) {
    if (inputs.empty()) {
        return create_mem_token();
    }

    Token result = inputs[0];
    for (Token token : inputs.drop_front()) {
        result = merge_order_edges(result, token);
    }
    return result;
}
```

### Plugins and Queues

`plugin` and `execute` give opaque kernel fragments a structured extension
point. They carry function-like operands, layout metadata, and resource
requirements such as registers, shared memory, tensor memory, and named
barriers. `queue.get`, `queue.put`, and `queue.yield` form a dataflow shell
that TileAS later collapses into producer and consumer pipeline regions.

```c
void lower_queue_region(QueueRegion region, PipelineBuilder builder) {
    for (QueueOp op : region.ops) {
        switch (op.kind) {
        case QUEUE_GET:
            builder.emit_consumer_wait(op.queue, op.consumer_index);
            bind_queue_results(op);
            break;
        case QUEUE_PUT:
            builder.emit_producer_commit(op.queue, op.values);
            break;
        case QUEUE_YIELD:
            builder.close_region(op.yielded_values);
            break;
        }
    }
}
```

## Verification Invariants

- A TileAA module should contain no remaining `cuda_tile` operations.
- Every memory op with effects participates in the token protocol.
- Pointer and memref operations preserve address space, element type, and alias
  provenance.
- Shape-changing ops preserve element count unless their semantics explicitly
  create or remove repetition.
- `yield`, `queue.yield`, and `return` operands match the parent region or
  function contract.
- `func`, `call`, `plugin`, and `execute` symbol references resolve inside the
  containing module.
- Block-scaled MMA and tensor-memory features require a target that supports
  the needed Blackwell instruction family.
- TMA eligibility attributes are promises to later lowering, not proof that TMA
  must be emitted.

