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

### TypeStorage and Uniquer Keying

Pipeline types are routed through the context `StorageUniquer` documented in [Storage Uniquer and Context Impl](../../mlir-infra/storage-uniquer-and-context-impl.md). Producer/consumer tokens and the generic async token are parameterless and resolve to a single canonical storage per context; the iterator type carries a wrapped element type and is keyed on that pointer.

| Type | TypeID singleton | Storage size | Uniquer key |
|---|---|---:|---|
| `PipelineProducerTokenType` | dialect TypeID slot | 0x18 | parameterless |
| `PipelineConsumerTokenType` | dialect TypeID slot | 0x18 | parameterless |
| `AsyncTokenType` | dialect TypeID slot | 0x18 | parameterless |
| `PipelineIteratorType` | `&unk_5B45A60` | 0x20 | `(element_type)` pointer |

```c
typedef struct PipelineTokenStorage {
    /*+0x00*/ BaseStorage    base;             // vtable, ctx, hash bucket
} PipelineTokenStorage;

typedef struct PipelineIteratorStorage {
    /*+0x00*/ BaseStorage    base;
    /*+0x18*/ Type           element_type;     // payload carried through SCF
} PipelineIteratorStorage;
```

Producer-side and consumer-side token classes share storage shape but carry distinct TypeIDs, so pointer-identity dispatch in the verifier and lowering tells them apart without parsing names. The iterator TypeID `&unk_5B45A60` is consulted by the verifier-template at `sub_1496C90` (see [Verifiers](verifiers.md#region-op-verifier-quintuplet)) before producer-type comparison; the unwrap always runs on the block-argument side, never on the producer-type list.

## Iterator Propagation

Pipeline iterators must survive structured control flow. Loops carry them as iter-args; branches must yield the same iterator type from both arms.

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
```

Treat iterator propagation as part of queue-to-pipeline lowering. Delaying it until final lowering means the scheduler cannot reliably assign stage meaning to merged SSA values.

## Agent Types

Agent metadata describes warp-specialized execution regions. It rides on `agent_switch` and related execute operations rather than appearing as ordinary SSA values.

| Agent field | Meaning |
|---|---|
| agent body regions | regions executed by each logical agent |
| `num_agents_per_group` | number of agents in the group |
| `max_regs` | per-agent register budget hint |
| warp count | derived from register budget or inherited from enclosing launch metadata |

The register budget quantizes to a warp-count-like unit. A sentinel value means "inherit the enclosing budget"; the scheduler and execution-unit propagation passes resolve that placeholder against the actual kernel configuration later.

```c
uint32_t quantize_agent_warps(uint32_t max_regs) {
    if (max_regs == INHERIT_REGISTER_BUDGET) {
        return INHERIT_REGISTER_BUDGET;
    }
    return 8 * ceil_div(max_regs + 7, 8);
}
```

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

## Reimplementation Invariants

- Producer and consumer tokens model ownership and ordering, not payload data.
- `PipelineIteratorType` must be preserved through loops and branches.
- Branches that merge pipeline iterators must yield identical iterator types.
- Agent metadata must preserve body regions, group count, and register budget.
- Layout information is the combination of value type, atom attribute, descriptor, and operand segments.
- Yield terminators delegate successor semantics to their parent region operation.

## Reimplementation Checklist

1. Implement producer, consumer, async token, and iterator types.
2. Implement iterator unwrap and merge checks.
3. Model agent metadata on `agent_switch` and execute-like operations.
4. Expose producer regions through a producer interface.
5. Expose agent bodies and warp counts through an agent-like interface.
6. Treat layout as value type plus attributes, not as a single universal layout type.
