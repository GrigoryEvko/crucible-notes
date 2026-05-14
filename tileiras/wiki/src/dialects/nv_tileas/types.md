# nv_tileas Types

## Abstract

The `nv_tileas` type system carries the state needed to make asynchronous tile pipelines explicit: producer and consumer tokens, generic async completion tokens, pipeline iterators, agent metadata, and layout-bearing value conventions. These types let passes reason about stage ownership, agent boundaries, region yields, and memory ordering before the IR is flattened into LLVM and NVVM operations.

Most TileAS types are control and scheduling types, not runtime heap objects — SSA-level contracts that verifiers, schedulers, and lowerings consume.

## Pipeline Types

| Type | Role |
|---|---|
| `PipelineProducerTokenType` | producer-side ownership token; acquired before writing a stage and consumed by commit |
| `PipelineConsumerTokenType` | consumer-side ownership token; produced by wait and consumed by release |
| `AsyncTokenType` | generic completion token for async copy, async dot, and other asynchronous work |
| `PipelineIteratorType` | rotating stage iterator that carries the element type and stage position through control flow |

Producer and consumer tokens carry no payload data — they represent ordering and ownership. Payload values move through region arguments and yields.

```c
typedef struct PipelineState {
    uint32_t stage_count;
    uint32_t producer_group;
    uint32_t consumer_group;
    Value storage;
} PipelineState;

typedef struct PipelineIterator {
    Type element_type;
    uint32_t stage;
    uint32_t phase;
    IteratorKind kind;
} PipelineIterator;
```

`PipelineIteratorType` is the only pipeline type with meaningful structural payload. Producer-side and consumer-side iterators stay distinct because they participate in different handshakes, but both unwrap to the element type yielded through the pipeline region.

### Type Storage and Uniquing

Pipeline types are routed through the context `StorageUniquer` documented in [Storage Uniquer and Context Impl](../../mlir-infra/storage-uniquer-and-context-impl.md). Producer/consumer tokens and the generic async token are parameterless and resolve to a single canonical storage per context; the iterator type carries a wrapped element type and is keyed on that pointer.

| Type | Uniquer key |
|---|---|
| `PipelineProducerTokenType` | parameterless (single canonical storage per context) |
| `PipelineConsumerTokenType` | parameterless |
| `AsyncTokenType` | parameterless |
| `PipelineIteratorType` | `(element_type)` pointer |

