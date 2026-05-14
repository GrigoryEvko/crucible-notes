# MLIR Infrastructure Overview

## Abstract

TileIR rides on top of a standard MLIR substrate: 0x48-byte `Operation` headers, a two-level
`StorageUniquer` gateway at `sub_4497E40` that interns every Type and Attribute, an `InterfaceMap`
keyed on TypeID sentinel addresses, four rewrite-pattern shapes (A/B/C/D at 0x60/0x68/0x70/0x78
bytes), and a 208-byte `Diagnostic` body with a 4-slot inline argument buffer. The dialect pages
assume this substrate is in place; this page is the index that names each piece, links to the
reimplementation-grade reference, and pins the invariants that the whole stack depends on.

The substrate is statically linked once and shared by `cuda_tile`, `nv_tileas`, `nv_tileaa`, `cute`,
`cute_nvgpu`, `cutlass`, `nvvm`, `llvm`, and the standard builtin / func / arith / scf / vector /
memref / cf / math / pdl dialects. There is one walker driver, one pattern application loop, one
uniquer gateway, and one diagnostic engine for the entire toolchain.

## Operation Model

Every IR node in TileIR is an `Operation` — a uniform record carrying a name, operands, results, attributes, successors, regions, and a parent block. Region-bearing operations own nested blocks. Terminators close regions and pass yielded values back to the parent. Dialects supply names and verifier logic; the substrate supplies storage and traversal.

```c
typedef struct Operation {
    OperationName name;
    ValueRange operands;
    ResultRange results;
    DictionaryAttr attrs;
    RegionRange regions;
    Block *parent_block;
} Operation;
```

Byte offsets are an implementation detail; semantic accessors are not. Passes reach operands, results, regions, and attributes through stable APIs rather than hard-coded offsets. Reverse-engineering notes name offsets to identify behaviour; a public reimplementation works through the MLIR object model.

## Storage Uniquing

Types, attributes, locations, affine maps, and most dialect-specific values are immutable and uniqued. A constructor either returns the existing canonical object from the context or inserts a new one into the context's storage table — pointer equality is identity.

```c
Type get_or_create_type(Context *ctx, TypeKey key) {
    uint64_t hash = hash_type_key(key);

    Type existing = lookup_type(ctx, key, hash);
    if (existing != NULL) {
        return existing;
    }

    Type created = allocate_type_storage(ctx, key);
    insert_type(ctx, key, hash, created);
    return created;
}
```

Hence the immutability rule: mutate a uniqued payload after construction and every equality test, every keyed map, every cache breaks at once.

## TypeID and Interfaces

TypeID-style identity distinguishes concrete operations, types, attributes, traits, and interfaces. Interfaces sit on top: small concept tables attached to an operation, type, attribute, or dialect that let generic passes ask semantic questions without naming a concrete C++ class.

Examples:

- a memory-effect interface tells canonicalization whether an operation can be reordered,
- an infer-type interface lets builders derive result types from operands,
- a region-branch interface tells control-flow utilities where a region terminator can transfer control,
- a dialect conversion interface supplies rewrite patterns for lowering to LLVM,
- a target-attribute interface tells the backend how to construct a target machine.

Keep interface dispatch semantic. A pass should depend on "this operation has memory effects" or "this dialect can lower to LLVM" — never on the operation's concrete registration address.

## Patterns and Conversion

Almost every lowering in TileIR is a rewrite pattern. A pattern matches a root operation, checks side conditions, and replaces it with zero or more new operations. Conversion patterns layer type conversion and legality tracking on top of the same machinery.

```c
LogicalResult match_and_rewrite(Operation *op, Rewriter *rewriter) {
    if (!matches_expected_shape(op)) {
        return failure();
    }

    SmallVector<Value> new_operands = convert_operands(op->operands);
    Operation *replacement = build_lowered_op(rewriter, op->loc, new_operands);
    rewriter->replace_op(op, replacement->results);
    return success();
}
```

Correctness hinges on three things: side effects, region semantics, and type invariants. A locally type-correct rewrite is still wrong if it slides a memory operation across a synchronisation edge or drops a yielded value out of a region.

## Diagnostics

Diagnostics are part of the compiler contract, not an afterthought. A verifier or conversion pass owes the user three facts: which operation failed, which invariant was expected, and which value or attribute violated it. The obligation grows sharper in late layers like `nvvm`, where the original source operation has long since been lowered away.

```c
LogicalResult verify_tma_load(TmaLoadOp op) {
    if (!is_tma_descriptor(op.descriptor)) {
        return op.emit_error("expected a TMA descriptor operand");
    }

    if (!is_compatible_shared_memory_layout(op.destination)) {
        return op.emit_error("destination layout is not compatible with TMA");
    }

    return success();
}
```

Reserve diagnostics for user-actionable failures. Use assertions for impossible compiler-internal states — anything that fires one is a bug in the implementation.

## Async Values

TileAS scheduling introduces async coordination values — `Pipe_` and `Mutex_` — that record producer operations, consumer operations, stage/order metadata, and optional payload. Once the schedule is fixed, the scheduler materialises these into explicit communication between producer and consumer regions.

```c
typedef struct AsyncValue {
    OperationSet producers;
    OperationSet consumers;
    StageOrderList producer_orders;
    StageOrderList consumer_orders;
    Optional<int> slot_id;
} AsyncValue;
```

The non-negotiable invariant is identity stability across materialisation. Pre-`MaterializeSchedule`, async edges live behind opaque handles. Post-materialisation, those handles resolve to concrete `Pipe_` or `Mutex_` values with deterministic producer and consumer sets — never a different identity, never a different membership.

## Reimplementation Checklist

A reimplementation of the infrastructure layer should provide:

- operations with operands, results, attributes, regions, successors, and parent links,
- immutable uniqued types and attributes scoped to a context,
- TypeID-like identity for concrete dialect objects and interfaces,
- interface dispatch for memory effects, type inference, region branching, dialect conversion, and target attributes,
- pattern rewriting with safe replacement and erasure,
- conversion rewriting with type conversion and legality checks,
- diagnostics that preserve operation context,
- walkers over modules, regions, blocks, and operations,
- async coordination values used by the TileAS scheduler.

## Cross-links

- [Operation Layout](operation-layout.md) covers operation storage and traversal.
- [Storage Uniquer and Context Impl](storage-uniquer-and-context-impl.md) covers uniquing behavior.
- [Pattern Vtables and Shapes](pattern-vtables-and-shapes.md) covers rewrite-pattern object shapes.
- [Interface Vtables](interface-vtables.md) covers interface dispatch.
- [TypeID Sentinels and Anchors](typeid-sentinels-and-anchors.md) covers identity anchors.
- [Container Fingerprints](container-fingerprints.md) covers common map/set shapes.
- [Diagnostic ABI and Helpers](diagnostic-abi-and-helpers.md) covers diagnostic construction.
- [AsyncValue and BLAKE3 Interning](asyncvalue-and-blake3-interning.md) covers scheduler async values and content addressing.
