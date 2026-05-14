# Lowering: cuda_tile to nv_tileaa

## Abstract

`ConvertCudaTileToTileAA` is the first lowering pass in the tileiras pipeline and the only one that translates from a publicly-defined dialect. It rewrites `cuda_tile` — the bytecode-input form users author against — into the internal `nv_tileaa` dialect every subsequent pass operates on. No `cuda_tile.*` operation may survive this pass.

The conversion is partial. The pass loads six legal dialects, marks `cuda_tile` illegal, attaches a dynamic-legality predicate to `ub.poison`, registers three type-conversion functor pairs, and applies a pattern bank assembled by three independent populators in a fixed order.

## Pass Driver

The driver at `sub_5FC1C0` (1314 B, 39 basic blocks) has a `runOnOperation` body that reads the stored `--compute-capability` option, builds the conversion target, populates the three pattern groups, and invokes `applyPartialConversion` through the `sub_36F9730` / `sub_36CB0C0` pair. Two diagnostics escape, both at severity 259 (`Error`): `"invalid or missing --compute-capability option"` when the option parses as malformed, and `"failed to convert cuda_tile to nv_tileaa"` when partial conversion fails to legalize every `cuda_tile.*` op.

```c
LogicalResult convertCudaTileToTileAA(ModuleOp mod) {
    RewritePatternSet patterns;
    sub_5EBED0(patterns);                      // Part A
    sub_5F8DC0(patterns);                      // Part B
    sub_5F8970(patterns);                      // Part C
    ConversionTarget target = buildTarget();
    sub_36F9730(patterns, /*frozen=*/&frozen);
    if (failed(sub_36CB0C0(mod, target, frozen))) {
        return emit("failed to convert cuda_tile to nv_tileaa");
    }
    return success();
}
```

Module enumeration runs through `sub_5C6420`, a recursive op-tree walker, with a predicate (`sub_5C6610`) that collects only ops whose TypeID matches the `cuda_tile.module` descriptor. Collected modules land in a `SmallVector<Operation *, 6>` whose 48-byte inline buffer fits the common case of one nested module per bytecode input.

## Input and Output Dialects

| Direction | Surface |
|---|---|
| input ops | `cuda_tile.*` (all executable ops), `ub.poison` (dyn-legal) |
| input types | `cuda_tile::TileType`, `cuda_tile::PointerType`, `cuda_tile::TokenType` |
| output ops (legal after this pass) | `arith`, `nv_tileaa`, `func`, `gpu`, `scf`, `math`, plus already-legal `llvm.struct` and `llvm.ptr` shapes produced by type materialisation |
| output types | tile types become `llvm.struct<...>`, pointer types become `llvm.ptr`, token types become `llvm.token` (via the materialiser triple) |

The canonical rewrite shape for a one-to-one Part-A pattern is:

```text
input  : %r = cuda_tile.addi %a, %b : <tile shape>
output : %r = nv_tileaa.addi %a, %b : <tile shape>
```

Region-bearing ops (`cuda_tile.reduce`, `cuda_tile.scan`) keep their region intact; only block-argument types and yielded values flow through the TypeConverter.

## Three-Populator Structure

Three populators build the pattern set in a deterministic order. Parts A and B are mutually independent — they could run in parallel at the source level, but the binary calls them sequentially to keep behaviour reproducible. Part C runs after both because its patterns depend on the type-conversion and layout decisions A and B have already published.

| Part | Populator   | Size    | Pattern count | Role |
|------|-------------|--------:|--------------:|------|
| A    | `sub_5EBED0`| 13.4 KB |       ~45     | Arithmetic, comparison, conversion, indexing, structured control flow |
| B    | `sub_5F8DC0`| 13.3 KB |       ~34     | Memory, pointer, token, view, partition |
| C    | `sub_5F8970`|  1.1 KB |         4     | mma, reduce, scan, transcendental specialists |