Producer-side and consumer-side token classes share storage shape but carry distinct TypeIDs, so pointer-identity dispatch in the verifier and lowering tells them apart without parsing names. The iterator TypeID is consulted by the region-op verifier template (see [Verifiers](verifiers.md#region-op-verifier-template)) before producer-type comparison; the unwrap always runs on the block-argument side, never on the producer-type list.

## Iterator Propagation

Pipeline iterators must survive structured control flow. Loops carry them as iter-args; branches must yield the same iterator type from both arms.

The iterator type encodes four logical fields:

| Field | Meaning |
|---|---|
| `element_type` | The payload type the iterator carries (typically the tile type yielded by the producer region). |
| `count` | The number of distinct stages the iterator rotates through, fixed by `numStages` on the enclosing pipeline. |
| `stride` | The advance step taken by `inc_iter` (always one in current TileAS). |
| `address_space` | The memory space the iterator's payload references (shared, tensor, or register). |

Propagation through structured control flow obeys explicit rules:

1. **Async producer/consumer ops** preserve `count` and `stride` but may transform `address_space`. A producer region that materializes its payload into shared memory exposes a shared-space iterator to the consumer region; a consumer region that copies the payload into registers exposes a register-space iterator to whatever consumes the consumer's yield.
2. **Reduction and scan ops** divide `count` by the reduction factor when the reduction collapses an entire stage dimension. The verifier rejects a reduction whose factor does not evenly divide `count`.
3. **Structured branches** must yield iterators that agree on all four fields. `scf.if` with a `PipelineIteratorType` result requires both arms' yields to match.
4. **Loops** carry the iterator unchanged as an iter-arg. The loop-coalescing pattern in [folds-and-mem-consistency.md](folds-and-mem-consistency.md) rejects coalescing a loop that carries an iterator iter-arg because the merged loop's iteration count would no longer match the iterator's `count`.

```c
LogicalResult verify_iterator_merge(Value lhs, Value rhs) {
    if (!isa<PipelineIteratorType>(lhs.get_type())) {
        return failure();
    }
    if (lhs.get_type() != rhs.get_type()) {
        return failure();
    }
    return success();
}

PipelineIteratorType propagate_through_async(PipelineIteratorType in,
                                             AddressSpace producer_space) {
    return PipelineIteratorType(in.element_type, in.count, in.stride, producer_space);
}

PipelineIteratorType propagate_through_reduction(PipelineIteratorType in,
                                                 uint32_t reduction_factor) {
    require(in.count % reduction_factor == 0);
    return PipelineIteratorType(in.element_type,
                                in.count / reduction_factor,
                                in.stride,
                                in.address_space);
}
```

Treat iterator propagation as part of queue-to-pipeline lowering. Delaying it until final lowering means the scheduler cannot reliably assign stage meaning to merged SSA values, and the verifier loses the ability to reject a malformed reduction-over-stages pattern at the right phase.

## Agent Types

Agent metadata describes warp-specialized execution regions. It rides on `agent_switch` and related execute operations rather than appearing as ordinary SSA values.

| Agent field | Meaning |
|---|---|
| agent body regions | One region per logical agent; each region runs on a disjoint subset of the warp budget. |
| `num_agents_per_group` | Number of agents in the group; controls how the launch's warp budget partitions. |
| `max_regs` | Per-agent register budget hint; quantizes to a warp-count-like unit. |
| `isolated` | Whether an agent's region sees the surrounding SSA scope or runs in an isolated value-space. |
| warp count | Derived from register budget or inherited from enclosing launch metadata. |

The register budget quantizes to a warp-count-like unit. A sentinel value means "inherit the enclosing budget"; the scheduler and execution-unit propagation passes resolve that placeholder against the actual kernel configuration later.

```c
uint32_t quantize_agent_warps(uint32_t max_regs) {
    if (max_regs == INHERIT_REGISTER_BUDGET) {
        return INHERIT_REGISTER_BUDGET;
    }
    return 8 * ceil_div(max_regs + 7, 8);
}
```

The agent verifier (see [Verifiers](verifiers.md#agent-switch-verification)) checks two structural facts: all agent regions in one group agree on their warp count (or inherit it), and the sum of resolved warp counts does not exceed the enclosing launch budget. Once resolved, the warp count drives both the launch geometry recorded in the GPU module attributes and the per-agent register-allocation decisions taken by NVGPU lowering.

## Layout-Carrying Values

`nv_tileas` does not lean on one monolithic layout type. Layout rides on the value type plus attributes such as `atom`, layout descriptors, memory-space information, and operand segment sizes.

| Layout carrier | Purpose |
|---|---|
| value type | element type, rank, shape, and memory-space view |
| `atom` attribute | selects the copy, MMA, TMA, or reduce atom used by the operation |
| layout descriptor | describes register/shared/tensor-memory arrangement |
| operand segments | separate view operands, coordinate operands, offsets, and tokens |

One operation describes both a logical tile and the hardware atom that will eventually move or compute it.

## Producer Interface

Producer-like operations expose their producer region through a private interface. The behavior is simple:

- `produce_one` and `produce_one_async` expose the region that generates producer values.
- `producer_write` exposes the region that writes into pipeline storage.
- a producer marker lets later passes identify producer boundaries without rediscovering the operation shape.

```c
Region *get_producer_region(Operation *op) {
    if (isa<ProduceOneOp>(op) || isa<ProduceOneAsyncOp>(op)) {
        return &op->region(0);
    }
    if (isa<ProducerWriteOp>(op)) {
        return &op->region(0);
    }
    return NULL;
}
```

## Agent-Like Interface

Agent-like operations expose body regions and warp-count information. `agent_switch` is the primary TileAS user; the upstream execute operation shares the same conceptual interface before queue-to-pipeline lowering.

```c
SmallVector<uint32_t> get_agent_warp_counts(AgentLikeOp op) {
    SmallVector<uint32_t> counts;
    for (AgentBody body : op.agent_bodies()) {
        counts.push_back(resolve_or_inherit_warp_count(body));
    }
    return counts;
}
```

Verification must ensure every path crossing an agent boundary agrees on the agent budget lowering will use.

## Yield Terminator Interface

Both ordinary TileAS `yield` and async pipeline `yield` act as region-branch terminators. Their successor regions and operands delegate to the enclosing region operation.

The rule stays local: a pipeline region decides what its yield values mean; the terminator just supplies the yielded operands.

```c
SuccessorInfo get_successors(YieldOp yield) {
    Operation *parent = yield.parent_region_op();
    return parent->region_branch_successors(yield.operands());
}
```

## Cross-References

[op-roster-and-builders.md](op-roster-and-builders.md) shows the operations that consume and produce each type. [verifiers.md](verifiers.md) details the region-op verifier template that validates iterator unwrap and producer-type agreement. [folds-and-mem-consistency.md](folds-and-mem-consistency.md) describes the rewrites that respect the iterator-propagation rules above.