Part A registers hand-written `OpConversion` patterns whose pretty-names live in anonymous namespaces (`{anonymous}::AddIOpConversion`, `{anonymous}::ReduceOpConversion`, and so on). Part B mixes two-thirds template-generated `mlir::GenericConversion<cuda_tile::XOp, target::YOp>` patterns with one-third custom view/token/entry patterns. Part C is four inlined specialists for operations whose lowering depends on layout choices A and B have already locked in: `mmaf`, `mmai`, `reduce`, and `scan`.

## Singleton Pattern Adders

Eight 480-B trampolines at `sub_5EAFD0..sub_5EBCF0` expose individual patterns to downstream callers (CudaTileOptimizer tests and rsqrt/fma fusion passes). Each is a byte-identical wrapper that allocates a 0x68-B `OpConversionPattern`, stamps the vtable, and pushes it onto the RewritePatternSet through the per-class `_M_realloc_insert` trampoline — the same pattern documented for the GenericOpPattern arith bank in [pattern-set-and-typeconverter.md](pattern-set-and-typeconverter.md). The eight adders:

| Trampoline   | cuda_tile op    | Pattern class             | Vtable     |
|--------------|-----------------|---------------------------|------------|
| `sub_5EAFD0` | `trunci`        | `TruncIOpConversion`      | `0x59A8200`|
| `sub_5EB1B0` | `rsqrt`         | `RsqrtOpConversion`       | `0x59A8A20`|
| `sub_5EB390` | `maxi`          | `MaxIOpConversion`        | `0x59A8340`|
| `sub_5EB570` | `itof`          | `IToFOpConversion`        | `0x59A81B0`|
| `sub_5EB750` | `global`        | `GlobalOpConversion`      | `0x59A8110`|
| `sub_5EB930` | `fma`           | `FmaOpConversion`         | `0x59A7EE0`|
| `sub_5EBB10` | `constant`      | `ConstantOpConversion`    | `0x59A7BC0`|
| `sub_5EBCF0` | `assume`        | `AssumeOpConversion`      | `0x59A7990`|

None of these eight ops appears in the inline rosters of populators A or B; the trampolines are the only registration path that brings them into a pattern set.

## Type-Converter Functor Triple

Three `(addConversion, addMaterialization)` functor pairs register through `sub_5F5AC0` before the populators run. Materializations bridge values during partial conversion only; they should not survive later canonicalization.

| Functor pair                  | Source type                       | Target type           | Materializer role |
|-------------------------------|-----------------------------------|-----------------------|-------------------|
| `(sub_5C5A60, sub_5DD280)`    | `cuda_tile` `TileType`            | `llvm.struct<...>`    | Source materialiser |
| `(sub_5C5A90, sub_5C6220)`    | `cuda_tile` `PointerType`         | `llvm.ptr`            | Target materialiser |
| `(sub_5C5AC0, sub_5D8DB0)`    | `cuda_tile` `TokenType`           | `llvm.token`          | Source materialiser |

Splitting source from target materialisers preserves token ordering and view identity for the scheduler, which still needs to reason about memory dependences before NVVM lowering flattens tokens into integers.

## Legal-Dialect Vector

Part B materialises the legal-dialect set inline as a `SmallVector<StringRef, 6>` and hands it to `sub_36B4F90(target, vec, 6, kind=0)`. These six dialects stay legal for the whole pass:

```text
{ "arith", "nv_tileaa", "func", "gpu", "scf", "math" }
```

The same routine adds `cuda_tile` as a fully-illegal dialect with `kind=2`. `ub.poison` is registered separately through `sub_36C1890` as a dynamically-legal op whose predicate pair is `{sub_5C5800, sub_5C5860}` — the predicate returns true when the poison's result type is already a legal `nv_tileaa` primitive, false when it still needs to flow through the standard cast-elimination path.

## Pattern-Bank Layout

The 42-row pattern-class vtable bank runs from `0x59A91A0` to `0x59A9AA8`. The row count splits:

- 28 vtables for the hand-written Part-A OpConversions whose pretty-names live in anonymous namespaces.
- 14 vtables for the memory/token/view custom patterns in Part B (the remaining Part-B patterns are GenericConversion instantiations that share a single template-vtable family).
- 4 inlined specialist vtables for the Part-C `mma`, `reduce`, `scan`, and transcendental patterns.

Part A registers more patterns than vtables because several of its rewrites get inlined directly into the populator body rather than earning their own pattern class. The 42 distinct `_M_realloc_insert` instantiations at `0x5D94A0..0x5DD120` exist for the same reason every `OpConversion` class has its own type — distinct C++ types yield distinct `unique_ptr` deleter vtables, which forces a unique `_M_realloc_insert<unique_ptr<T>>` instantiation even though the bodies are functionally identical.

## Region Rewrites

Region-bearing operations must preserve block-argument order, terminator meaning, and yielded value types. The pattern body cannot use the standard inline-region helper because block-argument types must flow through the same `TypeConverter` the pass already owns.

```c
LogicalResult lower_region_op(Operation *src, OperationName dst,
                              ConversionPatternRewriter &rw,
                              const TypeConverter &types) {
    OperationState state(src->loc(), dst);
    state.add_operands(convert_operands(src->operands(), rw, types));
    state.add_types(convert_types(src->result_types(), types));
    state.add_attributes(copy_semantic_attrs(src));

    for (Region &region : src->regions()) {
        Region *new_region = state.add_region();
        clone_region_with_converted_block_args(region, new_region, types, rw);
    }

    Operation *replacement = rw.create(state);
    rw.replace_op(src, replacement->results());
    return success();
}
```

`cuda_tile.reduce` and `cuda_tile.scan` are the important examples. Their combiner regions stay structured, but yielded values and block-argument types must convert in the same step, or later `nv_tileaa` verification will see a region signature that no longer matches its parent op.

## Tokens and Atomics

Token-aware operations stay explicit in the IR rather than collapsing immediately to NVVM. Loads, stores, atomic compare-and-swap, atomic read-modify-write, token creation, and token join all become `nv_tileaa` operations that still expose memory dependences. The downstream scheduler and async-pipeline passes reason about those dependences before LLVM/NVVM lowering flattens tokens into integers.

```c
TileAAToken lower_join_tokens(ValueRange tokens, OpBuilder &b) {
    if (tokens.empty()) {
        return b.create<nv_tileaa::CreateNullTokenOp>().getResult();
    }
    if (tokens.size() == 1) {
        return cast<TileAAToken>(tokens.front());
    }
    return b.create<nv_tileaa::JoinMemTokenOp>(tokens).getResult();
}
```

## Pipeline Handoff

The pass establishes the alias and view shapes that warp-specialised producer/consumer rewriting relies on later, but assigns no final layouts. It keeps enough structure around load/store views, atomic-token operations, and tensor partitions for TileAS layout assignment to insert `nv_tileas.view` and `nv_tileas.convert_layout` at producer and consumer boundaries. The invariant: a view produced here must still identify the same memory object, shape, layout intent, and token ordering when it reaches TileAS layout assignment.

## Failure Modes

The pass fails with a user-facing diagnostic when:

- compute capability is missing or malformed (`"invalid or missing --compute-capability option"`);
- partial conversion leaves a residual `cuda_tile.*` op (`"failed to convert cuda_tile to nv_tileaa"`);
- a type materialisation cannot bridge a value across the boundary;
- a region rewrite would produce mismatched block arguments or terminators.

## Reimplementation Invariants

- Run Part A, Part B, Part C in that order. A and B are independent; C depends on both.
- Register the three type-converter functor pairs before any populator runs.
- Keep the legal-dialect vector at exactly six entries and `cuda_tile` strictly illegal.
- Mark `ub.poison` dynamically legal with a predicate keyed on result-type legality.
- Preserve token ordering and view identity for later scheduling and layout passes.
- Verify that no `cuda_tile` operation remains after conversion before signalling success.

## Cross-References

[Pattern Set and Type Converter](pattern-set-and-typeconverter.md) documents the shared `OpConversion` 0x68-B object layout and the `_M_realloc_insert` trampoline family. [TileAA to TileAS](tileaa-to-tileas.md) is the next lowering stage and is where SM-specific copy, MMA, and TMA decisions begin.
